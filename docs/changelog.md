---
title: Version history
nav_order: 9
---

# Version history

The newest version is at the top. A fuller, technical record of the recent versions is in the [developer changelog](plugin-changelog.md).

## 5.129.2 — 1 October 2026

- **A day recorded before Octopus has published its prices is no longer undervalued in the money tables.** With the October Flux export prices still unpublished, 1 October's exports would have been recorded at the last export price the plugin held (the 9.7p day rate) when most of them went out in the 4pm to 7pm peak at 27.7p. Such a day is now valued at the last published day's prices, the same figures the strategy plans on, and marked "provisional". The day Octopus publishes, the plugin weighs it again at the real prices, replaces the figure and, where the day's whole-house cost has already been settled, works the export income and the net out again. The Event Log says so when it happens.

## 5.129.1 — 1 October 2026

- **When Octopus has not published one side of the Flux prices yet, the strategy plans on the last published day's prices until it does.** From midnight on 1 October Octopus published October's import prices but no export prices, in any region, so the strategy refused to plan all day: no peak sale, and nothing to serve that evening's Saving Session. Now a side that stops short of the other is filled in by repeating its last published day, with the bands on the same clock times, and the Event Log warns once with the prices it is using. It never does this when Octopus has published neither side. The day's money is still valued only at prices Octopus actually published.

## 5.129.0 — 30 September 2026

- **The 2am charge now brings the battery up to at least 50% every night, except on a day with a booked free hour.** On 30 September the forecast was 31 kWh and the day brought about 19, the battery left the cheap window at 42%, and the 4pm sale ran out after about 2 kWh of a possible 12. Replayed over every day on record (161 days, April to September), a 50% minimum acts on about one day in five and costs about £10 a year: the extra energy is bought at 14.6p and, on a day the sun fills the battery anyway, the room it took is filled by sunshine sold at 9.7p. On 30 September it would have bought 2.9 kWh more and sold 2.7 kWh more at the peak, about 33p better. In winter the charge already reaches a full battery, so nothing changes then.

## 5.128.1 — 30 September 2026

- **A Saving Session no longer holds back energy that an Axle event already covers.** When an Axle event and a session share an hour, the Axle event already keeps its own 4 kWh out of the 4pm sale and the session is served by the same export, but the plugin kept back another 4 kWh for the session. It now counts only what an event does not already cover. A session on its own still gets its share, as before. This did not happen on 30 September (that day's Axle event was the next day's); it came from checking the code and the simulator.
- **The sale no longer switches on and off while it waits for a session.** On 30 September, from 4:43pm to 5:16pm, the plugin started the sale, handed the inverter back a minute later, banked the roof's surplus, and started again, twelve times. While it waits for a session it now keeps the inverter, runs the house from the battery and sells the roof's surplus at the peak rate, so there is nothing left to switch.

## 5.128.0 — 29 September 2026

- **The 2am charge now plans on 80% of the solar forecast, all year.** When the forecast was too sunny, the charge bought too little and the 4pm to 7pm sale ran out early. On 29 September the forecast said about 15 kWh of sun, the day brought 11, the battery reached 4pm at 65%, and the sale stopped at 5:46pm having sold 7 kWh of a possible 12. Replayed over every day on record (161 days, April to September), planning on 80% left the sale short on 5 days instead of 15, needed nothing from the grid in the day, and earned slightly more: a kWh sold at the peak earns about 10p, while one whose sunshine then goes to the grid at 9.7p costs about 6p. In winter the charge already reaches a full battery, so nothing changes then.

## 5.127.1 — 28 September 2026

- **The 4pm to 7pm sale no longer stops half an hour before an Axle event.** Half an hour before an event the plugin sets the lowest level the battery may reach, and to do that it used to take the battery off the peak sale. On 28 September that stopped the sale at 5:31pm for a 6pm event, with the battery at 88% and the event needing about 4 kWh, so half an hour of selling at 27.7p was lost. The sale was already keeping the event's energy back, so now it carries on, and the plugin sets the event's lowest level two minutes before the start, when the event takes over. Outside 4pm to 7pm, or when the battery is not selling, nothing changes.

## 5.127.0 — 28 September 2026

- **The 4pm to 7pm sale is timed to fit a Saving Session.** On a winter day the battery only has enough spare to sell for about an hour and a half at the export limit, and the sale used to start at 4pm, so a 5:30pm session got only a little of it. Now, before a session starts, the plugin sells only what the session will not need, and the battery runs the house meanwhile. During the session it sells at the full limit, and afterwards it carries on as before. The total sold is the same, but more of it falls inside the session, where it earns the session's points as well. When there is spare for the whole three hours, nothing changes.

## 5.126.0 — 28 September 2026

- **On Octopus Flux, Saving Sessions no longer have battery energy of their own.** A session between 4pm and 7pm falls in the peak, when the battery is already selling what it can spare, so the session gets that sale and nothing is set aside for it. A session outside 4pm to 7pm is still joined, but nothing is exported: the battery runs the house through it, which is the lower use the session asks for, and selling then would earn less than the energy is worth later. Every Power Down is now joined on Flux, since joining costs nothing. Other tariffs work as before.
- The scenario simulator runs winter days as well (`--season winter`) and reports how much of the battery each day uses.

## 5.125.4 — 28 September 2026

- **An Axle event now always leaves enough to reach 2am.** On a day forecast sunny that turns out dull, the top-up before an Axle event could still count on sun that never came, because the correction for a dull day stops at 60% of the forecast. The top-up now goes by the sun the day is really giving, with no such limit, and keeps 1 kWh above the reserve for the rest of the evening. Axle pays about £1 a kWh and the dearest import is about 30p, so buying a little too much for an event is the right side to err on. The rest of the plugin keeps the 60% limit.

## 5.125.3 — 28 September 2026

- **The Axle cover and the 4pm-7pm plan now allow for how the day's sun is actually going.** They used the morning's forecast all day, so on a day forecast sunny that turned out dull, the plugin bought too little before an Axle event, and the battery ran out after it. They now use the same correction the rest of the plugin already applies: once enough of the day has been measured, the forecast for the rest of today is scaled by how the sun has done so far. The overnight charge is not affected.
- A scenario simulator (`tools/scenario_sim.py`) runs the plugin's own decisions through whole days: high and low sun, Axle events, Saving Sessions and free hours, with the forecast right or wrong. It checks each day against the rules: the battery is never stopped in the day, the day buys only in free hours, at the reserve or to cover Axle, and every Axle event runs in full.

## 5.125.2 — 28 September 2026

- **An Axle event no longer stops the battery hours before it starts.** From the moment an event was announced, the plugin raised the lowest charge the battery may run down to by the event's energy, and kept it there all day. On 28 September, for a 6pm event, that was 32% from 5am, so from 7am the battery sat idle and the house bought from the grid, on a day forecast to bring 35 kWh of sun. The battery now runs the house down to its usual 20%. The event is still covered: the overnight charge allows for it, the evening sale never sells its energy, and if the battery would reach it short the plugin buys just the difference before 4pm. The raised floor now applies only while an event's export is running, so it cannot sell energy promised to a later one.

## 5.125.1 — 27 September 2026

- **The battery no longer stops for half a minute when the Flux controller changes what it is doing.** Each time it took control at 4pm, switched between selling and running the house, handed back at 7pm, or checked the inverter after a restart, it first set both battery limits to zero, and the house drew from the grid for 15 to 30 seconds. It now puts the inverter into its ordinary run-the-house mode first, which can neither sell from the battery nor charge from the grid, and changes the settings from there, so the battery keeps running the house throughout.

## 5.125.0 — 27 September 2026

- **The solar panels keep working while the battery charges from the grid.** Every grid charge the plugin runs used to tell the inverter to take the grid first, and the inverter then held the panels back to nothing. In the first Weekend Happy Hour, on 27 September, the panels made 1.7 kW just before 1pm and just after 3pm, and nothing at all in between. The charge now takes the sun first and the grid for the rest, so the battery fills at the same rate. In a free hour nothing is thrown away once the battery is full, because the sun runs the house and anything spare is sold. In a charge the house pays for, every unit the sun gives is one less bought.
- **A safety check stands behind it.** If the battery takes no more than the sun can give while the plugin is asking for much more, two minutes running, the grid is not coming in, and that charge goes back to taking the grid first.
- **A restart during a free hour no longer stops the charge** for the rest of the hour.
- **The end of a free hour no longer logs a warning** when the plugin simply noticed the window close a few seconds before its usual check.

## 5.124.0 — 27 September 2026

- **The battery is never stopped during the day.** From 5am until 2am it always runs the house. The two hours before the 4pm peak no longer hold it back. The peak sells only what is spare above what the house needs until 2am, and sells nothing when nothing is.
- **The day buys only in three cases:** free-electricity hours, the battery reaching its lowest allowed charge, and an Axle event the battery cannot cover. The top-up bought at the standard price before the peak is gone. So is the rule that bought at once whenever Octopus's cheap-window times failed to load: the plugin now waits and tells you. A 2am charge that an Axle event pushed past 5am is dropped instead of running in the morning.
- **Axle events are covered.** If the battery would reach an Axle event without enough to run all of it and carry the house to 2am, the plugin buys the difference, as late as it can, and before 4pm for an event in the peak. An event announced before 2am is covered by the overnight charge.
- **After a restart the plugin no longer assumes the Tracker tariff** while it waits for Octopus. Until the tariff arrives it simply runs the house from the battery.

## 5.123.0 — 27 September 2026

- **The battery is no longer held before the 4pm peak when it already has plenty.** In the two hours before the peak the plugin can stop the battery running the house, so its charge is kept to sell at the peak price. It used to do this whenever the peak price beat the standard price, however full the battery was. At a 4 kW export limit the peak sells about 12 kWh, so on a full day that energy was never sold: it was bought at the standard price and then waited for the night. On 27 September it held a 93% battery from 3pm to 4pm and the house bought about 2 kWh an hour meanwhile.
- **Now it holds only while the battery would reach 4pm short** of what the peak can sell and what the house needs until 2am, and only for as long as it is short. An Axle hour inside the peak counts once, because it uses the same export limit as the sale. On a day that has bought at the standard price, when the peak does not sell, it holds only for the house.

## 5.122.0 — 27 September 2026

- **The daily patterns are published for the Dashboards Energy page.** Dashboards 3.51.0 adds a "Through the day" card that charts each kind of day's use hour by hour against the everyday pattern, and says in words where they differ. Nothing about how the battery is run has changed.

## 5.121.0 — 27 September 2026

