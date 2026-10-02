#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    test_review_2026_10_02.py
# Description: Faults from the independent review of 5.131.1, each one a
#              transition or a failure path the earlier tests stubbed past.
# Author:      CliveS & Claude Opus 5.5
# Date:        02-10-2026
# Version:     1.0

import pathlib
import sys
import tempfile
import threading
import time
import types
import unittest
from datetime import date, timedelta
from unittest.mock import MagicMock, patch

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

import plugin                        # noqa: E402
import flux_strategy as fs           # noqa: E402
import flux_execution as fx          # noqa: E402
import test_flux_strategy as T       # noqa: E402
import test_flux_execution as TX     # noqa: E402
from sigenergy_modbus import SigenergyModbus   # noqa: E402


def _bare(**store):
    p = plugin.Plugin.__new__(plugin.Plugin)
    p._state_lock = threading.RLock()
    p.logger = MagicMock()
    p.pluginPrefs = {}
    p.store = dict(store)
    return p


# -- P1: the cheap-window charge band ------------------------------------------

class TestChargeBandIsNeverInverted(unittest.TestCase):
    """A charge target below the household floor (the roof covers an Axle event
    the floor counts in full) was refused by the executor every tick, and
    nothing charged all night."""

    def _scenarios(self):
        d = (2026, 6, 21)                 # a free-hour Sunday: the 50% minimum is waived
        ev = T._axle(d, 17, 18, kw=4.0)
        hh = fs.EventCommitment(source="octopus", kind="import",
                                start=fs._wall(T.LONDON, date(*d), fs.time(13, 0)),
                                end=fs._wall(T.LONDON, date(*d), fs.time(14, 0)),
                                energy_kwh=10.0)
        d2 = (2026, 6, 24)
        yield d, 45.0, (ev, hh), 30.0
        yield d, 45.0, (ev, hh), 12.0
        yield d2, 70.0, (T._axle(d2, 12, 15, kw=4.0),), 30.0

    def test_every_charge_plan_is_a_band_the_executor_accepts(self):
        charges = 0
        for d, pv_kwh, commitments, soc in self._scenarios():
            pv = T._pv(date(*d), pv_kwh, first_hour=6, last_hour=20)
            p = fs.plan(T._inputs((2, 30), soc_pct=soc, day=d, pv=pv,
                                  commitments=commitments))
            self.assertLessEqual(p.discharge_cutoff_pct, p.charge_cutoff_pct, (d, soc))
            if p.mode != fs.MODE_CHARGE:
                continue
            charges += 1
            now = p.decision_at
            regs = TX.Registers()
            with tempfile.TemporaryDirectory() as tmp:
                e = fx.FluxExecutor(regs, pathlib.Path(tmp) / "j.json",
                                    baseline_charge_w=10000, baseline_discharge_w=10000,
                                    baseline_discharge_cutoff_pct=20., clock=lambda: now)
                results = []
                for _ in range(3):
                    t = fx.FluxTarget(now, p.decision_until, now, now + timedelta(seconds=30),
                                      p.ems_mode, p.charge_limit_w, p.discharge_limit_w,
                                      round(p.charge_cutoff_pct, 1),
                                      round(p.discharge_cutoff_pct, 1))
                    results.append(e.step(t, now))
                self.assertIn("applied", results, (d, soc, e.last_error))
                self.assertNotIn("band", e.last_error)
        self.assertGreaterEqual(charges, 2)          # the scenarios do reach a charge


