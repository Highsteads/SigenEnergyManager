#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    happy_hour_booking.py
# Description: Decides which Octopus Weekend Happy Hour slots to book, and says so
#              in plain English. Pure: slots, tokens, a solar forecast, the house
#              profile and the battery in; a booking plan and its messages out.
# Author:      CliveS & Claude Opus 5.5
# Date:        22-09-2026
# Version:     1.0
#
# SigenEnergyManager 5.112.0. The rules, agreed with CliveS on 22-Sep-2026:
#
#   * Two free hours on any Sunday the battery can actually use them. That is
#     judged by SIMULATING the day with the free hours in it, using the Flux
#     planner's own walk: the free energy the battery would take, less any
#     sunshine the free energy would push out to the grid. On a bright day the
#     roof fills the battery anyway, and a free hour then mostly exports the
#     day's own solar at the day rate, which is why the tokens are held back.
#   * Every token spent by the last Sunday before the offer ends. A token held
#     past that is simply lost, so once the Sundays left cannot absorb the hours
#     held, the surplus is booked regardless of the weather.
#   * Nothing is ever cancelled. The API offers a cancellation, but whether it
#     hands the tokens back is not documented and has not been measured. So a
#     booking is only made when it is meant, and a later forecast that turns
#     brighter is lived with rather than undone.
#
# Octopus's own rules, read 22-Sep-2026 (octopus.energy/saving-sessions/
# weekend-happy-hours/): slots are announced on Thursdays, generally Sunday
# 11am to 3pm in one-hour slots; places per slot are limited; you may book up
# to five minutes before a slot starts; up to two in one day; everything the
# house imports in the hour is free up to 16 kWh; the offer ends 1 November.

from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from itertools import combinations
from typing import Optional, Tuple

import flux_strategy as fs


MAX_HOURS_PER_DAY       = 2                      # "You can use up to two in one day"
MIN_USEFUL_KWH_PER_HOUR = 5.0                    # about half of what an hour can bring in
BOOKING_LEAD            = timedelta(minutes=5)   # "up to 5 minutes before ... starts"
FAIR_USE_KWH_PER_HOUR   = 16.0                   # "free up to 16kWh" an hour

# Outcomes. Only BOOK changes anything; the rest say why nothing was booked.
BOOK             = "book"
HOLD_BRIGHT      = "hold_bright"        # the battery could not use enough of it
HOLD_NO_FORECAST = "hold_no_forecast"   # the forecast does not reach the day yet
NO_TOKENS        = "no_tokens"          # not enough for an hour, or not reported
DAY_FULL         = "day_full"           # two hours already booked for the day
NOTHING_BOOKABLE = "nothing_bookable"   # every slot full, refused or too close
UNKNOWN_COST     = "unknown_cost"       # the token price of an hour is not set

_NUMBER_WORDS = ("no", "one", "two", "three", "four", "five", "six", "seven",
                 "eight", "nine", "ten", "eleven", "twelve")


@dataclass(frozen=True)
class Slot:
    """One Happy Hour slot as Octopus announced it. Times are aware (UTC)."""
    event_id: str
    code:     str
    start:    datetime
    end:      datetime
    capacity: Optional[str] = None
    booked:   bool          = False

    @property
    def hours(self):
        return max(0.0, (self.end - self.start).total_seconds() / 3600.0)


@dataclass(frozen=True)
class BookingPlan:
    day:             date
    outcome:         str
    book:            Tuple[Slot, ...]  = ()
    booked:          Tuple[Slot, ...]  = ()
    forced:          bool              = False
    useful_kwh:      Optional[float]   = None   # what the battery could use of booked + book
    best_single_kwh: Optional[float]   = None   # what the best single new hour was worth
    hours_held:      int               = 0
    tokens:          Optional[int]     = None
    tokens_per_hour: int               = 2
    sundays_after:   int               = 0
    pv_day_kwh:      Optional[float]   = None


# ================================================================
# The calendar
# ================================================================

def sundays_after(day, scheme_end):
    """Sundays strictly after `day` and strictly before `scheme_end`.

    Only SUNDAYS count as later chances to spend a token. Octopus say the slots
    are "generally on a Sunday" and every one announced so far has been, and the
    two readings fail in opposite directions: count a Saturday that never gets
    slots and tokens are lost at the end; leave one out and the worst case is
    spending an hour a week early. `scheme_end` is exclusive — 1 November 2026
    is itself a Sunday, and "use them by 1st November" does not settle it.
    """
    n = 0
    d = day + timedelta(days=1)
    while d < scheme_end:
        if d.weekday() == 6:
            n += 1
        d += timedelta(days=1)
    return n


