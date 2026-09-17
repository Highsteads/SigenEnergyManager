#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    test_flux_strategy.py
# Description: Contract tests for the Flux planner, including every scenario
#              raised in the Codex review: event commitments, chronological
#              budgets, the two floors, strict validation, DST and headroom.
# Author:      CliveS & Claude Opus 5 (1M context)
# Date:        16-09-2026
# Version:     2.0

import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import flux_strategy as fs

LONDON = ZoneInfo("Europe/London")

# A published Flux day in the API's real shape: a handful of spans, not 48 rows.
IMPORT_P = {"cheap": 16.8, "day": 31.4, "peak": 44.9}
EXPORT_P = {"cheap": 4.7,  "day": 10.2, "peak": 29.6}
BANDS_SPEC = ((0, 2, "day"), (2, 5, "cheap"), (5, 16, "day"),
              (16, 19, "peak"), (19, 24, "day"))


def _spans(day, prices, days=3):
    """Contiguous spans from `day`, covering `days` local days.

    Boundaries are built from LOCAL WALL TIMES, not by adding hours to local
    midnight — Flux bands are fixed in local time, so on the spring-forward day
    the 02:00-05:00 cheap band is 01:00-04:00 UTC and the band before it is one
    real hour long. Adding UTC hours to midnight produces spans an hour out on
    both changeover days, which is the fixture making the same mistake as the
    code it is meant to catch.
    """
    out = []
    for offset in range(days):
        d = day + timedelta(days=offset)
        for h0, h1, key in BANDS_SPEC:
            start = fs._wall(LONDON, d, fs.time(h0, 0))
            end   = (fs._wall(LONDON, d + timedelta(days=1), fs.time(0, 0))
                     if h1 == 24 else fs._wall(LONDON, d, fs.time(h1, 0)))
            out.append(fs.RateSpan(start=start, end=end, p=prices[key]))
    return out


def _bands(day=None, now=None, imp=None, exp=None):
    day = day or datetime(2026, 9, 16, tzinfo=LONDON).date()
    now = now or fs._wall(LONDON, day, fs.time(0, 0))
    return fs.derive_bands(_spans(day, imp or IMPORT_P),
                           _spans(day, exp or EXPORT_P), LONDON, now, day)


def _profile(daily_kwh=24.0):
    return fs.HalfHourProfile([daily_kwh / 48.0] * 48, LONDON)


def _pv(day, total_kwh, first_hour=8, last_hour=17, days=3):
    buckets = {}
    hours = last_hour - first_hour
    for offset in range(days):
        d = day + timedelta(days=offset)
        for h in range(24):
            wh = (total_kwh * 1000.0 / hours) if first_hour <= h < last_hour else 0.0
            buckets[f"{d:%Y-%m-%d} {h:02d}:00:00"] = wh
    return fs.HourlyPvForecast(buckets, LONDON)


def _site(**over):
    base = dict(
        capacity_kwh=35.04, charge_power_w=10000, discharge_power_w=10000,
        export_limit_w=4000, import_limit_w=10000, import_limit_verified=True,
        efficiency=0.94, wear_p_per_kwh=5.0, reserve_pct=20.0,
    )
    base.update(over)
    return fs.FluxSite(**base)


def _flows(pv_w=0.0, house_w=500.0, grid_w=500.0):
    return fs.FluxFlows(pv_w=pv_w, house_w=house_w, grid_w=grid_w)


def _inputs(local_hhmm, soc_pct=50.0, day=(2026, 9, 16), **over):
    d   = datetime(*day, tzinfo=LONDON).date()
    now = fs._wall(LONDON, d, fs.time(local_hhmm[0], local_hhmm[1]))
    kw = dict(
        now=now, local_tz=LONDON, bands=_bands(d, now), site=_site(),
        soc_pct=soc_pct, house=_profile(), pv=_pv(d, 20.0), flows=_flows(),
        commitments=(),
        tariff_verified=True, commissioned=True, enabled=True,
        rates_age_s=600.0, forecast_age_s=600.0, telemetry_age_s=2.0,
        flows_age_s=2.0, profile_age_s=3600.0,
    )
    kw.update(over)
    return fs.FluxInputs(**kw)


def _axle(day, start_h, end_h, kw=4.0, announced_h=None):
    d = datetime(*day, tzinfo=LONDON).date() if isinstance(day, tuple) else day
    start = fs._wall(LONDON, d, fs.time(start_h, 0))
    end   = fs._wall(LONDON, d, fs.time(end_h, 0))
    hours = (end - start).total_seconds() / 3600.0
    return fs.EventCommitment(
        source="axle", kind="export", start=start, end=end,
        energy_kwh=kw * hours, event_id=f"axle-{start_h}",
        announced_at=(fs._wall(LONDON, d, fs.time(announced_h, 0))
                      if announced_h is not None else None))


# ================================================================
# 1. Event commitments
# ================================================================

