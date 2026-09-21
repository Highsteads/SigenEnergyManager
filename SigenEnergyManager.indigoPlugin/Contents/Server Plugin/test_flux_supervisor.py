#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    test_flux_supervisor.py
# Description: Contract tests for the plugin side of the Flux strategy: who owns
#              the inverter, who owns the hardware discharge floor, how a claim is
#              recovered, and what the old manager may do while one stands.
# Author:      CliveS & Claude Opus 5 (1M context)
# Date:        16-09-2026
# Version:     2.0

import os
import sqlite3
import sys
import tempfile
import threading
import time
import types
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock
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

import plugin              # noqa: E402
import flux_strategy as fs  # noqa: E402

LONDON = ZoneInfo("Europe/London")


class _FakeExecutor:
    """Records calls rather than acting — this file tests WHICH calls are made."""

    def __init__(self, owns=False):
        self.calls       = []
        self.baselines   = []
        self._owns       = owns
        self.last_error  = ""
        self.next_result = "applied"
        self.release_ok  = True

    @property
    def owns_control(self):
        return self._owns

    def rebind(self, raw_driver):
        self.raw = raw_driver
        self._owns = True

    def configure_baseline(self, **kw):
        self.baselines.append(kw)

    def step(self, target, now, *, supervisor_owns=False, communications_ok=True):
        self.calls.append({"target": target, "now": now,
                           "supervisor_owns": supervisor_owns,
                           "communications_ok": communications_ok})
        if supervisor_owns:
            self._owns = False
            return "supervisor"
        if target is None:
            if self.release_ok:
                self._owns = False
                return "released"
            self._owns = True
            return "pending"
        # A failed start retains pending ownership — that is the executor's own
        # rule, and modelling it as "no longer owns" would let a test pass on a
        # deadlock the real thing cannot reach.
        self._owns = True
        return self.next_result

    def targets(self):
        return [c["target"] for c in self.calls if c["target"] is not None]


class _FakeModbus:
    connected = True

    def __init__(self, soc=50.0, pv_w=0.0, house_w=500.0, grid_w=500.0):
        self.soc     = soc
        self.pv_w    = pv_w
        self.house_w = house_w
        self.grid_w  = grid_w
        self.reads   = 0
        self.writes  = []

    def read_power_flows(self):
        self.reads += 1
        return {"batterySoc": self.soc, "pvPowerWatts": self.pv_w,
                "homePowerWatts": self.house_w, "gridPowerWatts": self.grid_w,
                "batteryPowerWatts": 0}

    def read_battery_soc(self):
        self.reads += 1
        return self.soc

    def set_self_consumption(self):
        self.writes.append("set_self_consumption")
        return True

    def set_charge_limit(self, watts, quiet=False):
        self.writes.append(("charge_limit", watts, quiet))
        return True

    def set_discharge_limit(self, watts):
        self.writes.append(("discharge_limit", watts))
        return True

    def set_charge_cutoff(self, pct):
        self.writes.append(("charge_cutoff", pct))
        return True

    def set_discharge_cutoff(self, pct):
        self.writes.append(("discharge_cutoff", pct))
        return True

    def set_remote_ems_mode(self, mode):
        self.writes.append(("mode", mode))
        return True

    def enable_remote_ems(self):
        self.writes.append("enable_remote_ems")
        return True

    def read_ems_mode(self):
        return 2

    def read_charge_limit(self):
        return 10000

    def read_discharge_limit(self):
        return 10000

    def read_charge_cutoff(self):
        return 100.0

    def read_discharge_cutoff(self):
        return 1.0

    def read_remote_ems_enabled(self):
        return True

    def set_backup_soc(self, pct):
        self.writes.append(("backup_soc", pct))
        return True

    def read_backup_soc(self):
        return 20.0

    def set_grid_import_limit(self, watts):
        self.writes.append(("grid_import_limit", watts))
        return True

    def read_grid_import_limit(self):
        return 4294967295

    def night_export(self, watts):
        self.writes.append(("night_export", watts))
        return True

    def daytime_export(self, watts):
        self.writes.append(("daytime_export", watts))
        return True

    def force_charge(self, watts, cutoff_soc=None):
        self.writes.append(("force_charge", watts, cutoff_soc))
        return True

    def return_to_local(self):
        self.writes.append("return_to_local")
        return True


def _flux_prefs(**over):
    prefs = {
        "fluxEnabled":            True,
        "fluxCommissioned":       True,
        "fluxReservePct":         "20",
        "fluxWearPencePerKwh":    "5",
        "fluxSiteImportLimitKw":  "10.0",
        "fluxSiteImportVerified": True,
        "inverterMaxKw":          "10.0",
        "maxExportKw":            "4.0",
        "batteryCapacityKwh":     "35.04",
        "batteryEfficiency":      "94",
        "batteryHealthCutoff":    "1",
    }
    prefs.update(over)
    return prefs


def _evidence(import_key="flux", export_key="flux", fetched_at=None,
              import_product="FLUX-IMPORT-23-02-14",
              export_product="FLUX-EXPORT-23-02-14"):
    """One account fetch, carrying its own timestamp — the shape the proof takes."""
    return {
        "fetched_at": time.time() if fetched_at is None else fetched_at,
        "import": {"tariff_key": import_key, "product_code": import_product,
                   "tariff_code": f"E-1R-{import_product}-F", "mpan": "import-mpan"},
        "export": {"tariff_key": export_key, "product_code": export_product,
                   "tariff_code": f"E-1R-{export_product}-F", "mpan": "export-mpan"},
    }


class _pinned_clock:
    """Pin `plugin.datetime.now` for a block.

    Anything that measures against the next 02:00 is time-of-day dependent, and
    a test written at 22:00 with an event four hours out lands beyond the horizon
    and reserves nothing — passing all morning and failing all evening. Pinned to
    a fixed local evening, every horizon in these tests is six hours long.
    """

    def __init__(self, local="2026-09-16 20:00"):
        self.at = datetime.strptime(local, "%Y-%m-%d %H:%M").replace(tzinfo=LONDON)

    def __enter__(self):
        at, real = self.at, plugin.datetime

        class _Clock(real):
            @classmethod
            def now(cls, tz=None):
                return at.astimezone(tz or timezone.utc)

        self._real = real
        plugin.datetime = _Clock
        return at.astimezone(timezone.utc)

    def __exit__(self, *exc):
        plugin.datetime = self._real
        return False


def _mk_plugin(prefs=None, soc=50.0, executor=None, data_dir=None):
    p = plugin.Plugin.__new__(plugin.Plugin)
    p._state_lock = threading.RLock()
    p.logger      = MagicMock()
    p.debug       = False
    p.pluginPrefs = prefs if prefs is not None else _flux_prefs()
    p.data_dir    = data_dir or tempfile.mkdtemp()
    p.modbus      = _FakeModbus(soc)
    p.octopus     = None
    p.forecast    = None
    p.axle        = None
    p.flux_executor = executor
    p.latest_inverter_data = {"batterySoc": soc, "pvPowerWatts": 0.0,
                              "homePowerWatts": 500.0, "gridPowerWatts": 500.0,
                              "_read_at": time.time()}
    p.latest_forecast_data = {}
    p.latest_rates_data    = {}
    p.latest_decision      = None
    p.store = {
        "vpp_state": "idle", "vpp_event": None, "manager_paused": False,
        "import_active": False, "export_active": False,
        "solar_overflow_active": False, "flood_prev_target_soc": None,
        "storm_level": "none", "flux_manual_preempt": "",
        "flux_preempted_at": 0.0, "flux_clear_ticks": 99,
        "flux_applied_key": None, "flux_last_step": 0.0,
        "flux_last_result": "", "flux_status": "", "flux_note_logged": "",
        "flux_pending_since": 0.0, "flux_pending_logged": 0.0,
        "flux_commitment_signature": None,
        "consumption_profile": [0.5] * 48,
        "profile_built_at": time.time(),
        "forecast_ok_at": time.time(),
        "last_forecast": time.time(),
        "flux_rates_at": time.time(),
        "import_charge_cutoff_pct": None,
        "happy_hour_import_active": False,
        "saving_session_export_active": False,
        "saving_sessions_windows": [],
    }
    p._power_cut_window_active = MagicMock(return_value=False)
    p._find_device            = MagicMock(return_value=None)
    p._save_accumulators      = MagicMock()
    p._trigger_event          = MagicMock()
    p._set_import_cutoff      = MagicMock()
    p._restore_import_cutoff  = MagicMock()
    return p


def _seed_flux_day(p, when_local, soc=50.0):
    """A complete, fresh, published Flux day plus a verified paired account."""
    day = when_local.date()

    def _slots(prices):
        out = []
        for h0, h1, key in ((0, 2, "day"), (2, 5, "cheap"), (5, 16, "day"),
                            (16, 19, "peak"), (19, 24, "day")):
            for offset in range(2):
                d = day + timedelta(days=offset)
                start = fs._wall(LONDON, d, fs.time(h0, 0))
                end   = (fs._wall(LONDON, d + timedelta(days=1), fs.time(0, 0))
                         if h1 == 24 else fs._wall(LONDON, d, fs.time(h1, 0)))
                out.append({"valid_from": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                            "valid_to":   end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                            "value_inc_vat": prices[key]})
        return out

    p.store["flux_import_slots"] = _slots({"cheap": 16.8, "day": 31.4, "peak": 44.9})
    p.store["flux_export_slots"] = _slots({"cheap": 4.7, "day": 10.2, "peak": 29.6})
    p.store["flux_rates_at"]     = time.time()
    p.store["flux_account_evidence"] = _evidence()
    p.latest_rates_data = {"tariff_info": {"tariff_key": "flux",
                                           "product_code": "FLUX-IMPORT-23-02-14"}}
    buckets = {}
    for offset in range(3):
        d = day + timedelta(days=offset)
        for h in range(24):
            buckets[f"{d:%Y-%m-%d} {h:02d}:00:00"] = 2000.0 if 8 <= h < 17 else 0.0
    p.latest_forecast_data = {"_hourly_p50_today": buckets, "biasFactor": 1.0}
    p.store["forecast_ok_at"] = time.time()
    p.latest_inverter_data = {"batterySoc": soc, "pvPowerWatts": 0.0,
                              "homePowerWatts": 500.0, "gridPowerWatts": 500.0,
                              "_read_at": time.time()}
    p.modbus.soc = soc


class _FluxCase(unittest.TestCase):
    """Runs a supervisor pass with BOTH clocks pinned to a chosen local time.

    Both, because the supervisor uses `datetime.now()` for the decision lease and
    `time.time()` for every freshness stamp, and it compares them: an observation
    stamped from the real wall clock against a decision made in a pinned 2026
    morning is "in the future", and the target is correctly refused. Pinning one
    clock and not the other tests the harness, not the code.
    """

    def _at(self, hhmm, soc=50.0, prefs=None, **store):
        p = _mk_plugin(prefs=prefs, soc=soc)
        p.flux_executor = _FakeExecutor()
        when  = datetime(2026, 9, 16, hhmm[0], hhmm[1], tzinfo=LONDON)
        epoch = when.timestamp()
        _seed_flux_day(p, when, soc=soc)
        for key, age in (("flux_rates_at", 600), ("forecast_ok_at", 600),
                         ("profile_built_at", 3600), ("last_forecast", 600)):
            p.store[key] = epoch - age
        # The account evidence carries its own timestamp and is checked against
        # the same pinned clock as everything else.
        p.store["flux_account_evidence"] = _evidence(fetched_at=epoch - 600)
        p.latest_inverter_data["_read_at"] = epoch - 1
        p.store.update(store)
        return p, when

    def _run(self, p, when, at=None):
        epoch = (at or when).timestamp()
        real_dt, real_time = plugin.datetime, plugin.time

        class _Clock(real_dt):
            @classmethod
            def now(cls, tz=None):
                return (at or when).astimezone(tz or timezone.utc)

        class _Time:
            def __getattr__(self_inner, name):
                return getattr(real_time, name)

            @staticmethod
            def time():
                return epoch

        plugin.datetime = _Clock
        plugin.time     = _Time()
        try:
            p._flux_supervisor_step()
        finally:
            plugin.datetime = real_dt
            plugin.time     = real_time


# ================================================================
# Arming and the PAIRED account check (review 5)
# ================================================================

