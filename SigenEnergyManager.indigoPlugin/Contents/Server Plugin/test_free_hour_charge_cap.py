#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    test_free_hour_charge_cap.py
# Description: The 25-Oct-2026 free hour that charged at 200 W. Solar Overflow's
#              charge cap was left standing under the Happy Hour import, the verify
#              pass put it back a minute in, the shortfall check blamed the mode and
#              switched the panels off, and the result note said the free power went
#              "most of it into the battery" when the battery took 0.27 of 3.97 kWh.
# Author:      CliveS & Claude Opus 5.5
# Date:        25-10-2026
# Version:     1.0

import os
import sys
import tempfile
import threading
import types
import unittest
from datetime import datetime
from unittest.mock import MagicMock
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

import plugin                        # noqa: E402
import happy_hour_booking as hb      # noqa: E402

LONDON = ZoneInfo("Europe/London")


def _bare(**store):
    p = plugin.Plugin.__new__(plugin.Plugin)
    p._state_lock = threading.RLock()
    p.logger = MagicMock()
    p.debug = False
    p.pluginPrefs = {"inverterMaxKw": "10.0", "batteryHealthCutoff": "1.0"}
    p.store = dict(store)
    m = p.modbus = MagicMock()
    m.connected = True
    return p


class TestVerifyLetsAnImportOutrankTheOverflowCap(unittest.TestCase):
    """14:02 live: "Charge limit mismatch: inverter=10000W expected=200W". The
    verify pass read solar_overflow_active before import_active, so the cap of an
    overflow the import had replaced won, every minute, for the whole hour."""

    def _mk(self, inverter_charge_w, overflow=True, importing=True, happy=True):
        p = _bare(
            vpp_state=plugin.VPP_IDLE,
            saving_session_export_active=False,
            export_active=False,
            solar_overflow_active=overflow,
            solar_overflow_charge_cap_w=200,
            vpp_cutoff_raised=False,
            flood_prev_target_soc=None,
            import_charge_cutoff_pct=100.0 if importing else None,
            import_active=importing,
            import_power_w=10000 if importing else None,
            happy_hour_import_active=happy and importing,
        )
        m = p.modbus
        m.read_ems_mode.return_value         = 0x04 if importing else 0x02
        m.read_discharge_limit.return_value  = 0 if (happy and importing) else 10000
        m.read_charge_limit.return_value     = inverter_charge_w
        m.read_discharge_cutoff.return_value = 1.0
        m.read_charge_cutoff.return_value    = 100.0 if importing else 100.0
        return p

    def test_the_import_rate_is_left_alone_under_a_stale_overflow_flag(self):
        p = self._mk(inverter_charge_w=10000)
        p._verify_ems_registers()
        p.modbus.set_charge_limit.assert_not_called()

    def test_a_stale_cap_on_the_inverter_is_put_back_to_the_import_rate(self):
        p = self._mk(inverter_charge_w=200)
        p._verify_ems_registers()
        p.modbus.set_charge_limit.assert_called_once_with(10000)

    def test_an_ordinary_import_below_the_maximum_keeps_its_own_rate(self):
        """The old else-branch expected the inverter maximum for any import, so
        an import started at less than that was 'corrected' up within a minute."""
        p = self._mk(inverter_charge_w=6000, overflow=False, happy=False)
        p.store["import_power_w"] = 6000
        p._verify_ems_registers()
        p.modbus.set_charge_limit.assert_not_called()

    def test_overflow_without_an_import_still_holds_its_cap(self):
        p = self._mk(inverter_charge_w=10000, importing=False)
        p._verify_ems_registers()
        p.modbus.set_charge_limit.assert_called_once_with(200)


