#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    test_plugin_daily_energy.py
# Description: plugin.py's wiring of daily_energy.py (v5.89.0) — the observe ->
#              projection path, the midnight rollover and its post-midnight anchor
#              wait, the half-hourly lifetime deltas, persistence + the v5.88
#              migration, and the reconciliation tripwire. Runs without Indigo.
# Author:      CliveS & Claude Fable 5.1
# Date:        05-09-2026 13:30
# Version:     1.0
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import MagicMock, patch

# ---- Mock the Indigo runtime + pymodbus so plugin.py imports standalone ----
# setdefault throughout: test_plugin.py installs the same stubs and the two files
# share one process under discovery, in either order.
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

import plugin                                                   # noqa: E402
from daily_energy import DailyEnergy, local_midnight_epoch      # noqa: E402
from sigenergy_modbus import ENERGY_BLOCK_KEYS                  # noqa: E402

D1 = "2026-09-04"
D2 = "2026-09-05"
MID_D1 = local_midnight_epoch(D1)
MID_D2 = local_midnight_epoch(D2)


def _mk(tmp, store=None, with_modbus=True):
    p = plugin.Plugin.__new__(plugin.Plugin)
    p.logger       = MagicMock()
    p._state_lock  = threading.RLock()
    p.data_dir     = tmp
    p._get_data_dir = lambda: tmp          # _load_accumulators resolves the dir through this
    p.pluginPrefs  = {"batteryCapacityKwh": "35.04"}
    p.latest_inverter_data  = {}
    p.latest_rates_data     = {}
    p.latest_forecast_data  = {}
    p.latest_decision       = None
    p.octopus      = None
    p.forecast     = None
    p.modbus       = MagicMock() if with_modbus else None
    p.daily_energy = DailyEnergy()
    p._find_device = lambda which: MagicMock()
    for name in ("_write_energy_summary_variables", "_save_accumulators",
                 "_log_export_sync_summary", "_backup_data_dir",
                 "_record_battery_soh_snapshot", "_save_home_profile"):
        setattr(p, name, MagicMock())
    base = {
        "pv_daily_kwh": 0.0, "grid_import_daily_kwh": 0.0, "grid_export_daily_kwh": 0.0,
        "home_daily_kwh": 0.0, "battery_charge_daily_kwh": 0.0,
        "battery_discharge_daily_kwh": 0.0, "energy_balance_kwh": 0.0,
        "energy_day_partial": False, "energy_reconcile_warned": "",
        "energy_yesterday_projection": None,
        "peak_soc": 0.0, "min_soc": 100.0, "peak_pv_w": 0, "peak_pv_time": "",
        "today_date": D1, "pv_lifetime_start_kwh": None,
        "import_lifetime_start_kwh": None, "export_lifetime_start_kwh": None,
        "power_cut_events": [], "power_cut_started_at": None,
    }
    base.update(store or {})
    p.store = base
    return p


def _data(pv=7600.0, home=4000.0, imp=57.0, exp=3590.0, chg=2470.0, dis=2390.0,
          soc=80.0, read_at=None, fresh=True, daily_home=None, daily_chg=None,
          daily_dis=None, drop=()):
    d = {"batterySoc": soc, "pvPowerWatts": 3000, "gridPowerWatts": -1000,
         "batteryPowerWatts": 1000, "_read_at": read_at or MID_D1 + 3600,
         "pvLifetimeKwh": pv, "homeLifetimeKwh": home, "gridImportLifetimeKwh": imp,
         "gridExportLifetimeKwh": exp, "batteryChargeLifetimeKwh": chg,
         "batteryDischargeLifetimeKwh": dis}
    if fresh:
        d["_energyReadAt"] = read_at or MID_D1 + 3600
    if daily_home is not None:
        d["homeDailyDirectKwh"] = daily_home
    if daily_chg is not None:
        d["batteryDailyChargeKwh"] = daily_chg
    if daily_dis is not None:
        d["batteryDailyDischargeKwh"] = daily_dis
    for k in drop:
        d.pop(k, None)
    return d


