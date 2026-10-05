#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    mend_meter_extra_slot_2026_10.py
# Description: One-off repair of daily_history.json rows settled before v5.132.1.
#              Octopus's consumption endpoint also returns the half hour that STARTS
#              at period_to, so every settled day carried the next day's 00:00-00:30
#              import and gas reading on top of its own 48. This re-reads each
#              settled day from Octopus, counts only the readings that start inside
#              the local day, and re-derives the cost fields from them. A row is only
#              changed when its stored figure equals its own day PLUS that extra
#              slot, so a row settled some other way is reported and left alone.
#              Backs up first.
# Author:      CliveS & Claude Opus 5.5
# Date:        05-10-2026 15:10
# Version:     1.0
#
# Usage:  python3 scripts/mend_meter_extra_slot_2026_10.py [--apply]
#         Without --apply it prints what it WOULD change and touches nothing.

import glob
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

LON = ZoneInfo("Europe/London")
PA = "/Library/Application Support/Perceptive Automation"
BUNDLE_ID = "com.clives.indigoplugin.sigenergy-energy-manager"
API = "https://api.octopus.energy/v1"
APPLY = "--apply" in sys.argv
TOL = 0.0015            # kWh / m3: readings are given to 3 dp


def indigo_dir():
    dirs = sorted(glob.glob(os.path.join(PA, "Indigo 20*")))
    if not dirs:
        raise SystemExit("no Indigo install folder under " + PA)
    return dirs[-1]


def secrets():
    path = os.path.join(PA, "IndigoSecrets.py")
    ns = {}
    exec(compile(open(path, encoding="utf-8").read(), path, "exec"), ns)
    return ns


def fetch(key, path, start, end):
    """Every reading under `path` between two instants -> {interval_start (UTC): value}."""
    url = f"{API}/{path}/consumption/"
    params = {"period_from": start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
              "period_to": end.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
              "page_size": 25000, "order_by": "period"}
    out = {}
    while url:
        r = requests.get(url, params=params, auth=(key, ""), timeout=60)
        r.raise_for_status()
        body = r.json()
        for i in body.get("results", []):
            st = datetime.fromisoformat(i["interval_start"].replace("Z", "+00:00"))
            out[st.astimezone(timezone.utc)] = float(i["consumption"])
        url, params = body.get("next"), None
    return out


def day_bounds(date_str):
    d = datetime.strptime(date_str, "%Y-%m-%d")
    start = datetime(d.year, d.month, d.day, tzinfo=LON)
    nxt = d + timedelta(days=1)
    return start, datetime(nxt.year, nxt.month, nxt.day, tzinfo=LON)


