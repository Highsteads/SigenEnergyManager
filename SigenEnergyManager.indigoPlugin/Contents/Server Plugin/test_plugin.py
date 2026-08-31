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
import threading
import sqlite3
import tempfile
import logging
import types
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

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

# setdefault, not assignment — an unconditional assignment made the combined
# suite ordering-dependent (this file would clobber the pymodbus stub another
# test file had already installed and imported against).
_pm = MagicMock()
sys.modules.setdefault("pymodbus", _pm)
sys.modules.setdefault("pymodbus.client", sys.modules["pymodbus"].client)
sys.modules.setdefault("pymodbus.exceptions", sys.modules["pymodbus"].exceptions)
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

    # --- _num_state: real number stored + decimalPlaces hint, no uiValue ---
    def test_num_state_rounds_value_with_dp(self):
        st = plugin._num_state("batterySoc", 99.59999999999999, 1)
        self.assertEqual(st["key"], "batterySoc")
        self.assertEqual(st["value"], 99.6)
        self.assertEqual(st["decimalPlaces"], 1)
        self.assertNotIn("uiValue", st)               # avoids a <state>_ui history column

    def test_num_state_guards_bad_value(self):
        st = plugin._num_state("x", None, 2)
        self.assertEqual(st["value"], 0.0)
        self.assertEqual(st["decimalPlaces"], 2)


class TestSolarOverflowShadow(unittest.TestCase):
    """The 90%/95% and tariff comparison must remain reporting-only."""

    def test_pacing_shadow_accumulates_only_the_export_delta(self):
        p = plugin.Plugin.__new__(plugin.Plugin)
        p.pluginPrefs = {"solarOverflowShadowEnabled": True}
        p.store = {"shadow_95_export_foregone_kwh": 0.0, "shadow_95_samples": 0}
        p.logger = MagicMock()
        p.manager = MagicMock()
        p.manager._calculate_24h_balance.return_value = object()
        p.manager._check_solar_overflow.return_value = types.SimpleNamespace(export_kw=1.5)
        snap = types.SimpleNamespace(storm_active=False, solar_overflow_target_pct=90.0)
        live = types.SimpleNamespace(action=plugin.ACTION_SOLAR_OVERFLOW, export_kw=2.5)

        p._record_solar_overflow_shadow(snap, live)

        self.assertEqual(p.store["shadow_95_samples"], 1)
        self.assertAlmostEqual(p.store["shadow_95_export_foregone_kwh"], 1.0 / 60.0)
        self.assertEqual(snap.solar_overflow_target_pct, 90.0)  # copy, never mutate live snapshot

    def test_tariff_baseline_requires_complete_price_coverage(self):
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "energy_timeseries.db")
            con = sqlite3.connect(db)
            con.execute("""CREATE TABLE halfhourly (
                slot_end TEXT, grid_import_kwh REAL, tracker_price_p REAL, agile_price_p REAL)""")
            con.executemany("INSERT INTO halfhourly VALUES (?,?,?,?)", [
                ("2026-09-17T15:30:00", 1.0, 25.0, 10.0),
                ("2026-09-17T16:00:00", 2.0, 25.0, 30.0),
                ("2026-09-17T18:00:00", 1.0, 25.0, None),
            ])
            con.commit()
            con.close()
            p = plugin.Plugin.__new__(plugin.Plugin)
            p.data_dir = td
            p.logger = MagicMock()

            result = p._shadow_tariff_baseline("2026-09-17")

        self.assertEqual(result["evening_import_kwh"], 3.0)
        self.assertEqual(result["priced_import_kwh"], 3.0)
        self.assertEqual(result["missing_price_slots"], 1)
        self.assertIsNone(result["tracker_cost_gbp"])
        self.assertIsNone(result["agile_cost_gbp"])


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


class TestVppDriverField(unittest.TestCase):
    """_vpp_driver — judged from the LIVE mode register, not from our own intent
    flag. The old field mirrored store["export_active"], so it reported what we
    meant to do rather than what the inverter was doing."""

    def _p(self, **store):
        p = plugin.Plugin.__new__(plugin.Plugin)
        p.store = store
        return p

    def test_idle_when_not_exporting(self):
        self.assertEqual(self._p(export_active=False)._vpp_driver(0x06), "idle")

    def test_self_when_register_matches_what_we_wrote(self):
        p = self._p(export_active=True, vpp_export_mode=0x06)
        self.assertEqual(p._vpp_driver(0x06), "self")

    def test_external_when_register_holds_a_mode_we_did_not_write(self):
        """The whole point of the field: something else moved 40031."""
        p = self._p(export_active=True, vpp_export_mode=0x06)
        self.assertEqual(p._vpp_driver(0x02), "external")

    def test_unverified_when_the_modbus_read_failed(self):
        """A failed read must NOT read as agreement — absent is not a match."""
        p = self._p(export_active=True, vpp_export_mode=0x06)
        self.assertEqual(p._vpp_driver(None), "self (unverified)")

    def test_unverified_before_any_mode_has_been_written(self):
        p = self._p(export_active=True)
        self.assertEqual(p._vpp_driver(0x06), "self (unverified)")


class TestVppBankCapVerify(unittest.TestCase):
    """_verify_ems_registers maintains the bank sub-mode's charge cap during an
    active window. In bank (0x02) the cap IS the export mechanism — drift up to
    inverter max sends the surplus to the battery instead of the grid, and the
    mode register still reads a perfectly correct 0x02, so nothing else notices."""

    def _p(self, submode, expected_cap, actual_cap, mode=0x02, daytime=False,
           actual_dis=10000):
        p = plugin.Plugin.__new__(plugin.Plugin)
        p.logger      = MagicMock()
        p.modbus      = MagicMock()
        p.pluginPrefs = {"inverterMaxKw": "10.0", "batteryHealthCutoff": "1.0"}
        p.store = {
            "vpp_state":              plugin.VPP_ACTIVE,
            "export_active":          True,
            "vpp_export_mode":        mode,
            "vpp_export_submode":     submode,
            "vpp_bank_charge_cap_w":  expected_cap,
            "vpp_is_daytime":         daytime,
        }
        p.modbus.connected                          = True
        p.modbus.read_ems_mode.return_value         = mode
        p.modbus.read_charge_limit.return_value     = actual_cap
        p.modbus.read_discharge_limit.return_value  = actual_dis
        p.modbus.read_discharge_cutoff.return_value = 1.0     # matches the health floor
        p.modbus.read_charge_cutoff.return_value    = 100.0   # no import backstop raised
        return p

    def test_drifted_cap_is_reasserted(self):
        p = self._p("bank", expected_cap=1200, actual_cap=10000)
        p._verify_ems_registers()
        p.modbus.set_charge_limit.assert_called_once()
        self.assertEqual(p.modbus.set_charge_limit.call_args.args[0], 1200)

    def test_cap_within_deadband_is_left_alone(self):
        p = self._p("bank", expected_cap=1200, actual_cap=1350)   # 150 < 300
        p._verify_ems_registers()
        p.modbus.set_charge_limit.assert_not_called()

    def test_discharge_submode_healthy_registers_untouched(self):
        """Healthy discharge-mode registers get NO writes. This test used to be
        named ..._limits_untouched and asserted the verify loop NEVER touched
        the discharge sub-mode's limits — i.e. it pinned the v5.57.0 GAP as if
        it were the contract. The 10-Apr-2026 rule was about a FOREIGN cap (the
        solar-overflow one) being written over a window; re-asserting the
        registers the drive itself wrote is the opposite of that."""
        p = self._p("discharge", expected_cap=-1, actual_cap=0, mode=0x06,
                    daytime=False, actual_dis=10000)
        p._verify_ems_registers()
        p.modbus.set_charge_limit.assert_not_called()
        p.modbus.set_discharge_limit.assert_not_called()

    def test_discharge_drifted_discharge_limit_reasserted(self):
        """A stale low discharge cap throttles the paid export for the rest of
        the window; the verify docstring has promised inv-max during export
        since v5.16 while the ACTIVE branch skipped it."""
        p = self._p("discharge", expected_cap=-1, actual_cap=0, mode=0x06,
                    actual_dis=4000)
        p._verify_ems_registers()
        p.modbus.set_discharge_limit.assert_called_once()
        self.assertEqual(p.modbus.set_discharge_limit.call_args.args[0], 10000)

    def test_daytime_discharge_open_charge_cap_reasserted(self):
        """The v5.29.0 failure: in 0x05 an OPEN charge limit banks high PV into
        the battery instead of exporting it — mode register reads correct, so
        only this check can see it."""
        p = self._p("discharge", expected_cap=-1, actual_cap=10000, mode=0x05,
                    daytime=True)
        p._verify_ems_registers()
        p.modbus.set_charge_limit.assert_called_once()
        self.assertEqual(p.modbus.set_charge_limit.call_args.args[0], 0)

    def test_dark_discharge_never_pins_the_charge_cap(self):
        """Dark windows never wrote charge=0 (PV is zero, the register is
        irrelevant), so verify must not invent that expectation."""
        p = self._p("discharge", expected_cap=-1, actual_cap=10000, mode=0x06,
                    daytime=False)
        p._verify_ems_registers()
        p.modbus.set_charge_limit.assert_not_called()

    def test_discharge_unread_limits_not_treated_as_agreement(self):
        p = self._p("discharge", expected_cap=-1, actual_cap=None, mode=0x05,
                    daytime=True, actual_dis=None)
        p._verify_ems_registers()
        p.modbus.set_charge_limit.assert_not_called()
        p.modbus.set_discharge_limit.assert_not_called()

    def test_unread_cap_is_not_treated_as_agreement(self):
        p = self._p("bank", expected_cap=1200, actual_cap=None)
        p._verify_ems_registers()
        p.modbus.set_charge_limit.assert_not_called()


class TestVppDirectionGuard(unittest.TestCase):
    """_apply_vpp_event refuses to drive a non-export event (v5.57.0).

    Everything downstream of the announce self-drives an EXPORT — pre-charge,
    raised cutoff, night/daytime_export for the window. Axle's API carries
    import_export and has only ever sent "export", but nothing checked it: an
    IMPORT event would have been announced, pre-charged and then driven as a
    full 4 kW export THROUGH the very window the grid wants energy in. Flagged
    "noted only" at v5.28.0 and open ever since."""

    def _p(self, state=None):
        p = plugin.Plugin.__new__(plugin.Plugin)
        p.logger = MagicMock()
        p.modbus = MagicMock()
        p.store  = {"vpp_state": state or plugin.VPP_IDLE}
        p.latest_inverter_data   = {}
        p._vpp_transition        = MagicMock()
        p._trigger_event         = MagicMock()
        p._write_vpp_event_header = MagicMock()
        p._update_vpp_device     = MagicMock()
        p._start_vpp_precharge   = MagicMock()
        p._end_vpp_export        = MagicMock()
        p._log_vpp_snapshot      = MagicMock()
        p._set_vpp_discharge_cutoff = MagicMock()
        p._event_is_daytime      = MagicMock(return_value=False)
        p._restore_discharge_cutoff = MagicMock()
        return p

    @staticmethod
    def _event(direction, hours_ahead=2.0):
        start = datetime.now(timezone.utc) + timedelta(hours=hours_ahead)
        return {
            "start_time":    start,
            "end_time":      start + timedelta(hours=1),
            "import_export": direction,
            "duration_hrs":  1.0,
        }

    def test_export_event_announces(self):
        p = self._p()
        p._apply_vpp_event(self._event("export"))
        p._vpp_transition.assert_called_once_with(plugin.VPP_ANNOUNCED)
        self.assertIn("vpp_event", p.store)

    def test_import_event_is_never_announced(self):
        """The finding itself: pre-fix this announced and would have driven."""
        p = self._p()
        p._apply_vpp_event(self._event("import"))
        p._vpp_transition.assert_not_called()
        self.assertNotIn("vpp_event", p.store)

    def test_import_warning_is_latched_per_event(self):
        p  = self._p()
        ev = self._event("import")
        p._apply_vpp_event(ev)
        latch = p.store.get("vpp_direction_warned")
        self.assertEqual(latch, str(ev["start_time"]))
        p._apply_vpp_event(ev)          # second poll: still refused, latch stable
        p._vpp_transition.assert_not_called()
        self.assertEqual(p.store.get("vpp_direction_warned"), latch)

    def test_missing_direction_defaults_to_export(self):
        """Axle omitting the field must not strand real events — the client has
        always defaulted it to export, and so does the guard."""
        p  = self._p()
        ev = self._event("export")
        del ev["import_export"]
        p._apply_vpp_event(ev)
        p._vpp_transition.assert_called_once_with(plugin.VPP_ANNOUNCED)

    def test_import_event_mid_announced_stands_down(self):
        """An announced export window that Axle then republishes as import is a
        cancellation — the None branch's teardown must run."""
        p = self._p(state=plugin.VPP_ANNOUNCED)
        p.store["export_active"] = False
        p._apply_vpp_event(self._event("import", hours_ahead=0.4))
        p._restore_discharge_cutoff.assert_called_once()
        p._vpp_transition.assert_called_once_with(plugin.VPP_IDLE)


class TestVppExportModePersistence(unittest.TestCase):
    """vpp_export_mode survives a restart (v5.57.0).

    Verify runs BEFORE the act step, so the first tick after a mid-window
    restart used the store default 0x06 and "corrected" a daytime window's
    0x02/0x05 register to it — one spurious mode write per restart, each
    costing a real mode-switch settle (~26 s, measured 15-Jun-2026)."""

    def _saved_payload(self, store):
        import json as _json
        import tempfile
        p = plugin.Plugin.__new__(plugin.Plugin)
        p.logger      = MagicMock()
        p.pluginPrefs = {}
        p.store       = store
        p._state_lock = threading.RLock()
        p._save_home_profile = MagicMock()
        path = os.path.join(tempfile.mkdtemp(), "accumulators.json")
        p._save_accumulators_locked(path)
        with open(path, encoding="utf-8") as fh:
            return _json.load(fh)

    @staticmethod
    def _base_store(**extra):
        store = {
            "pv_daily_kwh": 1.0, "grid_import_daily_kwh": 0.0,
            "grid_export_daily_kwh": 2.0, "home_daily_kwh": 3.0,
            "peak_soc": 90.0, "min_soc": 40.0, "today_date": "2026-08-05",
            "pv_lifetime_start_kwh": 100.0, "import_lifetime_start_kwh": 10.0,
            "export_lifetime_start_kwh": 20.0,
        }
        store.update(extra)
        return store

    def test_export_mode_is_saved(self):
        payload = self._saved_payload(self._base_store(
            vpp_state=plugin.VPP_ACTIVE,
            vpp_event={"start_time": datetime(2026, 8, 5, 11, 0, tzinfo=timezone.utc),
                       "end_time":   datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)},
            vpp_export_mode=0x05,
        ))
        self.assertEqual(payload.get("vpp_export_mode"), 0x05)

    def test_export_mode_is_restored(self):
        import json as _json
        import tempfile
        tmp = tempfile.mkdtemp()
        start = datetime(2026, 8, 5, 11, 0, tzinfo=timezone.utc)
        data = {
            "vpp_state": plugin.VPP_ACTIVE,
            "vpp_event": plugin._serialise_vpp_event(
                {"start_time": start, "end_time": start + timedelta(hours=1)}),
            "vpp_export_mode": 0x05,
        }
        with open(os.path.join(tmp, "accumulators.json"), "w", encoding="utf-8") as fh:
            _json.dump(data, fh)
        p = plugin.Plugin.__new__(plugin.Plugin)
        p.logger        = MagicMock()
        p.pluginPrefs   = {}
        p.store         = {}
        p._get_data_dir = lambda: tmp
        p._load_accumulators()
        self.assertEqual(p.store.get("vpp_export_mode"), 0x05)
        self.assertEqual(p.store.get("vpp_state"), plugin.VPP_ACTIVE)

    def test_pre_557_file_leaves_the_default(self):
        """An accumulators file from before the key existed must not plant 0."""
        import json as _json
        import tempfile
        tmp = tempfile.mkdtemp()
        start = datetime(2026, 8, 5, 11, 0, tzinfo=timezone.utc)
        data = {
            "vpp_state": plugin.VPP_ACTIVE,
            "vpp_event": plugin._serialise_vpp_event(
                {"start_time": start, "end_time": start + timedelta(hours=1)}),
        }
        with open(os.path.join(tmp, "accumulators.json"), "w", encoding="utf-8") as fh:
            _json.dump(data, fh)
        p = plugin.Plugin.__new__(plugin.Plugin)
        p.logger        = MagicMock()
        p.pluginPrefs   = {}
        p.store         = {}
        p._get_data_dir = lambda: tmp
        p._load_accumulators()
        self.assertNotIn("vpp_export_mode", p.store)


class TestVppMidnightAnchor(unittest.TestCase):
    """_vpp_export_anchor_after_midnight — a window spanning midnight keeps its
    export delta across the daily counter reset. Latent since v5.28.0: the
    counter zeroes at midnight, so (total - start) went NEGATIVE and the
    settlement figure was logged, JSONL'd, state-written and Pushover'd as
    e.g. "-3.90 kWh exported"."""

    def test_rebase_arithmetic(self):
        # Window started with the day's counter at 3.0; by midnight it read 7.5.
        self.assertAlmostEqual(
            plugin._vpp_export_anchor_after_midnight(3.0, 7.5), -4.5)

    def test_delta_is_continuous_across_the_rollover(self):
        old_start, pre_total = 3.0, 7.5     # 4.5 kWh banked before midnight
        anchor = plugin._vpp_export_anchor_after_midnight(old_start, pre_total)
        for after_midnight in (0.0, 0.7, 1.2):
            self.assertAlmostEqual(
                after_midnight - anchor,
                (pre_total - old_start) + after_midnight)

    def test_never_negative_for_a_real_window(self):
        anchor = plugin._vpp_export_anchor_after_midnight(3.0, 7.5)
        self.assertGreaterEqual(0.0 - anchor, 0.0)


class TestVppPvStatus(unittest.TestCase):
    """The summariser's PV verdict. `min_pv_w > 100` alone made every DARK window
    report "PV collapsed" — an alarm about the sun having set. Live case
    05-Aug-2026: a 21:00-22:00 BST window, 45 snapshots at 0 W, a clean 4.23 kWh
    export, and a Pushover saying PV had collapsed."""

    @staticmethod
    def _verdict(daytime, min_pv_w):
        """Mirror of the summariser's decision, exercised directly."""
        if not daytime:
            return "n/a (dark window)", True
        if min_pv_w > 100:
            return "ran", True
        return "curtailed", False

    def test_dark_window_is_not_an_alarm(self):
        status, ok = self._verdict(daytime=False, min_pv_w=0)
        self.assertEqual(status, "n/a (dark window)")
        self.assertTrue(ok)

    def test_daytime_pv_running_is_fine(self):
        self.assertEqual(self._verdict(True, 1450), ("ran", True))

    def test_daytime_pv_pinned_at_zero_is_the_real_fault(self):
        """The 15-Jun-2026 mode-0x06 curtailment this check exists to catch."""
        self.assertEqual(self._verdict(True, 0), ("curtailed", False))


