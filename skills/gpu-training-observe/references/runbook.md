# Wait and triage runbook

On the control host, or without one on the operator's machine, in bash (`<(...)` below is bash), with the pinned
client's `sky` and `W=~/campaign/<run_id>`, the run's campaign directory (both as in the launch runbook). The worker
enforces the run's limits and notifies its starts, resumes, evaluations, stalls and stops (with the optional guard,
also recoveries and waits), so nobody polls from a chat session. Steps marked *(offline only)* have not run in a live test yet; the rest ran live (`VALIDATION.md`).

## Wait for a milestone (blocking, no polling)
Run waits as background commands (or `stage.sh run wait-$JOB -- bash -c '...'`), report, and end the turn;
come back when the command ends or a notice arrives.
```bash
# first checkpoint committed by this attempt. The line means committed on the worker: the upload to the
# bucket runs afterwards, and the checkpoint is durable only once its COMMITTED.json is listed there
# (a worker lost in between falls back to the previous checkpoint, or starts fresh when there is none)
grep -m1 CHECKPOINT_COMMITTED <(sky jobs logs --tail 0 $JOB 2>&1)
# the next evaluation after step $2, or a STALL, or nothing within $3 seconds: prints the EVAL or STALL
# line (exit 0), exit 1 when the log ended (the job is terminal: read the status), 124 when none came in time
next_event() { timeout "$3" bash -c "exec 3< <(sky jobs logs --tail 0 $1 2>&1); p=\$!; awk -v s=$2 '/STALL [0-9]+-/ { print substr(\$0, index(\$0, \"STALL \")); f = 1; exit } /EVAL step=/ { match(\$0, /step=[0-9]+/); if (substr(\$0, RSTART+5, RLENGTH-5) + 0 > s) { print substr(\$0, index(\$0, \"EVAL step=\")); f = 1; exit } } END { exit !f }' <&3; rc=\$?; kill \$p 2>/dev/null; exit \$rc"; }
next_event $JOB <step> <seconds>
# the end (follows the log until the job is terminal; keep the log as evidence)
mkdir -p $W/logs; sky jobs logs --tail 0 $JOB > $W/logs/job$JOB.log 2>&1
```
- **The waits** are the planned evaluations (planned metric reports in a project without an evaluation): the
  first evaluation, then the first after about 25 %, 50 % and 75 % of the target steps, then the end. Call
  `next_event` with the step before the planned evaluation (0 for the first) and a timeout of 1.5 x its expected
  time from now (from the qualification report: `step_seconds` x the steps to go, plus one evaluation, plus the
  startup allowance after a recovery). The worker sends every evaluation to the user as it comes; the agent judges
  only at the planned evaluations.
- **At each return:** judge the training (below) and send the user two lines, the step with the metrics against
  the expectation and the rate against the qualification, then the spend upper bound (at the on-demand price: the
  worker's latest `limits` event, or the guard's last tick when it runs) and the next planned evaluation; then
  re-arm.
- **At the first planned evaluation,** compare the measured step time with the qualification's `step_seconds` (the
  subset can read faster than the full data: page cache, smaller files). When they differ by more than 25 %, send
  the user the new expected end (steps to go x the measured step time) and its cost bound: the step count can
  still change then.
- **A timeout is itself an event:** the step stopped advancing or slowed. Read the status and the log tail before
  deciding anything. A `STALL` line means the stall watchdog stopped a hung run (triage below).

Status and the worker's spend bound at any time:
```bash
sky jobs queue -v -o json | python3 -c 'import json,sys; j=next(r for r in json.load(sys.stdin) if r["job_id"]==int(sys.argv[1])); print(j["status"], "recoveries:", j.get("recovery_count"), "cluster:", j.get("current_cluster_name"), j.get("failure_reason") or "")' $JOB
sky jobs logs --no-follow --tail 0 $JOB | grep CHECKPOINT_JOURNAL | grep '"event": "limits"' | tail -1
```
With the optional guard, its last line has the same information plus its own spend bound:
`journalctl -u campaign-guard-$JOB -n 3 --no-pager`.
Check after the first commit: the log's `CHECKPOINT_JOURNAL` lines show the preflight (GPU count, driver,
image digest, dataset version, world size) and the selection (`fresh` for a new run, `resume` after a
recovery): `sky jobs logs --no-follow --tail 0 $JOB | grep CHECKPOINT_JOURNAL`. In the provider's console
or CLI the VM has the approved purchase mode (spot) and region.