class _Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestObserveProjectsIntoTheStore(_Tmp):

    def test_daily_store_keys_are_lifetime_deltas(self):
        p = _mk(self.tmp)
        with patch.object(plugin, "_local_today_str", return_value=D1):
            p._observe_energy_counters(_data(read_at=MID_D1 + 20))
            p._observe_energy_counters(_data(pv=7612.5, home=4008.25, exp=3594.0, chg=2471.0,
                                             dis=2390.75, read_at=MID_D1 + 7200))
        self.assertEqual(p.store["pv_daily_kwh"], 12.5)
        self.assertEqual(p.store["home_daily_kwh"], 8.25)
        self.assertEqual(p.store["grid_export_daily_kwh"], 4.0)
        self.assertEqual(p.store["grid_import_daily_kwh"], 0.0)
        self.assertEqual(p.store["battery_charge_daily_kwh"], 1.0)
        self.assertEqual(p.store["battery_discharge_daily_kwh"], 0.75)
        # 12.5 + 0 + 0.75 - 4.0 - 1.0 - 8.25 = 0
        self.assertEqual(p.store["energy_balance_kwh"], 0.0)
        self.assertFalse(p.store["energy_day_partial"])

    def test_house_falls_back_to_the_identity_without_register_30094(self):
        p = _mk(self.tmp)
        with patch.object(plugin, "_local_today_str", return_value=D1):
            p._observe_energy_counters(_data(read_at=MID_D1 + 20, drop=("homeLifetimeKwh",)))
            p._observe_energy_counters(_data(pv=7620.0, exp=3595.0, chg=2472.0, dis=2391.0,
                                             read_at=MID_D1 + 7200, drop=("homeLifetimeKwh",)))
        # 20 + 0 + 1 - 5 - 2 = 14
        self.assertEqual(p.store["home_daily_kwh"], 14.0)

    def test_battery_falls_back_to_device_daily_counters_and_holds_between_reads(self):
        p = _mk(self.tmp)
        gone = ("batteryChargeLifetimeKwh", "batteryDischargeLifetimeKwh")
        with patch.object(plugin, "_local_today_str", return_value=D1):
            p._observe_energy_counters(_data(read_at=MID_D1 + 20, drop=gone,
                                             daily_chg=0.1, daily_dis=0.2))
            p._observe_energy_counters(_data(read_at=MID_D1 + 60, drop=gone))   # no daily regs this cycle
        self.assertEqual(p.store["battery_charge_daily_kwh"], 0.1)
        self.assertEqual(p.store["battery_discharge_daily_kwh"], 0.2)

    def test_a_cached_cycle_changes_nothing(self):
        p = _mk(self.tmp)
        with patch.object(plugin, "_local_today_str", return_value=D1):
            p._observe_energy_counters(_data(read_at=MID_D1 + 20))
            p._observe_energy_counters(_data(pv=7999.0, read_at=MID_D1 + 60, fresh=False))
        self.assertEqual(p.store["pv_daily_kwh"], 0.0)

    def test_inverter_device_states_come_from_the_projection(self):
        p = _mk(self.tmp, store={"battery_charge_daily_kwh": 3.3, "battery_discharge_daily_kwh": 4.4,
                                 "energy_balance_kwh": -0.12, "home_daily_kwh": 7.9})
        dev = MagicMock()
        p._find_device = lambda which: dev
        p._update_inverter_device({"batterySoc": 50.0})          # no daily registers this cycle
        st = {s["key"]: s["value"] for s in dev.updateStatesOnServer.call_args[0][0]}
        self.assertEqual(st["batteryDailyChargeKwh"], 3.3)     # NOT a fabricated 0.0
        self.assertEqual(st["batteryDailyDischargeKwh"], 4.4)
        self.assertEqual(st["energyBalanceKwh"], -0.12)
        self.assertEqual(st["homeDailyKwh"], 7.9)

    def test_first_observe_of_a_new_day_snapshots_yesterday_and_forces_fresh_blocks(self):
        p = _mk(self.tmp)
        with patch.object(plugin, "_local_today_str", return_value=D1):
            p._observe_energy_counters(_data(read_at=MID_D1 + 20))
            p._observe_energy_counters(_data(pv=7630.0, home=4020.0, exp=3600.0,
                                             read_at=MID_D2 - 30))
        with patch.object(plugin, "_local_today_str", return_value=D2):
            p._observe_energy_counters(_data(pv=7630.0, home=4020.02, exp=3600.0,
                                             read_at=MID_D2 + 15))
        snap = p.store["energy_yesterday_projection"]
        self.assertEqual(snap["date"], D1)
        self.assertEqual(snap["pv"], 30.0)
        self.assertEqual(snap["home"], 20.0)
        p.modbus.mark_slow_read_due.assert_called_once_with(*ENERGY_BLOCK_KEYS)
        self.assertEqual(p.store["pv_daily_kwh"], 0.0)          # the new day starts at zero
        self.assertEqual(p.daily_energy.today_date, D2)


