#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    test_happy_hour_plugin.py
# Description: The plugin's side of Weekend Happy Hour booking (v5.112.0): the
#              booking step on the Saving Sessions poll, its messages and their
#              dedupe, the morning reminder, the result note, what survives a
#              restart, and the Power Down join verdict that now knows the Flux peak.
# Author:      CliveS & Claude Opus 5.5
# Date:        22-09-2026
# Version:     1.0

import json
import os
import sys
import tempfile
import threading
import types
import unittest
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

import plugin              # noqa: E402
import flux_strategy as fs  # noqa: E402

LONDON   = ZoneInfo("Europe/London")
HH       = plugin.SAVING_SESSION_HAPPY_HOUR
THURSDAY = datetime(2026, 9, 24, 14, 0, tzinfo=LONDON).astimezone(timezone.utc)
SUNDAY   = datetime(2026, 9, 27, tzinfo=LONDON).date()


def _slot_events(day=SUNDAY, hours=(11, 12, 13, 14), joined=(), full=()):
    out = []
    for h in hours:
        start = fs.local_wall(LONDON, day, fs.time(h, 0))
        out.append({"id": 6000 + h, "code": f"EVENT_{h}", "direction": HH,
                    "joined": h in joined, "capacity": "FULL" if h in full else "AVAILABLE",
                    "reward_per_kwh_points": 0, "target_regions": [],
                    "start_at": start, "end_at": start + timedelta(hours=1)})
    return out


def _buckets(day, day_kwh, first=8, last=17):
    return {f"{day:%Y-%m-%d} {h:02d}:00:00":
            int(day_kwh * 1000.0 / (last - first)) if first <= h < last else 0
            for h in range(24)}


def _mk(pv_kwh=8.0, day=SUNDAY, prefs=None, book_result=None):
    p = plugin.Plugin.__new__(plugin.Plugin)
    p.logger      = MagicMock()
    p.debug       = False
    p.pluginPrefs = {"happyHourAutoBook": True, "happyHourImport": True,
                     "inverterMaxKw": "10", "batteryCapacityKwh": "35.04",
                     "fluxReservePct": "20", "happyHourTokensRequired": "2",
                     "batteryHealthCutoff": "1.0"}
    p.pluginPrefs.update(prefs or {})
    p.store = {
        "consumption_profile": [23.0 / 48.0] * 48,
        "happy_hour_notes_sent": [], "happy_hour_book_refused": [],
        "saving_sessions_not_our_region": [], "happy_hour_tokens": 7,
        "storm_level": "none", "flood_prev_target_soc": None,
        "saving_sessions_windows": [], "happy_hour_used": {},
    }
    p.latest_forecast_data = {"_hourly_p50_ahead": _buckets(day, pv_kwh),
                              "_hourly_p50_today": {}, "_hourly_p50_tomorrow": {},
                              "biasFactor": 1.0}
    p.latest_inverter_data = {"batterySoc": 50.0}
    p.octopus = MagicMock()
    p.octopus.saving_session_region_id = None
    p.octopus.book_happy_hour_event.return_value = (
        book_result or {"ok": True, "already": False, "permanent": False, "reason": "booked"})
    p._send_pushover          = MagicMock()
    p._save_accumulators      = MagicMock()
    p._power_cut_window_active = MagicMock(return_value=False)
    return p


def _booked_codes(p):
    return [c.args[0] for c in p.octopus.book_happy_hour_event.call_args_list]