- **Tuesday to Friday now have a pattern of use measured from those days alone.** Until now they used the everyday pattern, which is an average over the whole week and so carried some of the weekend's busy mornings and afternoons. Here a Tuesday to Friday uses about 0.8 kWh an hour from 10am to 1pm where the everyday pattern said 0.9 to 1.0, and about 1.0 kWh an hour at 5pm, 8pm and 10pm where it said 0.8 to 0.9. The total for those days does not change.
- **The four days share one pattern.** They were measured separately first, and they differ from each other by less than each day differs from itself from one week to the next, so four patterns would each learn mostly noise. Measured together they draw on about 63 days.
- Every day of the week now plans with its own measured pattern. The everyday pattern is still used for any day that has not had six whole examples recorded.

## 5.120.0 — 27 September 2026

- **Mondays now have their own pattern of use too**, measured from the last 18 Mondays in the same way as Saturday and Sunday. The difference is smaller than at the weekend: here a Monday uses about 1.1 kWh an hour between 5pm and 8pm where the everyday pattern says 0.9, which matters because 4pm to 7pm is the Flux peak. The total for a Monday does not change.
- Tuesday to Friday keep the everyday pattern, which is built mostly from those days anyway.

## 5.119.1 — 27 September 2026

- The Event Log line saying Saturdays or Sundays have their own pattern of use now appears once, when that changes, instead of again after every restart.

## 5.119.0 — 27 September 2026

- **Saturdays now have their own pattern of use too.** Over the last 18 Saturdays here, 10am to 1pm used between 1.7 and 2.2 kWh an hour where the everyday pattern said 1.1 to 1.2, and the afternoon less. The plugin plans Saturdays with that pattern now, as it has done for Sundays since 5.118.0. The total for a Saturday does not change.
- **The half-hourly energy record is honest at midnight.** The plugin skipped a row at midnight, so the last half-hour of each day was written into the first row of the next, under a label that said it began at midnight. It now writes a row at midnight, and every row says when its energy really started. Before 5.89.0 that first row was written as nothing at all, and the patterns now ignore those rows rather than learning a quiet midnight that never happened.
- A half-hour with some of its record missing, after a restart for instance, now counts at the rate it ran rather than as a quiet spell.

## 5.118.0 — 27 September 2026

- **Sundays now have their own pattern of use through the day.** The plugin used one pattern for every day of the week, so a Sunday roast, the microwaves and the week's wash were spread across the whole day rather than planned for in the afternoon. Over the last 18 Sundays here, 2pm to 4pm used about 1.4 kWh an hour where the everyday pattern said 0.9, and the late morning less. The plugin now measures a Sunday's pattern from the inverter's own half-hourly records for the last 18 weeks, and uses it for the overnight charge, the Flux plan, the free Happy Hour bookings and the battery's own forecasts.
- The total for a Sunday does not change: it is still the measured Sunday figure. Only the timing moves.
- It needs six whole Sundays before it is used, leaves out the Sundays the clocks change, and is not used while the house is marked as empty. The Event Log says once when Sundays start or stop using their own pattern.

## 5.117.0 — 27 September 2026

- **A position left at 0.0 in `IndigoSecrets.py` no longer puts your roof in the sea off West Africa.** The example file came with `LATITUDE` and `LONGITUDE` set to 0.0, and the file wins over the settings, so anyone who left them had a solar forecast for the wrong side of the world. Both at 0.0 now counts as not set, and the plugin uses the position in its settings. The example file now leaves them empty.
- **The Emergency Import Triggered event now means what it says.** It used to fire for every grid charge. Now it fires only when the battery charges to hold its overnight power-cut reserve. A trigger that sends you a message will go quiet on ordinary cheap-rate charges.
- The example `IndigoSecrets.py` now names the Axle token `AXLE_API_KEY`, which is the name the plugin reads. It used to say `AXLE_API_TOKEN`, which nothing read.
- **With no position set, the plugin no longer watches storm warnings for somebody else's area.** It used to fall back to a position built into the plugin, which was my house. Now it skips the storm check and says once in the Event Log that it needs your latitude and longitude in Configure.
- In the settings, the storm note now says the storm reserve is 50% at every warning level, which is what the plugin has done since June, and the Flux heading no longer calls the strategy a draft.

## 5.116.0 — 26 September 2026

- **The plugin now follows up the credit Octopus owes for a free hour.** A booked Weekend Happy Hour is free up to 16 kWh and Octopus pay it back as a credit. Four times a day the plugin works out what each Sunday's free hours are owed and keeps the amount on show until the credit arrives.
- You get one Pushover when it is paid, one if it arrives more than 5p short, and one if nothing has come after two weeks. Any other credit posted since the free hour is shown beside it, so a payment under an unfamiliar name is not missed.
- The plugin now reads Octopus's own result for every Power Down you joined, and your OctoPoints balance. The Dashboards Energy page (3.49.0) shows all of it.

## 5.115.0 — 26 September 2026

- **On Octopus Flux the Flux controller now does the 2am charge on its own.** The older battery manager used to take the battery at 2am and charge to its own, usually smaller, target. On 26 September that left the battery at 37% on a day that needed far more. The manager now charges in the cheap window only if the Flux controller cannot plan.
- On a dull afternoon, when the house uses more than the panels make, the plugin now counts that drain when it works out what will be left in the battery at dawn.

## 5.114.0 — 26 September 2026

- **On Octopus Flux, a dull day no longer buys tomorrow's electricity at the day rate.** The plugin now buys at the day rate only what the battery needs to get through the 4pm to 7pm peak, and only when that saves money after charging losses and wear. Everything else waits for 2am.
- A top-up has to be worth at least 1 kWh before it starts, so it no longer stops and starts every few minutes.
- On a day that has bought at the day rate, nothing is exported in the 4pm to 7pm peak. The battery runs the house instead.
- After anything took the battery from the Flux controller, even for a moment, the controller now takes it back without a restart.

## 5.113.0 — 24 September 2026

- **The overnight Flux charge no longer buys electricity the sun would have supplied anyway.** On two nights in September it bought about 15 kWh to sell in the peak, and the sun then pushed 13 and 7 kWh out before 4pm at the lower day export rate. A purchase to sell is now limited to what the peak can sell beyond what the sun leaves in the battery by 4pm. The charge the house itself needs is unchanged.

## 5.112.6 — 24 September 2026

- A Sunday that already has a free hour booked is no longer reported as held back. When the plugin decides not to add a second hour, it now says in the log which hour is booked and that it stays booked, with no Pushover.

## 5.112.5 — 23 September 2026

- A Saving Session that ends while an Axle event is running now lets go the moment it ends, and the Axle event hands the battery back once when it finishes. Before, the session stayed active for up to 33 minutes after its end.

## 5.112.4 — 23 September 2026

- A Saving Session now hands the battery back once when it finishes, not twice. If that hand-back is not confirmed, the plugin still tries again a minute later.

## 5.112.3 — 22 September 2026

- Saving the plugin's settings part way through reading the inverter no longer logs an error or sends an alert about a fault that is not there. A real connection fault is reported as before.

## 5.112.2 — 22 September 2026

- A routine restart with Flux switched on no longer logs a warning about the Flux controller. The warning now appears only when the controller was holding the battery at the restart, or its record cannot be read.

## 5.112.1 — 22 September 2026

- A Happy Hour the plugin has booked stays in its plan even when Octopus's own list is slow to show it, so it is never booked twice.
- When a second hour is added to a Sunday that already has one, the message now counts both.

## 5.112.0 — 22 September 2026

The plugin can now book your Weekend Happy Hours, and the battery is made ready for them. Off by default.

- **With the new box ticked, the plugin books two hours on any Sunday the battery can really use them,** judged from the solar forecast, and keeps your tokens back on a day bright enough to fill the battery by itself. Every token is spent by the last Sunday before the offer ends on 1 November. It never cancels a booking.
- You get one message saying what it booked and why, a reminder on the morning, and a note afterwards of what the free hours banked.
- **Three faults that would have spoiled a booked hour are fixed.** The overnight Flux charge now leaves room for the free power, a 2pm free hour can now take over from the Flux controller, and the house stays on the free power until the hour ends. The battery fills to 100% when nothing left of the day's sun could be wasted.
- **Power Downs between 4pm and 7pm are always joined on Flux,** because the battery exports at its limit through the peak anyway. The solar forecast now looks six days ahead instead of three.

## 5.111.8 — 22 September 2026

- The live figures no longer go quiet while the plugin changes the battery's settings, which can take about 16 seconds. It now answers within a second with the figures it read a few seconds earlier, saying how old they are. This stops the Dashboards energy page timing out and logging warnings, 133 of them in a week.

## 5.111.7 — 21 September 2026

- Half an hour before an Axle event, the step that stops the battery charging no longer switches off a Saving Session export that is already running.
- A change to the lowest level the battery may go to, made because a grid event started, is now an ordinary log line, not a warning. The warning is kept for a change the plugin did not make itself.

## 5.111.6 — 21 September 2026

- **On Octopus Flux the peak export now starts at 4pm, not five minutes later.** On a sunny day that is roughly a third of a kWh more sold at the 27.7p peak rate.

## 5.111.5 — 20 September 2026

- The plugin has an icon: a battery and a sun on a dark blue rounded square, drawn to match the other Highsteads plugins.

## 5.111.4 — 20 September 2026

- The plugin now stops with a clear message if Indigo ever gives it a folder location that is not a real one, instead of filing its records in the wrong place. Four empty folders this had left inside the installed plugin have been removed.

## 5.111.3 — 20 September 2026

- The log message for holding back daytime export on a dull day now quotes the forecast the decision was reached on, and the time. Before, it quoted the forecast at the moment the hold began, so on 20 September it said 40.5 kWh was below the 40.0 kWh threshold.

## 5.111.2 — 20 September 2026

- The log no longer records every ten-minute Axle check that finds no event, between 142 and 146 identical lines a day. It writes one line when a quiet spell starts and another when something changes. A failed check is still logged every time.

## 5.111.1 — 19 September 2026

- The log no longer says the export hold on a small day was released when it was never consulted. On 19 September it announced a release that did not happen. The hold now reports holding, released or not asked, and only a real release is written down.

## 5.111.0 — 19 September 2026

- **On Flux, once no hour left today can waste solar, the battery now takes all of the sun and aims for 100%.** Before, it still charged slowly to be full by 4pm and sold the rest at the daytime rate. On 18 September cloud arrived and the battery finished at 97.7% having sold 0.39 kWh at roughly a third of what the evening would have paid.

## 5.110.4 — 19 September 2026

- The Flux plan note now stays quiet unless charging starts or stops, the target percentage changes, or the explanation changes. A small charging figure that shifted on every check had still sent 143 lines between 2am and 5am for two plans.

