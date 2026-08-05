#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    test_london_time.py
# Description: Contract tests for london_time — the ONE Europe/London
#              implementation. Pins the DST fold mapping that kept
#              openmeteo_forecast out of the v5.55.3 sweep.
# Author:      CliveS & Claude Opus 5
# Date:        05-08-2026
# Version:     1.0
#
# These assert ABSOLUTE UTC instants, not "the two libraries agree", so every
# case tests something whether pytz is installed or not. There is one extra
# cross-library case that runs only when pytz is importable — and it ASSERTS in
# both branches rather than skipping, because a skipped test is a test that is
# not testing.

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from london_time import london_localise, london_now, london_tz, to_london

# 2026 transitions: clocks forward Sun 29 March, back Sun 25 October.
FALLBACK_AMBIGUOUS = datetime(2026, 10, 25, 1, 30)   # happens twice, locally
SUMMER             = datetime(2026, 8, 5, 12, 0)     # BST, unambiguous
WINTER             = datetime(2026, 1, 5, 12, 0)     # GMT, unambiguous


class TestZoneResolves(unittest.TestCase):
    def test_a_zone_is_available(self):
        """zoneinfo is stdlib from 3.9, so None here means a broken interpreter."""
        self.assertIsNotNone(london_tz())

    def test_london_now_is_aware(self):
        self.assertIsNotNone(london_now().tzinfo)


class TestUnambiguousOffsets(unittest.TestCase):
    """The plain cases. A silent-UTC fallback fails the summer one; a naive
    replace() on a pytz zone fails both with LMT (-00:01 for London)."""

    def test_summer_is_bst(self):
        self.assertEqual(london_localise(SUMMER).utcoffset(), timedelta(hours=1))

    def test_winter_is_gmt(self):
        self.assertEqual(london_localise(WINTER).utcoffset(), timedelta(0))

    def test_offsets_are_whole_hours_never_lmt(self):
        for naive in (SUMMER, WINTER, FALLBACK_AMBIGUOUS):
            off = london_localise(naive).utcoffset()
            self.assertEqual(off.total_seconds() % 3600, 0,
                             f"{naive} gave {off} — LMT is -00:01, not a whole hour")


class TestFallbackFold(unittest.TestCase):
    """THE case this module exists for.

    On 25-Oct-2026, 01:30 local occurs twice: once as BST (00:30 UTC) and again
    an hour later as GMT (01:30 UTC). pytz spells the choice `is_dst=`, zoneinfo
    spells it `fold=`, and there is no `is_dst` keyword in zoneinfo at all — so
    a half-migrated copy breaks this path once a year. Before london_time,
    battery_manager's zoneinfo branch took the FIRST occurrence while its pytz
    branch took the SECOND, so the answer depended on which library was
    installed."""

    def test_default_takes_the_second_gmt_occurrence(self):
        got = london_localise(FALLBACK_AMBIGUOUS).astimezone(timezone.utc)
        self.assertEqual(got, datetime(2026, 10, 25, 1, 30, tzinfo=timezone.utc))
        self.assertEqual(london_localise(FALLBACK_AMBIGUOUS).utcoffset(), timedelta(0))

    def test_prefer_dst_takes_the_first_bst_occurrence(self):
        got = london_localise(FALLBACK_AMBIGUOUS, prefer_dst=True).astimezone(timezone.utc)
        self.assertEqual(got, datetime(2026, 10, 25, 0, 30, tzinfo=timezone.utc))
        self.assertEqual(
            london_localise(FALLBACK_AMBIGUOUS, prefer_dst=True).utcoffset(),
            timedelta(hours=1))

    def test_the_two_branches_are_exactly_an_hour_apart(self):
        first  = london_localise(FALLBACK_AMBIGUOUS, prefer_dst=True)
        second = london_localise(FALLBACK_AMBIGUOUS, prefer_dst=False)
        self.assertEqual(second.astimezone(timezone.utc) - first.astimezone(timezone.utc),
                         timedelta(hours=1))

    def test_prefer_dst_is_ignored_outside_the_ambiguous_hour(self):
        """A flag that changed an unambiguous answer would be a bug of its own."""
        for naive in (SUMMER, WINTER):
            self.assertEqual(london_localise(naive, prefer_dst=True),
                             london_localise(naive, prefer_dst=False))

    def test_pytz_and_zoneinfo_agree_on_both_branches(self):
        """The equivalence itself, when both libraries are present.

        Asserts in BOTH branches — when pytz is absent the zoneinfo answer is
        still pinned against the hardcoded instants above, so this case never
        silently does nothing."""
        try:
            import pytz
        except ImportError:
            self.assertEqual(london_localise(FALLBACK_AMBIGUOUS).utcoffset(), timedelta(0))
            return
        pz = pytz.timezone("Europe/London")
        for prefer_dst, fold in ((False, 1), (True, 0)):
            from zoneinfo import ZoneInfo
            via_pytz = pz.localize(FALLBACK_AMBIGUOUS, is_dst=prefer_dst)
            via_zi   = FALLBACK_AMBIGUOUS.replace(tzinfo=ZoneInfo("Europe/London"), fold=fold)
            self.assertEqual(via_pytz.astimezone(timezone.utc),
                             via_zi.astimezone(timezone.utc),
                             f"is_dst={prefer_dst} must equal fold={fold}")
            self.assertEqual(
                london_localise(FALLBACK_AMBIGUOUS, prefer_dst=prefer_dst).astimezone(timezone.utc),
                via_pytz.astimezone(timezone.utc))


class TestToLondon(unittest.TestCase):
    def test_aware_utc_converts(self):
        dt = datetime(2026, 8, 5, 20, 0, tzinfo=timezone.utc)
        self.assertEqual(to_london(dt).strftime("%Y-%m-%d %H:%M"), "2026-08-05 21:00")

    def test_winter_does_not_over_correct(self):
        """Guards against a fixed +1 'fix' — the four GMT months must not move."""
        dt = datetime(2026, 1, 5, 20, 0, tzinfo=timezone.utc)
        self.assertEqual(to_london(dt).strftime("%Y-%m-%d %H:%M"), "2026-01-05 20:00")

    def test_naive_is_returned_untouched(self):
        """Stamping a zone on a naive value would invent information."""
        naive = datetime(2026, 8, 5, 12, 0)
        self.assertIs(to_london(naive), naive)

    def test_none_is_returned_untouched(self):
        self.assertIsNone(to_london(None))


class TestNoSilentUtcFallback(unittest.TestCase):
    """The behaviour the module exists to guarantee: when the zone cannot be
    resolved, callers are told — they are never handed a UTC value dressed up
    as local."""

    def test_localise_returns_none_when_no_zone(self):
        import london_time
        real = london_time.london_tz
        london_time.london_tz = lambda: None
        try:
            self.assertIsNone(london_time.london_localise(SUMMER))
            self.assertIsNone(london_time.london_now())
        finally:
            london_time.london_tz = real


if __name__ == "__main__":
    unittest.main()