class TestAutoBooking(unittest.TestCase):

    def test_a_dull_sunday_is_booked_two_hours_with_one_message(self):
        p = _mk(pv_kwh=8.0)
        data = {"token_balance": 7, "events": _slot_events()}
        p._auto_book_happy_hours(data, THURSDAY)
        self.assertEqual(_booked_codes(p), ["EVENT_13", "EVENT_14"])
        self.assertEqual([e["joined"] for e in data["events"]], [False, False, True, True])
        p._send_pushover.assert_called_once()
        title, body = p._send_pushover.call_args.args[:2]
        self.assertEqual(title, "Free electricity booked for Sunday")
        self.assertIn("from 1pm to 3pm", body)
        self.assertEqual(p.store["happy_hour_tokens"], 3)

    def test_a_booking_is_remembered_on_the_plugins_own_word(self):
        p = _mk(pv_kwh=8.0)
        p._auto_book_happy_hours({"token_balance": 7, "events": _slot_events()}, THURSDAY)
        self.assertEqual(p.store["happy_hour_booked_codes"], ["EVENT_13", "EVENT_14"])

    def test_a_feed_that_has_not_caught_up_cannot_cause_a_second_booking(self):
        """The same Sunday seen again with the booked slots still showing as free:
        the plugin's own record keeps it from booking two more."""
        p = _mk(pv_kwh=8.0)
        p._auto_book_happy_hours({"token_balance": 7, "events": _slot_events()}, THURSDAY)
        stale = {"token_balance": 3, "events": _slot_events()}      # joined=False again
        p._apply_local_happy_hour_bookings(stale)
        p._auto_book_happy_hours(stale, THURSDAY + timedelta(hours=1))
        self.assertEqual(p.octopus.book_happy_hour_event.call_count, 2)

    def test_the_booking_id_is_sent_so_the_reply_can_be_checked(self):
        p = _mk(pv_kwh=8.0)
        p._auto_book_happy_hours({"token_balance": 7, "events": _slot_events()}, THURSDAY)
        self.assertEqual(p.octopus.book_happy_hour_event.call_args_list[0].kwargs["event_id"],
                         "6013")

    def test_the_next_poll_books_nothing_more_and_says_nothing_more(self):
        p = _mk(pv_kwh=8.0)
        data = {"token_balance": 7, "events": _slot_events()}
        p._auto_book_happy_hours(data, THURSDAY)
        p._auto_book_happy_hours(data, THURSDAY + timedelta(hours=1))
        self.assertEqual(p.octopus.book_happy_hour_event.call_count, 2)
        self.assertEqual(p._send_pushover.call_count, 1)

    def test_a_bright_sunday_holds_with_one_message_only(self):
        p = _mk(pv_kwh=40.0)
        data = {"token_balance": 7, "events": _slot_events()}
        for hours in (0, 1, 2):
            p._auto_book_happy_hours(data, THURSDAY + timedelta(hours=hours))
        p.octopus.book_happy_hour_event.assert_not_called()
        self.assertEqual(p._send_pushover.call_count, 1)
        self.assertEqual(p._send_pushover.call_args.args[0],
                         "Keeping your free hours for a duller Sunday")

    def test_a_permanent_refusal_is_remembered_and_the_other_slot_still_booked(self):
        p = _mk(pv_kwh=8.0)
        refusal = {"ok": False, "already": False, "permanent": True,
                   "reason": "Octopus refused (OE-1309): full"}
        ok = {"ok": True, "already": False, "permanent": False, "reason": "booked"}
        p.octopus.book_happy_hour_event.side_effect = [refusal, ok]
        p._auto_book_happy_hours({"token_balance": 7, "events": _slot_events()}, THURSDAY)
        self.assertEqual(p.store["happy_hour_book_refused"], ["EVENT_13"])
        self.assertIn("one free hour", p._send_pushover.call_args.args[1].lower())

    def test_a_transient_failure_books_nothing_remembers_nothing_and_says_nothing(self):
        p = _mk(pv_kwh=8.0, book_result={"ok": False, "already": False,
                                         "permanent": False, "reason": "HTTP 502"})
        p._auto_book_happy_hours({"token_balance": 7, "events": _slot_events()}, THURSDAY)
        self.assertEqual(p.octopus.book_happy_hour_event.call_count, 1)
        self.assertEqual(p.store["happy_hour_book_refused"], [])
        p._send_pushover.assert_not_called()

    def test_nothing_is_booked_while_the_charging_box_is_off(self):
        p = _mk(pv_kwh=8.0, prefs={"happyHourImport": False})
        p._auto_book_happy_hours({"token_balance": 7, "events": _slot_events()}, THURSDAY)
        p.octopus.book_happy_hour_event.assert_not_called()
        p._send_pushover.assert_not_called()

    def test_nothing_is_booked_while_the_booking_box_is_off(self):
        p = _mk(pv_kwh=8.0, prefs={"happyHourAutoBook": False})
        p._auto_book_happy_hours({"token_balance": 7, "events": _slot_events()}, THURSDAY)
        p.octopus.book_happy_hour_event.assert_not_called()

    def test_the_last_sunday_is_booked_whatever_the_weather(self):
        last = datetime(2026, 10, 25, tzinfo=LONDON).date()
        now  = datetime(2026, 10, 22, 14, 0, tzinfo=LONDON).astimezone(timezone.utc)
        p = _mk(pv_kwh=40.0, day=last)
        p._auto_book_happy_hours({"token_balance": 7, "events": _slot_events(day=last)}, now)
        self.assertEqual(p.octopus.book_happy_hour_event.call_count, 2)
        self.assertIn("whatever the weather", p._send_pushover.call_args.args[1])

    def test_another_regions_slots_are_left_alone(self):
        p = _mk(pv_kwh=8.0)
        p.octopus.saving_session_region_id = 6
        events = _slot_events()
        for e in events:
            e["target_regions"] = [8, 9]
        p._auto_book_happy_hours({"token_balance": 7, "events": events}, THURSDAY)
        p.octopus.book_happy_hour_event.assert_not_called()

    def test_slots_after_the_scheme_are_never_booked(self):
        after = datetime(2026, 11, 1, tzinfo=LONDON).date()
        now   = datetime(2026, 10, 29, 14, 0, tzinfo=LONDON).astimezone(timezone.utc)
        p = _mk(pv_kwh=8.0, day=after)
        p._auto_book_happy_hours({"token_balance": 7, "events": _slot_events(day=after)}, now)
        p.octopus.book_happy_hour_event.assert_not_called()