class TestEventCommitments(unittest.TestCase):
    """An owner flag says 'not now'. A commitment says how many kWh, and when."""

    def test_an_18_to_19_axle_window_is_reserved_out_of_the_peak_export(self):
        """The classic overlap: the Axle window sits inside the Flux peak."""
        free = fs.plan(_inputs((16, 30), soc_pct=95.0))
        held = fs.plan(_inputs((16, 30), soc_pct=95.0,
                               commitments=(_axle((2026, 9, 16), 18, 19),)))
        self.assertEqual(free.mode, fs.MODE_EXPORT)
        self.assertGreater(held.discharge_cutoff_pct, free.discharge_cutoff_pct)
        self.assertLess(held.planned_kwh, free.planned_kwh)
        self.assertAlmostEqual(held.committed_kwh, 4.0, places=3)

    def test_a_19_to_20_axle_window_is_reserved_even_though_it_is_outside_the_peak(self):
        """After the peak ends is exactly when a naive planner would sell it."""
        free = fs.plan(_inputs((16, 30), soc_pct=95.0))
        held = fs.plan(_inputs((16, 30), soc_pct=95.0,
                               commitments=(_axle((2026, 9, 16), 19, 20),)))
        self.assertGreater(held.discharge_cutoff_pct, free.discharge_cutoff_pct)
        self.assertAlmostEqual(held.committed_kwh, 4.0, places=3)

    def test_axle_and_octopus_on_the_same_day_are_both_reserved(self):
        axle = _axle((2026, 9, 16), 18, 19)
        octo = fs.EventCommitment(
            source="octopus", kind="export",
            start=fs._wall(LONDON, datetime(2026, 9, 16).date(), fs.time(20, 0)),
            end=fs._wall(LONDON, datetime(2026, 9, 16).date(), fs.time(21, 0)),
            energy_kwh=4.0, event_id="ss-1")
        one  = fs.plan(_inputs((16, 30), soc_pct=95.0, commitments=(axle,)))
        both = fs.plan(_inputs((16, 30), soc_pct=95.0, commitments=(axle, octo)))
        self.assertAlmostEqual(both.committed_kwh, 8.0, places=3)
        self.assertGreater(both.discharge_cutoff_pct, one.discharge_cutoff_pct)

    def test_a_late_announcement_changes_the_plan_immediately(self):
        """Same clock, same battery — only the announcement is new."""
        before = fs.plan(_inputs((16, 30), soc_pct=95.0))
        after  = fs.plan(_inputs((16, 30), soc_pct=95.0,
                                 commitments=(_axle((2026, 9, 16), 17, 18,
                                                    announced_h=16),)))
        self.assertNotEqual(before.control_key(), after.control_key())
        self.assertGreater(after.discharge_cutoff_pct, before.discharge_cutoff_pct)

    def test_a_cancelled_event_releases_the_energy_again(self):
        """Cancellation is the commitment leaving the set, and it must be free."""
        held = fs.plan(_inputs((16, 30), soc_pct=95.0,
                               commitments=(_axle((2026, 9, 16), 18, 19),)))
        gone = fs.plan(_inputs((16, 30), soc_pct=95.0, commitments=()))
        self.assertLess(gone.discharge_cutoff_pct, held.discharge_cutoff_pct)
        self.assertGreater(gone.planned_kwh, held.planned_kwh)

    def test_the_commitment_signature_notices_a_new_or_changed_event(self):
        a = _axle((2026, 9, 16), 18, 19)
        b = _axle((2026, 9, 16), 18, 19, kw=6.0)
        self.assertEqual(fs.commitment_signature((a,)), fs.commitment_signature((a,)))
        self.assertNotEqual(fs.commitment_signature((a,)), fs.commitment_signature((b,)))
        self.assertNotEqual(fs.commitment_signature((a,)), fs.commitment_signature(()))

    def test_a_commitment_bigger_than_the_battery_is_reported_not_invented(self):
        """Promised energy the battery cannot hold is a fact, not a rounding."""
        huge = fs.EventCommitment(
            source="axle", kind="export",
            start=fs._wall(LONDON, datetime(2026, 9, 16).date(), fs.time(6, 0)),
            end=fs._wall(LONDON, datetime(2026, 9, 16).date(), fs.time(23, 0)),
            energy_kwh=120.0, event_id="huge")
        d = fs.plan(_inputs((2, 30), soc_pct=30.0, commitments=(huge,)))
        self.assertGreater(d.infeasible_kwh, 0.0)
        self.assertIn("not fit", d.reason)

    def test_the_charge_window_buys_for_a_commitment_it_cannot_yet_serve(self):
        plain     = fs.plan(_inputs((2, 30), soc_pct=30.0))
        committed = fs.plan(_inputs((2, 30), soc_pct=30.0,
                                    commitments=(_axle((2026, 9, 16), 18, 19),)))
        self.assertEqual(committed.mode, fs.MODE_CHARGE)
        self.assertGreater(committed.planned_kwh, plain.planned_kwh)

    def test_an_event_reward_is_never_added_to_the_tariff_price(self):
        """Different money for the same kWh. The margin must not move."""
        paid = _axle((2026, 9, 16), 18, 19)
        paid = fs.EventCommitment(**{**paid.__dict__, "reward_p_per_kwh": 300.0})
        a = fs.plan(_inputs((16, 30), soc_pct=95.0))
        b = fs.plan(_inputs((16, 30), soc_pct=95.0, commitments=(paid,)))
        self.assertEqual(a.margin_p, b.margin_p)

    def test_household_demand_is_not_counted_twice_against_a_commitment(self):
        """The reservation is the EVENT's energy, not the event plus the house.

        With a flat 24 kWh day, an hour of house load is 1 kWh; a 4 kWh event in
        that hour must raise the floor by the event's own energy and its losses,
        not by 5 kWh.
        """
        site = _site()
        base = fs.plan(_inputs((16, 30), soc_pct=95.0))
        ev   = fs.plan(_inputs((16, 30), soc_pct=95.0,
                               commitments=(_axle((2026, 9, 16), 18, 19),)))
        rise_kwh = ((ev.discharge_cutoff_pct - base.discharge_cutoff_pct) / 100.0
                    * site.capacity_kwh)
        self.assertGreater(rise_kwh, 4.0 - 0.6)
        self.assertLess(rise_kwh, 4.0 / site.one_way_efficiency + 0.8)


# ================================================================
# 2. Chronological energy budget
# ================================================================

