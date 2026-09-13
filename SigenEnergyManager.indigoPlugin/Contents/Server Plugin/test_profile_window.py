#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    test_profile_window.py
# Description: Contract tests for the rolling consumption window (v5.104.0) and
#              the three-way day model that replaced weekday/weekend.
#              Pins the 13-Sep-2026 audit: the lifetime accumulator could not
#              respond to a change in consumption, and Saturday and Sunday were
#              being averaged into one figure 2.6 kWh wide.
# Author:      CliveS & Claude Opus 5
# Date:        13-09-2026 22:30
# Version:     1.0

import json
import os
import sys
import tempfile
import threading
import types
import unittest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

# Same stub the sibling plugin suites use — plugin.py imports indigo at module
# level and there is none on a test runner.
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

import plugin                                      # noqa: E402
from battery_manager import need_for_weekday, ManagerSnapshot   # noqa: E402


def _mk(tmp):
    """A Plugin with a real store, no Indigo and no hardware."""
    p = plugin.Plugin.__new__(plugin.Plugin)
    p.data_dir    = tmp
    p.logger      = MagicMock()
    p.store       = {}
    p.pluginPrefs = {}              # the away seed reads awayDailyKwh from it
    p._state_lock = threading.RLock()   # _refresh_consumption_profile takes it
    # The store is built inside __init__, which needs a live Indigo, so these
    # keys are set by hand — deliberately the SAME names plugin.py uses, and
    # TestTheStoreKeysExist below fails if that stops being true.
    p.store.setdefault("home_profile_watts_sum", [0.0] * 48)
    p.store.setdefault("home_profile_count",     [0] * 48)
    p.store.setdefault("away_profile_watts_sum", [0.0] * 48)
    p.store.setdefault("away_profile_count",     [0] * 48)
    p.store.setdefault("home_profile_days",      {})
    p.store.setdefault("away_active",            False)
    p.store.setdefault("consumption_profile",    [])
    return p


class TestTheStoreKeysExist(unittest.TestCase):
    """The fixture above builds the store by hand, so prove the real one agrees.

    Without this the whole file could be testing keys plugin.py no longer sets.
    """

    def test_plugin_py_sets_every_key_these_tests_use(self):
        src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "plugin.py"), encoding="utf-8").read()
        for key in ("home_profile_watts_sum", "home_profile_count",
                    "home_profile_days", "away_profile_watts_sum"):
            self.assertIn(f'self.store["{key}"]', src, f"{key} is not set in plugin.py")


class TestDayMapping(unittest.TestCase):
    """One owner for "which figure belongs to which day"."""

    def _snap(self):
        return ManagerSnapshot(current_soc_pct=50.0, weekday_kwh=20.8,
                               saturday_kwh=24.7, sunday_kwh=22.1)

    def test_every_day_gets_its_own_figure(self):
        s = self._snap()
        for idx in range(5):
            self.assertEqual(need_for_weekday(s, idx), 20.8, f"weekday index {idx}")
        self.assertEqual(need_for_weekday(s, 5), 24.7)
        self.assertEqual(need_for_weekday(s, 6), 22.1)

    def test_the_reported_bug_does_not_recur(self):
        # Saturday and Sunday must not be the same number. A blended model
        # returns one figure for both, and that is what this pins out.
        s = self._snap()
        self.assertNotEqual(need_for_weekday(s, 5), need_for_weekday(s, 6))


