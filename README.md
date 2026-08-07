# SigenEnergyManager

**Indigo home automation plugin for Sigenergy solar / battery systems.**

A self-sufficiency-first battery manager: every 60 seconds it reads the inverter
over Modbus TCP, projects battery SOC at the next dawn against a half-hourly
home-consumption profile, and picks the inverter mode that keeps the most kWh
in the battery for the longest time. Grid import is never used unless the
battery genuinely cannot reach the configured minimum SOC by next sunrise.

## What it does

**Battery management (`battery_manager.py`)**
- 24-hour sufficiency model — projects dawn SOC using a 48-slot half-hourly
  consumption profile (auto-calibrated from live inverter data) and the
  Open-Meteo solar forecast
- Tariff-aware import scheduling — auto-detects the active Octopus tariff
  (Tracker / Go / iGo / Flux / iFlux / Agile) and defers import to the
  cheapest available window when one exists
- Seasonal resilience buffer — 10 % overnight floor April–September,
  20 % October–March (configurable)
- Daytime solar-overflow export — caps the battery's `HOLD_ESS_MAX_CHARGE`
  register once SOC ≥ 40 % so PV surplus flows continuously to the grid;
  battery reaches 100 % as near to dusk as solar allows, never curtailing PV
- Night export to grid — discharges battery surplus to the DNO cap when the
  bias-corrected solar forecast shows tomorrow will refill it
- Flood-prevention pre-drain — overnight pre-drain to create headroom for
  next-day peak solar, gated on the refill-day forecast (today vs tomorrow)
  and inclusive of any scheduled Axle VPP export
- Drift protection — `HOLD_ESS_MAX_CHARGE` / `HOLD_ESS_MAX_DISCHARGE` reset
  on every mode change and read-back-verified every 15 minutes

**Solar forecasting (`openmeteo_forecast.py`)**
- Open-Meteo, no API key — per-array tilt / azimuth / shade modelling
  (4 arrays out of the box, configurable JSON for any roof layout)
- Magnitude-conditional bias correction *(v5.21)* — 5-band correction factor
  (17.5 / 30 / 40 / 50 / 65 kWh) derived nightly from a rolling 60-day
  forecast-vs-actual record; corrects opposite-sign errors at low-vs-high
  forecast extremes that a single flat factor cannot
- 7-day rolling MAPE summary logged at midnight

**Octopus Energy integration (`octopus_api.py`)**
- Auto-detects tariff product code (no manual selection)
- Pulls today + tomorrow import + export rates; rate-limit-aware
  (warn > 80/hr, hard-stop > 95/hr)
- Export-sync check *(v5.19)* — compares the inverter's daily export kWh
  against Octopus's settled half-hourly readings for the last 7 settled
  days; anything outside ±5 % is flagged as drift

**Axle VPP (`axle_api.py`)**
- Reads the announced event schedule from Axle's API a day ahead, then **self-drives
  the export for the window** *(v5.28)* — `night_export` from T-2min to end+2min, the
  battery feeding the grid up to the DNO cap. Axle settle on the meter reading, so
  exporting it ourselves counts towards the event in exactly the same way (Axle
  confirmed this in writing), which means a dropped cloud dispatch on their side no
  longer costs you the event
- Reserves the event's export energy overnight so a morning event always has its kWh
  ready, and holds a next-day reserve floor so the battery only ever exports what it
  can spare — no grid import to feed an export
- Per-event JSONL telemetry at `<data_dir>/vpp_events/<YYYY-MM-DD_HHMM>.jsonl`
- Post-event summary written to the `axleVppMonitor` device, plus a Pushover
  with a pre-formed *Ask Claude* analysis prompt
- A `currentMode.vppExport` trigger sub-state so an Indigo trigger can fire the
  moment a VPP export window opens

**Grid + storm awareness**
- Storm watch (`storm_watch.py`) — MeteoAlarm CAP feed; raises the dawn-target
  SOC and suppresses export during amber / red warnings
- Power-cut detection with a rolling 100-event log — 4-hour export lockout
  after grid restoration; menu item to inspect the log

**Web dashboard (`web_dashboard.py`)**
- Local HTTP server on port 8179 with live power-flow diagram, status chips
  for grid state and current manager mode, and Chart.js charts (bundled with
  the plugin and served from `/chart.js`, so they draw with no internet — the
  CDN is a fallback only): 24h / 48h / 7d SOC line, stacked half-hourly energy
  bars (PV / export / import / home), 30-day daily totals
- JSON API: `/api/status`, `/api/history`, `/api/daily`, `/api/export-sync`

**Indigo integration**
- Five custom device types: Battery Manager, Sigenergy Inverter,
  Solar Forecast, Tariff Monitor, Axle VPP Monitor
- Custom plugin events for triggers — emergency import, export start / stop,
  VPP lifecycle, flood-prevention lifecycle, power-cut lockout
- Indigo variables for live status, the optimiser plan, and a
  `sigen_manager_paused` pause-from-anywhere switch
- Companion advisory script `openmeteo_battery_optimiser.py` — 20:00 EVENING
  and 01:45 OVERNIGHT Pushover messages describing tonight's plan in plain
  prose; reads the plugin-published `sigen_site_config.json` so it can never
  drift from plugin values

**Reliability + ops**
- Half-hourly SQLite energy log (feeds TariffAnalyser), daily history kept
  for good (no cap since v5.7 — a record is about 280 bytes, so decades of it
  stay under a few MB), weekly battery State-of-Health snapshot, weekly tar.gz backup
  of all on-disk state to `data_dir/data_backup/` (8 kept ≈ 2 months)
- Auto-update notifier — checks GitHub releases on startup, logs an INFO
  line if a newer plugin version is available
- All credentials resolved from `IndigoSecrets.py` first, PluginConfig
  fallback; an ERROR is logged and the feature skipped if neither is set
- Pushover quiet hours + configurable sound; HIGH-priority alerts always fire

---

## Logging

Every log line from `self.logger.*` carries a millisecond timestamp
`[HH:MM:SS.mmm]`, so you can line events up precisely against the other CliveS
plugins — Device Activity Monitor uses the same format.

To turn the prefix off, or back on, at any time:

**Plugins → Sigenergy Manager → Toggle Timestamps in Log (on/off)**

The plugin stores the setting in `pluginPrefs` (`timestampEnabled`) and it
survives a restart. It defaults to ON. *Note: some legacy submodules log via
`indigo.server.log()` directly and are unaffected by the toggle.*

## Version history

The 5.42–5.43 run below is the third full multi-agent deep review of the plugin
and its companion scripts. Every finding was adversarially verified against the
code before anything was fixed, the fixes went out in three batches with a live
restart check after each one, and the test suite grew from 257 to 283 to lock
them in.

The 5.36–5.38 run came out of the second full deep review — a fresh sweep for
correctness, robustness and shared-user fit, with every finding verified before
it was fixed and the test suite grown to 246 to lock the fixes in.

