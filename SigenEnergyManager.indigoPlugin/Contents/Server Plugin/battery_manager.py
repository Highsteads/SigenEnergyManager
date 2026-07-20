#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    battery_manager.py
# Description: 24-hour sufficiency model — export surplus today, import only
#              when tomorrow's battery+solar falls short of tomorrow's daily load.
#              No overnight forced discharge.
# Author:      CliveS & Claude Opus 4.8
# Date:        20-07-2026
# Version:     3.8
# 3.8 — Solar overflow charge is paced to a TARGET SOC (default 90%), not to 100%.
#       required_charge_kw is subtracted from export BEFORE the DNO cap is applied, so
#       a 100% target spends the low-surplus morning buying SOC out of exportable kWh
#       and still meets the afternoon peak with less headroom than it started with —
#       clipping anyway. Owner's actual requirement (CliveS, 20-Jul-2026): "I do not
#       need to get to 100%, it means the chance of clipping is greater. Anything above
#       90% is great, above 85% is still OK", 80%+ ample for a power cut. The target is
#       a GOAL not a ceiling — above-cap excess still charges past it, so bright days
#       finish high anyway out of surplus that would otherwise have been clipped.
#       Modelled on 20-Jul's measured curve: 100% -> 45.0 kWh export / 1.53 kWh clipped
#       / ends 91.1%; 90% -> 46.6 kWh / NOTHING clipped / ends 90.8%. New snapshot
#       fields solar_overflow_target_pct / solar_overflow_min_end_pct / storm_active.
#       A storm restores the 100% target (the one time a full battery is worth clipping
#       for) while keeping the pacing lazy, so it is never force-charged out of export
#       when the day's own solar would have got there. NO dull-day guard in the pacing:
#       the physics gate already refuses to export unless remaining solar exceeds the
#       room to 100%, and it re-evaluates every tick — a lower target keeps SOC lower,
#       so the gate bites EARLIER and the end-of-day level protects itself. The floor
#       pref is therefore just a clamp against a mis-set target. +11 contract tests,
#       including a pin that target=100 reduces to the exact pre-3.8 formula.
# 3.7 — _compute_flood_preview: the flood-export gate math extracted into one pure,
#       side-effect-free method (no daytime guard) — the single source of truth.
#       _check_flood_prevention now consumes it (control behaviour unchanged) and the
#       plugin publishes it (sigen_flood_preview.json) so the openmeteo advisory reports
#       the SAME gate instead of re-deriving it and drifting (the 23/24-Jun-2026 case).
#       compute_flood_preview() is the public entry (recomputes the pure 24h balance so
#       evaluate()'s control path is untouched). would_fire = the gate minus the daytime
#       guard, so a daytime advisory run gets a forward 'would it fire tonight' signal.
# 3.6 — VPP override now self-drives the export window (ACTION_VPP_EXPORT) instead
#       of standing down for Axle. Axle settle on the meter reading so exporting it
#       ourselves counts identically, and their cloud dispatch is unreliable (no-show
#       10-Jun-2026, Axle acknowledged a SigEnergy-API fault). Plugin.py drives
#       night_export for the window and ignores Axle's start/stop.
# 3.5 — Decision dataclass gains an `audit_trail: List[Tuple[str, str]]` field;
#       evaluate() populates it at every branch (CONTEXT, BALANCE, OVERRIDE,
#       RESILIENCE, FLOOD-PREP, IMPORT, OVERFLOW, RELEASE-OVERFLOW, DEFAULT)
#       so the whole decision tree — both matched and considered-but-skipped
#       branches — is visible in a single audit block.  Plan-object pattern
#       lifted from mlamoure/indigo-auto-lights; same shape already applied to
#       openmeteo_battery_optimiser v3.6 and octopus_tracker_rate v1.2 (the
#       three together = "every Sigenergy-touching decision script audits its
#       reasoning the same way").  Logged by plugin.py _log_manager_decision on
#       action change (no per-poll spam).  Pure additive — audit_trail defaults
#       to [] so existing tests in test_battery_manager.py are unaffected.
# 3.4 — flood prevention also subtracts Axle VPP export scheduled on the refill
#       day (treated as extra demand). Pro-rated for events that span midnight.
# 3.3 — flood prevention now gates on the *refill-day* solar forecast
#       (today's when dawn is later today; tomorrow's otherwise). Fixes the
#       21-May-2026 incident where a post-midnight check used the day-after's
#       forecast and dumped the battery into a poor-today/sunny-day-after pair.

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
        "go":    {"cheap_start": "23:30", "cheap_end": "04:30"},   # 23:30-04:30 (5h) — live GO-FIX product (region F, verified 05-Jul-2026)
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
ACTION_START_EXPORT     = "start_export"        # used by flood prevention pre-drain (v4.4+)
ACTION_STOP_EXPORT      = "stop_export"         # stop active legacy export (v3.x migration)
ACTION_SOLAR_OVERFLOW   = "solar_overflow"      # daytime: cap charge so PV surplus exports
ACTION_VPP_EXPORT       = "vpp_export"          # VPP event window: self-drive export (ignore Axle dispatch)

# Minimum percentage cheaper to justify waiting for tomorrow's Tracker rate
TRACKER_DEFER_THRESHOLD = 0.90   # tomorrow must be < 90% of today (10%+ cheaper)

# Minimum import quantity — below this don't bother charging
MIN_IMPORT_KWH = 0.5

# Minimum 24h surplus before daytime export is allowed.
# Below this the battery has barely enough for 24h — every kWh is worth more
# overnight (20p+) than as daytime export (12p flat).
MIN_EXPORT_KWH = 0.3

# Flood prevention — overnight pre-drain to create headroom for peak solar absorption.
# On high-SOC nights before very sunny days, a full battery at ~09:00 chokes off
# daytime export when the 4 kW DNO cap cannot route peak PV (7+ kW) fast enough.
# Pre-draining to TARGET% earns export revenue AND enables uninterrupted 4 kW export
# through peak hours because the battery has room to absorb the temporal surplus.
# Only fires when tomorrow solar >= MULT × tomorrow need (safe to refill without reimport).
FLOOD_PREV_SOC_THRESHOLD_PCT = 55.0   # min SOC % to trigger overnight pre-drain
FLOOD_PREV_TARGET_PCT        = 40.0   # drain to this SOC % before sunrise
FLOOD_PREV_FORECAST_MULT     = 3.0    # tomorrow solar must be >= this × tomorrow need

