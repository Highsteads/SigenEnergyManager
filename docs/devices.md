---
title: Your devices
nav_order: 3
---

# Your devices

The plugin has five kinds of device. Add one of each you want — none of them has any settings, and the plugin fills them in by itself. The names below are the ones Indigo shows on control pages and in triggers.

Power is in watts, and a plus or minus sign shows the direction. For the grid, plus is buying and minus is selling. For the battery, plus is charging and minus is running the house.

## Sigenergy Inverter

The live readings from the inverter, brought up to date every 10 seconds unless you choose otherwise in the settings. The device list shows the battery level.

| Shown as | What it means |
|---|---|
| **Battery SOC (%)** | How full the battery is. |
| **PV Power (W)** | What the solar panels are making now. |
| **Grid Power (W, +import/-export)** | What the house is buying from or selling to the grid now. |
| **Battery Power (W, +charge/-discharge)** | What is going into or out of the battery now. |
| **Home Consumption (W)** | What the house is using now. |
| **PV Daily**, **Grid Daily Import**, **Grid Daily Export**, **Home Daily** (kWh) | Today's totals, from midnight. |
| **Battery Daily Charge** and **Battery Daily Discharge** (kWh) | What went into and came out of the battery today. |
| **Energy Balance Today (kWh)** | A check that today's figures add up — solar and grid in against house, battery and grid out. It should stay close to zero. |
| **Grid Status** and **Grid Online (1=on-grid, 0=power cut)** | Whether the mains is on. Grid Online is 0 during a power cut, which makes it easy to use in a trigger. |
| **Grid Voltage (V)**, **Grid Current (A)**, **Grid Frequency (Hz)** | The state of the mains supply. |
| **PV String 1** to **PV String 4** (V, A and W) | Each solar input on the inverter on its own. The settings let you give each one a name for the dashboards. |
| **Battery SOH (%)** | The battery's health, as the inverter judges it. |
| **Battery Temp**, **Battery Max Temp**, **Battery Min Temp** | The average temperature of the battery, and of its hottest and coldest packs. |
| **Pack Temp Spread** and **Pack Balance** | How far apart the packs are, and whether one is running hot or cold compared with the rest. |
| **Avg Cell Voltage (V)** | The average voltage of the battery's cells. |
| **Inverter Temp (degC)** | The temperature inside the inverter. |
| **PV Insulation (MOhm)** | The insulation reading of the solar wiring, which the inverter measures as a safety check. |
| **Inverter Alarm** and **Inverter Alarm Code** | Whether the inverter has raised an alarm, and its code. |
| **Discharge Cutoff SOC (%)** | The lowest level the inverter will let the battery go to right now. The plugin moves it — for example, to stop a sale at the right level. |
| **EMS Work Mode** and **Plant Running State** | How the inverter is running, in Sigenergy's own words. |
| **Modbus Connected** | Whether the plugin can talk to the inverter. |
| **Last Update** | When the readings last came in. |

## Battery Manager

What the plugin has decided and why. The device list shows **Running**, or **Paused** when you have paused it, or **Modbus offline — holding** when it cannot reach the inverter and so is not changing anything.

| Shown as | What it means |
|---|---|
| **Manager Status** | Running, Paused, or holding because the inverter cannot be reached. |
| **Current Mode** | What the battery is doing, as one of a fixed list, which makes it the best state for a trigger: **Self Consumption** (running the house as normal), **Solar Overflow Export** (selling spare solar in the day), **Grid Import Active**, **Import Scheduled**, **Import Stopping**, **Night Export Active** (making room overnight before a sunny day), **Export Stopping**, **VPP Export Active** (an Axle event), **Saving Session Export** and **Happy Hour Import**. |
| **Current Action** and **Decision Reason** | The same decision in words, with the reason and the figures behind it. |
| **Projected SOC at Dawn (kWh)** and **Dawn SOC Viable** | How much the plugin expects to be in the battery when the sun comes up, and whether that is enough. |
| **Grid Import Active**, **Import Scheduled**, **Import Scheduled Time**, **Import Quantity (kWh)** | Whether the battery is charging from the grid, or will be, when, and how much. |
| **Grid Export Active** and **Export Power (kW)** | Whether the battery is selling to the grid, and how fast. |
| **Flood Prevention Active** and **Flood Prevention Target SOC (%)** | Whether it is making room overnight before a sunny day, and how low it will go. |
| **Banking First**, **Bank-First Target %**, **Bank-First Day Forecast kWh** | Whether it is holding back daytime selling until the battery is nearly full, and the figures it is working to. [Selling to the grid](selling.md) explains this. |
| **Power Cut Lockout Active** and **Power Cut Lockout Remaining (min)** | Whether selling is held off after a power cut, and for how much longer. |
| **Active Tariff**, **Rate Today (p/kWh)**, **Rate Tomorrow (p/kWh)** | Your tariff and prices. |
| **Tomorrow Solar (kWh)**, **Tomorrow Need (kWh)**, **Need Today (kWh)** | The solar expected tomorrow, and what the house is expected to use today and tomorrow. |
| **Solar vs Forecast Today (%)** | How today's solar is doing against the forecast so far. |
| **Away Mode** | Whether it is planning for an empty house. |
| **Happy Hour import active**, **Happy Hour Tokens**, **Happy Hour free kWh** | An Octopus free hour running now, how many Happy Hour tokens you hold, and how much the last free hour put in. |
| **Last Update** | When the manager last decided. |

