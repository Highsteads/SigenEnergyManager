#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    test_battery_manager.py
# Description: Unit tests for battery_manager.py decision engine
#              Runs without Indigo installed
# Author:      CliveS & Claude Sonnet 4.6
# Date:        03-05-2026
# Version:     1.3

import unittest
from datetime import datetime, timedelta, timezone

# Allow running without Indigo (battery_manager imports octopus_api constants via try/except)
from battery_manager import (
    BatteryManager,
    ManagerSnapshot,
    TariffData,
    ACTION_SELF_CONSUMPTION,
    ACTION_START_IMPORT,
    ACTION_SCHEDULE_IMPORT,
    ACTION_START_EXPORT,
    ACTION_STOP_EXPORT,
    ACTION_VPP_EXPORT,
    ACTION_SAVING_SESSION,
    ACTION_HAPPY_HOUR_IMPORT,
    saving_session_exportable_kwh,
    happy_hour_import_kwh,
    TARIFF_TRACKER,
    TARIFF_GO,
    FLOOD_PREV_TARGET_PCT,
    SufficiencyBalance,
    SOLAR_OVERFLOW_TARGET_SOC_PCT,
    SOLAR_OVERFLOW_MIN_END_SOC_PCT,
    SOLAR_OVERFLOW_ENGAGE_KWH,
    SOLAR_OVERFLOW_RELEASE_KWH,
    SOLAR_OVERFLOW_MIN_DWELL_MIN,
    SOLAR_OVERFLOW_BANK_FIRST_MAX_KWH,
    SOLAR_OVERFLOW_BANK_FIRST_SOC_PCT,
    SOLAR_OVERFLOW_BANK_FIRST_SOC_MAX,
    SOLAR_OVERFLOW_BANK_FIRST_KWH_MAX,
    MIN_EXPORT_KWH,
)


# ============================================================
# Helpers
# ============================================================

CAPACITY_KWH = 35.04
EFFICIENCY   = 0.94
DAWN_TARGET  = 10.0   # %
HEALTH_FLOOR = 10.0   # %

def _now(hour=14, minute=0):
    """Return a UTC datetime for today at a given hour."""
    d = datetime.now(timezone.utc).replace(
        hour=hour, minute=minute, second=0, microsecond=0
    )
    return d

def _tomorrow_dawn(hour=7):
    """Return UTC datetime for tomorrow's dawn."""
    return _now(0) + timedelta(days=1, hours=hour)

def _today_str():
    """Return today's date string (local BST/GMT) matching battery_manager's today_str."""
    # Must resolve the SAME zone battery_manager does, or this helper and the
    # module disagree about which day it is either side of local midnight during
    # BST — a test that passes all day and fails for one hour a night. stdlib
    # zoneinfo, so there is no pytz-missing branch to drift.
    from zoneinfo import ZoneInfo
    return datetime.now(timezone.utc).astimezone(
        ZoneInfo("Europe/London")).date().strftime("%Y-%m-%d")

def _make_sunny_p50(dusk_hour=19, peak_wh=10000):
    """Minimal P50 for a sunny day: peak_wh per hour from 07:00 to dusk_hour (local).

    battery_manager uses local (BST/GMT) date strings for P50 keys.
    Used to make is_daytime=True in tests that need daytime balance state.
    """
    today = _today_str()
    return {f"{today} {h:02d}:00:00": peak_wh for h in range(7, dusk_hour + 1)}

def _make_snapshot(
    soc_pct=50.0,
    tariff_key=TARIFF_TRACKER,
    today_rate_p=25.0,
    tomorrow_rate_p=None,
    cheap_start=None,
    cheap_end=None,
    export_enabled=False,
    vpp_active=False,
    now_hour=14,
    forecast_p50=None,
    consumption_profile=None,
    dawn_times=None,
    pv_watts=0,
    export_active=False,
    max_export_kw=4.0,
    corrected_today_kwh=0.0,
    corrected_tomorrow_kwh=0.0,
    vpp_today_kwh=0.0,
    vpp_tomorrow_kwh=0.0,
    flood_prev_target_soc=0.0,
    dawn_target_pct=DAWN_TARGET,
    weekday_kwh=22.0,
    weekend_kwh=30.0,
    bias_factor=1.0,
    bias_factor_today=1.0,
    saving_session_active=False,
    saving_session_hours=1.0,
    happy_hour_active=False,
    happy_hour_hours=1.0,
    inverter_max_kw=10.0,
):
    """Build a ManagerSnapshot for testing."""
    tomorrow_str = (datetime.now(timezone.utc).date() + timedelta(days=1)).strftime("%Y-%m-%d")

    tariff = TariffData(
        tariff_key      = tariff_key,
        today_rate_p    = today_rate_p,
        tomorrow_rate_p = tomorrow_rate_p,
        cheap_start     = cheap_start,
        cheap_end       = cheap_end,
    )

    # Default: flat 0.3 kWh/slot profile (~14.4 kWh/day)
    if consumption_profile is None:
        consumption_profile = [0.30] * 48

    # Default dawn times: tomorrow at 07:00
    if dawn_times is None:
        dawn_times = {tomorrow_str: _tomorrow_dawn(hour=7)}

    return ManagerSnapshot(
        bias_factor            = bias_factor,
        bias_factor_today      = bias_factor_today,
        current_soc_pct        = soc_pct,
        capacity_kwh           = CAPACITY_KWH,
        efficiency             = EFFICIENCY,
        dawn_target_pct        = dawn_target_pct,
        health_cutoff_pct      = HEALTH_FLOOR,
        export_enabled         = export_enabled,
        max_export_kw          = max_export_kw,
        pv_watts               = pv_watts,
        export_active          = export_active,
        corrected_today_kwh    = corrected_today_kwh,
        corrected_tomorrow_kwh = corrected_tomorrow_kwh,
        vpp_today_kwh          = vpp_today_kwh,
        vpp_tomorrow_kwh       = vpp_tomorrow_kwh,
        flood_prev_target_soc  = flood_prev_target_soc,
        tariff                 = tariff,
        forecast_p50           = forecast_p50 or {},
        dawn_times             = dawn_times,
        consumption_profile    = consumption_profile,
        now                    = _now(hour=now_hour),
        vpp_active             = vpp_active,
        weekday_kwh            = weekday_kwh,
        weekend_kwh            = weekend_kwh,
        saving_session_active  = saving_session_active,
        saving_session_hours   = saving_session_hours,
        happy_hour_active      = happy_hour_active,
        happy_hour_hours       = happy_hour_hours,
        inverter_max_kw        = inverter_max_kw,
    )


# ============================================================
# Test cases
# ============================================================

class TestSufficiencyBalance(unittest.TestCase):
    """Tests for 24-hour sufficiency balance (v4.0 — replaces DawnViability).

    Checks _calculate_24h_balance() results and their effect on import decisions.
    Default profile: [0.30]*48 = 14.4 kWh/day.  Default dawn_target_pct=10% (3.504 kWh).
    """

    def setUp(self):
        self.bm = BatteryManager()

    def test_high_soc_produces_correct_dawn_projection(self):
        """Battery at 80% overnight: plenty of kWh at dawn, no import flagged.

        80% * 35.04 = 28.03 kWh.  Drain from 20:00 to 07:00 (11h) = 6.6 kWh.
        battery_at_dawn = 21.43 kWh >> 3.504 kWh target.
        With good solar tomorrow (30 kWh) no import is needed.
        """
        snapshot = _make_snapshot(
            soc_pct                = 80.0,
            now_hour               = 20,
            corrected_tomorrow_kwh = 30.0,    # plenty of solar tomorrow
        )
        balance = self.bm._calculate_24h_balance(snapshot)

        self.assertGreater(balance.battery_at_dawn_kwh, 3.504)
        self.assertFalse(balance.import_needed)

    def test_low_soc_with_no_solar_tomorrow_flags_import(self):
        """Battery at 15%, no solar tomorrow: combined shortfall flags import.

        15% = 5.26 kWh. After overnight drain (6.6 kWh) battery is at floor (3.504 kWh).
        With 0 kWh solar tomorrow and 14.4 kWh daily need: import_needed=True.
        """
        snapshot = _make_snapshot(
            soc_pct                = 15.0,
            now_hour               = 20,
            corrected_tomorrow_kwh = 0.0,
        )
        balance = self.bm._calculate_24h_balance(snapshot)

        self.assertTrue(balance.import_needed)
        self.assertGreater(balance.import_kwh, 0.0)

    def test_tomorrow_solar_eliminates_import_need(self):
        """Sufficient solar tomorrow: even low battery doesn't need import.

        battery_at_dawn = 3.504 kWh (clamped at floor).
        tomorrow_need = 22 kWh weekday / 30 kWh weekend.
        Use 40 kWh solar forecast so available_tomorrow (3.504 + 40 = 43.5) always
        exceeds daily need regardless of day of week.
        """
        snapshot = _make_snapshot(
            soc_pct                = 15.0,
            now_hour               = 20,
            corrected_tomorrow_kwh = 40.0,   # exceeds max daily need (30 kWh weekend)
        )
        balance = self.bm._calculate_24h_balance(snapshot)

        self.assertFalse(balance.import_needed)

    def test_hours_to_dawn_is_long_from_evening(self):
        """At 20:00, hours to tomorrow dawn (~07:00) should be ~11 hours."""
        snapshot = _make_snapshot(soc_pct=50.0, now_hour=20)
        balance  = self.bm._calculate_24h_balance(snapshot)

        # Dawn defaults to tomorrow 07:00 UTC; 20:00 UTC to 07:00 = 11h
        self.assertGreater(balance.hours_to_dawn, 9.0)
        self.assertLess(balance.hours_to_dawn, 14.0)


class TestTrackerImportDecisions(unittest.TestCase):
    """Tests for Tracker flat-rate import logic (v4.0).

    KEY RULE: On Tracker (flat-rate), do NOT pre-charge battery.
    When battery is low the inverter imports direct to house with ZERO conversion
    loss at the same price — pre-charging wastes ~6% for no benefit.

    The ONLY reason to import on Tracker is:
      1. Tomorrow's rate is ≥10% cheaper → defer to 00:05 for rate saving
      2. SOC below resilience floor (dawn_target_pct=10%) at night
    Otherwise: SELF_CONSUMPTION with grid passthrough.
    """

    def setUp(self):
        self.bm = BatteryManager()

    def test_tracker_flat_rate_does_not_precharge_when_tomorrow_rate_unknown(self):
        """Tracker: unknown tomorrow rate → same rate assumed → self-consumption (grid passthrough).

        v4.0: no pre-charging at flat rate. Grid imports direct to house with zero
        conversion loss — battery pre-charge wastes ~6% at the same price.
        """
        snapshot = _make_snapshot(
            soc_pct         = 12.0,
            tariff_key      = TARIFF_TRACKER,
            today_rate_p    = 25.0,
            tomorrow_rate_p = None,   # not published yet — same rate assumed
            now_hour        = 20,
        )
        decision = self.bm.evaluate(snapshot)

        # Flat-rate: let inverter passthrough, don't pre-charge battery
        self.assertEqual(decision.action, ACTION_SELF_CONSUMPTION)

    def test_tracker_flat_rate_does_not_precharge_at_same_rate(self):
        """Tracker: similar tomorrow rate → grid passthrough → self-consumption."""
        snapshot = _make_snapshot(
            soc_pct         = 12.0,
            tariff_key      = TARIFF_TRACKER,
            today_rate_p    = 25.0,
            tomorrow_rate_p = 24.5,   # only 2% cheaper — below TRACKER_DEFER_THRESHOLD
            now_hour        = 20,
        )
        decision = self.bm.evaluate(snapshot)

        self.assertEqual(decision.action, ACTION_SELF_CONSUMPTION)

    def test_defer_import_when_tomorrow_significantly_cheaper(self):
        """When tomorrow is ≥10% cheaper AND battery has margin, defer to 00:05.

        Today: 28p, tomorrow: 20p (28.6% cheaper → above TRACKER_DEFER_THRESHOLD=10%).
        Battery at 25% (8.76 kWh). Drain to midnight (4h * 0.6 = 2.4 kWh).
        SOC at midnight: 8.76 - 2.4 = 6.36 kWh >> 3.504 health floor → can defer.

        Schedule must be Europe/London midnight (00:05 local) — represented as
        either UTC 00:05 (winter/GMT) or UTC 23:05 (summer/BST). The defer is
        anchored to the local-time tariff boundary, not UTC midnight.
        """
        snapshot = _make_snapshot(
            soc_pct         = 25.0,
            tariff_key      = TARIFF_TRACKER,
            today_rate_p    = 28.0,
            tomorrow_rate_p = 20.0,   # 28.6% cheaper
            now_hour        = 20,
        )
        decision = self.bm.evaluate(snapshot)

        self.assertEqual(decision.action, ACTION_SCHEDULE_IMPORT)
        self.assertIsNotNone(decision.scheduled_time)
        # Midnight Europe/London converted to UTC == 00 (GMT) or 23 (BST).
        # Both are valid; the +5 minutes makes it hour 0 (GMT) or hour 23 (BST).
        self.assertIn(decision.scheduled_time.hour, (0, 23))
        self.assertEqual(decision.scheduled_time.minute, 5)

    def test_tracker_cannot_defer_when_soc_too_low_to_reach_midnight(self):
        """When battery cannot safely reach midnight, fall back to grid passthrough.

        Today: 28p, tomorrow: 20p (much cheaper) but battery at 15% (5.26 kWh).
        Drain to midnight (4h * 0.6 = 2.4 kWh). SOC at midnight: 5.26 - 2.4 = 2.86 kWh
        < 3.504 health floor → cannot safely defer.
        Falls back to grid passthrough (SELF_CONSUMPTION) — no pre-charge.
        """
        snapshot = _make_snapshot(
            soc_pct         = 15.0,
            tariff_key      = TARIFF_TRACKER,
            today_rate_p    = 28.0,
            tomorrow_rate_p = 20.0,   # cheaper, but can't safely wait
            now_hour        = 20,
        )
        decision = self.bm.evaluate(snapshot)

        # Cannot defer AND flat-rate: grid passthrough, no battery pre-charge
        self.assertEqual(decision.action, ACTION_SELF_CONSUMPTION)

    def test_tracker_defers_import_when_tomorrow_cheap_and_good_soc(self):
        """High SOC + much cheaper tomorrow → schedule import at 00:05.

        today=25p, tomorrow=20p (20% cheaper). Battery at 70% (24.5 kWh).
        Drain to midnight = 2.4 kWh. SOC at midnight = 22.1 kWh >> floor → defer.
        """
        snapshot = _make_snapshot(
            soc_pct         = 70.0,
            tariff_key      = TARIFF_TRACKER,
            today_rate_p    = 25.0,
            tomorrow_rate_p = 20.0,   # 20% cheaper → above threshold
            now_hour        = 20,
        )
        decision = self.bm.evaluate(snapshot)

        # Tomorrow cheaper and can reach midnight → defer
        self.assertEqual(decision.action, ACTION_SCHEDULE_IMPORT)

    def test_tracker_self_consumption_when_tomorrow_solar_covers_load(self):
        """No import needed when tomorrow solar comfortably covers load.

        Good solar tomorrow (30 kWh >> 14.4 kWh need): import_needed=False → self-consumption.
        """
        snapshot = _make_snapshot(
            soc_pct                = 50.0,
            tariff_key             = TARIFF_TRACKER,
            today_rate_p           = 25.0,
            tomorrow_rate_p        = 25.0,
            now_hour               = 20,
            corrected_tomorrow_kwh = 30.0,   # abundant solar tomorrow
        )
        decision = self.bm.evaluate(snapshot)

        self.assertEqual(decision.action, ACTION_SELF_CONSUMPTION)