## 5.110.3 — 18 September 2026

- The Flux plan note no longer repeats on every check. The first full day on Flux logged 310 of them for a plan that changed four times. It now appears only when the plan or its explanation changes.

## 5.110.2 — 18 September 2026

- A restart no longer loses the record of how the day was judged for holding back export. Before, the day's history could read as though it had been judged in the afternoon. What the battery did was never affected.

## 5.110.1 — 18 September 2026

- The overnight Flux charge no longer fills the log with the same line every half minute, 381 of them on the first night. The battery's discharge limit is now logged only when it changes.

## 5.110.0 — 17 September 2026

- **Costs on Octopus Flux now use the price each unit was actually bought at.** Before, a day's import was valued at whatever price was in force when you looked, so the same day could read at 14.6p, 24.4p or 34.1p a unit. Days before the switch keep their old Tracker and Outgoing prices.
- The published unit rate, export rate and tariff name variables follow your account again, and today's export prices are published for the dashboard's Rates tiles.
- Three fixes to the Flux controller: early solar export no longer keeps it out of the 4pm to 7pm window, it no longer stops and restarts the export every half minute, and it lets the inverter's own 4 kW export limit do its job.

## 5.109.5 — 17 September 2026

- **On Octopus Flux the battery now fills to 100% once the day's sun can no longer be wasted,** keeping the last few points instead of selling them at 9.7p and buying them back overnight at 14.6p. Earlier in the day, and on other tariffs, the 95% target stands.

## 5.109.4 — 17 September 2026

- Saving Session notifications now read as plain English, with times like "tomorrow from 6pm to 7pm". A session you cannot join, because it is full or Octopus refused, is no longer announced.

## 5.109.3 — 17 September 2026

- **On Octopus Flux, spare daytime solar now fills the battery by 4pm rather than by dusk.** A unit sold in the day earns 9.7p, while the same unit kept sells for 27.7p in the peak or saves 34p of peak import. Other tariffs still pace to dusk.

## 5.109.2 — 17 September 2026

- Saving Sessions not open to your region are no longer announced, shown on the dashboard or warned about.

## 5.109.1 — 17 September 2026

- **The Flux reserve can no longer lock you out of your battery in a power cut.** Flux now keeps its reserve on the backup reserve, which steps aside in a power cut, and leaves the lowest level the battery may go to at its health floor.
- Once you verify your site import limit, the inverter itself holds the house to it, so an appliance switching on trims the battery charge at once.

## 5.109.0 — 17 September 2026

- **A new Octopus Flux controller, switched off until two separate boxes are ticked.** It charges the battery overnight for what the house is forecast to need, sells what is genuinely spare between 4pm and 7pm, and hands the inverter back outside those windows. It keeps 20% back at all times.
- Axle events, Saving Sessions, Happy Hours, storms, power cuts and anything you do by hand all take priority. It will not trade on missing or stale information, and your account must show Flux for both import and export.
- **Export is no longer reported at a flat 12p.** It now comes from your account's own export prices, and a day's exports are valued at the prices they were sold at.

## 5.108.0 — 15 September 2026

- The plugin now publishes every announced Saving Session for the next eight days, including ones you have not joined, with whether you are in, which way it runs and whether it is full. The battery's behaviour is unchanged. The Dashboards Energy page uses it for its new session banner.

## 5.107.0 — 15 September 2026

- **The plugin no longer joins a Power Down whose free hour you could never use.** It refuses once the Happy Hour scheme has ended on 1 November, when a session is too late in October to be scored in time to book a weekend, and when you already hold enough tokens.
- A new box lets you say how many free hours you expect to use, capped at what the calendar allows. Leave it blank and only the calendar applies.
- The alert no longer tells you to join by hand a session the plugin has just decided against. A session you join yourself is never second-guessed.

## 5.106.1 — 15 September 2026

- A Saving Session alert now leads with the free hour it earns towards, then where you stand on tokens, with the money last. Winning the session matters more than exporting a lot into it.
- The Happy Hour alert counts down to 1 November, when unused hours are lost.

## 5.106.0 — 15 September 2026

- **Correcting the solar forecast by how far out it has been works again.** The plugin read the day's solar just after the inverter reset its daily counter at midnight, so nine of the ten days to 14 September recorded nought. The correction was adding five per cent in a month when the forecast ran eleven per cent high. It now uses the settled daily figure and rebuilds the lost days on the next start.
- **Holding export back on a smaller day is no longer cancelled by a small wobble in the forecast.** Releasing the hold now needs the forecast five units clear of the threshold. Starting a hold is still immediate.
- The log comparison between two charge targets had not run since 31 August. It now reads both targets, has a setting for the one to compare against, and says why on a day with nothing to report.
- Saving Session alerts now give the value in pence. At an eighth of a penny per Octopoint, 85 points a unit is about 11p a unit on top of the export rate.

## 5.105.0 — 13 September 2026

- **Monday now has its own figure for how much the house uses,** about 1.3 kWh above the rest of the working week. The plugin plans against four figures, Monday, Tuesday to Friday, Saturday and Sunday, and each has a setting if you would rather fix it yourself.
- The comparison between days is now taken over eighteen weeks rather than nine.

## 5.104.0 — 13 September 2026

- **Saturday and Sunday are now planned separately.** Here Saturday runs about two and a half kWh above Sunday. There is a setting for each if you would rather fix them yourself.
- The half-hourly picture of your house's use now comes from a rolling nine weeks instead of every reading ever taken, so it keeps up with changes in how you live. The old figure carries the plan until the new window has a fortnight of data. The empty-house profile stays on the long-run average.

## 5.103.3 — 12 September 2026

- Warnings meant to be given once are no longer repeated after every restart. The plugin now remembers on disk which ones it has already given.

## 5.103.2 — 12 September 2026

- The log no longer asks you to enter by hand an Axle payment that your Axle account has already recorded. It had repeated that warning every six hours. A figure that is genuinely missing is reported once.

## 5.103.1 — 12 September 2026

- Ticking the new Axle account box now takes effect within the minute, and the log says at every start and every save that the plugin is reading your Axle account.

## 5.103.0 — 12 September 2026

- **The plugin now reads your Axle account directly.** Axle stopped sending a per-event payment email when they changed how this site is settled, so two paid events never reached the plugin and the earnings on screen were £8 short for three weeks.
- It borrows the sign-in link from Axle's emails in Apple Mail when needed, so there is nothing to set up and nothing stored. Off by default, because it reads your mail to find the link.
- Amounts are refused unless they add up to Axle's own balance. A separate check reports how long the oldest unpaid event has been waiting.
## 5.102.1 — 11 September 2026

- Internal changes only, nothing that changes what the plugin does.

## 5.102.0 — 10 September 2026

- **The plugin can join Octopus Power Down sessions for you.** A session you have not joined pays nothing. Tick the new checkbox and the plugin joins each Power Down as soon as it is announced.
- It only ever joins a Power Down, never one that has started or is full. Power Ups and Weekend Happy Hours stay your decision.
- Octopus offer no way to withdraw, so the plugin writes a line in the log at every start to say it is armed. Off by default.
- A new **Show Saving Sessions** menu item lists what is coming and whether you are in it.

## 5.101.0 — 10 September 2026

- **The overnight power-cut reserve now works on Agile.** The plugin keeps 15% in the battery overnight in summer and 20% from October. On Agile it did nothing, so from 1 October the winter reserve would have gone.
- It buys enough to still be at the reserve by dawn, allowing for what the house uses overnight, in the cheapest run of half hours. It does not wait for a bargain, because the reserve is about keeping the lights on.
- If no prices can be found the top-up is held and you get one Pushover a day saying so. Nothing changes on Tracker, Go or Flux.

## 5.100.0 — 10 September 2026

- **On Agile the overnight charge now starts where the whole charge is cheapest, not at the single cheapest half hour.** Over 150 winter nights of real prices the old rule would have cost about £25 more.
- If Octopus prices cannot be fetched, the plugin no longer starts a full-power import at whatever the price is. It uses the prices it already has, or runs the house from the grid as normal and tells you once a day by Pushover.
- A negative lunchtime price can now be used in the half hour it falls in.
- Every way of ending an import now lets the battery charge to 100% again, so the solar can fill it for the rest of the day.

## 5.99.3 — 8 September 2026

- **A day forecast small overnight but revised up is no longer held back all day.**
- The newest complete forecast for today now decides, in either direction. A partial forecast, or one for the wrong day, still changes nothing.

## 5.99.2 — 7 September 2026

- The Configure dialog no longer cuts its help text off mid-sentence. Two long help notes that stretched every row have moved into ordinary description text, which wraps. No setting or behaviour changed.

## 5.99.1 — 7 September 2026

- **Configure shows its settings again.** Five labels had grown so long that every control sat off the right-hand edge of the window, with no way to scroll to it. The long wording has moved into the description below each setting.
- The same fix applies to the **Force Grid Export** action. Quit and reopen Indigo to see the change, as the client keeps a copy of plugin dialogs.

## 5.99.0 — 7 September 2026

- **A Saving Session export no longer switches the solar off in daylight.** During one session it dropped to nothing for 57 minutes before sunset. The plugin now works out whether it is daylight at the time, and assumes it is when it cannot tell.
- The once-a-minute check on the inverter's settings now treats a Saving Session as its own window. Before, it could have undone the export mode and stopped the export for the rest of the session.
- A second Saving Session in the same run of the plugin now sets its export mode properly, instead of running the house from the battery as normal.

## 5.98.2 — 7 September 2026

- The **Tariff Monitor** device showed "None" where the Agile price should be. It now shows the right price, a missing rate shows as blank, and a zero or negative Agile price still shows.

## 5.98.1 — 7 September 2026

- The tariff line in the log showed no price on Agile. It now shows the price for the current half hour and how many of the day's prices it holds.
- It no longer writes a line every half hour as the Agile price moves, which would have meant 48 entries a day in the event log.

## 5.98.0 — 7 September 2026

- **You can rehearse a tariff before you move to it.** A new setting in Configure makes the plugin plan as though you were on another Octopus tariff, using that tariff's real published prices, so its handling can be tried before your first billed morning.
- Your bill is not touched, but the battery does make decisions for a tariff you are not on. Use it when the battery is not importing, and set it back to Automatic once your real tariff arrives.
- The tariff shows as "forced" wherever it appears, and a warning goes in the log at every start and every save. A mistyped value is ignored.

## 5.97.0 — 6 September 2026

