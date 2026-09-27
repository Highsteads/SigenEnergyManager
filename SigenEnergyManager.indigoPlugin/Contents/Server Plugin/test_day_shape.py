#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    test_day_shape.py
# Description: Contract tests for a weekday's own half-hourly shape (5.118.0
#              Sunday, 5.119.0 Saturday): the pure measurement, the Flux
#              planner's day-aware profile, the manager's day-aware estimates,
#              the plugin wiring that hands the shapes to all three, and the
#              recorder fix that makes the half-hourly rows honest at midnight.
#              CliveS, 27-Sep-2026: the roast, both microwaves and the wash put a
#              Sunday's load in the afternoon; a Saturday's lands in the morning.
# Author:      CliveS & Claude Opus 5.5
# Date:        27-09-2026 12:05 BST
# Version:     2.0 (was test_sunday_shape.py 1.0)

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
import unittest.mock
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

import day_shape as ss                                      # noqa: E402
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


def SUN(rows, dates, tz=None, min_days=6):
    return ss.day_shape(rows, dates, 6, tz, min_days)


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
        shape, used = SUN(self._rows(days, _roast_day()), days, LONDON)
        self.assertEqual(used, 8)
        self.assertAlmostEqual(sum(shape), 1.0)
        self.assertGreater(sum(shape[28:32]), 4 * max(shape[10:20]))

    def test_too_few_sundays_keep_the_everyday_curve(self):
        days = _sundays(5)
        shape, used = SUN(self._rows(days, _roast_day()), days, LONDON)
        self.assertIsNone(shape)
        self.assertEqual(used, 5)

    def test_only_listed_sundays_count(self):
        days     = _sundays(8)
        rows     = self._rows(days, _roast_day())
        # A Saturday with a morning peak, listed or not, must never be used.
        saturday = days[0] - timedelta(days=1)
        rows    += _day_rows(saturday, [5.0 if i == 20 else 0.1 for i in range(48)])
        shape, used = SUN(rows, set(days[:6]) | {saturday}, LONDON)
        self.assertEqual(used, 6)
        self.assertLess(shape[20], 0.05)

    def test_a_sunday_with_missing_half_hours_is_left_out(self):
        days = _sundays(7)
        rows = self._rows(days, _roast_day())
        gappy = [r for r in rows if not r[0].startswith(days[0].isoformat() + "T1")]
        _shape, used = SUN(gappy, days, LONDON)
        self.assertEqual(used, 6)

    def test_the_clock_change_sunday_is_left_out(self):
        change = date(2026, 10, 25)                  # BST ends
        days   = _sundays(6) + [change]
        _shape, used = SUN(self._rows(days, _roast_day()), days, LONDON)
        self.assertEqual(used, 6)

    def test_a_heavy_sunday_does_not_outvote_a_light_one_on_timing(self):
        days  = _sundays(6)
        rows  = []
        for k, d in enumerate(days):
            if k == 0:     # one enormous Sunday, all of it at 8am
                rows += _day_rows(d, [40.0 if i == 16 else 0.4 for i in range(48)])
            else:
                rows += _day_rows(d, _roast_day())
        shape, _used = SUN(rows, days, LONDON)
        self.assertGreater(sum(shape[28:32]), sum(shape[15:18]))

    def test_drifting_record_phases_give_the_same_shape(self):
        days = _sundays(6)
        a, _ = SUN(self._rows(days, _roast_day()), days, LONDON)
        # Starting twenty minutes late shifts each row but not when energy was used,
        # apart from the 20 minutes before midnight that fall off the end.
        b, _ = SUN(self._rows(days, _roast_day(), phase_min=20), days, LONDON)
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
        self.house  = fs.HalfHourProfile(FLAT, LONDON, day_slots={6: self.sunday})

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
            h = fs.HalfHourProfile(FLAT, LONDON, day_slots={6: bad})
            self.assertEqual(h.day_slots, {})

    def test_scale_applies_to_both_curves(self):
        h = fs.HalfHourProfile(FLAT, LONDON, scale=2.0, day_slots={6: self.sunday})
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
                               day_profiles={6: self.sunday})
        self.assertIs(profile_for_weekday(snap, 6), self.sunday)
        for wd in range(6):
            self.assertIs(profile_for_weekday(snap, wd), FLAT)

    def test_without_a_sunday_curve_sunday_uses_the_blend(self):
        snap = ManagerSnapshot(consumption_profile=FLAT)
        self.assertIs(profile_for_weekday(snap, 6), FLAT)

    def test_estimate_uses_the_sunday_curve_for_sunday_half_hours(self):
        bm = BatteryManager.__new__(BatteryManager)
        a  = datetime(2026, 9, 27, 13, 0, tzinfo=timezone.utc)   # 2pm BST, Sunday
        got = bm._estimate_consumption_until(a, a + timedelta(hours=1), FLAT, {6: self.sunday})
        self.assertAlmostEqual(got, 4.0)
        sat = a - timedelta(days=1)
        got = bm._estimate_consumption_until(sat, sat + timedelta(hours=1), FLAT, {6: self.sunday})
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
        # 4 since battery_manager 3.14 removed the peak top-up's two calls.
        self.assertGreaterEqual(len(calls), 4)
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




