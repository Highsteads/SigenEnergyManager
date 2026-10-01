#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    test_cheap_window_minimum.py
# Description: The 50% overnight minimum holds whoever runs the 02:00-05:00 window
#              (SigenEnergyManager 5.130.0, flux_strategy 2.10, battery_manager 3.15).
#              Built from 1-Oct-2026: Octopus published October's Flux import prices
#              but no export price after midnight, Flux deferred all night, and the
#              manager bought to 44%, then ran the house on the battery to 5am.
# Author:      CliveS & Claude Opus 5.5
# Date:        01-10-2026
# Version:     1.0

import json
import os
import sys
import tempfile
import types
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

_ind = types.ModuleType("indigo")


class _PB:
    class StopThread(Exception):
        pass

    def __init__(self, *a, **k):
        pass


_ind.PluginBase = _PB
_ind.Dict = dict
_ind.List = list
for _a in ("kStateImageSel", "server", "devices", "variables", "variable",
           "kDeviceAction", "activePlugin", "trigger"):
    setattr(_ind, _a, MagicMock())
sys.modules.setdefault("indigo", _ind)

_pm = MagicMock()
sys.modules.setdefault("pymodbus", _pm)
sys.modules.setdefault("pymodbus.client", _pm.client)
sys.modules.setdefault("pymodbus.exceptions", _pm.exceptions)
sys.modules.setdefault("requests", MagicMock())

import plugin                     # noqa: E402
import flux_strategy as fs        # noqa: E402
from battery_manager import (     # noqa: E402
    BatteryManager, TariffData, ACTION_START_IMPORT, ACTION_SELF_CONSUMPTION,
    CHEAP_WINDOW_PURPOSE,
)
from test_tou_peak_topup import _snap          # noqa: E402

LONDON = ZoneInfo("Europe/London")
UTC    = timezone.utc


def _local(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=LONDON).astimezone(UTC)


def _span(a, b, p):
    return fs.RateSpan(start=datetime.fromisoformat(a.replace("Z", "+00:00")),
                       end=datetime.fromisoformat(b.replace("Z", "+00:00")), p=p)


# What Octopus actually published for region F, read 1-Oct-2026 13:39 BST. The
# export schedule stops at 2026-09-30T23:00Z, i.e. midnight BST, in every region.
IMPORT_PUBLISHED = [
    _span("2026-09-29T18:00:00Z", "2026-09-30T01:00:00Z", 24.35433),
    _span("2026-09-30T01:00:00Z", "2026-09-30T04:00:00Z", 14.618415),
    _span("2026-09-30T04:00:00Z", "2026-09-30T15:00:00Z", 24.35433),
    _span("2026-09-30T15:00:00Z", "2026-09-30T18:00:00Z", 34.099905),
    _span("2026-09-30T18:00:00Z", "2026-10-01T01:00:00Z", 23.1946),
    _span("2026-10-01T01:00:00Z", "2026-10-01T04:00:00Z", 13.9223),
    _span("2026-10-01T04:00:00Z", "2026-10-01T15:00:00Z", 23.1946),
    _span("2026-10-01T15:00:00Z", "2026-10-01T18:00:00Z", 32.4761),
    _span("2026-10-01T18:00:00Z", "2026-10-02T01:00:00Z", 23.1946),
    _span("2026-10-02T01:00:00Z", "2026-10-02T04:00:00Z", 13.9223),
    _span("2026-10-02T04:00:00Z", "2026-10-02T15:00:00Z", 23.1946),
    _span("2026-10-02T15:00:00Z", "2026-10-02T18:00:00Z", 32.4761),
    _span("2026-10-02T18:00:00Z", "2026-10-03T01:00:00Z", 23.1946),
]
EXPORT_PUBLISHED = [
    _span("2026-09-29T18:00:00Z", "2026-09-30T01:00:00Z", 9.7084),
    _span("2026-09-30T01:00:00Z", "2026-09-30T04:00:00Z", 4.2064),
    _span("2026-09-30T04:00:00Z", "2026-09-30T15:00:00Z", 9.7084),
    _span("2026-09-30T15:00:00Z", "2026-09-30T18:00:00Z", 27.6905),
    _span("2026-09-30T18:00:00Z", "2026-09-30T23:00:00Z", 9.7084),
]


