#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    test_happy_hour_booking.py
# Description: Contract tests for the Weekend Happy Hour booking plan and its
#              plain-English messages (SigenEnergyManager 5.112.0).
# Author:      CliveS & Claude Opus 5.5
# Date:        22-09-2026
# Version:     1.0

import unittest
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import flux_strategy as fs
import happy_hour_booking as hb

LONDON     = ZoneInfo("Europe/London")
SCHEME_END = date(2026, 11, 1)
THURSDAY   = datetime(2026, 9, 24, 14, 0, tzinfo=LONDON).astimezone(timezone.utc)
SUNDAY     = date(2026, 9, 27)


def _site(**over):
    base = dict(capacity_kwh=35.04, charge_power_w=10000, discharge_power_w=10000,
                export_limit_w=4000, import_limit_w=16000, import_limit_verified=True,
                efficiency=0.94, wear_p_per_kwh=2.0, reserve_pct=20.0)
    base.update(over)
    return fs.FluxSite(**base)


def _house(daily_kwh=23.0):
    return fs.HalfHourProfile([daily_kwh / 48.0] * 48, LONDON)


def _pv(day_kwh, day=SUNDAY, first=8, last=17, days=(SUNDAY,)):
    """A flat-topped day of sun, in the plugin's local-key bucket shape."""
    buckets = {}
    for d in days:
        for h in range(24):
            wh = day_kwh * 1000.0 / (last - first) if first <= h < last else 0.0
            buckets[f"{d:%Y-%m-%d} {h:02d}:00:00"] = wh
    return fs.HourlyPvForecast(buckets, LONDON)


def _slots(day=SUNDAY, hours=(11, 12, 13, 14), booked=(), full=(), prefix="E"):
    out = []
    for h in hours:
        start = fs.local_wall(LONDON, day, fs.time(h, 0))
        out.append(hb.Slot(event_id=str(1000 + h), code=f"{prefix}_{h}",
                           start=start, end=start + timedelta(hours=1),
                           capacity="FULL" if h in full else "AVAILABLE",
                           booked=h in booked))
    return out


def _plan(pv_kwh=10.0, tokens=7, now=THURSDAY, day=SUNDAY, slots=None, pv=None, **kw):
    return hb.plan_day(day=day, slots=slots if slots is not None else _slots(day),
                       tokens=tokens, tokens_per_hour=2, now=now, tz=LONDON,
                       site=_site(), house=_house(),
                       pv=pv if pv is not None else _pv(pv_kwh, day=day, days=(day,)),
                       per_hour_kwh=10.0, scheme_end=SCHEME_END, **kw)


def _local_hours(slots):
    return [s.start.astimezone(LONDON).hour for s in slots]


class TestTheCalendar(unittest.TestCase):

    def test_sundays_left_count_only_sundays_before_the_end(self):
        self.assertEqual(hb.sundays_after(date(2026, 9, 27), SCHEME_END), 4)   # 4, 11, 18, 25 Oct
        self.assertEqual(hb.sundays_after(date(2026, 10, 18), SCHEME_END), 1)
        self.assertEqual(hb.sundays_after(date(2026, 10, 25), SCHEME_END), 0)

    def test_the_last_saturday_is_not_a_later_chance(self):
        """31 October is a Saturday. No Saturday slot has ever been offered, so
        counting it would leave tokens to die unbooked."""
        self.assertEqual(hb.sundays_after(date(2026, 10, 25), SCHEME_END), 0)

    def test_the_first_of_november_itself_is_not_counted(self):
        self.assertEqual(hb.sundays_after(date(2026, 10, 30), SCHEME_END), 0)


