#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    test_openmeteo_forecast.py
# Description: Unit tests for OpenMeteoForecast bias-correction maths
# Author:      CliveS & Claude Opus 4.7
# Date:        22-05-2026
# Version:     1.1
# 1.1 — added TestComputeCorrectionBands, TestApplyBandCorrection, and rewrote
#       TestBiasFactorNotApplied as TestBiasFactorApplied for v1.3 of the
#       module under test.

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from openmeteo_forecast import (
    OpenMeteoForecast,
    BIAS_CORRECTION_MIN,
    BIAS_CORRECTION_MAX,
    MIN_CALIBRATION_FORECAST_KWH,
    BIAS_BAND_CENTRES_KWH,
    MIN_BAND_SAMPLES,
)


def _rec(date, fc, actual, month=None):
    factor = round(actual / fc, 4) if fc else 1.0
    return {
        "date":         date,
        "month":        month or date[:7],
        "forecast_kwh": fc,
        "actual_kwh":   actual,
        "factor":       factor,
    }


class TestClampedSumRatio(unittest.TestCase):
    """Tests for OpenMeteoForecast._clamped_sum_ratio (kWh-weighted bias factor)."""

    def test_balanced_records_return_unity(self):
        recs = [_rec(f"2026-05-{d:02d}", 40.0, 40.0) for d in range(1, 6)]
        self.assertAlmostEqual(OpenMeteoForecast._clamped_sum_ratio(recs), 1.0, places=4)

    def test_consistent_under_forecast_returns_factor_above_one(self):
        # Forecast under by ~14% every day
        recs = [_rec(f"2026-05-{d:02d}", 35.0, 40.0) for d in range(1, 6)]
        f = OpenMeteoForecast._clamped_sum_ratio(recs)
        self.assertAlmostEqual(f, 200.0 / 175.0, places=4)   # 40*5 / 35*5

    def test_outlier_is_not_inflated_like_arithmetic_mean(self):
        """Mix of one big over-forecast (ratio 0.5) with several under-forecast (ratio 1.4).
        Arithmetic mean of ratios would over-weight the under days; sum-ratio is kWh-weighted."""
        recs = [
            _rec("2026-05-01", 60.0, 30.0),  # ratio 0.50  (over-forecast — big day)
            _rec("2026-05-02", 25.0, 35.0),  # ratio 1.40
            _rec("2026-05-03", 25.0, 35.0),  # ratio 1.40
            _rec("2026-05-04", 25.0, 35.0),  # ratio 1.40
            _rec("2026-05-05", 25.0, 35.0),  # ratio 1.40
        ]
        arith = sum(r["factor"] for r in recs) / len(recs)
        sumr  = OpenMeteoForecast._clamped_sum_ratio(recs)
        self.assertGreater(arith, sumr,
            f"arithmetic mean ({arith:.3f}) should over-estimate vs sum-ratio ({sumr:.3f})")
        # sum-ratio = (30 + 35*4) / (60 + 25*4) = 170 / 160 = 1.0625
        self.assertAlmostEqual(sumr, 170.0 / 160.0, places=4)

    def test_clamps_to_max(self):
        # All days under-forecast by 5x — should clamp to BIAS_CORRECTION_MAX
        recs = [_rec(f"2026-05-{d:02d}", 10.0, 50.0) for d in range(1, 6)]
        self.assertEqual(OpenMeteoForecast._clamped_sum_ratio(recs), BIAS_CORRECTION_MAX)

    def test_clamps_to_min(self):
        # All days over-forecast by 5x — should clamp to BIAS_CORRECTION_MIN
        recs = [_rec(f"2026-05-{d:02d}", 50.0, 10.0) for d in range(1, 6)]
        self.assertEqual(OpenMeteoForecast._clamped_sum_ratio(recs), BIAS_CORRECTION_MIN)

    def test_zero_forecast_total_returns_unity(self):
        self.assertEqual(OpenMeteoForecast._clamped_sum_ratio([]), 1.0)


