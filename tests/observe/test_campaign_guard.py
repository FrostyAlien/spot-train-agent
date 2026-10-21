import sys as _sys
from pathlib import Path as _Path
SKILL = _Path(__file__).resolve().parents[2] / 'skills' / 'gpu-training-observe'   # the skill under test
_sys.path.insert(0, str(SKILL / 'scripts'))
import unittest

import json
import sys
import tempfile
import time
import types
from pathlib import Path

import campaign_guard as cg
from campaign_guard import check_config, check_sdk, decide, main

MIN = 60
_OD = tempfile.NamedTemporaryFile(suffix='.yaml', delete=False)
_OD.close()
OD_YAML = _OD.name   # an existing absolute path, as check_config requires


def cfg(**over):
    base = {'job_id': 7, 'deadline_utc': '2100-01-01T00:00:00Z', 'max_attempts': 2,
            'max_capacity_wait_minutes': 10, 'budget_usd': 8.0, 'ceiling_hourly_usd': 4.0,
            'reserved_usd': 0.5, 'poll_seconds': 60, 'shutdown_reserve_minutes': 5,
            'state_path': '/tmp/unused', 'max_blind_minutes': 15, 'notify_milestones': False}
    base.update(over)
    return check_config(base)


def run(c, observations, step=MIN, start=1000.0):
    state, history = {}, []
    for i, obs in enumerate(observations):
        actions, state = decide(c, state, obs, start + i * step)
        history.append(actions)
    return history, state


RUNNING = {'status': 'RUNNING', 'recovery_count': 0}


