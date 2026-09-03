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

# setdefault, not assignment — an unconditional assignment made the combined
# suite ordering-dependent (clobbering the stub test_plugin.py may already have
# installed and imported sigenergy_modbus against). The tests don't depend on
# WHICH stub wins: _make_modbus() replaces modbus.client with its own mock.
sys.modules.setdefault("pymodbus", mock_modbus_module)
sys.modules.setdefault("pymodbus.client", sys.modules["pymodbus"].client)
sys.modules.setdefault("pymodbus.exceptions", sys.modules["pymodbus"].exceptions)

# Now safe to import
from sigenergy_modbus import (
    SigenergyModbus,
    HOLD_ESS_CHARGE_CUTOFF,
    HOLD_ESS_MAX_CHARGE,
    HOLD_ESS_MAX_DISCHARGE,
    HOLD_GRID_MAX_EXPORT_LIMIT,
    HOLD_REMOTE_EMS_ENABLE,
    HOLD_REMOTE_EMS_MODE,
    decode_pv_strings,
)


# ============================================================
# Helpers
# ============================================================

def _make_modbus():
    """Return a SigenergyModbus with a mocked pymodbus client, already connected.

    The mock ECHOES writes: read_holding_registers returns whatever was last
    written to the address (falling back to [0, 10000]). Since v5.43 a
    verified write whose read-back disagrees returns False, so a mock that
    never echoed would make every verify=True write look rejected. Tests can
    reach the register store via mock_client._regs.
    """
    modbus = SigenergyModbus("192.168.1.49")
    modbus._connected        = True
    modbus._last_request_time = 0   # bypass 1-second throttle
    modbus._sleep            = lambda _s: None   # no real throttle wait — suite was ~2 min

    mock_client = MagicMock()

    regs = {}   # address -> stored word

    ok_result = MagicMock()
    ok_result.isError.return_value = False

    def _write_register(address=None, value=None, device_id=None):
        regs[address] = value
        return ok_result

    def _write_registers(address=None, values=None, device_id=None):
        for i, v in enumerate(values or []):
            regs[address + i] = v
        return ok_result

    def _read_holding(address=None, count=1, device_id=None):
        defaults = [0, 10000, 0, 0]
        r = MagicMock()
        r.isError.return_value = False
        r.registers = [regs.get(address + i, defaults[i] if i < len(defaults) else 0)
                       for i in range(count)]
        return r

    mock_client.write_register.side_effect         = _write_register
    mock_client.write_registers.side_effect        = _write_registers
    mock_client.read_holding_registers.side_effect = _read_holding
    mock_client._regs = regs

    modbus.client = mock_client
    return modbus, mock_client


def _decode_write_registers_calls(mock_client, register):
    """Return list of watt values written to a 32-bit register via write_registers.

    Explicit `in`-checks, not `.get(...) or ...` — a falsy kwarg (address 0,
    values [0, 0]) must not silently fall through to the positional args and
    misreport a zero write as None/missing.
    """
    results = []
    for c in mock_client.write_registers.call_args_list:
        kwargs = c.kwargs if c.kwargs else {}
        args   = c.args   if c.args   else ()
        addr   = kwargs["address"] if "address" in kwargs else (args[0] if args else None)
        vals   = kwargs["values"]  if "values"  in kwargs else (args[1] if len(args) > 1 else None)
        if addr == register and vals is not None:
            results.append((vals[0] << 16) | vals[1])
    return results


def _decode_single_register_calls(mock_client, register):
    """Return list of values written to a 16-bit register via write_register.

    Sentinel-aware lookup (see _decode_write_registers_calls) — value=0 via
    kwargs (e.g. the EMS-disable write, register 40029 = 0) must be recorded
    as 0, not dropped to None by a truthiness check.
    """
    results = []
    for c in mock_client.write_register.call_args_list:
        kwargs = c.kwargs if c.kwargs else {}
        args   = c.args   if c.args   else ()
        addr   = kwargs["address"] if "address" in kwargs else (args[0] if args else None)
        val    = kwargs["value"]   if "value"   in kwargs else (args[1] if len(args) > 1 else None)
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
        mock_client.read_holding_registers.side_effect  = None   # override echo harness
        mock_client.read_holding_registers.return_value = ok_read

        result = modbus.read_discharge_limit()
        self.assertEqual(result, 4000)

    def test_read_charge_limit_returns_watts(self):
        """read_charge_limit() returns the current register value in watts."""
        modbus, mock_client = _make_modbus()

        ok_read = MagicMock()
        ok_read.isError.return_value = False
        ok_read.registers             = [0, 7500]   # 7500W
        mock_client.read_holding_registers.side_effect  = None   # override echo harness
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
        mock_client.read_holding_registers.side_effect  = None   # override echo harness
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
        self.client.read_holding_registers.side_effect  = None   # override echo harness
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