class TestOtherWeekdaysAndOldRows(unittest.TestCase):

    def test_a_group_is_measured_as_one_shape(self):
        weds = [d - timedelta(days=4) for d in _sundays(3)]
        thus = [d - timedelta(days=3) for d in _sundays(3)]
        rows = []
        for d in weds + thus:
            rows += _day_rows(d, _roast_day())
        alone, n_alone = ss.day_shape(rows, set(weds + thus), 2, LONDON)
        group, n_group = ss.day_shape(rows, set(weds + thus), (1, 2, 3, 4), LONDON)
        self.assertIsNone(alone)
        self.assertEqual((n_alone, n_group), (3, 6))
        self.assertAlmostEqual(sum(group), 1.0)

    def test_saturday_gets_its_own_shape_from_saturdays_only(self):
        sats  = [d - timedelta(days=1) for d in _sundays(8)]
        suns  = _sundays(8)
        morning = [0.4 + (1.0 if 20 <= i < 26 else 0.0) for i in range(48)]   # 10am-1pm
        rows = []
        for d in sats:
            rows += _day_rows(d, morning)
        for d in suns:
            rows += _day_rows(d, _roast_day())
        sat, n_sat = ss.day_shape(rows, set(sats) | set(suns), 5, LONDON)
        sun, n_sun = ss.day_shape(rows, set(sats) | set(suns), 6, LONDON)
        self.assertEqual((n_sat, n_sun), (8, 8))
        self.assertGreater(sum(sat[20:26]), 2 * sum(sat[28:32]))
        self.assertGreater(sum(sun[28:32]), 2 * sum(sun[20:24]))

    def test_the_old_midnight_row_is_read_from_where_the_last_row_ended(self):
        # The pre-5.119 recorder: last write 23:34:52, then a row labelled
        # 00:00:35-00:30:35 holding everything since 23:34:52.
        rows = [("2026-09-19T23:04:52", "2026-09-19T23:34:52", 0.39),
                ("2026-09-20T00:00:35", "2026-09-20T00:30:35", 0.77)]
        out = ss.split_into_days(rows)
        late = out[date(2026, 9, 19)][0]
        self.assertGreater(late[47], 0.3)                # 23:34:52 to midnight
        self.assertLess(out[date(2026, 9, 20)][0][0], 0.45)

    def test_a_midnight_row_after_a_long_gap_keeps_its_label(self):
        rows = [("2026-09-19T21:00:00", "2026-09-19T21:30:00", 0.39),
                ("2026-09-20T00:00:35", "2026-09-20T00:30:35", 0.40)]
        out = ss.split_into_days(rows)
        self.assertNotIn(date(2026, 9, 19), {d for d in out if out[d][0][47] > 0})

    def test_rows_written_since_the_fix_are_left_alone(self):
        rows = [("2026-09-19T23:30:10", "2026-09-20T00:00:18", 0.40),
                ("2026-09-20T00:00:18", "2026-09-20T00:30:18", 0.35)]
        out = ss.split_into_days(rows)
        self.assertAlmostEqual(out[date(2026, 9, 20)][0][0] + out[date(2026, 9, 20)][0][1],
                               0.35 + 0.40 * 18 / 1808, places=3)

    def test_a_zero_row_is_missing_not_quiet(self):
        days = _sundays(6)
        rows = []
        for k, d in enumerate(days):
            day = _day_rows(d, [0.4] * 48)
            if k % 2:                                 # the old recorder's midnight zero
                day[0] = (day[0][0], day[0][1], 0.0)
            rows += day
        shape, used = SUN(rows, days, LONDON)
        self.assertEqual(used, 6)
        self.assertAlmostEqual(shape[0], 1 / 48.0, places=4)

    def test_a_partly_recorded_half_hour_counts_at_the_rate_it_ran(self):
        days = _sundays(6)
        rows = []
        for d in days:
            day = _day_rows(d, [0.4] * 48)
            s0, e0, _k = day[20]                      # 10:00-10:30: only 10:10-10:30 recorded
            day[20] = ((datetime.fromisoformat(s0) + timedelta(minutes=10)).isoformat(), e0,
                       0.4 * 20 / 30)
            rows += day
        shape, _used = SUN(rows, days, LONDON)
        self.assertAlmostEqual(shape[20], 1 / 48.0, places=4)

    def test_a_half_hour_never_recorded_on_any_day_gives_no_shape(self):
        days = _sundays(6)
        rows = []
        for d in days:
            rows += [r for i, r in enumerate(_day_rows(d, [0.4] * 48)) if i != 30]
        shape, _used = SUN(rows, days, LONDON)
        self.assertIsNone(shape)


