#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    test_review_2026_11_01_b.py
# Description: Pins the second batch of fixes from the 01-11-2026 code review
#              (v5.142.0): checked writes, the Modbus exception reply, battery SOC
#              plausibility, the sleep latch, the dashboard's loopback trust, and
#              the smaller tariff, forecast, storm and ledger items.
# Author:      CliveS & Claude Opus 5.5
# Date:        01-11-2026
# Version:     1.0

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
import test_web_dashboard as WD            # noqa: E402  (its handler factory)
import plugin                              # noqa: E402
import sigenergy_modbus                    # noqa: E402
import octopus_api                         # noqa: E402
import storm_watch                         # noqa: E402
import vpp_ledger                          # noqa: E402
import axle_account                        # noqa: E402
import openmeteo_forecast as omf           # noqa: E402
from battery_manager import ACTION_START_EXPORT, Decision  # noqa: E402


def _mk():
    p = DE._mk(tempfile.mkdtemp())
    p.debug = False
    return p


def _modbus(**returns):
    m = MagicMock()
    m.connected = True
    for name in ("set_self_consumption", "set_discharge_cutoff", "set_charge_limit",
                 "daytime_export", "night_export", "set_charge_cutoff"):
        getattr(m, name).return_value = returns.get(name, True)
    return m


def _warnings(logged):
    return [c.args[0] for c in logged.call_args_list if c.kwargs.get("level") == "WARNING"]


# ================================================================
# Modbus driver (finding 9, SOC plausibility)
# ================================================================

class _FakeExceptionResponse:
    def isError(self):
        return True


class TestExceptionReplyKeepsTheLink(unittest.TestCase):

    def _driver(self, result):
        m = sigenergy_modbus.SigenergyModbus(ip="192.0.2.1", port=502, plant_address=247,
                                             inverter_address=1, logger=MagicMock())
        m.client = MagicMock()
        m.client.write_register.return_value  = result
        m.client.write_registers.return_value = result
        m._connected = True
        m._last_request_time = 0.0
        return m

    def test_an_exception_reply_returns_false_but_keeps_the_connection(self):
        """The inverter ANSWERED, so the socket works. Dropping it made every later
        read in the same verify pass return None."""
        reply = _FakeExceptionResponse()
        with patch.object(sigenergy_modbus, "_ExceptionResponse", _FakeExceptionResponse):
            m = self._driver(reply)
            self.assertFalse(m._write_single_register(40047, 1000, verify=False))
            self.assertTrue(m._connected)
            self.assertFalse(m._write_uint32_registers(40032, 5000, verify=False))
            self.assertTrue(m._connected)

    def test_any_other_error_result_still_drops_it(self):
        other = MagicMock()
        other.isError.return_value = True
        with patch.object(sigenergy_modbus, "_ExceptionResponse", _FakeExceptionResponse):
            m = self._driver(other)
            self.assertFalse(m._write_single_register(40047, 1000, verify=False))
            self.assertFalse(m._connected)


class TestSocPlausibility(unittest.TestCase):

    def test_impossible_readings_are_absent(self):
        self.assertIsNone(sigenergy_modbus._soc_pct(0xFFFF))    # was 6553.5%
        self.assertIsNone(sigenergy_modbus._soc_pct(1001))
        self.assertIsNone(sigenergy_modbus._soc_pct(None))

    def test_real_readings_decode(self):
        self.assertEqual(sigenergy_modbus._soc_pct(1000), 100.0)
        self.assertEqual(sigenergy_modbus._soc_pct(537), 53.7)
        self.assertEqual(sigenergy_modbus._soc_pct(0), 0.0)


# ================================================================
# Finding 8 — checked writes
# ================================================================