class TestChronologicalBudget(unittest.TestCase):

    def test_an_afternoon_of_sun_cannot_supply_a_dawn_load(self):
        """The whole point of the rewrite: a day total would net these to zero.

        Load lands 05:00-08:00, the sun arrives from noon. A day-total planner
        sees 12 kWh of load and 12 kWh of sun and buys nothing.
        """
        d   = datetime(2026, 9, 16, tzinfo=LONDON).date()
        now = fs._wall(LONDON, d, fs.time(2, 30))
        slots = [0.0] * 48
        for i in range(10, 16):          # 05:00-08:00 local
            slots[i] = 2.0
        house = fs.HalfHourProfile(slots, LONDON)
        buckets = {}
        for offset in range(3):
            dd = d + timedelta(days=offset)
            for h in range(24):
                buckets[f"{dd:%Y-%m-%d} {h:02d}:00:00"] = 4000.0 if 12 <= h < 15 else 0.0
        pv = fs.HourlyPvForecast(buckets, LONDON)
        # Unprofitable peak, so the only kWh bought are the household's — this
        # test is about WHEN energy arrives, not about trading.
        lean = _bands(d, now, exp={**EXPORT_P, "peak": 18.0})
        decision = fs.plan(_inputs((2, 30), soc_pct=20.0, house=house, pv=pv,
                                   bands=lean))
        self.assertEqual(decision.mode, fs.MODE_CHARGE)
        self.assertGreaterEqual(decision.planned_kwh, 10.0)

    def test_the_same_energy_arriving_before_the_load_is_not_bought(self):
        """The mirror case, which is what proves the first one is about ORDER."""
        d = datetime(2026, 9, 16, tzinfo=LONDON).date()
        slots = [0.0] * 48
        for i in range(36, 42):          # 18:00-21:00 local
            slots[i] = 2.0
        house = fs.HalfHourProfile(slots, LONDON)
        buckets = {}
        for offset in range(3):
            dd = d + timedelta(days=offset)
            for h in range(24):
                buckets[f"{dd:%Y-%m-%d} {h:02d}:00:00"] = 4000.0 if 9 <= h < 12 else 0.0
        pv = fs.HourlyPvForecast(buckets, LONDON)
        lean = _bands(d, fs._wall(LONDON, d, fs.time(2, 30)),
                      exp={**EXPORT_P, "peak": 18.0})
        decision = fs.plan(_inputs((2, 30), soc_pct=20.0, house=house, pv=pv,
                                   bands=lean))
        self.assertLess(decision.planned_kwh, 6.0)

    def test_daytime_load_served_directly_by_pv_is_not_battery_headroom(self):
        """Gross solar is not the headroom requirement.

        A house that eats most of its own generation leaves little to bank, so
        the charge must not be cut back as though all of it were arriving.
        """
        d = datetime(2026, 9, 16, tzinfo=LONDON).date()
        hungry = fs.HalfHourProfile(
            [2.0 if 16 <= i < 34 else 0.2 for i in range(48)], LONDON)  # 08:00-17:00
        sunny  = _pv(d, 20.0)
        lean   = _bands(d, fs._wall(LONDON, d, fs.time(2, 30)),
                        exp={**EXPORT_P, "peak": 18.0})
        greedy = fs.plan(_inputs((2, 30), soc_pct=30.0, house=hungry, pv=sunny,
                                 bands=lean))
        idle   = fs.plan(_inputs((2, 30), soc_pct=30.0, house=_profile(8.0),
                                 pv=sunny, bands=lean))
        self.assertGreater(greedy.planned_kwh, idle.planned_kwh)

    def test_the_cheap_window_house_load_is_added_to_the_charge(self):
        """A charge the house eats half of does not arrive at 05:00 intact."""
        d     = datetime(2026, 9, 16, tzinfo=LONDON).date()
        lean  = _bands(d, fs._wall(LONDON, d, fs.time(2, 15)),
                       exp={**EXPORT_P, "peak": 18.0})
        quiet = fs.plan(_inputs((2, 15), soc_pct=30.0, house=_profile(6.0), bands=lean))
        busy  = fs.plan(_inputs((2, 15), soc_pct=30.0, house=_profile(48.0), bands=lean))
        self.assertGreater(busy.planned_kwh, quiet.planned_kwh)

    def test_charging_respects_the_maximum_charge_soc_even_for_arbitrage(self):
        d = fs.plan(_inputs((2, 30), soc_pct=20.0,
                            site=_site(max_charge_soc_pct=85.0)))
        self.assertLessEqual(d.charge_cutoff_pct, 85.0)

    def test_solar_headroom_never_pushes_the_target_below_the_reserve(self):
        d = fs.plan(_inputs((2, 30), soc_pct=5.0,
                            pv=_pv(datetime(2026, 9, 16, tzinfo=LONDON).date(), 40.0)))
        self.assertEqual(d.mode, fs.MODE_CHARGE)
        self.assertGreaterEqual(d.charge_cutoff_pct, 19.0)

    def test_a_sunnier_day_still_buys_less(self):
        day   = datetime(2026, 9, 16, tzinfo=LONDON).date()
        lean  = _bands(day, fs._wall(LONDON, day, fs.time(2, 30)),
                       exp={**EXPORT_P, "peak": 18.0})
        dull  = fs.plan(_inputs((2, 30), soc_pct=25.0, pv=_pv(day, 2.0), bands=lean))
        sunny = fs.plan(_inputs((2, 30), soc_pct=25.0, pv=_pv(day, 30.0), bands=lean))
        self.assertLess(sunny.planned_kwh, dull.planned_kwh)

    def test_efficiency_is_two_one_way_legs_whose_product_is_the_round_trip(self):
        site = _site(efficiency=0.94)
        self.assertAlmostEqual(site.one_way_efficiency ** 2, 0.94, places=9)

    def test_the_simulation_charges_losses_in_both_directions(self):
        """1 kWh of surplus PV banks less than 1 kWh; 1 kWh of load costs more."""
        inputs = _inputs((12, 0), soc_pct=50.0)
        start  = 10.0
        end, _low, _unmet, _high = fs.simulate(
            inputs, start,
            fs._wall(LONDON, datetime(2026, 9, 16).date(), fs.time(12, 0)),
            fs._wall(LONDON, datetime(2026, 9, 16).date(), fs.time(13, 0)))
        self.assertNotEqual(end, start)


