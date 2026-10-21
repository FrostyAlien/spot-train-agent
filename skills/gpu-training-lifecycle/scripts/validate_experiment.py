#!/usr/bin/env python3
"""Offline experiment-contract validation. Never grants approval or calls a cloud API."""
import argparse
import json
import math
import re
import sys
from pathlib import Path

STATES = {'planning','ready','running','recovering','paused','completed','cancelled','failed'}
HF_DATASET = re.compile(r'hf://datasets/[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*@[A-Za-z0-9._-]+(/[^?#]*)?')
SHUTDOWN_RESERVE_HOURS = (60 + 5 * 60) / 3600     # the guard's default poll plus shutdown reserve
# Where the SkyPilot API server runs. The first three are the older names, still accepted.
SELF_HOSTED = {'self-hosted', 'existing-host'}
CONTROLLER_MODES = SELF_HOSTED | {'existing', 'provider-managed', 'local', 'remote-api-server', 'standalone-managed'}
NOTIFY_FORMATS = ('text', 'slack', 'discord', 'json')    # observe skill: notify.py
GUARD_MODES = ('none', 'external')

REGION_EVIDENCE_COVERS={'checkpoint','dataset','image'}

def region_policy_errors(d):
    """Region change is opt-in and only with zero-cost shared storage across allowed regions."""
    errors=[]
    rec=d.get('recovery',{}) if isinstance(d.get('recovery'),dict) else {}
    allowed=rec.get('allowed_regions')
    region=d.get('region')
    if 'allowed_regions' in rec:
        if (not isinstance(allowed,list) or not allowed or len(set(map(str,allowed)))!=len(allowed)
                or not all(isinstance(r,str) and re.fullmatch(r'[A-Za-z0-9_.-]+',r) for r in allowed)):
            return ['recovery.allowed_regions must be a nonempty list of unique concrete regions']
        if region is not None and region not in allowed: errors.append('recovery.allowed_regions must include region')
    if rec.get('allow_region_change') is True:
        if not isinstance(allowed,list) or len(allowed)<2:
            errors.append('allow_region_change requires recovery.allowed_regions with at least two regions')
        ev=rec.get('region_change_evidence')
        if not isinstance(ev,dict):
            errors.append('allow_region_change requires recovery.region_change_evidence')
        else:
            if ev.get('shared_storage') is not True: errors.append('region change requires storage shared across allowed regions')
            cost=ev.get('cross_region_transfer_cost_per_gib')
            if isinstance(cost,bool) or not isinstance(cost,(int,float)) or cost!=0:
                errors.append('region change requires zero cross-region transfer cost per GiB')
            covers=ev.get('covers')
            if not isinstance(covers,list) or not REGION_EVIDENCE_COVERS<=set(covers):
                errors.append('region_change_evidence.covers must include checkpoint, dataset and image')
            for key in ('checked_at','source'):
                if not isinstance(ev.get(key),str) or not ev[key].strip(): errors.append(f'region_change_evidence.{key} required')
    elif isinstance(allowed,list) and allowed and region is not None and allowed!=[region]:
        errors.append('allowed_regions beyond region requires allow_region_change=true')
    return errors

