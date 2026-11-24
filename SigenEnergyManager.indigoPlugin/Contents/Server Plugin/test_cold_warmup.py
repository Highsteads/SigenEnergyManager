#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    test_cold_warmup.py
# Description: The cold-night warm-up: a small capped charge before the Flux cheap
#              window, so the battery can take the full rate from 02:00.
# Author:      CliveS & Claude Fable 5.2
# Date:        24-11-2026 23:10 GMT
# Version:     1.0
#
# Measured 24-Nov-2026: with the coldest pack at 4.4 C the battery took 0.64 kW for
# the first 13 minutes of the cheap window however much was asked for, and the charge
# ended at 86.9% where 97 to 99% is usual.

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

from test_flux_supervisor import (
    _FluxCase, plugin, fs, TestDayRateImportStopsThePeakExport as _DayRateCases,
)

WARMUP_WRITE = ("force_charge", plugin.COLD_WARMUP_W, 100.0)


class _WarmupCase(_FluxCase):
    _call_at = _DayRateCases._call_at

    def _cold(self, hhmm, temp=4.4, soc=20.0, **store):
        p, when = self._at(hhmm, soc=soc, **store)
        if temp is not None:
            p.latest_inverter_data["batteryMinTempC"] = temp
        return p, when

    def _step(self, p, when):
        self._call_at(p, when, p._cold_warmup_step)

    def _started(self, p):
        return WARMUP_WRITE in p.modbus.writes


class TestWhenTheWarmupStarts(_WarmupCase):

    def test_a_very_cold_battery_starts_half_an_hour_before_two(self):
        p, when = self._cold((1, 31))
        self._step(p, when)
        self.assertTrue(self._started(p))
        self.assertTrue(p.store["cold_warmup_active"])

    def test_not_before_its_lead_time(self):
        p, when = self._cold((1, 25))
        self._step(p, when)
        self.assertEqual(p.modbus.writes, [])
        self.assertFalse(p.store.get("cold_warmup_active"))

    def test_a_cool_battery_gets_the_shorter_lead(self):
        p, when = self._cold((1, 35), temp=5.5)
        self._step(p, when)
        self.assertEqual(p.modbus.writes, [], "20 minutes, not 30, between 5 and 6 C")
        p, when = self._cold((1, 41), temp=5.5)
        self._step(p, when)
        self.assertTrue(self._started(p))

    def test_a_battery_at_six_degrees_or_more_is_left_alone(self):
        for temp in (6.0, 6.4, 12.0):
            p, when = self._cold((1, 45), temp=temp)
            self._step(p, when)
            self.assertEqual(p.modbus.writes, [], temp)

    def test_no_temperature_reading_is_not_a_cold_battery(self):
        for temp in (None, "", "n/a"):
            p, when = self._cold((1, 45), temp=None)
            if temp is not None:
                p.latest_inverter_data["batteryMinTempC"] = temp
            self._step(p, when)
            self.assertEqual(p.modbus.writes, [], repr(temp))

    def test_a_nearly_full_battery_is_left_alone(self):
        p, when = self._cold((1, 45), soc=92.0)
        self._step(p, when)
        self.assertEqual(p.modbus.writes, [])

    def test_not_at_other_times_of_day(self):
        for hhmm in ((0, 30), (2, 30), (5, 30), (15, 45), (23, 0)):
            p, when = self._cold(hhmm)
            self._step(p, when)
            self.assertEqual(p.modbus.writes, [], hhmm)

    def test_anything_that_outranks_flux_stops_it_starting(self):
        for store in ({"export_active": True}, {"import_active": True},
                      {"manager_paused": True}, {"happy_hour_import_active": True},
                      {"vpp_state": "active"}):
            p, when = self._cold((1, 45), **store)
            self._step(p, when)
            self.assertFalse(self._started(p), store)

    def test_a_weather_warning_does_not_stop_it(self):
        """A warning night is the cold night. The warning writes nothing."""
        p, when = self._cold((1, 45), storm_level="yellow")
        self._step(p, when)
        self.assertTrue(self._started(p))

    def test_not_when_flux_is_off_or_the_tariff_is_not_flux(self):
        p, when = self._at((1, 45), soc=20.0, prefs=plugin_prefs_off())
        p.latest_inverter_data["batteryMinTempC"] = 4.4
        self._step(p, when)
        self.assertEqual(p.modbus.writes, [])
        p, when = self._cold((1, 45))
        p.latest_rates_data["tariff_info"]["tariff_key"] = "go"
        self._step(p, when)
        self.assertEqual(p.modbus.writes, [])

    def test_no_connection_starts_nothing_and_claims_nothing(self):
        p, when = self._cold((1, 45))
        p.modbus.connected = False
        self._step(p, when)
        self.assertEqual(p.modbus.writes, [])
        self.assertFalse(p.store.get("cold_warmup_active"))

    def test_a_refused_command_is_not_recorded_as_running(self):
        p, when = self._cold((1, 45))
        p.modbus.force_charge = MagicMock(return_value=False)
        self._step(p, when)
        self.assertFalse(p.store.get("cold_warmup_active"))


