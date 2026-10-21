#!/usr/bin/env python3
"""Reference campaign guard: enforces a spend ceiling, attempts, capacity wait and an optional deadline.

SkyPilot Managed Jobs will wait for spot capacity indefinitely and has no campaign budget. Since v0.10 the
worker enforces the run's limits itself (checkpoint_marker.py limits); this guard is optional and, with
worker_limits (set by --init-config when the contract's monitoring.guard is external), leaves the deadline and
the attempt limits to the worker and adds what no worker can see: waiting for capacity after a preemption, the
spend of billed waits, a timed on-demand fallback, a wedged VM and notices while no VM exists.
This guard runs OUTSIDE the GPU worker (cron/systemd timer or --loop) on an always-on host,
persists its ledger, and cancels the managed job (then notifies) when an approved limit is hit.
It watches managed jobs only; a plain `sky launch` cluster (Minimal tier) is bounded by its own
runtime timeout and autodown (and the worker deadline, when one is set) instead.

  python3 campaign_guard.py --init-config --contract experiment.json --job-id N --workdir ~/campaign/<run_id> \
          (--notify-cmd '<json array>' | --attended) [--fallback-task /abs/od.yaml --secrets-cmd '<json array>'] \
          [--dry-run]                                        # write guard.json for a submitted job
  python3 campaign_guard.py --close-job --workdir ~/campaign/<run_id> --job-id N [--reason TEXT]  # close a ledger
  python3 campaign_guard.py --config guard.json --once      # one tick (cron/systemd timer)
  python3 campaign_guard.py --config guard.json --loop      # poll every poll_seconds
  python3 campaign_guard.py --self-test                      # decision logic only, no SkyPilot
  python3 campaign_guard.py --check-sdk                      # installed SkyPilot exposes what the guard calls

The guard host must use the submitter's SkyPilot API server and a login allowed to cancel the job;
it looks the job up by id across users.

Run --self-test and --check-sdk once when installing the guard and after any SkyPilot upgrade
(the lifecycle start checks); only observe_skypilot() and run_action() touch the SkyPilot SDK.
If the SDK breaks at runtime, or the job is missing from the queue, observation becomes "blind"
(never "fine"): the guard never cancels or falls back blind, keeps charging the ceiling and
notifies the user; the worker's runtime timeout (the budget runway at the ceiling, per allocation)
is the last defence.

--init-config derives guard.json from experiment.json and the campaign ledger <workdir>/campaign.json
(earlier jobs of the same run carry their spend bound and attempts over; planned drills add attempts)
and registers the job there; --dry-run prints the config and the guard runway without writing.
guard.json (no secrets; unknown keys are rejected):
  {"job_id": 7, "deadline_utc": "2026-10-03T12:00:00Z" (optional), "max_attempts": 4, "max_attempts_without_progress": 10,
   "free_without_vm": true, "vm_count_cmd": ["bash", "/abs/project/vm-ids.sh"],
   "max_capacity_wait_minutes": 30, "startup_allowance_minutes": 10,
   "budget_usd": 8.0, "ceiling_hourly_usd": 4.57, "reserved_usd": 0.5,
   "spent_before_usd": 0.0, "attempts_before": 0, "poll_seconds": 60,
   "shutdown_reserve_minutes": 5, "graceful_cancel_seconds": 120,
   "state_path": "/var/lib/campaign-guard/run-7.json", "max_blind_minutes": 15,
   "notify_cmd": ["/usr/local/bin/notify-me"], "notify_milestones": true,
   "fallback": {"approved": true, "after_capacity_wait_minutes": 10,
                "task_yaml": "/etc/campaign-guard/training.on-demand.yaml",
                "secrets_cmd": ["bash", "/abs/project/secrets-env.sh"]}}

ceiling_hourly_usd is the node's ON-DEMAND list price: a price-following spot VM may bill up to it.
Spend is an upper bound: ceiling x every second the job is non-terminal, from its submission
(SkyPilot's submitted_at), waiting and blind time included, plus reserved_usd (controller, storage)
and spent_before_usd (upper bounds of earlier jobs of the same campaign), so the guard stops early
rather than late. It is not a billing report: every notification carries it, labelled as an upper bound at
the on-demand price; the real spot cost comes from the provider's bill.
Attempts = allocations: attempts_before + 1 + SkyPilot recovery_count, plus one for the fallback job;
max_attempts (optional) caps them in absolute terms. max_attempts_without_progress (default 10) stops a
run whose allocations keep ending without a new checkpoint: the guard reads the job log for the
CHECKPOINT_COMMITTED line while an allocation runs, so a long run with many preemptions but steady
progress is never stopped by a count (an unreadable log never counts; a commit in the last poll interval
before a preemption can be missed, which counts that allocation as stalled).
Billing rules: with free_without_vm (the provider bills compute only while a VM exists), PENDING time is
free, and STARTING/RECOVERING time is free while vm_count_cmd (the project's hook, given the cluster
name) reports no VM; once per allocation the hook must also find the running job's VM, or (a wrong hook or
cluster name) nothing counts as free from then on and the user is told once; anything unknown is charged, and so is any time two jobs may be alive after a
fallback. A provisioning attempt that creates and deletes a VM between two polls is not seen (seconds).
Capacity wait = continuous observed PENDING/SUBMITTED/STARTING/RECOVERING time beyond
startup_allowance_minutes (normal provisioning, image pull, mounts and data copy, measured once per
stack): a slow but healthy start is not "no capacity". The approved on-demand fallback fires after
fallback.after_capacity_wait_minutes (< max_capacity_wait_minutes) of such wait, then the fallback
job is watched under the same limits. The fallback is launched first (its secrets re-read at that
moment from fallback.secrets_cmd, which prints NAME=value lines), and only a launched fallback cancels
the waiting primary; a failed fallback launch keeps the primary and is retried on later ticks (three
tries; a launch accepted but not confirmed is adopted, never repeated; a fallback that ends at once
returns the guard to the primary). Every SkyPilot call has a deadline (sdk_timeout_seconds; a silent
API server reads as blind). Cancels are graceful first (cached-mount uploads finish, up to graceful_cancel_seconds), then
plain. One ledger per job: a state_path that belongs to another job is refused; one tick at a time
(file lock). Each tick prints one observation line; status changes are kept in the ledger's history
and, with notify_milestones, notified (running, recovering, running again). With notify_evals (default
true) the guard reads the job log on every RUNNING tick (or at most every log_read_seconds, when set for a
small or shared control host) and forwards the newest `EVAL step=<n> {...}` line the entrypoint printed
(each step once, so a recovery's replay is not sent again). The same read
watches the worker's stall watchdog (progress_watch.py): a new `STALL` line is notified when a tick reads
it before the job ends (exit 93; the FAILED status is the sure signal); RUNNING with a readable log whose
newest `PROGRESS` line is older than 3 x progress_period_seconds (300) means the VM or the watchdog is
wedged: notified, and cancelled after wedged_grace_minutes (15). A `PROGRESS` line pushed out of the read
window, or a log that cannot be read (only notified), is no evidence of a wedge.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

TERMINAL = {'SUCCEEDED', 'FAILED', 'FAILED_SETUP', 'FAILED_PRECHECKS', 'FAILED_NO_RESOURCE',
            'FAILED_CONTROLLER', 'CANCELLED'}
WAITING = {'PENDING', 'SUBMITTED', 'STARTING', 'RECOVERING'}
REQUIRED = ('job_id', 'max_capacity_wait_minutes', 'budget_usd', 'ceiling_hourly_usd', 'state_path')
OPTIONAL = ('deadline_utc', 'max_attempts', 'reserved_usd', 'poll_seconds', 'shutdown_reserve_minutes', 'max_blind_minutes', 'notify_cmd',
            'fallback', 'startup_allowance_minutes', 'spent_before_usd', 'attempts_before',
            'graceful_cancel_seconds', 'notify_milestones', 'sdk_timeout_seconds',
            'max_attempts_without_progress', 'free_without_vm', 'vm_count_cmd', 'notify_evals',
            'progress_period_seconds', 'wedged_grace_minutes', 'log_read_seconds', 'worker_limits')
HISTORY_MAX = 200
FALLBACK_TRIES = 3
SDK_TIMEOUT_SECONDS = 120
DEFAULT_ON_DEMAND_WAIT_MINUTES = 30
DEFAULT_ATTEMPTS_WITHOUT_PROGRESS = 10
NO_VM_STATUSES = {'PENDING', 'SUBMITTED'}      # a managed job holds no VM before STARTING
COMMIT_LINE = 'CHECKPOINT_COMMITTED'
LOG_TAIL_LINES = 4000          # lines per log read (minutes of log; progress bars are off)
EVAL_LINE = 'EVAL step='       # the entrypoint's evaluation line: EVAL step=<n> {metrics json}
EVAL_STEP = re.compile(r'EVAL step=(\d+)\b')
WATCH_LINE = r'(PROGRESS|STALL) \d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ .*'    # progress_watch.py's lines
UNREADABLE_NOTICE = 5          # failed log reads in a row before the user is told


def parse_utc(text: str) -> float:
    if not isinstance(text, str) or not text.endswith('Z'):
        raise ValueError('deadline_utc must be an ISO-8601 UTC string ending in Z')
    return datetime.fromisoformat(text[:-1]).replace(tzinfo=timezone.utc).timestamp()


def _reserve_hours(cfg) -> float:
    return (cfg.get('poll_seconds', 60) + 60 * cfg.get('shutdown_reserve_minutes', 5)) / 3600


def _base_usd(cfg) -> float:
    return cfg.get('reserved_usd', 0.0) + cfg.get('spent_before_usd', 0.0)


def _number(v) -> bool:
    return not isinstance(v, bool) and isinstance(v, (int, float)) and math.isfinite(v)


def check_config(cfg: dict) -> dict:
    missing = [k for k in REQUIRED if k not in cfg]
    if missing:
        raise ValueError(f'guard config missing {missing}')
    unknown = sorted(set(cfg) - set(REQUIRED) - set(OPTIONAL))
    if unknown:
        raise ValueError(f'unknown guard config keys {unknown}')
    for k in ('budget_usd', 'ceiling_hourly_usd', 'max_capacity_wait_minutes'):
        if not _number(cfg[k]) or cfg[k] <= 0:
            raise ValueError(f'{k} must be a positive number')
    for k in ('reserved_usd', 'spent_before_usd', 'startup_allowance_minutes'):
        if k in cfg and (not _number(cfg[k]) or cfg[k] < 0):
            raise ValueError(f'{k} must be a nonnegative number')
    for k in ('attempts_before', 'graceful_cancel_seconds'):
        if k in cfg and (type(cfg[k]) is not int or cfg[k] < 0):
            raise ValueError(f'{k} must be a nonnegative integer')
    if 'sdk_timeout_seconds' in cfg and (type(cfg['sdk_timeout_seconds']) is not int or cfg['sdk_timeout_seconds'] < 1):
        raise ValueError('sdk_timeout_seconds must be a positive integer')
    for k in ('notify_milestones', 'notify_evals'):
        if k in cfg and type(cfg[k]) is not bool:
            raise ValueError(f'{k} must be true or false')
    for k in ('progress_period_seconds', 'wedged_grace_minutes', 'log_read_seconds'):
        if k in cfg and (not _number(cfg[k]) or cfg[k] <= 0):
            raise ValueError(f'{k} must be a positive number')
    if cfg.get('max_attempts') is not None and (type(cfg['max_attempts']) is not int or cfg['max_attempts'] < 1):
        raise ValueError('max_attempts must be a positive integer or null (no absolute cap)')
    stalled = cfg.get('max_attempts_without_progress', DEFAULT_ATTEMPTS_WITHOUT_PROGRESS)
    if type(stalled) is not int or stalled < 1:
        raise ValueError('max_attempts_without_progress must be a positive integer')
    if 'free_without_vm' in cfg and type(cfg['free_without_vm']) is not bool:
        raise ValueError('free_without_vm must be true or false')
    if 'vm_count_cmd' in cfg and not (isinstance(cfg['vm_count_cmd'], list) and cfg['vm_count_cmd']
                                      and all(isinstance(a, str) for a in cfg['vm_count_cmd'])):
        raise ValueError('vm_count_cmd must be a nonempty argv list (the cluster name is appended)')
    if cfg.get('max_attempts') is not None and cfg.get('attempts_before', 0) + 1 > cfg['max_attempts']:
        raise ValueError(f"earlier jobs already used {cfg.get('attempts_before', 0)} of {cfg['max_attempts']} attempts: "
                         'a new job would be cancelled at once; a relaunch needs a new approval and a higher limit')
    if cfg.get('graceful_cancel_seconds', 120) > 60 * cfg.get('shutdown_reserve_minutes', 5):
        raise ValueError('graceful_cancel_seconds must fit inside shutdown_reserve_minutes')
    if _base_usd(cfg) + cfg['ceiling_hourly_usd'] * _reserve_hours(cfg) >= cfg['budget_usd']:
        raise ValueError('reserved_usd + spent_before_usd + shutdown reserve already reach budget_usd')
    fb = cfg.get('fallback')
    if fb is not None:
        if not isinstance(fb, dict) or set(fb) - {'approved', 'after_capacity_wait_minutes', 'task_yaml', 'secrets_cmd'}:
            raise ValueError('fallback accepts approved, after_capacity_wait_minutes, task_yaml and secrets_cmd only')
        if 'secrets_cmd' in fb and not (isinstance(fb['secrets_cmd'], list) and fb['secrets_cmd']
                                        and all(isinstance(a, str) for a in fb['secrets_cmd'])):
            raise ValueError('fallback.secrets_cmd must be a nonempty argv list (it prints NAME=value lines)')
        if not (isinstance(fb, dict) and fb.get('approved') is True and isinstance(fb.get('task_yaml'), str)
                and not isinstance(fb.get('after_capacity_wait_minutes'), bool)
                and isinstance(fb.get('after_capacity_wait_minutes'), (int, float))
                and fb['after_capacity_wait_minutes'] > 0):
            raise ValueError('fallback needs approved=true, task_yaml and positive after_capacity_wait_minutes')
        if not (os.path.isabs(fb['task_yaml']) and os.path.isfile(fb['task_yaml'])):
            raise ValueError('fallback.task_yaml must be an absolute path to an existing rendered task on the guard host')
        after = fb['after_capacity_wait_minutes']
        if after >= cfg['max_capacity_wait_minutes']:
            raise ValueError('fallback.after_capacity_wait_minutes must be less than max_capacity_wait_minutes')
        ceiling = cfg['ceiling_hourly_usd']
        waited = cfg.get('startup_allowance_minutes', 0) + after
        reach = _base_usd(cfg) + ceiling * (waited / 60 + _reserve_hours(cfg))
        if reach >= cfg['budget_usd'] and not cfg.get('free_without_vm'):   # free waiting may still reach it
            raise ValueError(f'fallback could never fire: waiting {waited} min is charged {reach:.2f}, '
                             f'reaching budget {cfg["budget_usd"]} first')
    if cfg.get('deadline_utc') is not None:          # none by default: the budget is the hard stop
        parse_utc(cfg['deadline_utc'])
    return cfg


def _milestone(prev: str | None, status: str, seen_running: bool, attempts: int) -> str | None:
    if status == 'RUNNING' and prev != 'RUNNING':
        return f'running again after recovery (attempt {attempts})' if seen_running else f'running (attempt {attempts})'
    if status == 'RECOVERING' and prev != 'RECOVERING':
        return f'recovering: allocation lost, reacquiring capacity (attempt {attempts} so far)'
    return None


def _remember(s: dict, now: float, status: str | None, obs: dict | None, cfg: dict):
    """Keep a compact history of status changes (and blind stretches) in the ledger."""
    entry = {'status': status or 'BLIND', 'recovery_count': None if obs is None else int(obs.get('recovery_count') or 0)}
    last = s['history'][-1] if s['history'] else None
    if last is None or (last['status'], last['recovery_count']) != (entry['status'], entry['recovery_count']):
        spend = _base_usd(cfg) + cfg['ceiling_hourly_usd'] * s['billable_seconds'] / 3600
        s['history'] = (s['history'] + [{'time': round(now, 1), **entry, 'spend_upper_usd': round(spend, 4)}])[-HISTORY_MAX:]
    if status is not None:
        s['last_status'] = status


def _free(cfg, obs, s=None) -> bool:
    """No compute is billed for this observation under the provider's billing rules (unknown = billed). Once
    the VM hook found no VM for a running job (a wrong hook or cluster name), nothing is free any more."""
    if not cfg.get('free_without_vm') or obs is None or (s or {}).get('vm_hook_broken'):
        return False
    return obs['status'] in NO_VM_STATUSES or (obs['status'] in WAITING and obs.get('vms') == 0)


def _track_progress(s: dict, obs: dict):
    """Allocations that ended without a new checkpoint, in a row. An allocation ends at RECOVERING or
    when the recovery count grows; it made progress if its log showed a commit line. It counts as
    stalled only when a log read succeeded and showed none: an unreadable log never counts."""
    rc = int(obs.get('recovery_count') or 0)
    ended = (rc > s.get('alloc_rc', 0)) or (obs['status'] == 'RECOVERING' and s.get('alloc_running'))
    if ended:
        if s.get('alloc_committed'):
            s['stalled'] = 0
        elif s.get('alloc_running') and s.get('alloc_read_ok'):
            s['stalled'] = s.get('stalled', 0) + 1
        s.update(alloc_rc=rc, alloc_running=False, alloc_committed=False, alloc_read_ok=False)
    if obs['status'] == 'RUNNING':
        s['alloc_running'] = True
        if obs.get('committed') is not None:
            s['alloc_read_ok'] = True
            s['log_unreadable'] = 0
        elif 'committed' in obs:
            s['log_unreadable'] = s.get('log_unreadable', 0) + 1
        if obs.get('committed'):
            s['alloc_committed'] = True


def _wedged(cfg, s, obs, now) -> str | None:
    """RUNNING while a successful log read shows the worker's newest PROGRESS line older than 3 periods: the
    VM or its watchdog is frozen. Only such a read is evidence: no PROGRESS line (no watchdog, or the line
    pushed out of the window) is none, and a failed read is the log channel's problem as much as the VM's
    (the unreadable-log notice covers it; never a cancel). A new allocation starts afresh."""
    if obs.get('log_read') == 'skipped':                 # no read this tick: no evidence either way
        return None
    line = obs.get('progress') if obs['status'] == 'RUNNING' else None
    if not line:
        if 'committed' not in obs or obs.get('committed') is not None or obs['status'] != 'RUNNING':
            s['wedged_since'] = None                     # a failed read keeps the state as it was
        return None
    s['progress_at'], s['progress_line'] = parse_utc(line.split()[1]), line[:120]
    age = now - s['progress_at']
    if age <= 3 * cfg.get('progress_period_seconds', 300):
        s['wedged_since'] = None
        s['notified'] = [n for n in s['notified'] if n != 'wedged']
        return None
    s['wedged_since'] = s.get('wedged_since') or now
    return f"worker wedged: RUNNING but its last PROGRESS line is {age / 60:.0f} min old ({s['progress_line']})"


def decide(cfg: dict, state: dict, obs: dict | None, now: float) -> tuple[list, dict]:
    """Pure decision. obs = {'status', 'recovery_count'[, 'submitted_at']} or None when the job cannot
    be observed. Returns (actions, new_state); actions are ('cancel'|'fallback'|'notify', reason)."""
    s = {'billable_seconds': 0.0, 'wait_started': None, 'last_tick': None, 'first_tick': None, 'blind_since': None,
         'done': False, 'stopping': None, 'fallback_used': False, 'attempts_base': 0, 'notified': [],
         'last_status': None, 'seen_running': False, 'observed': False, 'history': [], **state}
    actions = []
    if s['done']:
        return actions, s
    elapsed = 0.0 if s['last_tick'] is None else max(0.0, now - s['last_tick'])
    if s['first_tick'] is None:
        s['first_tick'] = now
    submitted = (obs or {}).get('submitted_at')
    first_obs = obs is not None and not s['observed']
    two_jobs = s.get('replaced_job') is not None or s.get('double_charge_once', False)
    if obs is not None and obs.get('vms_running') == 0 and not s.get('vm_hook_broken'):
        s['vm_hook_broken'] = True                     # a running job has a VM: the hook or the name is wrong
        actions.append(('notify', 'the VM hook found no VM for the running job: waiting is charged from now on; '
                                  'check the hook and the cluster name'))
    free_now = _free(cfg, obs, s) and not two_jobs     # a replaced primary may hold a VM: never free then
    if first_obs:                                      # charge from submission, not from the guard's start,
        s['observed'] = True                           # even when the first ticks were blind; skip only when
        never_ran = (obs['status'] in NO_VM_STATUSES and not obs.get('recovery_count'))   # no VM ever existed
        if _number(submitted) and 0 < submitted < s['first_tick'] and not (cfg.get('free_without_vm') and never_ran):
            elapsed += s['first_tick'] - submitted
    if free_now and s.get('prev_free'):                # free only when both ends of the interval were free
        s['free_seconds'] = s.get('free_seconds', 0.0) + elapsed
        elapsed = 0.0
    s['prev_free'] = free_now
    s['last_tick'] = now
    if s.get('replaced_job') is not None or s.pop('double_charge_once', False):
        elapsed *= 2                  # until the replaced primary is cancelled, two jobs may hold VMs: charge both
    status = None if obs is None else obs['status']
    if obs is None:                                    # blind: keep charging the conservative clock
        if s['blind_since'] is None:
            s['blind_since'] = now
    else:
        s['blind_since'] = None
        s['notified'] = [n for n in s['notified'] if not n.startswith('blind')]
        s['attempts'] = (cfg.get('attempts_before', 0) + s['attempts_base'] + 1
                         + int(obs.get('recovery_count') or 0))
        if status in TERMINAL:
            s['done'] = True
            s['billable_seconds'] += elapsed
            s['spend_upper_usd'] = round(_base_usd(cfg) + cfg['ceiling_hourly_usd'] * s['billable_seconds'] / 3600, 4)
            _remember(s, now, status, obs, cfg)
            return [('notify', f'job terminal: {status}')], s
        if status in WAITING:
            if s['wait_started'] is None:
                started = submitted if first_obs and status != 'RECOVERING' and _number(submitted) else None
                s['wait_started'] = started if started and 0 < started < now else now
        else:
            s['wait_started'] = None
        _track_progress(s, obs)
        note = _milestone(s['last_status'], status, s['seen_running'], s['attempts'])
        if s.get('log_unreadable', 0) >= UNREADABLE_NOTICE and 'log-unreadable' not in s['notified']:
            s['notified'].append('log-unreadable')
            actions.append(('notify', 'the job log cannot be read: the limit on attempts without progress is inactive'))
        if note and cfg.get('notify_milestones', True):
            actions.append(('notify', note))
        if cfg.get('notify_evals', True):
            steps = [(int(m[1]), line) for line in obs.get('evals') or [] if (m := EVAL_STEP.match(line))]
            if steps and max(steps)[0] > s.get('eval_step', -1):        # the newest, each step once
                s['eval_step'], line = max(steps)
                actions.append(('notify', 'evaluation: ' + line[:400]))
        stall = obs.get('stall')
        if stall and stall != s.get('stall_seen'):
            s['stall_seen'] = stall
            actions.append(('notify', f'{stall[:300]}: the worker stopped a hung run (exit 93); read the STALL line '
                                      'and the stacks under it in the job log'))
        wedged = _wedged(cfg, s, obs, now)
        if wedged and 'wedged' not in s['notified']:
            s['notified'].append('wedged')
            actions.append(('notify', f'{wedged}; cancelling in {cfg.get("wedged_grace_minutes", 15)} min unless it recovers'))
        if status == 'RUNNING':
            s['seen_running'] = True
    s['billable_seconds'] += elapsed
    _remember(s, now, status, obs, cfg)

    hourly = cfg['ceiling_hourly_usd']
    spend = _base_usd(cfg) + hourly * s['billable_seconds'] / 3600
    s['spend_upper_usd'] = round(spend, 4)
    reasons = []
    worker = cfg.get('worker_limits', False)           # the worker enforces deadline and attempts at every start
    if not worker and cfg.get('deadline_utc') is not None and now >= parse_utc(cfg['deadline_utc']):
        reasons.append('campaign deadline reached')
    if spend + hourly * _reserve_hours(cfg) >= cfg['budget_usd']:
        reasons.append(f'spend upper bound {spend:.2f} + shutdown reserve reaches budget {cfg["budget_usd"]}')
    if not worker and cfg.get('max_attempts') is not None and s.get('attempts', 1) > cfg['max_attempts']:
        reasons.append(f"attempts {s['attempts']} exceed {cfg['max_attempts']}")
    if s.get('wedged_since') is not None and now - s['wedged_since'] >= 60 * cfg.get('wedged_grace_minutes', 15):
        reasons.append(f"worker wedged: no PROGRESS line for {(now - s['progress_at']) / 60:.0f} min")
    stalled_max = cfg.get('max_attempts_without_progress', DEFAULT_ATTEMPTS_WITHOUT_PROGRESS)
    if not worker and s.get('stalled', 0) >= stalled_max:
        reasons.append(f"{s['stalled']} allocations in a row ended without a new checkpoint (limit {stalled_max})")
    waited = 0.0 if s['wait_started'] is None else (now - s['wait_started']) / 60
    wait_min = max(0.0, waited - cfg.get('startup_allowance_minutes', 0))    # beyond a normal start
    s['wait_minutes'] = round(waited, 2)
    if wait_min >= cfg['max_capacity_wait_minutes']:    # past the cap (e.g. after a blind stretch): no fallback
        reasons.append(f'capacity wait {wait_min:.0f} min exceeds {cfg["max_capacity_wait_minutes"]}')
    fb = cfg.get('fallback')
    if (fb and not s['fallback_used'] and not reasons and status in WAITING
            and wait_min >= fb['after_capacity_wait_minutes']):
        if cfg.get('max_attempts') is not None and s.get('attempts', 1) >= cfg['max_attempts']:
            reasons.append(f'capacity wait {wait_min:.0f} min; fallback would exceed max_attempts {cfg["max_attempts"]}')
        else:
            # Committed here, undone by tick() if the launch fails. attempts_base excludes attempts_before,
            # which the attempts formula adds once.
            s['fallback_prev'] = {'attempts_base': s['attempts_base'], 'wait_started': s['wait_started']}
            s['fallback_used'] = True
            s['attempts_base'] = s.get('attempts', 1) - cfg.get('attempts_before', 0)
            s['wait_started'] = None
            return [('fallback', f'capacity wait {wait_min:.0f} min; approved on-demand fallback'),
                    ('notify', 'switched to approved on-demand fallback')], s
    if not reasons:
        if s['blind_since'] is not None and (now - s['blind_since']) / 60 >= cfg.get('max_blind_minutes', 15) \
                and 'blind' not in s['notified']:
            s['notified'].append('blind')
            actions.append(('notify', 'guard cannot observe the job; limits still being tracked'))
        return actions, s
    actions = [a for a in actions if not (a[0] == 'notify' and a[1].startswith(('running', 'recovering')))]  # a limit outranks milestones
    reason = '; '.join(reasons)
    if obs is None:                                    # never cancel or fall back blind
        if 'blind-limit' not in s['notified']:
            s['notified'].append('blind-limit')
            actions.append(('notify', f'limit due but the job cannot be observed, stop it by hand: {reason}'))
        return actions, s
    actions.append(('cancel', reason))
    if s['stopping'] is None:                          # re-issue cancel each tick, notify once
        s['stopping'] = now
        actions.append(('notify', f'cancelling job: {reason}'))
    return actions, s


# ---- side effects (SkyPilot SDK adapter; --check-sdk verifies it in the start checks) ---------

_STUCK = []        # (thread, started) of SDK calls that outlived their deadline and are still blocked


def stuck_minutes() -> float:
    """Age of the oldest SDK call still blocked. The SDK reads without a timeout, so a connection a
    NAT or VPN dropped silently can block forever: --loop then exits for a clean restart."""
    _STUCK[:] = [(t, start) for t, start in _STUCK if t.is_alive()]
    return max(((time.time() - start) / 60 for _, start in _STUCK), default=0.0)


def _get(make_request, timeout=SDK_TIMEOUT_SECONDS):
    """sky.stream_and_get(make_request()) under a deadline (see _call)."""
    import sky
    return _call(lambda: sky.stream_and_get(make_request()), timeout)


def _call(fn, timeout=SDK_TIMEOUT_SECONDS):
    """fn() under a deadline: a connected but silent API server must not stall the guard (the caller
    treats a timeout as blind or as a failed action). While an earlier call is still blocked, no new one
    starts, so a silent server cannot pile up threads under --loop."""
    if stuck_minutes() > 0:
        raise TimeoutError('an earlier SkyPilot API call is still blocked')
    box = {}

    def run():
        try:
            box['value'] = fn()
        except BaseException as e:  # noqa: BLE001 - re-raised in the caller
            box['error'] = e
    worker = threading.Thread(target=run, daemon=True)
    started = time.time()
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        _STUCK.append((worker, started))
        raise TimeoutError(f'SkyPilot API call did not finish within {timeout} s')
    if 'error' in box:
        raise box['error']
    return box['value']


def _records(result) -> list:
    records = result[0] if isinstance(result, tuple) else result
    return [r if isinstance(r, dict) else getattr(r, 'model_dump', lambda: vars(r))() for r in records]


def _status(rec) -> str:
    return getattr(rec['status'], 'value', str(rec['status'])).split('.')[-1]


def observe_skypilot(job_id: int, timeout=SDK_TIMEOUT_SECONDS):
    try:
        from sky import jobs
        # all_users: the queue is otherwise scoped to the caller's SkyPilot user, and the guard
        # usually runs on another host than the submitter; the job id is the filter.
        result = _get(lambda: jobs.queue_v2(refresh=False, all_users=True, job_ids=[job_id],
                                            fields=['job_id', 'job_name', 'status', 'recovery_count',
                                                    'submitted_at', 'current_cluster_name']), timeout)
        for rec in _records(result):
            if int(rec['job_id']) == int(job_id):
                return {'status': _status(rec), 'recovery_count': rec.get('recovery_count', 0),
                        'submitted_at': rec.get('submitted_at'), 'cluster': rec.get('current_cluster_name'),
                        'name': rec.get('job_name')}
        # A job missing from the queue is unknown, never finished: guarding must not end silently.
        print(json.dumps({'guard': 'job_not_listed', 'job_id': job_id}), file=sys.stderr)
        return None
    except Exception as e:  # noqa: BLE001 - unreachable controller means "blind", never "fine"
        print(json.dumps({'guard': 'observe_failed', 'error': f'{type(e).__name__}: {e}'}), file=sys.stderr)
        return None


LOG_READ = ('import io, json, sys\nfrom sky import jobs\nb = io.StringIO()\n'
            'jobs.tail_logs(job_id=int(sys.argv[1]), follow=False, tail=int(sys.argv[2]), output_stream=b)\n'
            'text = b.getvalue()\n'
            'evals = [ln[ln.index(sys.argv[4]):].strip() for ln in text.splitlines() if sys.argv[4] in ln]\n'
            'import re\nwatch = [m.group(0).strip() for m in re.finditer(sys.argv[5], text)]\n'
            'newest = lambda tag: next((w for w in reversed(watch) if w.startswith(tag)), None)\n'
            'print("GUARD-LOG-READ", json.dumps({"committed": sys.argv[3] in text, "evals": evals[-5:],'
            ' "progress": newest("PROGRESS"), "stall": newest("STALL")}))\n')


def _log_cmd(job_id: int) -> list:
    return [sys.executable, '-c', LOG_READ, str(int(job_id)), str(LOG_TAIL_LINES), COMMIT_LINE, EVAL_LINE, WATCH_LINE]


def read_log(job_id: int, timeout=SDK_TIMEOUT_SECONDS):
    """{'committed': the current allocation's log shows a CHECKPOINT_COMMITTED line, 'evals': its last EVAL
    lines, 'progress'/'stall': the newest watchdog lines or None}; None if unreadable. Runs in a child process killed at the deadline: a log read can block (a VM
    that died before the controller noticed), and a killed child closes the stream instead of leaving a
    blocked call behind."""
    try:
        out = subprocess.run(_log_cmd(job_id), capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        print(json.dumps({'guard': 'log_read_failed', 'error': type(e).__name__}), file=sys.stderr)
        return None
    # The SDK may print its own messages to stdout: only the tagged answer line counts.
    answers = [ln.split(' ', 1)[1] for ln in out.stdout.splitlines() if ln.startswith('GUARD-LOG-READ ')]
    try:
        log = json.loads(answers[-1]) if out.returncode == 0 and answers else None
    except ValueError:
        return None
    return log if isinstance(log, dict) and isinstance(log.get('committed'), bool) else None


def cluster_name(obs, job_id) -> str | None:
    """The job's cluster for the VM hook: SkyPilot's current_cluster_name, or, when the queue leaves it empty
    (consolidated jobs control), the managed-job cluster name SkyPilot derives VM names from (<job name>-<job
    id>, the job name shortened and hashed by SkyPilot when long); None when neither is known (then waiting
    is charged, never counted as free)."""
    if obs.get('cluster'):
        return obs['cluster']
    if not obs.get('name'):
        return None
    try:
        from sky.jobs.utils import generate_managed_job_cluster_name
    except ImportError:
        return f"{obs['name']}-{int(job_id)}"
    return generate_managed_job_cluster_name(obs['name'], int(job_id))


def count_vms(cmd, cluster):
    """VMs of the cluster according to the project's hook (it prints one VM id per line); None if unknown."""
    if not cluster:
        return None
    try:
        out = subprocess.run([*cmd, cluster], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return len([ln for ln in out.stdout.splitlines() if ln.strip()]) if out.returncode == 0 else None


def find_launched(name: str, after_job: int, timeout=SDK_TIMEOUT_SECONDS):
    """A non-terminal job of this name with a higher id than the primary (managed job ids only grow; the
    name carries the run id): a launch the API server accepted although the guard saw an error, or died
    before recording it. Adopt it, never relaunch. (submitted_at stays empty while a job is PENDING.)"""
    from sky import jobs
    # The guard launched the fallback with its own login: only its own jobs are candidates (on a shared
    # server another user's job can carry the same name).
    result = _get(lambda: jobs.queue_v2(refresh=False, all_users=False, job_ids=None,
                                        fields=['job_id', 'job_name', 'status', 'submitted_at']), timeout)
    found = [int(r['job_id']) for r in _records(result)
             if r.get('job_name') == name and _status(r) not in TERMINAL and int(r['job_id']) > int(after_job)]
    return max(found) if found else None


def _launched_job_id(result) -> int:
    ids = result[0] if isinstance(result, tuple) else result
    if isinstance(ids, list) and len(ids) == 1:
        ids = ids[0]
    if isinstance(ids, bool) or not isinstance(ids, int):
        raise RuntimeError(f'fallback launched but its job id is unknown ({result!r}); the guard cannot watch it')
    return ids


def run_action(cfg, action, reason, state):
    """Perform one action. Returns the new job id for 'fallback', else None."""
    job_id = state.get('job_id', cfg['job_id'])
    timeout = cfg.get('sdk_timeout_seconds', SDK_TIMEOUT_SECONDS)
    if action == 'cancel':
        from sky import jobs
        grace = cfg.get('graceful_cancel_seconds', 120)
        if grace and not state.get('cancel_issued'):   # first request lets cached-mount uploads finish
            _get(lambda: jobs.cancel(job_ids=[int(job_id)], graceful=True, graceful_timeout=grace), timeout + grace)
        else:
            _get(lambda: jobs.cancel(job_ids=[int(job_id)]), timeout)
    elif action == 'fallback':
        import sky
        from sky import jobs
        fb = cfg['fallback']
        task = sky.Task.from_yaml(fb['task_yaml'])
        new_job = find_launched(task.name, job_id, timeout)
        if new_job is None:
            names = [n for n in (task.envs or {}).get('SECRET_NAMES', '').split(',') if n]
            values = read_secrets(fb['secrets_cmd']) if fb.get('secrets_cmd') else {}
            missing = [n for n in names if not values.get(n)]
            if missing:
                raise RuntimeError(f'fallback secrets missing: {missing} (fallback.secrets_cmd)')
            if values:
                task.update_secrets({n: values[n] for n in names})
            new_job = _launched_job_id(_get(lambda: jobs.launch(task), timeout))
        # The waiting primary is cancelled only once the fallback is observed alive (next tick).
        state['replaced_job'] = int(job_id)
        return new_job
    elif action == 'notify' and cfg.get('notify_cmd'):
        spend = state.get('spend_upper_usd')
        if spend is not None:
            reason += f'; spend <= {spend:.2f} USD (upper bound at the on-demand price; the provider bill has the real cost)'
        payload = json.dumps({'job_id': job_id, 'message': reason, 'time': time.time(), 'spend_upper_usd': spend})
        subprocess.run(cfg['notify_cmd'], input=payload, text=True, timeout=60)
    return None


def read_secrets(cmd) -> dict:
    """Run the project's secrets command (it prints NAME=value lines) and parse it. Values are never
    printed or stored; a short-lived registry token is fresh at fallback time."""
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if out.returncode != 0:
        raise RuntimeError(f'secrets command failed (exit {out.returncode})')
    values = {}
    for line in out.stdout.splitlines():
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            k, v = line.split('=', 1)
            values[k.strip().removeprefix('export ').strip()] = v.strip().strip('"\'')
    return values


def cancel_replaced(cfg, state):
    """Cancel the primary the fallback replaced (plain cancel: it is waiting, nothing to upload)."""
    from sky import jobs
    job = int(state['replaced_job'])
    _get(lambda: jobs.cancel(job_ids=[job]), cfg.get('sdk_timeout_seconds', SDK_TIMEOUT_SECONDS))
    return job


SDK_NEEDS = {'queue_v2': ('all_users', 'job_ids', 'fields'), 'cancel': ('job_ids', 'graceful', 'graceful_timeout'),
             'launch': (), 'tail_logs': ('job_id', 'follow', 'tail', 'output_stream')}   # tail_logs: in a child


def check_sdk(sky_module=None) -> dict:
    """Offline check that the installed SkyPilot exposes the calls this guard makes."""
    import inspect
    problems = []
    try:
        sky = sky_module or __import__('sky')
        jobs = getattr(sky, 'jobs')
    except Exception as e:  # noqa: BLE001
        return {'ok': False, 'problems': [f'cannot import sky.jobs: {type(e).__name__}: {e}']}
    for name, params in SDK_NEEDS.items():
        fn = getattr(jobs, name, None)
        if fn is None:
            problems.append(f'sky.jobs.{name} missing')
            continue
        accepted = inspect.signature(fn).parameters
        problems += [f'sky.jobs.{name} lacks parameter {p}' for p in params if p not in accepted]
    if not callable(getattr(sky, 'stream_and_get', None)):
        problems.append('sky.stream_and_get missing')
    for method in ('from_yaml', 'update_secrets'):
        if not callable(getattr(getattr(sky, 'Task', None), method, None)):
            problems.append(f'sky.Task.{method} missing')
    return {'ok': not problems, 'skypilot_version': getattr(sky, '__version__', None), 'problems': problems}


def load_ledger(cfg) -> dict:
    """The ledger at state_path belongs to one configured job; never reuse it for another."""
    path = Path(cfg['state_path'])
    if not path.exists():
        return {'job_id': cfg['job_id'], 'config_job_id': cfg['job_id']}
    state = json.loads(path.read_text())
    owner = state.get('config_job_id', state.get('job_id', cfg['job_id']))
    state.setdefault('job_id', owner)
    state.setdefault('config_job_id', owner)
    if owner != cfg['job_id']:
        raise ValueError(f"ledger {path} belongs to job {owner}, not {cfg['job_id']}: give each job its own state_path")
    return state


def tick(cfg) -> list:
    path = Path(cfg['state_path'])
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(path) + '.lock', 'w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)    # one tick at a time (timer and loop may overlap)
        except BlockingIOError:
            print(json.dumps({'guard': 'tick skipped: another tick holds the lock'}), flush=True)
            return []
        return _tick(cfg, path)


def _record(state, entry):
    state.setdefault('log', []).append(entry)
    _print(entry)


def _notify_safely(cfg, state, message):
    entry = {'time': time.time(), 'job_id': state['job_id'], 'action': 'notify', 'reason': message}
    try:
        run_action(cfg, 'notify', message, state)
        entry['ok'] = True
    except Exception as e:  # noqa: BLE001
        entry.update(ok=False, error=f'{type(e).__name__}: {e}')
    _record(state, entry)


def _save(path, state):
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(state, indent=1))
    os.replace(tmp, path)                       # durable ledger survives guard restarts


