# Flux controller — native Octopus Flux support

Status: released in v5.110.0, **disabled by default** — two switches and a verified site import
limit arm it. Commissioned and running on the author's installation since 17 September 2026;
see the commissioning record at the end.
Developed in an isolated worktree from `c8775b5` (5.108.0) and brought across on
17 September 2026, when the account went live on paired Flux.
Claude Code implemented the planner and plugin integration; Codex implemented the
executor, independently reviewed the integration and corrected the final call paths.

The agreed policy is **20% reserve, no extra 2 kWh or other arbitrary buffers**.
Actual household needs and announced export commitments are energy requirements,
not additional contingency buffers. Preparation is for standard paired Flux.

## Behaviour

- 02:00–05:00: size charging from chronological household demand, solar forecast,
  losses, battery capacity, available charging time and announced events. Keep
  solar headroom where it is compatible with supplying the house and events.
  Add discretionary trading energy only when profitable after losses and wear.
- 16:00–19:00: export only spare energy above the reserve, remaining household
  demand to the next cheap window and committed events. Account for the fact
  that mode 5 prevents battery charging from the PV the forecast predicts.
- Outside those controls: return to the existing manager with the policy floor.
  Household consumption may spend its own budget; discretionary exports may not.
- Axle, joined Octopus sessions, manual commands and emergency controls have
  priority. Announcing an event reserves energy without taking hardware ownership.
- An event can consume its own allocation. A later event remains protected.
  Overlaps use the physical union of export requirements, with the current
  dispatch's contribution subtracted, so only an unserved tail remains reserved.
  Late announcements and cancellations change the planner's commitment inputs.
- One physical exported kWh can earn ordinary tariff revenue and a separate event
  reward. Neither physical energy nor either payment should be counted twice.

Axle pre-charge is checked through the real `_start_vpp_precharge` path: the
cutoff is written while state is still ANNOUNCED. The event is passed explicitly
to the floor calculation; Flux releases before that cutoff write. The following
state transition cannot restore an older baseline over the new event floor.
Shortfall reporting uses the actual reserve and later commitments.

## Implementation and evidence

| Component | Files |
|---|---|
| Chronological planner | `flux_strategy.py`, `test_flux_strategy.py` |
| Acknowledged execution and durable ownership | `flux_execution.py`, `test_flux_execution.py` |
| Plugin ownership, inputs and event integration | `plugin.py`, `test_flux_supervisor.py` |
| Account-paired rates and strict agreement proof | `octopus_api.py` |
| Raw driver reads | `sigenergy_modbus.py` |
| Independent review regressions | `test_flux_review_contract.py` |

The full repository suite passed **1,772 tests, zero failures, errors or skips**.
Ruff and `git diff --check` passed; PluginConfig XML parses. Default enable,
commissioning and site-limit verification switches are false; reserve defaults to 20%.

The independent cases include back-to-back Axle 18:00–19:00 and Octopus
19:00–20:00, overlaps, late events, cancellation, actual pre-charge ordering,
shortfall reporting, DST, charge reachability, household consumption, PV banking,
stale forecast/account evidence and realistic driver staging latency. Tests use
fixtures and recording transports; none of this is physical commissioning.

## Ownership and recovery

The executor journals before starting, pre-arms and reads cutoffs, stages limits,
then verifies the resulting mode and registers. A failed acknowledgement retains
pending recovery. Disk failure prevents new starts but does not prevent stop attempts.
An already active external owner receives no baseline writes from Flux.

Startup does not lift legacy power limits or the charge cutoff while a Flux claim
needs recovery. Driver replacement uses the executor's `rebind()` interface and
requires fresh reconciliation. With Flux disabled, the executor and journal remain
through repeated external-owner ticks; once that owner leaves, baseline reconciliation
must succeed before they are removed.

An unresolved release has no timeout that falsely declares a handover. Recovery
continues to retry. Turning the feature off also requests reconciliation; it cannot
guarantee a physical stop while communications or writes remain unavailable.

Fresh observed values may support a lease of at most 60 seconds, bounded by decision
and tariff-window expiry. The real driver's throttled staging is longer than ten
seconds in the simulated transport test. **A software lease is not a hardware timer.**
SOC cutoffs limit energy but do not prove a timed stop after communication loss.

## Input evidence

Import and export codes come from the same fresh Octopus account response, using
the configured meters or a unique unambiguous meter of each direction. Newly
advertised products and tariff overrides cannot prove the billed tariff. Agreement
expiry is checked even within the cache lifetime; ambiguous or undated live
agreements cannot arm the controller. Public price freshness cannot renew account proof.

Forecast age uses `OpenMeteoForecast._cached_time`, the successful generation time,
including when a failed poll returns today's stale cached buckets. Today and tomorrow
use their own solar bias factors. Price coverage, consumption profile age, observation
age, physical headroom and finite numeric inputs are validated before decisions.

## Remaining work before live use (as written for the 5.109.0 draft; see the commissioning record)

1. Review and release through the established Claude/Clive plugin workflow. No
   version bump, commit, deployment, restart or preference change has occurred.
2. Verify this installation's import/export limits, charge/discharge semantics,
   Remote EMS enable and readbacks, and acceptable behaviour when communications
   fail. There is no verified hardware watchdog. These need supervised inverter
   checks, including cutoff, window-end stop and owner takeover/recovery.
3. Arrange the actual supplier switch and verify both billed Flux agreements and
   effective import/export rates. No switch or activation date is arranged here.
