#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    test_agile_readiness.py
# Description: The Agile import path on REAL winter prices (v5.100.0). Written first,
#              against the pre-fix code, from the 10-09-2026 adversarial review
#              (docs/agile-readiness-review-brief.md). Covers the planner's block
#              selection, the no-rates and no-reference fallbacks, the executor's
#              cutoff teardown and the API's silent-failure paths. Runs without Indigo.
# Author:      CliveS & Claude Fable 5.1
# Date:        10-09-2026 10:33
# Version:     1.0
import os
import sys
import threading
import types
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

if "indigo" not in sys.modules:
    _indigo = types.ModuleType("indigo")
    class _PluginBase:
        def __init__(self, *a, **k):
            pass
    _indigo.PluginBase = _PluginBase
    _indigo.Dict = dict
    _indigo.List = list
    for _attr in ("kStateImageSel", "server", "devices", "variables", "kDeviceAction",
                  "kDimmerRelayAction", "kSensorAction", "kUniversalAction", "activePlugin"):
        setattr(_indigo, _attr, MagicMock())
    sys.modules["indigo"] = _indigo
_pm = MagicMock()
sys.modules.setdefault("pymodbus", _pm)
sys.modules.setdefault("pymodbus.client", sys.modules["pymodbus"].client)
sys.modules.setdefault("pymodbus.exceptions", sys.modules["pymodbus"].exceptions)
sys.modules.setdefault("requests", MagicMock())
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import plugin                                   # noqa: E402
import octopus_api                              # noqa: E402
from battery_manager import (                   # noqa: E402
    BatteryManager, ManagerSnapshot, TariffData, Decision,
    ACTION_SELF_CONSUMPTION, ACTION_START_IMPORT, ACTION_SCHEDULE_IMPORT,
    TARIFF_AGILE,
)

# ============================================================
# Real region-F Agile prices — the fixtures the brief asked for
# ============================================================
# Pulled from the Octopus public API on 10-09-2026: product AGILE-24-10-01, tariff
# E-1R-AGILE-24-10-01-F, value_inc_vat. Each entry is (hours after 16:00 local on the
# evening date, p/kWh), 34 half-hours from 16:00 to 08:30 next morning. Two flat bands
# cannot see a slot-selection bug; these shapes can.
#
# 29-Nov-2025: the single cheapest half-hour is 06:00 (9.64p), a spike-down on the
# morning ramp — charging forward from it climbs into 13-18p. The cheap trough is
# 01:30-03:30 at 9.8-10.6p.
NIGHT_2025_11_29 = [
    (0.0, 32.886), (0.5, 33.81), (1.0, 33.5475), (1.5, 33.5475), (2.0, 32.5395),
    (2.5, 31.689), (3.0, 18.3855), (3.5, 16.758), (4.0, 16.758), (4.5, 16.695),
    (5.0, 17.199), (5.5, 16.695), (6.0, 16.695), (6.5, 14.112), (7.0, 16.0545),
    (7.5, 10.584), (8.0, 10.689), (8.5, 13.3665), (9.0, 12.789), (9.5, 10.8255),
    (10.0, 10.584), (10.5, 9.786), (11.0, 10.2375), (11.5, 10.5105), (12.0, 11.445),
    (12.5, 12.5685), (13.0, 11.277), (13.5, 10.731), (14.0, 9.639), (14.5, 13.4925),
    (15.0, 13.944), (15.5, 17.787), (16.0, 15.876), (16.5, 16.5375),
]
# 05-Oct-2025: the cheapest half-hour is 04:30 (2.45p), the LAST slot of a long
# near-zero trough, so charging forward from it buys 13-17p while 00:30-02:00 sat
# at 2.6-2.9p. The worst greedy night of the 150 replayed.
NIGHT_2025_10_05 = [
    (0.0, 10.941), (0.5, 19.4565), (1.0, 24.633), (1.5, 28.6335), (2.0, 26.4285),
    (2.5, 28.2135), (3.0, 16.5165), (3.5, 14.427), (4.0, 13.755), (4.5, 9.702),
    (5.0, 10.8675), (5.5, 5.4495), (6.0, 7.3605), (6.5, 2.562), (7.0, 8.589),
    (7.5, 3.927), (8.0, 4.1895), (8.5, 2.646), (9.0, 2.73), (9.5, 2.8665),
    (10.0, 4.1685), (10.5, 3.3075), (11.0, 4.41), (11.5, 4.011), (12.0, 4.011),
    (12.5, 2.4465), (13.0, 13.0725), (13.5, 14.8365), (14.0, 17.1885), (14.5, 18.7425),
    (15.0, 21.7455), (15.5, 24.4755), (16.0, 23.667), (16.5, 21.756),
]

CAPACITY_KWH = 35.04
EFFICIENCY   = 0.94
HALF_HOUR    = timedelta(minutes=30)