from sigenergy_modbus import (   # noqa: E402
    PLANT_BATTERY_SOC, PLANT_ESS_SOH, INV_PV_STRING_BLOCK, PLANT_PV_POWER,
    SLOW_CACHE_MAX_AGE_S, SLOW_READS_PER_CYCLE, decode_s32_pair,
)


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

        # Block primitive. TWO different blocks go through it since v1.13, so
        # it must answer by ADDRESS — a fake that returned the per-string
        # block for everything fed the fast tier's 30035 read four words of
        # string data and quietly produced a 262 kW "PV power".
        def _blk(register, count, slave=None, *a, **k):
            if register in fail:
                return None
            if register == PLANT_PV_POWER:
                # 30035-30038 as two big-endian S32s: PV 5000 W, ESS -1200 W.
                return [0, 5000, 0xFFFF, 0xFB50]
            return [4, 4, 2101, 633, 3081, 624, 3146, 651, 2044, 615]
        m._read_block_u16 = _blk
        m._blk_calls = []
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

    def test_pv_strings_delivered_when_block_reads(self):
        data = self._mk().read_all()
        self.assertIsNotNone(data)
        self.assertEqual(len(data.get("pvStrings", [])), 4)
        self.assertEqual(data["pvStrings"][2]["w"], 2048)

    def test_missing_pv_string_block_is_noncritical(self):
        data = self._mk(fail_addrs={INV_PV_STRING_BLOCK}).read_all()
        self.assertIsNotNone(data)               # snapshot survives
        self.assertNotIn("pvStrings", data)      # key absent, never []-invented

    def test_pv_string_absent_latch_after_three_misses(self):
        """Three consecutive block failures on a healthy link latch the read
        off — and once latched it is NOT retried even if the register would
        now answer (re-probe is a restart, by design)."""
        m = self._mk(fail_addrs={INV_PV_STRING_BLOCK})
        # force_full so each cycle actually ATTEMPTS the block. Since v1.13 a
        # routine cycle reads only SLOW_READS_PER_CYCLE slow registers, so
        # three read_all()s are no longer three attempts at this one.
        for _ in range(3):
            self.assertIsNotNone(m.read_all(force_full=True))
        self.assertTrue(m._pv_strings_absent)
        calls = []
        def _blk_ok(register, count, slave=None, *a, **k):
            calls.append(register)
            if register == PLANT_PV_POWER:
                return [0, 5000, 0xFFFF, 0xFB50]
            return [4, 4, 2101, 633, 3081, 624, 3146, 651, 2044, 615]
        m._read_block_u16 = _blk_ok
        data = m.read_all(force_full=True)
        self.assertNotIn("pvStrings", data)
        # NB not assertEqual(calls, []) any more: since v1.13 the fast tier
        # reads 30035-30038 through this same primitive every cycle. The
        # assertion that matters is that the LATCHED block is not attempted.
        self.assertNotIn(INV_PV_STRING_BLOCK, calls)
        self.assertEqual(calls, [PLANT_PV_POWER])

    # --- read tiering (v1.13) -----------------------------------------

    def _counting(self, fail_addrs=()):
        """A modbus whose every read primitive records the address it hit."""
        m = self._mk(fail_addrs=fail_addrs)
        seen = []
        for name in ("_read_uint16", "_read_int16", "_read_int32",
                     "_read_uint32", "_read_uint64"):
            inner = getattr(m, name)
            def wrap(register, slave=None, _i=inner, *a, **k):
                seen.append(register)
                return _i(register, slave=slave)
            setattr(m, name, wrap)
        blk = m._read_block_u16
        def wrap_blk(register, count, slave=None, *a, **k):
            seen.append(register)
            return blk(register, count, slave=slave)
        m._read_block_u16 = wrap_blk
        m._seen = seen
        return m

    def test_first_cycle_is_a_full_sweep_then_cycles_get_short(self):
        """The first snapshot after a connect must carry every key, and every
        cycle after it must be a fraction of the transactions. This is the
        whole point: 29 reads at the protocol's 1 s spacing made one poll
        ~43 s measured live, so the dashboards were ~40 s stale."""
        m = self._counting()
        first = m.read_all()
        n_first = len(m._seen)
        m._seen.clear()
        m.read_all()
        n_next = len(m._seen)
        self.assertGreater(n_first, 20, "first cycle must prime every register")
        self.assertLessEqual(n_next, 5 + SLOW_READS_PER_CYCLE)
        self.assertLess(n_next * 2, n_first, "a routine cycle must be far shorter")
        # and the first sweep really did deliver the whole contract
        for key in ("emsWorkMode", "batterySoh", "pvLifetimeKwh", "gridVoltageV",
                    "batteryTempC", "ratedCapacityKwh", "homeDailyDirectKwh"):
            self.assertIn(key, first)

    def test_every_critical_key_is_read_fresh_every_cycle(self):
        """The six CRITICAL_KEYS must never be served from cache — they drive
        force-charge decisions, and a cached SOC is exactly the phantom the
        partial-read guard exists to stop."""
        m = self._counting()
        m.read_all()
        m._seen.clear()
        m.read_all()
        self.assertIn(PLANT_BATTERY_SOC, m._seen)
        self.assertIn(PLANT_PV_POWER, m._seen)      # the block covers PV + ESS

    def test_slow_keys_are_served_from_cache_not_dropped(self):
        """A cycle that did not re-read a slow register still returns its key.
        read_all()'s dict shape is a contract every consumer relies on."""
        m = self._mk()
        m.read_all()
        for _ in range(3):
            data = m.read_all()
            for key in ("emsWorkMode", "batterySoh", "ratedCapacityKwh",
                        "pvLifetimeKwh", "batteryMinTempC"):
                self.assertIn(key, data, f"{key} vanished on a routine cycle")

    def test_pv_and_battery_power_come_from_one_block(self):
        """One transaction, so the two share an instant. homePowerWatts is
        derived from them and used to absorb the spread between separate
        reads — down to 0 W on a moving day."""
        m = self._counting()
        m.read_all()
        m._seen.clear()          # count ONE cycle, not the priming one too
        data = m.read_all()
        self.assertEqual(data["pvPowerWatts"], 5000)
        self.assertEqual(data["batteryPowerWatts"], -1200)
        self.assertEqual(m._seen.count(PLANT_PV_POWER), 1)

    def test_failed_fast_block_costs_both_keys_and_returns_none(self):
        m = self._mk(fail_addrs={PLANT_PV_POWER})
        self.assertIsNone(m.read_all())     # both are critical
        self.assertTrue(m._connected)       # healthy link, transient drop

    def test_a_failed_slow_read_keeps_the_previous_value(self):
        m = self._mk()
        first = m.read_all()
        self.assertEqual(first["batterySoh"], 10.0)
        # every subsequent read of that register fails
        inner = m._read_uint16
        m._read_uint16 = lambda register, slave=None, *a, **k: (
            None if register == PLANT_ESS_SOH else inner(register, slave=slave))
        for _ in range(12):
            data = m.read_all()
        self.assertEqual(data["batterySoh"], 10.0, "last good value must survive")

    def test_a_slow_value_that_never_comes_back_is_dropped_not_served_forever(self):
        """A cached value is a real reading that is merely old. One this old is
        not stale, it is absent — and an absent reading must present as a gap,
        never as an hour-old number offered as current."""
        m = self._mk()
        m.read_all()
        aged = {k: (v, t - SLOW_CACHE_MAX_AGE_S - 1)
                for k, (v, t) in m._slow_cache.items()}
        m._slow_cache = aged
        inner = m._read_uint16
        m._read_uint16 = lambda register, slave=None, *a, **k: (
            None if register == PLANT_ESS_SOH else inner(register, slave=slave))
        data = m.read_all()
        self.assertNotIn("batterySoh", data)
        self.assertIn("batterySoc", data)    # critical data unaffected

    def test_mark_slow_read_due_jumps_the_rotation(self):
        """After a control write a cached value must not outlive the change
        that invalidated it.

        Both arms, deliberately. The key is the LAST in the rotation, so it
        would not come round on the next cycle by itself — an earlier version
        of this test marked emsWorkMode, which happens to be first in the
        list and was therefore read either way, and it passed green against a
        mark_slow_read_due() gutted to `pass`."""
        specs = self._mk()._slow_read_specs()
        last_key, _rd, last_reg, _sl, _post = [sp for sp in specs
                                               if sp[0] != "pvStrings"][-1]

        control = self._counting()
        control.read_all()
        control._seen.clear()
        control.read_all()
        self.assertNotIn(last_reg, control._seen,
                         "test is not discriminating — pick a later key")

        m = self._counting()
        m.read_all()
        m._seen.clear()
        m.mark_slow_read_due(last_key)
        m.read_all()
        self.assertIn(last_reg, m._seen)

    def test_every_slow_register_comes_round(self):
        """The rotation must not starve anything — a register that is never
        re-read would silently freeze at its first value."""
        m = self._counting()
        m.read_all()
        m._seen.clear()
        for _ in range(40):
            m.read_all()
        specs = m._slow_read_specs()
        for key, _rd, register, _sl, _post in specs:
            if key == "pvStrings":
                continue
            self.assertIn(register, m._seen, f"{key} never came round")

    def test_decode_s32_pair_matches_the_int32_primitive(self):
        self.assertEqual(decode_s32_pair([0, 5000, 0xFFFF, 0xFB50]), (5000, -1200))
        self.assertEqual(decode_s32_pair([0, 0, 0, 0]), (0, 0))
        self.assertIsNone(decode_s32_pair([0, 1, 2]))     # short block
        self.assertIsNone(decode_s32_pair(None))

    def test_majority_errors_marks_disconnected(self):
        m = SigenergyModbus("192.168.1.49")
        m._connected         = True
        m._last_request_time = 0
        m._sleep             = lambda _s: None
        for name in ("_read_uint16", "_read_int16", "_read_int32",
                     "_read_uint32", "_read_uint64", "_read_block_u16"):
            setattr(m, name, lambda *a, **k: None)   # everything fails
        self.assertIsNone(m.read_all())
        self.assertFalse(m._connected)


