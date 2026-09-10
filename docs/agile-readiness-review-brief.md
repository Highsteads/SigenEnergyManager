# Brief: adversarial review of the Agile import path

**Written** 10-09-2026 · **For** a Fable 5 session, ~1h45m · **Scope** targeted, not a full
`/plugin-deep-review`

---

## The job in one line

Find what will go wrong when SigenEnergyManager starts buying electricity on Octopus Agile,
**before** 1 October, when it starts doing it with real money.

---

## Why this, why now

- The switch request fires **16-Sept** (`agile-switch-request-sept-2026`), targeting a
  **1 October** start.
- Winter import here is roughly **660 kWh/month at 20-30p**. The modelled Agile-vs-Tracker
  swing is **£50-60/month**. All of it flows through `_plan_agile_import`.
- The check that was booked to verify this — `agile-first-arbitrage-check-oct-2026` — runs on
  **26 October**, i.e. 26 days into being billed. It is an autopsy, not a gate.

**Every time anyone has looked at this path they have found something.** That is the argument
for looking once more with a better model:

| when | what was found |
|---|---|
| v5.44.0 | planner shipped; imported unconditionally, ignoring the ~6% round-trip loss |
| v5.59.0 | `get_agile_rates()` and `_plan_agile_import()` had **never been joined** — the planner always fell into its no-rates branch. Both halves present, never wired. |
| v5.98.0 | the path was ungated for rehearsal and, in four minutes, the same tariff-price bug surfaced in **three** places (`_refresh_rates`, `_build_tariff_data`, `_update_tariff_device`) |
| v5.98.1/.2 | those three, plus the change-detector going **silent** rather than wrong on Agile |
| optimiser v3.21 | `len(slots) >= 48` classifier broke on Octopus's routine 46-slot publication |

---

## The thing this brief exists to say

**The 07/08-Sept rehearsal could not have tested the decision path, and its "everything else
clean" verdict must not be read as if it had.**

`_plan_import` is reached only when there is a deficit — `battery_manager.py:1285-1287`:

```python
import_kwh   = max(0.0, tomorrow_need_kwh - available_tomorrow_kwh)
import_needed = import_kwh_grid >= MIN_IMPORT_KWH
```

Live state at 09:19 today, with the rehearsal override **still on** (`tariffActive: "agile"`):

```
tomorrow: 65.6 kWh avail, need 20.8 kWh   ->  import_kwh = 0
importActive: False    importScheduled: False
currentReason: "24h sufficient — surplus 25.0 kWh"
```

So the rehearsal has proven the **plumbing** — rates arriving, slots parsed, the right price
displayed, the tariff classified. It has not executed one line of `_plan_agile_import`, and in
September it cannot: there is no shortfall to plan for. The regime that exercises this code is
winter, and winter has not happened yet.

This is [[feedback_a_fixture_only_tests_its_own_regime]] with a *live rehearsal* as the fixture.
Treat every "clean" report about Agile so far as scoped to the summer regime.

**Corollary: the first execution of this code will be an unattended overnight in October, on a
cold night, with a real bill attached.** That is the event to make safe.

---

## Test coverage, measured

The plugin has **1,277 tests**. Exercising the Agile *import decision*: **four**, all in
`TestAgileBreakEven` (`test_battery_manager.py:1403`), all added by v5.44.0 for the round-trip
gate alone. Every other Agile test is rates plumbing or display.

`_agile()` in that class builds a fixture with **one overnight price and one daytime price** —
three slots at 22p, three at 23p. A real Agile night is 20-odd distinct prices with a shape.
**A flat fixture cannot see a bug in slot selection**, which is precisely what slot selection
does. Same family as the `.mjs` harness that fed identical samples to a min/median/mean picker.

---

## Where to look

Primary — `SigenEnergyManager.indigoPlugin/Contents/Server Plugin/`:

