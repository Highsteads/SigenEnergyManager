#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    test_fixed_windows_and_carried_prices.py
# Description: A fixed-band tariff (Flux, Go, iGo, iFlux) keeps its windows and its last
#              known prices when Octopus cannot be reached (v5.132.3). CliveS, 18-Oct-2026:
#              "as flux is fixed times and if you cannot get a price then use the last
#              known price until a update arrives".
# Author:      CliveS & Claude Sonnet 5.5
# Date:        18-10-2026
# Version:     1.0

import os
import sys
import threading
import types
import unittest
from datetime import date
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
import flux_strategy as fs                      # noqa: E402
from battery_manager import Decision, ACTION_SELF_CONSUMPTION   # noqa: E402


def _build(rates):
    fake = types.SimpleNamespace(latest_rates_data=rates,
                                 _rates_for_tariff=plugin.Plugin._rates_for_tariff)
    with patch.object(plugin, "log"):
        return plugin.Plugin._build_tariff_data(fake)


class TestFixedWindowWhenNothingHasArrived(unittest.TestCase):
    """The windows of a fixed-band tariff do not depend on a fetch. Straight after a
    restart with Octopus unreachable, rates["flux"] is empty and the manager held the
    import because "the cheap window times are not known"."""

    def test_flux_with_no_rate_data_gets_the_fixed_windows(self):
        t = _build({"tariff_info": {"tariff_key": "flux"}})
        self.assertEqual((t.cheap_start, t.cheap_end), ("02:00", "05:00"))
        self.assertEqual((t.peak_start, t.peak_end), ("16:00", "19:00"))

    def test_go_with_an_empty_block_gets_its_fixed_window(self):
        t = _build({"tariff_info": {"tariff_key": "go"}, "go": {}})
        self.assertEqual((t.cheap_start, t.cheap_end), ("00:30", "05:30"))

    def test_the_live_window_wins_over_the_table(self):
        live = {"cheap_start": "01:30", "cheap_end": "04:30", "cheap_p": 13.9,
                "peak_start": "16:00", "peak_end": "19:00"}
        t = _build({"tariff_info": {"tariff_key": "flux"}, "flux": live})
        self.assertEqual((t.cheap_start, t.cheap_end), ("01:30", "04:30"))
        self.assertEqual(t.cheap_rate_p, 13.9)

    def test_a_carried_price_is_not_thrown_away_for_the_table(self):
        # Window present, price missing: the window stays whatever it was.
        t = _build({"tariff_info": {"tariff_key": "flux"},
                    "flux": {"cheap_start": "02:00", "cheap_end": "05:00", "cheap_p": None}})
        self.assertEqual((t.cheap_start, t.cheap_end), ("02:00", "05:00"))
        self.assertIsNone(t.cheap_rate_p)

    def test_a_tariff_with_no_fixed_window_is_never_given_one(self):
        for key in ("agile", "tracker", "flexible", "unknown"):
            t = _build({"tariff_info": {"tariff_key": key}})
            self.assertIsNone(t.cheap_start, key)
            self.assertIsNone(t.cheap_end, key)

    def test_nothing_fetched_at_all_is_still_unknown(self):
        t = _build({})
        self.assertEqual(t.tariff_key, "unknown")
        self.assertIsNone(t.cheap_start)

    def test_the_fixed_table_matches_the_published_windows(self):
        self.assertEqual(octopus_api.fixed_tou_window(octopus_api.TARIFF_FLUX),
                         {"cheap_start": "02:00", "cheap_end": "05:00",
                          "peak_start": "16:00", "peak_end": "19:00"})
        self.assertIsNone(octopus_api.fixed_tou_window(octopus_api.TARIFF_AGILE))


def _slot(frm, to, p):
    return {"valid_from": frm, "valid_to": to, "value_inc_vat": p, "value_exc_vat": p}


class TestCarryForwardOneDay(unittest.TestCase):

    def test_bands_move_one_london_day(self):
        today = [_slot("2026-10-17T18:00:00Z", "2026-10-18T01:00:00Z", 23.1946),
                 _slot("2026-10-18T01:00:00Z", "2026-10-18T04:00:00Z", 13.9223)]
        out = octopus_api.carry_slots_forward_one_day(today)
        self.assertEqual([(s["valid_from"], s["valid_to"], s["value_inc_vat"]) for s in out],
                         [("2026-10-18T18:00:00Z", "2026-10-19T01:00:00Z", 23.1946),
                          ("2026-10-19T01:00:00Z", "2026-10-19T04:00:00Z", 13.9223)])

    def test_the_input_is_not_changed_and_the_copy_is_marked(self):
        today = [_slot("2026-10-18T01:00:00Z", "2026-10-18T04:00:00Z", 13.9223)]
        out = octopus_api.carry_slots_forward_one_day(today)
        self.assertEqual(today[0]["valid_from"], "2026-10-18T01:00:00Z")
        self.assertNotIn("carried_from_today", today[0])
        self.assertTrue(out[0]["carried_from_today"])

    def test_the_clocks_going_back_keep_the_local_time(self):
        # 24 Oct 02:00 BST is 01:00Z. On 25 Oct the clocks go back at 02:00 BST, so
        # 02:00 local is 02:00 GMT = 02:00Z. A flat +24h would say 01:00Z, an hour out.
        out = octopus_api.carry_slots_forward_one_day(
            [_slot("2026-10-24T01:00:00Z", "2026-10-24T04:00:00Z", 13.9223)])
        self.assertEqual(out[0]["valid_from"], "2026-10-25T02:00:00Z")
        self.assertEqual(out[0]["valid_to"],   "2026-10-25T05:00:00Z")

    def test_an_open_ended_span_stays_open_ended(self):
        out = octopus_api.carry_slots_forward_one_day(
            [{"valid_from": "2026-10-17T23:00:00Z", "valid_to": None, "value_inc_vat": 23.2}])
        self.assertEqual(out[0]["valid_from"], "2026-10-18T23:00:00Z")
        self.assertIsNone(out[0]["valid_to"])

    def test_one_unreadable_slot_carries_nothing(self):
        # Half a day of prices is worse than none: the optimiser would plan on it.
        self.assertEqual(octopus_api.carry_slots_forward_one_day(
            [_slot("2026-10-18T01:00:00Z", "2026-10-18T04:00:00Z", 13.9),
             {"valid_from": "not a time", "valid_to": "x", "value_inc_vat": 1}]), [])
        self.assertEqual(octopus_api.carry_slots_forward_one_day(
            [{"valid_to": "2026-10-18T04:00:00Z", "value_inc_vat": 1}]), [])

    def test_nothing_in_nothing_out(self):
        self.assertEqual(octopus_api.carry_slots_forward_one_day([]), [])
        self.assertEqual(octopus_api.carry_slots_forward_one_day(None), [])