class TestGoFluxImportDecisions(unittest.TestCase):
    """Tests for Go/Flux time-of-use tariff import logic."""

    def setUp(self):
        self.bm = BatteryManager()

    def test_go_defer_to_cheap_window_if_margin_allows(self):
        """On Go tariff at 20:00 with adequate SOC, defer import to 00:30 cheap window.

        30% SOC (10.51 kWh). Default corrected_tomorrow_kwh=0 → import needed for tomorrow.
        Drain to 00:30 = 4.5h * 0.6 = 2.7 kWh. SOC at 00:30 = 7.81 kWh > 3.504 floor.
        Battery can safely reach cheap window → defer (SCHEDULE_IMPORT to 00:30).
        """
        snapshot = _make_snapshot(
            soc_pct         = 30.0,
            tariff_key      = TARIFF_GO,
            today_rate_p    = 25.0,
            cheap_start     = "00:30",
            cheap_end       = "05:30",
            now_hour        = 20,
        )
        decision = self.bm.evaluate(snapshot)

        # Go tariff: defer to cheap window since battery can safely reach it
        self.assertEqual(decision.action, ACTION_SCHEDULE_IMPORT)
        self.assertIsNotNone(decision.scheduled_time)
        # Cheap window start is 00:30 LOCAL (Europe/London). Assert in local time
        # so the test is correct in both BST and GMT — the old UTC `.hour == 0`
        # check silently encoded the BST-offset bug (00:30 local is 23:30 UTC in
        # summer, not 00:00 UTC).
        # zoneinfo, not pytz: stdlib, so this test asserts the contract on any
        # supported Python instead of erroring out where pytz is absent.
        from zoneinfo import ZoneInfo
        local_sched = decision.scheduled_time.astimezone(ZoneInfo("Europe/London"))
        self.assertEqual((local_sched.hour, local_sched.minute), (0, 30))

    def test_go_import_now_if_margin_too_low_for_cheap_window(self):
        """On Go tariff, import immediately if battery cannot reach cheap window."""
        # 12% SOC = 4.2 kWh, drain to 00:30 = 4.5h * 0.6 = 2.7 kWh
        # SOC at 00:30 = 4.2 - 2.7 = 1.5 kWh < 3.504 floor -> must import now
        snapshot = _make_snapshot(
            soc_pct         = 12.0,
            tariff_key      = TARIFF_GO,
            today_rate_p    = 25.0,
            cheap_start     = "00:30",
            cheap_end       = "05:30",
            now_hour        = 20,
        )
        decision = self.bm.evaluate(snapshot)

        # Cannot wait (SOC at window start below the health floor) -> must
        # import NOW. Pinned to START_IMPORT — the old either-of-two assertion
        # would have let a regression that re-defers an unreachable-window
        # import pass silently.
        self.assertEqual(decision.action, ACTION_START_IMPORT)

    def test_import_during_cheap_window(self):
        """When in cheap window and import needed, import immediately."""
        # At 01:00 (inside Go cheap window 00:30-05:30)
        snapshot = _make_snapshot(
            soc_pct         = 12.0,
            tariff_key      = TARIFF_GO,
            today_rate_p    = 25.0,
            cheap_start     = "00:30",
            cheap_end       = "05:30",
            now_hour        = 1,
        )
        decision = self.bm.evaluate(snapshot)
        self.assertEqual(decision.action, ACTION_START_IMPORT)
        self.assertIn("cheap window", decision.reason.lower())


class TestTouWindowLocalTime(unittest.TestCase):
    """Regression: TOU cheap-window detection must use the LOCAL (Europe/London)
    clock, not the UTC clock. snapshot.now is UTC; cheap_start/cheap_end are local
    HH:MM. In BST (UTC+1) the old code was an hour off — importing outside the
    cheap window or scheduling at the wrong instant.
    """

    def test_next_window_start_returns_local_0030_in_bst(self):
        from zoneinfo import ZoneInfo
        london = ZoneInfo("Europe/London")
        now    = datetime(2026, 6, 15, 20, 0, tzinfo=timezone.utc)   # summer → BST
        result = BatteryManager._next_window_start(now, "00:30")
        # 00:30 LOCAL on 16 Jun == 23:30 UTC on 15 Jun (BST = UTC+1)
        self.assertEqual(
            result.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M"),
            "2026-06-15 23:30",
        )
        self.assertEqual(result.astimezone(london).strftime("%H:%M"), "00:30")

    def test_next_window_start_returns_0030_utc_in_gmt(self):
        now    = datetime(2026, 12, 15, 20, 0, tzinfo=timezone.utc)  # winter → GMT
        result = BatteryManager._next_window_start(now, "00:30")
        # 00:30 LOCAL == 00:30 UTC in GMT
        self.assertEqual(result.astimezone(timezone.utc).strftime("%H:%M"), "00:30")

    def test_in_window_check_uses_local_clock(self):
        # 22:45 UTC in summer == 23:45 BST, which IS inside an iGo 23:30–05:30 window.
        bst_2245 = datetime(2026, 6, 15, 22, 45, tzinfo=timezone.utc)
        local_hm = BatteryManager._to_local(bst_2245).strftime("%H:%M")
        self.assertEqual(local_hm, "23:45")
        self.assertTrue(BatteryManager._time_in_window(local_hm, "23:30", "05:30"))
        # The old UTC clock would have read 22:45 and judged it OUTSIDE the window.
        self.assertFalse(
            BatteryManager._time_in_window(bst_2245.strftime("%H:%M"), "23:30", "05:30")
        )


class TestVppSuppression(unittest.TestCase):
    """Tests that VPP active suppresses all manager decisions."""

    def setUp(self):
        self.bm = BatteryManager()

    def test_vpp_active_self_drives_export(self):
        """When VPP is active (and export enabled), self-drive the export regardless of SOC.

        v3.6: the override no longer stands down for Axle (their dispatch is
        unreliable and settlement is meter-based) — it drives the export itself.
        """
        snapshot = _make_snapshot(
            soc_pct        = 5.0,   # would normally trigger import
            vpp_active     = True,
            export_enabled = True,  # normal VPP window — export permitted
            now_hour       = 20,
        )
        decision = self.bm.evaluate(snapshot)

        self.assertEqual(decision.action, ACTION_VPP_EXPORT)
        self.assertIn("VPP", decision.reason)

    def test_vpp_export_takes_priority_over_import(self):
        """VPP export override wins even when SOC is low enough to want an import."""
        snapshot = _make_snapshot(
            soc_pct        = 5.0,
            vpp_active     = True,
            export_enabled = True,
            now_hour       = 8,
        )
        decision = self.bm.evaluate(snapshot)
        self.assertEqual(decision.action, ACTION_VPP_EXPORT)

    def test_vpp_stands_down_when_export_locked_out(self):
        """A power-cut lockout / storm forces export_enabled False — the VPP override
        must stand down (safety beats the ~£1/kWh payment), not self-drive export."""
        snapshot = _make_snapshot(
            soc_pct        = 50.0,
            vpp_active     = True,
            export_enabled = False,   # post-cut lockout (or storm) in force
            now_hour       = 20,
        )
        decision = self.bm.evaluate(snapshot)
        self.assertEqual(decision.action, ACTION_SELF_CONSUMPTION)
        self.assertIn("export currently disabled", decision.reason)


class TestConsumptionEstimation(unittest.TestCase):
    """Tests for the consumption estimation helper."""

    def setUp(self):
        self.bm = BatteryManager()

    def test_flat_profile_estimation(self):
        """Flat profile: consumption = hours * slot_kwh."""
        now_dt    = _now(hour=20, minute=0)
        target_dt = _now(hour=22, minute=0)
        profile   = [0.30] * 48  # 0.30 kWh per 30-min slot

        result = self.bm._estimate_consumption_until(now_dt, target_dt, profile)

        # 2 hours = 4 slots * 0.30 = 1.2 kWh
        self.assertAlmostEqual(result, 1.2, places=1)

    def test_zero_duration(self):
        """Zero duration returns zero consumption."""
        now_dt = _now(hour=20)
        result = self.bm._estimate_consumption_until(now_dt, now_dt, [0.30] * 48)
        self.assertEqual(result, 0.0)

    def test_empty_profile_falls_back_to_default(self):
        """Empty profile uses 0.45 kWh/hour default."""
        now_dt    = _now(hour=20)
        target_dt = _now(hour=22)
        result    = self.bm._estimate_consumption_until(now_dt, target_dt, [])

        # 2 hours * 0.45 = 0.9 kWh
        self.assertAlmostEqual(result, 0.9, places=1)

    def test_profile_indexed_by_local_time_in_bst(self):
        """The 48-slot profile is indexed by LOCAL time, not UTC. In BST (UTC+1) a
        UTC 08:00-08:30 window is local 09:00-09:30 → profile slot 18, not slot 16."""
        profile    = [float(i) for i in range(48)]   # slot i contributes i kWh
        now_utc    = datetime(2026, 6, 15, 8, 0, tzinfo=timezone.utc)   # = 09:00 BST
        target_utc = now_utc + timedelta(minutes=30)
        result = self.bm._estimate_consumption_until(now_utc, target_utc, profile)
        self.assertEqual(result, 18.0)   # local slot 18 (09:00), not UTC slot 16 (08:00)

    def test_profile_indexed_by_utc_in_winter(self):
        """In GMT (winter) local == UTC, so a UTC 08:00 window maps to slot 16."""
        profile    = [float(i) for i in range(48)]
        now_utc    = datetime(2026, 12, 15, 8, 0, tzinfo=timezone.utc)   # = 08:00 GMT
        target_utc = now_utc + timedelta(minutes=30)
        result = self.bm._estimate_consumption_until(now_utc, target_utc, profile)
        self.assertEqual(result, 16.0)


class TestTimeWindowHelper(unittest.TestCase):
    """Tests for the time window helper."""

    def setUp(self):
        self.bm = BatteryManager()

    def test_time_within_window(self):
        self.assertTrue(self.bm._time_in_window("02:30", "00:30", "05:30"))

    def test_time_outside_window(self):
        self.assertFalse(self.bm._time_in_window("10:00", "00:30", "05:30"))

    def test_time_at_window_start(self):
        self.assertTrue(self.bm._time_in_window("00:30", "00:30", "05:30"))

    def test_time_at_window_end_excluded(self):
        self.assertFalse(self.bm._time_in_window("05:30", "00:30", "05:30"))

    def test_overnight_window_within(self):
        """Overnight window (e.g. 23:30-05:30): midnight should be inside."""
        self.assertTrue(self.bm._time_in_window("00:15", "23:30", "05:30"))

    def test_overnight_window_outside(self):
        """Overnight window: midday should be outside."""
        self.assertFalse(self.bm._time_in_window("12:00", "23:30", "05:30"))