class TestTheMorningAndTheResult(unittest.TestCase):

    def test_the_morning_reminder_goes_once_from_8am(self):
        p = _mk()
        data = {"events": _slot_events(joined=(13, 14))}
        seven = fs.local_wall(LONDON, SUNDAY, fs.time(7, 30))
        p._happy_hour_morning_note(data, seven)
        p._send_pushover.assert_not_called()
        eight = fs.local_wall(LONDON, SUNDAY, fs.time(8, 30))
        p._happy_hour_morning_note(data, eight)
        p._happy_hour_morning_note(data, eight + timedelta(hours=1))
        p._send_pushover.assert_called_once()
        self.assertEqual(p._send_pushover.call_args.args[0],
                         "Free electricity from 1pm to 3pm today")

    def test_no_reminder_when_nothing_is_booked(self):
        p = _mk()
        p._happy_hour_morning_note({"events": _slot_events()},
                                   fs.local_wall(LONDON, SUNDAY, fs.time(9, 0)))
        p._send_pushover.assert_not_called()

    def test_a_booking_made_on_the_day_suppresses_the_reminder(self):
        p = _mk(pv_kwh=8.0)
        p.latest_forecast_data["_hourly_p50_today"] = _buckets(SUNDAY, 8.0)
        nine = fs.local_wall(LONDON, SUNDAY, fs.time(9, 0))
        data = {"token_balance": 7, "events": _slot_events()}
        p._auto_book_happy_hours(data, nine)
        p._happy_hour_morning_note(data, nine)
        self.assertEqual(p._send_pushover.call_count, 1)

    def test_the_result_waits_for_the_last_booked_hour_of_the_day(self):
        p = _mk()
        one = fs.local_wall(LONDON, SUNDAY, fs.time(13, 0))
        p._note_happy_hour_span({"start": one.isoformat(),
                                 "end": (one + timedelta(hours=1)).isoformat()})
        later = one + timedelta(hours=2)
        p.store["saving_sessions_windows"] = [{"direction": HH,
                                               "start": later.isoformat(),
                                               "end": (later + timedelta(hours=1)).isoformat()}]
        p._happy_hour_result_note(9.5, now_utc=one + timedelta(hours=1, minutes=1))
        p._send_pushover.assert_not_called()
        self.assertEqual(p.store["happy_hour_used"]["kwh"], 9.5)

        p.store["saving_sessions_windows"] = []
        p._note_happy_hour_span({"start": later.isoformat(),
                                 "end": (later + timedelta(hours=1)).isoformat()})
        p._happy_hour_result_note(9.0, now_utc=later + timedelta(hours=1, minutes=1))
        p._send_pushover.assert_called_once()
        title, body = p._send_pushover.call_args.args[:2]
        self.assertEqual(title, "The free hours banked 18 kWh")
        self.assertIn("from 1pm to 2pm and from 3pm to 4pm", body)

    def test_the_result_goes_once_a_day(self):
        p = _mk()
        one = fs.local_wall(LONDON, SUNDAY, fs.time(13, 0))
        p._note_happy_hour_span({"start": one.isoformat(),
                                 "end": (one + timedelta(hours=1)).isoformat()})
        p._happy_hour_result_note(9.5, now_utc=one + timedelta(hours=1, minutes=1))
        p._happy_hour_result_note(0.0, now_utc=one + timedelta(hours=1, minutes=2))
        p._send_pushover.assert_called_once()


