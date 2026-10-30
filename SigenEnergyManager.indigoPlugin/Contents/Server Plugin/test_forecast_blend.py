#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    test_forecast_blend.py
# Description: The model blend: scales, gaps, the raw x factor rule, the fall-back to
#              the bias bands, the archive seed and the day-ahead score. No network:
#              requests is replaced with a stub that answers from canned data.
# Author:      CliveS & Claude Opus 5.5
# Date:        30-10-2026
# Version:     1.0

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import forecast_blend as fb            # noqa: E402
import openmeteo_forecast as omf       # noqa: E402
from openmeteo_forecast import OpenMeteoForecast   # noqa: E402

MODELS = fb.BLEND_MODELS
UK, IFS, AIFS = MODELS
ARRAY = [{"name": "Only", "tilt": 30, "azimuth": 0, "kwp": 10.0, "shade": 1.0}]
TODAY = datetime(2026, 10, 30, 12, 0)
DAY_1 = "2026-10-30"
DAY_2 = "2026-10-31"


def _records(days=20, actual=20.0, **forecasts):
    """Records dated before TODAY, each with the same figures."""
    start = datetime(2026, 9, 1)
    return [{"date": (start + timedelta(days=i)).strftime("%Y-%m-%d"), "actual_kwh": actual,
             "source": "seed", "models": dict(forecasts)} for i in range(days)]


def _hours(date, kwh):
    return {f"{date} {h:02d}:00:00": (kwh if 8 <= h <= 15 else 0.0) for h in range(24)}


class TestCalibration(unittest.TestCase):

    def test_too_few_days_gives_no_scale(self):
        self.assertIsNone(fb.calibration_factor(_records(9, **{IFS: 25.0}), IFS))

    def test_enough_days_gives_the_ratio(self):
        got = fb.calibration_factor(_records(10, **{IFS: 25.0}), IFS)
        self.assertAlmostEqual(got, 0.8)

    def test_it_is_a_ratio_of_sums(self):
        rows = _records(10, actual=10.0, **{IFS: 10.0}) + [
            {"date": "2026-10-01", "actual_kwh": 40.0, "models": {IFS: 10.0}}]
        self.assertAlmostEqual(fb.calibration_factor(rows, IFS), 140.0 / 110.0)

    def test_only_the_latest_window_counts(self):
        rows = _records(50, actual=10.0, **{IFS: 20.0})
        for row in rows[:5]:
            row["actual_kwh"] = 1000.0
        self.assertAlmostEqual(fb.calibration_factor(rows, IFS), 0.5)

    def test_clamped(self):
        self.assertEqual(fb.calibration_factor(_records(10, actual=50.0, **{IFS: 10.0}), IFS),
                         fb.CAL_FACTOR_MAX)

    def test_a_day_without_an_actual_does_not_count(self):
        rows = _records(10, actual=0.0, **{IFS: 25.0})
        self.assertIsNone(fb.calibration_factor(rows, IFS))


class TestBlendDay(unittest.TestCase):

    def test_scaled_average_of_the_models(self):
        hourly = {UK: _hours(DAY_1, 1.0), IFS: _hours(DAY_1, 2.0)}
        got = fb.blend_day(hourly, DAY_1, {UK: 2.0, IFS: 0.5})
        self.assertEqual(sorted(got["models"]), sorted([UK, IFS]))
        self.assertAlmostEqual(got["hourly"][f"{DAY_1} 12:00:00"], 1.5)
        self.assertAlmostEqual(got["total"], 12.0)

    def test_a_gap_drops_the_model_rather_than_dimming_the_day(self):
        hourly = {UK: _hours(DAY_1, 1.0), IFS: _hours(DAY_1, 1.0), AIFS: _hours(DAY_1, 9.0)}
        hourly[AIFS][f"{DAY_1} 12:00:00"] = None
        got = fb.blend_day(hourly, DAY_1, {UK: 1.0, IFS: 1.0, AIFS: 1.0})
        self.assertNotIn(AIFS, got["models"])
        self.assertAlmostEqual(got["total"], 8.0)

    def test_an_uncalibrated_model_stays_out(self):
        hourly = {UK: _hours(DAY_1, 1.0), IFS: _hours(DAY_1, 9.0), AIFS: _hours(DAY_1, 1.0)}
        got = fb.blend_day(hourly, DAY_1, {UK: 1.0, AIFS: 1.0})
        self.assertNotIn(IFS, got["models"])

    def test_one_model_is_not_a_blend(self):
        hourly = {UK: _hours(DAY_1, 1.0), IFS: _hours(DAY_1, 2.0)}
        self.assertIsNone(fb.blend_day(hourly, DAY_1, {UK: 1.0}))

    def test_short_day_is_incomplete_but_clock_change_day_is_not(self):
        short = dict(list(_hours(DAY_1, 1.0).items())[:22])
        self.assertEqual(fb.complete_day_totals({UK: short}, DAY_1), {})
        spring = dict(list(_hours(DAY_1, 1.0).items())[:23])
        self.assertIn(UK, fb.complete_day_totals({UK: spring}, DAY_1))


