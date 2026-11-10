#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    test_notifications_2026_11_10.py
# Description: The faults the 10-Nov-2026 audit of every email and Pushover found
#              in this plugin's messages (5.145.1): the pound sign, the VPP
#              summary's wording, one valuation for a free hour, singular titles,
#              the first missed Axle poll as a Warning, and a dawn forecast that
#              is blank rather than 0 when there is none.
# Author:      CliveS & Claude Fable 5.2
# Date:        10-11-2026
# Version:     1.0

import os
import sys
import tempfile
import types
import unittest
from collections import defaultdict
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

import plugin                      # noqa: E402
import battery_manager as bm       # noqa: E402
import free_hour_credits as fhc    # noqa: E402
import happy_hour_booking as hb    # noqa: E402
import test_plugin as tp           # noqa: E402

LONDON = ZoneInfo("Europe/London")
POUND  = "\u00a3"


def _slots(hours, day=(2026, 11, 1)):
    out = []
    for h in hours:
        start = datetime(*day, h, 0, tzinfo=LONDON)
        out.append(hb.Slot(event_id=str(h), code=f"E{h}", start=start,
                           end=start + timedelta(hours=1)))
    return out


class TestThePoundSignSurvives(unittest.TestCase):
    """_ascii_plain swapped the pound sign for "GBP ", so every Happy Hour note
    read "GBP 1.74" while the rest of the estate's messages carry the sign."""

    def test_the_sign_is_kept(self):
        self.assertEqual(plugin._ascii_plain(f"about {POUND}2.88"), f"about {POUND}2.88")

    def test_everything_else_is_still_made_ascii(self):
        out = plugin._ascii_plain("a — b – c’s 12° … → é")
        self.assertEqual([c for c in out if ord(c) > 127], [])
        self.assertIn("12 degrees", out)
        self.assertIn("c's", out)
        self.assertIn("...", out)

    def test_a_happy_hour_note_reaches_pushover_and_the_log_with_the_sign(self):
        p = plugin.Plugin.__new__(plugin.Plugin)
        p.store = {"happy_hour_notes_sent": []}
        p._save_accumulators = MagicMock()
        p._send_pushover = MagicMock()
        with patch.object(plugin, "log") as logged:
            p._send_happy_hour_note("k", f"Title {POUND}1", f"Worth about {POUND}2.88.")
        title, body = p._send_pushover.call_args.args[:2]
        self.assertEqual(title, f"Title {POUND}1")
        self.assertEqual(body, f"Worth about {POUND}2.88.")
        self.assertIn(f"{POUND}2.88", logged.call_args.args[0])
        self.assertNotIn("GBP", title + body + logged.call_args.args[0])

    def test_no_message_source_writes_gbp_before_a_figure(self):
        """The sign is written as the escape, so the source stays ASCII there."""
        here = os.path.dirname(os.path.abspath(plugin.__file__))
        for name in ("happy_hour_booking.py", "free_hour_credits.py"):
            with open(os.path.join(here, name), encoding="utf-8") as fh:
                src = fh.read()
            self.assertIn('POUND = "\\u00a3"', src, name)
            self.assertNotRegex(src, r'f"[^"\n]*GBP \{', name)


