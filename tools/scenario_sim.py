#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    scenario_sim.py
# Description: Whole-day scenario simulator for the Flux house. Drives the plugin's
#              REAL decision code (flux_strategy.plan, flux_strategy.event_cover,
#              battery_manager.BatteryManager.evaluate) through a day in 15-minute
#              steps, applies simple battery/grid physics, and checks the result
#              against CliveS's standing rules of 27-Sep-2026:
#                * outside 02:00-05:00 the battery is never stopped from running
#                  the house;
#                * the day imports only in free hours, when the battery is at its
#                  reserve, or to cover an Axle event;
#                * an Axle event runs in full and the battery still reaches 02:00.
#              It models the plugin's orchestration (who owns the inverter when),
#              not its Modbus glue, so it proves what the decisions ARE, not that
#              the registers were written.
# Author:      CliveS & Claude Opus 5.5
# Date:        28-09-2026
# Version:     1.4 (28-09-2026: the peak sale is lined up with a joined session, as 5.127.0)

import argparse
import csv
import math
import os
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "SigenEnergyManager.indigoPlugin",
                                "Contents", "Server Plugin"))

import battery_manager as bm   # noqa: E402
import flux_strategy as fs     # noqa: E402

LONDON = ZoneInfo("Europe/London")
UTC    = timezone.utc

# ---- the house, as configured on 28-Sep-2026 -------------------------------
CAP_KWH     = 35.04
EFF         = 0.94                 # round trip
ETA         = math.sqrt(EFF)       # one way
INV_KW      = 10.0
EXPORT_KW   = 4.0                  # DNO cap
IMPORT_KW   = 16.0
RESERVE_PCT = 20.0
WEAR_P      = 2.0
STEP        = timedelta(minutes=15)

# Octopus Flux, region F, 27-Sep-2026
IMP = {"cheap": 14.62, "day": 24.35, "peak": 34.10}
EXP = {"cheap": 4.21,  "day": 9.71,  "peak": 27.69}
BANDS = ((0, 2, "day"), (2, 5, "cheap"), (5, 16, "day"), (16, 19, "peak"), (19, 24, "day"))

# A house of about 21 kWh a day, shaped like this one: quiet nights, a morning
# rise, a busy 5pm-9pm. kW by local hour.
HOUSE_KW = [0.35, 0.33, 0.32, 0.32, 0.33, 0.40, 0.70, 1.05, 1.00, 0.85, 0.80, 0.80,
            0.85, 0.80, 0.80, 0.90, 1.20, 1.45, 1.55, 1.50, 1.30, 1.05, 0.75, 0.50]


def _wall(day, hh, mm=0):
    return fs._wall(LONDON, day, time(hh, mm))


def band_of(when):
    h = when.astimezone(LONDON).hour
    for a, b, name in BANDS:
        if a <= h < b:
            return name
    return "day"


def _spans(day, prices, days=3):
    out = []
    for off in range(-1, days):
        d = day + timedelta(days=off)
        for h0, h1, key in BANDS:
            start = _wall(d, h0)
            end = (_wall(d + timedelta(days=1), 0) if h1 == 24 else _wall(d, h1))
            out.append(fs.RateSpan(start=start, end=end, p=prices[key]))
    return out


def pv_curve(total_kwh, first=7.0, last=19.0):
    """kW by local fractional hour: a sine bell from `first` to `last`."""
    width = last - first
    peak_kw = total_kwh * math.pi / (2.0 * width)
    def kw(hour):
        if hour <= first or hour >= last:
            return 0.0
        return peak_kw * math.sin(math.pi * (hour - first) / width)
    return kw


def house_kw(hour):
    return HOUSE_KW[int(hour) % 24]


def _hour(when):
    local = when.astimezone(LONDON)
    return local.hour + local.minute / 60.0


# ---- scenarios --------------------------------------------------------------

@dataclass
class Window:
    start: datetime
    end: datetime
    announced: datetime


