---
title: Power cuts and storms
parent: How it works
nav_order: 5
---

# Power cuts and storms

## When the power goes

The inverter tells the plugin when the grid goes and the house is running on the battery, and again when the mains comes back. The plugin sends a message each time, by Pushover and by email, if you have set them up:

- **When the power goes** — the time, the battery level and how much it holds, what the house is drawing, and about how long the battery would carry it at that rate.
- **When the power comes back** — the time, how long the cut lasted, the battery level, and what the plugin is doing about selling, below.

The Pushover message goes at normal priority, so it waits for the end of your quiet hours. A long power cut may take your broadband down with it, in which case the messages arrive once it is back. Untick **Notify on grid lost / restored** to stop them.

The **Grid Online** state on the **Sigenergy Inverter** device is 0 during a power cut and 1 otherwise, which makes it easy to use in your own triggers. **Plugins → Sigenergy Manager → Show Power Cut Log** lists the last 20 times the grid went and came back.

## After the power comes back

As a precaution, when the grid comes back the plugin holds off selling to the grid for up to four hours while the battery refills. It holds off only while the battery is below **Export lockout SOC floor**, 85% to start with. At or above that, selling carries on as normal, so a nearly full battery does not fill to the top and waste solar.

It also lets selling start again early when the rest of the day's solar forecast will refill the battery to that floor on its own, with plenty to spare — on a bright summer morning, holding back banks nothing the sun was not going to deliver anyway. That early release never applies while the battery is below **Lockout solar-refill minimum SOC**, 50% to start with.

While this runs, the **Battery Manager** shows **Power Cut Lockout Active** and the minutes left, Axle events and Saving Sessions do not sell, and there are triggers for when it starts and when it ends.

## Storm warnings

Every two hours the plugin checks the official weather warnings — the Met Office's own warnings for the UK, which it reads from the MeteoAlarm service. It only acts on a warning whose area covers your house, from the latitude and longitude in the settings, and only once the warning is due within the next 24 hours. It looks at warnings for wind, gales, storms, thunderstorms, snow, ice, rain and flooding.

While a yellow, amber or red warning is in force:

- **The overnight reserve rises to 50%,** and the plugin tops the battery up to it in the usual way for your tariff.
- **Selling is held off until the battery reaches** **Storm export release SOC**, 85% to start with, so the battery fills ahead of a possible power cut. Above that, selling carries on, so the battery does not fill to the top and waste solar.
- **The daytime charge aims at 100%,** so a battery filling from the sun goes all the way — but the plugin does not buy from the grid to get there.

You get a Pushover message when a warning reaches your area, naming the level. Amber and red warnings go at high priority, so they reach you even in your quiet hours. Another message says when the warning has passed and everything is back to normal.

If a check fails, the plugin keeps the last level it saw rather than treating the failure as all clear, for up to about a day, by which time any real warning has expired.

Put the name of your town in **Location name** in the settings to have it used in the yellow warning and the all-clear messages.
