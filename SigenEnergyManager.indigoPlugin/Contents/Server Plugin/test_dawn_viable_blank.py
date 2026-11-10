#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    test_dawn_viable_blank.py
# Description: dawnViable reads blank, not its default True, while the plugin
#              cannot work out whether the battery will last until dawn (5.145.2).
#              CliveS, 10-11-2026: blank rather than keep the last answer.
# Author:      CliveS & Claude Fable 5.2
# Date:        10-11-2026
# Version:     1.0

import os
import re
import sys
import time
import types
import unittest
from collections import defaultdict
from datetime import datetime, timezone
from unittest.mock import MagicMock

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

import plugin                      # noqa: E402
import battery_manager as bm       # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))


def _published(decision):
    p = MagicMock()
    p.store = defaultdict(lambda: None, vpp_active=True, import_active=False,
                          export_active=False, import_scheduled_time=None)
    p.pluginPrefs = {}
    dev = MagicMock()
    dev.states = {}
    p._find_device.return_value = dev
    snap = MagicMock()
    snap.now = datetime(2026, 11, 1, 6, 0, tzinfo=timezone.utc)
    plugin.Plugin._update_manager_device(p, decision, snap)
    states = dev.updateStatesOnServer.call_args_list[0].args[0]
    return {s["key"]: s["value"] for s in states}


class TestTheVerdictNeedsAForecast(unittest.TestCase):

    def test_no_forecast_is_no_verdict(self):
        self.assertIsNone(plugin._dawn_viable_or_none(None))
        self.assertIsNone(plugin._dawn_viable_or_none(bm.Decision()))
        self.assertIsNone(plugin._dawn_viable_or_none(
            bm.Decision(action=bm.ACTION_VPP_EXPORT, dawn_viable=True)))
        # False with no forecast behind it is no more of an answer than True.
        self.assertIsNone(plugin._dawn_viable_or_none(bm.Decision(dawn_viable=False)))
        for bad in (float("nan"), float("inf"), "junk"):
            self.assertIsNone(plugin._dawn_viable_or_none(
                bm.Decision(soc_at_dawn_kwh=bad)), bad)

    def test_a_forecast_carries_its_verdict(self):
        self.assertIs(plugin._dawn_viable_or_none(
            bm.Decision(soc_at_dawn_kwh=18.1)), True)
        self.assertIs(plugin._dawn_viable_or_none(
            bm.Decision(soc_at_dawn_kwh=2.0, dawn_viable=False)), False)
        # A real forecast of an empty battery is still a forecast.
        self.assertIs(plugin._dawn_viable_or_none(
            bm.Decision(soc_at_dawn_kwh=0.0, dawn_viable=False)), False)

    def test_the_state_text(self):
        self.assertEqual(plugin._dawn_viable_state_value(bm.Decision()), "")
        self.assertEqual(plugin._dawn_viable_state_value(
            bm.Decision(soc_at_dawn_kwh=18.1)), "True")
        self.assertEqual(plugin._dawn_viable_state_value(
            bm.Decision(soc_at_dawn_kwh=2.0, dawn_viable=False)), "False")


class TestTheManagerDevice(unittest.TestCase):

    def test_a_driven_export_publishes_both_dawn_states_blank(self):
        for action in (bm.ACTION_VPP_EXPORT, bm.ACTION_SELF_CONSUMPTION):
            out = _published(bm.Decision(action=action, reason="driven"))
            self.assertEqual(out["dawnViable"], "", action)
            self.assertEqual(out["socAtDawn"], "", action)

    def test_a_real_vpp_decision_from_the_engine(self):
        import test_battery_manager as tbm
        snap = tbm._make_snapshot(soc_pct=80.0, vpp_active=True,
                                  export_enabled=True, now_hour=6)
        d = bm.BatteryManager().evaluate(snap)
        self.assertEqual(d.action, bm.ACTION_VPP_EXPORT)
        self.assertEqual(_published(d)["dawnViable"], "")

    def test_a_forecast_still_publishes_true_or_false(self):
        self.assertEqual(_published(bm.Decision(
            reason="ok", soc_at_dawn_kwh=18.1))["dawnViable"], "True")
        self.assertEqual(_published(bm.Decision(
            reason="short", soc_at_dawn_kwh=2.0, dawn_viable=False))["dawnViable"], "False")

    def test_the_state_is_text_so_blank_is_legal(self):
        with open(os.path.join(HERE, "Devices.xml"), encoding="utf-8") as f:
            xml = f.read()
        m = re.search(r'<State id="dawnViable">\s*<ValueType>(\w+)</ValueType>', xml)
        self.assertEqual(m.group(1), "String")


