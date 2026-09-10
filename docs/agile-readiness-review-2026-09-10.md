# Agile import path — adversarial review, 10 September 2026

**Brief:** `agile-readiness-review-brief.md` · **Shipped as:** v5.100.0 · **Model:** Claude Fable 5.1

## Verdict

The path would have lost money in three ways from 1 October, none of them visible in the September
rehearsal, and all three are fixed and pinned by tests that failed against the old code. The greedy
design is replaced, on evidence: it cost **£24.79 a season** against the cheapest contiguous block at
20 kWh a night, well over the £2 the brief set as the threshold for leaving it alone. Two things are
genuinely fine (negative prices, the other claims on the battery). One policy decision is queued
for CliveS.

Everything below was measured on 10 September 2026 and can be re-run:
`python3 scripts/agile_replay.py` pulls the same public rates and prints the same tables.

## The evidence

7,248 real half-hourly prices, region F, product AGILE-24-10-01, 1 October 2025 to 28 February 2026.
Dawn taken as sunrise plus 45 minutes at the site. The plugin's rule replayed exactly: single
cheapest half-hour before dawn, then charge forward at the given rate until the need is met.

| need, rate | single slot (shipped) | cheapest contiguous block | N cheapest, any order |
|---|---|---|---|
| 20 kWh at 9.5 kW | £421.16 | £396.38 (−£24.79) | £390.36 (−£30.80) |
| 10 kWh at 9.5 kW | £200.43 | £194.83 (−£5.60) | £191.64 (−£8.79) |
| 30 kWh at 9.5 kW | £663.02 | £601.39 (−£61.63) | £592.99 (−£70.03) |
| 20 kWh at 6 kW (cold battery) | £445.48 | £401.43 (−£44.06) | £396.10 (−£49.39) |

- On 124 of 150 nights the single-slot start differs from the best block by more than half a penny.
  On 13 the cheapest slot is a spike-down, its neighbours more than 3p dearer.
- Where the cheapest slot falls (local hour, nights): 02:00–04:59 on 79, 22:00–23:59 on 34, 05:00–07:59
  on 24, 00:00–01:59 on 11.
- The round-trip gate never declined an overnight import in the whole winter, under the intended
  reference or either fallback. Winter Agile nights always clear it.
- Daytime, before tomorrow's rates publish: across 2,284 half-hours the shipped fallback (the current
  half-hour) flipped the verdict in 297, every one a refusal on a cheap or negative slot. Today's
  daytime mean flipped none.
- Negative prices: 54 half-hours over 7 days, lowest −4.41p.
- A deficit known from 10:00 fired before 16:00 on 12 nights, and those 12 buys were £2.04 cheaper in
  total than waiting for the overnight slot. Planning on today's slots alone is not a leak.

## Findings, ranked by money

### 1. CONFIRMED — the single-cheapest-slot rule buys the spike-down. £25–£62 a season.

*Scenario.* 29 November 2025: cheapest half-hour 06:00 at 9.64p, on the morning ramp. The plugin
schedules 06:00, the executor charges forward: 13.49p, 13.94p, 17.79p. The trough at 01:30–03:30 sat
under 11p. 5 October 2025: cheapest 04:30 at 2.45p, the last slot of a four-hour trough; the three
after it cost 13–17p while 00:30–02:00 were 2.6–2.9p.

*Fix.* The planner prices every candidate block — as many half-hours as the grid-side need takes at
the inverter's charge rate — and starts where the whole block is cheapest. Every half-hour of the
block must be published. The gate judges the block mean.

*Residual, accepted.* The N cheapest half-hours in any order would save a further £6 a season and
need an executor that stops and restarts. Not built.

### 2. CONFIRMED — four branches imported 10 kW with no price. Up to ~£5 an occurrence.

*Scenario.* A winter evening with a deficit. The Octopus products listing fails (rate-limit guard,
HTTP error, timeout, 404). The probe logged it at DEBUG and returned None, the slot fetch returned an
empty list without a word, the rates refresh replaced the slots the planner already held with that
empty list, and the planner started a full-power import "now" — at 17:00, the dearest half-hour of
the day. The same branch fired at 03:05 on 9 September under the override, and the override's own
warning is the only reason it was noticed. Three sibling branches did the same on "no future slot
before dawn", "no safely reachable slot" and "unknown tariff".

