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
| 3.1 | 06-Apr-2026 | Energy summary variables (9 `sigen_today_*` vars written every 30 min and at midnight — PV, import, export, consumption, self-sufficiency, peak/min SOC, last decision). Storm watch: polls MeteoAlarm CAP feed every 2h; raises dawn SOC target to 50% (yellow) or 80% (amber/red) and suppresses export during active storm warnings covering Medomsley. Power cut lockout: off-grid→on-grid transition blocks export for 4h. Real-time solar overflow export: battery charged exactly to fill by dusk, all PV surplus above that exported immediately. Inverter IP now from plugin prefs. New: storm_watch.py v1.3. |
| 3.0.2 | 01-Apr-2026 | Fix: direct edits to .indiPref are overwritten by Indigo on shutdown. Adds startup() migration that raises dawnSocTarget to 15% minimum (5% buffer above 10% health floor), then lets Indigo persist the corrected value. Previously both were 10%, leaving no margin on poor solar nights. |
| 3.0.1 | 01-Apr-2026 | Solar overflow dawn margin gate: before any daytime surplus export, projected SOC at dawn must be at least 3.5 kWh above dawn target (SOLAR_OVERFLOW_MIN_DAWN_MARGIN). Prevents exporting kWh that earn 12p but cost 20p+ to reimport overnight when the bias-corrected forecast over-inflates remaining solar. |
| 3.0 | 01-Apr-2026 | Fix: 01-Apr-2026 battery-to-4.2%-SOC incident. Three-part fix: (1) emergency floor check on sunny-day path — even when tomorrow is sunny, import is triggered if raw SOC projection breaches the health floor; (2) discharge cutoff register (40048) verified in _verify_ems_registers() every 15 min and corrected to batteryHealthCutoff (was never written — factory default 5%, not 10%); (3) VPP cutoff flag prevents verify() from interfering with VPP floors. 55 unit tests. |
| 2.9 | 31-Mar-2026 | Fix: night export (mode 0x06) fired at 14:48 in full daylight because _check_night_export() computed _is_daytime=False when snapshot.dawn_times had no entry for today (Solcast data stale or not yet fetched). With _is_daytime=False the function fell through to export logic, suppressing PV to 0W and discharging battery to grid during daylight. Fixed in battery_manager.py v1.6: if today's dawn time is absent, fall back to clock-based safe window (07:00-21:00 local time) so export is always blocked during daylight regardless of Solcast data availability. |
| 2.8 | 31-Mar-2026 | Fix: inverter stuck in mode 0x06 (Discharge ESS First) after plugin restart. After restart all store flags are False, so _act_on_decision(ACTION_SELF_CONSUMPTION) only called set_self_consumption() on a True→False transition — never on cold start. Fixed in two places: (1) _verify_ems_registers() now reads actual EMS mode register via new sigenergy_modbus.read_ems_mode() and corrects any mismatch; (2) _act_on_decision() checks emsWorkMode field from live inverter data and forces 0x02 if inverter is in wrong mode despite all store flags being False. |
| 2.7 | 31-Mar-2026 | Plugin log file: daily rotating log written to plugin data dir (Preferences/Plugins/.../logs/YYYY-MM-DD.log). log() now writes to both Indigo event log and plugin file simultaneously. 14-day retention with automatic purge. File opened/rotated in startup() and on each 10-second tick (no-op unless date has rolled over). Closed cleanly in shutdown(). Enables post-mortem diagnosis without relying solely on Indigo's event log. |
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

