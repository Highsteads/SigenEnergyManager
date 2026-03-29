#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    octopus_api.py
# Description: Octopus Energy API client - tariff rates for Tracker/Go/Flux/iGo/iFlux
#              and historical consumption profile for overnight drain prediction
# Author:      CliveS & Claude Sonnet 4.6
# Date:        26-03-2026 15:30 GMT
# Version:     1.0
#
# Octopus REST v1 API: https://docs.octopus.energy/rest/guides/endpoints/
# Kraken GraphQL API: https://api.octopus.energy/v1/graphql/
#
# Auth:
#   - Rate endpoints (30xxx): no auth required (public)
#   - Consumption endpoint: HTTP Basic (API key as username, empty password)
#   - Account endpoint: HTTP Basic
#   - Balance/tariff codes: GraphQL with Kraken JWT

import base64
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


# ============================================================
# Constants
# ============================================================

OCTOPUS_API_BASE  = "https://api.octopus.energy/v1"
KRAKEN_GRAPHQL    = "https://api.octopus.energy/v1/graphql/"
REQUEST_TIMEOUT   = 15   # seconds

# Cache TTLs
RATES_CACHE_TTL       = 1800   # 30 min - rates change daily but check frequently for tomorrow
CONSUMPTION_CACHE_TTL = 86400  # 24 hours - consumption profile updated daily

# Tariff key constants
TARIFF_TRACKER = "tracker"
TARIFF_GO      = "go"
TARIFF_FLUX    = "flux"
TARIFF_IGO     = "igo"
TARIFF_IFLUX   = "iflux"
TARIFF_AGILE   = "agile"
TARIFF_UNKNOWN = "unknown"

# Product code prefixes for auto-detection
TARIFF_PRODUCT_PREFIXES = {
    TARIFF_TRACKER: ("SILVER", "TRACKER"),
    TARIFF_GO:      ("GO-VAR",),
    TARIFF_FLUX:    ("FLUX-IMPORT",),
    TARIFF_IGO:     ("INTELLI-VAR", "INTELLI-GO"),
    TARIFF_IFLUX:   ("INTELLI-FLUX",),
    TARIFF_AGILE:   ("AGILE-",),
}

# Time-of-use windows for each tariff (local time, 24h)
TARIFF_WINDOWS = {
    TARIFF_GO:    {"cheap_start": "00:30", "cheap_end": "05:30"},
    TARIFF_FLUX:  {"cheap_start": "02:00", "cheap_end": "05:00"},
    TARIFF_IGO:   {"cheap_start": "23:30", "cheap_end": "05:30"},  # 23:30-05:30 (6h)
    TARIFF_IFLUX: {"cheap_start": "19:00", "cheap_end": "16:00"},  # 21h non-peak window (avoids 16:00-19:00 peak)
}


class OctopusApiError(Exception):
    pass


