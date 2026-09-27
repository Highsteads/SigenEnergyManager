#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    test_sunday_shape.py
# Description: Contract tests for the Sunday half-hourly shape (5.118.0): the pure
#              measurement, the Flux planner's day-aware profile, the manager's
#              day-aware estimates, and the plugin wiring that hands the shape to
#              all three. CliveS, 27-Sep-2026: the roast, both microwaves and the
#              wash put a Sunday's load in the afternoon, and the blended curve
#              spread it across the day.
# Author:      CliveS & Claude Opus 5.5
# Date:        27-09-2026 11:05 BST
# Version:     1.0

import ast
import json
import os
import sqlite3
import sys
import tempfile
import threading
import types
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

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
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import sunday_shape as ss                                   # noqa: E402
import flux_strategy as fs                                  # noqa: E402
import plugin                                               # noqa: E402
from battery_manager import (BatteryManager, ManagerSnapshot,  # noqa: E402
                             profile_for_weekday)

LONDON = ZoneInfo("Europe/London")
FLAT   = [0.4] * 48                               # 19.2 kWh, evenly spread


def _day_rows(day, per_slot, phase_min=0):
    """Half-hourly rows for one day, 30 minutes each, starting `phase_min` late."""
    rows  = []
    start = datetime(day.year, day.month, day.day) + timedelta(minutes=phase_min)
    for i in range(48):
        a = start + timedelta(minutes=30 * i)
        b = a + timedelta(minutes=30)
        rows.append((a.isoformat(), b.isoformat(), per_slot[i]))
    return rows


def _roast_day(extra=1.0):
    """0.4 kWh a half-hour, with `extra` added across 2pm-4pm."""
    return [0.4 + (extra if 28 <= i < 32 else 0.0) for i in range(48)]


def _sundays(n, first=date(2026, 6, 7)):
    return [first + timedelta(days=7 * k) for k in range(n)]


# ----------------------------------------------------------------------------
# The pure measurement
# ----------------------------------------------------------------------------

class TestSplitIntoDays(unittest.TestCase):

    def test_a_drifted_row_is_shared_between_the_half_hours_it_overlaps(self):
        # 09:32-10:02 holds 28 minutes of the 9:30 slot and 2 of the 10:00 one.
        out = ss.split_into_days([("2026-09-27T09:32:00", "2026-09-27T10:02:00", 0.6)])
        energy, covered = out[date(2026, 9, 27)]
        self.assertAlmostEqual(energy[19], 0.6 * 28 / 30)
        self.assertAlmostEqual(energy[20], 0.6 * 2 / 30)
        self.assertAlmostEqual(covered[19], 28 * 60)

    def test_a_row_across_midnight_is_split_between_the_two_days(self):
        out = ss.split_into_days([("2026-09-26T23:45:00", "2026-09-27T00:15:00", 0.4)])
        self.assertAlmostEqual(out[date(2026, 9, 26)][0][47], 0.2)
        self.assertAlmostEqual(out[date(2026, 9, 27)][0][0], 0.2)

    def test_a_gap_row_is_dropped_because_its_timing_is_unknown(self):
        out = ss.split_into_days([("2026-09-27T09:00:00", "2026-09-27T12:00:00", 3.0)])
        self.assertEqual(out, {})

    def test_junk_rows_are_ignored(self):
        out = ss.split_into_days([("nonsense", "2026-09-27T10:00:00", 0.3),
                                  ("2026-09-27T10:00:00", "2026-09-27T09:30:00", 0.3),
                                  ("2026-09-27T10:00:00", "2026-09-27T10:30:00", "x"),
                                  ("2026-09-27T10:00:00", "2026-09-27T10:30:00", -1)])
        self.assertEqual(out, {})


