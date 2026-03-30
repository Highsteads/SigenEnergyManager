#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    plugin.py
# Description: SigenEnergyManager - self-sufficiency battery management for
#              Sigenergy solar/battery systems. Replaces SigenergySolar v3.1.
#              Core philosophy: never import from grid unless battery cannot
#              reach next-day solar at minimum SOC. Export to prevent 100% cap.
# Author:      CliveS & Claude Sonnet 4.6
# Date:        27-03-2026 22:11 GMT
# Version:     1.4

import indigo
import json
import logging
import os
import platform
import sys
import time
from datetime import datetime, timedelta, timezone

# ============================================================
# Secrets (from master secrets.py - never committed to git)
# ============================================================

sys.path.insert(0, os.getcwd())
sys.path.insert(0, "/Library/Application Support/Perceptive Automation")
try:
    from secrets import (
        OCTOPUS_API_KEY, OCTOPUS_ACCOUNT, OCTOPUS_MPAN, OCTOPUS_SERIAL,
        SOLCAST_API_KEY, SOLCAST_SITE_1_ID, SOLCAST_SITE_2_ID,
        AXLE_API_KEY,
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

try:
    from plugin_utils import log_startup_banner
except ImportError:
    log_startup_banner = None

# Plugin modules
from sigenergy_modbus import SigenergyModbus
from solcast          import SolcastForecast
from octopus_api      import OctopusAPI, TARIFF_TRACKER
from battery_manager  import (
    BatteryManager, ManagerSnapshot, TariffData,
    ACTION_SELF_CONSUMPTION, ACTION_START_IMPORT, ACTION_STOP_IMPORT,
    ACTION_SCHEDULE_IMPORT, ACTION_START_EXPORT, ACTION_STOP_EXPORT,
    ACTION_SOLAR_OVERFLOW,
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

        if log_startup_banner:
            log_startup_banner(plugin_id, plugin_display_name, plugin_version)
        else:
            indigo.server.log(f"{plugin_display_name} v{plugin_version} starting")

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
        self.store["pv_daily_kwh"]              = 0.0
        self.store["grid_import_daily_kwh"]     = 0.0
        self.store["grid_export_daily_kwh"]     = 0.0
        self.store["home_daily_kwh"]            = 0.0
        self.store["peak_soc"]                  = 0.0
        self.store["min_soc"]                   = 100.0
        self.store["today_date"]                = datetime.now().strftime("%Y-%m-%d")
        # Lifetime total anchors for daily delta computation (set on first Modbus read)
        self.store["pv_lifetime_start_kwh"]     = None
        self.store["import_lifetime_start_kwh"] = None
        self.store["export_lifetime_start_kwh"] = None

        # VPP state machine
        self.store["vpp_state"]            = VPP_IDLE
        self.store["vpp_active"]           = False
        self.store["vpp_event"]            = None
        self.store["vpp_pre_charge_soc"]   = 0.0
        self.store["vpp_export_start_kwh"]  = 0.0   # grid_export_daily_kwh at event start
        self.store["vpp_last_export_kwh"]   = 0.0   # export kWh during last completed event
        self.store["vpp_cooling_start"]     = 0.0   # time.time() when COOLING_OFF entered
        self.store["vpp_release_alerted"]   = False # True once 45-min alert has been sent

        # Scheduled import state
        self.store["import_active"]          = False
        self.store["import_scheduled_time"]  = None
        self.store["import_target_soc"]      = 0.0

        # Export state (export limit is set once at startup; no dynamic tracking needed)
        self.store["export_active"]   = False

        # Solar overflow state (daytime charge-cap export)
        self.store["solar_overflow_active"]      = False
        self.store["solar_overflow_charge_cap_w"] = 0

        # Consumption profile (48 slots)
        self.store["consumption_profile"] = []

        self._load_accumulators()

    def startup(self):
        log(f"{PLUGIN_NAME} v{PLUGIN_VERSION} starting")
        self._init_modules()
        self.solcast.load_correction_factor()
        # Pre-populate latest_forecast_data from disk cache so the first manager
        # evaluation has forecast data available (disk cache was loaded in
        # SolcastForecast.__init__; this propagates it into plugin.py's dict).
        self._refresh_solcast()
        self.store["last_solcast"] = time.time()
        # Set initial state images for all devices that already exist
        # (deviceStartComm handles newly created devices; this handles existing ones on reload)
        for dev in indigo.devices.iter("self"):
            self._set_device_initial_state(dev)
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
        try:
            self._set_device_initial_state(dev)
        except Exception as e:
            self.logger.error(f"deviceStartComm error for {dev.name}: {e}")

    def deviceStopComm(self, dev):
        pass

    def _set_device_initial_state(self, dev):
        """Write placeholder states and state image for a device on startup."""
        type_id = dev.deviceTypeId

        if type_id == "sigenergyInverter":
            dev.updateStatesOnServer([
                {"key": "batterySoc",        "value": "0.0"},
                {"key": "pvPowerWatts",      "value": "0"},
                {"key": "gridPowerWatts",    "value": "0"},
                {"key": "batteryPowerWatts", "value": "0"},
                {"key": "homePowerWatts",    "value": "0"},
                {"key": "modbusConnected",   "value": "False"},
                {"key": "lastUpdate",        "value": "Initialising..."},
            ])
            dev.updateStateImageOnServer(indigo.kStateImageSel.SensorOff)

        elif type_id == "batteryManager":
            dev.updateStatesOnServer([
                {"key": "managerStatus", "value": "Initialising"},
                {"key": "currentAction", "value": "self_consumption"},
                {"key": "currentReason", "value": "Starting up"},
                {"key": "dawnViable",    "value": ""},
                {"key": "socAtDawn",     "value": ""},
                {"key": "lastUpdate",    "value": "Initialising..."},
            ])
            dev.updateStateImageOnServer(indigo.kStateImageSel.SensorOn)

        elif type_id == "solarForecast":
            dev.updateStatesOnServer([
                {"key": "todayKwh",       "value": "0.0"},
                {"key": "tomorrowKwh",    "value": "0.0"},
                {"key": "forecastStatus", "value": "Initialising"},
                {"key": "lastUpdate",     "value": "Initialising..."},
            ])
            dev.updateStateImageOnServer(indigo.kStateImageSel.SensorOn)

        elif type_id == "tariffMonitor":
            dev.updateStatesOnServer([
                {"key": "tariffActive", "value": "Initialising"},
                {"key": "rateToday",    "value": ""},
                {"key": "rateTomorrow", "value": ""},
                {"key": "lastUpdate",   "value": "Initialising..."},
            ])
            dev.updateStateImageOnServer(indigo.kStateImageSel.SensorOn)

        elif type_id == "axleVppMonitor":
            dev.updateStatesOnServer([
                {"key": "vppStatus",        "value": "Standby"},
                {"key": "vppState",         "value": "idle"},
                {"key": "vppLastExportKwh", "value": "0.00"},
                {"key": "lastUpdate",       "value": "Initialising..."},
            ])
            dev.updateStateImageOnServer(indigo.kStateImageSel.SensorOn)

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

        # 2. Solcast forecast (before manager so decision always has fresh data)
        if now - self.store["last_solcast"] >= SOLCAST_FETCH_INTERVAL:
            self._refresh_solcast()
            self.store["last_solcast"] = now

        # 3. Battery manager evaluation (every 15 min OR on SOC delta)
        soc_now    = self.latest_inverter_data.get("batterySoc", 0.0)
        soc_delta  = abs(soc_now - self.store["last_soc"])
        if (now - self.store["last_manager"] >= MANAGER_EVAL_INTERVAL
                or soc_delta >= SOC_CHANGE_TRIGGER):
            self._evaluate_manager()
            self.store["last_manager"] = now
            self.store["last_soc"]     = soc_now

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
        """Compute daily energy totals from Modbus registers where available.

        Home consumption: register 30092 resets at midnight on the inverter —
        read directly; always accurate regardless of when the plugin started.

        PV / grid import / grid export: only lifetime totals exist in the protocol
        (30088, 30216, 30220).  We snapshot the lifetime value at midnight (or at
        first read after plugin startup) and compute daily = current - snapshot.
        This is accurate for any full day even if the plugin restarts mid-day.

        If a register read fails the fallback is watt-integration (original method).
        """
        interval_h = MODBUS_POLL_INTERVAL / 3600.0   # fallback: hours per poll

        # --- Home daily: read directly from 30092 (resets at midnight) ---
        home_direct = data.get("homeDailyDirectKwh")
        if home_direct is not None:
            self.store["home_daily_kwh"] = home_direct
        else:
            self.store["home_daily_kwh"] += (
                max(0, data.get("homePowerWatts", 0)) * interval_h / 1000.0
            )

        # --- PV daily: delta from lifetime total (30088) ---
        pv_lifetime = data.get("pvLifetimeKwh")
        if pv_lifetime is not None:
            if self.store["pv_lifetime_start_kwh"] is None:
                self.store["pv_lifetime_start_kwh"] = pv_lifetime
                self.logger.info(
                    f"[Energy] PV lifetime anchor: {pv_lifetime:.2f} kWh "
                    f"(daily PV starts from this point)"
                )
            self.store["pv_daily_kwh"] = max(
                0.0, pv_lifetime - self.store["pv_lifetime_start_kwh"]
            )
        else:
            self.store["pv_daily_kwh"] += (
                max(0, data.get("pvPowerWatts", 0)) * interval_h / 1000.0
            )

        # --- Grid import daily: delta from lifetime total (30216) ---
        imp_lifetime = data.get("gridImportLifetimeKwh")
        if imp_lifetime is not None:
            if self.store["import_lifetime_start_kwh"] is None:
                self.store["import_lifetime_start_kwh"] = imp_lifetime
            self.store["grid_import_daily_kwh"] = max(
                0.0, imp_lifetime - self.store["import_lifetime_start_kwh"]
            )
        else:
            self.store["grid_import_daily_kwh"] += (
                max(0, data.get("gridPowerWatts", 0)) * interval_h / 1000.0
            )

        # --- Grid export daily: delta from lifetime total (30220) ---
        exp_lifetime = data.get("gridExportLifetimeKwh")
        if exp_lifetime is not None:
            if self.store["export_lifetime_start_kwh"] is None:
                self.store["export_lifetime_start_kwh"] = exp_lifetime
            self.store["grid_export_daily_kwh"] = max(
                0.0, exp_lifetime - self.store["export_lifetime_start_kwh"]
            )
        else:
            self.store["grid_export_daily_kwh"] += (
                max(0, -data.get("gridPowerWatts", 0)) * interval_h / 1000.0
            )

        # --- SOC peak/low tracking ---
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
            max_export_kw      = float(prefs.get("maxExportKw", 4.0)),
            pv_watts                = int(self.latest_inverter_data.get("pvPowerWatts", 0)),
            house_load_watts        = int(self.latest_inverter_data.get("homePowerWatts", 0)),
            export_active           = self.store["export_active"],
            corrected_tomorrow_kwh  = float(self.latest_forecast_data.get("correctedTomorrowKwh", 0.0)),
            tariff                  = tariff_data,
            forecast_p50            = self.latest_forecast_data.get("_hourly_p50_today", {}),
            forecast_p10            = self.latest_forecast_data.get("_hourly_p10_tomorrow", {}),
            dawn_times         = self.latest_forecast_data.get("_dawn_times", {}),
            consumption_profile = self.store.get("consumption_profile", []),
            now                = datetime.now(timezone.utc),
            bias_factor                 = float(self.latest_forecast_data.get("biasFactor", 1.0)),
            vpp_active                  = self.store["vpp_active"],
            solar_overflow_active       = self.store["solar_overflow_active"],
            solar_overflow_charge_cap   = self.store["solar_overflow_charge_cap_w"],
        )

        decision = self.manager.evaluate(snapshot)
        self.latest_decision = decision

        log(
            f"[Manager] SOC={soc_pct:.1f}%  PV={snapshot.pv_watts}W  "
            f"Action={decision.action}  {decision.reason}"
        )

        # Verify persistent inverter registers haven't drifted before acting
        self._verify_ems_registers()

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
                power_w = min(decision.power_watts or 10000,
                              int(float(self.pluginPrefs.get("inverterMaxKw", 10.0)) * 1000))
                if self.modbus.force_charge(power_w):
                    self.store["import_active"]     = True
                    self.store["import_target_soc"] = decision.target_soc_pct
                    self.store["export_active"]     = False
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
            # Idempotent: only call night_export if not already exporting
            if not prev_import and not prev_export:
                log(f"[Manager] Starting night export: {decision.reason}")
                inv_max_w = int(float(self.pluginPrefs.get("inverterMaxKw", 10.0)) * 1000)
                if self.modbus.night_export(inv_max_w):
                    self.store["export_active"] = True
                    self._trigger_event("exportStarted")

        elif action == ACTION_STOP_EXPORT:
            if prev_export:
                log("[Manager] Stopping night export - returning to self-consumption")
                self.modbus.set_self_consumption()
                self.store["export_active"] = False
                self._trigger_event("exportStopped")

        elif action == ACTION_SOLAR_OVERFLOW:
            # Daytime charge cap: stay in mode 0x02, reduce HOLD_ESS_MAX_CHARGE.
            # PV keeps generating at full power; surplus flows to grid.
            cap_w     = decision.power_watts
            export_kw = decision.export_kw
            prev_cap  = self.store["solar_overflow_charge_cap_w"]

            if not self.store["solar_overflow_active"]:
                # First entry: ensure self-consumption mode, then apply cap.
                # set_self_consumption() resets charge limit to inv_max_w so we
                # must call set_charge_limit() immediately after.
                log(
                    f"[Manager] Solar overflow starting: target export {export_kw:.2f} kW, "
                    f"charge cap {cap_w}W — PV surplus flowing to grid"
                )
                self.modbus.set_self_consumption()
                self.modbus.set_charge_limit(cap_w)
                self.store["solar_overflow_active"]       = True
                self.store["solar_overflow_charge_cap_w"] = cap_w
                self.store["export_active"]               = False
                self.store["import_active"]               = False
            elif abs(prev_cap - cap_w) > 500:
                # Cap has shifted by more than deadband — update inverter register
                log(
                    f"[Manager] Solar overflow: charge cap {prev_cap}W -> {cap_w}W "
                    f"(target export {export_kw:.2f} kW)"
                )
                self.modbus.set_charge_limit(cap_w)
                self.store["solar_overflow_charge_cap_w"] = cap_w
            # else: cap within deadband — idempotent, no Modbus writes

        elif action == ACTION_SELF_CONSUMPTION:
            if prev_import:
                log("[Manager] Returning to self-consumption")
                self.modbus.set_self_consumption()
                self.store["import_active"] = False
            elif prev_export:
                # export_enabled was disabled while exporting - clean up
                log("[Manager] Export disabled - returning to self-consumption")
                self.modbus.set_self_consumption()
                self.store["export_active"] = False
            elif self.store.get("solar_overflow_active"):
                # SOC dropped below release threshold — restore full charge rate
                log("[Manager] Solar overflow released — restoring full charge limit")
                self.modbus.set_self_consumption()   # resets charge limit to inv_max_w
                self.store["solar_overflow_active"]       = False
                self.store["solar_overflow_charge_cap_w"] = 0

        # Check if active import has reached target SOC
        if self.store["import_active"]:
            current_soc = self.latest_inverter_data.get("batterySoc", 0.0)
            target_soc  = self.store["import_target_soc"]
            if current_soc >= target_soc:
                log(f"[Manager] Import target SOC {target_soc:.0f}% reached - stopping")
                self.modbus.set_self_consumption()
                self.store["import_active"]      = False
                self.store["import_target_soc"]  = 0.0

    def _verify_ems_registers(self):
        """Read back HOLD_ESS_MAX_DISCHARGE and HOLD_ESS_MAX_CHARGE and correct if wrong.

        These registers persist on the inverter across mode changes. A previous
        force_discharge() or force_charge() call can leave a stale limit that
        caps battery output in self-consumption mode. This runs every manager
        evaluation cycle (~15 min) as a self-healing guard.

        Expected values:
          - export_active: discharge limit = inverter max (night_export uses export limit register,
                           not discharge register, to cap grid flow; battery must be free to supply
                           house load + grid simultaneously)
          - import_active: charge limit = inverter max (full import power), discharge = inverter max
          - otherwise:     both limits = inverter max (unrestricted self-consumption)
        """
        if not self.modbus or not self.modbus.connected:
            return

        inv_max_w = int(float(self.pluginPrefs.get("inverterMaxKw", 10.0)) * 1000)

        # Always expect inverter max — night_export() uses HOLD_GRID_MAX_EXPORT_LIMIT
        # (not the discharge register) to constrain grid flow.
        expected_discharge_w = inv_max_w

        # During solar overflow the charge limit is intentionally reduced.
        # Use the stored cap as the expected value so verify() doesn't fight it.
        if self.store.get("solar_overflow_active"):
            expected_charge_w = self.store.get("solar_overflow_charge_cap_w", inv_max_w)
        else:
            expected_charge_w = inv_max_w

        # --- Discharge limit ---
        actual_discharge_w = self.modbus.read_discharge_limit()
        if actual_discharge_w is not None:
            if abs(actual_discharge_w - expected_discharge_w) > 200:
                log(
                    f"[Verify] Discharge limit mismatch: inverter={actual_discharge_w}W "
                    f"expected={expected_discharge_w}W — correcting",
                    level="WARNING",
                )
                self.modbus.set_discharge_limit(expected_discharge_w)

        # --- Charge limit ---
        actual_charge_w = self.modbus.read_charge_limit()
        if actual_charge_w is not None:
            if abs(actual_charge_w - expected_charge_w) > 200:
                log(
                    f"[Verify] Charge limit mismatch: inverter={actual_charge_w}W "
                    f"expected={expected_charge_w}W — correcting",
                    level="WARNING",
                )
                self.modbus.set_charge_limit(expected_charge_w)

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
            if self.modbus and self.modbus.force_charge(10000):
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

        status   = data.get("forecastStatus", "")
        tmrw_kwh = data.get("correctedTomorrowKwh", 0.0)

        if "No data" in status:
            log(f"[Solcast] WARNING: forecast unavailable ({status}) — night export condition 3 will block", level="WARNING")
        elif tmrw_kwh == 0.0:
            log(f"[Solcast] WARNING: tomorrow forecast is 0.0 kWh (status: {status!r}) — night export condition 3 will block", level="WARNING")
        else:
            log(
                f"[Solcast] Today: {data.get('correctedTodayKwh', 0):.1f} kWh "
                f"(raw {data.get('todayKwh', 0):.1f}, bias {data.get('biasFactor', 1):.3f}), "
                f"Tomorrow: {tmrw_kwh:.1f} kWh"
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

        event         = self.axle.get_next_event()
        now           = datetime.now(timezone.utc)
        current_state = self.store["vpp_state"]

        if event is None:
            # Axle API returns None when no event is scheduled or the event has ended

            if current_state == VPP_ACTIVE:
                # Event ended (API stopped returning it)
                vpp_export = (self.store["grid_export_daily_kwh"]
                              - self.store.get("vpp_export_start_kwh", 0.0))
                self.store["vpp_last_export_kwh"] = round(vpp_export, 2)
                self.store["vpp_cooling_start"]   = time.time()
                self._vpp_transition(VPP_COOLING_OFF)
                self.store["vpp_active"] = False
                self._trigger_event("vppEnded")
                log(
                    f"[VPP] Event complete. Estimated VPP export: {vpp_export:.2f} kWh. "
                    f"Waiting for Axle to release inverter..."
                )

            elif current_state == VPP_COOLING_OFF:
                self._vpp_check_axle_release()

            elif current_state != VPP_IDLE:
                log("[VPP] Event cancelled/disappeared - restoring discharge cutoff")
                self._restore_discharge_cutoff()
                self._vpp_transition(VPP_IDLE)
                self.store["vpp_active"] = False

            self._update_vpp_device()
            return

        # Event is scheduled
        start_time     = event["start_time"]
        end_time       = event["end_time"]
        hours_to_start = (start_time - now).total_seconds() / 3600.0

        if current_state == VPP_IDLE and hours_to_start > 0:
            self.store["vpp_event"] = event
            self._set_vpp_discharge_cutoff(event)
            self._vpp_transition(VPP_ANNOUNCED)
            self._trigger_event("vppAnnounced")
            log(
                f"[VPP] Event announced: {start_time.strftime('%H:%M')} - "
                f"{end_time.strftime('%H:%M')} ({event['duration_hrs']:.1f}h)"
            )

        elif current_state == VPP_ANNOUNCED:
            if hours_to_start <= 1.0:
                log(f"[VPP] Event in {hours_to_start * 60:.0f} min - preparing")
            if hours_to_start <= 0.5:
                self._start_vpp_precharge(event)

        elif current_state == VPP_PRE_CHARGING:
            required_soc = self.store["vpp_pre_charge_soc"]
            current_soc  = self.latest_inverter_data.get("batterySoc", 0.0)

            if current_soc >= required_soc or hours_to_start <= 0:
                if self.modbus:
                    self.modbus.set_self_consumption()
                log(f"[VPP] Ready for event. SOC: {current_soc:.0f}%")

            if hours_to_start <= 0:
                # Record export baseline when Axle takes over
                self.store["vpp_export_start_kwh"] = self.store["grid_export_daily_kwh"]
                self._vpp_transition(VPP_ACTIVE)
                self.store["vpp_active"] = True
                self._trigger_event("vppStarted")
                log(f"[VPP] Event ACTIVE - Axle has control")

        elif current_state == VPP_ACTIVE:
            if now >= end_time:
                # Time-based end — record export and begin waiting for Axle release
                vpp_export = (self.store["grid_export_daily_kwh"]
                              - self.store.get("vpp_export_start_kwh", 0.0))
                self.store["vpp_last_export_kwh"] = round(vpp_export, 2)
                self.store["vpp_cooling_start"]   = time.time()
                self._vpp_transition(VPP_COOLING_OFF)
                self.store["vpp_active"] = False
                self._trigger_event("vppEnded")
                log(
                    f"[VPP] Event ended. Estimated VPP export: {vpp_export:.2f} kWh. "
                    f"Waiting for Axle to release inverter..."
                )

        elif current_state == VPP_COOLING_OFF:
            # Axle API may still return the event briefly after it ends
            self._vpp_check_axle_release()

        self._update_vpp_device()

    def _vpp_check_axle_release(self):
        """Check if Axle has released the inverter and reinstate Remote EMS.

        Axle always reverts the inverter to 'Max Self Consumption' (local mode,
        register 30003 = 0) when an event ends.  We watch emsWorkMode for that
        string — the moment we see it we know Axle has handed back control and
        we can switch to Remote EMS (set_self_consumption() enables 40029=1 +
        mode 0x02 in 40031, which puts 30003 back to 7 "Remote EMS").

        Alert thresholds after VPP_COOLING_OFF was entered (vpp_cooling_start):
          45 min  — Pushover + email to axle@strudwick.co.uk
          60 min  — Force reinstatement regardless of EMS mode
        """
        ems_mode      = self.latest_inverter_data.get("emsWorkMode", "")
        axle_released = "Self" in ems_mode   # Axle releases to "Self-Consumption" (mode 0)

        cooling_start    = self.store.get("vpp_cooling_start", 0)
        elapsed_secs     = time.time() - cooling_start
        alerted          = self.store.get("vpp_release_alerted", False)
        ALERT_SECS       = 2700   # 45 minutes
        FORCE_SECS       = 3600   # 60 minutes

        # Send alert at 45 min if not yet released
        if elapsed_secs >= ALERT_SECS and not alerted and not axle_released:
            self.store["vpp_release_alerted"] = True
            elapsed_min = int(elapsed_secs / 60)
            log(
                f"[VPP] WARNING: Axle has not released inverter after {elapsed_min} min "
                f"(EMS mode: '{ems_mode}'). Sending alert.",
                level="WARNING"
            )
            self._send_vpp_release_alert(elapsed_min, ems_mode)

        # Still waiting and not yet at force timeout
        if not (axle_released or elapsed_secs >= FORCE_SECS):
            if self.debug:
                log(f"[VPP] Waiting for Axle release "
                    f"(EMS='{ems_mode}', {int(elapsed_secs)}s elapsed)")
            return

        # Either released naturally or force timeout reached
        if not axle_released:
            log(
                f"[VPP] 60-min timeout - forcing Remote EMS reinstatement "
                f"(EMS still: '{ems_mode}')",
                level="WARNING"
            )
        else:
            log(
                f"[VPP] Axle released inverter (EMS now: '{ems_mode}') - "
                f"reinstating Remote EMS (Max Self Consumption)"
            )

        if self.modbus:
            self.modbus.set_self_consumption()

        self._restore_discharge_cutoff()
        self._vpp_transition(VPP_IDLE)
        self.store["vpp_event"]           = None
        self.store["vpp_release_alerted"] = False   # reset for next event
        self.store["had_vpp_today"]       = True

    def _send_vpp_release_alert(self, elapsed_min, ems_mode):
        """Send Pushover + email when Axle has not released the inverter on time."""
        subject = f"Axle VPP - Inverter not released after {elapsed_min} min"
        plain   = (
            f"The Axle VPP event ended {elapsed_min} minutes ago but the "
            f"Sigenergy inverter has not returned to Self Consumption mode.\n\n"
            f"Current EMS mode: {ems_mode}\n"
            f"Expected: Max Self Consumption\n\n"
            f"Remote EMS will be force-reinstated at 60 minutes if Axle has "
            f"still not released.\n\nPlease check the Axle app and Sigenergy portal."
        )
        html    = (
            f"<html><body>"
            f"<p>The Axle VPP event ended <strong>{elapsed_min} minutes ago</strong> "
            f"but the Sigenergy inverter has not returned to Self Consumption mode.</p>"
            f"<p><strong>Current EMS mode:</strong> {ems_mode}<br>"
            f"<strong>Expected:</strong> Max Self Consumption</p>"
            f"<p>Remote EMS will be force-reinstated at 60 minutes if Axle has "
            f"still not released.</p>"
            f"<p>Please check the Axle app and Sigenergy portal.</p>"
            f"</body></html>"
        )

        # Pushover alert
        try:
            pushover = indigo.server.getPlugin("io.thechad.indigoplugin.pushover")
            if pushover and pushover.isEnabled():
                pushover.executeAction("sendPushover", props={
                    "title":    subject,
                    "message":  plain,
                    "priority": "1",   # high priority
                })
                log("[VPP] Pushover alert sent")
        except Exception as e:
            log(f"[VPP] Pushover alert failed: {e}", level="WARNING")

        # Email to Axle support
        try:
            email_plugin = indigo.server.getPlugin("com.indigodomo.email")
            if email_plugin and email_plugin.isEnabled():
                email_plugin.executeAction("sendEmail", deviceId=1192809466,
                    props={
                        "emailTo":      "axle@strudwick.co.uk",
                        "emailSubject": subject,
                        "emailBody":    html,
                    }
                )
                log("[VPP] Email alert sent to axle@strudwick.co.uk")
        except Exception as e:
            log(f"[VPP] Email alert failed: {e}", level="WARNING")

    def _start_vpp_precharge(self, event):
        """Calculate required SOC for VPP event; pre-charge only if needed.

        Pre-charge is skipped if the current SOC already covers the event
        export plus the configured dawn SOC target.  The discharge cutoff
        register is always set to the dawn target for reserve protection.
        """
        duration_hrs    = event.get("duration_hrs", 1.0)
        cap_kwh         = float(self.pluginPrefs.get("batteryCapacityKwh", BATTERY_CAPACITY_KWH))
        max_export_kw   = float(self.pluginPrefs.get("maxExportKw", 4.0))
        dawn_target_pct = float(self.pluginPrefs.get("dawnSocTarget", 10))

        # Energy Axle will export + dawn reserve (replaces hard-coded 12 kWh)
        export_kwh    = max_export_kw * duration_hrs / VPP_DISCHARGE_EFFICIENCY
        dawn_kwh      = cap_kwh * dawn_target_pct / 100.0
        required_kwh  = export_kwh + dawn_kwh
        required_soc  = min(100.0, (required_kwh / cap_kwh) * 100.0)
        required_soc  = max(required_soc, dawn_target_pct)

        # Current battery level
        current_soc  = self.latest_inverter_data.get("batterySoc", 0.0)
        current_kwh  = cap_kwh * current_soc / 100.0

        self.store["vpp_pre_charge_soc"] = required_soc

        # Discharge cutoff was raised to VPP-aware floor at VPP_ANNOUNCED; no change needed here

        if current_kwh >= required_kwh:
            log(
                f"[VPP] SOC sufficient ({current_soc:.0f}%, {current_kwh:.1f} kWh) for "
                f"{duration_hrs:.1f}h export ({export_kwh:.1f} kWh) + dawn reserve "
                f"({dawn_kwh:.1f} kWh) - no pre-charge needed"
            )
        else:
            shortfall = required_kwh - current_kwh
            log(
                f"[VPP] Pre-charging to {required_soc:.0f}% for {duration_hrs:.1f}h event "
                f"({export_kwh:.1f} kWh export + {dawn_kwh:.1f} kWh dawn reserve, "
                f"shortfall: {shortfall:.1f} kWh)"
            )
            if self.modbus:
                self.modbus.force_charge(10000)

        self._vpp_transition(VPP_PRE_CHARGING)

    def _set_vpp_discharge_cutoff(self, event):
        """Raise discharge cutoff at VPP_ANNOUNCED to protect event energy reserve.

        Floor = dawn target kWh + full event export energy (conservative).
        The inverter enforces this floor regardless of night export or self-consumption.
        """
        import math
        duration_hrs    = event.get("duration_hrs", 1.0)
        cap_kwh         = float(self.pluginPrefs.get("batteryCapacityKwh", BATTERY_CAPACITY_KWH))
        max_export_kw   = float(self.pluginPrefs.get("maxExportKw", 4.0))
        dawn_target_pct = float(self.pluginPrefs.get("dawnSocTarget", 10))
        dawn_kwh        = cap_kwh * dawn_target_pct / 100.0
        event_kwh       = duration_hrs * max_export_kw / VPP_DISCHARGE_EFFICIENCY
        floor_pct       = math.ceil((dawn_kwh + event_kwh) / cap_kwh * 100.0)
        floor_pct       = max(floor_pct, dawn_target_pct)
        if self.modbus:
            self.modbus.set_discharge_cutoff(floor_pct)
            log(
                f"[VPP] Discharge cutoff raised to {floor_pct:.0f}% "
                f"(dawn {dawn_target_pct:.0f}% + event {event_kwh:.1f} kWh)"
            )

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
        self.store["pv_daily_kwh"]              = 0.0
        self.store["grid_import_daily_kwh"]     = 0.0
        self.store["grid_export_daily_kwh"]     = 0.0
        self.store["home_daily_kwh"]            = 0.0
        self.store["peak_soc"]                  = 0.0
        self.store["min_soc"]                   = 100.0
        self.store["today_date"]                = today
        # Clear lifetime anchors — next poll will re-snapshot at the new day's baseline
        self.store["pv_lifetime_start_kwh"]     = None
        self.store["import_lifetime_start_kwh"] = None
        self.store["export_lifetime_start_kwh"] = None

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
            ACTION_SOLAR_OVERFLOW:   "Solar Overflow Export",
            ACTION_START_IMPORT:     "Grid Import Active",
            ACTION_STOP_IMPORT:      "Import Stopping",
            ACTION_SCHEDULE_IMPORT:  "Import Scheduled",
            ACTION_START_EXPORT:     "Night Export Active",
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
            {"key": "vppStatus",         "value": "Active" if self.store["vpp_active"] else "Standby"},
            {"key": "vppState",          "value": self.store["vpp_state"]},
            {"key": "eventStartTime",    "value": start_str},
            {"key": "eventEndTime",      "value": end_str},
            {"key": "preChargeRequired", "value": str(self.store["vpp_pre_charge_soc"])},
            {"key": "estimatedEarnings", "value": str(earnings_est)},
            {"key": "vppLastExportKwh",  "value": str(round(self.store.get("vpp_last_export_kwh", 0.0), 2))},
            {"key": "lastUpdate",        "value": datetime.now().strftime("%H:%M:%S")},
        ]
        dev.updateStatesOnServer(states)

    # ================================================================
    # Indigo Action Callbacks
    # ================================================================

    def actionForceGridImport(self, action):
        """Action: Force immediate grid import."""
        props     = action.props
        power_kw  = float(props.get("powerKw", 10.0))
        target_soc = float(props.get("targetSocPct", 80.0))
        log(f"[Action] Force grid import: {power_kw}kW to {target_soc:.0f}% SOC")
        if self.modbus and self.modbus.force_charge(int(power_kw * 1000)):
            self.store["import_active"]     = True
            self.store["import_target_soc"] = target_soc
            self.store["export_active"]     = False

    def actionForceExport(self, action):
        """Action: Force immediate grid export."""
        inv_max_w = int(float(self.pluginPrefs.get("inverterMaxKw", 10.0)) * 1000)
        log("[Action] Force export: night_export mode")
        if self.modbus and self.modbus.night_export(inv_max_w):
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

    def menuRefreshAll(self):
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

    def menuShowStatus(self):
        """Menu: Log current manager status to event log."""
        from datetime import datetime as _dt
        now_str  = _dt.now().strftime("%H:%M:%S")
        capacity = float(self.pluginPrefs.get("batteryCapacityKwh", 35.04))

        inv   = self._find_device("sigenergyInverter")
        mgr   = self._find_device("batteryManager")
        fcast = self._find_device("solarForecast")
        tarif = self._find_device("tariffMonitor")

        log("[Status] ======= Live Status: " + now_str + " =======")

        # --- Battery ---
        if inv:
            soc_pct  = float(inv.states.get("batterySoc", 0))
            soc_kwh  = soc_pct / 100.0 * capacity
            batt_w   = int(inv.states.get("batteryPowerWatts", 0))
            modbus   = inv.states.get("modbusConnected", "False")
            if batt_w > 50:
                batt_str = f"Charging {batt_w}W"
            elif batt_w < -50:
                batt_str = f"Discharging {abs(batt_w)}W"
            else:
                batt_str = "Idle"
            log(f"[Status] Battery:  {soc_pct:.1f}% SOC  |  {soc_kwh:.1f} kWh stored  |  {batt_str}"
                f"  |  Modbus: {'OK' if modbus == 'True' else 'OFFLINE'}")
        else:
            log("[Status] Battery:  No inverter device found", level="WARNING")

        # --- Solar & Grid & Home ---
        if inv:
            pv_w    = int(inv.states.get("pvPowerWatts", 0))
            grid_w  = int(inv.states.get("gridPowerWatts", 0))
            home_w  = int(inv.states.get("homePowerWatts", 0))
            ems     = inv.states.get("emsWorkMode", "Unknown")
            grid_str = f"Exporting {abs(grid_w)}W" if grid_w < -50 else (
                       f"Importing {grid_w}W" if grid_w > 50 else "Idle (grid)")
            pv_today   = self.store.get("pv_daily_kwh", 0.0)
            imp_today  = self.store.get("grid_import_daily_kwh", 0.0)
            exp_today  = self.store.get("grid_export_daily_kwh", 0.0)
            home_today = self.store.get("home_daily_kwh", 0.0)
            fcst_remain  = fcast.states.get("correctedTodayKwh", "?") if fcast else "?"
            fcst_tmrw    = fcast.states.get("correctedTomorrowKwh", "?") if fcast else "?"
            try:
                fcst_expected_total = round(pv_today + float(fcst_remain), 1)
            except (ValueError, TypeError):
                fcst_expected_total = "?"
            log(f"[Status] Solar:    {pv_w}W now  |  {pv_today:.2f} kWh today"
                f"  |  {fcst_remain} kWh forecast remaining  |  {fcst_expected_total} kWh expected total")
            log(f"[Status] Grid:     {grid_str}  |  Import today {imp_today:.2f} kWh"
                f"  |  Export today {exp_today:.2f} kWh")
            log(f"[Status] Home:     {home_w}W now  |  {home_today:.2f} kWh today"
                f"  |  EMS mode: {ems}")

        # --- Tariff ---
        if tarif:
            t_name = tarif.states.get("tariffActive", "?")
            t_rate = tarif.states.get("rateToday", "?")
            t_tmrw = tarif.states.get("rateTomorrow", "?")
            log(f"[Status] Tariff:   {t_name}  |  Today {t_rate}p/kWh  |  Tomorrow {t_tmrw}p/kWh")

        # --- Manager decision ---
        if mgr:
            action   = mgr.states.get("currentAction", "unknown")
            reason   = mgr.states.get("currentReason", "")
            viable   = mgr.states.get("dawnViable", "?")
            soc_dawn = mgr.states.get("socAtDawn", "?")
            sched    = mgr.states.get("importScheduledTime", "")
            log(f"[Status] Manager:  {action}  |  {reason}")
            log(f"[Status] Dawn:     Viable={viable}  |  SOC at dawn {soc_dawn} kWh"
                + (f"  |  Import scheduled {sched}" if sched else ""))

        # --- Tomorrow ---
        if fcast:
            log(f"[Status] Tomorrow: {fcst_tmrw} kWh solar forecast")

        log("[Status] =============================================")
        return True

    def menuShowDailyHistory(self):
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

    def menuShowTariffRates(self):
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

    def menuShowVppStatus(self):
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

    def menuShowVppExport(self):
        """Menu: Log VPP export summary for today."""
        axle_enabled = self.pluginPrefs.get("axleEnabled", False)
        state        = self.store.get("vpp_state", "idle")
        active       = self.store.get("vpp_active", False)
        last_export  = self.store.get("vpp_last_export_kwh", 0.0)
        had_vpp      = self.store.get("had_vpp_today", False) or active

        log("[VPP] ============ VPP Export Summary ============")
        log(f"[VPP] Axle enabled:        {'YES' if axle_enabled else 'NO'}")
        log(f"[VPP] Axle token:          {'configured' if self.axle else 'not set'}")
        log(f"[VPP] Current state:       {state}")
        log(f"[VPP] Event active:        {'YES - Axle in control' if active else 'No'}")

        if active:
            ongoing = (self.store["grid_export_daily_kwh"]
                       - self.store.get("vpp_export_start_kwh", 0.0))
            log(f"[VPP] Export so far:       {ongoing:.2f} kWh  (event in progress)")
        elif had_vpp:
            log(f"[VPP] Last event export:   {last_export:.2f} kWh")
        else:
            log("[VPP] Last event export:   No VPP event recorded today")

        dev = self._find_device("axleVppMonitor")
        if dev:
            start = dev.states.get("eventStartTime", "")
            end   = dev.states.get("eventEndTime", "")
            earn  = dev.states.get("estimatedEarnings", "")
            chg   = dev.states.get("preChargeRequired", "0")
            if start:
                log(f"[VPP] Event window:        {start} - {end}")
                log(f"[VPP] Est. earnings:       GBP {earn}")
                if float(chg) > 0:
                    log(f"[VPP] Pre-charge target:   {chg}% SOC")
                else:
                    log(f"[VPP] Pre-charge:          Not needed (SOC sufficient)")

        log("[VPP] =============================================")
        return True

    def menuShowTodaySummary(self):
        """Menu: Log a human-readable summary of today's energy data."""
        today   = datetime.now().strftime("%d-%b-%Y")
        pv      = self.store.get("pv_daily_kwh", 0.0)
        imp     = self.store.get("grid_import_daily_kwh", 0.0)
        exp     = self.store.get("grid_export_daily_kwh", 0.0)
        home    = self.store.get("home_daily_kwh", 0.0)
        peak    = self.store.get("peak_soc", 0.0)
        low     = self.store.get("min_soc", 100.0)
        vpp_exp = self.store.get("vpp_last_export_kwh", 0.0)
        had_vpp = self.store.get("had_vpp_today", False) or self.store.get("vpp_active", False)

        inv         = self._find_device("sigenergyInverter")
        current_soc = float(inv.states.get("batterySoc", 0)) if inv else 0.0
        ems_mode    = inv.states.get("emsWorkMode", "Unknown") if inv else "Unknown"
        pv_now      = inv.states.get("pvPowerWatts", "0") if inv else "0"
        grid_now    = inv.states.get("gridPowerWatts", "0") if inv else "0"

        mgr      = self._find_device("batteryManager")
        action   = mgr.states.get("currentAction", "Unknown") if mgr else "Unknown"
        reason   = mgr.states.get("currentReason", "") if mgr else ""
        viable   = mgr.states.get("dawnViable", "?") if mgr else "?"
        soc_dawn = mgr.states.get("socAtDawn", "?") if mgr else "?"

        fcast       = self._find_device("solarForecast")
        fcst_today  = fcast.states.get("correctedTodayKwh", "?") if fcast else "?"
        fcst_tmrw   = fcast.states.get("correctedTomorrowKwh", "?") if fcast else "?"

        tariff  = self._find_device("tariffMonitor")
        t_name  = tariff.states.get("tariffActive", "?") if tariff else "?"
        t_rate  = tariff.states.get("rateToday", "") if tariff else ""
        t_tmrw  = tariff.states.get("rateTomorrow", "") if tariff else ""

        import_note = "  (self-sufficient - no grid draw)" if imp < 0.05 else ""
        export_note = f"  (VPP contribution: {vpp_exp:.2f} kWh)" if had_vpp and vpp_exp > 0 else ""
        rate_str    = (f" at {t_rate}p/kWh" if t_rate else "")
        tmrw_str    = (f"  |  tomorrow: {t_tmrw}p" if t_tmrw else "  |  tomorrow: TBD")

        log(f"[Today] ======= Energy Summary: {today} =======")
        try:
            expected_total = round(pv + float(fcst_today), 1)
        except (ValueError, TypeError):
            expected_total = "?"
        log(f"[Today] Solar generation:    {pv:.2f} kWh  (+{fcst_today} kWh remaining = {expected_total} kWh expected total)")
        log(f"[Today] Home consumption:    {home:.2f} kWh")
        log(f"[Today] Grid import:         {imp:.2f} kWh{import_note}")
        log(f"[Today] Grid export:         {exp:.2f} kWh{export_note}")
        log(f"[Today] Battery SOC now:     {current_soc:.0f}%  "
            f"(peak {peak:.0f}%,  low {low:.0f}%)")
        log(f"[Today] EMS mode:            {ems_mode}")
        log(f"[Today] Manager action:      {action}")
        if reason:
            log(f"[Today] Reason:              {reason}")
        log(f"[Today] Dawn viability:      {viable}  |  SOC at dawn: {soc_dawn} kWh")
        log(f"[Today] Tariff:              {t_name}{rate_str}{tmrw_str}")
        log(f"[Today] Tomorrow forecast:   {fcst_tmrw} kWh solar expected")
        log(f"[Today] Live:                PV {pv_now} W  |  Grid {grid_now} W")
        if had_vpp:
            vpp_state = self.store.get("vpp_state", "idle")
            if self.store.get("vpp_active"):
                ongoing = exp - self.store.get("vpp_export_start_kwh", 0.0)
                log(f"[Today] VPP:                 ACTIVE ({ongoing:.2f} kWh exported so far)")
            else:
                log(f"[Today] VPP:                 Completed  ({vpp_exp:.2f} kWh exported)  "
                    f"state: {vpp_state}")
        log(f"[Today] =============================================")
        return True

    def menuToggleDebug(self):
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
        # Startup Modbus initialisations — connect once for all startup writes.
        # HOLD_ESS_MAX_DISCHARGE (40034) persists across mode changes on the inverter.
        # A previous force_discharge() call may have left a low limit that caps battery
        # output even in self-consumption mode. Always reset to full inverter capacity.
        if self.modbus.connect():
            inverter_max_w = int(float(prefs.get("inverterMaxKw", 10.0)) * 1000)
            self.modbus.set_discharge_limit(inverter_max_w)   # clear any stale discharge cap
            self.modbus.set_charge_limit(inverter_max_w)      # clear any stale charge cap
            self.modbus.set_charge_cutoff(100.0)              # ensure unrestricted charging
            if prefs.get("exportEnabled", False):
                dno_startup_w = int(float(prefs.get("maxExportKw", 4.0)) * 1000)
                self.modbus.set_export_limit(dno_startup_w)

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
            "pv_daily_kwh":              self.store["pv_daily_kwh"],
            "grid_import_daily_kwh":     self.store["grid_import_daily_kwh"],
            "grid_export_daily_kwh":     self.store["grid_export_daily_kwh"],
            "home_daily_kwh":            self.store["home_daily_kwh"],
            "peak_soc":                  self.store["peak_soc"],
            "min_soc":                   self.store["min_soc"],
            "today_date":                self.store["today_date"],
            "pv_lifetime_start_kwh":     self.store["pv_lifetime_start_kwh"],
            "import_lifetime_start_kwh": self.store["import_lifetime_start_kwh"],
            "export_lifetime_start_kwh": self.store["export_lifetime_start_kwh"],
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
                # Same day — restore accumulators and lifetime anchors
                self.store["pv_daily_kwh"]              = data.get("pv_daily_kwh", 0.0)
                self.store["grid_import_daily_kwh"]     = data.get("grid_import_daily_kwh", 0.0)
                self.store["grid_export_daily_kwh"]     = data.get("grid_export_daily_kwh", 0.0)
                self.store["home_daily_kwh"]            = data.get("home_daily_kwh", 0.0)
                self.store["peak_soc"]                  = data.get("peak_soc", 0.0)
                self.store["min_soc"]                   = data.get("min_soc", 100.0)
                self.store["today_date"]                = today
                # Restore lifetime anchors so delta computation continues correctly
                self.store["pv_lifetime_start_kwh"]     = data.get("pv_lifetime_start_kwh")
                self.store["import_lifetime_start_kwh"] = data.get("import_lifetime_start_kwh")
                self.store["export_lifetime_start_kwh"] = data.get("export_lifetime_start_kwh")
                self.logger.debug("Restored daily accumulators from disk")
        except Exception as e:
            self.logger.warning(f"Cannot load accumulators: {e}")

    # -------------------------------------------------------------------------
    # Menu handlers
    # -------------------------------------------------------------------------

    def showPluginInfo(self, valuesDict=None, typeId=None):
        if log_startup_banner:
            log_startup_banner(self.pluginId, self.pluginDisplayName, self.pluginVersion)
        else:
            indigo.server.log(f"{self.pluginDisplayName} v{self.pluginVersion}")