class TestTomorrowVariableIsNeverYesterdaysTomorrow(unittest.TestCase):
    """18-Oct-2026: elec_rates_tomorrow_json held TODAY's bands with today's dates.
    It had been written as "tomorrow" on the 17th and, with tomorrow unpublished,
    never touched again after midnight, so it read as a day that had already arrived."""

    TODAY = [_slot("2026-10-17T18:00:00Z", "2026-10-18T01:00:00Z", 23.1946),
             _slot("2026-10-18T01:00:00Z", "2026-10-18T04:00:00Z", 13.9223)]

    def _run(self, tariff_key, tomorrow):
        written = {}
        fake = types.SimpleNamespace(
            octopus=types.SimpleNamespace(get_active_rate_schedule=lambda: {
                "product_code": "FLUX-IMPORT-23-02-14",
                "today_slots": list(self.TODAY), "tomorrow_slots": tomorrow}),
            _sigenergy_folder_id=lambda: 1,
            _ensure_var=lambda name, folder: name,
            _export_tariff_is_banded=lambda: False,
            logger=MagicMock(), store={})
        fake_indigo = MagicMock()
        fake_indigo.variable.updateValue.side_effect = lambda n, v: written.__setitem__(n, v)
        with patch.object(plugin, "indigo", fake_indigo), patch.object(plugin, "log"):
            plugin.Plugin._write_tariff_schedule_variables(
                fake, {"tariff_key": tariff_key}, {})
        return written

    def test_flux_with_tomorrow_unpublished_carries_todays_bands_forward(self):
        import json
        w = self._run("flux", [])
        got = json.loads(w["elec_rates_tomorrow_json"])
        self.assertEqual([s["valid_from"] for s in got],
                         ["2026-10-18T18:00:00Z", "2026-10-19T01:00:00Z"])

    def test_published_tomorrow_is_written_as_published(self):
        import json
        real = [_slot("2026-10-18T18:00:00Z", "2026-10-19T01:00:00Z", 24.0)]
        w = self._run("flux", real)
        self.assertEqual(json.loads(w["elec_rates_tomorrow_json"]), real)

    def test_a_tariff_whose_price_moves_is_never_carried(self):
        # Agile and Tracker change every day: yesterday's price is the wrong answer.
        for key in ("agile", "tracker"):
            self.assertNotIn("elec_rates_tomorrow_json", self._run(key, []), key)


class TestFluxRatesMayBeAsOldAsTheFixedBandsAre(unittest.TestCase):
    def test_the_rates_age_limit_is_thirty_six_hours(self):
        self.assertEqual(fs.MAX_RATES_AGE_S, 36 * 3600)


class TestHeldImportNoteDoesNotBlameTheConnectionOnAFixedTariff(unittest.TestCase):

    def _note(self, tariff_key):
        p = plugin.Plugin.__new__(plugin.Plugin)
        p.logger = MagicMock()
        p._state_lock = threading.RLock()
        p._send_pushover = MagicMock()
        p.store = {}
        p.latest_rates_data = {"tariff_info": {"tariff_key": tariff_key}}
        d = Decision(action=ACTION_SELF_CONSUMPTION, import_held=True, import_kwh=8.4,
                     reason="held", import_held_why="the cheap window times are not known")
        with patch.object(plugin, "log"), \
                patch.object(plugin, "_london_today", return_value=date(2026, 10, 18)):
            p._note_import_hold(d)
        return p._send_pushover.call_args.args[:2]

    def test_flux_note_points_at_the_log_not_the_octopus_connection(self):
        _title, body = self._note("flux")
        self.assertNotIn("Octopus connection", body)
        self.assertIn("plugin log", body)
        self.assertTrue(body.isascii() and len(body) <= 1024)

    def test_agile_note_still_names_the_connection(self):
        _title, body = self._note("agile")
        self.assertIn("Octopus connection", body)


if __name__ == "__main__":
    unittest.main()
