# Entrypoint contract

The training image's entrypoint is project code. The skills ship no template or library for it: library
APIs change with the project's pins. This page is the contract the runbooks and helpers rely on, with
framework-neutral pseudo-code. Write it once per project. On CPU check only what runs in seconds: the argv
parsed by the framework's own parser, the imports, pure functions. Fresh start, resume, stop and finalize
are proven on the GPUs by the qualification task (or the optional CPU rehearsal) against the real bucket
before the main run; a CPU pass never qualifies a multi-process or GPU fix.

## Inputs
The rendered task (`gpu-training-launch` → `scripts/render_job.py`) sets:

| Variable | Meaning |
|---|---|
| `EXPERIMENT_RUN_ID` | stable run identity |
| `CHECKPOINT_DIR` | the run's prefix on the checkpoint bucket mount (`/checkpoints/...`); qualification runs get `/checkpoints/qualify/<run>/<variant>-<tag>` |
| `DATASET_DIR`, `DATASET_URI`, `DATASET_VERSION` | the prepared, immutable dataset (mount or URI to stream from); for `qualify` runs the qualification subset when the contract has one (`dataset.qualify_uri`) |
| `RUN_MODE` | `train`, `qualify` (the real GPUs and world size on the qualification subset, run twice: fresh to its second save, then resume to the end) or `rehearsal` (the optional CPU fallback) |
| `IMAGE_DIGEST`, `GPU_COUNT` | the image digest and the GPU count from the contract (an image cannot know its own digest): identity fields of every manifest and the preflight journal |
| `CAMPAIGN_DEADLINE_UTC` | optional absolute deadline (train only; absent by default: the budget is the hard stop) |
| `CAMPAIGN_SUBMITTED_AT` (the run's first submission), `BUDGET_LIMIT_USD`, `BUDGET_RESERVED_USD`, `CEILING_HOURLY_USD`, `FREE_WITHOUT_VM`, `STARTUP_ALLOWANCE_MINUTES`, `MAX_ATTEMPTS_WITHOUT_PROGRESS`, `MAX_ATTEMPTS` | the run's limits (train only), read by `checkpoint_marker.py limits --from-env` at every start (below); no secrets |
| `PRICE_CAP_ID` | optional (`budget.spot_price_cap.cap_id`): the provider's spot price cap for `hook_price_cap` (provider hooks: optional spot price cap) |
| `NOTIFY_URL`, `NOTIFY_FORMAT` | the notification channel: `NOTIFY_URL` is a required secret (in `SECRET_NAMES`) whenever the contract names a channel, so the wrapper blocks when it is missing; `notify.py` reads both |
| `SECRET_NAMES` | secrets the launch supplies as environment variables (the `SKYPILOT_DOCKER_*` ones are consumed by SkyPilot for the image pull) |
| `WANDB_*` or `METRICS_BACKEND` | the researcher's metrics choice; qualification runs get their own metrics run id |
| `PURCHASE_MODE` | `on-demand-fallback` only in the guard's approved fallback task |
| `TQDM_DISABLE` | `1`: progress bars (which rewrite one line with `\r`) stay out of the job log; log plain lines |
| `HEARTBEAT_DIR` | where `progress_watch.tick()` writes each rank's heartbeat for the stall watchdog that wraps the launcher (the watchdog's own settings, `STALL_*`, are not the entrypoint's concern) |

**A finite collective timeout in argv:** set the framework's own collective timeout. Find the setting in the
framework's docs; some have none; PyTorch: [pytorch.md](pytorch.md). Main run:
max(600 s, 2 x the longest wait of one rank for the others, such as a rank-0 save). Qualification: max(300 s,
2 x that wait, estimated from the checkpoint size and the disk's write rate). A mismatched or stuck collective
then fails with a traceback instead of hanging; the stall watchdog below catches everything else. Download
models and data before the launcher starts the ranks, so no rank waits inside a collective for another rank's
download. The rendered task sets `PYTHONFAULTHANDLER=1`, so a rank killed by a fatal signal prints its Python
stack; the framework's own hang diagnosis is in its reference (PyTorch: [pytorch.md](pytorch.md)).

**Progress events for the stall watchdog** (observe skill: `scripts/progress_watch.py`, vendored at
`/opt/gpu-skills/`). The job stalls only when no rank ticked and its processes moved no bytes for longer than
the quiet limit of the phase in force (`gpu-training-observe` → `references/guard.md#stall-watchdog`). Every
rank calls `progress_watch.tick(phase, count, step, rank)` at the events E1-E4:

| Event | Phase | When | |
|---|---|---|---|
| E1 | `train` | the end of every optimizer step; also ends an evaluation or a save | required |
| E2 | `eval` | an evaluation begins (count 0) and every eval batch ends | recommended when the project evaluates |
| E3 | `save` | a save begins | optional |
| E4 | `final` | end-of-training work begins (the last export, a final evaluation, ranks waiting for rank 0) | recommended when that work writes a large export |

Rules: pass Python ints (a tensor is recorded as -1, never converted: `int()` of a GPU tensor waits for the
device). Tick from hooks the loop runs, never from a timer or a thread that keeps going while the loop is
stuck. Tick `save` only from a hook that fires before the write; a hook that fires after it ticks `train`. Work
between two steps without its own event (an evaluation without E2, RL rollouts, epoch-start work) is one long
`train` gap: the qualification measures it and the recommended limits cover it. Before the first tick the run
is in `startup` (30 minutes of silence allowed).

At decision time, find in the framework's docs or source and record in the project: the hook for each event
and how it is attached without editing the training code (a config entry, a plugin or entry point, an
environment variable, or the entrypoint's wrapper); whether the save hook fires before or after the write, and
on which ranks; what runs after the last eval batch (a gather, a metric computation). Then use the first tier
that applies:
1. The framework's callbacks tick, from the entrypoint's wrapper or callback, never from edits to the training
   code.
2. A script without hooks gets E1 from a global step hook of its framework. The launcher starts a helper in
   each process in place of the script; the script is not edited. PyTorch: rank mode, [pytorch.md](pytorch.md).
3. Otherwise set `watchdog.enabled: false` in the contract and tell the user: only the collective timeout and
   the budget then end a hang.
Every rank registers `faulthandler.register(signal.SIGUSR2, all_threads=True)` (the tier-2 helper does it), so a
stall puts every rank's stack in the job log.

**Everything tunable comes from argv** (or a config file the task mounts), never from the image: per-device
batch, gradient accumulation, data-loader workers, steps or epochs, data subset, evaluation and checkpoint
intervals. An out-of-memory error or a slow loader is then fixed by changing argv and repeating the
qualification task, never by rebuilding the image. Keep the effective batch (per-device batch x accumulation x world size) when
lowering the per-device batch for memory. These values are part of `config_sha256`, so settle them in the
qualification: after the first commit, a changed config blocks the resume by design (a new run revision,
approved by the user), it never silently continues with different hyperparameters.

## Behaviour by RUN_MODE
- `train`: the full run below.
- `qualify`: the real image, launcher, world size, precision and per-device batch, on the qualification subset.
  Its argv is the real argv plus the framework's own settings for the step count and the save and evaluation
  intervals, never a debug mode that turns saving off. Default shape: save (and evaluate, when the project
  evaluates) every `S` steps and train `3S + 2`, with `S` = 6 + `L`. `L` is the loader's look-ahead on one
  rank: its background workers x the batches each prefetches (an auto-tuned buffer: its upper bound, or set a fixed prefetch for the qualification; 0
  without background loading, which gives 20 steps). The loader fills `L` batches during startup and again
  during every save and evaluation, so the first `L` steps after each run on ready batches and show only the
  GPU's speed; the last 6 steps before each save run at the data's real pace. `L` counts batches and `S` counts
  steps: with gradient accumulation one step uses several batches, so `S` is more than enough. Size the subset so the `3S + 2` steps
  cross one epoch boundary: epoch-start work then shows in the measured gaps.
  The task runs the entrypoint twice and checks the journal (`checkpoint_marker.py journal-check`):
  - start 1 (fresh) stops after its second commit (steps `S` and `2S`; the second save shows the steady
    state): journal `finish`, exit 0;
  - start 2 resumes from step `2S`, saves at `3S`, trains to `3S + 2`, saves and runs `finalize` (`completed`).
  Shuffling is off in this mode, and the first train batch holds the subset's largest samples by the
  project's cost driver (tokens, pixels after resize, boxes per image), so peak memory shows in step 1.
  When the project evaluates, the eval set has `N·b + 1` samples (N ranks, b per-device eval batch): every
  way of sharding it shows its uneven last batch, where a mismatched gather hangs. Start 1 evaluates twice
  (some frameworks change how they gather after the first evaluation).
  Rank 0 journals `save_seconds` (how long that save blocked training) and `checkpoint_gb` with every
  `commit`, and with `finish`: `peak_memory_fraction` (highest rank, of the device total), `startup_seconds`
  (entrypoint start to the first step), `step_seconds` (mean wall time per train step over start 1's last 6 steps
  before each save, steps `L + 1` to `S` and `S + L + 1` to `2S`; not one step),
  `eval_seconds` (one evaluation of the subset's eval set, when the project evaluates) and
  `loader_wait_fraction` (share of step time spent waiting for data, over the same 12 steps; for `MOUNT` data
  also `read_mb_s`).
  Above 0.9 memory the task fails: lower the per-device batch in argv. The numbers size the save interval,
  the disk and the startup allowance (launch runbook).
- `rehearsal`: the optional CPU fallback, the same two starts on a CPU VM: one process, GPU-only options
  (precision, fused kernels) dropped, a small batch, no GPU probe. It proves storage and resume wiring only
  and has its own config hash.

## Exit codes (what triage reads)
`0` completed, or stopped by a limit (FINAL.json for completion and the deadline; a journal `limit` event for the
budget, allocations without progress and allocations): exit 0 is the only code that ends a managed job without a
restart - `3` blocked: restore, identity, storage,
data manifest or credentials; never start fresh - `4` job-start preflight failed (image, driver, host) - `93`
the stall watchdog stopped a hung run (never in `recover_on_exit_codes`: a hang is not repaired by a
restart) - `124` the runtime timeout - anything else: an application failure.
A launcher that starts several worker processes reports its own exit code, not a worker's. So the one-time
checks run in one process per node before any launcher (the wrapper below), and their exits 3 and 4 reach
SkyPilot unchanged. A failure inside a rank (a restore that loads with missing keys, or a check a project keeps in
its ranks) arrives as the launcher's code; the `blocked: ` or preflight `"ok": false` line printed before it
names the cause.

## Pseudo-code
```text
wrapper(argv):                                    # one process per node, the task's argv, under the watchdog
    attempt = utc_timestamp()                     # letters/digits, once per worker start
    run = env.CHECKPOINT_DIR
    if a non-SKYPILOT_DOCKER_ name in SECRET_NAMES is unset: print("blocked: ..."); exit 3
    if the dataset manifest is missing under DATASET_DIR (or DATASET_URI): print("blocked: ..."); exit 3   # an empty prefix mounts nothing
    if RUN_MODE != rehearsal:
        report = gpu_preflight.probe(expected_gpus, expected_platform, min_free_gb=disk budget)   # as `gpu_preflight.py probe` does: print the JSON; exit 4 when not ok
        journal(run, attempt, "preflight", report + identity)   # the probe JSON as printed (GPU name and driver are per device, in report.devices[]); image digest, dataset version, world size
    if RUN_MODE == train and FINAL.json exists under run:   # completed or stopped at the deadline: final
        print("FINAL.json exists (outcome ...): nothing to train"); exit 0
    limits = none
    if RUN_MODE == train and BUDGET_LIMIT_USD is set:   # the run's limits (Standard tier); no outside guard is needed
        limits = `checkpoint_marker.py limits $CHECKPOINT_DIR --from-env`   # exit 0: keep going, 5: a limit now
        if its exit code is neither 0 nor 5: print("blocked: limits unreadable ..."); exit 3   # never train unbounded
        journal(run, attempt, "limits", **limits)  # the finish and the status checks read the spend bound here
        if limits.limit:                          # reached now: stop without training, exit 0 (never a restart)
            journal(run, attempt, "limit", reached=limits.limit)
            if limits.limit == "deadline": try finalize(run, identity, outcome="deadline_stopped") except Blocked: pass
            notify(f"stopped: {limits.limit} (spend <= {limits.spend_upper_usd} USD, an upper bound)", "stopped"); exit 0
            # budget, no_progress, attempts write no FINAL.json: a relaunch with a raised limit resumes this run
        env.CAMPAIGN_STOP_UTC, env.CAMPAIGN_STOP_BY = limits.stop_at_utc, limits.stop_by   # budget or deadline
    elif RUN_MODE == train and CAMPAIGN_DEADLINE_UTC is set:   # Minimal tier: the deadline only
        env.CAMPAIGN_STOP_UTC, env.CAMPAIGN_STOP_BY = CAMPAIGN_DEADLINE_UTC, "deadline"
    if RUN_MODE == train and PRICE_CAP_ID is set:  # optional (offline only): each new VM caps its own spot price
        cap = hook_price_cap(PRICE_CAP_ID)        # provider hooks; needs the provider CLI and credentials in the image
        if cap failed: journal(run, attempt, "price_cap", state="failed"); notify("spot price cap failed: running uncapped", "price_cap")
        if cap == "switched":                     # put under the cap and asked to stop: SkyPilot restarts this VM
            journal(run, attempt, "price_cap", state="switched"); notify("spot price cap set; restarting this VM", "price_cap")
            wait up to 10 minutes for the stop    # never exit 0 here: that would end the job
            if hook_price_cap(PRICE_CAP_ID) != "capped": print("blocked: the VM did not stop under the cap"); exit 3
    selection = checkpoint_marker.select(run, identity, mount_root="/checkpoints")   # Blocked: print("blocked: ..."); exit 3
    journal(run, attempt, "selection", action=selection.action, step=selection.global_step)
    if RUN_MODE == train:
        notify(f"started fresh" if selection.action == "fresh" else f"resumed from step {selection.global_step}"
               + (f", allocation {limits.allocations}, spend <= {limits.spend_upper_usd} USD" if limits else ""), "started")
    env.CHECKPOINT_ATTEMPT, env.CHECKPOINT_SELECTION = attempt, json(selection)   # handed to every rank
    exec the launcher with the training argv      # exactly one (or the training itself with one process per node);
                                                  # exec, so the watchdog's TERM reaches it directly

main():                                           # every rank, started by the launcher
    faulthandler.register(SIGUSR2, all_threads=True)    # the watchdog's stack dump on a stall
    attempt, selection, run = env.CHECKPOINT_ATTEMPT, json(env.CHECKPOINT_SELECTION), env.CHECKPOINT_DIR
    state = framework.restore(selection.path) if selection.action == "resume" else framework.init()
    a load that reports missing or unexpected keys is a restore failure: print("blocked: ..."); exit 3
    check restored counters, optimizer/scheduler state and every rank's data/RNG position before any update
    if RUN_MODE != train: no shuffling; the subset's largest samples first

    for each optimizer update:
        train_one_update(state)
        progress_watch.tick("train", count=step, step=step, rank=rank)
        stop_now = RUN_MODE != train and selection.action == "fresh" and commits_this_start == 2
        past_deadline = RUN_MODE == train and CAMPAIGN_STOP_UTC is set and now >= CAMPAIGN_STOP_UTC   # budget runway or deadline
        if checkpoint_due or step == last or stop_now or past_deadline:   # the last step is always saved
            if free_disk(home) < 1.1 * last_checkpoint_size + 10 GB:   # the mount cache lives on the boot disk
                rank0: journal(run, attempt, "save_skipped", reason="disk")
                if not past_deadline: continue        # at a stop, stop anyway: the newest commit is the result
            else:
                progress_watch.tick("save", step=step, rank=rank)
                all ranks: framework.save(step_dir(run, step, attempt) / "payload")   # create parents first
                barrier()                                                            # every file closed
                rank0: checkpoint_marker.commit(step_dir, fields, files)    # prints CHECKPOINT_COMMITTED {...}
                rank0: journal(run, attempt, "commit", step=step, save_seconds=..., checkpoint_gb=...)
                rank0, first commit of this start: notify(f"checkpoint at step {step} committed", "commit")
                rank0: copy outputs the user needs at finish that changed (best model) to run/final/...
                progress_watch.tick("train", count=step, step=step, rank=rank)      # the save is done
        if evaluation_due:                                      # all ranks evaluate (none: see Notes)
            metrics = evaluate(state, on_batch=lambda i: progress_watch.tick("eval", count=i, step=step, rank=rank))
            metrics += {"train_loss": recent train loss}
            rank0: print("EVAL step=<n> " + json(metrics))       # one flushed line: the guard and the waits read it
            rank0: notify(f"eval step {step}: " + short summary of metrics, "eval")
            rank0: journal(run, attempt, "eval", step=step, **metrics)   # the journal survives attempts
            progress_watch.tick("train", count=step, step=step, rank=rank)      # the evaluation is done
        if stop_now:
            rank0: journal(run, attempt, "finish", peak_memory_fraction=..., startup_seconds=..., ...); exit 0
        if past_deadline:
            progress_watch.tick("final", step=step, rank=rank)
            if CAMPAIGN_STOP_BY == "deadline": rank0: checkpoint_marker.finalize(run, identity, outcome="deadline_stopped", artifacts=...)
            else: rank0: journal(run, attempt, "limit", reached="budget", step=step)   # not final: the run can go on
            rank0: notify(f"stopped at the {CAMPAIGN_STOP_BY}, step {step}", "stopped"); exit 0

    progress_watch.tick("final", step=last, rank=rank)           # end-of-training work: its own quiet limit
    rank0: checkpoint_marker.finalize(run, identity, outcome="completed", target_step=last, artifacts=[final/...])
    rank0: journal(run, attempt, "finish"); notify(f"completed at step {last}", "completed"); exit 0
```
`notify(text, event)` runs `timeout 20 python3 /opt/gpu-skills/notify.py --event <event> <text>` (best effort; it
never fails or holds up the run).
Only completion and the deadline write FINAL.json. A budget, no-progress or attempt stop leaves a journal `limit`
event: after the user approves a raised limit, a relaunch under the same run id resumes from the newest checkpoint.

Notes:
- Object stores have no empty directories: every write under `CHECKPOINT_DIR` creates its parents first.
- Checkpoints and the boot disk: the mount writes to a cache on the boot disk and uploads in the
  background; SkyPilot evicts only uploaded files. The free-space check above keeps a slow upload from
  filling the disk (a skipped save costs progress, a full disk fails the run). Never delete checkpoints
  through the mount from the worker.
- `journal` also prints each record as a `CHECKPOINT_JOURNAL {json}` line, so the job log shows the
  preflight, selection and commits that the observe runbook checks; `commit` prints `CHECKPOINT_COMMITTED`.
- The wrapper (preflight, selection) and rank 0 (commits, finish) write the journal; rank 0 commits; every
  rank saves its own state. Multi-process launchers can rebuild the training object in each child from the
  parent's pickled state (including registered callbacks): register project hooks only if absent, and let
  the qualification task exercise the real launcher.
- Metrics: rank 0 only, the stable run id from the environment; never decide resume from metrics history.
  A project without an evaluation still prints the `EVAL step=<n> {...}` line at the planned points, with its
  training metrics (loss, learning rate, throughput): the guard forwards it and the observe waits read it.
- The stdlib helpers (the list: `gpu-training-image` SKILL.md step 6) are vendored into the image at
  `/opt/gpu-skills/` (the rendered `run:` starts `/opt/gpu-skills/progress_watch.py`). The qualification runs those
  copies, so it proves what the main run will use.
