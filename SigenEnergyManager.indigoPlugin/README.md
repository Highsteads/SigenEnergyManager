# SigenEnergyManager v1.1

Self-sufficiency-first battery management plugin for Sigenergy solar/battery systems
running under Indigo 2025.1.

## Quick start

1. Install the plugin by double-clicking `SigenEnergyManager.indigoPlugin`
2. Copy `secrets_example.py` to `/Library/Application Support/Perceptive Automation/secrets.py`
   and fill in your API keys
3. Configure the plugin via Plugins → SigenEnergyManager → Configure
4. Create a **Battery Manager** device — the plugin starts managing immediately

## What it does

- Reads inverter data every 60 s via Modbus TCP
- Projects battery SOC at next dawn (P10 Solcast forecast + consumption profile)
- Imports from grid only if the battery cannot reach dawn at the configured minimum SOC
- Exports surplus solar in two stages (2 kW / 4 kW) to prevent 100% SOC PV curtailment
- Schedules imports at cheapest available rate (Tracker/Go/Flux/Agile aware)
- Manages Axle VPP events with pre-charge and post-event reserve protection

## Version history

| Version | Notes |
|---------|-------|
| 1.1 | Fix nighttime grid import caused by export_limit=0W throttling battery discharge. Symmetric 10% export hysteresis deadband. 48 unit tests. |
| 1.0 | Initial release. |

## Files

| File | Purpose |
|------|---------|
| `plugin.py` | Indigo plugin lifecycle, Modbus polling, decision dispatch |
| `battery_manager.py` | Stateless decision engine (testable without Indigo) |
| `sigenergy_modbus.py` | Modbus TCP client, register map, control methods |
| `solcast.py` | Solcast API — P10/P50 forecast with bias correction |
| `octopus_api.py` | Octopus Energy API — rates, tariff detection |
| `axle_api.py` | Axle VPP — event polling and 5-state machine |
| `test_battery_manager.py` | 48 unit tests — run with `python3 test_battery_manager.py` |
| `plugin_utils.py` | Shared startup banner utility |

## Author

CliveS & Claude Sonnet 4.6