class TestRollingWindow(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def _feed(self, p, when, watts, slot_hour=12):
        """One reading at a given moment."""
        with patch.object(plugin, "datetime") as dt:
            dt.now.return_value = when.replace(hour=slot_hour, minute=0)
            dt.strptime = datetime.strptime
            p._accumulate_home_profile(watts)

    def _feed_day(self, p, day, watts, readings=20):
        for i in range(readings):
            self._feed(p, day, watts)

    def test_readings_are_filed_under_their_own_day(self):
        p = _mk(self.tmp)
        base = datetime(2026, 9, 13)
        self._feed_day(p, base, 800.0)
        self._feed_day(p, base - timedelta(days=1), 900.0)
        self.assertEqual(sorted(p.store["home_profile_days"]),
                         ["2026-09-12", "2026-09-13"])

    def test_days_outside_the_window_are_dropped(self):
        p = _mk(self.tmp)
        base = datetime(2026, 9, 13)
        # One day well inside the window, one well outside.
        p.store["home_profile_days"] = {
            "2026-09-12": {"sum": [1.0] * 48, "count": [1] * 48},
            "2026-01-01": {"sum": [1.0] * 48, "count": [1] * 48},
        }
        with patch.object(plugin, "datetime") as dt:
            dt.now.return_value = base
            dt.strptime = datetime.strptime
            p._prune_profile_days()
        self.assertIn("2026-09-12", p.store["home_profile_days"])
        self.assertNotIn("2026-01-01", p.store["home_profile_days"])

    def test_the_window_is_a_whole_number_of_weeks(self):
        # 63 not 60, so the window holds the same count of every weekday and the
        # blended profile carries no day-of-week composition bias.
        self.assertEqual(plugin.PROFILE_WINDOW_DAYS % 7, 0)

    def test_a_slot_needs_enough_DAYS_not_enough_readings(self):
        """The guard that makes the window safe on the day it ships.

        At a 10 s poll one day alone puts ~147 readings in every slot, so a
        reading-count threshold would let yesterday speak for the whole window.
        """
        p = _mk(self.tmp)
        # One day, an enormous number of readings, all saying 3000 W.
        p.store["home_profile_days"] = {
            "2026-09-13": {"sum": [3000.0 * 200] * 48, "count": [200] * 48},
        }
        # The lifetime accumulator says 800 W.
        p.store["home_profile_watts_sum"] = [800.0 * 5000] * 48
        p.store["home_profile_count"]     = [5000] * 48
        with patch.object(plugin, "log"):
            p._refresh_consumption_profile_impl()
        # 800 W over a half hour is 0.4 kWh; 3000 W would be 1.5.
        self.assertAlmostEqual(p.store["consumption_profile"][0], 0.4, places=3)

    def test_the_window_takes_over_once_it_has_enough_days(self):
        p = _mk(self.tmp)
        days = {}
        for i in range(plugin.PROFILE_MIN_WINDOW_DAYS):
            d = (datetime(2026, 9, 13) - timedelta(days=i)).strftime("%Y-%m-%d")
            days[d] = {"sum": [3000.0 * 10] * 48, "count": [10] * 48}
        p.store["home_profile_days"]      = days
        p.store["home_profile_watts_sum"] = [800.0 * 5000] * 48
        p.store["home_profile_count"]     = [5000] * 48
        with patch.object(plugin, "log"):
            p._refresh_consumption_profile_impl()
        self.assertAlmostEqual(p.store["consumption_profile"][0], 1.5, places=3)

    def test_the_lifetime_mean_carries_the_plan_until_then(self):
        """No discontinuity on upgrade: one day short, the old figure still serves."""
        p = _mk(self.tmp)
        days = {}
        for i in range(plugin.PROFILE_MIN_WINDOW_DAYS - 1):
            d = (datetime(2026, 9, 13) - timedelta(days=i)).strftime("%Y-%m-%d")
            days[d] = {"sum": [3000.0 * 10] * 48, "count": [10] * 48}
        p.store["home_profile_days"]      = days
        p.store["home_profile_watts_sum"] = [800.0 * 5000] * 48
        p.store["home_profile_count"]     = [5000] * 48
        with patch.object(plugin, "log"):
            p._refresh_consumption_profile_impl()
        self.assertAlmostEqual(p.store["consumption_profile"][0], 0.4, places=3)

    def test_an_away_day_is_not_filed_in_the_window(self):
        p = _mk(self.tmp)
        p.store["away_active"] = True
        self._feed_day(p, datetime(2026, 9, 13), 500.0)
        self.assertEqual(p.store["home_profile_days"], {})
        self.assertGreater(sum(p.store["away_profile_count"]), 0)

    def test_the_away_profile_still_uses_its_lifetime_accumulator(self):
        p = _mk(self.tmp)
        p.store["away_active"] = True
        p.store["away_profile_watts_sum"] = [507.0 * 100] * 48
        p.store["away_profile_count"]     = [100] * 48
        # A full window of occupied days must not reach the away answer.
        p.store["home_profile_days"] = {
            (datetime(2026, 9, 13) - timedelta(days=i)).strftime("%Y-%m-%d"):
                {"sum": [3000.0 * 10] * 48, "count": [10] * 48}
            for i in range(plugin.PROFILE_WINDOW_DAYS)
        }
        with patch.object(plugin, "log"):
            p._refresh_consumption_profile_impl()
        self.assertAlmostEqual(p.store["consumption_profile"][0], 0.2535, places=3)


class TestWindowPersistence(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_the_window_survives_a_restart(self):
        p = _mk(self.tmp)
        p.store["home_profile_watts_sum"] = [800.0 * 10] * 48
        p.store["home_profile_count"]     = [10] * 48
        p.store["home_profile_days"] = {
            (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d"):
                {"sum": [900.0 * 5] * 48, "count": [5] * 48}
            for i in range(3)
        }
        p._save_home_profile()

        q = _mk(self.tmp)
        q._is_away = lambda: False
        with patch.object(plugin, "log"):
            q._load_home_profile()
        self.assertEqual(len(q.store["home_profile_days"]), 3)
        for bucket in q.store["home_profile_days"].values():
            self.assertEqual(len(bucket["sum"]), 48)
            self.assertEqual(len(bucket["count"]), 48)

    def test_a_file_from_an_older_version_loads_with_an_empty_window(self):
        """No migration: <= 5.103.x wrote no 'days' key at all."""
        path = os.path.join(self.tmp, "home_load_profile.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"watts_sum": [800.0 * 10] * 48, "count": [10] * 48}, fh)
        q = _mk(self.tmp)
        q._is_away = lambda: False
        with patch.object(plugin, "log"):
            q._load_home_profile()
        self.assertEqual(q.store["home_profile_days"], {})
        # and the lifetime accumulator still answers, so the plan does not jump
        self.assertAlmostEqual(q.store["consumption_profile"][0], 0.4, places=3)

    def test_a_corrupt_day_bucket_is_skipped_not_fatal(self):
        path = os.path.join(self.tmp, "home_load_profile.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({
                "watts_sum": [800.0 * 10] * 48, "count": [10] * 48,
                "days": {
                    "2026-09-13": {"sum": [1.0] * 48, "count": [1] * 48},   # good
                    "2026-09-12": {"sum": [1.0] * 7,  "count": [1] * 7},    # wrong length
                    "2026-09-11": {"sum": ["x"] * 48, "count": [1] * 48},   # not numbers
                },
            }, fh)
        q = _mk(self.tmp)
        q._is_away = lambda: False
        with patch.object(plugin, "log"):
            q._load_home_profile()
        self.assertEqual(list(q.store["home_profile_days"]), ["2026-09-13"])


if __name__ == "__main__":
    unittest.main()