| file | lines | what |
|---|---|---|
| `battery_manager.py` | 1372-1412 | `_plan_import` — the tariff dispatch |
| `battery_manager.py` | **1580-1676** | `_plan_agile_import` — **the money** |
| `battery_manager.py` | 1680-1694 | `_agile_daytime_reference_rate` |
| `battery_manager.py` | 1255-1310 | `_calculate_24h_balance` — the gate that decides a deficit exists |
| `plugin.py` | 5196-5260 | the executor: `START_IMPORT` / `SCHEDULE_IMPORT`, and the stored `import_scheduled_time` |
| `plugin.py` | 5822, 5895, 7765 | the other places that clear that stored time |
| `octopus_api.py` | 486-522 | the slots join (the v5.59.0 fix) |
| `octopus_api.py` | 362-421 | `_forced_tariff_info` — the rehearsal override |
| `octopus_api.py` | 1735-1780 | `_fetch_rate_schedule` — the day-window construction |

---

## The questions, ranked

Ranked by money at risk. Answer with evidence, and say plainly when the answer is *this is fine*.

### 1. What actually stops an import, and can it run into an expensive slot?

**Measured 10-09-2026: `ACTION_STOP_IMPORT` is never returned by `battery_manager.py`** — it
is defined at line 192 and no code path produces it. But `plugin.py:5238` carries a live handler
for it, and that handler is where the import teardown lives:

```python
elif action == ACTION_STOP_IMPORT:
    if prev_import:
        log("[Manager] Import complete - returning to self-consumption")
        self.modbus.set_self_consumption()
        self._restore_import_cutoff()
        self.store["import_active"] = False
```

So the teardown — including **`_restore_import_cutoff()`** — sits in a branch nothing can reach.
Something else must therefore be ending the charge and restoring that cutoff, or neither is
happening. Establish which, and specifically:

- What ends a charge in practice — the inverter reaching `target_soc`, or a later tick returning
  `SELF_CONSUMPTION`?
- Does the import cutoff get restored on that path? If the cutoff is the power-cut reserve
  backstop, a charge that ends without restoring it leaves the floor moved.
- Is this dead branch a leftover, or a teardown that was silently orphaned by a refactor? Either
  way it is a live lead, not tidying.

The October brief records the greedy design as known and accepted: *"it starts at the cheapest
slot and charges forward to target, so it can run on into dearer half-hours."* **Do not re-report
that as a discovery. Do price it.** The open question is whether greedy-forward is the right
design at all, and that is an economics question, not a code question:

- Agile's single cheapest slot is often a **spike down** that is not contiguous with the cheap
  block. Charging forward from it climbs.
- Picking the **N cheapest slots** in the window, contiguous or not, is the obvious alternative,
  and the executor already has a scheduling mechanism.
- Quantify it against **real region-F winter Agile rates** (Oct 2025 - Feb 2026, via the Octopus
  API) and a 20 kWh overnight need. If greedy costs under about £2 a season, say so and drop it —
  a correct answer that the design is adequate is a good outcome and closes the question.

### 2. Does the reference rate survive the publication schedule?

`_agile_daytime_reference_rate` falls back to `tariff.today_rate_p` when tomorrow's slots are
absent. Octopus publish tomorrow at ~16:00, and the log shows `tomorrow 0 slot(s)` all morning
today. So an evening plan made before 16:00 compares tomorrow's overnight against **today's**
shape.

- How often does the planner run against a fallback reference in practice?
- `today_rate_p` on Agile is the **current half-hour**, not a daily mean — at 09:19 today that
  was **36.183p**. Compared against an overnight rate, a peak-hour reference makes almost any
  import look like a bargain. Is that what happens? Is it a real bias, and in which direction?

### 3. The no-rates branches import 10 kW immediately. What reaches them?

Four branches in `_plan_agile_import` return `ACTION_START_IMPORT` at `power_watts=10000` with no
price gate at all: no slots, no future slots before dawn, no viable slot, plus the unknown-tariff
fallback at `1414-1423`.

**This is not hypothetical.** From the plugin log, 09-Sept 03:05:17:

```
WARNING [Octopus] Tariff override is set to 'agile' but no live product could be found
for it, so there are no rates to plan from ... the planner falls back to its no-rates branch.
```

