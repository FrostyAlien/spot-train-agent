import sys as _sys
from pathlib import Path as _Path
SKILL = _Path(__file__).resolve().parents[2] / 'skills' / 'gpu-training-launch'   # the skill under test
_sys.path.insert(0, str(SKILL / 'scripts'))
import copy
import unittest
import sys
import types
import json
import os
import tempfile
from pathlib import Path
from render_job import render, validate_with_skypilot, check_secret_file, secret_names, default_helpers


def fixture():
    return {'schema_version': 1, 'purchase_model': 'spot', 'run_id': 'test-run', 'provider': 'aws', 'region': 'us-east-1',
            'gpu': {'model': 'A100', 'count': 8}, 'image': {'reference': 'org/train:1', 'digest': 'sha256:' + 'a'*64},
            'dataset': {'uri': 's3://data/version1', 'version': 'v1'},
            'checkpoint': {'uri': 's3://ckpt/test-run', 'run_id': 'test-run'},
            'recovery': {'enabled': True, 'allow_provider_change': False, 'allow_region_change': False,
                         'allow_on_demand': False, 'max_attempts': 3},
            'budget': {'total_limit': None, 'max_runtime_hours': None, 'controller_allowance': None},
            'approval': {'status': 'pending', 'scope': []}}


class RenderTests(unittest.TestCase):
    def test_render_pinned_and_quote(self):
        j = render(fixture(), ['python', 'train.py', '--name', 'a b'])
        self.assertEqual(j['resources']['infra'], 'aws/us-east-1')
        self.assertEqual(j['num_nodes'], 1)
        self.assertFalse(j['api_server_access'])
        self.assertIn("'a b'", j['run'])
        self.assertEqual(j['resources']['job_recovery']['max_restarts_on_errors'], 0)
    def test_missing_budget_allows_planning(self):
        self.assertIn('Set the reviewed training argv', render(fixture())['run'])
    def test_invalid_inputs(self):
        cases = [('region', '*'), ('provider', 'aws/gcp'), ('gpu', {'model': 'A100', 'count': 9}),
                 ('checkpoint', {'uri': 's3://ckpt/x', 'run_id': 'other'}),
                 ('dataset', {'uri': 'https://x/d?token=secret', 'version': 'v1'}),
                 ('budget', {'total_limit': float('nan')}), ('budget', {'total_limit': float('inf')})]
        for key, val in cases:
            with self.subTest(key=key):
                m = fixture(); m[key] = val
                with self.assertRaises(ValueError): render(m)
    def test_recovery_invariants(self):
        for key in ['allow_provider_change', 'allow_region_change', 'allow_on_demand']:
            m = fixture(); m['recovery'][key] = True
            with self.assertRaises(ValueError): render(m)
    def test_purchase_model_explicit(self):
        for mode in (None, '', 'automatic'):
            m = fixture(); m['purchase_model'] = mode
            with self.assertRaises(ValueError): render(m)
        m = fixture(); m['purchase_model'] = 'on-demand'
        self.assertFalse(render(m)['resources']['use_spot'])
        self.assertTrue(render(fixture())['resources']['use_spot'])
    def test_malformed_objects(self):
        for m in ([], None, 1):
            with self.assertRaises(ValueError): render(m)
        for field in ('gpu', 'image', 'dataset', 'checkpoint', 'recovery', 'budget'):
            for value in ([], None, 'invalid'):
                m = fixture(); m[field] = value
                with self.assertRaises(ValueError): render(m)
    def test_local_uri_rejected(self):
        for uri in ('file:///tmp/checkpoint', 'local://disk/checkpoint', '/tmp/checkpoint', 's3:///missing-bucket'):
            m = fixture(); m['checkpoint']['uri'] = uri
            with self.assertRaises(ValueError): render(m)
    def test_strict_schema_and_dataset_version(self):
        for version in (True, 1.0, '1'):
            m = fixture(); m['schema_version'] = version
            with self.assertRaises(ValueError): render(m)
        for version in ({'id': 'v1'}, ['v1'], True, 1, '', '   '):
            m = fixture(); m['dataset']['version'] = version
            with self.assertRaises(ValueError): render(m)
    def test_exact_instance_type_preserved(self):
        m=fixture(); m['gpu']['instance_type']='verified-catalog-type_2gpu-64vcpu-384gb'
        self.assertEqual(render(m)['resources']['instance_type'],m['gpu']['instance_type'])
        self.assertNotIn('instance_type',render(fixture())['resources'])
    def test_instance_type_must_be_concrete(self):
        for value in (None, '', '*', 'family/*', 'a b', True, ['type']):
            m=fixture();m['gpu']['instance_type']=value
            with self.assertRaises(ValueError):render(m)
    def test_digest_mismatch(self):
        m = fixture(); m['image']['reference'] += '@sha256:' + 'b'*64
        with self.assertRaises(ValueError): render(m)


