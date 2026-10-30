# Sigenergy Manager for Indigo

**Runs a Sigenergy solar battery from Indigo, so the house buys as little from the grid as it can.**

**Version:** 5.140.1 | **Author:** CliveS & Claude | **Needs:** Indigo 2025.2 or later and a Sigenergy inverter

**[Read the full guide](https://highsteads.github.io/SigenEnergyManager/)** — setting up, how it decides what the battery does, and what to do when something goes wrong.

---

## What it does

This plugin lets [Indigo](https://www.indigodomo.com) run a Sigenergy solar and battery system. It reads the inverter over your home network every few seconds, and once a minute it decides what the battery should do next, with one aim above the rest: buy as little from the grid as it can.

- **Works out every minute whether the battery will last until the sun comes back,** from the battery's level, a solar forecast for your own roof, and what your house really uses at each time of day, which it learns for itself.
- **Charges from the grid only when it has to,** in the cheapest hours your Octopus tariff offers — Tracker, Flexible, Go, Intelligent Go, Flux, Intelligent Flux or Agile, which it reads from your Octopus account.
- **Keeps a reserve in the battery overnight** for a power cut, higher in the winter than the summer.
- **Sells spare solar in the day** without letting the battery miss its target, and on a smaller day waits until the battery is nearly full before it sells anything.
- **Makes room in the battery overnight** before a very sunny day, so the morning sun is not wasted.
- **On Octopus Flux,** can fill the battery in the cheap 2am to 5am window with what the house will need, and sell what is truly spare between 4pm and 7pm.
- **Watches for power cuts and storm warnings,** tells you by Pushover and email when the power goes and comes back, and keeps more in the battery while a Met Office warning covers your house.
- **Takes part in Octopus Saving Sessions** if you want it to — joining Power Downs, selling from the battery during them, booking your Weekend Happy Hours, filling the battery with the free electricity, and checking Octopus pay back what they owe.
- **Runs Axle events** if you are signed up with Axle, and keeps a record of what each one paid.

I run it on my own house, with 14.25 kWp of solar in four arrays, a 10 kW Sigenergy inverter and 35.04 kWh of battery, on Octopus Flux.

## What it works with

| You need | For |
|---|---|
| **A Sigenergy inverter** with its local network connection switched on — Sigenergy's setting is **ModBus TCP Server Enable** | Everything |
| **Your roof's latitude and longitude** | The solar forecast, which comes free from [Open-Meteo](https://open-meteo.com) |
| **An Octopus Energy account and API key** | Knowing your tariff and prices, and the costs |
| The **Pushover** plugin for Indigo | Optional — alerts on your phone |
| An **Email+** mail account in Indigo | Optional — power-cut emails |
| An **Axle** account | Optional — Axle events |

My [Dashboards plugin](https://github.com/Highsteads/Dashboards) reads this plugin's figures for its Energy and Cost pages.

## Installing

1. Go to the [Releases page](https://github.com/Highsteads/SigenEnergyManager/releases/latest) and download `SigenEnergyManager.indigoPlugin.zip`
2. Unzip the downloaded file — you will get `SigenEnergyManager.indigoPlugin`
3. Double-click `SigenEnergyManager.indigoPlugin` — Indigo will install it automatically

## Setting it up

1. Open **Plugins → Sigenergy Manager → Configure** and fill in **Inverter IP address** — the four numbers, such as `192.168.1.49`, that your router shows for the inverter — and **Site latitude** and **Site longitude**.
2. Fill in **Battery capacity**, **Inverter max charge power**, and your Octopus details under **OCTOPUS ENERGY**. If you sell to the grid, tick **Enable grid export** and set **DNO export limit**. Click **Save**.
3. Create a **New Device** of type **Sigenergy Manager** for each of **Sigenergy Inverter**, **Battery Manager**, **Solar Forecast** and **Tariff Monitor**, and **Axle VPP Monitor** if you use Axle.
4. Choose **Plugins → Sigenergy Manager → Run Self-Test (Check All Subsystems)**. The Event Log should say each part works.

The [full guide](https://highsteads.github.io/SigenEnergyManager/) goes through each step, explains every setting, and covers what to do if something does not work.

## What's new

**5.140.1 (30 October 2026)** - An ordinary night's charge can now reach 100%, not 95%, when the day ahead needs it. It still buys only what the day needs and leaves room for the sun, so a bright day is charged no higher than before. On a dull day the extra sells in the 4pm to 7pm peak or saves buying at the day rate.

**5.140.0 (30 October 2026)** - A cloudier or brighter day than forecast now catches the plugin out less often. The corrected solar figures now blend three weather models, the Met Office and two from ECMWF, each one scaled by how far out it has been at your house. Tested on 158 past days, the average miss fell from 5.4 kWh a day to 4.2. If the extra models cannot be read, the plugin goes back to the old correction by itself. The plugin also now scores tomorrow's forecast against the day itself, and logs how it did each midnight.

**5.139.2 (28 October 2026)** - A restart between 4pm and 7pm now finishes handing the inverter back before Indigo's time limit. Every setting the plugin writes during a Flux hand-back was being read back twice; it is now read back once, which takes the hand-back from about 23 seconds to about 17, inside the roughly 22 seconds Indigo allows. Each Flux change of mode during the day is about five seconds quicker as a result.

**5.139.1 (28 October 2026)** - A restart between 4pm and 7pm no longer loses the day's energy totals. Handing the inverter back takes about 23 seconds and Indigo allows a plugin about 22 to stop, so every restart in the Flux peak since 26 October was cut off before the totals were saved. The totals are now saved first, and the inverter is no longer handed back twice. Indigo can still stop the plugin during the last checks of a peak-time hand-back, but by then every setting has been written. Octopus allow two Happy Hours a weekend, and Saturday slots start appearing from 1 November, so the plugin now counts Saturday's bookings against Sunday's. A held-back message names the day instead of always saying Sunday.

**5.139.0 (28 October 2026)** - Octopus have extended the Weekend Happy Hour scheme to 14 February 2027, and from 1 November it is paid for with tokens: usually two tokens buy an hour, sometimes three on a dearer weekend. The plugin now uses 14 February as its end date, so the "use it by" note and the check that refuses a Power Down settling too late both follow it. Set the tokens-per-hour figure in Configure to what Octopus charge you, because their API does not report it.

**5.138.2 (28 October 2026)** - After a short drop in the inverter connection, the plugin has live readings again within seconds instead of half a minute. It used to re-read every register before giving the first reading after any reconnect; now, while its stored readings are still fresh, it reads only the power figures, a few of the slower ones and the battery settings, and keeps the full re-read for a drop of more than five minutes.

**5.138.1 (26 October 2026)** - A confirmed manual clearance of a false day-rate export block now saves immediately, so restarting cannot restore the old flag. Overnight charging remains 2am to 4:58am.

**v5.138.0** — Overnight Flux charging now runs from 2am to 4:58am, leaving two minutes to stop and confirm the hand-back before the cheap rate ends at 5am. The main planner and fallback use the earlier deadline, including their calculation of charging time. Prices still use the real 2am to 5am tariff window.

**v5.137.1** — The charging hand-back fix also preserves the restart safeguard: an unexpected grid charge is recorded before stopping it. Only the controller's own recent cheap-window charge is recognised as a handover, and a failed correction still blocks the sale.

**v5.137.0** — Ending an overnight charge no longer leaves an old charging reading that blocks the afternoon Flux sale. The plugin reads the inverter again after correcting its mode. A dated, confirmed review action can clear an incorrect day-rate block without forcing export or removing the reserves and 7pm stop.

**v5.136.0** — A free Happy Hour now charges the battery at full rate even when Solar Overflow was holding charging back beforehand. On 25 October the battery stored 0.3 kWh of a free 4 kWh, because the overflow limit of 200 W stayed in force. If an import is ever short again, the plugin checks the inverter's charge limit before it changes mode, so the panels stay on. The note after a free hour now says how much the battery stored, rather than assuming most of it, and gives money as pounds and pence.

**v5.135.0** — When a free Happy Hour is booked for later in the day, Solar Overflow now leaves the battery room for it. Spare sunshine before the free hour is exported instead of filling the battery, and the free hour then fills that room from the grid for nothing. The room never takes the battery below the power-cut level. During a storm warning the free hour now fills the battery to 100%, as Solar Overflow already did.

**v5.134.1** — After a restart the plugin no longer sends "Battery import held tonight" while it waits for its first Octopus tariff read. If Octopus still has not answered after five minutes, the alert says the tariff has not been read yet, rather than calling it unrecognised.

**v5.134.0** — An ordinary night's cheap-window charge now stores 95%, not 100%. A storm warning still fills to 100%, and so does a night with an announced Axle event, which the battery owes. A booked free hour's room is still kept back on an ordinary night.

**v5.133.2** — The Met Office warnings feed gives its times in UTC, and the plugin now reads them as UTC. Before, it read them as UK time, so each warning started and ended an hour early.

**v5.133.1** — If the Met Office cannot be read, the log now says so. Before, the storm level quietly came from MeteoAlarm alone.

**v5.133.0** — Any yellow, amber or red wind warning now fills the battery to full during the 2am to 5am cheap window, and a warning no longer stops Flux charging in that window. The Met Office is read alongside MeteoAlarm, because MeteoAlarm can lag it; the higher colour wins, and a disagreement is logged. Set the region in Configure ("Met Office region").

**v5.132.3** — Flux, Go, iGo and iFlux run on the clock, so the plugin now keeps their cheap-window times and last known prices when Octopus has not answered, instead of holding the grid import. Tomorrow's rates variable now carries today's bands one day on until Octopus publishes, where before it kept a day-old copy. The Flux controller accepts rates up to 36 hours old (was 3). The held-import Pushover no longer blames the Octopus connection on these tariffs.

**v5.132.2** — The 5am import hold no longer blocks tomorrow's cheap window. Once the 2am window had closed, the plugin compared it with this morning's sunrise rather than the sunrise that ends its own night, so it held the grid import and blamed the price feed. The hold now looks at the right sunrise.

**v5.132.1** — The free-hour check no longer says Octopus paid short. It was counting an extra half hour of electricity in each free hour; Octopus had paid the 27 September free hours in full. Daily electricity and gas totals no longer include the first half hour of the next day.

**v5.132.0** — Octopus being slow no longer holds up the battery, one failed check no longer switches Flux off, an Axle blip no longer cancels an event, and the 4pm sale no longer starts and stops on sunny afternoons. Plus fixes to the 2am charge, event cover, the summer drain and the daily records.

**v5.131.2** — The inverter is handed back properly when the plugin stops, the 2am charge can no longer be refused all night on a day with an Axle event, and a refused Happy Hour booking is no longer counted as booked.

**v5.131.1** — The 4pm to 7pm sale no longer stops for a few seconds every few minutes as its reserve moves, and its plan is logged once rather than every few minutes.

**v5.131.0** — Nothing happens on the inverter half an hour before an Axle event any more; the event's battery floor is set two minutes before it starts, as the event takes over.

**v5.130.1** — The fallback overnight charge fits the house's supply, a shortfall against the 50% minimum is always reported, and 5am no longer counts 49% as meeting 50%.

**v5.130.0** — The 50% overnight minimum now holds even when the Flux strategy cannot plan, and 5am reports the level the battery actually reached.

**v5.129.2** — A day recorded before Octopus has published its prices is valued provisionally at the last published prices, then re-priced automatically once the real ones arrive.

**v5.129.1** — When Octopus has not published one side of the Flux prices yet, the strategy plans on the last published day's prices until it does.

**v5.129.0** — The 2am charge now brings the battery up to at least 50% every night, except on a day with a booked free hour, so a forecast that is too sunny no longer leaves too little to sell at the peak.

**v5.128.1** — A Saving Session no longer holds back energy an Axle event already covers, and the 4pm to 7pm sale no longer switches on and off while it waits for a session.

**v5.128.0** — The 2am charge plans on 80% of the solar forecast all year, so a forecast that is too sunny no longer leaves the 4pm to 7pm sale short.

**v5.127.1** — The 4pm to 7pm sale no longer stops half an hour before an Axle event. It carries on, still keeping the event's energy back, until the event takes over two minutes before it starts.

**v5.127.0** — The 4pm to 7pm sale is timed to fit a Saving Session. When there is not enough spare to sell for the whole three hours, the session gets its share of the sale instead of the sale running out before it starts.

**v5.126.0** — On Octopus Flux, Saving Sessions no longer have battery energy of their own. Inside 4pm to 7pm the normal peak sale runs through them; outside it the plugin still joins them but exports nothing, and the battery runs the house.

**v5.125.4** — An Axle event now always leaves enough to reach 2am. The top-up before an event goes by the sun the day is really giving and keeps 1 kWh spare, since Axle pays about £1 a kWh against about 30p to buy.

**v5.125.3** — The Axle cover and the 4pm-7pm plan now allow for how the day's sun is actually going, so a dull day after a sunny forecast no longer leaves the battery short after an Axle event.

**v5.125.2** — An announced Axle event no longer stops the battery running the house hours before it starts. The event is still covered by the overnight charge and, if needed, a small top-up before 4pm.

**v5.125.1** — The battery no longer stops for half a minute when the Flux controller changes what it is doing (taking over at 4pm, switching between selling and running the house, handing back at 7pm, or checking the inverter after a restart). The house used to draw from the grid for those 15 to 30 seconds.

**v5.125.0** — The solar panels keep working while the battery charges from the grid. The inverter used to be told to charge from the grid first, and it switched the panels off to do it: in the first Weekend Happy Hour they made nothing for two hours. The battery now takes the sun first and the grid for the rest, so it fills just as fast and no sun is wasted.

**v5.124.0** — The battery always runs the house during the day. The plugin buys from the grid in the day only in free-electricity hours, when the battery reaches its lowest allowed charge, or to cover an Axle event the battery could not manage alone, and then only enough for the event and the house until 2am. After a restart it no longer assumes the Tracker tariff while it waits for Octopus.

**v5.123.0** — Before the 4pm peak the battery is held back only when it would otherwise be short of what the peak can sell and what the house needs until 2am. A battery that already has plenty keeps running the house, instead of the house buying at the standard price for energy the peak could never sell.

**v5.122.0** — The daily patterns are published for the Dashboards Energy page (3.51.0), which charts each kind of day against the everyday pattern.

**v5.121.0** — Tuesday to Friday now share a pattern of use measured from those days alone, so every day of the week is planned from how it really runs. The four days are too alike to tell apart, so they are measured together.

**v5.120.0** — Mondays now have their own pattern of use too, alongside Saturday and Sunday. Here a Monday uses a little more between 5pm and 8pm than the everyday pattern says, which falls in the Flux peak.

**v5.119.1** — The Event Log line saying a day has its own pattern of use now appears once, not after every restart.

**v5.119.0** — Saturdays now have their own pattern of use as well, measured from the last 18 Saturdays: most of a Saturday's extra use comes between 10am and 1pm. The half-hourly energy record now writes a row at midnight, so the last half-hour of each day is no longer recorded as the first of the next.

**v5.118.0** — Sundays now have their own pattern of use through the day. The plugin measures it from the last 18 Sundays, so a roast and a wash in the afternoon are planned for in the afternoon, not spread across the day. The total for a Sunday is unchanged. Until six whole Sundays are recorded, Sundays use the everyday pattern as before.

**v5.117.0** — Fixes found while writing the guide. A position left at 0.0 in `IndigoSecrets.py` now counts as not set, so the one in the settings is used. **Emergency Import Triggered** now fires only when the battery charges to hold its power-cut reserve. The example secrets file names the Axle token `AXLE_API_KEY`, as the plugin reads it. With no position set anywhere, the storm check now waits for one instead of watching a built-in area.

**v5.116.0** — The plugin checks that Octopus pay back each Weekend Happy Hour.
- Four times a day it works out what each free hour is owed and looks for the credit on your Octopus account, with one Pushover when it is paid, one if it is more than 5p short, and one if nothing has come after two weeks.
- It reads Octopus's own result for every Power Down you joined, and your OctoPoints balance, for the Dashboards Energy page.

**v5.115.0** — On Octopus Flux, the Flux strategy does the 2am charge on its own, and the ordinary plan steps in only if it cannot make a plan. A dull afternoon, when the house uses more than the panels make, now counts when the plugin works out what will be left at dawn.

| Version | Released | In short |
|---|---|---|
| 5.140.1 | 30 October 2026 | An ordinary night's charge can reach 100% when the day ahead needs it. |
| 5.140.0 | 30 October 2026 | Corrected solar figures blend three weather models, and tomorrow's forecast is now scored. |
| 5.139.2 | 28 October 2026 | A Flux hand-back reads each setting back once, so a peak-time restart finishes in time. |
| 5.139.1 | 28 October 2026 | A peak-time restart keeps the day's totals; two Happy Hours a weekend, Saturday included. |
| 5.139.0 | 28 October 2026 | Weekend Happy Hour scheme runs to 14 February 2027, with tokens from 1 November. |
| 5.138.2 | 28 October 2026 | Live readings within seconds after a short drop in the inverter connection. |
| 5.138.1 | 26 October 2026 | Save confirmed export-block clearance immediately so it survives restart. |
| 5.138.0 | 26 October 2026 | Overnight Flux charging ends at 4:58am, with tariff pricing unchanged |
| 5.137.1 | 26 October 2026 | Preserve day-rate protection for an unexpected grid charge found after restart |
| 5.137.0 | 26 October 2026 | Charging hand-back no longer leaves a false peak-export block, with a guarded review action |
| 5.136.0 | 25 October 2026 | A free hour charges at full rate even after Solar Overflow, and its note says what the battery stored |
| 5.135.0 | 25 October 2026 | Solar Overflow leaves room for a booked free hour later the same day |
| 5.134.1 | 24 October 2026 | No import-held alert while the first Octopus tariff read is awaited after a restart |
| 5.134.0 | 24 October 2026 | Ordinary nights store 95% from the cheap window; warnings and announced Axle events still fill to 100% |
| 5.133.2 | 24 October 2026 | Met Office times read as UTC, so warnings no longer start and end an hour early |
| 5.133.1 | 24 October 2026 | A Met Office read failure is logged, not silent |
| 5.133.0 | 24 October 2026 | Any storm warning fills the battery in the cheap window; the Met Office is a second warning source |
| 5.132.3 | 18 October 2026 | Fixed-band tariffs keep their windows and last known prices when Octopus is silent |
| 5.132.2 | 18 October 2026 | The 5am import hold judges tomorrow's cheap window against its own night's sunrise |
| 5.132.1 | 5 October 2026 | Free-hour check and daily meter totals count only their own half hours |
| 5.132.0 | 2 October 2026 | Review part 2: control, money and records fixes |
| 5.131.2 | 2 October 2026 | Hand-back at shutdown; 2am charge never refused; Happy Hour refusals |
| 5.131.1 | 1 October 2026 | Peak sale moves its floor without stopping; one log line |
| 5.131.0 | 1 October 2026 | Axle "pre-charge" writes nothing; floor set at the start |
| 5.130.1 | 1 October 2026 | Fallback charge sized to the site; shortfalls always shown |
| 5.130.0 | 1 October 2026 | The 50% minimum holds when Flux cannot plan |
| 5.129.2 | 1 October 2026 | Unpublished days valued provisionally, re-priced later |
| 5.129.1 | 1 October 2026 | Missing Flux prices: plan on the last published day's |
| 5.129.0 | 30 September 2026 | The 2am charge reaches at least 50% every night |
| 5.128.1 | 30 September 2026 | Session holdback agrees with Axle; no on-off cycling |
| 5.128.0 | 29 September 2026 | The 2am charge plans on 80% of the solar forecast |
| 5.127.1 | 28 September 2026 | The peak sale runs on until an Axle event starts |
| 5.127.0 | 28 September 2026 | The peak sale is timed to fit a Saving Session |
| 5.126.0 | 28 September 2026 | Saving Sessions on Flux: no kWh of their own, no export outside the peak |
| 5.125.4 | 28 September 2026 | Axle cover always reaches 2am |
| 5.125.3 | 28 September 2026 | Axle cover follows the day's real sun |
| 5.125.2 | 28 September 2026 | An Axle event no longer stops the battery early |
| 5.125.1 | 27 September 2026 | No half-minute battery stop when Flux changes mode |
| 5.125.0 | 27 September 2026 | Solar keeps working during a grid charge |
| 5.124.0 | 27 September 2026 | Battery never stopped in the day; Axle events covered |
| 5.123.0 | 27 September 2026 | No hold before the peak when the battery has plenty |
| 5.122.0 | 27 September 2026 | Day patterns published for the Energy page |
| 5.121.0 | 27 September 2026 | Tuesday to Friday get a measured pattern |
| 5.120.0 | 27 September 2026 | Mondays get their own pattern too |
| 5.119.1 | 27 September 2026 | Day-pattern note logged once |
| 5.119.0 | 27 September 2026 | Saturdays get their own pattern too; honest midnight record |
| 5.118.0 | 27 September 2026 | Sundays get their own pattern of use |
| 5.117.0 | 27 September 2026 | Fixes found while writing the guide |
| 5.116.0 | 26 September 2026 | Free-hour payments checked, Power Down results read |
| 5.115.0 | 26 September 2026 | The Flux strategy owns the 2am charge |
| 5.114.0 | 26 September 2026 | The day rate buys only what the peak needs |
| 5.113.0 | 24 September 2026 | The Flux charge no longer buys what the sun will supply |
| 5.112.0 | 22 September 2026 | Weekend Happy Hours booked for you |
| 5.111.0 | 19 September 2026 | On Flux, the battery takes all the sun once none can be lost |
| 5.110.0 | 17 September 2026 | Flux costs priced at the rate each unit was bought at |
| 5.109.0 | 17 September 2026 | The Octopus Flux strategy |

Every version is listed in the [version history](https://highsteads.github.io/SigenEnergyManager/changelog.html).

## Authors & licence

Vibed into existence by **CliveS**, who knew what he wanted, argued until he got it, and tested it on a real house. Typed at inhuman speed by **Claude** (Anthropic), who mostly did as it was told.

© 2026 CliveS · [MIT licence](LICENSE) — copy it, fork it, bend it, break it, fix it, ship it. If it breaks, you get to keep both pieces.