class TestVppExportDriverChecksItsWrites(unittest.TestCase):

    def _plugin(self, **returns):
        p = _mk()
        p._flux_preempt = MagicMock()
        p._trigger_event = MagicMock()
        p._export_is_daylight = lambda now=None: False
        p.modbus = _modbus(**returns)
        p.latest_inverter_data = {"pvPowerWatts": 0, "homePowerWatts": 500}
        p.store.update({"export_active": False, "vpp_export_submode": None})
        return p

    def test_a_failed_export_write_is_not_recorded_and_is_retried(self):
        p = self._plugin(night_export=False)
        with patch.object(plugin, "vpp_log") as logged:
            p._drive_vpp_export()
        self.assertIsNone(p.store["vpp_export_submode"])
        self.assertFalse(p.store["export_active"])
        p._trigger_event.assert_not_called()
        self.assertTrue(_warnings(logged))
        p.modbus.night_export.return_value = True       # next tick: it lands
        with patch.object(plugin, "vpp_log"):
            p._drive_vpp_export()
        self.assertEqual(p.store["vpp_export_submode"], "discharge")
        self.assertTrue(p.store["export_active"])
        p._trigger_event.assert_called_once_with("exportStarted")

    def test_a_failed_bank_cap_is_not_recorded(self):
        p = self._plugin(set_charge_limit=False)
        p._export_is_daylight = lambda now=None: True
        p.latest_inverter_data = {"pvPowerWatts": 9000, "homePowerWatts": 500}
        with patch.object(plugin, "vpp_log"):
            p._drive_vpp_export()
        self.assertEqual(p.store.get("vpp_bank_charge_cap_w"), -1)
        self.assertIsNone(p.store["vpp_export_submode"])

    def test_nothing_is_written_while_the_link_is_down(self):
        p = self._plugin()
        p.modbus.connected = False
        with patch.object(plugin, "vpp_log"):
            p._drive_vpp_export()
        p.modbus.night_export.assert_not_called()
        self.assertFalse(p.store["export_active"])


class TestFloodFloorIsChecked(unittest.TestCase):

    def _plugin(self, floor_ok):
        p = _mk()
        p.modbus = _modbus(set_discharge_cutoff=floor_ok)
        p._policy_discharge_floor_pct = lambda dispatch_event=None: 20.0
        p._set_flood_prev_target = MagicMock()
        p._trigger_event = MagicMock()
        p.pluginPrefs["inverterMaxKw"] = "10"
        p.store.update({"import_active": False, "export_active": False,
                        "import_scheduled_time": None, "export_count_today": 0,
                        "cheap_window_import_active": False})
        return p

    def _decide(self, p):
        d = Decision(action=ACTION_START_EXPORT, power_watts=4000, target_soc_pct=40.0)
        with patch.object(plugin, "log") as logged:
            p._act_on_decision(d)
        return logged

    def test_the_discharge_limit_is_the_inverter_maximum(self):
        """The DNO cap here was 'corrected' by the verify pass a minute later."""
        p = self._plugin(floor_ok=True)
        self._decide(p)
        p.modbus.night_export.assert_called_once_with(10000)
        p._set_flood_prev_target.assert_called_once_with(40.0)
        self.assertTrue(p.store["export_active"])

    def test_no_floor_means_no_drain(self):
        p = self._plugin(floor_ok=False)
        logged = self._decide(p)
        p._set_flood_prev_target.assert_not_called()
        self.assertFalse(p.store["export_active"])
        p.modbus.set_self_consumption.assert_called_once()
        self.assertTrue(_warnings(logged))


class TestHandBacksAreConfirmed(unittest.TestCase):

    def test_a_failed_hand_back_sets_the_retry(self):
        p = _mk()
        p.modbus = _modbus(set_self_consumption=False)
        p.store["vpp_handback_pending"] = False
        with patch.object(plugin, "log") as logged:
            self.assertFalse(p._hand_back_confirmed("the import end"))
        self.assertTrue(p.store["vpp_handback_pending"])
        self.assertTrue(_warnings(logged))

    def test_a_good_one_does_not(self):
        p = _mk()
        p.modbus = _modbus()
        p.store["vpp_handback_pending"] = False
        self.assertTrue(p._hand_back_confirmed("the import end"))
        self.assertFalse(p.store["vpp_handback_pending"])


class TestVppHandBackRetryReconnects(unittest.TestCase):

    def test_the_retry_reconnects_before_writing(self):
        p = _mk()
        m = _modbus()
        calls = []

        def _sc():
            calls.append("sc")
            if len(calls) == 1:
                m.connected = False         # the first write dropped the link
                return False
            return True

        def _connect(**kw):
            calls.append("connect")
            m.connected = True
            return True
        m.set_self_consumption.side_effect = _sc
        m.connect.side_effect = _connect
        p.modbus = m
        for n in ("_restore_discharge_cutoff", "_vpp_transition", "_summarise_vpp_event",
                  "_update_vpp_device", "_trigger_event", "_record_vpp_result",
                  "_write_vpp_event_footer"):
            setattr(p, n, MagicMock())
        p.store.update({"vpp_state": plugin.VPP_ACTIVE, "vpp_event": None,
                        "export_active": True, "grid_export_daily_kwh": 0.0,
                        "vpp_export_start_kwh": 0.0})
        with patch.object(plugin, "vpp_log"), patch.object(plugin, "log"):
            try:
                p._end_vpp_export(datetime.now(timezone.utc), None)
            except Exception:                           # noqa: BLE001
                pass    # the wrap-up after the hand-back is not under test
        self.assertEqual(calls[:3], ["sc", "connect", "sc"])
        self.assertFalse(p.store["vpp_handback_pending"])


