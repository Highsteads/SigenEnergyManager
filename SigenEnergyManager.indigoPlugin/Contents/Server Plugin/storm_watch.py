#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    storm_watch.py
# Description: Storm/wind warning detection for SigenEnergyManager.
#              Polls the MeteoAlarm CAP Atom feed — the official Met Office
#              warning source for the UK (free, no API key required).
#              Each CAP entry is filtered by polygon to confirm the warning
#              area actually covers Medomsley, County Durham before acting.
#              Onset time is checked: warnings are only acted on when the
#              storm is due within STORM_ACTIVATE_HOURS (default 24h), so
#              early announcements do not prematurely alter battery behaviour.
#              Met Office warnings are calibrated for real disruption/power-cut
#              risk, avoiding false positives from ordinary windy days.
#              Returns a severity string: "none", "yellow", "amber", or "red".
# Author:      CliveS & Claude Sonnet 4.6
# Date:        05-04-2026
# Version:     1.3

import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

# ============================================================
# Location: Medomsley, County Durham
# ============================================================
LATITUDE  = 54.882
LONGITUDE = -1.818

# ============================================================
# How far ahead (hours) to activate the storm battery override.
# Warnings issued beyond this horizon are logged but ignored —
# the override activates only when the storm is genuinely imminent.
# ============================================================
STORM_ACTIVATE_HOURS = 24

# ============================================================
# Severity hierarchy (index = severity; higher = worse)
# ============================================================
_LEVELS = ["none", "yellow", "amber", "red"]


def _level_max(a, b):
    """Return the more severe of two storm level strings."""
    ia = _LEVELS.index(a) if a in _LEVELS else 0
    ib = _LEVELS.index(b) if b in _LEVELS else 0
    return _LEVELS[max(ia, ib)]


# ============================================================
# Geometry helpers — point-in-polygon (ray-casting)
# ============================================================

def _parse_cap_polygon(polygon_text):
    """
    Parse a CAP polygon string into a list of (lat, lon) tuples.
    CAP format: "lat,lon lat,lon lat,lon ..."  (space-separated pairs)
    Returns list of (float, float) or [] on parse failure.
    """
    points = []
    for pair in polygon_text.strip().split():
        parts = pair.split(",")
        if len(parts) == 2:
            try:
                points.append((float(parts[0]), float(parts[1])))
            except ValueError:
                pass
    return points


def _point_in_polygon(lat, lon, polygon):
    """
    Ray-casting algorithm — returns True if (lat, lon) is inside the polygon.
    polygon: list of (lat, lon) tuples.
    Returns False for degenerate polygons (< 3 points).
    """
    n = len(polygon)
    if n < 3:
        return False
    inside = False
    x, y = lon, lat          # work in lon/lat (x/y) space
    j = n - 1
    for i in range(n):
        xi, yi = polygon[i][1], polygon[i][0]   # lon, lat
        xj, yj = polygon[j][1], polygon[j][0]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside


def _warning_covers_location(entry, ns, lat, lon):
    """
    Check whether a CAP entry's area polygon covers (lat, lon).
    Rules:
    - If the entry has one or more polygons, return True only if at least
      one polygon contains our point.
    - If the entry has NO polygon element (some entries omit it), return True
      conservatively so we do not silently miss a warning without area data.
    """
    polygons = entry.findall(".//cap:polygon", ns)
    if not polygons:
        return True   # No polygon — include conservatively
    for poly_el in polygons:
        if poly_el.text:
            pts = _parse_cap_polygon(poly_el.text)
            if pts and _point_in_polygon(lat, lon, pts):
                return True
    return False


# ============================================================
# MeteoAlarm CAP feed (official Met Office UK warnings)
# ============================================================

# MeteoAlarm numeric awareness-level codes -> severity string
_MA_LEVEL_MAP = {"2": "yellow", "3": "amber", "4": "red"}

# MeteoAlarm awareness types to consider (wind-related hazards)
_WIND_TYPES = {"wind", "thunderstorm", "thunderstorms", "rain-flooding"}


# ============================================================
# Public API
# ============================================================