# ----------------------------------------------------------------------------
# The plugin wiring
# ----------------------------------------------------------------------------

def _mk(tmp):
    p = plugin.Plugin.__new__(plugin.Plugin)
    p.data_dir    = tmp
    p.logger      = MagicMock()
    p.store       = {"consumption_profile": list(FLAT), "away_active": False,
                     "day_shapes": {}, "day_shape_days": {}}
    p.pluginPrefs = {}
    p._state_lock = threading.RLock()
    return p


def _source_tree():
    return ast.parse(open(os.path.join(HERE, "plugin.py"), encoding="utf-8").read())


class TestPluginWiring(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp  = self._tmp.name
        self.p    = _mk(self.tmp)
        self.p._measured_day_uplifts = lambda: (1.06, 1.2, 1.1)

    def tearDown(self):
        self._tmp.cleanup()

    def _write_history(self, days_and_slots):
        with open(os.path.join(self.tmp, "daily_history.json"), "w", encoding="utf-8") as fh:
            json.dump([{"date": d.isoformat(), "home_kwh": sum(sl)}
                       for d, sl in days_and_slots], fh)
        con = sqlite3.connect(os.path.join(self.tmp, "energy_timeseries.db"))
        con.execute("CREATE TABLE halfhourly (id INTEGER PRIMARY KEY, slot_start TEXT UNIQUE,"
                    " slot_end TEXT, home_kwh REAL)")
        for d, sl in days_and_slots:
            con.executemany("INSERT INTO halfhourly (slot_start, slot_end, home_kwh)"
                            " VALUES (?,?,?)", _day_rows(d, sl))
        con.commit()
        con.close()

    def _recent(self, weekday, n):
        today = date.today()
        back  = (today.weekday() - weekday) % 7 or 7
        last  = today - timedelta(days=back)
        return [last - timedelta(days=7 * k) for k in range(n)]

    def test_refresh_measures_saturday_and_sunday(self):
        morning = [0.4 + (1.0 if 20 <= i < 26 else 0.0) for i in range(48)]
        self._write_history([(d, _roast_day()) for d in self._recent(6, 8)]
                            + [(d, morning) for d in self._recent(5, 7)])
        self.p._refresh_day_shapes()
        shapes = self.p.store["day_shapes"]
        self.assertEqual(sorted(shapes), [5, 6])
        self.assertEqual(self.p.store["day_shape_days"],
                         {0: 0, 1: 0, 2: 0, 3: 0, 4: 0, 5: 7, 6: 8})
        self.assertGreater(sum(shapes[6][28:32]), 0.2)
        self.assertGreater(sum(shapes[5][20:26]), 0.25)

    def test_the_announcement_is_made_once_and_survives_a_restart(self):
        self._write_history([(d, _roast_day()) for d in self._recent(6, 8)])
        self.p._save_accumulators = MagicMock()
        with unittest.mock.patch.object(plugin, "log") as log:
            self.p._refresh_day_shapes()
            self.assertEqual(log.call_count, 1)
            self.assertEqual(self.p.store["day_shapes_announced"], [6])
            self.p._save_accumulators.assert_called_once()
            restarted = _mk(self.tmp)
            restarted._measured_day_uplifts = self.p._measured_day_uplifts
            restarted._save_accumulators = MagicMock()
            restarted.store["day_shapes_announced"] = [6]     # as _load_accumulators restores it
            restarted._refresh_day_shapes()
            self.assertEqual(log.call_count, 1)
            restarted._save_accumulators.assert_not_called()

    def test_the_announcement_list_is_saved_and_restored(self):
        p = plugin.Plugin.__new__(plugin.Plugin)
        p.logger = MagicMock()
        p.store  = {"pv_daily_kwh": 1.0, "grid_import_daily_kwh": 0.0,
                    "grid_export_daily_kwh": 2.0, "home_daily_kwh": 3.0,
                    "peak_soc": 90.0, "min_soc": 40.0, "today_date": "2026-09-27",
                    "pv_lifetime_start_kwh": 100.0, "import_lifetime_start_kwh": 10.0,
                    "export_lifetime_start_kwh": 20.0, "day_shapes_announced": [5, 6]}
        p.pluginPrefs = {}
        p._state_lock = threading.RLock()
        p._save_home_profile = MagicMock()
        path = os.path.join(self.tmp, "accumulators.json")
        p._save_accumulators_locked(path)
        with open(path, encoding="utf-8") as fh:
            saved = json.load(fh)
        self.assertEqual(saved["day_shapes_announced"], [5, 6])
        saved["day_shapes_announced"] = [5, 6, 9, "x"]           # junk is dropped
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(saved, fh)
        q = plugin.Plugin.__new__(plugin.Plugin)
        q.logger = MagicMock()
        q.store  = {"day_shapes_announced": []}
        q._get_data_dir = lambda: self.tmp
        q._load_accumulators()
        self.assertEqual(q.store["day_shapes_announced"], [5, 6])

    def test_refresh_measures_monday(self):
        evening = [0.4 + (0.6 if 34 <= i < 40 else 0.0) for i in range(48)]   # 5pm-8pm
        self._write_history([(d, evening) for d in self._recent(0, 7)])
        self.p._refresh_day_shapes()
        shapes = self.p.store["day_shapes"]
        self.assertEqual(sorted(shapes), [0])
        self.assertGreater(sum(shapes[0][34:40]), sum(shapes[0][20:26]))

    def test_a_day_with_too_few_examples_is_left_out(self):
        self._write_history([(d, _roast_day()) for d in self._recent(6, 8)]
                            + [(d, _roast_day()) for d in self._recent(5, 3)])
        self.p._refresh_day_shapes()
        self.assertEqual(sorted(self.p.store["day_shapes"]), [6])
        self.assertEqual(self.p.store["day_shape_days"][5], 3)

    def test_tuesday_to_friday_share_one_shape_from_all_four(self):
        evening = [0.4 + (0.6 if 34 <= i < 40 else 0.0) for i in range(48)]
        # Three Wednesdays and three Thursdays: neither day has six on its own,
        # but the group does.
        self._write_history([(d, evening) for d in self._recent(2, 3)]
                            + [(d, evening) for d in self._recent(3, 3)])
        with unittest.mock.patch.object(plugin, "log") as log:
            self.p._refresh_day_shapes()
        shapes = self.p.store["day_shapes"]
        self.assertEqual(sorted(shapes), [1, 2, 3, 4])
        self.assertTrue(shapes[1] == shapes[2] == shapes[3] == shapes[4])
        self.assertEqual(self.p.store["day_shape_days"][4], 6)
        self.assertEqual(log.call_count, 1)
        self.assertIn("Tuesdays to Fridays now have their own daily pattern, measured "
                      "from 6 of those days", log.call_args[0][0])
        self.assertEqual(self.p.store["day_shapes_announced"], [1, 2, 3, 4])

    def test_refresh_without_files_warns_and_keeps_what_it_had(self):
        self.p.store["day_shapes"] = {6: [1 / 48.0] * 48}
        self.p._refresh_day_shapes()
        self.assertEqual(list(self.p.store["day_shapes"]), [6])
        self.p.logger.warning.assert_called()

    def test_day_profiles_carry_each_days_total(self):
        self.p.store["day_shapes"] = {0: [1 / 48.0] * 48, 5: [1 / 48.0] * 48,
                                      6: [1 / 48.0] * 48}
        scales = plugin._need_scales(1.06, 1.2, 1.1)
        got = self.p._day_profiles()
        self.assertAlmostEqual(sum(got[0]), sum(FLAT) * scales[1], places=2)
        self.assertAlmostEqual(sum(got[5]), sum(FLAT) * scales[2], places=2)
        self.assertAlmostEqual(sum(got[6]), sum(FLAT) * scales[3], places=2)
        got = self.p._day_profiles({5: 30.0, 6: 25.0})
        self.assertAlmostEqual(sum(got[5]), 30.0, places=2)
        self.assertAlmostEqual(sum(got[6]), 25.0, places=2)

    def test_no_day_profiles_while_the_house_is_empty(self):
        self.p.store["day_shapes"]  = {6: [1 / 48.0] * 48}
        self.p.store["away_active"] = True
        self.assertEqual(self.p._day_profiles(), {})

    def test_no_day_profiles_without_a_shape_or_a_usable_base(self):
        self.assertEqual(self.p._day_profiles(), {})
        self.p.store["day_shapes"]          = {6: [1 / 48.0] * 48}
        self.p.store["consumption_profile"] = [0.05] * 48          # 2.4 kWh a day
        self.assertEqual(self.p._day_profiles(), {})
        self.p.store["consumption_profile"] = list(FLAT)
        for bad in ("x", float("nan"), -3.0, 0.0):
            self.assertEqual(self.p._day_profiles({6: bad}), {})

    def test_every_planner_profile_is_built_with_the_day_curves(self):
        """Both HalfHourProfile sites — the Flux planner and the booking decision."""
        calls = [n for n in ast.walk(_source_tree()) if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Attribute) and n.func.attr == "HalfHourProfile"]
        self.assertEqual(len(calls), 2)
        for c in calls:
            kw = {k.arg for k in c.keywords}
            self.assertIn("day_slots", kw, f"line {c.lineno} builds without the day curves")

    def test_the_snapshot_is_given_every_shaped_days_level(self):
        found = [k for n in ast.walk(_source_tree()) if isinstance(n, ast.Call)
                 and getattr(n.func, "id", None) == "ManagerSnapshot"
                 for k in n.keywords if k.arg == "day_profiles"]
        self.assertEqual(len(found), 1)
        levels = found[0].value.args[0]
        self.assertIsInstance(levels, ast.Dict)
        names = {k.value: v.id for k, v in zip(levels.keys, levels.values)}
        self.assertEqual(names, {0: "monday_pref", 1: "weekday_pref", 2: "weekday_pref",
                                 3: "weekday_pref", 4: "weekday_pref",
                                 5: "saturday_pref", 6: "sunday_pref"})
        self.assertEqual(set(names), set(plugin.SHAPED_WEEKDAYS))

    def test_every_shaped_day_has_its_own_level(self):
        self.assertEqual(set(plugin._NEED_SCALE_INDEX), set(plugin.SHAPED_WEEKDAYS))

    def test_each_weekday_is_in_exactly_one_group(self):
        seen = [wd for g in plugin.DAY_SHAPE_GROUPS for wd in g]
        self.assertEqual(sorted(seen), list(range(7)))

    def test_the_weekday_levels_come_from_the_tue_fri_scale(self):
        self.p.store["day_shapes"] = {wd: [1 / 48.0] * 48 for wd in range(7)}
        scales = plugin._need_scales(1.06, 1.2, 1.1)
        got = self.p._day_profiles()
        for wd in (1, 2, 3, 4):
            self.assertAlmostEqual(sum(got[wd]), sum(FLAT) * scales[0], places=2)

    def test_the_refresh_measures_the_day_shapes_first(self):
        fn = next(n for n in ast.walk(_source_tree()) if isinstance(n, ast.FunctionDef)
                  and n.name == "_refresh_consumption_profile")
        called = [n.func.attr for n in ast.walk(fn) if isinstance(n, ast.Call)
                  and isinstance(n.func, ast.Attribute)]
        self.assertIn("_refresh_day_shapes", called)