| Version | Date | Notes |
|---------|------|-------|
| 5.58.0 | 07-Aug-2026 | **A warning when the battery is too low for a grid event.** Half an hour before each event the plugin already works out whether there is enough stored energy to see it through, and if there is not it carries on anyway — it will never buy electricity at full price to sell it, and the export simply stops when the battery reaches its floor. Sensible, but until now it said so in one line of the log and nowhere else, so the first you knew of a short event was the payment days later. That was fine while events came with a day's notice and you had to opt in to each one. It is not fine now the scheme opts you in by default and has started calling events at two hours' notice, which leaves far less room for the sun to top the battery up. So it now sends a message giving the window, what is stored, how far short it is, and where the export will stop. It goes out at normal priority on purpose, so quiet hours can hold it back — there is nothing you could do at three in the morning anyway. Nothing about the export itself changed: the plugin still starts two minutes early, which measurements show is already enough to be at full power the moment the paid hour opens. 7 new tests; 507 pass. |
| 5.57.0 | 06-Aug-2026 | **A fresh pair of eyes over the code that earns the money.** The part of the plugin that drives grid-event exports had not had a proper adversarial review since June, so it got one, and five things came out. The biggest: the grid service labels every event as import or export, and the plugin never looked — an import event, which they have never yet sent, would have been driven as a full export, pushing energy out through the very hour the grid wants it in. It is now refused with a clear warning. Two self-checks were added for the settings that make a sunny-day export actually reach the grid instead of quietly refilling the battery — the failure that cost a missed dispatch in June resurfaces silently if one write fails, and now heals within a minute. A restart in the middle of a daytime event no longer briefly flips the inverter to the wrong mode while it finds its feet. And an event spanning midnight would have settled with a negative export figure on every readout — the daily meter resets under it — which is now carried across correctly. One suspected fault was checked and found NOT to be one: the day/night decision already survives a restart. 15 new tests, ten of them proven to fail on the old code; 500 pass. |
| 5.56.0 | 05-Aug-2026 | **The report sent after a grid event was answering the wrong question, and crying wolf.** Since June the plugin has driven every export itself, over the local connection to the inverter, ignoring anything the grid service sends — that is what makes the events pay reliably. The summary notification had never been reworded, so it still asked what the grid service had done, and the reader went looking for something that was never going to be in the file. Worse, the check on whether the sun kept running through the export had no test for whether the sun was up: every night-time window reported "PV collapsed". Both fired together on tonight's 21:00 window — a textbook hour, 4.23 kWh sold with the grid held steady the whole time, and a notification saying solar had failed. The verdict now reads ran, curtailed, or not applicable, and the notification says plainly that we drove the export ourselves. Two new readings on the monitor device carry the detail. The "who is driving" field used to repeat our own intention back to us rather than look at the inverter; it now compares what the inverter is set to against what we set it to, so anything else taking control is reported instead of hidden. The self-check that runs through an export gained one register: on sunny days the charging cap is the thing that pushes the surplus out to the grid rather than into the battery, and if it drifted the export would quietly fall to nothing while everything else still looked right. And the timezone tidy-up of 30 July turned out to have reached only part of the plugin. Working out UK local time correctly matters here more than it sounds: it decides which half-hour of the tariff you are in, when the day rolls over, when the sun comes up, and whether a grid event counts as daytime. It had been tidied three times over the past few months and finished none of them — and each time, the places left behind would quietly fall back to an hour out for the eight months of British Summer Time, saying nothing. There is now one piece of code that answers that question, shared by everything that asks, and if the timezone information is ever unavailable it says so loudly instead of guessing. The trickiest corner is now pinned down by tests: the hour on the last Sunday in October that happens twice, which the two available libraries spell differently and which the code had been resolving inconsistently. 51 new tests, 485 pass, and the check that they pass without the optional library was itself broken — it had been quietly doing nothing, so every earlier claim of testing that case was wrong. Fixed and proved before being trusted. |
| 5.55.5 | 05-Aug-2026 | **Daytime export was switching itself on and off every few minutes.** When the day's sunshine will more than fill the battery, the plugin caps the charging rate so the surplus goes out to the grid rather than being thrown away. On some afternoons that is a close call, and the plugin was making it with no margin at all, so on a day sitting right on the line it kept changing its mind — nine times in one day here, four of them inside twenty minutes. Every change writes a block of settings to the inverter and a page of workings to the log. The real cost is that export stops each time, so the surplus goes back into a battery that is already nearly full and the spare room on your grid connection sits unused, which is the waste the whole feature exists to avoid. It now wants a clear margin before it starts, and ten minutes between one stop and the next start. Stopping is untouched and still happens at once, so dusk, a storm or a fading forecast stand it down straight away as before. The log line that explains the decision now prints the figure it judged and the mark that figure had to beat, so the next close call can be read without digging through the code. 14 new tests, 448 pass. |
| 5.55.4 | 04-Aug-2026 | **A tidying fix, so the automated checks pass again.** A warning message carried a stray marker that told Python to substitute values into the text, when there were no values to substitute. Harmless in itself, and the message reads exactly as it always did, but the code checker that runs on every change had been objecting to it since the 2nd of August — so every change since had been reported as failing. Corrected, and the checks are green again. |
| 5.55.3 | 30-Jul-2026 | **A timing fallback that could quietly be an hour out.** Working out why a handful of tests failed turned up something worth having: in two places the battery planner worked out local time using an optional add-on library, and if that library were ever missing it carried on using UTC instead of saying so. British Summer Time is an hour ahead of UTC, so for eight months of the year those two places would have been an hour out — the wrong half-hour of the daily usage profile, and the cheap-rate overnight charge scheduled at the wrong time. Nothing was ever actually wrong on a real installation, because that library is bundled and pinned. But it was five near-identical copies of the same conversion, three written defensively and two not, which is exactly how the two got missed. There is now one copy, and it uses the timezone support built into Python itself rather than depending on an add-on at all. Behaviour is unchanged. |
| 5.55.2 | 30-Jul-2026 | **"Nothing scheduled" was being reported as a fault.** Axle has two ways of saying no event is coming: an empty reply, and — from the moment an event finishes — a full reply with every field left blank. The plugin understood the first and mistook the second for a broken response, so from a minute after tonight's event ended it logged an error every ten minutes and eventually sent an alert, while in fact reading the reply perfectly well. Nothing was ever wrong with the battery or the event. A blank reply is now recognised for what it is; a reply with only half its times missing, or with times that make no sense, is still treated as the fault it is. |
| 5.55.1 | 30-Jul-2026 | **The dashboards said "VPP event announced:" and then stopped.** The times were supposed to follow, but the plugin had always sent an empty value in their place — a placeholder written when the feature went in and never filled. Nothing had shown it up because no event had reached that line before. The announced window now comes through properly, in local time, reading as `19:00-20:00`, with the date added only when the window is not today. If there is no event, or the stored one is unreadable, it sends nothing at all rather than half a sentence. |
| 5.55.0 | 30-Jul-2026 | **A broken Axle connection now shows up, instead of looking exactly like a quiet week.** The plugin asks Axle every ten minutes whether a grid event is coming, and that question has two very different answers which used to look identical from the outside: "nothing scheduled" and "we are not speaking to you". When this system's Axle token stopped being accepted, every one of those questions failed and nothing was written down anywhere — no log line, no error, and the Axle VPP Monitor carried on reading Standby, which is precisely what a genuinely quiet week looks like. The fault was found only because an event was announced by email and the plugin plainly had not noticed it. The Axle client now reports through the plugin's own log rather than one that had nothing listening to it, and the monitor device gained two readings: whether the last check succeeded, and when the last good one was. A failure is written once and then at most hourly, so a long outage does not bury the log, and the recovery is written too. If your own token ever stops working, you will now be told rather than left wondering why the events went quiet. |
| 5.54.0 | 26-Jul-2026 | **The restore alert now works out whether today's sun will do the job, rather than leaving you to guess.** The previous message named both ways the export pause can end — the battery reaching the reserve, or the day's own solar refilling it — but not which one applied to you that morning. The plugin already answers exactly that question every minute, so the alert now asks it once more as it sends and tells you the outcome, with both figures so you can check the sum: *"today's solar covers that reserve on its own (10.6 kWh spare against the 4.6 kWh needed), so export should restart within the minute"*, or *"does not cover that reserve yet (3.1 against 5.9), so it stands until the battery reaches 85%, the forecast improves, or the window ends"*. At night it does not dangle a forecast that cannot arrive. If the answer cannot be worked out — no forecast yet, for instance — it says nothing it does not know and simply names both rules as before. |
| 5.53.0 | 26-Jul-2026 | **The power-cut alerts now tell you what you actually need to know.** They are the two messages you read on a phone while the lights are off, and until now they said almost nothing — the time, the off-grid mode, and how long the cut lasted. Both the Pushover and the email now carry the same full picture: how full the battery is and how many kWh that is, what the house is drawing, and roughly how long the battery would carry it at that load. On a restore they also say what export is doing — held off until a stated time, and both ways that ends, either the battery reaching the reserve or the day's own sunshine refilling it. The runtime is a snapshot at the load right now, not a forecast, so it moves the moment anything switches on and stretches a long way further on a sunny day. Any figure the inverter did not report is left out rather than shown as a zero, so a partial reading costs you one line and never the whole alert. Also fixes the **storm alerts**, which have been quietly broken since early July: a yellow warning and every all-clear failed to send at all, and because the plugin only remembers an alert once it has gone out, a failed all-clear left it convinced a storm was still running — so later warnings at the same level stayed silent too. Amber and red were unaffected. No storm has hit here since it broke, so nothing was missed. |
| 5.52.1 | 26-Jul-2026 | **The power-cut message now tells you both ways export can restart.** When the grid comes back, the plugin holds export off for a few hours and logs a line saying so. That line named only one way out of the pause — the battery reaching 85% — because it was written before 5.50.0 added the second one, where export restarts early if the day's own sunshine will rebuild the reserve without any help. So an 83-second cut here on a bright Sunday morning was followed by export resuming at 74%, exactly as designed, from a plugin that had just said it would wait for 85%. It now names both. Nothing about the behaviour has changed. |
| 5.52.0 | 25-Jul-2026 | **The cost figures now add up, and you can check them yourself.** The Period totals table on the dashboards had two columns quietly on different footings. "Grid-only" — what the house would have cost with no solar behind it — left out the daily standing charge, even though a grid-only home pays exactly the same one, so it sat about 62p a day under the truth. And "Elec bill" only included the standing charge on days Octopus had already settled; since settlement runs about a day behind, almost every seven-day window had one day billed at unit rate alone. Both are fixed, and the row now reads as a sum you can check by eye: **solar benefit = grid-only − elec bill + export earned**. Your headline saving does not move — the standing charge cancels out of it either way — it is the two columns beside it that were wrong. The calendar-month table gains the same electric-bill column so it reconciles too. Elsewhere: the "same week last year" comparison could never have appeared, however long you had been recording, because the history feed refused to hand back more than 365 days and the comparison needs to look 364 to 370 days back — that limit is now 800. If the unit rate is ever unavailable, today's card no longer bills the standing charge on its own and declares the day covered; it shows a dash instead. And the dashboards can now read your battery's capacity from the plugin rather than assuming this system's size. 19 new tests, 374 pass. |
| 5.51.1–5.51.2 | 21-Jul-2026 | Housekeeping pair. Named log levels now map to the real logging levels — warnings and errors raised through the shared helper had been appearing as plain info lines, so amber and red entries people relied on for diagnosis never showed. Shared-utility refresh: calling the log timestamp filter twice no longer double-stamps every line, and the module imports cleanly outside Indigo. |
| 5.51.0 | 20-Jul-2026 | **The battery no longer chases 100% at the cost of throwing solar away.** On a sunny day the charge was paced to reach a full battery exactly at dusk, and that pacing is taken out of export *before* your DNO cap is applied — so the plugin spent every low-sun morning giving away units it could have sold, to buy charge, and then still met the afternoon peak with less room to absorb it than it started with, and curtailed anyway. It now paces to a **90% target** instead, which you can change. This is a target, not a limit: once export is running at your cap, surplus solar has nowhere else to go and keeps charging the battery past it, so bright days still finish near full — but out of energy that would otherwise have been wasted. Measured against a real July day here, the change exported 1.6 kWh more, curtailed nothing at all instead of 1.5 kWh, and still finished within a third of a percent of where it would have. A second setting sets the floor you want kept for a power cut, and the target is never paced below it. Storm warnings restore the 100% target — the one time a genuinely full battery is worth curtailing for — but even then it will not force-charge out of export if the day's own sunshine was going to fill it anyway. 11 new tests. |
| 5.50.0 | 20-Jul-2026 | **The post-power-cut export pause now understands the weather.** After the grid comes back, export is held off for a few hours as a precaution while the battery rebuilds a reserve. Until now that pause only ended when the battery reached a fixed percentage, which cannot tell a January night from a July morning. A 109-second blip at half past five one bright July morning was enough to hold export off for over two hours while the battery filled from sunshine that was arriving anyway — and because the battery went into the afternoon fuller than it should have been, it reached 100% early and the strongest part of the day's generation had nowhere to go and was thrown away. Export now also resumes as soon as the day's remaining sunshine will comfortably rebuild the reserve on its own, which on a summer morning is almost immediately. A new **Lockout solar-refill minimum SOC** setting (default 50%) is the safety line: however good the forecast looks, the early release never applies to a nearly-empty battery. On a winter night, with no sun to come, behaviour is exactly as before. Storm warnings are deliberately left alone, since a storm forecast means the sun may not turn up at all. 22 new tests. |
| 5.49.0 | 19-Jul-2026 | **Solar forecast figures that add up.** The solar card could show numbers that plainly contradicted each other — 38.3 kWh generated today, forecast 53 kWh, remaining 25.3 kWh. Two causes, both in the "remaining today" figure: it was added up from the *uncorrected* forecast while the headline beside it had the self-learned bias correction applied, and the whole of the current hour was counted as still to come even at ten to the hour (worth about 7 kWh on its own at midday). Both fixed, and the calculation now lives in one place rather than being written out twice. The same inflated figure fed the "expected total" line in Show Today's Energy Summary and Show Manager Status, so those were overstating the day too. The hourly forecast bars on the dashboards are corrected the same way, so they now add up to the headline forecast rather than the raw model total, and a new **Expected total** figure shows generated-so-far plus still-to-come. Battery and export decisions were never affected — the decision engine calculates its own remaining-solar figure and was already doing both of these things correctly. 9 new tests. |
| 5.48.0 | 06-Jul-2026 | **VPP events survive a plugin restart.** If the plugin restarts while an Axle VPP export window is running — a config change, an Indigo restart, or an update landing mid-event — it now picks the window straight back up and carries on exporting to the end. Previously it worked out what to do afresh from Axle's API on each poll, so resuming a live window relied on Axle still reporting the event, and if it stopped doing so once the window was under way the rest of that window could be missed. The active window is now saved on every change and restored on startup, so the export continues without needing Axle to re-confirm it. A window that finished while the plugin was down is tidied up cleanly instead of leaving the battery reserve held high. Window times remain fully daylight-saving-safe. No configuration changes. |
| 5.47.0 | 05-Jul-2026 | **Octopus Go and Intelligent Octopus Go ready.** The Go off-peak window is corrected to 23:30–04:30 (it had been set to an older 00:30–05:30) — on the old value the battery would have missed the genuinely cheap 23:30–00:30 half-hour and, worse, tried to grid-charge between 04:30 and 05:30 at the full day rate. The overnight top-up that guarantees your power-cut reserve now runs on time-of-use tariffs too. Previously it only ran on flat-rate tariffs, so on Go, Intelligent Octopus Go or Flux a night before a sunny day could leave the battery drifting below your reserve with nothing to top it back up. It now maintains that reserve inside the cheap window only, so the top-up is always bought at the night rate, and stands aside when the battery is already charging to cover tomorrow. No change for Tracker or Flexible users. 304 tests pass. |
| 5.46.0 | 03-Jul-2026 | **Gas cost fix — partial days can no longer settle at £0.00.** The whole-house cost card could show £0.00 gas for a day when Octopus had only delivered part of that day's readings at the moment the plugin settled it, and the near-zero figure was then frozen permanently, with the running estimate for newer days inheriting it. The settle now checks that the day's readings actually reach the end of the day before freezing the figure, which works for both half-hourly gas meters and daily-read meters. Days already frozen with partial data re-settle correctly after upgrading. Six new tests. |
| 5.45.0 | 02-Jul-2026 | **The plugin no longer makes you wait.** Internally, everything used to queue behind one lock while the inverter was being read or a forecast fetched — so pressing Pause, opening Configure or refreshing the dashboard mid-poll could hang for up to twenty seconds. Network work now runs outside the lock and only the results are merged under it, while battery decisions stay fully serialised for safety. The dashboard now answers in milliseconds even mid-poll, and actions bite immediately. A new set of concurrency tests pins the design so it cannot quietly regress, and the review along the way caught and fixed the outage back-off timer being accidentally cancelled each poll. 295 tests pass. |
| 5.44.0 | 02-Jul-2026 | **Smarter Agile imports, and a decision made explicit.** On Agile, an overnight top-up now has to actually beat tomorrow's daytime rates once the battery's ~6% round-trip loss is counted — on a flat-priced Agile day the plugin now lets the house draw from the grid directly rather than cycling the battery at a loss (Tracker and Flexible already worked this way). And after a supervised live test on the inverter proved the new import safety ceiling works in every mode, the deliberately cautious evening export model was reviewed, confirmed as the right choice for self-sufficiency, and documented in the code with a test to keep it that way. 288 tests pass. |
| 5.43.1 | 02-Jul-2026 | **Polish across the board.** The dashboard's chart library is now bundled with the plugin, so charts draw even with no internet (it was fetched from a CDN before). Storm watch only logs when something actually changes rather than every couple of hours, the Configure dialog now checks the port and power figures as you save them, the plugin's variables get their own "Sigenergy" folder on a fresh install, and a long tail of small robustness fixes landed across every module and companion script — around eighty in all, from clearer failure messages to guards against odd meter readings. 283 tests pass. |
| 5.43.0 | 02-Jul-2026 | **Robustness through and through.** Pausing the manager now survives a plugin restart. During an inverter outage the manager holds its last good reading and backs off rather than repeatedly retrying, and it no longer plans battery moves from hours-old data once the readings go stale. The overnight pre-drain target and the post-power-cut export lockout now survive a crash, not just a tidy shutdown. The inverter connection is health-checked before it is trusted, a rejected register write is now reported as the failure it is, and restarting the plugin no longer writes a phantom 0% battery reading into your history. Octopus rate windows are built against the UK day in summer correctly, month cost figures start from zero on the 1st instead of showing last month all day, and the solar forecast's accuracy learning now grades the real morning forecast rather than marking its own homework. 283 tests pass. |
| 5.42.0 | 02-Jul-2026 | **Control safety.** Four significant finds from the third deep review. A grid import now sets a hardware ceiling on the inverter, so if the plugin ever crashes or loses contact mid-import the battery cannot keep charging from the grid unbounded (export already had the matching floor). An overnight pre-drain now stops immediately if a storm warning arrives mid-drain, rather than carrying on exporting the very reserve the storm needs. An inverter outage now aborts its read cycle in about a second with one log line instead of twenty. And a failed storm-feed check now holds the previous warning level rather than treating "couldn't reach the feed" as "all clear" — with a graceful release if the feed stays down for a day. 272 tests pass. |
| 5.41.0 | 30-Jun-2026 | **Live cost and rate variables.** The `elec_*`, `gas_*`, `export_*` and `account_balance_gbp` Indigo variables are now published by the plugin from the Octopus account ledger, bill-exact and refreshed every half hour. They had been orphaned since the old standalone Octopus script was retired, quietly holding rates from weeks ago. |
| 5.40.0 | 30-Jun-2026 | **Storm reserve simplified to a flat 50%.** Every storm level now holds the same 50% minimum reserve, and the battery is never charged from the grid above it — the previous 80–90% targets bought little extra cover and cost real import money. |
| 5.39.0 | 29-Jun-2026 | **Storm export released when the battery is nearly full.** During a storm warning, once the battery is above roughly 85% the reserve is safely banked — so deliberate export resumes and the day's surplus solar goes to the grid instead of being curtailed. |
| 5.38.2 | 26-Jun-2026 | **Octopus, forecast and action tidy-up.** Octopus rate windows are now read against the UK local day rather than UTC, so nothing lands on the wrong day in the small hours of a summer morning. Export-meter detection is more careful — if you have a second electricity supply it is no longer mistaken for an export meter. The solar forecast's timezone fallback is now correct in winter as well as summer. The Force Grid Import and Force Grid Export test actions now honour the values you type and the limits their labels promise (export always stays under your DNO cap regardless). Storm alerts no longer repeat every time the plugin restarts during an active warning. Plus a good batch of new tests and small guards. 246 tests pass. |
| 5.38.1 | 26-Jun-2026 | **Dashboard hardening.** The dashboard's data is now same-origin only, its API errors return proper HTTP status codes so the charts fail cleanly instead of trying to draw an error, and a few display fields are null-safe so one missing value can never blank the whole page. The dashboard still serves on the LAN exactly as before. |
| 5.38.0 | 26-Jun-2026 | **Whole-house cost works for more meter setups.** A daily-read gas meter (one reading a day) now settles properly instead of being stuck on an estimate for ever, a gas meter that reports in kWh is no longer over-counted, a home with no gas meter gets an electricity-only card, and the settle window is wider so a day that settles late at Octopus is not missed. Your own setup is unaffected — this is about working cleanly for other people's meters too. 236 tests pass. |
| 5.37.0 | 26-Jun-2026 | **Storm watch brought back to life.** MeteoAlarm quietly changed the format of its warning feed, and the storm detection had stopped finding anything as a result — it had been reporting "no warnings" through real ones. It now reads the current feed format (with a fall-back to the old one and a guard that shouts if the format ever changes again), so the battery is pre-charged and export held off ahead of a Met Office storm as intended. The location name shown in the alert is now a setting. Validated live against a real Yellow thunderstorm warning. |
| 5.36.0 | 26-Jun-2026 | **Pause and sleep safety.** Pausing the manager now properly hands the battery back to a safe state — before, if you paused while an overnight pre-drain or storm floor had raised the discharge limit, the battery could be left locked above that level and quietly import from the grid for the whole pause. A pause also no longer lets a VPP event carry on driving the inverter behind the scenes. Plus a daily-figures timezone fix around midnight and a Modbus read-health correction. 207 tests pass. |
| 5.35.0 | 24-Jun-2026 | **Full numeric telemetry for charting, plus tidy-ups.** Every inverter state (SOC, power flows, temperatures, daily totals) used to be stored as text, which Indigo's built-in history doesn't chart — so only timestamps were logged. They are now proper numbers, recorded as chartable columns, with per-state decimal-place hints keeping the device display clean. No separate database needed. The export-lockout SOC floor becomes a config field (default 85%). Cosmetic fix: the Live Power Flow card showed "Lockout" for the whole post-cut window even when the battery was happily exporting, and now shows it only when export is genuinely held off. 193 tests. Note for existing installs: the one-time text-to-numeric migration leaves a few harmless duplicate columns in the history database — chart the clean numeric ones and ignore the leftovers. |
| 5.34.0 | 24-Jun-2026 | **Keep exporting after a power cut when the battery is high.** After the grid is restored the plugin holds export off for 4 hours as a precaution, and until now that applied no matter how full the battery was — so a near-full battery on a sunny afternoon would climb to 100% and start clipping solar that could have been exported. The lockout now only holds export off while the battery is below 85%, so at or above that flood-prevention can shed the surplus and no generation is lost. Below 85% the precaution still applies, and an unknown SOC fails safe. Also adds a numeric `gridOnline` state (1 = on-grid, 0 = power cut) so device history can chart a clean outage timeline — it isn't written when Modbus is merely offline, so a comms blip can't look like a cut. 5 new tests (188 passing), verified live at 99% SOC with a lockout window active. |
| 5.33.0 | 24-Jun-2026 | **Power-cut alerts when the grid drops and when it comes back.** When the system loses mains and the house islands onto the battery you now get a Pushover alert and an email, and again the moment the grid is restored, with how long the outage lasted. Pushover alerts go at normal priority so they honour your quiet-hours window — overnight blips stay silent there but still arrive by email. A new Power-cut notifications section in Configure has an on/off toggle (on by default) and an email recipient field, which resolves from `IndigoSecrets.py` (`POWERCUT_EMAIL`) first and falls back to the config field. Both sends are best-effort and never interrupt inverter polling. A longer outage may take your broadband down too, in which case the alert arrives once connectivity returns. |
| 5.32.0 | 24-Jun-2026 | **Overnight-export messages now always match what the battery does.** If the 8pm "tonight's battery plan" message ever promised a pre-dawn export that then didn't happen, this is the cause. The advisory script used to work out the export decision on its own, separately from the battery manager, and the two could quietly drift apart — on a marginal night the 8pm message could say "we'll export overnight" while the plugin, re-checking against the latest forecast, correctly decided not to. The battery manager is now the single source of truth: it publishes the exact gate it acts on every cycle and the advisory simply reports it. The 8pm message flags a marginal call rather than stating it as certain, and the 1:45am check-in reads the actual export rather than assuming. Battery control itself is unchanged. 4 new contract tests lock the published gate to the live decision (183 passing). |
| 5.31.6 | 22-Jun-2026 | **Solar card data.** Adds today's solar against forecast, current and peak power (with the time of peak), tomorrow's forecast, yield per kWp, self-sufficiency, forecast accuracy and lifetime total to the dashboard. Per-string measured output is being worked on separately (needs daylight to read the inverter). |
| 5.31.5 | 22-Jun-2026 | **Whole-house cost — Yesterday shows up straight away.** While a day's gas is still settling at Octopus, "Yesterday" now shows a provisional figure rather than a blank: the electric and export are accurate (Sigenergy measured them all day) and only the gas is estimated, flipping to the settled figure once it lands. 179 tests pass. |
| 5.31.4 | 22-Jun-2026 | **Whole-house cost — Today, Yesterday and the day before.** The card now shows three days side by side. Because Octopus settles about a day in arrears, "Yesterday" is often still settling first thing — so the third "Day before" column always gives you a complete, settled day to look at. 178 tests pass. |
| 5.31.3 | 22-Jun-2026 | **Whole-house cost — wait for a full day before settling.** A day is only frozen once Octopus has settled (nearly) all of its half-hourly readings. Previously the most recent day could be locked in while only the first hour or two had settled, showing a near-zero gas and electric usage cost that never corrected. Yesterday's panel now shows "awaiting settlement" until the full day lands, then fills in correctly. 177 tests pass. |
| 5.31.2 | 21-Jun-2026 | **Whole-house cost — robustness pass.** Follow-up hardening from the same review: the daily history file is now written atomically so a crash mid-save can never truncate it; the Octopus ledger queries pass the account details as proper query variables rather than building the query as text; one day of bad meter data is skipped rather than stopping the whole settle; and the dashboard caches the history between refreshes instead of re-reading it every few seconds. 175 tests pass. |
| 5.31.1 | 21-Jun-2026 | **Whole-house cost hardening.** A deep-review pass tightened the new cost feature. Each settled day now keeps the standing and gas rates that applied on that day, so a future tariff or price-cap change can never retroactively re-value a frozen day. The Octopus ledger lookup backs off during an outage instead of retrying on every dashboard refresh, and a fault in the whole-house block can no longer blank the rest of the dashboard. 164 tests pass. |
| 5.31.0 | 21-Jun-2026 | **Whole-house cost — gas + electric, including standing charges.** A new `economics.whole_house` block on `/api/status` drives the Dashboards "Whole-house cost" card: today (provisional), yesterday (settled), month-to-date net (in credit / owing), days self-funded, live account balance, and a 30-day bill-vs-export chart. Each day's full cost is frozen into `daily_history.json` once Octopus settles it, valued at the rate that applied on that day — so changing tariff never re-writes your history. Rates and the account balance come bill-exact from the Octopus Kraken account ledger; gas is valued from settled half-hourly consumption (m³ → kWh via a calorific factor you can pin to your bill). Gas has no live meter, so today's gas figure is an estimate until it settles next day. New `OCTOPUS_GAS_MPRN` / `OCTOPUS_GAS_SERIAL` secrets and `octopusGasMprn` / `octopusGasSerial` / `gasKwhPerM3` config fields. 157 tests pass. |
| 5.30.1 | 15-Jun-2026 | **Guarantee the full VPP export at any solar level.** A correctness follow-up to 5.30.0. With no sun the battery exports the full target. With some sun below the cap, solar and battery together make up the target. With plenty of sun the full target is exported and the spare solar is banked. 5.30.0 had an edge case where, if solar dropped from above the cap to just below it, the system could briefly export slightly under target because it stayed in bank-the-surplus mode, which doesn't draw on the battery. It now switches to topping up from the battery the moment solar falls below target, so the paid figure is always delivered. |
| 5.30.0 | 15-Jun-2026 | **Daytime VPP export now banks the surplus — export and charge together.** Until now a daytime VPP window either curtailed the solar above the export cap (5.29.1) or drained the battery. A daytime window now behaves like the system's own Solar Overflow mode: export the full amount to the grid *and* bank the spare solar into the battery at the same time, with nothing wasted. The plugin re-checks live solar every cycle and picks the approach automatically — plenty of sun stays in Max Self-Consumption and caps the battery charge so exactly the export figure goes to the grid (proven at a real 10 kW solar moment: 4 kW exported, ~4.9 kW into the battery, house covered, no solar curtailed), and not enough sun falls back to discharging the battery to top the export up. It switches with hysteresis so it won't dither around the threshold, and only talks to the inverter when something changes. Six new tests. |
| 5.29.2 | 15-Jun-2026 | Register-map corrections after a deep read of Sigenergy's Modbus Protocol V2.9, the revision after the V2.8 baseline the plugin was built on. Labels and comments only, no change in behaviour: EMS mode `0x07` is Reserved rather than the "AI Mode" it had been labelled (it was never commanded), `0x08` is V2G, and the comments on registers 40032 / 40034 now say what those registers really are — global caps that apply whatever the EMS mode, which is exactly why pinning the charge limit to 0 in 5.29.1 forces the export. The header also records register 40001 (PCS active-power dispatch) as a future way to export a precise figure, deliberately left unused until its sign can be checked on real hardware. |
| 5.29.1 | 15-Jun-2026 | **Daytime VPP export fix — force PV to the grid.** Use this rather than 5.29.0. In 5.29.0 the daytime export left the battery charge limit open, so when solar exceeded house load plus the export cap the inverter would charge the battery with the surplus instead of exporting — meaning the paid grid dispatch could be missed exactly when the sun was strongest (seen live: 9.6 kW of solar, battery charging at 7.7 kW, grid near zero). The battery charge limit is now pinned to 0 during a daytime export window, so the surplus has nowhere to go but the grid. Re-tested live: a clean, stable 4 kW export from the panels with the battery left flat and the solar still running. The trade-off against the old night-export mode is that solar above the cap plus house load is curtailed for the window, which for a paid event is the right call. Night windows are unchanged. |
| 5.29.0 | 15-Jun-2026 | **Daytime VPP export keeps your solar running.** Daytime Axle VPP windows now export PV-first (Remote EMS mode 0x05) instead of ESS-first. Under the old mode the inverter made the battery do all the work and shut the solar down for the whole event — on the 15-Jun morning event 4.22 kWh was exported entirely from the battery with PV flat at 0 W. Mode 0x05 sources the dispatch from the panels first and only draws the battery for the shortfall, so your solar keeps running and the battery is preserved while the full DNO-capped export is still delivered. It behaves identically to the old mode when there is no sun, so there is no downside on dark mornings. Live-validated: solar held ~2.9 kW, battery covered the ~1.9 kW gap, grid export pinned at the 4 kW cap. Adds a Force Daytime Export test action. |
| 5.28.3 | 11-Jun-2026 | Housekeeping from the fleet-wide audit — lint cleanup and a CI gate, generic example IP addresses throughout the published files, and the standard Authors and licence footer. No functional change. |
| 5.28.2 | 10-Jun-2026 | **Configurable Axle VPP rate.** The £1/kWh payment rate is now a plugin preference (`axleVppRatePerKwh`, default `1.00`) rather than hardcoded, used for the earnings estimate on the Axle VPP Monitor — handy if Axle change the rate. Coerced through the guarded `_as_float` so a blank or non-numeric value falls back to 1.00. Mirrors Predbat's `axle_pence_per_kwh`. 127 tests pass, restarted clean. |
| 5.28.1 | 10-Jun-2026 | **VPP self-drive cleanup.** Removed the now-dead Axle release-watcher (`_vpp_check_axle_release` + `_send_vpp_release_alert`, about 122 lines), the `VPP_COOLING_OFF` state and its branches, and the unused `AXLE_SUPPORT_EMAIL` import. Added a dedicated `vppExport` `currentMode` enum Option so an Indigo trigger can fire on "VPP export active" (it previously reused the `startExport` token). No behaviour change to the self-drive itself. 127 tests pass, restarted clean. |
| 5.28.0 | 10-Jun-2026 | **VPP self-drive — the plugin now drives the export itself for the announced window rather than waiting for Axle's cloud dispatch.** The 10-Jun event was a no-show (Axle emailed confirming a SigEnergy-API fault that "may not be resolved before the next event"), and releasing Remote EMS to hand Axle the channel made no difference — their cloud simply did not dispatch. Since Axle settle on the meter reading, exporting it ourselves counts identically (about £1/kWh, stacking with the Octopus Outgoing rate). The manager's VPP override now returns `ACTION_VPP_EXPORT` instead of standing down. `night_export` fires on entry to `VPP_ACTIVE` at T-2min and is re-asserted idempotently each manager tick so a transient drop self-heals, then restored to self-consumption at end+2min via the new `_end_vpp_export()`. The discharge floor stays at the next-day reserve (daytime → health floor, night → reserve) and there is no grid import — pre-charge stays solar-only until a cheap overnight EV tariff makes importing-to-export worthwhile. Axle's start / stop dispatch is ignored, though the announced window times are still read from their API. |
| 5.27.0 | 06-Jun-2026 | **One source of truth for Octopus rates.** The plugin now writes the rate and slot variables the optimiser script reads — `elec_rates_today_json` / `elec_rates_tomorrow_json`, `tracker_rate_today` / `tracker_rate_tomorrow`, the product code and name, and the fetch status — from its own Octopus fetch, on every refresh. A new `octopus_api.get_active_rate_schedule()` returns today's and tomorrow's raw slots for whichever tariff is active, so it is right for people who are not on Tracker. That retires the standalone `octopus_tracker_rate.py` script, which had been fetching the same data a second time and had gone stale. Its Indigo schedule is disabled and the script kept on disk as a fallback. |
| 5.26.2 | 06-Jun-2026 | **Low-severity sweep**, clearing the review queue bar the Octopus consolidation above. Sleep now also drops a raised discharge cutoff back to the health floor, so a flood-prevention, storm or VPP floor left high cannot lock the battery and force overnight grid import across a long Mac sleep. The Modbus power-limit setters clamp to a 100 kW ceiling before writing watts to the inverter. The poll loop honours the advertised 5s "very live" interval rather than a hardcoded 10s. Seasonal and storm overrides log only when they change, instead of every 60 seconds. The dashboard survives one bad float (NaN and Infinity are now JSON-safe), keeps the selected calendar year across a refresh, and its Back link is host-relative rather than a hardcoded LAN address. The Modbus test harness no longer really sleeps, which takes the suite from 123 seconds to about 11. 126 tests pass. |
| 5.26.1 | 06-Jun-2026 | **Medium-severity hardening**, following the highs below. Each tick task is wrapped, so one bad Modbus, forecast or VPP tick is logged and retried rather than killing the whole polling loop, and the config coercion helpers now coerce their fallback too — a string default could otherwise leak into arithmetic when a field was left blank. The consumption estimate indexes the 48-slot load profile by local time rather than the UTC hour, which had it two slots out in British Summer Time. The forecast drops a day-shifted disk cache on restart instead of serving yesterday's buckets as today's. Eight new regression tests. |
| 5.26.0 | 06-Jun-2026 | **High-severity fixes from the first comprehensive review.** The pause feature was dead — `sigen_manager_paused` and the Pause action set a flag nothing read, so a manager reading "Paused" carried on driving the inverter. It is now honoured, returns the inverter to self-consumption, seeds itself from the variable at startup and re-evaluates the moment you resume. A partial Modbus read could feed the manager a phantom 0% state of charge and start a force-charge that never finished — `read_all()` now returns nothing at all if a critical register is missing, so the last known-good snapshot stands. Cheap-window detection compared a UTC clock against local-time window strings, an hour out in summer. And a partial Open-Meteo fetch was stamped OK and allowed to overwrite a good cache with a low total, which triggered import nobody needed. Six new tests. |
| 5.25.7 | 05-Jun-2026 | **Config hardening from the estate-wide bug sweep.** All 42 unguarded `float()` and `int()` calls on preferences and action properties now go through guarded helpers, so a blanked or non-numeric field falls back to its default instead of raising on the battery-evaluate hot path. Also guards a divide-by-zero in the VPP pre-charge sizing and a missing-key crash in the Octopus tomorrow-slot read. |
| 5.25.6 | 04-Jun-2026 | The module-level `log()` helper built its prefix at second precision, bypassing the millisecond filter every other line goes through. It now emits `[HH:MM:SS.mmm]` like the rest of the estate, so events line up across plugins. |
| 5.25.5 | 04-Jun-2026 | Recurring red `HTTP 502` errors from Open-Meteo were their gateway having a moment, not a fault here — the forecast already falls back to the cached copy, so the failure was harmless, but the red line with an HTML body dumped after it was alarming. A 5xx now logs a short warning saying it is transient and that cached data is in use. A 4xx still logs an error. |
| 5.25.4 | 01-Jun-2026 | Live Power Flow diagram polish — the four outer nodes (Solar, Home, Grid, Battery) are now the same size, the Grid figure is as large as Home with an Import / Export / Idle line beneath it, the Battery leads with kW and carries the percentage below, and the Home and Grid figures line up on the flow axis. This release also caught the repository up: the 5.25.0–5.25.3 dashboard work had been running here for days without ever being committed. |
| 5.24.1 | 28-May-2026 | Bug fix — silences the one spurious red ERROR per restart (`device "Battery Manager" state key currentMode not defined`). The `currentMode` enum state is registered asynchronously after `stateListOrDisplayStateIdChanged()`, so the first write — which batches `currentMode` with about twenty other keys — raced the registration and logged a harmless error while the rest of the batch applied fine. `currentMode` is now appended to the batch only once it exists in the device's states, so the first tick after a restart skips it and the next writes the real mode. |
| 5.24.0 | 28-May-2026 | Added the `currentMode` enum state to the Battery Manager, which makes Indigo generate one boolean sub-state per mode (`currentMode.solarOverflow` and the rest). You can now trigger on "the battery entered night export" without comparing strings. Additive — `currentAction` keeps its friendly display text. `deviceStartComm` re-fetches the device after the state-list refresh so the new enum and its sub-states register cleanly. |
| 5.23.0 | 27-May-2026 | **Safe on sleep.** Added `prepare_to_sleep` and `wake_up` hooks, harvested from a sweep of `plugin_base.py`. A sleeping Mac used to leave the inverter in whatever forced mode it was last given, so an eight-hour overnight sleep during a force-charge would overcharge the battery. On sleep the plugin now returns the inverter to self-consumption, saves its accumulators, stops the dashboard and closes the Modbus connection. On wake it restarts the dashboard and forces an immediate poll and re-evaluation rather than waiting out the interval. |
| 5.22.1 | 27-May-2026 | **Bug fix — Octopus Go BST cheap-window classification.** `octopus_api._parse_tou_slots` resolved Europe/London exclusively via `pytz`. When `pytz` wasn't importable (test environments without it installed) the fallback assumed `UTC == local`, so a UTC 23:30 slot in summer was bucketed as standard instead of cheap — local 00:30 is the *start* of the Go 00:30–05:30 cheap window. The path now prefers stdlib `zoneinfo` (always available on Python 3.9+) and only falls back to `pytz`, then `None`. Production Indigo installs were unaffected because `pytz>=2024.1` is in `requirements.txt`, but tests and runtime now exercise the same resolver. New regression case `test_bst_utc_0030_is_local_0130_not_cheap` locks down the boundary at UTC 04:30 BST = local 05:30 (exclusive end → standard). 57/57 tests pass. |
| 5.22.0 | 27-May-2026 | **Decision-audit trail in `battery_manager.evaluate()`** — plan-object pattern lifted from `mlamoure/indigo-auto-lights`. The `Decision` dataclass gains an `audit_trail: List[Tuple[str, str]]` field that `evaluate()` populates at every branch — CONTEXT, BALANCE, OVERRIDE, RESILIENCE, FLOOD-PREP, IMPORT, OVERFLOW, RELEASE-OVERFLOW, DEFAULT — including the branches considered but skipped with short skip reasons. `plugin.py:_log_manager_decision` dumps the audit block immediately after the action-change INFO line, once per action transition (no per-poll spam). The same shape lands the same day in `openmeteo_battery_optimiser.py` v3.6 and `octopus_tracker_rate.py` v1.2, so every Sigenergy-touching decision script now audits its reasoning the same way. Existing 17 BatteryManager unit tests pass unchanged — `audit_trail` defaults to an empty list, the change is purely additive. |
| 5.21.4 | 26-May-2026 | `openmeteo_forecast.py` bumped 1.3 → 1.4 — one-shot retry (2 s back-off) on transient network errors (`Timeout`, `ConnectionError`, `ChunkedEncodingError`, the last covering Open-Meteo's occasional `SSL: UNEXPECTED_EOF_WHILE_READING` hiccups). Transient blips now log at WARNING rather than ERROR. Cache fallback and the existing 3-of-4 array path are unchanged. Triggered by a one-off East-array fetch failure at 13:11 on 26-May — the 3-of-4 fallback handled it correctly, but the red ERROR line in the event log was the wrong noise level for a transient hiccup. |
| 5.21.3 | 25-May-2026 | Added `didDeviceCommPropertyChange` returning `False`. The plugin's devices are internally managed (Modbus polling, Open-Meteo, snapshot writes) — no user pluginProps justify a comm restart, so internal `replacePluginPropsOnServer` writes no longer trigger spurious deviceStop / deviceStart cycles. |
| 5.21.2 | 23-May-2026 | Millisecond timestamp `[HH:MM:SS.mmm]` prefix on every `self.logger` line via `plugin_utils.install_timestamp_filter()`; new "Toggle Timestamps in Log" menu item. |
| 5.21.1 | 23-May-2026 | **Site coordinates moved into the IndigoSecrets pattern; no built-in default.** Plugin now reads `LATITUDE` / `LONGITUDE` from `IndigoSecrets.py` first, PluginConfig (`siteLatitude` / `siteLongitude`) next. There is no hardcoded fallback — if neither is set the plugin logs an ERROR and skips the solar forecast feature (matching the existing pattern for a missing inverter IP). The previous developer-home defaults (54.882 / -1.818) have been removed from `plugin.py`, `PluginConfig.xml`, and `openmeteo_forecast.py`. `OpenMeteoForecast.__init__` now raises `ValueError` if instantiated with no coordinates. The README and PluginConfig label show **Big Ben (51.5007, -0.1246)** purely as a recognisable example. Run-Self-Test secrets block now lists `LATITUDE` / `LONGITUDE`. Four `self.forecast.X` call sites guarded so the plugin stays up when the forecast is disabled. 101/101 unit tests still pass (test fixtures pass explicit example coords). |
| 5.21.0 | 22-May-2026 | **Magnitude-conditional bias correction.** `openmeteo_forecast.py` bumped 1.2.1 → 1.3, replacing v5.19.6's "no correction" with a 5-band per-day correction factor. Centres at 17.5 / 30 / 40 / 50 / 65 kWh; each band's factor is the median `actual/forecast` ratio of records whose raw forecast falls within ±7.5 kWh of the centre, clamped to [0.5, 1.5]. Bands with fewer than 3 in-window records inherit the global kWh-weighted scalar. Linear interpolation between centres so a forecast of 35 kWh blends the 30-band and 40-band factors. Recomputed nightly from a rolling 60-day window of accuracy records. Analysis of 31 days showed `err% vs forecast_kwh r = -0.462` — the model under-forecasts on moderate-prediction days (25–45 kWh, ratio 1.18–1.28) and over-forecasts on bright days (>55 kWh, ratio ~0.93). A single flat factor cancels these opposite-sign errors out (which is why v5.19.6 reverted to raw); the band table follows the shape. Projects MAPE 19.8% → ~14–16%. Per-day factor applied to `correctedTodayKwh`/`correctedTomorrowKwh` AND scaled proportionally across every hourly slot in `openmeteo_forecast.json` so the battery optimiser sees a shape-preserving correction (peaks scale, zero hours stay zero). New JSON / state fields: `biasFactorToday`, `biasFactorTomorrow`, `biasBands`. `test_openmeteo_forecast.py` bumped 1.0 → 1.1: added `TestComputeCorrectionBands`, `TestApplyBandCorrection`, and `TestBiasFactorApplied` (replacing v1.2's `TestBiasFactorNotApplied`); 24 tests pass. First-startup bands on the live 32-record file: 17.5/1.052, 30/1.282, 40/1.179, 50/0.984, 65/0.926. |
| 5.20.0 | 21-May-2026 | **VPP handover fix — confirmed via a 4 kWh paid event that reported 0.00 kWh export.** Root cause: the old `_poll_vpp` Step 2 called `disable_remote_ems()` at T-5min, which writes 40029=0 and literally kicks Axle out of Remote EMS — the very channel Axle dispatches through. Fix: no explicit "release" call. The "skip Modbus writes" guard now extends from `{ACTIVE, COOLING_OFF}` to also include `PRE_CHARGING`, so from T-30min the plugin stops touching register 40031 and the charge/discharge limits, letting Axle's writes stand. Step 1's `set_self_consumption()` now reads 40031 first and skips if Axle is already dispatching (mode 0x06). `_vpp_check_axle_release()` broadened to also accept `40031 == 0x02` as Axle's end-of-event signal (previously only `30003 == 0`, which Axle doesn't always set — that night's cooling_off only released at the 60-min force-timeout). Dead `VPP_PRE_EXPORT_MINUTES` constant removed. |
| 5.19.7 | 21-May-2026 | Bug fix — `_write_optimiser_file` debug-log line referenced `corrected_tmrw` (renamed to `tomorrow_kwh` in v5.19.6), firing `NameError` on every forecast refresh. Output file itself was unaffected. |
| 5.19.6 | 21-May-2026 | **Disabled application of the bias correction factor** *(later revised by v5.21.0 — see above).* Empirically the raw forecast outperformed every bias-corrected variant tried over 31 days (raw MAPE 18.7% vs corrected 21.9–22.3%). `_enrich_forecast` returned raw totals; `biasFactor` was still tracked for display only. Daily accuracy records still maintained. The conclusion was correct for a **flat** factor — a single multiplier cancelled opposite-sign errors at the extremes (see v5.21.0 for the magnitude-conditional follow-up). |
| 5.19.5 | 21-May-2026 | **Bias correction formula fix.** `_compute_correction_factor` in `openmeteo_forecast.py` switched from `mean(actual/forecast)` (arithmetic mean of ratios, statistically biased upward because ratios are bounded below by 0 but unbounded above) to `sum(actual)/sum(forecast)` (kWh-weighted, unbiased). For May 2026: factor 1.163 → 1.150. New `_clamped_sum_ratio` helper. New `test_openmeteo_forecast.py` (11 tests) including a live-data replay that proves the new formula reproduces the kWh-weighted truth. |
| 5.19.4 | 21-May-2026 | **Flood-prevention now accounts for Axle VPP exports on the refill day.** New `vpp_today_kwh` / `vpp_tomorrow_kwh` snapshot fields (computed by `_compute_vpp_export_by_date` from `store["vpp_event"]`, pro-rated for cross-midnight events, future-only). The flood-prevention gate becomes `refill_solar >= 3 × (refill_need + refill_vpp)` — previously a VPP event scheduled for the refill day was invisible to the gate, so the plugin could drain to flood-prevent overnight and then find that solar had been pre-committed to Axle and couldn't refill. |
| 5.19.3 | 21-May-2026 | **Flood-prevention refill-day fix** — `_check_flood_prevention` / `_continue_flood_prevention` now gate on the *refill-day* solar forecast (today's when dawn is later today; tomorrow's otherwise). Pre-bug code always used `corrected_tomorrow_kwh`, so a post-midnight check on a poor-today / sunny-day-after pair dumped the battery and then failed to refill (21-May incident: 14.9 kWh exported at 00:25 on a 29.5 kWh-forecast day). New helpers `_refill_day_view` / `_refill_day_label` + `corrected_today_kwh` field on `ManagerSnapshot`. |
| 5.19.2 | 15-May-2026 | **Live Power Flow visual polish (Sigenergy-app inspired).** Soft teal radial-gradient glow and horizon bar behind the card. Two new status chips top-right — grid state (`On Grid` / `Lockout` / `Grid Down`) and current manager mode (`Self Consumption` / `Solar Overflow` / `Night Export` / etc, with VPP-state override when an event is running). Richer node labels: battery now reads `0.98 kW · Charging` / `0.50 kW · Discharging` / `Idle`; grid flips to `0.94 kW · Exporting` ordering. No new data sources — everything was already in `/api/status`. |
| 5.19.1 | 15-May-2026 | Dashboard cosmetic — Live Power Flow card now shows all four power nodes (solar / battery / home / grid) in kW with 2 decimals (e.g. 980 W → 0.98 kW) for a cleaner at-a-glance read. Other cards retain the existing auto-switching W/kW format. |
| 5.19 | 15-May-2026 | **Export sync check — Sigenergy vs Octopus.** New `/api/export-sync` endpoint and **Export Sync** dashboard card. Compares the inverter's daily export kWh (from `daily_history.json`) against the half-hourly readings settled by Octopus on the export MPAN, for the last 7 fully-settled days. The most recent 3 days are skipped because Octopus typically settles export readings over 24–48 h, so today / yesterday / day-before would always look wrong. Tolerance is ±5%; anything outside is flagged as drift. New `OCTOPUS_EXPORT_MPAN` / `OCTOPUS_EXPORT_SERIAL` keys in `IndigoSecrets.py` (with `octopusExportMpan` / `octopusExportSerial` PluginConfig fallback) — feature silently disabled if absent so existing installs are unaffected. New `octopus_api.get_export_kwh_for_date(date, mpan, serial)` returns `{kwh, slots}` for a single Europe/London day. Results cached on `self.store["export_sync_cache"]` for 6 h; dashboard re-polls hourly. One-line INFO summary at midnight: `[ExportSync] 7d avg diff +0.8%  worst: 2026-05-08 +3.1%` (with `[DRIFT >5%]` suffix when out of tolerance). Show Plugin Info / Run Self-Test now list the two new secret keys. |
| 5.18.2 | 14-May-2026 | **VPP event post-mortem.** At VPP_ACTIVE → COOLING_OFF the plugin parses the per-event JSONL file just closed and writes nine new summary states to the `axleVppMonitor` device (`lastVppDate`, `lastVppExportKwh`, `lastVppPvKwh`, `lastVppMinPvW`, `lastVppMaxBatteryDischargeW`, `lastVppPeakGridExportW`, `lastVppPvSurvived`, `lastVppEmsModes`, `lastVppLogPath`). Pushover at event end carries the headline numbers AND a pre-formed *Ask Claude* block — JSONL path + four pointed questions ready to paste into Claude Code for analysis. One concise summary line goes to the Indigo Event Log; per-minute snapshots remain JSONL-only. Best-effort: summary failure WARNs but never blocks cool-off. |
| 5.18.1 | 14-May-2026 | **Quiet VPP event log.** Per-minute VPP snapshots moved out of the Indigo Event Log and into a per-event JSONL file at `<data_dir>/vpp_events/<YYYY-MM-DD_HHMM>.jsonl`. Each file: one `announcement` record (every field Axle's API returned), one `snapshot` record per minute (SOC, PV/battery/home/grid W, EMS mode + register, charge/discharge limits, plant state), one `event_ended` record (final export kWh). Event Log keeps only the key markers: announced / T-10min / RELEASED / WINDOW ACTIVE / event ended / REGAINED. |
| 5.18 | 14-May-2026 | **TRUE Axle handoff via Remote EMS release.** v5.16+v5.17 were stop-gap measures that had the plugin drive the export through Modbus mode selection (0x06 or 0x02+charge_limit=0) — both held Remote EMS enabled, blocking Axle's cloud channel and forcing us to pick among simple modes that can't do what Axle's cloud can (e.g. simultaneous battery discharge + PV charge). v5.18 properly releases Remote EMS at T-5min via `modbus.disable_remote_ems()`. With Remote EMS off the inverter follows Sigenergy's cloud commands directly — Axle now controls the inverter the way other Axle+Sigenergy users see, including keeping PV running through battery export. Pre-export step (T-4min mode 0x06) removed. Minute-by-minute countdown spam replaced by a single T-10min warning. New `>>> RELEASED CONTROL TO AXLE <<<` and `>>> REGAINED CONTROL <<<` markers for greppability. `_vpp_check_axle_release()` watches `emsWorkMode` for the "Self" string at event end and re-enables Remote EMS the moment Axle hands back. Verify loop skips writes during VPP_ACTIVE/COOLING_OFF (observe-only). |
| 5.17 | 14-May-2026 | **Daytime VPP follow-up to 5.16.** 5.16 fixed the export-stops-at-event-start bug by using mode 0x06 (Discharge ESS First) for the VPP window, and export resumed at 4 kW — but PV dropped to 0 W, curtailed by the inverter, because 0x06 makes the battery do all the discharge and with the grid capped at 4 kW there is nowhere for the solar to go. For a daytime VPP window the right mode is 0x02 (Max Self Consumption) with the charge limit pinned to 0 W: the solar can't be diverted into the battery, so it exits via the AC side and exports, and the battery only discharges if solar can't cover house load plus the grid cap. Net effect — 4 kW of grid export from the panels, the battery preserved for later, and no curtailment. Both registers are maintained through the event. |
| 5.16 | 14-May-2026 | **Critical — VPP event handoff fix.** On the 14-May morning event, pre-export started correctly at 07:56 with the battery exporting, then at 08:00:55 the transition to the active window called `set_self_consumption()` to clear the solar overflow cap before handing control to Axle. That call switched the inverter back to Max Self Consumption and stopped the export, and Axle could not override because Remote EMS was still locked to the plugin — so for the rest of the paid window the battery charged from solar instead of exporting, and 0 kWh flowed where about 10 kWh should have. The root cause was assuming Axle would take Modbus control after the transition and drive the discharge itself, when in practice Axle uses Sigenergy's cloud channel, which is blocked while Remote EMS is held. (Superseded by 5.17 for daytime windows and 5.18's true handoff.) |
| 5.2 | 12-May-2026 | Three small additions. **Web dashboard charts** via Chart.js (CDN): 24h/48h/7d SOC line + stacked energy bars (PV / export / import / home) and a 30-day daily totals chart. Backed by new `/api/history?hours=N` (half-hourly slots from SQLite) and `/api/daily?days=N` (from `daily_history.json`) endpoints. **Weekly data-dir backup** — every Monday midnight, tar.gz of `accumulators.json`, `daily_history.json`, `soh_history.json`, `home_load_profile.json`, `forecast_accuracy.json`, `energy_timeseries.db` and the openmeteo combined cache to `data_dir/data_backup/`. Retains 8 most recent (~2 months). **Auto-update notifier** — checks GitHub releases on startup (daily-cached in `pluginPrefs.lastUpdateCheck`), logs an INFO line if a newer plugin version is available. Silent on network failure. Also fixed a double-count bug in **Show Today's Energy Summary** and **Show Manager Status**: both used `correctedTodayKwh` (whole-day forecast) labelled as "kWh remaining" — they now use `remainingTodayKwh` (now → dusk). |
| 5.1 | 12-May-2026 | Site config consolidation. Plugin now publishes `sigen_site_config.json` to the Python Scripts folder on every startup and every PluginConfig save. The companion `openmeteo_battery_optimiser.py` script (v2.7) reads it so battery / inverter / tariff / resilience / flood-prevention values always match the plugin. Fixes a real drift bug: optimiser had `FLOOD_PREV_FORECAST_MULT = 4.0` while plugin used `3.0` — could give "no pre-drain" advisory while the plugin actually pre-drained. New `siteArraysJson` PluginConfig field — per-array PV specs as a JSON list (`{name, tilt, azimuth, kwp, shade}`), strict-shape parsed at startup with ERROR log on bad JSON (falls back to built-in default 4-array config). |
| 5.0 | 12-May-2026 | Major hardening + feature pass. Threading lock around shared state; web dashboard thread/socket cleanup on shutdown; SQLite connections timeout=5.0 + try/finally; Octopus rate-limit tracker (warn >80/hr, hard-stop >95/hr); Modbus writes are read-back-verified; JSON parse guards on every `response.json()`; Kraken token cleared on any failure path; `battery_manager.evaluate()` refactored (`_check_overrides` / `_check_resilience_buffer`) and the dead v4.0 night-export branch removed; site coordinates moved to PluginConfig (`siteLatitude`/`siteLongitude`); variable folder ID cached; `_ensure_plugin_log` throttled to hourly; `ServerApiVersion` bumped 3.0 → 3.8 (Indigo 2025.2 native); pymodbus pinned `>=3.0,<4.0`; EMS mode 0x07 ("AI Mode") added; Axle `forecast_dispatch_kwh` / `estimated_revenue_p` surfaced when present; auto-calibrated weekday/weekend kWh from live consumption profile; new menu items **Run Self-Test** and **Show Power Cut Log**; dashboard adds `tomorrow_surplus_kwh`, `tomorrow_revenue_gbp`, `forecast_accuracy`, `power_cut`; weekly battery State-of-Health snapshot with degradation warnings; power-cut event log; variable-driven pause/resume via `sigen_manager_paused`; Pushover quiet hours + configurable sound; 7-day rolling forecast MAPE summary at midnight. Companion `openmeteo_battery_optimiser.py` script rewrote 20:00 / 01:45 Pushover messages in full prose. `getActionConfigUiValues` now pre-populates Force-Import / Force-Export dialogs with live values. |
| 4.9 | 10-May-2026 | Plugin version is now read dynamically from Info.plist (`self.pluginVersion`) — single source of truth, no separate Python constant. Pushover action calls fixed: action ID `send` (was `sendPushover`), correct prop keys `msgTitle`/`msgBody`/`msgUser`/`msgPriority`/`msgSound`. Implemented `triggerStartProcessing` / `triggerStopProcessing` lifecycle so all custom Indigo trigger events (`emergencyImportTriggered`, `exportStarted/Stopped`, `vppAnnounced/Started/Ended`, `floodPreventionStarted/Stopped`, `powerCutLockoutStarted/Cleared`) now fire correctly via `indigo.trigger.execute(trigger_object)`. Moved hardcoded IPs and the Axle support email to `IndigoSecrets.py`: `SIGENERGY_IP`, `DASHBOARD_HOST`, `AXLE_SUPPORT_EMAIL`, `PUSHOVER_USER_TOKEN`. Each has a PluginConfig fallback and logs an ERROR if neither source is set. Dashboard URL auto-detects the LAN IP via socket when no override is configured. Swallowed-failure log levels promoted from WARNING to ERROR (trigger execute fail, dashboard stop, VPP email, Timeseries DB init/write). `_init_modules()` now initialises forecast/Octopus/Axle before the inverter-IP check so a missing IP only skips Modbus instead of crashing startup. |
| 4.8 | 03-May-2026 | Remove 15-minute heartbeat log — only log on action change (web dashboard covers live status). |
| 4.7 | 02-May-2026 | Remove Solcast code/variables (Open-Meteo only); raise FLOOD_PREV_FORECAST_MULT 2.0 → 3.0; delete test_overnight.py. |
| 4.6 | 01-May-2026 | Half-hourly SQLite energy logging for TariffAnalyser feed. |
| 4.5 | 30-Apr-2026 | Rename to SigenEnergyManager + critical bug fixes and polish. |
| 4.4 | 29-Apr-2026 | **Flood prevention overnight pre-drain.** When SOC is above 55% and tomorrow's solar forecast is at least twice the daily need, the battery is pre-drained to 40% overnight at the 4 kW DNO export cap, creating morning headroom so export runs uninterrupted through peak hours. Uses the discharge cutoff register (40048) as a hardware stop so the battery halts automatically at 40%, with the register verification skipping the cutoff check while pre-drain is active and the dawn-break transition resetting it if the run is interrupted. 43 tests, 14 of them new. |
| 3.1 | 06-Apr-2026 | **Energy summary variables, storm watch and power-cut lockout.** Auto-creates nine Indigo variables in the `Sigenergy` folder (PV, import, export, home, self-sufficiency, peak and minimum SOC, decision action and reason), updated every 30 minutes and at midnight. Storm watch polls the MeteoAlarm feed every 2 hours and filters by polygon to confirm the warning actually covers Medomsley, charging to 50% and suspending export on a yellow warning and to 80% on amber or red, with Pushover alerts on escalation and all-clear, acting only when the storm is due within 24 hours. Power-cut lockout detects off-grid to on-grid transitions and suppresses export for 4 hours afterwards as a precaution. Dawn SOC target self-heals to a 15% minimum on startup. Solar overflow export becomes real-time — the battery gets exactly enough charge rate to fill by dusk and all surplus above that exports immediately, which starts export earlier on sunny days. |
| 2.9 | 31-Mar-2026 | **Critical — night export no longer fires in daylight.** Night export (mode 0x06) could run in full daylight when today's dawn time was missing from the forecast data, for instance after a restart before the forecast had been fetched or after an API failure. With daytime detection returning false it fell through to export logic, suppressing solar to 0 W and discharging the battery to the grid in daylight. If today's dawn time is absent it now falls back to a clock-based safe window of 07:00–21:00, so export is always blocked during daylight regardless of forecast availability. Also includes EMS mode mismatch detection (2.8) — the register is read every 15 minutes and any drift corrected, closing the case where a restart left the inverter stuck in an export mode — and a daily rotating plugin log file with 14-day retention (2.7). 53 tests. |
| 2.6 | 31-Mar-2026 | Solar overflow SOC gate: export now only starts once battery SOC reaches 40%, preventing the algorithm from exporting aggressively while the battery is still low after overnight discharge. Solcast Indigo variables (solcast_today_kwh, solcast_tomorrow_kwh, solcast_last_updated) now populated on every Solcast refresh — were previously always 0.0. P10 forecast data removed from all modules: was dead code never used in any decision logic since v1.3; removes _hourly_p10_today and _hourly_p10_tomorrow from solcast.py, forecast_p10 from ManagerSnapshot, and _sum_tomorrow_forecast() static method from battery_manager.py. P90 also removed. |
| 2.5 | 30-Mar-2026 | Fix: v2.4 ineffective because dawn_target_pct and health_cutoff_pct are both 10% so changing threshold had no effect. Root cause: when tomorrow is sunny (forecast >= daily consumption) import is never needed regardless of dawn SOC — the inverter's discharge cutoff register (40048) already prevents the battery going below health_cutoff_pct. Import now fully suppressed on sunny days. Only on poor solar days (tomorrow forecast < daily consumption) does the dawn_target buffer apply. |
| 2.4 | 30-Mar-2026 | Fix: overnight grid import triggered unnecessarily when battery was low but tomorrow has good solar. Import threshold is now solar-aware: if tomorrow's bias-corrected Solcast P50 >= daily consumption, import only triggers if projected dawn SOC would hit the hardware cutoff floor (inverter stops discharging anyway). On poor solar days the full dawn_target buffer is maintained as before. Eliminates small unnecessary top-up imports on sunny days. |
| 2.3 | 30-Mar-2026 | Fix: dawn viability check incorrectly triggered grid import during daylight when battery SOC was low but abundant solar remained. _check_dawn_viability() now credits remaining bias-corrected Solcast P50 solar (net of home consumption to dusk) to current SOC before projecting overnight drain to next dawn. Import is only triggered if the battery genuinely cannot reach dawn target even after today's remaining solar is accounted for. |
| 2.2 | 30-Mar-2026 | Replaced fixed SOC threshold overflow logic with forecast-based dynamic export. Each evaluation: sums remaining bias-corrected Solcast P50 from now to dusk, subtracts expected home consumption and battery headroom to reach 100%, spreads any genuine surplus evenly across remaining daylight hours up to 4 kW DNO cap. Battery reaches 100% as near to dusk as solar allows while exporting continuously from first surplus detection. Mode stays 0x02 throughout; PV never suppressed. Adds house_load_watts and bias_factor to ManagerSnapshot. |
| 2.1 | 30-Mar-2026 | New: daytime solar overflow export. When SOC >= 80% during daylight, caps HOLD_ESS_MAX_CHARGE (register 40032) to 2 kW so PV surplus that can't enter the battery flows to grid instead. At SOC >= 90% caps to near-zero (200 W). Releases below 75% SOC (hysteresis). Mode stays 0x02 throughout — PV is never suppressed. Previous attempt used mode 0x06 (Discharge ESS First) which causes inverter to curtail PV. _verify_ems_registers() updated to respect the charge cap during overflow. |
| 2.0 | 29-Mar-2026 | Fix: "Cannot find Tracker product code" warning still firing after v1.8. Root cause: two bugs. (1) _detect_tariff_from_account() returned the FIRST meter point regardless of whether it was import or export — accounts with an export MPAN (OUTGOING tariff) had it returned first, classifying as TARIFF_UNKNOWN and bypassing the account path. Fixed: filter by self.mpan when set, only check matching meter point; skip TARIFF_UNKNOWN results. (2) _probe_product_by_prefix() used is_variable=True filter — Tracker (SILVER-*) is a daily-changing flat rate, not flagged is_variable by Octopus, so it was silently excluded. Fixed: removed is_variable filter. |
| 1.9 | 29-Mar-2026 | Fix: Night export still blocked after restart despite v1.7 disk-cache pre-warm. Root cause: _tick() ran manager (step 2) before Solcast refresh (step 3), so latest_forecast_data was still {} on the first evaluation. Two fixes: (1) startup() now calls _refresh_solcast() immediately after _init_modules() to pre-populate latest_forecast_data from the disk cache before any manager evaluation; (2) Solcast refresh moved before manager in _tick() as a permanent ordering guarantee. |
| 1.8 | 29-Mar-2026 | Fix: "Cannot find Tracker product code" warning fired every 30 minutes. Root cause: _probe_tracker_product_code() guessed TRACKER-VAR-YY-MM-DD dates at 30-day intervals, which never match real Octopus product release dates. Replaced with _probe_product_by_prefix(("SILVER","TRACKER")) -- the same public-products-listing approach used by Go/Flux/Agile. _probe_tracker_product_code() deleted. |
| 1.7 | 29-Mar-2026 | Fix: Solcast combined forecast now pre-warmed from disk cache on startup. Previously every plugin restart cleared the in-memory forecast, causing correctedTomorrowKwh=0.0 and silently blocking night export until the next live API fetch (up to 2.4h). _load_combined_cache() reads solcast_combined_cache.json at __init__ time; logs a warning if the cache is >7.2h old. _refresh_solcast() now logs an explicit WARNING if forecastStatus contains 'No data' or correctedTomorrowKwh==0.0, so the blockage is visible in the event log. |
| 1.6 | 29-Mar-2026 | Fix: Intelligent Go cheap window corrected to 23:30-05:30 (was 00:30, same as standard Go). Fix: Intelligent Flux has no narrow cheap window -- entire 21h outside peak (16:00-19:00) is cheap, so window now modelled as 19:00-16:00 wrap-around instead of incorrectly using Flux's 02:00-05:00. Battery manager will now import immediately at any non-peak time on iFlux rather than waiting for a 02:00 window that doesn't exist. |
| 1.5 | 29-Mar-2026 | VPP: discharge cutoff register (40048) raised at VPP_ANNOUNCED rather than PRE_CHARGING. Floor = dawn target + full event export energy, so the battery reserve is protected from the moment an Axle event is announced. Cutoff restored on cancellation (event disappears while ANNOUNCED/PRE_CHARGING) as well as on COOLING_OFF completion. |
| 1.4 | 29-Mar-2026 | Fix: night export stop condition replaced -- PV watts reads 0W in Discharge ESS First mode so solar could never trigger a stop. Export now stops at Solcast-predicted sunrise (dawn_times) instead. Fix: night_export() sets HOLD_ESS_MAX_DISCHARGE=10000W and relies on the inverter's own DNO cap for grid limiting -- battery now supplies house load + 4kW to grid simultaneously. 54 unit tests, all pass. |
| 1.3 | 29-Mar-2026 | Night export feature: force-discharge surplus to grid at night when SOC is high and tomorrow's forecast is good. Fix: persistent Modbus register (HOLD_ESS_MAX_DISCHARGE / HOLD_ESS_MAX_CHARGE) left at reduced value after force-discharge, capping battery output in self-consumption mode. Fix: tomorrow viability check now uses bias-corrected P50 (correctedTomorrowKwh x 60%) instead of P10. New: test_sigenergy_modbus.py (16 Modbus register tests). 49 unit tests total, all pass. |
| 1.2 | 27-Mar-2026 | Fix: inverter capacity corrected to 10 kW. |
| 1.1 | 27-Mar-2026 | Fix: nighttime grid import caused by export limit register set to 0W when export stops. Fix: symmetric hysteresis on export restart (10% deadband). 48 unit tests all pass. |
| 1.0 | 26-Mar-2026 | Initial release. Replaces SigenergySolar v3.1. |

---

## Requirements

- Indigo 2025.2 or later (Python 3.13)
- Sigenergy inverter with Modbus TCP enabled (port 502)
- Python packages: `pymodbus>=3.0`, `pytz>=2024.1` (auto-installed from `requirements.txt`)
- Optional: Octopus Energy API key (tariff-aware import scheduling)
- Optional: Axle VPP account credentials
- Optional: Pushover Indigo plugin (storm + VPP alerts)
- Solar forecast uses Open-Meteo — no API key required

---

## Installation

1. Go to the [Releases](https://github.com/Highsteads/SigenEnergyManager/releases) page
   and download `SigenEnergyManager.indigoPlugin.zip`
2. Unzip the downloaded file -- you will get `SigenEnergyManager.indigoPlugin`
3. Double-click `SigenEnergyManager.indigoPlugin` -- Indigo will install it automatically

---

## Configuration

### Credentials — `IndigoSecrets.py` vs `IndigoSecrets_example.py`

There are **two** files involved, and only one of them holds your real values:

| File | Purpose | Contains real data? | Committed to GitHub? |
|------|---------|---------------------|----------------------|
| `IndigoSecrets.py` | The **working file** the plugin reads at runtime. Lives at `/Library/Application Support/Perceptive Automation/IndigoSecrets.py`. Keep a backup in a password manager. | YES | **NO** — listed in `.gitignore`, never pushed. |
| `IndigoSecrets_example.py` | **Template only** — empty placeholders so users know which keys to set. Shipped inside the plugin bundle. | NO | YES — public on GitHub. |

**Setup:**

1. If you already have `IndigoSecrets.py`, just add the keys below to it.
2. If you do not, copy `IndigoSecrets_example.py` (inside the plugin bundle) to
   `/Library/Application Support/Perceptive Automation/`, rename it to `IndigoSecrets.py`,
   and fill in your values.
3. Or skip `IndigoSecrets.py` entirely and enter values via the plugin's configuration
   dialog (Indigo menu → Plugins → Sigenergy Manager → Configure). `IndigoSecrets.py`
   wins over the dialog when both are set.

If neither source provides a value the plugin logs an ERROR for the missing item
and skips just that feature (e.g. no inverter IP → Modbus skipped, but Octopus
and Open-Meteo still run).

**Keys read by SigenEnergyManager:**

```python
# Octopus Energy (tariff data)
OCTOPUS_API_KEY     = "sk_live_..."
OCTOPUS_ACCOUNT     = "A-XXXXXXXX"
OCTOPUS_MPAN        = "1300000000000"
OCTOPUS_SERIAL      = "00X0000000"

# Octopus export MPAN (v5.19 — optional, enables the Export Sync dashboard card)
OCTOPUS_EXPORT_MPAN   = ""
OCTOPUS_EXPORT_SERIAL = ""

# Site coordinates (v5.21.1 — required somewhere; PluginConfig fallback)
# Example uses Big Ben, London — replace with your own roof's coordinates.
LATITUDE            = 51.5007
LONGITUDE           = -0.1246

# Sigenergy inverter (Modbus)
SIGENERGY_IP        = "192.168.x.x"

# Axle VPP (optional)
AXLE_API_KEY        = ""
AXLE_SUPPORT_EMAIL  = ""   # recipient for VPP "inverter not released" escalation

# Pushover notifications (optional — Pushover Indigo plugin must also be installed)
PUSHOVER_USER_TOKEN = ""

# Web dashboard (optional — blank = auto-detect LAN IP via socket)
DASHBOARD_HOST      = ""
```

### Plugin preferences

| Setting | Description |
|---------|-------------|
| Inverter IP | Sigenergy inverter LAN address (e.g. 192.168.1.49 — find it in your router or the Sigenergy app) |
| Modbus port | Inverter Modbus TCP port (default 502) |
| Plant slave address | Modbus slave address for plant data (default 247) |
| Inverter slave address | Modbus slave address for inverter data (default 1) |
| Poll interval | Inverter data poll frequency in seconds (default 60) |
| **Site latitude** *(v5.0, secrets-aware v5.21.1)* | Degrees N for Open-Meteo forecast. Read from `IndigoSecrets.py` (`LATITUDE`) first, this field next. **No built-in default — blank means "skip the forecast feature"**. Example: Big Ben, London = 51.5007. |
| **Site longitude** *(v5.0, secrets-aware v5.21.1)* | Degrees E, negative for W. Read from `IndigoSecrets.py` (`LONGITUDE`) first, this field next. No built-in default. Example: Big Ben, London = -0.1246. |
| **PV array specs (JSON)** *(v5.1)* | Optional JSON list of `{name, tilt, azimuth, kwp, shade}` — leave blank to use the built-in 4-array default. Bad JSON falls back to the default and logs an ERROR. |
| Battery capacity (kWh) | Total usable battery capacity (default 35.04) |
| Battery efficiency | Round-trip efficiency 0-100% (default 94) |
| Inverter max kW | Inverter rated output power -- sets battery discharge ceiling (default 10) |
| Dawn SOC target (%) | Summer resilience buffer — minimum SOC kept overnight on flat-rate tariffs (default 10%) |
| Winter buffer (%) *(v4.x)* | Higher overnight floor for Oct-Mar (default 20%) |
| Battery health cutoff (%) | Hardware discharge floor (default 1%) |
| Weekday kWh / Weekend kWh | Daily consumption estimate (auto-calibrated from live inverter data in v5.0+; user values only used until the 48-slot profile is populated) |
| Export enabled | Enable grid export (requires active export MPAN) |
| Max export kW | DNO export cap — used at startup to initialise the export limit register (default 4 kW) |
| VPP (Axle) enabled | Enable Axle Virtual Power Plant integration |
| **Pushover sound** *(v5.0)* | Sound for storm + VPP alerts (default `vibrate`; full Pushover sound list available) |
| **Pushover quiet hours** *(v5.0)* | Start / End times (HH:MM). INFO-priority alerts are suppressed during this window; HIGH-priority (storm amber/red, VPP release failure) always fire. |
| Dashboard host | Blank = auto-detect LAN IP; otherwise hostname / IP visible to other devices |
| Show debug logging | Verbose log output |

Note: Octopus tariff type (Tracker/Go/Flux/iGo/iFlux/Agile) is detected
automatically from your Octopus account -- no manual selection required.

### Shared site config — `sigen_site_config.json` *(v5.1)*

The plugin publishes a small JSON file every startup and every PluginConfig save:

```
/Library/Application Support/Perceptive Automation/Python Scripts/sigen_site_config.json
```

It contains the battery / inverter / tariff / resilience / flood-prevention values
the plugin is currently using. The companion `openmeteo_battery_optimiser.py`
script (used by the 20:00 EVENING and 01:45 OVERNIGHT scheduled Pushover messages)
reads this file at runtime instead of hardcoding its own copies — so the plugin
and the advisory script can no longer drift apart on tunable thresholds.

If the file is absent (fresh install, or the script is being used without the
plugin) the script falls back to a built-in copy of the same defaults. Bad JSON
is treated the same as "absent".

### Companion script — `openmeteo_battery_optimiser.py`

Lives at:

```
/Library/Application Support/Perceptive Automation/Python Scripts/openmeteo_battery_optimiser.py
```

Driven by two Indigo schedules — `openmeteo_battery_optimiser At 20:00` and
`openmeteo_battery_optimiser At 01:45`. Sends a friendly full-prose Pushover
message describing tonight's plan (EVENING) and a short overnight check-in
(OVERNIGHT). Advisory only — all actual battery control is in the plugin.

Reads:
- `openmeteo_forecast.json` (published by the plugin's forecast module)
- `octopus_consumption_profile.json` (published by the plugin's Octopus module)
- `sigen_site_config.json` (published by the plugin, v5.1+)

---

## Core logic

### Self-sufficiency first

Every 60 seconds the plugin:

1. Reads live data from the inverter via Modbus TCP
2. Projects battery SOC at the next dawn using the Open-Meteo dawn time and a
   48-slot half-hourly consumption profile (auto-calibrated from live data)
3. If projected SOC at dawn < dawn target: schedules or starts a grid import
4. During daylight, once SOC >= 40%: caps HOLD_ESS_MAX_CHARGE so PV surplus exports
   to grid continuously, reaching 100% SOC as near to dusk as solar allows (see below)
5. If it is night and battery has surplus above the dawn floor: force-discharges to grid,
   provided tomorrow's solar forecast is good enough to recharge (see below)
6. If tomorrow's forecast is high enough that a full battery would choke off morning
   export: pre-drains overnight (flood prevention)
7. Otherwise: holds in Max Self Consumption mode (Remote EMS 0x02)

Storm warnings (MeteoAlarm amber/red) raise the dawn target and suppress export
for the duration of the alert. A 4-hour export lockout is enforced after any
grid restoration (power-cut recovery).

### Night export

When there is no solar generation (PV < 500W), the plugin can export battery surplus
directly to grid at the configured max export rate (typically 4 kW). Three conditions
must all be true:

| Condition | Detail |
|-----------|--------|
| **Night** | Current time is outside the daytime window (before today's Open-Meteo dawn, or more than 14h after it) |
| **Surplus** | Projected SOC at dawn > dawn target + 1 kWh safety buffer |
| **Tomorrow viable** | `correctedTomorrowKwh x 0.6 >= daily_consumption_kWh` |

The tomorrow viability check uses the **bias-corrected** Open-Meteo estimate
(`correctedTomorrowKwh`) at 60% confidence -- meaning "even if tomorrow comes in
40% below our best estimate, the battery will still be recharged". The bias
correction is the magnitude-conditional 5-band scheme introduced in v5.21
(see Solar forecasting above).

**Why PV watts is not used as the night/day indicator:** In Discharge ESS First mode
(0x06) the Sigenergy inverter suppresses PV generation to 0W, so `pvPowerWatts`
reads zero regardless of actual solar. A PV threshold check would never fire while
exporting. Sunrise is instead detected from the Open-Meteo-predicted `dawn_times`.

**Daytime window:** Export is blocked for 14 hours after today's dawn time (e.g.
dawn 07:00 -> blocked until 21:00, then nighttime resumes and export can start again).

Night export stops automatically when:
- Today's Open-Meteo dawn time is reached (sunrise)
- Battery surplus drops below the minimum threshold
- Tomorrow's forecast deteriorates below the viability check

Example log message:

```
[Manager] Night export: 15.3 kWh surplus above dawn floor.
          Tomorrow forecast 25.1 kWh (60% = 15.1) >= daily 14.4 kWh.
          Exporting 4.0 kW
```

### Persistent Modbus register protection

The Sigenergy inverter retains certain registers across mode changes. Specifically,
`HOLD_ESS_MAX_DISCHARGE` (40034) and `HOLD_ESS_MAX_CHARGE` (40032) set during a
`force_discharge` or `force_charge` call persist as power caps even after returning
to Self Consumption mode.

The plugin guards against register drift at three layers:

1. **On every mode change** -- `set_self_consumption()` always resets both registers to
   10000W (inverter maximum) before setting the mode
2. **On startup** -- both registers are explicitly written to 10000W before any other
   Modbus operation
3. **Every 15 minutes** -- `_verify_ems_registers()` reads back both registers; if
   either has drifted it is corrected and logged with a warning

### Tariff-aware import scheduling

| Tariff | Import strategy |
|--------|----------------|
| Tracker | Import now, or defer to midnight if tomorrow is 10%+ cheaper |
| Go / iGo | Defer to cheap window (00:30-05:30) if battery can reach it |
| Flux / iFlux | Defer to cheap window (02:00-05:00) |
| Agile | Find cheapest 30-min slot before dawn |

### VPP (Axle) integration

State machine: `IDLE` → `ANNOUNCED` → `PRE_CHARGING` → `ACTIVE` → `IDLE`.

The plugin **drives the export itself** for each announced window and does not rely on
Axle's real-time cloud dispatch. Axle settle on the meter reading, so whatever the meter
records as exported during the window counts towards the event in exactly the same way —
Axle confirmed this in writing after the 10-Jun-2026 event, when a SigEnergy-API fault on
their side meant the battery never discharged on its own. Self-driving sidesteps that
entirely, and it stacks the Axle rate with the Octopus Outgoing rate on the same exported
kWh.

**Pre-event:**
- On announcement (a day ahead) the plugin reserves the event's export energy, so an
  overnight or morning event always has the kWh ready and the overnight optimiser will
  not drain below it.
- At T-30min it sets the discharge cutoff to the next-day reserve floor — the health
  floor for a daytime event (solar will refill it) or the dawn reserve for a night event
  — so the battery only ever exports what it can spare. There is no grid import to feed
  an export, pre-charge stays solar-only until a cheap overnight EV tariff makes
  import-to-export worthwhile.

**During the window (T-2min → end+2min):**
- On entry to `ACTIVE` the plugin calls `night_export()` (Remote EMS mode 0x06, discharge
  limit at inverter maximum) and the inverter's commissioned DNO cap holds grid export at
  4 kW. The manager's `ACTION_VPP_EXPORT` override re-asserts this idempotently on every
  tick, so a transient drop self-heals.
- The window runs on the plugin's own clock from the stored event times. A transient blip
  in Axle's API mid-event cannot cut the export short, and a genuine cancellation before
  the window stands the plugin back down.
- Per-minute snapshots are appended to `<data_dir>/vpp_events/<YYYY-MM-DD_HHMM>.jsonl`
  (SOC, PV/battery/home/grid W, EMS mode + register, limits, plant state) — the Indigo
  Event Log stays clean.

**Event end (+2min):**
- `_end_vpp_export()` restores Max Self Consumption and the health-floor discharge cutoff,
  returns the state machine to `IDLE`, and writes the post-event summary states to the
  `axleVppMonitor` device plus a Pushover carrying the headline numbers.
- The discharge cutoff register is restored to the health floor.

---

## Historical bug fixes (v1.1 – v1.4)

*Kept as a deep-dive on the early Modbus / register-persistence quirks. For
recent changes (v5.x) see the Version history table at the top of this file
and the per-version notes in the repo `CLAUDE.md`.*

### v1.4 -- Night export stop condition permanently blind (critical)

**Symptom:** Night export ran through sunrise and into the morning without stopping.

**Root cause:** The stop condition checked `pvPowerWatts >= 500W`. In Discharge ESS
First mode (0x06) the inverter suppresses PV output to 0W regardless of actual solar
generation, so this condition could never fire while export was active.

**Fix:** PV watts check replaced with a dawn_times check. The Solcast-predicted
sunrise time for today is stored in `snapshot.dawn_times`. Export is stopped (and
blocked from starting) for 14 hours after today's dawn time. At dawn + 14h
(typically 21:00) the nighttime window reopens and export can start again.

### v1.4 -- Night export limited to 4kW total instead of 4kW to grid

**Symptom:** With house consuming 0.9kW and export set to 4kW, only ~3.1kW reached
the grid. Battery was discharging at exactly 4kW total.

**Root cause:** `force_discharge(4000)` wrote `HOLD_ESS_MAX_DISCHARGE = 4000W`,
capping total battery output. House load consumed ~0.9kW of that, leaving 3.1kW
for the grid.

**Fix:** New `night_export(inverter_max_w)` method sets `HOLD_ESS_MAX_DISCHARGE =
10000W` (inverter maximum) and relies on the inverter's own DNO export cap (set
during commissioning) to limit grid flow to 4kW. Battery now discharges at
`house_load + 4kW`, so the grid always receives the full 4kW regardless of
home consumption.

### v1.3 -- Persistent register caps battery output (critical)

**Symptom:** Battery limited to 1.8 kW output in Self Consumption mode; inverter
importing 1.3 kW from grid even with 78% SOC.

**Root cause:** `HOLD_ESS_MAX_DISCHARGE` (Modbus 40034) is a persistent register that
survives Remote EMS mode changes. A `force_discharge(2000)` call during staged export
testing left 2000W in this register. When the plugin returned to Self Consumption mode
it only changed the mode register, never resetting the discharge cap.

**Fix:** `set_self_consumption()` now always resets both `HOLD_ESS_MAX_DISCHARGE` and
`HOLD_ESS_MAX_CHARGE` to 10000W before engaging the mode. Startup performs the same
reset. A 15-minute verification loop detects and corrects any future drift.

### v1.3 -- Night export never triggered (tomorrow viability check)

**Symptom:** Night export never started despite adequate SOC and good solar forecast.

**Root cause:** The tomorrow viability check used `_hourly_p10_tomorrow` (hourly P10
data by date string), but the Solcast module only populated `_hourly_p10_today`. The
`_sum_tomorrow_forecast()` helper searched for tomorrow's date in today's dict, found
nothing, returned 0 kWh, and the check `0 < 14.4` blocked export every night.

**Fix:** The viability check was switched from P10 to the bias-corrected P50
(`correctedTomorrowKwh x 0.6`), which is far more appropriate for this decision
-- see Night export above. P10 has since been removed entirely from the codebase.

### v1.1 -- Nighttime grid import at high SOC

**Symptom:** Battery at 78% SOC importing 1.3 kW from grid at night while discharging
at only 1.8 kW (home load 3.1 kW).

**Root cause:** When export stops, the old code wrote `HOLD_GRID_MAX_EXPORT_LIMIT = 0W`
to Modbus register 40038. Sigenergy interprets 0W as a hard constraint meaning "never
let net power cross zero into export territory". To guarantee compliance, the inverter
deliberately targets a small positive grid import rather than fully covering the home
load from battery.

**Fix:** When stopping export at night (PV <= 500W), the export limit register is now
set to the DNO cap (4000W) rather than 0W.

### v1.1 -- Export cycling

**Symptom:** Export starting and stopping every 15 minutes in the evening as SOC
oscillated around 80%.

**Fix:** Restart from tier 0 now requires SOC >= stage1 + 5% (symmetric 10% deadband).

---

## Device types

| Type ID | Display name | Purpose |
|---------|--------------|---------|
| `batteryManager` | Battery Manager | Main control device -- one per system |
| `sigenergyInverter` | Sigenergy Inverter | Real-time PV, battery, grid, home power readings |
| `solarForecast` | Solar Forecast | Today/tomorrow Open-Meteo forecast (bias-corrected, per-array model) |
| `tariffMonitor` | Tariff Monitor | Current unit rate, standing charge, tomorrow's rate, export rate |
| `axleVppMonitor` | Axle VPP Monitor | VPP event state machine, SOC management, post-event summary states (`lastVpp*`) |

---

## Menu items

Available from Indigo: **Plugins → Sigenergy Manager** menu.

| Item | What it does |
|------|--------------|
| Refresh All Data Now | Forces an immediate forecast + Octopus rates + manager re-evaluation |
| Show Manager Status | Prints current manager decision, snapshot and tariff data |
| Show Daily History (Last 7 Days) | Reads `daily_history.json` and prints the last 7 days |
| Show Current Tariff Rates | Prints today / tomorrow rates (import + export) |
| Show VPP Status | Axle VPP state machine summary |
| Show VPP Export Summary | Cumulative VPP earnings since plugin install |
| Show Today's Energy Summary | PV / Import / Export / Home / SOC peaks for today |
| **Run Self-Test** *(v5.0)* | Verifies Modbus / Octopus / Open-Meteo / Axle / Pushover / secrets resolution in one report |
| **Show Power Cut Log** *(v5.0)* | Last 20 grid-status transitions (rolling 100-entry log) |
| Open Web Dashboard | Opens the local dashboard URL in the default browser |
| Toggle Debug Logging | Quick on/off without opening the prefs dialog |
| Toggle Timestamps in Log (on/off) | Turns the `[HH:MM:SS.mmm]` log prefix on or off |
| Show Plugin Info | Re-prints the startup banner (version, paths, API version) |

## Indigo variables created by the plugin

| Variable | Folder | Purpose |
|----------|--------|---------|
| `sigen_manager_paused` *(v5.0)* | Sigenergy | Set to `true` / `1` / `yes` / `on` from any Indigo automation to pause the battery manager; set back to `false` to resume. The manager device's `managerStatus` state mirrors this. |
| `battery_optimiser_status` | Sigenergy | Status line from the optimiser script (`EVENING 20:00 | Tracker | …`) |
| `battery_import_kw` / `battery_import_kwh` | Sigenergy | Plan from the optimiser script |
| various `sigen_*` / `elec_*` | Sigenergy | Energy summary values written every 30 min |

## Custom events

Custom Indigo triggers that fire from the plugin (use **New Trigger** → **Plugin Event**):

| Event ID | Fires when |
|----------|-----------|
| `emergencyImportTriggered` | Battery cannot reach dawn — grid import initiated |
| `exportStarted` / `exportStopped` | Battery export to grid starts / stops |
| `vppAnnounced` / `vppStarted` / `vppEnded` | Axle VPP event lifecycle |
| `floodPreventionStarted` / `floodPreventionStopped` | Overnight pre-drain export lifecycle |
| `powerCutLockoutStarted` / `powerCutLockoutCleared` | 4-hour export lockout after grid restoration |

## Web dashboard

Local HTTP dashboard runs on port **8179** by default. Visit
`http://<your-indigo-host>:8179/` for a live view, now with **Chart.js charts**
(v5.2): a 24h/48h/7d SOC line + stacked energy bars (PV / export / import / home)
and a 30-day daily totals bar chart. Chart.js ships inside the plugin and is
served from `/chart.js` (v5.43.1), so the charts draw during a broadband
outage — which is exactly when you check the battery. The JSON API:

| Endpoint | Returns |
|----------|---------|
| `/api/status` | Live snapshot (see table below) — updates every 30s |
| `/api/history?hours=N` *(v5.2)* | Half-hourly slots from SQLite for last N hours (max 168) — used by the SOC + stacked-bar charts |
| `/api/daily?days=N` *(v5.2)* | Per-day totals from `daily_history.json` for last N days (max 800 since v5.52.0, was 365) — used by the daily chart |
| `/api/export-sync` *(v5.19)* | Sigenergy vs Octopus daily-export comparison for the last 7 settled days (D-3 to D-9). Returns per-row status (`ok` / `drift` / `unsettled` / `fetch_error` / `no_sigen_record`) plus a summary block. Powers the **Export Sync** dashboard card. |

`/api/status` returns:

| Section | Contains |
|---------|----------|
| `battery` | SOC, power |
| `solar` | Today / tomorrow kWh, bias factor, remaining today, **tomorrow surplus kWh**, **tomorrow revenue £**, **export rate p** *(v5.0)* |
| `grid` | Power, status |
| `home` | Load watts |
| `decision` | Manager action + reason |
| `tariff` | Name, product code, today/tomorrow p |
| `today_summary` | PV, import, export, home, peak/min SOC, self-sufficiency % |
| `vpp` | State machine status |
| `storm` | Current MeteoAlarm level |
| `flags` | export_active, solar_overflow_active, import_active, modbus_connected |
| `hourly_forecast` | Per-hour kWh today |
| **`power_cut`** *(v5.0)* | Last 10 transitions, `ongoing`, `lockout_active` |
| **`forecast_accuracy`** *(v5.0)* | 7-day rolling MAPE, mean factor, over/under counts |

---

## Unit tests

```bash
cd SigenEnergyManager.indigoPlugin/Contents/Server\ Plugin
python3 -m pytest -q          # or: python3 -m unittest discover -p 'test_*.py' -v
```

**415 tests** across eight test files, all passing without Indigo installed —
`indigo`, `pymodbus` and `requests` are mocked, so nothing touches the network
or the inverter:

| File | Tests | Coverage |
|------|-------|---------|
| `test_plugin.py` | 179 | Config coercion (`_as_float` / `_as_int`), VPP export drive and its start latch, power-cut export lockout and SOC floor, solar-refill release, safe-baseline disengage, whole-house cost card and settlement, cost + energy-summary variable writes, inverter device updates |
| `test_battery_manager.py` | 92 | Dawn viability, import scheduling (Tracker/Go/Flux/Agile), flood prevention (refill-day + VPP-aware cases), legacy migration paths, VPP suppression, seasonal logic, tariff midnight handling |
| `test_sigenergy_modbus.py` | 43 | `set_self_consumption()` register resets, force_discharge/force_charge sequences, read_discharge_limit/read_charge_limit, export limit validation, signed decode boundaries, write-back verification |
| `test_openmeteo_forecast.py` | 36 | kWh-weighted bias correction formula, magnitude-conditional band table, per-day factor application across hourly slots, day-shifted cache rejection, live-data replay |
| `test_octopus_api.py` | 31 | Kraken account-ledger parsing, per-day import and gas consumption, m³→kWh calorific conversion, error paths and classification |
| `test_storm_watch.py` | 14 | MeteoAlarm CAP parsing on the current schema and the legacy fallback, polygon filtering, empty feed, schema-drift guard |
| `test_axle_api.py` | 13 | `get_next_event` against a misbehaving upstream — null body, 204, 401, malformed JSON, odd timestamps, missing optional fields |
| `test_concurrency.py` | 7 | Locking contract — network I/O runs unlocked, the evaluate/verify/act path stays locked, outage back-off, callback latency |

---

## Hardware reference

Developed and tested on:
- 14.25 kWp solar (30 panels, 4 arrays)
- Sigenergy 10 kW hybrid inverter
- 35.04 kWh battery (4 x 8.76 kWh SigenStor)
- DNO export cap: 4 kW
- Tariff: Octopus Tracker

## Authors & licence

Vibed into existence by **CliveS**, who knew what he wanted, argued until he got it, and tested it on a real house. Typed at inhuman speed by **Claude** (Anthropic), who mostly did as it was told.

© 2026 CliveS · [MIT licence](LICENSE) — copy it, fork it, bend it, break it, fix it, ship it. If it breaks, you get to keep both pieces.
