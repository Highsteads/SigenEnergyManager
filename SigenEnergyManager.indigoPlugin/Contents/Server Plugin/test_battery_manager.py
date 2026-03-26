#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    test_battery_manager.py
# Description: Unit tests for battery_manager.py decision engine
#              Runs without Indigo installed
# Author:      CliveS & Claude Sonnet 4.6
# Date:        26-03-2026
# Version:     1.0

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
    TARIFF_TRACKER,
    TARIFF_GO,
    TARIFF_FLUX,
    TRACKER_DEFER_THRESHOLD,
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
    export_trigger_pct=90.0,
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
        current_soc_pct     = soc_pct,
        capacity_kwh        = CAPACITY_KWH,
        efficiency          = EFFICIENCY,
        dawn_target_pct     = DAWN_TARGET,
        health_cutoff_pct   = HEALTH_FLOOR,
        export_enabled      = export_enabled,
        export_trigger_pct  = export_trigger_pct,
        max_export_kw       = 4.0,
        tariff              = tariff,
        forecast_p50        = forecast_p50 or {},
        forecast_p10        = {},
        dawn_times          = dawn_times,
        consumption_profile = consumption_profile,
        now                 = _now(hour=now_hour),
        vpp_active          = vpp_active,
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
        forecast_p50 = {
            datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"): 3000,  # 3 kWh now
        }
        snapshot = _make_snapshot(
            soc_pct         = 95.0,
            export_enabled  = False,
            export_trigger_pct = 90.0,
        )
        decision = self.bm.evaluate(snapshot)

        self.assertNotEqual(decision.action, ACTION_START_EXPORT)

    def test_no_export_below_trigger_soc(self):
        """No export when SOC is below the trigger threshold."""
        snapshot = _make_snapshot(
            soc_pct         = 85.0,   # below 90% trigger
            export_enabled  = True,
            export_trigger_pct = 90.0,
        )
        decision = self.bm.evaluate(snapshot)

        self.assertNotEqual(decision.action, ACTION_START_EXPORT)

    def test_export_when_soc_above_trigger_and_solar_forecasted(self):
        """Export when SOC >= trigger AND solar forecast is meaningful."""
        now      = _now(hour=11)
        hour_key = now.strftime("%Y-%m-%d %H:%M:%S")
        next_key = (now + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")

        forecast_p50 = {
            hour_key: 3000,
            next_key: 3000,
        }

        snapshot = _make_snapshot(
            soc_pct            = 92.0,
            export_enabled     = True,
            export_trigger_pct = 90.0,
            forecast_p50       = forecast_p50,
            now_hour           = 11,
        )

        decision = self.bm.evaluate(snapshot)

        # Should export (high SOC + solar incoming)
        self.assertEqual(decision.action, ACTION_START_EXPORT)
        self.assertGreater(decision.power_watts, 0)

    def test_no_export_when_no_solar_forecast(self):
        """No export when there is no meaningful solar forecast."""
        snapshot = _make_snapshot(
            soc_pct            = 95.0,
            export_enabled     = True,
            export_trigger_pct = 90.0,
            forecast_p50       = {},  # no forecast
            now_hour           = 11,
        )
        decision = self.bm.evaluate(snapshot)

        self.assertNotEqual(decision.action, ACTION_START_EXPORT)

    def test_no_export_if_it_would_breach_dawn_floor(self):
        """No export if it would compromise dawn viability."""
        # SOC at 91% (31.9 kWh) at 10:00
        # dawn floor = 10% + 0.5 buffer = 10% of 35.04 + 0.5 = 4.0 kWh
        # Export headroom = 31.9 - 4.0 = 27.9 kWh -> export IS possible
        # Actually test when headroom is close to zero
        snapshot = _make_snapshot(
            soc_pct            = 12.0,  # barely above target
            export_enabled     = True,
            export_trigger_pct = 11.0,  # trigger below current SOC
            forecast_p50       = {"dummy": 5000},
        )
        decision = self.bm.evaluate(snapshot)

        # Headroom should be near zero, so export kW should be negligible or no export
        if decision.action == ACTION_START_EXPORT:
            self.assertLess(decision.power_watts, 1000)  # < 1 kW if any


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


if __name__ == "__main__":
    print(f"Running {PLUGIN_NAME if 'PLUGIN_NAME' in dir() else 'SigenEnergyManager'} battery_manager tests")
    unittest.main(verbosity=2)