def _print(obj):
    try:
        print(json.dumps(obj), flush=True)
    except (BrokenPipeError, OSError):          # a closed stdout (e.g. `| head`) must not lose the ledger
        pass


def _resolve_replaced(cfg, state, obs):
    """After a fallback: cancel the replaced primary once the fallback is seen alive; if the fallback
    ended at once (no on-demand quota, a failed precheck), go back to watching the primary."""
    primary = state['replaced_job']
    if obs['status'] in TERMINAL:
        prev = state.pop('fallback_prev', {})
        fallback = state['job_id']
        state.update(job_id=primary, stopping=None, cancel_issued=False,
                     attempts_base=prev.get('attempts_base', 0) + 1, wait_started=prev.get('wait_started'),
                     alloc_rc=0, alloc_running=False, alloc_committed=False)
        del state['replaced_job']
        message = f'fallback job {fallback} ended at once ({obs["status"]}); still watching spot job {primary}'
        _record(state, {'time': time.time(), 'job_id': primary, 'action': 'fallback-ended', 'reason': message, 'ok': True})
        _notify_safely(cfg, state, message)
        return observe_skypilot(primary, cfg.get('sdk_timeout_seconds', SDK_TIMEOUT_SECONDS))
    entry = {'time': time.time(), 'job_id': state['job_id'], 'action': 'cancel-replaced', 'reason': f'replaced job {primary}'}
    try:
        cancel_replaced(cfg, state)
        entry['ok'] = True
        del state['replaced_job']
        state.pop('fallback_prev', None)
    except Exception as e:  # noqa: BLE001 - retried next tick; the replaced job may still get capacity
        entry.update(ok=False, error=f'{type(e).__name__}: {e}')
        if 'cancel-replaced' not in state.setdefault('notified', []):
            state['notified'].append('cancel-replaced')
            _notify_safely(cfg, state, f'could not cancel replaced job {primary}; cancel it by hand')
    _record(state, entry)
    return obs


