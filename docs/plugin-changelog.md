# SigenEnergyManager — developer changelog

The technical history, moved out of `plugin.py` on 25-Aug-2026. It had reached **2,002
lines at the top of the file** — 17.4% of an 11,534-line module — which cost every reader
two thousand lines of scrolling to reach the imports, and bought nothing at runtime.

Nothing was lost in the move: the parsed AST of `plugin.py` is byte-identical either side
of it, because only comments were taken out.

This is the *developer* record — what changed inside, why, and what it broke. The
user-facing version history is the table in [README.md](../README.md), and that stays the
one users read.

New entries go at the top, as they were kept in the file.

---

## v5.92.0 — 05-09-2026

**The earnings ledger had stopped being fed 17.2 days earlier and nothing on the system said
so.** Asked to automate the Axle ledger fetch, the research came back saying the fetch itself
needs Axle's permission — and turned up a fault that needed nobody's.

### The gap

`_record_vpp_api_status` exists because a dead events endpoint "looks exactly like a quiet
week": this install polled a revoked Axle token for six weeks in complete silence while the VPP
page read a calm "Standby". The LEDGER — the other Axle feed — had no equivalent at all.
`axle_age_days` has been computed on every `summarise()` call since the ledger shipped
(`vpp_ledger.py:590`) and rendered by nothing; `earnings.age_days` reaches `/api/status` and no
HTML consumes it; the only warning lived inside `menuShowVppEarnings`, at a 14-day threshold,
fired by hand. Same failure class, same plugin, one feed guarded and one not.

Added, modelled line-for-line on the existing guard:
- `_record_vpp_ledger_status(problem)` — latch, log on first occurrence and hourly after, and
  log an explicit recovery. Silence on recovery would leave the last word being an error.
- `_check_ledger_freshness()` — pure local work off the mtime-cached summary.
- `VPP_LEDGER_CHECK_INTERVAL = 21600` and a tick stage. Six hours, not minutes: settlement runs
  days behind and final revenue lands at the end of the following month.
- `ledgerAgeDays` + `ledgerStatus` device states beside `ledgerUpdated`, because a bare
  "imported 19/08" reads as calmly as "imported this morning" on a control page.
- `ledgerStaleDays` pref, default 7, guarded coercion per the house rule.

Three distinctions that are the whole point:
- **Never imported is NOT fresh.** An empty ledger has an age of `None`, and treating that as
  healthy would make a feed that has never delivered once read better than one a day late —
  the absent-state fault this estate keeps meeting.
- **Unreadable is not merely old**, and is reported separately.
- **A non-Axle install is never warned** about a ledger it will never have.

### The merge seam

`_merge_axle_payload(payload)` lifted out of `importAxleLedger`. The comment beside the ledger
has always said Axle's rows "arrive through ONE importer", but the load/merge/save/invalidate
sequence lived inline in a menu callback, so any automated feed would have had to copy it —
including the `_vpp_ledger_cache = None` that three dashboard pages depend on.

It now **refuses a ledger carrying `load_error`**. `save_ledger` drops that marker and rewrites
the file wholesale, so merging over an unparseable ledger destroyed both the evidence and
whatever was still in it. A test asserts the damaged file is left byte-identical.

### What the research established about the fetch itself

Ten agents across five discovery lanes, three adversarial route assessments and a completeness
critic. Recorded here so nobody repeats it:

- **The data exists and is documented.** Axle publish an OpenAPI spec (docs.axle.energy) with
  `/rewards/{site_id}/info`, `/rewards/{site_id}/transactions` and
  `/entities/site/{site_id}/flex-events`.
- **The door is shut.** All three require an ORGANISATIONAL bearer token. ha-axle-vpp issue #19
  records a real HTTP 403 from a consumer token, confirmed by that maintainer. Four independent
  integrations (ha-axle-vpp, Predbat, the Homey app, SolisAgileManager) all stop at the events
  endpoint. Asking Axle would need to be for a CONSUMER endpoint where the site is implied by
  the token — the rewards role alone would still leave no way to resolve a `site_id`, since
  `/entities/site` is itself organisation-scoped.
- **The cookie route is dead, and the old comment saying otherwise is withdrawn.**
  `vpp_ledger.py` used to name "a cookie-authenticated fetch" as a future feed. vpp.axle.energy
  is React Router v7, not Remix, so there is no `?_data=` route-loader URL; and Axle sign-in is
  **magic-link only** — no password, so no storable credential and no way to revive a dead
  session without a human clicking an email.
- **The settlement email is the live channel.** The completeness critic caught the research
  lane killing this on "no specimen exists on this system" — true of Indigo and worthless as a
  conclusion, because the emails go to CliveS's own mailbox, which nothing looked in. An
  unsearched channel read as an empty one, which is the absent-state fault applied to research.
  The plugin's own source says the opposite: `test_vpp_ledger.py:409` — "Axle email the result
  days before the account page catches up" — and the live ledger's newest Axle row IS an email
  row (`email-2026-08-16T19:00`, −3.87 kWh, 387p). The window-keyed supersede logic in
  `import_axle_payload` was written for exactly this.

So the email is the route, it needs no vendor negotiation, and it is blocked only on a
specimen and on knowing which mailbox receives it. Both are one question to CliveS.

### Tests

1118 -> 1140. Eleven mutations, each written from the consequence, all eleven proven to turn
the suite red.

One SURVIVED the first sweep: removing `self._vpp_ledger_cache = None` from the merge changed
nothing, because the cache is mtime-keyed and the file's mtime moves on write. The invalidation
is not redundant though — the cache compares `cached[0] == mtime` exactly, so on any filesystem
with coarse mtime a merge inside the same tick would serve pre-merge figures to the dashboard.
APFS is fine-grained enough that no natural test can reach it, so a test now freezes
`os.path.getmtime` and pins it. *A fixture only tests the regime it was built in.*

---

## v5.91.0 — 05-09-2026

**The VPP summary headlined the figure Axle does not pay on, and the per-minute snapshot
threw away evidence it had already paid for.** Found by reading back the 05-Sep-2026
18:30 UTC window, which was itself textbook: 4.000 kWh integrated inside the hour against a
4 kW cap, 55 in-window samples with a mean of 3,999.6 W and a standard deviation of 19 W,
mode 0x06 held on all 58 snapshots, and no external interference.

### 1. `estimate_gbp`, the Pushover title and the event-log line all priced the whole run

`export_kwh` is the counter delta across everything we drove. The driver deliberately runs
T-2min to end+2min (v5.28), so on a textbook event it is ~0.23 kWh larger than the settled
figure. `window_kwh` — integrated strictly inside the paid hour — has been computed and
stored in the ledger since v5.63.0 with a comment saying it is *"the only one comparable
with what Axle settled"*, and then every headline used the other one.

**Axle's own settlements decide it.** Over the seven events they have settled, measured
05-Sep-2026 from `vpp_ledger.json`:

| | mean abs error vs Axle |
|---|---|
| `window_kwh` | **0.17 kWh** |
| `export_kwh` | 0.82 kWh |

The ledger's eight local rows read **GBP 40.77** where the settled basis gives **GBP 35.99**
— 13.3% high overall, ~25p an event routinely, and GBP 3.05 of it from the single 11-Aug
over-run (7.05 kWh recorded for a hour whose cap allows 4).

Changed:
- `vpp_ledger.record_local_event` prices `estimate_gbp` off `window_kwh`, falling back to
  `export_kwh` only when the window figure was never measured, and records which it used in
  a new `estimate_basis` field.
- `_summarise_vpp_event` leads the title, the body and the event-log one-liner with the paid
  hour, keeps the run total as an explained secondary sentence, and says plainly when the
  window figure could not be measured rather than passing the run total off as settled.

**Scope, stated because it is easy to overstate:** `estimate_gbp` on a local row is written
and never read by any code, and `summarise()` has always used `window_kwh` with a `run_kwh`
fallback for its `our_kwh` column. So no money figure on the dashboard was ever wrong — the
lifetime and month-to-date totals come from Axle's settled pence. What was wrong is what a
human reads: the notification, the log line, and a stored estimate that misled the very
analysis that found this.

### 2. `_verify_vpp_export_registers` — the snapshot now acts on what it already read

`_log_vpp_snapshot` reads the mode register, the charge limit and the discharge limit every
minute of every window and, until now, only wrote them to JSONL. So a drift landing between
manager cycles was *recorded* and not *acted on*.

The VPP branch of `_verify_ems_registers` is extracted into
`_verify_vpp_export_registers(ems_mode=None, charge_w=None, discharge_w=None)`. Any argument
left None is read as before; the snapshot passes the three it holds. One set of rules, one
place — which matters here, because the question "which registers may be written mid-window"
has an expensive wrong answer (10-Apr-2026, a stray limit write causing a 2 kW grid import),
and two copies would have been free to diverge on it.

The state guard moved INSIDE the method. It used to be the `elif` condition; with a second
caller, a routine that re-asserts an export mode must be unable to fire outside a live window
whoever calls it.

**Measured cadence, which changed the design.** The first plan was to cache the reads and
drop the duplication. The log says the two callers do not overlap — over the live window,
verify, then the snapshot 39-48 s later, then verify ~20 s after that — so the mode is really
checked every 20-45 s, and a cache would have made the effective rate variable and *worse*.
Read counts over the hour (110 `read_ems_mode`, 110 `read_discharge_limit`, 55
`read_charge_limit`) confirm 55 runs of each caller. So the reads stay and the evidence is now
used: no extra Modbus traffic, roughly double the acting check rate in-window.

### 3. `_fit_pushover_body`, and four stale cadence comments

Rewriting the headline into plain English took the body to **1062 characters**. Pushover's
free tier truncates silently above 1024, and the casualty would have been the last line of
the Ask Claude prompt. `_fit_pushover_body(essential, optional, tail)` sheds *diagnostic*
lines from the bottom — every one of which is in the JSONL anyway — announces how many went,
and marks a last-resort truncation rather than letting one happen invisibly. The money lines,
the external-control warning and the prompt are never shed. The title's em-dash and the
`── Ask Claude ──` separator became ASCII, per the standing no-Unicode rule for notifications.

Four comments claimed the manager cycle runs every ~15 minutes. `MANAGER_EVAL_INTERVAL` is
**60**, and 55 runs were counted in the hour. Corrected in `_verify_ems_registers`'s
docstring, `_end_vpp_export` (which had used the wrong figure to size a failed hand-back at
"~1.25 kWh" — nearer 0.08 on the real cadence), the store-init comment and
`_retry_vpp_handback`. `VPP_OVERRUN_GRACE_MINS`' comment was checked and left alone: it is
about the 15-minute grace constant and already says the poll is 60 s.

### Tests