class TestARefusedPlanHandsTheWindowBack(unittest.TestCase):

    def test_a_refusal_is_logged_once_and_releases_the_window(self):
        p = _bare()
        ex = types.SimpleNamespace(last_error="Invalid target energy band")
        dec = types.SimpleNamespace(owns=True)
        with patch.object(plugin, "log") as lg:
            p._flux_note_refused(ex, dec, "released")
            p._flux_note_refused(ex, dec, "released")
        self.assertTrue(p.store["flux_refused"])
        self.assertEqual(lg.call_count, 1)

    def test_a_plain_release_is_not_a_refusal(self):
        p = _bare()
        p._flux_note_refused(types.SimpleNamespace(last_error=""),
                             types.SimpleNamespace(owns=True), "released")
        self.assertFalse(p.store.get("flux_refused"))

    def test_the_manager_keeps_the_window_while_refused(self):
        p = _bare(flux_refused=True,
                  flux_decision=types.SimpleNamespace(mode=fs.MODE_CHARGE))
        p.latest_rates_data = {"tariff_info": {"tariff_key": plugin.TARIFF_FLUX}}
        with patch.object(plugin, "FLUX_AVAILABLE", True), \
                patch.object(fs, "in_window", return_value=True):
            p._flux_armed = lambda: True
            p._flux_other_owner = lambda: False
            p._flux_owns_control = lambda: True
            p._flux_may_claim = lambda: True
            self.assertFalse(p._flux_owns_cheap_window())
            p.store["flux_refused"] = False
            self.assertTrue(p._flux_owns_cheap_window())


# -- P1: shutdown after Indigo has stopped the thread ---------------------------

class TestShutdownStillWritesSelfConsumption(unittest.TestCase):
    """Indigo stops the concurrent thread before shutdown(), so self.sleep()
    raises; every Modbus write after the first died on its pause."""

    def test_the_mode_register_is_written_after_the_stop(self):
        class Host(plugin.Plugin):
            stopThread = True

            def sleep(self, seconds):
                if self.stopThread:
                    raise self.StopThread
        p = Host.__new__(Host)
        p._state_lock = threading.RLock()
        p.logger = MagicMock()
        p.pluginPrefs = {}
        p.store = {}
        p.web_dashboard = None
        p.flux_executor = None
        p._flux_release = lambda why: None
        p._save_accumulators = MagicMock()
        writes = []
        client = MagicMock()

        def _ok():
            r = MagicMock()
            r.isError.return_value = False
            return r

        def _wr(address, value, device_id):
            writes.append((address, value))
            return _ok()

        def _wrs(address, values, device_id):
            writes.append((address, values))
            return _ok()

        def _rd(address, count, device_id):
            r = _ok()
            last = [v for a, v in writes if a == address and isinstance(v, int)]
            r.registers = [last[-1] if last else 0] * count
            return r
        client.write_register.side_effect = _wr
        client.write_registers.side_effect = _wrs
        client.read_holding_registers.side_effect = _rd
        m = SigenergyModbus(ip="192.0.2.1", port=502, plant_address=247,
                            inverter_address=1, logger=MagicMock(), sleep_func=p.sleep)
        m.client = client
        m._connected = True
        m._last_request_time = 0.0
        m.disconnect = MagicMock()
        p.modbus = m
        with patch.object(plugin, "log"), patch.object(time, "sleep"):
            p.shutdown()
        self.assertIn((40031, 2), writes)


# -- batch 2: control and money ------------------------------------------------

import test_plugin_daily_energy as DE      # noqa: E402  (the repo's own plugin factory)
from datetime import datetime, timezone    # noqa: E402


def _mk():
    return DE._mk(tempfile.mkdtemp())


