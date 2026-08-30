#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    test_economics.py
# Description: Characterisation tests for the whole-house economics cluster — the cost
#              settlement and the daily / yesterday / period / calendar summaries. Written
#              BEFORE that code is extracted to its own module, so the extraction has
#              something to be measured against rather than "the tests still pass".
# Author:      CliveS & Claude Opus 5
# Date:        25-08-2026
# Version:     1.0

"""Pin what the economics code does today, against a frozen history fixture.

Two different kinds of test live here and it matters which is which.

The pure calculators — `_compute_daily_economics`, `_wh_build_card`, `_row_standing_p` —
are tested for CORRECTNESS. Every expected number was worked out by hand from the inputs,
so these would catch the arithmetic being wrong today, not merely being changed.

The summaries — `_yesterday_economics`, `_period_economics_summary`,
`_calendar_months_summary`, `_whole_house_summary` — are CHARACTERISATION tests. Their
expected values were captured from the current implementation and then reconciled by hand
where that was practical (day counts, kWh totals). They say "this is what it does", not
"this is what it should do". If one fails after a refactor, the refactor changed behaviour;
that is the whole point of them, and it is not automatically a bug in the new code.

Everything is anchored to a fixed clock. Four of these read `_london_now()` and derive
week/month/year cutoffs from it, so without pinning it the suite would quietly change
meaning every day and break outright at a month boundary.

The fixture deliberately covers every row shape the real file contains: fully settled with
standing rates, settled without them, unsettled, backfilled, a row carrying a shadow
comparison, a minimal row from the oldest schema with most fields absent, an all-zero day,
and a day whose export revenue far exceeds its bill. Sparse rows are the norm in the real
file — 82 of 152 rows were settled when this was written — so absent fields are the common
case, not an edge case.
"""

import json
import os
import sys
import types
import unittest
from datetime import datetime
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

# ---- Indigo + hardware stubs so plugin.py imports standalone -------------
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
sys.modules.setdefault("indigo", _indigo)
sys.modules.setdefault("pymodbus", MagicMock())
sys.modules.setdefault("pymodbus.client", sys.modules["pymodbus"].client)
sys.modules.setdefault("pymodbus.exceptions", sys.modules["pymodbus"].exceptions)
sys.modules.setdefault("requests", MagicMock())

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import plugin   # noqa: E402

FIXTURE = os.path.join(HERE, "test_fixtures", "daily_history_fixture.json")

# The fixture's rows are laid out around this instant: 07-14 is "yesterday",
# 07-13 is "day before", and the 7-day window opens on 07-09.
PINNED_NOW = datetime(2026, 7, 15, 12, 0, tzinfo=ZoneInfo("Europe/London"))


def load_fixture_rows():
    with open(FIXTURE, encoding="utf-8") as handle:
        return json.load(handle)


class _EconBase(unittest.TestCase):
    """Builds a Plugin with the fixture as its history and the clock held still."""

    @classmethod
    def setUpClass(cls):
        cls.rows = load_fixture_rows()
        cls.by_date = {r["date"]: r for r in cls.rows}

    def setUp(self):
        import shutil
        import tempfile
        self.data_dir = tempfile.mkdtemp()
        shutil.copy(FIXTURE, os.path.join(self.data_dir, "daily_history.json"))

        self._real_now = plugin._london_now
        plugin._london_now = lambda: PINNED_NOW

        self.p = object.__new__(plugin.Plugin)
        self.p.data_dir = self.data_dir
        self.p.pluginPrefs = {}
        self.p.logger = MagicMock()
        self.p.store = {"grid_import_daily_kwh": 0.30, "grid_export_daily_kwh": 2.00}
        # Stub the Octopus surface these methods actually reach for, with real
        # numbers. A bare MagicMock returns mocks that flow straight into the
        # arithmetic and fail with a type error twenty frames away.
        self.p.octopus = MagicMock()
        self.p.octopus.gas_mprn = "1234567890"
        self.p.octopus.gas_serial = "G4-TEST-0001"
        self.p.octopus.get_standing_charge_p.return_value = 61.51824
        self.p.octopus.get_account_financials.return_value = {
            "balance_gbp": 142.50,
            "elec": {"standing_p": 61.51824, "unit_p": 28.20},
            "gas":  {"standing_p": 29.06169, "unit_p": 6.58413},
        }
        self.p.octopus.get_import_kwh_for_date.return_value = None
        self.p.octopus.get_gas_kwh_for_date.return_value = None

    def tearDown(self):
        plugin._london_now = self._real_now