class TestSundayShape(unittest.TestCase):

    def _rows(self, days, per_slot, phase_min=0):
        rows = []
        for d in days:
            rows += _day_rows(d, per_slot, phase_min)
        return rows

    def test_the_afternoon_roast_shows_in_the_shape(self):
        days  = _sundays(8)
        shape, used = ss.sunday_shape(self._rows(days, _roast_day()), days, LONDON)
        self.assertEqual(used, 8)
        self.assertAlmostEqual(sum(shape), 1.0)
        self.assertGreater(sum(shape[28:32]), 4 * max(shape[10:20]))

    def test_too_few_sundays_keep_the_everyday_curve(self):
        days = _sundays(5)
        shape, used = ss.sunday_shape(self._rows(days, _roast_day()), days, LONDON)
        self.assertIsNone(shape)
        self.assertEqual(used, 5)

    def test_only_listed_sundays_count(self):
        days     = _sundays(8)
        rows     = self._rows(days, _roast_day())
        # A Saturday with a morning peak, listed or not, must never be used.
        saturday = days[0] - timedelta(days=1)
        rows    += _day_rows(saturday, [5.0 if i == 20 else 0.1 for i in range(48)])
        shape, used = ss.sunday_shape(rows, set(days[:6]) | {saturday}, LONDON)
        self.assertEqual(used, 6)
        self.assertLess(shape[20], 0.05)

    def test_a_sunday_with_missing_half_hours_is_left_out(self):
        days = _sundays(7)
        rows = self._rows(days, _roast_day())
        gappy = [r for r in rows if not r[0].startswith(days[0].isoformat() + "T1")]
        _shape, used = ss.sunday_shape(gappy, days, LONDON)
        self.assertEqual(used, 6)

    def test_the_clock_change_sunday_is_left_out(self):
        change = date(2026, 10, 25)                  # BST ends
        days   = _sundays(6) + [change]
        _shape, used = ss.sunday_shape(self._rows(days, _roast_day()), days, LONDON)
        self.assertEqual(used, 6)

    def test_a_heavy_sunday_does_not_outvote_a_light_one_on_timing(self):
        days  = _sundays(6)
        rows  = []
        for k, d in enumerate(days):
            if k == 0:     # one enormous Sunday, all of it at 8am
                rows += _day_rows(d, [40.0 if i == 16 else 0.4 for i in range(48)])
            else:
                rows += _day_rows(d, _roast_day())
        shape, _used = ss.sunday_shape(rows, days, LONDON)
        self.assertGreater(sum(shape[28:32]), sum(shape[15:18]))

    def test_drifting_record_phases_give_the_same_shape(self):
        days = _sundays(6)
        a, _ = ss.sunday_shape(self._rows(days, _roast_day()), days, LONDON)
        # Starting twenty minutes late shifts each row but not when energy was used,
        # apart from the 20 minutes before midnight that fall off the end.
        b, _ = ss.sunday_shape(self._rows(days, _roast_day(), phase_min=20), days, LONDON)
        self.assertAlmostEqual(sum(a[28:32]), sum(b[28:32]), delta=0.03)

    def test_scaled_carries_the_level(self):
        shape = [1 / 48.0] * 48
        self.assertAlmostEqual(sum(ss.scaled(shape, 21.3)), 21.3, places=2)


# ----------------------------------------------------------------------------
# The Flux planner's profile
# ----------------------------------------------------------------------------

class TestHalfHourProfileSundays(unittest.TestCase):

    def setUp(self):
        self.sunday = [0.2] * 48
        self.sunday[28] = self.sunday[29] = 2.0      # 2pm-3pm
        self.house  = fs.HalfHourProfile(FLAT, LONDON, sunday_slots=self.sunday)

    def _between(self, y, m, d, h0, h1):
        a = datetime(y, m, d, h0, tzinfo=LONDON)
        return self.house.kwh_between(a, a + timedelta(hours=h1 - h0))

    def test_sunday_afternoon_uses_the_sunday_curve(self):
        self.assertAlmostEqual(self._between(2026, 9, 27, 14, 15), 4.0)

    def test_saturday_afternoon_keeps_the_everyday_curve(self):
        self.assertAlmostEqual(self._between(2026, 9, 26, 14, 15), 0.8)

    def test_a_walk_into_sunday_changes_curve_at_midnight(self):
        a = datetime(2026, 9, 26, 23, 0, tzinfo=LONDON)
        got = self.house.kwh_between(a, a + timedelta(hours=2))
        self.assertAlmostEqual(got, 0.8 + 0.4)

    def test_without_a_sunday_curve_nothing_changes(self):
        plain = fs.HalfHourProfile(FLAT, LONDON)
        a = datetime(2026, 9, 27, 14, 0, tzinfo=LONDON)
        self.assertAlmostEqual(plain.kwh_between(a, a + timedelta(hours=1)), 0.8)

    def test_an_unusable_sunday_curve_is_dropped_not_fatal(self):
        for bad in ([0.1] * 47, [float("nan")] * 48, [0.0] * 48, ["x"] * 48):
            h = fs.HalfHourProfile(FLAT, LONDON, sunday_slots=bad)
            self.assertIsNone(h.sunday_slots)

    def test_scale_applies_to_both_curves(self):
        h = fs.HalfHourProfile(FLAT, LONDON, scale=2.0, sunday_slots=self.sunday)
        a = datetime(2026, 9, 27, 14, 0, tzinfo=LONDON)
        self.assertAlmostEqual(h.kwh_between(a, a + timedelta(hours=1)), 8.0)