class TestNightExport(unittest.TestCase):
    """Tests for overnight export behaviour (v4.0/v4.4).

    v4.0: Forced overnight discharge (v3.x 'night export') DISABLED.
    v4.4: Flood prevention pre-drain (FLOOD_PREV) replaces it — see TestFloodPrevention.

    This class tests:
      - Export disabled → self-consumption
      - Flood prevention NOT triggered when forecast < 2× need (25 kWh < 28.8 kWh)
      - Legacy (v3.x) export_active=True (no flood_prev_target_soc) → stopped immediately
      - Import takes precedence over any export on Tracker flat-rate
    """

    def setUp(self):
        self.bm = BatteryManager()
        # forecast that is 'good' by old v3.x standard (25*0.6=15>14.4) but NOT
        # sufficient for flood prevention (25 < 2*14.4=28.8)
        self._moderate_tomorrow_kwh = 25.0
        # forecast that is clearly poor (15 kWh)
        self._poor_tomorrow_kwh = 15.0

    def test_overnight_export_not_triggered_when_forecast_below_flood_threshold(self):
        """High SOC + moderate forecast (25 kWh < 2×14.4=28.8 kWh) → self-consumption.

        Night export is disabled in v4.0. Flood prevention requires forecast ≥ 2× need.
        25 kWh < 28.8 kWh threshold → no export, stay in self-consumption.
        """
        snapshot = _make_snapshot(
            soc_pct                = 80.0,
            export_enabled         = True,
            pv_watts               = 0,
            corrected_tomorrow_kwh = self._moderate_tomorrow_kwh,
            now_hour               = 2,
        )
        decision = self.bm.evaluate(snapshot)

        self.assertEqual(decision.action, ACTION_SELF_CONSUMPTION)

    def test_overnight_export_not_triggered_when_export_disabled(self):
        """Export disabled → self-consumption regardless of SOC or forecast."""
        snapshot = _make_snapshot(
            soc_pct                = 80.0,
            export_enabled         = False,
            pv_watts               = 0,
            corrected_tomorrow_kwh = self._moderate_tomorrow_kwh,
            now_hour               = 2,
        )
        decision = self.bm.evaluate(snapshot)

        self.assertEqual(decision.action, ACTION_SELF_CONSUMPTION)

    def test_no_export_after_sunrise(self):
        """After today's sunrise, flood prevention does not fire — daytime detected.

        is_daytime requires P50 data. Provide a sunny-day P50 so balance correctly
        marks 08:00 as daytime. Flood prevention only runs overnight.
        """
        today_str    = _today_str()
        tomorrow_str = (datetime.now(timezone.utc).date() + timedelta(days=1)).strftime("%Y-%m-%d")
        snapshot = _make_snapshot(
            soc_pct                = 80.0,
            export_enabled         = True,
            pv_watts               = 0,
            corrected_tomorrow_kwh = 60.0,   # very sunny — would trigger flood prev overnight
            now_hour               = 8,
            forecast_p50           = _make_sunny_p50(dusk_hour=19),
            dawn_times             = {
                today_str:    _now(hour=7),
                tomorrow_str: _tomorrow_dawn(hour=7),
            },
        )
        decision = self.bm.evaluate(snapshot)

        self.assertNotEqual(decision.action, ACTION_START_EXPORT)

    def test_legacy_export_active_ignored(self):
        """Legacy export_active=True without flood_prev context: no longer treated
        as a v3.x "stop export" override (removed in v5.0). Evaluator now falls
        through to normal 24h sufficiency logic instead of forcibly stopping."""
        snapshot = _make_snapshot(
            soc_pct                = 75.0,
            export_enabled         = True,
            pv_watts               = 0,
            export_active          = True,
            flood_prev_target_soc  = 0.0,    # no flood prevention context
            corrected_tomorrow_kwh = self._moderate_tomorrow_kwh,
            now_hour               = 2,
        )
        decision = self.bm.evaluate(snapshot)

        # The deprecated ACTION_STOP_EXPORT migration path is gone.  Decision
        # should be one of the normal evaluator outcomes (self-consumption,
        # overflow, scheduled import etc.), never the legacy stop signal.
        self.assertNotEqual(decision.action, ACTION_STOP_EXPORT)

    def test_poor_forecast_gives_self_consumption(self):
        """Poor solar forecast → no export at all; system stays in self-consumption."""
        snapshot = _make_snapshot(
            soc_pct                = 80.0,
            export_enabled         = True,
            pv_watts               = 0,
            corrected_tomorrow_kwh = self._poor_tomorrow_kwh,
            now_hour               = 2,
        )
        decision = self.bm.evaluate(snapshot)

        self.assertNotEqual(decision.action, ACTION_START_EXPORT)

    def test_tracker_flat_rate_overrides_export_on_low_soc(self):
        """Low SOC on Tracker flat-rate: grid passthrough, not import or export.

        12% SOC, poor tomorrow forecast → import_needed. Tracker flat-rate returns
        SELF_CONSUMPTION (grid passthrough to house) rather than ACTION_START_IMPORT.
        """
        snapshot = _make_snapshot(
            soc_pct                = 12.0,
            export_enabled         = True,
            pv_watts               = 0,
            corrected_tomorrow_kwh = 5.0,    # poor day — shortfall guaranteed
            now_hour               = 20,
        )
        decision = self.bm.evaluate(snapshot)

        # Tracker flat-rate: grid passthrough is more efficient than battery pre-charge
        self.assertEqual(decision.action, ACTION_SELF_CONSUMPTION)


class TestFloodPrevention(unittest.TestCase):
    """Tests for overnight flood prevention pre-drain logic (v4.4).

    Constants: threshold=55%, target=40%, forecast_mult=3.0x
    Daily need pinned to 22.0 kWh via weekday_kwh=22.0, weekend_kwh=22.0 in snapshots.
    Flood prevention fires when: tomorrow_solar >= 3 * 22.0 = 66.0 kWh.
    sunny_tomorrow = 70.0 kWh  (well above 3x threshold, pinned need avoids day-of-week fragility)
    poor_tomorrow  = 20.0 kWh  (below 3x threshold)
    """

    def setUp(self):
        self.bm = BatteryManager()
        self.sunny_tomorrow   = 70.0   # 70 >= 66.0 — triggers flood prevention (3 * 22.0)
        self.poor_tomorrow    = 20.0   # 20 <  66.0 — blocked

    # ── Trigger conditions ─────────────────────────────────────────────────────

    def test_flood_prev_triggers_when_all_conditions_met(self):
        """High SOC + sunny tomorrow forecast at night + export enabled → pre-drain."""
        snapshot = _make_snapshot(
            soc_pct                = 70.0,
            export_enabled         = True,
            corrected_tomorrow_kwh = self.sunny_tomorrow,
            now_hour               = 22,
            weekday_kwh            = 22.0,
            weekend_kwh            = 22.0,
        )
        decision = self.bm.evaluate(snapshot)

        self.assertEqual(decision.action, ACTION_START_EXPORT)
        self.assertAlmostEqual(decision.target_soc_pct, FLOOD_PREV_TARGET_PCT)
        self.assertEqual(decision.power_watts, 4000)   # max_export_kw=4.0 default

    def test_flood_prev_blocked_when_export_disabled(self):
        """Export MPAN not active → no flood prevention."""
        snapshot = _make_snapshot(
            soc_pct                = 70.0,
            export_enabled         = False,
            corrected_tomorrow_kwh = self.sunny_tomorrow,
            now_hour               = 22,
        )
        decision = self.bm.evaluate(snapshot)

        self.assertNotEqual(decision.action, ACTION_START_EXPORT)

    def test_flood_prev_blocked_when_forecast_not_abundant(self):
        """Tomorrow forecast < 3x daily need → don't pre-drain (risk of reimport)."""
        snapshot = _make_snapshot(
            soc_pct                = 70.0,
            export_enabled         = True,
            corrected_tomorrow_kwh = self.poor_tomorrow,
            now_hour               = 22,
        )
        decision = self.bm.evaluate(snapshot)

        self.assertNotEqual(decision.action, ACTION_START_EXPORT)

    def test_flood_prev_post_midnight_blocked_when_today_forecast_poor(self):
        """21-May-2026 regression: post-midnight pre-drain must check TODAY's forecast.

        At 00:25 local, "tomorrow" in the cache rolls over to the day after the
        refill day. The pre-bug check used tomorrow_solar (= sunny day-after) and
        dumped the battery into a poor-today/sunny-day-after pair, then failed to
        refill from today's weak sun. Fix: when dawn is later today (post-midnight),
        gate on corrected_today_kwh instead.
        """
        today_str = _today_str()
        # Live 21-May numbers: today=29.5 kWh raw (poor refill), tomorrow=82.6 kWh (irrelevant here)
        snapshot = _make_snapshot(
            soc_pct                = 82.0,                # was 82.5% in the incident
            export_enabled         = True,
            corrected_today_kwh    = 34.3,                # 29.5 raw * 1.163 bias
            corrected_tomorrow_kwh = self.sunny_tomorrow, # day-after looks great, irrelevant
            now_hour               = 0,                   # 00:25-ish — post-midnight
            weekday_kwh            = 22.0,
            weekend_kwh            = 22.0,
            dawn_times             = {today_str: _now(hour=6)},  # dawn is later today
        )
        decision = self.bm.evaluate(snapshot)

        # 34.3 < 3 * 22.0 = 66.0 → must NOT export
        self.assertNotEqual(decision.action, ACTION_START_EXPORT)

    def test_flood_prev_allows_small_vpp_on_refill_day(self):
        """Small Axle event on refill day: 70 - (22+4)*3 = 70 - 78 → would block,
        but with 80 kWh forecast: 80 >= 3*(22+4)=78 → passes.

        Confirms VPP is added to demand but doesn't kill flood-prev on genuinely
        sunny days.
        """
        snapshot = _make_snapshot(
            soc_pct                = 70.0,
            export_enabled         = True,
            corrected_tomorrow_kwh = 80.0,         # sunny — covers 3x (22+4)
            vpp_tomorrow_kwh       = 4.0,          # 1h × 4kW
            now_hour               = 22,
            weekday_kwh            = 22.0,
            weekend_kwh            = 22.0,
        )
        decision = self.bm.evaluate(snapshot)

        self.assertEqual(decision.action, ACTION_START_EXPORT)
        self.assertIn("Axle", decision.reason)        # log breakdown shows VPP
        self.assertIn("4.0", decision.reason)

    def test_flood_prev_blocked_by_large_vpp_on_refill_day(self):
        """Large Axle event pushes refill demand above 3x threshold → block.

        70 kWh forecast vs (22 + 8) × 3 = 90 kWh → fails. Without the VPP gate
        this would have fired (70 > 3*22 = 66).
        """
        snapshot = _make_snapshot(
            soc_pct                = 70.0,
            export_enabled         = True,
            corrected_tomorrow_kwh = 70.0,         # would pass without VPP
            vpp_tomorrow_kwh       = 8.0,          # 2h × 4kW VPP scheduled tomorrow
            now_hour               = 22,
            weekday_kwh            = 22.0,
            weekend_kwh            = 22.0,
        )
        decision = self.bm.evaluate(snapshot)

        self.assertNotEqual(decision.action, ACTION_START_EXPORT)

    def test_flood_prev_post_midnight_uses_today_vpp_field(self):
        """After midnight, the refill-day VPP comes from vpp_today_kwh, not
        vpp_tomorrow_kwh. Same arithmetic, different field."""
        today_str = _today_str()
        snapshot = _make_snapshot(
            soc_pct                = 70.0,
            export_enabled         = True,
            corrected_today_kwh    = 70.0,         # would pass without VPP
            corrected_tomorrow_kwh = 100.0,        # irrelevant — refill day is today
            vpp_today_kwh          = 8.0,          # large VPP scheduled today
            vpp_tomorrow_kwh       = 0.0,
            now_hour               = 0,
            weekday_kwh            = 22.0,
            weekend_kwh            = 22.0,
            dawn_times             = {today_str: _now(hour=6)},
        )
        decision = self.bm.evaluate(snapshot)

        self.assertNotEqual(decision.action, ACTION_START_EXPORT)

    def test_flood_prev_post_midnight_triggers_when_today_forecast_sunny(self):
        """Post-midnight, when TODAY's forecast itself is abundantly sunny,
        flood prevention should still trigger (the bug fix must not be too tight)."""
        today_str = _today_str()
        snapshot = _make_snapshot(
            soc_pct                = 70.0,
            export_enabled         = True,
            corrected_today_kwh    = self.sunny_tomorrow,  # today is great
            corrected_tomorrow_kwh = self.poor_tomorrow,   # day-after irrelevant when dawn is today
            now_hour               = 0,
            weekday_kwh            = 22.0,
            weekend_kwh            = 22.0,
            dawn_times             = {today_str: _now(hour=6)},
        )
        decision = self.bm.evaluate(snapshot)

        self.assertEqual(decision.action, ACTION_START_EXPORT)

    def test_flood_prev_blocked_when_soc_below_threshold(self):
        """SOC below 55% threshold — not enough to drain to 40% usefully."""
        snapshot = _make_snapshot(
            soc_pct                = 50.0,   # below FLOOD_PREV_SOC_THRESHOLD_PCT (55%)
            export_enabled         = True,
            corrected_tomorrow_kwh = self.sunny_tomorrow,
            now_hour               = 22,
        )
        decision = self.bm.evaluate(snapshot)

        self.assertNotEqual(decision.action, ACTION_START_EXPORT)

    def test_flood_prev_blocked_when_already_at_target(self):
        """SOC already at or below target — nothing to drain."""
        snapshot = _make_snapshot(
            soc_pct                = 40.0,   # = FLOOD_PREV_TARGET_PCT
            export_enabled         = True,
            corrected_tomorrow_kwh = self.sunny_tomorrow,
            now_hour               = 22,
        )
        decision = self.bm.evaluate(snapshot)

        self.assertNotEqual(decision.action, ACTION_START_EXPORT)

    def test_flood_prev_blocked_during_daytime(self):
        """Daytime hours — flood prevention only runs overnight.

        is_daytime=True requires P50 data (to determine dusk). Provide a sunny-day
        P50 so the balance correctly marks 13:00 as daytime.
        """
        today_str    = _today_str()
        tomorrow_str = (datetime.now(timezone.utc).date() + timedelta(days=1)).strftime("%Y-%m-%d")
        snapshot = _make_snapshot(
            soc_pct                = 70.0,
            export_enabled         = True,
            corrected_tomorrow_kwh = self.sunny_tomorrow,
            now_hour               = 13,
            forecast_p50           = _make_sunny_p50(dusk_hour=19),   # dusk ~20:00 local
            dawn_times             = {
                today_str:    _now(hour=6),             # today dawn 6h ago → daytime
                tomorrow_str: _tomorrow_dawn(hour=6),
            },
        )
        decision = self.bm.evaluate(snapshot)

        self.assertNotEqual(decision.action, ACTION_START_EXPORT)

    def test_flood_prev_uses_storm_floor_when_higher(self):
        """Storm raises dawn_target_pct above default — effective target uses that floor."""
        # dawn_target_pct raised to 50% by storm watch. 50% < 55% threshold so still triggers.
        # target should be 50%, not 40%.
        snapshot = _make_snapshot(
            soc_pct                = 70.0,
            export_enabled         = True,
            corrected_tomorrow_kwh = self.sunny_tomorrow,
            now_hour               = 22,
            dawn_target_pct        = 50.0,   # storm raised floor
            weekday_kwh            = 22.0,
            weekend_kwh            = 22.0,
        )
        decision = self.bm.evaluate(snapshot)

        self.assertEqual(decision.action, ACTION_START_EXPORT)
        self.assertAlmostEqual(decision.target_soc_pct, 50.0)   # uses storm floor, not 40%

    def test_flood_prev_blocked_when_storm_floor_at_threshold(self):
        """Storm raises dawn_target_pct to >= 55% threshold — flood prevention skipped."""
        # effective_target = max(40%, 55%) = 55% >= 55% threshold → no point draining
        snapshot = _make_snapshot(
            soc_pct                = 70.0,
            export_enabled         = True,
            corrected_tomorrow_kwh = self.sunny_tomorrow,
            now_hour               = 22,
            dawn_target_pct        = 55.0,   # storm raised floor to threshold
        )
        decision = self.bm.evaluate(snapshot)

        self.assertNotEqual(decision.action, ACTION_START_EXPORT)

    def test_flood_prev_power_watts_matches_max_export_kw(self):
        """power_watts in decision reflects the max_export_kw setting."""
        snapshot = _make_snapshot(
            soc_pct                = 70.0,
            export_enabled         = True,
            corrected_tomorrow_kwh = self.sunny_tomorrow,
            now_hour               = 22,
            max_export_kw          = 3.6,
            weekday_kwh            = 22.0,
            weekend_kwh            = 22.0,
        )
        decision = self.bm.evaluate(snapshot)

        self.assertEqual(decision.action, ACTION_START_EXPORT)
        self.assertEqual(decision.power_watts, 3600)

    # ── compute_flood_preview (single source of truth for the advisory) ────────

    def test_flood_preview_would_fire_matches_live_decision(self):
        """The published preview's would_fire must agree with the live decision: when
        the night gate fires, would_fire is True and the numbers reflect the refill day."""
        snapshot = _make_snapshot(
            soc_pct                = 70.0,
            export_enabled         = True,
            corrected_tomorrow_kwh = self.sunny_tomorrow,
            now_hour               = 22,
            weekday_kwh            = 22.0,
            weekend_kwh            = 22.0,
        )
        decision = self.bm.evaluate(snapshot)
        preview  = self.bm.compute_flood_preview(snapshot)

        self.assertEqual(decision.action, ACTION_START_EXPORT)
        self.assertTrue(preview["would_fire"])
        self.assertTrue(preview["forecast_gate_pass"])
        self.assertEqual(preview["refill_label"], "Tomorrow")
        self.assertAlmostEqual(preview["refill_solar_kwh"], self.sunny_tomorrow, places=1)
        self.assertAlmostEqual(preview["refill_demand_kwh"], 22.0, places=1)

    def test_flood_preview_is_forward_looking_during_daytime(self):
        """KEY property: the preview ignores the daytime guard so a 20:00 (still-daylight)
        advisory run gets a real 'would it fire tonight' signal. evaluate() returns
        self-consumption during daytime, but the preview's would_fire is still True."""
        today_str    = _today_str()
        tomorrow_str = (datetime.now(timezone.utc).date() + timedelta(days=1)).strftime("%Y-%m-%d")
        snapshot = _make_snapshot(
            soc_pct                = 70.0,
            export_enabled         = True,
            corrected_tomorrow_kwh = self.sunny_tomorrow,
            now_hour               = 13,
            forecast_p50           = _make_sunny_p50(dusk_hour=19),
            dawn_times             = {
                today_str:    _now(hour=6),
                tomorrow_str: _tomorrow_dawn(hour=6),
            },
            weekday_kwh            = 22.0,
            weekend_kwh            = 22.0,
        )
        decision = self.bm.evaluate(snapshot)
        preview  = self.bm.compute_flood_preview(snapshot)

        self.assertNotEqual(decision.action, ACTION_START_EXPORT)   # daytime: doesn't act
        self.assertTrue(preview["would_fire"])                      # but would, tonight

    def test_flood_preview_post_midnight_uses_today_refill_day(self):
        """23/24-Jun-2026 regression at the preview level: post-midnight the gate must use
        TODAY's (refill-day) forecast, not the day-after. Poor today -> would_fire False and
        refill_label 'Today' — this is exactly what the advisory now reports verbatim."""
        today_str = _today_str()
        snapshot = _make_snapshot(
            soc_pct                = 72.0,
            export_enabled         = True,
            corrected_today_kwh    = 61.3,                # 2.77x of 22 — below the 3x gate
            corrected_tomorrow_kwh = self.sunny_tomorrow, # day-after looks great, irrelevant
            now_hour               = 1,                   # 01:45-ish — post-midnight
            weekday_kwh            = 22.0,
            weekend_kwh            = 22.0,
            dawn_times             = {today_str: _now(hour=6)},
        )
        decision = self.bm.evaluate(snapshot)
        preview  = self.bm.compute_flood_preview(snapshot)

        self.assertNotEqual(decision.action, ACTION_START_EXPORT)
        self.assertFalse(preview["would_fire"])
        self.assertFalse(preview["forecast_gate_pass"])
        self.assertEqual(preview["refill_label"], "Today")
        self.assertAlmostEqual(preview["refill_solar_kwh"], 61.3, places=1)
        self.assertLess(preview["ratio"], 3.0)

    def test_flood_preview_export_disabled_blocks_would_fire(self):
        """Export-MPAN lockout (e.g. post power-cut) -> would_fire False even on a sunny
        high-SOC night, so the advisory can't promise an export during the lockout."""
        snapshot = _make_snapshot(
            soc_pct                = 70.0,
            export_enabled         = False,
            corrected_tomorrow_kwh = self.sunny_tomorrow,
            now_hour               = 22,
            weekday_kwh            = 22.0,
            weekend_kwh            = 22.0,
        )
        preview = self.bm.compute_flood_preview(snapshot)

        self.assertFalse(preview["would_fire"])
        self.assertFalse(preview["export_enabled"])
        self.assertTrue(preview["forecast_gate_pass"])   # forecast is fine; MPAN is the blocker

    # ── Continue / stop logic ─────────────────────────────────────────────────

    def test_flood_prev_continues_when_active_and_above_target(self):
        """Flood prevention running (export_active=True, flood_prev_target_soc=40)
        and SOC still above target → continue (ACTION_START_EXPORT, idempotent)."""
        snapshot = _make_snapshot(
            soc_pct                = 55.0,   # above 40% target
            export_enabled         = True,
            export_active          = True,   # export already running
            flood_prev_target_soc  = 40.0,
            corrected_today_kwh    = self.sunny_tomorrow,  # post-midnight: today is refill day
            corrected_tomorrow_kwh = self.sunny_tomorrow,
            now_hour               = 23,
            weekday_kwh            = 22.0,
            weekend_kwh            = 22.0,
        )
        decision = self.bm.evaluate(snapshot)

        self.assertEqual(decision.action, ACTION_START_EXPORT)
        self.assertAlmostEqual(decision.target_soc_pct, 40.0)

    def test_flood_prev_stops_when_target_reached(self):
        """SOC reached target — stop and return to self-consumption."""
        snapshot = _make_snapshot(
            soc_pct                = 39.8,   # at (or below) 40% target
            export_enabled         = True,
            export_active          = True,
            flood_prev_target_soc  = 40.0,
            corrected_tomorrow_kwh = self.sunny_tomorrow,
            now_hour               = 23,
        )
        decision = self.bm.evaluate(snapshot)

        self.assertEqual(decision.action, ACTION_SELF_CONSUMPTION)

    def test_flood_prev_stops_at_dawn(self):
        """Dawn breaks mid-drain — stop export, let solar overflow take over.

        is_daytime=True requires P50 data (to determine dusk). Provide a sunny-day
        P50 so the balance correctly marks 08:00 as daytime.
        """
        today_str    = _today_str()
        tomorrow_str = (datetime.now(timezone.utc).date() + timedelta(days=1)).strftime("%Y-%m-%d")
        snapshot = _make_snapshot(
            soc_pct                = 50.0,   # still above target
            export_enabled         = True,
            export_active          = True,
            flood_prev_target_soc  = 40.0,
            corrected_tomorrow_kwh = self.sunny_tomorrow,
            now_hour               = 8,      # after dawn
            forecast_p50           = _make_sunny_p50(dusk_hour=19),   # dusk ~20:00 local
            dawn_times             = {
                today_str:    _now(hour=6),   # today dawn 2h ago → daytime
                tomorrow_str: _tomorrow_dawn(hour=6),
            },
        )
        decision = self.bm.evaluate(snapshot)

        self.assertEqual(decision.action, ACTION_SELF_CONSUMPTION)

    def test_flood_prev_stops_if_forecast_weakens(self):
        """Forecast updated overnight to < 2x need — abort to protect tomorrow."""
        snapshot = _make_snapshot(
            soc_pct                = 55.0,
            export_enabled         = True,
            export_active          = True,
            flood_prev_target_soc  = 40.0,
            corrected_tomorrow_kwh = self.poor_tomorrow,   # forecast dropped
            now_hour               = 23,
        )
        decision = self.bm.evaluate(snapshot)

        self.assertEqual(decision.action, ACTION_SELF_CONSUMPTION)

    def test_legacy_export_active_no_longer_forces_stop(self):
        """v5.0+: export_active=True with flood_prev_target_soc=0 no longer
        triggers the legacy ACTION_STOP_EXPORT migration path. The evaluator
        falls through and may issue start_export for flood prevention if the
        forecast supports it."""
        snapshot = _make_snapshot(
            soc_pct                = 80.0,
            export_enabled         = True,
            export_active          = True,
            flood_prev_target_soc  = 0.0,   # no flood prevention context
            corrected_tomorrow_kwh = self.sunny_tomorrow,
            now_hour               = 2,
        )
        decision = self.bm.evaluate(snapshot)

        self.assertNotEqual(decision.action, ACTION_STOP_EXPORT)