# ================================================================
# 3. Two floors
# ================================================================

class TestTwoFloors(unittest.TestCase):

    def test_the_house_may_eat_its_own_evening(self):
        """20% reserve plus exactly the evening's demand must be CONSUMABLE.

        A single floor that protected all future household consumption from the
        household itself would leave the battery full and the house importing at
        the day rate — precisely backwards.
        """
        d = fs.plan(_inputs((16, 30), soc_pct=45.0))
        self.assertEqual(d.mode, fs.MODE_SUPPLY_HOUSE)
        self.assertLessEqual(d.household_floor_pct, 21.0)
        self.assertGreater(d.discharge_limit_w, 0)

    def test_but_discretionary_export_may_not_sell_the_evening(self):
        d = fs.plan(_inputs((16, 30), soc_pct=95.0))
        self.assertEqual(d.mode, fs.MODE_EXPORT)
        self.assertGreater(d.protect_soc_pct, d.household_floor_pct)

    def test_the_household_floor_still_protects_a_commitment(self):
        plain = fs.plan(_inputs((16, 30), soc_pct=45.0))
        held  = fs.plan(_inputs((16, 30), soc_pct=45.0,
                                commitments=(_axle((2026, 9, 16), 18, 19),)))
        self.assertGreater(held.household_floor_pct, plain.household_floor_pct)

    def test_the_reserve_is_never_sold_or_consumed_through(self):
        for soc in (21.0, 35.0, 60.0, 100.0):
            for hhmm in ((16, 30), (2, 30), (23, 0)):
                d = fs.plan(_inputs(hhmm, soc_pct=soc))
                self.assertGreaterEqual(d.household_floor_pct, 20.0,
                                        f"reserve breached at {soc}% / {hhmm}")
                if d.owns:
                    self.assertGreaterEqual(d.discharge_cutoff_pct, 20.0)

    def test_a_raised_policy_floor_outranks_the_flat_reserve(self):
        d = fs.plan(_inputs((16, 30), soc_pct=95.0, site=_site(policy_floor_pct=70.0)))
        self.assertGreaterEqual(d.discharge_cutoff_pct, 70.0)


# ================================================================
# 5. Rate evidence
# ================================================================

class TestRateEvidence(unittest.TestCase):

    def test_bands_are_read_from_one_day_and_never_averaged_across_two(self):
        """An October product change must not produce a price nobody charged."""
        day = datetime(2026, 9, 16, tzinfo=LONDON).date()
        old = _spans(day, IMPORT_P, days=1)
        new = _spans(day + timedelta(days=1), {"cheap": 21.0, "day": 35.0, "peak": 50.0},
                     days=1)
        now = fs._wall(LONDON, day, fs.time(0, 0))
        bands = fs.derive_bands(old + new, _spans(day, EXPORT_P), LONDON, now, day)
        self.assertIsNotNone(bands)
        self.assertEqual(bands.import_cheap_p, IMPORT_P["cheap"])

    def test_a_gap_in_coverage_is_refused(self):
        day = datetime(2026, 9, 16, tzinfo=LONDON).date()
        spans = [s for s in _spans(day, IMPORT_P)
                 if not (2 <= s.start.astimezone(LONDON).hour < 5
                         and s.start.astimezone(LONDON).date() == day)]
        now = fs._wall(LONDON, day, fs.time(0, 0))
        self.assertIsNone(fs.derive_bands(spans, _spans(day, EXPORT_P), LONDON, now, day))

    def test_conflicting_overlapping_prices_are_refused(self):
        day = datetime(2026, 9, 16, tzinfo=LONDON).date()
        spans = _spans(day, IMPORT_P)
        clash = fs.RateSpan(start=spans[1].start, end=spans[1].end, p=99.0)
        now = fs._wall(LONDON, day, fs.time(0, 0))
        self.assertIsNone(fs.derive_bands(spans + [clash], _spans(day, EXPORT_P),
                                          LONDON, now, day))

    def test_the_decision_never_outlives_the_published_prices(self):
        day = datetime(2026, 9, 16, tzinfo=LONDON).date()
        now = fs._wall(LONDON, day, fs.time(2, 30))
        bands = _bands(day, now)
        short = fs.FluxBands(**{**bands.__dict__,
                                "covers_until": now + timedelta(minutes=8)})
        d = fs.plan(_inputs((2, 30), soc_pct=30.0, bands=short))
        self.assertLessEqual(d.decision_until, now + timedelta(minutes=8))

    def test_prices_that_have_run_out_defer(self):
        day = datetime(2026, 9, 16, tzinfo=LONDON).date()
        now = fs._wall(LONDON, day, fs.time(2, 30))
        bands = _bands(day, now)
        expired = fs.FluxBands(**{**bands.__dict__, "covers_until": now})
        d = fs.plan(_inputs((2, 30), soc_pct=30.0, bands=expired))
        self.assertTrue(d.deferred)

    def test_a_two_band_go_shape_is_refused(self):
        day = datetime(2026, 9, 16, tzinfo=LONDON).date()
        base = fs._wall(LONDON, day, fs.time(0, 0))
        go = [fs.RateSpan(base, base + timedelta(hours=5), 8.6),
              fs.RateSpan(base + timedelta(hours=5), base + timedelta(hours=24), 28.0)]
        self.assertIsNone(fs.derive_bands(go, _spans(day, EXPORT_P), LONDON, base, day))

    def test_a_cheap_window_on_a_different_clock_is_refused(self):
        day = datetime(2026, 9, 16, tzinfo=LONDON).date()
        base = fs._wall(LONDON, day, fs.time(0, 0))
        moved = []
        for offset in range(3):
            b = base + timedelta(days=offset)
            for h0, h1, key in ((0, 1, "day"), (1, 4, "cheap"), (4, 16, "day"),
                                (16, 19, "peak"), (19, 24, "day")):
                moved.append(fs.RateSpan(b + timedelta(hours=h0),
                                         b + timedelta(hours=h1), IMPORT_P[key]))
        self.assertIsNone(fs.derive_bands(moved, _spans(day, EXPORT_P),
                                          LONDON, base, day))