- Indigo 2025.1 or later
- Sigenergy inverter with Modbus TCP enabled (port 502)
- Python package: `pymodbus` (installed via Indigo's package manager)
- Optional: Solcast API key (solar forecast)
- Optional: Octopus Energy API key (tariff-aware import scheduling)
- Optional: Axle VPP account credentials

---

## Installation

1. Go to the [Releases](https://github.com/Highsteads/SigenEnergyManager/releases) page
   and download `SigenEnergyManager.indigoPlugin.zip`
2. Unzip the downloaded file -- you will get `SigenEnergyManager.indigoPlugin`
3. Double-click `SigenEnergyManager.indigoPlugin` -- Indigo will install it automatically

---

## Configuration

### Credentials

**If you already have a `secrets.py`** in
`/Library/Application Support/Perceptive Automation/` add the keys below to it.
The plugin will pick them up automatically at startup.

**If you do not have a `secrets.py`** you can either:
- Copy `secrets_example.py` (included in the plugin bundle) to
  `/Library/Application Support/Perceptive Automation/`, rename it to `secrets.py`,
  and fill in your values, **or**
- Enter your credentials directly in the plugin's configuration dialog
  (Indigo menu → Plugins → Sigenergy Energy Manager → Configure)

All credential fields fall back to the plugin configuration dialog if
`secrets.py` is absent or a key is missing.

```python
OCTOPUS_API_KEY   = "your-octopus-api-key-here"
OCTOPUS_ACCOUNT   = "A-XXXXXXXX"
SOLCAST_SITE_1_ID = "xxxx-xxxx-xxxx-xxxx"
SOLCAST_SITE_2_ID = "xxxx-xxxx-xxxx-xxxx"
SOLCAST_API_KEY   = "..."
AXLE_API_KEY      = ""
AXLE_CLIENT_ID    = ""
```

### Plugin preferences

| Setting | Description |
|---------|-------------|
| Inverter IP | Sigenergy inverter LAN address (default 192.168.100.49) |
| Modbus port | Inverter Modbus TCP port (default 502) |
| Plant slave address | Modbus slave address for plant data (default 247) |
| Inverter slave address | Modbus slave address for inverter data (default 1) |
| Poll interval | Inverter data poll frequency in seconds (default 60) |
| Battery capacity (kWh) | Total usable battery capacity (default 35.04) |
| Battery efficiency | Round-trip efficiency 0-100% (default 94) |
| Inverter max kW | Inverter rated output power -- sets battery discharge ceiling (default 10) |
| Dawn SOC target (%) | Minimum SOC required at next solar dawn (default 10%) |
| Battery health cutoff (%) | Hardware discharge floor (default 10%) |
| Export enabled | Enable grid export (requires active export MPAN) |
| Max export kW | DNO export cap — used at startup to initialise the export limit register (default 4 kW) |
| VPP (Axle) enabled | Enable Axle Virtual Power Plant integration |

Note: Octopus tariff type (Tracker/Go/Flux/iGo/iFlux/Agile) is detected
automatically from your Octopus account -- no manual selection required.

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
3. **Every 15 minutes** -- `_verify_ems_registers()` reads back both registers and
   the EMS mode register; any drift is corrected and logged with a warning

### Tariff-aware import scheduling

| Tariff | Import strategy |
|--------|----------------|
| Tracker | Import now, or defer to midnight if tomorrow is 10%+ cheaper |
| Go / iGo | Defer to cheap window (00:30-05:30) if battery can reach it |
| Flux / iFlux | Defer to cheap window (02:00-05:00) |
| Agile | Find cheapest 30-min slot before dawn |

### VPP (Axle) integration

Full 5-state machine: IDLE -> SCHEDULED -> PRE_CHARGING -> ACTIVE -> COOLING_OFF.
The plugin pre-charges the battery to cover the event export plus the configured dawn
reserve. After the event, the discharge cutoff register is restored to the health floor.

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
| Solcast Forecast | Today/tomorrow solar forecast (bias-corrected P50) |
| Octopus Tariff | Current unit rate, standing charge, tomorrow's rate |
| Axle VPP | VPP event state machine and SOC management |

---

## Unit tests

```bash
cd SigenEnergyManager.indigoPlugin/Contents/Server\ Plugin
python3 -m unittest test_battery_manager test_sigenergy_modbus -v
```

**53 tests** across two test files, all passing without Indigo installed:

| File | Tests | Coverage |
|------|-------|---------|
| `test_battery_manager.py` | 32 | Dawn viability, import scheduling (Tracker/Go/Flux/Agile), staged export hysteresis, night export (9 cases), VPP suppression |
| `test_sigenergy_modbus.py` | 21 | `set_self_consumption()` register resets, force_discharge/force_charge sequences, night_export sequence, read_discharge_limit/read_charge_limit, export limit validation |

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

CliveS & Claude Sonnet 4.6 -- Medomsley, County Durham, England