class TestCancelledEventLeavesTheManagersExport(unittest.TestCase):

    def test_a_flood_export_survives_a_cancelled_pre_charge(self):
        p = _mk()
        for n in ("_restore_discharge_cutoff", "_update_vpp_device", "_record_vpp_api_status"):
            setattr(p, n, MagicMock())
        p.modbus = _modbus()
        now = datetime.now(timezone.utc)
        p.store.update({"vpp_state": plugin.VPP_PRE_CHARGING, "export_active": True,
                        "flood_prev_target_soc": 40.0,
                        "vpp_event": {"start_time": now + timedelta(minutes=20),
                                      "end_time": now + timedelta(minutes=80)}})
        p.pluginPrefs["axleEnabled"] = True
        p.axle = MagicMock()
        p.axle.get_next_event.return_value = None
        p.axle.last_error = None
        with patch.object(plugin, "vpp_log"):
            p._poll_vpp()
        self.assertEqual(p.store["vpp_state"], plugin.VPP_IDLE)
        self.assertTrue(p.store["export_active"])
        p.modbus.set_self_consumption.assert_not_called()


# ================================================================
# Finding 6 — the sleep latch
# ================================================================

class TestSleepLatch(unittest.TestCase):

    def test_a_sleeping_tick_does_nothing(self):
        p = _mk()
        p.store["mac_asleep_at"]  = 100000.0
        p.store["last_log_check"] = 0.0
        with patch.object(plugin, "_ensure_plugin_log") as ensure:
            p._tick(100010.0)
        ensure.assert_not_called()

    def test_the_latch_expires_if_the_mac_never_slept(self):
        p = _mk()
        p.store["mac_asleep_at"]  = 100000.0
        p.store["last_log_check"] = 0.0
        with patch.object(plugin, "log"), \
                patch.object(plugin, "_ensure_plugin_log", side_effect=RuntimeError("ran")):
            with self.assertRaises(RuntimeError):      # the tick went on to real work
                p._tick(100000.0 + p.SLEEP_LATCH_MAX_S + 1)
        self.assertIsNone(p.store["mac_asleep_at"])

    def test_prepare_to_sleep_sets_it_and_wake_clears_it(self):
        p = _mk()
        p.web_dashboard = None
        p._flux_release = MagicMock()
        p._disengage_to_safe_baseline = MagicMock()
        p.modbus = _modbus()
        with patch.object(plugin, "log"), \
                patch.object(plugin.indigo.PluginBase, "prepare_to_sleep", create=True), \
                patch.object(plugin.indigo.PluginBase, "wake_up", create=True), \
                patch.object(plugin, "WebDashboard"):
            p.prepare_to_sleep()
            self.assertTrue(p.store["mac_asleep_at"])
            p._resolve_dashboard_bind = lambda: plugin.DASHBOARD_BIND_LOOPBACK
            p._resolve_dashboard_token = lambda: "t"
            p.wake_up()
        self.assertIsNone(p.store["mac_asleep_at"])


# ================================================================
# Finding 12 — the dashboard
# ================================================================

class TestDashboardLoopbackTrust(unittest.TestCase):

    def test_a_local_request_still_needs_no_token(self):
        h = WD.make_handler("127.0.0.1", headers={"Host": "127.0.0.1:8179"})
        self.assertTrue(h._authorised())
        h = WD.make_handler("127.0.0.1", headers={"Host": "localhost:8179"})
        self.assertTrue(h._authorised())
        self.assertTrue(WD.make_handler("::1", headers={"Host": "[::1]:8179"})._authorised())

    def test_a_tunnelled_request_needs_the_token(self):
        """cloudflared connects from 127.0.0.1 and forwards the public name."""
        h = WD.make_handler("127.0.0.1", headers={"Host": "energy.example.com"})
        self.assertFalse(h._authorised())
        h = WD.make_handler("127.0.0.1", headers={"Host": "127.0.0.1:8179",
                                                  "Cf-Connecting-Ip": "203.0.113.9"})
        self.assertFalse(h._authorised())
        h = WD.make_handler("127.0.0.1", headers={"Host": "energy.example.com",
                                                  "Authorization": f"Bearer {WD.TOKEN}"})
        self.assertTrue(h._authorised())

    def test_a_rebinding_page_needs_the_token(self):
        h = WD.make_handler("127.0.0.1", headers={"Host": "attacker.example:8179"})
        self.assertFalse(h._authorised())


