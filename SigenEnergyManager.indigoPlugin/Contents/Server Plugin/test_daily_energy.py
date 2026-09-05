#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    test_daily_energy.py
# Description: Contract tests for daily_energy.py — the lifetime-anchor model that
#              replaced the mutable daily accumulators in v5.89.0. Runs without Indigo.
# Author:      CliveS & Claude Fable 5.1
# Date:        05-09-2026 12:45
# Version:     1.0
import unittest
from datetime import datetime

from daily_energy import (
    DailyEnergy, KEYS, local_midnight_epoch, readings_from_data, recovery_from_data,
    BACKWARDS_TOLERANCE_KWH,
)

D0 = "2026-09-04"
D1 = "2026-09-05"
D2 = "2026-09-06"

MID_D1 = local_midnight_epoch(D1)
MID_D2 = local_midnight_epoch(D2)


def _full(pv=7600.0, home=4000.0, imp=57.0, exp=3590.0, chg=2470.0, dis=2390.0):
    return {"pv": pv, "home": home, "gridImport": imp, "gridExport": exp,
            "batteryCharge": chg, "batteryDischarge": dis}


def _bump(readings, **delta):
    out = dict(readings)
    for k, v in delta.items():
        out[k] = out[k] + v
    return out


class TestMidnightAnchorsAndDerivation(unittest.TestCase):

    def test_midnight_epoch_is_local_not_utc(self):
        # 5-Sep-2026 is BST: local midnight is 23:00 UTC the day before.
        self.assertIsNotNone(MID_D1)
        self.assertEqual(datetime.utcfromtimestamp(MID_D1).strftime("%Y-%m-%d %H:%M"),
                         "2026-09-04 23:00")

    def test_daily_value_is_latest_minus_anchor(self):
        de = DailyEnergy()
        de.observe(_full(), MID_D1 + 30, D1, soc_pct=80.0)     # first read of the day
        de.observe(_bump(_full(), pv=5.5, home=2.25, gridExport=3.0), MID_D1 + 7200, D1)
        t = de.today()
        self.assertEqual(t["values"]["pv"], 5.5)
        self.assertEqual(t["values"]["home"], 2.25)
        self.assertEqual(t["values"]["gridExport"], 3.0)
        self.assertEqual(t["values"]["gridImport"], 0.0)
        self.assertEqual(t["soc_at_anchor"], 80.0)

    def test_rollover_takes_a_provisional_anchor_then_upgrades_on_first_fresh_read(self):
        de = DailyEnergy()
        de.observe(_full(), MID_D1 + 10, D1)
        pre_midnight = _bump(_full(), pv=20.0, home=18.0)
        de.observe(pre_midnight, MID_D2 - 40, D1)             # 23:59:20
        de.rollover(D2)
        self.assertTrue(de.today(D2)["provisional"])
        self.assertEqual(de.today(D2)["values"]["pv"], 0.0)   # the day starts at zero at once
        # 00:00:12 — the first genuine reading of the new day. The counter moved
        # a touch in the 52 s between; the anchor must follow it.
        post = _bump(pre_midnight, home=0.02)
        de.observe(post, MID_D2 + 12, D2)
        t = de.today(D2)
        self.assertFalse(t["provisional"])
        self.assertEqual(t["sources"]["home"], "midnight")
        self.assertEqual(t["values"]["home"], 0.0)            # NOT 0.02 — the anchor moved
        de.observe(_bump(post, home=3.0), MID_D2 + 3600, D2)
        self.assertEqual(de.today(D2)["values"]["home"], 3.0)

    def test_a_cached_reading_is_ignored_for_anchoring(self):
        de = DailyEnergy()
        de.observe(_full(), MID_D1 + 10, D1)
        pre = _bump(_full(), home=18.0)
        de.observe(pre, MID_D2 - 40, D1)
        de.rollover(D2)
        # The next cycle merges the SAME pre-midnight value from cache (this is
        # exactly the v5.88 fault). fresh=False must neither upgrade the anchor
        # nor move `latest`.
        de.observe(pre, MID_D2 + 12, D2, fresh=False)
        self.assertTrue(de.today(D2)["provisional"])
        de.observe(_bump(pre, home=0.5), MID_D2 + 70, D2)
        self.assertFalse(de.today(D2)["provisional"])
        self.assertEqual(de.today(D2)["values"]["home"], 0.0)

    def test_observe_with_a_new_date_rolls_over_by_itself(self):
        de = DailyEnergy()
        de.observe(_full(), MID_D1 + 10, D1)
        de.observe(_bump(_full(), pv=30.0), MID_D2 + 5, D2)   # plugin forgot rollover()
        self.assertEqual(de.today_date, D2)
        self.assertIn(D2, de.anchors)
        # provisional from the D1 reading, then upgraded by this post-midnight read
        self.assertFalse(de.today(D2)["provisional"])
        self.assertEqual(de.today(D2)["values"]["pv"], 0.0)

    def test_rollover_is_idempotent(self):
        de = DailyEnergy()
        de.observe(_full(), MID_D1 + 10, D1)
        de.rollover(D2)
        de.observe(_bump(_full(), pv=1.0), MID_D2 + 100, D2)   # upgrades the anchor
        before = dict(de.anchors[D2]["values"])
        de.rollover(D2)                                          # a second call
        self.assertEqual(de.anchors[D2]["values"], before)
        self.assertEqual(de.today(D2)["values"]["pv"], 0.0)


