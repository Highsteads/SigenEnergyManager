#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    test_vpp_ledger.py
# Description: Contract tests for the VPP earnings ledger
# Author:      CliveS & Claude Opus 5
# Date:        18-08-2026
# Version:     1.0
#
# The cases that matter here are the ones about ABSENCE. An unsettled event and
# an event settled at zero look the same on a tile and mean opposite things, and
# the estate has been bitten by exactly that confusion twice. Most of what
# follows exists to keep them apart.

import json
import os
import tempfile
import unittest

from datetime import datetime, timezone

import vpp_ledger as VL


# Real rows from the Axle account, 18-Aug-2026. Anchored deliberately: a
# fixture invented from the schema tests the schema, not the thing.
AXLE_PAYLOAD = {
    "balance": {
        "current_balance_pence": 8760,
        "total_earnings_pence": 8760,
        "minimum_withdrawal_threshold_pence": 1000,
        "withdrawal_min_wait_days": 0,
        "can_withdraw": True,
    },
    "transactions": [
        {"transaction_type": "flex event", "transaction_id": "t-0814",
         "start_time": "2026-08-14T19:00:00+00:00", "end_time": "2026-08-14T20:00:00+00:00",
         "settlement_date": None, "flex_kwh": -3.838, "credit_pence": 384},
        {"transaction_type": "flex event", "transaction_id": "t-0812",
         "start_time": "2026-08-12T17:00:00+00:00", "end_time": "2026-08-12T19:00:00+00:00",
         "settlement_date": None, "flex_kwh": -7.823, "credit_pence": 782},
        {"transaction_type": "flex period top-up", "transaction_id": "t-jul",
         "start_time": "2026-07-01T00:00:00+00:00", "end_time": "2026-07-31T23:59:00+00:00",
         "settlement_date": None, "flex_kwh": None, "credit_pence": 618},
        {"transaction_type": "flex event", "transaction_id": "t-0420",
         "start_time": "2026-04-20T07:00:00+00:00", "end_time": "2026-04-20T08:00:00+00:00",
         "settlement_date": None, "flex_kwh": 0, "credit_pence": 0},
        {"transaction_type": "Referred Credit", "transaction_id": "t-ref",
         "start_time": "2026-03-19T12:03:00+00:00", "credit_pence": 2500},
    ],
    "events": [
        {"start_time": "2026-08-16T19:00:00+00:00", "end_time": "2026-08-16T20:00:00+00:00",
         "opted_out_at": None, "settled_via": None},
        {"start_time": "2026-08-14T19:00:00+00:00", "end_time": "2026-08-14T20:00:00+00:00",
         "opted_out_at": None, "settled_via": "asset_readings"},
    ],
}

NOW = datetime(2026, 8, 18, 19, 0, tzinfo=timezone.utc)


def _seeded():
    led, _ = VL.import_axle_payload(VL.empty_ledger(), AXLE_PAYLOAD,
                                    fetched_at="2026-08-18T18:00:00+00:00")
    return led


class TestLoadSave(unittest.TestCase):

    def test_missing_file_gives_empty_ledger(self):
        led = VL.load_ledger("/nowhere/at/all/vpp_ledger.json")
        self.assertEqual(led["axle"]["transactions"], [])
        self.assertIsNone(led.get("load_error"))

    def test_corrupt_file_reports_error_rather_than_looking_empty(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "vpp_ledger.json")
            with open(p, "w", encoding="utf-8") as fh:
                fh.write("{not json at all")
            led = VL.load_ledger(p)
            self.assertTrue(led["load_error"])
            # And the summary must carry it, so nothing renders a confident zero.
            self.assertTrue(VL.summarise(led, now=NOW)["load_error"])

    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "vpp_ledger.json")
            VL.save_ledger(p, _seeded())
            back = VL.load_ledger(p)
            self.assertEqual(len(back["axle"]["transactions"]), 5)
            self.assertEqual(back["axle"]["balance"]["total_earnings_pence"], 8760)

    def test_save_leaves_no_temp_files_behind(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "vpp_ledger.json")
            VL.save_ledger(p, _seeded())
            self.assertEqual(sorted(os.listdir(d)), ["vpp_ledger.json"])

    def test_partial_file_does_not_raise(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "vpp_ledger.json")
            with open(p, "w", encoding="utf-8") as fh:
                json.dump({"schema": 1}, fh)
            led = VL.load_ledger(p)
            self.assertEqual(led["local"], [])
            VL.summarise(led, now=NOW)      # must not raise