def own_and_extra(readings, date_str):
    """(sum of readings starting inside the day, the reading starting at its end)."""
    start, end = day_bounds(date_str)
    s, e = start.astimezone(timezone.utc), end.astimezone(timezone.utc)
    own = [v for t, v in readings.items() if s <= t < e]
    return (round(sum(own), 3) if own else None), readings.get(e), len(own)


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def main():
    ns = secrets()
    key = ns["OCTOPUS_API_KEY"]
    data_dir = os.path.join(indigo_dir(), "Preferences", "Plugins", BUNDLE_ID)
    path = os.path.join(data_dir, "daily_history.json")
    with open(path, encoding="utf-8") as fh:
        records = json.load(fh)
    settled = [r for r in records if r.get("cost_settled") and r.get("date")]
    if not settled:
        print("nothing settled - nothing to do")
        return
    first = min(r["date"] for r in settled)
    last = max(r["date"] for r in settled)
    lo = day_bounds(first)[0] - timedelta(hours=1)
    hi = day_bounds(last)[1] + timedelta(hours=2)
    imp = fetch(key, f"electricity-meter-points/{ns['OCTOPUS_MPAN']}/meters/{ns['OCTOPUS_SERIAL']}", lo, hi)
    gas = fetch(key, f"gas-meter-points/{ns['OCTOPUS_GAS_MPRN']}/meters/{ns['OCTOPUS_GAS_SERIAL']}", lo, hi)
    print(f"{len(settled)} settled days {first} to {last}; {len(imp)} import and {len(gas)} gas readings")

    changed, skipped = 0, []
    tot = {"imp": 0.0, "gas_kwh": 0.0, "bill": 0.0}
    for r in settled:
        d = r["date"]
        old_imp, old_gas_kwh, old_m3 = num(r.get("import_kwh_octo")), num(r.get("gas_kwh")), num(r.get("gas_m3"))
        imp_own, imp_x, imp_n = own_and_extra(imp, d)
        gas_own, gas_x, _ = own_and_extra(gas, d)
        if old_imp is None or imp_own is None or imp_x is None:
            skipped.append((d, "import reading missing"))
            continue
        if abs(old_imp - imp_own) <= TOL:
            continue                                     # already right
        if abs(old_imp - (imp_own + imp_x)) > TOL:
            skipped.append((d, f"import stored {old_imp} is neither {imp_own} nor {imp_own + imp_x:.3f}"))
            continue
        upd = {"import_kwh_octo": round(imp_own, 3)}
        # Electricity unit cost: recompute at the row's own rate when that is the rate
        # it was settled at; otherwise scale the stored figure.
        old_cost = num(r.get("elec_unit_cost_gbp")) or 0.0
        rate = num(r.get("rate_today_p"))
        if rate is not None and abs(round(old_imp * rate / 100.0, 2) - old_cost) <= 0.01:
            upd["elec_unit_cost_gbp"] = round(imp_own * rate / 100.0, 2)
        else:
            upd["elec_unit_cost_gbp"] = round(old_cost * imp_own / old_imp, 2) if old_imp else old_cost
        # Gas: same test on the m3 the row stored, then scale kWh and cost alike.
        if old_m3 is not None and gas_own is not None and gas_x is not None \
                and abs(old_m3 - (gas_own + gas_x)) <= TOL and old_m3 > 0:
            k = gas_own / old_m3
            upd["gas_m3"] = round(gas_own, 3)
            upd["gas_kwh"] = round(old_gas_kwh * k, 3)
            upd["gas_unit_cost_gbp"] = round((num(r.get("gas_unit_cost_gbp")) or 0.0) * k, 2)
        elif old_m3 is not None and gas_own is not None and abs(old_m3 - gas_own) > TOL:
            skipped.append((d, f"gas stored {old_m3} m3 does not match {gas_own} (+{gas_x}); import fixed, gas left"))
        # The stored bill was rounded from UNROUNDED parts, so take the unrounded
        # saving off it rather than re-adding the rounded parts (which drifts a
        # penny either way per day).
        if rate is not None:
            elec_saving = (old_imp - imp_own) * rate / 100.0
        else:
            elec_saving = old_cost * (1 - imp_own / old_imp) if old_imp else 0.0
        gas_saving = 0.0
        if "gas_m3" in upd:
            gas_saving = (num(r.get("gas_unit_cost_gbp")) or 0.0) * (1 - k)
        bill = round((num(r.get("whole_house_bill_gbp")) or 0.0) - elec_saving - gas_saving, 2)
        rev = num(r.get("export_revenue_gbp")) or 0.0
        upd.update({"whole_house_bill_gbp": bill, "wh_net_gbp": round(rev - bill, 2),
                    "covered": bool(rev >= bill)})
        tot["imp"] += old_imp - imp_own
        tot["gas_kwh"] += (old_gas_kwh or 0.0) - upd.get("gas_kwh", old_gas_kwh or 0.0)
        tot["bill"] += (num(r.get("whole_house_bill_gbp")) or 0.0) - bill
        print(f"{d}: import {old_imp} -> {upd['import_kwh_octo']}  "
              f"gas {old_gas_kwh} -> {upd.get('gas_kwh', old_gas_kwh)} kWh  "
              f"bill {r.get('whole_house_bill_gbp')} -> {bill}")
        if APPLY:
            r.update(upd)
        changed += 1

    print(f"\n{changed} days to correct: import over by {tot['imp']:.2f} kWh, gas over by "
          f"{tot['gas_kwh']:.2f} kWh, bills over by GBP {tot['bill']:.2f} in all")
    for d, why in skipped:
        print(f"LEFT ALONE {d}: {why}")
    if APPLY and changed:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        shutil.copy2(path, path + ".bak-extra-slot-" + stamp)
        fd, tmp = tempfile.mkstemp(dir=data_dir, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(records, fh, indent=2)
        os.chmod(tmp, os.stat(path).st_mode & 0o777)
        os.replace(tmp, path)
        print(f"daily_history.json written (backup .bak-extra-slot-{stamp})")
    print("done" if APPLY else "dry run complete - re-run with --apply to write")


if __name__ == "__main__":
    main()