def _tick(cfg, path) -> list:
    state = load_ledger(cfg)
    loaded = json.loads(json.dumps(state))      # the ledger as it was before this tick
    timeout = cfg.get('sdk_timeout_seconds', SDK_TIMEOUT_SECONDS)
    obs = observe_skypilot(state['job_id'], timeout)
    if obs is not None and state.get('replaced_job') is not None:
        state['double_charge_once'] = True    # both jobs could run since the last tick
        obs = _resolve_replaced(cfg, state, obs)
    if obs is not None:
        status = obs['status']
        if cfg.get('free_without_vm') and cfg.get('vm_count_cmd') and status in WAITING - NO_VM_STATUSES:
            obs['vms'] = count_vms(cfg['vm_count_cmd'], cluster_name(obs, state['job_id']))
        rc = int(obs.get('recovery_count') or 0)
        if (cfg.get('free_without_vm') and cfg.get('vm_count_cmd') and status == 'RUNNING'
                and state.get('vm_checked_rc') != rc and not state.get('vm_hook_broken')):
            # Once per allocation: the hook must find the running job's VM, or every wait would count as free.
            vms = count_vms(cfg['vm_count_cmd'], cluster_name(obs, state['job_id']))
            if vms is not None:
                obs['vms_running'], state['vm_checked_rc'] = vms, rc
        # Read the log on every RUNNING tick (commits, evaluations, the watchdog's lines), and only then: for
        # a job in any other non-terminal state this SkyPilot's tail_logs(follow=False) waits for it to run
        # again and never returns.
        # log_read_seconds (optional) reads it less often on a small or shared control host.
        every = cfg.get('log_read_seconds')
        if status == 'RUNNING' and every and time.time() - state.get('log_read_at', 0) < every:
            obs['log_read'] = 'skipped'
        elif status == 'RUNNING':
            state['log_read_at'] = time.time()
            log = read_log(state['job_id'], timeout)
            obs['committed'] = None if log is None else log['committed']
            obs['evals'] = [] if log is None else [e for e in log.get('evals', []) if isinstance(e, str)]
            for key in ('progress', 'stall'):
                obs[key] = log.get(key) if log and isinstance(log.get(key), str) else None
    actions, state = decide(cfg, state, obs, time.time())
    _print({'tick': round(state['last_tick'], 1), 'job_id': state['job_id'],
            'status': None if obs is None else obs['status'], 'attempts': state.get('attempts'),
            'wait_minutes': state.get('wait_minutes'), 'spend_upper_usd': state.get('spend_upper_usd'),
            'done': state['done']})
    fallback_failed = False
    for action, reason in actions:
        if fallback_failed and action == 'notify' and reason.startswith('switched'):
            continue
        if action == 'fallback':
            # Persist the start before launching: if the launch is accepted but its reply is lost, or the
            # guard dies before saving, the next tick adopts that job instead of launching another one.
            started = state.get('fallback_started_at') or time.time()
            state['fallback_started_at'] = loaded['fallback_started_at'] = started
            _save(path, loaded)
        record = {'time': time.time(), 'job_id': state['job_id'], 'action': action, 'reason': reason}
        escalate = None
        try:
            new_job = run_action(cfg, action, reason, state)
            if action == 'fallback':
                record['fallback_job_id'] = new_job
                state.update(job_id=new_job, stopping=None, cancel_issued=False,
                             alloc_rc=0, alloc_running=False, alloc_committed=False)
            elif action == 'cancel':
                state['cancel_issued'] = True
            record['ok'] = True
        except Exception as e:  # noqa: BLE001 - keep the ledger; next tick retries
            record.update(ok=False, error=f'{type(e).__name__}: {e}')
            state['stopping'] = None
            if action == 'fallback':                 # keep the primary; retry (adopting a lost launch) later
                fallback_failed = True
                tries = state.get('fallback_failures', 0) + 1
                prev = state.pop('fallback_prev', {})
                state.update(fallback_failures=tries, fallback_used=tries >= FALLBACK_TRIES,
                             attempts_base=prev.get('attempts_base', state.get('attempts_base', 0)),
                             wait_started=prev.get('wait_started'))
                escalate = (f'fallback launch failed ({record["error"]}); the spot job keeps waiting'
                            + ('; no more fallback tries' if tries >= FALLBACK_TRIES else f'; retry {tries + 1} of {FALLBACK_TRIES} next tick'))
            elif action == 'cancel':
                escalate = f'cancel failed ({record["error"]}); limit: {reason}'
        _record(state, record)
        if escalate:                            # notify after recording the failure
            _notify_safely(cfg, state, escalate)
    _save(path, state)
    return actions