class TestComputeCorrectionFactor(unittest.TestCase):
    """End-to-end test of _compute_correction_factor including filtering."""

    def setUp(self):
        # Need a writable data_dir for the class but we don't trigger any disk I/O.
        self._tmp = tempfile.mkdtemp(prefix="openmeteo_test_")
        self.f = OpenMeteoForecast(data_dir=self._tmp, latitude=51.5007, longitude=-0.1246)

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_empty_records_returns_unity(self):
        self.assertEqual(self.f._compute_correction_factor([]), 1.0)

    def test_under_three_records_returns_unity(self):
        recs = [_rec("2026-05-01", 30.0, 35.0), _rec("2026-05-02", 30.0, 35.0)]
        self.assertEqual(self.f._compute_correction_factor(recs), 1.0)

    def test_filters_out_low_forecast_days(self):
        """Days below MIN_CALIBRATION_FORECAST_KWH should be excluded."""
        # 3 valid days (under by 14%) + one low-forecast outlier with extreme ratio
        recs = [
            _rec("2026-05-01", 35.0, 40.0),
            _rec("2026-05-02", 35.0, 40.0),
            _rec("2026-05-03", 35.0, 40.0),
            _rec("2026-05-04", MIN_CALIBRATION_FORECAST_KWH * 0.5, 8.0),  # excluded
        ]
        f = self.f._compute_correction_factor(recs)
        # Should equal sum-ratio of the 3 valid days only
        self.assertAlmostEqual(f, 120.0 / 105.0, places=4)

    def test_filters_out_extreme_per_day_ratios(self):
        """Per-day ratios outside [0.1, 2x MAX] should be excluded."""
        recs = [
            _rec("2026-05-01", 30.0, 35.0),
            _rec("2026-05-02", 30.0, 35.0),
            _rec("2026-05-03", 30.0, 35.0),
            _rec("2026-05-04", 30.0, 0.5),   # ratio ~0.017 — excluded
        ]
        f = self.f._compute_correction_factor(recs)
        self.assertAlmostEqual(f, 105.0 / 90.0, places=4)

    def test_reproduces_may_2026_correction_at_1_150(self):
        """Replay the actual May 2026 records and verify the new factor.

        Before fix: arithmetic-mean-of-ratios produced 1.163.
        After fix:  sum-ratio produces 1.150 (matches the kWh-weighted truth).
        """
        # Live data from openmeteo_accuracy_records.json (May 2026 subset, fc >= 10)
        may_2026 = [
            ("2026-05-01", 67.8, 56.81),
            ("2026-05-02", 42.3, 53.45),
            ("2026-05-04", 32.8, 51.78),
            ("2026-05-05", 24.3, 29.16),
            ("2026-05-06", 42.0, 53.13),
            ("2026-05-07", 40.1, 41.22),
            ("2026-05-08", 41.5, 38.59),
            ("2026-05-10", 11.6, 9.42),
            ("2026-05-10", 47.1, 51.79),
            ("2026-05-11", 32.9, 48.27),
            ("2026-05-13", 28.6, 42.18),
            ("2026-05-14", 32.8, 41.97),
            ("2026-05-15", 43.6, 40.93),
            ("2026-05-16", 44.2, 51.89),
            ("2026-05-17", 39.6, 54.42),
            ("2026-05-18", 40.6, 48.06),
            ("2026-05-19", 35.8, 31.08),
            ("2026-05-20", 36.5, 42.19),
        ]
        recs = [_rec(d, fc, act) for (d, fc, act) in may_2026]

        # Direct call to the static method to keep this independent of "current month"
        valid = [r for r in recs
                 if r["forecast_kwh"] >= MIN_CALIBRATION_FORECAST_KWH
                 and 0.1 < r["factor"] < BIAS_CORRECTION_MAX * 2]
        factor = OpenMeteoForecast._clamped_sum_ratio(valid)
        self.assertAlmostEqual(factor, 1.150, places=3)


