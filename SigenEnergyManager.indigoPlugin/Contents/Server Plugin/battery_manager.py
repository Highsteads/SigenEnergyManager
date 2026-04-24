#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    battery_manager.py
# Description: Core battery management decision engine - self-sufficiency first
#              No grid import unless battery cannot reach next-day solar at minimum SOC.
#              Export to prevent 100% cap during solar generation window.
# Author:      CliveS & Claude Sonnet 4.6
# Date:        09-04-2026
# Version:     2.2

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Dict, Tuple

# Import tariff key constants
try:
    from octopus_api import (
        TARIFF_TRACKER, TARIFF_GO, TARIFF_FLUX,
        TARIFF_IGO, TARIFF_IFLUX, TARIFF_AGILE,
        TARIFF_FLEXIBLE,
        TARIFF_WINDOWS,
    )
except ImportError:
    # Allow standalone testing without Indigo environment
    TARIFF_TRACKER  = "tracker"
    TARIFF_GO       = "go"
    TARIFF_FLUX     = "flux"
    TARIFF_IGO      = "igo"
    TARIFF_IFLUX    = "iflux"
    TARIFF_AGILE    = "agile"
    TARIFF_FLEXIBLE = "flexible"
    TARIFF_WINDOWS  = {
        "go":    {"cheap_start": "00:30", "cheap_end": "05:30"},
        "flux":  {"cheap_start": "02:00", "cheap_end": "05:00"},
        "igo":   {"cheap_start": "23:30", "cheap_end": "05:30"},  # 23:30-05:30 (6h)
        "iflux": {"cheap_start": "19:00", "cheap_end": "16:00"},  # 21h non-peak window (avoids 16:00-19:00 peak)
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
ACTION_SOLAR_OVERFLOW    = "solar_overflow"      # daytime: cap charge so PV surplus exports

# Minimum percentage cheaper to justify waiting for tomorrow's Tracker rate
TRACKER_DEFER_THRESHOLD = 0.90   # tomorrow must be < 90% of today (10%+ cheaper)

# Minimum import quantity - below this don't bother charging
MIN_IMPORT_KWH = 0.5

# Night export constants
# Note: pvPowerWatts reads 0W in Discharge ESS First mode (0x06) — the inverter
# suppresses PV. A PV-based night/day check is therefore permanently blind while
# exporting. Sunrise is detected via today's forecast dawn_time instead.
NIGHT_EXPORT_BUFFER_KWH           = 1.0   # safety margin above dawn target before exporting
MIN_NIGHT_EXPORT_KWH              = 0.5   # minimum surplus to bother starting export
NIGHT_EXPORT_TOMORROW_CONFIDENCE  = 0.6   # 60% of correctedTomorrowKwh must cover daily load
                                          # (P50 bias-corrected; 0.6 tolerates 40% shortfall)
DAYTIME_WINDOW_HOURS              = 14    # hours after dawn during which export is blocked
                                          # dawn 07:00 + 14h = 21:00 → nighttime resumes
NIGHT_EXPORT_START_HOUR           = 0     # export only permitted from midnight (00:00 local).
                                          # 21:00-00:00: self-consume — house load naturally
                                          # drains the battery to a lower pre-dawn SOC.
                                          # 00:00-dawn: export surplus if viable.
                                          # Lower dawn SOC = more solar headroom = less clipping.

# Solar overflow constants (daytime forecast-based export)
# Mode stays 0x02 throughout — only HOLD_ESS_MAX_CHARGE register is reduced.
# PV is never suppressed; surplus that can't enter the battery flows to grid.
# The export rate is calculated dynamically each evaluation so the battery
# reaches exactly 100% SOC at dusk, exporting all genuine surplus to grid.
SOLAR_OVERFLOW_MIN_SURPLUS_KWH   = 0.3   # below this surplus don't bother exporting
SOLAR_OVERFLOW_MIN_CHARGE_W      = 200   # minimum charge cap floor (avoid writing 0W to register)
SOLAR_OVERFLOW_CAP_DEADBAND_W    = 500   # only rewrite charge limit if cap changes by > this
SOLAR_DUSK_THRESHOLD_WH          = 500   # Wh/hr below which a slot is considered post-dusk
SOLAR_OVERFLOW_MIN_DAWN_MARGIN   = 3.5   # kWh margin required above dawn target before exporting
                                          # e.g. target=3.5 kWh → need 7.0 kWh (20% SOC) at dawn
                                          # guards against bias_factor over-inflation and keeps
                                          # charge that's more valuable at night (20p) than
                                          # earned by export (12p)


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
    health_cutoff_pct: float = 1.0     # hardware discharge floor
    export_enabled:    bool  = False   # export MPAN active
    max_export_kw:     float = 4.0     # DNO export cap (kW)

    # Live inverter readings
    pv_watts:               int   = 0     # current PV generation (W)
    house_load_watts:       int   = 0     # current home consumption (W) = PV + grid - battery
    export_active:          bool  = False # night export currently running
    corrected_tomorrow_kwh: float = 0.0   # bias-corrected forecast for tomorrow (kWh)
    bias_factor:            float = 1.0   # forecast bias correction factor (applied to hourly values)

    # Tariff data
    tariff: TariffData = field(default_factory=TariffData)

    # Forecast: hourly Wh dicts {"YYYY-MM-DD HH:00:00": wh_int}
    forecast_p50: Dict[str, int] = field(default_factory=dict)

    # Dawn times: {"YYYY-MM-DD": datetime} - first hour with meaningful PV
    dawn_times: Dict[str, datetime] = field(default_factory=dict)

    # Consumption profile: 48 half-hourly floats (kWh per slot)
    consumption_profile: List[float] = field(default_factory=list)

    # Current time
    now: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    # VPP active - when True all battery commands are suppressed
    vpp_active: bool = False

    # VPP reserve: kWh to protect from night export for an upcoming event.
    # Non-zero when state is ANNOUNCED or PRE_CHARGING. Pre-computed by plugin.py
    # so battery_manager.py stays stateless and free of VPP constants.
    vpp_reserved_kwh: float = 0.0

    # Solar overflow state (from plugin.py store — passed in so manager is stateless)
    solar_overflow_active:     bool = False  # charge cap currently applied
    solar_overflow_charge_cap: int  = 0      # current cap in watts


@dataclass
class DawnViability:
    """Result of the dawn viability check."""
    viable:           bool  = True
    soc_at_dawn_kwh:  float = 0.0    # clamped to health floor for display
    raw_soc_at_dawn:  float = 0.0    # actual projection (may be below health floor)
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

        # Step 4: Daytime solar overflow — cap charge so PV surplus exports to grid
        # Only checked when export_enabled (export MPAN active); no point capping
        # charge if there is nowhere for the surplus to go. Viability is passed so
        # the overflow logic can verify there is enough dawn margin before exporting
        # (export earns 12p/kWh; importing at night costs 20p+ — a net loss).
        if snapshot.export_enabled:
            overflow_decision = self._check_solar_overflow(snapshot, viability)
            if overflow_decision is not None:
                return overflow_decision

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

        Dawn time comes from snapshot.dawn_times, which is the first half-hourly
        slot with P50 forecast > PV_GENERATION_THRESHOLD_W. Overnight drain is
        estimated from the 48-slot half-hourly consumption profile.
        """
        cap_kwh          = snapshot.capacity_kwh
        current_soc_kwh  = snapshot.current_soc_pct / 100.0 * cap_kwh
        dawn_target_kwh  = snapshot.dawn_target_pct / 100.0 * cap_kwh
        health_floor_kwh = snapshot.health_cutoff_pct / 100.0 * cap_kwh
        now              = snapshot.now

        # Find next dawn: the nearest future dawn in the forecast.
        # Always scan today → tomorrow → day-after so we never land on a
        # UTC date that is a full calendar day ahead of the local BST date.
        # This matters between 23:00 BST and 00:00 BST (= 00:00-01:00 UTC)
        # when the UTC date has rolled over but the local date has not —
        # without this guard the code looks up UTC "tomorrow" dawn which
        # is ~27 hours away rather than the correct 3-4 hours away.
        today_str = now.date().strftime("%Y-%m-%d")   # kept for daytime credit below
        dawn_dt   = None
        for _days in range(3):
            _candidate_dt = snapshot.dawn_times.get(
                (now.date() + timedelta(days=_days)).strftime("%Y-%m-%d")
            )
            if _candidate_dt is not None and _candidate_dt > now:
                dawn_dt = _candidate_dt
                break

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

        # During daytime, credit remaining solar so import is not triggered
        # while PV is actively charging the battery. Net solar (forecast minus
        # home use from now to dusk) is added to current SOC before projecting
        # to dawn — capped at battery capacity to avoid overcounting.
        today_p50 = {
            k: v for k, v in snapshot.forecast_p50.items()
            if k.startswith(today_str)
        }
        _today_dawn_dt = snapshot.dawn_times.get(today_str)
        _is_daytime    = (
            _today_dawn_dt is not None
            and now >= _today_dawn_dt
            and (now - _today_dawn_dt).total_seconds() < DAYTIME_WINDOW_HOURS * 3600
        )

        if _is_daytime and today_p50:
            try:
                import pytz
                _tz = pytz.timezone("Europe/London")
                now_local = now.astimezone(_tz)
            except (ImportError, Exception):
                now_local = now
            now_hour_naive = now_local.replace(minute=0, second=0,
                                               microsecond=0, tzinfo=None)

            # Find dusk = last hour above threshold
            dusk_hour_dt = None
            for key in sorted(today_p50.keys(), reverse=True):
                if today_p50[key] >= SOLAR_DUSK_THRESHOLD_WH:
                    try:
                        dusk_hour_dt = datetime.strptime(key, "%Y-%m-%d %H:%M:%S")
                        break
                    except ValueError:
                        continue

            if dusk_hour_dt is not None and dusk_hour_dt > now_hour_naive:
                remaining_solar_kwh = sum(
                    wh / 1000.0
                    for k, wh in today_p50.items()
                    if now_hour_naive
                       <= datetime.strptime(k, "%Y-%m-%d %H:%M:%S")
                       <= dusk_hour_dt
                ) * snapshot.bias_factor

                now_slot  = now_local.hour * 2 + (1 if now_local.minute >= 30 else 0)
                dusk_slot = min(dusk_hour_dt.hour * 2 + 2, 48)
                if len(snapshot.consumption_profile) == 48:
                    home_to_dusk_kwh = sum(
                        snapshot.consumption_profile[now_slot:dusk_slot]
                    )
                else:
                    home_to_dusk_kwh = 0.225 * max(
                        0.0, (dusk_hour_dt - now_hour_naive).total_seconds() / 3600.0
                    ) * 2

                net_solar_kwh = max(0.0, remaining_solar_kwh - home_to_dusk_kwh)
                current_soc_kwh = min(cap_kwh, current_soc_kwh + net_solar_kwh)

        # Projected SOC at dawn (raw, before hardware floor)
        raw_soc_at_dawn = current_soc_kwh - expected_kwh

        # Import threshold: depends on whether tomorrow has meaningful solar.
        #
        # Tomorrow is sunny (forecast >= daily consumption):
        #   The battery will refill during the day. No import needed for the
        #   dawn_target buffer — UNLESS the projected raw SOC at dawn is below
        #   the health floor itself. In that case apply an emergency import to
        #   reach the floor. We cannot rely solely on register 40048 because it
        #   may not be at the expected value (e.g. factory default 5%, not 10%).
        #
        # Tomorrow is a poor solar day (forecast < daily consumption):
        #   The battery will not recover fully during the day. Maintain the full
        #   dawn_target buffer so the house can run through the following night.
        daily_cons_kwh = (
            sum(snapshot.consumption_profile)
            if len(snapshot.consumption_profile) == 48
            else 10.8
        )
        tomorrow_is_sunny = snapshot.corrected_tomorrow_kwh >= daily_cons_kwh

        if tomorrow_is_sunny:
            if raw_soc_at_dawn < health_floor_kwh:
                # Emergency floor: battery would breach hardware health limit.
                # Import just enough to reach the floor, even on a sunny day.
                import_needed   = True
                import_kwh_net  = max(0.0, health_floor_kwh - raw_soc_at_dawn)
                import_kwh_grid = import_kwh_net / max(0.01, snapshot.efficiency)
            else:
                import_needed   = False
                import_kwh_net  = 0.0
                import_kwh_grid = 0.0
        else:
            import_needed   = raw_soc_at_dawn < dawn_target_kwh
            import_kwh_net  = max(0.0, dawn_target_kwh - raw_soc_at_dawn)
            import_kwh_grid = import_kwh_net / max(0.01, snapshot.efficiency)

        # Clamp reported value to hardware floor for display; raw value logged by caller
        soc_at_dawn_kwh = max(health_floor_kwh, raw_soc_at_dawn)

        return DawnViability(
            viable                   = not import_needed,
            soc_at_dawn_kwh          = round(soc_at_dawn_kwh, 2),
            raw_soc_at_dawn          = round(raw_soc_at_dawn, 2),
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

        # Correct target: charge enough so that after overnight drain we reach dawn_target.
        #   target_kwh = dawn_target + expected_drain
        #   target_soc = target_kwh / cap * 100 + 2%  (2% safety buffer above minimum)
        # Capped at 98% (preserve solar headroom at full charge).
        # Floored at dawn_target_pct + 2% = 17% to guard against zero-drain edge cases.
        target_kwh = viability.dawn_target_kwh + viability.expected_consumption_kwh
        target_soc = min(98.0, target_kwh / max(1.0, snapshot.capacity_kwh) * 100.0 + 2.0)
        target_soc = max(target_soc, snapshot.dawn_target_pct + 2.0)

        # Defensive guard: if battery already meets the import target, return immediately.
        # With the corrected formula above, target_soc accounts for overnight drain, so
        # this guard only fires when there is a genuine inconsistency between the viability
        # calculation (which flagged "import needed") and the actual battery state — e.g.
        # if a buggy dawn-time lookup produced an inflated drain estimate while the battery
        # is comfortably safe.  This prevents runaway start/stop import cycling.
        if snapshot.current_soc_pct >= target_soc:
            return Decision(
                action          = ACTION_SELF_CONSUMPTION,
                reason          = (f"Import target {target_soc:.0f}% already met "
                                   f"({snapshot.current_soc_pct:.1f}% SOC) — no import needed"),
                dawn_viable     = True,
                soc_at_dawn_kwh = viability.soc_at_dawn_kwh,
            )

        # Tariff-specific import timing
        if tariff.tariff_key in (TARIFF_GO, TARIFF_FLUX, TARIFF_IGO, TARIFF_IFLUX):
            return self._plan_tou_import(snapshot, viability, target_soc)

        if tariff.tariff_key == TARIFF_TRACKER:
            return self._plan_tracker_import(snapshot, viability, target_soc)

        if tariff.tariff_key == TARIFF_AGILE:
            return self._plan_agile_import(snapshot, viability, target_soc)

        if tariff.tariff_key == TARIFF_FLEXIBLE:
            return self._plan_flexible_import(snapshot, viability, target_soc)

        # Unknown tariff - import now at half inverter power
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

    def _plan_flexible_import(
        self, snapshot: ManagerSnapshot, viability: DawnViability, target_soc: float
    ) -> Decision:
        """Plan import on Flexible Octopus (flat rate, no time-of-use windows).

        Flexible Octopus is a simple flat rate with no cheap period — there is
        no benefit in delaying, so import immediately when dawn viability is at risk.
        Uses the rate from today_rate_p for logging if available.
        """
        tariff   = snapshot.tariff
        rate_str = f"{tariff.today_rate_p:.2f}p/kWh" if tariff.today_rate_p else "flat rate"
        return Decision(
            action         = ACTION_START_IMPORT,
            reason         = (
                f"Dawn risk ({viability.soc_at_dawn_kwh:.1f} kWh at dawn, "
                f"need {viability.dawn_target_kwh:.1f}). "
                f"Importing now at Flexible {rate_str}"
            ),
            power_watts    = 10000,
            target_soc_pct = target_soc,
        )

    # ================================================================
    # Night Export
    # ================================================================

    def _check_solar_overflow(
        self, snapshot: ManagerSnapshot, viability: DawnViability
    ) -> Optional[Decision]:
        """Dynamic daytime export: cap battery charge so PV fills battery to 100% at dusk.

        Algorithm:
          0. Dawn margin gate: only export if projected SOC at dawn (from viability,
             which already credits remaining today's solar) exceeds the dawn target by
             SOLAR_OVERFLOW_MIN_DAWN_MARGIN kWh. This prevents exporting at 12p/kWh
             when the battery may be needed overnight at 20p+/kWh.
          1. Find dusk = last hour in today's P50 forecast above SOLAR_DUSK_THRESHOLD_WH
          2. Sum remaining bias-corrected solar from now to dusk
          3. Sum expected home consumption from now to dusk (from consumption profile)
          4. Battery headroom = kWh needed to reach 100% SOC
          5. surplus_kwh = remaining_solar - remaining_home - headroom
          6. If surplus < SOLAR_OVERFLOW_MIN_SURPLUS_KWH: solar can fill battery without
             any export — no cap applied. (Replaces the old fixed 40% SOC gate: if there
             is genuine forecast surplus the battery will fill regardless of current SOC,
             so export can start from any SOC when solar clearly exceeds capacity.)
          7. required_charge_kw = headroom / hours_to_dusk  (fills battery exactly at dusk)
             export_kw = min(max(0, pv_now - house_now - required_charge_kw), DNO cap)
          8. charge_cap_w = max(MIN, pv_now - house_now - export_target_w)

        Mode stays 0x02 (Max Self Consumption) throughout. Only HOLD_ESS_MAX_CHARGE
        is reduced. PV is never suppressed; surplus flows to grid via export connection.
        Switching to mode 0x06 (Discharge ESS First) would suppress PV — wrong approach.

        Only active during daytime. Only called when export_enabled is True (MPAN active).
        """
        # ── 0. Dawn margin gate ───────────────────────────────────────────────
        # Don't export if the projected SOC at dawn isn't comfortably above the
        # target. The margin (default 3.5 kWh = 10% battery) means we need the
        # battery at 20%+ SOC at dawn before we'll give away daytime kWh at 12p.
        # This protects against bias_factor over-inflation (raw_soc_at_dawn may
        # look fine at 1.5x bias but be borderline at the true generation level).
        dawn_margin = viability.raw_soc_at_dawn - viability.dawn_target_kwh
        if dawn_margin < SOLAR_OVERFLOW_MIN_DAWN_MARGIN:
            if snapshot.solar_overflow_active:
                return Decision(
                    action      = ACTION_SELF_CONSUMPTION,
                    reason      = (
                        f"Solar overflow: dawn margin {dawn_margin:.1f} kWh below "
                        f"{SOLAR_OVERFLOW_MIN_DAWN_MARGIN:.1f} kWh threshold — "
                        f"holding charge (12p export < 20p+ overnight import)"
                    ),
                    dawn_viable = True,
                )
            return None
        # ── 1. Daytime check ──────────────────────────────────────────────────
        try:
            import pytz
            _tz        = pytz.timezone("Europe/London")
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

        if not _is_daytime:
            if snapshot.solar_overflow_active:
                return Decision(
                    action      = ACTION_SELF_CONSUMPTION,
                    reason      = "Solar overflow: night — releasing charge cap",
                    dawn_viable = True,
                )
            return None

        # ── 2. Find dusk from today's forecast ───────────────────────────────
        now_naive  = _local_now.replace(tzinfo=None)
        now_hour   = now_naive.replace(minute=0, second=0, microsecond=0)

        today_p50 = {
            k: v for k, v in snapshot.forecast_p50.items()
            if k.startswith(_today_str)
        }

        if not today_p50:
            if snapshot.solar_overflow_active:
                return Decision(
                    action      = ACTION_SELF_CONSUMPTION,
                    reason      = "Solar overflow: no forecast data — releasing cap",
                    dawn_viable = True,
                )
            return None

        # Last hour of the day with meaningful PV
        dusk_hour_dt = None
        for key in sorted(today_p50.keys(), reverse=True):
            if today_p50[key] >= SOLAR_DUSK_THRESHOLD_WH:
                try:
                    dusk_hour_dt = datetime.strptime(key, "%Y-%m-%d %H:%M:%S")
                    break
                except ValueError:
                    continue

        if dusk_hour_dt is None or dusk_hour_dt <= now_hour:
            if snapshot.solar_overflow_active:
                return Decision(
                    action      = ACTION_SELF_CONSUMPTION,
                    reason      = "Solar overflow: at or past dusk — releasing cap",
                    dawn_viable = True,
                )
            return None

        hours_to_dusk = max(0.5, (dusk_hour_dt - now_hour).total_seconds() / 3600.0)

        # ── 3. Remaining bias-corrected solar (now → dusk) ───────────────────
        remaining_solar_kwh = 0.0
        for key, wh in today_p50.items():
            try:
                key_dt = datetime.strptime(key, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue
            if now_hour <= key_dt <= dusk_hour_dt:
                remaining_solar_kwh += wh / 1000.0
        remaining_solar_kwh *= snapshot.bias_factor

        # ── 4. Remaining home consumption (now → dusk, from 48-slot profile) ─
        now_slot  = _local_now.hour * 2 + (1 if _local_now.minute >= 30 else 0)
        dusk_slot = min(dusk_hour_dt.hour * 2 + 2, 48)   # +2 slots covers the full dusk hour
        if len(snapshot.consumption_profile) == 48:
            remaining_home_kwh = sum(snapshot.consumption_profile[now_slot:dusk_slot])
        else:
            remaining_home_kwh = 0.225 * hours_to_dusk * 2  # fallback: ~10 kWh/day flat

        # ── 5. Battery headroom to reach 100% ────────────────────────────────
        headroom_kwh = (100.0 - snapshot.current_soc_pct) / 100.0 * snapshot.capacity_kwh

        # ── 6. Surplus calculation ────────────────────────────────────────────
        net_to_battery = remaining_solar_kwh - remaining_home_kwh
        surplus_kwh    = net_to_battery - headroom_kwh

        if surplus_kwh < SOLAR_OVERFLOW_MIN_SURPLUS_KWH:
            # Solar won't fill battery — every watt goes to charging
            if snapshot.solar_overflow_active:
                return Decision(
                    action      = ACTION_SELF_CONSUMPTION,
                    reason      = (
                        f"Solar overflow: {remaining_solar_kwh:.1f} kWh remaining solar - "
                        f"{remaining_home_kwh:.1f} kWh home = {net_to_battery:.1f} kWh net, "
                        f"need {headroom_kwh:.1f} kWh headroom — no surplus, releasing cap"
                    ),
                    dawn_viable = True,
                )
            return None

        # ── 7. Real-time export: give battery exactly enough charge to fill by dusk ──
        # required_charge_kw = rate at which battery must charge to reach 100% at dusk.
        # Everything above that in the real-time PV surplus is exported immediately.
        # This starts exporting much earlier on sunny days vs the old "spread forecast
        # surplus evenly" approach (e.g. 2.3 kW from 10am vs waiting until 1pm).
        required_charge_kw = headroom_kwh / hours_to_dusk
        pv_surplus_kw      = max(0.0, (snapshot.pv_watts - snapshot.house_load_watts) / 1000.0)
        export_kw          = min(
            max(0.0, pv_surplus_kw - required_charge_kw),
            snapshot.max_export_kw,
        )
        export_w           = int(export_kw * 1000)

        # ── 8. Charge cap: battery absorbs what's left after export target ────
        # Floor at MIN to avoid writing 0 to register (ambiguous on some inverters)
        cap_w = max(
            SOLAR_OVERFLOW_MIN_CHARGE_W,
            snapshot.pv_watts - snapshot.house_load_watts - export_w,
        )

        return Decision(
            action           = ACTION_SOLAR_OVERFLOW,
            reason           = (
                f"Solar overflow: {surplus_kwh:.1f} kWh forecast surplus\n"
                f"Req charge {required_charge_kw:.2f} kW  |  "
                f"PV surplus {pv_surplus_kw:.2f} kW  |  "
                f"Export {export_kw:.2f} kW  |  Cap {cap_w}W\n"
                f"Solar {remaining_solar_kwh:.1f} kWh  |  "
                f"Home {remaining_home_kwh:.1f} kWh  |  "
                f"Headroom {headroom_kwh:.1f} kWh  |  "
                f"{hours_to_dusk:.1f}h to dusk"
            ),
            power_watts      = cap_w,
            export_kw        = export_kw,
            dawn_viable      = True,
            soc_at_dawn_kwh  = viability.soc_at_dawn_kwh,
        )

    def _check_night_export(
        self, snapshot: ManagerSnapshot, viability: DawnViability
    ) -> Optional[Decision]:
        """Check if conditions are right for night export via force-discharge.

        All three conditions must hold:
        1. Before sunrise — checked against today's forecast dawn time.
           pvPowerWatts cannot be used: in Discharge ESS First mode (0x06)
           the inverter suppresses PV to 0W regardless of actual solar.
        2. Battery surplus above dawn floor + safety buffer
        3. Tomorrow forecast solar (at 60% confidence) >= expected daily consumption

        Returns a Decision or None if conditions not met.
        """
        # Condition 1: Night only — stop/block export once sunrise is reached.
        # Dawn time for today comes from the forecast in snapshot.dawn_times.
        # Export is blocked for DAYTIME_WINDOW_HOURS after dawn (covers full daylight).
        try:
            import pytz
            _tz = pytz.timezone("Europe/London")
            _local_now = snapshot.now.astimezone(_tz)
        except (ImportError, Exception):
            _tz = None
            _local_now = snapshot.now

        _today_str     = _local_now.date().strftime("%Y-%m-%d")
        _today_dawn_dt = snapshot.dawn_times.get(_today_str)
        _local_hour    = _local_now.hour + _local_now.minute / 60.0

        if _today_dawn_dt is not None:
            _is_daytime = (
                snapshot.now >= _today_dawn_dt
                and (snapshot.now - _today_dawn_dt).total_seconds() < DAYTIME_WINDOW_HOURS * 3600
            )
        else:
            # No forecast dawn time for today — use clock-based fallback.
            # Safe assumption: 07:00–21:00 local time is always daytime.
            # This prevents night export from firing during daylight when
            # forecast data is unavailable (e.g. fetch failure, first startup).
            _is_daytime = 7.0 <= _local_hour < 21.0

        if _is_daytime:
            if snapshot.export_active:
                if _today_dawn_dt is not None and _tz is not None:
                    _dawn_str = _today_dawn_dt.astimezone(_tz).strftime("%H:%M")
                elif _today_dawn_dt is not None:
                    _dawn_str = _today_dawn_dt.strftime("%H:%M")
                else:
                    _dawn_str = "07:00 (fallback)"
                return Decision(
                    action          = ACTION_STOP_EXPORT,
                    reason          = f"Sunrise reached ({_dawn_str}) - stopping night export",
                    dawn_viable     = viability.viable,
                    soc_at_dawn_kwh = viability.soc_at_dawn_kwh,
                )
            return None

        # Block export before midnight (21:00-23:59). Let the house load drain the
        # battery naturally in the early evening — it arrives at midnight at a lower
        # SOC than if export had run from 21:00. From 00:00 onwards, if there is still
        # a genuine surplus above the dawn floor, export runs until sunrise.
        # This maximises solar headroom: a lower dawn SOC absorbs more morning PV
        # before the battery hits 100% and PV clipping begins (export cap 4 kW).
        # Storm watch already protects resilience by raising dawn_target_pct.
        _pre_midnight = _local_hour >= 21.0   # 21:00-23:59 — wait for midnight
        if _pre_midnight:
            if snapshot.export_active:
                return Decision(
                    action          = ACTION_STOP_EXPORT,
                    reason          = (
                        f"Night export: before midnight ({_local_now.strftime('%H:%M')}) — "
                        f"self-consuming until 00:00 to lower dawn SOC"
                    ),
                    dawn_viable     = viability.viable,
                    soc_at_dawn_kwh = viability.soc_at_dawn_kwh,
                )
            return None

        # Calculate surplus above dawn floor
        cap_kwh          = snapshot.capacity_kwh
        current_soc_kwh  = snapshot.current_soc_pct / 100.0 * cap_kwh
        drain_to_dawn    = viability.expected_consumption_kwh
        projected_dawn   = current_soc_kwh - drain_to_dawn

        # VPP reserve: if an event is announced for tomorrow morning, protect
        # that energy from being exported overnight. The reserve is zero when
        # no event is pending (vpp_reserved_kwh defaults to 0.0).
        vpp_reserve      = snapshot.vpp_reserved_kwh
        surplus_kwh      = projected_dawn - viability.dawn_target_kwh - NIGHT_EXPORT_BUFFER_KWH - vpp_reserve

        # Condition 2: Battery has surplus worth exporting
        if surplus_kwh < MIN_NIGHT_EXPORT_KWH:
            if snapshot.export_active:
                vpp_note = (
                    f" + {vpp_reserve:.1f} kWh VPP reserve" if vpp_reserve > 0 else ""
                )
                return Decision(
                    action          = ACTION_STOP_EXPORT,
                    reason          = (
                        f"Night export: projected dawn {projected_dawn:.1f} kWh approaching "
                        f"floor ({viability.dawn_target_kwh:.1f} + {NIGHT_EXPORT_BUFFER_KWH:.1f} kWh buffer"
                        f"{vpp_note}) - stopping"
                    ),
                    dawn_viable     = viability.viable,
                    soc_at_dawn_kwh = viability.soc_at_dawn_kwh,
                )
            return None

        # Condition 3: Tomorrow P50 (bias-corrected) at 60% confidence >= daily consumption
        # Using correctedTomorrowKwh rather than P10 because P10 (10th percentile) is
        # too pessimistic — it blocks export even on days that clearly will be sunny.
        # At 60% confidence we tolerate a 40% shortfall below the forecast's best estimate,
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
        vpp_note = (
            f", {vpp_reserve:.1f} kWh VPP reserve held" if vpp_reserve > 0 else ""
        )
        return Decision(
            action          = ACTION_START_EXPORT,
            reason          = (
                f"Night export: {surplus_kwh:.1f} kWh surplus above dawn floor"
                f"{vpp_note}. "
                f"Tomorrow forecast {snapshot.corrected_tomorrow_kwh:.1f} kWh "
                f"(60% = {tomorrow_viable_kwh:.1f}) >= daily {daily_cons_kwh:.1f} kWh. "
                f"Exporting {export_kw:.1f} kW"
            ),
            power_watts     = int(export_kw * 1000),
            export_kw       = export_kw,
            dawn_viable     = True,
            soc_at_dawn_kwh = viability.soc_at_dawn_kwh,
        )

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