class TestDayPatternsForTheDashboard(unittest.TestCase):
    """5.122.0: /api/day-patterns, charted on the Dashboards Energy page."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.p = _mk(self._tmp.name)
        self.p._measured_day_uplifts = lambda: (1.06, 1.2, 1.1)

    def tearDown(self):
        self._tmp.cleanup()

    def test_one_group_each_with_its_own_or_the_everyday_pattern(self):
        sunday = [0.0] * 48
        sunday[30] = 1.0
        self.p.store["day_shapes"]     = {6: sunday}
        self.p.store["day_shape_days"] = {0: 3, 1: 60, 2: 60, 3: 60, 4: 60, 5: 17, 6: 17}
        out = self.p.get_dashboard_day_patterns()
        self.assertTrue(out["available"])
        self.assertIn(out["today_weekday"], range(7))
        by_key = {g["key"]: g for g in out["groups"]}
        self.assertEqual(list(by_key), ["mon", "tue-fri", "sat", "sun"])
        self.assertEqual(by_key["tue-fri"]["label"], "Tuesdays to Fridays")
        self.assertEqual(by_key["tue-fri"]["weekdays"], [1, 2, 3, 4])
        scales = plugin._need_scales(1.06, 1.2, 1.1)
        for key, idx in (("mon", 1), ("tue-fri", 0), ("sat", 2), ("sun", 3)):
            g = by_key[key]
            self.assertEqual(len(g["kwh"]), 48)
            self.assertAlmostEqual(g["total_kwh"], sum(FLAT) * scales[idx], places=2)
            self.assertAlmostEqual(sum(g["kwh"]), g["total_kwh"], places=1)
            self.assertAlmostEqual(sum(g["everyday_kwh"]), g["total_kwh"], places=1)
        self.assertTrue(by_key["sun"]["own_pattern"])
        self.assertAlmostEqual(by_key["sun"]["kwh"][30], by_key["sun"]["total_kwh"], places=2)
        self.assertFalse(by_key["mon"]["own_pattern"])
        self.assertEqual(by_key["mon"]["kwh"], by_key["mon"]["everyday_kwh"])
        self.assertEqual(by_key["mon"]["days_used"], 3)

    def test_while_the_house_is_empty_no_day_has_its_own(self):
        self.p.store["day_shapes"]  = {6: [1 / 48.0] * 48}
        self.p.store["away_active"] = True
        out = self.p.get_dashboard_day_patterns()
        self.assertTrue(out["away"])
        self.assertFalse(any(g["own_pattern"] for g in out["groups"]))
        for g in out["groups"]:
            self.assertAlmostEqual(g["total_kwh"], sum(FLAT), places=2)

    def test_no_profile_yet_says_so(self):
        self.p.store["consumption_profile"] = []
        out = self.p.get_dashboard_day_patterns()
        self.assertFalse(out["available"])
        self.assertEqual(out["groups"], [])
        self.assertTrue(out["reason"])

    def test_the_payload_is_plain_json(self):
        self.p.store["day_shapes"] = {wd: [1 / 48.0] * 48 for wd in range(7)}
        json.dumps(self.p.get_dashboard_day_patterns())


class TestRecorderIsHonestAtMidnight(unittest.TestCase):
    """5.119.0: the half-hourly rows say when their energy was really used."""

    def test_midnight_writes_the_old_days_last_row_before_resetting_the_timer(self):
        fn = next(n for n in ast.walk(_source_tree()) if isinstance(n, ast.FunctionDef)
                  and n.name == "_check_midnight_impl")
        order = []
        for n in ast.walk(fn):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) \
                    and n.func.attr == "_log_halfhourly_to_db":
                order.append(("write", n.lineno))
            if isinstance(n, ast.Assign) and any(
                    isinstance(t, ast.Subscript) and getattr(t.slice, "value", None)
                    == "last_energy_var" for t in n.targets):
                order.append(("reset", n.lineno))
        order.sort(key=lambda x: x[1])
        self.assertTrue(order and order[0][0] == "write", order)

    def _plugin_with_anchor(self, tmp, anchor_at):
        p = _mk(tmp)
        p.pluginPrefs = {"batteryCapacityKwh": "35.04"}
        p.latest_inverter_data = {"batterySoc": 50.0}
        p.latest_rates_data    = {}
        p.latest_decision      = None
        p._find_device = lambda *_a, **_k: None
        snap = {"pv": 10.0, "gridImport": 5.0, "gridExport": 2.0, "home": 20.0}
        p.daily_energy = MagicMock()
        p.daily_energy.lifetime_snapshot.return_value = dict(snap, home=20.9)
        p.store.update({"hh_anchor_lifetime": snap, "hh_anchor_soc_pct": 50.0,
                        "hh_anchor_at": anchor_at})
        con = sqlite3.connect(os.path.join(tmp, "energy_timeseries.db"))
        con.execute("""CREATE TABLE halfhourly (id INTEGER PRIMARY KEY AUTOINCREMENT,
            slot_start TEXT UNIQUE, slot_end TEXT, grid_import_kwh REAL, grid_export_kwh REAL,
            pv_kwh REAL, home_kwh REAL, battery_soc_start_pct REAL, battery_soc_end_pct REAL,
            battery_net_kwh REAL, tracker_price_p REAL, agile_price_p REAL,
            manager_action TEXT, battery_charge_kwh REAL, battery_discharge_kwh REAL)""")
        con.commit()
        con.close()
        return p

    def _row(self, tmp):
        con = sqlite3.connect(os.path.join(tmp, "energy_timeseries.db"))
        try:
            return con.execute("SELECT slot_start, slot_end, home_kwh FROM halfhourly").fetchone()
        finally:
            con.close()

    def test_a_row_starts_where_its_energy_started(self):
        with tempfile.TemporaryDirectory() as tmp:
            anchor = (datetime.now() - timedelta(minutes=55)).strftime("%Y-%m-%dT%H:%M:%S")
            p = self._plugin_with_anchor(tmp, anchor)
            p._log_halfhourly_to_db_impl()
            start, end, home = self._row(tmp)
            self.assertEqual(start, anchor)
            self.assertAlmostEqual(home, 0.9)
            self.assertEqual(p.store["hh_anchor_at"], end)

    def test_an_unknown_anchor_time_keeps_the_old_label(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = self._plugin_with_anchor(tmp, None)
            p._log_halfhourly_to_db_impl()
            start, end, _home = self._row(tmp)
            gap = datetime.fromisoformat(end) - datetime.fromisoformat(start)
            self.assertEqual(gap, timedelta(seconds=plugin.ENERGY_VAR_INTERVAL))

    def test_a_stale_anchor_time_is_not_trusted(self):
        with tempfile.TemporaryDirectory() as tmp:
            anchor = (datetime.now() - timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%S")
            p = self._plugin_with_anchor(tmp, anchor)
            p._log_halfhourly_to_db_impl()
            start, end, _home = self._row(tmp)
            gap = datetime.fromisoformat(end) - datetime.fromisoformat(start)
            self.assertEqual(gap, timedelta(seconds=plugin.ENERGY_VAR_INTERVAL))

    def test_the_seed_records_its_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = self._plugin_with_anchor(tmp, None)
            p.store["hh_anchor_lifetime"] = None
            p._log_halfhourly_to_db_impl()
            self.assertIsNone(self._row(tmp))
            self.assertTrue(p.store["hh_anchor_at"])


if __name__ == "__main__":
    unittest.main()
