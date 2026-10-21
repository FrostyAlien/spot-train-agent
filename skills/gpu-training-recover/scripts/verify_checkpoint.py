#!/usr/bin/env python3
"""Read-only verifier for a quiescent, downloaded checkpoint; no deserialization."""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys


class Invalid(ValueError):
    pass


# State a framework may rebuild from saved counters/seeds instead of storing (e.g. an
# epoch-boundary resume). Declared explicitly and reported; never silently assumed.
DERIVABLE_ROLES = {'scheduler', 'rng', 'data'}


def require(ok, message):
    if not ok:
        raise Invalid(message)


def no_duplicates(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, 'duplicate JSON key')
        result[key] = value
    return result


def safe_path(root, relative):
    require(isinstance(relative, str) and relative, 'empty/non-string path')
    p = PurePosixPath(relative)
    require(not p.is_absolute() and '\\' not in relative and ':' not in relative,
            'absolute or platform-ambiguous path')
    require(all(x not in ('', '.', '..') for x in relative.split('/')), 'path traversal or noncanonical path')
    current = root
    for part in p.parts:
        current = current / part
        require(not current.is_symlink(), 'symlink forbidden')
    require(current.is_file(), 'missing/non-regular file')
    return current


def read_file(root, relative, collect=False):
    path = safe_path(root, relative)
    # O_NOFOLLOW also rejects a symlink substituted for the final component.
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    chunks, count, digest = [], 0, hashlib.sha256()
    with os.fdopen(fd, 'rb') as f:
        require(stat.S_ISREG(os.fstat(f.fileno()).st_mode), 'non-regular file')
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            count += len(chunk)
            if collect:
                require(count <= 16 * 1024 * 1024, 'metadata too large')
                chunks.append(chunk)
            digest.update(chunk)
    return count, digest.hexdigest(), b''.join(chunks)


def read_json(root, name):
    _, digest, raw = read_file(root, name, collect=True)
    value = json.loads(raw, object_pairs_hook=no_duplicates,
                       parse_constant=lambda value: (_ for _ in ()).throw(Invalid('nonfinite JSON')))
    require(isinstance(value, dict), 'metadata must be an object')
    return value, digest


IDENTITY_FIELDS = ('image_digest', 'code_sha256', 'config_sha256')


def commit_bytes(record):
    """Canonical bytes of a COMMITTED.json record (what a publisher writes and what is hashed)."""
    return json.dumps(record, sort_keys=True, separators=(',', ':')).encode()


