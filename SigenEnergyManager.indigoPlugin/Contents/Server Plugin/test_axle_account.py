#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    test_axle_account.py
# Description: Tests for the direct Axle account reader — the token harvest, the
#              two fetch layers, the single-fetch decoder and the consistency
#              check that decides whether a decoded payload may be believed.
# Author:      CliveS & Claude Opus 5
# Date:        12-09-2026
# Version:     1.0
#
# The encoding fixtures below are written out INDEX BY INDEX rather than built
# by a helper, so they test the decoder rather than a round trip through an
# encoder of my own. The shape is the real one: verified live against the
# account on 12-09-2026, where it decoded to GBP 99.47 over 20 transactions
# matching the account page exactly.

import base64
import json
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import axle_account as AA   # noqa: E402


def _token(exp_dt, site_id="site-abc"):
    """A JWT-shaped string carrying the two claims the reader uses.

    The signature is never checked here — the server does that — so a
    structurally correct token with a real payload segment is the honest
    fixture. Anything else would be testing a stricter parser than we ship.
    """
    claims = {"sub": "axle-vpp", "internal_site_id": site_id,
              "exp": int(exp_dt.timestamp())}
    raw = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"header.{raw}.signature"


NOW = datetime(2026, 9, 12, 17, 0, tzinfo=timezone.utc)


# ======================================================================
# Picking a token out of the mail
# ======================================================================

class TestNewestLiveToken(unittest.TestCase):

    def _walk(self, *pairs):
        return [(_token(e, s), e, s) for e, s in pairs]

    def test_picks_the_latest_expiry(self):
        got = AA.newest_live_token(now=NOW.timestamp(), iterator=self._walk(
            (NOW + timedelta(days=1), "a"),
            (NOW + timedelta(days=6), "b"),
            (NOW + timedelta(days=3), "c"),
        ))
        self.assertIsNotNone(got)
        self.assertEqual(got[2], "b")

    def test_an_expired_token_is_never_chosen(self):
        got = AA.newest_live_token(now=NOW.timestamp(), iterator=self._walk(
            (NOW - timedelta(days=1), "stale"),
            (NOW + timedelta(hours=2), "live"),
        ))
        self.assertEqual(got[2], "live")

    def test_all_expired_returns_none(self):
        got = AA.newest_live_token(now=NOW.timestamp(), iterator=self._walk(
            (NOW - timedelta(days=1), "a"), (NOW - timedelta(days=9), "b")))
        self.assertIsNone(got)

    def test_nothing_at_all_returns_none(self):
        self.assertIsNone(AA.newest_live_token(now=NOW.timestamp(), iterator=[]))

    def test_a_token_about_to_expire_is_treated_as_dead(self):
        """It has to survive the request it would be used for. Starting one
        that is certain to 401 teaches nothing except that we were slow."""
        got = AA.newest_live_token(now=NOW.timestamp(), iterator=self._walk(
            (NOW + timedelta(seconds=20), "expiring")))
        self.assertIsNone(got)

    def test_a_token_comfortably_live_is_accepted(self):
        got = AA.newest_live_token(now=NOW.timestamp(), iterator=self._walk(
            (NOW + timedelta(minutes=30), "fine")))
        self.assertIsNotNone(got)


class TestJwtClaims(unittest.TestCase):

    def test_reads_the_claims(self):
        c = AA._decode_jwt_claims(_token(NOW, "s1"))
        self.assertEqual(c["internal_site_id"], "s1")

    def test_rubbish_returns_none_rather_than_raising(self):
        for bad in ("", "notatoken", "a.b", "a.!!!.c", "a." + "x" * 9 + ".c"):
            self.assertIsNone(AA._decode_jwt_claims(bad), bad)


# ======================================================================
# Layer 1 — the events feed
# ======================================================================

def _getter(status, text):
    return lambda url, token, timeout: (status, text)


EVENTS_JSON = json.dumps([
    {"event_id": "e1", "start_time": "2026-09-11T17:00:00+00:00",
     "end_time": "2026-09-11T18:00:00+00:00", "settled_via": None,
     "opted_out_at": None, "image_url": "https://example.invalid/x.jpg"},
    {"event_id": "e2", "start_time": "2026-09-07T18:00:00+00:00",
     "end_time": "2026-09-07T19:00:00+00:00", "settled_via": "boundary_meter",
     "opted_out_at": None},
])