class TestComputeCorrectionBands(unittest.TestCase):
    """v1.3 (22-May-2026): per-band correction factors via median(actual/forecast).

    Each band centre in BIAS_BAND_CENTRES_KWH gets a factor computed from
    records whose raw forecast falls within ±BIAS_BAND_HALF_WIDTH of centre.
    Bands with fewer than MIN_BAND_SAMPLES records inherit the global
    kWh-weighted factor across all valid records.
    """

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="openmeteo_test_")
        self.f = OpenMeteoForecast(data_dir=self._tmp, latitude=51.5007, longitude=-0.1246)

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_empty_records_returns_unity_bands(self):
        bands = self.f._compute_correction_bands([])
        self.assertEqual(len(bands), len(BIAS_BAND_CENTRES_KWH))
        for centre, factor in bands:
            self.assertEqual(factor, 1.0)

    def test_band_uses_median_of_in_band_ratios(self):
        """Three records in the 40 kWh band — factor should equal the median ratio."""
        recs = [
            _rec("2026-05-01", 38.0, 44.0),   # ratio 1.1579
            _rec("2026-05-02", 40.0, 52.0),   # ratio 1.3000  <- median
            _rec("2026-05-03", 42.0, 56.0),   # ratio 1.3333
        ]
        bands = self.f._compute_correction_bands(recs)
        band_dict = dict(bands)
        self.assertAlmostEqual(band_dict[40.0], 1.3000, places=4)

    def test_sparse_band_inherits_global_factor(self):
        """A band with < MIN_BAND_SAMPLES records inherits the kWh-weighted scalar."""
        # 4 records all near the 30 kWh band; nothing near 50/65
        recs = [_rec(f"2026-05-{d:02d}", 30.0, 36.0)
                for d in range(1, 5)]   # ratio 1.20 each
        bands = self.f._compute_correction_bands(recs)
        band_dict = dict(bands)
        # 30 band has 4 samples → median = 1.20
        self.assertAlmostEqual(band_dict[30.0], 1.20, places=4)
        # Bands with no in-window records inherit global sum-ratio (also 1.20 here)
        self.assertAlmostEqual(band_dict[65.0], 1.20, places=4)

    def test_band_factor_clamped_to_max(self):
        # Ratio 2.0 — above MAX (1.5) but inside the per-day outlier filter
        # [0.1, 2 × MAX = 3.0] so records survive to the band stage.
        recs = [_rec(f"2026-05-{d:02d}", 30.0, 60.0)
                for d in range(1, 5)]
        bands = self.f._compute_correction_bands(recs)
        band_dict = dict(bands)
        self.assertEqual(band_dict[30.0], BIAS_CORRECTION_MAX)

    def test_band_factor_clamped_to_min(self):
        # Ratio 0.30 — below MIN (0.5) but above the 0.1 outlier floor.
        recs = [_rec(f"2026-05-{d:02d}", 50.0, 15.0)
                for d in range(1, 5)]
        bands = self.f._compute_correction_bands(recs)
        band_dict = dict(bands)
        self.assertEqual(band_dict[50.0], BIAS_CORRECTION_MIN)

    def test_below_min_calibration_records_excluded(self):
        recs = (
            [_rec(f"2026-05-{d:02d}", 30.0, 36.0) for d in range(1, 4)]
            + [_rec("2026-05-04", MIN_CALIBRATION_FORECAST_KWH * 0.5, 8.0)]
        )
        bands = self.f._compute_correction_bands(recs)
        # 30 band has exactly 3 valid samples — median = 1.20, not pulled by the outlier
        self.assertAlmostEqual(dict(bands)[30.0], 1.20, places=4)


