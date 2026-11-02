#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    test_free_hour_settled.py
# Description: The free-hour ledger against what Octopus really did from 27-Sep to
#              1-Nov-2026: settled Sundays stay settled, each credit finds its own
#              hour, and a credit whose id changes at billing is not paid twice.
# Author:      CliveS & Claude Opus 5.5
# Date:        02-11-2026
# Version:     1.0

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import free_hour_credits as fh
from test_flux_supervisor import _mk_plugin
import plugin                                     # noqa: E402

UTC = timezone.utc

# id, start (UTC), kWh, the price Octopus published for that half hour
HOURS = [
    ("6540", datetime(2026, 9, 27, 12, tzinfo=UTC), 12.251, 24.35433),
    ("6541", datetime(2026, 9, 27, 13, tzinfo=UTC), 12.992, 24.35433),
    ("6706", datetime(2026, 10, 4, 10, tzinfo=UTC), 11.392, 23.1946),
    ("6707", datetime(2026, 10, 4, 11, tzinfo=UTC), 9.549, 23.1946),
    ("6810", datetime(2026, 10, 11, 12, tzinfo=UTC), 11.026, 23.1946),
    ("6811", datetime(2026, 10, 11, 13, tzinfo=UTC), 5.217, 23.1946),
    ("7244", datetime(2026, 10, 18, 10, tzinfo=UTC), 11.967, 23.1946),
    ("7247", datetime(2026, 10, 18, 13, tzinfo=UTC), 8.463, 23.1946),
    ("7726", datetime(2026, 10, 25, 14, tzinfo=UTC), 4.117, 25.6271),
]

# Every Happy Hour credit on the account, in the order Octopus posted them.
CREDITS = [
    ("3608220286", "2026-10-04", 317), ("3608258710", "2026-10-04", 299),
    ("3660451648", "2026-10-14", 265), ("3660577492", "2026-10-14", 222),
    ("3677413089", "2026-10-18", 122), ("3677866289", "2026-10-18", 256),
    ("3710781562", "2026-10-25", 197), ("3711402793", "2026-10-25", 278),
    ("3744767158", "2026-11-01", 106),
]


def local_day(dt):
    return dt.strftime("%Y-%m-%d")      # every hour here is the same day in both zones


def credit(cid, posted, amount_p):
    return {"id": cid, "posted": posted, "amount_p": amount_p,
            "title": "Weekend Happy Hour", "reason": "WEEKEND_HAPPY_HOUR",
            "reversed": False}


def credits_list():
    return [credit(*c) for c in CREDITS]


def real_ledger(upto=None):
    """Every free hour with its true figures, settled to the meter."""
    led = fh.new_ledger()
    now = datetime(2026, 10, 26, tzinfo=UTC)
    hours = [h for h in HOURS if upto is None or h[1] <= upto]
    fh.record_hours(led, [{"id": h, "start": s, "end": s + timedelta(hours=1), "rate_p": r}
                          for h, s, _k, r in hours], now, local_day)
    for h, _s, k, _r in hours:
        fh.set_meter_kwh(led, h, k)
    return led


class TestTheWorth(unittest.TestCase):

    def test_each_hour_rounds_up_to_the_penny_as_octopus_does(self):
        worth = {h: fh.hour_worth_p({"meter_kwh": k, "rate_p": r}) for h, _s, k, r in HOURS}
        self.assertEqual(sorted(worth.values()),
                         sorted(c[2] for c in CREDITS))

    def test_a_whole_penny_is_not_pushed_up_by_floating_point(self):
        self.assertEqual(fh.hour_worth_p({"meter_kwh": 3.0, "rate_p": 0.1 * 3 * 100 / 3}), 30)

    def test_the_allowance_still_caps_an_hour(self):
        self.assertEqual(fh.hour_worth_p({"meter_kwh": 20.0, "rate_p": 10.0}), 160)

    def test_no_reading_or_no_price_has_no_worth(self):
        self.assertIsNone(fh.hour_worth_p({"meter_kwh": None, "rate_p": 10.0}))
        self.assertIsNone(fh.hour_worth_p({"meter_kwh": 1.0, "rate_p": None}))


