Research checked: 2026-10-01 UTC. Design guidance; verify APIs against the project’s pinned versions.

Contents: primary-source findings; checkpoint content and boundaries; crash-safe publication on a bucket mount (checkpoint size, path and save interval); fail-closed restore and retry; compatibility limits; offline acceptance tests; resume qualification; worker replacement.

## Primary-source findings that constrain the design

- **Frameworks:** PyTorch-based stacks: `gpu-training-lifecycle` → `references/pytorch.md#checkpoints-and-restore`; streaming loaders: `gpu-training-data` → `references/contract.md#3-exactly-one-owner-of-data-advancement`.
- **Object publication:** object stores update single keys atomically, not multi-file checkpoints, so a completeness marker is application logic. [S3 consistency](https://docs.aws.amazon.com/AmazonS3/latest/userguide/Welcome.html#ConsistencyModel)
- **SkyPilot bucket mounts:** SkyPilot recommends checkpointing to a bucket mounted with `MOUNT_CACHED` (or `MOUNT`), or to a volume. In `MOUNT_CACHED`, `close()` does not mean the file is in the bucket, and SkyPilot waits for uploads only when a task completes, not on preemption (it appends that wait to the task's `run:` script, so the rendered `run:` never `exec`s the training command). Uploads run in parallel (the rendered mount uses SkyPilot's `MODEL_CHECKPOINT_RW` preset, several transfers at once) and rclone re-queues a failed upload behind later files, so upload order is not guaranteed. `sky jobs cancel --graceful` waits for queued uploads; a plain cancel and a preemption do not. `MOUNT` and `MOUNT_CACHED` accept only a bucket root as the source. A newest checkpoint directory can therefore be partly uploaded after preemption; a marker written last plus verification on restore is what makes completeness checkable. [Managed jobs: checkpointing](https://docs.skypilot.ai/en/latest/examples/managed-jobs.html), [Storage modes](https://docs.skypilot.ai/en/latest/reference/storage.html)

## 2. Training checkpoint content and boundaries

Required logical state (files are backend-specific):
- Model parameters/buffers and exact model/config/base revision, including adapter weights where applicable
- Optimizer state, parameter groups, master weights and sharded optimizer partitions where applicable
- Scheduler state and step convention; mixed-precision loss scaler where used, explicitly marked not applicable otherwise
- Global optimizer step, epoch, consumed examples/tokens, accumulation configuration
- Python, NumPy, framework CPU and per-device GPU RNG states for every training rank; dataset/worker RNG handled by the data adapter
- Rank-specific data-loader/sampler/cursor/packing state, or complete deterministic replay metadata
- Backend/parallelism config, world size, code/config/image/dataset hashes, framework/library versions, precision, GPU model/compute capability, relevant deterministic/kernel settings
- Stateful callbacks, early stopping, best metric/checkpoint identity, EMA or custom algorithm state when used

Prefer checkpoints immediately after a completed optimizer update and scheduler update with accumulation phase zero. Snapshot model and data at the same logical boundary. Mid-accumulation recovery would also need gradients and precise partial-microbatch state; keep that out of a minimal adapter.

Use the framework's real resumable checkpoints and native (sharded) state APIs, and restore from an explicit verified path. A model-only export or a blindly unwrapped state dictionary is not a substitute. All distributed save coordination completes before publication; the uploader needs a consistent snapshot and rank agreement. Never design around a guessed callback ordering.

## 4. Crash-safe checkpoint publication on a SkyPilot bucket mount

Follow SkyPilot's checkpoint pattern (framework-native checkpoints in a mounted bucket, resume from the latest) and add one marker so "latest" means "complete". The bundled `scripts/checkpoint_marker.py` (stdlib only, vendored into the image like the verifier) implements the commit and the selection.

Layout under the run's checkpoint prefix. `checkpoint.uri` is `<bucket>/<prefix>`; the rendered task mounts the bucket at `/checkpoints` with `MOUNT_CACHED` (SkyPilot's `MODEL_CHECKPOINT_RW` preset) or `MOUNT`, and sets `CHECKPOINT_DIR=/checkpoints/<prefix>`:
```
<step, 12 digits>-<attempt>/payload/...    framework-native files, written once
<step, 12 digits>-<attempt>/manifest.json  sizes, SHA-256, roles, identities
<step, 12 digits>-<attempt>/COMMITTED.json written last
journal/<attempt>/<seq>-<event>.json       per-attempt events, one write-once file each
alive/<start>-<host>.json                  per allocation, rewritten by the stall watchdog each progress period
FINAL.json                                 written once at the end by `finalize`
```

`<attempt>` is a tag the container entrypoint sets once per worker start (for example `CHECKPOINT_ATTEMPT=$(date -u +%Y%m%dT%H%M%SZ)`) and passes to every rank. A replacement that re-trains to a step its predecessor saved but never committed writes a new directory instead of reusing a partial one; when the same step is committed twice, the newest attempt wins.

1. Object stores have no empty directories: a new run's prefix, and any subdirectory, exists only after its first file is written, so **every** write under `CHECKPOINT_DIR` (checkpoints, markers of the training code, metrics-tool markers, final exports) creates its parents first (`mkdir -p`), and selection treats a missing prefix under a mounted `/checkpoints` as "nothing committed". At a coordinated update boundary, every required rank saves into `<step>-<attempt>/payload/` (directly, or into a fast local directory that is then copied in). Wait for asynchronous saves; every file must be closed before the next step. Never rename on the mount and never write a checkpoint directory twice.
2. One rank calls `commit()` (or `checkpoint_marker.py commit`): it hashes the payload, writes `manifest.json`, verifies the directory with the verifier as if committed (identity, roles, rank coverage, no unlisted files), and only then creates `COMMITTED.json` and prints one `CHECKPOINT_COMMITTED {json}` line to the job log (run, step, directory, manifest hash), which operators wait for. A failed check leaves no marker, so the directory is never selected.
3. Uploads run in parallel and their order is not guaranteed, so the small marker can reach the bucket before its payload; restore verifies every file. A marker without its payload makes that checkpoint look damaged, and selection falls back to the previous one. The first checkpoint of a run has nothing to fall back to: selection then blocks (exit 3, "committed but none verifies"); a person checks in the bucket that its files are missing, deletes that directory from the control host and relaunches, which starts fresh. A stopped VM that SkyPilot restarts keeps its upload cache and resumes pending uploads, so its listing can briefly show a checkpoint that is not yet in the bucket; that checkpoint is complete on disk and safe to restore.
4. Never delete checkpoints from the training worker: `commit()` returns while uploads of the newest checkpoints may still be queued, and deleting an older directory through the mount removes it from the bucket immediately. Keep everything during the run, or prune from outside the worker with `checkpoint_marker.py prune-list` (the finish runbook's pruning; usable during a long run too), and at finish keep the final and referenced best checkpoints. Directories without a marker are interrupted saves; `prune-list` includes the old ones.

5. **Journal and final record.** The entrypoint's wrapper (preflight, selection) and rank 0 (commits, finish) record each worker start's events with `checkpoint_marker.py journal $CHECKPOINT_DIR --attempt $CHECKPOINT_ATTEMPT --event <preflight|selection|commit|deadline|finish> [--field k=v]`: `sky jobs logs` returns only the last attempt, so the journal is the durable lineage of every attempt (read it with `journal-show`). At the end, inside the worker after the last commit, rank 0 runs `finalize` with the outcome (`completed` with `--target-step`, `deadline_stopped`, `stopped`, or the limit that ended the run:
`budget_stopped`, `no_progress_stopped`, `attempts_stopped`) and `--artifact` for every extra file the user needs at finish (best model, exported metrics), which must already be on the mount: only the resume state is otherwise durable, so a best model kept on the worker's disk is lost when a replacement takes over. The finish stage checks the record with `verify-final` against the bucket.

6. **The run's limits, from inside the worker.** At each start, after the preflight is journaled, the wrapper runs
`checkpoint_marker.py limits $CHECKPOINT_DIR` with the rendered budget values (lifecycle `references/entrypoint.md#inputs`).
It reads the journal (allocations, and those in a row that ended without a commit) and the alive files (how long each
allocation ran), and prints the spend upper bound and `stop_at_utc`, the time the budget runway ends (or the deadline,
if earlier). Exit 5 means a limit is reached now: the wrapper finalizes with that limit's outcome without training
(or, with nothing committed yet, journals `limit` and exits 0). Otherwise it trains until `stop_at_utc` like a
deadline. Every stop exits 0, the only code that ends a managed job without a restart whatever
`max_restarts_on_errors` is. The bound counts waiting without a VM as free only with `--free-without-vm` and readable
alive files; an allocation lost before its watchdog started is not seen, so the optional campaign guard stays the
stricter outside count.

One writer: a SkyPilot managed job runs one worker at a time, so no fencing, attempt records or pointer files are needed. Setups with several writers can still have a coordinator accept each commit and use the verifier's `--accepted-record` check ([verifier](verifier.md#optional-coordinator-acceptance-record-multi-writer-setups-only)). Keep provider, region/access plan, GPU count and backend fixed for a run; replacement selection is not permission to migrate scientific conditions.

### Checkpoint size, path and save interval
The checkpoint mount (`MOUNT_CACHED`) writes each file to a cache on the boot disk and uploads it about a second after it is closed. SkyPilot caps the cache at 10 GB by default, but only uploaded files are evicted. The disk fills only when checkpoints are written faster than they upload, or one does not fit. Uploads are usually fast enough. Keep free disk of at least 1.25 x checkpoint + 10 GB on top of the image and data (`gpu-training-data` → `references/staging.md#disk-budget`); the entrypoint skips a save when space is short (entrypoint contract). When a checkpoint exceeds about half of the free disk, or saves come faster than uploads finish, switch the project to one of:
- **direct writes:** `checkpoint.mount_mode: MOUNT` (sequential writes; close() returns after the upload, so training waits for it) or the framework's own object-store writer;
- **local write and explicit upload:** write to local NVMe, upload with a copy tool or the framework's asynchronous uploader, delete the local copy only after the upload is verified (the commit and verification rules stay the same).

Choose the save interval by time, then convert it to steps with the measured step rate: at least 20 x the steady-state `save_seconds` (training blocked at most 5 %), and at least 1.2 x `upload_flush_seconds`, the time one checkpoint takes to upload (SkyPilot flushes queued uploads when a task ends and prints `cached mount upload complete (took <N>s)` in the qualification task's log). The qualification task measures both (`gpu-training-data` → `references/staging.md#measure-io-in-the-qualification-task`). Periodic durable saves stay essential when the provider gives no or too short SIGTERM notice. A checkpoint that cannot upload within any affordable interval needs a different checkpoint path (above) before the paid run.

## 5. Fail-closed restore and retry contract

Worker inputs: `gpu-training-lifecycle` → `references/entrypoint.md#inputs`; `select` checks the identities they carry.

Restore algorithm:
1. `checkpoint_marker.py select` lists the run's step directories on the mount, newest step first. Directories without `COMMITTED.json` are ignored; a folder name alone is not completeness. It verifies the newest committed candidate with all expected identities. An older candidate is tried only for positively missing/incomplete/checksum-corrupt state, recording the reason and lost steps. No marker at all means the run never committed progress and may start fresh under its declared policy; markers exist but none verifies: block, never start fresh. Identity or software mismatch, a missing mount, permission or I/O errors stop selection; a bounded retry may re-read the same candidate, never infer absence
2. The verified directory is read through the mount (MOUNT_CACHED fetches files on read, with a 10 GB cache by default). Copy it to local disk once and load from there; for large checkpoints raise the mount's `vfs_cache_max_size` so verification and loading do not download it twice
3. Build the identical model/optimizer/backend/loader objects; restore using that adapter's documented order, through the script's real restore interface (read its code; never invent a universal `--resume` flag)
4. Assert expected nonzero restored step, optimizer and scheduler counters, RNG/rank coverage, data fingerprint and accumulation phase; perform all-rank agreement
5. Report `RESTORE_VERIFIED(step, manifest_digest, attempt)` before the first training update; controller rejects a fresh-start report for a required resume
6. Publish future checkpoints by the same protocol; completion requires a committed final checkpoint and final run marker, not only process exit 0

A positively missing/incomplete/checksum-corrupt candidate may be skipped only under the declared `latest-complete` policy after the failure has been classified. Unsupported state, wrong dataset/topology or a semantic backend load failure (e.g. a load that returns no path) blocks the attempt; do not silently downgrade scientific compatibility. Never use `except: train_from_scratch()`. A transient network failure may retry the same restore with bounded backoff and then stop. No eligible verified checkpoint after expected progress means blocked recovery, not a new run. An incompatible config/code requires a new run/fork, not automatic mutation to make it load.

Before the first checkpoint exists, mark the run explicitly `UNINITIALIZED`; restarting from step zero is the same unprogressed run only under a declared policy. Once a checkpoint has committed, loss of access is not evidence that none existed.

Record logical run ID separately from attempt ID so logs remain joinable. Deduplicate metrics using run/step/event identity and keep attempt lineage; wall-clock retries can repeat work since the last commit. Estimate recoverable lost work from checkpoint age plus upload lag, not merely the local save interval. Checkpoint frequency must account for serialize/upload time and spot risk ([save interval](#checkpoint-size-path-and-save-interval)).

## 6. Compatibility and reproducibility limits

Safe default replacement tuple: same code, image digest, package lock, model/data revisions, backend and parallelism config, rank count, effective batch geometry, loader workers, sampler/packing algorithm and precision. Same GPU family is preferable for numerical comparisons; availability-driven family substitutions must satisfy runtime compatibility and be recorded as changed conditions.

Backend resharding or topology conversion does not remap rank RNG or arbitrary streaming cursors, and is no license to change topology without a tested path. Standardize a same-topology checkpoint for spot recovery and generate a separate portable model export at the end. Keep topology fixed by default, even where the backend supports resharding.

Frameworks do not guarantee identical results across releases/platforms or even CPU/GPU with the same seed (e.g. PyTorch: [Reproducibility](https://docs.pytorch.org/docs/2.14/notes/randomness.html)). Deterministic algorithms and seeded loaders reduce variability but can cost performance. Therefore acceptance has two levels: (A) state restoration and bounded lost work, required; (B) bitwise/deterministic continuation for a specified tested hardware/software/data tuple, optional and never broadly promised. Keep state recovery, exact sample continuity and bitwise reproducibility as separate claims.

## 8. Offline test plan and acceptance criteria

Adapter acceptance tests. A local directory stands in for the mounted bucket; CPU processes (e.g. Gloo) can exercise rank-aware contracts. They test the adapter, not SkyPilot's upload ordering, the provider's durability or GPU kernels; GPU acceptance is the qualification task. The helpers' own tests (commit, selection, verifier) are in the bundle repository, not installed. Items 3–5 test the data-position rules (`gpu-training-data` → `references/contract.md#3-exactly-one-owner-of-data-advancement`).

1. **Baseline interrupted continuation:** train a tiny deterministic dropout model on numbered samples for N updates; separately stop at K, persist complete state, instantiate a new process and resume. Compare weights, optimizer moments, scheduler/LR, RNG draws, sample IDs and counters at N
2. **Accumulation boundary:** interrupt each microstep. Only a completed update is eligible to commit; replay of uncommitted work produces the baseline result
3. **Streaming with workers:** 0 and 2 workers, shard rollover, partial batches and packing leftovers, epoch resets. Verify exact next sample sequence where the adapter promises it
4. **Double-skip trap:** save a nonzero cursor and deliberately enable framework skip; test must detect missing sample IDs and reject this configuration
5. **Shuffle limitation:** show that a stream whose shuffle buffer is not saved (e.g. HF streaming `.shuffle()`) diverges on resume; the exact-continuity capability flag cannot be set for that adapter without replacement buffering logic
6. **Compatibility:** change data/tokenizer/config/image/backend/world size/worker count. Reject unsupported resume before updates rather than adapting silently
7. **Disk/network failures:** insufficient scratch, permission errors, transient outage, checksum discrepancy and backend `None` return all produce structured failure, bounded retry where appropriate, never fresh training
8. **Log lineage:** replayed metrics retain step/attempt identity and do not masquerade as new unique optimizer progress
9. **Finalization:** process exit alone leaves run non-complete; FINAL becomes valid only after the final committed checkpoint verifies

Deliver a machine-readable resume-test report with tested software tuple, adapter capability (`stateful_recovery`, `exact_data_continuity`, `bitwise_continuation`), pass/fail evidence, and known exclusions. No installable skill should promise an untested capability.

## Resume qualification
Qualify resume once per stack (image, framework, checkpoint store, provider) with the launch skill's GPU qualification task: the entrypoint runs fresh, then resumes from its own commit, on the real GPUs and the real bucket mount. Offline CPU tests do not prove GPU/provider behavior. Requalify for material stack changes or regressions, not routine deployment. Normal attempts reuse the image and run quick identity/access/device checks plus the mandatory restore verification before updates; never skip restore integrity to save time.

## Worker replacement
A Managed Jobs recovery may restart the same stopped VM (providers that stop rather than delete preempted VMs) or create a new one; record instance IDs and report which happened. The worker boot disk is never checkpoint truth: SkyPilot deletes it when it falls back to a new VM, and on cancel/down.

Owned elsewhere:
- Placement, purchase mode, retry and budget bounds: lifecycle gates; recovery never changes them. Where checkpoint truth lives (provider, region, durability): `gpu-training-lifecycle` → `references/experiment-contract.md#launch-phase-fields`.
- The metrics tool never decides which training state to load; its run identity, resume and history rules: `gpu-training-observe` → `references/wandb.md`.