class TestTrackerMidnightLocal(unittest.TestCase):
    """Tests for Tracker defer scheduling at Europe/London midnight (v4.5 fix).

    The tariff boundary is at local midnight, not UTC midnight. During BST
    this means UTC 23:00, during GMT this means UTC 00:00. Either is valid;
    importantly the schedule must NEVER land at UTC 01:00 (the bug we fixed).
    """

    def setUp(self):
        self.bm = BatteryManager()

    def test_tracker_defer_uses_europe_london_midnight(self):
        """Schedule must be at Europe/London 00:05, not now.tzinfo midnight."""
        snapshot = _make_snapshot(
            soc_pct         = 25.0,
            tariff_key      = TARIFF_TRACKER,
            today_rate_p    = 28.0,
            tomorrow_rate_p = 20.0,
            now_hour        = 20,
        )
        decision = self.bm.evaluate(snapshot)

        self.assertEqual(decision.action, ACTION_SCHEDULE_IMPORT)
        # Resolve the scheduled time in Europe/London — must always be 00:05 local.
        # Was skipped entirely when pytz was absent — i.e. this assertion had
        # never actually run on the usual test runner. zoneinfo is stdlib, so it
        # runs everywhere and there is nothing left to skip.
        from zoneinfo import ZoneInfo
        london = decision.scheduled_time.astimezone(ZoneInfo("Europe/London"))
        self.assertEqual(london.hour, 0)
        self.assertEqual(london.minute, 5)


# NB: the old TestPowerCutLockoutParsing class was deleted here (01-Jul-2026) —
# all three of its tests exercised only stdlib datetime.fromisoformat, never any
# plugin code. The real behaviour (corrupt / tz-aware / naive powerRestoredTime)
# is covered against the actual parsing path by TestResolveExportLockout in
# test_plugin.py, including the naive-isoformat case moved there.


class TestOctopusTouLocalBucketing(unittest.TestCase):
    """Tests for Octopus TOU UTC→Europe/London conversion (v4.5 fix).

    The bug: cheap_start/cheap_end are local-time strings ("00:30"–"05:30")
    but slots arrive as UTC. During BST a UTC slot at 23:30 is local 00:30 —
    so it should be classified as cheap. Pre-fix it was classified as standard.
    """

    def setUp(self):
        try:
            from octopus_api import OctopusAPI, TARIFF_GO, TARIFF_WINDOWS
            self.api    = OctopusAPI(api_key="", account_id="", mpan="", serial="")
            self.window = TARIFF_WINDOWS[TARIFF_GO]
        except ImportError:
            self.skipTest("octopus_api not importable")

    def test_bst_utc_2330_classified_as_cheap_local_0030(self):
        """During BST: UTC 23:30 == local 00:30 (cheap window starts at 00:30)."""
        # Build a Go-style cheap slot at UTC 23:30 in summer (BST in effect).
        slots = [{
            "valid_from":    "2026-06-15T23:30:00Z",
            "valid_to":      "2026-06-16T00:00:00Z",
            "value_inc_vat": 7.0,
        }]
        result = self.api._parse_tou_slots(slots, self.window)
        # Should be picked up as cheap (local 00:30 is in the 00:30-05:30 window)
        self.assertIsNotNone(result.get("cheap_p"))
        self.assertEqual(result["cheap_p"], 7.0)

    def test_gmt_utc_0030_still_classified_as_cheap(self):
        """During GMT (winter): UTC 00:30 == local 00:30 — cheap as expected."""
        slots = [{
            "valid_from":    "2026-12-15T00:30:00Z",
            "valid_to":      "2026-12-15T01:00:00Z",
            "value_inc_vat": 7.0,
        }]
        result = self.api._parse_tou_slots(slots, self.window)
        self.assertEqual(result.get("cheap_p"), 7.0)

    def test_bst_utc_0030_is_local_0130_not_cheap(self):
        """During BST: UTC 00:30 == local 01:30 — still inside the 00:30-05:30
        cheap window so SHOULD be cheap. Regression for the inverse case to
        confirm BST→local shift is applied (not just classified-as-cheap by
        coincidence)."""
        # UTC 04:30 in summer == local 05:30 — boundary, should be standard
        slots = [{
            "valid_from":    "2026-06-15T04:30:00Z",
            "valid_to":      "2026-06-15T05:00:00Z",
            "value_inc_vat": 25.0,
        }]
        result = self.api._parse_tou_slots(slots, self.window)
        # local 05:30 is the END of the cheap window (exclusive) → standard
        self.assertIsNone(result.get("cheap_p"))
        self.assertEqual(result.get("standard_p"), 25.0)


