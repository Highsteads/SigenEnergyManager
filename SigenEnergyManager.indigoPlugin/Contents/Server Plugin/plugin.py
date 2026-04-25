#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    plugin.py
# Description: SigenEnergyManager - self-sufficiency battery management for
#              Sigenergy solar/battery systems. Replaces SigenergySolar v3.1.
#              Core philosophy: never import from grid unless battery cannot
#              reach next-day solar at minimum SOC. Export to prevent 100% cap.
# Author:      CliveS & Claude Sonnet 4.6
# Date:        25-04-2026
# Version:     4.4

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
        AXLE_API_KEY,
    )
except ImportError:
    OCTOPUS_API_KEY = ""
    OCTOPUS_ACCOUNT = ""
    OCTOPUS_MPAN    = ""
    OCTOPUS_SERIAL  = ""
    AXLE_API_KEY    = ""

try:
    from plugin_utils import log_startup_banner
except ImportError:
    log_startup_banner = None

# Plugin modules
from sigenergy_modbus import SigenergyModbus
from openmeteo_forecast import OpenMeteoForecast
from octopus_api      import OctopusAPI, TARIFF_TRACKER, TARIFF_FLEXIBLE
from battery_manager  import (
    BatteryManager, ManagerSnapshot, TariffData,
    ACTION_SELF_CONSUMPTION, ACTION_START_IMPORT, ACTION_STOP_IMPORT,
    ACTION_SCHEDULE_IMPORT, ACTION_START_EXPORT, ACTION_STOP_EXPORT,
    ACTION_SOLAR_OVERFLOW, SOLAR_OVERFLOW_CAP_DEADBAND_W,
)
from axle_api      import AxleAPI
from storm_watch   import check_storm_level
from web_dashboard import WebDashboard

# ============================================================
# Constants
# ============================================================

PLUGIN_VERSION     = "4.4"
PLUGIN_NAME        = "SigenEnergyManager"
WEB_DASHBOARD_PORT = 8179

# Minimum inverter readings required per half-hourly slot before we trust the
# accumulated average over the default profile.  5 readings = ~5 days of data
# in that time-slot (one reading per day during that 30-min window).
HOME_PROFILE_MIN_READINGS = 5

# Polling intervals (seconds)
MODBUS_POLL_INTERVAL      = 60
MANAGER_EVAL_INTERVAL     = 60    # evaluate every Modbus poll cycle
MANAGER_LOG_INTERVAL      = 900   # heartbeat log every 15 min even if action unchanged
FORECAST_FETCH_INTERVAL   = 1800  # 30 minutes (Open-Meteo: 10,000 calls/day free)
OCTOPUS_RATES_INTERVAL    = 1800  # 30 minutes
OCTOPUS_PROFILE_INTERVAL  = 86400 # 24 hours
VPP_POLL_NORMAL_INTERVAL  = 600   # 10 minutes
VPP_POLL_ACTIVE_INTERVAL  = 60    # 1 minute (near/during event)
ACCUMULATOR_SAVE_INTERVAL = 300   # 5 minutes
STORM_WATCH_INTERVAL = 7200  # 2 hours
ENERGY_VAR_INTERVAL  = 1800  # 30 minutes — write running totals to Indigo variables

# Storm-level hierarchy (mirrors storm_watch._LEVELS)
STORM_LEVELS = ["none", "yellow", "amber", "red"]

# SOC targets applied when a Met Office warning covers our location
# Yellow = watch level: charge to 50%, suspend exports
# Amber/Red = significant disruption risk: charge to 80%, suspend exports
STORM_SOC_YELLOW = 50.0
STORM_SOC_AMBER  = 80.0

# Power cut lockout: suppress export for this many hours after grid is restored
POWER_CUT_LOCKOUT_HOURS = 4.0


# VPP state machine values
VPP_IDLE         = "idle"
VPP_ANNOUNCED    = "announced"
VPP_PRE_CHARGING = "pre_charging"
VPP_ACTIVE       = "active"
VPP_COOLING_OFF  = "cooling_off"

# Axle VPP SOC calculation constants (from SigenergySolar)
VPP_DISCHARGE_EFFICIENCY  = 0.97
VPP_RESERVE_KWH           = 12.0  # 4 overnight + 3 morning + 5 buffer
BATTERY_CAPACITY_KWH      = 35.04
VPP_PRE_EXPORT_MINUTES    = 5     # start exporting this many minutes before event start.
                                   # Axle pays based on smart meter readings during their
                                   # event window — being already exporting at T+0 captures
                                   # the full paid window even if Axle's dispatch command
                                   # arrives late (observed: 08:30 event, command at 08:45).


# ============================================================
# Plugin log file (daily rotation, 14-day retention)
# ============================================================

_plugin_log_fh   = None
_plugin_log_date = None