class TestRecords(unittest.TestCase):

    def test_a_seed_never_replaces_a_live_record(self):
        live = {"date": DAY_1, "source": "live", "actual_kwh": 5.0}
        seed = {"date": DAY_1, "source": "seed", "actual_kwh": 9.0}
        self.assertEqual(fb.merge_records([live], [seed]), [live])

    def test_a_live_record_replaces_a_seed(self):
        live = {"date": DAY_1, "source": "live", "actual_kwh": 5.0}
        seed = {"date": DAY_1, "source": "seed", "actual_kwh": 9.0}
        self.assertEqual(fb.merge_records([seed], [live]), [live])

    def test_summary_ignores_seeded_days(self):
        rows = [{"date": DAY_1, "actual_kwh": 9.0, "source": "seed"},
                {"date": DAY_2, "actual_kwh": 10.0, "headline_kwh": 8.0, "source": "live"}]
        self.assertEqual(fb.day_ahead_summary(rows),
                         {"days": 1, "mean_error_kwh": 2.0, "mean_actual_kwh": 10.0})

    def test_summary_with_nothing(self):
        self.assertEqual(fb.day_ahead_summary([])["days"], 0)


class _Response:

    def __init__(self, body, status=200):
        self.status_code = status
        self._body       = body
        self.text        = "stub"

    def json(self):
        return self._body