class TestApplyBandCorrection(unittest.TestCase):
    """v1.3: linear interpolation of the band factor for any raw kWh value."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="openmeteo_test_")
        self.f = OpenMeteoForecast(data_dir=self._tmp, latitude=51.5007, longitude=-0.1246)
        # Hand-crafted bands so interpolation behaviour is unambiguous
        self.f._correction_bands = [
            (17.5, 1.00),
            (30.0, 1.30),
            (40.0, 1.15),
            (50.0, 0.95),
            (65.0, 0.88),
        ]

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_zero_returns_unity(self):
        self.assertEqual(self.f._apply_band_correction(0.0), 1.0)

    def test_below_first_centre_returns_first_factor(self):
        self.assertEqual(self.f._apply_band_correction(5.0), 1.00)

    def test_above_last_centre_returns_last_factor(self):
        self.assertEqual(self.f._apply_band_correction(80.0), 0.88)

    def test_exactly_at_centre_returns_that_factor(self):
        self.assertEqual(self.f._apply_band_correction(30.0), 1.30)
        self.assertEqual(self.f._apply_band_correction(50.0), 0.95)

    def test_midpoint_is_linear_average(self):
        # Halfway between 30 (1.30) and 40 (1.15) → 1.225
        self.assertAlmostEqual(self.f._apply_band_correction(35.0), 1.225, places=4)
        # Halfway between 50 (0.95) and 65 (0.88) → 0.915
        self.assertAlmostEqual(self.f._apply_band_correction(57.5), 0.915, places=4)


class TestBiasFactorApplied(unittest.TestCase):
    """v1.3 (22-May-2026): bias factor IS now applied to corrected totals.

    Replaces v1.2's TestBiasFactorNotApplied since the magnitude-conditional
    band correction makes corrected != raw whenever any band factor != 1.0.
    """

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="openmeteo_test_")
        self.f = OpenMeteoForecast(data_dir=self._tmp, latitude=51.5007, longitude=-0.1246)

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_corrected_uses_per_day_band_factor(self):
        """Today and tomorrow fall in different bands → different multipliers."""
        self.f._correction_factor = 1.10  # display scalar — used unchanged
        self.f._correction_bands = [
            (17.5, 1.00),
            (30.0, 1.30),   # today's band (raw 30 kWh)
            (40.0, 1.15),
            (50.0, 0.95),   # tomorrow's band (raw 50 kWh)
            (65.0, 0.88),
        ]
        enriched = self.f._enrich_forecast({
            "todayKwh":    30.0,
            "tomorrowKwh": 50.0,
        })
        self.assertEqual(enriched["biasFactor"],         1.10)
        self.assertEqual(enriched["biasFactorToday"],    1.300)
        self.assertEqual(enriched["biasFactorTomorrow"], 0.950)
        self.assertEqual(enriched["correctedTodayKwh"],    39.0)   # 30 × 1.30
        self.assertEqual(enriched["correctedTomorrowKwh"], 47.5)   # 50 × 0.95

    def test_corrected_equals_raw_with_unity_bands(self):
        # Default bands are all 1.0 from __init__ — corrected must equal raw
        enriched = self.f._enrich_forecast({
            "todayKwh":    30.0,
            "tomorrowKwh": 50.0,
        })
        self.assertEqual(enriched["correctedTodayKwh"],    30.0)
        self.assertEqual(enriched["correctedTomorrowKwh"], 50.0)


class TestPartialFetch(unittest.TestCase):
    """Regression: a partial Open-Meteo fetch (one or more arrays missing) must
    never be stamped 'OK' nor overwrite a complete cached forecast — a low total
    would inflate the manager's import need and trigger unnecessary grid import.
    """

    def setUp(self):
        import openmeteo_forecast as omf
        self._omf      = omf
        self._prev_req = omf.REQUESTS_AVAILABLE
        omf.REQUESTS_AVAILABLE = True   # _fetch_array is mocked, so no real HTTP
        self._tmp = tempfile.mkdtemp()
        self.f = OpenMeteoForecast(data_dir=self._tmp, latitude=51.5007, longitude=-0.1246)

    def tearDown(self):
        self._omf.REQUESTS_AVAILABLE = self._prev_req

    def _mock_arrays(self, ok_count):
        """First ok_count arrays return (empty) data; the rest fail (None)."""
        state = {"n": 0}
        def fake_fetch(_array_cfg):
            state["n"] += 1
            return [] if state["n"] <= ok_count else None
        self.f._fetch_array = fake_fetch

    def test_partial_without_cache_flagged_and_not_cached(self):
        self.assertGreater(len(self.f.arrays), 1)
        self._mock_arrays(ok_count=1)                      # 1 of N arrays succeed
        result = self.f.fetch_forecast(force=True)
        self.assertTrue(
            result["forecastStatus"].startswith("Partial"),
            f"expected a Partial status, got {result['forecastStatus']!r}",
        )
        self.assertIsNone(self.f._cached_forecast)          # degraded → not cached

    def test_partial_does_not_clobber_complete_cache(self):
        n = len(self.f.arrays)
        self.f._cached_forecast = {
            "todayKwh": 50.0, "tomorrowKwh": 40.0,
            "arrays_ok": n, "arrays_total": n, "forecastStatus": "OK",
            "_hourly_p50_today": {}, "_hourly_p50_tomorrow": {}, "_dawn_times": {},
        }
        self._mock_arrays(ok_count=1)                       # next fetch is partial
        result = self.f.fetch_forecast(force=True)
        self.assertEqual(result["todayKwh"], 50.0)          # served the complete cache
        self.assertEqual(self.f._cached_forecast["todayKwh"], 50.0)   # cache untouched

    def test_complete_fetch_is_ok_and_cached(self):
        self._mock_arrays(ok_count=len(self.f.arrays))      # all arrays succeed
        result = self.f.fetch_forecast(force=True)
        self.assertEqual(result["forecastStatus"], "OK")
        self.assertIsNotNone(self.f._cached_forecast)


if __name__ == "__main__":
    unittest.main()
