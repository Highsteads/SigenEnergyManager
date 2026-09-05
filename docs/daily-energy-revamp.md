# Daily energy revamp — design (v5.89.0 counters, v5.90.0 export feedback)

Written 05-Sep-2026, Fable 5.1, before any code. Stages are committed and shipped one at a
time; each is complete on its own.

## The fault that started it

`homeDailyKwh` read 18.6 kWh from midnight on 5-Sep while the house had used 8. It had been
frozen since 4-Sep, and 4-Sep's record in `daily_history.json` is a copy of 3-Sep's. The
half-hourly table has `home_kwh = 0` for every slot since, and `daily_summary` (written by
TariffAnalyser from that table) shows 0.06 kWh for the day.

Mechanism, measured from the plugin log:

1. `_check_midnight` zeroes `home_daily_kwh` at local midnight.
2. The next poll merges a CACHED `homeDailyDirectKwh` (register 30092) up to ten minutes old
   — v1.13's read tiering, shipped 3-Sep — and writes yesterday's total into the fresh store.
3. The inverter rolls its own counter to 0.01 moments later.
4. The reset guard sees 18.60 -> 0.01, calls it suspicious, and holds the wrong value for the
   whole day (4,019 times on 5-Sep).

The guard's comment predicted the race and defended the wrong direction. Before v1.13 the
register was read every cycle, so the window was one poll and the race rarely landed wrong.

## Why it keeps breaking

The plugin has THREE definitions of "today":

- Europe/London midnight (`_check_midnight`)
- the inverter's own day — governs 30092 and the inverter daily charge/discharge (30566/30572)
- whenever the next poll happens to land — when the lifetime anchors re-snapshot

and it stores daily totals as MUTABLE running state, so once one is wrong nothing can recompute
it. Every fix so far picked a different one of the three to be right.

## Principles

1. **One clock.** Every daily figure is defined against Europe/London midnight. The inverter's
   clock is irrelevant (measured 05-Sep-2026: register 30000 holds local wall-clock as an epoch,
   tz register 30002 = 0, within 1 s of the Mac — so its midnight IS ours, but nothing depends on
   that any more).
2. **Derive on read, never accumulate.** Keep a boundary ANCHOR of each monotone lifetime counter
   at midnight; today's figure is `latest - anchor`. A missed midnight is a late anchor, which is
   repairable. A restart recovers exactly. No guard exists, so none can latch.
3. **Never cache a discontinuity.** A cached value is fine for a level and wrong for anything
   that resets or wraps. Reset-type registers are served only on the cycle they were read.
4. **Reconcile every cycle.** `pv + import + discharge - export - charge - home` must close, and
   the derived house figure must agree with the inverter's own daily counter. A WARNING when
   either drifts. Today's fault would have paged within the hour.

## Registers (probed read-only on the live inverter, 05-Sep-2026, two samples 90 s apart)

| Register | Meaning | Type | Verified |
|---|---|---|---|
| 30088 | PV lifetime | U64 /100 kWh | in use since v1.0 |
| 30092 | Load DAILY (inverter day) | U32 /100 | in use; now cache-excluded |
| 30094 | Load LIFETIME | U64 /100 | +0.020 kWh in 90 s at ~900 W: real |
| 30200 | ESS charge lifetime | U64 /100 | +0.010 kWh while charging at ~100 W: real |
| 30204 | ESS discharge lifetime | U64 /100 | 0 while charging: real |
| 30216 / 30220 | Grid import / export lifetime | U64 /100 | in use since v1.0 |
| 30000 / 30002 | Plant clock / tz | U32 / U16 | local-as-epoch, tz 0 |

The counters update on a coarse tick (a 90 s delta under-read PV and export by ~30%), so they
are for daily and half-hourly deltas, never for instantaneous power.