class TestLondonTimeHelpers(unittest.TestCase):
    """plugin.py's local-time helpers, after the v5.55.3 sweep finally reached it.

    Every one of these used to be a hand-rolled `import pytz` with an
    `except: carry on in UTC` fallback — an answer an hour wrong for the eight
    months of BST, with nothing logged."""

    def test_london_now_is_aware(self):
        self.assertIsNotNone(plugin._london_now().tzinfo)

    def test_bst_conversion_is_applied(self):
        """August: London is UTC+1. A silent-UTC fallback returns 12:00."""
        dt = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(plugin._local_time(dt), "13:00")

    def test_gmt_conversion_is_applied(self):
        """January: London is UTC. Guards against a fixed +1 'fix'."""
        dt = datetime(2026, 1, 5, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(plugin._local_time(dt), "12:00")

    def test_naive_localise_is_not_lmt(self):
        """Attaching a pytz zone with a bare replace() yields LMT, -00:01 for
        London. The shared helper picks the right call for the library in use."""
        aware = plugin._london_localise(datetime(2026, 8, 5, 12, 0))
        self.assertEqual(aware.utcoffset(), timedelta(hours=1))

    def test_today_str_matches_the_london_date(self):
        self.assertEqual(plugin._local_today_str(),
                         plugin._london_now().strftime("%Y-%m-%d"))


class TestWholeHouseCard(unittest.TestCase):
    """Plugin._wh_card_from_row — turns a cost-settled daily_history row into the
    dashboard card dict (v5.31.0). Pure/static, so no Indigo instance needed."""

    SETTLED = {
        "date": "2026-06-20", "cost_settled": True,
        "elec_unit_cost_gbp": 0.02, "elec_standing_gbp": 0.62,
        "gas_unit_cost_gbp": 0.53, "gas_standing_gbp": 0.29,
        "whole_house_bill_gbp": 1.45, "export_revenue_gbp": 4.42,
        "wh_net_gbp": 2.97, "covered": True,
    }

    def test_sums_and_passthrough(self):
        c = plugin.Plugin._wh_card_from_row(self.SETTLED)
        self.assertEqual(c["electric_gbp"], 0.64)     # 0.02 + 0.62
        self.assertEqual(c["gas_gbp"], 0.82)          # 0.53 + 0.29
        self.assertEqual(c["bill_gbp"], 1.45)
        self.assertEqual(c["export_gbp"], 4.42)
        self.assertEqual(c["net_gbp"], 2.97)
        self.assertTrue(c["covered"])
        self.assertFalse(c["provisional"])
        self.assertFalse(c["gas_estimated"])

    def test_unsettled_row_returns_none(self):
        self.assertIsNone(plugin.Plugin._wh_card_from_row(
            {"date": "2026-06-21", "grid_export_kwh": 5.0}))

    def test_none_row_returns_none(self):
        self.assertIsNone(plugin.Plugin._wh_card_from_row(None))


# ---- Helpers for the settle / summary tests (build Plugin without __init__) ----
class _FakeOcto:
    # Models a gas-having user (a real OctopusAPI always exposes these attributes;
    # the settle / summary code keys "does this user have gas" off the gas meter id).
    gas_mprn   = "g-mprn"
    gas_serial = "g-serial"

    def __init__(self, fin="default", gas="default", slots=48, gas_mprn="g-mprn",
                 gas_serial="g-serial"):
        self._fin = {
            "elec":   {"standing_p": 61.51824, "unit_p": 23.478},
            "gas":    {"unit_p": 6.58413, "standing_p": 29.06169},
            "export": {"unit_p": 12.0}, "balance_gbp": 392.39,
        } if fin == "default" else fin
        self._slots = slots
        # v5.46.0: a real get_gas_kwh_for_date carries `complete` (full-day coverage).
        self._gas = ({"m3": 0.716, "kwh": 8.03, "slots": slots, "complete": True}
                     if gas == "default" else gas)
        self.gas_mprn   = gas_mprn
        self.gas_serial = gas_serial

    def get_account_financials(self, force=False):
        return self._fin

    def get_import_kwh_for_date(self, d):
        return {"kwh": 0.066, "slots": self._slots}

    def get_gas_kwh_for_date(self, d):
        return self._gas


def _mk_plugin(tmp, octo, store=None):
    p = plugin.Plugin.__new__(plugin.Plugin)
    p.octopus = octo
    p.data_dir = tmp
    p.logger = MagicMock()
    p.store = store if store is not None else {}
    p._state_lock = threading.RLock()   # v5.45.0: stages lock their merges
    return p


def _london_today():
    """Today in Europe/London, via the module under test.

    Was a private pytz copy with a `except ImportError: return the UTC date`
    fallback — a test helper that quietly disagreed with the module for one hour
    every night in BST, and on a pytz-less runner disagreed all summer. Use the
    module's own helper so the test cannot pass against a basis production never
    uses.
    """
    return plugin._london_today()


class TestSettleWholeHouseCosts(unittest.TestCase):
    """_settle_whole_house_costs — the money-bearing settle (plugin.py)."""

    def _run(self, rows, octo=None):
        import json, tempfile, shutil, os as _os
        tmp = tempfile.mkdtemp()
        try:
            with open(_os.path.join(tmp, "daily_history.json"), "w", encoding="utf-8") as f:
                json.dump(rows, f)
            _mk_plugin(tmp, octo or _FakeOcto())._settle_whole_house_costs()
            with open(_os.path.join(tmp, "daily_history.json"), encoding="utf-8") as f:
                return {r["date"]: r for r in json.load(f)}
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def _yesterday(self):
        from datetime import timedelta
        return (_london_today() - timedelta(days=1)).strftime("%Y-%m-%d")

    def test_settles_and_computes_bill(self):
        d1 = self._yesterday()
        r = self._run([{"date": d1, "rate_today_p": 23.478,
                        "export_rate_p": 12.0, "grid_export_kwh": 36.0}])[d1]
        self.assertTrue(r["cost_settled"])
        # 0.066*23.478/100 + 61.518/100 + 8.03*6.58413/100 + 29.062/100 ~= 1.45
        self.assertAlmostEqual(r["whole_house_bill_gbp"], 1.45, delta=0.02)
        self.assertAlmostEqual(r["export_revenue_gbp"], 4.32, delta=0.01)
        self.assertTrue(r["covered"])

    def test_per_day_rates_preferred_over_ledger(self):
        d1 = self._yesterday()
        r = self._run([{"date": d1, "rate_today_p": 23.478, "export_rate_p": 12.0,
                        "grid_export_kwh": 10.0, "elec_standing_p_day": 50.0,
                        "gas_unit_p_day": 6.0, "gas_standing_p_day": 25.0}])[d1]
        self.assertEqual(r["elec_standing_gbp"], 0.50)   # not the ledger's 0.62
        self.assertEqual(r["gas_standing_gbp"], 0.25)    # not the ledger's 0.29

    def test_gas_unsettled_not_frozen(self):
        d1 = self._yesterday()
        octo = _FakeOcto(gas={"m3": None, "kwh": None, "slots": 0, "complete": False})
        r = self._run([{"date": d1, "rate_today_p": 23.478, "grid_export_kwh": 10.0}], octo)[d1]
        self.assertFalse(r.get("cost_settled", False))

    def test_gas_partial_day_not_frozen(self):
        # THE 03-07-2026 BUG: gas had settled PARTIALLY (a single 00:00-00:30
        # slot, 0.003 m3) — presence-gating froze 1 Jul at £0.00 gas forever.
        # A kWh that is present but with complete=False must NOT settle.
        d1 = self._yesterday()
        octo = _FakeOcto(gas={"m3": 0.003, "kwh": 0.034, "slots": 1, "complete": False})
        r = self._run([{"date": d1, "rate_today_p": 23.478, "grid_export_kwh": 10.0}], octo)[d1]
        self.assertFalse(r.get("cost_settled", False))

    def test_gas_daily_read_meter_settles(self):
        # A daily-read meter's ONE reading spans the whole day → complete=True
        # even at slots=1; it must still settle (the reason presence-gating
        # replaced slot-gating in the first place).
        d1 = self._yesterday()
        octo = _FakeOcto(gas={"m3": 0.716, "kwh": 8.03, "slots": 1, "complete": True})
        r = self._run([{"date": d1, "rate_today_p": 23.478,
                        "export_rate_p": 12.0, "grid_export_kwh": 10.0}], octo)[d1]
        self.assertTrue(r["cost_settled"])
        self.assertAlmostEqual(r["gas_unit_cost_gbp"], 0.53, delta=0.01)

    def test_no_ledger_skips(self):
        d1 = self._yesterday()
        r = self._run([{"date": d1, "rate_today_p": 23.478, "grid_export_kwh": 10.0}],
                      _FakeOcto(fin=None))[d1]
        self.assertFalse(r.get("cost_settled", False))

    def test_partial_day_not_frozen(self):
        # Octopus has only the first hour of the day (2 of 48 half-hour slots) —
        # must NOT freeze a near-zero bill (the 21-Jun-2026 premature-settle bug).
        d1 = self._yesterday()
        r = self._run([{"date": d1, "rate_today_p": 23.478, "grid_export_kwh": 10.0}],
                      _FakeOcto(slots=2))[d1]
        self.assertFalse(r.get("cost_settled", False))

    def test_daily_granularity_gas_settles(self):
        # A daily-read gas meter reports ONE reading (slots=1) while electricity is the
        # complete-day signal (48 slots). The day MUST still settle — gating gas on 46
        # slots would strand daily-read meters on an estimate forever (H2).
        d1 = self._yesterday()
        octo = _FakeOcto(gas={"m3": 2.5, "kwh": 28.0, "slots": 1, "complete": True})  # import keeps 48 slots
        r = self._run([{"date": d1, "rate_today_p": 23.478, "grid_export_kwh": 10.0}], octo)[d1]
        self.assertTrue(r["cost_settled"])
        self.assertGreater(r["gas_unit_cost_gbp"], 0.0)

    def test_no_gas_meter_settles_on_electricity(self):
        # A user with no gas meter settles on electricity alone (gas component = 0),
        # rather than the day never settling because gas data is absent.
        d1 = self._yesterday()
        octo = _FakeOcto(gas_mprn="", gas_serial="",
                         gas={"m3": None, "kwh": None, "slots": 0})
        r = self._run([{"date": d1, "rate_today_p": 23.478, "grid_export_kwh": 10.0}], octo)[d1]
        self.assertTrue(r["cost_settled"])
        self.assertEqual(r["gas_unit_cost_gbp"], 0.0)
        self.assertEqual(r["gas_standing_gbp"], 0.0)

    def test_complete_day_freezes(self):
        d1 = self._yesterday()
        r = self._run([{"date": d1, "rate_today_p": 23.478, "grid_export_kwh": 10.0}],
                      _FakeOcto(slots=48))[d1]
        self.assertTrue(r["cost_settled"])


class TestWholeHouseSummary(unittest.TestCase):
    """_whole_house_summary — the /api/status block."""

    def _summary(self, rows, store=None):
        import json, tempfile, shutil, os as _os
        tmp = tempfile.mkdtemp()
        try:
            with open(_os.path.join(tmp, "daily_history.json"), "w", encoding="utf-8") as f:
                json.dump(rows, f)
            p = _mk_plugin(tmp, _FakeOcto(),
                           store or {"grid_import_daily_kwh": 0.05, "grid_export_daily_kwh": 20.0})
            return p._whole_house_summary(import_rate_p=23.478, export_rate_p=12.0)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def _settled_row(self, date, bill, exp, covered):
        return {"date": date, "month": date[:7], "cost_settled": True,
                "whole_house_bill_gbp": bill, "export_revenue_gbp": exp,
                "wh_net_gbp": round(exp - bill, 2), "covered": covered, "gas_kwh": 8.0,
                "elec_unit_cost_gbp": 0.02, "elec_standing_gbp": 0.62,
                "gas_unit_cost_gbp": 0.53, "gas_standing_gbp": 0.29}

    def test_today_provisional_balance_and_yesterday(self):
        from datetime import timedelta
        d1 = (_london_today() - timedelta(days=1)).strftime("%Y-%m-%d")
        out = self._summary([self._settled_row(d1, 1.45, 4.42, True)])
        self.assertEqual(out["balance_gbp"], 392.39)
        self.assertIsNotNone(out["today"])
        self.assertTrue(out["today"]["provisional"])
        self.assertTrue(out["today"]["gas_estimated"])        # estimated from the settled row
        self.assertIsNotNone(out["yesterday"])
        self.assertEqual(out["yesterday"]["bill_gbp"], 1.45)
        self.assertFalse(out["yesterday"]["provisional"])

    def test_month_aggregation_and_self_funded(self):
        from datetime import timedelta
        t = _london_today(); mp = t.strftime("%Y-%m")
        raw = [self._settled_row((t - timedelta(days=k)).strftime("%Y-%m-%d"),
                                 1.40, (3.0 if k != 2 else 0.50), k != 2) for k in range(0, 3)]
        out = self._summary(raw)
        this_month = [r for r in raw if r["date"][:7] == mp]
        self.assertEqual(out["month"]["bill_gbp"], round(sum(r["whole_house_bill_gbp"] for r in this_month), 2))
        self.assertEqual(out["month"]["export_gbp"], round(sum(r["export_revenue_gbp"] for r in this_month), 2))
        self.assertEqual(out["month"]["in_credit"], out["month"]["export_gbp"] >= out["month"]["bill_gbp"])
        self.assertEqual(out["self_funded"]["covered_days"], sum(1 for r in this_month if r["covered"]))
        self.assertEqual(out["self_funded"]["settled_days"], len(this_month))
        self.assertEqual(len(out["series30"]), 3)   # series30 is all settled, month-agnostic

    def test_empty_history_safe(self):
        out = self._summary([])
        self.assertIsNone(out["yesterday"])
        self.assertIsNone(out["day_before"])
        self.assertIsNone(out["month"])
        self.assertEqual(out["series30"], [])
        self.assertEqual(out["balance_gbp"], 392.39)

    def test_day_before(self):
        from datetime import timedelta
        d2 = (_london_today() - timedelta(days=2)).strftime("%Y-%m-%d")
        out = self._summary([self._settled_row(d2, 1.30, 3.00, True)])
        self.assertEqual(out["day_before_date"], d2)
        self.assertIsNotNone(out["day_before"])
        self.assertEqual(out["day_before"]["bill_gbp"], 1.30)
        self.assertFalse(out["day_before"]["provisional"])

    def test_yesterday_provisional_when_unsettled(self):
        # Row exists (written at midnight) but Octopus hasn't settled it yet —
        # show a provisional card from Sigen import/export, not a blank.
        from datetime import timedelta
        y = (_london_today() - timedelta(days=1)).strftime("%Y-%m-%d")
        out = self._summary([{"date": y, "month": y[:7], "grid_import_kwh": 0.07,
                              "grid_export_kwh": 36.0, "rate_today_p": 23.478,
                              "export_rate_p": 12.0}])
        c = out["yesterday"]
        self.assertIsNotNone(c)                       # not blank
        self.assertTrue(c["provisional"])
        self.assertEqual(c["electric_standing_gbp"], 0.62)   # ledger standing
        self.assertAlmostEqual(c["export_gbp"], 4.32, delta=0.01)  # 36 kWh x 12p


class TestWholeHouseEdges(unittest.TestCase):
    """covered== boundary and _wh_card_from_row partial-row coalescing."""

    def _settle(self, rows, octo):
        import json, tempfile, shutil, os as _os
        tmp = tempfile.mkdtemp()
        try:
            with open(_os.path.join(tmp, "daily_history.json"), "w", encoding="utf-8") as f:
                json.dump(rows, f)
            _mk_plugin(tmp, octo)._settle_whole_house_costs()
            with open(_os.path.join(tmp, "daily_history.json"), encoding="utf-8") as f:
                return {r["date"]: r for r in json.load(f)}
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def _yest(self):
        from datetime import timedelta
        return (_london_today() - timedelta(days=1)).strftime("%Y-%m-%d")

    def test_covered_boundary_zero_equals(self):
        d1 = self._yest()
        zero = {"elec": {"standing_p": 0.0, "unit_p": 0.0},
                "gas": {"unit_p": 0.0, "standing_p": 0.0},
                "export": {"unit_p": 12.0}, "balance_gbp": 0.0}
        octo = _FakeOcto(fin=zero, gas={"m3": 0.0, "kwh": 0.0, "slots": 48, "complete": True})
        r = self._settle([{"date": d1, "rate_today_p": 0.0,
                           "export_rate_p": 12.0, "grid_export_kwh": 0.0}], octo)[d1]
        self.assertEqual(r["whole_house_bill_gbp"], 0.0)
        self.assertEqual(r["export_revenue_gbp"], 0.0)
        self.assertTrue(r["covered"])          # export 0 >= bill 0 (the boundary)

    def test_not_covered_when_short(self):
        d1 = self._yest()
        r = self._settle([{"date": d1, "rate_today_p": 23.478,
                           "export_rate_p": 12.0, "grid_export_kwh": 1.0}], _FakeOcto())[d1]
        self.assertFalse(r["covered"])         # ~£0.12 export vs ~£1.45 bill

    def test_partial_settled_row_coalesces(self):
        c = plugin.Plugin._wh_card_from_row({"cost_settled": True})
        self.assertEqual(c["electric_gbp"], 0.0)   # `or 0.0` coalescing
        self.assertEqual(c["gas_gbp"], 0.0)
        self.assertIsNone(c["bill_gbp"])           # missing passthrough stays None


class TestPowerCutExportLockoutSocFloor(unittest.TestCase):
    """v5.34.0: the post-cut export lockout is held only while SOC < floor, so a
    near-full battery still exports (protects solar from being clipped at 100%)."""

    def test_no_window_never_suppresses(self):
        # Outside the lockout window export is free regardless of SOC.
        self.assertFalse(plugin._export_locked_out(False, 10.0, 85.0))
        self.assertFalse(plugin._export_locked_out(False, None, 85.0))

    def test_in_window_below_floor_suppresses(self):
        self.assertTrue(plugin._export_locked_out(True, 84.9, 85.0))
        self.assertTrue(plugin._export_locked_out(True, 50.0, 85.0))

    def test_in_window_at_or_above_floor_allows(self):
        # The case CliveS hit: 92% battery in the window must still export.
        self.assertFalse(plugin._export_locked_out(True, 85.0, 85.0))
        self.assertFalse(plugin._export_locked_out(True, 92.0, 85.0))

    def test_unknown_soc_fails_safe(self):
        # An unknown SOC inside the window must NOT fail-open.
        self.assertTrue(plugin._export_locked_out(True, None, 85.0))

    def test_default_floor_is_85(self):
        self.assertEqual(plugin.POWER_CUT_LOCKOUT_SOC_FLOOR, 85.0)


class TestLockoutSocFloorPref(unittest.TestCase):
    """v5.35.0: the SOC floor is configurable (powerCutLockoutSocFloor), guarded."""

    class _Stub:
        def __init__(self, prefs):
            self.pluginPrefs = prefs

    def _floor(self, prefs):
        return plugin.Plugin._power_cut_lockout_soc_floor(self._Stub(prefs))

    def test_blank_uses_default(self):
        self.assertEqual(self._floor({}), plugin.POWER_CUT_LOCKOUT_SOC_FLOOR)

    def test_pref_overrides(self):
        self.assertEqual(self._floor({"powerCutLockoutSocFloor": "70"}), 70.0)

    def test_garbage_falls_back(self):
        self.assertEqual(self._floor({"powerCutLockoutSocFloor": "abc"}),
                         plugin.POWER_CUT_LOCKOUT_SOC_FLOOR)


class TestLockoutMinSocPref(unittest.TestCase):
    """v5.50.0: the solar-refill release floor is configurable, guarded."""

    class _Stub:
        def __init__(self, prefs):
            self.pluginPrefs = prefs

    def _min(self, prefs):
        return plugin.Plugin._power_cut_lockout_min_soc(self._Stub(prefs))

    def test_blank_uses_default(self):
        self.assertEqual(self._min({}), plugin.POWER_CUT_LOCKOUT_MIN_SOC_PCT)

    def test_pref_overrides(self):
        self.assertEqual(self._min({"powerCutLockoutMinSocPct": "40"}), 40.0)

    def test_garbage_falls_back(self):
        self.assertEqual(self._min({"powerCutLockoutMinSocPct": ""}),
                         plugin.POWER_CUT_LOCKOUT_MIN_SOC_PCT)


class TestSolarRefillReleasesLockout(unittest.TestCase):
    """v5.50.0: the second, strictly-additional lockout release — today's own solar
    refills the reserve, so holding export off banks nothing and only risks clipping.

    Anchored on the 20-Jul-2026 incident: a 109-second grid blip at 05:25 armed the
    lockout at SOC 75.6%, and the flat 85% floor held export off until 07:36. Those
    ~3.3 kWh were exportable under the 4 kW DNO cap at the time, and banking them
    instead cost the afternoon headroom that would have absorbed the above-cap peak.
    """

    # 85% of the 35.04 kWh pack — the default floor expressed in kWh.
    FLOOR_KWH = 0.85 * 35.04

    def _release(self, **kw):
        args = dict(is_daytime=True, soc_pct=80.0, min_soc_pct=50.0,
                    battery_kwh=28.0, floor_kwh=self.FLOOR_KWH,
                    remaining_solar_kwh=40.0, home_to_dusk_kwh=10.0)
        args.update(kw)
        return plugin._solar_refill_releases_lockout(**args)

    # ── The incident itself ────────────────────────────────────────────────
    def test_incident_0736_already_at_floor(self):
        # Logged live: Battery 29.8 kWh | Solar remaining 53.0 | Home to dusk 12.7.
        self.assertTrue(self._release(soc_pct=85.0, battery_kwh=29.8,
                                      remaining_solar_kwh=53.0,
                                      home_to_dusk_kwh=12.7))

    def test_incident_backed_up_to_first_light_releases(self):
        # The point of the change: at SOC 75.6% (26.5 kWh) the same day's forecast
        # already covers the 3.3 kWh gap many times over, so export should resume
        # at the first daytime tick rather than at 07:36.
        self.assertTrue(self._release(soc_pct=75.6, battery_kwh=26.5,
                                      remaining_solar_kwh=53.0,
                                      home_to_dusk_kwh=12.7))

    # ── Fail-safe directions (all must HOLD the lockout) ───────────────────
    def test_night_holds(self):
        # No solar to come -> the flat SOC floor must govern on its own.
        self.assertFalse(self._release(is_daytime=False, remaining_solar_kwh=0.0,
                                       home_to_dusk_kwh=0.0))

    def test_unknown_soc_holds(self):
        self.assertFalse(self._release(soc_pct=None))

    def test_below_min_soc_holds_despite_perfect_forecast(self):
        # A nearly-empty battery is not a resilience reserve, however bright it is.
        self.assertFalse(self._release(soc_pct=49.9, battery_kwh=17.5,
                                       remaining_solar_kwh=60.0,
                                       home_to_dusk_kwh=10.0))

    def test_dull_winter_day_holds(self):
        # 4 kWh of solar left against 6 kWh of house load -> no surplus at all.
        self.assertFalse(self._release(soc_pct=60.0, battery_kwh=21.0,
                                       remaining_solar_kwh=4.0,
                                       home_to_dusk_kwh=6.0))

    def test_house_load_exceeding_solar_clamps_to_zero(self):
        # Negative surplus must not sneak through as a large negative number.
        self.assertFalse(self._release(soc_pct=60.0, battery_kwh=21.0,
                                       remaining_solar_kwh=2.0,
                                       home_to_dusk_kwh=30.0))

    # ── Boundaries ─────────────────────────────────────────────────────────
    def test_min_soc_boundary_is_inclusive(self):
        self.assertTrue(self._release(soc_pct=50.0, battery_kwh=17.5,
                                      remaining_solar_kwh=60.0,
                                      home_to_dusk_kwh=10.0))

    def test_margin_boundary_exact_release(self):
        # needed = 4.0 kWh, margin 1.25 -> requires exactly 5.0 kWh of surplus.
        battery = self.FLOOR_KWH - 4.0
        self.assertTrue(self._release(battery_kwh=battery,
                                      remaining_solar_kwh=15.0,
                                      home_to_dusk_kwh=10.0))

    def test_margin_boundary_just_short_holds(self):
        battery = self.FLOOR_KWH - 4.0
        self.assertFalse(self._release(battery_kwh=battery,
                                       remaining_solar_kwh=14.99,
                                       home_to_dusk_kwh=10.0))

    def test_at_floor_short_circuits_true(self):
        # Nothing left to bank -> release regardless of the forecast.
        self.assertTrue(self._release(battery_kwh=self.FLOOR_KWH,
                                      remaining_solar_kwh=0.0,
                                      home_to_dusk_kwh=0.0))

    def test_kwh_formulation_is_satisfiable_at_default_floor(self):
        # Regression guard on the design note: an SOC-space formulation
        # (projected_dusk_pct >= floor_pct * margin) is unsatisfiable at the
        # defaults, because 85 * 1.25 = 106 > 100. The kWh form must not be.
        self.assertTrue(self._release(soc_pct=51.0, battery_kwh=17.9,
                                      remaining_solar_kwh=45.0,
                                      home_to_dusk_kwh=8.0))

    def test_defaults(self):
        self.assertEqual(plugin.POWER_CUT_LOCKOUT_MIN_SOC_PCT, 50.0)
        self.assertEqual(plugin.POWER_CUT_LOCKOUT_REFILL_MARGIN, 1.25)


class TestResolveExportLockout(unittest.TestCase):
    """_resolve_export_lockout — the post-power-cut export lockout WRAPPER (the pure
    core _export_locked_out is tested separately). Window is time-based; suppression
    requires export enabled AND SOC below the floor; the cleared-event fires once on
    real window expiry; a corrupt powerRestoredTime fails closed-then-clears."""

    def _mk(self, export_enabled=True, restored_minutes_ago=None,
            powerRestoredTime=None, prev_suppressed=False, prev_lockout=False,
            soc_floor="85"):
        p = plugin.Plugin.__new__(plugin.Plugin)
        p.pluginPrefs = {"exportEnabled": export_enabled,
                         "powerCutLockoutSocFloor": soc_floor}
        if powerRestoredTime is not None:
            p.pluginPrefs["powerRestoredTime"] = powerRestoredTime
        elif restored_minutes_ago is not None:
            prt = datetime.now(timezone.utc) - timedelta(minutes=restored_minutes_ago)
            p.pluginPrefs["powerRestoredTime"] = prt.isoformat()
        else:
            p.pluginPrefs["powerRestoredTime"] = ""
        p.store = {"power_cut_export_suppressed": prev_suppressed,
                   "power_cut_lockout_active": prev_lockout}
        p._events = []
        p._trigger_event = lambda name: p._events.append(name)
        return p

    def test_within_window_low_soc_suppresses_export(self):
        # Restored 60 min ago (window 4h), SOC 50 < 85 floor -> export held off.
        p = self._mk(restored_minutes_ago=60)
        self.assertFalse(p._resolve_export_lockout(50.0))
        self.assertTrue(p.store["power_cut_export_suppressed"])
        self.assertTrue(p.store["power_cut_lockout_active"])

    def test_within_window_high_soc_allows_export(self):
        # SOC 90 >= 85 floor -> export resumes mid-window to protect solar.
        p = self._mk(restored_minutes_ago=60, prev_suppressed=True)
        self.assertTrue(p._resolve_export_lockout(90.0))
        self.assertFalse(p.store["power_cut_export_suppressed"])

    def test_within_window_unknown_soc_fails_safe(self):
        # Unknown SOC must fail safe (suppress), never fail-open.
        p = self._mk(restored_minutes_ago=60)
        self.assertFalse(p._resolve_export_lockout(None))
        self.assertTrue(p.store["power_cut_export_suppressed"])

    def test_window_expiry_fires_cleared_event_once(self):
        # Restored 5h ago (> 4h window) with the lockout previously active.
        p = self._mk(restored_minutes_ago=300, prev_lockout=True)
        self.assertTrue(p._resolve_export_lockout(50.0))   # window expired -> export ok
        self.assertFalse(p.store["power_cut_lockout_active"])
        self.assertEqual(p._events.count("powerCutLockoutCleared"), 1)

    def test_corrupt_restored_time_clears_and_resumes(self):
        # A garbage pluginPrefs value must clear itself and NOT hold export off.
        p = self._mk(powerRestoredTime="not-a-timestamp")
        self.assertTrue(p._resolve_export_lockout(50.0))
        self.assertEqual(p.pluginPrefs["powerRestoredTime"], "")

    def test_naive_restored_time_promoted_to_utc_not_cleared(self):
        # A hand-edited NAIVE isoformat timestamp must be promoted to UTC and
        # still enforce the lockout window — not treated as corrupt and cleared.
        # (Was previously only pseudo-covered by a stdlib-only test.)
        prt = (datetime.now(timezone.utc) - timedelta(minutes=60)).replace(tzinfo=None)
        p = self._mk(powerRestoredTime=prt.isoformat())
        self.assertFalse(p._resolve_export_lockout(50.0))   # inside 4h window
        self.assertTrue(p.store["power_cut_export_suppressed"])
        self.assertEqual(p.pluginPrefs["powerRestoredTime"], prt.isoformat())

    def test_export_disabled_short_circuits(self):
        # Export disabled by pref -> always False, and nothing suppressed spuriously.
        p = self._mk(export_enabled=False, restored_minutes_ago=60)
        self.assertFalse(p._resolve_export_lockout(50.0))
        self.assertFalse(p.store["power_cut_export_suppressed"])

    def test_soc_exactly_at_floor_allows_export(self):
        # 85 is NOT below the 85 floor -> export allowed (boundary).
        p = self._mk(restored_minutes_ago=60)
        self.assertTrue(p._resolve_export_lockout(85.0))

    # ── v5.50.0 solar-refill release, through the wrapper ──────────────────
    @staticmethod
    def _balance(is_daytime=True, battery_kwh=26.5,
                 remaining_solar_kwh=53.0, remaining_home_to_dusk_kwh=12.7):
        return types.SimpleNamespace(
            is_daytime=is_daytime, battery_kwh=battery_kwh,
            remaining_solar_kwh=remaining_solar_kwh,
            remaining_home_to_dusk_kwh=remaining_home_to_dusk_kwh,
        )

    def test_balance_none_reproduces_pre_5_50_behaviour(self):
        # Every pre-5.50 caller omitted `balance` — that path must be unchanged.
        p = self._mk(restored_minutes_ago=60)
        self.assertFalse(p._resolve_export_lockout(75.6))
        self.assertTrue(p.store["power_cut_export_suppressed"])

    def test_solar_refill_releases_below_the_soc_floor(self):
        # The 20-Jul-2026 incident: SOC 75.6% is below the 85% floor, but the day's
        # own solar refills the reserve, so export resumes instead of banking kWh
        # that the sun was going to deliver anyway.
        p = self._mk(restored_minutes_ago=60, prev_suppressed=True)
        self.assertTrue(p._resolve_export_lockout(75.6, self._balance()))
        self.assertFalse(p.store["power_cut_export_suppressed"])
        self.assertTrue(p.store["power_cut_solar_release"])

    def test_solar_refill_does_not_release_at_night(self):
        p = self._mk(restored_minutes_ago=60)
        bal = self._balance(is_daytime=False, remaining_solar_kwh=0.0,
                            remaining_home_to_dusk_kwh=0.0)
        self.assertFalse(p._resolve_export_lockout(75.6, bal))
        self.assertTrue(p.store["power_cut_export_suppressed"])
        self.assertFalse(p.store["power_cut_solar_release"])

    def test_solar_release_flag_clears_outside_the_window(self):
        # Window expired -> not a "release", just normal operation.
        p = self._mk(restored_minutes_ago=300)
        self.assertTrue(p._resolve_export_lockout(75.6, self._balance()))
        self.assertFalse(p.store["power_cut_solar_release"])

    def test_release_log_survives_unknown_soc(self):
        # export_enabled=False makes suppressed False regardless of SOC, so the
        # one-shot log can be reached with soc_val=None. It must not raise.
        p = self._mk(export_enabled=False, restored_minutes_ago=60,
                     prev_suppressed=True)
        self.assertFalse(p._resolve_export_lockout(None))
        self.assertFalse(p.store["power_cut_export_suppressed"])


class TestDisengageToSafeBaseline(unittest.TestCase):
    """_disengage_to_safe_baseline — pause/sleep must release a raised discharge-cutoff
    floor (flood-prev / storm / VPP) to the health floor and clear the raised-floor
    flags, or the battery stays hardware-locked above that SOC and forces grid import."""

    def _mk(self, vpp_state="idle", connected=True):
        p = plugin.Plugin.__new__(plugin.Plugin)
        p.modbus = MagicMock()
        p.modbus.connected = connected
        p.pluginPrefs = {"batteryHealthCutoff": "1"}
        p.store = {"vpp_state": vpp_state, "vpp_event": None, "vpp_active": False,
                   "vpp_cutoff_raised": True, "flood_prev_target_soc": 30.0,
                   "import_active": True, "export_active": True}
        p._events = []
        p._trigger_event = lambda name: p._events.append(name)
        # v5.43: _set_flood_prev_target persists via _save_accumulators (crash-
        # safe copy) — stub it, this minimal harness has no data_dir/logger.
        p._save_accumulators = lambda: None
        return p

    def test_resets_cutoff_to_health_floor_and_clears_flags(self):
        p = self._mk()
        p._disengage_to_safe_baseline("Pause")
        p.modbus.set_discharge_cutoff.assert_called_with(1.0)
        self.assertFalse(p.store["vpp_cutoff_raised"])
        self.assertIsNone(p.store["flood_prev_target_soc"])
        self.assertEqual(p.pluginPrefs["floodPrevTargetSoc"], "")
        self.assertFalse(p.store["import_active"])
        self.assertFalse(p.store["export_active"])

    def test_modbus_offline_still_clears_flags(self):
        # No modbus write possible, but the raised-floor flags must still clear so the
        # next online evaluate re-asserts the health floor.
        p = self._mk(connected=False)
        p._disengage_to_safe_baseline("Pause")
        p.modbus.set_discharge_cutoff.assert_not_called()
        self.assertFalse(p.store["vpp_cutoff_raised"])
        self.assertIsNone(p.store["flood_prev_target_soc"])


class TestUpdateInverterDevice(unittest.TestCase):
    """_update_inverter_device — the String->Number telemetry write path. Numeric
    states must be written as real numbers (so Indigo history charts them), gridOnline
    must derive 1/0 from the grid status, and categorical states stay strings."""

    def _run(self, data):
        p = plugin.Plugin.__new__(plugin.Plugin)
        dev = MagicMock()
        p._find_device = lambda which: dev
        p.store = {"pv_daily_kwh": 3.4, "grid_import_daily_kwh": 0.02,
                   "grid_export_daily_kwh": 2.6, "home_daily_kwh": 7.9}
        p._update_inverter_device(data)
        states = dev.updateStatesOnServer.call_args[0][0]
        return {s["key"]: s for s in states}, dev

    def test_numeric_states_written_as_numbers(self):
        st, _ = self._run({"batterySoc": 58.8, "gridPowerWatts": 2,
                           "gridStatus": "On-grid"})
        self.assertIsInstance(st["batterySoc"]["value"], float)   # number, not str
        self.assertEqual(st["batterySoc"]["value"], 58.8)
        self.assertIsInstance(st["gridPowerWatts"]["value"], int)
        # categorical stays a string
        self.assertIsInstance(st["gridStatus"]["value"], str)

    def test_grid_online_on_grid_is_1(self):
        st, _ = self._run({"gridStatus": "On-grid"})
        self.assertEqual(st["gridOnline"]["value"], 1)

    def test_grid_online_off_grid_is_0(self):
        st, _ = self._run({"gridStatus": "Off-grid (auto)"})
        self.assertEqual(st["gridOnline"]["value"], 0)

    def test_grid_online_unknown_is_1(self):
        # An unmapped "Unknown (N)" read must NOT chart a false power cut.
        st, _ = self._run({"gridStatus": "Unknown (9)"})
        self.assertEqual(st["gridOnline"]["value"], 1)

    def test_no_device_is_safe(self):
        p = plugin.Plugin.__new__(plugin.Plugin)
        p._find_device = lambda which: None
        p._update_inverter_device({"batterySoc": 50.0})   # must not raise


class TestVppOverlapHours(unittest.TestCase):
    """_vpp_overlap_hours — pro-rates a VPP event across local dates (feeds the
    flood-prevention refill capacity). A midnight-spanning event must split correctly."""

    def test_event_within_one_day(self):
        d = datetime(2026, 6, 26, 0, 0)
        s = datetime(2026, 6, 26, 18, 0)
        e = datetime(2026, 6, 26, 19, 30)
        self.assertAlmostEqual(plugin.Plugin._vpp_overlap_hours(s, e, d.date()), 1.5)

    def test_event_spanning_midnight_splits(self):
        s = datetime(2026, 6, 26, 22, 0)
        e = datetime(2026, 6, 27, 1, 0)      # 3h total, 22:00->01:00
        d1 = plugin.Plugin._vpp_overlap_hours(s, e, datetime(2026, 6, 26).date())
        d2 = plugin.Plugin._vpp_overlap_hours(s, e, datetime(2026, 6, 27).date())
        self.assertAlmostEqual(d1, 2.0)      # 22:00-24:00
        self.assertAlmostEqual(d2, 1.0)      # 00:00-01:00
        self.assertAlmostEqual(d1 + d2, 3.0)

    def test_no_overlap_is_zero(self):
        s = datetime(2026, 6, 26, 18, 0)
        e = datetime(2026, 6, 26, 19, 0)
        self.assertEqual(plugin.Plugin._vpp_overlap_hours(s, e, datetime(2026, 6, 28).date()), 0.0)


class TestDriveVppExportStartLatch(unittest.TestCase):
    """The first _drive_vpp_export tick (export_active False) must latch export_active
    True and fire the exportStarted trigger — the existing TestDriveVppExport always
    pre-sets export_active=True so that path was never exercised."""

    def test_first_drive_latches_and_fires_exportStarted(self):
        p = plugin.Plugin.__new__(plugin.Plugin)
        p.modbus = MagicMock()
        p.latest_inverter_data = {"pvPowerWatts": 9630, "homePowerWatts": 678}
        p.pluginPrefs = {"inverterMaxKw": "10.0", "maxExportKw": "4.0"}
        p.store = {"export_active": False, "vpp_is_daytime": True,
                   "vpp_export_submode": None, "vpp_bank_charge_cap_w": -1}
        fired = []
        p._trigger_event = lambda name: fired.append(name)
        p._drive_vpp_export()
        self.assertTrue(p.store["export_active"])
        self.assertIn("exportStarted", fired)


class TestStormExportRelease(unittest.TestCase):
    """_apply_storm_override suppresses export ONLY while SOC is below the release
    point (default 85%, never below the active reserve target). At/above it the
    reserve is banked, so export is left enabled and the dawn floor still applies."""

    def _mk(self, level, prefs=None):
        p = plugin.Plugin.__new__(plugin.Plugin)
        p.pluginPrefs = prefs or {}
        p.store = {
            "storm_level": level,
            "storm_override_logged_level": None,
            "storm_export_suppressed": False,
        }
        p.logger = MagicMock()
        return p

    def _snap(self, dawn=10.0):
        return types.SimpleNamespace(dawn_target_pct=dawn, export_enabled=True)

    def test_no_storm_leaves_export_untouched(self):
        p = self._mk("none")
        s = self._snap()
        p._apply_storm_override(s, 40.0)
        self.assertTrue(s.export_enabled)
        self.assertFalse(p.store["storm_export_suppressed"])

    def test_yellow_below_release_suppresses(self):
        p = self._mk("yellow")
        s = self._snap()
        p._apply_storm_override(s, 40.0)          # 40 < 85 release
        self.assertFalse(s.export_enabled)         # export held off
        self.assertTrue(p.store["storm_export_suppressed"])
        self.assertGreaterEqual(s.dawn_target_pct, plugin.STORM_SOC_YELLOW)

    def test_yellow_at_release_allows_export_but_keeps_floor(self):
        p = self._mk("yellow")
        s = self._snap()
        p._apply_storm_override(s, 90.0)          # 90 >= 85 release
        self.assertTrue(s.export_enabled)          # export re-enabled
        self.assertFalse(p.store["storm_export_suppressed"])
        # Resilience floor still applies above the release point.
        self.assertGreaterEqual(s.dawn_target_pct, plugin.STORM_SOC_YELLOW)

    def test_storm_reserve_is_flat_50_all_levels(self):
        # CliveS 26-Jun: all storm levels reserve a FLAT 50% (amber/red no longer 80),
        # and the storm floor is never raised above 50 — so no grid charge above 50%.
        self.assertEqual(plugin.STORM_SOC_YELLOW, 50.0)
        self.assertEqual(plugin.STORM_SOC_AMBER, 50.0)
        for level in ("yellow", "amber", "red"):
            p = self._mk(level)
            s = self._snap(dawn=10.0)
            p._apply_storm_override(s, 30.0)
            self.assertEqual(s.dawn_target_pct, 50.0)   # raised to the 50% reserve, never above

    def test_amber_above_release_allows_export(self):
        p = self._mk("amber")
        s = self._snap()
        p._apply_storm_override(s, 90.0)
        self.assertTrue(s.export_enabled)
        self.assertFalse(p.store["storm_export_suppressed"])

    def test_release_pct_pref_honoured(self):
        p = self._mk("yellow", prefs={"stormExportReleasePct": "92"})
        s = self._snap()
        p._apply_storm_override(s, 88.0)          # 88 < 92 -> suppressed
        self.assertFalse(s.export_enabled)
        s2 = self._snap()
        p._apply_storm_override(s2, 95.0)         # 95 >= 92 -> allowed
        self.assertTrue(s2.export_enabled)

    def test_release_pref_cannot_drop_below_reserve(self):
        # A misconfigured low release (40) must not let export resume below the 50% reserve.
        p = self._mk("amber", prefs={"stormExportReleasePct": "40"})
        s = self._snap()
        p._apply_storm_override(s, 45.0)          # release = max(40, 50) = 50; 45 < 50 -> suppressed
        self.assertFalse(s.export_enabled)
        s2 = self._snap()
        p._apply_storm_override(s2, 55.0)         # 55 >= 50 -> allowed
        self.assertTrue(s2.export_enabled)

    def test_bad_soc_fails_safe_to_suppressed(self):
        p = self._mk("red")
        s = self._snap()
        p._apply_storm_override(s, None)          # unknown SOC -> keep export off
        self.assertFalse(s.export_enabled)
        self.assertTrue(p.store["storm_export_suppressed"])

    def test_storm_never_lowers_higher_existing_floor(self):
        # A higher pre-existing floor (e.g. a 60% winter buffer set by
        # _apply_seasonal) must survive the max() — a storm can only RAISE the
        # dawn target, never lower it to the 50% storm reserve.
        p = self._mk("yellow")
        s = self._snap(dawn=60.0)
        p._apply_storm_override(s, 30.0)
        self.assertEqual(s.dawn_target_pct, 60.0)

    def test_storm_end_clears_suppression_flag(self):
        # When the storm ends, a previously-set suppression flag must reset —
        # export must not stay reported (or held) suppressed after the warning
        # clears.
        p = self._mk("none")
        p.store["storm_export_suppressed"] = True
        s = self._snap()
        p._apply_storm_override(s, 40.0)
        self.assertFalse(p.store["storm_export_suppressed"])
        self.assertTrue(s.export_enabled)


class TestWriteCostVariables(unittest.TestCase):
    """_write_cost_variables (v5.41) — republishes the orphaned Octopus cost/rate
    variables from the Kraken ledger + the live economics (single source)."""

    def _run(self, fin="default", econ=None):
        import tempfile, shutil, types as _types
        tmp = tempfile.mkdtemp()
        try:
            p = _mk_plugin(tmp, _FakeOcto(fin=fin))
            p._ensure_var = lambda name, fid: name          # use the var NAME as its id
            # v5.43: the writer reads the minimal _cost_vars_economics helper
            # (whole_house + periods only), not the full dashboard payload.
            p._cost_vars_economics = lambda: (econ or {})
            writes = {}
            # The harness mocks indigo.variables (plural) but not indigo.variable
            # (singular, what the writer uses) — install a capturing stand-in.
            had = hasattr(plugin.indigo, "variable")
            orig = getattr(plugin.indigo, "variable", None)
            plugin.indigo.variable = _types.SimpleNamespace(
                updateValue=lambda vid, val: writes.__setitem__(vid, val))
            try:
                p._write_cost_variables(0)
            finally:
                if had:
                    plugin.indigo.variable = orig
                else:
                    delattr(plugin.indigo, "variable")
            return writes
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_publishes_bill_exact_rates_and_costs(self):
        econ = {"whole_house": {"today": {"electric_gbp": 0.62, "gas_gbp": 0.80,
                                          "export_gbp": 1.45, "bill_gbp": 1.42}},
                "periods": {"month": {"import_total_gbp": 0.48, "export_total_gbp": 86.22}}}
        w = self._run(econ=econ)
        self.assertEqual(w["elec_unit_rate_p"], "23.4780")        # live Tracker rate, not stale
        self.assertEqual(w["elec_standing_charge_p"], "61.5182")
        self.assertEqual(w["gas_unit_rate_p"], "6.5841")
        self.assertEqual(w["gas_standing_charge_p"], "29.0617")
        self.assertEqual(w["export_rate_p"], "12.0000")
        self.assertEqual(w["export_rate"], "12.0000")             # legacy name the digest reads
        self.assertEqual(w["account_balance_gbp"], "392.39")
        self.assertEqual(w["elec_today_cost_gbp"], "0.62")
        self.assertEqual(w["gas_today_cost_gbp"], "0.80")
        self.assertEqual(w["export_today_revenue_gbp"], "1.45")
        self.assertEqual(w["combined_today_actual_gbp"], "1.42")
        self.assertEqual(w["elec_month_cost_gbp"], "0.48")
        self.assertEqual(w["export_month_revenue_gbp"], "86.22")

    def test_no_financials_is_graceful(self):
        # Kraken down + no economics → no writes, no crash, nothing blanked.
        self.assertEqual(self._run(fin=None, econ=None), {})

    def test_costs_published_even_if_ledger_down(self):
        w = self._run(fin=None, econ={"whole_house": {"today": {"electric_gbp": 0.62}}})
        self.assertNotIn("elec_unit_rate_p", w)                   # no rate without the ledger
        self.assertEqual(w["elec_today_cost_gbp"], "0.62")        # economics still published

    def test_month_vars_zero_when_no_rows_yet(self):
        # 1st of the month before any daily_history row exists: days==0 means
        # the month genuinely starts at zero — previously the write was
        # skipped and the CLOSED month's totals showed all day (v5.43).
        w = self._run(econ={"periods": {"month": {"days": 0,
                                                  "import_total_gbp": None,
                                                  "export_total_gbp": None}}})
        self.assertEqual(w["elec_month_cost_gbp"], "0.00")
        self.assertEqual(w["export_month_revenue_gbp"], "0.00")

    def test_month_cost_prefers_whole_house_basis(self):
        # Settled rows carry unit+standing — the month figure uses that basis
        # (matching elec_today_cost_gbp) over the unit-only aggregate.
        w = self._run(econ={"periods": {"month": {"days": 5,
                                                  "import_total_gbp": 0.48,
                                                  "elec_whole_house_total_gbp": 3.62,
                                                  "export_total_gbp": 40.0}}})
        self.assertEqual(w["elec_month_cost_gbp"], "3.62")


class TestWriteEnergySummaryVariables(unittest.TestCase):
    """_write_energy_summary_variables — the ~30-min writer of the sigen_*
    variables, including the self-sufficiency KPI formula. Previously only its
    _write_cost_variables callee was covered."""

    def _run(self, store):
        import tempfile, shutil, types as _types
        tmp = tempfile.mkdtemp()
        try:
            p = _mk_plugin(tmp, None, store=store)
            p._ensure_var           = lambda name, fid: name
            p._sigenergy_folder_id  = lambda: 0
            p._write_cost_variables = lambda folder_id: None   # covered separately
            p.latest_decision       = None
            writes = {}
            had  = hasattr(plugin.indigo, "variable")
            orig = getattr(plugin.indigo, "variable", None)
            plugin.indigo.variable = _types.SimpleNamespace(
                updateValue=lambda vid, val: writes.__setitem__(vid, val))
            try:
                p._write_energy_summary_variables()
            finally:
                if had:
                    plugin.indigo.variable = orig
                else:
                    delattr(plugin.indigo, "variable")
            return writes
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_normal_day_self_sufficiency(self):
        w = self._run({"pv_daily_kwh": 41.24, "grid_import_daily_kwh": 0.05,
                       "home_daily_kwh": 7.9, "grid_export_daily_kwh": 20.0,
                       "peak_soc": 94.4, "min_soc": 67.6})
        # (1 - 0.05/7.9) * 100 = 99.4 (rounded 1dp)
        self.assertEqual(w["sigen_today_self_suff_pct"], "99.4")
        self.assertEqual(w["sigen_today_pv_kwh"], "41.24")

    def test_zero_home_load_is_100pct(self):
        # Nothing was needed from the grid on a zero-load day — 100%, matching
        # the dashboard computation (was 0.0 here, an inconsistency).
        w = self._run({"home_daily_kwh": 0.0, "grid_import_daily_kwh": 0.0})
        self.assertEqual(w["sigen_today_self_suff_pct"], "100.0")

    def test_import_exceeding_home_clamps_to_zero(self):
        w = self._run({"home_daily_kwh": 1.0, "grid_import_daily_kwh": 5.0})
        self.assertEqual(w["sigen_today_self_suff_pct"], "0.0")

    def test_every_value_written_is_str(self):
        w = self._run({"pv_daily_kwh": 1.5, "grid_import_daily_kwh": 0.1,
                       "home_daily_kwh": 3.0})
        for name, value in w.items():
            self.assertIsInstance(value, str, name)

    def test_one_failing_write_does_not_abort_the_rest(self):
        import tempfile, shutil, types as _types
        tmp = tempfile.mkdtemp()
        try:
            p = _mk_plugin(tmp, None, store={"home_daily_kwh": 3.0})
            p._ensure_var           = lambda name, fid: name
            p._sigenergy_folder_id  = lambda: 0
            p._write_cost_variables = lambda folder_id: None
            p.latest_decision       = None
            writes = {}

            def _upd(vid, val):
                if vid == "sigen_today_pv_kwh":
                    raise RuntimeError("boom")
                writes[vid] = val
            had  = hasattr(plugin.indigo, "variable")
            orig = getattr(plugin.indigo, "variable", None)
            plugin.indigo.variable = _types.SimpleNamespace(updateValue=_upd)
            try:
                p._write_energy_summary_variables()
            finally:
                if had:
                    plugin.indigo.variable = orig
                else:
                    delattr(plugin.indigo, "variable")
            self.assertIn("sigen_today_self_suff_pct", writes)   # later writes still ran
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestVppRestartPersistence(unittest.TestCase):
    """v5.48.0 — an in-progress Axle VPP window must survive a plugin restart.

    Covers the three pure helpers behind _rehydrate_vpp_state: the serialise /
    deserialise round-trip (JSON-safe, DST-safe, tz-aware UTC) and the
    resume/ended/idle decision that drives whether a restart re-enters the window
    without the Axle API or cleans up a window that closed during downtime.
    """

    def _event(self, start, end):
        return {
            "start_time":            start,
            "end_time":              end,
            "import_export":         "export",
            "duration_hrs":          (end - start).total_seconds() / 3600.0,
            "forecast_dispatch_kwh": 5.5,
            "estimated_revenue_p":   550.0,
            "raw":                   {"anything": "dropped on serialise"},
        }

    # ---- serialise / deserialise round-trip ----
    def test_roundtrip_preserves_utc_instant(self):
        start = datetime(2026, 7, 6, 18, 0, tzinfo=timezone.utc)
        end   = datetime(2026, 7, 6, 19, 30, tzinfo=timezone.utc)
        ser   = plugin._serialise_vpp_event(self._event(start, end))
        # JSON-safe: datetimes are strings, and the raw blob is dropped.
        self.assertIsInstance(ser["start_time"], str)
        self.assertNotIn("raw", ser)
        import json
        json.dumps(ser)   # must not raise
        back = plugin._deserialise_vpp_event(ser)
        self.assertEqual(back["start_time"], start)
        self.assertEqual(back["end_time"], end)
        self.assertEqual(back["import_export"], "export")
        self.assertAlmostEqual(back["duration_hrs"], 1.5, places=6)
        self.assertEqual(back["forecast_dispatch_kwh"], 5.5)

    def test_deserialise_normalises_offset_to_utc(self):
        # A BST (+01:00) window must come back as the same UTC instant — the DST
        # trap that shifted Predbat's notifications an hour can't occur here.
        ser  = {"start_time": "2026-07-06T19:00:00+01:00",
                "end_time":   "2026-07-06T20:00:00+01:00"}
        back = plugin._deserialise_vpp_event(ser)
        self.assertEqual(back["start_time"],
                         datetime(2026, 7, 6, 18, 0, tzinfo=timezone.utc))
        self.assertEqual(back["start_time"].tzinfo, timezone.utc)

    def test_deserialise_rejects_corrupt_payload(self):
        self.assertIsNone(plugin._deserialise_vpp_event(None))
        self.assertIsNone(plugin._deserialise_vpp_event({}))
        self.assertIsNone(plugin._deserialise_vpp_event({"start_time": "not-a-date",
                                                         "end_time": "also-bad"}))

    def test_serialise_none_event(self):
        self.assertIsNone(plugin._serialise_vpp_event(None))

    # ---- resume / ended / idle decision ----
    def test_decision_resume_while_window_open(self):
        start = datetime(2026, 7, 6, 18, 0, tzinfo=timezone.utc)
        end   = datetime(2026, 7, 6, 19, 30, tzinfo=timezone.utc)
        now   = datetime(2026, 7, 6, 18, 45, tzinfo=timezone.utc)   # mid-window
        self.assertEqual(
            plugin._vpp_resume_decision(plugin.VPP_ACTIVE, self._event(start, end), now),
            "resume")

    def test_decision_ended_past_tail(self):
        start = datetime(2026, 7, 6, 18, 0, tzinfo=timezone.utc)
        end   = datetime(2026, 7, 6, 19, 30, tzinfo=timezone.utc)
        now   = datetime(2026, 7, 6, 19, 40, tzinfo=timezone.utc)   # past end + 2min tail
        self.assertEqual(
            plugin._vpp_resume_decision(plugin.VPP_ACTIVE, self._event(start, end), now),
            "ended")

    def test_decision_resume_within_tail(self):
        start = datetime(2026, 7, 6, 18, 0, tzinfo=timezone.utc)
        end   = datetime(2026, 7, 6, 19, 30, tzinfo=timezone.utc)
        now   = datetime(2026, 7, 6, 19, 31, tzinfo=timezone.utc)   # inside the +2min tail
        self.assertEqual(
            plugin._vpp_resume_decision(plugin.VPP_ACTIVE, self._event(start, end), now),
            "resume")

    def test_decision_resume_across_midnight(self):
        # A window that spans midnight must resume when the restart lands after
        # 00:00 — the reason the persisted state is restored regardless of day.
        start = datetime(2026, 7, 6, 23, 30, tzinfo=timezone.utc)
        end   = datetime(2026, 7, 7, 0, 30, tzinfo=timezone.utc)
        now   = datetime(2026, 7, 7, 0, 5, tzinfo=timezone.utc)
        self.assertEqual(
            plugin._vpp_resume_decision(plugin.VPP_PRE_CHARGING, self._event(start, end), now),
            "resume")

    def test_decision_idle_when_state_idle(self):
        start = datetime(2026, 7, 6, 18, 0, tzinfo=timezone.utc)
        end   = datetime(2026, 7, 6, 19, 30, tzinfo=timezone.utc)
        now   = datetime(2026, 7, 6, 18, 45, tzinfo=timezone.utc)
        self.assertEqual(
            plugin._vpp_resume_decision(plugin.VPP_IDLE, self._event(start, end), now),
            "idle")

    def test_decision_idle_when_no_event(self):
        now = datetime(2026, 7, 6, 18, 45, tzinfo=timezone.utc)
        self.assertEqual(
            plugin._vpp_resume_decision(plugin.VPP_ACTIVE, None, now), "idle")


class TestRowStandingCharge(unittest.TestCase):
    """_row_standing_p — the per-day standing charge with its ledger fallback.

    87 of the first 121 live history rows carry no elec_standing_p_day, so the
    fallback is the common path, not the edge case.
    """

    def test_prefers_the_value_frozen_on_the_day(self):
        self.assertEqual(
            plugin.Plugin._row_standing_p({"elec_standing_p_day": 50.0}, 61.5), 50.0)

    def test_falls_back_to_the_current_ledger(self):
        self.assertEqual(plugin.Plugin._row_standing_p({}, 61.5), 61.5)
        self.assertEqual(
            plugin.Plugin._row_standing_p({"elec_standing_p_day": None}, 61.5), 61.5)

    def test_zero_when_nothing_is_known(self):
        # Never invent a charge — understating beats making one up.
        self.assertEqual(plugin.Plugin._row_standing_p({}, None), 0.0)

    def test_non_numeric_falls_through(self):
        self.assertEqual(
            plugin.Plugin._row_standing_p({"elec_standing_p_day": "n/a"}, 61.5), 61.5)
        self.assertEqual(plugin.Plugin._row_standing_p({}, "n/a"), 0.0)

    def test_a_genuine_zero_standing_charge_is_honoured(self):
        self.assertEqual(
            plugin.Plugin._row_standing_p({"elec_standing_p_day": 0.0}, 61.5), 0.0)


class TestPeriodTotalsIdentity(unittest.TestCase):
    """The Period totals row must read as an identity the eye can check:

        solar benefit  =  grid-only  -  elec bill  +  export earned

    It did not before: "Elec bill" carried the standing charge on settled days
    only, and "Grid-only" never carried it at all, so the row was out by one
    standing charge per unsettled day (~£0.62).
    """

    RATE_P, STAND_P, EXPORT_P = 20.0, 61.5, 12.0

    def _periods(self, rows):
        import json, tempfile, shutil, os as _os
        tmp = tempfile.mkdtemp()
        try:
            with open(_os.path.join(tmp, "daily_history.json"), "w", encoding="utf-8") as f:
                json.dump(rows, f)
            p = _mk_plugin(tmp, _FakeOcto())
            return p._period_economics_summary(
                export_rate_p=self.EXPORT_P, fallback_import_rate_p=self.RATE_P)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def _row(self, days_back, settled):
        from datetime import timedelta
        d = (_london_today() - timedelta(days=days_back)).strftime("%Y-%m-%d")
        r = {"date": d, "month": d[:7], "home_kwh": 20.0, "grid_import_kwh": 5.0,
             "grid_export_kwh": 10.0, "rate_today_p": self.RATE_P,
             "export_rate_p": self.EXPORT_P, "elec_standing_p_day": self.STAND_P}
        if settled:
            r.update({"cost_settled": True,
                      "elec_unit_cost_gbp": 5.0 * self.RATE_P / 100.0,
                      "elec_standing_gbp":  self.STAND_P / 100.0})
        return r

    def _assert_identity(self, w):
        self.assertAlmostEqual(
            w["benefit_total_gbp"],
            round(w["no_solar_total_gbp"]
                  - w["elec_whole_house_total_gbp"]
                  + w["export_total_gbp"], 2),
            delta=0.02)

    def test_identity_holds_for_settled_days(self):
        self._assert_identity(self._periods([self._row(n, True) for n in (1, 2, 3)])["week"])

    def test_identity_holds_for_unsettled_days(self):
        # THE BUG: Octopus settles ~a day in arrears, so a 7-day window nearly
        # always holds an unsettled day. Those contributed unit cost only.
        self._assert_identity(self._periods([self._row(n, False) for n in (1, 2, 3)])["week"])

    def test_identity_holds_for_a_mixed_window(self):
        rows = [self._row(1, False), self._row(2, True), self._row(3, True)]
        self._assert_identity(self._periods(rows)["week"])

    def test_unsettled_day_carries_its_standing_charge(self):
        w = self._periods([self._row(1, False)])["week"]
        # unit 5 kWh x 20p = £1.00, plus 61.5p standing = £1.615
        self.assertAlmostEqual(w["elec_whole_house_total_gbp"], 1.615, delta=0.01)

    def test_grid_only_carries_the_standing_charge(self):
        w = self._periods([self._row(1, False)])["week"]
        # 20 kWh x 20p = £4.00, plus the same 61.5p standing charge
        self.assertAlmostEqual(w["no_solar_total_gbp"], 4.62, delta=0.01)

    def test_benefit_is_unchanged_by_the_standing_charge(self):
        # Standing cancels in (no_sol + st) - (imp + st) + exp, so the headline
        # saving must not move. 20x20p - 5x20p + 10x12p = £4.20.
        w = self._periods([self._row(1, False)])["week"]
        self.assertAlmostEqual(w["benefit_total_gbp"], 4.20, delta=0.01)

    def test_calendar_months_report_the_elec_bill_too(self):
        import json, tempfile, shutil, os as _os
        tmp = tempfile.mkdtemp()
        try:
            with open(_os.path.join(tmp, "daily_history.json"), "w", encoding="utf-8") as f:
                json.dump([self._row(1, False)], f)
            cal = _mk_plugin(tmp, _FakeOcto())._calendar_months_summary(
                export_rate_p=self.EXPORT_P, fallback_import_rate_p=self.RATE_P)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        live = [m for m in cal["months"] if m["days"]]
        self.assertTrue(live, "expected one month with data")
        self._assert_identity(live[0])


class TestWholeHouseCardRateMissing(unittest.TestCase):
    """_wh_build_card must not paint a green "Covered" badge over a bill it
    could not work out. With no unit rate it used to bill the standing charge
    alone, which export nearly always beat."""

    def _card(self, unit_p, import_kwh=8.0, export_kwh=10.0):
        return plugin.Plugin._wh_build_card(
            import_kwh, export_kwh, unit_p, 12.0, 61.5,
            gas_kwh=0.0, gas_unit_p=0.0, gas_standing_p=0.0,
            provisional=True, gas_estimated=False)

    def test_missing_rate_blanks_the_verdict(self):
        c = self._card(None)
        self.assertTrue(c["rate_missing"])
        self.assertIsNone(c["bill_gbp"])
        self.assertIsNone(c["net_gbp"])
        self.assertIsNone(c["covered"])
        self.assertIsNone(c["electric_gbp"])

    def test_known_rate_still_answers(self):
        c = self._card(20.0)
        self.assertFalse(c["rate_missing"])
        self.assertAlmostEqual(c["bill_gbp"], 8.0 * 0.20 + 0.615, delta=0.01)
        self.assertFalse(c["covered"])          # £1.20 export vs £2.22 bill

    def test_no_import_needs_no_rate(self):
        # Nothing drawn from the grid, so a missing rate costs nothing and the
        # card can still be trusted.
        c = self._card(None, import_kwh=0.0)
        self.assertFalse(c["rate_missing"])
        self.assertIsNotNone(c["bill_gbp"])

    def test_zero_export_rate_is_a_real_rate(self):
        # `if export_rate_p else 12.0` silently swapped a genuine 0p for 12p.
        c = plugin.Plugin._wh_build_card(
            0.0, 10.0, 20.0, 0.0, 61.5, 0.0, 0.0, 0.0,
            provisional=True, gas_estimated=False)
        self.assertEqual(c["export_gbp"], 0.0)

    def test_gas_estimated_needs_a_gas_meter(self):
        tmp_rec = {"grid_import_kwh": 5.0, "grid_export_kwh": 10.0,
                   "rate_today_p": 20.0, "export_rate_p": 12.0,
                   "elec_standing_p_day": 61.5}
        p = plugin.Plugin.__new__(plugin.Plugin)
        with_gas = p._wh_provisional_from_row(tmp_rec, 61.5, 6.0, 29.0, 8.0, 20.0,
                                              has_gas=True)
        without  = p._wh_provisional_from_row(tmp_rec, 61.5, 6.0, 29.0, 8.0, 20.0,
                                              has_gas=False)
        self.assertTrue(with_gas["gas_estimated"])
        self.assertFalse(without["gas_estimated"])


class TestBatteryCapacityPublished(unittest.TestCase):
    """energy.html hardcoded 35.04 kWh, which is wrong for every other install."""

    def test_capacity_constant_is_sane(self):
        self.assertGreater(plugin.BATTERY_CAPACITY_KWH, 0)

    def test_default_export_rate_constant_exists(self):
        self.assertEqual(plugin.DEFAULT_EXPORT_RATE_P, 12.0)


class TestBackupRuntimeHours(unittest.TestCase):
    """v5.53.0: how long the battery would carry the house, for the power-cut alerts.

    Usable energy is everything ABOVE the discharge cutoff, because the inverter
    stops there — counting the last percent would overstate the backup in the one
    message where being optimistic is worst.
    """

    def test_typical_case(self):
        # 74% of 35.04 kWh above a 1% floor = 25.6 kWh, against a 647 W house.
        hours = plugin._backup_runtime_hours(74.0, 1.0, 35.04, 647.0)
        self.assertAlmostEqual(hours, 25.579 / 0.647, places=1)

    def test_floor_is_subtracted(self):
        with_floor = plugin._backup_runtime_hours(50.0, 10.0, 10.0, 1000.0)
        no_floor   = plugin._backup_runtime_hours(50.0, 0.0, 10.0, 1000.0)
        self.assertEqual(with_floor, 4.0)     # (50-10)% of 10 kWh at 1 kW
        self.assertEqual(no_floor, 5.0)
        self.assertLess(with_floor, no_floor)

    def test_at_or_below_floor_is_zero_not_negative(self):
        self.assertEqual(plugin._backup_runtime_hours(1.0, 1.0, 35.04, 500.0), 0.0)
        self.assertEqual(plugin._backup_runtime_hours(0.5, 1.0, 35.04, 500.0), 0.0)

    def test_unknown_inputs_return_none(self):
        # None rather than a number — the caller omits the line instead of guessing.
        self.assertIsNone(plugin._backup_runtime_hours(None, 1.0, 35.04, 647.0))
        self.assertIsNone(plugin._backup_runtime_hours(74.0, 1.0, 35.04, None))
        self.assertIsNone(plugin._backup_runtime_hours("abc", 1.0, 35.04, 647.0))

    def test_zero_or_negative_load_returns_none(self):
        # A meter dropping out mid-outage must not divide by zero or read as forever.
        self.assertIsNone(plugin._backup_runtime_hours(74.0, 1.0, 35.04, 0.0))
        self.assertIsNone(plugin._backup_runtime_hours(74.0, 1.0, 35.04, -50.0))

    def test_zero_capacity_returns_none(self):
        self.assertIsNone(plugin._backup_runtime_hours(74.0, 1.0, 0.0, 647.0))


class TestFormatRuntime(unittest.TestCase):
    """v5.53.0: the runtime figure has to read well on a phone at 3am."""

    def test_minutes_under_the_hour(self):
        self.assertEqual(plugin._format_runtime(0.5), "30 minutes")

    def test_one_decimal_to_half_a_day(self):
        self.assertEqual(plugin._format_runtime(3.25), "3.2 hours")

    def test_whole_hours_to_two_days(self):
        self.assertEqual(plugin._format_runtime(21.4), "21 hours")

    def test_days_beyond_two(self):
        self.assertEqual(plugin._format_runtime(60.0), "2.5 days")

    def test_capped_at_ten_days(self):
        self.assertEqual(plugin._format_runtime(500.0), "10+ days")

    def test_none_and_zero_return_none(self):
        self.assertIsNone(plugin._format_runtime(None))
        self.assertIsNone(plugin._format_runtime(0.0))
        self.assertIsNone(plugin._format_runtime("abc"))


class TestLockoutMessage(unittest.TestCase):
    """v5.53.0: the restore alert must name BOTH ways the export lockout ends.

    26-Jul-2026: an 83-second cut was followed by export correctly resuming at 74%
    on the solar-refill rule, from a plugin whose message had promised 85%. The
    behaviour was right and the sentence was wrong, which is indistinguishable
    from a fault when you are reading it on a phone.
    """

    def test_names_both_release_rules(self):
        msg = plugin._lockout_message(True, "12:41", 85.0)
        self.assertIn("12:41", msg)
        self.assertIn("85%", msg)
        self.assertIn("solar", msg.lower())

    def test_floor_is_not_hardcoded(self):
        msg = plugin._lockout_message(True, "12:41", 70.0)
        self.assertIn("70%", msg)
        self.assertNotIn("85%", msg)

    def test_export_disabled_says_so(self):
        msg = plugin._lockout_message(False, "12:41", 85.0)
        self.assertIn("switched off", msg)
        self.assertNotIn("12:41", msg)      # no lockout time when nothing is held

    def test_no_end_time_omits_the_paragraph(self):
        self.assertIsNone(plugin._lockout_message(True, "", 85.0))

    # --- v5.54.0: say WHICH way the solar rule has gone, with the figures ---

    def test_outlook_yes_says_export_restarts(self):
        msg = plugin._lockout_message(True, "12:40", 85.0, {
            "releases": True, "surplus_kwh": 10.6, "needed_kwh": 4.6, "is_daytime": True})
        self.assertIn("covers that reserve on its own", msg)
        self.assertIn("10.6 kWh spare", msg)
        self.assertIn("4.6 kWh needed", msg)
        self.assertIn("restart within the minute", msg)

    def test_outlook_no_in_daylight_says_not_yet_with_figures(self):
        msg = plugin._lockout_message(True, "12:40", 85.0, {
            "releases": False, "surplus_kwh": 3.1, "needed_kwh": 5.9, "is_daytime": True})
        self.assertIn("does not cover that reserve yet", msg)
        self.assertIn("3.1 kWh spare", msg)
        self.assertIn("5.9 kWh needed", msg)
        self.assertIn("forecast improves", msg)

    def test_outlook_no_at_night_does_not_dangle_a_forecast(self):
        # At night there is no forecast to improve — saying so would be false hope.
        msg = plugin._lockout_message(True, "01:40", 85.0, {
            "releases": False, "surplus_kwh": 0.0, "needed_kwh": 6.0, "is_daytime": False})
        self.assertIn("no solar left today", msg)
        self.assertNotIn("forecast improves", msg)
        self.assertIn("85%", msg)

    def test_outlook_none_falls_back_to_naming_both_rules(self):
        # Unknown must not be dressed up as either answer.
        msg = plugin._lockout_message(True, "12:40", 85.0, None)
        self.assertIn("or if today's solar can refill", msg)
        self.assertNotIn("does not cover", msg)
        self.assertNotIn("covers that reserve on its own", msg)

    def test_missing_figures_still_gives_the_verdict(self):
        msg = plugin._lockout_message(True, "12:40", 85.0,
                                      {"releases": True, "is_daytime": True})
        self.assertIn("covers that reserve on its own", msg)
        self.assertNotIn("kWh spare", msg)      # no invented numbers


class TestPowerCutStatusLines(unittest.TestCase):
    """v5.53.0: assembly of the alert body. A missing reading costs one line,
    never the message."""

    class _Stub:
        def __init__(self, prefs, data, outlook=None):
            self.pluginPrefs = prefs
            self.latest_inverter_data = data
            self._outlook = outlook

        def _power_cut_lockout_soc_floor(self):
            return plugin.Plugin._power_cut_lockout_soc_floor(self)

        def _power_cut_lockout_end_local(self):
            return plugin.Plugin._power_cut_lockout_end_local(self)

        def _solar_refill_outlook(self, soc_pct):
            # Stubbed: the outlook needs the whole manager + forecast stack.
            # Its own arithmetic is covered by TestSolarRefillOutlook.
            return self._outlook

    def _lines(self, kind, prefs=None, data=None, outlook=None):
        prefs = dict(prefs or {})
        prefs.setdefault("batteryCapacityKwh", "35.04")
        prefs.setdefault("batteryHealthCutoff", "1")
        stub = self._Stub(prefs, data if data is not None else {}, outlook)
        return plugin.Plugin._power_cut_status_lines(stub, kind)

    def test_lost_reports_battery_load_and_runtime(self):
        lines = self._lines("lost", data={"batterySoc": 74.0, "homePowerWatts": 647.0})
        self.assertEqual(len(lines), 1)
        self.assertIn("74%", lines[0])
        self.assertIn("647 W", lines[0])
        self.assertIn("carry it for about", lines[0])

    def test_restore_adds_the_lockout_paragraph(self):
        restored = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        lines = self._lines(
            "restored",
            prefs={"exportEnabled": True, "powerRestoredTime": restored},
            data={"batterySoc": 74.0, "homePowerWatts": 647.0},
        )
        self.assertEqual(len(lines), 2)
        self.assertIn("backup", lines[0])
        self.assertIn("Export is held off until", lines[1])

    def test_missing_soc_drops_only_the_battery_line(self):
        restored = (datetime.now(timezone.utc)).isoformat()
        lines = self._lines(
            "restored",
            prefs={"exportEnabled": True, "powerRestoredTime": restored},
            data={},                       # partial Modbus read — no SOC at all
        )
        self.assertEqual(len(lines), 1)
        self.assertIn("Export is held off", lines[0])

    def test_missing_load_still_reports_the_battery(self):
        lines = self._lines("lost", data={"batterySoc": 74.0})
        self.assertEqual(len(lines), 1)
        self.assertIn("74%", lines[0])
        self.assertNotIn("drawing", lines[0])
        self.assertNotIn("about", lines[0])   # no runtime without a load figure

    def test_lost_never_mentions_the_lockout(self):
        # The lockout only exists after a restore. Quoting a stale powerRestoredTime
        # during a live outage would be nonsense.
        stale = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        lines = self._lines(
            "lost",
            prefs={"exportEnabled": True, "powerRestoredTime": stale},
            data={"batterySoc": 74.0, "homePowerWatts": 647.0},
        )
        self.assertEqual(len(lines), 1)
        self.assertNotIn("Export", lines[0])

    def test_corrupt_restored_time_omits_the_lockout_paragraph(self):
        lines = self._lines(
            "restored",
            prefs={"exportEnabled": True, "powerRestoredTime": "not-a-date"},
            data={"batterySoc": 74.0, "homePowerWatts": 647.0},
        )
        self.assertEqual(len(lines), 1)          # battery line only, no bad time quoted

    def test_lockout_end_is_four_hours_after_the_restore(self):
        restored = datetime(2026, 7, 26, 7, 40, 52, tzinfo=timezone.utc)
        stub = self._Stub({"powerRestoredTime": restored.isoformat()}, {})
        # 07:40 UTC + 4h = 11:40 UTC = 12:40 BST. Asserted exactly: the module
        # resolves Europe/London through stdlib zoneinfo, so there is no longer a
        # pytz-less host to tolerate — and "12:40 or 11:40" would have passed on
        # the silent-UTC bug this release removes.
        self.assertEqual(plugin.Plugin._power_cut_lockout_end_local(stub), "12:40")


class TestSolarRefillOutlook(unittest.TestCase):
    """v5.54.0: the alert works out whether today's solar will refill the reserve,
    instead of leaving the reader to guess which of the two rules applies to them.

    Uses the manager's OWN balance so the message and the decision cannot disagree,
    and returns None — not a guess — whenever it cannot be worked out.
    """

    class _Balance:
        def __init__(self, is_daytime=True, battery_kwh=26.1,
                     remaining_solar_kwh=21.3, remaining_home_to_dusk_kwh=10.7):
            self.is_daytime                 = is_daytime
            self.battery_kwh                = battery_kwh
            self.remaining_solar_kwh        = remaining_solar_kwh
            self.remaining_home_to_dusk_kwh = remaining_home_to_dusk_kwh

    class _Manager:
        def __init__(self, balance):
            self._balance = balance

        def _calculate_24h_balance(self, snapshot):
            if self._balance is None:
                raise RuntimeError("no forecast yet")
            return self._balance

    class _Stub:
        def __init__(self, balance, prefs=None):
            self.pluginPrefs = prefs if prefs is not None else {}
            self.manager     = TestSolarRefillOutlook._Manager(balance)

        def _build_manager_snapshot(self, soc, export_enabled, vpp):
            return object()

        def _power_cut_lockout_soc_floor(self):
            return plugin.Plugin._power_cut_lockout_soc_floor(self)

        def _power_cut_lockout_min_soc(self):
            return plugin.Plugin._power_cut_lockout_min_soc(self)

    def _outlook(self, soc, balance, prefs=None):
        prefs = dict(prefs or {})
        prefs.setdefault("batteryCapacityKwh", "35.04")
        return plugin.Plugin._solar_refill_outlook(self._Stub(balance, prefs), soc)

    def test_this_mornings_figures_release(self):
        # 26-Jul-2026 09:00: 21.3 kWh solar left vs 10.7 to dusk = 10.6 spare;
        # 85% of 35.04 = 29.78 kWh floor, battery 26.1 → 3.68 short, × 1.25 = 4.6.
        out = self._outlook(74.5, self._Balance())
        self.assertTrue(out["releases"])
        self.assertAlmostEqual(out["surplus_kwh"], 10.6, places=1)
        self.assertAlmostEqual(out["needed_kwh"], 4.6, places=1)

    def test_needed_quotes_the_margin_inflated_bar(self):
        # The gate is needed × 1.25, so quoting the raw gap would understate it
        # and make a "not yet" verdict look wrong to anyone checking the sums.
        out = self._outlook(74.5, self._Balance())
        raw = 29.784 - 26.1
        self.assertGreater(out["needed_kwh"], raw)
        self.assertAlmostEqual(out["needed_kwh"],
                               raw * plugin.POWER_CUT_LOCKOUT_REFILL_MARGIN, places=2)

    def test_dull_day_holds(self):
        out = self._outlook(74.5, self._Balance(remaining_solar_kwh=12.0,
                                                remaining_home_to_dusk_kwh=10.0))
        self.assertFalse(out["releases"])
        self.assertAlmostEqual(out["surplus_kwh"], 2.0, places=1)

    def test_night_holds_and_reports_night(self):
        out = self._outlook(74.5, self._Balance(is_daytime=False,
                                                remaining_solar_kwh=0.0,
                                                remaining_home_to_dusk_kwh=0.0))
        self.assertFalse(out["releases"])
        self.assertFalse(out["is_daytime"])

    def test_surplus_never_negative(self):
        out = self._outlook(74.5, self._Balance(remaining_solar_kwh=1.0,
                                                remaining_home_to_dusk_kwh=9.0))
        self.assertEqual(out["surplus_kwh"], 0.0)

    def test_unknown_soc_returns_none(self):
        self.assertIsNone(self._outlook(None, self._Balance()))

    def test_balance_failure_returns_none(self):
        # None, not a guess — the caller then names both rules and claims nothing.
        self.assertIsNone(self._outlook(74.5, None))

    def test_below_min_soc_holds_however_good_the_forecast(self):
        out = self._outlook(20.0, self._Balance(battery_kwh=7.0,
                                                remaining_solar_kwh=60.0,
                                                remaining_home_to_dusk_kwh=5.0))
        self.assertFalse(out["releases"])


class TestApplyStormResultLocName(unittest.TestCase):
    """v5.53.0 regression: _apply_storm_result referenced loc_name, which the
    v5.45.0 locking restructure left behind in _check_storm_watch.

    The two bodies that quote it — yellow escalation and the all-clear — raised
    NameError instead of sending. The sting is in the tail: storm_alerted_level
    is only written AFTER a successful send, so a failed all-clear left it stuck
    at the old level and every later storm at or below it was judged "already
    alerted" and stayed silent. Amber and red never quote the name, so those kept
    working, which is why nothing looked broken.

    These tests raise NameError against the pre-fix code.
    """

    class _Stub:
        def __init__(self, store, prefs=None):
            self.store       = store
            self.pluginPrefs = prefs if prefs is not None else {}
            self.sent        = []

        def _send_pushover(self, title, body, priority="0"):
            self.sent.append((title, body, priority))

        def _save_accumulators(self):
            pass

    def _apply(self, stub, level, reason="test reason"):
        plugin.Plugin._apply_storm_result(stub, level, reason)

    def test_yellow_escalation_sends(self):
        stub = self._Stub({"storm_level": "none", "storm_alerted_level": "none"},
                          {"siteLocationName": "Medomsley"})
        self._apply(stub, "yellow")
        self.assertEqual(len(stub.sent), 1)
        self.assertIn("Yellow", stub.sent[0][0])
        self.assertIn("Medomsley", stub.sent[0][1])
        self.assertEqual(stub.store["storm_alerted_level"], "yellow")

    def test_all_clear_sends_and_resets_the_latch(self):
        stub = self._Stub({"storm_level": "amber", "storm_alerted_level": "amber"},
                          {"siteLocationName": "Medomsley"})
        self._apply(stub, "none")
        self.assertEqual(len(stub.sent), 1)
        self.assertIn("Cleared", stub.sent[0][0])
        # The latch MUST reset, or the next storm is judged already-alerted.
        self.assertEqual(stub.store["storm_alerted_level"], "none")

    def test_unset_location_falls_back(self):
        stub = self._Stub({"storm_level": "none", "storm_alerted_level": "none"}, {})
        self._apply(stub, "yellow")
        self.assertIn("your area", stub.sent[0][1])

    def test_amber_and_red_still_send(self):
        for level, word in (("amber", "Amber"), ("red", "RED")):
            stub = self._Stub({"storm_level": "none", "storm_alerted_level": "none"})
            self._apply(stub, level)
            self.assertEqual(len(stub.sent), 1, level)
            self.assertIn(word, stub.sent[0][0])
            self.assertEqual(stub.sent[0][2], "1")      # high priority

    def test_no_alert_when_level_unchanged(self):
        stub = self._Stub({"storm_level": "amber", "storm_alerted_level": "amber"})
        self._apply(stub, "amber")
        self.assertEqual(stub.sent, [])


class TestVppEventStr(unittest.TestCase):
    """v5.55.1 — the dashboard's VPP window used to be a hardcoded "".

    `/api/status` published `"event_str": ""` as a literal from the day the
    block was written, so every consumer that appends it rendered "VPP event
    announced:" and then stopped. The one fact worth showing — WHEN — was the
    one that was missing, and it went unseen because nothing had ever taken
    that branch: the feed was dead (see 5.55.0) so no event had reached it.

    The empty string still has to mean "say nothing", because the callers all
    guard on falsiness — so the tests below pin BOTH directions: a real window
    renders, and a missing or malformed one renders nothing rather than a
    half-sentence or an exception.
    """

    def _p(self, event):
        p = plugin.Plugin.__new__(plugin.Plugin)
        p.store = {"vpp_event": event}
        return p

    def _utc(self, y, m, d, hh, mm):
        return datetime(y, m, d, hh, mm, tzinfo=timezone.utc)

    def test_todays_window_is_short(self):
        # 18:00Z = 19:00 BST — and the local conversion is the point: printing
        # the stored UTC raw would read an hour early all summer.
        today = datetime.now().date()
        ev = {"start_time": datetime(today.year, today.month, today.day, 18, 0, tzinfo=timezone.utc),
              "end_time":   datetime(today.year, today.month, today.day, 19, 0, tzinfo=timezone.utc)}
        s = self._p(ev)._vpp_event_str()
        self.assertRegex(s, r"^\d{2}:\d{2}-\d{2}:\d{2}$")
        self.assertNotIn("/", s)          # today's window carries no date

    def test_other_day_window_carries_the_date(self):
        ev = {"start_time": self._utc(2026, 3, 20, 18, 0),
              "end_time":   self._utc(2026, 3, 20, 19, 30)}
        s = self._p(ev)._vpp_event_str()
        self.assertIn("20/03", s)
        self.assertIn("-", s)

    def test_no_event_says_nothing(self):
        self.assertEqual(self._p(None)._vpp_event_str(), "")
        self.assertEqual(self._p({})._vpp_event_str(), "")

    def test_half_an_event_says_nothing(self):
        # A start with no end must not render "19:00-" .
        ev = {"start_time": self._utc(2026, 3, 20, 18, 0), "end_time": None}
        self.assertEqual(self._p(ev)._vpp_event_str(), "")

    def test_malformed_event_does_not_raise(self):
        # This runs inside the /api/status build — an exception here would blank
        # the whole payload and take every dashboard card with it.
        ev = {"start_time": "not-a-datetime", "end_time": "nor-this"}
        self.assertEqual(self._p(ev)._vpp_event_str(), "")


class TestVppShortfallAlert(unittest.TestCase):
    """Pre-charge Pushovers a heads-up when the battery is short (v5.58.0).

    Before this, a shortfall produced ONE log line and nothing else, so the
    first anyone knew of an under-delivered window was the settlement figure
    days later. Axle opt SigEnergy members in BY DEFAULT from 08-Aug-2026, and
    their new short-notice events give as little as 2 h — far less room for
    solar to top the battery up before the window opens."""

    def _p(self, soc_pct, cap_kwh=35.04, max_export_kw=4.0, daytime=True):
        p = plugin.Plugin.__new__(plugin.Plugin)
        p.logger = MagicMock()
        p.modbus = MagicMock()
        p.store  = {}
        p.pluginPrefs = {
            "batteryCapacityKwh": str(cap_kwh),
            "maxExportKw":        str(max_export_kw),
            "dawnSocTarget":      "15",
        }
        p.latest_inverter_data      = {"batterySoc": soc_pct}
        p._event_is_daytime         = MagicMock(return_value=daytime)
        p._set_vpp_discharge_cutoff = MagicMock()
        p._vpp_transition           = MagicMock()
        p._vpp_event_str            = MagicMock(return_value="21:00-22:00")
        p._send_pushover            = MagicMock()
        return p

    @staticmethod
    def _event(hours=1.0):
        start = datetime.now(timezone.utc) + timedelta(minutes=30)
        return {
            "start_time":   start,
            "end_time":     start + timedelta(hours=hours),
            "duration_hrs": hours,
        }

    def test_sufficient_soc_sends_nothing(self):
        """A full battery must stay silent — an alert per event would be noise
        that trains the reader to ignore the one that matters."""
        p = self._p(soc_pct=95.0)
        p._start_vpp_precharge(self._event())
        p._send_pushover.assert_not_called()

    def test_shortfall_alerts(self):
        p = self._p(soc_pct=5.0)
        p._start_vpp_precharge(self._event())
        p._send_pushover.assert_called_once()

    def test_alert_is_priority_zero_so_quiet_hours_can_suppress_it(self):
        """Nothing can be done about it at 03:00 — pre-charge never imports by
        design — so this must never override quiet hours."""
        p = self._p(soc_pct=5.0)
        p._start_vpp_precharge(self._event())
        self.assertEqual(p._send_pushover.call_args.kwargs.get("priority"), "0")

    def test_body_carries_the_figures_and_the_window(self):
        p = self._p(soc_pct=5.0)
        p._start_vpp_precharge(self._event())
        body = p._send_pushover.call_args.args[1]
        self.assertIn("21:00-22:00", body)
        self.assertIn("5%", body)
        self.assertIn("short by", body)
        # It must say the window is still driven, or the reader assumes it isn't.
        self.assertIn("still be driven", body)

    def test_night_and_day_name_different_floors(self):
        """The floor decides where export stops, so the message must not
        describe a dawn reserve on a daytime window or the reverse."""
        day = self._p(soc_pct=5.0, daytime=True)
        day._start_vpp_precharge(self._event())
        self.assertIn("health floor", day._send_pushover.call_args.args[1])

        night = self._p(soc_pct=5.0, daytime=False)
        night._start_vpp_precharge(self._event())
        self.assertIn("dawn reserve", night._send_pushover.call_args.args[1])

    def test_a_failing_alert_never_stops_the_window_being_driven(self):
        """This is an advisory on the pre-charge path. A Pushover outage, or a
        bad pref inside the message build, must not cost us the export."""
        p = self._p(soc_pct=5.0)
        p._send_pushover.side_effect = RuntimeError("pushover down")
        p._start_vpp_precharge(self._event())
        p._vpp_transition.assert_called_once_with(plugin.VPP_PRE_CHARGING)

    def test_cutoff_is_still_set_when_short(self):
        """The hardware discharge floor is what actually stops the export early
        — it must be written whether or not the battery is short."""
        p = self._p(soc_pct=5.0)
        p._start_vpp_precharge(self._event())
        p._set_vpp_discharge_cutoff.assert_called_once()


class TestCostKwhVariables(unittest.TestCase):
    """v5.61.0 — the nine kWh variables are published from the SAME card as
    the costs beside them.

    They had no writer anywhere in the estate since the Octopus consumption
    script was retired (12-Apr-2026), so they sat frozen next to live money:
    export_today_kwh read 0.000 while export_today_revenue_gbp read GBP 2.12,
    i.e. 17.69 kWh at 12p. The pair is now sourced from one dict, so it cannot
    disagree again.
    """

    # ---- the cards carry the kWh they were costed from ----

    def test_build_card_returns_its_kwh(self):
        card = plugin.Plugin._wh_build_card(
            import_kwh=4.5, export_kwh=17.69, elec_unit_p=26.271,
            export_rate_p=12.0, elec_standing_p=61.518, gas_kwh=7.5,
            gas_unit_p=6.584, gas_standing_p=29.062,
            provisional=True, gas_estimated=False)
        self.assertEqual(card["import_kwh"], 4.5)
        self.assertEqual(card["export_kwh"], 17.69)
        self.assertEqual(card["gas_kwh"], 7.5)

    def test_build_card_kwh_and_money_agree(self):
        """The complaint, stated as an assertion: kWh x rate == the revenue."""
        card = plugin.Plugin._wh_build_card(
            import_kwh=0.0, export_kwh=17.69, elec_unit_p=26.271,
            export_rate_p=12.0, elec_standing_p=0.0, gas_kwh=0.0,
            gas_unit_p=0.0, gas_standing_p=0.0,
            provisional=True, gas_estimated=False)
        self.assertAlmostEqual(
            card["export_kwh"] * 12.0 / 100.0, card["export_gbp"], places=2)

    def test_settled_card_uses_the_kwh_that_was_priced(self):
        """import_kwh_octo is what the settle step billed — NOT grid_import_kwh,
        which is the Sigen CT figure and differs."""
        rec = {
            "cost_settled": True,
            "elec_unit_cost_gbp": 1.18, "elec_standing_gbp": 0.62,
            "gas_unit_cost_gbp": 0.45, "gas_standing_gbp": 0.29,
            "whole_house_bill_gbp": 2.54, "export_revenue_gbp": 2.12,
            "wh_net_gbp": -0.42, "covered": False,
            "import_kwh_octo": 4.49, "grid_import_kwh": 4.61,
            "grid_export_kwh": 17.69, "gas_kwh": 6.777,
        }
        card = plugin.Plugin._wh_card_from_row(rec)
        self.assertEqual(card["import_kwh"], 4.49)
        self.assertNotEqual(card["import_kwh"], 4.61)
        self.assertEqual(card["export_kwh"], 17.69)
        self.assertEqual(card["gas_kwh"], 6.777)

    def test_unsettled_row_returns_no_card(self):
        self.assertIsNone(plugin.Plugin._wh_card_from_row({"cost_settled": False}))

    # ---- the writer publishes them ----

    def _run_writer(self, econ):
        p = plugin.Plugin.__new__(plugin.Plugin)
        p.octopus = None
        p.logger = MagicMock()
        p.store = {}
        p._cost_vars_economics = lambda: econ
        p._ensure_var = lambda name, folder_id: 1
        written = {}
        real_variable = getattr(plugin.indigo, "variable", None)
        plugin.indigo.variable = MagicMock()
        plugin.indigo.variable.updateValue.side_effect = (
            lambda vid, value: None)

        # Capture by wrapping _ensure_var instead: the name is what matters.
        names = []

        def _ensure(name, folder_id):
            names.append(name)
            return len(names)
        p._ensure_var = _ensure
        try:
            p._write_cost_variables(folder_id=1)
        finally:
            if real_variable is None:
                delattr(plugin.indigo, "variable")
            else:
                plugin.indigo.variable = real_variable
        # Rebuild name->value from the update calls in order.
        for i, name in enumerate(names):
            written[name] = None
        return names, written

    def test_writer_publishes_all_nine_kwh_variables(self):
        econ = {
            "whole_house": {
                "today":     {"import_kwh": 0.65, "export_kwh": 17.69,
                              "gas_kwh": 23.38, "electric_gbp": 0.79,
                              "gas_gbp": 1.83, "export_gbp": 2.12,
                              "bill_gbp": 2.62},
                "yesterday": {"import_kwh": 0.75, "export_kwh": 16.38,
                              "gas_kwh": 46.40},
            },
            "periods": {"month": {"days": 11, "import_kwh": 16.49,
                                  "export_kwh": 192.14, "gas_kwh": 420.88,
                                  "export_total_gbp": 23.06,
                                  "elec_whole_house_total_gbp": 12.10}},
        }
        names, _ = self._run_writer(econ)
        for expected in ("elec_today_kwh", "gas_today_kwh", "export_today_kwh",
                         "elec_yesterday_kwh", "gas_yesterday_kwh",
                         "export_yesterday_kwh", "elec_month_kwh",
                         "gas_month_kwh", "export_month_kwh"):
            self.assertIn(expected, names, f"{expected} was not published")

    def test_writer_skips_unknown_kwh_rather_than_writing_zero(self):
        """A missing figure must leave the variable ALONE. Writing 0.000 would
        state a measurement we do not have — the failure direction that makes a
        dead sensor look healthy."""
        econ = {
            "whole_house": {"today": {"export_gbp": 2.12}, "yesterday": None},
            "periods": {"month": {"days": 11}},
        }
        names, _ = self._run_writer(econ)
        self.assertNotIn("export_today_kwh", names)
        self.assertNotIn("export_yesterday_kwh", names)
        self.assertNotIn("export_month_kwh", names)

    def test_writer_survives_missing_whole_house_block(self):
        names, _ = self._run_writer({"whole_house": None, "periods": {}})
        self.assertNotIn("export_today_kwh", names)


class TestPeriodKwhTotals(unittest.TestCase):
    """The window's kWh must cover exactly the days its money covers."""

    def _summary(self, records):
        """_period_economics_summary reads daily_history.json out of
        self.data_dir, so the fixture is a real file in a temp dir — stubbing a
        loader that does not exist would have silently tested nothing."""
        import json
        import tempfile
        p = plugin.Plugin.__new__(plugin.Plugin)
        p.logger = MagicMock()
        p.store = {}
        p._row_standing_p = lambda rec, fallback: 61.518
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "daily_history.json"), "w",
                      encoding="utf-8") as f:
                json.dump(records, f)
            p.data_dir = d
            return p._period_economics_summary(
                export_rate_p=12.0, fallback_import_rate_p=None)

    def test_rate_less_day_contributes_neither_money_nor_kwh(self):
        today = datetime.now().date()
        day = today.isoformat()
        recs = [
            {"date": day, "home_kwh": 10.0, "grid_import_kwh": 2.0,
             "grid_export_kwh": 5.0, "gas_kwh": 3.0, "rate_today_p": 26.271},
            # No rate at all — skipped for cost, so must be skipped for kWh.
            {"date": day, "home_kwh": 99.0, "grid_import_kwh": 99.0,
             "grid_export_kwh": 99.0, "gas_kwh": 99.0},
        ]
        out = self._summary(recs)
        month = out.get("month") or {}
        self.assertIn("import_kwh", month)
        self.assertEqual(month["days"], 1)
        self.assertAlmostEqual(month["import_kwh"], 2.0, places=3)
        self.assertAlmostEqual(month["export_kwh"], 5.0, places=3)
        self.assertAlmostEqual(month["gas_kwh"], 3.0, places=3)