class TestImport(unittest.TestCase):

    def test_import_counts_new_rows(self):
        led, added = VL.import_axle_payload(VL.empty_ledger(), AXLE_PAYLOAD)
        self.assertEqual(added, 5)

    def test_reimport_is_idempotent(self):
        led = _seeded()
        led, added = VL.import_axle_payload(led, AXLE_PAYLOAD)
        self.assertEqual(added, 0)
        self.assertEqual(len(led["axle"]["transactions"]), 5)

    def test_later_payload_updates_settlement_date(self):
        led = _seeded()
        later = {"transactions": [dict(AXLE_PAYLOAD["transactions"][0],
                                       settlement_date="2026-09-30")]}
        led, added = VL.import_axle_payload(led, later)
        self.assertEqual(added, 0)
        row = [t for t in led["axle"]["transactions"] if t["transaction_id"] == "t-0814"][0]
        self.assertEqual(row["settlement_date"], "2026-09-30")

    def test_partial_payload_does_not_truncate(self):
        led = _seeded()
        led, _ = VL.import_axle_payload(led, {"transactions": []})
        self.assertEqual(len(led["axle"]["transactions"]), 5)

    def test_rejects_non_dict(self):
        with self.assertRaises(ValueError):
            VL.import_axle_payload(VL.empty_ledger(), [1, 2, 3])


class TestLocalRows(unittest.TestCase):

    def test_record_and_upsert(self):
        led = VL.empty_ledger()
        VL.record_local_event(led, "2026-08-16T19:00:00+00:00",
                              "2026-08-16T20:00:00+00:00", 4.32, 1.0, driver="self")
        VL.record_local_event(led, "2026-08-16T19:00:00+00:00",
                              "2026-08-16T20:00:00+00:00", 4.40, 1.0, driver="self")
        self.assertEqual(len(led["local"]), 1)
        self.assertEqual(led["local"][0]["export_kwh"], 4.40)
        self.assertEqual(led["local"][0]["estimate_gbp"], 4.40)

    def test_accepts_datetime_objects(self):
        led = VL.empty_ledger()
        VL.record_local_event(led, datetime(2026, 8, 16, 19, tzinfo=timezone.utc),
                              datetime(2026, 8, 16, 20, tzinfo=timezone.utc), 4.32, 1.0)
        self.assertEqual(len(led["local"]), 1)

    def test_junk_kwh_does_not_raise(self):
        led = VL.empty_ledger()
        VL.record_local_event(led, "2026-08-16T19:00:00+00:00", "", "not a number", "")
        self.assertEqual(led["local"][0]["export_kwh"], 0.0)

    def test_start_time_is_required(self):
        with self.assertRaises(ValueError):
            VL.record_local_event(VL.empty_ledger(), None, None, 1.0, 1.0)


class TestSummaryMoney(unittest.TestCase):

    def test_headline_figures(self):
        s = VL.summarise(_seeded(), now=NOW)
        self.assertEqual(s["lifetime_gbp"], 87.60)
        self.assertEqual(s["available_gbp"], 87.60)
        self.assertEqual(s["withdraw_threshold_gbp"], 10.0)
        self.assertTrue(s["can_withdraw"])

    def test_referral_is_not_counted_as_grid_earnings(self):
        s = VL.summarise(_seeded(), now=NOW)
        self.assertEqual(s["by_kind"]["events_gbp"], 11.66)   # 384 + 782 + 0
        self.assertEqual(s["by_kind"]["top_ups_gbp"], 6.18)
        self.assertEqual(s["by_kind"]["other_gbp"], 25.0)

    def test_month_to_date_uses_settled_rows_only(self):
        s = VL.summarise(_seeded(), now=NOW)
        self.assertEqual(s["month_to_date_gbp"], 11.66)

    def test_month_with_no_settled_rows_is_none_not_zero(self):
        # September: nothing settled. A tile showing GBP 0.00 would claim a
        # month of no earnings when the truth is "nothing has settled yet".
        s = VL.summarise(_seeded(), now=datetime(2026, 9, 3, tzinfo=timezone.utc))
        self.assertIsNone(s["month_to_date_gbp"])

    def test_no_balance_yet_reports_none_not_zero(self):
        s = VL.summarise(VL.empty_ledger(), now=NOW)
        self.assertIsNone(s["lifetime_gbp"])
        self.assertIsNone(s["available_gbp"])


