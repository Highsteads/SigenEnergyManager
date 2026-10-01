#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    test_tou_peak_topup.py
# Description: The day-rate import on a time-of-use tariff (battery_manager 3.12,
#              3.14). Pins the 26-Sep-2026 fault: a dull Flux day bought tomorrow's
#              whole shortfall at the 24.4p day rate before midday, four times, with
#              the target ratcheting 34% -> 54%, while 2am's 14.6p was twelve hours
#              off. From 3.14 the day buys nothing at all for the peak either, and
#              only an Axle event the battery cannot cover buys in the day.
# Author:      CliveS & Claude Opus 5.5
# Date:        26-09-2026; 2.0 27-09-2026
# Version:     2.0

import unittest
from datetime import datetime, timedelta, timezone

from battery_manager import (
    BatteryManager,
    ManagerSnapshot,
    TariffData,
    ACTION_START_IMPORT,
    ACTION_SCHEDULE_IMPORT,
    ACTION_SELF_CONSUMPTION,
    MIN_IMPORT_KWH,
    TARIFF_GO,
)

try:
    from battery_manager import TARIFF_FLUX
except ImportError:                                   # pragma: no cover
    TARIFF_FLUX = "flux"

CAP  = 35.04
EFF  = 0.94
SLOT = 0.30                     # kWh per half hour: 0.6 kWh an hour, 14.4 a day


def _utc(day, hh, mm=0):
    """A UTC instant for a LOCAL wall time on a BST day (26-Sep-2026 is BST)."""
    return datetime(2026, 9, day, hh, mm, tzinfo=timezone.utc) - timedelta(hours=1)


def _flux(peak_p=34.10, day_p=24.35, cheap_p=14.62):
    return TariffData(
        tariff_key="flux", today_rate_p=day_p,
        cheap_start="02:00", cheap_end="05:00", cheap_rate_p=cheap_p,
        day_rate_p=day_p, peak_start="16:00", peak_end="19:00", peak_rate_p=peak_p)


def _snap(soc, local_hh, local_mm=0, tariff=None, reserve=20.0, p50=None,
          import_pending=False, tomorrow_kwh=0.0, tracking=1.0, flux_owns=False,
          dawn_target=10.0):
    return ManagerSnapshot(
        current_soc_pct     = soc,
        capacity_kwh        = CAP,
        efficiency          = EFF,
        health_cutoff_pct   = 1.0,
        reserve_floor_pct   = reserve,
        wear_p_per_kwh      = 2.0,
        import_pending      = import_pending,
        weekday_kwh         = 14.4, monday_kwh=14.4, saturday_kwh=14.4, sunday_kwh=14.4,
        corrected_tomorrow_kwh = tomorrow_kwh,
        tariff              = tariff if tariff is not None else _flux(),
        forecast_p50        = p50 or {},
        dawn_times          = {"2026-09-26": _utc(26, 7), "2026-09-27": _utc(27, 7)},
        consumption_profile = [SLOT] * 48,
        pv_tracking_factor  = tracking,
        flux_owns_cheap_window = flux_owns,
        dawn_target_pct     = dawn_target,
        now                 = _utc(26, local_hh, local_mm),
    )


