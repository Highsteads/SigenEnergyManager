#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    test_review_2026_10_02_money.py
# Description: Money faults from the 02-10-2026 review: Flux bands lost on one
#              failed refetch, Flux prices read off a product the house is not
#              billed on, a clocks-back day settled short, a free-hour claim that
#              swallows every credit, and an email stand-in that evicts Axle's
#              real settlement. Network is stubbed throughout.
# Author:      CliveS & Claude Opus 5.5
# Date:        02-10-2026
# Version:     1.0

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import octopus_api as oa             # noqa: E402
import economics as ec               # noqa: E402
import free_hour_credits as fh       # noqa: E402
import vpp_ledger as VL              # noqa: E402
from london_time import london_tz    # noqa: E402

UTC = timezone.utc

# Flux as the API serves it: a handful of SPANS, not 48 half-hours.
FLUX_SPANS = [
    {"valid_from": "2026-10-01T18:00:00Z", "valid_to": "2026-10-02T01:00:00Z", "value_inc_vat": 24.4},
    {"valid_from": "2026-10-02T01:00:00Z", "valid_to": "2026-10-02T04:00:00Z", "value_inc_vat": 14.6},
    {"valid_from": "2026-10-02T04:00:00Z", "valid_to": "2026-10-02T15:00:00Z", "value_inc_vat": 24.4},
    {"valid_from": "2026-10-02T15:00:00Z", "valid_to": "2026-10-02T18:00:00Z", "value_inc_vat": 34.1},
    {"valid_from": "2026-10-02T18:00:00Z", "valid_to": "2026-10-03T01:00:00Z", "value_inc_vat": 24.4},
]


def _api(**kw):
    a = oa.OctopusAPI(kw.pop("api_key", "k"), kw.pop("account_id", "A-1"),
                      kw.pop("mpan", "1111111111111"), kw.pop("serial", "S1"),
                      region="F", **kw)
    a._get_kraken_token = lambda: "tok"
    return a


class _Resp:
    def __init__(self, payload, status=200):
        self._p = payload
        self.status_code = status
        self.ok = 200 <= status < 300

    def json(self):
        return self._p


# ================================================================
# 1. A failed Flux refetch must not throw away the last good bands
# ================================================================

class TestTouRatesServeStaleOnFailure(unittest.TestCase):

    def _setup(self):
        api = _api()
        api._find_product_code = lambda key: "FLUX-IMPORT-23-02-14"
        calls = {"n": 0, "fail": False}

        def fetch(product_code, tariff_code, day):
            calls["n"] += 1
            return [] if calls["fail"] else list(FLUX_SPANS)
        api._fetch_rate_schedule = fetch
        return api, calls

    def test_a_failed_refetch_keeps_the_last_good_bands(self):
        api, calls = self._setup()
        good = api.get_tou_rates(oa.TARIFF_FLUX)
        self.assertEqual(good["peak_p"], 34.1)
        api._rates_cache["tou_flux"]["cached_at"] -= oa.RATES_CACHE_TTL + 1   # expire
        calls["fail"] = True
        after = api.get_tou_rates(oa.TARIFF_FLUX)
        self.assertEqual(after, good)

    def test_a_failure_backs_off_instead_of_refetching_every_tick(self):
        api, calls = self._setup()
        api.get_tou_rates(oa.TARIFF_FLUX)
        api._rates_cache["tou_flux"]["cached_at"] -= oa.RATES_CACHE_TTL + 1
        calls["fail"] = True
        api.get_tou_rates(oa.TARIFF_FLUX)
        n = calls["n"]
        for _ in range(5):
            api.get_tou_rates(oa.TARIFF_FLUX)
        self.assertEqual(calls["n"], n)

    def test_recovery_after_the_back_off_refreshes(self):
        api, calls = self._setup()
        api.get_tou_rates(oa.TARIFF_FLUX)
        api._rates_cache["tou_flux"]["cached_at"] -= oa.RATES_CACHE_TTL + 1
        calls["fail"] = True
        api.get_tou_rates(oa.TARIFF_FLUX)
        api._rates_neg_at["tou_flux"] -= oa.RATES_NEG_CACHE_TTL + 1
        calls["fail"] = False
        n = calls["n"]
        self.assertEqual(api.get_tou_rates(oa.TARIFF_FLUX)["peak_p"], 34.1)
        self.assertEqual(calls["n"], n + 1)
        self.assertNotIn("tou_flux", api._rates_neg_at)