class TestVppEndsOnStoredWindow(unittest.TestCase):
    """v5.61.1 — the ACTIVE branch must judge the END against OUR STORED window,
    never the event the Axle API just returned.

    Axle publish the NEXT event within a minute of one finishing. Reading that
    event's end_time made the stop test `now >= <tomorrow> + 2min`, false for a
    further ~21 hours, so the plugin kept self-driving a 4 kW export from the
    battery with a 1% discharge floor and nothing to halt it. Live-hit
    11-Aug-2026: tonight's 19:30-20:30 window was still exporting at 21:15.
    """

    @staticmethod
    def _apply_at(p, event, now):
        """Drive _apply_vpp_event with the clock frozen at `now`.

        It takes no `now` argument — it calls datetime.now(timezone.utc) itself —
        so a subclass is patched in rather than a MagicMock, which would break the
        constructor and every other datetime use inside the call."""
        class _Frozen(datetime):
            @classmethod
            def now(cls, tz=None):
                return now
        real = plugin.datetime
        plugin.datetime = _Frozen
        try:
            p._apply_vpp_event(event)
        finally:
            plugin.datetime = real

    def _plugin(self, stored_event, now=None):
        p = plugin.Plugin.__new__(plugin.Plugin)
        p.logger = MagicMock()
        p.modbus = MagicMock()
        p.latest_inverter_data = {"batterySoc": 80.0}
        p.pluginPrefs = {}
        p.store = {
            "vpp_state": plugin.VPP_ACTIVE,
            "vpp_event": stored_event,
            "vpp_active": True,
            "export_active": True,
            "grid_export_daily_kwh": 40.0,
            "vpp_export_start_kwh": 30.0,
        }
        p._log_vpp_snapshot = MagicMock()
        p._update_vpp_device = MagicMock()
        p._end_vpp_export = MagicMock()
        p._trigger_event = MagicMock()
        p._write_vpp_event_header = MagicMock()
        p._vpp_transition = MagicMock()
        p._restore_discharge_cutoff = MagicMock()
        p._set_vpp_discharge_cutoff = MagicMock()
        p._event_is_daytime = MagicMock(return_value=True)
        return p

    @staticmethod
    def _event(start, end):
        return {"start_time": start, "end_time": end,
                "import_export": "export", "duration_hrs": 1.0}

    def test_future_event_returned_does_not_extend_the_active_window(self):
        now      = datetime(2026, 8, 11, 19, 40, tzinfo=timezone.utc)   # 20:40 BST
        tonight  = self._event(datetime(2026, 8, 11, 18, 30, tzinfo=timezone.utc),
                               datetime(2026, 8, 11, 19, 30, tzinfo=timezone.utc))
        tomorrow = self._event(datetime(2026, 8, 12, 17, 0, tzinfo=timezone.utc),
                               datetime(2026, 8, 12, 18, 0, tzinfo=timezone.utc))
        p = self._plugin(tonight, now)
        self._apply_at(p, tomorrow, now)
        p._end_vpp_export.assert_called_once()
        # And the snapshot must land in TONIGHT's file, not the next event's.
        self.assertIs(p._log_vpp_snapshot.call_args[0][0], tonight)

    def test_window_still_running_is_not_ended_early(self):
        now     = datetime(2026, 8, 11, 19, 0, tzinfo=timezone.utc)     # mid-window
        tonight = self._event(datetime(2026, 8, 11, 18, 30, tzinfo=timezone.utc),
                              datetime(2026, 8, 11, 19, 30, tzinfo=timezone.utc))
        p = self._plugin(tonight, now)
        self._apply_at(p, tonight, now)
        p._end_vpp_export.assert_not_called()

    def test_two_minute_tail_is_still_honoured(self):
        tonight = self._event(datetime(2026, 8, 11, 18, 30, tzinfo=timezone.utc),
                              datetime(2026, 8, 11, 19, 30, tzinfo=timezone.utc))
        # 19:31 — inside the tail, must keep going.
        p = self._plugin(tonight, None)
        self._apply_at(p, tonight, datetime(2026, 8, 11, 19, 31, tzinfo=timezone.utc))
        p._end_vpp_export.assert_not_called()
        # 19:33 — past the tail, must stop.
        p = self._plugin(tonight, None)
        self._apply_at(p, tonight, datetime(2026, 8, 11, 19, 33, tzinfo=timezone.utc))
        p._end_vpp_export.assert_called_once()

    def test_no_stored_window_falls_back_to_the_returned_event(self):
        """Defensive: an ACTIVE state with nothing stored must not crash, and must
        still be able to stop."""
        now     = datetime(2026, 8, 11, 19, 40, tzinfo=timezone.utc)
        tonight = self._event(datetime(2026, 8, 11, 18, 30, tzinfo=timezone.utc),
                              datetime(2026, 8, 11, 19, 30, tzinfo=timezone.utc))
        p = self._plugin(tonight, now)
        p.store["vpp_event"] = None
        self._apply_at(p, tonight, now)
        p._end_vpp_export.assert_called_once()