*Fix.* The probe warns. Agile uses the account's own product code, as Tracker already did. An empty
fetch keeps the last good slots and warns once per outage. The planner holds on self-consumption when
it cannot price a charge, flags it, and the plugin logs a WARNING and sends one Pushover a day. The
reachability filter is gone: a battery that meets its floor before the cheap block puts the house on
grid for its own load, about 0.3 kWh an hour at the evening rate, not 19 kWh at it. Agile has no
resilience floor either way (see the decision below).

### 3. CONFIRMED — the reference rate before 16:00 was the current half-hour. Harmless in winter, wrong in principle.

*Scenario.* Tomorrow's daytime mean is unpublished until about 16:00. The gate fell back to the price
of the half-hour in force. At 17:00 that is the peak, and any import beats it. At a lunchtime plunge
it is negative, and every import loses to it, including the negative slot itself, which the planner
could not buy because the slot in progress was excluded from its window.

*Fix.* Fallback is today's daytime mean, the same twelve-hour window a day earlier. The slot in
progress is a candidate.

### 4. CONFIRMED — what ends an import, and the four exits that left the ceiling pinned.

`ACTION_STOP_IMPORT` has not been returned since the v4.0 sufficiency model. The teardown lives in the
self-consumption branch (target reached) and the unconditional target check at the foot of the
executor, and both restore the hardware charge cutoff. The dead branch was a leftover and is removed.
But solar-overflow entry, Force Export, Set Self-Consumption and Return to Local EMS all cleared the
import flag without restoring the cutoff, and the verify pass re-asserts the recorded cutoff every
minute — so an import interrupted by dawn kept register 40047 at the overnight target plus 3% as the
PV charge ceiling for the rest of the day. All four restore it now.

### 5. CONFIRMED — a schedule armed while an import was running. Small.

The window excluded the slot in progress, so mid-import the planner re-emitted a schedule for the next
half-hour. Stored, that time outlived the import and could fire a second charge seconds after the first
reached target, with a 12% cutoff so little energy moved, but a mode write and a misleading log line.
No longer armed while an import runs.

### 6. PLAUSIBLE, small, not changed

- The block is planned at the inverter's 10 kW. A cold battery taking 6 kW stretches the charge over
  more half-hours than planned. The replay at 6 kW still shows the block beating the single slot by
  £44, so the choice of start is robust; the residual from planning at the wrong rate is unquantified.
- The import may start up to five minutes before the block, buying up to 0.8 kWh at the previous
  half-hour's price. With 34 of 150 cheapest slots at 22:00–23:59, that is pennies a night at most.
- Octopus publishes 46 of 48 slots on some days. A block that spans an unpublished half-hour is not a
  candidate, which is conservative and correct; it can only defer a start, never buy blind.

## What is fine

- **Negative prices.** Sorting by rate puts the most negative first, a negative rate divided by the
  efficiency stays negative, the gate passes, and the plugin buys the deficit. Buying more than the
  deficit is trading, which lives in `experiments/agile_trading/` and is deliberately not wired in.
- **The other claims on the battery.** An Axle window and a Saving Session outrank the import in
  `evaluate()`, and a queued import is held through pre-charging and the window. Flood prevention needs
  a forecast three times demand, which never coincides with a deficit. Bank-first sits below import.
  Nothing here changed.

## Decisions for CliveS (in TRIAGE_QUEUE.md)

1. **The winter power-cut reserve is not maintained on Agile.** The resilience buffer fires on flat
   tariffs any time overnight and on Go/Flux inside the cheap window; on Agile it returns nothing, so
   from 1 October the 20% winter buffer does nothing and a night before a sunny day can end on the 1%
   floor. Not a bug, a trade the switch makes silently. It could be extended to Agile by buying the
   top-up in the cheapest block, which the new planner can price.
2. **The first arbitrage check runs on 26 October, 26 days into billing.** A copy on 2 or 3 October
   would catch a first-night fault while it costs pennies.

## Tests and proof

- `test_agile_readiness.py`, 29 tests, run against the unfixed code first: 21 failed, the 8 that
  passed are the both-sides guards. Fixtures are the two real nights above, 34 half-hours each.
- `TestAgileBreakEven`'s fixture changed from three hourly points a side to contiguous half-hours;
  its four assertions are unchanged and pass.
- Mutation sweep: 20 mutations, each written from its consequence, 20 killed, 0 skipped.
- Suite 1277 to 1306, ruff clean, no plugin restart, rehearsal override left on.
