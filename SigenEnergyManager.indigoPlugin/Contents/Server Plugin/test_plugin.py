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
import types
import unittest
from datetime import datetime, timedelta, timezone
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
    from datetime import datetime
    try:
        import pytz
        return datetime.now(pytz.timezone("Europe/London")).date()
    except ImportError:
        return datetime.now().date()


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
        # 07:40 UTC + 4h = 11:40 UTC = 12:40 BST.
        self.assertIn(plugin.Plugin._power_cut_lockout_end_local(stub),
                      ("12:40", "11:40"))       # tolerate a pytz-less test host


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
