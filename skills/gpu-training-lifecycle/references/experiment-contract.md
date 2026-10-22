# Experiment contract v1

Use one project-local JSON record per run, plus an append-only resource/event journal. The schema is intentionally small; preserve extra project fields. All stored approval fields are descriptive and must be checked against real user authorization before external actions.

Contents: [fields](#fields); [what to record before launch](#what-to-record-before-launch); [state and locks](#state-and-locks); [adjacent artifacts](#adjacent-artifacts); [launch-phase fields](#launch-phase-fields).

## Fields
- `schema_version`: 1; `run_id`: stable nonempty identifier; `state`: planning, ready, running, recovering, paused, completed, cancelled, or failed.
- `tier`: `minimal` (plain self-bounding on-demand cluster) or `standard` (managed job; the worker enforces the run's limits, the campaign guard is optional); null while planning, absent means `standard`. Locked with the run.
- `purchase_model`: `spot` or `on-demand`, explicitly selected before launch; never default silently to a paid mode. Keep fixed throughout automatic recovery; never fall back from spot to on-demand silently. The only exception is an approved `recovery.on_demand_fallback` with `allow_on_demand=true`, in the same provider and budget, rendered in one of two ways (`gpu-training-launch` → `references/launch-gates.md#recovery-semantics`). On-demand runs need no capacity-wait limit; a plain `sky launch` with `recovery.enabled=false` is `tier: minimal`. A separately requested initial on-demand experiment is a different reviewed run.
- `code`: `repo` (a local path or a git URL) and `commit` (the exact commit baked into the image); required at launch. Code from a git host is cloned at that commit on the build machine and baked into the image, so recovery never depends on the git host or a token; a private repository's deploy key or token stays on the build machine.
- `provider`, `region`: resolved names for launch. Before launch these may be null. The controller provider may differ from the GPU provider.
- `gpu`: model, count (1–8 for this suite). Record GPU memory, interconnect, CPU/RAM, disk and network in an attached offer, not just card count. Optional `gpu.instance_type` records an exact pinned provider-catalog identifier when preset CPU/RAM matters; render it unchanged and keep it in the GPU lock. Resolve it when the provider sells GPU counts only as fixed presets, instead of multiplying a one-GPU preset's price.
- `disk`: optional `size_gb`, the worker boot disk (SkyPilot's default otherwise), sized by the data skill's disk budget (image, `COPY` data, mount caches, checkpoints not yet uploaded, margin).
- `image`: reference and immutable digest; `registry_login: true` for a private registry (the launch passes `SKYPILOT_DOCKER_USERNAME/PASSWORD/SERVER` through `--secret-file`; the rendered task lists the names in `SECRET_NAMES`). Do not store registry passwords.
- `dataset`: uri and immutable version/manifest digest, optional `mount_mode` (`COPY`, `MOUNT`, `MOUNT_CACHED`, or null for no SkyPilot mount), optional `qualify_uri` (the staged qualification subset, e.g. `<uri>/qualify`, mounted in the same mode by the qualification task; data skill: staging); an `hf://datasets/<org>/<name>@<revision>` uri reads the Hub directly (read-only, `MOUNT` or `COPY`, a pinned revision).
- `checkpoint`: uri `<bucket>/<prefix>` (one prefix per run) and the same run_id, optional `mount_mode` (`MOUNT_CACHED` default, or `MOUNT`) and `mount_cached` (overrides of SkyPilot's `MODEL_CHECKPOINT_RW` preset that the renderer applies to a `MOUNT_CACHED` checkpoint mount, such as `{"transfers": 4}`; rarely needed). URIs identify durable destinations, not merely a mountpoint on an ephemeral worker; the renderer mounts the bucket with SkyPilot file mounts and points `CHECKPOINT_DIR`/`DATASET_DIR` at the prefix (SkyPilot's `MOUNT` modes take a bucket root only).
- `budget` (monetary values finite and nonnegative; unknown values stay null while planning and block launch validation, except a null deadline, which means none):
  - `currency`, `total_limit` and `controller_allowance` (included in the total).
  - `ceiling_hourly_usd`: the node's on-demand rate.
  - `deadline_utc` (optional, null by default: the budget is the hard stop): when set, a time bound the worker enforces, and the guard when used.
  - `planned_job_hours` (optional): expected job time including startup, drills and replays; the validator requires the budget runway at the ceiling to cover it.
  - `free_without_vm`: true when the provider research shows compute is billed only while a VM exists; the worker's limits and the guard then do not charge waiting without a VM.
  - `spot_price_cap` (optional, spot only): see [launch-phase fields](#launch-phase-fields).
  - This file enforces nothing. A Standard run's spend bound is the ceiling charge and its time bound `deadline_utc`; the worker enforces them at every start and during the run (`gpu-training-recover` → `references/contract.md#4-crash-safe-checkpoint-publication-on-a-skypilot-bucket-mount`, the run's limits from inside the worker), and the campaign guard too when `monitoring.guard` is `external`.
  - `tier: minimal` instead needs `max_runtime_hours`, `max_worker_runtime_seconds` (an integer, Minimal only: the single run's timeout, which the rendered task enforces with `timeout`) and `autodown_idle_minutes`. `max_runtime_hours` must exceed the timeout plus idle autodown with room for startup, and `max_runtime_hours x ceiling_hourly_usd + controller_allowance` must fit `total_limit`.
- `recovery` (a policy to translate into the SkyPilot version chosen at the start; it is NOT a SkyPilot configuration field, and a local retry count needs persistent accounting across replacement workers):
  - `enabled`; `allow_provider_change=false`.
  - `allow_region_change=false` unless specifically agreed; then `allowed_regions` (including `region`) and `region_change_evidence` {shared_storage: true, cross_region_transfer_cost_per_gib: 0, covers: [checkpoint, dataset, image], checked_at, source}.
  - `allow_on_demand=false` unless the user pre-approved an on-demand fallback (see `purchase_model`).
  - `max_attempts` (optional): an absolute cap on infrastructure allocations, null for none; unused for `tier: minimal`, which is one allocation. The worker counts allocations as worker starts in the run's journal: a start after an application-error restart on the same VM counts too (a start that only put its VM under the spot price cap does not).
  - `max_attempts_without_progress` (default 10): allocations in a row that end without a new checkpoint; a long run with many preemptions but steady progress is never stopped by a count.
  - `max_app_restarts` (optional nonnegative integer, SkyPilot's default 0): application-error restarts. Why a deterministic failure makes a high value costly: `gpu-training-launch` → `references/launch-gates.md#recovery-semantics`.
  - `max_capacity_wait_minutes`: a positive finite limit for spot reacquisition when supplied, counted beyond `startup_allowance_minutes` (the stack's normal allocation start: provision, image pull, mounts, data copy; measured by the qualification smoke, default 10). With `free_without_vm` a long wait costs little, so it can be generous.
  - `planned_drills` (nonnegative integer, default 0): deliberate interruptions for skill development, whose instructions live in the bundle repository, not a step of a run. They consume attempts, so the attempt cap is `max_attempts + planned_drills`.
- `watchdog` (optional; the worker's stall watchdog, observe skill `scripts/progress_watch.py`; defaults in parentheses):
  - `enabled` (true; false when the entrypoint cannot tick), `k` (5: the train and eval limits rise to k x the largest gap seen), `progress_period_seconds` (300).
  - `limits`: the quiet limit of each phase, seconds without a tick or bytes moved: `train` (180), `eval` (600), `save` (600), `final` (600), `startup` (1800). Copy the qualification's `recommended.watchdog` (launch runbook).
  - `ceilings`: hard caps on the length of one `eval`, `save` or `final` phase; none by default, set only when the user asks.
  - The renderer passes them to the task as environment variables; the qualification task uses the defaults, with `startup` inside its per-start timeout.
  - Once every rank has exited while the launcher still runs (its own end-of-run work, or a restart of its workers), `final` applies: raise `limits.final` for a launcher that restarts its workers with a long silent startup.
- `controller` (Standard tier; Minimal has none): where the SkyPilot API server runs, `mode`: `self-hosted` (a control host the user runs), `existing` (a server someone else runs, such as a team's), `provider-managed` (a cloud provider's hosted server) or `local` (on the operator's machine; managed jobs then get their own controller VM); the validator also accepts the older names. `state_uri` (required for `self-hosted`) describes persistent control state; a bare URI does not prove a backup exists or is restorable. Optional `endpoint` (the server's host and port only: no scheme, user, password or query) and `server_version` (as `sky api info` reports it; `--locked` compares it, so a server upgrade reruns the start checks).
- `approval`: status, scope and `mode` (the interaction level asked at intake: `automatic`, `gated` or `steps`; working rules). Never use these fields to bypass the executing agent's permissions or substitute for user approval.

## What to record before launch
Compare providers before launch. Record the selected resource offer, quote time/currency, node configuration, image digest, data identity, durable checkpoint URI, controller mode, permitted recovery range, budget, timeout, and owned-resource inventory. A manifest or a value `approved` is evidence bookkeeping, never proof of authorization by itself.

## State and locks
Create a read-only run-lock snapshot from the approved launch contract after the provider has been selected, before the full run. Record its hash and reference in the run journal. The helper compares critical fields to a supplied snapshot; it is not a tamper-resistant authorization system. Keep the provider invariant throughout this run. If a user explicitly decides to move providers, close/abort this run safely and plan a separately approved successor run with provenance.

Compare region when the lock disallows region changes, GPU count/model, image reference/digest, data URI/version and checkpoint identity. A scientifically intentional configuration change, or a code fix during a run, is a new run revision (a new run id, starting again from the intake) requiring review; do not hide it inside auto-recovery.

## Adjacent artifacts
What the runbooks actually produce, in the project and on the control host (`~/campaign/<run_id>`):
- the provider research record and the quote (`compare_costs.py` input), with timestamps;
- `run-lock.json` (a copy of the approved contract; no secrets);
- `campaign.json`, `guard/` (configs and ledgers), `tasks/` (rendered tasks), `qualification.json` (the qualification task's result and the stack tuple it proves), `evidence/` (logs, verification output, inventory); a new run revision (new run id) gets its own directory.
Record resource IDs you create by hand (VMs, buckets, registries) in the provider record with their owner and retention.

The project files above hold no secrets and belong in version control. Before the first run, check the project's `.gitignore` covers what training and testing leave behind locally: secret files (`*.env`, `secrets.env`), checkpoints, datasets and caches, the metrics tool's local folders (`wandb/`, `mlruns/`, `lightning_logs/`) and logs. Add what is missing (ask the user first). SkyPilot reads the same `.gitignore` when it uploads the project as `--workdir`, unless the project has a `.skyignore`, which then replaces it.

Unknown inventory means reconcile read-only first. Do not recreate an apparently missing job until its actual provider status is resolved, to avoid duplicate paid training.

## Launch-phase fields
`--phase launch` requires the fields below, plus the tier rules (Standard: managed recovery with positive attempts and a spot capacity-wait bound; Minimal: the timeout/autodown cost bound); passing checks still grants no authorization.
- **Checkpoint storage:** `checkpoint.provider` equals the selected GPU provider; `region` identifies the actual persistent service location (same region preferred); `durable=true` records independently verified lifetime; `storage_type` is `object-store` or `retained-volume`; `resume_policy=latest-complete` authorizes automatic selection within the declared recovery policy, never cloud spending or fresh start. Cross-region storage requires explicit locality/cost review. These declarations are not proof of provider behavior.
- **`budget.spot_price_cap`** (optional, spot only): `per_gpu_hour` (the cap, in the budget's currency) and `cap_id` (the provider's object that carries it, such as a pricing-policy ID, created before the run). How the worker applies it, and why it never raises the spend bound: lifecycle `references/provider-hooks.md#optional-spot-price-cap`.
- **`monitoring.notify`:** `channel` (where notices go, described in words; required at launch) and `format` (`text`, `slack`, `discord` or `json`, default `text`); the URL itself is the `NOTIFY_URL` secret of the launch's secrets file, never in this file.
- **`monitoring.guard`:** `none` (default: the worker enforces the run's limits) or `external` (also attach the optional campaign guard; what it adds: `gpu-training-observe` → `references/guard.md#campaign-guard`).
- **Metrics tooling** is the researcher's choice. Launch requires it to be explicit: either `monitoring.wandb` with `enabled=true`, or `monitoring.metrics_backend` naming the tool (for example `mlflow`, `tensorboard`, or `none`).
  - `monitoring.wandb`: `enabled=true`, deployment-resolved `entity`, `project`, stable `run_id`, `mode=online` or explicitly selected `offline`, and the user-chosen `history_policy` (`append` default, or `rewind`; see the observe skill's W&B reference). Store no API key or credentials.
  - The application initializes the W&B run only once and requires that established run on later attempts; metrics and checkpoints have separate resume semantics. Lock monitoring identity with the run.
  - Preserve unknown fields, but reject secret-bearing monitoring keys; use an approved runtime secret channel.

Store qualification evidence separately: image digest, framework/kernel versions, GPU/topology/driver range, data/checkpoint adapters and durable storage semantics. Reuse it while that compatibility tuple remains valid; full paid tests are not per-run admission checks.