- **The plugin now checks each Axle export against what was paid.**
- It flags four things in plain words: most of an export landing outside its paid hour, Axle settling a different amount from the one measured, Axle paying for a window the plugin has no record of, and a window with no minute-by-minute record.
- Each event is reported once, and nothing older than 45 days is checked.

## 5.96.0 — 6 September 2026

- **Axle messages have moved out of the Indigo event log** into the plugin's own daily log file. Faults still appear in the event log.
- The ledger warning added the day before now says its piece once instead of every hour, and the mail scan warning does the same. The current state stays on the **Axle VPP Monitor** device.

## 5.95.0 — 5 September 2026

- **The plugin can read Axle's settlement emails for you** from the mail Apple Mail has already downloaded on this Mac. No login, password or forwarding rule is needed, and it looks every six hours.
- It only opens messages from Axle's own address with their settlement subject, and takes about a second and a half.
- **Off until you switch it on**, in the Axle section of the plugin's settings. If it stops finding anything it says so.

## 5.94.0 — 5 September 2026

- **Reading settlement emails no longer mistakes Axle's "min £10/month" wording for earnings.** An event settled at 4p would have been filed as £10.00.
- A nil event is filed as a genuine zero, and an event the plugin did not drive can now be filed using Axle's own list of events.
- Of the thirteen real settlement emails, twelve are read and every one agrees with what Axle paid. The thirteenth does not say what it paid and is refused.

## 5.93.0 — 5 September 2026

- **Axle settlement emails can file themselves in the earnings record** instead of being typed in by hand. A new action, **Import Axle Settlement Email**, is there for an Email+ trigger to fire.
- The email brings the money and the plugin supplies the event times, which it already knows because it drove the window.
- The plugin checks the sender and subject, refuses figures outside a sensible range, and refuses a message whose figures disagree with each other. Filing the same email twice does no harm.

## 5.92.1 — 5 September 2026

- The **Axle VPP Monitor** device now updates the moment the ledger warning changes, instead of up to ten minutes later.

## 5.92.0 — 5 September 2026

- **The plugin now warns when the Axle earnings record has not been updated.** The last import was 17 days old and nothing said so. It checks every six hours, says how many days behind it is, and says so again when it recovers.
- The **Axle VPP Monitor** device gains **Ledger Age** and **Ledger Feed Health** states, and a new setting chooses how many days is too many, seven by default. Installs that do not use Axle are never warned.
- An import no longer overwrites an earnings file that could not be read.

## 5.91.0 — 5 September 2026

- **The message after a paid Axle export now quotes the energy sold inside the paid hour,** for example 4.00 kWh rather than the 4.24 kWh the whole run exported. The dashboard's money figures were right throughout.
- The message is written in sentences, stays inside Pushover's 1024-character limit and says when it has dropped detail.
- During a window the plugin now checks the inverter's mode and power limits every minute from readings it already takes, and puts back anything that has drifted.

## 5.90.2 — 5 September 2026

- Running the plugin's checks on the Indigo Mac could wipe the solar forecast the 8pm plan reads, making it plan for no sun tomorrow. That can no longer happen, and an empty forecast is never saved. No change to any decision.

## 5.90.1 — 5 September 2026

- The 8pm message now quotes the same daily need as the plugin itself. It had been 0.9 kWh higher, for example 21.6 kWh against 20.8.

## 5.90.0 — 5 September 2026

- **The export decision now uses what today has actually done.** The plugin compares the solar that has arrived with the forecast for the same hours and scales the rest of the day's forecast, never by more than 40% down or 30% up. A new state on the **Battery Manager** device shows the percentage.
- The day's need is what the house has used so far plus the expected remainder. The weekend allowance is measured from your own history rather than a fixed 30%, which here takes Saturday's need from 28 kWh to 23. A new state shows today's need.

## 5.89.0 — 5 September 2026

- **The daily figures no longer stick or reset wrongly.** Every daily figure now comes from the inverter's lifetime totals measured from local midnight, so a missed midnight or a restart recovers exactly.
- A new state on the inverter device shows whether the day's figures add up, and the log warns the first time they drift apart.
- The half-hourly table keeps the half hour that spans midnight, so it has 48 slots a day instead of 44 to 47, and gains battery charge and discharge columns. No change to battery decisions.

## 5.88.0 — 4 September 2026

- **The export hold no longer judges today from yesterday's forecast.** Between midnight and the first forecast of the morning it could mark a 47 kWh day as small and hold export for over four hours.
- Each forecast now carries its date, and a forecast that never arrived counts as unknown, so the hold stands aside rather than treating it as 0 kWh.

## 5.87.1 — 4 September 2026

- Six export settings now sit under their own heading, **DAYTIME SOLAR EXPORT**, instead of under the power-cut notifications.
- The daytime charge target now says it does not start an export. The hold settings are renamed to **Hold export on days forecast below (kWh)** and a second that holds export until the battery reaches a set level.
- Wording and layout only. Restart the Indigo client to see the new labels.

## 5.87.0 — 3 September 2026

- **Readings now start on time.** The poll interval used to be added after each reading, so "every 5 seconds" meant five seconds plus the reading. The gap between fresh readings has gone from 12 seconds to 8.
- The backup energy count used when the inverter's daily totals are missing had under-counted eightfold. It now measures the real time passed.

## 5.86.0 — 3 September 2026

- **The dashboard is no longer forty seconds behind.** Reading everything took 43 seconds a cycle. The readings that matter are now taken every cycle and the rest in rotation, so fresh figures arrive every 12 seconds.
- Grid, solar and battery are now read within a second of each other, so the house figure worked out from them no longer drops towards zero on cloudy days.
- A kept reading that has not refreshed for ten minutes is dropped rather than shown.

## 5.85.1 — 3 September 2026

- The Happy Hour token count is now quoted as what Octopus reports, pointing you to the Octopus app, which decides whether you can book. The plugin never tells you a slot cannot be booked.

## 5.85.0 — 3 September 2026

- **Happy Hour alerts now say whether a slot is booked, full, needs more tokens, or is ready to book,** instead of asking you to opt in to something that is booked with tokens.
- If a slot is booked and the charging option is off, the alert tells you while there is still time to turn it on. If Octopus does not report your token balance the alert says nothing about it.
## 5.84.0 — 3 September 2026

- A string of panels could report an impossible figure at dawn and dusk. The South array showed 215 kW and a day's total of 220 kWh. It had happened on 21 days since per-string readings arrived on 13 August, and once stuck for half an hour.
- The inverter's readings are now read the right way round, a string's power can no longer go below zero, and a reading no string of panels could physically produce is thrown away along with the rest of that batch.
- Dashboards 3.2.0 cleans up the figures already logged.

## 5.83.1 — 3 September 2026

- The decision log and the dashboard now show when a Saving Session is booked and armed, before it starts, including which way the session runs. Nothing about the battery's behaviour changed.

## 5.83.0 — 3 September 2026

- **The battery can use your free Happy Hour.** Two successful Power Down sessions earn an hour of free electricity, which you book on the Octopus site. With the new setting ticked, the battery charges from the grid at full power for the slot you booked.
- It fills to your usual target rather than 100%, stops early once full, and has three separate ways of stopping so it cannot run past the free hour.
- What it banks is recorded separately, so a Sunday dip in self-sufficiency can be explained. On a bright Sunday with the battery already full it gains nothing, and says so.
- Worth about £8 to £15 before the promotion ends on 1 November. Off by default.

## 5.82.0 — 3 September 2026

- Safety fix to 5.81.0. Octopus runs three kinds of session in the same list — turn-down sessions, **Power Up** (use more) and **Weekend Happy Hour** (an hour of free electricity). The plugin treated them alike, so it would have exported the battery through a Power Up and through your free hour, earning nothing.
- It now exports only during a turn-down session and leaves anything it does not recognise alone. You are still told about the others, with a note that the battery stays out of it.

## 5.81.1 — 3 September 2026

- The check that stops a session being announced twice now copes if Octopus changes the form of the values it sends. Such a change would have repeated the same alert every hour.

## 5.81.0 — 3 September 2026

- **The battery can earn from a Saving Session.** Tick the new setting and during a session the plugin exports above your usual baseline, which is what the Octopoints pay for. Off by default.
- It only runs for a session you have opted into in the Octopus app. Joining the scheme does not enrol you in each session — this account had missed all 17 sessions of the new season.
- An Axle event always takes priority, since Axle pays roughly thirteen times more per unit, and energy Axle is counting on is held back.
- It will not export if that would leave the battery unable to carry the house to morning without buying from the grid, and it says in the log when it declines.

## 5.80.1 — 3 September 2026

- **Fixes the Saving Sessions alert, which did not work in 5.80.0.** The plugin sent Octopus the wrong kind of authorisation, read the failure as "not signed up" and went quiet without a word in the log.
- If it cannot tell whether you are signed up it now says so in the log and tries again next hour. If you are genuinely not signed up it says that once.

## 5.80.0 — 3 September 2026

- **Saving Sessions alerts.** The plugin sends a Pushover message the moment Octopus announces a new Saving Session, naming the time and the Octopoints per kWh.
- It only notifies and does not move the battery. Export earns the same flat 12p a kWh whenever it happens, so timing the battery around a session would only chase the Octopoints, which on this account's history have been pennies (one 15p session in six).

## 5.79.0 — 31 August 2026

- **Daytime export now waits for a full battery on days that are not very sunny.** On a sunny forecast the plugin slows battery charging and sends the rest to the grid, but it judged this on a 1 kWh margin when the forecast is out by 7.5 kWh on an average day.
- On 31 August it began exporting at 8:01am with the battery at 57% and stopped at 1:30pm with the battery at 69%, selling 10.5 kWh at 12p that the house then bought back at 25p to 30p.
- On any day forecast under **40 kWh**, daytime export now waits until the battery reaches **95%**, then sells everything spare as before. Days above 40 kWh are unchanged. Storms are exempt and an Axle event still overrides everything.
- Two new settings, a **Show Bank-First Export Report** menu item, three new device states and a daily record of what was held back and what it cost. Setting the kWh figure to **0** switches it off and restores the old behaviour exactly.

## 5.78.1 — 29 August 2026

- **The Configure dialog opens again.** It had failed to open since 24 August, because 5.75.0 added a second web dashboard section that reused three field names, which Indigo refuses. No setting could be changed in that time.
- The two dashboard sections are now one, with the host setting beside the access and token settings.

## 5.78.0 — 29 August 2026

