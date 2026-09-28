---
title: Octopus Flux
parent: How it works
nav_order: 4
---

# Octopus Flux

Octopus Flux has three prices for what you buy and three for what you sell, on the same clock: cheap from 2am to 5am, dear from 4pm to 7pm, and a standard price the rest of the day. It is made for a house with a battery — fill it cheaply overnight, and sell what is spare when the price is highest.

On Flux the ordinary plan already charges in the cheap hours and paces the day's solar charge to finish by 4pm. The **Flux strategy** goes further: it plans the whole day, sizes the overnight charge to include what the battery can sell at the peak, and runs the sale. It is switched off to start with.

I have run it on my own house since 17 September 2026.

## What it does

**From 2am to 5am** it charges the battery with what the house will need, worked out hour by hour from what your house uses, the solar forecast, the losses of charging, the size of the battery, the time left to charge, and any Axle event already announced. It leaves room for the day's solar. It adds energy to sell at the peak only when the sale pays after the losses and the battery wear charge, and only as much as the 4pm to 7pm sale can use beyond what the sun will leave in the battery by 4pm on its own. It also checks that a day 35% sunnier than forecast would not push that energy straight back out to the grid at the standard price.

**Through the day** the battery always runs the house. The plugin never stops it to save charge for the peak. The house only draws from the grid during free-electricity hours, when the battery reaches its lowest allowed charge, or when an [Axle event](axle.md) needs more than the battery holds, and then only enough to run the event and carry the house to 2am.

**From 4pm to 7pm** it sells what the battery can spare, at the full rate your export limit allows. It keeps back your reserve, what the house will need until the cheap hours come round again, and anything promised to an event that is still to come.

**The rest of the time** it hands the inverter back to the ordinary plan, which runs the house from the battery and handles the solar as described in [Selling to the grid](selling.md).

On a day the ordinary plan has had to buy at the standard rate to get through the peak, the strategy sells nothing in the peak, because buying at the standard price and selling at the peak price gains nothing once the losses are paid.

## What comes first

The strategy steps aside, straight away, for anything more important: an Axle event, a Weekend Happy Hour, a storm warning, a power cut, an action you run by hand, or pausing the plugin. An event that is only announced keeps its energy set aside without the strategy giving up the inverter, and a 4pm to 7pm sale carries on until two minutes before an Axle event starts. A Saving Session needs neither: inside 4pm to 7pm the peak sale runs through it, and outside it the battery simply runs the house (see [Saving Sessions](saving-sessions.md)). The Event Log says when it steps aside and when it takes the inverter back.

## Switching it on

The strategy does nothing at all until three boxes are ticked in **Plugins → Sigenergy Manager → Configure**, under **OCTOPUS FLUX STRATEGY**:

1. **Use the Flux strategy** — that you want it.
2. **Commissioning is signed off** — that you have watched it do each of its jobs on your own inverter. The checks I made on mine are in the [Flux controller technical note](flux-controller.md).
3. **That import limit has been verified** — that someone has checked what your supply can safely carry, and put it in **Most the site can safely import (kW)**. An overnight charge at full power, on top of everything else in the house, draws hard on the supply, and the inverter's own rating says nothing about your main fuse and wiring. Until this box is ticked, the strategy will not plan a grid charge.

Even then, it only runs when your Octopus account shows it is on Flux for both buying and selling. It checks this at least every six hours, and the **Plan as though the tariff were** setting never counts as proof.

With the import limit verified, the plugin also sets it on the inverter as a limit on the whole house's draw from the grid, so an oven or a car charger switching on during the charge takes power from the battery's charge, not from your supply.

## Its settings

| Setting | What it does |
|---|---|
| **Keep this much battery back at all times (%)** | A floor no sale may go below, whatever the price. 20% to start with. What the house needs before the cheap hours come back is worked out separately and added on top. Storm, power-cut and overnight-room floors can raise it, never lower it. |
| **Battery wear to charge each traded kWh (pence)** | A charge for battery wear, added to the cost of every unit bought to sell, so a sale that does not cover it is not made. 5p to start with. The losses of charging and discharging are counted separately, from **Round-trip efficiency**. |
| **Most the site can safely import (kW)** | The most your supply can carry, for the overnight charge. 10 kW to start with. |

## If Indigo loses touch with the inverter

The inverter has no timer of its own that stops a sale or a charge when the plugin goes quiet, so the plugin sets its stopping points on the inverter before it starts, not only in its own memory:

- **During a sale**, the battery stops at the level the plugin set, and if a power cut follows, the house can still use the whole battery.
- **During a charge**, the battery stops at the level the plugin set, and the house never draws more from the grid than your import limit. What a stuck charge costs is money, not safety: energy bought at the standard price after 5am, and the solar the inverter does not collect while it is charging from the grid.