class TestArming(unittest.TestCase):

    def test_both_switches_are_needed(self):
        self.assertFalse(_mk_plugin(_flux_prefs(fluxEnabled=False))._flux_enabled())
        self.assertFalse(_mk_plugin(_flux_prefs(fluxCommissioned=False))._flux_enabled())
        self.assertTrue(_mk_plugin()._flux_enabled())

    def test_an_unconfigured_install_is_off(self):
        self.assertFalse(_mk_plugin({})._flux_enabled())

    def test_no_account_evidence_at_all_is_refused(self):
        ok, why = _mk_plugin()._flux_tariff_verified()
        self.assertFalse(ok)
        self.assertIn("have not been read", why)

    def test_both_agreements_on_flux_are_accepted(self):
        p = _mk_plugin()
        p.store["flux_account_evidence"] = _evidence()
        self.assertTrue(p._flux_tariff_verified()[0])

    def test_an_import_flux_agreement_alone_does_not_prove_paired_flux(self):
        p = _mk_plugin()
        p.store["flux_account_evidence"] = _evidence(export_key="outgoing")
        ok, why = p._flux_tariff_verified()
        self.assertFalse(ok)
        self.assertIn("export agreement", why)

    def test_a_tracker_import_agreement_is_refused(self):
        p = _mk_plugin()
        p.store["flux_account_evidence"] = _evidence(import_key="tracker")
        ok, why = p._flux_tariff_verified()
        self.assertFalse(ok)
        self.assertIn("import agreement", why)

    def test_stale_account_evidence_is_refused_however_fresh_the_prices_are(self):
        """The proof must age with the ACCOUNT fetch, not with the price fetch.

        This is the shape of the old bug exactly: the account lookup fails, the
        last good agreement is kept, the public prices refresh, and the house
        goes on trading a tariff nothing has re-checked.
        """
        p = _mk_plugin()
        p.store["flux_account_evidence"] = _evidence(
            fetched_at=time.time() - plugin.FLUX_ACCOUNT_EVIDENCE_MAX_AGE_S - 60)
        p.store["flux_rates_at"] = time.time()          # prices perfectly fresh
        ok, why = p._flux_tariff_verified()
        self.assertFalse(ok)
        self.assertIn("recently", why)

    def test_evidence_with_no_timestamp_at_all_is_refused(self):
        p = _mk_plugin()
        p.store["flux_account_evidence"] = _evidence(fetched_at=0)
        self.assertFalse(p._flux_tariff_verified()[0])

    def test_evidence_stamped_in_the_future_is_refused(self):
        p = _mk_plugin()
        p.store["flux_account_evidence"] = _evidence(fetched_at=time.time() + 3600)
        self.assertFalse(p._flux_tariff_verified()[0])

    def test_a_forced_tariff_cannot_arm_anything(self):
        """The override never reaches this check — only the account does."""
        p = _mk_plugin()
        p.pluginPrefs["tariffOverride"] = "flux"
        p.latest_rates_data = {"tariff_info": {
            "tariff_key": "flux", "overridden": True, "detected_key": "tracker"}}
        self.assertFalse(p._flux_tariff_verified()[0])
        p.store["flux_account_evidence"] = _evidence()
        self.assertTrue(p._flux_tariff_verified()[0],
                        "the account proof should stand on its own")

    def test_export_product_codes_are_classified_from_the_account(self):
        import octopus_api
        api = octopus_api.OctopusAPI.__new__(octopus_api.OctopusAPI)
        for code, expected in (
                ("E-1R-FLUX-EXPORT-23-02-14-F", "flux"),
                ("E-1R-OUTGOING-VAR-24-10-26-F", "outgoing"),
                ("E-1R-AGILE-OUTGOING-19-05-13-F", "agile_outgoing"),
                ("E-1R-INTELLI-FLUX-EXPORT-23-07-14-F", "iflux"),
                ("E-1R-NONSENSE-1-F", "unknown")):
            self.assertEqual(
                api._classify_export_tariff_code(code)["tariff_key"], expected)


class TestAccountEvidencePlumbing(unittest.TestCase):
    """The rate refresh must fail closed and use the BILLED product."""

    def _plugin_with_octopus(self, evidence):
        p = _mk_plugin()
        p.octopus = MagicMock()
        p.octopus.get_account_agreements.return_value = evidence
        p.octopus._fetch_rate_schedule.return_value = [
            {"valid_from": "2026-09-16T01:00:00Z", "valid_to": "2026-09-16T04:00:00Z",
             "value_inc_vat": 16.8}]
        return p

    def test_the_billed_product_is_used_not_the_newest_advertised_one(self):
        """`_probe_product_by_prefix` returns the most recently LAUNCHED product.

        On a re-versioned tariff that is not the one this house is billed for, so
        the rates would come from a product nobody pays.
        """
        p = self._plugin_with_octopus(_evidence(import_product="FLUX-IMPORT-23-02-14",
                                                export_product="FLUX-EXPORT-23-02-14"))
        p.octopus._probe_product_by_prefix.return_value = "FLUX-IMPORT-26-09-01"
        p._refresh_flux_rates()
        used = {call.args[0] for call in p.octopus._fetch_rate_schedule.call_args_list}
        self.assertEqual(used, {"FLUX-IMPORT-23-02-14", "FLUX-EXPORT-23-02-14"})
        p.octopus._probe_product_by_prefix.assert_not_called()

    def test_a_failed_account_read_clears_the_evidence_and_the_prices(self):
        p = self._plugin_with_octopus({})
        p.store["flux_account_evidence"] = _evidence()
        stamp = p.store["flux_rates_at"] = time.time() - 500
        p._refresh_flux_rates()
        self.assertEqual(p.store["flux_account_evidence"], {})
        self.assertEqual(p.store["flux_rates_at"], stamp,
                         "prices were stamped fresh on a failed account read")
        self.assertIn("nothing is proven", p.store["flux_rates_problem"])

    def test_an_account_with_no_export_agreement_fetches_no_rates(self):
        evidence = _evidence()
        evidence["export"] = {}
        p = self._plugin_with_octopus(evidence)
        p._refresh_flux_rates()
        p.octopus._fetch_rate_schedule.assert_not_called()
        self.assertIn("could not both", p.store["flux_rates_problem"])

    def test_a_successful_read_stores_the_evidence_with_its_timestamp(self):
        p = self._plugin_with_octopus(_evidence())
        p._refresh_flux_rates()
        stored = p.store["flux_account_evidence"]
        self.assertGreater(stored.get("fetched_at", 0), 0)
        self.assertTrue(p._flux_tariff_verified()[0])


# ================================================================
# The ONE owner of the hardware discharge floor (review 4)
# ================================================================

class TestPolicyFloorOwnership(unittest.TestCase):

    def test_the_reserve_survives_flux_letting_go(self):
        """The floor is a day-long policy, not a window-long one."""
        p = _mk_plugin()
        self.assertEqual(p._policy_discharge_floor_pct(), 20.0)
        self.assertEqual(p._flux_baseline()["baseline_discharge_cutoff_pct"], 20.0)

    def test_with_the_feature_off_the_floor_is_exactly_what_it_always_was(self):
        """Nothing changes for an install that never arms this."""
        p = _mk_plugin(_flux_prefs(fluxEnabled=False))
        self.assertEqual(p._policy_discharge_floor_pct(), 1.0)

    def test_the_register_verifier_now_expects_the_policy_floor(self):
        """It used to re-assert batteryHealthCutoff every 60 seconds.

        That is the mechanism that would have undone the reserve within a minute
        of Flux handing back, and nothing else in the plugin would have noticed.
        """
        p = _mk_plugin()
        p.store["export_active"] = False
        p.store["import_active"] = False
        p.modbus.read_discharge_cutoff = lambda: 20.0
        p.modbus.read_backup_soc = lambda: 1.0
        p._verify_ems_registers()
        # Reserve on the backup register; the absolute cutoff back to health.
        self.assertIn(("backup_soc", 20.0), p.modbus.writes)
        self.assertIn(("discharge_cutoff", 1.0), p.modbus.writes)

    def test_a_flood_prevention_target_still_outranks_the_reserve(self):
        p = _mk_plugin()
        p.store["flood_prev_target_soc"] = 70.0
        self.assertEqual(p._policy_discharge_floor_pct(), 70.0)

    def test_the_vpp_restore_returns_to_the_policy_floor_not_one_percent(self):
        p = _mk_plugin()
        p._restore_discharge_cutoff()
        self.assertIn(("backup_soc", 20.0), p.modbus.writes)
        self.assertIn(("discharge_cutoff", 1.0), p.modbus.writes)

    def test_the_pause_disengage_returns_to_the_policy_floor(self):
        p = _mk_plugin()
        p.store["vpp_state"] = "idle"
        p._disengage_to_safe_baseline("Pause")
        self.assertIn(("backup_soc", 20.0), p.modbus.writes)
        self.assertIn(("discharge_cutoff", 1.0), p.modbus.writes)

    def test_a_committed_event_raises_the_floor_for_everybody(self):
        p = _mk_plugin()
        with _pinned_clock() as now:
            p.store["vpp_state"] = "announced"
            p.store["vpp_event"] = {"start_time": now + timedelta(hours=1),
                                    "end_time": now + timedelta(hours=2),
                                    "import_export": "export", "duration_hrs": 1.0}
            self.assertGreater(p._policy_discharge_floor_pct(), 20.0)

    def test_a_driving_owner_takes_the_commitment_component_off_the_floor(self):
        """During pre-charge and the window itself, the VPP owns that energy.

        It sets its own floor for the event, raised to this policy floor. Adding
        the reservation on top as well would hold the floor up against the
        dispatch the reservation was made for.
        """
        p = _mk_plugin()
        now = datetime.now(timezone.utc)
        p.store["vpp_event"] = {"start_time": now + timedelta(hours=1),
                                "end_time": now + timedelta(hours=2),
                                "import_export": "export", "duration_hrs": 1.0}
        p.store["vpp_state"] = "announced"
        announced = p._policy_discharge_floor_pct()
        self.assertGreater(announced, 20.0)
        for driving in ("pre_charging", "active"):
            p.store["vpp_state"] = driving
            self.assertEqual(p._policy_discharge_floor_pct(), 20.0, driving)

    def test_the_dispatch_being_served_is_not_reserved_against_itself(self):
        """The VPP writes this register ONCE, at pre-charge, half an hour early.

        So a floor that includes the event's own allocation at that moment blocks
        the whole dispatch, and nothing later corrects it. Both phases.
        """
        p = _mk_plugin()
        with _pinned_clock() as now:
            p.store["vpp_event"] = {"id": "axle-1",
                                    "start_time": now + timedelta(minutes=30),
                                    "end_time": now + timedelta(minutes=90),
                                    "import_export": "export", "duration_hrs": 1.0}
            p.store["vpp_state"] = "announced"
            self.assertGreater(p._policy_discharge_floor_pct(), 20.0,
                               "an announced event should still be reserved")
            for driving in ("pre_charging", "active"):
                p.store["vpp_state"] = driving
                self.assertEqual(p._policy_discharge_floor_pct(), 20.0, driving)

    def test_another_event_is_still_reserved_while_one_is_dispatched(self):
        """Only the dispatch's OWN allocation comes off, not everybody else's."""
        p = _mk_plugin()
        with _pinned_clock() as now:
            p.store["vpp_event"] = {"id": "axle-1",
                                    "start_time": now + timedelta(minutes=30),
                                    "end_time": now + timedelta(minutes=90),
                                    "import_export": "export", "duration_hrs": 1.0}
            p.store["vpp_state"] = "pre_charging"
            p.store["saving_sessions_windows"] = [{
                "id": "ss-later", "start": (now + timedelta(hours=3)).isoformat(),
                "end": (now + timedelta(hours=4)).isoformat(),
                "points": 1800, "direction": plugin.SAVING_SESSION_TURN_DOWN}]
            self.assertGreater(p._policy_discharge_floor_pct(), 20.0,
                               "an unrelated later session was dropped with the dispatch")

    def test_the_floor_uses_the_union_budget_not_a_sum(self):
        """Two schemes paying for one exported kWh must not reserve it twice."""
        p = _mk_plugin()
        with _pinned_clock() as now:
            start = (now + timedelta(hours=2)).isoformat()
            end   = (now + timedelta(hours=3)).isoformat()
            p.store["saving_sessions_windows"] = [
                {"id": "a", "start": start, "end": end, "points": 1800,
                 "direction": plugin.SAVING_SESSION_TURN_DOWN}]
            one = p._policy_discharge_floor_pct()
            p.store["vpp_event"] = {"id": "axle-1",
                                    "start_time": now + timedelta(hours=2),
                                    "end_time": now + timedelta(hours=3),
                                    "import_export": "export", "duration_hrs": 1.0}
            p.store["vpp_state"] = "announced"
            both = p._policy_discharge_floor_pct()
            self.assertGreater(one, 20.0)
            self.assertEqual(both, one, "overlapping events were summed, not unioned")

    def test_an_announced_window_does_not_pre_empt_on_the_state_change_either(self):
        """The _flux_other_owner fix is undone if the transition pre-empts."""
        p = _mk_plugin()
        p.flux_executor = _FakeExecutor(owns=True)
        p.store["vpp_state"] = plugin.VPP_IDLE
        p._vpp_transition(plugin.VPP_ANNOUNCED)
        self.assertTrue(p.flux_executor.owns_control)
        self.assertEqual(p.store.get("flux_manual_preempt", ""), "")

    def test_the_storm_reserve_reaches_the_planner_but_never_the_register(self):
        """A storm floor written to 40048 is one nothing would ever lower."""
        p = _mk_plugin()
        p.store["storm_level"] = "amber"
        self.assertEqual(p._policy_discharge_floor_pct(), 20.0)
        self.assertGreaterEqual(p._flux_planner_floor_pct(), plugin.STORM_SOC_AMBER)

    def test_flood_prevention_may_not_drain_through_the_reserve(self):
        """Flood prevention protects money; the reserve protects a power cut."""
        p = _mk_plugin()
        p.modbus = _FakeModbus()
        decision = MagicMock()
        decision.action = plugin.ACTION_START_EXPORT
        decision.reason = "flood"
        decision.power_watts = 4000
        decision.target_soc_pct = 8.0          # below the 20% reserve
        p.latest_inverter_data = {"batterySoc": 90.0, "emsWorkMode": "Max Self Consumption"}
        p.store["export_active"] = False
        p.store["import_active"] = False
        p._set_flood_prev_target = MagicMock()
        p._act_on_decision(decision)
        cutoffs = [w[1] for w in p.modbus.writes
                   if isinstance(w, tuple) and w[0] == "discharge_cutoff"]
        self.assertTrue(cutoffs)
        self.assertGreaterEqual(cutoffs[-1], 20.0)

    def test_a_scheduled_import_pre_empts_before_its_own_charge_command(self):
        """It writes straight from the tick, outside the manager's evaluate."""
        p = _mk_plugin()
        p.flux_executor = _FakeExecutor(owns=True)
        order = []

        class _Watching(_FakeExecutor):
            def step(self_inner, target, now, **kw):
                order.append("flux_release")
                return super().step(target, now, **kw)

        p.flux_executor = _Watching(owns=True)

        class _WatchingModbus(_FakeModbus):
            def force_charge(self_inner, watts, cutoff_soc=None):
                order.append("force_charge")
                return True

        p.modbus = _WatchingModbus()
        p.store["import_scheduled_time"] = datetime.now(timezone.utc) - timedelta(minutes=1)
        p.store["import_target_soc"]     = 80.0
        p._check_scheduled_import_impl()
        self.assertEqual(order[:2], ["flux_release", "force_charge"])

    def test_an_import_in_flight_keeps_its_raised_charge_ceiling(self):
        p = _mk_plugin()
        p.store["import_charge_cutoff_pct"] = 83.0
        self.assertEqual(p._flux_baseline()["baseline_charge_cutoff_pct"], 83.0)


# ================================================================
# Commitments from live plugin state (review 1)
# ================================================================