class TestVppOverrunBackstop(unittest.TestCase):
    """v5.62.0 — the SECOND, independent guard on an over-running VPP export.

    v5.61.1 fixed the cause that fired on 11-Aug-2026, but the end-of-window still
    depended on ONE path (_poll_vpp -> _apply_vpp_event). The manager re-drives
    ACTION_VPP_EXPORT from the `vpp_active` boolean and never looks at the clock,
    so anything that stops that poll — axleEnabled unticked, token cleared, a
    raise before the ACTIVE branch, the VPP tick task dying — leaves 4 kW pouring
    out of the battery against a 1% floor for ever.
    """

    END = datetime(2026, 8, 11, 19, 30, tzinfo=timezone.utc)

    def _plugin(self, state=None, event=None):
        p = plugin.Plugin.__new__(plugin.Plugin)
        p.logger = MagicMock()
        p.store = {
            "vpp_state": plugin.VPP_ACTIVE if state is None else state,
            "vpp_event": {"start_time": self.END - timedelta(hours=1),
                          "end_time": self.END} if event is None else event,
        }
        p._end_vpp_export = MagicMock()
        return p

    @staticmethod
    def _run_at(p, now):
        class _Frozen(datetime):
            @classmethod
            def now(cls, tz=None):
                return now
        real = plugin.datetime
        plugin.datetime = _Frozen
        try:
            p._check_vpp_overrun()
        finally:
            plugin.datetime = real

    def test_ends_a_window_running_long_past_its_end(self):
        p = self._plugin()
        self._run_at(p, self.END + timedelta(minutes=16))
        p._end_vpp_export.assert_called_once()

    def test_tonights_actual_overshoot_would_have_been_caught(self):
        """11-Aug: the export was still running 45 min past the end."""
        p = self._plugin()
        self._run_at(p, self.END + timedelta(minutes=45))
        p._end_vpp_export.assert_called_once()

    def test_never_truncates_a_live_window(self):
        p = self._plugin()
        self._run_at(p, self.END - timedelta(minutes=10))
        p._end_vpp_export.assert_not_called()

    def test_leaves_the_primary_path_room_to_work(self):
        """The poll stops at end+2min; the backstop must not race it."""
        p = self._plugin()
        self._run_at(p, self.END + timedelta(minutes=3))
        p._end_vpp_export.assert_not_called()

    def test_inactive_state_is_untouched(self):
        p = self._plugin(state=plugin.VPP_IDLE)
        self._run_at(p, self.END + timedelta(hours=5))
        p._end_vpp_export.assert_not_called()

    def test_missing_end_time_does_nothing_rather_than_guessing(self):
        p = self._plugin(event={"start_time": self.END})
        self._run_at(p, self.END + timedelta(hours=5))
        p._end_vpp_export.assert_not_called()

    def test_no_stored_event_does_nothing(self):
        p = self._plugin(event={})
        self._run_at(p, self.END + timedelta(hours=5))
        p._end_vpp_export.assert_not_called()

    def test_a_failure_inside_the_backstop_cannot_break_the_evaluate(self):
        p = self._plugin()
        p._end_vpp_export.side_effect = RuntimeError("modbus down")
        self._run_at(p, self.END + timedelta(minutes=30))   # must not raise