class TestMissingAnchorsRecoveryAndPartialDays(unittest.TestCase):

    def test_midday_start_recovers_exact_anchor_from_device_daily_counters(self):
        """Plugin starts at noon: pv/import/export have no boundary reading, but
        home/charge/discharge can be recovered from the device's own daily figures."""
        de = DailyEnergy()
        noon = MID_D1 + 12 * 3600
        de.observe(_full(home=4012.05, chg=2477.93, dis=2396.07), noon, D1,
                   recovery={"home": 12.05, "batteryCharge": 7.93, "batteryDischarge": 6.07})
        t = de.today()
        self.assertEqual(t["values"]["home"], 12.05)
        self.assertEqual(t["values"]["batteryCharge"], 7.93)
        self.assertEqual(t["values"]["batteryDischarge"], 6.07)
        self.assertEqual(t["sources"]["home"], "recovered")
        self.assertEqual(t["values"]["pv"], 0.0)
        self.assertEqual(t["sources"]["pv"], "late")
        self.assertTrue(t["partial"])          # pv/import/export lost the morning

    def test_recovery_is_taken_only_from_a_reading_this_cycle(self):
        """No recovery dict = no device daily counter this cycle. The key anchors
        late rather than guessing."""
        de = DailyEnergy()
        de.observe(_full(), MID_D1 + 12 * 3600, D1)
        self.assertEqual(de.today()["sources"]["home"], "late")

    def test_key_absent_at_anchor_time_gets_an_anchor_when_it_appears(self):
        de = DailyEnergy()
        r = _full()
        del r["home"]                                  # 30094 block failed this cycle
        de.observe(r, MID_D1 + 20, D1)
        self.assertIsNone(de.today()["values"]["home"])
        self.assertEqual(de.today()["sources"]["home"], "absent")
        de.observe(_full(home=4000.5), MID_D1 + 90, D1, recovery={"home": 0.5})
        self.assertEqual(de.today()["values"]["home"], 0.5)
        self.assertEqual(de.today()["sources"]["home"], "recovered")
        self.assertFalse(de.today()["partial"])

    def test_down_over_midnight_from_the_evening_anchors_late_and_says_so(self):
        """Plugin dies at 20:00, back at 09:00. The last reading is nine hours
        before midnight, so it is NOT a boundary value: pv/import/export anchor
        late and the day is flagged partial. home/charge/discharge are recovered
        exactly from the device's own daily counters."""
        de = DailyEnergy()
        de.observe(_full(), MID_D1 + 10, D1)
        de.observe(_bump(_full(), pv=25.0, home=15.0), MID_D2 - 4 * 3600, D1)   # 20:00
        de.observe(_bump(_full(), pv=45.0, home=22.0), MID_D2 + 9 * 3600, D2,     # 09:00 next day
                   recovery={"home": 3.5})
        t = de.today(D2)
        self.assertEqual(t["sources"]["pv"], "late")
        self.assertEqual(t["values"]["pv"], 0.0)
        self.assertTrue(t["partial"])
        self.assertEqual(t["sources"]["home"], "recovered")
        self.assertEqual(t["values"]["home"], 3.5)
        self.assertFalse(t["provisional"])

    def test_down_over_midnight_from_just_before_it_keeps_the_boundary(self):
        """Plugin dies at 23:58, back at 09:00. The provisional reading is two
        minutes old at midnight — near enough to keep. Not partial."""
        de = DailyEnergy()
        de.observe(_full(), MID_D1 + 10, D1)
        de.observe(_bump(_full(), pv=25.0), MID_D2 - 120, D1)                    # 23:58
        de.observe(_bump(_full(), pv=45.0), MID_D2 + 9 * 3600, D2)               # 09:00
        t = de.today(D2)
        self.assertEqual(t["sources"]["pv"], "boundary")
        self.assertEqual(t["values"]["pv"], 20.0)
        self.assertFalse(t["partial"])
        self.assertFalse(t["provisional"])

    def test_first_fresh_read_late_in_the_upgrade_window_still_upgrades(self):
        de = DailyEnergy()
        de.observe(_full(), MID_D1 + 10, D1)
        de.observe(_bump(_full(), pv=25.0), MID_D2 - 30, D1)
        de.observe(_bump(_full(), pv=25.02), MID_D2 + 540, D2)                   # 00:09
        self.assertEqual(de.today(D2)["sources"]["pv"], "midnight")
        self.assertEqual(de.today(D2)["values"]["pv"], 0.0)

    def test_meter_reset_drops_the_anchor_and_reanchors(self):
        de = DailyEnergy()
        de.observe(_full(), MID_D1 + 10, D1)
        de.observe(_bump(_full(), pv=10.0), MID_D1 + 3600, D1)
        self.assertEqual(de.today()["values"]["pv"], 10.0)
        # the plant re-based PV to zero (firmware replacement)
        de.observe(_full(pv=0.5), MID_D1 + 7200, D1)
        self.assertEqual(de.last_backwards, ("pv",))
        self.assertEqual(de.today()["sources"]["pv"], "late")
        self.assertEqual(de.today()["values"]["pv"], 0.0)
        de.observe(_full(pv=1.5), MID_D1 + 7300, D1)
        self.assertEqual(de.today()["values"]["pv"], 1.0)

    def test_rounding_wobble_is_not_a_meter_reset(self):
        de = DailyEnergy()
        de.observe(_full(), MID_D1 + 10, D1)
        de.observe(_full(pv=7600.0 - BACKWARDS_TOLERANCE_KWH / 2), MID_D1 + 20, D1)
        self.assertEqual(de.last_backwards, ())
        self.assertEqual(de.today()["values"]["pv"], 0.0)   # clamped, never negative

    def test_implausible_lifetime_value_is_ignored(self):
        de = DailyEnergy()
        de.observe(_full(), MID_D1 + 10, D1)
        de.observe(_full(pv=4.29e9), MID_D1 + 20, D1)     # 0xFFFFFFFF-style decode
        self.assertEqual(de.lifetime("pv"), 7600.0)


