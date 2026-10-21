# Signals, budgets, and bounded intervention
Checked 2026-10-01. Hangs and guard notices: [guard.md](guard.md).

Commands: `sky api info`; `sky status -u`; `sky jobs queue -a`; `sky jobs logs JOB_ID`; `sky jobs logs --controller JOB_ID`; `sky cost-report -a`. `sky jobs queue -r` can restart a stopped remote controller, so check spending scope first. CLI/API version differences must be verified against the lock. Current queue includes RECOVERING for worker or controller problems.
Source: https://docs.skypilot.ai/en/latest/reference/cli.html

Cost reports are estimates from cached status, explicitly potentially inaccurate for spot/autostop and out-of-band changes. An authoritative provider bill may also lag. `max_hourly_cost` is an optimizer filter, not a budget guard. Maintain both billed-to-date and conservative accrued-unbilled estimates without double counting. Count setup/restore/recompute, controller, disk/object storage/requests and transfer/egress. Maintain an observed upper hourly rate and a safety margin for teardown/checkpoint latency. Revalidate dynamic prices, especially around provider pricing migrations.
Sources: https://docs.skypilot.ai/en/stable/reference/cli.html and https://docs.skypilot.ai/en/latest/reference/yaml-spec.html

Hardware recovery and no-capacity retries can be unbounded (SkyPilot FAILOVER keeps retrying spot capacity indefinitely; see the launch gates). max_restarts_on_errors only limits code errors; special recover_on_exit_codes bypass it. The worker stops itself at its limits (exit 0); with the optional guard, a limit it reaches cancels the managed job itself. Ordinary idleness autostop does not limit total training duration. A worker-level timeout resets on recovery; the worker's limits read the whole run's journal and alive files, so they do not. The stall watchdog's exit 93 must never be listed in `recover_on_exit_codes`: a hang is not repaired by a restart.
Sources: https://docs.skypilot.ai/en/latest/examples/managed-jobs.html and https://docs.skypilot.ai/en/latest/reference/auto-stop.html

Useful-work metrics: global step/tokens/examples committed, measured throughput after warmup, GPU utilization and memory, data-loader wait, CPU/disk/network contention, loss/NaN signal, latest complete durable checkpoint step/time and restore cost. Compare numerics with the training plan; do not invent convergence thresholds. For checkpoint completeness use verified commit/manifest protocol, not mere object existence or an open upload.

Record the metrics run identity (W&B or the chosen tool), attempt/restart count, live scientific step, durable checkpoint step, replay work and telemetry freshness separately. W&B history indices count logging events, not optimizer updates. A resumed checkpoint can legitimately trail the latest logged scientific step; expose that rollback and replay rather than double-counting it as new productive work. Keep durable checkpoints independent of W&B, preferably in the training provider's same-region object store. A W&B outage is an observability event; follow the predeclared bounded-buffer/fail policy and never let it change restore truth or create duplicate training workers.

Implement these checks in the deployed campaign guard (or a versioned project equivalent), using conservative deadlines and explicit approved actions; no continuous AI reasoning is needed. Reuse qualified scripts across experiments with validated configuration.

On cancellation verify terminal state. Cleanup inventory: `gpu-training-finish` → `references/closure.md#exact-cleanup-inventory`.

## Spot-only recovery
For a spot run, verify actual purchase mode as well as provider/region on every replacement. No automatic on-demand fallback, except the guard-executed fallback the user approved in advance (`recovery.on_demand_fallback`). Track durable total attempts and capacity-wait elapsed time against max_capacity_wait_minutes; unavailable capacity triggers bounded waiting and then stop/report. SkyPilot application-error restart limits are not total infrastructure recovery limits. Do not broaden resource candidates to make the dashboard green.

## Observation records
The guard ledger is the observation record (status history with timestamps, attempts, spend bound, actions); the job logs' `CHECKPOINT_JOURNAL`/`CHECKPOINT_COMMITTED` lines and the bucket journal carry progress; the finish runbook collects both with provider resource IDs.

## Classifying job state
Do not treat a healthy API or RUNNING job as evidence of useful progress. Distinguish productive training, dataset staging, compilation/warmup, checkpoint upload, recovery, capacity wait, application failure and controller/network outage. Compare step/token progression with its expected envelope before terminating an apparently slow job. A short API failure is not proof the worker died.

## Placement and price checks
With the provider/region check on initial allocation and every recovery, record GPU count/type, current rate/quote age and storage locality. Catalog price alone is not an actual bill.

## Intervention and updates
Respect the approved checkpoint grace and reserve its cost. Send meaningful updates for first useful steps/durable checkpoint, material slowdown or price change, recovery, budget risk, blockers and completion. Honor the requested monitoring stopping condition. A terminal job is not cleanup proof: reconcile provider inventory, preserved artifacts and residual charges before handing off the final report. Label estimates and unperformed tests explicitly.
