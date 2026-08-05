#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    run_tests.py
# Description: Runs the plugin's contract-test suite and fails on a SKIP as well as
#              on a failure or an error. Used by CI and runnable by hand.
# Author:      CliveS & Claude Opus 5
# Date:        05-08-2026
# Version:     1.0
#
# Run it from anywhere:
#     python3 scripts/run_tests.py
#
# WHY A RUNNER RATHER THAN `python -m unittest discover`:
#
# plain unittest exits 0 when tests SKIP, and a skipped test is a test that is not
# testing. On 30-Jul-2026 two tests in this very suite had been quietly skipping on
# "pytz not available" for weeks while reporting OK — and what they would have caught
# was a real bug, a silent UTC fallback in the decision engine that was an hour out
# for the whole of BST (v5.55.3). The `skipped=2` in the summary line had been read
# past more than once. So a skip fails the build here, and anything genuinely not
# applicable should be deleted rather than skipped.
#
# Grepping the verbose output for "skipped" was the other option and is wrong: five
# test METHODS in this suite have "skip" in their names, so the grep matches them.
# Reading the TestResult cannot be fooled that way.

import os
import sys
import unittest

# The suite lives beside the modules it tests and imports them as top-level names
# (`from battery_manager import ...`), so discovery has to start there.
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEST_DIR  = os.path.join(
    REPO_ROOT, "SigenEnergyManager.indigoPlugin", "Contents", "Server Plugin"
)


def main():
    if not os.path.isdir(TEST_DIR):
        print(f"Test directory not found: {TEST_DIR}", file=sys.stderr)
        return 2

    # discover() puts TEST_DIR on sys.path itself, but do it explicitly so the
    # failure mode is an ImportError naming the module rather than an empty run.
    sys.path.insert(0, TEST_DIR)
    os.chdir(TEST_DIR)   # a few tests read fixture paths relative to the module dir

    suite  = unittest.defaultTestLoader.discover(TEST_DIR, pattern="test_*.py")
    result = unittest.TextTestRunner(verbosity=2).run(suite)

    skipped   = len(result.skipped)
    failed    = len(result.failures)
    errored   = len(result.errors)
    unexpectd = len(result.unexpectedSuccesses)

    print(
        f"\n{result.testsRun} tests | {failed} failed | {errored} errors | "
        f"{skipped} skipped | {unexpectd} unexpected successes"
    )

    if skipped:
        print(
            "\nFAIL: tests were SKIPPED. A skipped test is not testing — see the note "
            "at the top of this file. Fix the condition or delete the test:",
            file=sys.stderr,
        )
        for case, reason in result.skipped:
            print(f"  {case.id()} — {reason}", file=sys.stderr)

    # An "expected failure" that starts passing is a stale expectation, so treat an
    # unexpected success as red too rather than letting it sit there unnoticed.
    ok = not (failed or errored or skipped or unexpectd)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