class TestModbusSleepFunction(unittest.TestCase):
    """Tests for sigenergy_modbus sleep_func injection (v4.5 fix)."""

    def test_default_uses_time_sleep(self):
        """Without sleep_func, _sleep is the standard time.sleep."""
        try:
            import time as _time
            from sigenergy_modbus import SigenergyModbus
        except ImportError:
            self.skipTest("sigenergy_modbus not importable")
        m = SigenergyModbus(ip="127.0.0.1")
        self.assertIs(m._sleep, _time.sleep)

    def test_injected_sleep_func_used(self):
        """A custom sleep_func is invoked by _throttle()."""
        try:
            from sigenergy_modbus import SigenergyModbus
        except ImportError:
            self.skipTest("sigenergy_modbus not importable")

        calls = []
        def fake_sleep(secs):
            calls.append(secs)

        m = SigenergyModbus(ip="127.0.0.1", sleep_func=fake_sleep)
        m._last_request_time = time_module.time()   # force throttle to engage
        m._throttle()
        m._throttle()   # second call within 1s — must sleep
        self.assertGreater(len(calls), 0)
        # All sleeps must be <= the 1.0s protocol minimum
        for s in calls:
            self.assertLessEqual(s, 1.0)
            self.assertGreaterEqual(s, 0.0)


class TestResilienceBuffer(unittest.TestCase):
    """_check_resilience_buffer — the ONLY Tracker grid-import path and the
    carrier of the v5.40 storm reserve (a storm override raises dawn_target_pct
    to 50%, and this is what actually imports up to it). Previously untested."""

    def setUp(self):
        self.bm = BatteryManager()

    def _run(self, **kwargs):
        snapshot = _make_snapshot(**kwargs)
        balance  = self.bm._calculate_24h_balance(snapshot)
        return self.bm._check_resilience_buffer(snapshot, balance)

    def test_tracker_night_below_storm_floor_imports_to_floor_plus_2(self):
        # Storm override raised dawn target to 50%; SOC 30% at night -> import
        # at full power to 52% (the +2% overshoot prevents stop/start cycling).
        d = self._run(soc_pct=30.0, dawn_target_pct=50.0, now_hour=20)
        self.assertIsNotNone(d)
        self.assertEqual(d.action, ACTION_START_IMPORT)
        self.assertAlmostEqual(d.target_soc_pct, 52.0)
        self.assertEqual(d.power_watts, 10000)

    def test_at_or_above_floor_no_import(self):
        # >= boundary: exactly at the floor means no import (and none above it).
        self.assertIsNone(self._run(soc_pct=50.0, dawn_target_pct=50.0,
                                    now_hour=20))
        self.assertIsNone(self._run(soc_pct=55.0, dawn_target_pct=50.0,
                                    now_hour=20))

    def test_daytime_skips_resilience_import(self):
        # Daytime with sun: solar recharges the buffer — no grid import.
        # is_daytime needs today's dawn in dawn_times AND P50 data for dusk.
        today_str    = _today_str()
        tomorrow_str = (datetime.now(timezone.utc).date()
                        + timedelta(days=1)).strftime("%Y-%m-%d")
        d = self._run(soc_pct=30.0, dawn_target_pct=50.0, now_hour=12,
                      forecast_p50=_make_sunny_p50(), pv_watts=5000,
                      dawn_times={today_str:    _now(hour=7),
                                  tomorrow_str: _tomorrow_dawn(hour=7)})
        self.assertIsNone(d)

    def test_default_floor_imports_to_12_pct(self):
        # Default dawn target 10%: SOC 8% -> import to 12% (10 + 2 overshoot).
        d = self._run(soc_pct=8.0, dawn_target_pct=10.0, now_hour=20)
        self.assertIsNotNone(d)
        self.assertEqual(d.action, ACTION_START_IMPORT)
        self.assertAlmostEqual(d.target_soc_pct, 12.0)

    def test_tou_outside_cheap_window_no_resilience_import(self):
        # Go, below floor but OUTSIDE the cheap window: never import the reserve
        # at the day rate — wait for the cheap window. Resilience returns None.
        d = self._run(soc_pct=8.0, dawn_target_pct=10.0, now_hour=20,
                      tariff_key=TARIFF_GO, cheap_start="23:30",
                      cheap_end="04:30", corrected_tomorrow_kwh=40.0)
        self.assertIsNone(d)

    def test_tou_in_cheap_window_below_floor_imports_at_night_rate(self):
        # Go, tomorrow already covered (import planner won't fire) yet SOC below
        # the power-cut floor, INSIDE the cheap window: top the reserve up at the
        # night rate. This is the gap that otherwise drains to the health floor
        # on a night before a sunny day (fixed 05-Jul-2026).
        today_str    = _today_str()
        tomorrow_str = (datetime.now(timezone.utc).date()
                        + timedelta(days=1)).strftime("%Y-%m-%d")
        d = self._run(soc_pct=8.0, dawn_target_pct=10.0, now_hour=2,
                      tariff_key=TARIFF_GO, cheap_start="23:30",
                      cheap_end="04:30", corrected_tomorrow_kwh=40.0,
                      dawn_times={today_str:    _now(hour=7),
                                  tomorrow_str: _tomorrow_dawn(hour=7)})
        self.assertIsNotNone(d)
        self.assertEqual(d.action, ACTION_START_IMPORT)
        self.assertAlmostEqual(d.target_soc_pct, 12.0)

    def test_tou_import_needed_defers_to_planner(self):
        # Go, SOC below floor AND tomorrow not covered (import_needed=True):
        # defer to the TOU import planner (priority 4), which charges to the full
        # tomorrow-cover target. Resilience returns None to avoid truncating it.
        today_str    = _today_str()
        tomorrow_str = (datetime.now(timezone.utc).date()
                        + timedelta(days=1)).strftime("%Y-%m-%d")
        d = self._run(soc_pct=8.0, dawn_target_pct=10.0, now_hour=2,
                      tariff_key=TARIFF_GO, cheap_start="23:30",
                      cheap_end="04:30", corrected_tomorrow_kwh=0.0,
                      dawn_times={today_str:    _now(hour=7),
                                  tomorrow_str: _tomorrow_dawn(hour=7)})
        self.assertIsNone(d)


class TestAgileBreakEven(unittest.TestCase):
    """v5.44.0: the Agile import planner must respect the ~6% round-trip loss —
    the cheapest overnight slot only beats grid-to-house passthrough when
    rate / efficiency undercuts tomorrow's daytime average. Tracker and
    Flexible already gated on this economics; Agile imported unconditionally
    (closing 26-Jun deferred item (b))."""

    def setUp(self):
        self.bm = BatteryManager()

    def _agile(self, overnight_p, daytime_p, today_rate_p=25.0, soc=20.0):
        snapshot = _make_snapshot(soc_pct=soc, now_hour=20,
                                  today_rate_p=today_rate_p)
        snapshot.tariff.tariff_key = "agile"
        dawn = _tomorrow_dawn(hour=7)
        slots = []
        if overnight_p is not None:
            for h in (23, 24, 25):                       # tonight, before dawn
                slots.append((_now(hour=20) + timedelta(hours=h - 20),
                              overnight_p))
        if daytime_p is not None:
            for h in (3, 5, 7):                          # tomorrow daytime
                slots.append((dawn + timedelta(hours=h), daytime_p))
        snapshot.tariff.agile_slots = slots
        balance = self.bm._calculate_24h_balance(snapshot)
        return self.bm._plan_agile_import(snapshot, balance, target_soc=50.0)

    def test_flat_agile_day_prefers_passthrough(self):
        # 22p overnight / 23p daytime: 22 / 0.94 = 23.4p effective — importing
        # costs MORE than letting the house pull from grid tomorrow.
        d = self._agile(overnight_p=22.0, daytime_p=23.0)
        self.assertEqual(d.action, ACTION_SELF_CONSUMPTION)
        self.assertIn("conversion loss", d.reason)

    def test_steep_agile_day_still_imports(self):
        # 12p overnight / 30p daytime: 12.77p effective — clear win, schedule it.
        d = self._agile(overnight_p=12.0, daytime_p=30.0)
        self.assertIn(d.action, (ACTION_SCHEDULE_IMPORT, ACTION_START_IMPORT))

    def test_no_reference_rate_imports_ungated(self):
        # No tomorrow slots published and no today rate — no reference to
        # compare against, so behave as before v5.44.0 (import).
        d = self._agile(overnight_p=22.0, daytime_p=None, today_rate_p=None)
        self.assertIn(d.action, (ACTION_SCHEDULE_IMPORT, ACTION_START_IMPORT))

    def test_break_even_boundary_exact(self):
        # Effective cost exactly equal to the reference -> passthrough (the
        # round trip buys nothing, so don't cycle the battery for free).
        # 23.5 / 0.94 = 25.0 == daytime 25.0.
        d = self._agile(overnight_p=23.5, daytime_p=25.0)
        self.assertEqual(d.action, ACTION_SELF_CONSUMPTION)


class TestSurplusConservatism(unittest.TestCase):
    """Characterisation: surplus_kwh is DELIBERATELY conservative (owner
    decision 02-07-2026, closing 26-Jun deferred item (c)) — tomorrow-morning
    solar inside the next-24h window is NOT counted, so evening export
    eligibility is understated in the KPI-safe direction. If this test fails
    because someone made the formula 'precise', that is a decision change
    needing owner sign-off, not a bug fix."""

    def test_surplus_is_conservative_no_tomorrow_solar(self):
        bm = BatteryManager()
        # 20:00, SOC 50% (17.52 kWh), daily need 22 kWh, and a BIG tomorrow
        # forecast that a 'precise' rolling-24h model would partially count.
        # weekday_kwh == weekend_kwh here so the assertion is day-of-week agnostic
        # (it used to fail every weekend when _now() landed on a Sat/Sun and the
        # balance used the 30 kWh weekend need against a hard-coded 22).
        snapshot = _make_snapshot(soc_pct=50.0, now_hour=20,
                                  corrected_tomorrow_kwh=60.0,
                                  weekday_kwh=22.0, weekend_kwh=22.0)
        balance = bm._calculate_24h_balance(snapshot)
        # battery(17.52) + remaining_solar(0, night) - need(22) = -4.48:
        # negative despite 60 kWh forecast tomorrow — deliberately so.
        self.assertAlmostEqual(balance.surplus_kwh, 17.52 - 22.0, places=1)
        self.assertLess(balance.surplus_kwh, 0.0)


class TestFloodContinuationGuards(unittest.TestCase):
    """v5.42: an in-progress flood pre-drain must respect overrides that arrive
    MID-drain — a storm warning that suppresses export or raises the mandated
    reserve cannot be ignored just because the drain started on a calm night."""

    def setUp(self):
        self.bm = BatteryManager()

    def test_export_disabled_mid_drain_aborts(self):
        # Storm/lockout forced export_enabled False after the drain started:
        # the safety gate wins — stop, do not keep exporting the reserve.
        snapshot = _make_snapshot(
            soc_pct=60.0, now_hour=1, export_active=True,
            flood_prev_target_soc=40.0, export_enabled=False,
        )
        d = self.bm._check_overrides(snapshot)
        self.assertIsNotNone(d)
        self.assertEqual(d.action, ACTION_SELF_CONSUMPTION)
        self.assertIn("export disabled", d.reason.lower())

    def test_raised_dawn_target_ends_drain_early(self):
        # Storm raised dawn_target_pct to 50% mid-drain (drain target was 40%):
        # SOC 48% is already below the effective 50% stop -> drain ends now.
        snapshot = _make_snapshot(
            soc_pct=48.0, now_hour=1, export_active=True,
            flood_prev_target_soc=40.0, export_enabled=True,
            dawn_target_pct=50.0,
            corrected_tomorrow_kwh=60.0,   # refill gate would otherwise pass
        )
        d = self.bm._check_overrides(snapshot)
        self.assertIsNotNone(d)
        self.assertEqual(d.action, ACTION_SELF_CONSUMPTION)
        self.assertIn("50% reached", d.reason)

    def test_flood_prev_zero_demand_does_not_fire(self):
        # v5.43: explicit 0 in both consumption fields used to degenerate the
        # forecast gate to always-pass (threshold 3 x 0 = 0) — a pre-drain could
        # start before a completely sunless day. Zero demand now fails the gate.
        snapshot = _make_snapshot(
            soc_pct=70.0, now_hour=22, export_enabled=True,
            weekday_kwh=0.0, weekend_kwh=0.0,
            corrected_today_kwh=0.5, corrected_tomorrow_kwh=0.5,
        )
        preview = self.bm.compute_flood_preview(snapshot)
        self.assertFalse(preview["forecast_gate_pass"])
        self.assertFalse(preview["would_fire"])

    def test_calm_night_drain_continues_unchanged(self):
        # No overrides: drain above target with a sunny refill day continues,
        # still targeting the original 40%. NB the helper's default dawn_times
        # holds only TOMORROW's dawn, so the refill day resolves to tomorrow —
        # whose need follows the REAL calendar (snapshot.now anchors to the
        # actual today): weekday need 22 (gate 3x = 66; 70 passes) but weekend
        # need 30 (gate 90; 70 FAILS). That made this test fail every Friday
        # and Saturday run. Pin weekend_kwh to the weekday value so the gate
        # is 66 whichever real day the suite runs on.
        snapshot = _make_snapshot(
            soc_pct=70.0, now_hour=1, export_active=True,
            flood_prev_target_soc=40.0, export_enabled=True,
            corrected_today_kwh=70.0, corrected_tomorrow_kwh=70.0,
            weekend_kwh=22.0,   # calendar-proof: need is 22 on any run day
        )
        d = self.bm._check_overrides(snapshot)
        self.assertIsNotNone(d)
        self.assertEqual(d.action, ACTION_START_EXPORT)
        self.assertAlmostEqual(d.target_soc_pct, 40.0)