class TestGridOutageReleasesTheReserve(unittest.TestCase):
    """Register 40048 is absolute — the inverter honours it OFF-GRID as well.

    So an economic floor written there locks the backup reserve away during the
    very power cut it was saved for. Backup reserve is 40046's job; this register
    must fall to the health floor while the house is islanded.
    """

    def _outage(self, p, status="Off-grid (auto)", age=0.0, started=True):
        p.store["power_cut_started_at"] = (datetime.now(timezone.utc)
                                           if started else None)
        p.latest_inverter_data = {"batterySoc": 50.0, "gridStatus": status,
                                  "_read_at": time.time() - age}
        return p

    def test_on_grid_the_reserve_stands(self):
        self.assertEqual(_mk_plugin()._policy_discharge_floor_pct(), 20.0)

    def test_a_verified_outage_drops_the_floor_to_the_health_cutoff(self):
        p = self._outage(_mk_plugin())
        self.assertEqual(p._policy_discharge_floor_pct(), 1.0)

    def test_nothing_economic_can_raise_it_back_during_an_outage(self):
        """A flood pre-drain target is worth nothing in a blackout."""
        p = self._outage(_mk_plugin())
        p.store["flood_prev_target_soc"] = 70.0
        self.assertEqual(p._policy_discharge_floor_pct(), 1.0)

    def test_a_committed_grid_event_cannot_raise_it_back_either(self):
        p = self._outage(_mk_plugin())
        now = datetime.now(timezone.utc)
        p.store["vpp_state"] = "announced"
        p.store["vpp_event"] = {"id": "axle-1", "start_time": now + timedelta(hours=1),
                                "end_time": now + timedelta(hours=2),
                                "import_export": "export", "duration_hrs": 1.0}
        self.assertEqual(p._policy_discharge_floor_pct(), 1.0)

    def test_an_unknown_grid_reading_is_not_an_outage(self):
        """`Unknown (N)` is a transient unmapped read, not a blackout.

        Lowering a safety floor on one is exactly the false positive the outage
        detector was written to avoid in the first place.
        """
        p = self._outage(_mk_plugin(), status="Unknown (7)")
        self.assertEqual(p._policy_discharge_floor_pct(), 20.0)

    def test_a_stale_reading_is_not_a_fresh_outage(self):
        """The flag survives a dead poll loop; the floor must not drop on memory."""
        p = self._outage(_mk_plugin(), age=3600)
        self.assertFalse(p._grid_outage_active())
        self.assertEqual(p._policy_discharge_floor_pct(), 20.0)

    def test_the_flag_alone_without_an_off_grid_reading_is_not_an_outage(self):
        p = self._outage(_mk_plugin(), status="On-grid")
        self.assertFalse(p._grid_outage_active())

    def test_an_off_grid_reading_without_the_verified_flag_is_not_an_outage(self):
        """Both sources must agree — one of them alone is not verification."""
        p = self._outage(_mk_plugin(), started=False)
        self.assertFalse(p._grid_outage_active())

    def test_flux_lets_go_before_the_floor_is_lowered(self):
        """An outage is the highest-priority owner, so the claim goes down first.

        The supervisor tick runs before the manager in _tick, and the manager's
        verify pass is what writes the new floor — so releasing here is what puts
        the release ahead of the write.
        """
        p = self._outage(_mk_plugin())
        self.assertIn("grid is down", p._flux_other_owner())

    def test_the_release_baseline_carries_the_lowered_floor(self):
        p = self._outage(_mk_plugin())
        self.assertEqual(p._flux_baseline()["baseline_discharge_cutoff_pct"], 1.0)

    def test_the_verify_pass_writes_the_lowered_floor(self):
        """End to end: what actually reaches register 40048 during an outage."""
        p = self._outage(_mk_plugin())
        p.modbus = _FakeModbus()
        p.modbus.read_discharge_cutoff = lambda: 20.0
        p.store["export_active"] = False
        p.store["import_active"] = False
        p._verify_ems_registers()
        self.assertIn(("discharge_cutoff", 1.0), p.modbus.writes)


class TestWindowEndIsNotAFault(unittest.TestCase):
    """Live 17-Sep-2026 19:00:33: the clean end of the peak logged a WARNING saying
    the inverter had not acknowledged the export command."""

    class _Decision:
        mode = "export"
        def __init__(self, until):
            self.decision_until = until

    def _log(self, until):
        import plugin
        from unittest.mock import patch
        p = _mk_plugin()
        lines = []
        ex = _FakeExecutor(owns=True)
        ex.last_error = "Expired or invalid Flux decision/observation lease"
        with patch.object(plugin, "log", lambda msg, level="INFO": lines.append((level, msg))):
            p._flux_note_pending(ex, self._Decision(until))
        return lines

    def test_a_window_that_has_ended_reports_plainly(self):
        level, msg = self._log(datetime.now(timezone.utc) - timedelta(seconds=1))[0]
        self.assertEqual(level, "INFO")
        self.assertIn("window ended", msg)
        self.assertNotIn("did not acknowledge", msg)

    def test_a_real_refusal_inside_the_window_still_warns(self):
        level, msg = self._log(datetime.now(timezone.utc) + timedelta(minutes=10))[0]
        self.assertEqual(level, "WARNING")
        self.assertIn("did not acknowledge", msg)

    def test_an_undated_decision_still_warns(self):
        level, _ = self._log(None)[0]
        self.assertEqual(level, "WARNING")


class TestSolarOverflowGivesWayInThePeak(unittest.TestCase):
    """17-Sep-2026 live: overflow from 14:24 held Flux off for the whole peak."""

    def test_overflow_is_an_owner_outside_the_peak(self):
        p = _mk_plugin()
        p.store["solar_overflow_active"] = True
        p._flux_peak_now = lambda: False
        self.assertEqual(p._flux_other_owner(), "solar overflow is running")

    def test_overflow_is_not_an_owner_inside_the_peak(self):
        p = _mk_plugin()
        p.store["solar_overflow_active"] = True
        p._flux_peak_now = lambda: True
        self.assertEqual(p._flux_other_owner(), "")

    def test_peak_now_uses_local_time_and_needs_flux_armed(self):
        p = _mk_plugin()
        with _pinned_clock("2026-09-17 16:30"):
            self.assertTrue(p._flux_peak_now())
        with _pinned_clock("2026-09-17 15:30"):
            self.assertFalse(p._flux_peak_now())
        off = _mk_plugin(_flux_prefs(fluxEnabled=False))
        with _pinned_clock("2026-09-17 16:30"):
            self.assertFalse(off._flux_peak_now())

    def test_other_owners_still_outrank_flux_in_the_peak(self):
        p = _mk_plugin()
        p._flux_peak_now = lambda: True
        p.store["solar_overflow_active"] = True
        p.store["export_active"] = True
        self.assertIn("export in flight", p._flux_other_owner())


class TestManagerWaitsForTheTariff(unittest.TestCase):
    """The first tick after a restart must not ACT on a plan made as Tracker."""

    def _run(self, rates, wait_since=None):
        from unittest.mock import MagicMock
        p = _mk_plugin()
        p.latest_rates_data = rates
        p.store["tariff_wait_since"] = wait_since
        p.flux_executor = None
        for name in ("_check_vpp_overrun", "_update_pv_tracking", "_apply_seasonal_override",
                     "_apply_storm_override", "_record_solar_overflow_shadow",
                     "_record_bank_first_metrics", "_log_manager_decision",
                     "_note_import_hold", "_update_manager_device", "_publish_flood_preview",
                     "_verify_ems_registers", "_act_on_decision", "_build_manager_snapshot"):
            setattr(p, name, MagicMock())
        p.manager = MagicMock()
        p._evaluate_manager_impl()
        return p

    def test_no_tariff_yet_means_no_action(self):
        p = self._run({})
        p._act_on_decision.assert_not_called()
        p._update_manager_device.assert_called_once()
        self.assertIsNotNone(p.store["tariff_wait_since"])

    def test_after_the_wait_it_acts_anyway(self):
        import plugin
        p = self._run({}, wait_since=time.time() - plugin.TARIFF_WAIT_S - 1)
        p._act_on_decision.assert_called_once()

    def test_with_a_tariff_it_acts_and_clears_the_wait(self):
        p = self._run({"tariff_info": {"tariff_key": "flux"}}, wait_since=time.time())
        p._act_on_decision.assert_called_once()
        self.assertIsNone(p.store["tariff_wait_since"])


class TestReserveNeverOnTheAbsoluteCutoff(unittest.TestCase):
    """17-Sep-2026 commissioning: no hardware watchdog exists, so whatever floor
    was last written survives a lost connection. On 40048 that locks the battery
    away in a power cut that follows; on 40046 (verified to stop a forced export
    on-grid) it does not. So Flux's floors must never reach 40048."""

    def test_the_flux_adapter_writes_its_floor_to_the_backup_reserve(self):
        import plugin
        m = _FakeModbus()
        d = plugin._FluxRawDriver(m, {"inverterMaxKw": "10"})
        self.assertTrue(d.set_discharge_cutoff(45.0))
        self.assertIn(("backup_soc", 45.0), m.writes)
        self.assertNotIn(("discharge_cutoff", 45.0), m.writes)
        self.assertEqual(d.read_discharge_cutoff(), 20.0)

    def test_the_absolute_cutoff_ignores_the_flux_reserve_and_commitments(self):
        p = _mk_plugin()
        now = datetime.now(timezone.utc)
        p.store["vpp_state"] = "announced"
        p.store["vpp_event"] = {"id": "axle-1", "start_time": now + timedelta(hours=1),
                                "end_time": now + timedelta(hours=2),
                                "import_export": "export", "duration_hrs": 1.0}
        self.assertEqual(p._absolute_cutoff_pct(), 1.0)
        self.assertGreater(p._policy_discharge_floor_pct(), 20.0)

    def test_an_unarmed_install_never_touches_the_backup_reserve(self):
        p = _mk_plugin(_flux_prefs(fluxEnabled=False))
        p.store["export_active"] = False
        p.store["import_active"] = False
        p.modbus.read_backup_soc = lambda: 5.0
        p._verify_ems_registers()
        p._restore_discharge_cutoff()
        self.assertFalse([w for w in p.modbus.writes
                          if isinstance(w, tuple) and w[0] in ("backup_soc", "grid_import_limit")])

    def test_the_verified_site_limit_is_enforced_by_the_inverter(self):
        p = _mk_plugin(_flux_prefs(fluxSiteImportLimitKw="16", fluxSiteImportVerified=True))
        p.store["export_active"] = False
        p.store["import_active"] = False
        p._verify_ems_registers()
        self.assertIn(("grid_import_limit", 16000), p.modbus.writes)

    def test_an_unverified_site_limit_is_not_written(self):
        p = _mk_plugin(_flux_prefs(fluxSiteImportVerified=False))
        p.store["export_active"] = False
        p.store["import_active"] = False
        p._verify_ems_registers()
        self.assertNotIn("grid_import_limit",
                         [w[0] for w in p.modbus.writes if isinstance(w, tuple)])


class TestCommitmentsFromState(unittest.TestCase):

    def _axle_event(self, p, start_h, end_h, direction="export"):
        """An event `start_h` hours ahead, lasting `end_h - start_h`.

        Relative to the real clock, because these tests do not pin one and an
        event at a fixed wall time would drift out of the future as the day wore
        on — passing in the morning and failing after seven in the evening.
        """
        now = datetime.now(timezone.utc)
        p.store["vpp_state"] = "announced"
        p.store["vpp_event"] = {
            "start_time": now + timedelta(hours=start_h - 16),
            "end_time":   now + timedelta(hours=end_h - 16),
            "import_export": direction,
            "duration_hrs": float(end_h - start_h)}

    def test_an_announced_axle_export_becomes_a_reservation(self):
        p = _mk_plugin()
        self._axle_event(p, 18, 19)
        c = p._flux_commitments()
        self.assertEqual(len(c), 1)
        self.assertEqual(c[0].source, "axle")
        self.assertEqual(c[0].kind, "export")
        self.assertAlmostEqual(c[0].energy_kwh, 4.0, places=3)

    def test_an_axle_IMPORT_event_is_not_an_export_reservation(self):
        p = _mk_plugin()
        self._axle_event(p, 18, 19, direction="import")
        self.assertEqual(p._flux_commitments(), ())

    def test_a_finished_axle_window_is_not_reserved(self):
        p = _mk_plugin()
        now = datetime.now(timezone.utc)
        p.store["vpp_state"] = "announced"
        p.store["vpp_event"] = {"start_time": now - timedelta(hours=3),
                                "end_time": now - timedelta(hours=2),
                                "import_export": "export", "duration_hrs": 1.0}
        self.assertEqual(p._flux_commitments(), ())

    def test_a_joined_turn_down_session_becomes_a_reservation(self):
        p = _mk_plugin()
        now = datetime.now(timezone.utc)
        p.store["saving_sessions_windows"] = [{
            "id": "ss1", "start": (now + timedelta(hours=1)).isoformat(),
            "end": (now + timedelta(hours=2)).isoformat(),
            "points": 1800, "direction": plugin.SAVING_SESSION_TURN_DOWN}]
        c = p._flux_commitments()
        self.assertEqual(len(c), 1)
        self.assertEqual(c[0].kind, "export")
        self.assertAlmostEqual(c[0].energy_kwh, 4.0, places=3)

    def test_a_happy_hour_is_an_IMPORT_commitment_not_an_export_one(self):
        """Free power wants an empty battery, not a full one."""
        p = _mk_plugin()
        now = datetime.now(timezone.utc)
        p.store["saving_sessions_windows"] = [{
            "id": "hh1", "start": (now + timedelta(hours=1)).isoformat(),
            "end": (now + timedelta(hours=2)).isoformat(),
            "points": 0, "direction": plugin.SAVING_SESSION_HAPPY_HOUR}]
        c = p._flux_commitments()
        self.assertEqual(len(c), 1)
        self.assertEqual(c[0].kind, "import")

    def test_axle_and_octopus_on_the_same_day_both_appear(self):
        p = _mk_plugin()
        self._axle_event(p, 18, 19)
        now = datetime.now(timezone.utc)
        p.store["saving_sessions_windows"] = [{
            "id": "ss1", "start": (now + timedelta(hours=5)).isoformat(),
            "end": (now + timedelta(hours=6)).isoformat(),
            "points": 1800, "direction": plugin.SAVING_SESSION_TURN_DOWN}]
        sources = {c.source for c in p._flux_commitments()}
        self.assertEqual(sources, {"axle", "octopus"})

    def test_a_malformed_session_window_is_skipped_not_fatal(self):
        p = _mk_plugin()
        p.store["saving_sessions_windows"] = [{"id": "bad", "start": "nonsense",
                                               "direction": plugin.SAVING_SESSION_TURN_DOWN}]
        self.assertEqual(p._flux_commitments(), ())


