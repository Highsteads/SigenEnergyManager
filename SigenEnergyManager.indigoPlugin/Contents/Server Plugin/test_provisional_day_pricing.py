#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    test_provisional_day_pricing.py
# Description: A day recorded before Octopus published one side of the Flux prices is
#              valued at the last published day's prices, marked "provisional", and
#              re-priced (settled money included) once the real prices are published
#              (SigenEnergyManager 5.129.2). CliveS, 1-Oct-2026: "do it".
# Author:      CliveS & Claude Opus 5.5
# Date:        01-10-2026
# Version:     1.0

import json
import os
import unittest
from datetime import datetime
from unittest.mock import patch

import test_flux_supervisor as tfs   # its indigo stub and fixtures
from test_flux_supervisor import LONDON, _seed_flux_day, _mk_plugin, _evidence

plugin = tfs.plugin
fs     = tfs.fs

DAY = "2026-09-18"           # the second seeded day


def _plugin_with_export_unpublished():
    """Import published for 17 and 18 September, export for the 17th only."""
    p = _mk_plugin()
    _seed_flux_day(p, datetime(2026, 9, 17, 17, 0, tzinfo=LONDON))
    ev = _evidence()
    ev["export_valid_from"] = "2026-09-16T23:00:00Z"
    ev["import_valid_from"] = "2026-09-16T23:00:00Z"
    p.store["flux_account_evidence"] = ev
    p.store["_published_export"] = list(p.store["flux_export_slots"])
    p.store["flux_export_slots"] = [s for s in p.store["flux_export_slots"]
                                    if str(s["valid_from"]) < "2026-09-17T23:00:00Z"]
    return p


def _series(p, rows):
    import sqlite3
    con = sqlite3.connect(os.path.join(p.data_dir, "energy_timeseries.db"))
    con.execute("CREATE TABLE halfhourly (slot_start TEXT, slot_end TEXT, "
                "grid_export_kwh REAL, grid_import_kwh REAL)")
    con.executemany("INSERT INTO halfhourly VALUES (?, ?, ?, ?)", rows)
    con.commit()
    con.close()


ROWS = [(f"{DAY}T16:00:00", f"{DAY}T16:30:00", 2.0, 0.0),     # peak
        (f"{DAY}T11:00:00", f"{DAY}T11:30:00", 2.0, 0.0)]     # day


class TestAnUnpublishedDayIsProvisional(unittest.TestCase):

    def test_published_only_still_refuses_it(self):
        p = _plugin_with_export_unpublished()
        _series(p, ROWS)
        with patch.object(plugin, "log"):
            self.assertIsNone(p._export_rate_for_day_p(DAY))

    def test_the_record_gets_the_carried_prices_marked_provisional(self):
        p = _plugin_with_export_unpublished()
        _series(p, ROWS)
        with patch.object(plugin, "log"):
            rate, basis = p._export_rate_and_basis(DAY, 12.0)
        self.assertEqual(basis, "provisional")
        self.assertAlmostEqual(rate, (29.6 + 10.2) / 2.0, places=2)

    def test_a_day_that_cannot_be_priced_even_so_stays_estimated(self):
        p = _plugin_with_export_unpublished()
        p.store["flux_import_slots"] = []           # nothing to carry towards
        _series(p, ROWS)
        with patch.object(plugin, "log"):
            rate, basis = p._export_rate_and_basis(DAY, 12.0)
        self.assertEqual((rate, basis), (12.0, "estimated"))


