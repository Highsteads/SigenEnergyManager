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


class TestWeekendUplift(_Tmp):

    def _history(self, p, weekday_kwh, weekend_kwh, days=42, partial_days=(), partial_kwh=5.0):
        rows = []
        today = datetime.strptime(D1, "%Y-%m-%d")
        for i in range(1, days + 1):
            d = today - timedelta(days=i)
            ds = d.strftime("%Y-%m-%d")
            partial = ds in partial_days
            rows.append({"date": ds,
                         "home_kwh": partial_kwh if partial else
                                     (weekend_kwh if d.weekday() >= 5 else weekday_kwh),
                         "energy_partial": partial})
        with open(os.path.join(p.data_dir, "daily_history.json"), "w", encoding="utf-8") as fh:
            json.dump(rows, fh)

    def test_uplift_is_the_measured_ratio(self):
        p = _mk(self.tmp)
        self._history(p, 21.2, 23.4)
        with patch.object(plugin, "_local_today_str", return_value=D1), patch.object(plugin, "log"):
            self.assertAlmostEqual(p._measured_weekend_uplift(), 23.4 / 21.2, places=3)

    def test_uplift_is_clamped(self):
        p = _mk(self.tmp)
        self._history(p, 10.0, 40.0)
        with patch.object(plugin, "_local_today_str", return_value=D1), patch.object(plugin, "log"):
            self.assertEqual(p._measured_weekend_uplift(), 1.5)

    def test_too_little_history_uses_the_default(self):
        p = _mk(self.tmp)
        self._history(p, 21.2, 23.4, days=9)
        with patch.object(plugin, "_local_today_str", return_value=D1), patch.object(plugin, "log"):
            self.assertEqual(p._measured_weekend_uplift(), plugin.WEEKEND_UPLIFT_DEFAULT)
        q = _mk(self.tmp)
        os.remove(os.path.join(self.tmp, "daily_history.json"))
        with patch.object(plugin, "_local_today_str", return_value=D1), patch.object(plugin, "log"):
            self.assertEqual(q._measured_weekend_uplift(), plugin.WEEKEND_UPLIFT_DEFAULT)

    def test_partial_days_are_left_out_and_the_value_is_cached_per_day(self):
        p = _mk(self.tmp)
        partial = [(datetime.strptime(D1, "%Y-%m-%d") - timedelta(days=i)).strftime("%Y-%m-%d")
                   for i in range(1, 43) if (datetime.strptime(D1, "%Y-%m-%d") - timedelta(days=i)).weekday() == 5]
        # Every Saturday is a partial day recorded at 5 kWh. Left out, the
        # Sundays give 22/20 = 1.10; counted, the weekend mean collapses to 13.5
        # and the answer would clamp at 0.9 — so the exclusion is what is pinned.
        self._history(p, 20.0, 22.0, partial_days=partial)
        with patch.object(plugin, "_local_today_str", return_value=D1), patch.object(plugin, "log") as lg:
            u = p._measured_weekend_uplift()
            self.assertAlmostEqual(u, 1.10, places=3)
            self.assertEqual(lg.call_count, 1)
            self._history(p, 20.0, 20.0)                          # file changes, cache does not
            self.assertEqual(p._measured_weekend_uplift(), u)
            self.assertEqual(lg.call_count, 1)

    def test_need_scales_keep_the_week_averaging_the_profile(self):
        wd, we = plugin._need_scales(1.10)
        self.assertAlmostEqual((5 * wd + 2 * we) / 7.0, 1.0, places=3)
        self.assertAlmostEqual(we / wd, 1.10, places=3)
        self.assertEqual(plugin._need_scales(1.0), (1.0, 1.0))
        self.assertEqual(plugin._need_scales("garbage"), (1.0, 1.0))


if __name__ == "__main__":
    unittest.main()
