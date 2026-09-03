#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    test_concurrency.py
# Description: Locking-model contract tests (v5.45.0). The tick must NOT hold
#              _state_lock across network I/O (Modbus read cycle, HTTP fetches)
#              — action callbacks and the dashboard were stalling up to ~20s
#              behind a poll. The manager evaluate/verify/act path must STAY
#              fully locked (hardware decisions serialise with the locked
#              action callbacks). These tests pin both sides of that contract.
# Author:      CliveS & Claude Fable 5
# Date:        02-07-2026
# Version:     1.0

import sys
import threading
import time
import types
import unittest
from unittest.mock import MagicMock

# ============================================================
# Stub indigo + pymodbus before importing plugin (setdefault so
# running the whole suite in one process keeps the first stub).
# ============================================================

_ind = types.ModuleType("indigo")


class _PB:
    class StopThread(Exception):
        pass

    def __init__(self, *a, **k):
        pass


_ind.PluginBase = _PB
_ind.Dict = dict
_ind.List = list
for _a in ("kStateImageSel", "server", "devices", "variables", "variable",
           "kDeviceAction", "activePlugin", "trigger"):
    setattr(_ind, _a, MagicMock())
sys.modules.setdefault("indigo", _ind)

_pm = MagicMock()
sys.modules.setdefault("pymodbus", _pm)
sys.modules.setdefault("pymodbus.client", _pm.client)
sys.modules.setdefault("pymodbus.exceptions", _pm.exceptions)
sys.modules.setdefault("requests", MagicMock())

import plugin  # noqa: E402


def _mk_plugin():
    """Minimal Plugin instance able to run _tick with every stage idle."""
    p = plugin.Plugin.__new__(plugin.Plugin)
    p._state_lock = threading.RLock()
    p.logger = MagicMock()
    p.debug = False
    p.pluginPrefs = {}
    p.data_dir = "/tmp"
    p.modbus = None
    p.octopus = None
    p.forecast = None
    p.axle = None
    p.latest_inverter_data = {}
    p.latest_forecast_data = {}
    p.latest_rates_data = {}
    p.latest_decision = None
    now = time.time()
    p.store = {k: now for k in (
        "last_modbus", "last_forecast", "last_manager", "last_octopus",
        "last_profile", "last_cost_settle", "last_vpp", "last_acc_save",
        "last_storm_watch", "last_energy_var", "last_log_check",
        "last_saving_sessions",
    )}
    p.store.update({
        "vpp_state": "idle", "vpp_event": None, "manager_paused": False,
    })
    # Stages with side effects the harness doesn't drive
    p._check_midnight = MagicMock()
    p._check_scheduled_import = MagicMock()
    p._save_accumulators = MagicMock()
    p._update_inverter_device_offline = MagicMock()
    p._update_forecast_device = MagicMock()
    p._find_device = MagicMock(return_value=None)
    return p


class TestNetworkRunsUnlocked(unittest.TestCase):
    """Network I/O must never execute while _state_lock is held by the tick."""

    def test_modbus_read_all_runs_unlocked(self):
        p = _mk_plugin()
        seen = {}

        class _FakeModbus:
            connected = True

            def read_all(self_inner):
                seen["locked"] = p._state_lock._is_owned()
                return None

        p.modbus = _FakeModbus()
        p.store["last_modbus"] = 0
        p._tick(time.time())
        self.assertIn("locked", seen, "read_all was never called")
        self.assertFalse(seen["locked"],
                         "_state_lock held across modbus.read_all()")

    def test_forecast_fetch_runs_unlocked(self):
        p = _mk_plugin()
        seen = {}
        p.forecast = MagicMock()

        def _fetch(force=False):
            seen["locked"] = p._state_lock._is_owned()
            return {"forecastStatus": "OK", "correctedTomorrowKwh": 10.0,
                    "correctedTodayKwh": 5.0, "todayKwh": 5.0, "biasFactor": 1.0}

        p.forecast.fetch_forecast.side_effect = _fetch
        p.store["last_forecast"] = 0
        p._tick(time.time())
        self.assertIn("locked", seen, "fetch_forecast was never called")
        self.assertFalse(seen["locked"],
                         "_state_lock held across forecast fetch")

    def test_storm_poll_runs_unlocked(self):
        p = _mk_plugin()
        seen = {}
        orig = plugin.check_storm_level

        def _check(*a, **k):
            seen["locked"] = p._state_lock._is_owned()
            return ("none", "harness")

        plugin.check_storm_level = _check
        try:
            p.store["last_storm_watch"] = 0
            p.store["storm_level"] = "none"
            p.store["storm_alerted_level"] = "none"
            p._tick(time.time())
        finally:
            plugin.check_storm_level = orig
        self.assertIn("locked", seen, "check_storm_level was never called")
        self.assertFalse(seen["locked"],
                         "_state_lock held across the MeteoAlarm poll")

    def test_vpp_fetch_runs_unlocked(self):
        p = _mk_plugin()
        seen = {}
        p.pluginPrefs["axleEnabled"] = True
        p.axle = MagicMock()

        def _next_event():
            seen["locked"] = p._state_lock._is_owned()
            return None

        p.axle.get_next_event.side_effect = _next_event
        p._update_vpp_device = MagicMock()
        p.store["last_vpp"] = 0
        p._tick(time.time())
        self.assertIn("locked", seen, "get_next_event was never called")
        self.assertFalse(seen["locked"],
                         "_state_lock held across the Axle poll")