class _Requests:
    """Answers the raw, the multi-model and the archive calls from canned GTI."""

    class exceptions:
        Timeout = ChunkedEncodingError = ConnectionError = type("E", (Exception,), {})

    def __init__(self, gti):
        self.gti    = gti         # {"raw": W/m2, model: W/m2}, by name
        self.fail   = False
        self.calls  = []

    @staticmethod
    def _times(start, days):
        begin = datetime.strptime(start, "%Y-%m-%d")
        return [(begin + timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M") for h in range(24 * days)]

    def _series(self, value, times):
        return [value if 8 <= int(t[11:13]) <= 15 else 0.0 for t in times]

    def get(self, url, params=None, timeout=None):
        self.calls.append(url)
        if "models" in (params or {}):
            if self.fail:
                return _Response({}, 503)
            models = params["models"].split(",")
            if url == omf.PREVIOUS_RUNS_URL:
                times = self._times(params["start_date"], 40)
                times = [t for t in times if t[:10] <= params["end_date"]]
                hourly = {"time": times}
                for m in models:
                    hourly[f"global_tilted_irradiance_previous_day1_{m}"] = self._series(
                        self.gti[m], times)
                return _Response({"hourly": hourly})
            times = self._times(DAY_1, 6)
            hourly = {"time": times}
            for m in models:
                hourly[f"global_tilted_irradiance_{m}"] = self._series(self.gti[m], times)
            return _Response({"hourly": hourly})
        times = self._times(DAY_1, 6)
        return _Response({"hourly": {"time": times,
                                     "global_tilted_irradiance": self._series(self.gti["raw"], times)}})


class _Base(unittest.TestCase):

    def setUp(self):
        self.dir      = tempfile.mkdtemp()
        self.stub     = _Requests({"raw": 200.0, UK: 200.0, IFS: 300.0, AIFS: 400.0})
        self._saved   = (omf.requests, omf.REQUESTS_AVAILABLE, omf.PERFORMANCE_RATIO)
        omf.requests, omf.REQUESTS_AVAILABLE, omf.PERFORMANCE_RATIO = self.stub, True, 1.0

    def tearDown(self):
        omf.requests, omf.REQUESTS_AVAILABLE, omf.PERFORMANCE_RATIO = self._saved

    def make(self, now=TODAY, models=MODELS, records=None):
        if records is not None:
            with open(os.path.join(self.dir, "openmeteo_model_records.json"), "w",
                      encoding="utf-8") as f:
                json.dump(records, f)
        f = OpenMeteoForecast(self.dir, latitude=54.0, longitude=-1.0, arrays=ARRAY,
                              optimiser_file=os.path.join(self.dir, "optimiser.json"),
                              blend_models=models)
        f._now_local = lambda: now
        return f


class TestBlendedForecast(_Base):
    """Eight hours at 200/300/400 W/m2 on 10 kWp: raw 16 kWh, models 16/24/32."""

    SCALED = _records(20, actual=16.0, **{UK: 16.0, IFS: 24.0, AIFS: 32.0})

    def test_corrected_is_the_blend_and_raw_is_untouched(self):
        got = self.make(records=self.SCALED).fetch_forecast(force=True)
        self.assertEqual(got["todayKwh"], 16.0)
        self.assertEqual(got["correctedTodayKwh"], 16.0)  # every model scales to 16
        self.assertEqual(got["tomorrowKwh"], 16.0)

    def test_scales_are_used_not_an_average_of_raw_models(self):
        records = _records(20, actual=20.0, **{UK: 16.0, IFS: 24.0, AIFS: 32.0})
        got = self.make(records=records).fetch_forecast(force=True)
        self.assertAlmostEqual(got["correctedTodayKwh"], 20.0)
        self.assertAlmostEqual(got["biasFactorToday"], 1.25)

    def test_raw_times_factor_gives_the_corrected_total(self):
        records = _records(20, actual=20.0, **{UK: 16.0, IFS: 24.0, AIFS: 32.0})
        got = self.make(records=records).fetch_forecast(force=True)
        total = sum(got["_hourly_p50_today"].values()) / 1000.0
        self.assertAlmostEqual(total, got["todayKwh"], places=1)
        self.assertAlmostEqual(total * got["biasFactorToday"], got["correctedTodayKwh"], places=1)

    def test_remaining_before_dawn_equals_corrected(self):
        records = _records(20, actual=20.0, **{UK: 16.0, IFS: 24.0, AIFS: 32.0})
        f = self.make(now=TODAY.replace(hour=3), records=records)
        got = f.fetch_forecast(force=True)
        self.assertAlmostEqual(got["remainingTodayKwh"], got["correctedTodayKwh"], delta=0.1)

    def test_cached_serve_keeps_the_blend(self):
        records = _records(20, actual=20.0, **{UK: 16.0, IFS: 24.0, AIFS: 32.0})
        f = self.make(records=records)
        f.fetch_forecast(force=True)
        self.assertAlmostEqual(f.fetch_forecast()["correctedTodayKwh"], 20.0)

    def test_optimiser_file_carries_the_blend(self):
        records = _records(20, actual=20.0, **{UK: 16.0, IFS: 24.0, AIFS: 32.0})
        f = self.make(records=records)
        f.fetch_forecast(force=True)
        with open(f.optimiser_file, encoding="utf-8") as fh:
            doc = json.load(fh)
        self.assertAlmostEqual(doc["today_kwh"], 20.0, places=1)
        self.assertAlmostEqual(sum(v["kwh"] for v in doc["hourly"].values()), 40.0, places=1)

    def test_accuracy_records_stay_on_the_raw_figure(self):
        records = _records(20, actual=20.0, **{UK: 16.0, IFS: 24.0, AIFS: 32.0})
        f = self.make(records=records)
        f.fetch_forecast(force=True)
        self.assertEqual(f._morning_baselines[DAY_1], 16.0)


class TestFallsBackToBands(_Base):

    def test_model_fetch_failure_uses_the_bands(self):
        self.stub.fail = True
        records = _records(20, actual=20.0, **{UK: 16.0, IFS: 24.0, AIFS: 32.0})
        f = self.make(records=records)
        f._correction_bands = [(c, 0.5) for c, _ in f._correction_bands]
        got = f.fetch_forecast(force=True)
        self.assertEqual(got["forecastStatus"], "OK")
        self.assertEqual(got["correctedTodayKwh"], 8.0)

    def test_failure_with_a_recent_cache_keeps_blending(self):
        records = _records(20, actual=20.0, **{UK: 16.0, IFS: 24.0, AIFS: 32.0})
        f = self.make(records=records)
        f.fetch_forecast(force=True)
        path = os.path.join(self.dir, "openmeteo_models_Only.json")
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
        doc["cached_time"] -= omf.CACHE_TTL + 1
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
        self.stub.fail = True
        self.assertAlmostEqual(f.fetch_forecast(force=True)["correctedTodayKwh"], 20.0)

    def test_blend_off_never_calls_for_models(self):
        got = self.make(models=()).fetch_forecast(force=True)
        self.assertEqual(got["blendModels"], [])
        self.assertTrue(all(c == omf.OPENMETEO_URL for c in self.stub.calls))
        self.assertEqual(len(self.stub.calls), 1)


class TestSeed(_Base):

    def _accuracy(self, days=20, actual=8.0):
        start = datetime(2026, 10, 1)
        rows = [{"date": (start + timedelta(days=i)).strftime("%Y-%m-%d"), "actual_kwh": actual,
                 "forecast_kwh": 8.0} for i in range(days)]
        with open(os.path.join(self.dir, "openmeteo_accuracy_records.json"), "w",
                  encoding="utf-8") as f:
            json.dump(rows, f)

    def test_seed_fills_the_scales_and_the_blend_runs_at_once(self):
        self._accuracy()
        f = self.make()
        got = f.fetch_forecast(force=True)
        self.assertEqual(len(f._model_records), 20)
        # Archive 16/24/32 kWh against 8 made: scales clamp at 0.5, 0.5, 0.5
        self.assertAlmostEqual(got["correctedTodayKwh"], (8 + 12 + 16) / 3, places=1)
        self.assertTrue(os.path.exists(f._model_records_path()))

    def test_no_actuals_no_seed_and_the_bands_run(self):
        got = self.make().fetch_forecast(force=True)
        self.assertNotIn(omf.PREVIOUS_RUNS_URL, self.stub.calls)
        self.assertEqual(got["blendModels"], [])

    def test_failed_seed_waits_before_trying_again(self):
        self._accuracy()
        self.stub.fail = True
        f = self.make()
        f._seed_model_records()
        f._seed_model_records()
        self.assertEqual(self.stub.calls.count(omf.PREVIOUS_RUNS_URL), 1)


class TestDayAheadScore(_Base):

    EVENING = TODAY.replace(hour=18)
    SCALED  = _records(20, actual=20.0, **{UK: 16.0, IFS: 24.0, AIFS: 32.0})

    def test_not_captured_before_the_evening(self):
        f = self.make(records=self.SCALED)
        f.fetch_forecast(force=True)
        self.assertEqual(f._day_ahead, {})

    def test_first_evening_fetch_is_kept(self):
        f = self.make(now=self.EVENING, records=self.SCALED)
        f.fetch_forecast(force=True)
        self.assertAlmostEqual(f._day_ahead[DAY_2]["headline_kwh"], 20.0)
        self.assertEqual(f._day_ahead[DAY_2]["models"], {UK: 16.0, IFS: 24.0, AIFS: 32.0})
        f._day_ahead[DAY_2]["headline_kwh"] = 1.0
        f.fetch_forecast(force=True)
        self.assertEqual(f._day_ahead[DAY_2]["headline_kwh"], 1.0)

    def test_midnight_grades_it_even_with_no_morning_baseline(self):
        f = self.make(now=self.EVENING, records=self.SCALED)
        f.fetch_forecast(force=True)
        f._morning_baselines.clear()
        f.record_accuracy(22.0, date_str=DAY_2)
        last = f._model_records[-1]
        self.assertEqual((last["date"], last["actual_kwh"], last["source"]), (DAY_2, 22.0, "live"))
        self.assertEqual(f.get_day_ahead_summary()["mean_error_kwh"], 2.0)
        self.assertNotIn(DAY_2, f._day_ahead)

    def test_survives_a_restart(self):
        f = self.make(now=self.EVENING, records=self.SCALED)
        f.fetch_forecast(force=True)
        self.assertIn(DAY_2, self.make()._day_ahead)

    def test_no_actual_records_nothing(self):
        f = self.make(now=self.EVENING, records=self.SCALED)
        f.fetch_forecast(force=True)
        before = len(f._model_records)
        f.record_accuracy(0.0, date_str=DAY_2)
        self.assertEqual(len(f._model_records), before)


if __name__ == "__main__":
    unittest.main()