class TestFetchEvents(unittest.TestCase):

    def test_happy_path_carries_settled_via(self):
        r = AA.fetch_events("s", "t", getter=_getter(200, EVENTS_JSON))
        self.assertEqual(r["note"], "")
        self.assertEqual(len(r["events"]), 2)
        self.assertIsNone(r["events"][0]["settled_via"])
        self.assertEqual(r["events"][1]["settled_via"], "boundary_meter")

    def test_the_bulky_keys_are_dropped(self):
        r = AA.fetch_events("s", "t", getter=_getter(200, EVENTS_JSON))
        self.assertNotIn("image_url", r["events"][0])

    def test_a_403_is_reported_not_swallowed(self):
        r = AA.fetch_events("s", "t", getter=_getter(
            403, '{"detail":"This token cannot access this endpoint"}'))
        self.assertIsNone(r["events"])
        self.assertIn("403", r["note"])

    def test_unreadable_json_is_reported(self):
        r = AA.fetch_events("s", "t", getter=_getter(200, "<html>nope"))
        self.assertIsNone(r["events"])
        self.assertTrue(r["note"])

    def test_a_json_object_instead_of_a_list_is_refused(self):
        r = AA.fetch_events("s", "t", getter=_getter(200, '{"events":[]}'))
        self.assertIsNone(r["events"])
        self.assertTrue(r["note"])

    def test_rows_without_a_window_are_skipped_not_fatal(self):
        body = json.dumps([{"event_id": "x"},
                           {"start_time": "2026-09-07T18:00:00+00:00",
                            "end_time": "2026-09-07T19:00:00+00:00"}])
        r = AA.fetch_events("s", "t", getter=_getter(200, body))
        self.assertEqual(len(r["events"]), 1)

    def test_a_failure_never_returns_an_empty_note(self):
        """A feed that fails silently is the whole reason this module exists."""
        for status, text in ((0, "boom"), (500, ""), (404, "x"), (200, "{")):
            r = AA.fetch_events("s", "t", getter=_getter(status, text))
            if r["events"] is None:
                self.assertTrue(r["note"], f"{status} produced no note")

    def test_an_empty_list_is_success_not_failure(self):
        """No events is a real answer for a new account, and must not read as
        a broken feed — the absent-versus-empty distinction again."""
        r = AA.fetch_events("s", "t", getter=_getter(200, "[]"))
        self.assertEqual(r["events"], [])
        self.assertEqual(r["note"], "")


# ======================================================================
# The single-fetch decoder
# ======================================================================

# Hand-written index by index. arr[0] is the root; an integer is an index into
# this same array; an object's "_N" key is itself an index giving the key name.
REAL_SHAPE = json.dumps([
    {"_1": 2, "_11": 12},                       # 0  root
    "balance",                                  # 1
    {"_3": 4, "_5": 4},                         # 2  both totals point at one value
    "current_balance_pence",                    # 3
    400,                                        # 4  one row of 400p, so the totals agree
    "total_earnings_pence",                     # 5
    "start_time",                               # 6
    "2026-09-07T18:00:00+00:00",                # 7
    "credit_pence",                             # 8
    400,                                        # 9
    "transaction_type",                         # 10
    "transactions",                             # 11
    [13],                                       # 12
    {"_6": 7, "_8": 9, "_10": 14, "_15": 16},   # 13
    "flex event",                               # 14
    "flex_kwh",                                 # 15
    -4.001,                                     # 16
])


class TestDecodeSingleFetch(unittest.TestCase):

    def test_decodes_the_real_shape(self):
        got = AA.decode_single_fetch(REAL_SHAPE)
        self.assertEqual(got, {
            "balance": {"current_balance_pence": 400, "total_earnings_pence": 400},
            "transactions": [{"start_time": "2026-09-07T18:00:00+00:00",
                              "credit_pence": 400,
                              "transaction_type": "flex event",
                              "flex_kwh": -4.001}],
        })

    def test_negative_sentinels_become_none(self):
        got = AA.decode_single_fetch(json.dumps([{"_1": -5}, "settled_via"]))
        self.assertEqual(got, {"settled_via": None})

    def test_a_cycle_resolves_rather_than_recursing_for_ever(self):
        got = AA.decode_single_fetch(json.dumps([{"_1": 0}, "self"]))
        self.assertEqual(got, {"self": None})

    def test_an_index_past_the_end_is_none(self):
        got = AA.decode_single_fetch(json.dumps([{"_1": 99}, "k"]))
        self.assertEqual(got, {"k": None})

    def test_non_json_raises_valueerror(self):
        with self.assertRaises(ValueError):
            AA.decode_single_fetch("<html>signed out</html>")

    def test_an_empty_array_raises_valueerror(self):
        with self.assertRaises(ValueError):
            AA.decode_single_fetch("[]")

    def test_a_json_object_raises_valueerror(self):
        with self.assertRaises(ValueError):
            AA.decode_single_fetch('{"balance":{}}')


