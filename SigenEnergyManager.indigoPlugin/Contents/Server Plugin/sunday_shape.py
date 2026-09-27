#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    sunday_shape.py
# Description: The half-hourly SHAPE of a Sunday's house load, measured from the
#              inverter's own half-hourly energy records. Pure: rows in, 48
#              fractions out. The plugin sets the level; this only says when in
#              the day a Sunday's energy goes.
# Author:      CliveS & Claude Opus 5.5
# Date:        27-09-2026 10:45 BST
# Version:     1.0 (SigenEnergyManager 5.118.0)
#
# Why a separate shape. The planning profile is one 48-slot curve averaged over
# every day of the week, and a Sunday is a different day: the roast, both
# microwaves and the week's wash put the load in the afternoon. Measured over the
# nine Sundays to 20-Sep-2026, 2pm-4pm ran 1.4-1.6 kWh an hour against about
# 0.9 on the blended curve, and the late morning ran lighter. The Sunday TOTAL was
# already right (the day uplift, x1.05), so only the timing was wrong — and on a
# day with a booked free Happy Hour, timing is the whole question.
#
# Why these records and not the plugin's own rolling window. The window
# (home_load_profile.json "days") only began on 13-Sep-2026, so it holds two
# Sundays. energy_timeseries.db has every half-hour since May, from the inverter's
# cumulative counters, which is also the measurement the 13-Sep audit trusted.
#
# Why a SHAPE and not a level. Nine to eighteen Sundays is thin for a level, and
# the level already has an owner (the measured Sunday uplift over 126 days). So
# the level stays where it is and the Sundays only move energy within the day.

from datetime import datetime, timedelta

SLOTS               = 48
SLOT_SECONDS        = 1800
MIN_DAY_COVERAGE    = 0.9     # a Sunday with a tenth of its half-hours missing is left out
SMOOTHING_KERNEL    = (0.25, 0.5, 0.25)


def _parse(value):
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _is_24_hour_day(day, tz):
    """False on a clock-change day, whose naive local half-hours double up or vanish."""
    if tz is None:
        return True
    start = datetime(day.year, day.month, day.day, 0, 0, tzinfo=tz)
    end   = datetime(day.year, day.month, day.day, 23, 59, tzinfo=tz)
    return start.utcoffset() == end.utcoffset()


def split_into_days(rows):
    """{date: (kwh[48], covered_seconds[48])} from half-hourly rows.

    Each row is (slot_start, slot_end, home_kwh) in naive LOCAL time. The plugin's
    record slots carry no fixed phase — a gap pushes them twenty minutes late for
    the rest of the day — so a row is shared between the local half-hours it
    overlaps, in proportion to the overlap. Filing a whole row under the slot it
    starts in would move energy up to half an hour early.
    """
    out = {}
    for start_s, end_s, kwh in rows:
        start, end = _parse(start_s), _parse(end_s)
        try:
            kwh = float(kwh)
        except (TypeError, ValueError):
            continue
        if start is None or end is None or end <= start or kwh < 0.0:
            continue
        length = (end - start).total_seconds()
        if length > 2 * SLOT_SECONDS:
            continue                       # a gap row: its energy has no known timing
        cursor = start
        while cursor < end:
            edge = cursor.replace(minute=0 if cursor.minute < 30 else 30,
                                  second=0, microsecond=0) + timedelta(minutes=30)
            piece_end = min(edge, end)
            seconds   = (piece_end - cursor).total_seconds()
            day       = cursor.date()
            idx       = cursor.hour * 2 + (1 if cursor.minute >= 30 else 0)
            energy, covered = out.setdefault(day, ([0.0] * SLOTS, [0.0] * SLOTS))
            energy[idx]  += kwh * seconds / length
            covered[idx] += seconds
            cursor = piece_end
    return out


def _smooth(values):
    """A 1-2-1 pass round the clock: one Sunday's roast lands at 1pm, the next at
    2:30, and the shape should say "early afternoon", not pick one of them."""
    n = len(values)
    a, b, c = SMOOTHING_KERNEL
    return [a * values[(i - 1) % n] + b * values[i] + c * values[(i + 1) % n]
            for i in range(n)]


def sunday_shape(rows, dates, tz=None, min_days=6):
    """(fractions[48] or None, sundays_used).

    `dates` is the set of Sundays the caller accepts — whole days, the house
    occupied, inside its window. Of those, a day is used only when the records
    cover MIN_DAY_COVERAGE of it and it is not a clock-change day. The shape is
    the mean of the used Sundays, smoothed, as fractions summing to 1. None when
    fewer than `min_days` Sundays qualify: the caller keeps the blended curve,
    which is what it had before.
    """
    by_day = split_into_days(rows)
    wanted = set(dates or ())
    total  = [0.0] * SLOTS
    used   = 0
    for day, (energy, covered) in by_day.items():
        if day not in wanted or day.weekday() != 6:
            continue
        if not _is_24_hour_day(day, tz):
            continue
        if sum(min(c, SLOT_SECONDS) for c in covered) < MIN_DAY_COVERAGE * SLOTS * SLOT_SECONDS:
            continue
        day_kwh = sum(energy)
        if day_kwh <= 0.0:
            continue
        # Each Sunday counts once, as a shape: a heavy Sunday must not outvote
        # a light one on WHEN the energy goes.
        for i in range(SLOTS):
            total[i] += energy[i] / day_kwh
        used += 1
    if used < max(1, int(min_days)):
        return None, used
    shape = _smooth([v / used for v in total])
    s = sum(shape)
    if s <= 0.0:
        return None, used
    return [v / s for v in shape], used


def scaled(shape, level_kwh):
    """48 kWh-per-half-hour figures: the shape carrying `level_kwh` over the day."""
    return [round(f * float(level_kwh), 4) for f in shape]
