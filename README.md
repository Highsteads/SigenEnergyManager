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

### Secrets (recommended)

Copy `secrets_example.py` to `/Library/Application Support/Perceptive Automation/secrets.py`
and fill in your credentials. The plugin reads from this file at startup; all fields fall
back to `PluginConfig.xml` if the file is absent.

```python
OCTOPUS_API_KEY      = "sk_live_..."
OCTOPUS_ACCOUNT_NUM  = "A-XXXXXXXX"
SOLCAST_SITE_1_ID    = "xxxx-xxxx-xxxx-xxxx"
SOLCAST_SITE_2_ID    = "xxxx-xxxx-xxxx-xxxx"
SOLCAST_API_KEY      = "..."
AXLE_USERNAME        = "..."
AXLE_PASSWORD        = "..."
```

### Plugin preferences

| Setting | Description |
|---------|-------------|
| Inverter IP | Sigenergy inverter LAN address |
| Plant slave address | Modbus slave address for plant data (default 247) |
| Inverter slave address | Modbus slave address for inverter data (default 1) |
| Battery capacity (kWh) | Total usable capacity (default 35.04) |
| Battery efficiency | Round-trip efficiency 0-100% (default 94) |
| Dawn SOC target (%) | Minimum SOC required at next solar dawn (default 10%) |
| Battery health cutoff (%) | Hardware discharge floor (default 10%) |
| Export enabled | Enable staged grid export when SOC is high |
| Export stage 1 SOC (%) | SOC threshold to start stage 1 export (default 80%) |
| Export stage 1 kW | Export power at stage 1 (default 2 kW) |
| Export stage 2 SOC (%) | SOC threshold to increase to stage 2 export (default 90%) |
| Export stage 2 kW | Export power at stage 2 / DNO cap (default 4 kW) |
| Max export kW | DNO export cap -- used for night export power (default 4 kW) |
| Tariff | Octopus tariff type (Tracker/Go/Flux/iGo/iFlux/Agile) |

---

## Core logic

### Self-sufficiency first

Every 60 seconds the plugin:

1. Reads live data from the inverter via Modbus TCP
2. Projects battery SOC at the next dawn using the P10 (conservative) Solcast forecast
   and a 48-slot half-hourly consumption profile
3. If projected SOC at dawn < dawn target: schedules or starts a grid import
4. If SOC is above the export stage 1 threshold: opens the export limit register to
   allow surplus solar to flow to grid (staged, 2 kW then 4 kW)
5. If it is night (PV < 500W) and battery has surplus above the dawn floor: force-discharges
   to grid, provided tomorrow's solar forecast is good enough to recharge (see below)
6. Otherwise: holds in Max Self Consumption mode (Remote EMS 0x02)

### Night export

When there is no solar generation (PV < 500W), the plugin can export battery surplus
directly to grid at the configured max export rate (typically 4 kW). Three conditions
must all be true:

| Condition | Detail |
|-----------|--------|
| **Night** | PV watts < 500W (safe to force-discharge -- no solar to suppress) |
| **Surplus** | Projected SOC at dawn > dawn target + 1 kWh safety buffer |
| **Tomorrow viable** | `correctedTomorrowKwh x 0.6 >= daily_consumption_kWh` |

The tomorrow viability check uses Solcast's **bias-corrected P50** estimate
(`correctedTomorrowKwh`) at 60% confidence -- meaning "even if tomorrow comes in
40% below our best estimate, the battery will still be recharged". This is far less
conservative than P10 (10th percentile), which would block export even on nights
before clearly sunny days.

Night export stops automatically when:
- Solar is detected at dawn (PV > 500W)
- Battery surplus drops below the minimum threshold
- Tomorrow's forecast deteriorates below the viability check

Example log message:

```
[Manager] Night export: 15.3 kWh surplus above dawn floor.
          Tomorrow forecast 25.1 kWh (60% = 15.1) >= daily 14.4 kWh.
          Exporting 4.0 kW
```

### Export staging (daytime)

Daytime export is controlled via `HOLD_GRID_MAX_EXPORT_LIMIT` (Modbus register 40038) in
Remote EMS Max Self Consumption mode. This allows surplus PV to export naturally without
curtailing generation.

**Export thresholds (with symmetric 10% hysteresis deadband):**

| State | Condition |
|-------|-----------|
| Stage 1 starts | SOC >= stage1 + 5% (default: 85%) |
| Stage 2 starts | SOC >= stage2 + 5% (default: 95%) |
| Stage 2 to Stage 1 | SOC < stage2 - 5% (default: 85%) |
| Stage 1 to off | SOC < stage1 - 5% (default: 75%) |

The 10% wide deadband prevents rapid on/off cycling when SOC oscillates around
a threshold, which would otherwise toggle the export limit register to 0W and
cause grid import at night.

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

Full 5-state machine: IDLE -> SCHEDULED -> PRE_CHARGING -> ACTIVE -> COOLING_OFF.
The plugin pre-charges the battery to cover the event export plus the configured dawn
reserve. After the event, the discharge cutoff register is restored to the health floor.

---

## Bug fixes (v1.1 - v1.3)

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

**Fix:** Solcast module now populates `_hourly_p10_tomorrow`. The viability check was
also switched from P10 to the bias-corrected P50 (`correctedTomorrowKwh x 0.6`), which
is far more appropriate for this decision -- see Night export above.

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
| Solcast Forecast | Today/tomorrow solar forecast (P10/P50) |
| Octopus Tariff | Current unit rate, standing charge, tomorrow's rate |
| Axle VPP | VPP event state machine and SOC management |

---

## Unit tests

```bash
cd SigenEnergyManager.indigoPlugin/Contents/Server\ Plugin
python3 -m unittest test_battery_manager test_sigenergy_modbus -v
```

**49 tests** across two test files, all passing without Indigo installed:

| File | Tests | Coverage |
|------|-------|---------|
| `test_battery_manager.py` | 33 | Dawn viability, import scheduling (Tracker/Go/Flux/Agile), staged export hysteresis, night export (10 cases), VPP suppression |
| `test_sigenergy_modbus.py` | 16 | `set_self_consumption()` register resets, force_discharge/force_charge sequences, read_discharge_limit/read_charge_limit, export limit validation |

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