class TestSolarOverflowChargeTarget(unittest.TestCase):
    """v3.8: the overflow charge is paced to a TARGET SOC (default 90%), not to 100%.

    Aiming at 100% actively causes clipping: required_charge is subtracted from export
    BEFORE the DNO cap is applied, so a high target spends the low-surplus morning
    buying SOC out of exportable kWh, then still meets the afternoon peak with less
    headroom than it started with. Modelled on 20-Jul-2026's measured curve, a 90%
    target exported 1.6 kWh more, clipped nothing (vs 1.53 kWh) and still finished at
    90.8% vs 91.1%.

    The target is a GOAL, not a ceiling — above-cap excess still charges past it.
    """

    CAP = 35.04

    def _snap(self, soc_pct=77.0, pv_w=8500, home_w=750, target=None,
              min_end=None, storm=False, max_export_kw=4.0):
        return ManagerSnapshot(
            current_soc_pct           = soc_pct,
            capacity_kwh              = self.CAP,
            export_enabled            = True,
            max_export_kw             = max_export_kw,
            pv_watts                  = pv_w,
            house_load_watts          = home_w,
            solar_overflow_target_pct = (SOLAR_OVERFLOW_TARGET_SOC_PCT
                                         if target is None else target),
            solar_overflow_min_end_pct= (SOLAR_OVERFLOW_MIN_END_SOC_PCT
                                         if min_end is None else min_end),
            storm_active              = storm,
        )

    def _bal(self, soc_pct=77.0, remaining_solar=26.0, home_to_dusk=6.6,
             hours_to_dusk=7.0, surplus=36.0):
        return SufficiencyBalance(
            battery_kwh                = soc_pct / 100.0 * self.CAP,
            remaining_solar_kwh        = remaining_solar,
            remaining_home_to_dusk_kwh = home_to_dusk,
            is_daytime                 = True,
            hours_to_dusk              = hours_to_dusk,
            surplus_kwh                = surplus,
        )

    def _decide(self, **kw):
        snap_kw = {k: v for k, v in kw.items()
                   if k in ("soc_pct", "pv_w", "home_w", "target", "min_end",
                            "storm", "max_export_kw")}
        bal_kw  = {k: v for k, v in kw.items()
                   if k in ("remaining_solar", "home_to_dusk", "hours_to_dusk", "surplus")}
        soc = kw.get("soc_pct", 77.0)
        return BatteryManager()._check_solar_overflow(
            self._snap(**snap_kw), self._bal(soc_pct=soc, **bal_kw)
        )

    # ── The headline behaviour ─────────────────────────────────────────────
    def test_lower_target_exports_more_in_the_morning(self):
        """The whole point, and it bites exactly where the money was being lost: on a
        MORNING surplus that already fits under the DNO cap. At 4.05 kW surplus the
        100% target skims 1.15 kW off for charge, the 90% target only 0.65 kW — so
        half a kW that used to be stored is exported instead, and the battery arrives
        at the afternoon peak with more room. (At full midday surplus both hit the cap
        and look identical, which is why the morning is the case that matters.)"""
        at_90  = self._decide(target=90.0,  pv_w=4700, home_w=650)
        at_100 = self._decide(target=100.0, pv_w=4700, home_w=650)
        self.assertIsNotNone(at_90)
        self.assertIsNotNone(at_100)
        self.assertGreater(at_90.export_kw, at_100.export_kw)
        self.assertLess(at_90.power_watts, at_100.power_watts)   # smaller charge cap

    def test_midday_both_targets_pin_to_the_export_cap(self):
        """Characterisation: once surplus exceeds cap + required, the target makes no
        difference to export — the excess simply charges the battery either way."""
        at_90  = self._decide(target=90.0)
        at_100 = self._decide(target=100.0)
        self.assertAlmostEqual(at_90.export_kw, 4.0, places=6)
        self.assertAlmostEqual(at_100.export_kw, 4.0, places=6)

    def test_target_of_100_is_identical_to_pre_v38(self):
        """Regression pin: the formula reduces EXACTLY to the old one at target=100.
        headroom = (100 - soc)/100 * cap, required = headroom / hours_to_dusk."""
        d = self._decide(target=100.0, soc_pct=77.0, hours_to_dusk=7.0,
                         pv_w=4700, home_w=650)
        expected_req = (100.0 - 77.0) / 100.0 * self.CAP / 7.0
        surplus_kw   = (4700 - 650) / 1000.0
        self.assertAlmostEqual(d.export_kw, min(surplus_kw - expected_req, 4.0), places=6)

    def test_at_or_above_target_paces_at_zero_and_exports_full_cap(self):
        """Above the target there is nothing to pace towards — export runs at the cap
        and the battery simply takes the overspill (the 'goal not ceiling' property)."""
        d = self._decide(soc_pct=93.0, target=90.0)
        self.assertAlmostEqual(d.export_kw, 4.0, places=6)

    def test_excess_above_export_cap_still_charges_past_the_target(self):
        """8.5 kW PV - 0.75 kW home = 7.75 kW surplus, only 4 kW can leave. The rest
        MUST go to the battery even though we are already over the 90% target."""
        d = self._decide(soc_pct=93.0, target=90.0)
        self.assertAlmostEqual(d.power_watts, 8500 - 750 - 4000, delta=1)

    # ── Floor clamp ────────────────────────────────────────────────────────
    def test_target_below_the_floor_is_clamped_up(self):
        """A mis-set pref must never pace below the level wanted for a power cut."""
        d = self._decide(target=60.0, min_end=80.0)
        self.assertIn("to 80% target", d.reason)

    def test_target_above_the_floor_is_left_alone(self):
        d = self._decide(target=90.0, min_end=80.0)
        self.assertIn("to 90% target", d.reason)

    def test_physics_gate_already_protects_a_dull_day(self):
        """Documents WHY no dull-day guard is needed in the pacing. When the remaining
        solar can no longer fill the battery to 100%, the physics gate declines to
        export at all and everything charges — so a low target can never strand the
        battery. A lower target keeps SOC lower, which makes this bite EARLIER."""
        d = self._decide(soc_pct=55.0, target=90.0,
                         remaining_solar=9.0, home_to_dusk=5.0)
        self.assertIsNone(d)

    # ── Storm ──────────────────────────────────────────────────────────────
    def test_storm_restores_the_100_pct_target(self):
        d = self._decide(target=90.0, storm=True)
        self.assertIn("to 100% target", d.reason)
        self.assertIn("(storm)", d.reason)

    def test_storm_pacing_stays_lazy_not_a_force_charge(self):
        """CliveS's constraint: a storm raises the target but must not ram the battery
        full out of export when the day's own solar would get there anyway. Charge is
        still only the paced rate, never the whole surplus."""
        d = self._decide(target=90.0, storm=True, soc_pct=77.0)
        surplus_w = 8500 - 750
        self.assertLess(d.power_watts, surplus_w)
        self.assertGreater(d.export_kw, 0.0)

    def test_defaults(self):
        self.assertEqual(SOLAR_OVERFLOW_TARGET_SOC_PCT, 90.0)
        self.assertEqual(SOLAR_OVERFLOW_MIN_END_SOC_PCT, 80.0)


class TestSolarOverflowHysteresis(unittest.TestCase):
    """v3.10: the engage/release boundary has a dead band and a re-engage dwell.

    The physics gate used to be a hard cut at exactly zero. On 05-Aug-2026 the day's
    physics surplus sat on that boundary and the decision flapped: nine transitions,
    four of them inside twenty minutes, at surplus 0.0 / 0.4 / 0.2 kWh. Export stops
    during every gap, so surplus PV banks into an already-high battery with the DNO
    cap unused — which is the clipping the whole feature exists to prevent.

    Everything here is asymmetric on purpose. Starting is made harder; stopping is
    untouched and must stay immediate, because dusk, a storm and a collapsing
    forecast all arrive through the same release path.
    """

    CAP = 35.04

    def _snap(self, soc_pct=77.0, active=False, released_at=None, now=None):
        return ManagerSnapshot(
            current_soc_pct            = soc_pct,
            capacity_kwh               = self.CAP,
            export_enabled             = True,
            max_export_kw              = 4.0,
            pv_watts                   = 8500,
            house_load_watts           = 750,
            solar_overflow_active      = active,
            solar_overflow_released_at = released_at,
            now                        = now or _now(hour=14),
        )

    def _bal(self, surplus_kwh, soc_pct=77.0):
        """Build a balance whose PHYSICS surplus is exactly surplus_kwh.

        Derived from the gate's own definition rather than hand-tuned numbers, so a
        change to the headroom term cannot leave these tests quietly measuring
        something else.
        """
        headroom = (100.0 - soc_pct) / 100.0 * self.CAP
        home     = 6.6
        return SufficiencyBalance(
            battery_kwh                = soc_pct / 100.0 * self.CAP,
            remaining_solar_kwh        = surplus_kwh + headroom + home,
            remaining_home_to_dusk_kwh = home,
            is_daytime                 = True,
            hours_to_dusk              = 7.0,
            surplus_kwh                = 36.0,      # 24h gate: comfortably passed
        )

    def _decide(self, surplus_kwh, **kw):
        soc = kw.pop("soc_pct", 77.0)
        return BatteryManager()._check_solar_overflow(
            self._snap(soc_pct=soc, **kw), self._bal(surplus_kwh, soc_pct=soc)
        )

    # ── The bug, pinned ────────────────────────────────────────────────────
    def test_the_flapping_afternoon_no_longer_engages(self):
        """The three surpluses measured during the 05-Aug-2026 flapping — every one of
        them used to start an export, and none of them may now."""
        for observed in (0.0, 0.4, 0.2):
            with self.subTest(physics_surplus=observed):
                self.assertIsNone(self._decide(observed, active=False))

    def test_a_marginal_surplus_does_not_start_an_export(self):
        self.assertIsNone(self._decide(SOLAR_OVERFLOW_ENGAGE_KWH - 0.1, active=False))

    def test_a_clear_surplus_still_starts_one(self):
        d = self._decide(SOLAR_OVERFLOW_ENGAGE_KWH + 0.1, active=False)
        self.assertIsNotNone(d)
        self.assertGreater(d.export_kw, 0.0)

    # ── Asymmetry: the dead band only ever protects a RUNNING export ───────
    def test_a_running_export_survives_the_marginal_band(self):
        """The same surplus that may not START one must not STOP one either — that
        gap between the two thresholds is the entire fix."""
        marginal = SOLAR_OVERFLOW_ENGAGE_KWH - 0.1
        self.assertIsNone(self._decide(marginal, active=False))
        self.assertIsNotNone(self._decide(marginal, active=True))

    def test_release_is_still_immediate_below_zero(self):
        """Never delayed. Dusk, a storm and a collapsing forecast all land here."""
        self.assertIsNone(self._decide(SOLAR_OVERFLOW_RELEASE_KWH - 0.01, active=True))

    def test_release_ignores_the_dwell_entirely(self):
        """A dwell that could postpone a stand-down would be a stale cap on the
        inverter. Stamp the release clock as if we had only just released, and the
        gate must still let go."""
        now = _now(hour=14)
        self.assertIsNone(self._decide(
            -1.0, active=True, released_at=now, now=now,
        ))

    # ── The dwell ──────────────────────────────────────────────────────────
    def test_dwell_blocks_a_re_engage_just_after_a_release(self):
        now = _now(hour=14)
        just_now = now - timedelta(minutes=SOLAR_OVERFLOW_MIN_DWELL_MIN / 2.0)
        self.assertIsNone(self._decide(
            SOLAR_OVERFLOW_ENGAGE_KWH + 5.0, active=False,
            released_at=just_now, now=now,
        ))

    def test_dwell_expires(self):
        now = _now(hour=14)
        old = now - timedelta(minutes=SOLAR_OVERFLOW_MIN_DWELL_MIN + 1.0)
        self.assertIsNotNone(self._decide(
            SOLAR_OVERFLOW_ENGAGE_KWH + 5.0, active=False,
            released_at=old, now=now,
        ))

    def test_no_release_stamp_means_not_blocked(self):
        """The startup case. A plugin that has not released this run must never be
        locked out of exporting — the helper fails open."""
        self.assertIsNotNone(self._decide(
            SOLAR_OVERFLOW_ENGAGE_KWH + 5.0, active=False, released_at=None,
        ))

    def test_a_naive_release_stamp_does_not_raise(self):
        """A datetime that lost its tzinfo would otherwise blow up the subtraction and
        take the whole decision down. Treated as UTC, so it still blocks."""
        now = _now(hour=14)
        naive = (now - timedelta(minutes=1)).replace(tzinfo=None)
        self.assertIsNone(self._decide(
            SOLAR_OVERFLOW_ENGAGE_KWH + 5.0, active=False,
            released_at=naive, now=now,
        ))

    def test_a_future_release_stamp_does_not_block_forever(self):
        """A clock step must not strand the export for the rest of the day."""
        now = _now(hour=14)
        future = now + timedelta(hours=3)
        self.assertIsNotNone(self._decide(
            SOLAR_OVERFLOW_ENGAGE_KWH + 5.0, active=False,
            released_at=future, now=now,
        ))

    def test_a_junk_release_stamp_fails_open(self):
        self.assertIsNotNone(self._decide(
            SOLAR_OVERFLOW_ENGAGE_KWH + 5.0, active=False, released_at="not a datetime",
        ))

    # ── The shared physics number ──────────────────────────────────────────
    def test_gate_and_audit_read_the_same_surplus(self):
        """One definition, so the log can never quote a figure the decision was not
        made on — the v5.55.3 duplication lesson, applied up front."""
        mgr = BatteryManager()
        for wanted in (-2.0, 0.0, 0.4, 3.5):
            with self.subTest(surplus=wanted):
                self.assertAlmostEqual(
                    mgr._overflow_physics_surplus(self._snap(), self._bal(wanted)),
                    wanted, places=6,
                )

    def test_defaults(self):
        self.assertEqual(SOLAR_OVERFLOW_ENGAGE_KWH, 1.0)
        self.assertEqual(SOLAR_OVERFLOW_RELEASE_KWH, 0.0)
        self.assertEqual(SOLAR_OVERFLOW_MIN_DWELL_MIN, 10.0)
        self.assertGreater(SOLAR_OVERFLOW_ENGAGE_KWH, SOLAR_OVERFLOW_RELEASE_KWH)


