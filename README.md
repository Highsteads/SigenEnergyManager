# SigenEnergyManager

**Indigo home automation plugin for Sigenergy solar/battery systems.**

Self-sufficiency-first battery management: never import from grid unless the battery
cannot reach the next solar generation window at the configured minimum SOC. Exports
surplus to grid to prevent the battery from hitting 100% and curtailing PV generation.
At night, exports battery surplus to grid when the battery has more energy than needed
to reach dawn, provided tomorrow's solar forecast is good enough to recharge it.

---

## Version history

| Version | Date | Notes |
|---------|------|-------|
| 5.18.2 | 14-May-2026 | **VPP event post-mortem.** At VPP_ACTIVE → COOLING_OFF the plugin parses the per-event JSONL file just closed and writes nine new summary states to the `axleVppMonitor` device (`lastVppDate`, `lastVppExportKwh`, `lastVppPvKwh`, `lastVppMinPvW`, `lastVppMaxBatteryDischargeW`, `lastVppPeakGridExportW`, `lastVppPvSurvived`, `lastVppEmsModes`, `lastVppLogPath`). Pushover at event end carries the headline numbers AND a pre-formed *Ask Claude* block — JSONL path + four pointed questions ready to paste into Claude Code for analysis. One concise summary line goes to the Indigo Event Log; per-minute snapshots remain JSONL-only. Best-effort: summary failure WARNs but never blocks cool-off. |
| 5.18.1 | 14-May-2026 | **Quiet VPP event log.** Per-minute VPP snapshots moved out of the Indigo Event Log and into a per-event JSONL file at `<data_dir>/vpp_events/<YYYY-MM-DD_HHMM>.jsonl`. Each file: one `announcement` record (every field Axle's API returned), one `snapshot` record per minute (SOC, PV/battery/home/grid W, EMS mode + register, charge/discharge limits, plant state), one `event_ended` record (final export kWh). Event Log keeps only the key markers: announced / T-10min / RELEASED / WINDOW ACTIVE / event ended / REGAINED. |
| 5.18 | 14-May-2026 | **TRUE Axle handoff via Remote EMS release.** v5.16+v5.17 were stop-gap measures that had the plugin drive the export through Modbus mode selection (0x06 or 0x02+charge_limit=0) — both held Remote EMS enabled, blocking Axle's cloud channel and forcing us to pick among simple modes that can't do what Axle's cloud can (e.g. simultaneous battery discharge + PV charge). v5.18 properly releases Remote EMS at T-5min via `modbus.disable_remote_ems()`. With Remote EMS off the inverter follows Sigenergy's cloud commands directly — Axle now controls the inverter the way other Axle+Sigenergy users see, including keeping PV running through battery export. Pre-export step (T-4min mode 0x06) removed. Minute-by-minute countdown spam replaced by a single T-10min warning. New `>>> RELEASED CONTROL TO AXLE <<<` and `>>> REGAINED CONTROL <<<` markers for greppability. `_vpp_check_axle_release()` watches `emsWorkMode` for the "Self" string at event end and re-enables Remote EMS the moment Axle hands back. Verify loop skips writes during VPP_ACTIVE/COOLING_OFF (observe-only). |
| 5.2 | 12-May-2026 | Three small additions. **Web dashboard charts** via Chart.js (CDN): 24h/48h/7d SOC line + stacked energy bars (PV / export / import / home) and a 30-day daily totals chart. Backed by new `/api/history?hours=N` (half-hourly slots from SQLite) and `/api/daily?days=N` (from `daily_history.json`) endpoints. **Weekly data-dir backup** — every Monday midnight, tar.gz of `accumulators.json`, `daily_history.json`, `soh_history.json`, `home_load_profile.json`, `forecast_accuracy.json`, `energy_timeseries.db` and the openmeteo combined cache to `data_dir/data_backup/`. Retains 8 most recent (~2 months). **Auto-update notifier** — checks GitHub releases on startup (daily-cached in `pluginPrefs.lastUpdateCheck`), logs an INFO line if a newer plugin version is available. Silent on network failure. Also fixed a double-count bug in **Show Today's Energy Summary** and **Show Manager Status**: both used `correctedTodayKwh` (whole-day forecast) labelled as "kWh remaining" — they now use `remainingTodayKwh` (now → dusk). |
| 5.1 | 12-May-2026 | Site config consolidation. Plugin now publishes `sigen_site_config.json` to the Python Scripts folder on every startup and every PluginConfig save. The companion `openmeteo_battery_optimiser.py` script (v2.7) reads it so battery / inverter / tariff / resilience / flood-prevention values always match the plugin. Fixes a real drift bug: optimiser had `FLOOD_PREV_FORECAST_MULT = 4.0` while plugin used `3.0` — could give "no pre-drain" advisory while the plugin actually pre-drained. New `siteArraysJson` PluginConfig field — per-array PV specs as a JSON list (`{name, tilt, azimuth, kwp, shade}`), strict-shape parsed at startup with ERROR log on bad JSON (falls back to built-in default 4-array config). |
| 5.0 | 12-May-2026 | Major hardening + feature pass. Threading lock around shared state; web dashboard thread/socket cleanup on shutdown; SQLite connections timeout=5.0 + try/finally; Octopus rate-limit tracker (warn >80/hr, hard-stop >95/hr); Modbus writes are read-back-verified; JSON parse guards on every `response.json()`; Kraken token cleared on any failure path; `battery_manager.evaluate()` refactored (`_check_overrides` / `_check_resilience_buffer`) and the dead v4.0 night-export branch removed; site coordinates moved to PluginConfig (`siteLatitude`/`siteLongitude`); variable folder ID cached; `_ensure_plugin_log` throttled to hourly; `ServerApiVersion` bumped 3.0 → 3.8 (Indigo 2025.2 native); pymodbus pinned `>=3.0,<4.0`; EMS mode 0x07 ("AI Mode") added; Axle `forecast_dispatch_kwh` / `estimated_revenue_p` surfaced when present; auto-calibrated weekday/weekend kWh from live consumption profile; new menu items **Run Self-Test** and **Show Power Cut Log**; dashboard adds `tomorrow_surplus_kwh`, `tomorrow_revenue_gbp`, `forecast_accuracy`, `power_cut`; weekly battery State-of-Health snapshot with degradation warnings; power-cut event log; variable-driven pause/resume via `sigen_manager_paused`; Pushover quiet hours + configurable sound; 7-day rolling forecast MAPE summary at midnight. Companion `openmeteo_battery_optimiser.py` script rewrote 20:00 / 01:45 Pushover messages in full prose. `getActionConfigUiValues` now pre-populates Force-Import / Force-Export dialogs with live values. |
| 4.9 | 10-May-2026 | Plugin version is now read dynamically from Info.plist (`self.pluginVersion`) — single source of truth, no separate Python constant. Pushover action calls fixed: action ID `send` (was `sendPushover`), correct prop keys `msgTitle`/`msgBody`/`msgUser`/`msgPriority`/`msgSound`. Implemented `triggerStartProcessing` / `triggerStopProcessing` lifecycle so all custom Indigo trigger events (`emergencyImportTriggered`, `exportStarted/Stopped`, `vppAnnounced/Started/Ended`, `floodPreventionStarted/Stopped`, `powerCutLockoutStarted/Cleared`) now fire correctly via `indigo.trigger.execute(trigger_object)`. Moved hardcoded IPs and the Axle support email to `IndigoSecrets.py`: `SIGENERGY_IP`, `DASHBOARD_HOST`, `AXLE_SUPPORT_EMAIL`, `PUSHOVER_USER_TOKEN`. Each has a PluginConfig fallback and logs an ERROR if neither source is set. Dashboard URL auto-detects the LAN IP via socket when no override is configured. Swallowed-failure log levels promoted from WARNING to ERROR (trigger execute fail, dashboard stop, VPP email, Timeseries DB init/write). `_init_modules()` now initialises forecast/Octopus/Axle before the inverter-IP check so a missing IP only skips Modbus instead of crashing startup. |
| 4.8 | 03-May-2026 | Remove 15-minute heartbeat log — only log on action change (web dashboard covers live status). |
| 4.7 | 02-May-2026 | Remove Solcast code/variables (Open-Meteo only); raise FLOOD_PREV_FORECAST_MULT 2.0 → 3.0; delete test_overnight.py. |
| 4.6 | 01-May-2026 | Half-hourly SQLite energy logging for TariffAnalyser feed. |
| 4.5 | 30-Apr-2026 | Rename to SigenEnergyManager + critical bug fixes and polish. |
| 2.6 | 31-Mar-2026 | Solar overflow SOC gate: export now only starts once battery SOC reaches 40%, preventing the algorithm from exporting aggressively while the battery is still low after overnight discharge. Solcast Indigo variables (solcast_today_kwh, solcast_tomorrow_kwh, solcast_last_updated) now populated on every Solcast refresh — were previously always 0.0. P10 forecast data removed from all modules: was dead code never used in any decision logic since v1.3; removes _hourly_p10_today and _hourly_p10_tomorrow from solcast.py, forecast_p10 from ManagerSnapshot, and _sum_tomorrow_forecast() static method from battery_manager.py. P90 also removed. |
| 2.5 | 30-Mar-2026 | Fix: v2.4 ineffective because dawn_target_pct and health_cutoff_pct are both 10% so changing threshold had no effect. Root cause: when tomorrow is sunny (forecast >= daily consumption) import is never needed regardless of dawn SOC — the inverter's discharge cutoff register (40048) already prevents the battery going below health_cutoff_pct. Import now fully suppressed on sunny days. Only on poor solar days (tomorrow forecast < daily consumption) does the dawn_target buffer apply. |
| 2.4 | 30-Mar-2026 | Fix: overnight grid import triggered unnecessarily when battery was low but tomorrow has good solar. Import threshold is now solar-aware: if tomorrow's bias-corrected Solcast P50 >= daily consumption, import only triggers if projected dawn SOC would hit the hardware cutoff floor (inverter stops discharging anyway). On poor solar days the full dawn_target buffer is maintained as before. Eliminates small unnecessary top-up imports on sunny days. |
| 2.3 | 30-Mar-2026 | Fix: dawn viability check incorrectly triggered grid import during daylight when battery SOC was low but abundant solar remained. _check_dawn_viability() now credits remaining bias-corrected Solcast P50 solar (net of home consumption to dusk) to current SOC before projecting overnight drain to next dawn. Import is only triggered if the battery genuinely cannot reach dawn target even after today's remaining solar is accounted for. |
| 2.2 | 30-Mar-2026 | Replaced fixed SOC threshold overflow logic with forecast-based dynamic export. Each evaluation: sums remaining bias-corrected Solcast P50 from now to dusk, subtracts expected home consumption and battery headroom to reach 100%, spreads any genuine surplus evenly across remaining daylight hours up to 4 kW DNO cap. Battery reaches 100% as near to dusk as solar allows while exporting continuously from first surplus detection. Mode stays 0x02 throughout; PV never suppressed. Adds house_load_watts and bias_factor to ManagerSnapshot. |
| 2.1 | 30-Mar-2026 | New: daytime solar overflow export. When SOC >= 80% during daylight, caps HOLD_ESS_MAX_CHARGE (register 40032) to 2 kW so PV surplus that can't enter the battery flows to grid instead. At SOC >= 90% caps to near-zero (200 W). Releases below 75% SOC (hysteresis). Mode stays 0x02 throughout — PV is never suppressed. Previous attempt used mode 0x06 (Discharge ESS First) which causes inverter to curtail PV. _verify_ems_registers() updated to respect the charge cap during overflow. |
| 2.0 | 29-Mar-2026 | Fix: "Cannot find Tracker product code" warning still firing after v1.8. Root cause: two bugs. (1) _detect_tariff_from_account() returned the FIRST meter point regardless of whether it was import or export — accounts with an export MPAN (OUTGOING tariff) had it returned first, classifying as TARIFF_UNKNOWN and bypassing the account path. Fixed: filter by self.mpan when set, only check matching meter point; skip TARIFF_UNKNOWN results. (2) _probe_product_by_prefix() used is_variable=True filter — Tracker (SILVER-*) is a daily-changing flat rate, not flagged is_variable by Octopus, so it was silently excluded. Fixed: removed is_variable filter. |
| 1.9 | 29-Mar-2026 | Fix: Night export still blocked after restart despite v1.7 disk-cache pre-warm. Root cause: _tick() ran manager (step 2) before Solcast refresh (step 3), so latest_forecast_data was still {} on the first evaluation. Two fixes: (1) startup() now calls _refresh_solcast() immediately after _init_modules() to pre-populate latest_forecast_data from the disk cache before any manager evaluation; (2) Solcast refresh moved before manager in _tick() as a permanent ordering guarantee. |
| 1.8 | 29-Mar-2026 | Fix: "Cannot find Tracker product code" warning fired every 30 minutes. Root cause: _probe_tracker_product_code() guessed TRACKER-VAR-YY-MM-DD dates at 30-day intervals, which never match real Octopus product release dates. Replaced with _probe_product_by_prefix(("SILVER","TRACKER")) -- the same public-products-listing approach used by Go/Flux/Agile. _probe_tracker_product_code() deleted. |
| 1.7 | 29-Mar-2026 | Fix: Solcast combined forecast now pre-warmed from disk cache on startup. Previously every plugin restart cleared the in-memory forecast, causing correctedTomorrowKwh=0.0 and silently blocking night export until the next live API fetch (up to 2.4h). _load_combined_cache() reads solcast_combined_cache.json at __init__ time; logs a warning if the cache is >7.2h old. _refresh_solcast() now logs an explicit WARNING if forecastStatus contains 'No data' or correctedTomorrowKwh==0.0, so the blockage is visible in the event log. |
| 1.6 | 29-Mar-2026 | Fix: Intelligent Go cheap window corrected to 23:30-05:30 (was 00:30, same as standard Go). Fix: Intelligent Flux has no narrow cheap window -- entire 21h outside peak (16:00-19:00) is cheap, so window now modelled as 19:00-16:00 wrap-around instead of incorrectly using Flux's 02:00-05:00. Battery manager will now import immediately at any non-peak time on iFlux rather than waiting for a 02:00 window that doesn't exist. |
| 1.5 | 29-Mar-2026 | VPP: discharge cutoff register (40048) raised at VPP_ANNOUNCED rather than PRE_CHARGING. Floor = dawn target + full event export energy, so the battery reserve is protected from the moment an Axle event is announced. Cutoff restored on cancellation (event disappears while ANNOUNCED/PRE_CHARGING) as well as on COOLING_OFF completion. |
| 1.4 | 29-Mar-2026 | Fix: night export stop condition replaced -- PV watts reads 0W in Discharge ESS First mode so solar could never trigger a stop. Export now stops at Solcast-predicted sunrise (dawn_times) instead. Fix: night_export() sets HOLD_ESS_MAX_DISCHARGE=10000W and relies on the inverter's own DNO cap for grid limiting -- battery now supplies house load + 4kW to grid simultaneously. 54 unit tests, all pass. |
| 1.3 | 29-Mar-2026 | Night export feature: force-discharge surplus to grid at night when SOC is high and tomorrow's forecast is good. Fix: persistent Modbus register (HOLD_ESS_MAX_DISCHARGE / HOLD_ESS_MAX_CHARGE) left at reduced value after force-discharge, capping battery output in self-consumption mode. Fix: tomorrow viability check now uses bias-corrected P50 (correctedTomorrowKwh x 60%) instead of P10. New: test_sigenergy_modbus.py (16 Modbus register tests). 49 unit tests total, all pass. |
| 1.2 | 27-Mar-2026 | Fix: inverter capacity corrected to 10 kW. |
| 1.1 | 27-Mar-2026 | Fix: nighttime grid import caused by export limit register set to 0W when export stops. Fix: symmetric hysteresis on export restart (10% deadband). 48 unit tests all pass. |
| 1.0 | 26-Mar-2026 | Initial release. Replaces SigenergySolar v3.1. |

---

## Requirements

- Indigo 2025.2 or later (Python 3.13)
- Sigenergy inverter with Modbus TCP enabled (port 502)
- Python packages: `pymodbus>=3.0`, `pytz>=2024.1` (auto-installed from `requirements.txt`)
- Optional: Octopus Energy API key (tariff-aware import scheduling)
- Optional: Axle VPP account credentials
- Optional: Pushover Indigo plugin (storm + VPP alerts)
- Solar forecast uses Open-Meteo — no API key required

---

## Installation

1. Go to the [Releases](https://github.com/Highsteads/SigenEnergyManager/releases) page
   and download `SigenEnergyManager.indigoPlugin.zip`
2. Unzip the downloaded file -- you will get `SigenEnergyManager.indigoPlugin`
3. Double-click `SigenEnergyManager.indigoPlugin` -- Indigo will install it automatically

---

## Configuration

### Credentials — `IndigoSecrets.py` vs `IndigoSecrets_example.py`

There are **two** files involved, and only one of them holds your real values:

| File | Purpose | Contains real data? | Committed to GitHub? |
|------|---------|---------------------|----------------------|
| `IndigoSecrets.py` | The **working file** the plugin reads at runtime. Lives at `/Library/Application Support/Perceptive Automation/IndigoSecrets.py`. Keep a backup in a password manager. | YES | **NO** — listed in `.gitignore`, never pushed. |
| `IndigoSecrets_example.py` | **Template only** — empty placeholders so users know which keys to set. Shipped inside the plugin bundle. | NO | YES — public on GitHub. |

**Setup:**

1. If you already have `IndigoSecrets.py`, just add the keys below to it.
2. If you do not, copy `IndigoSecrets_example.py` (inside the plugin bundle) to
   `/Library/Application Support/Perceptive Automation/`, rename it to `IndigoSecrets.py`,
   and fill in your values.
3. Or skip `IndigoSecrets.py` entirely and enter values via the plugin's configuration
   dialog (Indigo menu → Plugins → Sigenergy Manager → Configure). `IndigoSecrets.py`
   wins over the dialog when both are set.

If neither source provides a value the plugin logs an ERROR for the missing item
and skips just that feature (e.g. no inverter IP → Modbus skipped, but Octopus
and Open-Meteo still run).

**Keys read by SigenEnergyManager:**

```python
# Octopus Energy (tariff data)
OCTOPUS_API_KEY     = "sk_live_..."
OCTOPUS_ACCOUNT     = "A-XXXXXXXX"
OCTOPUS_MPAN        = "1300000000000"
OCTOPUS_SERIAL      = "00X0000000"

# Sigenergy inverter (Modbus)
SIGENERGY_IP        = "192.168.x.x"

# Axle VPP (optional)
AXLE_API_KEY        = ""
AXLE_SUPPORT_EMAIL  = ""   # recipient for VPP "inverter not released" escalation

# Pushover notifications (optional — Pushover Indigo plugin must also be installed)
PUSHOVER_USER_TOKEN = ""

# Web dashboard (optional — blank = auto-detect LAN IP via socket)
DASHBOARD_HOST      = ""
```

### Plugin preferences

| Setting | Description |
|---------|-------------|
| Inverter IP | Sigenergy inverter LAN address (default 192.168.100.49) |
| Modbus port | Inverter Modbus TCP port (default 502) |
| Plant slave address | Modbus slave address for plant data (default 247) |
| Inverter slave address | Modbus slave address for inverter data (default 1) |
| Poll interval | Inverter data poll frequency in seconds (default 60) |
| **Site latitude** *(v5.0)* | Degrees N for Open-Meteo forecast (default 54.882) |
| **Site longitude** *(v5.0)* | Degrees E, negative for W (default -1.818) |
| **PV array specs (JSON)** *(v5.1)* | Optional JSON list of `{name, tilt, azimuth, kwp, shade}` — leave blank to use the built-in 4-array default. Bad JSON falls back to the default and logs an ERROR. |
| Battery capacity (kWh) | Total usable battery capacity (default 35.04) |
| Battery efficiency | Round-trip efficiency 0-100% (default 94) |
| Inverter max kW | Inverter rated output power -- sets battery discharge ceiling (default 10) |
| Dawn SOC target (%) | Summer resilience buffer — minimum SOC kept overnight on flat-rate tariffs (default 10%) |
| Winter buffer (%) *(v4.x)* | Higher overnight floor for Oct-Mar (default 20%) |
| Battery health cutoff (%) | Hardware discharge floor (default 1%) |
| Weekday kWh / Weekend kWh | Daily consumption estimate (auto-calibrated from live inverter data in v5.0+; user values only used until the 48-slot profile is populated) |
| Export enabled | Enable grid export (requires active export MPAN) |
| Max export kW | DNO export cap — used at startup to initialise the export limit register (default 4 kW) |
| VPP (Axle) enabled | Enable Axle Virtual Power Plant integration |
| **Pushover sound** *(v5.0)* | Sound for storm + VPP alerts (default `vibrate`; full Pushover sound list available) |
| **Pushover quiet hours** *(v5.0)* | Start / End times (HH:MM). INFO-priority alerts are suppressed during this window; HIGH-priority (storm amber/red, VPP release failure) always fire. |
| Dashboard host | Blank = auto-detect LAN IP; otherwise hostname / IP visible to other devices |
| Show debug logging | Verbose log output |

Note: Octopus tariff type (Tracker/Go/Flux/iGo/iFlux/Agile) is detected
automatically from your Octopus account -- no manual selection required.

### Shared site config — `sigen_site_config.json` *(v5.1)*

The plugin publishes a small JSON file every startup and every PluginConfig save:

```
/Library/Application Support/Perceptive Automation/Python Scripts/sigen_site_config.json
```

It contains the battery / inverter / tariff / resilience / flood-prevention values
the plugin is currently using. The companion `openmeteo_battery_optimiser.py`
script (used by the 20:00 EVENING and 01:45 OVERNIGHT scheduled Pushover messages)
reads this file at runtime instead of hardcoding its own copies — so the plugin
and the advisory script can no longer drift apart on tunable thresholds.

If the file is absent (fresh install, or the script is being used without the
plugin) the script falls back to a built-in copy of the same defaults. Bad JSON
is treated the same as "absent".

### Companion script — `openmeteo_battery_optimiser.py`

Lives at:

```
/Library/Application Support/Perceptive Automation/Python Scripts/openmeteo_battery_optimiser.py
```

Driven by two Indigo schedules — `openmeteo_battery_optimiser At 20:00` and
`openmeteo_battery_optimiser At 01:45`. Sends a friendly full-prose Pushover
message describing tonight's plan (EVENING) and a short overnight check-in
(OVERNIGHT). Advisory only — all actual battery control is in the plugin.

Reads:
- `openmeteo_forecast.json` (published by the plugin's forecast module)
- `octopus_consumption_profile.json` (published by the plugin's Octopus module)
- `sigen_site_config.json` (published by the plugin, v5.1+)

---

## Core logic

### Self-sufficiency first

Every 60 seconds the plugin:

1. Reads live data from the inverter via Modbus TCP
2. Projects battery SOC at the next dawn using the Solcast P50 forecast dawn time
   and a 48-slot half-hourly consumption profile
3. If projected SOC at dawn < dawn target: schedules or starts a grid import
4. During daylight, once SOC >= 40%: caps HOLD_ESS_MAX_CHARGE so PV surplus exports
   to grid continuously, reaching 100% SOC as near to dusk as solar allows (see below)
5. If it is night and battery has surplus above the dawn floor: force-discharges to grid,
   provided tomorrow's solar forecast is good enough to recharge (see below)
6. Otherwise: holds in Max Self Consumption mode (Remote EMS 0x02)

### Night export

When there is no solar generation (PV < 500W), the plugin can export battery surplus
directly to grid at the configured max export rate (typically 4 kW). Three conditions
must all be true:

| Condition | Detail |
|-----------|--------|
| **Night** | Current time is outside the daytime window (before today's Solcast dawn, or more than 14h after it) |
| **Surplus** | Projected SOC at dawn > dawn target + 1 kWh safety buffer |
| **Tomorrow viable** | `correctedTomorrowKwh x 0.6 >= daily_consumption_kWh` |

The tomorrow viability check uses Solcast's **bias-corrected P50** estimate
(`correctedTomorrowKwh`) at 60% confidence -- meaning "even if tomorrow comes in
40% below our best estimate, the battery will still be recharged". This is far less
conservative than P10 (10th percentile), which would block export even on nights
before clearly sunny days.

**Why PV watts is not used as the night/day indicator:** In Discharge ESS First mode
(0x06) the Sigenergy inverter suppresses PV generation to 0W, so `pvPowerWatts`
reads zero regardless of actual solar. A PV threshold check would never fire while
exporting. Sunrise is instead detected from the Solcast-predicted `dawn_times`.

**Daytime window:** Export is blocked for 14 hours after today's dawn time (e.g.
dawn 07:00 -> blocked until 21:00, then nighttime resumes and export can start again).

Night export stops automatically when:
- Today's Solcast dawn time is reached (sunrise)
- Battery surplus drops below the minimum threshold
- Tomorrow's forecast deteriorates below the viability check

Example log message:

```
[Manager] Night export: 15.3 kWh surplus above dawn floor.
          Tomorrow forecast 25.1 kWh (60% = 15.1) >= daily 14.4 kWh.
          Exporting 4.0 kW
```

### Persistent Modbus register protection

The Sigenergy inverter retains certain registers across mode changes. Specifically,
`HOLD_ESS_MAX_DISCHARGE` (40034) and `HOLD_ESS_MAX_CHARGE` (40032) set during a
`force_discharge` or `force_charge` call persist as power caps even after returning
to Self Consumption mode.

The plugin guards against register drift at three layers:

1. **On every mode change** -- `set_self_consumption()` always resets both registers to
   10000W (inverter maximum) before setting the mode
2. **On startup** -- both registers are explicitly written to 10000W before any other
   Modbus operation
3. **Every 15 minutes** -- `_verify_ems_registers()` reads back both registers; if
   either has drifted it is corrected and logged with a warning

### Tariff-aware import scheduling

| Tariff | Import strategy |
|--------|----------------|
| Tracker | Import now, or defer to midnight if tomorrow is 10%+ cheaper |
| Go / iGo | Defer to cheap window (00:30-05:30) if battery can reach it |
| Flux / iFlux | Defer to cheap window (02:00-05:00) |
| Agile | Find cheapest 30-min slot before dawn |

### VPP (Axle) integration

Full 5-state machine: `IDLE` → `ANNOUNCED` → `PRE_CHARGING` → `ACTIVE` → `COOLING_OFF`.

**Pre-event (v5.18+ behaviour):**
- On announcement, the plugin raises the discharge cutoff register (40048) to a floor of
  `dawn_target + full event export energy` so the battery reserve is protected from
  the moment Axle dispatches the event.
- The plugin pre-charges the battery to cover the event export plus the configured
  dawn reserve.
- A single `T-10min` warning is logged. Minute-by-minute countdown spam is gone.

**Handoff to Axle (v5.18 — the right model):**
- **T-5min:** the plugin calls `modbus.disable_remote_ems()` to release Remote EMS.
  Axle controls the inverter via Sigenergy's cloud channel; the plugin is observe-only
  through the window. This is what enables Axle's "PV stays alive while battery
  exports" trick — something none of the plugin's single-Modbus-mode paths could do.
- A `>>> RELEASED CONTROL TO AXLE <<<` marker is written to the Event Log.

**During the event (v5.18.1+ data capture, v5.18.2+ post-mortem):**
- Per-minute snapshots are appended to a per-event JSONL file at
  `<data_dir>/vpp_events/<YYYY-MM-DD_HHMM>.jsonl`. Each file contains one
  `announcement` record (every field Axle's API returned), one `snapshot` per
  minute (SOC, PV/battery/home/grid W, EMS mode + register, charge/discharge
  limits, plant state) and one `event_ended` record at close.
- The Indigo Event Log stays clean — only the key state markers appear.
- The verify loop skips Modbus writes during `VPP_ACTIVE`/`VPP_COOLING_OFF` so the
  plugin can never fight Axle's commands.

**Event end (v5.18.2):**
- The plugin parses the JSONL file it just closed and writes nine summary states
  to the `axleVppMonitor` device: `lastVppDate`, `lastVppExportKwh`, `lastVppPvKwh`,
  `lastVppMinPvW`, `lastVppMaxBatteryDischargeW`, `lastVppPeakGridExportW`,
  `lastVppPvSurvived` (true if PV never collapsed below 100 W), `lastVppEmsModes`
  (set of EMS strings Axle used), `lastVppLogPath`.
- A single concise summary line is logged to the Indigo Event Log.
- A Pushover is sent carrying the headline numbers AND a pre-formed *Ask Claude*
  block — JSONL path plus four pointed questions (Did Axle keep PV running through
  battery export, and how? What EMS mode + register values did Axle use? Can the
  plugin replicate this via Modbus? Recommended changes to `_vpp_transition` /
  `_verify_ems_registers`). Paste straight into Claude Code for analysis.

**Cool-off:**
- `_vpp_check_axle_release()` watches `emsWorkMode` for the `"Self"` string
  (Axle always reverts to Max Self Consumption when it finishes). The moment we
  see it, Remote EMS is re-enabled and a `>>> REGAINED CONTROL <<<` marker is
  logged. Alert thresholds: Pushover + email at 45 min, force re-enable at 60 min.
- The discharge cutoff register is restored to the health floor.

---

## Bug fixes (v1.1 - v1.4)

### v1.4 -- Night export stop condition permanently blind (critical)

**Symptom:** Night export ran through sunrise and into the morning without stopping.

**Root cause:** The stop condition checked `pvPowerWatts >= 500W`. In Discharge ESS
First mode (0x06) the inverter suppresses PV output to 0W regardless of actual solar
generation, so this condition could never fire while export was active.

**Fix:** PV watts check replaced with a dawn_times check. The Solcast-predicted
sunrise time for today is stored in `snapshot.dawn_times`. Export is stopped (and
blocked from starting) for 14 hours after today's dawn time. At dawn + 14h
(typically 21:00) the nighttime window reopens and export can start again.

### v1.4 -- Night export limited to 4kW total instead of 4kW to grid

**Symptom:** With house consuming 0.9kW and export set to 4kW, only ~3.1kW reached
the grid. Battery was discharging at exactly 4kW total.

**Root cause:** `force_discharge(4000)` wrote `HOLD_ESS_MAX_DISCHARGE = 4000W`,
capping total battery output. House load consumed ~0.9kW of that, leaving 3.1kW
for the grid.

**Fix:** New `night_export(inverter_max_w)` method sets `HOLD_ESS_MAX_DISCHARGE =
10000W` (inverter maximum) and relies on the inverter's own DNO export cap (set
during commissioning) to limit grid flow to 4kW. Battery now discharges at
`house_load + 4kW`, so the grid always receives the full 4kW regardless of
home consumption.

### v1.3 -- Persistent register caps battery output (critical)

**Symptom:** Battery limited to 1.8 kW output in Self Consumption mode; inverter
importing 1.3 kW from grid even with 78% SOC.

**Root cause:** `HOLD_ESS_MAX_DISCHARGE` (Modbus 40034) is a persistent register that
survives Remote EMS mode changes. A `force_discharge(2000)` call during staged export
testing left 2000W in this register. When the plugin returned to Self Consumption mode
it only changed the mode register, never resetting the discharge cap.

**Fix:** `set_self_consumption()` now always resets both `HOLD_ESS_MAX_DISCHARGE` and
`HOLD_ESS_MAX_CHARGE` to 10000W before engaging the mode. Startup performs the same
reset. A 15-minute verification loop detects and corrects any future drift.

### v1.3 -- Night export never triggered (tomorrow viability check)

**Symptom:** Night export never started despite adequate SOC and good solar forecast.

**Root cause:** The tomorrow viability check used `_hourly_p10_tomorrow` (hourly P10
data by date string), but the Solcast module only populated `_hourly_p10_today`. The
`_sum_tomorrow_forecast()` helper searched for tomorrow's date in today's dict, found
nothing, returned 0 kWh, and the check `0 < 14.4` blocked export every night.

**Fix:** The viability check was switched from P10 to the bias-corrected P50
(`correctedTomorrowKwh x 0.6`), which is far more appropriate for this decision
-- see Night export above. P10 has since been removed entirely from the codebase.

### v1.1 -- Nighttime grid import at high SOC

**Symptom:** Battery at 78% SOC importing 1.3 kW from grid at night while discharging
at only 1.8 kW (home load 3.1 kW).

**Root cause:** When export stops, the old code wrote `HOLD_GRID_MAX_EXPORT_LIMIT = 0W`
to Modbus register 40038. Sigenergy interprets 0W as a hard constraint meaning "never
let net power cross zero into export territory". To guarantee compliance, the inverter
deliberately targets a small positive grid import rather than fully covering the home
load from battery.

**Fix:** When stopping export at night (PV <= 500W), the export limit register is now
set to the DNO cap (4000W) rather than 0W.

### v1.1 -- Export cycling

**Symptom:** Export starting and stopping every 15 minutes in the evening as SOC
oscillated around 80%.

**Fix:** Restart from tier 0 now requires SOC >= stage1 + 5% (symmetric 10% deadband).

---

## Device types

| Type | Purpose |
|------|---------|
| Battery Manager | Main control device -- one per system |
| Inverter Monitor | Real-time PV, battery, grid, home power readings |
| Solcast Forecast | Today/tomorrow solar forecast (bias-corrected P50) — *note: v4.7 removed Solcast, this is now Open-Meteo backed* |
| Octopus Tariff | Current unit rate, standing charge, tomorrow's rate |
| Axle VPP | VPP event state machine, SOC management, and v5.18.2 post-event summary states (`lastVpp*`) |

---

## Menu items

Available from Indigo: **Plugins → Sigenergy Manager** menu.

| Item | What it does |
|------|--------------|
| Refresh All Data Now | Forces an immediate forecast + Octopus rates + manager re-evaluation |
| Show Manager Status | Prints current manager decision, snapshot and tariff data |
| Show Daily History (Last 7 Days) | Reads the 365-day ring buffer and prints the last 7 days |
| Show Current Tariff Rates | Prints today / tomorrow rates (import + export) |
| Show VPP Status | Axle VPP state machine summary |
| Show VPP Export Summary | Cumulative VPP earnings since plugin install |
| Show Today's Energy Summary | PV / Import / Export / Home / SOC peaks for today |
| **Run Self-Test** *(v5.0)* | Verifies Modbus / Octopus / Open-Meteo / Axle / Pushover / secrets resolution in one report |
| **Show Power Cut Log** *(v5.0)* | Last 20 grid-status transitions (rolling 100-entry log) |
| Toggle Debug Logging | Quick on/off without opening the prefs dialog |
| Show Plugin Info | Re-prints the startup banner (version, paths, API version) |

## Indigo variables created by the plugin

| Variable | Folder | Purpose |
|----------|--------|---------|
| `sigen_manager_paused` *(v5.0)* | Sigenergy | Set to `true` / `1` / `yes` / `on` from any Indigo automation to pause the battery manager; set back to `false` to resume. The manager device's `managerStatus` state mirrors this. |
| `battery_optimiser_status` | Sigenergy | Status line from the optimiser script (`EVENING 20:00 | Tracker | …`) |
| `battery_import_kw` / `battery_import_kwh` | Sigenergy | Plan from the optimiser script |
| various `sigen_*` / `elec_*` | Sigenergy | Energy summary values written every 30 min |

## Custom events

Custom Indigo triggers that fire from the plugin (use **New Trigger** → **Plugin Event**):

| Event ID | Fires when |
|----------|-----------|
| `emergencyImportTriggered` | Battery cannot reach dawn — grid import initiated |
| `exportStarted` / `exportStopped` | Battery export to grid starts / stops |
| `vppAnnounced` / `vppStarted` / `vppEnded` | Axle VPP event lifecycle |
| `floodPreventionStarted` / `floodPreventionStopped` | Overnight pre-drain export lifecycle |
| `powerCutLockoutStarted` / `powerCutLockoutCleared` | 4-hour export lockout after grid restoration |

## Web dashboard

Local HTTP dashboard runs on port **8179** by default. Visit
`http://<your-indigo-host>:8179/` for a live view, now with **Chart.js charts**
(v5.2): a 24h/48h/7d SOC line + stacked energy bars (PV / export / import / home)
and a 30-day daily totals bar chart. The JSON API has three endpoints:

| Endpoint | Returns |
|----------|---------|
| `/api/status` | Live snapshot (see table below) — updates every 30s |
| `/api/history?hours=N` *(v5.2)* | Half-hourly slots from SQLite for last N hours (max 168) — used by the SOC + stacked-bar charts |
| `/api/daily?days=N` *(v5.2)* | Per-day totals from `daily_history.json` for last N days (max 365) — used by the daily chart |

`/api/status` returns:

| Section | Contains |
|---------|----------|
| `battery` | SOC, power |
| `solar` | Today / tomorrow kWh, bias factor, remaining today, **tomorrow surplus kWh**, **tomorrow revenue £**, **export rate p** *(v5.0)* |
| `grid` | Power, status |
| `home` | Load watts |
| `decision` | Manager action + reason |
| `tariff` | Name, product code, today/tomorrow p |
| `today_summary` | PV, import, export, home, peak/min SOC, self-sufficiency % |
| `vpp` | State machine status |
| `storm` | Current MeteoAlarm level |
| `flags` | export_active, solar_overflow_active, import_active, modbus_connected |
| `hourly_forecast` | Per-hour kWh today |
| **`power_cut`** *(v5.0)* | Last 10 transitions, `ongoing`, `lockout_active` |
| **`forecast_accuracy`** *(v5.0)* | 7-day rolling MAPE, mean factor, over/under counts |

---

## Unit tests

```bash
cd SigenEnergyManager.indigoPlugin/Contents/Server\ Plugin
python3 -m unittest test_battery_manager test_sigenergy_modbus -v
```

**72 tests** across two test files, all passing without Indigo installed:

| File | Tests | Coverage |
|------|-------|---------|
| `test_battery_manager.py` | 51 | Dawn viability, import scheduling (Tracker/Go/Flux/Agile), flood prevention (multiple cases), legacy migration paths, VPP suppression, seasonal logic, tariff midnight handling |
| `test_sigenergy_modbus.py` | 21 | `set_self_consumption()` register resets, force_discharge/force_charge sequences, read_discharge_limit/read_charge_limit, export limit validation, write-back verification |

---

## Hardware reference

Developed and tested on:
- 14.25 kWp solar (30 panels, 4 arrays)
- Sigenergy 10 kW hybrid inverter
- 35.04 kWh battery (4 x 8.76 kWh SigenStor)
- DNO export cap: 4 kW
- Tariff: Octopus Tracker

---

## Author

CliveS & Claude (Sonnet 4.6 / Opus 4.7) -- Medomsley, County Durham, England
