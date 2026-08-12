#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    storm_watch.py
# Description: Storm/wind warning detection for SigenEnergyManager.
#              Polls the MeteoAlarm CAP Atom feed — the official Met Office
#              warning source for the UK (free, no API key required).
#              Each CAP entry is filtered by polygon to confirm the warning
#              area actually covers the configured site before acting.
#              Severity is read from the current feed schema (cap:event colour +
#              cap:severity) with a legacy awareness_* fallback and a schema-drift
#              guard. Onset time is checked: warnings are only acted on when the
#              storm is due within STORM_ACTIVATE_HOURS (default 24h), so
#              early announcements do not prematurely alter battery behaviour.
#              Met Office warnings are calibrated for real disruption/power-cut
#              risk, avoiding false positives from ordinary windy days.
#              Returns a severity string: "none", "yellow", "amber", or "red".
# Author:      CliveS & Claude Fable 5
# Date:        02-07-2026
# Version:     1.5 (failure paths return None — caller holds previous level, no false all-clear)

import re
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

# MeteoAlarm severity mapping.
#
# The MeteoAlarm feed schema CHANGED (confirmed live 26-Jun-2026): the old
# <cap:awareness_type> / <cap:awareness_level> elements are no longer served. The
# current feed carries <cap:event> (e.g. "Yellow thunderstorm warning") + a CAP
# <cap:severity> (Minor/Moderate/Severe/Extreme). We classify from the colour word
# in the event/title FIRST (most direct — MeteoAlarm titles are literally colour-led),
# then fall back to cap:severity, then to the legacy numeric awareness code. Keeping
# all three paths means a future schema flip can't silently disable storm watch again.

# Legacy MeteoAlarm numeric awareness-level codes -> severity string (fallback)
_MA_LEVEL_MAP = {"2": "yellow", "3": "amber", "4": "red"}

# CAP severity -> colour, used when the title carries no explicit colour word
_MA_SEVERITY_MAP = {"minor": "yellow", "moderate": "yellow",
                    "severe": "amber", "extreme": "red"}

# Hazard keywords (matched against cap:event / atom:title) that carry genuine
# power-cut / disruption risk — the reason storm watch exists.
# Matched on WORD BOUNDARIES: bare substring matching made "ice" match
# notice/service and "rain" match training — the title-fallback path feeds
# whole sentences into this matcher, exactly where loose matching bites.
# v5.65.0: MeteoAlarm's cap:event vocabulary joins compound hazards with an
# UNDERSCORE — snow_ice, rain_flood, coastal_flooding — and `_` is a word
# character in Python's re, so `\b` cannot fire beside it and every compound
# token was silently discarded. Measured against the shipped regex: snow_ice,
# rain_flood and coastal_flooding all MISSED while the hyphenated snow-ice
# matched. The live UK feed carries the underscored form today.
#
# This is the power-cut reserve feature, so a missed warning means the 50%
# reserve never engages, the plugin keeps exporting and grid-charging into the
# warning, and check_storm_level returns a confident "no active warnings".
#
# Fix: normalise the separators BEFORE matching rather than trying to enumerate
# every compound in the keyword set — a vocabulary we do not control. The word
# boundaries stay: they are what stops "ice" matching notice/service and "rain"
# matching training on the title-fallback path, and tests pin that.
# "flood" joins "flooding" — a plain flood warning missed on its own, separators
# or not.
_WIND_TYPES = {"wind", "winds", "gale", "gales", "thunderstorm", "thunderstorms",
               "storm", "storms", "snow", "ice", "rain", "flood", "flooding"}
_HAZARD_RE = re.compile(
    r"\b(" + "|".join(sorted(_WIND_TYPES, key=len, reverse=True)) + r")\b")
# Separators MeteoAlarm uses to join compound event tokens. Collapsed to spaces
# so the word-boundary matcher can see each component word.
_SEPARATOR_RE = re.compile(r"[_\-/]+")


def _normalise_hazard(text):
    """Lower-case and split compound tokens so `\\b` can match each component."""
    return _SEPARATOR_RE.sub(" ", (text or "").lower())

# Colour words likewise on word boundaries ("red" is a substring of
# predicted/hundred/covered); worst of ALL matches wins, not first-found.
_COLOUR_RE = re.compile(r"\b(red|amber|yellow)\b")


# ============================================================
# Public API
# ============================================================