- **An empty house gets its own consumption profile.** Point the new **Away mode** setting at an Indigo variable your holiday schedules already set, and the plugin keeps a second profile for days the house is empty, starting from a flat figure you can set. The two profiles never mix.
- Measured across a 45-day absence, the empty house here drew a flat 507 W, 12.16 kWh a day. Without a separate profile the plugin would plan the battery around people who are away, then carry those quiet weeks into the normal profile for months.
- The weekend uplift is dropped while away. A new **Away Mode** state on the **Battery Manager** device shows which profile is in use.
- A missing or unreadable variable counts as somebody home, since wrongly assuming an empty house could leave the battery short on a winter evening. Worth roughly £12 over a six-week December absence on last year's Agile prices.

## 5.77.3 — 28 August 2026

- Internal changes only, nothing that changes what the plugin does.

## 5.77.2 — 28 August 2026

- The event log no longer gathers about 140 lines a day saying Axle has no event scheduled. Failures are still logged as before, and a sustained outage still reports hourly.

## 5.77.1 — 25 August 2026

- Internal changes only, nothing that changes what the plugin does.

## 5.77.0 — 24 August 2026

- The level the battery keeps for dawn in summer now falls back to 15% if the setting has never been saved, not 10%, which is the battery-health limit it is meant to sit above. The documentation now says 15%, as it has been since 3.0.
- The companion optimiser script had the same seasonal rule without the plugin's check, and now matches it.

## 5.76.0 — 24 August 2026

- The plugin's own fallback energy page is cut back to power flow, battery level, live power, the manager's decision and today's totals — the view for when Indigo's web server is stuck or the broadband is down. Charts, calendar, tariff, cost and export tables are left to the Dashboards plugin, which already draws them.
- The data the Dashboards Energy and Cost pages read is unchanged.

## 5.75.0 — 24 August 2026

- **The web dashboard is no longer open to the whole network by default.** It had served battery, tariff, Axle and power-cut data to any device on the network with no password. It now answers only the Indigo Mac itself, which changes nothing you see, because the Dashboards plugin reaches it from the same machine.
- A new **Dashboard access** setting opens it to the network, and then a token is required — the server will not start without one. The token comes from your secrets file, the Configure dialog, or one the plugin makes and keeps private. A browser needs it only once.
- New menu item **Show Web Dashboard Access**.
- The plugin can read back the export limit set on the inverter, so the standalone SigenVPP program can share the same code.

## 5.74.0 — 20 August 2026

- To help choose between a 90% and a 95% daytime battery target, the manager works out in the background what a 95% target would have held back from early export, without acting on it. Each day's history keeps this beside the actual end-of-day battery level, export and imports after 4pm.
- It also prices each day's grid imports at both the Tracker price and Octopus Agile's half-hourly prices, for the autumn tariff decision. This assumes the imports happen at the same times, and gaps in price data stay blank rather than guessed.
- New menu item **Show 90% vs 95% / Agile Shadow Comparison**. Nothing about charging, export or the inverter changed.

## 5.73.0 — 19 August 2026

- A grid event near sunset no longer reports the solar as shut down when the sun was simply going down. A small forecast now excuses a zero reading, a large one does not, and an unknown one excuses nothing. Reporting only.

## 5.72.3 — 19 August 2026

- The Axle earnings ledger adds up its own rows and says when that total disagrees with Axle's headline figure, for example after a settlement entered from an email left the headline at £87.60 while the rows came to £91.47. It never replaces Axle's figure with its own. The check stops once a withdrawal appears.

## 5.72.2 — 19 August 2026

- A settlement figure typed in from Axle's email can no longer be counted twice when Axle's account page catches up. A new grid-event row whose time window is already held now replaces the existing row. Monthly payments and referral credits are not affected.

## 5.72.1 — 18 August 2026

- The ledger now records what was exported inside the paid window, as well as over the whole run, which extends a few minutes either side. Inside the window every one-hour event comes to 4.00 kWh and the two-hour one to 8.01 kWh.
- The small steady gap left against what Axle pays is their baseline. An over-run is now reported as an over-run, not as money Axle failed to pay.

## 5.72.0 — 18 August 2026

- **A ledger of what Axle actually paid.** Axle pays on the change against a baseline, not on raw export, so an hour recorded here as 4.23 kWh settles at 3.838. The ledger shows Axle's settled figures unchanged beside the plugin's own estimate, clearly labelled.
- An event not yet settled shows as pending, not zero, since settlement runs several days behind.
- Two new menu items — one prints the ledger, one imports figures from the account page. The dashboards get the ledger and a countdown to the next window.

## 5.71.1 — 15 August 2026

- A grid charge queued for later (for example "start at 2am, the battery will not last until morning") could start during a paid Axle export window, buying electricity while selling the battery. It now waits for the window to close. Charging ahead of an announced event is unchanged.

## 5.71.0 — 13 August 2026

- **The real grid voltage is now read** — 252 V here, against a legal ceiling of 253 V. The plugin had been reading a fixed 230 V rating that never moves.
- Above 253 V an inverter must curtail or disconnect, losing export, and the cause lies with the network operator. Voltage and current are recorded as numbers so you can chart how often it nears the limit.

## 5.70.0 — 13 August 2026

- The status feed and dashboards now use the battery size from your settings, the same figure the battery decisions use, so correcting it keeps them in step.
- The pack's rated size, 36.16 kWh, is now published beside the configured 35.04 kWh. Measured from six days of history, the usable figure is about 35.6 kWh. The setting is left for you to change.

## 5.69.0 — 13 August 2026

- Checked against Sigenergy's own documentation, the inverter does not report the four packs separately on the local connection. The app gets those figures from Sigenergy's cloud.
- Four new readings: the **inverter's own temperature** (58 degrees), **solar insulation resistance** (a falling figure means water getting in or a damaged cable), the **number of battery packs**, and the **alarm**, shown as raised or clear.

## 5.68.0 — 13 August 2026

- The inverter reports only the hottest, coldest and average of its four packs, so the plugin now works out which pack, if any, is the odd one out. On the first reading one pack sat 4.8 degrees above its neighbours.
- It names a pack only when it is at least 2 degrees clear and twice as far out as the other end, and declines to answer when the figures cannot support it. The spread is recorded so a widening gap over weeks can be seen.
- The plugin also reads **grid frequency**, seen moving between 49.98 and 49.95 Hz. Grid events exist to answer frequency problems.

## 5.67.0 — 13 August 2026

- **Each string of panels reports its own voltage, current and power**, as device states you can chart and as a live strip on the dashboards.
- Strings are numbered until you name them (and give each its size in kWp) in the plugin's configuration. A clear day's curves show which roof each feeds — an east string peaks mid-morning, a west one in the evening.
- On an inverter without per-string readings the plugin asks three times, notes it once in the log, and stops.

## 5.66.0 — 12 August 2026

- The report after a grid event no longer claims the solar was shut down just because the panels dropped to zero during the window. On 12 August cloud and a partial eclipse did that, while the 6pm to 8pm event sold 8.26 kWh at a steady 4 kW.
- The report now judges by the highest solar reading in the window, and records it beside the lowest. How events are run did not change.

## 5.65.0 — 12 August 2026

First batch of fixes from an independent review.

- A grid import booked for later is now cancelled when the plugin changes its mind, and no longer fires while the manager shows "Paused". A fault that could turn a finished import into "charge to 100%" is also closed.
- **Storm watch now reads combined warnings** such as snow and ice, or rain and flood, which it had been ignoring while carrying on exporting. It names anything it sets aside.
- The decision engine now uses the day-by-day correction to the solar forecast, not the overall one meant for display.
- Two fixes for other systems — returning to normal no longer resets battery power limits to a figure fixed for a 10 kW inverter, and daytime export no longer briefly lets the sun charge the battery before selling.

## 5.64.0 — 12 August 2026

- At the end of a grid event the plugin now checks the inverter was actually handed back to normal running. Before, a lost instruction could leave the battery exporting unpaid, up to about 1.25 kWh, with nothing in the log. It now retries at once, warns if that fails, and keeps trying every ten seconds, never during a live event.

## 5.63.0 — 11 August 2026

- The report after a grid event counts only readings from that event's window. Before, an over-running export could put one night's figures into the next event's report. Any readings that do not belong are noted in the log.

## 5.62.0 — 11 August 2026

- **A second safety net for grid events.** If an export is still running a quarter of an hour after the window should have ended, the **Battery Manager** ends it and says so in the log. It cannot cut a real event short.

## 5.61.1 — 11 August 2026

- **A grid event did not stop when it ended.** Axle publish the next event within a minute, and the plugin was using the new event's end time, 21 hours away, so the export carried on and would have emptied the battery overnight. It now stops on the window it is driving. If you run this plugin, update.

## 5.61.0 — 11 August 2026

- Nine energy variables giving the kWh beside each cost figure had not been updated since April. Export read 0.000 kWh beside earnings of £2.12 that came from 17.69 kWh. Both now come from the same figures.
- A figure not yet known leaves the variable alone rather than setting it to zero.

## 5.60.1 — 8 August 2026

- The **About** item in the Plugins menu now opens the plugin's web page. It went nowhere before.

## 5.60.0 — 8 August 2026

- **Intelligent Octopus Go and Go 12M Fixed now work.** Octopus had renamed them, so the plugin fell back to charging at once at whatever the price was — 32p a unit instead of waiting for 8p. It also now fetches their cheap-hours times.
- The Octopus Go cheap window had been an hour out since July, stored as 11:30pm to 4:30am when the real window is 12:30am to 5:30am. The plugin now works the cheap window out from the prices, and will not guess for tariffs with no single cheap window, such as Agile or Cosy.
- Nothing changes if you are on Tracker, Agile or Flexible.
## 5.59.0 — 8 August 2026

- **Agile support now works end to end.** The Agile prices never reached the part that picks the cheapest half hour to charge, so it charged at once at 10 kW, whatever the price. It now uses today's and tomorrow's prices. Proved by testing, not yet on a real Agile account.
- The tariff display no longer shows a Tracker price while you are on Agile.

## 5.58.0 — 7 August 2026

- If the battery is too low to see a grid event through, you now get a message half an hour before, saying how far short it is and where the export will stop. It goes at normal priority, so quiet hours can hold it back.

## 5.57.0 — 6 August 2026

- An import event from the grid service is refused with a warning instead of being run as an export.
- The settings that send sunny-day export to the grid are checked through each event and put right within a minute if one fails to stick.
- A restart mid-event, or an event past midnight, no longer upsets the inverter or the export figures.

## 5.56.0 — 5 August 2026

- The report after a grid event now says the plugin drove the export and gives a plain verdict: ran, curtailed or not applicable. Night-time events no longer report that solar collapsed.
- The **Axle VPP Monitor** compares the inverter's real settings with the plugin's, so anything else taking control is reported.
- UK local time is worked out in one place, so tariff times and sunrise stay right through British Summer Time and the October clock change.