def check_storm_level():
    """
    Check the MeteoAlarm CAP feed for active wind/storm warnings covering
    Medomsley, County Durham (54.882N, 1.818W).

    Filters for:
      - wind/thunderstorm awareness types only
      - non-expired warnings
      - warnings whose CAP polygon covers our location

    Returns:
        level  (str): "none", "yellow", "amber", or "red"
        reason (str): human-readable explanation
    """
    url = "https://feeds.meteoalarm.org/feeds/meteoalarm-legacy-atom-united-kingdom"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "SigenEnergyManager/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            xml_bytes = resp.read()
    except Exception as exc:
        return "none", f"MeteoAlarm unavailable: {exc}"

    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        return "none", f"MeteoAlarm XML parse error: {exc}"

    # Atom + CAP namespaces used in MeteoAlarm feeds
    ns = {
        "atom": "http://www.w3.org/2005/Atom",
        "cap":  "urn:oasis:names:tc:emergency:cap:1.2",
    }

    now_utc      = datetime.now(timezone.utc)
    horizon_utc  = now_utc + timedelta(hours=STORM_ACTIVATE_HOURS)
    highest      = "none"
    reasons      = []
    pending      = []   # warnings that exist but are beyond the activation horizon

    for entry in root.findall(".//atom:entry", ns):

        # --- Filter by awareness type (wind/storm only) ---
        atype_el = entry.find(".//cap:awareness_type", ns)
        if atype_el is None or not atype_el.text:
            continue
        atype_lower = atype_el.text.strip().lower()
        if not any(w in atype_lower for w in _WIND_TYPES):
            continue

        # --- Map awareness level ---
        alevel_el = entry.find(".//cap:awareness_level", ns)
        if alevel_el is None or not alevel_el.text:
            continue
        # Format: "2; Yellow; Moderate" — numeric code is the first token
        code  = alevel_el.text.strip().split(";")[0].strip()
        level = _MA_LEVEL_MAP.get(code)
        if level is None:
            continue   # Green (code "1") or unknown — skip

        # --- Skip expired warnings ---
        expires_el = entry.find(".//cap:expires", ns)
        if expires_el is not None and expires_el.text:
            try:
                exp_dt = datetime.fromisoformat(expires_el.text.strip())
                if exp_dt.tzinfo is None:
                    exp_dt = exp_dt.replace(tzinfo=timezone.utc)
                if exp_dt <= now_utc:
                    continue
            except (ValueError, OverflowError):
                pass   # Cannot parse — include conservatively

        # --- Location check: does this warning's polygon cover Medomsley? ---
        if not _warning_covers_location(entry, ns, LATITUDE, LONGITUDE):
            continue   # Warning is for another part of the UK — ignore

        # --- Onset horizon check: only activate if storm is within 24 hours ---
        # Try cap:onset first, fall back to cap:effective, then treat as imminent
        onset_dt = None
        for tag in ("cap:onset", "cap:effective"):
            el = entry.find(f".//{tag}", ns)
            if el is not None and el.text:
                try:
                    onset_dt = datetime.fromisoformat(el.text.strip())
                    if onset_dt.tzinfo is None:
                        onset_dt = onset_dt.replace(tzinfo=timezone.utc)
                    break
                except (ValueError, OverflowError):
                    pass

        # Collect title for logging
        title_el = entry.find("atom:title", ns)
        title    = (title_el.text.strip() if title_el is not None and title_el.text
                    else atype_el.text.strip())

        if onset_dt is not None and onset_dt > horizon_utc:
            # Storm is forecast but still more than STORM_ACTIVATE_HOURS away
            hrs_away = (onset_dt - now_utc).total_seconds() / 3600
            pending.append(
                f"MeteoAlarm {level.upper()} (in {hrs_away:.0f}h — monitoring, not yet active): {title}"
            )
            continue   # Do not activate override yet

        # Storm is imminent (onset within 24h, or onset unknown — conservative)
        highest = _level_max(highest, level)
        if onset_dt is not None:
            onset_str = onset_dt.strftime("%a %d %b %H:%M UTC")
            reasons.append(f"MeteoAlarm {level.upper()} onset {onset_str}: {title}")
        else:
            reasons.append(f"MeteoAlarm {level.upper()}: {title}")

    if highest == "none":
        base = "MeteoAlarm: no active wind/storm warnings covering Medomsley"
        if pending:
            base += " | " + " | ".join(pending[:2])
        return "none", base
    return highest, " | ".join(reasons[:3])