def _export_published_later():
    """The same export SHAPE carried into 1-2 October — a test fixture standing in
    for a schedule Octopus has not published, never a price the plugin may use."""
    out = list(EXPORT_PUBLISHED[:-1])
    out.append(_span("2026-09-30T18:00:00Z", "2026-10-01T01:00:00Z", 9.7084))
    for day in ("2026-10-01", "2026-10-02"):
        nxt = "2026-10-02" if day == "2026-10-01" else "2026-10-03"
        out += [_span(f"{day}T01:00:00Z", f"{day}T04:00:00Z", 4.2064),
                _span(f"{day}T04:00:00Z", f"{day}T15:00:00Z", 9.7084),
                _span(f"{day}T15:00:00Z", f"{day}T18:00:00Z", 27.6905),
                _span(f"{day}T18:00:00Z", f"{nxt}T01:00:00Z", 9.7084)]
    return out


def _site(**over):
    base = dict(capacity_kwh=35.04, one_way_efficiency=0.97, charge_power_w=10000,
                discharge_power_w=10000, export_limit_w=4000, import_limit_w=16000,
                import_limit_verified=True, reserve_pct=20.0, max_charge_soc_pct=100.0,
                wear_p_per_kwh=2.0)
    fields = set(fs.FluxSite.__dataclass_fields__)
    return fs.FluxSite(**{k: v for k, v in {**base, **over}.items() if k in fields})


def _free_hour(day_local, h0, h1):
    return fs.EventCommitment(source="octopus", kind="import",
                              start=_local(*day_local, h0), end=_local(*day_local, h1),
                              energy_kwh=10.0 * (h1 - h0), event_id=f"hh-{h0}")


# ============================================================================
# The midnight rollover — why Flux deferred
# ============================================================================

class TestTheMidnightRollover(unittest.TestCase):

    def test_the_published_rates_at_midnight_are_refused(self):
        now = _local(2026, 10, 1, 0, 0) + timedelta(seconds=4)
        self.assertIsNone(fs.derive_bands(IMPORT_PUBLISHED, EXPORT_PUBLISHED, LONDON, now))

    def test_the_reason_names_the_missing_export_side_and_when(self):
        now = _local(2026, 10, 1, 3, 48)
        why = fs.bands_problem(IMPORT_PUBLISHED, EXPORT_PUBLISHED, LONDON, now)
        self.assertIn("export", why)
        self.assertIn("00:00 on 1 October", why)
        self.assertNotIn("import prices beyond", why)

    def test_the_planner_defers_with_that_reason(self):
        now = _local(2026, 10, 1, 3, 48)
        why = fs.bands_problem(IMPORT_PUBLISHED, EXPORT_PUBLISHED, LONDON, now)
        inputs = fs.FluxInputs(now=now, local_tz=LONDON, bands=None, site=_site(),
                               soc_pct=44.9, house=None, pv=None, tariff_verified=True,
                               commissioned=True, enabled=True, bands_problem=why)
        d = fs.plan(inputs)
        self.assertTrue(d.deferred)
        self.assertIn("00:00 on 1 October", d.reason)

    def test_once_export_is_published_the_new_import_prices_are_read(self):
        now = _local(2026, 10, 1, 3, 48)
        bands = fs.derive_bands(IMPORT_PUBLISHED, _export_published_later(), LONDON, now)
        self.assertIsNotNone(bands)
        self.assertAlmostEqual(bands.import_cheap_p, 13.9223)
        self.assertAlmostEqual(bands.import_peak_p, 32.4761)
        self.assertEqual(fs.bands_problem(IMPORT_PUBLISHED, _export_published_later(),
                                          LONDON, now), "")

    def test_a_missing_import_side_is_named_as_import(self):
        now = _local(2026, 10, 3, 3, 0)
        why = fs.bands_problem(IMPORT_PUBLISHED, _export_published_later(), LONDON, now)
        self.assertIn("import", why)