class TestEveryCreditFindsItsSunday(unittest.TestCase):

    def test_all_five_sundays_are_paid_in_full(self):
        led = real_ledger()
        fh.assign_credits(led, credits_list())
        paid = {d: sum(c["amount_p"] for c in led["claims"][d]["credits"])
                for d in led["claims"]}
        self.assertEqual(paid, {"2026-09-27": 616, "2026-10-04": 487, "2026-10-11": 378,
                                "2026-10-18": 475, "2026-10-25": 106})
        now = datetime(2026, 11, 2, tzinfo=UTC)
        self.assertEqual({fh.status(c, now)["state"] for c in led["claims"].values()},
                         {fh.STATE_PAID})

    def test_a_credit_for_a_later_sunday_passes_an_older_open_one(self):
        # The fault of 26-10-2026: £4.75 for 18-Oct landed on an older open claim.
        led = real_ledger()
        fh.assign_credits(led, [credit("3710781562", "2026-10-25", 197),
                                credit("3711402793", "2026-10-25", 278)])
        self.assertEqual(led["claims"]["2026-09-27"]["credits"], [])
        self.assertEqual(sorted(c["amount_p"] for c in led["claims"]["2026-10-18"]["credits"]),
                         [197, 278])

    def test_each_credit_names_its_hour(self):
        led = real_ledger()
        fh.assign_credits(led, credits_list())
        hours = {c["amount_p"]: c.get("hour") for c in led["claims"]["2026-09-27"]["credits"]}
        self.assertEqual(hours, {299: "6540", 317: "6541"})

    def test_a_credit_for_an_estimated_day_still_lands(self):
        # A lump sum for a day with no meter reading matches on the day, not the hour.
        led = fh.new_ledger()
        s = datetime(2026, 10, 25, 14, tzinfo=UTC)
        fh.record_hours(led, [{"id": "1", "start": s, "end": s + timedelta(hours=1),
                               "rate_p": 25.0}], s + timedelta(hours=2), local_day)
        fh.add_inverter_kwh(led, "2026-10-25", 4.0)
        fh.assign_credits(led, [credit("x", "2026-11-01", 100)])
        self.assertEqual(len(led["claims"]["2026-10-25"]["credits"]), 1)

    def test_a_small_hour_after_a_big_one_lands_on_its_own_day(self):
        # After the big hour the day is within 5p of paid, but the small hour's credit
        # is still that day's and must not fall on the next open Sunday.
        led = fh.new_ledger()
        a = datetime(2026, 10, 4, 10, tzinfo=UTC)
        b = datetime(2026, 10, 11, 10, tzinfo=UTC)
        fh.record_hours(led, [
            {"id": "a1", "start": a, "end": a + timedelta(hours=1), "rate_p": 20.0},
            {"id": "a2", "start": a + timedelta(hours=1), "end": a + timedelta(hours=2),
             "rate_p": 20.0},
            {"id": "b1", "start": b, "end": b + timedelta(hours=1), "rate_p": 20.0}],
            b + timedelta(days=1), local_day)
        fh.set_meter_kwh(led, "a1", 10.0)
        fh.set_meter_kwh(led, "a2", 0.1)
        fh.set_meter_kwh(led, "b1", 5.0)
        fh.assign_credits(led, [credit("c1", "2026-10-14", 200), credit("c2", "2026-10-14", 2)])
        self.assertEqual(sum(c["amount_p"] for c in led["claims"]["2026-10-04"]["credits"]), 202)
        self.assertEqual(led["claims"]["2026-10-11"]["credits"], [])


class TestAReissuedCreditIsNotNew(unittest.TestCase):

    def test_same_credit_under_a_new_id_is_not_counted_again(self):
        # Octopus replaced these two ids when it billed: 3710781562 -> 3744767156,
        # 3711402793 -> 3744767157. Same amount, same date.
        led = real_ledger()
        fh.assign_credits(led, credits_list())
        billed = [credit("3744767156", "2026-10-25", 197),
                  credit("3744767157", "2026-10-25", 278)] + credits_list()[:6]
        self.assertEqual(fh.assign_credits(led, billed), [])

    def test_two_real_credits_of_the_same_size_on_one_day_both_count(self):
        led = fh.new_ledger()
        s = datetime(2026, 10, 4, 10, tzinfo=UTC)
        fh.record_hours(led, [
            {"id": "a", "start": s, "end": s + timedelta(hours=1), "rate_p": 20.0},
            {"id": "b", "start": s + timedelta(hours=1), "end": s + timedelta(hours=2),
             "rate_p": 20.0}], s + timedelta(days=1), local_day)
        fh.set_meter_kwh(led, "a", 5.0)
        fh.set_meter_kwh(led, "b", 5.0)
        got = fh.assign_credits(led, [credit("1", "2026-10-11", 100), credit("2", "2026-10-11", 100)])
        self.assertEqual(len(got), 2)
        self.assertEqual(fh.assign_credits(led, [credit("3", "2026-10-11", 100),
                                                 credit("4", "2026-10-11", 100)]), [])


