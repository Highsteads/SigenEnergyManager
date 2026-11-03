#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    test_flux_charge_margin.py
# Description: The 2am charge keeps CHARGE_MARGIN_KWH on top of its sized target, where
#              the sun leaves room (flux_strategy 2.16). CliveS, 02-Nov-2026: the charge
#              stopped at 81% and left about 1.5 kWh above the reserve at the next 2am.
# Author:      CliveS & Claude Opus 5.5
# Date:        02-11-2026
# Version:     1.0

import unittest
from datetime import date
from dataclasses import replace
from unittest import mock

import flux_strategy as fs
from test_flux_strategy import _bands, _inputs, _profile, _pv, _site, EXPORT_P, LONDON

DAY = (2026, 11, 2)


def _plain(inputs):
    """The same night with no margin, for comparison."""
    with mock.patch.object(fs, "CHARGE_MARGIN_KWH", 0.0):
        return fs.plan(inputs)


def _night(soc_pct=17.5, forecast=13.1, house=21.0, **over):
    """2 November's numbers: 17.5% at 2am, 13.1 kWh forecast, a 15% reserve."""
    d = date(*DAY)
    over.setdefault("site", _site(reserve_pct=15.0, wear_p_per_kwh=2.0))
    return _inputs((2, 0), soc_pct=soc_pct, day=DAY, pv=_pv(d, forecast),
                   house=_profile(house), **over)


class TestTheMarginIsKept(unittest.TestCase):
    def test_a_dull_night_buys_the_margin_on_top_of_the_sized_charge(self):
        inputs = _night()
        before, after = _plain(inputs), fs.plan(inputs)
        self.assertEqual((before.mode, after.mode), (fs.MODE_CHARGE, fs.MODE_CHARGE))
        self.assertAlmostEqual(after.planned_kwh - before.planned_kwh,
                               fs.CHARGE_MARGIN_KWH, delta=0.05)
        points = fs.CHARGE_MARGIN_KWH / inputs.site.capacity_kwh * 100.0
        self.assertAlmostEqual(after.charge_cutoff_pct - before.charge_cutoff_pct,
                               points, delta=1.0)

    def test_the_log_says_what_the_margin_is(self):
        reason = fs.plan(_night()).reason
        self.assertIn("4.0 kWh is kept back as a margin", reason)
        self.assertIn("is for the peak window", reason)

    def test_it_is_kept_when_the_peak_sale_is_not_worth_doing(self):
        d = date(*DAY)
        now = fs._wall(LONDON, d, fs.time(2, 0))
        lean = _bands(d, now, exp={**EXPORT_P, "peak": 12.0})
        # A big enough house that the 50% minimum does not already cover the margin.
        inputs = _night(bands=lean, forecast=6.0, house=26.0)
        before, after = _plain(inputs), fs.plan(inputs)
        self.assertAlmostEqual(after.planned_kwh - before.planned_kwh,
                               fs.CHARGE_MARGIN_KWH, delta=0.05)
        self.assertNotIn("is for the peak window", after.reason)

    def test_a_battery_already_above_the_whole_target_is_not_topped_up(self):
        target = fs.plan(_night()).charge_cutoff_pct
        decision = fs.plan(_night(soc_pct=target + 1.0))
        self.assertEqual(decision.mode, fs.MODE_HOLD, decision.reason)


