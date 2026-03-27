# SigenEnergyManager

**Indigo home automation plugin for Sigenergy solar/battery systems.**

Self-sufficiency-first battery management: never import from grid unless the battery
cannot reach the next solar generation window at the configured minimum SOC. Exports
surplus solar to prevent the battery from hitting 100% and curtailing PV generation.

---

## Version history

| Version | Date | Notes |
|---------|------|-------|
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
2. Unzip the downloaded file — you will get `SigenEnergyManager.indigoPlugin`
3. Double-click `SigenEnergyManager.indigoPlugin` — Indigo will install it automatically

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
| Battery efficiency | Round-trip efficiency 0–100% (default 94) |
| Dawn SOC target (%) | Minimum SOC required at next solar dawn (default 10%) |
| Battery health cutoff (%) | Hardware discharge floor (default 10%) |
| Export enabled | Enable staged grid export when SOC is high |
| Export stage 1 SOC (%) | SOC threshold to start stage 1 export (default 80%) |
| Export stage 1 kW | Export power at stage 1 (default 2 kW) |
| Export stage 2 SOC (%) | SOC threshold to increase to stage 2 export (default 90%) |
| Export stage 2 kW | Export power at stage 2 / DNO cap (default 4 kW) |
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
5. Otherwise: holds in Max Self Consumption mode (Remote EMS 0x02)

### Export staging

Export is controlled via `HOLD_GRID_MAX_EXPORT_LIMIT` (Modbus register 40038) in Remote
EMS Max Self Consumption mode. This allows surplus PV to export naturally without
curtailing generation.

**Export thresholds (with symmetric 10% hysteresis deadband):**

| State | Condition |
|-------|-----------|
| Stage 1 starts | SOC ≥ stage1 + 5% (default: 85%) |
| Stage 2 starts | SOC ≥ stage2 + 5% (default: 95%) |
| Stage 2 → Stage 1 | SOC < stage2 − 5% (default: 85%) |
| Stage 1 → off | SOC < stage1 − 5% (default: 75%) |

The 10% wide deadband prevents rapid on/off cycling when SOC oscillates around
a threshold, which would otherwise toggle the export limit register to 0W and
cause grid import at night (see v1.1 bug fix notes below).

### Tariff-aware import scheduling

| Tariff | Import strategy |
|--------|----------------|
| Tracker | Import now, or defer to midnight if tomorrow is 10%+ cheaper |
| Go / iGo | Defer to cheap window (00:30–05:30) if battery can reach it |
| Flux / iFlux | Defer to cheap window (02:00–05:00) |
| Agile | Find cheapest 30-min slot before dawn |

### VPP (Axle) integration

Full 5-state machine: IDLE → SCHEDULED → PRE_CHARGING → ACTIVE → COOLING_OFF.
The plugin pre-charges the battery to cover the event export plus the configured dawn
reserve. After the event, the discharge cutoff register is restored to the health floor.

---

## v1.1 bug fixes

### Nighttime grid import at high SOC

**Symptom:** Battery at 78% SOC importing 1.3 kW from grid at night while discharging
at only 1.8 kW (home load 3.1 kW).

**Root cause:** When export stops, the old code wrote `HOLD_GRID_MAX_EXPORT_LIMIT = 0W`
to Modbus register 40038. Sigenergy interprets 0W as a hard constraint meaning "never
let net power cross zero into export territory". To guarantee compliance, the inverter
deliberately targets a small positive grid import as a safety margin rather than fully
covering the home load from battery.

**Fix:** When stopping export at night (PV ≤ 500W), the export limit register is now
set to the DNO cap (4000W) rather than 0W. The self-consumption algorithm prevents
accidental battery-to-grid export at night anyway (there is no PV source driving export).
During solar generation hours the register is still set to 0W to prevent unintended export
when the SOC is below the export tier threshold.

### Export cycling causing repeated 0W writes

**Symptom:** Export starting and stopping every 15 minutes in the evening as SOC
oscillated around 80%, each stop triggering the 0W bug above.

**Root cause:** The export restart logic had no upward hysteresis — it restarted at
exactly the stage 1 threshold (80%) after stopping at 75%, creating a narrow band
that SOC noise could cross repeatedly.

**Fix:** Restart from tier 0 now requires SOC ≥ stage1 + EXPORT_HYSTERESIS_PCT (85%),
creating a symmetric 10% deadband: stop at 75%, restart at 85%.

---

## Device types

| Type | Purpose |
|------|---------|
| Battery Manager | Main control device — one per system |
| Inverter Monitor | Real-time PV, battery, grid, home power readings |
| Solcast Forecast | Today/tomorrow solar forecast (P10/P50) |
| Octopus Tariff | Current unit rate, standing charge, tomorrow's rate |
| Axle VPP | VPP event state machine and SOC management |

---

## Unit tests

```bash
cd SigenEnergyManager.indigoPlugin/Contents/Server\ Plugin
python3 test_battery_manager.py
```

48 tests covering dawn viability, import scheduling (Tracker/Go/Flux/Agile),
staged export hysteresis, VPP suppression, and the v1.1 bug fixes. All pass
without Indigo installed.

---

## Hardware reference

Developed and tested on:
- 11.4 kWp solar (30 panels, 4 arrays)
- Sigenergy 10 kW hybrid inverter
- 35.04 kWh battery (4 × 8.76 kWh SigenStor)
- DNO export cap: 4 kW
- Tariff: Octopus Tracker

---

## Author

CliveS & Claude Sonnet 4.6 — Medomsley, County Durham, England