class TestVppSummaryWording(unittest.TestCase):
    """The 2-Nov-2026 push: "GBP 4.00", "Solar: 0.00 kWh (n/a (no PV expected)),
    peaking at 0 W." and "(2 more detail lines left out to fit.)"."""

    H = tp.TestVppSummaryHeadlinesThePaidHour

    def _run(self, daytime=False, pv_w=0, forecast_w=None, external=False):
        h = self.H("test_the_body_fits_pushovers_cap")
        with tempfile.TemporaryDirectory() as td:
            # The real file path is about 160 characters and is part of the body, so
            # a short temp path understates the body by about 90 (10-Nov-2026).
            folder = os.path.join(td, "p" * max(1, 150 - len(td)))
            os.makedirs(folder, exist_ok=True)
            path = os.path.join(folder, "ev.jsonl")
            self.assertGreaterEqual(len(path), 155)
            h._write_log(path)
            if pv_w or external:
                import json
                with open(path, encoding="utf-8") as fh:
                    rows = [json.loads(line) for line in fh]
                for i, r in enumerate(rows):
                    if "pv_w" in r:
                        r["pv_w"] = pv_w
                        if external and i == 30:
                            r["driver"] = "external"
                with open(path, "w", encoding="utf-8") as fh:
                    for r in rows:
                        fh.write(json.dumps(r) + "\n")
            p = plugin.Plugin.__new__(plugin.Plugin)
            p.logger      = MagicMock()
            p.pluginPrefs = {"axleVppRatePerKwh": "1.00", "inverterMaxKw": "10.0"}
            p.store       = {"vpp_is_daytime": daytime}
            p._vpp_event_log_path      = MagicMock(return_value=path)
            p._record_vpp_ledger_event = MagicMock()
            p._send_pushover           = MagicMock()
            p._update_vpp_device       = MagicMock()
            p._forecast_pv_peak_w      = MagicMock(return_value=forecast_w)
            p._summarise_vpp_event({"start_time": h.START, "end_time": h.END,
                                    "duration_hrs": 1.0})
            title, body = p._send_pushover.call_args.args[:2]
        return title, body, body.split("-- Ask Claude --")[0]

    def test_the_money_carries_the_pound_sign(self):
        title, body, prose = self._run()
        self.assertIn(f"{POUND}4.00 at 100p a unit", prose)
        self.assertNotIn("GBP", title + body)

    def test_whole_figures_drop_their_noughts(self):
        title, _body, prose = self._run()
        self.assertEqual(title, "VPP window done - 4 kWh sold")
        self.assertNotIn("4.00 kWh", prose)
        self.assertIn("4.24 kWh altogether", prose)

    def test_a_dark_window_says_nothing_about_the_panels(self):
        _t, _b, prose = self._run(daytime=False)
        for bad in ("Solar", "n/a", "panels", "0.00 kWh", "peaking at 0 W"):
            self.assertNotIn(bad, prose)

    def test_a_daylight_window_with_no_sun_forecast_says_nothing_either(self):
        _t, _b, prose = self._run(daytime=True, forecast_w=20)
        self.assertNotIn("n/a", prose)
        self.assertNotIn("panels", prose)

    def test_a_sunny_window_gives_the_panels_a_sentence(self):
        _t, _b, prose = self._run(daytime=True, pv_w=2000, forecast_w=2500)
        self.assertIn("The panels made about 2 kWh in the window, peaking at 2000 W.",
                      prose)

    def test_the_prose_has_no_brackets_and_is_all_sentences(self):
        for kw in ({}, {"daytime": True, "pv_w": 2000, "forecast_w": 2500}):
            _t, _b, prose = self._run(**kw)
            for mark in "()[]|=":
                self.assertNotIn(mark, prose, kw)
            for line in [x for x in prose.splitlines() if x.strip()]:
                self.assertTrue(line.rstrip().endswith("."), line)

    def test_the_ask_claude_block_stays(self):
        _t, body, _p = self._run()
        self.assertIn("-- Ask Claude --", body)
        self.assertIn("_verify_ems_registers", body)

    def test_the_mode_line_names_who_drove_it_in_words(self):
        _t, _b, prose = self._run()
        self.assertIn('the inverter ran in "Remote EMS", driven by the plugin.', prose)

    def test_held_back_panels_are_never_shed(self):
        """The reworded sentence sat with the lines shed to fit, and with a
        real-length path it was shed: the push said nothing about the one fault
        the solar check exists for."""
        for forecast_w in (4000, None):
            _t, body, prose = self._run(daytime=True, forecast_w=forecast_w)
            self.assertIn("The panels made only 0 kWh in daylight and may have "
                          "been held back.", prose, forecast_w)
            self.assertNotIn("forecast", prose)
            self.assertLessEqual(len(body), plugin.PUSHOVER_BODY_CAP)
            self.assertIn("-- Ask Claude --", body)
            self.assertIn("_verify_ems_registers", body)

    def test_both_warnings_fit_without_the_last_resort_cut(self):
        _t, body, prose = self._run(daytime=True, forecast_w=4000, external=True)
        self.assertIn("held back", prose)
        self.assertIn("Warning: something outside the plugin", prose)
        self.assertIn("either side of the window", prose)
        self.assertLessEqual(len(body), plugin.PUSHOVER_BODY_CAP)
        self.assertNotIn("The rest is in the saved file.", body)
        self.assertIn("_verify_ems_registers", body)

    def test_the_last_resort_cut_keeps_only_the_essentials(self):
        body = plugin._fit_pushover_body(["Essential sentence. " * 30] * 3,
                                         ["A detail."], ["", "-- Ask Claude --", "x"])
        self.assertLessEqual(len(body), plugin.PUSHOVER_BODY_CAP)
        self.assertTrue(body.endswith("The rest is in the saved file."))
        self.assertNotIn("Ask Claude", body)
        self.assertNotIn("in the file below", body)

    def test_plain_number(self):
        self.assertEqual(plugin._plain_number(4.0), "4")
        self.assertEqual(plugin._plain_number(3.87), "3.87")
        self.assertEqual(plugin._plain_number(4.2), "4.2")
        self.assertEqual(plugin._plain_number(100.0, 1), "100")
        self.assertEqual(plugin._plain_number(0.0), "0")


