#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    test_sigenergy_modbus.py
# Description: Unit tests for sigenergy_modbus.py persistent register handling.
#              Specifically tests that mode transitions correctly reset
#              HOLD_ESS_MAX_DISCHARGE (40034) and HOLD_ESS_MAX_CHARGE (40032).
#              Runs without Indigo installed — uses unittest.mock for pymodbus.
# Author:      CliveS & Claude Sonnet 4.6
# Date:        28-03-2026
# Version:     1.0

import sys
import unittest
from unittest.mock import MagicMock

# ============================================================
# Patch pymodbus before importing sigenergy_modbus
# ============================================================

mock_modbus_module = MagicMock()

# ModbusTcpClient is instantiated; connect() must return True
mock_client_instance = MagicMock()
mock_client_instance.connect.return_value = True

mock_modbus_module.client.ModbusTcpClient.return_value = mock_client_instance
mock_modbus_module.exceptions.ModbusException      = Exception
mock_modbus_module.exceptions.ConnectionException  = Exception

sys.modules["pymodbus"]                   = mock_modbus_module
sys.modules["pymodbus.client"]            = mock_modbus_module.client
sys.modules["pymodbus.exceptions"]        = mock_modbus_module.exceptions

# Now safe to import
from sigenergy_modbus import (
    SigenergyModbus,
    HOLD_ESS_MAX_CHARGE,
    HOLD_ESS_MAX_DISCHARGE,
    HOLD_GRID_MAX_EXPORT_LIMIT,
    HOLD_REMOTE_EMS_ENABLE,
    HOLD_REMOTE_EMS_MODE,
)


# ============================================================
# Helpers
# ============================================================

def _make_modbus():
    """Return a SigenergyModbus with a mocked pymodbus client, already connected."""
    modbus = SigenergyModbus("192.168.1.49")
    modbus._connected        = True
    modbus._last_request_time = 0   # bypass 1-second throttle
    modbus._sleep            = lambda _s: None   # no real throttle wait — suite was ~2 min

    mock_client = MagicMock()

    # All single-register writes succeed
    ok_result = MagicMock()
    ok_result.isError.return_value = False
    mock_client.write_register.return_value  = ok_result
    mock_client.write_registers.return_value = ok_result

    # All reads return 10000W by default ([high=0, low=10000])
    ok_read = MagicMock()
    ok_read.isError.return_value = False
    ok_read.registers             = [0, 10000]
    mock_client.read_holding_registers.return_value = ok_read

    modbus.client = mock_client
    return modbus, mock_client


def _decode_write_registers_calls(mock_client, register):
    """Return list of watt values written to a 32-bit register via write_registers."""
    results = []
    for c in mock_client.write_registers.call_args_list:
        kwargs = c.kwargs if c.kwargs else {}
        args   = c.args   if c.args   else ()
        addr   = kwargs.get("address") or (args[0] if args else None)
        vals   = kwargs.get("values")  or (args[1] if len(args) > 1 else None)
        if addr == register and vals is not None:
            results.append((vals[0] << 16) | vals[1])
    return results


def _decode_single_register_calls(mock_client, register):
    """Return list of values written to a 16-bit register via write_register."""
    results = []
    for c in mock_client.write_register.call_args_list:
        kwargs = c.kwargs if c.kwargs else {}
        args   = c.args   if c.args   else ()
        addr   = kwargs.get("address") or (args[0] if args else None)
        val    = kwargs.get("value")   or (args[1] if len(args) > 1 else None)
        if addr == register:
            results.append(val)
    return results


# ============================================================
# Tests: set_self_consumption register resets
# ============================================================

