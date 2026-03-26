#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    solcast.py
# Description: Solcast solar forecast API client - direct calls with disk cache
#              and daily bias correction
# Author:      CliveS & Claude Sonnet 4.6
# Date:        26-03-2026
# Version:     1.0
#
# Solcast Hobbyist plan: 10 API calls/day/site (max)
# Cache TTL: 8640 seconds (2.4 hours) per site to stay within 10 calls/day
# 2 sites: East+South (site 1), West+Garage NE (site 2)

import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone

try:
    import requests
    REQUESTS_AVAILABLE = True
except ImportError:
    REQUESTS_AVAILABLE = False

try:
    import pytz
    LONDON_TZ = pytz.timezone("Europe/London")
    PYTZ_AVAILABLE = True
except ImportError:
    PYTZ_AVAILABLE = False
    LONDON_TZ = None


# ============================================================
# Constants
# ============================================================

SOLCAST_API_BASE = "https://api.solcast.com.au/rooftop_sites"
REQUEST_TIMEOUT  = 30   # seconds per API call
CACHE_TTL        = 8640 # seconds = 2.4 hours (10 calls/day limit)

# Conservative lower bound used for dawn planning
PV_GENERATION_THRESHOLD_W = 500  # watts - forecast below this = pre-dawn / post-dusk