class TestMidnightRollover(_Tmp):
    """The regression this whole revamp exists for: the record written at midnight
    must be YESTERDAY's total even though the projection already shows today."""

    def _run_day_then_midnight(self, p, now_after_midnight, fresh_after=True):
        with patch.object(plugin, "_local_today_str", return_value=D1):
            p._observe_energy_counters(_data(read_at=MID_D1 + 20))
            p._observe_energy_counters(_data(pv=7630.0, home=4020.0, exp=3600.0, chg=2472.0,
                                             dis=2394.0, read_at=MID_D2 - 30))
        with patch.object(plugin, "_local_today_str", return_value=D2):
            if fresh_after:
                p._observe_energy_counters(_data(pv=7630.0, home=4020.02, exp=3600.0, chg=2472.0,
                                                 dis=2394.0, read_at=MID_D2 + 15))
            with patch("plugin.time.time", return_value=now_after_midnight):
                p._check_midnight_impl()

    def _history(self, p):
        path = os.path.join(p.data_dir, "daily_history.json")
        with open(path, encoding="utf-8") as fh:
            return {r["date"]: r for r in json.load(fh)}

    def test_yesterdays_record_is_exact_not_the_new_days_zeros(self):
        p = _mk(self.tmp)
        self._run_day_then_midnight(p, MID_D2 + 30)
        rec = self._history(p)[D1]
        self.assertEqual(rec["pv_kwh"], 30.0)
        self.assertEqual(rec["home_kwh"], 20.02)        # up to the true boundary reading
        self.assertEqual(rec["grid_export_kwh"], 10.0)
        self.assertEqual(rec["battery_charge_kwh"], 2.0)
        self.assertEqual(rec["battery_discharge_kwh"], 4.0)
        # 30 + 0 + 4 - 10 - 2 - 20.02 = 1.98 — the flows in this fixture do not
        # balance, and the record must SAY so rather than hide it.
        self.assertEqual(rec["energy_balance_kwh"], 1.98)
        self.assertFalse(rec["energy_partial"])
        self.assertEqual(rec["energy_sources"]["pv"], "midnight")
        self.assertEqual(p.store["today_date"], D2)
        self.assertEqual(p.store["pv_daily_kwh"], 0.0)
        self.assertEqual(p.store["energy_reconcile_warned"], "")

    def test_midnight_task_waits_for_the_post_midnight_read_then_proceeds(self):
        p = _mk(self.tmp)
        with patch.object(plugin, "_local_today_str", return_value=D1):
            p._observe_energy_counters(_data(read_at=MID_D1 + 20))
            p._observe_energy_counters(_data(pv=7630.0, read_at=MID_D2 - 30))
        p.daily_energy.rollover(D2)                     # provisional, nothing fresh yet
        with patch.object(plugin, "_local_today_str", return_value=D2):
            with patch("plugin.time.time", return_value=MID_D2 + 120):
                p._check_midnight_impl()
            self.assertEqual(p.store["today_date"], D1, "must wait inside the window")
            with patch("plugin.time.time", return_value=MID_D2 + plugin.MIDNIGHT_ANCHOR_WAIT_S + 5):
                p._check_midnight_impl()
        self.assertEqual(p.store["today_date"], D2)
        self.assertEqual(self._history(p)[D1]["pv_kwh"], 30.0)

    def test_modbus_out_at_midnight_still_records_and_rolls_over(self):
        """No observe between the last D1 read and the midnight task: the task
        itself rolls the object over and forces fresh blocks."""
        p = _mk(self.tmp)
        self._run_day_then_midnight(p, MID_D2 + 700, fresh_after=False)
        self.assertEqual(self._history(p)[D1]["pv_kwh"], 30.0)
        self.assertEqual(p.store["today_date"], D2)
        self.assertEqual(p.daily_energy.today_date, D2)
        p.modbus.mark_slow_read_due.assert_called_with(*ENERGY_BLOCK_KEYS)

    def test_no_daily_energy_object_zeroes_the_keys_without_crashing(self):
        p = _mk(self.tmp, store={"pv_daily_kwh": 9.0, "home_daily_kwh": 4.0})
        del p.daily_energy
        with patch.object(plugin, "_local_today_str", return_value=D2):
            with patch("plugin.time.time", return_value=MID_D2 + 700):
                p._check_midnight_impl()
        self.assertEqual(p.store["pv_daily_kwh"], 0.0)
        self.assertEqual(p.store["today_date"], D2)
        self.assertEqual(self._history(p)[D1]["pv_kwh"], 9.0)   # the projection as it stood