# =====================================================================
# Correctness — every figure below was worked out by hand
# =====================================================================

class TestComputeDailyEconomics(unittest.TestCase):
    """20 kWh home, 1 kWh imported, 12 kWh exported, import 28p, export 12p."""

    def setUp(self):
        self.e = plugin.Plugin._compute_daily_economics(20.0, 1.0, 12.0, 28.0, 12.0)

    def test_import_cost(self):
        self.assertEqual(self.e["import_cost_gbp"], 0.28)          # 1.0 x 28p

    def test_export_revenue(self):
        self.assertEqual(self.e["export_revenue_gbp"], 1.44)       # 12.0 x 12p

    def test_no_solar_cost_prices_the_whole_house(self):
        """What the day would have cost buying every kWh the house used."""
        self.assertEqual(self.e["no_solar_cost_gbp"], 5.60)        # 20.0 x 28p

    def test_net_is_revenue_minus_cost(self):
        self.assertEqual(self.e["net_today_gbp"], 1.16)            # 1.44 - 0.28

    def test_solar_benefit(self):
        """Avoided cost plus export revenue, less what was still bought."""
        self.assertEqual(self.e["solar_benefit_gbp"], 6.76)        # 5.60 + 1.44 - 0.28

    def test_rates_are_echoed_back(self):
        self.assertEqual(self.e["import_rate_p"], 28.0)
        self.assertEqual(self.e["export_rate_p"], 12.0)

    def test_zero_day_is_all_zero_not_an_error(self):
        z = plugin.Plugin._compute_daily_economics(0.0, 0.0, 0.0, 28.0, 12.0)
        for k in ("import_cost_gbp", "export_revenue_gbp", "no_solar_cost_gbp",
                  "net_today_gbp", "solar_benefit_gbp"):
            self.assertEqual(z[k], 0.0, k)

    def test_import_only_day_is_a_net_loss(self):
        """No sun, no export: net must be negative, benefit zero-ish."""
        e = plugin.Plugin._compute_daily_economics(24.0, 24.0, 0.0, 28.0, 12.0)
        self.assertEqual(e["import_cost_gbp"], 6.72)
        self.assertEqual(e["export_revenue_gbp"], 0.0)
        self.assertLess(e["net_today_gbp"], 0)


class TestRowStandingCharge(_EconBase):
    """`elec_standing_p_day` is present on only some rows — 65 of 152 in the real file."""

    def test_uses_the_rows_own_rate_when_present(self):
        self.assertAlmostEqual(
            plugin.Plugin._row_standing_p(self.by_date["2026-07-13"], 60.0), 61.51824, places=5)

    def test_falls_back_when_the_row_predates_the_field(self):
        self.assertEqual(plugin.Plugin._row_standing_p(self.by_date["2026-06-25"], 60.0), 60.0)

    def test_minimal_row_falls_back_rather_than_raising(self):
        """The oldest rows carry seven fields. They must not blow up a summary."""
        self.assertEqual(plugin.Plugin._row_standing_p(self.by_date["2026-05-02"], 60.0), 60.0)


class TestWholeHouseCardFromRow(_EconBase):
    """The card is RECOMPUTED from the row's components, not read from wh_net_gbp."""

    def setUp(self):
        super().setUp()
        self.card = plugin.Plugin._wh_card_from_row(self.by_date["2026-07-13"])

    def test_electric_is_unit_plus_standing(self):
        self.assertEqual(self.card["electric_gbp"], 0.63)          # 0.01 + 0.62

    def test_gas_is_unit_plus_standing(self):
        self.assertEqual(self.card["gas_gbp"], 0.70)               # 0.41 + 0.29

    def test_bill_is_both_fuels(self):
        self.assertEqual(self.card["bill_gbp"], 1.33)              # 0.63 + 0.70

    def test_net_is_export_minus_bill(self):
        self.assertEqual(self.card["net_gbp"], 1.31)               # 2.64 - 1.33

    def test_settled_row_is_not_marked_provisional(self):
        self.assertFalse(self.card["provisional"])
        self.assertTrue(self.card["covered"])


