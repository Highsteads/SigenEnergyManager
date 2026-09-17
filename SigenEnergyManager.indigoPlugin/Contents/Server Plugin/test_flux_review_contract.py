"""Independent Codex regressions for event-aware Flux energy accounting."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import unittest
from zoneinfo import ZoneInfo

import flux_strategy as fs


TZ = ZoneInfo('Europe/London')


def instant(hour, minute=0):
    return datetime(2026, 9, 16, hour, minute, tzinfo=TZ).astimezone(timezone.utc)


def scenario(hour=16, **changes):
    buckets={f'2026-09-{day} {h:02d}:00:00':0 for day in (16,17) for h in range(24)}
    site=fs.FluxSite(35.,10000,10000,4000,10000,True,efficiency=1.)
    data=dict(now=instant(hour),local_tz=TZ,
        bands=fs.FluxBands(15.,25.,35.,4.,10.,28.,instant(23)),
        site=site,soc_pct=90.,house=fs.HalfHourProfile([.25]*48,TZ),
        pv=fs.HourlyPvForecast(buckets,TZ),flows=fs.FluxFlows(0.,500.,500.),
        tariff_verified=True,commissioned=True,enabled=True,
        rates_age_s=1.,forecast_age_s=1.,telemetry_age_s=1.,flows_age_s=1.,profile_age_s=1.)
    data.update(changes)
    return fs.FluxInputs(**data)


class FluxIndependentReviewTests(unittest.TestCase):
    def test_partial_house_interval_crosses_real_slot_boundary(self):
        slots=[0.]*48
        slots[32]=2.  # 4 kW from 16:00 to 16:30
        profile=fs.HalfHourProfile(slots,TZ)
        self.assertAlmostEqual(profile.kwh_between(instant(15,55),instant(16,25)),5./3.)

    def test_partial_solar_interval_crosses_real_hour_boundary(self):
        pv=fs.HourlyPvForecast({'2026-09-16 15:00:00':0,
                                 '2026-09-16 16:00:00':6000},TZ)
        self.assertAlmostEqual(pv.kwh_between(instant(15,55),instant(16,25)),2.5)

    def test_overlapping_rewards_do_not_double_physical_export(self):
        a=fs.EventCommitment('axle','export',instant(18),instant(19),4.)
        b=replace(a,source='octopus')
        one=fs.plan(scenario(commitments=(a,)))
        both=fs.plan(scenario(commitments=(a,b)))
        self.assertEqual(both.committed_kwh,4.)
        self.assertEqual(both.protect_soc_pct,one.protect_soc_pct)

    def test_back_to_back_events_are_both_reserved(self):
        a=fs.EventCommitment('axle','export',instant(18),instant(19),4.)
        b=fs.EventCommitment('octopus','export',instant(19),instant(20),4.)
        plain=fs.plan(scenario())
        event_day=fs.plan(scenario(commitments=(a,b)))
        self.assertEqual(event_day.committed_kwh,8.)
        self.assertGreaterEqual(event_day.protect_soc_pct-plain.protect_soc_pct,22.)
        self.assertLess(event_day.planned_kwh,plain.planned_kwh)

    def test_late_event_removes_discretionary_export_and_cancellation_restores_it(self):
        original=scenario(soc_pct=45.)
        a=fs.EventCommitment('axle','export',instant(19),instant(20),4.)
        before=fs.plan(original)
        after=fs.plan(replace(original,commitments=(a,)))
        cancelled=fs.plan(original)
        self.assertEqual(before.mode,fs.MODE_EXPORT)
        self.assertNotEqual(after.mode,fs.MODE_EXPORT)
        self.assertEqual(cancelled.control_key(),before.control_key())

    def test_household_can_consume_evening_budget_above_reserve(self):
        decision=fs.plan(scenario(soc_pct=32.))
        self.assertEqual(decision.mode,fs.MODE_SUPPLY_HOUSE)
        self.assertEqual(decision.discharge_cutoff_pct,20.)

    def test_solar_replenishment_cannot_exceed_charge_power(self):
        values=[.0001]*48
        values[40]=4.
        values[41]=4.
        i=scenario(hour=5,house=fs.HalfHourProfile(values,TZ))
        i=replace(i,site=replace(i.site,charge_power_w=1000),
            pv=fs.HourlyPvForecast({f'2026-09-16 {h:02d}:00:00':
                                     (12000 if h==12 else 0) for h in range(24)},TZ))
        need,_=fs.required_start_kwh(i,instant(5),instant(23),7.)
        # Only 1 kWh can enter the battery at noon; 8 kWh is needed that evening.
        self.assertGreaterEqual(need,14.)

    def test_zoneinfo_and_plugin_pytz_fallback_resolve_same_flux_boundary(self):
        import pytz
        for stamp in ('2026-09-16T00:00:00+00:00',
                      '2026-10-25T00:30:00+00:00',
                      '2027-03-28T00:30:00+00:00'):
            now=datetime.fromisoformat(stamp)
            self.assertEqual(fs.next_cheap_start(now,TZ),
                             fs.next_cheap_start(now,pytz.timezone('Europe/London')))

    def test_grid_supplies_cheap_window_house_without_double_battery_purchase(self):
        i=scenario(hour=2,soc_pct=20.,house=fs.HalfHourProfile([.5]*48,TZ))
        i=replace(i,bands=replace(i.bands,export_peak_p=15.))  # no optional trade
        decision=fs.plan(i)
        # From 05:00 to 02:00: 21 kWh house + 7 kWh reserve = 28 kWh / 35.
        self.assertEqual(decision.charge_cutoff_pct,80.)
        self.assertAlmostEqual(decision.planned_kwh,21.,places=1)

    def test_short_cheap_window_reports_unreachable_household_energy(self):
        i=scenario(hour=4,soc_pct=20.)
        i=replace(i,now=instant(4,55),site=replace(i.site,charge_power_w=1000),
                  bands=replace(i.bands,export_peak_p=15.))
        decision=fs.plan(i)
        self.assertGreater(decision.infeasible_kwh,5.)

    def test_invalid_reserve_is_a_defer_not_an_exception(self):
        for value in (float('nan'),float('inf'),True):
            with self.subTest(value=value):
                i=scenario()
                decision=fs.plan(replace(i,site=replace(i.site,reserve_pct=value)))
                self.assertTrue(decision.deferred)

    def test_conflicting_contained_price_span_is_not_a_valid_flux_schedule(self):
        def spans(prices):
            edges=(0,2,5,16,19,24)
            return [fs.RateSpan(instant(0)+timedelta(hours=edges[n]),
                               instant(0)+timedelta(hours=edges[n+1]),price)
                    for n,price in enumerate(prices)]
        imports=spans((25.,15.,25.,35.,25.))
        exports=spans((10.,4.,10.,28.,10.))
        imports.insert(0,fs.RateSpan(instant(0),instant(0)+timedelta(days=1),25.))
        self.assertIsNone(fs.derive_bands(imports,exports,TZ,instant(2)))

    def test_peak_solar_needed_by_house_is_actually_allowed_to_charge(self):
        """Replay actual mode/limit effects; a forecast alone does not store PV."""
        i=scenario(hour=16,soc_pct=30.,house=fs.HalfHourProfile([.5]*48,TZ))
        buckets={f'2026-09-{day} {h:02d}:00:00':
                 (4000 if day==16 and h in (16,17) else 0)
                 for day in (16,17) for h in range(24)}
        end=instant(2)+timedelta(days=1)
        i=replace(i,pv=fs.HourlyPvForecast(buckets,TZ),
                  bands=replace(i.bands,covers_until=end))
        energy=10.5
        imports=0.
        cursor=i.now
        while cursor<end:
            stop=min(cursor+timedelta(minutes=5),end)
            hours=(stop-cursor).total_seconds()/3600.
            house=i.house.kwh_between(cursor,stop)
            pv=i.pv.kwh_between(cursor,stop)
            live=replace(i,now=cursor,soc_pct=energy/35.*100.,
                         flows=fs.FluxFlows(pv/hours*1000.,house/hours*1000.,0.))
            decision=fs.plan(live)
            floor=decision.discharge_cutoff_pct/100.*35. if decision.owns else 7.
            charge_w=decision.charge_limit_w if decision.owns else 10000
            discharge_w=decision.discharge_limit_w if decision.owns else 10000
            top=decision.charge_cutoff_pct/100.*35. if decision.owns else 35.
            short=max(0.,house-pv)
            surplus=max(0.,pv-house)
            allowed=min(max(0.,energy-floor),discharge_w/1000.*hours)
            supplied=min(short,allowed)
            energy-=supplied
            imports+=short-supplied
            if decision.owns and decision.ems_mode==5:
                # The inverter's grid export cap (40038) is enforced in hardware:
                # PV surplus goes first and the battery only tops the meter up to it.
                cap_kwh=i.site.export_limit_w/1000.*hours
                energy-=min(max(0.,allowed-supplied),max(0.,cap_kwh-surplus))
            energy+=min(surplus,charge_w/1000.*hours,max(0.,top-energy))
            cursor=stop
        # Initial 10.5 + PV 8 - demand 10 leaves 8.5 kWh, above the 7 kWh
        # reserve. Exporting the solar that this plan needs must not create import.
        self.assertLess(imports,.15)
        self.assertGreaterEqual(energy,7.-1e-6)


class FluxEventIntegrationReviewTests(unittest.TestCase):
    def test_supervisor_target_can_survive_realistic_staged_command_latency(self):
        from test_flux_supervisor import _mk_plugin
        from test_flux_execution import Registers
        from flux_execution import FluxExecutor
        from unittest.mock import patch
        from pathlib import Path
        import tempfile
        import plugin
        clock=[instant(2)]
        class Clock(datetime):
            @classmethod
            def now(cls,tz=None):
                return clock[0].astimezone(tz) if tz else clock[0].replace(tzinfo=None)
        raw=Registers()
        with tempfile.TemporaryDirectory() as folder:
            ex=FluxExecutor(raw,Path(folder)/'claim.json',baseline_charge_w=10000,
                baseline_discharge_w=10000,baseline_discharge_cutoff_pct=20.,
                clock=lambda:clock[0])
            ex.step(None,clock[0])
            p=_mk_plugin()
            decision=fs.FluxDecision(mode=fs.MODE_CHARGE,owns=True,reason='test',
                decision_at=clock[0],decision_until=clock[0]+timedelta(minutes=25),
                ems_mode=3,charge_limit_w=2000,discharge_limit_w=0,
                charge_cutoff_pct=80.,discharge_cutoff_pct=20.)
            with patch.object(plugin,'datetime',Clock):
                target=p._flux_target(decision,clock[0].timestamp())
            self.assertIsNotNone(target)
            def advance(_key,_value):
                clock[0]+=timedelta(seconds=3)
            raw.before_write=advance
            self.assertEqual(ex.step(target,clock[0]),'applied')

    def test_already_active_axle_is_not_overwritten_by_a_baseline_restore(self):
        from test_flux_supervisor import _mk_plugin, _FakeExecutor
        executor=_FakeExecutor(owns=True)
        p=_mk_plugin(executor=executor)
        p.store['vpp_state']='active'
        p._flux_supervisor_step()
        self.assertTrue(executor.calls)
        self.assertTrue(all(c['supervisor_owns'] for c in executor.calls))

    def test_disabled_recovery_does_not_write_over_an_active_axle_owner(self):
        from test_flux_supervisor import _mk_plugin, _FakeExecutor
        executor=_FakeExecutor(owns=True)
        p=_mk_plugin(executor=executor)
        p.pluginPrefs['fluxEnabled']=False
        p.store['vpp_state']='active'
        p._flux_recover_while_disabled()
        self.assertTrue(executor.calls)
        self.assertTrue(all(c['supervisor_owns'] for c in executor.calls))

    def test_disabled_recovery_retains_owner_across_ticks_and_reconciles_afterwards(self):
        from test_flux_supervisor import _mk_plugin
        from test_flux_execution import Registers
        from flux_execution import FluxExecutor
        from pathlib import Path
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            raw=Registers()
            ex=FluxExecutor(raw,Path(folder)/'flux_claim.json',baseline_charge_w=10000,
                baseline_discharge_w=10000,baseline_discharge_cutoff_pct=20.)
            p=_mk_plugin(executor=ex,data_dir=folder)
            p.pluginPrefs['fluxEnabled']=False
            p.store['vpp_state']='active'
            for _ in range(3):
                p._flux_recover_while_disabled()
                self.assertIs(p.flux_executor,ex)
                self.assertTrue(ex.path.exists())
                self.assertEqual(raw.writes,[])
            p.store['vpp_state']='idle'
            p._flux_recover_while_disabled()
            self.assertTrue(raw.writes)
            self.assertEqual(raw.values['mode'],2)
            self.assertIsNone(p.flux_executor)
            self.assertFalse(ex.path.exists())

    def test_announced_axle_does_not_block_the_charge_that_funds_it(self):
        from test_flux_supervisor import _mk_plugin
        p=_mk_plugin()
        p.store['vpp_state']='announced'
        self.assertEqual(p._flux_other_owner(),'')

    def test_active_axle_can_spend_its_own_reserved_energy(self):
        from test_flux_supervisor import _mk_plugin
        import plugin
        # The real path sets the event's hardware cutoff during precharge, before
        # its start; merely excluding events already running leaves that old
        # cutoff in force for the entire dispatch.
        for state,lead in ((plugin.VPP_ACTIVE,-5),(plugin.VPP_PRE_CHARGING,30)):
            with self.subTest(state=state):
                p=_mk_plugin()
                now=datetime.now(timezone.utc)
                event={'start_time':now+timedelta(minutes=lead),
                       'end_time':now+timedelta(minutes=lead+60),'import_export':'export'}
                p.store.update(vpp_state=state,vpp_event=event)
                p._dawn_target_pct=lambda:20.
                p._set_vpp_discharge_cutoff(event,is_daytime=True)
                # The reserve now lives on the backup register (40046); the
                # absolute cutoff (40048) stays at the health floor by day.
                cutoffs=[v[1] for v in p.modbus.writes if isinstance(v,tuple)
                         and v[0]=='backup_soc']
                self.assertTrue(cutoffs)
                self.assertEqual(cutoffs[-1],20.)
                self.assertIn(('discharge_cutoff',1.),p.modbus.writes)

    def test_axle_precharge_does_not_release_the_later_octopus_allocation(self):
        from test_flux_supervisor import _mk_plugin
        from unittest.mock import patch
        import plugin
        p=_mk_plugin()
        class Clock(datetime):
            @classmethod
            def now(cls,tz=None):
                return instant(17,30).astimezone(tz or timezone.utc)
        event={'start_time':instant(18),'end_time':instant(19),'import_export':'export'}
        p.store.update(vpp_state=plugin.VPP_PRE_CHARGING,vpp_event=event,
            saving_sessions_windows=[{'id':'following-event',
                'start':instant(19).isoformat(),'end':instant(20).isoformat(),
                'direction':plugin.SAVING_SESSION_TURN_DOWN}])
        p._dawn_target_pct=lambda:20.
        with patch.object(plugin,'datetime',Clock):
            p._set_vpp_discharge_cutoff(event,is_daytime=True)
        cutoffs=[v[1] for v in p.modbus.writes if isinstance(v,tuple)
                 and v[0]=='backup_soc']
        # Release Axle's own 4 kWh, preserve the subsequent 4 kWh plus losses.
        self.assertGreater(cutoffs[-1],31.)
        self.assertLess(cutoffs[-1],33.)

    def test_actual_announced_precharge_can_export_and_reports_real_shortfall(self):
        from test_flux_supervisor import _mk_plugin, _pinned_clock
        from unittest.mock import MagicMock
        import plugin
        p=_mk_plugin(soc=35.)
        with _pinned_clock("2026-09-16 17:30") as now:
            event={'id':'axle-1','start_time':now+timedelta(minutes=30),
                   'end_time':now+timedelta(minutes=90),'import_export':'export',
                   'duration_hrs':1.}
            p.store.update(vpp_state=plugin.VPP_ANNOUNCED,vpp_event=event,
                saving_sessions_windows=[{'id':'later','start':(now+timedelta(minutes=90)).isoformat(),
                    'end':(now+timedelta(minutes=150)).isoformat(),
                    'direction':plugin.SAVING_SESSION_TURN_DOWN}])
            p._dawn_target_pct=lambda:20.
            p._event_is_daytime=lambda _:True
            p._alert_vpp_shortfall=MagicMock()
            p._start_vpp_precharge(event)
            floors=[x[1] for x in p.modbus.writes if isinstance(x,tuple) and x[0]=='backup_soc']
            self.assertGreater(floors[0],31.)
            self.assertLess(floors[0],33.)
            self.assertEqual(p.store['vpp_state'],plugin.VPP_PRE_CHARGING)
            p._alert_vpp_shortfall.assert_called_once()
            self.assertGreater(p._alert_vpp_shortfall.call_args.args[3],15.)

    def test_flux_release_precedes_axle_cutoff_and_does_not_overwrite_it(self):
        from test_flux_supervisor import _mk_plugin, _FakeExecutor, _pinned_clock
        from unittest.mock import MagicMock
        import plugin
        p=_mk_plugin(soc=90.)
        order=[]
        class Watch(_FakeExecutor):
            def step(self, target, now, **kw):
                order.append('release')
                return super().step(target,now,**kw)
        p.flux_executor=Watch(owns=True)
        old=p.modbus.set_discharge_cutoff
        def cutoff(value):
            order.append('cutoff')
            return old(value)
        p.modbus.set_discharge_cutoff=cutoff
        p._dawn_target_pct=lambda:20.
        p._event_is_daytime=lambda _:True
        p._alert_vpp_shortfall=MagicMock()
        with _pinned_clock("2026-09-16 17:30") as now:
            event={'start_time':now+timedelta(minutes=30),
                   'end_time':now+timedelta(minutes=90),'import_export':'export'}
            p.store.update(vpp_state=plugin.VPP_ANNOUNCED,vpp_event=event)
            p._start_vpp_precharge(event)
        self.assertEqual(order,['release','cutoff'])

    def test_overlapping_later_session_only_reserves_its_tail(self):
        from test_flux_supervisor import _mk_plugin, _pinned_clock
        import plugin
        p=_mk_plugin()
        with _pinned_clock("2026-09-16 17:30") as now:
            event={'id':'axle-1','start_time':now+timedelta(minutes=30),
                   'end_time':now+timedelta(minutes=90),'import_export':'export'}
            p.store.update(vpp_state=plugin.VPP_ANNOUNCED,vpp_event=event,
                saving_sessions_windows=[{'id':'overlap','start':(now+timedelta(minutes=60)).isoformat(),
                    'end':(now+timedelta(minutes=120)).isoformat(),
                    'direction':plugin.SAVING_SESSION_TURN_DOWN}])
            floor=p._policy_discharge_floor_pct(dispatch_event=event)
            self.assertGreater(floor,25.)
            self.assertLess(floor,27.)  # Only the 19:00-19:30 tail, 2 kWh.

    def test_same_day_cached_forecast_keeps_its_generation_age(self):
        from test_flux_supervisor import TestForecastFreshnessStamp
        import plugin
        import time
        today=plugin._london_today()
        p=TestForecastFreshnessStamp()._plugin({'forecastStatus':'OK',
            '_hourly_p50_today':{f'{today:%Y-%m-%d} 12:00:00':1000.},
            'correctedTomorrowKwh':10.,'correctedTodayKwh':5.})
        old=time.time()-7200
        p.forecast._cached_time=old
        p._refresh_forecast()
        self.assertEqual(p.store['forecast_ok_at'],old)


class FluxAccountProofReviewTests(unittest.TestCase):
    def client(self, points):
        from octopus_api import OctopusAPI
        from unittest.mock import MagicMock
        api=OctopusAPI.__new__(OctopusAPI)
        api.api_key='fixture'
        api.account_id='fixture'
        api.export_mpan='this-export'
        api._rates_cache={}
        api.logger=MagicMock()
        api._api_get=lambda *a,**kw:{'properties':[{'electricity_meter_points':points}]}
        return api

    def test_expired_or_future_agreement_is_not_active_flux_proof(self):
        now=datetime.now(timezone.utc)
        for start,end in ((now-timedelta(days=10),now-timedelta(days=1)),
                          (now+timedelta(days=1),None)):
            agreement={'tariff_code':'E-1R-FLUX-EXPORT-23-02-14-F',
                       'valid_from':start.isoformat(),
                       'valid_to':end.isoformat() if end else None}
            api=self.client([{'mpan':'this-export','is_export':True,
                              'agreements':[agreement]}])
            self.assertEqual(api.get_export_agreement(force=True),{})

    def test_another_property_export_meter_cannot_prove_this_sites_flux(self):
        agreement={'tariff_code':'E-1R-FLUX-EXPORT-23-02-14-F',
                   'valid_from':(datetime.now(timezone.utc)-timedelta(days=1)).isoformat(),
                   'valid_to':None}
        api=self.client([{'mpan':'other-export','is_export':True,
                          'agreements':[agreement]}])
        self.assertEqual(api.get_export_agreement(force=True),{})

    def test_ambiguous_or_undated_agreements_are_not_proof(self):
        from octopus_api import OctopusAPI
        ag={'tariff_code':'E-1R-FLUX-EXPORT-23-02-14-F',
            'valid_from':(datetime.now(timezone.utc)-timedelta(days=1)).isoformat()}
        self.assertIsNone(OctopusAPI._live_agreement([ag,dict(ag)]))
        self.assertIsNone(OctopusAPI._live_agreement([{'tariff_code':ag['tariff_code']}]))

    def test_agreement_expiry_is_checked_inside_account_cache_lifetime(self):
        from test_flux_supervisor import _mk_plugin, _evidence
        p=_mk_plugin()
        evidence=_evidence()
        evidence['export_valid_to']=(datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()
        p.store['flux_account_evidence']=evidence
        self.assertFalse(p._flux_tariff_verified()[0])

    def test_nonfinite_account_stamp_cannot_arm_flux(self):
        from test_flux_supervisor import _mk_plugin, _evidence
        p=_mk_plugin()
        for bad in (float('nan'),float('inf'),True,'bad'):
            p.store['flux_account_evidence']=_evidence(fetched_at=bad)
            self.assertFalse(p._flux_tariff_verified()[0])


if __name__ == '__main__':
    unittest.main()