# ============================================================================
# The rule's one owner
# ============================================================================

class TestTheMinimumHasOneOwner(unittest.TestCase):

    def test_fifty_inside_the_window(self):
        self.assertEqual(fs.cheap_window_minimum_pct(
            _site(), (), _local(2026, 10, 1, 3, 0), LONDON), 50.0)

    def test_none_outside_the_window(self):
        for h in (1, 5, 12, 23):
            self.assertEqual(fs.cheap_window_minimum_pct(
                _site(), (), _local(2026, 10, 1, h, 30 if h == 1 else 0), LONDON), 0.0, h)

    def test_none_on_a_day_with_a_booked_free_hour(self):
        free = (_free_hour((2026, 10, 4), 13, 15),)
        self.assertEqual(fs.cheap_window_minimum_pct(
            _site(), free, _local(2026, 10, 4, 3, 0), LONDON), 0.0)

    def test_a_free_hour_on_another_day_does_not_count(self):
        free = (_free_hour((2026, 10, 5), 13, 15),)
        self.assertEqual(fs.cheap_window_minimum_pct(
            _site(), free, _local(2026, 10, 4, 3, 0), LONDON), 50.0)

    def test_never_above_the_site_ceiling(self):
        self.assertEqual(fs.cheap_window_minimum_pct(
            _site(max_charge_soc_pct=40.0), (), _local(2026, 10, 1, 3, 0), LONDON), 40.0)


# ============================================================================
# The late-window sizing, and an honest shortfall
# ============================================================================

def _inputs_at(hh, mm, soc, site=None):
    return types.SimpleNamespace(now=_local(2026, 10, 1, hh, mm), soc_pct=soc,
                                 site=site or _site(), flows=None)


class TestTheMinimumIsSizedOnTheRealTimeLeft(unittest.TestCase):

    def _min(self, hh, mm, soc, site=None, headroom=None):
        inputs = _inputs_at(hh, mm, soc, site)
        window_end = _local(2026, 10, 1, 5, 0)
        with patch.object(fs, "import_headroom_w",
                          return_value=headroom if headroom is not None
                          else (site or _site()).import_limit_w):
            return fs._minimum_charge(inputs, window_end, None, 0, 0.0, 50.0)

    def test_a_restart_at_four_fifty_five_asks_for_enough(self):
        # 49% -> 50% is 0.3504 kWh in five minutes. v2.9 asked for 1,445 W, which
        # delivers about 0.117 kWh, a third of it.
        target, power, buy, raised, short = self._min(4, 55, 49.0)
        need = 0.01 * 35.04
        delivered = power / 1000.0 * (5 / 60) * 0.97
        self.assertEqual(target, 50.0)
        self.assertGreaterEqual(delivered, need - 1e-6)
        self.assertGreater(power, 1445 * 2)
        self.assertEqual(short, 0.0)

    def test_limited_charge_rate_reports_the_shortfall(self):
        # 30% -> 50% is 7 kWh; at 1 kW for an hour the battery gets about 0.97 kWh.
        target, power, buy, raised, short = self._min(4, 0, 30.0,
                                                      site=_site(charge_power_w=1000))
        self.assertEqual(power, 1000)
        self.assertAlmostEqual(short, 0.2 * 35.04 - 0.97, places=1)

    def test_no_import_headroom_reports_the_whole_need(self):
        target, power, buy, raised, short = self._min(3, 0, 40.0, headroom=0)
        self.assertIsNone(target)
        self.assertAlmostEqual(short, 0.1 * 35.04, places=2)

    def test_an_early_start_has_no_shortfall(self):
        *_rest, short = self._min(2, 0, 20.0)
        self.assertEqual(short, 0.0)


# ============================================================================
# The manager's fallback (battery_manager 3.15)
# ============================================================================

