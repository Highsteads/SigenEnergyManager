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
from unittest.mock import MagicMock, call

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
    modbus = SigenergyModbus("192.168.100.49")
    modbus._connected        = True
    modbus._last_request_time = 0   # bypass 1-second throttle

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


if __name__ == "__main__":
    print("Running SigenEnergyManager Modbus register tests")
    unittest.main(verbosity=2)