class TestOutageBurstHandling(unittest.TestCase):
    """v5.42: a transport-level failure (BrokenPipeError etc., NOT wrapped in
    ModbusException by pymodbus) must mark the connection dead and abort the
    rest of the read cycle — the live 01-Jul-2026 outage burned a throttled,
    ERROR-logged read per register (~25s, 20 ERROR lines) before disconnecting.

    The module-level ModbusException/ConnectionException names are rebound per
    test because this harness aliases both to Exception (which would swallow
    the generic branch under test)."""

    def _distinct_exceptions(self):
        import sigenergy_modbus as sm
        class _Distinct(Exception):
            pass
        orig = (sm.ModbusException, sm.ConnectionException)
        sm.ModbusException = sm.ConnectionException = _Distinct
        return sm, orig

    def test_generic_transport_error_marks_disconnected(self):
        modbus, mock_client = _make_modbus()
        sm, orig = self._distinct_exceptions()
        try:
            mock_client.read_holding_registers.side_effect = \
                BrokenPipeError("[Errno 32] Broken pipe")
            self.assertIsNone(modbus._read_uint16(30003))
            self.assertFalse(modbus.connected)
        finally:
            sm.ModbusException, sm.ConnectionException = orig

    def test_disconnected_guard_short_circuits_reads(self):
        # Once _connected is False, read primitives return None WITHOUT touching
        # the socket (no throttle sleep, no ERROR line per register).
        modbus, mock_client = _make_modbus()
        modbus._connected = False
        self.assertIsNone(modbus._read_uint16(30003))
        mock_client.read_holding_registers.assert_not_called()

    def test_read_all_aborts_early_after_transport_failure(self):
        # First register read raises BrokenPipeError -> whole cycle aborts:
        # exactly ONE socket call, not 20.
        modbus, mock_client = _make_modbus()
        modbus.connect = lambda: False   # no mid-cycle reconnect
        sm, orig = self._distinct_exceptions()
        try:
            mock_client.read_holding_registers.side_effect = \
                BrokenPipeError("[Errno 32] Broken pipe")
            self.assertIsNone(modbus.read_all())
            self.assertEqual(mock_client.read_holding_registers.call_count, 1)
        finally:
            sm.ModbusException, sm.ConnectionException = orig