# ================================================================
# 2. The ACTIVE tariff's prices come from the product the house is billed on
# ================================================================

class TestActiveTouProductIsTheBilledOne(unittest.TestCase):

    BILLED = "FLUX-IMPORT-23-02-14"
    RELAUNCH = "FLUX-IMPORT-26-10-01"

    def _api(self, key=oa.TARIFF_FLUX, product=None):
        api = _api()
        api.get_current_tariff = lambda force=False: {
            "tariff_key": key, "product_code": product or self.BILLED,
            "tariff_code": f"E-1R-{product or self.BILLED}-F", "display_name": "x"}
        api._probe_product_by_prefix = lambda prefixes: (
            self.RELAUNCH if "FLUX-IMPORT" in prefixes else "GO-VAR-99-01-01")
        return api

    def test_active_flux_uses_the_billed_product(self):
        self.assertEqual(self._api()._find_product_code(oa.TARIFF_FLUX), self.BILLED)

    def test_a_comparison_tariff_still_probes(self):
        self.assertEqual(self._api()._find_product_code(oa.TARIFF_GO), "GO-VAR-99-01-01")

    def test_flux_is_probed_when_it_is_not_the_active_tariff(self):
        api = self._api(key=oa.TARIFF_GO, product="GO-VAR-22-10-14")
        self.assertEqual(api._find_product_code(oa.TARIFF_FLUX), self.RELAUNCH)
        self.assertEqual(api._find_product_code(oa.TARIFF_GO), "GO-VAR-22-10-14")

    def test_monitored_rates_fetch_the_billed_flux_product(self):
        api = self._api()
        seen = []

        def fetch(product_code, tariff_code, day):
            seen.append(product_code)
            return list(FLUX_SPANS)
        api._fetch_rate_schedule = fetch
        api._get_tracker_rates = lambda force=False: {}
        api.get_all_monitored_rates()
        self.assertIn(self.BILLED, seen)
        self.assertNotIn(self.RELAUNCH, seen)


# ================================================================
# 3. Settling waits for the WHOLE local day, however long that day is
# ================================================================

class _FakeOcto:
    gas_mprn = None
    gas_serial = None

    def __init__(self, slots):
        self.slots = slots

    def get_account_financials(self, force=False):
        return {"elec": {"standing_p": 61.5, "unit_p": 24.4}, "gas": None,
                "export": {"unit_p": 15.0}}

    def get_import_kwh_for_date(self, date_str):
        n = self.slots[date_str]
        return {"kwh": round(0.5 * n, 3), "slots": n}

    def get_gas_kwh_for_date(self, date_str):
        return None


class TestSettleNeedsTheWholeLocalDay(unittest.TestCase):

    def _settle(self, day, slots, now):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "daily_history.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump([{"date": day, "grid_import_kwh": 25.0, "grid_export_kwh": 3.0,
                        "rate_today_p": 24.4, "export_rate_p": 15.0,
                        "elec_standing_p_day": 61.5}], f)
        E = ec.Economics(d, octopus=_FakeOcto({day: slots}), now_fn=lambda: now)
        E._settle_whole_house_costs()
        with open(path, encoding="utf-8") as f:
            return json.load(f)[0]

    def test_clocks_back_day_missing_four_slots_is_not_settled(self):
        tz = london_tz()
        row = self._settle("2026-10-25", 46, datetime(2026, 10, 27, 12, tzinfo=tz))
        self.assertFalse(row.get("cost_settled"))

    def test_clocks_back_day_with_48_of_50_settles(self):
        tz = london_tz()
        row = self._settle("2026-10-25", 48, datetime(2026, 10, 27, 12, tzinfo=tz))
        self.assertTrue(row.get("cost_settled"))

    def test_normal_day_still_settles_at_46_of_48(self):
        tz = london_tz()
        row = self._settle("2026-10-20", 46, datetime(2026, 10, 22, 12, tzinfo=tz))
        self.assertTrue(row.get("cost_settled"))
        row = self._settle("2026-10-20", 45, datetime(2026, 10, 22, 12, tzinfo=tz))
        self.assertFalse(row.get("cost_settled"))

    def test_clocks_forward_day_settles_at_44_of_46(self):
        tz = london_tz()
        row = self._settle("2027-03-28", 44, datetime(2027, 3, 30, 12, tzinfo=tz))
        self.assertTrue(row.get("cost_settled"))

    def test_import_and_export_pass_the_complete_flag_through(self):
        tz = london_tz()
        start = datetime(2026, 10, 25, tzinfo=tz).astimezone(UTC)
        rows = []
        for i in range(46):
            a = start + timedelta(minutes=30 * i)
            rows.append({"consumption": 0.5,
                         "interval_start": a.isoformat().replace("+00:00", "Z"),
                         "interval_end": (a + timedelta(minutes=30)).isoformat().replace("+00:00", "Z")})
        api = _api()
        api._paginate = lambda url, params, authenticated=False: list(rows)
        self.assertIs(api.get_import_kwh_for_date("2026-10-25")["complete"], False)
        self.assertIs(api.get_export_kwh_for_date("2026-10-25", "9", "S9")["complete"], False)