# ================================================================
# 6. Strict validation
# ================================================================

class TestStrictValidation(unittest.TestCase):

    def _deferred(self, **over):
        over.setdefault("soc_pct", 30.0)
        d = fs.plan(_inputs((2, 30), **over))
        self.assertTrue(d.deferred, f"expected a defer, got {d.mode}: {d.reason}")
        self.assertFalse(d.owns)
        return d

    def test_nan_soc_is_refused(self):
        self._deferred(soc_pct=float("nan"))

    def test_infinite_capacity_is_refused(self):
        self._deferred(site=_site(capacity_kwh=float("inf")))

    def test_a_bool_is_not_a_number(self):
        """`isinstance(True, int)` is True, so a bool would read as 1."""
        self._deferred(soc_pct=True)

    def test_a_negative_age_means_a_future_timestamp_and_is_refused(self):
        d = self._deferred(telemetry_age_s=-5.0)
        self.assertIn("future", d.reason)

    def test_an_soc_above_a_hundred_is_refused(self):
        self._deferred(soc_pct=140.0)

    def test_a_negative_soc_is_refused(self):
        self._deferred(soc_pct=-1.0)

    def test_a_reserve_over_a_hundred_is_refused(self):
        self._deferred(site=_site(reserve_pct=180.0))

    def test_a_zero_power_limit_is_refused(self):
        self._deferred(site=_site(charge_power_w=0))

    def test_an_efficiency_over_one_is_refused(self):
        self._deferred(site=_site(efficiency=1.4))

    def test_a_stale_profile_is_refused(self):
        d = self._deferred(profile_age_s=fs.MAX_PROFILE_AGE_S + 1)
        self.assertIn("profile", d.reason)

    def test_a_profile_with_a_negative_slot_is_refused(self):
        with self.assertRaises(ValueError):
            fs.HalfHourProfile([0.5] * 47 + [-1.0], LONDON)

    def test_a_profile_of_the_wrong_length_is_refused(self):
        with self.assertRaises(ValueError):
            fs.HalfHourProfile([0.5] * 47, LONDON)

    def test_an_empty_profile_is_refused_rather_than_read_as_no_need(self):
        with self.assertRaises(ValueError):
            fs.HalfHourProfile([0.0] * 48, LONDON)

    def test_a_nan_in_the_forecast_is_dropped_not_integrated(self):
        day = datetime(2026, 9, 16, tzinfo=LONDON).date()
        pv = fs.HourlyPvForecast(
            {f"{day:%Y-%m-%d} 12:00:00": float("nan"),
             f"{day:%Y-%m-%d} 13:00:00": 1000.0}, LONDON)
        a = fs._wall(LONDON, day, fs.time(12, 0))
        self.assertFalse(pv.covers(a, a + timedelta(hours=2)))
        self.assertAlmostEqual(pv.kwh_between(a, a + timedelta(hours=2)), 1.0, places=6)

    def test_a_commitment_that_ends_before_it_starts_is_refused(self):
        bad = fs.EventCommitment(
            source="axle", kind="export",
            start=fs._wall(LONDON, datetime(2026, 9, 16).date(), fs.time(19, 0)),
            end=fs._wall(LONDON, datetime(2026, 9, 16).date(), fs.time(18, 0)),
            energy_kwh=4.0)
        self._deferred(commitments=(bad,))

    def test_a_naive_commitment_datetime_is_refused(self):
        bad = fs.EventCommitment(
            source="axle", kind="export",
            start=datetime(2026, 9, 16, 18, 0), end=datetime(2026, 9, 16, 19, 0),
            energy_kwh=4.0)
        self._deferred(commitments=(bad,))

    def test_a_missing_forecast_hour_defers_the_peak_decision_too(self):
        """Zero PV is not the safe reading when it decides whether to hand back.

        In the peak window an unpublished hour read as zero hides the generation
        that decides between exporting and letting the old manager bank it, and a
        charge-blocking mode held over a working roof throws the afternoon away.
        """
        day = datetime(2026, 9, 16, tzinfo=LONDON).date()
        buckets = {f"{day:%Y-%m-%d} {h:02d}:00:00": 0.0 for h in range(16)}
        d = fs.plan(_inputs((16, 30), soc_pct=95.0,
                            pv=fs.HourlyPvForecast(buckets, LONDON)))
        self.assertTrue(d.deferred)
        self.assertIn("peak window", d.reason)

    def test_a_missing_forecast_hour_defers_the_charge_rather_than_reading_zero(self):
        day = datetime(2026, 9, 16, tzinfo=LONDON).date()
        buckets = {f"{day:%Y-%m-%d} {h:02d}:00:00": 0.0 for h in range(24)}
        d = self._deferred(pv=fs.HourlyPvForecast(buckets, LONDON))
        self.assertIn("does not cover", d.reason)

    def test_every_missing_input_defers(self):
        for over, fragment in (
                ({"enabled": False},          "switched off"),
                ({"commissioned": False},     "commissioning"),
                ({"tariff_verified": False},  "import and the export"),
                ({"bands": None},             "Flux shape"),
                ({"rates_age_s": fs.MAX_RATES_AGE_S + 1}, "rates"),
                ({"forecast_age_s": fs.MAX_FORECAST_AGE_S + 1}, "forecast"),
                ({"telemetry_age_s": fs.MAX_TELEMETRY_AGE_S + 1}, "inverter reading"),
                ({"soc_pct": None},           "state of charge"),
                ({"house": None},             "consumption profile"),
                ({"pv": None},                "solar forecast"),
                ({"site": _site(import_limit_verified=False)}, "site import limit"),
        ):
            over.setdefault("soc_pct", 30.0)
            d = fs.plan(_inputs((2, 30), **over))
            self.assertTrue(d.deferred, f"{over} did not defer")
            self.assertIn(fragment, d.reason)


