---
title: Axle events
parent: How it works
nav_order: 7
---

# Axle events

[Axle](https://axle.energy) pay households with a battery to sell to the grid at set times, which they call events, and they settle what you earn from your meter readings. If you have signed up with Axle, the plugin runs each event for you and keeps a record of what it paid.

To switch it on, tick **Enable Axle VPP monitoring** in the plugin's settings and fill in **Axle API token**, which Axle give you, or keep it in the shared secrets file as the [Settings](settings.md) page explains. Add an **Axle VPP Monitor** device to see what is going on.

## Before an event

The plugin checks Axle for new events every ten minutes, and every minute when one is near.

- **When an event is announced,** usually a day ahead, the plugin sets its energy aside, so the overnight plan does not use it, and an event in the morning always has what it needs. The **VPP Event Announced** trigger runs.
- **Half an hour before,** it sets the lowest level the battery may go to during the event — the reserve the house needs afterwards — so the event only sells what the battery can spare. For an event in the day that is the battery's safety floor, because the sun will refill it. For an event at night it is the overnight reserve. If the 4pm to 7pm sale on Octopus Flux is running then, it carries on, still keeping the event's energy back, and the lowest level is set two minutes before the start instead, when the event takes over.
- **If the battery cannot cover the event,** the plugin buys just enough from the grid to run the whole event and still carry the house to 2am. It works this out from the forecast and the house's usual pattern, and buys as late as it can, so a sunny afternoon has every chance to make it unnecessary. For an event between 4pm and 7pm it buys before 4pm, at the standard price rather than the peak price. An event announced before 2am is covered by the overnight charge instead. The Event Log says when a top-up is planned and when it starts.
- If the battery still looks short for the event, you get a Pushover message saying so.

## During an event

From two minutes before the start to two minutes after the end, the plugin sells from the battery itself, at the full rate your export limit allows. The **Battery Manager** shows **VPP Export Active**, and the **VPP Event Started** trigger runs. Because Axle settle on your meter readings, a sale the plugin runs counts in exactly the same way as one Axle would start, and the same units earn your ordinary Octopus export price as well.

The plugin keeps time by the event's own start and end, so a short break in Axle's service during an event does not cut it short. An event Axle cancel before it starts is stood down.

An event comes before everything else the plugin does, except a storm warning or the hold-off after a power cut, which stop it selling.

## After an event

When the event ends, the plugin hands the battery back to run the house as normal, and the **VPP Event Ended** trigger runs. It sends a Pushover message with how much was sold, and the **Axle VPP Monitor** shows a summary of the event.

## What Axle paid

Axle pay a few days after each event. The plugin can collect what they paid in three ways, which you can use together:

- **From Axle's settlement emails in Apple Mail.** Tick **Read Axle's settlement emails from Apple Mail on this Mac** and the plugin reads them straight from Mail's own files, every six hours. It only opens messages from Axle with their settlement subject, it never signs in to anything, and no password is stored. This needs Apple Mail on the Mac that runs Indigo, collecting the mailbox Axle write to.
- **From your Axle account page.** Every email Axle send carries a sign-in link that works for seven days. Tick **Read your Axle account page directly** and the plugin uses the latest link from Apple Mail to read your account, the same way your browser would, so earnings arrive even when Axle send no settlement email. Nothing is stored, and the link runs out on its own. The account page also says which events Axle have settled, so the plugin can tell you money is owed even when it cannot read the amount.
- **From an email read by the Email+ plugin.** The **Import Axle Settlement Email** action reads the newest message on an Email+ mail account and files the figures. Fire it from an Email+ trigger on Axle's results email.

The **Axle VPP Monitor** shows what you have earned in total, this month, what is waiting to be drawn, and how many events are paid and still to be paid. **Plugins → Sigenergy Manager → Show VPP Earnings Ledger** lists the events, with the energy Axle counted beside the energy the plugin measured.

**Warn if the earnings figures are this many days stale** — seven days to start with — makes **Ledger Feed Health** warn you when no new figures have come in for that long, which catches a feed that has stopped. Set it to 0 to switch the warning off.

**Axle VPP payment rate** — £1.00 a kWh to start with — is only used for the estimate of what an event will earn, shown as **Estimated Earnings**.
