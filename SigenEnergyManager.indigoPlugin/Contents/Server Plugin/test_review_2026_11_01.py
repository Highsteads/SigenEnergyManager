#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    test_review_2026_11_01.py
# Description: Pins the fixes from the 01-11-2026 code review (v5.141.0): the
#              shutdown hand-back, the restart re-assert of a raised floor, Axle
#              reschedules, an end time for manager grid imports, the pause/sleep
#              feature flags, and cached forecasts dated by their real age. (The
#              Flux price fetch and the zero solar factor are pinned beside their
#              neighbours in test_flux_supervisor.py and test_flux_strategy.py.)
# Author:      CliveS & Claude Opus 5.5
# Date:        01-11-2026
# Version:     1.0

import json
import os
import sys
import tempfile
import time
import types
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import test_plugin_daily_energy as DE      # noqa: E402  (the repo's own plugin factory)
import plugin                              # noqa: E402
import sigenergy_modbus                    # noqa: E402
import openmeteo_forecast as omf           # noqa: E402
from battery_manager import (              # noqa: E402
    ACTION_SELF_CONSUMPTION, Decision, TariffData, TARIFF_FLUX, TARIFF_GO, TARIFF_AGILE)


def _mk():
    p = DE._mk(tempfile.mkdtemp())
    p.debug = False
    return p


def _modbus(connected=True, sc_ok=True):
    m = MagicMock()
    m.connected = connected
    m.set_self_consumption.return_value = sc_ok
    m.set_discharge_cutoff.return_value = True
    m.set_charge_cutoff.return_value    = True
    return m


def _warnings(logged):
    return [c.args[0] for c in logged.call_args_list if c.kwargs.get("level") == "WARNING"]


# ================================================================
# Finding 1 — shutdown hands the inverter back fully
# ================================================================

class TestShutdownHandBack(unittest.TestCase):

    def _plugin(self, modbus):
        p = _mk()
        p.pluginPrefs["batteryHealthCutoff"] = "5"
        p.web_dashboard  = None
        p.flux_executor  = None
        p._flux_release  = lambda why: ""
        p._owns_backup_reserve = lambda: False
        p.modbus = modbus
        return p

    def test_shutdown_releases_both_cutoffs_after_self_consumption(self):
        """Before 5.141.0 shutdown reset only the mode and power limits, leaving
        an import's charge cutoff (e.g. 83%) and a flood/VPP discharge floor
        (e.g. 30%) on the inverter until the plugin next started."""
        m = _modbus()
        p = self._plugin(m)
        with patch.object(plugin, "log"):
            p.shutdown()
        m.set_self_consumption.assert_called_once()
        m.set_discharge_cutoff.assert_called_once_with(5.0)
        m.set_charge_cutoff.assert_called_once_with(100.0)
        m.disconnect.assert_called()

    def test_the_backup_reserve_is_reset_when_the_plugin_owns_it(self):
        m = _modbus()
        p = self._plugin(m)
        p._owns_backup_reserve = lambda: True
        p._policy_discharge_floor_pct = lambda dispatch_event=None: 20.0
        p._set_backup_reserve = MagicMock(return_value=True)
        with patch.object(plugin, "log"):
            p._hand_back_for_stop("")
        p._set_backup_reserve.assert_called_once_with(20.0)

    def test_a_link_marked_down_gets_one_quick_reconnect(self):
        m = _modbus(connected=False)

        def _connect(**kw):
            m.connected = True
            return True
        m.connect.side_effect = _connect
        p = self._plugin(m)
        with patch.object(plugin, "log"):
            p.shutdown()
        m.connect.assert_called_once_with(timeout=p.SHUTDOWN_CONNECT_TIMEOUT_S,
                                          retries=0, ignore_backoff=True)
        m.set_self_consumption.assert_called_once()
        m.set_charge_cutoff.assert_called_once_with(100.0)

    def test_an_unreachable_inverter_is_a_warning_not_silence(self):
        m = _modbus(connected=False)
        m.connect.return_value = False
        p = self._plugin(m)
        with patch.object(plugin, "log") as logged:
            p.shutdown()
        m.set_self_consumption.assert_not_called()
        self.assertTrue(any("Could not reach the inverter" in w for w in _warnings(logged)))

    def test_a_failed_mode_hand_back_keeps_the_backstops_and_says_so(self):
        """Lifting the charge cutoff over a charge that is still running would
        remove its only backstop, so the floors stay when the mode did not land."""
        m = _modbus(sc_ok=False)
        p = self._plugin(m)
        with patch.object(plugin, "log") as logged:
            p.shutdown()
        m.set_charge_cutoff.assert_not_called()
        m.set_discharge_cutoff.assert_not_called()
        self.assertTrue(any("NOT fully handed back" in w for w in _warnings(logged)))

    def test_an_exception_is_logged_not_swallowed(self):
        m = _modbus()
        m.set_self_consumption.side_effect = RuntimeError("socket gone")
        p = self._plugin(m)
        with patch.object(plugin, "log") as logged:
            p.shutdown()
        self.assertTrue(any("socket gone" in w for w in _warnings(logged)))

    def test_a_confirmed_flux_release_writes_nothing_more(self):
        """5.139.1 kept: the release already wrote the baseline and read it back."""
        m = _modbus()
        p = self._plugin(m)
        p._flux_release = lambda why: "released"
        with patch.object(plugin, "log"):
            p.shutdown()
        m.set_self_consumption.assert_not_called()
        m.set_charge_cutoff.assert_not_called()
        m.disconnect.assert_called()

    def test_shutdown_does_not_clear_the_resume_flags(self):
        """A restart mid-event must still resume it, so only hardware changes."""
        m = _modbus()
        p = self._plugin(m)
        p.store.update({"vpp_cutoff_raised": True, "flood_prev_target_soc": 40.0})
        with patch.object(plugin, "log"):
            p._hand_back_for_stop("")
        self.assertTrue(p.store["vpp_cutoff_raised"])
        self.assertEqual(p.store["flood_prev_target_soc"], 40.0)


