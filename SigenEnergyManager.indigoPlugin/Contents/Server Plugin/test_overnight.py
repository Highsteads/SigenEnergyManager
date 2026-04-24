#! /usr/bin/env python3
# -*- coding: utf-8 -*-
# Filename:    test_overnight.py
# Description: Mock test for the complete overnight sequence — dawn lookup,
#              viability, export, and import decisions across all BST/UTC
#              boundary edge cases.
# Author:      CliveS & Claude Sonnet 4.6
# Date:        24-04-2026
# Version:     1.0
#
# Run from the Server Plugin folder:
#   cd "...SigenEnergyManager.indigoPlugin/Contents/Server Plugin"
#   python3 test_overnight.py

import sys
import os
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from battery_manager import BatteryManager, ManagerSnapshot, TariffData, DawnViability

# ============================================================
# Test infrastructure
# ============================================================

_results = []

def check(name, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    _results.append((status, name, detail))
    marker = "  [OK]" if condition else "  [!!] FAIL"
    print(f"{marker}  {name}" + (f"  ({detail})" if detail else ""))
    return condition

def section(title):
    print(f"\n{'='*70}")
    print(f"  {title}")
    print(f"{'='*70}")

# ============================================================
# Shared fixtures
# ============================================================

UTC = timezone.utc

# County Durham approximate dawn times (UTC) for late April
DAWN_APR24 = datetime(2026, 4, 24, 3, 50, tzinfo=UTC)
DAWN_APR25 = datetime(2026, 4, 25, 3, 47, tzinfo=UTC)
DAWN_APR26 = datetime(2026, 4, 26, 3, 44, tzinfo=UTC)

DAWN_TIMES = {
    "2026-04-24": DAWN_APR24,
    "2026-04-25": DAWN_APR25,
    "2026-04-26": DAWN_APR26,
}

# Realistic 48-slot consumption profile (~23 kWh/day), UTC-indexed
def make_profile(daily_kwh=23.0):
    base = daily_kwh / 48.0
    profile = [round(base * 0.65, 4)] * 48      # low overnight
    for s in range(12, 16):                       # morning peak
        profile[s] = round(base * 1.5, 4)
    for s in range(16, 34):                       # daytime elevated
        profile[s] = round(base * 1.4, 4)
    for s in range(34, 42):                       # evening peak
        profile[s] = round(base * 1.8, 4)
    return profile

PROFILE_23 = make_profile(23.0)

# Minimal forecast P50 (UTC hours)
def make_p50(date_str, dawn_utc_h=4, dusk_utc_h=20):
    slots = {}
    for h in range(24):
        key = f"{date_str} {h:02d}:00:00"
        slots[key] = 1500 if dawn_utc_h <= h < dusk_utc_h else 0
    return slots

P50 = {**make_p50("2026-04-24", 4, 20), **make_p50("2026-04-25", 4, 20)}

TRACKER = TariffData(
    tariff_key      = "tracker",
    today_rate_p    = 23.55,
    tomorrow_rate_p = 23.55,
)

def snap(soc_pct, now_utc, export_enabled=True, export_active=False,
         corrected_tomorrow_kwh=74.0, profile=None, pv_watts=0):
    return ManagerSnapshot(
        current_soc_pct         = soc_pct,
        capacity_kwh            = 35.04,
        efficiency              = 0.94,
        dawn_target_pct         = 15.0,
        health_cutoff_pct       = 1.0,
        export_enabled          = export_enabled,
        max_export_kw           = 4.0,
        pv_watts                = pv_watts,
        house_load_watts        = 800,
        export_active           = export_active,
        corrected_tomorrow_kwh  = corrected_tomorrow_kwh,
        bias_factor             = 1.0,
        tariff                  = TRACKER,
        forecast_p50            = P50,
        dawn_times              = DAWN_TIMES,
        consumption_profile     = profile or PROFILE_23,
        now                     = now_utc,
        vpp_active              = False,
        vpp_reserved_kwh        = 0.0,
        solar_overflow_active   = False,
        solar_overflow_charge_cap = 0,
    )

CAP   = 35.04
FLOOR = CAP * 0.15   # dawn target 15% = 5.256 kWh

mgr = BatteryManager()


# ============================================================
# Section 1: Dawn lookup — correct future dawn always selected
# ============================================================
section("1. Dawn lookup: correct future dawn across UTC/BST boundary")

# 1.1 — 22:00 UTC Apr 23 (23:00 BST): UTC date = Apr 23, scans today(Apr23 no entry)
#        → tomorrow = Apr 24, dawn 03:50 UTC Apr 24 in future → correct (5.83h)
t = datetime(2026, 4, 23, 22, 0, tzinfo=UTC)
v = mgr._check_dawn_viability(snap(80.0, t))
ref = mgr._estimate_consumption_until(t, DAWN_APR24, PROFILE_23)
check("22:00 UTC (23:00 BST): uses Apr 24 dawn, 5.8h away",
      abs(v.expected_consumption_kwh - ref) < 0.05,
      f"drain={v.expected_consumption_kwh:.2f} ref={ref:.2f}")
check("22:00 UTC: 80% SOC safe at dawn",
      v.soc_at_dawn_kwh > FLOOR,
      f"at_dawn={v.soc_at_dawn_kwh:.2f} kWh")

# 1.2 — 23:00 UTC Apr 23 (00:00 BST): UTC date = Apr 23, no Apr 23 dawn entry,
#        → Apr 24 dawn in future (4.83h) → correct
t = datetime(2026, 4, 23, 23, 0, tzinfo=UTC)
v = mgr._check_dawn_viability(snap(70.0, t))
ref = mgr._estimate_consumption_until(t, DAWN_APR24, PROFILE_23)
check("23:00 UTC (00:00 BST): uses Apr 24 dawn, 4.8h away",
      abs(v.expected_consumption_kwh - ref) < 0.05,
      f"drain={v.expected_consumption_kwh:.2f} ref={ref:.2f}")
check("23:00 UTC: 70% SOC safe at dawn",
      v.soc_at_dawn_kwh > FLOOR,
      f"at_dawn={v.soc_at_dawn_kwh:.2f} kWh")

# 1.3 — 00:00 UTC Apr 24 (01:00 BST) — THE BUG CASE
#   OLD: scanned Apr 25 dawn (27.8h) → 29.8 kWh drain > 19 kWh battery → false alarm
#   NEW: scans today = Apr 24, dawn 03:50 UTC Apr 24 > now → uses it (3.8h, 2.4 kWh drain)
t = datetime(2026, 4, 24, 0, 0, tzinfo=UTC)
v = mgr._check_dawn_viability(snap(54.3, t))
ref_correct = mgr._estimate_consumption_until(t, DAWN_APR24, PROFILE_23)
ref_buggy   = mgr._estimate_consumption_until(t, DAWN_APR25, PROFILE_23)
check("00:00 UTC [BUG CASE]: drain matches Apr 24 dawn (3.8h), NOT Apr 25 (27.8h)",
      abs(v.expected_consumption_kwh - ref_correct) < 0.05,
      f"drain={v.expected_consumption_kwh:.2f}  "
      f"Apr24_ref={ref_correct:.2f}  Apr25_old={ref_buggy:.2f}")
check("00:00 UTC [BUG CASE]: 54% SOC correctly viable (no false import)",
      v.viable,
      f"viable={v.viable}  at_dawn={v.soc_at_dawn_kwh:.2f} kWh (floor={FLOOR:.2f})")

# 1.4 — 00:30 UTC Apr 24 (01:30 BST) — 30 min into the bug window
t = datetime(2026, 4, 24, 0, 30, tzinfo=UTC)
v = mgr._check_dawn_viability(snap(52.0, t))
check("00:30 UTC (01:30 BST): still correct, 52% SOC viable",
      v.viable,
      f"at_dawn={v.soc_at_dawn_kwh:.2f} kWh")

# 1.5 — 01:00 UTC Apr 24 (02:00 BST) — still pre-dawn, 2.83h to go
t = datetime(2026, 4, 24, 1, 0, tzinfo=UTC)
v = mgr._check_dawn_viability(snap(19.0, t))
ref = mgr._estimate_consumption_until(t, DAWN_APR24, PROFILE_23)
check("01:00 UTC (02:00 BST): 2.8h to dawn, small drain",
      ref < 2.0,
      f"drain={ref:.2f} kWh in 2.8h")
check("01:00 UTC: 19% (6.7 kWh) is above 15% floor with 1.8 kWh drain",
      v.viable,
      f"at_dawn={v.soc_at_dawn_kwh:.2f} kWh (floor={FLOOR:.2f})")

# 1.6 — 03:50 UTC Apr 24 — exact dawn moment (dawn_dt == now, NOT > now)
#        Scan: today Apr24 dawn == now → skip (not > now)
#              tomorrow Apr25 dawn → 23.95h away → use that
t = DAWN_APR24
v = mgr._check_dawn_viability(snap(16.0, t))
check("03:50 UTC (dawn exactly): falls through to Apr 25 dawn (~24h ahead)",
      v.hours_to_dawn > 20.0,
      f"hours_to_dawn={v.hours_to_dawn:.1f}h")

# 1.7 — 04:30 UTC Apr 24 (05:30 BST) — post-dawn
#        Apr 24 dawn has passed → scan Apr 24 (passed) → Apr 25 (future) → 23.3h
t = datetime(2026, 4, 24, 4, 30, tzinfo=UTC)
v = mgr._check_dawn_viability(snap(16.0, t))
ref = mgr._estimate_consumption_until(t, DAWN_APR25, PROFILE_23)
check("04:30 UTC (post-dawn): uses Apr 25 dawn, ~23h away",
      abs(v.expected_consumption_kwh - ref) < 0.5,
      f"drain={v.expected_consumption_kwh:.2f} ref={ref:.2f}")

# 1.8 — 23:00 UTC Apr 24 (00:00 BST Apr 25) — next night
#        Apr 24 dawn passed → Apr 25 dawn in future (4.78h) → correct
t = datetime(2026, 4, 24, 23, 0, tzinfo=UTC)
v = mgr._check_dawn_viability(snap(65.0, t))
ref = mgr._estimate_consumption_until(t, DAWN_APR25, PROFILE_23)
check("23:00 UTC Apr 24 (00:00 BST Apr 25): uses Apr 25 dawn (4.8h)",
      abs(v.expected_consumption_kwh - ref) < 0.05,
      f"drain={v.expected_consumption_kwh:.2f} ref={ref:.2f}")
check("23:00 UTC Apr 24: 65% SOC safe for Apr 25 dawn",
      v.viable,
      f"at_dawn={v.soc_at_dawn_kwh:.2f} kWh")


# ============================================================
# Section 2: Genuine dawn risk correctly flagged
# ============================================================
section("2. Genuine dawn risk: low battery behaviour")

# Key insight: the engine has TWO dawn-viability paths:
#   - Sunny tomorrow (tomorrow_kwh >= daily_cons): only import below health FLOOR (1% = 0.35 kWh)
#     Solar will recover any shortfall → very lenient
#   - Cloudy tomorrow (tomorrow_kwh < daily_cons): import below full dawn TARGET (15% = 5.26 kWh)
#     Battery must hold through the next solar-poor night → conservative

# 2.1 — Sunny day: 14% SOC (4.9 kWh), at_dawn ≈ 2.8 kWh > 0.35 kWh health floor
#        Engine correctly says "viable" — solar tomorrow will recharge immediately
t = datetime(2026, 4, 24, 0, 30, tzinfo=UTC)
v = mgr._check_dawn_viability(snap(14.0, t, corrected_tomorrow_kwh=74.0))
check("00:30 UTC, 14% SOC, SUNNY tomorrow (74 kWh): viable=True (solar covers shortfall)",
      v.viable,
      f"at_dawn={v.soc_at_dawn_kwh:.2f} kWh (health_floor=0.35, dawn_target=5.26)")

# 2.2 — Cloudy day: 14% SOC, tomorrow only 5 kWh → at_dawn ≈ 2.8 kWh < 5.26 floor
#        Engine must flag risk and require import
v_cloudy = mgr._check_dawn_viability(snap(14.0, t, corrected_tomorrow_kwh=5.0))
check("00:30 UTC, 14% SOC, CLOUDY tomorrow (5 kWh): not viable (at_dawn < 15% floor)",
      not v_cloudy.viable,
      f"at_dawn={v_cloudy.soc_at_dawn_kwh:.2f} kWh")
check("00:30 UTC, 14% SOC, cloudy: import_kwh_grid > 0",
      v_cloudy.import_kwh_grid > 0,
      f"import_kwh_grid={v_cloudy.import_kwh_grid:.2f} kWh")

# 2.3 — Sunny day, battery so low it breaches health floor (< 0.35 kWh at dawn)
#        e.g. 1% SOC (0.35 kWh), drain ≈ 2.4 kWh → at_dawn = -2.05 kWh < 0 → import
t_crit = datetime(2026, 4, 24, 0, 0, tzinfo=UTC)
v_crit = mgr._check_dawn_viability(snap(1.5, t_crit, corrected_tomorrow_kwh=74.0))
check("00:00 UTC, 1.5% SOC, SUNNY tomorrow: still triggers import (below health floor)",
      not v_crit.viable,
      f"at_dawn={v_crit.soc_at_dawn_kwh:.2f} kWh (health_floor=0.35)")

# 2.4 — Cloudy day, 13% SOC at 02:00 UTC (1.8h before dawn)
t = datetime(2026, 4, 24, 2, 0, tzinfo=UTC)
v = mgr._check_dawn_viability(snap(13.0, t, corrected_tomorrow_kwh=5.0))
check("02:00 UTC, 13% SOC, CLOUDY tomorrow: dawn risk correctly flagged",
      not v.viable,
      f"at_dawn={v.soc_at_dawn_kwh:.2f} kWh")


# ============================================================
# Section 3: Night export decisions
# ============================================================
section("3. Night export: correct start/stop decisions")

# 3.1 — 00:15 UTC, 65% SOC, good tomorrow → export
t = datetime(2026, 4, 24, 0, 15, tzinfo=UTC)
d = mgr.evaluate(snap(65.0, t, export_enabled=True, corrected_tomorrow_kwh=74.0))
check("00:15 UTC, 65% SOC, 74 kWh tomorrow: export starts",
      d.action == "start_export",
      f"action={d.action}  reason={d.reason[:70]}")

# 3.2 — 00:15 UTC, 65% SOC, poor tomorrow (5 kWh) → no export
d = mgr.evaluate(snap(65.0, t, export_enabled=True, corrected_tomorrow_kwh=5.0))
check("00:15 UTC, 65% SOC, poor tomorrow (5 kWh): no export",
      d.action != "start_export",
      f"action={d.action}")

# 3.3 — 00:15 UTC, 18% SOC — too low to export
d = mgr.evaluate(snap(18.0, t, export_enabled=True, export_active=True,
                       corrected_tomorrow_kwh=74.0))
check("00:15 UTC, 18% SOC: export stops / self-consumption",
      d.action in ("stop_export", "self_consumption", "start_import"),
      f"action={d.action}")

# 3.4 — THE BUG MOMENT: 00:00 UTC, 54.3% SOC, good tomorrow → must export (NOT import)
t = datetime(2026, 4, 24, 0, 0, tzinfo=UTC)
d = mgr.evaluate(snap(54.3, t, export_enabled=True, export_active=True,
                       corrected_tomorrow_kwh=74.0))
check("[BUG MOMENT] 00:00 UTC (01:00 BST), 54% SOC: continues export, NOT import",
      d.action != "start_import",
      f"action={d.action}  reason={d.reason[:70]}")

# 3.5 — 00:30 UTC (01:30 BST), 54% SOC, export started → also not import
t = datetime(2026, 4, 24, 0, 30, tzinfo=UTC)
d = mgr.evaluate(snap(54.3, t, export_enabled=True, corrected_tomorrow_kwh=74.0))
check("00:30 UTC (01:30 BST), 54% SOC: no import triggered",
      d.action != "start_import",
      f"action={d.action}")


# ============================================================
# Section 4: Import precision — correct timing and quantities
# ============================================================
section("4. Import decisions: right amount, right time")

# 4.1 — 02:00 UTC, 13% SOC, CLOUDY tomorrow → real import needed
#        (sunny tomorrow: engine defers to solar, no import at 13%)
t = datetime(2026, 4, 24, 2, 0, tzinfo=UTC)
d = mgr.evaluate(snap(13.0, t, export_enabled=True, corrected_tomorrow_kwh=5.0))
check("02:00 UTC, 13% SOC, CLOUDY tomorrow: genuine import triggered",
      d.action == "start_import",
      f"action={d.action}  reason={d.reason[:70]}")

# 4.2 — 02:00 UTC, 54% SOC → no import needed (18.9 kWh, only 1.1 kWh drain)
d = mgr.evaluate(snap(54.0, t, export_enabled=True))
check("02:00 UTC, 54% SOC: no import (18.9 kWh >> 1.1 kWh drain)",
      d.action != "start_import",
      f"action={d.action}")

# 4.3 — 00:00 UTC (01:00 BST), 54.3% → fixed, no false import
t = datetime(2026, 4, 24, 0, 0, tzinfo=UTC)
d = mgr.evaluate(snap(54.3, t, export_enabled=True))
check("00:00 UTC (01:00 BST), 54.3% [FIXED]: no false import",
      d.action != "start_import",
      f"action={d.action}  reason={d.reason[:70]}")


# ============================================================
# Section 5: _estimate_consumption_until accuracy
# ============================================================
section("5. Consumption estimator: slot arithmetic")

t0 = datetime(2026, 4, 24, 0, 0, tzinfo=UTC)
flat = [0.5] * 48   # 24 kWh/day flat

# 5.1 — Full 24h with flat profile
drain = mgr._estimate_consumption_until(t0, t0 + timedelta(hours=24), flat)
check("Flat 0.5/slot: 24h → 24.0 kWh",
      abs(drain - 24.0) < 0.05,
      f"drain={drain:.3f}")

# 5.2 — Exactly one 30-min slot
t_slot = datetime(2026, 4, 24, 6, 0, tzinfo=UTC)
drain = mgr._estimate_consumption_until(t_slot, t_slot + timedelta(minutes=30), flat)
check("Flat 0.5/slot: 30 min → 0.5 kWh",
      abs(drain - 0.5) < 0.01,
      f"drain={drain:.4f}")

# 5.3 — Half a slot (15 min)
drain = mgr._estimate_consumption_until(t_slot, t_slot + timedelta(minutes=15), flat)
check("Flat 0.5/slot: 15 min → 0.25 kWh",
      abs(drain - 0.25) < 0.01,
      f"drain={drain:.4f}")

# 5.4 — Zero-length window
drain = mgr._estimate_consumption_until(t_slot, t_slot, flat)
check("Zero-length window → 0.0 kWh",
      drain == 0.0,
      f"drain={drain}")

# 5.5 — Empty profile fallback: 0.45 kWh/hr
drain = mgr._estimate_consumption_until(t_slot, t_slot + timedelta(hours=4), [])
check("Empty profile fallback: 4h × 0.45 kWh/hr = 1.8 kWh",
      abs(drain - 1.8) < 0.05,
      f"drain={drain:.3f}")

# 5.6 — Cross-midnight overnight span (23:00 UTC → 03:47 UTC = 4.78h)
#        All slots are overnight-low (~0.31 kWh/slot × 9.5 slots ≈ 2.95 kWh)
#        NOT the day-average (23/24 × 4.78h = 4.58 kWh)
t_night = datetime(2026, 4, 24, 23, 0, tzinfo=UTC)
drain   = mgr._estimate_consumption_until(t_night, DAWN_APR25, PROFILE_23)
# Overnight slots (23:00-03:47 UTC) are all low-consumption slots
overnight_slot_kwh = PROFILE_23[0]   # midnight slot = lowest tier
hours = (DAWN_APR25 - t_night).total_seconds() / 3600.0
lower = hours * (overnight_slot_kwh / 0.5)   # lower bound at overnight rate
upper = hours * (23.0 / 24.0) * 1.2           # generous upper bound
check(f"Cross-midnight 23:00→03:47 ({hours:.1f}h): drain is overnight-low (not day-average)",
      lower * 0.8 <= drain <= upper,
      f"drain={drain:.2f}  overnight_rate×hours={lower:.2f}  day_avg_upper={upper:.2f}")

# 5.7 — 27h span (old bug span): covers a full day+3.8h so > full daily profile
#        Expected: roughly 1 full day (23 kWh) + overnight prefix ≈ 27-32 kWh
t_bug  = datetime(2026, 4, 24, 0, 0, tzinfo=UTC)
drain  = mgr._estimate_consumption_until(t_bug, DAWN_APR25, PROFILE_23)
daily  = sum(PROFILE_23)   # full 24h profile
check("27h span: drain > one full day's profile (full day + 3.8h overnight)",
      daily <= drain <= daily * 1.3,
      f"drain={drain:.2f}  daily_profile={daily:.2f}")


# ============================================================
# Section 6: BST boundary minute sweep — zero false triggers
# ============================================================
section("6. BST boundary sweep: every minute 23:30 UTC Apr23 → 01:30 UTC Apr24")

def sweep_sunny(soc):
    """Sweep with sunny tomorrow — no import expected unless below health floor."""
    false_imports = []
    t = datetime(2026, 4, 23, 23, 30, tzinfo=UTC)
    end = datetime(2026, 4, 24, 1, 30, tzinfo=UTC)
    while t <= end:
        d = mgr.evaluate(snap(soc, t, export_enabled=True, corrected_tomorrow_kwh=74.0))
        if d.action == "start_import":
            false_imports.append(t.strftime("%H:%M UTC"))
        t += timedelta(minutes=1)
    return false_imports

def sweep_cloudy(soc):
    """Sweep with cloudy tomorrow — imports expected when below dawn target."""
    imports = []
    t = datetime(2026, 4, 23, 23, 30, tzinfo=UTC)
    end = datetime(2026, 4, 24, 1, 30, tzinfo=UTC)
    while t <= end:
        d = mgr.evaluate(snap(soc, t, export_enabled=True, corrected_tomorrow_kwh=5.0))
        if d.action == "start_import":
            imports.append(t.strftime("%H:%M UTC"))
        t += timedelta(minutes=1)
    return imports

# --- Sunny tomorrow: no false imports even at moderate/low SOC ---
fi60 = sweep_sunny(60.0)
check("SUNNY, 60% SOC: zero false imports across 3h BST boundary",
      len(fi60) == 0,
      f"{len(fi60)} false trigger(s): {fi60[:5]}")

fi50 = sweep_sunny(50.0)
check("SUNNY, 50% SOC: zero false imports",
      len(fi50) == 0,
      f"{len(fi50)} false trigger(s): {fi50[:5]}")

fi40 = sweep_sunny(40.0)
check("SUNNY, 40% SOC: zero false imports",
      len(fi40) == 0,
      f"{len(fi40)} false trigger(s): {fi40[:5]}")

fi20_sunny = sweep_sunny(20.0)
# Sunny day: 20% = 7.0 kWh, drain ~2.4 kWh, at_dawn ~4.6 kWh > 0.35 kWh health floor
# → engine correctly says "viable" (solar will recharge)
check("SUNNY, 20% SOC: no import (engine trusts solar will cover 4.6 kWh shortfall)",
      len(fi20_sunny) == 0,
      f"{len(fi20_sunny)} import(s) — expected 0 (sunny-day leniency)")

# --- Cloudy tomorrow: imports fire correctly when below 15% dawn target ---
fi20_cloudy = sweep_cloudy(20.0)
# Cloudy day: 20% = 7.0 kWh, drain ~2-3 kWh, at_dawn ~4-5 kWh < 5.26 kWh target
# → engine correctly imports to cover dawn
check("CLOUDY, 20% SOC: imports DO fire (at_dawn < 15% floor, no solar to rescue)",
      len(fi20_cloudy) > 0,
      f"{len(fi20_cloudy)} import decisions (expected > 0)")
if fi20_cloudy:
    print(f"         CLOUDY 20%: imports from {fi20_cloudy[0]} to {fi20_cloudy[-1]}")

fi30_cloudy = sweep_cloudy(30.0)
batt_30 = 30.0 / 100.0 * CAP
drain_30 = mgr._estimate_consumption_until(
    datetime(2026, 4, 24, 0, 0, tzinfo=UTC), DAWN_APR24, PROFILE_23
)
print(f"         CLOUDY 30% ({batt_30:.1f} kWh): drain@01:00BST≈{drain_30:.2f} kWh, "
      f"at_dawn≈{batt_30-drain_30:.2f} kWh — "
      f"{'import fires' if fi30_cloudy else 'self-consumption (above 15% floor)'}")


# ============================================================
# Summary
# ============================================================
section("Summary")
passed = sum(1 for s, _, _ in _results if s == "PASS")
failed = sum(1 for s, _, _ in _results if s == "FAIL")
total  = passed + failed
print(f"\n  {passed}/{total} tests passed", end="")
if failed:
    print(f"  ({failed} FAILED)\n")
    print("  Failed tests:")
    for s, name, detail in _results:
        if s == "FAIL":
            print(f"    [!!] {name}")
            if detail:
                print(f"         {detail}")
else:
    print("  - all OK\n")
sys.exit(0 if failed == 0 else 1)
