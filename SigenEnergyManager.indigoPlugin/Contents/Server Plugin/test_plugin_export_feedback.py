#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    test_plugin_export_feedback.py
# Description: plugin.py's side of the export feedback loop (v5.90.0): the intraday
#              PV tracking accumulators and their clipping gate, the forecast slice
#              integrator, the measured weekend uplift, and the need scales. Runs
#              without Indigo.
# Author:      CliveS & Claude Fable 5.1
# Date:        05-09-2026 15:10
# Version:     1.0
import json
import os
import shutil
import sys
import tempfile
import threading
import types
import unittest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

if "indigo" not in sys.modules:
    _indigo = types.ModuleType("indigo")
    class _PluginBase:
        def __init__(self, *a, **k):
            pass
    _indigo.PluginBase = _PluginBase
    _indigo.Dict = dict
    _indigo.List = list
    for _attr in ("kStateImageSel", "server", "devices", "variables", "kDeviceAction",
                  "kDimmerRelayAction", "kSensorAction", "kUniversalAction", "activePlugin"):
        setattr(_indigo, _attr, MagicMock())
    sys.modules["indigo"] = _indigo
_pm = MagicMock()
sys.modules.setdefault("pymodbus", _pm)
sys.modules.setdefault("pymodbus.client", sys.modules["pymodbus"].client)
sys.modules.setdefault("pymodbus.exceptions", sys.modules["pymodbus"].exceptions)
sys.modules.setdefault("requests", MagicMock())
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import plugin                                                    # noqa: E402
from daily_energy import DailyEnergy, local_midnight_epoch       # noqa: E402

D1     = "2026-09-05"
MID_D1 = local_midnight_epoch(D1)
T10    = MID_D1 + 10 * 3600          # 10:00 local


def _mk(tmp):
    p = plugin.Plugin.__new__(plugin.Plugin)
    p.logger        = MagicMock()
    p._state_lock   = threading.RLock()
    p.data_dir      = tmp
    p._get_data_dir = lambda: tmp
    p.pluginPrefs   = {"batteryCapacityKwh": "35.04", "maxExportKw": "4.0"}
    p.latest_inverter_data = {"gridPowerWatts": -1000, "batterySoc": 60.0, "batteryPowerWatts": 3000}
    p.latest_forecast_data = {"biasFactorToday": 1.0,
                              "_hourly_p50_today": {f"{D1} {h:02d}:00:00": 6000 for h in range(8, 18)}}
    p.daily_energy  = DailyEnergy()
    # A reading at the boundary, so today is neither partial nor absent — the
    # tracker must refuse to learn from a day it never saw the start of.
    p.daily_energy.observe({"pv": 7600.0, "home": 4000.0, "gridImport": 57.0,
                            "gridExport": 3590.0, "batteryCharge": 2470.0,
                            "batteryDischarge": 2390.0}, MID_D1 + 20, D1)
    p.store = {"pv_daily_kwh": 0.0, "energy_day_partial": False,
               "pv_track_date": "", "pv_track_actual_kwh": 0.0, "pv_track_forecast_kwh": 0.0,
               "pv_track_last_epoch": 0.0, "pv_track_last_pv_kwh": None, "pv_track_clipped_min": 0.0,
               "pv_track_factor": 1.0, "pv_track_ratio": None, "pv_track_last_hour": None,
               "weekend_uplift": None, "weekend_uplift_date": ""}
    return p


