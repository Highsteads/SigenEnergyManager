#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    mend_daily_energy_2026_09.py
# Description: One-off repair of the records damaged by the v5.87.0/v5.88.0 midnight
#              cache fault (fixed in v5.89.0): daily_history.json for 4-Sep-2026, the
#              half-hourly table's home_kwh from 4-Sep 00:00 to the v5.89.0 restart, and
#              TariffAnalyser's daily_summary row for 4-Sep. Kept in the repo as the
#              reference for any future repair of the same shape. Backs up first.
#              Read-only against the SQL Logger (PK-ranged, never a ts scan).
# Author:      CliveS & Claude Fable 5.1
# Date:        05-09-2026 14:20
# Version:     1.0
#
# Usage:  python3 scripts/mend_daily_energy_2026_09.py [--apply]
#         Without --apply it prints what it WOULD change and touches nothing.

import glob
import json
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

LON = ZoneInfo("Europe/London")
PA  = "/Library/Application Support/Perceptive Automation"
BUNDLE_ID = "com.clives.indigoplugin.sigenergy-energy-manager"

# The plugin's SQL Logger device (Sigenergy Inverter) and the days to mend.
INVERTER_DEVICE_ID = 1563154425
MEND_FROM = "2026-09-04"           # first bad day (inclusive)
APPLY = "--apply" in sys.argv


def indigo_dir():
    """The newest versioned Indigo folder — never a hardcoded version."""
    dirs = sorted(glob.glob(os.path.join(PA, "Indigo 20*")))
    if not dirs:
        raise SystemExit("no Indigo install folder under " + PA)
    return dirs[-1]


def local_to_utc_str(local_str):
    """'YYYY-MM-DDTHH:MM:SS' local -> 'YYYY-MM-DD HH:MM:SS' UTC (the SQL Logger's ts form)."""
    dt = datetime.strptime(local_str[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=LON)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def load_counter_series(db_path, since_utc):
    """Forward-filled series of the five daily counters from the SQL Logger.

    PK-ranged: the last 300k rows comfortably cover several days of this chatty
    device, and an `id >=` range is an index scan that holds no long read lock
    (a ts filter would full-scan a 1.8 GB table and wedge the writer).
    Returns a list of (ts_utc_str, {col: value}) with every column filled.
    """
    cols = ("pvdailykwh", "griddailyimportkwh", "griddailyexportkwh",
            "batterydailychargekwh", "batterydailydischargekwh")
    con = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True)
    con.execute("PRAGMA busy_timeout = 5000")
    t = "device_history_%d" % INVERTER_DEVICE_ID
    lo = con.execute("SELECT MAX(id) - 300000 FROM %s" % t).fetchone()[0]
    q = ("SELECT ts, %s FROM %s WHERE id >= ? AND (%s) ORDER BY id"
         % (", ".join(cols), t, " OR ".join("%s IS NOT NULL" % c for c in cols)))
    series, cur = [], {}
    for row in con.execute(q, (lo,)):
        ts = row[0]
        for c, v in zip(cols, row[1:]):
            if v is not None:
                cur[c] = float(v)
        if ts >= since_utc and len(cur) == len(cols):
            series.append((ts, dict(cur)))
    con.close()
    if not series:
        raise SystemExit("no counter rows found since %s — widen the PK window" % since_utc)
    return series


def value_at(series, ts_utc):
    """The counters as they stood at ts (last row at or before it)."""
    best = None
    for ts, vals in series:
        if ts <= ts_utc:
            best = vals
        else:
            break
    return best if best is not None else series[0][1]


def slot_deltas(series, start_local, end_local):
    """Delta of each daily counter across a window, surviving the midnight reset.

    Walks every sample inside the window: a rise adds the increment, a DROP
    (the counter reset) adds the post-reset value instead — so a window that
    spans the reset gets (peak - start) + (end - 0), and one that ends at
    00:00:00, before the counters have reset, gets the plain difference.
    """
    s_utc, e_utc = local_to_utc_str(start_local), local_to_utc_str(end_local)
    prev   = dict(value_at(series, s_utc))
    totals = {k: 0.0 for k in prev}
    for ts, vals in series:
        if ts <= s_utc:
            continue
        if ts > e_utc:
            break
        for k in totals:
            if vals[k] >= prev[k] - 0.011:
                totals[k] += vals[k] - prev[k]
            else:
                totals[k] += vals[k]          # reset: count what accrued since
        prev = dict(vals)
    return {k: max(0.0, round(v, 4)) for k, v in totals.items()}


def house_from(d):
    return max(0.0, d["pvdailykwh"] + d["griddailyimportkwh"] + d["batterydailydischargekwh"]
               - d["griddailyexportkwh"] - d["batterydailychargekwh"])