class TestTheManagerKeepsTheMinimumWhenFluxCannot(unittest.TestCase):

    def setUp(self):
        self.bm = BatteryManager()

    def _eval(self, soc, hh, mm=0, minimum=50.0, **kw):
        return self.bm.evaluate(replace(_snap(soc, hh, mm, **kw),
                                        cheap_window_min_pct=minimum))

    def test_1_october_at_03_38_charges_on_to_fifty(self):
        d = self._eval(40.8, 3, 38)
        self.assertEqual(d.action, ACTION_START_IMPORT)
        self.assertEqual(d.import_purpose, CHEAP_WINDOW_PURPOSE)
        self.assertGreaterEqual(d.target_soc_pct, 50.0)
        self.assertIn("50% minimum", d.reason)

    def test_above_the_minimum_it_holds_rather_than_discharging(self):
        d = self._eval(55.0, 4, 30)
        self.assertEqual(d.action, ACTION_START_IMPORT)
        self.assertEqual(d.import_purpose, CHEAP_WINDOW_PURPOSE)
        self.assertLessEqual(d.target_soc_pct, 55.0)
        self.assertIn("holding the battery at 55%", d.reason)

    def test_tomorrow_wins_when_it_needs_more(self):
        d = self._eval(12.0, 3, 0, minimum=20.0)
        self.assertGreater(d.target_soc_pct, 20.0)
        self.assertIn("for tomorrow", d.reason)
        self.assertNotIn("minimum", d.reason)

    def test_a_free_hour_day_has_no_minimum_but_still_holds(self):
        d = self._eval(70.0, 3, 0, minimum=0.0)
        self.assertEqual(d.import_purpose, CHEAP_WINDOW_PURPOSE)
        self.assertLessEqual(d.target_soc_pct, 70.0)
        self.assertNotIn("minimum", d.reason)

    def test_flux_running_the_window_keeps_the_manager_out(self):
        d = self._eval(40.8, 3, 38, flux_owns=True)
        self.assertNotEqual(d.import_purpose, CHEAP_WINDOW_PURPOSE)

    def test_five_am_ends_the_fallback(self):
        d = self._eval(48.0, 5, 0)
        self.assertNotEqual(d.import_purpose, CHEAP_WINDOW_PURPOSE)

    def test_other_tariffs_are_untouched(self):
        go = TariffData(tariff_key="go", today_rate_p=8.5, cheap_start="00:30",
                        cheap_end="05:30", cheap_rate_p=8.5, day_rate_p=25.0)
        d = self._eval(40.0, 3, 0, tariff=go, reserve=0.0)
        self.assertNotEqual(d.import_purpose, CHEAP_WINDOW_PURPOSE)

    def test_the_branch_is_in_the_audit(self):
        d = self._eval(40.8, 3, 38)
        self.assertIn("CHEAP-WINDOW", [tag for tag, _ in d.audit_trail])


# ============================================================================
# The plugin's side: drive, hold, end, hand to Flux, report
# ============================================================================

def _mk(soc=40.8, prefs=None):
    p = plugin.Plugin.__new__(plugin.Plugin)
    p.logger      = MagicMock()
    p.debug       = False
    p.pluginPrefs = {"inverterMaxKw": "10", "batteryCapacityKwh": "35.04",
                     "fluxReservePct": "20", "batteryHealthCutoff": "1.0"}
    p.pluginPrefs.update(prefs or {})
    p.store = {"import_active": False, "export_active": False, "import_target_soc": 0.0,
               "cheap_window_import_active": False, "happy_hour_import_active": False,
               "import_charge_cutoff_pct": None, "vpp_state": plugin.VPP_IDLE,
               "flood_prev_target_soc": None, "storm_level": "none"}
    p.latest_inverter_data = {"batterySoc": soc, "emsWorkMode": "Max Self Consumption"}
    p.latest_rates_data = {"tariff_info": {"tariff_key": plugin.TARIFF_FLUX}}
    p.modbus = MagicMock()
    p.modbus.connected = True
    p.modbus.force_charge.return_value = True
    p.modbus.set_charge_cutoff.return_value = True
    p.modbus.set_self_consumption.return_value = True
    p._save_accumulators = MagicMock()
    p._trigger_event = MagicMock()
    p.flux_executor = None
    return p