class TestASettledSundayStaysSettled(unittest.TestCase):

    def _settled(self):
        led = real_ledger()
        fh.assign_credits(led, credits_list()[:2])
        fh.prune(led, datetime(2026, 10, 5, tzinfo=UTC))
        return led

    def test_a_dropped_paid_claim_is_not_recorded_again(self):
        # The fault of 19-10-2026: 27 Sep came back 22 days later as unpaid, and it
        # was still inside the 30-day look-back, so it would have done so every time.
        led = self._settled()
        later = datetime(2026, 10, 19, 15, tzinfo=UTC)
        fh.prune(led, later + timedelta(days=1))
        self.assertNotIn("2026-09-27", led["claims"])
        hours = [{"id": h, "start": s, "end": s + timedelta(hours=1), "rate_p": 25.6271}
                 for h, s, _k, _r in HOURS[:2]]
        fh.record_hours(led, hours, later + timedelta(days=1), local_day)
        self.assertNotIn("2026-09-27", led["claims"])

    def test_it_is_not_even_looked_up(self):
        led = self._settled()
        fh.prune(led, datetime(2026, 10, 20, tzinfo=UTC))
        now = datetime(2026, 10, 20, tzinfo=UTC)
        self.assertFalse(fh.hour_wanted(led, "6540", HOURS[0][1] + timedelta(hours=1), now))

    def test_the_memory_does_not_grow_for_ever(self):
        led = self._settled()
        fh.prune(led, datetime(2026, 10, 20, tzinfo=UTC))
        self.assertIn("6540", led["settled_hours"])
        fh.prune(led, datetime(2026, 12, 1, tzinfo=UTC))
        self.assertNotIn("6540", led["settled_hours"])

    def test_a_new_hour_and_one_with_no_price_are_wanted(self):
        led = real_ledger()
        now = datetime(2026, 10, 20, tzinfo=UTC)
        end = now - timedelta(hours=1)
        self.assertTrue(fh.hour_wanted(led, "new", end, now))
        led["claims"]["2026-09-27"]["hours"]["6540"]["rate_p"] = None
        self.assertTrue(fh.hour_wanted(led, "6540", HOURS[0][1], now))
        self.assertFalse(fh.hour_wanted(led, "6541", HOURS[1][1], now))

    def test_an_hour_past_the_look_back_is_not_wanted(self):
        led = fh.new_ledger()
        now = datetime(2026, 11, 20, tzinfo=UTC)
        self.assertFalse(fh.hour_wanted(led, "x", now - timedelta(days=31), now))


class TestDisplayedWindows(unittest.TestCase):

    def test_two_separate_hours_are_two_windows(self):
        led = real_ledger()
        row = next(r for r in fh.for_display(led, datetime(2026, 10, 20, tzinfo=UTC))
                   if r["date"] == "2026-10-18")
        self.assertEqual(len(row["windows"]), 2)

    def test_two_adjacent_hours_are_one_window(self):
        led = real_ledger()
        row = next(r for r in fh.for_display(led, datetime(2026, 10, 20, tzinfo=UTC))
                   if r["date"] == "2026-09-27")
        self.assertEqual(len(row["windows"]), 1)


