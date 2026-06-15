#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    test_plugin.py
# Description: Unit tests for plugin.py module-level helpers that run without Indigo.
#              Mocks indigo + pymodbus so plugin.py imports standalone, then exercises
#              the config-coercion helpers (_as_float / _as_int) — the hot-path guards
#              that turn a blank/None/non-numeric config field into the default instead
#              of a ValueError/TypeError during battery evaluate.
# Author:      CliveS & Claude Opus 4.8
# Date:        06-06-2026
# Version:     1.0

import os
import sys
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
sys.modules["indigo"] = _indigo

_pm = MagicMock()
sys.modules["pymodbus"] = _pm
sys.modules["pymodbus.client"] = _pm.client
sys.modules["pymodbus.exceptions"] = _pm.exceptions
sys.modules.setdefault("requests", MagicMock())

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import plugin   # noqa: E402


class TestConfigCoercion(unittest.TestCase):
    """_as_float / _as_int — value path, fallback path, and the v5.26.1 fix where a
    STRING fallback (e.g. '94') must be coerced, not leaked into arithmetic."""

    # --- _as_float value path ---
    def test_as_float_valid_string(self):
        self.assertEqual(plugin._as_float("12.5", 0.0), 12.5)

    def test_as_float_valid_number(self):
        self.assertEqual(plugin._as_float(7, 0.0), 7.0)

    # --- _as_float fallback path ---
    def test_as_float_blank_returns_numeric_fallback(self):
        self.assertEqual(plugin._as_float("", 35.04), 35.04)

    def test_as_float_none_returns_fallback(self):
        self.assertEqual(plugin._as_float(None, 1.0), 1.0)

    def test_as_float_garbage_returns_fallback(self):
        self.assertEqual(plugin._as_float("on", 4.0), 4.0)

    def test_as_float_string_fallback_is_coerced(self):
        # The bug fixed in v5.26.1: callers pass string defaults ('94', '35.04');
        # a blank field must yield a FLOAT, not the raw string (which then hit
        # `_as_float(blank,'94') / 100.0` -> TypeError on the evaluate path).
        result = plugin._as_float("", "94")
        self.assertEqual(result, 94.0)
        self.assertIsInstance(result, float)
        # And it must survive the arithmetic that crashed before.
        self.assertAlmostEqual(plugin._as_float("", "94") / 100.0, 0.94)

    # --- _as_int ---
    def test_as_int_valid(self):
        self.assertEqual(plugin._as_int("5", 0), 5)

    def test_as_int_blank_returns_fallback(self):
        self.assertEqual(plugin._as_int("", 30), 30)

    def test_as_int_string_fallback_is_coerced(self):
        result = plugin._as_int("", "30")
        self.assertEqual(result, 30)
        self.assertIsInstance(result, int)

    def test_as_int_garbage_returns_fallback(self):
        self.assertEqual(plugin._as_int("port", 8080), 8080)


