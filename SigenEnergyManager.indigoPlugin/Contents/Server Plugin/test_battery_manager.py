#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    test_battery_manager.py
# Description: Unit tests for battery_manager.py decision engine
#              Runs without Indigo installed
# Author:      CliveS & Claude Sonnet 4.6
# Date:        27-03-2026 22:11 GMT
# Version:     1.2

import sys
import unittest
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

# Allow running without Indigo (battery_manager imports octopus_api constants via try/except)
from battery_manager import (
    BatteryManager,
    ManagerSnapshot,
    TariffData,
    Decision,
    SufficiencyBalance,
    ACTION_SELF_CONSUMPTION,
    ACTION_START_IMPORT,
    ACTION_SCHEDULE_IMPORT,
    ACTION_START_EXPORT,
    ACTION_STOP_EXPORT,
    TARIFF_TRACKER,
    TARIFF_GO,
    TARIFF_FLUX,
    TRACKER_DEFER_THRESHOLD,
    FLOOD_PREV_SOC_THRESHOLD_PCT,
    FLOOD_PREV_TARGET_PCT,
    FLOOD_PREV_FORECAST_MULT,
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
    try:
        import pytz
        _tz_l = pytz.timezone("Europe/London")
        return datetime.now(timezone.utc).astimezone(_tz_l).date().strftime("%Y-%m-%d")
    except ImportError:
        return datetime.now(timezone.utc).date().strftime("%Y-%m-%d")

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
    corrected_tomorrow_kwh=0.0,
    flood_prev_target_soc=0.0,
    dawn_target_pct=DAWN_TARGET,
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
        current_soc_pct        = soc_pct,
        capacity_kwh           = CAPACITY_KWH,
        efficiency             = EFFICIENCY,
        dawn_target_pct        = dawn_target_pct,
        health_cutoff_pct      = HEALTH_FLOOR,
        export_enabled         = export_enabled,
        max_export_kw          = max_export_kw,
        pv_watts               = pv_watts,
        export_active          = export_active,
        corrected_tomorrow_kwh = corrected_tomorrow_kwh,
        flood_prev_target_soc  = flood_prev_target_soc,
        tariff                 = tariff,
        forecast_p50           = forecast_p50 or {},
        dawn_times             = dawn_times,
        consumption_profile    = consumption_profile,
        now                    = _now(hour=now_hour),
        vpp_active             = vpp_active,
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
        self.assertEqual(decision.scheduled_time.hour, 0)   # cheap window start hour

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

        # Dawn not viable and cannot wait -> import now
        self.assertIn(decision.action, (ACTION_START_IMPORT, ACTION_SCHEDULE_IMPORT))
        # If scheduled, must be tonight's cheap window
        if decision.action == ACTION_SCHEDULE_IMPORT:
            self.assertIsNotNone(decision.scheduled_time)

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


class TestVppSuppression(unittest.TestCase):
    """Tests that VPP active suppresses all manager decisions."""

    def setUp(self):
        self.bm = BatteryManager()

    def test_vpp_active_returns_self_consumption(self):
        """When VPP is active, always return self-consumption regardless of SOC."""
        snapshot = _make_snapshot(
            soc_pct    = 5.0,   # would normally trigger import
            vpp_active = True,
            now_hour   = 20,
        )
        decision = self.bm.evaluate(snapshot)

        self.assertEqual(decision.action, ACTION_SELF_CONSUMPTION)
        self.assertIn("VPP", decision.reason)


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

    def test_legacy_export_active_stopped_immediately(self):
        """v3.x export_active=True with no flood_prev context → ACTION_STOP_EXPORT."""
        today_str    = datetime.now(timezone.utc).date().strftime("%Y-%m-%d")
        tomorrow_str = (datetime.now(timezone.utc).date() + timedelta(days=1)).strftime("%Y-%m-%d")
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

        self.assertEqual(decision.action, ACTION_STOP_EXPORT)

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

    Constants: threshold=55%, target=40%, forecast_mult=2.0x
    Default daily need from consumption_profile [0.30]*48 = 14.4 kWh/day.
    Flood prevention fires when: tomorrow_solar >= 2 * 14.4 = 28.8 kWh.
    sunny_tomorrow = 60.0 kWh  (well above 2x threshold)
    poor_tomorrow  = 20.0 kWh  (below 2x threshold)
    """

    def setUp(self):
        self.bm = BatteryManager()
        self.sunny_tomorrow   = 60.0   # 60 >= 28.8 — triggers flood prevention
        self.poor_tomorrow    = 20.0   # 20 <  28.8 — blocked

    # ── Trigger conditions ─────────────────────────────────────────────────────

    def test_flood_prev_triggers_when_all_conditions_met(self):
        """High SOC + sunny tomorrow forecast at night + export enabled → pre-drain."""
        snapshot = _make_snapshot(
            soc_pct                = 70.0,
            export_enabled         = True,
            corrected_tomorrow_kwh = self.sunny_tomorrow,
            now_hour               = 22,
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
        """Tomorrow forecast < 2x daily need → don't pre-drain (risk of reimport)."""
        snapshot = _make_snapshot(
            soc_pct                = 70.0,
            export_enabled         = True,
            corrected_tomorrow_kwh = self.poor_tomorrow,
            now_hour               = 22,
        )
        decision = self.bm.evaluate(snapshot)

        self.assertNotEqual(decision.action, ACTION_START_EXPORT)

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
        )
        decision = self.bm.evaluate(snapshot)

        self.assertEqual(decision.action, ACTION_START_EXPORT)
        self.assertEqual(decision.power_watts, 3600)

    # ── Continue / stop logic ─────────────────────────────────────────────────

    def test_flood_prev_continues_when_active_and_above_target(self):
        """Flood prevention running (export_active=True, flood_prev_target_soc=40)
        and SOC still above target → continue (ACTION_START_EXPORT, idempotent)."""
        snapshot = _make_snapshot(
            soc_pct                = 55.0,   # above 40% target
            export_enabled         = True,
            export_active          = True,   # export already running
            flood_prev_target_soc  = 40.0,
            corrected_tomorrow_kwh = self.sunny_tomorrow,
            now_hour               = 23,
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

    def test_legacy_export_still_stopped_immediately(self):
        """export_active=True but flood_prev_target_soc=0 → legacy v3.x export → stop."""
        snapshot = _make_snapshot(
            soc_pct                = 80.0,
            export_enabled         = True,
            export_active          = True,
            flood_prev_target_soc  = 0.0,   # no flood prevention context
            corrected_tomorrow_kwh = self.sunny_tomorrow,
            now_hour               = 2,
        )
        decision = self.bm.evaluate(snapshot)

        self.assertEqual(decision.action, ACTION_STOP_EXPORT)


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
        try:
            import pytz
            london = decision.scheduled_time.astimezone(pytz.timezone("Europe/London"))
            self.assertEqual(london.hour, 0)
            self.assertEqual(london.minute, 5)
        except ImportError:
            self.skipTest("pytz not available")


class TestPowerCutLockoutParsing(unittest.TestCase):
    """Tests for defensive parsing of pluginPrefs powerRestoredTime (v4.5)."""

    def test_isoformat_with_tz_parses_cleanly(self):
        """The normal case: a tz-aware ISO timestamp parses back to UTC."""
        from datetime import datetime, timezone
        original = datetime(2026, 4, 30, 6, 4, 42, tzinfo=timezone.utc)
        s        = original.isoformat()
        parsed   = datetime.fromisoformat(s)
        self.assertEqual(parsed, original)
        self.assertIsNotNone(parsed.tzinfo)

    def test_naive_isoformat_can_be_recovered(self):
        """A hand-edited naive timestamp must be recoverable as UTC."""
        from datetime import datetime, timezone
        s      = "2026-04-30T06:04:42"   # naive
        parsed = datetime.fromisoformat(s)
        self.assertIsNone(parsed.tzinfo)
        # plugin.py promotes naive to UTC; ensure that doesn't crash subtraction
        promoted = parsed.replace(tzinfo=timezone.utc)
        delta_h  = (datetime.now(timezone.utc) - promoted).total_seconds() / 3600.0
        self.assertIsInstance(delta_h, float)

    def test_garbage_string_raises_valueerror(self):
        """A corrupt timestamp raises ValueError (caught and cleared by plugin)."""
        from datetime import datetime
        with self.assertRaises(ValueError):
            datetime.fromisoformat("not a timestamp")


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


# Provide time module alias so the Modbus test above can grab a baseline timestamp
import time as time_module


if __name__ == "__main__":
    print(f"Running {PLUGIN_NAME if 'PLUGIN_NAME' in dir() else 'SigenEnergyManager'} battery_manager tests")
    unittest.main(verbosity=2)
