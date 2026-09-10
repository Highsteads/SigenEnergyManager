#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    agile_replay.py
# Description: Replays SigenEnergyManager's Agile import rule over a winter of REAL
#              half-hourly Octopus Agile prices, so the numbers behind v5.100.0's block
#              planner can be re-measured rather than believed. Pulls the rates from the
#              public products API (no key), caches them, and prints: the season cost of
#              starting at the single cheapest half-hour versus the cheapest contiguous
#              block versus the N cheapest half-hours in any order; the round-trip gate's
#              verdicts under its intended reference and both fallbacks; and a daytime
#              walk of the pre-publication reference bias. No Indigo, no Modbus, no key.
# Author:      CliveS & Claude Fable 5.1
# Date:        10-09-2026
# Version:     1.0
#
#   python3 scripts/agile_replay.py                       # 20 kWh at 9.5 kW, Oct-25 to Feb-26
#   python3 scripts/agile_replay.py --need 30 --charge-kw 6
#   python3 scripts/agile_replay.py --from 2026-10-01 --to 2027-03-01 --product AGILE-24-10-01

import argparse
import json
import math
import os
import sys
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

LONDON = ZoneInfo("Europe/London")
LAT, LNG = 54.882, -1.818            # the site; dawn is derived from sunrise here
EFFICIENCY = 0.94
CACHE_DIR = os.path.expanduser("~/.cache/sigen-agile-replay")