class ExtensionTests(unittest.TestCase):
    def configured(self):
        m=fixture();m['checkpoint'].update(provider='aws',region='us-east-1',durable=True,storage_type='object-store',resume_policy='latest-complete')
        m['monitoring']={'wandb':{'enabled':True,'entity':'team','project':'train','run_id':'stable-run','mode':'online'}}
        return m
    def test_monitoring_render(self):
        env=render(self.configured())['envs']
        self.assertEqual(env['WANDB_RUN_ID'],'stable-run')
        self.assertEqual(env['WANDB_LOG_MODEL'],'false')
        self.assertNotIn('WANDB_API_KEY',env)
        self.assertNotIn('WANDB_RESUME',env)
    def test_checkpoint_extension_rejection(self):
        for key,value in [('provider','other'),('region',None),('durable',False),('storage_type','root-disk'),('resume_policy','fresh')]:
            m=self.configured();m['checkpoint'][key]=value
            with self.assertRaises(ValueError):render(m)
    def test_monitoring_secrets_and_shapes(self):
        for value in (None,[],{'wandb':[]},{'wandb':{'enabled':True,'api_key':'secret'}}):
            m=self.configured();m['monitoring']=value
            with self.assertRaises(ValueError):render(m)
    def test_monitoring_values(self):
        for key,value in [('enabled','true'),('entity','bad\\nvalue'),('run_id',''),('mode','invalid')]:
            m=self.configured();m['monitoring']['wandb'][key]=value
            with self.assertRaises(ValueError):render(m)

    def test_nebius_spot_only(self):
        m=fixture();m.update(provider='nebius',region='eu-north1',purchase_model='spot')
        m['recovery']['max_capacity_wait_minutes']=10
        job=render(m)
        self.assertEqual(job['resources']['infra'],'nebius/eu-north1')
        self.assertIs(job['resources']['use_spot'],True)
        self.assertNotIn('any_of',job['resources'])
        m['recovery']['allow_on_demand']=True
        with self.assertRaises(ValueError):render(m)
    def region_policy_fixture(self, **evidence):
        m = fixture(); m.update(provider='nebius', region='eu-north1')
        ev = {'shared_storage': True, 'cross_region_transfer_cost_per_gib': 0,
              'covers': ['checkpoint', 'dataset', 'image'], 'checked_at': '2026-10-02', 'source': 'provider docs'}
        ev.update(evidence)
        m['recovery'].update(allow_region_change=True, allowed_regions=['eu-north1', 'eu-west1'],
                             region_change_evidence=ev)
        return m
    def test_fixed_region_is_explicit_failover(self):
        self.assertEqual(render(fixture())['resources']['job_recovery']['strategy'], 'FAILOVER')
    def test_approved_multi_region_renders_eager_next_region_same_provider(self):
        res = render(self.region_policy_fixture())['resources']
        self.assertEqual(res['job_recovery']['strategy'], 'EAGER_NEXT_REGION')
        self.assertNotIn('infra', res)
        self.assertEqual(res['any_of'], [{'infra': 'nebius/eu-north1'}, {'infra': 'nebius/eu-west1'}])
        self.assertIs(res['use_spot'], True)
    def test_region_change_needs_free_shared_storage_evidence(self):
        for bad in ({'cross_region_transfer_cost_per_gib': 0.015}, {'shared_storage': False},
                    {'covers': ['checkpoint']}, {'cross_region_transfer_cost_per_gib': True}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                render(self.region_policy_fixture(**bad))
    def test_region_change_must_include_primary_region(self):
        m = self.region_policy_fixture(); m['recovery']['allowed_regions'] = ['eu-west1', 'us-central1']
        with self.assertRaises(ValueError): render(m)
    def test_wandb_history_policy_env(self):
        m=fixture(); m['monitoring']={'wandb':{'enabled':True,'entity':'e','project':'p','run_id':'r','mode':'online'}}
        self.assertEqual(render(m)['envs']['WANDB_HISTORY_POLICY'],'append')
        m['monitoring']['wandb']['history_policy']='rewind'
        self.assertEqual(render(m)['envs']['WANDB_HISTORY_POLICY'],'rewind')
        m['monitoring']['wandb']['history_policy']='overwrite'
        with self.assertRaises(ValueError): render(m)
    def test_app_restarts_are_separate_and_default_zero(self):
        m = fixture(); m['recovery']['max_attempts'] = 5
        self.assertEqual(render(m)['resources']['job_recovery']['max_restarts_on_errors'], 0)
        m['recovery']['max_app_restarts'] = 1
        self.assertEqual(render(m)['resources']['job_recovery']['max_restarts_on_errors'], 1)
        m['recovery']['max_app_restarts'] = 5          # SkyPilot's examples use 3-5; no cap of our own
        self.assertEqual(render(m)['resources']['job_recovery']['max_restarts_on_errors'], 5)
        for bad in (-1, True, 1.0):
            m['recovery']['max_app_restarts'] = bad
            with self.assertRaises(ValueError): render(m)
    def test_worker_deadline_env(self):
        m = fixture(); m['budget']['deadline_utc'] = '2026-10-03T12:00:00Z'
        self.assertEqual(render(m)['envs']['CAMPAIGN_DEADLINE_UTC'], '2026-10-03T12:00:00Z')
        self.assertNotIn('CAMPAIGN_DEADLINE_UTC', render(fixture())['envs'])
        m['budget']['deadline_utc'] = 'tomorrow'
        with self.assertRaises(ValueError): render(m)
    def test_runtime_backstop_is_the_budget_runway(self):
        m = fixture(); m['budget'].update(total_limit=15.0, controller_allowance=4.0, ceiling_hourly_usd=4.4)
        run = render(m, ['python', 'train.py'])['run']        # (15 - 4) / 4.4 h minus the 0.1 h shutdown reserve
        self.assertIn('\ntimeout --signal=TERM --kill-after=120 8640 python3 /opt/gpu-skills/progress_watch.py watch -- python train.py', run)
        self.assertNotIn('timeout', render(fixture(), ['python', 'train.py'])['run'])     # planning: no budget yet
        q = render(m, ['python', 'train.py'], variant='qualify', qualify_tag='q1', helpers=HELPERS)['run']
        self.assertNotIn('8640', q)                                                       # qualification: per-start timeout
        m['budget'].update(controller_allowance=15.0)
        with self.assertRaisesRegex(ValueError, 'no runway'):
            render(m, ['python', 'train.py'])
    def test_approved_fallback_with_the_guard_renders_separate_on_demand_task(self):
        m = fixture(); m['monitoring'] = {'guard': 'external'}
        m['recovery'].update(allow_on_demand=True, on_demand_fallback={'approved': True, 'after_capacity_wait_minutes': 120})
        primary = render(m)
        self.assertIs(primary['resources']['use_spot'], True)          # primary never mixes purchase modes
        self.assertNotIn('any_of', primary['resources'])
        self.assertEqual(primary['resources']['job_recovery']['strategy'], 'FAILOVER')
        fb = render(m, variant='on-demand-fallback')
        self.assertIs(fb['resources']['use_spot'], False)
        self.assertEqual((fb['name'], fb['envs']['PURCHASE_MODE']), ('test-run-od', 'on-demand-fallback'))
        with self.assertRaises(ValueError): render(fixture(), variant='on-demand-fallback')
        m['recovery']['on_demand_fallback'] = {'approved': True}        # the guard needs its timed wait
        with self.assertRaises(ValueError): render(m)
        m['recovery']['on_demand_fallback'] = {'approved': False, 'after_capacity_wait_minutes': 120}
        with self.assertRaises(ValueError): render(m)
    def test_approved_fallback_without_the_guard_is_ordered_spot_first(self):
        m = fixture(); m['recovery'].update(allow_on_demand=True, on_demand_fallback={'approved': True})
        r = render(m)['resources']
        self.assertNotIn('use_spot', r)
        self.assertNotIn('infra', r)
        self.assertEqual(r['ordered'], [{'infra': 'aws/us-east-1', 'use_spot': True},
                                        {'infra': 'aws/us-east-1', 'use_spot': False}])
        self.assertEqual(r['job_recovery']['strategy'], 'EAGER_NEXT_REGION')   # spot first again on each recovery
        self.assertIs(render(fixture())['resources']['use_spot'], True)        # no approval: spot only, FAILOVER
        self.assertEqual(render(fixture())['resources']['job_recovery']['strategy'], 'FAILOVER')
    def budgeted(self, **budget):
        m = fixture()
        m['budget'].update(total_limit=50, controller_allowance=2, ceiling_hourly_usd=4.5, free_without_vm=True, **budget)
        m['recovery'].update(max_capacity_wait_minutes=60, startup_allowance_minutes=8, max_attempts_without_progress=6)
        return m
    def test_the_workers_limits_are_rendered_for_the_run_only(self):
        j = render(self.budgeted(), ['python', 'train.py'], spent_before=3.25)
        e = j['envs']
        self.assertEqual((e['BUDGET_LIMIT_USD'], e['BUDGET_RESERVED_USD'], e['BUDGET_SPENT_BEFORE_USD'],
                          e['CEILING_HOURLY_USD'], e['FREE_WITHOUT_VM'], e['STARTUP_ALLOWANCE_MINUTES'],
                          e['MAX_ATTEMPTS_WITHOUT_PROGRESS'], e['MAX_ATTEMPTS']),
                         ('50', '2', '3.25', '4.5', '1', '8', '6', '3'))
        self.assertTrue(e['CAMPAIGN_SUBMITTED_AT'].isdigit())
        q = render(self.budgeted(), ['python', 'train.py'], variant='qualify', qualify_tag='q1')
        self.assertNotIn('BUDGET_LIMIT_USD', q['envs'])
        self.assertNotIn('BUDGET_LIMIT_USD', render(fixture())['envs'])      # unknown budget: planning only
        with self.assertRaises(ValueError): render(self.budgeted(), spent_before=-1)
    def test_notification_channel_is_a_required_secret_and_a_format(self):
        m = fixture(); m['monitoring'] = {'notify': {'channel': 'ntfy', 'format': 'slack'}}
        j = render(m, secrets=['WANDB_API_KEY'])
        self.assertEqual(j['envs']['SECRET_NAMES'], 'WANDB_API_KEY,NOTIFY_URL')
        self.assertEqual(j['envs']['NOTIFY_FORMAT'], 'slack')
        self.assertIn('NOTIFY_URL', secret_names(m))
        self.assertNotIn('NOTIFY_URL', secret_names(fixture()))
    def test_spot_price_cap_reaches_the_primary_worker_only(self):
        m = fixture(); m['budget']['spot_price_cap'] = {'per_gpu_hour': 1.2, 'cap_id': 'pricingpolicy-e00abc'}
        self.assertEqual(render(m)['envs']['PRICE_CAP_ID'], 'pricingpolicy-e00abc')
        self.assertNotIn('PRICE_CAP_ID', render(m, variant='qualify', qualify_tag='q1')['envs'])
        m['budget']['spot_price_cap']['cap_id'] = 'bad id; rm'
        with self.assertRaises(ValueError): render(m)
    def test_scheduling_timeout_only_on_request(self):
        m = self.budgeted()
        self.assertNotIn('job', render(m))
        self.assertEqual(render(m, scheduling_timeout=True)['job'], {'wait_for_scheduling_timeout': '68m'})
        self.assertNotIn('job', render(m, variant='qualify', qualify_tag='q1', scheduling_timeout=True))
        del m['recovery']['max_capacity_wait_minutes']
        with self.assertRaises(ValueError): render(m, scheduling_timeout=True)
    def minimal(self):
        m = fixture(); m.update(tier='minimal', purchase_model='on-demand')
        m['recovery'].update(enabled=False, max_attempts=0)
        m['budget'].update(max_worker_runtime_seconds=3600, autodown_idle_minutes=10)
        return m
    def test_minimal_tier_is_a_plain_bounded_launch(self):
        job = render(self.minimal(), ['python', 'train.py'])
        self.assertNotIn('job_recovery', job['resources'])
        self.assertIs(job['resources']['use_spot'], False)
        self.assertEqual(job['resources']['autostop'], {'idle_minutes': 10, 'down': True})
        self.assertIn('\ntimeout --signal=TERM --kill-after=120 3600 python3 /opt/gpu-skills/progress_watch.py watch -- python train.py', job['run'])
    def test_minimal_tier_rejects_unbounded_or_spot(self):
        for change in ({'purchase_model': 'spot'}, {'recovery': 'enabled'}, {'budget': 'max_worker_runtime_seconds'},
                       {'budget': 'autodown_idle_minutes'}, {'idle': 0}, {'timeout': 3600.5}):
            m = self.minimal()
            if 'purchase_model' in change: m['purchase_model'] = 'spot'
            elif change.get('recovery'): m['recovery']['enabled'] = True
            elif change.get('budget'): del m['budget'][change['budget']]
            elif 'idle' in change: m['budget']['autodown_idle_minutes'] = 0
            else: m['budget']['max_worker_runtime_seconds'] = change['timeout']
            with self.subTest(change=change), self.assertRaises(ValueError): render(m)
        with self.assertRaises(ValueError): render(self.minimal(), variant='on-demand-fallback')
        m = self.minimal(); m['tier'] = 'tiny'
        with self.assertRaises(ValueError): render(m)
    def test_standard_tier_is_always_managed(self):
        m = fixture(); m['purchase_model'] = 'on-demand'; m['recovery']['enabled'] = False
        with self.assertRaises(ValueError): render(m)
        m['recovery']['enabled'] = True
        self.assertNotIn('autostop', render(m)['resources'])
        run = render(m)['run']
        self.assertTrue(run.startswith('set -u\nexport TORCH_FR_DUMP_TEMP_FILE='))
        self.assertIn('|| true\npython3 /opt/gpu-skills/progress_watch.py watch -- python', run)
    def test_stall_watchdog_wraps_the_launcher_with_the_contract_settings(self):
        m = fixture(); m['budget'].update(total_limit=15.0, controller_allowance=4.0, ceiling_hourly_usd=4.4)
        m['watchdog'] = {'limits': {'train': 240, 'eval': 900, 'save': 1200}, 'ceilings': {'final': 7200}}
        j = render(m, ['torchrun', 'train.py'])
        self.assertIn('timeout --signal=TERM --kill-after=120 8640 python3 /opt/gpu-skills/progress_watch.py watch -- torchrun train.py', j['run'])
        self.assertEqual({k: j['envs'][k] for k in ('STALL_LIMITS', 'STALL_CEILINGS', 'STALL_K', 'PROGRESS_PERIOD_SECONDS',
                                                    'HEARTBEAT_DIR', 'PYTHONFAULTHANDLER', 'TORCH_NCCL_DEBUG_INFO_PIPE_FILE',
                                                    'STALL_DUMP_PIPES')},
                         {'STALL_LIMITS': 'train=240,eval=900,save=1200,final=600,startup=1800', 'STALL_CEILINGS': 'final=7200',
                          'STALL_K': '5', 'PROGRESS_PERIOD_SECONDS': '300', 'HEARTBEAT_DIR': '/tmp/heartbeat',
                          'PYTHONFAULTHANDLER': '1', 'TORCH_NCCL_DEBUG_INFO_PIPE_FILE': '/tmp/heartbeat/fr-pipe-',
                          'STALL_DUMP_PIPES': '/tmp/heartbeat/fr-pipe-*.pipe'})
        self.assertNotIn('STALL_CEILINGS', render(fixture(), ['python'])['envs'])     # none by default
        # flight-recorder dumps go to the run's durable directory, one folder per worker start
        self.assertIn('export TORCH_FR_DUMP_TEMP_FILE="${TORCH_FR_DUMP_TEMP_FILE:-$CHECKPOINT_DIR/debug/$(date -u', j['run'])
        self.assertLess(j['run'].index('mkdir -p "${TORCH_FR_DUMP_TEMP_FILE%/*}" || true'), j['run'].index('progress_watch.py watch'))
    def test_qualification_watchdog_uses_default_limits_and_reports_its_summary(self):
        m = fixture(); m['watchdog'] = {'limits': {'eval': 4000}, 'ceilings': {'eval': 5000}}
        j = render(m, ['python'], variant='qualify', qualify_tag='q7', timeout_minutes=15, helpers=HELPERS, eval_scale=12.5)
        # default limits, but startup inside the 15-minute per-start timeout: a startup hang ends with a STALL line
        self.assertEqual((j['envs']['STALL_LIMITS'], j['envs']['HEARTBEAT_DIR'], 'STALL_CEILINGS' in j['envs']),
                         ('train=180,eval=600,save=600,final=600,startup=780', '/tmp/heartbeat/qualify-q7', False))
        long = render(m, ['python'], variant='qualify', qualify_tag='q7', timeout_minutes=60, helpers=HELPERS)
        self.assertTrue(long['envs']['STALL_LIMITS'].endswith('startup=1800'))
        self.assertIn('--watch-summaries "$HEARTBEAT_DIR/summaries.jsonl" --eval-scale 12.5', j['run'])
        self.assertEqual(j['run'].count('export TORCH_FR_DUMP_TEMP_FILE'), 1)         # once for both starts
        self.assertNotIn('--eval-scale', render(m, ['python'], variant='qualify', qualify_tag='q7', helpers=HELPERS)['run'])
        with self.assertRaises(ValueError):
            render(m, ['python'], variant='qualify', qualify_tag='q7', helpers=HELPERS, eval_scale=0.5)
        self.assertIn('/opt/gpu-skills/progress_watch.py', default_helpers())
    def test_watchdog_can_be_turned_off_and_is_checked(self):
        m = fixture(); m['watchdog'] = {'enabled': False}
        j = render(m, ['python', 'train.py'])
        self.assertTrue(j['run'].startswith('set -u\nexport TORCH_FR_DUMP_TEMP_FILE='))   # diagnosis without the watchdog
        self.assertTrue(j['run'].endswith('|| true\npython train.py\n'))
        self.assertNotIn('HEARTBEAT_DIR', j['envs'])
        self.assertEqual(j['envs']['PYTHONFAULTHANDLER'], '1')
        for bad in ({'k': 0}, {'limits': {'warmup': 60}}, {'limits': {'eval': -1}}, {'ceilings': []}, {'floor': 60},
                    {'ceilings': {'startup': 600}}, {'ceilings': {'train': 600}}, {'enabled': 'no'}, {'step_hook': True}, []):
            m['watchdog'] = bad
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                render(m, ['python'])
        verify = render(fixture(), ['python'], variant='verify', helpers=HELPERS)
        self.assertNotIn('progress_watch', verify['run'])
        self.assertNotIn('PYTHONFAULTHANDLER', verify['envs'])
    def test_v08_watchdog_keys_are_refused_with_the_new_names(self):
        for old in ({'floor_seconds': 180}, {'seed_gap_seconds': 2}, {'budgets': {'save': 120}}):
            m = fixture(); m['watchdog'] = old
            with self.subTest(old=old), self.assertRaisesRegex(ValueError, 'v0.8 keys.*watchdog.limits'):
                render(m, ['python'])
    def test_validation_output_never_lands_in_the_rendered_task(self):
        # A fresh host refreshes SkyPilot's catalog on first use and prints progress records to stdout.
        import subprocess
        with tempfile.TemporaryDirectory() as tmp:
            pkg = Path(tmp) / 'sky'; pkg.mkdir()
            (pkg / '__init__.py').write_text(
                'print("<sky-payload>catalog refresh</sky-payload>")\n__version__ = "stub"\n'
                'class Task:\n    managed_secret_refs = None\n    storage_mounts = {}\n'
                '    @staticmethod\n    def from_yaml_config(c):\n        return Task()\n')
            manifest = Path(tmp) / 'm.json'; manifest.write_text(json.dumps(fixture()))
            out = subprocess.run([sys.executable, str(SKILL / 'scripts' / 'render_job.py'), str(manifest),
                                  '--argv-json', '["python"]', '--validate-with-skypilot'],
                                 capture_output=True, text=True, env={**os.environ, 'PYTHONPATH': tmp})
        self.assertEqual(out.returncode, 0, out.stderr)
        json.loads(out.stdout)                                   # stdout is the task, nothing else
        self.assertIn('sky-payload', out.stderr)
    def test_progress_bars_stay_out_of_the_job_log(self):
        for variant in ('primary', 'qualify'):
            self.assertEqual(render(fixture(), ['python'], variant=variant, qualify_tag='q1', helpers=HELPERS)['envs']['TQDM_DISABLE'], '1')
    def test_upload_flush_appended_by_skypilot_runs_and_keeps_the_exit_code(self):
        # SkyPilot appends its cached-mount flush to `run:`; an `exec` or `set -e` would skip it.
        import subprocess
        watcher = default_helpers()['/opt/gpu-skills/progress_watch.py']
        for code in (0, 3):
            task = render(fixture(), ['sh', '-c', f'exit {code}'])
            run = task['run'].replace('/opt/gpu-skills/progress_watch.py', watcher)
            with tempfile.TemporaryDirectory() as hb:
                out = subprocess.run(['bash', '-c', run + '\n__c=$?; [[ -o pipefail ]] && p=on || p=off; echo "FLUSH $__c $p"; exit $__c'], capture_output=True,
                                     text=True, env={**os.environ, **task['envs'], 'HEARTBEAT_DIR': hb, 'CHECKPOINT_DIR': hb})
                self.assertEqual(len(list(Path(hb).glob('debug/*Z'))), 1)     # the flight-recorder dump folder
            self.assertEqual((out.stdout.strip().splitlines()[-1], out.returncode), (f'FLUSH {code} off', code))   # pipefail stays off
    def test_qualification_script_runs_under_bash(self):
        # set -euo pipefail plus the flight-recorder lines, two starts under the watcher, then journal-check
        import subprocess
        h = default_helpers()
        task = render(fixture(), ['sh', '-c', 'exit 0'], variant='qualify', qualify_tag='q1', helpers=HELPERS, eval_scale=4)
        run = task['run'].replace('/opt/gpu-skills/progress_watch.py', h['/opt/gpu-skills/progress_watch.py']) \
                         .replace('/opt/gpu-skills/checkpoint_marker.py', h['/opt/gpu-skills/checkpoint_marker.py'])
        with tempfile.TemporaryDirectory() as tmp:
            stub = Path(tmp) / 'bin'; stub.mkdir()
            (stub / 'timeout').write_text('#!/bin/sh\nwhile [ "${1#-}" != "$1" ]; do shift; done; shift; exec "$@"\n')
            (stub / 'timeout').chmod(0o755)                    # macOS has no timeout(1)
            env = {**os.environ, **task['envs'], 'CHECKPOINT_DIR': tmp, 'HEARTBEAT_DIR': tmp + '/hb',
                   'PATH': f"{stub}:{os.environ['PATH']}"}
            out = subprocess.run(['bash', '-c', run], capture_output=True, text=True, env=env)
            self.assertEqual(len(list(Path(tmp).glob('debug/*Z'))), 1, out.stderr)
        self.assertEqual(out.stdout.count('WATCH-SUMMARY'), 2, out.stdout + out.stderr)
        self.assertIn('"ok": false', out.stdout)              # journal-check ran (no journal here) and set -e stopped
        self.assertEqual(out.returncode, 1)
    def test_checkpoint_and_dataset_mounts(self):
        job = render(fixture())
        # MOUNT modes take a bucket root only; the run prefix becomes part of CHECKPOINT_DIR.
        self.assertEqual(job['file_mounts'], {'/checkpoints': {'source': 's3://ckpt', 'mode': 'MOUNT_CACHED',
                                                               'type': 'MODEL_CHECKPOINT_RW'}})
        self.assertEqual(job['envs']['CHECKPOINT_DIR'], '/checkpoints/test-run')
        self.assertNotIn('DATASET_DIR', job['envs'])
        m = fixture(); m['checkpoint']['mount_mode'] = 'MOUNT'; m['dataset']['mount_mode'] = 'COPY'
        job = render(m)
        self.assertEqual(job['file_mounts']['/checkpoints'], {'source': 's3://ckpt', 'mode': 'MOUNT'})
        self.assertEqual(job['file_mounts']['/data'], {'source': 's3://data/version1', 'mode': 'COPY'})
        self.assertEqual(job['envs']['DATASET_DIR'], '/data')
        m = fixture(); m['dataset']['mount_mode'] = 'MOUNT'
        job = render(m)
        self.assertEqual((job['file_mounts']['/data']['source'], job['envs']['DATASET_DIR']), ('s3://data', '/data/version1'))
        m = fixture(); m['checkpoint']['uri'] = 's3://ckpt-bucket'
        self.assertEqual(render(m)['envs']['CHECKPOINT_DIR'], '/checkpoints')
        cases = {'https://acct.blob.core.windows.net/container/runs/test-run':
                     ('https://acct.blob.core.windows.net/container', '/checkpoints/runs/test-run'),
                 'https://acct.blob.core.windows.net/container': ('https://acct.blob.core.windows.net/container', '/checkpoints'),
                 'cos://us-east/bucket/test-run': ('cos://us-east/bucket', '/checkpoints/test-run'),
                 'cos://us-east/bucket': ('cos://us-east/bucket', '/checkpoints')}
        for uri, (source, ckdir) in cases.items():          # bucket is the first path segment here
            m = fixture(); m['checkpoint']['uri'] = uri
            job = render(m)
            with self.subTest(uri=uri):
                self.assertEqual((job['file_mounts']['/checkpoints']['source'], job['envs']['CHECKPOINT_DIR']), (source, ckdir))
        m = fixture(); m['dataset'].update(uri='cos://us-east/bucket/v1', mount_mode='COPY')
        self.assertEqual(render(m)['file_mounts']['/data']['source'], 'cos://us-east/bucket/v1')
        m = fixture(); m['checkpoint']['uri'] = 'gcs://ckpt/test-run'       # not a SkyPilot scheme
        with self.assertRaises(ValueError): render(m)
        for field, bad in (('checkpoint', 'COPY'), ('dataset', 'SYNC')):
            m = fixture(); m[field]['mount_mode'] = bad
            with self.subTest(field=field), self.assertRaises(ValueError): render(m)
        m = fixture(); m['checkpoint']['storage_type'] = 'retained-volume'
        self.assertNotIn('file_mounts', render(m))      # volumes are wired by the user
    def test_checkpoint_mount_follows_the_preset_with_optional_overrides(self):
        m = fixture(); m['checkpoint']['mount_cached'] = {'transfers': 4}
        mount = render(m)['file_mounts']['/checkpoints']
        self.assertEqual((mount['type'], mount['config']), ('MODEL_CHECKPOINT_RW', {'mount_cached': {'transfers': 4}}))
        for bad in ({}, [], 'fast'):
            m['checkpoint']['mount_cached'] = bad
            with self.subTest(bad=bad), self.assertRaises(ValueError): render(m)
        m = fixture(); m['checkpoint'].update(mount_mode='MOUNT', mount_cached={'transfers': 4})
        with self.assertRaisesRegex(ValueError, 'MOUNT_CACHED'): render(m)
    def test_metrics_backend_without_wandb(self):
        m = fixture(); m['monitoring'] = {'metrics_backend': 'mlflow', 'notify': {'channel': 'email'}}
        envs = render(m)['envs']
        self.assertEqual(envs['METRICS_BACKEND'], 'mlflow')
        self.assertFalse(any(k.startswith('WANDB_') for k in envs))
        m['monitoring']['metrics_backend'] = 'ml flow; rm -rf'
        with self.assertRaises(ValueError): render(m)
    def test_capacity_wait_bound(self):
        for value in (0,-1,True,float('nan'),'10'):
            m=fixture();m['recovery']['max_capacity_wait_minutes']=value
            with self.assertRaises(ValueError):render(m)

    def test_nebius_storage_identifier(self):
        m=fixture();m['checkpoint']['uri']='nebius://approved-bucket/run/checkpoints'
        self.assertEqual(render(m)['envs']['CHECKPOINT_URI'],m['checkpoint']['uri'])
        for uri in ('nebius:///missing-bucket','nebius://bucket/key?token=secret'):
            m['checkpoint']['uri']=uri
            with self.assertRaises(ValueError):render(m)

class SkyPilotDriftTests(unittest.TestCase):
    def with_fake_sky(self, parser):
        fake = types.SimpleNamespace(__version__='9.9', Task=types.SimpleNamespace(from_yaml_config=parser))
        saved = sys.modules.get('sky'); sys.modules['sky'] = fake
        try:
            return validate_with_skypilot(render(fixture()))
        finally:
            if saved is None: del sys.modules['sky']
            else: sys.modules['sky'] = saved

    def test_installed_parser_accepts(self):
        self.assertEqual(self.with_fake_sky(lambda cfg: None), '9.9')

    def test_schema_drift_surfaces_before_launch(self):
        def strict(cfg):
            raise ValueError("Invalid task YAML: Found unsupported field 'api_server_access'")
        with self.assertRaisesRegex(ValueError, 'unsupported field'):
            self.with_fake_sky(strict)


HELPERS = {'/opt/gpu-skills/checkpoint_marker.py': '/tmp/checkpoint_marker.py',
           '/opt/gpu-skills/verify_checkpoint.py': '/tmp/verify_checkpoint.py'}


class QualifyAndSecretTests(unittest.TestCase):
    def test_secret_names_only_in_env_never_a_secrets_section(self):
        m = fixture(); m['image']['registry_login'] = True
        j = render(m, ['python', 'train.py'], secrets=['WANDB_API_KEY'])
        self.assertNotIn('secrets', j)         # a null-valued secret is a managed-secret reference in SkyPilot
        self.assertEqual(j['envs']['SECRET_NAMES'].split(','), ['WANDB_API_KEY', 'SKYPILOT_DOCKER_USERNAME',
                                                               'SKYPILOT_DOCKER_PASSWORD', 'SKYPILOT_DOCKER_SERVER'])
        self.assertEqual(j['envs']['RUN_MODE'], 'train')
        for bad in (['lower'], ['SKYPILOT_DOCKER_PASSWORD'], ['A', 'A']):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                render(fixture(), ['python'], secrets=bad)
        self.assertNotIn('SECRET_NAMES', render(fixture(), ['python'])['envs'])

    def test_check_secret_file(self):
        m = fixture(); m['image']['registry_login'] = True
        names = secret_names(m, ['WANDB_API_KEY'])
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, 'secrets.env')
            with open(p, 'w') as f:
                f.write('WANDB_API_KEY=k\nSKYPILOT_DOCKER_USERNAME=iam\nSKYPILOT_DOCKER_PASSWORD="t"\n'
                        'SKYPILOT_DOCKER_SERVER=\n')
            os.chmod(p, 0o644)
            r = check_secret_file(p, names)
            self.assertFalse(r['ok'])
            self.assertIn("missing or empty: ['SKYPILOT_DOCKER_SERVER']", r['problems'][0])
            self.assertIn('chmod 600', r['problems'][1])
            self.assertNotIn('"t"', json.dumps(r))            # values never echoed
            with open(p, 'a') as f:
                f.write('SKYPILOT_DOCKER_SERVER=cr.example\n')
            os.chmod(p, 0o600)
            self.assertTrue(check_secret_file(p, names)['ok'])

    def test_rehearsal_is_cpu_streams_data_and_checks_the_journal(self):
        m = fixture(); m['budget']['deadline_utc'] = '2100-01-01T00:00:00Z'; m['dataset']['mount_mode'] = 'COPY'
        j = render(m, ['python', 'train.py'], variant='rehearsal', qualify_tag='q1', timeout_minutes=15, helpers=HELPERS)
        r = j['resources']
        self.assertEqual((r['infra'], r['use_spot'], r['cpus']), ('aws', False, '4+'))
        self.assertNotIn('accelerators', r)
        self.assertNotIn('job_recovery', r)
        self.assertEqual(r['autostop'], {'idle_minutes': 10, 'down': True})
        self.assertEqual(j['file_mounts']['/data'], {'source': 's3://data', 'mode': 'MOUNT'})   # streamed, not copied
        self.assertEqual(j['envs']['DATASET_DIR'], '/data/version1')
        self.assertEqual(j['envs']['CHECKPOINT_DIR'], '/checkpoints/qualify/test-run/rehearsal-q1')
        self.assertEqual(j['envs']['RUN_MODE'], 'rehearsal')
        self.assertNotIn('CAMPAIGN_DEADLINE_UTC', j['envs'])
        lines = j['run'].strip().split('\n')
        self.assertEqual(lines.count('timeout --signal=TERM --kill-after=120 900 python3 /opt/gpu-skills/progress_watch.py watch -- python train.py'), 2)   # fresh, resume
        self.assertIn('journal-check "$CHECKPOINT_DIR" --expect fresh,resume --require-final', lines[-3])
        self.assertEqual(lines[-2:], ['echo "QUALIFY-DONE $(date -u +%s)"', 'set +e +o pipefail'])   # then SkyPilot's flush
        # the image's own helpers: SkyPilot cannot mount onto /opt/gpu-skills, which the image already has
        self.assertFalse(any(k.startswith('/opt/gpu-skills') for k in j['file_mounts']))
        self.assertEqual(j['name'], 'test-run-rehearsal')

    def test_qualification_mounts_the_staged_subset_like_the_dataset(self):
        m = fixture(); m['dataset'].update(mount_mode='COPY', qualify_uri='s3://data/version1/qualify')
        j = render(m, ['python'], variant='qualify', qualify_tag='q1', helpers=HELPERS)
        self.assertEqual(j['file_mounts']['/data'], {'source': 's3://data/version1/qualify', 'mode': 'COPY'})
        self.assertEqual((j['envs']['DATASET_DIR'], j['envs']['DATASET_URI']), ('/data', 's3://data/version1/qualify'))
        m['dataset']['mount_mode'] = 'MOUNT'
        j = render(m, ['python'], variant='qualify', qualify_tag='q1', helpers=HELPERS)
        self.assertEqual((j['file_mounts']['/data']['source'], j['envs']['DATASET_DIR']), ('s3://data', '/data/version1/qualify'))
        self.assertEqual(render(m, ['python'])['envs']['DATASET_DIR'], '/data/version1')     # the run reads the full data
        m['dataset']['mount_mode'] = None
        with self.assertRaisesRegex(ValueError, 'needs dataset.mount_mode'):
            render(m, ['python'], variant='qualify', qualify_tag='q1', helpers=HELPERS)
        m['dataset'].update(mount_mode='COPY', qualify_uri='/local/subset')
        with self.assertRaisesRegex(ValueError, 'qualify_uri'):
            render(m, ['python'])
    def test_qualify_runs_fresh_then_resume_on_the_real_gpus(self):
        m = fixture(); m['monitoring'] = {'wandb': {'enabled': True, 'entity': 'e', 'project': 'p', 'run_id': 'test-run',
                                                     'mode': 'online'}}
        j = render(m, ['python', 'train.py'], variant='qualify', qualify_tag='q1', helpers=HELPERS)
        r = j['resources']
        self.assertEqual((r['infra'], r['accelerators'], r['use_spot']), ('aws/us-east-1', 'A100:8', True))
        self.assertNotIn('job_recovery', r)
        self.assertEqual(r['autostop'], {'idle_minutes': 10, 'down': True})
        self.assertEqual(j['envs']['WANDB_RUN_ID'], 'test-run-qualify-q1')     # never the run's own W&B history
        self.assertEqual((j['envs']['RUN_MODE'], j['envs']['GPU_COUNT'], j['envs']['IMAGE_DIGEST']),
                         ('qualify', '8', m['image']['digest']))
        lines = j['run'].strip().split('\n')
        self.assertEqual(lines.count('timeout --signal=TERM --kill-after=120 1200 python3 /opt/gpu-skills/progress_watch.py watch -- python train.py'), 2)   # fresh, resume
        self.assertIn('--expect fresh,resume --require-final --require preflight', lines[-3])
        self.assertEqual(j['name'], 'test-run-qualify')
        for bad in ({'qualify_tag': None}, {'qualify_tag': 'bad tag'}, {'qualify_tag': 'q', 'timeout_minutes': 0}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                render(fixture(), ['python'], variant='qualify', helpers=HELPERS, **bad)

    def test_qualification_is_stamped_with_its_render_time(self):
        import time
        for variant in ('qualify', 'rehearsal'):
            stamp = render(fixture(), ['python'], variant=variant, qualify_tag='q1', helpers=HELPERS)['envs']['QUALIFY_SUBMITTED_AT']
            self.assertLessEqual(abs(int(stamp) - time.time()), 5)
        self.assertNotIn('QUALIFY_SUBMITTED_AT', render(fixture(), ['python'])['envs'])

    def test_qualification_leaves_pipefail_off_for_the_appended_flush(self):
        # SkyPilot's flush runs `tac log | grep -m 1 | grep -q`: under pipefail it fails on a long log forever.
        import subprocess
        run = render(fixture(), ['python'], variant='qualify', qualify_tag='q1', helpers=HELPERS)['run']
        shell = '\n'.join(line if line.startswith(('set ', 'echo "QUALIFY-DONE')) else 'true' for line in run.strip().split('\n'))
        out = subprocess.run(['bash', '-c', shell + '\n[[ -o pipefail ]] && echo PIPEFAIL-ON || echo PIPEFAIL-OFF'],
                             capture_output=True, text=True)
        self.assertEqual(out.stdout.split()[-1], 'PIPEFAIL-OFF')

    def test_cpu_tasks_can_be_pinned_to_a_region_with_cpu_quota(self):
        for variant in ('rehearsal', 'verify'):
            j = render(fixture(), ['python'], variant=variant, qualify_tag='q1', helpers=HELPERS, cpu_region='eu-west-2')
            self.assertEqual(j['resources']['infra'], 'aws/eu-west-2')
        self.assertEqual(render(fixture(), ['python'], variant='qualify', qualify_tag='q1', helpers=HELPERS,
                                cpu_region='eu-west-2')['resources']['infra'], 'aws/us-east-1')   # GPUs stay put
        with self.assertRaisesRegex(ValueError, 'cpu_region'):
            render(fixture(), ['python'], variant='verify', helpers=HELPERS, cpu_region='bad region')
        self.assertEqual(render(fixture(), ['python', 'train.py'])['envs']['GPU_COUNT'], '8')

    def test_verify_is_a_cpu_check_of_final_against_the_bucket(self):
        j = render(fixture(), None, variant='verify', helpers=HELPERS)
        self.assertEqual(j['resources'], {'infra': 'aws', 'cpus': '2+', 'use_spot': False,
                                          'autostop': {'idle_minutes': 10, 'down': True}})
        self.assertEqual(j['file_mounts']['/checkpoints'], {'source': 's3://ckpt', 'mode': 'MOUNT'})
        self.assertIn('verify-final "$R" --mount-root /checkpoints --run-id test-run --data-version v1 --world-size 8', j['run'])
        self.assertIn('journal-show', j['run'])
        self.assertIn('FINISH-PARTIAL', j['run'])          # saves cut off by a preemption, for the inventory
        self.assertNotIn('image_id', j['resources'])       # no registry secrets needed

    def test_default_helpers_found_next_to_this_skill(self):
        h = default_helpers()
        self.assertTrue(all(os.path.isfile(v) for v in h.values()))
        self.assertEqual(sorted(h), sorted([*HELPERS, '/opt/gpu-skills/progress_watch.py']))

    def test_tasks_parse_and_round_trip_with_installed_skypilot(self):
        try:
            import sky
        except ImportError:
            self.skipTest('SkyPilot not installed here')
        m = fixture(); m['image']['registry_login'] = True
        validate_with_skypilot(render(m, ['python'], secrets=['WANDB_API_KEY']))
        for variant in ('qualify', 'rehearsal', 'verify'):
            with self.subTest(variant=variant):
                validate_with_skypilot(render(fixture(), ['python'], variant=variant, qualify_tag='q1',
                                              helpers=default_helpers()))
        bad = render(fixture(), ['python']); bad['secrets'] = {'WANDB_API_KEY': None}
        with self.assertRaisesRegex(ValueError, 'managed-secret'):
            validate_with_skypilot(bad)
        m = fixture(); m['checkpoint']['mount_cached'] = {'transfers': 4}
        job = render(m, ['python'])
        validate_with_skypilot(job)                          # the preset and an override parse and round-trip
        mount = sky.Task.from_yaml_config(json.loads(json.dumps(job))).storage_mounts['/checkpoints']
        self.assertEqual((mount.file_mount_type.value, mount.resolve_mount_cached_config().transfers), ('MODEL_CHECKPOINT_RW', 4))
        m['checkpoint']['mount_cached'] = {'no_such_setting': 1}
        with self.assertRaises(Exception):                   # SkyPilot's schema rejects unknown settings
            validate_with_skypilot(render(m, ['python']))


class DataAndDiskTests(unittest.TestCase):
    HF = 'hf://datasets/org/name@' + 'c' * 40

    def test_hf_dataset_is_mounted_as_given_and_pinned(self):
        m = fixture(); m['dataset'].update(uri=self.HF + '/train', mount_mode='MOUNT')
        j = render(m, ['python'])
        self.assertEqual((j['file_mounts']['/data'], j['envs']['DATASET_DIR']),
                         ({'source': self.HF + '/train', 'mode': 'MOUNT'}, '/data'))
        r = render(m, ['python'], variant='rehearsal', qualify_tag='q1', helpers=HELPERS)
        self.assertEqual(r['file_mounts']['/data']['source'], self.HF + '/train')
        for bad_uri, mode in (('hf://datasets/org/name', 'MOUNT'), ('hf://org/model@main', 'MOUNT'),
                              (self.HF, 'MOUNT_CACHED'), (self.HF, None)):
            m = fixture(); m['dataset'].update(uri=bad_uri, mount_mode=mode)
            with self.subTest(uri=bad_uri, mode=mode), self.assertRaises(ValueError):
                render(m, ['python'])
        m = fixture(); m['checkpoint']['uri'] = self.HF
        with self.assertRaises(ValueError):
            render(m, ['python'])                     # checkpoints never go to a read-only Hub repo

    def test_no_absolute_attempt_cap(self):
        for value in (None, 'absent'):
            m = fixture()
            if value == 'absent':
                del m['recovery']['max_attempts']
            else:
                m['recovery']['max_attempts'] = None
            self.assertIn('job_recovery', render(m, ['python'])['resources'])
        m = fixture(); m['recovery']['max_attempts'] = 0
        with self.assertRaises(ValueError):
            render(m, ['python'])

    def test_disk_size(self):
        m = fixture(); m['disk'] = {'size_gb': 500}
        self.assertEqual(render(m, ['python'])['resources']['disk_size'], 500)
        self.assertEqual(render(m, ['python'], variant='qualify', qualify_tag='q', helpers=HELPERS)['resources']['disk_size'], 500)
        self.assertNotIn('disk_size', render(fixture(), ['python'])['resources'])
        for bad in ({'size_gb': '500'}, {'size_gb': 5}, []):
            m = fixture(); m['disk'] = bad
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                render(m, ['python'])

    def test_hf_and_disk_round_trip_with_installed_skypilot(self):
        try:
            import sky  # noqa: F401
        except ImportError:
            self.skipTest('SkyPilot not installed here')
        m = fixture(); m['dataset'].update(uri=self.HF, mount_mode='MOUNT'); m['disk'] = {'size_gb': 500}
        validate_with_skypilot(render(m, ['python']))


class ScriptEndToEndTests(unittest.TestCase):
    """Run the rendered rehearsal and verify scripts with bash against a local directory standing in
    for the bucket (helpers at their real paths, `timeout` shimmed)."""
    IDENT = {'image_digest': 'sha256:' + 'a' * 64, 'code_sha256': 'c0de', 'config_sha256': 'cf9'}

    def setUp(self):
        import importlib.util, subprocess
        self.subprocess = subprocess
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.helpers = default_helpers()
        self.hdir = os.path.dirname(self.helpers['/opt/gpu-skills/checkpoint_marker.py'])
        spec = importlib.util.spec_from_file_location('cm_e2e', os.path.join(self.hdir, 'checkpoint_marker.py'))
        self.cm = importlib.util.module_from_spec(spec)
        sys.path.insert(0, self.hdir)
        spec.loader.exec_module(self.cm)
        os.makedirs(os.path.join(self.root, 'bin'))
        shim = os.path.join(self.root, 'bin', 'timeout')
        with open(shim, 'w') as f:                     # drop timeout's options and duration, run the rest
            f.write('#!/bin/bash\nwhile [[ $1 == -* ]]; do shift; done; shift; exec "$@"\n')
        os.chmod(shim, 0o755)

    @staticmethod
    def check_line(r):
        return json.loads([line for line in r.stdout.splitlines() if line.startswith('{')][-1])

    def bash(self, script, env):
        for remote, local in self.helpers.items():          # the helpers at their real paths
            script = script.replace(remote, local)
        return self.subprocess.run(['bash', '-c', script], capture_output=True, text=True,
                                   env={**os.environ, 'PATH': os.path.join(self.root, 'bin') + ':' + os.environ['PATH'], **env})

    def test_rehearsal_script_runs_fresh_then_resume_and_checks_the_journal(self):
        entry = os.path.join(self.root, 'entry.py')
        with open(entry, 'w') as f:
            f.write(f'''import os, sys, time
sys.path.insert(0, {self.hdir!r}); sys.path.insert(0, {os.path.dirname(self.helpers['/opt/gpu-skills/progress_watch.py'])!r})
import progress_watch as pw
import checkpoint_marker as cm
run = os.environ['CHECKPOINT_DIR']; attempt = time.strftime('%H%M%S') + 'p' + str(os.getpid())
ident = {self.IDENT!r}
if os.environ.get('RUN_MODE') == 'qualify':
    cm.journal(run, attempt, 'preflight', {{'gpus': os.environ.get('GPU_COUNT'), 'image': os.environ.get('IMAGE_DIGEST')}})
sel = cm.select(run, 'test-run', 'v1', 'm1', 1, ident)
cm.journal(run, attempt, 'selection', {{'action': sel['action']}})
start = sel.get('global_step', 0)
for step in (start + 6, start + 12) if sel['action'] == 'fresh' else (start + 6, 20):   # stop after two commits; resume to 20
    pw.tick('train', step, step); time.sleep(0.15); pw.tick('train', step, step)       # the stall watchdog's heartbeat
    d = cm.step_dir(run, step, attempt); os.makedirs(os.path.join(d, 'payload'))
    open(os.path.join(d, 'payload', 'm.pt'), 'w').write(str(step))
    cm.commit(d, {{'run_id': 'test-run', 'data_version': 'v1', 'model_version': 'm1', 'world_size': 1, 'global_step': step,
                  'optimizer_boundary': True, 'scaler_used': False, 'derived_roles': {{'rng': {{'from': ['seed'], 'semantics': 'reseeded'}}, 'data': {{'from': ['epoch'], 'semantics': 'replayed'}}}},
                  **ident}}, [{{'path': 'payload/m.pt', 'roles': ['model', 'optimizer', 'scheduler', 'training_state']}}])
    cm.journal(run, attempt, 'commit', {{'global_step': str(step), 'save_seconds': '1'}})
if sel['action'] == 'resume':
    cm.finalize(run, 'test-run', 'v1', 'm1', 1, ident, 'completed', 20)
cm.journal(run, attempt, 'finish')
''')
        j = render(fixture(), ['python3', entry], variant='rehearsal', qualify_tag='q1', helpers=self.helpers)
        ckdir = os.path.join(self.root, 'mnt', 'qualify', 'test-run', 'rehearsal-q1')
        os.makedirs(os.path.dirname(ckdir))
        r = self.bash(j['run'], {'CHECKPOINT_DIR': ckdir, 'HEARTBEAT_DIR': os.path.join(self.root, 'hb1')})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(r.stdout.count('CHECKPOINT_COMMITTED'), 4)
        self.assertTrue(self.check_line(r)['ok'])
        self.assertRegex(r.stdout.strip().splitlines()[-1], r'^QUALIFY-DONE \d+$')
        # The GPU qualification task: the same two starts, and the preflight must be journaled.
        j = render(fixture(), ['python3', entry], variant='qualify', qualify_tag='q2', helpers=self.helpers)
        ckdir = os.path.join(self.root, 'mnt', 'qualify', 'test-run', 'qualify-q2')
        r = self.bash(j['run'], {'CHECKPOINT_DIR': ckdir, 'RUN_MODE': 'qualify', 'GPU_COUNT': '1', 'IMAGE_DIGEST': 'd',
                                 'HEARTBEAT_DIR': os.path.join(self.root, 'hb2'),
                                 'QUALIFY_SUBMITTED_AT': str(int(__import__('time').time()) - 60)})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        result = self.check_line(r)
        self.assertTrue(result['ok'], result)
        self.assertIn('recommended_startup_allowance_minutes', result)
        r = self.bash(j['run'], {'CHECKPOINT_DIR': ckdir, 'GPU_COUNT': '1', 'IMAGE_DIGEST': 'd',
                                 'HEARTBEAT_DIR': os.path.join(self.root, 'hb3')})   # no preflight: fails
        self.assertEqual(r.returncode, 1)

    def test_verify_script_checks_final_and_fails_on_a_changed_artifact(self):
        run = os.path.join(self.root, 'mnt', 'test-run')
        d = self.cm.step_dir(run, 3, 'a1'); os.makedirs(os.path.join(d, 'payload')); os.makedirs(os.path.join(run, 'final'))
        Path(d, 'payload', 'm.pt').write_text('3')
        Path(run, 'final', 'best.pt').write_text('best')
        fields = {'run_id': 'test-run', 'data_version': 'v1', 'model_version': 'm1', 'world_size': 8, 'global_step': 3,
                  'optimizer_boundary': True, 'scaler_used': False,
                  'derived_roles': {'rng': {'from': ['seed'], 'semantics': 'reseeded'}, 'data': {'from': ['epoch'], 'semantics': 'replayed'}}, **self.IDENT}
        with __import__('contextlib').redirect_stdout(__import__('io').StringIO()):
            self.cm.commit(d, fields, [{'path': 'payload/m.pt', 'roles': ['model', 'optimizer', 'scheduler', 'training_state']}])
        self.cm.finalize(run, 'test-run', 'v1', 'm1', 8, self.IDENT, 'completed', 3, ['final/best.pt'])
        os.makedirs(os.path.join(run, '000000000004-a2', 'payload'))         # a save cut off by a preemption
        script = render(fixture(), None, variant='verify', helpers=self.helpers)['run']
        script = script.replace("R=/checkpoints/test-run", f'R={run}').replace(' --mount-root /checkpoints', '')
        r = self.bash(script, {})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('FINISH-VERIFY {"artifacts": 1', r.stdout)
        self.assertIn('FINISH-PARTIAL 000000000004-a2', r.stdout)
        self.assertIn('FINISH-JOURNAL', r.stdout)
        prune = json.loads(r.stdout.split('FINISH-PRUNE ', 1)[1].splitlines()[0])
        self.assertEqual((prune['keep'], prune['prunable']), (['000000000003-a1'], []))    # one checkpoint: keep it
        Path(run, 'final', 'best.pt').write_text('changed')
        r = self.bash(script, {})
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertIn('changed since FINAL.json', r.stdout)
        os.remove(os.path.join(run, 'FINAL.json'))                       # a stalled, cancelled or crashed run
        r = self.bash(script, {})
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertIn('FINISH no readable FINAL.json', r.stdout)
        self.assertIn('FINISH-JOURNAL', r.stdout)                        # its lineage is still printed
        self.assertIn('FINISH-PARTIAL 000000000004-a2', r.stdout)


if __name__ == '__main__': unittest.main()