# ================================================================
# 4. A claim still MEASURING must not swallow every credit
# ================================================================

def _ld(dt):
    return dt.strftime("%Y-%m-%d")


class TestMeasuringClaimDoesNotSinkCredits(unittest.TestCase):

    def _ledger(self):
        led = fh.new_ledger()
        h1 = {"id": "E1", "start": datetime(2026, 9, 27, 12, tzinfo=UTC),
              "end": datetime(2026, 9, 27, 13, tzinfo=UTC), "rate_p": 24.4}
        h2 = {"id": "E2", "start": datetime(2026, 10, 4, 12, tzinfo=UTC),
              "end": datetime(2026, 10, 4, 13, tzinfo=UTC), "rate_p": 24.4}
        fh.record_hours(led, [h1, h2], datetime(2026, 10, 6, 9, tzinfo=UTC), _ld)
        fh.set_meter_kwh(led, "E2", 10.0)              # 27 Sep never measured
        return led

    def test_the_credit_goes_to_the_measured_claim(self):
        led = self._ledger()
        fh.assign_credits(led, [{"id": "c1", "posted": "2026-10-06", "amount_p": 244,
                                 "title": "Free electricity",
                                 "reason": "FREE_ELECTRICITY_REWARD"}])
        self.assertEqual(led["claims"]["2026-09-27"]["credits"], [])
        self.assertEqual([c["amount_p"] for c in led["claims"]["2026-10-04"]["credits"]], [244])
        st = fh.status(led["claims"]["2026-10-04"], datetime(2026, 10, 20, tzinfo=UTC))
        self.assertEqual(st["state"], fh.STATE_PAID)

    def test_a_credit_with_nowhere_measured_to_go_waits(self):
        led = fh.new_ledger()
        fh.record_hours(led, [{"id": "E1", "start": datetime(2026, 9, 27, 12, tzinfo=UTC),
                               "end": datetime(2026, 9, 27, 13, tzinfo=UTC), "rate_p": 24.4}],
                        datetime(2026, 9, 28, tzinfo=UTC), _ld)
        c = {"id": "c1", "posted": "2026-10-01", "amount_p": 244,
             "title": "Free electricity", "reason": "FREE_ELECTRICITY_REWARD"}
        self.assertEqual(fh.assign_credits(led, [c]), [])
        self.assertNotIn("c1", led["assigned_credit_ids"])     # tried again next time
        fh.set_meter_kwh(led, "E1", 10.0)
        self.assertEqual(len(fh.assign_credits(led, [c])), 1)

    def test_an_exact_match_beats_oldest_first(self):
        led = fh.new_ledger()
        hours = [{"id": "A", "start": datetime(2026, 9, 27, 12, tzinfo=UTC),
                  "end": datetime(2026, 9, 27, 13, tzinfo=UTC), "rate_p": 24.4},
                 {"id": "B", "start": datetime(2026, 10, 4, 12, tzinfo=UTC),
                  "end": datetime(2026, 10, 4, 13, tzinfo=UTC), "rate_p": 24.4}]
        fh.record_hours(led, hours, datetime(2026, 10, 6, tzinfo=UTC), _ld)
        fh.set_meter_kwh(led, "A", 16.0)               # 390p owed
        fh.set_meter_kwh(led, "B", 10.0)               # 244p owed
        fh.assign_credits(led, [{"id": "c1", "posted": "2026-10-06", "amount_p": 244,
                                 "reason": "FREE_ELECTRICITY_REWARD"}])
        self.assertEqual(led["claims"]["2026-09-27"]["credits"], [])
        self.assertEqual(len(led["claims"]["2026-10-04"]["credits"]), 1)

    def test_a_claim_stuck_measuring_is_aged_out(self):
        led = self._ledger()
        end = datetime(2026, 9, 27, 13, tzinfo=UTC)
        fh.prune(led, end + timedelta(days=fh.CLAIM_LOOKBACK_DAYS - 1))
        self.assertIn("2026-09-27", led["claims"])
        self.assertIsNone(led["claims"]["2026-09-27"]["closed_at"])
        at = end + timedelta(days=fh.CLAIM_LOOKBACK_DAYS)
        fh.prune(led, at)
        self.assertIsNotNone(led["claims"]["2026-09-27"]["closed_at"])
        fh.prune(led, at + timedelta(days=fh.KEEP_NOTHING_DUE_DAYS + 1))
        self.assertNotIn("2026-09-27", led["claims"])

    def test_an_hourless_claim_ages_from_its_date(self):
        led = fh.new_ledger()
        led["claims"]["2026-09-27"] = {"date": "2026-09-27", "hours": {}, "credits": [],
                                       "inverter_kwh": 3.0, "notified": {},
                                       "closed_at": None}
        fh.prune(led, datetime(2026, 10, 27, 12, tzinfo=UTC))
        self.assertIsNotNone(led["claims"]["2026-09-27"]["closed_at"])


