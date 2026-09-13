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
    """v5.104.0 / v5.105.0: Monday, Saturday and Sunday are measured separately.

    Every day in the fixture gets a DIFFERENT value on purpose. A fixture that
    gives two buckets the same number cannot tell a split model from a blended
    one — every assertion would pass on either.
    """

    def _history(self, p, base_kwh, mon_kwh, sat_kwh, sun_kwh, days=120,
                 partial_days=(), partial_kwh=5.0):
        rows = []
        today = datetime.strptime(D1, "%Y-%m-%d")
        for i in range(1, days + 1):
            d  = today - timedelta(days=i)
            ds = d.strftime("%Y-%m-%d")
            partial = ds in partial_days
            if partial:
                kwh = partial_kwh
            elif d.weekday() == 0:
                kwh = mon_kwh
            elif d.weekday() == 5:
                kwh = sat_kwh
            elif d.weekday() == 6:
                kwh = sun_kwh
            else:
                kwh = base_kwh
            rows.append({"date": ds, "home_kwh": kwh, "energy_partial": partial})
        with open(os.path.join(p.data_dir, "daily_history.json"), "w", encoding="utf-8") as fh:
            json.dump(rows, fh)

    def test_each_day_gets_its_own_measured_ratio(self):
        p = _mk(self.tmp)
        # The real shape here: Saturday highest, Monday and Sunday close but
        # deliberately NOT equal, Tue-Fri the base.
        self._history(p, 20.8, 22.1, 24.7, 22.3)
        with patch.object(plugin, "_local_today_str", return_value=D1), patch.object(plugin, "log"):
            mon, sat, sun = p._measured_day_uplifts()
        self.assertAlmostEqual(mon, round(22.1 / 20.8, 3), places=3)
        self.assertAlmostEqual(sat, round(24.7 / 20.8, 3), places=3)
        self.assertAlmostEqual(sun, round(22.3 / 20.8, 3), places=3)

    def test_the_base_is_tuefri_and_excludes_the_split_days(self):
        """Measuring against a mean a day belongs to makes the ratios move each other."""
        p = _mk(self.tmp)
        self._history(p, 20.0, 30.0, 30.0, 30.0)   # every split day far above the base
        with patch.object(plugin, "_local_today_str", return_value=D1), patch.object(plugin, "log"):
            mon, sat, sun = p._measured_day_uplifts()
        # Against a Tue-Fri base of 20.0 that is 1.5 for each, the clamp ceiling.
        # Against a Mon-Fri base it would be 30/22 = 1.36, and against an all-week
        # base 30/24.3 = 1.24 — so the value proves which mean was used.
        for u in (mon, sat, sun):
            self.assertEqual(u, 1.5)

    def test_the_reported_bug_does_not_recur(self):
        """A blended figure split each real gap down the middle."""
        p = _mk(self.tmp)
        self._history(p, 20.76, 22.09, 24.72, 22.05)   # the measured 90-day shape
        with patch.object(plugin, "_local_today_str", return_value=D1), patch.object(plugin, "log"):
            mon, sat, sun = p._measured_day_uplifts()
        P = 21.5
        wd, mon_s, sat_s, sun_s = plugin._need_scales(mon, sat, sun)
        # Monday must sit ABOVE the Tue-Fri figure, and Saturday above Sunday.
        self.assertGreater(P * mon_s, P * wd)
        self.assertGreater(P * sat_s, P * sun_s)
        # and the old single weekday figure sat between Monday and Tue-Fri
        _, blend_s, _, _ = plugin._need_scales(1.0, sat, sun)
        self.assertLess(P * blend_s, P * mon_s)

    def test_each_day_is_clamped_on_its_own(self):
        p = _mk(self.tmp)
        # An absurd Saturday must not drag the other two with it.
        self._history(p, 10.0, 10.5, 40.0, 10.2)
        with patch.object(plugin, "_local_today_str", return_value=D1), patch.object(plugin, "log"):
            mon, sat, sun = p._measured_day_uplifts()
        self.assertEqual(sat, 1.5)
        self.assertEqual(mon, 1.05)
        self.assertEqual(sun, 1.02)

    def test_too_little_history_uses_each_default(self):
        p = _mk(self.tmp)
        self._history(p, 20.8, 22.1, 24.7, 22.3, days=9)
        with patch.object(plugin, "_local_today_str", return_value=D1), patch.object(plugin, "log"):
            self.assertEqual(p._measured_day_uplifts(),
                             (plugin.MONDAY_UPLIFT_DEFAULT,
                              plugin.SATURDAY_UPLIFT_DEFAULT,
                              plugin.SUNDAY_UPLIFT_DEFAULT))
        q = _mk(self.tmp)
        os.remove(os.path.join(self.tmp, "daily_history.json"))
        with patch.object(plugin, "_local_today_str", return_value=D1), patch.object(plugin, "log"):
            self.assertEqual(q._measured_day_uplifts(),
                             (plugin.MONDAY_UPLIFT_DEFAULT,
                              plugin.SATURDAY_UPLIFT_DEFAULT,
                              plugin.SUNDAY_UPLIFT_DEFAULT))

    def test_a_day_with_too_few_samples_falls_back_alone(self):
        """Splitting a day out costs sample size — a thin one must not guess."""
        p = _mk(self.tmp)
        base = datetime.strptime(D1, "%Y-%m-%d")
        # Drop all but three Mondays; Saturday and Sunday keep a full set.
        thin = [(base - timedelta(days=i)).strftime("%Y-%m-%d")
                for i in range(1, 121)
                if (base - timedelta(days=i)).weekday() == 0][3:]
        self._history(p, 20.8, 22.1, 24.7, 22.3, partial_days=thin)
        with patch.object(plugin, "_local_today_str", return_value=D1), patch.object(plugin, "log"):
            mon, sat, sun = p._measured_day_uplifts()
        self.assertEqual(mon, plugin.MONDAY_UPLIFT_DEFAULT)
        self.assertAlmostEqual(sat, round(24.7 / 20.8, 3), places=3)
        self.assertAlmostEqual(sun, round(22.3 / 20.8, 3), places=3)

    def test_partial_days_are_left_out_and_the_value_is_cached_per_day(self):
        p = _mk(self.tmp)
        base = datetime.strptime(D1, "%Y-%m-%d")
        partial = [(base - timedelta(days=i)).strftime("%Y-%m-%d")
                   for i in range(1, 121) if (base - timedelta(days=i)).weekday() == 5]
        # Every Saturday is partial at 5 kWh. Left out, Saturday has too few real
        # days and falls back to its default while the others still measure.
        # Counted, Saturday would clamp at 0.9 — so the exclusion is what is pinned.
        self._history(p, 20.0, 22.0, 24.0, 21.0, partial_days=partial)
        with patch.object(plugin, "_local_today_str", return_value=D1), patch.object(plugin, "log") as lg:
            mon, sat, sun = p._measured_day_uplifts()
            self.assertEqual(sat, plugin.SATURDAY_UPLIFT_DEFAULT)
            self.assertAlmostEqual(mon, 1.10, places=3)
            self.assertAlmostEqual(sun, 1.05, places=3)
            self.assertEqual(lg.call_count, 1)
            self._history(p, 20.0, 20.0, 20.0, 20.0)      # file changes, cache does not
            self.assertEqual(p._measured_day_uplifts(), (mon, sat, sun))
            self.assertEqual(lg.call_count, 1)

    def test_the_uplift_window_is_wider_than_the_profile_window(self):
        # Splitting days out costs sample size; the ratio is stable enough to
        # afford a longer window, and the level is not.
        self.assertGreater(plugin.DAY_UPLIFT_WINDOW_DAYS, plugin.PROFILE_WINDOW_DAYS)
        self.assertEqual(plugin.DAY_UPLIFT_WINDOW_DAYS % 7, 0)

    def test_need_scales_keep_the_week_averaging_the_profile(self):
        wd, mon, sat, sun = plugin._need_scales(1.06, 1.18, 1.05)
        self.assertAlmostEqual((4 * wd + mon + sat + sun) / 7.0, 1.0, places=3)
        self.assertAlmostEqual(mon / wd, 1.06, places=3)
        self.assertAlmostEqual(sat / wd, 1.18, places=3)
        self.assertAlmostEqual(sun / wd, 1.05, places=3)

    def test_flat_uplifts_reproduce_the_flat_answer_exactly(self):
        self.assertEqual(plugin._need_scales(1.0, 1.0, 1.0), (1.0, 1.0, 1.0, 1.0))
        self.assertEqual(plugin._need_scales("x", "y", "z"), (1.0, 1.0, 1.0, 1.0))

    def test_pulling_monday_out_cannot_move_saturday(self):
        """The point of a reference bucket that is in none of the ratios."""
        p = _mk(self.tmp)
        self._history(p, 20.8, 22.1, 24.7, 22.3)
        with patch.object(plugin, "_local_today_str", return_value=D1), patch.object(plugin, "log"):
            _, sat_a, sun_a = p._measured_day_uplifts()
        q = _mk(self.tmp)
        # A wildly different Monday, everything else untouched.
        self._history(q, 20.8, 28.0, 24.7, 22.3)
        with patch.object(plugin, "_local_today_str", return_value=D1), patch.object(plugin, "log"):
            _, sat_b, sun_b = q._measured_day_uplifts()
        self.assertEqual((sat_a, sun_a), (sat_b, sun_b))


