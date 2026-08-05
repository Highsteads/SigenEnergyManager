#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    plugin.py
# Description: SigenEnergyManager - self-sufficiency battery management for
#              Sigenergy solar/battery systems. Replaces SigenergySolar v3.1.
#              Core philosophy: never import from grid unless battery cannot
#              reach next-day solar at minimum SOC. Export to prevent 100% cap.
# Author:      CliveS & Claude Opus 5
# Date:        05-08-2026
# Version:     5.56.0
#
# v5.56.0 (05-08-2026): THE POST-EVENT REPORT WAS ANSWERING A QUESTION WE STOPPED
# ASKING IN JUNE. The VPP summary Pushover still asked what AXLE had done — "Did
# Axle keep PV running through battery export?", "What EMS mode did Axle use?" —
# wording left over from the observe-and-hand-over model that v5.28.0 replaced
# with self-drive. Every window since has been driven by us over Modbus with
# Axle's dispatch ignored, so those questions had a false premise baked in.
#
# Alongside it, `pv_survived` was a bare `min_pv_w > 100` with no daylight test,
# so EVERY DARK WINDOW reported "PV collapsed" — an alarm about the sun having
# set. Both fired together on the 05-Aug-2026 21:00-22:00 BST event: 45 snapshots
# at 0 W PV, a textbook 4.23 kWh export holding grid at -4000 W (+/-50 W) all
# hour, and a notification saying PV had collapsed and asking what Axle had done.
# The report was the only thing wrong with that event.
#
# Now: the PV verdict is gated on the daylight flag latched at VPP_ACTIVE entry
# and reads ran / curtailed / n/a (dark window), with the boolean state kept as
# the "nothing went wrong" flag a trigger wants. The prompt states plainly that
# the export was self-driven and asks about OUR mode choice. New device states
# lastVppPvStatus and lastVppDriver carry what a boolean cannot.
#
# `driver` in the JSONL was `"self" if export_active else "axle"` — it mirrored
# our own intent flag, so it could only ever read "self" once we started driving.
# It never looked at the hardware, and so could not answer the one question it
# existed to answer. It now compares the LIVE mode register against the mode we
# wrote: a mode we did not write, while we hold Remote EMS, means something
# external moved it. Reported per snapshot and summarised, with a WARNING if it
# is ever seen.
#
# _verify_ems_registers gains ONE register during an active window: the bank
# sub-mode's charge cap. In bank (0x02) that cap IS the export mechanism — it is
# what stops the inverter soaking the PV surplus into the battery instead of
# selling it. Drift there would leave the mode register reading a perfectly
# correct 0x02 while the export fell to nothing, and _drive_vpp_export only
# rewrites the cap when the surplus moves by >300 W, so it could stand for the
# rest of the window. This does not reopen the 10-Apr-2026 grid-import incident:
# that was the solar-overflow cap being written over a VPP window; here the
# expected value is the VPP driver's own cap and the check runs only while bank
# is live. Every other limit still stays untouched for the whole window.
#
# AND THE v5.55.3 TIME-ZONE SWEEP NEVER REACHED THIS FILE. That release unified
# battery_manager on _london_tz / _london_localise / _to_london, with stdlib
# zoneinfo preferred, precisely because five hand-rolled copies had left two
# sites silently an hour out. plugin.py still had FIFTEEN of them — including
# _local_today_str(), the midnight-rollover basis, which is the exact bug class
# that release was written about, and _event_is_daytime(), where an hour's error
# flips a dusk-edge window to daytime and runs the mode that curtails PV. All
# fifteen now call the one shared implementation. A missing tz database logs an
# ERROR once and says what it affects, instead of quietly answering in UTC.
#
# The tests were lying in the same two ways as last time: a private _london_today
# helper with its own pytz fallback (so it could disagree with the module for an
# hour every night in BST), and a `("12:40", "11:40")` assertion tolerating a
# pytz-less host — which would have passed against the very bug being removed.
# Both replaced with exact assertions. Suite 434 -> 465, 0 skipped, green with
# AND without pytz.
#
# Still deliberately NOT done, unchanged from v5.55.3: openmeteo_forecast.py
# carries the same pattern and needs the localize(is_dst=False) -> fold=1 mapping
# pinned by tests before it can move. Its own change.
#
# v5.55.5 (05-08-2026): SOLAR OVERFLOW WAS FLAPPING (battery_manager 3.9 -> 3.10).
# Its physics gate — does today's remaining solar exceed the room left in the
# battery — was a hard cut at exactly zero with no hysteresis, so a day sitting on
# that boundary flipped the decision every few minutes. Measured on 05-Aug-2026:
# nine transitions in a day, four inside twenty minutes, at physics surplus 0.0 /
# 0.4 / 0.2 kWh. Each flip writes a full decision audit plus five Modbus registers.
#
# The cost is not just log noise. Export STOPS for every gap, so PV surplus banks
# into an already-high battery with the DNO cap unused — the clipping the feature
# exists to prevent. Live at the time: 86.5% SOC, 7.7 kW PV, grid at -7 W.
#
# It also self-destabilises. Engaging caps the charge, so SOC climbs slower, so
# headroom to 100% stays large, so the surplus falls back under zero; releasing then
# fills the battery fast and pushes it straight back over. Cloud (PV 8146 W ->
# 2152 W in twelve minutes) only adds noise on top of that loop, which is why it
# looked like weather.
#
# Fix is asymmetric and errs late: engaging now needs >= 1.0 kWh of physics surplus
# AND ten minutes since the last release; releasing is unchanged at < 0 and stays
# immediate, so dusk, a storm or a collapsing forecast still stand it down on the
# next tick and no path can hold a stale cap. All four of that afternoon's
# re-engages would have been refused by the threshold alone. Erring late is the
# KPI-safe direction anyway — a kWh kept in the battery beats one exported at 12p.
# SOLAR_OVERFLOW_CAP_DEADBAND_W has always damped cap REWRITES on exactly this
# reasoning; it was simply never applied to the engage/release boundary.
#
# The OVERFLOW audit line now quotes the physics surplus and the threshold it was
# judged against. The old bare "no surplus or conditions not met" was what made this
# take a source read to diagnose. The gate and the log share one definition of that
# number (_overflow_physics_surplus), so they cannot drift apart — the v5.55.3
# lesson, applied before rather than after.
#
# v5.55.4 (04-08-2026): CI has been failing since 02-08 on two ruff F541s — an
# f-string with no placeholders, in the charge-cutoff backstop warning at
# sigenergy_modbus.py:953. Dropped the two stray f prefixes. No behaviour change:
# the string had nothing to interpolate, which is why ruff objected. This repo's
# CI is a syntax check plus ruff and has NO test suite at all, so a lint error is
# the whole gate — worth noting for the most complex plugin in the estate.
#
# v5.55.3 (30-07-2026): A SILENT UTC FALLBACK IN THE DECISION ENGINE (battery_manager
# 3.8 -> 3.9). Chasing why four battery_manager tests failed on the usual runner
# turned up something better than a test-environment quirk: two production sites
# converted to Europe/London with pytz ONLY and, when pytz was missing, fell back
# to returning the UTC value unchanged. Not an error — an answer quietly one hour
# out for the eight months of BST. `_to_local()` returned `dt` unconverted, so
# every caller compared a UTC clock against local wall-clock windows; and the
# overnight-drain midnight boundary was built at UTC midnight, i.e. 01:00 BST.
# The failing tests were not noise, they were the two sites being caught.
#
# ROOT CAUSE WAS DUPLICATION: five hand-rolled copies of the same conversion,
# three with a stdlib-zoneinfo tier and two without. That is also why these two
# were missed when octopus_api got exactly this fix in v5.22.1 (27-May-2026).
# Now ONE implementation — `_london_tz` / `_london_localise` / `_to_london` — used
# by all five, with stdlib zoneinfo PREFERRED over pytz: it ships with Python
# 3.9+, so it cannot vanish when a Packages rebuild fails (this install has had
# that happen more than once), and it has no `.localize()` trap. Attaching a pytz
# zone via a bare `replace(tzinfo=...)` yields LMT, -00:01 for London — the exact
# detail a copied block gets wrong, now impossible to get wrong twice.
#
# LIVE INSTALLS WERE NEVER AFFECTED: pytz>=2024.1 is pinned in requirements.txt
# and bundled in Contents/Packages (2026.1.post1 present), so the working path
# was always taken. This removes a latent wrong-answer path, not a live fault.
#
# The test suite was lying too, in three ways, all fixed: two tests did their own
# `import pytz` and ERRORED before asserting anything; two more SKIPPED silently
# (a skipped test is a test that is not testing); and a `_today_str()` helper
# returned the UTC date where the module returns the London one, which would have
# disagreed for one hour every night in BST. Suite now 434 tests, 0 skipped,
# 0 failures, WITH and WITHOUT pytz — previously 4 failed and 2 skipped.
#
# NOT DONE, deliberately, and flagged rather than half-finished: openmeteo_forecast.py
# carries the SAME pattern (module-level LONDON_TZ + a `PYTZ_AVAILABLE` gate that
# leaves a datetime NAIVE when absent). It cannot be converted piecemeal — its
# dawn parse calls `LONDON_TZ.localize(dt, is_dst=False)` to resolve the autumn
# fold, and zoneinfo expresses that as `fold=1`, not a kwarg. Changing the module
# constant without that mapping would break the once-a-year path. Worth doing as
# its own change with tests pinning the fold equivalence.
#
# v5.55.2 (30-07-2026): "NO EVENT" WAS BEING REPORTED AS A FAULT. Axle signals
# "nothing scheduled" in TWO shapes: a null body, and — from the moment an event
# ends — a full object with every field null:
#   {"start_time": null, "end_time": null, "import_export": null,
#    "opted_out": false, "updated_at": "..."}
# That object is TRUTHY, so it sailed past the empty-body check and landed in
# the malformed-timestamps branch, logging an ERROR every 10 minutes from
# 20:00:47 — one minute after tonight's event, the first in six weeks, ended.
# By 21:03 it had raised 8 consecutive "failures", pushed a Pushover alert
# through Log_Error_Watch, and left the monitor device reading a fault.
#
# The plugin's BEHAVIOUR was right all along (it read the reply as "no event");
# only the reporting was wrong. This misclassification is older than yesterday
# and was harmless while invisible — v5.55.0 made failures visible, which is
# exactly how it surfaced. The visibility is doing its job; the classification
# needed to learn Axle's second dialect.
#
# BOTH timestamps null = no event. Only ONE null, or present-but-unparseable =
# genuinely malformed, and STILL an error — that discrimination is the point,
# and a broader "any null → no event" guard would have quietly lost it.
# +2 tests, 433 -> 434. One EXISTING test had to be moved rather than relaxed:
# it asserted an error for a both-absent payload, which encoded the very bug
# being fixed, so it now uses a present-but-unparseable pair instead.
#
# v5.55.1 (30-07-2026): THE DASHBOARD'S VPP WINDOW WAS A HARDCODED EMPTY STRING.
# `/api/status` published `"event_str": ""` as a literal, from the day the block
# was written, so every consumer that appends it rendered "VPP event announced:"
# and then stopped — the one fact worth showing, WHEN, was the fact missing.
# Live-spotted on the phone within an hour of the feed coming back, which is the
# first time anything had ever taken that branch. New `_vpp_event_str()` formats
# the stored window through `_local_time` (so it matches the device states and
# the log rather than reading an hour early through BST), prefixes the date only
# when the window is not today, and returns "" on a missing or malformed event
# because every caller already treats "" as "say nothing". +5 tests, 428 -> 433.
#
# v5.55.0 (30-07-2026): A FAILING AXLE POLL IS NOW VISIBLE. Axle announced a grid
# event for this evening; the plugin knew nothing about it, and had known nothing
# for six weeks. The token was revoked server-side some time after the 15-Jun
# event (a JWT, but NOT expired — exp is 2053; the endpoint answers
# 401 "Could not validate credentials"), so every poll since had failed.
#
# NOT ONE LINE was logged about it. Two faults compounded:
#   1. AxleAPI was the only API client in this plugin constructed WITHOUT a
#      logger, so it fell back to logging.getLogger("SigenEnergyManager.AxleAPI")
#      — a logger with no handler attached anywhere here. Every 401 was
#      discarded. Compare OctopusAPI and SigenergyModbus, both of which are
#      handed self.logger.
#   2. get_next_event() returns None for "no event scheduled" AND for a hard
#      failure, so even a caller watching the return value could not tell a dead
#      feed from a quiet week. The VPP device read a calm "Standby" throughout.
#
# The silence is the bug worth fixing — a rejected token is Axle's business, but
# six weeks of not knowing is ours. AxleAPI now takes a logger and records
# last_error; _record_vpp_api_status() logs a failure once and then hourly (a
# sustained outage costs one line an hour, not one per 10-min poll) and logs the
# recovery; and the Axle VPP Monitor device carries apiStatus + apiLastOk so the
# state is visible without reading a log at all. +13 tests (all verified failing
# against 5.54.0), suite 415 -> 428.
#
# RESOLVED SAME DAY: CliveS fetched a replacement token from his Axle account and
# put it in IndigoSecrets.py at 14:41; the restart onto this version picked it up
# and the poll went healthy immediately — apiStatus "OK", and tonight's 19:00-20:00
# event was announced within seconds, which is the new machinery proving itself on
# the first try.
#
# GOTCHA THAT NEARLY CAUSED A MISDIAGNOSIS, worth remembering: a plugin host caches
# IndigoSecrets in sys.modules at ITS OWN startup, so the token a host holds is the
# one that was on disk when that host last started, NOT what is on disk now. Testing
# the same key from ClaudeBridge's context (running since 23-Jul) kept returning 401
# AFTER the replacement was in place, because that host still held the old value —
# a sys.path.insert cannot help, the cached module wins. Read the file directly with
# importlib when you need to know what is REALLY on disk, and remember a credential
# change needs a restart of every host that reads it, not just the file edited.
#
# v5.54.0 (26-07-2026): the restore alert now WORKS OUT whether today's solar will
# refill the reserve, instead of leaving the reader to guess which of the two
# release rules applies to them. CliveS asked whether this was possible or too
# difficult — it is neither: the plugin already answers exactly that question
# every 60 seconds, so the alert now asks it once more at send time and states
# the outcome, with both figures (spare vs needed) so the sums can be checked.
# New _solar_refill_outlook() builds the provisional snapshot + 24h balance the
# SAME way _evaluate_manager_impl does inside a lockout window, so the message
# and the decision cannot disagree. Four outcomes, each phrased honestly: solar
# covers it (export restarts within the minute), does not cover it YET (names
# the forecast as a way out), night (does NOT dangle a forecast that cannot
# arrive), and unknown — where it falls back to naming both rules and claiming
# nothing. The needed figure quotes the MARGIN-INFLATED bar (× 1.25), because
# that is the bar actually used; quoting the raw gap would make a "not yet"
# verdict look wrong to anyone checking it.
#
# The same outlook is now logged at restore time. On 26-Jul-2026 export sat
# suppressed for 20 minutes after the restore — manager evaluating every 60 s on
# live data, no errors — and nothing in the log recorded what was being judged,
# so afterwards the only honest answer was "we cannot tell". That gap is still
# UNEXPLAINED and worth a proper look; this line makes the next one answerable.
# +18 tests, 402 -> 415.
#
# v5.53.0 (26-07-2026): the power-cut alerts now carry the whole picture. These are
# the two messages read on a phone during an outage, and they said almost nothing:
# time, off-grid mode, and how long the cut lasted. Everything needed to judge the
# situation — how full the battery is, what the house is pulling, how long that
# lasts, and on a restore what export is doing and both ways the lockout ends —
# lived in a log nobody opens at the time. Both channels now carry the same full
# body. Three pure helpers so the arithmetic is testable without a power cut:
# _backup_runtime_hours (usable energy is everything ABOVE the discharge cutoff,
# since the inverter stops there and overstating backup is worst in exactly this
# message), _format_runtime (minutes / one decimal / whole hours / days, capped at
# "10+ days"), and _lockout_message. Every figure is optional: a paragraph whose
# readings are missing is dropped rather than printed as a zero, so a partial
# Modbus read costs one line and never the alert. The lockout end time is derived
# from the SAME pluginPrefs["powerRestoredTime"] the window itself uses, so the
# time quoted cannot drift from when export actually resumes.
#
# Also fixes a LATENT BREAKAGE IN THE STORM ALERTS, found by ruff while in here
# (F821, pre-existing on HEAD). The v5.45.0 locking restructure split
# _apply_storm_result out of _check_storm_watch and left `loc_name` behind in the
# caller, so the two bodies that quote it — the YELLOW escalation and every
# ALL-CLEAR — raised NameError instead of sending. Amber and red never quote it,
# which is why nothing looked broken. The sting is in the tail: storm_alerted_level
# is written only AFTER a successful send, so a failed all-clear left it stuck at
# the old level and every later storm at or below it was judged "already alerted"
# and stayed silent. Armed but never fired — no storm here since 02-Jul-2026.
# +28 tests total, 374 -> 402; the 3 storm tests raise NameError on the old code.
#
# v5.52.1 (26-07-2026): the grid-restore message named only ONE of the two export
# release rules. It promised "unless SOC >= 85%", wording written before v5.50.0
# added the forecast-aware solar-refill release — so when this morning's 83-second
# cut (grid lost 08:39:29, restored 08:40:52) was followed by export resuming at
# 74% SOC at 09:00:30, the plugin had told the owner one thing and done another.
# The release path itself was already honest (it names WHICH rule fired), but the
# line read FIRST, at the moment of the outage, was not. Now names both. The
# Pushover/email body says nothing about export at all, so it needed no change.
#
# v5.52.0 (25-07-2026): dashboard economics audit — four faults found by checking
# the figures against the live ledger rather than reading the code.
# * "Grid-only" now carries the standing charge. A grid-only home pays the same
#   daily charge, so the counterfactual sat ~£0.62/day (~£225/yr) under the truth
#   while the elec bill printed beside it included the charge. The solar BENEFIT
#   is unchanged — standing cancels in (no_sol + st) - (imp + st) + exp.
# * "Elec bill" now carries the standing charge on UNSETTLED days too. It was
#   unit-only for those, and Octopus settles ~a day in arrears, so a 7-day window
#   nearly always held one. Live proof: the week reported £3.82 against a true
#   £4.44 — one day's standing charge missing, under a header promising
#   "unit + standing".
#   Together these two make the Period totals row a real identity:
#       solar benefit = grid-only - elec bill + export earned
# * /api/daily accepts up to 800 days (was 365). The dashboards' week-on-week card
#   compares against the same week last year by probing offsets 364-370, and a 365
#   cap returned at most one of the seven — the year column could never unlock,
#   however long the history grew.
# * A missing electricity unit rate no longer paints a green "Covered" badge.
#   _wh_build_card billed the standing charge alone, which export nearly always
#   beat; bill/net/covered now come back None so the page can render "—".
# * Calendar months report elec_whole_house_total_gbp too, so that table reads as
#   the same identity as Period totals.
# * The 12p export fallback was written out in nine places — now one constant,
#   DEFAULT_EXPORT_RATE_P, and the `if export_rate_p else 12.0` test that silently
#   swapped a genuine 0p rate for 12p is now an `is None` test.
# * gas_estimated now honours has_gas on the yesterday / day-before cards, so an
#   electricity-only user stops seeing "(est)" on a £0.00 gas line.
# * /api/status publishes battery.capacity_kwh so dashboards stop hardcoding this
#   system's 35.04 kWh pack.
#
# v5.51.2 (21-07-2026): shared plugin_utils.py refreshed to v1.3 — the
# estate-wide propagation of the four Appliance Monitor deep-review fixes.
# * install_timestamp_filter() is idempotent — a second call used to stack a
#   second filter, so every log line came out with two timestamps.
# * `import indigo` is soft, so the module imports outside the Indigo host and
#   can be exercised by offline tests.
# * A malformed log call keeps its arguments in the log instead of dropping
#   them, so a %-placeholder mismatch is visible.
# * New shared as_bool() — a pref re-serialised as the string "false" is
#   truthy, which is exactly the wrong answer.
# This bundle keeps its LOCAL variant: install_timestamp_filter also walks up
# to every reachable handler so module-logger records get stamped. That walk
# is now idempotent too.
#
# 5.51.0 — Daytime charge is paced to a 90% target, not 100% (battery_manager 3.7→3.8).
#   The same root cause as 5.50.0, one layer down: the plugin kept treating 100% as the
#   goal when the owner's requirement is 85-90%. Solar overflow paced the charge to hit
#   100% exactly at dusk, and because `required_charge_kw` is subtracted from export
#   BEFORE the DNO cap is applied, that high target spent the low-surplus MORNING buying
#   SOC out of kWh that would have fitted under the cap — then still met the afternoon
#   peak with less headroom than it started with, and clipped anyway. CliveS, 20-Jul:
#   "I do not need to get to 100%, it means the chance of clipping is greater. Anything
#   above 90% is great, above 85% is still OK", with 80%+ ample for a power cut.
#   The target is a GOAL, not a ceiling. Once export is at the cap the above-cap excess
#   still has nowhere to go but the battery, so it charges straight past the target —
#   the high finish comes free, out of surplus that would otherwise have been binned.
#   Modelled on 20-Jul's measured curve (dull ~5.1 kW morning, breaking clear to 8.59 kW
#   at 14:00): 100% target -> 45.0 kWh exported, 1.53 kWh CLIPPED, ends 91.1%. 90%
#   target -> 46.6 kWh exported, NOTHING clipped, ends 90.8%. Three-tenths of a point of
#   finish for 1.6 kWh of export and the elimination of the waste.
#   New prefs `solarOverflowTargetSoc` (90) + `solarOverflowMinEndSoc` (80), guarded;
#   new snapshot fields solar_overflow_target_pct / solar_overflow_min_end_pct /
#   storm_active. `_apply_storm_override` sets storm_active, which restores the 100%
#   target — a storm is the one time a genuinely full battery is worth clipping for —
#   while KEEPING the lazy pacing, so it is never force-charged out of export when the
#   day's own solar would have reached 100% unaided (CliveS's explicit constraint).
#   NO dull-day guard in the pacing, and this is the interesting bit: the contract tests
#   proved one would be dead code. The physics gate above it only exports when remaining
#   solar EXCEEDS the room to 100%, so whenever overflow runs the day can demonstrably
#   reach 100% — and the gate re-evaluates every tick, so the moment the rest of the day
#   can no longer fill the battery it returns None, export stops and everything charges.
#   A lower target keeps SOC lower, which makes that gate bite EARLIER: the pacing change
#   is self-limiting and the end-of-day level is protected by machinery already present.
#   The floor pref is therefore just a clamp against a mis-set target. Caveat recorded
#   honestly: because the gate cuts export earlier in the afternoon, the real-world gain
#   will be somewhat below the 1.6 kWh the model shows (the model has no gate).
#   +11 contract tests (344 → 355), including a pin that target=100 reduces to the exact
#   pre-3.8 formula, so the change is provably a no-op at the old setting.
# 5.50.0 — Post-power-cut export lockout is now FORECAST-aware as well as SOC-aware.
#   Prompted by a live incident this morning: the grid dropped for 109 SECONDS at
#   05:25:54 (restored 05:27:43) and the standard 4-hour lockout armed at SOC 75.6%.
#   The flat 85% floor (v5.34/5.35) then held export off until 07:36:07 while the
#   battery climbed to the floor — 2h 08m of no export, ~3.3 kWh banked instead of
#   sold. Every one of those kWh was exportable at the time: PV surplus ran
#   1.1-4.25 kW, comfortably inside the 4 kW DNO cap. The bill came due in the
#   afternoon. By 13:28 the battery was at 91.6% with only ~2.9 kWh of headroom
#   against 6.8 kW of PV, so once the washing machine finished the pack would fill
#   and every watt above house+4 kW would be CLIPPED — thrown away, because the DNO
#   cap leaves nowhere for it to go. Without the lockout we'd have been at ~82% with
#   ~6.2 kWh of headroom and clipped nothing. The lockout had converted exportable
#   morning kWh into afternoon curtailment. Underlining it: the overnight optimiser
#   had already computed the day's actual power-cut resilience minimum as 10% (3.5
#   kWh), while the lockout insisted on banking to 85%.
#   FIX: a SECOND, strictly-additional release condition (`_solar_refill_releases_
#   lockout`, pure + unit-tested). Export also resumes mid-lockout once the day's
#   remaining solar, net of house load to dusk, covers the gap up to the SOC floor
#   with a 1.25x margin — because on a bright summer morning holding export off
#   banks nothing the sun wasn't going to deliver anyway. Guarded by a new
#   `powerCutLockoutMinSocPct` floor (default 50): however good the forecast, the
#   early release never applies to a nearly-empty battery, which is not a resilience
#   reserve. Deliberately expressed in kWh, not SOC percent — an SOC-space form
#   (projected >= floor x margin) is unsatisfiable at the defaults since 85 x 1.25 =
#   106 > 100. Replayed against this morning's logged figures it releases at the
#   first daytime tick (~06:00) instead of 07:36; a winter night, a dull December
#   day, an unknown SOC and a below-minimum battery all still hold.
#   Plumbing: `_power_cut_window_active()` extracted from `_resolve_export_lockout`
#   so `_evaluate_manager_impl` can build a provisional snapshot + SufficiencyBalance
#   ONLY inside a lockout window (both calls are pure and side-effect free; outside a
#   window — nearly always — normal ticks pay nothing). `_resolve_export_lockout`
#   takes an optional `balance`; omitting it reproduces pre-5.50 behaviour exactly,
#   and a failure building the balance falls back to the flat floor. The one-shot
#   "export re-enabled during lockout" INFO now names WHICH rule released it, and is
#   no longer able to crash formatting an unknown SOC (reachable when export is
#   disabled mid-window). Dashboard `power_cut` block gains `lockout_min_soc` +
#   `solar_release_active` so the Lockout chip can explain itself.
#   NOT applied to the storm override, despite the two mirroring each other since
#   v5.39: a storm forecast means the solar may not arrive, so releasing export on
#   the strength of a forecast is exactly wrong there. Commented in both places so
#   nobody "restores symmetry" later. +22 tests (344 total, all green).
# 5.49.0 — Solar card figures reconciled. The Energy page read "38.3 kWh today,
#   forecast 53, Remaining 25.3" — figures that cannot be added up. Two causes,
#   both in how remainingTodayKwh was derived (openmeteo_forecast.py 1.6 → 1.7):
#   (a) it was summed off the RAW hourly p50 buckets while the forecast beside it
#   is bias-corrected, so the two sat on different scales (raw 57.7 × 0.915 =
#   52.8); (b) it counted the WHOLE current hour as still to come, overstating it
#   by up to a full peak hour (~7 kWh at midday). Both now owned by one helper,
#   openmeteo_forecast._remaining_today_kwh, which the fetch path and the
#   enrichment path share. This also corrects the "expected total" line in Show
#   Today's Energy Summary and Show Manager Status, which add pvDailyKwh to it.
#   The hourly forecast published to the dashboards is scaled by the same day
#   factor, so the bars and their kWh tooltips now sum to the headline forecast
#   (the optimiser JSON already did this). New "Expected total" figure on both
#   dashboards' solar cards = generated so far + still to come, so the projected
#   end-of-day number is stated rather than left to the reader to work out.
#   NOT touched: forecast_p50 passed to the decision engine stays raw (it is
#   compared against SOLAR_DUSK_THRESHOLD_WH, and scaling would shift dusk
#   detection), and the persisted _hourly_p50_* cache buckets stay raw because
#   the bands are recomputed nightly. 9 new tests (27 → 36).
# 5.48.0 — VPP window survives a plugin restart. The Axle state machine was
#   re-driven purely from the API each poll, so a restart mid-window relied on
#   Axle still returning the active event; if its endpoint drops the event once
#   live (Predbat issue #3051's failure mode) the rest of the window was silently
#   missed. Now the active window (state + event + pre-charge/cutoff/export flags)
#   is persisted into accumulators.json on every _vpp_transition (crash-safe,
#   atomic — pluginPrefs only flush on a graceful shutdown), restored by
#   _load_accumulators regardless of day (a window can span midnight), and
#   _rehydrate_vpp_state() in startup() makes the time-based call: resume an open
#   window WITHOUT the API, or reset the discharge-cutoff register + Self
#   Consumption for a window that ended during downtime. New pure module helpers
#   _serialise_vpp_event / _deserialise_vpp_event / _vpp_resume_decision with a
#   contract test for the restart path. DST-safe throughout (UTC end-to-end).
# 5.47.0 — Octopus Go/iGo readiness (pre-September switch). (1) Go cheap-window
#   corrected 00:30-05:30 -> 23:30-04:30 in TARIFF_WINDOWS (octopus_api.py +
#   battery_manager.py) to match the live GO-FIX product (region F) — the stale
#   window missed the cheap 23:30-00:30 hour and would have charged 04:30-05:30
#   at the 31.36p day rate. (2) Power-cut reserve now guaranteed on TOU tariffs:
#   _check_resilience_buffer fired only on Tracker/Flexible, so on Go/iGo a night
#   before a well-covered (sunny) day left nothing holding the dawn_target floor
#   and the battery drifted to the 1% health floor. It now tops the reserve up on
#   Go/iGo/Flux/iFlux too, but ONLY inside the cheap window (night rate, never the
#   day/peak rate) and ONLY when the import planner is not already covering
#   tomorrow, so it never truncates the arbitrage fill. +3 resilience tests;
#   fixed the calendar-flaky surplus-conservatism test (weekend need). 304 pass.
# 5.46.0 — Gas cost settle: full-day COVERAGE gate (fixes £0.00 gas on the
#   whole-house card). Gas settles slower than electricity and can arrive
#   PARTIALLY: on 03-07 the 1 Jul row froze (cost_settled) at 0.034 kWh / £0.00
#   gas off a single 00:00-00:30 slot — and because the day/yesterday gas
#   ESTIMATE reuses the most recent settled gas_kwh, the bad row poisoned the
#   estimates too (Octopus app showed £0.74/£0.45; card showed £0.00). History:
#   the same freeze happened 21-Jun and was fixed with a 46-slot gate on BOTH
#   fuels; the later daily-read-meter accommodation (H2) relaxed gas back to
#   presence-only, reintroducing it. Fix: _sum_consumption_for_date now returns
#   `complete` — readings reach the end of the local day (90-min tolerance) —
#   which is True for a whole half-hourly day AND for a daily meter's single
#   24h reading, False for a partial day; the settle gates gas on it. The
#   frozen 2026-07-01 row was un-settled to re-settle with complete data.
#   +6 tests (301 pass; the 1 pre-existing failure is the time-of-day-dependent
#   test_calm_night_drain_continues_unchanged flake, unrelated).
# 5.45.0 — Locking-model restructure (the last deep-review-#3 deferral). _tick no
#   longer holds _state_lock for its duration: network stages (modbus/forecast/
#   octopus/VPP/storm/settle) run I/O UNLOCKED and lock only their merge; control
#   stages (evaluate/verify/act, midnight, scheduled import) self-lock whole;
#   get_dashboard_data takes a ms-scale locked snapshot then builds lock-free.
#   NEW test_concurrency.py pins the contract. Bonus bug: the tick stamped
#   last_modbus AFTER _poll_modbus returned, clobbering the v5.43.0 outage
#   back-off (it never worked) — stamps before the call now. Live-verified:
#   dashboard 2.6-10ms during polling (was up to ~20s mid-poll). 288→295 tests.
# 5.44.0 — Decision tuning. _plan_agile_import gates the cheapest viable slot on
#   round-trip break-even (rate/0.94 must undercut tomorrow's daytime reference;
#   None = ungated) — returns SELF_CONSUMPTION passthrough when pre-charging
#   loses money. surplus_kwh conservatism CONFIRMED by CliveS and pinned with an
#   annotation + characterisation test. 283→288 tests.
# 5.43.1 — Deep review #3 batch 3 (~75 lows/infos): Chart.js bundled locally;
#   power-cut state persisted; atomic JSON writes; octopus JWT purge + failure
#   negative-caching; GTI clamps; monotonic throttle; Sigenergy variable folder
#   auto-created; test-quality fixes (tautologies replaced, value-0 decode);
#   companion scripts hardened (optimiser v3.14, digest v1.1, axle v1.3).
# 5.43.0 — Deep review #3 batch 2 (mediums): pause survives restart; staleness
#   guard holds evaluation on frozen inverter data + poll back-off tiers; flood
#   target + power-cut lockout crash-safe in accumulators.json; no phantom 0%
#   SOC at restart; menu/prefs callbacks under _state_lock; connect() health
#   probe + escalating reconnect; storm word-boundary matching; flood gate
#   requires demand>0; Kraken null-token guard; London-day rate windows (BST
#   skew); forecast staleness caps + day-aware persisted bias baseline; month
#   cost vars fixed (1st-of-month zero + whole-house basis).
# 5.42.0 — Deep review #3 batch 1 (highs; 122 confirmed findings across the
#   review, 257→283 tests by batch 3): hardware charge-cutoff (reg 40047)
#   backstop on every grid import (target+3%, verify-maintained, released on
#   stop/disengage/startup — a crash mid-import can no longer grid-charge to
#   100%; hardware-verified 02-07); flood pre-drain aborts when a storm
#   suppresses export mid-drain + stops at max(target, dawn_target); modbus
#   outage aborts the read cycle in ~1s with 2 log lines (was ~20 ERROR lines);
#   storm-feed failure returns None not "none" (level HELD through flaky polls,
#   ~24h decay with its own Pushover).
# 5.41.0 — Publish the Octopus cost/rate variables (REVIVE). The elec_*/gas_*/
#   export_*/account_balance Indigo variables had no active writer since their
#   original script was retired, so they had gone stale — elec_unit_rate_p frozen
#   weeks behind the live Tracker rate (read 11p while the ledger said 25.78p),
#   account_balance_gbp stuck at 0. weekly_home_digest.py reads elec_unit_rate_p /
#   export_rate, and get_dashboard_data's import-rate fallback reads
#   elec_unit_rate_p, so both were silently using stale data. New
#   _write_cost_variables (called from _write_energy_summary_variables, so the
#   long-standing comment that elec_unit_rate_p is written every 30 min is now
#   TRUE) republishes the bill-exact rates + balance from get_account_financials
#   (the Kraken ledger — single source, no duplicate fetch/drift) and today/month
#   costs from the live economics: elec_unit_rate_p, elec_standing_charge_p,
#   gas_unit_rate_p, gas_standing_charge_p, export_rate_p (+ legacy export_rate),
#   account_balance_gbp, elec_today_cost_gbp, gas_today_cost_gbp,
#   export_today_revenue_gbp, combined_today_actual_gbp, elec_month_cost_gbp,
#   export_month_revenue_gbp. Best-effort + fully guarded; a Kraken/economics
#   hiccup leaves the values in place rather than blanking them.
# 5.40.0 — Storm reserve is now a FLAT 50% for ALL levels (was 50% yellow / 80% amber-red).
#   CliveS's call: a storm should keep a 50% power-cut reserve and NEVER grid-charge above it.
#   The overnight resilience-buffer import (flat-rate tariff only) tops the battery to the storm
#   floor when below it; with amber/red previously at 80% a storm night would grid-charge to ~82%
#   (costly, against the self-sufficiency KPI). STORM_SOC_AMBER 80→50 so the floor — and thus any
#   storm-driven grid charging — is capped at 50% (tops to ~52% via the existing +2% anti-cycling
#   guard; solar still fills above 50% for free, and export still reopens at the 85% release).
#   Pushover storm alerts reworded to match (50% minimum reserve, no grid charge above it, export
#   held off until nearly full — the old "export suspended" wording predated the 5.39.0 release).
#   Tests updated for the flat-50 reserve (+1; 73 in test_plugin, 151 across suites).
# 5.39.0 — Storm export suppression is now SOC-aware (mirrors the post-cut lockout floor).
#   The storm override held export OFF for the entire duration of a wind/storm warning,
#   regardless of SOC. With a near-full battery under good solar that rammed it to 100%
#   (charge takes priority over export in self-consumption) and then clipped every watt of
#   PV above the DNO export cap — and it thrashed Solar Overflow on/off every poll. Export
#   is now suppressed ONLY while SOC < STORM_EXPORT_RELEASE_PCT (default 85, configurable via
#   stormExportReleasePct, never below the active reserve target). At/above it the reserve is
#   already banked, so export resumes — Solar Overflow throttles the charge and pushes surplus
#   to grid so the battery creeps up with headroom instead of curtailing. One-shot INFO logs
#   the mid-storm resume. Report exposes storm.export_suppressed + storm.export_release_pct.
#   The openmeteo advisory needs no change — it already defers to the plugin's published
#   export_enabled/would_fire verdict. +8 tests (73 in test_plugin).
# 5.35.0 — Comprehensive numeric telemetry for SQL history + tidy-ups.
#   • All numeric inverter telemetry states (batterySoc, *PowerWatts, temps, cell voltage,
#     SoH, cutoff, daily kWh) changed from ValueType=String to Number/Integer and written
#     as real numbers (guarded via _as_int/_as_float), so Indigo's built-in history
#     (indigo_history.sqlite) records them as chartable columns. Previously every state was
#     a String so nothing but gridOnline charted. Categorical states (emsWorkMode, gridStatus,
#     etc.) stay String. No separate DB (InfluxDB/Postgres) — SQLite + the plugin's own
#     half-hourly energy_timeseries.db cover it.
#   • SOC floor for the post-cut export lockout is now configurable (powerCutLockoutSocFloor
#     pref, default 85, guarded by _power_cut_lockout_soc_floor()).
#   • Cosmetic: the Live Power Flow "Lockout" chip now keys off power_cut.export_suppressed,
#     not the time window — so a battery exporting above the SOC floor shows "On Grid", not
#     "Lockout".  Numeric states carry a clean uiValue (_num_state) so the device UI shows
#     "99.6" not "99.59999999999999".  +5 tests (193).
# 5.34.0 — Power-cut export lockout is now SOC-aware + grid-online SQL state.
#   (1) The 4-hour post-restore export lockout previously killed ALL export, so a
#   near-full battery (e.g. 92%) under good solar would climb to 100% and clip
#   generation we could have exported. The lockout now holds export off only while
#   SOC < POWER_CUT_LOCKOUT_SOC_FLOOR (85%); at/above the floor export resumes so
#   flood-prevention can shed surplus and protect solar. New pure `_export_locked_out`
#   helper (fail-safe: unknown SOC suppresses); `_resolve_export_lockout(soc_pct)`;
#   store flag `power_cut_lockout_active` now tracks the time WINDOW (cleared-event
#   fires once on expiry) with `power_cut_export_suppressed` for the live state.
#   (2) New numeric `gridOnline` device state (1=on-grid, 0=power cut) so SQL Logger
#   can chart a clean power-cut timeline — the existing states are all strings and
#   don't chart. Not written on modbus-offline (offline != a real cut).
# 5.33.0 — Power-cut notifications. When the inverter reports the grid has been lost
#   (the house islands onto the battery) and again when mains power is restored, send a
#   Pushover alert (normal priority, so it respects the configured quiet hours) and an
#   email. Recipient resolves IndigoSecrets.POWERCUT_EMAIL first, then the new
#   powerCutEmailRecipient pref; toggle via powerCutNotify (default on). Both sends are
#   best-effort and never break the poll loop — note a longer outage may also drop the
#   broadband, in which case the alert lands once connectivity returns.
# 5.32.0 — Single source of truth for the flood-export gate. battery_manager gains
#   _compute_flood_preview (pure, no daytime guard, no side effects) — the ONE place the
#   gate math now lives; _check_flood_prevention consumes it (control behaviour unchanged,
#   183 tests green). Each manager tick publishes the gate it acts on to
#   sigen_flood_preview.json (_publish_flood_preview, atomic write) so the openmeteo advisory
#   reports the SAME gate verbatim instead of re-deriving and drifting — the 23/24-Jun-2026
#   "promised an export that never ran" case (advisory used the day+2 'tomorrow' states at
#   01:45 instead of the refill day). 4 new contract tests lock the preview to the live
#   decision (incl. the forward-looking daytime property + the regression).
# 5.31.6 — Solar card data: /api/status solar block now also carries
#   actual_today_kwh, peak_w + peak_time (new daily peak-PV tracking, mirrors
#   peak_soc — init/update/midnight-reset/persist), lifetime_kwh and total_kwp.
#   Feeds the new Dashboards Solar card (today vs forecast, now/peak, tomorrow,
#   yield/kWp, self-sufficiency, forecast accuracy, lifetime). Per-array forecast
#   + measured per-string DEFERRED to a daylight probe (PV=0 at night; inverter
#   reports a '4' count at reg 31025-ish, promising for 4 PV inputs). 179 tests.
# 5.31.5 — Whole-house cost: an unsettled recent day (Yesterday before its gas
#   settles) now shows a PROVISIONAL card from the row's Sigen-measured
#   import/export (complete at midnight) + estimated gas, instead of a blank
#   "awaiting settlement". Electric + export are accurate; only gas is estimated
#   until Octopus settles, then the frozen settled row takes over. New
#   _wh_build_card / _wh_provisional_from_row helpers (today now uses the shared
#   builder too). +1 test (179). Pairs with Dashboards v2.14.6 (tag flips
#   settled<->provisional).
# 5.31.4 — Whole-house cost: /api/status now also exposes `day_before` +
#   `day_before_date` (the settled day before yesterday) so the dashboard can
#   show Today / Yesterday / Day-before. Reliably complete given the ~1-day
#   settlement lag. +1 test (178). Pairs with Dashboards v2.14.3.
# 5.31.3 — Whole-house cost: don't freeze a partially-settled day. The settle
#   pass gated only on "gas data present", so the most recent day (Octopus
#   settles ~a day in arrears, often just the first 1-2 half-hour slots past
#   midnight) was frozen with a near-zero bill PERMANENTLY (cost_settled). Now
#   requires COST_SETTLE_MIN_SLOTS (46 of 48 half-hours) for BOTH import and gas
#   before freezing; the 21-Jun-2026 premature row was un-settled to re-settle
#   when complete. +2 tests (177 pass). Pairs with Dashboards v2.14.2 (Chart.js
#   "Canvas already in use" fix on the 30-day bar).
# 5.31.2 — Whole-house cost deep-review medium/low batch: atomic daily_history.json
#   writes (_atomic_write_json — temp + fsync + os.replace, both settle and
#   midnight writers, so a crash can't truncate the never-pruned history);
#   settle float() of API kWh guarded (one bad day skips, not aborts the cycle);
#   _whole_house_summary caches the history parse by file mtime (it runs every
#   ~5s on /api/status) and bounds today's gas estimate to the last 7 days.
#   octopus_api 1.2->1.3: GraphQL queries parameterised (variables, not raw
#   string-interpolation of account/key), import-vs-export classified by MPAN
#   (not just OUTGOING), first-active-agreement wins, zoneinfo TZ fallback.
#   +11 tests (175 pass): financials error/empty/errors paths, MPAN
#   classification, force-bypass, gas-zero boundary, covered== boundary,
#   partial-row coalescing. Pairs with Dashboards v2.14.1.
# 5.31.1 — Whole-house cost hardening (deep-review highs batch): (1) settle now
#   values each day's STANDING + GAS rates at the rate saved on the day
#   (elec_standing_p_day/gas_unit_p_day/gas_standing_p_day, captured in
#   _write_daily_history) rather than the current ledger snapshot — frozen days
#   stay correct across a tariff/price-cap change; falls back to the current
#   ledger only for older/backfilled rows. (2) get_account_financials now
#   negative-caches failures (FINANCIALS_NEG_CACHE_TTL) and returns the stale
#   value, so a Kraken outage no longer makes /api/status fire a GraphQL request
#   every ~5s. (3) _whole_house_summary call isolated in get_dashboard_data so a
#   fault in the new block can't blank the rest of /api/status. +7 tests
#   (TestSettleWholeHouseCosts, TestWholeHouseSummary); 164 pass. octopus_api 1.1->1.2.
# 5.31.0 — Whole-house cost (gas + electric, incl. standing charges). New
#   /api/status economics.whole_house block: today (provisional), yesterday
#   (settled), month-to-date net, days self-funded, account balance and a
#   30-day bill-vs-export series. A 6-hourly settle pass (_settle_whole_house_costs)
#   freezes each day's cost into daily_history.json once Octopus settles it,
#   valued at the rate that applied on the day so a tariff change never re-writes
#   history. Rates + balance come bill-exact from the Kraken account ledger
#   (octopus_api.get_account_financials, active:true). Gas valued from settled
#   m3 consumption via a configurable calorific factor; gas has no live meter so
#   today's gas is estimated from the latest settled day. New OCTOPUS_GAS_MPRN /
#   OCTOPUS_GAS_SERIAL secrets + octopusGasMprn / octopusGasSerial / gasKwhPerM3
#   config fields. Pairs with Dashboards v2.14.0 'Whole-house cost' card.
# 5.30.1 — Guarantee the full export across ALL PV. Closes a hysteresis gap in 5.30.0:
#   the band was (target-HYST, target+HYST) and HELD the previous sub-mode, so if PV fell
#   from above the cap to just below it (surplus in 3.6-4.0 kW) while latched in "bank",
#   self-consumption would export only the surplus (~3.7 kW), not the full target — bank
#   mode never discharges to top up. Now: drop to "discharge" the instant surplus < target
#   (battery tops the grid up to the target), and apply the +HYST margin only on ENTERING
#   bank (so a brief PV spike can't flap us into 0x02). Net guarantee, battery permitting:
#   PV=0 -> battery exports the target; PV<target -> PV + battery = target; PV>target ->
#   target exported + surplus banked. +2 unit tests (test_plugin).
# 5.30.0 — Daytime VPP export now BANKS the surplus instead of curtailing it. New
#   _drive_vpp_export() re-evaluates every manager tick during VPP_ACTIVE and picks a
#   sub-mode from live PV vs the export target:
#     • "bank" (daytime, PV surplus >= target): Max Self Consumption (mode 0x02) with the
#       battery charge limit capped to (surplus - target). The inverter exports the full
#       target to the grid (held at the DNO cap) AND banks the PV above the target into
#       the battery — same mechanism as Solar Overflow. Live-proven 15-Jun at 10 kW PV:
#       export 4.06 kW + battery charge 4.94 kW + home 1.05 kW, ZERO curtailment.
#     • "discharge" (dark window, or PV surplus < target): the v5.29.x path — mode 0x05
#       (PV-first) + charge 0 daytime, or 0x06 (ESS-first) dark — battery tops the export
#       up to the target. Guarantees the paid dispatch when PV can't cover it.
#   Hysteresis (+/-400 W) around the crossover stops mode flapping; Modbus only writes on
#   a sub-mode change or a charge-cap shift > 300 W. vpp_export_mode tracks the live mode
#   (0x02/0x05/0x06) so _verify_ems_registers maintains the right one. New
#   "Force VPP Export Drive (test)" action exercises the integrated driver on hardware.
#   6 new unit tests (test_plugin). This SUPERSEDES 5.29.1's always-curtail behaviour for
#   high-PV daytime events while keeping its guaranteed-export floor for low PV.
# 5.29.2 — Register-map corrections from a deep-dive review against Sigenergy Modbus
#   Protocol V2.9 (2026-05-13), the revision after our V2.8 baseline. Doc/label only,
#   NO behaviour change: REMOTE_EMS_MODES 0x07 is "Reserved" (was mislabelled "AI Mode";
#   0x07 was never commanded), 0x08="V2G" added; the snapshot ems_mode_name decode matches.
#   Corrected the 40032/40034 comments — they are GLOBAL caps "regardless of EMS mode"
#   (which is exactly why 5.29.1's charge=0 forces export). Header notes register 40001
#   (PCS active-power dispatch, S32 kW, needs 40029=1 + 40031=0, no command watchdog) as a
#   future "export a precise power" option — deliberately NOT used yet (PCS-level not a grid
#   target, sign must be verified on hardware). sigenergy_modbus module 1.5 -> 1.6.
# 5.29.1 — Daytime export (mode 0x05) now pins the CHARGE limit to 0. Hardware testing
#   at PV > 4 kW (15-Jun) showed that with the charge limit left open, mode 0x05 greedily
#   charges the battery with PV surplus INSTEAD of exporting — grid sat near 0 for the
#   first 20-60s and the paid 4 kW dispatch was missed (battery +3.5 kW, grid 0). Pinning
#   charge to 0 removes the competing path, so PV is forced out to the grid up to the DNO
#   cap immediately and stably (re-test: grid -4001 W, battery flat from the first sample).
#   Cost: PV above (cap + house) is curtailed for the window — acceptable, the payment far
#   outweighs the un-banked surplus and the battery refills from solar after the event.
#   Sub-4 kW behaviour unchanged (battery already had to top the export up). daytime_export()
#   docstring documents the why; test asserts charge=0.
# 5.29.0 — Daytime VPP export now uses mode 0x05 (Discharge PV First) instead of 0x06
#   (Discharge ESS First). 0x06 curtailed PV to 0 W during daytime windows (battery did
#   all the work — confirmed on the 15-Jun 07:00-08:00 event, 4.22 kWh all from battery,
#   PV flat). 0x05 sources the grid dispatch from PV first and only draws the battery for
#   the shortfall, so PV keeps running and the battery is preserved — yet the full (paid)
#   4 kW dispatch is still guaranteed (and 0x05 == 0x06 when PV is zero, so no downside).
#   _vpp_transition(VPP_ACTIVE) + the manager's ACTION_VPP_EXPORT re-assert now pick the
#   mode by self._event_is_daytime(); dark windows stay on 0x06. _verify_ems_registers
#   self-heals the chosen mode during VPP_ACTIVE (mode register only — never the limits).
#   New sigenergy_modbus.daytime_export(); new "Force Daytime Export (PV First, test)"
#   action for hardware validation. JSONL snapshots now carry ems_mode_name + driver.
# 5.28.2 — Axle VPP payment rate is now a config pref (axleVppRatePerKwh, default 1.00
#   GBP/kWh) instead of a hardcoded £1, used for the earnings estimate on the Axle VPP
#   Monitor. Coerced via the guarded _as_float so a blank/bad value falls back to 1.00.
#   Mirrors Predbat's axle_pence_per_kwh. No behaviour change beyond the estimate figure.
# 5.28.1 — VPP cleanup follow-up: removed the now-dead Axle release-watcher
#   (_vpp_check_axle_release + _send_vpp_release_alert, ~122 lines), the VPP_COOLING_OFF
#   state + its dead branches, and the unused AXLE_SUPPORT_EMAIL import. Added a dedicated
#   `vppExport` currentMode enum Option so a trigger can fire on "VPP export active"
#   (previously reused the startExport token). No behaviour change to the self-drive.
# 5.28.0 — VPP self-drive. The plugin now drives the export itself for each announced
#   Axle window (T-2min on -> end+2min off) instead of waiting for Axle's cloud dispatch,
#   which proved unreliable (10-Jun-2026 no-show; Axle confirmed a SigEnergy-API fault).
#   Axle settle on the meter reading so self-export counts identically (~£1/kWh, stacking
#   with Octopus Outgoing 12p). Manager override now returns ACTION_VPP_EXPORT (was a
#   self-consumption stand-down); night_export fires on VPP_ACTIVE entry and is re-asserted
#   idempotently each manager tick; discharge floor = next-day reserve; no grid import.
#   The Axle release-watcher (45/60-min alerts) is bypassed — dead code retained for now,
#   to be removed in a follow-up cleanup once proven over a couple of live events.
# 5.27.0 — Octopus single source of truth (retires octopus_tracker_rate.py). The plugin
#   now writes the rate + slot-JSON variables the openmeteo battery optimiser consumes —
#   elec_rates_today_json / elec_rates_tomorrow_json (raw Octopus slots), tracker_rate_today
#   / tracker_rate_tomorrow, tracker_product_code/name, tracker_last_updated/fetch_status —
#   from its own octopus_api fetch, on every octopus refresh. New
#   octopus_api.get_active_rate_schedule() returns today+tomorrow raw slots for the ACTIVE
#   tariff (tariff-agnostic, correct for non-Tracker users); plugin._write_tariff_schedule_
#   variables() emits them (tomorrow only once published). Removes the two-Octopus-clients
#   duplication; the standalone script's Indigo schedule ("Octopus Tracker Daily Rate") is
#   disabled and the script kept on disk as a fallback. Verified live: plugin writes fresh,
#   correct values (fixed the script's stale tracker_rate_today). 126 tests pass.
# 5.26.2 — Low-severity sweep (clears the review queue bar the octopus consolidation):
#   • prepare_to_sleep now also resets a raised discharge cutoff to the health floor —
#     a flood-prev/storm/VPP floor left high would lock the battery and force overnight
#     grid import across a long Mac sleep.
#   • Modbus power-limit setters (charge/discharge/export) clamp to a 100kW sanity
#     ceiling before writing watts to the inverter (was lower-bound only).
#   • Poll loop sleeps min(modbus_poll_s, 10) so the advertised 5s "very live" interval
#     is actually honoured (was a hardcoded 10s).
#   • Seasonal + storm overrides log only on state change (were spamming every 60s).
#   • web_dashboard 1.3: NaN/Infinity-safe JSON (one bad float no longer breaks the live
#     update), calendar view state hoisted to <script> scope (selected year survives the
#     5s refresh), Back link host-relative (was a hardcoded LAN IP).
#   • Tests: modbus harness uses a no-op throttle sleep (suite 123s → ~11s); new
#     test_plugin.py (config-coercion helpers). 126 tests pass.
#   DEFERRED (needs its own session + decision): retire octopus_tracker_rate.py by having
#   the plugin write elec_rates_*_json itself (single source of truth vs octopus_api).
# 5.26.1 — Medium-severity hardening batch (follow-up to 5.26.0's highs):
#   (A) runConcurrentThread wraps each _tick so one bad tick task (modbus/forecast/
#       VPP) is logged and retried, not fatal to the whole polling loop; _as_float/
#       _as_int now coerce their fallback too, so a string default ('94') can't leak
#       into arithmetic (e.g. _as_float(blank,'94')/100.0) when a field is blank.
#   (B) battery_manager._estimate_consumption_until indexes the 48-slot profile by
#       LOCAL time, not the UTC hour (was 2 slots off in BST).
#   (C) openmeteo_forecast skips a day-shifted disk cache (a pre-midnight cache whose
#       "today" buckets are yesterday's) on restart instead of serving stale day data.
#   +8 regression tests (battery 62, modbus 27 incl. signed negative/boundary decode).
#   Advisory scripts: octopus_tracker_rate v1.5 (re-read secrets each run), optimiser
#   v3.8 (Pushover msgSound/msgPriority + honest day+2-beyond-horizon log), axle diff
#   analyser v1.1 (de-nest f-string + None-register guard).
# 5.26.0 — High-severity bug-fix batch from the comprehensive review:
#   (1) Pause feature was dead — sigen_manager_paused / Pause action set a flag
#       nothing read, so a "Paused" manager kept driving the inverter. Now gated
#       in _evaluate_manager (skips evaluate/verify/act); pause returns the
#       inverter to self-consumption (KPI-safe), seeds from the variable at
#       startup, and resumes with an immediate re-evaluate.
#   (2) Partial Modbus read could feed the manager a phantom 0% SOC (a dropped
#       SOC register left the key missing → consumers .get(...,0.0)) → force-charge
#       that never completes / keeps importing. read_all() now returns None on any
#       missing critical register so _poll_modbus keeps the last-known-good snapshot.
#   (3) battery_manager TOU cheap-window detection used the UTC clock vs local-time
#       window strings (1h off in BST) — now converts to Europe/London first.
#   (4) Open-Meteo partial fetch (some arrays missing) was stamped OK and clobbered
#       the good cache, producing a low total that triggered needless import — now
#       flagged "Partial N/M" and never overwrites a complete forecast.
#   +6 regression tests (108 pass).
# Changes:     v5.25.4 (01-06-2026) — Live Power Flow diagram polish: uniform
#              r=38 circles (Solar/Home/Grid/Battery), Grid kW enlarged to match
#              Home with an Import/Export/Idle line below it, Battery shows kW
#              over % (swapped), Home/Grid kW aligned on the flow axis. This
#              commit also catches the repo up from 5.24.1 — the 5.25.0–5.25.3
#              dashboard work was live/installed but had never been committed.
#              v5.24.1 (28-05-2026) — silence the one spurious red ERROR per
#              restart: `device "Battery Manager" state key currentMode not
#              defined (ignoring update request)`. The currentMode List-enum is
#              registered ASYNCHRONOUSLY after stateListOrDisplayStateIdChanged()
#              in deviceStartComm, so the FIRST evaluate write (which batches
#              currentMode with ~20 other keys) raced the registration and
#              logged one harmless ERROR (rest of the batch applied fine). The
#              init-state seed already correctly omitted currentMode (v5.24.0);
#              this extends the same guard to the evaluate write — currentMode is
#              now appended to the batch only once `"currentMode" in dev.states`,
#              so the first post-restart tick skips it and the next (<=60s) writes
#              the real mode. No alarming red line, nothing lost.
#              v5.24.0 (28-05-2026) — added currentMode List-enum state to the
#              Battery Manager device so Indigo auto-generates one BoolTrueFalse
#              sub-state per mode (currentMode.solarOverflow etc.). Users can now
#              trigger directly on "battery entered night-export" without a
#              string compare. Additive: currentAction keeps its friendly display
#              string. deviceStartComm now re-fetches the device after the state
#              list refresh so the new enum + sub-states register cleanly.
#              v5.23.0 (27-05-2026) — added prepare_to_sleep / wake_up overrides
#              harvested from the 27-May plugin_base.py sweep. Mac sleep used
#              to leave the inverter in whatever forced mode it was last set
#              to (force-charge, night-export) — an 8-hour overnight Mac sleep
#              while the inverter sat in force-charge would overcharge the
#              battery. Now: on sleep, return to self-consumption (safe
#              baseline), save accumulators, stop dashboard, disconnect
#              modbus. On wake, restart dashboard, force last_modbus and
#              last_manager to 0 so the next tick polls + re-evaluates
#              immediately rather than waiting the full interval. The
#              SAFE-on-sleep behaviour is the most important operational
#              improvement in this version — biggest win of the harvest.
#              v5.22.1 (27-05-2026) — octopus_api._parse_tou_slots: BST-aware
#              UTC→Europe/London conversion no longer depends on pytz being
#              importable. Now prefers stdlib zoneinfo (always available on
#              Python 3.9+) and only falls back to pytz, with None as last
#              resort. Fixes test_battery_manager regression where a UTC
#              23:30 Go cheap slot in summer was misclassified as standard
#              because the test environment lacks pytz; production Indigo
#              installs (pytz in requirements.txt) were already correct but
#              this hardens the path so the test environment matches.
#              v5.22.0 (27-05-2026) — battery_manager.py 3.4 → 3.5: plan-object
#              decision-audit pattern lifted from mlamoure/indigo-auto-lights.
#              Decision dataclass gains an audit_trail field populated at every
#              branch (CONTEXT, BALANCE, OVERRIDE, RESILIENCE, FLOOD-PREP,
#              IMPORT, OVERFLOW, RELEASE-OVERFLOW, DEFAULT) — both matched AND
#              considered-but-skipped branches recorded.  _log_manager_decision
#              now dumps the audit block after the action-change INFO line,
#              once per action transition (not per-poll) so no log spam.  Same
#              shape applied to openmeteo_battery_optimiser v3.6 and
#              octopus_tracker_rate v1.2 the same day.  Existing 16+ unit
#              tests in test_battery_manager.py unaffected (audit_trail
#              defaults to empty list).
#              v5.21.4 (26-05-2026) — openmeteo_forecast.py 1.3 → 1.4: one-shot
#              retry on transient network errors (Timeout, ConnectionError,
#              ChunkedEncodingError — the last covers Open-Meteo's occasional
#              SSL UNEXPECTED_EOF hiccups). Transient blips now log at WARNING
#              not ERROR; cache fallback and 3-of-4 array path unchanged.
#              v5.21.2 (23-05-2026) — millisecond timestamp [HH:MM:SS.mmm]
#              prefix on every log line via plugin_utils.install_timestamp_filter().
#              Matches Device Activity Monitor convention. New "Toggle
#              Timestamps in Log" menu item.
#              v5.21.0 (22-05-2026) — magnitude-conditional bias correction in
#              openmeteo_forecast.py (module bumped 1.2.1 → 1.3). Analysis of
#              31 days showed err% vs forecast_kwh r = -0.462: the model
#              under-forecasts on moderate-prediction days (25-45 kWh,
#              ratio 1.18-1.28) and over-forecasts on bright days (>55 kWh,
#              ratio ~0.93). A flat factor (v1.2's experiment) cancels these
#              out; a 5-band table (centres 17.5/30/40/50/65 kWh, median
#              actual/forecast per band, linear interp) follows the shape and
#              projects MAPE 19.8% → ~14-16%. Bands recomputed nightly from
#              accuracy records (rolling 60 days). Per-day factor applied to
#              both correctedTodayKwh/Tomorrow and to every hourly slot in
#              openmeteo_forecast.json so the battery optimiser sees the
#              corrected shape. New JSON fields: biasFactorToday,
#              biasFactorTomorrow, biasBands. 24 unit tests; plugin restart
#              clean.
# Changes:     v5.19.2 (15-05-2026) — Live Power Flow visual polish (option B):
#              soft teal aurora glow + horizon bar behind the card; two
#              status chips top-right ("On Grid" / "Lockout" / "Grid Down"
#              and the current manager mode, with VPP override during an
#              event); richer node labels — battery shows "0.98 kW · Charging"
#              / "0.50 kW · Discharging" / "Idle", grid flips to Sigenergy-
#              app ordering "0.94 kW · Exporting". No new data sources;
#              everything is already in /api/status.
# Changes:     v5.19.1 (15-05-2026) — Live Power Flow card now uses kW with
#              2 decimals (e.g. 980 W -> 0.98 kW) for all four nodes (solar,
#              battery, home, grid). Other cards retain the existing W/kW
#              auto-switching format.
# Changes:     v5.19 (15-05-2026) — Export sync check (Sigenergy vs Octopus).
#   • New /api/export-sync endpoint and Export Sync dashboard card. Compares
#     the inverter's daily export kWh (from daily_history.json) against the
#     half-hourly readings settled by Octopus on the export MPAN, for the
#     last 7 fully-settled days. The most recent 3 days are skipped because
#     Octopus typically takes 24-48 h to settle.
#   • Tolerance ±5% — anything wider is flagged as drift.
#   • New OCTOPUS_EXPORT_MPAN / OCTOPUS_EXPORT_SERIAL keys (IndigoSecrets.py
#     first, PluginConfig fallback). Feature silently disabled if absent.
#   • Octopus client gets get_export_kwh_for_date(date, mpan, serial)
#     returning {kwh, slots}; reuses the same _paginate + auth path as the
#     consumption-profile call.
#   • Results cached on self.store["export_sync_cache"] for 6 h to avoid
#     hammering the Octopus API; the dashboard refreshes hourly.
#   • Once-a-day INFO line at midnight: "[ExportSync] 7d avg diff +0.8%
#     worst: 2026-05-08 +3.1%" (or "[DRIFT >5%]" suffix). Skipped silently
#     if the export MPAN isn't configured.
#   • Show Plugin Info / self-test now lists OCTOPUS_EXPORT_MPAN +
#     OCTOPUS_EXPORT_SERIAL in the secrets table.
# Changes:     v5.18.2 (14-05-2026) — VPP event post-mortem: states + Pushover.
#   • New _summarise_vpp_event() parses the per-event JSONL file at the
#     VPP_ACTIVE -> COOLING_OFF transition and computes:
#       export_kwh, pv_kwh (avg watts * duration), min_pv_w, max battery
#       discharge W, peak grid export W, "PV survived" flag (min_pv_w > 100),
#       and the set of distinct emsWorkMode strings observed.
#   • Nine new states on the axleVppMonitor device (Devices.xml):
#       lastVppDate, lastVppExportKwh, lastVppPvKwh, lastVppMinPvW,
#       lastVppMaxBatteryDischargeW, lastVppPeakGridExportW,
#       lastVppPvSurvived (Boolean), lastVppEmsModes, lastVppLogPath.
#     Lets the user spot at a glance whether Axle's strategy worked
#     without having to read the JSONL file.
#   • Pushover at event end carries the headline numbers AND a pre-formed
#     "Ask Claude" block listing the JSONL path and four pointed questions
#     the user can paste straight into Claude Code for analysis. Priority 0
#     (vibrate, respects quiet hours).
#   • One concise grep-able summary line goes to the Indigo Event Log; the
#     per-minute snapshots remain JSONL-only (no log noise).
#   • Summariser is best-effort: any failure logs WARNING but never blocks
#     the COOLING_OFF state machine.
#   • COMPANION: a daily scheduled Claude task ('vpp-event-morning-analysis')
#     is intended to read the newest JSONL each morning and produce a
#     written analysis (mode/registers/limits Axle used, can we copy it?).
#     The prompt is preserved in this commit's notes; create with
#     mcp__scheduled-tasks__create_scheduled_task when next interactive.
#
# v5.51.1 (21-07-2026): LOG-LEVEL FIX. indigo.server.log(level=...) wants a Python
# logging INT — a STRING is silently ignored and the line logs as plain Info.
# The log() helper passed its level name straight through, so every WARNING and
# ERROR raised through it had been appearing as an ordinary Info line. Added
# _lvl() to map the name to a real level. Estate-wide sweep (38 files).
#
# Changes:     v5.18.1 (14-05-2026) — quiet the VPP event log.
#   • Per-minute VPP/Axle snapshots moved OUT of the Indigo Event Log
#     and INTO a per-event JSONL file under <data_dir>/vpp_events/,
#     filename derived from the event start time (e.g.
#     2026-05-15_0800.jsonl). One JSON line per minute during the
#     window plus an "announcement" line at the start and an
#     "event_ended" line at the close. Lets the file be parsed after
#     the event (eg `jq -c 'select(.type=="snapshot")' file.jsonl`)
#     to see exactly what Axle did with the inverter — without
#     drowning the live log.
#   • Indigo Event Log during an event now only carries the key state
#     markers: announced, T-10min warning, T-5min RELEASED CONTROL,
#     VPP WINDOW ACTIVE, event ended, REGAINED CONTROL.
#   • _write_vpp_event_header() writes every field Axle's API returned
#     to the JSONL file once at announcement time.
#
# Changes:     v5.18 (14-05-2026) — TRUE Axle handoff via Remote EMS release.
#   • v5.16 + v5.17 were both stop-gap measures that had the plugin drive
#     the export through mode selection (0x06 or 0x02+charge_limit=0).
#     Both held Remote EMS enabled, which BLOCKED Axle's cloud channel
#     and forced us to pick among simple Modbus modes that can't do what
#     Axle's cloud can (e.g. simultaneous battery discharge + PV charge).
#   • v5.18 properly releases Remote EMS at T-5min before event start
#     via modbus.disable_remote_ems(). With Remote EMS off the inverter
#     follows Sigenergy's cloud commands directly — Axle now controls
#     the inverter the way other Axle+Sigenergy users see, including
#     keeping PV running through battery export.
#   • Pre-export step (T-4min mode 0x06) removed — replaced by the early
#     T-5min release so Axle has lead-time to dispatch.
#   • Minute-by-minute countdown spam ("[VPP] Event in N min - preparing"
#     every minute from 60 min out) removed. Single T-10min warning
#     instead. T-30min pre-charge trigger unchanged.
#   • New >>> RELEASED CONTROL TO AXLE <<< marker at T-5min, and
#     >>> REGAINED CONTROL <<< marker when Axle releases the inverter
#     in COOLING_OFF. Easy to grep.
#   • _log_vpp_snapshot() fires once per minute during VPP_ACTIVE,
#     dumping SOC / PV / battery / home / grid power + EMS mode +
#     charge/discharge limits. Lets us see exactly what Axle is doing
#     for post-event analysis.
#   • Full event-detail dump on announcement (every field Axle's API
#     returned) — useful for learning what the dispatch metadata
#     contains.
#   • Verify loop reverted to skip during VPP_ACTIVE/COOLING_OFF: the
#     plugin is in observe-only mode for those states; any write would
#     fight Axle.
#   • COOLING_OFF logic unchanged — _vpp_check_axle_release() watches
#     for emsWorkMode containing "Self" (the inverter falls back to
#     Max Self Consumption when Axle finishes), then re-enables Remote
#     EMS and logs REGAINED CONTROL.
#
# Changes:     v5.17 (14-05-2026) — DAYTIME VPP fix follow-up.
#   • v5.16 fixed the export-stops-at-event-start bug by setting mode 0x06
#     (Discharge ESS First) for the VPP window. Export resumed at 4 kW,
#     but PV dropped to 0 W (curtailed by the inverter — mode 0x06 makes
#     the battery do all the discharge, and with grid capped at 4 kW
#     there is nowhere for PV to go, so the MPPT shuts down).
#   • For daytime VPP the right mode is 0x02 (Max Self Consumption) with
#     charge_limit pinned to 0 W. PV can't be diverted to charge the
#     battery, so PV exits via the AC side and exports to grid; battery
#     only discharges if PV is insufficient to meet (home + grid_cap).
#     Net effect: 4 kW grid export from PV (free), battery preserved
#     for later, no PV curtailment.
#   • Modbus sequence in _vpp_transition(VPP_ACTIVE):
#       set_self_consumption()  → mode 0x02, charge/discharge limits 10kW
#       set_charge_limit(0)     → battery can't absorb PV
#   • _verify_ems_registers maintains both registers throughout the event.
#   • Log line updated: "PV exports to grid, battery fills any shortfall
#     (no PV curtailment)".
#
# Changes:     v5.16 (14-05-2026) — VPP event handoff fix.  CRITICAL.
#   • Symptom (14-May-2026 morning VPP event): pre-export started correctly
#     at 07:56 with mode 0x06 (Discharge ESS First) — battery exporting.  At
#     08:00:55 the plugin's _vpp_transition(VPP_ACTIVE) called
#     set_self_consumption() to "clear solar overflow cap before handing
#     control to Axle".  That call switched the inverter from mode 0x06 back
#     to 0x02 (Max Self Consumption), STOPPING the export.  Axle then could
#     not override because Remote EMS was still locked to the plugin — so
#     for the rest of the event the battery charged from PV (4 kW) instead
#     of exporting.  Result: 0 kWh exported during the paid VPP window when
#     ~10 kWh should have flowed.
#   • Root cause: the plugin used to assume Axle would take Modbus control
#     after the transition and drive the discharge itself.  In practice Axle
#     uses Sigenergy's cloud channel, which is blocked while Remote EMS
#     holds the lock.  The "handoff" model never worked end-to-end.
#   • Fix: switch to plugin-driven export through the VPP window.  Axle
#     measures via the smart meter, not by sending commands.  Specifically:
#       1. _vpp_transition(VPP_ACTIVE) now calls night_export() (mode 0x06,
#          10 kW discharge limit) instead of set_self_consumption() —
#          idempotent if pre-export already set the mode; rescues the
#          late-detection path where pre-export never ran.
#       2. _verify_ems_registers() now actively maintains mode 0x06 during
#          VPP_ACTIVE (was previously skipping all writes, allowing drift
#          if anything else touched register 40031).
#       3. VPP_ACTIVE -> VPP_COOLING_OFF entry now calls set_self_consumption()
#          to cleanly close the export and return to Max Self Consumption.
#          The "waiting for Axle to release" log line is gone; there is no
#          handback to wait for in the plugin-driven model.
#   • Backward-compatibility: VPP_COOLING_OFF logic (_vpp_check_axle_release)
#     left intact — it'll see "Self Consumption" in emsWorkMode immediately
#     after our explicit set_self_consumption() and complete the cool-off
#     phase in normal time.
#   • Test plan: at next VPP event, expect mode 0x06 to persist from
#     pre-export through event end with no gap; grid should be exporting
#     at ~10 kW with battery discharging; at event end, mode returns to
#     0x02 and normal self-consumption resumes.
#
# Changes:     v5.15 (13-05-2026) — publish auto-calibrated consumption
#              profile in sigen_site_config.json:
#   • _write_site_config() now includes a "consumption" block with
#     hourly weekday/weekend kWh derived from the 48-slot inverter
#     profile (only when 48 valid slots are accumulated).
#   • _refresh_consumption_profile() republishes the site_config after
#     each refresh so the JSON stays current.
#   • Lets openmeteo_battery_optimiser.py (v2.10+) replace its old
#     Octopus-grid-only profile (~11 kWh/day, wrong) with the plugin's
#     real-load profile (~22 kWh/day, right).
#   • Background: the Octopus smart-meter export only sees grid imports,
#     so for a solar+battery house it massively under-counts true home
#     consumption.  This is the root cause of yesterday's incident.
# Changes:     v5.14 (13-05-2026) — expose tomorrow solar/need on
#              BatteryManager device:
#   • New Devices.xml states tomorrowSolarKwh and tomorrowNeedKwh published
#     every manager tick by _update_manager_device, computed from snapshot
#     using the SAME logic battery_manager._calculate_24h_balance() uses
#     (tomorrow_weekday + weekday_kwh/weekend_kwh).
#   • Lets external scripts (openmeteo_battery_optimiser.py v2.9+) read the
#     plugin's actual flood-prevention inputs instead of computing their own
#     and ending up with a different ratio.
#   • Background: 12-May-2026 the optimiser's 20:00 Pushover promised an
#     overnight pre-drain export (40 kWh solar / 11 kWh typical = 3.6x),
#     but at 00:27 the plugin's internal view was 63 kWh / 22.4 kWh = 2.81x
#     — just below the 3.0x FLOOD_PREV_FORECAST_MULT gate. No export ran.
#     Both sides used the same constant; the inputs differed because the
#     plugin's auto-calibrated weekday_kwh (~22) is biased high by spring
#     heating-on data still in its rolling 48-slot profile, while the
#     script used a May seasonal value (~11). Aligning their inputs is
#     cheaper and lower-risk than retuning the calibration.
# Changes:     v5.13 (12-05-2026) — help tooltips on every static label.
# Changes:     v5.10 (12-05-2026) — compact forecast chart with hover tips:
#   • Hourly forecast SVG shrunk from 130px to 80px high (~60% shorter).
#   • kWh labels above each bar removed (less visual noise).
#   • Hover any bar to see a custom floating tooltip with the hour and
#     exact kWh value, with glassmorphism panel + glow.
#   • Bars highlighted on hover for clear visual feedback.
#   • Native SVG <title> retained as accessibility / no-JS fallback.
# Changes:     v5.9 (12-05-2026) — live polling + dashboard cadence:
#   • Modbus poll interval is now actually wired up to PluginConfig
#     (was hardcoded). Default lowered 60s -> 10s so the dashboard sees
#     fresh data within ~10s of any change. Range 5-600s. Watt-integration
#     fallback in _accumulate_daily_energy uses the live value too.
#   • PluginConfig dropdown gained 5/10/15s options at the top with clear
#     "live" labelling; 30/60/120s remain for low-traffic setups.
#   • Dashboard auto-refresh tightened 30s -> 5s. Number tweens (added in
#     v5.8) now appear visibly continuous as PV/grid/battery watts shift.
#   • One-time pref migration: existing installs sitting on the legacy 60s
#     or 120s default get bumped to 10s on next startup; users who chose
#     30s explicitly are left alone.
# Changes:     v5.8 (12-05-2026) — dashboard glamour pass:
#   • Glassmorphism: cards now have semi-transparent backgrounds with
#     14px backdrop blur over a soft drifting radial-gradient backdrop
#     (slow 28s drift). Cards lift on hover with a subtle outer glow.
#   • Headline numbers (SOC %, solar benefit £, tariff rate) get a coloured
#     text-shadow glow that matches the value — green for SOC / benefit,
#     amber for tariff, red when benefit goes negative.
#   • Smooth number transitions: SOC % and solar benefit £ tween between
#     old and new values with a 700ms easeOutCubic instead of snapping.
#   • Live-pulse: header timestamp now leads with a green pulsing dot to
#     show the data is fresh.
#   • Cards fade-in on initial page load (staggered 50ms apart).
#   • SOC ring stroke now eases between values with a soft glow filter.
#   • Sparkline added to the SOC card — last 24h SOC trend with low/high
#     caption, gradient-filled SVG, glow on the line.
#   • Skeleton shimmer class available for any future "loading" placeholders.
#   • Tabular numbers everywhere KPIs live to stop digit jitter.
# Changes:     v5.7 (12-05-2026) — true day-by-day rates + forever retention:
#   • daily_history.json retention: cap removed entirely. Records are kept
#     forever (~280 bytes each — 50 years of daily data is < 6 MB).
#   • Each daily record now persists both `rate_today_p` (import rate) AND
#     `export_rate_p` (export rate that was live on that day). Historical
#     economics now value every day at the exact pence/kWh it was paid /
#     earned on — future export-tariff changes will NOT retroactively
#     re-value past days at the new rate.
#   • All three roll-up paths (today, yesterday, period totals, calendar
#     months) prefer the per-record export_rate_p; live/current rate is
#     only the fallback for older records that pre-date this change.
#   • Existing 43 records back-filled with export_rate_p=12.0 (Octopus
#     Outgoing has been flat 12p since 26-Mar-2026; all current records
#     fall after that date).
# Changes:     v5.6 (12-05-2026) — calendar-month breakdown on the dashboard:
#   • New "<year> calendar months" card — Jan-Dec table for the current
#     calendar year, same five economics columns as the period card. Each
#     month row shows total + per-day average; current month flagged
#     "(partial)"; months with no data show "—".
#   • Year-total footer row sums every populated month.
#   • Year selector tabs (one button per year with any data, always
#     including current year) — click switches the calendar card to a
#     historical year without disturbing the rest of the dashboard.
#     Backed by new endpoints /api/calendar?year=YYYY and /api/years.
#   • daily_history.json retention bumped from 365 to 3650 days (~10
#     years) so prior years aren't lost. Each record is ~250 bytes so
#     10 years ≈ 1 MB JSON — negligible.
#   • Periodic /api/status refresh only updates the calendar card if the
#     user is viewing the current year — historical-year selections are
#     preserved across the auto-refresh tick.
#   • New `economics.calendar_months` block in /api/status (current year).
# Changes:     v5.5 (12-05-2026) — period totals on the dashboard:
#   • New "Period totals" card under the bottom row, listing Week / Month /
#     Year roll-ups of all five economics fields (solar benefit, net grid,
#     without-solar, import paid, export earned). Each cell shows the
#     period total as the headline number and the per-day average underneath.
#   • Window definitions: Week = last 7 days; Month = current calendar
#     month so far (variable day count); Year = last 365 days.
#   • Each historical day is valued at its own saved rate_today_p (Tracker
#     rates change daily); export rate is assumed flat 12p across history
#     (Octopus Outgoing 12p has been live since 26-Mar-2026).
#   • New `economics.periods` block in /api/status with totals + averages.
# Changes:     v5.4 (12-05-2026) — dashboard yesterday economics:
#   • New "Yesterday" card alongside "Today's Cost" — same five fields
#     (import paid, export earned, net, without-solar, headline benefit)
#     for a full-day reading, since today's view is partial until midnight.
#   • Reads daily_history.json's most recent entry; uses the saved
#     rate_today_p (falls back to today's live rate if older entries lack it).
#   • Refactored economics calc into a shared `_compute_daily_economics`
#     helper so today and yesterday use identical maths.
#   • /api/status `economics` block restructured to:
#       {"today": {...}, "yesterday": {...}, "yesterday_date": "YYYY-MM-DD"}.
#   • Bottom-row grid switched to auto-fit/minmax so it now accommodates
#     5 cards (Decision / Today Summary / Tariff / Today Cost / Yesterday)
#     without media-query gymnastics.
#   • BUG FIX: home_daily_kwh was being overwritten with 0 when the inverter's
#     register 30092 reset at its midnight (which can race the plugin's local
#     midnight handler). Every record in daily_history.json had home_kwh ~= 0
#     instead of the true ~15-20 kWh. The accumulator now ignores a sudden
#     drop in the inverter counter and lets _check_midnight reset the store
#     value at the right moment. Going forward, the daily history will be
#     accurate; existing past records cannot be retroactively corrected.
# Changes:     v5.3 (12-05-2026) — daily economics on the dashboard:
#   • New "Today's Cost" card showing import paid, export earned, net today,
#     what the day would have cost without solar, and the headline solar
#     benefit (£) — i.e. counterfactual cost minus actual net cost.
#   • New `economics` block in /api/status — all values in GBP, plus the
#     import/export pence rates used in the calculation.
#   • Also: new menu item "Open Web Dashboard" — logs the URL (clickable
#     from any Indigo client) and best-effort browser launch on the server.
# Changes:     v5.2 (12-05-2026) — three small additions:
#   • Web dashboard charts (Chart.js via CDN). New 24h/48h/7d SOC + energy
#     stacked-bar charts, and a 30-day daily totals bar chart. Backed by two
#     new endpoints: /api/history?hours=N (half-hourly slots from SQLite) and
#     /api/daily?days=N (daily_history.json).
#   • Weekly tar.gz backup of the data dir at Monday midnight. Backs up
#     accumulators.json, daily_history.json, soh_history.json,
#     home_load_profile.json, forecast_accuracy.json, energy_timeseries.db
#     and the openmeteo combined cache. Retains the 8 most recent (~2 months).
#   • Auto-update notifier: GitHub releases API check on startup, daily-cached
#     in pluginPrefs.lastUpdateCheck. Logs an INFO line if a newer plugin
#     version is published. Silent on network failure.
#   • Also: fixed double-count bug in Show Today's Energy Summary +
#     Show Manager Status — both used correctedTodayKwh (whole-day forecast)
#     labelled as "remaining". Now read remainingTodayKwh.
# Changes:     v5.1 (12-05-2026) — site config consolidation:
#   • New shared sigen_site_config.json published to Python Scripts/ on every
#     plugin start and every PluginConfig save. Companion optimiser script reads
#     it so battery / inverter / flood-prevention values can no longer drift.
#   • Fixes confirmed drift bug: optimiser FLOOD_PREV_FORECAST_MULT was 4.0
#     while plugin used 3.0 — could give "no pre-drain" advisory while the
#     plugin actually pre-drained.
#   • PluginConfig: new siteArraysJson field — per-array specs as JSON list,
#     strict-shape parsed at startup with ERROR log on bad JSON (falls back to
#     built-in ARRAYS).
# Changes:     v5.0 (12-05-2026) — major hardening and feature pass:
#   • Threading lock around self.store (tick + action callbacks)
#   • Web dashboard now joins thread + server_close on shutdown
#   • SQLite timeseries connections use timeout=5.0 + try/finally
#   • Octopus rate-limit tracker (warns >80/hr, hard-stops >95/hr)
#   • Modbus writes are read-back-verified (warns on mismatch)
#   • JSON parse guards on every response.json() in Octopus / Open-Meteo / Axle
#   • Kraken token cleared on any failure path so stale tokens cannot persist
#   • battery_manager.evaluate() refactored — _check_overrides /
#     _check_resilience_buffer extracted, dead v4.0 night-export branch removed
#   • Site coordinates moved to PluginConfig (siteLatitude / siteLongitude)
#   • Variable folder ID cached after first lookup
#   • _ensure_plugin_log throttled to hourly (was every tick)
#   • ServerApiVersion bumped 3.0 -> 3.8 (Indigo 2025.2 native)
#   • pymodbus pinned to >=3.0,<4.0
#   • EMS mode 0x07 ("AI Mode") added to decode table
#   • Axle: forecast_dispatch_kwh / estimated_revenue_p surfaced when present
#   • Auto-calibrated weekday/weekend kWh from live inverter consumption profile
#   • New menu items: Run Self-Test, Show Power Cut Log
#   • Dashboard: tomorrow_surplus_kwh, tomorrow_revenue_gbp, forecast_accuracy
#   • Battery State-of-Health weekly snapshot + degradation warnings
#   • Power cut event log (rolling 100, surfaced via menu + dashboard)
#   • Variable-driven pause/resume via sigen_manager_paused
#   • Pushover quiet hours + configurable sound
#   • Forecast accuracy 7-day MAPE rolling summary
# Date:        10-05-2026
# Version:     4.9 (prior)

import indigo
import json
import os
import sqlite3
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

# ============================================================
# Secrets (from master IndigoSecrets.py - never committed to git)
# ============================================================

sys.path.insert(0, os.getcwd())
sys.path.insert(0, "/Library/Application Support/Perceptive Automation")
# Master credentials file: IndigoSecrets.py at
# /Library/Application Support/Perceptive Automation/IndigoSecrets.py
# (renamed from `IndigoSecrets.py` on 10-May-2026 — see feedback_secrets_module_shadowing.md
# for why: avoid colliding with Python's stdlib `secrets` module).
# Per-key try/except so a missing single key does not blank the others.
try:
    from IndigoSecrets import OCTOPUS_API_KEY
except ImportError:
    OCTOPUS_API_KEY = ""
try:
    from IndigoSecrets import OCTOPUS_ACCOUNT
except ImportError:
    OCTOPUS_ACCOUNT = ""
try:
    from IndigoSecrets import OCTOPUS_MPAN
except ImportError:
    OCTOPUS_MPAN = ""
try:
    from IndigoSecrets import OCTOPUS_SERIAL
except ImportError:
    OCTOPUS_SERIAL = ""
try:
    from IndigoSecrets import OCTOPUS_EXPORT_MPAN
except ImportError:
    OCTOPUS_EXPORT_MPAN = ""
try:
    from IndigoSecrets import OCTOPUS_EXPORT_SERIAL
except ImportError:
    OCTOPUS_EXPORT_SERIAL = ""
try:
    from IndigoSecrets import OCTOPUS_GAS_MPRN
except ImportError:
    OCTOPUS_GAS_MPRN = ""
try:
    from IndigoSecrets import OCTOPUS_GAS_SERIAL
except ImportError:
    OCTOPUS_GAS_SERIAL = ""
try:
    from IndigoSecrets import AXLE_API_KEY
except ImportError:
    AXLE_API_KEY = ""
try:
    from IndigoSecrets import PUSHOVER_USER_TOKEN
except ImportError:
    PUSHOVER_USER_TOKEN = ""
try:
    from IndigoSecrets import POWERCUT_EMAIL
except ImportError:
    POWERCUT_EMAIL = ""
try:
    from IndigoSecrets import SIGENERGY_IP
except ImportError:
    SIGENERGY_IP = ""
try:
    from IndigoSecrets import DASHBOARD_HOST
except ImportError:
    DASHBOARD_HOST = ""
# Site coordinates — IndigoSecrets first, PluginConfig fallback, Big Ben default.
# Names match the existing IndigoSecrets convention (LATITUDE / LONGITUDE).
try:
    from IndigoSecrets import LATITUDE as SITE_LATITUDE
except ImportError:
    SITE_LATITUDE = None
try:
    from IndigoSecrets import LONGITUDE as SITE_LONGITUDE
except ImportError:
    SITE_LONGITUDE = None
# Unified Dashboards plugin: menuOpenDashboard now points at the Dashboards
# hub rather than Sigenergy's internal mini-dashboard on WEB_DASHBOARD_PORT.
try:
    from IndigoSecrets import INDIGO_URL
except ImportError:
    INDIGO_URL = ""
try:
    from IndigoSecrets import INDIGO_API_KEY
except ImportError:
    INDIGO_API_KEY = ""
try:
    from IndigoSecrets import CLAUDEBRIDGE_BEARER_TOKEN
except ImportError:
    CLAUDEBRIDGE_BEARER_TOKEN = ""

try:
    from plugin_utils import log_startup_banner
except ImportError:
    log_startup_banner = None
try:
    from plugin_utils import install_timestamp_filter
except ImportError:
    install_timestamp_filter = None

# Plugin modules
from sigenergy_modbus import SigenergyModbus
from openmeteo_forecast import OpenMeteoForecast
from octopus_api      import OctopusAPI, TARIFF_TRACKER, TARIFF_FLEXIBLE, GAS_KWH_PER_M3
from battery_manager  import (
    BatteryManager, ManagerSnapshot, TariffData,
    # The ONE Europe/London implementation (v5.55.3). Imported rather than
    # re-declared: five hand-rolled copies is what put two sites an hour out
    # in the first place, and a sixth here would be the same mistake again.
    _london_tz, _london_localise, _to_london,
    ACTION_SELF_CONSUMPTION, ACTION_START_IMPORT, ACTION_STOP_IMPORT,
    ACTION_SCHEDULE_IMPORT, ACTION_START_EXPORT, ACTION_STOP_EXPORT,
    ACTION_VPP_EXPORT,
    ACTION_SOLAR_OVERFLOW, FLOOD_PREV_SOC_THRESHOLD_PCT, FLOOD_PREV_TARGET_PCT,
    FLOOD_PREV_FORECAST_MULT,
    SOLAR_OVERFLOW_TARGET_SOC_PCT, SOLAR_OVERFLOW_MIN_END_SOC_PCT,
)
from axle_api      import AxleAPI
from storm_watch   import check_storm_level
from web_dashboard import WebDashboard

# ============================================================
# Constants
# ============================================================

# PLUGIN_VERSION is read dynamically from Info.plist by Indigo and passed to
# Plugin.__init__ as `plugin_version` (exposed as self.pluginVersion).  Do NOT
# add a hardcoded version constant here — Info.plist is the single source of truth.
PLUGIN_NAME        = "Sigenergy Manager"
WEB_DASHBOARD_PORT = 8179

# Maps the raw decision action (snake_case) to the camelCase token written to
# the batteryManager "currentMode" List-enum state. Indigo derives one
# BoolTrueFalse sub-state per token (currentMode.solarOverflow etc.) for
# per-mode triggering. Tokens MUST match the <Option value=> entries in
# Devices.xml. currentAction keeps the friendly display string separately.
ACTION_MODE_TOKEN = {
    ACTION_SELF_CONSUMPTION: "selfConsumption",
    ACTION_SOLAR_OVERFLOW:   "solarOverflow",
    ACTION_START_IMPORT:     "startImport",
    ACTION_SCHEDULE_IMPORT:  "scheduleImport",
    ACTION_STOP_IMPORT:      "stopImport",
    ACTION_START_EXPORT:     "startExport",
    ACTION_STOP_EXPORT:      "stopExport",
    ACTION_VPP_EXPORT:       "vppExport",     # dedicated enum token (currentMode.vppExport trigger)
}

# Minimum inverter readings required per half-hourly slot before we trust the
# accumulated average over the default profile.  5 readings = ~5 days of data
# in that time-slot (one reading per day during that 30-min window).
HOME_PROFILE_MIN_READINGS = 5

# Polling intervals (seconds).
# MODBUS_POLL_INTERVAL is the default fallback when no value is in pluginPrefs;
# the actual interval used at runtime is `self.modbus_poll_s`, set in
# _init_modules from pluginPrefs.pollInterval.  Wiring up the pref was added
# in v5.9 — previously the constant was used everywhere and the PluginConfig
# `pollInterval` field had no effect.
MODBUS_POLL_INTERVAL      = 10
MANAGER_EVAL_INTERVAL     = 60    # evaluation cadence — independent of poll cadence
FORECAST_FETCH_INTERVAL   = 1800  # 30 minutes (Open-Meteo: 10,000 calls/day free)
OCTOPUS_RATES_INTERVAL    = 1800  # 30 minutes
OCTOPUS_PROFILE_INTERVAL  = 86400 # 24 hours
COST_SETTLE_INTERVAL      = 21600 # 6 hours - backfill settled whole-house costs into daily_history
VPP_POLL_NORMAL_INTERVAL  = 600   # 10 minutes
VPP_POLL_ACTIVE_INTERVAL  = 60    # 1 minute (near/during event)
ACCUMULATOR_SAVE_INTERVAL = 300   # 5 minutes
STORM_WATCH_INTERVAL = 7200  # 2 hours


def _as_float(value, fallback):
    """Coerce a config value to float, returning fallback on blank/None/non-numeric.
    Config textfields come back as strings (blank after a dialog save), so float() must
    be guarded everywhere a pref reaches the battery-evaluate maths. The fallback is
    coerced too: several callers pass a string default ('35.04', '94'), which must never
    leak unconverted into arithmetic (e.g. `_as_float(blank, '94') / 100.0` → TypeError)."""
    try:
        if value not in (None, ""):
            return float(value)
    except (TypeError, ValueError):
        pass
    try:
        return float(fallback)
    except (TypeError, ValueError):
        return fallback


def _as_int(value, fallback):
    """Coerce a config value to int, returning fallback on blank/None/non-numeric.
    The fallback is coerced too so a string default can't leak into arithmetic."""
    try:
        if value not in (None, ""):
            return int(value)
    except (TypeError, ValueError):
        pass
    try:
        return int(fallback)
    except (TypeError, ValueError):
        return fallback


def _num_state(key, value, dp):
    """Build a numeric device-state dict that stores the real number (so Indigo's
    history charts it) with a `decimalPlaces` hint so the device UI renders it
    cleanly (e.g. '99.6', not the raw float '99.59999999999999'). We deliberately
    do NOT set an explicit uiValue — that would make the history log a separate
    `<state>_ui` text column. decimalPlaces alone is the idiomatic Indigo pattern
    (matches the first-party Ecowitt etc.). Coercion is guarded."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        v = 0.0
    return {"key": key, "value": round(v, dp), "decimalPlaces": dp}


def _atomic_write_json(path, data, indent=2):
    """Write JSON atomically: serialise to a sibling temp file, fsync, then
    os.replace() over the target.  A crash mid-write can never truncate the
    destination (matters for the never-pruned daily_history.json)."""
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=indent)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


ENERGY_VAR_INTERVAL  = 1800  # 30 minutes — write running totals to Indigo variables

# Storm-level hierarchy (mirrors storm_watch._LEVELS)
STORM_LEVELS = ["none", "yellow", "amber", "red"]

# Storm reserve SOC — the minimum the battery is held at during a warning so the
# house can ride out a power cut. CliveS's decision (26-Jun-2026): a FLAT 50% for
# ALL levels (yellow/amber/red). The overnight resilience-buffer import tops the
# battery up to this floor ONLY when below it and ONLY on a flat-rate tariff —
# above 50% there is no storm-driven grid charging (a 50% reserve is enough
# resilience; charging higher from the grid wastes money and self-sufficiency).
# Both constants are deliberately equal — kept separate only so a future user
# could re-differentiate severity without restructuring.
STORM_SOC_YELLOW = 50.0
STORM_SOC_AMBER  = 50.0

# Consecutive failed MeteoAlarm polls tolerated while HOLDING a previous storm
# level before decaying to "none" (poll cadence ~2h → 12 ≈ 24h; any real Met
# Office warning has expired by then). A failed poll is "unknown", never
# all-clear — see _check_storm_watch.
STORM_POLL_FAIL_LIMIT = 12

# Storm export-suppression release point. The storm override holds export off so
# the battery banks kWh ahead of a possible power cut — but ONLY while it is still
# filling toward the reserve. At/above this SOC the reserve is effectively in, and
# continuing to suppress export banks nothing extra: it just rams the battery to
# 100% (charging takes priority over export in self-consumption), which then clips
# every watt of PV above the 4 kW DNO export cap. Releasing here lets Solar Overflow
# take over — it throttles the charge and pushes the surplus out at the full export
# cap, so the battery creeps up slowly with headroom to spare instead of slamming
# full and curtailing. Mirrors POWER_CUT_LOCKOUT_SOC_FLOOR, which solves the
# identical "near-full battery clips solar" problem for the post-cut lockout.
STORM_EXPORT_RELEASE_PCT = 85.0

# Power cut lockout: suppress export for this many hours after grid is restored
POWER_CUT_LOCKOUT_HOURS = 4.0
# During that window, hold the export ban ONLY while SOC is below this floor.
# Above it, let export resume so flood-prevention can shed surplus — otherwise a
# near-full battery (e.g. 92%) under good solar would hit 100% and clip generation
# we could have exported. Below the floor there is no flood risk, so the
# precaution holds.
POWER_CUT_LOCKOUT_SOC_FLOOR = 85.0

# v5.50.0 — second, STRICTLY-ADDITIONAL release condition for the lockout (see
# _solar_refill_releases_lockout). A flat SOC floor cannot tell a January night
# from a July morning: on 20-Jul-2026 a 109-SECOND grid blip at 05:25 armed the
# lockout at SOC 75.6%, and export stayed off until 07:36 while the battery
# climbed to the 85% floor. Those ~3.3 kWh were all exportable under the 4 kW DNO
# cap at the time, and banking them instead cost us the afternoon headroom that
# would otherwise have absorbed the above-cap peak — so the lockout converted
# exportable morning kWh into afternoon curtailment. The overnight optimiser had
# already computed the day's actual power-cut resilience minimum as 10%.
#
# Never release on the strength of a forecast below this SOC, however good the day
# looks — a nearly-empty battery is not a resilience reserve, and the whole point
# of the lockout is that the grid has just proved itself unreliable.
POWER_CUT_LOCKOUT_MIN_SOC_PCT = 50.0
# Today's remaining surplus must exceed the gap-to-floor by this factor before we
# trust it. remaining_solar_kwh is already bias-corrected (battery_manager applies
# snapshot.bias_factor), so this is belt-and-braces on a calibrated figure rather
# than a hedge against raw forecast optimism.
POWER_CUT_LOCKOUT_REFILL_MARGIN = 1.25


def _export_locked_out(within_window, soc_pct, soc_floor):
    """Pure decision: should export be suppressed by the post-power-cut lockout?

    Suppress while inside the post-restore window AND the battery is below the
    SOC floor. At/above the floor, allow export (flood-prevention protects the
    solar). An unknown SOC fails safe (suppress) — the lockout must never
    fail-open. Returns True = export suppressed.
    """
    if not within_window:
        return False
    if soc_pct is None:
        return True
    return soc_pct < soc_floor


def _solar_refill_releases_lockout(is_daytime, soc_pct, min_soc_pct, battery_kwh,
                                   floor_kwh, remaining_solar_kwh, home_to_dusk_kwh,
                                   margin=POWER_CUT_LOCKOUT_REFILL_MARGIN):
    """Pure decision: does today's own solar make the lockout reserve self-refilling?

    Returns True when the day's remaining generation (net of house load to dusk)
    comfortably covers the gap between where the battery is now and the lockout
    SOC floor. In that case holding export off banks NOTHING the sun wasn't going
    to deliver anyway — it just risks driving the battery to 100% and clipping
    every watt above the DNO export cap. This is a release-EARLY condition only:
    the caller ORs it with the flat SOC floor, so it can never hold export off for
    longer than the floor alone would.

    Deliberately expressed in kWh rather than as an SOC percentage: an SOC-space
    formulation like `projected_dusk_pct >= floor_pct * margin` is unsatisfiable
    whenever `floor_pct * margin > 100` (85 * 1.25 = 106), because the projection
    is capped at the battery's physical capacity.

    Fail-safe in the same direction as _export_locked_out — night, unknown SOC, or
    a battery below min_soc_pct all return False, which HOLDS the lockout.

    NOT used by the storm override, which solves the same clipping problem via
    STORM_EXPORT_RELEASE_PCT. That is deliberate: a storm forecast means the solar
    may not arrive at all, so releasing export because a forecast looks good is
    exactly the wrong call there. Do not "restore symmetry" between the two.
    """
    if not is_daytime or soc_pct is None:
        return False
    if soc_pct < min_soc_pct:
        return False
    needed = max(0.0, floor_kwh - battery_kwh)
    if needed <= 0.0:
        return True          # already at/above the floor — nothing left to bank
    surplus = max(0.0, remaining_solar_kwh - home_to_dusk_kwh)
    return surplus >= needed * margin


def _backup_runtime_hours(soc_pct, floor_pct, capacity_kwh, home_w):
    """Pure: hours the battery could carry the house at the load it is drawing now.

    Usable energy is everything above the inverter's discharge cutoff — below that
    it stops, so counting it would overstate the backup. Returns None when the
    answer is unknowable rather than guessing: no SOC reading, no house load, a
    zero or negative load (the meter dropping out mid-outage), or a nonsense
    capacity. A caller that gets None should say nothing about runtime.

    This is a snapshot at the CURRENT load, not a forecast — the house will draw
    differently over the next few hours, and on a bright day the panels keep the
    battery topped up. It is the same simple figure the dashboard shows, which
    matters: two places quoting the same number should compute it the same way.
    """
    if soc_pct is None or home_w is None:
        return None
    try:
        soc      = float(soc_pct)
        floor    = float(floor_pct)
        capacity = float(capacity_kwh)
        load_w   = float(home_w)
    except (TypeError, ValueError):
        return None
    if capacity <= 0.0 or load_w <= 0.0:
        return None
    usable_kwh = max(0.0, soc - floor) / 100.0 * capacity
    return usable_kwh / (load_w / 1000.0)


def _format_runtime(hours):
    """Pure: turn a backup-runtime figure into something readable on a phone.

    Minutes under the hour, one decimal up to half a day, whole hours to two
    days, then days. Capped at "10+ days" because past that the number is
    meaningless — the load will have changed many times over.
    """
    if hours is None:
        return None
    try:
        h = float(hours)
    except (TypeError, ValueError):
        return None
    if h <= 0.0:
        return None
    if h >= 240.0:
        return "10+ days"
    if h < 1.0:
        return f"{h * 60:.0f} minutes"
    if h < 12.0:
        return f"{h:.1f} hours"
    if h < 48.0:
        return f"{h:.0f} hours"
    return f"{h / 24:.1f} days"


def _lockout_message(export_enabled, lockout_end_local, soc_floor_pct, outlook=None):
    """Pure: one plain sentence on what the export lockout is doing after a restore.

    Must name BOTH ways the lockout ends, or an early resume on the solar rule
    reads as a fault — which is exactly what happened on 26-Jul-2026, when export
    restarted at 74% after a message promising 85%. Returns None when there is
    nothing to say, so the caller can leave the paragraph out entirely.

    `outlook` is the answer to "will today's solar refill the reserve?" worked out
    at send time — a dict of {releases, surplus_kwh, needed_kwh, is_daytime} — or
    None when it could not be worked out. Given one, the message SAYS which way it
    has gone and shows the two figures, rather than leaving the reader to wonder
    which of the two rules will apply to them. It is still a forecast, so it is
    phrased as what the plugin thinks now and re-checks every minute — never as a
    promise. Without an outlook it falls back to naming both rules and no more.
    """
    if not export_enabled:
        return "Export is switched off in the plugin settings, so nothing is being held back."
    if not lockout_end_local:
        return None

    head = f"Export is held off until {lockout_end_local} as a precaution."
    if not outlook:
        return (f"{head} It restarts early if the battery reaches {soc_floor_pct:.0f}%, "
                f"or if today's solar can refill that reserve on its own.")

    surplus = outlook.get("surplus_kwh")
    needed  = outlook.get("needed_kwh")
    figures = ""
    if surplus is not None and needed is not None:
        figures = f" ({surplus:.1f} kWh spare against the {needed:.1f} kWh needed)"

    if outlook.get("releases"):
        return (f"{head} But today's solar covers that reserve on its own{figures}, so "
                f"export should restart within the minute. It re-checks every minute.")
    if not outlook.get("is_daytime"):
        return (f"{head} There is no solar left today to refill the reserve any sooner, "
                f"so it stands until the battery reaches {soc_floor_pct:.0f}% or the "
                f"window ends.")
    return (f"{head} Today's solar does not cover that reserve yet{figures}, so it stands "
            f"until the battery reaches {soc_floor_pct:.0f}%, the forecast improves, or "
            f"the window ends. It re-checks every minute.")


# VPP state machine values
VPP_IDLE         = "idle"
VPP_ANNOUNCED    = "announced"
VPP_PRE_CHARGING = "pre_charging"
VPP_ACTIVE       = "active"

# Axle VPP SOC calculation constants (from SigenergySolar)
VPP_DISCHARGE_EFFICIENCY  = 0.97
BATTERY_CAPACITY_KWH      = 35.04

# Fallback export rate, pence per kWh, used only when no live or per-day rate is
# known (Octopus Outgoing flat 12p, live here since 26-Mar-2026). It was written
# out as a bare 12.0 in nine places, so a tariff change meant finding all nine.
DEFAULT_EXPORT_RATE_P = 12.0


def _serialise_vpp_event(event):
    """Serialise an Axle VPP event dict to JSON-safe primitives.

    datetimes -> ISO-8601 strings; the `raw` API blob is dropped (not needed to
    resume a window). Returns None for a falsy event. Pure — unit-tested.
    """
    if not event:
        return None

    def _iso(v):
        return v.isoformat() if hasattr(v, "isoformat") else v

    return {
        "start_time":            _iso(event.get("start_time")),
        "end_time":              _iso(event.get("end_time")),
        "import_export":         event.get("import_export", "export"),
        "duration_hrs":          event.get("duration_hrs"),
        "forecast_dispatch_kwh": event.get("forecast_dispatch_kwh"),
        "estimated_revenue_p":   event.get("estimated_revenue_p"),
    }


def _deserialise_vpp_event(d):
    """Inverse of _serialise_vpp_event — ISO strings back to tz-aware UTC datetimes.

    Returns None when start/end are missing or unparseable, so a corrupt payload
    can never resurrect a dangling VPP state. Pure — unit-tested.
    """
    if not d:
        return None
    try:
        start = datetime.fromisoformat(d["start_time"])
        end   = datetime.fromisoformat(d["end_time"])
    except (KeyError, TypeError, ValueError):
        return None
    # The VPP state machine works in UTC end-to-end — normalise both ends.
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    start = start.astimezone(timezone.utc)
    end   = end.astimezone(timezone.utc)
    return {
        "start_time":            start,
        "end_time":              end,
        "import_export":         d.get("import_export", "export"),
        "duration_hrs":          d.get("duration_hrs")
                                 or (end - start).total_seconds() / 3600.0,
        "forecast_dispatch_kwh": d.get("forecast_dispatch_kwh"),
        "estimated_revenue_p":   d.get("estimated_revenue_p"),
    }


def _vpp_resume_decision(vpp_state, event, now, tail_minutes=2):
    """Decide how to handle a persisted VPP window found on restart.

    Returns:
        "idle"   — nothing to resume (state IDLE or no usable event)
        "ended"  — the window closed while the plugin was down; clean up hardware
        "resume" — the window is still open; restore the state machine

    Pure decision function (no side effects) so the restart contract is
    unit-tested without Indigo or hardware.
    """
    if vpp_state == VPP_IDLE or not event:
        return "idle"
    end_time = event.get("end_time")
    if end_time is None:
        return "idle"
    if now >= end_time + timedelta(minutes=tail_minutes):
        return "ended"
    return "resume"


# ============================================================
# Plugin log file (daily rotation, 14-day retention)
# ============================================================

_plugin_log_fh   = None
_plugin_log_date = None

def _ensure_plugin_log(data_dir):
    """Open (or rotate) the daily plugin log file in data_dir/logs/."""
    global _plugin_log_fh, _plugin_log_date
    today = datetime.now().strftime("%Y-%m-%d")
    if _plugin_log_date == today and _plugin_log_fh is not None:
        return  # already open for today
    # Close previous file if open
    if _plugin_log_fh is not None:
        try:
            _plugin_log_fh.close()
        except Exception:
            pass
    log_dir  = os.path.join(data_dir, "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, f"{today}.log")
    try:
        _plugin_log_fh = open(log_path, "a", encoding="utf-8")  # noqa: SIM115
    except Exception:
        _plugin_log_fh = None
    _plugin_log_date = today
    # Purge log files older than 14 days
    cutoff = datetime.now() - timedelta(days=14)
    try:
        for fname in os.listdir(log_dir):
            if fname.endswith(".log") and len(fname) == 14:
                try:
                    fdate = datetime.strptime(fname[:10], "%Y-%m-%d")
                    if fdate < cutoff:
                        os.remove(os.path.join(log_dir, fname))
                except (ValueError, OSError):
                    pass
    except OSError:
        pass


import logging


_LOG_LEVELS = {
    "DEBUG":   logging.DEBUG,
    "INFO":    logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR":   logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}


def _lvl(level):
    """Map a level NAME to a Python logging int.

    indigo.server.log(level=...) wants an int. A STRING is silently ignored
    and the line logs as plain Info, which hid every WARNING and ERROR raised
    through log() until this was corrected (21-07-2026).
    """
    if isinstance(level, int):
        return level
    return _LOG_LEVELS.get(str(level).upper(), logging.INFO)


def log(message, level="INFO"):
    """Custom log function — writes to Indigo event log and daily plugin log file."""
    ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    indigo.server.log(f"[{ts}] {message}", level=_lvl(level))
    if _plugin_log_fh is not None:
        try:
            _plugin_log_fh.write(f"{ts} [{level:<7}] {message}\n")
            _plugin_log_fh.flush()
        except Exception:
            pass


# Latch so a missing tz database is reported ONCE, not on every 60-second
# evaluate. A plain module global is unreliable inside plugin callbacks, so the
# house idiom is a mutable container.
_TZ_WARN_STATE = {"warned": False}


def _warn_no_tzdb():
    """Report — loudly, once — that Europe/London could not be resolved.

    Every local-time answer in this module is a decision input: tariff windows,
    the midnight rollover, dawn/dusk, the VPP daytime/dark mode choice. The old
    code met a missing tz database with `except: carry on in UTC`, which is an
    answer an hour wrong for the eight months of BST with nothing logged. Degrade
    visibly instead. In practice unreachable — zoneinfo is stdlib from 3.9, so
    this survives a Contents/Packages wipe that would take pytz with it.
    """
    if _TZ_WARN_STATE["warned"]:
        return
    _TZ_WARN_STATE["warned"] = True
    log("No Europe/London time zone available (neither zoneinfo nor pytz) — "
        "local times are falling back to UTC and will be an hour out during "
        "BST. Tariff windows, midnight rollover and VPP daytime/dark detection "
        "are all affected.", level="ERROR")


def _london_now():
    """Current time as an AWARE Europe/London datetime.

    The single entry point for "what is the local time now" in this module.
    """
    tz = _london_tz()
    if tz is None:
        _warn_no_tzdb()
        return datetime.now(timezone.utc)
    return datetime.now(timezone.utc).astimezone(tz)


def _london_today():
    """Today's date in Europe/London."""
    return _london_now().date()


def _local_time(dt, fmt="%H:%M"):
    """Format a UTC-aware datetime in Europe/London local time (BST/GMT).

    All datetimes from the Axle API and VPP state machine are UTC-aware.
    Displaying them without conversion shows UTC, which is 1 hour behind
    BST during British Summer Time (late March — late October).

    A naive datetime is formatted as-is: callers holding naive values treat them
    as already-local, and stamping a zone on one would invent information.
    """
    if _london_tz() is None:
        _warn_no_tzdb()
        return dt.strftime(fmt)
    return _to_london(dt).strftime(fmt)


def _local_today_str():
    """Return today's date (YYYY-MM-DD) in Europe/London, matching _check_midnight.

    The midnight rollover and the accumulator save both stamp today_date in
    Europe/London. The initialiser and the on-disk accumulator restore must use
    the SAME basis — a naive datetime.now() on a UTC-hosted server is the previous
    day for the first hour after local midnight in BST, which would make the loader
    reject (and silently drop) a fresh day's accumulators on a restart in that window.
    """
    return _london_today().strftime("%Y-%m-%d")


class Plugin(indigo.PluginBase):
    """SigenEnergyManager Indigo Plugin.

    Manages a Sigenergy solar/battery system with self-sufficiency as the
    primary goal. Only imports from grid if battery cannot reach next day's
    solar generation window at the configured minimum SOC. Exports to
    prevent 100% battery cap during peak solar generation.
    """

    # ================================================================
    # Plugin Lifecycle
    # ================================================================

    def __init__(self, plugin_id, plugin_display_name, plugin_version, plugin_prefs):
        super().__init__(plugin_id, plugin_display_name, plugin_version, plugin_prefs)

        self.timestamp_enabled = bool(plugin_prefs.get("timestampEnabled", True))
        if install_timestamp_filter:
            self._ts_filter = install_timestamp_filter(self, enabled=self.timestamp_enabled)
        else:
            self._ts_filter = None

        # Startup banner moved to showPluginInfo on demand (revised 25-May-2026 per Jay).

        self.debug = plugin_prefs.get("showDebugInfo", False)

        # Data directory for cache files
        self.data_dir = self._get_data_dir()

        # Initialise module instances (configured properly in startup())
        self.modbus   = None
        self.forecast  = None
        self.octopus  = None
        self.manager  = BatteryManager()
        # Export + gas MPAN/serial — populated by _init_modules
        self.export_mpan   = ""
        self.export_serial = ""
        self.gas_mprn      = ""
        self.gas_serial    = ""
        self.axle     = None

        # Indigo trigger registry — populated by triggerStartProcessing/Stop.
        # Maps trigger.id -> trigger object so _trigger_event can find triggers
        # whose pluginTypeId matches the event being fired.  Without this
        # registry, indigo.trigger.execute() has no trigger object to fire.
        self.event_triggers = {}

        # Latest data from each module
        self.latest_inverter_data = {}
        self.latest_forecast_data = {}
        self.latest_rates_data    = {}
        self.latest_decision      = None

        # Web dashboard server (started in startup, stopped in shutdown)
        self.web_dashboard        = None

        # Reentrant lock for self.store. Indigo's runConcurrentThread runs in a
        # background thread while action callbacks, menu items, deviceUpdated and
        # device-config submissions all run on the main thread.  Without a lock,
        # a composite read-modify-write on store (e.g. _act_on_decision flipping
        # flags while an action callback is also writing) can race.  RLock so the
        # tick can call _act_on_decision which can call _trigger_event etc.
        self._state_lock = threading.RLock()

        # Poll timers
        self.store                   = {}   # mutable state dict (replaces globals)
        self.store["last_modbus"]    = 0.0
        self.store["last_manager"]   = 0.0
        self.store["last_forecast"]   = 0.0
        self.store["last_octopus"]   = 0.0
        self.store["last_profile"]   = 0.0
        self.store["last_vpp"]            = 0.0
        self.store["last_acc_save"]       = 0.0
        self.store["last_cost_settle"]    = 0.0
        self.store["last_manager_action"] = ""
        self.store["last_overflow_cap_w"] = 0

        # Daily energy accumulators (kWh, reset at midnight)
        self.store["pv_daily_kwh"]              = 0.0
        self.store["grid_import_daily_kwh"]     = 0.0
        self.store["grid_export_daily_kwh"]     = 0.0
        self.store["home_daily_kwh"]            = 0.0
        self.store["peak_soc"]                  = 0.0
        self.store["min_soc"]                   = 100.0
        self.store["peak_pv_w"]                 = 0
        self.store["peak_pv_time"]              = ""
        self.store["today_date"]                = _local_today_str()
        # Lifetime total anchors for daily delta computation (set on first Modbus read)
        self.store["pv_lifetime_start_kwh"]     = None
        self.store["import_lifetime_start_kwh"] = None
        self.store["export_lifetime_start_kwh"] = None

        # VPP state machine
        self.store["vpp_state"]            = VPP_IDLE
        self.store["vpp_active"]           = False
        self.store["vpp_event"]            = None
        self.store["vpp_pre_charge_soc"]   = 0.0
        self.store["vpp_export_start_kwh"]  = 0.0   # grid_export_daily_kwh at event start
        self.store["vpp_last_export_kwh"]   = 0.0   # export kWh during last completed event
        self.store["vpp_charge_stopped"]    = False # True once pre-charge import has ended
        self.store["vpp_10min_warning_sent"] = False # T-10min warning latched
        self.store["vpp_last_snapshot_at"]   = 0.0    # time.time() of last detailed snapshot log

        # Scheduled import state
        self.store["import_active"]          = False
        self.store["import_scheduled_time"]  = None
        self.store["import_target_soc"]      = 0.0

        # Export state (export limit is set once at startup; no dynamic tracking needed)
        self.store["export_active"]   = False

        # Storm watch state
        self.store["last_storm_watch"]    = 0.0    # time.time() of last poll
        self.store["last_energy_var"]     = 0.0    # time.time() of last variable write
        self._energy_var_ids: dict        = {}     # cached variable IDs by name

        # Half-hourly SQLite logging — delta anchors (reset each write)
        self.store["hh_anchor_pv_kwh"]     = None  # cumulative PV at last slot boundary
        self.store["hh_anchor_import_kwh"] = None  # cumulative import at last slot boundary
        self.store["hh_anchor_export_kwh"] = None  # cumulative export at last slot boundary
        self.store["hh_anchor_home_kwh"]   = None  # cumulative home at last slot boundary
        self.store["hh_anchor_soc_pct"]    = None  # SOC at start of current slot
        self.store["storm_level"]         = "none" # current storm level
        # Defaults; _load_accumulators() restores the persisted values (accumulators.json
        # is the plugin's reliable cross-restart store — written every poll) so an
        # already-active warning does not re-send its Pushover on every plugin restart.
        self.store["storm_alerted_level"] = "none" # level at which alert was last sent
        self.store["storm_export_suppressed"] = False  # is export held off right now?

        # Power cut detection
        self.store["grid_status_prev"] = "On-grid"  # previous poll's gridStatus
        # Rolling log of grid-status transitions (most recent 100 entries).
        # Each entry is a human-readable string with timestamp + transition +
        # duration of the outage when it ends.  Surfaced via menuShowPowerCutLog
        # and the web dashboard /api/status.
        self.store["power_cut_events"]      = []
        self.store["power_cut_started_at"]  = None

        # Solar overflow state (daytime charge-cap export)
        self.store["solar_overflow_active"]      = False
        self.store["solar_overflow_charge_cap_w"] = 0
        # When the cap was last released (UTC datetime), for the manager's v3.10
        # re-engage dwell. None = not released this run, which the manager treats as
        # "not blocked" — a fresh start must never be locked out of exporting.
        self.store["solar_overflow_released_at"]  = None

        # Flood prevention state (overnight pre-drain).
        # Persisted to pluginPrefs so a mid-pre-drain plugin restart doesn't leave
        # the inverter cutoff register raised but the store flag empty — which
        # would let _verify_ems_registers() reset the hardware floor and break
        # the in-progress drain. Rehydrated in startup() below.
        self.store["flood_prev_target_soc"] = None  # set when pre-drain export is active

        # Import charge-cutoff backstop (hardware ceiling while a grid import is
        # active — see _set_import_cutoff). Deliberately NOT rehydrated at startup:
        # import_active does not survive a restart (the stuck-mode recovery returns
        # the inverter to self-consumption) and _init_modules parks 40047 at 100%.
        self.store["import_charge_cutoff_pct"] = None

        # Consumption profile (48 slots)
        self.store["consumption_profile"] = []

        # Long-lived home-load profile accumulators (persist across days; never reset at midnight)
        # Built from real homePowerWatts inverter readings, one reading per Modbus poll.
        self.store["home_profile_watts_sum"] = [0.0] * 48
        self.store["home_profile_count"]     = [0]   * 48

        self._load_accumulators()
        self._load_home_profile()   # restore accumulated inverter profile from disk

    def startup(self):
        _ensure_plugin_log(self.data_dir)
        log(f"{PLUGIN_NAME} v{self.pluginVersion} starting")

        # ── Pref migrations ──────────────────────────────────────────────────
        # v3.0: raise dawnSocTarget minimum to 15% so there is a real buffer
        # above the 10% health floor on poor solar days.  Direct file edits are
        # overwritten by Indigo on shutdown, so we correct the value here and
        # let Indigo persist it naturally.
        _dawn_target = _as_float(self.pluginPrefs.get("dawnSocTarget"), "10")
        if _dawn_target < 15.0:
            self.pluginPrefs["dawnSocTarget"] = "15"
            log(
                f"[Migration] dawnSocTarget raised from {_dawn_target:.0f}% to 15% "
                f"(minimum recommended to buffer above 10% health floor)"
            )
        # (The v5.9 pollInterval 60/120 -> 10s migration was DELETED in v5.43:
        # it had no one-shot gate, so a user deliberately choosing the
        # still-offered 60s/120s ConfigUI options got silently reverted to 10s
        # on every restart, forever. Anyone on the pre-5.9 legacy default has
        # long since been migrated — ~30 versions have passed.)

        self._init_modules()
        self._init_timeseries_db()
        if self.forecast:
            self.forecast.load_correction_factor()
        # Pre-populate latest_forecast_data from disk cache so the first manager
        # evaluation has forecast data available (disk cache was loaded in
        # OpenMeteoForecast.__init__; this propagates it into plugin.py's dict).
        self._refresh_forecast()
        self.store["last_forecast"] = time.time()

        # Rehydrate flood prevention target SOC from pluginPrefs.
        # If the plugin restarts mid-pre-drain, the inverter still has its
        # discharge cutoff register raised to this target — without rehydrating
        # we'd lose the flag and _verify_ems_registers() would lower the cutoff
        # back to the health floor mid-drain.
        # Clear any stale persisted import charge-cutoff: the backstop does not
        # survive a restart (register parked at 100% by _init_modules above) so a
        # leftover pref would make _verify_ems_registers expect the wrong value.
        if self.pluginPrefs.get("importChargeCutoffPct", ""):
            self._set_import_cutoff(None)

        _flood_prev_str = self.pluginPrefs.get("floodPrevTargetSoc", "")
        if _flood_prev_str:
            try:
                _flood_prev_val = float(_flood_prev_str)
                if _flood_prev_val > 0:
                    self.store["flood_prev_target_soc"] = _flood_prev_val
                    self.store["export_active"]         = True
                    log(
                        f"[Manager] Flood prevention rehydrated from prefs: "
                        f"target {_flood_prev_val:.0f}% — pre-drain still in progress"
                    )
            except (ValueError, TypeError):
                pass

        # Resume an in-progress Axle VPP window if the plugin restarted mid-event.
        # The window was restored from accumulators.json by _load_accumulators;
        # this makes the time-based resume/cleanup decision now modbus is up.
        self._rehydrate_vpp_state()

        # Set initial state images for all devices that already exist
        # (deviceStartComm handles newly created devices; this handles existing ones on reload)
        for dev in indigo.devices.iter("self"):
            self._set_device_initial_state(dev)
        log(f"{PLUGIN_NAME} ready")

        try:
            self.web_dashboard = WebDashboard(self, port=WEB_DASHBOARD_PORT)
            self.web_dashboard.start()
            host = self._resolve_dashboard_host()
            log(f"[Web] Dashboard at http://{host}:{WEB_DASHBOARD_PORT}")
        except Exception as exc:
            log(f"[Web] Dashboard failed to start: {exc}", level="ERROR")

        # Subscribe to variable changes so an external automation (or the
        # Indigo client UI) can toggle pause via the `sigen_manager_paused`
        # variable.  Creating the variable on startup means users discover the
        # control without having to read the docs.
        try:
            indigo.variables.subscribeToChanges()
            self._ensure_pause_variable()
        except Exception as exc:
            log(f"[Startup] Variable subscription failed: {exc}", level="ERROR")

        # Write the shared site_config.json so the companion optimiser script
        # always uses the same numbers as the plugin (no constant drift).
        self._write_site_config()

        # Auto-update check (best-effort, daily-cached, fully silent on failure).
        try:
            self._check_for_update()
        except Exception as exc:
            self.logger.debug(f"[Update] check error: {exc}")

    def _write_site_config(self):
        """Write a shared site_config.json that companion scripts can read.

        The optimiser script (Python Scripts/openmeteo_battery_optimiser.py)
        used to hardcode duplicates of every site constant — a maintenance
        hazard that caused FLOOD_PREV_FORECAST_MULT to drift (script had 4.0
        while the plugin had 3.0).  Now the plugin is the single source of
        truth: every plugin start, and every PluginConfig save, writes this
        JSON to a stable path.  The script reads it with fallbacks to its
        existing constants in case the file is missing.

        Written to the Python Scripts folder — same place the optimiser
        already reads openmeteo_forecast.json from, so no extra plumbing.
        """
        prefs = self.pluginPrefs
        try:
            capacity   = _as_float(prefs.get("batteryCapacityKwh"), "35.04")
        except (TypeError, ValueError):
            capacity = 35.04
        try:
            efficiency = _as_float(prefs.get("batteryEfficiency"), "94") / 100.0
        except (TypeError, ValueError):
            efficiency = 0.94
        try:
            inv_kw     = _as_float(prefs.get("inverterMaxKw"), "10.0")
        except (TypeError, ValueError):
            inv_kw     = 10.0
        try:
            export_kw  = _as_float(prefs.get("maxExportKw"), "4.0")
        except (TypeError, ValueError):
            export_kw  = 4.0
        try:
            dawn_pct   = _as_float(prefs.get("dawnSocTarget"), "10")
        except (TypeError, ValueError):
            dawn_pct   = 10.0
        try:
            winter_pct = _as_float(prefs.get("winterBufferPct"), "20")
        except (TypeError, ValueError):
            winter_pct = 20.0
        try:
            health_pct = _as_float(prefs.get("batteryHealthCutoff"), "1")
        except (TypeError, ValueError):
            health_pct = 1.0
        # Site coordinates — IndigoSecrets first, PluginConfig next, None last.
        # No built-in default: a fresh install must explicitly configure a
        # location (in IndigoSecrets.py or the PluginConfig fields). If neither
        # is set the forecast feature logs an ERROR and skips itself.  The
        # site_config publish below writes None into the JSON in that case.
        def _coord(secrets_value, prefs_key):
            if secrets_value is not None:
                try:
                    return float(secrets_value)
                except (TypeError, ValueError):
                    return None
            raw = (prefs.get(prefs_key) or "").strip()
            if not raw:
                return None
            try:
                return float(raw)
            except (TypeError, ValueError):
                return None
        site_lat = _coord(SITE_LATITUDE,  "siteLatitude")
        site_lon = _coord(SITE_LONGITUDE, "siteLongitude")

        # Export rate — best-effort lookup from latest_rates_data; default 12p.
        try:
            export_rate_p = float(self.latest_rates_data.get("export_rate_p", 0.0))
            if export_rate_p <= 0:
                export_rate_p = DEFAULT_EXPORT_RATE_P
        except (TypeError, ValueError):
            export_rate_p = DEFAULT_EXPORT_RATE_P

        # v5.15: publish the auto-calibrated consumption profile so the
        # optimiser script uses the SAME real-inverter-derived numbers the
        # plugin uses, instead of its older Octopus-grid-import-only file.
        # The Octopus profile under-counts true home consumption because it
        # only sees what the grid imports — everything covered by solar or
        # battery during the day is invisible to the smart meter.  For a
        # solar+battery house this dragged the script's daily total down
        # to ~11 kWh when actual usage is ~22 kWh, causing the script to
        # promise overnight exports the plugin then declined (confirmed
        # 12/13-May-2026 incident).
        profile_48 = self.store.get("consumption_profile", []) or []
        consumption_block = None
        if len(profile_48) == 48:
            # Aggregate half-hourly slots into hourly: hour H = slot 2H + slot 2H+1
            hourly_wd = {
                str(h): round(profile_48[2 * h] + profile_48[2 * h + 1], 4)
                for h in range(24)
            }
            daily_wd = round(sum(profile_48), 2)
            # Match plugin's _build_manager_snapshot weekend ratio (1.30x)
            hourly_we = {
                str(h): round(float(hourly_wd[str(h)]) * 1.30, 4)
                for h in range(24)
            }
            daily_we = round(daily_wd * 1.30, 2)
            consumption_block = {
                "source":           "sigen_inverter_48slot",
                "daily_kwh_weekday": daily_wd,
                "daily_kwh_weekend": daily_we,
                "weekend_multiplier": 1.30,
                "hourly_kwh": {
                    "weekday": hourly_wd,
                    "weekend": hourly_we,
                },
            }

        data = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "generated_by": f"SigenEnergyManager v{self.pluginVersion}",
            "battery": {
                "capacity_kwh":       capacity,
                "efficiency":         efficiency,
                "health_cutoff_pct":  health_pct,
            },
            "inverter": {
                "max_kw":         inv_kw,
                "max_export_kw":  export_kw,
            },
            "tariff": {
                "export_rate_p": export_rate_p,
            },
            "resilience": {
                "summer_pct":           dawn_pct,
                "winter_pct":           winter_pct,
                "power_cut_buffer_kwh": 3.5,
            },
            "flood_prevention": {
                "soc_threshold_pct": FLOOD_PREV_SOC_THRESHOLD_PCT,
                "target_pct":        FLOOD_PREV_TARGET_PCT,
                "forecast_mult":     FLOOD_PREV_FORECAST_MULT,
            },
            "site": {
                "latitude":  site_lat,
                "longitude": site_lon,
                "timezone":  "Europe/London",
            },
        }
        # Only include the consumption block when we have a real 48-slot
        # profile so script callers can distinguish "plugin published" from
        # "fall back to local file".
        if consumption_block is not None:
            data["consumption"] = consumption_block
        path = ("/Library/Application Support/Perceptive Automation/"
                "Python Scripts/sigen_site_config.json")
        try:
            # Atomic — the optimiser script reads this file and must never
            # see a half-written copy.
            _atomic_write_json(path, data)
            self.logger.debug(f"[SiteConfig] Wrote shared config to {path}")
        except OSError as exc:
            self.logger.warning(f"[SiteConfig] Cannot write {path}: {exc}")

    def _ensure_pause_variable(self):
        """Create `sigen_manager_paused` in the Sigenergy folder if it doesn't
        already exist.  The current paused state is mirrored in.  Indigo
        variables are always strings — "true" / "false" lower-case."""
        if "sigen_manager_paused" in indigo.variables:
            # Seed the in-memory flag from the variable so a restart-while-paused
            # stays paused (the _evaluate_manager gate self-heals the device label).
            var = indigo.variables["sigen_manager_paused"]
            self.store["manager_paused"] = (
                str(var.value).strip().lower() in ("true", "1", "yes", "on", "paused")
            )
            if self.store["manager_paused"]:
                log("[Setup] Battery manager is PAUSED at startup "
                    "(sigen_manager_paused=true) — not driving the inverter until resumed.")
            return
        folder_id = self._sigenergy_folder_id()
        try:
            indigo.variable.create(
                "sigen_manager_paused",
                value="false",
                folder=folder_id,
            )
            self.store["manager_paused"] = False
            log("[Setup] Created variable 'sigen_manager_paused' — set to 'true' "
                "to pause the battery manager from anywhere in Indigo.")
        except Exception as exc:
            log(f"[Setup] Cannot create pause variable: {exc}", level="WARNING")

    def variableUpdated(self, orig_var, new_var):
        """Indigo callback when any subscribed variable changes value.

        Picks up `sigen_manager_paused` toggles and mirrors them into
        self.store["manager_paused"].  Plain string compare; accepts the usual
        truthy strings ('true', '1', 'yes', 'on').
        """
        super().variableUpdated(orig_var, new_var)
        if new_var.name != "sigen_manager_paused":
            return
        with self._state_lock:
            new_value = str(new_var.value).strip().lower()
            paused = new_value in ("true", "1", "yes", "on", "paused")
            if self.store.get("manager_paused", False) != paused:
                self._set_manager_paused(
                    paused, f"sigen_manager_paused={new_var.value!r}"
                )

    def shutdown(self):
        log(f"{PLUGIN_NAME} shutting down")
        if self.web_dashboard:
            try:
                self.web_dashboard.stop()
                log("[Web] Dashboard stopped")
            except Exception as exc:
                log(f"[Web] Dashboard stop error: {exc}", level="ERROR")
        global _plugin_log_fh
        if _plugin_log_fh is not None:
            try:
                _plugin_log_fh.close()
            except Exception:
                pass
            _plugin_log_fh = None
        if self.modbus and self.modbus.connected:
            # Return to self-consumption on shutdown
            try:
                self.modbus.set_self_consumption()
            except Exception:
                pass
            self.modbus.disconnect()
        self._save_accumulators()

    # ────────────────────────────────────────────────────────────────────────
    # Mac sleep / wake
    # ────────────────────────────────────────────────────────────────────────
    # Defensive: an unattended Mac could sleep for hours. If the inverter is
    # in a forced mode (force-charge, night-export) when sleep happens, that
    # mode would persist until wake — overcharging or over-discharging the
    # battery. Return to self-consumption (the safe baseline) on sleep, then
    # let the manager re-evaluate normally on wake. Modbus reconnects lazily
    # on next poll cycle; we only need to restart the web dashboard.
    def prepare_to_sleep(self):
        log("Mac going to sleep — returning inverter to self-consumption and stopping dashboard")
        if self.web_dashboard:
            try:
                self.web_dashboard.stop()
            except Exception as exc:
                log(f"[Web] Dashboard stop error on sleep: {exc}", level="ERROR")
        # Return to the safe self-consumption baseline AND release any raised
        # discharge-cutoff floor / VPP engagement (see _disengage_to_safe_baseline),
        # so an unattended sleep can't strand the battery above a raised SOC and
        # force grid import. The manager re-evaluates and re-applies on wake.
        self._disengage_to_safe_baseline("Sleep")
        if self.modbus and self.modbus.connected:
            try:
                self.modbus.disconnect()
            except Exception:
                pass
        self._save_accumulators()
        super().prepare_to_sleep()
    prepareToSleep = prepare_to_sleep

    def wake_up(self):
        log("Mac woke — restarting dashboard; modbus reconnects on next poll, manager re-evaluates within 60s")
        super().wake_up()
        try:
            self.web_dashboard = WebDashboard(self, port=WEB_DASHBOARD_PORT)
            self.web_dashboard.start()
            host = self._resolve_dashboard_host()
            log(f"[Web] Dashboard at http://{host}:{WEB_DASHBOARD_PORT}")
        except Exception as exc:
            log(f"[Web] Dashboard restart on wake failed: {exc}", level="ERROR")
        # Force a fresh modbus poll on the next tick (not wait the full
        # interval). This makes the system feel responsive after sleep.
        self.store["last_modbus"] = 0.0
        self.store["last_manager"] = 0.0
    wakeUp = wake_up

    # ------------------------------------------------------------------ #
    # Web dashboard data provider                                          #
    # ------------------------------------------------------------------ #

    def get_dashboard_data(self):
        """Return a dict of live system data for the web dashboard /api/status."""
        try:
            # v5.45.0: millisecond snapshot under the lock, then the whole
            # payload builds lock-free from consistent local copies — handler
            # threads used to read store live (torn composite reads
            # possible around the midnight counter reset).
            with self._state_lock:
                store  = dict(self.store)
                inv    = self.latest_inverter_data  or {}
                fcast  = self.latest_forecast_data  or {}
                rates  = self.latest_rates_data     or {}
                dec    = self.latest_decision

            tariff_info = rates.get("tariff_info", {})
            tracker     = rates.get("tracker", {})

            pv_w   = int(inv.get("pvPowerWatts",     0))
            bat_w  = int(inv.get("batteryPowerWatts", 0))
            grid_w = int(inv.get("gridPowerWatts",    0))
            home_w = int(inv.get("homePowerWatts",    0))
            soc    = float(inv.get("batterySoc",      0.0))

            # Hourly forecast: {hour_label: kWh}
            # Scaled by today's band factor so the bars (and their per-hour kWh
            # tooltips) sum to the corrected headline forecast rather than the raw
            # model total — same shape-preserving scaling _write_optimiser_file
            # applies to the optimiser JSON. The cached _hourly_p50_* buckets stay
            # raw: bands are recomputed nightly, so the cache must hold the
            # model's own numbers.
            raw_hourly = fcast.get("_hourly_p50_today", {})
            try:
                hour_factor = float(fcast.get("biasFactorToday", 1.0) or 1.0)
            except (TypeError, ValueError):
                hour_factor = 1.0
            hourly = {}
            for key in sorted(raw_hourly.keys()):
                wh = raw_hourly[key]
                try:
                    hour = int(str(key).split(" ")[1].split(":")[0])
                except (IndexError, ValueError):
                    continue
                hourly[f"{hour:02d}:00"] = round(wh / 1000.0 * hour_factor, 2)

            # Self-sufficiency
            home_kwh   = store.get("home_daily_kwh", 0.0)
            import_kwh = store.get("grid_import_daily_kwh", 0.0)
            if home_kwh > 0:
                self_suff = round(max(0.0, (home_kwh - import_kwh) / home_kwh * 100.0), 1)
            else:
                self_suff = 100.0

            # Tomorrow revenue estimate: surplus solar × export rate.
            # Surplus is the optimistic forecast minus expected daily need
            # (auto-calibrated from inverter data, see _build_manager_snapshot).
            # Export rate defaults to 12p (Octopus Outgoing flat) — overridden if
            # the rates_data feed publishes a different export rate.
            tomorrow_solar = float(fcast.get("correctedTomorrowKwh", 0.0))
            profile        = store.get("consumption_profile", []) or []
            tomorrow_need  = sum(profile) if len(profile) == 48 else 22.0
            tomorrow_surplus = max(0.0, tomorrow_solar - tomorrow_need)
            export_rate_p  = DEFAULT_EXPORT_RATE_P
            try:
                rates_export = float(rates.get("export_rate_p", 0.0))
                if rates_export > 0:
                    export_rate_p = rates_export
            except (TypeError, ValueError):
                pass
            tomorrow_revenue_gbp = round(tomorrow_surplus * export_rate_p / 100.0, 2)

            # ---- Today's economics ----
            # All four numbers — actual import cost, export revenue, what the
            # home would have cost on grid alone, and the net financial benefit
            # of having solar today.  None if no import rate is known yet.
            #
            # Prefer the tariffMonitor device state (always populated from the
            # Octopus refresh cycle) over latest_rates_data which is empty for
            # ~30 minutes after a plugin restart.
            import_rate_p = None
            try:
                r = float(tracker.get("today_p") or 0.0)
                if r > 0:
                    import_rate_p = r
            except (TypeError, ValueError):
                pass
            if import_rate_p is None:
                tariff_dev = self._find_device("tariffMonitor")
                if tariff_dev:
                    try:
                        r = float(tariff_dev.states.get("rateToday", "") or 0.0)
                        if r > 0:
                            import_rate_p = r
                    except (TypeError, ValueError):
                        pass
            if import_rate_p is None:
                # Final fallback: the elec_unit_rate_p Indigo variable, which
                # is written every 30 minutes by _write_energy_summary_variables
                # and persists across plugin restarts (unlike device states,
                # which deviceStartComm clears to "Initialising").
                try:
                    if "elec_unit_rate_p" in indigo.variables:
                        r = float(indigo.variables["elec_unit_rate_p"].value or 0.0)
                        if r > 0:
                            import_rate_p = r
                except (TypeError, ValueError, KeyError):
                    pass

            today_econ = self._compute_daily_economics(
                home_kwh      = home_kwh,
                import_kwh    = import_kwh,
                export_kwh    = float(store.get("grid_export_daily_kwh", 0.0)),
                import_rate_p = import_rate_p,
                export_rate_p = export_rate_p,
            )
            yesterday_econ, yesterday_date = self._yesterday_economics(
                export_rate_p          = export_rate_p,
                fallback_import_rate_p = import_rate_p,
            )
            periods = self._period_economics_summary(
                export_rate_p          = export_rate_p,
                fallback_import_rate_p = import_rate_p,
            )
            calendar_months = self._calendar_months_summary(
                export_rate_p          = export_rate_p,
                fallback_import_rate_p = import_rate_p,
            )
            # Isolated so a fault in the (newer, network-touching) whole-house
            # block can never blank the rest of /api/status.
            try:
                whole_house = self._whole_house_summary(
                    import_rate_p = import_rate_p,
                    export_rate_p = export_rate_p,
                )
            except Exception as exc:
                self.logger.debug(f"[WholeHouse] summary failed: {exc}")
                whole_house = None
            economics = {
                "today":           today_econ,
                "yesterday":       yesterday_econ,
                "yesterday_date":  yesterday_date,
                "periods":         periods,
                "calendar_months": calendar_months,
                "whole_house":     whole_house,
            }

            # Publish the EFFECTIVE storm release point, not the raw pref —
            # _apply_storm_override clamps it to the active reserve target
            # (max(pref, STORM_SOC_*)), so a pref below the reserve would show
            # a release percentage the override never honours during a storm.
            storm_level_now   = store.get("storm_level", "none")
            storm_release_now = self._storm_export_release_pct()
            if storm_level_now in ("amber", "red"):
                storm_release_now = max(storm_release_now, STORM_SOC_AMBER)
            elif storm_level_now == "yellow":
                storm_release_now = max(storm_release_now, STORM_SOC_YELLOW)

            return {
                "timestamp":  datetime.now().strftime("%H:%M:%S"),
                "battery": {
                    "soc_pct":  round(soc, 1),
                    "power_w":  bat_w,
                    # Published so dashboards can turn SOC into kWh without
                    # hardcoding this system's pack size — energy.html carried
                    # its own 35.04 literal, which is wrong for anyone else
                    # running the plugin.
                    "capacity_kwh": BATTERY_CAPACITY_KWH,
                },
                "solar": {
                    "power_w":        pv_w,
                    "today_kwh":      round(fcast.get("correctedTodayKwh",     0.0), 1),
                    "tomorrow_kwh":   round(fcast.get("correctedTomorrowKwh",  0.0), 1),
                    "bias_factor":    round(fcast.get("biasFactor",            1.0), 3),
                    "remaining_kwh":  round(fcast.get("remainingTodayKwh",     0.0), 1),
                    "tomorrow_surplus_kwh":  round(tomorrow_surplus, 1),
                    "tomorrow_revenue_gbp":  tomorrow_revenue_gbp,
                    "export_rate_p":         round(export_rate_p, 2),
                    "actual_today_kwh":      round(store.get("pv_daily_kwh", 0.0), 2),
                    "peak_w":                store.get("peak_pv_w", 0),
                    "peak_time":             store.get("peak_pv_time", ""),
                    "lifetime_kwh":          inv.get("pvLifetimeKwh"),
                    "total_kwp":             self._total_kwp(),
                },
                "grid": {
                    "power_w": grid_w,
                    "status":  inv.get("gridStatus", "On-grid"),
                },
                "home": {
                    "load_w": home_w,
                },
                "decision": {
                    "action":        dec.action         if dec else "unknown",
                    "reason":        dec.reason         if dec else "",
                    "dawn_viable":   dec.dawn_viable     if dec else True,
                    "soc_at_dawn_kwh": round(dec.soc_at_dawn_kwh if dec else 0.0, 1),
                },
                "tariff": {
                    "name":         tariff_info.get("display_name", "Unknown"),
                    "product_code": tariff_info.get("product_code", ""),
                    "today_p":      tracker.get("today_p"),
                    "tomorrow_p":   tracker.get("tomorrow_p"),
                },
                "today_summary": {
                    "pv_kwh":     round(store.get("pv_daily_kwh",          0.0), 2),
                    "import_kwh": round(import_kwh,                                   2),
                    "export_kwh": round(store.get("grid_export_daily_kwh", 0.0), 2),
                    "home_kwh":   round(home_kwh,                                     2),
                    "peak_soc":   round(store.get("peak_soc",            0.0), 1),
                    "min_soc":    round(store.get("min_soc",           100.0), 1),
                    "self_suff":  self_suff,
                },
                "vpp": {
                    "state":     store.get("vpp_state",  "idle"),
                    "active":    store.get("vpp_active", False),
                    # Was hardcoded "" from the day this block was written, so
                    # every consumer that appends it produced a dangling
                    # "VPP event announced:" with the one useful fact — WHEN —
                    # missing. Live-spotted 30-07-2026 on the phone.
                    "event_str": self._vpp_event_str(),
                },
                "storm": {
                    "level": storm_level_now,
                    "export_suppressed": store.get("storm_export_suppressed", False),
                    "export_release_pct": storm_release_now,
                },
                "power_cut": {
                    "events":  (store.get("power_cut_events", []) or [])[-10:],
                    "ongoing": store.get("power_cut_started_at") is not None,
                    "lockout_active": store.get("power_cut_lockout_active", False),
                    "export_suppressed": store.get("power_cut_export_suppressed", False),
                    "lockout_soc_floor": self._power_cut_lockout_soc_floor(),
                    "lockout_min_soc": self._power_cut_lockout_min_soc(),
                    # True when export is running mid-lockout because today's solar
                    # refills the reserve on its own — lets the Lockout chip explain
                    # itself rather than looking like a bug.
                    "solar_release_active": store.get("power_cut_solar_release", False),
                },
                "forecast_accuracy": (
                    self.forecast.get_accuracy_summary(window_days=7)
                    if self.forecast else
                    {"days": 0, "mape_pct": 0.0, "mean_factor": 1.0,
                     "over_count": 0, "under_count": 0}
                ),
                "economics": economics,
                "flags": {
                    "export_active":         store.get("export_active",         False),
                    "solar_overflow_active": store.get("solar_overflow_active", False),
                    "import_active":         store.get("import_active",         False),
                    # Live connection state — latest_inverter_data is kept at
                    # last-known-good on failure, so bool(inv) could never go
                    # false again after the first successful poll.
                    "modbus_connected":      bool(self.modbus and self.modbus.connected),
                },
                "hourly_forecast": hourly,
            }
        except Exception as exc:
            return {"error": str(exc), "timestamp": datetime.now().strftime("%H:%M:%S")}

    @staticmethod
    def _compute_daily_economics(home_kwh, import_kwh, export_kwh,
                                 import_rate_p, export_rate_p):
        """Return the economics dict for a single day's energy figures.

        All four kWh values are non-negative floats.  import_rate_p and
        export_rate_p are pence per kWh (positive).  Either rate being None
        produces a None-result dict so the caller can render "—".
        """
        if import_rate_p is None or export_rate_p is None:
            return {
                "import_rate_p":      import_rate_p,
                "export_rate_p":      export_rate_p,
                "import_cost_gbp":    None,
                "export_revenue_gbp": None,
                "no_solar_cost_gbp":  None,
                "net_today_gbp":      None,
                "solar_benefit_gbp":  None,
            }
        import_cost_p   = import_kwh * import_rate_p
        export_rev_p    = export_kwh * export_rate_p
        no_solar_cost_p = home_kwh   * import_rate_p
        net_p           = export_rev_p - import_cost_p
        benefit_p       = no_solar_cost_p - import_cost_p + export_rev_p
        def _gbp(p):
            return round(p / 100.0, 2)
        return {
            "import_rate_p":      round(float(import_rate_p), 2),
            "export_rate_p":      round(float(export_rate_p), 2),
            "import_cost_gbp":    _gbp(import_cost_p),
            "export_revenue_gbp": _gbp(export_rev_p),
            "no_solar_cost_gbp":  _gbp(no_solar_cost_p),
            "net_today_gbp":      _gbp(net_p),
            "solar_benefit_gbp":  _gbp(benefit_p),
        }

    def _settle_whole_house_costs(self):
        """Backfill settled whole-house cost fields into daily_history.json rows.

        For each recent day not yet cost-settled, fetch Octopus settled grid-import
        and gas consumption, value them at the rate in force on that day (the saved
        rate_today_p for elec unit, and the standing + gas rates saved on the day
        in daily_history, falling back to the current Kraken ledger only for older
        rows), compute the whole-house bill, and freeze the row.  Gas is the
        gating signal — a day only settles once Octopus has its gas data.  Export
        revenue uses the Sigen-measured daily export (final at midnight, ~0.02%
        accurate) so it needs no settlement wait.
        """
        if not self.octopus:
            return
        path = os.path.join(self.data_dir, "daily_history.json")
        try:
            with open(path, "r", encoding="utf-8") as f:
                records = json.load(f)
        except (OSError, ValueError):
            return
        if not records:
            return

        try:
            fin = self.octopus.get_account_financials()
        except Exception as exc:
            self.logger.debug(f"[CostSettle] financials fetch failed: {exc}")
            return
        has_gas_meter = bool(self.octopus.gas_mprn and self.octopus.gas_serial)
        if not fin or not fin.get("elec") or (has_gas_meter and not fin.get("gas")):
            self.logger.debug("[CostSettle] No ledger rates yet — skipping this cycle")
            return
        elec_standing_p = fin["elec"].get("standing_p")
        fin_elec_unit_p = fin["elec"].get("unit_p")
        gas_unit_p      = (fin.get("gas") or {}).get("unit_p")     if has_gas_meter else 0.0
        gas_standing_p  = (fin.get("gas") or {}).get("standing_p") if has_gas_meter else 0.0
        fin_export_p    = (fin.get("export") or {}).get("unit_p")
        # Electricity rates are always required; gas rates only when a gas meter exists.
        if elec_standing_p is None or (has_gas_meter and None in (gas_unit_p, gas_standing_p)):
            self.logger.debug("[CostSettle] Ledger missing standing/gas rates — skipping")
            return

        by_date = {r.get("date"): r for r in records if r.get("date")}
        today   = _london_today()

        settled_n = 0
        for offset in range(1, self.COST_SETTLE_WINDOW_DAYS + 1):
            date_str = (today - timedelta(days=offset)).strftime("%Y-%m-%d")
            rec = by_date.get(date_str)
            if rec is None or rec.get("cost_settled"):
                continue
            try:
                imp = self.octopus.get_import_kwh_for_date(date_str)
                gas = self.octopus.get_gas_kwh_for_date(date_str)
            except Exception as exc:
                self.logger.debug(f"[CostSettle] consumption fetch error {date_str}: {exc}")
                continue
            # Import (electricity) must be present and is the DAY-COMPLETENESS signal:
            # a smart electricity meter always reports half-hourly, so 46+ of 48 slots
            # means the day is whole. Octopus settles ~a day in arrears, so the most
            # recent day often has only the first hour or two — freezing that would lock
            # in a near-zero bill permanently (cost_settled). Wait until the day is whole.
            if not imp or imp.get("kwh") is None:
                continue
            if imp.get("slots", 0) < self.COST_SETTLE_MIN_SLOTS:
                continue

            # Gas: gate on FULL-DAY COVERAGE, not a 46-slot half-hourly count.
            # Many SMETS1 / daily-read gas meters report ONE reading per day (slots=1),
            # which can never reach 46 — gating on slots strands those users on an
            # estimate forever. But presence alone is NOT enough either: gas settles
            # slower than electricity and can arrive PARTIALLY — on 03-07-2026 the
            # 1 Jul row froze at 0.034 kWh (£0.00) off a single 00:00-00:30 slot.
            # The `complete` flag (readings reach the end of the local day) is true
            # for a whole half-hourly day AND for a daily meter's single 24h reading,
            # but false for a partial day. A user with no gas meter settles on
            # electricity alone.
            if has_gas_meter:
                if not gas or gas.get("kwh") is None:
                    continue   # gas meter configured but not settled yet — wait
                if not gas.get("complete", False):
                    self.logger.debug(
                        f"[CostSettle] gas for {date_str} only partially settled "
                        f"({gas.get('slots', 0)} slot(s), coverage short of day end) — waiting")
                    continue
                try:
                    gas_kwh = float(gas["kwh"])
                except (TypeError, ValueError):
                    self.logger.debug(f"[CostSettle] non-numeric gas kWh for {date_str}; skipping")
                    continue
                gas_m3 = gas.get("m3")
            else:
                gas_kwh = 0.0
                gas_m3  = None

            # Elec unit rate that applied on this day (saved at midnight); fall
            # back to the current ledger rate only if the row never captured one.
            try:
                elec_unit_p = float(rec.get("rate_today_p"))
            except (TypeError, ValueError):
                elec_unit_p = fin_elec_unit_p
            if elec_unit_p is None:
                continue
            try:
                export_rate_p = float(rec.get("export_rate_p"))
            except (TypeError, ValueError):
                export_rate_p = fin_export_p if fin_export_p is not None else DEFAULT_EXPORT_RATE_P

            try:
                import_kwh = float(imp["kwh"])
            except (TypeError, ValueError):
                self.logger.debug(f"[CostSettle] non-numeric import kWh for {date_str}; skipping")
                continue
            try:
                export_kwh = float(rec.get("grid_export_kwh", 0.0))
            except (TypeError, ValueError):
                export_kwh = 0.0

            # Standing charges + gas unit rate: prefer the value saved on the
            # day (tariff-change-proof); fall back to the current ledger only for
            # older / backfilled rows that predate per-day capture.
            def _day_rate(key, fallback):
                v = rec.get(key)
                try:
                    return float(v) if v is not None else fallback
                except (TypeError, ValueError):
                    return fallback
            day_elec_standing_p = _day_rate("elec_standing_p_day", elec_standing_p)
            day_gas_unit_p      = _day_rate("gas_unit_p_day",      gas_unit_p)
            day_gas_standing_p  = _day_rate("gas_standing_p_day",  gas_standing_p)

            elec_unit_cost = import_kwh * elec_unit_p / 100.0
            elec_standing  = day_elec_standing_p / 100.0
            gas_unit_cost  = gas_kwh * day_gas_unit_p / 100.0
            gas_standing   = day_gas_standing_p / 100.0
            bill           = elec_unit_cost + elec_standing + gas_unit_cost + gas_standing
            export_rev     = export_kwh * export_rate_p / 100.0
            net            = export_rev - bill

            rec.update({
                "import_kwh_octo":      round(import_kwh, 3),
                "gas_m3":               round(gas_m3, 3) if gas_m3 is not None else None,
                "gas_kwh":              round(gas_kwh, 3),
                "elec_unit_cost_gbp":   round(elec_unit_cost, 2),
                "elec_standing_gbp":    round(elec_standing, 2),
                "gas_unit_cost_gbp":    round(gas_unit_cost, 2),
                "gas_standing_gbp":     round(gas_standing, 2),
                "whole_house_bill_gbp": round(bill, 2),
                "export_revenue_gbp":   round(export_rev, 2),
                "wh_net_gbp":           round(net, 2),
                "covered":              bool(export_rev >= bill),
                "cost_settled":         True,
            })
            settled_n += 1

        if settled_n:
            try:
                _atomic_write_json(path, records)
                log(f"[CostSettle] Settled whole-house cost for {settled_n} day(s)")
            except Exception as e:
                log(f"[CostSettle] Cannot write daily history: {e}", level="ERROR")

    def _total_kwp(self):
        """Sum of configured PV array peak power (kWp) — for the solar yield figure."""
        try:
            if self.forecast and getattr(self.forecast, "arrays", None):
                return round(sum(float(a.get("kwp", 0)) for a in self.forecast.arrays), 2)
        except Exception:
            pass
        return 0.0

    @staticmethod
    def _wh_card_from_row(rec):
        """Build the whole-house card dict from a cost-settled daily_history row."""
        if not rec or not rec.get("cost_settled"):
            return None
        eu = rec.get("elec_unit_cost_gbp") or 0.0
        es = rec.get("elec_standing_gbp")  or 0.0
        gu = rec.get("gas_unit_cost_gbp")  or 0.0
        gs = rec.get("gas_standing_gbp")   or 0.0
        return {
            "electric_unit_gbp":     round(eu, 2),
            "electric_standing_gbp": round(es, 2),
            "electric_gbp":          round(eu + es, 2),
            "gas_unit_gbp":          round(gu, 2),
            "gas_standing_gbp":      round(gs, 2),
            "gas_gbp":               round(gu + gs, 2),
            "bill_gbp":              rec.get("whole_house_bill_gbp"),
            "export_gbp":            rec.get("export_revenue_gbp"),
            "net_gbp":               rec.get("wh_net_gbp"),
            "covered":               rec.get("covered"),
            "provisional":           False,
            "gas_estimated":         False,
        }

    @staticmethod
    def _wh_build_card(import_kwh, export_kwh, elec_unit_p, export_rate_p,
                       elec_standing_p, gas_kwh, gas_unit_p, gas_standing_p,
                       provisional, gas_estimated):
        """Assemble a whole-house card dict from kWh + rates (None-safe).

        When the electricity unit rate is unknown but import is non-zero the
        bill cannot be known either, so bill/net/covered come back None and the
        page renders "—". Returning 0.00 for the unit cost — as this did — left
        the standing charge as the whole bill, which nearly always beat the
        export revenue and painted a green "Covered" badge over a bill nobody
        had actually worked out.
        """
        rate_missing = (elec_unit_p is None and (import_kwh or 0.0) > 0.0)
        eu = (import_kwh * elec_unit_p / 100.0) if (import_kwh is not None and elec_unit_p is not None) else 0.0
        es = (elec_standing_p / 100.0) if elec_standing_p is not None else 0.0
        gu = (gas_kwh * gas_unit_p / 100.0) if (gas_kwh is not None and gas_unit_p is not None) else 0.0
        gs = (gas_standing_p / 100.0) if gas_standing_p is not None else 0.0
        bill = eu + es + gu + gs
        # `is None` not truthiness — a genuine 0p export rate is a real rate,
        # and the old test silently swapped it for 12p.
        er   = export_rate_p if export_rate_p is not None else DEFAULT_EXPORT_RATE_P
        exp  = (export_kwh or 0.0) * er / 100.0
        return {
            "electric_unit_gbp":     None if rate_missing else round(eu, 2),
            "electric_standing_gbp": round(es, 2),
            "electric_gbp":          None if rate_missing else round(eu + es, 2),
            "gas_unit_gbp":          round(gu, 2),
            "gas_standing_gbp":      round(gs, 2),
            "gas_gbp":               round(gu + gs, 2),
            "bill_gbp":              None if rate_missing else round(bill, 2),
            "export_gbp":            round(exp, 2),
            "net_gbp":               None if rate_missing else round(exp - bill, 2),
            "covered":               None if rate_missing else bool(exp >= bill),
            "rate_missing":          rate_missing,
            "provisional":           provisional,
            "gas_estimated":         gas_estimated,
        }

    def _wh_provisional_from_row(self, rec, elec_standing_p, gas_unit_p,
                                 gas_standing_p, gas_est_kwh, fin_elec_unit_p,
                                 has_gas=True):
        """Provisional card for a not-yet-settled recent day, from the row's
        Sigen-measured import/export (complete at midnight) plus an estimated gas
        figure.  Electric and export are accurate; only gas is an estimate until
        Octopus settles the day, at which point the settled row takes over."""
        def _f(v, d=None):
            try:
                return float(v) if v is not None else d
            except (TypeError, ValueError):
                return d
        imp_kwh       = _f(rec.get("grid_import_kwh"), 0.0)
        exp_kwh       = _f(rec.get("grid_export_kwh"), 0.0)
        elec_unit_p   = _f(rec.get("rate_today_p"), fin_elec_unit_p)
        export_rate_p = _f(rec.get("export_rate_p"), DEFAULT_EXPORT_RATE_P)
        es_p          = _f(rec.get("elec_standing_p_day"), elec_standing_p)
        gu_p          = _f(rec.get("gas_unit_p_day"), gas_unit_p)
        gs_p          = _f(rec.get("gas_standing_p_day"), gas_standing_p)
        return self._wh_build_card(imp_kwh, exp_kwh, elec_unit_p, export_rate_p,
                                   es_p, gas_est_kwh, gu_p, gs_p,
                                   provisional=True,
                                   gas_estimated=(gas_est_kwh is not None and has_gas))

    def _whole_house_summary(self, import_rate_p, export_rate_p):
        """Whole-house cost block for /api/status.

        Returns today (provisional), yesterday (settled where available),
        month-to-date net, days self-funded, account balance and the 30-day
        bill-vs-export series.  Fields are None when data isn't available yet.
        """
        out = {
            "today": None, "yesterday": None, "yesterday_date": "",
            "day_before": None, "day_before_date": "",
            "month": None, "self_funded": None, "balance_gbp": None,
            "series30": [],
        }
        try:
            fin = self.octopus.get_account_financials() if self.octopus else None
        except Exception:
            fin = None

        elec_standing_p = gas_unit_p = gas_standing_p = None
        fin_elec_unit_p = None
        if fin:
            out["balance_gbp"] = fin.get("balance_gbp")
            if fin.get("elec"):
                elec_standing_p = fin["elec"].get("standing_p")
                fin_elec_unit_p = fin["elec"].get("unit_p")
            if fin.get("gas"):
                gas_unit_p     = fin["gas"].get("unit_p")
                gas_standing_p = fin["gas"].get("standing_p")

        # /api/status hits this every ~5s but daily_history.json only changes at
        # midnight / on a settle, so cache the parse keyed by the file's mtime.
        path = os.path.join(self.data_dir, "daily_history.json")
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            mtime = 0
        if (getattr(self, "_wh_hist_mtime", None) == mtime
                and getattr(self, "_wh_hist_cache", None) is not None):
            records = self._wh_hist_cache
        else:
            records = []
            try:
                with open(path, "r", encoding="utf-8") as f:
                    records = json.load(f)
            except (OSError, ValueError):
                records = []
            self._wh_hist_cache = records
            self._wh_hist_mtime = mtime
        by_date  = {r.get("date"): r for r in records if r.get("date")}
        settled  = [r for r in records if r.get("cost_settled")]

        now_local  = _london_now()
        today      = now_local.date()
        month_pref = today.strftime("%Y-%m")

        # ---- Gas estimate: most recent settled gas_kwh within the last 7 days,
        # used for any day whose gas hasn't settled yet (today + recent days). ----
        gas_est_kwh = None
        gas_cutoff = (today - timedelta(days=7)).strftime("%Y-%m-%d")
        for r in sorted(settled, key=lambda x: x.get("date", ""), reverse=True):
            if r.get("date", "") < gas_cutoff:
                break
            if r.get("gas_kwh") is not None:
                try:
                    gas_est_kwh = float(r["gas_kwh"])
                    break
                except (TypeError, ValueError):
                    continue

        # Resolved before _day_card so the yesterday / day-before cards apply the
        # same gas test the today card does — without it an electricity-only user
        # got an "(est)" tag on a £0.00 gas line.
        has_gas = bool(self.octopus and self.octopus.gas_mprn and self.octopus.gas_serial)

        def _day_card(date_str):
            """Settled row if frozen, else a provisional card from the row's
            Sigen-measured import/export (complete at midnight) + estimated gas —
            so yesterday shows accurate electric + export while gas still settles."""
            rec = by_date.get(date_str)
            settled_card = self._wh_card_from_row(rec)
            if settled_card:
                return settled_card
            if rec is None:
                return None
            return self._wh_provisional_from_row(
                rec, elec_standing_p, gas_unit_p, gas_standing_p,
                gas_est_kwh, fin_elec_unit_p, has_gas=has_gas)

        # ---- Yesterday + day before ----
        y_str = (today - timedelta(days=1)).strftime("%Y-%m-%d")
        out["yesterday_date"] = y_str
        out["yesterday"]      = _day_card(y_str)
        d2_str = (today - timedelta(days=2)).strftime("%Y-%m-%d")
        out["day_before_date"] = d2_str
        out["day_before"]      = _day_card(d2_str)

        # ---- Today (provisional, from live Sigen totals) ----
        # Gas rates are only required when a gas meter exists — an electricity-only
        # user still gets a today card (the None-safe card treats gas cost as 0).
        has_gas = bool(self.octopus and self.octopus.gas_mprn and self.octopus.gas_serial)
        elec_unit_p_today = import_rate_p if import_rate_p is not None else fin_elec_unit_p
        if elec_standing_p is not None and (not has_gas or
                (gas_standing_p is not None and gas_unit_p is not None)):
            try:
                imp_kwh = float(self.store.get("grid_import_daily_kwh", 0.0))
                exp_kwh = float(self.store.get("grid_export_daily_kwh", 0.0))
            except (TypeError, ValueError):
                imp_kwh = exp_kwh = 0.0
            out["today"] = self._wh_build_card(
                imp_kwh, exp_kwh, elec_unit_p_today, export_rate_p,
                elec_standing_p, gas_est_kwh, gas_unit_p, gas_standing_p,
                provisional=True, gas_estimated=(gas_est_kwh is not None and has_gas))

        # ---- Month to date (settled rows this month) ----
        m_rows = [r for r in settled if (r.get("month") == month_pref
                                         or str(r.get("date", "")).startswith(month_pref))]
        if m_rows:
            bill_sum = sum(float(r.get("whole_house_bill_gbp") or 0.0) for r in m_rows)
            exp_sum  = sum(float(r.get("export_revenue_gbp")   or 0.0) for r in m_rows)
            covered_days = sum(1 for r in m_rows if r.get("covered"))
            out["month"] = {
                "bill_gbp":   round(bill_sum, 2),
                "export_gbp": round(exp_sum, 2),
                "net_gbp":    round(exp_sum - bill_sum, 2),
                "in_credit":  bool(exp_sum >= bill_sum),
                "days":       len(m_rows),
            }
            out["self_funded"] = {
                "covered_days": covered_days,
                "settled_days": len(m_rows),
            }

        # ---- 30-day bill-vs-export series ----
        recent = sorted(settled, key=lambda x: x.get("date", ""))[-30:]
        out["series30"] = [
            {
                "date":   r.get("date"),
                "bill":   round(float(r.get("whole_house_bill_gbp") or 0.0), 2),
                "export": round(float(r.get("export_revenue_gbp")   or 0.0), 2),
            }
            for r in recent
        ]
        return out

    def _current_elec_standing_p(self):
        """Today's electricity standing charge in pence/day, or None.

        Used as the fallback for history rows written before per-day rate
        capture (elec_standing_p_day), which is 87 of the first 121 rows here.
        Mirrors the _day_rate fallback in _settle_whole_house_costs.
        """
        try:
            fin = self.octopus.get_account_financials() if self.octopus else None
        except Exception:
            return None
        if not fin or not fin.get("elec"):
            return None
        try:
            v = fin["elec"].get("standing_p")
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _row_standing_p(rec, fallback_p):
        """Electricity standing charge in pence for one history row.

        Prefers the value frozen on the day; falls back to the current ledger;
        returns 0.0 when neither is known so a missing rate can only ever
        understate, never invent a charge.
        """
        v = rec.get("elec_standing_p_day")
        try:
            if v is not None:
                return float(v)
        except (TypeError, ValueError):
            pass
        try:
            return float(fallback_p) if fallback_p is not None else 0.0
        except (TypeError, ValueError):
            return 0.0

    def _period_economics_summary(self, export_rate_p, fallback_import_rate_p):
        """Roll up weekly / monthly / yearly economics from daily_history.json.

        Each window returns:
          {"days": N, "import_total_gbp", "export_total_gbp",
           "no_solar_total_gbp", "net_total_gbp", "benefit_total_gbp",
           "benefit_avg_gbp", "no_solar_avg_gbp", "net_avg_gbp",
           "import_avg_gbp", "export_avg_gbp"}

        Each daily record uses its own saved `rate_today_p` (the import rate
        on that day) — Tracker rates change daily so historical days mustn't
        all be valued at today's rate.  Export rate is assumed flat 12p
        across history (Octopus Outgoing 12p has been live since 26-Mar-2026,
        which is when this system started exporting).
        """
        path = os.path.join(self.data_dir, "daily_history.json")
        try:
            with open(path, "r", encoding="utf-8") as f:
                records = json.load(f)
        except (OSError, ValueError):
            records = []
        today = _london_today()

        fallback_standing_p = self._current_elec_standing_p()

        def aggregate(subset):
            if not subset:
                return {
                    "days": 0,
                    "import_total_gbp":   None, "export_total_gbp":   None,
                    "no_solar_total_gbp": None, "net_total_gbp":      None,
                    "benefit_total_gbp":  None,
                    "elec_whole_house_total_gbp": None,
                    "elec_whole_house_avg_gbp":   None,
                    "import_avg_gbp":     None, "export_avg_gbp":     None,
                    "no_solar_avg_gbp":   None, "net_avg_gbp":        None,
                    "benefit_avg_gbp":    None,
                }
            total_imp_p = total_exp_p = 0.0
            total_no_solar_p = total_net_p = total_benefit_p = 0.0
            total_elec_wh_gbp = 0.0   # whole-house electric (unit + standing)
            counted = 0
            for r in subset:
                home  = float(r.get("home_kwh",        0) or 0)
                imp_k = float(r.get("grid_import_kwh", 0) or 0)
                exp_k = float(r.get("grid_export_kwh", 0) or 0)
                try:
                    ir = float(r.get("rate_today_p") or 0)
                except (TypeError, ValueError):
                    ir = 0
                if ir <= 0 and fallback_import_rate_p is not None:
                    ir = fallback_import_rate_p
                if ir <= 0:
                    continue   # skip days with no rate info
                # Prefer per-record export rate (saved from v5.7 onwards);
                # older records fall back to the live/current export rate.
                try:
                    er = float(r.get("export_rate_p") or 0)
                except (TypeError, ValueError):
                    er = 0
                if er <= 0:
                    er = export_rate_p
                # The day's standing charge, needed twice below: a grid-only
                # home pays exactly the same one, and the whole-house electric
                # column claims to include it.
                st_p = self._row_standing_p(r, fallback_standing_p)

                imp_p       = imp_k * ir
                exp_p       = exp_k * er
                no_sol_unit = home  * ir
                net         = exp_p - imp_p
                # Standing cancels out of the benefit — (no_sol + st) - (imp + st)
                # + exp — so it is computed from the unit-only figures and does
                # NOT change when the standing charge is reported below.
                benefit     = no_sol_unit - imp_p + exp_p
                total_imp_p      += imp_p
                total_exp_p      += exp_p
                # Grid-only counterfactual carries the standing charge: the same
                # meter, the same daily charge, just no solar behind it. Without
                # it the column sat ~£0.62/day under the truth while the elec
                # bill beside it included the charge.
                total_no_solar_p += no_sol_unit + st_p
                total_net_p      += net
                total_benefit_p  += benefit
                # Whole-house electric, ALWAYS unit + standing so the column
                # matches its header and the row reads as an identity:
                #   solar benefit = grid-only - elec bill + export earned
                # Settled rows carry frozen bill-exact figures; unsettled rows
                # (Octopus settles ~a day in arrears, so a 7-day window nearly
                # always holds one) previously contributed unit only.
                if r.get("elec_unit_cost_gbp") is not None:
                    total_elec_wh_gbp += (float(r.get("elec_unit_cost_gbp") or 0)
                                          + float(r.get("elec_standing_gbp") or 0))
                else:
                    total_elec_wh_gbp += (imp_p + st_p) / 100.0
                counted += 1
            if counted == 0:
                return aggregate([])   # all rate-less, treat as empty
            def _g(p):  return round(p / 100.0,           2)
            def _ga(p): return round(p / 100.0 / counted, 2)
            return {
                "days":                counted,
                "import_total_gbp":    _g(total_imp_p),
                "export_total_gbp":    _g(total_exp_p),
                "no_solar_total_gbp":  _g(total_no_solar_p),
                "net_total_gbp":       _g(total_net_p),
                "benefit_total_gbp":   _g(total_benefit_p),
                "elec_whole_house_total_gbp": round(total_elec_wh_gbp, 2),
                "elec_whole_house_avg_gbp":   round(total_elec_wh_gbp / counted, 2),
                "import_avg_gbp":      _ga(total_imp_p),
                "export_avg_gbp":      _ga(total_exp_p),
                "no_solar_avg_gbp":    _ga(total_no_solar_p),
                "net_avg_gbp":         _ga(total_net_p),
                "benefit_avg_gbp":     _ga(total_benefit_p),
            }

        # Window selectors
        wk_cutoff   = (today - timedelta(days=7)).isoformat()
        yr_cutoff   = (today - timedelta(days=365)).isoformat()
        month_pref  = today.strftime("%Y-%m")
        week_recs   = [r for r in records if (r.get("date") or "") >= wk_cutoff]
        month_recs  = [r for r in records if (r.get("date") or "").startswith(month_pref)]
        year_recs   = [r for r in records if (r.get("date") or "") >= yr_cutoff]

        return {
            "week":  aggregate(week_recs),
            "month": aggregate(month_recs),
            "year":  aggregate(year_recs),
        }

    def _calendar_months_summary(self, export_rate_p, fallback_import_rate_p,
                                  year=None):
        """Per-month economics for Jan-Dec of `year` (default: current year).

        Returns a list of 12 dicts in calendar order, each containing the
        same shape as _period_economics_summary's aggregate (days, totals,
        averages).  Months with no records report days=0 and None values
        so the dashboard can render "—".
        """
        path = os.path.join(self.data_dir, "daily_history.json")
        try:
            with open(path, "r", encoding="utf-8") as f:
                records = json.load(f)
        except (OSError, ValueError):
            records = []
        now_local = _london_now()
        if year is None:
            year = now_local.year

        # Group records by calendar month string ("YYYY-MM")
        by_month = {f"{year:04d}-{m:02d}": [] for m in range(1, 13)}
        for r in records:
            d = r.get("date") or ""
            m_key = d[:7]
            if m_key in by_month:
                by_month[m_key].append(r)

        fallback_standing_p = self._current_elec_standing_p()

        # Reuse the aggregate maths from the period summary
        def aggregate(subset):
            if not subset:
                return {
                    "days": 0,
                    "import_total_gbp":   None, "export_total_gbp":   None,
                    "no_solar_total_gbp": None, "net_total_gbp":      None,
                    "benefit_total_gbp":  None,
                    "elec_whole_house_total_gbp": None,
                    "elec_whole_house_avg_gbp":   None,
                    "import_avg_gbp":     None, "export_avg_gbp":     None,
                    "no_solar_avg_gbp":   None, "net_avg_gbp":        None,
                    "benefit_avg_gbp":    None,
                }
            total_imp_p = total_exp_p = 0.0
            total_no_solar_p = total_net_p = total_benefit_p = 0.0
            total_elec_wh_gbp = 0.0   # whole-house electric (unit + standing)
            counted = 0
            for r in subset:
                home  = float(r.get("home_kwh",        0) or 0)
                imp_k = float(r.get("grid_import_kwh", 0) or 0)
                exp_k = float(r.get("grid_export_kwh", 0) or 0)
                try:
                    ir = float(r.get("rate_today_p") or 0)
                except (TypeError, ValueError):
                    ir = 0
                if ir <= 0 and fallback_import_rate_p is not None:
                    ir = fallback_import_rate_p
                if ir <= 0:
                    continue
                # Prefer per-record export rate (saved from v5.7 onwards);
                # older records fall back to the live/current export rate —
                # matches _period_economics_summary so history isn't re-valued
                # at today's rate.
                try:
                    er = float(r.get("export_rate_p") or 0)
                except (TypeError, ValueError):
                    er = 0
                if er <= 0:
                    er = export_rate_p
                # Same basis as _period_economics_summary — see the comments
                # there for why standing sits in grid-only and in the elec bill
                # but never in the benefit.
                st_p = self._row_standing_p(r, fallback_standing_p)

                imp_p       = imp_k * ir
                exp_p       = exp_k * er
                no_sol_unit = home  * ir
                net         = exp_p - imp_p
                benefit     = no_sol_unit - imp_p + exp_p
                total_imp_p      += imp_p
                total_exp_p      += exp_p
                total_no_solar_p += no_sol_unit + st_p
                total_net_p      += net
                total_benefit_p  += benefit
                if r.get("elec_unit_cost_gbp") is not None:
                    total_elec_wh_gbp += (float(r.get("elec_unit_cost_gbp") or 0)
                                          + float(r.get("elec_standing_gbp") or 0))
                else:
                    total_elec_wh_gbp += (imp_p + st_p) / 100.0
                counted += 1
            if counted == 0:
                return aggregate([])
            def _g(p):  return round(p / 100.0,           2)
            def _ga(p): return round(p / 100.0 / counted, 2)
            return {
                "days":                counted,
                "import_total_gbp":    _g(total_imp_p),
                "export_total_gbp":    _g(total_exp_p),
                "no_solar_total_gbp":  _g(total_no_solar_p),
                "net_total_gbp":       _g(total_net_p),
                "benefit_total_gbp":   _g(total_benefit_p),
                "elec_whole_house_total_gbp": round(total_elec_wh_gbp, 2),
                "elec_whole_house_avg_gbp":   round(total_elec_wh_gbp / counted, 2),
                "import_avg_gbp":      _ga(total_imp_p),
                "export_avg_gbp":      _ga(total_exp_p),
                "no_solar_avg_gbp":    _ga(total_no_solar_p),
                "net_avg_gbp":         _ga(total_net_p),
                "benefit_avg_gbp":     _ga(total_benefit_p),
            }

        month_names = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                       "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
        result = []
        for m in range(1, 13):
            key = f"{year:04d}-{m:02d}"
            entry = aggregate(by_month[key])
            entry["month_key"] = key
            entry["month_name"] = month_names[m - 1]
            entry["partial"]   = (m == now_local.month and year == now_local.year)
            result.append(entry)
        return {"year": year, "months": result}

    def _yesterday_economics(self, export_rate_p, fallback_import_rate_p):
        """Read yesterday's totals from daily_history.json and compute economics.

        Returns (economics_dict, date_str) or (None-econ-dict, "").
        Uses the rate_today_p recorded in yesterday's history entry; falls
        back to today's live rate if that field is empty (older entries
        pre-rate-recording).
        """
        path = os.path.join(self.data_dir, "daily_history.json")
        if not os.path.exists(path):
            return self._compute_daily_economics(0, 0, 0, None, None), ""
        try:
            with open(path, "r", encoding="utf-8") as f:
                records = json.load(f)
        except (OSError, ValueError):
            return self._compute_daily_economics(0, 0, 0, None, None), ""
        if not records:
            return self._compute_daily_economics(0, 0, 0, None, None), ""
        # Look up YESTERDAY by date, not records[-1] — after a missed-midnight restart
        # (Mac asleep over midnight) the last row may be the day before, mislabelling
        # the "yesterday" card. Fall back to the most recent row if the date is absent.
        today_local = _london_today()
        y_str   = (today_local - timedelta(days=1)).strftime("%Y-%m-%d")
        by_date = {r.get("date"): r for r in records if r.get("date")}
        yest = by_date.get(y_str) or records[-1]
        date_str = yest.get("date", "")
        rate_p   = None
        try:
            r = float(yest.get("rate_today_p") or 0.0)
            if r > 0:
                rate_p = r
        except (TypeError, ValueError):
            pass
        if rate_p is None:
            rate_p = fallback_import_rate_p   # may also be None
        # Use yesterday's own saved export rate if present (v5.7+);
        # fall back to today's live export rate for older records.
        yest_export_p = export_rate_p
        try:
            er = float(yest.get("export_rate_p") or 0.0)
            if er > 0:
                yest_export_p = er
        except (TypeError, ValueError):
            pass
        return (
            self._compute_daily_economics(
                home_kwh      = float(yest.get("home_kwh",       0.0) or 0.0),
                import_kwh    = float(yest.get("grid_import_kwh", 0.0) or 0.0),
                export_kwh    = float(yest.get("grid_export_kwh", 0.0) or 0.0),
                import_rate_p = rate_p,
                export_rate_p = yest_export_p,
            ),
            date_str,
        )

    def get_dashboard_calendar(self, year):
        """Return calendar_months summary for a specific year.

        Backs the /api/calendar?year=YYYY endpoint so the dashboard's year
        selector can fetch historical years on demand instead of dumping
        every year into every /api/status response.
        """
        try:
            yi = int(year)
        except (TypeError, ValueError):
            yi = datetime.now().year
        # Resolve export rate same as in get_dashboard_data
        export_rate_p = DEFAULT_EXPORT_RATE_P
        try:
            rates_export = float((self.latest_rates_data or {}).get("export_rate_p", 0.0))
            if rates_export > 0:
                export_rate_p = rates_export
        except (TypeError, ValueError):
            pass
        # Best-effort current import rate for fallback
        fallback_rate = None
        try:
            t = (self.latest_rates_data or {}).get("tracker", {}) or {}
            r = float(t.get("today_p") or 0.0)
            if r > 0:
                fallback_rate = r
        except (TypeError, ValueError):
            pass
        if fallback_rate is None:
            try:
                if "elec_unit_rate_p" in indigo.variables:
                    r = float(indigo.variables["elec_unit_rate_p"].value or 0.0)
                    if r > 0:
                        fallback_rate = r
            except (TypeError, ValueError, KeyError):
                pass
        return self._calendar_months_summary(
            export_rate_p          = export_rate_p,
            fallback_import_rate_p = fallback_rate,
            year                   = yi,
        )

    def get_dashboard_years(self):
        """Return the sorted list of years that have at least one daily record."""
        path = os.path.join(self.data_dir, "daily_history.json")
        if not os.path.exists(path):
            return {"years": []}
        try:
            with open(path, "r", encoding="utf-8") as f:
                records = json.load(f)
        except (OSError, ValueError):
            return {"years": []}
        years = sorted({(r.get("date") or "")[:4] for r in records
                        if (r.get("date") or "")[:4].isdigit()})
        return {"years": years}

    def get_dashboard_history(self, hours=24):
        """Return half-hourly slots for the last N hours from the SQLite store.

        Used by the dashboard's /api/history endpoint to plot SOC and energy
        flows over time.  Returns a JSON-serialisable dict:
            {"hours": N, "slots": [{"t","soc_start","soc_end","pv_kwh",
                                    "import_kwh","export_kwh","home_kwh",
                                    "action"}, ...]}
        """
        db_path = os.path.join(self.data_dir, "energy_timeseries.db")
        if not os.path.exists(db_path):
            return {"hours": hours, "slots": []}
        cutoff = (datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S")
        con = None
        slots = []
        try:
            con = sqlite3.connect(db_path, timeout=5.0)
            cur = con.execute(
                """SELECT slot_start, slot_end,
                          battery_soc_start_pct, battery_soc_end_pct,
                          pv_kwh, grid_import_kwh, grid_export_kwh, home_kwh,
                          manager_action
                     FROM halfhourly
                    WHERE slot_end >= ?
                 ORDER BY slot_end ASC""",
                (cutoff,),
            )
            for row in cur.fetchall():
                slots.append({
                    "t":          row[1],
                    "soc_start":  row[2],
                    "soc_end":    row[3],
                    "pv_kwh":     row[4],
                    "import_kwh": row[5],
                    "export_kwh": row[6],
                    "home_kwh":   row[7],
                    "action":     row[8] or "",
                })
        except sqlite3.Error as exc:
            self.logger.debug(f"[Dashboard] history query failed: {exc}")
        finally:
            if con is not None:
                try:
                    con.close()
                except sqlite3.Error:
                    pass
        return {"hours": hours, "slots": slots}

    def get_dashboard_daily(self, days=30):
        """Return per-day totals for the last N days from daily_history.json.

        Used by /api/daily for the longer-range bar charts.
        """
        path = os.path.join(self.data_dir, "daily_history.json")
        if not os.path.exists(path):
            return {"days": days, "records": []}
        try:
            with open(path, "r", encoding="utf-8") as f:
                records = json.load(f)
        except (OSError, ValueError):
            return {"days": days, "records": []}
        return {"days": days, "records": records[-days:]}

    # ================================================================
    # Export sync check (v5.19) — Sigenergy vs Octopus settled exports
    # ================================================================
    #
    # Octopus settles export half-hourly readings over ~24-48h, so we only
    # compare days that are at least settle_days (default 3) old. Results
    # are cached on self.store so repeated calls (dashboard polling, action
    # callbacks, etc.) don't hammer the Octopus API.
    EXPORT_SYNC_WINDOW_DAYS    = 7
    EXPORT_SYNC_SETTLE_DAYS    = 3
    COST_SETTLE_WINDOW_DAYS    = 14  # settle whole-house cost for the last N days — wide enough
                                     # that a day whose gas/import settles late (Octopus can lag
                                     # several days, esp. daily-read gas) is not permanently missed
    COST_SETTLE_MIN_SLOTS      = 46  # require a (near-)complete electricity day (48 half-hours)
    EXPORT_SYNC_TOLERANCE_PCT  = 5.0
    EXPORT_SYNC_MIN_DAY_KWH    = 0.5    # below this daily total the % is noise, skip drift check
    EXPORT_SYNC_CACHE_TTL      = 6 * 3600   # 6 h — re-check four times/day at most

    def _compute_export_sync(self, force=False):
        """Compare Sigenergy daily export vs Octopus settled export.

        Window: last EXPORT_SYNC_WINDOW_DAYS settled days
                (skipping the most recent EXPORT_SYNC_SETTLE_DAYS, which
                Octopus may not have published yet).

        Returns a dict:
            {
              "computed_at":  ISO8601 UTC timestamp,
              "window_days":  int,
              "settle_days":  int,
              "tolerance_pct": float,
              "rows": [
                {"date", "sigen_kwh", "octopus_kwh", "diff_kwh",
                 "diff_pct", "status", "slots"}, ...
              ],
              "summary": {
                "days_compared":  int,
                "days_unsettled": int,
                "avg_diff_pct":   float | None,
                "worst": {"date", "diff_pct"} | None,
                "all_within_tolerance": bool,
              },
              "available": bool,    # False if no octopus client or no export MPAN
              "reason":   str,      # populated when available=False
            }
        """
        # Hot cache — skip if recent
        cached = self.store.get("export_sync_cache")
        if not force and cached:
            try:
                age = time.time() - float(cached.get("_cached_at", 0))
                if age < self.EXPORT_SYNC_CACHE_TTL:
                    return cached["data"]
            except (TypeError, ValueError):
                pass

        result = {
            "computed_at":   datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "window_days":   self.EXPORT_SYNC_WINDOW_DAYS,
            "settle_days":   self.EXPORT_SYNC_SETTLE_DAYS,
            "tolerance_pct": self.EXPORT_SYNC_TOLERANCE_PCT,
            "rows":          [],
            "summary":       {
                "days_compared":  0,
                "days_unsettled": 0,
                "avg_diff_pct":   None,
                "worst":          None,
                "all_within_tolerance": True,
            },
            "available":     True,
            "reason":        "",
        }

        if not self.octopus or not self.octopus.api_key:
            result["available"] = False
            result["reason"]    = "Octopus API key not configured"
            return result
        if not self.export_mpan or not self.export_serial:
            result["available"] = False
            result["reason"]    = (
                "Export MPAN/serial not configured — set OCTOPUS_EXPORT_MPAN "
                "and OCTOPUS_EXPORT_SERIAL in IndigoSecrets.py, or fill in "
                "PluginConfig"
            )
            return result

        # Load daily_history once and index by date
        path = os.path.join(self.data_dir, "daily_history.json")
        history_by_date = {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                for rec in json.load(f):
                    d = rec.get("date")
                    if d:
                        history_by_date[d] = rec
        except (OSError, ValueError):
            pass

        # Use Europe/London for "today" (matches _check_midnight)
        today      = _london_now()
        today_date = today.date()

        diffs_for_avg = []
        worst_row     = None

        # Iterate D-(settle_days) back through D-(settle_days+window-1), oldest first
        oldest_offset = self.EXPORT_SYNC_SETTLE_DAYS + self.EXPORT_SYNC_WINDOW_DAYS - 1
        for offset in range(oldest_offset, self.EXPORT_SYNC_SETTLE_DAYS - 1, -1):
            day = today_date - timedelta(days=offset)
            date_str = day.strftime("%Y-%m-%d")

            sigen_rec = history_by_date.get(date_str)
            sigen_kwh = None
            if sigen_rec is not None:
                try:
                    sigen_kwh = float(sigen_rec.get("grid_export_kwh", 0.0))
                except (TypeError, ValueError):
                    sigen_kwh = None

            try:
                octo = self.octopus.get_export_kwh_for_date(
                    date_str, self.export_mpan, self.export_serial
                )
            except Exception as exc:
                self.logger.debug(f"[ExportSync] Octopus fetch error for {date_str}: {exc}")
                octo = None

            row = {
                "date":        date_str,
                "sigen_kwh":   round(sigen_kwh, 2) if sigen_kwh is not None else None,
                "octopus_kwh": None,
                "diff_kwh":    None,
                "diff_pct":    None,
                "slots":       0,
                "status":      "unsettled",
            }

            if octo is None:
                row["status"] = "fetch_error"
            else:
                row["slots"] = int(octo.get("slots", 0))
                if octo.get("kwh") is None:
                    row["status"] = "unsettled"
                else:
                    row["octopus_kwh"] = round(float(octo["kwh"]), 2)
                    if sigen_kwh is not None:
                        diff = sigen_kwh - row["octopus_kwh"]
                        row["diff_kwh"] = round(diff, 2)
                        # Use the LARGER of the two as the denominator —
                        # protects against tiny Sigenergy values inflating %.
                        denom = max(sigen_kwh, row["octopus_kwh"], 0.001)
                        pct = (diff / denom) * 100.0 if denom > 0 else 0.0
                        row["diff_pct"] = round(pct, 1)
                        # Near-zero export days (winter, rain) — a 0.05 kWh
                        # delta on a 0.07 kWh day reads as 71% but is
                        # operationally meaningless. Force-ok and exclude
                        # from the average / worst summary stats.
                        if denom < self.EXPORT_SYNC_MIN_DAY_KWH:
                            row["status"] = "ok"
                        else:
                            row["status"] = (
                                "ok" if abs(pct) <= self.EXPORT_SYNC_TOLERANCE_PCT
                                else "drift"
                            )
                            diffs_for_avg.append(pct)
                            if worst_row is None or abs(pct) > abs(worst_row["diff_pct"]):
                                worst_row = row
                    else:
                        row["status"] = "no_sigen_record"

            result["rows"].append(row)

        # Summary aggregates
        compared    = [r for r in result["rows"] if r["status"] in ("ok", "drift")]
        unsettled   = [r for r in result["rows"] if r["status"] == "unsettled"]
        result["summary"]["days_compared"]  = len(compared)
        result["summary"]["days_unsettled"] = len(unsettled)
        if diffs_for_avg:
            result["summary"]["avg_diff_pct"] = round(
                sum(diffs_for_avg) / len(diffs_for_avg), 2
            )
        if worst_row is not None:
            result["summary"]["worst"] = {
                "date":     worst_row["date"],
                "diff_pct": worst_row["diff_pct"],
            }
        result["summary"]["all_within_tolerance"] = all(
            r["status"] == "ok" for r in compared
        )

        # Cache
        self.store["export_sync_cache"] = {
            "_cached_at": time.time(),
            "data":       result,
        }
        return result

    def _log_export_sync_summary(self):
        """Emit one INFO line summarising the export-sync window.

        Called from _check_midnight (so it runs once a day) and skipped
        silently if the feature isn't available.
        """
        try:
            data = self._compute_export_sync(force=True)
        except Exception as exc:
            self.logger.error(f"[ExportSync] Summary computation failed: {exc}")
            return
        if not data.get("available", False):
            return  # disabled by config — stay quiet
        s = data["summary"]
        if s["days_compared"] == 0:
            return  # nothing to say yet (cold start, no settled history)
        worst_str = ""
        if s["worst"]:
            worst_str = (
                f"  worst: {s['worst']['date']} "
                f"{s['worst']['diff_pct']:+.1f}%"
            )
        avg_str = (
            f"{s['avg_diff_pct']:+.1f}%"
            if s["avg_diff_pct"] is not None else "n/a"
        )
        flag = "" if s["all_within_tolerance"] else "  [DRIFT >5%]"
        log(
            f"[ExportSync] {s['days_compared']}d avg diff {avg_str}"
            f"{worst_str}{flag}"
        )

    def get_dashboard_export_sync(self):
        """Public accessor used by the /api/export-sync dashboard endpoint."""
        try:
            return self._compute_export_sync(force=False)
        except Exception as exc:
            return {"available": False, "reason": f"compute failed: {exc}"}

    def deviceStartComm(self, dev):
        dev.stateListOrDisplayStateIdChanged()
        dev = indigo.devices[dev.id]   # re-fetch: state list changed, local object is stale
        try:
            self._set_device_initial_state(dev)
        except Exception as e:
            self.logger.error(f"deviceStartComm error for {dev.name}: {e}")

    def deviceStopComm(self, dev):
        pass

    @staticmethod
    def didDeviceCommPropertyChange(oldDevice, newDevice):
        """Suppress unnecessary deviceStopComm/deviceStartComm cycles.

        Devices in this plugin are created and managed internally from Modbus
        polling, Open-Meteo forecast updates and snapshot writes; there are no
        user-editable pluginProps that justify a comm restart. Returning False
        prevents Indigo from cycling comm on every internal
        replacePluginPropsOnServer write.
        """
        return False

    def _set_device_initial_state(self, dev):
        """Write placeholder states and state image for a device on startup."""
        type_id = dev.deviceTypeId

        if type_id == "sigenergyInverter":
            # Only the CONNECTION states are seeded. batterySoc and the power
            # states are real charted numbers — writing 0s here injected a
            # phantom 0% SOC sample into SQL history and fired any user
            # SOC-threshold trigger on every restart. The states simply hold
            # their last real values until the first Modbus poll (~5-10s).
            dev.updateStatesOnServer([
                {"key": "modbusConnected",   "value": "False"},
                {"key": "lastUpdate",        "value": "Initialising..."},
            ])
            dev.updateStateImageOnServer(indigo.kStateImageSel.SensorOff)

        elif type_id == "batteryManager":
            dev.updateStatesOnServer([
                {"key": "managerStatus", "value": "Initialising"},
                {"key": "currentAction", "value": "self_consumption"},
                # currentMode (List enum) is intentionally NOT seeded here:
                # the state-list registration kicked off by
                # stateListOrDisplayStateIdChanged() is async and hasn't
                # completed at deviceStartComm time, so an immediate write
                # logs a spurious "state key not defined" ERROR. Indigo
                # registers the enum with its first Option (selfConsumption)
                # as the default, and the first evaluate tick (<=5s) writes
                # the real mode — so nothing is lost.
                {"key": "currentReason", "value": "Starting up"},
                {"key": "dawnViable",    "value": ""},
                {"key": "socAtDawn",     "value": ""},
                {"key": "lastUpdate",    "value": "Initialising..."},
            ])
            dev.updateStateImageOnServer(indigo.kStateImageSel.SensorOn)

        elif type_id == "solarForecast":
            dev.updateStatesOnServer([
                {"key": "todayKwh",       "value": "0.0"},
                {"key": "tomorrowKwh",    "value": "0.0"},
                {"key": "forecastStatus", "value": "Initialising"},
                {"key": "lastUpdate",     "value": "Initialising..."},
            ])
            dev.updateStateImageOnServer(indigo.kStateImageSel.SensorOn)

        elif type_id == "tariffMonitor":
            dev.updateStatesOnServer([
                {"key": "tariffActive", "value": "Initialising"},
                {"key": "rateToday",    "value": ""},
                {"key": "rateTomorrow", "value": ""},
                {"key": "lastUpdate",   "value": "Initialising..."},
            ])
            dev.updateStateImageOnServer(indigo.kStateImageSel.SensorOn)

        elif type_id == "axleVppMonitor":
            dev.updateStatesOnServer([
                {"key": "vppStatus",        "value": "Standby"},
                {"key": "vppState",         "value": "idle"},
                {"key": "vppLastExportKwh", "value": "0.00"},
                {"key": "lastUpdate",       "value": "Initialising..."},
            ])
            dev.updateStateImageOnServer(indigo.kStateImageSel.SensorOn)

    def runConcurrentThread(self):
        """Main 10-second polling loop.

        A failure in any single tick task must not kill the whole polling loop —
        one bad modbus read / forecast parse / VPP poll should be logged and
        retried on the next tick, not take the manager offline until a plugin
        restart. StopThread still propagates so shutdown stays clean. (Since
        v5.45.0 the tick does not hold _state_lock itself — each stage locks
        only its merge section — so no lock is ever held across the retry.)
        """
        try:
            while True:
                now = time.time()
                try:
                    self._tick(now)
                except self.StopThread:
                    raise
                except Exception:
                    self.logger.exception(
                        "[Tick] Unhandled error in poll tick — continuing to next tick"
                    )
                # Sleep the smaller of the 10s base tick and the configured modbus poll
                # interval, so the advertised 5s "very live" setting is actually honoured
                # (per-task interval checks inside _tick gate everything else).
                self.sleep(min(getattr(self, "modbus_poll_s", MODBUS_POLL_INTERVAL), 10))
        except self.StopThread:
            pass

    # ================================================================
    # Main Poll Tick
    # ================================================================

    def _tick(self, now):
        """Called every 10 seconds. Dispatches all timed tasks.

        v5.45.0 LOCKING MODEL: the tick no longer holds _state_lock for its
        whole duration (it used to stall every action callback and dashboard
        request behind a ~16-20s Modbus cycle or a slow HTTP fetch). Instead:
          - network stages (_poll_modbus, _refresh_forecast,
            _refresh_octopus_rates, _poll_vpp, _check_storm_watch,
            _settle_whole_house_costs) run their I/O UNLOCKED and take the
            lock only to merge results into the store;
          - control stages (_evaluate_manager incl. verify+act,
            _check_midnight, _check_scheduled_import) are SELF-LOCKING and run
            entirely under the lock — hardware decisions must stay serialised
            with the locked action callbacks;
          - the last_* stamps below are scalar dict ops with the tick as the
            only regular writer — safe without the lock.
        Contract pinned by test_concurrency.py.
        """
        # Date rotation only needs checking every ~hour (Phase 4C).  Skipping
        # the per-tick filesystem stat avoids 8,640 redundant calls/day.
        if now - self.store.get("last_log_check", 0.0) >= 3600.0:
            _ensure_plugin_log(self.data_dir)
            self.store["last_log_check"] = now

        # 1. Modbus poll. Stamp BEFORE the call: the outage back-off inside
        # _apply_modbus_result pushes last_modbus into the future, and
        # stamping afterwards would clobber it (which is exactly what made
        # the v5.43.0 back-off silently ineffective — caught in the v5.45.0
        # locking review, pinned by test_outage_backoff_survives_the_tick).
        if now - self.store["last_modbus"] >= getattr(self, "modbus_poll_s", MODBUS_POLL_INTERVAL):
            self.store["last_modbus"] = now
            self._poll_modbus()

        # 2. Solar forecast (before manager so decision always has fresh data)
        if now - self.store["last_forecast"] >= FORECAST_FETCH_INTERVAL:
            self._refresh_forecast()
            self.store["last_forecast"] = now

        # 3. Battery manager evaluation (every 60s — matches Modbus poll frequency)
        if now - self.store["last_manager"] >= MANAGER_EVAL_INTERVAL:
            self._evaluate_manager()
            self.store["last_manager"] = now

        # 4. Octopus rates
        if now - self.store["last_octopus"] >= OCTOPUS_RATES_INTERVAL:
            self._refresh_octopus_rates()
            self.store["last_octopus"] = now

        # 5. Consumption profile (daily)
        if now - self.store["last_profile"] >= OCTOPUS_PROFILE_INTERVAL:
            self._refresh_consumption_profile()
            self.store["last_profile"] = now

        # 5b. Whole-house cost settle (every 6h — backfill settled gas/import/cost)
        if now - self.store.get("last_cost_settle", 0.0) >= COST_SETTLE_INTERVAL:
            self._settle_whole_house_costs()
            self.store["last_cost_settle"] = now

        # 6. VPP polling (adaptive)
        vpp_interval = self._vpp_poll_interval()
        if now - self.store["last_vpp"] >= vpp_interval:
            self._poll_vpp()
            self.store["last_vpp"] = now

        # 7. Accumulator save
        if now - self.store["last_acc_save"] >= ACCUMULATOR_SAVE_INTERVAL:
            self._save_accumulators()
            self.store["last_acc_save"] = now

        # 8. Daily midnight tasks
        self._check_midnight()

        # 9. Check scheduled import
        self._check_scheduled_import()

        # 10. Storm watch (every 2 hours)
        if now - self.store["last_storm_watch"] >= STORM_WATCH_INTERVAL:
            self._check_storm_watch()
            self.store["last_storm_watch"] = now

        # 11. Write energy summary to Indigo variables + SQLite (every 30 min)
        if now - self.store["last_energy_var"] >= ENERGY_VAR_INTERVAL:
            self._log_halfhourly_to_db()
            self._write_energy_summary_variables()
            self.store["last_energy_var"] = now

    # ================================================================
    # Modbus Polling
    # ================================================================

    def _poll_modbus(self):
        """Read all inverter registers and update sigenergyInverter device states.

        v5.45.0: the throttled read cycle (~16-20s) runs WITHOUT _state_lock —
        only the merge of the result takes it. A locked callback commanding a
        write mid-cycle interleaves safely (the modbus client serialises each
        transaction internally).
        """
        if not self.modbus:
            return

        data = self.modbus.read_all()   # NETWORK — unlocked
        with self._state_lock:
            self._apply_modbus_result(data)

    def _apply_modbus_result(self, data):
        """Merge a read_all() result into store/devices. Caller holds the lock."""
        if data is None:
            # Track consecutive failures: one WARNING at the transition (the
            # modbus layer's per-cycle lines cover detail), then widen the
            # effective poll gap so a long outage doesn't re-run a failed
            # cycle every few seconds (60s after 3 fails, 300s after 10).
            fails = self.store.get("modbus_consecutive_failures", 0) + 1
            self.store["modbus_consecutive_failures"] = fails
            if fails == 1:
                log("[Modbus] Inverter poll failed — holding last-known-good "
                    "snapshot, backing off while offline", level="WARNING")
            if fails >= 10:
                backoff = 300
            elif fails >= 3:
                backoff = 60
            else:
                backoff = 0
            if backoff:
                self.store["last_modbus"] = (
                    time.time() + backoff
                    - getattr(self, "modbus_poll_s", MODBUS_POLL_INTERVAL))
            self._update_inverter_device_offline()
            return

        if self.store.get("modbus_consecutive_failures", 0) > 0:
            log(f"[Modbus] Inverter poll recovered after "
                f"{self.store['modbus_consecutive_failures']} failed attempt(s)")
        self.store["modbus_consecutive_failures"] = 0
        data["_read_at"] = time.time()   # staleness stamp for _evaluate_manager
        self.latest_inverter_data = data

        # Detect grid-status transitions in either direction so the power-cut
        # log captures both the start and end of every outage.  Key the edges on a
        # GENUINE off-grid status (register 30009 = 1/2 → "Off-grid …") and on an
        # in-progress-outage flag, NOT on "any value that isn't On-grid": a transient
        # unmapped read ("Unknown (N)") must not fire a false outage notification, and
        # an Unknown→On-grid blip must not fire a false "restored".
        new_grid_status  = data.get("gridStatus", "On-grid")
        in_outage        = self.store.get("power_cut_started_at") is not None
        is_off_grid      = new_grid_status.startswith("Off-grid")
        now_utc          = datetime.now(timezone.utc)
        if is_off_grid and not in_outage:
            # Outage started
            self.store["power_cut_started_at"] = now_utc
            event_str = (
                f"{now_utc.strftime('%Y-%m-%d %H:%M')} UTC — grid LOST "
                f"({new_grid_status})"
            )
            self.store["power_cut_events"].append(event_str)
            # Cap log at 100 entries to bound memory
            if len(self.store["power_cut_events"]) > 100:
                self.store["power_cut_events"] = self.store["power_cut_events"][-100:]
            log(f"[PowerCut] Grid lost — entering {new_grid_status} mode",
                level="WARNING")
            self._save_accumulators()   # crash-safe copy — a restart mid-outage must not re-alert
            self._send_power_cut_notification("lost", detail=new_grid_status)
        elif in_outage and new_grid_status == "On-grid":
            # Outage ended (we were genuinely off-grid and the grid is now back)
            started = self.store.get("power_cut_started_at")
            duration_str = ""
            if started:
                seconds = (now_utc - started).total_seconds()
                if seconds < 120:
                    duration_str = f" (duration {seconds:.0f}s)"
                elif seconds < 3600:
                    duration_str = f" (duration {seconds/60:.1f} min)"
                else:
                    duration_str = f" (duration {seconds/3600:.1f} h)"
            event_str = (
                f"{now_utc.strftime('%Y-%m-%d %H:%M')} UTC — grid RESTORED"
                f"{duration_str}"
            )
            self.store["power_cut_events"].append(event_str)
            if len(self.store["power_cut_events"]) > 100:
                self.store["power_cut_events"] = self.store["power_cut_events"][-100:]
            self.store["power_cut_started_at"]  = None
            self.store["power_restored_time"]   = now_utc
            # Only arm the export lockout if export is actually enabled — otherwise
            # there is nothing to lock out and _resolve_export_lockout would fire a
            # phantom 'lockout cleared' on its next pass.
            export_pref = bool(self.pluginPrefs.get("exportEnabled", False))
            if export_pref:
                self.store["power_cut_lockout_active"] = True
                self.pluginPrefs["powerRestoredTime"]  = now_utc.isoformat()
                self._save_accumulators()   # crash-safe copy — prefs alone only persist on graceful shutdown
                # Name BOTH release rules. Since v5.50.0 the lockout also lifts
                # when the day's own solar can refill the reserve unaided, so a
                # message quoting only the SOC floor makes an early resume look
                # like a fault (live: 26-Jul-2026, export resumed at 74%).
                log(
                    f"[PowerCut] Grid restored after outage — export locked for "
                    f"{POWER_CUT_LOCKOUT_HOURS:.0f} hours as precaution (export resumes "
                    f"early if SOC reaches {self._power_cut_lockout_soc_floor():.0f}%, or "
                    f"if today's solar can refill that reserve on its own)",
                    level="WARNING",
                )
                # Record what the solar rule thinks RIGHT NOW, with its figures.
                # On 26-Jul-2026 export sat suppressed for 20 minutes after the
                # restore and nothing in the log said what was being judged, so
                # the only honest answer afterwards was "we cannot tell". One
                # line here makes the next one answerable.
                outlook = self._solar_refill_outlook(
                    _as_float(data.get("batterySoc"), None))
                if outlook:
                    log(f"[PowerCut] Solar-refill outlook at restore: "
                        f"{'RELEASES' if outlook['releases'] else 'holds'} — "
                        f"{outlook['surplus_kwh']:.1f} kWh spare vs "
                        f"{outlook['needed_kwh']:.1f} kWh needed, "
                        f"daytime={outlook['is_daytime']}")
                self._trigger_event("powerCutLockoutStarted")
            else:
                log("[PowerCut] Grid restored after outage (export disabled — "
                    "no export lockout needed)", level="WARNING")
                self._save_accumulators()   # clear the persisted outage marker (export branch saved above)
            self._send_power_cut_notification("restored", detail=duration_str)
        self.store["grid_status_prev"] = new_grid_status

        # Update daily energy accumulators
        self._accumulate_daily_energy(data)

        # Accumulate home load into persistent half-hourly profile
        self._accumulate_home_profile(max(0.0, float(data.get("homePowerWatts", 0))))

        # Update device states
        self._update_inverter_device(data)

    def _accumulate_daily_energy(self, data):
        """Compute daily energy totals from Modbus registers where available.

        Home consumption: register 30092 resets at midnight on the inverter —
        read directly; always accurate regardless of when the plugin started.

        PV / grid import / grid export: only lifetime totals exist in the protocol
        (30088, 30216, 30220).  We snapshot the lifetime value at midnight (or at
        first read after plugin startup) and compute daily = current - snapshot.
        This is accurate for any full day even if the plugin restarts mid-day.

        If a register read fails the fallback is watt-integration (original method).
        """
        interval_h = getattr(self, "modbus_poll_s", MODBUS_POLL_INTERVAL) / 3600.0
        # ^ fallback: hours per poll, used only when the inverter's direct daily
        # registers are unavailable and we fall through to watt-integration.

        # --- Home daily: read directly from 30092 (resets at midnight) ---
        # The inverter's daily counter may reset at a slightly different
        # moment than _check_midnight (its clock could be UTC, BST, or
        # vendor-default), creating a window where a poll captures the
        # post-reset 0 while our store still represents yesterday — and
        # _write_daily_history then snapshots 0 as yesterday's home_kwh.
        # Confirmed bug on 12-May-2026: every prior day in daily_history.json
        # had home_kwh ~= 0 despite real consumption ~15-20 kWh/day.
        #
        # Defence: a sudden DROP in the inverter's daily counter (while our
        # store has accumulated > 2 kWh) is treated as the inverter's reset
        # and ignored.  _check_midnight runs in the same _tick and will reset
        # our store value cleanly when the local date rolls over.
        home_direct = data.get("homeDailyDirectKwh")
        if home_direct is not None:
            current = self.store.get("home_daily_kwh", 0.0)
            if (home_direct + 1.0) < current and current > 2.0:
                self.logger.debug(
                    f"[Energy] Suspected inverter daily reset detected — "
                    f"home_direct={home_direct:.2f} < store={current:.2f}. "
                    f"Holding store value until _check_midnight runs."
                )
                # Hold value; _check_midnight will reset cleanly on date change
            else:
                self.store["home_daily_kwh"] = home_direct
        else:
            self.store["home_daily_kwh"] += (
                max(0, data.get("homePowerWatts", 0)) * interval_h / 1000.0
            )

        # --- PV daily: delta from lifetime total (30088) ---
        pv_lifetime = data.get("pvLifetimeKwh")
        if pv_lifetime is not None:
            if self.store["pv_lifetime_start_kwh"] is None:
                self.store["pv_lifetime_start_kwh"] = pv_lifetime
                self.logger.info(
                    f"[Energy] PV lifetime anchor: {pv_lifetime:.2f} kWh "
                    f"(daily PV starts from this point)"
                )
            self.store["pv_daily_kwh"] = max(
                0.0, pv_lifetime - self.store["pv_lifetime_start_kwh"]
            )
        else:
            self.store["pv_daily_kwh"] += (
                max(0, data.get("pvPowerWatts", 0)) * interval_h / 1000.0
            )

        # --- Grid import daily: delta from lifetime total (30216) ---
        imp_lifetime = data.get("gridImportLifetimeKwh")
        if imp_lifetime is not None:
            if self.store["import_lifetime_start_kwh"] is None:
                self.store["import_lifetime_start_kwh"] = imp_lifetime
            self.store["grid_import_daily_kwh"] = max(
                0.0, imp_lifetime - self.store["import_lifetime_start_kwh"]
            )
        else:
            self.store["grid_import_daily_kwh"] += (
                max(0, data.get("gridPowerWatts", 0)) * interval_h / 1000.0
            )

        # --- Grid export daily: delta from lifetime total (30220) ---
        exp_lifetime = data.get("gridExportLifetimeKwh")
        if exp_lifetime is not None:
            if self.store["export_lifetime_start_kwh"] is None:
                self.store["export_lifetime_start_kwh"] = exp_lifetime
            self.store["grid_export_daily_kwh"] = max(
                0.0, exp_lifetime - self.store["export_lifetime_start_kwh"]
            )
        else:
            self.store["grid_export_daily_kwh"] += (
                max(0, -data.get("gridPowerWatts", 0)) * interval_h / 1000.0
            )

        # --- SOC peak/low tracking ---
        soc = data.get("batterySoc", 0.0)
        if soc > self.store["peak_soc"]:
            self.store["peak_soc"] = soc
        if soc < self.store["min_soc"]:
            self.store["min_soc"] = soc

        # --- Peak PV tracking (max generation today + the time it hit) ---
        try:
            pv_w = int(data.get("pvPowerWatts", 0) or 0)
        except (TypeError, ValueError):
            pv_w = 0
        if pv_w > self.store.get("peak_pv_w", 0):
            self.store["peak_pv_w"]    = pv_w
            self.store["peak_pv_time"] = datetime.now().strftime("%H:%M")

    # ================================================================
    # Manager Evaluation
    # ================================================================

    def _evaluate_manager(self):
        # v5.45.0: the ENTIRE evaluate/verify/act path runs under the lock —
        # a register-verify racing an action callback (e.g. Pause) could
        # otherwise re-assert a stale mode. Control trades a few locked
        # seconds per minute for serialisation; the bulk I/O lives elsewhere.
        with self._state_lock:
            return self._evaluate_manager_impl()

    def _evaluate_manager_impl(self):
        """Run the battery manager decision engine and act on the result.

        Orchestrates:
          1. resolve power-cut lockout state (and toggle lockout-cleared event)
          2. compute VPP energy reserve
          3. build the immutable ManagerSnapshot
          4. apply seasonal + storm overrides
          5. evaluate the manager
          6. log on action change / heartbeat
          7. verify persistent inverter registers
          8. act on the decision
          9. push device state
        """
        if not self.latest_inverter_data:
            return

        # Staleness guard: during a Modbus outage the last-known-good snapshot
        # is deliberately kept for display, but the MANAGER must not act on a
        # frozen SOC for hours — at reconnect the first writes would be based
        # on arbitrarily old data. Hold evaluation once the snapshot exceeds
        # ~3 poll intervals + 60s.
        read_at = self.latest_inverter_data.get("_read_at", 0.0)
        if read_at:
            poll_s  = getattr(self, "modbus_poll_s", MODBUS_POLL_INTERVAL)
            max_age = 3 * poll_s + 60
            age     = time.time() - read_at
            if age > max_age:
                dev = self._find_device("batteryManager")
                if dev and dev.states.get("managerStatus") != "Modbus offline — holding":
                    log(f"[Manager] Inverter data {age:.0f}s stale — holding "
                        f"evaluation until Modbus recovers", level="WARNING")
                    dev.updateStateOnServer("managerStatus",
                                            value="Modbus offline — holding")
                return

        # Manager paused (Pause action or sigen_manager_paused variable): stay
        # completely hands-off the inverter.  Skipping evaluate/verify/act here is
        # what makes pause a real control rather than a cosmetic label.  The
        # inverter was returned to self-consumption on the pause transition (see
        # _set_manager_paused); self-heal the device label so a restart-while-
        # paused shows "Paused".
        if self.store.get("manager_paused", False):
            dev = self._find_device("batteryManager")
            if dev and dev.states.get("managerStatus") != "Paused":
                dev.updateStateOnServer("managerStatus", value="Paused")
            return

        soc_pct = self.latest_inverter_data.get("batterySoc", 0.0)

        # 1. Power cut lockout (SOC- and forecast-aware — see _resolve_export_lockout).
        #    The solar-refill release needs the energy balance, which normally isn't
        #    built until step 3, so inside a lockout window we build a provisional
        #    snapshot first purely to obtain it. Both calls are pure (no side
        #    effects, no disk I/O) and safe to repeat, and a lockout window is a
        #    rare state — outside one, normal ticks pay nothing for this.
        lockout_balance = None
        if self._power_cut_window_active():
            try:
                provisional = self._build_manager_snapshot(
                    soc_pct, bool(self.pluginPrefs.get("exportEnabled", False)), 0.0,
                )
                lockout_balance = self.manager._calculate_24h_balance(provisional)
            except Exception as exc:
                # Never let the release calculation break the lockout itself —
                # balance=None simply falls back to the flat SOC floor.
                log(f"[PowerCut] Could not build balance for the solar-refill "
                    f"release ({exc!r}) — falling back to the SOC floor",
                    level="WARNING")
        export_enabled = self._resolve_export_lockout(soc_pct, lockout_balance)

        # 2. VPP reserve
        vpp_reserved_kwh = self._compute_vpp_reserved_kwh()

        # 3. Build snapshot
        snapshot = self._build_manager_snapshot(
            soc_pct, export_enabled, vpp_reserved_kwh,
        )

        # 4. Seasonal + storm overrides (mutates snapshot)
        self._apply_seasonal_override(snapshot)
        self._apply_storm_override(snapshot, soc_pct)

        # 5. Evaluate
        decision = self.manager.evaluate(snapshot)
        self.latest_decision = decision

        # 6. Log if action changed or heartbeat
        self._log_manager_decision(decision, snapshot, soc_pct)

        # 7. Verify persistent inverter registers haven't drifted before acting
        self._verify_ems_registers()

        # 8. Act
        self._act_on_decision(decision)

        # 9. Push device state
        self._update_manager_device(decision, snapshot)

        # 10. Publish the flood-export gate preview for the openmeteo advisory so it
        #     reports the SAME gate the manager acts on (single source of truth — stops
        #     the advisory re-deriving and drifting; see the 23/24-Jun-2026 case).
        self._publish_flood_preview(snapshot, decision)

    def _publish_flood_preview(self, snapshot, decision):
        """Write sigen_flood_preview.json so openmeteo_battery_optimiser.py can report
        the flood-export gate verbatim instead of re-deriving it.  Best-effort and
        side-effect-free with respect to control — any failure is logged and swallowed
        so it can never disrupt the manager tick.  Published every tick (incl. daytime)
        because the preview is forward-looking: would_fire answers "would the gate drain
        tonight given the current forecast and SOC", which is exactly what the advisory's
        20:00 (still-daylight) run needs."""
        try:
            preview = self.manager.compute_flood_preview(snapshot)
        except Exception as exc:
            self.logger.debug(f"[FloodPreview] compute failed: {exc!r}")
            return
        try:
            today_export_kwh = float(
                self.latest_inverter_data.get("gridDailyExportKwh", 0.0) or 0.0
            )
        except (TypeError, ValueError):
            today_export_kwh = 0.0
        preview["today_export_kwh"] = round(today_export_kwh, 2)
        preview["export_active"]    = bool(getattr(snapshot, "export_active", False))
        preview["decision_action"]  = decision.action
        preview["generated_at"]     = datetime.now(timezone.utc).isoformat()
        preview["plugin_version"]   = self.pluginVersion
        path = ("/Library/Application Support/Perceptive Automation/"
                "Python Scripts/sigen_flood_preview.json")
        tmp = path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(preview, f, indent=2)
            os.replace(tmp, path)   # atomic — readers never see a half-written file
        except OSError as exc:
            self.logger.warning(f"[FloodPreview] Cannot write {path}: {exc}")

    def _power_cut_lockout_soc_floor(self):
        """SOC floor (%) above which export resumes during the post-cut lockout.
        Reads the powerCutLockoutSocFloor pref, guarded, defaulting to the
        POWER_CUT_LOCKOUT_SOC_FLOOR constant."""
        return _as_float(self.pluginPrefs.get("powerCutLockoutSocFloor"),
                         POWER_CUT_LOCKOUT_SOC_FLOOR)

    def _power_cut_lockout_min_soc(self):
        """SOC floor (%) below which the solar-refill release never applies.
        Reads the powerCutLockoutMinSocPct pref, guarded, defaulting to the
        POWER_CUT_LOCKOUT_MIN_SOC_PCT constant."""
        return _as_float(self.pluginPrefs.get("powerCutLockoutMinSocPct"),
                         POWER_CUT_LOCKOUT_MIN_SOC_PCT)

    def _storm_export_release_pct(self):
        """SOC (%) at/above which the storm override stops suppressing export.
        Reads the stormExportReleasePct pref, guarded, defaulting to the
        STORM_EXPORT_RELEASE_PCT constant. Never allowed below the active storm
        reserve target — the caller clamps with max(release, override_soc)."""
        return _as_float(self.pluginPrefs.get("stormExportReleasePct"),
                         STORM_EXPORT_RELEASE_PCT)

    def _power_cut_window_active(self):
        """True while inside the POWER_CUT_LOCKOUT_HOURS window after a restore.

        The window is purely time-based (decoupled from the export pref) so the
        cleared-event fires on real expiry, not immediately when export happens to
        be disabled.

        Defensive parsing: a hand-edited or corrupt pluginPrefs value (naive ISO,
        garbage string, or wrong type) must NEVER fail-open and let export resume
        during the lockout window. On parse failure we clear the bad value (so it
        doesn't block forever) and resume normal operation.

        Extracted from _resolve_export_lockout in v5.50.0 so _evaluate_manager_impl
        can cheaply ask "are we in a lockout?" before deciding whether the extra
        energy-balance calculation for the solar-refill release is worth doing.
        """
        prt_str = self.pluginPrefs.get("powerRestoredTime", "")
        if not prt_str:
            return False
        try:
            power_restored = datetime.fromisoformat(prt_str)
            if power_restored.tzinfo is None:
                power_restored = power_restored.replace(tzinfo=timezone.utc)
            hours_since = (datetime.now(timezone.utc) - power_restored).total_seconds() / 3600.0
            return hours_since < POWER_CUT_LOCKOUT_HOURS
        except (ValueError, TypeError, AttributeError) as exc:
            log(
                f"[PowerCut] Bad powerRestoredTime in prefs ({exc!r}) — "
                f"clearing and resuming normal operation",
                level="WARNING",
            )
            self.pluginPrefs["powerRestoredTime"] = ""
            return False

    def _resolve_export_lockout(self, soc_pct=None, balance=None):
        """Apply the post-power-cut export lockout (returns export_enabled bool).

        For POWER_CUT_LOCKOUT_HOURS after the grid is restored, export is held
        off as a precaution — BUT only while SOC is below
        POWER_CUT_LOCKOUT_SOC_FLOOR. At/above the floor export resumes so
        flood-prevention can shed surplus and we don't clip solar by letting a
        near-full battery hit 100%. The `power_cut_lockout_active` store flag
        tracks the time WINDOW (so the cleared-event fires once on expiry, not
        when SOC merely crosses the floor); `power_cut_export_suppressed` tracks
        whether export is actually held off right now.

        v5.50.0 adds a SECOND, strictly-additional release condition: pass the
        current SufficiencyBalance as `balance` and export also resumes once
        today's own remaining solar comfortably refills the battery to the floor
        by itself (see _solar_refill_releases_lockout). Omitting `balance` — as
        every pre-5.50 caller did — reproduces the old behaviour exactly.

        Side effects: updates the store flags and fires the
        `powerCutLockoutCleared` trigger event when the window expires.
        """
        export_pref   = bool(self.pluginPrefs.get("exportEnabled", False))
        within_window = self._power_cut_window_active()

        # SOC floor: within the window, suppress export only while SOC is low AND
        # export is actually enabled (nothing to suppress otherwise).
        soc_val = None
        try:
            if soc_pct is not None:
                soc_val = float(soc_pct)
        except (TypeError, ValueError):
            soc_val = None
        soc_floor  = self._power_cut_lockout_soc_floor()
        locked_out = _export_locked_out(within_window, soc_val, soc_floor)

        # Solar-refill release. Only consulted while the SOC floor would otherwise
        # hold export off, and only when the caller supplied a balance — so it can
        # release EARLIER than the floor but never hold longer.
        solar_release = False
        if locked_out and balance is not None:
            cap_kwh = _as_float(self.pluginPrefs.get("batteryCapacityKwh"), 35.04)
            solar_release = _solar_refill_releases_lockout(
                is_daytime          = bool(getattr(balance, "is_daytime", False)),
                soc_pct             = soc_val,
                min_soc_pct         = self._power_cut_lockout_min_soc(),
                battery_kwh         = float(getattr(balance, "battery_kwh", 0.0)),
                floor_kwh           = soc_floor / 100.0 * cap_kwh,
                remaining_solar_kwh = float(getattr(balance, "remaining_solar_kwh", 0.0)),
                home_to_dusk_kwh    = float(getattr(balance, "remaining_home_to_dusk_kwh", 0.0)),
            )

        suppressed     = export_pref and locked_out and not solar_release
        export_enabled = export_pref and not suppressed
        self.store["power_cut_solar_release"] = bool(within_window and solar_release)

        # One-shot INFO when export first resumes mid-window, naming WHICH rule
        # released it — this line is what explains the behaviour in the event log
        # months later. soc_val is formatted defensively: export_pref=False makes
        # suppressed False regardless of SOC, so this can be reached with an
        # unknown SOC.
        prev_suppressed = bool(self.store.get("power_cut_export_suppressed", False))
        if within_window and prev_suppressed and not suppressed:
            soc_str = f"{soc_val:.0f}%" if soc_val is not None else "unknown"
            if solar_release:
                surplus = (float(getattr(balance, "remaining_solar_kwh", 0.0))
                           - float(getattr(balance, "remaining_home_to_dusk_kwh", 0.0)))
                log(
                    f"[PowerCut] SOC {soc_str} — today's solar refills the "
                    f"{soc_floor:.0f}% reserve on its own ({surplus:.1f} kWh surplus "
                    f"to dusk), so export re-enabled during lockout to protect solar",
                )
            else:
                log(
                    f"[PowerCut] SOC {soc_str} ≥ {soc_floor:.0f}% "
                    f"floor — export re-enabled during lockout to protect solar",
                )
        self.store["power_cut_export_suppressed"] = suppressed

        # Detect lockout-window-cleared transition for the Indigo trigger event.
        # Tied to the WINDOW (not the SOC override) so it fires once on expiry.
        prev_lockout = bool(self.store.get("power_cut_lockout_active", False))
        if prev_lockout and not within_window:
            log("[PowerCut] Export lockout cleared — normal operation resumed")
            self.store["power_cut_lockout_active"] = False
            self._trigger_event("powerCutLockoutCleared")
        else:
            self.store["power_cut_lockout_active"] = within_window

        return export_enabled

    def _compute_vpp_reserved_kwh(self):
        """Pre-compute kWh that must be reserved for an upcoming VPP event."""
        vpp_state = self.store.get("vpp_state", VPP_IDLE)
        vpp_event = self.store.get("vpp_event") or {}
        if vpp_state in (VPP_ANNOUNCED, VPP_PRE_CHARGING) and vpp_event:
            max_export_kw = _as_float(self.pluginPrefs.get("maxExportKw"), 4.0)
            duration_hrs  = vpp_event.get("duration_hrs", 1.0)
            return max_export_kw * duration_hrs / VPP_DISCHARGE_EFFICIENCY
        return 0.0

    def _compute_vpp_export_by_date(self):
        """Return (today_kwh, tomorrow_kwh) — Axle export expected on each local date.

        Used by flood prevention to subtract VPP export from refill-day capacity.
        Future-only: an ACTIVE event's elapsed portion is not counted (SOC already
        reflects it). Pro-rated for events that span local midnight.

        Counts only ANNOUNCED / PRE_CHARGING / ACTIVE states — COOLING_OFF means
        the export is already complete and reflected in SOC.
        """
        vpp_state = self.store.get("vpp_state", VPP_IDLE)
        vpp_event = self.store.get("vpp_event") or {}
        if not vpp_event or vpp_state not in (VPP_ANNOUNCED, VPP_PRE_CHARGING, VPP_ACTIVE):
            return (0.0, 0.0)

        start = vpp_event.get("start_time")
        end   = vpp_event.get("end_time")
        if start is None or end is None:
            return (0.0, 0.0)

        max_export_kw = _as_float(self.pluginPrefs.get("maxExportKw"), 4.0)

        now_local   = _london_now()
        start_local = _to_london(start)   # naive values are returned unchanged
        end_local   = _to_london(end)

        # Future-only: clip to "from now"
        effective_start = max(start_local, now_local)
        if effective_start >= end_local:
            return (0.0, 0.0)

        today_date    = now_local.date()
        tomorrow_date = today_date + timedelta(days=1)

        today_hours    = self._vpp_overlap_hours(effective_start, end_local, today_date)
        tomorrow_hours = self._vpp_overlap_hours(effective_start, end_local, tomorrow_date)

        return (
            round(today_hours    * max_export_kw, 3),
            round(tomorrow_hours * max_export_kw, 3),
        )

    @staticmethod
    def _vpp_overlap_hours(start_dt, end_dt, date_obj):
        """Return overlap hours between [start_dt, end_dt] and local date date_obj.

        start_dt and end_dt are local-time datetimes (tz-aware if pytz available).
        Returns 0.0 if no overlap, otherwise the duration of the overlap in hours.
        """
        day_start = start_dt.replace(
            year=date_obj.year, month=date_obj.month, day=date_obj.day,
            hour=0, minute=0, second=0, microsecond=0,
        )
        day_end = day_start + timedelta(days=1)
        overlap_start = max(start_dt, day_start)
        overlap_end   = min(end_dt,   day_end)
        if overlap_end <= overlap_start:
            return 0.0
        return (overlap_end - overlap_start).total_seconds() / 3600.0

    def _build_manager_snapshot(self, soc_pct, export_enabled, vpp_reserved_kwh):
        """Construct the immutable snapshot passed to manager.evaluate()."""
        prefs = self.pluginPrefs

        # Auto-calibrated daily consumption — once the 48-slot profile has
        # enough real readings, its total daily kWh is a better estimate than
        # the static weekday/weekend prefs (which were originally hand-tuned).
        # Prefs still win when they're set to a non-default value, so the user
        # can override the auto-derived figure if they want.  Detection of
        # "user set a custom value" is approximate — any value differing from
        # the documented default by > 1 kWh is treated as user-intent.
        vpp_today_kwh, vpp_tomorrow_kwh = self._compute_vpp_export_by_date()

        profile      = self.store.get("consumption_profile", []) or []
        live_daily   = sum(profile) if len(profile) == 48 else 0.0
        weekday_pref = _as_float(prefs.get("weekdayKwh"), 22.0)
        weekend_pref = _as_float(prefs.get("weekendKwh"), 30.0)
        if live_daily >= 5.0:    # plausibility floor — ignore wildly low partial profiles
            weekday_user_override = abs(weekday_pref - 22.0) > 1.0
            weekend_user_override = abs(weekend_pref - 30.0) > 1.0
            if not weekday_user_override:
                weekday_pref = round(live_daily, 1)
            if not weekend_user_override:
                # Weekend tends to be ~30% higher in CliveS' data — preserve
                # ratio if the user hasn't customised it.
                weekend_pref = round(live_daily * 1.30, 1)

        return ManagerSnapshot(
            current_soc_pct    = soc_pct,
            capacity_kwh       = _as_float(prefs.get("batteryCapacityKwh"), 35.04),
            efficiency         = _as_float(prefs.get("batteryEfficiency"), 94) / 100.0,
            dawn_target_pct    = _as_float(prefs.get("dawnSocTarget"), 10),    # v4.0: retained for VPP/storm
            health_cutoff_pct  = _as_float(prefs.get("batteryHealthCutoff"), 1),
            export_enabled     = export_enabled,
            max_export_kw      = _as_float(prefs.get("maxExportKw"), 4.0),
            export_rate_p      = _as_float((self.latest_rates_data or {}).get("export_rate_p"), DEFAULT_EXPORT_RATE_P),
            weekday_kwh        = weekday_pref,
            weekend_kwh        = weekend_pref,
            pv_watts                = int(self.latest_inverter_data.get("pvPowerWatts", 0)),
            house_load_watts        = int(self.latest_inverter_data.get("homePowerWatts", 0)),
            export_active           = self.store["export_active"],
            corrected_today_kwh     = float(self.latest_forecast_data.get("correctedTodayKwh", 0.0)),
            corrected_tomorrow_kwh  = float(self.latest_forecast_data.get("correctedTomorrowKwh", 0.0)),
            tariff                  = self._build_tariff_data(),
            forecast_p50            = self.latest_forecast_data.get("_hourly_p50_today", {}),
            dawn_times              = self.latest_forecast_data.get("_dawn_times", {}),
            consumption_profile     = self.store.get("consumption_profile", []),
            now                     = datetime.now(timezone.utc),
            bias_factor                 = float(self.latest_forecast_data.get("biasFactor", 1.0)),
            vpp_active                  = self.store["vpp_active"],
            vpp_reserved_kwh            = vpp_reserved_kwh,
            vpp_today_kwh               = vpp_today_kwh,
            vpp_tomorrow_kwh            = vpp_tomorrow_kwh,
            solar_overflow_active       = self.store["solar_overflow_active"],
            solar_overflow_charge_cap   = self.store["solar_overflow_charge_cap_w"],
            solar_overflow_released_at  = self.store.get("solar_overflow_released_at"),
            solar_overflow_target_pct   = _as_float(prefs.get("solarOverflowTargetSoc"),
                                                    SOLAR_OVERFLOW_TARGET_SOC_PCT),
            solar_overflow_min_end_pct  = _as_float(prefs.get("solarOverflowMinEndSoc"),
                                                    SOLAR_OVERFLOW_MIN_END_SOC_PCT),
            # storm_active is set by _apply_storm_override AFTER the snapshot is built
            # (it already mutates dawn_target_pct/export_enabled there) — see step 4.
            flood_prev_target_soc       = float(self.store.get("flood_prev_target_soc") or 0.0),
        )

    def _apply_seasonal_override(self, snapshot):
        """Raise resilience floor in winter months (Oct–Mar) — longer nights."""
        local_month = _london_now().month

        applied = None
        if local_month in (10, 11, 12, 1, 2, 3):
            winter_buf = _as_float(self.pluginPrefs.get("winterBufferPct"), 20)
            if winter_buf > snapshot.dawn_target_pct:
                snapshot.dawn_target_pct = winter_buf
                applied = winter_buf
        # Log only on a state change — this runs every 60s evaluate, so an
        # unconditional log spammed the event log all winter.
        if applied != self.store.get("seasonal_override_applied"):
            if applied is not None:
                log(f"[Seasonal] Winter buffer active (month {local_month}): "
                    f"resilience floor raised to {applied:.0f}%")
            elif self.store.get("seasonal_override_applied") is not None:
                log("[Seasonal] Winter buffer no longer active — back to summer floor")
            self.store["seasonal_override_applied"] = applied

    def _apply_storm_override(self, snapshot, soc_pct):
        """Raise dawn target and suppress exports during active storm warnings.

        Export is suppressed ONLY while the battery is still filling toward the
        storm reserve (SOC below the release point). At/above the release point
        the reserve is banked, so holding export off achieves nothing for
        resilience and merely rams the battery to 100% (charge takes priority
        over export in self-consumption), clipping all PV above the DNO export
        cap. Releasing lets Solar Overflow resume — throttling the charge and
        pushing surplus to grid so the battery creeps up with headroom rather
        than slamming full and curtailing. Mirrors the post-cut lockout floor.
        """
        storm_level = self.store.get("storm_level", "none")
        if storm_level in ("amber", "red"):
            override_soc = STORM_SOC_AMBER
        elif storm_level == "yellow":
            override_soc = STORM_SOC_YELLOW
        else:
            self.store["storm_override_logged_level"] = None
            self.store["storm_export_suppressed"]     = False
            snapshot.storm_active                     = False
            return

        snapshot.dawn_target_pct = max(snapshot.dawn_target_pct, override_soc)
        # v5.51.0: restore the 100% solar-overflow charge target for the duration. A
        # storm is the one time a genuinely full battery is worth clipping for. The
        # pacing stays lazy even then — if the day's own solar reaches 100% unaided,
        # required_charge stays small and nothing is force-charged out of export, which
        # is CliveS's explicit constraint: don't ram the battery full ahead of a storm
        # when the sun was going to do it anyway.
        snapshot.storm_active = True

        # Release point can never sit below the active reserve target — below the
        # target we genuinely want max charge (overflow off) to fill fast for a cut.
        try:
            soc_val = float(soc_pct)
        except (TypeError, ValueError):
            soc_val = 0.0   # unknown SOC — fail safe: keep export suppressed
        release_pct = max(self._storm_export_release_pct(), override_soc)
        suppress    = soc_val < release_pct
        if suppress:
            snapshot.export_enabled = False

        # Log on storm-level change OR when the release point first lets export
        # resume mid-storm, so the event log explains why export is running while
        # a warning is active. Both are state changes, not a 60s heartbeat.
        prev_suppressed = bool(self.store.get("storm_export_suppressed", False))
        level_changed   = self.store.get("storm_override_logged_level") != storm_level
        if level_changed:
            log(
                f"[Storm] Storm override active (level={storm_level}): dawn target "
                f"raised to {snapshot.dawn_target_pct:.0f}%, export "
                f"{'suppressed' if suppress else f'allowed (SOC {soc_val:.0f}% >= {release_pct:.0f}% release)'}"
            )
            self.store["storm_override_logged_level"] = storm_level
        elif prev_suppressed and not suppress:
            log(
                f"[Storm] SOC {soc_val:.0f}% >= {release_pct:.0f}% release point — "
                f"export re-enabled during storm to protect solar (reserve already banked)"
            )
        self.store["storm_export_suppressed"] = suppress

    def _log_manager_decision(self, decision, snapshot, soc_pct):
        """Log manager decisions only on action change — no periodic heartbeat."""
        last_action    = self.store.get("last_manager_action", "")
        action_changed = decision.action != last_action

        if decision.action == ACTION_SOLAR_OVERFLOW:
            if not action_changed:
                return
            log(
                f"[Manager] SOC={soc_pct:.1f}%  PV={snapshot.pv_watts}W  "
                f"Action=solar_overflow"
            )
            if self.debug:
                for line in decision.reason.split("\n"):
                    indigo.server.log(f"  {line}")
        else:
            if not action_changed:
                return
            log(
                f"[Manager] SOC={soc_pct:.1f}%  PV={snapshot.pv_watts}W  "
                f"Action={decision.action}  {decision.reason}"
            )

        # v5.22.0 — Decision audit trail (plan-object pattern from
        # battery_manager.py v3.5).  Logged on action change so the WHY of the
        # new action is visible without re-running with debug on.  Silently
        # skipped when audit_trail is empty (e.g. unit-test Decisions
        # constructed directly without going through evaluate()).
        audit_trail = getattr(decision, "audit_trail", None) or []
        if audit_trail:
            log("[Manager] === DECISION AUDIT ===")
            tag_width = max((len(t) for t, _ in audit_trail), default=10)
            for tag, msg in audit_trail:
                log(f"[Manager]   [{tag:<{tag_width}}]  {msg}")
            log("[Manager] ======================")

        self.store["last_manager_action"] = decision.action

    # ================================================================
    # Storm Watch
    # ================================================================

    def _resolve_pushover_user(self):
        """Return the Pushover user/group key, preferring IndigoSecrets.py over PluginConfig.

        Returns "" and logs an ERROR (once per call) if neither source is set.
        Callers should treat "" as "skip alert".
        """
        token = PUSHOVER_USER_TOKEN or self.pluginPrefs.get("pushoverUserToken", "")
        if not token:
            log(
                "[Pushover] No user/group key configured. Set PUSHOVER_USER_TOKEN in "
                "IndigoSecrets.py or fill in 'Pushover user/group key' under Plugins -> "
                "Sigenergy Manager -> Configure. Alert skipped.",
                level="ERROR",
            )
        return token

    def _resolve_dashboard_host(self):
        """Resolve the host shown in the dashboard URL log line.

        Order of precedence: DASHBOARD_HOST in IndigoSecrets.py, then dashboardHost
        in PluginConfig, then auto-detect via socket.  Auto-detect is the silent
        default — no warning needed, since the dashboard binds to all interfaces
        regardless and the URL is purely a convenience log line.
        """
        host = DASHBOARD_HOST or self.pluginPrefs.get("dashboardHost", "")
        if host:
            return host
        try:
            import socket
            # UDP connect to a public address (no packets sent) — tells the OS
            # which local interface IP would route outbound traffic.
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                s.connect(("8.8.8.8", 80))
                host = s.getsockname()[0]
            finally:
                s.close()
        except Exception as exc:
            log(f"[Web] Could not auto-detect LAN IP ({exc}); falling back to localhost", level="WARNING")
            host = "localhost"
        return host

    def _is_in_quiet_hours(self):
        """True if the current local time falls within the configured Pushover
        quiet-hours window.  Window may straddle midnight (e.g. 22:00 → 07:00).
        Blank/invalid config returns False so alerts always send."""
        start_str = (self.pluginPrefs.get("pushoverQuietStart") or "").strip()
        end_str   = (self.pluginPrefs.get("pushoverQuietEnd")   or "").strip()
        if not start_str or not end_str:
            return False
        try:
            sh, sm = [int(x) for x in start_str.split(":")]
            eh, em = [int(x) for x in end_str.split(":")]
        except (ValueError, AttributeError):
            return False
        now       = _london_now()
        now_min   = now.hour * 60 + now.minute
        start_min = sh * 60 + sm
        end_min   = eh * 60 + em
        if start_min <= end_min:
            return start_min <= now_min < end_min
        # straddles midnight
        return now_min >= start_min or now_min < end_min

    def _send_pushover(self, title, message, priority="0"):
        """Send a Pushover notification. Called from the main plugin thread.

        Honours the configured sound (pluginPrefs.pushoverSound, default
        'vibrate') and quiet-hours window.  HIGH-priority alerts (priority>=1)
        always send regardless of quiet hours so storm amber/red and VPP
        release failures are never silenced.
        """
        user = self._resolve_pushover_user()
        if not user:
            return
        try:
            priority_int = int(priority)
        except (TypeError, ValueError):
            priority_int = 0
        if priority_int < 1 and self._is_in_quiet_hours():
            log(f"[Pushover] suppressed (quiet hours): {title}")
            return
        sound = self.pluginPrefs.get("pushoverSound", "vibrate") or "vibrate"
        try:
            pushover = indigo.server.getPlugin("io.thechad.indigoplugin.pushover")
            if pushover and pushover.isEnabled():
                pushover.executeAction("send", props={
                    "msgTitle":    title,
                    "msgBody":     message,
                    "msgUser":     user,
                    "msgPriority": str(priority),
                    "msgSound":    sound,
                })
                log(f"[Pushover] sent: {title}")
            else:
                log("[Pushover] plugin not enabled — alert not sent", level="ERROR")
        except Exception as exc:
            log(f"[Pushover] send failed: {exc}", level="ERROR")

    def _resolve_powercut_email(self):
        """Return the power-cut email recipient, preferring IndigoSecrets.py
        (POWERCUT_EMAIL) over the powerCutEmailRecipient PluginConfig field.
        Returns '' when neither is set (email is then skipped)."""
        return (POWERCUT_EMAIL or self.pluginPrefs.get("powerCutEmailRecipient", "") or "").strip()

    def _power_cut_lockout_end_local(self):
        """Local clock time the export lockout expires, or "" if none is armed.

        Read from the SAME pluginPrefs["powerRestoredTime"] the lockout window
        itself uses, so the time quoted in the alert cannot drift from the time
        export actually resumes. The caller arms the lockout (and writes that
        pref) before sending the restore alert, so it is already there.
        """
        prt_str = self.pluginPrefs.get("powerRestoredTime", "")
        if not prt_str:
            return ""
        try:
            restored = datetime.fromisoformat(prt_str)
            if restored.tzinfo is None:
                restored = restored.replace(tzinfo=timezone.utc)
            return _local_time(restored + timedelta(hours=POWER_CUT_LOCKOUT_HOURS))
        except (ValueError, TypeError, AttributeError):
            return ""

    def _solar_refill_outlook(self, soc_pct):
        """Will today's solar refill the lockout reserve on its own? Answer it NOW.

        Returns {releases, surplus_kwh, needed_kwh, is_daytime} or None when the
        question cannot be answered (no SOC, no forecast yet, anything raising).
        None is a real answer here — the caller then falls back to naming both
        rules without claiming to know which will apply.

        Built the SAME way _evaluate_manager_impl builds it inside a lockout
        window — provisional snapshot, then the manager's own 24h balance — so
        the alert and the decision cannot disagree. Both calls are pure and hold
        no lock of their own; _state_lock is an RLock, so re-entering is safe.

        The two figures are worth returning even when the answer is no: they are
        what makes "not yet" checkable rather than a bare refusal, and they are
        the numbers that were missing from the log on 26-Jul-2026 when export sat
        suppressed for 20 minutes with no way to see what the plugin was judging.
        """
        if soc_pct is None:
            return None
        try:
            provisional = self._build_manager_snapshot(
                soc_pct, bool(self.pluginPrefs.get("exportEnabled", False)), 0.0,
            )
            balance = self.manager._calculate_24h_balance(provisional)
        except Exception as exc:
            log(f"[PowerCut] Could not work out the solar-refill outlook ({exc!r})",
                level="WARNING")
            return None

        cap_kwh     = _as_float(self.pluginPrefs.get("batteryCapacityKwh"), BATTERY_CAPACITY_KWH)
        floor_kwh   = self._power_cut_lockout_soc_floor() / 100.0 * cap_kwh
        battery_kwh = float(getattr(balance, "battery_kwh", 0.0))
        is_daytime  = bool(getattr(balance, "is_daytime", False))
        surplus     = max(0.0, float(getattr(balance, "remaining_solar_kwh", 0.0))
                          - float(getattr(balance, "remaining_home_to_dusk_kwh", 0.0)))
        # Quote the margin-inflated figure, because that is the bar actually used.
        needed      = max(0.0, floor_kwh - battery_kwh) * POWER_CUT_LOCKOUT_REFILL_MARGIN

        return {
            "releases": _solar_refill_releases_lockout(
                is_daytime          = is_daytime,
                soc_pct             = soc_pct,
                min_soc_pct         = self._power_cut_lockout_min_soc(),
                battery_kwh         = battery_kwh,
                floor_kwh           = floor_kwh,
                remaining_solar_kwh = float(getattr(balance, "remaining_solar_kwh", 0.0)),
                home_to_dusk_kwh    = float(getattr(balance, "remaining_home_to_dusk_kwh", 0.0)),
            ),
            "surplus_kwh": surplus,
            "needed_kwh":  needed,
            "is_daytime":  is_daytime,
        }

    def _power_cut_status_lines(self, kind):
        """Build the detail paragraphs for a power-cut alert. Returns a list.

        Each paragraph is independent and is simply omitted when the readings
        behind it are missing, so a dropped Modbus register costs one line rather
        than the whole message.
        """
        lines = []
        data  = getattr(self, "latest_inverter_data", None) or {}

        soc      = _as_float(data.get("batterySoc"), None)
        home_w   = _as_float(data.get("homePowerWatts"), None)
        capacity = _as_float(self.pluginPrefs.get("batteryCapacityKwh"), BATTERY_CAPACITY_KWH)
        floor    = _as_float(self.pluginPrefs.get("batteryHealthCutoff"), 1.0)

        # Battery + load + how long that lasts.
        if soc is not None:
            stored = soc / 100.0 * capacity
            bits   = [f"Battery {soc:.0f}% ({stored:.1f} kWh stored)"]
            if home_w is not None:
                bits.append(f"the house is drawing {home_w:.0f} W")
            runtime = _format_runtime(
                _backup_runtime_hours(soc, floor, capacity, home_w))
            if runtime:
                # Say plainly that this is at the load right now — the figure
                # moves the moment anything switches on, and on a sunny day the
                # panels stretch it a long way further.
                if kind == "lost":
                    bits.append(f"enough to carry it for about {runtime} at that load")
                else:
                    bits.append(f"about {runtime} of backup at that load "
                                f"if the power goes again")
            lines.append(", ".join(bits) + ".")

        # What export is doing now the grid is back, and which way the solar rule
        # is currently pointing. Worked out here rather than left as a conditional
        # — the reader wants to know whether it applies to them today.
        if kind != "lost":
            lockout = _lockout_message(
                export_enabled    = bool(self.pluginPrefs.get("exportEnabled", False)),
                lockout_end_local = self._power_cut_lockout_end_local(),
                soc_floor_pct     = self._power_cut_lockout_soc_floor(),
                outlook           = self._solar_refill_outlook(soc),
            )
            if lockout:
                lines.append(lockout)
        return lines

    def _send_power_cut_notification(self, kind, detail=""):
        """Alert on a grid-status transition via Pushover + email.

        kind     — "lost" when mains power drops (house islands onto the battery)
                   or "restored" when it returns.
        detail   — the off-grid mode string on loss (e.g. "Off-grid (auto)"), or
                   the pre-formatted duration suffix on restoration (e.g.
                   " (duration 83s)") as built by the caller.

        Both channels carry the SAME full body: battery level, what the house is
        drawing, how long the battery would carry it, and — on a restore — what
        the export lockout is doing and both ways out of it. These are the two
        messages read on a phone during an outage, so everything needed to judge
        the situation goes in them rather than in a log nobody opens at the time.

        Every figure is optional. A line whose numbers are missing is left out
        rather than printed as a dash or a zero, so a partial Modbus read can
        never dress a guess up as a reading.

        Pushover is sent at normal priority so it honours the configured quiet
        hours. Both sends are best-effort: a failure (most likely when the outage
        also took the broadband down) is logged and swallowed so the poll loop
        never dies — the alert then lands once connectivity returns.
        """
        if not self.pluginPrefs.get("powerCutNotify", True):
            return

        local_now = datetime.now().strftime("%d-%b-%Y %H:%M:%S")
        if kind == "lost":
            title = "Power cut — grid lost"
            head  = (f"Mains power lost at {local_now}. The house is now running "
                     f"on the Sigenergy battery ({detail}).")
        else:
            title = "Power restored"
            head  = f"Mains power restored at {local_now}{detail}."

        paragraphs = [head]
        try:
            paragraphs.extend(self._power_cut_status_lines(kind))
        except Exception as exc:
            # The headline is the part that matters. Never let a missing reading
            # or a bad pref stop the alert going out.
            log(f"[PowerCut] Could not build notification detail: {exc!r}",
                level="WARNING")
        message = "\n\n".join(paragraphs)

        # Pushover — normal priority, so quiet hours are respected.
        try:
            self._send_pushover(title, message, priority="0")
        except Exception as exc:
            log(f"[PowerCut] Pushover notify failed: {exc}", level="WARNING")

        # Email via the Email+ SMTP device (best-effort — needs connectivity).
        recipient = self._resolve_powercut_email()
        if recipient:
            try:
                indigo.server.sendEmailTo(recipient, subject=title, body=message)
                log(f"[PowerCut] Email sent to {recipient}")
            except Exception as exc:
                log(f"[PowerCut] Email to {recipient} failed: {exc}", level="WARNING")
        else:
            log("[PowerCut] No email recipient configured — email step skipped")

    def _check_storm_watch(self):
        """
        Poll the MeteoAlarm CAP feed for incoming wind/storm risk covering the
        configured site.  Updates self.store['storm_level'] and sends a Pushover
        alert when the level escalates.  Sends an all-clear when it drops to 'none'.
        """
        loc_name = self.pluginPrefs.get("siteLocationName") or "your area"
        site_lat = (SITE_LATITUDE if SITE_LATITUDE is not None
                    else _as_float(self.pluginPrefs.get("siteLatitude"), None))
        site_lon = (SITE_LONGITUDE if SITE_LONGITUDE is not None
                    else _as_float(self.pluginPrefs.get("siteLongitude"), None))
        try:
            if site_lat is not None and site_lon is not None:
                new_level, reason = check_storm_level(site_lat, site_lon, loc_name)
            else:
                # No configured coordinates — fall back to storm_watch's defaults.
                new_level, reason = check_storm_level(location_name=loc_name)
        except Exception as exc:
            log(f"[Storm] check_storm_level() raised: {exc}", level="WARNING")
            return

        with self._state_lock:   # v5.45.0: poll unlocked, merge locked
            self._apply_storm_result(new_level, reason)

    def _apply_storm_result(self, new_level, reason):
        """Merge a MeteoAlarm poll result. Caller holds the lock."""
        # Read here, not from the caller. The v5.45.0 locking restructure split
        # this method out of _check_storm_watch and left loc_name behind in it,
        # so the yellow-escalation and all-clear alerts — the two bodies that
        # quote it — raised NameError instead of sending. Worse than the missing
        # alert: storm_alerted_level is only written AFTER a successful send, so
        # a failed all-clear left it stuck at the old level and every later
        # storm at or below that level was then judged "already alerted" and
        # stayed silent. Latent since 02-Jul-2026 — no storm has hit since.
        loc_name      = self.pluginPrefs.get("siteLocationName") or "your area"
        prev_level    = self.store.get("storm_level", "none")
        alerted_level = self.store.get("storm_alerted_level", "none")

        if new_level is None:
            # Poll FAILED (feed unreachable / XML unparseable / schema drift) —
            # that is "unknown", not all-clear. Hold the previous level so a flaky
            # internet connection mid-storm (the likeliest time for one) cannot
            # drop an active storm reserve and send a false "risk has passed"
            # push. After STORM_POLL_FAIL_LIMIT consecutive failures decay to
            # "none" with its own notification — any real warning would have
            # expired by then, and holding a stale reserve forever against a
            # permanently-changed feed would be its own failure mode.
            fails = self.store.get("storm_poll_failures", 0) + 1
            self.store["storm_poll_failures"] = fails
            if prev_level != "none" and fails >= STORM_POLL_FAIL_LIMIT:
                log(
                    f"[Storm] Feed unavailable for {fails} consecutive polls — "
                    f"releasing the held '{prev_level}' storm reserve (any real "
                    f"warning has likely expired). Last error: {reason}",
                    level="WARNING",
                )
                self.store["storm_level"] = "none"
                if alerted_level != "none":
                    self._send_pushover(
                        "Storm Watch - Feed Lost",
                        f"MeteoAlarm has been unreachable for ~24 hours; the held "
                        f"'{prev_level}' storm reserve has been released. Check "
                        f"the Met Office directly if storms are still forecast.",
                        priority="0",
                    )
                    self.store["storm_alerted_level"] = "none"
                    self._save_accumulators()
            else:
                held = (f" — holding level '{prev_level}'"
                        if prev_level != "none" else "")
                log(f"[Storm] Poll failed ({fails} consecutive): {reason}{held}",
                    level="WARNING")
            return

        self.store["storm_poll_failures"] = 0
        self.store["storm_level"] = new_level

        # Log-on-change only — the 2-hourly no-change poll was producing ~24
        # heartbeat lines/day.  The reason line goes through log() (not raw
        # indigo.server.log) so it keeps the timestamp/file-mirror convention.
        if new_level != prev_level:
            log(f"[Storm] Level={new_level}  prev={prev_level}")
            log(f"[Storm]   {reason}")
        elif new_level != "none":
            # Active warning — keep the reason visible on each poll so an
            # in-progress storm still leaves a trail, without the 'none' noise.
            log(f"[Storm] Level={new_level}  {reason}")

        # Severity indices for comparison
        new_idx     = STORM_LEVELS.index(new_level)    if new_level    in STORM_LEVELS else 0
        alerted_idx = STORM_LEVELS.index(alerted_level) if alerted_level in STORM_LEVELS else 0

        # Escalation: new level is more severe than the last alert sent
        if new_idx > alerted_idx:
            if new_level == "yellow":
                title = "Storm Watch - Yellow"
                body  = (
                    f"A yellow-level wind risk is forecast for {loc_name}. "
                    f"Battery held at a {STORM_SOC_YELLOW:.0f}% minimum reserve "
                    f"(no grid charging above that); export held off until the "
                    f"battery is nearly full, as a precaution.\n\n{reason}"
                )
                priority = "0"
            elif new_level == "amber":
                title = "Storm Warning - Amber"
                body  = (
                    f"An amber-level wind/storm warning is active for your area. "
                    f"Battery held at a {STORM_SOC_AMBER:.0f}% minimum reserve "
                    f"(no grid charging above that); export held off until the "
                    f"battery is nearly full, against power cuts.\n\n{reason}"
                )
                priority = "1"   # high priority
            else:  # red
                title = "Storm Warning - RED"
                body  = (
                    f"A RED storm warning is active for your area. Power cut risk "
                    f"is high. Battery held at a {STORM_SOC_AMBER:.0f}% minimum "
                    f"reserve (no grid charging above that); export held off until "
                    f"the battery is nearly full.\n\n{reason}"
                )
                priority = "1"   # high priority
            self._send_pushover(title, body, priority)
            self.store["storm_alerted_level"] = new_level
            self._save_accumulators()   # persist NOW so a restart can't re-send this alert

        # De-escalation: level dropped back to none after an alert was sent
        elif new_level == "none" and alerted_idx > 0:
            self._send_pushover(
                "Storm Watch Cleared",
                f"Storm/wind risk has passed for {loc_name}. "
                "Normal battery management and export schedule resumed.",
                priority="0",
            )
            self.store["storm_alerted_level"] = "none"
            self._save_accumulators()

    def _build_tariff_data(self):
        """Build a TariffData object from the latest Octopus rates."""
        rates       = self.latest_rates_data
        tariff_info = rates.get("tariff_info", {})
        tariff_key  = tariff_info.get("tariff_key", TARIFF_TRACKER)

        tracker  = rates.get(TARIFF_TRACKER, {})
        tou      = rates.get(tariff_key, {})     # cheap window data for Go/Flux/iGo/iFlux

        # today_rate_p: use the active tariff's rate, not always Tracker.
        # Flexible is a flat rate — no tomorrow rate.
        if tariff_key == TARIFF_FLEXIBLE:
            today_rate_p    = rates.get(TARIFF_FLEXIBLE, {}).get("today_p")
            tomorrow_rate_p = None
        else:
            today_rate_p    = tracker.get("today_p")
            tomorrow_rate_p = tracker.get("tomorrow_p")

        return TariffData(
            tariff_key      = tariff_key,
            today_rate_p    = today_rate_p,
            tomorrow_rate_p = tomorrow_rate_p,
            cheap_start     = tou.get("cheap_start"),
            cheap_end       = tou.get("cheap_end"),
            cheap_rate_p    = tou.get("cheap_p"),
            agile_slots     = rates.get("agile_slots", []),
        )

    def _act_on_decision(self, decision):
        """Translate a Decision into Modbus writes."""
        if not self.modbus:
            return

        action      = decision.action
        prev_import = self.store["import_active"]
        prev_export = self.store["export_active"]

        if action == ACTION_START_IMPORT:
            if not prev_import:
                log(f"[Manager] Starting grid import: {decision.reason}")
                power_w = min(decision.power_watts or 10000,
                              int(_as_float(self.pluginPrefs.get("inverterMaxKw"), 10.0) * 1000))
                # Hardware backstop: charge cutoff a little above the software
                # target so a plugin crash / Modbus outage mid-import cannot
                # grid-charge unbounded toward 100% (the export path has the
                # symmetric set_discharge_cutoff floor). +3% headroom keeps the
                # software SOC compare as the primary stop.
                cutoff = min((decision.target_soc_pct or 100.0) + 3.0, 100.0)
                if self.modbus.force_charge(power_w, cutoff_soc=cutoff):
                    self.store["import_active"]     = True
                    self.store["import_target_soc"] = decision.target_soc_pct
                    self.store["export_active"]     = False
                    self.store["had_import_today"]  = True   # daily history flag
                    self._set_import_cutoff(cutoff)
                    self._trigger_event("emergencyImportTriggered")

        elif action == ACTION_STOP_IMPORT:
            if prev_import:
                log("[Manager] Import complete - returning to self-consumption")
                self.modbus.set_self_consumption()
                self._restore_import_cutoff()
                self.store["import_active"] = False

        elif action == ACTION_SCHEDULE_IMPORT:
            # Store the scheduled time - checked in _check_scheduled_import
            self.store["import_scheduled_time"] = decision.scheduled_time
            self.store["import_target_soc"]     = decision.target_soc_pct
            if self.store.get("import_scheduled_logged") != str(decision.scheduled_time):
                log(f"[Manager] Import scheduled: {decision.reason}")
                self.store["import_scheduled_logged"] = str(decision.scheduled_time)

        elif action == ACTION_START_EXPORT:
            # Idempotent: only call night_export if not already exporting
            if not prev_import and not prev_export:
                log(f"[Manager] Starting night export: {decision.reason}")
                inv_max_w = int(_as_float(self.pluginPrefs.get("inverterMaxKw"), 10.0) * 1000)
                # Flood prevention uses DNO export cap (decision.power_watts).
                # Legacy night export falls back to full inverter capacity (inv_max_w).
                export_w = decision.power_watts if decision.power_watts > 0 else inv_max_w
                if self.modbus.night_export(export_w):
                    # Set hardware floor so battery stops automatically at target SOC.
                    # Plugin resets this cutoff on return to self-consumption.
                    if decision.target_soc_pct > 0:
                        self.modbus.set_discharge_cutoff(decision.target_soc_pct)
                        self._set_flood_prev_target(decision.target_soc_pct)
                        log(f"[Manager] Discharge cutoff set to {decision.target_soc_pct:.0f}% "
                            f"(flood prevention floor)")
                        self._trigger_event("floodPreventionStarted")
                    self.store["export_active"]      = True
                    self.store["export_count_today"] = (
                        self.store.get("export_count_today", 0) + 1
                    )
                    self._trigger_event("exportStarted")

        elif action == ACTION_VPP_EXPORT:
            # VPP event window: self-drive the export, re-evaluated each tick by
            # _drive_vpp_export() which picks bank-surplus (mode 0x02 + charge cap,
            # daytime with ample PV) or discharge (0x05/0x06) from live PV vs the
            # export target. The discharge floor was set at pre-charge (next-day
            # reserve) — leave it. Axle settle on the meter so this counts
            # identically; their own dispatch is ignored.
            if not prev_export:
                log(f"[Manager] VPP export — {decision.reason}")
            self._drive_vpp_export()

        elif action == ACTION_STOP_EXPORT:
            if prev_export:
                log("[Manager] Stopping night export - returning to self-consumption")
                self.modbus.set_self_consumption()
                # Clean up flood prevention cutoff if it was active
                flood_target = self.store.get("flood_prev_target_soc")
                if flood_target:
                    health_floor = _as_float(self.pluginPrefs.get("batteryHealthCutoff"), 1)
                    self.modbus.set_discharge_cutoff(health_floor)
                    log(f"[Manager] Discharge cutoff reset to {health_floor:.0f}% (health floor)")
                    self._set_flood_prev_target(None)
                    self._trigger_event("floodPreventionStopped")
                self.store["export_active"] = False
                self._trigger_event("exportStopped")

        elif action == ACTION_SOLAR_OVERFLOW:
            # Daytime charge cap: stay in mode 0x02, reduce HOLD_ESS_MAX_CHARGE.
            # PV keeps generating at full power; surplus flows to grid.
            cap_w     = decision.power_watts
            export_kw = decision.export_kw
            prev_cap  = self.store["solar_overflow_charge_cap_w"]

            if not self.store["solar_overflow_active"]:
                # First entry: ensure self-consumption mode, then apply cap.
                # set_self_consumption() resets charge limit to inv_max_w so we
                # must call set_charge_limit() immediately after.
                log(
                    f"[Manager] Solar overflow starting: target export {export_kw:.2f} kW, "
                    f"charge cap {cap_w}W"
                )
                indigo.server.log("  PV surplus flowing to grid")
                self.modbus.set_self_consumption()
                # If flood prevention was running overnight and dawn broke before
                # the target SOC was reached, reset the discharge cutoff register
                # now so it does not act as a hidden floor during daytime operation.
                flood_target = self.store.get("flood_prev_target_soc")
                if flood_target:
                    health_floor = _as_float(self.pluginPrefs.get("batteryHealthCutoff"), 1)
                    self.modbus.set_discharge_cutoff(health_floor)
                    log(f"[Manager] Discharge cutoff reset to {health_floor:.0f}% "
                        f"(flood prevention interrupted at dawn)")
                    self._set_flood_prev_target(None)
                    self._trigger_event("floodPreventionStopped")
                self.modbus.set_charge_limit(cap_w, quiet=True)
                self.store["solar_overflow_active"]       = True
                self.store["solar_overflow_charge_cap_w"] = cap_w
                self.store["export_active"]               = False
                self.store["import_active"]               = False
            elif abs(prev_cap - cap_w) > 500:
                # Cap has shifted by more than deadband — update inverter register silently.
                # No log here: Indigo shows all indigo.server.log() calls regardless of
                # level= so any per-cap-change line floods the event log. The 15-min
                # heartbeat summary already reflects the current cap in its reason string.
                self.modbus.set_charge_limit(cap_w, quiet=True)
                self.store["solar_overflow_charge_cap_w"] = cap_w
            # else: cap within deadband — idempotent, no Modbus writes

        elif action == ACTION_SELF_CONSUMPTION:
            # Determine if inverter is currently in a non-self-consumption mode.
            # Check store flags first; fall back to actual emsWorkMode from inverter data
            # so a restart (which resets all flags to False) can still recover a stuck mode.
            ems_mode_str   = self.latest_inverter_data.get("emsWorkMode", "")
            inverter_stuck = ems_mode_str in ("Discharge ESS First", "Charge Grid First")

            if prev_import:
                # Only cancel an active import if the target SOC has been reached.
                # Without this guard the manager oscillates: one tick of importing
                # nudges SOC above the viability floor → next evaluate() returns
                # SELF_CONSUMPTION → import cancelled → SOC drops → repeat.
                current_soc = self.latest_inverter_data.get("batterySoc", 0.0)
                target_soc  = self.store.get("import_target_soc", 0.0)
                if current_soc >= target_soc:
                    log(f"[Manager] Import complete ({current_soc:.1f}% >= {target_soc:.0f}%) - returning to self-consumption")
                    self.modbus.set_self_consumption()
                    self._restore_import_cutoff()
                    self.store["import_active"]     = False
                    self.store["import_target_soc"] = 0.0
                else:
                    # self.logger.debug respects Indigo's debug-logging toggle; the
                    # module log() helper passes a string level to indigo.server.log
                    # (which wants a logging int), so "DEBUG" there prints regardless.
                    self.logger.debug(
                        f"[Manager] Holding import - SOC {current_soc:.1f}% / "
                        f"target {target_soc:.0f}%")
            elif prev_export:
                # Flood prevention complete, export disabled, or other export end
                flood_target = self.store.get("flood_prev_target_soc")
                current_soc  = self.latest_inverter_data.get("batterySoc", 0.0)
                if flood_target:
                    log(f"[Manager] Flood prevention complete "
                        f"(SOC {current_soc:.1f}% reached {flood_target:.0f}% target) "
                        f"— returning to self-consumption")
                else:
                    log("[Manager] Export disabled — returning to self-consumption")
                self.modbus.set_self_consumption()
                if flood_target:
                    health_floor = _as_float(self.pluginPrefs.get("batteryHealthCutoff"), 1)
                    self.modbus.set_discharge_cutoff(health_floor)
                    log(f"[Manager] Discharge cutoff reset to {health_floor:.0f}% (health floor)")
                    self._set_flood_prev_target(None)
                    self._trigger_event("floodPreventionStopped")
                self.store["export_active"] = False
            elif self.store.get("solar_overflow_active"):
                # SOC dropped below release threshold — restore full charge rate
                log("[Manager] Solar overflow released — restoring full charge limit")
                self.modbus.set_self_consumption()   # resets charge limit to inv_max_w
                self.store["solar_overflow_active"]       = False
                self.store["solar_overflow_charge_cap_w"] = 0
                # Starts the manager's re-engage dwell (battery_manager v3.10).
                self.store["solar_overflow_released_at"]  = datetime.now(timezone.utc)
            elif inverter_stuck:
                # Inverter is in wrong mode (e.g. stuck in 0x06 after restart cleared store flags)
                log(f"[Manager] Inverter stuck in '{ems_mode_str}' — forcing self-consumption",
                    level="WARNING")
                self.modbus.set_self_consumption()

        # Check if active import has reached target SOC
        if self.store["import_active"]:
            current_soc = self.latest_inverter_data.get("batterySoc", 0.0)
            target_soc  = self.store["import_target_soc"]
            if current_soc >= target_soc:
                log(f"[Manager] Import target SOC {target_soc:.0f}% reached - stopping")
                self.modbus.set_self_consumption()
                self._restore_import_cutoff()
                self.store["import_active"]      = False
                self.store["import_target_soc"]  = 0.0

    def _set_flood_prev_target(self, target_soc_pct):
        """Set or clear flood-prevention target SOC, persisted to pluginPrefs.

        Persistence is critical: if the plugin restarts mid-pre-drain, the
        inverter's HOLD_ESS_DISCHARGE_CUTOFF register is still raised to the
        target. Without a persisted flag, startup() would lose the state and
        _verify_ems_registers() would reset the cutoff to the health floor —
        breaking the in-progress drain. Pass None or 0 to clear.
        """
        if target_soc_pct:
            self.store["flood_prev_target_soc"]    = target_soc_pct
            self.pluginPrefs["floodPrevTargetSoc"] = str(target_soc_pct)
        else:
            self.store["flood_prev_target_soc"]    = None
            self.pluginPrefs["floodPrevTargetSoc"] = ""
        # Crash-safe copy — runtime pluginPrefs writes only persist on a
        # graceful shutdown, and losing this flag mid-drain lets the verify
        # pass lower the raised hardware cutoff (see docstring).
        self._save_accumulators()

    def _set_import_cutoff(self, cutoff_pct):
        """Record (and persist) the hardware charge-cutoff backstop raised for an
        active grid import, so _verify_ems_registers maintains it instead of
        restoring 100%. Pass None (or >= 100) to clear. Mirrors
        _set_flood_prev_target — same pluginPrefs persistence caveats apply.
        """
        if cutoff_pct and float(cutoff_pct) < 100.0:
            self.store["import_charge_cutoff_pct"]    = float(cutoff_pct)
            self.pluginPrefs["importChargeCutoffPct"] = str(cutoff_pct)
        else:
            self.store["import_charge_cutoff_pct"]    = None
            self.pluginPrefs["importChargeCutoffPct"] = ""

    def _restore_import_cutoff(self):
        """Restore the hardware charge cutoff to 100% when a grid import ends.
        Best-effort — _verify_ems_registers self-heals a failed write next cycle."""
        if self.store.get("import_charge_cutoff_pct"):
            if self.modbus and self.modbus.connected:
                self.modbus.set_charge_cutoff(100.0)
            self._set_import_cutoff(None)

    def _verify_ems_registers(self):
        """Read back HOLD_ESS_MAX_DISCHARGE and HOLD_ESS_MAX_CHARGE and correct if wrong.

        These registers persist on the inverter across mode changes. A previous
        force_discharge() or force_charge() call can leave a stale limit that
        caps battery output in self-consumption mode. This runs every manager
        evaluation cycle (~15 min) as a self-healing guard.

        Expected values:
          - export_active: discharge limit = inverter max (night_export uses export limit register,
                           not discharge register, to cap grid flow; battery must be free to supply
                           house load + grid simultaneously)
          - import_active: charge limit = inverter max (full import power), discharge = inverter max
          - otherwise:     both limits = inverter max (unrestricted self-consumption)
        """
        if not self.modbus or not self.modbus.connected:
            return

        inv_max_w = int(_as_float(self.pluginPrefs.get("inverterMaxKw"), 10.0) * 1000)

        # Always expect inverter max — night_export() uses HOLD_GRID_MAX_EXPORT_LIMIT
        # (not the discharge register) to constrain grid flow.
        expected_discharge_w = inv_max_w

        # During solar overflow the charge limit is intentionally reduced.
        # Use the stored cap as the expected value so verify() doesn't fight it.
        if self.store.get("solar_overflow_active"):
            expected_charge_w = self.store.get("solar_overflow_charge_cap_w", inv_max_w)
        else:
            expected_charge_w = inv_max_w

        # --- EMS mode ---
        # Skip during VPP_PRE_CHARGING and VPP_ACTIVE — the VPP state machine and
        # manager own the inverter mode through the window (self-driven export), so
        # the verify loop must not overwrite mode 0x06 with our self-consumption
        # expectation. Normal verification resumes the moment the window ends.
        mode_names = {0x02: "Self Consumption", 0x03: "Charge Grid First",
                      0x05: "Discharge PV First", 0x06: "Discharge ESS First"}
        _vpp_state = self.store.get("vpp_state", VPP_IDLE)
        if _vpp_state not in (VPP_PRE_CHARGING, VPP_ACTIVE):
            # Determine what mode the inverter should be in based on store flags.
            # After a restart all flags are False, so expected_mode = 0x02 (Self Consumption).
            # If the inverter is stuck in 0x06 (Discharge ESS First) from overnight export
            # this will catch and correct it on the next manager tick.
            if self.store.get("export_active"):
                expected_mode = 0x06  # Discharge ESS First
            elif self.store.get("import_active"):
                expected_mode = 0x03  # Charge Grid First
            else:
                expected_mode = 0x02  # Max Self Consumption

            actual_mode = self.modbus.read_ems_mode()
            if actual_mode is not None and actual_mode != expected_mode:
                log(
                    f"[Verify] EMS mode mismatch: inverter={mode_names.get(actual_mode, actual_mode)} "
                    f"expected={mode_names.get(expected_mode, expected_mode)} — correcting",
                    level="WARNING",
                )
                self.modbus.set_remote_ems_mode(expected_mode)
        elif _vpp_state == VPP_ACTIVE and self.store.get("export_active"):
            # During the self-driven window the export mode is written once at
            # VPP_ACTIVE entry and the manager's ACTION_VPP_EXPORT guard does not
            # re-write it. Maintain the chosen mode (0x05 daytime / 0x06 dark)
            # here so a transient drift self-heals. Mode register, PLUS the bank
            # sub-mode's own charge cap (see below) — and nothing else. A stray
            # write of any OTHER limit once caused a brief 2 kW grid import
            # (10-Apr-2026), which is why the general limit block below still
            # skips the whole window.
            expected_mode = self.store.get("vpp_export_mode", 0x06)
            actual_mode   = self.modbus.read_ems_mode()
            if actual_mode is not None and actual_mode != expected_mode:
                log(
                    f"[Verify] VPP export mode drift: inverter={mode_names.get(actual_mode, actual_mode)} "
                    f"expected={mode_names.get(expected_mode, expected_mode)} — re-asserting",
                    level="WARNING",
                )
                self.modbus.set_remote_ems_mode(expected_mode)

            # ONE exception to "mode register only": the bank sub-mode's charge
            # cap. In bank (0x02) the cap IS the export mechanism — it is what
            # stops the inverter soaking the PV surplus into the battery instead
            # of sending it to the grid. If it drifted up to inverter max the
            # export would fall to ~0 and NOTHING above would notice, because the
            # mode register would still read a correct 0x02. _drive_vpp_export
            # only rewrites the cap when the surplus moves by >300 W, so a drift
            # could stand for the rest of the window — a silently unpaid event.
            #
            # This does not reopen the 10-Apr-2026 incident: that was the
            # solar-overflow cap being written back over a VPP window. Here the
            # expected value is the VPP driver's own cap, and the check only runs
            # while bank is the live sub-mode. The 0x05/0x06 limits stay untouched
            # (they are static for the window, and the snapshots record them).
            if self.store.get("vpp_export_submode") == "bank":
                expected_cap_w = self.store.get("vpp_bank_charge_cap_w", -1)
                if expected_cap_w is not None and expected_cap_w >= 0:
                    actual_cap_w = self.modbus.read_charge_limit()
                    if actual_cap_w is not None and abs(actual_cap_w - expected_cap_w) > 300:
                        log(
                            f"[Verify] VPP bank charge-cap drift: inverter={actual_cap_w}W "
                            f"expected={expected_cap_w}W — re-asserting (export would "
                            f"otherwise be banked instead of sold)",
                            level="WARNING",
                        )
                        self.modbus.set_charge_limit(expected_cap_w, quiet=True)

        # --- Discharge limit and charge limit ---
        # Skip during VPP_PRE_CHARGING and VPP_ACTIVE: the self-driven export owns
        # these registers through the window (night_export sets the discharge limit
        # to inverter max). A stray write here once caused a brief 2kW grid import
        # (10-Apr-2026) when the solar_overflow charge cap was written back mid-window.
        if _vpp_state not in (VPP_PRE_CHARGING, VPP_ACTIVE):
            actual_discharge_w = self.modbus.read_discharge_limit()
            if actual_discharge_w is not None:
                if abs(actual_discharge_w - expected_discharge_w) > 200:
                    log(
                        f"[Verify] Discharge limit mismatch: inverter={actual_discharge_w}W "
                        f"expected={expected_discharge_w}W — correcting",
                        level="WARNING",
                    )
                    self.modbus.set_discharge_limit(expected_discharge_w)

            actual_charge_w = self.modbus.read_charge_limit()
            if actual_charge_w is not None:
                if abs(actual_charge_w - expected_charge_w) > 200:
                    log(
                        f"[Verify] Charge limit mismatch: inverter={actual_charge_w}W "
                        f"expected={expected_charge_w}W — correcting",
                        level="WARNING",
                    )
                    self.modbus.set_charge_limit(expected_charge_w)

        # --- Discharge cutoff (health floor, register 40048) ---
        # This register physically stops battery discharge. It is only written by
        # VPP code, so on systems without VPP activity it drifts to whatever the
        # inverter factory default is (typically 5%). Verify every cycle so the
        # hardware floor always matches the plugin's batteryHealthCutoff preference.
        # Skip if VPP has temporarily raised the cutoff — the VPP state machine owns it.
        # Skip if flood prevention has temporarily raised the cutoff — it owns it too.
        if not self.store.get("vpp_cutoff_raised") and not self.store.get("flood_prev_target_soc"):
            expected_cutoff_pct = _as_float(self.pluginPrefs.get("batteryHealthCutoff"), 1.0)
            actual_cutoff_pct   = self.modbus.read_discharge_cutoff()
            if actual_cutoff_pct is not None:
                if abs(actual_cutoff_pct - expected_cutoff_pct) > 0.5:
                    log(
                        f"[Verify] Discharge cutoff mismatch: inverter={actual_cutoff_pct:.1f}% "
                        f"expected={expected_cutoff_pct:.1f}% — correcting",
                        level="WARNING",
                    )
                    self.modbus.set_discharge_cutoff(expected_cutoff_pct)

        # --- Charge cutoff (import backstop, register 40047) ---
        # Raised only while a grid import is active (hardware ceiling in case the
        # plugin dies mid-import); expected 100% at all other times. VPP never
        # writes this register, so no VPP-state gating is needed.
        expected_charge_cutoff = self.store.get("import_charge_cutoff_pct") or 100.0
        actual_charge_cutoff   = self.modbus.read_charge_cutoff()
        if actual_charge_cutoff is not None:
            if abs(actual_charge_cutoff - expected_charge_cutoff) > 0.5:
                log(
                    f"[Verify] Charge cutoff mismatch: inverter={actual_charge_cutoff:.1f}% "
                    f"expected={expected_charge_cutoff:.1f}% — correcting",
                    level="WARNING",
                )
                self.modbus.set_charge_cutoff(expected_charge_cutoff)

    def _check_scheduled_import(self):
        # v5.45.0: commands hardware from store state — runs under the lock.
        with self._state_lock:
            return self._check_scheduled_import_impl()

    def _check_scheduled_import_impl(self):
        """Check if a scheduled import time has arrived."""
        scheduled = self.store.get("import_scheduled_time")
        if scheduled is None:
            return

        now_utc = datetime.now(timezone.utc)
        # Normalise scheduled time to UTC if naive. A naive value here is local
        # wall-clock, so it needs localising (pytz) or stamping (zoneinfo) — the
        # one operation the two libraries spell differently, hence the shared
        # helper. It returns None only if no tz database exists at all; skip the
        # tick loudly rather than start an import at the wrong hour.
        if scheduled.tzinfo is None:
            localised = _london_localise(scheduled)
            if localised is None:
                _warn_no_tzdb()
                return
            scheduled = localised.astimezone(timezone.utc)

        if now_utc >= scheduled:
            log("[Manager] Scheduled import window reached - starting import")
            target_soc = self.store.get("import_target_soc", 12.0)
            cutoff     = min((target_soc or 100.0) + 3.0, 100.0)
            if self.modbus and self.modbus.force_charge(10000, cutoff_soc=cutoff):
                self.store["import_active"]      = True
                self.store["import_target_soc"]  = target_soc
                self.store["import_scheduled_time"] = None
                self.store["had_import_today"]   = True   # daily history flag
                self._set_import_cutoff(cutoff)
                self._trigger_event("emergencyImportTriggered")

    # ================================================================
    # Solar Forecast Refresh
    # ================================================================

    def _refresh_forecast(self, force=False):
        """Fetch updated solar forecast from Open-Meteo."""
        if not self.forecast:
            return

        data = self.forecast.fetch_forecast(force=force)
        self.latest_forecast_data = data

        self._update_forecast_device(data)

        status   = data.get("forecastStatus", "")
        tmrw_kwh = data.get("correctedTomorrowKwh", 0.0)

        if "No data" in status:
            log(f"[Solar] WARNING: forecast unavailable ({status}) — night export condition 3 will block", level="WARNING")
        elif tmrw_kwh == 0.0:
            log(f"[Solar] WARNING: tomorrow forecast is 0.0 kWh (status: {status!r}) — night export condition 3 will block", level="WARNING")
        elif self.debug:
            log(
                f"[Solar] Today: {data.get('correctedTodayKwh', 0):.1f} kWh "
                f"(raw {data.get('todayKwh', 0):.1f}, bias {data.get('biasFactor', 1):.3f}), "
                f"Tomorrow: {tmrw_kwh:.1f} kWh"
            )

    # ================================================================
    # Octopus Refresh
    # ================================================================

    def _refresh_octopus_rates(self, force=False):
        """Fetch current tariff rates from Octopus API."""
        if not self.octopus:
            return

        try:
            tariff_info   = self.octopus.get_current_tariff(force=force)
            monitored     = self.octopus.get_all_monitored_rates(force=force)

            self.latest_rates_data = {
                "tariff_info": tariff_info,
                **monitored,
            }

            self._update_tariff_device(tariff_info, monitored)
            self._write_tariff_schedule_variables(tariff_info, monitored)

            # Log on first fetch or when tariff / rate changes; also in debug mode
            tracker    = monitored.get("tracker", {})
            tariff_key = tariff_info.get("tariff_key", "?")
            today_rate = tracker.get("today_p")
            with self._state_lock:   # v5.45.0: fetch unlocked, store merge locked
                _changed   = (tariff_key != self.store.get("_last_tariff_key")
                              or today_rate != self.store.get("_last_tariff_rate"))
                if _changed:
                    self.store["_last_tariff_key"]  = tariff_key
                    self.store["_last_tariff_rate"] = today_rate
            if _changed or self.debug:
                log(
                    f"[Octopus] Tariff: {tariff_info.get('display_name', tariff_key)} "
                    f"({tariff_info.get('product_code', '?')}), "
                    f"today: {today_rate}p, "
                    f"tomorrow: {tracker.get('tomorrow_p', 'TBD')}p"
                )

        except Exception as e:
            log(f"[Octopus] Rate refresh error: {e}", level="ERROR")

    def _write_tariff_schedule_variables(self, tariff_info, monitored):
        """Write the rate + slot-JSON Indigo variables consumed by the openmeteo
        battery optimiser, so the plugin is the single source of truth and the
        standalone octopus_tracker_rate.py script can be retired.

        elec_rates_*_json carry the raw Octopus slots [{valid_from, valid_to,
        value_inc_vat}, ...]. Tomorrow's slots aren't published until ~16:00, so the
        tomorrow variable is only overwritten when slots are available — stale is
        softer than empty (the optimiser refuses to plan on an empty list).
        """
        if not self.octopus:
            return
        try:
            sched   = self.octopus.get_active_rate_schedule()
            tracker = (monitored or {}).get("tracker", {}) or {}
            folder  = self._sigenergy_folder_id()
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            today_slots    = sched.get("today_slots") or []
            tomorrow_slots = sched.get("tomorrow_slots") or []
            today_p        = tracker.get("today_p")
            tomorrow_p     = tracker.get("tomorrow_p")

            writes = []
            if today_slots:
                writes.append(("elec_rates_today_json", json.dumps(today_slots)))
            if tomorrow_slots:
                writes.append(("elec_rates_tomorrow_json", json.dumps(tomorrow_slots)))
            # today_p None = fetch failure (today's rate always exists once
            # published) — keep the last good numeric value rather than
            # destroying it with a string that breaks float() consumers;
            # tracker_fetch_status carries the failure signal. tomorrow_p None
            # is NORMAL before ~16:00, and "pending" is the truthful value
            # (yesterday's tomorrow-rate would be actively wrong after midnight).
            if today_p is not None:
                writes.append(("tracker_rate_today", f"{today_p:.2f}"))
            writes.append(("tracker_rate_tomorrow",
                           f"{tomorrow_p:.2f}" if tomorrow_p is not None else "pending"))
            if sched.get("product_code"):
                writes.append(("tracker_product_code", sched["product_code"]))
            if tariff_info and tariff_info.get("display_name"):
                writes.append(("tracker_product_name", tariff_info["display_name"]))
            writes.append(("tracker_last_updated", now_str))
            writes.append(("tracker_fetch_status", "OK"))

            for name, value in writes:
                var_id = self._ensure_var(name, folder)
                if var_id:
                    indigo.variable.updateValue(var_id, value)
            self.logger.debug(
                f"[Octopus] Tariff vars written: today {len(today_slots)} slot(s), "
                f"tomorrow {len(tomorrow_slots)} slot(s)"
            )
        except Exception as exc:
            log(f"[Octopus] Tariff-schedule variable write failed: {exc}", level="WARNING")

    def _accumulate_home_profile(self, home_watts):
        """Accumulate one inverter home-load reading into the 48-slot half-hourly profile.

        Called every Modbus poll (~60s).  Readings are averaged per 30-min slot over
        many days, giving a robust consumption profile that reflects actual house load
        rather than the Octopus import meter (which shows only ~0.7 kWh/day on a
        near self-sufficient system instead of the real ~12-15 kWh/day load).
        """
        now  = datetime.now()
        slot = now.hour * 2 + (1 if now.minute >= 30 else 0)
        slot = max(0, min(47, slot))
        self.store["home_profile_watts_sum"][slot] += home_watts
        self.store["home_profile_count"][slot]     += 1

    def _refresh_consumption_profile(self, force=False):
        # v5.45.0: reads the profile accumulators + writes the store — locked
        # (no network; the accumulators are fed by the locked modbus merge).
        with self._state_lock:
            return self._refresh_consumption_profile_impl(force=force)

    def _refresh_consumption_profile_impl(self, force=False):
        """Rebuild 48-slot consumption profile from accumulated inverter readings.

        Each slot (0=00:00, 1=00:30 … 47=23:30) holds the average homePowerWatts
        seen during that half-hour across all polling days.  Slots with fewer than
        HOME_PROFILE_MIN_READINGS readings fall back to the OctopusAPI default
        (UK typical ~12 kWh/day shape) so the first day still works correctly.

        Profile values are kWh per half-hourly slot (watts × 0.5 h / 1000).
        """
        try:
            default  = OctopusAPI._default_consumption_profile()
            watts_sum = self.store["home_profile_watts_sum"]
            counts    = self.store["home_profile_count"]
            profile   = []
            real_slots = 0
            for i in range(48):
                if counts[i] >= HOME_PROFILE_MIN_READINGS:
                    avg_watts = watts_sum[i] / counts[i]
                    profile.append(round(avg_watts * 0.5 / 1000.0, 4))
                    real_slots += 1
                else:
                    profile.append(default[i])

            self.store["consumption_profile"] = profile
            daily_kwh = sum(profile)
            log(
                f"[Profile] Consumption profile updated from inverter data — "
                f"daily: {daily_kwh:.1f} kWh  "
                f"({real_slots}/48 slots from real data, "
                f"{48 - real_slots} using default)"
            )
            # v5.15: republish sigen_site_config.json so the optimiser
            # script picks up the freshly-calibrated profile on its next run.
            # Wrapped in its own try so a write failure here doesn't mask
            # the profile-refresh success.
            try:
                self._write_site_config()
            except Exception as exc:
                self.logger.warning(
                    f"[Profile] Could not republish site_config after refresh: {exc}"
                )
        except Exception as e:
            log(f"[Profile] Refresh error: {e}", level="ERROR")

    def _save_home_profile(self):
        """Persist home-load profile accumulators to disk (home_load_profile.json).

        Written every 5 minutes (via _save_accumulators) and on plugin shutdown.
        The file survives across restarts and day-rollover; it is never deleted.
        """
        path = os.path.join(self.data_dir, "home_load_profile.json")
        data = {
            "watts_sum": self.store["home_profile_watts_sum"],
            "count":     self.store["home_profile_count"],
            "saved_at":  datetime.now().isoformat(),
        }
        try:
            # Atomic — written every 5 minutes; a crash mid-write must not
            # truncate the profile and silently reset it to defaults.
            _atomic_write_json(path, data)
        except Exception as e:
            self.logger.warning(f"Cannot save home profile: {e}")

    def _load_home_profile(self):
        """Restore home-load profile accumulators from disk on startup.

        If no file exists (fresh install) the in-memory defaults of all-zeros
        remain, and the first HOME_PROFILE_MIN_READINGS days fall back to the
        OctopusAPI default shape.
        """
        path = os.path.join(self.data_dir, "home_load_profile.json")
        if not os.path.exists(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            watts_sum = data.get("watts_sum", [])
            counts    = data.get("count", [])
            if len(watts_sum) == 48 and len(counts) == 48:
                self.store["home_profile_watts_sum"] = [float(v) for v in watts_sum]
                self.store["home_profile_count"]     = [int(v)   for v in counts]
                # Immediately build consumption_profile from restored data
                self._refresh_consumption_profile()
                real_slots = sum(1 for c in counts if c >= HOME_PROFILE_MIN_READINGS)
                self.logger.info(
                    f"Home load profile restored — {real_slots}/48 slots from real data"
                )
        except Exception as e:
            self.logger.warning(f"Cannot load home profile: {e}")

    # ================================================================
    # VPP State Machine
    # ================================================================

    def _vpp_poll_interval(self):
        """Return adaptive VPP poll interval.

        ACTIVE polls at 60s so the self-driven export window is tracked closely.
        """
        state = self.store["vpp_state"]
        event = self.store["vpp_event"]

        if state == VPP_ACTIVE:
            return VPP_POLL_ACTIVE_INTERVAL

        if event and state in (VPP_ANNOUNCED, VPP_PRE_CHARGING):
            start = event.get("start_time")
            if start:
                hours_away = (start - datetime.now(timezone.utc)).total_seconds() / 3600.0
                if hours_away <= 2.0:
                    return VPP_POLL_ACTIVE_INTERVAL

        return VPP_POLL_NORMAL_INTERVAL

    def _record_vpp_api_status(self, error):
        """Record the outcome of the latest Axle poll and surface a lasting failure.

        get_next_event() returns None for BOTH "no event scheduled" and a hard
        failure, so an unhealthy feed looks exactly like a quiet week. Axle
        revoked this install's token some time after 15-Jun-2026 and the plugin
        polled a dead endpoint for six weeks in complete silence — the VPP page
        simply read "Standby" throughout. This is what makes that state visible.

        Logs on the first occurrence of a given failure and hourly thereafter, so
        a sustained outage costs one line an hour rather than one every 10 min.
        """
        prev = self.store.get("vpp_api_error")
        self.store["vpp_api_error"] = error

        if error:
            self.store["vpp_api_fails"] = self.store.get("vpp_api_fails", 0) + 1
            now = time.time()
            if error != prev or now - self.store.get("vpp_api_logged", 0.0) >= 3600.0:
                self.store["vpp_api_logged"] = now
                log(f"[VPP] Axle poll failing - {error} "
                    f"(consecutive failures: {self.store['vpp_api_fails']})", "ERROR")
        else:
            if prev:
                log(f"[VPP] Axle poll recovered after {self.store.get('vpp_api_fails', 0)} "
                    f"failure(s) - API reachable again")
            self.store["vpp_api_fails"]  = 0
            self.store["vpp_api_logged"] = 0.0
            self.store["vpp_api_last_ok"] = time.time()

    def _poll_vpp(self):
        """Poll Axle API and advance VPP state machine."""
        if not self.pluginPrefs.get("axleEnabled", False):
            return
        if not self.axle:
            self._record_vpp_api_status("No API token configured")
            return
        # While the manager is paused the VPP state machine must not advance — pause
        # already stood down any active window and disengaged the inverter, so a
        # pre-charge (force-charge + raised cutoff) or a new window starting here
        # would drive the hardware behind a 'Paused' label. Resume re-detects an
        # in-progress Axle window via the late-detection path.
        if self.store.get("manager_paused", False):
            return

        event = self.axle.get_next_event()   # NETWORK — unlocked (v5.45.0)
        self._record_vpp_api_status(self.axle.last_error)
        with self._state_lock:
            self._apply_vpp_event(event)

    def _apply_vpp_event(self, event):
        """Advance the VPP state machine for a fetched event. Caller holds the lock."""
        now           = datetime.now(timezone.utc)
        current_state = self.store["vpp_state"]

        if event is None:
            # Axle API returns None when no event is scheduled or the event has ended

            if current_state == VPP_ACTIVE:
                # We self-drive on our OWN stored window — the API returning None
                # usually just means the event ended, but only stop once we are past
                # our stored end_time + 2-min tail, so a transient API blip mid-event
                # cannot cut the export short.
                stored   = self.store.get("vpp_event") or {}
                end_time = stored.get("end_time")
                if end_time is not None and now < end_time + timedelta(minutes=2):
                    self._log_vpp_snapshot(stored)
                    self._update_vpp_device()
                    return
                self._end_vpp_export(now, stored)

            elif current_state != VPP_IDLE:
                # Event cancelled / disappeared before the window opened — stand down.
                log("[VPP] Event cancelled/disappeared - restoring self-consumption")
                self._restore_discharge_cutoff()
                if self.store.get("export_active") and self.modbus:
                    self.modbus.set_self_consumption()
                self.store["export_active"]      = False
                self.store["vpp_charge_stopped"] = False
                self._vpp_transition(VPP_IDLE)
                self.store["vpp_active"] = False

            self._update_vpp_device()
            return

        # Event is scheduled
        start_time     = event["start_time"]
        end_time       = event["end_time"]
        hours_to_start = (start_time - now).total_seconds() / 3600.0

        if current_state == VPP_IDLE and hours_to_start > 0:
            self.store["vpp_event"] = event
            self.store["vpp_10min_warning_sent"]  = False  # latched per event
            # Discharge cutoff is NOT raised here — it is raised at pre-charge time
            # (30 min before event). Raising it at announcement (up to 24h early)
            # locks the battery below the floor if SOC is low, causing unnecessary
            # grid imports. Solar will always restore SOC before the event; if not,
            # Axle's own firmware will decline to dispatch.
            self._vpp_transition(VPP_ANNOUNCED)
            self._trigger_event("vppAnnounced")
            log(
                f"[VPP] Event announced: {_local_time(start_time)} - "
                f"{_local_time(end_time)} BST ({event['duration_hrs']:.1f}h)"
            )
            # Persist every field Axle's API returned to per-event JSONL file
            # under <data_dir>/vpp_events/. Read after the event with eg
            #   jq -c 'select(.type=="announcement")' <file>
            # to inspect the dispatch metadata.
            self._write_vpp_event_header(event)

        elif current_state == VPP_IDLE and hours_to_start <= 0 and now < end_time:
            # Axle published the event late — it's already under way.
            # Skip straight to ACTIVE; Axle's firmware already has control.
            mins_late = int(-hours_to_start * 60)
            log(
                f"[VPP] Late detection: event already active {_local_time(start_time)} - "
                f"{_local_time(end_time)} BST (Axle published {mins_late} min late) — "
                f"entering ACTIVE, self-driving export"
            )
            self.store["vpp_event"] = event
            self.store["vpp_export_start_kwh"] = self.store["grid_export_daily_kwh"]
            # Pre-charge was skipped, so set the discharge cutoff here too — otherwise
            # vpp_cutoff_raised stays False and _verify_ems_registers would reset the
            # cutoff to the health floor mid-window, letting a late-detected NIGHT event
            # over-discharge below the dawn reserve.
            self._set_vpp_discharge_cutoff(event, is_daytime=self._event_is_daytime(start_time))
            self._vpp_transition(VPP_ACTIVE)
            self.store["vpp_active"] = True
            self._trigger_event("vppStarted")

        elif current_state == VPP_ANNOUNCED:
            # Single T-10min warning instead of the old minute-by-minute spam.
            # Fires once when we cross 10 min; latched via vpp_10min_warning_sent.
            if hours_to_start <= 10.0 / 60.0 and not self.store.get("vpp_10min_warning_sent"):
                log(f"[VPP] Event in 10 minutes — Axle handover at T-5min, "
                    f"event window {_local_time(start_time)}-{_local_time(end_time)}")
                self.store["vpp_10min_warning_sent"] = True
            # Existing pre-charge trigger at T-30min (unchanged)
            if hours_to_start <= 0.5:
                self._start_vpp_precharge(event)

        elif current_state == VPP_PRE_CHARGING:
            required_soc       = self.store["vpp_pre_charge_soc"]
            current_soc        = self.latest_inverter_data.get("batterySoc", 0.0)
            charge_stopped     = self.store.get("vpp_charge_stopped",   False)
            soc_ready          = current_soc >= required_soc

            # Step 1: stop charging once SOC target is reached (fire once only).
            # Guard against fighting Axle: if 40031 already reads 0x06
            # (Discharge ESS first) then Axle is mid-dispatch and we must
            # not overwrite it with 0x02. Battery is already not charging
            # in that case, so the "stop charging" intent is satisfied.
            if soc_ready and not charge_stopped:
                cur_mode = self.modbus.read_ems_mode() if self.modbus else None
                if cur_mode == 0x06:
                    self.store["vpp_charge_stopped"] = True
                    log(f"[VPP] Pre-charge complete — SOC {current_soc:.0f}% >= "
                        f"{required_soc:.0f}% target. Axle already dispatching "
                        f"(40031=0x06) — leaving inverter under Axle control.")
                elif self.modbus:
                    self.modbus.set_self_consumption()
                    self.store["vpp_charge_stopped"] = True
                    log(f"[VPP] Pre-charge complete — SOC {current_soc:.0f}% >= "
                        f"{required_soc:.0f}% target. Holding in Self Consumption.")

            # Start the self-driven export 2 min BEFORE the window opens, so we are
            # already exporting by the time Axle's meter window begins (v5.28).
            if hours_to_start <= 2.0 / 60.0:
                self.store["vpp_export_start_kwh"]  = self.store["grid_export_daily_kwh"]
                self.store["vpp_charge_stopped"]    = False
                self._vpp_transition(VPP_ACTIVE)
                self.store["vpp_active"] = True
                self._trigger_event("vppStarted")
                log("[VPP] T-2min — self-driving export for the window "
                    "(ignoring Axle dispatch; meter-settled).")

        elif current_state == VPP_ACTIVE:
            # Periodic detailed snapshot — every poll cycle while active, so the
            # plugin captures what Axle is doing for post-hoc analysis.
            self._log_vpp_snapshot(event)
            if now >= end_time + timedelta(minutes=2):
                # Our timer drives the stop (+2-min tail past the window). We do not
                # wait for Axle to release anything — we never handed it over.
                self._end_vpp_export(now, event)

        self._update_vpp_device()

    def _vpp_event_log_path(self, event):
        """Return per-event JSONL file path under <data_dir>/vpp_events/.

        Filename derived from event start_time, e.g. 2026-05-15_0800.jsonl.
        Used both at announcement (to write the event header) and during
        VPP_ACTIVE (to append per-minute snapshots). Designed so the file
        can be read after the event to see exactly what Axle did with the
        inverter — without flooding the Indigo Event Log.
        """
        try:
            start = event.get("start_time")
            if hasattr(start, "strftime"):
                stamp = start.strftime("%Y-%m-%d_%H%M")
            else:
                stamp = str(start).replace(":", "")[:13]
        except Exception:
            stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M")
        dir_path = os.path.join(self.data_dir, "vpp_events")
        try:
            os.makedirs(dir_path, exist_ok=True)
        except Exception:
            pass
        return os.path.join(dir_path, f"{stamp}.jsonl")

    def _write_vpp_event_header(self, event):
        """Write the announcement record (every field Axle returned) to the
        per-event JSONL file. Called once when the event transitions to
        VPP_ANNOUNCED."""
        import json as _json
        path = self._vpp_event_log_path(event)
        record = {"type": "announcement",
                  "logged_at": datetime.now(timezone.utc).isoformat()}
        try:
            for k, v in event.items():
                if hasattr(v, "isoformat"):
                    v = v.isoformat()
                try:
                    _json.dumps(v)
                    record[k] = v
                except (TypeError, ValueError):
                    record[k] = str(v)
        except Exception:
            pass
        try:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(_json.dumps(record) + "\n")
            log(f"[VPP] Event log: {path}")
        except Exception as exc:
            log(f"[VPP] Could not write event header: {exc}", level="WARNING")

    def _vpp_driver(self, ems_mode):
        """Who is actually driving the inverter, judged from the live register.

        The old field was `"self" if store["export_active"] else "axle"` — it
        mirrored our own intent flag, so it could only ever read "self" once we
        started driving and "axle" before. It never looked at the hardware, and
        therefore could not answer the one question it existed to answer.

        Compare the LIVE mode register against the mode we last wrote instead.
        While we hold Remote EMS nothing else should be able to move it, so a
        mode we did not write means something external did — the signal that
        Axle's cloud dispatch has reached the inverter (or that a stray write
        has). Returns:
          "self"             — register matches the mode we wrote
          "external"         — register holds a mode we did not write
          "self (unverified)" — Modbus read failed, or no mode written yet
          "idle"             — not driving an export right now
        """
        if not self.store.get("export_active"):
            return "idle"
        expected = self.store.get("vpp_export_mode")
        if ems_mode is None or expected is None:
            return "self (unverified)"
        return "self" if ems_mode == expected else "external"

    def _log_vpp_snapshot(self, event):
        """Append one per-minute snapshot of inverter state to the per-event
        JSONL file. Captures everything we'd need to reconstruct what Axle
        did: power flows, EMS mode + limits, SOC. NOT logged to the Indigo
        Event Log — that stays clean.
        """
        import json as _json
        now_ts = time.time()
        if now_ts - self.store.get("vpp_last_snapshot_at", 0) < 55:
            return
        self.store["vpp_last_snapshot_at"] = now_ts

        try:
            inv = self.latest_inverter_data or {}
            ems_mode = self.modbus.read_ems_mode()        if self.modbus and self.modbus.connected else None
            chg_lim  = self.modbus.read_charge_limit()    if self.modbus and self.modbus.connected else None
            dis_lim  = self.modbus.read_discharge_limit() if self.modbus and self.modbus.connected else None

            try:
                start = event["start_time"]
                now_dt = datetime.now(timezone.utc)
                elapsed = (now_dt - start).total_seconds()
            except Exception:
                elapsed = None

            ems_mode_names = {0x00: "PCS Remote Control", 0x01: "Standby",
                              0x02: "Max Self Consumption", 0x03: "Charge Grid First",
                              0x04: "Charge PV First", 0x05: "Discharge PV First",
                              0x06: "Discharge ESS First", 0x07: "Reserved", 0x08: "V2G"}
            record = {
                "type":               "snapshot",
                "logged_at":          datetime.now(timezone.utc).isoformat(),
                "event_elapsed_secs": elapsed,
                "soc_pct":            inv.get("batterySoc"),
                "pv_w":               inv.get("pvPowerWatts"),
                "battery_w":          inv.get("batteryPowerWatts"),
                "home_w":             inv.get("homePowerWatts"),
                "grid_w":             inv.get("gridPowerWatts"),
                "ems_work_mode":      inv.get("emsWorkMode"),
                "ems_mode_register":  ems_mode,
                "ems_mode_name":      ems_mode_names.get(ems_mode) if ems_mode is not None else None,
                "driver":             self._vpp_driver(ems_mode),
                "charge_limit_w":     chg_lim,
                "discharge_limit_w":  dis_lim,
                "grid_status":        inv.get("gridStatus"),
                "battery_temp_c":     inv.get("batteryTempC"),
                "battery_cell_v":     inv.get("batteryCellVoltage"),
                "plant_state":        inv.get("plantRunningState"),
            }
            path = self._vpp_event_log_path(event)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(_json.dumps(record) + "\n")
        except Exception:
            pass   # snapshot is best-effort, never raise during active event

    def _summarise_vpp_event(self, event):
        """Parse the per-event JSONL file we just closed, compute summary
        stats, write them to the axleVppMonitor device states, and fire a
        Pushover with the headline numbers plus a pre-formed Claude prompt
        the user can paste straight into Claude Code.

        Called once, at the VPP_ACTIVE -> COOLING_OFF transition.  Best-effort:
        never raise — any failure here must not interfere with the cool-off
        state machine.
        """
        import json as _json
        try:
            path = self._vpp_event_log_path(event)
        except Exception:
            path = ""

        # Parse all snapshot records out of the JSONL file
        snapshots   = []
        ended       = None
        if path and os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rec = _json.loads(line)
                        except Exception:
                            continue
                        rtype = rec.get("type")
                        if rtype == "snapshot":
                            snapshots.append(rec)
                        elif rtype == "announcement":
                            pass  # announcement records are not surfaced here
                        elif rtype == "event_ended":
                            ended = rec
            except Exception as exc:
                log(f"[VPP] Could not parse event log {path}: {exc}",
                    level="WARNING")

        def _to_float(v, default=0.0):
            try:
                return float(v)
            except (TypeError, ValueError):
                return default

        # ----- core summary stats ----------------------------------------
        export_kwh   = _to_float((ended or {}).get("export_kwh", 0.0))
        pv_watts     = [_to_float(s.get("pv_w"))      for s in snapshots]
        bat_watts    = [_to_float(s.get("battery_w")) for s in snapshots]
        grid_watts   = [_to_float(s.get("grid_w"))    for s in snapshots]
        ems_strings  = [s.get("ems_work_mode")        for s in snapshots
                        if s.get("ems_work_mode")]

        min_pv_w           = int(min(pv_watts))                  if pv_watts   else 0
        # battery discharging = negative watts; "max discharge" is most negative magnitude
        max_bat_dis_w      = int(-min(bat_watts))                if bat_watts  else 0
        # grid exporting = negative watts; "peak export" is most negative magnitude
        peak_grid_export_w = int(-min(grid_watts))               if grid_watts else 0

        # Was the driver ever something other than us? Any snapshot reading
        # "external" means the live mode register held a mode we did not write.
        drivers_seen  = {s.get("driver") for s in snapshots if s.get("driver")}
        external_seen = "external" in drivers_seen
        driver_str    = ", ".join(sorted(drivers_seen)) if drivers_seen else "(unknown)"

        # Did our export mode curtail PV?  Mode 0x06 makes the battery do all the
        # discharge and, with the grid held at the DNO cap, the MPPT shuts down —
        # the 15-Jun-2026 fault that mode 0x05 (daytime) exists to avoid.
        #
        # THIS QUESTION ONLY EXISTS IN DAYLIGHT.  The old test was a bare
        # `min_pv_w > 100`, so every dark window reported "PV collapsed" — an
        # alarm about the sun having set.  Live case 05-Aug-2026, a 21:00-22:00
        # BST window: 45 snapshots at 0 W PV, a clean 4.23 kWh export, and a
        # Pushover claiming PV had collapsed.  Gate on the daylight flag latched
        # at VPP_ACTIVE entry, falling back to a fresh solar-window check if the
        # store has been cleared (a restart between event end and summary).
        daytime = self.store.get("vpp_is_daytime")
        if daytime is None:
            try:
                daytime = self._event_is_daytime(event.get("start_time"))
            except Exception:
                daytime = False
        daytime = bool(daytime)

        if not daytime:
            pv_status = "n/a (dark window)"
        elif pv_watts and min_pv_w > 100:   # 100 W tolerates the event-end sample
            pv_status = "ran"
        else:
            pv_status = "curtailed"

        # The boolean is the "nothing went wrong with PV" flag a trigger wants,
        # so a dark window is True — there was no PV to lose.  pv_status carries
        # the detail that a boolean cannot.
        pv_survived = pv_status != "curtailed"

        # rough PV produced over the window: avg watts * hours
        try:
            duration_hrs = float(event.get("duration_hrs", 0.0))
        except Exception:
            duration_hrs = 0.0
        if pv_watts:
            avg_pv_w = sum(pv_watts) / len(pv_watts)
            pv_kwh   = (avg_pv_w * duration_hrs) / 1000.0
        else:
            pv_kwh = 0.0

        ems_modes_seen = sorted({m for m in ems_strings if m})
        ems_modes_str  = ", ".join(ems_modes_seen) if ems_modes_seen else "(unknown)"

        # ----- write summary to axleVppMonitor states --------------------
        try:
            dev = self._find_device("axleVppMonitor")
            if dev:
                event_date = ""
                try:
                    st = event.get("start_time")
                    if hasattr(st, "strftime"):
                        event_date = _local_time(st, "%Y-%m-%d %H:%M")
                    else:
                        event_date = str(st)
                except Exception:
                    pass
                dev.updateStatesOnServer([
                    {"key": "lastVppDate",                 "value": event_date},
                    {"key": "lastVppExportKwh",            "value": round(export_kwh, 2)},
                    {"key": "lastVppPvKwh",                "value": round(pv_kwh,     2)},
                    {"key": "lastVppMinPvW",               "value": min_pv_w},
                    {"key": "lastVppMaxBatteryDischargeW", "value": max_bat_dis_w},
                    {"key": "lastVppPeakGridExportW",      "value": peak_grid_export_w},
                    {"key": "lastVppPvSurvived",           "value": pv_survived},
                    {"key": "lastVppPvStatus",             "value": pv_status},
                    {"key": "lastVppEmsModes",             "value": ems_modes_str},
                    {"key": "lastVppDriver",               "value": driver_str},
                    {"key": "lastVppLogPath",              "value": path or ""},
                ])
        except Exception as exc:
            log(f"[VPP] Could not update axleVppMonitor summary states: {exc}",
                level="WARNING")

        # ----- Pushover: headline numbers + pre-formed Claude prompt -----
        # The prompt used to ask what AXLE did — wording left over from the
        # observe-and-hand-over model that v5.28.0 replaced with self-drive in
        # June. We drive every window ourselves now and ignore Axle's dispatch,
        # so those questions had a false premise baked in and sent the reader
        # looking for behaviour that was never going to be in the file.
        window_str = "daytime" if daytime else "dark"
        title = f"VPP window done — {export_kwh:.2f} kWh exported"
        body_lines = [
            f"Export:   {export_kwh:.2f} kWh",
            f"PV:       {pv_kwh:.2f} kWh ({pv_status}; min {min_pv_w} W)",
            f"Battery:  peak discharge {max_bat_dis_w} W",
            f"Grid:     peak export {peak_grid_export_w} W",
            f"EMS:      {ems_modes_str}",
            f"Window:   {window_str}   Driver: {driver_str}",
        ]
        if external_seen:
            body_lines.append("WARNING:  external control detected mid-window")
        body_lines += [
            "",
            "── Ask Claude ──",
            "Read the latest VPP JSONL file at",
            f"  {path}",
            "This was a SELF-DRIVEN export (we hold Remote EMS over Modbus and",
            "ignore Axle's dispatch; Axle settles on the meter). Tell me:",
            f"  1. Did we hold the export target for the full window ({window_str})?",
            "  2. What EMS mode + register values did we use, and were they",
            "     the right ones for the conditions?",
            "  3. Did anything external move the mode register mid-window?",
            "  4. Any recommended changes to _drive_vpp_export /",
            "     _verify_ems_registers.",
        ]
        body = "\n".join(body_lines)
        try:
            self._send_pushover(title, body, priority="0")
        except Exception as exc:
            log(f"[VPP] Pushover summary send failed: {exc}", level="WARNING")

        # Also log the headline to the Indigo Event Log so the user has a
        # single grep-able line for each event.  No JSONL spam, just the
        # one-liner.
        log(f"[VPP] Summary: {export_kwh:.2f} kWh exported, "
            f"PV {pv_kwh:.2f} kWh ({pv_status}), "
            f"peak grid export {peak_grid_export_w} W, "
            f"{window_str} window, driver {driver_str}, "
            f"EMS modes: {ems_modes_str}. Log: {path}")
        if external_seen:
            log("[VPP] External control was detected during the window — the mode "
                "register held a value the plugin did not write. Check the JSONL "
                "snapshots for when it changed.", level="WARNING")

    def _start_vpp_precharge(self, event):
        """Assess SOC 30 min before VPP event; raise discharge cutoff; no grid import.

        The discharge cutoff is raised here (not at announcement) so it only
        applies close to the event — avoiding unnecessary battery lockout hours
        in advance. If SOC is below the required level, we do NOT import from
        grid: solar will have charged the battery throughout the day, and if
        energy is still short, Axle's own firmware will decline to dispatch
        rather than us importing at cost to cover their export.
        """
        duration_hrs    = event.get("duration_hrs", 1.0)
        cap_kwh         = _as_float(self.pluginPrefs.get("batteryCapacityKwh"), BATTERY_CAPACITY_KWH)
        if cap_kwh <= 0:
            cap_kwh = BATTERY_CAPACITY_KWH   # guard the (required_kwh / cap_kwh) divisions below
        max_export_kw   = _as_float(self.pluginPrefs.get("maxExportKw"), 4.0)
        dawn_target_pct = _as_float(self.pluginPrefs.get("dawnSocTarget"), 10)

        # For daytime events solar will recharge during/after the event, so we
        # only need to hold the export energy itself.  For night events we must
        # also hold the dawn reserve so the battery survives until morning.
        is_daytime  = self._event_is_daytime(event.get("start_time"))
        export_kwh  = max_export_kw * duration_hrs / VPP_DISCHARGE_EFFICIENCY
        if is_daytime:
            required_kwh = export_kwh
            required_soc = min(100.0, (required_kwh / cap_kwh) * 100.0)
            dawn_kwh     = 0.0
        else:
            dawn_kwh     = cap_kwh * dawn_target_pct / 100.0
            required_kwh = export_kwh + dawn_kwh
            required_soc = min(100.0, (required_kwh / cap_kwh) * 100.0)
            required_soc = max(required_soc, dawn_target_pct)

        # Current battery level
        current_soc  = self.latest_inverter_data.get("batterySoc", 0.0)
        current_kwh  = cap_kwh * current_soc / 100.0

        self.store["vpp_pre_charge_soc"] = required_soc

        # Set discharge cutoff now (30 min before) — not at announcement time
        self._set_vpp_discharge_cutoff(event, is_daytime)

        if current_kwh >= required_kwh:
            if is_daytime:
                log(
                    f"[VPP] SOC sufficient ({current_soc:.0f}%, {current_kwh:.1f} kWh) for "
                    f"{duration_hrs:.1f}h export ({export_kwh:.1f} kWh) — daytime, solar will recharge"
                )
            else:
                log(
                    f"[VPP] SOC sufficient ({current_soc:.0f}%, {current_kwh:.1f} kWh) for "
                    f"{duration_hrs:.1f}h export ({export_kwh:.1f} kWh) + dawn reserve "
                    f"({dawn_kwh:.1f} kWh)"
                )
        else:
            shortfall = required_kwh - current_kwh
            log(
                f"[VPP] SOC low ({current_soc:.0f}%, shortfall {shortfall:.1f} kWh) — "
                f"proceeding without grid import; Axle will assess at dispatch time"
            )

        self._vpp_transition(VPP_PRE_CHARGING)

    def _set_vpp_discharge_cutoff(self, event, is_daytime=False):
        """Set discharge cutoff at pre-charge time (30 min before event).

        Daytime events (solar forecast available during/after event):
          Use the health floor (1%) — the battery can discharge freely because
          solar will recharge it during the day.  No need to hold a dawn reserve.

        Night events (before dawn or after dusk, no solar recharge coming):
          Use the dawn target (15%) — ensures the battery can survive overnight
          until the next morning's solar even after the event has dispatched.

        Called from _start_vpp_precharge() with the is_daytime flag already
        determined, NOT at announcement time.
        """
        health_floor    = _as_float(self.pluginPrefs.get("batteryHealthCutoff"), 1.0)
        dawn_target_pct = _as_float(self.pluginPrefs.get("dawnSocTarget"), 10)

        if is_daytime:
            floor_pct = health_floor
            reason    = "daytime event — solar will recharge"
        else:
            floor_pct = dawn_target_pct
            reason    = "night event — protecting dawn floor"

        floor_pct = max(floor_pct, health_floor)  # never below the health floor

        if self.modbus:
            self.modbus.set_discharge_cutoff(floor_pct)
            self.store["vpp_cutoff_raised"] = True   # prevents verify() fighting the VPP floor
            log(f"[VPP] Discharge cutoff set to {floor_pct:.0f}% ({reason})")

    def _event_is_daytime(self, event_start):
        """Return True if event_start falls within the solar generation window.

        Uses _dawn_times (first PV slot above threshold) and the hourly forecast
        (last non-zero slot) to bracket the solar window for the event's date.
        Returns False if event_start is None or solar data is unavailable —
        night-event behaviour is the safe fallback.
        """
        if event_start is None:
            return False

        fcast      = self.latest_forecast_data or {}
        dawn_times = fcast.get("_dawn_times", {})

        # Convert event start to local (London) time for date lookup. An hour's
        # error here flips a dusk-edge window to "daytime" and runs mode 0x05
        # when it should run 0x06 (or the reverse — the 15-Jun-2026 PV
        # curtailment), so this must not degrade silently.
        if _london_tz() is None:
            _warn_no_tzdb()
        event_local = _to_london(event_start)

        event_date_str = event_local.strftime("%Y-%m-%d")

        # Dawn: first PV-generating slot on the event's date
        dawn = dawn_times.get(event_date_str)
        if dawn is None:
            return False   # no solar expected that day

        # Dusk: last slot with non-zero generation on the event's date
        # Check both today and tomorrow hourly dicts
        dusk = None
        for hourly_key in ("_hourly_p50_today", "_hourly_p50_tomorrow"):
            hourly = fcast.get(hourly_key, {})
            for slot_str in sorted(hourly.keys(), reverse=True):
                if slot_str.startswith(event_date_str) and hourly[slot_str] > 0:
                    try:
                        dt_naive = datetime.strptime(slot_str, "%Y-%m-%d %H:%M:%S")
                        # Forecast slots are local wall-clock, so localise (not
                        # stamp) — see _london_localise on why that distinction
                        # cannot be hand-rolled per site.
                        dusk = _london_localise(dt_naive) or dt_naive
                    except Exception:
                        pass
                    break
            if dusk is not None:
                break

        if dusk is None:
            return False   # can't determine dusk — treat as night

        # Compare event_start against the solar window
        try:
            return dawn <= event_start <= dusk
        except TypeError:
            # Mixed tz-aware / naive — strip timezone for comparison
            def _naive(dt):
                return dt.replace(tzinfo=None) if getattr(dt, "tzinfo", None) else dt
            return _naive(dawn) <= _naive(event_start) <= _naive(dusk)

    def _restore_discharge_cutoff(self):
        """Restore discharge cutoff to the health floor after VPP."""
        if self.modbus:
            health_floor = _as_float(self.pluginPrefs.get("batteryHealthCutoff"), 1.0)
            self.modbus.set_discharge_cutoff(health_floor)
            self.store["vpp_cutoff_raised"] = False   # allow verify() to manage cutoff again
            log(f"[VPP] Discharge cutoff restored to {health_floor:.0f}%")

    def _disengage_to_safe_baseline(self, reason):
        """Return the inverter to the safe self-consumption baseline AND release any
        raised discharge-cutoff floor (flood-prevention / storm / VPP), standing
        down any in-flight VPP export.

        Used by both prepare_to_sleep() and pause.  set_self_consumption() clears
        the forced MODE and the charge/discharge LIMIT registers, but NOT the
        discharge-cutoff SOC register — so a cutoff left raised (e.g. a 30% flood
        pre-drain target, or a VPP pre-charge floor) would lock the battery above
        that SOC for the whole hands-off period and force grid import, defeating the
        self-sufficiency KPI under a 'safe' label.  This helper resets the cutoff to
        the health floor and clears the raised-floor flags so the manager re-evaluates
        cleanly on wake/resume.  Best-effort; never raises (a hands-off transition
        must always complete).
        """
        # 1. Stand down any VPP engagement first.  For an ACTIVE window _end_vpp_export
        #    already restores the cutoff + records the wrap-up; earlier states just drop
        #    back to IDLE so a later resume/wake re-detects the window if it's still open.
        vpp_state = self.store.get("vpp_state", VPP_IDLE)
        if vpp_state == VPP_ACTIVE:
            try:
                self._end_vpp_export(datetime.now(timezone.utc),
                                     self.store.get("vpp_event"))
            except Exception as exc:
                log(f"[{reason}] VPP stand-down failed: {exc}", level="WARNING")
        elif vpp_state != VPP_IDLE:
            self.store["vpp_event"]  = None
            self.store["vpp_active"] = False
            self._vpp_transition(VPP_IDLE)
        # 2. Safe baseline mode + reset the discharge cutoff to the health floor.
        if self.modbus and self.modbus.connected:
            try:
                self.modbus.set_self_consumption()
            except Exception as exc:
                log(f"[{reason}] set_self_consumption failed: {exc}", level="WARNING")
            try:
                health_floor = _as_float(self.pluginPrefs.get("batteryHealthCutoff"), 1.0)
                self.modbus.set_discharge_cutoff(health_floor)
                log(f"[{reason}] Discharge cutoff reset to {health_floor:.0f}% (health floor)")
            except Exception as exc:
                log(f"[{reason}] discharge-cutoff reset failed: {exc}", level="WARNING")
            try:
                if self.store.get("import_charge_cutoff_pct"):
                    self.modbus.set_charge_cutoff(100.0)
                    log(f"[{reason}] Charge cutoff restored to 100% (import backstop released)")
            except Exception as exc:
                log(f"[{reason}] charge-cutoff reset failed: {exc}", level="WARNING")
        # 3. Clear the raised-floor flags so _verify_ems_registers manages the cutoff
        #    again on the next evaluate (it defers while flood/VPP floors are flagged).
        self._set_flood_prev_target(None)
        self._set_import_cutoff(None)
        self.store["vpp_cutoff_raised"] = False
        self.store["import_active"]     = False
        self.store["export_active"]     = False

    def _end_vpp_export(self, now, event):
        """Stop the self-driven VPP export at window end (+2-min tail).

        Restores Self Consumption + the health-floor discharge cutoff and returns
        the state machine straight to IDLE — no COOLING_OFF / Axle-release wait,
        because we never handed control to Axle. Records the wrap-up + summary.
        """
        event = event or {}
        vpp_export = (self.store["grid_export_daily_kwh"]
                      - self.store.get("vpp_export_start_kwh", 0.0))
        self.store["vpp_last_export_kwh"] = round(vpp_export, 2)

        if self.modbus:
            self.modbus.set_self_consumption()
        self._restore_discharge_cutoff()

        self.store["export_active"]        = False
        self.store["vpp_active"]           = False
        self.store["vpp_export_submode"]   = None
        self.store["vpp_bank_charge_cap_w"] = -1
        self._vpp_transition(VPP_IDLE)
        self.store["vpp_event"]     = None
        self.store["had_vpp_today"] = True
        self._trigger_event("vppEnded")

        # Wrap-up record in the per-event JSONL file (best-effort)
        try:
            import json as _json
            path = self._vpp_event_log_path(event)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(_json.dumps({
                    "type":       "event_ended",
                    "logged_at":  now.isoformat(),
                    "export_kwh": round(vpp_export, 3),
                }) + "\n")
        except Exception:
            pass

        log(f"[VPP] >>> EVENT COMPLETE <<<  self-driven export ~{vpp_export:.2f} kWh. "
            f"Restored to Self Consumption.")

        # Post-event summary -> axleVppMonitor states + Pushover (best-effort)
        try:
            self._summarise_vpp_event(event)
        except Exception as _exc:
            log(f"[VPP] Summary step failed: {_exc}", level="WARNING")

    def _vpp_transition(self, new_state):
        """Transition VPP state machine to a new state."""
        old_state = self.store["vpp_state"]
        self.store["vpp_state"] = new_state
        if self.debug:
            log(f"[VPP] State: {old_state} -> {new_state}")

        # On entry to VPP_ACTIVE we self-drive the export (v5.28). Axle settle on
        # the meter reading so exporting it ourselves counts identically — and
        # their cloud dispatch proved unreliable (no-show 10-Jun-2026; Axle
        # acknowledged a SigEnergy-API fault that may not be fixed before the next
        # event). _drive_vpp_export() does the actual driving here for a prompt
        # start at T-2min, and the manager's ACTION_VPP_EXPORT override re-runs it
        # each tick. The discharge floor (next-day reserve) was set at pre-charge.
        if new_state == VPP_ACTIVE:
            if self.store.get("solar_overflow_active"):
                self.store["solar_overflow_active"]       = False
                self.store["solar_overflow_charge_cap_w"] = 0
                # Same dwell as an ordinary release: a daylight VPP window ending
                # should not hand straight back to a marginal overflow decision.
                self.store["solar_overflow_released_at"]  = datetime.now(timezone.utc)

            # Latch whether this is a daylight window (set once at entry). The live
            # sub-mode (bank-surplus vs discharge) is then re-decided every tick by
            # _drive_vpp_export from real PV/home; vpp_export_mode tracks the actual
            # mode register (0x02/0x05/0x06) for the verify loop.
            event = self.store.get("vpp_event") or {}
            self.store["vpp_is_daytime"]       = self._event_is_daytime(event.get("start_time"))
            self.store["vpp_export_submode"]   = None    # force a mode log on first drive
            self.store["vpp_bank_charge_cap_w"] = -1
            log("[VPP] >>> VPP WINDOW ACTIVE <<<  self-driving export "
                f"({'daytime' if self.store['vpp_is_daytime'] else 'dark'} window, "
                "DNO-capped; Axle dispatch ignored).")
            self._drive_vpp_export()

        # Persist the new state immediately (crash-safe, atomic) so a restart or
        # crash mid-window resumes without relying on the Axle API still
        # returning the active event. accumulators.json is the authoritative
        # cross-restart store (pluginPrefs only flush on a graceful shutdown).
        self._save_accumulators()

    def _rehydrate_vpp_state(self):
        """Resume an in-progress Axle VPP window after a plugin restart.

        The VPP state machine is otherwise re-driven purely from the Axle API on
        each poll, so a restart mid-window relies on Axle still returning the
        active event. If its endpoint drops the event once the window is live,
        the rest of the window would be silently missed — Predbat issue #3051's
        failure mode. The window is persisted to accumulators.json on every
        transition and restored into the store by _load_accumulators; here, with
        modbus available, we make the time-based decision.

        Best-effort — never blocks startup.
        """
        state = self.store.get("vpp_state", VPP_IDLE)
        event = self.store.get("vpp_event")
        now   = datetime.now(timezone.utc)
        decision = _vpp_resume_decision(state, event, now)

        if decision == "idle":
            return

        if decision == "ended":
            # Window finished while the plugin was down. The discharge-cutoff
            # register may still be raised in hardware (it survives a plugin
            # restart) — reset it to the health floor and drop back to Self
            # Consumption so the manager evaluates cleanly.
            log(f"[VPP] Persisted window ended during downtime "
                f"(ended {_local_time(event.get('end_time'))}) — cleaning up")
            try:
                if self.modbus:
                    self.modbus.set_self_consumption()
                self._restore_discharge_cutoff()
            except Exception as exc:
                log(f"[VPP] Restart cleanup failed: {exc}", level="WARNING")
            self.store["vpp_event"]         = None
            self.store["vpp_active"]        = False
            self.store["export_active"]     = False
            self.store["vpp_cutoff_raised"] = False
            self._vpp_transition(VPP_IDLE)   # re-persists, clearing the window
            return

        # decision == "resume" — the window is still open. Restore the state
        # machine only; NO hardware re-issue here. The discharge-cutoff register
        # persisted across the restart, and for an ACTIVE window the manager's
        # ACTION_VPP_EXPORT override re-drives the export on its next tick.
        # Keeping vpp_cutoff_raised set stops _verify_ems_registers lowering the
        # floor before then.
        self.store["vpp_active"] = (state == VPP_ACTIVE)
        log(f"[VPP] Resuming persisted '{state}' window after restart — "
            f"{_local_time(event.get('start_time'))}-{_local_time(event.get('end_time'))} "
            f"(Axle API not required; cutoff-raised="
            f"{self.store.get('vpp_cutoff_raised', False)})")

    def _drive_vpp_export(self):
        """Self-drive the VPP export, re-evaluated each manager tick during VPP_ACTIVE.

        Two sub-modes, chosen from live PV vs the export target:

        - "bank"  (daytime, PV surplus >= target): stay in Max Self Consumption
          (mode 0x02) and CAP the battery charge to (surplus - target). The
          inverter then exports the target to the grid (held at the DNO cap) and
          banks only the PV above the target — full paid export AND the battery
          charges, with no PV curtailment. This is the same mechanism as Solar
          Overflow (proven on hardware 15-Jun-2026: PV 5.75 kW -> home 0.61 +
          export 4.00 + battery charge 1.13).

        - "discharge" (dark window, or PV surplus < target): issue a discharge
          command so the battery tops the export up to the target — daytime uses
          mode 0x05 (PV-first) with charge pinned to 0; dark uses 0x06 (ESS-first).

        Hysteresis (HYST_W) around the crossover stops the mode flapping when PV
        hovers at the target. Modbus is only written on a sub-mode change or when
        the bank charge cap shifts beyond a deadband, so re-running every tick is
        cheap. The grid export is held at the DNO cap by the inverter's
        commissioned export limit in every sub-mode.
        """
        if not self.modbus:
            return

        first      = not self.store.get("export_active")
        inv_max_w  = int(_as_float(self.pluginPrefs.get("inverterMaxKw"), 10.0) * 1000)
        target_w   = int(_as_float(self.pluginPrefs.get("maxExportKw"), 4.0) * 1000)
        daytime    = bool(self.store.get("vpp_is_daytime"))

        inv       = self.latest_inverter_data or {}
        pv_w      = int(inv.get("pvPowerWatts", 0))
        home_w    = int(inv.get("homePowerWatts", 0))
        surplus_w = max(0, pv_w - home_w)

        # Guarantee the full export: the instant surplus < target, use discharge so
        # the battery tops the grid up to the target (bank mode / self-consumption
        # will NOT discharge to export, so it would fall short). Only ENTER bank when
        # surplus is comfortably above target (>= target + HYST) — that one-sided
        # margin stops a brief PV spike flapping us into 0x02. Once banking, hold it
        # down to target (in the [target, target+HYST) band bank still exports the
        # full target, just banks a little less), then drop to discharge below target.
        HYST_W   = 400
        prev_sub = self.store.get("vpp_export_submode")
        if not daytime:
            sub = "discharge"
        elif surplus_w < target_w:
            sub = "discharge"
        elif surplus_w >= target_w + HYST_W:
            sub = "bank"
        else:
            sub = prev_sub or "discharge"   # band [target, target+HYST): hold prev

        if sub == "bank":
            charge_cap_w = max(0, surplus_w - target_w)
            if prev_sub != "bank":
                # Switch into Max Self Consumption (resets limits to inv_max), then
                # apply the charge cap below. vpp_export_mode tracks 0x02 for verify.
                self.modbus.set_self_consumption()
                self.store["vpp_export_mode"]       = 0x02
                self.store["vpp_bank_charge_cap_w"] = -1   # force the cap write below
                log(f"[VPP] Bank-surplus export — PV {pv_w}W, home {home_w}W, surplus "
                    f"{surplus_w}W >= target {target_w}W: mode 0x02, charge cap "
                    f"{charge_cap_w}W (export {target_w}W from PV, bank the rest).")
            prev_cap = self.store.get("vpp_bank_charge_cap_w", -1)
            if abs(prev_cap - charge_cap_w) > 300:
                self.modbus.set_charge_limit(charge_cap_w, quiet=True)
                self.store["vpp_bank_charge_cap_w"] = charge_cap_w
        else:
            if prev_sub != "discharge":
                if daytime:
                    self.modbus.daytime_export(inv_max_w)   # mode 0x05 + charge 0
                    self.store["vpp_export_mode"] = 0x05
                else:
                    self.modbus.night_export(inv_max_w)     # mode 0x06
                    self.store["vpp_export_mode"] = 0x06
                self.store["vpp_bank_charge_cap_w"] = -1
                log(f"[VPP] Discharge export — PV {pv_w}W, home {home_w}W, surplus "
                    f"{surplus_w}W < target {target_w}W: mode "
                    f"{'0x05 PV-first' if daytime else '0x06 ESS-first'}, battery tops "
                    f"the export up to {target_w}W.")

        self.store["vpp_export_submode"] = sub
        if first:
            self.store["export_active"] = True
            self._trigger_event("exportStarted")

    # ================================================================
    # Midnight Tasks
    # ================================================================

    def _check_midnight(self):
        # v5.45.0: accumulator rollover is a compound store mutation — locked.
        with self._state_lock:
            return self._check_midnight_impl()

    def _check_midnight_impl(self):
        """Run once-daily tasks at local (Europe/London) midnight.

        Naive datetime.now() returns server-local time which may not match
        Europe/London if the host runs UTC, causing accumulators to roll over
        on the wrong calendar day around BST/UTC boundaries.
        """
        today = _local_today_str()   # Europe/London (shared with init + accumulator restore)
        if today == self.store["today_date"]:
            return  # Not yet midnight

        # New day
        yesterday = self.store["today_date"]
        log(f"Midnight: recording daily history for {yesterday}")

        # Write the bias-accuracy record for yesterday (pass the date explicitly —
        # datetime.now() inside record_accuracy would give the new day). The
        # morning baseline it consumes was captured by the forecast module itself
        # on yesterday's FIRST complete fetch (day-ahead), and record_accuracy
        # skips if the baseline's date doesn't match — no capture call here
        # (capturing at midnight fed it a same-day hindcast, v5.43 fix).
        if self.forecast:
            self.forecast.record_accuracy(self.store["pv_daily_kwh"], date_str=yesterday)

        # Write daily history — every record kept, no cap since v5.7
        self._write_daily_history(yesterday)

        # Forecast accuracy: log the rolling 7-day summary so trends are
        # visible without having to read the JSON file.
        try:
            summary = self.forecast.get_accuracy_summary(window_days=7) if self.forecast else {"days": 0}
            if summary["days"] > 0:
                log(
                    f"[Forecast] 7-day accuracy: MAPE {summary['mape_pct']:.1f}%  "
                    f"mean factor {summary['mean_factor']:.2f}  "
                    f"(over: {summary['over_count']}, under: {summary['under_count']})"
                )
        except Exception as exc:
            self.logger.debug(f"Accuracy summary skipped: {exc}")

        # Weekly data-directory backup — once a week on Monday's midnight task.
        # Cheap insurance against accumulator/SoH/daily_history corruption.
        try:
            today_dt = datetime.strptime(today, "%Y-%m-%d")
            if today_dt.weekday() == 0:
                self._backup_data_dir(today)
        except (ValueError, OSError) as exc:
            self.logger.debug(f"[Backup] Skipped: {exc}")

        # Battery State-of-Health snapshot — once a week, on Monday's midnight task.
        # LFP cells typically lose ~0.3%/year of capacity in normal cycling; anything
        # faster is worth flagging.  Cheap to compute and stored as a small JSON ring
        # buffer in data_dir.
        try:
            today_dt = datetime.strptime(today, "%Y-%m-%d")
            if today_dt.weekday() == 0:   # Monday
                self._record_battery_soh_snapshot(today)
        except (ValueError, OSError) as exc:
            self.logger.debug(f"SoH snapshot skipped: {exc}")

        # Daily export-sync check: compare Sigenergy daily export totals against
        # what Octopus has settled (D-3 to D-9). One-line INFO summary, silent
        # if export MPAN isn't configured. Full results are visible on the
        # web dashboard's Export Sync card.
        try:
            self._log_export_sync_summary()
        except Exception as exc:
            self.logger.debug(f"[ExportSync] Skipped: {exc}")

        # Write final daily totals to Indigo variables before reset
        self._write_energy_summary_variables()
        self.store["last_energy_var"] = time.time()

        # Reset accumulators
        self.store["pv_daily_kwh"]              = 0.0
        self.store["grid_import_daily_kwh"]     = 0.0
        self.store["grid_export_daily_kwh"]     = 0.0
        self.store["home_daily_kwh"]            = 0.0
        self.store["peak_soc"]                  = 0.0
        self.store["min_soc"]                   = 100.0
        self.store["peak_pv_w"]                 = 0
        self.store["peak_pv_time"]              = ""
        self.store["today_date"]                = today
        # Clear lifetime anchors — next poll will re-snapshot at the new day's baseline
        self.store["pv_lifetime_start_kwh"]     = None
        self.store["import_lifetime_start_kwh"] = None
        self.store["export_lifetime_start_kwh"] = None

        self._save_accumulators()

        # Rewrite the *_today_* variable set now the accumulators are zeroed —
        # without this the just-ended day's full totals sat in the today
        # variables for up to 30 minutes while yesterday was already inside
        # the month roll-up (a today+month double-count window).
        self._write_energy_summary_variables()
        self.store["last_energy_var"] = time.time()

    def _write_daily_history(self, date_str):
        """Append today's totals to the all-time daily history.

        Persists both the import rate (`rate_today_p`) and the export rate
        (`export_rate_p`) on every record so that historical economics roll-ups
        use the exact rate that was in effect on each day rather than today's
        live rate.  Without this, a future export-rate change would
        retroactively re-value every past day at the new rate.
        """
        # Capture the export rate as it stood on this day.  Falls back to
        # 12p (Octopus Outgoing flat from 26-Mar-2026) if the live feed
        # hasn't published a value yet.
        try:
            export_rate_p = float(
                (self.latest_rates_data or {}).get("export_rate_p", 0.0)
            )
            if export_rate_p <= 0:
                export_rate_p = DEFAULT_EXPORT_RATE_P
        except (TypeError, ValueError):
            export_rate_p = DEFAULT_EXPORT_RATE_P

        # Capture the standing charges + gas unit rate in force on this day, so
        # the whole-house settle values each frozen day at its OWN rates rather
        # than whatever the ledger reads when the settle pass later runs.  A
        # tariff or price-cap change must never retroactively re-value past days.
        day_elec_standing_p = day_gas_unit_p = day_gas_standing_p = None
        try:
            _fin = self.octopus.get_account_financials() if self.octopus else None
        except Exception:
            _fin = None
        if _fin:
            if _fin.get("elec"):
                day_elec_standing_p = _fin["elec"].get("standing_p")
            if _fin.get("gas"):
                day_gas_unit_p     = _fin["gas"].get("unit_p")
                day_gas_standing_p = _fin["gas"].get("standing_p")

        record = {
            "date":                 date_str,
            "month":                date_str[:7],
            "pv_kwh":               round(self.store["pv_daily_kwh"], 2),
            "pv_forecast_kwh":      round(self.latest_forecast_data.get("todayKwh", 0.0), 2),
            "grid_import_kwh":      round(self.store["grid_import_daily_kwh"], 2),
            "grid_export_kwh":      round(self.store["grid_export_daily_kwh"], 2),
            "home_kwh":             round(self.store["home_daily_kwh"], 2),
            "battery_charge_kwh":   round(
                self.latest_inverter_data.get("batteryDailyChargeKwh", 0.0), 2
            ),
            "battery_discharge_kwh": round(
                self.latest_inverter_data.get("batteryDailyDischargeKwh", 0.0), 2
            ),
            "peak_soc":   round(self.store["peak_soc"], 1),
            "min_soc":    round(self.store["min_soc"], 1),
            "tariff":     self.latest_rates_data.get("tariff_info", {}).get("tariff_key", "?"),
            "rate_today_p":   self.latest_rates_data.get("tracker", {}).get("today_p"),
            "export_rate_p":  round(export_rate_p, 4),
            "elec_standing_p_day": day_elec_standing_p,
            "gas_unit_p_day":      day_gas_unit_p,
            "gas_standing_p_day":  day_gas_standing_p,
            "import_events": 1 if self.store.get("had_import_today", False) else 0,
            "export_events": self.store.get("export_count_today", 0),
            "vpp_event":  self.store.get("had_vpp_today", False),
        }

        path    = os.path.join(self.data_dir, "daily_history.json")
        records = []
        try:
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    records = json.load(f)
        except Exception:
            pass

        records.append(record)
        # Retention: keep every record indefinitely (v5.7).  Each record is
        # ~280 bytes; even 50 years of daily data is < 6 MB JSON.  The user
        # explicitly asked to never lose history.  No pruning.

        try:
            _atomic_write_json(path, records)
        except Exception as e:
            log(f"Cannot write daily history: {e}", level="ERROR")

        # Reset daily counters
        self.store["had_import_today"]   = False
        self.store["export_count_today"] = 0
        self.store["had_vpp_today"]      = False

    # ================================================================
    # Data directory backup (weekly, Monday midnight)
    # ================================================================

    def _backup_data_dir(self, date_str):
        """Write a tar.gz of the small JSON / SQLite files in the data dir.

        Cheap protection against accumulator / SoH / daily_history corruption.
        Backups go to a sibling 'data_backup/' folder; only the 8 most recent
        are kept (~2 months of weekly snapshots).
        """
        import tarfile
        files_to_backup = [
            "accumulators.json",
            "daily_history.json",
            "home_load_profile.json",
            "soh_history.json",
            "forecast_accuracy.json",
            "energy_timeseries.db",
            "openmeteo_combined_cache.json",
        ]
        backup_dir = os.path.join(self.data_dir, "data_backup")
        try:
            os.makedirs(backup_dir, exist_ok=True)
        except OSError as exc:
            self.logger.warning(f"[Backup] Cannot create backup dir: {exc}")
            return

        tar_name = f"sigen_data_{date_str}.tar.gz"
        tar_path = os.path.join(backup_dir, tar_name)
        added = 0
        try:
            with tarfile.open(tar_path, "w:gz") as tar:
                for fname in files_to_backup:
                    fpath = os.path.join(self.data_dir, fname)
                    if os.path.exists(fpath):
                        tar.add(fpath, arcname=fname)
                        added += 1
            size_kb = os.path.getsize(tar_path) / 1024.0
            log(f"[Backup] Wrote {tar_name} ({added} files, {size_kb:.1f} KB)")
        except OSError as exc:
            self.logger.warning(f"[Backup] Failed: {exc}")
            return

        # Retention: keep the 8 most recent tarballs.  Sort by filename which
        # embeds an ISO date, so lexicographic sort = chronological.
        try:
            existing = sorted(
                fn for fn in os.listdir(backup_dir)
                if fn.startswith("sigen_data_") and fn.endswith(".tar.gz")
            )
            for fn in existing[:-8]:
                try:
                    os.remove(os.path.join(backup_dir, fn))
                    self.logger.debug(f"[Backup] Pruned old backup {fn}")
                except OSError:
                    pass
        except OSError:
            pass

    # ================================================================
    # Auto-update notifier (GitHub releases)
    # ================================================================

    def _check_for_update(self):
        """Hit the GitHub releases API once per day and log if a newer plugin
        version is available.  Best-effort — silent if offline or rate-limited.

        Stores the last-check timestamp in pluginPrefs so a plugin restart
        doesn't re-query within 24h.  Compares the GitHub tag's leading
        version digits (so 'v5.2' / 'v5.2.0' / '5.2' all parse equivalently).
        """
        last_check_str = self.pluginPrefs.get("lastUpdateCheck", "")
        try:
            last_check = datetime.fromisoformat(last_check_str)
        except (ValueError, TypeError):
            last_check = datetime.min
        if (datetime.now() - last_check).total_seconds() < 86400:
            return   # already checked within the last 24 hours

        import urllib.request
        import urllib.error

        url = ("https://api.github.com/repos/Highsteads/SigenEnergyManager/"
               "releases/latest")
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": f"SigenEnergyManager/{self.pluginVersion}"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                payload = json.loads(resp.read().decode("utf-8", errors="replace"))
        except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError) as exc:
            self.logger.debug(f"[Update] Check failed (silent): {exc}")
            return

        latest_raw = (payload.get("tag_name") or payload.get("name") or "").strip()
        if not latest_raw:
            return

        # Strip a leading 'v' / 'V' and any non-version suffix
        clean = latest_raw.lstrip("vV").split(" ", 1)[0]

        def _parse(s):
            try:
                return tuple(int(x) for x in s.split(".")[:3])
            except (ValueError, AttributeError):
                return ()

        latest_tuple  = _parse(clean)
        current_tuple = _parse(self.pluginVersion)
        if not latest_tuple or not current_tuple:
            return

        if latest_tuple > current_tuple:
            log(
                f"[Update] New plugin version available: {latest_raw} "
                f"(running {self.pluginVersion}). "
                f"See {payload.get('html_url', 'https://github.com/Highsteads/SigenEnergyManager/releases')}"
            )
        else:
            self.logger.debug(
                f"[Update] Plugin is up to date ({self.pluginVersion})"
            )

        self.pluginPrefs["lastUpdateCheck"] = datetime.now().isoformat()

    # ================================================================
    # Battery State-of-Health tracking
    # ================================================================

    def _record_battery_soh_snapshot(self, date_str):
        """Append a weekly SoH snapshot to soh_history.json (data_dir).

        Logs a WARNING if SoH has dropped by more than 2 percentage points in
        the last year, or more than 1 point in the last 4 weeks — either
        indicates faster-than-expected degradation for LFP chemistry and is
        worth investigating (cell imbalance, BMS calibration drift, etc.).
        """
        inv = self.latest_inverter_data or {}
        soh = float(inv.get("batterySoh", 0.0) or 0.0)
        if soh <= 0.0:
            self.logger.debug("[SoH] No batterySoh reading yet — snapshot skipped")
            return

        path = os.path.join(self.data_dir, "soh_history.json")
        records = []
        try:
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    records = json.load(f)
        except (OSError, ValueError):
            records = []

        records.append({"date": date_str, "soh": round(soh, 2)})
        # Keep ~5 years of weekly snapshots (260 entries)
        if len(records) > 260:
            records = records[-260:]
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(records, f)
        except OSError as exc:
            self.logger.warning(f"[SoH] Cannot persist soh_history.json: {exc}")

        # Degradation checks — compare against 4-weeks and 52-weeks ago.
        def _lookup(n_weeks_ago):
            if len(records) < n_weeks_ago + 1:
                return None
            return records[-(n_weeks_ago + 1)].get("soh")

        prev_month = _lookup(4)
        prev_year  = _lookup(52)
        log(f"[SoH] Weekly snapshot: {soh:.1f}% "
            f"(4w ago: {prev_month if prev_month is not None else 'n/a'},  "
            f"52w ago: {prev_year if prev_year is not None else 'n/a'})")
        if prev_month is not None and (prev_month - soh) > 1.0:
            log(
                f"[SoH] WARNING: capacity dropped {prev_month - soh:.1f}% in 4 weeks. "
                f"LFP norm is < 0.1%/4 weeks — investigate BMS / cell balance.",
                level="WARNING",
            )
        if prev_year is not None and (prev_year - soh) > 2.0:
            log(
                f"[SoH] WARNING: capacity dropped {prev_year - soh:.1f}% in 52 weeks. "
                f"Expected LFP degradation is ~0.3%/year — consider raising with installer.",
                level="WARNING",
            )

    # ================================================================
    # Trigger Events
    # ================================================================

    def triggerStartProcessing(self, trigger):
        """Indigo lifecycle: called when a trigger configured against this
        plugin is enabled.  Store the trigger object so _trigger_event() can
        fire it later via indigo.trigger.execute().
        """
        self.event_triggers[trigger.id] = trigger
        if self.debug:
            log(f"[Trigger] Registered: id={trigger.id} type={trigger.pluginTypeId}")

    def triggerStopProcessing(self, trigger):
        """Indigo lifecycle: called when a trigger is disabled or deleted."""
        self.event_triggers.pop(trigger.id, None)
        if self.debug:
            log(f"[Trigger] Unregistered: id={trigger.id} type={trigger.pluginTypeId}")

    def _trigger_event(self, event_id):
        """Fire all registered Indigo triggers whose pluginTypeId matches event_id.

        indigo.trigger.execute() requires a trigger object (NOT a string event ID),
        and indigo.server.fireEvent() does not exist.  We iterate self.event_triggers
        (populated by triggerStartProcessing) and fire every matching one.
        """
        fired = 0
        for trigger in self.event_triggers.values():
            if trigger.pluginTypeId == event_id:
                try:
                    indigo.trigger.execute(trigger)
                    fired += 1
                except Exception as e:
                    log(f"[Trigger] execute failed for {event_id} (id={trigger.id}): {e}", level="ERROR")
        if fired == 0 and self.debug:
            # Not an error — user may simply have no triggers wired to this event.
            log(f"[Trigger] No triggers configured for event '{event_id}'")

    # ================================================================
    # Device State Updates
    # ================================================================

    def _update_inverter_device(self, data):
        """Push Modbus data to sigenergyInverter device."""
        dev = self._find_device("sigenergyInverter")
        if not dev:
            return
        # Numeric states are written as real numbers (not str()) so Indigo's
        # built-in history records them as chartable columns. Coercion is guarded
        # via _as_int/_as_float so a missing/odd Modbus value can never crash the
        # state write. Categorical states (emsWorkMode, gridStatus, etc.) stay str.
        states = [
            {"key": "emsWorkMode",              "value": str(data.get("emsWorkMode", ""))},
            {"key": "gridSensorConnected",      "value": str(data.get("gridSensorConnected", False))},
            {"key": "gridPowerWatts",           "value": _as_int(data.get("gridPowerWatts"), 0)},
            {"key": "gridStatus",               "value": str(data.get("gridStatus", ""))},
            # 0 only on a GENUINE off-grid status — an unmapped "Unknown (N)" read must
            # not chart a false power cut (mirrors the _poll_modbus edge detection).
            {"key": "gridOnline",               "value": 0 if str(data.get("gridStatus", "")).startswith("Off-grid") else 1},
            _num_state("batterySoc",               _as_float(data.get("batterySoc"), 0.0),            1),
            {"key": "pvPowerWatts",             "value": _as_int(data.get("pvPowerWatts"), 0)},
            {"key": "batteryPowerWatts",        "value": _as_int(data.get("batteryPowerWatts"), 0)},
            {"key": "homePowerWatts",           "value": _as_int(data.get("homePowerWatts"), 0)},
            {"key": "plantRunningState",        "value": str(data.get("plantRunningState", ""))},
            _num_state("dischargeCutoffSoc",       _as_float(data.get("dischargeCutoffSoc"), 0.0),    1),
            _num_state("batterySoh",               _as_float(data.get("batterySoh"), 0.0),            1),
            _num_state("batteryTempC",             _as_float(data.get("batteryTempC"), 0.0),          1),
            _num_state("batteryCellVoltage",       _as_float(data.get("batteryCellVoltage"), 0.0),    3),
            _num_state("batteryMaxTempC",          _as_float(data.get("batteryMaxTempC"), 0.0),       1),
            _num_state("batteryMinTempC",          _as_float(data.get("batteryMinTempC"), 0.0),       1),
            _num_state("batteryDailyChargeKwh",    _as_float(data.get("batteryDailyChargeKwh"), 0.0), 2),
            _num_state("batteryDailyDischargeKwh", _as_float(data.get("batteryDailyDischargeKwh"), 0.0), 2),
            _num_state("pvDailyKwh",               self.store["pv_daily_kwh"],          2),
            _num_state("gridDailyImportKwh",       self.store["grid_import_daily_kwh"], 2),
            _num_state("gridDailyExportKwh",       self.store["grid_export_daily_kwh"], 2),
            _num_state("homeDailyKwh",             self.store["home_daily_kwh"],        2),
            {"key": "modbusConnected",          "value": "True"},
            {"key": "lastUpdate",               "value": data.get("lastUpdate", "")},
        ]
        dev.updateStatesOnServer(states)
        dev.updateStateImageOnServer(indigo.kStateImageSel.SensorOn)

    def _update_inverter_device_offline(self):
        """Mark inverter device as offline."""
        dev = self._find_device("sigenergyInverter")
        if dev:
            dev.updateStateOnServer("modbusConnected", value="False")
            dev.updateStateImageOnServer(indigo.kStateImageSel.SensorOff)

    def _update_manager_device(self, decision, snapshot):
        """Push battery manager decision state to batteryManager device."""
        dev = self._find_device("batteryManager")
        if not dev:
            return

        scheduled_str = ""
        if decision.scheduled_time:
            scheduled_str = decision.scheduled_time.strftime("%H:%M")

        action_display = {
            ACTION_SELF_CONSUMPTION: "Self Consumption",
            ACTION_SOLAR_OVERFLOW:   "Solar Overflow Export",
            ACTION_START_IMPORT:     "Grid Import Active",
            ACTION_STOP_IMPORT:      "Import Stopping",
            ACTION_SCHEDULE_IMPORT:  "Import Scheduled",
            ACTION_START_EXPORT:     "Night Export Active",
            ACTION_STOP_EXPORT:      "Export Stopping",
            ACTION_VPP_EXPORT:       "VPP Export Active",
        }.get(decision.action, decision.action)

        # Flood prevention visibility (v4.5)
        flood_target_pct  = self.store.get("flood_prev_target_soc")
        flood_active      = bool(flood_target_pct)

        # Power cut lockout visibility (v4.5)
        lockout_active    = bool(self.store.get("power_cut_lockout_active", False))
        lockout_remain_min = ""
        if lockout_active:
            prt = self.store.get("power_restored_time")
            if prt and isinstance(prt, datetime):
                # Re-promote to UTC if naive
                _prt = prt if prt.tzinfo else prt.replace(tzinfo=timezone.utc)
                _hours_since = (datetime.now(timezone.utc) - _prt).total_seconds() / 3600.0
                _hours_left  = max(0.0, POWER_CUT_LOCKOUT_HOURS - _hours_since)
                lockout_remain_min = str(int(round(_hours_left * 60)))

        # v5.14: Compute tomorrow's solar/need using the *same* logic
        # battery_manager._calculate_24h_balance() uses (lines 405-406 of
        # battery_manager.py) so external scripts that read these states
        # can never disagree with the plugin's flood-prevention gate.
        # The 12-May-2026 incident: optimiser script said "export will
        # happen" (40 kWh vs 11 kWh = 3.6x) but plugin saw 63 kWh vs 22 kWh
        # = 2.81x, just under the 3.0x gate -> no export ran.
        try:
            _local_now = snapshot.now.astimezone(ZoneInfo("Europe/London"))
        except Exception:
            _local_now = snapshot.now
        _tomorrow_weekday  = (_local_now.date() + timedelta(days=1)).weekday()
        _tomorrow_need_kwh = (snapshot.weekend_kwh if _tomorrow_weekday >= 5
                              else snapshot.weekday_kwh)
        _tomorrow_solar_kwh = snapshot.corrected_tomorrow_kwh

        states = [
            {"key": "managerStatus",       "value": "Running" if not self.store["vpp_active"] else "VPP Active"},
            {"key": "currentAction",       "value": action_display},
            # currentMode (List enum) appended below, guarded — see note before
            # the updateStatesOnServer call.
            {"key": "currentReason",       "value": decision.reason[:255]},
            {"key": "dawnViable",          "value": str(decision.dawn_viable)},
            {"key": "socAtDawn",           "value": str(round(decision.soc_at_dawn_kwh, 2))},
            {"key": "importActive",        "value": str(self.store["import_active"])},
            {"key": "importScheduled",     "value": str(bool(self.store["import_scheduled_time"]))},
            {"key": "importScheduledTime", "value": scheduled_str},
            {"key": "importKwh",           "value": str(round(decision.import_kwh, 2))},
            {"key": "exportActive",        "value": str(self.store["export_active"])},
            {"key": "exportKw",            "value": str(round(decision.export_kw, 1))},
            {"key": "floodPrevActive",     "value": str(flood_active)},
            {"key": "floodPrevTarget",     "value": str(flood_target_pct) if flood_active else ""},
            {"key": "powerCutLockoutActive",        "value": str(lockout_active)},
            {"key": "powerCutLockoutRemainingMin",  "value": lockout_remain_min},
            {"key": "tariffActive",        "value": snapshot.tariff.tariff_key},
            {"key": "rateToday",           "value": str(snapshot.tariff.today_rate_p or "")},
            {"key": "rateTomorrow",        "value": str(snapshot.tariff.tomorrow_rate_p or "")},
            {"key": "tomorrowSolarKwh",    "value": round(_tomorrow_solar_kwh, 2)},
            {"key": "tomorrowNeedKwh",     "value": round(_tomorrow_need_kwh, 1)},
            {"key": "lastUpdate",          "value": datetime.now().strftime("%H:%M:%S")},
        ]
        # currentMode is a List-enum registered ASYNCHRONOUSLY after the
        # stateListOrDisplayStateIdChanged() call in deviceStartComm. On the
        # very first evaluate tick right after a plugin restart it may not be
        # registered yet, and writing it then logs a spurious red
        #   device "Battery Manager" state key currentMode not defined
        # ERROR (the rest of the batch still applies, so it's harmless — but it
        # breaks the "no alarming red lines for expected conditions" rule). Only
        # include it once the state actually exists; the next tick (<=60s) writes
        # the real mode, so nothing is lost.
        if "currentMode" in dev.states:
            states.append({"key": "currentMode",
                           "value": ACTION_MODE_TOKEN.get(decision.action, "selfConsumption")})
        dev.updateStatesOnServer(states)

    def _update_forecast_device(self, data):
        """Push Open-Meteo forecast to solarForecast device."""
        dev = self._find_device("solarForecast")
        if not dev:
            return
        states = [
            {"key": "todayKwh",             "value": str(data.get("todayKwh", 0.0))},
            {"key": "tomorrowKwh",          "value": str(data.get("tomorrowKwh", 0.0))},
            {"key": "correctedTodayKwh",    "value": str(data.get("correctedTodayKwh", 0.0))},
            {"key": "correctedTomorrowKwh", "value": str(data.get("correctedTomorrowKwh", 0.0))},
            {"key": "biasFactor",           "value": str(data.get("biasFactor", 1.0))},
            {"key": "remainingTodayKwh",    "value": str(data.get("remainingTodayKwh", 0.0))},
            {"key": "currentHourWatts",     "value": str(data.get("currentHourWatts", 0))},
            {"key": "nextHourWatts",        "value": str(data.get("nextHourWatts", 0))},
            {"key": "forecastStatus",       "value": str(data.get("forecastStatus", ""))},
            {"key": "lastUpdate",           "value": data.get("lastUpdate", "")},
        ]
        dev.updateStatesOnServer(states)

    def _sigenergy_folder_id(self) -> int:
        """Return the Sigenergy variable folder ID, creating the folder if it
        doesn't exist yet (fresh installs would otherwise dump 25+ plugin
        variables at the root of the Variables window).  Falls back to 0
        (root) only if the create itself fails.

        Result is cached on the instance after the first successful lookup so
        the half-hourly variable-write path doesn't re-scan every folder.
        The cache is invalidated when the folder cannot be resolved and again
        on plugin restart (instance is fresh).
        """
        cached = getattr(self, "_sigen_folder_id_cache", None)
        if cached is not None:
            return cached
        result = 0
        try:
            for fid in indigo.variables.folders:
                f = indigo.variables.folders[fid]
                if f.name.lower() in ("sigenergy", "sigen energy", "sigen"):
                    result = f.id
                    break
        except Exception:
            result = 0
        if not result:
            # No existing folder — create it so plugin variables stay grouped.
            try:
                folder = indigo.variables.folder.create("Sigenergy")
                result = folder.id
                log(f"[Energy Vars] Created 'Sigenergy' variable folder (id={result})")
            except Exception as exc:
                self.logger.warning(f"[Energy Vars] Could not create 'Sigenergy' folder: {exc}")
                result = 0
        # Only cache successful hits — a 0 result means the create failed and
        # we want a future call to retry rather than pin variables to root.
        if result:
            self._sigen_folder_id_cache = result
        return result

    def _ensure_var(self, name: str, folder_id: int) -> int:
        """
        Return the Indigo variable ID for `name`, creating it in `folder_id`
        if it does not already exist. Caches the result in self._energy_var_ids.
        """
        if name in self._energy_var_ids:
            return self._energy_var_ids[name]
        # Look up by name
        try:
            v = indigo.variables[name]
            self._energy_var_ids[name] = v.id
            return v.id
        except KeyError:
            pass
        # Create it
        try:
            v = indigo.variable.create(name, value="", folder=folder_id)
            self._energy_var_ids[name] = v.id
            log(f"[Energy Vars] Created variable '{name}' (id={v.id})")
            return v.id
        except Exception as exc:
            log(f"[Energy Vars] Could not create '{name}': {exc}", level="WARNING")
            return 0

    def _init_timeseries_db(self):
        """Create energy_timeseries.db in data_dir if it does not already exist."""
        db_path = os.path.join(self.data_dir, "energy_timeseries.db")
        con = None
        try:
            con = sqlite3.connect(db_path, timeout=5.0)
            con.execute("""
                CREATE TABLE IF NOT EXISTS halfhourly (
                    id               INTEGER PRIMARY KEY AUTOINCREMENT,
                    slot_start       TEXT    NOT NULL UNIQUE,
                    slot_end         TEXT    NOT NULL,
                    grid_import_kwh  REAL    NOT NULL DEFAULT 0.0,
                    grid_export_kwh  REAL    NOT NULL DEFAULT 0.0,
                    pv_kwh           REAL    NOT NULL DEFAULT 0.0,
                    home_kwh         REAL    NOT NULL DEFAULT 0.0,
                    battery_soc_start_pct REAL,
                    battery_soc_end_pct   REAL,
                    battery_net_kwh  REAL,
                    tracker_price_p  REAL,
                    manager_action   TEXT
                )
            """)
            con.commit()
            log(f"[Timeseries] DB ready: {db_path}")
        except sqlite3.Error as exc:
            log(f"[Timeseries] DB init failed: {exc}", level="ERROR")
        finally:
            if con is not None:
                try:
                    con.close()
                except sqlite3.Error:
                    pass

    def _log_halfhourly_to_db(self):
        # v5.45.0: reads/advances store anchors — locked (local sqlite, fast).
        with self._state_lock:
            return self._log_halfhourly_to_db_impl()

    def _log_halfhourly_to_db_impl(self):
        """Append one half-hourly slot to energy_timeseries.db.

        Computes energy deltas since the last write. On the very first call
        (anchors are None) the deltas would span an unknown period so the
        row is skipped and anchors are seeded for the next call instead.
        """
        db_path = os.path.join(self.data_dir, "energy_timeseries.db")
        if not os.path.exists(db_path):
            return

        now_dt   = datetime.now()
        slot_end = now_dt.strftime("%Y-%m-%dT%H:%M:%S")
        slot_start_dt = now_dt - timedelta(seconds=ENERGY_VAR_INTERVAL)
        slot_start = slot_start_dt.strftime("%Y-%m-%dT%H:%M:%S")

        cur_pv     = round(self.store.get("pv_daily_kwh",          0.0), 4)
        cur_import = round(self.store.get("grid_import_daily_kwh", 0.0), 4)
        cur_export = round(self.store.get("grid_export_daily_kwh", 0.0), 4)
        cur_home   = round(self.store.get("home_daily_kwh",        0.0), 4)

        inv_data  = self.latest_inverter_data or {}
        cur_soc   = float(inv_data.get("batterySoc", 0.0))
        cap_kwh   = _as_float(self.pluginPrefs.get("batteryCapacityKwh"), "35.04")

        anchor_pv     = self.store.get("hh_anchor_pv_kwh")
        anchor_import = self.store.get("hh_anchor_import_kwh")
        anchor_export = self.store.get("hh_anchor_export_kwh")
        anchor_home   = self.store.get("hh_anchor_home_kwh")
        anchor_soc    = self.store.get("hh_anchor_soc_pct")

        # Seed anchors on first call — skip writing this slot (unknown period)
        if anchor_pv is None:
            self.store["hh_anchor_pv_kwh"]     = cur_pv
            self.store["hh_anchor_import_kwh"] = cur_import
            self.store["hh_anchor_export_kwh"] = cur_export
            self.store["hh_anchor_home_kwh"]   = cur_home
            self.store["hh_anchor_soc_pct"]    = cur_soc
            return

        # Guard against midnight reset making deltas negative
        delta_pv     = max(0.0, round(cur_pv     - anchor_pv,     4))
        delta_import = max(0.0, round(cur_import - anchor_import,  4))
        delta_export = max(0.0, round(cur_export - anchor_export,  4))
        delta_home   = max(0.0, round(cur_home   - anchor_home,    4))
        battery_net  = round((cur_soc - (anchor_soc or cur_soc)) * cap_kwh / 100.0, 4)

        # Tracker price from tariff monitor device state
        tracker_p = None
        try:
            tariff_dev = self._find_device("tariffMonitor")
            if tariff_dev:
                rate_str = tariff_dev.states.get("rateToday", "")
                if rate_str:
                    tracker_p = float(rate_str)
        except Exception:
            pass

        action = ""
        if self.latest_decision:
            action = str(self.latest_decision.action)

        con = None
        try:
            con = sqlite3.connect(db_path, timeout=5.0)
            con.execute(
                """INSERT OR IGNORE INTO halfhourly
                   (slot_start, slot_end,
                    grid_import_kwh, grid_export_kwh, pv_kwh, home_kwh,
                    battery_soc_start_pct, battery_soc_end_pct, battery_net_kwh,
                    tracker_price_p, manager_action)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (slot_start, slot_end,
                 delta_import, delta_export, delta_pv, delta_home,
                 anchor_soc, cur_soc, battery_net,
                 tracker_p, action)
            )
            con.commit()
        except sqlite3.Error as exc:
            log(f"[Timeseries] Write failed: {exc}", level="ERROR")
        finally:
            if con is not None:
                try:
                    con.close()
                except sqlite3.Error:
                    pass

        # Advance anchors
        self.store["hh_anchor_pv_kwh"]     = cur_pv
        self.store["hh_anchor_import_kwh"] = cur_import
        self.store["hh_anchor_export_kwh"] = cur_export
        self.store["hh_anchor_home_kwh"]   = cur_home
        self.store["hh_anchor_soc_pct"]    = cur_soc

    def _write_energy_summary_variables(self):
        """
        Write today's running energy totals and battery decision to Indigo variables
        in the Sigenergy folder. Called every 30 min and at midnight.
        Variables created automatically if they do not exist.
        """
        try:
            folder_id = self._sigenergy_folder_id()
            with self._state_lock:   # v5.45.0: snapshot the inputs, write unlocked
                pv     = round(self.store.get("pv_daily_kwh",          0.0), 2)
                imp    = round(self.store.get("grid_import_daily_kwh",  0.0), 2)
                exp    = round(self.store.get("grid_export_daily_kwh",  0.0), 2)
                home   = round(self.store.get("home_daily_kwh",         0.0), 2)
                peak   = round(self.store.get("peak_soc",               0.0), 1)
                minsoc = round(self.store.get("min_soc",              100.0), 1)
            # Self-sufficiency = share of home load NOT met by grid import. On a
            # zero-load day (home=0) nothing was needed from the grid, so that's 100%
            # — matches the dashboard's get_dashboard_data computation (was 0.0 here,
            # an inconsistency between the Indigo variable and the dashboard).
            sself  = (round(max(0.0, (1 - imp / home) * 100), 1)
                      if home > 0 else 100.0)

            decision = self.latest_decision
            action   = str(decision.action)  if decision else ""
            reason   = str(decision.reason)  if decision else ""

            updates = [
                ("sigen_today_pv_kwh",       str(pv)),
                ("sigen_today_import_kwh",   str(imp)),
                ("sigen_today_export_kwh",   str(exp)),
                ("sigen_today_home_kwh",     str(home)),
                ("sigen_today_self_suff_pct", str(sself)),
                ("sigen_today_peak_soc",     str(peak)),
                ("sigen_today_min_soc",      str(minsoc)),
                ("sigen_decision_action",    action),
                ("sigen_decision_reason",    reason),
            ]
            for var_name, value in updates:
                var_id = self._ensure_var(var_name, folder_id)
                if var_id:
                    try:
                        indigo.variable.updateValue(var_id, value)
                    except Exception as exc:
                        log(f"[Energy Vars] Update failed '{var_name}': {exc}",
                            level="WARNING")
            # v5.41: republish the Octopus cost/rate variables from the bill-exact
            # ledger + economics (they had no active writer and went stale).
            self._write_cost_variables(folder_id)
        except Exception as exc:
            log(f"[Energy Vars] _write_energy_summary_variables failed: {exc}",
                level="WARNING")

    def _cost_vars_economics(self):
        """Minimal economics for _write_cost_variables: whole-house today +
        month periods ONLY. get_dashboard_data() builds the entire dashboard
        payload (including three parses of the ever-growing daily_history.json)
        and was being invoked inside the _state_lock-holding tick every ~30
        minutes just for these two blocks (v5.43).
        Rate resolution mirrors get_dashboard_data's three-tier fallback.
        """
        rates   = self.latest_rates_data or {}
        tracker = rates.get("tracker", {})
        export_rate_p = DEFAULT_EXPORT_RATE_P
        try:
            rates_export = float(rates.get("export_rate_p", 0.0))
            if rates_export > 0:
                export_rate_p = rates_export
        except (TypeError, ValueError):
            pass
        import_rate_p = None
        try:
            r = float(tracker.get("today_p") or 0.0)
            if r > 0:
                import_rate_p = r
        except (TypeError, ValueError):
            pass
        if import_rate_p is None:
            tariff_dev = self._find_device("tariffMonitor")
            if tariff_dev:
                try:
                    r = float(tariff_dev.states.get("rateToday", "") or 0.0)
                    if r > 0:
                        import_rate_p = r
                except (TypeError, ValueError):
                    pass
        if import_rate_p is None:
            try:
                if "elec_unit_rate_p" in indigo.variables:
                    r = float(indigo.variables["elec_unit_rate_p"].value or 0.0)
                    if r > 0:
                        import_rate_p = r
            except (TypeError, ValueError, KeyError):
                pass
        periods = self._period_economics_summary(
            export_rate_p          = export_rate_p,
            fallback_import_rate_p = import_rate_p,
        )
        try:
            whole_house = self._whole_house_summary(
                import_rate_p = import_rate_p,
                export_rate_p = export_rate_p,
            )
        except Exception as exc:
            self.logger.debug(f"[WholeHouse] summary failed: {exc}")
            whole_house = None
        return {"whole_house": whole_house, "periods": periods}

    def _write_cost_variables(self, folder_id):
        """v5.41: republish the orphaned Octopus cost/rate variables.

        elec_*/gas_*/export_*/account_balance lost their writer when the old
        Octopus consumption script was retired, so they froze (elec_unit_rate_p
        weeks behind the live Tracker rate, account_balance_gbp stuck at 0).
        weekly_home_digest.py and get_dashboard_data's import-rate fallback both
        read elec_unit_rate_p, so keeping it live here makes this the single
        source of truth. Rates + balance come from get_account_financials (the
        Kraken ledger — already cached, no duplicate fetch); today/month costs
        from the live economics (no recompute → no drift). Fully guarded so a
        Kraken/economics hiccup leaves the values in place rather than blanking
        them, and never disturbs the sigen_* writes that ran before it.
        """
        updates = []
        # ---- bill-exact rates + balance (Kraken ledger, cached ~30 min) ----
        try:
            fin = self.octopus.get_account_financials() if self.octopus else None
        except Exception as exc:
            fin = None
            log(f"[Cost Vars] financials fetch failed: {exc}", level="WARNING")
        if fin:
            elec = fin.get("elec") or {}
            gas  = fin.get("gas") or {}
            exp  = fin.get("export") or {}
            if elec.get("unit_p") is not None:
                updates.append(("elec_unit_rate_p", f"{float(elec['unit_p']):.4f}"))
            if elec.get("standing_p") is not None:
                updates.append(("elec_standing_charge_p", f"{float(elec['standing_p']):.4f}"))
            if gas.get("unit_p") is not None:
                updates.append(("gas_unit_rate_p", f"{float(gas['unit_p']):.4f}"))
            if gas.get("standing_p") is not None:
                updates.append(("gas_standing_charge_p", f"{float(gas['standing_p']):.4f}"))
            if exp.get("unit_p") is not None:
                rp = f"{float(exp['unit_p']):.4f}"
                updates.append(("export_rate_p", rp))
                updates.append(("export_rate",   rp))   # legacy alias kept for any old consumer (digest now reads export_rate_p)
            if fin.get("balance_gbp") is not None:
                updates.append(("account_balance_gbp", f"{float(fin['balance_gbp']):.2f}"))
        # ---- today/month costs from the live economics (single source, no recompute) ----
        try:
            econ = self._cost_vars_economics() or {}
            wh   = (econ.get("whole_house") or {}).get("today") or {}
            mon  = (econ.get("periods") or {}).get("month") or {}

            def _add(name, val):
                if val is not None:
                    try:
                        updates.append((name, f"{float(val):.2f}"))
                    except (TypeError, ValueError):
                        pass
            _add("elec_today_cost_gbp",       wh.get("electric_gbp"))
            _add("gas_today_cost_gbp",        wh.get("gas_gbp"))
            _add("export_today_revenue_gbp",  wh.get("export_gbp"))
            _add("combined_today_actual_gbp", wh.get("bill_gbp"))
            # Companion flag: the today card's gas component is an ESTIMATE
            # (most recent settled day's kWh at current rates) until Octopus
            # settles the day — surface that to variable consumers, who can't
            # see the API payload's provisional/gas_estimated flags.
            if wh.get("provisional") is not None:
                updates.append(("combined_today_is_provisional",
                                "true" if wh.get("provisional") else "false"))
            if mon.get("days") == 0:
                # A successful history read with no rows yet this month: the
                # month genuinely starts at zero. (Rows are written at the
                # FOLLOWING midnight, so the whole 1st of each month has none —
                # skipping here kept the closed month's totals showing all day.)
                updates.append(("elec_month_cost_gbp",     "0.00"))
                updates.append(("export_month_revenue_gbp", "0.00"))
            else:
                # Whole-house basis (unit + standing from settled rows) so the
                # month figure matches elec_today_cost_gbp's basis; falls back
                # to the unit-only aggregate for pre-settle installs.
                _add("elec_month_cost_gbp",
                     mon.get("elec_whole_house_total_gbp")
                     if mon.get("elec_whole_house_total_gbp") is not None
                     else mon.get("import_total_gbp"))
                _add("export_month_revenue_gbp", mon.get("export_total_gbp"))
        except Exception as exc:
            log(f"[Cost Vars] economics read failed: {exc}", level="WARNING")
        # ---- write ----
        if not updates:
            return
        for name, value in updates:
            vid = self._ensure_var(name, folder_id)
            if vid:
                try:
                    indigo.variable.updateValue(vid, value)
                except Exception as exc:
                    log(f"[Cost Vars] update '{name}': {exc}", level="WARNING")
        elec_rate = next((v for n, v in updates if n == "elec_unit_rate_p"), "—")
        # Log-on-change only: on Tracker the rate changes once a day, so the
        # ~48 identical heartbeat lines this produced were pure noise. The
        # routine confirmation drops to debug (Indigo's toggle, not log()).
        if elec_rate != self.store.get("cost_vars_last_logged_rate"):
            self.store["cost_vars_last_logged_rate"] = elec_rate
            log(f"[Cost Vars] published {len(updates)} Octopus cost/rate variables "
                f"(elec unit rate {elec_rate}p)")
        else:
            self.logger.debug(
                f"[Cost Vars] published {len(updates)} Octopus cost/rate variables "
                f"(elec unit rate {elec_rate}p)")

    def _update_tariff_device(self, tariff_info, monitored):
        """Push Octopus tariff data to tariffMonitor device."""
        dev = self._find_device("tariffMonitor")
        if not dev:
            return

        active_key = tariff_info.get("tariff_key", TARIFF_TRACKER)
        tracker    = monitored.get("tracker",  {})
        go         = monitored.get("go",       {})
        flux       = monitored.get("flux",     {})
        flexible   = monitored.get("flexible", {})

        # rateToday: show the active tariff's actual unit rate
        if active_key == TARIFF_FLEXIBLE:
            active_rate_today    = str(flexible.get("today_p", ""))
            active_rate_tomorrow = ""                                 # flat rate — no tomorrow
        else:
            active_rate_today    = str(tracker.get("today_p", ""))
            active_rate_tomorrow = str(tracker.get("tomorrow_p") or "")

        states = [
            {"key": "tariffActive",        "value": tariff_info.get("display_name", "")},
            {"key": "rateToday",           "value": active_rate_today},
            {"key": "rateTomorrow",        "value": active_rate_tomorrow},
            {"key": "trackerRateToday",    "value": str(tracker.get("today_p", ""))},
            {"key": "trackerRateTomorrow", "value": str(tracker.get("tomorrow_p") or "")},
            {"key": "goOffPeakRate",       "value": str(go.get("cheap_p", ""))},
            {"key": "goStandardRate",      "value": str(go.get("standard_p", ""))},
            {"key": "goPeakRate",          "value": str(go.get("peak_p", ""))},
            {"key": "fluxOffPeakRate",     "value": str(flux.get("cheap_p", ""))},
            {"key": "fluxStandardRate",    "value": str(flux.get("standard_p", ""))},
            {"key": "fluxPeakRate",        "value": str(flux.get("peak_p", ""))},
            {"key": "flexibleRate",        "value": str(flexible.get("today_p", ""))},
            {"key": "lastUpdate",          "value": datetime.now().strftime("%H:%M:%S")},
        ]
        dev.updateStatesOnServer(states)

    def _vpp_event_str(self):
        """The announced window as "19:00-20:00", or "" when there is none.

        Consumers append this to a sentence ("VPP event announced: …"), so an
        empty string must read as "nothing to add" rather than "unknown" — the
        callers already guard on falsiness. A window on another day is prefixed
        with its date; today's stays short, because that is the common case and
        the whole point is a glanceable line on a phone.

        Times go through _local_time so they match the device states and the
        log, all of which are Europe/London — the stored event is UTC-aware and
        printing that raw would read an hour early through BST.
        """
        event = self.store.get("vpp_event") or {}
        start = event.get("start_time")
        end   = event.get("end_time")
        if not start or not end:
            return ""
        try:
            window = f"{_local_time(start)}-{_local_time(end)}"
            return window if _local_time(start, "%Y-%m-%d") == _local_today_str() \
                else f"{_local_time(start, '%d/%m')} {window}"
        except Exception:
            # A malformed stored event must not take the whole status payload
            # down — every caller treats "" as "say nothing".
            return ""

    def _update_vpp_device(self):
        """Push VPP state to axleVppMonitor device."""
        dev = self._find_device("axleVppMonitor")
        if not dev:
            return

        event         = self.store.get("vpp_event") or {}
        start_str     = ""
        end_str       = ""
        duration_hrs  = 0.0

        if event.get("start_time"):
            start_str    = _local_time(event["start_time"], "%H:%M %d/%m")
            end_str      = _local_time(event["end_time"])
            duration_hrs = event.get("duration_hrs", 0.0)

        max_export_kw = _as_float(self.pluginPrefs.get("maxExportKw"), 4.0)
        vpp_rate      = _as_float(self.pluginPrefs.get("axleVppRatePerKwh"), 1.00)  # GBP/kWh, configurable (default £1)
        earnings_est  = round(max_export_kw * duration_hrs * vpp_rate, 2)

        # Feed health. Without this the device reads a calm "Standby" whether the
        # API is healthy and quiet or rejecting every call (see _record_vpp_api_status).
        api_error = self.store.get("vpp_api_error")
        last_ok   = self.store.get("vpp_api_last_ok")
        last_ok_s = datetime.fromtimestamp(last_ok).strftime("%H:%M %d/%m") if last_ok else "never"

        states = [
            {"key": "apiStatus",         "value": api_error or "OK"},
            {"key": "apiLastOk",         "value": last_ok_s},
            {"key": "vppStatus",         "value": "Active" if self.store["vpp_active"] else "Standby"},
            {"key": "vppState",          "value": self.store["vpp_state"]},
            {"key": "eventStartTime",    "value": start_str},
            {"key": "eventEndTime",      "value": end_str},
            {"key": "preChargeRequired", "value": str(self.store["vpp_pre_charge_soc"])},
            {"key": "estimatedEarnings", "value": str(earnings_est)},
            {"key": "vppLastExportKwh",  "value": str(round(self.store.get("vpp_last_export_kwh", 0.0), 2))},
            {"key": "lastUpdate",        "value": datetime.now().strftime("%H:%M:%S")},
        ]
        dev.updateStatesOnServer(states)

    # ================================================================
    # Action dialog pre-population (Indigo getActionConfigUiValues)
    # ================================================================

    def getActionConfigUiValues(self, plugin_props, type_id, dev_id):
        """Pre-fill action dialogs with live values where useful.

        For Force Grid Import: defaults the target SOC to current SOC + 20%
        (clamped to 95%), so the slider opens at a sensible "top-up by ~20%"
        position rather than a static 80%.

        For Force Grid Export: defaults the kW field to the configured DNO
        export limit (4 kW by default) rather than a hardcoded 4.0.

        Any user-saved values in plugin_props take precedence — we only
        write a key when the user hasn't already set it.
        """
        values = indigo.Dict(plugin_props) if plugin_props else indigo.Dict()
        errors = indigo.Dict()
        try:
            inv = self.latest_inverter_data or {}
            soc = float(inv.get("batterySoc", 0.0))
            if type_id == "forceGridImport":
                if not values.get("targetSocPct"):
                    suggested = int(min(95, max(20, round(soc + 20))))
                    values["targetSocPct"] = str(suggested)
                if not values.get("powerKw"):
                    values["powerKw"] = str(
                        _as_float(self.pluginPrefs.get("inverterMaxKw"), "10.0")
                    )
            elif type_id == "forceExport":
                # powerKw is the discharge HEADROOM (grid export is DNO-capped) —
                # default it to the inverter max so the dialog opens at full export.
                if not values.get("powerKw"):
                    values["powerKw"] = str(
                        _as_float(self.pluginPrefs.get("inverterMaxKw"), "10.0")
                    )
        except Exception as exc:
            self.logger.debug(f"getActionConfigUiValues fallback: {exc}")
        return (values, errors)

    # ================================================================
    # Indigo Action Callbacks
    # ================================================================

    def actionForceGridImport(self, action):
        """Action: Force immediate grid import. Clamps to the bounds the ConfigUI
        labels promise (power 0..inverterMaxKw, target SOC 10..100%)."""
        with self._state_lock:
            props      = action.props
            inv_max_kw = _as_float(self.pluginPrefs.get("inverterMaxKw"), 10.0)
            power_kw   = min(max(0.0, _as_float(props.get("powerKw"), inv_max_kw)), inv_max_kw)
            target_soc = min(max(10.0, _as_float(props.get("targetSocPct"), 80.0)), 100.0)
            log(f"[Action] Force grid import: {power_kw:.1f}kW to {target_soc:.0f}% SOC")
            cutoff = min(target_soc + 3.0, 100.0)
            if self.modbus and self.modbus.force_charge(int(power_kw * 1000),
                                                        cutoff_soc=cutoff):
                self.store["import_active"]     = True
                self.store["import_target_soc"] = target_soc
                self.store["export_active"]     = False
                self._set_import_cutoff(cutoff)

    def actionForceExport(self, action):
        """Action: Force immediate grid export at the DNO cap (test).

        night_export sets the battery DISCHARGE limit; grid export itself is capped
        automatically by the inverter's commissioned DNO limit (no grid-export
        setpoint is written). So the powerKw field sets the discharge HEADROOM —
        default = inverter max, which exports the full DNO allocation; a lower value
        limits how hard the battery can discharge (grid export then = headroom minus
        house load). The field used to be ignored entirely (always inverter max)."""
        with self._state_lock:
            inv_max_kw  = _as_float(self.pluginPrefs.get("inverterMaxKw"), 10.0)
            power_kw    = min(max(0.0, _as_float(action.props.get("powerKw"), inv_max_kw)),
                              inv_max_kw)
            discharge_w = int(power_kw * 1000)
            log(f"[Action] Force export: discharge limit {power_kw:.1f}kW "
                f"(grid export auto-capped at the DNO limit)")
            if self.modbus and self.modbus.night_export(discharge_w):
                self.store["export_active"]  = True
                self.store["import_active"]  = False

    def actionForceDaytimeExport(self, action):
        """Action: Force PV-first grid export (mode 0x05) for hardware validation.

        Lets us confirm the inverter's real 0x05 behaviour before trusting it in
        an unattended VPP window: with PV above the DNO cap the grid should hold
        ~4 kW sourced from PV (battery flat / charging from surplus); below the
        cap the battery should top the export up to 4 kW. Watch home_status /
        the inverter device states after firing. Reverts via Set Self-Consumption
        (or the manager's next tick) — pause the manager first to hold it.
        """
        with self._state_lock:
            inv_max_w = int(_as_float(self.pluginPrefs.get("inverterMaxKw"), 10.0) * 1000)
            log("[Action] Force daytime export: mode 0x05 (Discharge PV First) — test")
            if self.modbus and self.modbus.daytime_export(inv_max_w):
                self.store["export_active"] = True
                self.store["import_active"] = False

    def actionForceVppExportTest(self, action):
        """Action: exercise the live VPP export driver (auto bank vs discharge) for testing.

        Forces a daytime VPP context and runs _drive_vpp_export() once against the
        current live PV/home, so the inverter picks the same sub-mode it would in a
        real daytime event: bank-surplus (mode 0x02 + charge cap) when PV exceeds the
        export target, or discharge (0x05) when it doesn't. Pause the manager first to
        hold it; restore with Set Self-Consumption then Resume Battery Manager.
        """
        with self._state_lock:
            self.store["vpp_is_daytime"]        = True
            self.store["vpp_export_submode"]    = None
            self.store["vpp_bank_charge_cap_w"] = -1
            log("[Action] Force VPP export drive (test) — daytime context; live PV "
                "decides bank vs discharge")
            self._drive_vpp_export()

    def actionSetSelfConsumption(self, action):
        """Action: Return to self-consumption mode."""
        with self._state_lock:
            log("[Action] Set self-consumption mode")
            if self.modbus:
                self.modbus.set_self_consumption()
                self.store["import_active"] = False
                self.store["export_active"] = False

    def actionReturnToLocalEms(self, action):
        """Action: Disable Remote EMS and return to local inverter control."""
        with self._state_lock:
            log("[Action] Return to local EMS control")
            if self.modbus:
                self.modbus.return_to_local()
                self.store["import_active"] = False
                self.store["export_active"] = False

    def _set_manager_paused(self, paused, source):
        """Single entry point for pause/resume (Pause/Resume actions + the
        sigen_manager_paused variable).  Caller must hold self._state_lock.

        On PAUSE we hand the inverter back to self-consumption — the same safe
        baseline prepare_to_sleep() uses — so a latched force-charge or
        force-export cannot keep importing/exporting under a "Paused" label
        (the self-sufficiency KPI must never lose to a dead control).  The
        manager evaluate is then gated in _evaluate_manager while paused.
        On RESUME we force an immediate re-evaluation on the next tick.
        """
        was_paused = self.store.get("manager_paused", False)
        self.store["manager_paused"] = paused
        dev = self._find_device("batteryManager")
        if dev:
            dev.updateStateOnServer(
                "managerStatus", value="Paused" if paused else "Running"
            )
        # Mirror into the sigen_manager_paused variable — it is the ONLY thing
        # startup seeds the paused flag from, so an action-initiated pause that
        # never wrote it silently resumed inverter control after any restart
        # (and the variable disagreed with the device label throughout). The
        # variableUpdated value-compare guard makes this converge, not loop.
        try:
            var_id = self._ensure_var("sigen_manager_paused",
                                      self._sigenergy_folder_id())
            if var_id:
                indigo.variable.updateValue(var_id,
                                            "true" if paused else "false")
        except Exception as exc:
            log(f"[Pause] Could not mirror pause state to variable: {exc}",
                level="WARNING")
        if paused:
            # Hand the inverter back to the safe baseline AND release any raised
            # discharge-cutoff floor (flood-prev / storm / VPP) + stand down any
            # in-flight VPP export — the same disengage prepare_to_sleep() uses.
            # Without the cutoff release a 'Paused' manager could leave the battery
            # locked above a raised SOC and silently force grid import for the whole
            # pause. import_active/export_active are cleared inside the helper.
            if self.modbus and self.modbus.connected:
                log(f"[Pause] Manager paused ({source}) — inverter returned to "
                    f"self-consumption and raised floors released; holding hands-off "
                    f"until resumed.")
            else:
                log(f"[Pause] Manager paused ({source}) — modbus offline, inverter "
                    f"left as-is; raised-floor flags cleared.")
            self._disengage_to_safe_baseline("Pause")
        elif was_paused:
            # Resume: re-evaluate now rather than waiting up to MANAGER_EVAL_INTERVAL.
            self.store["last_manager"] = 0.0
            log(f"[Pause] Manager resumed ({source}) — re-evaluating immediately.")

    def actionPauseManager(self, action):
        """Action: Pause battery manager (hands the inverter back to
        self-consumption, then stops the manager acting — see
        _set_manager_paused)."""
        with self._state_lock:
            self._set_manager_paused(True, "Pause action")

    def actionResumeManager(self, action):
        """Action: Resume battery manager."""
        with self._state_lock:
            self._set_manager_paused(False, "Resume action")

    def actionRefreshForecast(self, action):
        """Action: Manual solar forecast refresh (Open-Meteo)."""
        # v5.45.0: unlocked — the fetch runs outside the lock by design and
        # the stamp write is a scalar dict op.
        log("[Action] Manual solar forecast refresh")
        self._refresh_forecast(force=True)
        self.store["last_forecast"] = time.time()

    def actionRefreshOctopus(self, action):
        """Action: Manual Octopus rates refresh."""
        # v5.45.0: unlocked — see actionRefreshForecast.
        log("[Action] Manual Octopus rates refresh")
        self._refresh_octopus_rates(force=True)
        self.store["last_octopus"] = time.time()

    # ================================================================
    # Indigo Menu Callbacks
    # ================================================================

    def menuRefreshAll(self):
        """Menu: Force refresh solar forecast + Octopus + re-evaluate manager.

        Runs under _state_lock like every sibling action callback — unlocked it
        raced the background tick, running the manager (store mutation + Modbus
        writes) concurrently on two threads.
        """
        log("[Menu] Refresh All: fetching solar forecast, Octopus and re-evaluating...")
        # v5.45.0: no outer lock — the fetches run unlocked by design and
        # _evaluate_manager is self-locking, so holding the lock here would
        # just stall callbacks behind two HTTP fetches for no protection.
        self._refresh_forecast(force=True)
        self.store["last_forecast"] = time.time()
        self._refresh_octopus_rates(force=True)
        self.store["last_octopus"] = time.time()
        self._evaluate_manager()
        self.store["last_manager"] = time.time()
        log("[Menu] Refresh All complete")
        return True

    def menuShowStatus(self):
        """Menu: Log current manager status to event log."""
        from datetime import datetime as _dt
        now_str  = _dt.now().strftime("%H:%M:%S")
        capacity = _as_float(self.pluginPrefs.get("batteryCapacityKwh"), 35.04)

        inv   = self._find_device("sigenergyInverter")
        mgr   = self._find_device("batteryManager")
        fcast = self._find_device("solarForecast")
        tarif = self._find_device("tariffMonitor")

        log("[Status] ======= Live Status: " + now_str + " =======")

        # --- Battery ---
        if inv:
            soc_pct  = float(inv.states.get("batterySoc", 0))
            soc_kwh  = soc_pct / 100.0 * capacity
            batt_w   = int(inv.states.get("batteryPowerWatts", 0))
            modbus   = inv.states.get("modbusConnected", "False")
            if batt_w > 50:
                batt_str = f"Charging {batt_w}W"
            elif batt_w < -50:
                batt_str = f"Discharging {abs(batt_w)}W"
            else:
                batt_str = "Idle"
            log(f"[Status] Battery:  {soc_pct:.1f}% SOC  |  {soc_kwh:.1f} kWh stored  |  {batt_str}"
                f"  |  Modbus: {'OK' if modbus == 'True' else 'OFFLINE'}")
        else:
            log("[Status] Battery:  No inverter device found", level="WARNING")

        # --- Solar & Grid & Home ---
        # Forecast reads depend only on fcast — hoisted out of the `if inv:`
        # block so the Tomorrow line at the bottom can't hit an unassigned
        # fcst_tmrw when a forecast device exists but no inverter does.
        # Use remainingTodayKwh (now -> dusk), NOT correctedTodayKwh
        # (whole-day forecast) — same double-count bug as the Today
        # Energy Summary, fixed here at the same time.
        fcst_remain  = fcast.states.get("remainingTodayKwh", "?") if fcast else "?"
        fcst_tmrw    = fcast.states.get("correctedTomorrowKwh", "?") if fcast else "?"
        if inv:
            pv_w    = int(inv.states.get("pvPowerWatts", 0))
            grid_w  = int(inv.states.get("gridPowerWatts", 0))
            home_w  = int(inv.states.get("homePowerWatts", 0))
            ems     = inv.states.get("emsWorkMode", "Unknown")
            grid_str = f"Exporting {abs(grid_w)}W" if grid_w < -50 else (
                       f"Importing {grid_w}W" if grid_w > 50 else "Idle (grid)")
            pv_today   = self.store.get("pv_daily_kwh", 0.0)
            imp_today  = self.store.get("grid_import_daily_kwh", 0.0)
            exp_today  = self.store.get("grid_export_daily_kwh", 0.0)
            home_today = self.store.get("home_daily_kwh", 0.0)
            try:
                remain_val          = float(fcst_remain)
                fcst_expected_total = round(pv_today + max(0.0, remain_val), 1)
                fcst_remain_str     = f"{remain_val:.1f}"
            except (ValueError, TypeError):
                fcst_expected_total = "?"
                fcst_remain_str     = str(fcst_remain)
            log(f"[Status] Solar:    {pv_w}W now  |  {pv_today:.2f} kWh today"
                f"  |  {fcst_remain_str} kWh forecast remaining  |  {fcst_expected_total} kWh expected total")
            log(f"[Status] Grid:     {grid_str}  |  Import today {imp_today:.2f} kWh"
                f"  |  Export today {exp_today:.2f} kWh")
            log(f"[Status] Home:     {home_w}W now  |  {home_today:.2f} kWh today"
                f"  |  EMS mode: {ems}")

        # --- Tariff ---
        if tarif:
            t_name = tarif.states.get("tariffActive", "?")
            t_rate = tarif.states.get("rateToday", "?")
            t_tmrw = tarif.states.get("rateTomorrow", "?")
            log(f"[Status] Tariff:   {t_name}  |  Today {t_rate}p/kWh  |  Tomorrow {t_tmrw}p/kWh")

        # --- Manager decision ---
        if mgr:
            action   = mgr.states.get("currentAction", "unknown")
            reason   = mgr.states.get("currentReason", "")
            viable   = mgr.states.get("dawnViable", "?")
            soc_dawn = mgr.states.get("socAtDawn", "?")
            sched    = mgr.states.get("importScheduledTime", "")
            log(f"[Status] Manager:  {action}  |  {reason}")
            log(f"[Status] Dawn:     Viable={viable}  |  SOC at dawn {soc_dawn} kWh"
                + (f"  |  Import scheduled {sched}" if sched else ""))

        # --- Tomorrow ---
        if fcast:
            log(f"[Status] Tomorrow: {fcst_tmrw} kWh solar forecast")

        log("[Status] =============================================")
        return True

    def menuShowDailyHistory(self):
        """Menu: Log last 7 days from daily_history.json."""
        path = os.path.join(self.data_dir, "daily_history.json")
        if not os.path.exists(path):
            log("[History] No daily_history.json found yet", level="WARNING")
            return True

        try:
            with open(path, "r", encoding="utf-8") as f:
                records = json.load(f)
        except Exception as e:
            log(f"[History] Cannot read daily_history.json: {e}", level="ERROR")
            return True

        recent = records[-7:] if len(records) >= 7 else records
        log(f"[History] Last {len(recent)} days:")
        for r in reversed(recent):
            import_flag = " IMPORT" if r.get("import_events", 0) > 0 else ""
            export_flag = f" exports={r['export_events']}" if r.get("export_events", 0) > 0 else ""
            vpp_flag    = " VPP" if r.get("vpp_event") else ""
            log(
                f"  {r['date']}  PV={r.get('pv_kwh', 0):.1f}kWh "
                f"(fcst={r.get('pv_forecast_kwh', 0):.1f}) "
                f"Import={r.get('grid_import_kwh', 0):.2f} "
                f"Export={r.get('grid_export_kwh', 0):.2f} "
                f"Home={r.get('home_kwh', 0):.1f} "
                f"SOC {r.get('min_soc', 0):.0f}-{r.get('peak_soc', 0):.0f}%"
                f"{import_flag}{export_flag}{vpp_flag}"
            )
        return True

    def menuShowTariffRates(self):
        """Menu: Log current Tracker/Go/Flux rates from cached Octopus data."""
        rates = self.latest_rates_data
        if not rates:
            log("[Tariff] No rates cached yet — run Refresh All first", level="WARNING")
            return True

        tariff_info = rates.get("tariff_info", {})
        tracker     = rates.get("tracker", {})
        go          = rates.get("go", {})
        flux        = rates.get("flux", {})

        flexible = rates.get("flexible", {})

        log(f"[Tariff] Active tariff: {tariff_info.get('display_name', '?')} "
            f"({tariff_info.get('tariff_key', '?')})")

        # Flexible Octopus (flat rate — no TOU windows)
        if flexible.get("today_p") is not None:
            log(f"[Tariff] Flexible: {flexible['today_p']:.2f}p/kWh (flat rate, no cheap window)")

        # Tracker (shown if available, e.g. when user is on Tracker or monitoring it)
        today_p    = tracker.get("today_p")
        tomorrow_p = tracker.get("tomorrow_p")
        if today_p is not None:
            log(f"[Tariff] Tracker today: {today_p:.2f}p/kWh" +
                (f"  tomorrow: {tomorrow_p:.2f}p/kWh" if tomorrow_p is not None else
                 "  tomorrow: not yet published"))
        else:
            log("[Tariff] Tracker: not available (suspended or not active)")

        if go:
            log(f"[Tariff] Go: off-peak={go.get('cheap_p', '?')}p "
                f"({go.get('cheap_start', '?')}-{go.get('cheap_end', '?')}) "
                f"standard={go.get('standard_p', '?')}p")
        if flux:
            log(f"[Tariff] Flux: off-peak={flux.get('cheap_p', '?')}p "
                f"({flux.get('cheap_start', '?')}-{flux.get('cheap_end', '?')}) "
                f"peak={flux.get('peak_p', '?')}p "
                f"standard={flux.get('standard_p', '?')}p")
        return True

    def menuShowVppStatus(self):
        """Menu: Log current VPP state and next event details."""
        state   = self.store.get("vpp_state", "idle")
        active  = self.store.get("vpp_active", False)
        event   = self.store.get("vpp_event") or {}
        log(f"[VPP] State={state} Active={'YES' if active else 'no'}")
        if event:
            start = event.get("start_time")
            end   = event.get("end_time")
            start_s = _local_time(start, "%H:%M %d/%m") if start else "?"
            end_s   = _local_time(end)                  if end   else "?"
            log(f"[VPP] Next event: {start_s} - {end_s} BST "
                f"({event.get('duration_hrs', 0):.1f}h) "
                f"precharge={self.store.get('vpp_pre_charge_soc', 0):.0f}%")
        else:
            log("[VPP] No event scheduled")
        if not self.axle:
            log("[VPP] Axle API not configured", level="WARNING")
        return True

    def menuShowVppExport(self):
        """Menu: Log VPP export summary for today."""
        axle_enabled = self.pluginPrefs.get("axleEnabled", False)
        state        = self.store.get("vpp_state", "idle")
        active       = self.store.get("vpp_active", False)
        last_export  = self.store.get("vpp_last_export_kwh", 0.0)
        had_vpp      = self.store.get("had_vpp_today", False) or active

        log("[VPP] ============ VPP Export Summary ============")
        log(f"[VPP] Axle enabled:        {'YES' if axle_enabled else 'NO'}")
        log(f"[VPP] Axle token:          {'configured' if self.axle else 'not set'}")
        log(f"[VPP] Current state:       {state}")
        log(f"[VPP] Event active:        {'YES - Axle in control' if active else 'No'}")

        if active:
            ongoing = (self.store["grid_export_daily_kwh"]
                       - self.store.get("vpp_export_start_kwh", 0.0))
            log(f"[VPP] Export so far:       {ongoing:.2f} kWh  (event in progress)")
        elif had_vpp:
            log(f"[VPP] Last event export:   {last_export:.2f} kWh")
        else:
            log("[VPP] Last event export:   No VPP event recorded today")

        dev = self._find_device("axleVppMonitor")
        if dev:
            start = dev.states.get("eventStartTime", "")
            end   = dev.states.get("eventEndTime", "")
            earn  = dev.states.get("estimatedEarnings", "")
            chg   = dev.states.get("preChargeRequired", "0")
            if start:
                log(f"[VPP] Event window:        {start} - {end}")
                log(f"[VPP] Est. earnings:       GBP {earn}")
                if float(chg) > 0:
                    log(f"[VPP] Pre-charge target:   {chg}% SOC")
                else:
                    log("[VPP] Pre-charge:          Not needed (SOC sufficient)")

        log("[VPP] =============================================")
        return True

    def menuShowTodaySummary(self):
        """Menu: Log a human-readable summary of today's energy data."""
        today   = datetime.now().strftime("%d-%b-%Y")
        pv      = self.store.get("pv_daily_kwh", 0.0)
        imp     = self.store.get("grid_import_daily_kwh", 0.0)
        exp     = self.store.get("grid_export_daily_kwh", 0.0)
        home    = self.store.get("home_daily_kwh", 0.0)
        peak    = self.store.get("peak_soc", 0.0)
        low     = self.store.get("min_soc", 100.0)
        vpp_exp = self.store.get("vpp_last_export_kwh", 0.0)
        had_vpp = self.store.get("had_vpp_today", False) or self.store.get("vpp_active", False)

        inv         = self._find_device("sigenergyInverter")
        current_soc = float(inv.states.get("batterySoc", 0)) if inv else 0.0
        ems_mode    = inv.states.get("emsWorkMode", "Unknown") if inv else "Unknown"
        pv_now      = inv.states.get("pvPowerWatts", "0") if inv else "0"
        grid_now    = inv.states.get("gridPowerWatts", "0") if inv else "0"

        mgr      = self._find_device("batteryManager")
        action   = mgr.states.get("currentAction", "Unknown") if mgr else "Unknown"
        reason   = mgr.states.get("currentReason", "") if mgr else ""
        viable   = mgr.states.get("dawnViable", "?") if mgr else "?"
        soc_dawn = mgr.states.get("socAtDawn", "?") if mgr else "?"

        fcast         = self._find_device("solarForecast")
        # correctedTodayKwh is the WHOLE-day bias-corrected forecast.  For the
        # "remaining today" line we need remainingTodayKwh which the forecast
        # module computes as the sum from now -> dusk.  Using
        # correctedTodayKwh here caused a real double-count bug surfaced on
        # 12-May-2026 (87.8 kWh expected total for a 14.25 kWp array — physically
        # impossible).  fcst_today retained for compatibility but not used in
        # the summary line below.
        fcst_today    = fcast.states.get("correctedTodayKwh",  "?") if fcast else "?"  # noqa: F841 — retained for compatibility (see comment above)
        fcst_remain   = fcast.states.get("remainingTodayKwh", "?") if fcast else "?"
        fcst_tmrw     = fcast.states.get("correctedTomorrowKwh", "?") if fcast else "?"

        tariff  = self._find_device("tariffMonitor")
        t_name  = tariff.states.get("tariffActive", "?") if tariff else "?"
        t_rate  = tariff.states.get("rateToday", "") if tariff else ""
        t_tmrw  = tariff.states.get("rateTomorrow", "") if tariff else ""

        import_note = "  (self-sufficient - no grid draw)" if imp < 0.05 else ""
        export_note = f"  (VPP contribution: {vpp_exp:.2f} kWh)" if had_vpp and vpp_exp > 0 else ""
        rate_str    = (f" at {t_rate}p/kWh" if t_rate else "")
        tmrw_str    = (f"  |  tomorrow: {t_tmrw}p" if t_tmrw else "  |  tomorrow: TBD")

        log(f"[Today] ======= Energy Summary: {today} =======")
        try:
            remaining_val  = float(fcst_remain)
            expected_total = round(pv + max(0.0, remaining_val), 1)
            remain_str     = f"{remaining_val:.1f}"
        except (ValueError, TypeError):
            expected_total = "?"
            remain_str     = str(fcst_remain)
        log(f"[Today] Solar generation:    {pv:.2f} kWh  (+{remain_str} kWh remaining = {expected_total} kWh expected total)")
        log(f"[Today] Home consumption:    {home:.2f} kWh")
        log(f"[Today] Grid import:         {imp:.2f} kWh{import_note}")
        log(f"[Today] Grid export:         {exp:.2f} kWh{export_note}")
        log(f"[Today] Battery SOC now:     {current_soc:.0f}%  "
            f"(peak {peak:.0f}%,  low {low:.0f}%)")
        log(f"[Today] EMS mode:            {ems_mode}")
        log(f"[Today] Manager action:      {action}")
        if reason:
            log(f"[Today] Reason:              {reason}")
        log(f"[Today] Dawn viability:      {viable}  |  SOC at dawn: {soc_dawn} kWh")
        log(f"[Today] Tariff:              {t_name}{rate_str}{tmrw_str}")
        log(f"[Today] Tomorrow forecast:   {fcst_tmrw} kWh solar expected")
        log(f"[Today] Live:                PV {pv_now} W  |  Grid {grid_now} W")
        if had_vpp:
            vpp_state = self.store.get("vpp_state", "idle")
            if self.store.get("vpp_active"):
                ongoing = exp - self.store.get("vpp_export_start_kwh", 0.0)
                log(f"[Today] VPP:                 ACTIVE ({ongoing:.2f} kWh exported so far)")
            else:
                log(f"[Today] VPP:                 Completed  ({vpp_exp:.2f} kWh exported)  "
                    f"state: {vpp_state}")
        log("[Today] =============================================")
        return True

    def menuToggleDebug(self):
        """Menu: Toggle debug logging on/off."""
        self.debug = not self.debug
        self.pluginPrefs["showDebugInfo"] = self.debug
        state = "ENABLED" if self.debug else "disabled"
        log(f"[Menu] Debug logging {state}")
        return True

    def menuSelfTest(self):
        """Menu: Run a quick health check across every subsystem.

        Logs a single block summarising secrets resolution, Modbus connect,
        Octopus auth, Open-Meteo reachability and Axle auth.  Each line is
        an explicit OK/FAIL so configuration problems surface without log
        digging.  Read-only — no side effects beyond a fresh forecast/rates
        fetch and a Modbus connect check.
        """
        # Estate convention: every diagnostic menu dumps the full plugin-info
        # banner first, so one log block carries environment AND results
        # (same extras as showPluginInfo — one source of truth).
        if log_startup_banner:
            log_startup_banner(
                self.pluginId, self.pluginDisplayName, self.pluginVersion,
                extras=[("Timestamps in Log:", "ON" if self.timestamp_enabled else "OFF")])
        log("=" * 56)
        log("Plugin self-test")
        log("=" * 56)

        # Secrets
        secrets_status = []
        for name, value in (
            ("OCTOPUS_API_KEY",       OCTOPUS_API_KEY),
            ("OCTOPUS_ACCOUNT",       OCTOPUS_ACCOUNT),
            ("OCTOPUS_MPAN",          OCTOPUS_MPAN),
            ("OCTOPUS_SERIAL",        OCTOPUS_SERIAL),
            ("OCTOPUS_EXPORT_MPAN",   OCTOPUS_EXPORT_MPAN),
            ("OCTOPUS_EXPORT_SERIAL", OCTOPUS_EXPORT_SERIAL),
            ("OCTOPUS_GAS_MPRN",      OCTOPUS_GAS_MPRN),
            ("OCTOPUS_GAS_SERIAL",    OCTOPUS_GAS_SERIAL),
            ("AXLE_API_KEY",          AXLE_API_KEY),
            ("PUSHOVER_USER_TOKEN",   PUSHOVER_USER_TOKEN),
            ("SIGENERGY_IP",        SIGENERGY_IP),
            ("LATITUDE",            SITE_LATITUDE),
            ("LONGITUDE",           SITE_LONGITUDE),
        ):
            secrets_status.append(
                f"  {name:<22}: {'SET' if value not in (None, '') else 'missing (using PluginConfig or default)'}"
            )
        log("Secrets (from IndigoSecrets.py):")
        for line in secrets_status:
            log(line)

        # Modbus
        log("Modbus inverter:")
        if not self.modbus:
            log("  FAIL — Modbus not initialised (no inverter IP configured)",
                level="ERROR")
        else:
            try:
                # Under _state_lock: connect() closes and replaces the shared
                # socket, which must not interleave with an in-flight tick read.
                with self._state_lock:
                    ok = bool(self.modbus.connected) or self.modbus.connect()
                if ok and self.modbus.connected:
                    inv = self.latest_inverter_data or {}
                    soc = inv.get("batterySoc", "unknown")
                    log(f"  OK — connected, SOC {soc}%")
                else:
                    log("  FAIL — cannot connect to inverter", level="ERROR")
            except Exception as exc:
                log(f"  FAIL — {exc}", level="ERROR")

        # Octopus
        log("Octopus API:")
        if not self.octopus or not self.octopus.api_key:
            log("  SKIP — no Octopus API key configured")
        else:
            try:
                t = self.octopus.get_current_tariff(force=False)
                if t:
                    log(f"  OK — tariff {t.get('display_name', 'unknown')} "
                        f"({t.get('tariff_code', '')})")
                else:
                    log("  FAIL — could not detect tariff (auth or network)",
                        level="ERROR")
            except Exception as exc:
                log(f"  FAIL — {exc}", level="ERROR")

        # Open-Meteo
        log("Open-Meteo forecast:")
        try:
            data = self.latest_forecast_data
            if data and data.get("correctedTomorrowKwh") is not None:
                log(f"  OK — tomorrow {data.get('correctedTomorrowKwh', 0):.1f} kWh "
                    f"(cache age {data.get('cache_age_hours', 0):.1f}h)")
            else:
                log("  WARN — no forecast cached yet — try Menu > Refresh All Data Now",
                    level="WARNING")
        except Exception as exc:
            log(f"  FAIL — {exc}", level="ERROR")

        # Axle
        log("Axle VPP:")
        if not self.axle:
            log("  SKIP — Axle integration disabled or no API key")
        else:
            try:
                event = self.axle.get_next_event()
                if event:
                    start = _local_time(event["start_time"], "%H:%M %d/%m")
                    log(f"  OK — next event {start} ({event.get('import_export')})")
                else:
                    log("  OK — connected, no event scheduled")
            except Exception as exc:
                log(f"  FAIL — {exc}", level="ERROR")

        # Pushover
        log("Pushover plugin:")
        try:
            po = indigo.server.getPlugin("io.thechad.indigoplugin.pushover")
            if po and po.isEnabled():
                user = self._resolve_pushover_user()
                log(f"  OK — plugin enabled, user token "
                    f"{'set' if user else 'NOT SET'}")
            else:
                log("  WARN — Pushover plugin not enabled (storm/VPP alerts skipped)",
                    level="WARNING")
        except Exception as exc:
            log(f"  WARN — {exc}", level="WARNING")

        log("=" * 56)
        log("Self-test complete.")
        return True

    def menuOpenDashboard(self):
        """Menu: Open the unified Dashboards hub in the default browser.

        Routes to the Dashboards plugin's index.html via IWS rather than the
        legacy internal mini-dashboard on WEB_DASHBOARD_PORT. The internal
        dashboard server still runs (Sigen-only Sankey/charts) but the menu
        now lands on the unified hub, which links to Sigen / Heating / etc.
        Falls back to the legacy URL if INDIGO_URL is not configured.

        Note: this opens the browser on the Indigo SERVER. If the Indigo
        client is running on a different Mac, the dashboard will appear on
        the server's screen, not the client's. The URL (without api-key) is
        also logged so it can be clicked from the event log on any client.
        """
        if INDIGO_URL:
            base = INDIGO_URL.rstrip("/")
            url_log = f"{base}/com.clives.indigoplugin.dashboards/static/pages/index.html"
            api_key = INDIGO_API_KEY or CLAUDEBRIDGE_BEARER_TOKEN
            url_open = f"{url_log}?api-key={api_key}" if api_key else url_log
        else:
            host = self._resolve_dashboard_host()
            url_log  = f"http://{host}:{WEB_DASHBOARD_PORT}/"
            url_open = url_log
        log(f"[Menu] Dashboards: {url_log}")
        try:
            import webbrowser
            opened = webbrowser.open(url_open, new=2)
            if not opened:
                log("[Menu] Could not auto-open browser — open the URL above manually",
                    level="WARNING")
        except Exception as exc:
            log(f"[Menu] Browser launch failed ({exc}) — open the URL above manually",
                level="WARNING")
        return True

    def menuShowPowerCutLog(self):
        """Menu: Show the last 20 grid-status transitions from the in-memory log."""
        events = self.store.get("power_cut_events", []) or []
        if not events:
            log("[PowerCut] No grid outages have been observed since plugin start.")
            return True
        log("=" * 56)
        log("Power cut log — last 20 grid-status transitions")
        log("=" * 56)
        for ev in events[-20:]:
            log(f"  {ev}")
        log("=" * 56)
        return True

    # ================================================================
    # Plugin Preferences Callback
    # ================================================================

    def validatePrefsConfigUi(self, values_dict):
        """Enforce the dawnSocTarget >= 15% floor AT SAVE TIME so the user sees
        the constraint in the dialog — previously a saved 10-14% was silently
        bumped back to 15 by a startup migration on the next restart, with the
        XML default (10) contradicting the enforced minimum the whole time."""
        errors = indigo.Dict()
        raw = str(values_dict.get("dawnSocTarget", "")).strip()
        if raw:
            try:
                if float(raw) < 15.0:
                    errors["dawnSocTarget"] = (
                        "Minimum 15% — keeps a real buffer above the battery "
                        "health floor on poor solar days.")
            except ValueError:
                errors["dawnSocTarget"] = "Enter a number (percent, 15-100)."
        # Cheap sanity checks on the most damaging numeric fields — a typo here
        # otherwise only surfaces later as a silent _as_float/_as_int fallback.
        raw = str(values_dict.get("modbusPort", "")).strip()
        if raw:
            try:
                if not (1 <= int(raw) <= 65535):
                    errors["modbusPort"] = "Enter a TCP port between 1 and 65535 (Sigenergy default is 502)."
            except ValueError:
                errors["modbusPort"] = "Enter a whole number (Sigenergy default is 502)."
        raw = str(values_dict.get("inverterMaxKw", "")).strip()
        if raw:
            try:
                if not (0.5 <= float(raw) <= 100.0):
                    errors["inverterMaxKw"] = "Enter the inverter rating in kW (0.5-100, e.g. 10.0)."
            except ValueError:
                errors["inverterMaxKw"] = "Enter a number (kW, e.g. 10.0)."
        raw = str(values_dict.get("batteryCapacityKwh", "")).strip()
        if raw:
            try:
                if not (1.0 <= float(raw) <= 1000.0):
                    errors["batteryCapacityKwh"] = "Enter the battery capacity in kWh (1-1000, e.g. 35.04)."
            except ValueError:
                errors["batteryCapacityKwh"] = "Enter a number (kWh, e.g. 35.04)."
        if errors:
            return (False, values_dict, errors)
        return (True, values_dict)

    def closedPrefsConfigUi(self, values_dict, user_cancelled):
        if user_cancelled:
            return
        self.debug = values_dict.get("showDebugInfo", False)
        log("[Prefs] Plugin preferences updated - reinitialising modules")
        # Under _state_lock: _init_modules disconnects and REPLACES self.modbus
        # while the background tick may be mid-read on the old client. The RLock
        # means the dialog close simply waits for the current tick to finish.
        with self._state_lock:
            self._init_modules()
            # Re-publish the shared site config so the optimiser script picks up
            # any tariff / battery / inverter changes immediately rather than
            # waiting for the next plugin restart.
            self._write_site_config()

    # ================================================================
    # Initialisation Helpers
    # ================================================================

    def _init_modules(self):
        """Initialise all module instances from current preferences."""
        prefs = self.pluginPrefs

        # Resolve credentials: IndigoSecrets.py wins over PluginConfig
        api_key    = OCTOPUS_API_KEY or prefs.get("octopusApiKey", "")
        account_id = OCTOPUS_ACCOUNT or prefs.get("octopusAccount", "")
        mpan       = OCTOPUS_MPAN    or prefs.get("octopusMpan", "")
        serial     = OCTOPUS_SERIAL  or prefs.get("octopusSerial", "")
        region     = prefs.get("octopusRegion", "F")
        # Export MPAN/serial (Octopus Outgoing) — used by v5.19 export-sync check.
        # Optional: if blank, the export-sync feature is silently disabled.
        self.export_mpan   = OCTOPUS_EXPORT_MPAN   or prefs.get("octopusExportMpan", "")
        self.export_serial = OCTOPUS_EXPORT_SERIAL or prefs.get("octopusExportSerial", "")
        self.gas_mprn      = OCTOPUS_GAS_MPRN       or prefs.get("octopusGasMprn", "")
        self.gas_serial    = OCTOPUS_GAS_SERIAL     or prefs.get("octopusGasSerial", "")
        # Gas m3->kWh calorific factor (override on the bill's exact figure).
        try:
            gas_kwh_per_m3 = float(prefs.get("gasKwhPerM3", "") or 0.0)
        except (TypeError, ValueError):
            gas_kwh_per_m3 = 0.0
        if gas_kwh_per_m3 <= 0:
            gas_kwh_per_m3 = GAS_KWH_PER_M3

        axle_key   = AXLE_API_KEY or prefs.get("axleApiKey", "")

        # Solar forecast (Open-Meteo — all 4 arrays, no API key needed).
        # Initialised before Modbus so that downstream callers (startup, etc.)
        # always have a forecast object available even if the Modbus IP is not
        # yet configured.
        # Site coordinates: IndigoSecrets.py first (LATITUDE / LONGITUDE),
        # then PluginConfig (siteLatitude / siteLongitude), then None — there
        # is NO built-in default.  If both are unset the forecast feature is
        # skipped with a clear ERROR.  Array specs remain in source for v5 —
        # a per-array UI is on the v6 roadmap.
        # Uses the module-level _as_float (handles a None fallback correctly).
        site_lat = SITE_LATITUDE  if SITE_LATITUDE  is not None else _as_float(prefs.get("siteLatitude"),  None)
        site_lon = SITE_LONGITUDE if SITE_LONGITUDE is not None else _as_float(prefs.get("siteLongitude"), None)
        # Optional per-array JSON override.  Strict shape check — every entry
        # must declare all 5 required keys (name, tilt, azimuth, kwp, shade)
        # with the right types.  Bad JSON falls back to the module default
        # ARRAYS and is logged at ERROR level so the user notices.
        arrays_override = None
        arrays_raw = (prefs.get("siteArraysJson") or "").strip()
        if arrays_raw:
            try:
                parsed = json.loads(arrays_raw)
                if not isinstance(parsed, list) or not parsed:
                    raise ValueError("expected a non-empty JSON list")
                cleaned = []
                for i, entry in enumerate(parsed):
                    if not isinstance(entry, dict):
                        raise ValueError(f"array #{i+1} is not an object")
                    for key in ("name", "tilt", "azimuth", "kwp", "shade"):
                        if key not in entry:
                            raise ValueError(f"array #{i+1} missing '{key}'")
                    cleaned.append({
                        "name":    str(entry["name"]),
                        "tilt":    float(entry["tilt"]),
                        "azimuth": float(entry["azimuth"]),
                        "kwp":     float(entry["kwp"]),
                        "shade":   float(entry["shade"]),
                    })
                arrays_override = cleaned
                log(f"[Config] Loaded {len(cleaned)} PV array(s) from PluginConfig "
                    f"siteArraysJson override.")
            except (ValueError, TypeError) as exc:
                log(
                    f"[Config] siteArraysJson parse failed ({exc}) — falling back "
                    f"to built-in default ARRAYS. Fix the JSON in PluginConfig.",
                    level="ERROR",
                )
        if site_lat is None or site_lon is None:
            log(
                "[Config] No site coordinates configured. Set LATITUDE / "
                "LONGITUDE in IndigoSecrets.py OR fill in siteLatitude / "
                "siteLongitude under Plugins → Sigenergy Manager → Configure. "
                "Solar forecast feature is disabled until both are set.",
                level="ERROR",
            )
            self.forecast = None
        else:
            self.forecast = OpenMeteoForecast(
                data_dir=self.data_dir,
                logger=self.logger,
                latitude=site_lat,
                longitude=site_lon,
                arrays=arrays_override,
            )

        # Octopus
        self.octopus = OctopusAPI(
            api_key=api_key,
            account_id=account_id,
            mpan=mpan,
            serial=serial,
            region=region,
            data_dir=self.data_dir,
            logger=self.logger,
            gas_mprn=self.gas_mprn,
            gas_serial=self.gas_serial,
            export_mpan=self.export_mpan,
            export_serial=self.export_serial,
            gas_kwh_per_m3=gas_kwh_per_m3,
            gas_unit=(self.pluginPrefs.get("gasMeterUnit") or "m3"),
        )

        # Axle VPP. Pass our own logger — AxleAPI's private fallback logger has no
        # handler, so its errors go nowhere (a revoked token hid for six weeks).
        self.axle = AxleAPI(api_token=axle_key, logger=self.logger) if axle_key else None

        # Inverter IP: IndigoSecrets.py wins over PluginConfig.  If neither is set,
        # log an ERROR and skip Modbus init only — the rest of the plugin can
        # still run for development/diagnostic use.
        inv_ip     = SIGENERGY_IP or prefs.get("inverterIp", "")
        if not inv_ip:
            log(
                "[Config] No inverter IP configured. Set SIGENERGY_IP in IndigoSecrets.py "
                "or fill in 'Inverter IP address' under Plugins -> Sigenergy Manager -> "
                "Configure. Modbus connection will not start until this is set.",
                level="ERROR",
            )
            log(
                f"[Init] Modbus=NOT CONFIGURED, "
                f"Octopus={'OK' if api_key else 'not configured'}, "
                f"Solar=Open-Meteo (4 arrays), "
                f"Axle={'OK' if axle_key else 'disabled'}"
            )
            return
        inv_port   = _as_int(prefs.get("modbusPort"), 502)
        # Modbus poll interval — read from pluginPrefs (v5.9), clamped to a
        # safe range. Sigenergy spec allows ~1s; 5s gives plenty of headroom.
        try:
            self.modbus_poll_s = max(5, min(600, int(prefs.get("pollInterval",
                                                               MODBUS_POLL_INTERVAL))))
        except (TypeError, ValueError):
            self.modbus_poll_s = MODBUS_POLL_INTERVAL
        log(f"[Init] Modbus poll interval: {self.modbus_poll_s}s")
        plant_addr = _as_int(prefs.get("plantAddress"), 247)
        inv_addr   = _as_int(prefs.get("inverterSlaveId"), 1)

        # Modbus
        if self.modbus:
            self.modbus.disconnect()
        # Pass self.sleep so the Modbus 1-second throttle between reads can be
        # interrupted by StopThread during plugin shutdown. Without this,
        # read_all() can block the plugin thread for ~16s and Indigo hard-kills
        # the plugin if it doesn't respond to StopThread within ~10s.
        self.modbus = SigenergyModbus(
            ip=inv_ip, port=inv_port,
            plant_address=plant_addr, inverter_address=inv_addr,
            logger=self.logger,
            sleep_func=self.sleep,
        )
        # Startup Modbus initialisations — connect once for all startup writes.
        # HOLD_ESS_MAX_DISCHARGE (40034) persists across mode changes on the inverter.
        # A previous force_discharge() call may have left a low limit that caps battery
        # output even in self-consumption mode. Always reset to full inverter capacity.
        if self.modbus.connect():
            inverter_max_w = int(_as_float(prefs.get("inverterMaxKw"), 10.0) * 1000)
            self.modbus.set_discharge_limit(inverter_max_w)   # clear any stale discharge cap
            self.modbus.set_charge_limit(inverter_max_w)      # clear any stale charge cap
            self.modbus.set_charge_cutoff(100.0)              # ensure unrestricted charging
            if prefs.get("exportEnabled", False):
                dno_startup_w = int(_as_float(prefs.get("maxExportKw"), 4.0) * 1000)
                self.modbus.set_export_limit(dno_startup_w)

        log(
            f"[Init] Modbus={inv_ip}:{inv_port}, "
            f"Octopus={'OK' if api_key else 'not configured'}, "
            f"Solar=Open-Meteo (4 arrays), "
            f"Axle={'OK' if axle_key else 'disabled'}"
        )

    def _get_data_dir(self):
        """Return plugin data directory path (create if needed)."""
        data_dir = indigo.server.getInstallFolderPath()
        data_dir = os.path.join(data_dir, "Preferences", "Plugins",
                                "com.clives.indigoplugin.sigenergy-energy-manager")
        if not os.path.exists(data_dir):
            os.makedirs(data_dir)
        return data_dir

    def _find_device(self, type_id):
        """Find the first enabled device of a given typeId."""
        for dev in indigo.devices.iter("self"):
            if dev.deviceTypeId == type_id and dev.enabled:
                return dev
        return None

    # ================================================================
    # Accumulator Persistence
    # ================================================================

    def _save_accumulators(self):
        """Save daily accumulators to disk (survives plugin reload).

        Best-effort throughout — called from control paths (flood-target and
        lockout changes) that must never fail because a save couldn't run.
        """
        try:
            path = os.path.join(self.data_dir, "accumulators.json")
        except Exception as e:
            self.logger.warning(f"Cannot save accumulators: {e}")
            return
        # v5.45.0: build + write under the lock so the saved snapshot is
        # internally consistent (RLock — locked callers nest fine; the
        # atomic file write is local and fast).
        with self._state_lock:
            self._save_accumulators_locked(path)

    def _save_accumulators_locked(self, path):
        """Build + write the accumulators payload. Caller holds the lock."""
        data = {
            "pv_daily_kwh":              self.store["pv_daily_kwh"],
            "grid_import_daily_kwh":     self.store["grid_import_daily_kwh"],
            "grid_export_daily_kwh":     self.store["grid_export_daily_kwh"],
            "home_daily_kwh":            self.store["home_daily_kwh"],
            "peak_soc":                  self.store["peak_soc"],
            "min_soc":                   self.store["min_soc"],
            "peak_pv_w":                 self.store.get("peak_pv_w", 0),
            "peak_pv_time":              self.store.get("peak_pv_time", ""),
            "today_date":                self.store["today_date"],
            "pv_lifetime_start_kwh":     self.store["pv_lifetime_start_kwh"],
            "import_lifetime_start_kwh": self.store["import_lifetime_start_kwh"],
            "export_lifetime_start_kwh": self.store["export_lifetime_start_kwh"],
            # Storm state is NOT day-specific (a warning can span midnight) — persist it
            # so a restart during an active warning doesn't re-send the Pushover.
            "storm_alerted_level":       self.store.get("storm_alerted_level", "none"),
            "storm_level":               self.store.get("storm_level", "none"),
            # Restart-critical control state. These also live in pluginPrefs,
            # but runtime pref writes only reach .indiPref on a GRACEFUL
            # shutdown — a crash or hard-kill (plausible in exactly the
            # power-cut scenario the lockout exists for) lost both. This file
            # is written on every change, so it is the authoritative copy.
            "flood_prev_target_soc":     self.store.get("flood_prev_target_soc") or 0,
            "power_restored_time":       self.pluginPrefs.get("powerRestoredTime", ""),
            # Outage state is NOT day-specific either — persist it so a plugin
            # restart mid-outage doesn't re-send the 'grid LOST' alert or
            # under-report the RESTORED duration.
            "power_cut_started_at":      (self.store["power_cut_started_at"].isoformat()
                                          if self.store.get("power_cut_started_at") else ""),
            "power_cut_events":          list(self.store.get("power_cut_events", [])),
            # Axle VPP window — restart-critical control state. A window can span
            # midnight, so it is restored regardless of day. Written on every
            # transition (see _vpp_transition) so a restart or crash mid-window
            # resumes WITHOUT relying on Axle still returning the active event —
            # the endpoint may drop it once live (Predbat issue #3051).
            "vpp_state":                 self.store.get("vpp_state", VPP_IDLE),
            "vpp_event":                 (_serialise_vpp_event(self.store.get("vpp_event"))
                                          if self.store.get("vpp_state", VPP_IDLE) != VPP_IDLE
                                          else None),
            "vpp_pre_charge_soc":        self.store.get("vpp_pre_charge_soc", 0.0),
            "vpp_export_start_kwh":      self.store.get("vpp_export_start_kwh", 0.0),
            "vpp_charge_stopped":        self.store.get("vpp_charge_stopped", False),
            "vpp_cutoff_raised":         self.store.get("vpp_cutoff_raised", False),
            "vpp_is_daytime":            self.store.get("vpp_is_daytime", False),
            "vpp_export_active":         (self.store.get("export_active", False)
                                          if self.store.get("vpp_state", VPP_IDLE) != VPP_IDLE
                                          else False),
        }
        try:
            # Atomic — rewritten every 5 minutes; a crash/power blip mid-write
            # must not truncate the file and silently reset the day's totals.
            _atomic_write_json(path, data)
        except Exception as e:
            self.logger.warning(f"Cannot save accumulators: {e}")
        self._save_home_profile()

    def _load_accumulators(self):
        """Load daily accumulators from disk on startup."""
        path = os.path.join(self._get_data_dir(), "accumulators.json")
        if not os.path.exists(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            # Storm state is restored regardless of day (a warning can span midnight),
            # so a restart during an active warning won't re-send the Pushover.
            if data.get("storm_alerted_level"):
                self.store["storm_alerted_level"] = data.get("storm_alerted_level")
            if data.get("storm_level"):
                self.store["storm_level"] = data.get("storm_level")
            # Restart-critical control state, restored regardless of day.
            # pluginPrefs copies (written by the same paths) are kept as a
            # fallback for installs upgrading from before v5.43 — startup()'s
            # pref rehydration runs after this and only fills gaps.
            _flood = data.get("flood_prev_target_soc")
            if _flood:
                try:
                    self.store["flood_prev_target_soc"] = float(_flood)
                    self.store["export_active"]         = True
                    self.logger.debug(
                        f"Flood pre-drain target {float(_flood):.0f}% restored "
                        f"from accumulators (crash-safe copy)")
                except (TypeError, ValueError):
                    pass
            # Outage state, restored regardless of day — a restart mid-outage
            # must not re-send the 'grid LOST' alert or reset the duration.
            _pcs = data.get("power_cut_started_at")
            if _pcs:
                try:
                    self.store["power_cut_started_at"] = datetime.fromisoformat(_pcs)
                    self.logger.debug(
                        f"Outage-in-progress marker {_pcs} restored from accumulators")
                except (TypeError, ValueError):
                    pass
            if data.get("power_cut_events"):
                self.store["power_cut_events"] = list(data["power_cut_events"])[-100:]
            # Axle VPP window, restored regardless of day (a window can span
            # midnight). Only the raw fields are restored here — the time-based
            # resume/cleanup decision is deferred to _rehydrate_vpp_state() in
            # startup(), where modbus is available for any hardware cleanup.
            _vpp_state = data.get("vpp_state", VPP_IDLE)
            if _vpp_state and _vpp_state != VPP_IDLE:
                _vpp_event = _deserialise_vpp_event(data.get("vpp_event"))
                if _vpp_event is not None:
                    self.store["vpp_state"]            = _vpp_state
                    self.store["vpp_event"]            = _vpp_event
                    self.store["vpp_active"]           = (_vpp_state == VPP_ACTIVE)
                    self.store["vpp_pre_charge_soc"]   = data.get("vpp_pre_charge_soc", 0.0)
                    self.store["vpp_export_start_kwh"] = data.get("vpp_export_start_kwh", 0.0)
                    self.store["vpp_charge_stopped"]   = bool(data.get("vpp_charge_stopped", False))
                    self.store["vpp_cutoff_raised"]    = bool(data.get("vpp_cutoff_raised", False))
                    self.store["vpp_is_daytime"]       = bool(data.get("vpp_is_daytime", False))
                    if data.get("vpp_export_active"):
                        self.store["export_active"] = True
                    self.logger.debug(
                        f"VPP {_vpp_state} window restored from accumulators (crash-safe copy)")
            _prt = data.get("power_restored_time")
            if _prt and not self.pluginPrefs.get("powerRestoredTime", ""):
                # _resolve_export_lockout reads the PREF — reseed it so the
                # 4h lockout survives a non-graceful stop. It self-expires.
                self.pluginPrefs["powerRestoredTime"]  = _prt
                self.store["power_cut_lockout_active"] = True
            today = _local_today_str()   # Europe/London, matches the save/midnight basis
            if data.get("today_date") == today:
                # Same day — restore accumulators and lifetime anchors
                self.store["pv_daily_kwh"]              = data.get("pv_daily_kwh", 0.0)
                self.store["grid_import_daily_kwh"]     = data.get("grid_import_daily_kwh", 0.0)
                self.store["grid_export_daily_kwh"]     = data.get("grid_export_daily_kwh", 0.0)
                self.store["home_daily_kwh"]            = data.get("home_daily_kwh", 0.0)
                self.store["peak_soc"]                  = data.get("peak_soc", 0.0)
                self.store["min_soc"]                   = data.get("min_soc", 100.0)
                self.store["peak_pv_w"]                 = data.get("peak_pv_w", 0)
                self.store["peak_pv_time"]              = data.get("peak_pv_time", "")
                self.store["today_date"]                = today
                # Restore lifetime anchors so delta computation continues correctly
                self.store["pv_lifetime_start_kwh"]     = data.get("pv_lifetime_start_kwh")
                self.store["import_lifetime_start_kwh"] = data.get("import_lifetime_start_kwh")
                self.store["export_lifetime_start_kwh"] = data.get("export_lifetime_start_kwh")
                self.logger.debug("Restored daily accumulators from disk")
        except Exception as e:
            self.logger.warning(f"Cannot load accumulators: {e}")

    # -------------------------------------------------------------------------
    # Menu handlers
    # -------------------------------------------------------------------------

    def showPluginInfo(self, valuesDict=None, typeId=None):
        extras = [("Timestamps in Log:", "ON" if self.timestamp_enabled else "OFF")]
        if log_startup_banner:
            log_startup_banner(self.pluginId, self.pluginDisplayName, self.pluginVersion, extras=extras)
        else:
            indigo.server.log(f"{self.pluginDisplayName} v{self.pluginVersion}")
            for label, value in extras:
                indigo.server.log(f"  {label} {value}")

    def menuToggleTimestamps(self, valuesDict=None, typeId=None):
        self.timestamp_enabled = not self.timestamp_enabled
        self.pluginPrefs["timestampEnabled"] = self.timestamp_enabled
        if self._ts_filter:
            self._ts_filter.enabled = self.timestamp_enabled
        state = "ON" if self.timestamp_enabled else "OFF"
        indigo.server.log(f"[{self.pluginDisplayName}] Timestamps in Log -> {state}")
        return True
