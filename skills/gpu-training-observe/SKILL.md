---
name: gpu-training-observe
description: Monitor a running GPU training campaign for useful progress, checkpoint durability, spending, fixed placement and recovery health, and reconcile uncertain job state. Use when a job is running, stuck, recovering or preempted, when asked how the training is going or how much budget is left, or to install or read the campaign guard.
---

# GPU training observation

Install or read the campaign guard, monitor the running campaign and reconcile uncertain job state. Routine telemetry, checkpointing and approved enforcement run in deterministic scripts and the campaign guard, never a resident AI agent. Bundled scripts and references are relative to this skill's directory (`<skill-dir>`); call helpers by absolute path with absolute project paths.

## Inputs
`experiment.json`, launch's outputs (`campaign.json`, `guard/guard-job<ID>.json`) and the launch record, controller version lock, durable checkpoint manifest, actual approval scope, prior observations.

## Steps
### Campaign guard (installed once, attached per job by launch)
SkyPilot waits for spot capacity indefinitely and has no campaign budget, so `scripts/campaign_guard.py` (or the user's existing equivalent) enforces the approved limits on managed jobs (Standard tier); a Minimal-tier cluster bounds itself with its run timeout and autodown. On the worker, the stall watchdog (`scripts/progress_watch.py`) ends a hung run (`STALL`, stacks, exit 93); the guard notifies it and cancels a wedged job ([guard](references/guard.md#stall-watchdog)).
1. The guard runs on the always-on control host, independent of the GPU worker, against the submitter's SkyPilot API server in the same active workspace as the job, with a login allowed to cancel it. A laptop suits only an attended, kept-awake pilot, where the worker's runtime timeout is the last defence.
2. At install and after any SkyPilot upgrade, run its self-test and SDK check (part of the lifecycle start checks):
   `~/sky-venv/bin/python <skill-dir>/scripts/campaign_guard.py --self-test` and `--check-sdk`
3. The launch runbook §4 attaches it to each job (`--init-config`, a first `--once` tick that must observe the job, then `--loop` under systemd; both recorded in the launch evidence). Fields and behaviour: [guard.md](references/guard.md#campaign-guard).

### Observe and diagnose
4. **Wait, don't poll** ([runbook](references/runbook.md)): after launch, wait as a background command for the first `CHECKPOINT_COMMITTED` line (`grep -m1` on `sky jobs logs`), then for each planned evaluation (the first, then about 25/50/75 % and the end: an `EVAL` or `STALL` line, with a timeout of 1.5 x its expected time); report and end the turn. The guard notifies milestones, every evaluation, limits and the end. On a failure, triage by status and exit code (runbook table): preflight (4), blocked (3), application, infrastructure or cancelled, with the next action (an application failure is fixed and requalified with the qualification task before any relaunch).
5. **Judge the training at planned evaluations** (and after each recovery and at the end; [runbook](references/runbook.md#training-quality-at-planned-evaluations)): read the metrics with what you know about this kind of model and notify the user about anything suspicious. Never change the code or the run on your own; only a crash, the stall watchdog or a guard limit stops a run.
6. Correlate managed job state, provider inventory, training logs/metrics (W&B or the project's chosen tool) and completed checkpoint metadata.
7. At allocation and every recovery, check the exact provider/region against the manifest.
8. [Classify what the job is doing](references/signals-budget.md#classifying-job-state) before terminating an apparently slow job.
9. Track the deadline (if set), all worker attempts and non-worker charges; the guard's ledger carries the conservative spend bound, and the provider's billing the actual cost.
10. If submission/status is uncertain, recover the original request/job and provider IDs (run names are not idempotency keys). Controller state and duplicate controllers: `gpu-training-controller`.
11. At a budget, runtime or attempt limit, within explicit existing scope: stop new retries, ask the trainer to checkpoint if reachable, cancel the exact managed job (not just the worker, which managed recovery may replace) and verify cleanup; otherwise report the approval needed.

## Outputs
- The guard ledger (status history, attempts, spend bound, actions) persisted outside the worker and across restarts, the job logs and the campaign evidence (finish runbook); no tokens or secret-bearing logs.
- Updates at meaningful events; a final report only after reconciling provider inventory, preserved artifacts and residual charges.
- Without the training project: reviewed integration templates naming unimplemented adapters.

## Gates
- **Authorization:** manifests are planning records, not permission. Read-only observation is generally safe; credentials, billing integrations, new permissions and mutations need host authorization. Missing budget blocks spending/recovery expansion, not research or reporting; missing billing never means zero spending.
- **Fixed placement:** training provider fixed; region changes need explicit approval first. No automatic spot-to-on-demand promotion except the guard's user-approved `fallback`.
- **No duplicate launch:** unknown/stale inputs block new attempts and trigger reconciliation, never a duplicate launch or fresh campaign.
- **Metrics tooling is the researcher's choice:** W&B, MLflow, TensorBoard or none, recorded as `monitoring.wandb` or `monitoring.metrics_backend`; it never decides recovery.
- **W&B history policy** (`monitoring.wandb.history_policy`): the user chooses at intake ([wandb.md](references/wandb.md#history-policy-the-user-chooses)); never switch mid-run.
- **Access preparation:** follow the lifecycle credential-preparation reference; keep observer permissions separate from trainer/controller mutation rights; report expiry or denied reads without logging secrets; never create a W&B run or upload metrics to test authentication.
- **Project-first decisions:** before asking the user anything or deciding for them, read `gpu-training-lifecycle` → `references/working-rules.md` (interaction levels included).

## References
- [runbook.md](references/runbook.md): read after launch (waiting, status, triage).
- [signals-budget.md](references/signals-budget.md): read when judging progress, cost or interventions.
- [guard.md](references/guard.md): read when configuring the guard or the `watchdog` limits, when a guard notice arrives (cancel, blind, wedged, STALL, fallback), or before a relaunch.
- [wandb.md](references/wandb.md): read before implementing W&B logging (the same identity, rank-zero and append-only principles apply to other trackers).
- [commands.md](references/commands.md): read for diagnostics and before cancelling an exact job (the runbook has the commands it runs).