# ================================================================
# 5. An email stand-in never evicts Axle's real settlement
# ================================================================

class TestEmailStandInNeverEvictsTheRealRow(unittest.TestCase):

    EMAIL = {"transactions": [{
        "transaction_type": "flex event", "transaction_id": "email-2026-08-16T19:00",
        "start_time": "2026-08-16T19:00:00+00:00", "end_time": "2026-08-16T20:00:00+00:00",
        "settlement_date": None, "flex_kwh": -3.87, "credit_pence": 387}]}
    REAL = {"transactions": [{
        "transaction_type": "flex event", "transaction_id": "t-0816",
        "start_time": "2026-08-16T19:00:00+00:00", "end_time": "2026-08-16T20:00:00+00:00",
        "settlement_date": "2026-08-21", "payment_status": "paid",
        "flex_kwh": -4.02, "credit_pence": 402}]}

    def test_the_next_mail_scan_leaves_the_real_row(self):
        led, _ = VL.import_axle_payload(VL.empty_ledger(), self.EMAIL)
        led, _ = VL.import_axle_payload(led, self.REAL)
        led, added = VL.import_axle_payload(led, self.EMAIL)
        txs = led["axle"]["transactions"]
        self.assertEqual([t["transaction_id"] for t in txs], ["t-0816"])
        self.assertEqual(txs[0]["settlement_date"], "2026-08-21")
        self.assertEqual(added, 0)

    def test_email_first_on_an_empty_ledger_still_lands(self):
        led, added = VL.import_axle_payload(VL.empty_ledger(), self.EMAIL)
        self.assertEqual(added, 1)

    def test_a_newer_email_still_replaces_an_older_email_row(self):
        led, _ = VL.import_axle_payload(VL.empty_ledger(), self.EMAIL)
        newer = {"transactions": [dict(self.EMAIL["transactions"][0],
                                       transaction_id="email-2026-08-16T19:00:00")]}
        led, _ = VL.import_axle_payload(led, newer)
        self.assertEqual([t["transaction_id"] for t in led["axle"]["transactions"]],
                         ["email-2026-08-16T19:00:00"])


# ================================================================
# 6. One odd reward figure must not lose the whole Saving Sessions fetch
# ================================================================

class TestSavingSessionsRewardTolerant(unittest.TestCase):

    def test_bad_reward_values_do_not_fail_the_fetch(self):
        api = _api()
        api._record_request = lambda: True

        def ev(i, pts):
            return {"id": str(i), "code": f"E{i}", "startAt": "2026-10-10T17:00:00Z",
                    "endAt": "2026-10-10T18:00:00Z", "eventType": "TURN_DOWN",
                    "rewardPerKwhInOctoPoints": pts}
        payload = {"data": {"savingSessions": {
            "account": {"hasJoinedCampaign": True, "tokenBalance": 1, "joinedEvents": []},
            "events": [ev(1, "1600.0"), ev(2, "abc"), ev(3, None), ev(4, 800)]}}}
        with patch.object(oa.requests, "post", return_value=_Resp(payload)):
            got = api.get_saving_sessions(force=True)
        self.assertIsNotNone(got)
        self.assertEqual([e["reward_per_kwh_points"] for e in got["events"]],
                         [1600, 0, 0, 800])


if __name__ == "__main__":
    unittest.main()
