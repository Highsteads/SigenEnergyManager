#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    test_review_2026_10_02_modbus.py
# Description: Regression tests for the 02-10-2026 review of sigenergy_modbus.py
#              and daily_energy.py: a zero/backwards read taken as a meter reset,
#              no full sweep after a reconnect, midnight anchors taken from the
#              slow cache, the U32 0xFFFFFFFF sentinel, and a float handed to a
#              32-bit write. Runs without Indigo or an inverter.
# Author:      CliveS & Claude Opus 5.5
# Date:        02-10-2026
# Version:     1.0

import sys
import unittest
from unittest import mock
from unittest.mock import MagicMock

_pm = MagicMock()
sys.modules.setdefault("pymodbus", _pm)
sys.modules.setdefault("pymodbus.client", sys.modules["pymodbus"].client)
sys.modules.setdefault("pymodbus.exceptions", sys.modules["pymodbus"].exceptions)

import sigenergy_modbus as sm                                     # noqa: E402
from daily_energy import (                                        # noqa: E402
    DailyEnergy, local_midnight_epoch, readings_from_data, recovery_from_data,
)

D0 = "2026-10-01"
D1 = "2026-10-02"
MID_D1 = local_midnight_epoch(D1)


# ------------------------------------------------------------------
# A register-backed fake client. It never raises: an outage is a read
# that answers with an error result and a connect() that returns False.
# ------------------------------------------------------------------

class _Result:
    def __init__(self, regs=None, err=False):
        self.registers = regs or []
        self._err = err

    def isError(self):
        return self._err


class _FakeClient:
    def __init__(self):
        self.regs   = {}
        self.down   = False
        self.writes = []

    def connect(self):
        return not self.down

    def close(self):
        pass

    def read_holding_registers(self, address=None, count=1, device_id=None):
        if self.down:
            return _Result(err=True)
        return _Result([self.regs.get(address + i, 1) for i in range(count)])

    def write_register(self, address=None, value=None, device_id=None):
        self.writes.append((address, value))
        self.regs[address] = value
        return _Result()

    def write_registers(self, address=None, values=None, device_id=None):
        self.writes.append((address, list(values)))
        for i, v in enumerate(values):
            self.regs[address + i] = v
        return _Result()


def _put_u64(regs, addr, kwh):
    raw = int(round(kwh * 100))
    for i in range(4):
        regs[addr + i] = (raw >> (48 - 16 * i)) & 0xFFFF


def _set_counters(fc, imp=18000.0):
    _put_u64(fc.regs, sm.PLANT_PV_TOTAL_KWH, 25000.0)
    _put_u64(fc.regs, sm.PLANT_LOAD_TOTAL_KWH, 30000.0)
    _put_u64(fc.regs, sm.PLANT_ESS_CHARGE_TOTAL_KWH, 7000.0)
    _put_u64(fc.regs, sm.PLANT_ESS_CHARGE_TOTAL_KWH + 4, 6500.0)
    _put_u64(fc.regs, sm.PLANT_TOTAL_IMPORT_KWH, imp)
    _put_u64(fc.regs, sm.PLANT_TOTAL_IMPORT_KWH + 4, 9000.0)


def _modbus(fc):
    m = sm.SigenergyModbus("192.0.2.10", sleep_func=lambda s: None)
    m.logger = MagicMock()
    m.client = fc
    m._connected = True
    return m


def _full(**over):
    r = {"pv": 25000.0, "home": 30000.0, "gridImport": 18000.0, "gridExport": 9000.0,
         "batteryCharge": 7000.0, "batteryDischarge": 6500.0}
    r.update(over)
    return r


# ==================================================================
# Fix 1 — one zero / backwards read is not a meter reset
# ==================================================================