class TestTheMarginLeavesRoom(unittest.TestCase):
    def test_never_above_the_ceiling(self):
        inputs = _night(forecast=2.0, house=30.0)
        self.assertLessEqual(fs.plan(inputs).charge_cutoff_pct, 100.0)
        capped = replace(inputs, site=replace(inputs.site, max_charge_soc_pct=90.0))
        decision = fs.plan(capped)
        self.assertLessEqual(decision.charge_cutoff_pct, 90.0)
        # Nor does the amount it says it is buying run past the ceiling.
        room = capped.site.capacity_kwh * (90.0 - capped.soc_pct) / 100.0
        self.assertLessEqual(decision.planned_kwh, room + 0.01)

    def test_a_sunny_day_keeps_the_room_for_its_own_sun(self):
        inputs = _night(soc_pct=30.0, forecast=45.0)
        before, after = _plain(inputs), fs.plan(inputs)
        self.assertEqual(after.charge_cutoff_pct, before.charge_cutoff_pct)
        self.assertEqual(after.planned_kwh, before.planned_kwh)
        self.assertNotIn("margin", after.reason.replace("p margin", ""))

    def test_the_sun_room_check_is_what_stops_it(self):
        """Mutation guard: with no brighter-day check the same night would buy more."""
        inputs = _night(soc_pct=30.0, forecast=45.0)
        with mock.patch.object(fs, "_sun_room_kwh", lambda i, b, want, w, p: want):
            unchecked = fs.plan(inputs)
        self.assertGreaterEqual(unchecked.charge_cutoff_pct or 0.0,
                                fs.plan(inputs).charge_cutoff_pct or 0.0)

    def test_a_booked_free_hour_keeps_its_room(self):
        d = date(2026, 11, 1)
        start = fs._wall(LONDON, d, fs.time(13, 0))
        end = fs._wall(LONDON, d, fs.time(15, 0))
        free = fs.EventCommitment(source="octopus", kind="import", start=start, end=end,
                                  energy_kwh=20.0)
        inputs = _inputs((2, 0), soc_pct=20.0, day=(2026, 11, 1), pv=_pv(d, 5.0),
                         house=_profile(21.0), commitments=(free,),
                         site=_site(reserve_pct=15.0, wear_p_per_kwh=2.0))
        site, end_of_night = inputs.site, fs.next_local(inputs.now, LONDON, fs.FLUX_CHARGE_END)
        peak_at = fs.next_local(end_of_night, LONDON, fs.FLUX_PEAK_START)

        def free_taken(decision):
            start_kwh = site.capacity_kwh * decision.charge_cutoff_pct / 100.0
            steps = fs.build_steps(inputs, end_of_night, peak_at)
            return fs.walk(site, steps, start_kwh).free_in_kwh

        before, after = _plain(inputs), fs.plan(inputs)
        self.assertAlmostEqual(free_taken(after), free_taken(before), delta=0.5)


class TestAShortfallTheLogCanShow(unittest.TestCase):
    PLAN = (81.0, 1000, 0.5, 10.6, 0.0)

    def _reason(self, infeasible, late=0.0):
        with mock.patch.object(fs, "_charge_plan",
                               lambda i: self.PLAN + (infeasible, 0.0, late)):
            return fs.plan(_inputs((3, 0), soc_pct=50.0, day=DAY)).reason

    def test_nothing_is_said_about_a_shortfall_that_rounds_to_nothing(self):
        self.assertNotIn("will not fit", self._reason(0.04))

    def test_a_real_shortfall_is_still_reported(self):
        self.assertIn("About 0.4 kWh of what is needed will not fit", self._reason(0.4))

    def test_a_small_late_shortfall_is_not_called_a_full_battery(self):
        # 3-Nov-2026 04:49-04:52: 0.1 kWh short on time, logged as "will not fit".
        reason = self._reason(0.1, late=0.1)
        self.assertNotIn("will not fit", reason)
        self.assertNotIn("time left", reason)

    def test_a_large_late_shortfall_names_the_time_left(self):
        reason = self._reason(3.0, late=3.0)
        self.assertIn(f"About 3.0 kWh of it cannot be bought in the time left before "
                      f"{fs.FLUX_CHARGE_END:%H:%M}", reason)
        self.assertNotIn("will not fit", reason)

    def test_both_shortfalls_are_told_apart(self):
        reason = self._reason(2.0, late=1.2)
        self.assertIn("About 0.8 kWh of what is needed will not fit", reason)
        self.assertIn("About 1.2 kWh of it cannot be bought", reason)


class TestTheLateShareOfTheShortfall(unittest.TestCase):
    def test_a_charge_started_late_reports_its_time_shortfall(self):
        # 04:50 with a near-empty battery: the room is there, the minutes are not.
        out = fs._charge_plan(_inputs((4, 50), soc_pct=20.0, day=DAY))
        self.assertEqual(len(out), 8)
        infeasible, late = out[5], out[7]
        self.assertGreater(late, fs.MIN_TRADE_KWH)
        self.assertLessEqual(late, infeasible + 1e-9)


if __name__ == "__main__":
    unittest.main()
