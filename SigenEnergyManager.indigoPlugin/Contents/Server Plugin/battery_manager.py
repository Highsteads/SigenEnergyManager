#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    battery_manager.py
# Description: Core battery management decision engine - self-sufficiency first
#              No grid import unless battery cannot reach next-day solar at minimum SOC.
#              Export to prevent 100% cap during solar generation window.
# Author:      CliveS & Claude Sonnet 4.6
# Date:        27-03-2026 22:11 GMT
# Version:     1.4

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Dict, Tuple

# Import tariff key constants
try:
    from octopus_api import (
        TARIFF_TRACKER, TARIFF_GO, TARIFF_FLUX,
        TARIFF_IGO, TARIFF_IFLUX, TARIFF_AGILE,
        TARIFF_WINDOWS,
    )
except ImportError:
    # Allow standalone testing without Indigo environment
    TARIFF_TRACKER = "tracker"
    TARIFF_GO      = "go"
    TARIFF_FLUX    = "flux"
    TARIFF_IGO     = "igo"
    TARIFF_IFLUX   = "iflux"
    TARIFF_AGILE   = "agile"
    TARIFF_WINDOWS = {
        "go":    {"cheap_start": "00:30", "cheap_end": "05:30"},
        "flux":  {"cheap_start": "02:00", "cheap_end": "05:00"},
        "igo":   {"cheap_start": "00:30", "cheap_end": "05:30"},
        "iflux": {"cheap_start": "02:00", "cheap_end": "05:00"},
    }


# ============================================================
# Decision action constants
# ============================================================

ACTION_SELF_CONSUMPTION  = "self_consumption"   # default: battery covers home load
ACTION_START_IMPORT      = "start_import"        # begin charging from grid now
ACTION_STOP_IMPORT       = "stop_import"         # charging complete - return to self_consumption
ACTION_SCHEDULE_IMPORT   = "schedule_import"     # defer import to a cheaper/later window
ACTION_START_EXPORT      = "start_export"        # force-discharge to grid at night
ACTION_STOP_EXPORT       = "stop_export"         # stop night export, return to self_consumption

# Minimum percentage cheaper to justify waiting for tomorrow's Tracker rate
TRACKER_DEFER_THRESHOLD = 0.90   # tomorrow must be < 90% of today (10%+ cheaper)

# Minimum import quantity - below this don't bother charging
MIN_IMPORT_KWH = 0.5

# Night export constants
# Note: pvPowerWatts reads 0W in Discharge ESS First mode (0x06) — the inverter
# suppresses PV. A PV-based night/day check is therefore permanently blind while
# exporting. Sunrise is detected via today's Solcast dawn_time instead.
NIGHT_EXPORT_BUFFER_KWH           = 1.0   # safety margin above dawn target before exporting
MIN_NIGHT_EXPORT_KWH              = 0.5   # minimum surplus to bother starting export
NIGHT_EXPORT_TOMORROW_CONFIDENCE  = 0.6   # 60% of correctedTomorrowKwh must cover daily load
                                          # (P50 bias-corrected; 0.6 tolerates 40% shortfall)
DAYTIME_WINDOW_HOURS              = 14    # hours after dawn during which export is blocked
                                          # dawn 07:00 + 14h = 21:00 → nighttime resumes


@dataclass
class TariffData:
    """Tariff-related information passed to the decision engine."""
    tariff_key:    str   = TARIFF_TRACKER
    today_rate_p:  Optional[float] = None   # pence/kWh
    tomorrow_rate_p: Optional[float] = None # pence/kWh (may be None until ~16:00)
    cheap_start:   Optional[str] = None     # "HH:MM" local time (Go/Flux cheap window)
    cheap_end:     Optional[str] = None     # "HH:MM"
    cheap_rate_p:  Optional[float] = None   # cheap window rate (Go/Flux)
    agile_slots:   List[Tuple[datetime, float]] = field(default_factory=list)