# ======================================================================
# The consistency check — the load-bearing guard
# ======================================================================

def _payload(total, rows):
    return {"balance": {"total_earnings_pence": total},
            "transactions": [{"start_time": "2026-09-07T18:00:00+00:00",
                              "credit_pence": p,
                              "transaction_type": "flex event"} for p in rows]}


class TestValidateAccountPayload(unittest.TestCase):

    def test_a_consistent_payload_is_accepted(self):
        ok, note = AA.validate_account_payload(_payload(800, [400, 400]))
        self.assertTrue(ok)
        self.assertEqual(note, "")

    def test_rows_that_do_not_sum_to_the_total_are_refused(self):
        """The one check a garbled decode cannot satisfy by accident. The two
        figures come from different parts of the payload, so an encoding change
        that mangles either breaks the identity."""
        ok, note = AA.validate_account_payload(_payload(9947, [400]))
        self.assertFalse(ok)
        self.assertIn("9947", note)

    def test_being_one_penny_out_is_still_refused(self):
        ok, _ = AA.validate_account_payload(_payload(801, [400, 400]))
        self.assertFalse(ok)

    def test_no_balance_block_is_refused(self):
        ok, _ = AA.validate_account_payload({"transactions": [{"credit_pence": 1}]})
        self.assertFalse(ok)

    def test_no_transactions_is_refused(self):
        ok, _ = AA.validate_account_payload({"balance": {"total_earnings_pence": 0},
                                             "transactions": []})
        self.assertFalse(ok)

    def test_a_float_total_is_refused(self):
        """Pence are whole numbers. A float here means the decode found a
        different field, and rounding it would bank the mistake."""
        p = _payload(800, [400, 400])
        p["balance"]["total_earnings_pence"] = 800.0
        self.assertFalse(AA.validate_account_payload(p)[0])

    def test_a_boolean_is_not_a_number(self):
        p = _payload(1, [1])
        p["transactions"][0]["credit_pence"] = True
        self.assertFalse(AA.validate_account_payload(p)[0])

    def test_an_absurd_row_is_refused(self):
        ok, note = AA.validate_account_payload(_payload(999999, [999999]))
        self.assertFalse(ok)
        self.assertIn("plausible", note)

    def test_a_row_without_a_start_time_is_refused(self):
        p = _payload(400, [400])
        del p["transactions"][0]["start_time"]
        self.assertFalse(AA.validate_account_payload(p)[0])

    def test_a_withdrawal_skips_the_sum_check(self):
        """A withdrawal reduces the available balance without reducing lifetime
        earnings. This account has never had one, so the sign convention is
        unverified and a check built on a guess is worse than no check."""
        p = _payload(800, [400, 400])
        p["transactions"].append({"start_time": "2026-09-01T00:00:00+00:00",
                                  "credit_pence": -500,
                                  "transaction_type": "withdrawal"})
        self.assertTrue(AA.validate_account_payload(p)[0])

    def test_a_non_dict_is_refused(self):
        for bad in (None, [], "x", 7):
            self.assertFalse(AA.validate_account_payload(bad)[0], repr(bad))


# ======================================================================
# Layer 2 — the account fetch end to end
# ======================================================================