class _Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestForecastSlice(_Tmp):

    def test_integrates_the_bucket_over_the_interval(self):
        p = _mk(self.tmp)
        # 10:15 -> 10:45 inside a 6 kWh bucket = 3.0 kWh
        self.assertAlmostEqual(p._forecast_kwh_between(T10 + 900, T10 + 2700), 3.0, places=3)
        # 10:30 -> 11:30 straddles two 6 kWh buckets = 6.0 kWh
        self.assertAlmostEqual(p._forecast_kwh_between(T10 + 1800, T10 + 5400), 6.0, places=3)

    def test_applies_todays_bias_factor_and_handles_empty_or_inverted(self):
        p = _mk(self.tmp)
        p.latest_forecast_data["biasFactorToday"] = 0.5
        self.assertAlmostEqual(p._forecast_kwh_between(T10, T10 + 3600), 3.0, places=3)
        p.latest_forecast_data = {}
        self.assertEqual(p._forecast_kwh_between(T10, T10 + 3600), 0.0)
        p = _mk(self.tmp)
        self.assertEqual(p._forecast_kwh_between(T10 + 60, T10), 0.0)


class TestClippingGate(_Tmp):

    def test_export_at_the_cap_is_clipped(self):
        p = _mk(self.tmp)
        self.assertFalse(p._pv_unclipped({"gridPowerWatts": -3900, "batterySoc": 70.0, "batteryPowerWatts": 500}))
        self.assertTrue(p._pv_unclipped({"gridPowerWatts": -3000, "batterySoc": 70.0, "batteryPowerWatts": 500}))

    def test_full_battery_not_charging_is_clipped(self):
        p = _mk(self.tmp)
        self.assertFalse(p._pv_unclipped({"gridPowerWatts": -500, "batterySoc": 99.5, "batteryPowerWatts": 20}))
        self.assertTrue(p._pv_unclipped({"gridPowerWatts": -500, "batterySoc": 99.5, "batteryPowerWatts": 2000}))

    def test_unreadable_data_does_not_block_learning(self):
        p = _mk(self.tmp)
        self.assertTrue(p._pv_unclipped({"gridPowerWatts": "x"}))
        self.assertTrue(p._pv_unclipped(None))


class TestTrackingAccumulators(_Tmp):

    def _tick(self, p, t, pv_kwh, unclipped=True):
        p.store["pv_daily_kwh"] = pv_kwh
        p.latest_inverter_data["gridPowerWatts"] = -1000 if unclipped else -4000
        with patch.object(plugin, "_local_today_str", return_value=D1), \
             patch("plugin.time.time", return_value=t), \
             patch.object(plugin, "_london_now", return_value=datetime(2026, 9, 5, 10, 0)):
            p._update_pv_tracking()

    def test_ratio_is_measured_against_the_forecast_for_the_same_minutes(self):
        p = _mk(self.tmp)
        self._tick(p, T10, 10.0)                       # seeds, nothing accumulated
        self.assertEqual(p.store["pv_track_actual_kwh"], 0.0)
        # ten minutes later 0.5 kWh arrived against 1.0 kWh forecast (6 kW * 10 min)
        self._tick(p, T10 + 600, 10.5)
        self.assertAlmostEqual(p.store["pv_track_actual_kwh"], 0.5, places=3)
        self.assertAlmostEqual(p.store["pv_track_forecast_kwh"], 1.0, places=3)
        self.assertEqual(p.store["pv_track_factor"], 1.0)          # below the 2 kWh minimum
        # another 30 minutes at half the forecast: 3.0 forecast in total -> weight 0.375
        self._tick(p, T10 + 2400, 11.5)
        self.assertAlmostEqual(p.store["pv_track_forecast_kwh"], 4.0, places=3)
        self.assertAlmostEqual(p.store["pv_track_actual_kwh"], 1.5, places=3)
        self.assertEqual(p.store["pv_track_ratio"], 0.375)
        self.assertAlmostEqual(p.store["pv_track_factor"], 1.0 + 0.5 * (0.375 - 1.0), places=3)

    def test_clipped_minutes_are_not_learned_from(self):
        p = _mk(self.tmp)
        self._tick(p, T10, 10.0)
        self._tick(p, T10 + 600, 10.2, unclipped=False)   # at the export cap: PV turned away
        self.assertEqual(p.store["pv_track_actual_kwh"], 0.0)
        self.assertEqual(p.store["pv_track_forecast_kwh"], 0.0)
        self.assertAlmostEqual(p.store["pv_track_clipped_min"], 10.0, places=3)
        self._tick(p, T10 + 1200, 11.2)                     # free again: counts from here
        self.assertAlmostEqual(p.store["pv_track_actual_kwh"], 1.0, places=3)

    def test_a_partial_day_yields_a_neutral_factor(self):
        p = _mk(self.tmp)
        p.store["energy_day_partial"] = True
        self._tick(p, T10, 10.0)
        self._tick(p, T10 + 3600, 12.0)                     # 2 kWh vs 6 forecast -> would be 0.7
        self.assertEqual(p.store["pv_track_factor"], 1.0)
        self.assertIsNone(p.store["pv_track_ratio"])

    def test_a_day_the_object_never_saw_the_start_of_is_not_learned_from(self):
        p = _mk(self.tmp)
        p.daily_energy = DailyEnergy()                       # nothing observed today
        self._tick(p, T10, 10.0)
        self._tick(p, T10 + 3600, 12.0)
        self.assertEqual(p.store["pv_track_factor"], 1.0)
        self.assertIsNone(p.store["pv_track_ratio"])

    def test_a_new_day_resets_the_accumulators(self):
        p = _mk(self.tmp)
        self._tick(p, T10, 10.0)
        self._tick(p, T10 + 3600, 14.0)
        self.assertGreater(p.store["pv_track_forecast_kwh"], 0.0)
        with patch.object(plugin, "_local_today_str", return_value="2026-09-06"), \
             patch("plugin.time.time", return_value=T10 + 86400), \
             patch.object(plugin, "_london_now", return_value=datetime(2026, 9, 6, 10, 0)):
            p._update_pv_tracking()
        self.assertEqual(p.store["pv_track_forecast_kwh"], 0.0)
        self.assertEqual(p.store["pv_track_date"], "2026-09-06")

    def test_hourly_recorder_writes_one_row_per_hour(self):
        p = _mk(self.tmp)
        self._tick(p, T10, 10.0)
        self._tick(p, T10 + 600, 10.5)
        path = os.path.join(self.tmp, "intraday_pv_tracking.json")
        rows = json.load(open(path, encoding="utf-8"))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["date"], D1)
        self.assertEqual(rows[0]["hour"], 10)