class OctopusAPI:
    """Octopus Energy API client for SigenEnergyManager.

    Responsibilities:
    - Auto-detect current tariff from account endpoint
    - Fetch today's and tomorrow's rates for the active tariff
    - Fetch rates for Tracker, Go, and Flux for monitoring display
    - Fetch 30-day historical consumption to build overnight profile
    - Compare tariff costs

    All rate data is returned in pence per kWh (inc. VAT).
    """

    def __init__(self, api_key, account_id, mpan, serial,
                 region="F", data_dir=None, logger=None):
        """Initialise Octopus API client.

        Args:
            api_key:    Octopus API key (sk_live_...)
            account_id: Octopus account number (A-XXXXXXXX)
            mpan:       13-digit electricity meter point number
            serial:     Electricity meter serial number
            region:     Grid region code A-P (default F = North East)
            data_dir:   Cache directory
            logger:     Logger instance
        """
        self.api_key    = api_key
        self.account_id = account_id
        self.mpan       = mpan
        self.serial     = serial
        self.region     = region.upper()
        self.data_dir   = data_dir or ""
        self.logger     = logger or logging.getLogger("SigenEnergyManager.Octopus")

        # HTTP Basic auth header (api_key as username, empty password)
        if api_key:
            credentials        = base64.b64encode(f"{api_key}:".encode()).decode()
            self._auth_header  = {"Authorization": f"Basic {credentials}"}
        else:
            self._auth_header  = {}

        # In-memory cache
        self._rates_cache      = {}     # tariff_key -> {data, cached_at}
        self._profile_cache    = None   # consumption profile
        self._profile_cache_at = 0.0
        self._tariff_cache     = None   # detected tariff info
        self._tariff_cache_at  = 0.0
        self._kraken_token     = None
        self._kraken_token_at  = 0.0

    # ================================================================
    # Public: Tariff Detection
    # ================================================================

    def get_current_tariff(self, force=False):
        """Detect the currently active tariff from the account endpoint.

        Returns dict:
            tariff_key:     "tracker" | "go" | "flux" | "igo" | "iflux" | "agile" | "unknown"
            tariff_code:    Full tariff code (e.g. "E-1R-TRACKER-VAR-25-04-01-F")
            product_code:   Product code (e.g. "TRACKER-VAR-25-04-01")
            display_name:   Human-readable name
        """
        now = time.time()
        if (not force and self._tariff_cache
                and now - self._tariff_cache_at < RATES_CACHE_TTL):
            return self._tariff_cache

        tariff_info = self._detect_tariff_from_account()
        if not tariff_info:
            # REST account endpoint failed — try Kraken GraphQL as fallback
            tariff_info = self._detect_tariff_from_kraken()
        if not tariff_info:
            tariff_info = {
                "tariff_key":   TARIFF_UNKNOWN,
                "tariff_code":  "",
                "product_code": "",
                "display_name": "Unknown",
            }

        self._tariff_cache    = tariff_info
        self._tariff_cache_at = now
        return tariff_info

    # ================================================================
    # Public: Rate Fetching
    # ================================================================

    def get_tracker_rates(self, force=False):
        """Fetch Tracker unit rate for today and tomorrow (if published ~16:00).

        Returns dict:
            today_p:    float rate in pence/kWh (inc. VAT)
            tomorrow_p: float or None if not yet published
        """
        return self._get_tracker_rates(force=force)

    def get_tou_rates(self, tariff_key, force=False):
        """Fetch time-of-use rates for Go, Flux, iGo, or iFlux.

        Returns dict:
            cheap_start:  "HH:MM" (local time)
            cheap_end:    "HH:MM"
            cheap_p:      float pence/kWh
            standard_p:   float pence/kWh
            peak_p:       float or None (Flux has peak 16:00-19:00)
            peak_start:   "HH:MM" or None
            peak_end:     "HH:MM" or None
        """
        if tariff_key not in (TARIFF_GO, TARIFF_FLUX, TARIFF_IGO, TARIFF_IFLUX):
            return {}
        return self._get_tou_rates(tariff_key, force=force)

    def get_all_monitored_rates(self, force=False):
        """Fetch rates for all three monitored tariffs (Tracker, Go, Flux).

        Returns dict keyed by tariff_key with rate sub-dicts.
        Used to populate the tariffMonitor device.
        """
        result = {}

        tracker = self._get_tracker_rates(force=force)
        if tracker:
            result[TARIFF_TRACKER] = tracker

        for tariff_key in (TARIFF_GO, TARIFF_FLUX):
            tou = self._get_tou_rates(tariff_key, force=force)
            if tou:
                result[tariff_key] = tou

        return result

    def get_agile_rates(self, target_date=None, force=False):
        """Fetch Agile half-hourly rates for a given date.

        Returns list of (datetime, rate_p) tuples sorted by time, or [].
        """
        if target_date is None:
            target_date = datetime.now().date()

        cache_key = f"agile_{target_date}"
        now = time.time()
        cached = self._rates_cache.get(cache_key)
        if not force and cached and now - cached["cached_at"] < RATES_CACHE_TTL:
            return cached["data"]

        # Find Agile product code for this region
        product_code = self._find_product_code(TARIFF_AGILE)
        if not product_code:
            return []

        tariff_code = self._build_tariff_code(product_code)
        slots = self._fetch_rate_schedule(product_code, tariff_code, target_date)

        result = []
        for slot in slots:
            try:
                dt = datetime.fromisoformat(
                    slot["valid_from"].replace("Z", "+00:00")
                )
                result.append((dt, slot["value_inc_vat"]))
            except (KeyError, ValueError):
                continue

        result.sort(key=lambda x: x[0])
        self._rates_cache[cache_key] = {"data": result, "cached_at": now}
        return result

    # ================================================================
    # Public: Consumption Profile
    # ================================================================

    def get_consumption_profile(self, days=30, force=False):
        """Fetch 30-day consumption history and return 48-slot daily average profile.

        Returns list of 48 floats: average kWh per half-hour slot
        (slot 0 = 00:00-00:30, slot 1 = 00:30-01:00, ..., slot 47 = 23:30-00:00).
        Falls back to UK typical flat profile if insufficient data.
        """
        now = time.time()
        if (not force and self._profile_cache
                and now - self._profile_cache_at < CONSUMPTION_CACHE_TTL):
            return self._profile_cache

        profile = self._fetch_consumption_profile(days)
        self._profile_cache    = profile
        self._profile_cache_at = now
        return profile

    # ================================================================
    # Internal: Tracker Rates
    # ================================================================

    def _get_tracker_rates(self, force=False):
        """Fetch Tracker rates for today and (if published) tomorrow."""
        cache_key = "tracker_rates"
        now = time.time()
        cached = self._rates_cache.get(cache_key)
        if not force and cached and now - cached["cached_at"] < RATES_CACHE_TTL:
            return cached["data"]

        product_code = self._find_product_code(TARIFF_TRACKER)
        if not product_code:
            self.logger.warning("Cannot find Tracker product code")
            return {"today_p": None, "tomorrow_p": None}

        tariff_code = self._build_tariff_code(product_code)

        # Today's rate
        today_rate = self._fetch_current_rate(product_code, tariff_code)

        # Tomorrow's rate (published around 16:00 each day)
        tomorrow_date   = (datetime.now(timezone.utc) + timedelta(days=1)).date()
        tomorrow_slots  = self._fetch_rate_schedule(product_code, tariff_code, tomorrow_date)
        tomorrow_rate   = tomorrow_slots[0]["value_inc_vat"] if tomorrow_slots else None

        result = {
            "today_p":    today_rate,
            "tomorrow_p": tomorrow_rate,
        }
        self._rates_cache[cache_key] = {"data": result, "cached_at": now}
        return result

    # ================================================================
    # Internal: TOU (Go/Flux) Rates
    # ================================================================

    def _get_tou_rates(self, tariff_key, force=False):
        """Fetch time-of-use rates for Go or Flux tariff."""
        cache_key = f"tou_{tariff_key}"
        now = time.time()
        cached = self._rates_cache.get(cache_key)
        if not force and cached and now - cached["cached_at"] < RATES_CACHE_TTL:
            return cached["data"]

        product_code = self._find_product_code(tariff_key)
        if not product_code:
            self.logger.debug(f"Cannot find product code for {tariff_key}")
            return {}

        tariff_code = self._build_tariff_code(product_code)
        today       = datetime.now(timezone.utc).date()
        slots       = self._fetch_rate_schedule(product_code, tariff_code, today)

        if not slots:
            return {}

        window = TARIFF_WINDOWS.get(tariff_key, {})
        result = self._parse_tou_slots(slots, window)

        self._rates_cache[cache_key] = {"data": result, "cached_at": now}
        return result

    def _parse_tou_slots(self, slots, window):
        """Parse rate slots into cheap/standard/peak breakdown."""
        if not slots:
            return {}

        cheap_start = window.get("cheap_start", "02:00")
        cheap_end   = window.get("cheap_end", "05:00")

        # Group rates by time window
        cheap_rates    = []
        peak_rates     = []
        standard_rates = []

        for slot in slots:
            try:
                valid_from = datetime.fromisoformat(
                    slot["valid_from"].replace("Z", "+00:00")
                )
                # Use UTC hour for time comparison (rates are in UTC)
                hour_min = valid_from.strftime("%H:%M")
                rate     = slot["value_inc_vat"]
            except (KeyError, ValueError):
                continue

            if self._time_in_window(hour_min, cheap_start, cheap_end):
                cheap_rates.append(rate)
            elif self._time_in_window(hour_min, "16:00", "19:00"):
                peak_rates.append(rate)
            else:
                standard_rates.append(rate)

        result = {
            "cheap_start": cheap_start,
            "cheap_end":   cheap_end,
            "cheap_p":     round(sum(cheap_rates) / len(cheap_rates), 4) if cheap_rates else None,
            "standard_p":  round(sum(standard_rates) / len(standard_rates), 4) if standard_rates else None,
            "peak_p":      round(sum(peak_rates) / len(peak_rates), 4) if peak_rates else None,
            "peak_start":  "16:00" if peak_rates else None,
            "peak_end":    "19:00" if peak_rates else None,
        }
        return result

    # ================================================================
    # Internal: Product Code Discovery
    # ================================================================

    def _find_product_code(self, tariff_key):
        """Find the current product code for a given tariff key.

        All tariffs: tries public products listing via _probe_product_by_prefix().
        Tracker additionally checks the account endpoint first (when credentials
        are configured) so the exact active product code is used.
        """
        if tariff_key == TARIFF_TRACKER:
            # Prefer account endpoint when credentials are available
            info = self.get_current_tariff()
            if info and info.get("tariff_key") == TARIFF_TRACKER:
                return info.get("product_code", "")
            # Fall back to public products listing (SILVER-* or TRACKER-VAR-* prefixes)
            return self._probe_product_by_prefix(TARIFF_PRODUCT_PREFIXES.get(tariff_key, ()))

        return self._probe_product_by_prefix(TARIFF_PRODUCT_PREFIXES.get(tariff_key, ()))

    def _probe_product_by_prefix(self, prefixes):
        """Search public products listing for a product matching given prefixes.

        No is_variable filter: Tracker (SILVER-*) is a daily-changing flat
        rate that Octopus does not flag as is_variable in their products API,
        so filtering on that flag silently excludes it.
        """
        if not prefixes:
            return None

        url = f"{OCTOPUS_API_BASE}/products/"
        params = {"page_size": 100}

        try:
            response = self._api_get(url, params=params, authenticated=False)
            if not response:
                return None

            results = response.get("results", [])
            for product in results:
                code = product.get("code", "")
                for prefix in prefixes:
                    if code.startswith(prefix):
                        return code

        except Exception as e:
            self.logger.debug(f"Product probe error: {e}")

        return None

    def _build_tariff_code(self, product_code):
        """Build a full tariff code from a product code and region."""
        # Pattern: E-1R-{PRODUCT_CODE}-{REGION}
        return f"E-1R-{product_code}-{self.region}"

    # ================================================================
    # Internal: Account / Tariff Detection
    # ================================================================

    def _detect_tariff_from_account(self):
        """Fetch account endpoint to discover the active electricity tariff.

        If self.mpan is configured, only the matching meter point is checked —
        this prevents the export MPAN (OUTGOING tariff) from being returned
        first and mis-classified as TARIFF_UNKNOWN.
        """
        if not self.api_key or not self.account_id:
            return None

        url = f"{OCTOPUS_API_BASE}/accounts/{self.account_id}"
        try:
            data = self._api_get(url, authenticated=True)
            if not data:
                return None

            # Walk properties -> electricity_meter_points -> agreements
            for prop in data.get("properties", []):
                for point in prop.get("electricity_meter_points", []):
                    # Skip non-import MPANs when we know our import MPAN
                    if self.mpan and point.get("mpan") != self.mpan:
                        continue
                    agreements = point.get("agreements", [])
                    active     = self._active_agreement(agreements)
                    if active:
                        tariff_code = active.get("tariff_code", "")
                        result = self._classify_tariff_code(tariff_code)
                        if result.get("tariff_key") != TARIFF_UNKNOWN:
                            return result

        except OctopusApiError as e:
            self.logger.warning(f"Account endpoint failed: {e}")
        except Exception as e:
            self.logger.warning(f"Account detection error: {e}")

        return None

    def _get_kraken_token(self):
        """Obtain (or return cached) a Kraken JWT for GraphQL authentication.

        The Kraken token is obtained via GraphQL mutation using the API key.
        Cached for 55 minutes (Octopus tokens are typically valid for 60 min).
        Returns the token string or None on failure.
        """
        now = time.time()
        if self._kraken_token and now - self._kraken_token_at < 3300:
            return self._kraken_token

        if not self.api_key:
            return None

        mutation = json.dumps({
            "query": f'mutation {{ obtainKrakenToken(input: {{ APIKey: "{self.api_key}" }}) {{ token }} }}'
        })
        try:
            response = requests.post(
                KRAKEN_GRAPHQL,
                data=mutation.encode(),
                headers={"Content-Type": "application/json"},
                timeout=REQUEST_TIMEOUT,
            )
            if not response.ok:
                self.logger.debug(f"Kraken token request failed: HTTP {response.status_code}")
                return None
            token = response.json().get("data", {}).get("obtainKrakenToken", {}).get("token")
            if token:
                self._kraken_token    = token
                self._kraken_token_at = now
                self.logger.debug("Kraken token obtained")
            return token
        except Exception as e:
            self.logger.debug(f"Kraken token error: {e}")
            return None

    def _detect_tariff_from_kraken(self):
        """Fetch active electricity tariff via Kraken GraphQL API.

        Used as fallback when the REST v1/accounts/ endpoint returns 500.
        Queries the active electricity agreement for the configured account
        and returns the same tariff_info dict as _detect_tariff_from_account().
        """
        if not self.api_key or not self.account_id:
            return None

        token = self._get_kraken_token()
        if not token:
            return None

        query = json.dumps({
            "query": (
                f'{{ account(accountNumber: "{self.account_id}") {{'
                f"  electricityAgreements(active: true) {{"
                f"    tariff {{"
                f"      ...on TariffType       {{ displayName productCode tariffCode }}"
                f"      ...on HalfHourlyTariff {{ displayName productCode tariffCode }}"
                f"    }}"
                f"  }}"
                f"}}}}"
            )
        })
        try:
            response = requests.post(
                KRAKEN_GRAPHQL,
                data=query.encode(),
                headers={
                    "Content-Type":  "application/json",
                    "Authorization": f"JWT {token}",
                },
                timeout=REQUEST_TIMEOUT,
            )
            if not response.ok:
                self.logger.debug(f"Kraken tariff query failed: HTTP {response.status_code}")
                return None

            agreements = (
                response.json()
                .get("data", {})
                .get("account", {})
                .get("electricityAgreements", [])
            )
            for agr in agreements:
                tariff_node   = agr.get("tariff", {}) or {}
                tariff_code   = tariff_node.get("tariffCode", "")
                if tariff_code:
                    result = self._classify_tariff_code(tariff_code)
                    self.logger.info(
                        f"[Kraken] Active tariff: {result.get('display_name')} "
                        f"({tariff_code})"
                    )
                    return result

        except Exception as e:
            self.logger.debug(f"Kraken tariff detection error: {e}")

        return None

    def _classify_tariff_code(self, tariff_code):
        """Classify a full tariff code into one of our tariff keys."""
        upper_code   = tariff_code.upper()
        product_code = self._product_from_tariff_code(tariff_code)

        for tariff_key, prefixes in TARIFF_PRODUCT_PREFIXES.items():
            for prefix in prefixes:
                if product_code.upper().startswith(prefix):
                    display_names = {
                        TARIFF_TRACKER: "Octopus Tracker",
                        TARIFF_GO:      "Octopus Go",
                        TARIFF_FLUX:    "Octopus Flux",
                        TARIFF_IGO:     "Intelligent Go",
                        TARIFF_IFLUX:   "Intelligent Flux",
                        TARIFF_AGILE:   "Octopus Agile",
                    }
                    return {
                        "tariff_key":   tariff_key,
                        "tariff_code":  tariff_code,
                        "product_code": product_code,
                        "display_name": display_names.get(tariff_key, tariff_key.title()),
                    }

        return {
            "tariff_key":   TARIFF_UNKNOWN,
            "tariff_code":  tariff_code,
            "product_code": product_code,
            "display_name": product_code or "Unknown",
        }

    @staticmethod
    def _product_from_tariff_code(tariff_code):
        """Extract product code from full tariff code.

        E.g. "E-1R-TRACKER-VAR-25-04-01-F" -> "TRACKER-VAR-25-04-01"
        """
        # Pattern: E-1R-{PRODUCT}-{REGION_CHAR}
        parts = tariff_code.split("-")
        if len(parts) >= 4:
            # Remove first 2 (E, 1R) and last 1 (region)
            return "-".join(parts[2:-1])
        return tariff_code

    @staticmethod
    def _active_agreement(agreements):
        """Return the currently active agreement from a list."""
        now_utc = datetime.now(timezone.utc)
        for ag in agreements:
            valid_from = ag.get("valid_from")
            valid_to   = ag.get("valid_to")
            if valid_from:
                try:
                    from_dt = datetime.fromisoformat(valid_from.replace("Z", "+00:00"))
                    if from_dt > now_utc:
                        continue  # future agreement
                except ValueError:
                    pass
            if valid_to:
                try:
                    to_dt = datetime.fromisoformat(valid_to.replace("Z", "+00:00"))
                    if to_dt <= now_utc:
                        continue  # expired
                except ValueError:
                    pass
            return ag
        # Fallback: last agreement
        return agreements[-1] if agreements else None

    # ================================================================
    # Internal: Rate Endpoints
    # ================================================================

    def _fetch_current_rate(self, product_code, tariff_code):
        """Fetch the currently active unit rate for a tariff."""
        url = (
            f"{OCTOPUS_API_BASE}/products/{product_code}/electricity-tariffs/"
            f"{tariff_code}/standard-unit-rates/"
        )
        try:
            data = self._api_get(url, params={"page_size": 10}, authenticated=False)
            if not data:
                return None
            rates  = data.get("results", [])
            active = self._active_rate(rates)
            return active.get("value_inc_vat") if active else None
        except Exception as e:
            self.logger.warning(f"Rate fetch error ({product_code}): {e}")
            return None

    def _fetch_rate_schedule(self, product_code, tariff_code, target_date):
        """Fetch all rate slots overlapping a given date.

        For Tracker: returns 1 slot.
        For Agile: returns up to 48 half-hourly slots.
        For Go/Flux: returns 2-5 time-band slots.
        """
        period_from = datetime(
            target_date.year, target_date.month, target_date.day,
            0, 0, 0, tzinfo=timezone.utc
        ).isoformat().replace("+00:00", "Z")
        period_to = datetime(
            target_date.year, target_date.month, target_date.day,
            23, 59, 59, tzinfo=timezone.utc
        ).isoformat().replace("+00:00", "Z")

        url = (
            f"{OCTOPUS_API_BASE}/products/{product_code}/electricity-tariffs/"
            f"{tariff_code}/standard-unit-rates/"
        )
        params = {
            "period_from": period_from,
            "period_to":   period_to,
            "page_size":   100,
        }
        try:
            data = self._api_get(url, params=params, authenticated=False)
            if not data:
                return []
            results = data.get("results", [])
            return sorted(results, key=lambda r: r.get("valid_from", ""))
        except Exception as e:
            self.logger.warning(f"Rate schedule fetch error ({product_code}, {target_date}): {e}")
            return []

    @staticmethod
    def _active_rate(rates):
        """Return the rate with valid_from <= now < valid_to (or no valid_to)."""
        now_utc = datetime.now(timezone.utc)
        for rate in rates:
            valid_from = rate.get("valid_from")
            valid_to   = rate.get("valid_to")
            try:
                if valid_from:
                    from_dt = datetime.fromisoformat(valid_from.replace("Z", "+00:00"))
                    if from_dt > now_utc:
                        continue
                if valid_to:
                    to_dt = datetime.fromisoformat(valid_to.replace("Z", "+00:00"))
                    if to_dt <= now_utc:
                        continue
                return rate
            except ValueError:
                continue
        return rates[-1] if rates else {}

    # ================================================================
    # Internal: Consumption Profile
    # ================================================================

    def _fetch_consumption_profile(self, days):
        """Fetch consumption data and build 48-slot half-hourly average profile."""
        if not self.mpan or not self.serial:
            self.logger.warning("MPAN or serial not configured - using default consumption profile")
            return self._default_consumption_profile()

        end_date   = datetime.now(timezone.utc)
        start_date = end_date - timedelta(days=days)

        url    = (
            f"{OCTOPUS_API_BASE}/electricity-meter-points/{self.mpan}/"
            f"meters/{self.serial}/consumption/"
        )
        params = {
            "period_from": start_date.isoformat().replace("+00:00", "Z"),
            "period_to":   end_date.isoformat().replace("+00:00", "Z"),
            "page_size":   25000,
            "order_by":    "period",
        }

        try:
            all_intervals = self._paginate(url, params, authenticated=True)
        except Exception as e:
            self.logger.warning(f"Consumption fetch error: {e} - using default profile")
            return self._default_consumption_profile()

        if not all_intervals:
            self.logger.warning("No consumption data returned - using default profile")
            return self._default_consumption_profile()

        # Build 48-slot averages (slot 0 = 00:00, slot 1 = 00:30, ...)
        slot_totals = [0.0] * 48
        slot_counts = [0]   * 48

        for interval in all_intervals:
            try:
                start_str = interval.get("interval_start", "")
                kwh       = float(interval.get("consumption", 0))
                if not start_str or kwh < 0:
                    continue

                dt    = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
                slot  = dt.hour * 2 + (1 if dt.minute >= 30 else 0)
                slot  = max(0, min(47, slot))

                slot_totals[slot] += kwh
                slot_counts[slot] += 1
            except (ValueError, TypeError):
                continue

        profile = []
        for i in range(48):
            if slot_counts[i] > 0:
                profile.append(round(slot_totals[i] / slot_counts[i], 4))
            else:
                # Fill missing slots with default
                profile.append(0.225)  # ~0.45 kWh/hour typical UK

        self.logger.info(
            f"Built consumption profile from {len(all_intervals)} intervals "
            f"({days} days). Daily total: {sum(profile):.1f} kWh"
        )
        return profile

    @staticmethod
    def _default_consumption_profile():
        """Return a UK typical 48-slot half-hourly consumption profile (kWh/slot).

        Based on typical UK home: ~10 kWh/day overnight, ~12 kWh/day total.
        Higher slots in morning and evening.
        """
        # Flat overnight (~0.3 kWh/slot) with peaks at 07:00 and 18:00-21:00
        profile = [0.20] * 48  # base

        # Morning boost (06:00-08:30 = slots 12-16)
        for slot in range(12, 17):
            profile[slot] = 0.45

        # Evening peak (17:00-22:00 = slots 34-43)
        for slot in range(34, 44):
            profile[slot] = 0.55

        return profile

    # ================================================================
    # Internal: HTTP Helpers
    # ================================================================

    def _api_get(self, url, params=None, authenticated=False):
        """HTTP GET with optional Basic auth. Returns parsed JSON or raises."""
        if not REQUESTS_AVAILABLE:
            raise OctopusApiError("requests library not available")

        headers = {"Accept": "application/json"}
        if authenticated:
            headers.update(self._auth_header)

        try:
            response = requests.get(
                url, headers=headers, params=params, timeout=REQUEST_TIMEOUT
            )
        except requests.exceptions.Timeout:
            raise OctopusApiError(f"Request timed out: {url}")
        except requests.exceptions.ConnectionError as e:
            raise OctopusApiError(f"Connection error: {url}: {e}")
        except Exception as e:
            raise OctopusApiError(f"Request error: {url}: {e}")

        if response.status_code == 401:
            raise OctopusApiError(f"Authentication failed (401): {url}")
        if response.status_code == 404:
            return None
        if not response.ok:
            body = response.text[:200].strip()
            detail = f" ({body})" if body else ""
            raise OctopusApiError(
                f"HTTP {response.status_code}{detail}: {url}"
            )

        try:
            return response.json()
        except Exception as e:
            raise OctopusApiError(f"JSON decode error: {url}: {e}")

    def _get(self, url, authenticated=False):
        """Simple HTTP GET, returns parsed JSON or None on 404."""
        try:
            return self._api_get(url, authenticated=authenticated)
        except OctopusApiError as e:
            self.logger.debug(f"GET failed: {e}")
            return None

    def _paginate(self, url, params, authenticated=False):
        """Follow pagination to collect all results."""
        all_results = []
        next_url    = url
        next_params = params

        while next_url:
            data = self._api_get(next_url, params=next_params, authenticated=authenticated)
            if not data:
                break

            results = data.get("results", [])
            all_results.extend(results)

            # Follow 'next' link if present
            next_url    = data.get("next")
            next_params = None  # params are encoded in next URL

            if len(all_results) > 50000:  # safety limit
                self.logger.warning("Pagination safety limit reached")
                break

        return all_results

    @staticmethod
    def _time_in_window(time_str, start_str, end_str):
        """Check if a HH:MM time string falls within a start-end window.

        Handles overnight windows (e.g. 23:30-05:30).
        """
        def to_minutes(t):
            h, m = t.split(":")
            return int(h) * 60 + int(m)

        t     = to_minutes(time_str)
        start = to_minutes(start_str)
        end   = to_minutes(end_str)

        if start <= end:
            return start <= t < end
        else:
            # Overnight window (e.g. 23:30 to 05:30)
            return t >= start or t < end
