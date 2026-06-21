#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    octopus_api.py
# Description: Octopus Energy API client - tariff rates for Tracker/Go/Flux/iGo/iFlux
#              and historical consumption profile for overnight drain prediction
# Author:      CliveS & Claude Opus 4.8
# Date:        21-06-2026 17:00 BST
# Version:     1.3
#
# v1.3 (21-06-2026) — GraphQL queries parameterised (variables, not raw
#   account/key string-interpolation); import-vs-export classified by MPAN not
#   just the OUTGOING code; first active agreement wins; zoneinfo TZ fallback.
#
# v1.2 (21-06-2026) — get_account_financials negative-caches failures
#   (FINANCIALS_NEG_CACHE_TTL) and returns the last good value, so a Kraken
#   outage can't make the dashboard's /api/status poll hammer the API.
#
# v1.1 (21-06-2026) — whole-house cost support:
#   • get_account_financials() — bill-exact standing/unit rates (elec, gas,
#     export) + account balance from the Kraken ledger via active:true
#     agreements (survives tariff changes with no config change).
#   • get_import_kwh_for_date() / get_gas_kwh_for_date() — per-day settled
#     consumption, mirroring get_export_kwh_for_date(); gas m3->kWh via a
#     configurable calorific factor.
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
FINANCIALS_CACHE_TTL  = 1800   # 30 min - standing/unit rates change at most daily; balance slowly
FINANCIALS_NEG_CACHE_TTL = 120 # 2 min - debounce failures so /api/status can't hammer Kraken

# Gas volume (m3) -> kWh conversion.  Octopus bills gas in kWh but the
# consumption API returns m3 for metric SMETS meters.  kWh = m3 * VCF * CV / 3.6.
# CV (calorific value, MJ/m3) varies slightly by region/day and is printed on each
# bill; default 39.5 gives ~11.19 kWh/m3.  Override via the gas_kwh_per_m3 ctor arg
# to match your bill exactly.
GAS_VCF          = 1.02264
GAS_CALORIFIC_MJ = 39.5
GAS_KWH_PER_M3   = GAS_VCF * GAS_CALORIFIC_MJ / 3.6   # ~11.19

# Tariff key constants
TARIFF_TRACKER  = "tracker"
TARIFF_GO       = "go"
TARIFF_FLUX     = "flux"
TARIFF_IGO      = "igo"
TARIFF_IFLUX    = "iflux"
TARIFF_AGILE    = "agile"
TARIFF_FLEXIBLE = "flexible"   # Octopus Flexible / standard variable rate
TARIFF_UNKNOWN  = "unknown"