@dataclass
class ManagerSnapshot:
    """Complete system snapshot passed to BatteryManager.evaluate()."""
    # Battery state
    current_soc_pct:    float = 0.0     # 0-100%
    capacity_kwh:       float = 35.04
    efficiency:         float = 0.94    # round-trip, for import quantity calc

    # Manager settings
    dawn_target_pct:   float = 10.0    # minimum SOC at dawn
    health_cutoff_pct: float = 10.0    # hardware discharge floor
    export_enabled:    bool  = False   # export MPAN active
    max_export_kw:     float = 4.0     # DNO export cap (kW)

    # Live inverter readings (for night export logic)
    pv_watts:               int   = 0     # current PV generation (W)
    export_active:          bool  = False # night export currently running
    corrected_tomorrow_kwh: float = 0.0   # Solcast bias-corrected P50 for tomorrow (kWh)

    # Tariff data
    tariff: TariffData = field(default_factory=TariffData)

    # Forecast: hourly Wh dicts {"YYYY-MM-DD HH:00:00": wh_int}
    # P50 for display; P10 (conservative) for dawn viability planning
    forecast_p50: Dict[str, int] = field(default_factory=dict)
    forecast_p10: Dict[str, int] = field(default_factory=dict)

    # Dawn times: {"YYYY-MM-DD": datetime} - first hour with meaningful PV
    dawn_times: Dict[str, datetime] = field(default_factory=dict)

    # Consumption profile: 48 half-hourly floats (kWh per slot)
    consumption_profile: List[float] = field(default_factory=list)

    # Current time
    now: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    # VPP active - when True all battery commands are suppressed
    vpp_active: bool = False


@dataclass
class DawnViability:
    """Result of the dawn viability check."""
    viable:           bool  = True
    soc_at_dawn_kwh:  float = 0.0
    dawn_target_kwh:  float = 3.504   # 10% of 35.04
    import_needed:    bool  = False
    import_kwh_net:   float = 0.0     # energy needed at battery terminals
    import_kwh_grid:  float = 0.0     # energy needed from grid (= net / efficiency)
    dawn_dt:          Optional[datetime] = None
    hours_to_dawn:    float = 8.0
    expected_consumption_kwh: float = 0.0
    current_soc_kwh:  float = 0.0


@dataclass
class Decision:
    """Battery management decision returned by BatteryManager.evaluate()."""
    action:           str   = ACTION_SELF_CONSUMPTION
    reason:           str   = ""
    power_watts:      int   = 0
    target_soc_pct:   float = 0.0
    scheduled_time:   Optional[datetime] = None   # for deferred imports
    dawn_viable:      bool  = True
    soc_at_dawn_kwh:  float = 0.0
    import_kwh:       float = 0.0
    export_kw:        float = 0.0    # kW being exported (night export)