class TestDashboardSettings(unittest.TestCase):

    def _plugin(self, bind):
        p = _mk()
        p._resolve_dashboard_bind  = lambda: bind
        p._resolve_dashboard_token = lambda: "secret-token"
        p._resolve_dashboard_host  = lambda: "192.0.2.10"
        return p

    def test_the_automatic_url_carries_no_token(self):
        p = self._plugin("0.0.0.0")
        self.assertNotIn("secret-token", p._dashboard_url(with_token=False))
        self.assertIn("secret-token", p._dashboard_url())

    def test_a_changed_bind_restarts_the_server(self):
        p = self._plugin("127.0.0.1")
        old = MagicMock(_bind_host="0.0.0.0", _token="secret-token")
        p.web_dashboard = old
        with patch.object(plugin, "WebDashboard") as cls, patch.object(plugin, "log"):
            p._restart_dashboard_if_changed()
        old.stop.assert_called_once()
        cls.assert_called_once()
        self.assertEqual(cls.call_args.kwargs["bind_host"], "127.0.0.1")

    def test_unchanged_settings_leave_it_running(self):
        p = self._plugin("127.0.0.1")
        live = MagicMock(_bind_host="127.0.0.1", _token="secret-token")
        p.web_dashboard = live
        with patch.object(plugin, "WebDashboard") as cls:
            p._restart_dashboard_if_changed()
        live.stop.assert_not_called()
        cls.assert_not_called()


# ================================================================
# Smaller items
# ================================================================

class TestInverterInUse(unittest.TestCase):

    def test_flags(self):
        p = _mk()
        p.store.update({"vpp_state": plugin.VPP_IDLE})
        self.assertFalse(p._inverter_in_use())
        for key in ("import_active", "export_active", "happy_hour_import_active",
                    "cheap_window_import_active", "saving_session_export_active"):
            p.store[key] = True
            self.assertTrue(p._inverter_in_use(), key)
            p.store[key] = False
        p.store["vpp_state"] = plugin.VPP_ANNOUNCED
        self.assertFalse(p._inverter_in_use())
        p.store["vpp_state"] = plugin.VPP_ACTIVE
        self.assertTrue(p._inverter_in_use())

    def test_a_settings_save_asks_for_the_cautious_reset(self):
        p = _mk()
        p._init_modules = MagicMock()
        p._write_site_config = MagicMock()
        for n in ("_log_bank_first_setting", "_log_tariff_override_setting",
                  "_log_saving_session_auto_join_setting", "_log_axle_account_setting",
                  "_restart_dashboard_if_changed"):
            setattr(p, n, MagicMock())
        with patch.object(plugin, "log"):
            p.closedPrefsConfigUi({}, False)
        p._init_modules.assert_called_once_with(from_prefs_save=True)


class TestRemoteEmsIsCheckedWhileDriving(unittest.TestCase):

    def _verify(self, in_use, enabled):
        p = _mk()
        m = MagicMock()
        m.connected = True
        reads = ("read_ems_mode", "read_discharge_limit", "read_charge_limit",
                 "read_discharge_cutoff", "read_backup_soc", "read_grid_import_limit",
                 "read_charge_cutoff", "read_export_limit")
        for name in reads:
            getattr(m, name).return_value = None
        m.read_remote_ems_enabled.return_value = enabled
        p.modbus = m
        p._inverter_in_use = lambda: in_use
        p._owns_backup_reserve = lambda: False
        p._flux_armed = lambda: False
        p._driven_export_owns_registers = lambda: False
        p._import_ems_mode = lambda: plugin.EMS_CHARGE_PV_FIRST
        p._recent_flux_cheap_charge = lambda: False
        p._grid_outage_active = lambda: False
        p.store.update({"import_active": in_use, "export_active": False,
                        "vpp_state": plugin.VPP_IDLE})
        with patch.object(plugin, "log") as logged:
            p._verify_ems_registers()
        return m, logged

    def test_switched_off_mid_import_is_switched_back_on(self):
        m, logged = self._verify(in_use=True, enabled=False)
        m.enable_remote_ems.assert_called_once()
        self.assertTrue(_warnings(logged))

    def test_left_alone_when_the_plugin_is_not_driving(self):
        """The Return to Local EMS action must not be undone a minute later."""
        m, _ = self._verify(in_use=False, enabled=False)
        m.read_remote_ems_enabled.assert_not_called()
        m.enable_remote_ems.assert_not_called()