# ----------------------------------------------------------------------------
# The manager
# ----------------------------------------------------------------------------

class TestManagerSundays(unittest.TestCase):

    def setUp(self):
        self.sunday = [0.2] * 48
        self.sunday[28] = self.sunday[29] = 2.0

    def test_profile_for_weekday_picks_the_sunday_curve_on_sunday_only(self):
        snap = ManagerSnapshot(consumption_profile=FLAT,
                               sunday_consumption_profile=self.sunday)
        self.assertIs(profile_for_weekday(snap, 6), self.sunday)
        for wd in range(6):
            self.assertIs(profile_for_weekday(snap, wd), FLAT)

    def test_without_a_sunday_curve_sunday_uses_the_blend(self):
        snap = ManagerSnapshot(consumption_profile=FLAT)
        self.assertIs(profile_for_weekday(snap, 6), FLAT)

    def test_estimate_uses_the_sunday_curve_for_sunday_half_hours(self):
        bm = BatteryManager.__new__(BatteryManager)
        a  = datetime(2026, 9, 27, 13, 0, tzinfo=timezone.utc)   # 2pm BST, Sunday
        got = bm._estimate_consumption_until(a, a + timedelta(hours=1), FLAT, self.sunday)
        self.assertAlmostEqual(got, 4.0)
        sat = a - timedelta(days=1)
        got = bm._estimate_consumption_until(sat, sat + timedelta(hours=1), FLAT, self.sunday)
        self.assertAlmostEqual(got, 0.8)

    def test_estimate_without_a_sunday_curve_is_unchanged(self):
        bm = BatteryManager.__new__(BatteryManager)
        a  = datetime(2026, 9, 27, 13, 0, tzinfo=timezone.utc)
        self.assertAlmostEqual(
            bm._estimate_consumption_until(a, a + timedelta(hours=1), FLAT), 0.8)

    def test_every_estimate_call_passes_the_sunday_curve(self):
        """Walk the AST: a call that forgets the Sunday curve plans Sunday on the blend."""
        tree = ast.parse(open(os.path.join(HERE, "battery_manager.py"),
                              encoding="utf-8").read())
        calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Attribute)
                 and n.func.attr == "_estimate_consumption_until"]
        self.assertGreaterEqual(len(calls), 6)
        for c in calls:
            self.assertEqual(len(c.args) + len(c.keywords), 4,
                             f"line {c.lineno} does not pass the Sunday curve")

    def test_no_reader_indexes_the_blended_curve_for_today_directly(self):
        """snapshot.consumption_profile may only be passed on, never sliced."""
        tree = ast.parse(open(os.path.join(HERE, "battery_manager.py"),
                              encoding="utf-8").read())
        for n in ast.walk(tree):
            if isinstance(n, ast.Subscript) and isinstance(n.value, ast.Attribute) \
                    and n.value.attr == "consumption_profile":
                self.fail(f"line {n.lineno} slices consumption_profile directly")


# ----------------------------------------------------------------------------
# The plugin wiring
# ----------------------------------------------------------------------------

def _mk(tmp):
    p = plugin.Plugin.__new__(plugin.Plugin)
    p.data_dir    = tmp
    p.logger      = MagicMock()
    p.store       = {"consumption_profile": list(FLAT), "away_active": False,
                     "sunday_shape": [], "day_uplifts": [1.0, 1.0, 1.1],
                     "day_uplift_date": datetime.now().strftime("%Y-%m-%d")}
    p.pluginPrefs = {}
    p._state_lock = threading.RLock()
    return p


