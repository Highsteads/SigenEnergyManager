#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    test_house_energy_cost.py
# Description: Tests for house_energy_cost.HouseCostLedger
# Author:      CliveS & Claude Opus 5.5
# Date:        28-11-2026
# Version:     1.0

import os
import unittest

from house_energy_cost import HouseCostLedger, MAX_STEP_S
from test_flux_supervisor import _mk_plugin   # installs the indigo stub
import plugin                                  # noqa: E402

CAP = 35.04


def run(ledger, t0, seconds, step, **kw):
    """Feed constant readings every `step` seconds; SOC follows the battery power."""
    soc = kw.pop("soc")
    t = t0
    while t < t0 + seconds:
        t += step
        soc += kw["batt_w"] * step / 3600.0 / 1000.0 / CAP * 100.0
        ledger.step(t, soc_pct=soc, capacity_kwh=CAP, **kw)
    return t, soc


class TestHouseCostLedger(unittest.TestCase):

    def test_battery_energy_is_priced_at_what_charged_it(self):
        led = HouseCostLedger()
        # Empty-ish battery seeded at the cheap rate, charged 3 h from the grid at 15.47p.
        t, soc = run(led, 0, 3 * 3600, 10, soc=20.0, grid_w=8000, batt_w=7500,
                     home_w=500, rate_p=15.47)
        self.assertAlmostEqual(led.battery_price_p(), 15.47, places=2)
        k0, c0 = led.house_kwh, led.house_cost_p
        # Daytime: house on the battery while the grid price is 25.63p.
        run(led, t, 3600, 10, soc=soc, grid_w=0, batt_w=-1000, home_w=1000, rate_p=25.63)
        rate = (led.house_cost_p - c0) / (led.house_kwh - k0)
        self.assertAlmostEqual(rate, 15.47, places=2)

    def test_grid_energy_to_the_house_is_priced_at_the_rate_in_force(self):
        led = HouseCostLedger()
        run(led, 0, 3600, 10, soc=50.0, grid_w=2000, batt_w=0, home_w=2000,
            rate_p=25.63)
        # The first reading only starts the clock, so one 10 s step is missing.
        self.assertAlmostEqual(led.house_kwh, 2.0 - 2.0 * 10 / 3600, places=6)
        self.assertAlmostEqual(led.house_cost_p / led.house_kwh, 25.63, places=2)

    def test_solar_is_free_and_lowers_the_battery_price(self):
        led = HouseCostLedger({"version": 1, "pool_kwh": 10.0, "pool_cost_p": 154.7})
        t, soc = run(led, 0, 3600, 10, soc=10.0 / CAP * 100, grid_w=0, batt_w=5000,
                     home_w=500, rate_p=25.63)
        # 10 kWh at 15.47p plus 5 kWh free -> about 10.3p a kWh.
        self.assertAlmostEqual(led.battery_price_p(), 154.7 / 15.0, places=1)
        self.assertAlmostEqual(led.house_cost_p, 0.0, places=6)

    def test_discharge_sold_to_the_grid_is_not_a_house_cost(self):
        led = HouseCostLedger({"version": 1, "pool_kwh": 20.0, "pool_cost_p": 309.4})
        run(led, 0, 3600, 10, soc=20.0 / CAP * 100, grid_w=-9000, batt_w=-10000,
            home_w=1000, rate_p=35.78)
        self.assertAlmostEqual(led.house_kwh, 1.0, places=2)
        self.assertAlmostEqual(led.house_cost_p / led.house_kwh, 15.47, places=2)

    def test_no_price_ever_known_counts_nothing(self):
        led = HouseCostLedger()
        run(led, 0, 600, 10, soc=50.0, grid_w=1000, batt_w=0, home_w=1000, rate_p=None)
        self.assertEqual(led.house_kwh, 0.0)
        self.assertEqual(led.house_cost_p, 0.0)

    def test_an_unknown_price_uses_the_last_one_known(self):
        led = HouseCostLedger()
        t, soc = run(led, 0, 60, 10, soc=50.0, grid_w=1000, batt_w=0, home_w=1000, rate_p=20.0)
        k0, c0 = led.house_kwh, led.house_cost_p
        run(led, t, 3600, 10, soc=soc, grid_w=1000, batt_w=0, home_w=1000, rate_p=None)
        self.assertAlmostEqual((led.house_cost_p - c0) / (led.house_kwh - k0), 20.0, places=6)

    def test_a_long_gap_is_not_integrated(self):
        led = HouseCostLedger()
        led.step(0, 1000, 0, 1000, 50.0, CAP, 20.0)
        self.assertFalse(led.step(MAX_STEP_S + 1, 1000, 0, 1000, 50.0, CAP, 20.0))
        self.assertEqual(led.house_kwh, 0.0)
        self.assertTrue(led.step(MAX_STEP_S + 11, 1000, 0, 1000, 50.0, CAP, 20.0))
        self.assertGreater(led.house_kwh, 0.0)

    def test_state_survives_a_round_trip(self):
        led = HouseCostLedger()
        run(led, 0, 600, 10, soc=50.0, grid_w=1500, batt_w=500, home_w=1000, rate_p=15.47)
        again = HouseCostLedger(led.to_state())
        self.assertAlmostEqual(again.house_kwh, led.house_kwh, places=5)
        self.assertAlmostEqual(again.house_cost_p, led.house_cost_p, places=5)
        self.assertAlmostEqual(again.battery_price_p(), led.battery_price_p(), places=5)
        self.assertIsNone(again.last_ts)

    def test_a_bad_state_file_starts_fresh(self):
        for bad in (None, "x", {}, {"version": 99, "house_kwh": 5},
                    {"version": 1, "house_kwh": "nan", "pool_kwh": -3, "pool_cost_p": 1}):
            led = HouseCostLedger(bad)
            self.assertEqual(led.house_kwh, 0.0)
            self.assertIsNone(led.battery_price_p())

    def test_seed_price_prices_the_first_contents(self):
        led = HouseCostLedger()
        led.step(0, 0, 0, 500, 60.0, CAP, 25.63, seed_price_p=15.47)
        self.assertAlmostEqual(led.battery_price_p(), 15.47, places=6)
        led2 = HouseCostLedger()
        led2.step(0, 0, 0, 500, 60.0, CAP, 25.63)
        self.assertAlmostEqual(led2.battery_price_p(), 25.63, places=6)

    def test_bad_readings_are_refused(self):
        led = HouseCostLedger()
        self.assertFalse(led.step(0, None, 0, 500, 50.0, CAP, 20.0))
        self.assertFalse(led.step(10, "x", 0, 500, 50.0, CAP, 20.0))
        self.assertFalse(led.step(20, 1000, float("nan"), 500, 50.0, CAP, 20.0))
        self.assertEqual(led.house_kwh, 0.0)

    def test_totals_never_go_down(self):
        led = HouseCostLedger({"version": 1, "pool_kwh": 1.0, "pool_cost_p": 15.0})
        last_k = last_c = 0.0
        t = 0
        for i in range(500):
            t += 10
            led.step(t, (i % 7 - 3) * 1000, (i % 5 - 2) * 3000, (i % 3) * 800,
                     (i * 3) % 101, CAP, 15.0 + (i % 4) * 5)
            self.assertGreaterEqual(led.house_kwh, last_k)
            self.assertGreaterEqual(led.house_cost_p, last_c)
            last_k, last_c = led.house_kwh, led.house_cost_p