class TestFetchAccount(unittest.TestCase):

    def test_happy_path(self):
        r = AA.fetch_account("s", "t", getter=_getter(200, REAL_SHAPE))
        self.assertEqual(r["note"], "")
        self.assertEqual(r["payload"]["balance"]["total_earnings_pence"], 400)
        self.assertEqual(len(r["payload"]["transactions"]), 1)

    def test_the_token_is_carried_in_the_query_string(self):
        seen = {}

        def spy(url, token, timeout):
            seen["url"] = url
            return (200, REAL_SHAPE)

        AA.fetch_account("SITE", "TOK", getter=spy)
        self.assertIn("token=TOK", seen["url"])
        self.assertIn("siteId=SITE", seen["url"])

    def test_a_signed_out_page_is_reported(self):
        r = AA.fetch_account("s", "t", getter=_getter(200, "<html>sign in</html>"))
        self.assertIsNone(r["payload"])
        self.assertIn("decoded", r["note"])

    def test_a_redirect_to_login_is_reported(self):
        r = AA.fetch_account("s", "t", getter=_getter(302, ""))
        self.assertIsNone(r["payload"])
        self.assertIn("302", r["note"])

    def test_an_inconsistent_decode_is_refused_with_its_reason(self):
        """A decode that yields plausible-looking rubbish must not reach the
        ledger. This is the case that makes layer 1 worth having."""
        bad = json.loads(REAL_SHAPE)
        bad[9] = 123                       # credit_pence no longer sums to the total
        r = AA.fetch_account("s", "t", getter=_getter(200, json.dumps(bad)))
        self.assertIsNone(r["payload"])
        self.assertIn("did not decode cleanly", r["note"])

    def test_events_joined_is_never_synthesised(self):
        """Axle's page computes it in the browser and it is absent from this
        payload. Producing our own would be our arithmetic wearing their name."""
        r = AA.fetch_account("s", "t", getter=_getter(200, REAL_SHAPE))
        self.assertNotIn("events_joined", r["payload"]["balance"])


# ======================================================================
# What the two layers together can say
# ======================================================================

def _ledger(events, txs):
    return {"axle": {"events": events, "transactions": txs}}


class TestSettledWithoutFigures(unittest.TestCase):

    def test_silent_when_every_settled_event_has_a_figure(self):
        led = _ledger(
            [{"start_time": "2026-09-07T18:00:00+00:00", "settled_via": "boundary_meter"}],
            [{"start_time": "2026-09-07T18:00:00+00:00", "transaction_type": "flex event",
              "credit_pence": 400}])
        self.assertEqual(AA.settled_without_figures(led), [])

    def test_reports_a_settled_event_with_no_money(self):
        """The 12-Sep-2026 fault, reproduced: Axle settled it, we hold nothing."""
        led = _ledger(
            [{"start_time": "2026-09-07T18:00:00+00:00", "settled_via": "boundary_meter"},
             {"start_time": "2026-09-05T18:30:00+00:00", "settled_via": "boundary_meter"}],
            [])
        self.assertEqual(AA.settled_without_figures(led),
                         ["2026-09-07T18:00:00+00:00", "2026-09-05T18:30:00+00:00"])

    def test_an_unsettled_event_is_never_reported(self):
        """settled_via None means Axle have not paid it yet, which is the
        normal state of the newest event and is not a fault."""
        led = _ledger(
            [{"start_time": "2026-09-11T17:00:00+00:00", "settled_via": None}], [])
        self.assertEqual(AA.settled_without_figures(led), [])

    def test_a_settled_event_paying_zero_still_counts_as_paid(self):
        """20-Apr-2026 settled at 0.000 kWh and Axle recorded a real 0p row.
        A genuine zero is not a missing figure."""
        led = _ledger(
            [{"start_time": "2026-04-20T07:00:00+00:00", "settled_via": "asset_readings"}],
            [{"start_time": "2026-04-20T07:00:00+00:00", "transaction_type": "flex event",
              "credit_pence": 0}])
        self.assertEqual(AA.settled_without_figures(led), [])

    def test_a_top_up_row_does_not_pay_for_an_event(self):
        led = _ledger(
            [{"start_time": "2026-09-07T18:00:00+00:00", "settled_via": "boundary_meter"}],
            [{"start_time": "2026-09-07T18:00:00+00:00",
              "transaction_type": "flex period top-up", "credit_pence": 618}])
        self.assertEqual(len(AA.settled_without_figures(led)), 1)

    def test_an_empty_ledger_is_not_a_fault(self):
        for led in ({}, None, {"axle": {}}, _ledger([], [])):
            self.assertEqual(AA.settled_without_figures(led), [])

    def test_rubbish_rows_do_not_raise(self):
        led = _ledger([None, "x", {"settled_via": "m"}], [None, 7])
        self.assertEqual(AA.settled_without_figures(led), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
