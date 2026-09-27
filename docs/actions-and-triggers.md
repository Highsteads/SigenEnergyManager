---
title: Actions and triggers
nav_order: 5
---

# Actions and triggers

## Actions

Add these to an action group, a schedule or a trigger by choosing **Sigenergy Manager** as the action type. Most of them work on a device, so choose your **Sigenergy Inverter**, **Battery Manager**, **Solar Forecast** or **Tariff Monitor** device when asked.

The plugin decides afresh every minute, so it can undo something you started by hand once it no longer fits its plan. To be sure a forced action holds, run **Pause Battery Manager** first, and **Resume Battery Manager** when you are done.

| Action | Device | What it does |
|---|---|---|
| **Pause Battery Manager** | Battery Manager | Hands the inverter back to run the house as normal, then leaves it alone until you resume. The plugin still reads the inverter and keeps the devices up to date. Setting the **sigen_manager_paused** variable to `true` does the same. |
| **Resume Battery Manager** | Battery Manager | Lets the plugin carry on deciding, straight away. |
| **Force Grid Import** | Sigenergy Inverter | Charges the battery from the grid now. Set **Charge power** in kW — it will not go above the inverter's rating in the settings — and **Target SOC**, from 10% to 100%, where it stops. It refuses while an Axle event is getting ready or running, because charging then would lose the event's payment. |
| **Force Grid Export** | Sigenergy Inverter | Sells from the battery to the grid now. The inverter keeps the sale to your export limit by itself. **Battery discharge limit** lets you slow the battery, if you want to. |
| **Set Self-Consumption Mode** | Sigenergy Inverter | Puts the inverter back to running the house from the battery as normal. Use it to end a forced charge or sale. |
| **Return to Local EMS Control** | Sigenergy Inverter | Hands the inverter back to its own control entirely, as if the plugin were not there. The plugin takes it over again the next time it needs to, unless you pause it first. |
| **Refresh Solar Forecast** | Solar Forecast | Fetches a new solar forecast now instead of waiting up to 30 minutes. |
| **Refresh Octopus Rates** | Tariff Monitor | Fetches your prices from Octopus now. |
| **Import Axle Settlement Email** | — | Reads Axle's results email from an Email+ mail account and files what the event paid. [Axle events](axle.md) explains it. |

Two more actions are for testing the inverter, not for everyday use:

| Action | What it does |
|---|---|
| **Force Daytime Export (PV First, test)** | Sells with the solar going to the grid first and the battery making up the rest. |
| **Force VPP Export Drive (auto bank/discharge, test)** | Runs the plugin's Axle event selling once, as it would in a daytime event, against the solar and house as they are now. |

Pause the manager before either, and afterwards run **Set Self-Consumption Mode** and then **Resume Battery Manager**.

## Triggers

Create a new trigger, set its type to **Sigenergy Manager**, and choose one of these:

| Trigger | Runs when |
|---|---|
| **Emergency Import Triggered** | The plugin starts charging the battery from the grid. Despite its name, it runs for every charge the plugin starts, not only in an emergency. |
| **Grid Export Started** | The battery starts selling to the grid — to make room overnight before a sunny day, or at the start of an Axle event. |
| **Grid Export Stopped** | That selling stops. |
| **Flood Prevention Pre-Drain Started** | The plugin starts making room in the battery overnight before a very sunny day. |
| **Flood Prevention Pre-Drain Stopped** | That stops — the battery reached its target, dawn came, the forecast dropped, or selling was turned off. |
| **VPP Event Announced** | Axle announce a new event. |
| **VPP Event Started** | An Axle event starts. |
| **VPP Event Ended** | An Axle event ends. |
| **Power Cut Lockout Started** | The grid comes back after a power cut and the plugin holds off selling. |
| **Power Cut Lockout Cleared** | That hold-off ends. |

### Using device states

Indigo's own **Device State Changed** trigger works with the plugin's devices too, and covers anything the list above does not. Two useful ones:

- **Current Mode** on the **Battery Manager** — choose a mode such as **Saving Session Export**, **Happy Hour Import** or **Solar Overflow Export** to run something when the battery starts doing it.
- **Grid Online** on the **Sigenergy Inverter** — becomes 0 when the power goes and 1 when it comes back.

For example, you could have a trigger on **Current Mode** becoming **Happy Hour Import** switch on an immersion heater, so the free hour heats the water as well as filling the battery.
