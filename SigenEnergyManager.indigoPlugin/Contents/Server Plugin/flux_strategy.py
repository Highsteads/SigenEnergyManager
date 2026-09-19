#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    flux_strategy.py
# Description: The Octopus Flux planner. Pure stdlib. Published paired rates, a
#              solar forecast, a household profile, event commitments and one
#              battery observation in; one decision, or a refusal, out.
# Author:      CliveS & Claude Opus 5 (1M context)
# Date:        16-09-2026
# Version:     2.0
#
# v2.0 answers the Codex review (docs/flux-codex-review-required.md):
#   * the energy budget is CHRONOLOGICAL — a half-hourly simulation in UTC, not a
#     day-total subtraction. A sunny afternoon cannot supply a 06:00 load;
#   * event commitments (Axle, Octopus) are reserved energy, not just an owner flag;
#   * two floors, not one: what may be EXPORTED is not what the house may CONSUME;
#   * one-way efficiencies whose product is the configured round trip;
#   * strict input validation — NaN, bools-as-numbers, negative ages and future
#     observations are refused;
#   * all integration walks UTC and indexes local buckets, so a clock change
#     neither skips nor invents a half-hour.
#
# Order of precedence in every decision: reserve floor > event commitments >
# household demand > discretionary trading. Anything missing is a defer.

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
import math
import re
from typing import Mapping, Optional, Tuple


# --- The structural Flux clock. Also pinned in flux_execution._valid_target;
# derive_bands refuses to plan if the published rates disagree with it. ---
FLUX_CHEAP_START = time(2, 0)
FLUX_CHEAP_END   = time(5, 0)
FLUX_PEAK_START  = time(16, 0)
FLUX_PEAK_END    = time(19, 0)

# Remote EMS modes the executor accepts.
EMS_SELF_CONSUMPTION = 2
EMS_CHARGE_GRID      = 3
EMS_DISCHARGE_PV     = 5

MODE_CHARGE       = "charge"
MODE_HOLD         = "hold"
MODE_SUPPLY_HOUSE = "supply_house"
MODE_EXPORT       = "export"
MODE_SOLAR        = "solar"          # hand back to the existing manager
MODE_DEFER        = "defer"          # something is missing — touch nothing

MAX_DECISION_MINUTES   = 25          # executor allows 30
MIN_ARBITRAGE_MARGIN_P = 1.0
DEFAULT_WEAR_P_PER_KWH = 5.0
DEFAULT_RESERVE_PCT    = 20.0

MAX_RATES_AGE_S     = 3 * 3600
MAX_FORECAST_AGE_S  = 3 * 3600
MAX_TELEMETRY_AGE_S = 120
MAX_PROFILE_AGE_S   = 7 * 86400      # rebuilt daily; a week old is a fault
MAX_FLOW_AGE_S      = 120            # power flows, for site headroom

HOLD_BEFORE_PEAK_MINUTES = 120
MIN_TRADE_KWH            = 0.5
SIM_STEP_MINUTES         = 30
SEARCH_TOLERANCE_KWH     = 1e-6      # the binary search's own convergence error
MAX_HORIZON_HOURS        = 36        # bounds every simulation


class FluxDeferred(Exception):
    """Internal: an input cannot support this decision. Never escapes plan()."""


# ================================================================
# Strict numeric validation
# ================================================================