def verify(directory, run_id, data_version, model_version, world_size, accepted_record=None, identity=None,
           require_identity=False, pending_commit=None):
    given = Path(os.path.abspath(directory))
    require(not given.is_symlink(), 'checkpoint root is a symlink')
    require(given.is_dir(), 'checkpoint directory missing')
    # Ancestor symlinks are platform layout (macOS /tmp, /var; Linux mounts), not
    # checkpoint content: canonicalize once, then reject symlinks inside the root.
    root = given.resolve(strict=True)
    require(type(world_size) is int and 1 <= world_size <= 8, 'unsupported world size')
    if pending_commit is None:
        commit, commit_hash = read_json(root, 'COMMITTED.json')
    else:
        # Publisher check before the marker exists: verify everything the marker would vouch for.
        require(isinstance(pending_commit, dict), 'pending commit must be an object')
        require(not (root / 'COMMITTED.json').exists(), 'checkpoint already committed')
        commit, commit_hash = pending_commit, hashlib.sha256(commit_bytes(pending_commit)).hexdigest()
    require(type(commit.get('schema_version')) is int and commit['schema_version'] == 1, 'unsupported commit schema')
    require(commit.get('manifest') == 'manifest.json', 'unexpected manifest path')
    manifest, manifest_hash = read_json(root, 'manifest.json')
    require(commit.get('manifest_sha256') == manifest_hash, 'manifest hash mismatch')
    require(type(manifest.get('schema_version')) is int and manifest['schema_version'] == 1, 'unsupported manifest schema')
    require(type(manifest.get('world_size')) is int and 1 <= manifest['world_size'] <= 8, 'manifest world_size must be an integer 1..8')
    expected = {'run_id': run_id, 'data_version': data_version, 'model_version': model_version,
                'world_size': world_size}
    identity = identity or {}
    require(set(identity) <= set(IDENTITY_FIELDS), 'unsupported identity field')
    if require_identity:
        # A restore adapter that forgets an expected value must fail closed, not skip the check.
        for key in IDENTITY_FIELDS:
            require(key in identity, 'identity mismatch: expected ' + key + ' not supplied (require_identity)')
    for key, value in identity.items():
        require(isinstance(value, str) and value.strip(), 'empty expected identity: ' + key)
        expected[key] = value
    for key, value in expected.items():
        require(manifest.get(key) == value, 'identity mismatch: ' + key)
    require(commit.get('run_id') == run_id, 'commit run mismatch')
    step = manifest.get('global_step')
    require(type(step) is int and step >= 0 and type(commit.get('global_step')) is int and commit['global_step'] >= 0 and commit['global_step'] == step, 'step mismatch')
    if accepted_record is not None:
        require(isinstance(accepted_record, dict), 'accepted controller record must be an object')
        require(type(accepted_record.get('schema_version')) is int and accepted_record['schema_version'] == 1, 'unsupported controller record schema')
        require(accepted_record.get('run_id') == run_id, 'controller record run mismatch')
        require(type(accepted_record.get('global_step')) is int and accepted_record['global_step'] == step, 'controller record step mismatch')
        require(accepted_record.get('manifest_sha256') == manifest_hash, 'controller record manifest mismatch')
        require(accepted_record.get('commit_sha256') == commit_hash, 'controller record commit mismatch')
        attempt = accepted_record.get('attempt_id')
        generation = accepted_record.get('fencing_generation')
        require(isinstance(attempt, str) and bool(attempt.strip()), 'controller attempt identity missing')
        require(type(generation) is int and generation >= 0, 'controller fencing generation must be a nonnegative integer')
        for metadata in (manifest, commit):
            require(metadata.get('attempt_id') == attempt, 'unaccepted attempt lineage')
            require(type(metadata.get('fencing_generation')) is int and metadata['fencing_generation'] == generation, 'unaccepted fencing generation')
    require(manifest.get('optimizer_boundary') is True, 'not an optimizer boundary')
    require(type(manifest.get('scaler_used')) is bool, 'scaler applicability missing')
    entries = manifest.get('files')
    require(isinstance(entries, list) and entries, 'empty file inventory')
    seen, roles, ranks, total = set(), set(), {'rng': set(), 'data': set()}, 0
    for entry in entries:
        require(isinstance(entry, dict), 'invalid entry')
        path = entry.get('path')
        require(isinstance(path, str) and path.startswith('payload/'), 'payload prefix required')
        require(path not in seen, 'duplicate file path')
        seen.add(path)
        size, digest, _ = read_file(root, path)
        require(type(entry.get('bytes')) is int and entry['bytes'] == size, 'file size mismatch')
        require(isinstance(entry.get('sha256'), str) and re.fullmatch('[0-9a-f]{64}', entry['sha256']), 'invalid hash')
        require(entry['sha256'] == digest, 'file hash mismatch')
        require(('role' in entry) != ('roles' in entry), 'entry needs exactly one of role/roles')
        entry_roles = [entry['role']] if 'role' in entry else entry['roles']
        require(isinstance(entry_roles, list) and entry_roles, 'missing role')
        require(all(isinstance(r, str) and r for r in entry_roles), 'missing role')
        require(len(set(entry_roles)) == len(entry_roles), 'duplicate role in entry')
        for role in entry_roles:
            roles.add(role)
            if role in ranks:
                rank = entry.get('rank')
                require(type(rank) is int and 0 <= rank < world_size, 'invalid rank')
                ranks[role].add(rank)
        total += size
    derived = manifest.get('derived_roles', {})
    require(isinstance(derived, dict), 'derived_roles must be an object')
    for role, how in derived.items():
        require(role in DERIVABLE_ROLES, 'role cannot be derived: ' + str(role))
        require(role not in roles, 'role both stored and derived: ' + role)
        require(isinstance(how, dict) and isinstance(how.get('from'), list) and how['from']
                and all(isinstance(x, str) and x for x in how['from'])
                and isinstance(how.get('semantics'), str) and how['semantics'].strip(),
                'derived role needs nonempty from list and semantics')
    required = {'model', 'optimizer', 'scheduler', 'rng', 'data', 'training_state'}
    if manifest['scaler_used']:
        required.add('scaler')
    require(required <= roles | set(derived), 'required state role missing')
    require(all(r == set(range(world_size)) for k, r in ranks.items() if k not in derived), 'rank coverage missing')
    actual = set()
    for path in root.rglob('*'):
        require(not path.is_symlink(), 'symlink forbidden anywhere in checkpoint')
        if path.is_file():
            actual.add(path.relative_to(root).as_posix())
        else:
            require(path.is_dir(), 'nonregular checkpoint entry')
    markers = {'manifest.json'} | ({'COMMITTED.json'} if pending_commit is None else set())
    require(actual == seen | markers, 'unlisted files present')
    return {'ok': True, 'run_id': run_id, 'global_step': step,
            'manifest_sha256': manifest_hash, 'files_verified': len(seen), 'bytes_verified': total,
            'derived_roles': sorted(derived),
            'identity_checked': sorted(identity),
            'controller_acceptance': 'record_matched' if accepted_record is not None else 'not_checked',
            'restore_ready': False,
            'scope': 'local integrity and declared inventory; supplied controller record matched if present; journal authenticity, candidate selection and semantic restore unverified'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory')
    for name in ('run-id', 'data-version', 'model-version'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--world-size', required=True, type=int)
    for name in IDENTITY_FIELDS:
        parser.add_argument('--' + name.replace('_', '-'), help='expected ' + name + ' (restore must pass all three)')
    parser.add_argument('--require-identity', action='store_true',
                        help='fail unless all three expected identities were supplied (use for every restore)')
    parser.add_argument('--accepted-record', help='Multi-writer setups only: coordinator journal record for this exact accepted commit; not checkpoint-authored metadata')
    args = parser.parse_args()
    try:
        accepted = None
        if args.accepted_record:
            record_path = Path(os.path.abspath(args.accepted_record))
            require(not record_path.resolve().is_relative_to(Path(args.directory).resolve()), 'controller record must be external to checkpoint directory')
            accepted, _ = read_json(record_path.parent, record_path.name)
        identity = {k: getattr(args, k) for k in IDENTITY_FIELDS if getattr(args, k) is not None}
        result = verify(args.directory, args.run_id, args.data_version, args.model_version, args.world_size, accepted,
                        identity, args.require_identity)
    except (ValueError, OSError, TypeError, KeyError) as error:
        print(json.dumps({'ok': False, 'error': str(error)}))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == '__main__':
    sys.exit(main())
