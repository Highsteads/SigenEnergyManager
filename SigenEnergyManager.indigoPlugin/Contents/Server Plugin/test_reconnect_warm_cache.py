#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    test_reconnect_warm_cache.py
# Description: A short Modbus drop must not cost a full read sweep. On 28-10-2026
#              the inverter reset the socket twice; the plugin reconnected in
#              0.3 s but the first snapshot took ~31 s, because every reconnect
#              re-read all ~29 registers at the protocol's 1 s spacing. While the
#              slow cache is still fresh, a reconnect now runs a normal short
#              cycle plus the control settings; a long outage still gets the
#              full sweep the 02-10-2026 review added.
# Author:      CliveS & Claude Opus 5.5
# Date:        28-10-2026
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
from test_review_2026_10_02_modbus import (                       # noqa: E402
    _FakeClient, _modbus, _set_counters,
)

SLOW_KEYS = ("emsWorkMode", "gridSensorConnected", "dischargeCutoffSoc",
             "batterySoh", "batteryTempC", "batteryMinTempC",
             "batteryMaxTempC", "batteryCellVoltage")


class _CountingClient(_FakeClient):
    def __init__(self):
        super().__init__()
        self.reads = []

    def read_holding_registers(self, address=None, count=1, device_id=None):
        if not self.down:
            self.reads.append(address)
        return super().read_holding_registers(address=address, count=count,
                                              device_id=device_id)


class TestReconnectWithAWarmCache(unittest.TestCase):

    def _drop(self, outage_s, change_during=None):
        """Prime, drop the link for outage_s, reconnect. Returns (modbus,
        client, snapshot, reads on the reconnect cycle, reads on the prime)."""
        clock = [1000.0]
        fc = _CountingClient()
        _set_counters(fc)
        fc.regs[sm.INV_BATTERY_AVG_TEMP] = 215
        fc.regs[sm.PLANT_ESS_SOH] = 998
        fc.regs[sm.PLANT_ESS_DISCHARGE_CUTOFF] = 100           # 10.0 %
        with mock.patch.object(sm.time, "monotonic", lambda: clock[0]), \
             mock.patch.object(sm, "ModbusTcpClient", lambda **kw: fc), \
             mock.patch.object(sm, "PYMODBUS_AVAILABLE", True):
            m = _modbus(fc)
            self.assertIn("batteryTempC", m.read_all())
            n_prime = len(fc.reads)
            for _ in range(3):                                  # routine cycles
                clock[0] += 10
                m.read_all()
            fc.down = True
            clock[0] += 5
            self.assertIsNone(m.read_all())
            self.assertFalse(m.connected)
            if change_during:
                change_during(fc)
            clock[0] += outage_s
            fc.down = False
            fc.reads.clear()
            d = m.read_all()
        return m, fc, d, len(fc.reads), n_prime

    def test_a_short_drop_reconnects_with_a_short_cycle(self):
        _, _, d, n, n_prime = self._drop(40)
        self.assertIsNotNone(d)
        recheck = len(sm.RECONNECT_RECHECK_KEYS)
        # the connect probe, the fast tier, the rotation and the control settings
        self.assertLessEqual(n, 1 + 5 + sm.SLOW_READS_PER_CYCLE + recheck)
        self.assertLess(n * 2, n_prime, "a reconnect after a blip must not re-read everything")

    def test_a_short_drop_still_returns_every_slow_key(self):
        _, _, d, _, _ = self._drop(40)
        for key in SLOW_KEYS:
            self.assertIn(key, d, f"{key} missing after a short drop")

    def test_control_settings_are_read_fresh_on_reconnect(self):
        # The inverter may have restarted during the drop and come back with
        # different settings, so they must not be served from the cache.
        def change(fc):
            fc.regs[sm.PLANT_ESS_DISCHARGE_CUTOFF] = 200       # 20.0 %
        _, _, d, _, _ = self._drop(40, change_during=change)
        self.assertEqual(d["dischargeCutoffSoc"], 20.0)

    def test_a_long_drop_still_gets_the_full_sweep(self):
        _, _, d, n, n_prime = self._drop(700)
        self.assertGreaterEqual(n, n_prime, "past the cache age every register is re-read")
        for key in SLOW_KEYS:
            self.assertIn(key, d)

    def test_a_drop_that_leaves_the_cache_half_aged_gets_the_full_sweep(self):
        # Between half and the whole SLOW_CACHE_MAX_AGE_S some keys would expire
        # before the rotation reached them, so the snapshot would lose them.
        _, _, d, n, n_prime = self._drop(sm.SLOW_CACHE_MAX_AGE_S // 2 + 30)
        self.assertGreaterEqual(n, n_prime)
        for key in SLOW_KEYS:
            self.assertIn(key, d)

    def test_the_first_connect_is_always_a_full_sweep(self):
        fc = _CountingClient()
        _set_counters(fc)
        with mock.patch.object(sm, "ModbusTcpClient", lambda **kw: fc), \
             mock.patch.object(sm, "PYMODBUS_AVAILABLE", True):
            m = sm.SigenergyModbus("192.0.2.10", sleep_func=lambda s: None)
            m.logger = MagicMock()
            self.assertTrue(m.connect())
            fc.reads.clear()
            d = m.read_all()
        self.assertGreater(len(fc.reads), 20)
        for key in SLOW_KEYS:
            self.assertIn(key, d)


if __name__ == "__main__":
    unittest.main()