class TestDriverConnectForShutdown(unittest.TestCase):

    def _driver(self):
        m = sigenergy_modbus.SigenergyModbus(ip="192.0.2.1", port=502, plant_address=247,
                                             inverter_address=1, logger=MagicMock())
        m._last_connect_attempt = time.monotonic()      # inside the back-off
        return m

    def test_the_back_off_still_applies_by_default(self):
        m = self._driver()
        with patch.object(sigenergy_modbus, "PYMODBUS_AVAILABLE", True), \
                patch.object(sigenergy_modbus, "ModbusTcpClient", create=True) as cls:
            self.assertFalse(m.connect())
        cls.assert_not_called()

    def test_ignore_backoff_tries_at_once_with_the_given_timeout(self):
        m = self._driver()
        with patch.object(sigenergy_modbus, "PYMODBUS_AVAILABLE", True), \
                patch.object(sigenergy_modbus, "ModbusTcpClient", create=True) as cls:
            cls.return_value.connect.return_value = False
            m.connect(timeout=3, retries=0, ignore_backoff=True)
        cls.assert_called_once()
        self.assertEqual(cls.call_args.kwargs["timeout"], 3)
        self.assertEqual(cls.call_args.kwargs["retries"], 0)


class TestRaisedFloorAfterRestart(unittest.TestCase):
    """Shutdown now lowers 40048, so a VPP window or flood drain resumed after a
    restart must have its floor put back — the verify pass leaves a flagged
    floor to its owner, which wrote it before the restart."""

    def test_a_resumed_vpp_window_flags_its_floor_for_restoring(self):
        p = _mk()
        now = datetime.now(timezone.utc)
        p.store.update({"vpp_state": plugin.VPP_ACTIVE, "vpp_cutoff_raised": True,
                        "vpp_event": {"start_time": now - timedelta(minutes=10),
                                      "end_time": now + timedelta(minutes=50),
                                      "duration_hrs": 1.0}})
        with patch.object(plugin, "vpp_log"):
            p._rehydrate_vpp_state()
        self.assertTrue(p.store.get("raised_floor_unconfirmed"))

    def test_the_vpp_floor_is_written_back_once(self):
        p = _mk()
        p.store.update({"vpp_cutoff_raised": True, "vpp_is_daytime": False,
                        "raised_floor_unconfirmed": True})
        p._vpp_floor_pct = MagicMock(return_value=(15.0, "night event"))
        p.modbus = _modbus()
        p.modbus.read_discharge_cutoff.return_value = 5.0
        with patch.object(plugin, "log"):
            p._reassert_raised_floor()
        p._vpp_floor_pct.assert_called_once_with(False)
        p.modbus.set_discharge_cutoff.assert_called_once_with(15.0)
        self.assertFalse(p.store["raised_floor_unconfirmed"])

    def test_a_flood_floor_is_written_back(self):
        p = _mk()
        p.pluginPrefs["batteryHealthCutoff"] = "5"
        p._grid_outage_active = lambda: False
        p.store.update({"flood_prev_target_soc": 40.0, "raised_floor_unconfirmed": True})
        p.modbus = _modbus()
        p.modbus.read_discharge_cutoff.return_value = 5.0
        with patch.object(plugin, "log"):
            p._reassert_raised_floor()
        p.modbus.set_discharge_cutoff.assert_called_once_with(40.0)

    def test_a_floor_already_in_place_is_not_rewritten(self):
        p = _mk()
        p.store.update({"vpp_cutoff_raised": True, "raised_floor_unconfirmed": True})
        p._vpp_floor_pct = MagicMock(return_value=(15.0, "night event"))
        p.modbus = _modbus()
        p.modbus.read_discharge_cutoff.return_value = 15.0
        p._reassert_raised_floor()
        p.modbus.set_discharge_cutoff.assert_not_called()
        self.assertFalse(p.store["raised_floor_unconfirmed"])

    def test_a_failed_write_is_retried_next_pass(self):
        p = _mk()
        p.store.update({"vpp_cutoff_raised": True, "raised_floor_unconfirmed": True})
        p._vpp_floor_pct = MagicMock(return_value=(15.0, "night event"))
        p.modbus = _modbus()
        p.modbus.read_discharge_cutoff.return_value = None
        p.modbus.set_discharge_cutoff.return_value = False
        p._reassert_raised_floor()
        self.assertTrue(p.store["raised_floor_unconfirmed"])