class TestTrackerHasNoRateInForce(unittest.TestCase):

    def test_none_in_force_is_nothing_not_the_oldest_row(self):
        old = {"valid_from": "2026-10-20T00:00:00Z", "valid_to": "2026-10-21T00:00:00Z",
               "value_inc_vat": 21.0}
        self.assertEqual(octopus_api.OctopusAPI._active_rate([old]), {})


class TestAgileFailureIsNotCached(unittest.TestCase):

    def _api(self):
        api = octopus_api.OctopusAPI.__new__(octopus_api.OctopusAPI)
        api.logger = MagicMock()
        api._rates_cache = {}
        api._find_product_code = lambda key: "AGILE-24-10-01"
        api._build_tariff_code = lambda code: "E-1R-AGILE-24-10-01-F"
        return api

    def test_a_failed_day_serves_its_last_slots_and_is_retried(self):
        api = self._api()
        day = datetime(2026, 11, 1).date()
        api._fetch_rate_schedule = MagicMock(return_value=[
            {"valid_from": "2026-11-01T20:00:00Z", "value_inc_vat": 15.0}])
        good = api.get_agile_rates(day)
        self.assertEqual(len(good), 1)
        api._fetch_rate_schedule = MagicMock(return_value=None)          # fails
        self.assertEqual(api.get_agile_rates(day, force=True), good)
        api._fetch_rate_schedule.assert_called_once()
        self.assertTrue(api._fetch_rate_schedule.call_args.kwargs.get("failed_as_none"))

    def test_a_failure_with_nothing_before_is_empty_and_not_cached(self):
        api = self._api()
        day = datetime(2026, 11, 1).date()
        api._fetch_rate_schedule = MagicMock(return_value=None)
        self.assertEqual(api.get_agile_rates(day), [])
        self.assertNotIn(f"agile_{day}", api._rates_cache)


class TestBandedExportRateIsNotFrozen(unittest.TestCase):

    def test_a_missing_band_uses_the_carried_day_not_the_last_published_figure(self):
        p = _mk()
        p._export_tariff_is_banded = lambda: True
        p._flux_export_band_p = lambda when_utc=None: None
        now = datetime.now(timezone.utc)
        span = types.SimpleNamespace(start=now - timedelta(hours=1),
                                     end=now + timedelta(hours=1), p=10.2)
        p._flux_planning_spans = lambda key: [span]
        p.latest_rates_data = {"export_rate_p": 29.6}     # a 16:00 peak, carried
        self.assertEqual(p._export_rate_now_p(), 10.2)

    def test_with_nothing_carried_it_is_the_default_not_the_stale_peak(self):
        p = _mk()
        p._export_tariff_is_banded = lambda: True
        p._flux_export_band_p = lambda when_utc=None: None
        p._flux_planning_spans = lambda key: []
        p.latest_rates_data = {"export_rate_p": 29.6}
        self.assertEqual(p._export_rate_now_p(), plugin.DEFAULT_EXPORT_RATE_P)


class TestDayRateImportSeenInEitherChargeMode(unittest.TestCase):

    def test_charge_pv_first_after_a_restart_is_recorded(self):
        p = _mk()
        p._note_day_rate_import = MagicMock()
        p.store.update({"import_active": False, "export_active": False,
                        "vpp_state": plugin.VPP_IDLE, "happy_hour_import_active": False,
                        "remote_ems_mode_seen": (plugin.EMS_CHARGE_PV_FIRST, time.time())})
        p.modbus = _modbus()
        p._end_import_past_its_window = MagicMock(return_value=False)
        with patch.object(plugin, "log"):
            try:
                p._act_on_decision(Decision(action="none"))
            except Exception:                           # noqa: BLE001
                pass
        p._note_day_rate_import.assert_called()