class GuardTests(unittest.TestCase):
    def test_healthy_run_has_no_actions(self):
        history, state = run(cfg(), [RUNNING] * 5)
        self.assertEqual(sum(history, []), [])
        self.assertAlmostEqual(state['spend_upper_usd'], 0.5 + 4.0 * 4 / 60, places=3)

    def test_capacity_wait_cancels_without_fallback(self):
        waiting = {'status': 'RECOVERING', 'recovery_count': 1}
        history, _ = run(cfg(), [RUNNING] + [waiting] * 12)
        first = next(a for a in history if a)
        self.assertEqual(first[0][0], 'cancel')
        self.assertIn('capacity wait', first[0][1])

    def test_capacity_wait_resets_when_running_again(self):
        waiting = {'status': 'RECOVERING', 'recovery_count': 1}
        obs = [waiting] * 6 + [{'status': 'RUNNING', 'recovery_count': 1}] + [waiting] * 6
        history, _ = run(cfg(), obs)
        self.assertEqual(sum(history, []), [])

    def test_attempts_limit(self):
        history, _ = run(cfg(), [RUNNING, {'status': 'RUNNING', 'recovery_count': 2}])
        self.assertIn('attempts 3 exceed 2', history[-1][0][1])

    def test_budget_upper_bound_includes_shutdown_reserve(self):
        history, _ = run(cfg(budget_usd=1.0), [RUNNING] * 10)
        first = next(i for i, a in enumerate(history) if a)
        self.assertEqual(history[first][0][0], 'cancel')
        self.assertLess(first, 10)

    def test_deadline(self):
        history, _ = run(cfg(deadline_utc='1970-01-01T00:20:00Z'), [RUNNING] * 5, start=1000.0, step=60)  # reaches t=1240
        self.assertEqual(history[-1][0], ('cancel', 'campaign deadline reached'))

    def test_no_deadline_by_default(self):
        c = {k: v for k, v in cfg(budget_usd=1000.0).items() if k != 'deadline_utc'}
        check_config(c)                                   # the budget is the hard stop
        history, state = run(c, [RUNNING] * 3, start=4102444800.0 + 3600)    # long past any 2100 deadline
        self.assertEqual(sum(history, []), [])
        history, _ = run({**c, 'budget_usd': 1.0}, [RUNNING] * 10)
        self.assertIn('reaches budget', next(a for a in history if a)[0][1])

    def test_notification_carries_the_spend_upper_bound(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'msg.json'
            c = cfg(notify_cmd=['sh', '-c', f'cat > {out}'])
            cg.run_action(c, 'notify', 'job terminal: SUCCEEDED', {'job_id': 7, 'spend_upper_usd': 1.234})
            msg = json.loads(out.read_text())
            self.assertEqual(msg['spend_upper_usd'], 1.234)
            self.assertIn('spend <= 1.23 USD (upper bound at the on-demand price', msg['message'])

    def test_terminal_status_notifies_once_and_stops(self):
        history, state = run(cfg(), [RUNNING, {'status': 'SUCCEEDED'}, RUNNING])
        self.assertEqual(history[1], [('notify', 'job terminal: SUCCEEDED')])
        self.assertEqual(history[2], [])
        self.assertTrue(state['done'])

    def test_approved_fallback_once_then_capacity_limit_applies(self):
        c = cfg(fallback={'approved': True, 'after_capacity_wait_minutes': 5, 'task_yaml': OD_YAML})
        waiting = {'status': 'PENDING', 'recovery_count': 0}
        history, state = run(c, [waiting] * 20)
        kinds = [a[0][0] for a in history if a]
        self.assertEqual(kinds[0], 'fallback')
        self.assertEqual(kinds[1], 'cancel')       # fallback also waited too long
        self.assertTrue(state['fallback_used'])

    def test_fallback_task_must_be_an_existing_absolute_path(self):
        for path in ('od.yaml', '/nonexistent/od.yaml'):
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, 'absolute path'):
                cfg(fallback={'approved': True, 'after_capacity_wait_minutes': 5, 'task_yaml': path})

    def test_no_fallback_after_the_max_wait_passed_while_blind(self):
        c = cfg(max_capacity_wait_minutes=30, fallback={'approved': True, 'after_capacity_wait_minutes': 10, 'task_yaml': OD_YAML})
        obs = [{'status': 'PENDING', 'recovery_count': 0}] * 2 + [None] * 40 + [{'status': 'PENDING', 'recovery_count': 0}]
        history, state = run(c, obs)
        self.assertEqual(history[-1][0][0], 'cancel')
        self.assertFalse(state['fallback_used'])

    def test_unapproved_fallback_rejected(self):
        with self.assertRaises(ValueError):
            cfg(fallback={'approved': False, 'after_capacity_wait_minutes': 5, 'task_yaml': OD_YAML})

    def test_blind_guard_keeps_charging_and_never_cancels(self):
        history, state = run(cfg(budget_usd=1.0), [RUNNING] + [None] * 20)
        kinds = [a[0] for acts in history for a in acts]
        self.assertNotIn('cancel', kinds)
        self.assertGreater(state['spend_upper_usd'], 1.0)
        notes = [a[1] for acts in history for a in acts if a[0] == 'notify']
        self.assertTrue(any('stop it by hand' in n for n in notes))

    def test_blind_without_limit_notifies_once(self):
        history, _ = run(cfg(budget_usd=1.0), [RUNNING] + [None] * 20)
        notes = [a[1] for acts in history for a in acts if a[0] == 'notify']
        self.assertEqual(sum('cannot be observed' in n for n in notes), 1)   # once, not every tick

    def test_fallback_never_fires_blind(self):
        c = cfg(fallback={'approved': True, 'after_capacity_wait_minutes': 5, 'task_yaml': OD_YAML})
        history, state = run(c, [{'status': 'PENDING', 'recovery_count': 0}] * 2 + [None] * 8)
        kinds = [a[0] for acts in history for a in acts]
        self.assertNotIn('fallback', kinds)
        self.assertNotIn('cancel', kinds)
        self.assertFalse(state['fallback_used'])

    def test_fallback_blocked_at_attempt_limit(self):
        c = cfg(max_attempts=1, fallback={'approved': True, 'after_capacity_wait_minutes': 5, 'task_yaml': OD_YAML})
        history, _ = run(c, [{'status': 'RECOVERING', 'recovery_count': 0}] * 7)
        first = next(a for a in history if a)
        self.assertEqual(first[0][0], 'cancel')
        self.assertIn('fallback would exceed', first[0][1])

    def test_fallback_counts_as_an_attempt(self):
        c = cfg(fallback={'approved': True, 'after_capacity_wait_minutes': 5, 'task_yaml': OD_YAML})
        obs = [{'status': 'PENDING', 'recovery_count': 0}] * 6 + [{'status': 'RUNNING', 'recovery_count': 1}]
        history, state = run(c, obs)
        self.assertEqual(state['attempts'], 3)
        self.assertIn('attempts 3 exceed 2', history[-1][0][1])

    def test_cancel_reissued_but_notified_once(self):
        history, _ = run(cfg(deadline_utc='1970-01-01T00:00:01Z'), [RUNNING] * 4)
        kinds = [a[0] for acts in history for a in acts]
        self.assertEqual(kinds.count('cancel'), 4)
        self.assertEqual(kinds.count('notify'), 1)

    def test_fallback_that_cannot_fire_is_rejected(self):
        fb = {'approved': True, 'after_capacity_wait_minutes': 120, 'task_yaml': OD_YAML}
        with self.assertRaisesRegex(ValueError, 'never fire'):
            cfg(budget_usd=8.0, ceiling_hourly_usd=4.57, max_capacity_wait_minutes=130, fallback=fb)
        with self.assertRaisesRegex(ValueError, 'must be less than'):
            cfg(max_capacity_wait_minutes=60, fallback=fb)
        with self.assertRaisesRegex(ValueError, 'must be less than'):
            cfg(max_capacity_wait_minutes=120, fallback=fb)

    def test_config_validation(self):
        for bad in ({'budget_usd': 0}, {'max_attempts': 0}, {'deadline_utc': '2100-01-01'},
                    {'ceiling_hourly_usd': True}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                cfg(**bad)
        for removed in ({'provider_stop_cmd': ['stop']}, {'spend_mode': 'assumed'}, {'reserve_usd': 0.5}):
            with self.subTest(removed=removed), self.assertRaisesRegex(ValueError, 'unknown guard config keys'):
                cfg(**removed)

    def test_self_test_cli(self):
        self.assertIsNone(main(['--self-test']))


    def test_normal_startup_is_not_capacity_wait(self):
        starting = {'status': 'STARTING', 'recovery_count': 0}
        history, state = run(cfg(startup_allowance_minutes=8), [starting] * 17 + [RUNNING])
        self.assertEqual(sum(history, []), [])             # 16 min waiting = 8 startup + 8 capacity < 10
        history, _ = run(cfg(startup_allowance_minutes=8), [starting] * 20)
        self.assertIn('capacity wait 10 min', next(a for a in history if a)[0][1])

    def test_spend_and_wait_count_from_submission(self):
        c = cfg(startup_allowance_minutes=0)
        actions, state = decide(c, {}, {'status': 'STARTING', 'recovery_count': 0, 'submitted_at': 1000.0}, 1000.0 + 11 * MIN)
        self.assertAlmostEqual(state['spend_upper_usd'], 0.5 + 4.0 * 11 / 60, places=3)
        self.assertEqual(actions[0][0], 'cancel')          # 11 min since submission > 10 min cap

    def test_campaign_carry_over(self):
        c = cfg(spent_before_usd=2.0, attempts_before=1)
        _, state = run(c, [RUNNING, RUNNING])
        self.assertEqual(state['attempts'], 2)
        self.assertAlmostEqual(state['spend_upper_usd'], 2.5 + 4.0 / 60, places=3)
        history, _ = run(c, [RUNNING, {'status': 'RUNNING', 'recovery_count': 1}])
        self.assertIn('attempts 3 exceed 2', history[-1][0][1])
        with self.assertRaisesRegex(ValueError, 'already reach'):
            cfg(spent_before_usd=7.5)

    def test_milestones_notify_on_status_changes_only(self):
        rec = {'status': 'RECOVERING', 'recovery_count': 1}
        back = {'status': 'RUNNING', 'recovery_count': 1}
        history, state = run(cfg(notify_milestones=True), [RUNNING, RUNNING, rec, rec, back, back])
        notes = [a[1] for acts in history for a in acts]
        self.assertEqual(notes, ['running (attempt 1)', 'recovering: allocation lost, reacquiring capacity (attempt 2 so far)',
                                 'running again after recovery (attempt 2)'])
        self.assertEqual([h['status'] for h in state['history']], ['RUNNING', 'RECOVERING', 'RUNNING'])

    def test_limit_outranks_milestone(self):
        history, _ = run(cfg(notify_milestones=True, deadline_utc='1970-01-01T00:00:01Z'), [RUNNING])
        self.assertEqual([a[0] for a in history[0]], ['cancel', 'notify'])
        self.assertIn('cancelling', history[0][1][1])

    def test_new_config_keys_validated(self):
        for bad in ({'startup_allowance_minutes': -1}, {'attempts_before': 1.5}, {'graceful_cancel_seconds': 400},
                    {'notify_milestones': 'yes'}, {'spent_before_usd': True}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                cfg(**bad)

    def test_ledger_of_another_job_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = cfg(state_path=str(Path(tmp) / 'state.json'))
            Path(c['state_path']).write_text(json.dumps({'job_id': 3, 'config_job_id': 3, 'done': True}))
            with self.assertRaisesRegex(ValueError, 'belongs to job 3'):
                cg.load_ledger(c)
            with self.assertRaisesRegex(ValueError, 'belongs to job 3'):
                main(['--config', _write_cfg(tmp, c), '--loop'])

    def test_first_cancel_is_graceful_then_plain(self):
        calls = []
        jobs = types.SimpleNamespace(cancel=lambda **kw: calls.append(kw) or 'req')
        fake = types.SimpleNamespace(jobs=jobs, stream_and_get=lambda req: None)
        saved = {k: sys.modules.get(k) for k in ('sky', 'sky.jobs')}
        sys.modules['sky'], sys.modules['sky.jobs'] = fake, jobs
        try:
            state = {'job_id': 7}
            cg.run_action(cfg(graceful_cancel_seconds=90), 'cancel', 'r', state)
            state['cancel_issued'] = True
            cg.run_action(cfg(graceful_cancel_seconds=90), 'cancel', 'r', state)
        finally:
            for k, v in saved.items():
                if v is None:
                    sys.modules.pop(k, None)
                else:
                    sys.modules[k] = v
        self.assertEqual(calls, [{'job_ids': [7], 'graceful': True, 'graceful_timeout': 90}, {'job_ids': [7]}])

    def test_tick_prints_one_observation_line(self):
        import io, contextlib
        with tempfile.TemporaryDirectory() as tmp:
            c = cfg(state_path=str(Path(tmp) / 'state.json'))
            original = cg.observe_skypilot
            cg.observe_skypilot = lambda job_id, *_: RUNNING
            out = io.StringIO()
            try:
                with contextlib.redirect_stdout(out):
                    cg.tick(c)
            finally:
                cg.observe_skypilot = original
            line = json.loads(out.getvalue().strip().splitlines()[0])
            ledger = json.loads(Path(c['state_path']).read_text())
        self.assertEqual((line['job_id'], line['status'], line['attempts']), (7, 'RUNNING', 1))
        self.assertEqual(ledger['config_job_id'], 7)


def _write_cfg(tmp, c):
    p = Path(tmp) / 'guard.json'
    p.write_text(json.dumps(c))
    return str(p)

def fake_sky(queue_params=('refresh', 'all_users', 'job_ids', 'fields'),
             cancel_params=('name', 'job_ids', 'graceful', 'graceful_timeout'), drop=()):
    jobs = types.SimpleNamespace()
    def make(params):
        ns = {}
        exec('def f(' + ', '.join(p + '=None' for p in params) + '): pass', ns)
        return ns['f']
    jobs.queue_v2, jobs.cancel, jobs.launch = make(queue_params), make(cancel_params), make(('task',))
    jobs.tail_logs = make(('name', 'job_id', 'follow', 'tail', 'output_stream'))
    for name in drop:
        delattr(jobs, name)
    task = types.SimpleNamespace(from_yaml=lambda path: None, update_secrets=lambda self, values: None)
    return types.SimpleNamespace(jobs=jobs, stream_and_get=lambda req: None, Task=task, __version__='test')


class SdkDriftTests(unittest.TestCase):
    def test_check_sdk_accepts_expected_api(self):
        self.assertEqual(check_sdk(fake_sky())['problems'], [])

    def test_check_sdk_reports_renamed_or_changed_calls(self):
        problems = check_sdk(fake_sky(drop=('queue_v2',), cancel_params=('name',)))['problems']
        self.assertIn('sky.jobs.queue_v2 missing', problems)
        self.assertIn('sky.jobs.cancel lacks parameter job_ids', problems)

    def test_failed_cancel_notifies(self):
        calls = []
        def fake_action(cfg_, action, reason, state):
            calls.append(action)
            if action == 'cancel':
                raise RuntimeError('API changed')
        with tempfile.TemporaryDirectory() as tmp:
            c = cfg(deadline_utc='1970-01-01T00:00:01Z', state_path=str(Path(tmp) / 'state.json'))
            original = (cg.observe_skypilot, cg.run_action)
            cg.observe_skypilot, cg.run_action = (lambda job_id, *_: RUNNING), fake_action
            try:
                cg.tick(c)
            finally:
                cg.observe_skypilot, cg.run_action = original
            log = json.loads(Path(c['state_path']).read_text())['log']
        self.assertEqual(calls, ['cancel', 'notify', 'notify'])   # escalation, then the cancel notice
        self.assertFalse(log[0]['ok'])

    def test_tick_follows_the_fallback_job(self):
        observed = []
        def fake_observe(job_id, *_):
            observed.append(job_id)
            return {7: {'status': 'PENDING', 'recovery_count': 0}, 99: RUNNING}[job_id] \
                if not (job_id == 7 and len(observed) > 1) else {'status': 'CANCELLED', 'recovery_count': 0}
        def fake_action(cfg_, action, reason, state):
            if action == 'fallback':
                state['replaced_job'] = state['job_id']
                return 99
        with tempfile.TemporaryDirectory() as tmp:
            c = cfg(state_path=str(Path(tmp) / 'state.json'),
                    fallback={'approved': True, 'after_capacity_wait_minutes': 5, 'task_yaml': OD_YAML})
            now = time.time()
            Path(c['state_path']).write_text(json.dumps({'wait_started': now - 360, 'last_tick': now - 60}))
            cancelled = []
            original = (cg.observe_skypilot, cg.run_action, cg.cancel_replaced)
            cg.observe_skypilot, cg.run_action = fake_observe, fake_action
            cg.cancel_replaced = lambda cfg_, st: cancelled.append(st['replaced_job'])
            try:
                first = cg.tick(c)
                self.assertEqual(json.loads(Path(c['state_path']).read_text())['replaced_job'], 7)
                self.assertEqual(cancelled, [])              # not before the fallback is seen alive
                second = cg.tick(c)
            finally:
                cg.observe_skypilot, cg.run_action, cg.cancel_replaced = original
            state = json.loads(Path(c['state_path']).read_text())
        self.assertEqual(first[0][0], 'fallback')
        self.assertEqual(observed, [7, 99])
        self.assertEqual(cancelled, [7])
        self.assertNotIn('replaced_job', state)
        self.assertEqual(second, [])
        self.assertEqual(state['job_id'], 99)
        self.assertFalse(state['done'])

    def test_unlisted_job_is_blind_not_finished(self):
        seen = {}
        jobs = types.SimpleNamespace(queue_v2=lambda **kw: seen.update(kw) or 'req')
        fake = types.SimpleNamespace(jobs=jobs, stream_and_get=lambda req: [])
        saved = {k: sys.modules.get(k) for k in ('sky', 'sky.jobs')}
        sys.modules['sky'], sys.modules['sky.jobs'] = fake, jobs
        try:
            self.assertIsNone(cg.observe_skypilot(7))
            self.assertIs(seen['all_users'], True)      # the guard host is usually another SkyPilot user
        finally:
            for k, v in saved.items():
                if v is None:
                    sys.modules.pop(k, None)
                else:
                    sys.modules[k] = v

    def test_launched_job_id_shapes(self):
        self.assertEqual(cg._launched_job_id(([5], object())), 5)
        self.assertEqual(cg._launched_job_id([5]), 5)
        for bad in (None, ([], None), ([1, 2], None), (True, None)):
            with self.assertRaises(RuntimeError):
                cg._launched_job_id(bad)

class FixRoundTests(unittest.TestCase):
    FB = {'approved': True, 'after_capacity_wait_minutes': 5, 'task_yaml': OD_YAML}

    def test_fallback_attempts_count_attempts_before_once(self):
        c = cfg(attempts_before=1, max_attempts=4, fallback=self.FB)
        obs = [{'status': 'PENDING', 'recovery_count': 0}] * 6 + [RUNNING]
        history, state = run(c, obs)
        self.assertEqual(history[5][0][0], 'fallback')
        self.assertEqual(state['attempts'], 3)          # 1 earlier job + the primary + the fallback

    def test_blind_first_ticks_still_charge_from_submission(self):
        c = cfg(startup_allowance_minutes=0)
        _, st = decide(c, {}, None, 1000.0)
        _, st = decide(c, st, {'status': 'RUNNING', 'recovery_count': 0, 'submitted_at': 700.0}, 1060.0)
        self.assertAlmostEqual(st['billable_seconds'], 360.0)          # 300 before the guard + 60 since
        _, st = decide(c, st, {'status': 'RUNNING', 'recovery_count': 0, 'submitted_at': 700.0}, 1120.0)
        self.assertAlmostEqual(st['billable_seconds'], 420.0)          # charged once, not every tick

    def _tick_with(self, c, observe, action, state=None):
        original = (cg.observe_skypilot, cg.run_action)
        cg.observe_skypilot, cg.run_action = observe, action
        try:
            if state is not None:
                Path(c['state_path']).write_text(json.dumps(state))
            acts = cg.tick(c)
        finally:
            cg.observe_skypilot, cg.run_action = original
        return acts, json.loads(Path(c['state_path']).read_text())

    def test_failed_fallback_keeps_the_primary_and_retries_three_times(self):
        calls = []
        def failing(cfg_, action, reason, state):
            calls.append(action)
            if action == 'fallback':
                raise RuntimeError('quota')
        waiting = lambda job_id, *_: {'status': 'PENDING', 'recovery_count': 0}
        with tempfile.TemporaryDirectory() as tmp:
            c = cfg(state_path=str(Path(tmp) / 'state.json'), max_capacity_wait_minutes=60, budget_usd=20.0,
                    fallback=self.FB)
            now = time.time()
            _, st = self._tick_with(c, waiting, failing, {'wait_started': now - 600, 'last_tick': now - 60})
            self.assertEqual((st['job_id'], st['fallback_used'], st['fallback_failures']), (7, False, 1))
            self.assertIsNotNone(st['wait_started'])
            self.assertNotIn('switched', json.dumps(st['log']))
            for _ in range(2):
                _, st = self._tick_with(c, waiting, failing)
        self.assertEqual(calls.count('fallback'), 3)
        self.assertTrue(st['fallback_used'])            # gave up; the spot job keeps waiting under the limits
        self.assertIn('no more fallback tries', st['log'][-1]['reason'])

    def fake_api(self, queue, launch_raises=False):
        """A fake sky module: queue_v2 returns `queue`; launch appends a job to it (accepted), then
        optionally loses the reply."""
        log = []
        task = types.SimpleNamespace(name='r1-od', envs={'SECRET_NAMES': 'WANDB_API_KEY'},
                                     update_secrets=lambda values: log.append(('secrets', sorted(values))))
        def launch(t):
            queue.append({'job_id': 40 + len(queue), 'job_name': 'r1-od', 'status': 'PENDING', 'submitted_at': time.time()})
            log.append('launch')
            return ('L', queue[-1]['job_id'])
        def stream_and_get(req):
            if isinstance(req, tuple) and req[0] == 'L':
                if launch_raises:
                    raise ConnectionError('reply lost')
                return [req[1]]
            return list(queue) if req == 'Q' else None
        jobs = types.SimpleNamespace(launch=launch, queue_v2=lambda **kw: 'Q',
                                     cancel=lambda **kw: log.append(('cancel', kw['job_ids'])) or 'C')
        fake = types.SimpleNamespace(jobs=jobs, Task=types.SimpleNamespace(from_yaml=lambda p: task),
                                     stream_and_get=stream_and_get)
        return fake, jobs, log

    def with_modules(self, fake, jobs, fn):
        saved = {k: sys.modules.get(k) for k in ('sky', 'sky.jobs')}
        sys.modules['sky'], sys.modules['sky.jobs'] = fake, jobs
        try:
            return fn()
        finally:
            for k, v in saved.items():
                if v is None:
                    sys.modules.pop(k, None)
                else:
                    sys.modules[k] = v

    def test_fallback_launches_then_cancels_the_primary_later(self):
        queue = []
        fake, jobs, log = self.fake_api(queue)
        def body():
            with tempfile.TemporaryDirectory() as tmp:
                sec = Path(tmp) / 's.sh'
                sec.write_text('echo WANDB_API_KEY=k1\n')
                c = cfg(fallback={**self.FB, 'secrets_cmd': ['bash', str(sec)]})
                state = {'job_id': 7, 'fallback_started_at': time.time()}
                self.assertEqual(cg.run_action(c, 'fallback', 'r', state), 40)
                self.assertEqual(log, [('secrets', ['WANDB_API_KEY']), 'launch'])     # no cancel yet
                self.assertEqual(state['replaced_job'], 7)
                self.assertEqual(cg.run_action(c, 'fallback', 'r', {'job_id': 7, 'fallback_started_at': time.time()}), 40)
                self.assertEqual(log.count('launch'), 1)                              # adopted, not relaunched
                cg.cancel_replaced(c, state)
                self.assertEqual(log[-1], ('cancel', [7]))
                queue.clear()
                sec.write_text('echo OTHER=1\n')
                with self.assertRaisesRegex(RuntimeError, 'secrets missing'):
                    cg.run_action(c, 'fallback', 'r', {'job_id': 7})
        self.with_modules(fake, jobs, body)

    def test_lost_launch_reply_is_adopted_not_repeated(self):
        queue = []
        fake, jobs, log = self.fake_api(queue, launch_raises=True)
        waiting = lambda job_id, *_: {'status': 'PENDING', 'recovery_count': 0}
        def body():
            with tempfile.TemporaryDirectory() as tmp:
                c = cfg(state_path=str(Path(tmp) / 'state.json'), max_capacity_wait_minutes=60, budget_usd=20.0,
                        fallback={**self.FB, 'secrets_cmd': ['echo', 'WANDB_API_KEY=k']})
                now = time.time()
                original = (cg.observe_skypilot, cg.run_action)
                cg.observe_skypilot = waiting
                cg.run_action = lambda cfg_, a, r, st: original[1](cfg_, a, r, st) if a != 'notify' else None
                try:
                    Path(c['state_path']).write_text(json.dumps({'wait_started': now - 600, 'last_tick': now - 60}))
                    cg.tick(c)                                   # accepted, reply lost: recorded as a failure
                    st = json.loads(Path(c['state_path']).read_text())
                    self.assertEqual((st['job_id'], st['fallback_failures']), (7, 1))
                    cg.tick(c)                                   # adopts job 40 instead of launching again
                    st = json.loads(Path(c['state_path']).read_text())
                finally:
                    cg.observe_skypilot, cg.run_action = original
                self.assertEqual(log.count('launch'), 1)
                self.assertEqual((st['job_id'], st['replaced_job']), (40, 7))
        self.with_modules(fake, jobs, body)

    def test_guard_death_after_launch_adopts_on_restart(self):
        queue = []
        fake, jobs, log = self.fake_api(queue)
        saves = []
        def body():
            with tempfile.TemporaryDirectory() as tmp:
                c = cfg(state_path=str(Path(tmp) / 'state.json'), max_capacity_wait_minutes=60, budget_usd=20.0,
                        fallback={**self.FB, 'secrets_cmd': ['echo', 'WANDB_API_KEY=k']})
                now = time.time()
                original = (cg.observe_skypilot, cg.run_action, cg._save)
                cg.observe_skypilot = lambda job_id, *_: {'status': 'PENDING', 'recovery_count': 0}
                cg.run_action = lambda cfg_, a, r, st: original[1](cfg_, a, r, st) if a != 'notify' else None
                cg._save = lambda path, st: saves.append(json.loads(json.dumps(st))) or original[2](path, st)
                try:
                    Path(c['state_path']).write_text(json.dumps({'wait_started': now - 600, 'last_tick': now - 60}))
                    cg.tick(c)
                    Path(c['state_path']).write_text(json.dumps(saves[0]))   # died before the final save
                    self.assertIn('fallback_started_at', saves[0])
                    self.assertNotIn('replaced_job', saves[0])
                    cg.tick(c)
                    st = json.loads(Path(c['state_path']).read_text())
                finally:
                    cg.observe_skypilot, cg.run_action, cg._save = original
                self.assertEqual(log.count('launch'), 1)
                self.assertEqual(st['job_id'], 40)
        self.with_modules(fake, jobs, body)

    def test_fallback_that_ends_at_once_returns_to_the_primary(self):
        def observe(job_id, *_):
            return {7: {'status': 'PENDING', 'recovery_count': 0}, 99: {'status': 'FAILED_PRECHECKS', 'recovery_count': 0}}[job_id]
        notes = []
        def action(cfg_, a, r, st):
            if a == 'notify':
                notes.append(r)
            if a == 'fallback':
                st['replaced_job'] = st['job_id']
                return 99
        with tempfile.TemporaryDirectory() as tmp:
            c = cfg(state_path=str(Path(tmp) / 'state.json'), max_capacity_wait_minutes=60, budget_usd=20.0,
                    max_attempts=3, fallback=self.FB)
            now = time.time()
            original = (cg.observe_skypilot, cg.run_action, cg.cancel_replaced)
            cg.observe_skypilot, cg.run_action = observe, action
            cg.cancel_replaced = lambda *a: self.fail('must not cancel the primary')
            try:
                Path(c['state_path']).write_text(json.dumps({'wait_started': now - 600, 'last_tick': now - 60}))
                cg.tick(c)
                cg.tick(c)
            finally:
                cg.observe_skypilot, cg.run_action, cg.cancel_replaced = original
            st = json.loads(Path(c['state_path']).read_text())
        self.assertEqual((st['job_id'], st['done'], st['fallback_used'], st['attempts']), (7, False, True, 2))
        self.assertTrue(any('ended at once' in n for n in notes))

    def test_pending_fallback_without_submitted_at_is_adopted(self):
        queue = [{'job_id': 41, 'job_name': 'r1-od', 'status': 'PENDING', 'submitted_at': None},
                 {'job_id': 5, 'job_name': 'r1-od', 'status': 'PENDING', 'submitted_at': None}]   # older id: not ours
        fake, jobs, log = self.fake_api(queue)
        def body():
            c = cfg(fallback={**self.FB, 'secrets_cmd': ['echo', 'WANDB_API_KEY=k']})
            st = {'job_id': 7, 'fallback_started_at': time.time()}
            self.assertEqual(cg.run_action(c, 'fallback', 'r', st), 41)
            self.assertNotIn('launch', log)
            queue[0]['status'] = 'CANCELLED'
            self.assertEqual(cg.find_launched('r1-od', 7), None)       # job 5 predates the primary
        self.with_modules(fake, jobs, body)

    def test_both_jobs_charged_until_the_primary_is_cancelled(self):
        c = cfg(startup_allowance_minutes=0)
        _, st = decide(c, {}, RUNNING, 1000.0)
        st['replaced_job'] = 3
        _, st = decide(c, st, None, 1060.0)                     # blind: the primary may have capacity too
        self.assertAlmostEqual(st['billable_seconds'], 120.0)
        st['double_charge_once'] = True; del st['replaced_job']
        _, st = decide(c, st, RUNNING, 1120.0)                  # the tick that cancels it still charges both
        _, st = decide(c, st, RUNNING, 1180.0)
        self.assertAlmostEqual(st['billable_seconds'], 300.0)

    def test_blocked_sdk_call_is_not_repeated(self):
        fake = types.SimpleNamespace(stream_and_get=lambda req: time.sleep(1.5))
        saved = sys.modules.get('sky'); sys.modules['sky'] = fake
        try:
            with self.assertRaises(TimeoutError):
                cg._get(lambda: 'req', timeout=0.1)
            with self.assertRaisesRegex(TimeoutError, 'still blocked'):
                cg._get(lambda: 'req', timeout=0.1)
            time.sleep(1.6)
            fake.stream_and_get = lambda req: 'ok'
            self.assertEqual(cg._get(lambda: 'req', timeout=1), 'ok')
        finally:
            cg._STUCK.clear()
            if saved is None:
                sys.modules.pop('sky', None)
            else:
                sys.modules['sky'] = saved

    def test_loop_restarts_when_a_call_stays_blocked(self):
        import threading, io, contextlib
        never = threading.Event()
        jobs = types.SimpleNamespace(queue_v2=lambda **kw: 'req')
        fake = types.SimpleNamespace(jobs=jobs, stream_and_get=lambda req: never.wait())
        notes = []
        def body():
            with tempfile.TemporaryDirectory() as tmp:
                c = cfg(state_path=str(Path(tmp) / 'state.json'), sdk_timeout_seconds=1, max_blind_minutes=0.01,
                        poll_seconds=0)
                original = cg.run_action
                cg.run_action = lambda cfg_, a, r, st: notes.append(r)
                start = time.time()
                try:
                    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                        code = main(['--config', _write_cfg(tmp, c), '--loop'])
                finally:
                    cg.run_action = original
                    never.set()
                    cg._STUCK.clear()
                self.assertEqual(code, 1)                       # supervisor restarts a clean process
                self.assertLess(time.time() - start, 10)
                self.assertTrue(any('blocked' in n for n in notes))
        self.with_modules(fake, jobs, body)

    def test_exhausted_attempts_refused_before_launch(self):
        with self.assertRaisesRegex(ValueError, 'already used 2 of 2'):
            cfg(attempts_before=2, max_attempts=2)

    def test_silent_api_server_times_out(self):
        fake = types.SimpleNamespace(stream_and_get=lambda req: time.sleep(1))
        saved = sys.modules.get('sky'); sys.modules['sky'] = fake
        try:
            start = time.time()
            with self.assertRaises(TimeoutError):
                cg._get(lambda: 'req', timeout=0.2)
            self.assertLess(time.time() - start, 0.9)
        finally:
            cg._STUCK.clear()
            if saved is None:
                sys.modules.pop('sky', None)
            else:
                sys.modules['sky'] = saved

    def test_close_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            w = Path(tmp)
            m = {'run_id': 'r1', 'purchase_model': 'spot',
                 'budget': {'deadline_utc': '2100-01-01T00:00:00Z', 'total_limit': 15.0, 'ceiling_hourly_usd': 4.0},
                 'recovery': {'max_attempts': 2, 'max_capacity_wait_minutes': 30}}
            cg.init_config(m, 3, w, attended=True)
            original = cg.observe_skypilot
            try:
                cg.observe_skypilot = lambda job_id, *_: RUNNING
                with self.assertRaisesRegex(ValueError, 'cancel it first'):
                    cg.close_job(w, 3)
                cg.observe_skypilot = lambda job_id, *_: None
                with self.assertRaisesRegex(ValueError, '--reason'):
                    cg.close_job(w, 3)
                import io, contextlib
                ledger = json.loads((w / 'campaign.json').read_text())
                ledger['jobs'][0]['registered_at'] = time.time() - 3600
                (w / 'campaign.json').write_text(json.dumps(ledger))
                with contextlib.redirect_stdout(io.StringIO()):
                    out = cg.close_job(w, 3, reason='console shows no VM')
                self.assertEqual(out['closed'], 'by hand')
                self.assertAlmostEqual(out['spend_upper_usd'], 4.0, places=1)    # one hour since registration
                cg.init_config(m, 4, w, attended=True)            # the campaign can continue
            finally:
                cg.observe_skypilot = original

    def test_concurrent_tick_is_skipped(self):
        import fcntl
        with tempfile.TemporaryDirectory() as tmp:
            c = cfg(state_path=str(Path(tmp) / 'state.json'))
            with open(c['state_path'] + '.lock', 'w') as held:
                fcntl.flock(held, fcntl.LOCK_EX)
                self.assertEqual(cg.tick(c), [])
                self.assertFalse(Path(c['state_path']).exists())

    def contract(self, **rec):
        return {'run_id': 'r1', 'purchase_model': 'spot',
                'budget': {'deadline_utc': '2100-01-01T00:00:00Z', 'total_limit': 15.0, 'ceiling_hourly_usd': 4.57,
                           'controller_allowance': 0.5},
                'recovery': {'max_attempts': 2, 'max_capacity_wait_minutes': 30, 'planned_drills': 1, **rec}}

    def test_init_config_carries_the_campaign_over(self):
        with tempfile.TemporaryDirectory() as tmp:
            w = Path(tmp)
            out = cg.init_config(self.contract(), 4, w, notify_cmd=['true'])
            cfg4 = json.loads(Path(out['guard_config']).read_text())
            self.assertEqual((cfg4['max_attempts'], cfg4['spent_before_usd'], cfg4['attempts_before']), (3, 0.0, 0))
            with self.assertRaisesRegex(ValueError, 'not finished'):
                cg.init_config(self.contract(), 5, w, notify_cmd=['true'])
            Path(cfg4['state_path']).parent.mkdir(parents=True, exist_ok=True)
            Path(cfg4['state_path']).write_text(json.dumps({'done': True, 'billable_seconds': 3600, 'attempts': 2}))
            out5 = cg.init_config(self.contract(), 5, w, notify_cmd=['true'])
            self.assertEqual((out5['spent_before_usd'], out5['attempts_before']), (4.57, 2))
            with self.assertRaisesRegex(ValueError, 'already registered'):
                cg.init_config(self.contract(), 5, w, notify_cmd=['true'])
            Path(json.loads(Path(out5['guard_config']).read_text())['state_path']).write_text(
                json.dumps({'done': True, 'billable_seconds': 0, 'attempts': 3}))
            with self.assertRaisesRegex(ValueError, 'already used 3 of 3'):     # no attempt left for a relaunch
                cg.init_config(self.contract(), 0, w, notify_cmd=['true'], dry_run=True)
            m = self.contract(max_attempts=4)
            self.assertEqual(cg.init_config(m, 0, w, notify_cmd=['true'], dry_run=True)['attempts_before'], 3)
            self.assertEqual(len(json.loads((w / 'campaign.json').read_text())['jobs']), 2)

    def test_init_config_without_deadline(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = self.contract()
            del m['budget']['deadline_utc']
            out = cg.init_config(m, 1, Path(tmp), notify_cmd=['true'], dry_run=True)
            self.assertIsNone(out['deadline_utc'])
            self.assertNotIn('deadline_utc', out['config'])
            m['budget']['deadline_utc'] = '2100-01-01'
            with self.assertRaisesRegex(ValueError, 'ending in Z'):
                cg.init_config(m, 1, Path(tmp), notify_cmd=['true'], dry_run=True)

    def test_init_config_requires_notify_or_attended(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, 'notify-cmd'):
                cg.init_config(self.contract(), 1, Path(tmp))
            out = cg.init_config(self.contract(), 1, Path(tmp), attended=True, dry_run=True)
            self.assertFalse(out['notify'])

    def test_init_config_on_demand_default_wait_and_fallback_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = self.contract(max_capacity_wait_minutes=None); m['purchase_model'] = 'on-demand'
            out = cg.init_config(m, 1, Path(tmp), attended=True, dry_run=True)
            self.assertEqual(out['config']['max_capacity_wait_minutes'], cg.DEFAULT_ON_DEMAND_WAIT_MINUTES)
            m = self.contract(max_capacity_wait_minutes=None)
            with self.assertRaisesRegex(ValueError, 'required for a spot run'):
                cg.init_config(m, 1, Path(tmp), attended=True, dry_run=True)
            od = Path(tmp) / 'od.json'
            od.write_text(json.dumps({'envs': {'SECRET_NAMES': 'WANDB_API_KEY'}}))
            m = self.contract(allow_on_demand=True, on_demand_fallback={'approved': True, 'after_capacity_wait_minutes': 10})
            with self.assertRaisesRegex(ValueError, 'secrets-cmd'):
                cg.init_config(m, 1, Path(tmp), attended=True, fallback_task=str(od), dry_run=True)
            with self.assertRaisesRegex(ValueError, 'does not print'):
                cg.init_config(m, 1, Path(tmp), attended=True, fallback_task=str(od), dry_run=True,
                               secrets_cmd=['echo', 'OTHER=1'])
            out = cg.init_config(m, 1, Path(tmp), attended=True, fallback_task=str(od), dry_run=True,
                                 secrets_cmd=['echo', 'WANDB_API_KEY=x'])
            self.assertEqual(out['config']['fallback']['secrets_cmd'], ['echo', 'WANDB_API_KEY=x'])

    def test_missing_notify_warns(self):
        import io, contextlib
        with tempfile.TemporaryDirectory() as tmp:
            c = cfg(state_path=str(Path(tmp) / 'state.json'))
            original = cg.observe_skypilot
            cg.observe_skypilot = lambda job_id, *_: {'status': 'SUCCEEDED', 'recovery_count': 0}
            err = io.StringIO()
            try:
                with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
                    main(['--config', _write_cfg(tmp, c), '--once'])
            finally:
                cg.observe_skypilot = original
        self.assertIn('no notify_cmd', err.getvalue())


class ProgressAndBillingTests(unittest.TestCase):
    def test_allocations_without_a_commit_stop_the_run(self):
        c = cfg(max_attempts_without_progress=2, budget_usd=100.0, max_capacity_wait_minutes=60)
        run_no = {'status': 'RUNNING', 'recovery_count': 0, 'committed': False}
        obs = [run_no, {'status': 'RECOVERING', 'recovery_count': 1},
               {'status': 'RUNNING', 'recovery_count': 1, 'committed': False}, {'status': 'RECOVERING', 'recovery_count': 2}]
        history, state = run(c, obs)
        self.assertEqual(state['stalled'], 2)
        self.assertIn('2 allocations in a row ended without a new checkpoint', history[-1][0][1])

    def test_progress_resets_the_count_and_no_absolute_cap(self):
        c = cfg(max_attempts=None, max_attempts_without_progress=2, budget_usd=1000.0, max_capacity_wait_minutes=60)
        obs = []
        for rc in range(6):                            # six preemptions, each after a commit
            obs += [{'status': 'RUNNING', 'recovery_count': rc, 'committed': True},
                    {'status': 'RECOVERING', 'recovery_count': rc + 1}]
        history, state = run(c, obs)
        self.assertEqual((state['stalled'], state['attempts']), (0, 7))
        self.assertEqual([a for acts in history for a in acts if a[0] == 'cancel'], [])
        unreadable = cfg(max_attempts=None, max_attempts_without_progress=2, budget_usd=1000.0, max_capacity_wait_minutes=60)
        obs = []
        for rc in range(5):                             # the log can never be read: nothing counts
            obs += [{'status': 'RUNNING', 'recovery_count': rc, 'committed': None},
                    {'status': 'RECOVERING', 'recovery_count': rc + 1}]
        history, st = run(unreadable, obs)
        self.assertEqual(st.get('stalled', 0), 0)
        self.assertEqual([a for acts in history for a in acts if a[0] == 'cancel'], [])

    def test_waiting_without_vm_is_free_under_billing_rules(self):
        c = cfg(free_without_vm=True, startup_allowance_minutes=0, max_capacity_wait_minutes=600, budget_usd=100.0)
        pend = {'status': 'PENDING', 'recovery_count': 0, 'submitted_at': 400.0}
        rec_free = {'status': 'RECOVERING', 'recovery_count': 1, 'vms': 0}
        rec_unknown = {'status': 'RECOVERING', 'recovery_count': 1, 'vms': None}
        _, st = decide(c, {}, pend, 1000.0)                  # submitted PENDING: nothing billed before the guard
        _, st = decide(c, st, pend, 1060.0)
        self.assertEqual(st['billable_seconds'], 0.0)
        _, st = decide(c, st, RUNNING, 1120.0)               # the interval into RUNNING is charged
        _, st = decide(c, st, rec_free, 1180.0)
        _, st = decide(c, st, rec_free, 1240.0)              # both ends free: not charged
        _, st = decide(c, st, rec_unknown, 1300.0)
        self.assertAlmostEqual(st['billable_seconds'], 180.0)
        self.assertAlmostEqual(st['free_seconds'], 120.0)
        strict = cfg(startup_allowance_minutes=0, max_capacity_wait_minutes=600, budget_usd=100.0)
        _, st = decide(strict, {}, pend, 1000.0)
        _, st = decide(strict, st, pend, 1060.0)
        self.assertAlmostEqual(st['billable_seconds'], 660.0)   # without the rule every second counts

    def test_billing_rule_edges_charge_when_a_vm_may_exist(self):
        c = cfg(free_without_vm=True, startup_allowance_minutes=0, max_capacity_wait_minutes=600, budget_usd=100.0)
        late = {'status': 'RECOVERING', 'recovery_count': 1, 'vms': 0, 'submitted_at': 400.0}
        _, st = decide(c, {}, late, 1000.0)              # a late guard: RECOVERING proves a VM ran before
        self.assertAlmostEqual(st['billable_seconds'], 600.0)
        pend = {'status': 'PENDING', 'recovery_count': 0}
        _, st = decide(c, {}, pend, 1000.0)
        st['replaced_job'] = 3                            # the replaced primary may hold a VM
        _, st = decide(c, st, pend, 1060.0)
        _, st = decide(c, st, pend, 1120.0)
        self.assertAlmostEqual(st['billable_seconds'], 240.0)    # charged twice over, never free

    def test_log_read_runs_in_a_killable_child(self):
        original = cg._log_cmd
        answer = lambda text: [sys.executable, '-c', f'print("INFO: server healthy"); print({text!r})']
        try:
            cg._log_cmd = lambda job_id: [sys.executable, '-c', 'import time; time.sleep(30)']
            start = time.time()
            self.assertIsNone(cg.read_log(7, timeout=1))
            self.assertLess(time.time() - start, 5)
            self.assertEqual(cg.stuck_minutes(), 0.0)           # nothing left blocking later SDK calls
            cg._log_cmd = lambda job_id: answer('GUARD-LOG-READ {"committed": true, "evals": ["EVAL step=6 {}"]}')
            self.assertEqual(cg.read_log(7), {'committed': True, 'evals': ['EVAL step=6 {}']})
            cg._log_cmd = lambda job_id: answer('GUARD-LOG-READ {"committed": false, "evals": []}')
            self.assertIs(cg.read_log(7)['committed'], False)
            for bad in ('YES', 'GUARD-LOG-READ YES', 'GUARD-LOG-READ {"evals": []}'):
                cg._log_cmd = lambda job_id, bad=bad: answer(bad)
                self.assertIsNone(cg.read_log(7), bad)
            cg._log_cmd = lambda job_id: [sys.executable, '-c', 'raise SystemExit(3)']
            self.assertIsNone(cg.read_log(7))
        finally:
            cg._log_cmd = original
        self.assertIn('CHECKPOINT_COMMITTED', cg._log_cmd(7))

    def test_log_read_child_finds_commit_and_eval_lines(self):
        with tempfile.TemporaryDirectory() as tmp:          # a stand-in `sky` package for the child
            (Path(tmp) / 'sky').mkdir()
            (Path(tmp) / 'sky' / '__init__.py').write_text('from . import jobs\n')
            (Path(tmp) / 'sky' / 'jobs.py').write_text(
                'def tail_logs(job_id, follow, tail, output_stream):\n'
                '    output_stream.write("(r1, pid=3) CHECKPOINT_COMMITTED {}\\n\\x1b[36m(r1)\\x1b[0m \\r 40%\\rEVAL step=6 {\\"l\\": 1}\\n"\n'
                '        "(r1) PROGRESS 2026-10-04T10:00:00Z step=1 phase=startup age=0\\n(r1) say PROGRESS later\\n"\n'
                '        "(r1) PROGRESS 2026-10-04T10:05:00Z step=6 phase=train age=1\\n")\n')
            out = __import__('subprocess').run(cg._log_cmd(7), capture_output=True, text=True, env={**__import__('os').environ, 'PYTHONPATH': tmp})
        line = [ln for ln in out.stdout.splitlines() if ln.startswith('GUARD-LOG-READ ')][-1]
        self.assertEqual(json.loads(line.split(' ', 1)[1]), {'committed': True, 'evals': ['EVAL step=6 {"l": 1}'],
                                                             'progress': 'PROGRESS 2026-10-04T10:05:00Z step=6 phase=train age=1',
                                                             'stall': None})

    def test_a_stall_line_is_notified_once(self):
        line = 'STALL 2026-10-04T10:00:00Z rank=1 phase=eval count=3 step=1200 gap=900 rule=b'
        obs = {'status': 'RUNNING', 'recovery_count': 0, 'committed': True, 'evals': [], 'stall': line}
        history, _ = run(cfg(budget_usd=100.0), [obs, obs, {'status': 'FAILED', 'recovery_count': 0}])
        notes = [a[1] for acts in history for a in acts if a[0] == 'notify']
        self.assertEqual(len(notes), 2, notes)
        self.assertTrue(notes[0].startswith(line) and 'exit 93' in notes[0])
        self.assertEqual(notes[1], 'job terminal: FAILED')

    def test_a_wedged_worker_is_notified_then_cancelled_after_the_grace(self):
        utc = lambda t: __import__('time').strftime('%Y-%m-%dT%H:%M:%SZ', __import__('time').gmtime(t))
        prog = lambda t: {'status': 'RUNNING', 'recovery_count': 0, 'committed': True, 'evals': [],
                          'progress': f'PROGRESS {utc(t)} step=40 phase=train age=3'}
        failed_read = {'status': 'RUNNING', 'recovery_count': 0, 'committed': None, 'evals': []}
        c = cfg(budget_usd=1000.0, max_capacity_wait_minutes=60, wedged_grace_minutes=15)
        history, _ = run(c, [prog(1000.0)] + [failed_read] * 40, start=1000.0, step=60)   # reads fail: notice only
        self.assertFalse(any(a[0] == 'cancel' for acts in history for a in acts))
        self.assertTrue(any('cannot be read' in a[1] for acts in history for a in acts))
        obs = [prog(1000.0)] * 41                                    # readable, and the line stopped advancing
        history, st = run(c, obs, start=1000.0, step=60)
        notes = [(i, a[1]) for i, acts in enumerate(history) for a in acts if a[0] == 'notify']
        self.assertTrue(any('worker wedged' in n for _, n in notes), notes)
        first = next(i for i, n in notes if 'worker wedged' in n)
        self.assertEqual(first, 16)                                 # older than 3 x 300 s
        cancel = next(i for i, acts in enumerate(history) if any(a[0] == 'cancel' for a in acts))
        self.assertEqual(cancel, first + 15)
        self.assertIn('worker wedged', history[cancel][0][1])

    def test_skipped_log_reads_keep_the_wedged_clock(self):
        utc = lambda t: __import__('time').strftime('%Y-%m-%dT%H:%M:%SZ', __import__('time').gmtime(t))
        prog = {'status': 'RUNNING', 'recovery_count': 0, 'committed': True, 'evals': [],
                'progress': f'PROGRESS {utc(1000.0)} step=40 phase=train age=3'}
        skipped = {'status': 'RUNNING', 'recovery_count': 0, 'log_read': 'skipped'}
        c = cfg(budget_usd=1000.0, max_capacity_wait_minutes=60, wedged_grace_minutes=15, log_read_seconds=120)
        history, _ = run(c, [prog, skipped] * 21, start=1000.0, step=60)
        cancel = next(i for i, acts in enumerate(history) if any(a[0] == 'cancel' for a in acts))
        self.assertEqual(cancel, 31)                                # first notice at 16, + 15 min, read or not
        with self.assertRaises(ValueError):
            cfg(log_read_seconds=0)

    def test_log_read_seconds_reads_less_often(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = cfg(state_path=str(Path(tmp) / 'state.json'), budget_usd=100.0, log_read_seconds=120)
            reads, original = [], (cg.observe_skypilot, cg.read_log)
            try:
                cg.observe_skypilot = lambda job_id, *_: {'status': 'RUNNING', 'recovery_count': 0}
                cg.read_log = lambda job_id, *_: reads.append(job_id) or {'committed': True, 'evals': []}
                import io, contextlib
                with contextlib.redirect_stdout(io.StringIO()):
                    cg.tick(c)
                    cg.tick(c)                                      # within 120 s: skipped
                    state = json.loads(Path(c['state_path']).read_text())
                    state['log_read_at'] -= 121
                    Path(c['state_path']).write_text(json.dumps(state))
                    cg.tick(c)
                self.assertEqual(len(reads), 2)
                c.pop('log_read_seconds')
                with contextlib.redirect_stdout(io.StringIO()):
                    cg.tick(c)                                      # default: every RUNNING tick
                self.assertEqual(len(reads), 3)
            finally:
                cg.observe_skypilot, cg.read_log = original

    def test_no_progress_line_or_a_fresh_one_is_never_wedged(self):
        utc = lambda t: __import__('time').strftime('%Y-%m-%dT%H:%M:%SZ', __import__('time').gmtime(t))
        no_line = {'status': 'RUNNING', 'recovery_count': 0, 'committed': True, 'evals': []}   # no watchdog, or pushed out
        fresh = lambda t: {**no_line, 'progress': f'PROGRESS {utc(t)} step=1 phase=train age=0'}
        history, _ = run(cfg(budget_usd=1000.0), [no_line] * 30, start=1000.0, step=60)
        self.assertEqual(sum(history, []), [])
        history, _ = run(cfg(budget_usd=1000.0), [fresh(1000.0 + 60 * i) for i in range(30)], start=1000.0, step=60)
        self.assertEqual(sum(history, []), [])
        with self.assertRaises(ValueError):
            cfg(progress_period_seconds=0)

    def test_new_evaluations_are_forwarded_once(self):
        c = cfg(budget_usd=100.0)
        ev = lambda *steps: {'status': 'RUNNING', 'recovery_count': 0, 'committed': True,
                             'evals': [f'EVAL step={n} {{"eval_loss": {n}}}' for n in steps]}
        history, st = run(c, [ev(), ev(6), ev(6), ev(6, 12, 18), {'status': 'RECOVERING', 'recovery_count': 1},
                              {**ev(12), 'recovery_count': 1}])
        notes = [a[1] for acts in history for a in acts if a[0] == 'notify']
        self.assertEqual(notes, ['evaluation: EVAL step=6 {"eval_loss": 6}', 'evaluation: EVAL step=18 {"eval_loss": 18}'])
        history, _ = run(cfg(budget_usd=100.0, notify_evals=False), [ev(6)])
        self.assertEqual(sum(history, []), [])
        with self.assertRaises(ValueError):
            cfg(notify_evals='yes')

    def test_unreadable_log_is_notified_once(self):
        c = cfg(budget_usd=100.0, max_capacity_wait_minutes=60)
        history, st = run(c, [{'status': 'RUNNING', 'recovery_count': 0, 'committed': None}] * 7)
        notes = [a[1] for acts in history for a in acts if a[0] == 'notify']
        self.assertEqual(sum('cannot be read' in n for n in notes), 1)

    def test_fallback_reachability_with_free_waiting(self):
        fb = {'approved': True, 'after_capacity_wait_minutes': 100, 'task_yaml': OD_YAML}
        with self.assertRaisesRegex(ValueError, 'could never fire'):
            cfg(budget_usd=8.0, ceiling_hourly_usd=4.57, max_capacity_wait_minutes=130, fallback=fb)
        cfg(budget_usd=8.0, ceiling_hourly_usd=4.57, max_capacity_wait_minutes=130, fallback=fb, free_without_vm=True)

    def test_tick_reads_progress_and_counts_vms(self):
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            c = cfg(state_path=str(Path(tmp) / 'state.json'), free_without_vm=True, vm_count_cmd=['true'])
            original = (cg.observe_skypilot, cg.read_log, cg.count_vms)
            seq = iter([{'status': 'RUNNING', 'recovery_count': 0, 'cluster': 'r1-7'},
                        {'status': 'RUNNING', 'recovery_count': 0, 'cluster': 'r1-7'},
                        {'status': 'RECOVERING', 'recovery_count': 1, 'cluster': 'r1-7'}])
            cg.observe_skypilot = lambda job_id, *_: dict(next(seq))
            cg.read_log = lambda job_id, *_: calls.append('log') or {'committed': True, 'evals': []}
            cg.count_vms = lambda cmd, cluster: calls.append(('vms', cluster)) or 0
            try:
                for _ in range(3):
                    cg.tick(c)
            finally:
                cg.observe_skypilot, cg.read_log, cg.count_vms = original
            st = json.loads(Path(c['state_path']).read_text())
        self.assertEqual(calls, ['log', 'log', ('vms', 'r1-7')])   # every RUNNING tick: evaluations are forwarded
        self.assertEqual(st['stalled'], 0)

    def test_cluster_name_falls_back_to_job_name_and_id(self):
        self.assertEqual(cg.cluster_name({'cluster': 'r1-7-abc', 'name': 'r1'}, 7), 'r1-7-abc')
        self.assertEqual(cg.cluster_name({'cluster': None, 'name': 'r1'}, 7), 'r1-7')   # consolidated control
        self.assertIsNone(cg.cluster_name({'cluster': None, 'name': None}, 7))
        self.assertIsNone(cg.count_vms(['true'], None))                                   # unknown stays charged

    def test_long_job_names_follow_skypilots_cluster_naming(self):
        import sys
        import unittest.mock
        name = 'experiment-ablation-run-000002'                                            # 30 chars
        with unittest.mock.patch.dict(sys.modules, {'sky.jobs.utils': None}):             # no SkyPilot here
            self.assertEqual(cg.cluster_name({'cluster': None, 'name': name}, 7), f'{name}-7')
        fake = unittest.mock.MagicMock()
        fake.generate_managed_job_cluster_name = lambda n, j: f'{n[:22]}-ab-{j}'
        with unittest.mock.patch.dict(sys.modules, {'sky.jobs.utils': fake}):
            self.assertEqual(cg.cluster_name({'cluster': None, 'name': name}, 7), 'experiment-ablation-ru-ab-7')

    def test_init_config_progress_and_billing(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = {'run_id': 'r1', 'purchase_model': 'spot',
                 'budget': {'deadline_utc': '2100-01-01T00:00:00Z', 'total_limit': 15.0, 'ceiling_hourly_usd': 4.0,
                            'free_without_vm': True},
                 'recovery': {'max_attempts': None, 'max_capacity_wait_minutes': 120, 'planned_drills': 1}}
            out = cg.init_config(m, 1, Path(tmp), attended=True, dry_run=True, vm_count_cmd=['bash', 'ids.sh'])
            c = out['config']
            self.assertNotIn('max_attempts', c)
            self.assertEqual((c['max_attempts_without_progress'], c['free_without_vm'], c['vm_count_cmd']),
                             (10, True, ['bash', 'ids.sh']))


if __name__ == '__main__':
    unittest.main()
