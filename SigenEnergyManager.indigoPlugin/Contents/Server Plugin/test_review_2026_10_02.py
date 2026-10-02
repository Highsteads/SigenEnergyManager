#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    test_review_2026_10_02.py
# Description: Faults from the independent review of 5.131.1, each one a
#              transition or a failure path the earlier tests stubbed past.
# Author:      CliveS & Claude Opus 5.5
# Date:        02-10-2026
# Version:     1.0

import pathlib
import sys
import tempfile
import threading
import time
import types
import unittest
from datetime import date, timedelta
from unittest.mock import MagicMock, patch

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

import plugin                        # noqa: E402
import flux_strategy as fs           # noqa: E402
import flux_execution as fx          # noqa: E402
import test_flux_strategy as T       # noqa: E402
import test_flux_execution as TX     # noqa: E402
from sigenergy_modbus import SigenergyModbus   # noqa: E402


def _bare(**store):
    p = plugin.Plugin.__new__(plugin.Plugin)
    p._state_lock = threading.RLock()
    p.logger = MagicMock()
    p.pluginPrefs = {}
    p.store = dict(store)
    return p


# -- P1: the cheap-window charge band ------------------------------------------

class TestChargeBandIsNeverInverted(unittest.TestCase):
    """A charge target below the household floor (the roof covers an Axle event
    the floor counts in full) was refused by the executor every tick, and
    nothing charged all night."""

    def _scenarios(self):
        d = (2026, 6, 21)                 # a free-hour Sunday: the 50% minimum is waived
        ev = T._axle(d, 17, 18, kw=4.0)
        hh = fs.EventCommitment(source="octopus", kind="import",
                                start=fs._wall(T.LONDON, date(*d), fs.time(13, 0)),
                                end=fs._wall(T.LONDON, date(*d), fs.time(14, 0)),
                                energy_kwh=10.0)
        d2 = (2026, 6, 24)
        yield d, 45.0, (ev, hh), 30.0
        yield d, 45.0, (ev, hh), 12.0
        yield d2, 70.0, (T._axle(d2, 12, 15, kw=4.0),), 30.0

    def test_every_charge_plan_is_a_band_the_executor_accepts(self):
        charges = 0
        for d, pv_kwh, commitments, soc in self._scenarios():
            pv = T._pv(date(*d), pv_kwh, first_hour=6, last_hour=20)
            p = fs.plan(T._inputs((2, 30), soc_pct=soc, day=d, pv=pv,
                                  commitments=commitments))
            self.assertLessEqual(p.discharge_cutoff_pct, p.charge_cutoff_pct, (d, soc))
            if p.mode != fs.MODE_CHARGE:
                continue
            charges += 1
            now = p.decision_at
            regs = TX.Registers()
            with tempfile.TemporaryDirectory() as tmp:
                e = fx.FluxExecutor(regs, pathlib.Path(tmp) / "j.json",
                                    baseline_charge_w=10000, baseline_discharge_w=10000,
                                    baseline_discharge_cutoff_pct=20., clock=lambda: now)
                results = []
                for _ in range(3):
                    t = fx.FluxTarget(now, p.decision_until, now, now + timedelta(seconds=30),
                                      p.ems_mode, p.charge_limit_w, p.discharge_limit_w,
                                      round(p.charge_cutoff_pct, 1),
                                      round(p.discharge_cutoff_pct, 1))
                    results.append(e.step(t, now))
                self.assertIn("applied", results, (d, soc, e.last_error))
                self.assertNotIn("band", e.last_error)
        self.assertGreaterEqual(charges, 2)          # the scenarios do reach a charge


class TestARefusedPlanHandsTheWindowBack(unittest.TestCase):

    def test_a_refusal_is_logged_once_and_releases_the_window(self):
        p = _bare()
        ex = types.SimpleNamespace(last_error="Invalid target energy band")
        dec = types.SimpleNamespace(owns=True)
        with patch.object(plugin, "log") as lg:
            p._flux_note_refused(ex, dec, "released")
            p._flux_note_refused(ex, dec, "released")
        self.assertTrue(p.store["flux_refused"])
        self.assertEqual(lg.call_count, 1)

    def test_a_plain_release_is_not_a_refusal(self):
        p = _bare()
        p._flux_note_refused(types.SimpleNamespace(last_error=""),
                             types.SimpleNamespace(owns=True), "released")
        self.assertFalse(p.store.get("flux_refused"))

    def test_the_manager_keeps_the_window_while_refused(self):
        p = _bare(flux_refused=True,
                  flux_decision=types.SimpleNamespace(mode=fs.MODE_CHARGE))
        p.latest_rates_data = {"tariff_info": {"tariff_key": plugin.TARIFF_FLUX}}
        with patch.object(plugin, "FLUX_AVAILABLE", True), \
                patch.object(fs, "in_window", return_value=True):
            p._flux_armed = lambda: True
            p._flux_other_owner = lambda: False
            p._flux_owns_control = lambda: True
            p._flux_may_claim = lambda: True
            self.assertFalse(p._flux_owns_cheap_window())
            p.store["flux_refused"] = False
            self.assertTrue(p._flux_owns_cheap_window())


# -- P1: shutdown after Indigo has stopped the thread ---------------------------

class TestShutdownStillWritesSelfConsumption(unittest.TestCase):
    """Indigo stops the concurrent thread before shutdown(), so self.sleep()
    raises; every Modbus write after the first died on its pause."""

    def test_the_mode_register_is_written_after_the_stop(self):
        class Host(plugin.Plugin):
            stopThread = True

            def sleep(self, seconds):
                if self.stopThread:
                    raise self.StopThread
        p = Host.__new__(Host)
        p._state_lock = threading.RLock()
        p.logger = MagicMock()
        p.pluginPrefs = {}
        p.store = {}
        p.web_dashboard = None
        p.flux_executor = None
        p._flux_release = lambda why: None
        p._save_accumulators = MagicMock()
        writes = []
        client = MagicMock()

        def _ok():
            r = MagicMock()
            r.isError.return_value = False
            return r

        def _wr(address, value, device_id):
            writes.append((address, value))
            return _ok()

        def _wrs(address, values, device_id):
            writes.append((address, values))
            return _ok()

        def _rd(address, count, device_id):
            r = _ok()
            last = [v for a, v in writes if a == address and isinstance(v, int)]
            r.registers = [last[-1] if last else 0] * count
            return r
        client.write_register.side_effect = _wr
        client.write_registers.side_effect = _wrs
        client.read_holding_registers.side_effect = _rd
        m = SigenergyModbus(ip="192.0.2.1", port=502, plant_address=247,
                            inverter_address=1, logger=MagicMock(), sleep_func=p.sleep)
        m.client = client
        m._connected = True
        m._last_request_time = 0.0
        m.disconnect = MagicMock()
        p.modbus = m
        with patch.object(plugin, "log"), patch.object(time, "sleep"):
            p.shutdown()
        self.assertIn((40031, 2), writes)


if __name__ == "__main__":
    unittest.main()
