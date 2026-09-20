#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    test_no_stray_artefacts.py
# Description: The suite must not leave directories behind inside the plugin bundle.
# Author:      CliveS & Claude Opus 5
# Date:        20-09-2026
# Version:     1.0
#
# WHY THIS EXISTS.
#
# On 20-09-2026 the live, installed bundle was found carrying
# `Contents/Server Plugin/MagicMock/mock.getInstallFolderPath()/<object id>/
# Preferences/Plugins/com.clives.indigoplugin.sigenergy-energy-manager` — four of
# them, one per test run, dated 12-09-2026 and still there eight days later. A suite
# run had built a Plugin for real against a mocked `indigo`, `_get_data_dir()` joined
# the stringified mock onto a path, and `os.makedirs` obligingly created it. The
# suite's own working directory is the bundle (`run_tests.py` chdirs there), so the
# junk landed inside the plugin people install.
#
# `_get_data_dir` now refuses a non-path, so that exact route is closed. This file
# guards the CLASS rather than the route: any future test that writes where it should
# not will leave a trace here, and unlike the original it will be noticed the same
# day instead of eight days later.
#
# IT CHECKS RESIDUE, NOT ONLY THIS RUN, and that is deliberate. Test ordering would
# otherwise decide what it can see; residue from any earlier run is exactly the thing
# that sat unnoticed for over a week.

import os
import re
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
BUNDLE = os.path.dirname(os.path.dirname(HERE))        # <name>.indigoPlugin

# A stringified mock, a bare repr, or a path built from one. Deliberately narrow:
# a pattern that fires on ordinary names would be turned off within a week.
SUSPECT = re.compile(r"mock|<.*object at 0x|^None$|\(\)$", re.IGNORECASE)

# Directories that legitimately live in a working tree.
ALLOWED = {".git", "__pycache__", ".pytest_cache", ".ruff_cache", "Packages",
           "MagicMock.egg-info"}


class NoStrayArtefacts(unittest.TestCase):

    def _offenders(self):
        found = []
        for root, dirs, _files in os.walk(BUNDLE):
            dirs[:] = [d for d in dirs if d not in ALLOWED]
            for d in dirs:
                if SUSPECT.search(d):
                    found.append(os.path.relpath(os.path.join(root, d), BUNDLE))
        return sorted(found)

    def test_the_bundle_carries_no_mock_shaped_directories(self):
        offenders = self._offenders()
        self.assertEqual(
            offenders, [],
            "Directories inside the plugin bundle look like they were created from a "
            "mock rather than a real path. A test has written into the bundle. "
            f"Delete them and fix the test that made them: {offenders}")

    def test_the_detector_can_actually_fire(self):
        """A guard nobody has watched fail is not a guard. Build the exact shape the
        live fault left behind, confirm it is caught, and take it away again.

        The probe root carries this process's pid so it cannot collide with real
        residue. The first version reused the real name, found the eight-day-old
        tree already sitting there, and blew up trying to remove a directory that was
        not empty — a probe must never be able to delete something it did not make.
        """
        import shutil
        root = os.path.join(BUNDLE, "Contents", "Server Plugin",
                            f"MagicMock-probe-{os.getpid()}")
        self.assertFalse(os.path.exists(root), "the probe path already exists")
        victim = os.path.join(root, "mock.getInstallFolderPath()")
        os.makedirs(victim)
        try:
            self.assertTrue(
                any(os.path.basename(root) in o for o in self._offenders()),
                "the detector did not notice a directory of the shape that was "
                "actually found in the live bundle")
        finally:
            shutil.rmtree(root, ignore_errors=True)
        self.assertFalse(os.path.exists(root), "the probe was not cleaned up")


if __name__ == "__main__":
    unittest.main()
