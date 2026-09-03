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

    def test_gas_unit_kwh_skips_conversion(self):
        # A kWh-reporting meter must NOT be multiplied by the calorific factor.
        api = octopus_api.OctopusAPI(api_key="k", account_id="A", mpan="m", serial="s",
                                     gas_mprn="g", gas_serial="s", gas_unit="kwh")
        api._sum_consumption_for_date = lambda url, d: {"value": 8.0, "slots": 48}
        out = api.get_gas_kwh_for_date("2026-06-20")
        self.assertEqual(out["kwh"], 8.0)                       # NOT 8 * 11.19
        self.assertAlmostEqual(out["m3"], round(8.0 / octopus_api.GAS_KWH_PER_M3, 3), places=3)

    def test_gas_unit_defaults_to_m3(self):
        api = octopus_api.OctopusAPI(api_key="k", account_id="A", mpan="m", serial="s",
                                     gas_mprn="g", gas_serial="s")
        self.assertEqual(api.gas_unit, "m3")

    def test_gas_unit_invalid_falls_back_to_m3(self):
        api = octopus_api.OctopusAPI(api_key="k", account_id="A", mpan="m", serial="s",
                                     gas_unit="furlongs")
        self.assertEqual(api.gas_unit, "m3")

    def test_gas_daily_granularity_one_slot(self):
        # A daily-read meter returns ONE reading; get_gas_kwh_for_date still returns a
        # kWh (the plugin settle gate no longer requires 46 gas slots).
        self.api._sum_consumption_for_date = lambda url, d: {"value": 2.5, "slots": 1}
        out = self.api.get_gas_kwh_for_date("2026-06-20")
        self.assertEqual(out["slots"], 1)
        self.assertIsNotNone(out["kwh"])

    def test_gas_complete_flag_passthrough(self):
        # v5.46.0: get_gas_kwh_for_date carries the full-day-coverage flag through.
        self.api._sum_consumption_for_date = (
            lambda url, d: {"value": 0.716, "slots": 48, "complete": True})
        self.assertTrue(self.api.get_gas_kwh_for_date("2026-06-20")["complete"])
        self.api._sum_consumption_for_date = (
            lambda url, d: {"value": 0.003, "slots": 1, "complete": False})
        self.assertFalse(self.api.get_gas_kwh_for_date("2026-06-20")["complete"])


class TestConsumptionCoverage(unittest.TestCase):
    """_sum_consumption_for_date `complete` flag (v5.46.0) — full-day coverage.

    The 03-07-2026 gas bug: Octopus had returned only the day's FIRST half-hour
    slot when the settle ran; presence-gating froze 1 Jul at 0.034 kWh (£0.00)
    permanently. `complete` is True only when readings reach the end of the
    local day — true for a whole half-hourly day AND for a daily-read meter's
    single 24h reading, false for a partial day."""

    def setUp(self):
        self.api = _make_api()

    def _run(self, intervals):
        self.api._paginate = lambda url, params, authenticated: intervals
        return self.api._sum_consumption_for_date("http://x/", "2026-07-01")

    @staticmethod
    def _slot(i):
        # Half-hour slot i of the local (BST) day 2026-07-01, in UTC Z-form.
        from datetime import datetime, timedelta, timezone
        start = datetime(2026, 6, 30, 23, 0, tzinfo=timezone.utc) + timedelta(minutes=30 * i)
        end = start + timedelta(minutes=30)
        return {"consumption": 0.01,
                "interval_start": start.isoformat().replace("+00:00", "Z"),
                "interval_end": end.isoformat().replace("+00:00", "Z")}

    def test_full_halfhourly_day_is_complete(self):
        out = self._run([self._slot(i) for i in range(48)])
        self.assertEqual(out["slots"], 48)
        self.assertTrue(out["complete"])

    def test_partial_day_is_incomplete(self):
        # THE BUG SHAPE: only the first slot (00:00-00:30 local) has settled.
        out = self._run([self._slot(0)])
        self.assertEqual(out["slots"], 1)
        self.assertFalse(out["complete"])

    def test_daily_read_single_interval_is_complete(self):
        # One reading spanning the whole local day (daily-read gas meter).
        out = self._run([{"consumption": 0.716,
                          "interval_start": "2026-06-30T23:00:00Z",
                          "interval_end":   "2026-07-01T23:00:00Z"}])
        self.assertEqual(out["slots"], 1)
        self.assertTrue(out["complete"])

    def test_no_data_is_incomplete(self):
        out = self._run([])
        self.assertIsNone(out["value"])
        self.assertFalse(out["complete"])


