# plugin.py — slimming plan

Companion to `decomposition-analysis.md`, which holds the measurements this rests on.

## Where things stand

`plugin.py` is **9,551 lines**, down from 11,534. The developer changelog that had grown to
2,002 lines at the top of the file now lives in `plugin-changelog.md`, which is a better home
for it: the history is easier to read as a document than as a comment block, and the module
starts with its imports rather than with a sixth of its own past.

That was done in v5.77.1 and proved behaviour-neutral by comparing the module's parsed AST
either side of the change.

## What is worth doing next

**`economics.py`** — the cost settlement and the period, calendar and yesterday summaries.
They read `daily_history.json` and produce numbers. They stay off the control path, they do
not touch the contested control-mode flags, and they take no part in the midnight ordering.
That combination makes them the cleanest thing to lift out, and lifting them takes roughly
750 lines with it.

Two things go with that work rather than after it:

- **Characterisation tests first**, written against current behaviour. Half the class is
  untested and the untested half includes what this touches.
- **A frozen `daily_history.json` fixture** as the comparison input, so the before-and-after
  diff of `/api/calendar` and the economics block is meaningful. A live capture never repeats.

Then stop and look again. Each extraction should earn the next one rather than assume it.

## What is deliberately out of scope

- **Ownership of `self.store`.** Splitting state across modules means inheriting the v5.45.0
  locking model, and that is a design decision in its own right — one lock passed down, or
  per-module locks with a documented acquisition order — with a test that proves a
  cross-module snapshot is still atomic. It comes before any such split, not with it.
- **`accumulators.json`.** It is the restart-critical whole-plugin snapshot spanning 28 keys
  across several concerns. If it is ever divided, it wants renaming to what it is, with the
  Plugin owning serialisation and each part offering `to_dict()`/`from_dict()`.
- **The VPP runtime and the control-mode flags.** Both sit on the control path of a system
  that runs a real house. `export_active` having 14 writers is a genuine design point worth
  fixing, but as its own considered change rather than folded into a move.
- **`_act_on_decision`** (226L). Cohesive, on the control path, and large for good reasons.
- **Merging the nine persistence files.** Years of live data, migrated for tidiness.

## The principle

The plugin works, 748 tests pass and there is no dead code. Everything here buys readability,
so each step has to be cheap, provable and easy to abandon — and the cheapest steps come
first. The changelog move was the largest single win available and cost nothing at runtime.