class TestTheDayBuysNothingForThePeak(unittest.TestCase):
    """3.14. CliveS, 27-Sep-2026: "the only time during the day that we import is
    when we have free hours or the battery gets near the lower battery limit".
    The 3.12 day-rate top-up for the peak is gone; tomorrow waits for 2am."""

    def setUp(self):
        self.bm = BatteryManager()

    def _day_times(self):
        for hh in range(5, 24):
            for mm in (0, 30):
                yield hh, mm
        yield 0, 30
        yield 1, 30

    def test_a_dull_morning_buys_nothing(self):
        """25% at 11:30 used to buy for the peak at 24.4p."""
        d = self.bm.evaluate(_snap(25.0, 11, 30))
        self.assertNotEqual(d.action, ACTION_START_IMPORT)
        self.assertEqual(d.action, ACTION_SCHEDULE_IMPORT)
        self.assertIn("cheap window from 2am", d.reason)

    def test_nothing_starts_an_import_anywhere_in_the_day(self):
        for soc in (3.0, 12.0, 25.0, 40.0):
            for hh, mm in self._day_times():
                for owns in (False, True):
                    d = self.bm.evaluate(_snap(soc, hh, mm, flux_owns=owns))
                    self.assertNotEqual(d.action, ACTION_START_IMPORT,
                                        (soc, hh, mm, owns, d.reason))

    def test_nothing_is_bought_at_the_day_rate_after_the_peak(self):
        """20:00, battery low: every kWh until 2am costs the day rate either way."""
        d = self.bm.evaluate(_snap(12.0, 20, 0))
        self.assertEqual(d.action, ACTION_SCHEDULE_IMPORT)
        self.assertIn("normal rate", d.reason)

    def test_go_has_no_peak_so_it_always_waits(self):
        go = TariffData(tariff_key=TARIFF_GO, today_rate_p=25.0,
                        cheap_start="00:30", cheap_end="05:30", cheap_rate_p=8.5,
                        day_rate_p=25.0)
        d = self.bm.evaluate(_snap(12.0, 11, 30, tariff=go, reserve=0.0))
        self.assertEqual(d.action, ACTION_SCHEDULE_IMPORT)

    def test_the_cheap_window_still_buys_tomorrow(self):
        d = self.bm.evaluate(_snap(12.0, 3, 0))
        self.assertEqual(d.action, ACTION_START_IMPORT)
        # 3.15: with Flux not running the window, the manager's cheap-window fallback
        # buys it, and holds the battery to 5am.
        self.assertEqual(d.import_purpose, "cheap_window")
        self.assertIn("for tomorrow", d.reason)

    def test_an_unknown_cheap_window_holds_rather_than_buying_now(self):
        """One failed Octopus slot fetch used to mean 10 kW at once, at any hour."""
        blind = TariffData(tariff_key="flux", today_rate_p=24.35, day_rate_p=24.35,
                           peak_start="16:00", peak_end="19:00", peak_rate_p=34.10)
        for hh in (11, 17, 21):
            d = self.bm.evaluate(_snap(12.0, hh, 0, tariff=blind))
            self.assertEqual(d.action, ACTION_SELF_CONSUMPTION, hh)
            self.assertTrue(d.import_held, hh)
            self.assertIn("cheap window times are not known", d.reason)


class TestAxleEventCover(unittest.TestCase):
    """3.14. The one daytime purchase: an Axle event the battery cannot cover.
    The sums are flux_strategy.event_cover's; the manager only acts on them."""

    def setUp(self):
        self.bm = BatteryManager()

    def _cover(self, soc, active, target, hh=14):
        snap = _snap(soc, hh, 0)
        snap.event_cover_active     = active
        snap.event_cover_target_pct = target
        snap.event_cover_reason     = "the test event needs 60% by 4pm"
        return self.bm.evaluate(snap)

    def test_an_active_cover_buys_to_its_target(self):
        d = self._cover(30.0, True, 60.0)
        self.assertEqual(d.action, ACTION_START_IMPORT)
        self.assertAlmostEqual(d.target_soc_pct, 60.0)
        self.assertIn("Axle", d.reason)
        self.assertEqual(d.audit_trail[-1][0], "EVENT-COVER")

    def test_a_planned_cover_buys_nothing_yet(self):
        d = self._cover(30.0, False, 60.0)
        self.assertNotEqual(d.action, ACTION_START_IMPORT)
        self.assertTrue(any(tag == "EVENT-COVER" and "planned" in msg
                            for tag, msg in d.audit_trail))

    def test_a_battery_already_at_the_target_buys_nothing(self):
        d = self._cover(61.0, True, 60.0)
        self.assertNotEqual(d.action, ACTION_START_IMPORT)


class TestImportNeededHysteresis(unittest.TestCase):
    """26-Sep-2026: import_needed read True at 11:24, False at 11:30, True at 11:36."""

    def setUp(self):
        self.bm = BatteryManager()

    def _short_by(self, grid_kwh, pending):
        # Night, so the balance is battery - drain to dawn. 03:00 -> 07:00 drains
        # 2.4 kWh; tomorrow needs 14.4 with no sun. Pick the SOC that leaves the
        # grid-side shortfall at grid_kwh.
        need_at_dawn = 14.4 - grid_kwh * EFF
        soc = (need_at_dawn + 2.4) / CAP * 100.0
        return self.bm._calculate_24h_balance(
            _snap(soc, 3, 0, import_pending=pending))

    def test_a_small_shortfall_does_not_start_a_plan(self):
        b = self._short_by(MIN_IMPORT_KWH / 2, pending=False)
        self.assertFalse(b.import_needed)

    def test_the_same_shortfall_keeps_a_plan_that_is_already_running(self):
        b = self._short_by(MIN_IMPORT_KWH / 2, pending=True)
        self.assertTrue(b.import_needed)

    def test_a_running_plan_lets_go_once_the_shortfall_is_gone(self):
        b = self._short_by(-0.5, pending=True)
        self.assertFalse(b.import_needed)

    def test_the_start_threshold_is_unchanged(self):
        b = self._short_by(MIN_IMPORT_KWH + 0.1, pending=False)
        self.assertTrue(b.import_needed)