class TestFinancialsErrorPaths(unittest.TestCase):
    def setUp(self):
        self.api = _make_api()
        self.api._get_kraken_token = lambda: "tok"
        self._orig = octopus_api.requests
        octopus_api.requests = MagicMock()

    def tearDown(self):
        octopus_api.requests = self._orig

    def test_http_not_ok_returns_none(self):
        octopus_api.requests.post.return_value = _FakeResp(_LEDGER, ok=False, status=500)
        self.assertIsNone(self.api.get_account_financials(force=True))

    def test_empty_account_returns_none(self):
        octopus_api.requests.post.return_value = _FakeResp({"data": {"account": None}})
        self.assertIsNone(self.api.get_account_financials(force=True))

    def test_graphql_errors_returns_none(self):
        octopus_api.requests.post.return_value = _FakeResp({"errors": [{"message": "bad"}]})
        self.assertIsNone(self.api.get_account_financials(force=True))

    def test_failure_stamps_negative_cache(self):
        octopus_api.requests.post.return_value = _FakeResp({"data": {"account": None}})
        self.api.get_account_financials(force=True)
        self.assertGreater(self.api._financials_neg_at, 0)

    def test_force_bypasses_cache(self):
        octopus_api.requests.post.return_value = _FakeResp(_LEDGER)
        self.api.get_account_financials(force=True)
        octopus_api.requests.post.reset_mock()
        self.api.get_account_financials(force=True)      # force -> hits network again
        octopus_api.requests.post.assert_called()


class TestFinancialsClassification(unittest.TestCase):
    """Import vs export classification, incl. the MPAN-match path (not just OUTGOING)."""

    def setUp(self):
        self.api = _make_api()
        self.api._get_kraken_token = lambda: "tok"
        self._orig = octopus_api.requests
        octopus_api.requests = MagicMock()

    def tearDown(self):
        octopus_api.requests = self._orig

    def _ledger(self, second_mpan, second_code):
        return {"data": {"account": {"balance": 0, "gasAgreements": [],
            "electricityAgreements": [
                {"meterPoint": {"mpan": "1591059073620"},
                 "tariff": {"__typename": "StandardTariff", "tariffCode": "E-1R-SILVER-26-04-01-F",
                            "displayName": "Tracker", "standingCharge": 61.5, "unitRate": 23.0}},
                {"meterPoint": {"mpan": second_mpan},
                 "tariff": {"__typename": "StandardTariff", "tariffCode": second_code,
                            "displayName": "Exp", "standingCharge": 0.0, "unitRate": 12.0}},
            ]}}}

    def test_export_by_mpan_without_outgoing_code(self):
        octopus_api.requests.post.return_value = _FakeResp(self._ledger("1574300590436", "E-1R-WEIRD-F"))
        fin = self.api.get_account_financials(force=True)
        self.assertEqual(fin["export"]["unit_p"], 12.0)
        self.assertEqual(fin["elec"]["unit_p"], 23.0)   # import unaffected

    def test_unknown_mpan_not_assumed_export(self):
        # A second meter that is neither the export MPAN nor OUTGOING-coded must NOT be
        # assumed to be export — a user with a SECOND import supply would otherwise have
        # it mis-tagged and its tariff mistaken for the export rate. (Was the old buggy
        # "any non-import meter is export" heuristic.)
        octopus_api.requests.post.return_value = _FakeResp(self._ledger("9999999999999", "E-1R-WEIRD-F"))
        fin = self.api.get_account_financials(force=True)
        self.assertIsNone(fin["export"])                # no positive export evidence
        self.assertEqual(fin["elec"]["unit_p"], 23.0)   # import meter unaffected

    def test_export_by_outgoing_code(self):
        # OUTGOING in the product code IS positive export evidence.
        octopus_api.requests.post.return_value = _FakeResp(
            self._ledger("9999999999999", "E-1R-OUTGOING-VAR-24-10-26-F"))
        fin = self.api.get_account_financials(force=True)
        self.assertEqual(fin["export"]["unit_p"], 12.0)


