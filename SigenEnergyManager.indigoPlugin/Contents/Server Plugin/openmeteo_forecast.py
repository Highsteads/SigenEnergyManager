#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    openmeteo_forecast.py
# Description: Open-Meteo solar forecast client - models all 4 PV arrays directly
#              using hourly Global Tilted Irradiance (GTI) data. No API key needed.
#              Free tier: 10,000 calls/day. 4 arrays x ~48 calls/day = well within limit.
#              Exposes the same public interface as SolcastForecast so plugin.py
#              needs only a simple constructor swap.
# Author:      CliveS & Claude Sonnet 4.6
# Date:        19-04-2026
# Version:     1.0

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
# Site configuration — Highsteads, Medomsley (54.882N, 1.818W)
# ============================================================

LATITUDE  = 54.882
LONGITUDE = -1.818

# Four PV arrays — specs from original solar quotation (Alps Electrical, 2025)
# azimuth: 0=South, 90=West, -90=East, 180=North  (Open-Meteo convention)
# tilt:    degrees from horizontal (0=flat, 90=vertical)
# kwp:     peak power of the array (kW)
# shade:   shading correction factor from system design survey (dimensionless, 0-1)
ARRAYS = [
    {"name": "South",     "tilt": 34, "azimuth": -13,  "kwp": 4.275, "shade": 0.86},
    {"name": "West",      "tilt": 32, "azimuth":  77,  "kwp": 2.850, "shade": 0.85},
    {"name": "East",      "tilt": 32, "azimuth": -103, "kwp": 4.275, "shade": 0.87},
    {"name": "NorthEast", "tilt": 39, "azimuth": -118, "kwp": 2.850, "shade": 0.84},
]

# ============================================================
# System constants
# ============================================================

INVERTER_CAP_KW = 10.0   # Sigenergy 10 kW inverter limit

# Performance ratio: accounts for inverter efficiency (~97%), wiring losses (~2%),
# temperature derating (~3%), soiling (~2%). Shading is separate via shade factor above.
PERFORMANCE_RATIO = 0.90

# Open-Meteo API
OPENMETEO_URL   = "https://api.open-meteo.com/v1/forecast"
REQUEST_TIMEOUT = 30    # seconds per array call
CACHE_TTL       = 1800  # 30 minutes — well within 10,000 call/day free tier

# Dawn detection: first hourly slot whose output exceeds this threshold (Wh).
# 500 Wh/h ~ 500 W average sustained; below this = pre-dawn / post-dusk.
PV_GENERATION_THRESHOLD_WH = 500

# Bias correction
# For Open-Meteo the factor should settle near 1.0 (all 4 arrays modelled correctly).
# Retained so small PR / shade inaccuracies self-calibrate over time.
BIAS_CORRECTION_MAX          = 1.5   # tighter than Solcast (was 3.0 for 2-array workaround)
BIAS_CORRECTION_MIN          = 0.5
MIN_CALIBRATION_FORECAST_KWH = 10.0  # ignore days where forecast < 10 kWh (overcast outliers)

# Path of the optimiser input file (read by openmeteo_battery_optimiser.py).
OPTIMISER_FORECAST_FILE = (
    "/Library/Application Support/Perceptive Automation/"
    "Python Scripts/openmeteo_forecast.json"
)