class TestForceChargeCutoffBackstop(unittest.TestCase):
    """v5.42: force_charge(cutoff_soc=) writes HOLD_ESS_CHARGE_CUTOFF (40047) as
    a hardware ceiling so a plugin crash mid-import cannot grid-charge to 100%."""

    def test_force_charge_writes_cutoff_when_given(self):
        modbus, mock_client = _make_modbus()
        self.assertTrue(modbus.force_charge(10000, cutoff_soc=52.0))
        writes = [(c.kwargs.get("address"), c.kwargs.get("value"))
                  for c in mock_client.write_register.call_args_list]
        self.assertIn((HOLD_ESS_CHARGE_CUTOFF, 520), writes)   # gain 10

    def test_force_charge_without_cutoff_leaves_register_alone(self):
        modbus, mock_client = _make_modbus()
        self.assertTrue(modbus.force_charge(10000))
        addrs = [c.kwargs.get("address")
                 for c in mock_client.write_register.call_args_list]
        self.assertNotIn(HOLD_ESS_CHARGE_CUTOFF, addrs)

    def test_cutoff_write_failure_does_not_fail_the_import(self):
        # The backstop is best-effort: if the cutoff write fails the import must
        # still report success (mode 0x03 is already latched — returning False
        # would leave it running with the plugin believing no import started).
        modbus, mock_client = _make_modbus()
        ok_result = MagicMock()
        ok_result.isError.return_value = False
        bad_result = MagicMock()
        bad_result.isError.return_value = True

        def _write(address=None, value=None, device_id=None):
            if address == HOLD_ESS_CHARGE_CUTOFF:
                return bad_result
            mock_client._regs[address] = value   # echo so verify passes
            return ok_result
        mock_client.write_register.side_effect = _write
        self.assertTrue(modbus.force_charge(10000, cutoff_soc=52.0))