# ---- --init-config: guard.json from the contract and the campaign ledger -------------------------

def campaign_history(ledger: dict, read_state) -> tuple[float, int]:
    """Spend bound and attempts of earlier jobs of this campaign, from their guard ledgers."""
    spent, attempts = 0.0, 0
    for job in ledger.get('jobs', []):
        state = read_state(job['state_path'])
        if not state.get('done'):
            raise ValueError(f"job {job['job_id']} of this campaign is not finished according to its guard; "
                             'close it first (cancel it, then run its guard once with --once)')
        spent += job['ceiling_hourly_usd'] * state.get('billable_seconds', 0.0) / 3600
        attempts += max(0, int(state.get('attempts', 1)) - int(job.get('attempts_before', 0)))
    return round(spent, 4), attempts


def runway_hours(cfg) -> float:
    """Hours of non-terminal job time the budget allows at the ceiling (before the shutdown reserve)."""
    left = cfg['budget_usd'] - _base_usd(cfg) - cfg['ceiling_hourly_usd'] * _reserve_hours(cfg)
    return round(left / cfg['ceiling_hourly_usd'], 2)


def init_config(m: dict, job_id: int, workdir: Path, notify_cmd=None, attended=False, fallback_task=None,
                secrets_cmd=None, dry_run=False, vm_count_cmd=None) -> dict:
    b, r = m.get('budget') or {}, m.get('recovery') or {}
    for k in ('total_limit', 'ceiling_hourly_usd'):
        if b.get(k) is None:
            raise ValueError(f'budget.{k} is required for the guard')
    if notify_cmd is None and not attended:
        raise ValueError('give --notify-cmd (limits, milestones and the end must reach someone); '
                         '--attended only when a person watches the whole run')
    ledger_path = workdir / 'campaign.json'
    ledger = json.loads(ledger_path.read_text()) if ledger_path.exists() else {'run_id': m['run_id'], 'jobs': []}
    if ledger['run_id'] != m['run_id']:
        raise ValueError(f"{ledger_path} belongs to run {ledger['run_id']}")
    if any(int(j['job_id']) == int(job_id) for j in ledger['jobs']):
        raise ValueError(f'job {job_id} is already registered in {ledger_path}')
    spent, attempts = campaign_history(ledger, lambda p: json.loads(Path(p).read_text()) if Path(p).exists() else {})
    wait = r.get('max_capacity_wait_minutes')
    if wait is None:
        if m.get('purchase_model') != 'on-demand':
            raise ValueError('recovery.max_capacity_wait_minutes is required for a spot run')
        wait = DEFAULT_ON_DEMAND_WAIT_MINUTES
    absolute = r.get('max_attempts')
    worker = isinstance(m.get('monitoring'), dict) and m['monitoring'].get('guard') == 'external'
    cfg = {'job_id': int(job_id), **({'deadline_utc': b['deadline_utc']} if b.get('deadline_utc') is not None else {}),
           **({'max_attempts': int(absolute) + int(r.get('planned_drills', 0))} if absolute is not None else {}),
           'max_attempts_without_progress': int(r.get('max_attempts_without_progress', DEFAULT_ATTEMPTS_WITHOUT_PROGRESS)),
           'max_capacity_wait_minutes': wait, 'startup_allowance_minutes': r.get('startup_allowance_minutes', 10),
           'budget_usd': b['total_limit'], 'ceiling_hourly_usd': b['ceiling_hourly_usd'],
           'reserved_usd': b.get('controller_allowance') or 0.0, 'spent_before_usd': spent,
           'attempts_before': attempts, 'poll_seconds': 60, 'shutdown_reserve_minutes': 5,
           'graceful_cancel_seconds': 120, 'max_blind_minutes': 15, 'notify_milestones': True,
           # With the contract's guard set to external the worker enforces its limits too and sends the evaluations.
           'worker_limits': worker, 'notify_evals': not worker,
           **({'progress_period_seconds': m['watchdog']['progress_period_seconds']}
              if isinstance(m.get('watchdog'), dict) and 'progress_period_seconds' in m['watchdog'] else {}),
           'state_path': str(workdir / 'guard' / f'state-job{job_id}.json')}
    if notify_cmd is not None:
        cfg['notify_cmd'] = notify_cmd
    if b.get('free_without_vm') is True:
        cfg['free_without_vm'] = True
        if vm_count_cmd:
            cfg['vm_count_cmd'] = vm_count_cmd
    fb = r.get('on_demand_fallback')
    if r.get('allow_on_demand') is True and fb:
        if not fallback_task:
            raise ValueError('the contract approves an on-demand fallback: give --fallback-task (render_job.py '
                             '--variant on-demand-fallback)')
        task = json.loads(Path(fallback_task).read_text())
        needed = [n for n in task.get('envs', {}).get('SECRET_NAMES', '').split(',') if n]
        if needed and not secrets_cmd:
            raise ValueError(f'the fallback task needs secrets {needed}: give --secrets-cmd')
        if secrets_cmd:
            missing = [n for n in needed if not read_secrets(secrets_cmd).get(n)]
            if missing:
                raise ValueError(f'--secrets-cmd does not print {missing}')
        cfg['fallback'] = {'approved': True, 'after_capacity_wait_minutes': fb['after_capacity_wait_minutes'],
                           'task_yaml': str(Path(fallback_task).resolve()),
                           **({'secrets_cmd': secrets_cmd} if secrets_cmd else {})}
    check_config(cfg)
    cfg_path = workdir / 'guard' / f'guard-job{job_id}.json'
    summary = {'guard_config': str(cfg_path), 'runway_hours': runway_hours(cfg), 'spent_before_usd': spent,
               'attempts_before': attempts, 'max_attempts': cfg.get('max_attempts'),
               'max_attempts_without_progress': cfg['max_attempts_without_progress'], 'deadline_utc': cfg.get('deadline_utc'),
               'free_without_vm': cfg.get('free_without_vm', False), 'vm_count': 'vm_count_cmd' in cfg,
               'notify': 'notify_cmd' in cfg, 'fallback': 'fallback' in cfg}
    if dry_run:
        return {**summary, 'dry_run': True, 'config': cfg}
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(json.dumps(cfg, indent=1))
    ledger['jobs'].append({'job_id': int(job_id), 'registered_at': time.time(), 'guard_config': str(cfg_path),
                           'state_path': cfg['state_path'], 'ceiling_hourly_usd': cfg['ceiling_hourly_usd'],
                           'attempts_before': attempts})
    ledger_path.write_text(json.dumps(ledger, indent=1))
    return summary