4. Observe a complete cheap charge and peak export cycle, including an event day.
5. Replay this native controller over the same historical data if quoting its
   savings. Earlier MILP comparisons are separate evidence and do not prove that
   this rules-based implementation achieves the same result.

**The reserve in a power cut.** Register 40048 is absolute — honoured off-grid as well as
on — so the economic floor is dropped to the health cutoff during a verified outage and the
emergency is allowed to spend the reserve. Backup reserve belongs on 40046 (set to 20%).
**If the host loses communication with the inverter the plugin cannot release that floor at
all**: there is no hardware watchdog, and the cutoff stays wherever it was last written.
That limitation is one reason the commissioning sign-off stays false.

Happy Hour import events are recognised for ownership but the native planner does
not yet reduce earlier charging to exploit all future free energy. That remains
an optimisation opportunity. Account evidence is supplier evidence, not an
independent check of the meter's eventual settlement. Tests with stubbed startup
constructors prove ordering, not live socket behaviour.

## Verification

From the repository root:

```sh
python3 scripts/run_tests.py
python3 -m ruff check .
python3 -m pytest tests/test_version_consistency.py -q
```

At v5.109.0: 1784 tests, no failures, errors or skips; Ruff clean; every XML and the
Info.plist parse. The deployment route and its blockers are in
[flux-changeover-handover.md](flux-changeover-handover.md).


## Commissioning record — 17 September 2026 (Claude, after Codex paused)

Supervised on the live inverter from a separate Modbus client, battery manager paused for
each test and resumed afterwards. Raw samples are in the session scratchpad; the numbers
below are the readings.

| Check | How | Result |
|---|---|---|
| Whole-site import cap, 40040 | Mode 3, charge limit 10 kW, cap 1 kW then 3 kW | Grid import held at 1.00 kW then 3.00 kW, house still supplied; the battery takes the cut. **Passed.** |
| Grid-first charge and PV | Same test | PV read 0 W throughout mode 3 — grid-first charging stops solar. Harmless 02:00–05:00; a stuck mode 3 would cost a day's generation. |
| Hardware watchdog | 3.5 minutes with no Modbus traffic, mode 3 active | Mode and cap both held. **No watchdog**, as documented. |
| Backup reserve 40046 under forced export | Mode 5, 40048 = 1%. Reserve 0.6% below SOC, then 2.0% above | Below SOC: battery discharged 1.8 kW with PV at 2.5 kW. Above SOC: battery held 0 W for six minutes with PV 2.3–2.8 kW and 2 kW of export headroom. **40046 stops a forced export on-grid.** |
| Charge/discharge limits, cutoffs, mode writes | Every write above read back | All acknowledged. |

**The communications-loss limitation is resolved by design, not by a watchdog.** From
v5.109.1 every Flux floor — the reserve, household need, event commitments, the export
sell floor — is written to **40046**, and **40048 stays at the 1% health floor**. The
Sigenergy manual says 40046 does not apply off-grid, where the battery runs to 40048. So if
the host loses touch mid-trade:

- **mid-export**: exports stop at the sell floor (40046), and a power cut that follows can
  still use the whole battery;
- **mid-charge**: charging stops at the charge cutoff (40047), grid import never exceeds the
  16 kW cap (40040), and the cost is the standard rate on energy bought after 05:00 plus any
  solar lost to mode 3 — money, not safety.

**Site import limit: 16 kW, verified.** The installation's electrical certificate records a
100 A supply protective device at 230 V single phase, so about 23 kW. 16 kW (about 70 A) lets the full 10 kW charge run while the house draws up to
6 kW; the highest half-hour house average in the plugin's history is 8.1 kW (6-Jul) and
typically under 5 kW. Because the inverter enforces 40040 at the meter, the grid only ever
exceeds 16 kW if the house alone does, and then the battery takes nothing.

**Armed 17-Sep-2026 12:51 BST** — *Use the Flux strategy*, *Commissioning is signed off* and
*That import limit has been verified* ticked, limit 16 kW. The switches were ticked on the
hardware evidence above; the remaining items are observations of the running controller:
the first peak export, a restart mid-trade, a manual pause mid-trade, the 19:00 window-end
stop, the first 02:00–05:00 charge, and the first Axle or joined Saving Session day.

### The first live peak — 17 September 2026

Observed from 16:00 with the controller armed; each fault below was fixed and installed within the
window (all in 5.110.0).

| Time | Observed | Outcome |
|---|---|---|
| 16:00-16:05 | Flux stood aside all window: a solar overflow export (engaged 14:24) counted as a higher owner, and nothing was logged | Overflow is no longer an owner inside 16:00-19:00; the supervisor now logs when it stands aside |
| 16:07-16:10 | Export ran, but the target power moved every tick and the executor neutralised first, dropping export to 0 kW for 20-30 s each time | Power-only changes are applied in place |
| 16:12 | Plugin restarted mid-export | Mode 5 held while the plugin was down; startup reconciled to the baseline; Flux re-claimed ~90 s later |
| 16:17 | Manager paused mid-export (manual priority) | Flux released before the pause wrote self-consumption; reserve back to 20%, cutoff 1%; re-claimed after the stand-down |
| 16:43-16:48 | When PV alone filled the 4 kW cap, the planned battery power read 0 and the plan handed back ("nothing worth selling") | Export now allows full battery power; the inverter's grid export cap holds the meter at 4 kW |
| 16:51 onward | Mode 5, discharge limit 10 kW, grid 3.98 kW, battery topping up whatever PV and house leave; sell floor 38% on 40046, cutoff 1% | Stable, no per-tick writes |