def _decision(target, purpose=CHEAP_WINDOW_PURPOSE, action=ACTION_START_IMPORT):
    d = MagicMock()
    d.action = action
    d.target_soc_pct = target
    d.power_watts = 10000
    d.import_purpose = purpose
    d.reason = "Flux is not running the cheap window: test"
    return d


class TestThePluginRunsTheWindowToFiveAm(unittest.TestCase):

    def test_it_charges_to_the_minimum_with_the_house_on_the_grid(self):
        p = _mk(soc=40.8)
        with patch.object(p, "_in_flux_cheap_window", return_value=True):
            p._act_on_decision(_decision(50.0))
        p.modbus.force_charge.assert_called_once()
        self.assertEqual(p.modbus.force_charge.call_args.kwargs["cutoff_soc"], 50.0)
        p.modbus.set_discharge_limit.assert_called_with(0)
        self.assertTrue(p.store["cheap_window_import_active"])
        self.assertEqual(p.store["import_target_soc"], 50.0)

    def test_it_does_not_stop_at_the_target_inside_the_window(self):
        p = _mk(soc=40.8)
        with patch.object(p, "_in_flux_cheap_window", return_value=True):
            p._act_on_decision(_decision(50.0))
            p.latest_inverter_data["batterySoc"] = 50.4
            p.modbus.set_self_consumption.reset_mock()
            p._act_on_decision(_decision(50.0))
        p.modbus.set_self_consumption.assert_not_called()
        self.assertTrue(p.store["import_active"])

    def test_already_above_it_holds_where_it_is(self):
        p = _mk(soc=57.0)
        with patch.object(p, "_in_flux_cheap_window", return_value=True):
            p._act_on_decision(_decision(50.0))
        self.assertEqual(p.modbus.force_charge.call_args.kwargs["cutoff_soc"], 57.0)

    def test_five_am_ends_it_even_below_the_target(self):
        p = _mk(soc=40.8)
        with patch.object(p, "_in_flux_cheap_window", return_value=True):
            p._act_on_decision(_decision(50.0))
        p.latest_inverter_data["batterySoc"] = 47.0
        logged = []
        with patch.object(p, "_in_flux_cheap_window", return_value=False), \
             patch.object(plugin, "log", side_effect=lambda m, level="INFO": logged.append(m)):
            p._act_on_decision(_decision(0.0, purpose="tomorrow",
                                         action=ACTION_SELF_CONSUMPTION))
        # ONE hand-back, said once: not a second "Import complete" from the
        # ordinary import teardown on the same tick.
        p.modbus.set_self_consumption.assert_called_once()
        self.assertFalse(any("Import complete" in m for m in logged), logged)
        self.assertTrue(any("Cheap-window charge ended (the cheap window has closed)" in m
                            for m in logged), logged)
        self.assertFalse(p.store["import_active"])
        self.assertFalse(p.store["cheap_window_import_active"])

    def test_the_level_only_rises(self):
        p = _mk(soc=40.8)
        with patch.object(p, "_in_flux_cheap_window", return_value=True):
            p._act_on_decision(_decision(50.0))
            p.modbus.set_charge_cutoff.reset_mock()
            p._act_on_decision(_decision(45.0))
            p.modbus.set_charge_cutoff.assert_not_called()
            p._act_on_decision(_decision(62.0))
        p.modbus.set_charge_cutoff.assert_called_with(62.0)
        self.assertEqual(p.store["import_target_soc"], 62.0)