class TestPerDayUplifts(_Tmp):
    """v5.104.0: Saturday and Sunday are measured separately.

    The fixture gives them DIFFERENT values on purpose. The old one used a
    single weekend figure, which cannot tell a split model from a blended one —
    every assertion would have passed on either.
    """

    def _history(self, p, weekday_kwh, sat_kwh, sun_kwh, days=42,
                 partial_days=(), partial_kwh=5.0):
        rows = []
        today = datetime.strptime(D1, "%Y-%m-%d")
        for i in range(1, days + 1):
            d  = today - timedelta(days=i)
            ds = d.strftime("%Y-%m-%d")
            partial = ds in partial_days
            if partial:
                kwh = partial_kwh
            elif d.weekday() == 5:
                kwh = sat_kwh
            elif d.weekday() == 6:
                kwh = sun_kwh
            else:
                kwh = weekday_kwh
            rows.append({"date": ds, "home_kwh": kwh, "energy_partial": partial})
        with open(os.path.join(p.data_dir, "daily_history.json"), "w", encoding="utf-8") as fh:
            json.dump(rows, fh)

    def test_the_two_days_get_their_own_measured_ratio(self):
        p = _mk(self.tmp)
        # The real shape here: Saturday well above Sunday, Sunday just above a weekday.
        self._history(p, 21.0, 24.7, 22.1)
        with patch.object(plugin, "_local_today_str", return_value=D1), patch.object(plugin, "log"):
            sat, sun = p._measured_day_uplifts()
        self.assertAlmostEqual(sat, round(24.7 / 21.0, 3), places=3)
        self.assertAlmostEqual(sun, round(22.1 / 21.0, 3), places=3)
        self.assertGreater(sat, sun)

    def test_the_reported_bug_does_not_recur(self):
        """A blended figure split the real gap down the middle."""
        p = _mk(self.tmp)
        self._history(p, 21.0, 24.7, 22.1)
        with patch.object(plugin, "_local_today_str", return_value=D1), patch.object(plugin, "log"):
            sat, sun = p._measured_day_uplifts()
        blended = ((24.7 + 22.1) / 2.0) / 21.0
        wd, sat_s, sun_s = plugin._need_scales(sat, sun)
        _, blend_s, _    = plugin._need_scales(blended, blended)
        P = 21.5
        # The blended model over-states Sunday and under-states Saturday. Both
        # errors must shrink, and the direction of each is what is pinned.
        self.assertGreater(P * blend_s, P * sun_s)
        self.assertLess(P * blend_s,    P * sat_s)

    def test_each_day_is_clamped_on_its_own(self):
        p = _mk(self.tmp)
        # An absurd Saturday must not drag Sunday's figure with it.
        self._history(p, 10.0, 40.0, 10.5)
        with patch.object(plugin, "_local_today_str", return_value=D1), patch.object(plugin, "log"):
            sat, sun = p._measured_day_uplifts()
        self.assertEqual(sat, 1.5)
        self.assertEqual(sun, 1.05)

    def test_too_little_history_uses_each_default(self):
        p = _mk(self.tmp)
        self._history(p, 21.2, 24.7, 22.1, days=9)
        with patch.object(plugin, "_local_today_str", return_value=D1), patch.object(plugin, "log"):
            self.assertEqual(p._measured_day_uplifts(),
                             (plugin.SATURDAY_UPLIFT_DEFAULT, plugin.SUNDAY_UPLIFT_DEFAULT))
        q = _mk(self.tmp)
        os.remove(os.path.join(self.tmp, "daily_history.json"))
        with patch.object(plugin, "_local_today_str", return_value=D1), patch.object(plugin, "log"):
            self.assertEqual(q._measured_day_uplifts(),
                             (plugin.SATURDAY_UPLIFT_DEFAULT, plugin.SUNDAY_UPLIFT_DEFAULT))

    def test_partial_days_are_left_out_and_the_value_is_cached_per_day(self):
        p = _mk(self.tmp)
        base = datetime.strptime(D1, "%Y-%m-%d")
        partial = [(base - timedelta(days=i)).strftime("%Y-%m-%d")
                   for i in range(1, 43) if (base - timedelta(days=i)).weekday() == 5]
        # Every Saturday is partial at 5 kWh. Left out, Saturday has too few real
        # days and falls back to its default while Sunday still measures 22/20.
        # Counted, Saturday would clamp at 0.9 — so the exclusion is what is pinned.
        self._history(p, 20.0, 24.0, 22.0, partial_days=partial)
        with patch.object(plugin, "_local_today_str", return_value=D1), patch.object(plugin, "log") as lg:
            sat, sun = p._measured_day_uplifts()
            self.assertEqual(sat, plugin.SATURDAY_UPLIFT_DEFAULT)
            self.assertAlmostEqual(sun, 1.10, places=3)
            self.assertEqual(lg.call_count, 1)
            self._history(p, 20.0, 20.0, 20.0)            # file changes, cache does not
            self.assertEqual(p._measured_day_uplifts(), (sat, sun))
            self.assertEqual(lg.call_count, 1)

    def test_need_scales_keep_the_week_averaging_the_profile(self):
        wd, sat, sun = plugin._need_scales(1.18, 1.05)
        self.assertAlmostEqual((5 * wd + sat + sun) / 7.0, 1.0, places=3)
        self.assertAlmostEqual(sat / wd, 1.18, places=3)
        self.assertAlmostEqual(sun / wd, 1.05, places=3)

    def test_equal_uplifts_reproduce_the_old_two_bucket_answer_exactly(self):
        # The v5.90.0 algebra was wd = 7 / (5 + 2u). Passing one figure twice
        # must still give it, or the upgrade silently moves everyone's numbers.
        for u in (1.0, 1.10, 1.30):
            wd, sat, sun = plugin._need_scales(u, u)
            self.assertAlmostEqual(wd, 7.0 / (5.0 + 2.0 * u), places=4)
            self.assertEqual(sat, sun)
        self.assertEqual(plugin._need_scales(1.0, 1.0), (1.0, 1.0, 1.0))
        self.assertEqual(plugin._need_scales("garbage", "garbage"), (1.0, 1.0, 1.0))