class TestSummaryEvents(unittest.TestCase):

    def test_unsettled_event_is_pending_with_no_amount(self):
        s = VL.summarise(_seeded(), now=NOW)
        aug16 = [e for e in s["events"] if e["start"].startswith("2026-08-16")][0]
        self.assertFalse(aug16["settled"])
        self.assertIsNone(aug16["paid_gbp"])       # NOT 0.0
        self.assertIsNone(aug16["paid_kwh"])

    def test_event_settled_at_zero_is_settled(self):
        # 20 Apr genuinely paid nothing. It is settled, and it is not pending.
        s = VL.summarise(_seeded(), now=NOW)
        apr20 = [e for e in s["events"] if e["start"].startswith("2026-04-20")][0]
        self.assertTrue(apr20["settled"])
        self.assertEqual(apr20["paid_gbp"], 0.0)
        self.assertEqual(apr20["paid_kwh"], 0.0)

    def test_pending_and_settled_counts(self):
        s = VL.summarise(_seeded(), now=NOW)
        self.assertEqual(s["events_pending"], 1)     # 16 Aug
        self.assertEqual(s["events_settled"], 3)     # 14 Aug, 12 Aug, 20 Apr

    def test_top_up_is_not_an_event(self):
        s = VL.summarise(_seeded(), now=NOW)
        self.assertFalse(any(e["start"].startswith("2026-07-01") for e in s["events"]))

    def test_local_row_supplies_our_kwh_and_the_difference(self):
        led = _seeded()
        VL.record_local_event(led, "2026-08-14T19:00:00+00:00",
                              "2026-08-14T20:00:00+00:00", 4.02, 1.0, driver="self")
        s = VL.summarise(led, now=NOW)
        aug14 = [e for e in s["events"] if e["start"].startswith("2026-08-14")][0]
        self.assertEqual(aug14["our_kwh"], 4.02)
        self.assertEqual(aug14["paid_kwh"], 3.838)
        self.assertEqual(aug14["diff_kwh"], 0.182)

    def test_difference_is_none_when_either_side_is_missing(self):
        s = VL.summarise(_seeded(), now=NOW)
        for e in s["events"]:
            if e["our_kwh"] is None or e["paid_kwh"] is None:
                self.assertIsNone(e["diff_kwh"])

    def test_local_only_event_appears_as_pending(self):
        led = VL.empty_ledger()
        VL.record_local_event(led, "2026-08-16T19:00:00+00:00",
                              "2026-08-16T20:00:00+00:00", 4.32, 1.0)
        s = VL.summarise(led, now=NOW)
        self.assertEqual(s["events_total"], 1)
        self.assertFalse(s["events"][0]["settled"])
        self.assertEqual(s["events"][0]["our_kwh"], 4.32)

    def test_events_are_newest_first(self):
        s = VL.summarise(_seeded(), now=NOW)
        starts = [e["start"] for e in s["events"]]
        self.assertEqual(starts, sorted(starts, reverse=True))

    def test_settled_via_is_carried_through(self):
        s = VL.summarise(_seeded(), now=NOW)
        aug14 = [e for e in s["events"] if e["start"].startswith("2026-08-14")][0]
        self.assertEqual(aug14["settled_via"], "asset_readings")


class TestSummaryTimeAndStaleness(unittest.TestCase):

    def test_local_times_are_bst_in_august(self):
        # 19:00Z on 14 Aug is 20:00 BST. Rendering the UTC value would put the
        # event an hour early, and on a late window, on the wrong day.
        s = VL.summarise(_seeded(), now=NOW)
        aug14 = [e for e in s["events"] if e["start"].startswith("2026-08-14")][0]
        self.assertTrue(aug14["start_local"].endswith("20:00"), aug14["start_local"])

    def test_local_times_are_gmt_in_winter(self):
        led = VL.empty_ledger()
        VL.record_local_event(led, "2026-01-14T19:00:00+00:00",
                              "2026-01-14T20:00:00+00:00", 4.0, 1.0)
        s = VL.summarise(led, now=datetime(2026, 1, 15, tzinfo=timezone.utc))
        self.assertTrue(s["events"][0]["start_local"].endswith("19:00"),
                        s["events"][0]["start_local"])

    def test_age_of_the_axle_data_is_reported(self):
        s = VL.summarise(_seeded(), now=NOW)
        self.assertAlmostEqual(s["axle_age_days"], 0.0, places=1)

    def test_never_imported_has_no_age(self):
        self.assertIsNone(VL.summarise(VL.empty_ledger(), now=NOW)["axle_age_days"])

    def test_naive_timestamp_is_read_as_utc(self):
        self.assertEqual(VL._window_key("2026-08-14T19:00:00"), "2026-08-14T19:00")


if __name__ == "__main__":
    unittest.main(verbosity=2)
