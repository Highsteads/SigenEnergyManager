---
title: Saving Sessions and Happy Hours
parent: How it works
nav_order: 6
---

# Saving Sessions and Happy Hours

Octopus Saving Sessions ask you to use less from the grid than usual for a set hour, which Octopus call a **Power Down**. Beat your usual use and you earn OctoPoints and, while the current offer runs, a token towards a **Weekend Happy Hour** — an hour of free electricity on a Sunday, which you book on the Octopus website or app. Two tokens book an hour.

Everything on this page is off to start with apart from the alerts. You switch each part on under **OCTOPUS SAVING SESSIONS** in the plugin's settings. You need to have joined the Saving Sessions scheme with Octopus yourself first.

## Alerts

When Octopus announce a session that is open to your region, the plugin sends a Pushover message saying when it is, and whether you are in it. **Plugins → Sigenergy Manager → Show Saving Sessions** lists the sessions still to come, whether you have joined each one, and how many Happy Hour tokens you hold.

## Joining Power Downs for you

Joining the scheme does not enter you into each session — every one is a separate opt-in, and a session you have not joined pays nothing. Tick **Opt in to Power Down sessions automatically** and the plugin joins each Power Down as soon as it is announced, so none is missed while you are asleep or away.

- It only ever joins a Power Down, never a Power Up, which asks you to use more.
- Joining cannot be undone — Octopus offer no way to withdraw. A session you then do not beat simply pays nothing, with no penalty.
- It does not join a session whose token you could never spend. It counts how many Happy Hours are left before the offer ends, and you can lower that with **Happy Hours you expect to use**. It also turns down a session too late in October for Octopus to score it in time to book a weekend.
- On Octopus Flux, every Power Down is joined. Joining costs nothing there, because the plugin never exports for a session on Flux (see below).
- After the offer ends, every Power Down is joined for its points.

## Selling during a Power Down

**On Octopus Flux the plugin never exports for a session, and sets no energy aside for one.** A session between 4pm and 7pm falls in the peak, when the battery is already selling whatever it can spare, so the session gets that sale. When there is not enough spare to sell for the whole three hours, as on a winter day, the plugin times the sale so the session gets its share: before the session it sells only what the session will not need, and the battery runs the house meanwhile. A session outside 4pm to 7pm is still joined, but the battery just runs the house through it: selling then would earn less than the energy is worth later, and running the house from the battery is itself the lower use the session asks for. The setting below does nothing on Flux.

On other tariffs, tick **Export the battery during a Saving Session** and, during a session you have joined, the plugin sells from the battery to beat your usual use. **Saving Session Export** shows on the **Battery Manager** while it runs.

- An Axle event always comes first, and energy already promised to Axle is kept back for it.
- It does not sell if that would leave the battery unable to reach the next morning without buying from the grid.
- A storm warning or the hold-off after a power cut stops it, as usual.

## Charging in a Weekend Happy Hour

Tick **Charge the battery during a Weekend Happy Hour** and, during an hour you have booked, the plugin charges the battery from the grid at full power, so the free electricity is banked rather than wasted. It fills to your **Daytime charge target**, or to 100% on Flux once nothing left of the day's sun could be lost. Once the battery reaches that level, the house carries on running on the free grid power until the hour ends, instead of on the battery.

The solar panels keep working through a free hour. The battery takes the sun first and the grid makes up the rest, so it fills just as fast. Once it is full the sun runs the house and anything spare is sold. (Until 5.125.0 the inverter was told to charge from the grid first, and it switched the panels off for the whole hour to do it.)

On Flux, the 2am to 5am charge leaves room for a booked hour, so the free energy replaces energy that would otherwise have been bought.

**Happy Hour Import** shows on the **Battery Manager** while it runs, and **Happy Hour free kWh** says afterwards how much went in.

## Booking Happy Hours for you

Octopus open the Sunday slots on a Thursday — usually four one-hour slots between 11am and 3pm — and each slot has limited places. Tick **Book Weekend Happy Hours automatically** and the plugin books them for you, checking every hour, and every ten minutes as a slot draws near. It needs the charging box above ticked too, since a free hour that does not charge the battery banks nothing.

- It books up to two hours on a Sunday the battery can really use them. It works this out by running through the day with the solar forecast, and counting the free energy the battery would take, less any sunshine that free energy would push out to the grid.
- It keeps your tokens on a day bright enough to fill the battery by itself.
- It spends every token by the last Sunday before the offer ends, whatever the weather, so none are lost.
- It never cancels a booking, because it is not known whether Octopus give the tokens back.
- **Tokens needed to book a Happy Hour** is 2 to start with. Octopus do not publish this in a way the plugin can read, so if they change it, change it here.

You get one message saying what it booked and why, a reminder on the morning of a booked Sunday — a good day to run the washing machine, tumble dryer and dishwasher — and a note afterwards of how much the house took free and what that would have cost. **Happy Hour Tokens** on the **Battery Manager** shows how many tokens you hold.

## Checking the free hour is paid

Octopus pay for a free hour by putting a credit on your account afterwards. Four times a day the plugin works out what each Sunday's free hours are owed — from Octopus's own meter reading once it has arrived, and the inverter's reading until then — up to the 16 kWh a free hour covers, at the price in force at the time. It then looks for the matching credit on your Octopus account.

You get one Pushover message when it is paid, one if it arrives more than 5p short, and one if nothing has come two weeks after the free hour.

The plugin also reads the result Octopus give each Power Down you joined — whether it was won, what your usual use is, what you used this time and the points paid — and your OctoPoints balance. My [Dashboards plugin](https://github.com/Highsteads/Dashboards) shows these, and the free-hour payments, on its Energy page.