class TestPluginClassStructure(unittest.TestCase):
    """A syntax check is NOT a structure check.

    While adding the v5.63.0 filter a module-level helper was inserted INSIDE the
    class body. `py_compile` passed — the file is valid Python — but the unindented
    `def` TERMINATED the class, so every method below it became a nested function
    and `Plugin` silently lost them. Nothing would have failed until the plugin
    tried to call one at runtime. This pins the shape, not just the syntax.
    """

    CORE_METHODS = (
        "startup", "shutdown", "runConcurrentThread",
        "_evaluate_manager_impl", "_poll_vpp", "_apply_vpp_event",
        "_end_vpp_export", "_check_vpp_overrun", "_summarise_vpp_event",
        "_write_cost_variables", "_verify_ems_registers",
    )

    def _plugin_class(self):
        import ast
        import os
        path = os.path.join(os.path.dirname(os.path.abspath(plugin.__file__)), "plugin.py")
        tree = ast.parse(open(path, encoding="utf-8").read())
        return tree, next(n for n in tree.body
                          if isinstance(n, ast.ClassDef) and n.name == "Plugin")

    def test_core_methods_are_really_on_the_class(self):
        import ast
        _tree, cls = self._plugin_class()
        methods = {n.name for n in cls.body if isinstance(n, ast.FunctionDef)}
        missing = [m for m in self.CORE_METHODS if m not in methods]
        self.assertEqual(missing, [], f"not methods of Plugin: {missing}")

    def test_the_class_still_holds_the_bulk_of_the_code(self):
        """A dedent mid-class silently moves everything below it out. If this
        number collapses, that is what happened."""
        import ast
        _tree, cls = self._plugin_class()
        methods = [n for n in cls.body if isinstance(n, ast.FunctionDef)]
        self.assertGreater(len(methods), 120,
                           f"Plugin has only {len(methods)} methods — class body truncated?")

    def test_module_helpers_are_not_nested_inside_the_class(self):
        import ast
        tree, _cls = self._plugin_class()
        mod = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
        self.assertIn("_snapshot_in_window", mod)