class TestItSurvivesARestart(unittest.TestCase):

    def test_the_booking_state_round_trips(self):
        p = plugin.Plugin.__new__(plugin.Plugin)
        p.logger      = MagicMock()
        p.pluginPrefs = {}
        p.store = {
            "pv_daily_kwh": 1.0, "grid_import_daily_kwh": 0.0,
            "grid_export_daily_kwh": 2.0, "home_daily_kwh": 3.0,
            "peak_soc": 90.0, "min_soc": 40.0, "today_date": "2026-09-27",
            "pv_lifetime_start_kwh": 100.0, "import_lifetime_start_kwh": 10.0,
            "export_lifetime_start_kwh": 20.0,
            "happy_hour_notes_sent":   ["booked:2026-09-27:EVENT_13,EVENT_14"],
            "happy_hour_book_refused": ["EVENT_12"],
            "happy_hour_booked_codes": ["EVENT_13", "EVENT_14"],
            "happy_hour_used": {"day": "2026-09-27", "spans": [["a", "b"]], "kwh": 9.5},
        }
        p._state_lock = threading.RLock()
        p._save_home_profile = MagicMock()
        d = tempfile.mkdtemp()
        path = os.path.join(d, "accumulators.json")
        p._save_accumulators_locked(path)
        with open(path, encoding="utf-8") as fh:
            saved = json.load(fh)
        for k in ("happy_hour_notes_sent", "happy_hour_book_refused",
                  "happy_hour_booked_codes", "happy_hour_used"):
            self.assertEqual(saved[k], p.store[k], k)

        q = plugin.Plugin.__new__(plugin.Plugin)
        q.logger = MagicMock()
        q.store  = {"happy_hour_notes_sent": [], "happy_hour_book_refused": [],
                    "happy_hour_booked_codes": [], "happy_hour_used": {}}
        q._get_data_dir = lambda: d
        q._load_accumulators()
        for k in ("happy_hour_notes_sent", "happy_hour_book_refused",
                  "happy_hour_booked_codes", "happy_hour_used"):
            self.assertEqual(q.store[k], p.store[k], k)


