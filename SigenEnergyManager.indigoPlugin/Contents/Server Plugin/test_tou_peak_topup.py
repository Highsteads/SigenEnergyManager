#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    test_tou_peak_topup.py
# Description: The day-rate import on a time-of-use tariff (battery_manager 3.12).
#              Pins the 26-Sep-2026 fault: a dull Flux day bought tomorrow's whole
#              shortfall at the 24.4p day rate before midday, four times, with the
#              target ratcheting 34% -> 54%, while 2am's 14.6p was twelve hours off.
# Author:      CliveS & Claude Opus 5.5
# Date:        26-09-2026
# Version:     1.0

import unittest
from datetime import datetime, timedelta, timezone

from battery_manager import (
    BatteryManager,
    ManagerSnapshot,
    TariffData,
    ACTION_START_IMPORT,
    ACTION_SCHEDULE_IMPORT,
    ACTION_SELF_CONSUMPTION,
    PEAK_TOPUP_BUFFER_KWH,
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


def _peak_target(soc, hours_to_peak, reserve=20.0, solar_before=0.0):
    """What the battery must reach now: reserve + the peak + the run-up to it."""
    battery = soc / 100.0 * CAP
    need    = reserve / 100.0 * CAP + 3 * 2 * SLOT
    projected = battery + solar_before - hours_to_peak * 2 * SLOT
    buy = need - projected + PEAK_TOPUP_BUFFER_KWH
    return (battery + buy) / CAP * 100.0


class TestDayRateBuysOnlyThePeak(unittest.TestCase):

    def setUp(self):
        self.bm = BatteryManager()

    def test_a_dull_morning_buys_only_what_the_peak_needs(self):
        """25% at 11:30: 8.8 kWh, the run-up eats 2.7 and the peak needs 1.8 above a
        7.0 kWh reserve. Buy the 2.7 kWh gap plus the buffer, not tomorrow."""
        d = self.bm.evaluate(_snap(25.0, 11, 30))
        self.assertEqual(d.action, ACTION_START_IMPORT)
        self.assertAlmostEqual(d.target_soc_pct, _peak_target(25.0, 4.5), places=1)
        self.assertLess(d.target_soc_pct, 40.0)
        self.assertIn("peak", d.reason)
        self.assertIn("4pm to 7pm", d.reason)
        self.assertNotIn("Tomorrow at risk", d.reason)

    def test_the_target_does_not_ratchet_as_the_battery_fills(self):
        """The 26-Sep fault: each restart set target = SOC now + tomorrow's gap, so
        it climbed with the battery. The peak target is an absolute level."""
        first  = self.bm.evaluate(_snap(22.0, 11, 30))
        second = self.bm.evaluate(_snap(24.0, 11, 30))
        self.assertEqual(first.action, ACTION_START_IMPORT)
        self.assertEqual(second.action, ACTION_START_IMPORT)
        self.assertAlmostEqual(first.target_soc_pct, second.target_soc_pct, places=6)

    def test_reaching_the_target_does_not_start_another(self):
        """After a top-up the battery sits the buffer above need; nothing restarts."""
        d1 = self.bm.evaluate(_snap(25.0, 11, 30))
        d2 = self.bm.evaluate(_snap(d1.target_soc_pct, 11, 30, import_pending=True))
        self.assertEqual(d2.action, ACTION_SCHEDULE_IMPORT)

    def test_a_small_drift_after_a_top_up_does_not_restart_it(self):
        """The deadband: need grows by 1 kWh (under min + buffer) -> still no start."""
        d1 = self.bm.evaluate(_snap(25.0, 11, 30))
        drifted = d1.target_soc_pct - 1.0 / CAP * 100.0
        d2 = self.bm.evaluate(_snap(drifted, 11, 30, import_pending=True))
        self.assertEqual(d2.action, ACTION_SCHEDULE_IMPORT)

    def test_solar_before_the_peak_shrinks_the_purchase(self):
        p50 = {f"2026-09-26 {h:02d}:00:00": 500 for h in range(7, 19)}
        dull  = self.bm.evaluate(_snap(22.0, 11, 30))
        sunny = self.bm.evaluate(_snap(22.0, 11, 30, p50=p50))
        self.assertEqual(sunny.action, ACTION_START_IMPORT)
        # 11:30-16:00 is half of 11:00 plus 12:00-15:00: 0.25 + 2.0 kWh of sun.
        self.assertAlmostEqual(sunny.target_soc_pct,
                               _peak_target(22.0, 4.5, solar_before=2.25), places=1)
        self.assertLess(sunny.target_soc_pct, dull.target_soc_pct)

    def test_enough_for_the_peak_waits_for_the_cheap_window(self):
        d = self.bm.evaluate(_snap(35.0, 11, 30))
        self.assertEqual(d.action, ACTION_SCHEDULE_IMPORT)
        self.assertIn("cheap window from 2am", d.reason)

    def test_a_thin_peak_margin_is_not_worth_the_round_trip(self):
        """24.35 / 0.94 + 2p wear = 27.9p; a 28p peak beats it by under 1p."""
        d = self.bm.evaluate(_snap(15.0, 11, 30, tariff=_flux(peak_p=28.0)))
        self.assertEqual(d.action, ACTION_SCHEDULE_IMPORT)

    def test_nothing_is_bought_at_the_day_rate_after_the_peak(self):
        """20:00, battery low: every kWh until 2am costs the day rate either way."""
        d = self.bm.evaluate(_snap(12.0, 20, 0))
        self.assertEqual(d.action, ACTION_SCHEDULE_IMPORT)
        self.assertIn("normal rate", d.reason)

    def test_nothing_is_bought_during_the_peak(self):
        d = self.bm.evaluate(_snap(12.0, 17, 0))
        self.assertEqual(d.action, ACTION_SCHEDULE_IMPORT)

    def test_go_has_no_peak_so_it_always_waits(self):
        go = TariffData(tariff_key=TARIFF_GO, today_rate_p=25.0,
                        cheap_start="00:30", cheap_end="05:30", cheap_rate_p=8.5,
                        day_rate_p=25.0)
        d = self.bm.evaluate(_snap(12.0, 11, 30, tariff=go, reserve=0.0))
        self.assertEqual(d.action, ACTION_SCHEDULE_IMPORT)

    def test_the_cheap_window_still_buys_tomorrow(self):
        d = self.bm.evaluate(_snap(12.0, 3, 0))
        self.assertEqual(d.action, ACTION_START_IMPORT)
        self.assertIn("cheap window (2am to 5am)", d.reason)

    def test_the_reserve_floor_counts_not_the_health_floor(self):
        """With Flux armed the house stops at the 20% backup reserve, so a battery
        that is fine against a 1% floor can still be short for the peak."""
        armed   = self.bm.evaluate(_snap(28.0, 11, 30, reserve=20.0))
        unarmed = self.bm.evaluate(_snap(28.0, 11, 30, reserve=0.0))
        self.assertEqual(armed.action, ACTION_START_IMPORT)
        self.assertEqual(unarmed.action, ACTION_SCHEDULE_IMPORT)


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

    def test_the_day_rate_peak_top_up_is_still_the_managers(self):
        d = self.bm.evaluate(_snap(25.0, 11, 30, flux_owns=True))
        self.assertEqual(d.action, ACTION_START_IMPORT)
        self.assertIn("peak", d.reason)

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
