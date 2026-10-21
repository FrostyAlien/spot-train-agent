#!/usr/bin/env python3
"""Render a SkyPilot task (JSON, valid YAML) from experiment.json. Never submits anything.

  render_job.py experiment.json --argv-json '[...]' [--variant V] [--secret NAME ...] > task.yaml
  render_job.py experiment.json --secret NAME ... --check-secret-file /abs/secrets.env
  render_job.py experiment.json --argv-json '[...]' --validate-with-skypilot      (start checks only)
  [--spent-before USD]          primary/fallback: the upper bound of earlier jobs of the run (the worker's limits)
  [--scheduling-timeout]        primary/fallback: add job.wait_for_scheduling_timeout (only where the chosen
                                SkyPilot accepts it: the start checks' round trip decides)

Variants: primary (the managed job), on-demand-fallback (the guard's pre-approved task), qualify (the real
GPUs: fresh then resume, measurements, journal checked), rehearsal (optional CPU fallback: wiring only),
verify (finish: FINAL.json checked
against the bucket). The task holds secret NAMES only (env SECRET_NAMES); values reach SkyPilot at launch
through `--secret-file`, never through this file.
"""
import argparse
import json
import math
import os
import re
import shlex
import stat
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit


HF_DATASET = re.compile(r'hf://datasets/[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*@[A-Za-z0-9._-]+(/[^?#]*)?')


def bucket_mount(uri, mode, mountpoint):
    """SkyPilot MOUNT/MOUNT_CACHED accept only a bucket root as source: mount the bucket and point
    the directory variable at the prefix inside it. COPY accepts the full URI, and so does an hf://
    repo (SkyPilot's hf-mount mounts sub-paths, read-only)."""
    u = urlsplit(uri)
    if u.scheme == 'hf':
        return {'source': uri, 'mode': mode}, mountpoint
    parts = [p for p in u.path.split('/') if p]
    # The bucket is the host for most schemes; Azure (https://account.../container) and IBM COS
    # (cos://region/bucket) carry it as the first path segment.
    in_path = u.scheme in ('https', 'cos') and parts
    root = f'{u.scheme}://{u.netloc}/{parts[0]}' if in_path else f'{u.scheme}://{u.netloc}'
    prefix = '/'.join(parts[1:] if in_path else parts)
    if mode == 'COPY' or not prefix:
        return {'source': uri, 'mode': mode}, mountpoint
    return {'source': root, 'mode': mode}, f'{mountpoint}/{prefix}'


DOCKER_LOGIN = ('SKYPILOT_DOCKER_USERNAME', 'SKYPILOT_DOCKER_PASSWORD', 'SKYPILOT_DOCKER_SERVER')
QUALIFY = ('qualify', 'rehearsal')
VARIANTS = ('primary', 'on-demand-fallback') + QUALIFY + ('verify',)
SECRET_NAME = re.compile(r'[A-Z_][A-Z0-9_]{0,63}')
HELPER_DIR = '/opt/gpu-skills'
HELPERS = ('checkpoint_marker.py', 'verify_checkpoint.py')
WATCHER = f'{HELPER_DIR}/progress_watch.py'       # the observe skill's stall watchdog, vendored in the image
PHASES = ('train', 'eval', 'save', 'final', 'startup')
LIMITS = {'train': 180, 'eval': 600, 'save': 600, 'final': 600, 'startup': 1800}   # progress_watch.py's defaults
CEILING_PHASES = ('eval', 'save', 'final')        # a ceiling caps one evaluation, save or end-of-training phase
SHUTDOWN_RESERVE_HOURS = (60 + 5 * 60) / 3600     # the guard's default poll plus shutdown reserve


def secret_names(m, secrets=()):
    """Every secret the launch must supply: the project's names, NOTIFY_URL when the contract names a
    notification channel (the worker sends notices itself), and for a private registry the three SkyPilot
    registry-login variables (SkyPilot needs them together, all as secrets)."""
    names = list(secrets)
    mon = m.get('monitoring') if isinstance(m.get('monitoring'), dict) else {}
    if isinstance(mon.get('notify'), dict) and m.get('tier', 'standard') != 'minimal' and 'NOTIFY_URL' not in names:
        names.append('NOTIFY_URL')
    if isinstance(m.get('image'), dict) and m['image'].get('registry_login') is True:
        names += [n for n in DOCKER_LOGIN if n not in names]
    if not (len(set(names)) == len(names) and all(isinstance(n, str) and SECRET_NAME.fullmatch(n) for n in names)):
        raise ValueError('secret names must be unique UPPER_CASE environment variable names')
    if set(names) & set(DOCKER_LOGIN) and not set(DOCKER_LOGIN) <= set(names):
        raise ValueError('SKYPILOT_DOCKER_USERNAME, _PASSWORD and _SERVER must be secrets together')
    return names


def check_secret_file(path, names) -> dict:
    """The dotenv file passed to `sky jobs launch --secret-file` has a value for every required name and
    is readable by its owner only. Prints names, never values."""
    p = Path(path)
    mode = p.stat().st_mode
    values = {}
    for line in p.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            k, v = line.split('=', 1)
            values[k.strip().removeprefix('export ').strip()] = v.strip().strip('"\'')
    missing = [n for n in names if not values.get(n)]
    problems = [f'missing or empty: {missing}'] if missing else []
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        problems.append(f'{path} is readable by others: chmod 600 it')
    return {'ok': not problems, 'required': list(names), 'problems': problems}


