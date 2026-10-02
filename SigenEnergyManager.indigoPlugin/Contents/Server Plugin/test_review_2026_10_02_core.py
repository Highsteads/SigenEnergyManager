#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    test_review_2026_10_02_core.py
# Description: Regression tests for the 02-10-2026 review of the Flux planner and the
#              battery manager: the Axle cover inside the peak, the peak sale flipping
#              against the roof, the cheap-window charge stopping short of its own
#              target, and the overnight flood drain on Flux (floor and reason text).
# Author:      CliveS & Claude Opus 5.5
# Date:        02-10-2026
# Version:     1.0

import unittest
from dataclasses import replace
from datetime import date, timedelta

import battery_manager as bm
import flux_strategy as fs
import test_flux_strategy as T

LONDON = T.LONDON


def _flood_snapshot(export_rate_p=12.0):
    """A Flux night at 22:00, 80% battery, a very sunny tomorrow: the flood gate fires."""
    d = date(2026, 6, 17)
    nxt = d + timedelta(days=1)
    now = fs._wall(LONDON, d, fs.time(22, 0))
    dawn = fs._wall(LONDON, nxt, fs.time(5, 0))
    p50 = {f"{d:%Y-%m-%d} {h:02d}:00:00": 6000 for h in range(5, 21)}
    p50.update({f"{nxt:%Y-%m-%d} {h:02d}:00:00": 6000 for h in range(5, 21)})
    return bm.ManagerSnapshot(
        current_soc_pct=80.0, export_enabled=True, dawn_target_pct=15.0,
        health_cutoff_pct=1.0, corrected_today_kwh=70.0, corrected_tomorrow_kwh=70.0,
        tariff=bm.TariffData(tariff_key=bm.TARIFF_FLUX, cheap_start="02:00",
                             cheap_end="05:00", peak_start="16:00", peak_end="19:00"),
        forecast_p50=p50, dawn_times={f"{nxt:%Y-%m-%d}": dawn},
        consumption_profile=[22.0 / 48] * 48, now=now,
        flux_owns_cheap_window=True, export_rate_p=export_rate_p)


class EventCoverInsideThePeak(unittest.TestCase):
    """Fix 1: the cover's peak is the one on the event's own day, and inside the peak
    it buys only what the event itself needs, not the evening at the peak price."""

    DAY = (2026, 10, 14)

    def _cover(self, hhmm):
        day = date(*self.DAY)
        ev = T._axle(self.DAY, 17, 18, kw=4.0)
        inp = T._inputs(hhmm, soc_pct=30.0, day=self.DAY, pv=T._pv(day, 3.0),
                        commitments=(ev,))
        return fs.event_cover(inp, avoid_peak=True)

    def test_before_the_peak_the_deadline_is_4pm(self):
        cover = self._cover((15, 59))
        self.assertIsNotNone(cover)
        self.assertEqual(cover.deadline.astimezone(LONDON).strftime("%H:%M"), "16:00")

    def test_inside_the_peak_it_does_not_buy_the_evening(self):
        before = self._cover((15, 55))
        self.assertIsNotNone(before)
        for hhmm in ((16, 0), (16, 5), (16, 30)):
            cover = self._cover(hhmm)
            if cover is None:
                continue
            # Before 4pm the whole evening is bought at the day rate (about 12 kWh);
            # inside the peak only the 4 kWh event and its hour are worth buying.
            self.assertLess(cover.buy_kwh, 6.0, f"{hhmm}: {cover.buy_kwh} kWh")
            self.assertLess(cover.buy_kwh, before.buy_kwh / 2.0)
            self.assertEqual(cover.deadline, cover.event_start)
            self.assertNotIn("until 2am", cover.reason)