class TestSnapshotWindowFilter(unittest.TestCase):
    """v5.63.0 — one window's readings must never be summarised into another's."""

    EV = {"duration_hrs": 1.0}

    def test_in_window_snapshot_kept(self):
        self.assertTrue(plugin._snapshot_in_window({"event_elapsed_secs": 1800.0}, self.EV))

    def test_lead_in_and_trail_kept(self):
        self.assertTrue(plugin._snapshot_in_window({"event_elapsed_secs": -0.66}, self.EV))
        self.assertTrue(plugin._snapshot_in_window({"event_elapsed_secs": 3720.0}, self.EV))

    def test_the_real_11_aug_pollution_is_rejected(self):
        """31 snapshots landed in the next event's file at elapsed -1288 min."""
        self.assertFalse(plugin._snapshot_in_window({"event_elapsed_secs": -1288.6 * 60}, self.EV))

    def test_two_hour_window_keeps_its_second_hour(self):
        """Tomorrow's event is 2 h — a 1 h assumption would drop half of it."""
        self.assertTrue(plugin._snapshot_in_window({"event_elapsed_secs": 7000.0},
                                                   {"duration_hrs": 2.0}))
        self.assertFalse(plugin._snapshot_in_window({"event_elapsed_secs": 7000.0},
                                                    {"duration_hrs": 1.0}))

    def test_unknown_elapsed_or_duration_is_kept_not_dropped(self):
        self.assertTrue(plugin._snapshot_in_window({}, self.EV))
        self.assertTrue(plugin._snapshot_in_window({"event_elapsed_secs": "x"}, self.EV))
        self.assertTrue(plugin._snapshot_in_window({"event_elapsed_secs": 99999.0}, {}))


class TestVppHandbackConfirmation(unittest.TestCase):
    """v5.64.0 — the hand-back at window end must be CONFIRMED, not assumed.

    Until v5.64 set_self_consumption()'s return value was discarded, so a
    rejected / clamped / socket-dead write left the inverter selling the battery
    while the state machine reported IDLE, and only the ~15-minute manager cycle
    put it right. Predbat hit the same class of bug on the cloud path (#4477).
    """

    def _p(self, connected=True, results=(True,)):
        p = plugin.Plugin.__new__(plugin.Plugin)
        p.logger = MagicMock()
        p.debug  = False
        p.modbus = MagicMock()
        p.modbus.connected = connected
        p.modbus.set_self_consumption.side_effect = list(results)
        p.store = {
            "vpp_state":              plugin.VPP_ACTIVE,
            "export_active":          True,
            "import_active":          False,
            "grid_export_daily_kwh":  10.0,
            "vpp_export_start_kwh":   6.0,
            "vpp_handback_pending":   False,
        }
        p._restore_discharge_cutoff = MagicMock()
        p._vpp_transition           = MagicMock()
        p._trigger_event            = MagicMock()
        p._vpp_event_log_path       = MagicMock(return_value="/dev/null")
        return p

    # ---- _end_vpp_export -------------------------------------------------

    def test_confirmed_handback_leaves_no_pending_flag(self):
        p = self._p(results=(True,))
        p._end_vpp_export(datetime.now(timezone.utc), {})
        self.assertFalse(p.store["vpp_handback_pending"])
        self.assertEqual(p.modbus.set_self_consumption.call_count, 1)

    def test_failed_write_is_retried_once_immediately(self):
        p = self._p(results=(False, True))
        p._end_vpp_export(datetime.now(timezone.utc), {})
        self.assertEqual(p.modbus.set_self_consumption.call_count, 2)
        self.assertFalse(p.store["vpp_handback_pending"])

    def test_both_attempts_failing_raises_the_flag_and_warns(self):
        p = self._p(results=(False, False))
        p._end_vpp_export(datetime.now(timezone.utc), {})
        self.assertTrue(p.store["vpp_handback_pending"])

    def test_disconnected_modbus_does_not_claim_success(self):
        """The old code called through `if self.modbus:` with no connected check."""
        p = self._p(connected=False)
        p._end_vpp_export(datetime.now(timezone.utc), {})
        p.modbus.set_self_consumption.assert_not_called()
        self.assertTrue(p.store["vpp_handback_pending"])

    def test_state_machine_still_reaches_idle_when_handback_fails(self):
        """It must never wedge in ACTIVE — terminate, then keep re-asserting."""
        p = self._p(results=(False, False))
        p._end_vpp_export(datetime.now(timezone.utc), {})
        p._vpp_transition.assert_called_once_with(plugin.VPP_IDLE)
        self.assertFalse(p.store["export_active"])

    # ---- _retry_vpp_handback --------------------------------------------

    def _r(self, pending=True, state=None, connected=True, ok=True,
           export=False, imp=False):
        p = plugin.Plugin.__new__(plugin.Plugin)
        p.logger = MagicMock()
        p.modbus = MagicMock()
        p.modbus.connected = connected
        p.modbus.set_self_consumption.return_value = ok
        p.store = {
            "vpp_handback_pending": pending,
            "vpp_state":            state or plugin.VPP_IDLE,
            "export_active":        export,
            "import_active":        imp,
        }
        return p

    def test_retry_is_a_no_op_when_nothing_pending(self):
        p = self._r(pending=False)
        p._retry_vpp_handback()
        p.modbus.set_self_consumption.assert_not_called()

    def test_retry_clears_flag_on_success(self):
        p = self._r()
        p._retry_vpp_handback()
        p.modbus.set_self_consumption.assert_called_once()
        self.assertFalse(p.store["vpp_handback_pending"])

    def test_retry_keeps_trying_while_it_fails(self):
        p = self._r(ok=False)
        p._retry_vpp_handback()
        self.assertTrue(p.store["vpp_handback_pending"])

    def test_retry_never_writes_during_a_live_window(self):
        """Re-asserting 0x02 mid-event would kill a paid export."""
        p = self._r(state=plugin.VPP_ACTIVE)
        p._retry_vpp_handback()
        p.modbus.set_self_consumption.assert_not_called()
        self.assertFalse(p.store["vpp_handback_pending"])

    def test_retry_stands_down_if_an_export_or_import_is_engaged(self):
        for kwargs in ({"export": True}, {"imp": True}):
            p = self._r(**kwargs)
            p._retry_vpp_handback()
            p.modbus.set_self_consumption.assert_not_called()
            self.assertFalse(p.store["vpp_handback_pending"])

    def test_retry_waits_quietly_while_the_socket_is_down(self):
        p = self._r(connected=False)
        p._retry_vpp_handback()
        p.modbus.set_self_consumption.assert_not_called()
        self.assertTrue(p.store["vpp_handback_pending"])   # still owed

    def test_engaging_a_window_cancels_a_pending_retry(self):
        p = plugin.Plugin.__new__(plugin.Plugin)
        p.logger = MagicMock()
        p.debug  = False
        p.store  = {"vpp_state": plugin.VPP_IDLE, "vpp_handback_pending": True}
        p._vpp_transition(plugin.VPP_PRE_CHARGING)
        self.assertFalse(p.store["vpp_handback_pending"])

class TestNoTestsStrandedBelowMain(unittest.TestCase):
    """Every test class must sit ABOVE the `if __name__ == "__main__"` block.

    `unittest.main()` calls sys.exit(), so a class defined BELOW that block is
    never reached on a direct `python3 test_x.py` run — it is collected only by
    an importing runner. The two disagreed silently: test_plugin.py ran 223
    tests directly against 263 imported, and test_openmeteo_forecast.py 36
    against 41, so 45 tests — including every VPP guard added in v5.61.1
    through v5.64.0 — were invisible to anyone following the repo's own
    documented "run it directly" instructions. CI used discover() and was fine,
    which is exactly why nobody noticed.

    Structural, not behavioural: it asserts the shape that keeps the other
    tests reachable, so it has to be a test rather than a comment.
    """

    def test_no_class_defined_after_the_main_block(self):
        import ast
        import glob
        import os
        here = os.path.dirname(os.path.abspath(__file__))
        offenders = []
        for path in sorted(glob.glob(os.path.join(here, "test_*.py"))):
            tree = ast.parse(open(path, encoding="utf-8").read())
            main_line = None
            for node in tree.body:
                if (isinstance(node, ast.If)
                        and isinstance(node.test, ast.Compare)
                        and isinstance(node.test.left, ast.Name)
                        and node.test.left.id == "__name__"):
                    main_line = node.lineno
            if main_line is None:
                continue
            for node in tree.body:
                if isinstance(node, ast.ClassDef) and node.lineno > main_line:
                    offenders.append(
                        f"{os.path.basename(path)}:{node.lineno} {node.name}")
        self.assertEqual(
            offenders, [],
            "test class(es) defined below the __main__ block are unreachable on "
            "a direct run — move the __main__ block to the end of the file: "
            + ", ".join(offenders))


