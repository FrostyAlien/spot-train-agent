import sys as _sys
from pathlib import Path as _Path
SKILL = _Path(__file__).resolve().parents[2] / 'skills' / 'gpu-training-recover'   # the skill under test
_sys.path.insert(0, str(SKILL / 'scripts'))
import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = SKILL / 'scripts'
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location('checkpoint_marker', SCRIPTS / 'checkpoint_marker.py')
cm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cm)

IDENT = {'image_digest': 'sha256:img', 'code_sha256': 'c0de', 'config_sha256': 'cf9'}
FIELDS = {'run_id': 'run-a', 'data_version': 'data-v1', 'model_version': 'model-v1', 'world_size': 2,
          'optimizer_boundary': True, 'scaler_used': False, **IDENT}
FILES = [{'path': 'payload/model.pt', 'roles': ['model', 'optimizer', 'scheduler', 'training_state']},
         {'path': 'payload/rank0.pt', 'roles': ['rng', 'data'], 'rank': 0},
         {'path': 'payload/rank1.pt', 'roles': ['rng', 'data'], 'rank': 1}]


class MarkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.mount = Path(self.tmp.name) / 'mnt'
        self.run = self.mount / 'run-a'
        self.run.mkdir(parents=True)

    def save(self, step, commit=True, files=FILES, attempt='20261002T120000Z'):
        d = cm.step_dir(self.run, step, attempt)
        (d / 'payload').mkdir(parents=True)
        for f in FILES:
            (d / f['path']).write_bytes(f'{step}:{f["path"]}'.encode())
        if commit:
            cm.commit(d, {**FIELDS, 'global_step': step}, files)
        return d

    def select(self, **ident):
        return cm.select(self.run, 'run-a', 'data-v1', 'model-v1', 2, {**IDENT, **ident})

    def test_commit_then_resume_newest(self):
        self.save(100)
        d = self.save(200)
        result = self.select()
        self.assertEqual((result['action'], result['global_step'], result['path']), ('resume', 200, str(d)))
        self.assertEqual(result['skipped'], [])

    def test_uncommitted_newer_save_is_ignored(self):
        self.save(100)
        self.save(200, commit=False)          # preempted before its marker reached the bucket
        self.assertEqual(self.select()['global_step'], 100)

    def test_damaged_newest_falls_back_with_reason(self):
        self.save(100)
        d = self.save(200)
        (d / 'payload/model.pt').write_bytes(b'truncated')
        result = self.select()
        self.assertEqual(result['global_step'], 100)
        self.assertEqual(result['skipped'][0]['checkpoint'], d.name)
        self.assertIn('mismatch', result['skipped'][0]['reason'])

    def test_identity_mismatch_blocks(self):
        self.save(100)
        with self.assertRaisesRegex(cm.Blocked, 'identity mismatch: image_digest'):
            self.select(image_digest='sha256:other')

    def test_identity_is_required(self):
        self.save(100)
        with self.assertRaisesRegex(cm.Blocked, 'not supplied'):
            cm.select(self.run, 'run-a', 'data-v1', 'model-v1', 2, {'image_digest': 'sha256:img'})

    def test_fresh_only_without_any_marker(self):
        self.assertEqual(cm.select(self.mount / 'new-run', 'new-run', 'd', 'm', 2, IDENT)['action'], 'fresh')
        self.save(100, commit=False)
        result = self.select()
        self.assertEqual(result['action'], 'fresh')
        self.assertEqual(result['uncommitted'], [cm.step_dir(self.run, 100, '20261002T120000Z').name])

    def test_committed_but_none_valid_blocks(self):
        d = self.save(100)
        (d / 'payload/rank1.pt').unlink()
        with self.assertRaisesRegex(cm.Blocked, 'none verifies'):
            self.select()

    def test_missing_mount_blocks(self):
        with self.assertRaisesRegex(cm.Blocked, 'not available'):
            cm.select(Path(self.tmp.name) / 'no-mount' / 'run-a', 'run-a', 'd', 'm', 2, IDENT)

    def test_unmounted_mount_root_blocks(self):
        self.save(100)
        with self.assertRaisesRegex(cm.Blocked, 'not a mounted filesystem'):
            cm.select(self.run, 'run-a', 'data-v1', 'model-v1', 2, IDENT, mount_root=self.mount)
        root = '/' if os.path.ismount('/') else None
        if root:                                    # a real mount point passes the check
            self.assertEqual(cm.select(self.run, 'run-a', 'data-v1', 'model-v1', 2, IDENT,
                                       mount_root=root)['action'], 'resume')

    def test_mounted_bucket_without_run_prefix_is_fresh(self):
        root = '/' if os.path.ismount('/') else None
        if root is None:
            self.skipTest('no mount point to test with')
        missing = Path(self.tmp.name) / 'runs' / 'run-b'     # neither the run nor its parent exist yet
        sel = cm.select(missing, 'run-b', 'd', 'm', 2, IDENT, mount_root=root)
        self.assertEqual(sel['action'], 'fresh')

    @unittest.skipIf(os.geteuid() == 0, 'root ignores permissions')
    def test_unreadable_storage_blocks(self):
        self.save(100)
        os.chmod(self.run, 0)
        self.addCleanup(os.chmod, self.run, 0o755)
        with self.assertRaises(cm.Blocked):
            self.select()

    def test_no_marker_when_state_is_missing(self):
        files = [{'path': 'payload/model.pt', 'roles': ['model', 'scheduler', 'training_state']}] + FILES[1:]
        d = cm.step_dir(self.run, 100, '20261002T120000Z')
        with self.assertRaisesRegex(cm.vc.Invalid, 'required state role missing'):
            self.save(100, files=files)
        self.assertFalse((d / 'COMMITTED.json').exists())
        self.assertEqual(self.select()['action'], 'fresh')

    def test_marker_is_never_overwritten(self):
        d = self.save(100)
        with self.assertRaises(FileExistsError):
            cm.commit(d, {**FIELDS, 'global_step': 100}, FILES)

    def test_step_must_match_directory(self):
        d = cm.step_dir(self.run, 300, 'x1')
        (d / 'payload').mkdir(parents=True)
        for f in FILES:
            (d / f['path']).write_bytes(b'x')
        cm.commit(d, {**FIELDS, 'global_step': 299}, FILES)
        self.save(100)
        result = self.select()
        self.assertEqual(result['global_step'], 100)
        self.assertIn('does not match', result['skipped'][0]['reason'])

    def test_replacement_resaves_a_step_in_a_new_directory(self):
        self.save(100)
        stale = self.save(200, commit=False, attempt='20261002T120000Z')   # preempted before the marker
        (stale / 'manifest.json').write_text('{}')                        # even a half-written manifest
        fresh = self.save(200, attempt='20261002T130000Z')                # the replacement's own directory
        self.assertNotEqual(stale, fresh)
        self.assertEqual(self.select()['path'], str(fresh))

    def test_same_step_twice_prefers_the_newest_attempt(self):
        self.save(200, attempt='20261002T120000Z')
        newer = self.save(200, attempt='20261002T130000Z')
        self.assertEqual(self.select()['path'], str(newer))

    def test_attempt_tag_is_checked(self):
        for bad in ('', 'a-b', 'a/b', None):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                cm.step_dir(self.run, 1, bad)

    def test_cli_exit_codes(self):
        self.save(100)
        args = ['select', str(self.run), '--run-id', 'run-a', '--data-version', 'data-v1', '--model-version',
                'model-v1', '--world-size', '2', '--image-digest', 'sha256:img', '--code-sha256', 'c0de',
                '--config-sha256', 'cf9']
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cm.main(args), 0)
        self.assertEqual(json.loads(out.getvalue())['action'], 'resume')
        args[args.index('sha256:img')] = 'sha256:other'
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cm.main(args), 3)
        self.assertEqual(json.loads(out.getvalue())['action'], 'blocked')


    ID_ARGS = ['--run-id', 'run-a', '--data-version', 'data-v1', '--model-version', 'model-v1', '--world-size', '2',
               '--image-digest', 'sha256:img', '--code-sha256', 'c0de', '--config-sha256', 'cf9']

    def finalize(self, **kw):
        return cm.finalize(self.run, 'run-a', 'data-v1', 'model-v1', 2, IDENT, **kw)

    def test_journal_is_write_once_per_event_and_ordered(self):
        fresh = Path(self.tmp.name) / 'mnt' / 'runs' / 'new-run'          # prefix does not exist yet
        cm.journal(fresh, '20261002T120000Z', 'preflight', {'host_gpus': '2'})
        cm.journal(fresh, '20261002T120000Z', 'commit', {'global_step': '1'})
        cm.journal(fresh, '20261002T130000Z', 'selection', {'action': 'resume'})
        events = cm.read_journal(fresh)
        self.assertEqual([(e['attempt'][-6:], e['seq'], e['event']) for e in events],
                         [('20000Z', 1, 'preflight'), ('20000Z', 2, 'commit'), ('30000Z', 1, 'selection')])
        self.assertEqual(events[0]['host_gpus'], '2')
        for bad in (('bad attempt!', 'x'), ('20261002T120000Z', 'Bad Event')):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                cm.journal(fresh, *bad)

    def test_unreadable_journal_entry_is_reported(self):
        cm.journal(self.run, 'a1', 'preflight')
        (self.run / 'journal' / 'a1' / '0002-commit.json').write_text('{cut')
        events = cm.read_journal(self.run)
        self.assertIn('error', events[1])

    def test_journal_does_not_disturb_select(self):
        self.save(100)
        cm.journal(self.run, '20261002T120000Z', 'commit')
        self.assertEqual(self.select()['global_step'], 100)

    def test_finalize_completed_needs_the_target_step(self):
        self.save(100)
        self.save(200)
        with self.assertRaisesRegex(cm.Blocked, 'not the target 300'):
            self.finalize(outcome='completed', target_step=300)
        self.assertFalse((self.run / 'FINAL.json').exists())
        final = self.finalize(outcome='completed', target_step=200)
        self.assertEqual((final['final_step'], final['outcome']), (200, 'completed'))
        with self.assertRaises(FileExistsError):                        # written once
            self.finalize(outcome='completed', target_step=200)

    def test_finalize_records_artifacts_and_deadline_outcome(self):
        self.save(100)
        (self.run / 'final').mkdir()
        (self.run / 'final' / 'best.pt').write_bytes(b'best')
        final = self.finalize(outcome='deadline_stopped', target_step=300, artifacts=['final/best.pt'])
        self.assertEqual(final['artifacts'][0]['bytes'], 4)
        self.assertEqual(final['outcome'], 'deadline_stopped')
        with self.assertRaisesRegex(ValueError, 'inside the run directory'):
            cm._artifact(self.run, '../outside')

    def test_finalize_needs_a_committed_checkpoint(self):
        with self.assertRaisesRegex(cm.Blocked, 'nothing was committed'):
            self.finalize(outcome='stopped')

    def test_verify_final_detects_changes(self):
        self.save(100)
        (self.run / 'final').mkdir()
        (self.run / 'final' / 'best.pt').write_bytes(b'best')
        self.finalize(outcome='completed', target_step=100, artifacts=['final/best.pt'])
        ok = cm.verify_final(self.run, 'run-a', 'data-v1', 'model-v1', 2, IDENT)
        self.assertEqual((ok['final_step'], ok['artifacts'], ok['lineage_warnings']), (100, 1, []))
        with contextlib.redirect_stdout(io.StringIO()):
            cm.journal(self.run, 'a1', 'selection', {'action': 'fresh'})
            cm.journal(self.run, 'a1', 'commit')
            cm.journal(self.run, 'a2', 'selection', {'action': 'fresh'})
        warn = cm.verify_final(self.run, 'run-a', 'data-v1', 'model-v1', 2, IDENT)['lineage_warnings']
        self.assertEqual(warn, ['start 2: fresh after an earlier start committed (progress lost)'])
        (self.run / 'final' / 'best.pt').write_bytes(b'other')
        with self.assertRaisesRegex(cm.Blocked, 'changed since FINAL'):
            cm.verify_final(self.run, 'run-a', 'data-v1', 'model-v1', 2, IDENT)
        (self.run / 'final' / 'best.pt').write_bytes(b'best')
        self.save(200)                                                    # a newer checkpoint than FINAL names
        with self.assertRaisesRegex(cm.Blocked, 'newest verified checkpoint'):
            cm.verify_final(self.run, 'run-a', 'data-v1', 'model-v1', 2, IDENT)

    def test_finalize_and_journal_cli(self):
        self.save(100)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cm.main(['journal', str(self.run), '--attempt', 'a1', '--event', 'finish',
                                      '--field', 'outcome=completed']), 0)
            self.assertEqual(cm.main(['finalize', str(self.run), *self.ID_ARGS, '--outcome', 'completed',
                                      '--target-step', '100']), 0)
            self.assertEqual(cm.main(['verify-final', str(self.run), *self.ID_ARGS]), 0)
            self.assertEqual(cm.main(['journal-show', str(self.run)]), 0)
        lines = out.getvalue().strip().splitlines()
        self.assertEqual(json.loads(lines[-1])[0]['outcome'], 'completed')
        (self.run / 'FINAL.json').unlink()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cm.main(['verify-final', str(self.run), *self.ID_ARGS]), 3)

class CommitLineAndJournalCheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.run = Path(self.tmp.name) / 'run-a'

    def commit(self, step, attempt):
        d = cm.step_dir(self.run, step, attempt)
        (d / 'payload').mkdir(parents=True)
        for f in FILES:
            (d / f['path']).write_bytes(f'{step}:{f["path"]}'.encode())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cm.commit(d, {**FIELDS, 'global_step': step}, FILES)
        return out.getvalue()

    def start(self, attempt, action, step, preflight=True, **fields):
        if preflight:
            cm.journal(self.run, attempt, 'preflight', fields)
        cm.journal(self.run, attempt, 'selection', {'action': action})
        self.commit(step, attempt)
        cm.journal(self.run, attempt, 'commit', {'global_step': str(step)})

    def test_commit_prints_one_standard_line(self):
        line = self.commit(5, 'a1').strip()
        self.assertTrue(line.startswith('CHECKPOINT_COMMITTED {'))
        rec = json.loads(line.split(' ', 1)[1])
        self.assertEqual((rec['global_step'], rec['dir'], rec['run_id']), (5, '000000000005-a1', 'run-a'))

    def test_rehearsal_passes_with_fresh_then_resume(self):
        self.start('20261003T100000Z', 'fresh', 1)
        self.start('20261003T100100Z', 'resume', 2)
        r = cm.check_journal(self.run, ['fresh', 'resume'])
        self.assertTrue(r['ok'], r)
        self.assertEqual(r['starts'], 2)

    def test_journal_check_failures(self):
        self.start('20261003T100000Z', 'fresh', 1, preflight=False)
        r = cm.check_journal(self.run, ['fresh', 'resume'])
        self.assertIn('expected 2 worker start(s), found 1', r['failures'])
        r = cm.check_journal(self.run, ['fresh'], require=['preflight'])
        self.assertIn('start 1: no preflight journaled', r['failures'])
        r = cm.check_journal(self.run, ['resume'])
        self.assertIn("start 1: expected selection resume, got ['fresh']", r['failures'])

    def test_qualification_measures_startup_and_memory_headroom(self):
        cm.journal(self.run, 'a1', 'preflight', {'peak_memory_fraction': '0.95'})
        cm.journal(self.run, 'a1', 'selection', {'action': 'fresh'})
        self.commit(1, 'a1')
        cm.journal(self.run, 'a1', 'commit')
        first = cm.read_journal(self.run)[0]
        submitted = cm._epoch(first['time']) - 240
        r = cm.check_journal(self.run, ['fresh'], ['preflight'], submitted_at=submitted, max_memory_fraction=0.9)
        self.assertEqual((r['startup_minutes'], r['recommended_startup_allowance_minutes']), (4.0, 8))
        self.assertFalse(r['ok'])
        self.assertIn('lower the per-device batch', r['failures'][0])

    def test_fresh_after_a_commit_is_progress_lost(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.start('20261003T100000Z', 'fresh', 1)
            self.start('20261003T100100Z', 'fresh', 1)
        r = cm.check_journal(self.run, ['fresh', 'fresh'])
        self.assertIn('start 2: fresh after an earlier start committed (progress lost)', r['failures'])

    def test_worker_starts_are_ordered_by_time_not_tag(self):
        import unittest.mock
        times = iter(['2026-10-03T10:00:00Z'] * 2 + ['2026-10-03T10:05:00Z'] * 2)
        with unittest.mock.patch.object(cm, '_utc', lambda: next(times)), \
                contextlib.redirect_stdout(io.StringIO()):
            self.start('zhost1', 'fresh', 1, preflight=False)       # tags sort the other way round
            self.start('ahost2', 'resume', 2, preflight=False)
        self.assertTrue(cm.check_journal(self.run, ['fresh', 'resume'])['ok'])

    def test_journal_echoes_to_the_job_log(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cm.journal(self.run, 'a1', 'selection', {'action': 'fresh'})
        line = out.getvalue().strip()
        self.assertTrue(line.startswith('CHECKPOINT_JOURNAL {'))
        self.assertEqual(json.loads(line.split(' ', 1)[1])['action'], 'fresh')

    def test_qualification_io_measurements_are_reported(self):
        with contextlib.redirect_stdout(io.StringIO()):
            cm.journal(self.run, 'a1', 'preflight')
            cm.journal(self.run, 'a1', 'selection', {'action': 'fresh'})
            self.commit(1, 'a1')
            cm.journal(self.run, 'a1', 'commit', {'save_seconds': '3.5', 'checkpoint_gb': '1.2'})
            cm.journal(self.run, 'a1', 'finish', {'read_mb_s': '640'})
        r = cm.check_journal(self.run, ['fresh'], ['preflight'])
        self.assertTrue(r['ok'], r)
        self.assertEqual((r['read_mb_s'], r['save_seconds'], r['checkpoint_gb']), (640.0, 3.5, 1.2))

    def test_steady_state_save_and_conservative_measurements(self):
        with contextlib.redirect_stdout(io.StringIO()):
            cm.journal(self.run, 'a1', 'selection', {'action': 'fresh'})
            for step, save in ((6, '1.0'), (12, '9.5')):          # the first save only writes locally
                self.commit(step, 'a1')
                cm.journal(self.run, 'a1', 'commit', {'save_seconds': save})
            cm.journal(self.run, 'a1', 'finish', {'startup_seconds': '80', 'step_seconds': '0.4', 'eval_seconds': '12', 'read_mb_s': '900',
                                                  'loader_wait_fraction': '0.02', 'peak_memory_fraction': '0.5'})
            cm.journal(self.run, 'a2', 'selection', {'action': 'resume'})
            self.commit(18, 'a2')
            cm.journal(self.run, 'a2', 'commit', {'save_seconds': '4.0'})
            cm.journal(self.run, 'a2', 'finish', {'startup_seconds': '60', 'eval_seconds': '15', 'read_mb_s': '700'})
        r = cm.check_journal(self.run, ['fresh', 'resume'])
        self.assertTrue(r['ok'], r)
        self.assertEqual((r['save_seconds'], r['startup_seconds'], r['eval_seconds'], r['read_mb_s'], r['loader_wait_fraction']),
                         (9.5, 80.0, 15.0, 700.0, 0.02))
        self.assertEqual(r['step_seconds'], 0.4)
        self.assertNotIn('notes', r)

    def test_low_gpu_memory_is_noted(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.start('a1', 'fresh', 1, peak_memory_fraction='0.07')
        r = cm.check_journal(self.run, ['fresh'], ['preflight'])
        self.assertTrue(r['ok'], r)
        self.assertEqual(r['notes'], ['low_utilisation'])

    def test_qualification_requires_a_completed_final_record(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.start('a1', 'fresh', 1)
            self.start('a2', 'resume', 2)
        r = cm.check_journal(self.run, ['fresh', 'resume'], require_final=True)
        self.assertIn('FINAL.json outcome unreadable (FileNotFoundError)', r['failures'][0])
        with contextlib.redirect_stdout(io.StringIO()):
            cm.finalize(self.run, 'run-a', FIELDS['data_version'], FIELDS['model_version'], FIELDS['world_size'],
                        {k: FIELDS[k] for k in ('image_digest', 'code_sha256', 'config_sha256')}, 'completed', 2)
        self.assertTrue(cm.check_journal(self.run, ['fresh', 'resume'], require_final=True)['ok'])

    def test_watchdog_summaries_give_the_stall_seed_and_phase_durations(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.start('a1', 'fresh', 1)
        f = self.run / 'summaries.jsonl'
        f.write_text(json.dumps({'largest_train_gap_seconds': 2.5, 'phase_seconds': {'eval': 10.0}, 'train_ticks': 12}) + '\n'
                     + json.dumps({'largest_train_gap_seconds': 4.0, 'phase_seconds': {'eval': 8.0, 'save': 3.0}, 'train_ticks': 8}) + '\n')
        r = cm.check_journal(self.run, ['fresh'], watch_summaries=f)
        self.assertEqual((r['largest_train_gap_seconds'], r['phase_seconds']), (4.0, {'eval': 10.0, 'save': 3.0}))
        r = cm.check_journal(self.run, ['fresh'], watch_summaries=self.run / 'missing.jsonl')
        self.assertIn('the stall watchdog did not run', r['failures'][0])
        f.write_text(json.dumps({'largest_train_gap_seconds': 0.0, 'phase_seconds': {}, 'train_ticks': 0}) + '\n')   # never ticked
        r = cm.check_journal(self.run, ['fresh'], watch_summaries=f)
        self.assertIn("the entrypoint's hooks did not tick", r['failures'][0])
        f.write_text(json.dumps({'largest_train_gap_seconds': 0.0, 'phase_seconds': {}, 'train_ticks': 20}) + '\n')   # fast steps
        self.assertTrue(cm.check_journal(self.run, ['fresh'], watch_summaries=f)['ok'])

    def summary(self, **over):
        s = {'largest_train_gap_seconds': 2.0, 'largest_eval_gap_seconds': 3.0, 'eval_tail_seconds': 20.0,
             'phase_seconds': {'eval': 40.0, 'save': 300.0, 'final': 30.0}, 'train_ticks': 20,
             'quiet_seconds': {'train': 2.0, 'eval': 20.0, 'save': 250.0, 'final': 5.0, 'startup': 700.0},
             'io_mb': {'save': 9000.0, 'startup': 50.0}, 'io_signal': True}
        s.update(over)
        return json.dumps(s) + '\n'

    def test_recommended_watchdog_limits(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.start('a1', 'fresh', 1)
        f = self.run / 'summaries.jsonl'
        f.write_text(self.summary() + self.summary(largest_train_gap_seconds=50.0, quiet_seconds={'startup': 100.0}))
        r = cm.check_journal(self.run, ['fresh'], watch_summaries=f, eval_scale=20)
        self.assertEqual(r['recommended'], {'watchdog': {'limits': {'train': 250, 'eval': 1200, 'save': 750,
                                                                     'final': 600, 'startup': 2100}}})
        self.assertEqual((r['quiet_seconds']['startup'], r['io_mb']['save'], r['io_signal'], r['eval_tail_seconds']),
                         (700.0, 9000.0, True, 20.0))
        self.assertNotIn('notes', r)
        r = cm.check_journal(self.run, ['fresh'], watch_summaries=f)          # no eval scale: the tail as measured
        self.assertEqual(r['recommended']['watchdog']['limits']['eval'], 600)

    def test_saves_the_byte_counters_cannot_see_fall_back_to_their_duration(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.start('a1', 'fresh', 1)
        cm.journal(self.run, 'a1', 'commit', {'step': '1', 'checkpoint_gb': '20'})
        f = self.run / 'summaries.jsonl'
        f.write_text(self.summary())                                    # 9 GB counted of a 20 GB checkpoint
        r = cm.check_journal(self.run, ['fresh'], watch_summaries=f)
        self.assertEqual(r['recommended']['watchdog']['limits']['save'], 1500)   # 5 x 300 s
        self.assertIn('byte counters did not see the saves', r['notes'][0])
        f.write_text(self.summary(io_signal=False, io_mb={}))          # no /proc io at all
        self.assertIn('byte counters', cm.check_journal(self.run, ['fresh'], watch_summaries=f)['notes'][0])
        f.write_text(self.summary(phase_seconds={'save': 0.2}, io_mb={}))   # saves shorter than a poll: no verdict
        r = cm.check_journal(self.run, ['fresh'], watch_summaries=f)
        self.assertEqual((r['recommended']['watchdog']['limits']['save'], 'notes' in r), (750, False))

    def test_an_evaluation_hidden_in_train_gaps_widens_the_train_limit(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.start('a1', 'fresh', 1)
        f = self.run / 'summaries.jsonl'
        hidden = self.summary(largest_eval_gap_seconds=0.0, eval_tail_seconds=0.0, phase_seconds={'save': 3.0},
                              quiet_seconds={'train': 30.0})
        f.write_text(hidden)
        r = cm.check_journal(self.run, ['fresh'], watch_summaries=f, eval_scale=10)
        self.assertEqual(r['recommended']['watchdog']['limits']['train'], 900)  # 3 x 30 s x 10
        self.assertIn('evaluation ran inside train gaps', r['notes'][0])
        r = cm.check_journal(self.run, ['fresh'], watch_summaries=f)            # no evaluation at all
        self.assertEqual((r['recommended']['watchdog']['limits']['train'], 'notes' in r), (180, False))

    def test_prune_list_keeps_the_newest_complete_and_the_final(self):
        with contextlib.redirect_stdout(io.StringIO()):
            for step in (1, 2, 3, 4, 5):
                self.commit(step, 'a1')
        (self.run / '000000000006-a1').mkdir()                         # a save in progress: never touched
        (self.run / '000000000000-a0').mkdir()                         # an old partial save
        self.assertEqual(cm.prune_list(self.run)['prunable'], ['000000000002-a1', '000000000001-a1', '000000000000-a0'])
        (self.run / 'FINAL.json').write_text(json.dumps({'final_dir': '000000000001-a1'}))
        r = cm.prune_list(self.run)
        self.assertEqual((r['keep'], r['prunable']), (['000000000005-a1', '000000000004-a1', '000000000003-a1'],
                                                      ['000000000002-a1', '000000000000-a0']))
        payload = self.run / '000000000004-a1' / FILES[0]['path']
        payload.write_bytes(payload.read_bytes()[:-1])                 # an upload cut short: not counted as kept
        self.assertEqual(cm.prune_list(self.run)['keep'], ['000000000005-a1', '000000000003-a1', '000000000002-a1'])
        self.assertEqual(cm.prune_list(self.run, keep=9)['prunable'], [])
        (self.run / 'FINAL.json').write_text(json.dumps({'final_dir': '000000000001-a1',       # an artifact kept in
                                                         'artifacts': [{'path': '000000000000-a0/best.pt'}]}))  # an old dir
        self.assertNotIn('000000000000-a0', cm.prune_list(self.run)['prunable'])

    def test_journal_check_cli_exit_code(self):
        self.start('a1', 'fresh', 1)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cm.main(['journal-check', str(self.run), '--expect', 'fresh']), 0)
            self.assertEqual(cm.main(['journal-check', str(self.run), '--expect', 'fresh,resume']), 1)


if __name__ == '__main__':
    unittest.main()