class TestZeroReadIsNotAMeterReset(unittest.TestCase):

    def test_lifetime_decoders_treat_exact_zero_as_absent(self):
        self.assertIsNone(sm.decode_energy_block_d([0] * 8))
        self.assertIsNone(sm.decode_energy_block_c([0] * 8))
        self.assertIsNone(sm.decode_energy_block_b([0] * 4))
        self.assertIsNone(sm.decode_energy_block_a([0, 0, 0, 0, 0, 5]))
        # a daily counter of zero is real (just after midnight) and stays valid
        self.assertEqual(sm.decode_energy_block_a([0, 0, 0x0026, 0x25A0, 0, 0]),
                         {"pvLifetimeKwh": 25000.0, "homeDailyDirectKwh": 0.0})

    def test_all_zero_block_does_not_latch_the_block_absent(self):
        """A block that answers with counters not yet loaded is not an absent
        register; latching it off would lose the figure until a restart."""
        fc = _FakeClient()
        _set_counters(fc)
        for i in range(8):
            fc.regs[sm.PLANT_ESS_CHARGE_TOTAL_KWH + i] = 0
        m = _modbus(fc)
        for _ in range(4):
            self.assertIsNotNone(m.read_all(force_full=True))
        self.assertNotIn("_energyC", m._energy_block_absent)

    def test_one_backwards_read_is_ignored(self):
        de = DailyEnergy()
        de.set_anchor(D0, _full(gridImport=17990.0), source="midnight")
        de.observe(_full(), MID_D1 + 30, D1)
        de.observe(_full(gridImport=18003.0), MID_D1 + 8 * 3600, D1)
        de.observe({"gridImport": 5.0}, MID_D1 + 8 * 3600 + 15, D1)    # one bad read
        self.assertEqual(de.last_backwards, ())
        self.assertEqual(de.lifetime("gridImport"), 18003.0)
        de.observe(_full(gridImport=18003.01), MID_D1 + 8 * 3600 + 30, D1)
        t = de.today()
        self.assertEqual(t["values"]["gridImport"], 3.01)
        self.assertFalse(t["partial"])
        self.assertIn("gridImport", de.anchors[D0]["values"])
        self.assertIn("gridImport", de.anchors[D1]["values"])

    def test_a_reset_is_accepted_when_the_low_value_persists(self):
        de = DailyEnergy()
        de.set_anchor(D0, _full(pv=24990.0), source="midnight")
        de.observe(_full(), MID_D1 + 30, D1)
        de.observe(_full(pv=25010.0), MID_D1 + 3600, D1)
        de.observe({"pv": 0.5}, MID_D1 + 7200, D1)
        self.assertEqual(de.last_backwards, ())
        de.observe({"pv": 0.6}, MID_D1 + 7215, D1)                     # persists
        self.assertEqual(de.last_backwards, ("pv",))
        t = de.today()
        self.assertEqual(t["sources"]["pv"], "late")
        self.assertEqual(t["values"]["pv"], 0.1)       # anchored at the first low read
        de.observe({"pv": 1.5}, MID_D1 + 7300, D1)
        self.assertEqual(de.today()["values"]["pv"], 1.0)
        # yesterday's anchor is never popped
        self.assertEqual(de.anchors[D0]["values"]["pv"], 24990.0)

    def test_completed_day_is_absent_not_zero_across_a_reset(self):
        de = DailyEnergy()
        de.set_anchor(D0, _full(pv=24990.0), source="midnight")
        de.set_anchor(D1, _full(pv=3.0), source="late")
        self.assertIsNone(de.completed(D0)["values"]["pv"])
        self.assertEqual(de.completed(D0)["values"]["gridImport"], 0.0)

    def test_repro_zero_block_then_good_read(self):
        """The reviewer's reproduction end to end through the decoder."""
        de = DailyEnergy()
        good = {"gridImportLifetimeKwh": 18432.10, "gridExportLifetimeKwh": 9120.55,
                "pvLifetimeKwh": 25010.00}
        de.observe(readings_from_data(good), MID_D1 + 30, D1)
        good2 = {k: v + 3.0 for k, v in good.items()}
        de.observe(readings_from_data(good2), MID_D1 + 8 * 3600, D1)
        zero = sm.decode_energy_block_d([0] * 8) or {}
        de.observe(readings_from_data(zero), MID_D1 + 8 * 3600 + 15, D1)
        good3 = {k: v + 0.01 for k, v in good2.items()}
        de.observe(readings_from_data(good3), MID_D1 + 8 * 3600 + 30, D1)
        self.assertEqual(de.today()["values"]["gridImport"], 3.01)