class TestSiteConfigPublishesTheManagersFigures(_Tmp):
    """v5.90.1: sigen_site_config.json must carry the SAME need figures the manager
    uses, or the optimiser's evening message quotes a number 0.9 kWh off the
    plugin's own in the same sentence (05-Sep-2026). v5.104.0: three of them."""

    def _write(self, p):
        written = {}
        with patch.object(plugin, "_local_today_str", return_value=D1), \
             patch.object(plugin, "_atomic_write_json",
                          lambda path, data: written.update(path=path, data=data)):
            p._write_site_config()
        return written["data"]["consumption"]

    def _plugin(self):
        p = _mk(self.tmp)
        p.pluginVersion = "test"
        p.latest_rates_data = {}
        p._dawn_target_pct = lambda: 15.0
        p.store["consumption_profile"] = [0.5] * 48                  # 24 kWh blended
        p.store["day_uplifts"]     = [1.18, 1.05]
        p.store["day_uplift_date"] = D1
        return p

    def test_profile_is_split_with_the_same_scales_the_manager_uses(self):
        cons = self._write(self._plugin())
        wd, sat, sun = plugin._need_scales(1.18, 1.05)
        self.assertAlmostEqual(cons["daily_kwh_weekday"],  round(24.0 * wd,  2), places=2)
        self.assertAlmostEqual(cons["daily_kwh_saturday"], round(24.0 * sat, 2), places=2)
        self.assertAlmostEqual(cons["daily_kwh_sunday"],   round(24.0 * sun, 2), places=2)
        self.assertEqual(cons["saturday_multiplier"], 1.18)
        self.assertEqual(cons["sunday_multiplier"],   1.05)
        for key, scale in (("weekday", wd), ("saturday", sat), ("sunday", sun)):
            self.assertAlmostEqual(sum(cons["hourly_kwh"][key].values()), 24.0 * scale, places=1)
        # the week still averages the profile the figures came from
        self.assertAlmostEqual(
            (5 * cons["daily_kwh_weekday"] + cons["daily_kwh_saturday"]
             + cons["daily_kwh_sunday"]) / 7.0, 24.0, places=1)

    def test_saturday_and_sunday_are_actually_different(self):
        cons = self._write(self._plugin())
        self.assertGreater(cons["daily_kwh_saturday"], cons["daily_kwh_sunday"])
        self.assertNotEqual(cons["hourly_kwh"]["saturday"], cons["hourly_kwh"]["sunday"])

    def test_the_blended_weekend_key_is_kept_for_an_older_optimiser(self):
        # openmeteo_battery_optimiser.py before v3.22 asks for 'weekend' by name,
        # and a config file missing the key it reads sends it to the Octopus
        # grid-only fallback, which under-counts a solar house by about half.
        cons = self._write(self._plugin())
        self.assertIn("weekend", cons["hourly_kwh"])
        self.assertAlmostEqual(cons["daily_kwh_weekend"],
                               (cons["daily_kwh_saturday"] + cons["daily_kwh_sunday"]) / 2.0,
                               places=1)
        self.assertAlmostEqual(sum(cons["hourly_kwh"]["weekend"].values()),
                               cons["daily_kwh_weekend"], places=1)

    def test_the_window_length_is_published_so_a_reader_knows_the_basis(self):
        self.assertEqual(self._write(self._plugin())["window_days"],
                         plugin.PROFILE_WINDOW_DAYS)


if __name__ == "__main__":
    unittest.main()
