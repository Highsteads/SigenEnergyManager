#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    test_flux_carry_forward.py
# Description: A Flux side Octopus has not published yet uses the last published day's
#              prices (flux_strategy 2.9.1, SigenEnergyManager 5.129.1). Built on what
#              Octopus actually published for region F, read 1-Oct-2026 13:57 BST: October
#              import prices out to 3 October, export stopping at midnight on 30 September.
# Author:      CliveS & Claude Opus 5.5
# Date:        01-10-2026
# Version:     1.0

import sys
import types
import unittest
from datetime import datetime, timezone
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

import plugin                  # noqa: E402
import flux_strategy as fs     # noqa: E402

LONDON = ZoneInfo("Europe/London")
UTC    = timezone.utc


def _local(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=LONDON).astimezone(UTC)


def _span(a, b, p):
    return fs.RateSpan(start=datetime.fromisoformat(a.replace("Z", "+00:00")),
                       end=datetime.fromisoformat(b.replace("Z", "+00:00")), p=p)


IMPORT = [
    _span("2026-09-29T18:00:00Z", "2026-09-30T01:00:00Z", 24.35433),
    _span("2026-09-30T01:00:00Z", "2026-09-30T04:00:00Z", 14.618415),
    _span("2026-09-30T04:00:00Z", "2026-09-30T15:00:00Z", 24.35433),
    _span("2026-09-30T15:00:00Z", "2026-09-30T18:00:00Z", 34.099905),
    _span("2026-09-30T18:00:00Z", "2026-10-01T01:00:00Z", 23.1946),
    _span("2026-10-01T01:00:00Z", "2026-10-01T04:00:00Z", 13.9223),
    _span("2026-10-01T04:00:00Z", "2026-10-01T15:00:00Z", 23.1946),
    _span("2026-10-01T15:00:00Z", "2026-10-01T18:00:00Z", 32.4761),
    _span("2026-10-01T18:00:00Z", "2026-10-02T01:00:00Z", 23.1946),
    _span("2026-10-02T01:00:00Z", "2026-10-02T04:00:00Z", 13.9223),
    _span("2026-10-02T04:00:00Z", "2026-10-02T15:00:00Z", 23.1946),
    _span("2026-10-02T15:00:00Z", "2026-10-02T18:00:00Z", 32.4761),
    _span("2026-10-02T18:00:00Z", "2026-10-03T01:00:00Z", 23.1946),
]
EXPORT = [
    _span("2026-09-29T18:00:00Z", "2026-09-30T01:00:00Z", 9.7084),
    _span("2026-09-30T01:00:00Z", "2026-09-30T04:00:00Z", 4.2064),
    _span("2026-09-30T04:00:00Z", "2026-09-30T15:00:00Z", 9.7084),
    _span("2026-09-30T15:00:00Z", "2026-09-30T18:00:00Z", 27.6905),
    _span("2026-09-30T18:00:00Z", "2026-09-30T23:00:00Z", 9.7084),
]
UNTIL = max(s.end for s in IMPORT)


