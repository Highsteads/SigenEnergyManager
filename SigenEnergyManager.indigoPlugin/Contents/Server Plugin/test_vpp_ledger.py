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
        # The baseline is only claimed when our side covers the SAME hour, so
        # the row has to carry the in-window figure for a difference to mean
        # anything. Without it the run total is displayed and no gap claimed.
        led = _seeded()
        VL.record_local_event(led, "2026-08-14T19:00:00+00:00",
                              "2026-08-14T20:00:00+00:00", 4.23, 1.0,
                              driver="self", window_kwh=4.02)
        s = VL.summarise(led, now=NOW)
        aug14 = [e for e in s["events"] if e["start"].startswith("2026-08-14")][0]
        self.assertEqual(aug14["our_kwh"], 4.02)
        self.assertEqual(aug14["run_kwh"], 4.23)
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


class TestWindowIntegration(unittest.TestCase):
    """The in-window figure — the only one comparable with what Axle settles.

    The plugin's own counter runs from two minutes before the window to two
    minutes after, deliberately, so the full paid hour is captured rather than
    ramped into. That makes it WIDER than Axle's basis, and on 11-Aug-2026 it
    was wider by forty-five minutes because the window never stopped: 7.05 kWh
    recorded for an hour whose export cap allows 4.
    """

    def _flat(self, watts, minutes, step_s=83):
        """A window held at a steady export, sampled at the real cadence.

        The last sample is pinned to the end of the span. Without it the
        83-second cadence leaves up to 82 unmeasured seconds at the tail —
        0.07 kWh at the export cap — which is a hole in the FIXTURE, not in
        the integration. Real runs always have a sample past the window,
        because the driver keeps going for two minutes after it closes.
        """
        end = int(minutes * 60)
        pts = [(t, watts) for t in range(0, end + 1, step_s)]
        if pts[-1][0] != end:
            pts.append((end, watts))
        return pts

    def test_a_flat_hour_at_the_cap_integrates_to_the_cap(self):
        kwh = VL.integrate_window_kwh(self._flat(-4000, 60), 1.0)
        self.assertAlmostEqual(kwh, 4.0, places=1)

    def test_two_hour_window_doubles_it(self):
        kwh = VL.integrate_window_kwh(self._flat(-4000, 120), 2.0)
        self.assertAlmostEqual(kwh, 8.0, places=1)

    def test_export_AFTER_the_window_is_excluded(self):
        # The 11-Aug shape: an hour at the cap, then 45 minutes more.
        samples = self._flat(-4000, 105)
        self.assertAlmostEqual(VL.integrate_window_kwh(samples, 1.0), 4.0, places=1)

    def test_the_lead_in_is_excluded_too(self):
        # The driver starts two minutes early; those minutes are not paid.
        samples = [(t, -4000) for t in range(-120, 3600 + 120, 83)]
        self.assertAlmostEqual(VL.integrate_window_kwh(samples, 1.0), 4.0, places=1)

    def test_importing_minutes_do_not_subtract(self):
        # Half an hour exporting, half an hour importing. The import earned
        # nothing; letting it net off would give 1.0 rather than 2.0.
        samples = [(t, -4000 if t < 1800 else 2000) for t in range(0, 3601, 60)]
        kwh = VL.integrate_window_kwh(samples, 1.0)
        # Bounded rather than exact: the single sample either side of the step
        # makes that one 60-second segment genuinely ambiguous to a trapezoid.
        # The point is that it lands near the exported half, not near 1.0.
        self.assertGreater(kwh, 1.85)
        self.assertLess(kwh, 2.05)

    def test_nothing_to_integrate_is_None_not_zero(self):
        self.assertIsNone(VL.integrate_window_kwh([], 1.0))
        self.assertIsNone(VL.integrate_window_kwh([(0, -4000)], 1.0))

    def test_a_missing_or_daft_duration_is_None(self):
        s = self._flat(-4000, 60)
        self.assertIsNone(VL.integrate_window_kwh(s, None))
        self.assertIsNone(VL.integrate_window_kwh(s, 0))
        self.assertIsNone(VL.integrate_window_kwh(s, -1))

    def test_junk_samples_are_skipped_not_fatal(self):
        samples = [(0, -4000), ("x", "y"), (1800, -4000), (None, None), (3600, -4000)]
        self.assertAlmostEqual(VL.integrate_window_kwh(samples, 1.0), 4.0, places=1)

    def test_unordered_samples_are_sorted(self):
        s = self._flat(-4000, 60)
        self.assertAlmostEqual(VL.integrate_window_kwh(list(reversed(s)), 1.0),
                               VL.integrate_window_kwh(s, 1.0), places=3)


