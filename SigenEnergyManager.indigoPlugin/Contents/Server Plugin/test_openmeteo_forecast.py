#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    test_openmeteo_forecast.py
# Description: Unit tests for OpenMeteoForecast bias-correction maths
# Author:      CliveS & Claude Opus 4.8
# Date:        19-07-2026
# Version:     1.2
# 1.2 — added TestRemainingToday for v1.7 of the module under test (bias-corrected,
#       current-hour-pro-rated remainingTodayKwh). Every case passes an explicit
#       `now=` so the suite never depends on the wall clock.
# 1.1 — added TestComputeCorrectionBands, TestApplyBandCorrection, and rewrote
#       TestBiasFactorNotApplied as TestBiasFactorApplied for v1.3 of the
#       module under test.

import io
import json
import os
import sys
import openmeteo_forecast
import tempfile
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from openmeteo_forecast import (
    OpenMeteoForecast,
    BIAS_CORRECTION_MIN,
    BIAS_CORRECTION_MAX,
    MIN_CALIBRATION_FORECAST_KWH,
    BIAS_BAND_CENTRES_KWH,
)


# v1.8 — the module constant is the LIVE optimiser file. Re-point it for the whole
# test module so no construction, present or future, can write the production file.
# (05-09-2026: the suite had been clobbering it with zero slots on every run.)
import openmeteo_forecast as _omf_module
_omf_module.OPTIMISER_FORECAST_FILE = os.path.join(tempfile.mkdtemp(prefix="omf_live_"),
                                                   "openmeteo_forecast.json")


def _isolated_forecast(latitude=51.5, longitude=-0.12):
    """A forecast object whose every file, the optimiser one included, is in a temp dir."""
    d = tempfile.mkdtemp()
    return OpenMeteoForecast(d, latitude=latitude, longitude=longitude,
                             optimiser_file=os.path.join(d, "openmeteo_forecast.json"))


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
        self.f = OpenMeteoForecast(data_dir=self._tmp, latitude=51.5007, longitude=-0.1246,
                                 optimiser_file=os.path.join(self._tmp, "openmeteo_forecast.json"))

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
        self.f = OpenMeteoForecast(data_dir=self._tmp, latitude=51.5007, longitude=-0.1246,
                                 optimiser_file=os.path.join(self._tmp, "openmeteo_forecast.json"))

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
        self.f = OpenMeteoForecast(data_dir=self._tmp, latitude=51.5007, longitude=-0.1246,
                                 optimiser_file=os.path.join(self._tmp, "openmeteo_forecast.json"))
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
        self.f = OpenMeteoForecast(data_dir=self._tmp, latitude=51.5007, longitude=-0.1246,
                                 optimiser_file=os.path.join(self._tmp, "openmeteo_forecast.json"))

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