def plugin_prefs_off():
    from test_flux_supervisor import _flux_prefs
    return _flux_prefs(fluxEnabled=False)


class TestWhileItRuns(_WarmupCase):

    def _running(self):
        p, when = self._cold((1, 31))
        self._step(p, when)
        self.assertTrue(p.store["cold_warmup_active"])
        return p, when

    def test_it_is_written_once_and_kept_as_the_battery_warms(self):
        p, when = self._running()
        p.latest_inverter_data["batteryMinTempC"] = 7.5      # warmed past the threshold
        later = when + timedelta(minutes=10)
        self._step(p, later)
        self.assertEqual(p.modbus.writes.count(WARMUP_WRITE), 1)
        self.assertTrue(p.store["cold_warmup_active"])

    def test_it_does_not_outrank_flux(self):
        """An owner would start the five-minute stand-down and delay the 2am charge,
        which is the fault 5.146.1 fixed for a weather warning."""
        p, when = self._running()
        self.assertEqual(self._call_at(p, when, p._flux_other_owner), "")

    def test_the_plugin_counts_the_inverter_as_in_use(self):
        p, _ = self._running()
        self.assertTrue(p._inverter_in_use())

    def test_a_writer_arriving_is_given_the_inverter_with_no_writes(self):
        """It has written its own mode already. Self Consumption over the top of a
        live import or a paid export is the failure to avoid."""
        for store in ({"export_active": True}, {"import_active": True},
                      {"vpp_state": "active"}, {"happy_hour_import_active": True}):
            p, when = self._running()
            before = list(p.modbus.writes)
            p.store.update(store)
            self._step(p, when + timedelta(minutes=5))
            self.assertEqual(p.modbus.writes, before, store)
            self.assertFalse(p.store["cold_warmup_active"], store)

    def test_a_pause_hands_the_inverter_back(self):
        """Nothing else is driving, so the charge mode is ours to take off."""
        p, when = self._running()
        p.store["manager_paused"] = True
        self._step(p, when + timedelta(minutes=5))
        self.assertIn("set_self_consumption", p.modbus.writes)
        self.assertFalse(p.store["cold_warmup_active"])

    def test_a_failed_hand_back_is_tried_again(self):
        p, when = self._running()
        p.store["manager_paused"] = True
        p.modbus.set_self_consumption = MagicMock(return_value=False)
        self._step(p, when + timedelta(minutes=5))
        self.assertTrue(p.store["cold_warmup_active"], "not handed back, so still ours")
        p.modbus.set_self_consumption = MagicMock(return_value=True)
        self._step(p, when + timedelta(minutes=6))
        self.assertFalse(p.store["cold_warmup_active"])


