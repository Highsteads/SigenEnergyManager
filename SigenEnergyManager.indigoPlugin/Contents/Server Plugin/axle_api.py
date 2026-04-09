#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    axle_api.py
# Description: Axle VPP REST API client - polls for export event schedule
# Author:      CliveS & Claude Sonnet 4.6
# Date:        09-04-2026
# Version:     1.1
#
# Adapted from SigenergySolar v3.1 axle_api.py
# Changes: Updated logger name to SigenEnergyManager

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
            api_token: Bearer token from Axle signup (stored in secrets.py).
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
                    "Axle API authentication failed - check AXLE_API_KEY in secrets.py"
                )
                return None

            if response.status_code == 204 or not response.content:
                self.logger.debug("Axle API: no event scheduled (204 / empty body)")
                return None

            if response.status_code != 200:
                self.logger.error(
                    f"Axle API HTTP error: {response.status_code} - {response.text[:200]}"
                )
                return None

            data = response.json()

            if not data:
                self.logger.debug("Axle API: no event scheduled (null response)")
                return None

            start_time = self._parse_dt(data.get("start_time"))
            end_time   = self._parse_dt(data.get("end_time"))

            if start_time is None or end_time is None:
                self.logger.error(
                    f"Axle API: missing or unparseable timestamps in response: {data}"
                )
                return None

            duration_hrs = (end_time - start_time).total_seconds() / 3600.0

            try:
                import pytz
                _tz = pytz.timezone("Europe/London")
                _s  = start_time.astimezone(_tz).strftime("%H:%M")
                _e  = end_time.astimezone(_tz).strftime("%H:%M")
            except Exception:
                _s, _e = start_time.strftime("%H:%M"), end_time.strftime("%H:%M")
            self.logger.debug(
                f"Axle event: {data.get('import_export', '?')} "
                f"{_s} - {_e} BST ({duration_hrs:.1f}h)"
            )

            return {
                "start_time":    start_time,
                "end_time":      end_time,
                "import_export": data.get("import_export", "export"),
                "duration_hrs":  duration_hrs,
                "raw":           data,
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
        if not dt_str:
            return None
        try:
            dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
            return dt.astimezone(timezone.utc)
        except (ValueError, TypeError) as e:
            self.logger.error(f"Cannot parse datetime '{dt_str}': {e}")
            return None