# ==================================================================
# Fix 2 — a reconnect re-primes the slow tier
# ==================================================================

class TestFullSweepAfterReconnect(unittest.TestCase):

    def test_first_snapshot_after_a_long_outage_carries_every_slow_key(self):
        clock = [1000.0]
        fc = _FakeClient()
        _set_counters(fc)
        fc.regs[sm.INV_BATTERY_AVG_TEMP] = 215
        fc.regs[sm.PLANT_ESS_SOH] = 998
        with mock.patch.object(sm.time, "monotonic", lambda: clock[0]), \
             mock.patch.object(sm, "ModbusTcpClient", lambda **kw: fc), \
             mock.patch.object(sm, "PYMODBUS_AVAILABLE", True):
            m = _modbus(fc)
            self.assertIn("batteryTempC", m.read_all())
            fc.down = True
            clock[0] += 5
            self.assertIsNone(m.read_all())
            self.assertFalse(m.connected)
            clock[0] += 700                        # longer than SLOW_CACHE_MAX_AGE_S
            fc.down = False
            d = m.read_all()
        self.assertIsNotNone(d)
        for key in ("emsWorkMode", "gridSensorConnected", "dischargeCutoffSoc",
                    "batterySoh", "batteryTempC", "batteryMinTempC",
                    "batteryMaxTempC", "batteryCellVoltage"):
            self.assertIn(key, d, f"{key} missing from the first snapshot after reconnect")


# ==================================================================
# Fix 3 — only keys read after midnight upgrade the midnight anchor
# ==================================================================

class TestMidnightAnchorPerKeyFreshness(unittest.TestCase):

    def test_read_all_names_the_keys_it_read_fresh(self):
        fc = _FakeClient()
        _set_counters(fc)
        m = _modbus(fc)
        m.read_all()
        specs = [s[0] for s in m._slow_read_specs()]
        m._slow_cursor = specs.index("_energyA")
        d = m.read_all()
        self.assertIsInstance(d["_energyReadAt"], float)        # still a number
        fresh = set(d["_energyFreshKeys"])
        self.assertIn("pvLifetimeKwh", fresh)
        self.assertNotIn("gridImportLifetimeKwh", fresh)
        self.assertIn("gridImportLifetimeKwh", d)               # served from cache
        self.assertNotIn("gridImport", readings_from_data(d))
        self.assertIn("pv", readings_from_data(d))

    def test_readings_without_a_fresh_list_are_all_taken(self):
        d = {"pvLifetimeKwh": 1.0, "gridImportLifetimeKwh": 2.0, "_energyReadAt": 5.0}
        self.assertEqual(readings_from_data(d), {"pv": 1.0, "gridImport": 2.0})

    def test_provisional_anchor_upgrades_only_the_keys_read_after_midnight(self):
        de = DailyEnergy()
        de.observe(_full(), MID_D1 - 300, D0)
        de.observe({"pv": 25000.0}, MID_D1 + 15, D1)            # only pv fresh
        a = de.anchors[D1]
        self.assertEqual(a["sources"]["pv"], "midnight")
        self.assertEqual(a["sources"]["gridImport"], "provisional")
        self.assertTrue(a["provisional"])
        de.observe(_full(gridImport=18000.4), MID_D1 + 30, D1)  # the forced full read
        a = de.anchors[D1]
        self.assertEqual(a["sources"]["gridImport"], "midnight")
        self.assertEqual(a["values"]["gridImport"], 18000.4)
        self.assertFalse(a["provisional"])

    def test_window_expiry_settles_only_the_pending_keys(self):
        de = DailyEnergy()
        de.observe(_full(), MID_D1 - 120, D0)
        de.observe({"pv": 25000.1}, MID_D1 + 15, D1)
        de.observe(_full(pv=25003.0, gridImport=18002.0), MID_D1 + 3600, D1)
        a = de.anchors[D1]
        self.assertEqual(a["sources"]["pv"], "midnight")
        self.assertEqual(a["values"]["pv"], 25000.1)
        self.assertEqual(a["sources"]["gridImport"], "boundary")
        self.assertFalse(a["provisional"])

    def test_repro_cached_pre_midnight_value_never_becomes_the_anchor(self):
        wall = [MID_D1 - 300]
        mono = [5000.0]
        imp = [18000.0]
        fc = _FakeClient()
        _set_counters(fc, imp[0])
        with mock.patch.object(sm.time, "monotonic", lambda: mono[0]), \
             mock.patch.object(sm.time, "time", lambda: wall[0]):
            m = _modbus(fc)
            de = DailyEnergy()
            d = m.read_all()
            de.observe(readings_from_data(d), d["_energyReadAt"], D0)
            imp[0] += 0.40
            _set_counters(fc, imp[0])
            wall[0] += 315
            mono[0] += 315
            m._slow_cursor = [s[0] for s in m._slow_read_specs()].index("_energyA")
            d = m.read_all()
            m.mark_slow_read_due(*sm.ENERGY_BLOCK_KEYS)
            de.observe(readings_from_data(d), float(d["_energyReadAt"]), D1,
                       recovery=recovery_from_data(d), fresh=d.get("_energyReadAt") is not None)
            wall[0] += 15
            mono[0] += 15
            d = m.read_all()
            de.observe(readings_from_data(d), float(d["_energyReadAt"]), D1,
                       recovery=recovery_from_data(d), fresh=True)
        a = de.anchors[D1]
        self.assertEqual(a["sources"]["gridImport"], "midnight")
        self.assertEqual(a["values"]["gridImport"], 18000.4)
        self.assertEqual(de.today()["values"]["gridImport"], 0.0)