def _ensure_plugin_log(data_dir):
    """Open (or rotate) the daily plugin log file in data_dir/logs/."""
    global _plugin_log_fh, _plugin_log_date
    today = datetime.now().strftime("%Y-%m-%d")
    if _plugin_log_date == today and _plugin_log_fh is not None:
        return  # already open for today
    # Close previous file if open
    if _plugin_log_fh is not None:
        try:
            _plugin_log_fh.close()
        except Exception:
            pass
    log_dir  = os.path.join(data_dir, "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, f"{today}.log")
    try:
        _plugin_log_fh = open(log_path, "a", encoding="utf-8")  # noqa: SIM115
    except Exception:
        _plugin_log_fh = None
    _plugin_log_date = today
    # Purge log files older than 14 days
    cutoff = datetime.now() - timedelta(days=14)
    try:
        for fname in os.listdir(log_dir):
            if fname.endswith(".log") and len(fname) == 14:
                try:
                    fdate = datetime.strptime(fname[:10], "%Y-%m-%d")
                    if fdate < cutoff:
                        os.remove(os.path.join(log_dir, fname))
                except (ValueError, OSError):
                    pass
    except OSError:
        pass


def log(message, level="INFO"):
    """Custom log function — writes to Indigo event log and daily plugin log file."""
    ts = datetime.now().strftime("%H:%M:%S")
    indigo.server.log(f"[{ts}] {message}", level=level)
    if _plugin_log_fh is not None:
        try:
            _plugin_log_fh.write(f"{ts} [{level:<7}] {message}\n")
            _plugin_log_fh.flush()
        except Exception:
            pass


def _local_time(dt, fmt="%H:%M"):
    """Format a UTC-aware datetime in Europe/London local time (BST/GMT).

    All datetimes from the Axle API and VPP state machine are UTC-aware.
    Displaying them without conversion shows UTC, which is 1 hour behind
    BST during British Summer Time (late March — late October).
    """
    try:
        import pytz
        local = pytz.timezone("Europe/London")
        return dt.astimezone(local).strftime(fmt)
    except Exception:
        return dt.strftime(fmt)   # fallback: still UTC but won't crash


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
        self.forecast  = None
        self.octopus  = None
        self.manager  = BatteryManager()
        self.axle     = None

        # Latest data from each module
        self.latest_inverter_data = {}
        self.latest_forecast_data = {}
        self.latest_rates_data    = {}
        self.latest_decision      = None

        # Web dashboard server (started in startup, stopped in shutdown)
        self.web_dashboard        = None

        # Poll timers
        self.store                   = {}   # mutable state dict (replaces globals)
        self.store["last_modbus"]    = 0.0
        self.store["last_manager"]   = 0.0
        self.store["last_forecast"]   = 0.0
        self.store["last_octopus"]   = 0.0
        self.store["last_profile"]   = 0.0
        self.store["last_vpp"]            = 0.0
        self.store["last_acc_save"]       = 0.0
        self.store["last_manager_action"] = ""
        self.store["last_manager_log"]    = 0.0
        self.store["last_overflow_cap_w"] = 0

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
        self.store["vpp_pre_export_active"] = False # True while plugin is pre-exporting
        self.store["vpp_charge_stopped"]    = False # True once pre-charge import has ended

        # Scheduled import state
        self.store["import_active"]          = False
        self.store["import_scheduled_time"]  = None
        self.store["import_target_soc"]      = 0.0

        # Export state (export limit is set once at startup; no dynamic tracking needed)
        self.store["export_active"]   = False

        # Storm watch state
        self.store["last_storm_watch"]    = 0.0    # time.time() of last poll
        self.store["last_energy_var"]     = 0.0    # time.time() of last variable write
        self._energy_var_ids: dict        = {}     # cached variable IDs by name
        self.store["storm_level"]         = "none" # current storm level
        self.store["storm_alerted_level"] = "none" # level at which alert was sent

        # Power cut detection
        self.store["grid_status_prev"] = "On-grid"  # previous poll's gridStatus

        # Solar overflow state (daytime charge-cap export)
        self.store["solar_overflow_active"]      = False
        self.store["solar_overflow_charge_cap_w"] = 0

        # Flood prevention state (overnight pre-drain)
        self.store["flood_prev_target_soc"] = None  # set when pre-drain export is active

        # Consumption profile (48 slots)
        self.store["consumption_profile"] = []

        # Long-lived home-load profile accumulators (persist across days; never reset at midnight)
        # Built from real homePowerWatts inverter readings, one reading per Modbus poll.
        self.store["home_profile_watts_sum"] = [0.0] * 48
        self.store["home_profile_count"]     = [0]   * 48

        self._load_accumulators()
        self._load_home_profile()   # restore accumulated inverter profile from disk

    def startup(self):
        _ensure_plugin_log(self.data_dir)
        log(f"{PLUGIN_NAME} v{PLUGIN_VERSION} starting")

        # ── Pref migrations ──────────────────────────────────────────────────
        # v3.0: raise dawnSocTarget minimum to 15% so there is a real buffer
        # above the 10% health floor on poor solar days.  Direct file edits are
        # overwritten by Indigo on shutdown, so we correct the value here and
        # let Indigo persist it naturally.
        _dawn_target = float(self.pluginPrefs.get("dawnSocTarget", "10"))
        if _dawn_target < 15.0:
            self.pluginPrefs["dawnSocTarget"] = "15"
            log(
                f"[Migration] dawnSocTarget raised from {_dawn_target:.0f}% to 15% "
                f"(minimum recommended to buffer above 10% health floor)"
            )

        self._init_modules()
        self.forecast.load_correction_factor()
        # Pre-populate latest_forecast_data from disk cache so the first manager
        # evaluation has forecast data available (disk cache was loaded in
        # OpenMeteoForecast.__init__; this propagates it into plugin.py's dict).
        self._refresh_forecast()
        self.store["last_forecast"] = time.time()
        # Set initial state images for all devices that already exist
        # (deviceStartComm handles newly created devices; this handles existing ones on reload)
        for dev in indigo.devices.iter("self"):
            self._set_device_initial_state(dev)
        log(f"{PLUGIN_NAME} ready")

        try:
            self.web_dashboard = WebDashboard(self, port=WEB_DASHBOARD_PORT)
            self.web_dashboard.start()
            log(f"[Web] Dashboard at http://192.168.100.160:{WEB_DASHBOARD_PORT}")
        except Exception as exc:
            log(f"[Web] Dashboard failed to start: {exc}", level="WARNING")

    def shutdown(self):
        log(f"{PLUGIN_NAME} shutting down")
        if self.web_dashboard:
            try:
                self.web_dashboard.stop()
                log("[Web] Dashboard stopped")
            except Exception as exc:
                log(f"[Web] Dashboard stop error: {exc}", level="WARNING")
        global _plugin_log_fh
        if _plugin_log_fh is not None:
            try:
                _plugin_log_fh.close()
            except Exception:
                pass
            _plugin_log_fh = None
        if self.modbus and self.modbus.connected:
            # Return to self-consumption on shutdown
            try:
                self.modbus.set_self_consumption()
            except Exception:
                pass
            self.modbus.disconnect()
        self._save_accumulators()

    # ------------------------------------------------------------------ #
    # Web dashboard data provider                                          #
    # ------------------------------------------------------------------ #

    def get_dashboard_data(self):
        """Return a dict of live system data for the web dashboard /api/status."""
        try:
            inv    = self.latest_inverter_data  or {}
            fcast  = self.latest_forecast_data  or {}
            rates  = self.latest_rates_data     or {}
            dec    = self.latest_decision

            tariff_info = rates.get("tariff_info", {})
            tracker     = rates.get("tracker", {})

            pv_w   = int(inv.get("pvPowerWatts",     0))
            bat_w  = int(inv.get("batteryPowerWatts", 0))
            grid_w = int(inv.get("gridPowerWatts",    0))
            home_w = int(inv.get("homePowerWatts",    0))
            soc    = float(inv.get("batterySoc",      0.0))

            # Hourly forecast: {hour_label: kWh}
            raw_hourly = fcast.get("_hourly_p50_today", {})
            hourly = {}
            for key in sorted(raw_hourly.keys()):
                wh = raw_hourly[key]
                try:
                    hour = int(str(key).split(" ")[1].split(":")[0])
                except (IndexError, ValueError):
                    continue
                hourly[f"{hour:02d}:00"] = round(wh / 1000.0, 2)

            # Self-sufficiency
            home_kwh   = self.store.get("home_daily_kwh", 0.0)
            import_kwh = self.store.get("grid_import_daily_kwh", 0.0)
            if home_kwh > 0:
                self_suff = round(max(0.0, (home_kwh - import_kwh) / home_kwh * 100.0), 1)
            else:
                self_suff = 100.0

            return {
                "timestamp":  datetime.now().strftime("%H:%M:%S"),
                "battery": {
                    "soc_pct":  round(soc, 1),
                    "power_w":  bat_w,
                },
                "solar": {
                    "power_w":        pv_w,
                    "today_kwh":      round(fcast.get("correctedTodayKwh",     0.0), 1),
                    "tomorrow_kwh":   round(fcast.get("correctedTomorrowKwh",  0.0), 1),
                    "bias_factor":    round(fcast.get("biasFactor",            1.0), 3),
                    "remaining_kwh":  round(fcast.get("remainingTodayKwh",     0.0), 1),
                },
                "grid": {
                    "power_w": grid_w,
                    "status":  inv.get("gridStatus", "On-grid"),
                },
                "home": {
                    "load_w": home_w,
                },
                "decision": {
                    "action":        dec.action         if dec else "unknown",
                    "reason":        dec.reason         if dec else "",
                    "dawn_viable":   dec.dawn_viable     if dec else True,
                    "soc_at_dawn_kwh": round(dec.soc_at_dawn_kwh if dec else 0.0, 1),
                },
                "tariff": {
                    "name":         tariff_info.get("display_name", "Unknown"),
                    "product_code": tariff_info.get("product_code", ""),
                    "today_p":      tracker.get("today_p"),
                    "tomorrow_p":   tracker.get("tomorrow_p"),
                },
                "today_summary": {
                    "pv_kwh":     round(self.store.get("pv_daily_kwh",          0.0), 2),
                    "import_kwh": round(import_kwh,                                   2),
                    "export_kwh": round(self.store.get("grid_export_daily_kwh", 0.0), 2),
                    "home_kwh":   round(home_kwh,                                     2),
                    "peak_soc":   round(self.store.get("peak_soc",            0.0), 1),
                    "min_soc":    round(self.store.get("min_soc",           100.0), 1),
                    "self_suff":  self_suff,
                },
                "vpp": {
                    "state":     self.store.get("vpp_state",  "idle"),
                    "active":    self.store.get("vpp_active", False),
                    "event_str": "",
                },
                "storm": {
                    "level": self.store.get("storm_level", "none"),
                },
                "flags": {
                    "export_active":         self.store.get("export_active",         False),
                    "solar_overflow_active": self.store.get("solar_overflow_active", False),
                    "import_active":         self.store.get("import_active",         False),
                    "modbus_connected":      bool(inv),
                },
                "hourly_forecast": hourly,
            }
        except Exception as exc:
            return {"error": str(exc), "timestamp": datetime.now().strftime("%H:%M:%S")}

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
        _ensure_plugin_log(self.data_dir)  # no-op unless date has rolled over
        # 1. Modbus poll
        if now - self.store["last_modbus"] >= MODBUS_POLL_INTERVAL:
            self._poll_modbus()
            self.store["last_modbus"] = now

        # 2. Solar forecast (before manager so decision always has fresh data)
        if now - self.store["last_forecast"] >= FORECAST_FETCH_INTERVAL:
            self._refresh_forecast()
            self.store["last_forecast"] = now

        # 3. Battery manager evaluation (every 60s — matches Modbus poll frequency)
        if now - self.store["last_manager"] >= MANAGER_EVAL_INTERVAL:
            self._evaluate_manager()
            self.store["last_manager"] = now

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

        # 10. Storm watch (every 2 hours)
        if now - self.store["last_storm_watch"] >= STORM_WATCH_INTERVAL:
            self._check_storm_watch()
            self.store["last_storm_watch"] = now

        # 11. Write energy summary to Indigo variables (every 30 min)
        if now - self.store["last_energy_var"] >= ENERGY_VAR_INTERVAL:
            self._write_energy_summary_variables()
            self.store["last_energy_var"] = now

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

        # Detect Off-grid → On-grid transition (power cut recovery)
        new_grid_status  = data.get("gridStatus", "On-grid")
        prev_grid_status = self.store.get("grid_status_prev", "On-grid")
        if prev_grid_status != "On-grid" and new_grid_status == "On-grid":
            restored_at = datetime.now(timezone.utc)
            self.store["power_restored_time"] = restored_at
            self.pluginPrefs["powerRestoredTime"] = restored_at.isoformat()
            log(
                f"[PowerCut] Grid restored after outage — export locked for "
                f"{POWER_CUT_LOCKOUT_HOURS:.0f} hours as precaution",
                level="WARNING",
            )
        self.store["grid_status_prev"] = new_grid_status

        # Update daily energy accumulators
        self._accumulate_daily_energy(data)

        # Accumulate home load into persistent half-hourly profile
        self._accumulate_home_profile(max(0.0, float(data.get("homePowerWatts", 0))))

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

        # Power cut lockout: suppress export for POWER_CUT_LOCKOUT_HOURS after grid restore
        export_enabled = bool(prefs.get("exportEnabled", False))
        prt_str = self.pluginPrefs.get("powerRestoredTime", "")
        if prt_str and export_enabled:
            try:
                power_restored = datetime.fromisoformat(prt_str)
                hours_since = (datetime.now(timezone.utc) - power_restored).total_seconds() / 3600.0
                if hours_since < POWER_CUT_LOCKOUT_HOURS:
                    export_enabled = False
            except (ValueError, TypeError):
                pass

        # Pre-compute VPP energy reserve for snapshot.
        # If an event is ANNOUNCED or PRE_CHARGING, protect that kWh from night export.
        _vpp_state = self.store.get("vpp_state", VPP_IDLE)
        _vpp_event = self.store.get("vpp_event") or {}
        _vpp_reserved_kwh = 0.0
        if _vpp_state in (VPP_ANNOUNCED, VPP_PRE_CHARGING) and _vpp_event:
            _max_export_kw   = float(prefs.get("maxExportKw", 4.0))
            _duration_hrs    = _vpp_event.get("duration_hrs", 1.0)
            _vpp_reserved_kwh = _max_export_kw * _duration_hrs / VPP_DISCHARGE_EFFICIENCY

        # Build snapshot
        snapshot = ManagerSnapshot(
            current_soc_pct    = soc_pct,
            capacity_kwh       = float(prefs.get("batteryCapacityKwh", 35.04)),
            efficiency         = float(prefs.get("batteryEfficiency", 94)) / 100.0,
            dawn_target_pct    = float(prefs.get("dawnSocTarget", 10)),    # v4.0: retained for VPP/storm
            health_cutoff_pct  = float(prefs.get("batteryHealthCutoff", 1)),
            export_enabled     = export_enabled,
            max_export_kw      = float(prefs.get("maxExportKw", 4.0)),
            weekday_kwh        = float(prefs.get("weekdayKwh", 22.0)),
            weekend_kwh        = float(prefs.get("weekendKwh", 30.0)),
            pv_watts                = int(self.latest_inverter_data.get("pvPowerWatts", 0)),
            house_load_watts        = int(self.latest_inverter_data.get("homePowerWatts", 0)),
            export_active           = self.store["export_active"],
            corrected_tomorrow_kwh  = float(self.latest_forecast_data.get("correctedTomorrowKwh", 0.0)),
            tariff                  = tariff_data,
            forecast_p50            = self.latest_forecast_data.get("_hourly_p50_today", {}),
            dawn_times         = self.latest_forecast_data.get("_dawn_times", {}),
            consumption_profile = self.store.get("consumption_profile", []),
            now                = datetime.now(timezone.utc),
            bias_factor                 = float(self.latest_forecast_data.get("biasFactor", 1.0)),
            vpp_active                  = self.store["vpp_active"],
            vpp_reserved_kwh            = _vpp_reserved_kwh,
            solar_overflow_active       = self.store["solar_overflow_active"],
            solar_overflow_charge_cap   = self.store["solar_overflow_charge_cap_w"],
            flood_prev_target_soc       = float(self.store.get("flood_prev_target_soc") or 0.0),
        )

        # --- Seasonal buffer: raise resilience floor Oct-Mar (longer nights, weaker solar) ---
        # Apr-Sep: summer buffer (dawnSocTarget, default 10%)
        # Oct-Mar: winter buffer (winterBufferPct, default 20%)
        try:
            import pytz as _pytz_s
            _local_month = datetime.now(_pytz_s.timezone("Europe/London")).month
        except Exception:
            _local_month = datetime.now().month

        if _local_month in (10, 11, 12, 1, 2, 3):
            _winter_buf = float(prefs.get("winterBufferPct", 20))
            if _winter_buf > snapshot.dawn_target_pct:
                snapshot.dawn_target_pct = _winter_buf
                log(
                    f"[Seasonal] Winter buffer active (month {_local_month}): "
                    f"resilience floor raised to {_winter_buf:.0f}%"
                )

        # --- Storm override: raise dawn target and suppress exports during storms ---
        storm_level = self.store.get("storm_level", "none")
        if storm_level in ("amber", "red"):
            override_soc = STORM_SOC_AMBER
        elif storm_level == "yellow":
            override_soc = STORM_SOC_YELLOW
        else:
            override_soc = None

        if override_soc is not None:
            snapshot.dawn_target_pct = max(snapshot.dawn_target_pct, override_soc)
            snapshot.export_enabled  = False   # never export during a storm
            log(
                f"[Storm] Storm override active (level={storm_level}): "
                f"dawn target raised to {snapshot.dawn_target_pct:.0f}%, export suppressed"
            )

        decision = self.manager.evaluate(snapshot)
        self.latest_decision = decision

        # Log on: action change or 15-min heartbeat only.
        # Solar overflow cap shifts are silent Modbus writes — logged only when
        # the action itself changes (overflow starts/stops) or at heartbeat.
        # Indigo shows all indigo.server.log() calls regardless of level= parameter,
        # so any per-cap-change log line would flood the event log during sunny days.
        _last_action    = self.store.get("last_manager_action", "")
        _last_log       = self.store.get("last_manager_log", 0.0)
        _action_changed = decision.action != _last_action
        _heartbeat      = (time.time() - _last_log) >= MANAGER_LOG_INTERVAL

        if _action_changed or _heartbeat:
            if decision.action == ACTION_SOLAR_OVERFLOW:
                # Header on its own line; each continuation is a separate log call so
                # Indigo renders them as proper rows with the plugin name column —
                # content then aligns naturally with all other log messages.
                log(
                    f"[Manager] SOC={soc_pct:.1f}%  PV={snapshot.pv_watts}W  "
                    f"Action=solar_overflow"
                )
                for _line in decision.reason.split("\n"):
                    indigo.server.log(f"  {_line}")
            else:
                log(
                    f"[Manager] SOC={soc_pct:.1f}%  PV={snapshot.pv_watts}W  "
                    f"Action={decision.action}  {decision.reason}"
                )
            self.store["last_manager_action"] = decision.action
            self.store["last_manager_log"]    = time.time()

        # Verify persistent inverter registers haven't drifted before acting
        self._verify_ems_registers()

        # Act on the decision
        self._act_on_decision(decision)

        # Update batteryManager device states
        self._update_manager_device(decision, snapshot)

    # ================================================================
    # Storm Watch
    # ================================================================

    def _send_pushover(self, title, message, priority="0"):
        """Send a Pushover notification. Called from the main plugin thread."""
        try:
            pushover = indigo.server.getPlugin("io.thechad.indigoplugin.pushover")
            if pushover and pushover.isEnabled():
                pushover.executeAction("sendPushover", props={
                    "title":    title,
                    "message":  message,
                    "priority": priority,
                })
                log(f"[Storm] Pushover sent: {title}")
            else:
                log("[Storm] Pushover plugin not enabled — alert not sent", level="WARNING")
        except Exception as exc:
            log(f"[Storm] Pushover send failed: {exc}", level="WARNING")

    def _check_storm_watch(self):
        """
        Poll Open-Meteo and MeteoAlarm for incoming wind/storm risk.
        Updates self.store['storm_level'] and sends a Pushover alert when
        the level escalates.  Sends an all-clear when level drops back to 'none'.
        """
        try:
            new_level, reason = check_storm_level()
        except Exception as exc:
            log(f"[Storm] check_storm_level() raised: {exc}", level="WARNING")
            return

        prev_level    = self.store.get("storm_level", "none")
        alerted_level = self.store.get("storm_alerted_level", "none")

        self.store["storm_level"] = new_level

        log(f"[Storm] Level={new_level}  prev={prev_level}")
        indigo.server.log(f"  {reason}")

        # Severity indices for comparison
        new_idx     = STORM_LEVELS.index(new_level)    if new_level    in STORM_LEVELS else 0
        alerted_idx = STORM_LEVELS.index(alerted_level) if alerted_level in STORM_LEVELS else 0

        # Escalation: new level is more severe than the last alert sent
        if new_idx > alerted_idx:
            if new_level == "yellow":
                title = "Storm Watch - Yellow"
                body  = (
                    f"A yellow-level wind risk is forecast for Medomsley. "
                    f"Battery will be charged to {STORM_SOC_YELLOW:.0f}% and "
                    f"export suspended until the risk passes.\n\n{reason}"
                )
                priority = "0"
            elif new_level == "amber":
                title = "Storm Warning - Amber"
                body  = (
                    f"An amber-level wind/storm warning is active for your area. "
                    f"Battery will be charged to {STORM_SOC_AMBER:.0f}% and "
                    f"export suspended as a precaution against power cuts.\n\n{reason}"
                )
                priority = "1"   # high priority
            else:  # red
                title = "Storm Warning - RED"
                body  = (
                    f"A RED storm warning is active for your area. "
                    f"Battery will be charged to {STORM_SOC_AMBER:.0f}% and "
                    f"all exports suspended. Power cut risk is high.\n\n{reason}"
                )
                priority = "1"   # high priority
            self._send_pushover(title, body, priority)
            self.store["storm_alerted_level"] = new_level

        # De-escalation: level dropped back to none after an alert was sent
        elif new_level == "none" and alerted_idx > 0:
            self._send_pushover(
                "Storm Watch Cleared",
                "Storm/wind risk has passed for Medomsley. "
                "Normal battery management and export schedule resumed.",
                priority="0",
            )
            self.store["storm_alerted_level"] = "none"

    def _build_tariff_data(self):
        """Build a TariffData object from the latest Octopus rates."""
        rates       = self.latest_rates_data
        tariff_info = rates.get("tariff_info", {})
        tariff_key  = tariff_info.get("tariff_key", TARIFF_TRACKER)

        tracker  = rates.get(TARIFF_TRACKER, {})
        tou      = rates.get(tariff_key, {})     # cheap window data for Go/Flux/iGo/iFlux

        # today_rate_p: use the active tariff's rate, not always Tracker.
        # Flexible is a flat rate — no tomorrow rate.
        if tariff_key == TARIFF_FLEXIBLE:
            today_rate_p    = rates.get(TARIFF_FLEXIBLE, {}).get("today_p")
            tomorrow_rate_p = None
        else:
            today_rate_p    = tracker.get("today_p")
            tomorrow_rate_p = tracker.get("tomorrow_p")

        return TariffData(
            tariff_key      = tariff_key,
            today_rate_p    = today_rate_p,
            tomorrow_rate_p = tomorrow_rate_p,
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
                # Flood prevention uses DNO export cap (decision.power_watts).
                # Legacy night export falls back to full inverter capacity (inv_max_w).
                export_w = decision.power_watts if decision.power_watts > 0 else inv_max_w
                if self.modbus.night_export(export_w):
                    # Set hardware floor so battery stops automatically at target SOC.
                    # Plugin resets this cutoff on return to self-consumption.
                    if decision.target_soc_pct > 0:
                        self.modbus.set_discharge_cutoff(decision.target_soc_pct)
                        self.store["flood_prev_target_soc"] = decision.target_soc_pct
                        log(f"[Manager] Discharge cutoff set to {decision.target_soc_pct:.0f}% "
                            f"(flood prevention floor)")
                    self.store["export_active"] = True
                    self._trigger_event("exportStarted")

        elif action == ACTION_STOP_EXPORT:
            if prev_export:
                log("[Manager] Stopping night export - returning to self-consumption")
                self.modbus.set_self_consumption()
                # Clean up flood prevention cutoff if it was active
                flood_target = self.store.get("flood_prev_target_soc")
                if flood_target:
                    health_floor = float(self.pluginPrefs.get("batteryHealthCutoff", 1))
                    self.modbus.set_discharge_cutoff(health_floor)
                    log(f"[Manager] Discharge cutoff reset to {health_floor:.0f}% (health floor)")
                    self.store["flood_prev_target_soc"] = None
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
                    f"charge cap {cap_w}W"
                )
                indigo.server.log("  PV surplus flowing to grid")
                self.modbus.set_self_consumption()
                # If flood prevention was running overnight and dawn broke before
                # the target SOC was reached, reset the discharge cutoff register
                # now so it does not act as a hidden floor during daytime operation.
                flood_target = self.store.get("flood_prev_target_soc")
                if flood_target:
                    health_floor = float(self.pluginPrefs.get("batteryHealthCutoff", 1))
                    self.modbus.set_discharge_cutoff(health_floor)
                    log(f"[Manager] Discharge cutoff reset to {health_floor:.0f}% "
                        f"(flood prevention interrupted at dawn)")
                    self.store["flood_prev_target_soc"] = None
                self.modbus.set_charge_limit(cap_w, quiet=True)
                self.store["solar_overflow_active"]       = True
                self.store["solar_overflow_charge_cap_w"] = cap_w
                self.store["export_active"]               = False
                self.store["import_active"]               = False
            elif abs(prev_cap - cap_w) > 500:
                # Cap has shifted by more than deadband — update inverter register silently.
                # No log here: Indigo shows all indigo.server.log() calls regardless of
                # level= so any per-cap-change line floods the event log. The 15-min
                # heartbeat summary already reflects the current cap in its reason string.
                self.modbus.set_charge_limit(cap_w, quiet=True)
                self.store["solar_overflow_charge_cap_w"] = cap_w
            # else: cap within deadband — idempotent, no Modbus writes

        elif action == ACTION_SELF_CONSUMPTION:
            # Determine if inverter is currently in a non-self-consumption mode.
            # Check store flags first; fall back to actual emsWorkMode from inverter data
            # so a restart (which resets all flags to False) can still recover a stuck mode.
            ems_mode_str   = self.latest_inverter_data.get("emsWorkMode", "")
            inverter_stuck = ems_mode_str in ("Discharge ESS First", "Charge Grid First")

            if prev_import:
                # Only cancel an active import if the target SOC has been reached.
                # Without this guard the manager oscillates: one tick of importing
                # nudges SOC above the viability floor → next evaluate() returns
                # SELF_CONSUMPTION → import cancelled → SOC drops → repeat.
                current_soc = self.latest_inverter_data.get("batterySoc", 0.0)
                target_soc  = self.store.get("import_target_soc", 0.0)
                if current_soc >= target_soc:
                    log(f"[Manager] Import complete ({current_soc:.1f}% >= {target_soc:.0f}%) - returning to self-consumption")
                    self.modbus.set_self_consumption()
                    self.store["import_active"]     = False
                    self.store["import_target_soc"] = 0.0
                else:
                    log(f"[Manager] Holding import - SOC {current_soc:.1f}% / target {target_soc:.0f}%",
                        level="DEBUG")
            elif prev_export:
                # Flood prevention complete, export disabled, or other export end
                flood_target = self.store.get("flood_prev_target_soc")
                current_soc  = self.latest_inverter_data.get("batterySoc", 0.0)
                if flood_target:
                    log(f"[Manager] Flood prevention complete "
                        f"(SOC {current_soc:.1f}% reached {flood_target:.0f}% target) "
                        f"— returning to self-consumption")
                else:
                    log("[Manager] Export disabled — returning to self-consumption")
                self.modbus.set_self_consumption()
                if flood_target:
                    health_floor = float(self.pluginPrefs.get("batteryHealthCutoff", 1))
                    self.modbus.set_discharge_cutoff(health_floor)
                    log(f"[Manager] Discharge cutoff reset to {health_floor:.0f}% (health floor)")
                    self.store["flood_prev_target_soc"] = None
                self.store["export_active"] = False
            elif self.store.get("solar_overflow_active"):
                # SOC dropped below release threshold — restore full charge rate
                log("[Manager] Solar overflow released — restoring full charge limit")
                self.modbus.set_self_consumption()   # resets charge limit to inv_max_w
                self.store["solar_overflow_active"]       = False
                self.store["solar_overflow_charge_cap_w"] = 0
            elif inverter_stuck:
                # Inverter is in wrong mode (e.g. stuck in 0x06 after restart cleared store flags)
                log(f"[Manager] Inverter stuck in '{ems_mode_str}' — forcing self-consumption",
                    level="WARNING")
                self.modbus.set_self_consumption()

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

        # --- EMS mode ---
        # Skip during VPP_ACTIVE and VPP_COOLING_OFF: Axle has control of the inverter
        # and may have changed register 40031. Writing to it during this window could
        # fight Axle's firmware or confuse the inverter during the handback sequence.
        # _vpp_check_axle_release() reinstates Remote EMS once Axle has fully released.
        # Also skip while vpp_pre_export_active: the plugin has intentionally set the
        # inverter to 0x06 (Discharge ESS First) a few minutes before the event window
        # opens — correcting it back to Self Consumption would cancel the pre-export.
        _vpp_state      = self.store.get("vpp_state", VPP_IDLE)
        _pre_exporting  = self.store.get("vpp_pre_export_active", False)
        if _vpp_state not in (VPP_ACTIVE, VPP_COOLING_OFF) and not _pre_exporting:
            # Determine what mode the inverter should be in based on store flags.
            # After a restart all flags are False, so expected_mode = 0x02 (Self Consumption).
            # If the inverter is stuck in 0x06 (Discharge ESS First) from overnight export
            # this will catch and correct it on the next manager tick.
            if self.store.get("export_active"):
                expected_mode = 0x06  # Discharge ESS First
            elif self.store.get("import_active"):
                expected_mode = 0x03  # Charge Grid First
            else:
                expected_mode = 0x02  # Max Self Consumption

            actual_mode = self.modbus.read_ems_mode()
            if actual_mode is not None and actual_mode != expected_mode:
                mode_names = {0x02: "Self Consumption", 0x03: "Charge Grid First", 0x06: "Discharge ESS First"}
                log(
                    f"[Verify] EMS mode mismatch: inverter={mode_names.get(actual_mode, actual_mode)} "
                    f"expected={mode_names.get(expected_mode, expected_mode)} — correcting",
                    level="WARNING",
                )
                self.modbus.set_remote_ems_mode(expected_mode)

        # --- Discharge limit and charge limit ---
        # Skip during VPP_ACTIVE and VPP_COOLING_OFF: Axle controls these registers.
        # Writing them during the handover window caused a brief 2kW grid import on
        # 10-Apr-2026 when the solar_overflow charge cap (2395W) was written back 1s
        # after Axle cleared it to allow full discharge.
        if _vpp_state not in (VPP_ACTIVE, VPP_COOLING_OFF):
            actual_discharge_w = self.modbus.read_discharge_limit()
            if actual_discharge_w is not None:
                if abs(actual_discharge_w - expected_discharge_w) > 200:
                    log(
                        f"[Verify] Discharge limit mismatch: inverter={actual_discharge_w}W "
                        f"expected={expected_discharge_w}W — correcting",
                        level="WARNING",
                    )
                    self.modbus.set_discharge_limit(expected_discharge_w)

            actual_charge_w = self.modbus.read_charge_limit()
            if actual_charge_w is not None:
                if abs(actual_charge_w - expected_charge_w) > 200:
                    log(
                        f"[Verify] Charge limit mismatch: inverter={actual_charge_w}W "
                        f"expected={expected_charge_w}W — correcting",
                        level="WARNING",
                    )
                    self.modbus.set_charge_limit(expected_charge_w)

        # --- Discharge cutoff (health floor, register 40048) ---
        # This register physically stops battery discharge. It is only written by
        # VPP code, so on systems without VPP activity it drifts to whatever the
        # inverter factory default is (typically 5%). Verify every cycle so the
        # hardware floor always matches the plugin's batteryHealthCutoff preference.
        # Skip if VPP has temporarily raised the cutoff — the VPP state machine owns it.
        # Skip if flood prevention has temporarily raised the cutoff — it owns it too.
        if not self.store.get("vpp_cutoff_raised") and not self.store.get("flood_prev_target_soc"):
            expected_cutoff_pct = float(self.pluginPrefs.get("batteryHealthCutoff", 1.0))
            actual_cutoff_pct   = self.modbus.read_discharge_cutoff()
            if actual_cutoff_pct is not None:
                if abs(actual_cutoff_pct - expected_cutoff_pct) > 0.5:
                    log(
                        f"[Verify] Discharge cutoff mismatch: inverter={actual_cutoff_pct:.1f}% "
                        f"expected={expected_cutoff_pct:.1f}% — correcting",
                        level="WARNING",
                    )
                    self.modbus.set_discharge_cutoff(expected_cutoff_pct)

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
    # Solar Forecast Refresh
    # ================================================================

    def _refresh_forecast(self, force=False):
        """Fetch updated solar forecast from Open-Meteo."""
        if not self.forecast:
            return

        data = self.forecast.fetch_forecast(force=force)
        self.latest_forecast_data = data

        self._update_forecast_device(data)
        self._update_forecast_variables(data)

        status   = data.get("forecastStatus", "")
        tmrw_kwh = data.get("correctedTomorrowKwh", 0.0)

        if "No data" in status:
            log(f"[Solar] WARNING: forecast unavailable ({status}) — night export condition 3 will block", level="WARNING")
        elif tmrw_kwh == 0.0:
            log(f"[Solar] WARNING: tomorrow forecast is 0.0 kWh (status: {status!r}) — night export condition 3 will block", level="WARNING")
        else:
            log(
                f"[Solar] Today: {data.get('correctedTodayKwh', 0):.1f} kWh "
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

            # Log on first fetch or when tariff / rate changes; also in debug mode
            tracker    = monitored.get("tracker", {})
            tariff_key = tariff_info.get("tariff_key", "?")
            today_rate = tracker.get("today_p")
            _changed   = (tariff_key != self.store.get("_last_tariff_key")
                          or today_rate != self.store.get("_last_tariff_rate"))
            if _changed:
                self.store["_last_tariff_key"]  = tariff_key
                self.store["_last_tariff_rate"] = today_rate
            if _changed or self.debug:
                log(
                    f"[Octopus] Tariff: {tariff_info.get('display_name', tariff_key)} "
                    f"({tariff_info.get('product_code', '?')}), "
                    f"today: {today_rate}p, "
                    f"tomorrow: {tracker.get('tomorrow_p', 'TBD')}p"
                )

        except Exception as e:
            log(f"[Octopus] Rate refresh error: {e}", level="ERROR")

    def _accumulate_home_profile(self, home_watts):
        """Accumulate one inverter home-load reading into the 48-slot half-hourly profile.

        Called every Modbus poll (~60s).  Readings are averaged per 30-min slot over
        many days, giving a robust consumption profile that reflects actual house load
        rather than the Octopus import meter (which shows only ~0.7 kWh/day on a
        near self-sufficient system instead of the real ~12-15 kWh/day load).
        """
        now  = datetime.now()
        slot = now.hour * 2 + (1 if now.minute >= 30 else 0)
        slot = max(0, min(47, slot))
        self.store["home_profile_watts_sum"][slot] += home_watts
        self.store["home_profile_count"][slot]     += 1

    def _refresh_consumption_profile(self, force=False):
        """Rebuild 48-slot consumption profile from accumulated inverter readings.

        Each slot (0=00:00, 1=00:30 … 47=23:30) holds the average homePowerWatts
        seen during that half-hour across all polling days.  Slots with fewer than
        HOME_PROFILE_MIN_READINGS readings fall back to the OctopusAPI default
        (UK typical ~12 kWh/day shape) so the first day still works correctly.

        Profile values are kWh per half-hourly slot (watts × 0.5 h / 1000).
        """
        try:
            default  = OctopusAPI._default_consumption_profile()
            watts_sum = self.store["home_profile_watts_sum"]
            counts    = self.store["home_profile_count"]
            profile   = []
            real_slots = 0
            for i in range(48):
                if counts[i] >= HOME_PROFILE_MIN_READINGS:
                    avg_watts = watts_sum[i] / counts[i]
                    profile.append(round(avg_watts * 0.5 / 1000.0, 4))
                    real_slots += 1
                else:
                    profile.append(default[i])

            self.store["consumption_profile"] = profile
            daily_kwh = sum(profile)
            log(
                f"[Profile] Consumption profile updated from inverter data — "
                f"daily: {daily_kwh:.1f} kWh  "
                f"({real_slots}/48 slots from real data, "
                f"{48 - real_slots} using default)"
            )
        except Exception as e:
            log(f"[Profile] Refresh error: {e}", level="ERROR")

    def _save_home_profile(self):
        """Persist home-load profile accumulators to disk (home_load_profile.json).

        Written every 5 minutes (via _save_accumulators) and on plugin shutdown.
        The file survives across restarts and day-rollover; it is never deleted.
        """
        path = os.path.join(self.data_dir, "home_load_profile.json")
        data = {
            "watts_sum": self.store["home_profile_watts_sum"],
            "count":     self.store["home_profile_count"],
            "saved_at":  datetime.now().isoformat(),
        }
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f)
        except Exception as e:
            self.logger.warning(f"Cannot save home profile: {e}")

    def _load_home_profile(self):
        """Restore home-load profile accumulators from disk on startup.

        If no file exists (fresh install) the in-memory defaults of all-zeros
        remain, and the first HOME_PROFILE_MIN_READINGS days fall back to the
        OctopusAPI default shape.
        """
        path = os.path.join(self.data_dir, "home_load_profile.json")
        if not os.path.exists(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            watts_sum = data.get("watts_sum", [])
            counts    = data.get("count", [])
            if len(watts_sum) == 48 and len(counts) == 48:
                self.store["home_profile_watts_sum"] = [float(v) for v in watts_sum]
                self.store["home_profile_count"]     = [int(v)   for v in counts]
                # Immediately build consumption_profile from restored data
                self._refresh_consumption_profile()
                real_slots = sum(1 for c in counts if c >= HOME_PROFILE_MIN_READINGS)
                self.logger.info(
                    f"Home load profile restored — {real_slots}/48 slots from real data"
                )
        except Exception as e:
            self.logger.warning(f"Cannot load home profile: {e}")

    # ================================================================
    # VPP State Machine
    # ================================================================

    def _vpp_poll_interval(self):
        """Return adaptive VPP poll interval.

        COOLING_OFF polls at 60s: Axle can release the inverter within 1-2 minutes
        of an event ending. The Modbus poll already refreshes emsWorkMode every 60s;
        we need to check equally often so Remote EMS is reinstated promptly.
        """
        state = self.store["vpp_state"]
        event = self.store["vpp_event"]

        if state in (VPP_ACTIVE, VPP_COOLING_OFF):
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
                if self.store.get("vpp_pre_export_active"):
                    if self.modbus:
                        self.modbus.set_self_consumption()
                    log("[VPP] Pre-export stopped (event cancelled)")
                self.store["vpp_pre_export_active"] = False
                self.store["vpp_charge_stopped"]    = False
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
            # Discharge cutoff is NOT raised here — it is raised at pre-charge time
            # (30 min before event). Raising it at announcement (up to 24h early)
            # locks the battery below the floor if SOC is low, causing unnecessary
            # grid imports. Solar will always restore SOC before the event; if not,
            # Axle's own firmware will decline to dispatch.
            self._vpp_transition(VPP_ANNOUNCED)
            self._trigger_event("vppAnnounced")
            log(
                f"[VPP] Event announced: {_local_time(start_time)} - "
                f"{_local_time(end_time)} BST ({event['duration_hrs']:.1f}h)"
            )

        elif current_state == VPP_IDLE and hours_to_start <= 0 and now < end_time:
            # Axle published the event late — it's already under way.
            # Skip straight to ACTIVE; Axle's firmware already has control.
            mins_late = int(-hours_to_start * 60)
            log(
                f"[VPP] Late detection: event already active {_local_time(start_time)} - "
                f"{_local_time(end_time)} BST (Axle published {mins_late} min late) — "
                f"entering ACTIVE, suppressing plugin Modbus writes"
            )
            self.store["vpp_event"] = event
            self.store["vpp_export_start_kwh"] = self.store["grid_export_daily_kwh"]
            self._vpp_transition(VPP_ACTIVE)
            self.store["vpp_active"] = True
            self._trigger_event("vppStarted")

        elif current_state == VPP_ANNOUNCED:
            if hours_to_start <= 1.0:
                log(f"[VPP] Event in {hours_to_start * 60:.0f} min - preparing")
            if hours_to_start <= 0.5:
                self._start_vpp_precharge(event)

        elif current_state == VPP_PRE_CHARGING:
            required_soc      = self.store["vpp_pre_charge_soc"]
            current_soc       = self.latest_inverter_data.get("batterySoc", 0.0)
            pre_export_active = self.store.get("vpp_pre_export_active", False)
            charge_stopped    = self.store.get("vpp_charge_stopped", False)
            soc_ready         = current_soc >= required_soc

            # Step 1: stop charging once SOC target is reached (fire once only)
            if soc_ready and not pre_export_active and not charge_stopped:
                if self.modbus:
                    self.modbus.set_self_consumption()
                self.store["vpp_charge_stopped"] = True
                log(f"[VPP] Pre-charge complete — SOC {current_soc:.0f}% >= {required_soc:.0f}% target. Holding.")

            # Step 2: pre-export — start exporting VPP_PRE_EXPORT_MINUTES before event.
            # Axle pays on smart meter readings during their window, not dispatch commands.
            # Starting export early ensures the full event window is captured even when
            # Axle's dispatch command arrives late (e.g. 08:30 event, command at 08:45).
            if (soc_ready and not pre_export_active
                    and 0 < hours_to_start <= VPP_PRE_EXPORT_MINUTES / 60.0):
                inv_max_w = int(float(self.pluginPrefs.get("inverterMaxKw", 10.0)) * 1000)
                if self.modbus and self.modbus.night_export(inv_max_w):
                    self.store["vpp_pre_export_active"] = True
                    log(
                        f"[VPP] Pre-export started {int(hours_to_start * 60 + 0.5)} min "
                        f"before event (SOC {current_soc:.0f}%) — "
                        f"capturing full metered window from event start"
                    )

            # Step 3: event window open — hand over to Axle
            if hours_to_start <= 0:
                # Record export baseline at the moment the paid window opens.
                # Export may already be running from pre-export above; Axle's own
                # command will confirm the same mode — no conflict.
                self.store["vpp_export_start_kwh"]  = self.store["grid_export_daily_kwh"]
                self.store["vpp_pre_export_active"] = False
                self.store["vpp_charge_stopped"]    = False
                self._vpp_transition(VPP_ACTIVE)
                self.store["vpp_active"] = True
                self._trigger_event("vppStarted")
                log(f"[VPP] Event ACTIVE - Axle has control (export already running)"
                    if pre_export_active else
                    f"[VPP] Event ACTIVE - Axle has control")

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
            indigo.server.sendEmailTo("axle@strudwick.co.uk", subject=subject, body=html)
            log("[VPP] Email alert sent to axle@strudwick.co.uk")
        except Exception as e:
            log(f"[VPP] Email alert failed: {e}", level="WARNING")

    def _start_vpp_precharge(self, event):
        """Assess SOC 30 min before VPP event; raise discharge cutoff; no grid import.

        The discharge cutoff is raised here (not at announcement) so it only
        applies close to the event — avoiding unnecessary battery lockout hours
        in advance. If SOC is below the required level, we do NOT import from
        grid: solar will have charged the battery throughout the day, and if
        energy is still short, Axle's own firmware will decline to dispatch
        rather than us importing at cost to cover their export.
        """
        duration_hrs    = event.get("duration_hrs", 1.0)
        cap_kwh         = float(self.pluginPrefs.get("batteryCapacityKwh", BATTERY_CAPACITY_KWH))
        max_export_kw   = float(self.pluginPrefs.get("maxExportKw", 4.0))
        dawn_target_pct = float(self.pluginPrefs.get("dawnSocTarget", 10))

        # For daytime events solar will recharge during/after the event, so we
        # only need to hold the export energy itself.  For night events we must
        # also hold the dawn reserve so the battery survives until morning.
        is_daytime  = self._event_is_daytime(event.get("start_time"))
        export_kwh  = max_export_kw * duration_hrs / VPP_DISCHARGE_EFFICIENCY
        if is_daytime:
            required_kwh = export_kwh
            required_soc = min(100.0, (required_kwh / cap_kwh) * 100.0)
            dawn_kwh     = 0.0
        else:
            dawn_kwh     = cap_kwh * dawn_target_pct / 100.0
            required_kwh = export_kwh + dawn_kwh
            required_soc = min(100.0, (required_kwh / cap_kwh) * 100.0)
            required_soc = max(required_soc, dawn_target_pct)

        # Current battery level
        current_soc  = self.latest_inverter_data.get("batterySoc", 0.0)
        current_kwh  = cap_kwh * current_soc / 100.0

        self.store["vpp_pre_charge_soc"] = required_soc

        # Set discharge cutoff now (30 min before) — not at announcement time
        self._set_vpp_discharge_cutoff(event, is_daytime)

        if current_kwh >= required_kwh:
            if is_daytime:
                log(
                    f"[VPP] SOC sufficient ({current_soc:.0f}%, {current_kwh:.1f} kWh) for "
                    f"{duration_hrs:.1f}h export ({export_kwh:.1f} kWh) — daytime, solar will recharge"
                )
            else:
                log(
                    f"[VPP] SOC sufficient ({current_soc:.0f}%, {current_kwh:.1f} kWh) for "
                    f"{duration_hrs:.1f}h export ({export_kwh:.1f} kWh) + dawn reserve "
                    f"({dawn_kwh:.1f} kWh)"
                )
        else:
            shortfall = required_kwh - current_kwh
            log(
                f"[VPP] SOC low ({current_soc:.0f}%, shortfall {shortfall:.1f} kWh) — "
                f"proceeding without grid import; Axle will assess at dispatch time"
            )

        self._vpp_transition(VPP_PRE_CHARGING)

    def _set_vpp_discharge_cutoff(self, event, is_daytime=False):
        """Set discharge cutoff at pre-charge time (30 min before event).

        Daytime events (solar forecast available during/after event):
          Use the health floor (1%) — the battery can discharge freely because
          solar will recharge it during the day.  No need to hold a dawn reserve.

        Night events (before dawn or after dusk, no solar recharge coming):
          Use the dawn target (15%) — ensures the battery can survive overnight
          until the next morning's solar even after the event has dispatched.

        Called from _start_vpp_precharge() with the is_daytime flag already
        determined, NOT at announcement time.
        """
        health_floor    = float(self.pluginPrefs.get("batteryHealthCutoff", 1.0))
        dawn_target_pct = float(self.pluginPrefs.get("dawnSocTarget", 10))

        if is_daytime:
            floor_pct = health_floor
            reason    = "daytime event — solar will recharge"
        else:
            floor_pct = dawn_target_pct
            reason    = f"night event — protecting dawn floor"

        floor_pct = max(floor_pct, health_floor)  # never below the health floor

        if self.modbus:
            self.modbus.set_discharge_cutoff(floor_pct)
            self.store["vpp_cutoff_raised"] = True   # prevents verify() fighting the VPP floor
            log(f"[VPP] Discharge cutoff set to {floor_pct:.0f}% ({reason})")

    def _event_is_daytime(self, event_start):
        """Return True if event_start falls within the solar generation window.

        Uses _dawn_times (first PV slot above threshold) and the hourly forecast
        (last non-zero slot) to bracket the solar window for the event's date.
        Returns False if event_start is None or solar data is unavailable —
        night-event behaviour is the safe fallback.
        """
        if event_start is None:
            return False

        fcast      = self.latest_forecast_data or {}
        dawn_times = fcast.get("_dawn_times", {})

        # Convert event start to local (London) time for date lookup
        try:
            import pytz as _pytz
            _london     = _pytz.timezone("Europe/London")
            event_local = event_start.astimezone(_london)
        except Exception:
            event_local = event_start

        event_date_str = event_local.strftime("%Y-%m-%d")

        # Dawn: first PV-generating slot on the event's date
        dawn = dawn_times.get(event_date_str)
        if dawn is None:
            return False   # no solar expected that day

        # Dusk: last slot with non-zero generation on the event's date
        # Check both today and tomorrow hourly dicts
        dusk = None
        for hourly_key in ("_hourly_p50_today", "_hourly_p50_tomorrow"):
            hourly = fcast.get(hourly_key, {})
            for slot_str in sorted(hourly.keys(), reverse=True):
                if slot_str.startswith(event_date_str) and hourly[slot_str] > 0:
                    try:
                        dt_naive = datetime.strptime(slot_str, "%Y-%m-%d %H:%M:%S")
                        try:
                            import pytz as _pytz
                            _london = _pytz.timezone("Europe/London")
                            dusk = _london.localize(dt_naive)
                        except Exception:
                            dusk = dt_naive
                    except Exception:
                        pass
                    break
            if dusk is not None:
                break

        if dusk is None:
            return False   # can't determine dusk — treat as night

        # Compare event_start against the solar window
        try:
            return dawn <= event_start <= dusk
        except TypeError:
            # Mixed tz-aware / naive — strip timezone for comparison
            def _naive(dt):
                return dt.replace(tzinfo=None) if getattr(dt, "tzinfo", None) else dt
            return _naive(dawn) <= _naive(event_start) <= _naive(dusk)

    def _restore_discharge_cutoff(self):
        """Restore discharge cutoff to the health floor after VPP."""
        if self.modbus:
            health_floor = float(self.pluginPrefs.get("batteryHealthCutoff", 1.0))
            self.modbus.set_discharge_cutoff(health_floor)
            self.store["vpp_cutoff_raised"] = False   # allow verify() to manage cutoff again
            log(f"[VPP] Discharge cutoff restored to {health_floor:.0f}%")

    def _vpp_transition(self, new_state):
        """Transition VPP state machine to a new state."""
        old_state = self.store["vpp_state"]
        self.store["vpp_state"] = new_state
        if self.debug:
            log(f"[VPP] State: {old_state} -> {new_state}")

        # When entering ACTIVE, release any solar_overflow charge cap.
        # Axle needs full control of the charge/discharge registers; leaving a
        # reduced charge cap in place caused a 2kW grid import on 10-Apr-2026
        # as Axle cleared it and _verify_ems_registers() wrote it back 1s later.
        if new_state == VPP_ACTIVE and self.store.get("solar_overflow_active"):
            log("[VPP] Clearing solar overflow cap before handing control to Axle")
            if self.modbus and self.modbus.connected:
                self.modbus.set_self_consumption()
            self.store["solar_overflow_active"]       = False
            self.store["solar_overflow_charge_cap_w"] = 0

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
        self.forecast.capture_morning_forecast()

        # Write accuracy record for yesterday
        self.forecast.record_accuracy(self.store["pv_daily_kwh"])

        # Write daily history ring buffer
        self._write_daily_history(yesterday)

        # Write final daily totals to Indigo variables before reset
        self._write_energy_summary_variables()
        self.store["last_energy_var"] = time.time()

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
        """Push Open-Meteo forecast to solarForecast device."""
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

    def _update_forecast_variables(self, data):
        """Write solar forecast totals to Indigo variables in the Sigenergy folder."""
        today_kwh    = data.get("correctedTodayKwh",   0.0)
        tomorrow_kwh = data.get("correctedTomorrowKwh", 0.0)
        now_str      = datetime.now().strftime("%H:%M %d/%m/%Y")

        updates = [
            (1085965464, str(today_kwh)),    # solcast_today_kwh
            (1029984958, str(tomorrow_kwh)), # solcast_tomorrow_kwh
            (1287165951, now_str),           # solcast_last_updated
        ]
        for var_id, value in updates:
            try:
                indigo.variable.updateValue(var_id, value)
            except Exception as e:
                log(f"[Solar] Variable update failed (id {var_id}): {e}", level="WARNING")

    def _sigenergy_folder_id(self) -> int:
        """Return the Sigenergy variable folder ID, or 0 (root) if not found."""
        try:
            for fid in indigo.variables.folders:
                f = indigo.variables.folders[fid]
                if f.name.lower() in ("sigenergy", "sigen energy", "sigen"):
                    return f.id
        except Exception:
            pass
        return 0

    def _ensure_var(self, name: str, folder_id: int) -> int:
        """
        Return the Indigo variable ID for `name`, creating it in `folder_id`
        if it does not already exist. Caches the result in self._energy_var_ids.
        """
        if name in self._energy_var_ids:
            return self._energy_var_ids[name]
        # Look up by name
        try:
            v = indigo.variables[name]
            self._energy_var_ids[name] = v.id
            return v.id
        except KeyError:
            pass
        # Create it
        try:
            v = indigo.variable.create(name, value="", folder=folder_id)
            self._energy_var_ids[name] = v.id
            log(f"[Energy Vars] Created variable '{name}' (id={v.id})")
            return v.id
        except Exception as exc:
            log(f"[Energy Vars] Could not create '{name}': {exc}", level="WARNING")
            return 0

    def _write_energy_summary_variables(self):
        """
        Write today's running energy totals and battery decision to Indigo variables
        in the Sigenergy folder. Called every 30 min and at midnight.
        Variables created automatically if they do not exist.
        """
        try:
            folder_id = self._sigenergy_folder_id()
            pv     = round(self.store.get("pv_daily_kwh",          0.0), 2)
            imp    = round(self.store.get("grid_import_daily_kwh",  0.0), 2)
            exp    = round(self.store.get("grid_export_daily_kwh",  0.0), 2)
            home   = round(self.store.get("home_daily_kwh",         0.0), 2)
            peak   = round(self.store.get("peak_soc",               0.0), 1)
            minsoc = round(self.store.get("min_soc",              100.0), 1)
            sself  = (round((1 - imp / home) * 100, 1)
                      if home > 0 else 0.0)

            decision = self.latest_decision
            action   = str(decision.action)  if decision else ""
            reason   = str(decision.reason)  if decision else ""

            updates = [
                ("sigen_today_pv_kwh",       str(pv)),
                ("sigen_today_import_kwh",   str(imp)),
                ("sigen_today_export_kwh",   str(exp)),
                ("sigen_today_home_kwh",     str(home)),
                ("sigen_today_self_suff_pct", str(sself)),
                ("sigen_today_peak_soc",     str(peak)),
                ("sigen_today_min_soc",      str(minsoc)),
                ("sigen_decision_action",    action),
                ("sigen_decision_reason",    reason),
            ]
            for var_name, value in updates:
                var_id = self._ensure_var(var_name, folder_id)
                if var_id:
                    try:
                        indigo.variable.updateValue(var_id, value)
                    except Exception as exc:
                        log(f"[Energy Vars] Update failed '{var_name}': {exc}",
                            level="WARNING")
        except Exception as exc:
            log(f"[Energy Vars] _write_energy_summary_variables failed: {exc}",
                level="WARNING")

    def _update_tariff_device(self, tariff_info, monitored):
        """Push Octopus tariff data to tariffMonitor device."""
        dev = self._find_device("tariffMonitor")
        if not dev:
            return

        active_key = tariff_info.get("tariff_key", TARIFF_TRACKER)
        tracker    = monitored.get("tracker",  {})
        go         = monitored.get("go",       {})
        flux       = monitored.get("flux",     {})
        flexible   = monitored.get("flexible", {})

        # rateToday: show the active tariff's actual unit rate
        if active_key == TARIFF_FLEXIBLE:
            active_rate_today    = str(flexible.get("today_p", ""))
            active_rate_tomorrow = ""                                 # flat rate — no tomorrow
        else:
            active_rate_today    = str(tracker.get("today_p", ""))
            active_rate_tomorrow = str(tracker.get("tomorrow_p") or "")

        states = [
            {"key": "tariffActive",        "value": tariff_info.get("display_name", "")},
            {"key": "rateToday",           "value": active_rate_today},
            {"key": "rateTomorrow",        "value": active_rate_tomorrow},
            {"key": "trackerRateToday",    "value": str(tracker.get("today_p", ""))},
            {"key": "trackerRateTomorrow", "value": str(tracker.get("tomorrow_p") or "")},
            {"key": "goOffPeakRate",       "value": str(go.get("cheap_p", ""))},
            {"key": "goStandardRate",      "value": str(go.get("standard_p", ""))},
            {"key": "goPeakRate",          "value": str(go.get("peak_p", ""))},
            {"key": "fluxOffPeakRate",     "value": str(flux.get("cheap_p", ""))},
            {"key": "fluxStandardRate",    "value": str(flux.get("standard_p", ""))},
            {"key": "fluxPeakRate",        "value": str(flux.get("peak_p", ""))},
            {"key": "flexibleRate",        "value": str(flexible.get("today_p", ""))},
            {"key": "lastUpdate",          "value": datetime.now().strftime("%H:%M:%S")},
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
            start_str    = _local_time(event["start_time"], "%H:%M %d/%m")
            end_str      = _local_time(event["end_time"])
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

    def actionRefreshForecast(self, action):
        """Action: Manual solar forecast refresh (Open-Meteo)."""
        log("[Action] Manual solar forecast refresh")
        self._refresh_forecast(force=True)
        self.store["last_forecast"] = time.time()

    def actionRefreshOctopus(self, action):
        """Action: Manual Octopus rates refresh."""
        log("[Action] Manual Octopus rates refresh")
        self._refresh_octopus_rates(force=True)
        self.store["last_octopus"] = time.time()

    # ================================================================
    # Indigo Menu Callbacks
    # ================================================================

    def menuRefreshAll(self):
        """Menu: Force refresh solar forecast + Octopus + re-evaluate manager."""
        log("[Menu] Refresh All: fetching solar forecast, Octopus and re-evaluating...")
        self._refresh_forecast(force=True)
        self.store["last_forecast"] = time.time()
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

        flexible = rates.get("flexible", {})

        log(f"[Tariff] Active tariff: {tariff_info.get('display_name', '?')} "
            f"({tariff_info.get('tariff_key', '?')})")

        # Flexible Octopus (flat rate — no TOU windows)
        if flexible.get("today_p") is not None:
            log(f"[Tariff] Flexible: {flexible['today_p']:.2f}p/kWh (flat rate, no cheap window)")

        # Tracker (shown if available, e.g. when user is on Tracker or monitoring it)
        today_p    = tracker.get("today_p")
        tomorrow_p = tracker.get("tomorrow_p")
        if today_p is not None:
            log(f"[Tariff] Tracker today: {today_p:.2f}p/kWh" +
                (f"  tomorrow: {tomorrow_p:.2f}p/kWh" if tomorrow_p is not None else
                 "  tomorrow: not yet published"))
        else:
            log("[Tariff] Tracker: not available (suspended or not active)")

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
            start_s = _local_time(start, "%H:%M %d/%m") if start else "?"
            end_s   = _local_time(end)                  if end   else "?"
            log(f"[VPP] Next event: {start_s} - {end_s} BST "
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

        # Solar forecast (Open-Meteo — all 4 arrays, no API key needed)
        self.forecast = OpenMeteoForecast(
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
            f"Solar=Open-Meteo (4 arrays), "
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
        self._save_home_profile()

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
