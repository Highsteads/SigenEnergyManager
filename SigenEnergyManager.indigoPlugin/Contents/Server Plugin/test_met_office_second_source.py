#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    test_met_office_second_source.py
# Description: The Met Office warnings RSS as a second storm source (5.133.0).
#              Parser on a checked-in fixture shaped like the live feed, the
#              region and horizon rules, and the rule that the higher of the
#              two sources wins. No network: the feed is passed in as bytes.
# Author:      CliveS & Claude Haiku 5.5
# Date:        24-10-2026
# Version:     1.0

import os
import sys
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import storm_watch   # noqa: E402
import plugin        # noqa: E402

_RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Met Office warnings for UK</title>
<item><title>Amber warning of wind affecting North East England</title>
<description>Amber warning of wind affecting North East England: Durham, Northumberland valid from 2000 Sat 24 Oct to 2100 Sun 25 Oct</description></item>
<item><title>Yellow warning of wind affecting North East England</title>
<description>Yellow warning of wind affecting North East England: Durham valid from 1100 Sat 24 Oct to 2100 Sun 25 Oct</description></item>
<item><title>Yellow warning of fog affecting Cornwall</title>
<description>Yellow warning of fog affecting Cornwall: Cornwall valid from 0600 Sat 24 Oct to 1200 Sat 24 Oct</description></item>
<item><title>Amber warning of snow and ice affecting Cornwall</title>
<description>Amber warning of snow and ice affecting Cornwall: Cornwall valid from 0600 Wed 28 Oct to 1200 Wed 28 Oct</description></item>
<item><title>Yellow warning of wind affecting Highlands &amp; Eilean Siar</title>
<description>Yellow warning of wind affecting Highlands: Highland valid from 0100 Mon 19 Oct to 0900 Mon 19 Oct</description></item>
</channel></rss>"""

# 08:50 BST on Saturday 24 October 2026 (07:50 UTC)
_NOW = datetime(2026, 10, 24, 7, 50, tzinfo=timezone.utc)


class TestMetOfficeParser(unittest.TestCase):

    def test_the_region_takes_the_highest_colour_in_force(self):
        level, reason = storm_watch.check_met_office_level(
            "North East England", _NOW, xml_bytes=_RSS)
        self.assertEqual(level, "amber")
        self.assertIn("AMBER wind", reason)
        self.assertIn("YELLOW wind", reason)

    def test_a_warning_inside_the_horizon_is_active_at_its_onset_hour(self):
        # Amber starts 20:00 BST = 19:00 UTC, which is inside the 24-hour horizon.
        level, _ = storm_watch.check_met_office_level(
            "North East England", datetime(2026, 10, 24, 18, 30, tzinfo=timezone.utc),
            xml_bytes=_RSS)
        self.assertEqual(level, "amber")

    def test_a_warning_beyond_the_horizon_is_pending_not_active(self):
        level, reason = storm_watch.check_met_office_level("Cornwall", _NOW, xml_bytes=_RSS)
        self.assertEqual(level, "none")
        self.assertIn("not yet active", reason)

    def test_fog_is_not_a_power_cut_hazard(self):
        level, _ = storm_watch.check_met_office_level(
            "Cornwall", datetime(2026, 10, 24, 8, 0, tzinfo=timezone.utc), xml_bytes=_RSS)
        self.assertEqual(level, "none")

    def test_an_expired_warning_is_ignored(self):
        level, _ = storm_watch.check_met_office_level(
            "Highlands & Eilean Siar", _NOW, xml_bytes=_RSS)
        self.assertEqual(level, "none")

    def test_another_region_is_not_this_one(self):
        level, _ = storm_watch.check_met_office_level("Wales", _NOW, xml_bytes=_RSS)
        self.assertEqual(level, "none")

    def test_a_html_page_is_unknown_not_all_clear(self):
        level, reason = storm_watch.check_met_office_level(
            "North East England", _NOW, xml_bytes=b"<html><body>down</body></html>")
        self.assertIsNone(level)
        self.assertIn("non-RSS", reason)

    def test_a_broken_body_is_unknown_not_all_clear(self):
        level, _ = storm_watch.check_met_office_level(
            "North East England", _NOW, xml_bytes=b"<rss><channel>")
        self.assertIsNone(level)


class TestHigherSourceWins(unittest.TestCase):

    def test_met_office_amber_beats_meteoalarm_yellow(self):
        level, reason = plugin._combine_storm_sources(
            ("yellow", "MeteoAlarm YELLOW"), ("amber", "Met Office AMBER"))
        self.assertEqual(level, "amber")
        self.assertEqual(reason, "Met Office AMBER")

    def test_meteoalarm_red_beats_met_office_yellow(self):
        level, _ = plugin._combine_storm_sources(
            ("red", "MeteoAlarm RED"), ("yellow", "Met Office YELLOW"))
        self.assertEqual(level, "red")

    def test_an_unreadable_source_defers_to_the_other(self):
        level, _ = plugin._combine_storm_sources((None, "MeteoAlarm down"), ("yellow", "MO"))
        self.assertEqual(level, "yellow")

    def test_both_unreadable_stays_unknown_so_the_level_is_held(self):
        level, reason = plugin._combine_storm_sources((None, "MA down"), (None, "MO down"))
        self.assertIsNone(level)
        self.assertIn("MA down", reason)

    def test_a_source_not_asked_is_ignored(self):
        level, _ = plugin._combine_storm_sources(("none", "quiet"), None)
        self.assertEqual(level, "none")


class TestWarningStormLevels(unittest.TestCase):

    def test_yellow_counts_as_an_active_storm(self):
        self.assertTrue(plugin._storm_level_active("yellow"))
        self.assertTrue(plugin._storm_level_active("amber"))
        self.assertTrue(plugin._storm_level_active("red"))

    def test_quiet_levels_are_not_a_storm(self):
        for level in ("none", "clear", "", None):
            self.assertFalse(plugin._storm_level_active(level), level)


if __name__ == "__main__":
    unittest.main(verbosity=2)