The identity closes: 4-Sep from the SQL Logger maxima gives
`44.39 + 0.10 + 10.25 - 17.66 - 18.48 = 18.60`, and the inverter's own 30092 ended 4-Sep at
18.60. At 12:00 on 5-Sep the identity gave 12.06 against 30092's 12.05.

Sources: the TypQxQ community definitions (read from the V2.9 PDF), validated against the four
addresses we already use, then probed. A register in the spec is not a register on the
hardware — the 30094 and 30200 blocks carry absent-latches with fallbacks for firmware that
lacks them (house from the identity; battery from the inverter's daily counters, labelled).

## Stage 1 — counters (v5.89.0)

### sigenergy_modbus.py v1.14
- Slow tier reads four plant BLOCKS in place of four single reads: 30088+6 (pv lifetime +
  daily load), 30094+4 (load lifetime), 30200+8 (charge + discharge lifetime), 30216+8
  (import + export lifetime). Same transaction count per sweep, three new values.
- `NO_CACHE_KEYS` = the reset-type registers (`homeDailyDirectKwh`, `batteryDailyChargeKwh`,
  `batteryDailyDischargeKwh`). Present in the dict only on a cycle they were actually read.
- `data["_energyReadAt"]` = epoch seconds when the lifetime blocks were read this cycle; absent
  when they were served from cache.
- `mark_slow_read_due("_energyA", "_energyB", "_energyC", "_energyD")` at midnight so the
  new day's anchor comes from a fresh read on the very next cycle.

### daily_energy.py (new, Indigo-free, pure)
`DailyEnergy` holds `anchors[date] = {key: kwh}` plus `anchor_soc`, and `latest[key] = (kwh,
read_at)`. Keys: pv, home, gridImport, gridExport, batteryCharge, batteryDischarge.
- `observe(readings, read_at, soc_pct, today)`: records the latest lifetime values. If today
  has no anchor, captures one now — from `recovery` (the device's own daily counters, so
  `anchor = lifetime - device_daily`) where available, else from the readings, marked
  `provisional`. A provisional anchor taken from pre-midnight readings is REPLACED by the first
  post-midnight fresh read.
- `rollover(new_date)`: called at local midnight; the next `observe` captures the anchor.
- `today(date)`: `{key: max(0, latest - anchor)}` plus `partial`/`provisional` flags.
- `balance(date)`: the identity residual.
- `to_dict()` / `from_dict()` for `accumulators.json` (`daily_energy` key).
- Migration: on first load with no `daily_energy` block but same-day `pv/import/export_lifetime_
  start_kwh`, those seed the anchor for the three keys they cover; the other three recover from
  the device daily counters on the first read.

### plugin.py
- `_accumulate_daily_energy` -> `_observe_energy_counters`: feeds `DailyEnergy`, then PROJECTS
  the derived values into the existing store keys (`pv_daily_kwh`, `grid_import_daily_kwh`,
  `grid_export_daily_kwh`, `home_daily_kwh`, plus new `battery_charge_daily_kwh`,
  `battery_discharge_daily_kwh`). The 60+ consumers of those keys are untouched; the keys are
  now a read-only projection, not state anyone mutates.
- `_check_midnight_impl`: the accumulator reset block becomes `daily_energy.rollover(today)`
  + `mark_slow_read_due(...)`. peak/min SOC and peak PV trackers stay as they are.
- `_write_daily_history`: battery charge/discharge come from the projection, not from the
  possibly-absent device keys.
- `_log_halfhourly_to_db_impl`: deltas of LIFETIME readings, so a slot spanning midnight keeps
  its energy (the old `max(0, daily - anchor)` clamp threw away the pre-midnight part of that
  slot every night — `winter_import_forecast.py` had already noticed "44-47 of 48 slots per
  day"). Two new columns, `battery_charge_kwh` / `battery_discharge_kwh`, added with the
  existing ALTER-if-missing pattern.
- Reconciliation each observe: residual over today and derived-vs-device house. WARNING once per
  day per check, re-armed when it clears. New inverter state `energyBalanceKwh` (the residual)
  so it charts.
- `_update_inverter_device`: battery daily states from the projection (never a fabricated 0.0
  on a cycle the register was not read).

### Tests
`test_daily_energy.py` (pure): anchor capture, provisional upgrade, recovery from device daily,
missed-midnight late anchor, absent register, persistence round-trip, residual, monotone-wrap
guard. `test_sigenergy_modbus.py`: block delivery by address, no-cache keys, `_energyReadAt`,
absent-latch fallback, transaction count. `test_plugin.py`: observe -> projection, midnight,
halfhourly across midnight, migration from the v5.88 accumulators file. Every guard mutation-
swept with `__pycache__` cleared per run.

## Stage 2 — mend the record (no version)

`scripts/mend_daily_energy_2026_09.py` (kept in the repo as the reference for future repairs):
- `daily_history.json` 4-Sep `home_kwh` 20.58 -> 18.60 (identity from the SQL Logger maxima,
  matching the inverter's own 30092 at 23:59). Backup beside it.
- `halfhourly` slots 4-Sep 00:00 onwards: `home_kwh` per slot from the SQL Logger
  (forward-filled deltas of pv, import, export, charge, discharge). Backup of the DB first.
- `daily_summary` 4-Sep: re-aggregated by TariffAnalyser's own collector (rolling 7-day
  refresh, `INSERT OR REPLACE`), triggered after the halfhourly repair.
- Today's record is written correctly at midnight by the new code; the script seeds today's
  anchors so the plugin starts right.

## Stage 3 — export feedback (v5.90.0)

The manager evaluates every 60 s already. Cadence is not the problem: every evaluation reads a
forecast scaled by a factor fitted on previous days, a need that is a pref or a profile sum,
and a SOC. Nothing measured about today enters.

1. **Intraday PV tracking.** `ratio = pv_today / forecast_elapsed_today` (same hourly buckets
   and bias factor as `remaining_solar`, pro-rated to the minute). Damped:
   `factor = 1 + w * (ratio - 1)`, `w = min(1, forecast_elapsed / 8 kWh)`, clamped to
   [0.6, 1.3]. Applied to `remaining_solar_kwh`. Undefined below 2 kWh of elapsed forecast.
   Exposed as `pvTrackingPct` and in the reason line. Every hour, (forecast_elapsed, pv_actual)
   is appended to `intraday_pv_tracking.json` so the damping can be tuned from data later.
2. **Measured need.** `need_today = home_today + profile(now -> midnight)`; `need_tomorrow =
   profile(day) * day_type_ratio`. The weekend uplift is MEASURED from `daily_history.json`
   (mean weekend / mean weekday over the last 8 weeks, clamped [0.9, 1.5], default 1.10 with
   fewer than four weekends of data) — the hard-coded 1.30 charged ~28 kWh against every
   Saturday when the measured mean is 23.4. A pref the user has set away from the default
   still wins, as today.
3. **Stop-export floor on actuals — designed, built, and DROPPED (05-Sep-2026).** The mutation
   sweep showed it dead by construction: the physics release fires whenever net solar is below the
   headroom to 100%, and while it does not, projected dusk SOC is at or above 100%, above any floor.
   With the tracking factor inside `remaining_solar_kwh`, the physics gate is the stop-on-actuals.
4. Reason line names the tracking factor and the measured need.

## Verification (each stage)
- Suite green twice (bare and with deps), ruff clean, `check_shared_modules.py` for SigenVPP.
- Live: derived `homeDailyKwh` within 0.1 kWh of 30092; residual within 0.1 kWh; half-hourly
  rows carry `home_kwh > 0`; the 20:00 evening message and the dashboard show the right
  figures; the reason line shows the tracking factor.
- Mutation sweep on every new guard, `__pycache__` cleared per run, anchors asserted unique.