class TestRemainingToday(unittest.TestCase):
    """v1.7 (19-Jul-2026): _remaining_today_kwh — bias-corrected + pro-rated.

    Regression cover for the Energy-page bug where the card read "38.3 kWh today,
    forecast 53, Remaining 25.3" — Remaining was summed off the RAW buckets while
    the forecast beside it was bias-corrected, and the whole current hour counted
    as still to come.

    Every case passes an explicit `now=` so nothing here depends on the wall clock.
    """

    # A simple day: 1 kWh in each of 06:00-11:00, nothing outside. Raw total 6 kWh.
    HOURLY = {f"2026-07-19 {h:02d}:00:00": (1000 if 6 <= h <= 11 else 0)
              for h in range(24)}

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="openmeteo_test_")
        self.f = OpenMeteoForecast(data_dir=self._tmp, latitude=51.5007, longitude=-0.1246,
                                 optimiser_file=os.path.join(self._tmp, "openmeteo_forecast.json"))

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_current_hour_is_pro_rated(self):
        """Half way through 09:00 → that bucket contributes half; 10 + 11 in full."""
        self.assertEqual(
            self.f._remaining_today_kwh(self.HOURLY, 1.0,
                                        now=datetime(2026, 7, 19, 9, 30)),
            2.5,
        )

    def test_on_the_hour_counts_the_whole_bucket(self):
        """Nothing of 09:00 elapsed yet → 09 + 10 + 11 in full."""
        self.assertEqual(
            self.f._remaining_today_kwh(self.HOURLY, 1.0,
                                        now=datetime(2026, 7, 19, 9, 0)),
            3.0,
        )

    def test_band_factor_is_applied(self):
        """Result lands on the corrected scale, not the raw one.

        Factor chosen to avoid a .x5 result — the helper rounds to 1 dp to match
        the published field's precision, so a half-way value would test rounding
        rather than the scaling this case is about.
        """
        self.assertEqual(
            self.f._remaining_today_kwh(self.HOURLY, 0.6,
                                        now=datetime(2026, 7, 19, 9, 30)),
            1.5,   # 2.5 raw × 0.6
        )

    def test_before_dawn_equals_whole_corrected_day(self):
        """The invariant that proves the scale matches correctedTodayKwh:
        remaining before any generation == raw daily total × the day's factor."""
        self.assertEqual(
            self.f._remaining_today_kwh(self.HOURLY, 0.915,
                                        now=datetime(2026, 7, 19, 3, 0)),
            round(6.0 * 0.915, 1),
        )

    def test_after_dusk_is_zero(self):
        self.assertEqual(
            self.f._remaining_today_kwh(self.HOURLY, 1.0,
                                        now=datetime(2026, 7, 19, 21, 15)),
            0.0,
        )

    def test_empty_and_missing_buckets_return_zero(self):
        self.assertEqual(self.f._remaining_today_kwh({}, 1.0,
                                                     now=datetime(2026, 7, 19, 9, 0)), 0.0)
        self.assertEqual(self.f._remaining_today_kwh(None, 1.0,
                                                     now=datetime(2026, 7, 19, 9, 0)), 0.0)

    def test_malformed_key_is_skipped_not_fatal(self):
        """One bad key must not cost us the rest of the day's forecast."""
        buckets = dict(self.HOURLY)
        buckets["not-a-timestamp"] = 5000
        self.assertEqual(
            self.f._remaining_today_kwh(buckets, 1.0,
                                        now=datetime(2026, 7, 19, 9, 0)),
            3.0,
        )

    def test_aware_now_is_accepted(self):
        """_now_local returns a tz-aware datetime under pytz — bucket keys are
        naive, so the helper must strip tz rather than raise on the comparison."""
        # Was skipped when pytz was absent, so this assertion never ran on the
        # usual runner. The test is about accepting an AWARE datetime — any real
        # zone proves that, and zoneinfo is stdlib.
        from zoneinfo import ZoneInfo
        aware = datetime(2026, 7, 19, 9, 30, tzinfo=ZoneInfo("Europe/London"))
        self.assertEqual(self.f._remaining_today_kwh(self.HOURLY, 1.0, now=aware), 2.5)

    def test_enrich_forecast_publishes_corrected_remaining(self):
        """End-to-end through _enrich_forecast: the published field is corrected."""
        self.f._correction_bands = [(17.5, 1.0), (30.0, 1.0), (40.0, 1.0),
                                    (50.0, 1.0), (65.0, 0.5)]
        enriched = self.f._enrich_forecast({
            "todayKwh":           65.0,     # lands squarely in the 0.5 band
            "tomorrowKwh":        20.0,
            "_hourly_p50_today":  self.HOURLY,
        })
        self.assertEqual(enriched["biasFactorToday"], 0.5)
        # Remaining must carry the same 0.5 the headline total carries.
        self.assertEqual(
            enriched["remainingTodayKwh"],
            self.f._remaining_today_kwh(self.HOURLY, 0.5),
        )


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
        self.f = OpenMeteoForecast(data_dir=self._tmp, latitude=51.5007, longitude=-0.1246,
                                 optimiser_file=os.path.join(self._tmp, "openmeteo_forecast.json"))

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


