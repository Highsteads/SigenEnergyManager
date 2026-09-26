#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    test_free_hour_credits.py
# Description: Contract tests for free_hour_credits.py — what Octopus owes back
#              for a booked Weekend Happy Hour, whether it has paid, and the words.
# Author:      CliveS & Claude Opus 5.5
# Date:        26-09-2026
# Version:     1.0

import re
import unittest
from datetime import datetime, timedelta, timezone

import free_hour_credits as fh

UTC = timezone.utc
# Sunday 27-Sep-2026, BST: the first real booked free hours, 1pm and 2pm local.
H1 = (datetime(2026, 9, 27, 12, 0, tzinfo=UTC), datetime(2026, 9, 27, 13, 0, tzinfo=UTC))
H2 = (datetime(2026, 9, 27, 13, 0, tzinfo=UTC), datetime(2026, 9, 27, 14, 0, tzinfo=UTC))
RATE = 24.35


def local_day(dt):
    return (dt + timedelta(hours=1)).strftime("%Y-%m-%d")      # BST, fine for these dates


def ledger_with_hours(now=None, rate=RATE):
    led = fh.new_ledger()
    now = now or datetime(2026, 9, 27, 15, 0, tzinfo=UTC)
    fh.record_hours(led, [{"id": 6540, "start": H1[0], "end": H1[1], "rate_p": rate},
                          {"id": 6541, "start": H2[0], "end": H2[1], "rate_p": rate}],
                    now, local_day)
    return led


def credit(cid, posted, amount_p, reason="FREE_ELECTRICITY_REWARD",
           title="Free Electricity Reward", reversed_=False):
    return {"id": cid, "posted": posted, "amount_p": amount_p, "title": title,
            "reason": reason, "reversed": reversed_}


def day_after(days):
    return H2[1] + timedelta(days=days)


class TestRecording(unittest.TestCase):

    def test_only_ended_hours_are_recorded(self):
        led = fh.new_ledger()
        fh.record_hours(led, [{"id": 1, "start": H1[0], "end": H1[1], "rate_p": RATE},
                              {"id": 2, "start": H2[0], "end": H2[1], "rate_p": RATE}],
                        H1[1] + timedelta(minutes=5), local_day)
        self.assertEqual(list(led["claims"]["2026-09-27"]["hours"]), ["1"])

    def test_both_hours_of_a_sunday_share_one_claim(self):
        led = ledger_with_hours()
        self.assertEqual(list(led["claims"]), ["2026-09-27"])
        self.assertEqual(len(led["claims"]["2026-09-27"]["hours"]), 2)

    def test_a_rate_found_on_the_day_is_never_replaced(self):
        led = ledger_with_hours()
        fh.record_hours(led, [{"id": 6540, "start": H1[0], "end": H1[1], "rate_p": 30.0}],
                        day_after(1), local_day)
        self.assertEqual(led["claims"]["2026-09-27"]["hours"]["6540"]["rate_p"], RATE)

    def test_an_old_hour_is_not_dug_up(self):
        led = fh.new_ledger()
        fh.record_hours(led, [{"id": 5144, "start": datetime(2026, 8, 16, 12, tzinfo=UTC),
                               "end": datetime(2026, 8, 16, 13, tzinfo=UTC), "rate_p": RATE}],
                        datetime(2026, 9, 26, tzinfo=UTC), local_day)
        self.assertEqual(led["claims"], {})

    def test_the_inverter_reading_can_arrive_before_the_hours(self):
        led = fh.new_ledger()
        fh.add_inverter_kwh(led, "2026-09-27", 15.2)
        fh.add_inverter_kwh(led, "2026-09-27", 14.8)
        self.assertEqual(led["claims"]["2026-09-27"]["inverter_kwh"], 30.0)
        self.assertEqual(fh.status(led["claims"]["2026-09-27"], day_after(0))["state"],
                         fh.STATE_MEASURING)

    def test_meter_is_asked_for_only_once_it_has_settled(self):
        led = ledger_with_hours()
        self.assertEqual(fh.hours_needing_meter(led, H2[1] + timedelta(hours=2)), [])
        self.assertEqual(len(fh.hours_needing_meter(led, day_after(1.5))), 2)
        fh.set_meter_kwh(led, 6540, 16.0)
        self.assertEqual([h[0] for h in fh.hours_needing_meter(led, day_after(2))], ["6541"])


