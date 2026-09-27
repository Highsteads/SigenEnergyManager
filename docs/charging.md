---
title: Charging from the grid
parent: How it works
nav_order: 2
---

# Charging from the grid

The plugin buys from the grid for two reasons only: tomorrow will fall short without it, or the battery is below the reserve it keeps for a power cut. It finds out which tariff you are on from your Octopus account, and buys in the way that suits that tariff.

When it charges, it charges at the inverter's full rate, and it sets the inverter to stop a little above the level it is aiming for, so a charge cannot run on if the plugin or the network stops.

## The overnight reserve

The plugin keeps a reserve in the battery overnight so a power cut never finds it empty. You set it in the plugin's settings:

- **Summer resilience buffer** — April to September, 15% to start with, and never less than 15%.
- **Winter resilience buffer** — October to March, 20% to start with, for the longer nights and weaker sun. It only applies when it is the higher of the two.

A storm warning raises the reserve to 50% while it lasts — [Power cuts and storms](power-cuts-and-storms.md) explains this. When the battery drops below the reserve overnight, the plugin charges it back to 2% above, so it does not stop and start. How and when depends on your tariff, as below.

## On a flat-rate tariff — Tracker and Flexible

With the same price all day, there is nothing to gain by charging the battery for tomorrow. Charging and then running the house from the battery loses some energy on the way in and out, so when the battery runs low the plugin lets the house draw straight from the grid instead.

There are two exceptions:

- **The reserve** — if the battery drops below the reserve overnight, the plugin tops it up whatever the time.
- **Tracker's price for tomorrow** — when tomorrow will fall short and tomorrow's Tracker price is at least 10% cheaper than today's, the plugin charges the battery at five past midnight, at the cheaper price, because the saving is larger than the energy lost in charging.

## On a tariff with cheap hours — Go, Intelligent Go, Flux and Intelligent Flux

The plugin reads the cheap hours from the prices Octopus publish for your tariff. When tomorrow will fall short, it waits for the cheap hours and buys the shortfall then. **Import Scheduled** and **Import Scheduled Time** on the **Battery Manager** device show when.

If the battery runs out before the cheap hours arrive, the house simply draws from the grid at the ordinary rate in the meantime. The plugin does not buy tomorrow's energy early at that rate, because running out costs the same and wastes nothing.

The reserve is topped up in the cheap hours too, never at the day rate.

### Before a dear peak

Flux charges its dearest price between 4pm and 7pm. When the battery will not last through that peak, the plugin buys at the day rate, before 4pm, just enough to carry the house through it — but only when that works out cheaper than buying in the peak, once the loss of charging and a charge for battery wear are counted. The rest of tomorrow's need still waits for the cheap hours. On a day it has bought at the day rate, the plugin does not sell from the battery in the peak, because buying at the day rate and selling at the peak rate gains nothing once the losses are paid.

### With the Flux strategy switched on

On Octopus Flux you can hand the overnight charge to the Flux strategy, which sizes it from the whole of the next day, including what the battery can sell at the peak. While it is switched on and working, the ordinary plan leaves the 2am to 5am charge to it, and only steps in if the strategy cannot make a plan. [Octopus Flux](flux.md) explains it.

## On Agile

Agile's price changes every half hour. When tomorrow will fall short, the plugin looks at the prices between now and dawn, and starts the charge at the point where the whole run of half hours it needs is cheapest — not simply at the cheapest half hour, which is often a dip with dearer half hours either side. It only buys when that price, after the losses of charging, beats what tomorrow's daytime electricity would cost.

It buys the reserve the same way, in the cheapest run of half hours before dawn, so the battery still holds the reserve when the sun comes up.

## When there is no price to buy at

If the plugin does not recognise your tariff, or has no Agile prices that cover the charge, it buys nothing rather than buy at a price it cannot see. The house draws from the grid as it needs to, and the plugin says so with a warning in the Event Log and one Pushover a day, headed **Battery import held tonight**, or **Battery reserve top-up held** when it is the reserve it could not buy.

## Charging by hand

You can start a charge yourself with the **Force Grid Import** action, at the power and to the level you choose. See [Actions and triggers](actions-and-triggers.md).