class TestAFailedAxlePollIsNotACancellation(unittest.TestCase):

    def _plugin(self, state=plugin.VPP_PRE_CHARGING):
        p = _mk()
        for n in ("_restore_discharge_cutoff", "_update_vpp_device", "_flux_preempt",
                  "_trigger_event", "_drive_vpp_export", "_set_vpp_discharge_cutoff",
                  "_write_vpp_event_header", "_log_vpp_snapshot", "_start_vpp_precharge",
                  "_record_vpp_api_status"):
            setattr(p, n, MagicMock())
        p.debug = False
        now = datetime.now(timezone.utc)
        ev = {"start_time": now + timedelta(minutes=3), "end_time": now + timedelta(minutes=63),
              "duration_hrs": 1.0, "import_export": "export"}
        p.store.update({"vpp_state": state, "vpp_event": ev, "export_active": False,
                        "vpp_active": False, "grid_export_daily_kwh": 5.0})
        p.pluginPrefs["axleEnabled"] = True
        p.axle = MagicMock()
        p.axle.get_next_event.return_value = None
        return p

    def test_a_timeout_holds_the_window_and_the_fast_poll(self):
        p = self._plugin()
        p.axle.last_error = "HTTP 503"
        p._poll_vpp()
        self.assertEqual(p.store["vpp_state"], plugin.VPP_PRE_CHARGING)
        self.assertIsNotNone(p.store["vpp_event"])
        p._restore_discharge_cutoff.assert_not_called()
        self.assertEqual(p._vpp_poll_interval(), 60)

    def test_a_clean_empty_answer_still_cancels_and_clears_the_event(self):
        p = self._plugin()
        p.axle.last_error = None
        p._poll_vpp()
        self.assertEqual(p.store["vpp_state"], plugin.VPP_IDLE)
        self.assertIsNone(p.store["vpp_event"])


class TestAHandBackWithTheSocketDownClearsTheCeiling(unittest.TestCase):

    def test_cheap_window(self):
        p = _mk()
        p.latest_inverter_data = {"batterySoc": 55.0}
        p.store.update({"cheap_window_import_active": True, "import_active": True,
                        "import_target_soc": 55.0, "vpp_handback_pending": False})
        p._set_import_cutoff(55.0)
        p.modbus = MagicMock(connected=False)
        p._end_cheap_window_import("the cheap window has closed")
        self.assertIsNone(p.store["import_charge_cutoff_pct"])
        self.assertTrue(p.store["vpp_handback_pending"])
        p.modbus.set_charge_cutoff.assert_not_called()

    def test_happy_hour(self):
        p = _mk()
        p.store.update({"happy_hour_import_active": True, "import_active": True,
                        "happy_hour_anchor_kwh": None})
        p._set_import_cutoff(95.0)
        p.modbus = MagicMock(connected=False)
        p._happy_hour_result_note = MagicMock()
        p._save_accumulators = MagicMock()
        p._end_happy_hour_import("the free hour ended")
        self.assertIsNone(p.store["import_charge_cutoff_pct"])


class TestOctopusRefreshRunsOffTheControlLoop(unittest.TestCase):

    def test_a_slow_refresh_does_not_hold_the_tick_and_is_not_doubled(self):
        p = _mk()
        gate = threading.Event()
        calls = []

        def slow():
            calls.append(1)
            gate.wait(2)
        p._refresh_octopus_rates = slow
        t0 = time.monotonic()
        self.assertTrue(p._start_octopus_refresh())
        self.assertFalse(p._start_octopus_refresh())       # still running
        self.assertLess(time.monotonic() - t0, 0.5)
        gate.set()
        p._octopus_worker.join(2)
        self.assertEqual(len(calls), 1)

    def test_the_new_rates_keep_the_last_numeric_export_rate_only(self):
        p = _mk()
        p.octopus = MagicMock()
        p.octopus.get_current_tariff.return_value = {"tariff_key": "x"}
        p.octopus.get_all_monitored_rates.return_value = {}
        p.pluginPrefs["solarOverflowShadowEnabled"] = False
        p._flux_enabled = lambda: False
        seen = []
        p._export_rate_now_p = lambda: seen.append(dict(p.latest_rates_data)) or 9.7
        for n in ("_update_tariff_device", "_write_tariff_schedule_variables"):
            setattr(p, n, MagicMock())
        p._rates_for_tariff = lambda *a: (None, None)
        p.latest_rates_data = {"export_rate_p": 27.69}
        p._refresh_octopus_rates()
        self.assertEqual(seen[0]["export_rate_p"], 27.69)
        p.latest_rates_data = {"export_rate_p": None}
        seen.clear()
        p._refresh_octopus_rates()
        self.assertNotIn("export_rate_p", seen[0])


