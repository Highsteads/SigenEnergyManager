#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    openmeteo_forecast.py
# Description: Open-Meteo solar forecast client - models all 4 PV arrays directly
#              using hourly Global Tilted Irradiance (GTI) data. No API key needed.
#              Free tier: 10,000 calls/day. 4 arrays x ~48 calls/day = well within limit.
#              Exposes the same public interface as SolcastForecast so plugin.py
#              needs only a simple constructor swap.
# Author:      CliveS & Claude Opus 4.8
# Date:        04-06-2026
# Version:     1.7 (remainingTodayKwh bias-corrected + current hour pro-rated)
# 1.7 — remainingTodayKwh was summed straight off the RAW hourly p50 buckets while
#       the headline forecast it sits beside (correctedTodayKwh) is bias-corrected,
#       so the two figures were on different scales — 19-Jul-2026 the Energy page
#       read 38.3 kWh today, forecast 53, remaining 25.3 (raw sum 57.7 x 0.915 =
#       52.8). It also counted the WHOLE current hour as still to come, overstating
#       the figure by up to a full peak hour (~7 kWh at midday). Both fixed in one
#       new helper, `_remaining_today_kwh`, now the single owner of the calculation
#       for both the fetch path and the enrichment path. currentHourWatts /
#       nextHourWatts stay RAW — they're instantaneous-shape diagnostics, not day
#       totals, and the optimiser reads the corrected shape from the hourly file.
# 1.6 — zoneinfo tz fallback, per-array cache day-shift guard, instance arrays.
# 1.5 — HTTP 5xx (502/503/504) from Open-Meteo now logged at WARNING (transient
#       server-side gateway blip; falls back to cached/partial forecast) instead
#       of a red ERROR. 4xx still ERROR. Fixes recurring "HTTP 502" ERROR spam.
# 1.4 — one-shot retry on transient network errors (Timeout, ConnectionError,
#       ChunkedEncodingError — the latter covering the SSL UNEXPECTED_EOF
#       hiccups Open-Meteo throws occasionally). All transient network errors
#       now log at WARNING, not ERROR — they're expected, the cache fallback
#       and 3-of-4 array path already handle them gracefully. ERROR is
#       reserved for HTTP non-200, malformed JSON, and genuinely unexpected
#       exceptions. No behaviour change when retry succeeds (silent except
#       for one WARNING on the first failed attempt).
# 1.3 — magnitude-conditional bias correction (replaces v1.2's "no correction").
#       Analysis of 31 days showed err% vs forecast_kwh r = -0.462: the model
#       under-forecasts on moderate-forecast days (25-45 kWh, factor ~1.2-1.34)
#       and over-forecasts on high-forecast clear days (>55 kWh, factor ~0.88).
#       A single flat factor cancels these out (v1.2's experiment); a per-band
#       table follows the shape and projects MAPE 19.8% -> ~14-16%. Bands
#       computed via median(actual/forecast) of records within ±7.5 kWh of each
#       centre (17.5/30/40/50/65); bands with fewer than 3 samples inherit the
#       global sum-ratio factor. Hourly outputs scale uniformly by that day's
#       daily-total band factor — the correction is calibrated against daily
#       totals, not hourly slots.
# 1.2.1 — fix NameError in _write_optimiser_file debug log left over from the
#         v1.2 rename (`corrected_tmrw` → `tomorrow_kwh`). The error fired on
#         every forecast refresh in the plugin log but didn't affect the
#         output file itself.
# 1.2 — bias factor NO LONGER APPLIED to forecast totals. 31 days of records
#       showed the raw forecast had lower mean error (-2.5%) and lower MAPE
#       (18.7%) than any bias-corrected variant tried (MAPE 21.9-22.3%). Factor
#       still computed and stored on `biasFactor` for diagnostic display.
#       SUPERSEDED by v1.3 — the v1.2 analysis used a single flat factor; a
#       magnitude-conditional table beats both raw and flat.
# 1.1 — bias correction switched from arithmetic-mean-of-ratios to ratio-of-sums
#       (kWh-weighted). Removes an upward statistical bias that pushed the May
#       2026 factor to 1.163 vs the correct 1.150. See test_openmeteo_forecast.

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

# Europe/London comes from the ONE shared implementation (london_time.py).
# This module used to carry a module-level LONDON_TZ behind a PYTZ_AVAILABLE
# gate, and all four of its call sites degraded silently when that gate was
# False: three left the datetime NAIVE and the fourth fell back to a crude
# "+1 hour", which is simply wrong for the four winter months. Open-Meteo
# returns LOCAL wall-clock times, so every one of those is a dawn or a slot
# key landing in the wrong hour.
#
# This was the piece deliberately left out of the v5.55.3 sweep, because its
# dawn parse asks for `is_dst=False` to resolve the October fallback hour and
# zoneinfo spells that `fold=1` rather than a keyword — swapping the constant
# without that mapping would have broken the once-a-year path. The mapping now
# lives in `london_localise(prefer_dst=)` and is pinned by tests.
from london_time import london_localise, london_now


# ============================================================
# Site configuration — coordinates are NOT defaulted here.
# The plugin passes latitude / longitude into the constructor below,
# resolved from IndigoSecrets.py (LATITUDE / LONGITUDE) first, then
# PluginConfig (siteLatitude / siteLongitude).  If neither is set the
# plugin skips forecast init entirely with an ERROR log — this module
# is never instantiated with None coordinates.
# ============================================================

LATITUDE  = None
LONGITUDE = None

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

# Plausibility ceiling for a single GTI sample (W/m²). The solar constant is
# ~1367 W/m²; cloud-edge enhancement can push ground-level readings a little
# above clear-sky, so 1500 gives headroom while rejecting garbage-large values.
GTI_MAX_WM2 = 1500.0