class TestScheduledImportRetraction(unittest.TestCase):
    """v5.65.0 — a queued grid import must not outlive the decision that queued it.

    ACTION_SCHEDULE_IMPORT stored a time that NOTHING cleared but the firing
    itself. When a later evaluate stopped wanting the import the stored time
    survived, fired with no fresh check, and the anti-oscillation guard in the
    SELF_CONSUMPTION branch then HELD the unwanted import until the stored target
    SOC was reached. On Tracker the midnight-deferral path arms this ~8 hours
    ahead — a long time for the forecast to improve. Cost per occurrence: 10 kW
    of grid import nobody wanted, against the whole point of the plugin.

    Two lenses of the review found this independently, one via a changed
    decision and one via pause.
    """

    def _p(self, action, scheduled=True, import_active=False, paused=False):
        p = plugin.Plugin.__new__(plugin.Plugin)
        p.logger      = MagicMock()
        p.debug       = False
        p.modbus      = MagicMock()
        p.modbus.connected = True
        p.pluginPrefs = {"inverterMaxKw": "10.0"}
        # The SELF_CONSUMPTION branch reads the last inverter poll to spot a stuck
        # mode; an empty dict is the honest "nothing polled yet" state.
        p.latest_inverter_data = {}
        p._restore_import_cutoff = MagicMock()
        p._set_import_cutoff     = MagicMock()
        p._trigger_event         = MagicMock()
        p.store = {
            "import_active":         import_active,
            "export_active":         False,
            "manager_paused":        paused,
            "import_scheduled_time": (datetime(2026, 8, 13, 0, 5, tzinfo=timezone.utc)
                                      if scheduled else None),
            "import_scheduled_logged": True,
            "import_target_soc":     12.0,
            "vpp_active":            False,
        }
        dec = MagicMock()
        dec.action = action
        return p, dec

    def test_a_different_decision_retracts_the_schedule(self):
        p, dec = self._p(plugin.ACTION_SELF_CONSUMPTION)

        p._act_on_decision(dec)

        self.assertIsNone(p.store["import_scheduled_time"],
            "a decision other than SCHEDULE_IMPORT means the manager changed its "
            "mind — the queued import must be retracted")

    def test_the_schedule_survives_while_still_wanted(self):
        """SCHEDULE_IMPORT is re-emitted every tick while the import is wanted."""
        p, dec = self._p(plugin.ACTION_SCHEDULE_IMPORT)
        dec.scheduled_time = datetime(2026, 8, 13, 0, 5, tzinfo=timezone.utc)

        p._act_on_decision(dec)

        self.assertIsNotNone(p.store["import_scheduled_time"])

    def test_a_running_import_is_not_retracted_here(self):
        """Stopping an in-flight import is STOP_IMPORT's job, not this guard's."""
        p, dec = self._p(plugin.ACTION_SELF_CONSUMPTION, import_active=True)

        p._act_on_decision(dec)

        self.assertIsNotNone(p.store["import_scheduled_time"],
            "must not clear bookkeeping out from under a running import")


class TestScheduledImportSafety(unittest.TestCase):
    """v5.65.0 — the firing path's own guards."""

    def _p(self, target_soc, paused=False, due=True):
        p = plugin.Plugin.__new__(plugin.Plugin)
        p.logger      = MagicMock()
        p.debug       = False
        p.modbus      = MagicMock()
        p.modbus.force_charge.return_value = True
        p.pluginPrefs = {"inverterMaxKw": "10.0"}
        when = datetime.now(timezone.utc) - timedelta(minutes=1 if due else -60)
        p.store = {
            "import_scheduled_time": when,
            "import_target_soc":     target_soc,
            "manager_paused":        paused,
            "import_active":         False,
            "had_import_today":      False,
        }
        p._set_import_cutoff = MagicMock()
        p._trigger_event     = MagicMock()
        return p

    def test_zero_target_does_not_become_a_100_percent_cutoff(self):
        """`(target or 100.0)` turned 0.0 into 'charge to full'.

        0.0 is exactly what an intervening completed import leaves behind, so the
        backstop meant to STOP a runaway import became permission for one.
        """
        p = self._p(target_soc=0.0)

        p._check_scheduled_import_impl()

        self.assertTrue(p.modbus.force_charge.called)
        cutoff = p.modbus.force_charge.call_args.kwargs["cutoff_soc"]
        self.assertLess(cutoff, 100.0,
            "a 0.0 target must fall back to the conservative default, not 100%")
        self.assertAlmostEqual(cutoff, 15.0, places=3)

    def test_a_paused_manager_does_not_fire_the_import(self):
        p = self._p(target_soc=12.0, paused=True)

        p._check_scheduled_import_impl()

        self.assertFalse(p.modbus.force_charge.called,
            "the schedule check runs outside the paused gate — it must refuse "
            "to drive the inverter while the manager is paused")
        self.assertIsNotNone(p.store["import_scheduled_time"],
            "held, not cancelled — resuming should keep the queued import")

    def test_power_follows_the_configured_inverter_rating(self):
        p = self._p(target_soc=12.0)
        p.pluginPrefs = {"inverterMaxKw": "15.0"}

        p._check_scheduled_import_impl()

        self.assertEqual(p.modbus.force_charge.call_args.args[0], 15000,
            "hardcoded 10000 W was right only on a 10 kW inverter")


class TestScheduledImportVppGate(unittest.TestCase):
    """v5.71.1 — a queued import must not fire inside a paid export window.

    `_check_scheduled_import_impl` runs from the 10s TICK and gated only on
    `manager_paused`, so it fired on the clock alone. The manager's own VPP
    override (which returns ACTION_VPP_EXPORT and retracts the queue) only
    re-evaluates on the ~15-minute cycle, and that retraction is skipped once an
    import is already running — so a schedule armed for a time inside a window
    started Charge Grid First at up to inverterMaxKw mid-export, buying at the
    import rate through the hour we are paid a premium to sell, with
    _verify_ems_registers deliberately standing down for the duration.

    Low odds on Tracker (no cheap window, so the deferral path rarely arms) but
    routine on a time-of-use tariff, which is where this house is heading.
    """

    def _p(self, vpp_state, due=True):
        p = plugin.Plugin.__new__(plugin.Plugin)
        p.logger      = MagicMock()
        p.debug       = False
        p.modbus      = MagicMock()
        p.modbus.force_charge.return_value = True
        p.pluginPrefs = {"inverterMaxKw": "10.0"}
        when = datetime.now(timezone.utc) - timedelta(minutes=1 if due else -60)
        p.store = {
            "import_scheduled_time": when,
            "import_target_soc":     12.0,
            "manager_paused":        False,
            "vpp_state":             vpp_state,
            "import_active":         False,
            "had_import_today":      False,
        }
        p._set_import_cutoff = MagicMock()
        p._trigger_event     = MagicMock()
        return p

    def test_an_active_window_holds_the_queued_import(self):
        p = self._p(plugin.VPP_ACTIVE)

        p._check_scheduled_import_impl()

        self.assertFalse(p.modbus.force_charge.called,
            "a grid charge inside a paid export window forfeits the premium")
        self.assertIsNotNone(p.store["import_scheduled_time"],
            "held, not cancelled — the battery still wants that charge")

    def test_pre_charging_holds_it_too(self):
        """The plugin is already grid-charging to its own target in PRE_CHARGING.

        A second charge command with its own cutoff would fight it.
        """
        p = self._p(plugin.VPP_PRE_CHARGING)

        p._check_scheduled_import_impl()

        self.assertFalse(p.modbus.force_charge.called)
        self.assertIsNotNone(p.store["import_scheduled_time"])

    def test_announced_does_NOT_hold_it(self):
        """An announcement can be hours ahead.

        Charging BEFORE a window is the arbitrage this plugin exists to do, so
        blocking it there would be a worse fault than the one being fixed.
        """
        p = self._p(plugin.VPP_ANNOUNCED)

        p._check_scheduled_import_impl()

        self.assertTrue(p.modbus.force_charge.called,
            "an announced window is not a running one")

    def test_idle_still_fires_normally(self):
        """The regression guard — the common case must be untouched."""
        p = self._p(plugin.VPP_IDLE)

        p._check_scheduled_import_impl()

        self.assertTrue(p.modbus.force_charge.called)
        self.assertIsNone(p.store["import_scheduled_time"],
            "a fired schedule clears itself")

    def test_a_held_schedule_fires_once_the_window_closes(self):
        """Held means deferred, not lost — otherwise the gate is an exclusion."""
        p = self._p(plugin.VPP_ACTIVE)
        p._check_scheduled_import_impl()
        self.assertFalse(p.modbus.force_charge.called)

        p.store["vpp_state"] = plugin.VPP_IDLE
        p._check_scheduled_import_impl()

        self.assertTrue(p.modbus.force_charge.called,
            "the queued import must survive the window and fire afterwards")

    def test_the_hold_is_logged_once_per_window_not_once_per_tick(self):
        """The tick is 10s. One line per window, or it drowns the log."""
        p = self._p(plugin.VPP_ACTIVE)
        with patch.object(plugin, "log") as mock_log:
            for _ in range(5):
                p._check_scheduled_import_impl()
            held = [c for c in mock_log.call_args_list
                    if "Scheduled import held" in str(c)]
        self.assertEqual(len(held), 1, f"expected one hold line, got {len(held)}")

    def test_an_absent_vpp_state_is_treated_as_idle(self):
        """A store without the key must not become a permanent block."""
        p = self._p(plugin.VPP_IDLE)
        del p.store["vpp_state"]

        p._check_scheduled_import_impl()

        self.assertTrue(p.modbus.force_charge.called)


class TestForceGridImportVppGate(unittest.TestCase):
    """v5.71.1 — the manual action carries the same fault, plus one of its own.

    It sets export_active False beneath a state machine that is still driving the
    export, so the store would disagree with the inverter. Refused rather than
    silently held: there is a person behind this one and they deserve an answer.
    """

    def _p(self, vpp_state):
        p = plugin.Plugin.__new__(plugin.Plugin)
        p.logger      = MagicMock()
        p.debug       = False
        p._state_lock = threading.RLock()
        p.modbus      = MagicMock()
        p.modbus.force_charge.return_value = True
        p.pluginPrefs = {"inverterMaxKw": "10.0"}
        p.store = {"vpp_state": vpp_state, "import_active": False,
                   "export_active": True}
        p._set_import_cutoff = MagicMock()
        return p

    @staticmethod
    def _action():
        a = MagicMock()
        a.props = {"powerKw": "5.0", "targetSocPct": "80"}
        return a

    def test_refused_during_an_active_window(self):
        p = self._p(plugin.VPP_ACTIVE)

        p.actionForceGridImport(self._action())

        self.assertFalse(p.modbus.force_charge.called)
        self.assertTrue(p.store["export_active"],
            "the refusal must leave the running export's state alone")

    def test_refused_during_pre_charging(self):
        p = self._p(plugin.VPP_PRE_CHARGING)

        p.actionForceGridImport(self._action())

        self.assertFalse(p.modbus.force_charge.called)

    def test_allowed_when_idle(self):
        """Both-sides guard — a refusal that refuses everything is not a fix."""
        p = self._p(plugin.VPP_IDLE)

        p.actionForceGridImport(self._action())

        self.assertTrue(p.modbus.force_charge.called)
        self.assertEqual(p.modbus.force_charge.call_args.args[0], 5000)


class TestVppPvVerdict(unittest.TestCase):
    """v5.64.0 — the PV verdict reads the window's PEAK, not its minimum.

    v5.56.0 fixed the fully-dark window ("n/a") but left the window that SPANS
    DUSK. The first two-hour event (12-Aug-2026, 18:00-20:00 BST) hit it: PV fell
    naturally 1757 W -> 0 as the sun set, the minimum was 0, and a textbook window
    was reported as "curtailed". Curtailment was not just absent but IMPOSSIBLE —
    in mode 0x05 with charge pinned at 0, PV can only be curtailed above
    house + export cap (~4.99 kW), and it peaked at 1.76 kW.
    """

    @staticmethod
    def _verdict(daytime, pv_watts):
        """The shipped rule, in isolation."""
        max_pv_w = int(max(pv_watts)) if pv_watts else 0
        if not daytime:
            return "n/a (dark window)"
        if pv_watts and max_pv_w > 100:
            return "ran"
        return "curtailed"

    def test_tonights_real_series_reads_ran_not_curtailed(self):
        """PV declining to zero at dusk is sunset, not curtailment."""
        series = [1476, 1459, 1200, 900, 600, 300, 120, 0, 0, 0, 0]
        self.assertEqual(self._verdict(True, series), "ran")

    def test_the_15_jun_failure_is_still_caught(self):
        """Mode 0x06 shut the MPPT off for the whole window — zero throughout."""
        self.assertEqual(self._verdict(True, [0] * 46), "curtailed")

    def test_dark_window_is_still_not_an_alarm(self):
        self.assertEqual(self._verdict(False, [0] * 45), "n/a (dark window)")

    def test_full_sun_throughout_reads_ran(self):
        self.assertEqual(self._verdict(True, [6000] * 40), "ran")

    def test_minimum_based_rule_would_have_failed_this(self):
        """Pins WHY the rule changed: the old test called tonight curtailed."""
        series = [1476, 1459, 1200, 900, 600, 300, 120, 0, 0, 0, 0]
        old_verdict = "ran" if (series and int(min(series)) > 100) else "curtailed"
        self.assertEqual(old_verdict, "curtailed")          # the bug
        self.assertEqual(self._verdict(True, series), "ran")  # the fix

    def test_no_snapshots_is_not_reported_as_healthy(self):
        self.assertEqual(self._verdict(True, []), "curtailed")


class TestVppPvVerdictOnRealSummariser(unittest.TestCase):
    """Drive the SHIPPED _summarise_vpp_event, not a replica of its rule.

    TestVppPvVerdict above re-implements the decision, so it can only ever
    confirm the rule I wrote — it could not have caught the rule being wrong
    in the first place.  This drives the real method over a real JSONL file
    shaped like the 12-Aug-2026 event (PV declining to zero at dusk).
    """

    def _run(self, pv_series, daytime=True, forecast=None, event=None):
        import json as _json
        import tempfile
        from unittest.mock import MagicMock as _MM

        verdict = {}

        class _Rec:
            id = 1
            states = {}
            def updateStatesOnServer(_self, states):
                for st in states:
                    verdict[st["key"]] = st["value"]
            def updateStateOnServer(_self, k, v, **kw):
                verdict[k] = v

        p = plugin.Plugin.__new__(plugin.Plugin)
        p.logger = _MM()
        p.store = {"vpp_is_daytime": daytime}
        p.pluginPrefs = {}
        p._send_pushover = lambda *a, **k: None
        p._find_device = lambda type_id: _Rec()
        # Absent by default, which is the point: with no forecast the verdict
        # must fall back to the older, noisier rule rather than excusing a zero.
        if forecast is not None:
            p.latest_forecast_data = forecast

        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "event.jsonl")
            step = (2.0 * 3600.0) / max(len(pv_series) - 1, 1)
            with open(path, "w", encoding="utf-8") as fh:
                for i, w in enumerate(pv_series):
                    fh.write(_json.dumps({
                        # "type" is REQUIRED: the parser filters on it, and a
                        # fixture without it yields ZERO snapshots — which reads
                        # as "curtailed" and would have passed the control test
                        # for entirely the wrong reason.
                        "type": "snapshot",
                        "event_elapsed_secs": i * step,
                        "pv_w": w, "battery_w": -2000.0, "grid_w": -4000.0,
                        "ems_work_mode": "0x05", "driver": "self",
                    }) + "\n")
            p._vpp_event_log_path = lambda ev: path
            saved_log = plugin.log
            plugin.log = lambda *a, **k: None
            try:
                p._summarise_vpp_event(event or {"duration_hrs": 2.0})
            finally:
                plugin.log = saved_log
        return verdict

    def test_dusk_window_declining_to_zero_reads_ran(self):
        """The 12-Aug-2026 shape: 1757 W falling to 0 as the sun set."""
        v = self._run([1757, 1476, 1200, 900, 600, 300, 120, 40, 0, 0, 0])
        self.assertEqual(v.get("lastVppPvStatus"), "ran")
        self.assertEqual(v.get("lastVppMaxPvW"), 1757)
        self.assertEqual(v.get("lastVppMinPvW"), 0)
        self.assertIs(v.get("lastVppPvSurvived"), True)

    def test_pv_flat_at_zero_all_window_still_reads_curtailed(self):
        """The 15-Jun-2026 fault must still be caught — this is the control."""
        v = self._run([0] * 11)
        self.assertEqual(v.get("lastVppPvStatus"), "curtailed")
        self.assertEqual(v.get("lastVppMaxPvW"), 0)
        self.assertIs(v.get("lastVppPvSurvived"), False)

    def test_dark_window_is_not_an_alarm(self):
        v = self._run([0] * 11, daytime=False)
        self.assertEqual(v.get("lastVppPvStatus"), "n/a (dark window)")
        self.assertIs(v.get("lastVppPvSurvived"), True)


    # ---- v5.73.0: what was EXPECTED, not merely what arrived ----------
    #
    # The 16-Aug-2026 window (20:00-21:00 BST, sunset 20:40) read 0 W across
    # all 48 snapshots and was reported "curtailed". The forecast for that hour
    # was 153 W falling to zero — it had predicted the zero — and curtailment
    # needs PV above house + export cap, roughly 5 kW, at five degrees of
    # elevation. The alarm was about the sun going down.
    DUSK_EVENT = {"duration_hrs": 1.0,
                  "start_time": "2026-08-16T19:00:00+00:00",
                  "end_time":   "2026-08-16T20:00:00+00:00"}
    DUSK_FORECAST = {"_hourly_p50_today": {"2026-08-16 19:00:00": 716,
                                           "2026-08-16 20:00:00": 153,
                                           "2026-08-16 21:00:00": 0}}

    def test_a_zero_the_forecast_predicted_is_not_an_alarm(self):
        v = self._run([0] * 11, forecast=self.DUSK_FORECAST, event=self.DUSK_EVENT)
        self.assertEqual(v.get("lastVppPvStatus"), "n/a (no PV expected)")
        self.assertIs(v.get("lastVppPvSurvived"), True)

    def test_a_zero_the_forecast_did_NOT_predict_is_still_condemned(self):
        # The 15-Jun-2026 fault, with a forecast running to kilowatts. This is
        # the case the whole check exists for and it must not be excused.
        busy = {"_hourly_p50_today": {"2026-08-16 19:00:00": 2600,
                                      "2026-08-16 20:00:00": 4100}}
        v = self._run([0] * 11, forecast=busy, event=self.DUSK_EVENT)
        self.assertEqual(v.get("lastVppPvStatus"), "curtailed")
        self.assertIs(v.get("lastVppPvSurvived"), False)

    def test_an_unknown_forecast_excuses_nothing(self):
        # A forecast that does not cover the window is UNKNOWN, and unknown
        # must not quietly become "expected zero".
        elsewhere = {"_hourly_p50_today": {"2026-07-04 12:00:00": 20}}
        v = self._run([0] * 11, forecast=elsewhere, event=self.DUSK_EVENT)
        self.assertEqual(v.get("lastVppPvStatus"), "curtailed")

    def test_real_pv_still_reads_ran_whatever_the_forecast_said(self):
        v = self._run([1757, 1200, 600, 0, 0], forecast=self.DUSK_FORECAST,
                      event=self.DUSK_EVENT)
        self.assertEqual(v.get("lastVppPvStatus"), "ran")


class TestForecastPeakForWindow(unittest.TestCase):
    """The pure half: what the forecast expected over a window's local hours."""

    HOURLY = {"2026-08-16 19:00:00": 716,
              "2026-08-16 20:00:00": 153,
              "2026-08-16 21:00:00": 0}

    def _local(self, h):
        from london_time import london_localise
        return london_localise(datetime(2026, 8, 16, h, 0))

    def test_takes_the_peak_across_every_hour_touched(self):
        self.assertEqual(
            plugin._forecast_peak_w_for_window(self.HOURLY, self._local(19), self._local(21)),
            716.0)

    def test_a_single_hour_window(self):
        self.assertEqual(
            plugin._forecast_peak_w_for_window(self.HOURLY, self._local(20), self._local(20)),
            153.0)

    def test_unknown_hours_return_None_not_zero(self):
        # None and 0.0 mean opposite things here - one is "no data", the other
        # is "the forecast says nothing will be generated".
        self.assertIsNone(
            plugin._forecast_peak_w_for_window(self.HOURLY, self._local(2), self._local(3)))

    def test_a_forecast_of_genuine_zero_is_zero_not_None(self):
        self.assertEqual(
            plugin._forecast_peak_w_for_window(self.HOURLY, self._local(21), self._local(21)),
            0.0)

    def test_empty_or_missing_inputs(self):
        self.assertIsNone(plugin._forecast_peak_w_for_window({}, self._local(20), self._local(20)))
        self.assertIsNone(plugin._forecast_peak_w_for_window(self.HOURLY, None, None))

    def test_junk_slots_and_values_are_skipped_not_fatal(self):
        junk = dict(self.HOURLY); junk["not-a-date"] = 999; junk["2026-08-16 20:00:00"] = "x"
        self.assertEqual(
            plugin._forecast_peak_w_for_window(junk, self._local(19), self._local(21)),
            716.0)