class TestSetSelfConsumptionResetsLimits(unittest.TestCase):
    """set_self_consumption() must reset both persistent power limit registers."""

    def test_resets_discharge_limit_to_10000w(self):
        """HOLD_ESS_MAX_DISCHARGE must be written to 10000W."""
        modbus, mock_client = _make_modbus()
        modbus.set_self_consumption()

        discharge_writes = _decode_write_registers_calls(mock_client, HOLD_ESS_MAX_DISCHARGE)
        self.assertGreater(len(discharge_writes), 0,
            "set_self_consumption must write to HOLD_ESS_MAX_DISCHARGE")
        self.assertEqual(discharge_writes[-1], 10000,
            "Discharge limit must be reset to 10000W (inverter max)")

    def test_resets_charge_limit_to_10000w(self):
        """HOLD_ESS_MAX_CHARGE must be written to 10000W."""
        modbus, mock_client = _make_modbus()
        modbus.set_self_consumption()

        charge_writes = _decode_write_registers_calls(mock_client, HOLD_ESS_MAX_CHARGE)
        self.assertGreater(len(charge_writes), 0,
            "set_self_consumption must write to HOLD_ESS_MAX_CHARGE")
        self.assertEqual(charge_writes[-1], 10000,
            "Charge limit must be reset to 10000W (inverter max)")

    def test_enables_remote_ems(self):
        """Remote EMS enable register must be set to 1."""
        modbus, mock_client = _make_modbus()
        modbus.set_self_consumption()

        ems_enable_writes = _decode_single_register_calls(mock_client, HOLD_REMOTE_EMS_ENABLE)
        self.assertIn(1, ems_enable_writes,
            "set_self_consumption must enable Remote EMS (register 40029 = 1)")

    def test_sets_mode_0x02(self):
        """Remote EMS mode must be set to 0x02 (Max Self Consumption)."""
        modbus, mock_client = _make_modbus()
        modbus.set_self_consumption()

        mode_writes = _decode_single_register_calls(mock_client, HOLD_REMOTE_EMS_MODE)
        self.assertIn(0x02, mode_writes,
            "set_self_consumption must set mode to 0x02 (Max Self Consumption)")


# ============================================================
# Tests: force_discharge + set_self_consumption sequence
# ============================================================

class TestForceDischargeSequence(unittest.TestCase):
    """Validates the force_discharge -> set_self_consumption transition."""

    def test_discharge_limit_cleared_after_force_discharge(self):
        """Discharge limit must be restored to 10000W when returning to SC after force_discharge."""
        modbus, mock_client = _make_modbus()

        # Simulate night export at 2000W (as occurred during staged export testing)
        modbus.force_discharge(2000)
        discharge_after_force = _decode_write_registers_calls(mock_client, HOLD_ESS_MAX_DISCHARGE)
        self.assertIn(2000, discharge_after_force,
            "force_discharge(2000) must write 2000W to HOLD_ESS_MAX_DISCHARGE")

        # Return to self-consumption
        modbus.set_self_consumption()
        discharge_all = _decode_write_registers_calls(mock_client, HOLD_ESS_MAX_DISCHARGE)
        self.assertEqual(discharge_all[-1], 10000,
            "Final HOLD_ESS_MAX_DISCHARGE after set_self_consumption must be 10000W")

    def test_discharge_limit_at_4kw_for_night_export(self):
        """force_discharge(4000) writes exactly 4000W to the discharge register."""
        modbus, mock_client = _make_modbus()
        modbus.force_discharge(4000)

        discharge_writes = _decode_write_registers_calls(mock_client, HOLD_ESS_MAX_DISCHARGE)
        self.assertIn(4000, discharge_writes)

    def test_multiple_force_discharge_then_sc_always_clears(self):
        """Even after multiple force_discharge calls, SC always restores 10000W."""
        modbus, mock_client = _make_modbus()

        # Simulate export at various powers then stop
        for power in (1000, 2000, 4000):
            modbus.force_discharge(power)

        modbus.set_self_consumption()
        discharge_all = _decode_write_registers_calls(mock_client, HOLD_ESS_MAX_DISCHARGE)
        self.assertEqual(discharge_all[-1], 10000)


# ============================================================
# Tests: force_charge + set_self_consumption sequence
# ============================================================

class TestForceChargeSequence(unittest.TestCase):
    """Validates the force_charge -> set_self_consumption transition."""

    def test_charge_limit_cleared_after_force_charge(self):
        """Charge limit must be restored to 10000W when returning to SC after force_charge."""
        modbus, mock_client = _make_modbus()

        modbus.force_charge(5000)
        charge_after_force = _decode_write_registers_calls(mock_client, HOLD_ESS_MAX_CHARGE)
        self.assertIn(5000, charge_after_force,
            "force_charge(5000) must write 5000W to HOLD_ESS_MAX_CHARGE")

        modbus.set_self_consumption()
        charge_all = _decode_write_registers_calls(mock_client, HOLD_ESS_MAX_CHARGE)
        self.assertEqual(charge_all[-1], 10000,
            "Final HOLD_ESS_MAX_CHARGE after set_self_consumption must be 10000W")

    def test_discharge_limit_not_affected_by_force_charge(self):
        """force_charge does not write to HOLD_ESS_MAX_DISCHARGE."""
        modbus, mock_client = _make_modbus()
        modbus.force_charge(10000)

        discharge_writes = _decode_write_registers_calls(mock_client, HOLD_ESS_MAX_DISCHARGE)
        self.assertEqual(len(discharge_writes), 0,
            "force_charge must not touch HOLD_ESS_MAX_DISCHARGE")


