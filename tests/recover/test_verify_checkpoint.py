import sys as _sys
from pathlib import Path as _Path
SKILL = _Path(__file__).resolve().parents[2] / 'skills' / 'gpu-training-recover'   # the skill under test
_sys.path.insert(0, str(SKILL / 'scripts'))
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('verify_checkpoint', SKILL / 'scripts/verify_checkpoint.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class VerificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'payload').mkdir()
        self.manifest = {'schema_version': 1, 'run_id': 'run-a', 'data_version': 'data-v1',
                         'model_version': 'model-v1', 'world_size': 2, 'global_step': 9,
                         'optimizer_boundary': True, 'scaler_used': False, 'files': []}
        for role in ('model', 'optimizer', 'scheduler', 'training_state', 'rng', 'data'):
            for rank in (range(2) if role in ('rng', 'data') else [None]):
                path = 'payload/' + role + str(rank) + '.bin'
                content = (role + str(rank)).encode()
                (self.root / path).write_bytes(content)
                self.manifest['files'].append({'path': path, 'bytes': len(content),
                    'sha256': hashlib.sha256(content).hexdigest(), 'role': role, 'rank': rank})
        self.publish()

    def publish(self):
        raw = json.dumps(self.manifest).encode()
        (self.root / 'manifest.json').write_bytes(raw)
        (self.root / 'COMMITTED.json').write_text(json.dumps({'schema_version': 1,
            'run_id': 'run-a', 'global_step': 9, 'manifest': 'manifest.json',
            'manifest_sha256': hashlib.sha256(raw).hexdigest()}))

    def verify(self, **kwargs):
        return m.verify(self.root, kwargs.get('run_id', 'run-a'), kwargs.get('data_version', 'data-v1'),
                        kwargs.get('model_version', 'model-v1'), kwargs.get('world_size', 2), kwargs.get('accepted_record'))

    def test_valid(self):
        self.assertEqual(self.verify()['files_verified'], 8)

    def test_pending_commit_checks_before_the_marker_exists(self):
        commit = json.loads((self.root / 'COMMITTED.json').read_text())
        with self.assertRaisesRegex(ValueError, 'already committed'):
            m.verify(self.root, 'run-a', 'data-v1', 'model-v1', 2, pending_commit=commit)
        (self.root / 'COMMITTED.json').unlink()
        self.assertEqual(m.verify(self.root, 'run-a', 'data-v1', 'model-v1', 2, pending_commit=commit)['files_verified'], 8)
        with self.assertRaisesRegex(ValueError, 'manifest hash mismatch'):
            m.verify(self.root, 'run-a', 'data-v1', 'model-v1', 2, pending_commit={**commit, 'manifest_sha256': '0' * 64})

    def test_partial(self):
        (self.root / 'COMMITTED.json').unlink()
        with self.assertRaises((ValueError, OSError)): self.verify()

    def test_corrupt(self):
        path = self.root / self.manifest['files'][0]['path']
        content = path.read_bytes()
        path.write_bytes(b'x' * len(content))
        with self.assertRaisesRegex(ValueError, 'hash mismatch'): self.verify()

    def test_manifest_hash(self):
        (self.root / 'manifest.json').write_text('{}')
        with self.assertRaisesRegex(ValueError, 'manifest hash'): self.verify()

    def test_expected_identities(self):
        for field, value in [('run_id', 'wrong'), ('data_version', 'wrong'), ('model_version', 'wrong'), ('world_size', 1)]:
            with self.subTest(field=field), self.assertRaises(ValueError): self.verify(**{field: value})

    def test_bad_paths(self):
        for path in ['payload/../../escape', '/etc/passwd', 'payload/./file', 'payload//file', 'payload/..\\escape']:
            with self.subTest(path=path):
                self.manifest['files'][0]['path'] = path
                self.publish()
                with self.assertRaises(ValueError): self.verify()

    def test_symlink(self):
        path = self.root / self.manifest['files'][0]['path']
        path.unlink()
        path.symlink_to('/etc/hostname')
        with self.assertRaisesRegex(ValueError, 'symlink'): self.verify()

    def single_file_epoch_manifest(self, derived=None):
        for entry in self.manifest['files']:
            (self.root / entry['path']).unlink()
        content = b'last.pt'
        (self.root / 'payload/last.pt').write_bytes(content)
        self.manifest['files'] = [{'path': 'payload/last.pt', 'bytes': len(content),
            'sha256': hashlib.sha256(content).hexdigest(),
            'roles': ['model', 'optimizer', 'training_state', 'scaler']}]
        self.manifest['scaler_used'] = True
        self.manifest['derived_roles'] = derived if derived is not None else {
            role: {'from': ['training_state.epoch', 'train_args.seed'], 'semantics': 'epoch-boundary rebuild'}
            for role in ('scheduler', 'rng', 'data')}
        self.publish()

    def test_multi_role_file_with_declared_derivations(self):
        self.single_file_epoch_manifest()
        result = self.verify()
        self.assertEqual(result['derived_roles'], ['data', 'rng', 'scheduler'])

    def test_undeclared_derivation_rejected(self):
        self.single_file_epoch_manifest(derived={'scheduler': {'from': ['epoch'], 'semantics': 'x'}})
        with self.assertRaisesRegex(ValueError, 'required state'): self.verify()

    def test_optimizer_cannot_be_derived(self):
        self.single_file_epoch_manifest()
        self.manifest['derived_roles']['optimizer'] = {'from': ['epoch'], 'semantics': 'x'}
        self.publish()
        with self.assertRaisesRegex(ValueError, 'cannot be derived'): self.verify()

    def test_derivation_needs_semantics(self):
        self.single_file_epoch_manifest(derived={r: {'from': ['epoch'], 'semantics': ' '} for r in ('scheduler', 'rng', 'data')})
        with self.assertRaisesRegex(ValueError, 'semantics'): self.verify()

    def test_stored_and_derived_conflict(self):
        self.manifest['derived_roles'] = {'scheduler': {'from': ['epoch'], 'semantics': 'x'}}
        self.publish()
        with self.assertRaisesRegex(ValueError, 'both stored and derived'): self.verify()

    def test_role_and_roles_exclusive(self):
        self.manifest['files'][0]['roles'] = ['model']
        self.publish()
        with self.assertRaisesRegex(ValueError, 'exactly one'): self.verify()

    def test_stored_roles_report_no_derivations(self):
        self.assertEqual(self.verify()['derived_roles'], [])

    def test_image_code_config_identity(self):
        ident = {'image_digest': 'sha256:img', 'code_sha256': 'c0de', 'config_sha256': 'cf9'}
        self.manifest.update(ident); self.publish()
        result = m.verify(self.root, 'run-a', 'data-v1', 'model-v1', 2, identity=ident)
        self.assertEqual(result['identity_checked'], ['code_sha256', 'config_sha256', 'image_digest'])
        with self.assertRaisesRegex(ValueError, 'identity mismatch: image_digest'):
            m.verify(self.root, 'run-a', 'data-v1', 'model-v1', 2, identity={**ident, 'image_digest': 'sha256:other'})

    def test_require_identity_fails_closed_when_caller_omits_one(self):
        ident = {'image_digest': 'sha256:img', 'code_sha256': 'c0de', 'config_sha256': 'cf9'}
        self.manifest.update(ident); self.publish()
        self.assertEqual(m.verify(self.root, 'run-a', 'data-v1', 'model-v1', 2, identity=ident,
                                  require_identity=True)['identity_checked'], sorted(ident))
        partial = {k: v for k, v in ident.items() if k != 'code_sha256'}
        with self.assertRaisesRegex(ValueError, 'identity mismatch: expected code_sha256 not supplied'):
            m.verify(self.root, 'run-a', 'data-v1', 'model-v1', 2, identity=partial, require_identity=True)
        self.assertEqual(m.verify(self.root, 'run-a', 'data-v1', 'model-v1', 2, identity=partial)['identity_checked'],
                         sorted(partial))   # opt-in: older callers keep working

    def test_identity_missing_from_manifest_is_mismatch(self):
        with self.assertRaisesRegex(ValueError, 'identity mismatch: config_sha256'):
            m.verify(self.root, 'run-a', 'data-v1', 'model-v1', 2, identity={'config_sha256': 'cf9'})
        with self.assertRaisesRegex(ValueError, 'unsupported identity'):
            m.verify(self.root, 'run-a', 'data-v1', 'model-v1', 2, identity={'hostname': 'x'})

    def alias_dir(self):
        other = tempfile.TemporaryDirectory()
        self.addCleanup(other.cleanup)
        return Path(other.name)

    def test_symlinked_root_rejected(self):
        alias = self.alias_dir() / 'root-alias'
        alias.symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'symlink'):
            m.verify(alias, 'run-a', 'data-v1', 'model-v1', 2)

    def test_symlinked_ancestor_canonicalized(self):
        alias = self.alias_dir() / 'parent-alias'
        alias.symlink_to(self.root.parent, target_is_directory=True)
        self.assertTrue(m.verify(alias / self.root.name, 'run-a', 'data-v1', 'model-v1', 2)['ok'])

    def test_symlink_directory(self):
        (self.root / 'alias').symlink_to(self.root / 'payload', target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'symlink'): self.verify()

    def test_missing_rank(self):
        self.manifest['files'][-1]['rank'] = 0
        self.publish()
        with self.assertRaisesRegex(ValueError, 'rank coverage'): self.verify()

    def test_missing_optimizer(self):
        self.manifest['files'][1]['role'] = 'export'
        self.publish()
        with self.assertRaisesRegex(ValueError, 'required state'): self.verify()

    def test_scaler_required(self):
        self.manifest['scaler_used'] = True
        self.publish()
        with self.assertRaisesRegex(ValueError, 'required state'): self.verify()

    def test_unlisted_file(self):
        (self.root / 'payload' / 'partial').write_text('incomplete')
        with self.assertRaisesRegex(ValueError, 'unlisted'): self.verify()

    def test_duplicate_path(self):
        self.manifest['files'].append(self.manifest['files'][0])
        self.publish()
        with self.assertRaisesRegex(ValueError, 'duplicate file'): self.verify()

    def test_duplicate_json_key(self):
        (self.root / 'COMMITTED.json').write_text('{"run_id":"run-a","run_id":"wrong"}')
        with self.assertRaisesRegex(ValueError, 'duplicate JSON'): self.verify()

    def test_update_boundary(self):
        self.manifest['optimizer_boundary'] = False
        self.publish()
        with self.assertRaisesRegex(ValueError, 'optimizer boundary'): self.verify()


    def publish_accepted(self):
        self.manifest.update(attempt_id='attempt-2',fencing_generation=2)
        self.publish()
        commit=json.loads((self.root/'COMMITTED.json').read_text())
        commit.update(attempt_id='attempt-2',fencing_generation=2,global_step=self.manifest['global_step'])
        (self.root/'COMMITTED.json').write_text(json.dumps(commit))
        return {'schema_version':1,'run_id':'run-a','global_step':self.manifest['global_step'],
                'attempt_id':'attempt-2','fencing_generation':2,
                'manifest_sha256':hashlib.sha256((self.root/'manifest.json').read_bytes()).hexdigest(),
                'commit_sha256':hashlib.sha256((self.root/'COMMITTED.json').read_bytes()).hexdigest()}

    def test_manifest_world_size_exact_integer(self):
        for value,expected in ((2.0,2),(True,1)):
            self.manifest['world_size']=value;self.publish()
            with self.assertRaisesRegex(ValueError,'world_size'):self.verify(world_size=expected)
    def test_commit_step_exact_integer(self):
        for value in (9.0,True):
            self.manifest['global_step']=1 if value is True else 9
            self.publish();commit=json.loads((self.root/'COMMITTED.json').read_text());commit['global_step']=value
            (self.root/'COMMITTED.json').write_text(json.dumps(commit))
            with self.assertRaisesRegex(ValueError,'step mismatch'):self.verify()
    def test_integrity_is_not_restore_readiness(self):
        result=self.verify();self.assertEqual(result['controller_acceptance'],'not_checked');self.assertFalse(result['restore_ready'])
    def test_accepted_record_matches(self):
        record=self.publish_accepted();result=self.verify(accepted_record=record)
        self.assertEqual(result['controller_acceptance'],'record_matched');self.assertFalse(result['restore_ready'])
    def test_unaccepted_orphan_higher_step(self):
        accepted=self.publish_accepted()
        self.manifest['global_step']=99;self.manifest['attempt_id']='fenced-attempt';self.manifest['fencing_generation']=1
        self.publish();commit=json.loads((self.root/'COMMITTED.json').read_text())
        commit.update(global_step=99,attempt_id='fenced-attempt',fencing_generation=1)
        (self.root/'COMMITTED.json').write_text(json.dumps(commit))
        self.assertTrue(self.verify()['ok'])  # bytes alone cannot establish journal acceptance
        with self.assertRaisesRegex(ValueError,'controller record'):self.verify(accepted_record=accepted)
    def test_controller_record_binding(self):
        for key,value in [('run_id','other'),('global_step',9.0),('commit_sha256','0'*64),('manifest_sha256','0'*64),('attempt_id','old'),('fencing_generation',True),('fencing_generation',1)]:
            record=self.publish_accepted();record[key]=value
            with self.subTest(key=key),self.assertRaises(ValueError):self.verify(accepted_record=record)
    def test_marker_lineage_must_match_record(self):
        record=self.publish_accepted();commit=json.loads((self.root/'COMMITTED.json').read_text());commit['fencing_generation']=True
        (self.root/'COMMITTED.json').write_text(json.dumps(commit))
        record['commit_sha256']=hashlib.sha256((self.root/'COMMITTED.json').read_bytes()).hexdigest()
        with self.assertRaisesRegex(ValueError,'fencing'):self.verify(accepted_record=record)

if __name__ == '__main__':
    unittest.main()