def close_job(workdir: Path, job_id: int, reason=None) -> dict:
    """Close a job's ledger so the campaign can continue: a terminal job is closed by one normal tick; a
    job the guard cannot observe (unlisted, server migrated) only with a reason a person gives after
    checking the provider's console; a job still running is refused (cancel it first)."""
    ledger = json.loads((workdir / 'campaign.json').read_text())
    job = next((j for j in ledger['jobs'] if int(j['job_id']) == int(job_id)), None)
    if job is None:
        raise ValueError(f'job {job_id} is not in {workdir / "campaign.json"}')
    cfg = check_config(json.loads(Path(job['guard_config']).read_text()))
    state = load_ledger(cfg)
    if state.get('done'):
        return {'job_id': job_id, 'closed': 'already done'}
    obs = observe_skypilot(state['job_id'], cfg.get('sdk_timeout_seconds', SDK_TIMEOUT_SECONDS))
    if obs is not None and obs['status'] not in TERMINAL:
        raise ValueError(f"job {state['job_id']} is {obs['status']}: cancel it first (sky jobs cancel), then close")
    if obs is not None:
        tick(cfg)
        return {'job_id': job_id, 'closed': obs['status']}
    if not reason:
        raise ValueError('the job cannot be observed: check the provider console that nothing of it runs, '
                         'then close it with --reason "<what you checked>"')
    if state.get('last_tick') is None and _number(job.get('registered_at')):
        state['last_tick'] = job['registered_at']              # never ticked: charge from registration
    _, state = decide(cfg, state, None, time.time())          # charge the blind time up to now
    state.update(done=True, closed_by_hand={'time': time.time(), 'reason': reason})
    _save(Path(cfg['state_path']), state)
    return {'job_id': job_id, 'closed': 'by hand', 'spend_upper_usd': state.get('spend_upper_usd')}