## Solar Forecast

The solar forecast for your roof, fetched every 30 minutes. The device list shows today's forecast.

| Shown as | What it means |
|---|---|
| **Today P50 (kWh)** and **Tomorrow P50 (kWh)** | The forecast for today and tomorrow, as it comes from the weather model. |
| **Today Corrected (kWh)** and **Tomorrow Corrected (kWh)** | The same forecasts corrected by how far out they have been at your house. The plugin plans from these. |
| **Bias Correction Factor** | The correction it applied — below 1 means the forecast has been running high. |
| **Remaining Today (kWh)** | The solar still to come today. |
| **Current Hour (W avg)** and **Next Hour (W avg)** | The forecast for this hour and the next. |
| **Forecast Status** | Whether the last fetch worked. |
| **Last Update** | When the forecast last came in. |

## Tariff Monitor

Your Octopus tariff and prices, fetched every 30 minutes. The device list shows the tariff's name.

| Shown as | What it means |
|---|---|
| **Active Tariff** | The tariff your account is on. It says **(forced)** if you have set **Plan as though the tariff were** in the settings. |
| **Rate Today (p/kWh)** and **Rate Tomorrow (p/kWh)** | The price that applies to you. |
| **Tracker**, **Go**, **Flux** and **Flexible** rates | The cheap, standard and dear prices for each of these tariffs, left blank where the plugin has no price for one. |
| **Last Update** | When the prices last came in. |

## Axle VPP Monitor

Only needed if you take part in Axle events. [Axle events](axle.md) explains what the plugin does for them.

| Shown as | What it means |
|---|---|
| **VPP Status** and **VPP State** | Whether an event is announced, getting ready, running, or nothing is due. |
| **Event Start Time** and **Event End Time** | The next event. |
| **Pre-Charge SOC Required (%)** | The battery level the event needs. |
| **Estimated Earnings (GBP)** and **VPP Export Running (kWh)** | What the current event is expected to pay, and what has gone out so far. |
| **Last VPP Event Date**, **Last VPP Event Export (kWh)** and the other **Last VPP** readings | A summary of the last event — what it sold, what the solar did during it, the highest battery and grid power, and whether it was run by the plugin. |
| **Lifetime Earnings**, **Available Balance**, **Earned This Month**, **Grid-Event Earnings, Lifetime** (GBP) | What Axle have paid you, from your Axle account. |
| **Events Settled** and **Events Awaiting Settlement** | How many events Axle have paid for, and how many they have still to pay. |
| **Ledger Last Imported**, **Ledger Age**, **Ledger Feed Health** | When the earnings figures last came in, and a warning if they have stopped. |
| **Axle API Status** and **Axle API Last Good Poll** | Whether the plugin can reach Axle. |

## Indigo variables

The plugin also keeps a set of Indigo variables in a folder called **Sigenergy**, which it creates. The main ones:

| Variable | What it holds |
|---|---|
| **sigen_manager_paused** | Set it to `true` from anything in Indigo to pause the plugin, and back to `false` to let it carry on. [Actions and triggers](actions-and-triggers.md) explains pausing. |
| **sigen_today_pv_kwh**, **sigen_today_import_kwh**, **sigen_today_export_kwh**, **sigen_today_home_kwh** | Today's running totals, written every 30 minutes. |
| **sigen_today_self_suff_pct** | Today's self-sufficiency — the share of what the house used that did not come from the grid. |
| **sigen_decision_action** and **sigen_decision_reason** | The plugin's latest decision. |
| **elec_**, **gas_** and **export_** variables | Today's and this month's costs, units and export earnings, your unit rates and standing charges, and your Octopus account balance. |