class TestLocalKeyToUtc(unittest.TestCase):
    """_local_key_to_utc — Open-Meteo returns LOCAL wall-clock keys, so this is
    where a timezone mistake turns into a forecast slot in the wrong hour.

    v5.56.0 moved it onto the shared london_time helper. The branch it replaced
    had two faults, one of them dormant in summer: its zoneinfo path used a bare
    replace(tzinfo=...) (fold=0) while its pytz path used is_dst=False (fold=1),
    so the ambiguous October hour depended on which library was installed; and
    its last-resort fell back to a flat "-1 hour", which is correct for BST and
    an hour wrong for the four months of GMT."""

    def _fc(self):
        return _isolated_forecast()

    def test_summer_key_is_one_hour_behind(self):
        self.assertEqual(self._fc()._local_key_to_utc("2026-08-05 12:00:00"),
                         "2026-08-05T11:00:00Z")

    def test_winter_key_is_unchanged(self):
        """The crude '-1 hour' fallback failed exactly here."""
        self.assertEqual(self._fc()._local_key_to_utc("2026-01-05 12:00:00"),
                         "2026-01-05T12:00:00Z")

    def test_ambiguous_autumn_hour_takes_the_gmt_occurrence(self):
        self.assertEqual(self._fc()._local_key_to_utc("2026-10-25 01:30:00"),
                         "2026-10-25T01:30:00Z")

    def test_unparseable_key_returns_none(self):
        self.assertIsNone(self._fc()._local_key_to_utc("not-a-time"))


class TestNowLocalIsAware(unittest.TestCase):
    """The old fallback was a bare datetime.now() — the SERVER clock, an hour
    behind local for eight months on a UTC-hosted machine, with nothing logged.
    Every caller uses it to decide which forecast slot is 'now'."""

    def test_now_local_is_timezone_aware(self):
        fc = _isolated_forecast()
        self.assertIsNotNone(fc._now_local().tzinfo)
class TestDaysAfterTomorrowAreKept(unittest.TestCase):
    """v1.9. The Weekend Happy Hour booking check decides on a Thursday about the
    Sunday three days on. A three-day fetch could not see it, and the days it did
    fetch after tomorrow were thrown away."""

    def setUp(self):
        import openmeteo_forecast as omf
        from datetime import datetime as _dt
        self._omf      = omf
        self._prev_req = omf.REQUESTS_AVAILABLE
        omf.REQUESTS_AVAILABLE = True
        self.f = _isolated_forecast()
        base = _dt(2026, 9, 24, 12, 0)            # a Thursday
        self.f._now_local = lambda: base.replace(tzinfo=__import__("zoneinfo").ZoneInfo("Europe/London"))
        rows = []
        for day in range(6):
            for h in range(24):
                t = (base.replace(hour=0) + timedelta(days=day, hours=h))
                rows.append((t.strftime("%Y-%m-%dT%H:%M"), 500.0 if 10 <= h < 14 else 0.0))
        self.f._fetch_array = lambda _cfg: rows

    def tearDown(self):
        self._omf.REQUESTS_AVAILABLE = self._prev_req

    def test_the_request_asks_for_six_days(self):
        self.assertEqual(self._omf.FORECAST_DAYS, 6)

    def test_sunday_is_there_on_thursday(self):
        combined = self.f._fetch_all_arrays()
        ahead = combined["_hourly_p50_ahead"]
        self.assertIn("2026-09-27 12:00:00", ahead)                 # Sunday
        self.assertIn("2026-09-29 12:00:00", ahead)                 # the last day fetched
        self.assertNotIn("2026-09-24 12:00:00", ahead)              # today is not "ahead"
        self.assertNotIn("2026-09-25 12:00:00", ahead)              # nor is tomorrow
        self.assertEqual(sorted(combined["aheadDayKwh"]),
                         ["2026-09-26", "2026-09-27", "2026-09-28", "2026-09-29"])
        self.assertGreater(combined["aheadDayKwh"]["2026-09-27"], 0.0)

    def test_today_and_tomorrow_are_exactly_as_before(self):
        combined = self.f._fetch_all_arrays()
        self.assertTrue(all(k.startswith("2026-09-24") for k in combined["_hourly_p50_today"]))
        self.assertTrue(all(k.startswith("2026-09-25") for k in combined["_hourly_p50_tomorrow"]))
        self.assertEqual(len(combined["_hourly_p50_today"]), 24)

    def test_the_empty_forecast_has_the_new_keys_too(self):
        d = self.f._empty_forecast("test")
        self.assertEqual(d["_hourly_p50_ahead"], {})
        self.assertEqual(d["aheadDayKwh"], {})