# ================================================================
# 7. DST
# ================================================================

# ================================================================
# Per-day solar bias
# ================================================================

class TestPerDayBias(unittest.TestCase):
    """Today and tomorrow carry DIFFERENT corrections, and both are published.

    The day in progress has measured generation behind it and the next one has
    none, so applying today's factor to tomorrow's buckets biases the overnight
    charge by whatever the two disagree by.
    """

    def test_each_day_gets_its_own_factor(self):
        day = datetime(2026, 9, 16, tzinfo=LONDON).date()
        buckets = {f"{day:%Y-%m-%d} 12:00:00": 1000.0,
                   f"{day + timedelta(days=1):%Y-%m-%d} 12:00:00": 1000.0}
        pv = fs.HourlyPvForecast(buckets, LONDON, bias=1.0,
                                 bias_by_date={day: 1.2,
                                               day + timedelta(days=1): 0.8})
        today_noon = fs._wall(LONDON, day, fs.time(12, 0))
        tmrw_noon  = fs._wall(LONDON, day + timedelta(days=1), fs.time(12, 0))
        self.assertAlmostEqual(
            pv.kwh_between(today_noon, today_noon + timedelta(hours=1)), 1.2, places=6)
        self.assertAlmostEqual(
            pv.kwh_between(tmrw_noon, tmrw_noon + timedelta(hours=1)), 0.8, places=6)

    def test_a_window_spanning_midnight_applies_both(self):
        day = datetime(2026, 9, 16, tzinfo=LONDON).date()
        buckets = {f"{day:%Y-%m-%d} 23:00:00": 1000.0,
                   f"{day + timedelta(days=1):%Y-%m-%d} 00:00:00": 1000.0}
        pv = fs.HourlyPvForecast(buckets, LONDON,
                                 bias_by_date={day: 2.0,
                                               day + timedelta(days=1): 0.5})
        a = fs._wall(LONDON, day, fs.time(23, 0))
        self.assertAlmostEqual(pv.kwh_between(a, a + timedelta(hours=2)), 2.5, places=6)

    def test_an_unusable_per_day_factor_falls_back_rather_than_breaking(self):
        day = datetime(2026, 9, 16, tzinfo=LONDON).date()
        buckets = {f"{day:%Y-%m-%d} 12:00:00": 1000.0}
        pv = fs.HourlyPvForecast(buckets, LONDON, bias=1.1,
                                 bias_by_date={day: float("nan")})
        noon = fs._wall(LONDON, day, fs.time(12, 0))
        self.assertAlmostEqual(pv.kwh_between(noon, noon + timedelta(hours=1)),
                               1.1, places=6)

    def test_a_single_bias_still_applies_when_no_day_is_named(self):
        day = datetime(2026, 9, 16, tzinfo=LONDON).date()
        buckets = {f"{day:%Y-%m-%d} 12:00:00": 1000.0}
        pv = fs.HourlyPvForecast(buckets, LONDON, bias=1.2)
        noon = fs._wall(LONDON, day, fs.time(12, 0))
        self.assertAlmostEqual(pv.kwh_between(noon, noon + timedelta(hours=1)),
                               1.2, places=6)



