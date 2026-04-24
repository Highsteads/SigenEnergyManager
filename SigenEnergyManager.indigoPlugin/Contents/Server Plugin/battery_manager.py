#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    battery_manager.py
# Description: 24-hour sufficiency model — export surplus today, import only
#              when tomorrow's battery+solar falls short of tomorrow's daily load.
#              No overnight forced discharge.
# Author:      CliveS & Claude Sonnet 4.6
# Date:        24-04-2026
# Version:     3.0

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
        "igo":   {"cheap_start": "23:30", "cheap_end": "05:30"},   # 23:30-05:30 (6h)
        "iflux": {"cheap_start": "19:00", "cheap_end": "16:00"},   # 21h non-peak window
    }


# ============================================================
# Decision action constants
# ============================================================

ACTION_SELF_CONSUMPTION = "self_consumption"   # default: battery covers home load
ACTION_START_IMPORT     = "start_import"        # begin charging from grid now
ACTION_STOP_IMPORT      = "stop_import"         # charging complete - return to self_consumption
ACTION_SCHEDULE_IMPORT  = "schedule_import"     # defer import to a cheaper/later window
ACTION_START_EXPORT     = "start_export"        # deprecated in v4.0 — night discharge removed
ACTION_STOP_EXPORT      = "stop_export"         # stop active legacy export (v3.x migration)
ACTION_SOLAR_OVERFLOW   = "solar_overflow"      # daytime: cap charge so PV surplus exports

# Minimum percentage cheaper to justify waiting for tomorrow's Tracker rate
TRACKER_DEFER_THRESHOLD = 0.90   # tomorrow must be < 90% of today (10%+ cheaper)

# Minimum import quantity — below this don't bother charging
MIN_IMPORT_KWH = 0.5

# Minimum 24h surplus before daytime export is allowed.
# Below this the battery has barely enough for 24h — every kWh is worth more
# overnight (20p+) than as daytime export (12p flat).
MIN_EXPORT_KWH = 0.3

# Solar overflow constants (daytime forecast-based export)
# Mode stays 0x02 (Max Self Consumption) throughout.
# Only HOLD_ESS_MAX_CHARGE register is reduced — PV is never suppressed.
SOLAR_OVERFLOW_MIN_CHARGE_W   = 200   # minimum charge cap floor (avoid writing 0W to register)
SOLAR_OVERFLOW_CAP_DEADBAND_W = 500   # only rewrite limit if cap changes by > this
SOLAR_DUSK_THRESHOLD_WH       = 500   # Wh/hr below which a slot is considered post-dusk


# ============================================================
# Data classes
# ============================================================

@dataclass
class TariffData:
    """Tariff-related information passed to the decision engine."""
    tariff_key:       str   = TARIFF_TRACKER
    today_rate_p:     Optional[float] = None   # pence/kWh
    tomorrow_rate_p:  Optional[float] = None   # pence/kWh (may be None until ~16:00)
    cheap_start:      Optional[str]   = None   # "HH:MM" local time (Go/Flux cheap window)
    cheap_end:        Optional[str]   = None   # "HH:MM"
    cheap_rate_p:     Optional[float] = None   # cheap window rate (Go/Flux)
    agile_slots:      List[Tuple[datetime, float]] = field(default_factory=list)


@dataclass
class ManagerSnapshot:
    """Complete system snapshot passed to BatteryManager.evaluate()."""
    # Battery state
    current_soc_pct:    float = 0.0
    capacity_kwh:       float = 35.04
    efficiency:         float = 0.94     # round-trip, for import quantity calc

    # Manager settings
    dawn_target_pct:    float = 10.0     # deprecated in v4.0 (kept for plugin.py compat)
    health_cutoff_pct:  float = 1.0      # hardware discharge floor
    export_enabled:     bool  = False    # export MPAN active
    max_export_kw:      float = 4.0      # DNO export cap (kW)

    # Daily consumption estimates (24h sufficiency model)
    weekday_kwh:        float = 22.0     # Mon-Fri daily load
    weekend_kwh:        float = 30.0     # Sat-Sun daily load (washing, cooking, oven)

    # Live inverter readings
    pv_watts:               int   = 0
    house_load_watts:       int   = 0
    export_active:          bool  = False   # legacy night export still running (v3.x)
    corrected_tomorrow_kwh: float = 0.0     # bias-corrected forecast for tomorrow (kWh)
    bias_factor:            float = 1.0     # forecast correction (applied to hourly values)

    # Tariff data
    tariff: TariffData = field(default_factory=TariffData)

    # Forecast: hourly Wh dicts {"YYYY-MM-DD HH:00:00": wh_int}
    forecast_p50: Dict[str, int] = field(default_factory=dict)

    # Dawn times: {"YYYY-MM-DD": datetime} — first hour with meaningful PV
    dawn_times: Dict[str, datetime] = field(default_factory=dict)

    # Consumption profile: 48 half-hourly floats (kWh per slot)
    consumption_profile: List[float] = field(default_factory=list)

    # Current time
    now: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    # VPP active — when True all battery commands are suppressed
    vpp_active: bool = False

    # VPP reserve: kWh to protect from export for an upcoming event
    vpp_reserved_kwh: float = 0.0

    # Solar overflow state (from plugin.py store — passed in so manager is stateless)
    solar_overflow_active:     bool = False   # charge cap currently applied
    solar_overflow_charge_cap: int  = 0       # current cap in watts


