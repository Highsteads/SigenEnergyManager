# Sigenergy Manager for Indigo

**Runs a Sigenergy solar battery from Indigo, so the house buys as little from the grid as it can.**

**Version:** 5.125.0 | **Author:** CliveS & Claude | **Needs:** Indigo 2025.2 or later and a Sigenergy inverter

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