def check_storm_level(lat=LATITUDE, lon=LONGITUDE, location_name="your location"):
    """
    Check the MeteoAlarm CAP feed for active wind/storm warnings covering the
    site at (lat, lon).

    Args:
        lat, lon       site coordinates (default the module's Medomsley constants;
                       the plugin passes its own configured site coordinates).
        location_name  label used in the human-readable reason string.

    Filters for:
      - wind/storm/snow/ice hazards only (power-cut risk)
      - non-expired warnings
      - warnings whose CAP polygon covers the site

    Returns:
        level  (str|None): "none", "yellow", "amber", or "red" — or None when the
                           check FAILED (feed unreachable, XML unparseable, or
                           schema drift). None means "unknown", NOT all-clear:
                           the caller must hold its previous level rather than
                           dropping an active storm reserve on a flaky poll.
        reason (str):      human-readable explanation
    """
    url = "https://feeds.meteoalarm.org/feeds/meteoalarm-legacy-atom-united-kingdom"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "SigenEnergyManager/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            xml_bytes = resp.read()
    except Exception as exc:
        return None, f"MeteoAlarm unavailable: {exc}"

    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        return None, f"MeteoAlarm XML parse error: {exc}"

    # Atom + CAP namespaces used in MeteoAlarm feeds
    ns = {
        "atom": "http://www.w3.org/2005/Atom",
        "cap":  "urn:oasis:names:tc:emergency:cap:1.2",
    }

    now_utc       = datetime.now(timezone.utc)
    horizon_utc   = now_utc + timedelta(hours=STORM_ACTIVATE_HOURS)
    highest       = "none"
    reasons       = []
    pending       = []   # warnings that exist but are beyond the activation horizon
    entries_total = 0    # CAP entries seen
    entries_known = 0    # entries whose schema we could read (cap:event or awareness_type)
    # v5.65.0: events read successfully but rejected by the hazard filter. The
    # rejection used to be a silent `continue`, so a vocabulary change (the
    # underscore compounds) left no trace anywhere — and the schema-drift guard
    # cannot catch it either, because such an entry parses fine and increments
    # entries_known. Surfaced in the all-clear reason so "no warnings" can never
    # again mean "warnings I could not read".
    ignored       = []

    for entry in root.findall(".//atom:entry", ns):
        entries_total += 1

        # --- Read the hazard text + level from whichever schema the feed uses ---
        # Current MeteoAlarm: <cap:event>"Yellow thunderstorm warning"</cap:event>
        #                     + <cap:severity>Moderate</cap:severity>
        # Legacy MeteoAlarm:  <cap:awareness_type> + <cap:awareness_level>"2; Yellow; …"
        event_el = entry.find(".//cap:event", ns)
        atype_el = entry.find(".//cap:awareness_type", ns)
        title_el = entry.find("atom:title", ns)

        if event_el is not None and event_el.text:
            hazard_text  = event_el.text.strip()
            known_schema = True
        elif atype_el is not None and atype_el.text:
            hazard_text  = atype_el.text.strip()
            known_schema = True
        elif title_el is not None and title_el.text:
            # Title alone still carries hazard + colour ("Yellow thunderstorm warning…")
            hazard_text  = title_el.text.strip()
            known_schema = False
        else:
            hazard_text  = ""
            known_schema = False

        if known_schema:
            entries_known += 1
        if not hazard_text:
            continue

        # --- Hazard filter (power-cut-relevant only, word-boundary match) ---
        # Separators collapsed first — see _normalise_hazard. A rejection is now
        # logged at DEBUG: the old silent `continue` meant a vocabulary change
        # left no trace anywhere, and the schema-drift guard cannot catch it
        # either (the entry parsed fine, so entries_known was incremented).
        hazard_lower = _normalise_hazard(hazard_text)
        if not _HAZARD_RE.search(hazard_lower):
            ignored.append(hazard_text.strip())
            continue

        # --- Severity: colour word in the event/title first, then cap:severity,
        #     then the legacy numeric awareness code ---
        level = None
        for colour in _COLOUR_RE.findall(hazard_lower):
            level = colour if level is None else _level_max(level, colour)
        if level is None:
            sev_el = entry.find(".//cap:severity", ns)
            if sev_el is not None and sev_el.text:
                level = _MA_SEVERITY_MAP.get(sev_el.text.strip().lower())
        if level is None:
            alevel_el = entry.find(".//cap:awareness_level", ns)
            if alevel_el is not None and alevel_el.text:
                code  = alevel_el.text.strip().split(";")[0].strip()
                level = _MA_LEVEL_MAP.get(code)
        if level is None:
            continue   # Green / unknown severity — skip

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

        # --- Location check: does this warning's polygon cover the site? ---
        if not _warning_covers_location(entry, ns, lat, lon):
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

        # Title for logging (full title preferred, else the hazard text)
        title = (title_el.text.strip() if title_el is not None and title_el.text
                 else hazard_text)

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

    # Schema-drift guard: a non-empty feed where NO entry exposed a readable schema
    # means MeteoAlarm changed its format again (the awareness_* -> event/severity
    # change of 2026 silently disabled this feature for weeks). Surface it loudly via
    # a distinctive reason the caller escalates to WARNING, so it can't go unnoticed.
    # Fire it only when NOTHING usable was extracted — entries parsed via the
    # atom:title fallback still yield valid levels/pending items, and discarding
    # a real title-parsed warning is exactly the failure this guard exists to stop.
    if entries_total > 0 and entries_known == 0 and highest == "none" and not pending:
        return None, (f"MeteoAlarm feed format unrecognised — {entries_total} "
                      f"entries, 0 parseable (storm_watch parser may need updating)")
    if entries_known == 0 and highest != "none":
        reasons.append("(parsed from titles only — MeteoAlarm schema may have drifted)")

    if highest == "none":
        base = f"MeteoAlarm: no active wind/storm warnings covering {location_name}"
        if pending:
            base += " | " + " | ".join(pending[:2])
        if ignored:
            # Names what was seen and set aside, so a warning the filter cannot
            # read is visible rather than indistinguishable from a quiet day.
            base += (f" | {len(ignored)} non-power-cut event(s) ignored: "
                     + ", ".join(sorted(set(ignored))[:3]))
        return "none", base
    return highest, " | ".join(reasons[:3])
