---
title: Selling to the grid
parent: How it works
nav_order: 3
---

# Selling to the grid

The plugin only sells when **Enable grid export** is ticked in its settings. Your installer set a limit on how fast the house may sell to the grid, which Sigenergy's inverter holds to by itself — put the same figure in **DNO export limit**, so the plugin's sums match what the inverter can really do.

The plugin sells in four ways: spare solar in the day, a little from the battery overnight before a very sunny day, from the battery during an event you have signed up to, and, on Octopus Flux, at the 4pm to 7pm peak.

## Spare solar in the day

Left to itself, the inverter charges the battery first and sends solar to the grid only once the battery is full. On a bright day that fills the battery by late morning, and from then on anything the panels make beyond what the house uses and your export limit is simply lost, because it has nowhere to go.

So on a day when the solar still to come will more than fill the battery, the plugin slows the battery's charging, and the spare solar goes to the grid through the day instead. The **Battery Manager** shows **Solar Overflow Export** while this runs. It paces the charge to reach the **Daytime charge target**, 90% to start with, by the end of the day — or by 4pm on Octopus Flux, so the battery is ready for the peak.

- **The target is not a limit.** Once the house is selling at your export limit, extra solar has nowhere to go but the battery, so most bright days still end near full.
- **It stops as soon as the day can no longer fill the battery.** If the solar falls behind the forecast, selling stops and every spare unit goes into the battery.
- **It does not flick on and off.** Once selling stops, it waits at least ten minutes before it can start again, and it needs a clear surplus to start, not a borderline one.
- **On Flux, once nothing left of the day's sun could be lost,** the plugin stops slowing the charge altogether and lets the battery fill to 100%, because a unit kept in the battery saves buying one overnight, which is worth more than selling it in the day.

### Holding back on smaller days

Selling early in the day only helps on a day with so much sun that the battery would otherwise fill and solar would be lost. On a smaller day the spare solar is still there in the afternoon, and a unit sold in the morning and bought back in the evening costs you the difference in price.

So on any day forecast to make less than **Hold export on days forecast below** — 40 kWh to start with — the plugin does not sell spare solar until the battery reaches **Hold export until SOC reaches**, 95% to start with. It delays selling, it never reduces it: once the battery gets there, everything spare is sold as before. The **Battery Manager** shows **Banking First** while this is holding, and **Plugins → Sigenergy Manager → Show Bank-First Export Report** shows what it did over the last three weeks.

Set **Hold export on days forecast below** to 0 to switch the hold off. The figure of 40 kWh suits my house, where no day under about 41 kWh has ever lost any solar.

## Making room overnight before a very sunny day

If the battery is well charged at night and tomorrow looks very sunny, a full battery would fill by mid-morning and the rest of the day's spare solar would be lost. So the plugin sells some of the battery overnight, at your export limit, to make room.

It only does this when all of these hold:

- it is night, and **Enable grid export** is ticked,
- the battery is at 55% or more,
- tomorrow's solar forecast is at least three times what the house will use tomorrow, plus anything promised to an Axle event, so the sun is sure to refill it.

It stops at 40%, or at your overnight reserve if that is higher — a storm warning raises it — and it stops early if dawn comes, the forecast drops or selling is turned off. The **Battery Manager** shows **Night Export Active** and **Flood Prevention Active** while it runs, and there are triggers for when it starts and stops.

## Selling at the Flux peak

On Octopus Flux, the price paid for exports between 4pm and 7pm is the highest of the day. With the Flux strategy switched on, the plugin sells what the battery can spare at that time. [Octopus Flux](flux.md) explains how it decides how much.

## When the plugin does not sell

- **During a storm warning**, until the battery reaches 85% — see [Power cuts and storms](power-cuts-and-storms.md).
- **After a power cut**, for up to four hours while the battery refills — see the same page.
- **When the battery is needed for tomorrow.** The plugin never sells energy the house will need before the sun or the cheap hours come back.

## Trying out a different target

If you are wondering whether a different **Daytime charge target** would suit you better, tick **Log-only daytime-target comparison** and put the target you have in mind in **Compare the charge target against**. Nothing on the inverter changes. Each night the plugin records how much selling your real target held back compared with the other one, and what the evening's imports would have cost on Tracker and Agile. **Plugins → Sigenergy Manager → Show 90% vs 95% / Agile Shadow Comparison** shows the last three weeks.