class TestTheHandoverAtTwo(_WarmupCase):

    def _running_until_two(self):
        p, when = self._cold((1, 31), flux_preempted_at=0.0)
        self._step(p, when)
        two = when.replace(hour=2, minute=0, second=5)
        return p, two

    def test_the_window_opening_ends_it_with_no_writes(self):
        p, two = self._running_until_two()
        before = list(p.modbus.writes)
        self._step(p, two)
        self.assertFalse(p.store["cold_warmup_active"])
        self.assertEqual(p.modbus.writes, before)

    def test_flux_claims_the_charge_on_the_same_tick(self):
        p, two = self._running_until_two()
        self._step(p, two)
        self._run(p, two, at=two)
        self.assertEqual(p.store["flux_owner_reason"], "")
        self.assertEqual(len(p.flux_executor.targets()), 1)
        self.assertEqual(p.store["flux_decision"].mode, fs.MODE_CHARGE)


class TestItIsNotADayRateImport(_WarmupCase):
    """Day-rate energy is never sold in the peak, so a recorded day-rate import
    stands the whole 4pm sale down. Half a kWh of warm-up must not do that."""

    def _running(self):
        p, when = self._cold((1, 31))
        self._step(p, when)
        p.modbus.read_ems_mode     = MagicMock(return_value=plugin.EMS_CHARGE_PV_FIRST)
        p.modbus.read_charge_limit = MagicMock(return_value=plugin.COLD_WARMUP_W)
        return p, when + timedelta(minutes=1)

    def test_the_verify_pass_leaves_the_warmup_alone(self):
        p, when = self._running()
        before = len(p.modbus.writes)
        self._call_at(p, when, p._verify_ems_registers)
        # The pass may hold the site import cap, which is not the warm-up's business.
        added = [w for w in p.modbus.writes[before:]
                 if w == "set_self_consumption"
                 or (isinstance(w, tuple) and w[0] in ("mode", "charge_limit",
                                                       "discharge_limit"))]
        self.assertEqual(added, [])
        self.assertEqual(p.pluginPrefs.get("dayRateImportDate", ""), "")

    def test_the_verify_pass_puts_a_lost_cap_back(self):
        p, when = self._running()
        p.modbus.read_charge_limit = MagicMock(return_value=10000)
        self._call_at(p, when, p._verify_ems_registers)
        self.assertIn(("charge_limit", plugin.COLD_WARMUP_W, False), p.modbus.writes)

    def test_the_manager_pass_does_not_record_it_either(self):
        p, when = self._running()
        p.store["remote_ems_mode_seen"] = (plugin.EMS_CHARGE_PV_FIRST,
                                           when.timestamp() - 10)
        p._note_day_rate_import = MagicMock()
        self._call_at(p, when, lambda: p._act_on_decision(SimpleNamespace(
            action=plugin.ACTION_SCHEDULE_IMPORT, target_soc_pct=40.0,
            scheduled_time=when, reason="test")))
        p._note_day_rate_import.assert_not_called()

    def test_the_minutes_after_the_handover_are_covered_too(self):
        p, when = self._running()
        two = when.replace(hour=2, minute=0, second=5)
        self._step(p, two)
        self.assertTrue(self._call_at(p, two + timedelta(seconds=60),
                                      p._cold_warmup_recent))
        self.assertFalse(self._call_at(p, two + timedelta(seconds=200),
                                       p._cold_warmup_recent))

    def test_a_mode_still_reading_charge_just_after_an_early_stop_is_ours(self):
        """Stopped early by a real owner; the register can read charge for a moment."""
        p, when = self._running()
        p.store["cold_warmup_active"]   = False
        p.store["cold_warmup_ended_at"] = when.timestamp() - 30
        p.modbus.read_ems_mode = MagicMock(side_effect=[plugin.EMS_CHARGE_PV_FIRST, 0x02])
        p._note_day_rate_import = MagicMock()
        self._call_at(p, when, p._verify_ems_registers)
        p._note_day_rate_import.assert_not_called()

    def test_a_charge_nobody_started_is_still_recorded(self):
        """Control: without a warm-up the guard still fires."""
        p, when = self._cold((1, 45), temp=12.0)
        p.modbus.read_ems_mode = MagicMock(side_effect=[plugin.EMS_CHARGE_PV_FIRST, 0x02])
        p._note_day_rate_import = MagicMock()
        self._call_at(p, when, p._verify_ems_registers)
        p._note_day_rate_import.assert_called_once()