@dataclass
class SufficiencyBalance:
    """Result of the 24-hour sufficiency check.

    Replaces DawnViability (v3.x). Models energy over a full 24-hour horizon:
      surplus_kwh  = battery + remaining_solar_today  - need_24h
      import check = battery_at_dawn + tomorrow_solar < tomorrow_need
    """
    # Current state
    battery_kwh:              float = 0.0    # current battery energy (kWh)
    remaining_solar_kwh:      float = 0.0    # bias-corrected solar remaining today (kWh)
    remaining_home_to_dusk_kwh: float = 0.0  # home consumption from now to dusk (kWh)
    is_daytime:               bool  = False  # True between dawn and dusk
    dusk_dt:                  Optional[datetime] = None
    dusk_slot:                int   = 48     # 48-slot profile index for end of daytime
    hours_to_dusk:            float = 0.0

    # 24-hour demand and surplus
    need_24h_kwh:             float = 22.0   # energy needed for next 24h (weekday/weekend)
    surplus_kwh:              float = 0.0    # battery + remaining_solar - need_24h
                                             # positive = export eligible

    # Tomorrow planning
    battery_at_dawn_kwh:      float = 0.0    # projected battery at next dawn (kWh)
    tomorrow_solar_kwh:       float = 0.0    # tomorrow's corrected forecast (kWh)
    available_tomorrow_kwh:   float = 0.0    # battery_at_dawn + tomorrow_solar
    tomorrow_need_kwh:        float = 22.0   # tomorrow's expected load (weekday/weekend)
    import_kwh:               float = 0.0    # net energy deficit at battery terminals
    import_kwh_grid:          float = 0.0    # energy from grid (= import_kwh / efficiency)
    import_needed:            bool  = False

    # Dawn planning
    dawn_dt:                  Optional[datetime] = None
    hours_to_dawn:            float = 8.0
    expected_overnight_kwh:   float = 0.0    # consumption from now to dawn


@dataclass
class Decision:
    """Battery management decision returned by BatteryManager.evaluate()."""
    action:          str   = ACTION_SELF_CONSUMPTION
    reason:          str   = ""
    power_watts:     int   = 0
    target_soc_pct:  float = 0.0
    scheduled_time:  Optional[datetime] = None   # for deferred imports
    dawn_viable:     bool  = True
    soc_at_dawn_kwh: float = 0.0
    import_kwh:      float = 0.0
    export_kw:       float = 0.0    # kW being exported (solar overflow)


# ============================================================
# BatteryManager
# ============================================================