class TestGasZeroBoundary(unittest.TestCase):
    def test_gas_zero_m3_is_settleable_zero(self):
        api = _make_api()
        api._sum_consumption_for_date = lambda url, d: {"value": 0.0, "slots": 48}
        out = api.get_gas_kwh_for_date("2026-06-20")
        self.assertEqual(out["m3"], 0.0)
        self.assertEqual(out["kwh"], 0.0)      # 0.0, not None — a real zero-gas day settles


class TestAgileSlotWiring(unittest.TestCase):
    """Regression guard for the v5.59.0 fix.

    get_agile_rates() and battery_manager._plan_agile_import() both existed for months,
    but nothing ever wrote an "agile_slots" key into the rates dict, so the planner's
    `if not tariff.agile_slots` branch fired every single time and the manager imported
    10 kW at the prevailing price — on Agile, possibly the 38p evening peak.

    Neither half was broken on its own, which is exactly why unit tests over each half
    stayed green. The test that catches it has to assert on the JOIN.
    """

    def setUp(self):
        self.api = _make_api()
        self.api._get_tracker_rates = lambda force=False: {"today_p": 21.84}
        self.api._get_tou_rates     = lambda key, force=False: {}
        self.today = octopus_api.datetime.now().date()
        self.tomorrow = self.today + octopus_api.timedelta(days=1)

        def _slots(target_date=None, force=False):
            # Two slots a day, deliberately returned newest-first so the sort is tested.
            base = octopus_api.datetime(target_date.year, target_date.month,
                                        target_date.day, tzinfo=octopus_api.timezone.utc)
            return [(base + octopus_api.timedelta(hours=20), 30.0),
                    (base + octopus_api.timedelta(hours=2),   9.0)]

        self.api.get_agile_rates = _slots

    def _run(self, tariff_key):
        self.api.get_current_tariff = lambda force=False: {"tariff_key": tariff_key}
        return self.api.get_all_monitored_rates()

    def test_agile_slots_present_and_sorted_across_both_days(self):
        out = self._run(octopus_api.TARIFF_AGILE)
        self.assertIn("agile_slots", out, "the key the planner reads must exist")
        slots = out["agile_slots"]
        self.assertEqual(len(slots), 4, "today AND tomorrow — dawn is tomorrow morning")
        self.assertEqual([dt for dt, _ in slots], sorted(dt for dt, _ in slots))
        self.assertEqual({dt.date() for dt, _ in slots}, {self.today, self.tomorrow})

    def test_slots_are_timezone_aware(self):
        # battery_manager compares them against an aware UTC `snapshot.now`; a naive
        # datetime here would raise TypeError inside the planner, not here.
        for dt, _ in self._run(octopus_api.TARIFF_AGILE)["agile_slots"]:
            self.assertIsNotNone(dt.tzinfo)

    def test_today_p_is_the_live_slot_not_the_tracker_rate(self):
        out = self._run(octopus_api.TARIFF_AGILE)
        self.assertIn(octopus_api.TARIFF_AGILE, out)
        self.assertIn(out[octopus_api.TARIFF_AGILE]["today_p"], (9.0, 30.0))
        self.assertNotEqual(out[octopus_api.TARIFF_AGILE]["today_p"], 21.84)

    def test_not_fetched_when_agile_is_not_the_active_tariff(self):
        # No pointless API round-trip for the tariffs that do not need slots.
        self.assertNotIn("agile_slots", self._run(octopus_api.TARIFF_TRACKER))

    def test_one_days_fetch_failing_does_not_lose_the_other(self):
        good = self.api.get_agile_rates

        def _flaky(target_date=None, force=False):
            if target_date == self.tomorrow:
                raise RuntimeError("Octopus 500")   # tomorrow's rates not published yet
            return good(target_date)

        self.api.get_agile_rates = _flaky
        slots = self._run(octopus_api.TARIFF_AGILE)["agile_slots"]
        self.assertEqual(len(slots), 2)
        self.assertTrue(all(dt.date() == self.today for dt, _ in slots))