class TestFluxProofSurvivesARestart(unittest.TestCase):

    def test_saved_and_restored_whatever_the_day(self):
        p = _mk()
        ev = {"fetched_at": time.time() - 60, "import": {"tariff_key": "flux"},
              "export": {"tariff_key": "flux"}}
        p.store["flux_account_evidence"] = ev
        plugin.Plugin._save_accumulators(p)        # the factory mocks it out
        q = DE._mk(p.data_dir)
        q.store["flux_account_evidence"] = {}
        q._load_accumulators()
        self.assertEqual(q.store["flux_account_evidence"], ev)


class TestAConfigureSaveKeepsTheBiasCorrection(unittest.TestCase):
    """Only startup() loaded the bands, so a Configure save rebuilt the forecast
    at 1.0 and the manager planned on the raw figure until midnight."""

    def test_the_bands_survive_a_rebuild(self):
        import json
        import os
        import openmeteo_forecast as om
        d = tempfile.mkdtemp()
        recs = [{"date": f"2026-09-{i:02d}", "month": "2026-09", "forecast_kwh": 30.0,
                 "actual_kwh": 24.0, "factor": 0.8} for i in range(1, 21)]
        with open(os.path.join(d, "openmeteo_accuracy_records.json"), "w",
                  encoding="utf-8") as fh:
            json.dump(recs, fh)
        p = _bare()
        p.pluginPrefs = {"siteLatitude": "54.9", "siteLongitude": "-1.8"}
        p.data_dir = d
        p.debug = False
        p.modbus = p.octopus = p.forecast = p.axle = None
        p.latest_inverter_data, p.latest_forecast_data, p.latest_rates_data = {}, {}, {}
        p.latest_decision = None
        p.sleep = lambda seconds: None
        with patch.object(om, "OPTIMISER_FORECAST_FILE", os.path.join(d, "opt.json")), \
                patch.object(plugin, "log"):
            p._init_modules()
            raw = {"todayKwh": 30.0, "tomorrowKwh": 30.0}
            self.assertAlmostEqual(
                p.forecast._enrich_forecast(raw)["correctedTomorrowKwh"], 24.0, places=1)


# -- batch 3: records and display ------------------------------------------------