class BatteryManager:
    """24-hour sufficiency battery decision engine (v4.0).

    Philosophy:
    1. Export surplus today: if battery + remaining solar exceeds today's 24h
       consumption, cap charge rate so excess PV flows to grid via the export
       connection. Export starts early on sunny days rather than waiting for 90%.
    2. Import only for tomorrow — and only when there is a rate benefit:
       - TOU tariffs (Go/Flux/Agile): import during cheap window. Rate saving
         (15-20p/kWh) far outweighs 6% round-trip conversion loss (~1.4p/kWh).
       - Flat-rate tariffs (Tracker/Flexible): do NOT pre-charge battery.
         When battery is low, the inverter imports direct from grid to house with
         ZERO conversion loss. Pre-charging wastes ~6% at no rate benefit.
         Exception: defer to 00:05 if tomorrow's Tracker rate is 10%+ cheaper.
    3. No overnight force-discharge: stays in Self Consumption mode (0x02) always.
    4. VPP events override all decisions (Axle cloud controls battery).

    This class is stateless: it takes a ManagerSnapshot and returns a Decision.
    All state is managed by plugin.py.
    """

    def evaluate(self, snapshot: ManagerSnapshot) -> Decision:
        """Main entry point — evaluate system state and return a decision."""
        # VPP takes precedence — suspend all manager actions
        if snapshot.vpp_active:
            return Decision(
                action = ACTION_SELF_CONSUMPTION,
                reason = "VPP event active — Axle has control",
            )

        # Stop any legacy night export still running from v3.x (migration safety)
        if snapshot.export_active:
            return Decision(
                action      = ACTION_STOP_EXPORT,
                reason      = "Night export disabled in v4.0 — returning to self-consumption",
                dawn_viable = True,
            )

        # Calculate 24-hour sufficiency balance
        balance = self._calculate_24h_balance(snapshot)

        # Import takes priority: ensure tomorrow is covered before exporting today
        if balance.import_needed:
            decision = self._plan_import(snapshot, balance)
            # Only flag dawn as not viable when we're actually importing.
            # Flat-rate planners (Tracker/Flexible) may return self_consumption
            # intentionally — grid passthrough is more efficient than pre-charging.
            if decision.action in (ACTION_START_IMPORT, ACTION_SCHEDULE_IMPORT):
                decision.dawn_viable = False
            decision.soc_at_dawn_kwh = balance.battery_at_dawn_kwh
            decision.import_kwh      = balance.import_kwh_grid
            return decision

        # Daytime solar overflow: export surplus PV that would otherwise be clipped
        if snapshot.export_enabled:
            overflow = self._check_solar_overflow(snapshot, balance)
            if overflow is not None:
                return overflow

        # Release charge cap if previously applied but conditions no longer hold
        if snapshot.solar_overflow_active:
            return Decision(
                action          = ACTION_SELF_CONSUMPTION,
                reason          = (
                    f"Solar overflow: conditions no longer met — releasing charge cap "
                    f"(24h surplus {balance.surplus_kwh:.1f} kWh, "
                    f"daytime={balance.is_daytime})"
                ),
                dawn_viable     = True,
                soc_at_dawn_kwh = balance.battery_at_dawn_kwh,
            )

        return Decision(
            action          = ACTION_SELF_CONSUMPTION,
            reason          = (
                f"24h sufficient — surplus {balance.surplus_kwh:.1f} kWh | "
                f"tomorrow: {balance.available_tomorrow_kwh:.1f} kWh avail, "
                f"need {balance.tomorrow_need_kwh:.1f} kWh"
            ),
            dawn_viable     = True,
            soc_at_dawn_kwh = balance.battery_at_dawn_kwh,
        )

    # ================================================================
    # 24-Hour Sufficiency Check
    # ================================================================

    def _calculate_24h_balance(self, snapshot: ManagerSnapshot) -> SufficiencyBalance:
        """Calculate 24-hour energy balance and determine import/export eligibility.

        Two key outputs:
          surplus_kwh   — battery + remaining_solar - need_24h
                          Positive means we have more than 24h needs → export eligible
          import_needed — True if battery_at_dawn + tomorrow_solar < tomorrow_need
                          Need to import tonight to survive tomorrow

        Dawn is found by forward-scan (safe across BST/UTC midnight boundary).
        Dusk is the last P50 slot >= SOLAR_DUSK_THRESHOLD_WH today.
        """
        cap_kwh         = snapshot.capacity_kwh
        current_soc_kwh = snapshot.current_soc_pct / 100.0 * cap_kwh
        health_floor    = snapshot.health_cutoff_pct / 100.0 * cap_kwh
        now             = snapshot.now

        # ── Local time for day-of-week ──────────────────────────────────────
        try:
            import pytz
            _tz       = pytz.timezone("Europe/London")
            local_now = now.astimezone(_tz)
        except (ImportError, Exception):
            local_now = now

        today_str = local_now.date().strftime("%Y-%m-%d")

        # ── Daily consumption estimate (today and tomorrow) ─────────────────
        day_of_week       = local_now.weekday()       # 0=Mon … 5=Sat, 6=Sun
        need_24h_kwh      = snapshot.weekend_kwh if day_of_week >= 5 else snapshot.weekday_kwh

        tomorrow_date     = local_now.date() + timedelta(days=1)
        tomorrow_weekday  = tomorrow_date.weekday()
        tomorrow_need_kwh = snapshot.weekend_kwh if tomorrow_weekday >= 5 else snapshot.weekday_kwh

        # ── Find next dawn (forward-scan prevents BST/UTC date mismatch) ────
        dawn_dt = None
        for _days in range(3):
            _candidate = snapshot.dawn_times.get(
                (now.date() + timedelta(days=_days)).strftime("%Y-%m-%d")
            )
            if _candidate is not None and _candidate > now:
                dawn_dt = _candidate
                break

        if dawn_dt is None:
            dawn_dt = datetime(
                now.year, now.month, now.day, 7, 0, 0, tzinfo=now.tzinfo
            ) + timedelta(days=1)

        hours_to_dawn  = max(0.0, (dawn_dt - now).total_seconds() / 3600.0)
        overnight_kwh  = self._estimate_consumption_until(
            now, dawn_dt, snapshot.consumption_profile
        )

        # ── Daytime / dusk detection ────────────────────────────────────────
        today_p50      = {k: v for k, v in snapshot.forecast_p50.items()
                          if k.startswith(today_str)}
        today_dawn_dt  = snapshot.dawn_times.get(today_str)

        # Find dusk = last P50 slot START with meaningful PV
        dusk_hour_naive = None
        for key in sorted(today_p50.keys(), reverse=True):
            if today_p50[key] >= SOLAR_DUSK_THRESHOLD_WH:
                try:
                    dusk_hour_naive = datetime.strptime(key, "%Y-%m-%d %H:%M:%S")
                    break
                except ValueError:
                    continue

        # dusk_dt = end of the last meaningful solar hour (start + 1h), tz-aware
        dusk_dt = None
        if dusk_hour_naive is not None:
            try:
                import pytz
                _tz_l   = pytz.timezone("Europe/London")
                dusk_dt = _tz_l.localize(dusk_hour_naive + timedelta(hours=1))
            except (ImportError, Exception):
                dusk_dt = (dusk_hour_naive + timedelta(hours=1)).replace(tzinfo=timezone.utc)

        is_daytime = (
            today_dawn_dt is not None
            and dusk_dt is not None
            and now >= today_dawn_dt
            and now < dusk_dt
        )

        hours_to_dusk = 0.0
        if is_daytime and dusk_dt is not None:
            hours_to_dusk = max(0.5, (dusk_dt - now).total_seconds() / 3600.0)

        # Profile slot for the end of daytime (used in charge cap calculation)
        # Formula matches old _check_solar_overflow: dusk_hour_start * 2 + 2
        dusk_slot = min(dusk_hour_naive.hour * 2 + 2, 48) if dusk_hour_naive else 48

        # ── Remaining solar (now → dusk, bias-corrected) ────────────────────
        remaining_solar_kwh = 0.0
        if is_daytime and today_p50 and dusk_hour_naive is not None:
            now_naive = local_now.replace(tzinfo=None)
            now_hour  = now_naive.replace(minute=0, second=0, microsecond=0)
            for key, wh in today_p50.items():
                try:
                    key_dt = datetime.strptime(key, "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    continue
                if now_hour <= key_dt <= dusk_hour_naive:
                    remaining_solar_kwh += wh / 1000.0
            remaining_solar_kwh *= snapshot.bias_factor

        # ── 24h surplus (export eligibility) ───────────────────────────────
        surplus_kwh = current_soc_kwh + remaining_solar_kwh - need_24h_kwh

        # ── Home consumption from now to dusk (for solar overflow charge cap) ─
        now_slot = local_now.hour * 2 + (1 if local_now.minute >= 30 else 0)
        if is_daytime and len(snapshot.consumption_profile) == 48:
            remaining_home_to_dusk_kwh = sum(
                snapshot.consumption_profile[now_slot:dusk_slot]
            )
        elif is_daytime:
            remaining_home_to_dusk_kwh = 0.225 * hours_to_dusk * 2   # ~10 kWh/day flat
        else:
            remaining_home_to_dusk_kwh = 0.0

        # ── Battery at dawn ─────────────────────────────────────────────────
        if is_daytime:
            # Remaining solar (net of home load) charges the battery during the day
            net_to_battery  = max(0.0, remaining_solar_kwh - remaining_home_to_dusk_kwh)
            battery_at_dusk = min(cap_kwh, current_soc_kwh + net_to_battery)

            # Overnight drain from dusk to dawn
            if dusk_dt is not None:
                drain_dusk_to_dawn = self._estimate_consumption_until(
                    dusk_dt, dawn_dt, snapshot.consumption_profile
                )
            else:
                drain_dusk_to_dawn = overnight_kwh

            battery_at_dawn = max(health_floor, battery_at_dusk - drain_dusk_to_dawn)
        else:
            # Nighttime: straightforward drain from now to dawn
            battery_at_dawn = max(health_floor, current_soc_kwh - overnight_kwh)

        # ── Tomorrow: import check ──────────────────────────────────────────
        tomorrow_solar_kwh     = snapshot.corrected_tomorrow_kwh
        available_tomorrow_kwh = battery_at_dawn + tomorrow_solar_kwh
        import_kwh             = max(0.0, tomorrow_need_kwh - available_tomorrow_kwh)
        import_kwh_grid        = import_kwh / max(0.01, snapshot.efficiency)
        import_needed          = import_kwh_grid >= MIN_IMPORT_KWH

        return SufficiencyBalance(
            battery_kwh              = round(current_soc_kwh, 2),
            remaining_solar_kwh      = round(remaining_solar_kwh, 2),
            remaining_home_to_dusk_kwh = round(remaining_home_to_dusk_kwh, 2),
            is_daytime               = is_daytime,
            dusk_dt                  = dusk_dt,
            dusk_slot                = dusk_slot,
            hours_to_dusk            = round(hours_to_dusk, 1),
            need_24h_kwh             = round(need_24h_kwh, 1),
            surplus_kwh              = round(surplus_kwh, 2),
            battery_at_dawn_kwh      = round(battery_at_dawn, 2),
            tomorrow_solar_kwh       = round(tomorrow_solar_kwh, 2),
            available_tomorrow_kwh   = round(available_tomorrow_kwh, 2),
            tomorrow_need_kwh        = round(tomorrow_need_kwh, 1),
            import_kwh               = round(import_kwh, 2),
            import_kwh_grid          = round(import_kwh_grid, 2),
            import_needed            = import_needed,
            dawn_dt                  = dawn_dt,
            hours_to_dawn            = round(hours_to_dawn, 1),
            expected_overnight_kwh   = round(overnight_kwh, 2),
        )

    # ================================================================
    # Consumption Helper
    # ================================================================

    def _estimate_consumption_until(
        self,
        now: datetime,
        target: datetime,
        profile: List[float],
    ) -> float:
        """Sum expected consumption from now until target using 48-slot profile.

        Args:
            now:     Current datetime
            target:  Target datetime (dawn / dusk)
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

    def _plan_import(
        self, snapshot: ManagerSnapshot, balance: SufficiencyBalance
    ) -> Decision:
        """Determine when and how much to import to ensure tomorrow is covered.

        Never imports for profit — only to cover a genuine tomorrow shortfall.
        Uses cheapest available time window for the active tariff.
        """
        tariff      = snapshot.tariff
        now         = snapshot.now
        battery_kwh = snapshot.current_soc_pct / 100.0 * snapshot.capacity_kwh

        # Target SOC: current SOC + the net import deficit.
        # +2% safety buffer; capped at 98% to preserve solar headroom at sunrise.
        target_kwh = battery_kwh + balance.import_kwh
        target_soc = min(98.0, target_kwh / max(1.0, snapshot.capacity_kwh) * 100.0 + 2.0)

        # Defensive guard: battery already above target → no import needed.
        # Can occur if viability and snapshot are inconsistent (e.g. forecast race).
        if snapshot.current_soc_pct >= target_soc:
            return Decision(
                action          = ACTION_SELF_CONSUMPTION,
                reason          = (
                    f"Import target {target_soc:.0f}% already met "
                    f"({snapshot.current_soc_pct:.1f}% SOC) — no import needed"
                ),
                dawn_viable     = True,
                soc_at_dawn_kwh = balance.battery_at_dawn_kwh,
            )

        # Tariff-specific import timing
        if tariff.tariff_key in (TARIFF_GO, TARIFF_FLUX, TARIFF_IGO, TARIFF_IFLUX):
            return self._plan_tou_import(snapshot, balance, target_soc)

        if tariff.tariff_key == TARIFF_TRACKER:
            return self._plan_tracker_import(snapshot, balance, target_soc)

        if tariff.tariff_key == TARIFF_AGILE:
            return self._plan_agile_import(snapshot, balance, target_soc)

        if tariff.tariff_key == TARIFF_FLEXIBLE:
            return self._plan_flexible_import(snapshot, balance, target_soc)

        # Unknown tariff — import now at half inverter power
        return Decision(
            action         = ACTION_START_IMPORT,
            reason         = (
                f"Tomorrow shortfall ({balance.available_tomorrow_kwh:.1f} kWh avail, "
                f"need {balance.tomorrow_need_kwh:.1f}). Unknown tariff — importing now."
            ),
            power_watts    = int(min(10000, snapshot.capacity_kwh * 1000 / 2)),
            target_soc_pct = target_soc,
        )

    def _plan_tou_import(
        self,
        snapshot:   ManagerSnapshot,
        balance:    SufficiencyBalance,
        target_soc: float,
    ) -> Decision:
        """Plan import for Go/Flux/iGo/iFlux — wait for cheap window if possible."""
        tariff    = snapshot.tariff
        now       = snapshot.now
        dawn_dt   = balance.dawn_dt
        cap_kwh   = snapshot.capacity_kwh
        floor_kwh = snapshot.health_cutoff_pct / 100.0 * cap_kwh

        cheap_start = tariff.cheap_start
        cheap_end   = tariff.cheap_end

        if not cheap_start or not cheap_end:
            return Decision(
                action         = ACTION_START_IMPORT,
                reason         = "Tomorrow at risk — Go/Flux cheap window unavailable, importing now",
                power_watts    = 10000,
                target_soc_pct = target_soc,
            )

        # Are we currently in the cheap window?
        now_hm = now.strftime("%H:%M")
        if self._time_in_window(now_hm, cheap_start, cheap_end):
            return Decision(
                action         = ACTION_START_IMPORT,
                reason         = (
                    f"Tomorrow at risk — in cheap window ({cheap_start}–{cheap_end}), "
                    f"importing now"
                ),
                power_watts    = 10000,
                target_soc_pct = target_soc,
            )

        # Check: can we safely wait until the cheap window starts?
        next_window_dt = self._next_window_start(now, cheap_start)
        if next_window_dt and dawn_dt:
            drain_to_window   = self._estimate_consumption_until(
                now, next_window_dt, snapshot.consumption_profile
            )
            soc_at_window_kwh = (snapshot.current_soc_pct / 100.0 * cap_kwh) - drain_to_window
            can_wait = (
                soc_at_window_kwh >= floor_kwh
                and next_window_dt < dawn_dt
            )
            if can_wait:
                return Decision(
                    action         = ACTION_SCHEDULE_IMPORT,
                    reason         = (
                        f"Tomorrow at risk — waiting for cheap window at {cheap_start}"
                    ),
                    power_watts    = 10000,
                    target_soc_pct = target_soc,
                    scheduled_time = next_window_dt,
                )

        # Cannot safely wait — import now (survival beats cheapness)
        return Decision(
            action         = ACTION_START_IMPORT,
            reason         = (
                "Tomorrow at risk — cannot wait for cheap window (battery too low), "
                "importing now"
            ),
            power_watts    = 10000,
            target_soc_pct = target_soc,
        )

    def _plan_tracker_import(
        self,
        snapshot:   ManagerSnapshot,
        balance:    SufficiencyBalance,
        target_soc: float,
    ) -> Decision:
        """Plan import on Tracker tariff (flat rate, same price all day).

        On a flat-rate tariff, pre-charging the battery wastes ~6% in AC/DC/AC
        conversion with zero rate benefit. When battery is low, the inverter's
        Self Consumption mode imports from the grid directly to the house with
        no conversion loss. Battery passthrough is more efficient in this case.

        Only exception: if tomorrow's Tracker rate is published and 10%+ cheaper,
        defer a small import to 00:05. The rate saving (~3p+/kWh typical) exceeds
        the ~1.4p round-trip efficiency loss, so it's worth pre-charging then.
        """
        tariff        = snapshot.tariff
        now           = snapshot.now
        today_rate    = tariff.today_rate_p
        tomorrow_rate = tariff.tomorrow_rate_p
        cap_kwh       = snapshot.capacity_kwh
        floor_kwh     = snapshot.health_cutoff_pct / 100.0 * cap_kwh

        # Defer to 00:05 if tomorrow's rate is meaningfully cheaper
        if (tomorrow_rate is not None
                and today_rate is not None
                and today_rate > 0
                and tomorrow_rate < today_rate * TRACKER_DEFER_THRESHOLD):

            midnight_dt = datetime.combine(
                now.date() + timedelta(days=1), datetime.min.time()
            ).replace(tzinfo=now.tzinfo)

            drain_to_midnight   = self._estimate_consumption_until(
                now, midnight_dt, snapshot.consumption_profile
            )
            soc_at_midnight_kwh = (snapshot.current_soc_pct / 100.0 * cap_kwh) - drain_to_midnight

            if soc_at_midnight_kwh >= floor_kwh:
                saving_p    = round(today_rate - tomorrow_rate, 2)
                import_time = midnight_dt + timedelta(minutes=5)
                return Decision(
                    action         = ACTION_SCHEDULE_IMPORT,
                    reason         = (
                        f"Tomorrow at risk — Tracker rate tomorrow {tomorrow_rate:.2f}p "
                        f"({saving_p:.2f}p/kWh cheaper than today {today_rate:.2f}p, "
                        f"exceeds ~1.4p/kWh efficiency loss). Deferring import to 00:05"
                    ),
                    power_watts    = 10000,
                    target_soc_pct = target_soc,
                    scheduled_time = import_time,
                )

        # Same rate all day — let inverter import direct to house as needed.
        # Pre-charging wastes ~6% (AC→DC→AC) at no rate benefit.
        rate_str = f"{today_rate:.2f}p/kWh" if today_rate else "unknown rate"
        return Decision(
            action          = ACTION_SELF_CONSUMPTION,
            reason          = (
                f"Tomorrow shortfall ({balance.available_tomorrow_kwh:.1f} kWh avail, "
                f"need {balance.tomorrow_need_kwh:.1f}) — Tracker flat rate ({rate_str}). "
                f"Grid imports direct to house; pre-charging wastes ~6% conversion loss "
                f"with no rate benefit"
            ),
            dawn_viable     = True,
            soc_at_dawn_kwh = balance.battery_at_dawn_kwh,
        )

    def _plan_agile_import(
        self,
        snapshot:   ManagerSnapshot,
        balance:    SufficiencyBalance,
        target_soc: float,
    ) -> Decision:
        """Plan import on Agile — find cheapest available slot before dawn."""
        tariff  = snapshot.tariff
        now     = snapshot.now
        dawn_dt = balance.dawn_dt

        if not tariff.agile_slots or dawn_dt is None:
            return Decision(
                action         = ACTION_START_IMPORT,
                reason         = "Tomorrow at risk — no Agile rates available, importing now",
                power_watts    = 10000,
                target_soc_pct = target_soc,
            )

        cap_kwh   = snapshot.capacity_kwh
        floor_kwh = snapshot.health_cutoff_pct / 100.0 * cap_kwh

        available_slots = [
            (dt, rate) for dt, rate in tariff.agile_slots
            if now < dt < dawn_dt
        ]

        if not available_slots:
            return Decision(
                action         = ACTION_START_IMPORT,
                reason         = "Tomorrow at risk — no future Agile slots before dawn, importing now",
                power_watts    = 10000,
                target_soc_pct = target_soc,
            )

        # Find cheapest slot the battery can safely reach
        cheapest_viable = None
        for slot_dt, rate in sorted(available_slots, key=lambda x: x[1]):
            drain = self._estimate_consumption_until(
                now, slot_dt, snapshot.consumption_profile
            )
            if (snapshot.current_soc_pct / 100.0 * cap_kwh) - drain >= floor_kwh:
                cheapest_viable = (slot_dt, rate)
                break

        if cheapest_viable:
            slot_dt, rate = cheapest_viable
            if slot_dt <= now + timedelta(minutes=5):
                return Decision(
                    action         = ACTION_START_IMPORT,
                    reason         = (
                        f"Tomorrow at risk — cheapest Agile slot now ({rate:.2f}p/kWh), "
                        f"importing"
                    ),
                    power_watts    = 10000,
                    target_soc_pct = target_soc,
                )
            return Decision(
                action         = ACTION_SCHEDULE_IMPORT,
                reason         = (
                    f"Tomorrow at risk — Agile import at "
                    f"{slot_dt.strftime('%H:%M')} ({rate:.2f}p/kWh)"
                ),
                power_watts    = 10000,
                target_soc_pct = target_soc,
                scheduled_time = slot_dt,
            )

        # Cannot safely reach any slot — import now
        return Decision(
            action         = ACTION_START_IMPORT,
            reason         = "Tomorrow at risk — no viable Agile slot available, importing now",
            power_watts    = 10000,
            target_soc_pct = target_soc,
        )

    def _plan_flexible_import(
        self,
        snapshot:   ManagerSnapshot,
        balance:    SufficiencyBalance,
        target_soc: float,
    ) -> Decision:
        """Plan import on Flexible Octopus (flat rate, no time-of-use windows).

        Same logic as Tracker: pre-charging a flat-rate battery wastes ~6% in
        conversion losses with no rate benefit. Inverter imports direct to house
        automatically when battery is low — more efficient, same price.
        """
        tariff   = snapshot.tariff
        rate_str = f"{tariff.today_rate_p:.2f}p/kWh" if tariff.today_rate_p else "flat rate"
        return Decision(
            action          = ACTION_SELF_CONSUMPTION,
            reason          = (
                f"Tomorrow shortfall ({balance.available_tomorrow_kwh:.1f} kWh avail, "
                f"need {balance.tomorrow_need_kwh:.1f}) — Flexible flat rate ({rate_str}). "
                f"Grid imports direct to house; pre-charging wastes ~6% conversion loss "
                f"with no rate benefit"
            ),
            dawn_viable     = True,
            soc_at_dawn_kwh = balance.battery_at_dawn_kwh,
        )

    # ================================================================
    # Solar Overflow (Daytime Export)
    # ================================================================

    def _check_solar_overflow(
        self, snapshot: ManagerSnapshot, balance: SufficiencyBalance
    ) -> Optional[Decision]:
        """Daytime export: cap charge rate so PV surplus flows to grid.

        Three gates before exporting:
          0. Daytime gate: balance.is_daytime must be True (dawn to dusk)
          1. 24h surplus gate: balance.surplus_kwh >= MIN_EXPORT_KWH
             If we don't have 24h surplus, keep every kWh — it's worth more
             overnight (20p+) than exported at 12p.
          2. Physics gate: remaining_solar - remaining_home > battery_headroom
             Only cap charge if solar would genuinely overflow the battery today.
             (24h surplus can be positive just because the battery is high, even
             if solar today won't fill it — no clipping risk in that case.)

        If all three pass:
          required_charge_kw = headroom / hours_to_dusk  (fills battery exactly at dusk)
          export_kw          = min(pv_surplus - required_charge, DNO cap)
          cap_w              = max(MIN, pv_surplus_w - export_w)

        Mode stays 0x02 throughout — only HOLD_ESS_MAX_CHARGE is written.
        """
        # ── 0. Daytime gate ───────────────────────────────────────────────
        if not balance.is_daytime:
            return None

        # ── 1. 24h surplus gate ───────────────────────────────────────────
        if balance.surplus_kwh < MIN_EXPORT_KWH:
            return None

        # ── 2. Physics gate: will solar actually overflow the battery? ────
        remaining_solar_kwh = balance.remaining_solar_kwh
        remaining_home_kwh  = balance.remaining_home_to_dusk_kwh
        hours_to_dusk       = balance.hours_to_dusk
        headroom_kwh        = (100.0 - snapshot.current_soc_pct) / 100.0 * snapshot.capacity_kwh
        net_to_battery      = remaining_solar_kwh - remaining_home_kwh
        solar_surplus       = net_to_battery - headroom_kwh

        if solar_surplus < 0:
            # Solar can fill battery without clipping — no export needed today
            return None

        # ── 3. Charge cap calculation ─────────────────────────────────────
        # Charge exactly fast enough to reach 100% at dusk; export everything else
        required_charge_kw = headroom_kwh / max(0.5, hours_to_dusk)
        pv_surplus_kw      = max(0.0, (snapshot.pv_watts - snapshot.house_load_watts) / 1000.0)
        export_kw          = min(
            max(0.0, pv_surplus_kw - required_charge_kw),
            snapshot.max_export_kw,
        )
        export_w = int(export_kw * 1000)
        cap_w    = max(
            SOLAR_OVERFLOW_MIN_CHARGE_W,
            snapshot.pv_watts - snapshot.house_load_watts - export_w,
        )

        return Decision(
            action          = ACTION_SOLAR_OVERFLOW,
            reason          = (
                f"Solar overflow: {balance.surplus_kwh:.1f} kWh 24h surplus | "
                f"{solar_surplus:.1f} kWh physics surplus\n"
                f"Req charge {required_charge_kw:.2f} kW  |  "
                f"PV surplus {pv_surplus_kw:.2f} kW  |  "
                f"Export {export_kw:.2f} kW  |  Cap {cap_w}W\n"
                f"Battery {balance.battery_kwh:.1f} kWh  |  "
                f"Solar remaining {remaining_solar_kwh:.1f} kWh  |  "
                f"Home to dusk {remaining_home_kwh:.1f} kWh  |  "
                f"{hours_to_dusk:.1f}h to dusk"
            ),
            power_watts     = cap_w,
            export_kw       = export_kw,
            dawn_viable     = True,
            soc_at_dawn_kwh = balance.battery_at_dawn_kwh,
        )

    # ================================================================
    # Night Export (disabled in v4.0)
    # ================================================================

    def _check_night_export(
        self, snapshot: ManagerSnapshot, viability=None
    ) -> Optional[Decision]:
        """Night export disabled in v4.0.

        The 24-hour sufficiency model uses reactive daytime export only.
        Overnight force-discharge (Discharge ESS First mode 0x06) removed:
        it suppresses PV generation and is uneconomic on a flat export tariff.

        Any active night export is stopped in evaluate() before this is reached.
        Retained as a stub for backward compatibility.
        """
        return None

    # ================================================================
    # Helpers
    # ================================================================

    @staticmethod
    def _time_in_window(time_str: str, start_str: str, end_str: str) -> bool:
        """Check if HH:MM falls within start–end window. Handles overnight windows."""
        def to_min(t):
            h, m = t.split(":")
            return int(h) * 60 + int(m)

        t     = to_min(time_str)
        start = to_min(start_str)
        end   = to_min(end_str)

        if start <= end:
            return start <= t < end
        else:
            return t >= start or t < end   # overnight window

    @staticmethod
    def _next_window_start(now: datetime, window_start_str: str) -> Optional[datetime]:
        """Return the next occurrence of HH:MM window start as a datetime."""
        try:
            h, m      = window_start_str.split(":")
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
                if now.tzinfo and dt.tzinfo is None:
                    import pytz
                    dt = pytz.timezone("Europe/London").localize(dt)
                if now <= dt < end_time:
                    total += wh
            except (ValueError, TypeError):
                continue

        return total