def _tou_spans(local_date, cheap_from, cheap_to, cheap_p, day_p, days=2):
    """Rate SPANS as Octopus really serves a two-rate tariff, for a LOCAL cheap window.

    Measured against the live API 08-Aug-2026: a two-rate product returns a handful of
    records carrying valid_from AND valid_to, not 48 half-hourly rows — e.g. Go gives
    `8.625p from 2026-08-07T23:30:00Z to 2026-08-08T04:30:00Z`.

    The window is given here in LOCAL time and converted to UTC, because that is the way
    round Octopus defines it. A fixture written directly in UTC would sail through the exact
    BST/GMT confusion these tests exist to catch — which is how the Go window came to be
    stored an hour out in the first place.
    """
    import datetime as _dt
    from zoneinfo import ZoneInfo
    lon = ZoneInfo("Europe/London")
    def z(d):
        return d.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    hh, hm = (int(x) for x in cheap_from.split(":"))
    th, tm = (int(x) for x in cheap_to.split(":"))
    out = []
    for n in range(days):
        base = local_date + _dt.timedelta(days=n)
        start = _dt.datetime(base.year, base.month, base.day, hh, hm, tzinfo=lon)
        end = _dt.datetime(base.year, base.month, base.day, th, tm, tzinfo=lon)
        if end <= start:                      # window wraps midnight
            end += _dt.timedelta(days=1)
        out.append({"valid_from": z(start), "valid_to": z(end), "value_inc_vat": cheap_p})
        out.append({"valid_from": z(end),
                    "valid_to": z(start + _dt.timedelta(days=1)),
                    "value_inc_vat": day_p})
    return out


class TestTariffDetection(unittest.TestCase):
    """Every LIVE Octopus product must classify, measured against /v1/products 08-Aug-2026.

    An unmatched code falls through to TARIFF_UNKNOWN, and that planner branch imports at
    once at half inverter power — on Intelligent Go, buying at 32.4p instead of waiting for
    8p. Both tariffs on the forward plan (Go 12M Fixed, Intelligent Go) were unmatched before
    v5.60.0 because every live product had moved to GO-FIX-* / INTELLI-FIX-*.
    """

    CASES = [
        ("SILVER-26-04-01",              octopus_api.TARIFF_TRACKER),
        ("SILVER-25-04-11",              octopus_api.TARIFF_TRACKER),
        ("AGILE-24-10-01",               octopus_api.TARIFF_AGILE),
        ("GO-VAR-22-10-14",              octopus_api.TARIFF_GO),
        ("GO-FIX-12M-26-06-30",          octopus_api.TARIFF_GO),
        ("INTELLI-FIX-12M-26-06-13",     octopus_api.TARIFF_IGO),
        ("INTELLI-FIX-OEV-12M-26-06-13", octopus_api.TARIFF_IGO),
        ("INTELLI-FLUX-IMPORT-23-07-14", octopus_api.TARIFF_IFLUX),
        ("FLUX-IMPORT-23-02-14",         octopus_api.TARIFF_FLUX),
        ("VAR-22-11-01",                 octopus_api.TARIFF_FLEXIBLE),
    ]

    def test_every_live_product_classifies(self):
        api = _make_api()
        for product, expected in self.CASES:
            with self.subTest(product=product):
                got = api._classify_tariff_code(f"E-1R-{product}-F")
                self.assertEqual(got["tariff_key"], expected)

    def test_intelli_fix_does_not_swallow_intelli_flux(self):
        # The two diverge at the 9th character; ordering in the dict must not matter.
        api = _make_api()
        self.assertEqual(
            api._classify_tariff_code("E-1R-INTELLI-FLUX-IMPORT-23-07-14-F")["tariff_key"],
            octopus_api.TARIFF_IFLUX)

    def test_unknown_product_warns_loudly(self):
        api = _make_api()
        api.logger = MagicMock()
        out = api._classify_tariff_code("E-1R-SOMETHING-NEW-27-01-01-F")
        self.assertEqual(out["tariff_key"], octopus_api.TARIFF_UNKNOWN)
        api.logger.warning.assert_called()          # silence here costs real money