# ============================================================
# Tests: read_discharge_limit / read_charge_limit
# ============================================================

class TestReadLimits(unittest.TestCase):
    """Tests for reading back the current power limit registers."""

    def test_read_discharge_limit_returns_watts(self):
        """read_discharge_limit() returns the current register value in watts."""
        modbus, mock_client = _make_modbus()

        ok_read = MagicMock()
        ok_read.isError.return_value = False
        ok_read.registers             = [0, 4000]   # 4000W
        mock_client.read_holding_registers.return_value = ok_read

        result = modbus.read_discharge_limit()
        self.assertEqual(result, 4000)

    def test_read_charge_limit_returns_watts(self):
        """read_charge_limit() returns the current register value in watts."""
        modbus, mock_client = _make_modbus()

        ok_read = MagicMock()
        ok_read.isError.return_value = False
        ok_read.registers             = [0, 7500]   # 7500W
        mock_client.read_holding_registers.return_value = ok_read

        result = modbus.read_charge_limit()
        self.assertEqual(result, 7500)

    def test_read_discharge_limit_returns_none_when_disconnected(self):
        """read_discharge_limit() returns None when not connected."""
        modbus, _ = _make_modbus()
        modbus._connected = False
        self.assertIsNone(modbus.read_discharge_limit())

    def test_read_charge_limit_returns_none_when_disconnected(self):
        """read_charge_limit() returns None when not connected."""
        modbus, _ = _make_modbus()
        modbus._connected = False
        self.assertIsNone(modbus.read_charge_limit())

    def test_read_discharge_limit_handles_large_value(self):
        """Discharge limit handles values > 65535 (split across two 16-bit registers)."""
        modbus, mock_client = _make_modbus()

        # 70000W = 0x00011170: high=1, low=4464
        ok_read = MagicMock()
        ok_read.isError.return_value = False
        ok_read.registers             = [1, 4464]
        mock_client.read_holding_registers.return_value = ok_read

        result = modbus.read_discharge_limit()
        self.assertEqual(result, (1 << 16) | 4464)


# ============================================================
# Tests: export limit
# ============================================================

class TestExportLimit(unittest.TestCase):
    """Tests for set_export_limit."""

    def test_set_export_limit_writes_correct_register(self):
        """set_export_limit(4000) writes 4000W to HOLD_GRID_MAX_EXPORT_LIMIT."""
        modbus, mock_client = _make_modbus()
        modbus.set_export_limit(4000)

        export_writes = _decode_write_registers_calls(mock_client, HOLD_GRID_MAX_EXPORT_LIMIT)
        self.assertIn(4000, export_writes)

    def test_set_export_limit_rejects_negative(self):
        """set_export_limit() with negative value returns False and does not write."""
        modbus, mock_client = _make_modbus()
        result = modbus.set_export_limit(-1)
        self.assertFalse(result)
        export_writes = _decode_write_registers_calls(mock_client, HOLD_GRID_MAX_EXPORT_LIMIT)
        self.assertEqual(len(export_writes), 0)


# ============================================================
# Tests: night_export()
# ============================================================

