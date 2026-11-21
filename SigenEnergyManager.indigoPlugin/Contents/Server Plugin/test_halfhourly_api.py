#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    test_halfhourly_api.py
# Description: /api/halfhourly (5.146.0) - the slots and free-hour credits the
#              Dashboards Savings page prices. Builds a real SQLite store in a
#              temporary folder, so the SQL itself is exercised.
# Author:      CliveS & Claude Opus 5.5
# Date:        21-11-2026
# Version:     1.0

import json
import os
import sqlite3
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta
from unittest.mock import MagicMock

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


def _make_db(path, starts):
    con = sqlite3.connect(path)
    con.execute("""CREATE TABLE halfhourly (
        id INTEGER PRIMARY KEY AUTOINCREMENT, slot_start TEXT NOT NULL UNIQUE,
        slot_end TEXT NOT NULL, grid_import_kwh REAL NOT NULL DEFAULT 0.0,
        grid_export_kwh REAL NOT NULL DEFAULT 0.0, pv_kwh REAL NOT NULL DEFAULT 0.0,
        home_kwh REAL NOT NULL DEFAULT 0.0, battery_soc_start_pct REAL,
        battery_soc_end_pct REAL, battery_net_kwh REAL, tracker_price_p REAL,
        manager_action TEXT, agile_price_p REAL, battery_charge_kwh REAL,
        battery_discharge_kwh REAL)""")
    for i, s in enumerate(starts):
        e = (datetime.fromisoformat(s) + timedelta(minutes=30)).isoformat(timespec="seconds")
        con.execute("""INSERT INTO halfhourly (slot_start, slot_end, grid_import_kwh,
            grid_export_kwh, pv_kwh, home_kwh, battery_soc_start_pct,
            battery_soc_end_pct, tracker_price_p, battery_charge_kwh,
            battery_discharge_kwh) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (s, e, 0.1, 0.2, 1.5, 0.4, 50.0, 52.0, 25.6271,
                     None if i == 0 else 0.8, None if i == 0 else 0.0))
    con.commit()
    con.close()


class TestHalfHourlyApi(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.p = plugin.Plugin.__new__(plugin.Plugin)
        self.p.data_dir = self.tmp.name
        self.p.logger = MagicMock()
        now = datetime.now().replace(microsecond=0)
        self.old = (now - timedelta(days=40)).isoformat(timespec="seconds")
        self.new1 = (now - timedelta(days=2)).isoformat(timespec="seconds")
        self.new2 = (now - timedelta(days=1)).isoformat(timespec="seconds")
        _make_db(os.path.join(self.tmp.name, "energy_timeseries.db"),
                 [self.old, self.new1, self.new2])
        with open(os.path.join(self.tmp.name, "free_hour_credits.json"), "w") as f:
            json.dump({"version": 3, "claims": {},
                       "assigned_credit_keys": ["2026-10-04|299|WEEKEND_HAPPY_HOUR",
                                                "garbage", "2026-10-05|x|BAD"]}, f)

    def tearDown(self):
        self.tmp.cleanup()

    def test_window_fields_and_order(self):
        out = self.p.get_dashboard_halfhourly(days=30)
        self.assertEqual(out["fields"], list(plugin.Plugin.HALFHOURLY_FIELDS))
        starts = [r[0] for r in out["rows"]]
        self.assertEqual(starts, [self.new1, self.new2])      # the 40-day-old slot is outside
        row = dict(zip(out["fields"], out["rows"][0]))
        self.assertEqual(row["pv_kwh"], 1.5)
        self.assertEqual(row["home_kwh"], 0.4)
        self.assertEqual(row["import_kwh"], 0.1)
        self.assertEqual(row["export_kwh"], 0.2)
        self.assertEqual(row["import_rate_p"], 25.6271)
        self.assertEqual(row["battery_charge_kwh"], 0.8)

    def test_null_battery_columns_pass_through(self):
        out = self.p.get_dashboard_halfhourly(days=60)
        row = dict(zip(out["fields"], out["rows"][0]))
        self.assertIsNone(row["battery_charge_kwh"])

    def test_free_hour_credits_skip_malformed_keys(self):
        out = self.p.get_dashboard_halfhourly(days=30)
        self.assertEqual(out["free_hour_credits"],
                         [{"date": "2026-10-04", "pence": 299, "reason": "WEEKEND_HAPPY_HOUR"}])

    def test_axle_payments_listed_whole(self):
        with open(os.path.join(self.tmp.name, "vpp_ledger.json"), "w") as f:
            json.dump({"schema": 1, "axle": {"transactions": [
                {"transaction_type": "flex event", "start_time": "2026-03-26T07:30:00+00:00",
                 "end_time": "2026-03-26T08:30:00+00:00", "credit_pence": 350, "flex_kwh": -3.5},
                {"transaction_type": "flex period top-up", "start_time": "2026-04-01T00:00:00+00:00",
                 "end_time": "2026-04-30T23:59:59+00:00", "credit_pence": 888, "flex_kwh": None},
                {"transaction_type": "flex event", "credit_pence": "bad"},
            ]}, "local": []}, f)
        out = self.p.get_dashboard_halfhourly(days=30)
        self.assertEqual([a["pence"] for a in out["axle_payments"]], [350, 888])
        self.assertEqual(out["axle_payments"][0]["type"], "flex event")
        self.assertEqual(out["axle_payments"][1]["end"], "2026-04-30T23:59:59+00:00")

    def test_no_ledger_is_empty_not_an_error(self):
        out = self.p.get_dashboard_halfhourly(days=30)
        self.assertEqual(out["axle_payments"], [])

    def test_no_store_is_empty_not_an_error(self):
        os.remove(os.path.join(self.tmp.name, "energy_timeseries.db"))
        out = self.p.get_dashboard_halfhourly(days=30)
        self.assertEqual(out["rows"], [])

    def test_json_serialisable(self):
        json.dumps(self.p.get_dashboard_halfhourly(days=30))


if __name__ == "__main__":
    unittest.main()
