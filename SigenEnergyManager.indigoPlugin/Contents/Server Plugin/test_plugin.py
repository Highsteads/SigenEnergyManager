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
        self._gas = {"m3": 0.716, "kwh": 8.03, "slots": slots} if gas == "default" else gas
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
        octo = _FakeOcto(gas={"m3": None, "kwh": None, "slots": 0})
        r = self._run([{"date": d1, "rate_today_p": 23.478, "grid_export_kwh": 10.0}], octo)[d1]
        self.assertFalse(r.get("cost_settled", False))

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
        octo = _FakeOcto(gas={"m3": 2.5, "kwh": 28.0, "slots": 1})  # import keeps 48 slots
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
        octo = _FakeOcto(fin=zero, gas={"m3": 0.0, "kwh": 0.0, "slots": 48})
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

    def test_export_disabled_short_circuits(self):
        # Export disabled by pref -> always False, and nothing suppressed spuriously.
        p = self._mk(export_enabled=False, restored_minutes_ago=60)
        self.assertFalse(p._resolve_export_lockout(50.0))
        self.assertFalse(p.store["power_cut_export_suppressed"])

    def test_soc_exactly_at_floor_allows_export(self):
        # 85 is NOT below the 85 floor -> export allowed (boundary).
        p = self._mk(restored_minutes_ago=60)
        self.assertTrue(p._resolve_export_lockout(85.0))


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