class TestResidualAndSnapshots(unittest.TestCase):

    def test_residual_closes_on_consistent_flows(self):
        de = DailyEnergy()
        de.observe(_full(), MID_D1 + 10, D1)
        # 4-Sep-2026 measured: pv 44.39 imp 0.10 dis 10.25 exp 17.66 chg 18.48 home 18.60
        de.observe(_bump(_full(), pv=44.39, gridImport=0.10, batteryDischarge=10.25,
                         gridExport=17.66, batteryCharge=18.48, home=18.60),
                   MID_D2 - 5, D1)
        self.assertEqual(de.residual(), 0.0)

    def test_residual_exposes_a_wrong_anchor(self):
        de = DailyEnergy()
        de.observe(_full(), MID_D1 + 10, D1)
        de.set_anchor(D1, {"home": 4000.0 - 10.5})     # home anchored 10.5 kWh too low
        de.observe(_bump(_full(), pv=10.0, home=10.0), MID_D1 + 3600, D1)
        self.assertEqual(de.residual(), -10.5)

    def test_residual_is_none_when_any_term_is_missing(self):
        de = DailyEnergy()
        r = _full()
        del r["batteryCharge"]
        de.observe(r, MID_D1 + 10, D1)
        self.assertIsNone(de.residual())

    def test_lifetime_snapshot_for_halfhourly_deltas(self):
        de = DailyEnergy()
        de.observe(_full(), MID_D1 + 10, D1)
        snap = de.lifetime_snapshot()
        self.assertEqual(set(snap), set(KEYS))
        self.assertEqual(snap["gridExport"], 3590.0)


class TestCompletedDays(unittest.TestCase):

    def test_completed_day_is_the_difference_of_two_midnight_anchors(self):
        de = DailyEnergy()
        de.observe(_full(), MID_D1 + 10, D1)
        # Consistent flows: 30 kWh of PV became 20.02 kWh of house use and 9.98 kWh of export.
        de.observe(_bump(_full(), pv=30.0, home=20.0, gridExport=9.98), MID_D2 - 30, D1)   # last of D1
        de.observe(_bump(_full(), pv=30.0, home=20.02, gridExport=9.98), MID_D2 + 15, D2)  # first of D2
        de.observe(_bump(_full(), pv=33.0, home=21.0, gridExport=9.98), MID_D2 + 3600, D2) # an hour on
        y = de.completed(D1)
        self.assertEqual(y["values"]["pv"], 30.0)
        self.assertEqual(y["values"]["home"], 20.02)      # up to the real boundary
        self.assertFalse(y["provisional"])
        self.assertEqual(de.today(D2)["values"]["pv"], 3.0)
        self.assertEqual(de.residual(D1), 0.0)

    def test_completed_before_the_boundary_upgrade_uses_the_provisional_anchor(self):
        de = DailyEnergy()
        de.observe(_full(), MID_D1 + 10, D1)
        de.observe(_bump(_full(), pv=30.0), MID_D2 - 30, D1)
        de.rollover(D2)
        y = de.completed(D1)
        self.assertEqual(y["values"]["pv"], 30.0)
        self.assertTrue(y["provisional"])

    def test_completed_of_a_running_day_falls_back_to_today(self):
        de = DailyEnergy()
        de.observe(_full(), MID_D1 + 10, D1)
        de.observe(_bump(_full(), pv=4.0), MID_D1 + 3600, D1)
        self.assertEqual(de.completed(D1)["values"]["pv"], 4.0)
        self.assertIsNone(de.completed("2020-01-01"))
        self.assertIsNone(de.completed("garbage"))