class TestDriveVppExport(unittest.TestCase):
    """_drive_vpp_export sub-mode decision: bank-surplus (mode 0x02 + charge cap)
    when daytime PV surplus >= export target, else discharge (0x05 daytime /
    0x06 dark). Charge cap = surplus - target, clamped >= 0. Hysteresis holds the
    sub-mode in the band around the target."""

    TARGET_W = 4000   # maxExportKw 4.0

    def _mk(self, pv_w, home_w, daytime=True, prev_sub=None, prev_cap=-1):
        p = plugin.Plugin.__new__(plugin.Plugin)
        p.modbus = MagicMock()
        p.latest_inverter_data = {"pvPowerWatts": pv_w, "homePowerWatts": home_w}
        p.pluginPrefs = {"inverterMaxKw": "10.0", "maxExportKw": "4.0"}
        p.store = {"export_active": True, "vpp_is_daytime": daytime,
                   "vpp_export_submode": prev_sub, "vpp_bank_charge_cap_w": prev_cap}
        p._trigger_event = lambda *a, **k: None
        return p

    def _charge_cap_writes(self, p):
        return [c.args[0] for c in p.modbus.set_charge_limit.call_args_list]

    def test_high_pv_picks_bank_and_caps_charge(self):
        """Daytime, surplus 8952 >> 4000 -> bank: mode 0x02, charge cap = surplus-target."""
        p = self._mk(pv_w=9630, home_w=678)        # surplus 8952
        p._drive_vpp_export()
        self.assertEqual(p.store["vpp_export_submode"], "bank")
        p.modbus.set_self_consumption.assert_called_once()
        self.assertEqual(p.store["vpp_bank_charge_cap_w"], 8952 - self.TARGET_W)
        self.assertIn(8952 - self.TARGET_W, self._charge_cap_writes(p))
        p.modbus.daytime_export.assert_not_called()
        p.modbus.night_export.assert_not_called()

    def test_low_pv_daytime_picks_discharge_0x05(self):
        """Daytime, surplus 1200 < 4000 -> discharge via daytime_export (mode 0x05)."""
        p = self._mk(pv_w=2000, home_w=800)        # surplus 1200
        p._drive_vpp_export()
        self.assertEqual(p.store["vpp_export_submode"], "discharge")
        p.modbus.daytime_export.assert_called_once()
        p.modbus.set_self_consumption.assert_not_called()

    def test_dark_window_always_discharge_0x06(self):
        """Dark window -> discharge via night_export (mode 0x06) regardless of PV."""
        p = self._mk(pv_w=0, home_w=600, daytime=False)
        p._drive_vpp_export()
        self.assertEqual(p.store["vpp_export_submode"], "discharge")
        p.modbus.night_export.assert_called_once()
        p.modbus.daytime_export.assert_not_called()

    def test_bank_entry_charge_cap_is_surplus_minus_target(self):
        """At the bank entry threshold (surplus = target+HYST) cap = surplus-target, >= 0."""
        p = self._mk(pv_w=4500, home_w=100)        # surplus 4400 = target+HYST -> bank
        p._drive_vpp_export()
        self.assertEqual(p.store["vpp_export_submode"], "bank")
        self.assertGreaterEqual(p.store["vpp_bank_charge_cap_w"], 0)
        self.assertEqual(p.store["vpp_bank_charge_cap_w"], 400)

    def test_inband_no_prior_mode_defaults_discharge(self):
        """In the [target, target+HYST) band with no prior sub-mode, default to
        discharge so the export is guaranteed (don't gamble on self-consumption)."""
        p = self._mk(pv_w=4300, home_w=100)        # surplus 4200, prev None
        p._drive_vpp_export()
        self.assertEqual(p.store["vpp_export_submode"], "discharge")

    def test_hysteresis_holds_submode_in_band(self):
        """In the +/-400W band around target, keep the previous sub-mode (no flap)."""
        # surplus 4100 is within [3600, 4400]; was discharging -> stays discharge
        p = self._mk(pv_w=4900, home_w=800, prev_sub="discharge")   # surplus 4100
        p._drive_vpp_export()
        self.assertEqual(p.store["vpp_export_submode"], "discharge")
        # same surplus but previously banking -> stays bank
        p2 = self._mk(pv_w=4900, home_w=800, prev_sub="bank", prev_cap=100)
        p2._drive_vpp_export()
        self.assertEqual(p2.store["vpp_export_submode"], "bank")

    def test_bank_drops_to_discharge_below_target(self):
        """Latched in bank but surplus fell below target -> discharge, so the
        battery tops the export up (guarantee). Was the <=target-HYST gap."""
        p = self._mk(pv_w=4400, home_w=700, prev_sub="bank", prev_cap=0)   # surplus 3700 < 4000
        p._drive_vpp_export()
        self.assertEqual(p.store["vpp_export_submode"], "discharge")
        p.modbus.daytime_export.assert_called_once()

    def test_bank_cap_deadband_no_rewrite(self):
        """Already banking with a near-identical cap -> no fresh charge-limit write."""
        # surplus 5000 -> cap 1000; prev cap 1100 (within 300 deadband) -> no write
        p = self._mk(pv_w=5800, home_w=800, prev_sub="bank", prev_cap=1100)
        p._drive_vpp_export()
        p.modbus.set_self_consumption.assert_not_called()   # no sub-mode change
        self.assertEqual(len(self._charge_cap_writes(p)), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