class TestDaylightSaving(unittest.TestCase):
    """Real ZoneInfo, both UK changes, through the plugin's own timezone object."""

    SPRING = (2026, 3, 29)     # 01:00 GMT -> 02:00 BST
    AUTUMN = (2026, 10, 25)    # 02:00 BST -> 01:00 GMT

    def test_the_spring_day_is_twenty_three_hours_of_household_load(self):
        p = _profile(24.0)
        d = datetime(*self.SPRING, tzinfo=LONDON).date()
        a = fs._wall(LONDON, d, fs.time(0, 0))
        b = fs._wall(LONDON, d + timedelta(days=1), fs.time(0, 0))
        self.assertAlmostEqual((b - a).total_seconds() / 3600.0, 23.0, places=6)
        # 23 real hours, charged at the local slot rates: one hour is skipped.
        self.assertLess(p.kwh_between(a, b), 24.0)
        self.assertGreater(p.kwh_between(a, b), 22.0)

    def test_the_autumn_day_is_twenty_five_hours_of_household_load(self):
        p = _profile(24.0)
        d = datetime(*self.AUTUMN, tzinfo=LONDON).date()
        a = fs._wall(LONDON, d, fs.time(0, 0))
        b = fs._wall(LONDON, d + timedelta(days=1), fs.time(0, 0))
        self.assertAlmostEqual((b - a).total_seconds() / 3600.0, 25.0, places=6)
        self.assertGreater(p.kwh_between(a, b), 24.0)
        self.assertLess(p.kwh_between(a, b), 26.0)

    def test_the_cheap_window_is_three_real_hours_on_both_changeover_days(self):
        for day in (self.SPRING, self.AUTUMN):
            d = datetime(*day, tzinfo=LONDON).date()
            start = fs._wall(LONDON, d, fs.FLUX_CHEAP_START)
            end   = fs._wall(LONDON, d, fs.FLUX_CHEAP_END)
            self.assertAlmostEqual((end - start).total_seconds() / 3600.0, 3.0,
                                   places=6, msg=f"{day}")

    def test_next_local_never_returns_a_time_in_the_past(self):
        for day in (self.SPRING, self.AUTUMN):
            d = datetime(*day, tzinfo=LONDON).date()
            for hour in range(0, 24):
                now = fs._wall(LONDON, d, fs.time(hour, 0))
                for hhmm in (fs.FLUX_CHEAP_START, fs.FLUX_CHEAP_END,
                             fs.FLUX_PEAK_START, fs.FLUX_PEAK_END):
                    nxt = fs.next_local(now, LONDON, hhmm)
                    self.assertGreater(nxt, now)
                    self.assertLess(nxt - now, timedelta(hours=26))

    def test_the_window_test_agrees_with_the_boundary_walk_on_changeover_days(self):
        for day in (self.SPRING, self.AUTUMN):
            d = datetime(*day, tzinfo=LONDON).date()
            cursor = fs._wall(LONDON, d, fs.time(0, 0))
            stop   = fs._wall(LONDON, d + timedelta(days=1), fs.time(0, 0))
            while cursor < stop:
                inside = fs.in_window(cursor, LONDON, fs.FLUX_CHEAP_START,
                                      fs.FLUX_CHEAP_END)
                local_hour = cursor.astimezone(LONDON).hour
                self.assertEqual(inside, 2 <= local_hour < 5,
                                 f"{day} {cursor.astimezone(LONDON)}")
                cursor += timedelta(minutes=30)

    def test_a_plan_made_on_each_changeover_day_is_coherent(self):
        for day in (self.SPRING, self.AUTUMN):
            d = fs.plan(_inputs((2, 30), soc_pct=30.0, day=day))
            self.assertIn(d.mode, (fs.MODE_CHARGE, fs.MODE_HOLD))
            self.assertLessEqual(d.decision_until - d.decision_at,
                                 timedelta(minutes=fs.MAX_DECISION_MINUTES))

    def test_a_forecast_bucket_is_found_by_local_wall_time_on_a_changeover_day(self):
        d = datetime(*self.AUTUMN, tzinfo=LONDON).date()
        pv = _pv(d, 12.0)
        a  = fs._wall(LONDON, d, fs.time(12, 0))
        self.assertTrue(pv.covers(a, a + timedelta(hours=1)))
        self.assertGreater(pv.kwh_between(a, a + timedelta(hours=1)), 0.0)


# ================================================================
# 8. Physical power caps
# ================================================================

class TestPhysicalHeadroom(unittest.TestCase):

    def test_site_import_headroom_is_the_limit_less_the_house(self):
        inputs = _inputs((2, 30), soc_pct=20.0,
                         flows=_flows(pv_w=0.0, house_w=3000.0, grid_w=3000.0))
        self.assertEqual(fs.import_headroom_w(inputs), 7000)

    def test_the_charge_is_capped_by_that_headroom_not_by_the_inverter_rating(self):
        d = fs.plan(_inputs((2, 30), soc_pct=10.0,
                            flows=_flows(pv_w=0.0, house_w=6000.0, grid_w=6000.0)))
        self.assertEqual(d.mode, fs.MODE_CHARGE)
        self.assertLessEqual(d.charge_limit_w, 4000)

    def test_a_house_already_at_the_site_limit_defers_the_charge(self):
        d = fs.plan(_inputs((2, 30), soc_pct=10.0,
                            flows=_flows(pv_w=0.0, house_w=10000.0, grid_w=10000.0)))
        self.assertTrue(d.deferred)
        self.assertIn("spare import capacity", d.reason)

    def test_pv_already_flowing_counts_against_the_export_cap(self):
        inputs = _inputs((16, 30), soc_pct=95.0,
                         flows=_flows(pv_w=3000.0, house_w=500.0, grid_w=-2500.0))
        self.assertEqual(fs.export_headroom_w(inputs), 4000 - 2500)

    def test_the_export_limit_is_full_power_and_the_inverter_caps_the_meter(self):
        """17-Sep-2026 live: sizing the limit to "cap less live PV" moved every tick
        and read zero when the roof filled the cap, which handed the peak back."""
        for pv in (3000.0, 4700.0, 6000.0):
            with self.subTest(pv=pv):
                d = fs.plan(_inputs((16, 30), soc_pct=95.0,
                                    flows=_flows(pv_w=pv, house_w=500.0, grid_w=-2500.0)))
                self.assertEqual(d.mode, fs.MODE_EXPORT)
                self.assertEqual(d.discharge_limit_w, 10000)
                self.assertEqual(d.charge_limit_w, 0)

    def test_missing_flow_readings_defer_rather_than_assume_the_rating(self):
        d = fs.plan(_inputs((2, 30), soc_pct=20.0, flows=None))
        self.assertTrue(d.deferred)
        self.assertIn("live power readings", d.reason)

    def test_stale_flow_readings_defer(self):
        d = fs.plan(_inputs((2, 30), soc_pct=20.0,
                            flows_age_s=fs.MAX_FLOW_AGE_S + 1))
        self.assertTrue(d.deferred)
        self.assertIn("power readings", d.reason)


# ================================================================
# Windows, leases and the executor contract
# ================================================================

