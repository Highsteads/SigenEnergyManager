#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    daily_energy.py
# Description: Today's energy figures DERIVED from the inverter's monotone lifetime
#              counters anchored at local midnight. Nothing is accumulated, nothing
#              is reset, no guard can latch, and the inverter's own clock plays no
#              part. Design note: docs/daily-energy-revamp.md.
# Author:      CliveS & Claude Fable 5.1
# Date:        05-09-2026 12:40
# Version:     1.0
#
# WHY THIS MODULE EXISTS
# ----------------------
# Until v5.88.0 the plugin kept six daily totals as mutable running state: a
# number that was written to, guarded, held and zeroed. Three different
# definitions of "today" fed it (Europe/London midnight, the inverter's own day
# for its daily registers, and whenever the next poll landed for the lifetime
# anchors), and once one figure went wrong nothing could recompute it. On
# 4-Sep-2026 the house figure froze on yesterday's total for two whole days.
#
# Here a daily figure is a pure function of two readings:
#
#     today[key] = latest[key] - anchor[today][key]
#
# where latest is the newest lifetime reading and anchor is the reading taken at
# local midnight. A missed midnight is a late anchor, which is repairable. A
# restart recovers exactly. Absent data reads as None, never as a number.
#
# Every key is a plant-level LIFETIME counter (U64, gain 100) — probed read-only
# on the live inverter 05-Sep-2026 and validated against the identity
# pv + gridImport + batteryDischarge - gridExport - batteryCharge - home = 0,
# which closed to 0.00 kWh over a whole day.

import time
from datetime import datetime, timedelta

try:
    from london_time import london_localise, london_now
except ImportError:                          # pragma: no cover — standalone use
    london_localise = None
    london_now      = None

# The six quantities, in the order the identity reads them.
KEYS = ("pv", "home", "gridImport", "gridExport", "batteryCharge", "batteryDischarge")

# read_all() dict key that carries each LIFETIME counter.
LIFETIME_DATA_KEYS = {
    "pv":               "pvLifetimeKwh",
    "home":             "homeLifetimeKwh",
    "gridImport":       "gridImportLifetimeKwh",
    "gridExport":       "gridExportLifetimeKwh",
    "batteryCharge":    "batteryChargeLifetimeKwh",
    "batteryDischarge": "batteryDischargeLifetimeKwh",
}

# The device's OWN daily counters. Never a source of the daily figure — they
# live on the inverter's clock — but usable to RECOVER a missing anchor
# (anchor = lifetime - device_daily) and to cross-check the derived house figure.
RECOVERY_DATA_KEYS = {
    "home":             "homeDailyDirectKwh",
    "batteryCharge":    "batteryDailyChargeKwh",
    "batteryDischarge": "batteryDailyDischargeKwh",
}

# A lifetime counter above this is not a reading, it is a decode of a sentinel.
MAX_PLAUSIBLE_LIFETIME_KWH = 1.0e8

# Anchors older than this are dropped on rollover: today and yesterday are the
# only days anything reads.
ANCHOR_RETENTION_DAYS = 2

# A provisional (pre-midnight) anchor is REPLACED by the first fresh reading
# that lands within this many seconds after midnight. Later than that the
# reading is not a boundary value: the plugin was down, and what happens next
# depends on how close to midnight the provisional reading was.
PROVISIONAL_UPGRADE_WINDOW_S = 600

# A provisional reading taken within this many seconds BEFORE midnight is a
# good enough boundary to keep when no post-midnight reading arrived in time.
# Older than this and the key is anchored late instead, flagged partial —
# attributing last evening's flows to today would be the worse error.
PROVISIONAL_MAX_AGE_S = 900

# How far a counter may step BACKWARDS before it is treated as a meter reset
# rather than rounding noise (the counters are integer hundredths of a kWh).
BACKWARDS_TOLERANCE_KWH = 0.011


