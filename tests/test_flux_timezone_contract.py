#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    test_flux_timezone_contract.py
# Description: The Flux cheap-window boundary must land in the same place whether the
#              plugin was handed a zoneinfo zone or a pytz one.
# Author:      CliveS & Claude Opus 5
# Date:        19-09-2026
# Version:     1.0
#
# WHY IT LIVES OUT HERE RATHER THAN BESIDE THE MODULE IT TESTS:
#
# It imports the real pytz to compare the two implementations, and CI runs the bundle
# suite ONCE WITH NO THIRD-PARTY PACKAGES so the pytz-absent fallbacks stay exercised.
# A comparison of two libraries cannot run in the leg that has only one of them, and
# run_tests.py fails on a skip by design. tests/ is run by pytest after
# requirements.txt is installed, so pytz is always present here.
#
# WHAT IT GUARDS. flux_strategy._localize has to branch: pytz needs localize(), and
# zoneinfo must NOT be given one — naive.replace(tzinfo=<pytz zone>) yields that
# zone's LMT, which for London is a minute out every time, for ever. london_time
# prefers zoneinfo and falls back to pytz, so both shapes genuinely reach the plugin.
# The three stamps are the cases that can tell them apart: an ordinary BST day, and
# the two clock changes.

import os
import sys
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_BUNDLE = os.path.join(_REPO, "SigenEnergyManager.indigoPlugin",
                       "Contents", "Server Plugin")
sys.path.insert(0, _BUNDLE)

pytz = pytest.importorskip(
    "pytz", reason="tests/ is the leg that has requirements.txt installed")
import flux_strategy as fs                                          # noqa: E402

STAMPS = (
    "2026-09-16T00:00:00+00:00",     # an ordinary BST day
    "2026-10-25T00:30:00+00:00",     # the morning the clocks go back
    "2027-03-28T00:30:00+00:00",     # the morning they go forward
)


class FluxTimezoneContract(unittest.TestCase):

    def test_zoneinfo_and_pytz_resolve_the_same_cheap_window_start(self):
        for stamp in STAMPS:
            with self.subTest(stamp=stamp):
                now = datetime.fromisoformat(stamp)
                self.assertEqual(
                    fs.next_cheap_start(now, ZoneInfo("Europe/London")),
                    fs.next_cheap_start(now, pytz.timezone("Europe/London")),
                )

    def test_the_pytz_shape_is_the_one_that_needs_localize(self):
        """The premise, asserted rather than assumed. Without this the test above
        could pass while quietly comparing zoneinfo with itself."""
        self.assertTrue(callable(getattr(pytz.timezone("Europe/London"),
                                         "localize", None)))
        self.assertIsNone(getattr(ZoneInfo("Europe/London"), "localize", None))

    def test_replacing_tzinfo_with_a_pytz_zone_really_does_give_lmt(self):
        """Why _localize has to branch at all: London's LMT is a minute out."""
        naive = datetime(2026, 9, 16, 2, 0)
        lmt = naive.replace(tzinfo=pytz.timezone("Europe/London"))
        correct = pytz.timezone("Europe/London").localize(naive, is_dst=True)
        self.assertNotEqual(lmt.utcoffset(), correct.utcoffset())


if __name__ == "__main__":
    unittest.main()