## 5.55.5 — 5 August 2026

- **Daytime export no longer switches on and off every few minutes** on close days. It now needs a clear margin to start and waits ten minutes after a stop. Stopping still happens at once.

## 5.55.4 — 4 August 2026

- Internal changes only, nothing that changes what the plugin does.

## 5.55.3 — 30 July 2026

- Internal changes only, nothing that changes what the plugin does.

## 5.55.2 — 30 July 2026

- The log no longer reports an error, and no alert goes out, when Axle sends a blank reply after an event ends. That reply means nothing is scheduled.

## 5.55.1 — 30 July 2026

- The dashboards' "VPP event announced" line now shows the event times, with the date only when it is not today.

## 5.55.0 — 30 July 2026

- **A broken Axle connection now shows up** in the log instead of looking like a quiet week. The **Axle VPP Monitor** gains two readings: whether the last check worked, and when the last good one was.

## 5.54.0 — 26 July 2026

- The restore alert after a power cut now says whether today's solar will rebuild the reserve on its own, with both figures.

## 5.53.0 — 26 July 2026

- **Power-cut alerts give the full picture:** battery level, what the house is drawing, roughly how long the battery would last, and on a restore when export will restart.
- Yellow storm warnings and all-clears, broken since early July, send again. Amber and red were unaffected.

## 5.52.1 — 26 July 2026

- The log after a power cut now names both ways export can restart: the battery reaching 85%, or the day's sunshine rebuilding the reserve.

## 5.52.0 — 25 July 2026

- **The Period totals now add up.** "Grid-only" and "Elec bill" both include the standing charge, so solar benefit = grid-only − elec bill + export earned. Your headline saving does not move.
- The "same week last year" comparison can now appear, and the calendar-month table gains an electric-bill column.

## 5.51.0 — 20 July 2026

- **Sunny-day charging is paced to a 90% target**, which you can change, instead of 100%, so less solar is given away to buy charge. On a real July day this exported 1.6 kWh more and curtailed nothing instead of 1.5 kWh.
- A second setting keeps a level for power cuts. Storm warnings bring back the 100% target.

## 5.50.0 — 20 July 2026

- **The export pause after a power cut now takes the weather into account.** Export restarts as soon as the day's sunshine will rebuild the reserve on its own. A new setting, default 50%, stops this early release on a nearly empty battery.

## 5.49.0 — 19 July 2026

- Solar forecast figures now agree with each other, on the solar card, the dashboards, **Show Today's Energy Summary** and **Show Manager Status**. A new **Expected total** shows generated so far plus still to come. Battery decisions were never affected.

## 5.48.0 — 6 July 2026

- **Axle events survive a plugin restart.** The plugin picks a running export window straight back up and exports to the end.

## 5.47.0 — 5 July 2026

- **Ready for Octopus Go and Intelligent Octopus Go.** The Go cheap window is corrected to 11:30pm to 4:30am.
- The overnight top-up that keeps your power-cut reserve now runs on time-of-use tariffs too, in the cheap window only.

## 5.46.0 — 3 July 2026

- The whole-house cost card can no longer settle a day's gas at £0.00 from a part day's readings. Days already affected settle again correctly.

## 5.45.0 — 2 July 2026

- Pause, Configure and the dashboard respond at once instead of hanging for up to twenty seconds while the inverter is read.

## 5.44.0 — 2 July 2026

- On Agile, an overnight top-up must beat tomorrow's daytime rates after the battery's roughly 6% loss. On a flat-priced day the house draws from the grid instead.

## 5.43.1 — 2 July 2026

- Dashboard charts draw without an internet connection, the Configure dialog checks figures as you save, and variables go in their own "Sigenergy" folder on a fresh install. Around eighty small robustness fixes.

## 5.43.0 — 2 July 2026

- Pause, the overnight emptying target and the export pause after a power cut all survive a restart.
- During an inverter outage the plugin holds its last good reading and stops planning from stale readings. Month costs start from zero on the 1st.

## 5.42.0 — 2 July 2026

- Charging from the grid sets a ceiling on the inverter itself, so the battery cannot keep charging if the plugin stops.
- Emptying the battery overnight stops at once if a storm warning arrives, and an unreachable storm feed keeps the last warning level.

## 5.41.0 — 30 June 2026

- The Octopus cost, rate and balance variables in Indigo are filled again from your Octopus account every half hour.

## 5.40.0 — 30 June 2026

- Every storm level now keeps the battery at 50% or above, and never charges from the grid beyond that.

## 5.39.0 — 29 June 2026

- During a storm warning, export resumes once the battery is above roughly 85%.

## 5.38.2 — 26 June 2026

- Octopus rate windows follow the UK day, and a second electricity supply is no longer mistaken for an export meter.
- **Force Grid Import** and **Force Grid Export** use the values you type. Storm alerts no longer repeat on a restart.

## 5.38.1 — 26 June 2026

- One missing value can no longer blank the whole dashboard.

## 5.38.0 — 26 June 2026

- Whole-house cost works with more meters: gas read once a day, gas reported in kWh, and homes with no gas.

## 5.37.0 — 26 June 2026

- **Storm watch works again** after MeteoAlarm changed its feed. The location name in the alert is now a setting.

## 5.36.0 — 26 June 2026

- Pausing the manager returns the battery to a safe state and stops any Axle event driving the inverter.

## 5.35.0 — 24 June 2026

- Inverter readings are stored as numbers, so Indigo's history can chart them. Existing installs gain a few harmless duplicate history columns.
- The battery level that ends the export pause after a power cut is a setting, default 85%.

## 5.34.0 — 24 June 2026

- After a power cut, export is held off only while the battery is below 85%. A new reading shows on-grid or power cut, for charting.

## 5.33.0 — 24 June 2026

- **Power-cut alerts** by Pushover and email when the grid drops and returns. Turn them on or off in the new Power-cut notifications section of Configure.

## 5.32.0 — 24 June 2026

- The 8pm battery plan message now reports the plugin's own overnight export decision, so it can no longer promise an export that does not happen.

## 5.31.6 — 22 June 2026

- The solar card gains solar against forecast, peak power, tomorrow's forecast, self-sufficiency, forecast accuracy and lifetime total.

## 5.31.5 — 22 June 2026

- "Yesterday" shows a provisional figure while the gas is still settling.

## 5.31.4 — 22 June 2026

- The whole-house cost card shows Today, Yesterday and the Day before.

## 5.31.3 — 22 June 2026

- A day's cost is frozen only once Octopus has settled nearly all of it.

## 5.31.2 — 21 June 2026

- The daily cost history cannot be cut short by a crash, and one bad day is skipped.

## 5.31.1 — 21 June 2026

- Each settled day keeps the rates that applied on that day.

## 5.31.0 — 21 June 2026

- **Whole-house cost, gas and electric, with standing charges.** A new dashboard card shows today, yesterday, month to date, your account balance and a 30-day chart. New settings take your gas meter details.

## 5.30.1 — 15 June 2026

- An Axle export tops up from the battery the moment solar falls short, so the full amount is always delivered.

## 5.30.0 — 15 June 2026

- **Daytime Axle exports also bank spare solar.** Tested live: 4 kW exported and about 4.9 kW into the battery.

## 5.29.2 — 15 June 2026

- Internal changes only, nothing that changes what the plugin does.

## 5.29.1 — 15 June 2026

- **Daytime Axle exports now reach the grid** instead of charging the battery in strong sun. Use this rather than 5.29.0.
## 5.29.0 — 15 June 2026

- **Daytime Axle events keep your solar running.** The plugin now exports from the panels first and only draws on the battery for the shortfall. Before, the battery did all the work and the inverter shut the solar down for the whole event — on the 15 June morning event 4.22 kWh went out entirely from the battery with solar at 0 W.
- On a dark morning it behaves exactly as before. In a live test solar held about 2.9 kW, the battery covered the 1.9 kW gap and export sat at the 4 kW limit.
- New **Force Daytime Export** test action.

## 5.28.3 — 11 June 2026

- Internal changes and tidying of the published files only, nothing that changes what the plugin does.

## 5.28.2 — 10 June 2026

- The Axle payment rate of £1 per kWh is now a plugin setting instead of fixed, so the earnings estimate on the **Axle VPP Monitor** stays right if Axle change it. A blank or non-numeric value falls back to £1.00.

## 5.28.1 — 10 June 2026

- The **Battery Manager** has its own "VPP export" mode, so an Indigo trigger can fire when an Axle export starts.
- Removed the old code that waited for Axle to hand the inverter back, which is no longer needed. No change to how the export itself runs.

## 5.28.0 — 10 June 2026

The plugin now runs Axle exports itself for the announced window, rather than waiting for Axle to send the command.

- The 10 June event was a no-show. Axle confirmed a fault in the link to Sigenergy that might not be fixed before the next event, and handing the inverter over made no difference. Axle pay on the meter reading, so an export the plugin runs itself counts the same — about £1 per kWh, on top of the Octopus Outgoing rate.
- Export starts two minutes before the window, is checked again every cycle so a brief drop puts itself right, and stops two minutes after the end, when the house goes back to running from the battery as normal.
- The battery keeps its reserve for the next day, and nothing is imported from the grid to export. The window times still come from Axle, but their start and stop commands are ignored.

## 5.27.0 — 6 June 2026

- **One source for Octopus rates.** On every refresh the plugin writes today's and tomorrow's rates and the tariff name into the Indigo variables the battery optimiser script reads, for whichever tariff you are on, not just Tracker.
- This retires the separate Octopus Tracker rate script, which fetched the same data a second time and had gone stale. Its Indigo schedule is disabled and the script kept as a fallback.

## 5.26.2 — 6 June 2026

- If the Mac sleeps while the lowest battery level is raised for flood prevention, a storm or an Axle event, the plugin drops it back to the normal floor, so a long sleep cannot lock the battery and force grid import overnight.
- Power limits sent to the inverter are capped at 100 kW. The fastest poll setting now really polls every 5 seconds instead of 10.
- Seasonal and storm settings are logged only when they change, not every 60 seconds.
- The dashboard copes with one bad reading, keeps the year you picked when it refreshes, and its Back link no longer points at a fixed address.

## 5.26.1 — 6 June 2026

- One failed inverter, forecast or Axle check is logged and retried rather than stopping all polling.
- A blank setting can no longer slip a wrong value into the sums.
- The estimate of household use now works in local time. In British Summer Time it had been an hour out.
- After a restart the forecast no longer serves yesterday's figures as today's.

## 5.26.0 — 6 June 2026

