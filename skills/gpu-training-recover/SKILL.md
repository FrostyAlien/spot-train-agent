---
name: gpu-training-recover
description: Design, validate and diagnose complete checkpoint resume after spot interruption for single-node training jobs on 1–8 GPUs, with immutable commits and fail-closed restoration. Use when wiring checkpoint save, commit and restore into training code, when a resume failed or looks wrong, or when a preemption must be understood.
---

# Recover a training run

Resume an interrupted spot run from its latest complete, verified checkpoint, or stop with a precise blocker; a provider retry is not a resume. Integrate and test the user-owned code's real checkpoint and data APIs (PyTorch-based stacks: `gpu-training-lifecycle` → `references/pytorch.md#checkpoints-and-restore`; any framework: [framework-mapping.md](references/framework-mapping.md)). Bundled `scripts/` and references are relative to this skill's directory (`<skill-dir>`); call helpers by absolute path with absolute project paths.

## Inputs
experiment.json (never authorization by itself; fields: `gpu-training-lifecycle` → `references/experiment-contract.md`), actual controller state, immutable checkpoint commit, expected run/data/model/image/code/config identities, backend, rank count, data_resume_mode (data skill: data-readiness.json).

## Steps
### Wire resume into the training code (once per project)
1. **Map the native checkpoint** into the verifier manifest ([framework mapping](references/framework-mapping.md)): complete state at an optimizer-update boundary on every required rank, via native sharded APIs.
2. **Declare rebuilt state** in `derived_roles` with its real semantics.
3. **Save to the bucket mount, then commit** ([publication](references/contract.md#4-crash-safe-checkpoint-publication-on-a-skypilot-bucket-mount)): every rank saves into `$CHECKPOINT_DIR/<step>-<attempt>/payload/` on the SkyPilot checkpoint mount (`<attempt>`: a tag set once per worker start); once all files are closed, one rank calls `scripts/checkpoint_marker.py` `commit`, which writes the manifest, verifies it, writes `COMMITTED.json` last and prints one `CHECKPOINT_COMMITTED {json}` line (what operators wait for). The run prefix does not exist until the first write: every write under `$CHECKPOINT_DIR` creates its parents.
4. **Never delete checkpoints from the training worker** (uploads of the newest ones may still be queued); keep them during the run, or prune from outside the worker once newer ones verify in the bucket.
5. **Fix one data-position owner** (data skill, contract §3) and run the contract's data-position tests.
6. **Test** the adapter: an interrupted-versus-uninterrupted training test ([acceptance tests](references/contract.md#8-offline-test-plan-and-acceptance-criteria)).
7. **Qualify resume once per stack** with the launch skill's GPU qualification task (fresh, then resume, on the real GPUs and bucket).
8. **Select and verify** at every start, before any update: `python3 <skill-dir>/scripts/checkpoint_marker.py select $CHECKPOINT_DIR --mount-root /checkpoints --run-id RUN --data-version VERSION --model-version VERSION --world-size N --image-digest DIGEST --code-sha256 SHA --config-sha256 SHA` returns the newest committed checkpoint that verifies with all identities (`resume`), `fresh` only if nothing was ever committed, or exit 3 (`blocked`, including an unmounted `/checkpoints`).
9. **Journal and finalize** ([contract](references/contract.md#4-crash-safe-checkpoint-publication-on-a-skypilot-bucket-mount)): the entrypoint's wrapper (preflight, selection) and rank 0 (commits, finish) write `journal` events per worker start; after the last commit, rank 0 writes `finalize` with the outcome and every extra artifact the user needs at finish (best model, exports), which must already be on the mount.
10. **Restore,** then check counters and all-rank data/RNG coverage before the first update.

### After a recovery or a failed resume
11. **Read the journal:** the attempt's `CHECKPOINT_JOURNAL` selection line (`resume` with its step, or `fresh`) and the commits since, or the job log's `blocked:` line (exit 3, or the launcher's code when a rank failed the restore: observe runbook triage) ([runbook](references/runbook.md)).
12. **Diagnose** a single directory with `scripts/verify_checkpoint.py` ([verifier](references/verifier.md)); `--accepted-record` is only for multi-writer coordinator setups.
13. After a recovery, **record instance IDs** (whether SkyPilot restarted the stopped VM or created a new one) and check the metrics continue ([runbook](references/runbook.md)).

## Outputs
The run journal (attempt lineage, preflight, selection, commits) and FINAL.json on the mount; after a recovery, a report: restored step, manifest identity, verified components and compatibility, exclusions, lost-work estimate, next action. Update planning state only from actual evidence.

## Gates
- Restore only a committed checkpoint that verifies with matching identities and supported compatibility; a directory without `COMMITTED.json` is an interrupted save, never a candidate.
- Use an older checkpoint only if newer ones are positively missing, incomplete or checksum-corrupt (log skips and lost steps). Authorization, network, unknown-inventory or compatibility failures block, and a deterministic restore failure is never retried; never start fresh after a restore error or read an inaccessible checkpoint as an unprogressed run.
- Never fabricate payloads; model-only export is not recovery. A verifier pass proves integrity only.
- Placement, purchase mode, retry and budget bounds: lifecycle gates; recovery never changes them.
- The worker boot disk is never checkpoint truth.
- Completion needs a verified final durable checkpoint and completion marker (FINAL.json from `finalize`, checked with `verify-final`), not an exit code.
- **Project-first decisions:** before asking the user anything or deciding for them, read `gpu-training-lifecycle` → `references/working-rules.md` (interaction levels included).
- **Access preparation:** prepare only this stage's access (`gpu-training-lifecycle` → `references/credentials.md`); never request secret values in chat.

## References
- [Framework mapping](references/framework-mapping.md): read when mapping any framework's checkpoint files to the manifest.
- [Checkpoint and recovery contract](references/contract.md): read before implementing an adapter or recovery sequence, or choosing the checkpoint path and save interval.
- [Local verifier](references/verifier.md): read before running `verify_checkpoint.py` or `checkpoint_marker.py`.
- [Runbook](references/runbook.md): read after a recovery or a failed resume.
- Read when writing the entrypoint's `select`, `commit`, `journal` and `finalize` calls (exit 3): `gpu-training-lifecycle` → `references/entrypoint.md#pseudo-code`.
- Read when choosing the data position's owner: `gpu-training-data` → `references/contract.md#3-exactly-one-owner-of-data-advancement`.
- Read for a PyTorch-based stack (save, restore, launchers): `gpu-training-lifecycle` → `references/pytorch.md#checkpoints-and-restore`.