class TestPluginWiring(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp  = self._tmp.name
        self.p    = _mk(self.tmp)
        self.p._measured_day_uplifts = lambda: (1.0, 1.0, 1.1)

    def tearDown(self):
        self._tmp.cleanup()

    def _write_history(self, days, per_slot):
        with open(os.path.join(self.tmp, "daily_history.json"), "w", encoding="utf-8") as fh:
            json.dump([{"date": d.isoformat(), "home_kwh": sum(per_slot)} for d in days], fh)
        con = sqlite3.connect(os.path.join(self.tmp, "energy_timeseries.db"))
        con.execute("CREATE TABLE halfhourly (id INTEGER PRIMARY KEY, slot_start TEXT UNIQUE,"
                    " slot_end TEXT, home_kwh REAL)")
        for d in days:
            con.executemany("INSERT INTO halfhourly (slot_start, slot_end, home_kwh)"
                            " VALUES (?,?,?)", _day_rows(d, per_slot))
        con.commit()
        con.close()

    def _recent_sundays(self, n):
        today = date.today()
        last  = today - timedelta(days=(today.weekday() + 1) % 7 or 7)
        return [last - timedelta(days=7 * k) for k in range(n)]

    def test_refresh_measures_and_logs_the_shape(self):
        self._write_history(self._recent_sundays(8), _roast_day())
        self.p._refresh_sunday_shape()
        shape = self.p.store["sunday_shape"]
        self.assertEqual(len(shape), 48)
        self.assertEqual(self.p.store["sunday_shape_days"], 8)
        self.assertGreater(sum(shape[28:32]), 0.2)

    def test_refresh_with_too_few_sundays_leaves_it_empty(self):
        self._write_history(self._recent_sundays(3), _roast_day())
        self.p._refresh_sunday_shape()
        self.assertEqual(self.p.store["sunday_shape"], [])

    def test_refresh_without_files_warns_and_keeps_what_it_had(self):
        self.p.store["sunday_shape"] = [1 / 48.0] * 48
        self.p._refresh_sunday_shape()
        self.assertEqual(len(self.p.store["sunday_shape"]), 48)
        self.p.logger.warning.assert_called()

    def test_sunday_profile_carries_the_sunday_total(self):
        self.p.store["sunday_shape"] = [1 / 48.0] * 48
        want = sum(FLAT) * plugin._need_scales(1.0, 1.0, 1.1)[3]
        self.assertAlmostEqual(sum(self.p._sunday_profile()), want, places=2)
        self.assertAlmostEqual(sum(self.p._sunday_profile(25.0)), 25.0, places=2)

    def test_no_sunday_profile_while_the_house_is_empty(self):
        self.p.store["sunday_shape"] = [1 / 48.0] * 48
        self.p.store["away_active"]  = True
        self.assertEqual(self.p._sunday_profile(), [])

    def test_no_sunday_profile_without_a_shape_or_a_usable_base(self):
        self.assertEqual(self.p._sunday_profile(), [])
        self.p.store["sunday_shape"]        = [1 / 48.0] * 48
        self.p.store["consumption_profile"] = [0.05] * 48          # 2.4 kWh a day
        self.assertEqual(self.p._sunday_profile(), [])
        self.p.store["consumption_profile"] = list(FLAT)
        for bad in ("x", float("nan"), -3.0, 0.0):
            self.assertEqual(self.p._sunday_profile(bad), [])

    def test_every_planner_profile_is_built_with_the_sunday_curve(self):
        """Both HalfHourProfile sites — the Flux planner and the booking decision."""
        tree = ast.parse(open(os.path.join(HERE, "plugin.py"), encoding="utf-8").read())
        calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Attribute) and n.func.attr == "HalfHourProfile"]
        self.assertEqual(len(calls), 2)
        for c in calls:
            kw = {k.arg for k in c.keywords}
            self.assertIn("sunday_slots", kw, f"line {c.lineno} builds without Sundays")

    def test_the_snapshot_is_given_the_sunday_curve(self):
        tree = ast.parse(open(os.path.join(HERE, "plugin.py"), encoding="utf-8").read())
        found = [k for n in ast.walk(tree) if isinstance(n, ast.Call)
                 and getattr(n.func, "id", None) == "ManagerSnapshot"
                 for k in n.keywords if k.arg == "sunday_consumption_profile"]
        self.assertEqual(len(found), 1)

    def test_the_refresh_measures_the_sunday_shape_first(self):
        tree = ast.parse(open(os.path.join(HERE, "plugin.py"), encoding="utf-8").read())
        fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                  and n.name == "_refresh_consumption_profile")
        called = [n.func.attr for n in ast.walk(fn) if isinstance(n, ast.Call)
                  and isinstance(n.func, ast.Attribute)]
        self.assertIn("_refresh_sunday_shape", called)


if __name__ == "__main__":
    unittest.main()
