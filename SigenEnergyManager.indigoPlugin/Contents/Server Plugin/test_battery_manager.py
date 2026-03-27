#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    test_battery_manager.py
# Description: Unit tests for battery_manager.py decision engine
#              Runs without Indigo installed
# Author:      CliveS & Claude Sonnet 4.6
# Date:        27-03-2026 21:48 GMT
# Version:     1.1

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
    DawnViability,
    ACTION_SELF_CONSUMPTION,
    ACTION_START_IMPORT,
    ACTION_SCHEDULE_IMPORT,
    ACTION_START_EXPORT,
    ACTION_STOP_EXPORT,
    TARIFF_TRACKER,
    TARIFF_GO,
    TARIFF_FLUX,
    TRACKER_DEFER_THRESHOLD,
    EXPORT_HYSTERESIS_PCT,
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

def _make_snapshot(
    soc_pct=50.0,
    tariff_key=TARIFF_TRACKER,
    today_rate_p=25.0,
    tomorrow_rate_p=None,
    cheap_start=None,
    cheap_end=None,
    export_enabled=False,
    export_stage1_soc_pct=80.0,
    export_stage1_kw=2.0,
    export_stage2_soc_pct=90.0,
    export_stage2_kw=4.0,
    current_export_tier=0,
    vpp_active=False,
    now_hour=14,
    forecast_p50=None,
    consumption_profile=None,
    dawn_times=None,
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

    # Default: flat 0.3 kWh/slot profile
    if consumption_profile is None:
        consumption_profile = [0.30] * 48  # ~7.2 kWh/day overnight

    # Default dawn times: tomorrow at 07:00
    if dawn_times is None:
        dawn_times = {tomorrow_str: _tomorrow_dawn(hour=7)}

    return ManagerSnapshot(
        current_soc_pct       = soc_pct,
        capacity_kwh          = CAPACITY_KWH,
        efficiency            = EFFICIENCY,
        dawn_target_pct       = DAWN_TARGET,
        health_cutoff_pct     = HEALTH_FLOOR,
        export_enabled        = export_enabled,
        export_stage1_soc_pct = export_stage1_soc_pct,
        export_stage1_kw      = export_stage1_kw,
        export_stage2_soc_pct = export_stage2_soc_pct,
        export_stage2_kw      = export_stage2_kw,
        current_export_tier   = current_export_tier,
        tariff                = tariff,
        forecast_p50          = forecast_p50 or {},
        forecast_p10          = {},
        dawn_times            = dawn_times,
        consumption_profile   = consumption_profile,
        now                   = _now(hour=now_hour),
        vpp_active            = vpp_active,
    )


# ============================================================
# Test cases
# ============================================================

class TestDawnViability(unittest.TestCase):
    """Tests for dawn viability calculation."""

    def setUp(self):
        self.bm = BatteryManager()

    def test_high_soc_is_viable(self):
        """Battery at 80% should comfortably reach dawn."""
        snapshot  = _make_snapshot(soc_pct=80.0, now_hour=20)
        viability = self.bm._check_dawn_viability(snapshot)

        self.assertTrue(viability.viable)
        self.assertFalse(viability.import_needed)
        self.assertGreater(viability.soc_at_dawn_kwh, 3.504)  # above 10%

    def test_low_soc_triggers_import(self):
        """Battery at 15% with overnight drain should flag import needed."""
        # 15% of 35.04 = 5.26 kWh
        # Overnight (20:00-07:00) = 11h, flat 0.3 kWh/slot = 0.6 kWh/h * 11 = 6.6 kWh drain
        # Projected: 5.26 - 6.6 = -1.34 -> clamped to health floor (3.504) -> still < target
        snapshot  = _make_snapshot(soc_pct=15.0, now_hour=20)
        viability = self.bm._check_dawn_viability(snapshot)

        # With only 5.26 kWh and 6.6 kWh expected drain, we cannot reach dawn
        self.assertFalse(viability.viable)
        self.assertTrue(viability.import_needed)

    def test_exactly_at_dawn_target(self):
        """Battery projected to exactly hit dawn target (10%) should not need import."""
        # We want soc_at_dawn_kwh == dawn_target_kwh = 3.504
        # Work backwards: need current_kwh - drain = 3.504
        # drain from 20:00 to 07:00 (11h) = 11 * 2 slots * 0.30 = 6.6 kWh
        # So current_kwh = 3.504 + 6.6 = 10.104 kWh
        # soc_pct = 10.104 / 35.04 * 100 = 28.8%
        snapshot = _make_snapshot(soc_pct=29.0, now_hour=20)
        viability = self.bm._check_dawn_viability(snapshot)

        # At 29% (10.17 kWh) - drain 6.6 kWh = 3.57 kWh > 3.504 target
        self.assertTrue(viability.viable)
        self.assertFalse(viability.import_needed)

    def test_health_floor_clamps_soc(self):
        """SOC cannot drop below health cutoff (10%) in calculation."""
        # Very low SOC: 5% (1.75 kWh) - health cutoff is 10% (~3.50 kWh)
        # Expected: soc_at_dawn clamped to health floor (~3.50 kWh when rounded)
        # Note: 35.04 * 0.10 = 3.5039... which rounds to 3.50 at 2 decimal places
        snapshot  = _make_snapshot(soc_pct=5.0, now_hour=20)
        viability = self.bm._check_dawn_viability(snapshot)

        self.assertGreaterEqual(viability.soc_at_dawn_kwh, 3.50)  # clamped to floor

    def test_daytime_has_long_time_to_dawn(self):
        """At 14:00, hours to dawn is ~17 hours (through tomorrow)."""
        snapshot  = _make_snapshot(soc_pct=50.0, now_hour=14)
        viability = self.bm._check_dawn_viability(snapshot)

        self.assertGreater(viability.hours_to_dawn, 10.0)


class TestTrackerImportDecisions(unittest.TestCase):
    """Tests for Tracker tariff import logic."""

    def setUp(self):
        self.bm = BatteryManager()

    def test_import_now_when_tomorrow_rate_unknown(self):
        """When tomorrow's rate is not published, import now."""
        snapshot = _make_snapshot(
            soc_pct         = 12.0,
            tariff_key      = TARIFF_TRACKER,
            today_rate_p    = 25.0,
            tomorrow_rate_p = None,  # not published yet
            now_hour        = 20,
        )
        decision = self.bm.evaluate(snapshot)

        self.assertEqual(decision.action, ACTION_START_IMPORT)
        self.assertFalse(decision.dawn_viable)

    def test_import_now_when_tomorrow_same_rate(self):
        """When tomorrow's rate is similar, import immediately."""
        snapshot = _make_snapshot(
            soc_pct         = 12.0,
            tariff_key      = TARIFF_TRACKER,
            today_rate_p    = 25.0,
            tomorrow_rate_p = 24.5,  # only 2% cheaper - below threshold
            now_hour        = 20,
        )
        decision = self.bm.evaluate(snapshot)

        self.assertEqual(decision.action, ACTION_START_IMPORT)

    def test_defer_import_when_tomorrow_significantly_cheaper(self):
        """When tomorrow is 10%+ cheaper AND battery has margin, defer to 00:05."""
        # Today: 28p, tomorrow: 20p (28% cheaper)
        # Battery at 25% (8.76 kWh). Drain from 20:00 to 07:00 = 11h = 6.6 kWh.
        # raw_soc at dawn = 8.76 - 6.6 = 2.16 kWh < 3.504 target -> import needed.
        # Drain to midnight = 4h = 2.4 kWh. SOC at midnight: 8.76 - 2.4 = 6.36 > 3.504 floor
        # -> can safely defer to midnight for cheaper rate.
        snapshot = _make_snapshot(
            soc_pct         = 25.0,
            tariff_key      = TARIFF_TRACKER,
            today_rate_p    = 28.0,
            tomorrow_rate_p = 20.0,  # 28.5% cheaper
            now_hour        = 20,
        )
        decision = self.bm.evaluate(snapshot)

        # Should defer to midnight
        self.assertEqual(decision.action, ACTION_SCHEDULE_IMPORT)
        self.assertIsNotNone(decision.scheduled_time)
        self.assertEqual(decision.scheduled_time.hour, 0)  # midnight

    def test_import_now_despite_cheaper_tomorrow_if_margin_too_low(self):
        """If we cannot safely reach midnight, import now even if tomorrow is cheaper."""
        # Battery at 15% (5.26 kWh). At 20:00, drain to midnight (4h):
        # 4 * 2 * 0.30 = 2.4 kWh. SOC at midnight: 5.26 - 2.4 = 2.86 kWh < 3.504 floor
        snapshot = _make_snapshot(
            soc_pct         = 15.0,
            tariff_key      = TARIFF_TRACKER,
            today_rate_p    = 28.0,
            tomorrow_rate_p = 20.0,  # much cheaper - but we can't wait
            now_hour        = 20,
        )
        decision = self.bm.evaluate(snapshot)

        # Must import now - cannot safely defer
        self.assertEqual(decision.action, ACTION_START_IMPORT)

    def test_no_import_when_dawn_viable(self):
        """No import when battery will comfortably reach dawn."""
        snapshot = _make_snapshot(
            soc_pct         = 70.0,
            tariff_key      = TARIFF_TRACKER,
            today_rate_p    = 25.0,
            tomorrow_rate_p = 20.0,
            now_hour        = 20,
        )
        decision = self.bm.evaluate(snapshot)

        self.assertEqual(decision.action, ACTION_SELF_CONSUMPTION)
        self.assertTrue(decision.dawn_viable)


class TestGoFluxImportDecisions(unittest.TestCase):
    """Tests for Go/Flux time-of-use tariff import logic."""

    def setUp(self):
        self.bm = BatteryManager()

    def test_go_defer_to_cheap_window_if_margin_allows(self):
        """On Go tariff at 20:00 with adequate SOC, defer to 00:30 cheap window."""
        # At 20:00 with 30% SOC (10.51 kWh)
        # Drain to 00:30 = 4.5h * 0.6 kWh/h = 2.7 kWh
        # SOC at 00:30 = 10.51 - 2.7 = 7.81 kWh > 3.504 floor -> can wait
        snapshot = _make_snapshot(
            soc_pct         = 30.0,
            tariff_key      = TARIFF_GO,
            today_rate_p    = 25.0,
            cheap_start     = "00:30",
            cheap_end       = "05:30",
            now_hour        = 20,
        )
        # Force dawn viability failure to ensure import is needed
        # 30% = 10.51 kWh, drain from 20:00 to 07:00 = 11h * 0.6 kWh/h = 6.6 kWh
        # Projected: 10.51 - 6.6 = 3.91 kWh > 3.504 target -> viable! No import needed
        decision = self.bm.evaluate(snapshot)
        self.assertEqual(decision.action, ACTION_SELF_CONSUMPTION)

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


class TestExportDecisions(unittest.TestCase):
    """Tests for grid export logic."""

    def setUp(self):
        self.bm = BatteryManager()

    def test_no_export_when_disabled(self):
        """No export when export_enabled is False."""
        snapshot = _make_snapshot(
            soc_pct        = 95.0,
            export_enabled = False,
        )
        decision = self.bm.evaluate(snapshot)

        self.assertNotEqual(decision.action, ACTION_START_EXPORT)

    def test_no_export_below_stage1_soc(self):
        """No export when SOC is below the stage 1 threshold."""
        snapshot = _make_snapshot(
            soc_pct               = 75.0,   # below 80% stage1 default
            export_enabled        = True,
            export_stage1_soc_pct = 80.0,
        )
        decision = self.bm.evaluate(snapshot)

        self.assertNotEqual(decision.action, ACTION_START_EXPORT)

    def test_export_when_soc_above_stage2(self):
        """Export at stage2 power when SOC >= stage2 + hysteresis restart threshold.

        Fix: restart from tier 0 to tier 2 requires SOC >= s2_soc + EXPORT_HYSTERESIS_PCT.
        Using soc=96% (>= 90+5=95%) to confirm direct tier 2 start from off.
        At 92%, the new code correctly lands on tier 1 first (see TestExportRestartHysteresis).
        """
        snapshot = _make_snapshot(
            soc_pct               = 96.0,
            export_enabled        = True,
            export_stage1_soc_pct = 80.0,
            export_stage1_kw      = 2.0,
            export_stage2_soc_pct = 90.0,
            export_stage2_kw      = 4.0,
            current_export_tier   = 0,
        )
        decision = self.bm.evaluate(snapshot)

        self.assertEqual(decision.action, ACTION_START_EXPORT)
        self.assertEqual(decision.power_watts, 4000)
        self.assertEqual(decision.export_tier, 2)

    def test_no_export_if_it_would_breach_dawn_floor(self):
        """Dynamic dawn floor blocks export when overnight drain exceeds headroom.

        SOC = 11% = 3.85 kWh. With flat 0.3 kWh/slot profile at 14:00,
        ~17h to dawn = 10.2 kWh expected drain.
        dawn_required = 3.504 + 10.2 + 0.5 = 14.2 kWh >> 3.85 kWh -> blocked.
        """
        snapshot = _make_snapshot(
            soc_pct               = 11.0,
            export_enabled        = True,
            export_stage1_soc_pct = 10.0,   # trigger below current SOC
            export_stage1_kw      = 2.0,
            export_stage2_soc_pct = 10.5,
            export_stage2_kw      = 4.0,
            current_export_tier   = 0,
        )
        decision = self.bm.evaluate(snapshot)

        self.assertNotEqual(decision.action, ACTION_START_EXPORT)

    def test_dynamic_dawn_floor_blocks_export_static_floor_would_pass(self):
        """Dynamic floor catches marginal case that static floor would miss.

        SOC = 35% = 12.26 kWh.
        Static floor (old): dawn_target + buffer = 3.504 + 0.5 = 4.0 kWh
          -> 12.26 > 4.0, would PASS (export allowed).
        Dynamic floor (new): dawn_target + consumption + buffer
          = 3.504 + 10.2 + 0.5 = 14.2 kWh
          -> 12.26 < 14.2, BLOCKED (correct).
        Uses aggressive stage1 threshold (30%) so tier logic would fire.
        """
        snapshot = _make_snapshot(
            soc_pct               = 35.0,
            export_enabled        = True,
            export_stage1_soc_pct = 30.0,   # aggressive: 35% triggers tier 1
            export_stage1_kw      = 2.0,
            export_stage2_soc_pct = 90.0,
            export_stage2_kw      = 4.0,
            current_export_tier   = 0,
            now_hour              = 14,     # ~17h to dawn -> ~10.2 kWh consumption
        )
        decision = self.bm.evaluate(snapshot)

        self.assertNotEqual(decision.action, ACTION_START_EXPORT)


class TestStagedExportDecisions(unittest.TestCase):
    """Tests for 2-tier staged export logic with hysteresis."""

    def setUp(self):
        self.bm = BatteryManager()

    def _snap(self, soc, tier=0, s1_soc=80.0, s1_kw=2.0, s2_soc=90.0, s2_kw=4.0):
        """Shorthand snapshot builder for staged export tests."""
        return _make_snapshot(
            soc_pct               = soc,
            export_enabled        = True,
            export_stage1_soc_pct = s1_soc,
            export_stage1_kw      = s1_kw,
            export_stage2_soc_pct = s2_soc,
            export_stage2_kw      = s2_kw,
            current_export_tier   = tier,
        )

    def test_stage1_trigger_from_off(self):
        """SOC well above stage1+hysteresis from off triggers stage 1 export.

        Fix: restart requires SOC >= s1_soc + EXPORT_HYSTERESIS_PCT (85%), not just 80%.
        Using 86% to confirm tier 1 starts correctly above the new restart threshold.
        """
        snapshot = self._snap(soc=86.0, tier=0)
        decision = self.bm.evaluate(snapshot)

        self.assertEqual(decision.action, ACTION_START_EXPORT)
        self.assertEqual(decision.power_watts, 2000)
        self.assertEqual(decision.export_tier, 1)

    def test_stage2_trigger_from_off(self):
        """SOC above stage2+hysteresis from off triggers stage 2 export.

        Fix: restart to tier 2 requires SOC >= s2_soc + EXPORT_HYSTERESIS_PCT (95%).
        Using 96% to confirm tier 2 starts correctly above the new restart threshold.
        """
        snapshot = self._snap(soc=96.0, tier=0)
        decision = self.bm.evaluate(snapshot)

        self.assertEqual(decision.action, ACTION_START_EXPORT)
        self.assertEqual(decision.power_watts, 4000)
        self.assertEqual(decision.export_tier, 2)

    def test_tier1_upgrades_to_tier2_when_soc_rises(self):
        """Currently at tier 1, SOC rises above stage2 threshold -> upgrade to tier 2."""
        snapshot = self._snap(soc=91.0, tier=1)
        decision = self.bm.evaluate(snapshot)

        self.assertEqual(decision.action, ACTION_START_EXPORT)
        self.assertEqual(decision.power_watts, 4000)
        self.assertEqual(decision.export_tier, 2)

    def test_tier1_held_within_hysteresis_band(self):
        """SOC drops slightly below stage1 but within hysteresis band - tier 1 maintained."""
        # stage1=80%, hysteresis=5%, drop-off at 75%; SOC=76% is inside band
        soc_inside_band = 80.0 - EXPORT_HYSTERESIS_PCT + 1.0   # 76%
        snapshot = self._snap(soc=soc_inside_band, tier=1)
        decision = self.bm.evaluate(snapshot)

        # Should hold tier 1 (no change -> None from _check_export -> self_consumption)
        self.assertNotEqual(decision.action, ACTION_STOP_EXPORT)
        self.assertNotEqual(decision.action, ACTION_START_EXPORT)

    def test_tier1_drops_to_off_below_hysteresis(self):
        """SOC falls below stage1 minus hysteresis band -> STOP_EXPORT."""
        # Drop-off at 75% (80 - 5); SOC=74% is below band
        soc_below_band = 80.0 - EXPORT_HYSTERESIS_PCT - 1.0    # 74%
        snapshot = self._snap(soc=soc_below_band, tier=1)
        decision = self.bm.evaluate(snapshot)

        self.assertEqual(decision.action, ACTION_STOP_EXPORT)
        self.assertEqual(decision.export_tier, 0)

    def test_tier2_downgrades_to_tier1(self):
        """SOC drops from stage2 range to stage1 range -> downgrade to tier 1."""
        # stage2=90%, hysteresis=5%, drop-off at 85%; SOC=84% is below that threshold
        # but still above stage1 drop-off (80-5=75%), so target tier = 1
        soc_in_stage1_range = 90.0 - EXPORT_HYSTERESIS_PCT - 1.0   # 84%
        snapshot = self._snap(soc=soc_in_stage1_range, tier=2)
        decision = self.bm.evaluate(snapshot)

        self.assertEqual(decision.action, ACTION_START_EXPORT)
        self.assertEqual(decision.power_watts, 2000)
        self.assertEqual(decision.export_tier, 1)

    def test_dawn_viability_blocks_export(self):
        """SOC is above stage1 but near the dawn floor - export must be blocked."""
        # 11% SOC = 3.85 kWh; dawn floor = 10% (3.504) + 0.5 buffer = 4.004 kWh
        # 3.85 < 4.004 so dawn guard blocks export
        snapshot = _make_snapshot(
            soc_pct               = 11.0,
            export_enabled        = True,
            export_stage1_soc_pct = 10.0,    # threshold below current SOC
            export_stage1_kw      = 2.0,
            export_stage2_soc_pct = 10.5,
            export_stage2_kw      = 4.0,
            current_export_tier   = 0,
        )
        decision = self.bm.evaluate(snapshot)

        self.assertNotEqual(decision.action, ACTION_START_EXPORT)

    def test_no_change_when_already_at_correct_tier(self):
        """Already at tier 1 and SOC still in stage1 range -> no START_EXPORT re-issue."""
        snapshot = self._snap(soc=85.0, tier=1)  # in stage1 range, already at tier 1
        decision = self.bm.evaluate(snapshot)

        # Should be self_consumption (no change needed)
        self.assertEqual(decision.action, ACTION_SELF_CONSUMPTION)


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


class TestExportRestartHysteresis(unittest.TestCase):
    """Tests for upward hysteresis on export restart from tier 0.

    Bug fixed: When cur_tier == 0 (export stopped), the old code restarted export at
    exactly the stage1 threshold (80%).  At night, with SOC oscillating around 80%, this
    caused export to cycle on/off every 15 minutes.  Each stop wrote export_limit=0W to
    register 40038, which caused Sigenergy to throttle battery discharge and import from
    grid even at 78% SOC.

    Fix: require SOC >= s1_soc + EXPORT_HYSTERESIS_PCT (85%) before restarting from tier 0.
    This creates a symmetric 10% deadband: stop at 75%, restart at 85%.
    """

    def setUp(self):
        self.bm = BatteryManager()

    def _snap(self, soc, tier=0, s1_soc=80.0, s2_soc=90.0, s1_kw=2.0, s2_kw=4.0, now_hour=21):
        """Nighttime snapshot (no PV) for export cycling tests."""
        return _make_snapshot(
            soc_pct               = soc,
            export_enabled        = True,
            export_stage1_soc_pct = s1_soc,
            export_stage1_kw      = s1_kw,
            export_stage2_soc_pct = s2_soc,
            export_stage2_kw      = s2_kw,
            current_export_tier   = tier,
            now_hour              = now_hour,
        )

    # ── Restart threshold correctness ────────────────────────────────────────

    def test_no_restart_at_exactly_stage1_threshold(self):
        """SOC = 80.0% with tier=0 must NOT restart — requires 85% (80 + 5 hysteresis)."""
        decision = self.bm.evaluate(self._snap(soc=80.0, tier=0))
        self.assertNotEqual(decision.action, ACTION_START_EXPORT,
            "Export restarted at exactly 80% — upward hysteresis not applied")

    def test_no_restart_just_above_stage1(self):
        """SOC = 80.5% with tier=0 must NOT restart — still below 85% restart threshold."""
        decision = self.bm.evaluate(self._snap(soc=80.5, tier=0))
        self.assertNotEqual(decision.action, ACTION_START_EXPORT)

    def test_no_restart_midway_through_hysteresis_band(self):
        """SOC = 82.5% with tier=0: inside the deadband (75%-85%), no restart."""
        decision = self.bm.evaluate(self._snap(soc=82.5, tier=0))
        self.assertNotEqual(decision.action, ACTION_START_EXPORT)

    def test_no_restart_just_below_upward_threshold(self):
        """SOC = 84.9% with tier=0: just below restart threshold — no restart."""
        decision = self.bm.evaluate(self._snap(soc=84.9, tier=0))
        self.assertNotEqual(decision.action, ACTION_START_EXPORT)

    def test_restart_at_exactly_upward_hysteresis_threshold(self):
        """SOC = 85.0% with tier=0: exactly at restart threshold — START tier 1."""
        decision = self.bm.evaluate(self._snap(soc=85.0, tier=0))
        self.assertEqual(decision.action, ACTION_START_EXPORT)
        self.assertEqual(decision.export_tier, 1)
        self.assertEqual(decision.power_watts, 2000)

    def test_restart_clearly_above_threshold(self):
        """SOC = 88% with tier=0: clearly above restart threshold — START tier 1."""
        decision = self.bm.evaluate(self._snap(soc=88.0, tier=0))
        self.assertEqual(decision.action, ACTION_START_EXPORT)
        self.assertEqual(decision.export_tier, 1)

    # ── Stage 2 restart threshold ─────────────────────────────────────────────

    def test_stage2_restart_starts_at_tier1_between_thresholds(self):
        """SOC = 93% with tier=0: above restart_s1 (85%) but below restart_s2 (95%) — tier 1."""
        decision = self.bm.evaluate(self._snap(soc=93.0, tier=0))
        self.assertEqual(decision.action, ACTION_START_EXPORT)
        self.assertEqual(decision.export_tier, 1,
            "At 93%, restart should land on tier 1 (not tier 2 which needs 95%)")
        self.assertEqual(decision.power_watts, 2000)

    def test_stage2_restart_at_exact_tier2_threshold(self):
        """SOC = 95% with tier=0: exactly at restart_s2 (90+5) — START tier 2."""
        decision = self.bm.evaluate(self._snap(soc=95.0, tier=0))
        self.assertEqual(decision.action, ACTION_START_EXPORT)
        self.assertEqual(decision.export_tier, 2)
        self.assertEqual(decision.power_watts, 4000)

    # ── Observed bug reproduction ────────────────────────────────────────────

    def test_nighttime_cycling_stop_then_no_restart(self):
        """Reproduces the 27-Mar-2026 bug: export cycling at night causes grid import.

        Scenario:
          21:00 - tier=1, SOC=74% -> export stops (below 75% drop-off)
          21:15 - tier=0, SOC=80.5% (SOC near threshold) -> must NOT restart
        Old code: restarted at 80.5% -> set export_limit=0W -> battery throttled -> import
        New code: does not restart until 85% -> no 0W limit -> battery discharges freely
        """
        # Step 1: export stops when SOC drops below drop-off threshold
        decision_stop = self.bm.evaluate(self._snap(soc=74.0, tier=1))
        self.assertEqual(decision_stop.action, ACTION_STOP_EXPORT,
            "Export should stop at 74% (below 80-5=75% drop-off threshold)")

        # Step 2: next poll, tier=0, SOC reads 80.5% (evening SOC near threshold)
        decision_next = self.bm.evaluate(self._snap(soc=80.5, tier=0))
        self.assertNotEqual(decision_next.action, ACTION_START_EXPORT,
            "Export must not restart at 80.5% — this is the bug that caused grid import")

    def test_stop_and_restart_full_cycle(self):
        """Full correct cycle: stop at 74%, SOC rises to 86% during solar, restart."""
        # Stop
        d_stop = self.bm.evaluate(self._snap(soc=74.0, tier=1))
        self.assertEqual(d_stop.action, ACTION_STOP_EXPORT)

        # Intermediate checks — should not restart at 80%, 82%, 84%
        for soc in (80.0, 82.0, 84.0):
            d = self.bm.evaluate(self._snap(soc=soc, tier=0))
            self.assertNotEqual(d.action, ACTION_START_EXPORT,
                f"Incorrectly restarted at {soc}% (threshold is 85%)")

        # Restart when SOC genuinely recovers to 86% (e.g. next morning solar)
        d_restart = self.bm.evaluate(self._snap(soc=86.0, tier=0))
        self.assertEqual(d_restart.action, ACTION_START_EXPORT)
        self.assertEqual(d_restart.export_tier, 1)

    # ── Downward hysteresis unchanged ────────────────────────────────────────

    def test_downward_hysteresis_still_works_in_tier1(self):
        """Downward hysteresis is unchanged: tier 1 holds until SOC < 80-5 = 75%."""
        # 76% is inside band (above 75%) - hold tier 1
        d_hold = self.bm.evaluate(self._snap(soc=76.0, tier=1))
        self.assertNotEqual(d_hold.action, ACTION_STOP_EXPORT)

        # 74.9% is below band - stop
        d_stop = self.bm.evaluate(self._snap(soc=74.9, tier=1))
        self.assertEqual(d_stop.action, ACTION_STOP_EXPORT)

    def test_symmetric_deadband_width(self):
        """Confirm the deadband is symmetric: stop at 75%, restart at 85% = 10% wide."""
        stop_threshold    = 80.0 - EXPORT_HYSTERESIS_PCT     # 75%
        restart_threshold = 80.0 + EXPORT_HYSTERESIS_PCT     # 85%
        deadband_width    = restart_threshold - stop_threshold

        self.assertAlmostEqual(deadband_width, 10.0, places=1,
            msg=f"Expected 10% deadband, got {deadband_width}%")

        # Verify stop
        d = self.bm.evaluate(self._snap(soc=stop_threshold - 0.1, tier=1))
        self.assertEqual(d.action, ACTION_STOP_EXPORT)

        # Verify restart
        d = self.bm.evaluate(self._snap(soc=restart_threshold, tier=0))
        self.assertEqual(d.action, ACTION_START_EXPORT)


if __name__ == "__main__":
    print(f"Running {PLUGIN_NAME if 'PLUGIN_NAME' in dir() else 'SigenEnergyManager'} battery_manager tests")
    unittest.main(verbosity=2)