# Solar overflow constants (daytime forecast-based export)
# Mode stays 0x02 (Max Self Consumption) throughout.
# Only HOLD_ESS_MAX_CHARGE register is reduced — PV is never suppressed.
SOLAR_OVERFLOW_MIN_CHARGE_W   = 200   # minimum charge cap floor (avoid writing 0W to register)
SOLAR_OVERFLOW_CAP_DEADBAND_W = 500   # only rewrite limit if cap changes by > this
SOLAR_DUSK_THRESHOLD_WH       = 500   # Wh/hr below which a slot is considered post-dusk

# v3.8 — the charge PACING target. Until now the overflow charge was paced to reach
# 100% exactly at dusk, and because that pacing is subtracted from export BEFORE the
# DNO cap is applied, a high target spends the low-surplus morning buying SOC out of
# exportable kWh — then still meets the afternoon peak with less headroom than it
# started with, and clips anyway. Owner's actual requirement (CliveS, 20-Jul-2026):
# "I do not need to get to 100%, it means the chance of clipping is greater. Anything
# above 90% is great, above 85% is still OK", with 80%+ ample for power-cut cover.
#
# CRITICAL: this is a charging GOAL, not a ceiling. Once export is at the DNO cap the
# above-cap excess still has nowhere to go but the battery, so it charges straight past
# the target — you get the high finish for free, out of surplus that would otherwise
# have been clipped. Modelled on 20-Jul's measured curve: 100% target -> 45.0 kWh
# exported, 1.53 kWh clipped, ends 91.1%; 90% target -> 46.6 kWh exported, NOTHING
# clipped, ends 90.8%. Three-tenths of a point of finish for 1.6 kWh of export.
SOLAR_OVERFLOW_TARGET_SOC_PCT = 90.0
# Dull-day guard. If pacing to the target would leave the battery below this at dusk,
# the day is too weak to give kWh away — revert to the old 100% pacing and keep them.
SOLAR_OVERFLOW_MIN_END_SOC_PCT = 80.0


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
    export_rate_p:      float = 12.0     # live export unit rate (p/kWh), for advisory revenue

    # Daily consumption estimates (24h sufficiency model)
    weekday_kwh:        float = 22.0     # Mon-Fri daily load
    weekend_kwh:        float = 30.0     # Sat-Sun daily load (washing, cooking, oven)

    # Live inverter readings
    pv_watts:               int   = 0
    house_load_watts:       int   = 0
    export_active:          bool  = False   # export active (flood prevention v4.4, or legacy v3.x)
    corrected_today_kwh:    float = 0.0     # bias-corrected forecast for today (kWh)
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

    # VPP export quantity expected on each local date (kWh). Pre-computed by
    # plugin.py and pro-rated for events that span midnight. "Future only" —
    # the portion of an ACTIVE event that has already happened is not counted.
    # Used by flood prevention to inflate refill-day demand.
    vpp_today_kwh:    float = 0.0
    vpp_tomorrow_kwh: float = 0.0

    # Solar overflow state (from plugin.py store — passed in so manager is stateless)
    solar_overflow_active:     bool = False   # charge cap currently applied
    solar_overflow_charge_cap: int  = 0       # current cap in watts

    # Solar overflow charge PACING target (v3.8). Charge is paced to reach this SOC at
    # dusk rather than 100% — see SOLAR_OVERFLOW_TARGET_SOC_PCT. A goal, not a ceiling:
    # above-cap excess still charges past it. Defaults keep pre-v3.8 behaviour reachable
    # by setting the target to 100.
    solar_overflow_target_pct:  float = SOLAR_OVERFLOW_TARGET_SOC_PCT
    solar_overflow_min_end_pct: float = SOLAR_OVERFLOW_MIN_END_SOC_PCT

    # Storm warning active — raises the overflow target back to 100% (a storm is the one
    # time a genuinely full battery is worth clipping for). Set by plugin's
    # _apply_storm_override. Note the pacing stays LAZY even at 100%: if the day's own
    # solar will fill the battery anyway, nothing is force-charged out of export.
    storm_active: bool = False

    # Flood prevention state (from plugin.py store — so evaluate() can continue a running pre-drain)
    # 0.0 when inactive; set to the target SOC % when a flood-prevention export is running
    flood_prev_target_soc: float = 0.0


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
    # v3.5 — Plan-object audit trail: (tag, message) tuples appended at every
    # branch evaluate() considers (matched OR skipped).  Plugin logs this on
    # action change to make the WHY visible without re-running with debug on.
    audit_trail:     List[Tuple[str, str]] = field(default_factory=list)


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
    4. VPP events override all decisions — self-drive export for the window
       (meter-settled, so Axle's own dispatch is not relied upon).

    This class is stateless: it takes a ManagerSnapshot and returns a Decision.
    All state is managed by plugin.py.
    """

    def evaluate(self, snapshot: ManagerSnapshot) -> Decision:
        """Main entry point — evaluate system state and return a decision.

        Decision flow (first match wins):
          1. Overrides     — VPP / active flood-prevention export
          2. Resilience    — flat-rate tariff overnight power-cut floor
          3. Flood prep    — overnight pre-drain before very sunny day
          4. Import        — tomorrow won't reach sufficiency without grid
          5. Overflow      — daytime export when surplus exceeds DNO cap
          6. Self-consume  — nothing else applies

        v3.5: every branch — matched OR considered-but-skipped — appends an
        entry to a local audit list which is attached to the returned Decision.
        Plugin.py logs the audit on action change.  Skip-reason text is short
        because each branch's full reasoning lives in the matched-case reason
        string; the audit captures the path through the tree, not the algebra.
        """
        audit: List[Tuple[str, str]] = []
        audit.append((
            "CONTEXT",
            f"SOC {snapshot.current_soc_pct:.1f}%, tariff={snapshot.tariff.tariff_key}, "
            f"export_enabled={snapshot.export_enabled}, vpp_active={snapshot.vpp_active}, "
            f"export_active={snapshot.export_active}"
        ))

        # 1. Overrides — VPP suspension or already-running flood prevention
        override = self._check_overrides(snapshot)
        if override is not None:
            audit.append(("OVERRIDE", f"matched -> {override.reason}"))
            override.audit_trail = audit
            return override
        audit.append(("OVERRIDE", "skipped — no VPP active, no running flood export"))

        # 24h sufficiency calc used by every later branch
        balance = self._calculate_24h_balance(snapshot)
        audit.append((
            "BALANCE",
            f"surplus {balance.surplus_kwh:.1f} kWh, import_needed={balance.import_needed}, "
            f"daytime={balance.is_daytime}, dawn SOC {balance.battery_at_dawn_kwh:.1f} kWh, "
            f"tomorrow need {balance.tomorrow_need_kwh:.1f} kWh"
        ))

        # 2. Resilience buffer (flat-rate any-time; TOU only in the cheap window
        #    when tomorrow is already covered — see _check_resilience_buffer)
        resilience = self._check_resilience_buffer(snapshot, balance)
        if resilience is not None:
            audit.append(("RESILIENCE", f"matched -> {resilience.reason}"))
            resilience.audit_trail = audit
            return resilience
        audit.append((
            "RESILIENCE",
            f"skipped — tariff={snapshot.tariff.tariff_key}, daytime={balance.is_daytime}, "
            f"SOC {snapshot.current_soc_pct:.1f}% vs dawn_target {snapshot.dawn_target_pct:.0f}%"
        ))

        # 3. Overnight flood prevention pre-drain (only if export enabled)
        if snapshot.export_enabled and not balance.is_daytime:
            flood = self._check_flood_prevention(snapshot, balance)
            if flood is not None:
                audit.append(("FLOOD-PREP", f"matched -> {flood.reason}"))
                flood.audit_trail = audit
                return flood
            audit.append(("FLOOD-PREP", "skipped — gate conditions not met (SOC or forecast)"))
        else:
            audit.append((
                "FLOOD-PREP",
                f"skipped — export_enabled={snapshot.export_enabled}, "
                f"daytime={balance.is_daytime}"
            ))

        # 4. Import takes priority: ensure tomorrow is covered before exporting today
        if balance.import_needed:
            decision = self._plan_import(snapshot, balance)
            # Only flag dawn as not viable when we're actually importing.
            # Flat-rate planners (Tracker/Flexible) may return self_consumption
            # intentionally — grid passthrough is more efficient than pre-charging.
            if decision.action in (ACTION_START_IMPORT, ACTION_SCHEDULE_IMPORT):
                decision.dawn_viable = False
            decision.soc_at_dawn_kwh = balance.battery_at_dawn_kwh
            decision.import_kwh      = balance.import_kwh_grid
            audit.append(("IMPORT", f"matched -> {decision.action}: {decision.reason}"))
            decision.audit_trail = audit
            return decision
        audit.append(("IMPORT", "skipped — import_needed=False (battery+solar covers tomorrow)"))

        # 5. Daytime solar overflow: export surplus PV that would otherwise be clipped
        if snapshot.export_enabled:
            overflow = self._check_solar_overflow(snapshot, balance)
            if overflow is not None:
                audit.append(("OVERFLOW", f"matched -> {overflow.reason}"))
                overflow.audit_trail = audit
                return overflow
            audit.append(("OVERFLOW", "skipped — no surplus or conditions not met"))
        else:
            audit.append(("OVERFLOW", "skipped — export not enabled"))

        # 5b. Release a previously-applied overflow cap if conditions no longer hold
        if snapshot.solar_overflow_active:
            release = Decision(
                action          = ACTION_SELF_CONSUMPTION,
                reason          = (
                    f"Solar overflow: conditions no longer met — releasing charge cap "
                    f"(24h surplus {balance.surplus_kwh:.1f} kWh, "
                    f"daytime={balance.is_daytime})"
                ),
                dawn_viable     = True,
                soc_at_dawn_kwh = balance.battery_at_dawn_kwh,
            )
            audit.append((
                "RELEASE-OVERFLOW",
                "matched -> previously-applied overflow cap no longer applicable"
            ))
            release.audit_trail = audit
            return release

        # 6. Default: nothing to do, sit on self-consumption
        default_decision = Decision(
            action          = ACTION_SELF_CONSUMPTION,
            reason          = (
                f"24h sufficient — surplus {balance.surplus_kwh:.1f} kWh | "
                f"tomorrow: {balance.available_tomorrow_kwh:.1f} kWh avail, "
                f"need {balance.tomorrow_need_kwh:.1f} kWh"
            ),
            dawn_viable     = True,
            soc_at_dawn_kwh = balance.battery_at_dawn_kwh,
        )
        audit.append(("DEFAULT", "matched -> self-consumption (no other branch applied)"))
        default_decision.audit_trail = audit
        return default_decision

    # ------------------------------------------------------------------
    # evaluate() decision branches — extracted for readability
    # ------------------------------------------------------------------

    def _check_overrides(self, snapshot: ManagerSnapshot):
        """Return a Decision if a higher-priority override applies, else None.

        Two overrides:
          - VPP active: self-drive the export window (meter-settled; Axle's own
            cloud dispatch is ignored). Returns ACTION_VPP_EXPORT.
          - Flood-prevention export already running: continue or stop based on
            the live 24h balance (managed by _continue_flood_prevention).

        The v3.x legacy "night export" branch was retired in v4.0 — an
        export_active state WITHOUT a flood_prev_target_soc falls through to
        normal evaluation (both conditions below are required); plugin.py's
        prev_export transition handler stands the stray export down on the
        resulting self-consumption decision.
        """
        if snapshot.vpp_active:
            # Don't self-drive a VPP export while export is currently disabled — a
            # post-power-cut lockout (or a storm override) forces export_enabled
            # False as a precaution, and that safety gate must win over the ~£1/kWh
            # VPP payment. Stand down to self-consumption; the window resumes
            # automatically once export is re-enabled (e.g. SOC clears the lockout
            # floor). Holding the battery here also maximises post-cut resilience.
            if not snapshot.export_enabled:
                return Decision(
                    action = ACTION_SELF_CONSUMPTION,
                    reason = "VPP event active but export currently disabled "
                             "(power-cut lockout / storm) — standing down",
                )
            # Self-drive the export window instead of standing down. Axle settle on
            # the meter reading, so exporting it ourselves counts identically — and
            # their cloud dispatch proved unreliable (no-show 10-Jun-2026, Axle
            # acknowledged a SigEnergy-API fault that "may not be resolved before the
            # next event"). We no longer wait for or hand control to Axle.
            return Decision(
                action = ACTION_VPP_EXPORT,
                reason = "VPP event active — self-driving export (Axle dispatch ignored)",
            )
        if snapshot.export_active and snapshot.flood_prev_target_soc > 0:
            balance = self._calculate_24h_balance(snapshot)
            return self._continue_flood_prevention(snapshot, balance)
        return None

    def _check_resilience_buffer(self, snapshot: ManagerSnapshot,
                                 balance: SufficiencyBalance):
        """Overnight power-cut floor — return Decision or None.

        Keeps at least dawn_target_pct in the battery so the house can ride out
        an outage for several hours. Import stops at dawn_target_pct + 2%
        (overshoot prevents cycling).

        Flat-rate tariffs (Tracker/Flexible): there is no rate benefit to
        pre-charging for tomorrow (grid passthrough is just as cheap and avoids
        the ~6% round-trip loss), so this power-cut floor is the ONE reason to
        import overnight — fire any time the battery is below it.

        TOU tariffs (Go/iGo/Flux/iFlux): the import branch (priority 4) already
        lifts the battery above this floor whenever tomorrow needs covering, so
        this branch only fills the gap it leaves — a night before a well-covered
        (e.g. sunny) day, where import_needed is False yet SOC has drifted below
        the floor. Restore it ONLY inside the cheap window so the top-up is
        bought at the night rate, never the peak/day rate. Without this, on
        Go/iGo the reserve is never guaranteed (added 05-Jul-2026).

        Returns None when not applicable (unknown tariff, daytime, battery above
        the floor, or — on TOU — tomorrow already being covered or outside the
        cheap window).
        """
        tariff_key = snapshot.tariff.tariff_key
        is_flat = tariff_key in (TARIFF_TRACKER, TARIFF_FLEXIBLE)
        is_tou  = tariff_key in (TARIFF_GO, TARIFF_IGO, TARIFF_FLUX, TARIFF_IFLUX)
        if not (is_flat or is_tou):
            return None
        if balance.is_daytime:
            return None
        if snapshot.current_soc_pct >= snapshot.dawn_target_pct:
            return None

        if is_tou:
            # The import planner already lifts SOC above the floor whenever
            # tomorrow needs covering — only step in for the gap it misses.
            if balance.import_needed:
                return None
            # Buy the reserve top-up at the night rate: only inside the cheap
            # window, never the peak/day rate.
            if not snapshot.tariff.cheap_start or not snapshot.tariff.cheap_end:
                return None
            now_hm = self._to_local(snapshot.now).strftime("%H:%M")
            if not self._time_in_window(now_hm, snapshot.tariff.cheap_start,
                                        snapshot.tariff.cheap_end):
                return None

        buffer_pct    = snapshot.dawn_target_pct          # default 10%
        buffer_target = min(buffer_pct + 2.0, 98.0)       # +2% prevents cycling
        cap_kwh       = snapshot.capacity_kwh
        deficit_kwh   = (buffer_pct - snapshot.current_soc_pct) / 100.0 * cap_kwh
        rate_str      = (f"{snapshot.tariff.today_rate_p:.2f}p/kWh"
                         if snapshot.tariff.today_rate_p else "")
        context       = (f"flat rate {rate_str}" if is_flat
                         else f"cheap window {snapshot.tariff.cheap_start}–"
                              f"{snapshot.tariff.cheap_end}")
        return Decision(
            action          = ACTION_START_IMPORT,
            reason          = (
                f"Resilience buffer: {snapshot.current_soc_pct:.1f}% below "
                f"{buffer_pct:.0f}% minimum — importing {deficit_kwh:.1f} kWh "
                f"to {buffer_target:.0f}% for power-cut protection "
                f"({context}, solar recharges from dawn)"
            ),
            power_watts     = 10000,
            target_soc_pct  = buffer_target,
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
        # (ImportError, Exception) was redundant — Exception already covers it and
        # masked real tz errors. Try pytz, then stdlib zoneinfo (correct in GMT too).
        try:
            import pytz
            local_now = now.astimezone(pytz.timezone("Europe/London"))
        except Exception:
            try:
                from zoneinfo import ZoneInfo
                local_now = now.astimezone(ZoneInfo("Europe/London"))
            except Exception:
                local_now = now

        today_str = local_now.date().strftime("%Y-%m-%d")

        # ── Daily consumption estimate (today and tomorrow) ─────────────────
        day_of_week       = local_now.weekday()       # 0=Mon … 5=Sat, 6=Sun
        need_24h_kwh      = snapshot.weekend_kwh if day_of_week >= 5 else snapshot.weekday_kwh

        tomorrow_date     = local_now.date() + timedelta(days=1)
        tomorrow_weekday  = tomorrow_date.weekday()
        tomorrow_need_kwh = snapshot.weekend_kwh if tomorrow_weekday >= 5 else snapshot.weekday_kwh

        # ── Find next dawn (forward-scan prevents BST/UTC date mismatch) ────
        # dawn_times is keyed by LOCAL date, so scan from local_now.date(), not the
        # UTC now.date() (which is a day behind in the 00:00-01:00 BST window).
        dawn_dt = None
        for _days in range(3):
            _candidate = snapshot.dawn_times.get(
                (local_now.date() + timedelta(days=_days)).strftime("%Y-%m-%d")
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
            # Mirror the local_now pattern above: pytz, then stdlib zoneinfo,
            # and only then the UTC stamp — the naive dusk time is Europe/London,
            # so stamping it UTC directly runs an hour late in BST (extending
            # is_daytime and hours_to_dusk by an hour).
            try:
                import pytz
                _tz_l   = pytz.timezone("Europe/London")
                dusk_dt = _tz_l.localize(dusk_hour_naive + timedelta(hours=1))
            except Exception:
                try:
                    from zoneinfo import ZoneInfo
                    dusk_dt = (dusk_hour_naive + timedelta(hours=1)).replace(
                        tzinfo=ZoneInfo("Europe/London"))
                except Exception:
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
                    # Pro-rate the current hour by minutes remaining — late in
                    # the hour most of its energy has already been generated
                    # and is either consumed or banked in current_soc_pct, so
                    # counting the full slot double-counts it in surplus_kwh
                    # and battery_at_dawn.
                    if key_dt == now_hour:
                        wh *= max(0.0, (60 - now_naive.minute) / 60.0)
                    remaining_solar_kwh += wh / 1000.0
            remaining_solar_kwh *= snapshot.bias_factor

        # ── 24h surplus (export eligibility) ───────────────────────────────
        # DELIBERATELY CONSERVATIVE (owner decision, 02-07-2026, closing the
        # 26-Jun deferred item): need_24h is the full-calendar-day figure while
        # the solar term spans only now->dusk — tomorrow-morning solar inside
        # the true next-24h window is NOT counted. Evening evaluations
        # therefore understate surplus and suppress marginal export
        # eligibility, which errs in the KPI-safe direction (keep kWh in the
        # battery over 12p export). The precise alternative (rolling-window
        # need via _estimate_consumption_until + tomorrow's pre-noon solar)
        # was considered and declined: it leans on the overnight forecast for
        # tonight's export decision. Pinned by
        # test_surplus_is_conservative_no_tomorrow_solar.
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
                # The 48-slot profile is indexed by LOCAL (Europe/London) half-hour,
                # but `cursor` is UTC — index by local time or we read the wrong slot
                # (two slots off in BST). London is always a whole-hour offset from
                # UTC, so the slot boundaries still align; only the index must shift.
                local_cursor = self._to_local(cursor)
                slot_idx   = local_cursor.hour * 2 + (1 if local_cursor.minute >= 30 else 0)
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

        # Are we currently in the cheap window? cheap_start/cheap_end are LOCAL
        # (Europe/London) HH:MM but snapshot.now is UTC — convert before comparing
        # or the test is an hour off in BST (mirrors the _plan_tracker_import path).
        now_hm = self._to_local(now).strftime("%H:%M")
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

            # Build midnight at Europe/London (00:00 local), not at now.tzinfo.
            # `now` is UTC, so a naive .replace(tzinfo=now.tzinfo) places the
            # boundary at UTC midnight — which is 01:00 BST in summer. The
            # cheap-rate Tracker boundary is local-time midnight.
            try:
                import pytz
                _tz_l        = pytz.timezone("Europe/London")
                local_now    = now.astimezone(_tz_l)
                midnight_naive = datetime.combine(
                    local_now.date() + timedelta(days=1), datetime.min.time()
                )
                midnight_dt  = _tz_l.localize(midnight_naive).astimezone(now.tzinfo or timezone.utc)
            except ImportError:
                # Fallback: UTC midnight if pytz unavailable
                midnight_dt = datetime.combine(
                    now.date() + timedelta(days=1), datetime.min.time()
                ).replace(tzinfo=now.tzinfo or timezone.utc)

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
            # Round-trip break-even (v5.44.0): pre-charging loses ~6% in
            # AC->DC->AC conversion, so the cheapest slot only beats letting
            # the inverter pass grid straight to the house tomorrow when
            # rate / efficiency undercuts tomorrow's daytime rates. Tracker
            # and Flexible already gate on exactly this economics — Agile
            # was the one path that imported unconditionally, which loses
            # money on flat-ish Agile days (overnight 22p vs daytime 23p:
            # 22 / 0.94 = 23.4p effective).
            reference = self._agile_daytime_reference_rate(tariff, dawn_dt)
            effective = rate / max(0.01, snapshot.efficiency)
            if reference is not None and effective >= reference:
                return Decision(
                    action          = ACTION_SELF_CONSUMPTION,
                    reason          = (
                        f"Tomorrow shortfall — cheapest Agile slot {rate:.2f}p is "
                        f"{effective:.2f}p after ~{(1 - snapshot.efficiency) * 100:.0f}% "
                        f"conversion loss, vs {reference:.2f}p daytime average. "
                        f"Grid imports direct to house instead; pre-charging would "
                        f"cost more than it saves"
                    ),
                    dawn_viable     = True,
                    soc_at_dawn_kwh = balance.battery_at_dawn_kwh,
                )
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

    @staticmethod
    def _agile_daytime_reference_rate(tariff: TariffData, dawn_dt):
        """Mean of tomorrow's daytime Agile slots (dawn -> dawn+12h), the
        passthrough price an overnight pre-charge competes against. Falls back
        to today's rate when tomorrow's slots aren't published yet; None when
        no reference exists (caller then imports ungated, as before v5.44.0).
        """
        if tariff.agile_slots and dawn_dt is not None:
            day_rates = [r for dt, r in tariff.agile_slots
                         if dawn_dt <= dt < dawn_dt + timedelta(hours=12)]
            if day_rates:
                return sum(day_rates) / len(day_rates)
        return tariff.today_rate_p or None

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
        # Charge exactly fast enough to reach the TARGET at dusk; export everything
        # else. v3.8: the target is 90% by default, not 100% — see
        # SOLAR_OVERFLOW_TARGET_SOC_PCT for why aiming at 100% actively causes
        # clipping. Above-cap excess still charges past the target (it has nowhere
        # else to go once export is capped), so the battery routinely finishes above
        # it anyway, for free.
        target_pct  = float(snapshot.solar_overflow_target_pct)
        min_end_pct = float(snapshot.solar_overflow_min_end_pct)

        # A storm is the one time a genuinely full battery is worth clipping for.
        # The pacing stays lazy even so: if the day's solar will reach 100% unaided,
        # required_charge stays small and nothing is force-charged out of export.
        if snapshot.storm_active:
            target_pct = 100.0

        # Floor. NOTE the dull-day case needs no guard here, and a "will the solar
        # actually reach the target?" test would be dead code: the physics gate above
        # only lets us export when net_to_battery EXCEEDS the room to 100%, so whenever
        # this method runs the day can demonstrably reach 100% — never mind the target.
        # Better still, that gate is re-evaluated every tick against remaining solar, so
        # the moment the rest of the day can no longer fill to 100% it returns None,
        # export stops and everything charges. A lower target keeps the battery lower,
        # which makes the gate bite EARLIER — the pacing change is self-limiting and the
        # end-of-day level is protected by machinery that already exists.
        #
        # So the floor's only remaining job is to stop a mis-set pref pacing to a level
        # below what the owner wants available for a power cut. A clamp does that
        # exactly, with no unreachable branches.
        target_pct = max(target_pct, min_end_pct)

        # Room up to the TARGET (not to 100%) is what sets the pacing rate. Clamped at
        # zero: at or above the target there is nothing left to pace towards, so export
        # runs at the full DNO cap and the battery simply takes the overspill.
        # Deliberately mirrors the headroom_kwh expression above term-for-term, so a
        # target of 100 reduces to exactly the pre-v3.8 formula — the change is provably
        # a no-op when the target is left at 100 (pinned by a contract test).
        headroom_to_target = max(
            0.0,
            (target_pct - snapshot.current_soc_pct) / 100.0 * snapshot.capacity_kwh,
        )
        required_charge_kw = headroom_to_target / max(0.5, hours_to_dusk)
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
                f"Req charge {required_charge_kw:.2f} kW to {target_pct:.0f}% target"
                f"{' (storm)' if snapshot.storm_active else ''}  |  "
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
    # Flood Prevention (Night Pre-Drain)
    # ================================================================

    def _refill_day_view(
        self, snapshot: ManagerSnapshot, balance: SufficiencyBalance
    ) -> Tuple[float, float, float]:
        """Return (solar_kwh, need_kwh, vpp_kwh) for the day that will refill the battery.

        Pre-drain happens overnight; the battery refills on the day the *next dawn*
        falls on (UK local). For a 22:00 pre-drain that's tomorrow; for a 00:25
        pre-drain it's today. Picking the wrong day was the 21-May-2026 bug.

        vpp_kwh is the Axle VPP export expected on the refill day — pro-rated
        for events that span midnight. Treated as additional demand by the
        flood-prevention gate so refill capacity isn't double-counted.

        Falls back to tomorrow values if dawn_dt is missing.
        """
        try:
            now_local = self._to_local(snapshot.now)
            if balance.dawn_dt is not None:
                dawn_local = self._to_local(balance.dawn_dt)
                if dawn_local.date() == now_local.date():
                    return (
                        snapshot.corrected_today_kwh,
                        balance.need_24h_kwh,
                        snapshot.vpp_today_kwh,
                    )
        except Exception:
            pass
        return (
            balance.tomorrow_solar_kwh,
            balance.tomorrow_need_kwh,
            snapshot.vpp_tomorrow_kwh,
        )

    def _refill_day_label(
        self, snapshot: ManagerSnapshot, balance: SufficiencyBalance
    ) -> str:
        """Human-readable label ("Today" or "Tomorrow") for the refill day."""
        try:
            now_local = self._to_local(snapshot.now)
            if balance.dawn_dt is not None:
                dawn_local = self._to_local(balance.dawn_dt)
                if dawn_local.date() == now_local.date():
                    return "Today"
        except Exception:
            pass
        return "Tomorrow"

    @staticmethod
    def _to_local(dt: datetime) -> datetime:
        """Convert dt to UK local time; handle naive datetimes as already local."""
        try:
            import pytz
            london = pytz.timezone("Europe/London")
            if dt.tzinfo is None:
                return dt
            return dt.astimezone(london)
        except Exception:
            return dt

    def _check_flood_prevention(
        self, snapshot: ManagerSnapshot, balance: SufficiencyBalance
    ) -> Optional[Decision]:
        """Overnight pre-drain to create headroom for peak solar absorption.

        On high-SOC nights before very sunny days, the battery fills early in the
        morning and cuts off daytime export at the DNO limit (4 kW). Pre-draining
        to FLOOD_PREV_TARGET_PCT creates enough capacity to absorb the full peak
        surplus, so export runs at the DNO cap continuously rather than being choked
        off when the battery tops out.

        Example (25-Apr): SOC 70%, tomorrow forecast 68 kWh vs 22 kWh need.
          Without pre-drain: battery fills ~13:00 → export stops → ~5 kWh clipped.
          With pre-drain: 70% → 40% overnight = 10.5 kWh exported (~£1.26 @ 12p),
          then 4 kW export runs uninterrupted 10:00–18:00 through peak hours.

        Conditions:
          - Nighttime only (daytime handled by solar overflow)
          - Export MPAN active
          - tomorrow_solar >= FLOOD_PREV_FORECAST_MULT × tomorrow_need (safe to refill, no reimport risk)
          - Current SOC >= FLOOD_PREV_SOC_THRESHOLD_PCT (worth draining)
          - Effective target < threshold (storm resilience floor not too high)

        Storm/seasonal floor is respected: if dawn_target_pct (raised by storm watch
        or seasonal buffer) is above FLOOD_PREV_TARGET_PCT, the higher floor is used.
        If the floor reaches the trigger threshold the method returns None — no point
        draining a few percent when storm resilience dominates.

        plugin.py ACTION_START_EXPORT handler sets the hardware discharge cutoff
        register (HOLD_ESS_DISCHARGE_CUTOFF) to target_soc_pct so the battery stops
        automatically. The cutoff is reset to health_floor on return to self-consumption.
        """
        # Nighttime only — daytime export handled by solar overflow. The gate MATH
        # lives in _compute_flood_preview (the single source of truth plugin.py also
        # publishes for the openmeteo advisory); here we apply the nighttime guard and
        # turn a would_fire=True preview into an actionable START_EXPORT decision.
        if balance.is_daytime:
            return None

        preview = self._compute_flood_preview(snapshot, balance)
        if not preview["would_fire"]:
            return None

        effective_target  = preview["effective_target_pct"]
        refill_solar_kwh  = preview["refill_solar_kwh"]
        refill_need_kwh   = preview["refill_need_kwh"]
        refill_vpp_kwh    = preview["refill_vpp_kwh"]
        refill_demand_kwh = preview["refill_demand_kwh"]
        export_kwh        = preview["expected_export_kwh"]
        revenue_gbp       = preview["expected_revenue_gbp"]
        refill_label      = preview["refill_label"]

        if refill_vpp_kwh > 0.01:
            demand_str = (
                f"{refill_demand_kwh:.1f} kWh = house {refill_need_kwh:.1f} "
                f"+ Axle {refill_vpp_kwh:.1f}"
            )
        else:
            demand_str = f"{refill_demand_kwh:.1f} kWh"
        return Decision(
            action          = ACTION_START_EXPORT,
            reason          = (
                f"Flood prevention: SOC {snapshot.current_soc_pct:.1f}% → "
                f"{effective_target:.0f}% ({export_kwh:.1f} kWh @ 12p = ~£{revenue_gbp:.2f}). "
                f"{refill_label} {refill_solar_kwh:.1f} kWh forecast "
                f">= {FLOOD_PREV_FORECAST_MULT:.0f}x need ({demand_str}) "
                f"— solar refills without reimport. "
                f"Lower dawn SOC sustains DNO-capped export through peak hours"
            ),
            power_watts     = int(snapshot.max_export_kw * 1000),
            target_soc_pct  = effective_target,
            dawn_viable     = True,
            soc_at_dawn_kwh = balance.battery_at_dawn_kwh,
        )

    def _compute_flood_preview(
        self, snapshot: ManagerSnapshot, balance: SufficiencyBalance
    ) -> dict:
        """Pure flood-export gate evaluation — NO nighttime guard, NO side effects.

        Single source of truth for the flood-prevention gate. _check_flood_prevention
        applies the nighttime guard and builds the START_EXPORT Decision from this;
        plugin.py publishes the returned dict (sigen_flood_preview.json) so the
        openmeteo advisory reports the SAME gate the plugin acts on rather than
        re-deriving it and drifting — the 23/24-Jun-2026 "promised export that never
        ran" case, where the advisory used the plugin's day+2 'tomorrow' states at
        01:45 instead of the refill day (today).

        Conditions (same algebra _check_flood_prevention used before, minus daytime):
          - Export MPAN active
          - refill_solar >= FLOOD_PREV_FORECAST_MULT × (refill_need + refill_vpp)
          - effective_target < FLOOD_PREV_SOC_THRESHOLD_PCT (storm floor leaves headroom)
          - current SOC > effective_target (something to drain)
          - current SOC >= FLOOD_PREV_SOC_THRESHOLD_PCT (worth draining)

        would_fire answers "would the gate drain the battery if it were night now,
        with the current forecast and SOC". The daytime guard is deliberately omitted
        so a 20:00 (still-daylight) advisory run gets a forward signal for tonight.
        """
        refill_solar_kwh, refill_need_kwh, refill_vpp_kwh = self._refill_day_view(
            snapshot, balance
        )
        refill_demand_kwh  = refill_need_kwh + refill_vpp_kwh
        gate_threshold_kwh = FLOOD_PREV_FORECAST_MULT * refill_demand_kwh
        ratio              = (refill_solar_kwh / refill_demand_kwh) if refill_demand_kwh > 0 else 0.0
        effective_target   = max(FLOOD_PREV_TARGET_PCT, snapshot.dawn_target_pct)
        soc                = snapshot.current_soc_pct

        # refill_demand_kwh > 0 required: with demand 0 (user enters 0 in both
        # weekday/weekend kWh fields) the threshold is 0.0 and ANY forecast —
        # including zero sun — would authorise a pre-drain. No demand estimate
        # means no evidence the battery can refill, so the gate fails safe.
        forecast_gate_pass = (refill_demand_kwh > 0
                              and refill_solar_kwh >= gate_threshold_kwh)
        target_headroom_ok = effective_target < FLOOD_PREV_SOC_THRESHOLD_PCT
        soc_gate_pass      = (soc >= FLOOD_PREV_SOC_THRESHOLD_PCT) and (soc > effective_target)
        would_fire         = bool(
            snapshot.export_enabled
            and forecast_gate_pass
            and target_headroom_ok
            and soc_gate_pass
        )
        export_kwh = (
            max(0.0, (soc - effective_target) / 100.0 * snapshot.capacity_kwh)
            if would_fire else 0.0
        )
        return {
            "refill_label":         self._refill_day_label(snapshot, balance),
            "refill_solar_kwh":     round(refill_solar_kwh, 2),
            "refill_need_kwh":      round(refill_need_kwh, 2),
            "refill_vpp_kwh":       round(refill_vpp_kwh, 2),
            "refill_demand_kwh":    round(refill_demand_kwh, 2),
            "gate_mult":            FLOOD_PREV_FORECAST_MULT,
            "gate_threshold_kwh":   round(gate_threshold_kwh, 2),
            "ratio":                round(ratio, 3),
            "soc_threshold_pct":    FLOOD_PREV_SOC_THRESHOLD_PCT,
            "target_pct":           FLOOD_PREV_TARGET_PCT,
            "effective_target_pct": round(effective_target, 1),
            "current_soc_pct":      round(soc, 1),
            "export_enabled":       bool(snapshot.export_enabled),
            "forecast_gate_pass":   bool(forecast_gate_pass),
            "soc_gate_pass":        bool(soc_gate_pass),
            "target_headroom_ok":   bool(target_headroom_ok),
            "would_fire":           would_fire,
            "expected_export_kwh":  round(export_kwh, 2),
            "expected_revenue_gbp": round(export_kwh * (snapshot.export_rate_p or 12.0) / 100.0, 2),
        }

    def compute_flood_preview(self, snapshot: ManagerSnapshot) -> dict:
        """Public entry: flood-export gate preview for the current snapshot, for
        plugin.py to publish to the advisory. Recomputes the (pure, side-effect-free)
        24h balance so evaluate()'s control path is left completely untouched."""
        balance = self._calculate_24h_balance(snapshot)
        return self._compute_flood_preview(snapshot, balance)

    def _continue_flood_prevention(
        self, snapshot: ManagerSnapshot, balance: SufficiencyBalance
    ) -> Decision:
        """Decide whether to continue or stop an in-progress flood prevention pre-drain.

        Called from evaluate() when export_active=True and flood_prev_target_soc > 0.
        Returns ACTION_START_EXPORT (idempotent — plugin.py skips if already exporting)
        to continue, or ACTION_SELF_CONSUMPTION to stop and clean up.

        Stopping triggers plugin.py's prev_export → SELF_CONSUMPTION branch which:
          - calls set_self_consumption()
          - resets HOLD_ESS_DISCHARGE_CUTOFF to health_floor
          - clears flood_prev_target_soc in the store
        """
        target = snapshot.flood_prev_target_soc

        # Export disabled mid-drain (storm override / power-cut lockout forced
        # export_enabled False after the drain started) — that safety gate must
        # win, exactly as it does for the VPP override above. Without this a
        # calm-night drain to 40% keeps exporting the storm reserve on the night
        # a power cut is most likely. Stopping routes through plugin.py's
        # prev_export → SELF_CONSUMPTION branch, which resets the hardware
        # discharge cutoff and clears the flood target.
        if not snapshot.export_enabled:
            return Decision(
                action      = ACTION_SELF_CONSUMPTION,
                reason      = (
                    f"Flood prevention: aborting — export disabled mid-drain "
                    f"(storm/lockout) at SOC {snapshot.current_soc_pct:.1f}%"
                ),
                dawn_viable = True,
            )

        # A storm override may have raised dawn_target_pct above the drain target
        # after the drain started — never drain below the higher of the two.
        effective_stop = max(target, snapshot.dawn_target_pct)

        # Dawn broke — stop now; ACTION_SOLAR_OVERFLOW first-entry handles cutoff reset
        if balance.is_daytime:
            return Decision(
                action      = ACTION_SELF_CONSUMPTION,
                reason      = (
                    f"Flood prevention: stopping at dawn (SOC {snapshot.current_soc_pct:.1f}%, "
                    f"target {target:.0f}%) — daytime solar will continue charging"
                ),
                dawn_viable = True,
            )

        # Target SOC reached — hardware cutoff will have stopped the discharge;
        # confirm and return to self-consumption. effective_stop (not the raw
        # target) so a mid-drain storm override's raised floor ends the drain
        # early rather than draining through the mandated reserve.
        if snapshot.current_soc_pct <= effective_stop:
            return Decision(
                action      = ACTION_SELF_CONSUMPTION,
                reason      = (
                    f"Flood prevention: target {effective_stop:.0f}% reached "
                    f"(SOC {snapshot.current_soc_pct:.1f}%) — returning to self-consumption"
                ),
                dawn_viable = True,
            )

        # Conditions changed (rare): refill day is no longer abundantly sunny — abort.
        # Includes any newly-announced Axle VPP export on the refill day.
        refill_solar_kwh, refill_need_kwh, refill_vpp_kwh = self._refill_day_view(
            snapshot, balance
        )
        refill_demand_kwh = refill_need_kwh + refill_vpp_kwh
        if (refill_demand_kwh <= 0
                or refill_solar_kwh < FLOOD_PREV_FORECAST_MULT * refill_demand_kwh):
            refill_label = self._refill_day_label(snapshot, balance)
            vpp_note = f" + Axle {refill_vpp_kwh:.1f}" if refill_vpp_kwh > 0.01 else ""
            return Decision(
                action      = ACTION_SELF_CONSUMPTION,
                reason      = (
                    f"Flood prevention: aborting — {refill_label.lower()} forecast "
                    f"{refill_solar_kwh:.1f} kWh no longer >= "
                    f"{FLOOD_PREV_FORECAST_MULT:.0f}x need "
                    f"({refill_demand_kwh:.1f} kWh{vpp_note})"
                ),
                dawn_viable = True,
            )

        # Still nighttime, above target, sunny forecast — continue pre-drain
        # ACTION_START_EXPORT is idempotent: plugin.py's `if not prev_export` guard
        # skips the Modbus calls when export is already running, so no register noise.
        return Decision(
            action          = ACTION_START_EXPORT,
            reason          = (
                f"Flood prevention in progress: SOC {snapshot.current_soc_pct:.1f}% → "
                f"{effective_stop:.0f}% target "
                f"({snapshot.current_soc_pct - effective_stop:.1f}% remaining)"
            ),
            power_watts     = int(snapshot.max_export_kw * 1000),
            target_soc_pct  = effective_stop,
            dawn_viable     = True,
            soc_at_dawn_kwh = balance.battery_at_dawn_kwh,
        )

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
        """Return the next occurrence of a LOCAL (Europe/London) HH:MM window
        start, expressed in `now`'s own timezone.

        window_start_str is the local-time cheap-window boundary (Go/Flux/iGo).
        Building it naively against a UTC `now` would be an hour off in BST and
        schedule the import at the wrong instant — so do the arithmetic in local
        time, then convert the result back to now's tz.
        """
        try:
            h, m = window_start_str.split(":")
            h, m = int(h), int(m)
        except (ValueError, AttributeError):
            return None
        try:
            import pytz
            london          = pytz.timezone("Europe/London")
            local_now       = now.astimezone(london) if now.tzinfo else london.localize(now)
            local_naive_now = local_now.replace(tzinfo=None)
            cand_naive      = local_naive_now.replace(
                hour=h, minute=m, second=0, microsecond=0
            )
            if cand_naive <= local_naive_now:
                cand_naive += timedelta(days=1)
            candidate = london.localize(cand_naive)        # localize handles DST gap/fold
            return candidate.astimezone(now.tzinfo) if now.tzinfo else cand_naive
        except Exception:
            # Fallback (pytz unavailable): original naive behaviour, UTC == local.
            candidate = now.replace(hour=h, minute=m, second=0, microsecond=0)
            if candidate <= now:
                candidate += timedelta(days=1)
            return candidate

