#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    day_shape.py
# Description: The half-hourly SHAPE of one weekday's house load, measured from
#              the inverter's own half-hourly energy records. Pure: rows in, 48
#              fractions out. The plugin sets the level; this only says when in
#              the day that weekday's energy goes.
# Author:      CliveS & Claude Opus 5.5
# Date:        27-09-2026 11:40 BST
# Version:     2.1 (SigenEnergyManager 5.120.0)
#
# History
#   1.0 (5.118.0) sunday_shape.py — Sundays only.
#   2.1 (5.120.0) Monday shaped too (plugin.SHAPED_WEEKDAYS); no change here.
#   2.0 (5.119.0) any weekday (Saturday added); the midnight row of the old
#       recorder read at its true start; a zero row read as missing, not as
#       nothing used; each half-hour averaged over the time actually recorded.
#
# Why a separate shape. The planning profile is one 48-slot curve averaged over
# every day of the week. Measured to 27-Sep-2026, a Sunday puts its load in the
# afternoon (the roast, both microwaves, the wash: 2pm-4pm 1.26 / 1.45 kWh an
# hour against 0.90 on the blend) and a Saturday in the late morning (11am-noon
# 2.4 kWh against 1.2). The day TOTALS were already right (the day uplifts), so
# only the timing was wrong — and on a day with a free Happy Hour or a Flux
# charge to plan, timing is the question.
#
# Why these records and not the plugin's own rolling window. The window
# (home_load_profile.json "days") only began on 13-Sep-2026. energy_timeseries.db
# has every half-hour since May, from the inverter's cumulative counters.
#
# Why a SHAPE and not a level. Eighteen of one weekday is thin for a level, and
# the level already has an owner (the measured day uplift over 126 days).

from datetime import datetime, timedelta

SLOTS               = 48
SLOT_SECONDS        = 1800
MIN_DAY_COVERAGE    = 0.9     # a day with a tenth of its time unrecorded is left out
MIDNIGHT_ROW_GRACE  = timedelta(minutes=5)
MIDNIGHT_ROW_REACH  = timedelta(minutes=60)
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


def _true_spans(rows):
    """(start, end, kwh) with each row's REAL start.

    Before 5.119.0 the recorder labelled every row as the thirty minutes before
    it was written, but its energy is everything since the previous write. At
    midnight the plugin reset its timer without writing, so the first row of each
    day held about 55 minutes of energy (23:35 to 00:30) under a 00:00-00:30
    label — which put the last half-hour of every day into the first of the next.
    A row labelled as starting within five minutes after midnight, whose
    predecessor ended less than an hour before that, really began where the
    predecessor ended. Rows written since 5.119.0 carry their true start and a
    midnight row of their own, so the rule never fires on them.
    Anything else keeps its label: after a restart the recorder re-seeds, and the
    label is then right.

    A row of exactly 0.0 kWh is dropped AFTER it has placed its end: a house
    never uses nothing for half an hour, and before 5.89.0 the midnight row was
    always written as zero (a daily counter's delta, clamped), so its energy was
    lost rather than small. Read as a measurement it taught the shape a quiet
    midnight that never happened; dropped, the slot is simply unrecorded.
    """
    parsed = []
    for start_s, end_s, kwh in rows:
        start, end = _parse(start_s), _parse(end_s)
        try:
            kwh = float(kwh)
        except (TypeError, ValueError):
            continue
        if start is None or end is None or end <= start or kwh < 0.0:
            continue
        parsed.append((start, end, kwh))
    parsed.sort(key=lambda r: r[1])
    out = []
    prev_end = None
    for start, end, kwh in parsed:
        midnight = start.replace(hour=0, minute=0, second=0, microsecond=0)
        if (prev_end is not None and prev_end < midnight <= start
                and start - midnight <= MIDNIGHT_ROW_GRACE
                and start - prev_end <= MIDNIGHT_ROW_REACH):
            start = prev_end
        if kwh > 0.0:
            out.append((start, end, kwh))
        prev_end = end
    return out


def split_into_days(rows):
    """{date: (kwh[48], covered_seconds[48])} from half-hourly rows.

    Each row is (slot_start, slot_end, home_kwh) in naive LOCAL time. The record
    slots carry no fixed phase — a gap pushes them twenty minutes late for the
    rest of the day — so a row is shared between the local half-hours it
    overlaps, in proportion to the overlap. Filing a whole row under the slot it
    starts in would move energy up to half an hour early.
    """
    out = {}
    for start, end, kwh in _true_spans(rows):
        length = (end - start).total_seconds()
        if length > 2 * SLOT_SECONDS + 1:
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


def day_shape(rows, dates, weekday, tz=None, min_days=6):
    """(fractions[48] or None, days_used) for one Python weekday (Mon=0 .. Sun=6).

    `dates` is the set of days the caller accepts — whole days, the house
    occupied, inside its window. Of those, a day is used only if it falls on
    `weekday`, is not a clock-change day, and its records cover MIN_DAY_COVERAGE
    of it.

    Each half-hour is read as a RATE over the time actually recorded, relative to
    that day's own mean rate, so:
      - a slot recorded for twenty minutes counts for what it measured, and a
        restart that lost ten minutes does not read as ten quiet minutes;
      - each day counts once, as a shape — a heavy day does not outvote a light
        one on WHEN the energy goes.
    The day's relative rates are averaged across days, weighted by the recorded
    time in each slot, then smoothed and returned as fractions summing to 1.
    None when fewer than `min_days` days qualify, or when any half-hour of the
    day was never recorded on any of them: the caller keeps the blended curve,
    which is what it had before.
    """
    by_day = split_into_days(rows)
    wanted = set(dates or ())
    weighted = [0.0] * SLOTS
    weights  = [0.0] * SLOTS
    used     = 0
    for day, (energy, covered) in by_day.items():
        if day not in wanted or day.weekday() != weekday:
            continue
        if not _is_24_hour_day(day, tz):
            continue
        capped = [min(c, SLOT_SECONDS) for c in covered]
        seconds = sum(capped)
        if seconds < MIN_DAY_COVERAGE * SLOTS * SLOT_SECONDS:
            continue
        mean_rate = sum(energy) / seconds
        if mean_rate <= 0.0:
            continue
        for i in range(SLOTS):
            if capped[i] > 0.0:
                relative = (energy[i] / capped[i]) / mean_rate
                weighted[i] += relative * capped[i]
                weights[i]  += capped[i]
        used += 1
    if used < max(1, int(min_days)) or min(weights) <= 0.0:
        return None, used
    shape = _smooth([w / c for w, c in zip(weighted, weights)])
    s = sum(shape)
    if s <= 0.0:
        return None, used
    return [v / s for v in shape], used


def scaled(shape, level_kwh):
    """48 kWh-per-half-hour figures: the shape carrying `level_kwh` over the day."""
    return [round(f * float(level_kwh), 4) for f in shape]