class BatteryManager:
    """Core self-sufficiency battery decision engine.

    Philosophy:
    1. Never import from grid unless battery cannot reach dawn at dawn_target_pct.
    2. Export only to prevent 100% SOC cap during peak solar generation.
    3. If forced import needed: use cheapest available time window.
    4. VPP events override all decisions (Axle cloud controls battery).

    This class is stateless: it takes a ManagerSnapshot and returns a Decision.
    All state is managed by plugin.py.
    """

    def evaluate(self, snapshot: ManagerSnapshot) -> Decision:
        """Main entry point - evaluate system state and return a decision.

        Args:
            snapshot: Complete system snapshot

        Returns:
            Decision object describing what action to take
        """
        # VPP takes precedence - suspend all manager actions
        if snapshot.vpp_active:
            return Decision(
                action=ACTION_SELF_CONSUMPTION,
                reason="VPP event active - Axle has control",
            )

        # Step 1: Calculate dawn viability
        viability = self._check_dawn_viability(snapshot)

        # Step 2: If import needed, plan it (import takes priority over export)
        if viability.import_needed:
            import_decision = self._plan_import(snapshot, viability)
            import_decision.dawn_viable     = False
            import_decision.soc_at_dawn_kwh = viability.soc_at_dawn_kwh
            import_decision.import_kwh      = viability.import_kwh_grid
            return import_decision

        # Step 3: Night export opportunity (or stop if conditions no longer met)
        if snapshot.export_enabled:
            export_decision = self._check_night_export(snapshot, viability)
            if export_decision is not None:
                return export_decision

        # Default: self-consumption
        return Decision(
            action           = ACTION_SELF_CONSUMPTION,
            reason           = f"Dawn OK (est {viability.soc_at_dawn_kwh:.1f} kWh at dawn)",
            dawn_viable      = True,
            soc_at_dawn_kwh  = viability.soc_at_dawn_kwh,
        )

    # ================================================================
    # Dawn Viability Check
    # ================================================================

    def _check_dawn_viability(self, snapshot: ManagerSnapshot) -> DawnViability:
        """Calculate projected SOC at the next solar generation window.

        Uses P10 (conservative) forecast to determine when PV starts.
        Uses consumption profile to estimate overnight drain.
        """
        cap_kwh          = snapshot.capacity_kwh
        current_soc_kwh  = snapshot.current_soc_pct / 100.0 * cap_kwh
        dawn_target_kwh  = snapshot.dawn_target_pct / 100.0 * cap_kwh
        health_floor_kwh = snapshot.health_cutoff_pct / 100.0 * cap_kwh
        now              = snapshot.now

        # Find next dawn time (first hour with meaningful PV generation tomorrow)
        tomorrow_str = (now.date() + timedelta(days=1)).strftime("%Y-%m-%d")
        today_str    = now.date().strftime("%Y-%m-%d")

        dawn_dt = (
            snapshot.dawn_times.get(tomorrow_str)
            or snapshot.dawn_times.get(today_str)
        )

        if dawn_dt is None:
            # No forecast data - assume dawn at 07:00 tomorrow
            dawn_dt = datetime(
                now.year, now.month, now.day, 7, 0, 0,
                tzinfo=now.tzinfo
            ) + timedelta(days=1)

        hours_to_dawn = max(0.0, (dawn_dt - now).total_seconds() / 3600.0)

        # Estimate consumption until dawn
        expected_kwh = self._estimate_consumption_until(
            now, dawn_dt, snapshot.consumption_profile
        )

        # Projected SOC at dawn (raw, before hardware floor)
        raw_soc_at_dawn = current_soc_kwh - expected_kwh

        # Use raw projection for the import decision (before clamping to floor)
        import_needed = raw_soc_at_dawn < dawn_target_kwh

        # Clamp reported value to hardware floor for display only
        soc_at_dawn_kwh = max(health_floor_kwh, raw_soc_at_dawn)

        import_kwh_net  = max(0.0, dawn_target_kwh - raw_soc_at_dawn)
        import_kwh_grid = import_kwh_net / max(0.01, snapshot.efficiency)

        return DawnViability(
            viable                   = not import_needed,
            soc_at_dawn_kwh          = round(soc_at_dawn_kwh, 2),
            dawn_target_kwh          = dawn_target_kwh,
            import_needed            = import_needed and import_kwh_grid >= MIN_IMPORT_KWH,
            import_kwh_net           = round(import_kwh_net, 2),
            import_kwh_grid          = round(import_kwh_grid, 2),
            dawn_dt                  = dawn_dt,
            hours_to_dawn            = round(hours_to_dawn, 1),
            expected_consumption_kwh = round(expected_kwh, 2),
            current_soc_kwh          = round(current_soc_kwh, 2),
        )

    def _estimate_consumption_until(
        self,
        now: datetime,
        target: datetime,
        profile: List[float],
    ) -> float:
        """Sum expected consumption from now until target using 48-slot profile.

        Args:
            now:     Current datetime
            target:  Target datetime (dawn)
            profile: 48-slot half-hourly profile (kWh per slot)

        Returns:
            Expected consumption in kWh
        """
        if not profile or len(profile) != 48:
            # Default: 0.45 kWh/hour overnight
            hours = (target - now).total_seconds() / 3600.0
            return max(0.0, hours * 0.45)

        total_kwh = 0.0
        cursor    = now

        while cursor < target:
            slot_start = cursor.replace(minute=0 if cursor.minute < 30 else 30,
                                         second=0, microsecond=0)
            slot_end   = slot_start + timedelta(minutes=30)

            # How much of this 30-min slot falls within [cursor, target]?
            effective_start = max(cursor, slot_start)
            effective_end   = min(target, slot_end)
            fraction        = (effective_end - effective_start).total_seconds() / 1800.0
            fraction        = max(0.0, min(1.0, fraction))

            if fraction > 0:
                slot_idx   = cursor.hour * 2 + (1 if cursor.minute >= 30 else 0)
                slot_idx   = max(0, min(47, slot_idx))
                total_kwh += profile[slot_idx] * fraction

            cursor = slot_end

        return max(0.0, total_kwh)

    # ================================================================
    # Import Planning
    # ================================================================

    def _plan_import(self, snapshot: ManagerSnapshot, viability: DawnViability) -> Decision:
        """Determine when and how much to import from the grid.

        Never imports for profit - only to ensure dawn viability.
        Uses cheapest available time window for the active tariff.
        """
        tariff     = snapshot.tariff
        import_kwh = viability.import_kwh_grid
        now        = snapshot.now
        target_soc = snapshot.dawn_target_pct + 2.0  # small buffer above minimum

        # Tariff-specific import timing
        if tariff.tariff_key in (TARIFF_GO, TARIFF_FLUX, TARIFF_IGO, TARIFF_IFLUX):
            return self._plan_tou_import(snapshot, viability, target_soc)

        if tariff.tariff_key == TARIFF_TRACKER:
            return self._plan_tracker_import(snapshot, viability, target_soc)

        if tariff.tariff_key == TARIFF_AGILE:
            return self._plan_agile_import(snapshot, viability, target_soc)

        # Unknown tariff - import now
        return Decision(
            action         = ACTION_START_IMPORT,
            reason         = f"Dawn viability at risk ({viability.soc_at_dawn_kwh:.1f} kWh at dawn, need {viability.dawn_target_kwh:.1f}). Unknown tariff - importing now.",
            power_watts    = int(min(10000, snapshot.capacity_kwh * 1000 / 2)),
            target_soc_pct = target_soc,
        )

    def _plan_tou_import(
        self, snapshot: ManagerSnapshot, viability: DawnViability, target_soc: float
    ) -> Decision:
        """Plan import for Go/Flux/iGo/iFlux - wait for cheap window if possible."""
        tariff    = snapshot.tariff
        now       = snapshot.now
        dawn_dt   = viability.dawn_dt
        cap_kwh   = snapshot.capacity_kwh
        floor_kwh = snapshot.health_cutoff_pct / 100.0 * cap_kwh

        cheap_start = tariff.cheap_start
        cheap_end   = tariff.cheap_end

        if not cheap_start or not cheap_end:
            # No window data - import now
            return Decision(
                action         = ACTION_START_IMPORT,
                reason         = f"Dawn risk - Go/Flux cheap window unavailable, importing now",
                power_watts    = 10000,
                target_soc_pct = target_soc,
            )

        # Are we currently in the cheap window?
        now_hm = now.strftime("%H:%M")
        if self._time_in_window(now_hm, cheap_start, cheap_end):
            return Decision(
                action         = ACTION_START_IMPORT,
                reason         = f"Dawn risk - in cheap window ({cheap_start}-{cheap_end}), importing now",
                power_watts    = 10000,
                target_soc_pct = target_soc,
            )

        # Check: will battery survive until the next cheap window starts?
        next_window_dt = self._next_window_start(now, cheap_start)
        if next_window_dt and dawn_dt:
            # Time until window starts
            hours_to_window = (next_window_dt - now).total_seconds() / 3600.0
            # Expected drain until window start
            drain_to_window = self._estimate_consumption_until(
                now, next_window_dt, snapshot.consumption_profile
            )
            soc_at_window_kwh = (snapshot.current_soc_pct / 100.0 * cap_kwh) - drain_to_window

            # Can we safely wait? Must stay above health floor AND window is before dawn
            can_wait = (
                soc_at_window_kwh >= floor_kwh
                and next_window_dt < dawn_dt
            )

            if can_wait:
                return Decision(
                    action           = ACTION_SCHEDULE_IMPORT,
                    reason           = f"Dawn risk - waiting for cheap window at {cheap_start}",
                    power_watts      = 10000,
                    target_soc_pct   = target_soc,
                    scheduled_time   = next_window_dt,
                )

        # Cannot safely wait - import now at standard rate (survival beats cheapness)
        return Decision(
            action         = ACTION_START_IMPORT,
            reason         = f"Dawn risk - cannot wait for cheap window (battery too low), importing now",
            power_watts    = 10000,
            target_soc_pct = target_soc,
        )

    def _plan_tracker_import(
        self, snapshot: ManagerSnapshot, viability: DawnViability, target_soc: float
    ) -> Decision:
        """Plan import on Tracker tariff.

        Tracker is flat rate all day. Compare today vs tomorrow:
        - If tomorrow's rate is published AND > 10% cheaper AND battery has margin:
          defer import until 00:05 (new day rate)
        - Otherwise: import now at today's rate
        """
        tariff       = snapshot.tariff
        now          = snapshot.now
        today_rate   = tariff.today_rate_p
        tomorrow_rate = tariff.tomorrow_rate_p
        cap_kwh      = snapshot.capacity_kwh
        floor_kwh    = snapshot.health_cutoff_pct / 100.0 * cap_kwh

        # Check if tomorrow's rate is known and meaningfully cheaper
        if (tomorrow_rate is not None
                and today_rate is not None
                and today_rate > 0
                and tomorrow_rate < today_rate * TRACKER_DEFER_THRESHOLD):

            # Will SOC at midnight still be above health floor?
            midnight_dt = datetime.combine(
                now.date() + timedelta(days=1),
                datetime.min.time()
            ).replace(tzinfo=now.tzinfo)

            drain_to_midnight = self._estimate_consumption_until(
                now, midnight_dt, snapshot.consumption_profile
            )
            soc_at_midnight_kwh = (
                snapshot.current_soc_pct / 100.0 * cap_kwh
            ) - drain_to_midnight

            if soc_at_midnight_kwh >= floor_kwh:
                import_time = midnight_dt + timedelta(minutes=5)
                saving_p    = round(today_rate - tomorrow_rate, 2)
                return Decision(
                    action           = ACTION_SCHEDULE_IMPORT,
                    reason           = (
                        f"Dawn risk - tomorrow Tracker rate {tomorrow_rate:.2f}p is "
                        f"{saving_p:.2f}p/kWh cheaper than today {today_rate:.2f}p. "
                        f"Deferring to 00:05"
                    ),
                    power_watts      = 10000,
                    target_soc_pct   = target_soc,
                    scheduled_time   = import_time,
                )

        # Import now at today's rate
        rate_str = f"{today_rate:.2f}p/kWh" if today_rate else "unknown rate"
        return Decision(
            action         = ACTION_START_IMPORT,
            reason         = f"Dawn risk ({viability.soc_at_dawn_kwh:.1f} kWh at dawn, need {viability.dawn_target_kwh:.1f}). Importing now at Tracker {rate_str}",
            power_watts    = 10000,
            target_soc_pct = target_soc,
        )

    def _plan_agile_import(
        self, snapshot: ManagerSnapshot, viability: DawnViability, target_soc: float
    ) -> Decision:
        """Plan import on Agile: find cheapest available slot before dawn."""
        tariff  = snapshot.tariff
        now     = snapshot.now
        dawn_dt = viability.dawn_dt

        if not tariff.agile_slots or dawn_dt is None:
            # No Agile data - import now
            return Decision(
                action         = ACTION_START_IMPORT,
                reason         = "Dawn risk - no Agile rates available, importing now",
                power_watts    = 10000,
                target_soc_pct = target_soc,
            )

        # Find cheapest slot before dawn that we can safely reach
        cap_kwh   = snapshot.capacity_kwh
        floor_kwh = snapshot.health_cutoff_pct / 100.0 * cap_kwh

        available_slots = [
            (dt, rate) for dt, rate in tariff.agile_slots
            if dt > now and dt < dawn_dt
        ]

        if not available_slots:
            return Decision(
                action         = ACTION_START_IMPORT,
                reason         = "Dawn risk - no future Agile slots before dawn, importing now",
                power_watts    = 10000,
                target_soc_pct = target_soc,
            )

        # Find the cheapest slot where we can still survive until it starts
        cheapest_viable = None
        for slot_dt, rate in sorted(available_slots, key=lambda x: x[1]):
            drain = self._estimate_consumption_until(now, slot_dt, snapshot.consumption_profile)
            soc_at_slot = (snapshot.current_soc_pct / 100.0 * cap_kwh) - drain
            if soc_at_slot >= floor_kwh:
                cheapest_viable = (slot_dt, rate)
                break

        if cheapest_viable:
            slot_dt, rate = cheapest_viable
            if slot_dt <= now + timedelta(minutes=5):
                return Decision(
                    action         = ACTION_START_IMPORT,
                    reason         = f"Dawn risk - cheapest Agile slot now ({rate:.2f}p/kWh), importing",
                    power_watts    = 10000,
                    target_soc_pct = target_soc,
                )
            return Decision(
                action           = ACTION_SCHEDULE_IMPORT,
                reason           = f"Dawn risk - scheduled import at {slot_dt.strftime('%H:%M')} ({rate:.2f}p/kWh Agile)",
                power_watts      = 10000,
                target_soc_pct   = target_soc,
                scheduled_time   = slot_dt,
            )

        # Cannot safely wait for any slot - import now
        return Decision(
            action         = ACTION_START_IMPORT,
            reason         = "Dawn risk - no viable Agile slot available, importing now",
            power_watts    = 10000,
            target_soc_pct = target_soc,
        )

    # ================================================================
    # Night Export
    # ================================================================

    def _check_night_export(
        self, snapshot: ManagerSnapshot, viability: DawnViability
    ) -> Optional[Decision]:
        """Check if conditions are right for night export via force-discharge.

        All three conditions must hold:
        1. Before sunrise — checked against today's Solcast dawn time.
           pvPowerWatts cannot be used: in Discharge ESS First mode (0x06)
           the inverter suppresses PV to 0W regardless of actual solar.
        2. Battery surplus above dawn floor + safety buffer
        3. Tomorrow P50 solar (at 60% confidence) >= expected daily consumption

        Returns a Decision or None if conditions not met.
        """
        # Condition 1: Night only — stop/block export once sunrise is reached.
        # Dawn time for today comes from the Solcast forecast in snapshot.dawn_times.
        # Export is blocked for DAYTIME_WINDOW_HOURS after dawn (covers full daylight).
        try:
            import pytz
            _tz = pytz.timezone("Europe/London")
            _local_now = snapshot.now.astimezone(_tz)
        except (ImportError, Exception):
            _local_now = snapshot.now

        _today_str     = _local_now.date().strftime("%Y-%m-%d")
        _today_dawn_dt = snapshot.dawn_times.get(_today_str)
        _is_daytime    = (
            _today_dawn_dt is not None
            and snapshot.now >= _today_dawn_dt
            and (snapshot.now - _today_dawn_dt).total_seconds() < DAYTIME_WINDOW_HOURS * 3600
        )

        if _is_daytime:
            if snapshot.export_active:
                _dawn_str = _today_dawn_dt.astimezone(_tz).strftime("%H:%M")
                return Decision(
                    action          = ACTION_STOP_EXPORT,
                    reason          = f"Sunrise reached ({_dawn_str}) - stopping night export",
                    dawn_viable     = viability.viable,
                    soc_at_dawn_kwh = viability.soc_at_dawn_kwh,
                )
            return None

        # Calculate surplus above dawn floor
        cap_kwh          = snapshot.capacity_kwh
        current_soc_kwh  = snapshot.current_soc_pct / 100.0 * cap_kwh
        drain_to_dawn    = viability.expected_consumption_kwh
        projected_dawn   = current_soc_kwh - drain_to_dawn
        surplus_kwh      = projected_dawn - viability.dawn_target_kwh - NIGHT_EXPORT_BUFFER_KWH

        # Condition 2: Battery has surplus worth exporting
        if surplus_kwh < MIN_NIGHT_EXPORT_KWH:
            if snapshot.export_active:
                return Decision(
                    action          = ACTION_STOP_EXPORT,
                    reason          = (
                        f"Night export: projected dawn {projected_dawn:.1f} kWh approaching "
                        f"floor ({viability.dawn_target_kwh:.1f} + {NIGHT_EXPORT_BUFFER_KWH:.1f} kWh buffer) - stopping"
                    ),
                    dawn_viable     = viability.viable,
                    soc_at_dawn_kwh = viability.soc_at_dawn_kwh,
                )
            return None

        # Condition 3: Tomorrow P50 (bias-corrected) at 60% confidence >= daily consumption
        # Using correctedTomorrowKwh rather than P10 because P10 (10th percentile) is
        # too pessimistic — it blocks export even on days that clearly will be sunny.
        # At 60% confidence we tolerate a 40% shortfall below Solcast's best estimate,
        # while the dawn floor (condition 2) remains the real safety net.
        daily_cons_kwh      = (
            sum(snapshot.consumption_profile)
            if len(snapshot.consumption_profile) == 48
            else 10.8   # default ~0.45 kWh/h * 24h
        )
        tomorrow_viable_kwh = snapshot.corrected_tomorrow_kwh * NIGHT_EXPORT_TOMORROW_CONFIDENCE

        if tomorrow_viable_kwh < daily_cons_kwh:
            if snapshot.export_active:
                return Decision(
                    action          = ACTION_STOP_EXPORT,
                    reason          = (
                        f"Night export: tomorrow forecast {snapshot.corrected_tomorrow_kwh:.1f} kWh "
                        f"(x{NIGHT_EXPORT_TOMORROW_CONFIDENCE} = {tomorrow_viable_kwh:.1f}) < "
                        f"daily load {daily_cons_kwh:.1f} kWh - stopping"
                    ),
                    dawn_viable     = viability.viable,
                    soc_at_dawn_kwh = viability.soc_at_dawn_kwh,
                )
            return None

        # All conditions met - export (idempotent if already running)
        export_kw = snapshot.max_export_kw
        return Decision(
            action          = ACTION_START_EXPORT,
            reason          = (
                f"Night export: {surplus_kwh:.1f} kWh surplus above dawn floor. "
                f"Tomorrow forecast {snapshot.corrected_tomorrow_kwh:.1f} kWh "
                f"(60% = {tomorrow_viable_kwh:.1f}) >= daily {daily_cons_kwh:.1f} kWh. "
                f"Exporting {export_kw:.1f} kW"
            ),
            power_watts     = int(export_kw * 1000),
            export_kw       = export_kw,
            dawn_viable     = True,
            soc_at_dawn_kwh = viability.soc_at_dawn_kwh,
        )

    @staticmethod
    def _sum_tomorrow_forecast(forecast_p10: Dict[str, int], now: datetime) -> float:
        """Sum P10 forecast Wh for tomorrow's date, returning kWh.

        Forecast keys are in Europe/London local time ("YYYY-MM-DD HH:MM:SS").
        """
        try:
            import pytz
            local_now = now.astimezone(pytz.timezone("Europe/London"))
        except (ImportError, Exception):
            local_now = now   # fallback to UTC

        tomorrow_str = (local_now.date() + timedelta(days=1)).strftime("%Y-%m-%d")
        total_wh = 0
        for key, wh in forecast_p10.items():
            if key.startswith(tomorrow_str):
                try:
                    total_wh += int(wh)
                except (ValueError, TypeError):
                    continue
        return total_wh / 1000.0

    # ================================================================
    # Helpers
    # ================================================================

    @staticmethod
    def _time_in_window(time_str: str, start_str: str, end_str: str) -> bool:
        """Check if HH:MM falls within start-end window. Handles overnight windows."""
        def to_min(t):
            h, m = t.split(":")
            return int(h) * 60 + int(m)

        t     = to_min(time_str)
        start = to_min(start_str)
        end   = to_min(end_str)

        if start <= end:
            return start <= t < end
        else:
            return t >= start or t < end  # overnight window

    @staticmethod
    def _next_window_start(now: datetime, window_start_str: str) -> Optional[datetime]:
        """Return the next occurrence of HH:MM window start as a datetime."""
        try:
            h, m = window_start_str.split(":")
            candidate = now.replace(
                hour=int(h), minute=int(m), second=0, microsecond=0
            )
            if candidate <= now:
                candidate += timedelta(days=1)
            return candidate
        except (ValueError, AttributeError):
            return None

    @staticmethod
    def _forecast_next_n_hours(
        forecast_p50: Dict[str, int], now: datetime, n_hours: int = 2
    ) -> int:
        """Sum forecast Wh for the next n_hours from now."""
        total    = 0
        end_time = now + timedelta(hours=n_hours)

        for key, wh in forecast_p50.items():
            try:
                dt = datetime.strptime(key, "%Y-%m-%d %H:%M:%S")
                # Make naive datetime timezone-aware if needed
                if now.tzinfo and dt.tzinfo is None:
                    import pytz
                    dt = pytz.timezone("Europe/London").localize(dt)
                if now <= dt < end_time:
                    total += wh
            except (ValueError, TypeError):
                continue

        return total