class TestCommitmentReplanning(_FluxCase):

    def test_a_new_commitment_invalidates_the_applied_command(self):
        """A late announcement must re-plan, not ride out the applied target."""
        p, when = self._at((16, 30), soc=95.0)
        self._run(p, when)
        self.assertIsNotNone(p.store["flux_commitment_signature"])
        first_key = p.store["flux_applied_key"]
        self.assertIsNotNone(first_key)

        day = when.date()
        p.store["vpp_state"] = "idle"     # announced but not yet owning
        p.store["saving_sessions_windows"] = [{
            "id": "ss-late",
            "start": fs._wall(LONDON, day, fs.time(18, 0)).isoformat(),
            "end":   fs._wall(LONDON, day, fs.time(19, 0)).isoformat(),
            "points": 1800, "direction": plugin.SAVING_SESSION_TURN_DOWN}]
        self._run(p, when)
        self.assertNotEqual(p.store["flux_applied_key"], first_key)
        applied = p.flux_executor.targets()
        self.assertGreater(applied[-1].discharge_cutoff_pct,
                           applied[0].discharge_cutoff_pct)


# ================================================================
# Leases and renewal (review 9)
# ================================================================

class TestLeases(_FluxCase):

    def test_a_target_is_renewed_every_tick_from_a_fresh_observation(self):
        """A skip longer than the observation lease would leave the executor
        holding a decision whose reading had expired, with nothing enforcing it."""
        p, when = self._at((2, 30), soc=25.0)
        self._run(p, when)
        self._run(p, when)
        self.assertGreaterEqual(len(p.flux_executor.targets()), 2)

    def test_the_observation_lease_is_inside_the_contract_budget(self):
        """Read the budget from the EXECUTOR, never a number copied into a test.

        It has already moved once — from ten seconds to sixty — because ten could
        not survive the executor's own throttled write sequence. A test holding
        its own copy of that number would have gone red for the wrong reason, and
        a test holding a stale copy would pass while the caller broke the rule.
        """
        import flux_execution as fe
        budget = fe.MAX_OBSERVATION_AGE_S
        p, when = self._at((2, 30), soc=25.0)
        self._run(p, when)
        t = p.flux_executor.targets()[0]
        self.assertLessEqual((t.expires_at - t.observed_at).total_seconds(), budget)
        self.assertLessEqual(t.expires_at, t.decision_until)

    def test_the_observation_itself_is_far_fresher_than_the_budget_allows(self):
        """The contract permits a minute; this supervisor uses seconds.

        At 10 kW an SOC moves about 1% a minute, so a minute-old reading is a
        different battery. The budget is what the executor will accept; this is
        what the caller chooses to offer.
        """
        p, when = self._at((2, 30), soc=25.0)
        self._run(p, when)
        t = p.flux_executor.targets()[0]
        age = (t.decision_at - t.observed_at).total_seconds()
        self.assertLessEqual(age, plugin.FLUX_OBSERVATION_MAX_AGE_S + 1.0)

    def test_decision_at_is_the_plan_time_not_the_moment_of_the_write(self):
        p, when = self._at((2, 30), soc=25.0)
        self._run(p, when)
        t = p.flux_executor.targets()[0]
        self.assertLessEqual(t.decision_at, t.decision_until)
        self.assertLessEqual(t.decision_until - t.decision_at, timedelta(minutes=30))

    def test_an_expired_plan_is_not_re_stamped_with_a_fresh_clock(self):
        p, _when = self._at((2, 30), soc=25.0)
        stale = fs.FluxDecision(
            mode=fs.MODE_CHARGE, owns=True, reason="stale",
            decision_at=datetime.now(timezone.utc) - timedelta(hours=2),
            decision_until=datetime.now(timezone.utc) + timedelta(minutes=5),
            ems_mode=3, charge_limit_w=3000, discharge_limit_w=0,
            charge_cutoff_pct=80.0, discharge_cutoff_pct=20.0)
        self.assertIsNone(p._flux_target(stale, time.time()))

    def test_a_decision_whose_window_has_passed_is_not_leased(self):
        p, _when = self._at((2, 30), soc=25.0)
        over = fs.FluxDecision(
            mode=fs.MODE_CHARGE, owns=True, reason="over",
            decision_at=datetime.now(timezone.utc) - timedelta(minutes=10),
            decision_until=datetime.now(timezone.utc) - timedelta(minutes=1),
            ems_mode=3, charge_limit_w=3000, discharge_limit_w=0,
            charge_cutoff_pct=80.0, discharge_cutoff_pct=20.0)
        self.assertIsNone(p._flux_target(over, time.time()))

    def test_an_observation_in_the_future_is_not_leased(self):
        p, _when = self._at((2, 30), soc=25.0)
        good = fs.FluxDecision(
            mode=fs.MODE_CHARGE, owns=True, reason="ok",
            decision_at=datetime.now(timezone.utc),
            decision_until=datetime.now(timezone.utc) + timedelta(minutes=10),
            ems_mode=3, charge_limit_w=3000, discharge_limit_w=0,
            charge_cutoff_pct=80.0, discharge_cutoff_pct=20.0)
        self.assertIsNone(p._flux_target(good, time.time() + 60))


# ================================================================
# Fresh physical headroom (review 8)
# ================================================================

class TestForecastFreshnessStamp(unittest.TestCase):
    """The stamp must record generation that came back, not a poll that ran."""

    def _plugin(self, payload):
        p = _mk_plugin()
        p.store["forecast_ok_at"] = 0.0
        p.forecast = MagicMock()
        p.forecast.fetch_forecast.return_value = payload
        p.forecast._cached_time = time.time()
        p._update_forecast_device = MagicMock()
        return p

    def test_a_fetch_that_returned_todays_buckets_stamps(self):
        today = plugin._london_today()
        p = self._plugin({"forecastStatus": "OK",
                          "_hourly_p50_today": {f"{today:%Y-%m-%d} 12:00:00": 1000.0},
                          "correctedTomorrowKwh": 10.0, "correctedTodayKwh": 5.0})
        p._refresh_forecast()
        self.assertGreater(p.store["forecast_ok_at"], 0.0)

    def test_a_cached_payload_from_yesterday_does_not_stamp(self):
        """It is not empty, and it is not today. Only dating it proves which."""
        stale = plugin._london_today() - timedelta(days=1)
        p = self._plugin({"forecastStatus": "OK",
                          "_hourly_p50_today": {f"{stale:%Y-%m-%d} 12:00:00": 1000.0},
                          "correctedTomorrowKwh": 10.0, "correctedTodayKwh": 5.0})
        p._refresh_forecast()
        self.assertEqual(p.store["forecast_ok_at"], 0.0)

    def test_a_failed_fetch_does_not_stamp(self):
        p = self._plugin({"forecastStatus": "No data", "_hourly_p50_today": {},
                          "correctedTomorrowKwh": 0.0, "correctedTodayKwh": 0.0})
        p._refresh_forecast()
        self.assertEqual(p.store["forecast_ok_at"], 0.0)


class TestObservation(unittest.TestCase):

    def test_a_fresh_cached_reading_costs_no_extra_transaction(self):
        p = _mk_plugin(soc=64.0)
        p.latest_inverter_data = {"batterySoc": 64.0, "pvPowerWatts": 100.0,
                                  "homePowerWatts": 400.0, "gridPowerWatts": 300.0,
                                  "_read_at": time.time()}
        observed, _at = p._flux_observation()
        self.assertEqual(observed["batterySoc"], 64.0)
        self.assertEqual(p.modbus.reads, 0)

    def test_an_aged_reading_is_re_read_with_its_flows(self):
        p = _mk_plugin(soc=64.0)
        p.modbus = _FakeModbus(soc=64.0, pv_w=1200.0, house_w=900.0, grid_w=-300.0)
        p.latest_inverter_data = {"batterySoc": 50.0, "pvPowerWatts": 0.0,
                                  "homePowerWatts": 0.0, "gridPowerWatts": 0.0,
                                  "_read_at": time.time() - 30}
        observed, at = p._flux_observation()
        self.assertEqual(p.modbus.reads, 1)
        self.assertEqual(observed["pvPowerWatts"], 1200.0)
        self.assertEqual(observed["homePowerWatts"], 900.0)
        self.assertLess(time.time() - at, 1.0)

    def test_a_cached_reading_missing_a_flow_figure_is_re_read(self):
        """Headroom needs the house figure, not just the SOC."""
        p = _mk_plugin(soc=64.0)
        p.latest_inverter_data = {"batterySoc": 64.0, "_read_at": time.time()}
        p._flux_observation()
        self.assertEqual(p.modbus.reads, 1)

    def test_no_reading_at_all_is_reported_rather_than_invented(self):
        p = _mk_plugin()
        p.latest_inverter_data = {}
        p.modbus.read_power_flows = lambda: None
        self.assertIsNone(p._flux_observation())

    def test_a_reading_stamped_in_the_future_is_not_used(self):
        p = _mk_plugin(soc=64.0)
        p.latest_inverter_data = {"batterySoc": 64.0, "pvPowerWatts": 0.0,
                                  "homePowerWatts": 0.0, "gridPowerWatts": 0.0,
                                  "_read_at": time.time() + 600}
        p._flux_observation()
        self.assertEqual(p.modbus.reads, 1, "a future timestamp was taken as fresh")


class TestHeadroomReachesTheHardware(_FluxCase):

    def test_a_busy_house_shrinks_the_charge_the_supervisor_asks_for(self):
        quiet, when = self._at((2, 30), soc=20.0)
        quiet.modbus = _FakeModbus(soc=20.0, house_w=500.0, grid_w=500.0)
        quiet.latest_inverter_data["_read_at"] = 0
        self._run(quiet, when)

        busy, when2 = self._at((2, 30), soc=20.0)
        busy.modbus = _FakeModbus(soc=20.0, house_w=7000.0, grid_w=7000.0)
        busy.latest_inverter_data["_read_at"] = 0
        self._run(busy, when2)

        self.assertGreater(quiet.flux_executor.targets()[0].charge_limit_w,
                           busy.flux_executor.targets()[0].charge_limit_w)


# ================================================================
# Ownership, pre-emption, and the manager standing down
# ================================================================

class TestPriority(unittest.TestCase):

    def test_nothing_in_the_way_means_nothing_in_the_way(self):
        self.assertEqual(_mk_plugin()._flux_other_owner(), "")

    def test_every_owner_that_outranks_flux_is_recognised(self):
        cases = {
            "manager_paused":               True,
            "vpp_state":                    "active",
            "saving_session_export_active": True,
            "happy_hour_import_active":     True,
            "import_active":                True,
            "export_active":                True,
            "solar_overflow_active":        True,
            "flood_prev_target_soc":        80.0,
            "storm_level":                  "amber",
            "flux_manual_preempt":          "a Force Grid Import action",
        }
        for key, value in cases.items():
            p = _mk_plugin()
            # Outside the peak: overflow gives way inside it (v5.109.5), so pin the
            # clock-dependent answer rather than let the real time of day decide.
            p._flux_peak_now = lambda: False
            p.store[key] = value
            self.assertNotEqual(p._flux_other_owner(), "", f"{key} did not stand down")

    def test_a_power_cut_lockout_outranks_flux(self):
        p = _mk_plugin()
        p._power_cut_window_active = MagicMock(return_value=True)
        self.assertIn("power-cut", p._flux_other_owner())