class TestFreeHourResultHasOneValuation(unittest.TestCase):
    """1-Nov-2026: the 3pm push valued 11 kWh at the 15.5p night rate (1.74)
    and the ledger at the hour's own 25.6p (2.90). One event, one basis."""

    def test_the_note_prices_the_hour_as_the_ledger_does(self):
        title, body = hb.result_message(11.22, _slots((14,)), LONDON, 25.6271)
        self.assertIn(f"The house took 11.2 kWh from the grid in that time, and "
                      f"Octopus should credit all of it, about {POUND}2.88 at the "
                      f"25.6p a unit it would have cost at that time of day.", body)
        self.assertNotIn("overnight", body)
        self.assertNotIn("GBP", body)
        ledger_p = fhc.expected_pence({"hours": {"1": {"rate_p": 25.6271,
                                                       "meter_kwh": None}},
                                       "inverter_kwh": 11.22})
        self.assertEqual(f"{ledger_p / 100:.2f}", "2.88")

    def test_the_readers_own_sum_comes_out_at_the_money_shown(self):
        """"11 kWh ... about 2.90 at 25.6p": 11 x 25.6p is 2.82. The units beside
        money go to one decimal, and the shown figures multiplied land within
        2p of the money, which is worked unrounded as the ledger works it."""
        import re
        for kwh, rate in ((11.3, 25.6271), (11.22, 25.6271), (7.96, 35.8779),
                          (15.04, 25.0), (3.5, 14.6)):
            _t, body = hb.result_message(kwh, _slots((14,)), LONDON, rate)
            m = re.search(r"took ([\d.]+) kWh .* about \u00a3([\d.]+) at the "
                          r"([\d.]+)p a unit", body)
            self.assertIsNotNone(m, body)
            shown_kwh, shown_gbp, shown_p = (float(g) for g in m.groups())
            self.assertLessEqual(abs(shown_kwh * shown_p / 100 - shown_gbp), 0.02, body)
            self.assertEqual(m.group(2), f"{kwh * rate / 100:.2f}")
        _t, body = hb.result_message(11.3, _slots((14,)), LONDON, 25.6271)
        self.assertIn("The house took 11.3 kWh", body)
        self.assertIn(f"{POUND}2.90", body)

    def test_a_round_price_and_round_units_drop_the_nought(self):
        _t, body = hb.result_message(12.0, _slots((14,)), LONDON, 25.0, battery_kwh=3.0)
        self.assertIn("at the 25p a unit", body)
        self.assertIn("The house took 12 kWh", body)
        self.assertNotRegex(body, r"\d\.0(?!\d)")
        title, body = hb.result_message(12.0, _slots((14,)), LONDON, None,
                                        battery_kwh=3.0)
        self.assertEqual(title, "The battery stored only 3 kWh of the free 12 kWh")
        self.assertNotRegex(body, r"\d\.0(?!\d)")

    def test_without_a_price_the_money_is_left_out(self):
        _t, body = hb.result_message(11.22, _slots((14,)), LONDON, None)
        self.assertNotIn(POUND, body)
        self.assertIn("Octopus should credit all of it.", body)

    def test_one_hour_has_a_singular_title(self):
        title, _b = hb.result_message(11.22, _slots((14,)), LONDON, 25.6)
        self.assertEqual(title, "The free hour banked 11 kWh")
        self.assertEqual(hb.result_message(None, _slots((14,)), LONDON)[0],
                         "The free hour is over")
        self.assertEqual(hb.result_message(0.2, _slots((14,)), LONDON)[0],
                         "The free hour banked almost nothing")

    def test_two_hours_keep_the_plural(self):
        title, _b = hb.result_message(18.6, _slots((13, 14)), LONDON, 25.6)
        self.assertEqual(title, "The free hours banked 19 kWh")
        self.assertEqual(hb.result_message(None, _slots((13, 14)), LONDON)[0],
                         "The free hours are over")

    def _plugin(self, rate):
        p = plugin.Plugin.__new__(plugin.Plugin)
        p._import_rate_at = MagicMock(side_effect=rate)
        return p

    def test_the_plugin_asks_for_the_price_of_the_hour_itself(self):
        slots = _slots((14,))
        p = self._plugin(lambda when: 25.6271)
        self.assertEqual(p._happy_hour_credit_rate_p(slots), 25.63)
        p._import_rate_at.assert_called_once_with(slots[0].start)

    def test_an_unpriced_hour_or_two_prices_leave_the_money_out(self):
        self.assertIsNone(self._plugin(lambda when: None)
                          ._happy_hour_credit_rate_p(_slots((14,))))
        prices = iter((25.6, 35.8))
        self.assertIsNone(self._plugin(lambda when: next(prices))
                          ._happy_hour_credit_rate_p(_slots((15, 16))))
        self.assertIsNone(self._plugin(lambda when: 25.6)._happy_hour_credit_rate_p([]))

    def test_the_result_note_is_built_on_that_price(self):
        import inspect
        src = inspect.getsource(plugin.Plugin._happy_hour_result_note)
        self.assertIn("self._happy_hour_credit_rate_p(slots)", src)
        self.assertFalse(hasattr(plugin.Plugin, "_happy_hour_cheap_rate_p"))