# Product code prefixes for auto-detection
TARIFF_PRODUCT_PREFIXES = {
    TARIFF_TRACKER:  ("SILVER", "TRACKER"),
    TARIFF_GO:       ("GO-VAR",),
    TARIFF_FLUX:     ("FLUX-IMPORT",),
    TARIFF_IGO:      ("INTELLI-VAR", "INTELLI-GO"),
    TARIFF_IFLUX:    ("INTELLI-FLUX",),
    TARIFF_AGILE:    ("AGILE-",),
    TARIFF_FLEXIBLE: ("VAR-", "FLEX-", "SILVER-FLEX"),
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


def _safe_float(value, default=None):
    """Coerce to float, returning default on None / non-numeric."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


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
                 region="F", data_dir=None, logger=None,
                 gas_mprn="", gas_serial="",
                 export_mpan="", export_serial="",
                 gas_kwh_per_m3=GAS_KWH_PER_M3):
        """Initialise Octopus API client.

        Args:
            api_key:        Octopus API key (sk_live_...)
            account_id:     Octopus account number (A-XXXXXXXX)
            mpan:           13-digit electricity import meter point number
            serial:         Electricity import meter serial number
            region:         Grid region code A-P (default F = North East)
            data_dir:       Cache directory
            logger:         Logger instance
            gas_mprn:       Gas meter point reference number (optional)
            gas_serial:     Gas meter serial number (optional)
            export_mpan:    Electricity export meter point number (optional)
            export_serial:  Electricity export meter serial number (optional)
            gas_kwh_per_m3: m3->kWh calorific factor (default ~11.19)
        """
        self.api_key        = api_key
        self.account_id     = account_id
        self.mpan           = mpan
        self.serial         = serial
        self.region         = region.upper()
        self.data_dir       = data_dir or ""
        self.logger         = logger or logging.getLogger("SigenEnergyManager.Octopus")
        self.gas_mprn       = gas_mprn or ""
        self.gas_serial     = gas_serial or ""
        self.export_mpan    = export_mpan or ""
        self.export_serial  = export_serial or ""
        self.gas_kwh_per_m3 = _safe_float(gas_kwh_per_m3, GAS_KWH_PER_M3) or GAS_KWH_PER_M3

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
        self._financials_cache    = None   # Kraken ledger rates + balance
        self._financials_cache_at = 0.0
        self._financials_neg_at   = 0.0    # last failure (negative-cache debounce)

        # Rate-limit tracker.  Octopus permits roughly 100 requests/hour per
        # endpoint family.  We're nowhere near that under normal poll cadence
        # (every 30 min), but multiple plugin reloads in quick succession can
        # spike the count.  When the rolling hourly window passes
        # _RATE_LIMIT_WARN we log a WARNING; above _RATE_LIMIT_HARD we short
        # the request and let the caller fall back to cache.
        self._request_log       = []     # list of monotonic timestamps
        self._RATE_LIMIT_WINDOW = 3600   # seconds
        self._RATE_LIMIT_WARN   = 80     # log warning above this
        self._RATE_LIMIT_HARD   = 95     # refuse new requests above this

    def _record_request(self):
        """Note a single API call for the rate-limit tracker.  Returns True if
        the request is permitted, False if we're at the hard cap (caller
        should bail and use cached data)."""
        now = time.time()
        cutoff = now - self._RATE_LIMIT_WINDOW
        # Prune older entries
        self._request_log = [t for t in self._request_log if t >= cutoff]
        count = len(self._request_log)
        if count >= self._RATE_LIMIT_HARD:
            self.logger.warning(
                f"[Octopus] Rate-limit guard: {count} requests in last hour "
                f"(cap {self._RATE_LIMIT_HARD}). Skipping this call — using cache."
            )
            return False
        if count >= self._RATE_LIMIT_WARN and (count - self._RATE_LIMIT_WARN) % 5 == 0:
            self.logger.warning(
                f"[Octopus] Rate-limit approaching: {count} requests in last hour "
                f"(soft cap {self._RATE_LIMIT_WARN}). Consider increasing poll intervals."
            )
        self._request_log.append(now)
        return True

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
        """Fetch rates for all monitored tariffs (Tracker, Go, Flux, Flexible if active).

        Returns dict keyed by tariff_key with rate sub-dicts.
        Used to populate the tariffMonitor device.
        """
        result = {}

        # Active tariff from cache — no extra API call
        tariff_info = self.get_current_tariff()

        tracker = self._get_tracker_rates(force=force)
        if tracker:
            result[TARIFF_TRACKER] = tracker

        for tariff_key in (TARIFF_GO, TARIFF_FLUX):
            tou = self._get_tou_rates(tariff_key, force=force)
            if tou:
                result[tariff_key] = tou

        # Fetch Flexible unit rate only when that is the active tariff
        if tariff_info and tariff_info.get("tariff_key") == TARIFF_FLEXIBLE:
            flexible = self._get_flexible_rate(tariff_info=tariff_info, force=force)
            if flexible.get("today_p") is not None:
                result[TARIFF_FLEXIBLE] = flexible

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
    # Public: Export-MPAN consumption (v5.19+)
    # ================================================================

    def _sum_consumption_for_date(self, url, date_str):
        """Sum all half-hourly readings at `url` for one local (Europe/London) day.

        Shared by the import / export / gas per-day helpers.  Octopus settles
        readings over ~24-48h, so callers should only query dates at least a
        couple of calendar days old.  Returns:
            { "value": float, "slots": int }   on success (value in the meter's
                                                native unit: kWh for elec, m3 for gas)
            { "value": None,  "slots": 0 }      if no data yet (unsettled / missing)
            None                                 on auth/network failure
        """
        try:
            try:
                from zoneinfo import ZoneInfo
                day_start = datetime.strptime(date_str, "%Y-%m-%d").replace(
                    tzinfo=ZoneInfo("Europe/London"))
            except Exception:
                try:
                    import pytz
                    day_start = pytz.timezone("Europe/London").localize(
                        datetime.strptime(date_str, "%Y-%m-%d"))
                except Exception:
                    day_start = datetime.strptime(date_str, "%Y-%m-%d")
            day_end = day_start + timedelta(days=1)
            period_from = day_start.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
            period_to   = day_end.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        except ValueError:
            return None

        params = {
            "period_from": period_from,
            "period_to":   period_to,
            "page_size":   100,
            "order_by":    "period",
        }
        try:
            intervals = self._paginate(url, params, authenticated=True)
        except Exception as exc:
            self.logger.debug(f"[Octopus] Consumption fetch failed for {date_str} @ {url}: {exc}")
            return None
        if intervals is None:
            return None
        if not intervals:
            return {"value": None, "slots": 0}

        total = 0.0
        slots = 0
        for interval in intervals:
            try:
                v = float(interval.get("consumption", 0))
                if v < 0:
                    continue
                total += v
                slots += 1
            except (TypeError, ValueError):
                continue
        return {"value": round(total, 3), "slots": slots}

    def get_export_kwh_for_date(self, date_str, export_mpan, export_serial):
        """Sum all half-hourly export readings for one local (Europe/London) day.

        Returns { "kwh": float|None, "slots": int } or None on failure.
        """
        if not export_mpan or not export_serial:
            return None
        url = (
            f"{OCTOPUS_API_BASE}/electricity-meter-points/{export_mpan}/"
            f"meters/{export_serial}/consumption/"
        )
        r = self._sum_consumption_for_date(url, date_str)
        if r is None:
            return None
        return {"kwh": r["value"], "slots": r["slots"]}

    def get_import_kwh_for_date(self, date_str):
        """Sum grid-import kWh for one local day (settled data only).

        Returns { "kwh": float|None, "slots": int } or None on failure.
        """
        if not self.mpan or not self.serial:
            return None
        url = (
            f"{OCTOPUS_API_BASE}/electricity-meter-points/{self.mpan}/"
            f"meters/{self.serial}/consumption/"
        )
        r = self._sum_consumption_for_date(url, date_str)
        if r is None:
            return None
        return {"kwh": r["value"], "slots": r["slots"]}

    def get_gas_kwh_for_date(self, date_str):
        """Sum gas consumption for one local day, returning both m3 and kWh.

        Octopus returns m3 for metric SMETS meters; kWh = m3 * gas_kwh_per_m3.
        Returns { "m3": float|None, "kwh": float|None, "slots": int } or None.
        """
        if not self.gas_mprn or not self.gas_serial:
            return None
        url = (
            f"{OCTOPUS_API_BASE}/gas-meter-points/{self.gas_mprn}/"
            f"meters/{self.gas_serial}/consumption/"
        )
        r = self._sum_consumption_for_date(url, date_str)
        if r is None:
            return None
        m3  = r["value"]
        kwh = round(m3 * self.gas_kwh_per_m3, 3) if m3 is not None else None
        return {"m3": m3, "kwh": kwh, "slots": r["slots"]}

    # ================================================================
    # Public: Account financials (Kraken ledger — bill-exact)
    # ================================================================

    def _financials_failed(self, now):
        """Stamp the negative-cache window and return the last good value (stale)
        or None if financials were never successfully fetched.  Prevents a Kraken
        outage from making every /api/status poll fire a fresh GraphQL request."""
        self._financials_neg_at = now
        return self._financials_cache

    def get_account_financials(self, force=False):
        """Bill-exact standing/unit rates + account balance from the Kraken ledger.

        Uses the account's ACTIVE agreements (active:true), so it always reflects
        the tariff currently in force and survives a tariff change with no config
        change.  Import vs export is distinguished by meter-point MPAN.  All rates
        in pence inc-VAT; balance in GBP (positive = in credit).  Returns:
            {
              "elec":   {"standing_p","unit_p","tariff_code","display_name"} | None,
              "export": {"unit_p","display_name"} | None,
              "gas":    {"standing_p","unit_p","tariff_code","display_name"} | None,
              "balance_gbp": float | None,
            }
        or None on auth/network failure.  Note: for Tracker the elec unit_p is
        TODAY's rate (changes daily) — use the dated rate endpoints for history.
        """
        now = time.time()
        if (not force and self._financials_cache is not None
                and now - self._financials_cache_at < FINANCIALS_CACHE_TTL):
            return self._financials_cache
        # Debounce failures: after a recent failure return the last good value
        # (or None) without re-hitting the network on every /api/status poll.
        if not force and now - self._financials_neg_at < FINANCIALS_NEG_CACHE_TTL:
            return self._financials_cache
        if not self.api_key or not self.account_id:
            return self._financials_failed(now)
        token = self._get_kraken_token()
        if not token:
            return self._financials_failed(now)

        query = json.dumps({
            "query": (
                "query ($a: String!) { account(accountNumber: $a) {"
                "  balance"
                "  electricityAgreements(active: true) { meterPoint { mpan } tariff { __typename"
                "    ...on StandardTariff   { tariffCode displayName standingCharge unitRate }"
                "    ...on HalfHourlyTariff { tariffCode displayName standingCharge } } }"
                "  gasAgreements(active: true) { tariff { __typename"
                "    ...on GasTariffType { tariffCode displayName standingCharge unitRate } } }"
                "}}"
            ),
            "variables": {"a": self.account_id},
        })
        try:
            response = requests.post(
                KRAKEN_GRAPHQL,
                data=query.encode(),
                headers={"Content-Type": "application/json",
                         "Authorization": f"JWT {token}"},
                timeout=REQUEST_TIMEOUT,
            )
            if not response.ok:
                self.logger.debug(f"Kraken financials query failed: HTTP {response.status_code}")
                return self._financials_failed(now)
            payload = response.json()
        except (requests.RequestException, ValueError) as e:
            self.logger.debug(f"Kraken financials error: {e}")
            return self._financials_failed(now)

        acct = ((payload or {}).get("data") or {}).get("account") or {}
        if not acct:
            errs = (payload or {}).get("errors")
            if errs:
                self.logger.debug(f"Kraken financials GraphQL errors: {errs}")
            return self._financials_failed(now)

        result = {"elec": None, "export": None, "gas": None, "balance_gbp": None}

        bal = acct.get("balance")
        if isinstance(bal, (int, float)):
            result["balance_gbp"] = round(bal / 100.0, 2)

        for agr in acct.get("electricityAgreements", []) or []:
            t    = agr.get("tariff") or {}
            code = t.get("tariffCode", "") or ""
            mpan = ((agr.get("meterPoint") or {}).get("mpan")) or ""
            # Classify import vs export: the import MPAN is definitive; otherwise
            # the export MPAN, the OUTGOING product family, or simply "a meter
            # point that isn't our import meter" all mark it as export.
            if mpan and self.mpan and mpan == self.mpan:
                is_export = False
            elif self.export_mpan and mpan == self.export_mpan:
                is_export = True
            elif "OUTGOING" in code.upper():
                is_export = True
            else:
                is_export = bool(mpan and self.mpan and mpan != self.mpan)
            # First active agreement wins (active:true should return one each).
            if is_export:
                if result["export"] is None:
                    result["export"] = {
                        "unit_p":       _safe_float(t.get("unitRate")),
                        "display_name": t.get("displayName"),
                    }
            elif result["elec"] is None:
                result["elec"] = {
                    "standing_p":   _safe_float(t.get("standingCharge")),
                    "unit_p":       _safe_float(t.get("unitRate")),
                    "tariff_code":  code,
                    "display_name": t.get("displayName"),
                }

        for agr in acct.get("gasAgreements", []) or []:
            if result["gas"] is not None:
                break
            t = agr.get("tariff") or {}
            result["gas"] = {
                "standing_p":   _safe_float(t.get("standingCharge")),
                "unit_p":       _safe_float(t.get("unitRate")),
                "tariff_code":  t.get("tariffCode", "") or "",
                "display_name": t.get("displayName"),
            }

        self._financials_cache    = result
        self._financials_cache_at = now
        return result

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
            self.logger.debug("Tracker product code not found (tariff may be suspended)")
            return {"today_p": None, "tomorrow_p": None}

        tariff_code = self._build_tariff_code(product_code)

        # Today's rate
        today_rate = self._fetch_current_rate(product_code, tariff_code)

        # Tomorrow's rate (published around 16:00 each day)
        tomorrow_date   = (datetime.now(timezone.utc) + timedelta(days=1)).date()
        tomorrow_slots  = self._fetch_rate_schedule(product_code, tariff_code, tomorrow_date)
        tomorrow_rate   = tomorrow_slots[0].get("value_inc_vat") if tomorrow_slots else None

        result = {
            "today_p":    today_rate,
            "tomorrow_p": tomorrow_rate,
        }
        self._rates_cache[cache_key] = {"data": result, "cached_at": now}
        return result

    def get_active_rate_schedule(self, force=False):
        """Today's and tomorrow's raw rate slots for the ACTIVE import tariff.

        Single source of truth for the elec_rates_today_json / elec_rates_tomorrow_json
        Indigo variables the openmeteo battery optimiser consumes — this lets the plugin
        replace the standalone octopus_tracker_rate.py script. Each slot is the raw
        Octopus dict {valid_from, valid_to, value_inc_vat, ...}; tomorrow_slots stays
        empty until Octopus publishes (~16:00 local). Tariff-agnostic (uses whatever
        get_current_tariff() resolves), so it is correct for non-Tracker users too.
        """
        cache_key = "active_schedule"
        now = time.time()
        cached = self._rates_cache.get(cache_key)
        if not force and cached and now - cached["cached_at"] < RATES_CACHE_TTL:
            return cached["data"]

        tariff_info  = self.get_current_tariff(force=force) or {}
        product_code = tariff_info.get("product_code")
        tariff_code  = tariff_info.get("tariff_code")
        if not product_code or not tariff_code:
            return {"product_code": product_code, "today_slots": [], "tomorrow_slots": []}

        today_date    = datetime.now(timezone.utc).date()
        tomorrow_date = today_date + timedelta(days=1)
        result = {
            "product_code":   product_code,
            "today_slots":    self._fetch_rate_schedule(product_code, tariff_code, today_date),
            "tomorrow_slots": self._fetch_rate_schedule(product_code, tariff_code, tomorrow_date),
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

    def _get_flexible_rate(self, tariff_info=None, force=False):
        """Fetch the unit rate for the active Flexible Octopus tariff.

        Flexible Octopus is a simple flat rate with no time-of-use windows.
        The rate is fetched from the standard-unit-rates endpoint using the
        product_code and tariff_code already identified by get_current_tariff().

        Returns dict: {"today_p": float_or_None}
        """
        cache_key = "flexible_rate"
        now = time.time()
        cached = self._rates_cache.get(cache_key)
        if not force and cached and now - cached["cached_at"] < RATES_CACHE_TTL:
            return cached["data"]

        if not tariff_info:
            tariff_info = self.get_current_tariff()

        product_code = (tariff_info or {}).get("product_code")
        tariff_code  = (tariff_info or {}).get("tariff_code")

        if not product_code or not tariff_code:
            self.logger.debug("Flexible rate: no product/tariff code available")
            return {"today_p": None}

        rate   = self._fetch_current_rate(product_code, tariff_code)
        result = {"today_p": rate}
        self._rates_cache[cache_key] = {"data": result, "cached_at": now}
        if rate is not None:
            self.logger.debug(f"Flexible rate: {rate:.4f}p/kWh ({product_code})")
        return result

    def _parse_tou_slots(self, slots, window):
        """Parse rate slots into cheap/standard/peak breakdown.

        TARIFF_WINDOWS values (cheap_start, cheap_end, peak times) are LOCAL
        time strings (Europe/London). Slot timestamps from Octopus are UTC.
        Convert each slot to Europe/London before comparing — otherwise during
        BST the cheap window is detected 1 hour earlier than reality.
        """
        if not slots:
            return {}

        cheap_start = window.get("cheap_start", "02:00")
        cheap_end   = window.get("cheap_end", "05:00")

        # Resolve Europe/London timezone once. Prefer stdlib zoneinfo
        # (Python 3.9+) so the conversion works even when pytz isn't
        # installed (e.g. test environments). pytz remains a fallback.
        _tz_l = None
        try:
            from zoneinfo import ZoneInfo
            _tz_l = ZoneInfo("Europe/London")
        except ImportError:
            try:
                import pytz
                _tz_l = pytz.timezone("Europe/London")
            except ImportError:
                _tz_l = None

        # Group rates by time window
        cheap_rates    = []
        peak_rates     = []
        standard_rates = []

        for slot in slots:
            try:
                valid_from = datetime.fromisoformat(
                    slot["valid_from"].replace("Z", "+00:00")
                )
                # Convert UTC slot to Europe/London for window comparison.
                # TARIFF_WINDOWS are stored in local time.
                if _tz_l is not None:
                    valid_local = valid_from.astimezone(_tz_l)
                else:
                    # Fallback: assume UTC == local (wrong in BST but won't crash)
                    valid_local = valid_from
                hour_min = valid_local.strftime("%H:%M")
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

        Returns the most recently launched matching product (highest available_from)
        so that when Octopus issues a new Tracker product (e.g. SILVER-26-04-XX),
        it is preferred over the older SILVER-25-04-11.

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

            results   = response.get("results", [])
            best_code = None
            best_date = ""   # ISO string — lexicographic sort is correct for YYYY-MM-DD

            for product in results:
                code = product.get("code", "")
                for prefix in prefixes:
                    if code.startswith(prefix):
                        avail_from = product.get("available_from") or ""
                        if avail_from > best_date:
                            best_date = avail_from
                            best_code = code
                        break   # matched a prefix — no need to check others

            return best_code

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
            "query": "mutation ($k: String!) { obtainKrakenToken(input: { APIKey: $k }) { token } }",
            "variables": {"k": self.api_key},
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
                self._kraken_token = None       # invalidate any stale cache
                return None
            try:
                payload = response.json()
            except ValueError as e:
                self.logger.debug(f"Kraken token malformed JSON: {e}")
                self._kraken_token = None
                return None
            token = (payload or {}).get("data", {}).get("obtainKrakenToken", {}).get("token")
            if token:
                self._kraken_token    = token
                self._kraken_token_at = now
                self.logger.debug("Kraken token obtained")
            else:
                # Empty/null token means auth failed — purge any stale cache so
                # subsequent calls don't keep sending a token that won't work.
                self._kraken_token = None
            return token
        except (requests.Timeout, requests.ConnectionError) as e:
            self.logger.debug(f"Kraken token network error: {e}")
            self._kraken_token = None
            return None
        except requests.RequestException as e:
            self.logger.debug(f"Kraken token request error: {e}")
            self._kraken_token = None
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
                "query ($a: String!) { account(accountNumber: $a) {"
                "  electricityAgreements(active: true) {"
                "    tariff {"
                "      ...on TariffType       { displayName productCode tariffCode }"
                "      ...on HalfHourlyTariff { displayName productCode tariffCode }"
                "    }"
                "  }"
                "}}"
            ),
            "variables": {"a": self.account_id},
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

            try:
                payload = response.json()
            except ValueError as e:
                self.logger.debug(f"Kraken tariff query malformed JSON: {e}")
                return None
            agreements = (
                (payload or {})
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
        product_code = self._product_from_tariff_code(tariff_code)

        for tariff_key, prefixes in TARIFF_PRODUCT_PREFIXES.items():
            for prefix in prefixes:
                if product_code.upper().startswith(prefix):
                    display_names = {
                        TARIFF_TRACKER:  "Octopus Tracker",
                        TARIFF_GO:       "Octopus Go",
                        TARIFF_FLUX:     "Octopus Flux",
                        TARIFF_IGO:      "Intelligent Go",
                        TARIFF_IFLUX:    "Intelligent Flux",
                        TARIFF_AGILE:    "Octopus Agile",
                        TARIFF_FLEXIBLE: "Octopus Flexible",
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

        if not self._record_request():
            raise OctopusApiError("Rate-limit guard active — request skipped")

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