class TestCheapWindowDerivation(unittest.TestCase):
    """The window must come from the rates, in LOCAL time, in BOTH GMT and BST.

    The Go window sat wrong from v5.47.0 to v5.60.0 — stored 23:30-04:30 when the product is
    00:30-05:30 local — because UTC timestamps were read as local. Deriving it kills the bug
    class; these tests pin the timezone behaviour that caused it.
    """

    def setUp(self):
        from zoneinfo import ZoneInfo
        self.tz = ZoneInfo("Europe/London")
        self.api = _make_api()

    def test_go_window_derived_in_winter_gmt(self):
        import datetime as _dt
        s = _tou_spans(_dt.date(2026, 1, 14), "00:30", "05:30", 8.4998, 29.3523)
        self.assertEqual(self.api._derive_cheap_window(s, self.tz), ("00:30", "05:30"))

    def test_go_window_derived_in_summer_bst(self):
        # Same LOCAL window, different UTC timestamps. This is the case that was got wrong.
        import datetime as _dt
        s = _tou_spans(_dt.date(2026, 8, 7), "00:30", "05:30", 8.6250, 29.7226)
        self.assertEqual(self.api._derive_cheap_window(s, self.tz), ("00:30", "05:30"))

    def test_igo_window_wraps_midnight(self):
        import datetime as _dt
        s = _tou_spans(_dt.date(2026, 8, 7), "23:30", "05:30", 8.0, 32.3623)
        self.assertEqual(self.api._derive_cheap_window(s, self.tz), ("23:30", "05:30"))

    def test_flat_tariff_yields_no_window(self):
        # A flat tariff publishes ONE open-ended record with no valid_to.
        self.assertIsNone(self.api._derive_cheap_window(
            [{"valid_from": "2026-08-07T00:00:00Z", "valid_to": None,
              "value_inc_vat": 21.84}], self.tz))

    def test_dynamic_tariff_yields_no_window(self):
        # Agile: 48 distinct half-hourly rates. The cheapest single half hour is NOT a
        # window, and returning it would send the manager to buy in one 30-minute slot.
        import datetime as _dt
        spans = []
        for i in range(48):
            f = _dt.datetime(2026, 8, 7, 0, 0, tzinfo=_dt.timezone.utc) + _dt.timedelta(minutes=30*i)
            spans.append({"valid_from": f.strftime("%Y-%m-%dT%H:%M:%SZ"),
                          "valid_to": (f + _dt.timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                          "value_inc_vat": 10.0 + i * 0.5})
        self.assertIsNone(self.api._derive_cheap_window(spans, self.tz))

    def test_multi_window_tariff_yields_no_window(self):
        # Cosy has THREE cheap blocks a day at the same price. Picking one of them would be
        # worse than admitting we cannot tell, so this must return None and let the table
        # stand. The cheap spans disagree, which is the signal.
        import datetime as _dt
        from zoneinfo import ZoneInfo
        lon = ZoneInfo("Europe/London")
        def z(d):
            return d.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        out = []
        for a, b in ((4, 7), (13, 16), (22, 24)):
            f = _dt.datetime(2026, 8, 7, a, 0, tzinfo=lon)
            t = _dt.datetime(2026, 8, 7, 0, 0, tzinfo=lon) + _dt.timedelta(hours=b)
            out.append({"valid_from": z(f), "valid_to": z(t), "value_inc_vat": 14.0})
        out.append({"valid_from": z(_dt.datetime(2026, 8, 7, 7, 0, tzinfo=lon)),
                    "valid_to":   z(_dt.datetime(2026, 8, 7, 13, 0, tzinfo=lon)),
                    "value_inc_vat": 30.0})
        self.assertIsNone(self.api._derive_cheap_window(out, self.tz))

    def test_single_record_is_refused(self):
        self.assertIsNone(self.api._derive_cheap_window(
            [{"valid_from": "2026-08-07T23:30:00Z", "valid_to": "2026-08-08T04:30:00Z",
              "value_inc_vat": 8.6}], self.tz))

    def test_no_timezone_is_refused_rather_than_guessed(self):
        import datetime as _dt
        s = _tou_spans(_dt.date(2026, 8, 7), "00:30", "05:30", 8.6, 29.7)
        self.assertIsNone(self.api._derive_cheap_window(s, None))


class TestIgoRatesAreFetchedWhenActive(unittest.TestCase):
    """_build_tariff_data reads rates[active_key] for the cheap window.

    IGO/IFLUX were never fetched before v5.60.0, so even with detection fixed the window came
    back None and _plan_tou_import took its "cheap window unavailable, importing now" branch
    at 10 kW — the same shape of failure as the Agile slots bug.
    """

    def setUp(self):
        self.api = _make_api()
        self.api._get_tracker_rates = lambda force=False: {"today_p": 21.84}
        self.fetched = []

        def _tou(key, force=False):
            self.fetched.append(key)
            return {"cheap_start": "23:30", "cheap_end": "05:30", "cheap_p": 8.0}

        self.api._get_tou_rates = _tou

    def _run(self, key):
        self.api.get_current_tariff = lambda force=False: {"tariff_key": key}
        return self.api.get_all_monitored_rates()

    def test_igo_active_gets_its_window(self):
        out = self._run(octopus_api.TARIFF_IGO)
        self.assertIn(octopus_api.TARIFF_IGO, self.fetched)
        self.assertEqual(out[octopus_api.TARIFF_IGO]["cheap_start"], "23:30")

    def test_iflux_active_gets_its_window(self):
        out = self._run(octopus_api.TARIFF_IFLUX)
        self.assertIn(octopus_api.TARIFF_IFLUX, self.fetched)
        self.assertIn(octopus_api.TARIFF_IFLUX, out)

    def test_go_and_flux_still_fetched_for_the_monitor(self):
        self._run(octopus_api.TARIFF_TRACKER)
        self.assertIn(octopus_api.TARIFF_GO, self.fetched)
        self.assertIn(octopus_api.TARIFF_FLUX, self.fetched)

    def test_no_duplicate_fetch_when_go_is_active(self):
        self._run(octopus_api.TARIFF_GO)
        self.assertEqual(self.fetched.count(octopus_api.TARIFF_GO), 1)


# A realistic savingSessions payload: one past joined event, one future joined event.
_SAVING_SESSIONS = {"data": {"savingSessions": {
    "account": {
        "hasJoinedCampaign": True,
        "joinedEvents": [{"eventId": "1001"}, {"eventId": "1002"}],
    },
    "events": [
        {"id": "1001", "code": "OCT12AUG", "startAt": "2026-08-12T18:00:00+01:00",
         "endAt": "2026-08-12T18:30:00+01:00", "rewardPerKwhInOctoPoints": 800},
        {"id": "1002", "code": "OCT01SEP", "startAt": "2026-09-10T18:00:00+01:00",
         "endAt": "2026-09-10T19:00:00+01:00", "rewardPerKwhInOctoPoints": 600},
    ],
}}}


class TestSavingSessions(unittest.TestCase):
    def setUp(self):
        self.api = _make_api()
        self._orig = octopus_api.requests
        octopus_api.requests = MagicMock()
        octopus_api.requests.post.return_value = _FakeResp(_SAVING_SESSIONS)

    def tearDown(self):
        octopus_api.requests = self._orig

    def test_uses_backend_host_not_main_graphql_host(self):
        # This query 404s on the main api.octopus.energy host — it must go to
        # api.backend.octopus.energy, not KRAKEN_GRAPHQL.
        self.api.get_saving_sessions(force=True)
        called_url = octopus_api.requests.post.call_args[0][0]
        self.assertEqual(called_url, octopus_api.KRAKEN_GRAPHQL_BACKEND)
        self.assertNotEqual(called_url, octopus_api.KRAKEN_GRAPHQL)

    def test_has_joined_parsed(self):
        data = self.api.get_saving_sessions(force=True)
        self.assertTrue(data["has_joined"])

    def test_events_parsed_and_sorted(self):
        data = self.api.get_saving_sessions(force=True)
        self.assertEqual(len(data["events"]), 2)
        self.assertEqual(data["events"][0]["id"], "1001")   # earlier startAt first
        self.assertEqual(data["events"][1]["reward_per_kwh_points"], 600)

    def test_timestamps_are_aware_with_offset_preserved(self):
        data = self.api.get_saving_sessions(force=True)
        ev = data["events"][0]
        self.assertIsNotNone(ev["start_at"].tzinfo)
        self.assertEqual(ev["start_at"].utcoffset().total_seconds(), 3600)  # +01:00 (BST)
        self.assertEqual(ev["start_at"].hour, 18)   # local wall-clock hour from the string

    def test_cache_hit_second_call(self):
        self.api.get_saving_sessions(force=True)
        octopus_api.requests.post.reset_mock()
        self.api.get_saving_sessions()          # cached — no network
        octopus_api.requests.post.assert_not_called()

    def test_no_account_configured_returns_none(self):
        api = octopus_api.OctopusAPI(api_key="", account_id="", mpan="", serial="")
        self.assertIsNone(api.get_saving_sessions())

    def test_http_failure_serves_none_when_never_fetched(self):
        octopus_api.requests.post.return_value = _FakeResp({}, ok=False, status=500)
        self.assertIsNone(self.api.get_saving_sessions(force=True))

    def test_http_failure_serves_stale_cache_after_one_success(self):
        good = self.api.get_saving_sessions(force=True)
        octopus_api.requests.post.return_value = _FakeResp({}, ok=False, status=500)
        stale = self.api.get_saving_sessions(force=True)
        self.assertEqual(stale, good)   # served the last good value, not None

    def test_malformed_event_skipped_not_fatal(self):
        payload = {"data": {"savingSessions": {
            "account": {"hasJoinedCampaign": True, "joinedEvents": []},
            "events": [
                {"id": "bad", "code": "X"},   # missing startAt/endAt
                _SAVING_SESSIONS["data"]["savingSessions"]["events"][0],
            ],
        }}}
        octopus_api.requests.post.return_value = _FakeResp(payload)
        data = self.api.get_saving_sessions(force=True)
        self.assertEqual(len(data["events"]), 1)
        self.assertEqual(data["events"][0]["id"], "1001")

    def test_graphql_errors_with_no_data_serves_none(self):
        octopus_api.requests.post.return_value = _FakeResp(
            {"data": None, "errors": [{"message": "boom"}]})
        self.assertIsNone(self.api.get_saving_sessions(force=True))

    def test_sends_the_raw_token_not_the_jwt_prefix(self):
        # MEASURED 03-Sep-2026: the backend host accepts a raw token (and "Bearer <token>")
        # and REJECTS "JWT <token>" with OE-0102, while the main host is the mirror image.
        # v5.80.0 shipped the JWT form and the feature was silently dead.
        self.api.get_saving_sessions(force=True)
        hdrs = octopus_api.requests.post.call_args[1]["headers"]
        self.assertEqual(hdrs["Authorization"], "tok")
        self.assertNotIn("JWT", hdrs["Authorization"])


class TestSavingSessionsPartialFailure(unittest.TestCase):
    """The shape that shipped broken in v5.80.0.

    `events` is public and resolves; `account` needs auth and errors to null. HTTP is 200
    and the events list is complete, so a naive read sees "a healthy reply from an account
    that never joined" — and the caller then goes silent for ever. An account block that is
    absent because of an ERROR is UNKNOWN, never a membership answer.
    """

    def setUp(self):
        self.api = _make_api()
        self._orig = octopus_api.requests
        octopus_api.requests = MagicMock()

    def tearDown(self):
        octopus_api.requests = self._orig

    def _payload(self, account, errors=None):
        p = {"data": {"savingSessions": {
            "account": account,
            "events": _SAVING_SESSIONS["data"]["savingSessions"]["events"],
        }}}
        if errors:
            p["errors"] = errors
        return p

    def test_errored_account_block_is_not_read_as_not_joined(self):
        octopus_api.requests.post.return_value = _FakeResp(self._payload(
            None,
            [{"message": "'Authorization' header is invalid.",
              "path": ["savingSessions", "account"],
              "extensions": {"errorCode": "OE-0102"}}]))
        # Must be a FAILURE (None), never a payload claiming has_joined False.
        self.assertIsNone(self.api.get_saving_sessions(force=True))

    def test_errored_account_purges_the_token_so_the_next_try_reauthenticates(self):
        self.api._kraken_token = "stale"
        octopus_api.requests.post.return_value = _FakeResp(self._payload(
            None, [{"path": ["savingSessions", "account"],
                    "extensions": {"errorCode": "OE-0102"}}]))
        self.api.get_saving_sessions(force=True)
        self.assertIsNone(self.api._kraken_token)

    def test_null_account_without_errors_is_also_unknown_not_false(self):
        octopus_api.requests.post.return_value = _FakeResp(self._payload(None))
        self.assertIsNone(self.api.get_saving_sessions(force=True))

    def test_a_genuine_not_joined_answer_still_comes_through(self):
        # The real negative must survive — this is the case the guard must NOT swallow.
        octopus_api.requests.post.return_value = _FakeResp(self._payload(
            {"hasJoinedCampaign": False, "joinedEvents": []}))
        data = self.api.get_saving_sessions(force=True)
        self.assertIsNotNone(data)
        self.assertFalse(data["has_joined"])

    def test_an_error_on_an_unrelated_path_does_not_bin_a_good_account_block(self):
        octopus_api.requests.post.return_value = _FakeResp(self._payload(
            {"hasJoinedCampaign": True, "joinedEvents": [{"eventId": "1001"}]},
            [{"path": ["savingSessions", "somethingElse"], "extensions": {"errorCode": "X"}}]))
        data = self.api.get_saving_sessions(force=True)
        self.assertIsNotNone(data)
        self.assertTrue(data["has_joined"])


if __name__ == "__main__":
    unittest.main()
