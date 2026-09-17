"""Fault-injected Flux driver tests; no sockets or real Indigo operations."""
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
import sys
import tempfile
import unittest
import json
sys.path.insert(0, str(Path(__file__).parent))
from flux_execution import FluxExecutor, FluxTarget


class Registers:
    inverter_max_w = 10000
    def __init__(self):
        self.values = {'mode': 3, 'charge': 10000, 'discharge': 10000, 'top': 90., 'bottom': 1.}
        self.enabled = False
        self.writes, self.fail, self.lie, self.before_write = [], None, None, None
    def _set(self, key, value):
        if self.before_write: self.before_write(key, value)
        self.writes.append((key, value))
        if self.fail == key: return False
        if self.lie != key: self.values[key] = value
        return True
    def enable_remote_ems(self):
        self.writes.append(('enable', True))
        if self.fail == 'enable': return False
        if self.lie != 'enable': self.enabled = True
        return True
    def read_remote_ems_enabled(self): return self.enabled
    def set_remote_ems_mode(self, value): return self._set('mode', value)
    def set_charge_limit(self, value): return self._set('charge', value)
    def set_discharge_limit(self, value): return self._set('discharge', value)
    def set_charge_cutoff(self, value): return self._set('top', round(value*10)/10)
    def set_discharge_cutoff(self, value): return self._set('bottom', round(value*10)/10)
    def read_ems_mode(self): return self.values['mode']
    def read_charge_limit(self): return self.values['charge']
    def read_discharge_limit(self): return self.values['discharge']
    def read_charge_cutoff(self): return self.values['top']
    def read_discharge_cutoff(self): return self.values['bottom']


class FluxExecutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.now = datetime.fromisoformat('2026-10-01T02:00:00+01:00')
        self.d = Registers(); self.path = Path(self.tmp.name)/'owner.json'; self.e = self.executor()
    def executor(self):
        return FluxExecutor(self.d, self.path, baseline_charge_w=10000,
                            baseline_discharge_w=10000, baseline_discharge_cutoff_pct=20.,
                            clock=lambda: self.now)
    def target(self, **kw):
        return replace(FluxTarget(self.now, self.now+timedelta(minutes=30), self.now,
                       self.now+timedelta(seconds=10), 3, 6000, 0, 80., 20.), **kw)
    def arm(self, t=None):
        self.assertEqual(self.e.step(t or self.target(), self.now), 'released')
        self.assertEqual(self.e.step(t or self.target(), self.now), 'applied')
    def test_driver_rebind_retains_claim_and_reconciles_before_new_start(self):
        self.arm()
        replacement = Registers()
        self.e.rebind(replacement)
        self.assertTrue(self.e.owns_control)
        self.assertEqual(replacement.writes, [])
        self.assertEqual(self.e.step(self.target(), self.now), 'released')
        self.assertEqual(replacement.values['mode'], 2)
        self.assertEqual(self.e.step(self.target(), self.now), 'applied')

    def test_bad_rebind_preserves_original_driver_and_claim(self):
        self.arm()
        replacement = Registers()
        replacement.inverter_max_w = 500
        with self.assertRaises(ValueError):
            self.e.rebind(replacement)
        self.assertIs(self.e.raw, self.d)
        self.assertTrue(self.e.owns_control)

    def test_constructor_no_writes_restart_reconciles(self):
        self.assertEqual(self.d.writes, []); self.arm(); self.d.writes.clear()
        e = self.executor(); self.assertEqual(self.d.writes, [])
        self.assertEqual(e.step(self.target(), self.now), 'released')
        self.assertEqual(self.d.values, {'mode':2,'charge':10000,'discharge':10000,'top':100.,'bottom':20.})
    def test_journal_and_cutoffs_precede_enable(self):
        self.e.step(None, self.now); self.d.writes.clear()
        self.d.before_write = lambda k,v: self.assertTrue(json.loads(self.path.read_text())['owns'])
        self.assertEqual(self.e.step(self.target(), self.now), 'applied')
        enable = self.d.writes.index(('enable', True))
        for value in [('top',80.),('bottom',20.),('charge',6000),('discharge',0)]:
            self.assertLess(self.d.writes.index(value), enable)
        self.assertGreater(self.d.writes.index(('mode',3)), enable)
    def test_five_boundary_stops_and_restores_policy(self):
        self.now = self.now.replace(hour=4,minute=59,second=55)
        t = self.target(expires_at=self.now+timedelta(seconds=5),decision_until=self.now+timedelta(seconds=5))
        self.arm(t); self.now += timedelta(seconds=5)
        self.assertEqual(self.e.step(t,self.now),'released')
        self.assertEqual((self.d.values['mode'],self.d.values['top'],self.d.values['bottom']),(2,100.,20.))
    def test_fresh_telemetry_cannot_renew_expired_decision(self):
        self.arm(); old=self.target(); self.now+=timedelta(minutes=30)
        t=replace(old,observed_at=self.now,expires_at=self.now+timedelta(seconds=10))
        self.assertEqual(self.e.step(t,self.now),'released'); self.assertIn('Expired',self.e.last_error)
    def test_export_hold_solar_and_house_supply(self):
        self.now=self.now.replace(hour=16)
        self.arm(self.target(ems_mode=5,charge_limit_w=0,discharge_limit_w=3000))
        for c,d in [(0,0),(2000,0),(0,500),(10000,10000)]:
            self.assertEqual(self.e.step(self.target(ems_mode=2,charge_limit_w=c,discharge_limit_w=d),self.now),'applied')
            self.assertEqual((self.d.values['mode'],self.d.values['charge'],self.d.values['discharge']),(2,c,d))
    def test_confirmed_supervisor_exclusive_then_release_reconcile(self):
        self.arm(); self.d.writes.clear()
        self.assertEqual(self.e.step(self.target(),self.now,supervisor_owns=True),'supervisor')
        self.assertEqual(self.d.writes,[]); self.assertFalse(self.e.owns_control)
        self.d.values['mode']=5
        self.assertEqual(self.e.step(self.target(),self.now),'released'); self.assertEqual(self.d.values['mode'],2)
    def test_comms_loss_then_recovery(self):
        self.arm(); self.d.writes.clear()
        self.assertEqual(self.e.step(None,self.now,communications_ok=False),'pending')
        self.assertTrue(self.e.owns_control); self.assertEqual(self.d.writes,[])
        self.assertEqual(self.e.step(self.target(),self.now),'released')
        self.assertEqual(self.e.step(self.target(),self.now),'applied')
    def test_cutoff_failure_or_false_success_never_commits_charge(self):
        for attribute in ['fail','lie']:
            self.e.step(None,self.now); setattr(self.d,attribute,'top'); self.d.writes.clear()
            self.assertEqual(self.e.step(self.target(),self.now),'pending')
            self.assertNotIn(('mode',3),self.d.writes); self.assertTrue(self.e.owns_control)
            setattr(self.d,attribute,None)
    def test_disk_full_blocks_start_not_restorative_stop(self):
        self.e.step(None,self.now)
        def fail(): raise OSError('disk full')
        self.e._save=fail; self.d.writes.clear()
        self.assertEqual(self.e.step(self.target(),self.now),'pending')
        self.assertNotIn(('mode',3),self.d.writes); self.assertEqual(self.d.values['mode'],2)
        self.assertEqual(self.e.step(None,self.now),'pending'); self.assertEqual(self.d.values['top'],100.)
    def test_failed_stop_never_lifts_cutoffs_while_exporting(self):
        self.now=self.now.replace(hour=16); self.arm(self.target(ems_mode=5,charge_limit_w=0,discharge_limit_w=3000))
        self.d.lie='mode'; self.d.writes.clear()
        self.assertEqual(self.e.step(None,self.now),'pending')
        self.assertNotIn(('top',100.),self.d.writes); self.assertNotIn(('bottom',20.),self.d.writes)
    def test_expiry_during_io_prevents_charge_commit(self):
        self.e.step(None,self.now); t=self.target()
        self.d.before_write=lambda k,v:setattr(self,'now',self.now+timedelta(seconds=3)); self.d.writes.clear()
        self.assertEqual(self.e.step(t,self.now),'pending'); self.assertNotIn(('mode',3),self.d.writes)
    def test_unchanged_target_reads_without_register_churn(self):
        self.arm(); self.d.writes.clear(); self.now+=timedelta(seconds=5)
        self.assertEqual(self.e.step(self.target(),self.now),'applied'); self.assertEqual(self.d.writes,[])

    def test_power_only_change_adjusts_in_place_without_neutralising(self):
        """17-Sep-2026 live: a moving peak-export power neutralised every tick and
        dropped a 4 kW export to zero for 20-30 s each time."""
        self.now=self.now.replace(hour=16)
        self.arm(self.target(ems_mode=5,charge_limit_w=0,discharge_limit_w=500))
        self.d.writes.clear(); self.now+=timedelta(seconds=5)
        self.assertEqual(self.e.step(self.target(ems_mode=5,charge_limit_w=0,discharge_limit_w=3900),self.now),'applied')
        self.assertEqual(self.d.writes,[('charge',0),('discharge',3900)])
        self.assertEqual((self.d.values['mode'],self.d.values['discharge']),(5,3900))

    def test_mode_or_band_change_still_neutralises_first(self):
        self.now=self.now.replace(hour=16)
        self.arm(self.target(ems_mode=5,charge_limit_w=0,discharge_limit_w=3000))
        self.d.writes.clear(); self.now+=timedelta(seconds=5)
        self.e.step(self.target(ems_mode=5,charge_limit_w=0,discharge_limit_w=3000,discharge_cutoff_pct=40.),self.now)
        self.assertEqual(self.d.writes[:3],[('charge',0),('discharge',0),('mode',2)])

    def test_failed_in_place_write_falls_back_to_full_apply(self):
        self.now=self.now.replace(hour=16)
        self.arm(self.target(ems_mode=5,charge_limit_w=0,discharge_limit_w=3000))
        self.d.writes.clear(); self.now+=timedelta(seconds=5); self.d.lie='discharge'
        self.assertEqual(self.e.step(self.target(ems_mode=5,charge_limit_w=0,discharge_limit_w=3900),self.now),'pending')
        # The in-place write is tried, then the full apply's neutralise (limits to 0).
        self.assertEqual(self.d.writes[:2],[('charge',0),('discharge',3900)])
        self.assertIn(('discharge',0),self.d.writes[2:])

    def test_realistic_slow_staging_within_observation_budget(self):
        self.e.step(None,self.now)
        t=self.target(expires_at=self.now+timedelta(seconds=60))
        self.d.before_write=lambda k,v:setattr(self,'now',self.now+timedelta(seconds=3))
        self.assertEqual(self.e.step(t,self.now),'applied')
        self.assertGreater((self.now-t.observed_at).total_seconds(),10)

    def test_observation_budget_cannot_exceed_sixty_seconds(self):
        self.e.step(None,self.now)
        t=self.target(expires_at=self.now+timedelta(seconds=61))
        self.assertEqual(self.e.step(t,self.now),'released')
        self.assertIn('lease',self.e.last_error)

    def test_real_modbus_primitives_with_fake_transport_and_throttle(self):
        """Exercise real units, addresses and duplicate driver readbacks offline."""
        import sigenergy_modbus as sm
        from types import SimpleNamespace
        import logging
        words={}
        calls=[]
        def response(values=()):
            return SimpleNamespace(registers=list(values),isError=lambda:False)
        def read(*,address,count,device_id):
            calls.append(('read',address,device_id))
            return response(words.get(address+i,0) for i in range(count))
        def write_many(*,address,values,device_id):
            calls.append(('write',address,device_id))
            words.update({address+i:v for i,v in enumerate(values)})
            return response()
        def write_one(*,address,value,device_id):
            return write_many(address=address,values=[value],device_id=device_id)
        def advance(seconds):
            self.now+=timedelta(seconds=seconds)
        driver=sm.SigenergyModbus('unused.invalid',logger=logging.getLogger('flux-test'),
                                 sleep_func=advance)
        driver._throttle=lambda:advance(1)
        driver._connected=True
        driver.client=SimpleNamespace(read_holding_registers=read,
            write_register=write_one,write_registers=write_many)
        executor=FluxExecutor(driver,self.path,baseline_charge_w=10000,
            baseline_discharge_w=10000,baseline_discharge_cutoff_pct=20.,
            clock=lambda:self.now)
        self.assertEqual(executor.step(None,self.now),'released')
        target=self.target(expires_at=self.now+timedelta(seconds=60),
                           charge_cutoff_pct=81.7,discharge_cutoff_pct=20.2)
        calls.clear()
        self.assertEqual(executor.step(target,self.now),'applied')
        self.assertEqual(words[sm.HOLD_ESS_CHARGE_CUTOFF],817)
        self.assertEqual(words[sm.HOLD_ESS_DISCHARGE_CUTOFF],202)
        self.assertEqual(words[sm.HOLD_ESS_MAX_CHARGE+1],6000)
        self.assertEqual(words[sm.HOLD_REMOTE_EMS_MODE],3)
        self.assertTrue(all(slave==driver.plant_address for _,_,slave in calls))
        self.assertGreater((self.now-target.observed_at).total_seconds(),10)
        self.assertLess((self.now-target.observed_at).total_seconds(),60)

    def test_expiry_during_final_readback_is_not_reported_as_applied(self):
        self.e.step(None,self.now)
        t=self.target()
        original=self.d.read_discharge_cutoff
        def slow_final_read():
            if self.d.values['mode']==3:
                self.now+=timedelta(seconds=11)
            return original()
        self.d.read_discharge_cutoff=slow_final_read
        self.assertEqual(self.e.step(t,self.now),'pending')
        self.assertTrue(self.e.owns_control)
        self.assertEqual(self.d.values['mode'],2)
    def test_bad_numbers_modes_and_cutoffs_rejected(self):
        self.e.step(None,self.now)
        for kw in [{'charge_limit_w':True},{'charge_limit_w':10001},{'discharge_cutoff_pct':float('nan')},
                   {'charge_cutoff_pct':80.05},{'ems_mode':5},{'ems_mode':6},{'discharge_limit_w':100}]:
            self.assertEqual(self.e.step(self.target(**kw),self.now),'released'); self.assertTrue(self.e.last_error)
    def test_disabled_remote_ems_is_not_an_acknowledged_target(self):
        self.e.step(None,self.now); self.d.enabled=False; self.d.lie='enable'
        self.assertEqual(self.e.step(self.target(),self.now),'pending')
        self.assertTrue(self.e.owns_control)

    def test_export_stops_at_nineteen_and_requested_axle_handover(self):
        self.now=self.now.replace(hour=18,minute=59,second=55)
        t=self.target(ems_mode=5,charge_limit_w=0,discharge_limit_w=3000,
                      expires_at=self.now+timedelta(seconds=5),decision_until=self.now+timedelta(seconds=5))
        self.arm(t); self.now+=timedelta(seconds=5)
        self.assertEqual(self.e.step(t,self.now),'released')
        self.assertEqual(self.d.values['mode'],2)
        self.d.writes.clear()
        self.assertEqual(self.e.step(None,self.now,supervisor_owns=True),'supervisor')
        self.assertEqual(self.d.writes,[])

    def test_current_storm_baseline_is_restored_instead_of_old_floor(self):
        self.arm()
        self.e.configure_baseline(baseline_charge_w=10000,baseline_discharge_w=10000,
                                  baseline_discharge_cutoff_pct=30.)
        self.assertEqual(self.e.step(None,self.now),'released')
        self.assertEqual(self.d.values['bottom'],30.)

    def test_missing_or_exceptional_readback_never_releases_ownership(self):
        self.arm(); original=self.d.read_ems_mode
        self.d.read_ems_mode=lambda:None
        self.assertEqual(self.e.step(None,self.now),'pending')
        self.assertTrue(self.e.owns_control)
        def broken(): raise ConnectionError('read failed')
        self.d.read_ems_mode=broken
        self.assertEqual(self.e.step(None,self.now),'pending')
        self.d.read_ems_mode=original
        self.assertEqual(self.e.step(None,self.now),'released')

    def test_both_dst_transitions_follow_local_window(self):
        for stamp,expected in [('2026-10-25T01:00:00+00:00','released'),('2027-03-28T01:00:00+00:00','applied')]:
            self.now=datetime.fromisoformat(stamp); self.e.step(None,self.now)
            self.assertEqual(self.e.step(self.target(),self.now),expected)


if __name__=='__main__': unittest.main()
