import sys as _sys
from pathlib import Path as _Path
SKILL = _Path(__file__).resolve().parents[2] / 'skills' / 'gpu-training-lifecycle'   # the skill under test
_sys.path.insert(0, str(SKILL / 'scripts'))
import copy
import json
import unittest
from pathlib import Path
from validate_experiment import validate

class ContractTests(unittest.TestCase):
    def setUp(self):
        self.plan=json.loads((SKILL / 'assets/experiment.example.json').read_text())
        self.ready=copy.deepcopy(self.plan)
        self.ready.update(provider='example-cloud',region='region-a',state='ready',purchase_model='spot')
        self.ready['gpu']['model']='example-gpu'
        self.ready['image']={'reference':'registry.example/train:v1','digest':'sha256:'+'a'*64}
        self.ready['dataset']={'uri':'s3://example/data','version':'v1'}
        self.ready['checkpoint'].update(uri='s3://example/checkpoints/research-run-001',provider='example-cloud',region='region-a')
        self.ready['monitoring']['wandb'].update(enabled=True,entity='team',project='training')
        self.ready['controller']={'mode':'remote-api-server','state_uri':'protected-volume://controller'}
        self.ready['budget'].update(total_limit=100,max_runtime_hours=12,controller_allowance=4,
                                    deadline_utc='2026-10-03T12:00:00Z',ceiling_hourly_usd=4.57)
        self.ready['monitoring']['notify']={'channel':'email'}
        self.ready['code']={'repo':'https://github.com/org/train','commit':'b'*40}
    def test_plan(self): self.assertEqual(validate(self.plan),[])
    def test_optional_keys_may_be_absent(self):
        c=copy.deepcopy(self.ready); del c['budget']['max_runtime_hours']
        self.assertEqual(validate(c,'launch'),[])      # Standard: the budget, not a runtime, bounds time
        self.assertIsNone(self.plan['dataset']['qualify_uri'])   # the example shows the field
        c=copy.deepcopy(self.ready); del c['budget']['total_limit']
        self.assertIn('missing budget.total_limit',validate(c,'launch'))
    def test_launch_requires_values(self): self.assertTrue(validate(self.plan,'launch'))
    def test_ready_contract(self): self.assertEqual(validate(self.ready,'launch'),[])
    def test_pending_is_not_authority(self):
        self.assertEqual(self.ready['approval']['status'],'pending')
        self.assertEqual(validate(self.ready,'launch'),[]) # validates structure, does NOT authorize
    def multi_region(self, **evidence):
        d=copy.deepcopy(self.ready)
        ev={'shared_storage':True,'cross_region_transfer_cost_per_gib':0,
            'covers':['checkpoint','dataset','image'],'checked_at':'2026-10-02','source':'provider docs'}
        ev.update(evidence)
        d['recovery'].update(allow_region_change=True,allowed_regions=['region-a','region-b'],region_change_evidence=ev)
        return d
    def test_region_policy_accepts_free_shared_storage(self):
        self.assertEqual(validate(self.multi_region(),'launch'),[])
    def test_region_policy_rejects_paid_or_unshared_storage(self):
        for bad in ({'cross_region_transfer_cost_per_gib':0.015},{'cross_region_transfer_cost_per_gib':False},
                    {'shared_storage':False},{'covers':['checkpoint','dataset']},{'source':' '}):
            with self.subTest(bad=bad):
                self.assertTrue(validate(self.multi_region(**bad),'launch'))
    def test_region_policy_needs_primary_and_two_regions(self):
        d=self.multi_region(); d['recovery']['allowed_regions']=['region-a']
        self.assertTrue(validate(d,'launch'))
        d=self.multi_region(); d['recovery']['allowed_regions']=['region-b','region-c']
        self.assertTrue(validate(d,'launch'))
    def test_allowed_regions_without_opt_in_rejected(self):
        d=copy.deepcopy(self.ready); d['recovery']['allowed_regions']=['region-a','region-b']
        self.assertTrue(validate(d,'launch'))
    def test_locked_multi_region_allows_listed_region_only(self):
        locked=self.multi_region()
        moved=copy.deepcopy(locked); moved['region']='region-b'; moved['checkpoint']=locked['checkpoint']
        self.assertEqual(validate(moved,'launch',locked),[])
        moved['region']='region-z'
        self.assertIn('region outside locked allowed_regions',validate(moved,'launch',locked))
    def test_wandb_history_policy_is_append_or_rewind(self):
        for policy in ('append','rewind'):
            d=copy.deepcopy(self.ready); d['monitoring']['wandb']['history_policy']=policy
            self.assertEqual(validate(d,'launch'),[])
        d=copy.deepcopy(self.ready); d['monitoring']['wandb']['history_policy']='overwrite'
        self.assertTrue(validate(d,'launch'))
    def test_app_restarts_nonnegative(self):
        for ok in (0,3,5):
            d=copy.deepcopy(self.ready); d['recovery']['max_app_restarts']=ok
            self.assertEqual(validate(d,'launch'),[])
        for bad in (-1,True,'1'):
            d=copy.deepcopy(self.ready); d['recovery']['max_app_restarts']=bad
            self.assertTrue(validate(d,'launch'))
    def test_launch_requires_guard_inputs(self):
        for path,bad in (('deadline_utc','2026-10-03 12:00'),('ceiling_hourly_usd',0),('ceiling_hourly_usd',True)):
            d=copy.deepcopy(self.ready); d['budget'][path]=bad
            self.assertTrue(validate(d,'launch'),path)
        d=copy.deepcopy(self.ready); del d['monitoring']['notify']
        self.assertTrue(any('notify' in e for e in validate(d,'launch')))
        self.assertEqual(validate(self.plan),[])   # planning stays permissive
    def test_on_demand_fallback_only_when_approved(self):
        d=copy.deepcopy(self.ready); d['recovery']['allow_on_demand']=True
        self.assertTrue(validate(d,'launch'))
        d['recovery']['on_demand_fallback']={'approved':True,'after_capacity_wait_minutes':5}
        self.assertEqual(validate(d,'launch'),[])
        d['recovery']['on_demand_fallback']['approved']=False
        self.assertTrue(validate(d,'launch'))
        d=copy.deepcopy(self.ready); d['recovery']['on_demand_fallback']={'approved':True,'after_capacity_wait_minutes':5}
        self.assertTrue(validate(d,'launch'))   # object without allow_on_demand
    def test_example_forces_an_explicit_metrics_choice(self):
        d=copy.deepcopy(self.ready); d['monitoring']['wandb']['enabled']=False
        self.assertTrue(any('metrics choice' in e for e in validate(d,'launch')))
        self.assertIsNone(self.plan['monitoring']['metrics_backend'])
    def minimal(self):
        d=copy.deepcopy(self.ready); d.update(tier='minimal',purchase_model='on-demand')
        d['recovery'].update(enabled=False,max_attempts=0); del d['recovery']['max_capacity_wait_minutes']
        d['controller']={'mode':None,'state_uri':None}; del d['monitoring']['notify']
        d['budget'].update(total_limit=10,max_runtime_hours=1.5,controller_allowance=0,ceiling_hourly_usd=4.57,
                           max_worker_runtime_seconds=3600,autodown_idle_minutes=10)
        return d
    def test_minimal_tier_needs_no_controller_or_guard(self):
        self.assertEqual(validate(self.minimal(),'launch'),[])
    def test_minimal_tier_proves_its_cost_bound(self):
        cases={'spot':lambda d:d.update(purchase_model='spot'),
               'managed':lambda d:d['recovery'].update(enabled=True,max_attempts=1),
               'no timeout':lambda d:d['budget'].pop('max_worker_runtime_seconds'),
               'float timeout':lambda d:d['budget'].update(max_worker_runtime_seconds=3600.5),
               'no autodown':lambda d:d['budget'].pop('autodown_idle_minutes'),
               'runtime too short':lambda d:d['budget'].update(max_runtime_hours=1.0),
               'over budget':lambda d:d['budget'].update(total_limit=6)}
        for name,change in cases.items():
            d=self.minimal(); change(d)
            with self.subTest(name): self.assertTrue(validate(d,'launch'))
    def test_standard_tier_is_managed(self):
        d=copy.deepcopy(self.ready); d['tier']='standard'; self.assertEqual(validate(d,'launch'),[])
        d['recovery']['enabled']=False; self.assertTrue(validate(d,'launch'))
        d=copy.deepcopy(self.ready); d['tier']='tiny'; self.assertTrue(validate(d,'plan'))
        d=copy.deepcopy(self.ready); d['controller']={'mode':None,'state_uri':None}
        self.assertTrue(validate(d,'launch'))        # absent tier means standard
    def test_tier_is_locked(self):
        d=copy.deepcopy(self.ready); d['tier']='standard'
        self.assertIn('locked field changed: tier',validate(d,'launch',self.ready))
    def test_fallback_wait_within_max_wait(self):
        d=copy.deepcopy(self.ready); d['recovery'].update(allow_on_demand=True,
            on_demand_fallback={'approved':True,'after_capacity_wait_minutes':d['recovery']['max_capacity_wait_minutes']})
        self.assertTrue(any('must be less than' in e for e in validate(d,'launch')))
    def test_mount_modes(self):
        d=copy.deepcopy(self.ready); d['checkpoint']['mount_mode']='MOUNT'; d['dataset']['mount_mode']='COPY'
        self.assertEqual(validate(d,'launch'),[])
        for field,bad in (('checkpoint','COPY'),('dataset','SYNC')):
            d=copy.deepcopy(self.ready); d[field]['mount_mode']=bad
            with self.subTest(field=field): self.assertTrue(validate(d,'launch'))
    def test_on_demand_run_needs_no_capacity_wait(self):
        d=copy.deepcopy(self.ready); d['purchase_model']='on-demand'; del d['recovery']['max_capacity_wait_minutes']
        self.assertEqual(validate(d,'launch'),[])
        d['purchase_model']='spot'
        self.assertTrue(validate(d,'launch'))
    def test_metrics_tooling_is_the_researchers_choice(self):
        for backend in ('mlflow','tensorboard','none'):
            d=copy.deepcopy(self.ready); d['monitoring']['wandb']['enabled']=False; d['monitoring']['metrics_backend']=backend
            self.assertEqual(validate(d,'launch'),[],backend)
            d=copy.deepcopy(self.ready); del d['monitoring']['wandb']; d['monitoring']['metrics_backend']=backend
            self.assertEqual(validate(d,'launch'),[],backend)
        d=copy.deepcopy(self.ready); del d['monitoring']['wandb']; d['monitoring']['metrics_backend']=' '
        self.assertTrue(validate(d,'launch'))
    def test_provider_lock(self):
        changed=copy.deepcopy(self.ready); changed['provider']='other'
        self.assertTrue(validate(changed,'launch',self.ready))
    def test_purchase_lock(self):
        changed=copy.deepcopy(self.ready); changed['purchase_model']='on-demand'
        self.assertTrue(validate(changed,'launch',self.ready))
    def test_region_lock(self):
        changed=copy.deepcopy(self.ready); changed['region']='other'
        self.assertTrue(validate(changed,'launch',self.ready))
    def test_expanded_recovery(self):
        changed=copy.deepcopy(self.ready); changed['recovery']['allow_on_demand']=True
        self.assertTrue(validate(changed,'launch',self.ready))
    def test_bad_numbers(self):
        for value in (float('nan'),float('inf'),-1,True):
            d=copy.deepcopy(self.ready); d['budget']['total_limit']=value
            self.assertTrue(validate(d,'launch'))
    def test_checkpoint_identity(self):
        self.ready['checkpoint']['run_id']='other'; self.assertTrue(validate(self.ready))
    def test_extra_fields_preserved(self):
        self.ready['notes']={'framework':'hf'}; self.assertEqual(validate(self.ready),[])
    def test_bad_shape(self): self.assertTrue(validate([]))
    def test_malformed_nested(self):
        for key in ('gpu','image','dataset','checkpoint','budget','recovery','controller','approval'):
            d=copy.deepcopy(self.ready); d[key]=[]; self.assertTrue(validate(d,'launch'))
    def test_out_of_scope_gpu(self):
        self.ready['gpu']['count']=16; self.assertTrue(validate(self.ready))

    def test_legacy_contract(self):
        d=copy.deepcopy(self.ready); d.pop('monitoring')
        d['checkpoint']={k:v for k,v in d['checkpoint'].items() if k in ('uri','run_id')}
        self.assertEqual(validate(d,'plan'),[])
        self.assertTrue(validate(d,'launch'))
    def test_checkpoint_extensions(self):
        for key,value in [('provider','other'),('region',None),('durable',False),('storage_type','ephemeral'),('resume_policy','fresh')]:
            d=copy.deepcopy(self.ready);d['checkpoint'][key]=value
            self.assertTrue(validate(d,'launch'),key)
    def test_monitoring_config(self):
        for key,value in [('enabled','true'),('entity',None),('project',''),('mode','invalid'),('api_key','secret')]:
            d=copy.deepcopy(self.ready);d['monitoring']['wandb'][key]=value
            self.assertTrue(validate(d,'launch'),key)
    def test_monitoring_identity_locked(self):
        d=copy.deepcopy(self.ready);d['monitoring']['wandb']['run_id']='other'
        self.assertTrue(validate(d,'launch',self.ready))
    def test_monitoring_bad_shape(self):
        for value in (None,[],{'wandb':[]}):
            d=copy.deepcopy(self.ready);d['monitoring']=value
            self.assertTrue(validate(d,'launch'))

    def test_no_unlocked_on_demand_fallback(self):
        self.ready['recovery']['allow_on_demand']=True
        self.assertTrue(validate(self.ready,'launch'))
    def test_capacity_wait_bound(self):
        for value in (0,-1,True,float('inf'),'10'):
            self.ready['recovery']['max_capacity_wait_minutes']=value
            self.assertTrue(validate(self.ready,'launch'))

    def test_launch_requires_storage_extensions(self):
        for key in ('provider','region','durable','storage_type','resume_policy'):
            d=copy.deepcopy(self.ready);d['checkpoint'].pop(key)
            self.assertTrue(validate(d,'launch'),key)
    def test_launch_requires_monitoring(self):
        for change in ('remove','disabled','empty'):
            d=copy.deepcopy(self.ready)
            if change=='remove': d.pop('monitoring')
            elif change=='disabled': d['monitoring']['wandb']['enabled']=False
            else:d['monitoring']['wandb']={}
            self.assertTrue(validate(d,'launch'),change)
    def test_launch_requires_capacity_wait(self):
        d=copy.deepcopy(self.ready);d['recovery'].pop('max_capacity_wait_minutes')
        self.assertTrue(validate(d,'launch'))
    def test_enabled_recovery_requires_attempt(self):
        d=copy.deepcopy(self.ready);d['recovery']['max_attempts']=0
        self.assertTrue(validate(d,'launch'))

    def test_optional_instance_type(self):
        self.ready['gpu']['instance_type']='verified-type_2gpu'
        self.assertEqual(validate(self.ready,'launch'),[])
        changed=copy.deepcopy(self.ready);changed['gpu']['instance_type']='different-type'
        self.assertTrue(validate(changed,'launch',self.ready))
        for value in (None,'','*',True,['type']):
            self.ready['gpu']['instance_type']=value
            self.assertTrue(validate(self.ready,'launch'))
    def test_optional_worker_runtime(self):
        self.ready['budget']['max_worker_runtime_seconds']=3600
        self.assertEqual(validate(self.ready,'launch'),[])
        for value in (None,0,-1,True,float('inf')):
            self.ready['budget']['max_worker_runtime_seconds']=value
            self.assertTrue(validate(self.ready,'launch'))

    def test_code_location_required_at_launch(self):
        d=copy.deepcopy(self.ready); del d['code']
        self.assertIn('launch requires code {repo, commit}: where the training code lives and the exact commit in the image',validate(d,'launch'))
        d=copy.deepcopy(self.ready); d['code']['commit']='main'
        self.assertTrue(validate(d,'launch'))
        self.assertEqual(validate(self.plan),[])                     # planning may leave it open
    def test_hf_dataset_and_checkpoint_storage(self):
        d=copy.deepcopy(self.ready); d['dataset']={'uri':'hf://datasets/org/name@'+'c'*40,'version':'c'*40,'mount_mode':'MOUNT'}
        self.assertEqual(validate(d,'launch'),[])
        d['dataset']['uri']='hf://datasets/org/name'
        self.assertTrue(validate(d,'launch'))
        d=copy.deepcopy(self.ready); d['checkpoint']['uri']='hf://datasets/org/ckpt@main'
        self.assertTrue(validate(d,'launch'))
    def test_attempt_limits(self):
        d=copy.deepcopy(self.ready); d['recovery']['max_attempts']=None
        self.assertEqual(validate(d,'launch'),[])                    # no absolute cap; no-progress limit applies
        d['recovery']['max_attempts_without_progress']=0
        self.assertTrue(validate(d,'launch'))
    def test_runway_must_cover_planned_job_time(self):
        d=copy.deepcopy(self.ready); d['budget'].update(total_limit=10,controller_allowance=0.5,ceiling_hourly_usd=4.57,planned_job_hours=2.5)
        self.assertTrue(any('budget runway' in e for e in validate(d,'launch')))
        d['budget']['total_limit']=15
        self.assertEqual(validate(d,'launch'),[])
    def test_standard_needs_no_max_runtime_hours(self):
        d=copy.deepcopy(self.ready); d['budget']['max_runtime_hours']=None
        self.assertEqual(validate(d,'launch'),[])
    def test_disk_and_billing_fields(self):
        d=copy.deepcopy(self.ready); d['disk']={'size_gb':500}; d['budget']['free_without_vm']=True
        self.assertEqual(validate(d,'launch'),[])
        for bad in ({'size_gb':'500'},{'size_gb':1}):
            d['disk']=bad
            self.assertTrue(validate(d,'launch'))
        d=copy.deepcopy(self.ready); d['budget']['free_without_vm']='yes'
        self.assertTrue(validate(d,'launch'))
    def test_spend_the_limits_cannot_see_is_flagged(self):
        from validate_experiment import warnings
        d=copy.deepcopy(self.ready); d['budget']['free_without_vm']=False
        self.assertIn('without the guard it is counted only at the next start',warnings(d)[0])
        d['monitoring']['guard']='external'
        self.assertEqual(warnings(d),[])
        d['monitoring']['guard']='none'; d['budget']['free_without_vm']=True
        self.assertEqual(warnings(d),[])
    def test_server_modes(self):
        for mode in ('self-hosted','existing','provider-managed','local','existing-host','remote-api-server','standalone-managed'):
            d=copy.deepcopy(self.ready); d['controller']['mode']=mode
            self.assertEqual(validate(d,'launch'),[],mode)
        d=copy.deepcopy(self.ready); d['controller']={'mode':'provider-managed','state_uri':None}
        self.assertEqual(validate(d,'launch'),[])                   # the provider keeps the server state
        d['controller']['mode']='self-hosted'
        self.assertTrue(any('controller.state_uri' in e for e in validate(d,'launch')))
        d['controller']['mode']='mars'
        self.assertTrue(any('controller.mode' in e for e in validate(d,'launch')))
    def test_endpoint_is_the_host_only(self):
        d=copy.deepcopy(self.ready); d['controller'].update(endpoint='skypilot.example.org:443',server_version='0.14.1')
        self.assertEqual(validate(d,'launch'),[])
        for bad in ('https://skypilot.example.org','user:pass@skypilot.example.org','host?token=x'):
            d['controller']['endpoint']=bad
            self.assertTrue(any('controller.endpoint' in e for e in validate(d,'launch')),bad)
    def test_a_server_upgrade_reruns_the_start_checks(self):
        old=copy.deepcopy(self.ready); old['controller']['server_version']='0.14.0'
        d=copy.deepcopy(old); d['controller']['server_version']='0.14.1'
        self.assertTrue(any('server_version changed' in e for e in validate(d,'launch',old)))
        self.assertEqual(validate(copy.deepcopy(old),'launch',old),[])
    def test_guard_and_notify_settings(self):
        d=copy.deepcopy(self.ready); d['monitoring']['guard']='external'; d['monitoring']['notify']={'channel':'ntfy','format':'slack'}
        self.assertEqual(validate(d,'launch'),[])
        for key,bad in (('guard','always'),):
            e=copy.deepcopy(d); e['monitoring'][key]=bad
            self.assertTrue(any('monitoring.guard' in x for x in validate(e,'launch')))
        e=copy.deepcopy(d); e['monitoring']['notify']['format']='sms'
        self.assertTrue(any('notify.format' in x for x in validate(e,'launch')))
        e=copy.deepcopy(d); e['monitoring']['notify']['url']='https://ntfy.sh/secret'
        self.assertTrue(any('NOTIFY_URL secret' in x for x in validate(e,'launch')))
    def test_fallback_without_the_guard_needs_no_timed_wait(self):
        d=copy.deepcopy(self.ready); d['recovery'].update(allow_on_demand=True,on_demand_fallback={'approved':True},max_capacity_wait_minutes=60)
        self.assertEqual(validate(d,'launch'),[])
        d['monitoring']['guard']='external'
        self.assertTrue(any('after_capacity_wait_minutes' in e for e in validate(d,'launch')))
        d['recovery']['on_demand_fallback']['after_capacity_wait_minutes']=30
        self.assertEqual(validate(d,'launch'),[])
    def test_spot_price_cap(self):
        d=copy.deepcopy(self.ready); d['budget']['spot_price_cap']={'per_gpu_hour':1.2,'cap_id':'pricingpolicy-e00abc'}
        self.assertEqual(validate(d,'launch'),[])
        for bad in ({'per_gpu_hour':0,'cap_id':'x'},{'per_gpu_hour':1.2},{'per_gpu_hour':1.2,'cap_id':'x','url':'y'}):
            e=copy.deepcopy(d); e['budget']['spot_price_cap']=bad
            self.assertTrue(any('spot_price_cap' in x for x in validate(e,'launch')),bad)
        e=copy.deepcopy(d); e['purchase_model']='on-demand'
        self.assertTrue(any('only to spot' in x for x in validate(e,'launch')))
    def test_watchdog_settings(self):
        d=copy.deepcopy(self.ready)
        d['watchdog']={'k':5,'limits':{'train':240,'eval':900,'save':600,'final':600,'startup':1800},'ceilings':{'final':7200},
                       'progress_period_seconds':300}
        self.assertEqual(validate(d,'launch'),[])
        d['watchdog']={'enabled':False}
        self.assertEqual(validate(d,'launch'),[])
    def test_watchdog_rejects_bad_settings(self):
        for bad in ({'k':0},{'limits':{'warmup':60}},{'limits':{'eval':True}},{'ceilings':[]},{'floor':60},{'enabled':'no'},
                    {'ceilings':{'startup':600}},{'ceilings':{'train':600}},
                    {'step_hook':True},[]):
            d=copy.deepcopy(self.ready); d['watchdog']=bad
            self.assertTrue(validate(d,'launch'),bad)
    def test_v08_watchdog_keys_name_the_new_ones(self):
        for old in ({'floor_seconds':180},{'seed_gap_seconds':2},{'budgets':{'save':120}}):
            d=copy.deepcopy(self.ready); d['watchdog']=old
            errors=validate(d,'launch')
            self.assertEqual(len(errors),1,errors)
            self.assertIn('v0.8 keys',errors[0]); self.assertIn('watchdog.limits',errors[0])
    def test_qualification_subset(self):
        d=copy.deepcopy(self.ready); d['dataset'].update(mount_mode='COPY',qualify_uri='s3://example/data/qualify')
        self.assertEqual(validate(d,'launch'),[])
        for bad in ('/local/subset','',5):
            d['dataset']['qualify_uri']=bad
            self.assertTrue(validate(d,'launch'),bad)
        d['dataset'].update(qualify_uri='s3://example/data/qualify',mount_mode=None)
        self.assertTrue(validate(d,'launch'))
    def test_checkpoint_mount_overrides(self):
        d=copy.deepcopy(self.ready); d['checkpoint']['mount_cached']={'transfers':4}
        self.assertEqual(validate(d,'launch'),[])
        for bad in ({},[],'fast'):
            d['checkpoint']['mount_cached']=bad
            self.assertTrue(validate(d,'launch'),bad)
        d['checkpoint'].update(mount_cached={'transfers':4},mount_mode='MOUNT')
        self.assertTrue(validate(d,'launch'))
    def test_deadline_is_optional(self):
        d=copy.deepcopy(self.ready); del d['budget']['deadline_utc']
        self.assertEqual(validate(d,'launch'),[])
        d['budget']['deadline_utc']=None
        self.assertEqual(validate(d,'launch'),[])
        for phase in ('plan','launch'):
            d['budget']['deadline_utc']='tomorrow'
            self.assertTrue(any('deadline_utc' in e for e in validate(d,phase)),phase)

    def test_approval_mode_is_descriptive_and_checked(self):
        d=copy.deepcopy(self.ready)
        for mode in ('automatic','gated','steps'):
            d['approval']['mode']=mode
            self.assertEqual(validate(d,'launch'),[])
        d['approval']['mode']='yolo'
        self.assertTrue(any('approval.mode' in e for e in validate(d,'launch')))

if __name__ == '__main__':
    unittest.main()
