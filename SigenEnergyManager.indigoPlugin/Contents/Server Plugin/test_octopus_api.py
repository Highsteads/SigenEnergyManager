#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    test_octopus_api.py
# Description: Unit tests for octopus_api.py v1.1 additions — the Kraken account-ledger
#              parser (get_account_financials), the per-day import/gas consumption
#              helpers, and the m3->kWh calorific conversion. Network is stubbed so the
#              parsing/classification logic is exercised without hitting Octopus.
# Author:      CliveS & Claude Opus 4.8
# Date:        21-06-2026
# Version:     1.0

import os
import sys
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import octopus_api  # noqa: E402


class _FakeResp:
    def __init__(self, payload, ok=True, status=200):
        self._p = payload
        self.ok = ok
        self.status_code = status

    def json(self):
        return self._p


# A realistic Kraken account(...) ledger payload: import (Tracker), export
# (Outgoing) and gas (12M fix) agreements, plus a balance in pence.
_LEDGER = {"data": {"account": {
    "balance": 39239,
    "electricityAgreements": [
        {"meterPoint": {"mpan": "1591059073620"},
         "tariff": {"__typename": "StandardTariff",
                    "tariffCode": "E-1R-SILVER-26-04-01-F",
                    "displayName": "Octopus Tracker",
                    "standingCharge": 61.51824, "unitRate": 23.478}},
        {"meterPoint": {"mpan": "1574300590436"},
         "tariff": {"__typename": "StandardTariff",
                    "tariffCode": "E-1R-OUTGOING-VAR-24-10-26-F",
                    "displayName": "Outgoing Octopus",
                    "standingCharge": 0.0, "unitRate": 12.0}},
    ],
    "gasAgreements": [
        {"tariff": {"__typename": "GasTariffType",
                    "tariffCode": "G-1R-OE-FIX-12M-26-06-01-F",
                    "displayName": "Octopus 12M Fixed",
                    "standingCharge": 29.06169, "unitRate": 6.58413}},
    ],
}}}


def _make_api():
    api = octopus_api.OctopusAPI(
        api_key="k", account_id="A-TEST",
        mpan="1591059073620", serial="21M0",
        export_mpan="1574300590436", export_serial="21M0",
        gas_mprn="5036739000", gas_serial="G4F",
    )
    api._get_kraken_token = lambda: "tok"     # skip the network token mutation
    return api


class TestSafeFloat(unittest.TestCase):
    def test_value(self):
        self.assertEqual(octopus_api._safe_float("12.5"), 12.5)

    def test_blank_default_none(self):
        self.assertIsNone(octopus_api._safe_float(""))

    def test_bad_default(self):
        self.assertEqual(octopus_api._safe_float("nope", 1.0), 1.0)


class TestCalorific(unittest.TestCase):
    def test_default_factor(self):
        # 1.02264 * 39.5 / 3.6 ~= 11.22
        self.assertAlmostEqual(octopus_api.GAS_KWH_PER_M3, 11.2206, places=3)


class TestAccountFinancials(unittest.TestCase):
    def setUp(self):
        self.api = _make_api()
        self._orig = octopus_api.requests
        octopus_api.requests = MagicMock()
        octopus_api.requests.post.return_value = _FakeResp(_LEDGER)

    def tearDown(self):
        octopus_api.requests = self._orig

    def test_elec_import_is_bill_exact(self):
        fin = self.api.get_account_financials(force=True)
        self.assertEqual(fin["elec"]["standing_p"], 61.51824)
        self.assertEqual(fin["elec"]["unit_p"], 23.478)
        self.assertEqual(fin["elec"]["display_name"], "Octopus Tracker")

    def test_export_distinguished_from_import(self):
        fin = self.api.get_account_financials(force=True)
        # The OUTGOING agreement must land in 'export', not overwrite 'elec'.
        self.assertEqual(fin["export"]["unit_p"], 12.0)
        self.assertNotEqual(fin["elec"]["tariff_code"], fin["export"].get("tariff_code"))

    def test_gas_rates(self):
        fin = self.api.get_account_financials(force=True)
        self.assertEqual(fin["gas"]["standing_p"], 29.06169)
        self.assertEqual(fin["gas"]["unit_p"], 6.58413)

    def test_balance_pence_to_gbp(self):
        fin = self.api.get_account_financials(force=True)
        self.assertEqual(fin["balance_gbp"], 392.39)

    def test_cache_hit_second_call(self):
        self.api.get_account_financials(force=True)
        octopus_api.requests.post.reset_mock()
        self.api.get_account_financials()          # cached — no network
        octopus_api.requests.post.assert_not_called()


class TestPerDayConsumption(unittest.TestCase):
    def setUp(self):
        self.api = _make_api()

    def test_import_kwh(self):
        self.api._sum_consumption_for_date = lambda url, d: {"value": 0.068, "slots": 49}
        self.assertEqual(self.api.get_import_kwh_for_date("2026-06-20"),
                         {"kwh": 0.068, "slots": 49})

    def test_gas_m3_converted_to_kwh(self):
        self.api._sum_consumption_for_date = lambda url, d: {"value": 0.716, "slots": 49}
        out = self.api.get_gas_kwh_for_date("2026-06-20")
        self.assertEqual(out["m3"], 0.716)
        self.assertAlmostEqual(out["kwh"], round(0.716 * octopus_api.GAS_KWH_PER_M3, 3), places=3)

    def test_gas_unsettled_passthrough(self):
        self.api._sum_consumption_for_date = lambda url, d: {"value": None, "slots": 0}
        out = self.api.get_gas_kwh_for_date("2026-06-20")
        self.assertIsNone(out["kwh"])
        self.assertEqual(out["slots"], 0)

    def test_gas_no_meter_returns_none(self):
        api = octopus_api.OctopusAPI(api_key="k", account_id="A", mpan="m", serial="s")
        self.assertIsNone(api.get_gas_kwh_for_date("2026-06-20"))


if __name__ == "__main__":
    unittest.main()