class TestNightExportMethod(unittest.TestCase):
    """Tests for night_export() — battery-to-grid export with house load supplied in addition.

    night_export() must:
      - Set mode 0x06 (Discharge ESS First)
      - Set HOLD_ESS_MAX_DISCHARGE = inverter_max_w (uncapped — house load + grid export)
      - NOT write HOLD_GRID_MAX_EXPORT_LIMIT (inverter's own DNO cap handles that)

    Battery discharges at (house_load + grid_export), up to inverter_max_w.
    The inverter's own configured export cap enforces the DNO limit automatically.
    """

    def test_night_export_sets_mode_0x06(self):
        """night_export() activates Discharge ESS First mode (0x06)."""
        modbus, mock_client = _make_modbus()
        modbus.night_export(10000)

        mode_writes = _decode_single_register_calls(mock_client, HOLD_REMOTE_EMS_MODE)
        self.assertIn(0x06, mode_writes)

    def test_night_export_sets_discharge_to_inverter_max(self):
        """night_export() sets HOLD_ESS_MAX_DISCHARGE = inverter_max_w.

        Battery must be uncapped so it can supply house_load + grid_export simultaneously.
        """
        modbus, mock_client = _make_modbus()
        modbus.night_export(10000)

        discharge_writes = _decode_write_registers_calls(mock_client, HOLD_ESS_MAX_DISCHARGE)
        self.assertIn(10000, discharge_writes)

    def test_night_export_does_not_write_export_limit_register(self):
        """night_export() does NOT write HOLD_GRID_MAX_EXPORT_LIMIT.

        The inverter's own DNO export cap (set during commissioning) handles grid
        limiting — writing this register from software is redundant and risks
        interfering with daytime staged export logic.
        """
        modbus, mock_client = _make_modbus()
        modbus.night_export(10000)

        export_writes = _decode_write_registers_calls(mock_client, HOLD_GRID_MAX_EXPORT_LIMIT)
        self.assertEqual(len(export_writes), 0)

    def test_night_export_enables_remote_ems(self):
        """night_export() enables Remote EMS before setting mode."""
        modbus, mock_client = _make_modbus()
        modbus.night_export(10000)

        ems_enable_writes = _decode_single_register_calls(mock_client, HOLD_REMOTE_EMS_ENABLE)
        self.assertIn(1, ems_enable_writes)

    def test_sc_after_night_export_resets_discharge_to_inverter_max(self):
        """Returning to SC after night_export resets discharge limit to 10000W."""
        modbus, mock_client = _make_modbus()
        modbus.night_export(10000)
        mock_client.reset_mock()
        modbus.set_self_consumption()

        discharge_writes = _decode_write_registers_calls(mock_client, HOLD_ESS_MAX_DISCHARGE)
        self.assertIn(10000, discharge_writes)


class TestDaytimeExportMethod(unittest.TestCase):
    """Tests for daytime_export() — PV-first grid export (mode 0x05).

    daytime_export() must:
      - Set mode 0x05 (Discharge PV First) — NOT 0x06
      - Set HOLD_ESS_MAX_DISCHARGE = inverter_max_w (battery free to cover shortfall)
      - Set HOLD_ESS_MAX_CHARGE = inverter_max_w (charge left open so excess PV
        charges the battery rather than being curtailed)
      - NOT write HOLD_GRID_MAX_EXPORT_LIMIT (inverter's own DNO cap handles that)
      - Enable Remote EMS first
    """

    def test_daytime_export_sets_mode_0x05(self):
        """daytime_export() activates Discharge PV First mode (0x05), not 0x06."""
        modbus, mock_client = _make_modbus()
        modbus.daytime_export(10000)

        mode_writes = _decode_single_register_calls(mock_client, HOLD_REMOTE_EMS_MODE)
        self.assertIn(0x05, mode_writes)
        self.assertNotIn(0x06, mode_writes)

    def test_daytime_export_sets_discharge_to_inverter_max(self):
        """daytime_export() sets HOLD_ESS_MAX_DISCHARGE = inverter_max_w."""
        modbus, mock_client = _make_modbus()
        modbus.daytime_export(10000)

        discharge_writes = _decode_write_registers_calls(mock_client, HOLD_ESS_MAX_DISCHARGE)
        self.assertIn(10000, discharge_writes)

    def test_daytime_export_pins_charge_to_zero(self):
        """daytime_export() sets HOLD_ESS_MAX_CHARGE = 0 so PV is forced to the grid.

        With the charge limit left open, mode 0x05 charges the battery with PV
        surplus instead of exporting when PV is high (confirmed on hardware
        15-Jun-2026). Pinning charge to 0 removes that competing path.
        """
        modbus, mock_client = _make_modbus()
        modbus.daytime_export(10000)

        charge_writes = _decode_write_registers_calls(mock_client, HOLD_ESS_MAX_CHARGE)
        self.assertIn(0, charge_writes)
        self.assertNotIn(10000, charge_writes)

    def test_daytime_export_does_not_write_export_limit_register(self):
        """daytime_export() does NOT write HOLD_GRID_MAX_EXPORT_LIMIT (DNO cap handles it)."""
        modbus, mock_client = _make_modbus()
        modbus.daytime_export(10000)

        export_writes = _decode_write_registers_calls(mock_client, HOLD_GRID_MAX_EXPORT_LIMIT)
        self.assertEqual(len(export_writes), 0)

    def test_daytime_export_enables_remote_ems(self):
        """daytime_export() enables Remote EMS before setting mode."""
        modbus, mock_client = _make_modbus()
        modbus.daytime_export(10000)

        ems_enable_writes = _decode_single_register_calls(mock_client, HOLD_REMOTE_EMS_ENABLE)
        self.assertIn(1, ems_enable_writes)

    def test_sc_after_daytime_export_resets_limits_to_inverter_max(self):
        """Returning to SC after daytime_export resets discharge limit to 10000W."""
        modbus, mock_client = _make_modbus()
        modbus.daytime_export(10000)
        mock_client.reset_mock()
        modbus.set_self_consumption()

        discharge_writes = _decode_write_registers_calls(mock_client, HOLD_ESS_MAX_DISCHARGE)
        self.assertIn(10000, discharge_writes)