def day_window(day, tz):
    """The stretch of `day` a free hour affects: 5am, when the overnight cheap
    charge ends, to 2am the next night, when the next one begins. UTC."""
    return (fs.local_wall(tz, day, time(5, 0)),
            fs.local_wall(tz, day + timedelta(days=1), time(2, 0)))


# ================================================================
# The value of a set of free hours
# ================================================================

def _free_commitments(slots, per_hour_kwh):
    return tuple(fs.EventCommitment(source="octopus", kind="import",
                                    start=s.start, end=s.end,
                                    energy_kwh=per_hour_kwh * s.hours,
                                    event_id=str(s.event_id))
                 for s in slots)


def useful_free_kwh(site, house, pv, slots, day, tz, per_hour_kwh,
                    start_kwh=None, now=None):
    """Free grid kWh the battery would really use, if `slots` were booked.

    Walks the day twice from the same starting charge, with and without the free
    hours, and returns the free energy taken LESS any extra sunshine it pushed
    out. That last part is the point: on a day the roof fills the battery by
    itself, a free hour mostly exports the day's own solar, and it should not
    look valuable just because the grid would happily send 10 kWh.

    `start_kwh` is the battery at the start of the walk. Left out, it is what the
    Flux planner itself would arrange: the least charge that keeps the house
    above the reserve until the free hour — which is what the overnight charge
    aims at now that it leaves room for booked hours. `now`, when later than
    5am on the day, starts the walk there instead (a decision made on the day).
    """
    a, b = day_window(day, tz)
    if now is not None and now > a:
        a = now
    if b <= a:
        return 0.0
    booked  = fs.SimInputs(site=site, house=house, pv=pv,
                           commitments=_free_commitments(slots, per_hour_kwh))
    without = fs.SimInputs(site=site, house=house, pv=pv, commitments=())
    if start_kwh is None:
        floor = site.capacity_kwh * fs.reserve_floor_pct(site) / 100.0
        start_kwh, _infeasible = fs.required_start_kwh(booked, a, b, floor)
    with_r    = fs.walk(site, fs.build_steps(booked, a, b), start_kwh)
    without_r = fs.walk(site, fs.build_steps(without, a, b), start_kwh)
    pushed_out = max(0.0, with_r.spill_kwh - without_r.spill_kwh)
    return max(0.0, with_r.free_in_kwh - pushed_out)


# ================================================================
# The decision
# ================================================================