class TestControlStaysLocked(unittest.TestCase):
    """The evaluate/verify/act path must run entirely under _state_lock —
    hardware decisions serialise with the locked action callbacks."""

    def test_evaluate_runs_under_lock(self):
        p = _mk_plugin()
        seen = {}

        def _impl():
            seen["locked"] = p._state_lock._is_owned()

        p._evaluate_manager_impl = _impl
        p.store["last_manager"] = 0
        p._tick(time.time())
        self.assertIn("locked", seen, "_evaluate_manager_impl never ran")
        self.assertTrue(seen["locked"],
                        "evaluate/act ran WITHOUT _state_lock — control "
                        "must stay serialised with action callbacks")


class TestOutageBackoff(unittest.TestCase):
    """The poll back-off written by the failure path must SURVIVE the tick —
    the tick used to stamp last_modbus AFTER the poll returned, clobbering the
    back-off and silently re-polling a dead inverter at full cadence."""

    def test_outage_backoff_survives_the_tick(self):
        p = _mk_plugin()
        p.modbus_poll_s = 10

        class _DeadModbus:
            connected = False

            def read_all(self_inner):
                return None

        p.modbus = _DeadModbus()
        p.store["modbus_consecutive_failures"] = 2   # next failure is the 3rd
        p.store["last_modbus"] = 0
        before = time.time()
        p._tick(before)
        # 3rd consecutive failure => 60s back-off: last_modbus must sit well
        # in the future relative to a plain "now" stamp.
        self.assertGreater(p.store["last_modbus"], before + 30,
                           "back-off stamp was clobbered by the tick")


class TestCallbackLatency(unittest.TestCase):
    """An action callback must not stall behind a slow Modbus cycle."""

    def test_callback_lock_acquisition_under_one_second(self):
        p = _mk_plugin()
        started = threading.Event()

        class _SlowModbus:
            connected = True

            def read_all(self_inner):
                started.set()
                time.sleep(1.5)   # simulates the ~16-20s throttled read cycle
                return None

        p.modbus = _SlowModbus()
        p.store["last_modbus"] = 0

        t = threading.Thread(target=lambda: p._tick(time.time()))
        t.start()
        self.assertTrue(started.wait(2.0), "poll never started")
        time.sleep(0.1)           # read_all definitely in flight
        t0 = time.time()
        with p._state_lock:       # what every action callback does
            waited = time.time() - t0
        t.join()
        self.assertLess(waited, 1.0,
                        f"callback blocked {waited:.2f}s behind the poll — "
                        f"the tick is holding _state_lock across network I/O")


# Module level, NOT class attributes: a staticmethod fetched off the class is
# a plain function, and assigning it INSIDE a class body rebinds it as an
# instance method, so self would be passed as poll_s.
_tick_sleep = plugin.Plugin._tick_sleep_seconds
_accum_h    = plugin.Plugin._accum_interval_h


class TestTickPacing(unittest.TestCase):
    """The loop used to do its work and THEN sleep the whole interval, so the
    real period was (work + interval). With the Modbus read at ~7 s and the
    interval at 5 s that made a 12 s cycle out of a setting that said 5, and
    before the v1.13 read tiering it was 43 s. Both helpers are pure."""

    def test_sleeps_the_remainder_not_the_whole_interval(self):
        # 5 s interval, tick took 3 s -> 2 s left, not another 5.
        self.assertAlmostEqual(_tick_sleep(5, 3.0), 2.0)

    def test_a_tick_slower_than_the_interval_does_not_add_more_delay(self):
        # The real case: the read is ~7 s against a 5 s interval.
        self.assertAlmostEqual(_tick_sleep(5, 7.0), 0.2)
        self.assertAlmostEqual(_tick_sleep(5, 60.0), 0.2)

    def test_a_long_interval_is_unaffected(self):
        """The low-traffic settings must not start waking every fraction of a
        second — there the work is short, so nearly the whole budget remains."""
        self.assertAlmostEqual(_tick_sleep(120, 0.05), 10 - 0.05)   # capped at the 10 s base
        self.assertAlmostEqual(_tick_sleep(30, 0.05), 10 - 0.05)

    def test_never_returns_zero_or_negative(self):
        for poll, took in ((5, 5), (5, 1e6), (120, 1e6), (5, -1)):
            self.assertGreater(_tick_sleep(poll, took), 0.0)

    def test_accum_interval_follows_measured_elapsed_time(self):
        # 12 s between passes is 12 s of energy, whatever the setting says.
        self.assertAlmostEqual(_accum_h(12.0, 5), 12.0 / 3600.0)
        self.assertAlmostEqual(_accum_h(43.0, 5), 43.0 / 3600.0)

    def test_accum_interval_is_clamped_after_an_outage(self):
        """A reconnect after an hour must not dump an hour of power into the
        day's total in one pass."""
        self.assertAlmostEqual(_accum_h(3600.0, 5), 60.0 / 3600.0)
        self.assertAlmostEqual(_accum_h(3600.0, 120), 360.0 / 3600.0)

    def test_accum_interval_never_goes_backwards(self):
        self.assertEqual(_accum_h(-5.0, 5), 0.0)
        self.assertEqual(_accum_h(0.0, 5), 0.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