class TestSupervisorStep(_FluxCase):

    def test_a_cheap_window_claim_reaches_the_executor_as_a_charge(self):
        p, when = self._at((2, 30), soc=25.0)
        self._run(p, when)
        targets = p.flux_executor.targets()
        self.assertEqual(len(targets), 1)
        self.assertEqual(targets[0].ems_mode, 3)
        self.assertEqual(targets[0].discharge_limit_w, 0)

    def test_the_claim_carries_the_reserve_as_its_discharge_floor(self):
        p, when = self._at((2, 30), soc=25.0)
        self._run(p, when)
        self.assertGreaterEqual(p.flux_executor.targets()[0].discharge_cutoff_pct, 20.0)

    def test_midday_releases_to_the_old_manager(self):
        p, when = self._at((11, 0), soc=60.0)
        p.flux_executor._owns = True
        self._run(p, when)
        self.assertTrue(any(c["target"] is None and not c["supervisor_owns"]
                            for c in p.flux_executor.calls))
        self.assertFalse(p.flux_executor.owns_control)

    def test_an_owner_who_outranks_us_gets_a_hand_back_before_anything_else(self):
        p, when = self._at((2, 30), soc=25.0, vpp_state="active")
        p.flux_executor._owns = True
        self._run(p, when)
        self.assertIsNone(p.flux_executor.calls[0]["target"])
        self.assertEqual(p.flux_executor.targets(), [])

    def test_an_unconfirmed_hand_back_under_pre_emption_gives_up_the_claim(self):
        p, when = self._at((2, 30), soc=25.0, vpp_state="active")
        p.flux_executor._owns      = True
        p.flux_executor.release_ok = False
        self._run(p, when)
        self.assertTrue(any(c["supervisor_owns"] for c in p.flux_executor.calls))
        self.assertFalse(p.flux_executor.owns_control)

    def test_a_failed_hand_back_at_a_window_end_keeps_the_claim_and_retries(self):
        p, when = self._at((11, 0), soc=60.0)
        p.flux_executor._owns      = True
        p.flux_executor.release_ok = False
        self._run(p, when)
        self.assertFalse(any(c["supervisor_owns"] for c in p.flux_executor.calls))
        self.assertTrue(p.flux_executor.owns_control)

    def test_a_lost_connection_holds_the_claim_rather_than_pretending_to_stop(self):
        p, when = self._at((2, 30), soc=25.0)
        p.modbus.connected = False
        self._run(p, when)
        self.assertFalse(p.flux_executor.calls[-1]["communications_ok"])

    def test_an_unverified_paired_tariff_never_reaches_the_hardware(self):
        p, when = self._at((2, 30), soc=25.0)
        p.store["flux_account_evidence"] = _evidence(
            export_key="outgoing", fetched_at=when.timestamp() - 600)
        self._run(p, when)
        self.assertEqual(p.flux_executor.targets(), [])

    def test_stale_account_evidence_never_reaches_the_hardware(self):
        """Fresh prices, stale proof — the combination the old code allowed."""
        p, when = self._at((2, 30), soc=25.0)
        p.store["flux_account_evidence"] = _evidence(
            fetched_at=when.timestamp() - plugin.FLUX_ACCOUNT_EVIDENCE_MAX_AGE_S - 60)
        self._run(p, when)
        self.assertEqual(p.flux_executor.targets(), [])

    def test_stale_rates_never_reach_the_hardware(self):
        p, when = self._at((2, 30), soc=25.0)
        p.store["flux_rates_at"] = when.timestamp() - fs.MAX_RATES_AGE_S - 60
        self._run(p, when)
        self.assertEqual(p.flux_executor.targets(), [])

    def test_a_forecast_that_has_been_failing_never_reaches_the_hardware(self):
        """The success stamp, not the attempt stamp: `last_forecast` is written
        whether the fetch worked or not."""
        p, when = self._at((2, 30), soc=25.0)
        p.store["forecast_ok_at"] = when.timestamp() - fs.MAX_FORECAST_AGE_S - 60
        p.store["last_forecast"]  = when.timestamp()
        self._run(p, when)
        self.assertEqual(p.flux_executor.targets(), [])

    def test_a_stale_consumption_profile_never_reaches_the_hardware(self):
        p, when = self._at((2, 30), soc=25.0)
        p.store["profile_built_at"] = when.timestamp() - fs.MAX_PROFILE_AGE_S - 60
        self._run(p, when)
        self.assertEqual(p.flux_executor.targets(), [])

    def test_an_unverified_site_import_limit_never_reaches_the_hardware(self):
        p, when = self._at((2, 30), soc=25.0)
        p.pluginPrefs["fluxSiteImportVerified"] = False
        self._run(p, when)
        self.assertEqual(p.flux_executor.targets(), [])

    def test_the_cooldown_blocks_a_reclaim_after_a_pre_emption(self):
        p, when = self._at((2, 30), soc=25.0)
        p.store["flux_preempted_at"] = when.timestamp()
        p.store["flux_clear_ticks"]  = 0
        self._run(p, when)
        self.assertEqual(p.flux_executor.targets(), [])

    def test_a_single_clear_tick_is_not_enough_to_take_the_inverter_back(self):
        p, when = self._at((2, 30), soc=25.0)
        p.store["flux_preempted_at"] = (when.timestamp()
                                        - plugin.FLUX_PREEMPT_COOLDOWN_S - 1)
        p.store["flux_clear_ticks"]  = 0
        self._run(p, when)
        self.assertEqual(p.flux_executor.targets(), [])

    def test_the_manual_flag_expires_by_itself(self):
        p, when = self._at((2, 30), soc=25.0)
        p.store["flux_manual_preempt"] = "a Set Self-Consumption action"
        p.store["flux_preempted_at"]   = (when.timestamp()
                                          - plugin.FLUX_PREEMPT_COOLDOWN_S - 1)
        self._run(p, when)
        self.assertEqual(p.store["flux_manual_preempt"], "")

    def test_the_manual_flag_holds_for_the_cooldown(self):
        p, when = self._at((2, 30), soc=25.0)
        p.store["flux_manual_preempt"] = "a Set Self-Consumption action"
        p.store["flux_preempted_at"]   = when.timestamp()
        self._run(p, when)
        self.assertNotEqual(p.store["flux_manual_preempt"], "")


class TestPeakClaimedAtFourNotFive(_FluxCase):
    """21-Sep-2026 live: overflow held the inverter until 16:00, every one of its
    ticks restarted the five-minute stand-down, and export began at ~16:05."""

    def _overflow_until_four(self, soc=95.0):
        p, when = self._at((15, 59), soc=soc, solar_overflow_active=True,
                           flux_preempted_at=0.0, flux_clear_ticks=0)
        for _ in range(plugin.FLUX_RECLAIM_TICKS):   # overflow held it for a while
            self._run(p, when)
        self.assertEqual(p.store["flux_owner_reason"],
                         plugin.FLUX_OWNER_PRE_PEAK_OVERFLOW)
        self.assertEqual(p.flux_executor.targets(), [])
        return p, when

    def test_overflow_standing_aside_does_not_start_the_stand_down(self):
        p, _ = self._overflow_until_four()
        self.assertEqual(p.store["flux_preempted_at"], 0.0)

    def test_the_peak_is_claimed_on_the_first_tick_after_four(self):
        p, when = self._overflow_until_four()
        four = when.replace(hour=16, minute=0, second=5)
        self._run(p, four, at=four)
        self.assertEqual(p.store["flux_owner_reason"], "")
        self.assertEqual(len(p.flux_executor.targets()), 1)

    def test_a_real_owner_still_starts_the_stand_down(self):
        p, when = self._at((15, 59), soc=95.0, export_active=True,
                           flux_preempted_at=0.0, flux_clear_ticks=5)
        self._run(p, when)
        self.assertGreater(p.store["flux_preempted_at"], 0.0)
        self.assertEqual(p.store["flux_clear_ticks"], 0)


class TestPendingClaim(_FluxCase):
    """review 4: no arbitrary timeout may pretend somebody else took over."""

    def test_an_unacknowledged_command_keeps_the_claim_and_retries(self):
        p, when = self._at((2, 30), soc=25.0)
        p.flux_executor.next_result = "pending"
        self._run(p, when)
        self.assertGreater(p.store["flux_pending_since"], 0.0)
        self.assertFalse(any(c["supervisor_owns"] for c in p.flux_executor.calls))

    def test_a_long_standing_pending_claim_is_never_declared_somebody_else_s(self):
        """supervisor_owns means another owner really has the registers.

        Saying so because a clock ran out would be a lie told to the one
        component that must not be lied to.
        """
        p, when = self._at((2, 30), soc=25.0)
        p.flux_executor.next_result = "pending"
        self._run(p, when)
        p.store["flux_pending_since"]  = when.timestamp() - 6 * 3600
        p.store["flux_pending_logged"] = when.timestamp() - 6 * 3600
        self._run(p, when)
        self.assertFalse(any(c["supervisor_owns"] for c in p.flux_executor.calls),
                         "a timeout fabricated an external owner")
        self.assertTrue(p.flux_executor.owns_control)

    def test_a_successful_command_clears_the_pending_clock(self):
        p, when = self._at((2, 30), soc=25.0)
        p.store["flux_pending_since"] = when.timestamp() - 10
        self._run(p, when)
        self.assertEqual(p.store["flux_pending_since"], 0.0)


class TestManagerStandsDown(unittest.TestCase):

    def _manager_plugin(self, owns):
        p = _mk_plugin()
        p.flux_executor = _FakeExecutor(owns=owns)
        p.manager = MagicMock()
        decision  = MagicMock()
        decision.action = plugin.ACTION_SELF_CONSUMPTION
        decision.import_held = False
        p.manager.evaluate.return_value = decision
        for name in ("_build_manager_snapshot", "_apply_seasonal_override",
                     "_apply_storm_override", "_compute_vpp_reserved_kwh",
                     "_update_pv_tracking", "_record_solar_overflow_shadow",
                     "_record_bank_first_metrics", "_log_manager_decision",
                     "_note_import_hold", "_check_vpp_overrun",
                     "_update_manager_device", "_publish_flood_preview",
                     "_verify_ems_registers", "_act_on_decision"):
            setattr(p, name, MagicMock())
        p._resolve_export_lockout = MagicMock(return_value=True)
        p.latest_rates_data = {"tariff_info": {"tariff_key": "flux"}}
        return p

    def test_verify_and_act_do_not_run_while_flux_owns_the_inverter(self):
        p = self._manager_plugin(owns=True)
        p._evaluate_manager_impl()
        p._verify_ems_registers.assert_not_called()
        p._act_on_decision.assert_not_called()

    def test_the_device_is_still_updated_while_flux_owns(self):
        p = self._manager_plugin(owns=True)
        p._evaluate_manager_impl()
        p._update_manager_device.assert_called_once()

    def test_verify_and_act_run_normally_when_flux_does_not_own(self):
        p = self._manager_plugin(owns=False)
        p._evaluate_manager_impl()
        p._verify_ems_registers.assert_called_once()
        p._act_on_decision.assert_called_once()

    def test_a_new_owner_is_pre_empted_inside_the_same_evaluation(self):
        p = self._manager_plugin(owns=True)
        p.store["vpp_state"] = "active"
        p._evaluate_manager_impl()
        self.assertFalse(p.flux_executor.owns_control)
        p._act_on_decision.assert_called_once()


class TestPreemption(unittest.TestCase):

    def test_a_manual_action_releases_before_it_writes(self):
        p = _mk_plugin()
        order = []

        class _Watching(_FakeExecutor):
            def step(self_inner, target, now, **kw):
                order.append("flux_release")
                return super().step(target, now, **kw)

        p.flux_executor = _Watching(owns=True)

        class _WatchingModbus(_FakeModbus):
            def set_self_consumption(self_inner):
                order.append("modbus_write")
                return True

        p.modbus = _WatchingModbus()
        action = MagicMock()
        action.props = {}
        p.actionSetSelfConsumption(action)
        self.assertEqual(order[:2], ["flux_release", "modbus_write"])

    def test_every_hardware_action_pre_empts(self):
        for name, props in (("actionForceGridImport", {"powerKw": "5", "targetSocPct": "80"}),
                            ("actionForceExport", {"powerKw": "5"}),
                            ("actionForceDaytimeExport", {}),
                            ("actionSetSelfConsumption", {}),
                            ("actionReturnToLocalEms", {})):
            p = _mk_plugin()
            p.flux_executor = _FakeExecutor(owns=True)
            action = MagicMock()
            action.props = props
            getattr(p, name)(action)
            self.assertNotEqual(p.store["flux_manual_preempt"], "", f"{name}")
            self.assertFalse(p.flux_executor.owns_control, f"{name}")

    def test_a_vpp_state_change_pre_empts_before_the_state_machine_writes(self):
        """An Axle window opening must not find a Flux claim still standing."""
        p = _mk_plugin()
        p.flux_executor = _FakeExecutor(owns=True)
        p.store["vpp_state"] = plugin.VPP_IDLE
        p._vpp_transition(plugin.VPP_PRE_CHARGING)
        self.assertFalse(p.flux_executor.owns_control)

    def test_returning_to_idle_does_not_pre_empt(self):
        p = _mk_plugin()
        p.flux_executor = _FakeExecutor(owns=False)
        p.store["vpp_state"] = plugin.VPP_ACTIVE
        p.store["export_active"] = False
        p.store["vpp_event"] = None
        p._vpp_transition(plugin.VPP_IDLE)
        self.assertEqual(p.store.get("flux_manual_preempt", ""), "")

    def test_a_driven_export_window_pre_empts_even_with_no_vpp_state(self):
        """A Saving Session reaches the driver with vpp_state still IDLE."""
        p = _mk_plugin()
        p.flux_executor = _FakeExecutor(owns=True)
        p.store.update({"vpp_is_daytime": False, "vpp_export_submode": None,
                        "vpp_bank_charge_cap_w": -1, "vpp_export_target_w": 4000})
        try:
            p._drive_vpp_export()
        except Exception:
            pass          # the rest of the driver is not under test here
        self.assertFalse(p.flux_executor.owns_control)

    def test_a_relinquishment_that_cannot_be_confirmed_is_reported_not_hidden(self):
        p = _mk_plugin()
        ex = _FakeExecutor(owns=True)
        ex.release_ok = False
        p.flux_executor = ex

        # supervisor_owns still clears the claim, so force the harder case where
        # nothing can be confirmed at all.
        def _stuck(target, now, *, supervisor_owns=False, communications_ok=True):
            ex.calls.append({"target": target, "now": now,
                             "supervisor_owns": supervisor_owns,
                             "communications_ok": communications_ok})
            return "pending"

        ex.step = _stuck
        ex.last_error = "inverter refused"
        self.assertFalse(p._flux_preempt("a Force Grid Import action"))

    def test_pausing_the_manager_takes_the_inverter_off_flux_too(self):
        p = _mk_plugin()
        p.flux_executor = _FakeExecutor(owns=True)
        p._disengage_to_safe_baseline = MagicMock()
        p._ensure_var                 = MagicMock(return_value=None)
        p._sigenergy_folder_id        = MagicMock(return_value=None)
        p._set_manager_paused(True, "test")
        self.assertFalse(p.flux_executor.owns_control)

    def test_resuming_clears_the_manual_flag_but_not_the_cooldown(self):
        p = _mk_plugin()
        p.flux_executor = _FakeExecutor(owns=False)
        p.store["manager_paused"]      = True
        p.store["flux_manual_preempt"] = "the manager being paused (test)"
        p.store["flux_preempted_at"]   = time.time()
        p._disengage_to_safe_baseline = MagicMock()
        p._ensure_var                 = MagicMock(return_value=None)
        p._sigenergy_folder_id        = MagicMock(return_value=None)
        p._set_manager_paused(False, "test")
        self.assertEqual(p.store["flux_manual_preempt"], "")
        self.assertFalse(p._flux_may_claim())


# ================================================================
# Disable and restart recovery (review 4)
# ================================================================