1080 -> 1118. Ten mutations, each written from the consequence ("the headline shows the run
total again", "the snapshot throws its reads away again", "the state guard is gone"), all ten
proven to turn the suite red, with `__pycache__` cleared before every run and each restore
asserted byte-identical.

One mutation SURVIVED the first pass and it was the fixture's fault, not the code's: the
end-to-end test used a temp path ~50 characters long where the real event-log path is ~170,
so the un-fitted body still fitted and nothing noticed `_fit_pushover_body` being removed.
A test now builds the real directory depth. *A fixture only tests the regime it was built in.*

---

## v5.90.2 — 05-09-2026

**`openmeteo_forecast.py` v1.8 — the optimiser file path is injectable; an empty forecast is
never published.** `OPTIMISER_FORECAST_FILE` was a hardcoded absolute path into the live Python
Scripts folder, and `test_complete_fetch_is_ok_and_cached` mocks every array to return `[]` and
calls `fetch_forecast(force=True)`, which reaches `_write_optimiser_file` — so every
`scripts/run_tests.py` run on the Indigo Mac wrote the LIVE `openmeteo_forecast.json` with zero
slots (fingerprint: `bias_bands` centres present with every factor 1.0, no `Wrote optimiser file`
line in the plugin log). It stayed wrong until the next fetch (<= 30 min), and the evening/overnight
planner run in that window would have read "0 kWh of solar". Caught rendering the 20:00 message
against live data at 13:34 — the file had been zeroed by the 13:32 suite run.

- `OpenMeteoForecast(..., optimiser_file=None)`; `_write_optimiser_file` writes `self.optimiser_file`.
- The writer returns with a WARNING when `hourly_out` is empty.
- `test_openmeteo_forecast.py` re-points the module constant to a temp path for the whole module
  AND passes `optimiser_file=` on every construction; `TestOptimiserFileIsolation` (4) pins both,
  including that the constant no longer contains "Perceptive Automation" under test.
- `openmeteo_battery_optimiser.py` v3.18 (Python Scripts, same session): a plan-day total of 0 is
  UNKNOWN — on an EVENING run it falls back to the plugin's `tomorrowSolarKwh`, and with no hourly
  set the shape-dependent power-cut line says the forecast was not to hand.
- General rule recorded in the global CLAUDE.md: check the live files' mtimes around a suite run.

## v5.90.1 — 05-09-2026

**`_write_site_config` publishes the manager's own weekday/weekend figures.** v5.90.0 wrote
`daily_kwh_weekday = sum(profile)` and `daily_kwh_weekend = sum(profile) * uplift` (and the hourly
profiles likewise), while `_build_manager_snapshot` splits the blended profile through
`_need_scales()` so the week still averages it. The optimiser's evening message reads the file for
its "against ~N kWh of house use" line and the plugin's flood preview for its "~N kWh typical use"
line, so the two figures in one message disagreed by 0.9 kWh (24.4 vs 23.5 on 05-Sep). Both now
use `_need_scales`. Test: `test_profile_is_split_with_the_same_scales_the_manager_uses`. Found by
auditing the 20:00 message's sources before it went out.

## v5.90.0 — 05-09-2026

**Export feedback — the day's own evidence enters the decision.** Stage 3 of
`docs/daily-energy-revamp.md`. Modules: `battery_manager.py`, `plugin.py`, `Devices.xml`.

**The gap.** `_evaluate_manager` ran every 60 s, and every run read `remaining_solar_kwh` (forecast
buckets x `bias_factor_today`, a band fitted on PREVIOUS days), `need_24h_kwh` (a pref, or the
profile sum x a hard-coded 1.30 at weekends) and the SOC. Nothing measured about today entered.
Re-checking could not help: same inputs, same answer. The weekend uplift was measured at 1.10
(26 weekends since June: weekday mean 21.2, weekend 23.4) against the 1.30 in code, so every
Saturday charged ~28 kWh of need against the balance.

- **`pv_tracking_factor(actual, forecast)`** (battery_manager, pure): `ratio = actual/forecast`,
  `factor = 1 + w(ratio - 1)`, `w = min(1, forecast / 8 kWh)`, clamped [0.6, 1.3]; `(1.0, None)` below
  2 kWh of elapsed forecast. Applied in `_calculate_24h_balance` right after the band factor:
  `remaining_solar_kwh *= snapshot.pv_tracking_factor`.
- **Accumulators** (`_update_pv_tracking`, once per evaluate): `pv_track_actual_kwh` += the
  projection's PV delta since the last evaluate; `pv_track_forecast_kwh` += `_forecast_kwh_between
  (last, now)` — the forecast integrated bucket-by-bucket over the SAME interval (robust to an
  hourly refresh: each minute takes whatever bucket is current). Both advance ONLY while
  `_pv_unclipped()`: export below 95% of `maxExportKw` and not (SOC >= 99 and battery <= 100 W).
  At the cap, or on a full battery, the inverter turns PV away and a shortfall says nothing about
  the weather — learning from it would talk the plugin out of exporting on the very days it must
  (low ratio -> less remaining -> physics gate releases -> battery charges -> clips again). Clipped
  minutes are counted (`pv_track_clipped_min`). A day whose PV anchor is `late`/`absent` yields a
  neutral factor. Reset at local midnight; persisted in `accumulators.json` (same-day restore).
- **Recorder:** one row per local hour to `intraday_pv_tracking.json` (ring of 2000) — the data
  the damping constants get tuned from.
- **Measured need** (`_calculate_24h_balance`): when `home_today_kwh` is known and not partial,
  `need_24h = used_so_far + (profile[slot:48] / sum(profile)) * need_day`. Still the full calendar
  day against supply to dusk (the deliberate conservatism stands), but the elapsed part is real.
  `SufficiencyBalance` gains `pv_tracking_factor`, `need_today_used_kwh`, `need_today_measured`.
- **Weekend uplift** (`_measured_weekend_uplift`): weekend mean / weekday mean over the last 56
  days of `daily_history.json`, partial days and < 2 kWh days excluded, >= 10 weekdays and >= 4
  weekend days else `WEEKEND_UPLIFT_DEFAULT` = 1.10, clamped [0.9, 1.5], cached per day, logged on
  change. `_need_scales(u)`: `wd = 7/(5+2u)`, `we = wd*u`, so the week still averages the profile
  (the old code set weekday = P and weekend = 1.3P, a week averaging 1.09P). A user pref set away
  from its default still wins, as before; away mode still means no uplift (v5.78.0).
  `sigen_site_config.json` publishes the measured multiplier.
- **Snapshot fields:** `pv_tracking_factor`, `pv_tracking_ratio`, `home_today_kwh` (None until the
  daily-energy object has seen a reading today — a fresh start cannot present 0.0 used as a
  measurement), `home_today_partial`. Neutral defaults reproduce v5.89 exactly (pinned).
- **States:** Battery Manager `needTodayKwh` (Float), `pvTrackingPct` (Integer, ratio x100, 100
  until judgeable). The `[BALANCE]` audit line and the default reason carry
  `solar tracking x0.87` and `need today 21.4 kWh (12.4 used)` only when they apply.
- **Designed and NOT shipped:** a "min-end floor on the actual trajectory" release inside
  `_check_solar_overflow`. The mutation sweep proved it dead by construction: the physics release
  fires whenever `net < headroom_to_100`, and while it does not, `soc + net/cap >= 100%`, above any
  floor. With the tracking factor inside `remaining_solar_kwh`, the physics gate IS the
  stop-on-actuals (pinned by `test_tracked_shortfall_releases_a_running_export_through_the_physics_gate`).

**Tests:** `test_battery_manager.py` +9, `test_plugin_export_feedback.py` (16, new). 1051 -> 1075.
Fifteen mutations killed; the sweep's two first-pass survivors were a dead guard (removed) and a
test whose partial-day fixture carried the same value as a real day (fixed).

## v5.89.0 — 05-09-2026

**Daily energy figures derived from lifetime counters anchored at local midnight.** Design note:
`docs/daily-energy-revamp.md`. Modules: `daily_energy.py` (new, pure), `sigenergy_modbus.py` v1.14,
`plugin.py`.

**The fault.** `homeDailyKwh` froze on 18.6 kWh from 4-Sep to 5-Sep, `daily_history.json` 4-Sep is
a copy of 3-Sep (and carries `battery_charge_kwh: 0.0`), and every `halfhourly.home_kwh` since 4-Sep
00:00 is 0. Mechanism, from the plugin log: `_check_midnight` zeroed `home_daily_kwh`; the next poll
merged a CACHED `homeDailyDirectKwh` (v1.13 read tiering, `SLOW_CACHE_MAX_AGE_S` = 600) and wrote
yesterday's total into the fresh store; the inverter rolled its own counter to 0.01 moments later;
the reset guard read 18.60 -> 0.01 as suspicious and held the value for the day (4,019 times on
5-Sep). The guard's comment predicted the window and defended the wrong direction.

**Why the class kept recurring.** Three definitions of "today" — Europe/London midnight, the
inverter's day (30092, 30566, 30572), and whenever the next poll landed (the lifetime re-anchor) —
and the totals kept as MUTABLE running state, so a wrong figure could never be recomputed.

**The model.** `DailyEnergy` keeps `anchors[date][key]` (a lifetime counter at local midnight) and
`latest[key]`; `today()[key] = latest - anchor`, `completed(day) = anchor[next] - anchor[day]`.
Six keys, all plant-level U64 lifetime counters: pv 30088, load 30094, ESS charge 30200, ESS
discharge 30204, grid import 30216, grid export 30220. Probed read-only 05-Sep-2026 against the
TypQxQ V2.9 definitions (validated on the four addresses already in use): 30094 moved +0.020 kWh in
90 s at ~900 W, 30200 +0.010 kWh while charging, 30204 stayed flat. The counters update on a coarse
tick (a 90 s delta under-read PV by ~30%), so they are for daily/half-hourly deltas only. Register
30000 holds local wall-clock as an epoch with tz 30002 = 0, within 1 s of the Mac — the inverter's
midnight IS ours, and nothing depends on that any more.

- **Rollover:** `rollover(date)` takes a PROVISIONAL anchor from the last pre-midnight reading; the
  first fresh read within 600 s after midnight replaces it (`PROVISIONAL_UPGRADE_WINDOW_S`). A
  later first read keeps the provisional one if it was within 900 s of midnight
  (`PROVISIONAL_MAX_AGE_S`), else anchors late and flags the day partial.
- **Recovery:** a key with no anchor takes `lifetime - device_daily` from the inverter's own daily
  counter read THIS cycle (`RECOVERY_DATA_KEYS`), so a mid-day start recovers house/battery exactly.
- **Never cache a discontinuity:** `NO_CACHE_KEYS` (30092, 30566, 30572) are present in `read_all()`'s
  dict only on the cycle they were read. `data["_energyReadAt"]` stamps a cycle that read a lifetime
  block fresh; `observe(fresh=False)` changes nothing.
- **Blocks:** four plant block reads (30088+6, 30094+4, 30200+8, 30216+8) replace four single reads —
  same transaction count, three new values. B and C carry absent-latches (three misses on a healthy
  link); the fallbacks are the identity for house and the device daily counters for battery flow.
- **Projection:** `_observe_energy_counters` feeds the object and PROJECTS the figures into the
  legacy store keys (`pv_daily_kwh` etc., plus `battery_charge_daily_kwh`,
  `battery_discharge_daily_kwh`, `energy_balance_kwh`, `energy_day_partial`). The sixty-odd
  consumers of those keys are untouched; nothing else may write them.
- **Midnight:** the first observe of a new day snapshots the projection
  (`energy_yesterday_projection`) and marks the energy blocks due; `_check_midnight_impl` waits up
  to `MIDNIGHT_ANCHOR_WAIT_S` = 600 s for the post-midnight read to replace the provisional anchor,
  then records yesterday from `completed()` — exact, and immune to the order the tasks ran in (the
  old code would have written the new day's zeros, since observe runs before midnight in `_tick`).
  Record gains `energy_balance_kwh`, `energy_partial`, `energy_sources`.
- **Half-hourly:** deltas of the lifetime snapshot (`hh_anchor_lifetime`), so a slot spanning
  midnight keeps its energy; new columns `battery_charge_kwh`, `battery_discharge_kwh` (ALTER-if-
  missing). The old daily-store anchors are ignored and reseeded (one skipped slot).
- **Tripwire:** `_reconcile_daily_energy` warns once per day, re-armed on clearing, when
  `|pv + import + discharge - export - charge - house| > max(0.5, 3% of throughput)` or when the
  derived house figure differs from 30092 by more than `max(0.5, 5%)`. New inverter state
  `energyBalanceKwh`.
- **Persistence:** `accumulators.json` gains `daily_energy` (restored whatever day the plugin starts
  on) and `energy_yesterday_projection`. A pre-5.89 file on the same day seeds pv/import/export
  anchors from `*_lifetime_start_kwh` (`migrate_legacy`); house and battery recover on the first read.
- **Device states:** battery daily charge/discharge and the new balance come from the projection —
  never `data.get(..., 0.0)`, which would chart a fabricated 0.0 on the cycles the register was not
  read.

**Untouched:** the decision engine, the bank-first hold, VPP, storm, flood prevention. The export
feedback loop that reads these figures is v5.90.0.

**Tests:** `test_daily_energy.py` (28), `test_plugin_daily_energy.py` (19), `test_sigenergy_modbus.py`
+9. 1032 -> 1051. Fifteen mutations, each killed, `__pycache__` cleared per run, anchors asserted
unique, files restored byte-identically.

**Still to do (Stage 2, no version):** mend `daily_history.json` 4-Sep (home 20.58 -> 18.60, battery
0.0 -> 18.48/10.25), `halfhourly.home_kwh` for 4-Sep and 5-Sep from the SQL Logger identity, and
`daily_summary` 4-Sep — `scripts/mend_daily_energy_2026_09.py`.

## v5.88.0 — 04-09-2026

**The bank-first latch classified today from yesterday's forecast.** `_record_bank_first_metrics`
reset the latch on a local-date change and immediately re-armed it from `latest_forecast_data
["todayKwh"]`, which between local midnight and the first fetch of the new day (84 minutes on
4-Sep) still held YESTERDAY's total. 4-Sep was forecast 45-49 kWh from 00:24 and was held anyway,
08:00-12:47, 7.244 kWh withheld. Fix: every forecast dict carries `forecastDate`; the latch arms
only when it equals today. Second fix, same family: `_overflow_bank_first_blocked` read a missing
forecast (`raw_today = 0.0`) as a small day and held export off; absent is now UNKNOWN and stands
aside. 989 -> 995 tests, four sabotages proven red. Full detail: the README row and the
Plugins/CLAUDE.MD chain entry (this file was not updated at the time — backfilled 05-09-2026).

## v5.87.1 — 04-09-2026

**Six config fields drew under the wrong heading.** `PluginConfig.xml` had no separator between the
power-cut block and the solar-overflow block, so `solarOverflowTargetSoc`, `solarOverflowMinEndSoc`,
both bank-first fields, `solarOverflowShadowEnabled` and `stormExportReleasePct` rendered under
POWER-CUT NOTIFICATIONS. New `separator_solarexport` / `label_solarexport` (DAYTIME SOLAR EXPORT)
plus an info line saying which fields start an export and which do not — CliveS had set the charge
target to 93 meaning the export-start level. Wording and layout only; client restart to see it.
Backfilled 05-09-2026.

## v5.87.0 — 03-09-2026

**The loop slept the interval on top of the work.** `runConcurrentThread` ran `_tick()` then
`self.sleep(min(modbus_poll_s, 10))`, so the real period was work + interval: 43 s before v5.86.0
and 12 s after, against a 5 s setting. Now sleeps the REMAINDER (`max(0.2, interval - tick_took)`),
measured 12 s -> 8 s; long intervals unaffected. Companion fix: the energy fallback that integrated
power used the CONFIGURED interval rather than measured elapsed time, under-counting ~8x; it now
measures, clamped so a post-outage reconnect cannot dump an hour into the day. 982 -> 989 tests.
Backfilled 05-09-2026.

## v5.86.0 — 03-09-2026

**Read tiering in `sigenergy_modbus.py` v1.13.** `read_all()` did 29 transactions at the spec's 1 s
spacing, so a poll took 43 s MEASURED whatever `pollInterval` said, and the three power figures
came from instants seconds apart while `homePowerWatts` is derived from them (0 W house readings
on broken-cloud days). Now the six critical reads run every cycle, PV+ESS from ONE block read, and
everything else rotates three per cycle behind `_slow_cache` (600 s TTL). ~8 transactions per
cycle; fresh-reading gap 43 s -> 12 s. NB: this cache is what served the daily-load register across
midnight and froze `homeDailyKwh` on 4/5-Sep — see v5.89.0. Backfilled 05-09-2026.

## v5.85.1 — 03-09-2026

**`tokenBalance` said 1; the Octopus app showed CliveS 0.** Reported within the hour of v5.85.0
shipping. The schema is unambiguous — *"The account's Weekend Happy Hours token balance (zero if
it has none)"* — and it still disagreed with Octopus's own UI.

**Which is right is not the point.** The app decides what the account may actually do; the API
field is a number. v5.85.0 turned that number into a VERDICT — *"so it cannot be booked yet"* —
which is a prediction about what Octopus will allow, made from a field measured to contradict
them. **A wrong refusal talks the owner out of a free hour he was entitled to, which is a far
worse failure than a redundant nudge**, so the asymmetry decides the design: report, never refuse.

The count is now ATTRIBUTED rather than asserted — "Octopus's API reports 1 of the 2 tokens a
booking needs — check the app, which is the authority, and book it there if it lets you" — and no
path can produce "cannot be booked". Two tests hold that down: one asserting the refusal wording
never appears on a low balance, one asserting the number is never stated in our own voice
("You have N"). Proven by reinstating the v5.85.0 wording, which turns both red.

**The general lesson, and it is not Octopus-specific: a documented API field can disagree with
the vendor's own UI, and the UI is authoritative for what the user can do.** A field description
tells you what a value is MEANT to be, not that it matches what the user sees. Anything derived
from a vendor field and shown to a user should be attributed to that vendor, so a disagreement
reads as a disagreement rather than as the plugin being wrong — or worse, as the plugin quietly
overruling the thing the user is looking at.

971 -> 972 tests. No behaviour change beyond wording; nothing about how the battery is driven.

## v5.85.0 — 03-09-2026

**The Happy Hour alert told CliveS to do something he could not do.** Four pushes went out on
03-Sep-2026 saying "opt in to earn" for four Sunday slots — and a Weekend Happy Hour is not
opted into, it is BOOKED, and booking costs tokens his balance could not pay for. Four
interruptions, no possible action. **Advice that cannot be acted on is worse than silence,
because the reader spends attention working out that there was nothing to do.**

The schema had the answer all along and the query never asked: `tokenBalance` on the account,
`capacityStatus` on each event. Both are now fetched, and the Happy Hour message is built from
what the reader can actually do — booked (and whether the battery will charge for it), slot
full, not enough tokens, or ready to book.

**The token balance is reported VERBATIM and never derived.** The accrual rule cannot be
reconstructed: measured against this account, 24 successful turn-downs and one booking leave a
balance of 1, which fits no simple "N earned per success, M spent per booking" arithmetic, and
the scheme itself only began on 16-Aug-2026. So the plugin repeats the API's own number or says
nothing. **`None` means NOT REPORTED and is deliberately distinct from 0** — "you have no
tokens" is a claim, and one the API never made. The device state uses `-1` for unknown, because
an Integer state cannot hold None and 0 is a real balance meaning something quite different.

**What a booking COSTS is a pref, not a constant.** It is absent from the API and underivable,
so `happyHourTokensRequired` (default 2, Octopus's published figure) holds it: if they change
it, that is one field to edit rather than a release. Set 0 and the alerts stop mentioning
tokens at all.

Also closes the worst silent outcome available here: a slot BOOKED, free power waiting, and the
plugin sitting it out because `happyHourImport` is unticked. The booking alert now says so, at
the moment it can still be fixed.

963 -> 971 tests; four sabotages each proven red (unknown-balance-as-zero, nag-without-tokens,
offer-a-full-slot, silent-when-switched-off), each asserted to have applied and each restore
byte-identical. NB the repo's own `TestNoTestsStrandedBelowMain` caught the new class being
appended below `__main__`, where it would never have run — a gate earning its keep.

NB v5.84.0 shipped a README row but no entry here, so the chain skips it; that one is the other
session's to backfill.

## v5.83.1 — 03-09-2026

**An armed Octopus session was not observable from anywhere.** Found half an hour before a live
18:00 turn-down, trying to answer "is this actually armed?" and discovering there was no way to:
the window cache lives only in `store["saving_sessions_windows"]`, is not persisted, and appears
in neither the decision audit nor the status API. The audit's `[OVERRIDE]` line still read
"skipped — no VPP active, no running flood export", wording that predates both new overrides, so
a cached session and no session looked identical. **A feature you cannot check before it runs can
only ever be verified after it has already failed** — and a restart clears the cache, so this was
not hypothetical.

Two changes, both read-only:
- The `[OVERRIDE]` audit line now names `saving_session_active` and `happy_hour_active`.
- `/api/status` gains `octopus_sessions` (the cached windows with their direction, plus
  `next_start`) and two flags, `saving_session_export_active` and `happy_hour_import_active`.

Direction is included in the exposed window because it decides which way the window drives, so a
reader can tell an export window from an import one without inferring it from the battery.

No behaviour change. 958 tests.

## v5.83.0 — 03-09-2026

**Weekend Happy Hour import — Phase 3.** During a BOOKED Octopus Happy Hour (an hour of free
electricity, earned by two successful turn-downs), grid-charge the battery at full inverter power
so the free energy is banked instead of wasted. Specced first in
`docs/happy-hour-import-spec.md` and signed off before a line was written; CliveS chose passive
fill, count-and-tag, full charge rate.

**Booked is the whole point.** Octopus offers FOUR 1-hour slots each Sunday (11:00-14:00 BST) and
only the reserved one comes back `joined` — measured 03-Sep-2026: Sun 16 Aug 12:00Z was booked,
its three siblings were not. The cache admits joined events only, so the unbooked siblings can
never drive anything.

**It fills to the configured target, not to 100%** (`solarOverflowTargetSoc`, 93 live). Free
electricity is not a reason to override a rule that exists to protect the pack — and following
the pref means changing that one setting moves both features. NB the standing note says 95 while
the live pref is 93; that discrepancy is the existing open item, not a decision taken here.

**No export gate, deliberately.** Unlike the turn-down branch this IMPORTS, so a post-power-cut
lockout or storm export-suppression is irrelevant, and a storm actively WANTS a full battery.
The asymmetry is commented at both sites because it is exactly the kind of thing a later tidy-up
"harmonises" into a bug.

**A turn-down and a happy hour together FAILS CLOSED** — they cannot both be right and guessing
which way to drive the battery is worse than doing nothing. They should never co-occur (weekday
evenings vs Sunday middays), so if they do, something upstream is wrong and quietly picking one
would hide it.

**Termination is bounded three ways** — window end, target SOC, and `_check_happy_hour_overrun`,
an independent backstop sharing no dependency with the primary path (the v5.62.0 lesson: one
path ending a window is one path too few). Importing past a free hour means BUYING at 25p, so
the exposure is real money. Hand-back is CONFIRMED with the `vpp_handback_pending` retry.

**Free kWh is measured from ONE anchor** — the cumulative import counter captured at entry and
persisted immediately, so a restart mid-window can neither double-count nor lose it. Surfaced as
`happyHourFreeKwhLast`, tagged rather than blended into ordinary import: the headline
self-sufficiency figure stays honest and matches the meter, and the Sunday dip is explainable.

**One shared window cache, two readers.** `_window_of_direction` is the single implementation, so
freshness and malformed-row handling cannot diverge; `_saving_session_window` and
`_happy_hour_window` each filter to the direction they drive. A row with NO direction is driven
by neither — fail closed both ways.

Reuses `force_charge` (mode 0x03 + charge limit + hardware charge-cutoff backstop), the same
proven path `ACTION_START_IMPORT` uses, so a crash mid-window cannot leave it charging unbounded.
New `inverter_max_kw` on the snapshot — the charge decision needs the CHARGE rate, and
`max_export_kw` is the DNO EXPORT cap (4 kW here), a different number.

**Honest value: ~£8-15 across the rest of the promotion, which ends 1 November.** Slots land at
peak solar, so a bright September Sunday leaves no headroom and it will correctly do nothing;
October is where the money is. Scoped small on purpose. Pre-drain to manufacture headroom was
considered and DEFERRED — revisit after one October hour shows whether it earns the extra cycle.

Ships OFF (`happyHourImport`). Tests 935 -> 957; five sabotages each proven red (5/1/1/5/6), each
asserted to have applied and each restore byte-identical. **Two existing tests were updated
rather than deleted**: the v5.82.0 "happy hour is never cached" assertion now asserts the
unchanged INTENT (never reaches the export path) since the mechanism moved to per-reader
filtering, and the turn-down window fixtures gained the `direction` they now carry — plus a new
symmetric test that an untagged row drives neither.

## v5.82.0 — 03-09-2026

**NO DIRECTION GUARD — v5.81.x would have exported the battery through a Power Up and through a
free-electricity hour.** Found by finally reading the Octopus dashboard page, which advertises
"Power Up — use more electricity in these sessions", and then introspecting the schema:
`SavingSessionsFlowDirection = TURN_DOWN | TURN_UP | WEEKEND_HAPPY_HOUR`, exposed as `eventType`
on every event. **The query never asked for it.**

The consequences of that omission are both wrong in the same direction — spend battery, earn
nothing:

* **TURN_UP** wants MORE consumption. Exporting is exactly backwards.
* **WEEKEND_HAPPY_HOUR** is FREE electricity. Exporting through it wastes the free power AND
  drains the battery.

**And this is not hypothetical: 12 of the 62 events on this account are already happy hours.**
The current promotion (to 1 Nov) grants an hour of free power for every two successful
turn-downs, so the account is actively working towards events of exactly the kind the code would
have mishandled — and a scheduled happy hour may well arrive flagged as joined.

`get_saving_sessions()` now returns `direction`; the window cache admits **only** TURN_DOWN, and
an absent or unrecognised value is never treated as one (a direction Octopus adds later must not
be assumed safe). The alert still fires for the others — knowing a Happy Hour is coming is
useful — but says what it is and that the battery is not driven for it.

Same class as the Axle import/export guard added in v5.57.0, which shipped without one and would
have self-driven a full 4 kW export through an IMPORT event. Twice now: **when an external feed
announces an event, ask which way it runs before acting on it.**

Tonight's session is TURN_DOWN / status UPCOMING / joined, 61 pts/kWh, so it still fires. 5 tests;
930 -> 935, and removing the guard turns 3 red.

## v5.81.1 — 03-09-2026

**The announcement dedupe depended on the id's Python TYPE.** `_check_saving_sessions` compared
the raw `event["id"]` against the persisted `saving_sessions_notified` set. Live that happens to
work — Octopus returns the id as an int and JSON round-trips it as an int — but **GraphQL's `ID`
type is SPECIFIED to serialise as a string**, so the day that payload changes, every hourly poll
stops matching and re-announces the same session. Both sides are now normalised with `str()`,
which also migrates a set persisted as ints by 5.80.x.

**Found by a probe, not by the suite.** Driving the shipped `_check_saving_sessions` against the
live API with a store seeded as `["5899"]` sent a Pushover for an event that was already
announced. The 927-test suite could not have caught it: every fixture used one type
consistently, so it was asserting the arithmetic of matching rather than the behaviour under
the type variance the API actually permits. `octopus_api.py` was already normalising with
`str()` for the `joined` lookup, so the two halves disagreed — the more useful signal, and the
reason the fix is to make the convention explicit in both places rather than to match whatever
today's payload happens to be.

3 tests (int-vs-str, str-vs-int migration, and a genuinely-new event still notifying as the
negative control); 927 -> 930, and reverting the normalisation turns 2 of them red.

## v5.81.0 — 03-09-2026

Phase 2, at CliveS's request: **drive the battery to export during a Saving Session** — the
thing v5.80.0 deliberately did not do. His rule, verbatim: do it "as long as it does not
interfere with an Axle event the same day unless we can do both and still have enough to get
through to next morning without importing."

**PER-EVENT OPT-IN IS THE HEADLINE, and it was found by checking rather than assuming.**
Campaign membership does NOT enrol you in each session. Measured today: this account reads
`hasJoinedCampaign=True` with **36 joined events ending 16-Aug-2026**, while **all 17 sessions
since — the entire new season — are un-joined**, including 1 Sept at 68 pts/kWh and 2 Sept at
56. They were missed in silence. So `get_saving_sessions()` now returns a per-event `joined`
flag, the alert LEADS with "NOT OPTED IN — join it in the Octopus app, or it pays nothing"
and logs at WARNING, and **the export branch will not fire for an un-joined session at all**:
exporting into one earns no Octopoints, so it would drain the battery for the plain 12p export
rate. Had I taken the earlier "you're already enrolled" reading at face value, the feature
would have driven the battery for nothing on its first run.

**Axle wins, and it is not close.** Axle pays about £1/kWh; a Saving Session at 61 Octopoints/kWh
pays 7.6p/kWh (800 points = £1) — roughly **13x**. So the new branch sits STRICTLY BELOW the
VPP override in `_check_overrides`, which means an overlapping Axle window has already returned
before this line is reached and the session can never take a paid VPP minute. It also cannot
spend Axle's *energy*: `snapshot.vpp_today_kwh` (future-only, pro-rated, in place since v5.19.4)
is subtracted from what may be exported, which is the "unless we can do both" half of the rule
priced rather than assumed.

**"Still reaches next morning without importing"** is expressed against the engine's own
projection rather than a second model: new pure `saving_session_exportable_kwh()` takes
`balance.battery_at_dawn_kwh` (which already carries the solar still to come and the overnight
drain), subtracts the reserve — `max(dawn_target, health_cutoff)` — subtracts Axle's promised
kWh, and caps the result at the DNO window (`max_export_kw x window_hours`). It refuses
outright when `balance.import_needed` is already True, because the engine saying "tomorrow needs
a grid import" settles the question whatever the arithmetic says. **That flag is load-bearing
for a specific reason: `battery_at_dawn_kwh` is CLAMPED at the health floor by its producer, so
it cannot go negative to signal a deep shortfall** — reading a shortfall out of a clamped number
is exactly the trap, so the refusal keys on the flag instead.

Also: a refusal now SAYS SO in the decision reason ("Saving Session window, but not exporting:
tomorrow already needs a grid import"), because a session that quietly does nothing is
indistinguishable from a broken feature — which is precisely what v5.80.0 was. The stand-down
latches on the flag, not the clock, so the export stops promptly if the dawn projection turns
against us MID-window, and the hand-back to Self Consumption is CONFIRMED with a retry on the
`vpp_handback_pending` path (the v5.64.0 lesson: never latch on an unconfirmed write).

**Adaptive poll cadence, and it is what makes tonight work.** Hourly normally, every 10 minutes
once a known session starts within 2 hours — because opting in is a tap in the Octopus app that
the owner may make shortly before the window, and the `joined` flag is what gates the export. On
a flat hourly cadence an opt-in at 17:45 would not have been seen until after an 18:00 session
had already started. The imminence test reads only a cached next-start (deliberately NOT filtered
on `joined` — the point is to already be polling often enough to NOTICE the opt-in), so it costs
no extra network.

**A gap found by restarting and looking rather than by any test:** the new
`ACTION_SAVING_SESSION -> "savingSession"` token had no matching `<Option>` on the `currentMode`
List state in Devices.xml, so `currentMode.savingSession` did not exist on the device and a
trigger built on it would never have fired. Added, plus `TestCurrentModeTokensMatchDevicesXml`
in test_config_xml.py which walks every token in ACTION_MODE_TOKEN and asserts an Option exists
(proven to fail by removing the Option again). NB an added Option needs an Indigo CLIENT restart
before the sub-state appears — the documented client XML caching.

The manager cycle does no network I/O for any of this — the hourly poll leaves the joined
windows in `store["saving_sessions_windows"]` and `_saving_session_window()` is a pure read of
that cache.

**Ships OFF** (`savingSessionExport`, default false) — a new behaviour that drives the battery
must not switch itself on for anyone. Read through `plugin_utils.as_bool`, not bare `bool()`;
**NB most of plugin.py still reads checkbox prefs with bare `bool()`, where `bool("false")` is
True — a real latent trap and an estate-wide sweep of its own.**

Tests 903 -> 927. Four sabotages, each asserted to have applied and each restore byte-identical:
disabling the VPP branch so the session steals the window (4 red), removing the `import_needed`
refusal (1), no longer holding back Axle's energy (1), ignoring the reserve floor (3). **A fifth
mutation SURVIVED and was the more useful result** — adding `and not vpp_active` to the session
branch changed nothing, because that branch already sits below the VPP one, so the mutation was
meaningless rather than the test weak. Re-run as a genuine precedence break, it went red.

## v5.80.1 — 03-09-2026

**v5.80.0 SHIPPED SILENTLY DEAD, AND THE WAY IT FAILED IS THE WHOLE ENTRY.**

`get_saving_sessions()` sent `Authorization: JWT <token>`, copying the convention every other
Kraken call in `octopus_api.py` uses. **The backend host does not take that form.** Measured
against the live API today, all three variants:

| header | result |
|---|---|
| `<token>` (raw) | account resolves — `hasJoinedCampaign=True`, 36 joined events |
| `Bearer <token>` | account resolves |
| `JWT <token>` (what shipped) | `account: null`, `errors[].extensions.errorCode = OE-0102` |
| main host + raw token | HTTP 400 — the two hosts are mirror images |

**The failure shape is what made it dangerous.** `events` is PUBLIC and resolved fine, so the
reply was HTTP 200 with a complete 61-event list and only the `account` sub-field errored to
null. `acct = ss.get("account") or {}` then read that as `hasJoinedCampaign = False`, and
`_check_saving_sessions` returns early on exactly that — so the feature would have stayed
silent for ever, with no log line, on a reply that looked entirely healthy. It was released
and reported as working on the strength of a clean plugin start, which is
[[feedback_verify_the_dispatched_path]] in its purest form: the plugin starting proves the
plugin starts.

**I noticed the discrepancy while reading barnybug's client** (he sends the raw token) and
consciously kept the JWT form "for consistency with this codebase". That was the wrong call:
consistency is a property of one host, and this is a different host. The comment at the call
site now says so, with the error code, so nobody harmonises it back.

**Second fix, and the one that matters more: an errored account block is UNKNOWN, never
"not joined".** `get_saving_sessions` now inspects `errors[].path` and returns a FAILURE
(None) with a WARNING naming the error code when the account leg errored or came back null —
so a future auth change surfaces in the log within the hour instead of quietly disabling the
feature. A GENUINE `hasJoinedCampaign: False` still comes through as a real answer, and
`_check_saving_sessions` now logs that once per start rather than returning in silence:
"this account has not joined the campaign, so no session alerts will fire". An absence and a
negative are different facts — [[feedback_absent_state_is_never_a_match]], hit again, this
time in an auth reply rather than a device state.

Live-verified after the restart, through the SHIPPED file: account block resolves,
`hasJoinedCampaign=True`, 36 joined events, signed-up meter point 1591059073620. Tests
897 → 903, and both fixes mutation-tested — reverting the header turns the suite red (1
failure), reverting the guard turns it red (3) — each sabotage asserted to have applied and
each restore verified byte-identical, with `__pycache__` cleared before every run.

## v5.80.0 — 03-09-2026

Octopus Saving Sessions — Phase 1: detect a newly-announced event and Pushover CliveS.
No dispatch change at all; this is visibility only.

**Why Phase 1 and not more.** Checked the account's actual history via the
`barnybug/savingsessions` calculator: across the six sessions since the battery went live
(Mar–Aug 2026), five earned £0.00 and one earned 120 points (15p) — because the battery
already exports close to its own 10-day baseline most evenings, leaving little "extra" for
Saving Sessions to reward. Octopoints are genuinely on top of the normal 12p/kWh export
payment (confirmed against Octopus's own FAQ: "your export tariff won't be affected — only
additional energy exported during the Session will be paid at the Saving Sessions incentive
rate"), but that normal export revenue would be earned at the same flat rate whichever half
hour of the day it went out in — Outgoing isn't time-of-use. So the marginal value of
actively *timing* dispatch around a session is still only the Octopoints bonus, and on this
account's numbers so far that's pennies, not pounds. Not worth arbitrating against the Axle
VPP and bank-first export hold for. Revisit once a winter session or two (historically worth
far more — the best pre-battery session here paid 416 points/£0.52) shows real numbers.

**octopus_api.py** — new `OctopusAPI.get_saving_sessions()`, mirroring the
`get_account_financials()` shape (30-min positive cache, 5-min negative-cache debounce,
serves the last good value through a network blip). Queries `savingSessions { account {
hasJoinedCampaign joinedEvents { eventId } } events { id code startAt endAt
rewardPerKwhInOctoPoints } }` against `KRAKEN_GRAPHQL_BACKEND`
(`api.backend.octopus.energy`) — this query 404s on the main `api.octopus.energy` graphql
host that every other Kraken call in this module uses; the JWT from the existing
`_get_kraken_token()` works unchanged against the backend host, so no new auth step was
needed. Only public reference for this query is the `barnybug/savingsessions` open-source
calculator (github.com/barnybug/savingsessions) — there is no first-party Octopus API doc
for it.

**plugin.py** — `_check_saving_sessions()`, hourly tick (`SAVING_SESSIONS_INTERVAL`).
Compares live events against `store["saving_sessions_notified"]` (persisted in
accumulators.json, not day-scoped — same treatment as storm state, so a restart between
the announcement and the session can't re-send the push), sends one Pushover per
newly-announced future event naming the window and the points/kWh rate, and stays
completely silent when there is nothing new, the account isn't a Saving Sessions member, or
the poll fails. 18 new tests (9 in `test_octopus_api.py`, 9 in `test_plugin.py`) — 897 total,
all green.

## v5.78.1 — 29-08-2026

CONFIGURE DIALOG WAS DEAD FOR FIVE DAYS.

`PAXDialogControllerError -- Field ID separator_dashboard was already used.` Indigo will not
build a dialog containing two `<Field>` elements with the same `id`, and v5.75.0 (24-Aug) added
a second WEB DASHBOARD section that reused three IDs from the existing one:
`separator_dashboard`, `label_dashboard`, `label_dashboard_info`. Every attempt to open
Plugins -> Sigenergy Manager -> Configure failed outright, so NO setting could be changed.

**Why it survived five days and two releases.** Nothing in the plugin reads its own dialog XML —
Indigo's client parses it, at the moment a user opens the dialog. So the failure is invisible to
`python3 -m unittest`, to `ruff`, to a plugin restart, and to every smoke test in the release
ritual. It surfaced only when CliveS went to tick a new setting. Note the wrinkle from the
client-XML-caching rule: a client holding the pre-5.75.0 XML would have opened the dialog fine,
which is another way this hides.

**Fix.** The two sections merged into one, `dashboardHost` folded in after the token fields where
it belongs (it is the same subject), and the duplicate heading deleted rather than renamed —
two "WEB DASHBOARD" headings in one dialog was the real defect, the ID clash was the symptom.

**Guard.** `test_config_xml.py` walks every `*.xml` in the Server Plugin folder and asserts, per
dialog scope (the root for PluginConfig, each `<ConfigUI>` elsewhere):
- every file parses
- no duplicate `Field` id within one dialog
- no `Field` without an id
- every `visibleBindingId` names a field present in the same dialog

That last one is not the bug that was hit, but it is the same family and it fails SILENTLY — a
mistyped binding hides the row instead of raising, so the setting simply never appears. The
suite also asserts the glob matched something, since a glob matching nothing makes the other
four pass vacuously. Mutation-checked 3/3.

Tests 807 -> 812.

---

## v5.78.0 — 29-08-2026

AWAY MODE — a second consumption profile, for the days the house is empty.

**The problem.** `_refresh_consumption_profile_impl` builds the 48-slot profile as a
**cumulative mean over every polling day**, and the accumulators persist across restarts.
That is the right shape for a house someone lives in and the wrong shape for one nobody
does. Six weeks away neither moves the mean far enough to change any decision during the
trip, nor stays out of it afterwards — so the plugin plans the battery for a full house
all holiday and then carries the quiet weeks home for months.

**The measurement, not an estimate.** Octopus half-hourly import, 15-Oct to 28-Nov 2025,
2,160 slots, 45 days. That absence predates the Sigen install, so grid import *was* house
load with nothing in between:

    12.16 kWh/day mean (min 11.4, max 18.3)
    flat 507 W, 1.2x trough to peak, no weekly cycle at all

The occupied default shape has a morning and an evening. Falling back to it for an empty
house invents a peak that is not coming, which is why away gets its own seed rather than
a scale factor on the existing one.

**What was added.**
- `_away_seed_profile(daily_kwh)` — module-level pure function, flat 48 slots, guarded
  coercion (blank / non-numeric / <=0 / >100 all fall back to `AWAY_DAILY_KWH_DEFAULT`).
- `away_profile_watts_sum` / `away_profile_count` — a second accumulator pair, fed only
  while away. `_accumulate_home_profile` picks by `store["away_active"]`.
- `_is_away()` — reads the configured Indigo variable by NAME (per the global rule: a
  name survives a recreate, an id does not). Warns ONCE on a missing variable, because it
  runs every Modbus poll.
- `_refresh_away_state()` — called from the merge immediately BEFORE the accumulate, so a
  transition cannot file a full-house reading against the empty-house profile. Rebuilds
  the live profile on change rather than waiting for the next scheduled refresh.
- `awayMode` state on `batteryManager`, behind the same `in dev.states` guard as
  `currentMode` (a state added this version is unregistered on the first tick after
  restart, and writing it early logs a red line for nothing).
- Config: `awayEnabled` / `awayVariable` / `awayDailyKwh`.

**It fails towards OCCUPIED, and that is the whole design.** The two errors are not
symmetrical. Believing the house is empty when it is not under-imports and leaves the
battery short on a winter evening. Believing it occupied when it is empty buys a little
more than needed, and the existing SOC guard caps that anyway. So a missing variable, a
blank name, a junk value and an exception all return False. This is
`feedback_absent_state_is_never_a_match` applied to a config read: unknown must not
resolve to the interesting branch just because the interesting branch is the new one.

**The weekend uplift is suppressed while away** (`_build_snapshot`). The x1.30 models
people being home on a Saturday. The 45-day measurement has no weekly cycle whatever, so
applying it would invent 30% of Saturday demand and buy for it.

**Persistence is additive.** `away_watts_sum` / `away_count` are new keys in
`home_load_profile.json`; a file written by <=5.77.3 simply lacks them and the away
accumulators start empty. `_load_home_profile` also seeds `away_active` from `_is_away()`
BEFORE the rebuild, so a restart mid-trip does not resume on the occupied profile.

**Sizing, for whoever wonders whether it was worth it.** 3-Dec to 15-Jan is 43 days,
~523 kWh of demand against ~250 kWh of December PV, so ~273 kWh net. Priced against the
real region-F Agile rates for 3-Dec-2025 to 15-Jan-2026: buying nightly costs £31.84
(11.8 p/kWh), buying across a 5-day window costs £19.56 (7.2 p/kWh). **~£12.** Modest, but
the mechanism matters more than the money — a 35 kWh battery against 6.3 kWh/day of net
need is five days of slack, and only a correct profile lets the planner use it.

**Tests.** `test_away_profile.py`, 25 cases: seed flatness and every coercion guard, all
six unhappy paths of `_is_away`, accumulator routing both ways, which profile the refresh
publishes, the persistence round trip including a pre-5.78 file, and the restart-while-away
case. Suite 782 -> 807. **Mutation-checked 4/4** — fail-safe direction flipped, routing
pinned to home, away fallback swapped for the occupied default, and away keys dropped from
the save, each run in a fresh subprocess with `__pycache__` cleared first and the restore
asserted byte-exact.

---

## v5.77.1 — 25-08-2026

THIS FILE. The developer changelog had grown to 2,002 lines at the top of `plugin.py` — a
sixth of an 11,534-line module — so the file opened with its own history rather than with its
imports. Moved here, where it reads better as a document than it ever did as a comment block.

The move is behaviour-neutral and that is proved rather than asserted: the module's AST,
dumped with `include_attributes=False` so line numbers are excluded, is byte-identical either
side of it. An identical dump means only comments changed. Word-diffed as a second check —
the only differences are the date reformat in the headings, one per entry. 748 tests pass,
ruff clean, verified live after a restart.

`plugin.py` 11,534 -> 9,551 lines. The file header stays, with a pointer here.

Structural measurements of the module, and what is worth slimming next, are in
`docs/decomposition-analysis.md` and `docs/decomposition-plan.md`.

## v5.77.0 — 24-08-2026

ONE READER FOR THE SUMMER RESILIENCE FLOOR.  Four call
sites coerced `dawnSocTarget` independently and every one of them
fell back to 10 - a value this plugin refuses.  Save-time
validation rejects anything under 15, and the startup migration
raises a stored value below it, so the only state that could
produce a 10 was a pref that had never been written.  And 10 is
the HEALTH floor this buffer exists to sit above, so the fallback
put the resilience floor exactly on the line it is meant to clear.
All four now go through `_dawn_target_pct()`, fallback 15.  The
migration's own read is deliberately left raw: it has to see the
pre-migration value to know whether to raise it.
Doc corrections that came with it - the README and the repo notes
both claimed a "10% summer floor", which has been 15 since v3.0,
and battery_manager carried a stale "default 10%" comment.
Companion script `openmeteo_battery_optimiser.py` v3.15 was fixed
in the same pass: it mirrored the seasonal rule WITHOUT the
plugin's guard, returning the winter buffer unconditionally where
`_apply_seasonal_override` applies it only when it exceeds the
summer floor.  Harmless at today's 15/20, silently wrong the
moment a dawn target above 20 is set.
Tests 743 -> 748.

## v5.76.0 — 24-08-2026

THE PLUGIN CARRIED A SECOND COPY OF THE DASHBOARDS
ENERGY PAGE.  web_dashboard.py was 2,217 lines, of which about
1,800 were an HTML/JS page held in a Python string - so no
linter, no editor and no `node --check` had ever looked at it,
and an unclosed brace in there was a blank page with no clue
where to start.  It also rendered charts, a calendar, tariff,
cost, period totals and an export-sync table that the Dashboards
plugin already draws from the same JSON.
The page now lives in dashboard.html beside the module, and it
has been cut back to what a fallback view is actually for:
power flow, battery state, live power, the manager's decision
and today's totals.  That is the view you want when IWS is
wedged or the broadband is down, which is the only reason this
server has a page at all.
THE JSON API IS UNCHANGED.  All seven endpoints - status,
history, daily, export-sync, years, calendar, vpp - answer
exactly as before, because Dashboards proxies every one of them.
A new test pins that list so a later tidy-up cannot quietly
break the Energy and Cost pages.
The bundled Chart.js (200 KB) and the /chart.js route went with
the charts.  43 orphaned CSS rules went too, several of them
dead since the flow diagram stopped using chevrons.
scripts/check_dashboard_js.py runs the page's own update() over
a real payload in a stub DOM, so a stale element reference fails
a check instead of blanking half the cards in a browser.
web_dashboard.py 2,217 -> 390 lines. Page 1,835 -> 1,044.
Tests 734 -> 743.

## v5.75.0 — 24-08-2026

THE WEB DASHBOARD ASKED NOBODY FOR ANYTHING.  Since it
was written it bound every network interface on port 8179 and
served the whole energy API - SOC, tariff, VPP, power cuts, the
lot - to any device on the LAN, with no authentication at all.
On a home network that was a known trade.  It stops being one
the moment the port is reached through a tunnel, and that is
where this is heading.
It now binds LOOPBACK by default.  Nothing user-facing changes:
the Dashboards plugin proxies to 127.0.0.1 server-side, so its
Energy and Cost pages carry on exactly as before.  What goes
away is the LAN exposure.
A new "Dashboard access" config setting widens it to the whole
network, and that path REQUIRES a token - the server refuses to
start rather than opening an unauthenticated port, because a
dashboard that fails to start gets noticed and a warning in a
busy log does not.  The token comes from IndigoSecrets
(SIGEN_DASHBOARD_TOKEN), then the config field, then one the
plugin generates and keeps 0600 in its own data folder.  It is
accepted as a Bearer header, an X-Auth-Token header, a ?token=
query string, or the cookie the query string sets, so a browser
only needs it once.  Loopback callers are exempt, which is what
keeps the Dashboards proxy working.
New menu item: Show Web Dashboard Access.
Also adds sigenergy_modbus read_export_limit() (reads back the
commissioned grid export cap 40038; the write side already
existed) so the standalone SigenVPP daemon can share this file
byte for byte instead of carrying a local edit.
Tests 696 -> 734.

## v5.74.0 — 20-08-2026

SHADOW-COMPARES 90% AND 95% DAYTIME PACING WITHOUT
TOUCHING THE INVERTER.  Each solar-overflow evaluation now runs
the same immutable snapshot through a 95% target and totals the
extra early export that the live 90% setting makes available.
At midnight it stores that estimate beside the observed end SOC,
export and post-16:00 import.  It also records the actual import
pattern at both Tracker's daily price and Agile's published
half-hourly prices.  This is a SAME-CONSUMPTION tariff baseline,
not a claim that Agile's battery scheduling would have behaved
identically.  No control setting, charge cap or tariff changes.

## v5.73.0 — 19-08-2026

"PV CURTAILED" WAS AN ALARM ABOUT THE SUN GOING DOWN.
Found checking the device states after the 5.72.3 restart, not
from any symptom. The 16-Aug window (19:00-20:00 UTC =
20:00-21:00 BST) reported `lastVppPvStatus: curtailed` with PV
at 0 W across all 48 snapshots. SUNSET WAS 20:40 BST, so the
window opened 41 min before it and ran 19 min past — genuinely
daytime by the dawn/dusk gate, which is why v5.56.0's dark-window
branch did not catch it. But the FORECAST for that hour was
153 W falling to zero: it had predicted the zero. And
curtailment was IMPOSSIBLE anyway — in 0x05 with charge pinned
at 0 it needs PV above house + export cap, about 5 kW, at five
degrees of elevation.
THE RULE WAS RIGHT AND ITS SCOPE WAS WRONG. v5.66.0 established
that only a peak which never lifts means a shut-down MPPT; that
holds mid-day and says nothing at the edge of the solar day,
where nothing was coming anyway. So the verdict now asks what
was EXPECTED: new pure `_forecast_peak_w_for_window` reads the
hourly p50 buckets over the window's LOCAL hours, and a peak
below PV_EXPECTED_MIN_W (200 W — 1.4% of nameplate, where a
zero cannot be told from cloud) reads "n/a (no PV expected)".
A substantial forecast still condemns a zero, which is the
15-Jun-2026 failure this check exists for and is pinned as the
control. An UNKNOWN forecast changes NOTHING — it must never
quietly excuse a real fault — and that fallback is pinned too.
Raw p50 deliberately, not bias-corrected: raw runs high here
(0.883), so it over-states the expectation and makes the guard
LESS willing to excuse, which is the safe direction.
Reporting only — nothing on the drive path changed. Tests
686 -> 696, 3/3 mutants killed with each mutation asserted to
have applied.

## v5.72.3 — 19-08-2026

THE HEADLINE COULD FALL BEHIND ITS OWN ROWS, SILENTLY.
Straight after the 5.72.2 email import, the ledger reported a
lifetime of GBP 87.60 while the transactions listed underneath
it summed to GBP 91.47 — because `lifetime_gbp` and
`available_gbp` are Axle's stored figures and a payload carrying
transactions and NO balance (exactly what a settlement email
gives you) never refreshes them. CliveS's screenshot of the
Balance page settled it: GBP 91.47, agreeing with the ROWS.
Two numbers in one ledger disagreeing, with nothing saying so.
`summarise` now totals the rows and reports the gap as
`balance_behind_gbp` — DETECTED AND REPORTED, never quietly
corrected, because publishing our arithmetic as Axle's settled
figure reads as truth while being a computation, which is the
worse fault and the rule the whole axle side turns on. The menu
says which figure is which and what to do; `/api/status` carries
the flag. SKIPPED once any withdrawal row exists: a withdrawal
cuts the available balance without cutting lifetime earnings and
this estate has never seen one, so the sign convention is
UNVERIFIED and a check built on a guess is worse than none.
THE FIXTURE CAUGHT MY OWN ASSUMPTION: the test payload is a
deliberate 5-row SUBSET of the real 17 kept beside the real
GBP 87.60 balance, so it never agreed with itself and the first
three cases failed. The tests were wrong, not the code.
Tests 50 -> 56, 2/2 mutants killed with each mutation asserted
to have applied. LIVE: the real balance imported from the
account page, so lifetime, available and the rows all read
GBP 91.47 and the flag is clear.

## v5.72.2 — 19-08-2026

A HAND-ENTERED SETTLEMENT ROW CAN NO LONGER DOUBLE-
COUNT. Axle email the result of an event days before their
account page catches up — the 16-Aug window arrived by email on
the 19th (GBP 3.87 for 3.87 kWh) while the account still showed
nothing — so entering the figure by hand is the sensible move.
But `import_axle_payload` deduped on `transaction_id` ALONE, and
a hand row cannot know the id Axle will later assign it, so the
authentic row would have landed BESIDE the stand-in and the
event would have been counted twice in the lifetime total for
ever, with nothing to notice.
A grid event settles once, so its WINDOW identifies it as surely
as its id does: a new flex-event row whose window is already
held now REPLACES the row that holds it. Deliberately flex
events only — the monthly floor payment and the referral credit
carry no window, and two top-ups in one month are two genuine
payments, which is its own test.
The 16-Aug row is now in the live ledger as email-sourced, so
the next account import quietly corrects it. 4 tests (50 in the
module); mutation-tested with the guard removed, and the
mutation was ASSERTED to have applied before its result was
believed.

## v5.72.1 — 18-08-2026

THE LEDGER NOW RECORDS THE IN-WINDOW EXPORT TOO, and
it changes what the comparison means. CliveS asked why 11-Aug
showed 7.05 kWh against an hour whose DNO cap allows 4 — a fair
question, because the figure being recorded was the counter
delta across the WHOLE driven run. The driver deliberately runs
T-2min to end+2min, so that is always wider than the paid
window; on 11-Aug it was wider by FORTY-FIVE MINUTES, because
the window never stopped (the v5.61.1 bug).
MEASURED across every event: integrating grid export strictly
INSIDE the window gives 4.00 kWh on every one-hour event and
8.01 on the two-hour one — the cap times the duration, as it
must be — and the gap against Axle collapses from a scattered
0.4-3.2 to a consistent +0.162 to +0.197. THAT is the baseline,
and it only becomes visible once both sides cover the same hour.
New pure `integrate_window_kwh`; both figures stored; the
baseline is claimed ONLY when ours is in-window, and an
over-run is reported separately rather than as a shortfall.
Historical rows re-seeded from the existing JSONLs. 13 tests
(46 in the module), and the first three failures were all my own
FIXTURES — a sample cadence that never reached the window end,
a step change a trapezoid cannot resolve, and a case still
asserting the old contract.

## v5.72.0 — 18-08-2026

THE VPP EARNINGS LEDGER — WHAT AXLE ACTUALLY PAID.
The plugin has always known what it EXPORTED and never what it
EARNED, and the two are different numbers. Axle settles on
`flex_kwh`, the change in energy flow measured against a
baseline, so a window our snapshots record as 4.23 kWh settles
at 3.838. Every previous session read that ~0.4 kWh gap as a
shortfall to be chased; it is not a deduction at all, it is a
different measurement, and the only place it exists is the Axle
account. Confirmed 18-08-2026 by reading the account payload:
credit_pence is exactly |flex_kwh| x 100, no deduction anywhere.
New vpp_ledger.py holds two sources side by side and never
merges them — `axle` (settled truth, imported wholesale) and
`local` (our own per-event figure, an ESTIMATE and labelled so).
THE RULE: an event with no Axle row is PENDING, not zero.
Settlement runs days behind — 16-Aug was still unsettled on the
18th — so a GBP 0.00 tile would report a loss that never
happened. paid_gbp stays None until Axle says otherwise, and a
genuine zero (20-Apr settled at 0.000 kWh) arrives as a real
transaction and is a different thing. Earnings states are
Strings for the same reason: a Float cannot say "pending".
Also: /api/vpp (ledger + next window), a machine-readable
next_event block on /api/status so a page can count down
without parsing English, api_status carried alongside it (a
dead feed and a quiet fortnight look identical otherwise —
that is how a revoked token hid for six weeks), two menu items,
and 33 contract tests weighted at absence.
Axle rows arrive through ONE importer reading a drop file, so
a future cookie fetch or a widened token changes nothing else.

## v5.71.1 — 15-08-2026

A QUEUED IMPORT COULD FIRE INSIDE A PAID EXPORT WINDOW.
Found by asking what Predbat's #4520 ("boost the import rate during
an Axle export event, not just export") lands on us, not from any
symptom. Their fix is planner pricing — an export event pays a
premium, so charging through it forfeits that premium and their
planner had no cost for it. The same opportunity cost is ours.
The manager itself was already safe: BatteryManager.evaluate() is
first-match-wins and step 1 is the VPP override, sitting ABOVE the
import branch, and _verify_ems_registers deliberately stands down
through PRE_CHARGING/ACTIVE. But _check_scheduled_import_impl runs
from the 10s TICK and gated only on `manager_paused` — it fired on
the clock alone. The manager evaluate that would retract the queue
runs on the ~15-MINUTE cycle, and that retraction is skipped once
an import is already running. So a schedule armed for a time inside
a window started Charge Grid First at up to inverterMaxKw
mid-export: buying at the import rate through the hour we are paid
to sell, with the verify loop standing down and ACTION_VPP_EXPORT
only re-driving the export (it never clears import_active, so the
store then claimed import AND export together).
Now gated on vpp_state, mirroring the manager_paused gate right
above it, and HELD rather than cancelled — the battery still wants
that charge, so it fires once the window closes; a gate that
silently became an exclusion would be the worse fault.
PRE_CHARGING is included (the plugin is already grid-charging to
its own target there, and a second charge command with its own
cutoff would fight it). ANNOUNCED is deliberately NOT — that can be
hours ahead, and charging BEFORE a window is the arbitrage this
plugin exists to do.
actionForceGridImport carried the same fault plus one of its own:
it sets export_active False beneath a state machine still driving
the export. REFUSED there rather than held, because a person is
behind that one and deserves an answer; pause the manager to
override.
LIKELIHOOD WAS LOW AND IS ABOUT TO RISE: on Tracker the deferral
path rarely arms (no cheap window) and events are evening while
imports are overnight — but CliveS leaves Tracker in October, and
on a time-of-use tariff the queue arms nightly. The midnight
deferral can arm ~8 hours ahead.
Tests 620 -> 630; 6 of the 10 verified FAILING against 5.71.0, the
other 4 deliberate both-sides guards (announced still fires, idle
still fires, an absent vpp_state is not a permanent block, and the
manual action still works when idle).

## v5.71.0 — 13-08-2026

THE REAL GRID VOLTAGE — 252.21 V, and the ceiling is
253.0. Register 31000 is the 230 V NAMEPLATE; the measurement
is 31011 (U32, gain 100), found by reading the phase-voltage
table in the official protocol after the CLOUD reported an
actual 251.34 V that no local register was showing. UK
statutory range is 230 V +10%/-6% = 216.2 to 253.0, and an
inverter MUST curtail or disconnect above the ceiling — so a
high reading here is lost export revenue, and the cause is the
DNO's network, not anything in this house. Sitting 0.8 V under
the limit is worth knowing about. Also reads 31017 phase
current. Both are Number states so the SQL Logger charts them:
the useful question is not "what is it now" but "how often does
it touch 253", which is a week of history rather than a
reading. 0xFFFFFFFF is this firmware's "not applicable" for an
unused phase and is rejected — taken at face value it decodes
to 42,949,672.95 V. /api/status `grid_quality` gains voltage,
current and both statutory limits so a dashboard never has to
hardcode them.

## v5.70.0 — 13-08-2026

THE CONFIGURED BATTERY CAPACITY IS WRONG, AND THE
DASHBOARDS WOULD NOT HAVE FOLLOWED THE FIX. Two findings.
(1) `/api/status` published the module CONSTANT
(BATTERY_CAPACITY_KWH) while every control path — the 24h
balance, the dawn reserve, flood prevention — reads the
`batteryCapacityKwh` PREF with the constant only as a
fallback. They agree today at 35.04 purely BY COINCIDENCE, so
the moment the pref is corrected the dashboards would have
silently disagreed with the battery logic, with nothing to
show it. Status now reads the pref, like everything else.
(2) 35.04 is not the right number. The pack's NAMEPLATE is
36.16 kWh, agreed to the decimal by THREE independent sources
— plant register 30083, inverter register 30548 (both newly
read, gain 100) and the Sigenergy cloud's own systems list.
But rated capacity is not what a SOC percentage converts at,
so it was MEASURED instead, from six days of logged history:
ten clean runs where only one energy counter moved give an
implied capacity of 35.33 kWh from discharge and 35.85 from
charge. Those BRACKET the truth — metered charge over-states
(round-trip losses) and metered discharge under-states — so
the real figure is ~35.6 kWh, and the bracket is the evidence
rather than a single reading. Configured 35.04 therefore
under-states stored energy by ~1.6%; the nameplate would
over-state it by the same. `rated_capacity_kwh` is now
published alongside so the two can never quietly drift again.
THE PREF ITSELF IS CliveS's TO SET — it feeds battery control
decisions, so this release exposes the evidence and changes no
behaviour. (The history query needed FORWARD-FILL to work at
all: the SQL Logger writes only CHANGED values, so a row
carrying a new SOC usually has null counters, and the first
pass — which required all three columns present — found ZERO
runs in six days. The documented sparse-row trap, walked into
in person.)

## v5.69.0 — 13-08-2026

READ THE DOCUMENTATION — five registers NAMED, and a
correction. CliveS showed the mySigen app listing all four
packs' SOC and asked whether I had looked at the docs and API.
I had not: v5.68.0 concluded per-pack data "does not exist"
from a probe alone, when what a probe can show is only "not at
the addresses I tried". Worse, I had ABANDONED the most
promising line — separate slave IDs for the packs — because
the scan was slow, and then wrote absence as fact.
THE CONCLUSION SURVIVES, on far better evidence. The official
Sigenergy Modbus Protocol V2.7 PDF contains exactly TWO SoC
registers, 30014 (plant) and 30601 (inverter), both aggregate,
and no per-pack temperature at all; the most complete
community implementation (sigenergy2mqtt, ~2200 lines of
sensor definitions) exposes the PACK COUNT and no per-pack
value; and slaves 2-5 do not answer. So per-pack SOC is NOT on
the local Modbus interface — the app reads it from Sigenergy's
CLOUD, which is a different channel with a richer model.
(Fetching the spec needs a browser User-Agent; a bare curl
gets a "Blocked" HTML page, same trap as the Indigo docs.)
WHAT THE DOCS PAID FOR: five registers this plugin had probed
but could not identify are now named and four are shipped —
31003 [PCS] internal temperature (the inverter's OWN
temperature, 58.9 degC live, and nothing in the estate was
watching it), 31037 insulation resistance (a SAFETY reading:
falling means moisture ingress or damaged DC cable), 31024
PACK count (so `_pack_count` now ASKS THE HARDWARE instead of
dividing capacity by an assumed 8.76 kWh module), and 30605
Alarm1 (reported as raised/clear — the Appendix 2 decode is
not carried, and inventing a description for a code we cannot
name would be worse than saying "something is raised").
THE DOCS ALSO GRADED v5.68.0'S GUESSWORK, and it held: 31000
and 31001 are indeed "Rated grid voltage" and "Rated grid
frequency" while 31002 is the live "Grid frequency" — exactly
what the sampling had concluded from the fact that only 31002
moved. A register that never moves is a nameplate, not a
reading, and that test cost 80 seconds.

## v5.68.0 — 13-08-2026

WHAT CAN HONESTLY BE SAID ABOUT THE FOUR BATTERY
PACKS, PLUS GRID FREQUENCY. Asked for per-pack SOC and
temperature, the answer had to start with a measurement: this
inverter DOES NOT EXPOSE THEM. Probed register by register on
13-08-2026 across the inverter space (30560-31400) and the
plant battery area — every battery figure is an aggregate
(SOC, SOH, average cell voltage) or a max/min ACROSS clusters.
Nothing per-pack exists at any address, and separate slave IDs
do not answer at all. A register in the spec is not a register
on the hardware, and neither is one you wish for.
BUT max, min AND mean over N identical packs still BOUND the
distribution: the other N-2 must average (mean*N - max - min)
/ (N-2), so whichever end sits furthest from that middle is
the odd one out. New pure `analyse_pack_balance` does exactly
that and returns "even" / "one_hot" / "one_cold" — a genuine
per-pack signal recovered from aggregates, and precisely what
an average is designed to hide. On the live reading (34.9 avg,
39.0 max, 32.4 min, 4 packs) the middle two must average 34.1,
putting ONE PACK 4.9 degC clear of its siblings while the cold
end is only 1.7 off — one pack running hot, which nothing in
the system would otherwise show. It REFUSES to answer when the
figures cannot support it: a middle falling outside [min, max]
means the mean is not the mean of these packs, so every
conclusion from it would be fiction. (That guard earned itself
immediately by rejecting an invented test fixture of mine.)
Only claims an outlier at >= 2 degC AND >= twice the other
gap — one warm pack in a tight group is not news.
NEW STATES: `packTempSpreadC` (Number, so the SQL Logger
charts it — one reading says little, a spread widening over
weeks is a pack going off), `packBalance` (List enum, so a
trigger can fire on "one pack running hot"), and
`gridFrequencyHz`. Frequency is register 31002, CONFIRMED by
sampling rather than assumed: it drifted 49.98 -> 49.95 ->
49.96 Hz over 80 s, which nothing but mains frequency does,
while its neighbours sat at exactly 5000 and 2300 throughout —
those are the NOMINAL 50.00 Hz and 230.0 V ratings, and a
register that never moves is a nameplate, not a reading. It
belongs beside the VPP work: a grid event is ultimately a
frequency problem, so this is the quantity the whole scheme
exists to defend. /api/status `battery` gains the temperatures,
cell voltage, SOH, pack_count and pack_balance; new
`grid_quality` block carries the frequency. Pack count is
DERIVED (capacity / 8.76 kWh module = 4 here), never painted
in, with `batteryPackCount` / `batteryModuleKwh` prefs for a
stack that differs; an unknown count simply skips the
inference. The whole balance block is wrapped on the state
path — it carries SOC, and an advisory figure must never cost
it. Tests 611 -> 620.

## v5.67.0 — 13-08-2026

PER-PV-STRING READINGS. The inverter's 31025 block
(probed live on this SigenStor 10 kW: [string_count, mppt_count,
V1, I1 .. V4, I4], V gain 10, I gain 100; four powers summed to
the plant PV total +6.8%, DC vs AC) is read once per poll cycle
as a single transaction (sigenergy_modbus v1.8: _read_block_u16 +
pure decode_pv_strings; NON-critical, and an absent-latch stops
an install whose firmware lacks the block paying a failing read
every cycle for ever — the 50000 pre-heat lesson). Each string
lands as inverter device states pv1Volts/Amps/Watts..pv4 (Number,
so the SQL Logger charts each string's day — that history is what
will NAME the strings: East peaks mid-morning, West in the
evening) and in /api/status solar.strings as [{n, label, v, a,
w, kwp?}]. Labels from the new pvStringLabels pref ("South:4.275,
East:4.275, ..." — kWp optional, for capacity-scaled bars),
default PV1..PVn until a clear day names the curves; parsing is
the pure _parse_pv_string_labels. States written only for
strings actually reported — a transient block failure must not
chart a phantom string dropout. Consumed by Dashboards v2.78.0
(per-string strip + the actual-vs-expected solar progress chart,
which itself needs no SEM change).

## v5.66.0 — 12-08-2026

PV FELL TO ZERO MID-WINDOW AND WAS REPORTED AS CURTAILED.
The PV verdict in _summarise_vpp_event was a bare `min_pv_w > 100`
under the v5.56.0 daylight gate, so ANY daytime window whose PV
touched zero at any point failed the test. The first two-hour
event (12-Aug-2026, 18:00-20:00 BST) hit it: PV ran 1454 W at the
start, spiked to 1757 W, then collapsed to 0 W from 18:58 and
stayed there, and a textbook 8.26 kWh export was summarised
"curtailed". Curtailment was not merely absent but IMPOSSIBLE —
in mode 0x05 with charge pinned at 0, PV can only be curtailed
once it exceeds house + the export cap (~4.99 kW that evening)
and it peaked at 1.76 kW. THE CAUSE WAS EXTERNAL, NOT US, AND
NOT SUNSET: sunset was 20:47:47, i.e. 48 min AFTER the window
closed and 1h50m after PV hit zero (a partial solar eclipse that
evening, plus cloud — the 551 W -> 1757 W recovery inside five
minutes is cloud, not an astronomical curve). Verdict now reads
max_pv_w: only a peak that never lifts means the MPPT was shut
down, whereas a zero MINIMUM says nothing at all, because PV can
reach zero mid-window for reasons that have nothing to do with
the inverter. min_pv_w is still reported (a real reading, just
not the test), the summary quotes BOTH, and the peak is recorded
in a new `lastVppMaxPvW` state so the number the verdict rests on
is durable rather than only in a log line.

## v5.65.0 — 12-08-2026

DEEP REVIEW #4, BATCH A1 — the control-and-safety highs.
A 16-lens adversarially-verified review (187 confirmed findings, 0 critical,
13 high) against v5.60.1/5.64.0. This batch ships the five that can mis-drive
the hardware or spend money on THIS install, plus the test-integrity fix that
protects every batch after it. Full register in
~/.claude/plans/sem-deep-review-2026-08-10/.

* TEST INTEGRITY FIRST. `unittest.main()` calls sys.exit(), so any class
  defined BELOW the `if __name__ == "__main__"` block was unreachable on a
  direct run. Measured: test_plugin.py ran 223 tests directly against 263
  imported, test_openmeteo_forecast 36 against 41 — 45 tests invisible,
  INCLUDING every VPP guard added in v5.61.1 through v5.64.0. CI uses
  discover() so it never noticed, while the repo's own documented command is
  the direct one. Blocks moved to the end of three files, plus a new
  TestNoTestsStrandedBelowMain that walks every test_*.py with ast. It caught
  me making the identical mistake twice while adding this batch's own tests.
* SCHEDULED IMPORT COULD OUTLIVE THE DECISION THAT QUEUED IT (found
  independently by two lenses — a changed decision, and pause).
  ACTION_SCHEDULE_IMPORT stored a time that nothing cleared but the firing
  itself, so when a later evaluate stopped wanting the import the stored time
  survived, fired with no fresh check, and the anti-oscillation guard then HELD
  the unwanted import until the stored target SOC was reached. On Tracker the
  midnight-deferral path arms this ~8 hours ahead. Now retracted centrally in
  _act_on_decision on any other action (never while an import is running —
  that is STOP_IMPORT's job), and cancelled by _disengage_to_safe_baseline so
  pause and sleep cancel the drive they had queued. _check_scheduled_import
  runs OUTSIDE the paused gate, so it also refuses directly while paused.
* A 0.0 TARGET BECAME A 100% CHARGE CUTOFF. `(target_soc or 100.0) + 3.0` —
  and 0.0 is exactly what an intervening completed import leaves behind, so the
  backstop meant to STOP a runaway import became permission for one. The firing
  path also hardcoded 10000 W; it now follows inverterMaxKw like START_IMPORT.
* THE CONTROL PATH USED THE DISPLAY-ONLY BIAS SCALAR. _calculate_24h_balance
  scaled remaining solar by `biasFactor`, the global kWh-weighted number whose
  own source comment reads "display only", while corrected_today/tomorrow in
  the SAME balance used the per-day BAND factor — one energy balance, two
  scales. Live bands 12-Aug: the 30 kWh band was 1.199 against a global 0.885,
  35% apart on exactly the marginal days where the 1.0 kWh overflow threshold
  sits, and within 3% on a bright day, which is why spot checks saw nothing.
  New snapshot field bias_factor_today; bias_factor kept for display.
* set_self_consumption() HARDCODED 10000 W for both limit registers — right on
  this 10 kW inverter, a silent discharge cap on any other for a whole verify
  interval after every return to self-consumption. The rating now lives on the
  modbus object (SigenergyModbus.inverter_max_w, fed from inverterMaxKw and
  refreshed on every prefs save), and night_export/daytime_export default to
  it, so a future mode method cannot reintroduce the hardcode by omission.
* daytime_export() COMMITTED MODE 0x05 BEFORE PINNING CHARGE=0 — the exact
  greedy-charge window the method exists to close, entered from
  self-consumption where the charge limit sits at inverter max. With 0x05 live
  and charge open, high PV banks into the battery instead of going to grid, so
  a paid VPP window silently exports nothing while the mode register reads a
  perfectly correct 0x05. Limits are now written BEFORE the mode commit.
* STORM WATCH COULD NOT SEE HALF THE WARNINGS. `_` is a word character, so the
  `\b` hazard boundaries could never fire beside it and MeteoAlarm's compound
  tokens (snow_ice, rain_flood, coastal_flooding) were silently discarded —
  measured against the shipped regex, while the hyphenated form matched. This
  is the power-cut reserve feature: a missed warning means the 50% reserve
  never engages and check_storm_level still returns a confident all-clear.
  Separators are now collapsed before matching, "flood" joins "flooding", the
  word boundaries are KEPT (notice/training/predicted still rejected, pinned by
  tests), and ignored events are NAMED in the all-clear reason so "no warnings"
  can never again mean "warnings I could not read".
* Tests 527 -> 588, every fix mutation-tested (mutant made to fail, restored).
  NB one fixture bug caught only by a deliberate assertGreater(base, 0) guard:
  a hand-rolled P50 shape summed to zero, and without that guard both
  assertions would have compared 0.0 to 0.0 and passed against the bug.

## v5.64.0 — 12-08-2026

THE HAND-BACK AT WINDOW END WAS NEVER CONFIRMED.
_end_vpp_export discarded set_self_consumption()'s return value, under a bare
`if self.modbus:` with no .connected check, so a rejected/clamped/dead-socket
write left the state machine IDLE while the inverter stayed in 0x05/0x06 still
selling the battery. Only _verify_ems_registers caught it, on the ~15-MINUTE
manager cycle — up to ~1.25 kWh to the grid outside the paid window, unpaid and
silent (one generic Modbus ERROR, nothing VPP-tagged). Now confirmed, retried
once immediately, WARNs in VPP terms, and sets vpp_handback_pending so the 10s
tick re-asserts the safe baseline (see _retry_vpp_handback). IDLE is still
reached regardless — a hand-back that wedged in ACTIVE would be worse than the
bug (Predbat #4477 records exactly that latch). Prompted by Predbat's #4477,
whose own bug is NOT ours: they drive the Sigenergy through the cloud gateway
and must offboard a platform authorisation; we self-drive over local Modbus.
The transferable lesson is only "latch on success, not attempt".

## v5.63.0 — 11-08-2026

THE POST-EVENT SUMMARY COULD REPORT ANOTHER EVENT'S READINGS.
_summarise_vpp_event appended EVERY snapshot record in the file with no
check that it belonged to the window being summarised — and files DO
hold foreign snapshots: tonight's over-running window wrote 31 of them
into the NEXT event's file at elapsed -1288 min. v5.61.1 stopped that at
source, but the summariser had no defence of its own, so tomorrow's
2-hour event would have had last night's peak grid export, min PV, mode
list and driver folded into its report. A confidently wrong report is
worse than a missing one, and this one would have been read as fact.
New pure `_snapshot_in_window(rec, event, slack_mins=15)`: the driver
runs T-2min to end+2min, so the bound is the window plus a generous 15
min either side — a legitimate lead/trail sample is always kept while a
different day's is rejected. An UNKNOWN elapsed or duration is KEPT, not
dropped: silently shrinking the summary is the worse error. Foreign rows
are COUNTED and WARNed, never discarded quietly, because their presence
means something upstream filed them wrongly. The live 12-Aug file was
also cleaned by hand (31 removed, .bak-polluted kept).
**AND A NEAR-MISS WORTH THE ENTRY ON ITS OWN**: the first cut inserted
the new module-level helper INSIDE the class body. `py_compile` PASSED —
the file is valid Python — but an unindented `def` TERMINATES the class,
so `_summarise_vpp_event` and every method below it became nested
functions and `Plugin` silently lost 100+ methods. Nothing would have
failed until the plugin called one at runtime. A SYNTAX CHECK IS NOT A
STRUCTURE CHECK. New TestPluginClassStructure asserts, via ast, that the
core methods really are methods of Plugin and that the class still holds
the bulk of the code — so a dedent mid-class can never ship silently.
Suite 547 -> 555.

## v5.62.0 — 11-08-2026

A SECOND, INDEPENDENT GUARD ON AN OVER-RUNNING VPP EXPORT.
v5.61.1 fixed the cause that fired tonight, but not the shape of the
risk: an active window is ended by exactly ONE path — _poll_vpp ->
_apply_vpp_event — while the manager re-drives ACTION_VPP_EXPORT every
60 s from the `vpp_active` BOOLEAN ALONE and never looks at the clock.
So every other route that stops that poll reaching its end test lands in
exactly the same place as tonight: 4 kW out of the battery against a 1%
discharge floor, for ever. Audited and real: `axleEnabled` unticked
mid-window returns at the first line of _poll_vpp; a cleared token makes
self.axle None and does the same; a raise before the ACTIVE branch skips
the test every tick; and the VPP tick task dying while the manager lives
leaves the manager happily re-asserting the export. NONE of these fired
tonight — they are simply all still open, which is what "can it happen
again" actually asks. New _check_vpp_overrun() runs at the TOP of
_evaluate_manager_impl: manager cadence, no network, no prefs, no Axle,
so it shares no dependency with the path it backs up. It force-ends
through _end_vpp_export — the same path the poll uses, so summary, JSONL
and state machine all land normally — and WARNs naming the overshoot,
because reaching that line means the primary path failed. Conservative by
construction: it acts only past our OWN STORED end + 15 min (the poll
stops at end+2min on a 60 s cadence, so it can never truncate a live
window), a missing or unparseable end time does NOTHING rather than
guess, and the whole body is wrapped so the guard can never break the
evaluate it protects. 8 tests incl. tonight's actual 45-min overshoot;
all 8 error against 5.61.1 because the method does not exist there.
Suite 539 -> 547.

## v5.61.1 — 11-08-2026

A VPP WINDOW NEVER STOPPED — the export ran 45 min past the
end and would have run for another 21 hours. Axle publish the NEXT event
within a minute of one finishing, and the VPP_ACTIVE branch of
_apply_vpp_event judged the stop against the event the API had JUST
RETURNED rather than our own stored window. So the test became
"now >= TOMORROW's end + 2min", false until 18:02 the following day, and
the plugin carried on self-driving 4 kW out of the battery with the
discharge cutoff at the 1% health floor and nothing left to halt it.
LIVE-HIT tonight: the 19:30-20:30 BST window was still exporting at
21:15, SOC 99% -> 73%, ~2.9 kWh sold at 12p that the house wanted at
26p, and on that trajectory the pack would have been flat by ~00:30 and
the house importing overnight. The tell was in the JSONL — snapshots
were landing in the 12-Aug file at elapsed MINUS 1288 minutes.
The end is now judged against self.store["vpp_event"], and the snapshot
is written against it too so the readings stay in the running event's
file. The `event is None` branch has always done exactly this and even
carries a comment explaining why ("we self-drive on our OWN stored
window"); this branch simply never got the same care. A future event
returned mid-window is picked up on the next poll once the transition
has put us back in IDLE. 4 tests, the load-bearing one verified FAILING
against 5.61.0 (0 calls where 1 is required); suite 535 -> 539.

## v5.61.0 — 11-08-2026

THE NINE kWh VARIABLES HAD NO WRITER ANYWHERE. elec_/gas_/
export_ today/yesterday/month kWh lost their writer when the Octopus
consumption script was retired (12-Apr-2026) and were never picked up
by the v5.41.0 revival, which took the RATES and COSTS only. So they sat
frozen next to live money: export_today_kwh read 0.000 beside an
export_today_revenue_gbp of GBP 2.12 (17.69 kWh at 12p), and
gas_yesterday_kwh still held the 46 kWh April figure that caused a scare.
Measured before touching anything: no writer in either script folder or
any plugin bundle, no trigger/schedule/action-group reference, no
dashboard reader, and no hard-coded id — dead in every direction.
They are now published from the SAME card the costs come from
(_wh_build_card / _wh_card_from_row / the period aggregate all carry the
kWh they were priced on), so the pair cannot contradict each other again.
The settled card deliberately publishes import_kwh_octo, NOT
grid_import_kwh — the Octopus figure is what the settle step billed, and
the Sigen CT figure differs. An unknown value leaves the variable ALONE
rather than writing a confident 0.000, since a fabricated measurement is
worse than a stale one. Window kWh accumulate over exactly the rows the
window's money covers, so a day skipped for want of a rate contributes
neither. Suite 527 -> 535; 5 of the 8 new cases verified FAILING against
5.60.1, the other 3 deliberate both-sides guards.

## v5.60.1 — 08-08-2026

REQUIRED Info.plist KEY. `CFBundleURLTypes` was PRESENT but
EMPTY, so the plugin shipped without the support URL that becomes its
"About" menu item — one of the SIX keys the official Developer's Guide lists as
required. An empty array satisfies "key exists" while giving users nowhere to go,
which is why an earlier sweep that only looked for a MISSING key passed it. Found
by an estate check auditing the VALUE rather than the key's presence.
No plugin logic changed.

## v5.60.0 — 08-08-2026

INTELLIGENT OCTOPUS GO WAS UNRECOGNISABLE, AND THE GO
WINDOW HAD BEEN AN HOUR OUT SINCE v5.47.0. Four defects on the Go/IOG path,
all found by asking a plain question: does the plugin actually support the
tariff the house is switching to next spring?
  (1) DETECTION. Every live Intelligent Go product is `INTELLI-FIX-*` and Go
      12M Fixed is `GO-FIX-*`; the prefix table only knew `INTELLI-VAR` /
      `INTELLI-GO` / `GO-VAR`. Both fell through to TARIFF_UNKNOWN, whose
      planner branch imports AT ONCE at half inverter power — on IOG that is
      buying at 32.4p instead of waiting for the 8p window. Prefixes updated;
      an unrecognised tariff now logs a WARNING naming the product code
      instead of failing silently.
  (2) IGO/IFLUX RATES WERE NEVER FETCHED. `get_all_monitored_rates` looped
      over Go and Flux only, so even with detection fixed the active tariff's
      cheap window came back None and `_plan_tou_import` took its "cheap
      window unavailable, importing now" branch at 10 kW. Same shape as the
      v5.59.0 agile_slots bug: two correct halves, never joined.
  (3) THE GO WINDOW WAS WRONG. Stored 23:30-04:30 since v5.47.0; the product
      is 00:30-05:30 LOCAL. Verified against both a GMT day (2026-01-14) and
      a BST day (2026-08-07): the window is fixed in local time and the UTC
      timestamps move with the clocks, so the earlier "fix" read UTC as local.
      Cost: an hour bought at the ~30p day rate and an hour of 8.5p missed,
      every night, all year.
  (4) THE ROOT CAUSE OF (3) IS HARDCODING. The window is now DERIVED from the
      live rates (`_derive_cheap_window`), with TARIFF_WINDOWS as fallback and
      a WARNING when the two disagree. It refuses to guess for a flat, dynamic
      (Agile) or multi-window (Cosy) tariff, because a wrong window silently
      buys at peak and is worse than a missing one.
A first attempt at (4) assumed the Agile half-hourly shape and returned None
for every real time-of-use tariff — its unit tests passed because the fixture
was built the same wrong way. Only running it against the live API found it;
the fixtures now mirror what Octopus really serves (spans with valid_to).
13 tests, 12 verified FAILING against 5.59.0; suite 512 -> 527. Live-verified
against real Octopus data for Go/Go-Fixed/IOG/Flux/Agile/Cosy/Tracker.

## v5.59.0 — 08-08-2026

AGILE SUPPORT WAS A FACADE — NOW WIRED UP. The manager
has had a full Agile planner (_plan_agile_import: pick the cheapest half-hour
slot before dawn, with a round-trip break-even gate) since v5.44.0, and
octopus_api has had get_agile_rates() to fetch those slots. Nothing ever
joined the two: no code path wrote an "agile_slots" key into the rates dict,
so plugin._build_tariff_data's `rates.get("agile_slots", [])` was ALWAYS
empty, get_agile_rates() had ZERO callers, and the planner fell every time
into its no-rates branch — "importing now" at 10 kW, at whatever the price
happened to be. On Agile that can be the 38p evening peak, i.e. the single
worst moment to buy. Both halves were individually correct and individually
tested, which is exactly why it stayed hidden.
get_all_monitored_rates now fetches today AND tomorrow's slots when Agile is
the active tariff (dawn is tomorrow morning, so today alone can never cover
the decision) and publishes them under "agile_slots"; a failed fetch for one
day no longer loses the other. The tariff device's today_p also stops
reporting the TRACKER rate while on Agile and reports the live half-hour slot
instead. 5 regression tests assert the JOIN, not either half — verified to
FAIL against v5.58.0. Found while pricing a no-EV winter tariff switch, where
Agile is the only time-of-use tariff this house is eligible for.

## v5.58.0 — 07-08-2026

LOW-SOC HEADS-UP BEFORE A VPP WINDOW. Pre-charge has
always compared SOC against what the window needs, and on a shortfall it
logged ONE line and did nothing else — so the first anyone knew of an
under-delivered event was the settlement figure days later. That was a fair
trade while events carried 18-24 h of notice and needed a manual opt-in.
Both halves of that changed on 07-08-2026: Axle now opt SigEnergy members
in BY DEFAULT from the 8th, and their new short-notice events give as little
as 2 h — far less room for solar to top the battery up before the window.
New _alert_vpp_shortfall Pushovers the figures, the window and where export
will stop. Deliberately priority 0 so quiet hours CAN suppress it: nothing
can be done at 03:00, because pre-charge never imports by design, and being
woken to be told the export will be smaller helps nobody — the WARNING (the
level was wrong too, so it had been logging as plain Info) is the durable
record. Wrapped whole: an advisory must never cost us the export. NOTHING
ELSE ON THE DRIVE PATH CHANGED — the lead-in stays at T-2min, measured as
already at full export by t=0 (05-Aug -0.66 s: -4000 W; 30-Jul +7.4 s:
-3908 W), so the community's "start 5-10 min early" fix addresses Axle's
CLOUD DISPATCH ramp, a path we do not use. Suite 500 -> 507; 4 of the 7 new
cases verified failing against 5.57.0, the other 3 deliberate regression
guards that pass on both sides.

## v5.57.0 — 06-08-2026

ADVERSARIAL REVIEW OF THE VPP DRIVE PATH — the
money-bearing code, first fresh-eyes pass since the 5.30 series. Five
confirmed findings, all fixed; one suspected finding REFUTED (the daytime
latch does survive a restart — v5.48.0's persistence covers it).

1. NO DIRECTION GUARD (latent since v5.28.0, recorded then as "noted only"
   and never closed). Axle's API carries import_export and the client
   returns it; _apply_vpp_event never read it. An announced IMPORT event
   would have been announced, pre-charged and then SELF-DRIVEN AS A FULL
   4 kW EXPORT — pushing energy out through the very window the grid wants
   it in, draining the battery for a dispatch that settles against us. A
   non-export event is now treated exactly like "no event" (the None branch
   already stands down pre-window state cleanly), with ONE warning per
   event, latched on its start time — the poll repeats every 10 minutes,
   potentially for hours of lead time.

2. DAYTIME-DISCHARGE CHARGE CAP NOT MAINTAINED. daytime_export pins
   charge=0 because in 0x05 an open charge limit lets high PV charge the
   battery INSTEAD of exporting — the v5.29.0 missed-dispatch failure. The
   verify loop skipped every limit during the window, so a failed or
   externally reverted write resurrected that failure with the mode
   register reading a perfectly correct 0x05. Exact sibling of the bank
   charge-cap gap closed in v5.56.0; the verify branch now maintains it,
   daytime discharge only (dark windows never pinned charge — PV is zero
   and the register is irrelevant).

3. DISCHARGE LIMIT NOT MAINTAINED EITHER, despite the verify docstring
   promising "export_active: discharge limit = inverter max" SINCE v5.16
   while the ACTIVE branch skipped the whole window. A stale low cap
   throttles the paid export with nothing to heal it. Now maintained in
   the discharge sub-mode (the register the drive itself wrote). Neither
   new write can cause a grid import, so the 10-Apr-2026 rule stands; the
   old test asserting "limits untouched in discharge" was pinning the GAP
   as if it were the contract, and has been flipped with its rationale.

4. vpp_export_mode NOT PERSISTED. Verify runs BEFORE the act step, so the
   first tick after a mid-window restart read the store default (0x06) and
   "corrected" a daytime window's 0x02/0x05 register to it — one spurious
   mode write per restart, each costing a real mode-switch settle (~26 s of
   degraded export, measured 15-Jun-2026). Now saved in accumulators.json
   and restored; a pre-5.57 file simply leaves the default.

5. A WINDOW SPANNING MIDNIGHT SETTLED NEGATIVE (latent since v5.28.0 —
   Axle has only ever sent within-day windows). The export figure is
   (grid_export_daily_kwh - anchor) and the midnight rollover zeroes the
   counter, so the log, the JSONL, the device states and the Pushover would
   all have carried "-N kWh exported". The rollover now re-bases the anchor
   (pure _vpp_export_anchor_after_midnight, unit-tested) so the delta is
   continuous across midnight.

Plus: the late-detection path now writes the JSONL event header it always
skipped, so a late-published event's snapshot file carries its announcement
record like every other.

Suite 485 -> 500, with the TEN finding-pinned cases verified FAILING against
the pre-fix code and the five deliberately-neutral ones passing on both
sides (they assert behaviour that was already right — a test that fails on
both sides proves nothing).

## v5.56.0 — 05-08-2026

THE POST-EVENT REPORT WAS ANSWERING A QUESTION WE STOPPED
ASKING IN JUNE. The VPP summary Pushover still asked what AXLE had done — "Did
Axle keep PV running through battery export?", "What EMS mode did Axle use?" —
wording left over from the observe-and-hand-over model that v5.28.0 replaced
with self-drive. Every window since has been driven by us over Modbus with
Axle's dispatch ignored, so those questions had a false premise baked in.

Alongside it, `pv_survived` was a bare `min_pv_w > 100` with no daylight test,
so EVERY DARK WINDOW reported "PV collapsed" — an alarm about the sun having
set. Both fired together on the 05-Aug-2026 21:00-22:00 BST event: 45 snapshots
at 0 W PV, a textbook 4.23 kWh export holding grid at -4000 W (+/-50 W) all
hour, and a notification saying PV had collapsed and asking what Axle had done.
The report was the only thing wrong with that event.

Now: the PV verdict is gated on the daylight flag latched at VPP_ACTIVE entry
and reads ran / curtailed / n/a (dark window), with the boolean state kept as
the "nothing went wrong" flag a trigger wants. The prompt states plainly that
the export was self-driven and asks about OUR mode choice. New device states
lastVppPvStatus and lastVppDriver carry what a boolean cannot.

`driver` in the JSONL was `"self" if export_active else "axle"` — it mirrored
our own intent flag, so it could only ever read "self" once we started driving.
It never looked at the hardware, and so could not answer the one question it
existed to answer. It now compares the LIVE mode register against the mode we
wrote: a mode we did not write, while we hold Remote EMS, means something
external moved it. Reported per snapshot and summarised, with a WARNING if it
is ever seen.

_verify_ems_registers gains ONE register during an active window: the bank
sub-mode's charge cap. In bank (0x02) that cap IS the export mechanism — it is
what stops the inverter soaking the PV surplus into the battery instead of
selling it. Drift there would leave the mode register reading a perfectly
correct 0x02 while the export fell to nothing, and _drive_vpp_export only
rewrites the cap when the surplus moves by >300 W, so it could stand for the
rest of the window. This does not reopen the 10-Apr-2026 grid-import incident:
that was the solar-overflow cap being written over a VPP window; here the
expected value is the VPP driver's own cap and the check runs only while bank
is live. Every other limit still stays untouched for the whole window.

AND THE TIME-ZONE SWEEP IS FINALLY FINISHED — IT HAD BEEN DONE THREE TIMES AND
NEVER FINISHED ONCE. v5.22.1 fixed octopus_api. v5.55.3 unified battery_manager
on _london_tz / _london_localise / _to_london with stdlib zoneinfo preferred,
and wrote a long, accurate note about why duplication caused the bug — filed in
plugin.py, which was one of the files it had NOT fixed. plugin.py still had
FIFTEEN hand-rolled copies, including _local_today_str() (the midnight-rollover
basis, the exact bug class that release was written about) and
_event_is_daytime() (where an hour's error flips a dusk-edge window to daytime
and runs the mode that curtails PV); openmeteo_forecast had four more.

The implementation now lives in `london_time.py`, BELOW every module that needs
it, so there is no longer anywhere sensible to put a sixth copy. plugin.py,
battery_manager, octopus_api and openmeteo_forecast all import it; `import pytz`
appears in exactly one place in this plugin, inside that module, as the second
choice after zoneinfo.

openmeteo_forecast was the piece v5.55.3 deliberately left out, because its dawn
parse asks for `is_dst=False` to resolve the October fallback hour and zoneinfo
spells that `fold=1` rather than a keyword. THE MAPPING TURNED OUT TO MATTER
MORE THAN THE NOTE SUGGESTED: battery_manager's own shared helper had the pytz
branch taking the SECOND (GMT) occurrence and the zoneinfo branch the FIRST
(BST), so that hour resolved differently depending on which library happened to
be installed. `london_localise(prefer_dst=)` now makes the choice explicit and
identical either way, pinned by tests asserting absolute UTC instants.
openmeteo's four sites all degraded silently too — three left the datetime
NAIVE and the fourth fell back to a flat "-1 hour", which is right for BST and
wrong for the four months of GMT. octopus_api's four were already
zoneinfo-first, but two ended in a bare naive datetime, and `.astimezone()` on
one of those reads the SERVER clock.

The tests were lying in the same two ways as last time: a private _london_today
helper with its own pytz fallback (so it could disagree with the module for an
hour every night in BST), and a `("12:40", "11:40")` assertion tolerating a
pytz-less host — which would have passed against the very bug being removed.
WORSE, THE HARNESS THAT PROVED "GREEN WITHOUT PYTZ" WAS ITSELF A NO-OP: it
blocked the import via find_module/load_module, a protocol Python REMOVED in
3.12, so it was silently ignored and pytz was present for every run that
claimed otherwise. Rewritten onto find_spec and self-tested before being
trusted. With it genuinely blocking, two of the new openmeteo cases fail
against the old code. Suite 434 -> 485, 0 skipped, green with and without.

## v5.55.5 — 05-08-2026

SOLAR OVERFLOW WAS FLAPPING (battery_manager 3.9 -> 3.10).
Its physics gate — does today's remaining solar exceed the room left in the
battery — was a hard cut at exactly zero with no hysteresis, so a day sitting on
that boundary flipped the decision every few minutes. Measured on 05-Aug-2026:
nine transitions in a day, four inside twenty minutes, at physics surplus 0.0 /
0.4 / 0.2 kWh. Each flip writes a full decision audit plus five Modbus registers.

The cost is not just log noise. Export STOPS for every gap, so PV surplus banks
into an already-high battery with the DNO cap unused — the clipping the feature
exists to prevent. Live at the time: 86.5% SOC, 7.7 kW PV, grid at -7 W.

It also self-destabilises. Engaging caps the charge, so SOC climbs slower, so
headroom to 100% stays large, so the surplus falls back under zero; releasing then
fills the battery fast and pushes it straight back over. Cloud (PV 8146 W ->
2152 W in twelve minutes) only adds noise on top of that loop, which is why it
looked like weather.

Fix is asymmetric and errs late: engaging now needs >= 1.0 kWh of physics surplus
AND ten minutes since the last release; releasing is unchanged at < 0 and stays
immediate, so dusk, a storm or a collapsing forecast still stand it down on the
next tick and no path can hold a stale cap. All four of that afternoon's
re-engages would have been refused by the threshold alone. Erring late is the
KPI-safe direction anyway — a kWh kept in the battery beats one exported at 12p.
SOLAR_OVERFLOW_CAP_DEADBAND_W has always damped cap REWRITES on exactly this
reasoning; it was simply never applied to the engage/release boundary.

The OVERFLOW audit line now quotes the physics surplus and the threshold it was
judged against. The old bare "no surplus or conditions not met" was what made this
take a source read to diagnose. The gate and the log share one definition of that
number (_overflow_physics_surplus), so they cannot drift apart — the v5.55.3
lesson, applied before rather than after.

## v5.55.4 — 04-08-2026

CI has been failing since 02-08 on two ruff F541s — an
f-string with no placeholders, in the charge-cutoff backstop warning at
sigenergy_modbus.py:953. Dropped the two stray f prefixes. No behaviour change:
the string had nothing to interpolate, which is why ruff objected. This repo's
CI is a syntax check plus ruff and has NO test suite at all, so a lint error is
the whole gate — worth noting for the most complex plugin in the estate.

## v5.55.3 — 30-07-2026

A SILENT UTC FALLBACK IN THE DECISION ENGINE (battery_manager
3.8 -> 3.9). Chasing why four battery_manager tests failed on the usual runner
turned up something better than a test-environment quirk: two production sites
converted to Europe/London with pytz ONLY and, when pytz was missing, fell back
to returning the UTC value unchanged. Not an error — an answer quietly one hour
out for the eight months of BST. `_to_local()` returned `dt` unconverted, so
every caller compared a UTC clock against local wall-clock windows; and the
overnight-drain midnight boundary was built at UTC midnight, i.e. 01:00 BST.
The failing tests were not noise, they were the two sites being caught.

ROOT CAUSE WAS DUPLICATION: five hand-rolled copies of the same conversion,
three with a stdlib-zoneinfo tier and two without. That is also why these two
were missed when octopus_api got exactly this fix in v5.22.1 (27-May-2026).
Now ONE implementation — `_london_tz` / `_london_localise` / `_to_london` — used
by all five, with stdlib zoneinfo PREFERRED over pytz: it ships with Python
3.9+, so it cannot vanish when a Packages rebuild fails (this install has had
that happen more than once), and it has no `.localize()` trap. Attaching a pytz
zone via a bare `replace(tzinfo=...)` yields LMT, -00:01 for London — the exact
detail a copied block gets wrong, now impossible to get wrong twice.

LIVE INSTALLS WERE NEVER AFFECTED: pytz>=2024.1 is pinned in requirements.txt
and bundled in Contents/Packages (2026.1.post1 present), so the working path
was always taken. This removes a latent wrong-answer path, not a live fault.

The test suite was lying too, in three ways, all fixed: two tests did their own
`import pytz` and ERRORED before asserting anything; two more SKIPPED silently
(a skipped test is a test that is not testing); and a `_today_str()` helper
returned the UTC date where the module returns the London one, which would have
disagreed for one hour every night in BST. Suite now 434 tests, 0 skipped,
0 failures, WITH and WITHOUT pytz — previously 4 failed and 2 skipped.

NOT DONE, deliberately, and flagged rather than half-finished: openmeteo_forecast.py
carries the SAME pattern (module-level LONDON_TZ + a `PYTZ_AVAILABLE` gate that
leaves a datetime NAIVE when absent). It cannot be converted piecemeal — its
dawn parse calls `LONDON_TZ.localize(dt, is_dst=False)` to resolve the autumn
fold, and zoneinfo expresses that as `fold=1`, not a kwarg. Changing the module
constant without that mapping would break the once-a-year path. Worth doing as
its own change with tests pinning the fold equivalence.

## v5.55.2 — 30-07-2026

"NO EVENT" WAS BEING REPORTED AS A FAULT. Axle signals
"nothing scheduled" in TWO shapes: a null body, and — from the moment an event
ends — a full object with every field null:
  {"start_time": null, "end_time": null, "import_export": null,
   "opted_out": false, "updated_at": "..."}
That object is TRUTHY, so it sailed past the empty-body check and landed in
the malformed-timestamps branch, logging an ERROR every 10 minutes from
20:00:47 — one minute after tonight's event, the first in six weeks, ended.
By 21:03 it had raised 8 consecutive "failures", pushed a Pushover alert
through Log_Error_Watch, and left the monitor device reading a fault.

The plugin's BEHAVIOUR was right all along (it read the reply as "no event");
only the reporting was wrong. This misclassification is older than yesterday
and was harmless while invisible — v5.55.0 made failures visible, which is
exactly how it surfaced. The visibility is doing its job; the classification
needed to learn Axle's second dialect.

BOTH timestamps null = no event. Only ONE null, or present-but-unparseable =
genuinely malformed, and STILL an error — that discrimination is the point,
and a broader "any null → no event" guard would have quietly lost it.
+2 tests, 433 -> 434. One EXISTING test had to be moved rather than relaxed:
it asserted an error for a both-absent payload, which encoded the very bug
being fixed, so it now uses a present-but-unparseable pair instead.

## v5.55.1 — 30-07-2026

THE DASHBOARD'S VPP WINDOW WAS A HARDCODED EMPTY STRING.
`/api/status` published `"event_str": ""` as a literal, from the day the block
was written, so every consumer that appends it rendered "VPP event announced:"
and then stopped — the one fact worth showing, WHEN, was the fact missing.
Live-spotted on the phone within an hour of the feed coming back, which is the
first time anything had ever taken that branch. New `_vpp_event_str()` formats
the stored window through `_local_time` (so it matches the device states and
the log rather than reading an hour early through BST), prefixes the date only
when the window is not today, and returns "" on a missing or malformed event
because every caller already treats "" as "say nothing". +5 tests, 428 -> 433.

## v5.55.0 — 30-07-2026

A FAILING AXLE POLL IS NOW VISIBLE. Axle announced a grid
event for this evening; the plugin knew nothing about it, and had known nothing
for six weeks. The token was revoked server-side some time after the 15-Jun
event (a JWT, but NOT expired — exp is 2053; the endpoint answers
401 "Could not validate credentials"), so every poll since had failed.

NOT ONE LINE was logged about it. Two faults compounded:
  1. AxleAPI was the only API client in this plugin constructed WITHOUT a
     logger, so it fell back to logging.getLogger("SigenEnergyManager.AxleAPI")
     — a logger with no handler attached anywhere here. Every 401 was
     discarded. Compare OctopusAPI and SigenergyModbus, both of which are
     handed self.logger.
  2. get_next_event() returns None for "no event scheduled" AND for a hard
     failure, so even a caller watching the return value could not tell a dead
     feed from a quiet week. The VPP device read a calm "Standby" throughout.

The silence is the bug worth fixing — a rejected token is Axle's business, but
six weeks of not knowing is ours. AxleAPI now takes a logger and records
last_error; _record_vpp_api_status() logs a failure once and then hourly (a
sustained outage costs one line an hour, not one per 10-min poll) and logs the
recovery; and the Axle VPP Monitor device carries apiStatus + apiLastOk so the
state is visible without reading a log at all. +13 tests (all verified failing
against 5.54.0), suite 415 -> 428.

RESOLVED SAME DAY: CliveS fetched a replacement token from his Axle account and
put it in IndigoSecrets.py at 14:41; the restart onto this version picked it up
and the poll went healthy immediately — apiStatus "OK", and tonight's 19:00-20:00
event was announced within seconds, which is the new machinery proving itself on
the first try.

GOTCHA THAT NEARLY CAUSED A MISDIAGNOSIS, worth remembering: a plugin host caches
IndigoSecrets in sys.modules at ITS OWN startup, so the token a host holds is the
one that was on disk when that host last started, NOT what is on disk now. Testing
the same key from ClaudeBridge's context (running since 23-Jul) kept returning 401
AFTER the replacement was in place, because that host still held the old value —
a sys.path.insert cannot help, the cached module wins. Read the file directly with
importlib when you need to know what is REALLY on disk, and remember a credential
change needs a restart of every host that reads it, not just the file edited.

## v5.54.0 — 26-07-2026

the restore alert now WORKS OUT whether today's solar will
refill the reserve, instead of leaving the reader to guess which of the two
release rules applies to them. CliveS asked whether this was possible or too
difficult — it is neither: the plugin already answers exactly that question
every 60 seconds, so the alert now asks it once more at send time and states
the outcome, with both figures (spare vs needed) so the sums can be checked.
New _solar_refill_outlook() builds the provisional snapshot + 24h balance the
SAME way _evaluate_manager_impl does inside a lockout window, so the message
and the decision cannot disagree. Four outcomes, each phrased honestly: solar
covers it (export restarts within the minute), does not cover it YET (names
the forecast as a way out), night (does NOT dangle a forecast that cannot
arrive), and unknown — where it falls back to naming both rules and claiming
nothing. The needed figure quotes the MARGIN-INFLATED bar (× 1.25), because
that is the bar actually used; quoting the raw gap would make a "not yet"
verdict look wrong to anyone checking it.

The same outlook is now logged at restore time. On 26-Jul-2026 export sat
suppressed for 20 minutes after the restore — manager evaluating every 60 s on
live data, no errors — and nothing in the log recorded what was being judged,
so afterwards the only honest answer was "we cannot tell". That gap is still
UNEXPLAINED and worth a proper look; this line makes the next one answerable.
+18 tests, 402 -> 415.

## v5.53.0 — 26-07-2026

the power-cut alerts now carry the whole picture. These are
the two messages read on a phone during an outage, and they said almost nothing:
time, off-grid mode, and how long the cut lasted. Everything needed to judge the
situation — how full the battery is, what the house is pulling, how long that
lasts, and on a restore what export is doing and both ways the lockout ends —
lived in a log nobody opens at the time. Both channels now carry the same full
body. Three pure helpers so the arithmetic is testable without a power cut:
_backup_runtime_hours (usable energy is everything ABOVE the discharge cutoff,
since the inverter stops there and overstating backup is worst in exactly this
message), _format_runtime (minutes / one decimal / whole hours / days, capped at
"10+ days"), and _lockout_message. Every figure is optional: a paragraph whose
readings are missing is dropped rather than printed as a zero, so a partial
Modbus read costs one line and never the alert. The lockout end time is derived
from the SAME pluginPrefs["powerRestoredTime"] the window itself uses, so the
time quoted cannot drift from when export actually resumes.

Also fixes a LATENT BREAKAGE IN THE STORM ALERTS, found by ruff while in here
(F821, pre-existing on HEAD). The v5.45.0 locking restructure split
_apply_storm_result out of _check_storm_watch and left `loc_name` behind in the
caller, so the two bodies that quote it — the YELLOW escalation and every
ALL-CLEAR — raised NameError instead of sending. Amber and red never quote it,
which is why nothing looked broken. The sting is in the tail: storm_alerted_level
is written only AFTER a successful send, so a failed all-clear left it stuck at
the old level and every later storm at or below it was judged "already alerted"
and stayed silent. Armed but never fired — no storm here since 02-Jul-2026.
+28 tests total, 374 -> 402; the 3 storm tests raise NameError on the old code.

## v5.52.1 — 26-07-2026

the grid-restore message named only ONE of the two export
release rules. It promised "unless SOC >= 85%", wording written before v5.50.0
added the forecast-aware solar-refill release — so when this morning's 83-second
cut (grid lost 08:39:29, restored 08:40:52) was followed by export resuming at
74% SOC at 09:00:30, the plugin had told the owner one thing and done another.
The release path itself was already honest (it names WHICH rule fired), but the
line read FIRST, at the moment of the outage, was not. Now names both. The
Pushover/email body says nothing about export at all, so it needed no change.

## v5.52.0 — 25-07-2026

dashboard economics audit — four faults found by checking
the figures against the live ledger rather than reading the code.
* "Grid-only" now carries the standing charge. A grid-only home pays the same
  daily charge, so the counterfactual sat ~£0.62/day (~£225/yr) under the truth
  while the elec bill printed beside it included the charge. The solar BENEFIT
  is unchanged — standing cancels in (no_sol + st) - (imp + st) + exp.
* "Elec bill" now carries the standing charge on UNSETTLED days too. It was
  unit-only for those, and Octopus settles ~a day in arrears, so a 7-day window
  nearly always held one. Live proof: the week reported £3.82 against a true
  £4.44 — one day's standing charge missing, under a header promising
  "unit + standing".
  Together these two make the Period totals row a real identity:
      solar benefit = grid-only - elec bill + export earned
* /api/daily accepts up to 800 days (was 365). The dashboards' week-on-week card
  compares against the same week last year by probing offsets 364-370, and a 365
  cap returned at most one of the seven — the year column could never unlock,
  however long the history grew.
* A missing electricity unit rate no longer paints a green "Covered" badge.
  _wh_build_card billed the standing charge alone, which export nearly always
  beat; bill/net/covered now come back None so the page can render "—".
* Calendar months report elec_whole_house_total_gbp too, so that table reads as
  the same identity as Period totals.
* The 12p export fallback was written out in nine places — now one constant,
  DEFAULT_EXPORT_RATE_P, and the `if export_rate_p else 12.0` test that silently
  swapped a genuine 0p rate for 12p is now an `is None` test.
* gas_estimated now honours has_gas on the yesterday / day-before cards, so an
  electricity-only user stops seeing "(est)" on a £0.00 gas line.
* /api/status publishes battery.capacity_kwh so dashboards stop hardcoding this
  system's 35.04 kWh pack.

## v5.51.2 — 21-07-2026

shared plugin_utils.py refreshed to v1.3 — the
estate-wide propagation of the four Appliance Monitor deep-review fixes.
* install_timestamp_filter() is idempotent — a second call used to stack a
  second filter, so every log line came out with two timestamps.
* `import indigo` is soft, so the module imports outside the Indigo host and
  can be exercised by offline tests.
* A malformed log call keeps its arguments in the log instead of dropping
  them, so a %-placeholder mismatch is visible.
* New shared as_bool() — a pref re-serialised as the string "false" is
  truthy, which is exactly the wrong answer.
This bundle keeps its LOCAL variant: install_timestamp_filter also walks up
to every reachable handler so module-logger records get stamped. That walk
is now idempotent too.

5.51.0 — Daytime charge is paced to a 90% target, not 100% (battery_manager 3.7→3.8).
  The same root cause as 5.50.0, one layer down: the plugin kept treating 100% as the
  goal when the owner's requirement is 85-90%. Solar overflow paced the charge to hit
  100% exactly at dusk, and because `required_charge_kw` is subtracted from export
  BEFORE the DNO cap is applied, that high target spent the low-surplus MORNING buying
  SOC out of kWh that would have fitted under the cap — then still met the afternoon
  peak with less headroom than it started with, and clipped anyway. CliveS, 20-Jul:
  "I do not need to get to 100%, it means the chance of clipping is greater. Anything
  above 90% is great, above 85% is still OK", with 80%+ ample for a power cut.
  The target is a GOAL, not a ceiling. Once export is at the cap the above-cap excess
  still has nowhere to go but the battery, so it charges straight past the target —
  the high finish comes free, out of surplus that would otherwise have been binned.
  Modelled on 20-Jul's measured curve (dull ~5.1 kW morning, breaking clear to 8.59 kW
  at 14:00): 100% target -> 45.0 kWh exported, 1.53 kWh CLIPPED, ends 91.1%. 90%
  target -> 46.6 kWh exported, NOTHING clipped, ends 90.8%. Three-tenths of a point of
  finish for 1.6 kWh of export and the elimination of the waste.
  New prefs `solarOverflowTargetSoc` (90) + `solarOverflowMinEndSoc` (80), guarded;
  new snapshot fields solar_overflow_target_pct / solar_overflow_min_end_pct /
  storm_active. `_apply_storm_override` sets storm_active, which restores the 100%
  target — a storm is the one time a genuinely full battery is worth clipping for —
  while KEEPING the lazy pacing, so it is never force-charged out of export when the
  day's own solar would have reached 100% unaided (CliveS's explicit constraint).
  NO dull-day guard in the pacing, and this is the interesting bit: the contract tests
  proved one would be dead code. The physics gate above it only exports when remaining
  solar EXCEEDS the room to 100%, so whenever overflow runs the day can demonstrably
  reach 100% — and the gate re-evaluates every tick, so the moment the rest of the day
  can no longer fill the battery it returns None, export stops and everything charges.
  A lower target keeps SOC lower, which makes that gate bite EARLIER: the pacing change
  is self-limiting and the end-of-day level is protected by machinery already present.
  The floor pref is therefore just a clamp against a mis-set target. Caveat recorded
  honestly: because the gate cuts export earlier in the afternoon, the real-world gain
  will be somewhat below the 1.6 kWh the model shows (the model has no gate).
  +11 contract tests (344 → 355), including a pin that target=100 reduces to the exact
  pre-3.8 formula, so the change is provably a no-op at the old setting.
5.50.0 — Post-power-cut export lockout is now FORECAST-aware as well as SOC-aware.
  Prompted by a live incident this morning: the grid dropped for 109 SECONDS at
  05:25:54 (restored 05:27:43) and the standard 4-hour lockout armed at SOC 75.6%.
  The flat 85% floor (v5.34/5.35) then held export off until 07:36:07 while the
  battery climbed to the floor — 2h 08m of no export, ~3.3 kWh banked instead of
  sold. Every one of those kWh was exportable at the time: PV surplus ran
  1.1-4.25 kW, comfortably inside the 4 kW DNO cap. The bill came due in the
  afternoon. By 13:28 the battery was at 91.6% with only ~2.9 kWh of headroom
  against 6.8 kW of PV, so once the washing machine finished the pack would fill
  and every watt above house+4 kW would be CLIPPED — thrown away, because the DNO
  cap leaves nowhere for it to go. Without the lockout we'd have been at ~82% with
  ~6.2 kWh of headroom and clipped nothing. The lockout had converted exportable
  morning kWh into afternoon curtailment. Underlining it: the overnight optimiser
  had already computed the day's actual power-cut resilience minimum as 10% (3.5
  kWh), while the lockout insisted on banking to 85%.
  FIX: a SECOND, strictly-additional release condition (`_solar_refill_releases_
  lockout`, pure + unit-tested). Export also resumes mid-lockout once the day's
  remaining solar, net of house load to dusk, covers the gap up to the SOC floor
  with a 1.25x margin — because on a bright summer morning holding export off
  banks nothing the sun wasn't going to deliver anyway. Guarded by a new
  `powerCutLockoutMinSocPct` floor (default 50): however good the forecast, the
  early release never applies to a nearly-empty battery, which is not a resilience
  reserve. Deliberately expressed in kWh, not SOC percent — an SOC-space form
  (projected >= floor x margin) is unsatisfiable at the defaults since 85 x 1.25 =
  106 > 100. Replayed against this morning's logged figures it releases at the
  first daytime tick (~06:00) instead of 07:36; a winter night, a dull December
  day, an unknown SOC and a below-minimum battery all still hold.
  Plumbing: `_power_cut_window_active()` extracted from `_resolve_export_lockout`
  so `_evaluate_manager_impl` can build a provisional snapshot + SufficiencyBalance
  ONLY inside a lockout window (both calls are pure and side-effect free; outside a
  window — nearly always — normal ticks pay nothing). `_resolve_export_lockout`
  takes an optional `balance`; omitting it reproduces pre-5.50 behaviour exactly,
  and a failure building the balance falls back to the flat floor. The one-shot
  "export re-enabled during lockout" INFO now names WHICH rule released it, and is
  no longer able to crash formatting an unknown SOC (reachable when export is
  disabled mid-window). Dashboard `power_cut` block gains `lockout_min_soc` +
  `solar_release_active` so the Lockout chip can explain itself.
  NOT applied to the storm override, despite the two mirroring each other since
  v5.39: a storm forecast means the solar may not arrive, so releasing export on
  the strength of a forecast is exactly wrong there. Commented in both places so
  nobody "restores symmetry" later. +22 tests (344 total, all green).
5.49.0 — Solar card figures reconciled. The Energy page read "38.3 kWh today,
  forecast 53, Remaining 25.3" — figures that cannot be added up. Two causes,
  both in how remainingTodayKwh was derived (openmeteo_forecast.py 1.6 → 1.7):
  (a) it was summed off the RAW hourly p50 buckets while the forecast beside it
  is bias-corrected, so the two sat on different scales (raw 57.7 × 0.915 =
  52.8); (b) it counted the WHOLE current hour as still to come, overstating it
  by up to a full peak hour (~7 kWh at midday). Both now owned by one helper,
  openmeteo_forecast._remaining_today_kwh, which the fetch path and the
  enrichment path share. This also corrects the "expected total" line in Show
  Today's Energy Summary and Show Manager Status, which add pvDailyKwh to it.
  The hourly forecast published to the dashboards is scaled by the same day
  factor, so the bars and their kWh tooltips now sum to the headline forecast
  (the optimiser JSON already did this). New "Expected total" figure on both
  dashboards' solar cards = generated so far + still to come, so the projected
  end-of-day number is stated rather than left to the reader to work out.
  NOT touched: forecast_p50 passed to the decision engine stays raw (it is
  compared against SOLAR_DUSK_THRESHOLD_WH, and scaling would shift dusk
  detection), and the persisted _hourly_p50_* cache buckets stay raw because
  the bands are recomputed nightly. 9 new tests (27 → 36).
5.48.0 — VPP window survives a plugin restart. The Axle state machine was
  re-driven purely from the API each poll, so a restart mid-window relied on
  Axle still returning the active event; if its endpoint drops the event once
  live (Predbat issue #3051's failure mode) the rest of the window was silently
  missed. Now the active window (state + event + pre-charge/cutoff/export flags)
  is persisted into accumulators.json on every _vpp_transition (crash-safe,
  atomic — pluginPrefs only flush on a graceful shutdown), restored by
  _load_accumulators regardless of day (a window can span midnight), and
  _rehydrate_vpp_state() in startup() makes the time-based call: resume an open
  window WITHOUT the API, or reset the discharge-cutoff register + Self
  Consumption for a window that ended during downtime. New pure module helpers
  _serialise_vpp_event / _deserialise_vpp_event / _vpp_resume_decision with a
  contract test for the restart path. DST-safe throughout (UTC end-to-end).
5.47.0 — Octopus Go/iGo readiness (pre-September switch). (1) Go cheap-window
  corrected 00:30-05:30 -> 23:30-04:30 in TARIFF_WINDOWS (octopus_api.py +
  battery_manager.py) to match the live GO-FIX product (region F) — the stale
  window missed the cheap 23:30-00:30 hour and would have charged 04:30-05:30
  at the 31.36p day rate. (2) Power-cut reserve now guaranteed on TOU tariffs:
  _check_resilience_buffer fired only on Tracker/Flexible, so on Go/iGo a night
  before a well-covered (sunny) day left nothing holding the dawn_target floor
  and the battery drifted to the 1% health floor. It now tops the reserve up on
  Go/iGo/Flux/iFlux too, but ONLY inside the cheap window (night rate, never the
  day/peak rate) and ONLY when the import planner is not already covering
  tomorrow, so it never truncates the arbitrage fill. +3 resilience tests;
  fixed the calendar-flaky surplus-conservatism test (weekend need). 304 pass.
5.46.0 — Gas cost settle: full-day COVERAGE gate (fixes £0.00 gas on the
  whole-house card). Gas settles slower than electricity and can arrive
  PARTIALLY: on 03-07 the 1 Jul row froze (cost_settled) at 0.034 kWh / £0.00
  gas off a single 00:00-00:30 slot — and because the day/yesterday gas
  ESTIMATE reuses the most recent settled gas_kwh, the bad row poisoned the
  estimates too (Octopus app showed £0.74/£0.45; card showed £0.00). History:
  the same freeze happened 21-Jun and was fixed with a 46-slot gate on BOTH
  fuels; the later daily-read-meter accommodation (H2) relaxed gas back to
  presence-only, reintroducing it. Fix: _sum_consumption_for_date now returns
  `complete` — readings reach the end of the local day (90-min tolerance) —
  which is True for a whole half-hourly day AND for a daily meter's single
  24h reading, False for a partial day; the settle gates gas on it. The
  frozen 2026-07-01 row was un-settled to re-settle with complete data.
  +6 tests (301 pass; the 1 pre-existing failure is the time-of-day-dependent
  test_calm_night_drain_continues_unchanged flake, unrelated).
5.45.0 — Locking-model restructure (the last deep-review-#3 deferral). _tick no
  longer holds _state_lock for its duration: network stages (modbus/forecast/
  octopus/VPP/storm/settle) run I/O UNLOCKED and lock only their merge; control
  stages (evaluate/verify/act, midnight, scheduled import) self-lock whole;
  get_dashboard_data takes a ms-scale locked snapshot then builds lock-free.
  NEW test_concurrency.py pins the contract. Bonus bug: the tick stamped
  last_modbus AFTER _poll_modbus returned, clobbering the v5.43.0 outage
  back-off (it never worked) — stamps before the call now. Live-verified:
  dashboard 2.6-10ms during polling (was up to ~20s mid-poll). 288→295 tests.
5.44.0 — Decision tuning. _plan_agile_import gates the cheapest viable slot on
  round-trip break-even (rate/0.94 must undercut tomorrow's daytime reference;
  None = ungated) — returns SELF_CONSUMPTION passthrough when pre-charging
  loses money. surplus_kwh conservatism CONFIRMED by CliveS and pinned with an
  annotation + characterisation test. 283→288 tests.
5.43.1 — Deep review #3 batch 3 (~75 lows/infos): Chart.js bundled locally;
  power-cut state persisted; atomic JSON writes; octopus JWT purge + failure
  negative-caching; GTI clamps; monotonic throttle; Sigenergy variable folder
  auto-created; test-quality fixes (tautologies replaced, value-0 decode);
  companion scripts hardened (optimiser v3.14, digest v1.1, axle v1.3).
5.43.0 — Deep review #3 batch 2 (mediums): pause survives restart; staleness
  guard holds evaluation on frozen inverter data + poll back-off tiers; flood
  target + power-cut lockout crash-safe in accumulators.json; no phantom 0%
  SOC at restart; menu/prefs callbacks under _state_lock; connect() health
  probe + escalating reconnect; storm word-boundary matching; flood gate
  requires demand>0; Kraken null-token guard; London-day rate windows (BST
  skew); forecast staleness caps + day-aware persisted bias baseline; month
  cost vars fixed (1st-of-month zero + whole-house basis).
5.42.0 — Deep review #3 batch 1 (highs; 122 confirmed findings across the
  review, 257→283 tests by batch 3): hardware charge-cutoff (reg 40047)
  backstop on every grid import (target+3%, verify-maintained, released on
  stop/disengage/startup — a crash mid-import can no longer grid-charge to
  100%; hardware-verified 02-07); flood pre-drain aborts when a storm
  suppresses export mid-drain + stops at max(target, dawn_target); modbus
  outage aborts the read cycle in ~1s with 2 log lines (was ~20 ERROR lines);
  storm-feed failure returns None not "none" (level HELD through flaky polls,
  ~24h decay with its own Pushover).
5.41.0 — Publish the Octopus cost/rate variables (REVIVE). The elec_*/gas_*/
  export_*/account_balance Indigo variables had no active writer since their
  original script was retired, so they had gone stale — elec_unit_rate_p frozen
  weeks behind the live Tracker rate (read 11p while the ledger said 25.78p),
  account_balance_gbp stuck at 0. weekly_home_digest.py reads elec_unit_rate_p /
  export_rate, and get_dashboard_data's import-rate fallback reads
  elec_unit_rate_p, so both were silently using stale data. New
  _write_cost_variables (called from _write_energy_summary_variables, so the
  long-standing comment that elec_unit_rate_p is written every 30 min is now
  TRUE) republishes the bill-exact rates + balance from get_account_financials
  (the Kraken ledger — single source, no duplicate fetch/drift) and today/month
  costs from the live economics: elec_unit_rate_p, elec_standing_charge_p,
  gas_unit_rate_p, gas_standing_charge_p, export_rate_p (+ legacy export_rate),
  account_balance_gbp, elec_today_cost_gbp, gas_today_cost_gbp,
  export_today_revenue_gbp, combined_today_actual_gbp, elec_month_cost_gbp,
  export_month_revenue_gbp. Best-effort + fully guarded; a Kraken/economics
  hiccup leaves the values in place rather than blanking them.
5.40.0 — Storm reserve is now a FLAT 50% for ALL levels (was 50% yellow / 80% amber-red).
  CliveS's call: a storm should keep a 50% power-cut reserve and NEVER grid-charge above it.
  The overnight resilience-buffer import (flat-rate tariff only) tops the battery to the storm
  floor when below it; with amber/red previously at 80% a storm night would grid-charge to ~82%
  (costly, against the self-sufficiency KPI). STORM_SOC_AMBER 80→50 so the floor — and thus any
  storm-driven grid charging — is capped at 50% (tops to ~52% via the existing +2% anti-cycling
  guard; solar still fills above 50% for free, and export still reopens at the 85% release).
  Pushover storm alerts reworded to match (50% minimum reserve, no grid charge above it, export
  held off until nearly full — the old "export suspended" wording predated the 5.39.0 release).
  Tests updated for the flat-50 reserve (+1; 73 in test_plugin, 151 across suites).
5.39.0 — Storm export suppression is now SOC-aware (mirrors the post-cut lockout floor).
  The storm override held export OFF for the entire duration of a wind/storm warning,
  regardless of SOC. With a near-full battery under good solar that rammed it to 100%
  (charge takes priority over export in self-consumption) and then clipped every watt of
  PV above the DNO export cap — and it thrashed Solar Overflow on/off every poll. Export
  is now suppressed ONLY while SOC < STORM_EXPORT_RELEASE_PCT (default 85, configurable via
  stormExportReleasePct, never below the active reserve target). At/above it the reserve is
  already banked, so export resumes — Solar Overflow throttles the charge and pushes surplus
  to grid so the battery creeps up with headroom instead of curtailing. One-shot INFO logs
  the mid-storm resume. Report exposes storm.export_suppressed + storm.export_release_pct.
  The openmeteo advisory needs no change — it already defers to the plugin's published
  export_enabled/would_fire verdict. +8 tests (73 in test_plugin).
5.35.0 — Comprehensive numeric telemetry for SQL history + tidy-ups.
  • All numeric inverter telemetry states (batterySoc, *PowerWatts, temps, cell voltage,
    SoH, cutoff, daily kWh) changed from ValueType=String to Number/Integer and written
    as real numbers (guarded via _as_int/_as_float), so Indigo's built-in history
    (indigo_history.sqlite) records them as chartable columns. Previously every state was
    a String so nothing but gridOnline charted. Categorical states (emsWorkMode, gridStatus,
    etc.) stay String. No separate DB (InfluxDB/Postgres) — SQLite + the plugin's own
    half-hourly energy_timeseries.db cover it.
  • SOC floor for the post-cut export lockout is now configurable (powerCutLockoutSocFloor
    pref, default 85, guarded by _power_cut_lockout_soc_floor()).
  • Cosmetic: the Live Power Flow "Lockout" chip now keys off power_cut.export_suppressed,
    not the time window — so a battery exporting above the SOC floor shows "On Grid", not
    "Lockout".  Numeric states carry a clean uiValue (_num_state) so the device UI shows
    "99.6" not "99.59999999999999".  +5 tests (193).
5.34.0 — Power-cut export lockout is now SOC-aware + grid-online SQL state.
  (1) The 4-hour post-restore export lockout previously killed ALL export, so a
  near-full battery (e.g. 92%) under good solar would climb to 100% and clip
  generation we could have exported. The lockout now holds export off only while
  SOC < POWER_CUT_LOCKOUT_SOC_FLOOR (85%); at/above the floor export resumes so
  flood-prevention can shed surplus and protect solar. New pure `_export_locked_out`
  helper (fail-safe: unknown SOC suppresses); `_resolve_export_lockout(soc_pct)`;
  store flag `power_cut_lockout_active` now tracks the time WINDOW (cleared-event
  fires once on expiry) with `power_cut_export_suppressed` for the live state.
  (2) New numeric `gridOnline` device state (1=on-grid, 0=power cut) so SQL Logger
  can chart a clean power-cut timeline — the existing states are all strings and
  don't chart. Not written on modbus-offline (offline != a real cut).
5.33.0 — Power-cut notifications. When the inverter reports the grid has been lost
  (the house islands onto the battery) and again when mains power is restored, send a
  Pushover alert (normal priority, so it respects the configured quiet hours) and an
  email. Recipient resolves IndigoSecrets.POWERCUT_EMAIL first, then the new
  powerCutEmailRecipient pref; toggle via powerCutNotify (default on). Both sends are
  best-effort and never break the poll loop — note a longer outage may also drop the
  broadband, in which case the alert lands once connectivity returns.
5.32.0 — Single source of truth for the flood-export gate. battery_manager gains
  _compute_flood_preview (pure, no daytime guard, no side effects) — the ONE place the
  gate math now lives; _check_flood_prevention consumes it (control behaviour unchanged,
  183 tests green). Each manager tick publishes the gate it acts on to
  sigen_flood_preview.json (_publish_flood_preview, atomic write) so the openmeteo advisory
  reports the SAME gate verbatim instead of re-deriving and drifting — the 23/24-Jun-2026
  "promised an export that never ran" case (advisory used the day+2 'tomorrow' states at
  01:45 instead of the refill day). 4 new contract tests lock the preview to the live
  decision (incl. the forward-looking daytime property + the regression).
5.31.6 — Solar card data: /api/status solar block now also carries
  actual_today_kwh, peak_w + peak_time (new daily peak-PV tracking, mirrors
  peak_soc — init/update/midnight-reset/persist), lifetime_kwh and total_kwp.
  Feeds the new Dashboards Solar card (today vs forecast, now/peak, tomorrow,
  yield/kWp, self-sufficiency, forecast accuracy, lifetime). Per-array forecast
  + measured per-string DEFERRED to a daylight probe (PV=0 at night; inverter
  reports a '4' count at reg 31025-ish, promising for 4 PV inputs). 179 tests.
5.31.5 — Whole-house cost: an unsettled recent day (Yesterday before its gas
  settles) now shows a PROVISIONAL card from the row's Sigen-measured
  import/export (complete at midnight) + estimated gas, instead of a blank
  "awaiting settlement". Electric + export are accurate; only gas is estimated
  until Octopus settles, then the frozen settled row takes over. New
  _wh_build_card / _wh_provisional_from_row helpers (today now uses the shared
  builder too). +1 test (179). Pairs with Dashboards v2.14.6 (tag flips
  settled<->provisional).
5.31.4 — Whole-house cost: /api/status now also exposes `day_before` +
  `day_before_date` (the settled day before yesterday) so the dashboard can
  show Today / Yesterday / Day-before. Reliably complete given the ~1-day
  settlement lag. +1 test (178). Pairs with Dashboards v2.14.3.
5.31.3 — Whole-house cost: don't freeze a partially-settled day. The settle
  pass gated only on "gas data present", so the most recent day (Octopus
  settles ~a day in arrears, often just the first 1-2 half-hour slots past
  midnight) was frozen with a near-zero bill PERMANENTLY (cost_settled). Now
  requires COST_SETTLE_MIN_SLOTS (46 of 48 half-hours) for BOTH import and gas
  before freezing; the 21-Jun-2026 premature row was un-settled to re-settle
  when complete. +2 tests (177 pass). Pairs with Dashboards v2.14.2 (Chart.js
  "Canvas already in use" fix on the 30-day bar).
5.31.2 — Whole-house cost deep-review medium/low batch: atomic daily_history.json
  writes (_atomic_write_json — temp + fsync + os.replace, both settle and
  midnight writers, so a crash can't truncate the never-pruned history);
  settle float() of API kWh guarded (one bad day skips, not aborts the cycle);
  _whole_house_summary caches the history parse by file mtime (it runs every
  ~5s on /api/status) and bounds today's gas estimate to the last 7 days.
  octopus_api 1.2->1.3: GraphQL queries parameterised (variables, not raw
  string-interpolation of account/key), import-vs-export classified by MPAN
  (not just OUTGOING), first-active-agreement wins, zoneinfo TZ fallback.
  +11 tests (175 pass): financials error/empty/errors paths, MPAN
  classification, force-bypass, gas-zero boundary, covered== boundary,
  partial-row coalescing. Pairs with Dashboards v2.14.1.
5.31.1 — Whole-house cost hardening (deep-review highs batch): (1) settle now
  values each day's STANDING + GAS rates at the rate saved on the day
  (elec_standing_p_day/gas_unit_p_day/gas_standing_p_day, captured in
  _write_daily_history) rather than the current ledger snapshot — frozen days
  stay correct across a tariff/price-cap change; falls back to the current
  ledger only for older/backfilled rows. (2) get_account_financials now
  negative-caches failures (FINANCIALS_NEG_CACHE_TTL) and returns the stale
  value, so a Kraken outage no longer makes /api/status fire a GraphQL request
  every ~5s. (3) _whole_house_summary call isolated in get_dashboard_data so a
  fault in the new block can't blank the rest of /api/status. +7 tests
  (TestSettleWholeHouseCosts, TestWholeHouseSummary); 164 pass. octopus_api 1.1->1.2.
5.31.0 — Whole-house cost (gas + electric, incl. standing charges). New
  /api/status economics.whole_house block: today (provisional), yesterday
  (settled), month-to-date net, days self-funded, account balance and a
  30-day bill-vs-export series. A 6-hourly settle pass (_settle_whole_house_costs)
  freezes each day's cost into daily_history.json once Octopus settles it,
  valued at the rate that applied on the day so a tariff change never re-writes
  history. Rates + balance come bill-exact from the Kraken account ledger
  (octopus_api.get_account_financials, active:true). Gas valued from settled
  m3 consumption via a configurable calorific factor; gas has no live meter so
  today's gas is estimated from the latest settled day. New OCTOPUS_GAS_MPRN /
  OCTOPUS_GAS_SERIAL secrets + octopusGasMprn / octopusGasSerial / gasKwhPerM3
  config fields. Pairs with Dashboards v2.14.0 'Whole-house cost' card.
5.30.1 — Guarantee the full export across ALL PV. Closes a hysteresis gap in 5.30.0:
  the band was (target-HYST, target+HYST) and HELD the previous sub-mode, so if PV fell
  from above the cap to just below it (surplus in 3.6-4.0 kW) while latched in "bank",
  self-consumption would export only the surplus (~3.7 kW), not the full target — bank
  mode never discharges to top up. Now: drop to "discharge" the instant surplus < target
  (battery tops the grid up to the target), and apply the +HYST margin only on ENTERING
  bank (so a brief PV spike can't flap us into 0x02). Net guarantee, battery permitting:
  PV=0 -> battery exports the target; PV<target -> PV + battery = target; PV>target ->
  target exported + surplus banked. +2 unit tests (test_plugin).
5.30.0 — Daytime VPP export now BANKS the surplus instead of curtailing it. New
  _drive_vpp_export() re-evaluates every manager tick during VPP_ACTIVE and picks a
  sub-mode from live PV vs the export target:
    • "bank" (daytime, PV surplus >= target): Max Self Consumption (mode 0x02) with the
      battery charge limit capped to (surplus - target). The inverter exports the full
      target to the grid (held at the DNO cap) AND banks the PV above the target into
      the battery — same mechanism as Solar Overflow. Live-proven 15-Jun at 10 kW PV:
      export 4.06 kW + battery charge 4.94 kW + home 1.05 kW, ZERO curtailment.
    • "discharge" (dark window, or PV surplus < target): the v5.29.x path — mode 0x05
      (PV-first) + charge 0 daytime, or 0x06 (ESS-first) dark — battery tops the export
      up to the target. Guarantees the paid dispatch when PV can't cover it.
  Hysteresis (+/-400 W) around the crossover stops mode flapping; Modbus only writes on
  a sub-mode change or a charge-cap shift > 300 W. vpp_export_mode tracks the live mode
  (0x02/0x05/0x06) so _verify_ems_registers maintains the right one. New
  "Force VPP Export Drive (test)" action exercises the integrated driver on hardware.
  6 new unit tests (test_plugin). This SUPERSEDES 5.29.1's always-curtail behaviour for
  high-PV daytime events while keeping its guaranteed-export floor for low PV.
5.29.2 — Register-map corrections from a deep-dive review against Sigenergy Modbus
  Protocol V2.9 (2026-05-13), the revision after our V2.8 baseline. Doc/label only,
  NO behaviour change: REMOTE_EMS_MODES 0x07 is "Reserved" (was mislabelled "AI Mode";
  0x07 was never commanded), 0x08="V2G" added; the snapshot ems_mode_name decode matches.
  Corrected the 40032/40034 comments — they are GLOBAL caps "regardless of EMS mode"
  (which is exactly why 5.29.1's charge=0 forces export). Header notes register 40001
  (PCS active-power dispatch, S32 kW, needs 40029=1 + 40031=0, no command watchdog) as a
  future "export a precise power" option — deliberately NOT used yet (PCS-level not a grid
  target, sign must be verified on hardware). sigenergy_modbus module 1.5 -> 1.6.
5.29.1 — Daytime export (mode 0x05) now pins the CHARGE limit to 0. Hardware testing
  at PV > 4 kW (15-Jun) showed that with the charge limit left open, mode 0x05 greedily
  charges the battery with PV surplus INSTEAD of exporting — grid sat near 0 for the
  first 20-60s and the paid 4 kW dispatch was missed (battery +3.5 kW, grid 0). Pinning
  charge to 0 removes the competing path, so PV is forced out to the grid up to the DNO
  cap immediately and stably (re-test: grid -4001 W, battery flat from the first sample).
  Cost: PV above (cap + house) is curtailed for the window — acceptable, the payment far
  outweighs the un-banked surplus and the battery refills from solar after the event.
  Sub-4 kW behaviour unchanged (battery already had to top the export up). daytime_export()
  docstring documents the why; test asserts charge=0.
5.29.0 — Daytime VPP export now uses mode 0x05 (Discharge PV First) instead of 0x06
  (Discharge ESS First). 0x06 curtailed PV to 0 W during daytime windows (battery did
  all the work — confirmed on the 15-Jun 07:00-08:00 event, 4.22 kWh all from battery,
  PV flat). 0x05 sources the grid dispatch from PV first and only draws the battery for
  the shortfall, so PV keeps running and the battery is preserved — yet the full (paid)
  4 kW dispatch is still guaranteed (and 0x05 == 0x06 when PV is zero, so no downside).
  _vpp_transition(VPP_ACTIVE) + the manager's ACTION_VPP_EXPORT re-assert now pick the
  mode by self._event_is_daytime(); dark windows stay on 0x06. _verify_ems_registers
  self-heals the chosen mode during VPP_ACTIVE (mode register only — never the limits).
  New sigenergy_modbus.daytime_export(); new "Force Daytime Export (PV First, test)"
  action for hardware validation. JSONL snapshots now carry ems_mode_name + driver.
5.28.2 — Axle VPP payment rate is now a config pref (axleVppRatePerKwh, default 1.00
  GBP/kWh) instead of a hardcoded £1, used for the earnings estimate on the Axle VPP
  Monitor. Coerced via the guarded _as_float so a blank/bad value falls back to 1.00.
  Mirrors Predbat's axle_pence_per_kwh. No behaviour change beyond the estimate figure.
5.28.1 — VPP cleanup follow-up: removed the now-dead Axle release-watcher
  (_vpp_check_axle_release + _send_vpp_release_alert, ~122 lines), the VPP_COOLING_OFF
  state + its dead branches, and the unused AXLE_SUPPORT_EMAIL import. Added a dedicated
  `vppExport` currentMode enum Option so a trigger can fire on "VPP export active"
  (previously reused the startExport token). No behaviour change to the self-drive.
5.28.0 — VPP self-drive. The plugin now drives the export itself for each announced
  Axle window (T-2min on -> end+2min off) instead of waiting for Axle's cloud dispatch,
  which proved unreliable (10-Jun-2026 no-show; Axle confirmed a SigEnergy-API fault).
  Axle settle on the meter reading so self-export counts identically (~£1/kWh, stacking
  with Octopus Outgoing 12p). Manager override now returns ACTION_VPP_EXPORT (was a
  self-consumption stand-down); night_export fires on VPP_ACTIVE entry and is re-asserted
  idempotently each manager tick; discharge floor = next-day reserve; no grid import.
  The Axle release-watcher (45/60-min alerts) is bypassed — dead code retained for now,
  to be removed in a follow-up cleanup once proven over a couple of live events.
5.27.0 — Octopus single source of truth (retires octopus_tracker_rate.py). The plugin
  now writes the rate + slot-JSON variables the openmeteo battery optimiser consumes —
  elec_rates_today_json / elec_rates_tomorrow_json (raw Octopus slots), tracker_rate_today
  / tracker_rate_tomorrow, tracker_product_code/name, tracker_last_updated/fetch_status —
  from its own octopus_api fetch, on every octopus refresh. New
  octopus_api.get_active_rate_schedule() returns today+tomorrow raw slots for the ACTIVE
  tariff (tariff-agnostic, correct for non-Tracker users); plugin._write_tariff_schedule_
  variables() emits them (tomorrow only once published). Removes the two-Octopus-clients
  duplication; the standalone script's Indigo schedule ("Octopus Tracker Daily Rate") is
  disabled and the script kept on disk as a fallback. Verified live: plugin writes fresh,
  correct values (fixed the script's stale tracker_rate_today). 126 tests pass.
5.26.2 — Low-severity sweep (clears the review queue bar the octopus consolidation):
  • prepare_to_sleep now also resets a raised discharge cutoff to the health floor —
    a flood-prev/storm/VPP floor left high would lock the battery and force overnight
    grid import across a long Mac sleep.
  • Modbus power-limit setters (charge/discharge/export) clamp to a 100kW sanity
    ceiling before writing watts to the inverter (was lower-bound only).
  • Poll loop sleeps min(modbus_poll_s, 10) so the advertised 5s "very live" interval
    is actually honoured (was a hardcoded 10s).
  • Seasonal + storm overrides log only on state change (were spamming every 60s).
  • web_dashboard 1.3: NaN/Infinity-safe JSON (one bad float no longer breaks the live
    update), calendar view state hoisted to <script> scope (selected year survives the
    5s refresh), Back link host-relative (was a hardcoded LAN IP).
  • Tests: modbus harness uses a no-op throttle sleep (suite 123s → ~11s); new
    test_plugin.py (config-coercion helpers). 126 tests pass.
  DEFERRED (needs its own session + decision): retire octopus_tracker_rate.py by having
  the plugin write elec_rates_*_json itself (single source of truth vs octopus_api).
5.26.1 — Medium-severity hardening batch (follow-up to 5.26.0's highs):
  (A) runConcurrentThread wraps each _tick so one bad tick task (modbus/forecast/
      VPP) is logged and retried, not fatal to the whole polling loop; _as_float/
      _as_int now coerce their fallback too, so a string default ('94') can't leak
      into arithmetic (e.g. _as_float(blank,'94')/100.0) when a field is blank.
  (B) battery_manager._estimate_consumption_until indexes the 48-slot profile by
      LOCAL time, not the UTC hour (was 2 slots off in BST).
  (C) openmeteo_forecast skips a day-shifted disk cache (a pre-midnight cache whose
      "today" buckets are yesterday's) on restart instead of serving stale day data.
  +8 regression tests (battery 62, modbus 27 incl. signed negative/boundary decode).
  Advisory scripts: octopus_tracker_rate v1.5 (re-read secrets each run), optimiser
  v3.8 (Pushover msgSound/msgPriority + honest day+2-beyond-horizon log), axle diff
  analyser v1.1 (de-nest f-string + None-register guard).
5.26.0 — High-severity bug-fix batch from the comprehensive review:
  (1) Pause feature was dead — sigen_manager_paused / Pause action set a flag
      nothing read, so a "Paused" manager kept driving the inverter. Now gated
      in _evaluate_manager (skips evaluate/verify/act); pause returns the
      inverter to self-consumption (KPI-safe), seeds from the variable at
      startup, and resumes with an immediate re-evaluate.
  (2) Partial Modbus read could feed the manager a phantom 0% SOC (a dropped
      SOC register left the key missing → consumers .get(...,0.0)) → force-charge
      that never completes / keeps importing. read_all() now returns None on any
      missing critical register so _poll_modbus keeps the last-known-good snapshot.
  (3) battery_manager TOU cheap-window detection used the UTC clock vs local-time
      window strings (1h off in BST) — now converts to Europe/London first.
  (4) Open-Meteo partial fetch (some arrays missing) was stamped OK and clobbered
      the good cache, producing a low total that triggered needless import — now
      flagged "Partial N/M" and never overwrites a complete forecast.
  +6 regression tests (108 pass).
Changes:     v5.25.4 (01-06-2026) — Live Power Flow diagram polish: uniform
             r=38 circles (Solar/Home/Grid/Battery), Grid kW enlarged to match
             Home with an Import/Export/Idle line below it, Battery shows kW
             over % (swapped), Home/Grid kW aligned on the flow axis. This
             commit also catches the repo up from 5.24.1 — the 5.25.0–5.25.3
             dashboard work was live/installed but had never been committed.
             v5.24.1 (28-05-2026) — silence the one spurious red ERROR per
             restart: `device "Battery Manager" state key currentMode not
             defined (ignoring update request)`. The currentMode List-enum is
             registered ASYNCHRONOUSLY after stateListOrDisplayStateIdChanged()
             in deviceStartComm, so the FIRST evaluate write (which batches
             currentMode with ~20 other keys) raced the registration and
             logged one harmless ERROR (rest of the batch applied fine). The
             init-state seed already correctly omitted currentMode (v5.24.0);
             this extends the same guard to the evaluate write — currentMode is
             now appended to the batch only once `"currentMode" in dev.states`,
             so the first post-restart tick skips it and the next (<=60s) writes
             the real mode. No alarming red line, nothing lost.
             v5.24.0 (28-05-2026) — added currentMode List-enum state to the
             Battery Manager device so Indigo auto-generates one BoolTrueFalse
             sub-state per mode (currentMode.solarOverflow etc.). Users can now
             trigger directly on "battery entered night-export" without a
             string compare. Additive: currentAction keeps its friendly display
             string. deviceStartComm now re-fetches the device after the state
             list refresh so the new enum + sub-states register cleanly.
             v5.23.0 (27-05-2026) — added prepare_to_sleep / wake_up overrides
             harvested from the 27-May plugin_base.py sweep. Mac sleep used
             to leave the inverter in whatever forced mode it was last set
             to (force-charge, night-export) — an 8-hour overnight Mac sleep
             while the inverter sat in force-charge would overcharge the
             battery. Now: on sleep, return to self-consumption (safe
             baseline), save accumulators, stop dashboard, disconnect
             modbus. On wake, restart dashboard, force last_modbus and
             last_manager to 0 so the next tick polls + re-evaluates
             immediately rather than waiting the full interval. The
             SAFE-on-sleep behaviour is the most important operational
             improvement in this version — biggest win of the harvest.
             v5.22.1 (27-05-2026) — octopus_api._parse_tou_slots: BST-aware
             UTC→Europe/London conversion no longer depends on pytz being
             importable. Now prefers stdlib zoneinfo (always available on
             Python 3.9+) and only falls back to pytz, with None as last
             resort. Fixes test_battery_manager regression where a UTC
             23:30 Go cheap slot in summer was misclassified as standard
             because the test environment lacks pytz; production Indigo
             installs (pytz in requirements.txt) were already correct but
             this hardens the path so the test environment matches.
             v5.22.0 (27-05-2026) — battery_manager.py 3.4 → 3.5: plan-object
             decision-audit pattern lifted from mlamoure/indigo-auto-lights.
             Decision dataclass gains an audit_trail field populated at every
             branch (CONTEXT, BALANCE, OVERRIDE, RESILIENCE, FLOOD-PREP,
             IMPORT, OVERFLOW, RELEASE-OVERFLOW, DEFAULT) — both matched AND
             considered-but-skipped branches recorded.  _log_manager_decision
             now dumps the audit block after the action-change INFO line,
             once per action transition (not per-poll) so no log spam.  Same
             shape applied to openmeteo_battery_optimiser v3.6 and
             octopus_tracker_rate v1.2 the same day.  Existing 16+ unit
             tests in test_battery_manager.py unaffected (audit_trail
             defaults to empty list).
             v5.21.4 (26-05-2026) — openmeteo_forecast.py 1.3 → 1.4: one-shot
             retry on transient network errors (Timeout, ConnectionError,
             ChunkedEncodingError — the last covers Open-Meteo's occasional
             SSL UNEXPECTED_EOF hiccups). Transient blips now log at WARNING
             not ERROR; cache fallback and 3-of-4 array path unchanged.
             v5.21.2 (23-05-2026) — millisecond timestamp [HH:MM:SS.mmm]
             prefix on every log line via plugin_utils.install_timestamp_filter().
             Matches Device Activity Monitor convention. New "Toggle
             Timestamps in Log" menu item.
             v5.21.0 (22-05-2026) — magnitude-conditional bias correction in
             openmeteo_forecast.py (module bumped 1.2.1 → 1.3). Analysis of
             31 days showed err% vs forecast_kwh r = -0.462: the model
             under-forecasts on moderate-prediction days (25-45 kWh,
             ratio 1.18-1.28) and over-forecasts on bright days (>55 kWh,
             ratio ~0.93). A flat factor (v1.2's experiment) cancels these
             out; a 5-band table (centres 17.5/30/40/50/65 kWh, median
             actual/forecast per band, linear interp) follows the shape and
             projects MAPE 19.8% → ~14-16%. Bands recomputed nightly from
             accuracy records (rolling 60 days). Per-day factor applied to
             both correctedTodayKwh/Tomorrow and to every hourly slot in
             openmeteo_forecast.json so the battery optimiser sees the
             corrected shape. New JSON fields: biasFactorToday,
             biasFactorTomorrow, biasBands. 24 unit tests; plugin restart
             clean.
Changes:     v5.19.2 (15-05-2026) — Live Power Flow visual polish (option B):
             soft teal aurora glow + horizon bar behind the card; two
             status chips top-right ("On Grid" / "Lockout" / "Grid Down"
             and the current manager mode, with VPP override during an
             event); richer node labels — battery shows "0.98 kW · Charging"
             / "0.50 kW · Discharging" / "Idle", grid flips to Sigenergy-
             app ordering "0.94 kW · Exporting". No new data sources;
             everything is already in /api/status.
Changes:     v5.19.1 (15-05-2026) — Live Power Flow card now uses kW with
             2 decimals (e.g. 980 W -> 0.98 kW) for all four nodes (solar,
             battery, home, grid). Other cards retain the existing W/kW
             auto-switching format.
Changes:     v5.19 (15-05-2026) — Export sync check (Sigenergy vs Octopus).
  • New /api/export-sync endpoint and Export Sync dashboard card. Compares
    the inverter's daily export kWh (from daily_history.json) against the
    half-hourly readings settled by Octopus on the export MPAN, for the
    last 7 fully-settled days. The most recent 3 days are skipped because
    Octopus typically takes 24-48 h to settle.
  • Tolerance ±5% — anything wider is flagged as drift.
  • New OCTOPUS_EXPORT_MPAN / OCTOPUS_EXPORT_SERIAL keys (IndigoSecrets.py
    first, PluginConfig fallback). Feature silently disabled if absent.
  • Octopus client gets get_export_kwh_for_date(date, mpan, serial)
    returning {kwh, slots}; reuses the same _paginate + auth path as the
    consumption-profile call.
  • Results cached on self.store["export_sync_cache"] for 6 h to avoid
    hammering the Octopus API; the dashboard refreshes hourly.
  • Once-a-day INFO line at midnight: "[ExportSync] 7d avg diff +0.8%
    worst: 2026-05-08 +3.1%" (or "[DRIFT >5%]" suffix). Skipped silently
    if the export MPAN isn't configured.
  • Show Plugin Info / self-test now lists OCTOPUS_EXPORT_MPAN +
    OCTOPUS_EXPORT_SERIAL in the secrets table.
Changes:     v5.18.2 (14-05-2026) — VPP event post-mortem: states + Pushover.
  • New _summarise_vpp_event() parses the per-event JSONL file at the
    VPP_ACTIVE -> COOLING_OFF transition and computes:
      export_kwh, pv_kwh (avg watts * duration), min_pv_w, max battery
      discharge W, peak grid export W, "PV survived" flag (min_pv_w > 100),
      and the set of distinct emsWorkMode strings observed.
  • Nine new states on the axleVppMonitor device (Devices.xml):
      lastVppDate, lastVppExportKwh, lastVppPvKwh, lastVppMinPvW,
      lastVppMaxBatteryDischargeW, lastVppPeakGridExportW,
      lastVppPvSurvived (Boolean), lastVppEmsModes, lastVppLogPath.
    Lets the user spot at a glance whether Axle's strategy worked
    without having to read the JSONL file.
  • Pushover at event end carries the headline numbers AND a pre-formed
    "Ask Claude" block listing the JSONL path and four pointed questions
    the user can paste straight into Claude Code for analysis. Priority 0
    (vibrate, respects quiet hours).
  • One concise grep-able summary line goes to the Indigo Event Log; the
    per-minute snapshots remain JSONL-only (no log noise).
  • Summariser is best-effort: any failure logs WARNING but never blocks
    the COOLING_OFF state machine.
  • COMPANION: a daily scheduled Claude task ('vpp-event-morning-analysis')
    is intended to read the newest JSONL each morning and produce a
    written analysis (mode/registers/limits Axle used, can we copy it?).
    The prompt is preserved in this commit's notes; create with
    mcp__scheduled-tasks__create_scheduled_task when next interactive.

## v5.51.1 — 21-07-2026

LOG-LEVEL FIX. indigo.server.log(level=...) wants a Python
logging INT — a STRING is silently ignored and the line logs as plain Info.
The log() helper passed its level name straight through, so every WARNING and
ERROR raised through it had been appearing as an ordinary Info line. Added
_lvl() to map the name to a real level. Estate-wide sweep (38 files).

Changes:     v5.18.1 (14-05-2026) — quiet the VPP event log.
  • Per-minute VPP/Axle snapshots moved OUT of the Indigo Event Log
    and INTO a per-event JSONL file under <data_dir>/vpp_events/,
    filename derived from the event start time (e.g.
    2026-05-15_0800.jsonl). One JSON line per minute during the
    window plus an "announcement" line at the start and an
    "event_ended" line at the close. Lets the file be parsed after
    the event (eg `jq -c 'select(.type=="snapshot")' file.jsonl`)
    to see exactly what Axle did with the inverter — without
    drowning the live log.
  • Indigo Event Log during an event now only carries the key state
    markers: announced, T-10min warning, T-5min RELEASED CONTROL,
    VPP WINDOW ACTIVE, event ended, REGAINED CONTROL.
  • _write_vpp_event_header() writes every field Axle's API returned
    to the JSONL file once at announcement time.

Changes:     v5.18 (14-05-2026) — TRUE Axle handoff via Remote EMS release.
  • v5.16 + v5.17 were both stop-gap measures that had the plugin drive
    the export through mode selection (0x06 or 0x02+charge_limit=0).
    Both held Remote EMS enabled, which BLOCKED Axle's cloud channel
    and forced us to pick among simple Modbus modes that can't do what
    Axle's cloud can (e.g. simultaneous battery discharge + PV charge).
  • v5.18 properly releases Remote EMS at T-5min before event start
    via modbus.disable_remote_ems(). With Remote EMS off the inverter
    follows Sigenergy's cloud commands directly — Axle now controls
    the inverter the way other Axle+Sigenergy users see, including
    keeping PV running through battery export.
  • Pre-export step (T-4min mode 0x06) removed — replaced by the early
    T-5min release so Axle has lead-time to dispatch.
  • Minute-by-minute countdown spam ("[VPP] Event in N min - preparing"
    every minute from 60 min out) removed. Single T-10min warning
    instead. T-30min pre-charge trigger unchanged.
  • New >>> RELEASED CONTROL TO AXLE <<< marker at T-5min, and
    >>> REGAINED CONTROL <<< marker when Axle releases the inverter
    in COOLING_OFF. Easy to grep.
  • _log_vpp_snapshot() fires once per minute during VPP_ACTIVE,
    dumping SOC / PV / battery / home / grid power + EMS mode +
    charge/discharge limits. Lets us see exactly what Axle is doing
    for post-event analysis.
  • Full event-detail dump on announcement (every field Axle's API
    returned) — useful for learning what the dispatch metadata
    contains.
  • Verify loop reverted to skip during VPP_ACTIVE/COOLING_OFF: the
    plugin is in observe-only mode for those states; any write would
    fight Axle.
  • COOLING_OFF logic unchanged — _vpp_check_axle_release() watches
    for emsWorkMode containing "Self" (the inverter falls back to
    Max Self Consumption when Axle finishes), then re-enables Remote
    EMS and logs REGAINED CONTROL.

Changes:     v5.17 (14-05-2026) — DAYTIME VPP fix follow-up.
  • v5.16 fixed the export-stops-at-event-start bug by setting mode 0x06
    (Discharge ESS First) for the VPP window. Export resumed at 4 kW,
    but PV dropped to 0 W (curtailed by the inverter — mode 0x06 makes
    the battery do all the discharge, and with grid capped at 4 kW
    there is nowhere for PV to go, so the MPPT shuts down).
  • For daytime VPP the right mode is 0x02 (Max Self Consumption) with
    charge_limit pinned to 0 W. PV can't be diverted to charge the
    battery, so PV exits via the AC side and exports to grid; battery
    only discharges if PV is insufficient to meet (home + grid_cap).
    Net effect: 4 kW grid export from PV (free), battery preserved
    for later, no PV curtailment.
  • Modbus sequence in _vpp_transition(VPP_ACTIVE):
      set_self_consumption()  → mode 0x02, charge/discharge limits 10kW
      set_charge_limit(0)     → battery can't absorb PV
  • _verify_ems_registers maintains both registers throughout the event.
  • Log line updated: "PV exports to grid, battery fills any shortfall
    (no PV curtailment)".

Changes:     v5.16 (14-05-2026) — VPP event handoff fix.  CRITICAL.
  • Symptom (14-May-2026 morning VPP event): pre-export started correctly
    at 07:56 with mode 0x06 (Discharge ESS First) — battery exporting.  At
    08:00:55 the plugin's _vpp_transition(VPP_ACTIVE) called
    set_self_consumption() to "clear solar overflow cap before handing
    control to Axle".  That call switched the inverter from mode 0x06 back
    to 0x02 (Max Self Consumption), STOPPING the export.  Axle then could
    not override because Remote EMS was still locked to the plugin — so
    for the rest of the event the battery charged from PV (4 kW) instead
    of exporting.  Result: 0 kWh exported during the paid VPP window when
    ~10 kWh should have flowed.
  • Root cause: the plugin used to assume Axle would take Modbus control
    after the transition and drive the discharge itself.  In practice Axle
    uses Sigenergy's cloud channel, which is blocked while Remote EMS
    holds the lock.  The "handoff" model never worked end-to-end.
  • Fix: switch to plugin-driven export through the VPP window.  Axle
    measures via the smart meter, not by sending commands.  Specifically:
      1. _vpp_transition(VPP_ACTIVE) now calls night_export() (mode 0x06,
         10 kW discharge limit) instead of set_self_consumption() —
         idempotent if pre-export already set the mode; rescues the
         late-detection path where pre-export never ran.
      2. _verify_ems_registers() now actively maintains mode 0x06 during
         VPP_ACTIVE (was previously skipping all writes, allowing drift
         if anything else touched register 40031).
      3. VPP_ACTIVE -> VPP_COOLING_OFF entry now calls set_self_consumption()
         to cleanly close the export and return to Max Self Consumption.
         The "waiting for Axle to release" log line is gone; there is no
         handback to wait for in the plugin-driven model.
  • Backward-compatibility: VPP_COOLING_OFF logic (_vpp_check_axle_release)
    left intact — it'll see "Self Consumption" in emsWorkMode immediately
    after our explicit set_self_consumption() and complete the cool-off
    phase in normal time.
  • Test plan: at next VPP event, expect mode 0x06 to persist from
    pre-export through event end with no gap; grid should be exporting
    at ~10 kW with battery discharging; at event end, mode returns to
    0x02 and normal self-consumption resumes.

Changes:     v5.15 (13-05-2026) — publish auto-calibrated consumption
             profile in sigen_site_config.json:
  • _write_site_config() now includes a "consumption" block with
    hourly weekday/weekend kWh derived from the 48-slot inverter
    profile (only when 48 valid slots are accumulated).
  • _refresh_consumption_profile() republishes the site_config after
    each refresh so the JSON stays current.
  • Lets openmeteo_battery_optimiser.py (v2.10+) replace its old
    Octopus-grid-only profile (~11 kWh/day, wrong) with the plugin's
    real-load profile (~22 kWh/day, right).
  • Background: the Octopus smart-meter export only sees grid imports,
    so for a solar+battery house it massively under-counts true home
    consumption.  This is the root cause of yesterday's incident.
Changes:     v5.14 (13-05-2026) — expose tomorrow solar/need on
             BatteryManager device:
  • New Devices.xml states tomorrowSolarKwh and tomorrowNeedKwh published
    every manager tick by _update_manager_device, computed from snapshot
    using the SAME logic battery_manager._calculate_24h_balance() uses
    (tomorrow_weekday + weekday_kwh/weekend_kwh).
  • Lets external scripts (openmeteo_battery_optimiser.py v2.9+) read the
    plugin's actual flood-prevention inputs instead of computing their own
    and ending up with a different ratio.
  • Background: 12-May-2026 the optimiser's 20:00 Pushover promised an
    overnight pre-drain export (40 kWh solar / 11 kWh typical = 3.6x),
    but at 00:27 the plugin's internal view was 63 kWh / 22.4 kWh = 2.81x
    — just below the 3.0x FLOOD_PREV_FORECAST_MULT gate. No export ran.
    Both sides used the same constant; the inputs differed because the
    plugin's auto-calibrated weekday_kwh (~22) is biased high by spring
    heating-on data still in its rolling 48-slot profile, while the
    script used a May seasonal value (~11). Aligning their inputs is
    cheaper and lower-risk than retuning the calibration.
Changes:     v5.13 (12-05-2026) — help tooltips on every static label.
Changes:     v5.10 (12-05-2026) — compact forecast chart with hover tips:
  • Hourly forecast SVG shrunk from 130px to 80px high (~60% shorter).
  • kWh labels above each bar removed (less visual noise).
  • Hover any bar to see a custom floating tooltip with the hour and
    exact kWh value, with glassmorphism panel + glow.
  • Bars highlighted on hover for clear visual feedback.
  • Native SVG <title> retained as accessibility / no-JS fallback.
Changes:     v5.9 (12-05-2026) — live polling + dashboard cadence:
  • Modbus poll interval is now actually wired up to PluginConfig
    (was hardcoded). Default lowered 60s -> 10s so the dashboard sees
    fresh data within ~10s of any change. Range 5-600s. Watt-integration
    fallback in _accumulate_daily_energy uses the live value too.
  • PluginConfig dropdown gained 5/10/15s options at the top with clear
    "live" labelling; 30/60/120s remain for low-traffic setups.
  • Dashboard auto-refresh tightened 30s -> 5s. Number tweens (added in
    v5.8) now appear visibly continuous as PV/grid/battery watts shift.
  • One-time pref migration: existing installs sitting on the legacy 60s
    or 120s default get bumped to 10s on next startup; users who chose
    30s explicitly are left alone.
Changes:     v5.8 (12-05-2026) — dashboard glamour pass:
  • Glassmorphism: cards now have semi-transparent backgrounds with
    14px backdrop blur over a soft drifting radial-gradient backdrop
    (slow 28s drift). Cards lift on hover with a subtle outer glow.
  • Headline numbers (SOC %, solar benefit £, tariff rate) get a coloured
    text-shadow glow that matches the value — green for SOC / benefit,
    amber for tariff, red when benefit goes negative.
  • Smooth number transitions: SOC % and solar benefit £ tween between
    old and new values with a 700ms easeOutCubic instead of snapping.
  • Live-pulse: header timestamp now leads with a green pulsing dot to
    show the data is fresh.
  • Cards fade-in on initial page load (staggered 50ms apart).
  • SOC ring stroke now eases between values with a soft glow filter.
  • Sparkline added to the SOC card — last 24h SOC trend with low/high
    caption, gradient-filled SVG, glow on the line.
  • Skeleton shimmer class available for any future "loading" placeholders.
  • Tabular numbers everywhere KPIs live to stop digit jitter.
Changes:     v5.7 (12-05-2026) — true day-by-day rates + forever retention:
  • daily_history.json retention: cap removed entirely. Records are kept
    forever (~280 bytes each — 50 years of daily data is < 6 MB).
  • Each daily record now persists both `rate_today_p` (import rate) AND
    `export_rate_p` (export rate that was live on that day). Historical
    economics now value every day at the exact pence/kWh it was paid /
    earned on — future export-tariff changes will NOT retroactively
    re-value past days at the new rate.
  • All three roll-up paths (today, yesterday, period totals, calendar
    months) prefer the per-record export_rate_p; live/current rate is
    only the fallback for older records that pre-date this change.
  • Existing 43 records back-filled with export_rate_p=12.0 (Octopus
    Outgoing has been flat 12p since 26-Mar-2026; all current records
    fall after that date).
Changes:     v5.6 (12-05-2026) — calendar-month breakdown on the dashboard:
  • New "<year> calendar months" card — Jan-Dec table for the current
    calendar year, same five economics columns as the period card. Each
    month row shows total + per-day average; current month flagged
    "(partial)"; months with no data show "—".
  • Year-total footer row sums every populated month.
  • Year selector tabs (one button per year with any data, always
    including current year) — click switches the calendar card to a
    historical year without disturbing the rest of the dashboard.
    Backed by new endpoints /api/calendar?year=YYYY and /api/years.
  • daily_history.json retention bumped from 365 to 3650 days (~10
    years) so prior years aren't lost. Each record is ~250 bytes so
    10 years ≈ 1 MB JSON — negligible.
  • Periodic /api/status refresh only updates the calendar card if the
    user is viewing the current year — historical-year selections are
    preserved across the auto-refresh tick.
  • New `economics.calendar_months` block in /api/status (current year).
Changes:     v5.5 (12-05-2026) — period totals on the dashboard:
  • New "Period totals" card under the bottom row, listing Week / Month /
    Year roll-ups of all five economics fields (solar benefit, net grid,
    without-solar, import paid, export earned). Each cell shows the
    period total as the headline number and the per-day average underneath.
  • Window definitions: Week = last 7 days; Month = current calendar
    month so far (variable day count); Year = last 365 days.
  • Each historical day is valued at its own saved rate_today_p (Tracker
    rates change daily); export rate is assumed flat 12p across history
    (Octopus Outgoing 12p has been live since 26-Mar-2026).
  • New `economics.periods` block in /api/status with totals + averages.
Changes:     v5.4 (12-05-2026) — dashboard yesterday economics:
  • New "Yesterday" card alongside "Today's Cost" — same five fields
    (import paid, export earned, net, without-solar, headline benefit)
    for a full-day reading, since today's view is partial until midnight.
  • Reads daily_history.json's most recent entry; uses the saved
    rate_today_p (falls back to today's live rate if older entries lack it).
  • Refactored economics calc into a shared `_compute_daily_economics`
    helper so today and yesterday use identical maths.
  • /api/status `economics` block restructured to:
      {"today": {...}, "yesterday": {...}, "yesterday_date": "YYYY-MM-DD"}.
  • Bottom-row grid switched to auto-fit/minmax so it now accommodates
    5 cards (Decision / Today Summary / Tariff / Today Cost / Yesterday)
    without media-query gymnastics.
  • BUG FIX: home_daily_kwh was being overwritten with 0 when the inverter's
    register 30092 reset at its midnight (which can race the plugin's local
    midnight handler). Every record in daily_history.json had home_kwh ~= 0
    instead of the true ~15-20 kWh. The accumulator now ignores a sudden
    drop in the inverter counter and lets _check_midnight reset the store
    value at the right moment. Going forward, the daily history will be
    accurate; existing past records cannot be retroactively corrected.
Changes:     v5.3 (12-05-2026) — daily economics on the dashboard:
  • New "Today's Cost" card showing import paid, export earned, net today,
    what the day would have cost without solar, and the headline solar
    benefit (£) — i.e. counterfactual cost minus actual net cost.
  • New `economics` block in /api/status — all values in GBP, plus the
    import/export pence rates used in the calculation.
  • Also: new menu item "Open Web Dashboard" — logs the URL (clickable
    from any Indigo client) and best-effort browser launch on the server.
Changes:     v5.2 (12-05-2026) — three small additions:
  • Web dashboard charts (Chart.js via CDN). New 24h/48h/7d SOC + energy
    stacked-bar charts, and a 30-day daily totals bar chart. Backed by two
    new endpoints: /api/history?hours=N (half-hourly slots from SQLite) and
    /api/daily?days=N (daily_history.json).
  • Weekly tar.gz backup of the data dir at Monday midnight. Backs up
    accumulators.json, daily_history.json, soh_history.json,
    home_load_profile.json, forecast_accuracy.json, energy_timeseries.db
    and the openmeteo combined cache. Retains the 8 most recent (~2 months).
  • Auto-update notifier: GitHub releases API check on startup, daily-cached
    in pluginPrefs.lastUpdateCheck. Logs an INFO line if a newer plugin
    version is published. Silent on network failure.
  • Also: fixed double-count bug in Show Today's Energy Summary +
    Show Manager Status — both used correctedTodayKwh (whole-day forecast)
    labelled as "remaining". Now read remainingTodayKwh.
Changes:     v5.1 (12-05-2026) — site config consolidation:
  • New shared sigen_site_config.json published to Python Scripts/ on every
    plugin start and every PluginConfig save. Companion optimiser script reads
    it so battery / inverter / flood-prevention values can no longer drift.
  • Fixes confirmed drift bug: optimiser FLOOD_PREV_FORECAST_MULT was 4.0
    while plugin used 3.0 — could give "no pre-drain" advisory while the
    plugin actually pre-drained.
  • PluginConfig: new siteArraysJson field — per-array specs as JSON list,
    strict-shape parsed at startup with ERROR log on bad JSON (falls back to
    built-in ARRAYS).
Changes:     v5.0 (12-05-2026) — major hardening and feature pass:
  • Threading lock around self.store (tick + action callbacks)
  • Web dashboard now joins thread + server_close on shutdown
  • SQLite timeseries connections use timeout=5.0 + try/finally
  • Octopus rate-limit tracker (warns >80/hr, hard-stops >95/hr)
  • Modbus writes are read-back-verified (warns on mismatch)
  • JSON parse guards on every response.json() in Octopus / Open-Meteo / Axle
  • Kraken token cleared on any failure path so stale tokens cannot persist
  • battery_manager.evaluate() refactored — _check_overrides /
    _check_resilience_buffer extracted, dead v4.0 night-export branch removed
  • Site coordinates moved to PluginConfig (siteLatitude / siteLongitude)
  • Variable folder ID cached after first lookup
  • _ensure_plugin_log throttled to hourly (was every tick)
  • ServerApiVersion bumped 3.0 -> 3.8 (Indigo 2025.2 native)
  • pymodbus pinned to >=3.0,<4.0
  • EMS mode 0x07 ("AI Mode") added to decode table
  • Axle: forecast_dispatch_kwh / estimated_revenue_p surfaced when present
  • Auto-calibrated weekday/weekend kWh from live inverter consumption profile
  • New menu items: Run Self-Test, Show Power Cut Log
  • Dashboard: tomorrow_surplus_kwh, tomorrow_revenue_gbp, forecast_accuracy
  • Battery State-of-Health weekly snapshot + degradation warnings
  • Power cut event log (rolling 100, surfaced via menu + dashboard)
  • Variable-driven pause/resume via sigen_manager_paused
  • Pushover quiet hours + configurable sound
  • Forecast accuracy 7-day MAPE rolling summary
Date:        10-05-2026
Version:     4.9 (prior)
