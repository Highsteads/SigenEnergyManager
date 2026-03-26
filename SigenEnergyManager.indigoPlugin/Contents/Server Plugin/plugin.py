#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    plugin.py
# Description: SigenEnergyManager - self-sufficiency battery management for
#              Sigenergy solar/battery systems. Replaces SigenergySolar v3.1.
#              Core philosophy: never import from grid unless battery cannot
#              reach next-day solar at minimum SOC. Export to prevent 100% cap.
# Author:      CliveS & Claude Sonnet 4.6
# Date:        26-03-2026
# Version:     1.0

import indigo
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone

# ============================================================
# Secrets (from master secrets.py - never committed to git)
# ============================================================

sys.path.insert(0, "/Library/Application Support/Perceptive Automation")
try:
    from secrets import (
        OCTOPUS_API_KEY, OCTOPUS_ACCOUNT, OCTOPUS_MPAN, OCTOPUS_SERIAL,
        SOLCAST_API_KEY, SOLCAST_SITE_1_ID, SOLCAST_SITE_2_ID,
        AXLE_API_KEY, AXLE_CLIENT_ID,
    )
except ImportError:
    OCTOPUS_API_KEY   = ""
    OCTOPUS_ACCOUNT   = ""
    OCTOPUS_MPAN      = ""
    OCTOPUS_SERIAL    = ""
    SOLCAST_API_KEY   = ""
    SOLCAST_SITE_1_ID = ""
    SOLCAST_SITE_2_ID = ""
    AXLE_API_KEY      = ""
    AXLE_CLIENT_ID    = ""

# Plugin modules
from sigenergy_modbus import SigenergyModbus
from solcast          import SolcastForecast
from octopus_api      import OctopusAPI, TARIFF_TRACKER
from battery_manager  import (
    BatteryManager, ManagerSnapshot, TariffData,
    ACTION_SELF_CONSUMPTION, ACTION_START_IMPORT, ACTION_STOP_IMPORT,
    ACTION_SCHEDULE_IMPORT, ACTION_START_EXPORT, ACTION_STOP_EXPORT,
)
from axle_api import AxleAPI

# ============================================================
# Constants
# ============================================================

PLUGIN_VERSION = "1.0"
PLUGIN_NAME    = "SigenEnergyManager"

# Polling intervals (seconds)
MODBUS_POLL_INTERVAL      = 60
MANAGER_EVAL_INTERVAL     = 900   # 15 minutes
SOLCAST_FETCH_INTERVAL    = 8640  # 2.4 hours (10 calls/day/site limit)
OCTOPUS_RATES_INTERVAL    = 1800  # 30 minutes
OCTOPUS_PROFILE_INTERVAL  = 86400 # 24 hours
VPP_POLL_NORMAL_INTERVAL  = 600   # 10 minutes
VPP_POLL_ACTIVE_INTERVAL  = 60    # 1 minute (near/during event)
ACCUMULATOR_SAVE_INTERVAL = 300   # 5 minutes

# SOC delta trigger for immediate manager re-evaluation (percent)
SOC_CHANGE_TRIGGER = 5.0

# VPP state machine values
VPP_IDLE         = "idle"
VPP_ANNOUNCED    = "announced"
VPP_PRE_CHARGING = "pre_charging"
VPP_ACTIVE       = "active"
VPP_COOLING_OFF  = "cooling_off"

# Axle VPP SOC calculation constants (from SigenergySolar)
VPP_DISCHARGE_EFFICIENCY = 0.97
VPP_RESERVE_KWH          = 12.0  # 4 overnight + 3 morning + 5 buffer
BATTERY_CAPACITY_KWH     = 35.04


def log(message, level="INFO"):
    """Custom log function with timestamp prefix."""
    from datetime import datetime
    indigo.server.log(f"[{datetime.now().strftime('%H:%M:%S')}] {message}", level=level)