def main():
    idir     = indigo_dir()
    hist_db  = os.path.join(idir, "Logs", "indigo_history.sqlite")
    data_dir = os.path.join(idir, "Preferences", "Plugins", BUNDLE_ID)
    dh_path  = os.path.join(data_dir, "daily_history.json")
    ts_db    = os.path.join(data_dir, "energy_timeseries.db")
    for p in (hist_db, dh_path, ts_db):
        if not os.path.exists(p):
            raise SystemExit("missing: " + p)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    print("mode:", "APPLY" if APPLY else "DRY RUN")

    since_utc = local_to_utc_str((datetime.strptime(MEND_FROM, "%Y-%m-%d") - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S"))
    series = load_counter_series(hist_db, since_utc)
    print("counter rows loaded:", len(series), "from", series[0][0], "UTC")

    # ---- 1. daily_history.json: whole-day figures for each bad day already recorded ----
    records = json.load(open(dh_path, encoding="utf-8"))
    by_date = {r.get("date"): r for r in records}
    changes = []
    for date_str in sorted(d for d in by_date if d and d >= MEND_FROM):
        day_start = date_str + "T00:00:00"
        day_end   = (datetime.strptime(date_str, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%dT00:00:00")
        d = slot_deltas(series, day_start, day_end)
        home = round(house_from(d), 2)
        rec  = by_date[date_str]
        new  = {"home_kwh": home,
                "battery_charge_kwh":    round(d["batterydailychargekwh"], 2),
                "battery_discharge_kwh": round(d["batterydailydischargekwh"], 2)}
        old  = {k: rec.get(k) for k in new}
        if any(abs(float(old[k] or 0.0) - new[k]) > 0.005 for k in new):
            changes.append((date_str, old, new))
            if APPLY:
                rec.update(new)
                rec["mend_note"] = ("%s: home/battery recomputed from the SQL Logger identity "
                                    "(pv + import + discharge - export - charge) after the v5.87-5.88 "
                                    "midnight cache fault; see docs/daily-energy-revamp.md" % stamp)
    for date_str, old, new in changes:
        print("daily_history %s: %s -> %s" % (date_str, old, new))
    if APPLY and changes:
        shutil.copy2(dh_path, dh_path + ".bak-mend-" + stamp)
        tmp = dh_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(records, fh, indent=2)
        os.replace(tmp, dh_path)
        print("daily_history.json written (backup .bak-mend-%s)" % stamp)

    # ---- 2. halfhourly: rows the old code wrote with home_kwh = 0 ----
    if APPLY:
        shutil.copy2(ts_db, ts_db + ".bak-mend-" + stamp)
    con = sqlite3.connect(ts_db, timeout=10.0)
    cols = {r[1] for r in con.execute("PRAGMA table_info(halfhourly)")}
    for c in ("battery_charge_kwh", "battery_discharge_kwh"):
        if c not in cols:
            if APPLY:
                con.execute("ALTER TABLE halfhourly ADD COLUMN %s REAL" % c)
            print("halfhourly: column %s %s" % (c, "added" if APPLY else "would be added"))
    rows = con.execute(
        "SELECT id, slot_start, slot_end, home_kwh FROM halfhourly "
        "WHERE slot_end >= ? AND home_kwh = 0 AND battery_charge_kwh IS NULL ORDER BY id",
        (MEND_FROM + "T00:00:00",)).fetchall() if ("battery_charge_kwh" in cols or APPLY) else \
        con.execute("SELECT id, slot_start, slot_end, home_kwh FROM halfhourly "
                    "WHERE slot_end >= ? AND home_kwh = 0 ORDER BY id", (MEND_FROM + "T00:00:00",)).fetchall()
    per_day = {}
    updates = []
    for rid, s, e, _h in rows:
        d = slot_deltas(series, s, e)
        home = round(house_from(d), 4)
        updates.append((home, round(d["batterydailychargekwh"], 4),
                        round(d["batterydailydischargekwh"], 4), rid))
        per_day.setdefault(e[:10], [0.0, 0])
        per_day[e[:10]][0] += home
        per_day[e[:10]][1] += 1
    for day, (tot, n) in sorted(per_day.items()):
        print("halfhourly %s: %d rows, home_kwh sum -> %.2f kWh" % (day, n, tot))
    if APPLY and updates:
        con.executemany("UPDATE halfhourly SET home_kwh = ?, battery_charge_kwh = ?, "
                        "battery_discharge_kwh = ? WHERE id = ?", updates)
        con.commit()
        print("halfhourly: %d rows updated" % len(updates))

    # ---- 3. daily_summary (TariffAnalyser's table): the sigen columns + the two it derives ----
    have_ds = con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='daily_summary'").fetchone()
    if have_ds:
        for date_str, old, new in changes:
            row = con.execute("SELECT home_kwh, tracker_avg_rate_p, elec_import_cost_gbp, "
                              "elec_export_revenue_gbp, cost_without_solar_gbp FROM daily_summary WHERE date = ?",
                              (date_str,)).fetchone()
            if row is None:
                continue
            home = new["home_kwh"]
            rate = row[1]
            cost_no_solar = round(home * rate / 100.0, 4) if rate else None
            savings = (round(cost_no_solar - (row[2] or 0.0) + (row[3] or 0.0), 4)
                       if cost_no_solar is not None and row[2] is not None else None)
            print("daily_summary %s: home %.3f -> %.3f, cost_without_solar %s -> %s"
                  % (date_str, row[0] or 0.0, home, row[4], cost_no_solar))
            if APPLY:
                con.execute("UPDATE daily_summary SET home_kwh = ?, battery_charge_kwh = ?, "
                            "battery_discharge_kwh = ?, cost_without_solar_gbp = ?, "
                            "savings_vs_no_solar_gbp = COALESCE(?, savings_vs_no_solar_gbp), "
                            "sigen_source = 'halfhourly' WHERE date = ?",
                            (home, new["battery_charge_kwh"], new["battery_discharge_kwh"],
                             cost_no_solar, savings, date_str))
                con.commit()
    con.close()
    print("done" if APPLY else "dry run complete — re-run with --apply to write")


if __name__ == "__main__":
    main()
