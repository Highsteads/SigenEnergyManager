#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    test_axle_email.py
# Description: Tests for the Axle settlement-email parser, built from a REAL
#              specimen (the 16-Aug-2026 event, supplied 05-Sep-2026).
# Author:      CliveS & Claude Opus 5
# Date:        05-09-2026 23:40
# Version:     1.0

import os
import sys
import unittest
from datetime import date, datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import axle_email as AE   # noqa: E402


# The real thing. Wording, punctuation and figures taken verbatim from the
# 16-Aug-2026 settlement mail; the HTML tags are the shape such a mail uses.
# A fixture invented from imagination would only ever test the imagination.
SPECIMEN_SENDER  = "Axle Energy <noreply@axle.energy>"
SPECIMEN_SUBJECT = "Recent grid event - results are in!"
SPECIMEN_AT      = datetime(2026, 8, 19, 16, 32, tzinfo=timezone.utc)
SPECIMEN_BODY = """
<p>Hey,</p>
<p>We&rsquo;ve crunched the numbers - you exported <b>3.87 kWh</b> during the grid event
on <b>Sun 16th August</b> &#127775;. This helped supply cleaner electricity to your
neighbourhood and reduce reliance upon gas peaker plants - nice job!</p>
<div class="tile"><span>Earned</span><h1>&pound;3.87</h1>
<small>3.87 kWh @ &pound;1.00/kWh</small></div>
<div class="tile"><span>gCO2 saved</span><h1>881.3</h1></div>
<p>You earned <b>&pound;3.87</b> - we&rsquo;ve added that to <a href="#">your account</a>.</p>
<p>Thanks for helping out the grid,</p>
"""

# What the plugin itself knows about that night: it drove the window, so it holds
# it to the second. The email only draws the clock times inside a chart image.
WINDOWS = {date(2026, 8, 16): ("2026-08-16T19:00:00+00:00", "2026-08-16T20:00:00+00:00")}


def lookup(d):
    return WINDOWS.get(d)


class TestTheRealSpecimen(unittest.TestCase):

    def _parse(self, **kw):
        args = dict(sender=SPECIMEN_SENDER, subject=SPECIMEN_SUBJECT,
                    body=SPECIMEN_BODY, received_at=SPECIMEN_AT, window_lookup=lookup)
        args.update(kw)
        return AE.parse_settlement_email(**args)

    def test_it_parses(self):
        r = self._parse()
        self.assertEqual(r["note"], "")
        self.assertIsNotNone(r["payload"])

    def test_the_row_matches_what_was_typed_in_by_hand(self):
        """The live ledger already holds this event, entered by hand on the day
        the mail arrived. The parser must reproduce that row exactly, or the
        window-keyed supersede in import_axle_payload has nothing to hang on."""
        tx = self._parse()["payload"]["transactions"][0]
        self.assertEqual(tx["transaction_id"],   "email-2026-08-16T19:00")
        self.assertEqual(tx["transaction_type"], "flex event")
        self.assertEqual(tx["start_time"],       "2026-08-16T19:00:00+00:00")
        self.assertEqual(tx["end_time"],         "2026-08-16T20:00:00+00:00")
        self.assertEqual(tx["flex_kwh"],         -3.87)
        self.assertEqual(tx["credit_pence"],     387)
        self.assertIsNone(tx["settlement_date"])

    def test_the_money_is_whole_pence(self):
        """The specimen's own figure, and it must be an int of whole pence.
        3.87 * 100 happens to be exactly 387.0, so this case cannot tell rounding
        from truncation - see the 1.15 case below, which can."""
        tx = self._parse()["payload"]["transactions"][0]
        self.assertIsInstance(tx["credit_pence"], int)
        self.assertEqual(tx["credit_pence"], 387)

    def test_a_short_event_is_not_banked_a_penny_light(self):
        """3.87 * 100 is exactly 387.0 in binary floating point, so the specimen
        cannot tell rounding from truncation — but 137 money values under GBP 20
        can, and 1.15 * 100 is 114.99999999999999. Truncating would bank 114p for
        a GBP 1.15 event, quietly and for ever. A mutation swapping round() for
        int() survived until this case existed."""
        body = ("you exported 1.15 kWh during the grid event on Sun 16th August. "
                "Earned GBP 1.15. 1.15 kWh @ GBP 1.00/kWh")
        tx = self._parse(body=body)["payload"]["transactions"][0]
        self.assertEqual(tx["credit_pence"], 115)

    def test_a_plain_text_body_parses_too(self):
        """Not every mail client hands over HTML."""
        plain = ("Hey, We've crunched the numbers - you exported 3.87 kWh during the "
                 "grid event on Sun 16th August. Earned GBP 3.87. 3.87 kWh @ GBP 1.00/kWh")
        self.assertEqual(self._parse(body=plain)["payload"]["transactions"][0]["credit_pence"], 387)


