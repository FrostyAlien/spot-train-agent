---
name: gpu-training-recover
description: Designs, validates and diagnoses complete checkpoint resume after spot interruption for single-node training on 1–8 GPUs, with immutable commits, fail-closed restoration and the run's limits checked in the worker. Use when wiring checkpoint save, commit and restore into training code, when a resume failed or looks wrong, or when a preemption must be understood.
---

# Recover a training run

Resumes an interrupted spot run from its newest complete, verified checkpoint, or stops with a precise blocker; a provider retry is not a resume. Bundled `scripts/` and `references/` paths are relative to this skill's directory (`<skill-dir>`); call helpers by absolute path with absolute project paths.

## Inputs
experiment.json (never authorization by itself; fields: `gpu-training-lifecycle` → `references/experiment-contract.md`), actual controller state, immutable checkpoint commit, expected run/data/model/image/code/config identities, backend, rank count, data_resume_mode (data skill: data-readiness.json).

## Steps
### Wire resume into the training code (once per project)
1. **Map the native checkpoint.** Save complete state through the pinned framework's real (sharded) APIs at an optimizer-update boundary on every required rank, and list every file it writes in the verifier manifest. Read [framework-mapping.md](references/framework-mapping.md) first, and for a PyTorch-based stack `gpu-training-lifecycle` → `references/pytorch.md#checkpoints-and-restore`.
2. **Declare rebuilt state** in `derived_roles` with its real semantics, never as placeholder files: the verifier cannot tell a placeholder from real state.
3. **Save to the bucket mount, then commit.** Every rank saves into `$CHECKPOINT_DIR/<step>-<attempt>/payload/`; once all files are closed, one rank runs `scripts/checkpoint_marker.py commit`, which writes `COMMITTED.json` last and prints the `CHECKPOINT_COMMITTED` line operators wait for. Create parents on every write: object stores have no empty directories. Read [publication](references/contract.md#4-crash-safe-checkpoint-publication-on-a-skypilot-bucket-mount) before writing the save code and [save interval](references/contract.md#checkpoint-size-path-and-save-interval) before choosing the path and interval.
4. **Fix one data-position owner** (`gpu-training-data` → `references/contract.md#3-exactly-one-owner-of-data-advancement`) and run its data-position tests ([acceptance tests](references/contract.md#8-offline-test-plan-and-acceptance-criteria), items 3–5).
5. **Test the adapter** with an interrupted-versus-uninterrupted training test (same acceptance tests).
6. **Qualify resume once per stack** with the launch skill's GPU qualification task, which resumes from its own commit on the real GPUs and bucket: offline CPU tests do not prove GPU or provider behavior.
7. **Select and verify at every start,** before any update. `checkpoint_marker.py select` returns `resume` (the newest commit that verifies with all identities), `fresh` only if nothing was ever committed, or exit 3 `blocked` (an unmounted `/checkpoints` too). Read the [restore contract](references/contract.md#5-fail-closed-restore-and-retry-contract) before writing the start code: it holds the command and the restore order.
8. **Journal, finalize and check the run's limits.** The wrapper (preflight, selection) and rank 0 (commits, finish) journal each worker start's events; after the last commit, rank 0 runs `finalize` with the outcome and every extra artifact the user needs at finish, already on the mount. At each start the wrapper runs `limits`: a reached limit stops with exit 0, and only the deadline writes FINAL.json. Read the [contract](references/contract.md#4-crash-safe-checkpoint-publication-on-a-skypilot-bucket-mount) (items 5 and 6) before writing these calls.
9. **Restore, then check** counters and all-rank data/RNG coverage before the first update.

### After a recovery or a failed resume
10. **Read the journal** ([runbook](references/runbook.md)): find the attempt's `CHECKPOINT_JOURNAL` selection line (`resume` with its step, or `fresh`) and the commits since, or the job log's `blocked:` line (exit 3, or the launcher's code when a rank failed the restore: `gpu-training-observe` → `references/runbook.md#triage-a-job-that-did-not-succeed`).
11. **Diagnose** a single directory with `scripts/verify_checkpoint.py` ([verifier](references/verifier.md)); `--accepted-record` is only for multi-writer coordinator setups.
12. **Record instance IDs** after a recovery (a restarted stopped VM or a new one) and check the metrics continue.

## Outputs
The run journal and FINAL.json on the mount; after a recovery, a report: restored step, manifest identity, verified components and compatibility, exclusions, lost-work estimate, next action. Update planning state only from actual evidence.

## Gates
- **Restore only a committed checkpoint that verifies with matching identities and supported compatibility:** a directory without `COMMITTED.json` is an interrupted save, never a candidate.
- **Use an older checkpoint only if newer ones are positively missing, incomplete or checksum-corrupt,** and log the skips and lost steps. Authorization, network, unknown-inventory or compatibility failures block; a deterministic restore failure is never retried. Never start fresh after a restore error or an inaccessible checkpoint: that throws away trained progress.
- **Never fabricate payloads:** a model-only export is not recovery, and a verifier pass proves integrity only.
- **Never delete checkpoints from the training worker:** uploads of the newest ones may still be queued. Prune from outside, once newer ones verify in the bucket.
- **Recovery never changes placement, purchase mode, retry or budget bounds:** they are lifecycle gates.
- **The worker boot disk is never checkpoint truth:** SkyPilot deletes it with a replaced VM.
- **Completion needs a verified final durable checkpoint and FINAL.json** (from `finalize`, checked with `verify-final`), not an exit code: a limit stop exits 0 too.
- **Read `gpu-training-lifecycle` → `references/working-rules.md` before asking the user anything or deciding for them:** it sets the interaction levels.
- **Prepare only this stage's access** (`gpu-training-lifecycle` → `references/credentials.md`); never request secret values in chat, where they would stay in the transcript.

## References
- [framework-mapping.md](references/framework-mapping.md): read before mapping a framework's checkpoint to the manifest; it holds the roles and common shapes.
- [contract.md](references/contract.md): read before writing the save, journal, limits or restore code or choosing the save interval; it holds the recovery contract and acceptance tests.
- [verifier.md](references/verifier.md): read before running `verify_checkpoint.py` or `checkpoint_marker.py`; it holds the manifest fields and exit codes.
- [runbook.md](references/runbook.md): read after a recovery or a failed resume; it holds what to check and when.
- `gpu-training-lifecycle` → `references/entrypoint.md#pseudo-code`: read when writing the entrypoint's `select`, `commit`, `journal`, `limits` and `finalize` calls (exit 3).