def _num(value, name, low=None, high=None):
    """A finite real number, or raise. Bools are NOT numbers here.

    `isinstance(True, int)` is True in Python, so a bool reaching a power or a
    percentage would be silently read as 1 or 0.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise FluxDeferred(f"{name} is not a number")
    v = float(value)
    if not math.isfinite(v):
        raise FluxDeferred(f"{name} is not a finite number")
    if low is not None and v < low:
        raise FluxDeferred(f"{name} is below {low}")
    if high is not None and v > high:
        raise FluxDeferred(f"{name} is above {high}")
    return v


def _age(value, name, limit):
    """A non-negative age in seconds, within `limit`, or raise.

    A negative age means the timestamp is in the future — a clock step or a
    fabricated stamp. Either way it is not evidence of freshness.
    """
    v = _num(value, name)
    if v < 0:
        raise FluxDeferred(f"{name} is in the future")
    if v > limit:
        raise FluxDeferred(f"{name.replace(' age', '')} is stale")
    return v


def _aware(value, name):
    if not isinstance(value, datetime) or value.tzinfo is None \
            or value.utcoffset() is None:
        raise FluxDeferred(f"{name} is not an aware datetime")
    return value.astimezone(timezone.utc)


# ================================================================
# Inputs
# ================================================================

@dataclass(frozen=True)
class RateSpan:
    start: datetime
    end:   datetime
    p:     float


@dataclass(frozen=True)
class EventCommitment:
    """Energy this house has already promised somebody else.

    `energy_kwh` is GRID-SIDE energy for an export commitment (what the meter
    must show), or grid-side import for a free-import window.

    ACCOUNTING, precisely. One exported kWh is counted ONCE as physical energy —
    which is why overlapping commitments are combined by taking the largest, not
    by adding. The MONEY is a separate question: that same kWh can earn the
    ordinary Flux export rate from the supplier AND an event payment from the
    scheme that called for it, and both are real. What must never happen is
    counting the kWh twice, or counting either payment twice. So the reward is
    carried here for reporting and is never added to `bands.export_peak_p`, which
    is what decides whether a DISCRETIONARY trade is worth doing — a trade the
    event would have paid for anyway is not made more profitable by the event.
    """
    source:           str              # "axle" | "octopus" | ...
    kind:             str              # "export" | "import"
    start:            datetime
    end:              datetime
    energy_kwh:       float
    announced_at:     Optional[datetime] = None
    reward_p_per_kwh: Optional[float]    = None
    event_id:         str                = ""

    def overlap_kwh(self, a, b):
        """Commitment energy falling inside [a, b), pro-rated by time."""
        a = a.astimezone(timezone.utc)
        b = b.astimezone(timezone.utc)
        if self.end <= self.start or b <= a:
            return 0.0
        overlap = (min(b, self.end) - max(a, self.start)).total_seconds()
        if overlap <= 0:
            return 0.0
        total = (self.end - self.start).total_seconds()
        return self.energy_kwh * overlap / total

    def key(self):
        """Identity for change detection — a late announcement must re-plan."""
        return (self.source, self.kind, self.event_id,
                self.start.isoformat(), self.end.isoformat(),
                round(float(self.energy_kwh), 3))


@dataclass(frozen=True)
class FluxBands:
    import_cheap_p: float
    import_day_p:   float
    import_peak_p:  float
    export_cheap_p: float
    export_day_p:   float
    export_peak_p:  float
    covers_until:   datetime


@dataclass(frozen=True)
class FluxSite:
    capacity_kwh:          float
    charge_power_w:        int
    discharge_power_w:     int
    export_limit_w:        int
    import_limit_w:        int
    import_limit_verified: bool
    efficiency:            float = 0.94   # ROUND TRIP
    wear_p_per_kwh:        float = DEFAULT_WEAR_P_PER_KWH
    reserve_pct:           float = DEFAULT_RESERVE_PCT
    policy_floor_pct:      float = 0.0
    max_charge_soc_pct:    float = 100.0

    @property
    def one_way_efficiency(self):
        """Charge and discharge each, whose product is the configured round trip."""
        return math.sqrt(self.efficiency)


@dataclass(frozen=True)
class FluxFlows:
    """Live power, for site headroom. Watts, inverter sign conventions."""
    pv_w:         float
    house_w:      float
    grid_w:       float      # positive = importing
    battery_w:    float = 0.0


@dataclass(frozen=True)
class FluxInputs:
    now:              datetime
    local_tz:         object
    bands:            Optional[FluxBands]
    site:             FluxSite
    soc_pct:          Optional[float]
    house:            Optional["HalfHourProfile"]
    pv:               Optional["HourlyPvForecast"]
    flows:            Optional[FluxFlows]  = None
    commitments:      Tuple[EventCommitment, ...] = field(default_factory=tuple)
    tariff_verified:  bool = False
    commissioned:     bool = False
    enabled:          bool = False
    rates_age_s:      Optional[float] = None
    forecast_age_s:   Optional[float] = None
    telemetry_age_s:  Optional[float] = None
    flows_age_s:      Optional[float] = None
    profile_age_s:    Optional[float] = None


@dataclass(frozen=True)
class FluxDecision:
    mode:                  str
    owns:                  bool
    reason:                str
    decision_at:           Optional[datetime] = None
    ems_mode:              Optional[int]      = None
    charge_limit_w:        int                = 0
    discharge_limit_w:     int                = 0
    charge_cutoff_pct:     float              = 100.0
    discharge_cutoff_pct:  float              = DEFAULT_RESERVE_PCT
    decision_until:        Optional[datetime] = None
    protect_soc_pct:       float              = DEFAULT_RESERVE_PCT
    household_floor_pct:   float              = DEFAULT_RESERVE_PCT
    planned_kwh:           float              = 0.0
    margin_p:              float              = 0.0
    committed_kwh:         float              = 0.0
    infeasible_kwh:        float              = 0.0

    @property
    def deferred(self):
        return self.mode == MODE_DEFER

    def control_key(self):
        return (self.mode, self.ems_mode, self.charge_limit_w, self.discharge_limit_w,
                round(self.charge_cutoff_pct, 1), round(self.discharge_cutoff_pct, 1))


# ================================================================
# Local-time helpers — UTC arithmetic, local indexing
# ================================================================

def _wall(tz, day, hhmm):
    """Local wall time `hhmm` on local date `day`, returned IN UTC.

    UTC, not the local zone, and that is the whole point. Subtracting two aware
    datetimes that share a tzinfo makes Python ignore the zone entirely, so on a
    clock-change day `local_midnight_tomorrow - local_midnight_today` returns 24
    hours when the real answer is 23 or 25. Every boundary here is normalised to
    UTC at birth, so no arithmetic downstream can make that mistake.

    A skipped wall time is stepped forward an hour. The round trip has to go
    THROUGH UTC to detect one: `aware.astimezone(same_zone)` is a no-op and
    reports a time that does not exist as though it did.
    """
    naive = datetime(day.year, day.month, day.day, hhmm.hour, hhmm.minute)
    local = _attach(tz, naive)
    if local.astimezone(timezone.utc).astimezone(tz).replace(tzinfo=None) != naive:
        local = _attach(tz, naive + timedelta(hours=1))
    return local.astimezone(timezone.utc)


def _attach(tz, naive):
    """Attach `tz` to a naive local time, correctly for zoneinfo AND pytz.

    `naive.replace(tzinfo=pytz_zone)` yields that zone's LMT — for London that is
    a minute out, every time, for ever. pytz needs `localize`; zoneinfo must not
    be given one. Both reach this plugin: london_time prefers zoneinfo and falls
    back to pytz.
    """
    localize = getattr(tz, "localize", None)
    if callable(localize):
        return localize(naive, is_dst=True)
    return naive.replace(tzinfo=tz)


def _local_date(now, tz):
    return now.astimezone(tz).date()


def next_local(now, tz, hhmm):
    """The next instant of local wall time `hhmm` strictly after `now`."""
    day = _local_date(now, tz)
    for offset in (0, 1, 2):
        candidate = _wall(tz, day + timedelta(days=offset), hhmm)   # already UTC
        if candidate > now:
            return candidate
    raise FluxDeferred("could not resolve the next window boundary")


def in_window(now, tz, start, end):
    hhmm = now.astimezone(tz).time()
    return (start <= hhmm < end) if start < end else (hhmm >= start or hhmm < end)


def next_cheap_start(now, tz):
    return next_local(now, tz, FLUX_CHEAP_START)


def next_boundary(now, tz):
    return min(next_local(now, tz, h) for h in
               (FLUX_CHEAP_START, FLUX_CHEAP_END, FLUX_PEAK_START, FLUX_PEAK_END))


# ================================================================
# Energy series — walk UTC, index local
# ================================================================

class HalfHourProfile:
    """48 half-hourly kWh figures, indexed by LOCAL half-hour."""

    def __init__(self, slots, tz, scale=1.0):
        if tz is None:
            raise ValueError("a local timezone is required")
        raw = list(slots or [])
        if len(raw) != 48:
            raise ValueError(f"consumption profile must have 48 slots, got {len(raw)}")
        out = []
        for i, s in enumerate(raw):
            if isinstance(s, bool) or not isinstance(s, (int, float)) \
                    or not math.isfinite(float(s)) or float(s) < 0:
                raise ValueError(f"consumption profile slot {i} is not a usable kWh value")
            out.append(float(s) * float(scale))
        if sum(out) <= 0.0:
            raise ValueError("consumption profile is empty")
        self.slots = tuple(out)
        self.tz    = tz

    def _rate_kwh_per_hour(self, instant):
        local = instant.astimezone(self.tz)
        idx   = (local.hour * 2) + (1 if local.minute >= 30 else 0)
        return self.slots[idx] * 2.0      # a half-hour figure is kWh per half hour

    def _next_edge(self, instant):
        """Next real half-hour; UK local boundaries align with UTC half-hours."""
        utc = instant.astimezone(timezone.utc)
        return utc.replace(minute=(utc.minute // 30) * 30, second=0,
                           microsecond=0) + timedelta(minutes=30)

    def kwh_between(self, a, b):
        """Integrate in UTC, stepping on REAL local slot boundaries.

        The boundaries matter as much as the UTC walk. Stepping a fixed thirty
        minutes from `a` charges the whole of 15:55-16:25 at the 15:30 slot's
        rate, so a window that straddles a boundary is priced entirely from the
        wrong side of it. Walking edge to edge charges each part at its own slot.

        Walking UTC is what makes a clock change safe: the 25-hour day integrates
        25 hours and the 23-hour day 23, because nothing here does local
        arithmetic.
        """
        a = a.astimezone(timezone.utc)
        b = b.astimezone(timezone.utc)
        if b <= a:
            return 0.0
        total  = 0.0
        cursor = a
        while cursor < b:
            end   = min(self._next_edge(cursor), b)
            hours = (end - cursor).total_seconds() / 3600.0
            total += self._rate_kwh_per_hour(cursor) * hours
            cursor = end
        return total


class HourlyPvForecast:
    """Bias-corrected PV from hourly buckets keyed by LOCAL wall time."""

    def __init__(self, buckets: Mapping[str, float], tz, bias=1.0, bias_by_date=None):
        """`bias_by_date` maps a local `date` to that day's own correction factor.

        The forecast publishes a SEPARATE factor for today and tomorrow, and they
        differ — the day in progress has measured generation behind it and the
        next one has none. Applying today's factor to tomorrow's buckets biases
        the overnight charge by whatever the two days disagree by. `bias` stays
        as the fallback for any date not named.
        """
        if tz is None:
            raise ValueError("a local timezone is required")
        self.tz   = tz
        b = float(bias if bias not in (None, 0) else 1.0)
        if not math.isfinite(b) or b <= 0:
            raise ValueError("solar bias factor is not usable")
        self.bias = b
        self.bias_by_date = {}
        for day, factor in (bias_by_date or {}).items():
            try:
                value = float(factor)
            except (TypeError, ValueError):
                continue
            if math.isfinite(value) and value > 0:
                self.bias_by_date[day] = value
        parsed = {}
        for key, wh in (buckets or {}).items():
            try:
                start = datetime.strptime(str(key), "%Y-%m-%d %H:%M:%S")
                value = float(wh)
            except (TypeError, ValueError):
                continue
            if isinstance(wh, bool) or not math.isfinite(value) or value < 0:
                continue
            parsed[(start.date(), start.hour)] = value
        self.buckets = parsed

    def _bucket(self, instant):
        local = instant.astimezone(self.tz)
        return self.buckets.get((local.date(), local.hour))

    def _bias_for(self, instant):
        return self.bias_by_date.get(instant.astimezone(self.tz).date(), self.bias)

    def _next_edge(self, instant):
        """Next real hour, including each occurrence of the UK repeated hour."""
        utc = instant.astimezone(timezone.utc)
        return utc.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)

    def kwh_between(self, a, b):
        """Integrate on REAL local hour boundaries — see HalfHourProfile."""
        a = a.astimezone(timezone.utc)
        b = b.astimezone(timezone.utc)
        if b <= a or not self.buckets:
            return 0.0
        total  = 0.0
        cursor = a
        while cursor < b:
            end = min(self._next_edge(cursor), b)
            wh  = self._bucket(cursor)
            if wh:
                hours  = (end - cursor).total_seconds() / 3600.0
                # The day's OWN factor, applied per step — a window that spans
                # midnight covers two days with two different corrections.
                total += wh * hours / 1000.0 * self._bias_for(cursor)
            cursor = end
        return total

    def covers(self, a, b, step_minutes=SIM_STEP_MINUTES):
        """True only when every step in [a, b) has a published bucket.

        A missing hour is NOT zero sun. Zero is the safe reading when the answer
        decides what to protect and the expensive one when it decides what to buy,
        so the caller asks explicitly and the planner defers when the answer would
        be load-bearing.
        """
        a = a.astimezone(timezone.utc)
        b = b.astimezone(timezone.utc)
        if b <= a:
            return True
        step   = timedelta(minutes=step_minutes)
        cursor = a
        while cursor < b:
            if self._bucket(cursor) is None:
                return False
            cursor += step
        return True


# ================================================================
# Band derivation — contiguous, single-priced, account-paired
# ================================================================

def _covered_until(spans, start):
    """How far a set of spans covers contiguously from `start`, with no conflicts.

    Returns None on a gap at `start` or on overlapping spans that disagree on
    price — a disagreement means two products (an October change, say) and
    averaging across them would invent a price nobody published.
    """
    ordered = sorted(spans, key=lambda s: s.start)
    cursor  = start
    for span in ordered:
        if span.end <= cursor:
            continue
        if span.start > cursor:
            return cursor if cursor > start else None
        if span.start < cursor:
            overlapping = [o for o in ordered
                           if o.start < span.end and o.end > span.start]
            if len({round(float(o.p), 4) for o in overlapping}) > 1:
                return None
        cursor = span.end
    return cursor if cursor > start else None


def _band_price(spans, tz, band_start, band_end, day):
    """The single published price of one band on one local day, or None.

    One day, never a mean across days: Flux re-versions, and the average of an
    old product and a new one is a price that was never charged.
    """
    values = set()
    for span in spans:
        local = span.start.astimezone(tz)
        if local.date() != day:
            continue
        hhmm = local.time()
        inside = (band_start <= hhmm < band_end if band_start < band_end
                  else (hhmm >= band_start or hhmm < band_end))
        if inside:
            values.add(round(float(span.p), 4))
    if len(values) != 1:
        return None
    return values.pop()


def _day_price(spans, tz, day):
    values = set()
    for span in spans:
        local = span.start.astimezone(tz)
        if local.date() != day:
            continue
        hhmm = local.time()
        if FLUX_CHEAP_START <= hhmm < FLUX_CHEAP_END:
            continue
        if FLUX_PEAK_START <= hhmm < FLUX_PEAK_END:
            continue
        values.add(round(float(span.p), 4))
    if len(values) != 1:
        return None
    return values.pop()


def _band_window(spans, tz, day, pick_cheapest):
    """(start, end) local times of the cheapest/dearest band on one day."""
    priced = {}
    for span in spans:
        if span.end <= span.start:
            continue
        s = span.start.astimezone(tz)
        if s.date() != day:
            continue
        e = span.end.astimezone(tz)
        priced.setdefault(round(float(span.p), 4), set()).add(
            (s.strftime("%H:%M"), e.strftime("%H:%M")))
    if not 3 <= len(priced) <= 4:
        return None
    windows = priced[min(priced) if pick_cheapest else max(priced)]
    if len(windows) != 1:
        return None
    start_s, end_s = windows.pop()
    if start_s == end_s:
        return None
    return (time(int(start_s[:2]), int(start_s[3:])),
            time(int(end_s[:2]), int(end_s[3:])))


def derive_bands(import_spans, export_spans, tz, now, day=None):
    """Six prices for ONE local day plus how far the published rates reach.

    Refuses — returns None — unless all of this holds:
      * both sides cover contiguously from `now`, with no gaps and no
        conflicting overlapping prices;
      * each band on the chosen day has ONE published price (no averaging);
      * the cheapest import band and the dearest export band sit on the clock
        both this module and the executor pin.
    """
    if not import_spans or not export_spans or tz is None:
        return None
    now = _aware(now, "now")
    imp_until = _covered_until(import_spans, now)
    exp_until = _covered_until(export_spans, now)
    if imp_until is None or exp_until is None:
        return None

    day = day or _local_date(now, tz)
    if _band_window(import_spans, tz, day, True) != (FLUX_CHEAP_START, FLUX_CHEAP_END):
        return None
    if _band_window(export_spans, tz, day, False) != (FLUX_PEAK_START, FLUX_PEAK_END):
        return None

    prices = {
        "import_cheap_p": _band_price(import_spans, tz, FLUX_CHEAP_START, FLUX_CHEAP_END, day),
        "import_peak_p":  _band_price(import_spans, tz, FLUX_PEAK_START, FLUX_PEAK_END, day),
        "import_day_p":   _day_price(import_spans, tz, day),
        "export_cheap_p": _band_price(export_spans, tz, FLUX_CHEAP_START, FLUX_CHEAP_END, day),
        "export_peak_p":  _band_price(export_spans, tz, FLUX_PEAK_START, FLUX_PEAK_END, day),
        "export_day_p":   _day_price(export_spans, tz, day),
    }
    if any(v is None for v in prices.values()):
        return None
    return FluxBands(covers_until=min(imp_until, exp_until), **prices)


# ================================================================
# The chronological energy budget
# ================================================================

def commitment_energy_kwh(commitments, a, b, kind="export"):
    """Public alias — the ONE overlap budget. Callers outside this module must
    use it rather than summing, or two schemes paying for the same exported kWh
    get reserved twice."""
    return _commitment_kwh(commitments, a, b, kind=kind)


def _commitment_kwh(commitments, a, b, kind="export"):
    """Physical energy the meter must show over [a, b), NOT a sum of rewards.

    Two schemes can pay for the SAME exported kWh — an Axle dispatch that happens
    to coincide with an Octopus session earns both, and the house still exports
    once. Summing them would reserve twice the energy and sell none of it. So
    overlapping commitments are combined by taking the largest demand in each
    slice, and only genuinely separate windows add up.
    """
    a = a.astimezone(timezone.utc)
    b = b.astimezone(timezone.utc)
    relevant = [c for c in commitments if c.kind == kind and c.end > a and c.start < b]
    if not relevant:
        return 0.0
    edges = sorted({a, b} | {c.start for c in relevant if a < c.start < b}
                   | {c.end for c in relevant if a < c.end < b})
    total = 0.0
    for start, end in zip(edges, edges[1:]):
        total += max(c.overlap_kwh(start, end) for c in relevant)
    return total


def build_steps(inputs, a, b):
    """The half-hourly demand/supply grid for [a, b), built ONCE.

    Separated from the walk because `required_start_kwh` binary-searches over it:
    rebuilding the series forty times meant forty passes over the profile and the
    forecast, and the supervisor runs this on the plugin's only thread every ten
    seconds. Built once, the search is forty cheap numeric loops.
    """
    a       = a.astimezone(timezone.utc)
    b       = b.astimezone(timezone.utc)
    steps   = []
    step    = timedelta(minutes=SIM_STEP_MINUTES)
    cursor  = a
    horizon = min(b, a + timedelta(hours=MAX_HORIZON_HOURS))
    while cursor < horizon:
        end = min(cursor + step, horizon)
        steps.append((
            cursor,
            (end - cursor).total_seconds() / 3600.0,
            inputs.house.kwh_between(cursor, end),
            inputs.pv.kwh_between(cursor, end),
            _commitment_kwh(inputs.commitments, cursor, end, "export"),
        ))
        cursor = end
    return steps


def run_steps(site, steps, start_kwh, serve_house=True, no_charge_until=None):
    """Walk a prebuilt grid. Returns (end_kwh, min_kwh, unmet_kwh, max_kwh).

    Per step, in this order:
      1. PV serves the house directly — that energy never touches the battery,
         which is why gross solar is not a headroom figure;
      2. surplus PV serves any export commitment;
      3. the battery covers the rest of the commitment, at the discharge
         efficiency and within the discharge power limit;
      4. the battery covers the remaining house load, if `serve_house`;
      5. whatever PV is still spare charges the battery, at the charge efficiency
         and within the charge power limit, up to capacity.

    Both power limits bind per step. Without them a 12 kW hour of sun banks 12
    kWh through a 1 kW charger, and the plan rests on energy the inverter could
    never have moved.

    `no_charge_until` models the regime an EXPORT decision creates: in mode 5 the
    charge limit is zero, so surplus PV goes to the grid, not the battery. A
    floor worked out assuming that PV would be banked, and then acted on by a
    decision that prevents the banking, invalidates its own premise.

    Commitment shortfall is reported, never absorbed.
    """
    eff      = site.one_way_efficiency
    capacity = site.capacity_kwh
    energy   = max(0.0, min(float(start_kwh), capacity))
    low = high = energy
    unmet    = 0.0
    for cursor, hours, house, pv, event in steps:
        max_in  = site.charge_power_w / 1000.0 * hours
        max_out = site.discharge_power_w / 1000.0 * hours

        direct    = min(pv, house)
        surplus   = pv - direct
        house_rem = house - direct
        out_left  = max_out

        from_pv   = min(surplus, event)
        surplus  -= from_pv
        event_rem = event - from_pv
        if event_rem > 0:
            need = event_rem / eff
            take = min(need, energy, out_left)
            energy   -= take
            out_left -= take
            if take < need - 1e-9:
                unmet += (need - take) * eff

        if serve_house and house_rem > 0:
            need = house_rem / eff
            take = min(need, energy, out_left)
            energy   -= take
            out_left -= take

        if surplus > 0 and (no_charge_until is None or cursor >= no_charge_until):
            energy = min(capacity, energy + min(surplus, max_in) * eff)

        low  = min(low, energy)
        high = max(high, energy)
    return energy, low, unmet, high


def simulate(inputs, start_kwh, a, b, serve_house=True, no_charge_until=None):
    """Convenience wrapper: build the grid and walk it once."""
    return run_steps(inputs.site, build_steps(inputs, a, b), start_kwh,
                     serve_house=serve_house, no_charge_until=no_charge_until)


def required_start_kwh(inputs, a, b, floor_kwh, no_charge_until=None):
    """Least battery energy at `a` that keeps the low-water mark at `floor_kwh`
    and meets every commitment in [a, b).

    Binary search over a forward walk. A search rather than a closed form because
    the two efficiencies, the two power limits, the capacity ceiling and PV
    arriving mid-horizon all interact; forty numeric passes over a prebuilt grid
    costs nothing, and the walk is the thing being tested.

    Returns (required_kwh, infeasible_kwh) — infeasible when even a full battery
    cannot do it, which is reported, never rounded away.
    """
    capacity = inputs.site.capacity_kwh
    steps    = build_steps(inputs, a, b)

    def ok(start):
        _end, low, unmet, _high = run_steps(inputs.site, steps, start,
                                            no_charge_until=no_charge_until)
        return (low >= floor_kwh - SEARCH_TOLERANCE_KWH
                and unmet <= SEARCH_TOLERANCE_KWH)

    if ok(floor_kwh):
        lo, hi = 0.0, floor_kwh
    elif not ok(capacity):
        _end, low, unmet, _high = run_steps(inputs.site, steps, capacity,
                                            no_charge_until=no_charge_until)
        return capacity, max(0.0, (floor_kwh - low)) + unmet
    else:
        lo, hi = floor_kwh, capacity
    for _ in range(40):
        mid = (lo + hi) / 2.0
        if ok(mid):
            hi = mid
        else:
            lo = mid
    # `ok` accepts within a tolerance, so the converged answer sits a hair BELOW
    # the true requirement. Give it back, rounded the safe way: a requirement
    # under-reported by a millionth of a kWh is what turns an exact 80% target
    # into 79% once it is floored to a whole percent.
    return min(capacity, hi + SEARCH_TOLERANCE_KWH), 0.0


def _pct(kwh, capacity):
    return max(0.0, min(100.0, kwh / capacity * 100.0)) if capacity > 0 else 0.0


# ================================================================
# Floors
# ================================================================

def _reserve_floor_pct(site):
    """The flat reserve, raised by policy. Never rounded down."""
    return min(100.0, float(math.ceil(max(float(site.reserve_pct),
                                          float(site.policy_floor_pct)))))


def household_floor_pct(inputs, until):
    """The floor the HOUSE may not eat into: reserve plus event commitments.

    Separate from the export floor on purpose. A house holding its reserve plus
    exactly its evening demand must be allowed to consume that demand — a single
    floor that protected all future household consumption from the household
    itself would leave the battery full and the house importing at the day rate,
    which is precisely backwards.
    """
    site  = inputs.site
    floor = _reserve_floor_pct(site)
    committed = _commitment_kwh(inputs.commitments, inputs.now, until, "export")
    if committed <= 0:
        return floor
    # The battery has to hold the promised grid-side energy plus its losses.
    need = committed / site.one_way_efficiency
    return min(100.0, float(math.ceil(floor + _pct(need, site.capacity_kwh))))


def export_floor_pct(inputs, until, no_charge_until=None):
    """The floor DISCRETIONARY export may not sell through.

    Everything the house is forecast to need between now and `until`, plus every
    committed kWh, plus the reserve — worked out chronologically, so a sunny
    tomorrow afternoon cannot pay for tonight, and worked out under the regime
    the export itself creates (see `no_charge_until`).
    """
    site      = inputs.site
    floor_pct = _reserve_floor_pct(site)
    floor_kwh = site.capacity_kwh * floor_pct / 100.0
    need_kwh, _infeasible = required_start_kwh(inputs, inputs.now, until, floor_kwh,
                                               no_charge_until=no_charge_until)
    return min(100.0, float(math.ceil(_pct(need_kwh, site.capacity_kwh))))


# ================================================================
# Prices
# ================================================================

def _export_is_profitable(bands, site):
    """(worth doing, margin p/kWh) for storing a cheap kWh and selling it at peak.

    One-way efficiencies: a kWh delivered to the grid cost cheap_p / (eff * eff)
    to put there, which is the configured round trip, plus wear.
    """
    cost_p   = (bands.import_cheap_p / (site.one_way_efficiency ** 2)) + site.wear_p_per_kwh
    margin_p = bands.export_peak_p - cost_p
    return margin_p >= MIN_ARBITRAGE_MARGIN_P, round(margin_p, 3)


def _hold_is_profitable(bands, site):
    """Holding means buying at the day rate now to sell at peak later."""
    margin_p = bands.export_peak_p - bands.import_day_p - site.wear_p_per_kwh
    return margin_p >= MIN_ARBITRAGE_MARGIN_P, round(margin_p, 3)


# ================================================================
# Physical headroom
# ================================================================

def import_headroom_w(inputs):
    """Spare site import capacity: the limit less what the house is already drawing.

    The inverter rating is not spare supply capacity. Requires fresh flows —
    without them the headroom is unknown, and an unknown headroom defers.
    """
    site = inputs.site
    if inputs.flows is None:
        raise FluxDeferred("the live power readings needed to size a grid charge "
                           "are not available")
    _age(inputs.flows_age_s, "the power readings age", MAX_FLOW_AGE_S)
    house_net_w = max(0.0, _num(inputs.flows.house_w, "house power")
                      - max(0.0, _num(inputs.flows.pv_w, "PV power")))
    return max(0, int(site.import_limit_w - house_net_w))


def export_headroom_w(inputs):
    """Spare battery discharge before the grid export cap is reached.

    Grid export is battery discharge plus PV less house load, so the PV already
    flowing counts against the cap.
    """
    site = inputs.site
    if inputs.flows is None:
        raise FluxDeferred("the live power readings needed to size an export are "
                           "not available")
    _age(inputs.flows_age_s, "the power readings age", MAX_FLOW_AGE_S)
    pv_surplus_w = max(0.0, _num(inputs.flows.pv_w, "PV power")
                       - _num(inputs.flows.house_w, "house power"))
    return max(0, int(site.export_limit_w - pv_surplus_w))


# ================================================================
# Gates
# ================================================================

def _validate(inputs):
    """Raises FluxDeferred with a plain reason for anything unusable."""
    site = inputs.site
    if not inputs.enabled:
        raise FluxDeferred("the Flux strategy is switched off")
    if not inputs.commissioned:
        raise FluxDeferred("commissioning has not been signed off — the physical "
                           "limits and the hand-back behaviour have not been proven "
                           "on this inverter")
    if not inputs.tariff_verified:
        raise FluxDeferred("the account has not been verified as Flux on BOTH the "
                           "import and the export agreement (a tariff override is a "
                           "rehearsal, not proof)")
    _aware(inputs.now, "now")
    if inputs.local_tz is None:
        raise FluxDeferred("the local timezone is unavailable")
    if inputs.bands is None:
        raise FluxDeferred("the published Flux import and export rates could not be "
                           "read as a contiguous Flux shape")
    _age(inputs.rates_age_s, "the Flux rates age", MAX_RATES_AGE_S)
    if inputs.bands.covers_until <= inputs.now:
        raise FluxDeferred("the published Flux rates do not reach the present moment")
    if inputs.soc_pct is None:
        raise FluxDeferred("the battery state of charge is unknown")
    _num(inputs.soc_pct, "the battery state of charge", 0.0, 100.0)
    _age(inputs.telemetry_age_s, "the inverter reading age", MAX_TELEMETRY_AGE_S)
    if inputs.house is None:
        raise FluxDeferred("the household consumption profile is not available")
    _age(inputs.profile_age_s, "the consumption profile age", MAX_PROFILE_AGE_S)
    if inputs.pv is None:
        raise FluxDeferred("the solar forecast is not available")
    _age(inputs.forecast_age_s, "the solar forecast age", MAX_FORECAST_AGE_S)
    if not site.import_limit_verified:
        raise FluxDeferred("the site import limit has not been verified, so a grid "
                           "charge cannot be sized safely")
    _num(site.capacity_kwh, "the battery capacity", 0.1)
    _num(site.charge_power_w, "the charge power limit", 1)
    _num(site.discharge_power_w, "the discharge power limit", 1)
    _num(site.import_limit_w, "the site import limit", 1)
    _num(site.export_limit_w, "the site export limit", 0)
    _num(site.efficiency, "the battery efficiency", 0.01, 1.0)
    _num(site.wear_p_per_kwh, "the battery wear cost", 0.0)
    _num(site.reserve_pct, "the battery reserve", 0.0, 100.0)
    _num(site.policy_floor_pct, "the policy floor", 0.0, 100.0)
    _num(site.max_charge_soc_pct, "the maximum charge SOC", 1.0, 100.0)
    for c in inputs.commitments:
        _aware(c.start, f"the {c.source} commitment start")
        _aware(c.end, f"the {c.source} commitment end")
        _num(c.energy_kwh, f"the {c.source} commitment energy", 0.0)
        if c.end <= c.start:
            raise FluxDeferred(f"the {c.source} commitment ends before it starts")


def _decision_until(inputs, hard_end=None):
    """Soonest of: our lease, the next band edge, the price coverage, any window
    end, and the next commitment edge — a commitment starting is a re-plan."""
    ends = [inputs.now + timedelta(minutes=MAX_DECISION_MINUTES),
            next_boundary(inputs.now, inputs.local_tz),
            inputs.bands.covers_until]
    if hard_end is not None:
        ends.append(hard_end)
    for c in inputs.commitments:
        for edge in (c.start, c.end):
            if edge > inputs.now:
                ends.append(edge)
    return min(ends)


# ================================================================
# The window plans
# ================================================================

def _charge_plan(inputs):
    """(target_pct, power_w, buy_kwh, margin_p, household_kwh, infeasible_kwh)."""
    site   = inputs.site
    tz     = inputs.local_tz
    bands  = inputs.bands
    eff    = site.one_way_efficiency

    window_end  = next_local(inputs.now, tz, FLUX_CHEAP_END)
    horizon_end = next_local(window_end, tz, FLUX_CHEAP_START)

    # A charge buys on the strength of a forecast, so it must have one — for the
    # rest of the cheap window as well as the day it is buying for.
    if not inputs.pv.covers(inputs.now, horizon_end):
        raise FluxDeferred("the solar forecast does not cover the day ahead, so a "
                           "grid charge cannot be sized against it")

    floor_pct = _reserve_floor_pct(site)
    floor_kwh = site.capacity_kwh * floor_pct / 100.0
    have_kwh  = site.capacity_kwh * float(inputs.soc_pct) / 100.0

    # What must be in the battery by the END of the window. The house draws
    # during 02:00-05:00 too, so the requirement is worked from the window end
    # and the cheap-window load is added back below.
    need_at_end, infeasible = required_start_kwh(inputs, window_end, horizon_end, floor_kwh)

    # The house's own draw during the cheap window is served by the GRID, not by
    # the battery: mode 3 is Charge Grid First, and the target's discharge limit
    # is zero. So it is not bought into the battery — buying it twice would
    # overstate the charge by a whole window's consumption. It does compete for
    # site import capacity, and that is handled where it belongs, in the power
    # calculation below.
    household_target_kwh = min(need_at_end, site.capacity_kwh * site.max_charge_soc_pct / 100.0)
    household_buy_kwh    = max(0.0, household_target_kwh - have_kwh)

    # Headroom: only the NET surplus the sun is expected to put INTO the battery
    # is worth leaving room for. It can never push the target below what the
    # house and the commitments need.
    ceiling_kwh = site.capacity_kwh * site.max_charge_soc_pct / 100.0
    _end, _low, _unmet, peak_kwh = simulate(inputs, household_target_kwh,
                                            window_end, horizon_end)
    solar_gain_kwh = max(0.0, peak_kwh - household_target_kwh)
    headroom_ceiling = max(household_target_kwh,
                           min(ceiling_kwh, site.capacity_kwh - solar_gain_kwh))

    profitable, margin_p = _export_is_profitable(bands, site)
    arb_kwh = 0.0
    if profitable:
        peak_hours = (FLUX_PEAK_END.hour - FLUX_PEAK_START.hour)
        sellable   = min(site.discharge_power_w, site.export_limit_w) / 1000.0 * peak_hours
        spare      = max(0.0, headroom_ceiling - max(have_kwh, household_target_kwh))
        arb_kwh    = max(0.0, min(spare, sellable))

    buy_kwh = household_buy_kwh + arb_kwh
    if buy_kwh < MIN_TRADE_KWH:
        return None, 0, 0.0, margin_p, household_buy_kwh, infeasible

    # The charge cutoff is a ceiling on SOC, so it is the target the battery
    # should reach — never above the configured maximum charge SOC, arbitrage
    # included.
    target_kwh = min(ceiling_kwh, have_kwh + buy_kwh)
    # The epsilon is not cosmetic: the requirement comes from a binary search
    # that converges to within a tolerance, so an exact 80% lands at 79.9999
    # about half the time, and flooring that gives away a whole percent.
    target_pct = min(site.max_charge_soc_pct,
                     float(math.floor(_pct(target_kwh, site.capacity_kwh) + 1e-3)))

    hours_left = max(1.0 / 60.0, (window_end - inputs.now).total_seconds() / 3600.0)
    wanted_w   = int((buy_kwh / eff / hours_left) * 1000.0)
    available  = min(site.charge_power_w, import_headroom_w(inputs))
    power_w    = max(0, min(wanted_w, available))
    if power_w <= 0:
        raise FluxDeferred("the site has no spare import capacity for a grid charge "
                           "right now")
    # What the window can no longer physically deliver. A charge sized correctly
    # and started too late is still short, and the shortfall is a fact the log
    # should carry rather than something the target quietly rounds away.
    deliverable = available / 1000.0 * hours_left * eff
    infeasible += max(0.0, buy_kwh - deliverable)
    return target_pct, power_w, buy_kwh, margin_p, household_buy_kwh, infeasible


def _export_plan(inputs):
    """(floor_pct, power_w, surplus_kwh, committed_kwh).

    The floor is computed with charging suppressed until the end of the peak
    window, because that is exactly what exporting does: mode 5 pins the charge
    limit at zero, so any PV surplus during the window reaches the grid and not
    the battery. Working the floor out as though that PV were being banked, and
    then preventing the banking, is how a plan comes to need energy it has just
    given away.
    """
    site   = inputs.site
    until  = next_cheap_start(inputs.now, inputs.local_tz)
    peak_end = next_local(inputs.now, inputs.local_tz, FLUX_PEAK_END)
    floor  = export_floor_pct(inputs, until, no_charge_until=peak_end)
    surplus_kwh = max(0.0, (float(inputs.soc_pct) - floor) / 100.0
                      * site.capacity_kwh) * site.one_way_efficiency
    # FULL battery power, and let the inverter's own grid export cap (40038) hold
    # the meter at the DNO limit. Sizing the limit to "cap less live PV" (5.109.x)
    # moved the target every tick as PV moved, and read ZERO whenever the roof
    # alone filled the cap — which the plan took as "nothing worth selling" and
    # handed back, flapping mode 5 / mode 2 through the first live peak
    # (17-Sep-2026, 16:43-16:46). The cap is hardware-enforced in Remote EMS
    # discharge: the 11-Sep Axle event drove 4.7 kW from the battery and the grid
    # export peaked at 4.05 kW. The energy is still bounded by the sell floor.
    # The headroom call stays for its freshness check on the live readings.
    export_headroom_w(inputs)
    power_w = max(0, int(site.discharge_power_w)) if site.export_limit_w > 0 else 0
    committed = _commitment_kwh(inputs.commitments, inputs.now, until, "export")
    return floor, power_w, surplus_kwh, committed


# ================================================================
# plan()
# ================================================================

def plan(inputs):
    """Inputs in, one decision out. Pure. `owns` False means release."""
    try:
        floor_guess = _reserve_floor_pct(inputs.site)
    except (TypeError, ValueError, OverflowError, AttributeError):
        # The floor is reported on a defer as well, and a NaN reserve must not
        # turn a refusal into a traceback.
        floor_guess = DEFAULT_RESERVE_PCT
    try:
        _validate(inputs)
    except FluxDeferred as exc:
        return FluxDecision(mode=MODE_DEFER, owns=False, decision_at=inputs.now,
                            reason=f"no Flux decision — {exc}",
                            protect_soc_pct=floor_guess,
                            household_floor_pct=floor_guess)

    site  = inputs.site
    tz    = inputs.local_tz
    bands = inputs.bands
    floor = _reserve_floor_pct(site)

    try:
        next_cheap = next_cheap_start(inputs.now, tz)
        house_floor = household_floor_pct(inputs, next_cheap)
        committed   = _commitment_kwh(inputs.commitments, inputs.now, next_cheap, "export")

        # ── the cheap window ────────────────────────────────────────────────
        if in_window(inputs.now, tz, FLUX_CHEAP_START, FLUX_CHEAP_END):
            window_end = next_local(inputs.now, tz, FLUX_CHEAP_END)
            (target_pct, power_w, buy_kwh, margin_p,
             household_kwh, infeasible) = _charge_plan(inputs)
            if target_pct is None:
                return FluxDecision(
                    mode=MODE_HOLD, owns=True, decision_at=inputs.now,
                    ems_mode=EMS_SELF_CONSUMPTION,
                    charge_limit_w=int(site.charge_power_w), discharge_limit_w=0,
                    charge_cutoff_pct=float(site.max_charge_soc_pct),
                    discharge_cutoff_pct=house_floor,
                    decision_until=_decision_until(inputs, window_end),
                    protect_soc_pct=house_floor, household_floor_pct=house_floor,
                    margin_p=margin_p, committed_kwh=committed,
                    infeasible_kwh=infeasible,
                    reason=("the cheap window is open and the battery already holds "
                            "everything the day ahead is forecast to need"))
            arb_kwh = max(0.0, buy_kwh - household_kwh)
            reason  = (f"cheap window — buying about {buy_kwh:.1f} kWh at "
                       f"{bands.import_cheap_p:.1f}p to {target_pct:.0f}%, of which "
                       f"{household_kwh:.1f} kWh is what the house and its "
                       f"commitments need")
            if arb_kwh >= MIN_TRADE_KWH:
                reason += (f", and {arb_kwh:.1f} kWh is for the peak window at a "
                           f"{margin_p:.1f}p margin")
            if infeasible > 0:
                reason += (f". About {infeasible:.1f} kWh of what is needed will not "
                           f"fit in the battery, so the house will import some of it "
                           f"at the day rate")
            return FluxDecision(
                mode=MODE_CHARGE, owns=True, decision_at=inputs.now,
                ems_mode=EMS_CHARGE_GRID,
                charge_limit_w=int(power_w), discharge_limit_w=0,
                charge_cutoff_pct=float(target_pct), discharge_cutoff_pct=house_floor,
                decision_until=_decision_until(inputs, window_end),
                protect_soc_pct=house_floor, household_floor_pct=house_floor,
                planned_kwh=round(buy_kwh, 2), margin_p=margin_p,
                committed_kwh=committed, infeasible_kwh=infeasible, reason=reason)

        # ── the peak window ─────────────────────────────────────────────────
        if in_window(inputs.now, tz, FLUX_PEAK_START, FLUX_PEAK_END):
            window_end = next_local(inputs.now, tz, FLUX_PEAK_END)
            # An unpublished hour is not zero sun here either. Reading it as zero
            # would hide the PV that decides whether to hand back rather than
            # hold a mode that cannot charge — the case where a missing forecast
            # silently throws the afternoon's generation at the grid.
            if not inputs.pv.covers(inputs.now, window_end):
                raise FluxDeferred("the solar forecast does not cover the rest of "
                                   "the peak window, so what is spare cannot be "
                                   "worked out")
            profitable, margin_p = _export_is_profitable(bands, site)
            sell_floor, power_w, surplus_kwh, committed = _export_plan(inputs)
            if (not profitable) or surplus_kwh < MIN_TRADE_KWH or power_w <= 0:
                # NOT exporting means mode 2 with the charge limit at zero, and
                # that would throw away any PV the roof is still making. If the
                # sun is working, hand back instead: the existing manager runs
                # self consumption, which charges from PV AND serves the house —
                # the one thing a single-direction target cannot express.
                pv_left = inputs.pv.kwh_between(inputs.now, window_end)
                house_left = inputs.house.kwh_between(inputs.now, window_end)
                if (pv_left - house_left) >= MIN_TRADE_KWH:
                    return FluxDecision(
                        mode=MODE_SOLAR, owns=False, decision_at=inputs.now,
                        protect_soc_pct=sell_floor, household_floor_pct=house_floor,
                        committed_kwh=committed, margin_p=margin_p,
                        reason=("peak window, but there is nothing worth selling and "
                                "the roof is still generating, so the existing "
                                "manager keeps the inverter and banks the solar"))
                why = ("selling does not cover what it cost to store, after losses "
                       "and wear" if not profitable else
                       "there is nothing spare above what the house and its "
                       "commitments need before the cheap rate comes back")
                return FluxDecision(
                    mode=MODE_SUPPLY_HOUSE, owns=True, decision_at=inputs.now,
                    ems_mode=EMS_SELF_CONSUMPTION,
                    charge_limit_w=0, discharge_limit_w=int(site.discharge_power_w),
                    charge_cutoff_pct=float(site.max_charge_soc_pct),
                    discharge_cutoff_pct=house_floor,
                    decision_until=_decision_until(inputs, window_end),
                    protect_soc_pct=sell_floor, household_floor_pct=house_floor,
                    margin_p=margin_p, committed_kwh=committed,
                    reason=f"peak window, but {why}, so the battery runs the house")
            reason = (f"peak window — about {surplus_kwh:.1f} kWh is spare above what "
                      f"the house needs before 2am, selling at "
                      f"{bands.export_peak_p:.1f}p for a {margin_p:.1f}p margin after "
                      f"losses and wear, holding {sell_floor:.0f}% back")
            if committed > 0:
                reason += (f" (including {committed:.1f} kWh already promised to a "
                           f"grid event, which is paid separately and is not part of "
                           f"this sale)")
            return FluxDecision(
                mode=MODE_EXPORT, owns=True, decision_at=inputs.now,
                ems_mode=EMS_DISCHARGE_PV,
                charge_limit_w=0, discharge_limit_w=int(power_w),
                charge_cutoff_pct=float(site.max_charge_soc_pct),
                discharge_cutoff_pct=sell_floor,
                decision_until=_decision_until(inputs, window_end),
                protect_soc_pct=sell_floor, household_floor_pct=house_floor,
                planned_kwh=round(surplus_kwh, 2), margin_p=margin_p,
                committed_kwh=committed, reason=reason)

        # ── the run-up to the peak ──────────────────────────────────────────
        peak_start = next_local(inputs.now, tz, FLUX_PEAK_START)
        minutes_to_peak = (peak_start - inputs.now).total_seconds() / 60.0
        if minutes_to_peak <= HOLD_BEFORE_PEAK_MINUTES:
            worth_holding, hold_margin_p = _hold_is_profitable(bands, site)
            if worth_holding and not inputs.pv.covers(inputs.now, peak_start):
                raise FluxDeferred("the solar forecast does not cover the run-up to "
                                   "the peak, so holding the battery cannot be "
                                   "judged")
            if worth_holding:
                hold_floor = max(house_floor, export_floor_pct(inputs, peak_start))
                if float(inputs.soc_pct) > hold_floor:
                    return FluxDecision(
                        mode=MODE_HOLD, owns=True, decision_at=inputs.now,
                        ems_mode=EMS_SELF_CONSUMPTION,
                        charge_limit_w=int(site.charge_power_w), discharge_limit_w=0,
                        charge_cutoff_pct=float(site.max_charge_soc_pct),
                        discharge_cutoff_pct=hold_floor,
                        decision_until=_decision_until(inputs, peak_start),
                        protect_soc_pct=hold_floor, household_floor_pct=house_floor,
                        margin_p=hold_margin_p, committed_kwh=committed,
                        reason=(f"holding the battery for the peak in "
                                f"{minutes_to_peak:.0f} minutes — it sells at "
                                f"{bands.export_peak_p:.1f}p there against the "
                                f"{bands.import_day_p:.1f}p the house pays now. Solar "
                                f"still charges"))

        # ── everything else ─────────────────────────────────────────────────
        return FluxDecision(
            mode=MODE_SOLAR, owns=False, decision_at=inputs.now,
            protect_soc_pct=house_floor, household_floor_pct=house_floor,
            committed_kwh=committed,
            reason=("outside the Flux windows — self consumption and solar overflow "
                    "are the existing manager's job, so Flux is not holding the "
                    "inverter"))

    except FluxDeferred as exc:
        return FluxDecision(mode=MODE_DEFER, owns=False, decision_at=inputs.now,
                            protect_soc_pct=floor, household_floor_pct=floor,
                            reason=f"no Flux decision — {exc}")


def commitment_signature(commitments):
    """Stable identity of a commitment set, for detecting a late announcement."""
    return tuple(sorted(c.key() for c in commitments))


def describe(decision):
    if decision.deferred:
        return decision.reason
    return f"{decision.mode.replace('_', ' ')}: {decision.reason}"


# A whole number masks to ONE "#", never one per digit: "19.0" and "9.9" are
# both a kWh figure that moved, and a per-digit mask made them different keys.
# An interior "." or "," is part of the number; a full stop ending a sentence is
# not, because it is not followed by a digit.
_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)*")


def note_control_key(decision):
    """The part of the control state a PLAN NOTE is allowed to be keyed on.

    control_key() is the register-level identity: it exists so the executor
    re-writes the inverter when any commanded value moves, and it therefore
    carries the raw watt figures. `charge_limit_w` is re-derived on every plan
    from the energy still to buy and the time left to buy it in, so on the
    night of 18/19-Sep-2026 it wandered between 316W and 337W and changed on
    essentially every tick. It appears NOWHERE in the message.

    So keying the note on control_key() reproduced the very bug v5.110.3 set
    out to fix, one layer down: 143 plan notes between 02:00 and 05:00, at a
    metronomic 27 seconds apart, carrying five distinct sentences between them.
    The running figure had simply moved out of the prose and into the fields.

    THE KEY MUST BE AS COARSE AS THE MESSAGE. Power is not in the note at all,
    so only its SIGN is kept -- charging or not, discharging or not. The
    cutoffs are in the note as whole percentages, so they are kept as whole
    percentages: when the text says 41% and then 42%, that is a visible change
    and deserves a line; 41.4% against 41.2% is not.
    """
    return (decision.mode, decision.ems_mode,
            decision.charge_limit_w > 0, decision.discharge_limit_w > 0,
            round(decision.charge_cutoff_pct), round(decision.discharge_cutoff_pct))


def note_key(decision):
    """Dedupe key for the one-line [Flux] plan note: the note-level control
    fields, plus the reason with its DIGITS masked.

    `reason` carries a running figure -- "buying about 7.9 kWh", "spare above
    what the house needs", "for the peak in 12 minutes" -- which moves on
    almost every tick. A key built from the prose therefore never matched
    itself and the "never the same line twice" guard never once fired: 310
    [Flux] lines reached the Indigo event log on 18-Sep-2026, one per tick,
    103 of them between 02:00 and 05:00.

    Masking only the digits is deliberately weaker than keying on the control
    state alone. Two plans can share every control field and still say
    different things -- MODE_SUPPLY_HOUSE explains itself either as "selling
    does not cover what it cost to store" or as "there is nothing spare above
    what the house needs", with identical registers -- so a key that ignored
    the wording would swallow a genuinely different explanation. A change of
    WORDING is always news; a change of only the digits is not.

    It uses note_control_key(), NOT control_key(): see there for why the
    register-level identity is the wrong granularity for a sentence.
    """
    masked = _NUMBER_RE.sub("#", decision.reason)
    return f"{note_control_key(decision)}|{masked}"