# =====================================================================
# Characterisation — captured from current behaviour, reconciled by hand
# =====================================================================

class TestYesterdayEconomics(_EconBase):
    """Yesterday relative to the pinned clock is 2026-07-14."""

    def test_returns_the_figures_and_the_date_it_used(self):
        econ, date_str = self.p._yesterday_economics(12.0, 28.0)
        self.assertEqual(date_str, "2026-07-14")
        self.assertEqual(econ["import_cost_gbp"], 0.23)            # 0.80 kWh x 28.2p
        self.assertEqual(econ["export_revenue_gbp"], 1.80)         # 15.00 kWh x 12p
        self.assertEqual(econ["net_today_gbp"], 1.57)

    def test_prefers_the_rows_own_rate_over_the_fallback(self):
        """The row says 28.2p; the caller's fallback of 99p must not win."""
        econ, _ = self.p._yesterday_economics(12.0, 99.0)
        self.assertEqual(econ["import_rate_p"], 28.2)


class TestPeriodEconomicsSummary(_EconBase):
    """Week / month / year roll-ups. Day counts and kWh reconciled against the fixture."""

    def setUp(self):
        super().setUp()
        self.s = self.p._period_economics_summary(12.0, 28.0)

    def test_week_covers_the_seven_days_from_the_cutoff(self):
        self.assertEqual(self.s["week"]["days"], 7)                # 07-09 .. 07-15

    def test_week_kwh_totals(self):
        self.assertAlmostEqual(self.s["week"]["import_kwh"], 5.35, places=2)
        self.assertAlmostEqual(self.s["week"]["export_kwh"], 100.5, places=2)

    def test_month_is_calendar_month_not_rolling_30_days(self):
        """Eight July rows in the fixture, including the 25 kWh backfilled 07-01."""
        self.assertEqual(self.s["month"]["days"], 8)
        self.assertAlmostEqual(self.s["month"]["import_kwh"], 30.35, places=2)

    def test_year_takes_every_row_in_the_fixture(self):
        self.assertEqual(self.s["year"]["days"], 14)
        self.assertAlmostEqual(self.s["year"]["export_kwh"], 212.5, places=2)

    def test_averages_are_totals_over_days(self):
        wk = self.s["week"]
        self.assertAlmostEqual(wk["import_avg_gbp"],
                               round(wk["import_total_gbp"] / wk["days"], 2), places=2)

    def test_unsettled_days_still_contribute_energy(self):
        """Three of the seven week rows have no settled cost fields. Their kWh must
        still count, or the week under-reports what the house actually did."""
        self.assertGreater(self.s["week"]["import_kwh"], 5.0)


class TestCalendarMonthsSummary(_EconBase):

    def setUp(self):
        super().setUp()
        self.cal = self.p._calendar_months_summary(12.0, 28.0, year=2026)

    def test_reports_the_year_asked_for(self):
        self.assertEqual(self.cal["year"], 2026)

    def test_only_months_with_data_have_days(self):
        months = self.cal["months"]
        populated = [m for m in months if (m.get("days") or 0) > 0]
        self.assertEqual(len(populated), 3)                        # May, June, July
        self.assertEqual({m["month_key"] for m in populated},
                         {"2026-05", "2026-06", "2026-07"})

    def test_empty_months_are_present_and_zeroed_not_absent(self):
        """The page renders twelve months. A missing month would shift the grid."""
        self.assertEqual(len(self.cal["months"]), 12)

    def test_may_totals(self):
        may = next(m for m in self.cal["months"] if m["month_key"] == "2026-05")
        self.assertEqual(may["days"], 3)

    def test_a_year_with_no_rows_returns_twelve_empty_months(self):
        cal = self.p._calendar_months_summary(12.0, 28.0, year=2019)
        self.assertEqual(len(cal["months"]), 12)
        self.assertTrue(all((m.get("days") or 0) == 0 for m in cal["months"]))