class TestFluxCanTakeTheWindowBack(unittest.TestCase):

    def test_charging_to_the_minimum_stands_flux_aside(self):
        p = _mk(soc=40.8)
        p.store.update(import_active=True, cheap_window_import_active=True,
                       import_target_soc=50.0)
        with patch.object(p, "_grid_outage_active", return_value=False), \
             patch.object(p, "_happy_hour_window", return_value=None):
            self.assertEqual(p._flux_other_owner(), "the manager has a grid import in flight")

    def test_holding_at_the_minimum_does_not(self):
        p = _mk(soc=50.2)
        p.store.update(import_active=True, cheap_window_import_active=True,
                       import_target_soc=50.0)
        with patch.object(p, "_grid_outage_active", return_value=False), \
             patch.object(p, "_happy_hour_window", return_value=None), \
             patch.object(p, "_flux_peak_now", return_value=False):
            self.assertNotEqual(p._flux_other_owner(),
                                "the manager has a grid import in flight")

    def test_an_ordinary_import_still_stands_flux_aside_at_its_target(self):
        p = _mk(soc=50.2)
        p.store.update(import_active=True, import_target_soc=50.0)
        with patch.object(p, "_grid_outage_active", return_value=False), \
             patch.object(p, "_happy_hour_window", return_value=None):
            self.assertEqual(p._flux_other_owner(), "the manager has a grid import in flight")

    def test_a_plan_flux_may_not_yet_apply_does_not_own_the_window(self):
        p = _mk(soc=50.2)
        p.store["flux_decision"] = MagicMock(mode=fs.MODE_HOLD)
        with patch.object(p, "_flux_armed", return_value=True), \
             patch.object(p, "_flux_other_owner", return_value=""), \
             patch.object(p, "_flux_owns_control", return_value=False), \
             patch.object(p, "_flux_may_claim", return_value=False), \
             patch.object(plugin._flux_strategy, "in_window", return_value=True):
            self.assertFalse(p._flux_owns_cheap_window())
        with patch.object(p, "_flux_armed", return_value=True), \
             patch.object(p, "_flux_other_owner", return_value=""), \
             patch.object(p, "_flux_owns_control", return_value=False), \
             patch.object(p, "_flux_may_claim", return_value=True), \
             patch.object(plugin._flux_strategy, "in_window", return_value=True):
            self.assertTrue(p._flux_owns_cheap_window())

    def test_taking_over_drops_the_bookkeeping_without_writing(self):
        p = _mk(soc=50.2)
        p.store.update(import_active=True, cheap_window_import_active=True,
                       import_target_soc=50.0, import_charge_cutoff_pct=50.0)
        p.modbus.reset_mock()
        p._forget_cheap_window_import("test")
        self.assertFalse(p.store["import_active"])
        self.assertIsNone(p.store["import_charge_cutoff_pct"])
        self.assertEqual(p.modbus.method_calls, [])
        self.assertEqual(p._flux_baseline()["baseline_charge_cutoff_pct"], 100.0)


class TestTheMinimumComesFromTheSharedRule(unittest.TestCase):

    def _at(self, p, when, commitments=()):
        class _DT(datetime):
            @classmethod
            def now(cls, tz=None):
                return when
        with patch.object(plugin, "datetime", _DT), \
             patch.object(p, "_flux_commitments", return_value=commitments), \
             patch.object(p, "_flux_site", return_value=_site()):
            return p._cheap_window_minimum_pct()

    def test_fifty_on_flux_in_the_window(self):
        self.assertEqual(self._at(_mk(), _local(2026, 10, 1, 3, 0)), 50.0)

    def test_none_with_a_free_hour_booked(self):
        p = _mk()
        self.assertEqual(self._at(p, _local(2026, 10, 4, 3, 0),
                                  (_free_hour((2026, 10, 4), 13, 15),)), 0.0)

    def test_none_off_flux(self):
        p = _mk()
        p.latest_rates_data = {"tariff_info": {"tariff_key": "go"}}
        self.assertEqual(self._at(p, _local(2026, 10, 1, 3, 0)), 0.0)


