# W&B continuation without changing recovery truth

Checked against official documentation on 2026-10-01. Adapt these patterns to the project's pinned SDK/framework; they are integration templates, not a deployed training runtime.

## Identity and authority

- Install and pin W&B in the reusable image lock. Set entity, project, stable run ID and mode in deployment configuration. Preserve that identity in the durable campaign ledger across attempts; attempt ID is a separate field.
- Use only an already approved secret-delivery mechanism for credentials. Never bake credentials or per-deployment identity into the image, print the API key, pass it on the CLI or put it in experiment.json. Enabling telemetry needs approval for its destination and payload; a manifest does not grant it.
- Run initialization/logging on global rank zero only. Perform any collective metric reductions on all required ranks before rank zero logs. Fence overlapping attempts before allowing another writer to the same W&B ID.
- Restore training exclusively from the latest verified complete checkpoint in the approved durable store on the selected training provider, preferably in the same region. W&B does not restore model/optimizer/scheduler/scaler/RNG/loader state and is not the checkpoint store.

## Resume semantics and append-only history

Use explicit entity/project/ID. `resume="allow"` resumes an existing ID or creates it if absent; use it only for the declared first initialization. Once successful initialization is durably recorded, use `resume="must"`, which errors if the run is missing. Reconcile ambiguous first-init results with the same ID rather than minting another. If the established run disappears, stop online logging and surface the discrepancy; do not silently recreate it. `auto` depends on the prior working directory/filesystem and is unsuitable as the replacement-worker contract. [W&B resume semantics](https://docs.wandb.ai/models/runs/resuming)

Let the resumed SDK own its monotonically increasing history counter: omit the `step=` argument and use one committed payload per logging event. Store the checkpoint's scientific optimizer update count as `training/global_step`; use `define_metric(..., step_metric="training/global_step")` for scientific plots. Never pass restored global_step as the W&B history step, reset the SDK counter from a checkpoint, or send past history steps. [Run.log and define_metric](https://docs.wandb.ai/ref/python/experiments/run/)

Default history-ahead policy: append a new attempt segment to the same run. Record attempt ID, restored checkpoint step/identity and scientific step with every event. If checkpoint step 100 resumes after step 120 was already logged, subsequent scientific steps may repeat 101–120 while W&B history keeps increasing. Mark those records as replay where the previous high-water mark is known; otherwise mark replay status unknown. Retain earlier records, distinguish attempts in analysis, and count committed scientific progress separately from recomputation. Do not claim ordinary `resume` rewinds or overwrites the earlier samples.

Explicit history rewriting is a separate, non-default operation. W&B documents `resume_from` rewind with SDK >=0.17.1, feature-access limitations and restrictions for non-monotonic custom axes. Do not enable it automatically; check current support and obtain approval before mutating history. [W&B rewind limitations](https://docs.wandb.ai/models/runs/rewind)

## Project logging adapter

Illustrative online-only, rank-zero setup; `runtime` is reviewed non-secret deployment config, and `established` comes from reconciled durable campaign state, not from whether a checkpoint exists. The controller must persist successful initialization and fence writers. The guard rejects offline/missing mode before importing or initializing the SDK. Offline mode needs a separately implemented and qualified adapter; this example never silently converts it to online. No actual W&B call is authorized by this example.

```python
def open_metrics_run(runtime, established):
    if runtime.get("mode") != "online":
        raise ValueError("This adapter supports online mode only; qualify a separate offline adapter")
    import wandb  # already pinned in the image
    run = wandb.init(
        entity=runtime["entity"], project=runtime["project"],
        id=runtime["run_id"], mode="online",
        resume="must" if established else "allow",
        settings=wandb.Settings(
            disable_code=True, disable_git=True,
            save_code=False, console="off",
        ),
    )
    run.define_metric("training/global_step")
    run.define_metric("training/loss", step_metric="training/global_step")
    return run

def log_metrics(run, global_step, loss, attempt_id, restored_step):
    run.log({
        "training/global_step": global_step,
        "training/loss": loss,
        "recovery/attempt_id": attempt_id,
        "recovery/restored_step": restored_step,
    }, commit=True)  # no explicit history step
```

Add only allowlisted metrics/configuration, a checkpoint identity safe to disclose, and replay status from the project ledger. Rate-limit logs, aggregate related scalars in one payload, and bound shutdown flushing. Keep one logging owner: adapt the existing Trainer/Accelerate callback or replace its W&B emission path, never install a second competing callback. Verify callback-provided config and media/artifact behavior against the pinned framework.

## Privacy, outages and qualification

Set `WANDB_LOG_MODEL=false`, `WANDB_WATCH=false`, and `WANDB_DISABLE_CODE=true`; preserve the SDK privacy settings above. Do not call artifact/model/code upload, `save`, `watch`, or sample/media logging without specific scope. Disable other integrations that automatically upload datasets, checkpoints, private source, arbitrary config or raw console logs. Keep the W&B working directory separate from datasets/checkpoints/secrets. Inspect generated telemetry during qualification; flags cannot prevent custom upload code. [HF callback controls](https://huggingface.co/docs/transformers/v5.4.0/en/main_classes/callback#transformers.integrations.WandbCallback), [code privacy](https://docs.wandb.ai/support/models/articles/how-do-i-log-metrics-without-saving-code), [SDK settings](https://docs.wandb.ai/models/ref/python/experiments/settings)

Choose a deterministic outage policy before launch: a bounded local telemetry buffer with approved continuation, or a bounded failure/shutdown path if online monitoring is required. Missing W&B access must not reset training or invalidate committed checkpoints. Do not silently switch to offline mode, create a new run, or sync multiple old attempt directories into an online run. Offline segments need explicit identity/order reconciliation before any later sync; record any unrecoverable telemetry gap.

Qualify the reusable stack once: create the approved test run, commit checkpoint N, log beyond N, interrupt, restart a fresh process from N with the same run ID, and verify appended history, repeated scientific steps labeled by attempt, no second writer, no unauthorized files, and unchanged checkpoint truth. Include missing-run and network-outage behavior. Requalify after major SDK/framework/adapter changes; each normal attempt needs only quick identity, runtime-secret, connectivity and restore-readiness checks. Tests that create or upload to W&B require their own approved test destination/payload. A dry-run/mock proves adapter logic only, not SDK/server resume behavior. A dashboard heartbeat alone is not a recovery test.

## History policy: the user chooses
Ask the user during planning which behaviour they want after an interruption, explain the trade-off, and record it as `monitoring.wandb.history_policy`. It is locked with the run; changing it is a new review.

- **`append` (default):** the resumed attempt attaches with `resume="must"` and appends. Steps between the last checkpoint and the interruption appear twice in history; plot against the scientific step and filter on the attempt and replay fields. Nothing is ever deleted.
- **`rewind`:** the resumed attempt calls `wandb.init(..., resume_from=f"{run_id}?_step={N}")`, where N is the W&B history step recorded in the restored checkpoint's manifest. History after N is replaced by the resumed attempt's rows. It rewrites history, needs SDK >= 0.17.1 and W&B's rewind feature for the account, and has documented limits for custom step axes. Confirm availability and whether step N itself is kept during the stack's first qualification. If the account cannot rewind, fail at start-up with a clear message; do not silently fall back to append.

Both policies need the checkpoint publisher to record, in the manifest, the W&B history step at commit time (`monitoring.wandb_history_step`, the SDK's next history index when the checkpoint was committed). Without that field, `rewind` cannot be exact and must not be attempted.

