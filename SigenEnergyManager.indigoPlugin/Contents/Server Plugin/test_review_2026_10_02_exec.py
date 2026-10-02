#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    test_review_2026_10_02_exec.py
# Description: Regression tests for the 02-10-2026 review findings in
#              flux_execution.py, storm_watch.py and openmeteo_forecast.py.
#              No sockets, no Indigo, no live files: every forecast object
#              writes into a temp dir and the clock is pinned.
# Author:      CliveS & Claude Opus 5.5
# Date:        02-10-2026
# Version:     1.0

import io
import json
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import openmeteo_forecast as omf
import storm_watch as sw
from flux_execution import FluxExecutor, FluxTarget
from test_flux_execution import Registers

# Same guard as test_openmeteo_forecast: nothing here may reach the live file.
omf.OPTIMISER_FORECAST_FILE = os.path.join(tempfile.mkdtemp(prefix="omf_review_"),
                                           "openmeteo_forecast.json")

LONDON = ZoneInfo("Europe/London")


# ============================================================
# 1. flux_execution — a journal write failure must not hold the claim
# ============================================================

class JournalFailureOnRelease(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.now  = datetime.fromisoformat("2026-10-01T02:00:00+01:00")
        self.regs = Registers()
        self.path = Path(self.tmp.name) / "owner.json"
        self.e    = self._executor()

    def _executor(self):
        return FluxExecutor(self.regs, self.path, baseline_charge_w=10000,
                            baseline_discharge_w=10000,
                            baseline_discharge_cutoff_pct=20., clock=lambda: self.now)

    def _target(self):
        return FluxTarget(self.now, self.now + timedelta(minutes=25), self.now,
                          self.now + timedelta(seconds=30), 3, 6000, 0, 80., 20.)

    def _arm(self):
        self.assertEqual(self.e.step(self._target(), self.now), "released")
        self.assertEqual(self.e.step(self._target(), self.now), "applied")
        self.assertEqual(self.regs.values["mode"], 3)

    def test_a_verified_restore_releases_even_when_the_journal_cannot_be_written(self):
        self._arm()

        def _disk_full():
            raise OSError(28, "No space left on device")
        self.e._save = _disk_full

        self.assertEqual(self.e.step(None, self.now), "released")
        self.assertFalse(self.e.owns_control)
        self.assertEqual(self.regs.values["mode"], 2)
        self.assertEqual(self.regs.values["top"], 100.0)
        self.assertIn("journal", self.e.last_error.lower())

        # And it does not repeat the twelve-write restore on every later tick.
        n = len(self.regs.writes)
        self.assertEqual(self.e.step(None, self.now), "released")
        self.assertEqual(len(self.regs.writes), n)

    def test_a_restart_after_the_unwritten_release_still_reconciles(self):
        """The stale journal still says owns=True. That is diagnostic only: a
        new executor always reconciles before it will start anything."""
        self._arm()
        self.e._save = mock.Mock(side_effect=OSError(13, "Permission denied"))
        self.assertEqual(self.e.step(None, self.now), "released")
        self.assertTrue(json.loads(self.path.read_text())["owns"])   # stale on disk

        e2 = self._executor()
        self.assertTrue(e2.owns_control)
        self.assertEqual(e2.step(self._target(), self.now), "released")
        self.assertEqual(e2.step(self._target(), self.now), "applied")

    def test_a_failed_restore_still_holds_the_claim(self):
        """The fix must not weaken the unverified case."""
        self._arm()
        self.regs.fail = "top"
        self.assertEqual(self.e.step(None, self.now), "pending")
        self.assertTrue(self.e.owns_control)

    def test_a_claim_still_needs_the_journal(self):
        """Disk failure still blocks a START."""
        self.assertEqual(self.e.step(self._target(), self.now), "released")
        self.e._save = mock.Mock(side_effect=OSError(28, "No space left on device"))
        self.assertEqual(self.e.step(self._target(), self.now), "pending")
        self.assertNotEqual(self.regs.values["mode"], 3)


# ============================================================
# 2 + 3. storm_watch — a non-Atom body is unknown; Minor is green
# ============================================================

class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _storm(body):
    with mock.patch.object(sw.urllib.request, "urlopen",
                           lambda req, timeout=15: _Resp(body)):
        return sw.check_storm_level(54.88, -1.82, "home")


_FEED = (b'<feed xmlns="http://www.w3.org/2005/Atom" '
         b'xmlns:cap="urn:oasis:names:tc:emergency:cap:1.2">%s</feed>')


def _entry(event, severity, title=None):
    return (f"<entry><title>{title or event}</title><cap:event>{event}</cap:event>"
            f"<cap:severity>{severity}</cap:severity>"
            f"<cap:expires>2099-01-01T00:00:00+00:00</cap:expires></entry>").encode()


class StormWatchUnknownBody(unittest.TestCase):
    def test_an_xhtml_maintenance_page_is_unknown_not_all_clear(self):
        level, _ = _storm(b"<html><body><p>Service temporarily unavailable</p></body></html>")
        self.assertIsNone(level)

    def test_an_xml_error_document_is_unknown_not_all_clear(self):
        level, _ = _storm(b"<error><code>503</code></error>")
        self.assertIsNone(level)

    def test_a_feed_in_the_wrong_namespace_is_unknown(self):
        level, _ = _storm(b"<feed><entry><title>Wind</title></entry></feed>")
        self.assertIsNone(level)

    def test_an_empty_atom_feed_is_still_a_quiet_day(self):
        level, _ = _storm(_FEED % b"")
        self.assertEqual(level, "none")


class StormWatchMinorIsGreen(unittest.TestCase):
    def test_minor_severity_without_a_colour_word_is_no_warning(self):
        level, _ = _storm(_FEED % _entry("Wind", "Minor", "Wind - no special awareness"))
        self.assertEqual(level, "none")

    def test_moderate_severity_is_still_yellow(self):
        level, _ = _storm(_FEED % _entry("Wind", "Moderate", "Wind"))
        self.assertEqual(level, "yellow")

    def test_a_colour_word_still_wins_over_minor(self):
        level, _ = _storm(_FEED % _entry("Yellow wind warning", "Minor"))
        self.assertEqual(level, "yellow")


# ============================================================
# 4. openmeteo_forecast — the combined fallback obeys the stale ceiling
# ============================================================

def _combined(day, today_kwh=30.0, tomorrow_kwh=41.0, ok=4, total=4):
    tmrw = (datetime.strptime(day, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    return {
        "forecastDate": day, "todayKwh": today_kwh, "tomorrowKwh": tomorrow_kwh,
        "forecastStatus": "OK", "lastUpdate": "12:00:00",
        "arrays_ok": ok, "arrays_total": total,
        "_hourly_p50_today": {f"{day} 12:00:00": 2000},
        "_hourly_p50_tomorrow": {f"{tmrw} 12:00:00": 6000},
        "_hourly_p50_ahead": {}, "aheadDayKwh": {}, "_dawn_times": {},
    }


class _ForecastCase(unittest.TestCase):
    NOW = datetime(2026, 10, 5, 1, 30, tzinfo=LONDON)

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="omf_review_case_")
        self.clock = {"now": self.NOW}
        for patcher in (mock.patch.object(omf, "london_now", lambda: self.clock["now"]),
                        mock.patch.object(omf, "REQUESTS_AVAILABLE", True)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.f = omf.OpenMeteoForecast(self.tmp, logger=mock.MagicMock(),
                                       latitude=54.9, longitude=-1.8,
                                       optimiser_file=os.path.join(self.tmp, "opt.json"))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)


class OpenMeteoStaleFallback(_ForecastCase):
    def _seed(self, day, age_s):
        self.f._cached_forecast = _combined(day)
        self.f._cached_time     = time.time() - age_s

    def test_all_arrays_failing_does_not_serve_an_85_hour_old_forecast_as_ok(self):
        self._seed("2026-10-02", 85.5 * 3600)
        self.f._fetch_all_arrays = mock.Mock(return_value=None)
        r = self.f.fetch_forecast()
        self.assertIn("No data", r["forecastStatus"])
        self.assertEqual(r["correctedTomorrowKwh"], 0.0)

    def test_a_fetch_exception_does_not_serve_an_old_forecast_either(self):
        self._seed("2026-10-02", 85.5 * 3600)
        self.f._fetch_all_arrays = mock.Mock(side_effect=RuntimeError("boom"))
        self.assertIn("No data", self.f.fetch_forecast()["forecastStatus"])

    def test_a_young_cache_from_yesterday_is_not_served_after_midnight(self):
        """Inside the 24 h ceiling but the wrong day: the v5.43 day-shift guard."""
        self._seed("2026-10-04", 2 * 3600)
        self.f._fetch_all_arrays = mock.Mock(return_value=None)
        self.assertIn("No data", self.f.fetch_forecast()["forecastStatus"])

    def test_a_fresh_cache_for_today_is_still_the_fallback(self):
        self._seed("2026-10-05", 3600)
        self.f._fetch_all_arrays = mock.Mock(return_value=None)
        r = self.f.fetch_forecast()
        self.assertEqual(r["forecastStatus"], "OK")
        self.assertEqual(r["tomorrowKwh"], 41.0)

    def test_a_partial_fetch_does_not_fall_back_to_an_old_complete_forecast(self):
        self._seed("2026-10-02", 85.5 * 3600)
        self.f._fetch_all_arrays = mock.Mock(
            return_value=_combined("2026-10-05", 10.0, 12.0, ok=3, total=4))
        r = self.f.fetch_forecast()
        self.assertEqual(r["forecastStatus"], "Partial 3/4")
        self.assertEqual(r["forecastDate"], "2026-10-05")

    def test_a_partial_fetch_still_keeps_a_fresh_complete_forecast(self):
        self._seed("2026-10-05", 3600)
        self.f._fetch_all_arrays = mock.Mock(
            return_value=_combined("2026-10-05", 10.0, 12.0, ok=3, total=4))
        r = self.f.fetch_forecast(force=True)
        self.assertEqual(r["forecastStatus"], "OK")
        self.assertEqual(r["tomorrowKwh"], 41.0)


# ============================================================
# 5. openmeteo_forecast — yesterday's baseline survives a fetch after midnight
# ============================================================

class OpenMeteoBaselineByDate(_ForecastCase):
    def _fetch_at(self, when, day, today_kwh):
        self.clock["now"] = when
        self.f._fetch_all_arrays = mock.Mock(return_value=_combined(day, today_kwh))
        self.f.fetch_forecast(force=True)

    def _records(self):
        p = self.f._accuracy_path()
        return json.load(open(p, encoding="utf-8")) if os.path.exists(p) else []

    def test_a_fetch_just_after_midnight_does_not_lose_yesterdays_record(self):
        self._fetch_at(datetime(2026, 10, 4, 6, 0, tzinfo=LONDON), "2026-10-04", 40.0)
        self._fetch_at(datetime(2026, 10, 5, 0, 0, 5, tzinfo=LONDON), "2026-10-05", 22.0)
        self.f.record_accuracy(31.2, date_str="2026-10-04")
        recs = [r for r in self._records() if r["date"] == "2026-10-04"]
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["forecast_kwh"], 40.0)   # the 06:00 day-ahead figure

    def test_todays_baseline_is_kept_after_yesterday_is_recorded(self):
        self._fetch_at(datetime(2026, 10, 4, 6, 0, tzinfo=LONDON), "2026-10-04", 40.0)
        self._fetch_at(datetime(2026, 10, 5, 0, 0, 5, tzinfo=LONDON), "2026-10-05", 22.0)
        self.f.record_accuracy(31.2, date_str="2026-10-04")
        self.f.record_accuracy(20.0, date_str="2026-10-05")
        recs = {r["date"]: r["forecast_kwh"] for r in self._records()}
        self.assertEqual(recs, {"2026-10-04": 40.0, "2026-10-05": 22.0})

    def test_the_first_fetch_of_a_day_still_wins(self):
        self._fetch_at(datetime(2026, 10, 4, 6, 0, tzinfo=LONDON), "2026-10-04", 40.0)
        self._fetch_at(datetime(2026, 10, 4, 15, 0, tzinfo=LONDON), "2026-10-04", 55.0)
        self.f.record_accuracy(31.2, date_str="2026-10-04")
        self.assertEqual(self._records()[0]["forecast_kwh"], 40.0)

    def test_a_day_is_recorded_once(self):
        self._fetch_at(datetime(2026, 10, 4, 6, 0, tzinfo=LONDON), "2026-10-04", 40.0)
        self.f.record_accuracy(31.2, date_str="2026-10-04")
        self.f.record_accuracy(31.2, date_str="2026-10-04")
        self.assertEqual(len(self._records()), 1)

    def test_baselines_survive_a_restart(self):
        self._fetch_at(datetime(2026, 10, 4, 6, 0, tzinfo=LONDON), "2026-10-04", 40.0)
        self._fetch_at(datetime(2026, 10, 5, 0, 0, 5, tzinfo=LONDON), "2026-10-05", 22.0)
        f2 = omf.OpenMeteoForecast(self.tmp, logger=mock.MagicMock(),
                                   latitude=54.9, longitude=-1.8,
                                   optimiser_file=os.path.join(self.tmp, "opt.json"))
        f2.record_accuracy(31.2, date_str="2026-10-04")
        self.assertEqual([r["forecast_kwh"] for r in self._records()], [40.0])

    def test_the_old_single_slot_file_still_loads(self):
        with open(os.path.join(self.tmp, "morning_baseline.json"), "w",
                  encoding="utf-8") as fh:
            json.dump({"date": "2026-10-04", "forecast_kwh": 38.5}, fh)
        f2 = omf.OpenMeteoForecast(self.tmp, logger=mock.MagicMock(),
                                   latitude=54.9, longitude=-1.8,
                                   optimiser_file=os.path.join(self.tmp, "opt.json"))
        f2.record_accuracy(31.2, date_str="2026-10-04")
        self.assertEqual([r["forecast_kwh"] for r in self._records()], [38.5])

    def test_only_the_last_few_days_are_kept(self):
        for d in range(1, 11):
            self._fetch_at(datetime(2026, 10, d, 6, 0, tzinfo=LONDON),
                           f"2026-10-{d:02d}", 30.0 + d)
        self.f.record_accuracy(31.2, date_str="2026-10-01")
        self.assertEqual(self._records(), [])
        self.f.record_accuracy(31.2, date_str="2026-10-10")
        self.assertEqual([r["forecast_kwh"] for r in self._records()], [40.0])


if __name__ == "__main__":
    unittest.main()