class TestTheMorningSaysWhatWasAchieved(unittest.TestCase):

    def test_a_missed_minimum_is_a_warning_and_a_record(self):
        p = _mk(soc=44.9)
        with tempfile.TemporaryDirectory() as tmp:
            p.data_dir = tmp
            logged = []
            with patch.object(plugin, "log", side_effect=lambda m, level="INFO":
                              logged.append((level, m))), \
                 patch.object(p, "_flux_owns_control", return_value=False):
                with patch.object(p, "_in_flux_cheap_window", return_value=True), \
                     patch.object(p, "_cheap_window_minimum_pct", return_value=50.0):
                    p.store["flux_status"] = "deferred: no export price"
                    p._note_cheap_window_result()
                p.latest_inverter_data["batterySoc"] = 42.0

                class _DT(datetime):
                    @classmethod
                    def now(cls, tz=None):
                        return _local(2026, 10, 1, 5, 1)
                rec = p.store["cheap_window_watch"]
                rec["day"] = "2026-10-01"
                with patch.object(plugin, "datetime", _DT), \
                     patch.object(p, "_in_flux_cheap_window", return_value=False):
                    p._note_cheap_window_result()
                    p._note_cheap_window_result()          # once a day only
            with open(os.path.join(tmp, "cheap_window_results.jsonl"),
                      encoding="utf-8") as fh:
                rows = [json.loads(line) for line in fh]
        warnings = [m for lvl, m in logged if lvl == "WARNING"]
        self.assertEqual(len(warnings), 1)
        self.assertIn("BELOW the 50% minimum", warnings[0])
        self.assertIn("no export price", warnings[0])
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]["met"])
        self.assertEqual(rows[0]["soc_at_close_pct"], 42.0)


# ============================================================================
# 5.130.1 — the review of 5.130.0 (1-Oct-2026)
# ============================================================================

class TestTheMinimumIsCheckedEvenUnderAHigherTarget(unittest.TestCase):

    def _min(self, hh, mm, soc, target, power_w, headroom):
        inputs = _inputs_at(hh, mm, soc)
        with patch.object(fs, "import_headroom_w", return_value=headroom):
            return fs._minimum_charge(inputs, _local(2026, 10, 1, 5, 0),
                                      target, power_w, 5.0, 50.0)

    def test_the_reviewed_case_reports_its_shortfall(self):
        # 40% at 04:55, plan aiming at 60%, 1 kW available: 3.5 kWh needed for the
        # minimum, about 0.08 deliverable. v2.10 reported 0.0.
        target, power, buy, raised, short = self._min(4, 55, 40.0, 60.0, 1000, 1000)
        self.assertEqual(target, 60.0)
        need = 0.10 * 35.04
        self.assertAlmostEqual(short, need - 1.0 * (5 / 60) * 0.97, places=1)
        self.assertGreater(short, 3.3)

    def test_a_small_plan_power_is_raised_to_what_the_minimum_needs(self):
        target, power, *_ = self._min(4, 0, 40.0, 60.0, 500, 16000)
        self.assertGreaterEqual(power / 1000.0 * 1.0 * 0.97, 0.10 * 35.04 - 1e-6)
        self.assertEqual(target, 60.0)

    def test_a_plan_that_reaches_it_reports_nothing(self):
        *_rest, short = self._min(2, 0, 40.0, 60.0, 10000, 16000)
        self.assertEqual(short, 0.0)


def _mk_site(soc=40.0, home_w=500.0, pv_w=0.0, verified=True):
    p = _mk(soc=soc, prefs={"fluxSiteImportLimitKw": "16",
                            "fluxSiteImportVerified": verified})
    p.latest_inverter_data.update(homePowerWatts=home_w, pvPowerWatts=pv_w)
    p._wear_p_per_kwh = MagicMock(return_value=2.0)
    p._flux_planner_floor_pct = MagicMock(return_value=20.0)
    return p