# Performance ratio: accounts for inverter efficiency (~97%), wiring losses (~2%),
# temperature derating (~3%), soiling (~2%). Shading is separate via shade factor above.
PERFORMANCE_RATIO = 0.90

# Open-Meteo API
OPENMETEO_URL   = "https://api.open-meteo.com/v1/forecast"
REQUEST_TIMEOUT = 10    # seconds per array call (Open-Meteo answers <1s;
                        # 30s made a blackholed network stall a tick ~4 min)
# Ceiling for serving DISK CACHE as a fetch-failure fallback. Beyond this the
# cache is too stale to act on — better to report the failure than fabricate
# a confident 'OK' forecast from day-old data.
STALE_FALLBACK_TTL = 24 * 3600
CACHE_TTL       = 1800  # 30 minutes — well within 10,000 call/day free tier
RETRY_ATTEMPTS  = 2     # total attempts per array call (1 initial + 1 retry)
RETRY_BACKOFF_S = 2     # seconds between attempts on transient network errors

# Dawn detection: first hourly slot whose output exceeds this threshold (Wh).
# 500 Wh/h ~ 500 W average sustained; below this = pre-dawn / post-dusk.
PV_GENERATION_THRESHOLD_WH = 500

# Bias correction
# For Open-Meteo the factor should settle near 1.0 (all 4 arrays modelled correctly).
# Retained so small PR / shade inaccuracies self-calibrate over time.
BIAS_CORRECTION_MAX          = 1.5   # tighter than Solcast (was 3.0 for 2-array workaround)
BIAS_CORRECTION_MIN          = 0.5
MIN_CALIBRATION_FORECAST_KWH = 10.0  # ignore days where forecast < 10 kWh (overcast outliers)