class TestAnalysePackBalance(unittest.TestCase):
    """analyse_pack_balance — recovering a per-pack signal from the three
    aggregates the inverter actually publishes (v5.68.0).

    The live fixture is the real reading taken while building this: average
    34.9, hottest cluster 39.0, coldest 32.4, four packs. The middle two must
    therefore average (34.9*4 - 39.0 - 32.4)/2 = 34.1, putting the hot end
    4.9 degC clear of its siblings and the cold end only 1.7 — one pack
    running hot, which the plant average alone can never show."""

    def test_live_reading_finds_the_hot_pack(self):
        b = plugin.analyse_pack_balance(34.9, 39.0, 32.4, 4)
        self.assertEqual(b["verdict"], "one_hot")
        self.assertAlmostEqual(b["middle_c"], 34.1, places=1)
        self.assertAlmostEqual(b["hot_gap_c"], 4.9, places=1)
        self.assertAlmostEqual(b["cold_gap_c"], 1.7, places=1)
        self.assertAlmostEqual(b["spread_c"], 6.6, places=1)

    def test_a_lone_cold_pack_is_found_the_same_way(self):
        # Mean sits high, so the LOW end is the one out of step: the middle
        # two must average (33.0*4 - 35.0 - 28.0)/2 = 34.5, which is 6.5 degC
        # clear of the cold end and only 0.5 off the hot one.
        # NB the first draft of this case used (34.0, 35.0, 28.0), which is
        # arithmetically IMPOSSIBLE — it needs the middle two to average 36.5,
        # above the reported maximum — and the guard rightly refused it. The
        # fixture was wrong, not the code.
        b = plugin.analyse_pack_balance(33.0, 35.0, 28.0, 4)
        self.assertEqual(b["verdict"], "one_cold")
        self.assertAlmostEqual(b["middle_c"], 34.5, places=1)

    def test_an_evenly_spread_battery_claims_no_outlier(self):
        b = plugin.analyse_pack_balance(30.0, 31.0, 29.0, 4)
        self.assertEqual(b["verdict"], "even")

    def test_a_wide_but_symmetric_spread_is_still_even(self):
        # both ends equally far from the middle: nothing singles either out
        b = plugin.analyse_pack_balance(30.0, 35.0, 25.0, 4)
        self.assertEqual(b["verdict"], "even")

    def test_a_small_gap_is_not_news_however_lopsided(self):
        b = plugin.analyse_pack_balance(30.05, 30.2, 30.0, 4)
        self.assertEqual(b["verdict"], "even")

    def test_impossible_aggregates_return_nothing_rather_than_fiction(self):
        # a mean outside [min, max] is not the mean of these packs, so every
        # conclusion drawn from it would be invented
        self.assertIsNone(plugin.analyse_pack_balance(50.0, 39.0, 32.4, 4))
        self.assertIsNone(plugin.analyse_pack_balance(10.0, 39.0, 32.4, 4))
        self.assertIsNone(plugin.analyse_pack_balance(34.9, 32.0, 39.0, 4))

    def test_missing_or_junk_figures_return_nothing(self):
        self.assertIsNone(plugin.analyse_pack_balance(None, 39.0, 32.4, 4))
        self.assertIsNone(plugin.analyse_pack_balance(34.9, None, 32.4, 4))
        self.assertIsNone(plugin.analyse_pack_balance(34.9, 39.0, None, 4))
        self.assertIsNone(plugin.analyse_pack_balance("warm", 39.0, 32.4, 4))

    def test_too_few_packs_to_have_an_outlier(self):
        # with two packs the max and min ARE the packs — no middle to be
        # unlike, and an unknown count (0) must not guess
        self.assertIsNone(plugin.analyse_pack_balance(34.9, 39.0, 32.4, 2))
        self.assertIsNone(plugin.analyse_pack_balance(34.9, 39.0, 32.4, 0))

    def test_three_packs_still_works(self):
        b = plugin.analyse_pack_balance(34.0, 40.0, 31.0, 3)
        self.assertIsNotNone(b)
        self.assertEqual(b["packs"], 3)


class TestParsePvStringLabels(unittest.TestCase):
    """_parse_pv_string_labels — the pvStringLabels pref parser (v5.67.0)."""

    def test_blank_pads_pv_names(self):
        out = plugin._parse_pv_string_labels("", 4)
        self.assertEqual([x["label"] for x in out], ["PV1", "PV2", "PV3", "PV4"])
        self.assertTrue(all(x["kwp"] is None for x in out))

    def test_names_with_kwp(self):
        out = plugin._parse_pv_string_labels(
            "South:4.275, East:4.275, West:2.85, NE:2.85", 4)
        self.assertEqual([x["label"] for x in out], ["South", "East", "West", "NE"])
        self.assertEqual([x["kwp"] for x in out], [4.275, 4.275, 2.85, 2.85])

    def test_short_input_pads_and_long_input_trims(self):
        out = plugin._parse_pv_string_labels("South, East", 4)
        self.assertEqual([x["label"] for x in out], ["South", "East", "PV3", "PV4"])
        out = plugin._parse_pv_string_labels("A,B,C,D,E", 2)
        self.assertEqual(len(out), 2)

    def test_junk_kwp_keeps_the_label(self):
        out = plugin._parse_pv_string_labels("South:lots", 1)
        self.assertEqual(out[0]["label"], "South")
        self.assertIsNone(out[0]["kwp"])

    def test_zero_count_returns_empty(self):
        self.assertEqual(plugin._parse_pv_string_labels("South", 0), [])




class TestDashboardAccessControl(unittest.TestCase):
    """v5.75.0 — the dashboard's bind address and token resolution.

    Before this the server bound every interface with no token, so the whole
    energy API sat on the LAN unauthenticated. These pin the replacement: closed
    by default, a stable generated token, and no token leaking into a URL that
    does not need one.
    """

    def _plugin(self, prefs=None, data_dir=None):
        p = object.__new__(plugin.Plugin)
        p.pluginPrefs = prefs if prefs is not None else {}
        p.data_dir = data_dir or tempfile.mkdtemp()
        p.logger = MagicMock()
        return p

    # --- bind address ---

    def test_default_bind_is_loopback(self):
        """A fresh install, with no pref saved, must be closed."""
        p = self._plugin()
        self.assertEqual(p._resolve_dashboard_bind(), plugin.DASHBOARD_BIND_LOOPBACK)

    def test_bind_all_when_explicitly_chosen(self):
        p = self._plugin({"dashboardBind": "all"})
        self.assertEqual(p._resolve_dashboard_bind(), plugin.DASHBOARD_BIND_ALL)

    def test_unrecognised_bind_pref_falls_back_to_loopback(self):
        """Junk in the pref must fail closed, not open."""
        p = self._plugin({"dashboardBind": "banana"})
        self.assertEqual(p._resolve_dashboard_bind(), plugin.DASHBOARD_BIND_LOOPBACK)

    # --- token resolution ---

    def test_token_from_prefs_is_used(self):
        p = self._plugin({"dashboardToken": "  from-prefs  "})
        self.assertEqual(p._resolve_dashboard_token(), "from-prefs")

    def test_indigosecrets_beats_prefs(self):
        p = self._plugin({"dashboardToken": "from-prefs"})
        with patch.object(plugin, "SIGEN_DASHBOARD_TOKEN", "from-secrets"):
            self.assertEqual(p._resolve_dashboard_token(), "from-secrets")

    def test_token_is_generated_and_persisted_when_absent(self):
        data_dir = tempfile.mkdtemp()
        p = self._plugin(data_dir=data_dir)
        token = p._resolve_dashboard_token()
        self.assertTrue(len(token) >= 20, "generated token is too short")
        path = os.path.join(data_dir, "dashboard_token.txt")
        self.assertTrue(os.path.exists(path))
        with open(path, encoding="utf-8") as handle:
            self.assertEqual(handle.read().strip(), token)

    def test_generated_token_file_is_owner_only(self):
        data_dir = tempfile.mkdtemp()
        p = self._plugin(data_dir=data_dir)
        p._resolve_dashboard_token()
        mode = os.stat(os.path.join(data_dir, "dashboard_token.txt")).st_mode
        self.assertEqual(mode & 0o077, 0, "token file is readable by others")

    def test_generated_token_is_stable_across_calls(self):
        """A token that changes each call would lock the user out on restart."""
        data_dir = tempfile.mkdtemp()
        first = self._plugin(data_dir=data_dir)._resolve_dashboard_token()
        second = self._plugin(data_dir=data_dir)._resolve_dashboard_token()
        self.assertEqual(first, second)

    def test_blank_token_file_is_replaced_not_returned(self):
        """An empty file must not read as a configured empty token."""
        data_dir = tempfile.mkdtemp()
        with open(os.path.join(data_dir, "dashboard_token.txt"), "w") as handle:
            handle.write("   \n")
        token = self._plugin(data_dir=data_dir)._resolve_dashboard_token()
        self.assertTrue(token.strip(), "blank token file produced a blank token")

    # --- the URL shown in the log ---

    def test_loopback_url_carries_no_token(self):
        """No point putting a secret in a URL that cannot use it."""
        url = self._plugin()._dashboard_url()
        self.assertIn("127.0.0.1", url)
        self.assertNotIn("token=", url)

    def test_all_interfaces_url_carries_the_token(self):
        p = self._plugin({"dashboardBind": "all", "dashboardToken": "abc123"})
        p._resolve_dashboard_host = lambda: "192.168.1.50"
        self.assertIn("token=abc123", p._dashboard_url())


class TestDawnTargetPct(unittest.TestCase):
    """v5.77.0 — one reader for the summer resilience floor.

    Four call sites used to coerce `dawnSocTarget` independently and every one
    fell back to 10, a value the plugin refuses: save-time validation rejects
    anything under 15 and the startup migration raises a stored value below it.
    A fallback of 10 put the floor ON the 10% health cutoff it exists to sit
    above.
    """

    def _plugin(self, prefs):
        p = object.__new__(plugin.Plugin)
        p.pluginPrefs = prefs
        return p

    def test_reads_the_configured_value(self):
        self.assertEqual(self._plugin({"dawnSocTarget": "22"})._dawn_target_pct(), 22)

    def test_absent_pref_falls_back_to_the_enforced_minimum(self):
        """Not 10. 10 is the health floor, not a resilience buffer."""
        self.assertEqual(self._plugin({})._dawn_target_pct(), 15)

    def test_blank_pref_falls_back_to_the_enforced_minimum(self):
        self.assertEqual(self._plugin({"dawnSocTarget": ""})._dawn_target_pct(), 15)

    def test_junk_pref_falls_back_rather_than_raising(self):
        """A ValueError here would abort snapshot building on every tick."""
        self.assertEqual(self._plugin({"dawnSocTarget": "banana"})._dawn_target_pct(), 15)

    def test_fallback_is_never_below_the_health_floor(self):
        """Whatever the fallback becomes, it must stay above the 1% health
        cutoff and above the 10% figure this rule was written to clear."""
        self.assertGreater(self._plugin({})._dawn_target_pct(), 10)


class TestBankFirstMetrics(unittest.TestCase):
    """v5.79.0: the measurement behind the bank-first export hold.

    None of this touches control — but if it is wrong, the evidence that decides
    whether the hold is free is wrong, and that is worse than no evidence at all.
    """

    class _Manager:
        def __init__(self, shadow_export_kw=None):
            self._shadow_export_kw = shadow_export_kw

        def _calculate_24h_balance(self, snapshot):
            return object()

        def _check_solar_overflow(self, snapshot, balance):
            if self._shadow_export_kw is None:
                return None
            return types.SimpleNamespace(export_kw=self._shadow_export_kw)

    class _Snap:
        def __init__(self, **kw):
            self.now = kw.get("now") or datetime(2026, 8, 31, 7, 12, tzinfo=timezone.utc)
            self.raw_today_kwh = kw.get("raw_today_kwh", 35.6)
            self.solar_overflow_bank_first_max_kwh = kw.get("bank_max", 40.0)
            self.solar_overflow_bank_first_soc = kw.get("bank_soc", 95.0)
            self.max_export_kw = kw.get("max_export_kw", 4.0)
            self.pv_watts = kw.get("pv_watts", 3192)
            self.house_load_watts = kw.get("house_load_watts", 620)

    def _stub(self, forecast=None, inverter=None, shadow_export_kw=None, store=None):
        st = plugin.Plugin.__new__(plugin.Plugin)
        st.store = dict(store or {})
        st.latest_forecast_data = dict(forecast if forecast is not None
                                       else {"forecastStatus": "OK", "todayKwh": 35.6})
        st.latest_inverter_data = dict(inverter or {})
        st.manager = self._Manager(shadow_export_kw)
        st.logger = logging.getLogger("bankfirst-test")
        st.pluginPrefs = {}
        return st

    @staticmethod
    def _decision(holding=False, gate=95.0):
        return types.SimpleNamespace(bank_first_holding=holding, bank_first_gate_pct=gate)

    def _record(self, stub, decision, snap, soc_pct=56.9):
        plugin.Plugin._record_bank_first_metrics(stub, snap, decision, soc_pct)

    # ── the day latch ──────────────────────────────────────────────────────
    def test_a_complete_forecast_below_the_threshold_latches_the_day_small(self):
        st = self._stub()
        self._record(st, self._decision(), self._Snap())
        self.assertTrue(st.store["bank_first_small_latched"])

    def test_a_partial_forecast_does_not_latch(self):
        """Reading low is exactly when you want to keep the units — but a degraded
        reading must not LOCK the day, or one bad fetch decides the afternoon."""
        st = self._stub(forecast={"forecastStatus": "Partial 3/4", "todayKwh": 12.0})
        self._record(st, self._decision(), self._Snap(raw_today_kwh=12.0))
        self.assertFalse(st.store["bank_first_small_latched"])

    def test_a_missing_forecast_does_not_latch(self):
        st = self._stub(forecast={"forecastStatus": "OK", "todayKwh": 0.0})
        self._record(st, self._decision(), self._Snap(raw_today_kwh=0.0))
        self.assertFalse(st.store["bank_first_small_latched"])

    def test_a_big_day_does_not_latch(self):
        st = self._stub(forecast={"forecastStatus": "OK", "todayKwh": 55.0})
        self._record(st, self._decision(), self._Snap(raw_today_kwh=55.0))
        self.assertFalse(st.store["bank_first_small_latched"])

    def test_the_latch_clears_on_a_new_local_day(self):
        st = self._stub(forecast={"forecastStatus": "OK", "todayKwh": 55.0},
                        store={"bank_first_small_latched": True,
                               "bank_first_latch_date": "2026-08-30"})
        self._record(st, self._decision(), self._Snap(raw_today_kwh=55.0))
        self.assertFalse(st.store["bank_first_small_latched"])

    # ── counting the hold ──────────────────────────────────────────────────
    def test_a_held_minute_is_counted_and_stamped(self):
        st = self._stub(shadow_export_kw=1.59)
        self._record(st, self._decision(holding=True), self._Snap())
        self.assertEqual(st.store["bank_first_blocked_samples"], 1)
        self.assertEqual(st.store["bank_first_first_block_local"], "08:12")
        self.assertAlmostEqual(st.store["bank_first_withheld_kwh"], 1.59 / 60.0, places=6)

    def test_the_withheld_figure_comes_from_the_legacy_arm(self):
        """The counterfactual is the same gate with one scalar changed, so any
        divergence is the gate under test and nothing else."""
        st = self._stub(shadow_export_kw=None)
        self._record(st, self._decision(holding=True), self._Snap())
        self.assertEqual(st.store["bank_first_blocked_samples"], 1)
        self.assertEqual(st.store["bank_first_withheld_kwh"], 0.0)

    def test_the_release_is_stamped_once_the_hold_lifts(self):
        st = self._stub(store={"bank_first_blocked_samples": 300})
        self._record(st, self._decision(holding=False), self._Snap(), soc_pct=95.1)
        self.assertEqual(st.store["bank_first_released_local"], "08:12")

    def test_a_day_that_never_held_gets_no_release_stamp(self):
        st = self._stub()
        self._record(st, self._decision(holding=False), self._Snap())
        self.assertFalse(st.store.get("bank_first_released_local"))

    # ── the measured cost ──────────────────────────────────────────────────
    def test_the_clip_boundary_needs_all_three_conditions(self):
        """Export pinned at the cap, the battery no longer taking anything, and the
        battery high enough that it is the battery that stopped taking it. Any one
        relaxed and a windy evening reads as clipping."""
        pinned = {"gridPowerWatts": -3950, "batteryPowerWatts": 50, "batterySoc": 99.0}
        st = self._stub(inverter=pinned)
        self._record(st, self._decision(), self._Snap(), soc_pct=99.0)
        self.assertEqual(st.store["bank_first_clip_boundary_min"], 1)

        for relaxed, soc in ((dict(pinned, gridPowerWatts=-2000), 99.0),
                             (dict(pinned, batteryPowerWatts=1500), 99.0),
                             (dict(pinned), 96.0)):
            with self.subTest(inverter=relaxed, soc=soc):
                st = self._stub(inverter=relaxed)
                self._record(st, self._decision(), self._Snap(), soc_pct=soc)
                self.assertEqual(st.store["bank_first_clip_boundary_min"], 0)

    def test_arming_counts_measured_surplus_not_forecast(self):
        st = self._stub()
        self._record(st, self._decision(), self._Snap(pv_watts=5000, house_load_watts=600))
        self.assertEqual(st.store["bank_first_arm_minutes"], 1)
        self.assertEqual(st.store["bank_first_first_arm_local"], "08:12")

        st = self._stub()
        self._record(st, self._decision(), self._Snap(pv_watts=3192, house_load_watts=620))
        self.assertEqual(st.store["bank_first_arm_minutes"], 0)

    def test_the_soc_minute_counters_move_at_their_thresholds(self):
        st = self._stub()
        self._record(st, self._decision(), self._Snap(), soc_pct=95.0)
        self.assertEqual(st.store["bank_first_minutes_soc_ge_95"], 1)
        self.assertEqual(st.store["bank_first_minutes_soc_ge_99"], 0)
        self._record(st, self._decision(), self._Snap(), soc_pct=99.0)
        self.assertEqual(st.store["bank_first_minutes_soc_ge_95"], 2)
        self.assertEqual(st.store["bank_first_minutes_soc_ge_99"], 1)

    def test_measurement_never_raises_into_the_control_path(self):
        """Analysis must be invisible to control. A broken input logs at debug and
        the tick continues."""
        st = self._stub()
        st.latest_inverter_data = {"gridPowerWatts": "not-a-number"}
        try:
            self._record(st, self._decision(), self._Snap())
        except Exception as exc:  # pragma: no cover - the point of the test
            self.fail(f"metrics raised into the control path: {exc!r}")

    # ── the snapshot must carry the RAW forecast ───────────────────────────
    def test_the_snapshot_reads_todayKwh_not_correctedTodayKwh(self):
        """The most damaging plausible mistake in this change. correctedTodayKwh is
        raw x band factor, and that factor reaches ~1.2 around 30 kWh — feeding it to
        a raw-calibrated 40 kWh threshold would move the gate by a fifth, silently.
        Read off the source because building a full snapshot needs the whole plugin.
        """
        import ast
        import os
        path = os.path.join(os.path.dirname(os.path.abspath(plugin.__file__)), "plugin.py")
        tree = ast.parse(open(path, encoding="utf-8").read())
        found = []
        for node in ast.walk(tree):
            if isinstance(node, ast.keyword) and node.arg == "raw_today_kwh":
                found.append(ast.dump(node.value))
        self.assertTrue(found, "raw_today_kwh is never populated on the snapshot")
        for dumped in found:
            self.assertIn("'todayKwh'", dumped)
            self.assertNotIn("correctedTodayKwh", dumped)


class TestBankFirstLogging(unittest.TestCase):
    """The hold must announce itself. _log_manager_decision returns early when the
    action has not changed, and a hold does not change the action — so without its
    own log pair the whole feature would be silent, and an absence always reads as
    a fault."""

    def _stub(self, store=None, prefs=None):
        st = plugin.Plugin.__new__(plugin.Plugin)
        st.store = dict(store or {})
        st.logger = logging.getLogger("bankfirst-log-test")
        st.pluginPrefs = dict(prefs or {})
        return st

    class _Snap:
        now = datetime(2026, 8, 31, 7, 12, tzinfo=timezone.utc)
        raw_today_kwh = 35.6
        solar_overflow_bank_first_max_kwh = 40.0
        max_export_kw = 4.0

    @staticmethod
    def _decision(holding, gate=95.0):
        return types.SimpleNamespace(bank_first_holding=holding, bank_first_gate_pct=gate)

    def _log(self, stub, holding, soc=56.9):
        with patch.object(plugin, "log") as logged:
            plugin.Plugin._log_bank_first(stub, self._decision(holding), self._Snap(), soc)
        return [c.args[0] for c in logged.call_args_list]

    def test_the_hold_is_logged_once_per_day(self):
        st = self._stub()
        first = self._log(st, holding=True)
        self.assertEqual(len(first), 1)
        self.assertIn("Banking first", first[0])
        self.assertEqual(self._log(st, holding=True), [],
                         "a second held tick must not log again")

    def test_the_release_is_logged_once(self):
        st = self._stub()
        self._log(st, holding=True)
        st.store["bank_first_blocked_samples"] = 329
        st.store["bank_first_withheld_kwh"] = 6.21
        released = self._log(st, holding=False, soc=95.1)
        self.assertEqual(len(released), 1)
        self.assertIn("Bank-first satisfied", released[0])
        self.assertIn("5h29m", released[0])
        self.assertEqual(self._log(st, holding=False, soc=95.4), [])

    def test_a_day_that_never_held_logs_nothing(self):
        self.assertEqual(self._log(self._stub(), holding=False), [])

    def test_the_setting_is_announced_and_names_the_kill_switch(self):
        st = self._stub(prefs={"solarOverflowBankFirstMaxKwh": "40",
                               "solarOverflowBankFirstSoc": "95"})
        with patch.object(plugin, "log") as logged:
            plugin.Plugin._log_bank_first_setting(st)
        msg = logged.call_args_list[0].args[0]
        self.assertIn("ON", msg)
        self.assertIn("95%", msg)
        self.assertIn("40.0 kWh", msg)
        self.assertIn("set the threshold to 0 to disable", msg)

    def test_a_zero_threshold_announces_itself_as_off(self):
        st = self._stub(prefs={"solarOverflowBankFirstMaxKwh": "0"})
        with patch.object(plugin, "log") as logged:
            plugin.Plugin._log_bank_first_setting(st)
        self.assertIn("OFF", logged.call_args_list[0].args[0])

    def test_an_absurd_threshold_warns(self):
        st = self._stub(prefs={"solarOverflowBankFirstMaxKwh": "500"})
        with patch.object(plugin, "log") as logged:
            plugin.Plugin._log_bank_first_setting(st)
        levels = [c.kwargs.get("level") for c in logged.call_args_list]
        self.assertIn("WARNING", levels)


if __name__ == "__main__":
    unittest.main(verbosity=2)
