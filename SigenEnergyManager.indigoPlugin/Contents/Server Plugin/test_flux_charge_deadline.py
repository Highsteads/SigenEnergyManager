#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    test_flux_charge_deadline.py
# Description: The 4:58am control deadline must not move the 5am tariff boundary.
# Author:      CliveS & Codex GPT-6
# Date:        26-10-2026 18:15 GMT
# Version:     1.0

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from test_flux_strategy import _inputs, fs, LONDON
from test_flux_execution import Registers
from flux_execution import FluxExecutor, FluxTarget
from battery_manager import BatteryManager, ManagerSnapshot, TariffData, ACTION_START_IMPORT
import test_flux_supervisor as supervisor

_FluxCase = supervisor._FluxCase


class TestChargingDeadline(unittest.TestCase):
    def test_start_end_and_price_band_in_both_seasons_and_clock_change_days(self):
        for day in ((2026, 1, 15), (2026, 7, 15), (2026, 3, 29), (2026, 10, 25)):
            for hm, charging in (((1, 59), False), ((2, 0), True), ((4, 57), True),
                                 ((4, 58), False), ((4, 59), False), ((5, 0), False)):
                with self.subTest(day=day, hm=hm):
                    # 1:59am does not exist on the spring-forward day.
                    if day == (2026, 3, 29) and hm == (1, 59):
                        hm = (0, 59)
                    inputs = _inputs(hm, soc_pct=20.0, day=day)
                    decision = fs.plan(inputs)
                    self.assertNotEqual(decision.mode, fs.MODE_DEFER, decision.reason)
                    self.assertEqual(decision.mode in (fs.MODE_CHARGE, fs.MODE_HOLD), charging)
                    if charging:
                        end = inputs.now.astimezone(LONDON).replace(hour=4, minute=58)
                        self.assertLessEqual(decision.decision_until, end)
                    if hm == (4, 59):
                        self.assertTrue(fs.in_window(inputs.now, LONDON,
                                                     fs.FLUX_CHEAP_START, fs.FLUX_CHEAP_END))
                        self.assertEqual(fs.cheap_window_minimum_pct(inputs.site, (), inputs.now, LONDON), 0)

    def test_storm_does_not_extend_the_charging_window(self):
        self.assertNotEqual(fs.plan(_inputs((4, 58), soc_pct=20, storm_fill=True)).mode, fs.MODE_CHARGE)

    def test_tariff_data_retains_real_band_but_charge_decisions_use_earlier_end(self):
        flux = TariffData(tariff_key='flux', cheap_start='02:00', cheap_end='05:00')
        self.assertEqual(flux.cheap_end, '05:00')
        self.assertEqual(flux.charging_end, '04:58')
        for key, start, end in (('go','23:30','04:30'), ('igo','23:30','05:30'),
                                ('iflux','19:00','16:00'), ('agile',None,None)):
            self.assertEqual(TariffData(tariff_key=key, cheap_start=start, cheap_end=end).charging_end, end)

    def test_fallback_and_reserve_cannot_restart_in_final_two_minutes(self):
        manager = BatteryManager()
        balance = SimpleNamespace(import_needed=False, import_kwh_grid=0,
                                  battery_at_dawn_kwh=0, is_daytime=False)
        for month in (1, 7):
            for minute, allowed in ((57, True), (58, False), (59, False)):
                with self.subTest(month=month, minute=minute):
                    snapshot = ManagerSnapshot(
                        now=datetime(2026,month,15,4,minute,tzinfo=LONDON).astimezone(timezone.utc),
                        current_soc_pct=10, dawn_target_pct=20, cheap_window_min_pct=50,
                        tariff=TariffData(tariff_key='flux', cheap_start='02:00', cheap_end='05:00'))
                    result = manager._check_cheap_window_fallback(snapshot, balance)
                    self.assertEqual(result is not None, allowed)
                    if allowed: self.assertEqual(result.action, ACTION_START_IMPORT)
                    if not allowed:
                        self.assertIsNone(manager._check_resilience_buffer(snapshot, balance))
                        planned = manager._plan_tou_import(snapshot, balance, 80)
                        self.assertNotEqual(planned.action, ACTION_START_IMPORT)

    def test_executor_releases_and_reads_back_at_0458(self):
        for month in (1,7):
            with self.subTest(month=month), tempfile.TemporaryDirectory() as tmp:
                now = datetime(2026,month,15,4,57,55,tzinfo=LONDON)
                clock = [now]
                driver = Registers()
                executor = FluxExecutor(driver,Path(tmp)/'claim.json',baseline_charge_w=10000,
                    baseline_discharge_w=10000,baseline_discharge_cutoff_pct=20,
                    clock=lambda:clock[0])
                target = FluxTarget(now,now+timedelta(seconds=5),now,now+timedelta(seconds=5),
                                    3,6000,0,80.,20.)
                self.assertEqual(executor.step(target,now),'released')
                self.assertEqual(executor.step(target,now),'applied')
                clock[0] += timedelta(seconds=5)
                self.assertEqual(executor.step(target,clock[0]),'released')
                self.assertEqual(driver.values['mode'],2)
                self.assertEqual(driver.values['discharge'],10000)
                self.assertFalse(executor.owns_control)

    def test_executor_refuses_charge_or_hold_with_a_lease_past_0458(self):
        with tempfile.TemporaryDirectory() as tmp:
            now=datetime(2026,7,15,4,57,55,tzinfo=LONDON)
            executor=FluxExecutor(Registers(),Path(tmp)/'claim.json',baseline_charge_w=10000,
                baseline_discharge_w=10000,baseline_discharge_cutoff_pct=20,clock=lambda:now)
            for mode in (2,3):
                with self.subTest(mode=mode):
                    target=FluxTarget(now,now+timedelta(seconds=10),now,now+timedelta(seconds=10),
                                      mode,6000,0,80.,20.)
                    with self.assertRaisesRegex(ValueError,'outside Flux cheap window'):
                        executor._valid_target(target,now)


class TestPluginChargingDeadline(_FluxCase):
    _call_at = supervisor.TestDayRateImportStopsThePeakExport._call_at

    def test_fast_tick_stops_fallback_after_deadline_only_when_it_owns_the_inverter(self):
        for minute, owns, stops in ((57,False,False),(58,False,True),(59,True,False)):
            with self.subTest(minute=minute, owns=owns):
                p, when=self._at((4,minute),soc=70,cheap_window_import_active=True)
                p._flux_owns_control=MagicMock(return_value=owns)
                p._end_cheap_window_import=MagicMock()
                self._call_at(p,when,p._stop_overnight_charge_at_cutoff)
                self.assertEqual(p._end_cheap_window_import.call_count, int(stops))

    def test_storm_handover_does_not_preempt_0458_release(self):
        p,when=self._at((4,58),soc=90,storm_level='yellow')
        self.assertEqual(self._call_at(p,when,p._flux_other_owner),'')
        self.assertFalse(self._call_at(p,when,p._in_flux_cheap_window))

    def test_late_schedule_is_refused_without_changing_the_tariff(self):
        p,when=self._at((4,58),soc=20)
        tariff=TariffData(tariff_key='flux',cheap_start='02:00',cheap_end='05:00')
        p._build_tariff_data=lambda:tariff
        self.assertTrue(p._scheduled_import_missed_its_window(when))
        self.assertEqual(tariff.cheap_end,'05:00')