class TestDisabledAndRestartRecovery(unittest.TestCase):

    def test_switching_off_reconciles_before_the_executor_is_dropped(self):
        p  = _mk_plugin(_flux_prefs(fluxEnabled=False))
        ex = _FakeExecutor(owns=True)
        p.flux_executor = ex
        p._flux_supervisor_tick()
        self.assertTrue(any(c["target"] is None for c in ex.calls),
                        "dropped the claim without reconciling")
        self.assertFalse(ex.owns_control)
        self.assertIsNone(p.flux_executor)

    def test_an_unconfirmed_hand_back_keeps_the_executor_for_the_next_try(self):
        """Discarding it would lose the only thing that knows to reconcile."""
        p  = _mk_plugin(_flux_prefs(fluxEnabled=False))
        ex = _FakeExecutor(owns=True)
        ex.release_ok = False
        p.flux_executor = ex
        p._flux_supervisor_tick()
        self.assertIsNotNone(p.flux_executor)
        self.assertTrue(p.flux_executor.owns_control)
        self.assertIn("not confirmed", p.store["flux_status"])

    def test_a_restart_with_a_journal_while_disabled_reconciles_without_trading(self):
        """The claim is durable; the process is not."""
        tmp = tempfile.mkdtemp()
        with open(os.path.join(tmp, "flux_claim.json"), "w") as fh:
            fh.write('{"version": 1, "owns": true, "pending": true}')
        p = _mk_plugin(_flux_prefs(fluxEnabled=False), data_dir=tmp)
        p.flux_executor = None
        built = {}

        def _fake_executor(driver, path, **baseline):
            built["path"] = path
            built["baseline"] = baseline
            return _FakeExecutor(owns=True)

        real = plugin._FluxExecutor
        plugin._FluxExecutor = _fake_executor
        try:
            p._flux_supervisor_tick()
        finally:
            plugin._FluxExecutor = real
        self.assertEqual(built.get("path"), os.path.join(tmp, "flux_claim.json"))
        self.assertIsNone(p.flux_executor, "the reconciled claim was not stood down")
        self.assertFalse(os.path.exists(os.path.join(tmp, "flux_claim.json")))

    def test_no_journal_and_disabled_builds_nothing_at_all(self):
        tmp = tempfile.mkdtemp()
        p = _mk_plugin(_flux_prefs(fluxEnabled=False), data_dir=tmp)
        built = []
        real = plugin._FluxExecutor
        plugin._FluxExecutor = lambda *a, **k: built.append(1)
        try:
            p._flux_supervisor_tick()
        finally:
            plugin._FluxExecutor = real
        self.assertEqual(built, [])
        self.assertIsNone(p.flux_executor)

    def test_the_disabled_path_never_offers_a_target(self):
        p  = _mk_plugin(_flux_prefs(fluxEnabled=False))
        ex = _FakeExecutor(owns=True)
        p.flux_executor = ex
        p._flux_supervisor_tick()
        self.assertEqual(ex.targets(), [])


class TestStartupAndReconfiguration(unittest.TestCase):
    """_init_modules must not disarm a backstop it has not yet read about."""

    def _startup_plugin(self, prefs, journal=None):
        p = _mk_plugin(prefs)
        if journal is not None:
            with open(os.path.join(p.data_dir, "flux_claim.json"), "w") as fh:
                fh.write(journal)
        p.sleep      = lambda *a, **k: None
        p.modbus     = None
        p._flux_rebind_driver = MagicMock(wraps=p._flux_rebind_driver)
        return p

    def _run_init(self, p):
        made = {}

        class _Modbus(_FakeModbus):
            def __init__(self_inner, **kw):
                super().__init__()
                self_inner.kw = kw
                made["modbus"] = self_inner

            def connect(self_inner):
                return True

            def disconnect(self_inner):
                pass

        saved = (plugin.SigenergyModbus, plugin.OctopusAPI,
                 plugin.OpenMeteoForecast, plugin.SIGENERGY_IP)
        plugin.SigenergyModbus   = _Modbus
        plugin.OctopusAPI        = MagicMock()
        plugin.OpenMeteoForecast = MagicMock()
        plugin.SIGENERGY_IP      = "192.0.2.10"
        try:
            p._init_modules()
        finally:
            (plugin.SigenergyModbus, plugin.OctopusAPI,
             plugin.OpenMeteoForecast, plugin.SIGENERGY_IP) = saved
        return made["modbus"]

    def test_an_ordinary_start_still_clears_stale_limits(self):
        """Default behaviour for every install that has never armed Flux."""
        p = self._startup_plugin(_flux_prefs(fluxEnabled=False))
        modbus = self._run_init(p)
        self.assertIn(("charge_cutoff", 100.0), modbus.writes)
        self.assertTrue(any(w[0] == "discharge_limit" for w in modbus.writes
                            if isinstance(w, tuple)))

    def test_a_journal_on_disk_suppresses_the_startup_reset(self):
        """A crash in mode 3 leaves a charge cutoff that is the only backstop.

        Lifting it to 100% before the journal has even been read removes it from
        a mode that may still be running.
        """
        p = self._startup_plugin(_flux_prefs(),
                                 journal='{"version": 1, "owns": true}')
        modbus = self._run_init(p)
        self.assertNotIn(("charge_cutoff", 100.0), modbus.writes)
        self.assertEqual(modbus.writes, [], "startup wrote while a claim was outstanding")

    def test_a_journal_suppresses_the_reset_even_with_the_feature_disabled(self):
        """The claim is durable; switching the feature off does not clear it."""
        p = self._startup_plugin(_flux_prefs(fluxEnabled=False),
                                 journal='{"version": 1, "owns": true}')
        modbus = self._run_init(p)
        self.assertEqual(modbus.writes, [])

    def test_a_live_claim_in_this_process_suppresses_the_reset_too(self):
        p = self._startup_plugin(_flux_prefs())
        p.flux_executor = _FakeExecutor(owns=True)
        modbus = self._run_init(p)
        self.assertEqual(modbus.writes, [])

    def test_an_active_vpp_owner_does_not_stop_the_ordinary_startup_reset(self):
        """Only an outstanding Flux CLAIM suppresses it — not somebody else's window."""
        p = self._startup_plugin(_flux_prefs())
        p.store["vpp_state"] = "active"
        modbus = self._run_init(p)
        self.assertIn(("charge_cutoff", 100.0), modbus.writes)

    def test_the_claim_is_rebound_to_the_new_driver(self):
        """A preference save replaces self.modbus under a live claim."""
        p  = self._startup_plugin(_flux_prefs())
        ex = _FakeExecutor(owns=True)
        ex.raw = object()
        p.flux_executor = ex
        modbus = self._run_init(p)
        self.assertTrue(p.flux_executor.owns_control, "the claim was lost in the swap")
        self.assertIsInstance(ex.raw, plugin._FluxRawDriver)
        self.assertIs(ex.raw._modbus, modbus)

    def test_a_rebind_that_fails_drops_the_executor_rather_than_stranding_it(self):
        p  = _mk_plugin()
        ex = _FakeExecutor(owns=True)
        p.flux_executor = ex
        p.pluginPrefs = None          # makes the adapter construction fail
        p._flux_rebind_driver()
        self.assertIsNone(p.flux_executor)

    def test_recovery_pending_is_false_for_an_install_that_never_armed_flux(self):
        p = _mk_plugin(_flux_prefs(fluxEnabled=False))
        p.flux_executor = None
        self.assertFalse(p._flux_recovery_pending())


class TestLifecycle(unittest.TestCase):

    def test_the_tick_is_a_no_op_when_the_feature_was_never_armed(self):
        p = _mk_plugin(_flux_prefs(fluxEnabled=False))
        p.flux_executor = None
        p._flux_supervisor_tick()
        self.assertIsNone(p.flux_executor)

    def test_a_supervisor_error_releases_control_instead_of_holding_it(self):
        p = _mk_plugin()
        p.flux_executor = _FakeExecutor(owns=True)
        p._flux_supervisor_step = MagicMock(side_effect=RuntimeError("boom"))
        p._flux_supervisor_tick()
        self.assertFalse(p.flux_executor.owns_control)

    def test_shutdown_gives_the_inverter_up_before_its_own_write(self):
        p = _mk_plugin()
        p.web_dashboard = None
        order = []

        class _Watching(_FakeExecutor):
            def step(self_inner, target, now, **kw):
                order.append("flux_release")
                return super().step(target, now, **kw)

        p.flux_executor = _Watching(owns=True)

        class _WatchingModbus(_FakeModbus):
            def set_self_consumption(self_inner):
                order.append("modbus_write")
                return True

            def disconnect(self_inner):
                pass

        p.modbus = _WatchingModbus()
        p.shutdown()
        self.assertEqual(order[:2], ["flux_release", "modbus_write"])

    def test_sleep_gives_the_inverter_up(self):
        p = _mk_plugin()
        p.flux_executor = _FakeExecutor(owns=True)
        p.web_dashboard = None
        try:
            p.prepare_to_sleep()
        except Exception:
            pass
        self.assertFalse(p.flux_executor.owns_control)


# ================================================================
# Rate plumbing and the driver adapter
# ================================================================

class TestTimeOfUseDisplayRate(unittest.TestCase):
    """Live on Flux, the status line and the tariff device both read "Nonep".

    `_rates_for_tariff` fell through to the Tracker bucket for every time-of-use
    tariff, and a TOU bucket holds cheap_p / standard_p / peak_p — none of them
    called today_p. The Tracker bucket is not even filled when Tracker is not
    active, so the answer was None.
    """

    FLUX = {"cheap_start": "02:00", "cheap_end": "05:00",
            "peak_start": "16:00", "peak_end": "19:00",
            "cheap_p": 4.2064, "standard_p": 9.7084, "peak_p": 27.6905}

    def _at(self, hhmm, tou=None, key="flux"):
        """The displayed rate at a local wall time."""
        local = datetime(2026, 9, 17, hhmm[0], hhmm[1], tzinfo=LONDON)
        return plugin._tou_rate_now_p(tou or self.FLUX, local)

    def test_each_band_reports_its_own_published_price(self):
        self.assertAlmostEqual(self._at((3, 0)),  4.2064, places=4)
        self.assertAlmostEqual(self._at((12, 0)), 9.7084, places=4)
        self.assertAlmostEqual(self._at((17, 0)), 27.6905, places=4)

    def test_the_band_boundaries_are_inclusive_at_the_start_exclusive_at_the_end(self):
        self.assertAlmostEqual(self._at((2, 0)),   4.2064, places=4)   # cheap opens
        self.assertAlmostEqual(self._at((4, 59)),  4.2064, places=4)
        self.assertAlmostEqual(self._at((5, 0)),   9.7084, places=4)   # and closes
        self.assertAlmostEqual(self._at((15, 59)), 9.7084, places=4)
        self.assertAlmostEqual(self._at((16, 0)),  27.6905, places=4)  # peak opens
        self.assertAlmostEqual(self._at((18, 59)), 27.6905, places=4)
        self.assertAlmostEqual(self._at((19, 0)),  9.7084, places=4)   # and closes

    def test_the_windows_are_read_in_LOCAL_time_through_both_clock_changes(self):
        """The windows are published as local HH:MM; a UTC clock is an hour out.

        That is the mistake the Go window carried from v5.47.0 to v5.60.0, and it
        buys an hour at the day rate every night for eight months of the year.
        """
        # Hours CHOSEN so a one-hour shift changes the band. 12:00 or 03:00
        # would survive being read as UTC — they are an hour clear of every
        # boundary — so a test built on them proves nothing about the mistake.
        # 02:30 BST read as 01:30 falls out of the cheap band; 16:30 read as
        # 15:30 falls out of the peak; 05:30 read as 04:30 falls back INTO cheap.
        for day, label in ((datetime(2026, 7, 1), "BST"),
                           (datetime(2026, 1, 14), "GMT")):
            for (hour, minute), expected in (((2, 30), 4.2064),
                                             ((5, 30), 9.7084),
                                             ((16, 30), 27.6905),
                                             ((19, 30), 9.7084)):
                local = day.replace(hour=hour, minute=minute, tzinfo=LONDON)
                self.assertAlmostEqual(plugin._tou_rate_now_p(self.FLUX, local),
                                       expected, places=4,
                                       msg=f"{label} {hour:02d}:{minute:02d}")

    def test_a_window_that_wraps_past_midnight_is_handled(self):
        """Intelligent Flux's off-peak runs 19:00-16:00, straight through 00:00."""
        iflux = {"cheap_start": "19:00", "cheap_end": "16:00",
                 "peak_start": "16:00", "peak_end": "19:00",
                 "cheap_p": 5.0, "standard_p": 20.0, "peak_p": 30.0}
        self.assertEqual(plugin._tou_band_now(
            iflux, datetime(2026, 9, 17, 23, 0, tzinfo=LONDON)), "cheap")
        self.assertEqual(plugin._tou_band_now(
            iflux, datetime(2026, 9, 17, 3, 0, tzinfo=LONDON)), "cheap")
        self.assertEqual(plugin._tou_band_now(
            iflux, datetime(2026, 9, 17, 17, 0, tzinfo=LONDON)), "peak")

    def test_a_band_with_no_published_price_reports_nothing_not_another_band(self):
        partial = dict(self.FLUX, peak_p=None)
        local = datetime(2026, 9, 17, 17, 0, tzinfo=LONDON)
        self.assertIsNone(plugin._tou_rate_now_p(partial, local))

    def test_an_empty_bucket_is_not_a_crash(self):
        self.assertIsNone(plugin._tou_rate_now_p({}, datetime(
            2026, 9, 17, 17, 0, tzinfo=LONDON)))
        self.assertEqual(plugin._tou_band_now(None), "standard")

    def test_every_time_of_use_tariff_uses_the_band_not_the_tracker_bucket(self):
        """Go, Intelligent Go, Flux and Intelligent Flux — all four fell through."""
        for key in ("go", "igo", "flux", "iflux"):
            rates = {key: self.FLUX, "tracker": {"today_p": 25.0, "tomorrow_p": 26.0}}
            today, tomorrow = plugin.Plugin._rates_for_tariff(key, rates)
            self.assertIsNotNone(today, f"{key} returned no rate")
            self.assertNotEqual(today, 25.0, f"{key} read the Tracker bucket")
            self.assertIsNone(tomorrow, f"{key} invented a tomorrow rate")

    def test_tracker_and_agile_are_untouched(self):
        rates = {"tracker": {"today_p": 25.0, "tomorrow_p": 26.0},
                 "agile": {"today_p": 13.0}}
        self.assertEqual(plugin.Plugin._rates_for_tariff("tracker", rates),
                         (25.0, 26.0))
        self.assertEqual(plugin.Plugin._rates_for_tariff("agile", rates), (13.0, None))