- **Pause now works.** Before, pausing set a flag nothing read, so a manager showing "Paused" carried on driving the inverter. It now hands the inverter back to running the house from the battery as normal, remembers the pause across restarts and acts the moment you resume.
- A partial reading from the inverter could make the battery look empty and start a charge that never finished. The plugin now ignores an incomplete reading and keeps the last good one.
- Cheap-rate windows were an hour out in summer, because a UTC clock was compared with local times. Fixed.
- A partial solar forecast could overwrite a good one with a low total and trigger import nobody needed. It no longer can.

## 5.25.7 — 5 June 2026

- A blank or non-numeric setting or action field now falls back to its default instead of stopping the battery check with an error.
- Two other crashes guarded against, one when sizing the charge before an Axle event and one when reading tomorrow's Octopus rates.

## 5.25.6 — 4 June 2026

- Log lines from one part of the plugin now carry milliseconds like the rest, so events line up across plugins.

## 5.25.5 — 4 June 2026

- A brief outage at the Open-Meteo forecast service now logs a short warning that it is temporary and that saved forecast data is in use, instead of a red error followed by a dump of a web page. A request refused outright still logs an error.

## 5.25.4 — 1 June 2026

- Live Power Flow diagram tidied. Solar, Home, Grid and Battery are the same size, Grid is as large as Home with Import, Export or Idle beneath it, and Battery shows kW first with the percentage below.

## 5.24.1 — 28 May 2026

- The log no longer shows a harmless red error about the **Battery Manager** mode on each restart.

## 5.24.0 — 28 May 2026

- The **Battery Manager** gains a mode state that Indigo splits into one true/false state per mode, so a trigger can fire on "the battery entered night export" without comparing text. The existing description of what the battery is doing is unchanged.

## 5.23.0 — 27 May 2026

- **Safe when the Mac sleeps.** A sleeping Mac used to leave the inverter in whatever forced mode it last had, so eight hours asleep during a forced charge would overcharge the battery. On sleep the plugin now hands the inverter back to running the house from the battery as normal, saves its running totals, stops the dashboard and closes the inverter connection.
- On wake it restarts the dashboard and checks the inverter straight away instead of waiting for the next poll.

## 5.22.1 — 27 May 2026

- Fixed a summer-time fault that could put an Octopus Go half-hour in the wrong price band. Normal Indigo installs were not affected, since they carry the time-zone library the old code needed.

## 5.22.0 — 27 May 2026

- Each time the manager changes what the battery is doing, the log lists every check it made and why it passed over the others, once per change. The companion optimiser and rate scripts log their reasoning the same way.

## 5.21.4 — 26 May 2026

- A brief network fault fetching the solar forecast is retried once after 2 seconds and logged as a warning, not a red error. If one array's forecast still fails, the other three carry the day as before.

## 5.21.3 — 25 May 2026

- The plugin's devices no longer stop and restart when the plugin updates their own settings.

## 5.21.2 — 23 May 2026

- Every log line carries the time to the millisecond. New **Toggle Timestamps in Log** menu item.

## 5.21.1 — 23 May 2026

- **Your site's latitude and longitude no longer have a built-in default.** The plugin reads them from your Indigo secrets file first, then from the plugin settings.
- If neither is set it logs an error and turns the solar forecast off, and the rest of the plugin keeps running. **Run Self-Test** lists the two location settings.

## 5.21.0 — 22 May 2026

- **Correcting the solar forecast by how far out it has been, now by size of day.** The forecast ran low on middling days (25 to 45 kWh) and high on bright days (above 55 kWh), so one flat correction cancelled itself out. The plugin now uses five bands, each with its own correction worked out nightly from the last 60 days.
- The hourly forecast is scaled by the same amount, so the shape of the day holds and dark hours stay at zero.

## 5.20.0 — 21 May 2026

- **Axle events handed over properly.** A paid 4 kWh event reported 0.00 kWh exported, because five minutes before the start the plugin closed the very channel Axle sends its commands through. From 30 minutes before an event the plugin now leaves the inverter's mode and power limits alone so Axle's commands stand.
- The plugin spots the end of an event more reliably. On one night the inverter was only taken back after the 60-minute time-out.

## 5.19.7 — 21 May 2026

- Fixed an error logged on every solar forecast refresh. The forecast itself was unaffected.

## 5.19.6 — 21 May 2026

- Stopped correcting the solar forecast by how far out it has been. Over 31 days the raw forecast was closer, 18.7% average error against 21.9% to 22.3% corrected. The correction is still worked out and shown. Replaced by the banded correction in 5.21.0.

## 5.19.5 — 21 May 2026

- The forecast correction is worked out from total kWh instead of averaging each day's ratio, which leaned high. For May 2026 the factor went from 1.163 to 1.150.

## 5.19.4 — 21 May 2026

- Flood prevention now counts Axle exports booked for the day the battery must refill. Before, the plugin could empty the battery overnight and then find that day's solar already promised to Axle.

## 5.19.3 — 21 May 2026

- Flood prevention now checks the forecast for the day that will actually refill the battery. After midnight it looked a day too far ahead, and on 21 May it exported 14.9 kWh at 12:25am on a day forecast at 29.5 kWh, then could not refill.

## 5.19.2 — 15 May 2026

- Live Power Flow card restyled with a soft glow, two chips showing the grid state (On Grid, Lockout or Grid Down) and what the manager is doing, and plainer labels such as "0.98 kW Charging" or "0.94 kW Exporting".

## 5.19.1 — 15 May 2026

- The Live Power Flow card shows all four figures in kW to two decimals, so 980 W reads 0.98 kW.

## 5.19 — 15 May 2026

- **Export check against Octopus.** A new **Export Sync** dashboard card compares the inverter's daily export with what Octopus recorded on your export meter over the last seven settled days, skipping the latest three because Octopus takes 24 to 48 hours to settle. Days more than 5% apart are flagged.
- It needs your export meter details in the secrets file or plugin settings, and stays off without them. A one-line summary goes to the log at midnight.

## 5.18.2 — 14 May 2026

- **Axle event report.** When an event ends, the **Axle VPP Monitor** device gets nine summary states, including the date, energy exported, solar produced and peak export.
- The end-of-event Pushover carries the headline figures, and one summary line goes to the Indigo log. A failed summary warns but never delays the handback.

## 5.18.1 — 14 May 2026

- Minute-by-minute readings during an Axle event go to a separate file per event instead of the Indigo log. The log keeps only the key moments: announced, 10 minutes to go, handed over, window active, ended and control taken back.

## 5.18 — 14 May 2026

- **Axle now truly takes over the inverter.** Five minutes before an event the plugin hands the inverter back fully, so it follows Axle's commands through Sigenergy the same as for other Axle and Sigenergy users, including keeping solar running while the battery exports.
- The plugin takes the inverter back as soon as Axle hands it over at the end. The minute-by-minute countdown in the log becomes one 10-minute warning, with clear lines marking the hand-over and take-back.

## 5.17 — 14 May 2026

- Daytime Axle events now export from the panels, with the battery kept from charging, so 4 kW goes out from the solar and the battery is saved for later. In 5.16 the battery did all the export and the inverter shut the solar down to 0 W.

## 5.16 — 14 May 2026

- **Urgent fix for Axle events.** On the 14 May morning event the plugin switched the inverter back to normal running at the start of the paid window, which stopped the export, and Axle could not override it. The battery charged from solar instead and 0 kWh went out where about 10 kWh should have. Later replaced by 5.17 and 5.18.

## 5.13 — 12 May 2026

- Hover any card title, row label or column heading on the dashboard to see what the figure is, where it comes from and how it is worked out.

## 5.12 — 12 May 2026

- Hover the four forecast labels (Today, Tomorrow, Remaining and Bias factor) to see what each number means. A dotted underline shows which labels have help.

## 5.11 — 12 May 2026

- Fixed the forecast chart tooltip reading 0.00 kWh for every hour. It now shows the real figure, such as a 5.66 kWh peak.

## 5.10 — 12 May 2026

- The hourly forecast chart is about 60% shorter and drops the figures above the bars. Hover a bar to see its hour and kWh, and small early and late bars are easier to hit.

## 5.9 — 12 May 2026

- **Live updates.** The plugin reads the inverter every 10 seconds instead of 60, and the dashboard refreshes every 5 seconds instead of 30. The poll setting now really takes effect and offers 5, 10 and 15 second options.
- Existing installs on 60 or 120 seconds move to 10 at the next start. 30 seconds stays as it is.

## 5.8 — 12 May 2026

- Dashboard restyle with frosted-glass cards, a slowly moving coloured backdrop, glowing headline figures that slide to new values instead of jumping, a pulsing live dot, and a 24-hour battery level trend line under the battery ring.

## 5.7 — 12 May 2026

- The daily history is now kept for ever.
- Each day records its own export rate, so a later change to the export rate does not re-value past days. Existing days were filled in at 12p, the rate since 26 March 2026.

## 5.6 — 12 May 2026

- New calendar months card showing each month of the year with totals and daily averages, the current month marked as partial, and a year total. Year buttons let you look back at earlier years.
- History is kept for about ten years instead of one.

## 5.5 — 12 May 2026

- New **Period totals** card for the last 7 days, this month and the last 365 days, showing: solar benefit, net grid, cost without solar, import paid and export earned, each with a daily average. Each day is valued at its own import rate.

## 5.4 — 12 May 2026

- New **Yesterday** card beside **Today's Cost**, with the same five figures.
- Fixed the house's daily use being saved as nearly zero, because the inverter resets its daily count at midnight slightly before the plugin does. Days saved before this fix cannot be corrected.

## 5.3 — 12 May 2026

- New **Today's Cost** dashboard card showing: import paid, export earned, the net, what the day would have cost without solar, and the day's total benefit from solar. It shows a dash until the plugin has its first Octopus rate.

## 5.2 — 12 May 2026

- **Dashboard charts** of battery level and energy over 24 hours, 48 hours or 7 days, and daily totals over 30 days.
- **Weekly backup** of the plugin's data every Monday at midnight, keeping the last eight.
- On startup the plugin logs a line if a newer version is on GitHub.
- **Show Today's Energy Summary** and **Show Manager Status** now show the solar still to come today, not the whole day's forecast labelled as remaining.

## 5.1 — 12 May 2026

- The plugin shares its settings with the companion battery optimiser script on every start and every save, so the two always agree. They had drifted, and the script could advise against emptying the battery overnight while the plugin was doing it.
- New setting to describe each solar array by name, tilt, direction, size and shading. A bad entry logs an error and the built-in four-array layout is used.

## 5.0 — 12 May 2026

A large hardening and feature release.