# Magnitude-conditional bias bands (v1.3).
# Each centre defines a daily-forecast-kWh bucket; the correction factor at
# any raw forecast value is linearly interpolated between adjacent centres.
# Centres derived from the May 2026 analysis: error shape is non-uniform —
# under-forecast at moderate predictions, accurate at extremes, over-forecast
# on bright days. A flat factor (v1.2) cancels these out; a per-band table
# follows the shape.
BIAS_BAND_CENTRES_KWH = (17.5, 30.0, 40.0, 50.0, 65.0)
BIAS_BAND_HALF_WIDTH  = 7.5    # ±7.5 kWh window of records that define a band
MIN_BAND_SAMPLES      = 3       # below this, band inherits the global factor

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

    Also writes openmeteo_forecast.json to the Python Scripts folder on every refresh
    so the battery optimiser script receives up-to-date Open-Meteo data.
    """

    def __init__(self, data_dir, logger=None, latitude=None, longitude=None, arrays=None):
        """Initialise the Open-Meteo forecast client.

        Args:
            data_dir:  Directory for cache and accuracy record files
                       (Preferences/Plugins/.../com.clives.indigoplugin.sigenergy-energy-manager/).
            logger:    Optional logger instance.
            latitude:  Site latitude in degrees N.  Required — there is NO
                       built-in default.  Callers must resolve from
                       IndigoSecrets.py / PluginConfig before instantiating.
            longitude: Site longitude in degrees E (negative for W). Required.
            arrays:    List of array config dicts (see ARRAYS in this module for
                       the expected shape: name/tilt/azimuth/kwp/shade).  None
                       falls back to the module ARRAYS for back-compat — a
                       future plugin release will expose per-array config in
                       PluginConfig.xml.
        """
        if latitude is None or longitude is None:
            raise ValueError(
                "OpenMeteoForecast requires explicit latitude and longitude; "
                "set LATITUDE / LONGITUDE in IndigoSecrets.py or siteLatitude / "
                "siteLongitude in PluginConfig."
            )
        self.data_dir = data_dir
        self.logger   = logger or logging.getLogger("SigenEnergyManager.OpenMeteo")
        self.latitude  = float(latitude)
        self.longitude = float(longitude)
        self.arrays    = ARRAYS if arrays is None else list(arrays)

        # In-memory cache: last successfully combined forecast dict
        self._cached_forecast = None
        self._cached_time     = 0.0

        # Per-cycle network short-circuit (reset in _fetch_all_arrays)
        self._network_down_this_cycle = False

        # Bias correction state. The morning baseline is captured on the FIRST
        # complete fetch of each local day (a genuine day-ahead forecast), tagged
        # with its date and persisted so a restart doesn't drop the day's record.
        self._morning_forecast_kwh  = 0.0
        self._morning_forecast_date = None
        self._correction_factor    = 1.0  # overall kWh-weighted scalar (display only)
        self._correction_bands     = [(c, 1.0) for c in BIAS_BAND_CENTRES_KWH]

        # Pre-warm in-memory cache from disk so restarts don't lose data
        self._load_combined_cache()
        self._load_morning_baseline()

    # ================================================================
    # Public API
    # ================================================================

    def fetch_forecast(self, force=False):
        """Fetch and return combined forecast from all 4 arrays.

        Respects 30-minute cache unless force=True.

        Returns dict with keys:
            todayKwh, tomorrowKwh, correctedTodayKwh, correctedTomorrowKwh,
            biasFactor (overall scalar — display only),
            biasFactorToday, biasFactorTomorrow (per-day effective factors),
            biasBands (list of [centre_kwh, factor] pairs in ascending order),
            currentHourWatts, nextHourWatts, remainingTodayKwh,
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

        arrays_ok    = combined.get("arrays_ok", 0)
        arrays_total = combined.get("arrays_total", arrays_ok)
        is_partial   = arrays_ok < arrays_total

        if is_partial:
            # A partial fetch (one or more arrays missing) produces a falsely-low
            # daily total. A low tomorrow_solar inflates the battery manager's
            # import_kwh and triggers unnecessary grid import — the exact opposite
            # of the self-sufficiency goal. So a partial result must NEVER be
            # stamped "OK", and must NEVER clobber a complete cached forecast.
            prev          = self._cached_forecast
            prev_complete = bool(prev) and prev.get("arrays_ok", 0) >= prev.get("arrays_total", 1)
            if prev_complete:
                self.logger.warning(
                    f"[OpenMeteo] Partial fetch ({arrays_ok}/{arrays_total} arrays) — "
                    f"keeping the previous complete forecast rather than overwriting "
                    f"with a low total."
                )
                return self._enrich_forecast(prev)

            # No complete forecast to fall back on — serve the partial but flag it
            # clearly (status carries 'Partial N/M') so consumers can detect the
            # degraded total, and do NOT persist it so the next full fetch wins.
            combined["forecastStatus"] = f"Partial {arrays_ok}/{arrays_total}"
            combined["lastUpdate"]     = datetime.now().strftime("%H:%M:%S")
            self.logger.warning(
                f"[OpenMeteo] Partial fetch ({arrays_ok}/{arrays_total} arrays) and no "
                f"complete cache — serving degraded forecast (status "
                f"'{combined['forecastStatus']}'); not caching."
            )
            return self._enrich_forecast(combined)

        combined["forecastStatus"] = "OK"
        combined["lastUpdate"]     = datetime.now().strftime("%H:%M:%S")

        self._save_cache(combined)
        self._cached_forecast = combined
        self._cached_time     = now

        # First complete fetch of each LOCAL day = that day's "morning" baseline
        # for bias calibration (paired with the day's actual PV at the following
        # midnight). The baseline used to be captured AT midnight from the ~23:30
        # cache — a near-zero-lead hindcast of the day just ended, never the
        # day-ahead forecast the calibration is supposed to grade (v5.43 fix).
        today_str = self._now_local().strftime("%Y-%m-%d")
        if self._morning_forecast_date != today_str:
            self._morning_forecast_kwh  = combined.get("todayKwh", 0.0)
            self._morning_forecast_date = today_str
            self._save_morning_baseline()
            self.logger.debug(
                f"[OpenMeteo] Morning baseline captured for {today_str}: "
                f"{self._morning_forecast_kwh:.1f} kWh"
            )

        # Write forecast file for the battery optimiser script
        self._write_optimiser_file(combined)

        return self._enrich_forecast(combined)

    def _morning_baseline_path(self):
        return os.path.join(self.data_dir, "morning_baseline.json")

    def _save_morning_baseline(self):
        """Persist the day's baseline so a restart doesn't drop the day's record."""
        try:
            self._atomic_write_json(self._morning_baseline_path(),
                                    {"date": self._morning_forecast_date,
                                     "forecast_kwh": self._morning_forecast_kwh})
        except Exception as e:
            self.logger.debug(f"[OpenMeteo] Baseline save failed: {e}")

    def _load_morning_baseline(self):
        try:
            with open(self._morning_baseline_path(), encoding="utf-8") as f:
                data = json.load(f)
            self._morning_forecast_date = data.get("date")
            self._morning_forecast_kwh  = float(data.get("forecast_kwh", 0.0))
        except FileNotFoundError:
            pass
        except Exception as e:
            self.logger.debug(f"[OpenMeteo] Baseline load failed: {e}")

    def record_accuracy(self, actual_pv_kwh, date_str=None):
        """Record today's forecast vs actual PV for bias correction.

        Call this at midnight after the daily energy accumulator is finalised.

        Args:
            actual_pv_kwh: Actual PV generation today (kWh from Modbus accumulators).
            date_str:      Date string "YYYY-MM-DD" for the day being recorded.
                           Pass yesterday's date from _check_midnight to avoid
                           the off-by-one that occurs when datetime.now() has
                           already rolled over to the new day.
        """
        if self._morning_forecast_kwh <= 0.0:
            self.logger.debug("[OpenMeteo] No morning forecast captured — skipping record")
            return

        if date_str is None:
            date_str = datetime.now().strftime("%Y-%m-%d")

        # The baseline must belong to the day being recorded — a mismatch means
        # the day's capture was missed (restart before the first fetch, etc.)
        # and pairing actual PV with another day's forecast would poison the
        # bias bands. Skip rather than record garbage.
        if self._morning_forecast_date != date_str:
            self.logger.debug(
                f"[OpenMeteo] Baseline date {self._morning_forecast_date} != "
                f"{date_str} — skipping accuracy record for this day"
            )
            return

        today_str = date_str
        month_str = today_str[:7]

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
        self._correction_bands  = self._compute_correction_bands(records)
        self._log_bands("Updated")

        # Consumed — the new day's first complete fetch captures its own baseline.
        self._morning_forecast_kwh  = 0.0
        self._morning_forecast_date = None
        self._save_morning_baseline()

    def load_correction_factor(self):
        """Load and compute correction factor + bands from saved accuracy records.

        Call on plugin startup so bias correction is active immediately.
        """
        records = self._load_accuracy_records()
        self._correction_factor = self._compute_correction_factor(records)
        self._correction_bands  = self._compute_correction_bands(records)
        self.logger.info(
            f"[OpenMeteo] Loaded bias correction from {len(records)} records: "
            f"overall {self._correction_factor:.3f}"
        )
        self._log_bands("Loaded")

    def _log_bands(self, prefix):
        """Log the band table at INFO level — one row per band."""
        line = ", ".join(
            f"{int(c):>3} kWh: {f:.3f}" for c, f in self._correction_bands
        )
        self.logger.info(f"[OpenMeteo] {prefix} bias bands: {line}")

    def get_accuracy_summary(self, window_days=7):
        """Return a dict summarising forecast accuracy over the last N days.

        Useful for surfacing on the dashboard or in a self-test report.
        Returns:
            {"days": int, "mape_pct": float, "mean_factor": float,
             "over_count": int, "under_count": int}
            All values may be 0 if no records exist yet.
        """
        records = self._load_accuracy_records()[-window_days:]
        if not records:
            return {
                "days": 0, "mape_pct": 0.0, "mean_factor": 1.0,
                "over_count": 0, "under_count": 0,
            }
        deltas = []
        factors = []
        over = under = 0
        for r in records:
            f_kwh = float(r.get("forecast_kwh") or 0.0)
            a_kwh = float(r.get("actual_kwh")   or 0.0)
            if f_kwh > 0.1:
                deltas.append(abs(a_kwh - f_kwh) / f_kwh)
                factors.append(a_kwh / f_kwh)
                if a_kwh > f_kwh:
                    under += 1   # forecast under-predicted
                elif a_kwh < f_kwh:
                    over += 1    # forecast over-predicted
        mape = (sum(deltas) / len(deltas) * 100.0) if deltas else 0.0
        mean_factor = (sum(factors) / len(factors)) if factors else 1.0
        return {
            "days":        len(records),
            "mape_pct":    round(mape, 1),
            "mean_factor": round(mean_factor, 3),
            "over_count":  over,
            "under_count": under,
        }

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
        self._network_down_this_cycle = False   # per-cycle short-circuit flag

        # Accumulate combined kWh per local-time hour key before inverter cap
        # key format: "YYYY-MM-DD HH:00:00"
        hourly_raw = {}
        arrays_ok  = 0

        for array_cfg in self.arrays:
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

        if arrays_ok < len(self.arrays):
            self.logger.warning(
                f"[OpenMeteo] Only {arrays_ok}/{len(self.arrays)} arrays fetched"
            )

        # Apply inverter cap and split into today / tomorrow buckets
        hourly_today    = {}
        hourly_tomorrow = {}
        today_total     = 0.0
        tomorrow_total  = 0.0
        dawn_times      = {}

        for key in sorted(hourly_raw.keys()):
            raw_kwh    = hourly_raw[key]
            # Bound below as well as above — a negative sentinel that slipped
            # through must not deflate the daily totals.
            capped_kwh = min(max(raw_kwh, 0.0), INVERTER_CAP_KW)
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
                    # prefer_dst=False resolves the autumn fallback ambiguity
                    # (01:00-02:00 local exists twice on the last Sunday in
                    # October). A dawn is never in that hour, but without an
                    # answer pytz raises AmbiguousTimeError and takes the whole
                    # forecast parse down once a year.
                    dt_aware = london_localise(dt_naive)
                    if dt_aware is None:
                        self._warn_no_tz()
                        continue          # a naive dawn compares wrongly later
                    dawn_times[date_str] = dt_aware

        # Remaining today: bias-corrected, with the current hour pro-rated.
        # _enrich_forecast recomputes this against the current clock on every
        # serve path, but both call sites go through the one helper — keeping a
        # second inline implementation here is exactly how the two drifted onto
        # different scales before v1.7.
        now_hour_naive = now_local.replace(minute=0, second=0,
                                           microsecond=0, tzinfo=None)
        remaining_today_kwh = self._remaining_today_kwh(
            hourly_today,
            self._apply_band_correction(today_total),
            now=now_local,
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
            "arrays_ok":            arrays_ok,
            "arrays_total":         len(self.arrays),
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
        fallback_data = None   # served on fetch failure if fresh enough (see below)
        cached = self._load_array_cache(array_cfg["name"])
        if cached:
            age  = time.time() - cached.get("cached_time", 0)
            data = cached.get("data", [])
            # Day-shift guard (mirrors the combined cache): a cache written just before
            # midnight has its first bucket dated yesterday, yet a 20-min-old cache
            # loaded at 00:10 passes the age check — serving it as today's irradiance
            # would feed the wrong day. Skip if the first bucket predates today (local).
            fresh_day = True
            if data:
                try:
                    first_date  = str(data[0][0])[:10]
                    local_today = self._now_local().date().strftime("%Y-%m-%d")
                    fresh_day   = first_date >= local_today
                except Exception:
                    fresh_day = True
            if age < CACHE_TTL and fresh_day:
                self.logger.debug(
                    f"[OpenMeteo] {array_cfg['name']}: disk cache (age {age:.0f}s)"
                )
                return data
            # Pre-compute what a fetch-failure fallback may serve: the same
            # fresh-day guard plus a 24h age ceiling. Previously every failure
            # path returned the cache unconditionally, so a multi-day outage
            # fabricated tomorrowKwh=0.0 stamped 'OK' (v5.43 fix).
            if age < STALE_FALLBACK_TTL and fresh_day:
                fallback_data = data

        # A blackholed network fails each array serially (attempts x timeout);
        # once one array exhausts its attempts on network errors, skip the live
        # fetch for the remaining arrays this cycle and go straight to fallback.
        if self._network_down_this_cycle:
            self.logger.debug(
                f"[OpenMeteo] {array_cfg['name']}: network down this cycle — "
                f"skipping live fetch")
            return fallback_data

        params = {
            "latitude":   self.latitude,
            "longitude":  self.longitude,
            "hourly":     "global_tilted_irradiance",
            "tilt":       array_cfg["tilt"],
            "azimuth":    array_cfg["azimuth"],
            "timezone":   "Europe/London",
            "forecast_days": 3,
            "timeformat": "iso8601",
        }

        # Transient network errors (TLS hiccups, connection resets, DNS blips,
        # read timeouts) are common against cloud APIs — one retry with a brief
        # backoff masks the vast majority. Anything still failing after the
        # retry falls through to cache, same as before.
        response  = None
        for attempt in range(1, RETRY_ATTEMPTS + 1):
            try:
                response = requests.get(
                    OPENMETEO_URL,
                    params=params,
                    timeout=REQUEST_TIMEOUT,
                )
                break
            except (requests.exceptions.Timeout,
                    requests.exceptions.ConnectionError,
                    requests.exceptions.ChunkedEncodingError) as e:
                if attempt < RETRY_ATTEMPTS:
                    self.logger.warning(
                        f"[OpenMeteo] {array_cfg['name']}: transient network "
                        f"error (attempt {attempt}/{RETRY_ATTEMPTS}) — "
                        f"{type(e).__name__}: {e}; retrying in "
                        f"{RETRY_BACKOFF_S}s"
                    )
                    time.sleep(RETRY_BACKOFF_S)
                else:
                    self.logger.warning(
                        f"[OpenMeteo] {array_cfg['name']}: transient network "
                        f"error after {RETRY_ATTEMPTS} attempts — "
                        f"{type(e).__name__}: {e}"
                    )
                    self._network_down_this_cycle = True
            except Exception as e:
                # Unexpected — log at ERROR and don't retry
                self.logger.error(
                    f"[OpenMeteo] Error fetching {array_cfg['name']}: {e}"
                )
                break

        if response is None:
            # All attempts failed — fall back to cache if we have one
            return fallback_data

        try:
            if response.status_code != 200:
                if response.status_code >= 500:
                    # Transient server-side blip (502/503/504 — Open-Meteo's
                    # nginx gateway). Not ours to fix; fall back to the cached/
                    # partial forecast and log at WARNING, not an alarming red
                    # ERROR. The 3-of-4 fallback / cache keeps the manager running.
                    self.logger.warning(
                        f"[OpenMeteo] {array_cfg['name']}: HTTP "
                        f"{response.status_code} (server-side, transient) — "
                        f"using cached/partial forecast"
                    )
                else:
                    self.logger.error(
                        f"[OpenMeteo] HTTP {response.status_code} for "
                        f"{array_cfg['name']}: {response.text[:200]}"
                    )
                return fallback_data

            try:
                data = response.json()
            except ValueError as e:
                self.logger.error(
                    f"[OpenMeteo] Malformed JSON for {array_cfg['name']}: {e}"
                )
                return fallback_data
            hourly = data.get("hourly", {})
            times  = hourly.get("time", [])
            gti    = hourly.get("global_tilted_irradiance", [])

            if not times or not gti or len(times) != len(gti):
                self.logger.error(
                    f"[OpenMeteo] Unexpected response for {array_cfg['name']}: "
                    f"{len(times)} times, {len(gti)} GTI values"
                )
                return fallback_data

            result  = []
            clamped = 0
            for i in range(len(times)):
                v = float(gti[i]) if gti[i] is not None else 0.0
                # Plausibility clamp: negative sentinels deflate daily totals
                # (and create negative hourly slots); garbage-large samples
                # inflate them to physically impossible values — either way
                # stamped 'OK' and cached. Bound each sample to [0, GTI_MAX_WM2].
                cv = min(max(0.0, v), GTI_MAX_WM2)
                if cv != v:
                    clamped += 1
                result.append((times[i], cv))
            if clamped:
                self.logger.debug(
                    f"[OpenMeteo] {array_cfg['name']}: clamped {clamped} "
                    f"implausible GTI samples to [0, {GTI_MAX_WM2:.0f}] W/m²"
                )

            self._save_array_cache(array_cfg["name"], result)
            self.logger.debug(
                f"[OpenMeteo] {array_cfg['name']}: fetched {len(result)} hours"
            )
            return result

        except Exception as e:
            self.logger.error(
                f"[OpenMeteo] Error parsing response for {array_cfg['name']}: {e}"
            )
            return fallback_data

    # ================================================================
    # Enrichment & Empty Result
    # ================================================================

    def _enrich_forecast(self, combined):
        """Add corrected forecast totals to the combined dict.

        v1.3 (22-May-2026): magnitude-conditional bias correction is applied.
        For each day, `_apply_band_correction(raw_kwh)` linearly interpolates
        a factor from `self._correction_bands` and multiplies the raw total.

        v1.7 (19-Jul-2026): `remainingTodayKwh` takes the same per-day factor and
        pro-rates the current hour — see `_remaining_today_kwh`.

        `biasFactor` keeps its v1.2 meaning — the overall kWh-weighted scalar
        across all records — so dashboard displays don't shift unexpectedly.
        Per-day effective factors are exposed as `biasFactorToday` and
        `biasFactorTomorrow` for diagnostics; the full band table is on
        `biasBands` so the dashboard can render the correction curve.
        """
        enriched     = dict(combined)
        raw_today    = combined.get("todayKwh", 0.0)
        raw_tomorrow = combined.get("tomorrowKwh", 0.0)

        factor_today    = self._apply_band_correction(raw_today)
        factor_tomorrow = self._apply_band_correction(raw_tomorrow)

        enriched["biasFactor"]           = round(self._correction_factor, 3)
        enriched["biasFactorToday"]      = round(factor_today, 3)
        enriched["biasFactorTomorrow"]   = round(factor_tomorrow, 3)
        enriched["biasBands"]            = [
            [round(c, 1), round(f, 4)] for c, f in self._correction_bands
        ]
        enriched["correctedTodayKwh"]    = round(raw_today    * factor_today,    1)
        enriched["correctedTomorrowKwh"] = round(raw_tomorrow * factor_tomorrow, 1)

        # remainingTodayKwh / currentHourWatts / nextHourWatts are baked in at
        # fetch time, but every cache-served and stale-fallback return passes
        # through here — recompute them against the current clock from the
        # hourly buckets so every serve path is time-correct. Note remaining
        # takes factor_today: it sits next to correctedTodayKwh on the dashboard
        # and must be on the same scale (v1.7).
        hourly_today = combined.get("_hourly_p50_today") or {}
        if hourly_today:
            now_hour = self._now_local().replace(minute=0, second=0,
                                                 microsecond=0, tzinfo=None)
            cur_key = now_hour.strftime("%Y-%m-%d %H:%M:%S")
            nxt_key = (now_hour + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
            enriched["remainingTodayKwh"] = self._remaining_today_kwh(
                hourly_today, factor_today
            )
            # currentHourWatts / nextHourWatts stay RAW — they describe the
            # model's instantaneous shape (and are labelled as W averages), not
            # day totals, so they are not comparable to correctedTodayKwh and
            # don't take the band factor. The optimiser gets the corrected shape
            # from _write_optimiser_file instead.
            enriched["currentHourWatts"]  = hourly_today.get(cur_key, 0)
            enriched["nextHourWatts"]     = hourly_today.get(nxt_key, 0)
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
            "biasFactorToday":      1.0,
            "biasFactorTomorrow":   1.0,
            "biasBands":            [[round(c, 1), 1.0]
                                     for c in BIAS_BAND_CENTRES_KWH],
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
        3. factor = sum(actual) / sum(forecast)  (kWh-weighted, unbiased)
        4. Clamped to [BIAS_CORRECTION_MIN, BIAS_CORRECTION_MAX]
        5. Returns 1.0 if fewer than 3 records available

        Days where forecast < MIN_CALIBRATION_FORECAST_KWH are excluded
        (overcast days where accuracy is not meaningful for calibration).
        Days with extreme per-day ratios (outside [0.1, 2 × BIAS_CORRECTION_MAX])
        are excluded too — a 95 %-overshoot day shouldn't define a season.

        21-May-2026: switched from arithmetic-mean-of-ratios (`mean(actual/fc)`) to
        ratio-of-sums (`sum(actual)/sum(fc)`). The old formula was statistically
        biased upward because per-day ratios are bounded below by 0 but unbounded
        above; under-forecast days (ratio > 1) skew the mean further than
        over-forecast days (ratio < 1). For May 2026 the old formula produced
        1.163 while the kWh-weighted true factor was 1.150.
        """
        if not records:
            return 1.0

        current_month = datetime.now().strftime("%Y-%m")

        same_month = [
            r for r in records
            if r.get("month", "").endswith("-" + current_month.split("-")[1])
            and r.get("forecast_kwh", 0) >= MIN_CALIBRATION_FORECAST_KWH
            and 0.1 < r.get("factor", 0) < BIAS_CORRECTION_MAX * 2
        ]

        if len(same_month) >= 3:
            return self._clamped_sum_ratio(same_month)

        valid = [
            r for r in records[-30:]
            if r.get("forecast_kwh", 0) >= MIN_CALIBRATION_FORECAST_KWH
            and 0.1 < r.get("factor", 0) < BIAS_CORRECTION_MAX * 2
        ]

        if len(valid) < 3:
            return 1.0

        return self._clamped_sum_ratio(valid)

    @staticmethod
    def _clamped_sum_ratio(records):
        """Return sum(actual)/sum(forecast) clamped to the bias bounds.

        Falls back to 1.0 if the records have zero total forecast (shouldn't
        happen because MIN_CALIBRATION_FORECAST_KWH excludes near-zero days).
        """
        # Defensive .get (the rest of the module reads these records with .get) so a
        # malformed record can't KeyError out of the whole bias-correction pass.
        sum_actual   = sum(float(r.get("actual_kwh")   or 0.0) for r in records)
        sum_forecast = sum(float(r.get("forecast_kwh") or 0.0) for r in records)
        if sum_forecast <= 0:
            return 1.0
        raw = sum_actual / sum_forecast
        return round(max(BIAS_CORRECTION_MIN, min(BIAS_CORRECTION_MAX, raw)), 4)

    def _compute_correction_bands(self, records):
        """Return per-band correction factors as list of (centre_kwh, factor) tuples.

        For each centre in BIAS_BAND_CENTRES_KWH:
          1. Filter records whose raw forecast falls within ±BIAS_BAND_HALF_WIDTH
             of the centre, are above MIN_CALIBRATION_FORECAST_KWH, and have a
             per-day ratio inside [0.1, 2 × BIAS_CORRECTION_MAX].
          2. If ≥ MIN_BAND_SAMPLES survive, factor = median(actual/forecast).
             Median (not mean / sum-ratio) is more robust given small per-band
             sample counts (n=4–12 typical for a 60-day window).
          3. Otherwise inherit the global kWh-weighted factor across the same
             window — better than 1.0 when we know the overall direction.
          4. Clamp every band to [BIAS_CORRECTION_MIN, BIAS_CORRECTION_MAX].

        Returns a list in ascending centre order so `_apply_band_correction`
        can linearly interpolate.
        """
        window = records[-60:] if records else []
        valid_all = [
            r for r in window
            if r.get("forecast_kwh", 0) >= MIN_CALIBRATION_FORECAST_KWH
            and 0.1 < r.get("factor", 0) < BIAS_CORRECTION_MAX * 2
        ]
        global_factor = (
            self._clamped_sum_ratio(valid_all) if len(valid_all) >= MIN_BAND_SAMPLES
            else 1.0
        )

        bands = []
        for centre in BIAS_BAND_CENTRES_KWH:
            lo = centre - BIAS_BAND_HALF_WIDTH
            hi = centre + BIAS_BAND_HALF_WIDTH
            band_records = [
                r for r in valid_all
                if lo <= r["forecast_kwh"] < hi
            ]
            if len(band_records) >= MIN_BAND_SAMPLES:
                ratios = sorted(r["actual_kwh"] / r["forecast_kwh"]
                                for r in band_records)
                mid = len(ratios) // 2
                raw_factor = (
                    ratios[mid] if len(ratios) % 2
                    else (ratios[mid - 1] + ratios[mid]) / 2.0
                )
            else:
                raw_factor = global_factor

            clamped = max(BIAS_CORRECTION_MIN,
                          min(BIAS_CORRECTION_MAX, raw_factor))
            bands.append((float(centre), round(clamped, 4)))

        return bands

    def _apply_band_correction(self, raw_kwh):
        """Linearly interpolate the correction factor for a given raw forecast kWh.

        Below the first band centre returns the first factor; above the last
        returns the last; between centres linearly interpolates.
        """
        bands = self._correction_bands
        if not bands or raw_kwh <= 0:
            return 1.0
        if raw_kwh <= bands[0][0]:
            return bands[0][1]
        if raw_kwh >= bands[-1][0]:
            return bands[-1][1]
        for (c_lo, f_lo), (c_hi, f_hi) in zip(bands, bands[1:]):
            if c_lo <= raw_kwh <= c_hi:
                span = c_hi - c_lo
                if span <= 0:
                    return f_lo
                w = (raw_kwh - c_lo) / span
                return f_lo + w * (f_hi - f_lo)
        return 1.0   # unreachable but defensive

    # ================================================================
    # Optimiser forecast file writer
    # ================================================================

    def _write_optimiser_file(self, combined):
        """Write openmeteo_forecast.json for the battery optimiser script.

        The optimiser reads {"hourly": {"YYYY-MM-DDTHH:MM:SSZ": {"kwh": float}}, ...}
        with UTC timestamp keys. Our internal dict uses local (BST/GMT) keys so
        this method converts them using pytz (falls back to UTC+1 if pytz absent).

        v1.3 (22-May-2026): magnitude-conditional bias correction applied.
          1. Compute raw daily totals per local-time day from the hourly buckets.
          2. Apply `_apply_band_correction(raw_daily)` to each day separately —
             today and tomorrow can land in different bands.
          3. Scale every hourly slot in that day's bucket by its day's factor,
             so the optimiser sees a shape-preserving correction (peaks scale,
             zero hours stay zero) rather than a uniform daily shift.
        Hourly slots are calibrated against daily totals, not against individual
        hours — that's why the per-day factor is applied uniformly within a day
        rather than re-binning each hourly value against the band table.
        """
        try:
            now_utc = datetime.now(timezone.utc)

            today_slots    = []   # (utc_key, raw_kwh)
            tomorrow_slots = []
            raw_today_kwh    = 0.0
            raw_tomorrow_kwh = 0.0

            for bucket_name, slot_list in (
                ("_hourly_p50_today",    today_slots),
                ("_hourly_p50_tomorrow", tomorrow_slots),
            ):
                bucket   = combined.get(bucket_name, {})
                # Which day a slot belongs to is decided by the bucket it came
                # from — the buckets are already split by local date upstream.
                is_today = (bucket_name == "_hourly_p50_today")
                for local_key, wh_int in bucket.items():
                    utc_key = self._local_key_to_utc(local_key)
                    if utc_key is None:
                        continue
                    kwh = wh_int / 1000.0
                    slot_list.append((utc_key, kwh))
                    if is_today:
                        raw_today_kwh += kwh
                    else:
                        raw_tomorrow_kwh += kwh

            factor_today    = self._apply_band_correction(raw_today_kwh)
            factor_tomorrow = self._apply_band_correction(raw_tomorrow_kwh)

            hourly_out = {}
            for utc_key, kwh in today_slots:
                hourly_out[utc_key] = {"kwh": round(kwh * factor_today, 3)}
            for utc_key, kwh in tomorrow_slots:
                hourly_out[utc_key] = {"kwh": round(kwh * factor_tomorrow, 3)}

            today_kwh    = raw_today_kwh    * factor_today
            tomorrow_kwh = raw_tomorrow_kwh * factor_tomorrow

            cached_time = self._cached_time or time.time()
            cache_age   = round((time.time() - cached_time) / 3600.0, 2)

            forecast_doc = {
                "generated_at":         now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "source":               "open_meteo",
                "cache_age_hours":      cache_age,
                "arrays":               [a["name"] for a in self.arrays],
                "bias_factor":          self._correction_factor,
                "bias_factor_today":    round(factor_today,    4),
                "bias_factor_tomorrow": round(factor_tomorrow, 4),
                "bias_bands":           [[round(c, 1), round(f, 4)]
                                         for c, f in self._correction_bands],
                "raw_today_kwh":        round(raw_today_kwh,    2),
                "raw_tomorrow_kwh":     round(raw_tomorrow_kwh, 2),
                "today_kwh":            round(today_kwh,        2),
                "tomorrow_kwh":         round(tomorrow_kwh,     2),
                "hourly":               hourly_out,
            }

            # Atomic write — an external scheduled script reads this file, so a
            # concurrent read (or crash mid-write) must never see truncated JSON.
            self._atomic_write_json(OPTIMISER_FORECAST_FILE, forecast_doc, indent=2)

            self.logger.debug(
                f"[OpenMeteo] Wrote optimiser file: {len(hourly_out)} slots, "
                f"today {today_kwh:.1f} kWh (raw {raw_today_kwh:.1f}, ×{factor_today:.3f}), "
                f"tomorrow {tomorrow_kwh:.1f} kWh (raw {raw_tomorrow_kwh:.1f}, ×{factor_tomorrow:.3f})"
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
            # The old zoneinfo branch here used a bare replace(tzinfo=...), i.e.
            # fold=0, while its pytz branch used is_dst=False, i.e. fold=1 — so
            # the ambiguous October hour converted differently depending on which
            # library was installed. It also ended in a crude "-1 hour", which is
            # right for BST and wrong for the four months of GMT.
            dt_local = london_localise(dt_naive)
            if dt_local is None:
                self._warn_no_tz()
                return None
            return dt_local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except Exception:
            return None

    # ================================================================
    # Disk Cache
    # ================================================================

    @staticmethod
    def _atomic_write_json(path, obj, indent=None):
        """Write JSON via temp file + fsync + os.replace (atomic on APFS) so a
        concurrent reader or a crash mid-write never sees truncated JSON."""
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=indent)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)

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
            self._atomic_write_json(path, {"cached_time": time.time(), "data": data})
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
            self._atomic_write_json(path, records, indent=2)
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

            # Day-shift guard: a cache written just before midnight has its "today"
            # buckets dated yesterday, yet a 20-min-old cache loaded at 00:10 passes
            # the age check below. Serving those as today's forecast would feed the
            # manager the wrong day's solar. If the cached today-bucket date no longer
            # matches today's LOCAL date, skip the pre-warm — the first tick's fetch
            # repopulates fresh data within the poll interval.
            _today_keys = sorted(data.get("_hourly_p50_today", {}).keys())
            if _today_keys:
                _cached_today = _today_keys[0][:10]
                _local_today  = self._now_local().date().strftime("%Y-%m-%d")
                if _cached_today != _local_today:
                    self.logger.warning(
                        f"[OpenMeteo] Disk cache is for {_cached_today}, not today "
                        f"({_local_today}) — skipping pre-warm; first fetch will refresh."
                    )
                    return

            cached_time = data.pop("_cached_time", 0.0)
            self._cached_forecast = data
            self._cached_time     = cached_time

            # Reconstruct dawn_times from hourly buckets
            dawn_times = {}
            for kwh_dict in (
                data.get("_hourly_p50_today", {}),
                data.get("_hourly_p50_tomorrow", {}),
            ):
                for key in sorted(kwh_dict.keys()):
                    if kwh_dict[key] >= PV_GENERATION_THRESHOLD_WH:
                        try:
                            dt_naive = datetime.strptime(key, "%Y-%m-%d %H:%M:%S")
                            dt       = london_localise(dt_naive)
                            if dt is None:
                                self._warn_no_tz()
                                continue
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
            self._atomic_write_json(path, saveable, indent=2)
        except Exception as e:
            self.logger.debug(f"[OpenMeteo] Cannot write combined cache: {e}")

    # ================================================================
    # Timezone Helper
    # ================================================================

    def _now_local(self):
        """Return current datetime in Europe/London timezone.

        The old fallback was a bare `datetime.now()` — the server clock, which
        on a UTC-hosted machine is an hour behind local for eight months of the
        year, with nothing logged. Every caller here is deciding which forecast
        slot is "now", so that error moves the whole remaining-today figure by a
        bucket. If the zone cannot be resolved we say so and use UTC knowingly,
        rather than a local clock we cannot vouch for.
        """
        now = london_now()
        if now is None:
            self._warn_no_tz()
            return datetime.now(timezone.utc)
        return now

    def _warn_no_tz(self):
        """Report a missing tz database once per instance, not per slot."""
        if getattr(self, "_tz_warned", False):
            return
        self._tz_warned = True
        self.logger.error(
            "[OpenMeteo] No Europe/London time zone available (neither zoneinfo "
            "nor pytz) — Open-Meteo returns LOCAL times, so dawn detection and "
            "the hourly slot keys cannot be trusted."
        )

    # ================================================================
    # Remaining-Today Helper
    # ================================================================

    def _remaining_today_kwh(self, hourly_today, factor=1.0, now=None):
        """kWh forecast from *now* to end of day — bias-corrected and pro-rated.

        Two things this owns, both of which were wrong when the calculation was
        inlined at two separate call sites (fixed in v1.7):

        1. `factor` is the day's band-correction multiplier, so the result lands
           on the SAME scale as correctedTodayKwh. The raw buckets alone put
           "Remaining" and "forecast" on different scales in the same sentence.
        2. The current hour contributes only its UNELAPSED fraction. Counting the
           whole bucket treated energy already generated as still to come — up to
           a full peak hour (~7 kWh at midday on this array).

        Args:
            hourly_today: {"YYYY-MM-DD HH:MM:SS": wh_int} for today, local time.
            factor:       band-correction multiplier for today's raw daily total.
            now:          override for the current local time (tests). Naive or
                          aware — tz is stripped to match the naive bucket keys.

        Returns:
            float kWh, rounded to 1 dp. 0.0 for empty/absent buckets.
        """
        if not hourly_today:
            return 0.0

        now_local = (now or self._now_local()).replace(tzinfo=None)
        now_hour  = now_local.replace(minute=0, second=0, microsecond=0)
        cur_key   = now_hour.strftime("%Y-%m-%d %H:%M:%S")

        # Fraction of the current hour still ahead of us (1.0 on the hour).
        elapsed_frac   = (now_local - now_hour).total_seconds() / 3600.0
        remaining_frac = min(1.0, max(0.0, 1.0 - elapsed_frac))

        total_kwh = 0.0
        for key, wh in hourly_today.items():
            try:
                slot = datetime.strptime(key, "%Y-%m-%d %H:%M:%S")
            except (ValueError, TypeError):
                continue                      # malformed key — skip, don't abort
            if slot > now_hour:
                total_kwh += wh / 1000.0
            elif key == cur_key:
                total_kwh += (wh / 1000.0) * remaining_frac

        return round(total_kwh * factor, 1)