class TestSignedRegisterDecode(unittest.TestCase):
    """Signed 16/32-bit Modbus decode — negative, boundary and positive paths.
    Previously only the positive path was exercised (via read_all); a sign-extension
    slip would silently turn a -700W battery discharge into +64836W."""

    def setUp(self):
        self.modbus, self.client = _make_modbus()
        self.modbus._sleep = lambda _s: None   # no real throttle wait in tests

    def _set_registers(self, registers):
        r = MagicMock()
        r.isError.return_value = False
        r.registers = registers
        self.client.read_holding_registers.return_value = r

    # --- signed 16-bit ---
    def test_int16_negative(self):
        self._set_registers([65486])          # -50 as unsigned 16-bit
        self.assertEqual(self.modbus._read_int16(30000), -50)

    def test_int16_min_boundary(self):
        self._set_registers([0x8000])         # 32768 -> -32768
        self.assertEqual(self.modbus._read_int16(30000), -32768)

    def test_int16_max_positive(self):
        self._set_registers([0x7FFF])         # 32767
        self.assertEqual(self.modbus._read_int16(30000), 32767)

    # --- signed 32-bit (big-endian word order [hi, lo]) ---
    def test_int32_negative(self):
        self._set_registers([0xFFFF, 0xFC18])  # 0xFFFFFC18 -> -1000
        self.assertEqual(self.modbus._read_int32(30000), -1000)

    def test_int32_min_boundary(self):
        self._set_registers([0x8000, 0x0000])  # -2147483648
        self.assertEqual(self.modbus._read_int32(30000), -2147483648)

    def test_int32_positive(self):
        self._set_registers([0x0001, 0x0000])  # 65536
        self.assertEqual(self.modbus._read_int32(30000), 65536)


from sigenergy_modbus import PLANT_BATTERY_SOC, PLANT_ESS_SOH   # noqa: E402


class TestReadAllPartial(unittest.TestCase):
    """read_all() must NEVER fabricate a 0 — the phantom-0%-SOC force-charge guard.
    A partial read that drops a CRITICAL register returns None (so _poll_modbus keeps
    the last-known-good snapshot, NOT a fake 0% SOC); a non-critical drop returns a
    dict omitting that key; a majority-failed read marks the connection disconnected."""

    def _mk(self, fail_addrs=()):
        m = SigenergyModbus("192.168.1.49")
        m._connected         = True
        m._last_request_time = 0
        m._sleep             = lambda _s: None
        fail = set(fail_addrs)

        def _rd(default):
            def _f(register, slave=None, *a, **k):
                return None if register in fail else default
            return _f
        # Benign non-zero value for every read primitive; failed addresses -> None.
        for name in ("_read_uint16", "_read_int16", "_read_int32",
                     "_read_uint32", "_read_uint64"):
            setattr(m, name, _rd(100))
        return m

    def test_all_present_returns_dict(self):
        data = self._mk().read_all()
        self.assertIsNotNone(data)
        self.assertIn("batterySoc", data)

    def test_missing_critical_soc_returns_none_connection_kept(self):
        m = self._mk(fail_addrs={PLANT_BATTERY_SOC})
        self.assertIsNone(m.read_all())          # never act on fabricated 0% SOC
        self.assertTrue(m._connected)            # healthy link, just a transient drop

    def test_missing_noncritical_soh_returns_dict_without_key(self):
        data = self._mk(fail_addrs={PLANT_ESS_SOH}).read_all()
        self.assertIsNotNone(data)
        self.assertNotIn("batterySoh", data)
        self.assertIn("batterySoc", data)        # critical data still delivered

    def test_majority_errors_marks_disconnected(self):
        m = SigenergyModbus("192.168.1.49")
        m._connected         = True
        m._last_request_time = 0
        m._sleep             = lambda _s: None
        for name in ("_read_uint16", "_read_int16", "_read_int32",
                     "_read_uint32", "_read_uint64"):
            setattr(m, name, lambda *a, **k: None)   # everything fails
        self.assertIsNone(m.read_all())
        self.assertFalse(m._connected)


if __name__ == "__main__":
    print("Running SigenEnergyManager Modbus register tests")
    unittest.main(verbosity=2)