class TestForecastCarriesItsOwnDate(unittest.TestCase):
    """Every forecast dict must say which local day its totals are FOR.

    The bank-first latch refuses to classify a day from a forecast whose date is not
    today, which is what stops it arming from yesterday's number in the window
    between local midnight and the first fetch of the new day. If this key ever stops
    being emitted the latch can never arm and the export hold silently switches
    itself off — a feature dying quietly, with nothing in the log.
    """

    def test_the_empty_forecast_carries_the_key_as_unknown(self):
        f = _isolated_forecast()
        d = f._empty_forecast("test")
        self.assertIn("forecastDate", d)
        self.assertEqual(d["forecastDate"], "")   # unknown must never match a date

    def test_every_forecast_shaped_return_carries_forecastDate(self):
        """The success dict needs a live fetch to build, so pin it at the source.

        Any `return {` block in the module carrying "todayKwh" is a forecast dict and
        must carry "forecastDate" beside it.
        """
        src = io.open(openmeteo_forecast.__file__, encoding="utf-8").read()
        blocks = [b.split("}")[0] for b in src.split("return {")[1:]]
        blocks = [b for b in blocks if '"todayKwh"' in b]
        self.assertGreaterEqual(len(blocks), 2, "no forecast-shaped returns found — "
                                                "this test would pass vacuously")
        for b in blocks:
            self.assertIn('"forecastDate"', b,
                          "a forecast dict without forecastDate disables the latch")