class TestSiteConfigPublishesTheManagersFigures(_Tmp):
    """v5.90.1: sigen_site_config.json must carry the SAME need figures the manager
    uses, or the optimiser's evening message quotes a number 0.9 kWh off the
    plugin's own in the same sentence (05-Sep-2026). v5.105.0: four of them."""

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
        p.store["day_uplifts"]     = [1.06, 1.18, 1.05]
        p.store["day_uplift_date"] = D1
        return p

    def test_profile_is_split_with_the_same_scales_the_manager_uses(self):
        cons = self._write(self._plugin())
        wd, mon, sat, sun = plugin._need_scales(1.06, 1.18, 1.05)
        for key, scale in (("tuefri", wd), ("monday", mon),
                           ("saturday", sat), ("sunday", sun)):
            self.assertAlmostEqual(cons[f"daily_kwh_{key}"], round(24.0 * scale, 2), places=2)
            self.assertAlmostEqual(sum(cons["hourly_kwh"][key].values()), 24.0 * scale, places=1)
        self.assertEqual(cons["monday_multiplier"],   1.06)
        self.assertEqual(cons["saturday_multiplier"], 1.18)
        self.assertEqual(cons["sunday_multiplier"],   1.05)
        # the week still averages the profile the figures came from
        self.assertAlmostEqual(
            (4 * cons["daily_kwh_tuefri"] + cons["daily_kwh_monday"]
             + cons["daily_kwh_saturday"] + cons["daily_kwh_sunday"]) / 7.0, 24.0, places=1)

    def test_the_four_buckets_are_actually_different(self):
        cons = self._write(self._plugin())
        vals = [cons[f"daily_kwh_{k}"] for k in ("tuefri", "monday", "saturday", "sunday")]
        self.assertEqual(len(set(vals)), 4, f"buckets are not distinct: {vals}")
        self.assertGreater(cons["daily_kwh_saturday"], cons["daily_kwh_sunday"])
        self.assertGreater(cons["daily_kwh_monday"],   cons["daily_kwh_tuefri"])
        h = cons["hourly_kwh"]
        self.assertNotEqual(h["monday"], h["tuefri"])
        self.assertNotEqual(h["saturday"], h["sunday"])

    def test_the_legacy_keys_are_kept_as_UNBIASED_blends(self):
        """An older optimiser asks for weekday/weekend by name.

        A config file missing the key it reads sends it to the Octopus grid-only
        fallback, which under-counts a solar house by about half. Publishing the
        blends rather than one of the buckets also keeps such a reader correct
        over the week instead of merely working.
        """
        cons = self._write(self._plugin())
        self.assertIn("weekday", cons["hourly_kwh"])
        self.assertIn("weekend", cons["hourly_kwh"])
        self.assertAlmostEqual(cons["daily_kwh_weekday"],
                               (cons["daily_kwh_monday"] + 4 * cons["daily_kwh_tuefri"]) / 5.0,
                               places=1)
        self.assertAlmostEqual(cons["daily_kwh_weekend"],
                               (cons["daily_kwh_saturday"] + cons["daily_kwh_sunday"]) / 2.0,
                               places=1)
        for key in ("weekday", "weekend"):
            self.assertAlmostEqual(sum(cons["hourly_kwh"][key].values()),
                                   cons[f"daily_kwh_{key}"], places=1)
        # and an old reader taking 5 weekdays + 2 weekend days still gets the week
        self.assertAlmostEqual(
            (5 * cons["daily_kwh_weekday"] + 2 * cons["daily_kwh_weekend"]) / 7.0,
            24.0, places=1)

    def test_both_window_lengths_are_published_so_a_reader_knows_the_basis(self):
        cons = self._write(self._plugin())
        self.assertEqual(cons["window_days"], plugin.PROFILE_WINDOW_DAYS)
        self.assertEqual(cons["uplift_window_days"], plugin.DAY_UPLIFT_WINDOW_DAYS)


if __name__ == "__main__":
    unittest.main()