class TestHalfhourlyLifetimeDeltas(_Tmp):

    def _rows(self, p):
        con = sqlite3.connect(os.path.join(p.data_dir, "energy_timeseries.db"))
        try:
            con.row_factory = sqlite3.Row
            return [dict(r) for r in con.execute("SELECT * FROM halfhourly ORDER BY id")]
        finally:
            con.close()

    def test_slot_spanning_midnight_keeps_its_energy(self):
        p = _mk(self.tmp)
        p._init_timeseries_db()
        with patch.object(plugin, "_local_today_str", return_value=D1):
            p._observe_energy_counters(_data(read_at=MID_D1 + 20))
            p._observe_energy_counters(_data(pv=7600.4, home=4001.0, read_at=MID_D2 - 900))
            p._log_halfhourly_to_db_impl()               # seeds, writes nothing
        self.assertEqual(self._rows(p), [])
        with patch.object(plugin, "_local_today_str", return_value=D2):
            p._observe_energy_counters(_data(pv=7600.4, home=4001.6, exp=3590.0, chg=2470.3,
                                             read_at=MID_D2 + 900))
            p._log_halfhourly_to_db_impl()
        rows = self._rows(p)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["home_kwh"], 0.6)          # the pre-midnight part is kept
        self.assertEqual(rows[0]["battery_charge_kwh"], 0.3)
        self.assertEqual(rows[0]["battery_discharge_kwh"], 0.0)
        self.assertEqual(rows[0]["pv_kwh"], 0.0)

    def test_old_daily_store_anchors_are_ignored_and_reseeded(self):
        p = _mk(self.tmp, store={"hh_anchor_pv_kwh": 3.0, "hh_anchor_soc_pct": 50.0})
        p._init_timeseries_db()
        with patch.object(plugin, "_local_today_str", return_value=D1):
            p._observe_energy_counters(_data(read_at=MID_D1 + 20))
            p._log_halfhourly_to_db_impl()
        self.assertEqual(self._rows(p), [])
        self.assertIn("hh_anchor_lifetime", p.store)

    def test_new_columns_are_added_to_an_existing_table(self):
        db = os.path.join(self.tmp, "energy_timeseries.db")
        con = sqlite3.connect(db)
        con.execute("""CREATE TABLE halfhourly (id INTEGER PRIMARY KEY AUTOINCREMENT,
            slot_start TEXT NOT NULL UNIQUE, slot_end TEXT NOT NULL,
            grid_import_kwh REAL NOT NULL DEFAULT 0.0, grid_export_kwh REAL NOT NULL DEFAULT 0.0,
            pv_kwh REAL NOT NULL DEFAULT 0.0, home_kwh REAL NOT NULL DEFAULT 0.0,
            battery_soc_start_pct REAL, battery_soc_end_pct REAL, battery_net_kwh REAL,
            tracker_price_p REAL, manager_action TEXT)""")
        con.commit(); con.close()
        p = _mk(self.tmp)
        p._init_timeseries_db()
        con = sqlite3.connect(db)
        cols = {r[1] for r in con.execute("PRAGMA table_info(halfhourly)")}
        con.close()
        self.assertTrue({"agile_price_p", "battery_charge_kwh", "battery_discharge_kwh"} <= cols)