def local_midnight_epoch(date_str):
    """Epoch seconds of local (Europe/London) midnight starting `date_str`.

    None when the date is malformed or no zone database is reachable — the
    caller treats None as "cannot judge", never as midnight-at-UTC.
    """
    try:
        naive = datetime.strptime(date_str, "%Y-%m-%d")
    except (TypeError, ValueError):
        return None
    if london_localise is None:
        return None
    aware = london_localise(naive)
    if aware is None:
        return None
    return aware.timestamp()


def local_today_str(now=None):
    """Today's date in Europe/London as YYYY-MM-DD. `now` overrides for tests."""
    if now is None and london_now is not None:
        now = london_now()
    if now is None:
        now = datetime.now()
    return now.strftime("%Y-%m-%d")


class DailyEnergy:
    """Lifetime-counter anchors and the daily figures derived from them.

    Thread-safety: none of its own. The plugin calls every method under its
    state lock, exactly as it did for the store keys this replaces.
    """

    def __init__(self):
        self.today_date = None      # YYYY-MM-DD the object believes is current
        # date_str -> {"values": {key: kwh}, "sources": {key: str}, "soc_pct": float|None,
        #              "captured_at": epoch, "provisional": bool}
        self.anchors    = {}
        # key -> {"kwh": float, "read_at": epoch}
        self.latest     = {}
        # keys whose counter stepped backwards on the last observe (meter reset)
        self.last_backwards = ()

    # ------------------------------------------------------------------
    # Observation
    # ------------------------------------------------------------------

    def rollover(self, new_date):
        """Local midnight: `new_date` starts now.

        A PROVISIONAL anchor is taken from the last readings held (which are from
        just before midnight, typically well under a minute old), so the day's
        figures start at zero immediately. The first FRESH reading stamped after
        midnight replaces it — see observe(). Idempotent: a second call for the
        same date changes nothing.
        """
        if new_date == self.today_date and new_date in self.anchors:
            return
        self.today_date = new_date
        if new_date not in self.anchors and self.latest:
            values = {k: v["kwh"] for k, v in self.latest.items()}
            self.anchors[new_date] = {
                "values":      values,
                "sources":     {k: "provisional" for k in values},
                "soc_pct":     None,
                "captured_at": max(v["read_at"] for v in self.latest.values()),
                "provisional": True,
            }
        self._prune(new_date)

    def observe(self, readings, read_at, today, soc_pct=None, recovery=None, fresh=True):
        """Take in one cycle's lifetime readings.

        readings:  {key: kwh} for any subset of KEYS — the LIFETIME counters.
        read_at:   epoch seconds the counters were read.
        today:     local date string the plugin considers current.
        soc_pct:   battery SOC at this reading (recorded on a new anchor only).
        recovery:  {key: device_daily_kwh} read THIS cycle — the device's own
                   daily counters, for anchor recovery. Never a cached value.
        fresh:     False when the readings were served from the modbus cache;
                   the object then updates nothing (they are the values it
                   already holds) and takes no anchor decision from them.
        """
        readings = {k: float(v) for k, v in (readings or {}).items()
                    if k in KEYS and v is not None
                    and 0.0 <= float(v) < MAX_PLAUSIBLE_LIFETIME_KWH}
        recovery = {k: float(v) for k, v in (recovery or {}).items()
                    if k in RECOVERY_DATA_KEYS and v is not None and float(v) >= 0.0}
        if today != self.today_date:
            self.rollover(today)
        if not fresh or not readings:
            return

        # Meter-reset guard: a lifetime counter cannot go down. If it did, the
        # plant re-based it, and every anchor for that key is meaningless.
        backwards = []
        for key, kwh in readings.items():
            prev = self.latest.get(key)
            if prev is not None and kwh < prev["kwh"] - BACKWARDS_TOLERANCE_KWH:
                backwards.append(key)
        self.last_backwards = tuple(backwards)
        for key in backwards:
            for anchor in self.anchors.values():
                anchor["values"].pop(key, None)
                anchor["sources"].pop(key, None)

        for key, kwh in readings.items():
            self.latest[key] = {"kwh": kwh, "read_at": float(read_at)}

        anchor = self.anchors.get(today)
        if anchor is None:
            self.anchors[today] = self._new_anchor(readings, recovery, read_at, soc_pct, today)
            self._prune(today)
            return

        midnight = local_midnight_epoch(today)
        if anchor.get("provisional") and midnight is not None and read_at >= midnight:
            if read_at - midnight <= PROVISIONAL_UPGRADE_WINDOW_S:
                # The first genuine post-midnight reading: the real boundary value.
                for key, kwh in readings.items():
                    anchor["values"][key]  = kwh
                    anchor["sources"][key] = "midnight"
            elif midnight - float(anchor.get("captured_at") or 0.0) <= PROVISIONAL_MAX_AGE_S:
                # No reading arrived near midnight (plugin down), but the
                # provisional one was taken just before it: keep it as the
                # boundary. Recovery still wins for the keys it covers.
                for key in list(anchor["values"]):
                    if key in recovery and key in readings:
                        anchor["values"][key]  = max(0.0, readings[key] - recovery[key])
                        anchor["sources"][key] = "recovered"
                    else:
                        anchor["sources"][key] = "boundary"
            else:
                # Down over midnight AND the last reading was hours old: the
                # morning is unattributable. Anchor late, say so, recover
                # whatever the device's own daily counters can tell us.
                anchor["values"].clear()
                anchor["sources"].clear()
            anchor["provisional"] = False
            anchor["captured_at"] = float(read_at)
            if anchor.get("soc_pct") is None and soc_pct is not None:
                anchor["soc_pct"] = float(soc_pct)

        # A key that had no anchor (register absent at the boundary, or a meter
        # reset above) gets one now, recovered from the device's daily counter
        # where possible.
        for key, kwh in readings.items():
            if key in anchor["values"]:
                continue
            if key in recovery:
                anchor["values"][key]  = max(0.0, kwh - recovery[key])
                anchor["sources"][key] = "recovered"
            else:
                anchor["values"][key]  = kwh
                anchor["sources"][key] = "late"
        if anchor.get("soc_pct") is None and soc_pct is not None:
            anchor["soc_pct"] = float(soc_pct)

    def _new_anchor(self, readings, recovery, read_at, soc_pct, today=None):
        """First anchor for a day the object has no boundary reading for.

        A reading inside the upgrade window after midnight IS the boundary (the
        first observe of a fresh process started at 00:00:20, say). Otherwise
        this is the plugin starting mid-day or a missed midnight: the device's
        own daily counters recover an exact boundary for the keys they cover,
        and any other key is anchored NOW and flagged "late", so today() can say
        the figure is partial rather than present it as the day's total.
        """
        midnight = local_midnight_epoch(today) if today else None
        at_boundary = (midnight is not None
                       and 0.0 <= float(read_at) - midnight <= PROVISIONAL_UPGRADE_WINDOW_S)
        values, sources = {}, {}
        for key, kwh in readings.items():
            if at_boundary:
                values[key]  = kwh
                sources[key] = "midnight"
            elif key in recovery:
                values[key]  = max(0.0, kwh - recovery[key])
                sources[key] = "recovered"
            else:
                values[key]  = kwh
                sources[key] = "late"
        return {
            "values":      values,
            "sources":     sources,
            "soc_pct":     float(soc_pct) if soc_pct is not None else None,
            "captured_at": float(read_at),
            "provisional": False,
        }

    def _prune(self, today):
        try:
            cutoff = (datetime.strptime(today, "%Y-%m-%d")
                      - timedelta(days=ANCHOR_RETENTION_DAYS - 1)).strftime("%Y-%m-%d")
        except (TypeError, ValueError):
            return
        for date_str in list(self.anchors):
            if date_str < cutoff:
                del self.anchors[date_str]

    # ------------------------------------------------------------------
    # Migration
    # ------------------------------------------------------------------

    def migrate_legacy(self, today, pv_start=None, import_start=None, export_start=None):
        """Seed today's anchor from the v5.88 accumulator fields.

        `pv_lifetime_start_kwh` and friends WERE midnight anchors for three of the
        six keys; carrying them across means an upgrade mid-day loses nothing for
        those keys. Only fills keys that have no anchor. Returns the keys seeded.
        """
        legacy = {"pv": pv_start, "gridImport": import_start, "gridExport": export_start}
        seeded = []
        if today != self.today_date:
            self.today_date = today
        anchor = self.anchors.get(today)
        if anchor is None:
            anchor = {"values": {}, "sources": {}, "soc_pct": None,
                      "captured_at": time.time(), "provisional": False}
            self.anchors[today] = anchor
        for key, value in legacy.items():
            if value is None or key in anchor["values"]:
                continue
            try:
                v = float(value)
            except (TypeError, ValueError):
                continue
            if 0.0 <= v < MAX_PLAUSIBLE_LIFETIME_KWH:
                anchor["values"][key]  = v
                anchor["sources"][key] = "migrated"
                seeded.append(key)
        return seeded

    def set_anchor(self, date_str, values, source="seeded", soc_pct=None, captured_at=None):
        """Install anchor values for a day outright (the mend script, tests)."""
        anchor = self.anchors.get(date_str)
        if anchor is None:
            anchor = {"values": {}, "sources": {}, "soc_pct": None,
                      "captured_at": float(captured_at or time.time()), "provisional": False}
            self.anchors[date_str] = anchor
        for key, v in (values or {}).items():
            if key in KEYS and v is not None:
                anchor["values"][key]  = float(v)
                anchor["sources"][key] = source
        if soc_pct is not None:
            anchor["soc_pct"] = float(soc_pct)
        anchor["provisional"] = False
        if self.today_date is None:
            self.today_date = date_str

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------

    def today(self, date_str=None):
        """Today's figures: {"values": {key: kwh|None}, "sources": {...},
        "partial": bool, "provisional": bool, "soc_at_anchor": float|None}.

        A key is None when it has no anchor or no reading. A negative delta
        (rounding of a counter that has not moved) reads as 0.0; a backwards
        step beyond tolerance was already handled in observe().
        """
        date_str = date_str or self.today_date
        anchor   = self.anchors.get(date_str) if date_str else None
        values, sources = {}, {}
        for key in KEYS:
            latest = self.latest.get(key)
            a = anchor["values"].get(key) if anchor else None
            if latest is None or a is None:
                values[key]  = None
                sources[key] = (anchor["sources"].get(key) if anchor else None) or "absent"
                continue
            values[key]  = round(max(0.0, latest["kwh"] - a), 2)
            sources[key] = anchor["sources"].get(key, "midnight")
        partial = any(s == "late" for s in sources.values())
        return {
            "values":        values,
            "sources":       sources,
            "partial":       partial,
            "provisional":   bool(anchor and anchor.get("provisional")),
            "soc_at_anchor": anchor.get("soc_pct") if anchor else None,
            "anchor_at":     anchor.get("captured_at") if anchor else None,
        }

    def completed(self, date_str):
        """A FINISHED day's figures: anchor[next day] - anchor[day].

        Exact by construction — two boundary readings, nothing in between
        matters. Falls back to latest - anchor when the next day's anchor does
        not exist yet (the day is still running, or the object never saw the
        boundary). None when the day has no anchor at all. Same shape as today().
        """
        anchor = self.anchors.get(date_str)
        if anchor is None:
            return None
        try:
            next_day = (datetime.strptime(date_str, "%Y-%m-%d")
                        + timedelta(days=1)).strftime("%Y-%m-%d")
        except (TypeError, ValueError):
            return None
        end = self.anchors.get(next_day)
        if end is None:
            return self.today(date_str)
        values, sources = {}, {}
        for key in KEYS:
            a, b = anchor["values"].get(key), end["values"].get(key)
            if a is None or b is None:
                values[key]  = None
                sources[key] = anchor["sources"].get(key) or "absent"
                continue
            values[key]  = round(max(0.0, b - a), 2)
            sources[key] = anchor["sources"].get(key, "midnight")
        return {
            "values":        values,
            "sources":       sources,
            "partial":       any(s == "late" for s in sources.values()),
            "provisional":   bool(end.get("provisional")),
            "soc_at_anchor": anchor.get("soc_pct"),
            "anchor_at":     anchor.get("captured_at"),
        }

    def residual(self, date_str=None):
        """The identity residual for the day, or None if any term is missing.

        pv + gridImport + batteryDischarge - gridExport - batteryCharge - home.
        Near zero when every anchor is right. The plant computes its own house
        figure from the same flows, so a residual is a wrong ANCHOR, not a
        wrong meter — which is exactly what needs catching.
        """
        day = self.completed(date_str) if date_str and date_str != self.today_date else self.today(date_str)
        if day is None:
            return None
        v = day["values"]
        if any(v[k] is None for k in KEYS):
            return None
        return round(v["pv"] + v["gridImport"] + v["batteryDischarge"]
                     - v["gridExport"] - v["batteryCharge"] - v["home"], 2)

    def lifetime(self, key):
        """Latest lifetime kWh for `key`, or None."""
        entry = self.latest.get(key)
        return entry["kwh"] if entry else None

    def lifetime_snapshot(self):
        """{key: kwh} of every key with a reading — for half-hourly deltas."""
        return {k: v["kwh"] for k, v in self.latest.items()}

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def to_dict(self):
        return {
            "version":    1,
            "today_date": self.today_date,
            "anchors":    {d: {"values": dict(a["values"]), "sources": dict(a["sources"]),
                               "soc_pct": a.get("soc_pct"), "captured_at": a.get("captured_at"),
                               "provisional": bool(a.get("provisional"))}
                           for d, a in self.anchors.items()},
            "latest":     {k: {"kwh": v["kwh"], "read_at": v["read_at"]}
                           for k, v in self.latest.items()},
        }

    @classmethod
    def from_dict(cls, data):
        """Rebuild from to_dict() output. Malformed input yields an empty object
        rather than raising — the plugin then anchors afresh, which is the safe
        failure (partial day, flagged) rather than a crash at startup."""
        obj = cls()
        if not isinstance(data, dict):
            return obj
        obj.today_date = data.get("today_date") or None
        anchors = data.get("anchors")
        latest  = data.get("latest")
        if not isinstance(anchors, dict):
            anchors = {}
        if not isinstance(latest, dict):
            latest = {}
        for date_str, a in anchors.items():
            if not isinstance(a, dict):
                continue
            raw_values = a.get("values")
            if not isinstance(raw_values, dict):
                continue
            values = {}
            for k, v in raw_values.items():
                try:
                    if k in KEYS and v is not None:
                        values[k] = float(v)
                except (TypeError, ValueError):
                    continue
            if not values:
                continue
            obj.anchors[date_str] = {
                "values":      values,
                "sources":     {k: str((a.get("sources") or {}).get(k, "midnight"))
                                if isinstance(a.get("sources"), dict) else "midnight"
                                for k in values},
                "soc_pct":     a.get("soc_pct"),
                "captured_at": float(a.get("captured_at") or 0.0),
                "provisional": bool(a.get("provisional")),
            }
        for key, v in latest.items():
            if key in KEYS and isinstance(v, dict) and v.get("kwh") is not None:
                try:
                    obj.latest[key] = {"kwh": float(v["kwh"]),
                                       "read_at": float(v.get("read_at") or 0.0)}
                except (TypeError, ValueError):
                    continue
        return obj


def readings_from_data(data):
    """Pull the lifetime readings out of a read_all() dict: {key: kwh}."""
    out = {}
    for key, data_key in LIFETIME_DATA_KEYS.items():
        v = data.get(data_key) if data else None
        if v is not None:
            out[key] = v
    return out


def recovery_from_data(data):
    """The device's own daily counters present in THIS cycle's dict: {key: kwh}."""
    out = {}
    for key, data_key in RECOVERY_DATA_KEYS.items():
        v = data.get(data_key) if data else None
        if v is not None:
            out[key] = v
    return out