class TestExportRateReporting(unittest.TestCase):
    """Paired Flux pays three export prices; the flat 12p was never populated.

    `latest_rates_data["export_rate_p"]` had no writer anywhere, so the
    dashboard, the manager snapshot, the VPP revenue estimate and the daily
    history all quoted the Outgoing flat rate whatever the account was on.
    """

    DAY = "2026-09-17"

    def _flux_plugin(self, export_key="flux", valid_from="2026-09-16T23:00:00Z"):
        p = _mk_plugin()
        _seed_flux_day(p, datetime(2026, 9, 17, 17, 0, tzinfo=LONDON))
        ev = _evidence(export_key=export_key)
        ev["export_valid_from"] = valid_from
        p.store["flux_account_evidence"] = ev
        return p

    def _series(self, p, rows):
        """The REAL half-hourly schema: naive LOCAL slot_start/slot_end.

        `_log_halfhourly_to_db_impl` writes `datetime.now().strftime(...)`, so
        these are local wall times with no zone, and they are NOT on the half
        hour — the row is written when the 30-minute tick fires.
        """
        db = os.path.join(p.data_dir, "energy_timeseries.db")
        con = sqlite3.connect(db)
        con.execute("""CREATE TABLE halfhourly (slot_start TEXT, slot_end TEXT,
                                                grid_export_kwh REAL)""")
        con.executemany("INSERT INTO halfhourly VALUES (?, ?, ?)", rows)
        con.commit()
        con.close()

    # --- the live rate -------------------------------------------------

    def test_a_banded_account_is_recognised(self):
        self.assertTrue(self._flux_plugin()._export_tariff_is_banded())
        self.assertFalse(self._flux_plugin(export_key="outgoing")
                         ._export_tariff_is_banded())

    def test_the_live_rate_is_the_band_in_force(self):
        p = self._flux_plugin()
        day = datetime(2026, 9, 17, tzinfo=LONDON).date()
        self.assertAlmostEqual(p._flux_export_band_p(fs._wall(LONDON, day, fs.time(17, 0))),
                               29.6, places=3)
        self.assertAlmostEqual(p._flux_export_band_p(fs._wall(LONDON, day, fs.time(3, 0))),
                               4.7, places=3)

    def test_an_outgoing_account_still_gets_the_flat_rate(self):
        p = self._flux_plugin(export_key="outgoing")
        self.assertEqual(p._export_rate_now_p(), plugin.DEFAULT_EXPORT_RATE_P)

    def test_a_published_feed_rate_beats_the_hardcoded_default(self):
        p = self._flux_plugin(export_key="outgoing")
        p.latest_rates_data["export_rate_p"] = 15.0
        self.assertEqual(p._export_rate_now_p(), 15.0)

    # --- the daily weighting -------------------------------------------

    def test_the_daily_rate_is_weighted_by_when_the_kwh_went_out(self):
        """Two kWh at the peak and two in the cheap band average the two."""
        p = self._flux_plugin()
        self._series(p, [(f"{self.DAY}T03:00:00", f"{self.DAY}T03:30:00", 2.0),
                         (f"{self.DAY}T17:00:00", f"{self.DAY}T17:30:00", 2.0)])
        weighted = p._export_rate_for_day_p(self.DAY)
        self.assertIsNotNone(weighted)
        self.assertAlmostEqual(weighted, (4.7 + 29.6) / 2.0, places=2)

    def test_a_slot_straddling_a_band_boundary_is_apportioned(self):
        """Rows are written by a 30-minute tick, not on the half hour.

        A slot from 15:58 to 16:28 is two minutes of standard rate and 28 of
        peak. Pricing the whole of it at the band its START falls in would value
        the day's biggest export at a third of what it earned.
        """
        p = self._flux_plugin()
        self._series(p, [(f"{self.DAY}T15:58:00", f"{self.DAY}T16:28:00", 1.0)])
        weighted = p._export_rate_for_day_p(self.DAY)
        expected = (10.2 * 2 + 29.6 * 28) / 30.0
        self.assertIsNotNone(weighted)
        self.assertAlmostEqual(weighted, expected, places=2)
        self.assertNotAlmostEqual(weighted, 10.2, places=1)

    def test_a_gap_in_the_published_bands_refuses_the_whole_day(self):
        """Averaging the covered subset and calling it weighted is the bug.

        ISOLATED to the coverage guard: the day is after the agreement started
        and every other slot is priced, so only the partly-covered slot can
        produce the refusal. An earlier version of this test used a day that the
        valid_from guard rejected first, and sabotaging the coverage check left
        it green — it was testing the wrong guard.
        """
        p = self._flux_plugin()
        # Trim the published export schedule to THIS day only, so a slot running
        # past midnight has no band for its second half.
        p.store["flux_export_slots"] = [
            slot for slot in p.store["flux_export_slots"]
            if str(slot["valid_from"]) < f"{self.DAY}T23:00:00Z"]
        # TWO slots: one fully priced, one only half covered. With only the
        # uncovered slot, skipping it and refusing the day are indistinguishable
        # — both leave nothing to weigh. The priced slot is what makes the
        # difference visible: skip-and-average would return a confident 29.6p
        # drawn from the covered half of the day.
        self._series(p, [(f"{self.DAY}T17:00:00", f"{self.DAY}T17:30:00", 2.0),
                         (f"{self.DAY}T23:45:00", "2026-09-18T00:15:00", 2.0)])
        self.assertIsNone(p._export_rate_for_day_p(self.DAY))

    def test_a_fully_covered_day_either_side_of_that_is_still_weighted(self):
        """The control arm: the same shape, fully published, must succeed."""
        p = self._flux_plugin()
        self._series(p, [(f"{self.DAY}T23:45:00", "2026-09-18T00:15:00", 2.0)])
        self.assertIsNotNone(p._export_rate_for_day_p(self.DAY))

    def test_a_day_before_the_agreement_started_is_not_valued_at_flux_bands(self):
        """16 September was billed Outgoing at a flat 12p and must stay that way.

        ISOLATED to the valid_from guard: the bands ARE published for 16 Sep in
        this fixture, so coverage cannot be what refuses it. Publishing bands for
        a day the account was not yet on the tariff is exactly the situation —
        Octopus will serve the Flux schedule for any date you ask.
        """
        p = self._flux_plugin(valid_from="2026-09-16T23:00:00Z")   # 17 Sep 00:00 BST
        _seed_flux_day(p, datetime(2026, 9, 16, 17, 0, tzinfo=LONDON))  # 16 + 17 Sep
        ev = _evidence()
        ev["export_valid_from"] = "2026-09-16T23:00:00Z"
        p.store["flux_account_evidence"] = ev
        self._series(p, [("2026-09-16T17:00:00", "2026-09-16T17:30:00", 2.0)])
        self.assertIsNotNone(p._flux_export_band_p(
            fs._wall(LONDON, datetime(2026, 9, 16).date(), fs.time(17, 0))),
            "fixture must publish bands for the day, or this tests coverage")
        self.assertIsNone(p._export_rate_for_day_p("2026-09-16"))

    def test_the_first_day_of_the_agreement_is_valued_at_flux_bands(self):
        """The other side of the same boundary — it must not refuse everything."""
        p = self._flux_plugin(valid_from="2026-09-16T23:00:00Z")
        self._series(p, [(f"{self.DAY}T17:00:00", f"{self.DAY}T17:30:00", 2.0)])
        self.assertIsNotNone(p._export_rate_for_day_p(self.DAY))

    def test_an_unreadable_row_refuses_the_day_rather_than_skipping_it(self):
        p = self._flux_plugin()
        # Lexically INSIDE the day, so the query selects it — a malformed row
        # outside the range is simply not part of the day and proves nothing.
        self._series(p, [(f"{self.DAY}T17:00:00", f"{self.DAY}T17:30:00", 2.0),
                         (f"{self.DAY}T17:BAD:00", f"{self.DAY}T17:BAD:30", 2.0)])
        self.assertIsNone(p._export_rate_for_day_p(self.DAY))

    def test_a_flat_tariff_is_not_weighted_at_all(self):
        p = self._flux_plugin(export_key="outgoing")
        self._series(p, [(f"{self.DAY}T17:00:00", f"{self.DAY}T17:30:00", 2.0)])
        self.assertIsNone(p._export_rate_for_day_p(self.DAY))

    def test_a_day_with_no_exports_returns_no_weighted_rate(self):
        p = self._flux_plugin()
        self._series(p, [])
        self.assertIsNone(p._export_rate_for_day_p(self.DAY))

    def test_a_missing_series_returns_none_rather_than_a_guess(self):
        self.assertIsNone(self._flux_plugin()._export_rate_for_day_p(self.DAY))

    # --- how the figure is labelled ------------------------------------

    def _basis(self, p, date_str):
        """The basis the daily record would carry. Real code, not a replica.

        `_export_rate_and_basis` is the single owner of that decision and is what
        `_write_daily_history` calls, so driving it directly tests the shipped
        logic without seeding the thirty-odd store keys the whole writer reads.
        """
        return p._export_rate_and_basis(date_str, plugin.DEFAULT_EXPORT_RATE_P)[1]

    def test_a_weighted_day_is_labelled_weighted(self):
        p = self._flux_plugin()
        self._series(p, [(f"{self.DAY}T17:00:00", f"{self.DAY}T17:30:00", 2.0)])
        self.assertEqual(self._basis(p, self.DAY), "weighted")

    def test_a_banded_day_that_cannot_be_weighted_is_labelled_estimated(self):
        """Not 'flat'. A flat label on a banded tariff reads as exact."""
        p = self._flux_plugin()          # no series at all
        self.assertEqual(self._basis(p, self.DAY), "estimated")

    def test_a_genuinely_flat_tariff_is_labelled_flat(self):
        p = self._flux_plugin(export_key="outgoing")
        self.assertEqual(self._basis(p, self.DAY), "flat")

    # --- plumbing -------------------------------------------------------

    def test_a_flux_account_gets_its_export_schedule_even_with_the_controller_off(self):
        """Reporting needs the bands, not just trading."""
        p = _mk_plugin(_flux_prefs(fluxEnabled=False, fluxCommissioned=False))
        p.octopus = MagicMock()
        p.octopus.get_current_tariff.return_value = {"tariff_key": "flux"}
        p.octopus.get_all_monitored_rates.return_value = {}
        p.octopus.get_agile_rates.return_value = []
        p._update_tariff_device = MagicMock()
        p._write_tariff_schedule_variables = MagicMock()
        p._refresh_flux_rates = MagicMock()
        p._refresh_octopus_rates()
        p._refresh_flux_rates.assert_called_once()

    def test_a_tracker_account_with_the_controller_off_fetches_nothing_extra(self):
        p = _mk_plugin(_flux_prefs(fluxEnabled=False, fluxCommissioned=False))
        p.octopus = MagicMock()
        p.octopus.get_current_tariff.return_value = {"tariff_key": "tracker"}
        p.octopus.get_all_monitored_rates.return_value = {}
        p.octopus.get_agile_rates.return_value = []
        p._update_tariff_device = MagicMock()
        p._write_tariff_schedule_variables = MagicMock()
        p._refresh_flux_rates = MagicMock()
        p._refresh_octopus_rates()
        p._refresh_flux_rates.assert_not_called()

    def test_a_rehearsal_override_does_not_hide_a_real_flux_account(self):
        p = _mk_plugin(_flux_prefs(fluxEnabled=False, fluxCommissioned=False))
        p.octopus = MagicMock()
        p.octopus.get_current_tariff.return_value = {
            "tariff_key": "agile", "overridden": True, "detected_key": "flux"}
        p.octopus.get_all_monitored_rates.return_value = {}
        p.octopus.get_agile_rates.return_value = []
        p._update_tariff_device = MagicMock()
        p._write_tariff_schedule_variables = MagicMock()
        p._refresh_flux_rates = MagicMock()
        p._refresh_octopus_rates()
        p._refresh_flux_rates.assert_called_once()

    def test_the_rate_is_published_AFTER_the_schedules_it_reads(self):
        """Published first, the FIRST refresh after every restart quotes 12p.

        The bands it needs arrive in _refresh_flux_rates, so the order is the
        difference between a correct figure today and a correct figure tomorrow.
        """
        p = _mk_plugin()
        order = []
        p.octopus = MagicMock()
        p.octopus.get_current_tariff.return_value = {"tariff_key": "flux"}
        p.octopus.get_all_monitored_rates.return_value = {}
        p.octopus.get_agile_rates.return_value = []
        p._update_tariff_device = MagicMock()
        p._write_tariff_schedule_variables = MagicMock()

        def _fetch(force=False):
            order.append("fetch")
            # SEEDED FOR TODAY, because what follows reads the REAL clock.
            # Pinned to 17-Sep-2026 this passed on the 17th and the 18th and
            # went red on the 19th, when the seeded bands no longer covered
            # "now" and the published figure fell back to the 12p default --
            # which is the very fault the test exists to catch, so it failed
            # for the right reason and the wrong cause.
            _seed_flux_day(p, datetime.now(LONDON))
            ev = _evidence()
            ev["export_valid_from"] = "2026-09-16T23:00:00Z"
            p.store["flux_account_evidence"] = ev

        p._refresh_flux_rates = _fetch
        real_now = p._export_rate_now_p

        def _publish():
            order.append("publish")
            return real_now()

        p._export_rate_now_p = _publish
        p._refresh_octopus_rates()
        self.assertEqual(order, ["fetch", "publish"])
        self.assertNotEqual(p.latest_rates_data["export_rate_p"],
                            plugin.DEFAULT_EXPORT_RATE_P)