class TestInverterMaxIsNotHardcoded(unittest.TestCase):
    """v5.65.0 — the rated power lives on the object, not as a literal 10000.

    `set_self_consumption()` reset both persistent limit registers to a hardcoded
    10000 W. That is exactly right on this 10 kW inverter and silently wrong on
    any other: on a 15 kW machine every return to self-consumption capped battery
    discharge at 10 kW until the next verify pass re-asserted it, and the mode
    register read perfectly correct throughout. Invisible here, which is why
    three prior reviews walked past it.
    """

    def test_self_consumption_uses_the_configured_rating(self):
        modbus, mock_client = _make_modbus()
        modbus.inverter_max_w = 15000

        modbus.set_self_consumption()

        discharge = _decode_write_registers_calls(mock_client, HOLD_ESS_MAX_DISCHARGE)
        charge    = _decode_write_registers_calls(mock_client, HOLD_ESS_MAX_CHARGE)
        self.assertEqual(discharge[-1], 15000,
            "discharge limit must follow the configured rating, not a literal 10000")
        self.assertEqual(charge[-1], 15000,
            "charge limit must follow the configured rating, not a literal 10000")

    def test_default_rating_preserves_existing_behaviour(self):
        """A 10 kW install must be byte-for-byte unchanged by this fix."""
        modbus, mock_client = _make_modbus()

        modbus.set_self_consumption()

        self.assertEqual(modbus.inverter_max_w, 10000)
        self.assertEqual(
            _decode_write_registers_calls(mock_client, HOLD_ESS_MAX_DISCHARGE)[-1], 10000)

    def test_export_modes_default_to_the_configured_rating(self):
        for method in ("night_export", "daytime_export"):
            with self.subTest(method=method):
                modbus, mock_client = _make_modbus()
                modbus.inverter_max_w = 15000

                getattr(modbus, method)()

                discharge = _decode_write_registers_calls(mock_client, HOLD_ESS_MAX_DISCHARGE)
                self.assertEqual(discharge[-1], 15000,
                    f"{method} must fall back to the object's rating")

    def test_explicit_argument_still_wins(self):
        modbus, mock_client = _make_modbus()
        modbus.inverter_max_w = 15000

        modbus.night_export(4000)

        self.assertEqual(
            _decode_write_registers_calls(mock_client, HOLD_ESS_MAX_DISCHARGE)[-1], 4000)


