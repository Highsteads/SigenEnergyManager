# plugin.py — structural analysis

Measured 25-Aug-2026 against v5.77.1 by AST analysis rather than estimated. Every number
came from a script. Re-run them before relying on them again, because they move with the code.

## The shape

176 methods on one `Plugin` class. Classifying each by how it touches Indigo:

| | methods | lines |
|---|---|---|
| lifecycle — Indigo dispatches to these by name, they stay | 46 | 1,601 |
| no Indigo reference in the method body | 89 | 3,274 |
| of those, still free of Indigo **transitively** | 51 | 2,036 |
| reach Indigo through a `self.` call | 38 | 1,238 |

Transitive reachability is the number that matters. A method with no `indigo.` in its own
body can still reach the API through a helper — `_apply_vpp_event` (183L) and
`_end_vpp_export` (64L) both look pure and both fire Indigo triggers. Any estimate of "how
much could move" has to close over the call graph, not scan bodies.

## The class is clusters, not a tangle

Only **four** methods are called by six or more others, and every one is small:
`_find_device` (5L, 15 callers), `_save_accumulators` (15L, 8), `_trigger_event` (17L, 7),
`_set_import_cutoff` (11L, 6). **76 methods have exactly one caller.**

Few shared helpers plus many leaves is the good case — there is no dense core to unpick.

## Shared state

`self.store` holds **89 keys**, touched by 78 of 176 methods.

- 17 keys touched by a single method
- 22 touched by two or three
- 50 touched by four or more
- 69 have more than one writer

Two different situations sit inside that.

**Coherent.** The daily-accumulator group — `pv_daily_kwh`, `home_daily_kwh`, the grid
counters, `peak_soc`, `min_soc`, `peak_pv_*` and the `*_lifetime_start_kwh` set — shares
**exactly the same four writers**: `__init__`, `_load_accumulators`, `_accumulate_daily_energy`,
`_check_midnight_impl`.

**Contested.** `export_active` has **14 writers** and `import_active` **9** — every action
callback, the VPP driver and the decision path write them directly. `vpp_state` is read by
19 methods. These are control-mode flags without an owner.

## Concurrency

`self._state_lock` appears **35 times** and guards a documented model (v5.45.0) across three
domains: `runConcurrentThread`'s background thread, the main thread serving callbacks and
device updates, and the `ThreadingMixIn` HTTP threads serving `/api/*`. `test_concurrency.py`
contract-tests it.

Anything taking ownership of `self.store` state inherits that model. The existing tests assert
that `_evaluate_manager` takes the lock, not that module-held state is guarded, so a change
here needs its own test.

## Persistence

`accumulators.json` is the restart-critical whole-plugin snapshot, not a daily-counter file:
`_save_accumulators_locked` writes **28 store keys** atomically — daily counters, storm state,
power-cut state, eight VPP keys and `export_active` — and `_load_accumulators` is itself one
of the writers of `export_active`. It also calls `_save_home_profile()` as a side effect.

`daily_history.json` is touched by **14 methods**, the widest ownership spread of any store.

`sigen_site_config.json` and `sigen_flood_preview.json` are **published contracts** — the
companion optimiser script reads both — so their shape is fixed.

## Test coverage

`test_plugin.py` references 67 of 176 methods; **4,261 of 8,157 lines are unreferenced**, and
the largest unreferenced methods are the ones any restructuring would touch:
`get_dashboard_data` (318L), `_init_modules` (178L), `_compute_export_sync` (177L),
`__init__` (165L), `_check_midnight_impl` (107L), `_accumulate_daily_energy` (104L).

The harness builds a plugin with `Plugin.__new__` and calls methods directly on the instance,
so a method that leaves the class needs its test updated with it.

## Proving a change is behaviour-neutral

For a comments-only change, `ast.dump(tree, include_attributes=False)` is the strongest proof
available: attributes off excludes line numbers, so an identical dump means nothing but
comments moved. That is what was used for v5.77.1.

For code that moves, the published artefacts are the fingerprint — but `/api/status` is built
from live inverter readings and never repeats, so any comparison has to run against a frozen
fixture rather than a live capture.