# ================================================================
# Finding 2 — Axle reschedules before the window opens
# ================================================================

class TestAxleReschedule(unittest.TestCase):

    def _plugin(self, state, start_in, length_min=60):
        p = _mk()
        for n in ("_restore_discharge_cutoff", "_update_vpp_device", "_flux_preempt",
                  "_trigger_event", "_drive_vpp_export", "_set_vpp_discharge_cutoff",
                  "_write_vpp_event_header", "_log_vpp_snapshot", "_start_vpp_precharge",
                  "_record_vpp_api_status", "_end_vpp_export"):
            setattr(p, n, MagicMock())
        now = datetime.now(timezone.utc)
        ev = {"start_time": now + start_in,
              "end_time": now + start_in + timedelta(minutes=length_min),
              "duration_hrs": length_min / 60.0, "import_export": "export"}
        p.store.update({"vpp_state": state, "vpp_event": ev, "export_active": False,
                        "vpp_active": False, "grid_export_daily_kwh": 5.0,
                        "vpp_10min_warning_sent": True})
        p.pluginPrefs["axleEnabled"] = True
        p.axle = MagicMock()
        p.axle.last_error = None
        return p, now

    def _answer(self, p, now, start_in, length_min=60):
        ev = {"start_time": now + start_in,
              "end_time": now + start_in + timedelta(minutes=length_min),
              "duration_hrs": length_min / 60.0, "import_export": "export"}
        p.axle.get_next_event.return_value = ev
        return ev

    def test_an_event_moved_earlier_adopts_the_new_end(self):
        """Old code kept the announced window, so the export ran to the OLD end."""
        p, now = self._plugin(plugin.VPP_ANNOUNCED, timedelta(hours=3))
        new = self._answer(p, now, timedelta(hours=2))
        with patch.object(plugin, "vpp_log") as logged:
            p._poll_vpp()
        self.assertEqual(p.store["vpp_event"]["end_time"], new["end_time"])
        self.assertFalse(p.store["vpp_10min_warning_sent"])
        self.assertTrue(any("rescheduled" in c.args[0] for c in logged.call_args_list))

    def test_an_unchanged_event_is_left_alone(self):
        p, now = self._plugin(plugin.VPP_ANNOUNCED, timedelta(hours=3))
        kept = p.store["vpp_event"]
        p.axle.get_next_event.return_value = dict(kept)
        with patch.object(plugin, "vpp_log"):
            p._poll_vpp()
        self.assertIs(p.store["vpp_event"], kept)
        p._save_accumulators.assert_not_called()

    def test_moved_well_later_during_pre_charge_goes_back_to_waiting(self):
        p, now = self._plugin(plugin.VPP_PRE_CHARGING, timedelta(minutes=20))
        new = self._answer(p, now, timedelta(hours=2))
        with patch.object(plugin, "vpp_log"):
            p._poll_vpp()
        self.assertEqual(p.store["vpp_state"], plugin.VPP_ANNOUNCED)
        self.assertEqual(p.store["vpp_event"]["start_time"], new["start_time"])

    def test_an_active_window_is_never_moved_by_the_api(self):
        p, now = self._plugin(plugin.VPP_ACTIVE, timedelta(minutes=-10))
        kept_end = p.store["vpp_event"]["end_time"]
        self._answer(p, now, timedelta(hours=20))           # tomorrow's event
        with patch.object(plugin, "vpp_log"):
            p._poll_vpp()
        self.assertEqual(p.store["vpp_event"]["end_time"], kept_end)