class TestDaytimeExportWriteOrder(unittest.TestCase):
    """v5.65.0 — charge limit 0 must be written BEFORE mode 0x05 is committed.

    The whole reason daytime_export pins charge to 0 is that in mode 0x05 with the
    charge limit open, high PV banks into the battery instead of going to grid —
    measured on hardware 15-Jun-2026. Committing 0x05 first and pinning the limit
    two writes later re-opens that exact window, and set_self_consumption leaves
    the charge limit at inverter max, so the window is entered in precisely the
    wrong state. Nothing downstream can detect it: the mode register reads a
    correct 0x05 while the paid window exports nothing.
    """

    @staticmethod
    def _record_order(modbus):
        """Spy on the two calls whose ORDER is the contract, in one shared list.

        The client's 16-bit and 32-bit writes go through separate mocks, so their
        relative order is not recoverable from call_args_list. Spying on the
        modbus methods records the one ordering that matters and reads as the
        guarantee itself.
        """
        order = []
        real_limit, real_mode = modbus.set_charge_limit, modbus.set_remote_ems_mode

        def limit(watts, *a, **k):
            order.append(("charge_limit", watts))
            return real_limit(watts, *a, **k)

        def mode(value, *a, **k):
            order.append(("mode", value))
            return real_mode(value, *a, **k)

        modbus.set_charge_limit    = limit
        modbus.set_remote_ems_mode = mode
        return order

    def test_charge_limit_zero_precedes_the_mode_commit(self):
        modbus, _ = _make_modbus()
        order = self._record_order(modbus)

        self.assertTrue(modbus.daytime_export())

        self.assertIn(("charge_limit", 0), order,
                      "daytime_export must pin the charge limit to 0")
        self.assertIn(("mode", 0x05), order,
                      "daytime_export must commit mode 0x05")
        self.assertLess(
            order.index(("charge_limit", 0)), order.index(("mode", 0x05)),
            "charge limit 0 must be written BEFORE mode 0x05 — committing the mode "
            "first re-opens the greedy-charge window this method exists to close")

    def test_night_export_is_unaffected(self):
        """0x06 never pinned charge to 0 — this fix must not change it."""
        modbus, _ = _make_modbus()
        order = self._record_order(modbus)

        self.assertTrue(modbus.night_export())

        self.assertNotIn(("charge_limit", 0), order,
                         "night_export must not start pinning the charge limit")