class TestUsefulEnergy(unittest.TestCase):

    def test_a_dull_day_uses_nearly_all_of_two_hours(self):
        u = hb.useful_free_kwh(_site(), _house(), _pv(8.0), _slots(hours=(13, 14)),
                               SUNDAY, LONDON, 10.0)
        self.assertGreater(u, 17.0)

    def test_a_bright_day_uses_far_less_because_it_pushes_the_sun_out(self):
        dull   = hb.useful_free_kwh(_site(), _house(), _pv(8.0), _slots(hours=(13, 14)),
                                    SUNDAY, LONDON, 10.0)
        bright = hb.useful_free_kwh(_site(), _house(), _pv(38.0), _slots(hours=(13, 14)),
                                    SUNDAY, LONDON, 10.0)
        self.assertLess(bright, dull - 8.0)

    def test_pushed_out_sunshine_is_subtracted_not_ignored(self):
        """Mutation guard: without the subtraction a bright day reads as valuable."""
        slots = _slots(hours=(13, 14))
        a, b = hb.day_window(SUNDAY, LONDON)
        sim = fs.SimInputs(site=_site(), house=_house(), pv=_pv(38.0),
                           commitments=hb._free_commitments(slots, 10.0))
        floor = _site().capacity_kwh * 0.20
        start, _ = fs.required_start_kwh(sim, a, b, floor)
        raw = fs.walk(_site(), fs.build_steps(sim, a, b), start).free_in_kwh
        useful = hb.useful_free_kwh(_site(), _house(), _pv(38.0), slots, SUNDAY, LONDON, 10.0)
        self.assertLess(useful, raw)


class TestThePlan(unittest.TestCase):

    def test_a_dull_sunday_books_two_hours(self):
        p = _plan(pv_kwh=8.0)
        self.assertEqual(p.outcome, hb.BOOK)
        self.assertEqual(len(p.book), 2)
        self.assertFalse(p.forced)
        self.assertGreater(p.useful_kwh, 15.0)

    def test_a_bright_sunday_holds_the_tokens(self):
        p = _plan(pv_kwh=40.0)
        self.assertEqual(p.outcome, hb.HOLD_BRIGHT)
        self.assertEqual(p.book, ())
        self.assertIsNotNone(p.best_single_kwh)
        self.assertLess(p.best_single_kwh, hb.MIN_USEFUL_KWH_PER_HOUR)

    def test_the_last_sunday_books_whatever_the_weather(self):
        last = date(2026, 10, 25)
        now  = datetime(2026, 10, 22, 14, 0, tzinfo=LONDON).astimezone(timezone.utc)
        p = _plan(pv_kwh=40.0, day=last, now=now)
        self.assertEqual(p.outcome, hb.BOOK)
        self.assertTrue(p.forced)
        self.assertEqual(len(p.book), 2)

    def test_surplus_tokens_are_booked_early_when_the_sundays_cannot_absorb_them(self):
        """Ten hours held on 18 Oct with one Sunday after it: two can wait for the
        25th, so eight must go — capped at the two a day allows."""
        day = date(2026, 10, 18)
        now = datetime(2026, 10, 15, 14, 0, tzinfo=LONDON).astimezone(timezone.utc)
        p = _plan(pv_kwh=40.0, day=day, now=now, tokens=20)
        self.assertEqual(p.outcome, hb.BOOK)
        self.assertTrue(p.forced)
        self.assertEqual(len(p.book), 2)

    def test_tokens_that_later_sundays_can_absorb_are_not_forced(self):
        day = date(2026, 10, 18)
        now = datetime(2026, 10, 15, 14, 0, tzinfo=LONDON).astimezone(timezone.utc)
        p = _plan(pv_kwh=40.0, day=day, now=now, tokens=4)     # two hours, one Sunday after
        self.assertEqual(p.outcome, hb.HOLD_BRIGHT)

    def test_one_hour_is_all_an_odd_token_count_pays_for(self):
        p = _plan(pv_kwh=8.0, tokens=3)
        self.assertEqual(p.outcome, hb.BOOK)
        self.assertEqual(len(p.book), 1)

    def test_no_tokens_books_nothing(self):
        self.assertEqual(_plan(pv_kwh=8.0, tokens=1).outcome, hb.NO_TOKENS)

    def test_an_unreported_balance_books_nothing(self):
        """None means Octopus did not say, which is not a licence to spend."""
        self.assertEqual(_plan(pv_kwh=8.0, tokens=None).outcome, hb.NO_TOKENS)

    def test_a_zero_token_price_is_refused_rather_than_guessed(self):
        p = hb.plan_day(day=SUNDAY, slots=_slots(), tokens=7, tokens_per_hour=0,
                        now=THURSDAY, tz=LONDON, site=_site(), house=_house(),
                        pv=_pv(8.0), per_hour_kwh=10.0, scheme_end=SCHEME_END)
        self.assertEqual(p.outcome, hb.UNKNOWN_COST)

    def test_full_slots_are_never_chosen(self):
        p = _plan(pv_kwh=8.0, slots=_slots(full=(13, 14)))
        self.assertEqual(p.outcome, hb.BOOK)
        self.assertTrue(set(_local_hours(p.book)).isdisjoint({13, 14}))

    def test_refused_slots_are_never_chosen(self):
        p = _plan(pv_kwh=8.0, refused=("E_14",))
        self.assertNotIn(14, _local_hours(p.book))

    def test_every_slot_full_is_nothing_bookable(self):
        p = _plan(pv_kwh=8.0, slots=_slots(full=(11, 12, 13, 14)))
        self.assertEqual(p.outcome, hb.NOTHING_BOOKABLE)

    def test_a_slot_starting_within_five_minutes_cannot_be_booked(self):
        now = fs.local_wall(LONDON, SUNDAY, fs.time(13, 57))
        p = _plan(pv_kwh=8.0, now=now, slots=_slots(hours=(14,)),
                  start_kwh=10.0)
        self.assertEqual(p.outcome, hb.NOTHING_BOOKABLE)

    def test_an_already_booked_hour_leaves_room_for_one_more(self):
        p = _plan(pv_kwh=8.0, slots=_slots(booked=(14,)))
        self.assertEqual(p.outcome, hb.BOOK)
        self.assertEqual(len(p.book), 1)
        self.assertNotIn(14, _local_hours(p.book))

    def test_two_booked_hours_fill_the_day(self):
        self.assertEqual(_plan(pv_kwh=8.0, slots=_slots(booked=(13, 14))).outcome,
                         hb.DAY_FULL)

    def test_no_forecast_holds_unless_the_tokens_must_go(self):
        empty = fs.HourlyPvForecast({}, LONDON)
        self.assertEqual(_plan(pv=empty).outcome, hb.HOLD_NO_FORECAST)
        last = date(2026, 10, 25)
        now  = datetime(2026, 10, 22, 14, 0, tzinfo=LONDON).astimezone(timezone.utc)
        p = _plan(pv=empty, day=last, now=now)
        self.assertEqual(p.outcome, hb.BOOK)
        self.assertTrue(p.forced)
        self.assertEqual(_local_hours(p.book), [13, 14])   # latest slots first

    def test_on_a_dull_day_the_tie_goes_to_the_later_slots(self):
        p = _plan(pv_kwh=0.0)
        self.assertEqual(_local_hours(p.book), [13, 14])

    def test_a_second_hour_must_earn_its_place(self):
        """A middling day: the first hour is worth having and the second would
        mostly push the afternoon sun out, so one hour is booked, not two."""
        p = _plan(pv_kwh=25.0)
        self.assertEqual(p.outcome, hb.BOOK)
        self.assertEqual(len(p.book), 1)
        self.assertFalse(p.forced)

    def test_the_bar_is_applied_to_the_first_hour_too(self):
        """No hour can bring in more than the charger's 10 kWh, so a bar above
        that books nothing that is not forced."""
        p = _plan(pv_kwh=8.0, min_useful_kwh=10.5)
        self.assertEqual(p.outcome, hb.HOLD_BRIGHT)

    def test_decided_on_the_day_it_starts_from_the_real_battery(self):
        """At 9am on the Sunday the overnight charge has happened; a full battery
        leaves nothing for the free power to fill."""
        now = fs.local_wall(LONDON, SUNDAY, fs.time(9, 0))
        full = _plan(pv_kwh=8.0, now=now, start_kwh=_site().capacity_kwh)
        empty = _plan(pv_kwh=8.0, now=now, start_kwh=8.0)
        self.assertLess(full.useful_kwh or 0.0, empty.useful_kwh)