class PeakSaleDoesNotFlip(unittest.TestCase):
    """Fix 2: a sunny peak with no Saving Session must not flip export / hand back
    every tick as the sale crosses MIN_TRADE_KWH and the roof refills it."""

    def test_sunny_peak_mode_changes_are_bounded(self):
        d = (2026, 6, 17)
        day = date(*d)
        pv = T._pv(day, 50.0, first_hour=5, last_hour=21)    # 3.125 kW, 05:00-21:00
        house_kw, pv_kw = 1.0, 3.125
        cap, eta = 35.04, 0.94 ** 0.5
        soc = 27.0
        t = fs._wall(LONDON, day, fs.time(16, 0))
        end = fs._wall(LONDON, day, fs.time(19, 0))
        base = T._inputs((16, 0), soc_pct=soc, day=d, pv=pv)
        prev, changes, modes = None, 0, []
        h = 30 / 3600
        while t < end:
            p = fs.plan(replace(base, now=t, soc_pct=soc))
            key = (p.mode, p.owns)
            if key != prev:
                changes += 1
                modes.append(f"{t.astimezone(LONDON):%H:%M:%S} {p.mode}")
                prev = key
            surplus_kw = pv_kw - house_kw
            if p.mode == fs.MODE_EXPORT:
                out = max(0.0, 4.0 - surplus_kw)
                if soc > p.discharge_cutoff_pct:
                    soc -= out * h / eta / cap * 100
            elif p.mode == fs.MODE_SOLAR:          # the manager banks the roof
                soc += surplus_kw * h * eta / cap * 100
            elif p.owns and p.charge_limit_w == 0:  # supply house: roof covers it
                soc -= max(0.0, house_kw - pv_kw) * h / eta / cap * 100
            t += timedelta(seconds=30)
        self.assertLessEqual(changes, 4, "\n".join(modes[:12]))

    def test_below_the_floor_the_roof_is_still_banked(self):
        # Nothing spare at all (SOC under the sell floor): the manager banks the
        # roof as before, so the evening is not left short (self-sufficiency KPI).
        d = (2026, 6, 17)
        pv = T._pv(date(*d), 50.0, first_hour=5, last_hour=21)
        p = fs.plan(T._inputs((16, 30), soc_pct=15.0, day=d, pv=pv))
        self.assertEqual(p.mode, fs.MODE_SOLAR)


class CheapWindowChargeReachesItsTarget(unittest.TestCase):
    """Fix 3: once charging, the cheap-window charge runs to its own target rather
    than stopping MIN_TRADE_KWH (about 1.5%) short of it."""

    def test_charges_up_to_the_cutoff(self):
        d = (2026, 10, 14)
        pv = T._pv(date(*d), 14.0)
        first = fs.plan(T._inputs((3, 0), soc_pct=30.0, day=d, pv=pv))
        self.assertEqual(first.mode, fs.MODE_CHARGE)
        target = first.charge_cutoff_pct
        soc = target - 1.5
        while soc < target - 0.1:
            p = fs.plan(T._inputs((3, 0), soc_pct=soc, day=d, pv=pv))
            self.assertEqual(p.mode, fs.MODE_CHARGE, f"stopped at {soc:.2f}%")
            self.assertEqual(p.charge_cutoff_pct, target)
            soc += 0.25
        done = fs.plan(T._inputs((3, 0), soc_pct=target, day=d, pv=pv))
        self.assertEqual(done.mode, fs.MODE_HOLD)


class FloodDrainOnFlux(unittest.TestCase):
    """Fixes 4 and 5: on Flux the overnight drain stops at the cheap-window minimum
    (no point selling at 9.7p what 02:00 buys back at 14.6p), and the reason prints
    the export rate actually used."""

    def test_flux_drain_floor_is_the_cheap_window_minimum(self):
        dec = bm.BatteryManager().evaluate(_flood_snapshot())
        self.assertEqual(dec.action, bm.ACTION_START_EXPORT)
        self.assertGreaterEqual(dec.target_soc_pct, fs.CHARGE_MIN_PCT)

    def test_off_flux_drain_floor_unchanged(self):
        snap = _flood_snapshot()
        snap = replace(snap, tariff=replace(snap.tariff, tariff_key=bm.TARIFF_TRACKER),
                       flux_owns_cheap_window=False)
        dec = bm.BatteryManager().evaluate(snap)
        self.assertEqual(dec.action, bm.ACTION_START_EXPORT)
        self.assertAlmostEqual(dec.target_soc_pct, bm.FLOOD_PREV_TARGET_PCT)

    def test_a_running_flux_drain_stops_at_the_minimum(self):
        snap = replace(_flood_snapshot(), current_soc_pct=45.0, export_active=True,
                       flood_prev_target_soc=40.0)
        dec = bm.BatteryManager().evaluate(snap)
        self.assertNotEqual(dec.action, bm.ACTION_START_EXPORT)

    def test_reason_prints_the_real_export_rate(self):
        snap = replace(_flood_snapshot(export_rate_p=9.71),
                       tariff=replace(_flood_snapshot().tariff,
                                      tariff_key=bm.TARIFF_TRACKER),
                       flux_owns_cheap_window=False)
        dec = bm.BatteryManager().evaluate(snap)
        self.assertEqual(dec.action, bm.ACTION_START_EXPORT)
        self.assertIn("@ 9.7p", dec.reason)
        self.assertNotIn("@ 12p", dec.reason)


if __name__ == "__main__":
    unittest.main()