def validate(d, phase='plan', locked=None):
    errors=[]
    if not isinstance(d,dict):
        return ['contract must be an object']
    for key in ('gpu','image','dataset','checkpoint','budget','recovery','controller','approval'):
        if not isinstance(d.get(key),dict): errors.append(f'{key} must be an object')
    if errors: return errors
    def err(msg): errors.append(msg)
    def field(path, optional=False):   # an optional key may be absent (the same as null)
        obj=d
        for key in path.split('.'):
            if not isinstance(obj,dict) or key not in obj:
                if not optional: err(f'missing {path}')
                return None
            obj=obj[key]
        return obj
    def text(path, required=False):
        value=field(path, optional=not required)
        if value is None and not required: return
        if not isinstance(value,str) or not value.strip(): err(f'{path} must be a nonempty string')
    def number(path, positive=False, optional=False):
        value=field(path, optional)
        if value is None and optional: return
        if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or value < 0 or (positive and value == 0):
            err(f'{path} must be finite and {"positive" if positive else "nonnegative"}')
    if field('schema_version') != 1 or type(d.get('schema_version')) is not int: err('schema_version must equal integer 1')
    text('run_id',True)
    if field('state') not in STATES: err('invalid state')
    required = phase == 'launch' or d.get('state') in {'ready','running','recovering'}
    tier=d.get('tier')
    if tier not in (None,'minimal','standard'): err('tier must be minimal or standard')
    minimal=tier=='minimal'   # absent means standard, the stricter tier
    if d.get('purchase_model') not in ({'spot','on-demand'} if required else {None,'spot','on-demand'}): err('purchase_model must be spot or on-demand for launch')
    for path in ('provider','region','gpu.model','image.reference','image.digest','dataset.uri','dataset.version','checkpoint.uri'):
        text(path,required)
    text('controller.mode',required and not minimal)   # Minimal has no managed-jobs controller
    # The controller state is the user's to keep only on a server they run.
    text('controller.state_uri',required and not minimal and d.get('controller',{}).get('mode') in SELF_HOSTED)
    ctl=d.get('controller') if isinstance(d.get('controller'),dict) else {}
    endpoint=ctl.get('endpoint')
    if endpoint is not None and (not isinstance(endpoint,str) or re.search(r'[@?#\s]',endpoint) or '://' in endpoint):
        err('controller.endpoint is the server host (and port) only: no scheme, user, password or query')
    if ctl.get('server_version') is not None and not (isinstance(ctl['server_version'],str) and ctl['server_version'].strip()):
        err('controller.server_version must be the version string `sky api info` reports')
    count=field('gpu.count')
    if isinstance(count,bool) or not isinstance(count,int) or not 1 <= count <= 8: err('gpu.count must be integer 1..8 for this suite')
    if 'instance_type' in d['gpu']:
        t=d['gpu']['instance_type']
        if not isinstance(t,str) or not re.fullmatch(r'[A-Za-z0-9_.:/-]+',t):err('gpu.instance_type must be a concrete catalog identifier')
    if 'max_worker_runtime_seconds' in d['budget']:number('budget.max_worker_runtime_seconds',positive=True)
    if field('checkpoint.run_id') != d.get('run_id'): err('checkpoint.run_id must match run_id')
    currency=field('budget.currency')
    if not isinstance(currency,str) or not re.fullmatch('[A-Z]{3}',currency): err('budget.currency must be a 3-letter uppercase code')
    number('budget.total_limit',positive=True,optional=not required)
    number('budget.max_runtime_hours',positive=True,optional=not (required and minimal))   # Standard: the deadline bounds time
    number('budget.controller_allowance',optional=not required)
    budget=d.get('budget',{})
    if isinstance(budget,dict):
        a,b=budget.get('controller_allowance'),budget.get('total_limit')
        if isinstance(a,(int,float)) and isinstance(b,(int,float)) and a>b: err('controller_allowance must be included within total_limit')
    for path in ('recovery.enabled','recovery.allow_provider_change','recovery.allow_region_change','recovery.allow_on_demand'):
        if not isinstance(field(path),bool): err(f'{path} must be boolean')
    if field('recovery.allow_provider_change') is not False: err('this suite forbids provider change within a run')
    fallback=d.get('recovery',{}).get('on_demand_fallback') if isinstance(d.get('recovery'),dict) else None
    if field('recovery.allow_on_demand') is True:
        # Never silent: only a user-approved fallback within the same provider. Without the guard SkyPilot
        # falls back itself (ordered resources, spot first); with the guard it waits after_capacity_wait_minutes.
        if d.get('purchase_model') not in (None,'spot'): err('on-demand fallback applies only to spot runs')
        mon=d.get('monitoring') if isinstance(d.get('monitoring'),dict) else {}
        timed=mon.get('guard','none')=='external'
        wait_ok=(isinstance(fallback,dict) and isinstance(fallback.get('after_capacity_wait_minutes'),(int,float))
                 and not isinstance(fallback.get('after_capacity_wait_minutes'),bool) and fallback['after_capacity_wait_minutes']>0)
        if not (isinstance(fallback,dict) and fallback.get('approved') is True and (wait_ok or not timed)):
            err('allow_on_demand requires recovery.on_demand_fallback {approved: true} '
                '(with monitoring.guard external also after_capacity_wait_minutes > 0)')
        elif wait_ok:
            wait=d['recovery'].get('max_capacity_wait_minutes')
            if isinstance(wait,(int,float)) and not isinstance(wait,bool) and fallback['after_capacity_wait_minutes']>=wait:
                err('on_demand_fallback.after_capacity_wait_minutes must be less than max_capacity_wait_minutes')
    elif fallback is not None: err('recovery.on_demand_fallback requires allow_on_demand=true')
    if 'max_capacity_wait_minutes' in d['recovery']: number('recovery.max_capacity_wait_minutes',positive=True)
    if 'startup_allowance_minutes' in d['recovery']: number('recovery.startup_allowance_minutes')
    drills=d['recovery'].get('planned_drills',0)
    if isinstance(drills,bool) or not isinstance(drills,int) or drills<0: err('recovery.planned_drills must be a nonnegative integer (drills consume attempts)')
    if 'registry_login' in d.get('image',{}) and not isinstance(d['image']['registry_login'],bool): err('image.registry_login must be true or false')
    for msg in region_policy_errors(d): err(msg)
    attempts=d['recovery'].get('max_attempts')            # absolute cap on allocations; null = none
    restarts=d.get('recovery',{}).get('max_app_restarts',0) if isinstance(d.get('recovery'),dict) else 0
    if isinstance(restarts,bool) or not isinstance(restarts,int) or restarts<0: err('recovery.max_app_restarts must be a nonnegative integer')
    if attempts is not None and (isinstance(attempts,bool) or not isinstance(attempts,int) or attempts < 0): err('recovery.max_attempts must be a nonnegative integer or null (no absolute cap)')
    stalled=d['recovery'].get('max_attempts_without_progress')
    if stalled is not None and (type(stalled) is not int or stalled<1): err('recovery.max_attempts_without_progress must be a positive integer')
    if 'free_without_vm' in d['budget'] and not isinstance(d['budget']['free_without_vm'],bool): err('budget.free_without_vm must be true or false (from the provider research record)')
    cap=d['budget'].get('spot_price_cap')
    if cap is not None:
        price=cap.get('per_gpu_hour') if isinstance(cap,dict) else None
        if not (isinstance(cap,dict) and set(cap)<={'per_gpu_hour','cap_id'} and isinstance(price,(int,float))
                and not isinstance(price,bool) and math.isfinite(price) and price>0
                and isinstance(cap.get('cap_id'),str) and re.fullmatch(r'[A-Za-z0-9_.:/-]{1,200}',cap['cap_id'])):
            err('budget.spot_price_cap must be {per_gpu_hour > 0, cap_id: the provider object ID}')
        elif d.get('purchase_model') not in (None,'spot'):
            err('budget.spot_price_cap applies only to spot runs')
    if d['budget'].get('planned_job_hours') is not None: number('budget.planned_job_hours',positive=True)
    code=d.get('code')
    if code is not None and not isinstance(code,dict): err('code must be an object {repo, commit}')
    elif isinstance(code,dict):
        for key in ('repo','commit'): text('code.'+key,required)
        c=code.get('commit')
        if isinstance(c,str) and c.strip() and not re.fullmatch(r'[0-9a-f]{7,40}',c): err('code.commit must be a git commit hash (the code baked into the image)')
    elif required: err('launch requires code {repo, commit}: where the training code lives and the exact commit in the image')
    deadline=d['budget'].get('deadline_utc')           # optional: none by default, the budget is the hard stop
    if deadline is not None and (not isinstance(deadline,str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?Z',deadline)):
        err('budget.deadline_utc must be ISO-8601 UTC ending in Z, or null (no deadline)')
    disk=d.get('disk')
    if disk is not None and (not isinstance(disk,dict) or ('size_gb' in disk and (type(disk['size_gb']) is not int or not 20<=disk['size_gb']<=65536))):
        err('disk.size_gb must be an integer number of GB (the worker boot disk; SkyPilot default otherwise)')
    if required:
        if not minimal and d.get('controller',{}).get('mode') not in CONTROLLER_MODES:
            err('controller.mode must be self-hosted, existing, provider-managed or local')
        digest=d.get('image',{}).get('digest')
        if not isinstance(digest,str) or not re.fullmatch(r'sha256:[0-9a-f]{64}',digest): err('image.digest must be an immutable sha256 digest')
    if not isinstance(field('approval.scope'),list): err('approval.scope must be an array, never authorization itself')
    if field('approval.status') not in {'pending','approved','declined','expired'}: err('invalid approval.status')
    mode=d.get('approval',{}).get('mode')
    if mode is not None and mode not in {'automatic','gated','steps'}: err('approval.mode must be automatic, gated or steps (the interaction level asked at intake; descriptive, never authorization)')
    if phase == 'launch':
        for path in ('checkpoint.provider','checkpoint.region','checkpoint.storage_type','checkpoint.resume_policy'):
            text(path,True)
        if d['checkpoint'].get('durable') is not True: err('launch requires checkpoint.durable=true with independent service evidence')
        if d.get('purchase_model')=='spot' and 'max_capacity_wait_minutes' not in d['recovery']: err('spot launch requires a bounded recovery.max_capacity_wait_minutes')
        if d['recovery'].get('enabled') is True and attempts is not None and (type(attempts) is not int or attempts < 1): err('enabled recovery requires max_attempts >= 1 or null (no absolute cap)')
        # Metrics tooling is the researcher's choice (W&B, MLflow, TensorBoard, none...), but it must be explicit.
        mon=d.get('monitoring') if isinstance(d.get('monitoring'),dict) else {}
        wandb_on=isinstance(mon.get('wandb'),dict) and mon['wandb'].get('enabled') is True
        backend=mon.get('metrics_backend')
        if not wandb_on and not (isinstance(backend,str) and backend.strip()):
            err('launch requires an explicit metrics choice: monitoring.wandb.enabled=true or monitoring.metrics_backend (e.g. mlflow, tensorboard, none)')
        # Runtime enforcement input for the campaign guard (the deadline is optional, checked below).
        ceiling=d['budget'].get('ceiling_hourly_usd')
        if isinstance(ceiling,bool) or not isinstance(ceiling,(int,float)) or not ceiling>0:
            err('launch requires budget.ceiling_hourly_usd: the node on-demand rate, the most a price-following spot VM can bill')
        notify=d.get('monitoring',{}).get('notify') if isinstance(d.get('monitoring'),dict) else None
        if not minimal and (not isinstance(notify,dict) or not isinstance(notify.get('channel'),str) or not notify['channel'].strip()):
            err('launch requires monitoring.notify.channel so stops reach a person (no secrets in this file)')
        b=d['budget']
        def num(v): return isinstance(v,(int,float)) and not isinstance(v,bool) and math.isfinite(v)
        if minimal:
            # One plain `sky launch` cluster: its own timeout and autodown bound the cost, so prove it here.
            if d.get('purchase_model')!='on-demand' or d['recovery'].get('enabled') is not False or d['recovery'].get('allow_on_demand') is not False:
                err('tier minimal is on-demand only, with recovery.enabled=false and no fallback (plain sky launch, no controller or guard)')
            limit,idle=b.get('max_worker_runtime_seconds'),b.get('autodown_idle_minutes')
            if type(limit) is not int or limit<=0: err('tier minimal requires integer budget.max_worker_runtime_seconds (hard run timeout)')
            if type(idle) is not int or idle<1: err('tier minimal requires integer budget.autodown_idle_minutes >= 1')
            hours,total,allowance=b.get('max_runtime_hours'),b.get('total_limit'),b.get('controller_allowance') or 0
            if type(limit) is int and type(idle) is int and num(hours) and limit/3600+idle/60>hours:
                err('budget.max_runtime_hours must exceed the run timeout plus idle autodown (add the expected startup time yourself)')
            if num(hours) and num(total) and num(ceiling) and num(allowance) and hours*ceiling+allowance>total:
                err('tier minimal: max_runtime_hours x ceiling_hourly_usd + controller_allowance exceeds total_limit')
        elif d['recovery'].get('enabled') is not True:
            err('tier standard runs as a managed job: recovery.enabled must be true (a plain sky launch is tier minimal)')
        else:
            planned,total,allowance=b.get('planned_job_hours'),b.get('total_limit'),b.get('controller_allowance') or 0
            if num(planned) and num(total) and num(ceiling) and ceiling>0 and num(allowance):
                runway=(total-allowance)/ceiling-SHUTDOWN_RESERVE_HOURS
                if runway<planned: err(f'budget runway {runway:.2f} h at the ceiling is shorter than budget.planned_job_hours {planned}: '
                                       "the run's limits would stop the job early; raise total_limit or plan less")
    cp=d['checkpoint']
    for key in ('provider','region'):
        if key in cp:
            text('checkpoint.'+key,required)
    if cp.get('provider') is not None and cp['provider'] != d.get('provider'): err('checkpoint.provider must equal training provider')
    if 'durable' in cp and cp['durable'] is not True: err('checkpoint.durable must be true; verify service lifetime separately')
    if 'storage_type' in cp and cp['storage_type'] not in ('object-store','retained-volume'): err('unsupported checkpoint.storage_type')
    if 'resume_policy' in cp and cp['resume_policy'] != 'latest-complete': err('checkpoint.resume_policy must be latest-complete')
    if 'mount_mode' in cp and cp['mount_mode'] not in ('MOUNT_CACHED','MOUNT'): err('checkpoint.mount_mode must be MOUNT_CACHED or MOUNT')
    if 'mount_cached' in cp and (not isinstance(cp['mount_cached'],dict) or not cp['mount_cached'] or cp.get('mount_mode','MOUNT_CACHED')!='MOUNT_CACHED'):
        err('checkpoint.mount_cached must be a nonempty object of SkyPilot mount_cached settings (overrides of its MODEL_CHECKPOINT_RW preset) with mount_mode MOUNT_CACHED')
    ds=d['dataset']
    if ds.get('mount_mode') is not None and ds['mount_mode'] not in ('COPY','MOUNT','MOUNT_CACHED'): err('dataset.mount_mode must be COPY, MOUNT, MOUNT_CACHED or null')
    if isinstance(ds.get('uri'),str) and ds['uri'].startswith('hf://'):
        if not HF_DATASET.fullmatch(ds['uri']): err('an hf:// dataset must be hf://datasets/<org>/<name>@<revision>[/<path>] (pin a full commit)')
        if ds.get('mount_mode') not in ('MOUNT','COPY'): err('an hf:// dataset is read with mount_mode MOUNT or COPY')
    if isinstance(cp.get('uri'),str) and cp['uri'].startswith('hf://'): err('checkpoint.uri must be the provider\'s object storage, not a Hugging Face repo')
    w=d.get('watchdog')                        # the worker's stall watchdog (observe skill: progress_watch.py)
    if w is not None:
        pos=lambda v: isinstance(v,(int,float)) and not isinstance(v,bool) and math.isfinite(v) and v>0
        old=sorted(set(w)&{'floor_seconds','seed_gap_seconds','budgets'}) if isinstance(w,dict) else []
        if old: err(f"watchdog.{', '.join(old)}: v0.8 keys; v0.9 judges silence: use watchdog.limits (quiet seconds per phase, from the qualification's recommended.watchdog) and optional watchdog.ceilings")
        elif not isinstance(w,dict) or set(w)-{'enabled','k','limits','ceilings','progress_period_seconds'}:
            err('watchdog accepts enabled, k, limits, ceilings and progress_period_seconds')
        else:
            if 'enabled' in w and type(w['enabled']) is not bool: err('watchdog.enabled must be true or false')
            for key in ('k','progress_period_seconds'):
                if key in w and not pos(w[key]): err(f'watchdog.{key} must be a positive number')
            for key,phases in (('limits',('train','eval','save','final','startup')),('ceilings',('eval','save','final'))):
                b=w.get(key,{})
                if not isinstance(b,dict) or set(b)-set(phases) or not all(pos(v) for v in b.values()):
                    err(f"watchdog.{key} maps {', '.join(phases)} to positive seconds")
    sub=ds.get('qualify_uri')                  # the staged qualification subset (data skill: staging)
    if sub is not None:
        if not isinstance(sub,str) or not re.fullmatch(r'[a-z][a-z0-9]*://\S+',sub) or (sub.startswith('hf://') and not HF_DATASET.fullmatch(sub)):
            err('dataset.qualify_uri must be the storage URI of the staged qualification subset (e.g. <dataset uri>/qualify)')
        elif ds.get('mount_mode') is None: err('dataset.qualify_uri needs dataset.mount_mode (the subset is mounted like the dataset)')
    if 'monitoring' in d:
        monitoring=d['monitoring']
        if not isinstance(monitoring,dict): err('monitoring must be an object')
        else:
            if monitoring.get('metrics_backend') is not None and not (isinstance(monitoring['metrics_backend'],str) and monitoring['metrics_backend'].strip()):
                err('monitoring.metrics_backend must be a nonempty name (e.g. mlflow, tensorboard, none)')
            if monitoring.get('guard','none') not in GUARD_MODES:
                err('monitoring.guard must be none (limits in the worker) or external (the optional campaign guard too)')
            notify=monitoring.get('notify')
            if isinstance(notify,dict) and notify.get('format','text') not in NOTIFY_FORMATS:
                err('monitoring.notify.format must be one of '+', '.join(NOTIFY_FORMATS)+' (the URL is a secret, never here)')
            if isinstance(notify,dict) and set(notify)-{'channel','format'}:
                err('monitoring.notify holds only channel and format; the URL is the NOTIFY_URL secret')
            if 'wandb' in monitoring and not isinstance(monitoring['wandb'],dict): err('monitoring.wandb must be an object')
            elif 'wandb' in monitoring:
                w=monitoring['wandb']
                if set(w)-{'enabled','entity','project','run_id','mode','history_policy'}: err('unsupported monitoring.wandb fields; never store secrets')
                if w.get('history_policy','append') not in ('append','rewind'): err('monitoring.wandb.history_policy must be append or rewind (user choice)')
                if type(w.get('enabled')) is not bool: err('monitoring.wandb.enabled must be boolean')
                if w.get('enabled') is True:
                    for key in ('entity','project','run_id'): text('monitoring.wandb.'+key,required)
                    if w.get('mode') not in ('online','offline'): err('monitoring.wandb.mode must be online or offline')
    # Deliberately do not accept approval.status as permission to act.
    if locked is not None:
        if not isinstance(locked,dict): err('locked contract must be an object')
        else:
            for path in ('run_id','tier','provider','purchase_model','gpu','image','dataset','checkpoint','monitoring'):
                if d.get(path) != locked.get(path): err(f'locked field changed: {path}')
            old_recovery=locked.get('recovery',{})
            if not isinstance(old_recovery,dict): err('locked recovery must be object')
            else:
                if not old_recovery.get('allow_region_change',False):
                    if d.get('region')!=locked.get('region'): err('locked region changed')
                elif d.get('region') not in (old_recovery.get('allowed_regions') or []):
                    err('region outside locked allowed_regions')
                # Expanded permission or budget is a new review, not silent recovery.
                if d.get('recovery')!=old_recovery: err('locked recovery policy changed; explicit review required')
            if d.get('budget')!=locked.get('budget'): err('locked budget changed; explicit review required')
            old_ctl=locked.get('controller') if isinstance(locked.get('controller'),dict) else {}
            if old_ctl.get('server_version') is not None and ctl.get('server_version')!=old_ctl['server_version']:
                err('controller.server_version changed: rerun the start checks (requalify only if the release notes touch recovery)')
    return errors

def warnings(d, now=None):
    """Advisory only: spend the run's limits cannot see. The worker enforces the budget, the deadline and the
    allocation limits itself; without the optional guard nothing counts waiting after a preemption until the
    next start."""
    out=[]
    b=d.get('budget') if isinstance(d.get('budget'),dict) else {}
    mon=d.get('monitoring') if isinstance(d.get('monitoring'),dict) else {}
    if d.get('tier')!='minimal' and mon.get('guard','none')=='none' and b.get('free_without_vm') is not True:
        out.append('budget.free_without_vm is not true: waiting for capacity after a preemption may be billed, and without '
                   'the guard it is counted only at the next start; set monitoring.guard to external to bound it, or '
                   'confirm from the provider research that only running VMs bill')
    return out

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('contract',type=Path)
    p.add_argument('--phase',choices=['plan','launch'],default='plan')
    p.add_argument('--locked',type=Path)
    args=p.parse_args()
    try:
        obj=json.loads(args.contract.read_text())
        old=json.loads(args.locked.read_text()) if args.locked else None
        errors=validate(obj,args.phase,old)
    except (OSError,ValueError,TypeError) as e:
        errors=[str(e)]
    try:
        notes=warnings(obj) if not errors else []
    except Exception:  # noqa: BLE001 - advisory only
        notes=[]
    print(json.dumps({'valid':not errors,'errors':errors,'warnings':notes,'authorization':'not_evaluated','cloud_checks':'not_run'},indent=2))
    return 1 if errors else 0

if __name__=='__main__': sys.exit(main())