def plan_day(*, day, slots, tokens, tokens_per_hour, now, tz, site, house, pv,
             per_hour_kwh, scheme_end, refused=(), start_kwh=None,
             min_useful_kwh=MIN_USEFUL_KWH_PER_HOUR):
    """What to book on one day's slots. Pure; nothing here touches the account.

    Order of thought:
      1. room — two hours a day at most, less any already booked;
      2. tokens — whole hours only, and none at all if Octopus did not say;
      3. which slots can still be booked — not full, not refused before, and
         more than five minutes from starting;
      4. how many hours MUST go today, because the Sundays left after it cannot
         absorb the rest before the offer ends;
      5. with a forecast: the best one and the best two slots by simulated
         useful energy, adding an hour only while it brings at least
         `min_useful_kwh`, then topped up to what must go today;
      6. without a forecast: only what must go today, latest slots first.
    """
    booked = tuple(sorted((s for s in slots if s.booked), key=lambda s: s.start))
    after  = sundays_after(day, scheme_end)
    plan   = BookingPlan(day=day, outcome="", booked=booked, tokens=tokens,
                         tokens_per_hour=int(tokens_per_hour or 0), sundays_after=after)

    room = MAX_HOURS_PER_DAY - len(booked)
    if room <= 0:
        return replace(plan, outcome=DAY_FULL)
    if not tokens_per_hour or int(tokens_per_hour) <= 0:
        return replace(plan, outcome=UNKNOWN_COST)
    if tokens is None:
        return replace(plan, outcome=NO_TOKENS)
    held = max(0, int(tokens) // int(tokens_per_hour))
    plan = replace(plan, hours_held=held)
    if held <= 0:
        return replace(plan, outcome=NO_TOKENS)

    refused    = {str(c) for c in (refused or ())}
    candidates = [s for s in slots
                  if not s.booked
                  and str(s.capacity or "").upper() != "FULL"
                  and str(s.code) not in refused
                  and s.start - BOOKING_LEAD > now]
    if not candidates:
        return replace(plan, outcome=NOTHING_BOOKABLE)

    n_max   = min(room, held, len(candidates))
    must    = max(0, held - MAX_HOURS_PER_DAY * after)
    n_force = min(must, n_max)

    pv_day = None
    a, _b = day_window(day, tz)
    midnight = fs.local_wall(tz, day + timedelta(days=1), time(0, 0))
    walk_from = max(a, now) if now is not None else a
    # Coverage is asked of the DAY, not of the whole walk. The walk runs on to
    # 2am for the house's sake, but the hours after midnight are dark, and the
    # furthest day a six-day forecast reaches has no buckets after its own
    # midnight — asking for them would refuse every last-day decision.
    covered = (pv is not None and house is not None and site is not None
               and walk_from < midnight and pv.covers(walk_from, midnight))
    if pv is not None:
        pv_day = pv.kwh_between(fs.local_wall(tz, day, time(0, 0)), midnight)
    plan = replace(plan, pv_day_kwh=(round(pv_day, 1) if covered else None))

    if not covered:
        if n_force > 0:
            pick = sorted(candidates, key=lambda s: s.start, reverse=True)[:n_force]
            return replace(plan, outcome=BOOK, forced=True,
                           book=tuple(sorted(pick, key=lambda s: s.start)))
        return replace(plan, outcome=HOLD_NO_FORECAST)

    def value(extra):
        return round(useful_free_kwh(site, house, pv, booked + tuple(extra), day, tz,
                                     per_hour_kwh, start_kwh=start_kwh, now=now), 2)

    best = {0: ((), value(()) if booked else 0.0)}
    for n in range(1, n_max + 1):
        # Ties go to the LATER slots: the battery has had longer to run down, and
        # the popular 11am slot (Octopus point IO Flux customers at it) is left
        # for others.
        options = sorted(((value(combo), sum(s.start.timestamp() for s in combo), combo)
                          for combo in combinations(candidates, n)),
                         key=lambda o: (o[0], o[1]), reverse=True)
        best[n] = (options[0][2], options[0][0])

    chosen = 0
    for n in range(1, n_max + 1):
        if best[n][1] - best[n - 1][1] >= min_useful_kwh - 1e-9:
            chosen = n
        else:
            break
    n = max(chosen, n_force)
    if n == 0:
        return replace(plan, outcome=HOLD_BRIGHT, useful_kwh=best[0][1],
                       best_single_kwh=round(best[1][1] - best[0][1], 1))
    return replace(plan, outcome=BOOK, forced=n > chosen,
                   book=tuple(sorted(best[n][0], key=lambda s: s.start)),
                   useful_kwh=best[n][1],
                   best_single_kwh=round(best[1][1] - best[0][1], 1))


# ================================================================
# Plain English — ASCII only, sentences, every number with its meaning
# ================================================================

def number_words(n):
    """Small counts as a person says them: 'four tokens', not '4 tokens'."""
    n = int(n)
    return _NUMBER_WORDS[n] if 0 <= n < len(_NUMBER_WORDS) else str(n)


def _plural(n, word):
    return f"{number_words(n)} {word}{'' if int(n) == 1 else 's'}"


def _cap(text):
    """Capital first letter only. str.capitalize() lowers the rest, which turned
    'Sundays' into 'sundays' in the first draft of these messages."""
    return text[:1].upper() + text[1:]


def clock_words(dt_local):
    """'1pm', '11am', 'midday', '1:30pm' — never '13:00'."""
    h, m = dt_local.hour, dt_local.minute
    if m == 0 and h == 0:
        return "midnight"
    if m == 0 and h == 12:
        return "midday"
    suffix = "am" if h < 12 else "pm"
    h12 = h % 12 or 12
    return f"{h12}{suffix}" if m == 0 else f"{h12}:{m:02d}{suffix}"


def join_words(items):
    items = [str(i) for i in items]
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + " and " + items[-1]


def span_words(slots, tz):
    """'from 1pm to 3pm', merging slots that follow on; separate spans joined."""
    spans = []
    for s in sorted(slots, key=lambda s: s.start):
        if spans and spans[-1][1] == s.start:
            spans[-1] = (spans[-1][0], s.end)
        else:
            spans.append((s.start, s.end))
    return join_words(f"from {clock_words(a.astimezone(tz))} to {clock_words(b.astimezone(tz))}"
                      for a, b in spans)


def day_words(day, today):
    if day == today:
        return "today"
    if day == today + timedelta(days=1):
        return "tomorrow"
    return f"{day:%A} {day.day} {day:%B}"


def _end_words(scheme_end):
    return f"{scheme_end.day} {scheme_end:%B}"


def _kwh(x):
    return f"{x:.0f} kWh"


def booked_message(plan, tz, today, scheme_end):
    """(title, body) for slots the plugin has just booked."""
    slots   = tuple(plan.booked) + tuple(plan.book)
    hours   = len(plan.book)
    when    = day_words(plan.day, today)
    title   = (f"Free electricity booked for {when}" if when in ("today", "tomorrow")
               else f"Free electricity booked for {plan.day:%A}")
    parts = [f"{_cap(_plural(hours, 'free hour'))} "
             f"{'is' if hours == 1 else 'are'} booked for {when}, "
             f"{span_words(slots, tz)}."]
    parts.append("The battery will fill itself from the grid for nothing then, and the "
                 "overnight top-up that morning will be smaller to leave room for it.")
    if plan.pv_day_kwh is not None and plan.useful_kwh is not None:
        parts.append(f"The forecast is about {_kwh(plan.pv_day_kwh)} of sun that day, so "
                     f"the battery should take about {_kwh(plan.useful_kwh)} of the "
                     f"free power.")
    if plan.forced:
        parts.append(f"It is booked now whatever the weather, because the tokens would "
                     f"otherwise run out of Sundays before the offer ends on "
                     f"{_end_words(scheme_end)}.")
    if plan.tokens is not None and plan.tokens_per_hour > 0:
        spent = hours * plan.tokens_per_hour
        left  = max(0, int(plan.tokens) - spent)
        hours_left = left // plan.tokens_per_hour
        tail = (f"which is {_plural(hours_left, 'more free hour')}, to use by "
                f"{_end_words(scheme_end)}" if hours_left else
                "not enough for another hour")
        parts.append(f"That spends {number_words(spent)} of your "
                     f"{_plural(plan.tokens, 'token')}, leaving {number_words(left)}, "
                     f"{tail}.")
    return title, " ".join(parts)


def hold_message(plan, tz, today, scheme_end):
    """(title, body) when bookings are open and the plugin is not booking."""
    when = day_words(plan.day, today)
    counts = (f"{_plural(plan.tokens, 'token')}, enough for "
              f"{_plural(plan.hours_held, 'free hour')},")
    later = (f"{_cap(_plural(plan.sundays_after, 'more Sunday'))} "
             f"{'is' if plan.sundays_after == 1 else 'are'} left before the offer ends "
             f"on {_end_words(scheme_end)}.")
    if plan.outcome == HOLD_NO_FORECAST:
        title = f"Waiting for the forecast before booking {plan.day:%A}"
        body = (f"Octopus have opened bookings for {when}, but the solar forecast does "
                f"not reach that far yet. The plugin will decide once it does, looking "
                f"every hour until the slots start. You have {counts} to use by "
                f"{_end_words(scheme_end)}.")
        return title, body
    title = "Keeping your free hours for a duller Sunday"
    parts = [f"Octopus have opened bookings for {when}."]
    if plan.pv_day_kwh is not None:
        parts.append(f"The forecast is about {_kwh(plan.pv_day_kwh)} of sun that day, "
                     f"enough to fill the battery by itself, so a free hour would "
                     f"mostly push the sunshine out to the grid.")
    if plan.best_single_kwh is not None:
        if plan.best_single_kwh < 1.0:
            parts.append("The battery would have almost no room for it.")
        else:
            parts.append(f"The battery could use only about "
                         f"{_kwh(plan.best_single_kwh)} of the best slot.")
    parts.append(f"The plugin is keeping your {counts} for a duller day, and will look "
                 f"again every hour until the slots start.")
    parts.append(later)
    return title, " ".join(parts)


def reminder_message(slots, tz):
    """(title, body) for the morning of a booked day."""
    span = span_words(slots, tz)
    title = f"Free electricity {span} today"
    body = (f"Everything the house uses {span} today is free, up to 16 kWh an hour, "
            f"and the battery will fill itself from the grid then. It is a good time "
            f"to run the washing machine, the tumble dryer and the dishwasher.")
    return title, body


def result_message(free_kwh, slots, tz, cheap_rate_p=None):
    """(title, body) once the day's last booked free hour has ended."""
    span = span_words(slots, tz) if slots else ""
    if free_kwh is None:
        return ("The free hours are over",
                f"The free electricity {span} is over. The plugin could not measure "
                f"how much the house took, so check the Octopus app for the credit.".replace("  ", " "))
    if free_kwh < 0.5:
        return ("The free hours banked almost nothing",
                f"The free electricity {span} is over, but the house took only "
                f"{free_kwh:.1f} kWh from the grid in that time.".replace("  ", " "))
    body = (f"The free electricity {span} is over. The house took {_kwh(free_kwh)} "
            f"from the grid in that time, most of it into the battery, and Octopus "
            f"should credit all of it.")
    if cheap_rate_p:
        pounds = free_kwh * float(cheap_rate_p) / 100.0
        body += (f" Bought at the cheap overnight rate of {float(cheap_rate_p):.1f}p "
                 f"instead, that much would have cost about {pounds:.2f} pounds.")
    return f"The free hours banked {_kwh(free_kwh)}", body.replace("  ", " ")
