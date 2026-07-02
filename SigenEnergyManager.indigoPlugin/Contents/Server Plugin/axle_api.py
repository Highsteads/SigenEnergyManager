#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    axle_api.py
# Description: Axle VPP REST API client - polls for export event schedule
# Author:      CliveS & Claude Opus 4.8
# Date:        26-06-2026
# Version:     1.2
#
# Adapted from SigenergySolar v3.1 axle_api.py
# Changes: Updated logger name to SigenEnergyManager; _parse_dt guards non-string input

import logging
import requests
from datetime import datetime, timezone


AXLE_EVENT_URL  = "https://api.axle.energy/vpp/home-assistant/event"
REQUEST_TIMEOUT = 15    # seconds


class AxleAPI:
    """Axle VPP REST client.

    Polls the Axle Home-Assistant-compatible endpoint to retrieve the
    next scheduled export event. Returns a normalised dict or None.

    The endpoint requires a Bearer token (obtained at signup) and returns
    either a single event object or null when no event is upcoming.

    Typical response when an event is scheduled:
        {
            "start_time":    "2026-03-20T18:00:00+00:00",
            "end_time":      "2026-03-20T19:30:00+00:00",
            "import_export": "export",
            "updated_at":    "2026-03-20T10:00:00+00:00"
        }

    Returns None (null body) when no event is scheduled.

    VPP cycle (Events Only mode - Axle uses Sigen Cloud for direct control):
    - Plugin pre-charges battery to required SOC before event
    - Plugin writes discharge cutoff register (40048) for reserve protection
    - During event: Axle controls via Sigen Cloud (no plugin Modbus writes)
    - After event: plugin detects EMS mode reversion and restores cutoff
    """

    def __init__(self, api_token):
        """Initialise the Axle API client.

        Args:
            api_token: Bearer token from Axle signup (stored in IndigoSecrets.py).
        """
        self.api_token = api_token
        self.logger    = logging.getLogger("SigenEnergyManager.AxleAPI")

    def get_next_event(self):
        """Fetch the next VPP event from the Axle API.

        Returns:
            dict with keys:
                start_time    (datetime, tz-aware UTC)
                end_time      (datetime, tz-aware UTC)
                import_export (str, e.g. "export")
                duration_hrs  (float, computed)
                raw           (original API dict)
            or None if no event is scheduled or API call fails.
        """
        if not self.api_token:
            self.logger.warning("Axle API token not configured - cannot poll for events")
            return None

        headers = {
            "Authorization": f"Bearer {self.api_token}",
            "Accept":        "application/json",
        }

        try:
            response = requests.get(
                AXLE_EVENT_URL,
                headers=headers,
                timeout=REQUEST_TIMEOUT,
            )

            if response.status_code == 401:
                self.logger.error(
                    "Axle API authentication failed - check AXLE_API_KEY in IndigoSecrets.py"
                )
                return None

            if response.status_code not in (200, 204):
                # Error branch must precede the empty-body check — a 5xx with
                # an empty body is an outage, not a routine "no event scheduled".
                self.logger.error(
                    f"Axle API HTTP error: {response.status_code} - {response.text[:200]}"
                )
                return None

            if response.status_code == 204 or not response.content:
                self.logger.info("Axle poll: no event scheduled (204 / empty body)")
                return None

            try:
                data = response.json()
            except ValueError as e:
                self.logger.error(f"Axle API malformed JSON response: {e}")
                return None

            if not data:
                self.logger.info("Axle poll: no event scheduled (null response)")
                return None

            start_time = self._parse_dt(data.get("start_time"))
            end_time   = self._parse_dt(data.get("end_time"))

            if start_time is None or end_time is None:
                self.logger.error(
                    f"Axle API: missing or unparseable timestamps in response: {data}"
                )
                return None

            duration_hrs = (end_time - start_time).total_seconds() / 3600.0

            # Reject implausible windows — a swapped/equal timestamp pair would
            # otherwise propagate a negative or zero duration into pre-charge
            # SOC sizing and dashboard earnings arithmetic.
            if duration_hrs <= 0 or duration_hrs > 24:
                self.logger.warning(
                    f"Axle API: implausible event window {start_time} -> {end_time} "
                    f"({duration_hrs:.1f}h) — ignoring event"
                )
                return None

            try:
                from zoneinfo import ZoneInfo
                _tz  = ZoneInfo("Europe/London")
                _s   = start_time.astimezone(_tz).strftime("%H:%M")
                _e   = end_time.astimezone(_tz).strftime("%H:%M")
                _lbl = end_time.astimezone(_tz).strftime("%Z")   # BST or GMT
            except Exception:
                # Fallback formats the UTC datetimes — label them honestly.
                _s, _e = start_time.strftime("%H:%M"), end_time.strftime("%H:%M")
                _lbl   = "UTC"
            self.logger.debug(
                f"Axle event: {data.get('import_export', '?')} "
                f"{_s} - {_e} {_lbl} ({duration_hrs:.1f}h)"
            )

            # Optional fields added to Axle's API in Apr 2026:
            #   forecast_dispatch_kwh — Axle's expected battery dispatch for the
            #                           event (used by the dashboard to surface
            #                           "expected VPP earnings tomorrow").
            #   estimated_revenue_p   — pence at Axle's contracted rate.
            # Both are absent on older API responses; default to None so callers
            # can render "—" gracefully rather than misreport zero.
            forecast_kwh = data.get("forecast_dispatch_kwh")
            estimated_p  = data.get("estimated_revenue_p")
            try:
                forecast_kwh = float(forecast_kwh) if forecast_kwh is not None else None
            except (TypeError, ValueError):
                forecast_kwh = None
            try:
                estimated_p  = float(estimated_p) if estimated_p is not None else None
            except (TypeError, ValueError):
                estimated_p  = None

            return {
                "start_time":             start_time,
                "end_time":               end_time,
                "import_export":          data.get("import_export", "export"),
                "duration_hrs":           duration_hrs,
                "forecast_dispatch_kwh":  forecast_kwh,
                "estimated_revenue_p":    estimated_p,
                "raw":                    data,
            }

        except requests.exceptions.ConnectionError:
            self.logger.warning("Axle API: connection error (no internet?)")
            return None
        except requests.exceptions.Timeout:
            self.logger.warning(f"Axle API: request timed out after {REQUEST_TIMEOUT}s")
            return None
        except Exception as e:
            self.logger.error(f"Axle API: unexpected error: {e}")
            return None

    def _parse_dt(self, dt_str):
        """Parse an ISO-8601 datetime string to a tz-aware UTC datetime."""
        if not dt_str or not isinstance(dt_str, str):
            # A numeric/dict value (malformed API response) would otherwise raise
            # AttributeError on .replace() and escape the ValueError/TypeError guard.
            return None
        try:
            dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
            return dt.astimezone(timezone.utc)
        except (ValueError, TypeError) as e:
            self.logger.error(f"Cannot parse datetime '{dt_str}': {e}")
            return None