class OpenMeteoForecast:
    """Open-Meteo solar forecast client.

    Models all 4 PV arrays individually using hourly GTI (Global Tilted Irradiance)
    data from the Open-Meteo free weather API. No API key required.

    GTI → kWh conversion per array per hour:
        P_ac (kWh) = GTI (W/m²) / 1000 × kWp × PR × shade_factor

    Results from all 4 arrays are summed per hour and capped at the 10 kW inverter
    limit. Bias correction is retained so the system self-calibrates if PR or shade
    factors need fine-tuning (factor will converge to 1.0 if the model is accurate).

    Public interface is identical to SolcastForecast so plugin.py needs only a
    constructor swap (no API key / site ID params).

    Also writes solcast_forecast.json to the Python Scripts folder on every refresh
    so the battery optimiser script receives up-to-date Open-Meteo data with no
    changes to the optimiser or its Indigo schedule.
    """

    def __init__(self, data_dir, logger=None):
        """Initialise the Open-Meteo forecast client.

        Args:
            data_dir: Directory for cache and accuracy record files
                      (Preferences/Plugins/.../com.clives.indigoplugin.sigenergy-energy-manager/)
            logger:   Optional logger instance.
        """
        self.data_dir = data_dir
        self.logger   = logger or logging.getLogger("SigenEnergyManager.OpenMeteo")

        # In-memory cache: last successfully combined forecast dict
        self._cached_forecast = None
        self._cached_time     = 0.0

        # Bias correction state
        self._morning_forecast_kwh = 0.0  # captured at 00:05
        self._correction_factor    = 1.0  # current seasonal factor

        # Pre-warm in-memory cache from disk so restarts don't lose data
        self._load_combined_cache()

    # ================================================================
    # Public API
    # ================================================================

    def fetch_forecast(self, force=False):
        """Fetch and return combined forecast from all 4 arrays.

        Respects 30-minute cache unless force=True.

        Returns dict with keys:
            todayKwh, tomorrowKwh, correctedTodayKwh, correctedTomorrowKwh,
            biasFactor, currentHourWatts, nextHourWatts, remainingTodayKwh,
            forecastStatus, lastUpdate,
            _hourly_p50_today  ({"YYYY-MM-DD HH:00:00": wh_int})
            _hourly_p50_tomorrow (same format, for tomorrow's date)
            _dawn_times ({"YYYY-MM-DD": tz-aware datetime of first PV > threshold})
        """
        if not REQUESTS_AVAILABLE:
            self.logger.error("[OpenMeteo] requests not available — cannot fetch forecast")
            return self._empty_forecast("requests not installed")

        now       = time.time()
        cache_age = now - self._cached_time

        if not force and cache_age < CACHE_TTL and self._cached_forecast:
            self.logger.debug(
                f"[OpenMeteo] Using cached forecast (age {cache_age:.0f}s / TTL {CACHE_TTL}s)"
            )
            return self._enrich_forecast(self._cached_forecast)

        # Fetch all 4 arrays
        try:
            combined = self._fetch_all_arrays()
        except Exception as e:
            self.logger.error(f"[OpenMeteo] Fetch failed: {e}")
            if self._cached_forecast:
                self.logger.warning("[OpenMeteo] Returning stale cached forecast")
                return self._enrich_forecast(self._cached_forecast)
            return self._empty_forecast(f"Fetch error: {e}")

        if combined is None:
            self.logger.error("[OpenMeteo] All array fetches failed")
            if self._cached_forecast:
                return self._enrich_forecast(self._cached_forecast)
            return self._empty_forecast("All array fetches failed")

        combined["forecastStatus"] = "OK"
        combined["lastUpdate"]     = datetime.now().strftime("%H:%M:%S")

        self._save_cache(combined)
        self._cached_forecast = combined
        self._cached_time     = now

        # Write forecast file for the battery optimiser script
        self._write_optimiser_file(combined)

        return self._enrich_forecast(combined)

    def capture_morning_forecast(self):
        """Record today's raw total kWh as the morning forecast baseline.

        Call this at ~00:05 each day. Used for end-of-day bias calibration.
        """
        if self._cached_forecast:
            self._morning_forecast_kwh = self._cached_forecast.get("todayKwh", 0.0)
            self.logger.debug(
                f"[OpenMeteo] Captured morning forecast: {self._morning_forecast_kwh:.1f} kWh"
            )
        else:
            self._morning_forecast_kwh = 0.0

    def record_accuracy(self, actual_pv_kwh):
        """Record today's forecast vs actual PV for bias correction.

        Call this at midnight after the daily energy accumulator is finalised.

        Args:
            actual_pv_kwh: Actual PV generation today (kWh from Modbus accumulators).
        """
        if self._morning_forecast_kwh <= 0.0:
            self.logger.debug("[OpenMeteo] No morning forecast captured — skipping record")
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
        if len(records) > 365:
            records = records[-365:]
        self._save_accuracy_records(records)

        self.logger.info(
            f"[OpenMeteo] Accuracy: forecast={self._morning_forecast_kwh:.1f} kWh, "
            f"actual={actual_pv_kwh:.1f} kWh, factor={record['factor']:.3f}"
        )

        self._correction_factor = self._compute_correction_factor(records)
        self.logger.info(
            f"[OpenMeteo] Updated bias correction factor: {self._correction_factor:.3f}"
        )

        self._morning_forecast_kwh = 0.0

    def load_correction_factor(self):
        """Load and compute correction factor from saved accuracy records.

        Call on plugin startup so bias correction is active immediately.
        """
        records = self._load_accuracy_records()
        self._correction_factor = self._compute_correction_factor(records)
        self.logger.info(
            f"[OpenMeteo] Loaded bias correction factor from {len(records)} records: "
            f"{self._correction_factor:.3f}"
        )

    # ================================================================
    # API Calls — 4 separate requests, one per array
    # ================================================================

    def _fetch_all_arrays(self):
        """Fetch GTI from Open-Meteo for all 4 arrays and combine.

        Makes 4 sequential HTTP calls (one per array). Each returns hourly GTI
        in W/m² for the requested tilt/azimuth in Europe/London local time.

        Conversion per array per hour:
            kWh = (GTI_wm2 / 1000) * kWp * PR * shade_factor

        Returns combined dict or None if all calls failed.
        """
        now_local = self._now_local()
        today     = now_local.date()
        tomorrow  = today + timedelta(days=1)

        # Accumulate combined kWh per local-time hour key before inverter cap
        # key format: "YYYY-MM-DD HH:00:00"
        hourly_raw = {}
        arrays_ok  = 0

        for array_cfg in ARRAYS:
            gti_data = self._fetch_array(array_cfg)
            if gti_data is None:
                self.logger.warning(
                    f"[OpenMeteo] {array_cfg['name']} array fetch failed — skipping"
                )
                continue

            arrays_ok += 1
            factor = array_cfg["kwp"] * PERFORMANCE_RATIO * array_cfg["shade"]

            for time_str, gti_wm2 in gti_data:
                # time_str from API: "YYYY-MM-DDTHH:MM" (local BST/GMT)
                try:
                    dt_naive = datetime.strptime(time_str, "%Y-%m-%dT%H:%M")
                except ValueError:
                    continue
                key = dt_naive.strftime("%Y-%m-%d %H:%M:%S")
                kwh = (gti_wm2 / 1000.0) * factor
                hourly_raw[key] = hourly_raw.get(key, 0.0) + kwh

        if arrays_ok == 0:
            return None

        if arrays_ok < len(ARRAYS):
            self.logger.warning(
                f"[OpenMeteo] Only {arrays_ok}/{len(ARRAYS)} arrays fetched"
            )

        # Apply inverter cap and split into today / tomorrow buckets
        hourly_today    = {}
        hourly_tomorrow = {}
        today_total     = 0.0
        tomorrow_total  = 0.0
        dawn_times      = {}

        for key in sorted(hourly_raw.keys()):
            raw_kwh    = hourly_raw[key]
            capped_kwh = min(raw_kwh, INVERTER_CAP_KW)
            wh_int     = int(capped_kwh * 1000)

            try:
                dt_naive  = datetime.strptime(key, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue

            slot_date = dt_naive.date()

            if slot_date == today:
                hourly_today[key] = wh_int
                today_total      += capped_kwh
            elif slot_date == tomorrow:
                hourly_tomorrow[key] = wh_int
                tomorrow_total      += capped_kwh
            # day-after-tomorrow data available from forecast_days=3 but not needed here

            # Dawn tracking: first slot above threshold for each date
            if wh_int >= PV_GENERATION_THRESHOLD_WH:
                date_str = slot_date.strftime("%Y-%m-%d")
                if date_str not in dawn_times:
                    if PYTZ_AVAILABLE and LONDON_TZ:
                        # is_dst=False handles the autumn fallback ambiguity
                        # (01:00–02:00 BST exists twice on the last Sunday in
                        # October). Without it, pytz raises AmbiguousTimeError
                        # and crashes the forecast parse once a year.
                        dt_aware = LONDON_TZ.localize(dt_naive, is_dst=False)
                    else:
                        dt_aware = dt_naive
                    dawn_times[date_str] = dt_aware

        # Remaining today: future hours only (naive comparison against now)
        now_hour_naive = now_local.replace(minute=0, second=0,
                                           microsecond=0, tzinfo=None)
        remaining_today_kwh = sum(
            wh / 1000.0
            for key, wh in hourly_today.items()
            if datetime.strptime(key, "%Y-%m-%d %H:%M:%S") >= now_hour_naive
        )

        # Current and next hour watts
        cur_key = now_hour_naive.strftime("%Y-%m-%d %H:%M:%S")
        nxt_key = (now_hour_naive + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")

        return {
            "todayKwh":             round(today_total, 1),
            "tomorrowKwh":          round(tomorrow_total, 1),
            "remainingTodayKwh":    round(remaining_today_kwh, 1),
            "currentHourWatts":     hourly_today.get(cur_key, 0),
            "nextHourWatts":        hourly_today.get(nxt_key, 0),
            "_hourly_p50_today":    hourly_today,
            "_hourly_p50_tomorrow": hourly_tomorrow,
            "_dawn_times":          dawn_times,
        }

    def _fetch_array(self, array_cfg):
        """Fetch hourly GTI for one array from the Open-Meteo API.

        Checks per-array disk cache first; fetches from API if stale.

        Args:
            array_cfg: dict with name, tilt, azimuth (other fields not needed here).

        Returns:
            List of (time_str, gti_wm2) tuples in local time order, or None on failure.
        """
        # Disk cache check
        cached = self._load_array_cache(array_cfg["name"])
        if cached:
            age = time.time() - cached.get("cached_time", 0)
            if age < CACHE_TTL:
                self.logger.debug(
                    f"[OpenMeteo] {array_cfg['name']}: disk cache (age {age:.0f}s)"
                )
                return cached.get("data", [])

        params = {
            "latitude":   LATITUDE,
            "longitude":  LONGITUDE,
            "hourly":     "global_tilted_irradiance",
            "tilt":       array_cfg["tilt"],
            "azimuth":    array_cfg["azimuth"],
            "timezone":   "Europe/London",
            "forecast_days": 3,
            "timeformat": "iso8601",
        }

        try:
            response = requests.get(
                OPENMETEO_URL,
                params=params,
                timeout=REQUEST_TIMEOUT,
            )

            if response.status_code != 200:
                self.logger.error(
                    f"[OpenMeteo] HTTP {response.status_code} for "
                    f"{array_cfg['name']}: {response.text[:200]}"
                )
                if cached:
                    return cached.get("data", [])
                return None

            data   = response.json()
            hourly = data.get("hourly", {})
            times  = hourly.get("time", [])
            gti    = hourly.get("global_tilted_irradiance", [])

            if not times or not gti or len(times) != len(gti):
                self.logger.error(
                    f"[OpenMeteo] Unexpected response for {array_cfg['name']}: "
                    f"{len(times)} times, {len(gti)} GTI values"
                )
                if cached:
                    return cached.get("data", [])
                return None

            result = [
                (times[i], float(gti[i]) if gti[i] is not None else 0.0)
                for i in range(len(times))
            ]

            self._save_array_cache(array_cfg["name"], result)
            self.logger.debug(
                f"[OpenMeteo] {array_cfg['name']}: fetched {len(result)} hours"
            )
            return result

        except requests.exceptions.Timeout:
            self.logger.warning(
                f"[OpenMeteo] Timeout fetching {array_cfg['name']}"
            )
            if cached:
                return cached.get("data", [])
            return None
        except Exception as e:
            self.logger.error(
                f"[OpenMeteo] Error fetching {array_cfg['name']}: {e}"
            )
            if cached:
                return cached.get("data", [])
            return None

    # ================================================================
    # Enrichment & Empty Result
    # ================================================================

    def _enrich_forecast(self, combined):
        """Add bias-corrected totals to the combined forecast dict."""
        enriched     = dict(combined)
        factor       = self._correction_factor
        raw_today    = combined.get("todayKwh", 0.0)
        raw_tomorrow = combined.get("tomorrowKwh", 0.0)

        enriched["biasFactor"]           = round(factor, 3)
        enriched["correctedTodayKwh"]    = round(raw_today    * factor, 1)
        enriched["correctedTomorrowKwh"] = round(raw_tomorrow * factor, 1)
        return enriched

    def _empty_forecast(self, reason=""):
        """Return a zeroed forecast dict when no data is available."""
        self.logger.warning(f"[OpenMeteo] Empty forecast: {reason}")
        return {
            "todayKwh":             0.0,
            "tomorrowKwh":          0.0,
            "correctedTodayKwh":    0.0,
            "correctedTomorrowKwh": 0.0,
            "biasFactor":           1.0,
            "remainingTodayKwh":    0.0,
            "currentHourWatts":     0,
            "nextHourWatts":        0,
            "forecastStatus":       f"No data: {reason}",
            "lastUpdate":           datetime.now().strftime("%H:%M:%S"),
            "_hourly_p50_today":    {},
            "_hourly_p50_tomorrow": {},
            "_dawn_times":          {},
        }

    # ================================================================
    # Bias Correction
    # ================================================================

    def _compute_correction_factor(self, records):
        """Compute seasonal bias correction factor from accuracy records.

        Algorithm:
        1. Try same calendar month (prefer seasonal match, need >= 3 valid records)
        2. Fall back to last 30 valid records
        3. factor = mean(actual / forecast), clamped to [0.5, 1.5]
        4. Returns 1.0 if fewer than 3 records available

        Days where forecast < MIN_CALIBRATION_FORECAST_KWH are excluded
        (overcast days where accuracy is not meaningful for calibration).
        """
        if not records:
            return 1.0

        current_month = datetime.now().strftime("%Y-%m")

        same_month = [
            r for r in records
            if r.get("month", "").endswith("-" + current_month.split("-")[1])
            and r.get("forecast_kwh", 0) >= MIN_CALIBRATION_FORECAST_KWH
        ]

        if len(same_month) >= 3:
            factors = [r["factor"] for r in same_month
                       if 0.1 < r.get("factor", 0) < BIAS_CORRECTION_MAX * 2]
            if len(factors) >= 3:
                raw_factor = sum(factors) / len(factors)
                return round(max(BIAS_CORRECTION_MIN, min(BIAS_CORRECTION_MAX, raw_factor)), 4)

        valid = [
            r for r in records[-30:]
            if r.get("forecast_kwh", 0) >= MIN_CALIBRATION_FORECAST_KWH
            and 0.1 < r.get("factor", 0) < BIAS_CORRECTION_MAX * 2
        ]

        if len(valid) < 3:
            return 1.0

        raw_factor = sum(r["factor"] for r in valid) / len(valid)
        return round(max(BIAS_CORRECTION_MIN, min(BIAS_CORRECTION_MAX, raw_factor)), 4)

    # ================================================================
    # Optimiser forecast file writer
    # ================================================================

    def _write_optimiser_file(self, combined):
        """Write openmeteo_forecast.json for the battery optimiser script.

        The optimiser reads {"hourly": {"YYYY-MM-DDTHH:MM:SSZ": {"kwh": float}}, ...}
        with UTC timestamp keys. Our internal dict uses local (BST/GMT) keys so
        this method converts them using pytz (falls back to UTC+1 if pytz absent).

        Writes raw (uncorrected) hourly values — with bias_factor ~1.0 for a
        properly-modelled 4-array system, raw and corrected are essentially equal.
        The corrected tomorrow_kwh total is written for export viability checks.
        """
        try:
            now_utc       = datetime.now(timezone.utc)
            today_date    = now_utc.date()
            tomorrow_date = (now_utc + timedelta(days=1)).date()

            hourly_out   = {}
            today_kwh    = 0.0
            tomorrow_kwh = 0.0

            for bucket_name, target_date in (
                ("_hourly_p50_today",    today_date),
                ("_hourly_p50_tomorrow", tomorrow_date),
            ):
                bucket = combined.get(bucket_name, {})
                for local_key, wh_int in bucket.items():
                    utc_key = self._local_key_to_utc(local_key)
                    if utc_key is None:
                        continue
                    kwh = round(wh_int / 1000.0, 3)
                    hourly_out[utc_key] = {"kwh": kwh}
                    if target_date == today_date:
                        today_kwh += kwh
                    else:
                        tomorrow_kwh += kwh

            # Use bias-corrected tomorrow total for export-viability check in optimiser
            factor            = self._correction_factor
            corrected_tmrw    = round(tomorrow_kwh * factor, 2)

            # Determine cache_age_hours from cached_time
            cached_time = self._cached_time or time.time()
            cache_age   = round((time.time() - cached_time) / 3600.0, 2)

            forecast_doc = {
                "generated_at":     now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "source":           "open_meteo",
                "cache_age_hours":  cache_age,
                "arrays":           [a["name"] for a in ARRAYS],
                "bias_factor":      factor,
                "today_kwh":        round(today_kwh, 2),
                "tomorrow_kwh":     corrected_tmrw,
                "hourly":           hourly_out,
            }

            with open(OPTIMISER_FORECAST_FILE, "w", encoding="utf-8") as f:
                json.dump(forecast_doc, f, indent=2)

            self.logger.debug(
                f"[OpenMeteo] Wrote optimiser file: {len(hourly_out)} slots, "
                f"today {today_kwh:.1f} kWh, tomorrow {corrected_tmrw:.1f} kWh"
            )

        except Exception as e:
            self.logger.error(f"[OpenMeteo] Failed to write optimiser file: {e}")

    def _local_key_to_utc(self, local_key):
        """Convert internal local-time key "YYYY-MM-DD HH:00:00" to UTC ISO string.

        Open-Meteo returns times in Europe/London timezone. This converts them
        correctly across BST (UTC+1) and GMT (UTC+0) transitions.

        Returns "YYYY-MM-DDTHH:MM:SSZ" or None on parse failure.
        """
        try:
            dt_naive = datetime.strptime(local_key, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None

        try:
            if PYTZ_AVAILABLE and LONDON_TZ:
                # is_dst=False handles the autumn fallback ambiguity safely
                dt_local = LONDON_TZ.localize(dt_naive, is_dst=False)
                dt_utc   = dt_local.astimezone(timezone.utc)
            else:
                # Fallback: assume BST (UTC+1) — accurate April-October
                dt_utc = dt_naive.replace(tzinfo=timezone.utc) - timedelta(hours=1)
            return dt_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
        except Exception:
            return None

    # ================================================================
    # Disk Cache
    # ================================================================

    def _array_cache_path(self, array_name):
        return os.path.join(self.data_dir, f"openmeteo_cache_{array_name}.json")

    def _combined_cache_path(self):
        return os.path.join(self.data_dir, "openmeteo_combined_cache.json")

    def _accuracy_path(self):
        return os.path.join(self.data_dir, "openmeteo_accuracy_records.json")

    def _load_array_cache(self, array_name):
        path = self._array_cache_path(array_name)
        try:
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception as e:
            self.logger.debug(f"[OpenMeteo] Cannot read array cache {array_name}: {e}")
        return None

    def _save_array_cache(self, array_name, data):
        path = self._array_cache_path(array_name)
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"cached_time": time.time(), "data": data}, f)
        except Exception as e:
            self.logger.warning(f"[OpenMeteo] Cannot write array cache {array_name}: {e}")

    def _load_accuracy_records(self):
        path = self._accuracy_path()
        try:
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception as e:
            self.logger.debug(f"[OpenMeteo] Cannot read accuracy records: {e}")
        return []

    def _save_accuracy_records(self, records):
        path = self._accuracy_path()
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(records, f, indent=2)
        except Exception as e:
            self.logger.error(f"[OpenMeteo] Cannot write accuracy records: {e}")

    def _load_combined_cache(self):
        """Pre-warm in-memory cache from disk on plugin startup.

        Prevents a forecast data gap for up to 30 minutes after restart.
        Reconstructs _dawn_times from hourly data since datetime objects
        cannot be JSON-serialised and are stripped by _save_cache().
        """
        path = self._combined_cache_path()
        if not os.path.exists(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            cached_time = data.pop("_cached_time", 0.0)
            self._cached_forecast = data
            self._cached_time     = cached_time

            # Reconstruct dawn_times from hourly buckets
            dawn_times = {}
            try:
                import pytz as _pytz
                _london = _pytz.timezone("Europe/London")
            except ImportError:
                _london = None

            for kwh_dict in (
                data.get("_hourly_p50_today", {}),
                data.get("_hourly_p50_tomorrow", {}),
            ):
                for key in sorted(kwh_dict.keys()):
                    if kwh_dict[key] >= PV_GENERATION_THRESHOLD_WH:
                        try:
                            dt_naive = datetime.strptime(key, "%Y-%m-%d %H:%M:%S")
                            # is_dst=False to avoid AmbiguousTimeError on
                            # the autumn fallback Sunday (01:00–02:00 occurs twice).
                            dt       = (
                                _london.localize(dt_naive, is_dst=False)
                                if _london else dt_naive
                            )
                            date_str = dt_naive.strftime("%Y-%m-%d")
                            if date_str not in dawn_times:
                                dawn_times[date_str] = dt
                        except Exception:
                            continue

            self._cached_forecast["_dawn_times"] = dawn_times

            age_h = (time.time() - cached_time) / 3600.0
            stale_h = (CACHE_TTL / 3600.0) * 6   # warn if older than 3 hours
            if age_h > stale_h:
                self.logger.warning(
                    f"[OpenMeteo] Stale disk cache (age {age_h:.1f}h) — data may be outdated"
                )
            else:
                self.logger.info(
                    f"[OpenMeteo] Pre-warmed from disk cache (age {age_h:.1f}h)"
                )
        except Exception as e:
            self.logger.warning(f"[OpenMeteo] Cannot load combined cache: {e}")

    def _save_cache(self, combined):
        """Persist combined forecast to disk (strips non-serialisable _dawn_times)."""
        path = self._combined_cache_path()
        try:
            saveable = {k: v for k, v in combined.items() if not k.startswith("_dawn")}
            saveable["_cached_time"] = time.time()
            with open(path, "w", encoding="utf-8") as f:
                json.dump(saveable, f, indent=2)
        except Exception as e:
            self.logger.debug(f"[OpenMeteo] Cannot write combined cache: {e}")

    # ================================================================
    # Timezone Helper
    # ================================================================

    def _now_local(self):
        """Return current datetime in Europe/London timezone."""
        if PYTZ_AVAILABLE and LONDON_TZ:
            return datetime.now(tz=LONDON_TZ)
        return datetime.now()