class TestRateSpans(unittest.TestCase):

    def test_published_slots_become_spans(self):
        p = _mk_plugin()
        _seed_flux_day(p, datetime(2026, 9, 16, 3, 0, tzinfo=LONDON))
        spans = p._flux_rate_spans("flux_import_slots")
        self.assertEqual(len(spans), 10)
        self.assertTrue(all(s.end > s.start for s in spans))

    def test_an_open_ended_slot_is_dropped_rather_than_guessed(self):
        p = _mk_plugin()
        p.store["flux_import_slots"] = [
            {"valid_from": "2026-09-16T01:00:00Z", "valid_to": None,
             "value_inc_vat": 20.0}]
        self.assertEqual(p._flux_rate_spans("flux_import_slots"), [])

    def test_a_malformed_slot_does_not_take_the_others_with_it(self):
        p = _mk_plugin()
        p.store["flux_import_slots"] = [
            {"valid_from": "not a date", "valid_to": "2026-09-16T05:00:00Z",
             "value_inc_vat": 16.8},
            {"valid_from": "2026-09-16T01:00:00Z", "valid_to": "2026-09-16T04:00:00Z",
             "value_inc_vat": 16.8}]
        self.assertEqual(len(p._flux_rate_spans("flux_import_slots")), 1)

    def test_the_slots_survive_an_octopus_refresh_that_rebuilds_the_rates_dict(self):
        p = _mk_plugin()
        _seed_flux_day(p, datetime(2026, 9, 16, 3, 0, tzinfo=LONDON))
        p.latest_rates_data = {"tariff_info": {"tariff_key": "flux"}}
        self.assertEqual(len(p._flux_rate_spans("flux_import_slots")), 10)

    def test_a_failed_fetch_keeps_the_last_good_schedule_and_timestamp(self):
        p = _mk_plugin()
        _seed_flux_day(p, datetime(2026, 9, 16, 3, 0, tzinfo=LONDON))
        stamp = p.store["flux_rates_at"] = time.time() - 100
        p.octopus = MagicMock()
        p.octopus.get_export_agreement.return_value = {"tariff_key": "flux",
                                                       "product_code": "FLUX-EXPORT-1"}
        p.octopus._probe_product_by_prefix.return_value = "FLUX-IMPORT-1"
        p.octopus._build_tariff_code.return_value = "E-1R-X-F"
        p.octopus._fetch_rate_schedule.return_value = []
        p._refresh_flux_rates()
        self.assertEqual(p.store["flux_rates_at"], stamp)
        self.assertEqual(len(p._flux_rate_spans("flux_import_slots")), 10)
        self.assertIn("empty", p.store["flux_rates_problem"])


class TestRawDriver(unittest.TestCase):

    def test_the_inverter_maximum_comes_from_the_preference(self):
        d = plugin._FluxRawDriver(_FakeModbus(), {"inverterMaxKw": "8.5"})
        self.assertEqual(d.inverter_max_w, 8500)

    def test_writes_return_real_booleans(self):
        d = plugin._FluxRawDriver(_FakeModbus(), {"inverterMaxKw": "10"})
        for value in (d.set_charge_limit(1000), d.set_discharge_limit(1000),
                      d.set_charge_cutoff(90.0), d.set_discharge_cutoff(20.0),
                      d.set_remote_ems_mode(2), d.enable_remote_ems()):
            self.assertIs(value, True)

    def test_the_charge_limit_write_is_quiet_so_only_one_verifier_speaks(self):
        m = _FakeModbus()
        plugin._FluxRawDriver(m, {"inverterMaxKw": "10"}).set_charge_limit(1234)
        self.assertIn(("charge_limit", 1234, True), m.writes)

    def test_the_adapter_covers_everything_the_executor_asks_the_driver_for(self):
        """Read the EXECUTOR's source for the surface it uses, not a list of it."""
        import inspect
        import re

        import flux_execution as fe
        used = set(re.findall(r"(?:self\.raw|\bd)\.([a-z_]+)", inspect.getsource(fe)))
        driver = plugin._FluxRawDriver(_FakeModbus(), {"inverterMaxKw": "10"})
        missing = sorted(n for n in used if not hasattr(driver, n))
        self.assertEqual(missing, [], f"the adapter is missing {missing}")
        self.assertIn("read_remote_ems_enabled", used,
                      "the surface scan found nothing — the regex stopped matching")

    def test_the_real_modbus_driver_has_every_method_the_adapter_forwards(self):
        import sigenergy_modbus
        for name in ("set_charge_limit", "set_discharge_limit", "set_charge_cutoff",
                     "set_discharge_cutoff", "set_remote_ems_mode", "enable_remote_ems",
                     "read_ems_mode", "read_charge_limit", "read_discharge_limit",
                     "read_charge_cutoff", "read_discharge_cutoff",
                     "read_remote_ems_enabled", "read_battery_soc",
                     "read_power_flows"):
            self.assertTrue(hasattr(sigenergy_modbus.SigenergyModbus, name),
                            f"SigenergyModbus has no {name}")


class TestImportBandsAndTiers(unittest.TestCase):
    """v5.110.0: paired Flux bills IMPORT by band too.

    Before this, a Flux day's imports were valued at whichever band was in force
    when the figure was read, so the same kWh cost 16.8p, 31.4p or 44.9p (these
    fixture prices) depending on the time somebody opened the page.
    """

    DAY = "2026-09-17"

    def _plugin(self, import_key="flux", valid_from="2026-09-16T23:00:00Z"):
        p = _mk_plugin()
        _seed_flux_day(p, datetime(2026, 9, 17, 17, 0, tzinfo=LONDON))
        ev = _evidence(import_key=import_key)
        ev["import_valid_from"] = valid_from
        ev["export_valid_from"] = valid_from
        p.store["flux_account_evidence"] = ev
        return p

    def _series(self, p, rows):
        """Both columns, naive LOCAL wall times, as the real writer stores them."""
        con = sqlite3.connect(os.path.join(p.data_dir, "energy_timeseries.db"))
        con.execute("""CREATE TABLE halfhourly (slot_start TEXT, slot_end TEXT,
                         grid_import_kwh REAL, grid_export_kwh REAL)""")
        con.executemany("INSERT INTO halfhourly VALUES (?, ?, ?, ?)", rows)
        con.commit()
        con.close()

    def test_import_is_weighted_by_when_it_was_bought(self):
        p = self._plugin()
        self._series(p, [(f"{self.DAY}T03:00:00", f"{self.DAY}T03:30:00", 2.0, 0.0),
                         (f"{self.DAY}T17:00:00", f"{self.DAY}T17:30:00", 2.0, 0.0)])
        rate, basis = p._import_rate_and_basis(self.DAY, None)
        self.assertEqual(basis, "weighted")
        self.assertAlmostEqual(rate, (16.8 + 44.9) / 2.0, places=3)

    def test_import_and_export_columns_are_not_confused(self):
        """Control arm: the export side of the same rows is weighted on its own kWh."""
        p = self._plugin()
        self._series(p, [(f"{self.DAY}T03:00:00", f"{self.DAY}T03:30:00", 2.0, 0.0),
                         (f"{self.DAY}T17:00:00", f"{self.DAY}T17:30:00", 0.0, 1.0)])
        self.assertAlmostEqual(p._import_rate_for_day_p(self.DAY), 16.8, places=3)
        self.assertAlmostEqual(p._export_rate_for_day_p(self.DAY), 29.6, places=3)

    def test_the_day_before_flux_keeps_its_tracker_rate(self):
        """16 September was Tracker. Its own saved rate stands, labelled flat."""
        p = self._plugin()
        _seed_flux_day(p, datetime(2026, 9, 16, 17, 0, tzinfo=LONDON))
        ev = _evidence()
        ev["import_valid_from"] = "2026-09-16T23:00:00Z"
        p.store["flux_account_evidence"] = ev
        self._series(p, [("2026-09-16T17:00:00", "2026-09-16T17:30:00", 2.0, 0.0)])
        self.assertIsNone(p._import_rate_for_day_p("2026-09-16"))
        self.assertEqual(p._import_rate_and_basis("2026-09-16", 26.21), (26.21, "estimated"))

    def test_a_banded_day_with_no_import_still_carries_a_rate(self):
        """No rate at all reads as "rate missing"; the day's time-average does not."""
        p = self._plugin()
        self._series(p, [(f"{self.DAY}T12:00:00", f"{self.DAY}T12:30:00", 0.0, 2.0)])
        rate, basis = p._import_rate_and_basis(self.DAY, None)
        self.assertEqual(basis, "time-average")
        self.assertAlmostEqual(rate, (3 * 16.8 + 3 * 44.9 + 18 * 31.4) / 24.0, places=3)

    def test_tracker_import_is_left_flat(self):
        p = self._plugin(import_key="tracker")
        self._series(p, [(f"{self.DAY}T17:00:00", f"{self.DAY}T17:30:00", 2.0, 0.0)])
        self.assertEqual(p._import_rate_and_basis(self.DAY, 26.21), (26.21, "flat"))

    # --- the tiers the Costs page shows --------------------------------

    def test_three_import_tiers_with_their_local_times(self):
        p = self._plugin()
        now = fs._wall(LONDON, datetime(2026, 9, 17).date(), fs.time(17, 0))
        tiers = p._band_tiers("import", self.DAY, now_utc=now)
        self.assertEqual([t["label"] for t in tiers], ["Off-peak", "Day", "Peak"])
        self.assertEqual([t["p"] for t in tiers], [16.8, 31.4, 44.9])
        self.assertEqual(tiers[0]["windows"], [{"start": "02:00", "end": "05:00",
                                                "current": False}])
        # The overnight day band is ONE window, not 00:00-02:00 plus 19:00-00:00.
        self.assertEqual([(w["start"], w["end"]) for w in tiers[1]["windows"]],
                         [("05:00", "16:00"), ("19:00", "02:00")])
        self.assertEqual([t["current"] for t in tiers], [False, False, True])

    def test_export_tiers_use_the_export_prices(self):
        p = self._plugin()
        tiers = p._band_tiers("export", self.DAY)
        self.assertEqual([t["p"] for t in tiers], [4.7, 10.2, 29.6])

    def test_no_tiers_for_a_flat_side(self):
        self.assertEqual(self._plugin(import_key="tracker")._band_tiers("import", self.DAY), [])

    def test_a_gap_in_the_bands_shows_no_tiers_rather_than_invented_windows(self):
        p = self._plugin()
        p.store["flux_import_slots"] = [
            s for s in p.store["flux_import_slots"]
            if not str(s["valid_from"]).startswith(f"{self.DAY}T15:00")]
        self.assertEqual(p._band_tiers("import", self.DAY), [])

    def test_the_overnight_window_is_current_either_side_of_midnight(self):
        p = self._plugin()
        late = fs._wall(LONDON, datetime(2026, 9, 17).date(), fs.time(23, 30))
        early = fs._wall(LONDON, datetime(2026, 9, 17).date(), fs.time(1, 0))
        for when in (late, early):
            tiers = p._band_tiers("import", self.DAY, now_utc=when)
            self.assertTrue(tiers[1]["current"], when)


class TestTariffSidesPayload(unittest.TestCase):
    """What /api/status hands the Costs page's Rates tiles."""

    def test_both_sides_carry_names_and_tiers(self):
        p = _mk_plugin()
        # _tariff_sides_payload reads the real clock, so the bands are seeded
        # for today. See the note in TestExportRateReporting.
        _seed_flux_day(p, datetime.now(LONDON))
        p.octopus = MagicMock()
        p.octopus.get_account_financials.return_value = {
            "elec":   {"display_name": "Octopus Flux Import",
                       "tariff_code": "E-1R-FLUX-IMPORT-23-02-14-F"},
            "export": {"display_name": "Octopus Flux Export",
                       "tariff_code": "E-1R-FLUX-EXPORT-23-02-14-F"}}
        out = p._tariff_sides_payload(12.0)
        self.assertEqual(out["import_side"]["name"], "Octopus Flux Import")
        self.assertEqual(out["export_side"]["name"], "Octopus Flux Export")
        self.assertTrue(out["import_side"]["banded"])
        self.assertEqual(len(out["import_side"]["tiers"]), 3)
        self.assertEqual(len(out["export_side"]["tiers"]), 3)

    def test_a_flat_export_side_reports_its_one_rate(self):
        p = _mk_plugin()
        _seed_flux_day(p, datetime(2026, 9, 17, 17, 0, tzinfo=LONDON))
        p.store["flux_account_evidence"] = _evidence(export_key="outgoing")
        p.octopus = None
        out = p._tariff_sides_payload(12.0)
        self.assertFalse(out["export_side"]["banded"])
        self.assertEqual(out["export_side"]["now_p"], 12.0)
        self.assertEqual(out["export_side"]["tiers"], [])


class TestBackupReserveMoveIsNotDrift(unittest.TestCase):
    """21-Sep-2026: pre-charge wrote 25.7% at 18:00:56, a Saving Session began at
    18:01:13, and verify's correct move to 20% was logged as a WARNING about drift."""

    def _verify(self, actual, written):
        p = _mk_plugin()
        p.store["export_active"] = False
        p.store["import_active"] = False
        p.modbus.read_backup_soc = lambda: actual
        if written is not None:
            p.store["backup_reserve_written"] = written
        lines = []
        real = plugin.log
        plugin.log = lambda msg, level="INFO", **kw: lines.append((level, msg))
        try:
            p._verify_ems_registers()
        finally:
            plugin.log = real
        reserve = [(lv, m) for lv, m in lines if "Backup reserve" in m]
        return p, reserve

    def test_our_own_earlier_value_moves_quietly(self):
        p, lines = self._verify(25.7, 25.7)
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0][0], "INFO")
        self.assertIn(("backup_soc", p._policy_discharge_floor_pct()), p.modbus.writes)

    def test_a_value_we_did_not_write_still_warns(self):
        _, lines = self._verify(25.7, 30.0)
        self.assertEqual(lines[0][0], "WARNING")

    def test_nothing_written_since_start_still_warns(self):
        _, lines = self._verify(25.7, None)
        self.assertEqual(lines[0][0], "WARNING")

    def test_the_correction_is_remembered(self):
        p, _ = self._verify(25.7, None)
        self.assertEqual(p.store["backup_reserve_written"],
                         round(p._policy_discharge_floor_pct(), 1))

    def test_the_flux_driver_reports_what_it_wrote(self):
        seen = []
        d = plugin._FluxRawDriver(_FakeModbus(), {"inverterMaxKw": "10"},
                                  on_backup_written=seen.append)
        self.assertTrue(d.set_discharge_cutoff(37.6))
        self.assertEqual(seen, [37.6])


if __name__ == "__main__":
    unittest.main()