class TestWindowVersusRun(unittest.TestCase):
    """Both figures are kept, and only the comparable one is compared."""

    def _led(self, run_kwh, window_kwh):
        led, _ = VL.import_axle_payload(VL.empty_ledger(), {
            "transactions": [
                {"transaction_id": "t1", "transaction_type": "flex event",
                 "start_time": "2026-08-11T18:30:00+00:00",
                 "end_time": "2026-08-11T19:30:00+00:00",
                 "flex_kwh": -3.801, "credit_pence": 380}]})
        VL.record_local_event(led, "2026-08-11T18:30:00+00:00",
                              "2026-08-11T19:30:00+00:00", run_kwh, 1.0,
                              window_kwh=window_kwh)
        return led

    def test_the_baseline_is_measured_against_the_WINDOW_figure(self):
        # 4.00 in the window against 3.801 paid = the ~0.2 kWh baseline.
        e = VL.summarise(self._led(7.05, 4.00), now=NOW)["events"][0]
        self.assertEqual(e["our_kwh"], 4.0)
        self.assertEqual(e["diff_kwh"], 0.199)

    def test_the_over_run_is_reported_separately_not_as_a_shortfall(self):
        e = VL.summarise(self._led(7.05, 4.00), now=NOW)["events"][0]
        self.assertEqual(e["run_kwh"], 7.05)
        self.assertEqual(e["outside_kwh"], 3.05)

    def test_the_ordinary_two_minute_tails_are_NOT_an_over_run(self):
        e = VL.summarise(self._led(4.23, 4.00), now=NOW)["events"][0]
        self.assertIsNone(e["outside_kwh"])

    def test_a_row_with_no_window_figure_claims_no_baseline(self):
        # Rows recorded before the in-window figure existed. Falling back to
        # the run total for display is fine; subtracting it from Axle's is not.
        e = VL.summarise(self._led(7.05, None), now=NOW)["events"][0]
        self.assertEqual(e["our_kwh"], 7.05)
        self.assertFalse(e["in_window"])
        self.assertIsNone(e["diff_kwh"])
        self.assertIsNone(e["outside_kwh"])


class TestHandEnteredRowIsSuperseded(unittest.TestCase):
    """A row typed in from a settlement email must not double-count later.

    Axle email the result days before the account page catches up, so it is
    reasonable to enter the figure by hand - but a hand row cannot know the
    transaction id Axle will eventually assign, and id-only dedupe would then
    keep both for ever and count that event twice in the lifetime total.
    """

    EMAIL = {"transactions": [{
        "transaction_type": "flex event",
        "transaction_id": "email-2026-08-16T19:00",
        "start_time": "2026-08-16T19:00:00+00:00",
        "end_time": "2026-08-16T20:00:00+00:00",
        "settlement_date": None, "flex_kwh": -3.87, "credit_pence": 387,
    }]}

    REAL = {"transactions": [{
        "transaction_type": "flex event",
        "transaction_id": "t-0816",
        "start_time": "2026-08-16T19:00:00+00:00",
        "end_time": "2026-08-16T20:00:00+00:00",
        "settlement_date": "2026-09-30", "flex_kwh": -3.871, "credit_pence": 387,
    }]}

    def _both(self):
        led, _ = VL.import_axle_payload(VL.empty_ledger(), self.EMAIL)
        led, added = VL.import_axle_payload(led, self.REAL)
        return led, added

    def test_the_real_row_replaces_the_hand_entered_one(self):
        led, added = self._both()
        ids = [t["transaction_id"] for t in led["axle"]["transactions"]]
        self.assertEqual(ids, ["t-0816"])
        # Nothing was ADDED - the event was already known, under another name.
        self.assertEqual(added, 0)

    def test_the_event_is_counted_once(self):
        led, _ = self._both()
        s = VL.summarise(led)
        self.assertEqual(s["by_kind"]["events_gbp"], 3.87)
        self.assertEqual(s["events_settled"], 1)

    def test_a_DIFFERENT_window_is_still_a_new_row(self):
        # The guard collapses one window, not every flex event in sight.
        led, _ = VL.import_axle_payload(VL.empty_ledger(), self.EMAIL)
        other = {"transactions": [dict(self.REAL["transactions"][0],
                                       transaction_id="t-0817",
                                       start_time="2026-08-17T19:00:00+00:00",
                                       end_time="2026-08-17T20:00:00+00:00")]}
        led, added = VL.import_axle_payload(led, other)
        self.assertEqual(added, 1)
        self.assertEqual(len(led["axle"]["transactions"]), 2)

    def test_non_events_are_NOT_collapsed_by_window(self):
        # Two monthly top-ups can legitimately share a start time; neither is
        # a stand-in for the other, and losing one loses real money.
        a = {"transactions": [{"transaction_type": "flex period top-up",
                               "transaction_id": "top-a",
                               "start_time": "2026-07-01T00:00:00+00:00",
                               "credit_pence": 618}]}
        b = {"transactions": [{"transaction_type": "flex period top-up",
                               "transaction_id": "top-b",
                               "start_time": "2026-07-01T00:00:00+00:00",
                               "credit_pence": 400}]}
        led, _ = VL.import_axle_payload(VL.empty_ledger(), a)
        led, added = VL.import_axle_payload(led, b)
        self.assertEqual(added, 1)
        self.assertEqual(len(led["axle"]["transactions"]), 2)