class TestLedgerVersion3(unittest.TestCase):

    def test_a_version_2_ledger_keeps_what_it_knows(self):
        led = {"version": 2, "assigned_credit_ids": ["1"], "claims": {"2026-10-11": {
            "date": "2026-10-11", "closed_at": None, "inverter_kwh": None, "notified": {},
            "hours": {"h": {"start": "2026-10-11T12:00:00+00:00",
                            "end": "2026-10-11T13:00:00+00:00",
                            "rate_p": 23.1946, "meter_kwh": 11.026}},
            "credits": [{"id": "1", "posted": "2026-10-18", "amount_p": 256,
                         "title": "Weekend Happy Hour", "reason": "WEEKEND_HAPPY_HOUR"}]}}}
        self.assertTrue(fh.upgrade(led))
        self.assertEqual(led["version"], fh.LEDGER_VERSION)
        self.assertEqual(led["claims"]["2026-10-11"]["hours"]["h"]["meter_kwh"], 11.026)
        self.assertEqual(fh.assign_credits(led, [credit("9", "2026-10-18", 256)]), [])
        self.assertEqual(led["settled_hours"], {})


class TestThePriceOfAnOldHour(unittest.TestCase):

    def _plugin(self):
        p = _mk_plugin()
        p._flux_import_band_p = MagicMock(return_value=None)
        p._build_tariff_data = MagicMock(return_value=MagicMock(today_rate_p=35.7813))
        p.octopus = MagicMock()
        return p

    def test_an_old_hour_is_priced_from_what_octopus_published_then(self):
        p = self._plugin()
        p.octopus.get_import_rate_at.return_value = 23.1946
        when = datetime.now(UTC) - timedelta(days=25)
        self.assertEqual(plugin.Plugin._import_rate_at(p, when), 23.1946)

    def test_an_old_hour_never_takes_todays_price(self):
        p = self._plugin()
        p.octopus.get_import_rate_at.return_value = None
        when = datetime.now(UTC) - timedelta(days=25)
        self.assertIsNone(plugin.Plugin._import_rate_at(p, when))

    def test_a_flat_rate_still_serves_an_hour_that_has_only_just_ended(self):
        p = self._plugin()
        p.octopus.get_import_rate_at.return_value = None
        when = datetime.now(UTC) - timedelta(hours=2)
        self.assertEqual(plugin.Plugin._import_rate_at(p, when), 35.7813)

    def test_a_settled_hour_costs_no_rate_lookup(self):
        p = _mk_plugin()
        p._import_rate_at = MagicMock(return_value=25.0)
        now = datetime.now(UTC)
        led = fh.new_ledger()
        led["settled_hours"]["e1"] = (now - timedelta(days=5)).strftime("%Y-%m-%d")
        p._save_free_hour_ledger(led)
        p._record_free_hour_claims([{"id": "e1", "start_at": now - timedelta(days=5, hours=1),
                                     "end_at": now - timedelta(days=5), "joined": True,
                                     "direction": "WEEKEND_HAPPY_HOUR"}])
        p._import_rate_at.assert_not_called()
        self.assertEqual(p._load_free_hour_ledger()["claims"], {})


class TestPublishedRate(unittest.TestCase):

    def _api(self, slots):
        import octopus_api
        api = octopus_api.OctopusAPI.__new__(octopus_api.OctopusAPI)
        api.get_current_tariff = MagicMock(return_value={
            "product_code": "FLUX-IMPORT-23-02-14",
            "tariff_code": "E-1R-FLUX-IMPORT-23-02-14-F"})
        api._fetch_rate_schedule = MagicMock(return_value=slots)
        return api

    def test_the_slot_covering_the_moment_wins(self):
        api = self._api([
            {"valid_from": "2026-10-04T01:00:00Z", "valid_to": "2026-10-04T04:00:00Z",
             "value_inc_vat": 13.9223},
            {"valid_from": "2026-10-04T04:00:00Z", "valid_to": "2026-10-04T15:00:00Z",
             "value_inc_vat": 23.1946}])
        when = datetime(2026, 10, 4, 10, tzinfo=UTC)
        self.assertEqual(api.get_import_rate_at(when), 23.1946)

    def test_no_slot_means_no_price(self):
        api = self._api([])
        self.assertIsNone(api.get_import_rate_at(datetime(2026, 10, 4, 10, tzinfo=UTC)))

    def test_no_tariff_means_no_price(self):
        api = self._api([])
        api.get_current_tariff.return_value = None
        self.assertIsNone(api.get_import_rate_at(datetime(2026, 10, 4, 10, tzinfo=UTC)))


if __name__ == "__main__":
    unittest.main()