def _cpu_infra(provider: str, cpu_region) -> str:
    """Where a CPU-only task runs: the provider, pinned to a region that has CPU quota when given."""
    if cpu_region is None:
        return provider
    if not (isinstance(cpu_region, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', cpu_region)):
        raise ValueError('cpu_region must be a region name of the provider')
    return f'{provider}/{cpu_region}'


def default_helpers() -> dict:
    """The recover skill's checkpoint helpers and the observe skill's stall watchdog next to this skill
    (directory names may differ)."""
    skills = Path(__file__).resolve().parents[2]
    watch = sorted(skills.glob('*/scripts/progress_watch.py'))
    for d in [skills / 'gpu-training-recover' / 'scripts', *sorted(p.parent for p in skills.glob('*/scripts/checkpoint_marker.py'))]:
        if all((d / h).is_file() for h in HELPERS) and watch:
            return {**{f'{HELPER_DIR}/{h}': str(d / h) for h in HELPERS}, WATCHER: str(watch[0])}
    raise ValueError('checkpoint_marker.py, verify_checkpoint.py (recover skill) and progress_watch.py (observe skill) '
                     'not found next to this skill')


def watchdog_env(m, qualify_seconds=None) -> dict:
    """The stall watchdog's settings (observe skill: progress_watch.py) from the contract's optional `watchdog`:
    the quiet limit of each phase (seconds without a tick or bytes moved), optional ceilings, k. A qualification
    (qualify_seconds: its per-start timeout) uses the default limits, a startup limit inside that timeout so a
    startup hang ends with a STALL line, and no ceilings. Empty when the contract turns the watchdog off (the
    entrypoint cannot tick)."""
    w = m.get('watchdog', {})
    if not isinstance(w, dict):
        raise ValueError('watchdog must be an object')
    old = sorted(set(w) & {'floor_seconds', 'seed_gap_seconds', 'budgets'})
    if old:
        raise ValueError(f'watchdog.{", ".join(old)}: v0.8 keys; v0.9 judges silence: use watchdog.limits (quiet '
                         'seconds per phase, from the qualification\'s recommended.watchdog) and optional watchdog.ceilings')
    if set(w) - {'enabled', 'k', 'limits', 'ceilings', 'progress_period_seconds'}:
        raise ValueError('watchdog accepts enabled, k, limits, ceilings and progress_period_seconds')
    if w.get('enabled', True) is not True:
        if w.get('enabled') is False:
            return {}
        raise ValueError('watchdog.enabled must be true or false')
    def positive(x):
        return type(x) in (int, float) and math.isfinite(x) and x > 0
    for key, phases in (('limits', PHASES), ('ceilings', CEILING_PHASES)):
        v = w.get(key, {})
        if not (isinstance(v, dict) and set(v) <= set(phases) and all(positive(s) for s in v.values())):
            raise ValueError(f'watchdog.{key} must map {", ".join(phases)} to positive seconds')
    for key in ('k', 'progress_period_seconds'):
        if key in w and not positive(w[key]):
            raise ValueError(f'watchdog.{key} must be a positive number')
    qualify = qualify_seconds is not None
    limits = ({**LIMITS, 'startup': min(LIMITS['startup'], max(60, qualify_seconds - 120))} if qualify
              else {**LIMITS, **w.get('limits', {})})
    ceilings = {} if qualify else w.get('ceilings', {})
    env = {'STALL_LIMITS': ','.join(f'{p}={limits[p]}' for p in PHASES), 'STALL_K': str(w.get('k', 5)),
           'PROGRESS_PERIOD_SECONDS': str(w.get('progress_period_seconds', 300))}
    if ceilings:
        env['STALL_CEILINGS'] = ','.join(f'{p}={ceilings[p]}' for p in CEILING_PHASES if p in ceilings)
    return env


def render(m, argv=None, variant='primary', secrets=(), qualify_tag=None, timeout_minutes=20, helpers=None,
           cpu_region=None, eval_scale=None, spent_before=0.0, scheduling_timeout=False):
    """variant: primary (the managed job), on-demand-fallback (the guard's pre-approved task), a
    qualification run on a plain self-bounded cluster (qualify: the real GPUs, fresh then resume, a few
    steps each, measurements; rehearsal: the optional CPU fallback, the same image and buckets, wiring
    only), or verify (finish: a CPU cluster checks FINAL.json
    against the bucket). Qualification writes under /checkpoints/qualify/<run>/<variant>-<tag>, never the
    run's prefix. secrets: names only, exported as SECRET_NAMES; values come at launch (--secret-file).
    helpers: local copies mounted at /opt/gpu-skills for the verify task, which runs without the image.
    eval_scale (qualification): the full eval set over the subset's, passed to journal-check.
    spent_before (primary, fallback): the upper bound of earlier jobs of the run, for the worker's limits.
    scheduling_timeout (primary, fallback): add SkyPilot's job.wait_for_scheduling_timeout (first start only)."""
    def require(condition, message):
        if not condition:
            raise ValueError(message)
    require(isinstance(m, dict), 'Manifest must be an object')
    for field in ('gpu', 'image', 'dataset', 'checkpoint', 'recovery'):
        require(isinstance(m.get(field), dict), f'{field} must be an object')
    require(isinstance(m.get('budget', {}), dict), 'budget must be an object')
    require(type(m.get('schema_version')) is int and m['schema_version'] == 1, 'Unsupported schema_version')
    purchase_model = m.get('purchase_model')
    require(purchase_model in ('spot', 'on-demand'), 'Set explicit purchase_model: spot or on-demand before rendering this launch artifact')
    run = m.get('run_id', '')
    require(isinstance(run, str) and re.fullmatch(r'[a-z][a-z0-9-]{0,39}', run), 'Use a safe lowercase run_id')
    provider, region = m.get('provider'), m.get('region')
    require(isinstance(provider, str) and re.fullmatch(r'[a-z][a-z0-9-]*', provider), 'Concrete provider required')
    require(isinstance(region, str) and re.fullmatch(r'[A-Za-z0-9_.-]+', region), 'Concrete region required for this renderer')
    gpu = m['gpu']
    require(type(gpu.get('count')) is int and 1 <= gpu['count'] <= 8, 'GPU count must be 1..8')
    require(isinstance(gpu.get('model'), str) and re.fullmatch(r'[A-Za-z0-9_.-]+', gpu['model']), 'Concrete GPU model required')
    instance_type = gpu.get('instance_type')
    if 'instance_type' in gpu:
        require(isinstance(instance_type, str) and bool(re.fullmatch(r'[A-Za-z0-9_.:/-]+', instance_type)),
                'instance_type must be a concrete catalog identifier; verify it against the pinned provider catalog')
    image = m['image']
    digest, ref = image.get('digest'), image.get('reference')
    require(isinstance(digest, str) and re.fullmatch(r'sha256:[0-9a-f]{64}', digest), 'Exact sha256 image digest required')
    require(isinstance(ref, str) and ref and not any(c.isspace() for c in ref), 'Image reference required')
    ref = ref.removeprefix('docker:')
    if '@' in ref:
        require(ref.rsplit('@', 1)[1] == digest, 'Image digest mismatch')
    else:
        ref += '@' + digest
    def check_uri(field, uri):
        require(isinstance(uri, str) and bool(uri), f'{field} URI required')
        u = urlsplit(uri)
        if field.startswith('dataset') and u.scheme == 'hf':
            # Read straight from the Hugging Face Hub (no copy): read-only, pinned to a revision.
            require(bool(HF_DATASET.fullmatch(uri)), 'an hf:// dataset must be hf://datasets/<org>/<name>@<revision>[/<path>] '
                    '(pin a full commit; a branch moves)')
            return
        require(u.scheme in ('s3', 'gs', 'r2', 'https', 'oci', 'cos', 'vastdata', 'nebius', 'cw')  # SkyPilot storage schemes
                and bool(u.netloc) and not u.username and not u.password and not u.query and not u.fragment,
                f'{field} URI must use a recognized remote storage scheme and contain no credentials; durability still needs verification')
    for field in ('dataset', 'checkpoint'):
        check_uri(field, m[field].get('uri'))
    subset = m['dataset'].get('qualify_uri')       # the staged qualification subset (data skill), optional
    if subset is not None:
        check_uri('dataset.qualify_uri', subset)
    require(isinstance(m['dataset'].get('version'), str) and bool(m['dataset']['version'].strip()), 'Immutable dataset version must be a nonempty string')
    require(m['checkpoint'].get('run_id') == run, 'Checkpoint run identity mismatch')
    cp = m['checkpoint']
    if 'provider' in cp:
        require(cp['provider'] == provider, 'Checkpoint provider must equal training provider')
    if 'region' in cp:
        require(isinstance(cp['region'], str) and bool(re.fullmatch(r'[A-Za-z0-9_.-]+', cp['region'])), 'Checkpoint region must be resolved; review cross-region locality separately')
    if 'durable' in cp:
        require(cp['durable'] is True, 'Checkpoint durability must be verified independently of workers')
    if 'storage_type' in cp:
        require(cp['storage_type'] in ('object-store', 'retained-volume'), 'Unsupported checkpoint storage_type')
    if 'resume_policy' in cp:
        require(cp['resume_policy'] == 'latest-complete', 'Use latest-complete resume policy')
    # SkyPilot bucket mounts: checkpoints MOUNT_CACHED by default (SkyPilot's recommended checkpoint mode);
    # the dataset only when the contract chooses a mode (COPY staged, MOUNT streamed, MOUNT_CACHED cached).
    mounts, mount_env = {}, {}
    if cp.get('storage_type', 'object-store') == 'object-store':
        cp_mode = cp.get('mount_mode', 'MOUNT_CACHED')
        require(cp_mode in ('MOUNT_CACHED', 'MOUNT'), 'checkpoint.mount_mode must be MOUNT_CACHED or MOUNT')
        mounts['/checkpoints'], mount_env['CHECKPOINT_DIR'] = bucket_mount(cp['uri'], cp_mode, '/checkpoints')
        tuning = cp.get('mount_cached')
        if cp_mode == 'MOUNT_CACHED':
            # SkyPilot's preset for read-write checkpoints (parallel uploads, large read chunks); upload
            # order is not guaranteed, so select() verifies every file before restoring.
            mounts['/checkpoints']['type'] = 'MODEL_CHECKPOINT_RW'
            if tuning is not None:                         # the project's overrides of the preset, as given
                require(isinstance(tuning, dict) and bool(tuning) and all(isinstance(k, str) for k in tuning),
                        'checkpoint.mount_cached must be an object of SkyPilot mount_cached settings')
                mounts['/checkpoints']['config'] = {'mount_cached': tuning}
        require(tuning is None or cp_mode == 'MOUNT_CACHED', 'checkpoint.mount_cached needs mount_mode MOUNT_CACHED')
    ds_mode = m['dataset'].get('mount_mode')
    if m['dataset']['uri'].startswith('hf://'):
        require(ds_mode in ('MOUNT', 'COPY'), 'an hf:// dataset is mounted read-only (MOUNT) or downloaded (COPY)')
    if ds_mode is not None:
        require(ds_mode in ('COPY', 'MOUNT', 'MOUNT_CACHED'), 'dataset.mount_mode must be COPY, MOUNT or MOUNT_CACHED')
        mounts['/data'], mount_env['DATASET_DIR'] = bucket_mount(m['dataset']['uri'], ds_mode, '/data')
    monitoring_env = {}
    if 'monitoring' in m:
        require(isinstance(m['monitoring'], dict), 'monitoring must be an object')
        backend = m['monitoring'].get('metrics_backend')
        if backend is not None:
            require(isinstance(backend, str) and bool(re.fullmatch(r'[A-Za-z0-9_.-]+', backend)),
                    'monitoring.metrics_backend must be a simple name (the researcher chooses the tool)')
            monitoring_env['METRICS_BACKEND'] = backend
    if isinstance(m.get('monitoring'), dict) and 'wandb' in m['monitoring']:
        require(isinstance(m['monitoring']['wandb'], dict), 'monitoring.wandb must be an object')
        w = m['monitoring']['wandb']
        require(not (set(w) - {'enabled', 'entity', 'project', 'run_id', 'mode', 'history_policy'}), 'Unsupported W&B fields; never serialize secrets')
        require(w.get('history_policy', 'append') in ('append', 'rewind'), 'W&B history_policy must be append or rewind')
        require(type(w.get('enabled')) is bool, 'W&B enabled must be boolean')
        if w['enabled']:
            for key in ('entity', 'project', 'run_id'):
                require(isinstance(w.get(key), str) and bool(re.fullmatch(r'[A-Za-z0-9_.-]+', w[key])), 'Resolve safe W&B ' + key)
            require(w.get('mode') in ('online', 'offline'), 'W&B mode must be online or offline')
            monitoring_env.update({'WANDB_ENTITY': w['entity'], 'WANDB_PROJECT': w['project'],
                              'WANDB_RUN_ID': w['run_id'], 'WANDB_MODE': w['mode'],
                              'WANDB_LOG_MODEL': 'false', 'WANDB_WATCH': 'false', 'WANDB_DISABLE_CODE': 'true',
                              'WANDB_HISTORY_POLICY': w.get('history_policy', 'append')})
    rec = m['recovery']
    require(rec.get('allow_provider_change') is False, 'Provider switching must be disabled')
    regions = [region]
    if rec.get('allow_region_change') is True:
        # Provider/user choice: only with zero-cost storage shared by every allowed region.
        allowed = rec.get('allowed_regions')
        require(isinstance(allowed, list) and len(allowed) >= 2 and len(set(map(str, allowed))) == len(allowed)
                and all(isinstance(r, str) and re.fullmatch(r'[A-Za-z0-9_.-]+', r) for r in allowed)
                and region in allowed, 'allow_region_change needs >=2 unique concrete allowed_regions including region')
        ev = rec.get('region_change_evidence')
        require(isinstance(ev, dict) and ev.get('shared_storage') is True
                and type(ev.get('cross_region_transfer_cost_per_gib')) in (int, float)
                and ev['cross_region_transfer_cost_per_gib'] == 0
                and isinstance(ev.get('covers'), list) and {'checkpoint', 'dataset', 'image'} <= set(ev['covers']),
                'Region change requires evidence of zero-cost storage shared across allowed regions (checkpoint, dataset, image)')
        regions = [region] + [r for r in allowed if r != region]
    else:
        require(rec.get('allow_region_change') is False, 'Region changes require a separately approved plan')
    require(variant in VARIANTS, 'variant must be one of ' + ', '.join(VARIANTS))
    fallback = rec.get('on_demand_fallback')
    mon = m.get('monitoring') if isinstance(m.get('monitoring'), dict) else {}
    guard = mon.get('guard', 'none')
    require(guard in ('none', 'external'), 'monitoring.guard must be none or external')
    if rec.get('allow_on_demand') is True:
        # Without the guard SkyPilot falls back itself (ordered resources): no timed wait to configure.
        require(purchase_model == 'spot' and isinstance(fallback, dict) and fallback.get('approved') is True
                and (guard == 'none' or (type(fallback.get('after_capacity_wait_minutes')) in (int, float)
                                         and fallback['after_capacity_wait_minutes'] > 0)),
                'allow_on_demand needs an approved recovery.on_demand_fallback on a spot run '
                '(with the guard: after_capacity_wait_minutes > 0)')
    else:
        require(rec.get('allow_on_demand') is False and fallback is None,
                'On-demand fallback must be explicitly approved (allow_on_demand + on_demand_fallback)')
    if variant == 'on-demand-fallback':
        # A separate, pre-approved task the campaign guard launches; the primary stays spot-only.
        require(rec.get('allow_on_demand') is True, 'No approved on-demand fallback to render')
    managed = rec.get('enabled') is True
    app_restarts = rec.get('max_app_restarts', 0)
    require(type(app_restarts) is int and app_restarts >= 0,
            'max_app_restarts must be a nonnegative integer (SkyPilot default 0; each restart may re-rent a worker)')
    if 'max_capacity_wait_minutes' in rec:
        wait = rec['max_capacity_wait_minutes']
        require(type(wait) in (int, float) and math.isfinite(wait) and wait > 0, 'Capacity wait limit must be finite and positive; enforce through external watchdog')
    # Unknown budgets are allowed for planning. Reject malformed specified amounts.
    budget = m.get('budget', {})
    for field in ('total_limit', 'max_runtime_hours', 'controller_allowance', 'ceiling_hourly_usd'):
        x = budget.get(field)
        if x is not None:
            require(type(x) in (int, float) and math.isfinite(x) and x >= 0, f'Invalid {field}')
    deadline = budget.get('deadline_utc')
    if deadline is not None:
        require(isinstance(deadline, str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?Z', deadline),
                'budget.deadline_utc must be ISO-8601 UTC ending in Z')
    tier = m.get('tier', 'standard')
    require(tier in ('minimal', 'standard'), 'tier must be minimal or standard')
    autostop, timeout = {}, ''
    if tier == 'minimal':
        # Short attended on-demand run: one plain `sky launch` cluster bounded by its own timeout and
        # autodown, with no managed-jobs controller or campaign guard.
        require(purchase_model == 'on-demand' and not managed and rec.get('allow_on_demand') is False
                and variant != 'on-demand-fallback', 'Minimal tier is on-demand only, with recovery.enabled=false and no fallback')
        limit, idle = budget.get('max_worker_runtime_seconds'), budget.get('autodown_idle_minutes')
        require(type(limit) is int and limit > 0, 'Minimal tier needs integer budget.max_worker_runtime_seconds (hard run timeout)')
        require(type(idle) is int and idle >= 1, 'Minimal tier needs integer budget.autodown_idle_minutes >= 1')
        autostop = {'autostop': {'idle_minutes': idle, 'down': True}}
        timeout = f'timeout --signal=TERM --kill-after=120 {limit} '
    else:
        require(managed, 'Standard tier runs as a managed job (recovery.enabled=true); a plain sky launch is the Minimal tier')
        attempts = rec.get('max_attempts')        # optional absolute cap, enforced by the guard (not SkyPilot)
        require(attempts is None or (type(attempts) is int and attempts >= 1), 'max_attempts must be >= 1 or null')
        # Runtime backstop: no allocation outlives the budget's runway at the on-demand ceiling (the guard's
        # runway before earlier jobs' spend), so a run stays bounded if the guard host dies. It restarts with
        # each allocation; the guard bounds the total. Exit 124 is an application failure: the job ends.
        total, ceiling = budget.get('total_limit'), budget.get('ceiling_hourly_usd')
        if total is not None and ceiling:
            runway = (total - (budget.get('controller_allowance') or 0)) / ceiling - SHUTDOWN_RESERVE_HOURS
            require(runway > 0, 'the budget leaves no runway at budget.ceiling_hourly_usd')
            timeout = f'timeout --signal=TERM --kill-after=120 {math.ceil(runway * 3600)} '
    if argv is None:
        argv = ['python', '-c', 'raise SystemExit("Set the reviewed training argv and durable storage wiring before launch")']
    require(isinstance(argv, list) and argv and all(isinstance(x, str) and '\x00' not in x for x in argv), 'argv must be a nonempty JSON string array')
    use_spot = purchase_model == 'spot' and variant in ('primary', 'qualify')
    # An approved fallback without the guard: SkyPilot's ordered resources, spot first. EAGER_NEXT_REGION
    # relaunches from the whole list on every recovery, so spot is tried first again; FAILOVER would first
    # retry what launched last and stay on-demand after a fallback.
    ordered = variant == 'primary' and use_spot and rec.get('allow_on_demand') is True and guard == 'none'
    strategy = 'EAGER_NEXT_REGION' if ordered or len(regions) > 1 else 'FAILOVER'
    recovery = ({'job_recovery': {'strategy': strategy, 'max_restarts_on_errors': app_restarts}} if managed else {})
    # No `secrets:` section: a null-valued secret in a task file is a managed-secret reference that the
    # jobs controller cannot resolve without a secrets provider. Values come with --secret-file at launch.
    names = secret_names(m, secrets)
    envs = {'EXPERIMENT_RUN_ID': run, 'DATASET_URI': m['dataset']['uri'], **mount_env,
            'DATASET_VERSION': str(m['dataset']['version']), 'CHECKPOINT_URI': m['checkpoint']['uri'],
            # An image cannot know its own digest: the entrypoint writes it into every manifest from here.
            'IMAGE_DIGEST': digest, 'GPU_COUNT': str(int(gpu['count'])), **monitoring_env,
            # Progress bars rewrite one line with \r: they flood the job log and push the EVAL, commit and
            # journal lines out of the window the guard and the waits read.
            'TQDM_DISABLE': '1',
            'RUN_MODE': variant if variant in QUALIFY else 'train',
            **({'CAMPAIGN_DEADLINE_UTC': deadline} if deadline and variant not in QUALIFY else {}),
            **({'PURCHASE_MODE': 'on-demand-fallback'} if variant == 'on-demand-fallback' else {}),
            **({'SECRET_NAMES': ','.join(names)} if names else {})}
    if isinstance(mon.get('notify'), dict) and variant != 'verify':
        envs['NOTIFY_FORMAT'] = str(mon['notify'].get('format') or 'text')
    total, ceiling = budget.get('total_limit'), budget.get('ceiling_hourly_usd')
    if tier == 'standard' and variant in ('primary', 'on-demand-fallback') and total is not None and ceiling:
        # The run's limits, checked by the worker at every start (recover skill: checkpoint_marker.py limits).
        require(type(spent_before) in (int, float) and math.isfinite(spent_before) and spent_before >= 0,
                'spent_before must be a finite nonnegative amount')
        no_progress = rec.get('max_attempts_without_progress', 10)
        envs.update(CAMPAIGN_SUBMITTED_AT=str(int(time.time())), BUDGET_LIMIT_USD=str(total),
                    BUDGET_RESERVED_USD=str(budget.get('controller_allowance') or 0),
                    BUDGET_SPENT_BEFORE_USD=str(spent_before), CEILING_HOURLY_USD=str(ceiling),
                    FREE_WITHOUT_VM='1' if budget.get('free_without_vm') is True else '0',
                    STARTUP_ALLOWANCE_MINUTES=str(rec.get('startup_allowance_minutes', 10)),
                    MAX_ATTEMPTS_WITHOUT_PROGRESS=str(no_progress))
        if rec.get('max_attempts') is not None:
            envs['MAX_ATTEMPTS'] = str(rec['max_attempts'] + (rec.get('planned_drills') or 0))
    cap = budget.get('spot_price_cap')
    if isinstance(cap, dict) and use_spot and variant == 'primary':
        require(isinstance(cap.get('cap_id'), str) and re.fullmatch(r'[A-Za-z0-9_.:/-]{1,200}', cap['cap_id']),
                'budget.spot_price_cap.cap_id must be the provider object ID')
        envs['PRICE_CAP_ID'] = cap['cap_id']       # the worker caps its own VM (provider hook hook_price_cap)
    watch_env = watchdog_env(m, timeout_minutes * 60 if variant in QUALIFY else None) if variant != 'verify' else {}
    watch = f'python3 {WATCHER} watch -- ' if watch_env else ''      # wraps the launcher (observe skill)
    if watch_env:
        hb = '/tmp/heartbeat' + (f'/{variant}-{qualify_tag}' if variant in QUALIFY else '')
        # PyTorch's flight recorder (lifecycle references/pytorch.md): each rank makes <stem><rank>.pipe; on a
        # stall the watcher writes to every pipe matching STALL_DUMP_PIPES. Other frameworks ignore both.
        envs.update(watch_env, HEARTBEAT_DIR=hb, TORCH_NCCL_DEBUG_INFO_PIPE_FILE=f'{hb}/fr-pipe-',
                    STALL_DUMP_PIPES=f'{hb}/fr-pipe-*.pipe')
    if variant != 'verify':
        envs['PYTHONFAULTHANDLER'] = '1'                    # stacks when a rank dies of a fatal signal, watchdog or not
    disk = m.get('disk', {})
    require(isinstance(disk, dict), 'disk must be an object')
    if 'size_gb' in disk:
        require(type(disk['size_gb']) is int and 20 <= disk['size_gb'] <= 65536, 'disk.size_gb must be an integer (GB)')
    if ordered:
        placement = {'ordered': [{'infra': f'{provider}/{r}', 'use_spot': spot} for spot in (True, False) for r in regions]}
    else:
        placement = ({'infra': f'{provider}/{region}'} if len(regions) == 1 else
                     {'any_of': [{'infra': f'{provider}/{r}'} for r in regions]})
        placement['use_spot'] = use_spot
    resources = {**({'instance_type': instance_type} if instance_type is not None else {}),
                 **({'disk_size': disk['size_gb']} if 'size_gb' in disk else {}),
                 **placement,
                 'accelerators': f"{gpu['model']}:{gpu['count']}",
                 'image_id': 'docker:' + ref,
                 # Always explicit when managed: recent SkyPilot defaults to EAGER_NEXT_REGION.
                 **recovery, **autostop}
    if variant in QUALIFY:
        require(isinstance(qualify_tag, str) and bool(re.fullmatch(r'[A-Za-z0-9]{1,32}', qualify_tag)),
                'qualification needs a qualify_tag of 1-32 letters or digits')
        require('CHECKPOINT_DIR' in mount_env, 'qualification needs an object-store checkpoint mount')
        require(type(timeout_minutes) is int and 1 <= timeout_minutes <= 240, 'timeout_minutes must be 1..240')
        envs['CHECKPOINT_DIR'] = f'/checkpoints/qualify/{run}/{variant}-{qualify_tag}'
        envs['QUALIFY_TAG'] = qualify_tag
        envs['QUALIFY_SUBMITTED_AT'] = str(int(time.time()))   # launched right after rendering: journal-check's startup
        if 'WANDB_RUN_ID' in envs:                     # exercise the metrics path without touching the run
            envs['WANDB_RUN_ID'] = f'{run}-{variant}-{qualify_tag}'
        # A plain cluster bounded by its own timeout and autodown. The CPU rehearsal runs in a region with
        # CPU-only quota (--cpu-region; a GPU region can have none, and a quota error there stops SkyPilot's
        # failover), mounts the same buckets and streams the dataset (MOUNT) instead of copying it.
        if subset is not None:                        # the staged subset, mounted like the dataset
            require(ds_mode is not None, 'dataset.qualify_uri needs dataset.mount_mode (the subset is mounted like the dataset)')
            mounts['/data'], mount_env['DATASET_DIR'] = bucket_mount(subset, ds_mode, '/data')
            envs.update(DATASET_URI=subset, DATASET_DIR=mount_env['DATASET_DIR'])
        if variant == 'rehearsal':
            resources = {'infra': _cpu_infra(provider, cpu_region), 'cpus': '4+', 'memory': '16+',
                         'image_id': 'docker:' + ref, 'use_spot': False}
            if '/data' in mounts and mounts['/data']['mode'] != 'MOUNT':
                mounts['/data'], mount_env['DATASET_DIR'] = bucket_mount(subset or m['dataset']['uri'], 'MOUNT', '/data')
                envs['DATASET_DIR'] = mount_env['DATASET_DIR']
        else:
            resources = {k: v for k, v in resources.items() if k != 'job_recovery'}
        resources['autostop'] = {'idle_minutes': 10, 'down': True}
        # The helpers come from the image (vendored at /opt/gpu-skills by the entrypoint contract), so the
        # qualification proves the copies the main run will use. Nothing is mounted there: SkyPilot refuses a
        # file mount onto a path the image already has.
        # --kill-after leaves the watchdog time to pass the TERM on and kill a launcher that ignores it
        entry = f'timeout --signal=TERM --kill-after=120 {timeout_minutes * 60} ' + watch + shlex.join(argv)
        if eval_scale is not None:
            require(type(eval_scale) in (int, float) and math.isfinite(eval_scale) and eval_scale >= 1,
                    'eval_scale (the full eval set over the qualification eval set) must be a number >= 1')
        # The journal check is the qualification result (the task's exit code): the entrypoint runs twice,
        # a fresh start to its second commit, then a resume to the end and FINAL.json; on the GPUs the
        # preflight must be journaled too.
        check = (f'python3 {HELPER_DIR}/checkpoint_marker.py journal-check "$CHECKPOINT_DIR" --expect fresh,resume --require-final'
                 + ('' if variant == 'rehearsal' else ' --require preflight')
                 + ' ${QUALIFY_SUBMITTED_AT:+--submitted-at "$QUALIFY_SUBMITTED_AT"}'
                 + (' --watch-summaries "$HEARTBEAT_DIR/summaries.jsonl"' if watch else '')
                 + (f' --eval-scale {eval_scale}' if watch and eval_scale is not None else ''))
        runs = [dump_dir(envs) + entry, entry]
        task = {'name': f'{run}-{variant}', 'num_nodes': 1, 'api_server_access': False, 'resources': resources,
                'file_mounts': mounts, 'envs': envs,
                # QUALIFY-DONE (Unix seconds) ends the task's own work: from it to the launch's return is
                # SkyPilot's appended upload flush (one checkpoint's upload time). `set +e +o pipefail` lets it run as
                # written: under pipefail its `tac | grep -m 1 | grep -q` check fails on a long rclone log
                # (tac dies of SIGPIPE) and the flush never ends.
                'run': 'set -euo pipefail\n' + '\n'.join(runs + [check, 'echo "QUALIFY-DONE $(date -u +%s)"', 'set +e +o pipefail']) + '\n'}
        return task
    if variant == 'verify':
        require('CHECKPOINT_DIR' in mount_env, 'verify needs an object-store checkpoint mount')
        for remote, local in (helpers or {}).items():
            require(remote.startswith('/') and isinstance(local, str), 'helpers map absolute remote paths to local files')
        require(f'{HELPER_DIR}/checkpoint_marker.py' in (helpers or {}), f'verify needs the checkpoint helpers at {HELPER_DIR}')
        mount, ckdir = bucket_mount(cp['uri'], 'MOUNT', '/checkpoints')
        # The contract fixes run, data version, world size and image digest; the model, code and config
        # hashes the entrypoint computed are read from the final manifest and must verify with the rest.
        # Checkpoint directories without a marker (saves cut off by a preemption) are listed for the
        # finish inventory; deleting them is the user's decision, never the worker's.
        read = ('import json,sys,pathlib; r=pathlib.Path(sys.argv[1]); f=json.loads((r/"FINAL.json").read_text()); '
                'm=json.loads((r/f["final_dir"]/"manifest.json").read_text()); '
                'print(m["model_version"], m.get("code_sha256","-"), m.get("config_sha256","-"))')
        ids = ' '.join(['--run-id', shlex.quote(run), '--data-version', shlex.quote(str(m['dataset']['version'])),
                        '--world-size', str(int(gpu['count'])), '--image-digest', shlex.quote(digest)])
        h = f'{HELPER_DIR}/checkpoint_marker.py'
        script = '\n'.join([
            'set -uo pipefail', f'R={shlex.quote(ckdir)}',
            # Checkpoint directories safe to prune (deletes nothing); first, so a run without FINAL.json
            # yet (pruning during a long run) still gets the list before the check below stops.
            f'python3 {h} prune-list "$R" | sed "s/^/FINISH-PRUNE /"',
            # The lineage and any cut-off saves first: a stalled, cancelled or crashed run has no FINAL.json.
            f'python3 {h} journal-show "$R" | sed "s/^/FINISH-JOURNAL /"',
            'for d in "$R"/[0-9]*/; do [ -d "$d" ] || continue; [ -f "$d/COMMITTED.json" ] || echo "FINISH-PARTIAL $(basename "$d")"; done',
            f"read MV CS FS < <(python3 -c {shlex.quote(read)} \"$R\") || {{ echo 'FINISH no readable FINAL.json'; exit 3; }}",
            f'python3 {h} verify-final "$R" --mount-root /checkpoints {ids} --model-version "$MV" --code-sha256 "$CS" --config-sha256 "$FS" | sed "s/^/FINISH-VERIFY /"',
            'exit "${PIPESTATUS[0]}"'])
        return {'name': f'{run}-verify', 'num_nodes': 1, 'api_server_access': False,
                'resources': {'infra': _cpu_infra(provider, cpu_region), 'cpus': '2+', 'use_spot': False,
                              'autostop': {'idle_minutes': 10, 'down': True}},
                'file_mounts': {'/checkpoints': mount, **(helpers or {})},
                'envs': {'EXPERIMENT_RUN_ID': run, 'CHECKPOINT_DIR': ckdir}, 'run': script + '\n'}
    job = {}
    if scheduling_timeout and managed and variant in ('primary', 'on-demand-fallback'):
        wait = rec.get('max_capacity_wait_minutes')
        require(type(wait) in (int, float) and wait > 0, 'scheduling_timeout needs recovery.max_capacity_wait_minutes')
        # SkyPilot cancels a job that has not started within this time (first start only, never a recovery).
        job = {'job': {'wait_for_scheduling_timeout': f"{math.ceil(rec.get('startup_allowance_minutes', 10) + wait)}m"}}
    task = {'name': run if variant == 'primary' else run + ('-od' if variant == 'on-demand-fallback' else '-' + variant),
            'num_nodes': 1, 'api_server_access': False, 'resources': resources, **job,
            **({'file_mounts': mounts} if mounts else {}), 'envs': envs,
            # No `exec`, no `-e`, no pipefail: SkyPilot appends its cached-mount upload flush to this script, and
            # it must run after training ends (success or failure) so the last checkpoints and FINAL.json reach
            # the bucket before the cluster goes; the flush then exits with training's exit code. Under
            # pipefail its `tac | grep -m 1 | grep -q` check fails on a long rclone log and never ends.
            'run': 'set -u\n' + dump_dir(envs) + timeout + watch + shlex.join(argv) + '\n'}
    return task


def dump_dir(envs) -> str:
    """run: lines that point PyTorch's flight-recorder dumps (on a collective timeout, or at the watcher's request
    on a stall) into the run's durable directory, one folder per worker start. A value the project set wins."""
    if 'CHECKPOINT_DIR' not in envs:
        return ''
    return ('export TORCH_FR_DUMP_TEMP_FILE="${TORCH_FR_DUMP_TEMP_FILE:-$CHECKPOINT_DIR/debug/$(date -u +%Y%m%dT%H%M%SZ)/fr-}"\n'
            'export TORCH_NCCL_DEBUG_INFO_TEMP_FILE="$TORCH_FR_DUMP_TEMP_FILE"   # its name before PyTorch 2.7\n'
            'mkdir -p "${TORCH_FR_DUMP_TEMP_FILE%/*}" || true\n')


def validate_with_skypilot(job):
    """Start check with the INSTALLED SkyPilot (offline, no cloud calls): parse the task, serialize it as
    the client does before sending it to the API server and jobs controller, reload it, and require the
    two serializations to match. Also refuses managed-secret references (a null-valued `secrets:` entry,
    which the controller cannot resolve without a secrets provider). Catches schema drift after an
    upgrade; behaviour changes (defaults) still need the release-note skim of the start checks."""
    try:
        import sky
    except ImportError as e:
        raise ValueError('SkyPilot is not installed in this environment; install the SkyPilot version chosen for this project to validate') from e
    task = sky.Task.from_yaml_config(json.loads(json.dumps(job)))
    if getattr(task, 'managed_secret_refs', None):
        raise ValueError(f'task has managed-secret references {task.managed_secret_refs}: pass secret values with --secret-file at launch instead')
    # Parsing defers storage-source checks to launch time; run SkyPilot's own offline check now
    # (for example MOUNT modes reject a bucket sub-path). Private API: skipped if it disappears.
    for storage in getattr(task, 'storage_mounts', {}).values():
        check = getattr(storage, '_validate_storage_spec', None)
        if check is not None:
            check(storage.name)
    to_config = getattr(task, 'to_yaml_config', None)
    if to_config is not None:
        def serial(t):
            c = t.to_yaml_config()
            c.pop('_user_specified_yaml', None)
            return json.loads(json.dumps(c, default=str))
        first = serial(task)
        second = serial(sky.Task.from_yaml_config(json.loads(json.dumps(first))))
        if first != second:
            changed = sorted(k for k in set(first) | set(second) if first.get(k) != second.get(k))
            raise ValueError(f'task does not survive a serialize/reload round trip (fields {changed})')
    return getattr(sky, '__version__', 'unknown')


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('manifest')
    p.add_argument('--argv-json', help='Reviewed training argv as a JSON string array')
    p.add_argument('--variant', default='primary', choices=VARIANTS,
                   help='on-demand-fallback: the pre-approved task the guard may launch; qualify: the GPU '
                        'qualification (fresh then resume); rehearsal: the optional CPU fallback; verify: '
                        'the finish check of FINAL.json')
    p.add_argument('--secret', action='append', default=[], help='secret NAME only; the value comes at launch')
    p.add_argument('--qualify-tag', help='qualify/rehearsal: tag of the qualification prefix')
    p.add_argument('--timeout-minutes', type=int, default=20, help='qualify/rehearsal: hard timeout of each run')
    p.add_argument('--eval-scale', type=float, help='qualify/rehearsal: the full eval set size over the qualification '
                   'eval set size; scales the evaluation tail in the recommended watchdog limits (omit when the project '
                   'does not evaluate)')
    p.add_argument('--cpu-region', help='rehearsal/verify: a region of the provider with CPU-only quota '
                                        '(for example the control host region)')
    p.add_argument('--spent-before', type=float, default=0.0,
                   help="primary/fallback: upper bound of earlier jobs of this run (the worker's limits)")
    p.add_argument('--scheduling-timeout', action='store_true',
                   help="primary/fallback: add SkyPilot's job.wait_for_scheduling_timeout (if the chosen version accepts it)")
    p.add_argument('--check-secret-file', metavar='PATH',
                   help='check the dotenv file for `sky jobs launch --secret-file` instead of rendering')
    p.add_argument('--validate-with-skypilot', action='store_true',
                   help='start checks: parse and round-trip the result with the installed SkyPilot (no cloud calls)')
    args = p.parse_args()
    try:
        with open(args.manifest) as f:
            m = json.load(f)
        if args.check_secret_file:
            result = check_secret_file(args.check_secret_file, secret_names(m, args.secret))
            print(json.dumps(result))
            return 0 if result['ok'] else 1
        argv = json.loads(args.argv_json) if args.argv_json else None
        helpers = default_helpers() if args.variant == 'verify' else None
        result = render(m, argv, args.variant, args.secret, args.qualify_tag, args.timeout_minutes, helpers,
                        args.cpu_region, args.eval_scale, args.spent_before, args.scheduling_timeout)
        if args.validate_with_skypilot:
            # SkyPilot may print to stdout while it loads (a catalog refresh writes progress records):
            # keep stdout for the rendered task only.
            import contextlib
            with contextlib.redirect_stdout(sys.stderr):
                version = validate_with_skypilot(result)
            print(f'Parsed and round-tripped by installed SkyPilot {version}.', file=sys.stderr)
    except (ValueError, KeyError, TypeError, OSError) as e:
        p.exit(2, f'Cannot render: {e}\n')
    print(json.dumps(result, indent=2))
    names = result.get('envs', {}).get('SECRET_NAMES')
    if names:
        print(f'Launch with --secret-file holding: {names} (check it with --check-secret-file).', file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