def fetch_rates(product, region, period_from, period_to):
    """All standard-unit-rate slots for the tariff between two dates, cached on disk."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    cache = os.path.join(CACHE_DIR, f"{product}_{region}_{period_from}_{period_to}.json")
    if os.path.exists(cache):
        return json.load(open(cache))
    tariff = f"E-1R-{product}-{region}"
    url = (f"https://api.octopus.energy/v1/products/{product}/electricity-tariffs/{tariff}/"
           f"standard-unit-rates/?" + urllib.parse.urlencode(
               {"period_from": f"{period_from}T00:00Z", "period_to": f"{period_to}T00:00Z",
                "page_size": 1500}))
    rows = []
    while url:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (agile_replay)"})
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.load(r)
        rows.extend(data["results"])
        url = data.get("next")
    rows.sort(key=lambda r: r["valid_from"])
    json.dump(rows, open(cache, "w"))
    return rows


def sunrise_utc(d):
    """NOAA sunrise for the site (the 'sunrise equation'), UTC."""
    from math import sin, cos, asin, acos, radians as R, degrees as D
    jd     = d.toordinal() + 1721424.5
    n      = math.ceil(jd - 2451545.0 + 0.0008)
    jstar  = n - LNG / 360.0
    m      = (357.5291 + 0.98560028 * jstar) % 360.0
    c      = 1.9148 * sin(R(m)) + 0.02 * sin(R(2 * m)) + 0.0003 * sin(R(3 * m))
    lam    = (m + c + 180.0 + 102.9372) % 360.0
    jtrans = 2451545.0 + jstar + 0.0053 * sin(R(m)) - 0.0069 * sin(R(2 * lam))
    delta  = asin(sin(R(lam)) * sin(R(23.4397)))
    cos_w  = (sin(R(-0.833)) - sin(R(LAT)) * sin(delta)) / (cos(R(LAT)) * cos(delta))
    w      = D(acos(max(-1.0, min(1.0, cos_w))))
    return (datetime(1970, 1, 1, tzinfo=timezone.utc)
            + timedelta(seconds=(jtrans - w / 360.0 - 2440587.5) * 86400.0))


def mean(xs):
    return sum(xs) / len(xs) if xs else None


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--need", type=float, default=20.0, help="grid-side kWh per night")
    ap.add_argument("--charge-kw", type=float, default=9.5, help="charge rate the battery takes")
    ap.add_argument("--dawn-offset", type=int, default=45, help="minutes after sunrise")
    ap.add_argument("--from", dest="date_from", default="2025-10-01")
    ap.add_argument("--to", dest="date_to", default="2026-03-01")
    ap.add_argument("--product", default="AGILE-24-10-01")
    ap.add_argument("--region", default="F")
    a = ap.parse_args()

    rows  = fetch_rates(a.product, a.region, a.date_from, a.date_to)
    slots = sorted(((datetime.fromisoformat(r["valid_from"].replace("Z", "+00:00")),
                     float(r["value_inc_vat"])) for r in rows), key=lambda x: x[0])
    by_dt = dict(slots)
    idx   = {dt: i for i, (dt, _) in enumerate(slots)}
    half  = timedelta(minutes=30)
    per   = a.charge_kw * 0.5
    energies, left = [], a.need
    while left > 1e-9:
        e = min(per, left); energies.append(e); left -= e
    n = len(energies)

    def forward_cost(i):
        if i + n > len(slots):
            return None
        return sum(slots[i + k][1] * e for k, e in enumerate(energies))

    def dawn_after(d):
        return sunrise_utc(d + timedelta(days=1)) + timedelta(minutes=a.dawn_offset)

    nights = []
    d = date.fromisoformat(a.date_from)
    last = date.fromisoformat(a.date_to) - timedelta(days=2)
    while d <= last:
        start = datetime(d.year, d.month, d.day, 16, 0, tzinfo=LONDON)
        dawn  = dawn_after(d)
        win   = [(dt, p) for dt, p in slots if start <= dt < dawn]
        if len(win) >= n + 2:
            i_min  = min(range(len(win)), key=lambda i: win[i][1])
            greedy = forward_cost(idx[win[i_min][0]])
            contig = min(c for c in (forward_cost(idx[dt]) for dt, _ in win) if c is not None)
            prices = sorted(p for _, p in win)
            best_n = sum(p * e for p, e in zip(prices, energies))
            day    = [p for dt, p in slots if dawn <= dt < dawn + timedelta(hours=12)]
            t17    = datetime(d.year, d.month, d.day, 17, 0, tzinfo=LONDON).astimezone(timezone.utc)
            nb     = [win[j][1] for j in (i_min - 1, i_min + 1) if 0 <= j < len(win)]
            nights.append(dict(
                d=d, hour=win[i_min][0].astimezone(LONDON).hour, greedy=greedy, contig=contig,
                best_n=best_n, eff=win[i_min][1] / EFFICIENCY, ref_day=mean(day),
                ref_17=by_dt.get(t17), spike=bool(nb) and (mean(nb) - win[i_min][1]) > 3.0,
            ))
        d += timedelta(days=1)

    g = sum(x["greedy"] for x in nights); c = sum(x["contig"] for x in nights); b = sum(x["best_n"] for x in nights)
    print(f"{a.product} region {a.region}, {a.date_from} to {a.date_to}: {len(nights)} nights, "
          f"{a.need:g} kWh at {a.charge_kw:g} kW = {n} half-hours, dawn = sunrise + {a.dawn_offset} min")
    print(f"  single cheapest slot then forward : GBP {g/100:8.2f}   (the rule shipped before v5.100.0)")
    print(f"  cheapest contiguous block         : GBP {c/100:8.2f}   ({(g-c)/100:+.2f} vs single slot)")
    print(f"  N cheapest half-hours, any order  : GBP {b/100:8.2f}   ({(g-b)/100:+.2f} vs single slot)")
    print(f"  nights where single slot != best block by >0.5p: "
          f"{sum(1 for x in nights if x['greedy'] - x['contig'] > 0.5)}; spike-down starts: "
          f"{sum(1 for x in nights if x['spike'])}")
    monthly = defaultdict(lambda: [0.0, 0.0, 0.0, 0])
    for x in nights:
        m = monthly[x["d"].strftime("%Y-%m")]
        m[0] += x["greedy"]; m[1] += x["contig"]; m[2] += x["best_n"]; m[3] += 1
    print("  month     nights   single   block   N-any   (GBP)")
    for k in sorted(monthly):
        gg, cc, bb, cnt = monthly[k]
        print(f"  {k}     {cnt:3d}   {gg/100:7.2f} {cc/100:7.2f} {bb/100:7.2f}")
    hours = defaultdict(int)
    for x in nights:
        hours[x["hour"]] += 1
    print("  cheapest-slot local hour:", dict(sorted(hours.items())))
    dec_day = sum(1 for x in nights if x["ref_day"] is not None and x["eff"] >= x["ref_day"])
    dec_17  = sum(1 for x in nights if x["ref_17"] is not None and x["eff"] >= x["ref_17"])
    print(f"  round-trip gate declines the overnight import: intended reference {dec_day}, "
          f"current-half-hour fallback at 17:00 {dec_17}, of {len(nights)}")

    # Daytime walk: dawn -> 16:00, tomorrow unpublished; candidate = cheapest remaining
    # today-slot before tomorrow's dawn; compare the gate's verdict under the reference it
    # cannot see yet, the shipped fallback (current half-hour) and today's daytime mean.
    tot = flip_cur = flip_today = 0
    d = date.fromisoformat(a.date_from)
    while d <= last:
        dawn_t = sunrise_utc(d) + timedelta(minutes=a.dawn_offset)
        dawn_n = dawn_after(d)
        ref_int   = mean([p for dt, p in slots if dawn_n <= dt < dawn_n + timedelta(hours=12)])
        ref_today = mean([p for dt, p in slots if dawn_t <= dt < dawn_t + timedelta(hours=12)])
        t   = dawn_t.replace(minute=(dawn_t.minute // 30) * 30, second=0, microsecond=0)
        end = datetime(d.year, d.month, d.day, 16, 0, tzinfo=LONDON).astimezone(timezone.utc)
        while t < end:
            cands = [(dt, p) for dt, p in slots
                     if t < dt < dawn_n and dt.astimezone(LONDON).date() == d]
            cur = by_dt.get(t)
            if cands and ref_int is not None and ref_today is not None and cur is not None:
                eff = min(p for _, p in cands) / EFFICIENCY
                v_int, v_cur, v_today = eff >= ref_int, eff >= cur, eff >= ref_today
                tot += 1; flip_cur += (v_cur != v_int); flip_today += (v_today != v_int)
            t += half
        d += timedelta(days=1)
    print(f"  daytime half-hours before publication: {tot}; verdict flips vs the intended reference: "
          f"current-half-hour fallback {flip_cur} ({100*flip_cur/max(1,tot):.1f}%), "
          f"today's-daytime-mean fallback {flip_today}")
    neg = [(dt, p) for dt, p in slots if p < 0]
    print(f"  negative half-hours: {len(neg)} over {len({dt.astimezone(LONDON).date() for dt, _ in neg})} days"
          + (f", lowest {min(p for _, p in slots):.2f}p" if slots else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