class TestTheFallbackChargeFitsTheSite(unittest.TestCase):

    def test_an_ev_on_the_2am_timer_leaves_nine_kw(self):
        self.assertEqual(_mk_site(home_w=7000.0)._cheap_window_charge_w(10000), 9000)

    def test_a_quiet_house_gets_the_inverter_rating(self):
        self.assertEqual(_mk_site(home_w=500.0)._cheap_window_charge_w(10000), 10000)

    def test_the_panels_offset_the_house(self):
        self.assertEqual(_mk_site(home_w=8000.0, pv_w=2000.0)._cheap_window_charge_w(10000),
                         10000)

    def test_an_unverified_limit_is_not_used(self):
        self.assertEqual(_mk_site(home_w=7000.0, verified=False)
                         ._cheap_window_charge_w(10000), 10000)

    def test_the_charge_starts_inside_the_headroom(self):
        p = _mk_site(home_w=7000.0)
        with patch.object(p, "_in_flux_cheap_window", return_value=True):
            p._act_on_decision(_decision(50.0))
        self.assertEqual(p.modbus.force_charge.call_args.args[0], 9000)

    def test_a_load_switching_on_trims_the_running_charge(self):
        p = _mk_site(home_w=500.0)
        p.modbus.set_charge_limit.return_value = True
        with patch.object(p, "_in_flux_cheap_window", return_value=True):
            p._act_on_decision(_decision(50.0))
            p.store["import_power_w"] = 10000
            p.latest_inverter_data["homePowerWatts"] = 7400.0
            p._act_on_decision(_decision(50.0))
        p.modbus.set_charge_limit.assert_called_with(8600)
        self.assertEqual(p.store["import_power_w"], 8600)

    def test_the_verify_pass_keeps_the_sized_limit(self):
        p = _mk_site(home_w=7000.0)
        p.store.update(import_active=True, cheap_window_import_active=True,
                       import_power_w=9000, import_target_soc=50.0)
        p.modbus.read_ems_mode.return_value = 0x04
        p.modbus.read_discharge_limit.return_value = 0
        p.modbus.read_charge_limit.return_value = 9000
        p.modbus.read_discharge_cutoff.return_value = None
        p.modbus.read_charge_cutoff.return_value = None
        with patch.object(p, "_driven_export_owns_registers", return_value=False), \
             patch.object(p, "_flux_armed", return_value=False):
            try:
                p._verify_ems_registers()
            except Exception:
                pass
        for c in p.modbus.set_charge_limit.call_args_list:
            self.assertNotEqual(c.args[0], 10000)


class TestTheMorningReportHasNoHiddenAllowance(unittest.TestCase):

    def _close_at(self, soc_at_close, minimum=50.0):
        p = _mk(soc=40.0)
        logged = []
        with tempfile.TemporaryDirectory() as tmp:
            p.data_dir = tmp
            p.store["cheap_window_watch"] = {"day": "2026-10-01", "minimum_pct": minimum,
                                             "manager_ran": False, "flux_ran": True,
                                             "flux_reason": ""}
            p.latest_inverter_data["batterySoc"] = soc_at_close

            class _DT(datetime):
                @classmethod
                def now(cls, tz=None):
                    return _local(2026, 10, 1, 5, 1)
            with patch.object(plugin, "datetime", _DT), \
                 patch.object(p, "_in_flux_cheap_window", return_value=False), \
                 patch.object(plugin, "log", side_effect=lambda m, level="INFO":
                              logged.append((level, m))):
                p._note_cheap_window_result()
            with open(os.path.join(tmp, "cheap_window_results.jsonl"),
                      encoding="utf-8") as fh:
                row = json.loads(fh.readline())
        return logged[0], row

    def test_49_6_is_not_met_and_says_by_how_much(self):
        (level, msg), row = self._close_at(49.6)
        self.assertFalse(row["met"])
        self.assertEqual(row["short_pct"], 0.4)
        self.assertEqual(level, "WARNING")
        self.assertIn("0.4 points under the 50% minimum", msg)

    def test_fifty_is_met(self):
        (level, msg), row = self._close_at(50.0)
        self.assertTrue(row["met"])
        self.assertEqual(level, "INFO")
        self.assertIn("meets the 50% minimum", msg)

    def test_well_below_says_how_far(self):
        (level, msg), row = self._close_at(42.0)
        self.assertIn("8.0 points short", msg)
        self.assertEqual(row["short_pct"], 8.0)


if __name__ == "__main__":
    unittest.main()