class TestNullIrradianceIsUnknown(unittest.TestCase):

    def test_an_hour_one_array_left_out_is_dropped_everywhere(self):
        tmp = tempfile.mkdtemp()
        f = omf.OpenMeteoForecast(data_dir=tmp, latitude=51.5, longitude=-0.12,
                                  optimiser_file=os.path.join(tmp, "o.json"))
        f.logger = MagicMock()
        today = f._now_local().strftime("%Y-%m-%d")
        full  = [(f"{today}T{h:02d}:00", 300.0) for h in range(8, 12)]
        short = [x for x in full if not x[0].endswith("T11:00")]
        calls = {"n": 0}

        def _fake(_cfg):
            calls["n"] += 1
            return short if calls["n"] == 1 else full
        f._fetch_array = _fake
        combined = f._fetch_all_arrays()
        keys = set(combined.get("_hourly_p50_today", {}))
        self.assertNotIn(f"{today} 11:00:00", keys)
        self.assertIn(f"{today} 10:00:00", keys)


class TestMetOfficeOverNewYear(unittest.TestCase):

    TEXT = "Yellow warning of wind valid from 1800 Thu 31 Dec to 1200 Fri 01 Jan"

    def test_on_new_years_eve_it_ends_next_year(self):
        now = datetime(2026, 12, 31, 20, 0, tzinfo=timezone.utc)
        start, end = storm_watch._mo_window(self.TEXT, now)
        self.assertEqual(start, datetime(2026, 12, 31, 18, 0, tzinfo=timezone.utc))
        self.assertEqual(end, datetime(2027, 1, 1, 12, 0, tzinfo=timezone.utc))

    def test_on_new_years_day_it_started_last_year(self):
        now = datetime(2027, 1, 1, 2, 0, tzinfo=timezone.utc)
        start, end = storm_watch._mo_window(self.TEXT, now)
        self.assertEqual(start.year, 2026)
        self.assertEqual(end, datetime(2027, 1, 1, 12, 0, tzinfo=timezone.utc))

    def test_an_ordinary_window_is_unchanged(self):
        now = datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc)
        start, end = storm_watch._mo_window(
            "valid from 0600 Mon 02 Nov to 2100 Mon 02 Nov", now)
        self.assertEqual(start, datetime(2026, 11, 2, 6, 0, tzinfo=timezone.utc))
        self.assertEqual(end, datetime(2026, 11, 2, 21, 0, tzinfo=timezone.utc))


class TestLedgerKeepsLastNewRow(unittest.TestCase):

    def test_round_trip(self):
        path = os.path.join(tempfile.mkdtemp(), "ledger.json")
        led = vpp_ledger.empty_ledger()
        led["axle"]["last_new_row_at"] = "2026-10-27T19:05:00+00:00"
        vpp_ledger.save_ledger(path, led)
        self.assertEqual(vpp_ledger.load_ledger(path)["axle"]["last_new_row_at"],
                         "2026-10-27T19:05:00+00:00")


class TestZoneName(unittest.TestCase):

    def test_gmt_and_bst(self):
        self.assertEqual(plugin._zone_name(datetime(2026, 11, 1, 12, tzinfo=timezone.utc)), "GMT")
        self.assertEqual(plugin._zone_name(datetime(2026, 7, 1, 12, tzinfo=timezone.utc)), "BST")


class TestDailyForecastIsForItsOwnDay(unittest.TestCase):

    def test_a_fetch_after_midnight_does_not_move_into_yesterday(self):
        p = _mk()
        p.store["forecast_kwh_by_date"] = {"2026-10-31": 12.5}
        p.latest_forecast_data = {"forecastDate": "2026-11-01", "todayKwh": 30.0}
        self.assertEqual(p._forecast_kwh_for("2026-10-31"), 12.5)
        self.assertEqual(p._forecast_kwh_for("2026-11-01"), 30.0)


class TestAxleErrorTextCarriesNoToken(unittest.TestCase):

    def test_the_query_string_is_removed(self):
        class _Boom:
            @staticmethod
            def get(url, timeout, headers):
                raise ConnectionError(f"Max retries exceeded with url: {url} (Caused by X)")
        with patch.dict(sys.modules, {"requests": _Boom}):
            status, text = axle_account._http_get(
                "https://vpp.axle.energy/app/account/balance.data?token=eyJsecret&siteId=1",
                "eyJsecret", 5)
        self.assertEqual(status, 0)
        self.assertNotIn("eyJsecret", text)


if __name__ == "__main__":
    unittest.main()
