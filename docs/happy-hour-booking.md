# Weekend Happy Hour booking (v5.112.0)

Status: **built 22-Sep-2026**, on CliveS's go-ahead the same evening. Extends the Happy Hour
import of v5.83.0 (`happy-hour-import-spec.md`), which charged the battery during a slot you had
booked yourself.

## What Octopus offer (read 22-Sep-2026)

From octopus.energy/saving-sessions/weekend-happy-hours/ and the "how to earn" help article:

- Beat your usual use in two Power Down sessions and you earn one Weekend Happy Hour. The API's
  `tokenBalance` counts one token per successful session; two buy an hour.
- Slots are announced on **Thursdays**, generally **Sunday 11am to 3pm in one-hour slots**. Every
  weekend since 16 August has been exactly that. Places per slot are **limited**, and a full slot
  greys out. You may book **up to five minutes before** a slot starts.
- Up to **two hours in one day**. Everything the house imports in the hour is free **up to 16 kWh**;
  the standing charge is paid as normal.
- The offer ends on **1 November 2026**, and unused hours are lost.
- Results of a Power Down take about three working days, so Monday to Wednesday sessions count
  towards that weekend and Thursday to Sunday ones towards the next.

## The three faults that made a booked hour worth almost nothing on Flux

1. **The overnight charge filled the battery the free hour needed empty.** `_flux_commitments()`
   has passed a booked hour to the planner as an `import` commitment since 5.109, but
   `build_steps` only ever read `export` ones. So the 02:00-05:00 charge bought its usual
   ~15 kWh for the 4pm export and a dull Sunday met the free hour three-quarters full. The walk
   now models a free window: the grid serves the house for nothing and fills the battery at the
   charge limit (sunshine first, sharing the one limit). `_charge_plan` then leaves the room
   without any change of its own, because its headroom already comes from the walk.
2. **A 2pm free hour could never start.** From 2pm the Flux controller holds the battery for the
   4pm peak. The manager never acts while Flux holds it, and the import flag that pre-empts Flux
   is only set by the manager acting. `_flux_other_owner()` now treats a live booked window as an
   owner in its own right.
3. **At the target the free hour handed back to self consumption**, so the house ran on the
   battery for the rest of the hour while the free grid sat idle. The import now runs to the end
   of the window: the hardware charge cutoff is set AT the target and stops the charging, the
   battery's discharge is pinned at zero (and `_verify_ems_registers` holds it there), and the
   end-of-method "import target reached" check leaves a free hour alone. The target is 100% once
   nothing left of today's sun could clip (the same `_flux_clip_risk_passed` test as CliveS's
   17-Sep-2026 Flux rule), otherwise the daily target.

## The booking rule (`happy_hour_booking.py`, pure)

On every Saving Sessions poll (hourly; ten-minutely near a slot), for each Sunday with slots
announced and not yet started:

1. **Room**: at most two hours a day, less any already booked.
2. **Tokens**: whole hours only; nothing if Octopus did not report a balance.
3. **Candidates**: not full, not refused before, more than five minutes from starting.
4. **Must spend**: hours held beyond two for each Sunday still left after this one (Sundays only,
   and 1 November itself excluded) are booked now whatever the weather.
5. **Worth it**: the day is simulated twice with the planner's own walk, from the charge the
   planner would arrange, with and without the candidate hours. Useful energy is the free energy
   the battery takes **less any sunshine it pushes out**. An hour is added only while it brings at
   least 5 kWh (about half of what an hour can). Ties go to later slots.
6. **No forecast** for the day: only the must-spend hours, latest slots first.

With the plugin's flat-sun test fixture and a 23 kWh house, that books two hours on a Sunday of
up to about 22 kWh of sun, one hour at 25-30 kWh, and none from about 33 kWh.

Nothing is ever cancelled. The API has `cancelSavingSessionsWeekendHappyHourBooking`, but whether
a cancel returns the tokens is not documented and has not been measured, so the rule only books
what it means to keep.

**The plugin trusts its own bookings (v5.112.1).** Every slot whose booking reply carried the
matching `bookedEvent` is kept in `happy_hour_booked_codes` and marked booked on every poll, so a
feed that lags the booking cannot drop the hour from the plan or invite a second booking.

## Messages (plain English, deduped on structured keys)

- **Booked** — once per booking: which hours, why, and the tokens left.
- **Holding** — once per Sunday and reason: bookings are open, the day is bright enough to fill
  the battery, the tokens are kept, and how many Sundays remain.
- **Morning** — from 8am on a booked day: the free hours and a nudge to run the washing machine,
  tumble dryer and dishwasher. Skipped if the booking itself was made that day.
- **Result** — after the day's last booked hour: the kWh the house took free and what that much
  would have cost at the cheap overnight rate.

The four per-slot "Happy Hour available" pushes are not sent while automatic booking is on.

## Also in 5.112.0

- **A Power Down wholly inside the 4pm-7pm Flux peak is always joined.** The controller exports
  at the 4 kW limit through the peak anyway (meter data 17-20 Sep-2026: 2.00 kWh every half hour
  on days with no session), so joining costs nothing. The token verdict still decides sessions
  outside the peak. Without this the auto-join would have declined every session from 27 October
  and, after 1 November, for good.
- **The Open-Meteo fetch covers six days**, and keeps the days after tomorrow as
  `_hourly_p50_ahead` / `aheadDayKwh`, so a Thursday decision can see the Sunday.

## Known limits, stated rather than hidden

- A booking decided before the day starts simulates from the charge the planner WOULD arrange.
  After a bright Saturday the battery can start the Sunday fuller than that, so the value is
  somewhat optimistic until the day itself, when the real battery is used.
- On Flux the 4pm-7pm export at the cap is filling the baseline that 6pm-7pm Power Downs are
  judged against. As more ordinary Flux weekdays enter it, those sessions will be harder to win.
  Never hold exports back to keep the baseline beatable — that is gaming the scheme.
- The whole prize is modest: a free hour replaces about 10 kWh bought at the Flux cheap rate
  (14.62p on 22-Sep-2026), so about GBP 1.50 an hour.