class TestStartingAnImportEndsSolarOverflow(unittest.TestCase):
    """Overflow starting cleared import_active, but no import starting cleared
    solar_overflow_active. Both flags set at once is what the verify pass met."""

    def _mk(self, ok):
        p = _bare(solar_overflow_active=True, solar_overflow_charge_cap_w=200)
        p.modbus.force_charge.return_value = ok
        return p

    def test_a_confirmed_import_clears_the_overflow_state(self):
        p = self._mk(ok=True)
        self.assertTrue(p._force_charge(10000, 100.0))
        self.assertFalse(p.store["solar_overflow_active"])
        self.assertEqual(p.store["solar_overflow_charge_cap_w"], 0)

    def test_a_refused_import_leaves_overflow_running(self):
        p = self._mk(ok=False)
        self.assertFalse(p._force_charge(10000, 100.0))
        self.assertTrue(p.store["solar_overflow_active"])
        self.assertEqual(p.store["solar_overflow_charge_cap_w"], 200)


class TestShortfallCheckReadsTheChargeLimitFirst(unittest.TestCase):
    """14:04 live: the battery took 198 W and the check blamed Charge PV First,
    switching to Grid First, which turned the panels off for 55 minutes. The
    battery was held by a 200 W charge limit; no mode could have fixed that."""

    def _mk(self, limit_on_inverter):
        p = _bare(import_active=True,
                  import_ems_mode=plugin.EMS_CHARGE_PV_FIRST,
                  import_power_w=10000,
                  import_charge_cutoff_pct=100.0,
                  pv_first_short_passes=plugin.PV_FIRST_SHORTFALL_PASSES - 1)
        p.latest_inverter_data = {"batteryPowerWatts": 198, "pvPowerWatts": 2486,
                                  "batterySoc": 88.9}
        p.modbus.read_charge_limit.return_value  = limit_on_inverter
        p.modbus.set_remote_ems_mode.return_value = True
        return p

    def test_a_low_charge_limit_is_restored_and_the_mode_kept(self):
        p = self._mk(limit_on_inverter=200)
        p._check_pv_first_charge()
        p.modbus.set_charge_limit.assert_called_once_with(10000)
        p.modbus.set_remote_ems_mode.assert_not_called()
        self.assertEqual(p.store["import_ems_mode"], plugin.EMS_CHARGE_PV_FIRST)
        self.assertEqual(p.store["pv_first_short_passes"], 0)

    def test_with_the_limit_right_the_mode_still_falls_back(self):
        p = self._mk(limit_on_inverter=10000)
        p._check_pv_first_charge()
        p.modbus.set_remote_ems_mode.assert_called_once_with(plugin.EMS_CHARGE_GRID_FIRST)
        p.modbus.set_charge_limit.assert_not_called()

    def test_an_unreadable_limit_falls_back_as_before(self):
        p = self._mk(limit_on_inverter=None)
        p._check_pv_first_charge()
        p.modbus.set_remote_ems_mode.assert_called_once_with(plugin.EMS_CHARGE_GRID_FIRST)