def self_test():
    cfg = check_config({'job_id': 1, 'max_attempts': 2,             # no deadline: the default
                        'max_capacity_wait_minutes': 10, 'budget_usd': 10, 'ceiling_hourly_usd': 4,
                        'state_path': '/dev/null'})
    acts, st = decide(cfg, {}, {'status': 'RECOVERING', 'recovery_count': 1}, 0)
    acts, st = decide(cfg, st, {'status': 'RECOVERING', 'recovery_count': 1}, 11 * 60)
    assert acts and acts[0][0] == 'cancel', acts
    acts, st = decide(cfg, {}, None, 0)
    acts, st = decide(cfg, st, None, 60 * 60 * 3)
    assert all(a[0] != 'cancel' for a in acts), acts      # never cancels blind
    acts, st = decide({**cfg, 'deadline_utc': '1970-01-01T00:00:01Z'}, {}, {'status': 'RUNNING'}, 60)
    assert ('cancel', 'campaign deadline reached') in acts, acts
    print(json.dumps({'self_test': 'ok'}))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--config')
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument('--check-sdk', action='store_true')
    mode.add_argument('--once', action='store_true')
    mode.add_argument('--loop', action='store_true')
    mode.add_argument('--self-test', action='store_true')
    mode.add_argument('--init-config', action='store_true')
    mode.add_argument('--close-job', action='store_true',
                      help='close a job\'s ledger (terminal, or unobservable with --reason) so the campaign can relaunch')
    ap.add_argument('--contract', help='--init-config: experiment.json')
    ap.add_argument('--job-id', type=int, help='--init-config: the submitted managed job id')
    ap.add_argument('--workdir', help='--init-config: campaign directory (campaign.json, guard/)')
    ap.add_argument('--notify-cmd', help='--init-config: JSON argv that receives a JSON message on stdin')
    ap.add_argument('--attended', action='store_true', help='--init-config: no notify command, a person watches')
    ap.add_argument('--fallback-task', help='--init-config: rendered on-demand fallback task (approved fallback)')
    ap.add_argument('--secrets-cmd', help='--init-config: JSON argv printing NAME=value lines for the fallback')
    ap.add_argument('--vm-count-cmd', help='--init-config: JSON argv listing a cluster\'s VMs (cluster name appended); '
                                           'with budget.free_without_vm, waiting without VMs is not charged')
    ap.add_argument('--dry-run', action='store_true', help='--init-config: print config and runway, write nothing')
    ap.add_argument('--reason', help='--close-job: what a person checked when the job cannot be observed')
    args = ap.parse_args(argv)
    if args.self_test:
        return self_test()
    if args.check_sdk:
        result = check_sdk()
        print(json.dumps(result))
        return 0 if result['ok'] else 1
    if args.init_config:
        if not (args.contract and args.workdir and args.job_id is not None):
            ap.error('--init-config needs --contract, --job-id and --workdir')
        try:
            result = init_config(json.loads(Path(args.contract).read_text()), args.job_id,
                                 Path(args.workdir).expanduser(),
                                 json.loads(args.notify_cmd) if args.notify_cmd else None, args.attended,
                                 args.fallback_task, json.loads(args.secrets_cmd) if args.secrets_cmd else None,
                                 args.dry_run, json.loads(args.vm_count_cmd) if args.vm_count_cmd else None)
        except (ValueError, KeyError, TypeError, OSError, RuntimeError) as e:
            print(json.dumps({'ok': False, 'error': f'{type(e).__name__}: {e}'}))
            return 1
        print(json.dumps(result, indent=1))
        return 0
    if args.close_job:
        if not (args.workdir and args.job_id is not None):
            ap.error('--close-job needs --workdir and --job-id')
        try:
            result = close_job(Path(args.workdir).expanduser(), args.job_id, args.reason)
        except (ValueError, KeyError, OSError) as e:
            print(json.dumps({'ok': False, 'error': f'{type(e).__name__}: {e}'}))
            return 1
        print(json.dumps(result))
        return 0
    if not args.config:
        ap.error('--once and --loop need --config')
    cfg = check_config(json.loads(Path(args.config).read_text()))
    if not cfg.get('notify_cmd'):
        print(json.dumps({'guard': 'warning', 'message': 'no notify_cmd: limits, milestones and the end reach nobody'}),
              file=sys.stderr)
    load_ledger(cfg)                          # refuse a ledger of another job before the first tick
    while True:
        tick(cfg)
        if args.once or json.loads(Path(cfg['state_path']).read_text()).get('done'):
            return 0
        blocked = stuck_minutes()
        if blocked >= cfg.get('max_blind_minutes', 15):
            message = (f'a SkyPilot call has been blocked for {blocked:.0f} min; the guard exits so its supervisor '
                       'restarts a clean process (systemd Restart=on-failure); without one, restart it by hand')
            try:
                current = json.loads(Path(cfg['state_path']).read_text()).get('job_id', cfg['job_id'])
                run_action(cfg, 'notify', message, {'job_id': current})
            except Exception:  # noqa: BLE001 - exiting matters more than the message
                pass
            print(json.dumps({'guard': 'restart', 'message': message}), file=sys.stderr)
            return 1
        time.sleep(cfg.get('poll_seconds', 60))


if __name__ == '__main__':
    sys.exit(main())