# ==================================================================
# Fix 4 — the U32 0xFFFFFFFF sentinel
# ==================================================================

class TestU32Sentinel(unittest.TestCase):

    def test_block_a_drops_a_sentinel_daily_and_keeps_the_lifetime(self):
        out = sm.decode_energy_block_a([0, 0, 0x0026, 0x25A0, 0xFFFF, 0xFFFF])
        self.assertEqual(out, {"pvLifetimeKwh": 25000.0})

    def test_every_u32_slow_post_rejects_the_sentinel(self):
        m = sm.SigenergyModbus("192.0.2.10")
        for key, reader, _reg, _slave, post in m._slow_read_specs():
            if reader == m._read_uint32:
                self.assertIsNone(post(0xFFFFFFFF), key)

    def test_observe_rejects_an_implausible_recovery(self):
        de = DailyEnergy()
        de.observe({"home": 30000.0, "batteryCharge": 7000.0, "batteryDischarge": 6500.0},
                   MID_D1 + 36000, D1,
                   recovery={"home": 42949672.95, "batteryCharge": 250.0,
                             "batteryDischarge": 12.5})
        t = de.today()
        self.assertEqual(t["sources"]["home"], "late")
        self.assertEqual(t["sources"]["batteryCharge"], "late")
        self.assertEqual(t["sources"]["batteryDischarge"], "recovered")
        self.assertEqual(t["values"]["home"], 0.0)
        self.assertEqual(t["values"]["batteryDischarge"], 12.5)

    def test_observe_rejects_recovery_above_the_lifetime(self):
        de = DailyEnergy()
        de.observe({"home": 30.0}, MID_D1 + 36000, D1, recovery={"home": 40.0})
        self.assertEqual(de.today()["sources"]["home"], "late")


# ==================================================================
# Fix 5 — a non-integer 32-bit write is a handled error
# ==================================================================

class TestUint32WriteValidatesItsValue(unittest.TestCase):

    def test_float_value_is_refused_and_logged(self):
        fc = _FakeClient()
        m = _modbus(fc)
        self.assertFalse(m._write_uint32_registers(sm.HOLD_ESS_MAX_DISCHARGE, 5000.5))
        self.assertTrue(m.logger.error.called)
        self.assertEqual(fc.writes, [])
        self.assertTrue(m.connected, "a bad argument is not a dead link")

    def test_integer_value_still_writes(self):
        fc = _FakeClient()
        m = _modbus(fc)
        self.assertTrue(m._write_uint32_registers(sm.HOLD_ESS_MAX_DISCHARGE, 70000))
        self.assertEqual(fc.writes, [(sm.HOLD_ESS_MAX_DISCHARGE, [1, 4464])])


if __name__ == "__main__":
    unittest.main()
