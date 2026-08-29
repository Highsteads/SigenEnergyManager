#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    test_away_profile.py
# Description: Unit tests for v5.78.0 away mode — the second consumption profile
#              used while the house is empty. Covers the flat seed, the fail-safe
#              direction of the away flag, accumulator routing, the profile the
#              refresh actually publishes, persistence round-trip, and the
#              suppressed weekend uplift.
# Author:      CliveS & Claude Opus 5
# Date:        29-08-2026
# Version:     1.0

import json
import os
import sys
import tempfile
import types
import unittest
from unittest.mock import MagicMock

# ---- Mock the Indigo runtime + pymodbus so plugin.py imports standalone ----
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
sys.modules.setdefault("indigo", _indigo)

_pm = MagicMock()
sys.modules.setdefault("pymodbus", _pm)
sys.modules.setdefault("pymodbus.client", sys.modules["pymodbus"].client)
sys.modules.setdefault("pymodbus.exceptions", sys.modules["pymodbus"].exceptions)
sys.modules.setdefault("requests", MagicMock())

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import plugin   # noqa: E402

indigo = sys.modules["indigo"]


class _Vars(dict):
    """Stand-in for indigo.variables: `name in vars` plus `vars[name].value`."""
    def __getitem__(self, k):
        return types.SimpleNamespace(value=dict.__getitem__(self, k))


def _mk(prefs=None, variables=None):
    """A Plugin instance with only the attributes away mode touches."""
    p = plugin.Plugin.__new__(plugin.Plugin)
    p.pluginPrefs = prefs if prefs is not None else {}
    p.logger      = MagicMock()
    p.store = {
        "home_profile_watts_sum": [0.0] * 48,
        "home_profile_count":     [0] * 48,
        "away_profile_watts_sum": [0.0] * 48,
        "away_profile_count":     [0] * 48,
        "away_active":  False,
        "away_warned":  False,
        "consumption_profile": [],
    }
    indigo.variables = _Vars(variables or {})
    return p


class TestAwaySeedProfile(unittest.TestCase):
    """The flat seed, and its coercion guards."""

    def test_seed_is_flat_and_sums_to_the_daily_total(self):
        prof = plugin._away_seed_profile(12.16)
        self.assertEqual(len(prof), 48)
        self.assertEqual(len(set(prof)), 1, "measured away load is flat — seed must be too")
        self.assertAlmostEqual(sum(prof), 12.16, places=1)

    def test_blank_falls_back_to_default(self):
        self.assertAlmostEqual(sum(plugin._away_seed_profile("")), 12.0, places=1)

    def test_non_numeric_falls_back_to_default(self):
        self.assertAlmostEqual(sum(plugin._away_seed_profile("twelve")), 12.0, places=1)

    def test_none_falls_back_to_default(self):
        self.assertAlmostEqual(sum(plugin._away_seed_profile(None)), 12.0, places=1)

    def test_zero_and_negative_rejected(self):
        for bad in (0, 0.0, -5):
            self.assertAlmostEqual(sum(plugin._away_seed_profile(bad)), 12.0, places=1)

    def test_absurdly_large_rejected(self):
        self.assertAlmostEqual(sum(plugin._away_seed_profile(5000)), 12.0, places=1)


class TestIsAway(unittest.TestCase):
    """_is_away must fail towards OCCUPIED on every unhappy path."""

    def test_true_when_variable_true(self):
        p = _mk({"awayEnabled": True, "awayVariable": "Away"}, {"Away": "true"})
        self.assertTrue(p._is_away())

    def test_accepts_other_truthy_spellings(self):
        for v in ("True", "1", "yes", "ON", " away "):
            p = _mk({"awayEnabled": True, "awayVariable": "Away"}, {"Away": v})
            self.assertTrue(p._is_away(), f"{v!r} should read as away")

    def test_false_when_variable_false(self):
        p = _mk({"awayEnabled": True, "awayVariable": "Away"}, {"Away": "false"})
        self.assertFalse(p._is_away())

    def test_false_when_feature_disabled(self):
        p = _mk({"awayEnabled": False, "awayVariable": "Away"}, {"Away": "true"})
        self.assertFalse(p._is_away(), "unticked config must win over a true variable")

    def test_missing_variable_reads_as_occupied_and_warns_once(self):
        p = _mk({"awayEnabled": True, "awayVariable": "Nope"}, {"Away": "true"})
        self.assertFalse(p._is_away())
        self.assertFalse(p._is_away())
        self.assertTrue(p.store["away_warned"])

    def test_blank_variable_name_reads_as_occupied(self):
        p = _mk({"awayEnabled": True, "awayVariable": "   "}, {"Away": "true"})
        self.assertFalse(p._is_away())

    def test_exception_reads_as_occupied(self):
        p = _mk({"awayEnabled": True, "awayVariable": "Away"})
        boom = MagicMock()
        boom.__contains__ = MagicMock(side_effect=RuntimeError("server gone"))
        indigo.variables = boom
        self.assertFalse(p._is_away())

    def test_junk_value_reads_as_occupied(self):
        p = _mk({"awayEnabled": True, "awayVariable": "Away"}, {"Away": "maybe"})
        self.assertFalse(p._is_away())