class TestPluginWiring(unittest.TestCase):
    """The plugin feeds the ledger from its Modbus readings and keeps it on disk."""

    def _plugin(self):
        p = _mk_plugin()
        p.house_cost = HouseCostLedger()
        p.latest_rates_data = {
            "tariff_info": {"tariff_key": plugin.TARIFF_FLUX},
            plugin.TARIFF_FLUX: {"cheap_p": 15.47, "standard_p": 25.63, "peak_p": 35.78,
                                 "cheap_start": "02:00", "cheap_end": "05:00",
                                 "peak_start": "16:00", "peak_end": "19:00"},
        }
        return p

    def test_prices_come_from_the_active_tariff(self):
        p = self._plugin()
        now_p, seed_p = p._house_cost_prices()
        self.assertIn(now_p, (15.47, 25.63, 35.78))
        self.assertEqual(seed_p, 15.47, "the battery's first contents at the cheap band")

    def test_no_rates_gives_no_prices(self):
        p = self._plugin()
        p.latest_rates_data = {}
        self.assertEqual(p._house_cost_prices(), (None, None))

    def test_readings_reach_the_ledger_and_survive_a_save(self):
        p = self._plugin()
        for i in range(7):
            p._house_cost_step({"_read_at": 1000.0 + i * 10, "gridPowerWatts": 0,
                                "batteryPowerWatts": -1000, "homePowerWatts": 1000,
                                "batterySoc": 60.0})
        self.assertAlmostEqual(p.house_cost.house_kwh, 60 / 3600, places=6)
        self.assertAlmostEqual(p.house_cost.battery_price_p(), 15.47, places=6)
        self.assertAlmostEqual(p.house_cost.house_cost_p / p.house_cost.house_kwh,
                               15.47, places=6)
        p._save_house_cost()
        again = p._load_house_cost()
        self.assertAlmostEqual(again.house_kwh, p.house_cost.house_kwh, places=6)
        self.assertTrue(os.path.exists(os.path.join(p.data_dir, plugin.HOUSE_COST_FILE)))

    def test_a_broken_file_starts_again_without_raising(self):
        p = self._plugin()
        with open(os.path.join(p.data_dir, plugin.HOUSE_COST_FILE), "w") as fh:
            fh.write("{not json")
        self.assertEqual(p._load_house_cost().house_kwh, 0.0)

    def test_a_step_with_junk_readings_does_not_raise(self):
        p = self._plugin()
        p._house_cost_step({"gridPowerWatts": "x"})
        p.house_cost = None
        p._house_cost_step({})


if __name__ == "__main__":
    unittest.main()