class Plugin(indigo.PluginBase):
    """SigenEnergyManager Indigo Plugin.

    Manages a Sigenergy solar/battery system with self-sufficiency as the
    primary goal. Only imports from grid if battery cannot reach next day's
    solar generation window at the configured minimum SOC. Exports to
    prevent 100% battery cap during peak solar generation.
    """

    # ================================================================
    # Plugin Lifecycle
    # ================================================================

    def __init__(self, plugin_id, plugin_display_name, plugin_version, plugin_prefs):
        super().__init__(plugin_id, plugin_display_name, plugin_version, plugin_prefs)

        # Log startup banner inside __init__ (params available here, not in startup())
        indigo.server.log(
            f"[{datetime.now().strftime('%H:%M:%S')}] "
            f"{PLUGIN_NAME} v{PLUGIN_VERSION} initialising"
        )

        self.debug = plugin_prefs.get("showDebugInfo", False)

        # Data directory for cache files
        self.data_dir = self._get_data_dir()

        # Initialise module instances (configured properly in startup())
        self.modbus   = None
        self.solcast  = None
        self.octopus  = None
        self.manager  = BatteryManager()
        self.axle     = None

        # Latest data from each module
        self.latest_inverter_data = {}
        self.latest_forecast_data = {}
        self.latest_rates_data    = {}
        self.latest_decision      = None

        # Poll timers
        self.store                   = {}   # mutable state dict (replaces globals)
        self.store["last_modbus"]    = 0.0
        self.store["last_manager"]   = 0.0
        self.store["last_solcast"]   = 0.0
        self.store["last_octopus"]   = 0.0
        self.store["last_profile"]   = 0.0
        self.store["last_vpp"]       = 0.0
        self.store["last_acc_save"]  = 0.0
        self.store["last_soc"]       = 0.0  # for SOC delta trigger

        # Daily energy accumulators (kWh, reset at midnight)
        self.store["pv_daily_kwh"]          = 0.0
        self.store["grid_import_daily_kwh"] = 0.0
        self.store["grid_export_daily_kwh"] = 0.0
        self.store["home_daily_kwh"]        = 0.0
        self.store["peak_soc"]              = 0.0
        self.store["min_soc"]               = 100.0
        self.store["today_date"]            = datetime.now().strftime("%Y-%m-%d")

        # VPP state machine
        self.store["vpp_state"]          = VPP_IDLE
        self.store["vpp_active"]         = False
        self.store["vpp_event"]          = None
        self.store["vpp_pre_charge_soc"] = 0.0

        # Scheduled import state
        self.store["import_active"]          = False
        self.store["import_scheduled_time"]  = None
        self.store["import_target_soc"]      = 0.0

        # Export state
        self.store["export_active"]   = False

        # Consumption profile (48 slots)
        self.store["consumption_profile"] = []

        self._load_accumulators()

    def startup(self):
        log(f"{PLUGIN_NAME} v{PLUGIN_VERSION} starting")
        self._init_modules()
        self.solcast.load_correction_factor()
        log(f"{PLUGIN_NAME} ready")

    def shutdown(self):
        log(f"{PLUGIN_NAME} shutting down")
        if self.modbus and self.modbus.connected:
            # Return to self-consumption on shutdown
            try:
                self.modbus.set_self_consumption()
            except Exception:
                pass
            self.modbus.disconnect()
        self._save_accumulators()

    def deviceStartComm(self, dev):
        dev.stateListOrDisplayStateIdChanged()

    def deviceStopComm(self, dev):
        pass

    def runConcurrentThread(self):
        """Main 10-second polling loop."""
        try:
            while True:
                now = time.time()
                self._tick(now)
                self.sleep(10)
        except self.StopThread:
            pass

    # ================================================================
    # Main Poll Tick
    # ================================================================

    def _tick(self, now):
        """Called every 10 seconds. Dispatches all timed tasks."""
        # 1. Modbus poll
        if now - self.store["last_modbus"] >= MODBUS_POLL_INTERVAL:
            self._poll_modbus()
            self.store["last_modbus"] = now

        # 2. Battery manager evaluation (every 15 min OR on SOC delta)
        soc_now    = self.latest_inverter_data.get("batterySoc", 0.0)
        soc_delta  = abs(soc_now - self.store["last_soc"])
        if (now - self.store["last_manager"] >= MANAGER_EVAL_INTERVAL
                or soc_delta >= SOC_CHANGE_TRIGGER):
            self._evaluate_manager()
            self.store["last_manager"] = now
            self.store["last_soc"]     = soc_now

        # 3. Solcast forecast
        if now - self.store["last_solcast"] >= SOLCAST_FETCH_INTERVAL:
            self._refresh_solcast()
            self.store["last_solcast"] = now

        # 4. Octopus rates
        if now - self.store["last_octopus"] >= OCTOPUS_RATES_INTERVAL:
            self._refresh_octopus_rates()
            self.store["last_octopus"] = now

        # 5. Consumption profile (daily)
        if now - self.store["last_profile"] >= OCTOPUS_PROFILE_INTERVAL:
            self._refresh_consumption_profile()
            self.store["last_profile"] = now

        # 6. VPP polling (adaptive)
        vpp_interval = self._vpp_poll_interval()
        if now - self.store["last_vpp"] >= vpp_interval:
            self._poll_vpp()
            self.store["last_vpp"] = now

        # 7. Accumulator save
        if now - self.store["last_acc_save"] >= ACCUMULATOR_SAVE_INTERVAL:
            self._save_accumulators()
            self.store["last_acc_save"] = now

        # 8. Daily midnight tasks
        self._check_midnight()

        # 9. Check scheduled import
        self._check_scheduled_import()

    # ================================================================
    # Modbus Polling
    # ================================================================

    def _poll_modbus(self):
        """Read all inverter registers and update sigenergyInverter device states."""
        if not self.modbus:
            return

        data = self.modbus.read_all()
        if data is None:
            self._update_inverter_device_offline()
            return

        self.latest_inverter_data = data

        # Update daily energy accumulators
        self._accumulate_daily_energy(data)

        # Update device states
        self._update_inverter_device(data)

    def _accumulate_daily_energy(self, data):
        """Integrate instantaneous power readings into daily kWh totals."""
        interval_h = MODBUS_POLL_INTERVAL / 3600.0  # hours per poll

        pv_w   = data.get("pvPowerWatts", 0)
        grid_w = data.get("gridPowerWatts", 0)
        home_w = data.get("homePowerWatts", 0)

        self.store["pv_daily_kwh"]          += max(0, pv_w)   * interval_h / 1000.0
        self.store["home_daily_kwh"]        += max(0, home_w) * interval_h / 1000.0
        self.store["grid_import_daily_kwh"] += max(0, grid_w) * interval_h / 1000.0
        self.store["grid_export_daily_kwh"] += max(0, -grid_w) * interval_h / 1000.0

        soc = data.get("batterySoc", 0.0)
        if soc > self.store["peak_soc"]:
            self.store["peak_soc"] = soc
        if soc < self.store["min_soc"]:
            self.store["min_soc"] = soc

    # ================================================================
    # Manager Evaluation
    # ================================================================

    def _evaluate_manager(self):
        """Run the battery manager decision engine and act on the result."""
        if not self.latest_inverter_data:
            return

        soc_pct = self.latest_inverter_data.get("batterySoc", 0.0)
        prefs   = self.pluginPrefs

        # Build TariffData from latest rates
        tariff_data = self._build_tariff_data()

        # Build snapshot
        snapshot = ManagerSnapshot(
            current_soc_pct    = soc_pct,
            capacity_kwh       = float(prefs.get("batteryCapacityKwh", 35.04)),
            efficiency         = float(prefs.get("batteryEfficiency", 94)) / 100.0,
            dawn_target_pct    = float(prefs.get("dawnSocTarget", 10)),
            health_cutoff_pct  = float(prefs.get("batteryHealthCutoff", 10)),
            export_enabled     = prefs.get("exportEnabled", False),
            export_trigger_pct = float(prefs.get("exportTriggerSoc", 90)),
            max_export_kw      = float(prefs.get("maxExportKw", 4.0)),
            tariff             = tariff_data,
            forecast_p50       = self.latest_forecast_data.get("_hourly_p50_today", {}),
            forecast_p10       = self.latest_forecast_data.get("_hourly_p10_today", {}),
            dawn_times         = self.latest_forecast_data.get("_dawn_times", {}),
            consumption_profile = self.store.get("consumption_profile", []),
            now                = datetime.now(timezone.utc),
            vpp_active         = self.store["vpp_active"],
        )

        decision = self.manager.evaluate(snapshot)
        self.latest_decision = decision

        # Act on the decision
        self._act_on_decision(decision)

        # Update batteryManager device states
        self._update_manager_device(decision, snapshot)

    def _build_tariff_data(self):
        """Build a TariffData object from the latest Octopus rates."""
        rates   = self.latest_rates_data
        tariff_info = rates.get("tariff_info", {})
        tariff_key  = tariff_info.get("tariff_key", TARIFF_TRACKER)

        tracker = rates.get(TARIFF_TRACKER, {})
        tou     = rates.get(tariff_key, {})  # may be same as tracker or go/flux

        return TariffData(
            tariff_key      = tariff_key,
            today_rate_p    = tracker.get("today_p"),
            tomorrow_rate_p = tracker.get("tomorrow_p"),
            cheap_start     = tou.get("cheap_start"),
            cheap_end       = tou.get("cheap_end"),
            cheap_rate_p    = tou.get("cheap_p"),
            agile_slots     = rates.get("agile_slots", []),
        )

    def _act_on_decision(self, decision):
        """Translate a Decision into Modbus writes."""
        if not self.modbus:
            return

        action      = decision.action
        prev_import = self.store["import_active"]
        prev_export = self.store["export_active"]

        if action == ACTION_START_IMPORT:
            if not prev_import:
                log(f"[Manager] Starting grid import: {decision.reason}")
                power_w = min(decision.power_watts or 8000,
                              int(float(self.pluginPrefs.get("inverterMaxKw", 8.0)) * 1000))
                if self.modbus.force_charge(power_w):
                    self.store["import_active"]       = True
                    self.store["import_target_soc"]   = decision.target_soc_pct
                    self.store["export_active"]       = False
                    self._trigger_event("emergencyImportTriggered")

        elif action == ACTION_STOP_IMPORT:
            if prev_import:
                log("[Manager] Import complete - returning to self-consumption")
                self.modbus.set_self_consumption()
                self.store["import_active"] = False

        elif action == ACTION_SCHEDULE_IMPORT:
            # Store the scheduled time - checked in _check_scheduled_import
            self.store["import_scheduled_time"] = decision.scheduled_time
            self.store["import_target_soc"]     = decision.target_soc_pct
            if self.store.get("import_scheduled_logged") != str(decision.scheduled_time):
                log(f"[Manager] Import scheduled: {decision.reason}")
                self.store["import_scheduled_logged"] = str(decision.scheduled_time)

        elif action == ACTION_START_EXPORT:
            if not prev_export and not self.store["import_active"]:
                log(f"[Manager] Starting export: {decision.reason}")
                export_w = decision.power_watts or int(
                    float(self.pluginPrefs.get("maxExportKw", 4.0)) * 1000
                )
                if self.modbus.force_discharge(export_w):
                    self.store["export_active"] = True
                    self._trigger_event("exportStarted")

        elif action == ACTION_STOP_EXPORT:
            if prev_export:
                log("[Manager] Stopping export - returning to self-consumption")
                self.modbus.set_self_consumption()
                self.store["export_active"] = False
                self._trigger_event("exportStopped")

        elif action == ACTION_SELF_CONSUMPTION:
            # Clear any active import/export
            if prev_import or prev_export:
                log("[Manager] Returning to self-consumption")
                self.modbus.set_self_consumption()
                if prev_export:
                    self._trigger_event("exportStopped")
                self.store["import_active"] = False
                self.store["export_active"] = False

        # Check if active import has reached target SOC
        if self.store["import_active"]:
            current_soc = self.latest_inverter_data.get("batterySoc", 0.0)
            target_soc  = self.store["import_target_soc"]
            if current_soc >= target_soc:
                log(f"[Manager] Import target SOC {target_soc:.0f}% reached - stopping")
                self.modbus.set_self_consumption()
                self.store["import_active"]      = False
                self.store["import_target_soc"]  = 0.0

    def _check_scheduled_import(self):
        """Check if a scheduled import time has arrived."""
        scheduled = self.store.get("import_scheduled_time")
        if scheduled is None:
            return

        now_utc = datetime.now(timezone.utc)
        # Normalise scheduled time to UTC if naive
        if scheduled.tzinfo is None:
            import pytz
            scheduled = pytz.timezone("Europe/London").localize(scheduled).astimezone(timezone.utc)

        if now_utc >= scheduled:
            log(f"[Manager] Scheduled import window reached - starting import")
            target_soc = self.store.get("import_target_soc", 12.0)
            if self.modbus and self.modbus.force_charge(8000):
                self.store["import_active"]      = True
                self.store["import_target_soc"]  = target_soc
                self.store["import_scheduled_time"] = None
                self._trigger_event("emergencyImportTriggered")

    # ================================================================
    # Solcast Refresh
    # ================================================================

    def _refresh_solcast(self, force=False):
        """Fetch updated solar forecast from Solcast API."""
        if not self.solcast:
            return

        data = self.solcast.fetch_forecast(force=force)
        self.latest_forecast_data = data

        self._update_forecast_device(data)

        if self.debug:
            log(
                f"[Solcast] Today: {data.get('correctedTodayKwh', 0):.1f} kWh "
                f"(raw {data.get('todayKwh', 0):.1f}, bias {data.get('biasFactor', 1):.3f}), "
                f"Tomorrow: {data.get('correctedTomorrowKwh', 0):.1f} kWh"
            )

    # ================================================================
    # Octopus Refresh
    # ================================================================

    def _refresh_octopus_rates(self, force=False):
        """Fetch current tariff rates from Octopus API."""
        if not self.octopus:
            return

        try:
            tariff_info   = self.octopus.get_current_tariff(force=force)
            monitored     = self.octopus.get_all_monitored_rates(force=force)

            self.latest_rates_data = {
                "tariff_info": tariff_info,
                **monitored,
            }

            self._update_tariff_device(tariff_info, monitored)

            if self.debug:
                tracker = monitored.get("tracker", {})
                log(
                    f"[Octopus] Tariff: {tariff_info.get('display_name', '?')}, "
                    f"Tracker today: {tracker.get('today_p', '?')}p, "
                    f"tomorrow: {tracker.get('tomorrow_p', 'TBD')}p"
                )

        except Exception as e:
            log(f"[Octopus] Rate refresh error: {e}", level="ERROR")

    def _refresh_consumption_profile(self, force=False):
        """Fetch 30-day consumption profile from Octopus API."""
        if not self.octopus:
            return
        try:
            profile = self.octopus.get_consumption_profile(force=force)
            self.store["consumption_profile"] = profile
            if self.debug:
                daily_kwh = sum(profile)
                log(f"[Octopus] Consumption profile updated. Daily total: {daily_kwh:.1f} kWh")
        except Exception as e:
            log(f"[Octopus] Profile refresh error: {e}", level="ERROR")

    # ================================================================
    # VPP State Machine
    # ================================================================

    def _vpp_poll_interval(self):
        """Return adaptive VPP poll interval."""
        state = self.store["vpp_state"]
        event = self.store["vpp_event"]

        if state == VPP_ACTIVE:
            return VPP_POLL_ACTIVE_INTERVAL

        if event and state in (VPP_ANNOUNCED, VPP_PRE_CHARGING):
            start = event.get("start_time")
            if start:
                hours_away = (start - datetime.now(timezone.utc)).total_seconds() / 3600.0
                if hours_away <= 2.0:
                    return VPP_POLL_ACTIVE_INTERVAL

        return VPP_POLL_NORMAL_INTERVAL

    def _poll_vpp(self):
        """Poll Axle API and advance VPP state machine."""
        if not self.axle or not self.pluginPrefs.get("axleEnabled", False):
            return

        event = self.axle.get_next_event()
        now   = datetime.now(timezone.utc)

        current_state = self.store["vpp_state"]

        # Check if Axle has taken Sigen Cloud control (EMS mode = Remote EMS
        # but NOT because WE set it)
        ems_mode_str = self.latest_inverter_data.get("emsWorkMode", "")
        axle_in_control = (
            "Remote EMS" in ems_mode_str
            and not self.store["import_active"]
            and not self.store["export_active"]
            and current_state in (VPP_ACTIVE, VPP_PRE_CHARGING)
        )

        if event is None:
            # No upcoming event
            if current_state == VPP_ACTIVE:
                # Check if Axle has released control
                if not axle_in_control:
                    self._vpp_transition(VPP_COOLING_OFF)
                    self.store["vpp_active"] = False
            elif current_state == VPP_COOLING_OFF:
                # Short cool-off period then return to idle
                self._vpp_transition(VPP_IDLE)
                self._restore_discharge_cutoff()
            elif current_state != VPP_IDLE:
                self._vpp_transition(VPP_IDLE)
                self.store["vpp_active"] = False
            return

        # Event is scheduled
        start_time   = event["start_time"]
        end_time     = event["end_time"]
        hours_to_start = (start_time - now).total_seconds() / 3600.0

        if current_state == VPP_IDLE and hours_to_start > 0:
            self.store["vpp_event"] = event
            self._vpp_transition(VPP_ANNOUNCED)
            self._trigger_event("vppAnnounced")
            log(
                f"[VPP] Event announced: {start_time.strftime('%H:%M')} - "
                f"{end_time.strftime('%H:%M')} ({event['duration_hrs']:.1f}h)"
            )

        elif current_state == VPP_ANNOUNCED:
            if hours_to_start <= 1.0:
                # 1-hour warning
                log(f"[VPP] Event in {hours_to_start:.1f}h - preparing pre-charge")

            if hours_to_start <= 0.5:
                # Enter pre-charge
                self._start_vpp_precharge(event)

        elif current_state == VPP_PRE_CHARGING:
            required_soc = self.store["vpp_pre_charge_soc"]
            current_soc  = self.latest_inverter_data.get("batterySoc", 0.0)

            if current_soc >= required_soc or hours_to_start <= 0:
                # Pre-charge complete or event starting
                if self.modbus:
                    self.modbus.set_self_consumption()
                log(f"[VPP] Pre-charge complete. SOC: {current_soc:.0f}%")

            if hours_to_start <= 0:
                self._vpp_transition(VPP_ACTIVE)
                self.store["vpp_active"] = True
                self._trigger_event("vppStarted")
                log(f"[VPP] Event ACTIVE - Axle has control")

        elif current_state == VPP_ACTIVE:
            # Check for event end
            if now >= end_time:
                self._vpp_transition(VPP_COOLING_OFF)
                self.store["vpp_active"] = False
                self._trigger_event("vppEnded")
                log(f"[VPP] Event ended - restoring battery manager control")

        self._update_vpp_device()

    def _start_vpp_precharge(self, event):
        """Calculate and start pre-charge for VPP export event."""
        duration_hrs = event.get("duration_hrs", 1.0)
        cap_kwh      = float(self.pluginPrefs.get("batteryCapacityKwh", BATTERY_CAPACITY_KWH))
        max_export_kw = float(self.pluginPrefs.get("maxExportKw", 4.0))

        # Energy to export + reserve
        export_kwh     = max_export_kw * duration_hrs / VPP_DISCHARGE_EFFICIENCY
        required_kwh   = export_kwh + VPP_RESERVE_KWH
        required_soc   = min(100.0, (required_kwh / cap_kwh) * 100.0)
        required_soc   = max(required_soc, float(self.pluginPrefs.get("dawnSocTarget", 10)))

        reserve_soc    = (VPP_RESERVE_KWH / cap_kwh) * 100.0

        self.store["vpp_pre_charge_soc"] = required_soc

        log(
            f"[VPP] Pre-charging to {required_soc:.0f}% for {duration_hrs:.1f}h event "
            f"({export_kwh:.1f} kWh export + {VPP_RESERVE_KWH} kWh reserve)"
        )

        # Set discharge cutoff register for reserve protection
        if self.modbus:
            self.modbus.set_discharge_cutoff(reserve_soc)
            current_soc = self.latest_inverter_data.get("batterySoc", 0.0)
            if current_soc < required_soc:
                self.modbus.force_charge(8000)

        self._vpp_transition(VPP_PRE_CHARGING)

    def _restore_discharge_cutoff(self):
        """Restore discharge cutoff to the health floor after VPP."""
        if self.modbus:
            health_floor = float(self.pluginPrefs.get("batteryHealthCutoff", 10.0))
            self.modbus.set_discharge_cutoff(health_floor)
            log(f"[VPP] Discharge cutoff restored to {health_floor:.0f}%")

    def _vpp_transition(self, new_state):
        """Transition VPP state machine to a new state."""
        old_state = self.store["vpp_state"]
        self.store["vpp_state"] = new_state
        if self.debug:
            log(f"[VPP] State: {old_state} -> {new_state}")

    # ================================================================
    # Midnight Tasks
    # ================================================================

    def _check_midnight(self):
        """Run once-daily tasks at midnight."""
        today = datetime.now().strftime("%Y-%m-%d")
        if today == self.store["today_date"]:
            return  # Not yet midnight

        # New day
        yesterday = self.store["today_date"]
        log(f"Midnight: recording daily history for {yesterday}")

        # Capture morning forecast for bias correction (00:05 next run)
        self.solcast.capture_morning_forecast()

        # Write accuracy record for yesterday
        self.solcast.record_accuracy(self.store["pv_daily_kwh"])

        # Write daily history ring buffer
        self._write_daily_history(yesterday)

        # Reset accumulators
        self.store["pv_daily_kwh"]          = 0.0
        self.store["grid_import_daily_kwh"] = 0.0
        self.store["grid_export_daily_kwh"] = 0.0
        self.store["home_daily_kwh"]        = 0.0
        self.store["peak_soc"]              = 0.0
        self.store["min_soc"]               = 100.0
        self.store["today_date"]            = today

        self._save_accumulators()

    def _write_daily_history(self, date_str):
        """Append today's totals to the 365-day ring buffer."""
        record = {
            "date":                 date_str,
            "month":                date_str[:7],
            "pv_kwh":               round(self.store["pv_daily_kwh"], 2),
            "pv_forecast_kwh":      round(self.latest_forecast_data.get("todayKwh", 0.0), 2),
            "grid_import_kwh":      round(self.store["grid_import_daily_kwh"], 2),
            "grid_export_kwh":      round(self.store["grid_export_daily_kwh"], 2),
            "home_kwh":             round(self.store["home_daily_kwh"], 2),
            "battery_charge_kwh":   round(
                self.latest_inverter_data.get("batteryDailyChargeKwh", 0.0), 2
            ),
            "battery_discharge_kwh": round(
                self.latest_inverter_data.get("batteryDailyDischargeKwh", 0.0), 2
            ),
            "peak_soc":   round(self.store["peak_soc"], 1),
            "min_soc":    round(self.store["min_soc"], 1),
            "tariff":     self.latest_rates_data.get("tariff_info", {}).get("tariff_key", "?"),
            "rate_today_p": self.latest_rates_data.get("tracker", {}).get("today_p"),
            "import_events": 1 if self.store.get("had_import_today", False) else 0,
            "export_events": self.store.get("export_count_today", 0),
            "vpp_event":  self.store.get("had_vpp_today", False),
        }

        path    = os.path.join(self.data_dir, "daily_history.json")
        records = []
        try:
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    records = json.load(f)
        except Exception:
            pass

        records.append(record)
        if len(records) > 365:
            records = records[-365:]

        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(records, f, indent=2)
        except Exception as e:
            log(f"Cannot write daily history: {e}", level="ERROR")

        # Reset daily counters
        self.store["had_import_today"]   = False
        self.store["export_count_today"] = 0
        self.store["had_vpp_today"]      = False

    # ================================================================
    # Trigger Events
    # ================================================================

    def _trigger_event(self, event_id):
        """Fire an Indigo custom trigger event."""
        try:
            indigo.trigger.execute(event_id)
        except Exception as e:
            self.logger.debug(f"Trigger event {event_id}: {e}")

    # ================================================================
    # Device State Updates
    # ================================================================

    def _update_inverter_device(self, data):
        """Push Modbus data to sigenergyInverter device."""
        dev = self._find_device("sigenergyInverter")
        if not dev:
            return
        states = [
            {"key": "emsWorkMode",              "value": str(data.get("emsWorkMode", ""))},
            {"key": "gridSensorConnected",      "value": str(data.get("gridSensorConnected", False))},
            {"key": "gridPowerWatts",           "value": str(data.get("gridPowerWatts", 0))},
            {"key": "gridStatus",               "value": str(data.get("gridStatus", ""))},
            {"key": "batterySoc",               "value": str(data.get("batterySoc", 0.0))},
            {"key": "pvPowerWatts",             "value": str(data.get("pvPowerWatts", 0))},
            {"key": "batteryPowerWatts",        "value": str(data.get("batteryPowerWatts", 0))},
            {"key": "homePowerWatts",           "value": str(data.get("homePowerWatts", 0))},
            {"key": "plantRunningState",        "value": str(data.get("plantRunningState", ""))},
            {"key": "dischargeCutoffSoc",       "value": str(data.get("dischargeCutoffSoc", 0.0))},
            {"key": "batterySoh",               "value": str(data.get("batterySoh", 0.0))},
            {"key": "batteryTempC",             "value": str(data.get("batteryTempC", 0.0))},
            {"key": "batteryCellVoltage",       "value": str(data.get("batteryCellVoltage", 0.0))},
            {"key": "batteryMaxTempC",          "value": str(data.get("batteryMaxTempC", 0.0))},
            {"key": "batteryMinTempC",          "value": str(data.get("batteryMinTempC", 0.0))},
            {"key": "batteryDailyChargeKwh",    "value": str(data.get("batteryDailyChargeKwh", 0.0))},
            {"key": "batteryDailyDischargeKwh", "value": str(data.get("batteryDailyDischargeKwh", 0.0))},
            {"key": "pvDailyKwh",               "value": str(round(self.store["pv_daily_kwh"], 2))},
            {"key": "gridDailyImportKwh",       "value": str(round(self.store["grid_import_daily_kwh"], 2))},
            {"key": "gridDailyExportKwh",       "value": str(round(self.store["grid_export_daily_kwh"], 2))},
            {"key": "homeDailyKwh",             "value": str(round(self.store["home_daily_kwh"], 2))},
            {"key": "modbusConnected",          "value": "True"},
            {"key": "lastUpdate",               "value": data.get("lastUpdate", "")},
        ]
        dev.updateStatesOnServer(states)
        dev.updateStateImageOnServer(indigo.kStateImageSel.SensorOn)

    def _update_inverter_device_offline(self):
        """Mark inverter device as offline."""
        dev = self._find_device("sigenergyInverter")
        if dev:
            dev.updateStateOnServer("modbusConnected", value="False")
            dev.updateStateImageOnServer(indigo.kStateImageSel.SensorOff)

    def _update_manager_device(self, decision, snapshot):
        """Push battery manager decision state to batteryManager device."""
        dev = self._find_device("batteryManager")
        if not dev:
            return

        scheduled_str = ""
        if decision.scheduled_time:
            scheduled_str = decision.scheduled_time.strftime("%H:%M")

        action_display = {
            ACTION_SELF_CONSUMPTION: "Self Consumption",
            ACTION_START_IMPORT:     "Grid Import Active",
            ACTION_STOP_IMPORT:      "Import Stopping",
            ACTION_SCHEDULE_IMPORT:  "Import Scheduled",
            ACTION_START_EXPORT:     "Grid Export Active",
            ACTION_STOP_EXPORT:      "Export Stopping",
        }.get(decision.action, decision.action)

        states = [
            {"key": "managerStatus",       "value": "Running" if not self.store["vpp_active"] else "VPP Active"},
            {"key": "currentAction",       "value": action_display},
            {"key": "currentReason",       "value": decision.reason[:255]},
            {"key": "dawnViable",          "value": str(decision.dawn_viable)},
            {"key": "socAtDawn",           "value": str(round(decision.soc_at_dawn_kwh, 2))},
            {"key": "importActive",        "value": str(self.store["import_active"])},
            {"key": "importScheduled",     "value": str(bool(self.store["import_scheduled_time"]))},
            {"key": "importScheduledTime", "value": scheduled_str},
            {"key": "importKwh",           "value": str(round(decision.import_kwh, 2))},
            {"key": "exportActive",        "value": str(self.store["export_active"])},
            {"key": "exportKw",            "value": str(round(decision.export_kw, 1))},
            {"key": "tariffActive",        "value": snapshot.tariff.tariff_key},
            {"key": "rateToday",           "value": str(snapshot.tariff.today_rate_p or "")},
            {"key": "rateTomorrow",        "value": str(snapshot.tariff.tomorrow_rate_p or "")},
            {"key": "lastUpdate",          "value": datetime.now().strftime("%H:%M:%S")},
        ]
        dev.updateStatesOnServer(states)

    def _update_forecast_device(self, data):
        """Push Solcast forecast to solarForecast device."""
        dev = self._find_device("solarForecast")
        if not dev:
            return
        states = [
            {"key": "todayKwh",             "value": str(data.get("todayKwh", 0.0))},
            {"key": "tomorrowKwh",          "value": str(data.get("tomorrowKwh", 0.0))},
            {"key": "correctedTodayKwh",    "value": str(data.get("correctedTodayKwh", 0.0))},
            {"key": "correctedTomorrowKwh", "value": str(data.get("correctedTomorrowKwh", 0.0))},
            {"key": "biasFactor",           "value": str(data.get("biasFactor", 1.0))},
            {"key": "remainingTodayKwh",    "value": str(data.get("remainingTodayKwh", 0.0))},
            {"key": "currentHourWatts",     "value": str(data.get("currentHourWatts", 0))},
            {"key": "nextHourWatts",        "value": str(data.get("nextHourWatts", 0))},
            {"key": "forecastStatus",       "value": str(data.get("forecastStatus", ""))},
            {"key": "lastUpdate",           "value": data.get("lastUpdate", "")},
        ]
        dev.updateStatesOnServer(states)

    def _update_tariff_device(self, tariff_info, monitored):
        """Push Octopus tariff data to tariffMonitor device."""
        dev = self._find_device("tariffMonitor")
        if not dev:
            return

        tracker = monitored.get("tracker", {})
        go      = monitored.get("go", {})
        flux    = monitored.get("flux", {})

        states = [
            {"key": "tariffActive",      "value": tariff_info.get("display_name", "")},
            {"key": "rateToday",         "value": str(tracker.get("today_p", ""))},
            {"key": "rateTomorrow",      "value": str(tracker.get("tomorrow_p") or "")},
            {"key": "trackerRateToday",  "value": str(tracker.get("today_p", ""))},
            {"key": "trackerRateTomorrow", "value": str(tracker.get("tomorrow_p") or "")},
            {"key": "goOffPeakRate",     "value": str(go.get("cheap_p", ""))},
            {"key": "goStandardRate",    "value": str(go.get("standard_p", ""))},
            {"key": "goPeakRate",        "value": str(go.get("peak_p", ""))},
            {"key": "fluxOffPeakRate",   "value": str(flux.get("cheap_p", ""))},
            {"key": "fluxStandardRate",  "value": str(flux.get("standard_p", ""))},
            {"key": "fluxPeakRate",      "value": str(flux.get("peak_p", ""))},
            {"key": "lastUpdate",        "value": datetime.now().strftime("%H:%M:%S")},
        ]
        dev.updateStatesOnServer(states)

    def _update_vpp_device(self):
        """Push VPP state to axleVppMonitor device."""
        dev = self._find_device("axleVppMonitor")
        if not dev:
            return

        event         = self.store.get("vpp_event") or {}
        start_str     = ""
        end_str       = ""
        duration_hrs  = 0.0

        if event.get("start_time"):
            start_str    = event["start_time"].strftime("%H:%M %d/%m")
            end_str      = event["end_time"].strftime("%H:%M")
            duration_hrs = event.get("duration_hrs", 0.0)

        max_export_kw = float(self.pluginPrefs.get("maxExportKw", 4.0))
        earnings_est  = round(max_export_kw * duration_hrs * 1.00, 2)  # GBP1/kWh Axle rate

        states = [
            {"key": "vppStatus",      "value": "Active" if self.store["vpp_active"] else "Standby"},
            {"key": "vppState",       "value": self.store["vpp_state"]},
            {"key": "eventStartTime", "value": start_str},
            {"key": "eventEndTime",   "value": end_str},
            {"key": "preChargeRequired", "value": str(self.store["vpp_pre_charge_soc"])},
            {"key": "estimatedEarnings", "value": str(earnings_est)},
            {"key": "lastUpdate",     "value": datetime.now().strftime("%H:%M:%S")},
        ]
        dev.updateStatesOnServer(states)

    # ================================================================
    # Indigo Action Callbacks
    # ================================================================

    def actionForceGridImport(self, action):
        """Action: Force immediate grid import."""
        props     = action.props
        power_kw  = float(props.get("powerKw", 8.0))
        target_soc = float(props.get("targetSocPct", 80.0))
        log(f"[Action] Force grid import: {power_kw}kW to {target_soc:.0f}% SOC")
        if self.modbus and self.modbus.force_charge(int(power_kw * 1000)):
            self.store["import_active"]     = True
            self.store["import_target_soc"] = target_soc
            self.store["export_active"]     = False

    def actionForceExport(self, action):
        """Action: Force immediate grid export."""
        props    = action.props
        power_kw = float(props.get("powerKw", 4.0))
        log(f"[Action] Force export: {power_kw}kW")
        if self.modbus and self.modbus.force_discharge(int(power_kw * 1000)):
            self.store["export_active"]  = True
            self.store["import_active"]  = False

    def actionSetSelfConsumption(self, action):
        """Action: Return to self-consumption mode."""
        log("[Action] Set self-consumption mode")
        if self.modbus:
            self.modbus.set_self_consumption()
            self.store["import_active"] = False
            self.store["export_active"] = False

    def actionReturnToLocalEms(self, action):
        """Action: Disable Remote EMS and return to local inverter control."""
        log("[Action] Return to local EMS control")
        if self.modbus:
            self.modbus.return_to_local()
            self.store["import_active"] = False
            self.store["export_active"] = False

    def actionPauseManager(self, action):
        """Action: Pause battery manager."""
        log("[Action] Battery manager paused")
        self.store["manager_paused"] = True
        dev = self._find_device("batteryManager")
        if dev:
            dev.updateStateOnServer("managerStatus", value="Paused")

    def actionResumeManager(self, action):
        """Action: Resume battery manager."""
        log("[Action] Battery manager resumed")
        self.store["manager_paused"] = False
        dev = self._find_device("batteryManager")
        if dev:
            dev.updateStateOnServer("managerStatus", value="Running")

    def actionRefreshSolcast(self, action):
        """Action: Manual Solcast forecast refresh."""
        log("[Action] Manual Solcast refresh")
        self._refresh_solcast(force=True)
        self.store["last_solcast"] = time.time()

    def actionRefreshOctopus(self, action):
        """Action: Manual Octopus rates refresh."""
        log("[Action] Manual Octopus rates refresh")
        self._refresh_octopus_rates(force=True)
        self.store["last_octopus"] = time.time()

    # ================================================================
    # Indigo Menu Callbacks
    # ================================================================

    def menuRefreshAll(self, values_dict, menu_item_id):
        """Menu: Force refresh Solcast + Octopus + re-evaluate manager."""
        log("[Menu] Refresh All: fetching Solcast, Octopus and re-evaluating...")
        self._refresh_solcast(force=True)
        self.store["last_solcast"] = time.time()
        self._refresh_octopus_rates(force=True)
        self.store["last_octopus"] = time.time()
        self._evaluate_manager()
        self.store["last_manager"] = time.time()
        log("[Menu] Refresh All complete")
        return True

    def menuShowStatus(self, values_dict, menu_item_id):
        """Menu: Log current manager status to event log."""
        dev = self._find_device("batteryManager")
        if dev:
            action  = dev.states.get("currentAction", "unknown")
            reason  = dev.states.get("currentReason", "")
            soc_str = dev.states.get("socAtDawn", "")
            viable  = dev.states.get("dawnViable", "")
            sched   = dev.states.get("importScheduledTime", "")
            log(
                f"[Status] Action={action} | Reason={reason} | "
                f"DawnViable={viable} | SocAtDawn={soc_str}kWh"
                + (f" | ImportAt={sched}" if sched else "")
            )
        else:
            log("[Status] No batteryManager device found", level="WARNING")

        inv = self._find_device("sigenergyInverter")
        if inv:
            soc     = inv.states.get("batterySoc", "?")
            pv      = inv.states.get("pvPowerWatts", "?")
            grid    = inv.states.get("gridPowerWatts", "?")
            modbus  = inv.states.get("modbusConnected", "False")
            updated = inv.states.get("lastUpdate", "")
            log(
                f"[Status] SOC={soc}% | PV={pv}W | Grid={grid}W | "
                f"Modbus={'OK' if modbus == 'True' else 'OFFLINE'} | Updated={updated}"
            )
        return True

    def menuShowDailyHistory(self, values_dict, menu_item_id):
        """Menu: Log last 7 days from daily_history.json."""
        path = os.path.join(self.data_dir, "daily_history.json")
        if not os.path.exists(path):
            log("[History] No daily_history.json found yet", level="WARNING")
            return True

        try:
            with open(path, "r", encoding="utf-8") as f:
                records = json.load(f)
        except Exception as e:
            log(f"[History] Cannot read daily_history.json: {e}", level="ERROR")
            return True

        recent = records[-7:] if len(records) >= 7 else records
        log(f"[History] Last {len(recent)} days:")
        for r in reversed(recent):
            import_flag = " IMPORT" if r.get("import_events", 0) > 0 else ""
            export_flag = f" exports={r['export_events']}" if r.get("export_events", 0) > 0 else ""
            vpp_flag    = " VPP" if r.get("vpp_event") else ""
            log(
                f"  {r['date']}  PV={r.get('pv_kwh', 0):.1f}kWh "
                f"(fcst={r.get('pv_forecast_kwh', 0):.1f}) "
                f"Import={r.get('grid_import_kwh', 0):.2f} "
                f"Export={r.get('grid_export_kwh', 0):.2f} "
                f"Home={r.get('home_kwh', 0):.1f} "
                f"SOC {r.get('min_soc', 0):.0f}-{r.get('peak_soc', 0):.0f}%"
                f"{import_flag}{export_flag}{vpp_flag}"
            )
        return True

    def menuShowTariffRates(self, values_dict, menu_item_id):
        """Menu: Log current Tracker/Go/Flux rates from cached Octopus data."""
        rates = self.latest_rates_data
        if not rates:
            log("[Tariff] No rates cached yet — run Refresh All first", level="WARNING")
            return True

        tariff_info = rates.get("tariff_info", {})
        tracker     = rates.get("tracker", {})
        go          = rates.get("go", {})
        flux        = rates.get("flux", {})

        log(f"[Tariff] Active tariff: {tariff_info.get('display_name', '?')} "
            f"({tariff_info.get('tariff_key', '?')})")

        today_p    = tracker.get("today_p")
        tomorrow_p = tracker.get("tomorrow_p")
        if today_p is not None:
            log(f"[Tariff] Tracker today: {today_p:.2f}p/kWh" +
                (f"  tomorrow: {tomorrow_p:.2f}p/kWh" if tomorrow_p is not None else
                 "  tomorrow: not yet published"))
        else:
            log("[Tariff] Tracker: not available")

        if go:
            log(f"[Tariff] Go: off-peak={go.get('cheap_p', '?')}p "
                f"({go.get('cheap_start', '?')}-{go.get('cheap_end', '?')}) "
                f"standard={go.get('standard_p', '?')}p")
        if flux:
            log(f"[Tariff] Flux: off-peak={flux.get('cheap_p', '?')}p "
                f"({flux.get('cheap_start', '?')}-{flux.get('cheap_end', '?')}) "
                f"peak={flux.get('peak_p', '?')}p "
                f"standard={flux.get('standard_p', '?')}p")
        return True

    def menuShowVppStatus(self, values_dict, menu_item_id):
        """Menu: Log current VPP state and next event details."""
        state   = self.store.get("vpp_state", "idle")
        active  = self.store.get("vpp_active", False)
        event   = self.store.get("vpp_event") or {}
        log(f"[VPP] State={state} Active={'YES' if active else 'no'}")
        if event:
            start = event.get("start_time")
            end   = event.get("end_time")
            log(f"[VPP] Next event: {start} - {end} "
                f"({event.get('duration_hrs', 0):.1f}h) "
                f"precharge={self.store.get('vpp_pre_charge_soc', 0):.0f}%")
        else:
            log("[VPP] No event scheduled")
        if not self.axle:
            log("[VPP] Axle API not configured", level="WARNING")
        return True

    def menuToggleDebug(self, values_dict, menu_item_id):
        """Menu: Toggle debug logging on/off."""
        self.debug = not self.debug
        self.pluginPrefs["showDebugInfo"] = self.debug
        state = "ENABLED" if self.debug else "disabled"
        log(f"[Menu] Debug logging {state}")
        return True

    # ================================================================
    # Plugin Preferences Callback
    # ================================================================

    def closedPrefsConfigUi(self, values_dict, user_cancelled):
        if user_cancelled:
            return
        self.debug = values_dict.get("showDebugInfo", False)
        log("[Prefs] Plugin preferences updated - reinitialising modules")
        self._init_modules()

    # ================================================================
    # Initialisation Helpers
    # ================================================================

    def _init_modules(self):
        """Initialise all module instances from current preferences."""
        prefs = self.pluginPrefs

        # Resolve credentials: secrets.py wins over PluginConfig
        api_key    = OCTOPUS_API_KEY or prefs.get("octopusApiKey", "")
        account_id = OCTOPUS_ACCOUNT or prefs.get("octopusAccount", "")
        mpan       = OCTOPUS_MPAN    or prefs.get("octopusMpan", "")
        serial     = OCTOPUS_SERIAL  or prefs.get("octopusSerial", "")
        region     = prefs.get("octopusRegion", "F")

        sc_key     = SOLCAST_API_KEY   or prefs.get("solcastApiKey", "")
        sc_site1   = SOLCAST_SITE_1_ID or prefs.get("solcastSite1Id", "")
        sc_site2   = SOLCAST_SITE_2_ID or prefs.get("solcastSite2Id", "")

        axle_key   = AXLE_API_KEY or prefs.get("axleApiKey", "")

        inv_ip     = prefs.get("inverterIp", "192.168.100.49")
        inv_port   = int(prefs.get("modbusPort", 502))
        plant_addr = int(prefs.get("plantAddress", 247))
        inv_addr   = int(prefs.get("inverterSlaveId", 1))

        # Modbus
        if self.modbus:
            self.modbus.disconnect()
        self.modbus = SigenergyModbus(
            ip=inv_ip, port=inv_port,
            plant_address=plant_addr, inverter_address=inv_addr,
            logger=self.logger,
        )
        # Set export limit on inverter (register 40038)
        if prefs.get("exportEnabled", False):
            max_export_w = int(float(prefs.get("maxExportKw", 4.0)) * 1000)
            if self.modbus.connect():
                self.modbus.set_export_limit(max_export_w)

        # Solcast
        self.solcast = SolcastForecast(
            api_key=sc_key,
            site_1_id=sc_site1,
            site_2_id=sc_site2,
            data_dir=self.data_dir,
            logger=self.logger,
        )

        # Octopus
        self.octopus = OctopusAPI(
            api_key=api_key,
            account_id=account_id,
            mpan=mpan,
            serial=serial,
            region=region,
            data_dir=self.data_dir,
            logger=self.logger,
        )

        # Axle VPP
        self.axle = AxleAPI(api_token=axle_key) if axle_key else None

        log(
            f"[Init] Modbus={inv_ip}:{inv_port}, "
            f"Octopus={'OK' if api_key else 'not configured'}, "
            f"Solcast={'OK' if sc_key else 'not configured'}, "
            f"Axle={'OK' if axle_key else 'disabled'}"
        )

    def _get_data_dir(self):
        """Return plugin data directory path (create if needed)."""
        data_dir = indigo.server.getInstallFolderPath()
        data_dir = os.path.join(data_dir, "Preferences", "Plugins",
                                "com.clives.indigoplugin.sigenergy-energy-manager")
        if not os.path.exists(data_dir):
            os.makedirs(data_dir)
        return data_dir

    def _find_device(self, type_id):
        """Find the first enabled device of a given typeId."""
        for dev in indigo.devices.iter("self"):
            if dev.deviceTypeId == type_id and dev.enabled:
                return dev
        return None

    # ================================================================
    # Accumulator Persistence
    # ================================================================

    def _save_accumulators(self):
        """Save daily accumulators to disk (survives plugin reload)."""
        path = os.path.join(self.data_dir, "accumulators.json")
        data = {
            "pv_daily_kwh":          self.store["pv_daily_kwh"],
            "grid_import_daily_kwh": self.store["grid_import_daily_kwh"],
            "grid_export_daily_kwh": self.store["grid_export_daily_kwh"],
            "home_daily_kwh":        self.store["home_daily_kwh"],
            "peak_soc":              self.store["peak_soc"],
            "min_soc":               self.store["min_soc"],
            "today_date":            self.store["today_date"],
        }
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f)
        except Exception as e:
            self.logger.warning(f"Cannot save accumulators: {e}")

    def _load_accumulators(self):
        """Load daily accumulators from disk on startup."""
        path = os.path.join(self._get_data_dir(), "accumulators.json")
        if not os.path.exists(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            today = datetime.now().strftime("%Y-%m-%d")
            if data.get("today_date") == today:
                # Same day - restore accumulators
                self.store["pv_daily_kwh"]          = data.get("pv_daily_kwh", 0.0)
                self.store["grid_import_daily_kwh"] = data.get("grid_import_daily_kwh", 0.0)
                self.store["grid_export_daily_kwh"] = data.get("grid_export_daily_kwh", 0.0)
                self.store["home_daily_kwh"]        = data.get("home_daily_kwh", 0.0)
                self.store["peak_soc"]              = data.get("peak_soc", 0.0)
                self.store["min_soc"]               = data.get("min_soc", 100.0)
                self.store["today_date"]            = today
                self.logger.debug("Restored daily accumulators from disk")
        except Exception as e:
            self.logger.warning(f"Cannot load accumulators: {e}")
