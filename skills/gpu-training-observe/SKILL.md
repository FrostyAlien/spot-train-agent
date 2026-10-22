---
name: gpu-training-observe
description: Monitors a running GPU training campaign for useful progress, checkpoint durability, spending, fixed placement and recovery health, and reconciles uncertain job state. Use when a job is running, stuck, recovering or preempted, when asked how training is going or how much budget is left, when a worker or guard notice arrives, or to install or read the optional campaign guard.
---

# GPU training observation

Waits for the worker's notices and the planned evaluations, judges the training, triages failures and reconciles uncertain job state. Bundled paths are relative to this skill's directory (`<skill-dir>`); helpers take absolute paths.

## Inputs
`experiment.json`, launch's outputs (`campaign.json`, `guard/guard-job<ID>.json`) and the launch record, controller version lock, durable checkpoint manifest, actual approval scope, prior observations.

## Steps
1. **Rely on the worker's limits.** The worker enforces the run's budget, deadline and attempt limits at every start, because SkyPilot has no campaign budget, and sends its notices (`scripts/notify.py`). Its stall watchdog (`scripts/progress_watch.py`) ends a hung run with exit 93. Read [guard.md](references/guard.md#stall-watchdog) before tuning the `watchdog` limits.
2. **Install the optional guard** only when `monitoring.guard` is `external`; read [guard.md](references/guard.md#campaign-guard) first for what it adds, its host and its fields. Run its start checks at install and after any SkyPilot upgrade (`gpu-training-controller` → `references/runbook.md#6-start-checks-once-per-chosen-skypilot-version`). The launch runbook attaches it to each job (`gpu-training-launch` → `references/runbook.md#4-submit-standard-tier-then-the-optional-guard`); record its first tick and the `--loop` unit in the launch evidence.
3. **Wait, don't poll.** Run each wait (the first `CHECKPOINT_COMMITTED` line, then each planned evaluation) as a background command, report, and end the turn ([runbook](references/runbook.md#wait-for-a-milestone-blocking-no-polling)); the worker's notices make polling useless.
4. **Judge the training** at the planned evaluations, after each recovery and at the end ([runbook](references/runbook.md#training-quality-at-planned-evaluations)): notify the user about anything suspicious. Never change the code or the run on your own, because the experiment is the user's; only a crash, the stall watchdog, a limit or the user stops a run.
5. **Triage a failed job** by status and exit code with the [triage table](references/runbook.md#triage-a-job-that-did-not-succeed); requalify any code fix before a relaunch.
6. **Check placement** at allocation and every recovery: report any provider, region or purchase mode that differs from the manifest, because the provider stays fixed (lifecycle provider lock; [spot-only recovery](references/signals-budget.md#spot-only-recovery), [price checks](references/signals-budget.md#placement-and-price-checks)).
7. **Classify what the job is doing** before terminating an apparently slow job ([signals](references/signals-budget.md#classifying-job-state)): RUNNING is no proof of useful progress.
8. **Track the spend** against the deadline (if set), all worker attempts and non-worker charges ([costs and limits](references/signals-budget.md#costs-and-limits)): the worker's `limits` journal events (or the guard's ledger) carry the conservative bound, the provider's bill the actual cost.
9. **Reconcile uncertain state.** Correlate managed job state, provider inventory, training logs and metrics and completed checkpoint metadata, and recover the original request, job and provider IDs, since run names are not idempotency keys ([commands](references/commands.md#read-state-and-logs)). Controller state and duplicate controllers: `gpu-training-controller`.
10. **Cancel on request or when a limit was not enforced.** The user authorizes a cancel, by asking or through the limit they approved; without either, report the approval needed. Stop new retries, ask the trainer to checkpoint if reachable, cancel the exact managed job (not just the worker, which managed recovery may replace) and verify cleanup ([commands](references/commands.md#cancel-within-explicit-operation-scope)).

## Outputs
- The run journal's `limits` and `limit` events and FINAL.json on the checkpoint mount, the guard ledger when the guard runs, the job logs and the campaign evidence (finish runbook); no tokens or secret-bearing logs.
- Updates at meaningful events; a final report only after reconciling provider inventory, preserved artifacts and residual charges.
- Without the training project: reviewed integration templates naming unimplemented adapters.

## Gates
- **Read `gpu-training-lifecycle` → `references/working-rules.md` before asking the user anything or deciding for them:** it sets the project-first decisions and the interaction levels.
- **Credentials, billing integrations, new permissions and mutations need the host's authorization,** because manifests are planning records, not permission; read-only observation is generally safe.
- **Missing budget blocks spending and recovery expansion,** because nothing caps what is not approved; research and reporting go on, and missing billing never means zero spending.
- **Never launch a duplicate:** unknown or stale inputs block new attempts and trigger reconciliation, never a fresh campaign.
- **The metrics tool is the researcher's choice** (`monitoring.wandb` or `monitoring.metrics_backend`) and never decides recovery, because checkpoints are the training truth.
- **Keep the W&B history policy** (`monitoring.wandb.history_policy`) the user chose at intake ([wandb.md](references/wandb.md#history-policy-the-user-chooses)); never switch it mid-run.
- **Keep observer access separate** from trainer and controller mutation rights (lifecycle `references/credentials.md`); report expiry or denied reads without logging secrets; never create a W&B run or upload metrics to test authentication.

## References
- [runbook.md](references/runbook.md): read after launch and when a job did not succeed; it holds the waits, quality check and triage table.
- [guard.md](references/guard.md): read when configuring the guard or the `watchdog` limits, when a guard or `STALL` notice arrives, or before a relaunch with the guard; it holds the guard's features, fields and notices and the stall watchdog.
- [signals-budget.md](references/signals-budget.md): read when judging progress, cost or an intervention; it holds the signals, cost rules and placement checks.
- [commands.md](references/commands.md): read for diagnostics and before cancelling an exact job; it holds the read and cancel commands.
- [wandb.md](references/wandb.md): read before implementing W&B logging; it holds the identity, rank-zero and append-only rules (they apply to other trackers too).