class TestPublicationRepricesTheDay(unittest.TestCase):

    def _history(self, p, rows):
        with open(os.path.join(p.data_dir, "daily_history.json"), "w",
                  encoding="utf-8") as fh:
            json.dump(rows, fh)

    def _read(self, p):
        with open(os.path.join(p.data_dir, "daily_history.json"), encoding="utf-8") as fh:
            return json.load(fh)

    def _settled_row(self, rate):
        return {"date": DAY, "grid_export_kwh": 4.0, "export_rate_p": rate,
                "export_rate_basis": "provisional", "rate_today_p": 20.0,
                "import_rate_basis": "weighted", "cost_settled": True,
                "import_kwh_octo": 10.0, "elec_unit_cost_gbp": 2.0,
                "elec_standing_gbp": 0.6, "gas_unit_cost_gbp": 1.0,
                "gas_standing_gbp": 0.3, "whole_house_bill_gbp": 3.9,
                "export_revenue_gbp": round(4.0 * rate / 100.0, 2),
                "wh_net_gbp": round(4.0 * rate / 100.0 - 3.9, 2), "covered": False}

    def test_nothing_changes_while_still_unpublished(self):
        p = _plugin_with_export_unpublished()
        _series(p, ROWS)
        self._history(p, [self._settled_row(19.9)])
        with patch.object(plugin, "log"):
            self.assertEqual(p._reprice_provisional_days(), 0)
        self.assertEqual(self._read(p)[0]["export_rate_basis"], "provisional")

    def test_publication_replaces_the_rate_and_the_settled_money(self):
        p = _plugin_with_export_unpublished()
        _series(p, ROWS)
        self._history(p, [self._settled_row(9.0)])        # a provisional figure
        p.store["flux_export_slots"] = p.store["_published_export"]
        logged = []
        with patch.object(plugin, "log", side_effect=lambda m, level="INFO": logged.append(m)):
            self.assertEqual(p._reprice_provisional_days(), 1)
        row = self._read(p)[0]
        rate = (29.6 + 10.2) / 2.0
        self.assertEqual(row["export_rate_basis"], "weighted")
        self.assertAlmostEqual(row["export_rate_p"], rate, places=2)
        self.assertEqual(row["export_rate_p_provisional"], 9.0)
        self.assertAlmostEqual(row["export_revenue_gbp"], round(4.0 * rate / 100.0, 2))
        self.assertAlmostEqual(row["wh_net_gbp"],
                               round(4.0 * rate / 100.0 - 3.9, 2), places=2)
        self.assertIn("18 September re-priced at the export prices", logged[0])

    def test_a_row_without_its_own_import_rate_keeps_its_bill(self):
        p = _plugin_with_export_unpublished()
        _series(p, ROWS)
        row = self._settled_row(9.0)
        row["rate_today_p"] = None
        self._history(p, [row])
        p.store["flux_export_slots"] = p.store["_published_export"]
        with patch.object(plugin, "log"):
            p._reprice_provisional_days()
        out = self._read(p)[0]
        self.assertEqual(out["whole_house_bill_gbp"], 3.9)
        self.assertEqual(out["elec_unit_cost_gbp"], 2.0)

    def test_a_new_import_rate_rebuilds_the_bill(self):
        row = self._settled_row(20.0)
        row["rate_today_p"] = 25.0          # re-priced import: 10 kWh now 2.50
        plugin.Plugin._resettle_day_money(row)
        self.assertEqual(row["elec_unit_cost_gbp"], 2.5)
        self.assertEqual(row["whole_house_bill_gbp"], 4.4)
        self.assertEqual(row["wh_net_gbp"], round(0.8 - 4.4, 2))

    def test_published_days_are_never_touched(self):
        p = _plugin_with_export_unpublished()
        _series(p, ROWS)
        row = self._settled_row(9.0)
        row["export_rate_basis"] = "weighted"
        self._history(p, [row])
        p.store["flux_export_slots"] = p.store["_published_export"]
        with patch.object(plugin, "log"):
            self.assertEqual(p._reprice_provisional_days(), 0)
        self.assertEqual(self._read(p)[0]["export_rate_p"], 9.0)

    def test_a_successful_rate_refresh_runs_the_repricing(self):
        from unittest.mock import MagicMock
        p = _plugin_with_export_unpublished()
        p.octopus = MagicMock()
        p.octopus.get_account_agreements.return_value = {
            "import": {"product_code": "FLUX-IMPORT-23-02-14",
                       "tariff_code": "E-1R-FLUX-IMPORT-23-02-14-F"},
            "export": {"product_code": "FLUX-EXPORT-23-02-14",
                       "tariff_code": "E-1R-FLUX-EXPORT-23-02-14-F"}}
        p.octopus._fetch_rate_schedule.return_value = [
            {"valid_from": "2026-09-18T00:00:00Z", "valid_to": "2026-09-18T01:00:00Z",
             "value_inc_vat": 9.0}]
        with patch.object(p, "_reprice_provisional_days") as rp:
            p._refresh_flux_rates()
        rp.assert_called_once()
        p.octopus.get_account_agreements.return_value = {}
        with patch.object(p, "_reprice_provisional_days") as rp:
            p._refresh_flux_rates()
        rp.assert_not_called()          # a failed refresh re-prices nothing

if __name__ == "__main__":
    unittest.main()