The override warns, correctly. **Does the real (non-override) path warn as loudly when the
product probe fails, or does it fail quietly into the same branch?** A silent fall-through to
"import 10 kW now" on a winter evening buys the peak. Also: is the WARNING alone enough, given
nothing pages on it — should this be a Pushover?

### 4. Can it buy during the 16:00-19:00 peak?

Agile's peak is the worst half-hour of the day to import. The window filter is
`if now < dt < dawn_dt`. Trace whether any path — a late deficit, a re-plan, a stale
`import_scheduled_time`, the round-trip gate declining and then something else firing — can put
an import inside it.

### 5. Interaction with everything else that drives this inverter

Agile import is new company for code that already has several claims on the battery. In each
case: who wins, and is that right?

- **Axle VPP** — pre-charge at T-30, self-drive from T-2min. A VPP window against a scheduled
  Agile import.
- **Octopus Saving Sessions** — v5.99.0 gave the shared driver its own register ownership.
- **Bank-first export hold** (v5.79.0+) and **flood prevention** — both move SOC with intent.
- **The power-cut reserve floor** — `floor_kwh` from `health_cutoff_pct`.
- The greedy chain assumes the battery is still free at the next slot.

### 6. Negative prices

Agile goes negative. `test_negative_agile_price_survives` exists (`test_plugin.py:5883`) but
covers *display*. Does `sorted(available_slots, key=lambda x: x[1])` plus the round-trip gate do
something sensible at a negative rate — where importing is paid, so conversion loss is
irrelevant and the correct move is to fill the battery hard? Or does `rate / efficiency` on a
negative number invert the comparison?

---

## House rules that apply

- **Never assume — check the source.** Read the producer before trusting a name, a unit or a
  comment. Everything asserted here was measured on 10-09-2026 and is re-checkable.
- **Mutation-sweep every guard you touch or add.** A guard that cannot fail is the recurring
  fault in this estate. Clear `__pycache__` before every run; a restore inside the same mtime
  second runs stale bytecode against clean source. An anchor matching != 1 is **SKIPPED**, never
  a pass.
- **Ask of any new guard whether an input can pass the one above it and fail this one.** v5.90.0
  shipped a min-end floor that was dead code because the physics release above it always spoke
  first. If nothing can reach it, it is a comment.
- **Give a filtered-out record a value that would move the answer if it leaked in**, or the
  fixture cannot tell you the filter works.
- **Price both sides of an unknown.** The safe-sounding default is not automatically the cheap
  one — see the daylight-vs-dark curtailment that cost an afternoon of solar.
- **A negative finding states the scope searched, never the conclusion.**
- Reply shape: answer first, bullets, no process narration. UK English.

---

## Deliverable

1. **A ranked findings list.** For each: the failure scenario in concrete terms (inputs -> wrong
   outcome), the money if it fires in a winter month, and the fix. Separate **CONFIRMED** from
   **PLAUSIBLE** and say which is which.
2. **Tests first, for anything confirmed.** Each verified failing against the pre-fix code before
   the fix lands. The Agile fixtures need a **real price shape** — 20+ distinct half-hourly
   prices from a genuine region-F winter night, not two flat bands.
3. **A verdict on question 1** with a number attached, so the greedy design is either defended
   or replaced on evidence.
4. **The changelog and memory updates**, per the standing rules.

## Boundaries

- **Do not restart the plugin.** It drives the battery; CliveS restarts this one.
- **The rehearsal override is currently ON** (`tariffActive: "agile"` while billed on Tracker).
  Leave it on — it is how the path stays testable — and say so if anything changes it.
- Ship fixes as normal releases with the full ritual. Anything needing hands on hardware, or a
  decision only CliveS can make, goes to `TRIAGE_QUEUE.md`.
- Budget is ~1h45m. If it runs short, **finish the ranked list and the tests for what is
  confirmed** rather than half-fixing several things — a finding with a test is durable, a
  half-applied fix is not.

## If it is all fine

Say so in a couple of lines and do not manufacture concerns. A defensible "this is sound, here is
the reasoning, here are the tests that now pin it" is a good use of the allocation, and it is
worth far more before 1 October than after.