class TestPersistenceAndMigration(_Tmp):

    def test_anchors_are_saved_and_restored_whatever_the_day(self):
        p = _mk(self.tmp)
        with patch.object(plugin, "_local_today_str", return_value=D1):
            p._observe_energy_counters(_data(read_at=MID_D1 + 20))
            p._observe_energy_counters(_data(pv=7605.0, read_at=MID_D1 + 3600))
        path = os.path.join(self.tmp, "accumulators.json")
        p._save_accumulators_locked(path)
        with open(path, encoding="utf-8") as fh:
            payload = json.load(fh)
        self.assertIn(D1, payload["daily_energy"]["anchors"])
        q = _mk(self.tmp)
        with patch.object(plugin, "_local_today_str", return_value=D2):   # a different day
            q._load_accumulators()
        self.assertIn(D1, q.daily_energy.anchors)
        self.assertEqual(q.daily_energy.completed(D1)["values"]["pv"], 5.0)

    def test_pre_589_file_seeds_three_anchors_on_the_same_day(self):
        legacy = {"today_date": D1, "pv_daily_kwh": 15.9, "grid_import_daily_kwh": 0.06,
                  "grid_export_daily_kwh": 2.87, "home_daily_kwh": 18.6,
                  "peak_soc": 93.7, "min_soc": 72.7,
                  "pv_lifetime_start_kwh": 7613.16, "import_lifetime_start_kwh": 57.05,
                  "export_lifetime_start_kwh": 3593.99}
        with open(os.path.join(self.tmp, "accumulators.json"), "w", encoding="utf-8") as fh:
            json.dump(legacy, fh)
        p = _mk(self.tmp)
        with patch.object(plugin, "_local_today_str", return_value=D1), \
             patch.object(plugin, "log") as lg:
            p._load_accumulators()
            self.assertTrue(any("carried over" in str(c) for c in lg.call_args_list))
            # the first read: house / battery recover from the device's own counters
            p._observe_energy_counters(_data(pv=7629.06, imp=57.11, exp=3596.86, home=4014.9,
                                             chg=2471.58, dis=2396.45, read_at=MID_D1 + 12 * 3600,
                                             daily_home=12.05, daily_chg=7.93, daily_dis=6.07))
        self.assertEqual(p.store["pv_daily_kwh"], 15.9)
        self.assertEqual(p.store["grid_export_daily_kwh"], 2.87)
        self.assertEqual(p.store["home_daily_kwh"], 12.05)      # NOT the frozen 18.6
        self.assertEqual(p.store["battery_charge_daily_kwh"], 7.93)
        self.assertFalse(p.store["energy_day_partial"])

    def test_pre_589_file_on_another_day_seeds_nothing(self):
        legacy = {"today_date": D1, "pv_lifetime_start_kwh": 7613.16}
        with open(os.path.join(self.tmp, "accumulators.json"), "w", encoding="utf-8") as fh:
            json.dump(legacy, fh)
        p = _mk(self.tmp)
        with patch.object(plugin, "_local_today_str", return_value=D2):
            p._load_accumulators()
        self.assertEqual(p.daily_energy.anchors, {})


class TestReconciliationTripwire(_Tmp):

    def test_wrong_anchor_warns_once_then_clears(self):
        p = _mk(self.tmp)
        with patch.object(plugin, "_local_today_str", return_value=D1), \
             patch.object(plugin, "log") as lg:
            p._observe_energy_counters(_data(read_at=MID_D1 + 20))
            p.daily_energy.set_anchor(D1, {"home": 4000.0 - 10.5})   # the 4-Sep-2026 shape
            p._observe_energy_counters(_data(pv=7610.0, home=4010.0, read_at=MID_D1 + 3600))
            warnings = [c for c in lg.call_args_list if c.kwargs.get("level") == "WARNING"]
            self.assertEqual(len(warnings), 1)
            self.assertIn("energy balance off by -10.50 kWh", str(warnings[0]))
            p._observe_energy_counters(_data(pv=7611.0, home=4011.0, read_at=MID_D1 + 3700))
            warnings = [c for c in lg.call_args_list if c.kwargs.get("level") == "WARNING"]
            self.assertEqual(len(warnings), 1, "must not repeat while the fault persists")
            p.daily_energy.set_anchor(D1, {"home": 4000.0})
            p._observe_energy_counters(_data(pv=7612.0, home=4012.0, read_at=MID_D1 + 3800))
            self.assertTrue(any("back within bounds" in str(c) for c in lg.call_args_list))
        self.assertEqual(p.store["energy_reconcile_warned"], "")

    def test_derived_house_is_checked_against_the_inverters_daily_counter(self):
        p = _mk(self.tmp)
        with patch.object(plugin, "_local_today_str", return_value=D1), \
             patch.object(plugin, "log") as lg:
            p._observe_energy_counters(_data(read_at=MID_D1 + 20))
            p._observe_energy_counters(_data(pv=7610.0, home=4010.0, exp=3600.0,
                                             read_at=MID_D1 + 3600, daily_home=4.0))
            msgs = [str(c) for c in lg.call_args_list if c.kwargs.get("level") == "WARNING"]
        self.assertTrue(any("disagrees with the inverter's own daily counter" in m for m in msgs))

    def test_a_balanced_day_stays_quiet(self):
        p = _mk(self.tmp)
        with patch.object(plugin, "_local_today_str", return_value=D1), \
             patch.object(plugin, "log") as lg:
            p._observe_energy_counters(_data(read_at=MID_D1 + 20))
            p._observe_energy_counters(_data(pv=7610.0, home=4006.0, exp=3594.0,
                                             read_at=MID_D1 + 3600, daily_home=6.0))
            self.assertFalse([c for c in lg.call_args_list if c.kwargs.get("level") == "WARNING"])


if __name__ == "__main__":
    unittest.main()