class TestTheJoinVerdictKnowsTheFluxPeak(unittest.TestCase):
    """Inside 4pm-7pm on Flux a Power Down costs nothing, so it is always joined —
    even after the Happy Hour scheme ends, when the token check alone would say no."""

    def _p(self, armed=True):
        p = plugin.Plugin.__new__(plugin.Plugin)
        p.logger = MagicMock()
        p.pluginPrefs = {"fluxEnabled": armed, "fluxCommissioned": armed,
                         "happyHourTokensRequired": "2"}
        p.store = {"happy_hour_tokens": 40}
        return p

    def _event(self, day, h0, h1):
        return {"start_at": fs.local_wall(LONDON, day, fs.time(h0, 0)),
                "end_at":   fs.local_wall(LONDON, day, fs.time(h1, 0))}

    def test_inside_the_peak_after_the_scheme_is_joined(self):
        day = datetime(2026, 11, 10, tzinfo=LONDON).date()
        ok, why = self._p()._session_join_verdict(self._event(day, 18, 19), day, day)
        self.assertTrue(ok)
        self.assertEqual(why, "")

    def test_outside_the_peak_after_the_scheme_is_declined_as_before(self):
        day = datetime(2026, 11, 10, tzinfo=LONDON).date()
        ok, why = self._p()._session_join_verdict(self._event(day, 19, 20), day, day)
        self.assertFalse(ok)
        self.assertIn("ended", why)

    def test_a_session_that_runs_past_seven_is_not_inside_the_peak(self):
        day = datetime(2026, 11, 10, tzinfo=LONDON).date()
        ok, _ = self._p()._session_join_verdict(self._event(day, 18, 20), day, day)
        self.assertFalse(ok)

    def test_without_flux_armed_the_token_check_decides(self):
        day = datetime(2026, 11, 10, tzinfo=LONDON).date()
        ok, _ = self._p(armed=False)._session_join_verdict(self._event(day, 18, 19), day, day)
        self.assertFalse(ok)

    def test_the_verdict_is_what_the_auto_join_asks(self):
        """The wiring, not only the helper: a mutation replacing the call with the
        bare token verdict must be caught here."""
        import inspect
        src = inspect.getsource(plugin.Plugin._auto_join_saving_sessions)
        self.assertIn("self._session_join_verdict(", src)
        self.assertNotIn("self._happy_hour_token_verdict(", src)
        src = inspect.getsource(plugin.Plugin._check_saving_sessions)
        self.assertIn("self._session_join_verdict(", src)
        self.assertNotIn("self._happy_hour_token_verdict(", src)


class TestTheWiring(unittest.TestCase):

    def test_the_poll_books_after_joining_and_before_the_window_cache(self):
        """Order is correctness: a booking flips `joined`, and the window cache
        built after it admits booked slots only."""
        import inspect
        src = inspect.getsource(plugin.Plugin._check_saving_sessions)
        join  = src.index("self._auto_join_saving_sessions(")
        book  = src.index("self._auto_book_happy_hours(")
        cache = src.index('self.store["saving_sessions_windows"] = [')
        self.assertLess(join, book)
        self.assertLess(book, cache)

    def _poll_with_slots(self, auto_book):
        """One real poll over four slots three days from NOW, whatever today is,
        so this never starts failing because the calendar moved on."""
        p = _mk(prefs={"happyHourAutoBook": auto_book, "happyHourImport": True})
        p._auto_book_happy_hours = MagicMock()        # the booking is tested above
        day = (datetime.now(timezone.utc) + timedelta(days=3)).astimezone(LONDON).date()
        p.octopus.get_saving_sessions.return_value = {
            "has_joined": True, "token_balance": 7, "events": _slot_events(day=day)}
        p.store.update({"saving_sessions_notified": [], "saving_sessions_join_refused": []})
        plugin.Plugin._check_saving_sessions(p)
        return p

    def test_the_four_slot_announcements_are_replaced_while_the_plugin_books(self):
        p = self._poll_with_slots(auto_book=True)
        titles = [c.args[0] for c in p._send_pushover.call_args_list]
        self.assertEqual([t for t in titles if t.startswith("Octopus Happy Hour")], [])
        self.assertEqual(len(p.store["saving_sessions_notified"]), 4)   # never re-announced

    def test_without_automatic_booking_the_slots_are_still_announced(self):
        p = self._poll_with_slots(auto_book=False)
        titles = [c.args[0] for c in p._send_pushover.call_args_list]
        self.assertEqual(len([t for t in titles if t.startswith("Octopus Happy Hour")]), 4)

    def test_a_slot_the_plugin_booked_reaches_the_window_cache_whatever_the_feed_says(self):
        p = _mk(prefs={"happyHourAutoBook": False})
        day = (datetime.now(timezone.utc) + timedelta(days=1)).astimezone(LONDON).date()
        events = _slot_events(day=day)                                # all joined=False
        p.store["happy_hour_booked_codes"] = ["EVENT_13"]
        p.octopus.get_saving_sessions.return_value = {
            "has_joined": True, "token_balance": 5, "events": events}
        p.store.update({"saving_sessions_notified": [e["id"] for e in events],
                        "saving_sessions_join_refused": []})
        plugin.Plugin._check_saving_sessions(p)
        cached = [w["id"] for w in p.store["saving_sessions_windows"]]
        self.assertEqual(cached, [6013])

    def test_a_booking_fault_cannot_stop_the_poll(self):
        p = _mk()
        p._auto_book_happy_hours = MagicMock(side_effect=RuntimeError("boom"))
        p.octopus.get_saving_sessions.return_value = {
            "has_joined": True, "token_balance": 7, "events": []}
        p.store.update({"saving_sessions_notified": [], "saving_sessions_join_refused": []})
        plugin.Plugin._check_saving_sessions(p)
        self.assertEqual(p.store["saving_sessions_windows"], [])
        p.logger.exception.assert_called()