def _now(hour=16, minute=0):
    return datetime.now(timezone.utc).replace(hour=hour, minute=minute,
                                              second=0, microsecond=0)


def _base():
    """16:00 UTC today — offset 0.0 of every night fixture."""
    return _now(hour=16)


def _dawn(hours_after_base):
    return _base() + timedelta(hours=hours_after_base)


def _slots(night, upto=None):
    """Fixture -> aware (dt, p) tuples, optionally only the slots before `upto` hours."""
    return [(_base() + timedelta(hours=h), p) for h, p in night
            if upto is None or h < upto]


def _daytime_slots(dawn, price=25.0, hours=12, days_back=0):
    """Contiguous half-hours from dawn (or dawn - days_back*24h) for `hours` hours."""
    start = dawn - timedelta(days=days_back)
    return [(start + k * HALF_HOUR, price) for k in range(hours * 2)]


def _snap(slots, now_off, dawn_off=17.0, soc=40.0, today_rate_p=None,
          health_floor=10.0, tariff_key=TARIFF_AGILE, dawn_target=10.0,
          tomorrow_kwh=0.0):
    """Snapshot at now = 16:00 + now_off hours, dawn at 16:00 + dawn_off hours.

    weekday == weekend so the need is day-agnostic; the flat 0.3 kWh/slot profile
    drains 0.6 kWh/h overnight. No forecast, so the balance takes its night branch.
    """
    dawn = _dawn(dawn_off)
    dawn_times = {dawn.astimezone(timezone.utc).date().strftime("%Y-%m-%d"): dawn}
    tariff = TariffData(tariff_key=tariff_key, today_rate_p=today_rate_p,
                        agile_slots=list(slots))
    return ManagerSnapshot(
        current_soc_pct=soc, capacity_kwh=CAPACITY_KWH, efficiency=EFFICIENCY,
        dawn_target_pct=dawn_target, health_cutoff_pct=health_floor,
        export_enabled=False, tariff=tariff, forecast_p50={},
        dawn_times=dawn_times, consumption_profile=[0.30] * 48,
        now=_base() + timedelta(hours=now_off),
        weekday_kwh=22.0, weekend_kwh=22.0, inverter_max_kw=10.0,
        corrected_tomorrow_kwh=tomorrow_kwh,
    )


def _plan(snapshot):
    bm = BatteryManager()
    balance = bm._calculate_24h_balance(snapshot)
    assert balance.import_needed, "fixture must carry a genuine tomorrow deficit"
    return bm._plan_import(snapshot, balance), balance


def _offset_of(dt):
    return (dt - _base()).total_seconds() / 3600.0


def _block_cost(slots, start_dt, energies):
    """Independent oracle: cost of charging `energies` from start_dt forward,
    None if any half-hour of the block is unpriced."""
    by_dt = dict(slots)
    cost = 0.0
    for k, e in enumerate(energies):
        p = by_dt.get(start_dt + k * HALF_HOUR)
        if p is None:
            return None
        cost += p * e
    return cost


# ============================================================
# 1. Block selection — the money question in the brief
# ============================================================