# Provide time module alias so the Modbus test above can grab a baseline timestamp
import time as time_module


class TestControlPathUsesPerDayBandFactor(unittest.TestCase):
    """v5.65.0 — the control path takes the per-day BAND factor, not the global scalar.

    `_calculate_24h_balance` corrected its remaining-solar sum with `biasFactor`,
    the overall kWh-weighted scalar that openmeteo_forecast.py's own source calls
    "display only", while corrected_today_kwh / corrected_tomorrow_kwh in the SAME
    balance are built from the per-day band. One energy balance, two scales.

    It is not cosmetic: the term feeds the overflow physics gate, surplus_kwh,
    battery_at_dusk / battery_at_dawn (hence import_needed) and the post-power-cut
    early release. Live bands on 12-Aug-2026 put the 30 kWh band at 1.199 against
    a global 0.885 — 35% apart on exactly the marginal days where the 1.0 kWh
    overflow threshold sits, and within 3% on a bright day, which is why a sunny
    afternoon spot check showed nothing wrong.
    """

    def setUp(self):
        self.bm = BatteryManager()

    def _remaining(self, **kw):
        """remaining_solar_kwh for a daytime snapshot with real buckets to sum.

        is_daytime needs TODAY's dawn present as well as the P50 data, and the
        P50 keys are local "YYYY-MM-DD HH:MM:SS" strings — the same shape
        _make_sunny_p50 produces. A hand-rolled "HH:00" dict summed to zero and
        the guard assertion below caught it.
        """
        today_str    = _today_str()
        tomorrow_str = (datetime.now(timezone.utc).date()
                        + timedelta(days=1)).strftime("%Y-%m-%d")
        snap = _make_snapshot(
            now_hour=12,
            forecast_p50=_make_sunny_p50(),
            dawn_times={today_str: _now(hour=7), tomorrow_str: _tomorrow_dawn(hour=7)},
            **kw)
        return self.bm._calculate_24h_balance(snap).remaining_solar_kwh

    def test_remaining_solar_scales_by_the_per_day_band(self):
        base   = self._remaining(bias_factor=1.0, bias_factor_today=1.0)
        banded = self._remaining(bias_factor=0.5, bias_factor_today=2.0)

        self.assertGreater(base, 0.0, "fixture must produce solar to scale")
        self.assertAlmostEqual(
            banded, base * 2.0, places=3,
            msg="must scale by the per-day band (2.0), not the display-only "
                "global scalar (0.5)")

    def test_display_only_scalar_cannot_move_a_control_decision(self):
        a = self._remaining(bias_factor=0.5, bias_factor_today=1.0)
        b = self._remaining(bias_factor=1.5, bias_factor_today=1.0)

        self.assertAlmostEqual(a, b, places=6,
            msg="biasFactor is display-only — changing it must not move the engine")
class TestSolarOverflowBankFirst(unittest.TestCase):
    """v3.11: on a day too small to fill the export cable, bank before selling.

    The gate this replaces read a FORECAST and engaged on a 1.0 kWh margin. Over the
    plugin's own 132 accuracy records that forecast has a mean absolute error of
    7.54 kWh and over-predicts on 82 of 132 days, so the margin was about a seventh
    of the noise, biased the wrong way. Live on 31-Aug-2026 it started an export at
    56.9% SOC on a claimed 10.4 kWh surplus, held the battery at a 967W charge cap
    for five and a half hours, and released with the claim at 0.0 kWh and the battery
    at 69.5%.

    The whole design rests on the hold being ENGAGE-ONLY. It may delay a start; it
    may never stand a running export down. That is what keeps v3.10's stability
    argument intact, and several tests below exist purely to pin it.
    """

    CAP = 35.04

    def _snap(self, soc_pct=56.9, active=False, released_at=None, now=None,
              raw_today=35.6, bank_max=SOLAR_OVERFLOW_BANK_FIRST_MAX_KWH,
              bank_soc=SOLAR_OVERFLOW_BANK_FIRST_SOC_PCT, latched=False,
              storm=False, target_pct=SOLAR_OVERFLOW_TARGET_SOC_PCT,
              export_enabled=True, pv_watts=8500):
        return ManagerSnapshot(
            current_soc_pct                   = soc_pct,
            capacity_kwh                      = self.CAP,
            export_enabled                    = export_enabled,
            max_export_kw                     = 4.0,
            pv_watts                          = pv_watts,
            house_load_watts                  = 750,
            solar_overflow_active             = active,
            solar_overflow_released_at        = released_at,
            solar_overflow_target_pct         = target_pct,
            storm_active                      = storm,
            raw_today_kwh                     = raw_today,
            bank_first_small_latched          = latched,
            solar_overflow_bank_first_max_kwh = bank_max,
            solar_overflow_bank_first_soc     = bank_soc,
            now                               = now or _now(hour=8),
        )

    def _bal(self, surplus_kwh, soc_pct=56.9, surplus_24h=36.0):
        """A balance whose PHYSICS surplus is exactly surplus_kwh.

        Derived from the gate's own definition, exactly as the v3.10 harness does, so
        a change to the headroom term cannot leave these tests quietly measuring
        something else.
        """
        headroom = (100.0 - soc_pct) / 100.0 * self.CAP
        home     = 6.6
        return SufficiencyBalance(
            battery_kwh                = soc_pct / 100.0 * self.CAP,
            remaining_solar_kwh        = surplus_kwh + headroom + home,
            remaining_home_to_dusk_kwh = home,
            is_daytime                 = True,
            hours_to_dusk              = 7.0,
            surplus_kwh                = surplus_24h,
        )

    def _decide(self, surplus_kwh=10.4, surplus_24h=36.0, **kw):
        soc = kw.pop("soc_pct", 56.9)
        snap = self._snap(soc_pct=soc, **kw)
        return BatteryManager()._check_solar_overflow(
            snap, self._bal(surplus_kwh, soc_pct=soc, surplus_24h=surplus_24h))

    def _evaluate(self, surplus_kwh=10.4, surplus_24h=36.0, **kw):
        """Drive evaluate()'s audit path with a stubbed balance, so the audit tests
        read the same gate order the control path took rather than a re-derivation."""
        soc  = kw.pop("soc_pct", 56.9)
        snap = self._snap(soc_pct=soc, **kw)
        bal  = self._bal(surplus_kwh, soc_pct=soc, surplus_24h=surplus_24h)
        mgr  = BatteryManager()
        mgr._calculate_24h_balance = lambda _s: bal
        return mgr.evaluate(snap)

    @staticmethod
    def _audit(decision, tag="OVERFLOW"):
        return " ".join(m for t, m in (decision.audit_trail or []) if t == tag)

    # ── The live failure, pinned ───────────────────────────────────────────
    def test_the_31_aug_morning_does_not_start_an_export(self):
        """08:01 on 31-Aug-2026: SOC 56.9%, forecast 35.6 kWh, claimed surplus
        +10.4 kWh. On v5.78.1 this returned a Decision capping charge at 967W and
        exporting 1.59 kW. It must now return None."""
        self.assertIsNone(self._decide(10.4, soc_pct=56.9, raw_today=35.6))

    def test_the_same_morning_on_a_big_day_still_exports(self):
        """Same numbers, a day the cable can actually be filled — unchanged."""
        d = self._decide(10.4, soc_pct=56.9, raw_today=55.0)
        self.assertIsNotNone(d)
        self.assertGreater(d.export_kw, 0.0)

    # ── The audit must name the gate that actually refused ─────────────────
    def test_the_audit_names_the_bank_first_hold(self):
        d = self._evaluate(10.4, soc_pct=56.9, raw_today=35.6)
        self.assertIn("banking first", self._audit(d))
        self.assertTrue(d.bank_first_holding)
        self.assertEqual(d.bank_first_gate_pct, SOLAR_OVERFLOW_BANK_FIRST_SOC_PCT)

    def test_a_dull_day_audit_names_the_physics_gate_not_bank_first(self):
        """A day turned down because the sun cannot fill the battery must not be
        reported as banking — from October the forecast is under the threshold on
        nearly every day, so a wrong attribution here would be the line the owner
        reads every day of the season this exists for."""
        d = self._evaluate(-25.0, soc_pct=45.0, raw_today=8.2)
        audit = self._audit(d)
        self.assertIn("physics surplus", audit)
        self.assertNotIn("banking first", audit)
        self.assertFalse(d.bank_first_holding)

    def test_a_thin_24h_surplus_audit_names_the_24h_gate(self):
        """Pre-existing mislabel: the old block quoted a physics surplus whatever had
        refused, so a 24h-surplus refusal read as a physics verdict."""
        d = self._evaluate(10.4, surplus_24h=MIN_EXPORT_KWH - 0.1, raw_today=35.6)
        audit = self._audit(d)
        self.assertIn("24h surplus", audit)
        self.assertNotIn("physics surplus", audit)

    def test_export_not_enabled_is_not_a_bank_first_hold(self):
        d = self._evaluate(10.4, soc_pct=50.0, raw_today=20.0, export_enabled=False)
        self.assertIn("export not enabled", self._audit(d))
        self.assertFalse(d.bank_first_holding)

    # ── No-op proofs: off, big days, above the gate ────────────────────────
    def test_the_feature_is_off_by_default_in_the_manager(self):
        """The 0.0 default is load-bearing — the two existing overflow harnesses
        build a snapshot without these fields, and a non-zero default here would
        silently re-target 25 passing tests."""
        snap = ManagerSnapshot(
            current_soc_pct  = 56.9,
            capacity_kwh     = self.CAP,
            export_enabled   = True,
            max_export_kw    = 4.0,
            pv_watts         = 8500,
            house_load_watts = 750,
            now              = _now(hour=8),
        )
        self.assertEqual(snap.solar_overflow_bank_first_max_kwh, 0.0)
        self.assertIsNotNone(BatteryManager()._check_solar_overflow(
            snap, self._bal(10.4, soc_pct=56.9)))

    def test_a_big_day_is_untouched(self):
        on  = self._decide(10.4, raw_today=55.0, bank_max=40.0)
        off = self._decide(10.4, raw_today=55.0, bank_max=0.0)
        self.assertIsNotNone(on)
        self.assertIsNotNone(off)
        self.assertEqual(on.export_kw, off.export_kw)
        self.assertEqual(on.power_watts, off.power_watts)

    def test_above_the_gate_behaviour_is_unchanged(self):
        on  = self._decide(10.4, soc_pct=96.0, raw_today=30.0, bank_max=40.0)
        off = self._decide(10.4, soc_pct=96.0, raw_today=30.0, bank_max=0.0)
        self.assertIsNotNone(on)
        self.assertEqual(on.export_kw, off.export_kw)
        self.assertEqual(on.power_watts, off.power_watts)

    def test_a_zero_soc_gate_is_todays_behaviour(self):
        on  = self._decide(10.4, bank_soc=0.0)
        off = self._decide(10.4, bank_max=0.0)
        self.assertIsNotNone(on)
        self.assertEqual(on.export_kw, off.export_kw)

    def test_storm_is_exempt(self):
        """A storm owns the target and already suppresses export below its release
        SOC. Exempting makes non-regression provable rather than argued."""
        d = self._decide(10.4, soc_pct=56.9, raw_today=30.0, storm=True)
        self.assertIsNotNone(d)
        self.assertIn("to 100% target", d.reason)
        self.assertIn("(storm)", d.reason)

    def test_pacing_target_at_95_does_not_change_behaviour_above_the_gate(self):
        a = self._decide(10.4, soc_pct=96.0, raw_today=30.0, target_pct=95.0)
        b = self._decide(10.4, soc_pct=96.0, raw_today=30.0, target_pct=90.0)
        self.assertIsNotNone(a)
        self.assertEqual(a.export_kw, b.export_kw)
        self.assertEqual(a.power_watts, b.power_watts)

    # ── Boundaries and clamps ──────────────────────────────────────────────
    def test_the_threshold_boundary(self):
        """40.0 exactly is a BIG day — the prose says "below 40" and the code must
        agree, or the two drift apart the first time somebody reads one of them."""
        self.assertIsNone(self._decide(10.4, raw_today=39.9))
        self.assertIsNotNone(self._decide(10.4, raw_today=40.0))
        self.assertIsNotNone(self._decide(10.4, raw_today=40.1))

    def test_the_soc_boundary(self):
        self.assertIsNone(self._decide(10.4, soc_pct=94.9, raw_today=30.0))
        self.assertIsNotNone(self._decide(10.4, soc_pct=95.0, raw_today=30.0))

    def test_a_soc_gate_above_the_clamp_is_clamped(self):
        """A typo of 950 must not become a permanent export ban."""
        for typo in (100.0, 950.0):
            with self.subTest(bank_soc=typo):
                self.assertIsNotNone(self._decide(
                    10.4, soc_pct=SOLAR_OVERFLOW_BANK_FIRST_SOC_MAX + 0.5,
                    raw_today=30.0, bank_soc=typo))

    def test_an_absurd_threshold_is_clamped(self):
        """Read from the constant, not a literal: if the clamp moves, this test must
        move with it rather than quietly testing a number nobody uses any more."""
        under = SOLAR_OVERFLOW_BANK_FIRST_KWH_MAX - 1.0
        over  = SOLAR_OVERFLOW_BANK_FIRST_KWH_MAX + 1.0
        self.assertIsNone(self._decide(10.4, raw_today=under, bank_max=10000.0))
        self.assertIsNotNone(self._decide(10.4, raw_today=over, bank_max=10000.0))

    def test_an_unreadable_forecast_falls_back_to_todays_behaviour(self):
        self.assertFalse(BatteryManager._overflow_bank_first_blocked(
            self._snap(raw_today=None)))

    # ── Engage-only: it may delay a start, never end a run ─────────────────
    def test_it_only_ever_delays_never_releases(self):
        d = self._decide(SOLAR_OVERFLOW_RELEASE_KWH + 0.5, soc_pct=40.0,
                         raw_today=20.0, active=True)
        self.assertIsNotNone(d, "a running export must not be stood down by the hold")

    def test_a_forecast_crossing_the_threshold_cannot_release(self):
        for raw in (55.0, 20.0):
            with self.subTest(raw_today=raw):
                self.assertIsNotNone(self._decide(
                    SOLAR_OVERFLOW_RELEASE_KWH + 0.5, soc_pct=40.0,
                    raw_today=raw, active=True))

    def test_the_small_day_latch_survives_a_high_sample(self):
        """Once a complete forecast has read the day small, one high sample must not
        lift the hold. The latch is one-way in the safe direction."""
        self.assertIsNone(self._decide(10.4, raw_today=45.0, latched=True))

    def test_the_latch_cannot_make_a_big_day_export_early(self):
        """Sanity on the latch's direction: it can only ever say SMALL."""
        self.assertIsNotNone(self._decide(10.4, raw_today=45.0, latched=False))

    # ── Composition with the neighbouring gates ────────────────────────────
    def test_dwell_and_bank_first_compose(self):
        now = _now(hour=14)
        blocked = self._decide(10.4, soc_pct=96.0, raw_today=30.0, now=now,
                               released_at=now - timedelta(minutes=5))
        self.assertIsNone(blocked, "the dwell still blocks a re-engage")
        clear = self._decide(10.4, soc_pct=96.0, raw_today=30.0, now=now,
                             released_at=now - timedelta(
                                 minutes=SOLAR_OVERFLOW_MIN_DWELL_MIN + 1))
        self.assertIsNotNone(clear)

    def test_the_dwell_is_named_ahead_of_bank_first(self):
        """Both gates can block at once. The audit must name the one the control path
        hit first, or the reader chases the wrong cause."""
        now = _now(hour=14)
        d = self._evaluate(10.4, soc_pct=50.0, raw_today=30.0, now=now,
                           released_at=now - timedelta(minutes=5))
        self.assertIn("dwell", self._audit(d))
        self.assertNotIn("banking first", self._audit(d))

    # ── The gate and the audit must never drift apart ──────────────────────
    def test_the_gate_and_the_audit_agree(self):
        """Two readers of one gate order is how the v3.9 drift happened. Whenever the
        control path refuses because of the hold, the audit must say so — and
        whenever it does not, the audit must not."""
        mgr = BatteryManager()
        for soc in (40.0, 56.9, 94.9, 95.0, 99.0):
            for raw in (8.2, 30.0, 39.9, 40.0, 55.0):
                for surplus in (-25.0, 10.4):
                    for s24 in (MIN_EXPORT_KWH - 0.1, 36.0):
                        with self.subTest(soc=soc, raw=raw, surplus=surplus, s24=s24):
                            snap = self._snap(soc_pct=soc, raw_today=raw)
                            bal  = self._bal(surplus, soc_pct=soc, surplus_24h=s24)
                            tag, _ = mgr._overflow_skip_reason(snap, bal)
                            blocked_by_bank_first = (
                                mgr._check_solar_overflow(snap, bal) is None
                                and mgr._overflow_bank_first_blocked(snap)
                                and surplus >= SOLAR_OVERFLOW_ENGAGE_KWH
                                and s24 >= MIN_EXPORT_KWH
                            )
                            self.assertEqual(tag == "bank_first", blocked_by_bank_first)

    def test_no_refusal_is_ever_unexplained(self):
        """The skip reason has an "unknown" branch as a backstop. If it ever fires,
        the gate order in the audit has drifted from the gate order in the control
        path — so pin that it does not."""
        mgr = BatteryManager()
        for soc in (40.0, 95.0):
            for raw in (30.0, 55.0):
                for surplus in (-25.0, 0.5, 10.4):
                    snap = self._snap(soc_pct=soc, raw_today=raw)
                    bal  = self._bal(surplus, soc_pct=soc)
                    if mgr._check_solar_overflow(snap, bal) is None:
                        tag, _ = mgr._overflow_skip_reason(snap, bal)
                        self.assertNotEqual(tag, "unknown",
                                            f"unexplained refusal at soc={soc} "
                                            f"raw={raw} surplus={surplus}")