class TestTheStartupLineSaysWhatIsArmed(unittest.TestCase):
    """Booking spends tokens on the owner's behalf, so, like the opt-in, it says
    at every start and every save that it is armed."""

    def _lines(self, prefs):
        p = plugin.Plugin.__new__(plugin.Plugin)
        p.logger = MagicMock()
        p.pluginPrefs = prefs
        with patch.object(plugin, "log") as lg:
            p._log_saving_session_auto_join_setting()
        return [(c.args[0], c.kwargs.get("level", "INFO")) for c in lg.call_args_list]

    def test_booking_on_announces_itself(self):
        lines = self._lines({"happyHourAutoBook": True, "happyHourImport": True})
        self.assertEqual(len(lines), 1)
        self.assertIn("Automatic booking is ON", lines[0][0])
        self.assertEqual(lines[0][1], "INFO")

    def test_booking_without_charging_is_a_warning(self):
        lines = self._lines({"happyHourAutoBook": True, "happyHourImport": False})
        self.assertEqual(lines[0][1], "WARNING")
        self.assertIn("nothing will be booked", lines[0][0])

    def test_the_opt_in_line_no_longer_says_happy_hours_are_never_automatic(self):
        lines = self._lines({"savingSessionAutoJoin": True})
        self.assertEqual(len(lines), 1)
        self.assertNotIn("Happy Hour", lines[0][0])

    def test_nothing_ticked_says_nothing(self):
        self.assertEqual(self._lines({}), [])


class TestTheFluxJournalLineIsOnlyAWarningWhenItShouldBe(unittest.TestCase):
    """5.112.2. An armed Flux plugin leaves a journal on disk between windows, and
    every restart logged a WARNING about it — 15 in three days — although the
    journal itself said nothing was held. Only a live or unreadable journal is a
    warning now. What the plugin DOES at startup is unchanged."""

    def _p(self, content):
        d = tempfile.mkdtemp()
        p = plugin.Plugin.__new__(plugin.Plugin)
        p.data_dir = d
        if content is not None:
            with open(os.path.join(d, "flux_claim.json"), "w", encoding="utf-8") as fh:
                fh.write(content)
        return p

    CLEAR = '{"version": 1, "owns": false, "pending": false, "supervisor_owned": false, "target_expiry": null}'

    def test_a_clear_journal_reads_clear(self):
        self.assertTrue(self._p(self.CLEAR)._flux_journal_says_clear())

    def test_a_live_claim_does_not(self):
        for field in ("owns", "pending", "supervisor_owned"):
            doc = json.loads(self.CLEAR)
            doc[field] = True
            self.assertFalse(self._p(json.dumps(doc))._flux_journal_says_clear(), field)

    def test_missing_unreadable_or_unknown_journals_keep_the_warning(self):
        self.assertFalse(self._p(None)._flux_journal_says_clear())
        self.assertFalse(self._p("{not json")._flux_journal_says_clear())
        doc = json.loads(self.CLEAR)
        doc["version"] = 2
        self.assertFalse(self._p(json.dumps(doc))._flux_journal_says_clear())
        doc = json.loads(self.CLEAR)
        doc["owns"] = 0                      # falsy is not False
        self.assertFalse(self._p(json.dumps(doc))._flux_journal_says_clear())

    def test_the_startup_path_asks_before_choosing_the_level(self):
        import inspect
        src = inspect.getsource(plugin.Plugin._init_modules)
        branch = src[src.index("if self._flux_recovery_pending():"):]
        self.assertLess(branch.index("self._flux_journal_says_clear()"),
                        branch.index('level="WARNING"'))

if __name__ == "__main__":
    unittest.main()
