# Findability check (release only, not installed)

After a docs restructure, a fresh agent that sees only the installed skills must still find what it needs. Give
each task below to a separate fresh agent with read access to the skills folder only (not `tests/`, review files,
CHANGELOG, VALIDATION or README). It starts from the SKILL.md routers, answers, and lists the files it read. Pass
when the answer has every point under "Expected" and nothing contradicting it.

## 1. Watchdog limits after qualification
Task: "The GPU qualification passed. What do I set for the stall watchdog in experiment.json, and from where?"
Expected: copy `recommended.watchdog` (its `limits`) from the qualification's journal-check line into the
contract's `watchdog`; the `notes` name the fallbacks; `ceilings` only when the user asks.

## 2. A job failed with return code 93
Task: "The managed job is FAILED with return code 93. What happened and what do I do?"
Expected: the stall watchdog stopped a silent job; read the `STALL` line and the stacks under it; a traceback
above it means a crash whose exit hung; for a collective, the framework's hang dump (PyTorch: flight recorder
under `$CHECKPOINT_DIR/debug/`); fix and requalify (a failing qualification keeps its cluster for the fix loop); healthy
silent work means raising that phase's `watchdog.limits`; 93 is never restarted.

## 3. A PyTorch script without hooks
Task: "My training is a plain PyTorch loop launched with torchrun, no callbacks, and I may not edit it. How does
it get progress ticks?"
Expected: rank mode: the launcher starts `/opt/gpu-skills/torch_rank.py --step-hook -- train.py <args>` in place
of the script; `torch_rank.py` is vendored in the image; an evaluation inside the loop is a train gap (pass
`--eval-scale` to the qualification); ranks the script spawns itself do not get it.

## 4. Closing a run that stalled
Task: "A run ended with exit 93 and there is no FINAL.json. How do I close it?"
Expected: the finish verify task still prints the journal (`FINISH-JOURNAL`), cut-off saves and the prune list,
then exit 3; the outcome comes from the observe triage; the last durable state is the newest directory in the
prune list's `keep`; then the inventory and cleanup as usual.

## 5. Helpers in the image
Task: "Which helper files go into the training image, and where?"
Expected: `/opt/gpu-skills/`: `checkpoint_marker.py`, `verify_checkpoint.py`, `gpu_preflight.py`,
`progress_watch.py`, and `torch_rank.py` for PyTorch rank mode; checked in the image's CPU checks.

## 6. Checkpoint save interval
Task: "How do I choose the checkpoint save interval for the main run?"
Expected: from the qualification: at least 20 x the steady-state `save_seconds` and at least 1.2 x
`upload_flush_seconds`; the boot disk holds about 1.25 x a checkpoint + 10 GB of not-yet-uploaded saves.

## 7. Control host for a one-off campaign
Task: "I am running one two-day campaign. Do I need a controller backup/restore rehearsal?"
Expected: no; the controller runbook steps are enough; backup, restore and DR acceptance are an optional step for
a long-lived or shared control plane.

## 8. A small shared control host
Task: "The control host is small and shared. Can the campaign guard read job logs less often?"
Expected: yes: `log_read_seconds` in the guard's config (e.g. 120); by default it reads on every RUNNING tick; a
skipped read is no evidence of a wedge.

## 9. SkyPilot start checks
Task: "We picked the SkyPilot version for this project. Which checks run once, before the first launch?"
Expected: the renderer's `--validate-with-skypilot` round trip, and the same with `--scheduling-timeout` (whether
this version accepts `job.wait_for_scheduling_timeout`, kept as `SCHED`); every flag the runbooks use exists in that
version's help (the launch runbook's `flags()` block); a skim of the release notes for recovery, credential upload
and spot fields; with the optional guard also its `--self-test` and `--check-sdk`; recorded; not repeated per launch.

## 10. A resume that logs missing keys
Task: "After a resume, the Hugging Face Trainer logs 'missing keys' while loading the checkpoint. Is that OK?"
Expected: no: a load with missing or unexpected keys is a restore failure: `blocked:` and exit 3, never a fresh
start; reload strictly (the PyTorch checkpoint notes).

## 11. A run without a control host
Task: "We already have a SkyPilot API server our team runs. Do we need a control host for the guard?"
Expected: no: the worker enforces the budget, deadline and attempt limits itself at every start and sends the
notices; connect to the existing server (client at the server's version, the user logs in, redacted checks);
builds and data copies run on a short-lived CPU VM through that SkyPilot; the optional guard only adds a limit on
waiting for capacity after a preemption, a timed fallback and notices while no VM exists.

## 12. Why did the run stop
Task: "The managed job shows SUCCEEDED but training did not reach the last step. What happened?"
Expected: a limit stopped it: read FINAL.json's outcome (`budget_stopped`, `no_progress_stopped`,
`attempts_stopped`, `deadline_stopped`) or, with nothing committed, the journal's `limit` event; every limit stop
exits 0, the only code that ends a managed job without a restart.

## 13. A Nebius spot price cap
Task: "On Nebius, can the run pause instead of paying above 1.50 per GPU-hour?"
Expected: optionally, without a SkyPilot change: create a pricing policy before the run (`nebius billing
pricing-policy create`), set `budget.spot_price_cap`, and the worker caps each new VM through `hook_price_cap`
(switch and stop; SkyPilot restarts it capped); offline only until proved on a small GPU spot VM; the spend bound
stays at the on-demand ceiling.