class TestTheLastPublishedDayIsCarriedForward(unittest.TestCase):

    def test_without_it_1_october_is_refused(self):
        self.assertIsNone(fs.derive_bands(IMPORT, EXPORT, LONDON, _local(2026, 10, 1, 14)))

    def test_with_it_1_october_trades_on_october_import_and_september_export(self):
        exp, stopped = fs.carry_forward_spans(EXPORT, LONDON, UNTIL)
        self.assertEqual(stopped, _local(2026, 10, 1, 0, 0))
        bands = fs.derive_bands(IMPORT, exp, LONDON, _local(2026, 10, 1, 14))
        self.assertIsNotNone(bands)
        self.assertAlmostEqual(bands.export_peak_p, 27.6905)
        self.assertAlmostEqual(bands.export_day_p, 9.7084)
        self.assertAlmostEqual(bands.export_cheap_p, 4.2064)
        self.assertAlmostEqual(bands.import_cheap_p, 13.9223)
        self.assertAlmostEqual(bands.import_peak_p, 32.4761)
        self.assertGreaterEqual(bands.covers_until, _local(2026, 10, 2, 5))

    def test_the_2am_charge_tonight_has_its_bands_too(self):
        exp, _ = fs.carry_forward_spans(EXPORT, LONDON, UNTIL)
        self.assertIsNotNone(fs.derive_bands(IMPORT, exp, LONDON, _local(2026, 10, 2, 2, 30)))

    def test_the_bands_stay_on_the_wall_clock(self):
        exp, _ = fs.carry_forward_spans(EXPORT, LONDON, UNTIL)
        peak = [s for s in exp if round(s.p, 4) == 27.6905
                and s.start.astimezone(LONDON).date().isoformat() == "2026-10-01"]
        self.assertEqual(len(peak), 1)
        self.assertEqual(peak[0].start.astimezone(LONDON).strftime("%H:%M"), "16:00")
        self.assertEqual(peak[0].end.astimezone(LONDON).strftime("%H:%M"), "19:00")

    def test_across_the_clock_change_the_peak_is_still_4pm(self):
        # The last Sunday of October: 25-Oct-2026 the clocks go back.
        day = [_span("2026-10-23T23:00:00Z", "2026-10-24T01:00:00Z", 9.7),
               _span("2026-10-24T01:00:00Z", "2026-10-24T04:00:00Z", 4.2),
               _span("2026-10-24T04:00:00Z", "2026-10-24T15:00:00Z", 9.7),
               _span("2026-10-24T15:00:00Z", "2026-10-24T18:00:00Z", 27.7),
               _span("2026-10-24T18:00:00Z", "2026-10-24T23:00:00Z", 9.7)]
        out, _ = fs.carry_forward_spans(day, LONDON, _local(2026, 10, 27, 0))
        peaks = sorted(s.start.astimezone(LONDON).strftime("%d %H:%M")
                       for s in out if s.p == 27.7)
        self.assertEqual(peaks, ["24 16:00", "25 16:00", "26 16:00"])

    def test_nothing_is_carried_when_the_schedule_already_reaches(self):
        out, stopped = fs.carry_forward_spans(IMPORT, LONDON, UNTIL)
        self.assertIsNone(stopped)
        self.assertEqual(len(out), len(IMPORT))

    def test_nothing_is_carried_from_less_than_a_day(self):
        out, stopped = fs.carry_forward_spans(EXPORT[-2:], LONDON, UNTIL)
        self.assertIsNone(stopped)

    def test_a_real_publication_replaces_the_carried_prices(self):
        # Carrying is done at READ time from the stored schedule, so once Octopus
        # publishes, the real spans reach `until` and nothing is carried.
        published = EXPORT + [_span("2026-09-30T23:00:00Z", "2026-10-03T01:00:00Z", 9.5)]
        out, stopped = fs.carry_forward_spans(published, LONDON, UNTIL)
        self.assertIsNone(stopped)


def _slots(spans):
    return [{"valid_from": x.start.isoformat(), "valid_to": x.end.isoformat(),
             "value_inc_vat": x.p} for x in spans]


class TestThePluginCarriesTheShortSideAndSaysSo(unittest.TestCase):

    def _mk(self, imp, exp):
        p = plugin.Plugin.__new__(plugin.Plugin)
        p.logger = MagicMock()
        p.store = {"flux_import_slots": _slots(imp), "flux_export_slots": _slots(exp)}
        return p

    def test_the_export_side_is_carried_and_logged_once(self):
        p = self._mk(IMPORT, EXPORT)
        logged = []
        with patch.object(plugin, "log", side_effect=lambda m, level="INFO":
                          logged.append((level, m))):
            exp = p._flux_planning_spans("flux_export_slots")
            p._flux_planning_spans("flux_export_slots")
            imp = p._flux_planning_spans("flux_import_slots")
        self.assertGreaterEqual(max(x.end for x in exp), UNTIL)
        self.assertEqual(len(imp), len(IMPORT))
        self.assertEqual(len(logged), 1)
        level, msg = logged[0]
        self.assertEqual(level, "WARNING")
        self.assertIn("export prices beyond 00:00 on 1 October", msg)
        self.assertIn("27.69p", msg)

    def test_nothing_is_carried_when_the_other_side_is_empty(self):
        p = self._mk([], EXPORT)
        with patch.object(plugin, "log") as lg:
            exp = p._flux_planning_spans("flux_export_slots")
        self.assertEqual(len(exp), len(EXPORT))
        lg.assert_not_called()

    def test_the_money_records_still_read_only_what_was_published(self):
        p = self._mk(IMPORT, EXPORT)
        with patch.object(plugin, "log"):
            self.assertEqual(len(p._flux_rate_spans("flux_export_slots")), len(EXPORT))

    def test_the_live_bands_derive_from_the_store(self):
        p = self._mk(IMPORT, EXPORT)
        with patch.object(plugin, "log"):
            bands = fs.derive_bands(p._flux_planning_spans("flux_import_slots"),
                                    p._flux_planning_spans("flux_export_slots"),
                                    LONDON, _local(2026, 10, 1, 15, 55))
        self.assertIsNotNone(bands)


if __name__ == "__main__":
    unittest.main()