class TestTheStatusApiAndThePage(unittest.TestCase):

    def test_the_api_sends_null_and_never_defaults_to_true(self):
        with open(os.path.join(HERE, "plugin.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertTrue('"dawn_viable":   _dawn_viable_or_none(dawn_dec),' in src)
        self.assertFalse("dec.dawn_viable     if dec else True" in src)

    def test_the_page_draws_a_dash_for_null(self):
        with open(os.path.join(HERE, "dashboard.html"), encoding="utf-8") as f:
            page = f.read()
        # A bare truth test reads null as "No", in red.
        self.assertFalse("d.decision.dawn_viable ?" in page)
        self.assertTrue("d.decision.dawn_viable === true" in page)
        self.assertTrue("d.decision.dawn_viable === false" in page)


class TestTheStatusLogLines(unittest.TestCase):

    def test_blank_is_said_in_words(self):
        self.assertEqual(plugin._dawn_viable_words(""), "not worked out just now")
        self.assertEqual(plugin._dawn_viable_words(None), "not worked out just now")
        self.assertEqual(plugin._dawn_viable_words("  "), "not worked out just now")
        self.assertEqual(plugin._dawn_viable_words("True"), "yes")
        self.assertEqual(plugin._dawn_viable_words("False"), "no")
        self.assertEqual(plugin._dawn_viable_words(True), "yes")
        self.assertEqual(plugin._dawn_viable_words(False), "no")
        # An unrecognised value is shown, never read as yes or no.
        self.assertEqual(plugin._dawn_viable_words("?"), '"?"')

    def test_both_status_dumps_use_it(self):
        with open(os.path.join(HERE, "plugin.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertFalse("Viable={viable}" in src)
        self.assertFalse("Dawn viability:      {viable}" in src)
        self.assertEqual(src.count("_dawn_viable_words(viable)"), 2)


class TestAHeldManagerHasNoAnswer(unittest.TestCase):
    """Second reader, 10-11-2026. A paused manager, and one holding because the
    inverter has stopped answering, return before they evaluate. Both left the
    last verdict and the last forecast on the device for as long as the hold
    lasted, which is exactly "keep its last answer"."""

    def _held(self, store, read_at, states):
        p = MagicMock()
        p.store = dict(store)
        p.modbus_poll_s = 30
        p.latest_inverter_data = {"_read_at": read_at, "batterySoc": 50.0}
        dev = MagicMock()
        dev.states = dict(states)
        p._find_device.return_value = dev
        p._hold_dawn_forecast = lambda d: plugin.Plugin._hold_dawn_forecast(p, d)
        plugin.Plugin._evaluate_manager_impl(p)
        # Neither hold may reach the engine.
        p.manager.evaluate.assert_not_called()
        return p, dev

    def _blanked(self, dev):
        out = {}
        for call in dev.updateStatesOnServer.call_args_list:
            out.update({s["key"]: s["value"] for s in call.args[0]})
        return out

    def test_a_paused_manager_blanks_both_dawn_states(self):
        p, dev = self._held({"manager_paused": True}, time.time(),
                            {"managerStatus": "Paused", "dawnViable": "True",
                             "socAtDawn": "18.1"})
        self.assertEqual(self._blanked(dev), {"dawnViable": "", "socAtDawn": ""})
        self.assertTrue(p.store["dawn_forecast_held"])

    def test_a_silent_inverter_blanks_both_dawn_states(self):
        for status in ("Running", "Modbus offline \u2014 holding"):
            p, dev = self._held({}, time.time() - 3600,
                                {"managerStatus": status, "dawnViable": "False",
                                 "socAtDawn": "2.0"})
            self.assertEqual(self._blanked(dev),
                             {"dawnViable": "", "socAtDawn": ""}, status)
            self.assertTrue(p.store["dawn_forecast_held"], status)

    def test_already_blank_is_not_written_again(self):
        # One write when the hold starts, not one a minute into SQL history.
        p, dev = self._held({"manager_paused": True}, time.time(),
                            {"managerStatus": "Paused", "dawnViable": "",
                             "socAtDawn": ""})
        dev.updateStatesOnServer.assert_not_called()
        self.assertTrue(p.store["dawn_forecast_held"])

    def test_the_next_evaluation_lets_go_of_the_hold(self):
        with open(os.path.join(HERE, "plugin.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertIn('self.latest_decision = decision\n'
                      '        self.store["dawn_forecast_held"] = False', src)

    def test_the_status_api_forgets_the_last_decision_while_held(self):
        last = bm.Decision(soc_at_dawn_kwh=18.1)
        self.assertIs(plugin._dawn_decision(last, {}), last)
        self.assertIs(plugin._dawn_decision(last, {"dawn_forecast_held": False}), last)
        self.assertIsNone(plugin._dawn_decision(last, {"dawn_forecast_held": True}))
        self.assertIsNone(plugin._dawn_decision(None, {}))
        self.assertIsNone(plugin._dawn_decision(last, None))
        with open(os.path.join(HERE, "plugin.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertEqual(src.count("dawn_dec = _dawn_decision(dec, store)"), 1)
        self.assertIn('_dawn_viable_or_none(dawn_dec)', src)
        self.assertIn('_finite_or_none(dawn_dec.soc_at_dawn_kwh, 1)', src)

    def test_an_absent_state_is_not_shown_as_a_question_mark(self):
        with open(os.path.join(HERE, "plugin.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertNotIn('states.get("dawnViable", "?")', src)


if __name__ == "__main__":
    unittest.main()