class TestMoney(unittest.TestCase):

    def test_an_estimate_before_the_meter_settles(self):
        led = ledger_with_hours()
        fh.add_inverter_kwh(led, "2026-09-27", 30.0)
        c = led["claims"]["2026-09-27"]
        self.assertEqual(fh.claim_energy(c), (30.0, "inverter"))
        self.assertEqual(fh.expected_pence(c), round(30.0 * RATE))

    def test_the_meter_replaces_the_estimate_and_each_hour_is_capped_at_16(self):
        led = ledger_with_hours()
        fh.add_inverter_kwh(led, "2026-09-27", 30.0)
        fh.set_meter_kwh(led, 6540, 17.5)          # over the allowance
        fh.set_meter_kwh(led, 6541, 12.0)
        c = led["claims"]["2026-09-27"]
        self.assertEqual(fh.claim_energy(c), (28.0, "meter"))
        self.assertEqual(fh.expected_pence(c), round(16 * RATE + 12 * RATE))

    def test_no_rate_means_the_amount_is_unknown_not_zero(self):
        led = ledger_with_hours(rate=None)
        fh.set_meter_kwh(led, 6540, 10.0)
        fh.set_meter_kwh(led, 6541, 10.0)
        self.assertIsNone(fh.expected_pence(led["claims"]["2026-09-27"]))
        self.assertEqual(fh.status(led["claims"]["2026-09-27"], day_after(20))["state"],
                         fh.STATE_MEASURING)


class TestWhatCountsAsThePayment(unittest.TestCase):

    def test_recognised(self):
        for c in (credit("a", "2026-09-30", 1),
                  credit("b", "2026-09-30", 1, reason="", title="Weekend Happy Hour"),
                  credit("c", "2026-09-30", 1, reason="SAVING_SESSION_REWARD", title="x")):
            self.assertTrue(fh.is_free_hour_credit(c), c)

    def test_not_recognised(self):
        for c in (credit("a", "2026-09-30", 7, reason="POINTS_REDEEMED_FOR_ACCOUNT_CREDIT",
                         title="Points redeemed for account credit"),
                  credit("b", "2026-09-30", 7922, reason="REVERSED_ACCOUNT_CHARGE", title="Gas"),
                  credit("c", "2026-09-30", 5, reversed_=True),
                  credit("d", "2026-09-30", 5, reason="GOODWILL", title="Goodwill"),
                  # A reversal of a free-electricity credit carries the old title but
                  # takes the money back: never a payment.
                  credit("e", "2026-09-30", 779, reason="REVERSED_ACCOUNT_CHARGE",
                         title="Free Electricity Reward")):
            self.assertFalse(fh.is_free_hour_credit(c), c)


def paid_claim(amounts, meter=(16.0, 16.0)):
    led = ledger_with_hours()
    fh.set_meter_kwh(led, 6540, meter[0])
    fh.set_meter_kwh(led, 6541, meter[1])
    fh.assign_credits(led, [credit(f"c{i}", "2026-10-01", a) for i, a in enumerate(amounts)])
    return led


class TestMatching(unittest.TestCase):

    def test_a_credit_is_never_counted_twice(self):
        led = paid_claim([779])
        again = fh.assign_credits(led, [credit("c0", "2026-10-01", 779)])
        self.assertEqual(again, [])
        self.assertEqual(len(led["claims"]["2026-09-27"]["credits"]), 1)

    def test_a_credit_older_than_the_free_hour_is_not_its_payment(self):
        led = ledger_with_hours()
        fh.set_meter_kwh(led, 6540, 16.0)
        fh.set_meter_kwh(led, 6541, 16.0)
        self.assertEqual(fh.assign_credits(led, [credit("old", "2026-09-20", 779)]), [])

    def test_oldest_claim_first_and_a_paid_claim_takes_nothing_more(self):
        led = paid_claim([779])
        fh.record_hours(led, [{"id": 7000, "start": datetime(2026, 10, 4, 12, tzinfo=UTC),
                               "end": datetime(2026, 10, 4, 13, tzinfo=UTC), "rate_p": RATE}],
                        datetime(2026, 10, 4, 15, tzinfo=UTC), local_day)
        fh.set_meter_kwh(led, 7000, 10.0)
        fh.assign_credits(led, [credit("next", "2026-10-07", 244)])
        self.assertEqual([c["id"] for c in led["claims"]["2026-10-04"]["credits"]], ["next"])
        self.assertEqual(len(led["claims"]["2026-09-27"]["credits"]), 1)

    def test_two_credits_one_per_hour_add_up(self):
        led = paid_claim([390, 389])
        self.assertEqual(fh.status(led["claims"]["2026-09-27"], day_after(4))["state"],
                         fh.STATE_PAID)


