#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    test_free_hour_plugin.py
# Description: The plugin's wiring of free_hour_credits.py (v5.116.0): claims are
#              started from the Saving Sessions poll, the inverter's reading is kept,
#              the daily check reads the meter and the account, and each piece of
#              news goes out once — held, not lost, in quiet hours.
# Author:      CliveS & Claude Opus 5.5
# Date:        26-09-2026
# Version:     1.0

import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from test_flux_supervisor import _mk_plugin      # the shared fake-plugin harness
import plugin                                     # noqa: E402

UTC = timezone.utc


def _events(ended_hours_ago=3, joined=True, direction="WEEKEND_HAPPY_HOUR"):
    now = datetime.now(UTC)
    s1 = now - timedelta(hours=ended_hours_ago + 2)
    return [{"id": "6540", "start_at": s1, "end_at": s1 + timedelta(hours=1),
             "joined": joined, "direction": direction},
            {"id": "6541", "start_at": s1 + timedelta(hours=1),
             "end_at": s1 + timedelta(hours=2), "joined": joined, "direction": direction}]


def _plugin():
    p = _mk_plugin()
    p._send_pushover = MagicMock()
    p._is_in_quiet_hours = MagicMock(return_value=False)
    p._import_rate_at = MagicMock(return_value=24.35)
    return p


def _ledger(p):
    with open(p._free_hour_ledger_path(), encoding="utf-8") as fh:
        return json.load(fh)


class TestClaimsStart(unittest.TestCase):

    def test_an_ended_booked_free_hour_starts_a_claim(self):
        p = _plugin()
        p.store["last_free_hour_check"] = 123.0
        p._record_free_hour_claims(_events())
        claims = _ledger(p)["claims"]
        self.assertEqual(len(claims), 1)
        hours = list(claims.values())[0]["hours"]
        self.assertEqual(sorted(hours), ["6540", "6541"])
        self.assertEqual(hours["6540"]["rate_p"], 24.35)
        self.assertEqual(p.store["last_free_hour_check"], 0.0, "shown on the next tick")

    def test_unbooked_future_or_other_sessions_start_nothing(self):
        for evs in (_events(joined=False), _events(ended_hours_ago=-5),
                    _events(direction="TURN_DOWN")):
            p = _plugin()
            p._record_free_hour_claims(evs)
            self.assertFalse(os.path.exists(p._free_hour_ledger_path()), evs[0])

    def test_the_end_of_the_import_keeps_the_inverters_reading(self):
        p = _plugin()
        p.store.update({"happy_hour_anchor_kwh": 10.0, "grid_import_daily_kwh": 41.5,
                        "happy_hour_import_active": True, "import_active": True})
        p.modbus = MagicMock()
        p.modbus.connected = True
        p._end_happy_hour_import("test")
        claim = _ledger(p)["claims"][plugin._local_today_str()]
        self.assertEqual(claim["inverter_kwh"], 31.5)


class TestDailyCheck(unittest.TestCase):

    def _ready(self, credits):
        p = _plugin()
        p._record_free_hour_claims(_events(ended_hours_ago=30))
        p.octopus = MagicMock()
        p.octopus.get_import_kwh_between.return_value = 16.0
        p.octopus.get_account_credits.return_value = credits
        return p

    def test_a_matching_credit_is_announced_once(self):
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        p = self._ready([{"id": "c1", "posted": today, "amount_p": 779,
                          "title": "Free Electricity Reward",
                          "reason": "FREE_ELECTRICITY_REWARD", "reversed": False}])
        p._check_free_hour_credits()
        p._send_pushover.assert_called_once()
        self.assertEqual(p._send_pushover.call_args[0][0], "Octopus has paid for the free hours")
        self.assertEqual(p.store["free_hour_credits"][0]["state"], "paid")
        p._check_free_hour_credits()
        p._send_pushover.assert_called_once()

    def test_no_credit_yet_says_nothing_and_shows_the_amount_owed(self):
        p = self._ready([])
        p._check_free_hour_credits()
        p._send_pushover.assert_not_called()
        row = p.store["free_hour_credits"][0]
        self.assertEqual(row["state"], "awaiting")
        self.assertEqual(row["owed_p"], round(32 * 24.35))
        self.assertEqual(row["kwh_source"], "meter")

    def test_news_is_held_through_quiet_hours_not_lost(self):
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        p = self._ready([{"id": "c1", "posted": today, "amount_p": 779,
                          "title": "Free Electricity Reward",
                          "reason": "FREE_ELECTRICITY_REWARD", "reversed": False}])
        p._is_in_quiet_hours.return_value = True
        p._check_free_hour_credits()
        p._send_pushover.assert_not_called()
        p._is_in_quiet_hours.return_value = False
        p._check_free_hour_credits()
        p._send_pushover.assert_called_once()

    def test_a_failed_account_read_changes_nothing(self):
        p = self._ready(None)
        p._check_free_hour_credits()
        p._send_pushover.assert_not_called()
        self.assertEqual(p.store["free_hour_credits"][0]["state"], "awaiting")

    def test_the_check_is_on_the_tick(self):
        src = open(os.path.join(os.path.dirname(plugin.__file__), "plugin.py"),
                   encoding="utf-8").read()
        tick = src[src.index("    def _tick(self, now):"):]
        tick = tick[:tick.index("\n    def ", 10)]
        self.assertIn("self._check_free_hour_credits()", tick)



class TestTheSessionsPollFeedsIt(unittest.TestCase):

    def test_history_points_and_events_are_passed_on(self):
        from test_plugin import TestCheckSavingSessions
        now = datetime.now(UTC)
        recent = {"id": 1, "start": (now - timedelta(days=2)).isoformat()}
        old = {"id": 2, "start": (now - timedelta(days=90)).isoformat()}
        ev = _events()[0]
        stub = TestCheckSavingSessions._Stub({"has_joined": True, "token_balance": 5,
                                              "events": [ev], "history": [recent, old],
                                              "points_balance": 3524})
        plugin.Plugin._check_saving_sessions(stub)
        self.assertEqual(stub.store["octopoints_balance"], 3524)
        self.assertEqual([r["id"] for r in stub.store["saving_sessions_history"]], [1])
        self.assertEqual(stub.free_hour_events, [ev])


if __name__ == "__main__":
    unittest.main()