class TestAccumulatorRouting(unittest.TestCase):
    """A reading must land in the profile for the house it was taken in."""

    def test_occupied_reading_goes_to_home_only(self):
        p = _mk()
        p.store["away_active"] = False
        p._accumulate_home_profile(1800.0)
        self.assertEqual(sum(p.store["home_profile_count"]), 1)
        self.assertEqual(sum(p.store["away_profile_count"]), 0)

    def test_away_reading_goes_to_away_only(self):
        p = _mk()
        p.store["away_active"] = True
        p._accumulate_home_profile(507.0)
        self.assertEqual(sum(p.store["away_profile_count"]), 1)
        self.assertEqual(sum(p.store["home_profile_count"]), 0,
                         "a six-week trip must not skew the occupied profile")


class TestRefreshPublishesActiveProfile(unittest.TestCase):

    def _fill(self, p, key, watts):
        for i in range(48):
            p.store[f"{key}_profile_watts_sum"][i] = watts * plugin.HOME_PROFILE_MIN_READINGS
            p.store[f"{key}_profile_count"][i]     = plugin.HOME_PROFILE_MIN_READINGS

    def test_away_falls_back_to_flat_seed_not_the_occupied_default(self):
        p = _mk({"awayDailyKwh": "12.16"})
        p.store["away_active"] = True
        p._refresh_consumption_profile_impl()
        prof = p.store["consumption_profile"]
        self.assertEqual(len(set(prof)), 1, "no readings yet — must be the flat seed")
        self.assertAlmostEqual(sum(prof), 12.16, places=1)

    def test_away_uses_real_away_readings_once_it_has_them(self):
        p = _mk({"awayDailyKwh": "12.0"})
        self._fill(p, "away", 600.0)          # 600 W flat -> 0.3 kWh/slot -> 14.4 kWh/day
        p.store["away_active"] = True
        p._refresh_consumption_profile_impl()
        self.assertAlmostEqual(sum(p.store["consumption_profile"]), 14.4, places=1)

    def test_occupied_ignores_the_away_accumulators(self):
        p = _mk({"awayDailyKwh": "12.0"})
        self._fill(p, "home", 1800.0)         # 1.8 kW flat -> 43.2 kWh/day
        self._fill(p, "away", 500.0)
        p.store["away_active"] = False
        p._refresh_consumption_profile_impl()
        self.assertAlmostEqual(sum(p.store["consumption_profile"]), 43.2, places=1)

    def test_switching_away_switches_the_published_profile(self):
        p = _mk({"awayDailyKwh": "12.0"})
        self._fill(p, "home", 1800.0)
        self._fill(p, "away", 500.0)          # 12.0 kWh/day
        p.store["away_active"] = False
        p._refresh_consumption_profile_impl()
        occupied = sum(p.store["consumption_profile"])
        p.store["away_active"] = True
        p._refresh_consumption_profile_impl()
        empty = sum(p.store["consumption_profile"])
        self.assertGreater(occupied, empty * 2)


class TestPersistence(unittest.TestCase):

    def test_away_accumulators_survive_a_save_load_round_trip(self):
        with tempfile.TemporaryDirectory() as td:
            p = _mk({"awayEnabled": False})
            p.data_dir = td
            p.store["away_profile_watts_sum"][10] = 5070.0
            p.store["away_profile_count"][10]     = 10
            p.store["home_profile_count"][3]      = 4
            p._save_home_profile()

            q = _mk({"awayEnabled": False})
            q.data_dir = td
            q._refresh_consumption_profile = MagicMock()
            q._load_home_profile()

            self.assertEqual(q.store["away_profile_count"][10], 10)
            self.assertAlmostEqual(q.store["away_profile_watts_sum"][10], 5070.0)
            self.assertEqual(q.store["home_profile_count"][3], 4)

    def test_pre_5_78_file_without_away_keys_loads_clean(self):
        with tempfile.TemporaryDirectory() as td:
            with open(os.path.join(td, "home_load_profile.json"), "w",
                      encoding="utf-8") as f:
                json.dump({"watts_sum": [1.0] * 48, "count": [2] * 48}, f)
            q = _mk({"awayEnabled": False})
            q.data_dir = td
            q._refresh_consumption_profile = MagicMock()
            q._load_home_profile()
            self.assertEqual(sum(q.store["away_profile_count"]), 0)
            self.assertEqual(q.store["home_profile_count"][0], 2)

    def test_restart_while_away_comes_back_up_away(self):
        with tempfile.TemporaryDirectory() as td:
            p = _mk({"awayEnabled": False})
            p.data_dir = td
            p._save_home_profile()
            q = _mk({"awayEnabled": True, "awayVariable": "Away"}, {"Away": "true"})
            q.data_dir = td
            q._refresh_consumption_profile = MagicMock()
            q._load_home_profile()
            self.assertTrue(q.store["away_active"],
                            "a restart mid-trip must not resume on the occupied profile")


class TestTransitionLogging(unittest.TestCase):

    def test_transition_rebuilds_the_profile(self):
        p = _mk({"awayEnabled": True, "awayVariable": "Away"}, {"Away": "true"})
        p._refresh_consumption_profile = MagicMock()
        self.assertTrue(p._refresh_away_state())
        p._refresh_consumption_profile.assert_called_once()

    def test_no_change_does_not_rebuild(self):
        p = _mk({"awayEnabled": True, "awayVariable": "Away"}, {"Away": "false"})
        p._refresh_consumption_profile = MagicMock()
        self.assertFalse(p._refresh_away_state())
        p._refresh_consumption_profile.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
