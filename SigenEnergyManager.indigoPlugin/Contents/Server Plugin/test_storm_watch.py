#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    test_storm_watch.py
# Description: Unit tests for storm_watch.check_storm_level — the MeteoAlarm CAP
#              parser. Guards against the exact failure that shipped undetected for
#              weeks: MeteoAlarm dropped the cap:awareness_* schema for cap:event/
#              cap:severity, so the parser silently returned "none" for every real
#              warning. Tests the current schema, the legacy fallback, polygon
#              filtering, the empty feed, and the schema-drift guard. No network —
#              urlopen is patched with checked-in CAP fixtures.
# Author:      CliveS & Claude Opus 4.8
# Date:        26-06-2026
# Version:     1.0

import os
import sys
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import storm_watch   # noqa: E402

# A polygon (CAP "lat,lon lat,lon …") that encloses 54.882, -1.818 (Medomsley)
_COVERS   = "54,-2 55,-2 55,-1 54,-1 54,-2"
# A polygon far to the south — does NOT enclose the test point
_ELSEWHERE = "50,-1 51,-1 51,0 50,0 50,-1"

_ATOM = "http://www.w3.org/2005/Atom"
_CAP  = "urn:oasis:names:tc:emergency:cap:1.2"


def _feed(entries_xml):
    return (
        f'<?xml version="1.0" encoding="UTF-8"?>'
        f'<feed xmlns="{_ATOM}" xmlns:cap="{_CAP}">{entries_xml}</feed>'
    ).encode("utf-8")


def _entry_current(event="Yellow thunderstorm warning", severity="Moderate",
                   polygon=_COVERS):
    poly = f"<cap:polygon>{polygon}</cap:polygon>" if polygon else ""
    sev  = f"<cap:severity>{severity}</cap:severity>" if severity else ""
    return (f"<entry><title>{event}</title>"
            f"<cap:event>{event}</cap:event>{sev}{poly}</entry>")


def _entry_legacy(level_code="3", atype="Wind", polygon=_COVERS):
    poly = f"<cap:polygon>{polygon}</cap:polygon>" if polygon else ""
    return (f"<entry><title>{atype} warning</title>"
            f"<cap:awareness_type>{atype}</cap:awareness_type>"
            f'<cap:awareness_level>{level_code}; Amber; Severe</cap:awareness_level>'
            f"{poly}</entry>")


def _run(xml_bytes, **kwargs):
    cm = MagicMock()
    cm.__enter__.return_value.read.return_value = xml_bytes
    orig = storm_watch.urllib.request.urlopen
    storm_watch.urllib.request.urlopen = lambda req, timeout=None: cm
    try:
        return storm_watch.check_storm_level(**kwargs)
    finally:
        storm_watch.urllib.request.urlopen = orig


class TestStormWatchCurrentSchema(unittest.TestCase):
    """The current MeteoAlarm schema: colour-led cap:event + cap:severity."""

    def test_yellow_thunderstorm_covering_site(self):
        level, reason = _run(_feed(_entry_current()))
        self.assertEqual(level, "yellow")
        self.assertIn("thunderstorm", reason.lower())

    def test_amber_wind_from_event_colour(self):
        level, _ = _run(_feed(_entry_current(event="Amber wind warning",
                                             severity="Severe")))
        self.assertEqual(level, "amber")

    def test_severity_used_when_no_colour_word(self):
        # No colour word in the event -> fall back to cap:severity (Extreme -> red).
        level, _ = _run(_feed(_entry_current(event="Storm Bert wind warning",
                                             severity="Extreme")))
        self.assertEqual(level, "red")

    def test_polygon_elsewhere_is_ignored(self):
        level, _ = _run(_feed(_entry_current(polygon=_ELSEWHERE)),
                        lat=54.882, lon=-1.818)
        self.assertEqual(level, "none")

    def test_non_power_cut_hazard_skipped(self):
        # A fog warning is not a power-cut hazard -> not acted on.
        level, _ = _run(_feed(_entry_current(event="Yellow fog warning")))
        self.assertEqual(level, "none")


class TestStormWatchLegacyAndGuards(unittest.TestCase):
    def test_legacy_awareness_schema_still_classifies(self):
        level, _ = _run(_feed(_entry_legacy(level_code="3")))
        self.assertEqual(level, "amber")

    def test_empty_feed_is_none(self):
        level, reason = _run(_feed(""))
        self.assertEqual(level, "none")
        self.assertNotIn("unrecognised", reason)

    def test_schema_drift_guard_flags_unparseable_feed(self):
        # Entries present but with NO cap:event / awareness_type / title -> the
        # parser can read nothing. v1.5: that is a FAILURE (None), not "none" —
        # the caller holds its previous level instead of dropping a live reserve.
        entry = "<entry><id>urn:x</id><updated>2026-01-01T00:00:00Z</updated></entry>"
        level, reason = _run(_feed(entry + entry))
        self.assertIsNone(level)
        self.assertIn("unrecognised", reason)

    def test_location_name_used_in_reason(self):
        _, reason = _run(_feed(""), location_name="Medomsley")
        self.assertIn("Medomsley", reason)

    def test_network_error_returns_failure_sentinel(self):
        # v1.5: a fetch failure returns None (unknown), NEVER "none" (all-clear) —
        # a flaky poll mid-storm must not release the storm reserve.
        def _boom(req, timeout=None):
            raise OSError("no network")
        orig = storm_watch.urllib.request.urlopen
        storm_watch.urllib.request.urlopen = _boom
        try:
            level, reason = storm_watch.check_storm_level()
        finally:
            storm_watch.urllib.request.urlopen = orig
        self.assertIsNone(level)
        self.assertIn("unavailable", reason)

    def test_xml_parse_error_returns_failure_sentinel(self):
        level, reason = _run(b"this is not xml <<<")
        self.assertIsNone(level)
        self.assertIn("parse error", reason.lower())

    def test_title_only_entries_still_classify(self):
        # v1.5: entries readable ONLY via the atom:title fallback used to be
        # discarded by the drift guard despite carrying a valid warning.
        entry = (f"<entry><title>Amber wind warning for North East England</title>"
                 f"<cap:polygon>{_COVERS}</cap:polygon></entry>")
        level, reason = _run(_feed(entry))
        self.assertEqual(level, "amber")
        self.assertIn("titles only", reason)

    def test_colour_substring_false_positives_rejected(self):
        # "predicted" contains "red", "notice" contains "ice" — word-boundary
        # matching must reject both (no colour word, no hazard word).
        # cap:event present so the entry counts as known-schema (a feed of only
        # unreadable entries would rightly trip the drift guard instead).
        entry = ("<entry><title>Service notice: coverage predicted to improve</title>"
                 "<cap:event>Service notice: coverage predicted to improve</cap:event>"
                 "</entry>")
        level, _ = _run(_feed(entry))
        self.assertEqual(level, "none")

    def test_worst_colour_wins_not_first_found(self):
        # A title mentioning an escalation must classify at the WORST colour.
        entry = (f"<entry><title>Yellow warning upgraded to red wind warning</title>"
                 f"<cap:event>Yellow warning upgraded to red wind warning</cap:event>"
                 f"<cap:polygon>{_COVERS}</cap:polygon></entry>")
        level, _ = _run(_feed(entry))
        self.assertEqual(level, "red")


if __name__ == "__main__":
    unittest.main(verbosity=2)