class TestAgileBlockSelection(unittest.TestCase):
    """The planner must start the charge where the whole block is cheapest, not at
    the single cheapest half-hour. Over 150 real region-F nights (Oct-2025 to
    Feb-2026, 20 kWh at 9.5 kW) the single-slot rule cost GBP 421.16 against
    GBP 396.38 for the cheapest contiguous block: GBP 24.79 a season, GBP 61.63
    at 30 kWh a night, GBP 44.06 if the battery only takes 6 kW."""

    def test_november_night_starts_in_the_trough_not_on_the_spike(self):
        d, _ = _plan(_snap(_slots(NIGHT_2025_11_29), now_off=4.0))
        self.assertEqual(d.action, ACTION_SCHEDULE_IMPORT)
        self.assertAlmostEqual(_offset_of(d.scheduled_time), 10.0, places=3,
            msg="02:00 opens the cheapest four-slot block; 06:00 is the cheapest "
                "single slot and the block from it climbs into the morning ramp")

    def test_october_night_starts_at_the_head_of_the_trough_not_its_tail(self):
        d, _ = _plan(_snap(_slots(NIGHT_2025_10_05), now_off=4.0, dawn_off=16.0))
        self.assertEqual(d.action, ACTION_SCHEDULE_IMPORT)
        self.assertAlmostEqual(_offset_of(d.scheduled_time), 8.5, places=3,
            msg="00:30-02:00 is the cheap block; 04:30 is the cheapest slot but "
                "the three after it cost 13-17p")

    def test_chosen_start_is_the_cheapest_priced_block_by_an_independent_oracle(self):
        for night, dawn_off in ((NIGHT_2025_11_29, 17.0), (NIGHT_2025_10_05, 16.0)):
            slots = _slots(night)
            d, balance = _plan(_snap(slots, now_off=4.0, dawn_off=dawn_off))
            per_slot = 10.0 * 0.5
            need = balance.import_kwh_grid
            n = int(-(-need // per_slot))
            energies = [per_slot] * (n - 1) + [need - per_slot * (n - 1)]
            costs = {dt: _block_cost(slots, dt, energies)
                     for dt, _ in slots if _block_cost(slots, dt, energies) is not None
                     and _base() + timedelta(hours=4.0) < dt < _dawn(dawn_off)}
            best = min(costs, key=costs.get)
            self.assertEqual(d.scheduled_time, best)

    def test_the_gate_judges_the_block_mean_not_the_single_cheapest_slot(self):
        # One 20p slot at 23:00 with 28p either side of it; tomorrow's daytime mean is
        # 25p. The single slot passes the round-trip gate (20 / 0.94 = 21.3 < 25) and
        # the block it starts does not (~26 / 0.94 = 27.7 >= 25).
        dawn  = _dawn(17.0)
        slots = ([(_base() + timedelta(hours=h), 28.0) for h in (6.0, 6.5)]
                 + [(_base() + timedelta(hours=7.0), 20.0)]
                 + [(_base() + timedelta(hours=h), 28.0) for h in (7.5, 8.0, 8.5, 9.0, 9.5, 10.0)]
                 + _daytime_slots(dawn, 25.0))
        d, _ = _plan(_snap(slots, now_off=4.0))
        self.assertEqual(d.action, ACTION_SELF_CONSUMPTION)
        self.assertIn("conversion loss", d.reason)

    def test_reachability_no_longer_forces_a_dear_near_slot(self):
        # SOC 5% at 19:00 against a 10% floor: the old rule found no slot the battery
        # could "safely reach" and imported NOW at 32.5p. Letting the battery sit on
        # its floor until 02:00 costs the house ~0.3 kWh/h of grid at the evening
        # price instead of 19 kWh at it.
        dawn  = _dawn(17.0)
        slots = _slots(NIGHT_2025_11_29) + _daytime_slots(dawn, 25.0)
        d, _ = _plan(_snap(slots, now_off=3.0, soc=5.0))
        self.assertEqual(d.action, ACTION_SCHEDULE_IMPORT)
        self.assertAlmostEqual(_offset_of(d.scheduled_time), 10.0, places=3)


# ============================================================
# 2. Fallbacks — nothing imports at 10 kW without a price any more
# ============================================================

class TestAgileFallbacksHoldInsteadOfImportingBlind(unittest.TestCase):

    def test_no_rates_at_all_holds_and_flags_it(self):
        d, _ = _plan(_snap([], now_off=4.0))
        self.assertEqual(d.action, ACTION_SELF_CONSUMPTION)
        self.assertTrue(getattr(d, "import_held", False),
            "the executor pages on this flag — a silent hold is the fault the "
            "old 10 kW branch was hiding")
        self.assertEqual(d.power_watts, 0)

    def test_only_todays_tail_published_holds_rather_than_buying_unpriced_slots(self):
        # 23:35 with tomorrow's slots missing: the old planner saw no FUTURE slot
        # before dawn and started a 10 kW import into half-hours it had no price for.
        d, _ = _plan(_snap(_slots(NIGHT_2025_11_29, upto=8.0), now_off=7.58))
        self.assertNotEqual(d.action, ACTION_START_IMPORT)
        self.assertTrue(getattr(d, "import_held", False))

    def test_the_current_half_hour_is_a_candidate(self):
        # Midday plunge with tomorrow unpublished: 12:00 at -2p is the best price of
        # the day and it is happening NOW. The old planner excluded the current slot
        # (`now < dt`) and then declined 12:30 against a -2p "reference".
        dawn       = _dawn(17.0)
        today_dawn = dawn - timedelta(days=1)                               # 09:00 today
        by_time = {today_dawn + k * HALF_HOUR: 20.0 for k in range(24)}     # 09:00-21:00
        for k, p in ((6, -2.0), (7, -1.0), (8, 0.0), (9, 1.0)):            # 12:00 .. 13:30
            by_time[today_dawn + k * HALF_HOUR] = p
        slots   = sorted(by_time.items())
        now_off = _offset_of(today_dawn + 6 * HALF_HOUR + timedelta(minutes=10))   # 12:10
        d, _ = _plan(_snap(slots, now_off=now_off, today_rate_p=-2.0))
        self.assertEqual(d.action, ACTION_START_IMPORT)

    def test_unknown_tariff_holds_instead_of_importing_at_half_power(self):
        d, _ = _plan(_snap(_slots(NIGHT_2025_11_29), now_off=4.0, tariff_key="unknown"))
        self.assertEqual(d.action, ACTION_SELF_CONSUMPTION)
        self.assertTrue(getattr(d, "import_held", False))


class TestAgileReferenceRate(unittest.TestCase):
    """The passthrough comparator is tomorrow's daytime mean. Before ~16:00 that is
    unpublished, and the old fallback was the CURRENT half-hour — a peak-hour number
    that flatters any import, or a plunge number that refuses every import. Over the
    150 replayed days the current-half-hour fallback flipped the verdict in 297 of
    2,284 daytime half-hours (13%); today's daytime mean flipped none."""

    def test_falls_back_to_todays_daytime_mean_not_the_current_half_hour(self):
        dawn   = _dawn(17.0)
        tariff = TariffData(tariff_key=TARIFF_AGILE, today_rate_p=38.0,
                            agile_slots=_daytime_slots(dawn, 25.0, days_back=1))
        ref = BatteryManager._agile_daytime_reference_rate(tariff, dawn)
        self.assertAlmostEqual(ref, 25.0, places=6)

    def test_tomorrows_daytime_mean_still_wins_when_published(self):
        dawn   = _dawn(17.0)
        tariff = TariffData(tariff_key=TARIFF_AGILE, today_rate_p=38.0,
                            agile_slots=(_daytime_slots(dawn, 25.0, days_back=1)
                                         + _daytime_slots(dawn, 30.0)))
        self.assertAlmostEqual(
            BatteryManager._agile_daytime_reference_rate(tariff, dawn), 30.0, places=6)

    def test_no_slots_either_day_falls_back_to_the_headline_rate(self):
        tariff = TariffData(tariff_key=TARIFF_AGILE, today_rate_p=38.0)
        self.assertEqual(BatteryManager._agile_daytime_reference_rate(tariff, _dawn(17.0)), 38.0)


# ============================================================
# 3. octopus_api — the real path must not fail silently into "no rates"
# ============================================================

def _api():
    api = octopus_api.OctopusAPI(api_key="k", account_id="A", mpan="m", serial="s",
                                 logger=MagicMock())
    return api


class TestAgileRateFetchFailsLoudly(unittest.TestCase):

    def test_a_failed_product_probe_is_a_warning_not_a_debug_line(self):
        api = _api()
        api._api_get = MagicMock(side_effect=octopus_api.OctopusApiError("HTTP 503"))
        self.assertIsNone(api._probe_product_by_prefix(("AGILE-",)))
        self.assertTrue(api.logger.warning.called,
            "a probe failure empties the planner's slot list; DEBUG is invisible")

    def test_agile_uses_the_accounts_own_product_code(self):
        api = _api()
        api.get_current_tariff = lambda force=False: {
            "tariff_key": octopus_api.TARIFF_AGILE, "product_code": "AGILE-24-10-01",
            "tariff_code": "E-1R-AGILE-24-10-01-F"}
        api._probe_product_by_prefix = MagicMock(return_value="AGILE-99-01-01")
        self.assertEqual(api._find_product_code(octopus_api.TARIFF_AGILE), "AGILE-24-10-01")
        self.assertFalse(api._probe_product_by_prefix.called,
            "the public listing is a guess at the product; the account is the fact")

    def test_agile_still_probes_when_the_account_is_on_another_tariff(self):
        # The shadow comparison fetches Agile slots while billed on Tracker.
        api = _api()
        api.get_current_tariff = lambda force=False: {
            "tariff_key": octopus_api.TARIFF_TRACKER, "product_code": "SILVER-26-04-01"}
        api._probe_product_by_prefix = MagicMock(return_value="AGILE-24-10-01")
        self.assertEqual(api._find_product_code(octopus_api.TARIFF_AGILE), "AGILE-24-10-01")


class TestAgileSlotsSurviveAFailedRefresh(unittest.TestCase):
    """A fetch that returns nothing must not replace slots the planner already holds.
    Slots carry their own timestamps, so a stale list is self-limiting: the ones that
    have ended fall out of the planner's window on their own."""

    def setUp(self):
        self.api = _api()
        self.api._get_tracker_rates = lambda force=False: {"today_p": 21.84}
        self.api._get_tou_rates     = lambda key, force=False: {}
        self.api.get_current_tariff = lambda force=False: {"tariff_key": octopus_api.TARIFF_AGILE}
        now = datetime.now(timezone.utc)
        self.future = [(now + timedelta(hours=3), 9.0), (now + timedelta(hours=4), 12.0)]
        self.past   = [(now - timedelta(hours=3), 30.0)]

    def test_last_good_future_slots_are_kept_and_the_gap_is_warned_once(self):
        self.api.get_agile_rates = lambda target_date=None, force=False: (
            self.past + self.future if target_date == datetime.now().date() else [])
        first = self.api.get_all_monitored_rates()["agile_slots"]
        self.assertEqual(len(first), 3)

        self.api.get_agile_rates = lambda target_date=None, force=False: []
        kept = self.api.get_all_monitored_rates()["agile_slots"]
        self.assertEqual(kept, sorted(self.future),
            "the future slots from the last good fetch, and only those")
        self.assertEqual(self.api.logger.warning.call_count, 1)

        self.api.get_all_monitored_rates()
        self.assertEqual(self.api.logger.warning.call_count, 1, "latched per outage")

    def test_nothing_to_keep_stays_empty(self):
        self.api.get_agile_rates = lambda target_date=None, force=False: []
        self.assertEqual(self.api.get_all_monitored_rates()["agile_slots"], [])


# ============================================================
# 4. plugin.py executor — every exit from an import restores the charge cutoff
# ============================================================

def _p(import_active=True):
    p = plugin.Plugin.__new__(plugin.Plugin)
    p.logger      = MagicMock()
    p.debug       = False
    p._state_lock = threading.RLock()
    p.modbus      = MagicMock()
    p.modbus.connected = True
    p.modbus.night_export.return_value = True
    p.pluginPrefs = {"inverterMaxKw": "10.0", "batteryHealthCutoff": "1"}
    p.latest_inverter_data = {"batterySoc": 40.0}
    p._restore_import_cutoff = MagicMock()
    p._set_import_cutoff     = MagicMock()
    p._set_flood_prev_target = MagicMock()
    p._trigger_event         = MagicMock()
    p._send_pushover         = MagicMock()
    p.store = {
        "import_active":          import_active,
        "import_target_soc":      45.0,
        "import_scheduled_time":  None,
        "import_scheduled_logged": False,
        "export_active":          False,
        "solar_overflow_active":  False,
        "solar_overflow_charge_cap_w": 0,
        "flood_prev_target_soc":  None,
        "manager_paused":         False,
        "vpp_state":              plugin.VPP_IDLE,
        "vpp_active":             False,
    }
    return p


class TestEveryImportExitRestoresTheCutoff(unittest.TestCase):
    """The hardware charge cutoff (40047) is raised to target+3% for every grid import
    and _verify_ems_registers keeps re-asserting whatever import_charge_cutoff_pct
    says. Four paths cleared import_active WITHOUT restoring it, leaving the battery
    unable to charge above the old import target from PV for the rest of the day."""

    def test_solar_overflow_entry(self):
        p = _p()
        dec = MagicMock(action=plugin.ACTION_SOLAR_OVERFLOW, power_watts=3000, export_kw=2.0)
        p._act_on_decision(dec)
        self.assertFalse(p.store["import_active"])
        p._restore_import_cutoff.assert_called_once()

    def test_force_export_action(self):
        p = _p()
        a = MagicMock(); a.props = {"powerKw": "4.0"}
        p.actionForceExport(a)
        self.assertFalse(p.store["import_active"])
        p._restore_import_cutoff.assert_called_once()

    def test_set_self_consumption_action(self):
        p = _p()
        p.actionSetSelfConsumption(MagicMock())
        p._restore_import_cutoff.assert_called_once()

    def test_return_to_local_ems_action(self):
        p = _p()
        p.actionReturnToLocalEms(MagicMock())
        p._restore_import_cutoff.assert_called_once()

    def test_not_called_when_no_import_was_running(self):
        p = _p(import_active=False)
        p.actionSetSelfConsumption(MagicMock())
        self.assertFalse(p._restore_import_cutoff.called)


class TestScheduleIsNotArmedWhileImporting(unittest.TestCase):
    """While an import runs the planner's window excludes the slot in progress, so it
    re-emits SCHEDULE for the NEXT slot. Storing that time while importing is how a
    stale schedule fires a second import seconds after the first completes."""

    def test_schedule_while_importing_leaves_the_stored_time_alone(self):
        p = _p(import_active=True)
        dec = MagicMock(action=plugin.ACTION_SCHEDULE_IMPORT,
                        scheduled_time=datetime(2026, 12, 1, 2, 0, tzinfo=timezone.utc),
                        target_soc_pct=45.0, reason="x")
        p._act_on_decision(dec)
        self.assertIsNone(p.store["import_scheduled_time"])

    def test_schedule_still_arms_when_idle(self):
        p = _p(import_active=False)
        dec = MagicMock(action=plugin.ACTION_SCHEDULE_IMPORT,
                        scheduled_time=datetime(2026, 12, 1, 2, 0, tzinfo=timezone.utc),
                        target_soc_pct=45.0, reason="x")
        p._act_on_decision(dec)
        self.assertIsNotNone(p.store["import_scheduled_time"])


class TestImportHoldIsAudible(unittest.TestCase):
    """A hold is a decision worth a WARNING and one Pushover a day, never a silent
    self-consumption line indistinguishable from 'nothing to do'."""

    def _held(self):
        return Decision(action=ACTION_SELF_CONSUMPTION, import_held=True,
                        reason="no Agile rates to plan from", import_kwh=19.4)

    def test_warns_and_pages_once_per_day(self):
        p = _p(import_active=False)
        with patch.object(plugin, "log") as log, \
                patch.object(plugin, "_london_today", return_value=date(2026, 12, 1)):
            p._note_import_hold(self._held())
            p._note_import_hold(self._held())
        warnings = [c for c in log.call_args_list if c.kwargs.get("level") == "WARNING"]
        self.assertEqual(len(warnings), 1)
        self.assertEqual(p._send_pushover.call_count, 1)
        title, body = p._send_pushover.call_args.args[:2]
        self.assertTrue(body.isascii() and len(body) <= 1024)
        self.assertNotIn("=", body); self.assertNotIn("|", body)

    def test_a_new_day_pages_again(self):
        p = _p(import_active=False)
        with patch.object(plugin, "log"), \
                patch.object(plugin, "_london_today", return_value=date(2026, 12, 1)):
            p._note_import_hold(self._held())
        with patch.object(plugin, "log"), \
                patch.object(plugin, "_london_today", return_value=date(2026, 12, 2)):
            p._note_import_hold(self._held())
        self.assertEqual(p._send_pushover.call_count, 2)

    def test_an_ordinary_decision_is_silent(self):
        p = _p(import_active=False)
        with patch.object(plugin, "log") as log:
            p._note_import_hold(Decision(action=ACTION_SELF_CONSUMPTION))
        self.assertFalse(log.called)
        self.assertFalse(p._send_pushover.called)


class TestHoldReachesTheExecutor(unittest.TestCase):
    """A flag is only a guard if evaluate() carries it out and the tick reads it —
    the two pure functions above were thoroughly tested and the wiring was not."""

    def test_evaluate_returns_the_held_decision_with_the_deficit_on_it(self):
        d = BatteryManager().evaluate(_snap([], now_off=4.0))
        self.assertEqual(d.action, ACTION_SELF_CONSUMPTION)
        self.assertTrue(d.import_held)
        self.assertGreater(d.import_kwh, 0.0, "the Pushover quotes this figure")
        self.assertIn("HELD", d.reason)

    def test_the_manager_tick_calls_the_notice_after_logging_the_decision(self):
        import ast
        tree = ast.parse(open(plugin.__file__, encoding="utf-8").read())
        impl = next(n for n in ast.walk(tree)
                    if isinstance(n, ast.FunctionDef) and n.name == "_evaluate_manager_impl")
        calls = [n.func.attr for n in ast.walk(impl)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)]
        self.assertIn("_note_import_hold", calls)
        self.assertGreater(calls.index("_note_import_hold"), calls.index("_log_manager_decision"))


# ============================================================
# 5. The power-cut reserve on Agile (v5.101.0)
# ============================================================

def _gate_declining_slots():
    """Overnight prices that fail the round-trip gate against tomorrow's 25p daytime
    mean whatever the block: a flat 28p, 29.8p after the 6% loss. A first version
    left one 20p half-hour in, and a one- or two-slot reserve block from it passed
    the gate on its own — so a mutation that gated the reserve survived. The
    fixture has to carry a value that would move the answer."""
    dawn = _dawn(17.0)
    return ([(_base() + timedelta(hours=h), 28.0)
             for h in (6.0, 6.5, 7.0, 7.5, 8.0, 8.5, 9.0, 9.5, 10.0)]
            + _daytime_slots(dawn, 25.0))


def _reserve_oracle(snap, slots, dawn_off, reserve_pct):
    """Independent oracle for the reserve top-up: the grid-side shortfall to hold
    reserve_pct at dawn, and the cheapest priced block start for it."""
    bm = BatteryManager()
    balance = bm._calculate_24h_balance(snap)
    short = reserve_pct / 100.0 * CAPACITY_KWH - balance.battery_at_dawn_kwh
    need  = short / EFFICIENCY
    per   = 5.0
    n     = int(-(-need // per))
    energies = [per] * (n - 1) + [need - per * (n - 1)]
    costs = {dt: _block_cost(slots, dt, energies)
             for dt, _ in slots if _block_cost(slots, dt, energies) is not None
             and snap.now < dt < _dawn(dawn_off)}
    return short, min(costs, key=costs.get)


class TestAgileReserveIsBoughtInTheCheapestBlock(unittest.TestCase):
    """On Tracker the resilience buffer buys the reserve whenever SOC is below it at
    night; on Go/Flux inside the cheap window; on Agile it returned None, so the
    winter 20% buffer did nothing from 1 October. Now it buys what the battery
    needs to still hold the reserve AT DAWN, in the cheapest priced block, gated on
    nothing but the price shape — resilience is not arbitrage."""

    def test_reserve_short_and_tomorrow_covered_schedules_the_cheapest_block(self):
        slots = _slots(NIGHT_2025_11_29)
        snap  = _snap(slots, now_off=4.0, soc=40.0, tomorrow_kwh=40.0, dawn_target=50.0)
        d = BatteryManager()._check_resilience_buffer(
            snap, BatteryManager()._calculate_24h_balance(snap))
        self.assertIsNotNone(d, "Agile used to return None here")
        self.assertEqual(d.action, ACTION_SCHEDULE_IMPORT)
        short, best = _reserve_oracle(snap, slots, 17.0, 50.0)
        self.assertEqual(d.scheduled_time, best)
        self.assertNotAlmostEqual(_offset_of(d.scheduled_time), 14.0, places=1,
            msg="06:00 is the single cheapest slot; a three-slot block from it climbs")

    def test_top_up_is_sized_to_hold_the_reserve_at_dawn_not_now(self):
        slots = _slots(NIGHT_2025_11_29)
        snap  = _snap(slots, now_off=4.0, soc=40.0, tomorrow_kwh=40.0, dawn_target=50.0)
        d = BatteryManager()._check_resilience_buffer(
            snap, BatteryManager()._calculate_24h_balance(snap))
        short, _ = _reserve_oracle(snap, slots, 17.0, 50.0)
        expected = (0.40 * CAPACITY_KWH + short) / CAPACITY_KWH * 100.0 + 2.0
        self.assertAlmostEqual(d.target_soc_pct, expected, places=1)
        self.assertGreater(d.target_soc_pct, 52.0,
            "the flat-tariff rule tops up to the floor plus 2 NOW; on Agile the "
            "overnight drain after the block has to be bought as well")
        self.assertAlmostEqual(d.import_kwh, round(short / EFFICIENCY, 2), places=2,
            msg="the block is sized on the GRID side of the conversion loss")

    def test_reserve_is_not_gated_on_the_round_trip_comparison(self):
        snap = _snap(_gate_declining_slots(), now_off=4.0, soc=40.0, tomorrow_kwh=40.0,
                     dawn_target=30.0)
        d = BatteryManager()._check_resilience_buffer(
            snap, BatteryManager()._calculate_24h_balance(snap))
        self.assertIsNotNone(d)
        self.assertEqual(d.action, ACTION_SCHEDULE_IMPORT)
        self.assertEqual(getattr(d, "import_purpose", ""), "reserve")

    def test_reserve_short_with_no_rates_holds_and_says_so(self):
        snap = _snap([], now_off=4.0, soc=40.0, tomorrow_kwh=40.0, dawn_target=20.0)
        d = BatteryManager()._check_resilience_buffer(
            snap, BatteryManager()._calculate_24h_balance(snap))
        self.assertIsNotNone(d)
        self.assertEqual(d.action, ACTION_SELF_CONSUMPTION)
        self.assertTrue(getattr(d, "import_held", False))
        self.assertEqual(getattr(d, "import_purpose", ""), "reserve")

    def test_reserve_already_held_at_dawn_does_nothing(self):
        snap = _snap(_slots(NIGHT_2025_11_29), now_off=4.0, soc=80.0, tomorrow_kwh=40.0,
                     dawn_target=20.0)
        self.assertIsNone(BatteryManager()._check_resilience_buffer(
            snap, BatteryManager()._calculate_24h_balance(snap)))

    def test_a_shortfall_under_the_minimum_import_does_nothing(self):
        # 40% now, 10.2 kWh drain -> 3.8 kWh at dawn; an 11% reserve is 3.85 kWh, so the
        # gap is 0.05 kWh, well under MIN_IMPORT_KWH.
        snap = _snap(_slots(NIGHT_2025_11_29), now_off=4.0, soc=40.0, tomorrow_kwh=40.0,
                     dawn_target=11.0)
        self.assertIsNone(BatteryManager()._check_resilience_buffer(
            snap, BatteryManager()._calculate_24h_balance(snap)))

    def test_daytime_is_left_to_the_sun_on_agile_too(self):
        from battery_manager import _to_london, SOLAR_DUSK_THRESHOLD_WH
        slots = _slots(NIGHT_2025_11_29)
        snap  = _snap(slots, now_off=-4.0, soc=20.0, tomorrow_kwh=40.0, dawn_target=50.0)  # 12:00
        today = _to_london(snap.now).date().strftime("%Y-%m-%d")
        # A DULL day: just enough forecast to count as daytime, not enough to lift
        # the dawn projection — so the reserve IS short, and only the daytime gate
        # stops it being planned. A sunny fixture had no shortfall to plan, and a
        # mutant that planned in daylight survived it.
        snap.forecast_p50 = {f"{today} {h:02d}:00:00": SOLAR_DUSK_THRESHOLD_WH + 100
                             for h in range(7, 20)}
        snap.dawn_times   = dict(snap.dawn_times, **{today: _now(hour=7)})
        balance = BatteryManager()._calculate_24h_balance(snap)
        self.assertTrue(balance.is_daytime, "fixture must be daytime for this to mean anything")
        self.assertGreater(BatteryManager._agile_reserve_shortfall_kwh(snap, balance), 1.0,
            "fixture must leave the reserve short, or the gate is untested")
        self.assertIsNone(BatteryManager()._check_resilience_buffer(snap, balance))


class TestAgileDeficitImportCarriesTheReserve(unittest.TestCase):
    """When tomorrow needs an import as well, the reserve rides in the same block —
    the bigger of the two shortfalls, one charge — and if the round-trip gate
    declines the deficit, the reserve is bought on its own regardless."""

    def test_import_target_is_the_larger_of_deficit_and_reserve(self):
        slots = _slots(NIGHT_2025_11_29)
        snap  = _snap(slots, now_off=4.0, soc=40.0, tomorrow_kwh=15.0, dawn_target=50.0)
        d, balance = _plan(snap)
        self.assertLess(balance.import_kwh, 5.0, "fixture: a small deficit ...")
        self.assertEqual(d.action, ACTION_SCHEDULE_IMPORT)
        short, best = _reserve_oracle(snap, slots, 17.0, 50.0)
        self.assertGreater(short, balance.import_kwh, "... and a bigger reserve shortfall")
        expected = (0.40 * CAPACITY_KWH + short) / CAPACITY_KWH * 100.0 + 2.0
        self.assertAlmostEqual(d.target_soc_pct, expected, places=1)
        self.assertEqual(d.scheduled_time, best)

    def test_gate_declines_the_deficit_but_the_reserve_is_still_bought(self):
        snap = _snap(_gate_declining_slots(), now_off=4.0, soc=40.0, tomorrow_kwh=0.0,
                     dawn_target=20.0)
        d, balance = _plan(snap)
        self.assertGreater(balance.import_kwh, 15.0, "fixture: a real deficit")
        self.assertEqual(d.action, ACTION_SCHEDULE_IMPORT)
        self.assertEqual(getattr(d, "import_purpose", ""), "reserve")
        short = 0.20 * CAPACITY_KWH - balance.battery_at_dawn_kwh
        expected = (0.40 * CAPACITY_KWH + short) / CAPACITY_KWH * 100.0 + 2.0
        self.assertAlmostEqual(d.target_soc_pct, expected, places=1,
            msg="the reserve alone, not the deficit the gate turned down")

    def test_a_deficit_night_leaves_the_reserve_to_the_import_branch(self):
        slots = _slots(NIGHT_2025_11_29)
        snap  = _snap(slots, now_off=4.0, soc=40.0, tomorrow_kwh=15.0, dawn_target=50.0)
        d = BatteryManager().evaluate(snap)
        self.assertEqual(d.action, ACTION_SCHEDULE_IMPORT)
        tags = {tag: msg for tag, msg in d.audit_trail}
        self.assertTrue(tags["RESILIENCE"].startswith("skipped"),
            "two branches must not both plan the same block")
        self.assertTrue(tags["IMPORT"].startswith("matched"))
        short, best = _reserve_oracle(snap, slots, 17.0, 50.0)
        self.assertAlmostEqual(d.target_soc_pct,
                               (0.40 * CAPACITY_KWH + short) / CAPACITY_KWH * 100.0 + 2.0, places=1)

    def test_evaluate_fires_the_reserve_at_priority_two(self):
        snap = _snap(_slots(NIGHT_2025_11_29), now_off=4.0, soc=40.0, tomorrow_kwh=40.0,
                     dawn_target=20.0)
        d = BatteryManager().evaluate(snap)
        self.assertEqual(d.action, ACTION_SCHEDULE_IMPORT)
        self.assertTrue(any(tag == "RESILIENCE" and msg.startswith("matched")
                            for tag, msg in d.audit_trail))


class TestReserveHoldNoticeSpeaksOfTheReserve(unittest.TestCase):

    def test_body_names_the_reserve_not_tomorrows_need(self):
        p = _p(import_active=False)
        held = Decision(action=ACTION_SELF_CONSUMPTION, import_held=True,
                        import_purpose="reserve", import_held_why="no Agile rates to plan from",
                        import_kwh=3.4, reason="Reserve short")
        with patch.object(plugin, "log"), \
                patch.object(plugin, "_london_today", return_value=date(2026, 12, 1)):
            p._note_import_hold(held)
        title, body = p._send_pushover.call_args.args[:2]
        self.assertIn("reserve", body.lower())
        self.assertNotIn("Tomorrow needs", body)
        self.assertTrue(body.isascii() and len(body) <= 1024)
        self.assertNotIn("=", body); self.assertNotIn("|", body)


if __name__ == "__main__":
    unittest.main()