class TestOptimiserFileIsolation(unittest.TestCase):
    """v1.8: the optimiser file path is injectable and an empty forecast never
    overwrites it. Before this the suite wrote the LIVE file with zero slots."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.path = os.path.join(self._tmp, "openmeteo_forecast.json")
        self.f = OpenMeteoForecast(data_dir=self._tmp, latitude=51.5007, longitude=-0.1246,
                                   optimiser_file=self.path)

    def test_the_default_path_is_the_module_constant(self):
        import openmeteo_forecast as omf
        f = OpenMeteoForecast(data_dir=self._tmp, latitude=51.5007, longitude=-0.1246)
        self.assertEqual(f.optimiser_file, omf.OPTIMISER_FORECAST_FILE)
        self.assertEqual(self.f.optimiser_file, self.path)

    def test_an_empty_forecast_leaves_the_file_alone(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write('{"tomorrow_kwh": 33.3, "hourly": {"x": 1}}')
        self.f._write_optimiser_file({"_hourly_p50_today": {}, "_hourly_p50_tomorrow": {}})
        with open(self.path, encoding="utf-8") as fh:
            self.assertIn('"tomorrow_kwh": 33.3', fh.read())

    def test_a_real_forecast_writes_to_the_injected_path(self):
        d = self.f._now_local().strftime("%Y-%m-%d")
        combined = {"_hourly_p50_today": {f"{d} 12:00:00": 4000},
                    "_hourly_p50_tomorrow": {}}
        self.f._write_optimiser_file(combined)
        self.assertTrue(os.path.exists(self.path))
        import json
        doc = json.load(open(self.path, encoding="utf-8"))
        self.assertEqual(len(doc["hourly"]), 1)
        self.assertGreater(doc["today_kwh"], 0.0)

    def test_the_suite_cannot_reach_the_live_file(self):
        import openmeteo_forecast as omf
        self.assertNotIn("Perceptive Automation", omf.OPTIMISER_FORECAST_FILE,
                         "the module constant must be re-pointed for the whole test module")


class TestRepairZeroActuals(unittest.TestCase):
    """v5.106.0 — reconstruct accuracy records that lost their actual PV total.

    Until this release the midnight task paired the morning baseline with the
    live mirror of the inverter's daily accumulator, which the inverter had
    already reset. 9 of the 10 days to 14-Sep-2026 recorded actual_kwh = 0.0.

    They never reached a band (`0.1 < factor` filters them) so nothing looked
    broken. What they did instead was push real samples out of the 60-record
    calibration window: on 15-Sep-2026 the 40 kWh band held twelve samples, all
    dated 17-Jul to 31-Aug, so it returned x1.05 over a September measuring
    0.888 — scaling a falling forecast UP.
    """

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="openmeteo_repair_")
        self.f = OpenMeteoForecast(data_dir=self._tmp, latitude=51.5007, longitude=-0.1246,
                                   optimiser_file=os.path.join(self._tmp, "openmeteo_forecast.json"))

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _seed(self, records):
        self.f._save_accuracy_records(records)

    def test_a_zero_record_is_repaired_from_the_settled_total(self):
        self._seed([_rec("2026-09-13", 42.7, 0.0)])
        n = self.f.repair_zero_actuals(lambda d: 41.21 if d == "2026-09-13" else None)
        self.assertEqual(n, 1)
        rec = self.f._load_accuracy_records()[0]
        self.assertAlmostEqual(rec["actual_kwh"], 41.21)
        self.assertAlmostEqual(rec["factor"], round(41.21 / 42.7, 4))
        self.assertTrue(rec["repaired"])          # auditable, not silently rewritten

    def test_a_good_record_is_left_exactly_alone(self):
        self._seed([_rec("2026-09-07", 28.2, 22.85)])
        before = json.loads(json.dumps(self.f._load_accuracy_records()))
        self.assertEqual(self.f.repair_zero_actuals(lambda d: 99.0), 0)
        self.assertEqual(self.f._load_accuracy_records(), before)

    def test_an_unsettleable_day_stays_zero_rather_than_guessing(self):
        """A missing day is not a dark day. Leaving it out keeps the bands honest."""
        self._seed([_rec("2026-09-05", 43.9, 0.0)])
        self.assertEqual(self.f.repair_zero_actuals(lambda d: None), 0)
        self.assertEqual(self.f._load_accuracy_records()[0]["actual_kwh"], 0.0)

    def test_a_zero_from_the_lookup_can_never_be_the_repair(self):
        """Zero is the bug. Writing it back would look like a successful repair."""
        self._seed([_rec("2026-09-05", 43.9, 0.0)])
        self.assertEqual(self.f.repair_zero_actuals(lambda d: 0.0), 0)

    def test_a_raising_lookup_does_not_abandon_the_remaining_days(self):
        def lookup(day):
            if day == "2026-09-05":
                raise RuntimeError("history unreadable")
            return 37.5
        self._seed([_rec("2026-09-05", 43.9, 0.0), _rec("2026-09-06", 34.3, 0.0)])
        self.assertEqual(self.f.repair_zero_actuals(lookup), 1)

    def test_repair_turns_the_band_it_was_starving(self):
        """The whole point: the band must stop scaling a falling forecast UP.

        Eight July samples over-delivering, then nine September days that
        under-delivered but recorded zero — the shape of the live file on
        15-Sep-2026. Before the repair the band can only see the summer.

        Replayed against the real file that day the 40 kWh band moved
        1.0428 -> 0.98, which turns yesterday's raw 40.4 kWh from a corrected
        41.9 into 39.5 against a measured 36.43.
        """
        recs  = [_rec(f"2026-07-{d:02d}", 40.0, 46.0) for d in range(1, 9)]
        recs += [_rec(f"2026-09-{d:02d}", 40.0, 0.0) for d in range(1, 10)]
        self._seed(recs)
        before = dict(self.f._compute_correction_bands(self.f._load_accuracy_records()))

        self.assertEqual(self.f.repair_zero_actuals(lambda d: 35.0), 9)

        band40 = dict(self.f._correction_bands)[40.0]
        self.assertGreater(before[40.0], 1.0, "precondition: it was scaling UP")
        self.assertLess(band40, 1.0,
                        "a band whose majority now under-delivers must scale DOWN")

    def test_a_minority_of_repaired_days_cannot_move_a_median_band(self):
        """Worth pinning, because it sets how fast this fix can possibly work.

        The band factor is a MEDIAN, so recovering a handful of days changes
        nothing until they outnumber the samples already there. Six repaired
        September days against twelve summer ones leave the median untouched —
        the repair is necessary and is not, on its own, instant.
        """
        recs  = [_rec(f"2026-07-{d:02d}", 40.0, 46.0) for d in range(1, 13)]
        recs += [_rec(f"2026-09-{d:02d}", 40.0, 0.0) for d in range(1, 7)]
        self._seed(recs)

        self.assertEqual(self.f.repair_zero_actuals(lambda d: 35.0), 6)

        self.assertAlmostEqual(dict(self.f._correction_bands)[40.0], 1.15, places=2)

    def test_nothing_to_repair_is_not_an_error(self):
        self.assertEqual(self.f.repair_zero_actuals(lambda d: 30.0), 0)


if __name__ == "__main__":
    unittest.main()