@dataclass
class Scenario:
    name: str
    day: date                       # the local day the sim starts on, at 02:00
    pv_kwh: float                   # what the sun actually brings
    forecast_kwh: float = None      # what the forecast said (default: the truth)
    start_soc: float = 30.0         # at 02:00
    axle: list = field(default_factory=list)       # [Window]
    saving: list = field(default_factory=list)     # [Window] joined Power Downs
    free: list = field(default_factory=list)       # [Window] Happy Hours
    note: str = ""
    track: bool = True              # model the plugin's intraday PV tracking
    sun_up: float = 7.0             # local hours the PV bell spans
    sun_down: float = 19.0
    house: tuple = None             # kW by local hour (default HOUSE_KW)


def W(day, h0, h1, announced=None):
    """A window on `day`, local hours; announced at a local datetime (default: the day before, 20:00)."""
    ann = announced or _wall(day - timedelta(days=1), 20)
    return Window(_wall(day, h0), _wall(day, h1), ann)


# ---- the simulation ---------------------------------------------------------

@dataclass
class Step:
    t: datetime
    soc: float
    mode: str
    why: str
    pv: float
    house: float
    imp: float
    exp: float
    imp_cause: str
    exp_cause: str


class Sim:
    def __init__(self, sc):
        self.sc = sc
        self.day = sc.day
        self.start = _wall(sc.day, 2)
        self.end = _wall(sc.day + timedelta(days=1), 2)
        self.house_kw = list(sc.house or HOUSE_KW)
        self.pv_true = pv_curve(sc.pv_kwh, sc.sun_up, sc.sun_down)
        fc = sc.forecast_kwh if sc.forecast_kwh is not None else sc.pv_kwh
        self.pv_fc = pv_curve(fc, sc.sun_up, sc.sun_down)
        self.fc_kwh = fc
        self.kwh = sc.start_soc / 100.0 * CAP_KWH
        self.site = fs.FluxSite(capacity_kwh=CAP_KWH, charge_power_w=int(INV_KW * 1000),
                                discharge_power_w=int(INV_KW * 1000),
                                export_limit_w=int(EXPORT_KW * 1000),
                                import_limit_w=int(IMPORT_KW * 1000),
                                import_limit_verified=True, efficiency=EFF,
                                wear_p_per_kwh=WEAR_P, reserve_pct=RESERVE_PCT)
        slots = []
        for i in range(48):
            slots.append(self.house_kw[i // 2] * 0.5)
        self.slots = slots
        self.house_profile = fs.HalfHourProfile(slots, LONDON)
        self.manager = bm.BatteryManager()
        self.import_active = False
        self.import_target = 0.0
        self.import_by_cover = False
        self.day_rate_bought = False
        self.steps = []
        # The plugin's intraday PV tracking (measured vs forecast so far today),
        # which the manager plans with and, from 5.125.3, the Flux planner too.
        self.track_day = None
        self.track_actual = 0.0
        self.track_fc = 0.0
        self.track = 1.0
        self.cover_track = 1.0           # 5.125.4: the same, with no 0.6 floor

    # -- forecasts in the two shapes the code reads
    def _pv_buckets(self):
        out = {}
        for off in range(-1, 3):
            d = self.day + timedelta(days=off)
            for h in range(24):
                wh = sum(self.pv_fc(h + m / 60.0) for m in range(0, 60, 5)) / 12.0 * 1000.0
                out[f"{d:%Y-%m-%d} {h:02d}:00:00"] = wh
        return out

    def _commitments(self, now):
        out = []
        for i, w in enumerate(self.sc.axle):
            if w.announced <= now and w.end > now:
                hours = (w.end - w.start).total_seconds() / 3600.0
                out.append(fs.EventCommitment(source="axle", kind="export", start=w.start,
                                              end=w.end, energy_kwh=EXPORT_KW * hours,
                                              event_id=f"axle-{i}"))
        # Saving Sessions reserve nothing on Flux (5.126.0).
        for i, w in enumerate(self.sc.free):
            if w.announced <= now and w.end > now:
                hours = (w.end - w.start).total_seconds() / 3600.0
                out.append(fs.EventCommitment(source="octopus", kind="import", start=w.start,
                                              end=w.end, energy_kwh=INV_KW * hours,
                                              event_id=f"hh-{i}"))
        return tuple(out)

    @staticmethod
    def _live(windows, now, lead=timedelta(0)):
        for w in windows:
            if w.announced <= now and w.start - lead <= now < w.end:
                return w
        return None

    def _flux_inputs(self, now, pv_w, house_w, track=None):
        track = self.track if track is None else track
        return fs.FluxInputs(
            now=now, local_tz=LONDON,
            bands=fs.derive_bands(_spans(self.day, IMP), _spans(self.day, EXP), LONDON, now),
            site=self.site, soc_pct=self.kwh / CAP_KWH * 100.0,
            house=self.house_profile,
            pv=fs.HourlyPvForecast(self._pv_buckets(), LONDON,
                                   bias_by_date={now.astimezone(LONDON).date(): track}),
            flows=fs.FluxFlows(pv_w=pv_w, house_w=house_w, grid_w=0.0),
            commitments=self._commitments(now), tariff_verified=True, commissioned=True,
            enabled=True, rates_age_s=600.0, forecast_age_s=600.0, telemetry_age_s=2.0,
            flows_age_s=2.0, profile_age_s=3600.0,
            day_rate_import_today=self.day_rate_bought,
            sale_priority=tuple((w.start, w.end) for w in self.sc.saving
                                if w.announced <= now and w.end > now))

    def _snapshot(self, now, pv_w, house_w, cover, vpp_live, ss_live, hh_live):
        tariff = bm.TariffData(tariff_key="flux", today_rate_p=IMP["day"],
                               cheap_start="02:00", cheap_end="05:00",
                               cheap_rate_p=IMP["cheap"], day_rate_p=IMP["day"],
                               peak_start="16:00", peak_end="19:00", peak_rate_p=IMP["peak"])
        dawn = {}
        for off in range(-1, 3):
            d = self.day + timedelta(days=off)
            dawn[f"{d:%Y-%m-%d}"] = _wall(d, int(self.sc.sun_up))
        daily = sum(self.house_kw)
        today_local = now.astimezone(LONDON).date()
        vpp_today = sum(EXPORT_KW * (w.end - w.start).total_seconds() / 3600.0
                        for w in self.sc.axle
                        if w.announced <= now and w.end > now
                        and w.start.astimezone(LONDON).date() == today_local)
        return bm.ManagerSnapshot(
            current_soc_pct=self.kwh / CAP_KWH * 100.0, capacity_kwh=CAP_KWH,
            efficiency=EFF, dawn_target_pct=RESERVE_PCT, health_cutoff_pct=1.0,
            export_enabled=True, max_export_kw=EXPORT_KW, inverter_max_kw=INV_KW,
            export_rate_p=EXP[band_of(now)],
            weekday_kwh=daily, monday_kwh=daily, saturday_kwh=daily, sunday_kwh=daily,
            pv_watts=int(pv_w), house_load_watts=int(house_w),
            corrected_today_kwh=self.fc_kwh, corrected_tomorrow_kwh=self.fc_kwh,
            tariff=tariff, forecast_p50=self._pv_buckets(), dawn_times=dawn,
            consumption_profile=list(self.slots), now=now,
            vpp_active=vpp_live is not None, vpp_today_kwh=vpp_today,
            saving_session_active=False,    # 5.126.0: never driven on Flux
            saving_session_hours=((ss_live.end - ss_live.start).total_seconds() / 3600.0
                                  if ss_live else 1.0),
            happy_hour_active=hh_live is not None,
            happy_hour_hours=((hh_live.end - hh_live.start).total_seconds() / 3600.0
                              if hh_live else 1.0),
            reserve_floor_pct=RESERVE_PCT, wear_p_per_kwh=WEAR_P,
            pv_tracking_factor=self.track,
            import_pending=self.import_active, flux_owns_cheap_window=True,
            event_cover_active=bool(cover is not None and cover.active),
            event_cover_target_pct=float(cover.target_pct) if cover else 0.0,
            event_cover_reason=cover.reason if cover else "",
        )

    # -- one step of physics for a chosen mode
    def _physics(self, mode, dt_h, pv, house, floor_pct, target_pct=100.0,
                 power_kw=INV_KW, charge_cap_kw=INV_KW, export_kw=EXPORT_KW):
        """Returns (import_kWh, export_kWh). pv/house are kWh this step."""
        floor = floor_pct / 100.0 * CAP_KWH
        room = lambda: max(0.0, CAP_KWH - self.kwh)
        imp = exp = 0.0
        direct = min(pv, house)
        surplus = pv - direct
        deficit = house - direct
        max_io = INV_KW * dt_h

        if mode in ("grid_charge", "hh"):
            target = target_pct / 100.0 * CAP_KWH
            want = max(0.0, target - self.kwh)                 # battery-side
            # PV first (5.125.0), then the grid, within the charge rate
            from_pv = min(surplus, want / ETA, power_kw * dt_h)
            self.kwh += from_pv * ETA
            surplus -= from_pv
            want = max(0.0, target - self.kwh)
            from_grid = min(want / ETA, power_kw * dt_h - from_pv,
                            IMPORT_KW * dt_h - deficit)
            from_grid = max(0.0, from_grid)
            self.kwh += from_grid * ETA
            imp = from_grid + deficit                          # battery never serves the house
            exp = surplus
            return imp, exp

        if mode == "hold":
            take = min(surplus, charge_cap_kw * dt_h, room() / ETA)
            self.kwh += take * ETA
            return deficit, surplus - take

        if mode in ("export", "vpp", "saving"):
            # Battery discharges to fill the export cap after the house.
            grid_room = export_kw * dt_h
            pv_out = min(surplus, grid_room)
            grid_room -= pv_out
            need = deficit + grid_room                         # grid-side
            give = min(need / ETA, max(0.0, self.kwh - floor), power_kw * dt_h)
            self.kwh -= give
            delivered = give * ETA
            to_house = min(deficit, delivered)
            to_grid = delivered - to_house
            return deficit - to_house, pv_out + to_grid + (surplus - pv_out) * 0.0

        if mode == "supply":
            give = min(deficit / ETA, max(0.0, self.kwh - floor), max_io)
            self.kwh -= give
            return deficit - give * ETA, surplus

        # self consumption (optionally with a charge cap)
        take = min(surplus, charge_cap_kw * dt_h, room() / ETA)
        self.kwh += take * ETA
        exp = surplus - take
        give = min(deficit / ETA, max(0.0, self.kwh - floor), max_io)
        self.kwh -= give
        imp = deficit - give * ETA
        return imp, exp

    def run(self):
        now = self.start
        dt_h = STEP.total_seconds() / 3600.0
        while now < self.end:
            hour = _hour(now)
            pv = self.pv_true(hour + 0.125) * dt_h
            house = self.house_kw[int(hour) % 24] * dt_h
            local_day = now.astimezone(LONDON).date()
            if local_day != self.track_day:
                self.track_day, self.track_actual, self.track_fc, self.track = \
                    local_day, 0.0, 0.0, 1.0
                self.cover_track = 1.0
            pv_w, house_w = pv / dt_h * 1000.0, house / dt_h * 1000.0

            inputs = self._flux_inputs(now, pv_w, house_w)
            in_cheap = fs.in_window(now, LONDON, fs.FLUX_CHEAP_START, fs.FLUX_CHEAP_END)
            cover = None if in_cheap else fs.event_cover(
                self._flux_inputs(now, pv_w, house_w, track=self.cover_track))
            vpp_live = self._live(self.sc.axle, now)
            vpp_pre = self._live(self.sc.axle, now, lead=timedelta(minutes=30))
            ss_live = self._live(self.sc.saving, now)
            hh_live = self._live(self.sc.free, now)
            snap = self._snapshot(now, pv_w, house_w, cover, vpp_live, ss_live, hh_live)
            dec = self.manager.evaluate(snap)

            # The plugin's ownership rules (_flux_other_owner): these outrank Flux.
            other = (vpp_pre is not None or hh_live is not None
                     or (cover is not None and cover.active) or self.import_active)
            mode, why, floor, target, power, cap = "sc", "", RESERVE_PCT, 100.0, INV_KW, INV_KW
            imp_cause = None
            if dec.action == bm.ACTION_START_IMPORT and not in_cheap:
                self.import_active = True
                self.import_target = dec.target_soc_pct
                self.import_by_cover = "Axle" in dec.reason
            if self.import_active and self.kwh / CAP_KWH * 100.0 >= self.import_target - 0.05:
                self.import_active = False
            if dec.action == bm.ACTION_VPP_EXPORT:
                mode, why = "vpp", "Axle event"
            elif dec.action == bm.ACTION_HAPPY_HOUR_IMPORT:
                mode, why, target = "hh", "free hour", dec.target_soc_pct
            elif dec.action == bm.ACTION_SAVING_SESSION:
                mode, why = "saving", "Saving Session"
            elif self.import_active:
                mode, why, target = "grid_charge", ("Axle cover" if self.import_by_cover
                                                     else "manager import"), self.import_target
                if band_of(now) != "cheap":
                    self.day_rate_bought = True
            elif not other:
                fd = fs.plan(inputs)
                if fd.owns:
                    why = f"Flux {fd.mode}"
                    if fd.mode == fs.MODE_CHARGE:
                        mode, target, power = "grid_charge", fd.charge_cutoff_pct, fd.charge_limit_w / 1000.0
                        floor = fd.discharge_cutoff_pct
                    elif fd.mode == fs.MODE_HOLD:
                        mode = "hold"
                    elif fd.mode == fs.MODE_EXPORT:
                        mode, floor, power = "export", fd.discharge_cutoff_pct, fd.discharge_limit_w / 1000.0
                    elif fd.mode == fs.MODE_SUPPLY_HOUSE:
                        mode, floor = "supply", fd.discharge_cutoff_pct
                else:
                    why = "self consumption"
                    if dec.action == bm.ACTION_SOLAR_OVERFLOW and dec.power_watts:
                        cap = dec.power_watts / 1000.0
                        why = "solar overflow cap"
            else:
                why = "self consumption (Flux standing aside)"

            before = self.kwh
            imp, exp = self._physics(mode, dt_h, pv, house, floor, target, power, cap)
            soc = self.kwh / CAP_KWH * 100.0

            # Why did the house import? The rules allow exactly these.
            imp_cause = ""
            if imp > 1e-6:
                if in_cheap:
                    imp_cause = "cheap window"
                elif mode == "hh":
                    imp_cause = "free hour"
                elif mode == "grid_charge" and self.import_by_cover:
                    imp_cause = "Axle cover"
                elif (before <= floor / 100.0 * CAP_KWH + 0.05
                      or self.kwh <= floor / 100.0 * CAP_KWH + 0.05):
                    imp_cause = "battery at reserve" if floor <= RESERVE_PCT + 0.1 \
                        else f"battery at a {floor:.0f}% floor"
                elif mode in ("export", "vpp", "saving"):
                    imp_cause = "export power limit"
                else:
                    imp_cause = f"UNEXPECTED ({mode}: {why})"
            exp_cause = ""
            if exp > 1e-6:
                exp_cause = {"vpp": "Axle", "saving": "Saving Session",
                             "export": "peak sale"}.get(mode, "spare solar")
            self.steps.append(Step(now, soc, mode, why, pv, house, imp, exp,
                                   imp_cause, exp_cause))
            if self.kwh < CAP_KWH - 0.05:            # unclipped minutes only
                self.track_actual += pv
                self.track_fc += self.pv_fc(hour + 0.125) * dt_h
                if self.sc.track:
                    self.track, ratio = bm.pv_tracking_factor(self.track_actual,
                                                              self.track_fc)
                    self.cover_track = (1.0 if ratio is None else bm.pv_tracking_factor(
                        self.track_actual, self.track_fc, min_factor=0.0)[0])
            now += STEP
        return self

    # -- the verdict
    def summary(self):
        s = self.steps
        imp_by = {}
        exp_by = {}
        cost_p = 0.0
        earn_p = 0.0
        for st in s:
            b = band_of(st.t)
            if st.imp > 1e-6:
                imp_by[st.imp_cause] = imp_by.get(st.imp_cause, 0.0) + st.imp
                if st.imp_cause != "free hour":
                    cost_p += st.imp * IMP[b]
            if st.exp > 1e-6:
                exp_by[st.exp_cause] = exp_by.get(st.exp_cause, 0.0) + st.exp
                earn_p += st.exp * EXP[b]
        axle = []
        for w in self.sc.axle:
            got = sum(st.exp for st in s if w.start <= st.t < w.end and st.mode == "vpp")
            want = EXPORT_KW * (w.end - w.start).total_seconds() / 3600.0
            after = [st for st in s if st.t >= w.end]
            reserve_after = sum(st.imp for st in after if st.imp_cause.startswith("battery at"))
            axle.append((w, got, want, reserve_after))
        saving = []
        for w in self.sc.saving:
            # Whatever leaves the house during the session counts towards it.
            got = sum(st.exp for st in s if w.start <= st.t < w.end)
            saving.append((w, got))
        unexpected = [st for st in s if st.imp_cause.startswith("UNEXPECTED")]
        stopped = [st for st in s
                   if st.mode in ("hold", "grid_charge", "hh")
                   and not fs.in_window(st.t, LONDON, fs.FLUX_CHEAP_START, fs.FLUX_CHEAP_END)
                   and st.mode != "hh" and not (st.mode == "grid_charge"
                                               and st.imp_cause == "Axle cover")]
        after_charge = next((st.soc for st in s if st.t.astimezone(LONDON).hour == 5), None)
        from_5am = [st.soc for st in s if st.t >= _wall(self.day, 5)]
        throughput = sum(max(0.0, a.soc - b.soc) for a, b in zip(s, s[1:])) / 100.0 * CAP_KWH
        return dict(imp_by=imp_by, exp_by=exp_by, cost_p=cost_p, earn_p=earn_p,
                    soc_5am=after_charge, soc_max=max(st.soc for st in s),
                    soc_min_day=min(from_5am), used_kwh=throughput,
                    axle=axle, saving=saving, unexpected=unexpected, stopped=stopped,
                    soc_min=min(st.soc for st in s), soc_end=s[-1].soc,
                    soc_1600=next((st.soc for st in s
                                   if st.t.astimezone(LONDON).hour == 16), None))


def scenarios():
    mon = date(2026, 9, 28)      # a Monday
    sun = date(2026, 10, 4)      # a Sunday (Happy Hours run at weekends)
    hi, lo = 34.0, 6.0
    return [
        Scenario("1 high sun, nothing booked", mon, hi, start_soc=35),
        Scenario("2 low sun, nothing booked", mon, lo, start_soc=35),
        Scenario("3 high sun + Axle 6-7pm", mon, hi, start_soc=35, axle=[W(mon, 18, 19)]),
        Scenario("4 low sun + Axle 6-7pm (known before 2am)", mon, lo, start_soc=35,
                 axle=[W(mon, 18, 19)]),
        Scenario("5 low sun + Axle 6-7pm announced 11am", mon, lo, start_soc=35,
                 axle=[W(mon, 18, 19, announced=_wall(mon, 11))]),
        Scenario("6 low sun + Axle 1-2pm announced 9am", mon, lo, start_soc=35,
                 axle=[W(mon, 13, 14, announced=_wall(mon, 9))]),
        Scenario("7 sunny forecast, dull day + Axle 6-7pm", mon, lo, forecast_kwh=hi,
                 start_soc=35, axle=[W(mon, 18, 19)]),
        Scenario("7c forecast 20, day 12 + Axle 6-7pm", mon, 12.0, forecast_kwh=20.0,
                 start_soc=35, axle=[W(mon, 18, 19)]),
        Scenario("7d dull forecast, sunny day + Axle 6-7pm", mon, hi, forecast_kwh=lo,
                 start_soc=35, axle=[W(mon, 18, 19)]),
        Scenario("8 high sun + Saving Session 5-6pm", mon, hi, start_soc=35,
                 saving=[W(mon, 17, 18)]),
        Scenario("9 low sun + Saving Session 5-6pm", mon, lo, start_soc=35,
                 saving=[W(mon, 17, 18)]),
        Scenario("10 low sun + Axle 6-7pm + Saving Session 6-7pm", mon, lo, start_soc=35,
                 axle=[W(mon, 18, 19)], saving=[W(mon, 18, 19)]),
        Scenario("10b low sun + Saving Session 8-9pm (outside the peak)", mon, lo,
                 start_soc=35, saving=[W(mon, 20, 21)]),
        Scenario("11 Sunday high sun + free hours 1-3pm", sun, hi, start_soc=35,
                 free=[W(sun, 13, 15)]),
        Scenario("12 Sunday low sun + free hours 1-3pm", sun, lo, start_soc=35,
                 free=[W(sun, 13, 15)]),
        Scenario("13 Sunday low sun + free hours 11am-1pm + Axle 6-7pm", sun, lo,
                 start_soc=35, free=[W(sun, 11, 13)], axle=[W(sun, 18, 19)]),
        Scenario("14 low start (15%), low sun + Axle 7-8am (known the night before)", mon,
                 lo, start_soc=15, axle=[W(mon, 7, 8)]),
        Scenario("15 high sun, battery 90% at 2am + Axle 6-7pm", mon, hi, start_soc=90,
                 axle=[W(mon, 18, 19)]),
    ]


# A December weekday here: more lighting and cooking in the evening, the house
# about 24 kWh a day (the September measure is 21). kW by local hour.
WINTER_HOUSE = (0.40, 0.38, 0.37, 0.37, 0.38, 0.45, 0.80, 1.20, 1.15, 0.95, 0.85, 0.85,
                0.90, 0.85, 0.85, 1.05, 1.45, 1.70, 1.75, 1.65, 1.45, 1.15, 0.85, 0.55)
COLD_HOUSE = tuple(round(k * 1.25, 3) for k in WINTER_HOUSE)     # about 30 kWh


def winter_scenarios():
    mon = date(2026, 12, 14)     # a Monday, GMT
    sun = date(2026, 12, 13)     # a Sunday
    def ws(name, day, pv, **kw):
        kw.setdefault("start_soc", 22.0)
        kw.setdefault("house", WINTER_HOUSE)
        return Scenario(name, day, pv, sun_up=8.25, sun_down=15.75, **kw)
    return [
        ws("W1 winter, dull (2 kWh sun), nothing booked", mon, 2.0),
        ws("W2 winter, bright (6 kWh sun), nothing booked", mon, 6.0),
        ws("W3 winter dull + Axle 5-6pm (known before 2am)", mon, 2.0,
           axle=[W(mon, 17, 18)]),
        ws("W4 winter dull + Axle 5-6pm announced noon", mon, 2.0,
           axle=[W(mon, 17, 18, announced=_wall(mon, 12))]),
        ws("W5 winter dull + Axle 4-6pm (two hours)", mon, 2.0,
           axle=[W(mon, 16, 18)]),
        ws("W6 winter dull + Saving Session 5:30-6:30pm", mon, 2.0,
           saving=[Window(_wall(mon, 17, 30), _wall(mon, 18, 30), _wall(mon - timedelta(days=1), 20))]),
        ws("W7 winter Sunday dull + free hours 1-3pm", sun, 2.0, free=[W(sun, 13, 15)]),
        ws("W8 winter Sunday dull + free hours 1-3pm + Axle 5-6pm", sun, 2.0,
           free=[W(sun, 13, 15)], axle=[W(sun, 17, 18)]),
        ws("W9 winter forecast 6, day 2 + Axle 5-6pm announced noon", mon, 2.0,
           forecast_kwh=6.0, axle=[W(mon, 17, 18, announced=_wall(mon, 12))]),
        ws("W10 cold winter day (30 kWh house), dull + Axle 5-6pm", mon, 2.0,
           house=COLD_HOUSE, axle=[W(mon, 17, 18)]),
        ws("W11 winter dull, battery 60% at 2am", mon, 2.0, start_soc=60.0),
    ]


def fmt_kwh(v):
    return f"{v:.1f}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv-dir", default="", help="write a per-step trace per scenario here")
    ap.add_argument("--only", default="", help="run scenarios whose name contains this")
    ap.add_argument("--season", choices=("autumn", "winter", "all"), default="autumn",
                    help="which scenario set to run (default autumn, the original 17)")
    args = ap.parse_args()
    failures = 0
    rows = []
    chosen = {"autumn": scenarios(), "winter": winter_scenarios(),
              "all": scenarios() + winter_scenarios()}[args.season]
    for sc in chosen:
        if args.only and args.only not in sc.name:
            continue
        sim = Sim(sc).run()
        r = sim.summary()
        bad = []
        if r["unexpected"]:
            first = r["unexpected"][0]
            bad.append(f"unexpected import {sum(x.imp for x in r['unexpected']):.1f} kWh, "
                       f"first at {first.t.astimezone(LONDON):%H:%M} ({first.imp_cause})")
        if r["stopped"]:
            first = r["stopped"][0]
            bad.append(f"battery stopped in the day {len(r['stopped']) * 15} min, first at "
                       f"{first.t.astimezone(LONDON):%H:%M} ({first.why})")
        for w, got, want, res_after in r["axle"]:
            if got < want - 0.05:
                bad.append(f"Axle {w.start.astimezone(LONDON):%H:%M} short: "
                           f"{got:.1f} of {want:.1f} kWh")
            if res_after > 0.05:
                bad.append(f"after Axle {w.start.astimezone(LONDON):%H:%M} the battery ran "
                           f"out: {res_after:.1f} kWh bought before 2am")
        failures += bool(bad)
        rows.append((sc, r, bad))
        if args.csv_dir:
            os.makedirs(args.csv_dir, exist_ok=True)
            path = os.path.join(args.csv_dir, sc.name.split(" ")[0] + ".csv")
            with open(path, "w", newline="", encoding="utf-8") as fh:
                w = csv.writer(fh)
                w.writerow(["time", "soc", "mode", "why", "pv", "house", "import",
                            "export", "import_cause", "export_cause"])
                for st in sim.steps:
                    w.writerow([st.t.astimezone(LONDON).strftime("%H:%M"),
                                f"{st.soc:.1f}", st.mode, st.why, f"{st.pv:.3f}",
                                f"{st.house:.3f}", f"{st.imp:.3f}", f"{st.exp:.3f}",
                                st.imp_cause, st.exp_cause])

    for sc, r, bad in rows:
        print(f"\n== {sc.name}  (sun {sc.pv_kwh:.0f} kWh"
              + (f", forecast {sc.forecast_kwh:.0f}" if sc.forecast_kwh is not None else "")
              + f", 2am battery {sc.start_soc:.0f}%)")
        print("   bought: " + (", ".join(f"{k} {fmt_kwh(v)}" for k, v in
                                          sorted(r["imp_by"].items())) or "nothing"))
        print("   sold:   " + (", ".join(f"{k} {fmt_kwh(v)}" for k, v in
                                          sorted(r["exp_by"].items())) or "nothing"))
        for w, got, want, _res in r["axle"]:
            print(f"   Axle {w.start.astimezone(LONDON):%H:%M}: {got:.1f} of {want:.1f} kWh")
        for w, got in r["saving"]:
            print(f"   Saving Session {w.start.astimezone(LONDON):%H:%M}: {got:.1f} kWh sold")
        print(f"   battery: after the 2am charge {r['soc_5am'] or 0:.0f}%, highest "
              f"{r['soc_max']:.0f}%, lowest after 5am {r['soc_min_day']:.0f}%, "
              f"discharged {r['used_kwh']:.1f} kWh ({r['used_kwh'] / CAP_KWH * 100:.0f}% of "
              f"the pack)")
        print(f"   battery: lowest {r['soc_min']:.0f}%, 4pm {r['soc_1600'] or 0:.0f}%, "
              f"2am {r['soc_end']:.0f}%   money: pay {r['cost_p'] / 100:.2f}, "
              f"earn {r['earn_p'] / 100:.2f} (Axle and points not included)")
        print("   RULES: " + ("all kept" if not bad else "; ".join(bad)))
    print(f"\n{len(rows)} scenarios, {failures} broke a rule")
    return 10 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