- New menu items **Run Self-Test** and **Show Power Cut Log**, a weekly battery health check with warnings as it wears, and a record of power cuts.
- Settings sent to the inverter are read back to check they took, Octopus requests are capped per hour, and bad replies no longer upset the plugin.
- Weekday and weekend use now calibrate themselves from real consumption. Pause and resume through an Indigo variable. Pushover gets quiet hours and a choice of sound.
- **Force Import** and **Force Export** dialogs open with live values, a midnight summary shows forecast accuracy over the last 7 days, and the companion script's 8pm and 1:45am Pushovers are rewritten in full sentences.

## 4.9 — 10 May 2026

- **Pushover alerts now send.** The plugin had been calling the wrong action.
- **Plugin triggers now fire** for emergency import, export started and stopped, Axle event announced, started and ended, flood prevention started and stopped, and power-cut lockout started and cleared.
- The inverter address, dashboard host and Pushover user key can live in your secrets file, with the plugin settings as fallback and an error if neither is set. The dashboard finds its own network address if none is given.
- A missing inverter address no longer stops the plugin starting, only the inverter link. Several quiet failures now log as errors.

## 4.8 — 3 May 2026

- The log no longer shows a status line every 15 minutes, only when the battery action changes.

## 4.7 — 2 May 2026

- Solcast removed, so the forecast now comes from Open-Meteo only.
- Flood prevention now needs tomorrow's solar forecast to be three times the day's need, up from two.

## 4.6 — 1 May 2026

- Energy flows are logged every half hour for the Tariff Analyser plugin to use.

## 4.5 — 30 April 2026

- Renamed Sigenergy Energy Manager, with critical bug fixes and polish.

## 4.4 — 29 April 2026

- **Flood prevention.** When the battery is above 55% and tomorrow's solar forecast is at least twice the day's need, the plugin empties the battery a little overnight, down to 40%, exporting at the 4 kW export limit. That leaves room in the morning so export runs through the peak hours.
- The inverter itself stops the battery at 40%, and if the run is interrupted the setting is put back at dawn.

## 4.3 — 24 April 2026

- **Winter reserve.** The overnight minimum battery level is 10% from April to September and 20% from October to March, for power-cut cover through longer nights. Both are settings.
- Storm warnings still come first, at 80% for amber or red and 50% for yellow.

## 4.2 — 24 April 2026

- **Power-cut reserve on flat-rate tariffs.** On Tracker and Flexible the plugin keeps the battery at 10% overnight, about 7 to 8 hours of house use. It imports to 12% if needed and stops there. Above that the grid feeds the house directly.

## 4.1 — 24 April 2026

- On Tracker and Flexible the plugin no longer charges the battery from the grid, because charging and then discharging loses about 6% for no saving. The grid feeds the house directly.
- On Tracker it still waits until after midnight if tomorrow's rate is at least 10% cheaper. Go, Flux, Intelligent Go, Intelligent Flux and Agile are unchanged.

## 4.0 — 24 April 2026

- **A new model based on the next 24 hours.** Export starts once the battery plus the rest of today's solar covers a full day's use, with the battery set to fill just at dusk, so on sunny days export starts from about 10am instead of waiting for 90%.
- Import happens only when the battery at dawn plus tomorrow's solar will not cover tomorrow's use. New settings for daily use, 22 kWh on weekdays and 30 kWh at weekends by default.
- Night export removed. The battery runs the house overnight as normal.

## 3.9 — 24 April 2026

- Fixed the overnight import target. It was a fixed 17%, which could sit below the current charge, so the import started and stopped in a loop. The target now covers the dawn reserve plus the night's use, plus 2%.

## 3.8 — 24 April 2026

- Fixed false alarms between 11pm and midnight in summer time. The plugin looked at the wrong day's dawn, took the night to be over 27 hours long and kept importing every minute even with the battery at 54%.

## 3.7 — 23 April 2026

- The plugin now builds its picture of household use, half hour by half hour, from the inverter's own readings instead of the Octopus import meter, which showed only about 0.7 kWh a day on a nearly self-sufficient house. With the wrong figure it expected about 0.3 kWh of use overnight instead of the real 5 to 8 kWh, so it exported freely at night and then had to import at dawn.
- Until it has enough readings it uses a typical UK pattern of about 12 kWh a day. The readings survive restarts.

## 3.4 — 19 April 2026

- **Solar forecast from Open-Meteo** instead of Solcast. The Solcast hobbyist plan covered only two of the four arrays. Open-Meteo covers all four with their real direction, tilt, size and shading, and needs no key.

## 3.3 — 16 April 2026

- **Octopus Flexible** support, importing when needed since the rate is the same all day.
- Fixed import stopping and starting every minute near the threshold. An import now carries on until the target is reached.
- The lowest level the battery may go to now defaults to 1% instead of 10%.

## 3.2 — 11 April 2026

- During an Axle event the plugin no longer rewrites the inverter's power limits. On 10 April it put back a solar charge cap one second after Axle cleared it, causing a brief 2 kW grid import at the start of the event. Any such cap is now cleared at the hand-over.

## 3.1 — 6 April 2026

- **Energy summary variables.** Nine Indigo variables in a Sigenergy folder cover solar, import, export, home use, self-sufficiency, peak and lowest battery level, and what the manager is doing and why, updated every 30 minutes and at midnight.
- **Storm watch.** The plugin checks the MeteoAlarm feed every 2 hours and confirms a warning covers your location. For a storm due within 24 hours it charges to 50% and stops export on yellow, and to 80% on amber or red, with Pushover alerts on escalation and all-clear.
- **Power-cut lockout.** After the grid comes back, export is held off for 4 hours.
- Solar overflow export works in real time. The battery gets just enough charge to fill by dusk and everything above that exports straight away, so export starts earlier on sunny days. The dawn battery target is raised to at least 15% at startup.

## 3.0.2 — 1 April 2026

- The dawn battery target is raised to at least 15% at startup, leaving a buffer above the 10% floor on poor solar days. Before, both were 10%, so a slightly heavier night reached the floor.

## 3.0.1 — 1 April 2026

- Daytime export now happens only if the battery is projected to reach dawn at least 3.5 kWh above the dawn target, about 20%. Exporting at 12p charge that may be needed overnight at 20p or more loses money.

## 3.0 — 1 April 2026

- Fix after the battery fell below its floor on 1 April. The lowest level on the inverter had never been set and sat at the factory 5%. The plugin now checks it every 15 minutes and corrects it, except while an Axle event has raised it.
- Even when tomorrow looks sunny, the plugin now imports if the battery would otherwise fall below its floor before dawn.

## 2.9 — 31 March 2026

- **Urgent fix: night export could run in daylight** if today's dawn time was missing from the forecast, for instance after a restart or a failed forecast fetch, shutting the solar down to 0 W and sending the battery to the grid. Without a dawn time the plugin now treats 7am to 9pm as daylight and blocks export.
- The inverter's mode is checked every 15 minutes and put right if it has drifted, so a restart can no longer leave it stuck exporting.
- A daily plugin log file, kept for 14 days.

## 2.6 — 31 March 2026

- Solar overflow export waits until the battery reaches 40%, so it does not export hard while the battery is still low after the night.
- The Solcast variables for today, tomorrow and last updated now fill in. Before they always read 0.0.

## 2.5 — 30 March 2026

- On days when tomorrow's forecast covers the day's use, overnight import is now switched off entirely, since the inverter already stops the battery at its lowest level. The dawn reserve applies only on poor solar days. 2.4 had no effect because both levels were 10%.

## 2.4 — 30 March 2026

- Overnight import is now solar-aware. If tomorrow's forecast covers the day's use, the plugin imports only if the battery would hit its lowest level before dawn, which stops small needless top-ups before sunny days.

## 2.3 — 30 March 2026

- The dawn check now counts the solar still to come today, less house use to dusk, before deciding to import. Before, a low battery could trigger grid import in daylight with plenty of sun left.

## 2.2 — 30 March 2026

- **Export based on the forecast.** The plugin adds up the solar still to come, takes off the house's use and the room left in the battery, and spreads any real surplus evenly over the rest of daylight, up to the 4 kW export limit. The battery fills as close to dusk as the sun allows while exporting from the first surplus, and solar is never shut down.

## 2.1 — 30 March 2026

- **Daytime solar overflow export.** Above 80% the battery's charge rate is capped at 2 kW so surplus solar goes to the grid, and above 90% at 200 W. The cap lifts below 75%. Solar is never shut down, unlike in an earlier attempt.

## 2.0 — 29 March 2026

- Fixed the "Cannot find Tracker product code" warning that still appeared after 1.8. On accounts with an export meter the export tariff could be picked up first, and Tracker was being left out of the product search.

## 1.9 — 29 March 2026

- Night export no longer stays blocked after a restart. The saved forecast loads straight away at startup, and the forecast is refreshed before the battery decision each cycle.

## 1.8 — 29 March 2026

- Fixed the "Cannot find Tracker product code" warning every 30 minutes. The plugin now finds Tracker the same way it finds Go, Flux and Agile.

## 1.7 — 29 March 2026

- The Solcast forecast loads from its saved copy at startup. Before, every restart cleared it, tomorrow's forecast read 0.0 and night export was blocked until the next fetch, up to 2.4 hours later.
- A warning appears if the saved copy is over 7.2 hours old or the forecast is empty.

## 1.6 — 29 March 2026

- Intelligent Go's cheap window corrected to 11:30pm to 5:30am.
- Intelligent Flux is cheap at all times outside 4pm to 7pm, so the plugin now imports then rather than waiting for a 2am window that does not exist on that tariff.

## 1.5 — 29 March 2026

- When an Axle event is announced, the lowest battery level is raised straight away to the dawn target plus the event's export, rather than later at pre-charge. It is put back if the event is cancelled as well as after it ends.

## 1.4 — 29 March 2026

- Night export now stops at the forecast sunrise. It could never stop on solar, because solar reads 0 W while the battery is exporting.
- The battery now supplies the house and 4 kW to the grid at the same time.

## 1.3 — 29 March 2026

- **Night export.** At night, with a high battery and a good forecast for tomorrow, the plugin sends surplus to the grid.
- Fixed a reduced power limit left on the inverter after a forced discharge, which capped the battery in normal running.
- Tomorrow's check now uses 60% of the corrected forecast instead of the most pessimistic one.

## 1.2 — 27 March 2026

- Inverter size corrected to 10 kW.

## 1.1 — 27 March 2026

- Fixed overnight grid import caused by the export limit being set to 0 W when export stopped.
- Export now restarts only after a 10% gap, so it does not flick on and off.

## 1.0 — 26 March 2026

- First release, replacing Sigenergy Solar 3.1.