class TestWholeHouseSummary(_EconBase):

    def test_produces_a_block_without_raising(self):
        wh = self.p._whole_house_summary(28.2, 12.0)
        self.assertIsInstance(wh, dict)

    def test_survives_an_empty_history(self):
        """A fresh install has no daily_history.json at all."""
        os.remove(os.path.join(self.data_dir, "daily_history.json"))
        wh = self.p._whole_house_summary(28.2, 12.0)
        self.assertIsInstance(wh, dict)


class TestMalformedHistoryIsSurvivable(_EconBase):
    """These read a file that other code writes. It will not always be perfect."""

    def _write(self, payload):
        with open(os.path.join(self.data_dir, "daily_history.json"), "w",
                  encoding="utf-8") as handle:
            handle.write(payload)

    def test_empty_list(self):
        self._write("[]")
        self.assertEqual(self.p._period_economics_summary(12.0, 28.0)["week"]["days"], 0)

    def test_corrupt_json_does_not_propagate(self):
        self._write("{not json at all")
        try:
            self.p._period_economics_summary(12.0, 28.0)
        except Exception as exc:                      # noqa: BLE001
            self.fail(f"corrupt history raised {type(exc).__name__}: {exc}")

    def test_row_missing_every_optional_field(self):
        self._write(json.dumps([{"date": "2026-07-14", "month": "2026-07"}]))
        try:
            self.p._period_economics_summary(12.0, 28.0)
            self.p._yesterday_economics(12.0, 28.0)
        except Exception as exc:                      # noqa: BLE001
            self.fail(f"a bare row raised {type(exc).__name__}: {exc}")