class TestPersistenceAndMigration(unittest.TestCase):

    def test_round_trip_preserves_anchors_latest_and_flags(self):
        de = DailyEnergy()
        de.observe(_full(), MID_D1 + 10, D1, soc_pct=77.7)
        de.observe(_bump(_full(), pv=3.0), MID_D1 + 900, D1)
        back = DailyEnergy.from_dict(de.to_dict())
        self.assertEqual(back.today_date, D1)
        self.assertEqual(back.today()["values"]["pv"], 3.0)
        self.assertEqual(back.today()["soc_at_anchor"], 77.7)
        self.assertEqual(back.to_dict(), de.to_dict())

    def test_from_dict_tolerates_garbage(self):
        for bad in (None, "x", 42, {"anchors": "no"}, {"anchors": {D1: {"values": {"pv": "abc"}}}}):
            try:
                obj = DailyEnergy.from_dict(bad)
            except Exception as exc:                     # pragma: no cover
                self.fail(f"from_dict raised on {bad!r}: {exc}")
            self.assertIsInstance(obj, DailyEnergy)

    def test_legacy_v588_anchors_seed_three_keys_only(self):
        de = DailyEnergy()
        seeded = de.migrate_legacy(D1, pv_start=7613.16, import_start=57.05, export_start=3593.99)
        self.assertEqual(sorted(seeded), ["gridExport", "gridImport", "pv"])
        de.observe(_full(pv=7629.06, imp=57.11, exp=3596.86, home=4014.9, chg=2471.58, dis=2396.45),
                   MID_D1 + 12 * 3600, D1,
                   recovery={"home": 12.05, "batteryCharge": 7.93, "batteryDischarge": 6.07})
        t = de.today()
        self.assertEqual(t["values"]["pv"], 15.9)
        self.assertEqual(t["values"]["gridImport"], 0.06)
        self.assertEqual(t["values"]["gridExport"], 2.87)
        self.assertEqual(t["values"]["home"], 12.05)
        self.assertEqual(t["sources"]["pv"], "migrated")
        self.assertFalse(t["partial"])

    def test_legacy_migration_never_overwrites_an_existing_anchor(self):
        de = DailyEnergy()
        de.observe(_full(), MID_D1 + 10, D1)
        self.assertEqual(de.migrate_legacy(D1, pv_start=1.0), [])
        self.assertEqual(de.anchors[D1]["values"]["pv"], 7600.0)

    def test_old_anchors_are_pruned_on_rollover(self):
        de = DailyEnergy()
        de.observe(_full(), local_midnight_epoch(D0) + 10, D0)
        de.observe(_bump(_full(), pv=1.0), MID_D1 + 10, D1)
        de.observe(_bump(_full(), pv=2.0), MID_D2 + 10, D2)
        self.assertNotIn(D0, de.anchors)
        self.assertIn(D1, de.anchors)
        self.assertIn(D2, de.anchors)


class TestDataDictAdapters(unittest.TestCase):

    def test_readings_and_recovery_pick_the_right_keys(self):
        data = {"pvLifetimeKwh": 1.0, "homeLifetimeKwh": 2.0, "gridImportLifetimeKwh": 3.0,
                "gridExportLifetimeKwh": 4.0, "batteryChargeLifetimeKwh": 5.0,
                "batteryDischargeLifetimeKwh": 6.0, "homeDailyDirectKwh": 0.7,
                "batteryDailyChargeKwh": 0.8, "batteryDailyDischargeKwh": 0.9, "batterySoc": 50}
        self.assertEqual(readings_from_data(data),
                         {"pv": 1.0, "home": 2.0, "gridImport": 3.0, "gridExport": 4.0,
                          "batteryCharge": 5.0, "batteryDischarge": 6.0})
        self.assertEqual(recovery_from_data(data),
                         {"home": 0.7, "batteryCharge": 0.8, "batteryDischarge": 0.9})
        self.assertEqual(readings_from_data({}), {})
        self.assertEqual(recovery_from_data(None), {})


if __name__ == "__main__":
    unittest.main()