class TestDecodePvStrings(unittest.TestCase):
    """decode_pv_strings — the pure per-string block decoder (v1.8).

    The happy-path fixture is the REAL block read off the live inverter on
    13-08-2026 (plant total PV 6142 W at the same instant), not an invented
    shape — the fixture-shares-the-mistake lesson from v5.60.0."""

    LIVE_BLOCK = [4, 4, 2101, 633, 3081, 624, 3146, 651, 2044, 615]

    def test_live_probe_block_decodes_four_strings(self):
        out = decode_pv_strings(self.LIVE_BLOCK)
        self.assertEqual(len(out), 4)
        self.assertEqual(out[0], {"v": 210.1, "a": 6.33, "w": 1330})
        self.assertEqual(out[1], {"v": 308.1, "a": 6.24, "w": 1923})
        self.assertEqual(out[2], {"v": 314.6, "a": 6.51, "w": 2048})
        self.assertEqual(out[3], {"v": 204.4, "a": 6.15, "w": 1257})

    def test_live_probe_powers_sum_near_plant_total(self):
        # DC-side string powers exceed the AC plant total by conversion losses
        # — measured +6.8% on the live probe. Pin the band loosely so a decode
        # regression (wrong gain, swapped V/I) fails loudly: a gain slip is a
        # factor of 10, nowhere near the band.
        total = sum(s["w"] for s in decode_pv_strings(self.LIVE_BLOCK))
        self.assertGreater(total, 6142 * 0.95)
        self.assertLess(total, 6142 * 1.20)

    def test_count_register_trims_the_pairs(self):
        two = decode_pv_strings([2, 2, 2101, 633, 3081, 624, 0, 0, 0, 0])
        self.assertEqual(len(two), 2)
        self.assertEqual(two[1]["w"], 1923)

    def test_count_beyond_available_pairs_is_capped(self):
        # A bigger inverter reporting 6 strings through a 10-register read
        # yields the 4 pairs the block carries, not an index error.
        out = decode_pv_strings([6, 6, 2101, 633, 3081, 624, 3146, 651, 2044, 615])
        self.assertEqual(len(out), 4)

    def test_night_block_decodes_to_zero_watt_strings(self):
        out = decode_pv_strings([4, 4, 0, 0, 0, 0, 0, 0, 0, 0])
        self.assertEqual(len(out), 4)
        self.assertTrue(all(s["w"] == 0 for s in out))

    def test_malformed_input_returns_empty_never_invents(self):
        self.assertEqual(decode_pv_strings(None), [])
        self.assertEqual(decode_pv_strings([]), [])
        self.assertEqual(decode_pv_strings([4]), [])
        self.assertEqual(decode_pv_strings([0, 0, 1, 2]), [])   # count 0

    # --- signed V/I (v5.86.0) ------------------------------------------
    # The happy-path fixture above was taken in full sun, where every word is
    # positive and the sign can never show. These are the dusk shapes.

    # Reconstructed exactly from the four simultaneous live states at
    # 2026-09-03 18:40:25, while South (PV3) sat at its zero-crossing:
    # V 218.5/327.6/327.8/219.2, I 0.11/0.11/655.34/0.03. The decode is a
    # pure divide, so multiplying back is lossless.
    DUSK_BLOCK = [4, 4, 2185, 11, 3276, 11, 3278, 65534, 2192, 3]

    def test_dusk_negative_current_is_signed_not_655_amps(self):
        out = decode_pv_strings(self.DUSK_BLOCK)
        self.assertEqual(len(out), 4)
        south = out[2]
        # 65534 is -2 as S16 -> -0.02 A, NOT 655.34 A.
        self.assertEqual(south["a"], -0.02)
        self.assertEqual(south["v"], 327.8)
        # The bug this pins: V*I unsigned put 214,853 W on a 4.275 kWp string.
        self.assertEqual(south["w"], 0)

    def test_dusk_block_leaves_the_healthy_strings_alone(self):
        out = decode_pv_strings(self.DUSK_BLOCK)
        self.assertEqual([s["a"] for s in out], [0.11, 0.11, -0.02, 0.03])
        self.assertEqual([s["w"] for s in out], [24, 36, 0, 7])

    def test_no_string_may_exceed_its_physical_limits(self):
        # Every observed wrap sat in 65532..65535; walk the whole run plus a
        # deeper negative, and assert none of them can reach the dashboard.
        for raw in (65532, 65533, 65534, 65535, 65000):
            with self.subTest(raw=raw):
                out = decode_pv_strings([1, 1, 3278, raw, 0, 0, 0, 0, 0, 0])
                self.assertTrue(all(s["w"] < 20000 for s in out),
                                f"raw {raw} produced {out}")

    def test_implausible_pair_discards_the_whole_block(self):
        # A misaligned block has no trustworthy members — [], never a
        # partial list that reads as a real measurement.
        self.assertEqual(decode_pv_strings([4, 4, 30000, 5000, 3081, 624,
                                            3146, 651, 2044, 615]), [])

    def test_watts_are_never_negative(self):
        out = decode_pv_strings([1, 1, 3278, 65436, 0, 0, 0, 0, 0, 0])  # -1.00 A
        self.assertEqual(out[0]["a"], -1.0)
        self.assertGreaterEqual(out[0]["w"], 0)


if __name__ == "__main__":
    print("Running SigenEnergyManager Modbus register tests")
    unittest.main(verbosity=2)
