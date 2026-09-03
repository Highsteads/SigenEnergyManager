#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    test_version_consistency.py
# Description: Fails when the version signals disagree — the bundle's Info.plist,
#              the README changelog, and the six required Info.plist keys.
# Author:      CliveS & Claude Opus 5
# Date:        31-08-2026
# Version:     1.0
#
# Drift here is silent. Nothing breaks when a README advertises a version the
# plugin no longer is, so it accumulates until somebody trips over it —
# ClaudeBridge's README header sat six releases behind with its changelog correct
# the whole time. ~/bin/estate-check sweeps the estate for this daily; this file
# is the same check at push time, where CI can refuse it.
#
# Written on unittest rather than pytest on purpose: this repo's CI runs the suite
# once with NO third-party packages installed, to keep the pytz-absent fallbacks
# exercised, and a pytest import would fail that run.
#
# Generic apart from the path back to the repo root — it finds the bundle by glob,
# so it drops into another repo with one line changed.

import glob
import os
import plistlib
import re
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
README = os.path.join(REPO, "README.md")

_PLISTS = sorted(glob.glob(os.path.join(REPO, "*.indigoPlugin", "Contents", "Info.plist")))
PLIST = _PLISTS[0] if len(_PLISTS) == 1 else None

# The six keys the Indigo Developer's Guide requires. CFBundleURLTypes is the one
# repos keep missing — it becomes the plugin's "About [PLUGIN]" menu item, and an
# estate sweep on 02-Aug-2026 found five bundles without it.
REQUIRED_KEYS = (
    "PluginVersion",
    "ServerApiVersion",
    "CFBundleDisplayName",
    "CFBundleIdentifier",
    "CFBundleVersion",
    "CFBundleURLTypes",
)


class TestVersionConsistency(unittest.TestCase):

    def setUp(self):
        # A glob that matches nothing makes every assertion below pass vacuously,
        # which is the failure mode this whole file exists to prevent elsewhere.
        self.assertIsNotNone(PLIST, "no single .indigoPlugin bundle found")
        self.assertTrue(os.path.isfile(README), "no README.md at the repo root")
        with open(PLIST, "rb") as fh:
            self.plist = plistlib.load(fh)
        with open(README, encoding="utf-8") as fh:
            self.readme = fh.read()

    def test_every_required_info_plist_key_is_present(self):
        for key in REQUIRED_KEYS:
            with self.subTest(key=key):
                self.assertIn(key, self.plist)
                self.assertTrue(self.plist[key], f"{key} is present but empty")

    def test_plugin_version_is_digits_and_dots_only(self):
        """The Developer's Guide is explicit that "1.0.5b2" is invalid, so a
        `-beta` suffix would be rejected at Plugin Store submission. Betas are
        expressed as GitHub pre-releases, never in the version string."""
        self.assertRegex(str(self.plist["PluginVersion"]), r"^\d+(\.\d+)*$")

    def test_cfbundleversion_is_the_layout_version_not_the_release(self):
        """Jay's clarification, 25-May-2026: CFBundleVersion describes the bundle
        LAYOUT and is controlled by Indigo. Bumping it claims a layout we have not
        targeted."""
        self.assertEqual(str(self.plist["CFBundleVersion"]), "1.0.0")

    def test_the_readme_changelog_leads_with_the_shipped_version(self):
        rows = re.findall(r"^\|\s*(\d+(?:\.\d+)+)\s*\|", self.readme, re.MULTILINE)
        self.assertTrue(rows, "no version rows found in the README changelog")
        self.assertEqual(
            rows[0], str(self.plist["PluginVersion"]),
            f"README changelog leads with {rows[0]} but the bundle ships "
            f"{self.plist['PluginVersion']}")

    def test_the_readme_version_header_matches_the_bundle(self):
        """The header line nothing else watches.

        estate-check reads it, and a reader reads it before anything else on the
        page — but no other check compared it to the bundle, so it could sit
        releases behind with the changelog table perfectly correct. That is how
        ClaudeBridge advertised 2.17.1 while shipping 2.23.0. Added 03-Sep-2026
        after the header itself was added and found to be unwatched: a header no
        test reads is drift waiting to happen.
        """
        found = re.search(r"^\*\*Version:\*\*\s*(\d+(?:\.\d+)+)",
                          self.readme, re.MULTILINE)
        self.assertIsNotNone(
            found, "README.md has no '**Version:** X.Y.Z' header line")
        self.assertEqual(
            found.group(1), str(self.plist["PluginVersion"]),
            f"README header says {found.group(1)} but the bundle ships "
            f"{self.plist['PluginVersion']}")

    def test_the_plugin_py_header_matches_the_bundle(self):
        path = os.path.join(HERE, "plugin.py")
        with open(path, encoding="utf-8") as fh:
            head = fh.read(2000)
        found = re.search(r"^# Version:\s*(\S+)", head, re.MULTILINE)
        self.assertIsNotNone(found, "plugin.py has no '# Version:' header line")
        self.assertEqual(found.group(1), str(self.plist["PluginVersion"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