class TestUnreadableRecordsAreNeverOverwritten(unittest.TestCase):

    def test_daily_history_is_moved_aside_not_replaced(self):
        import os
        p = _mk()
        path = os.path.join(p.data_dir, "daily_history.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write('[{"date": "2026-09-01"}, {"date": "2026-09-02"')     # truncated
        with patch.object(plugin, "log"):
            plugin.Plugin._write_daily_history(p, "2026-10-01")
        asides = [n for n in os.listdir(p.data_dir) if n.startswith("daily_history.json.unreadable-")]
        self.assertEqual(len(asides), 1)
        with open(os.path.join(p.data_dir, asides[0]), encoding="utf-8") as fh:
            self.assertIn("2026-09-02", fh.read())

    def test_a_ledger_that_did_not_parse_is_left_alone(self):
        import os
        p = _mk()
        path = os.path.join(p.data_dir, "vpp_ledger.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write('{"axle": {"transactions": [ {"id": "t-1"')            # truncated
        before = open(path, encoding="utf-8").read()
        p._vpp_ledger_path = lambda: path
        now = datetime.now(timezone.utc)
        with patch.object(plugin, "vpp_log"):
            p._record_vpp_ledger_event({"start_time": now - timedelta(hours=1),
                                        "end_time": now}, 3.9)
        self.assertEqual(open(path, encoding="utf-8").read(), before)


class TestTheExportCountSurvivesARestart(unittest.TestCase):

    def test_saved_and_restored_on_the_same_day(self):
        p = _mk()
        p.store["export_count_today"] = 3
        plugin.Plugin._save_accumulators(p)
        q = DE._mk(p.data_dir)
        q.store["export_count_today"] = 0
        with patch.object(plugin, "_local_today_str", return_value=p.store["today_date"]):
            q._load_accumulators()
        self.assertEqual(q.store["export_count_today"], 3)


class TestSlowInverterStatesAreNotFabricated(unittest.TestCase):

    def test_a_snapshot_without_them_writes_none_of_them(self):
        p = _mk()
        dev = MagicMock()
        p._find_device = lambda t: dev
        p.store.update({"pv_daily_kwh": 1.0, "grid_import_daily_kwh": 0.0,
                        "grid_export_daily_kwh": 0.0, "home_daily_kwh": 1.0})
        p._update_inverter_device({"batterySoc": 50.0, "gridPowerWatts": 0,
                                   "pvPowerWatts": 0, "batteryPowerWatts": 0,
                                   "homePowerWatts": 300, "gridStatus": "On-grid"})
        written = {st["key"] for call in dev.updateStatesOnServer.call_args_list
                   for st in call.args[0]}
        for key in ("batteryTempC", "batteryMinTempC", "dischargeCutoffSoc",
                    "gridSensorConnected", "emsWorkMode"):
            self.assertNotIn(key, written)
        self.assertIn("batterySoc", written)


class TestTheClocksBackNightKeepsItsEnergy(unittest.TestCase):

    def test_a_repeated_slot_is_added_to(self):
        import os
        import sqlite3
        import datetime as _dt
        p = _mk()
        p._init_timeseries_db()

        class FakeDT(_dt.datetime):
            cur = None

            @classmethod
            def now(cls, tz=None):
                return cls.cur if tz is None else _dt.datetime.now(tz)

        class Meter:
            n = 0

            def lifetime_snapshot(self):
                Meter.n += 1
                return {"pv": 0.0, "gridImport": 1000.0 + Meter.n, "gridExport": 0.0,
                        "home": 2000.0 + Meter.n}
        p.daily_energy = Meter()
        p.latest_inverter_data = {"batterySoc": 50.0}
        p.latest_rates_data, p.latest_decision = {}, None
        p._find_device = lambda t: None
        with patch.object(plugin, "datetime", FakeDT):
            for wall in ("00:20", "00:50", "01:20", "01:50", "01:20", "01:50", "02:20"):
                h, m = map(int, wall.split(":"))
                FakeDT.cur = FakeDT(2026, 10, 25, h, m)
                p._log_halfhourly_to_db_impl()
        con = sqlite3.connect(os.path.join(p.data_dir, "energy_timeseries.db"))
        total = con.execute("SELECT SUM(grid_import_kwh) FROM halfhourly").fetchone()[0]
        con.close()
        self.assertAlmostEqual(total, 6.0)


class TestAnAxleWindowOverMidnightKeepsItsExport(unittest.TestCase):

    def test_the_rebase_uses_the_ended_days_total(self):
        p = _mk()
        ended = p.store["today_date"]
        p.store.update({"grid_export_daily_kwh": 0.0,          # already rolled over
                        "energy_yesterday_projection": {"date": ended, "gridExport": 5.0}})
        self.assertEqual(p._export_total_for_ended_day(ended), 5.0)
        # A projection for some other day is not the ended day's total.
        self.assertEqual(p._export_total_for_ended_day("2000-01-01"), 0.0)


class TestTheAxleSenderIsTheAddress(unittest.TestCase):

    def test_lookalikes_are_refused(self):
        import axle_email
        self.assertTrue(axle_email.is_settlement_email(
            "Axle <noreply@axle.energy>", "Your grid event results are in"))
        for sender in ("Axle <noreply@axle.energy.payouts-example.net>",
                       '"axle.energy" <mallory@example.com>', "billing@notaxle.energy"):
            self.assertFalse(axle_email._from_axle(sender), sender)


class TestTheDashboardTokenTakesAnyText(unittest.TestCase):

    def test_a_non_ascii_token_is_refused_not_raised(self):
        import web_dashboard
        h = web_dashboard.__dict__.get("_Handler") or next(
            v for v in vars(web_dashboard).values()
            if isinstance(v, type) and hasattr(v, "_authorised"))
        obj = h.__new__(h)
        obj._auth_token = "secret"
        obj._client_is_loopback = lambda: False
        obj._presented_token = lambda: "\u00e9"
        self.assertFalse(obj._authorised())


if __name__ == "__main__":
    unittest.main()