## Training quality at planned evaluations
At each planned evaluation, after a resume notice (running again after a recovery) and at the end, read the
metrics (the `EVAL` line, which carries training metrics in a project without an evaluation; the chosen tool,
or the journal's `eval` events) and judge them with what you know
about this kind of model: the loss continues after a recovery instead of jumping back
towards its starting value; evaluation improves over epochs; the learning-rate schedule continues; no NaN,
divergence or stall; throughput near the qualification measurement. Report what you see to the user, with
the numbers; a suspicious sign (slow but progressing, a non-finite loss) is a notification, not a fix (the
SKILL's judging step). A run that never trips a limit can still be scientifically wrong; this check is what catches it.

## Triage a job that did not succeed
```bash
sky jobs logs --no-follow --tail 300 $JOB | sed 's/\x1b\[[0-9;]*m//g' | grep -o 'return code list: *\[[0-9, ]*\]' | tail -1
sky jobs logs --no-follow --tail 300 $JOB | grep -E -m1 'blocked: |"ok": false'
sky jobs logs --controller --no-follow $JOB | tail -50
```

| Signal | Class | Next action |
|---|---|---|
| `SUCCEEDED`, FINAL.json `completed` | done | finish runbook |
| `SUCCEEDED`, FINAL.json `deadline_stopped` | the deadline | report the step reached; finish runbook |
| `SUCCEEDED`, no FINAL.json and a journal `limit` event (budget, no_progress, attempts) | a limit in the worker | report which limit, the step reached and the spend bound (`journal-show`). To go on: the user approves a raised limit in the contract, then relaunch under the same run id (`gpu-training-launch` → `references/runbook.md#4-submit-standard-tier-then-the-optional-guard`); it resumes from the newest checkpoint. Otherwise the finish runbook's path without FINAL.json (`gpu-training-finish` → `references/runbook.md#a-run-without-finaljson-offline-only`) |
| `CANCELLED`, `failure_reason` names `wait_for_scheduling_timeout` | no capacity before the first start | report the wait; relaunch later, or with another approved GPU or region, only with new approval |
| `CANCELLED`, the guard ledger's `log` has a cancel (with the guard) | guard limit | report the reason (budget during a wait, capacity wait, wedged VM); a new launch needs new approval |
| `CANCELLED`, otherwise | a person or another tool | ask before anything else |
| `FAILED_NO_RESOURCE`, `FAILED_PRECHECKS`, `FAILED_SETUP`, `FAILED_CONTROLLER` | infrastructure | read `failure_reason` and the controller log: capacity, quota, credentials, image pull; relaunch only within the approval |
| `FAILED`, return code 4 (or 1 with a preflight `"ok": false` line) | preflight | image, driver or host mismatch: fix the image or the GPU choice; never relaunch as is |
| `FAILED`, return code 3 (or 1 with a `blocked:` line) | blocked | restore identity, storage or credentials: resolve it; never start fresh |
| `FAILED`, return code 93 | stall | the stall watchdog stopped a silent job: read the `STALL` line (the rank with the oldest tick, phase, step, gap, limit) and the stacks under it. A Python traceback just above it is a crash whose exit hung: fix that error. The stacks show where Python waits, often a step after the operation that hung (the next GPU copy or `.item()`); a collective hang: read the framework's dump (PyTorch: lifecycle `references/pytorch.md`). Show the user where the code stopped, fix it and requalify before any relaunch (`gpu-training-launch` → `references/runbook.md#1-gpu-qualification-once-per-new-stack-inside-the-approved-qualification-cap`: a failing qualification keeps its cluster for the fix loop). Healthy work silent past its limit: raise that phase's limit ([guard](guard.md#stall-watchdog)) |
| `FAILED`, return code 124 | runtime timeout | the allocation ran for the budget's whole runway at the ceiling: the worker's limits should have stopped it first, so check that the entrypoint calls `limits` (lifecycle entrypoint contract); report the spend bound |
| `FAILED`, other code | application | fix the code, rerun the qualification task, then relaunch within the approved fix-and-relaunch cycles |

With the guard, before any relaunch the failed job's guard ledger must be done, so the next guard carries its
spend and attempts over (`gpu-training-launch` → `references/runbook.md#4-submit-standard-tier-then-the-optional-guard`): `~/sky-venv/bin/python <skill-dir>/scripts/campaign_guard.py --close-job --workdir $W
--job-id $JOB` closes a terminal job; a job the guard cannot observe closes only with `--reason "<what you
checked in the provider console>"`.
