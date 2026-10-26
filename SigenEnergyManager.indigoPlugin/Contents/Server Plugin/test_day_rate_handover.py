#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    test_day_rate_handover.py
# Description: A corrected charging mode must not block the afternoon Flux sale.
# Author:      CliveS & Codex GPT-6
# Date:        26-10-2026 17:55 GMT
# Version:     1.0

import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

from test_flux_supervisor import (
    _FluxCase, LONDON, plugin, fs, TestDayRateImportStopsThePeakExport as _DayRateCases,
)


class TestDayRateHandover(_FluxCase):
    _call_at = _DayRateCases._call_at

    def _verify_and_act(self, readings, when, write_ok=True):
        p, _ = self._at((5, 1), soc=99.3)
        p.modbus.read_ems_mode = MagicMock(side_effect=readings)
        p.modbus.set_remote_ems_mode = MagicMock(return_value=write_ok)
        p.store['remote_ems_mode_seen'] = (0x03, when.timestamp() - 10)
        p._note_day_rate_import = MagicMock()
        def run():
            p._verify_ems_registers()
            p._act_on_decision(SimpleNamespace(
                action=plugin.ACTION_SCHEDULE_IMPORT, target_soc_pct=40.0,
                scheduled_time=when, reason='test'))
        self._call_at(p, when, run)
        return p

    def test_corrected_charge_does_not_block_peak_in_gmt_or_bst(self):
        for month in (7, 10):
            with self.subTest(month=month):
                when = datetime(2026, month, 26, 5, 1, tzinfo=LONDON)
                p = self._verify_and_act([0x03, 0x02], when)
                p._note_day_rate_import.assert_not_called()
                self.assertEqual(p.store['remote_ems_mode_seen'][0], 0x02)
                p.modbus.set_remote_ems_mode.assert_called_once_with(0x02)

    def test_successful_command_without_mode_change_still_blocks(self):
        p = self._verify_and_act([0x03, 0x03], datetime(2026, 10, 26, 5, 1, tzinfo=LONDON))
        p._note_day_rate_import.assert_called_once()

    def test_failed_command_still_reads_real_mode(self):
        p = self._verify_and_act([0x03, 0x03], datetime(2026, 10, 26, 5, 1, tzinfo=LONDON), False)
        p._note_day_rate_import.assert_called_once()
        self.assertEqual(p.modbus.read_ems_mode.call_count, 2)

    def test_failed_read_cannot_reuse_old_charge_reading(self):
        for readings in ([None], [0x03, None]):
            with self.subTest(readings=readings):
                p = self._verify_and_act(readings, datetime(2026, 10, 26, 5, 1, tzinfo=LONDON))
                p._note_day_rate_import.assert_not_called()
                self.assertEqual(p.store['remote_ems_mode_seen'], (None, 0.0))

    def test_no_correction_only_needs_one_read(self):
        p = self._verify_and_act([0x02], datetime(2026, 10, 26, 5, 1, tzinfo=LONDON))
        p.modbus.read_ems_mode.assert_called_once()
        p.modbus.set_remote_ems_mode.assert_not_called()


class TestReviewDayRateBlock(_FluxCase):
    _call_at = _DayRateCases._call_at

    def _review(self, p, when, **props):
        return json.loads(self._call_at(p, when, lambda: p.actionReviewDayRateExportBlock(
            SimpleNamespace(props=props))))

    def _blocked(self, hhmm=(16, 30)):
        p, when = self._at(hhmm, soc=95.0)
        p.pluginPrefs['dayRateImportDate'] = when.date().isoformat()
        return p, when

    def test_preview_changes_neither_flag_nor_inverter(self):
        p, when = self._blocked()
        result = self._review(p, when)
        self.assertTrue(result['ok'])
        self.assertTrue(result['preview'])
        self.assertEqual(result['mode'], fs.MODE_EXPORT)
        self.assertEqual(p.pluginPrefs['dayRateImportDate'], when.date().isoformat())
        self.assertEqual(p.modbus.writes, [])

    def test_dated_confirmation_clears_only_block_without_forcing_export(self):
        p, when = self._blocked()
        before = dict(p.pluginPrefs)
        result = self._review(p, when, previewOnly=False, confirmed=True,
                              reviewDate=when.date().isoformat())
        self.assertTrue(result['cleared'])
        before['dayRateImportDate'] = ''
        self.assertEqual(p.pluginPrefs, before)
        self.assertEqual(p.modbus.writes, [])
        self._run(p, when)
        self.assertEqual(p.store['flux_decision'].mode, fs.MODE_EXPORT)

    def test_missing_false_or_old_confirmation_does_not_clear(self):
        for props in ({}, {'confirmed': 'false'}, {'confirmed': True, 'reviewDate': '2026-09-15'}):
            p, when = self._blocked()
            result = self._review(p, when, previewOnly=False, **props)
            self.assertFalse(result['ok'])
            self.assertTrue(p.pluginPrefs['dayRateImportDate'])
            self.assertEqual(p.modbus.writes, [])

    def test_active_import_unreadable_mode_and_disconnection_refuse_clear(self):
        for fault in ('import', 'mode', 'connection', 'observation'):
            with self.subTest(fault=fault):
                p, when = self._blocked()
                if fault == 'import': p.store['import_active'] = True
                if fault == 'mode': p.modbus.read_ems_mode = lambda: None
                if fault == 'connection': p.modbus.connected = False
                if fault == 'observation': p._flux_observation = lambda: None
                result = self._review(p, when, previewOnly=False, confirmed=True,
                                      reviewDate=when.date().isoformat())
                self.assertFalse(result['ok'])
                self.assertTrue(p.pluginPrefs['dayRateImportDate'])
                self.assertEqual(p.modbus.writes, [])

    def test_clear_keeps_7pm_stop(self):
        p, when = self._blocked((19, 0))
        result = self._review(p, when, previewOnly=False, confirmed=True,
                              reviewDate=when.date().isoformat())
        self.assertTrue(result['cleared'])
        self.assertNotEqual(result['mode'], fs.MODE_EXPORT)
        self.assertEqual(p.modbus.writes, [])

    def test_low_battery_is_not_forced_to_export(self):
        p, when = self._blocked()
        p.modbus.soc = 20.0
        p.latest_inverter_data["batterySoc"] = 20.0
        result = self._review(p, when, previewOnly=False, confirmed=True,
                              reviewDate=when.date().isoformat())
        self.assertTrue(result['cleared'])
        self.assertNotEqual(result['mode'], fs.MODE_EXPORT)
        self.assertEqual(p.modbus.writes, [])