class TestWholeHouseCardArithmetic(unittest.TestCase):
    """Pin every term of the whole-house card independently.

    The characterisation tests above cover shape and specific paths, and a mutation
    run showed that is not enough: flipping `*` to `+` in the gas unit cost,
    turning `eu + es` into `eu - es` in the electricity total, dropping the gas
    standing charge from the bill, and — worst — reversing the sign of `net_gbp`
    all survived them. `net_gbp` is the headline figure on the cost page, so a
    silent sign flip there reports a bill as a profit.

    The inputs are chosen so that EVERY intermediate is a different number:

        electricity unit     10 kWh x 30p   = 3.00
        electricity standing        50p     = 0.50
        gas unit              8 kWh x  7p   = 0.56
        gas standing                27p     = 0.27
        bill                                = 4.33
        export               21 kWh x 15p   = 3.15
        net                  3.15 - 4.33    = -1.18   (negative on purpose)

    No two terms are equal and no two sums coincide, so any swapped operand,
    flipped operator or dropped term changes a value this class asserts.
    """

    @classmethod
    def setUpClass(cls):
        cls.card = plugin.Plugin._wh_build_card(
            import_kwh=10.0, export_kwh=21.0, elec_unit_p=30.0, export_rate_p=15.0,
            elec_standing_p=50.0, gas_kwh=8.0, gas_unit_p=7.0, gas_standing_p=27.0,
            provisional=False, gas_estimated=False)

    def test_electricity_unit_is_kwh_times_rate(self):
        self.assertEqual(self.card["electric_unit_gbp"], 3.00)

    def test_electricity_standing_is_pence_converted(self):
        self.assertEqual(self.card["electric_standing_gbp"], 0.50)

    def test_electricity_total_ADDS_standing_to_unit(self):
        """eu - es would give 2.50 and survived the earlier tests."""
        self.assertEqual(self.card["electric_gbp"], 3.50)

    def test_gas_unit_MULTIPLIES_kwh_by_rate(self):
        """gas_kwh + gas_unit_p/100 would give 8.07 and survived."""
        self.assertEqual(self.card["gas_unit_gbp"], 0.56)

    def test_gas_standing_is_pence_converted(self):
        self.assertEqual(self.card["gas_standing_gbp"], 0.27)

    def test_gas_total_adds_standing_to_unit(self):
        self.assertEqual(self.card["gas_gbp"], 0.83)

    def test_bill_includes_all_four_components(self):
        """Dropping the gas standing charge gives 4.06 and survived."""
        self.assertEqual(self.card["bill_gbp"], 4.33)

    def test_export_is_kwh_times_export_rate(self):
        self.assertEqual(self.card["export_gbp"], 3.15)

    def test_net_is_export_MINUS_bill_and_can_be_negative(self):
        """bill - exp gives +1.18. Same magnitude, opposite meaning: it reports a
        loss as a gain on the page's headline number."""
        self.assertEqual(self.card["net_gbp"], -1.18)

    def test_not_covered_when_export_falls_short_of_the_bill(self):
        self.assertIs(self.card["covered"], False)

    def test_covered_when_export_beats_the_bill(self):
        card = plugin.Plugin._wh_build_card(
            import_kwh=1.0, export_kwh=40.0, elec_unit_p=30.0, export_rate_p=15.0,
            elec_standing_p=50.0, gas_kwh=8.0, gas_unit_p=7.0, gas_standing_p=27.0,
            provisional=False, gas_estimated=False)
        self.assertEqual(card["export_gbp"], 6.00)
        self.assertEqual(card["bill_gbp"], 1.63)          # 0.30+0.50+0.56+0.27
        self.assertEqual(card["net_gbp"], 4.37)          # 6.00 - 1.63
        self.assertIs(card["covered"], True)

    def test_the_kwh_are_carried_beside_the_costs(self):
        """So a consumer cannot pair a live cost with a stale volume."""
        self.assertEqual(self.card["import_kwh"], 10.0)
        self.assertEqual(self.card["export_kwh"], 21.0)
        self.assertEqual(self.card["gas_kwh"], 8.0)

    def test_a_zero_export_rate_is_a_real_rate_not_a_missing_one(self):
        """0p must not fall back to the 12p default."""
        card = plugin.Plugin._wh_build_card(
            import_kwh=10.0, export_kwh=21.0, elec_unit_p=30.0, export_rate_p=0.0,
            elec_standing_p=50.0, gas_kwh=8.0, gas_unit_p=7.0, gas_standing_p=27.0,
            provisional=False, gas_estimated=False)
        self.assertEqual(card["export_gbp"], 0.0)

    def test_absent_export_rate_uses_the_default(self):
        card = plugin.Plugin._wh_build_card(
            import_kwh=10.0, export_kwh=21.0, elec_unit_p=30.0, export_rate_p=None,
            elec_standing_p=50.0, gas_kwh=8.0, gas_unit_p=7.0, gas_standing_p=27.0,
            provisional=False, gas_estimated=False)
        self.assertEqual(card["export_gbp"], 2.52)        # 21 x 12p

    def test_unknown_unit_rate_with_real_import_refuses_to_guess(self):
        """Returning 0.00 left the standing charge as the whole bill, which beat
        the export revenue and painted a green Covered badge over a bill nobody
        had worked out."""
        card = plugin.Plugin._wh_build_card(
            import_kwh=10.0, export_kwh=21.0, elec_unit_p=None, export_rate_p=15.0,
            elec_standing_p=50.0, gas_kwh=8.0, gas_unit_p=7.0, gas_standing_p=27.0,
            provisional=False, gas_estimated=False)
        self.assertTrue(card["rate_missing"])
        for field in ("electric_unit_gbp", "electric_gbp", "bill_gbp", "net_gbp", "covered"):
            self.assertIsNone(card[field], field)

    def test_no_import_and_no_rate_is_not_a_missing_rate(self):
        """Nothing was bought, so the unknown price of it does not matter."""
        card = plugin.Plugin._wh_build_card(
            import_kwh=0.0, export_kwh=21.0, elec_unit_p=None, export_rate_p=15.0,
            elec_standing_p=50.0, gas_kwh=8.0, gas_unit_p=7.0, gas_standing_p=27.0,
            provisional=False, gas_estimated=False)
        self.assertFalse(card["rate_missing"])
        self.assertEqual(card["bill_gbp"], 1.33)          # 0 + 0.50 + 0.56 + 0.27


if __name__ == "__main__":
    unittest.main(verbosity=2)