class TestSavingSessionExportableKwh(unittest.TestCase):
    """The arithmetic behind CliveS's rule (03-Sep-2026): run the session burst as
    long as it does not spend energy Axle needs, and the house still reaches next
    morning without importing."""

    def _kwh(self, **kw):
        args = dict(battery_at_dawn_kwh=20.0, reserve_pct=15.0, capacity_kwh=35.04,
                    planned_vpp_kwh=0.0, export_cap_kw=4.0, window_hours=1.0,
                    import_needed=False)
        args.update(kw)
        return saving_session_exportable_kwh(**args)

    def test_headroom_above_the_reserve_is_exportable(self):
        # 20 kWh at dawn, 15% of 35.04 = 5.26 kWh reserve -> ~14.7 spare, capped by
        # the DNO window (4 kW x 1 h = 4 kWh).
        self.assertEqual(self._kwh(), 4.0)

    def test_capped_by_the_export_window_not_the_battery(self):
        self.assertEqual(self._kwh(battery_at_dawn_kwh=30.0), 4.0)
        self.assertEqual(self._kwh(battery_at_dawn_kwh=30.0, window_hours=2.0), 8.0)

    def test_axle_energy_is_held_back(self):
        # 8 kWh at dawn, 5.26 reserve -> 2.74 spare; 2.0 of it promised to Axle
        # leaves 0.74, under the minimum, so it stands down entirely.
        self.assertEqual(self._kwh(battery_at_dawn_kwh=8.0), 2.74)
        self.assertEqual(self._kwh(battery_at_dawn_kwh=8.0, planned_vpp_kwh=2.0), 0.0)

    def test_import_needed_refuses_outright(self):
        # The engine's own verdict wins over any amount of apparent headroom.
        self.assertEqual(self._kwh(battery_at_dawn_kwh=30.0, import_needed=True), 0.0)

    def test_below_the_reserve_exports_nothing(self):
        self.assertEqual(self._kwh(battery_at_dawn_kwh=4.0), 0.0)

    def test_a_trivial_amount_is_not_worth_a_mode_switch(self):
        # 5.26 reserve + 1.0 minimum -> 6.0 at dawn leaves 0.74, under the floor.
        self.assertEqual(self._kwh(battery_at_dawn_kwh=6.0), 0.0)

    def test_unknown_inputs_never_guess(self):
        self.assertEqual(self._kwh(battery_at_dawn_kwh=None), 0.0)
        self.assertEqual(self._kwh(capacity_kwh=0), 0.0)
        self.assertEqual(self._kwh(export_cap_kw="nonsense"), 0.0)


class TestSavingSessionOverride(unittest.TestCase):
    """Placement matters more than the arithmetic: Axle pays ~£1/kWh against
    ~7.6p/kWh here, so an Axle window must always win."""

    def setUp(self):
        self.mgr = BatteryManager()

    def test_axle_wins_when_both_are_live(self):
        snap = _make_snapshot(soc_pct=95.0, export_enabled=True,
                              vpp_active=True, saving_session_active=True,
                              now_hour=20)
        self.assertEqual(self.mgr.evaluate(snap).action, ACTION_VPP_EXPORT)

    def test_session_drives_export_when_axle_is_idle(self):
        # weekend_kwh is pinned to the weekday figure ON PURPOSE. _make_snapshot
        # builds `now` from the REAL date, and evaluate() picks tomorrow's need
        # by tomorrow's weekday — so with the 22/30 defaults this test asserted
        # the session branch Mon-Thu and the sufficiency refusal Fri-Sat, and it
        # was red two days in seven from 03-Sep-2026 until anyone ran it on a
        # Friday. The subject here is the branch, not the sufficiency guard,
        # which test_a_flat_battery_refuses_and_says_why already covers.
        snap = _make_snapshot(soc_pct=95.0, export_enabled=True,
                              saving_session_active=True, now_hour=20,
                              weekday_kwh=22.0, weekend_kwh=22.0)
        self.assertEqual(self.mgr.evaluate(snap).action, ACTION_SAVING_SESSION)

    def test_export_disabled_stands_the_session_down(self):
        # Post-power-cut lockout / storm. Same precedence as the VPP branch.
        snap = _make_snapshot(soc_pct=95.0, export_enabled=False,
                              saving_session_active=True, now_hour=20)
        d = self.mgr.evaluate(snap)
        self.assertEqual(d.action, ACTION_SELF_CONSUMPTION)
        self.assertIn("export currently disabled", d.reason)

    def test_a_flat_battery_refuses_and_says_why(self):
        snap = _make_snapshot(soc_pct=12.0, export_enabled=True,
                              saving_session_active=True, now_hour=20)
        d = self.mgr.evaluate(snap)
        self.assertEqual(d.action, ACTION_SELF_CONSUMPTION)
        self.assertIn("Saving Session window, but not exporting", d.reason)

    def test_inactive_session_changes_nothing(self):
        # The negative control: without the flag the branch must be invisible.
        snap = _make_snapshot(soc_pct=95.0, export_enabled=True, now_hour=20)
        self.assertNotEqual(self.mgr.evaluate(snap).action, ACTION_SAVING_SESSION)



class TestHappyHourImportKwh(unittest.TestCase):
    """Three ceilings: headroom to target, the window, and Octopus's fair-use cap."""

    def _kwh(self, **kw):
        args = dict(soc_pct=50.0, capacity_kwh=35.04, target_soc_pct=93.0,
                    charge_kw=10.0, window_hours=1.0)
        args.update(kw)
        return happy_hour_import_kwh(**args)

    def test_window_is_the_binding_ceiling_when_the_battery_is_low(self):
        # 50% -> 93% of 35.04 = 15.07 kWh of headroom, but only 10 kWh fits in an hour.
        self.assertEqual(self._kwh(), 10.0)

    def test_headroom_binds_when_the_battery_is_nearly_full(self):
        # 88% -> 93% = 1.75 kWh, well under the 10 kWh the window allows.
        self.assertEqual(self._kwh(soc_pct=88.0), 1.75)

    def test_at_or_above_target_imports_nothing(self):
        self.assertEqual(self._kwh(soc_pct=93.0), 0.0)
        self.assertEqual(self._kwh(soc_pct=97.0), 0.0)

    def test_a_trivial_amount_is_not_worth_the_mode_switch(self):
        # 92.9% -> 93% is 0.035 kWh.
        self.assertEqual(self._kwh(soc_pct=92.9), 0.0)

    def test_fair_use_cap_cannot_bind_in_one_hour_but_is_honoured(self):
        # Unreachable at 10 kW x 1 h, so it must not interfere...
        self.assertEqual(self._kwh(soc_pct=10.0), 10.0)
        # ...but a longer window or a bigger inverter must still respect it.
        self.assertEqual(self._kwh(soc_pct=10.0, window_hours=3.0), 16.0)
        self.assertEqual(self._kwh(soc_pct=10.0, window_hours=3.0, fair_use_cap_kwh=5.0), 5.0)

    def test_it_fills_to_target_not_to_full(self):
        # The standing rule is to stop short of 100% to protect the pack; free
        # electricity is not a reason to override it.
        at_target = self._kwh(soc_pct=80.0, target_soc_pct=93.0)
        to_full   = self._kwh(soc_pct=80.0, target_soc_pct=100.0)
        self.assertLess(at_target, to_full)
        self.assertEqual(at_target, 4.56)

    def test_unknown_inputs_never_guess(self):
        self.assertEqual(self._kwh(soc_pct=None), 0.0)
        self.assertEqual(self._kwh(capacity_kwh=0), 0.0)
        self.assertEqual(self._kwh(charge_kw="nonsense"), 0.0)


class TestHappyHourOverride(unittest.TestCase):
    def setUp(self):
        self.mgr = BatteryManager()

    def test_it_imports_when_a_booked_hour_is_live(self):
        snap = _make_snapshot(soc_pct=55.0, happy_hour_active=True, now_hour=12)
        d = self.mgr.evaluate(snap)
        self.assertEqual(d.action, ACTION_HAPPY_HOUR_IMPORT)
        self.assertEqual(d.power_watts, 10000)

    def test_axle_still_wins(self):
        snap = _make_snapshot(soc_pct=55.0, happy_hour_active=True,
                              vpp_active=True, export_enabled=True, now_hour=12)
        self.assertEqual(self.mgr.evaluate(snap).action, ACTION_VPP_EXPORT)

    def test_both_directions_live_fails_closed(self):
        # A turn-down (export) and a happy hour (import) cannot both be right.
        snap = _make_snapshot(soc_pct=55.0, happy_hour_active=True,
                              saving_session_active=True, export_enabled=True, now_hour=12)
        d = self.mgr.evaluate(snap)
        self.assertEqual(d.action, ACTION_SELF_CONSUMPTION)
        self.assertIn("refusing to guess", d.reason)

    def test_full_battery_declines_and_explains_itself(self):
        snap = _make_snapshot(soc_pct=95.0, happy_hour_active=True, now_hour=12)
        d = self.mgr.evaluate(snap)
        self.assertEqual(d.action, ACTION_SELF_CONSUMPTION)
        self.assertIn("nothing worth importing", d.reason)

    def test_no_export_gate_applies(self):
        # Unlike the turn-down branch, this IMPORTS — a power-cut lockout or storm
        # export suppression is irrelevant to it, and a storm wants a full battery.
        snap = _make_snapshot(soc_pct=55.0, happy_hour_active=True,
                              export_enabled=False, now_hour=12)
        self.assertEqual(self.mgr.evaluate(snap).action, ACTION_HAPPY_HOUR_IMPORT)

    def test_inactive_changes_nothing(self):
        snap = _make_snapshot(soc_pct=55.0, now_hour=12)
        self.assertNotEqual(self.mgr.evaluate(snap).action, ACTION_HAPPY_HOUR_IMPORT)


if __name__ == "__main__":
    print(f"Running {globals().get('PLUGIN_NAME', 'SigenEnergyManager')} battery_manager tests")
    unittest.main(verbosity=2)
