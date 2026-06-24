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
    def __init__(self, fin="default", gas="default", slots=48):
        self._fin = {
            "elec":   {"standing_p": 61.51824, "unit_p": 23.478},
            "gas":    {"unit_p": 6.58413, "standing_p": 29.06169},
            "export": {"unit_p": 12.0}, "balance_gbp": 392.39,
        } if fin == "default" else fin
        self._slots = slots
        self._gas = {"m3": 0.716, "kwh": 8.03, "slots": slots} if gas == "default" else gas

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