class TestBalanceCanFallBehindItsRows(unittest.TestCase):
    """The headline is Axle's figure; the rows are Axle's rows. They can drift.

    A payload carrying transactions and no balance - which is exactly what a
    settlement email gives you - leaves the stored headline stating one number
    while the table under it sums to another. Live on 19-Aug-2026: headline
    GBP 87.60, rows GBP 91.47, portal agreeing with the rows. Detected and
    reported, never quietly corrected: publishing our arithmetic as Axle's
    settled figure would be the worse fault.
    """

    # NB the shipped AXLE_PAYLOAD is a deliberate 5-row SUBSET of the real 17
    # transactions kept beside the real GBP 87.60 balance, so it does NOT agree
    # with itself - the first cut of these tests assumed it did and failed,
    # which is the fixture doing its job. These cases set a balance that
    # matches their own rows.
    ROWS_GBP = 42.84

    def _agreeing(self):
        led = _seeded()
        led, _ = VL.import_axle_payload(led, {"balance": {
            "current_balance_pence": int(round(self.ROWS_GBP * 100)),
            "total_earnings_pence":  int(round(self.ROWS_GBP * 100))}})
        return led

    def test_agreement_reports_nothing(self):
        s = VL.summarise(self._agreeing())
        self.assertEqual(s["rows_total_gbp"], self.ROWS_GBP)
        self.assertIsNone(s["balance_behind_gbp"])

    def test_a_partial_history_is_itself_a_disagreement(self):
        # The stock fixture: real headline, only some of the rows. Saying so is
        # correct - the rows on show do not account for the money claimed.
        s = VL.summarise(_seeded())
        self.assertEqual(s["balance_behind_gbp"], round(self.ROWS_GBP - 87.60, 2))

    def test_a_transaction_only_import_is_flagged(self):
        led = self._agreeing()
        led, _ = VL.import_axle_payload(led, {"transactions": [{
            "transaction_type": "flex event", "transaction_id": "email-0816",
            "start_time": "2026-08-16T19:00:00+00:00",
            "end_time": "2026-08-16T20:00:00+00:00",
            "flex_kwh": -3.87, "credit_pence": 387}]})
        s = VL.summarise(led)
        self.assertEqual(s["balance_behind_gbp"], 3.87)
        # The headline itself is UNTOUCHED - still Axle's last word.
        self.assertEqual(s["lifetime_gbp"], self.ROWS_GBP)

    def test_importing_the_balance_clears_it(self):
        led = self._agreeing()
        led, _ = VL.import_axle_payload(led, {"transactions": [{
            "transaction_type": "flex event", "transaction_id": "email-0816",
            "start_time": "2026-08-16T19:00:00+00:00",
            "end_time": "2026-08-16T20:00:00+00:00",
            "flex_kwh": -3.87, "credit_pence": 387}]})
        fresh = int(round((self.ROWS_GBP + 3.87) * 100))
        led, _ = VL.import_axle_payload(led, {"balance": {
            "current_balance_pence": fresh, "total_earnings_pence": fresh}})
        s = VL.summarise(led)
        self.assertIsNone(s["balance_behind_gbp"])
        self.assertEqual(s["lifetime_gbp"], round(self.ROWS_GBP + 3.87, 2))

    def test_a_withdrawal_makes_the_check_UNSAFE_so_it_is_skipped(self):
        # (uses the mismatched stock fixture on purpose - the point is that
        # even a real gap is not reported once a withdrawal is in play.)
        # A withdrawal reduces the available balance without reducing lifetime
        # earnings, and this estate has never seen one, so the sign convention
        # is unverified. A check resting on a guess is worse than no check.
        led = _seeded()
        led["axle"]["transactions"].append({
            "transaction_type": "withdrawal", "transaction_id": "w-1",
            "start_time": "2026-08-18T09:00:00+00:00", "credit_pence": -1000})
        s = VL.summarise(led)
        self.assertIsNone(s["balance_behind_gbp"])

    def test_no_balance_at_all_claims_nothing(self):
        led = VL.empty_ledger()
        led, _ = VL.import_axle_payload(led, {"transactions": [{
            "transaction_type": "flex event", "transaction_id": "t-x",
            "start_time": "2026-08-16T19:00:00+00:00", "credit_pence": 387}]})
        s = VL.summarise(led)
        self.assertIsNone(s["lifetime_gbp"])
        self.assertIsNone(s["balance_behind_gbp"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