class TestWindowsAndLeases(unittest.TestCase):

    def test_the_charge_is_mode_three_with_no_discharge(self):
        d = fs.plan(_inputs((2, 30), soc_pct=25.0))
        self.assertEqual(d.mode, fs.MODE_CHARGE)
        self.assertEqual(d.ems_mode, fs.EMS_CHARGE_GRID)
        self.assertEqual(d.discharge_limit_w, 0)

    def test_the_export_is_mode_five_with_no_charging(self):
        d = fs.plan(_inputs((16, 30), soc_pct=95.0))
        self.assertEqual(d.ems_mode, fs.EMS_DISCHARGE_PV)
        self.assertEqual(d.charge_limit_w, 0)

    def test_an_unprofitable_peak_supplies_the_house_and_sells_nothing(self):
        day  = datetime(2026, 9, 16, tzinfo=LONDON).date()
        now  = fs._wall(LONDON, day, fs.time(16, 30))
        poor = _bands(day, now, exp={**EXPORT_P, "peak": 18.0})
        d = fs.plan(_inputs((16, 30), soc_pct=95.0, bands=poor))
        self.assertEqual(d.mode, fs.MODE_SUPPLY_HOUSE)
        self.assertEqual(d.charge_limit_w, 0)

    def test_midday_hands_the_inverter_back(self):
        d = fs.plan(_inputs((11, 0), soc_pct=60.0))
        self.assertEqual(d.mode, fs.MODE_SOLAR)
        self.assertFalse(d.owns)

    def test_no_hold_on_a_real_flux_shape_because_the_house_beats_the_sale(self):
        d = fs.plan(_inputs((15, 0), soc_pct=80.0))
        self.assertEqual(d.mode, fs.MODE_SOLAR)

    def test_a_hold_fires_where_it_genuinely_pays(self):
        day = datetime(2026, 9, 16, tzinfo=LONDON).date()
        now = fs._wall(LONDON, day, fs.time(15, 0))
        rich = _bands(day, now, exp={**EXPORT_P, "peak": 40.0})
        d = fs.plan(_inputs((15, 0), soc_pct=80.0, bands=rich))
        self.assertEqual(d.mode, fs.MODE_HOLD)
        self.assertEqual(d.discharge_limit_w, 0)
        self.assertGreater(d.charge_limit_w, 0)

    def test_no_decision_outlives_its_window_or_its_lease(self):
        for hhmm, soc in (((2, 30), 30.0), ((4, 55), 30.0), ((16, 30), 95.0),
                          ((18, 50), 95.0), ((16, 30), 22.0)):
            d = fs.plan(_inputs(hhmm, soc_pct=soc))
            if not d.owns:
                continue
            life = d.decision_until - d.decision_at
            self.assertGreater(life, timedelta(0))
            self.assertLessEqual(life, timedelta(minutes=fs.MAX_DECISION_MINUTES))

    def test_a_commitment_edge_ends_the_decision_early(self):
        d = fs.plan(_inputs((16, 30), soc_pct=95.0,
                            commitments=(_axle((2026, 9, 16), 16, 17),)))
        edge = fs._wall(LONDON, datetime(2026, 9, 16).date(), fs.time(17, 0))
        self.assertLessEqual(d.decision_until, edge)

    def test_decision_at_is_the_moment_the_plan_was_made(self):
        inputs = _inputs((2, 30), soc_pct=30.0)
        d = fs.plan(inputs)
        self.assertEqual(d.decision_at, inputs.now)

    def test_every_owning_decision_has_one_direction_of_travel(self):
        for hhmm, soc in (((2, 30), 30.0), ((16, 30), 95.0), ((16, 30), 22.0),
                          ((2, 30), 99.0)):
            d = fs.plan(_inputs(hhmm, soc_pct=soc))
            if not d.owns:
                continue
            self.assertFalse(d.charge_limit_w and d.discharge_limit_w)

    def test_cutoffs_are_ordered_and_at_register_resolution(self):
        for hhmm, soc in (((2, 30), 30.0), ((16, 30), 95.0), ((16, 30), 40.0)):
            d = fs.plan(_inputs(hhmm, soc_pct=soc))
            if not d.owns:
                continue
            self.assertLessEqual(d.discharge_cutoff_pct, d.charge_cutoff_pct)
            for v in (d.charge_cutoff_pct, d.discharge_cutoff_pct):
                self.assertAlmostEqual(v * 10, round(v * 10), places=6)


class TestExecutorWouldAcceptThesePlans(unittest.TestCase):
    """Runs the REAL executor validator over real planner output."""

    def test_every_owning_decision_makes_an_acceptable_target(self):
        import tempfile

        import flux_execution as fe

        class _Raw:
            inverter_max_w = 10000

        with tempfile.TemporaryDirectory() as tmp:
            ex = fe.FluxExecutor(_Raw(), f"{tmp}/journal.json",
                                 baseline_charge_w=10000, baseline_discharge_w=10000)
            checked = 0
            cases = [((2, 30), 30.0, ()), ((3, 0), 25.0, ()), ((4, 55), 40.0, ()),
                     ((16, 30), 95.0, ()), ((18, 55), 90.0, ()), ((16, 30), 22.0, ()),
                     ((2, 30), 99.0, ()),
                     ((16, 30), 95.0, (_axle((2026, 9, 16), 18, 19),)),
                     ((2, 30), 30.0, (_axle((2026, 9, 16), 18, 19),))]
            for hhmm, soc, commitments in cases:
                inputs = _inputs(hhmm, soc_pct=soc, commitments=commitments)
                d = fs.plan(inputs)
                if not d.owns:
                    continue
                now      = inputs.now
                observed = now - timedelta(seconds=1)
                target = fe.FluxTarget(
                    decision_at=d.decision_at, decision_until=d.decision_until,
                    observed_at=observed,
                    expires_at=min(observed + timedelta(seconds=9), d.decision_until),
                    ems_mode=d.ems_mode,
                    charge_limit_w=d.charge_limit_w,
                    discharge_limit_w=d.discharge_limit_w,
                    charge_cutoff_pct=d.charge_cutoff_pct,
                    discharge_cutoff_pct=d.discharge_cutoff_pct)
                try:
                    ex._valid_target(target, now)
                except Exception as exc:          # noqa: BLE001
                    self.fail(f"{d.mode} at {hhmm} rejected by the executor: {exc}")
                checked += 1
            self.assertGreaterEqual(checked, 6)


if __name__ == "__main__":
    unittest.main()