# ================================================================
# Finding 3 — a manager grid import ends with its cheap window
# ================================================================

def _tariff(key=TARIFF_FLUX, start="02:00", end="05:00"):
    return TariffData(tariff_key=key, cheap_start=start, cheap_end=end)


class TestImportWindowEnd(unittest.TestCase):

    def test_flux_in_winter_ends_at_the_charging_deadline(self):
        p = _mk()
        p._build_tariff_data = lambda: _tariff()
        now = datetime(2026, 11, 2, 3, 0, tzinfo=timezone.utc)       # 03:00 GMT
        self.assertEqual(p._import_window_end_utc(now),
                         datetime(2026, 11, 2, 4, 58, tzinfo=timezone.utc))

    def test_go_in_summer_is_converted_from_bst(self):
        p = _mk()
        p._build_tariff_data = lambda: _tariff(TARIFF_GO, "00:30", "05:30")
        now = datetime(2026, 7, 1, 1, 0, tzinfo=timezone.utc)        # 02:00 BST
        self.assertEqual(p._import_window_end_utc(now),
                         datetime(2026, 7, 1, 4, 30, tzinfo=timezone.utc))

    def test_a_window_spanning_midnight_ends_tomorrow(self):
        p = _mk()
        p._build_tariff_data = lambda: _tariff(TARIFF_GO, "23:30", "05:30")
        now = datetime(2026, 11, 2, 23, 45, tzinfo=timezone.utc)
        self.assertEqual(p._import_window_end_utc(now),
                         datetime(2026, 11, 3, 5, 30, tzinfo=timezone.utc))

    def test_outside_the_window_or_on_agile_there_is_no_end(self):
        p = _mk()
        p._build_tariff_data = lambda: _tariff()
        self.assertIsNone(p._import_window_end_utc(
            datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)))
        p._build_tariff_data = lambda: _tariff(TARIFF_AGILE, None, None)
        self.assertIsNone(p._import_window_end_utc(
            datetime(2026, 11, 2, 3, 0, tzinfo=timezone.utc)))


class TestImportStopsAtItsWindowEnd(unittest.TestCase):

    def _plugin(self, until_delta, soc=40.0):
        p = _mk()
        p.modbus = _modbus()
        p._restore_import_cutoff = MagicMock()
        p.latest_inverter_data = {"batterySoc": soc}
        p.store.update({"import_active": True, "import_target_soc": 80.0,
                        "export_active": False, "happy_hour_import_active": False,
                        "cheap_window_import_active": False,
                        "import_until": datetime.now(timezone.utc) + until_delta})
        return p

    def test_a_slow_import_stops_when_the_window_closes(self):
        """Old code held the import until SOC reached target, at the day rate."""
        p = self._plugin(timedelta(minutes=-1))
        with patch.object(plugin, "log"):
            self.assertTrue(p._end_import_past_its_window())
        self.assertFalse(p.store["import_active"])
        p.modbus.set_self_consumption.assert_called_once()
        p._restore_import_cutoff.assert_called_once()

    def test_an_import_inside_its_window_carries_on(self):
        p = self._plugin(timedelta(minutes=30))
        self.assertFalse(p._end_import_past_its_window())
        self.assertTrue(p.store["import_active"])

    def test_the_free_hour_and_cheap_window_keep_their_own_ends(self):
        for flag in ("happy_hour_import_active", "cheap_window_import_active"):
            p = self._plugin(timedelta(minutes=-1))
            p.store[flag] = True
            self.assertFalse(p._end_import_past_its_window())
            self.assertTrue(p.store["import_active"])

    def test_an_unconfirmed_hand_back_is_retried(self):
        p = self._plugin(timedelta(minutes=-1))
        p.modbus.set_self_consumption.return_value = False
        with patch.object(plugin, "log"):
            p._end_import_past_its_window()
        self.assertTrue(p.store["vpp_handback_pending"])

    def test_the_manager_decision_runs_the_check_first(self):
        p = self._plugin(timedelta(minutes=-1))
        p._end_import_past_its_window = MagicMock(return_value=False)
        p.modbus = None                    # returns straight after the guard
        p._act_on_decision(Decision(action=ACTION_SELF_CONSUMPTION))
        p._end_import_past_its_window.assert_not_called()   # no modbus: nothing at all
        p.modbus = _modbus()
        with patch.object(plugin, "log"):
            p._act_on_decision(Decision(action=ACTION_SELF_CONSUMPTION))
        p._end_import_past_its_window.assert_called_once()

    def test_force_charge_clears_an_old_end_time(self):
        p = _mk()
        p.modbus = MagicMock()
        p.modbus.force_charge.return_value = True
        p.store["import_until"] = datetime(2020, 1, 1, tzinfo=timezone.utc)
        p._force_charge(5000, 80.0)
        self.assertIsNone(p.store["import_until"])


