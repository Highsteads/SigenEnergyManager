---
title: The daily plan
parent: How it works
nav_order: 1
---

# The daily plan

## Reading the inverter

Every 10 seconds the plugin reads the inverter over your home network — the battery level, the solar, the grid, the house, and the day's totals — and brings the **Sigenergy Inverter** device up to date. You can make this slower in the settings.

## Deciding, once a minute

Once a minute the plugin works out what the battery should do. It asks two questions.

**Will the battery last until the sun comes back?** It starts from what is in the battery now, adds the solar still to come today, takes away what the house will use between now and dawn, and so arrives at how much will be left at dawn. The **Battery Manager** device shows this as **Projected SOC at Dawn**. Dawn here means the first hour the solar forecast expects the panels to make a useful amount.

**Will tomorrow need the grid?** It then adds tomorrow's solar forecast to what will be left at dawn, and sets that against what the house uses in a day. If the two come up short, the plugin plans to buy the difference from the grid, in the cheapest hours your tariff offers. If there is plenty, it can sell some.

It then goes through its choices in order, and takes the first that applies:

1. **An event you have signed up to** — an Axle event, an Octopus Saving Session or a Weekend Happy Hour.
2. **The overnight reserve** — keeping enough in the battery for a power cut.
3. **Making room overnight** before a very sunny day.
4. **Charging from the grid** for tomorrow.
5. **Selling spare solar** in the day.
6. **Otherwise, running the house from the battery as normal**, which is where it spends most of its time.

The pages that follow explain each one.

## What your house uses

The plugin learns what your house uses in each half hour of the day, from the inverter's own readings over the last nine weeks, so the pattern follows the seasons and a change in the way you live works through in a few weeks. It also works out a separate daily total for Monday, Tuesday to Friday, Saturday and Sunday, because those days are rarely alike.

Each day also gets its own pattern through the day, because the days rarely run alike: here a Saturday's extra use comes in the late morning, a Sunday's in the afternoon with the roast and the week's wash, and a Monday's a little in the early evening. Tuesday to Friday are too alike to tell apart, so they share one pattern measured from all four. The plugin measures each from the last 18 weeks, keeps the day's total as it was, and only moves the use to the time of day it really happens. Until six whole days of one kind have been recorded, that day uses the everyday pattern.

Until it has learnt enough, it uses the four daily figures in its settings — **Weekday**, **Monday**, **Saturday** and **Sunday daily consumption**. If you change one of them by more than 1 kWh from its starting value, the plugin takes that as you meaning it, and uses your figure for that day instead of its own.

### When the house is empty

An empty house uses a small, steady amount through the day, with none of the usual morning and evening rises. If you tick **Use a separate profile when the house is empty** and name the Indigo variable your holiday schedules set, the plugin keeps a second pattern for the days the house is empty, and plans from that while the variable says `true`. The two patterns never mix, so a long trip does not skew the everyday one. If the variable is missing or cannot be read, the plugin treats the house as occupied.

## The solar forecast

Every 30 minutes the plugin fetches a forecast from [Open-Meteo](https://open-meteo.com) for each of your solar arrays, from its angle, the direction it faces and its size, which you describe in the settings.

No forecast is exact, so each night the plugin compares the day's forecast with what the panels really made, and corrects future forecasts by how far out they have been at your house — separately for dull, middling and bright days, because a forecast is often wrong in different ways on each. The **Solar Forecast** device shows the forecast as it came, the corrected one, and the correction.

During the day it also watches how the solar is doing against the forecast so far, which the **Battery Manager** shows as **Solar vs Forecast Today**, and scales the rest of the day's forecast to match, within limits, so a dull morning or a bright one changes the plan for the afternoon.

## Keeping the inverter right

The plugin takes over the inverter only when it needs to — to charge, to sell or to hold — and hands it back to run the house as normal afterwards. Each minute it also reads back the limits it has set on the inverter, and puts right any that have changed.

If the plugin cannot reach the inverter for more than a few readings, the **Battery Manager** shows **Modbus offline — holding**, and it makes no changes until the readings come back, so it never acts on old figures.

## Pausing

You can pause the plugin with the **Pause Battery Manager** action, or by setting the **sigen_manager_paused** variable to `true`. It hands the inverter back to run the house as normal, and then leaves it alone until you resume it — it still reads the inverter and keeps the devices up to date. A pause survives a restart of the plugin or of Indigo.