class TestCreditNotesCountTheHours(unittest.TestCase):

    def _ledger(self, n_hours, credits=()):
        hours = {str(i): {"start": f"2026-11-01T{14 + i}:00:00+00:00",
                          "end": f"2026-11-01T{15 + i}:00:00+00:00",
                          "rate_p": 25.6271, "meter_kwh": 11.312}
                 for i in range(n_hours)}
        return {"version": 3, "claims": {"2026-11-01": {
            "date": "2026-11-01", "hours": hours, "credits": list(credits),
            "inverter_kwh": 11.22 * n_hours, "notified": {}, "closed_at": None}}}

    def test_a_late_credit_for_one_hour_is_singular(self):
        now = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
        notes = fhc.notifications(self._ledger(1), now)
        self.assertEqual(len(notes), 1)
        title, body = notes[0]
        self.assertIn("since the free hour on Sunday 1 November", body)
        self.assertNotIn("free hours", title + body)

    def test_two_hours_keep_the_plural(self):
        now = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
        _title, body = fhc.notifications(self._ledger(2), now)[0]
        self.assertIn("since the free hours on", body)


class TestAxlePollSeverity(unittest.TestCase):
    """9-Nov-2026: one poll failed while the router rebooted and was paged 54
    minutes later as an error. One miss is a Warning; three in a row an Error."""

    def _p(self):
        p = plugin.Plugin.__new__(plugin.Plugin)
        p.store = {"vpp_api_error": "", "vpp_api_fails": 0, "vpp_api_logged": 0.0}
        return p

    def _levels(self, p, errors):
        with patch.object(plugin, "vpp_log") as vl:
            for e in errors:
                p._record_vpp_api_status(e)
        return [(c.args[1] if len(c.args) > 1 else c.kwargs.get("level", "INFO"),
                 c.args[0]) for c in vl.call_args_list]

    def test_the_first_failure_is_a_warning(self):
        out = self._levels(self._p(), ["timed out"])
        self.assertEqual([lv for lv, _m in out], ["WARNING"])
        self.assertIn("Axle poll missed - timed out. This is failure 1 in a row",
                      out[0][1])

    def test_the_third_in_a_row_is_an_error_said_once(self):
        out = self._levels(self._p(), ["timed out"] * 6)
        self.assertEqual([lv for lv, _m in out], ["WARNING", "ERROR"])
        self.assertIn("That is 3 failures in a row", out[1][1])

    def test_a_long_outage_repeats_the_error_hourly(self):
        p = self._p()
        self._levels(p, ["timed out"] * 3)
        p.store["vpp_api_logged"] -= 3601
        out = self._levels(p, ["timed out"])
        self.assertEqual([lv for lv, _m in out], ["ERROR"])

    def test_a_recovery_starts_the_count_again(self):
        p = self._p()
        out = self._levels(p, ["timed out", "timed out", "", "timed out"])
        self.assertEqual([lv for lv, _m in out], ["WARNING", "INFO", "WARNING"])
        self.assertEqual(p.store["vpp_api_fails"], 1)

    def _recovery_event_flag(self, fails):
        p = self._p()
        with patch.object(plugin, "vpp_log") as vl:
            for e in ["timed out"] * fails + [""]:
                p._record_vpp_api_status(e)
        last = vl.call_args_list[-1]
        self.assertIn("recovered", last.args[0])
        return bool(last.kwargs.get("event"))

    def test_a_recovery_after_an_error_reaches_the_event_log(self):
        """The Error went to the event log, so the all-clear must follow it."""
        self.assertTrue(self._recovery_event_flag(3))
        self.assertTrue(self._recovery_event_flag(5))
        self.assertFalse(self._recovery_event_flag(1))
        self.assertFalse(self._recovery_event_flag(2))

    def test_a_dead_feed_is_never_silent(self):
        """The reason the check exists: a revoked token polled for six weeks."""
        out = self._levels(self._p(), ["HTTP 401"] * 3)
        self.assertEqual(out[-1][0], "ERROR")
        self.assertIn("HTTP 401", out[-1][1])