# ================================================================
# Finding 7 — pause and sleep clear the feature flags
# ================================================================

class TestDisengageClearsFeatureFlags(unittest.TestCase):

    def test_the_three_flags_are_cleared(self):
        p = _mk()
        p.modbus = _modbus()
        p._write_policy_floors = MagicMock(return_value=5.0)
        p._set_flood_prev_target = MagicMock()
        p._set_import_cutoff = MagicMock()
        p._end_happy_hour_import = MagicMock(
            side_effect=lambda why: p.store.update(happy_hour_import_active=False))
        p.store.update({"vpp_state": plugin.VPP_IDLE, "import_scheduled_time": None,
                        "happy_hour_import_active": True,
                        "cheap_window_import_active": True,
                        "saving_session_export_active": True,
                        "vpp_export_submode": "discharge"})
        with patch.object(plugin, "log"):
            p._disengage_to_safe_baseline("Pause")
        p._end_happy_hour_import.assert_called_once()
        self.assertFalse(p.store["happy_hour_import_active"])
        self.assertFalse(p.store["cheap_window_import_active"])
        self.assertFalse(p.store["saving_session_export_active"])
        self.assertIsNone(p.store["vpp_export_submode"])


# ================================================================
# Finding 10 — a forecast built from cached arrays carries their age
# ================================================================

class _ConnErr(Exception):
    pass


_FAKE_REQUESTS = types.SimpleNamespace(
    get=MagicMock(side_effect=_ConnErr("network down")),
    exceptions=types.SimpleNamespace(Timeout=_ConnErr, ConnectionError=_ConnErr,
                                     ChunkedEncodingError=_ConnErr))


class TestCachedForecastIsDatedByItsAge(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.f = omf.OpenMeteoForecast(data_dir=self.tmp, latitude=51.5, longitude=-0.12,
                                       optimiser_file=os.path.join(self.tmp, "o.json"))
        self.f.logger           = MagicMock()
        self.f._cached_forecast = None
        self.f._cached_time     = 0.0

    def _seed_array_caches(self, age_s):
        today = self.f._now_local().strftime("%Y-%m-%d")
        data  = [[f"{today}T{h:02d}:00", 300.0 if 8 <= h <= 16 else 0.0] for h in range(24)]
        for a in self.f.arrays:
            with open(self.f._array_cache_path(a["name"]), "w") as fh:
                json.dump({"cached_time": time.time() - age_s, "data": data}, fh)

    def _fetch(self):
        with patch.object(omf, "REQUESTS_AVAILABLE", True), \
                patch.object(omf, "requests", _FAKE_REQUESTS, create=True), \
                patch.object(omf.time, "sleep"):
            return self.f.fetch_forecast(force=True)

    def test_every_download_failing_is_not_dated_now(self):
        """01-11-2026 review: 14-hour-old caches came back 'OK' aged 0 s, so the
        planner's 3-hour freshness limit never tripped during an outage."""
        self._seed_array_caches(14 * 3600)
        result = self._fetch()
        age = time.time() - self.f._cached_time
        self.assertGreater(age, 13 * 3600)
        self.assertIn("cached", result["forecastStatus"])
        self.assertTrue(result["forecastStatus"].startswith("OK"))

    def test_a_fresh_disk_cache_is_still_plain_ok(self):
        self._seed_array_caches(60)
        result = self._fetch()
        self.assertEqual(result["forecastStatus"], "OK")
        self.assertLess(time.time() - self.f._cached_time, 600)


if __name__ == "__main__":
    unittest.main()
