# SPEC — Weekend Happy Hour import (SigenEnergyManager Phase 3)

Status: **SIGNED OFF and BUILT in v5.83.0** · drafted 03-Sep-2026 · target v5.83.0
Decisions taken by CliveS 03-Sep-2026: passive fill only · count-and-tag the import ·
full inverter charge rate.

## 1. Purpose & success

During a **booked** Octopus Weekend Happy Hour, charge the battery from the grid at full
inverter power so the free electricity is banked rather than wasted.

**Success:** on the next booked happy hour, the plugin imports for the window, stops cleanly at
the end, records how many free kWh it banked, and disturbs nothing else in dispatch.

**Honest value.** Slots are Sunday 11:00–14:00 BST — peak solar, when the battery is often
already at target. A sunny September Sunday leaves ~0–1.75 kWh of headroom (~£0.44); an October
one maybe 9–12 kWh, capped by the inverter at ~10 kWh (~£2.50). Across the promotion's remaining
Sundays, realistically **£8–15 total**, and **the promotion ends 1 November**. This is a small,
short-lived feature and is deliberately scoped as such.

## 2. Scope

**In:**
- Detect a joined `WEEKEND_HAPPY_HOUR` window (plumbing already shipped in v5.82.0).
- New pref `happyHourImport`, **default off**.
- New `ACTION_HAPPY_HOUR_IMPORT` in `_check_overrides`, **strictly below the Axle VPP override**.
- Drive grid charge at inverter max for the window; stop at the configured target SOC.
- Stand down at window end with a **confirmed** hand-back, plus an independent overrun backstop.
- Record free kWh banked, tagged separately from ordinary import.
- State the reason in the decision when it declines.

**Out (v1), deliberately:**
- **Auto-booking the slot** — an account action on Octopus's website, and CliveS's to click.
- **Pre-drain to manufacture headroom** — deferred by decision; revisit after one October hour
  shows whether the summer case is worth the extra cycle and the earlier dispatch interference.
- Shifting house loads (the plugin has no such lever).
- Any change to how self-sufficiency itself is computed.

## 3. External-system contract

**Nothing new.** Same `savingSessions` query on `KRAKEN_GRAPHQL_BACKEND`, same raw-token auth.
A booked slot is exactly `direction == "WEEKEND_HAPPY_HOUR" and joined is True`.

Verified against the live account 03-Sep-2026: Octopus offers **four 1-hour slots each Sunday**
(10:00–14:00 UTC) and only the booked one carries `joined=True` — Sun 16 Aug 12:00Z was booked,
its three siblings were not. Unbooked siblings must never be driven.

## 4. Device model

No new device. New states on `batteryManager`:

| State | Type | Purpose |
|---|---|---|
| `happyHourActive` | Bool | driving the free-hour import right now |
| `happyHourFreeKwhLast` | Number | free kWh banked in the current/most recent window |

Plus `currentMode` gains `<Option value="happyHourImport">Happy Hour Import</Option>` in
Devices.xml **and** the matching `ACTION_MODE_TOKEN` entry. `TestCurrentModeTokensMatchDevicesXml`
(added v5.81.1) already fails if one is added without the other — that test exists because
v5.81.0 shipped a token with no Option and the trigger sub-state silently never existed.

## 5. The six architecture questions

1. **State ownership.** The window cache is written only by `_check_saving_sessions`. The
   "am I driving" flag only by `_act_on_decision`. Free-kWh is measured from a single anchor —
   the cumulative grid-import counter captured at window entry — persisted in `accumulators.json`
   so a restart mid-window cannot double-count or lose it. No fact stored twice.
2. **Failure isolation.** The whole branch is wrapped; any exception logs and falls through to
   normal evaluation. Import must never be left running: the stand-down latches on the **flag,
   not the clock**, and the hand-back is confirmed with the `vpp_handback_pending` retry
   (the v5.64.0 lesson — never latch on an unconfirmed write).
3. **Config-blank safety.** Pref read through `plugin_utils.as_bool` (a saved checkbox comes
   back as the string `"false"`, and bare `bool()` calls that True). Any kW figure via
   `_as_float`. Absent or unparseable means **off**, never on.
4. **Idempotency + loops.** Re-asserting the same charge command each cycle is a no-op at the
   inverter, as the VPP drive already relies on; a deadband on the charge-limit write stops
   register spam.
5. **Termination.** Bounded three ways: the window end; the battery reaching target SOC; and an
   independent overrun backstop on the manager cycle mirroring `_check_vpp_overrun` (v5.62.0 —
   added precisely because one path ending a window is one path too few).
6. **Test seam.** Pure `happy_hour_import_kwh(soc_pct, capacity_kwh, target_soc_pct,
   charge_kw, window_hours, fair_use_cap_kwh)` → kWh worth importing, 0.0 for "don't".
   Contract-tested and mutation-checked before it drives anything.

## 6. Safety interactions

- **Axle VPP always wins.** Branch sits below it, so an overlapping window has already returned.
- **A turn-down and a happy hour cannot sensibly co-occur.** If both flags are somehow set, do
  **nothing** and warn — fail closed rather than guess which way to push.
- **Export gating does NOT apply.** Unlike the turn-down branch, this imports, so a post-power-cut
  lockout or storm suppression is irrelevant to it. A storm actively *wants* a full battery, so
  free charging is aligned rather than conflicting. Stated explicitly because it differs from
  the sibling branch and the difference is easy to "harmonise" away by mistake.
- **Fill to the configured target (95%), not 100%.** CliveS's standing rule is 95, with 100
  reserved for storms. Free electricity is not a reason to override a rule that exists to protect
  the pack — flagged here rather than silently decided.
- **The 16 kWh fair-use cap cannot bind** in a 1-hour window at ~10 kW. Tracked and honoured
  defensively anyway, so a future 2-hour slot or a raised inverter limit cannot walk past it.

## 7. Threading, secrets, config

- **Threading:** nothing new. Existing 10s tick + 60s manager cycle. The manager path does no
  network I/O — it reads only the cache the hourly/10-minute poll leaves behind.
- **Secrets:** none new; same Octopus API key.
- **Config:** one checkbox, `happyHourImport`, default off, in the existing
  OCTOPUS SAVING SESSIONS section.

## 8. Test plan

- Pure function: no headroom · partial headroom · already at target · cap unreachable ·
  cap artificially binding · unknown/None inputs never guess.
- Override precedence: Axle wins; turn-down + happy hour together fails closed; pref off means
  never; unbooked sibling slot never drives.
- Lifecycle: stand-down on window end, on reaching target, and via the overrun backstop;
  confirmed hand-back and the retry path.
- Mutation checks on every guard above, each sabotage asserted to have applied and each restore
  verified byte-identical, `__pycache__` cleared between runs.

## 9. Release plan

v5.83.0 — feature bump: Info.plist, plugin.py header, docs/plugin-changelog.md, README changelog
row, `Plugins/CLAUDE.MD` entry, GitHub release with the zip. Ships **off**; CliveS enables it.