class TestTheMessagesReadAsEnglish(unittest.TestCase):
    """The standing notification rules: sentences, ASCII only, no pipes or equals
    signs, numbers with their meaning, counts in words, times as a person says
    them, and under Pushover's 1024-character limit."""

    TODAY = date(2026, 9, 24)

    def _check(self, title, body):
        for text in (title, body):
            text.encode("ascii")                        # raises on anything else
            self.assertNotIn("|", text)
            self.assertNotIn("=", text)
            self.assertNotIn("  ", text)
            self.assertNotRegex(text, r"\b\d{2}:\d{2}\b")   # never '13:00'
        self.assertLessEqual(len(body), 1024)
        self.assertTrue(body.endswith("."), body)
        self.assertFalse(title.endswith("."), title)

    def test_the_booking_message(self):
        p = _plan(pv_kwh=8.0)
        title, body = hb.booked_message(p, LONDON, self.TODAY, SCHEME_END)
        self._check(title, body)
        self.assertEqual(title, "Free electricity booked for Sunday")
        self.assertIn("Two free hours are booked for Sunday 27 September, from 1pm to 3pm.", body)
        self.assertIn("spends four of your seven tokens, leaving three, which is one "
                      "more free hour, to use by 1 November.", body)
        self.assertIn("kWh of sun that day", body)

    def test_one_hour_reads_in_the_singular(self):
        p = _plan(pv_kwh=8.0, tokens=3)
        title, body = hb.booked_message(p, LONDON, self.TODAY, SCHEME_END)
        self._check(title, body)
        self.assertIn("One free hour is booked", body)
        self.assertIn("leaving one, not enough for another hour.", body)

    def test_a_forced_booking_says_why(self):
        last = date(2026, 10, 25)
        now  = datetime(2026, 10, 22, 14, 0, tzinfo=LONDON).astimezone(timezone.utc)
        p = _plan(pv_kwh=40.0, day=last, now=now)
        title, body = hb.booked_message(p, LONDON, date(2026, 10, 22), SCHEME_END)
        self._check(title, body)
        self.assertIn("whatever the weather", body)

    def test_the_holding_message(self):
        p = _plan(pv_kwh=40.0)
        title, body = hb.hold_message(p, LONDON, self.TODAY, SCHEME_END)
        self._check(title, body)
        self.assertEqual(title, "Keeping your free hours for a duller Sunday")
        self.assertIn("your seven tokens, enough for three free hours,", body)
        self.assertIn("Four more Sundays are left before the offer ends on 1 November.", body)
        self.assertIn("almost no room for it.", body)

    def test_the_waiting_for_a_forecast_message(self):
        p = _plan(pv=fs.HourlyPvForecast({}, LONDON))
        title, body = hb.hold_message(p, LONDON, self.TODAY, SCHEME_END)
        self._check(title, body)
        self.assertEqual(title, "Waiting for the forecast before booking Sunday")
        self.assertIn("does not reach that far yet", body)
        self.assertIn("You have seven tokens, enough for three free hours, to use by "
                      "1 November.", body)

    def test_one_sunday_left_is_singular(self):
        day = date(2026, 10, 18)
        now = datetime(2026, 10, 15, 14, 0, tzinfo=LONDON).astimezone(timezone.utc)
        p = _plan(pv_kwh=40.0, day=day, now=now, tokens=4)
        title, body = hb.hold_message(p, LONDON, date(2026, 10, 15), SCHEME_END)
        self._check(title, body)
        self.assertIn("One more Sunday is left", body)

    def test_the_morning_reminder(self):
        title, body = hb.reminder_message(_slots(hours=(13, 14)), LONDON)
        self._check(title, body)
        self.assertEqual(title, "Free electricity from 1pm to 3pm today")
        self.assertIn("the washing machine, the tumble dryer and the dishwasher.", body)

    def test_separate_slots_are_joined_with_and(self):
        words = hb.span_words(_slots(hours=(11, 14)), LONDON)
        self.assertEqual(words, "from 11am to midday and from 2pm to 3pm")

    def test_the_result(self):
        title, body = hb.result_message(18.6, _slots(hours=(13, 14)), LONDON, 14.62)
        self._check(title, body)
        self.assertEqual(title, "The free hours banked 19 kWh")
        self.assertIn("about 2.72 pounds", body)

    def test_a_result_with_nothing_banked_says_so_plainly(self):
        title, body = hb.result_message(0.2, _slots(hours=(13,)), LONDON, 14.62)
        self._check(title, body)
        self.assertIn("only 0.2 kWh", body)

    def test_a_result_that_could_not_be_measured_says_so(self):
        title, body = hb.result_message(None, _slots(hours=(13,)), LONDON, 14.62)
        self._check(title, body)

    def test_clock_words(self):
        mk = lambda h, m=0: datetime(2026, 9, 27, h, m, tzinfo=LONDON)
        self.assertEqual(hb.clock_words(mk(11)), "11am")
        self.assertEqual(hb.clock_words(mk(12)), "midday")
        self.assertEqual(hb.clock_words(mk(13)), "1pm")
        self.assertEqual(hb.clock_words(mk(0)), "midnight")
        self.assertEqual(hb.clock_words(mk(13, 30)), "1:30pm")


if __name__ == "__main__":
    unittest.main()