class SolcastForecast:
    """Direct Solcast API client with disk cache and bias correction.

    Makes API calls directly to the Solcast rooftop_sites API.
    Results are cached to disk to respect the 10 calls/day/site hobbyist limit.
    Combines P50/P10/P90 estimates from 2 sites for total generation forecast.

    Bias correction (hones forecasts over time):
    1. At 00:05 each day: capture today's P50 total as _morning_forecast
    2. At midnight: record (forecast_kwh, actual_kwh) to forecast_accuracy_records.json
    3. Compute seasonal correction factor from historical records
    4. Apply factor to raw forecast before publishing corrected states
    """

    def __init__(self, api_key, site_1_id, site_2_id, data_dir, logger=None):
        """Initialise the Solcast forecast client.

        Args:
            api_key:   Solcast API key (Bearer token)
            site_1_id: Resource ID for East+South arrays
            site_2_id: Resource ID for West+Garage NE arrays
            data_dir:  Directory for cache and accuracy record files
            logger:    Logger instance
        """
        self.api_key   = api_key
        self.site_ids  = [site_1_id, site_2_id]
        self.data_dir  = data_dir
        self.logger    = logger or logging.getLogger("SigenEnergyManager.Solcast")

        # In-memory cache: last successful combined forecast
        self._cached_forecast = None
        self._cached_time     = 0.0

        # Bias correction state
        self._morning_forecast_kwh = 0.0  # captured at 00:05
        self._correction_factor    = 1.0  # current seasonal factor

    # ================================================================
    # Public API
    # ================================================================

    def fetch_forecast(self, force=False):
        """Fetch and return combined forecast from both Solcast sites.

        Respects cache TTL (2.4h) unless force=True.

        Returns dict with keys:
            todayKwh, tomorrowKwh, correctedTodayKwh, correctedTomorrowKwh,
            biasFactor, currentHourWatts, nextHourWatts, remainingTodayKwh,
            forecastStatus, lastUpdate,
            _hourly_p50_today, _hourly_p10_today (for battery_manager),
            _dawn_times (dict: date -> first slot with PV > threshold)
        """
        if not REQUESTS_AVAILABLE:
            self.logger.error("requests library not available - cannot fetch Solcast forecast")
            return self._empty_forecast("requests not installed")

        now = time.time()
        cache_age = now - self._cached_time

        if not force and cache_age < CACHE_TTL and self._cached_forecast:
            self.logger.debug(f"Using cached forecast (age {cache_age:.0f}s / TTL {CACHE_TTL}s)")
            return self._enrich_forecast(self._cached_forecast)

        if not self.api_key or not all(self.site_ids):
            self.logger.error("Solcast API key or site IDs not configured")
            if self._cached_forecast:
                self.logger.warning("Using stale Solcast cache (config missing)")
                return self._enrich_forecast(self._cached_forecast)
            return self._empty_forecast("API key/site IDs not configured")

        # Fetch both sites
        site_forecasts = []
        failed_sites   = []

        for site_id in self.site_ids:
            data = self._fetch_site(site_id)
            if data:
                site_forecasts.append(data)
            else:
                failed_sites.append(site_id)

        if not site_forecasts:
            self.logger.error("All Solcast sites failed - cannot update forecast")
            if self._cached_forecast:
                self.logger.warning("Returning stale cached forecast")
                return self._enrich_forecast(self._cached_forecast)
            return self._empty_forecast("All API calls failed")

        # Combine site data (sum P50/P10/P90 per period)
        combined = self._combine_sites(site_forecasts)

        if failed_sites:
            combined["forecastStatus"] = f"Partial - {len(failed_sites)} site(s) failed"
        else:
            combined["forecastStatus"] = "OK"

        combined["lastUpdate"] = datetime.now().strftime("%H:%M:%S")

        # Persist combined forecast to disk cache
        self._save_cache(combined)
        self._cached_forecast = combined
        self._cached_time     = now

        return self._enrich_forecast(combined)

    def capture_morning_forecast(self):
        """Record today's P50 total kWh as the morning forecast baseline.

        Call this at ~00:05 each day. Used for end-of-day bias correction.
        """
        if self._cached_forecast:
            self._morning_forecast_kwh = self._cached_forecast.get("todayKwh", 0.0)
            self.logger.debug(
                f"Captured morning forecast: {self._morning_forecast_kwh:.1f} kWh"
            )
        else:
            self._morning_forecast_kwh = 0.0

    def record_accuracy(self, actual_pv_kwh):
        """Record today's forecast vs actual for bias correction.

        Call this at midnight after the daily energy accumulator is finalised.

        Args:
            actual_pv_kwh: Actual PV generation today (kWh from Modbus accumulators)
        """
        if self._morning_forecast_kwh <= 0.0:
            self.logger.debug("No morning forecast captured - skipping accuracy record")
            return

        today_str = datetime.now().strftime("%Y-%m-%d")
        month_str = datetime.now().strftime("%Y-%m")

        record = {
            "date":         today_str,
            "month":        month_str,
            "forecast_kwh": round(self._morning_forecast_kwh, 2),
            "actual_kwh":   round(actual_pv_kwh, 2),
            "factor":       round(actual_pv_kwh / self._morning_forecast_kwh, 4)
                            if self._morning_forecast_kwh > 0 else 1.0,
        }

        records = self._load_accuracy_records()
        records.append(record)
        # Keep last 365 days only
        if len(records) > 365:
            records = records[-365:]
        self._save_accuracy_records(records)

        self.logger.info(
            f"Accuracy record: forecast={self._morning_forecast_kwh:.1f} kWh, "
            f"actual={actual_pv_kwh:.1f} kWh, factor={record['factor']:.3f}"
        )

        # Update correction factor immediately
        self._correction_factor = self._compute_correction_factor(records)
        self.logger.info(f"Updated bias correction factor: {self._correction_factor:.3f}")

        # Reset morning forecast for the new day
        self._morning_forecast_kwh = 0.0

    def load_correction_factor(self):
        """Load and compute correction factor from saved records.

        Call on plugin startup so bias correction is active immediately.
        """
        records = self._load_accuracy_records()
        self._correction_factor = self._compute_correction_factor(records)
        self.logger.info(
            f"Loaded bias correction factor from {len(records)} records: "
            f"{self._correction_factor:.3f}"
        )

    # ================================================================
    # API Calls
    # ================================================================

    def _fetch_site(self, site_id):
        """Fetch 48-hour forecast for one Solcast site.

        Returns list of period dicts or None on failure.
        """
        # Check per-site disk cache first
        cached = self._load_site_cache(site_id)
        if cached:
            age = time.time() - cached.get("cached_time", 0)
            if age < CACHE_TTL:
                self.logger.debug(
                    f"Solcast site {site_id[:8]}...: using disk cache (age {age:.0f}s)"
                )
                return cached.get("forecasts", [])

        # Make API call
        url     = f"{SOLCAST_API_BASE}/{site_id}/forecasts"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Accept":        "application/json",
        }
        params = {"format": "json", "hours": 48}

        try:
            response = requests.get(url, headers=headers, params=params, timeout=REQUEST_TIMEOUT)

            if response.status_code == 429:
                self.logger.warning(f"Solcast rate limit hit for site {site_id[:8]}...")
                if cached:
                    return cached.get("forecasts", [])
                return None

            if response.status_code == 401:
                self.logger.error(
                    f"Solcast authentication failed for site {site_id[:8]}... - check API key"
                )
                return None

            if response.status_code != 200:
                self.logger.error(
                    f"Solcast API error {response.status_code} for site {site_id[:8]}...: "
                    f"{response.text[:200]}"
                )
                if cached:
                    return cached.get("forecasts", [])
                return None

            data      = response.json()
            forecasts = data.get("forecasts", [])

            if not forecasts:
                self.logger.warning(f"Solcast returned empty forecast for site {site_id[:8]}...")
                return None

            # Save to disk cache
            self._save_site_cache(site_id, forecasts)

            self.logger.debug(
                f"Solcast site {site_id[:8]}...: fetched {len(forecasts)} periods"
            )
            return forecasts

        except requests.exceptions.Timeout:
            self.logger.warning(f"Solcast timeout for site {site_id[:8]}...")
            if cached:
                return cached.get("forecasts", [])
            return None
        except Exception as e:
            self.logger.error(f"Solcast fetch error for site {site_id[:8]}...: {e}")
            if cached:
                return cached.get("forecasts", [])
            return None

    # ================================================================
    # Data Processing
    # ================================================================

    def _combine_sites(self, site_forecasts_list):
        """Sum P50/P10/P90 kW values from all sites into half-hourly slots.

        Returns combined dict with hourly buckets for today and tomorrow.
        """
        now_local = self._now_local()
        today     = now_local.date()
        tomorrow  = today + timedelta(days=1)

        # Accumulate summed power per period_end key
        combined_periods = {}  # period_end_str -> {p50, p10, p90}

        for forecasts in site_forecasts_list:
            for period in forecasts:
                key      = period.get("period_end", "")
                p50      = period.get("pv_estimate",   0.0)
                p10      = period.get("pv_estimate10", 0.0)
                p90      = period.get("pv_estimate90", 0.0)

                if key not in combined_periods:
                    combined_periods[key] = {"p50": 0.0, "p10": 0.0, "p90": 0.0}

                combined_periods[key]["p50"] += p50
                combined_periods[key]["p10"] += p10
                combined_periods[key]["p90"] += p90

        # Convert to hourly Wh buckets for today and tomorrow
        hourly_p50_today    = {}
        hourly_p10_today    = {}
        hourly_p50_tomorrow = {}

        today_total    = 0.0
        tomorrow_total = 0.0
        dawn_times     = {}  # date_str -> datetime of first slot with >threshold PV

        for period_end_str, vals in combined_periods.items():
            try:
                period_end = self._parse_period_end(period_end_str)
            except (ValueError, TypeError):
                continue

            period_start    = period_end - timedelta(minutes=30)
            period_date     = period_start.date()
            period_kwh_p50  = vals["p50"] * 0.5   # kW * 0.5h = kWh
            period_kwh_p10  = vals["p10"] * 0.5
            period_wh_p50   = vals["p50"] * 500    # kW * 0.5h * 1000 = Wh

            # Accumulate daily totals
            if period_date == today:
                today_total += period_kwh_p50
            elif period_date == tomorrow:
                tomorrow_total += period_kwh_p50

            # Hourly buckets (key = hour start as "YYYY-MM-DD HH:00:00")
            hour_key = period_start.replace(minute=0, second=0, microsecond=0).strftime(
                "%Y-%m-%d %H:%M:%S"
            )

            if period_date == today:
                hourly_p50_today[hour_key]  = hourly_p50_today.get(hour_key, 0) + int(period_wh_p50)
                hourly_p10_today[hour_key]  = hourly_p10_today.get(hour_key, 0) + int(vals["p10"] * 500)
            elif period_date == tomorrow:
                hourly_p50_tomorrow[hour_key] = hourly_p50_tomorrow.get(hour_key, 0) + int(period_wh_p50)

            # Track dawn time for each date
            if vals["p50"] * 1000 > PV_GENERATION_THRESHOLD_W:  # kW -> W
                date_str = period_date.strftime("%Y-%m-%d")
                if date_str not in dawn_times:
                    dawn_times[date_str] = period_start

        # Remaining today (future hours only)
        now_hour = now_local.replace(minute=0, second=0, microsecond=0)
        remaining_today_kwh = sum(
            wh / 1000.0
            for key, wh in hourly_p50_today.items()
            if datetime.strptime(key, "%Y-%m-%d %H:%M:%S") >= now_hour
        )

        # Current and next hour watts
        current_hour_key = now_hour.strftime("%Y-%m-%d %H:%M:%S")
        next_hour_key    = (now_hour + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
        current_hour_w   = hourly_p50_today.get(current_hour_key, 0)
        next_hour_w      = hourly_p50_today.get(next_hour_key, 0)

        return {
            "todayKwh":          round(today_total, 1),
            "tomorrowKwh":       round(tomorrow_total, 1),
            "remainingTodayKwh": round(remaining_today_kwh, 1),
            "currentHourWatts":  current_hour_w,
            "nextHourWatts":     next_hour_w,
            "_hourly_p50_today":    hourly_p50_today,
            "_hourly_p10_today":    hourly_p10_today,
            "_hourly_p50_tomorrow": hourly_p50_tomorrow,
            "_dawn_times":          dawn_times,
        }

    def _enrich_forecast(self, combined):
        """Add bias-corrected states to a combined forecast dict."""
        enriched = dict(combined)
        factor   = self._correction_factor

        raw_today    = combined.get("todayKwh", 0.0)
        raw_tomorrow = combined.get("tomorrowKwh", 0.0)

        enriched["biasFactor"]          = round(factor, 3)
        enriched["correctedTodayKwh"]   = round(raw_today * factor, 1)
        enriched["correctedTomorrowKwh"] = round(raw_tomorrow * factor, 1)

        return enriched

    def _empty_forecast(self, reason=""):
        """Return a zeroed forecast when no data is available."""
        self.logger.warning(f"Returning empty forecast: {reason}")
        return {
            "todayKwh":              0.0,
            "tomorrowKwh":           0.0,
            "correctedTodayKwh":     0.0,
            "correctedTomorrowKwh":  0.0,
            "biasFactor":            1.0,
            "remainingTodayKwh":     0.0,
            "currentHourWatts":      0,
            "nextHourWatts":         0,
            "forecastStatus":        f"No data: {reason}",
            "lastUpdate":            datetime.now().strftime("%H:%M:%S"),
            "_hourly_p50_today":     {},
            "_hourly_p10_today":     {},
            "_hourly_p50_tomorrow":  {},
            "_dawn_times":           {},
        }

    # ================================================================
    # Bias Correction
    # ================================================================

    def _compute_correction_factor(self, records):
        """Compute seasonal bias correction factor from accuracy records.

        Algorithm:
        1. Try same calendar month (prefer seasonal match, need >= 3 records)
        2. Fall back to last 30 records if not enough same-month data
        3. factor = mean(actual / forecast), clamped to [0.5, 1.5]
        4. Requires >= 3 records to activate (returns 1.0 if insufficient data)

        Args:
            records: list of accuracy record dicts

        Returns:
            float: correction factor (1.0 if insufficient data)
        """
        if not records:
            return 1.0

        current_month = datetime.now().strftime("%Y-%m")

        # Try same calendar month across all years
        same_month = [
            r for r in records
            if r.get("month", "").endswith("-" + current_month.split("-")[1])
            and r.get("forecast_kwh", 0) > 0
        ]

        if len(same_month) >= 3:
            factors = [r["factor"] for r in same_month if 0.1 < r.get("factor", 0) < 5.0]
            if len(factors) >= 3:
                raw_factor = sum(factors) / len(factors)
                return round(max(0.5, min(1.5, raw_factor)), 4)

        # Fall back to last 30 valid records
        valid = [
            r for r in records[-30:]
            if r.get("forecast_kwh", 0) > 0 and 0.1 < r.get("factor", 0) < 5.0
        ]

        if len(valid) < 3:
            return 1.0  # not enough data

        raw_factor = sum(r["factor"] for r in valid) / len(valid)
        return round(max(0.5, min(1.5, raw_factor)), 4)

    # ================================================================
    # Disk Cache
    # ================================================================

    def _cache_path(self, site_id):
        return os.path.join(self.data_dir, f"solcast_cache_{site_id}.json")

    def _accuracy_path(self):
        return os.path.join(self.data_dir, "forecast_accuracy_records.json")

    def _load_site_cache(self, site_id):
        path = self._cache_path(site_id)
        try:
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception as e:
            self.logger.debug(f"Cannot read site cache {path}: {e}")
        return None

    def _save_site_cache(self, site_id, forecasts):
        path = self._cache_path(site_id)
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"cached_time": time.time(), "forecasts": forecasts}, f)
        except Exception as e:
            self.logger.warning(f"Cannot write site cache {path}: {e}")

    def _load_accuracy_records(self):
        path = self._accuracy_path()
        try:
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception as e:
            self.logger.debug(f"Cannot read accuracy records: {e}")
        return []

    def _save_accuracy_records(self, records):
        path = self._accuracy_path()
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(records, f, indent=2)
        except Exception as e:
            self.logger.error(f"Cannot write accuracy records: {e}")

    def _save_cache(self, combined):
        """Save combined forecast to a single composite cache file."""
        path = os.path.join(self.data_dir, "solcast_combined_cache.json")
        try:
            # Strip non-serialisable items (hourly dicts are fine)
            saveable = {k: v for k, v in combined.items() if not k.startswith("_dawn")}
            saveable["_cached_time"] = time.time()
            with open(path, "w", encoding="utf-8") as f:
                json.dump(saveable, f, indent=2)
        except Exception as e:
            self.logger.debug(f"Cannot write combined cache: {e}")

    # ================================================================
    # Timezone Helpers
    # ================================================================

    def _now_local(self):
        """Return current datetime in Europe/London timezone."""
        if PYTZ_AVAILABLE and LONDON_TZ:
            return datetime.now(tz=LONDON_TZ)
        # Fallback: use local system time
        return datetime.now()

    def _parse_period_end(self, period_end_str):
        """Parse Solcast period_end string to local datetime.

        Solcast returns UTC times like "2026-03-26T14:30:00.0000000Z".
        Converts to Europe/London local time.
        """
        # Strip fractional seconds and Z, parse as UTC
        clean = period_end_str.rstrip("Z").split(".")[0]
        dt_utc = datetime.strptime(clean, "%Y-%m-%dT%H:%M:%S").replace(
            tzinfo=timezone.utc
        )
        if PYTZ_AVAILABLE and LONDON_TZ:
            return dt_utc.astimezone(LONDON_TZ)
        return dt_utc