class TestTheFreeHourCountsWhatTheBatteryStored(unittest.TestCase):
    """The grid-import delta is the free power; it says nothing about where it
    went. The battery's own daily charge counter does."""

    def test_entry_anchors_the_battery_charge_counter(self):
        from battery_manager import Decision
        p = _bare(import_active=False, export_active=False,
                  happy_hour_import_active=False, happy_hour_anchor_kwh=None,
                  grid_import_daily_kwh=28.77, import_target_soc=0.0,
                  import_scheduled_time=None)
        p.modbus.force_charge.return_value = True
        p.latest_inverter_data = {"batterySoc": 88.7, "batteryDailyChargeKwh": 30.39}
        p._set_import_cutoff = MagicMock()
        p._save_accumulators = MagicMock()
        p._trigger_event     = MagicMock()
        p._happy_hour_window = MagicMock(return_value=None)
        p._act_on_decision(Decision(action=plugin.ACTION_HAPPY_HOUR_IMPORT,
                                    reason="free hour", target_soc_pct=100.0,
                                    power_watts=10000))
        self.assertEqual(p.store["happy_hour_battery_anchor_kwh"], 30.39)

    def _end(self, battery_anchor, battery_now):
        p = _bare(happy_hour_import_active=True, import_active=True,
                  happy_hour_anchor_kwh=28.82, grid_import_daily_kwh=32.79,
                  happy_hour_battery_anchor_kwh=battery_anchor)
        p.latest_inverter_data = {"batteryDailyChargeKwh": battery_now}
        p._note_free_hour_kwh      = MagicMock()
        p._happy_hour_result_note  = MagicMock()
        p._save_accumulators       = MagicMock()
        p._restore_import_cutoff   = MagicMock()
        p._end_happy_hour_import("the free hour ended")
        return p

    def test_the_end_records_what_the_battery_took(self):
        p = self._end(30.39, 30.66)
        self.assertEqual(p.store["happy_hour_battery_kwh"], 0.27)
        self.assertIsNone(p.store["happy_hour_battery_anchor_kwh"])
        kwargs = p._happy_hour_result_note.call_args.kwargs
        self.assertEqual(kwargs["battery_kwh"], 0.27)

    def test_no_anchor_means_no_battery_figure(self):
        """After a restart from an older version, which never saved one."""
        p = self._end(None, 30.66)
        self.assertIsNone(p._happy_hour_result_note.call_args.kwargs["battery_kwh"])

    def test_the_battery_anchor_survives_a_restart(self):
        p = _bare(pv_daily_kwh=1.0, grid_import_daily_kwh=30.0,
                  grid_export_daily_kwh=2.0, home_daily_kwh=3.0,
                  peak_soc=90.0, min_soc=40.0, today_date="2026-10-25",
                  pv_lifetime_start_kwh=100.0, import_lifetime_start_kwh=10.0,
                  export_lifetime_start_kwh=20.0,
                  happy_hour_import_active=True, happy_hour_anchor_kwh=28.82,
                  happy_hour_battery_anchor_kwh=30.39, import_target_soc=100.0)
        p._save_home_profile = MagicMock()
        d = tempfile.mkdtemp()
        p._save_accumulators_locked(os.path.join(d, "accumulators.json"))
        q = _bare()
        q._get_data_dir = lambda: d
        q._load_accumulators()
        self.assertEqual(q.store["happy_hour_battery_anchor_kwh"], 30.39)


class TestTheResultSaysWhereThePowerWent(unittest.TestCase):

    def _slots(self):
        return [hb.Slot(event_id="", code="",
                        start=datetime(2026, 10, 25, 14, tzinfo=LONDON),
                        end=datetime(2026, 10, 25, 15, tzinfo=LONDON))]

    def _check(self, title, body):
        for text in (title, body):
            text.replace("£", "").encode("ascii")
            self.assertNotIn("|", text)
            self.assertNotIn("=", text)
            self.assertNotIn("  ", text)
        self.assertTrue(body.endswith("."), body)
        self.assertNotIn("pounds", body)

    def test_todays_hour_is_told_truthfully(self):
        title, body = hb.result_message(3.97, self._slots(), LONDON, 15.5,
                                        battery_kwh=0.27)
        self._check(title, body)
        self.assertNotIn("most of it into the battery", body)
        self.assertIn("The battery stored only 0.3 kWh of it", body)
        self.assertIn("the house used the other 3.7 kWh", body)
        self.assertIn("£0.62", body)

    def test_a_full_charge_says_it_went_into_the_battery(self):
        title, body = hb.result_message(4.0, self._slots(), LONDON, 15.5,
                                        battery_kwh=4.1)
        self._check(title, body)
        self.assertIn("The battery stored the lot.", body)

    def test_without_a_battery_figure_it_claims_nothing_about_the_battery(self):
        title, body = hb.result_message(3.97, self._slots(), LONDON, 15.5)
        self._check(title, body)
        self.assertNotIn("battery", body)


if __name__ == "__main__":
    unittest.main()