class TestStates(unittest.TestCase):

    def _claim(self, meter=(16.0, 16.0)):
        led = ledger_with_hours()
        fh.set_meter_kwh(led, 6540, meter[0])
        fh.set_meter_kwh(led, 6541, meter[1])
        return led, led["claims"]["2026-09-27"]

    def test_awaiting_then_late_at_fourteen_days(self):
        led, c = self._claim()
        self.assertEqual(fh.status(c, day_after(13))["state"], fh.STATE_AWAITING)
        self.assertEqual(fh.status(c, day_after(14))["state"], fh.STATE_LATE)

    def test_paid_within_five_pence(self):
        led = paid_claim([round(32 * RATE) - 5])
        self.assertEqual(fh.status(led["claims"]["2026-09-27"], day_after(3))["state"],
                         fh.STATE_PAID)

    def test_short_by_more_than_five_pence_stays_open_for_the_rest(self):
        led = paid_claim([700])
        c = led["claims"]["2026-09-27"]
        st = fh.status(c, day_after(3))
        self.assertEqual(st["state"], fh.STATE_SHORT)
        self.assertEqual(st["owed_p"], round(32 * RATE) - 700)
        fh.assign_credits(led, [credit("rest", "2026-10-03", 79)])
        self.assertEqual(fh.status(c, day_after(6))["state"], fh.STATE_PAID)

    def test_nothing_used_means_nothing_owed(self):
        led, c = self._claim(meter=(0.0, 0.0))
        self.assertEqual(fh.status(c, day_after(20))["state"], fh.STATE_NOTHING_DUE)


class TestNotifications(unittest.TestCase):

    def test_paid_is_said_once(self):
        led = paid_claim([779])
        first = fh.notifications(led, day_after(4))
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0][0], "Octopus has paid for the free hours")
        self.assertIn("£7.79", first[0][1])
        self.assertIn("32 kWh at 24.4p", first[0][1])
        self.assertIn("Sunday 27 September", first[0][1])
        self.assertEqual(fh.notifications(led, day_after(5)), [])

    def test_late_is_said_once_and_nothing_before_it(self):
        led = ledger_with_hours()
        fh.set_meter_kwh(led, 6540, 16.0)
        fh.set_meter_kwh(led, 6541, 16.0)
        self.assertEqual(fh.notifications(led, day_after(13)), [])
        late = fh.notifications(led, day_after(14))
        self.assertEqual(late[0][0], "Still waiting for the free-hour credit")
        self.assertEqual(fh.notifications(led, day_after(15)), [])

    def test_short_is_said_again_only_when_the_amount_changes(self):
        led = paid_claim([500])
        self.assertEqual(len(fh.notifications(led, day_after(4))), 1)
        self.assertEqual(fh.notifications(led, day_after(5)), [])
        fh.assign_credits(led, [credit("more", "2026-10-03", 100)])
        again = fh.notifications(led, day_after(6))
        self.assertEqual(len(again), 1)
        self.assertIn("short", again[0][0])

    def test_every_message_is_plain_english(self):
        msgs = (fh.notifications(paid_claim([779]), day_after(4))
                + fh.notifications(paid_claim([500]), day_after(4)))
        led = ledger_with_hours()
        fh.set_meter_kwh(led, 6540, 16.0)
        fh.set_meter_kwh(led, 6541, 16.0)
        msgs += fh.notifications(led, day_after(15))
        self.assertEqual(len(msgs), 3)
        for title, body in msgs:
            text = title + body
            self.assertTrue(body.endswith("."), body)
            self.assertNotIn("|", text)
            self.assertNotIn("=", text)
            self.assertTrue(all(ord(ch) < 128 or ch == "£" for ch in text), text)
            self.assertIsNone(re.search(r"\d\.0 kWh", text), text)
            self.assertLessEqual(len(body), 1024)


class TestDisplayAndPruning(unittest.TestCase):

    def test_money_words(self):
        self.assertEqual(fh.money(80), "80p")
        self.assertEqual(fh.money(779), "£7.79")
        self.assertEqual(fh.kwh_words(6.0), "6 kWh")
        self.assertEqual(fh.kwh_words(15.25), "15.2 kWh")

    def test_an_unrecognised_credit_since_the_hour_is_still_shown(self):
        led = ledger_with_hours()
        rows = fh.for_display(led, day_after(3), [
            credit("x", "2026-09-29", 412, reason="MYSTERY", title="Adjustment"),
            credit("y", "2026-09-29", 7, reason="POINTS_REDEEMED_FOR_ACCOUNT_CREDIT",
                   title="Points redeemed"),
            credit("z", "2026-09-20", 9, reason="MYSTERY", title="Before the hour")])
        self.assertEqual([c["title"] for c in rows[0]["other_credits"]], ["Adjustment"])

    def test_paid_is_dropped_after_fourteen_days_but_open_never_is(self):
        led = paid_claim([779])
        fh.prune(led, day_after(4))
        self.assertIn("2026-09-27", led["claims"])
        fh.prune(led, day_after(4) + timedelta(days=15))
        self.assertNotIn("2026-09-27", led["claims"])
        late = ledger_with_hours()
        fh.set_meter_kwh(late, 6540, 16.0)
        fh.set_meter_kwh(late, 6541, 16.0)
        fh.prune(late, day_after(90))
        self.assertIn("2026-09-27", late["claims"])


if __name__ == "__main__":
    unittest.main()