class TestItRefusesWhatItShould(unittest.TestCase):
    """The body is untrusted input: anyone can post to the address this is fed
    from, and a parsed figure lands in the earnings record."""

    def _parse(self, **kw):
        args = dict(sender=SPECIMEN_SENDER, subject=SPECIMEN_SUBJECT,
                    body=SPECIMEN_BODY, received_at=SPECIMEN_AT, window_lookup=lookup)
        args.update(kw)
        return AE.parse_settlement_email(**args)

    def test_a_stranger_is_refused_even_with_the_right_subject(self):
        r = self._parse(sender="Axle Energy <noreply@axle-energy.example.com>")
        self.assertIsNone(r["payload"])
        self.assertIn("not an Axle settlement email", r["note"])

    def test_another_axle_mail_is_ignored(self):
        r = self._parse(subject="Your monthly Axle statement")
        self.assertIsNone(r["payload"])

    def test_an_absurd_kwh_figure_is_refused(self):
        r = self._parse(body=SPECIMEN_BODY.replace("3.87 kWh</b> during", "9999 kWh</b> during"))
        self.assertIsNone(r["payload"])
        self.assertIn("plausible range", r["note"])

    def test_an_absurd_money_figure_is_refused_even_when_it_is_consistent(self):
        """A rate high enough to make the money absurd while the kWh stays sane
        and the cross-check still passes — so only the money bound can refuse it.
        Without this case a mutation deleting that bound survives."""
        body = ("you exported 3.87 kWh during the grid event on Sun 16th August. "
                "Earned GBP 3866.13. 3.87 kWh @ GBP 999.00/kWh")
        r = self._parse(body=body)
        self.assertIsNone(r["payload"])
        self.assertIn("plausible range", r["note"])

    def test_figures_that_disagree_are_refused(self):
        """The kWh and the money come from different parts of the mail. If they
        stop agreeing the template has moved and the parse cannot be trusted -
        better to refuse than to bank a number we misread."""
        r = self._parse(body=SPECIMEN_BODY.replace("3.87 kWh @", "9.99 kWh @"))
        self.assertIsNone(r["payload"])
        self.assertIn("disagree", r["note"])

    def test_a_window_we_never_drove_is_refused_and_says_so(self):
        """Never a silent skip. A settlement that quietly fails to land is the
        exact failure this feed exists to end."""
        r = self._parse(window_lookup=lambda d: None)
        self.assertIsNone(r["payload"])
        self.assertIn("no record of driving a window", r["note"])
        self.assertIn("3.87", r["note"])       # the figure is not thrown away

    def test_a_body_with_no_date_is_refused(self):
        r = self._parse(body="you exported 3.87 kWh during the grid event. Earned GBP 3.87")
        self.assertIsNone(r["payload"])
        self.assertIn("no event date", r["note"])

    def test_an_empty_body_is_refused(self):
        self.assertIsNone(self._parse(body="")["payload"])


class TestTheYearTheBodyNeverStates(unittest.TestCase):
    """The body says "Sun 16th August" and never names a year."""

    def test_the_ordinary_case_takes_the_year_from_the_message(self):
        self.assertEqual(
            AE.event_date_from_body("on Sun 16th August", SPECIMEN_AT), date(2026, 8, 16))

    def test_an_event_across_new_year_is_filed_in_the_right_year(self):
        """A settlement mail arrives days after its event, so a date that lands
        in the future belongs to the previous year - the 31st of December
        settled on the 2nd of January. Without this, one event a year is filed
        twelve months out."""
        received = datetime(2027, 1, 2, 10, 0, tzinfo=timezone.utc)
        self.assertEqual(
            AE.event_date_from_body("on Fri 31st December", received), date(2026, 12, 31))

    def test_a_same_day_settlement_is_not_pushed_back_a_year(self):
        received = datetime(2026, 8, 16, 23, 0, tzinfo=timezone.utc)
        self.assertEqual(
            AE.event_date_from_body("on Sun 16th August", received), date(2026, 8, 16))

    def test_an_impossible_date_does_not_raise(self):
        self.assertIsNone(AE.event_date_from_body("on Mon 31st February", SPECIMEN_AT))

    def test_a_missing_timestamp_does_not_raise(self):
        self.assertIsNone(AE.event_date_from_body("on Sun 16th August", None))


class TestTextFlattening(unittest.TestCase):

    def test_tags_become_a_space_rather_than_nothing(self):
        """Stripping tags without leaving a separator welds `<b>3.87</b>kWh`
        into `3.87kWh`, and the kWh pattern then misses it."""
        self.assertIn("3.87 kWh", AE.to_text("<b>3.87</b>kWh"))

    def test_the_pound_entity_survives(self):
        self.assertIn("£1.00", AE.to_text("&pound;1.00"))

    def test_a_none_body_is_empty_not_an_exception(self):
        self.assertEqual(AE.to_text(None), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