class TestDullAfternoonDrainsTheBattery(unittest.TestCase):
    """battery_manager 3.13: the house using more than the panels make before dusk
    comes off the dawn projection. It used to be max(0, solar - home)."""

    def setUp(self):
        self.bm = BatteryManager()

    def _dawn(self, wh_per_hour, soc=50.0, tracking=1.0):
        # Hours 07-17 at or above the 500 Wh dusk threshold, so it is daytime and
        # dusk is 18:00; `tracking` is the day's measured shortfall against forecast.
        p50 = {f"2026-09-26 {h:02d}:00:00": wh_per_hour for h in range(7, 18)}
        return self.bm._calculate_24h_balance(
            _snap(soc, 12, 0, p50=p50, tracking=tracking))

    def test_a_dull_afternoon_is_a_drain_not_a_zero(self):
        b = self._dawn(600, tracking=0.4)   # ~1.3 kWh of sun against 3.6 of house
        self.assertTrue(b.is_daytime)
        deficit = b.remaining_home_to_dusk_kwh - b.remaining_solar_kwh
        self.assertGreater(deficit, 1.5)
        overnight = self.bm._estimate_consumption_until(
            b.dusk_dt, b.dawn_dt, [SLOT] * 48)
        expected = 0.5 * CAP - deficit - overnight
        self.assertAlmostEqual(b.battery_at_dawn_kwh, round(expected, 2), places=2)

    def test_a_sunny_afternoon_still_charges(self):
        b = self._dawn(3000)
        surplus = b.remaining_solar_kwh - b.remaining_home_to_dusk_kwh
        self.assertGreater(surplus, 0.0)
        overnight = self.bm._estimate_consumption_until(
            b.dusk_dt, b.dawn_dt, [SLOT] * 48)
        expected = min(CAP, 0.5 * CAP + surplus) - overnight
        self.assertAlmostEqual(b.battery_at_dawn_kwh, round(expected, 2), places=2)

    def test_the_dusk_level_never_goes_below_the_floor(self):
        b = self._dawn(600, soc=3.0, tracking=0.4)
        self.assertTrue(b.is_daytime)
        self.assertAlmostEqual(b.battery_at_dawn_kwh, round(0.01 * CAP, 2), places=2)



class TestFluxOwnsTheCheapWindow(unittest.TestCase):
    """battery_manager 3.13: with Flux armed the manager leaves the 2am charge to it."""

    def setUp(self):
        self.bm = BatteryManager()

    def test_the_manager_no_longer_schedules_its_own_2am_charge(self):
        d = self.bm.evaluate(_snap(35.0, 11, 30, flux_owns=True))
        self.assertEqual(d.action, ACTION_SELF_CONSUMPTION)
        self.assertIn("Flux controller buys it in the cheap window from 2am", d.reason)

    def test_nor_starts_one_inside_the_window(self):
        d = self.bm.evaluate(_snap(12.0, 3, 0, flux_owns=True))
        self.assertEqual(d.action, ACTION_SELF_CONSUMPTION)
        self.assertIn("buys it now", d.reason)

    def test_the_day_buys_nothing_for_the_peak_under_flux_either(self):
        d = self.bm.evaluate(_snap(25.0, 11, 30, flux_owns=True))
        self.assertEqual(d.action, ACTION_SELF_CONSUMPTION)
        self.assertIn("Flux controller buys it", d.reason)

    def test_without_flux_the_manager_still_buys_in_the_window(self):
        d = self.bm.evaluate(_snap(12.0, 3, 0, flux_owns=False))
        self.assertEqual(d.action, ACTION_START_IMPORT)

    def test_the_resilience_top_up_is_left_to_flux_too(self):
        # Tomorrow covered by sun, SOC under the 20% reserve target, 03:00.
        plain = self.bm._check_resilience_buffer(
            _snap(15.0, 3, 0, tomorrow_kwh=40.0, dawn_target=20.0),
            self.bm._calculate_24h_balance(
                _snap(15.0, 3, 0, tomorrow_kwh=40.0, dawn_target=20.0)))
        owned = self.bm._check_resilience_buffer(
            _snap(15.0, 3, 0, tomorrow_kwh=40.0, dawn_target=20.0, flux_owns=True),
            self.bm._calculate_24h_balance(
                _snap(15.0, 3, 0, tomorrow_kwh=40.0, dawn_target=20.0, flux_owns=True)))
        self.assertIsNotNone(plain, "control: without Flux the reserve is bought")
        self.assertEqual(plain.action, ACTION_START_IMPORT)
        self.assertIsNone(owned)


if __name__ == "__main__":
    unittest.main()