class TestDawnForecastIsBlankWhenThereIsNone(unittest.TestCase):
    """1-Nov-2026 6am, in an Axle export: socAtDawn was published as 0 and the
    morning brief said the battery "should be about 0% full" by 11am. It was 55%."""

    def test_a_decision_made_without_a_forecast_carries_none(self):
        self.assertIsNone(bm.Decision().soc_at_dawn_kwh)

    def test_a_vpp_export_decision_has_no_dawn_figure(self):
        import test_battery_manager as tbm
        snap = tbm._make_snapshot(soc_pct=80.0, vpp_active=True,
                                  export_enabled=True, now_hour=6)
        d = bm.BatteryManager().evaluate(snap)
        self.assertEqual(d.action, bm.ACTION_VPP_EXPORT)
        self.assertIsNone(d.soc_at_dawn_kwh)

    def test_the_state_value(self):
        self.assertEqual(plugin._dawn_state_value(None), "")
        self.assertEqual(plugin._dawn_state_value(29.364), "29.36")
        self.assertEqual(plugin._dawn_state_value(0.0), "0.0")   # a real forecast of empty
        self.assertEqual(plugin._dawn_state_value("junk"), "")
        for bad in (float("nan"), float("inf"), float("-inf"), "nan"):
            self.assertEqual(plugin._dawn_state_value(bad), "", bad)

    def test_the_status_api_sends_null_for_no_reading(self):
        self.assertIsNone(plugin._finite_or_none(None, 1))
        self.assertIsNone(plugin._finite_or_none(float("nan"), 1))
        self.assertIsNone(plugin._finite_or_none(float("inf"), 1))
        self.assertEqual(plugin._finite_or_none(29.364, 1), 29.4)
        self.assertEqual(plugin._finite_or_none(0.0, 1), 0.0)
        import inspect
        src = inspect.getsource(plugin)
        self.assertIn('"soc_at_dawn_kwh": (_finite_or_none(dec.soc_at_dawn_kwh, 1)', src)

    def _published(self, decision):
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

    def test_the_manager_device_shows_blank_not_nought(self):
        d = bm.Decision(action=bm.ACTION_VPP_EXPORT, reason="VPP event active")
        self.assertEqual(self._published(d)["socAtDawn"], "")

    def test_a_real_forecast_is_still_published(self):
        d = bm.Decision(reason="ok", soc_at_dawn_kwh=29.364)
        self.assertEqual(self._published(d)["socAtDawn"], "29.36")


class TestSavingSessionTokenWords(unittest.TestCase):
    """One win is one token and an hour costs two: the balance in the messages
    of 15-Sep to 9-Nov-2026 rose by one after each session and fell by two for
    each hour booked. The sentences were right; the counts are now in words."""

    def test_count_words(self):
        self.assertEqual(plugin._count_words(0), "no")
        self.assertEqual(plugin._count_words(1), "one")
        self.assertEqual(plugin._count_words(2), "two")
        self.assertEqual(plugin._count_words(40), "40")

    def test_one_token_short_of_an_hour_says_winning_unlocks_it(self):
        T = tp.TestCheckSavingSessions
        t = T("test_token_progress_says_step_when_the_pair_is_not_yet_complete")
        stub = t._Stub({"has_joined": True, "token_balance": 1,
                        "events": [t._event("1", t._future(), points=149)]},
                       store={"saving_sessions_notified": []})
        t._check(stub)
        self.assertIn("Octopus report one token, so winning this one should unlock "
                      "a free hour.", stub.sent[0][1])


if __name__ == "__main__":
    unittest.main()
