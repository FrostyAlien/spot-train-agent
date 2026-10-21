# Qualification and launch runbook

On the control host in bash (`flags()` and `${PIPESTATUS[0]}` below are bash; zsh reports nothing from
`flags()`), with the pinned client's `sky` (`source ~/sky-venv/bin/activate` in each new shell) and the helpers
by absolute path: `R=<launch-skill>/scripts/render_job.py`, `G=<observe-skill>/scripts/campaign_guard.py`,
`ST=<controller-skill>/scripts/stage.sh` (long launches run detached through it, so an SSH drop never kills
them and the exit code is the launch's own), and `P=~/sky-venv/bin/python` for the helpers that import
SkyPilot (`$P $G`, `$P $R ... --validate-with-skypilot`).
Each step ends with a check; stop at the first failed one. `sky` flags are those of the version chosen at
the start (`sky jobs launch --help`); details in the official `skypilot` skill. The training argv is a
JSON array, e.g. `ARGV='["python","-m","project.train","--batch","32"]'`; every tunable lives there.
`RUN` is the contract's `run_id`; `W=~/campaign/$RUN` is the run's campaign directory (rendered tasks,
secrets file, qualification logs, guard ledgers, evidence). Steps marked *(offline only)* have not run in a live test yet; the rest ran live (`VALIDATION.md`).

## 0. Secrets file (before each launch; never in chat, YAML or arguments)
The project owns a small script that prints `NAME=value` lines from files and the provider CLI, e.g.:
```bash
#!/usr/bin/env bash
# <project>/secrets-env.sh: prints the launch secrets; reads them, never contains them
set -euo pipefail
source /abs/provider-hooks.sh
echo "WANDB_API_KEY=$(cat ~/.secrets/wandb)"
echo "SKYPILOT_DOCKER_SERVER=<registry host>"
echo "SKYPILOT_DOCKER_USERNAME=$(hook_registry_username)"
echo "SKYPILOT_DOCKER_PASSWORD=$(hook_registry_password)"
```
```bash
mkdir -p $W/tasks $W/qualify
(umask 077; bash /abs/secrets-env.sh > $W/secrets.env)
python3 $R /abs/experiment.json --secret WANDB_API_KEY --check-secret-file $W/secrets.env
```
Check: `"ok": true` (names only are printed). Delete the file after the launch (`rm $W/secrets.env`);
short-lived registry tokens are refreshed by running the script again.

Start checks, once per chosen SkyPilot version and after the image digest is recorded. First, confirm that
every `sky` flag these runbooks use exists in this version:
```bash
flags() { local sub=$1 h f; shift; h=$(sky $sub --help 2>&1) || { echo "MISSING: sky $sub"; return; }
  for f in "$@"; do grep -qE -- "(^|[ ,])$f([ ,=]|\$)" <<<"$h" || echo "MISSING: sky $sub $f"; done; }
flags "jobs launch" -y -d -n --secret-file; flags "jobs queue" -v -o; flags "jobs logs" --tail --no-follow --controller
flags "jobs cancel" -y --graceful --graceful-timeout; flags launch -y -c -d --dryrun --secret-file --env
flags exec -d --workdir --secret-file; flags logs; flags cancel -y; flags autostop -y -i --down; flags down -y
flags status -u; flags "storage ls"; flags check; flags "api info"; flags "api login" -e; flags "api start" --deploy --foreground
```
Check: no `MISSING` line; otherwise adapt that command from this version's `--help` before going on. Then the
renderer's round trip with the installed SkyPilot (`$P`) for the primary task and the
qualification task, then a dry run of the qualification task through the control host's API server (it proves the server answers, is not stale and
finds the resources; no VM is created; a dry run does not touch file mounts on the server, so the
controller runbook's step 5 checks the server's view of them):
```bash
$P $R /abs/experiment.json --argv-json "$ARGV" --validate-with-skypilot >/dev/null
$P $R /abs/experiment.json --argv-json "$ARGV" --variant qualify --qualify-tag t1 --validate-with-skypilot > $W/tasks/dryrun.yaml
sky launch --dryrun -y $W/tasks/dryrun.yaml
```
Check: "Parsed and round-tripped" twice and the dry run prints the chosen resources. A `sky` command silent
for two minutes: read the API server log (controller runbook) before anything else.

## 1. GPU qualification (once per new stack; inside the approved qualification cap)
Before it, in the image on CPU: the exact `ARGV` parsed by the framework's own parser (image runbook step 3).
Build the qualification argv `QARGV`: `ARGV` plus the framework's step settings for 20 steps, evaluating and
saving every 6 (lifecycle entrypoint contract: `qualify`). With a qualification subset (`dataset.qualify_uri`,
data skill: staging) the task reads the subset instead of the full dataset, in the same mount mode and with
the same disk. `ES` is the full eval set's size over the subset's. Leave `--eval-scale` out when the project
does not evaluate.
```bash
T=$(date -u +%Y%m%d%H%M%S)
python3 $R /abs/experiment.json --argv-json "$QARGV" --variant qualify --qualify-tag q$T --timeout-minutes 20 \
    --eval-scale $ES --secret WANDB_API_KEY > $W/tasks/qualify-$T.yaml
$ST run qualify-$T -- sky launch -y -c q-$T $W/tasks/qualify-$T.yaml --secret-file $W/secrets.env
$ST wait qualify-$T 60; cp ~/stages/qualify-$T.log $W/qualify/
D=$(grep -o 'QUALIFY-DONE [0-9]*' $W/qualify/qualify-$T.log | tail -1 | cut -d' ' -f2)
[ -n "$D" ] && echo "upload_flush_seconds $(( $(stat -c %Y ~/stages/qualify-$T.exit) - D ))"
```
The task runs the entrypoint twice on the approved GPUs with the real image and buckets (fresh to its
second save, then a resume to the end and `finalize`), then
`journal-check --expect fresh,resume --require-final --require preflight --watch-summaries ...`, and prints
`QUALIFY-DONE`. SkyPilot then flushes the queued uploads before the launch returns, so
`upload_flush_seconds` (from `QUALIFY-DONE` to the launch's exit) is about one checkpoint's upload. Run the
`wait` as a background command: it returns when the launch ends. The task usually takes about 2-3 minutes once the
allocation has started (after provisioning, image pull and mounts) and fails fast at the first broken step. `--timeout-minutes` bounds each start: the time to
the first step (model download, compilation; 10 minutes until `startup_seconds` is measured) plus 10 minutes
for the steps, evaluations and saves. Each start runs under the stall watchdog with its default limits, not the
contract's, so a hang ends within minutes with a `STALL` line and the ranks' stacks. A hang before the first tick
ends two minutes before the per-start timeout (up to a 32-minute timeout), without stacks; a slow but healthy
startup there needs a larger `--timeout-minutes`. The limits, and a `STALL` during healthy silent work:
`gpu-training-observe` → `references/guard.md#stall-watchdog`.
Check: `stage qualify-$T exit 0` and the journal-check line `{"events": ..., "ok": true, ...}` with `startup_minutes` (the
renderer stamps the task with its render time, so launch right after rendering); then `sky down -y q-$T`.
On a failure, first run `sky autostop -y q-$T -i 30 --down`: a launch that failed while provisioning or
mounting never set the task's own autodown, and `sky status` then shows no AUTOSTOP for a VM that bills until
someone tears it down. Keep the cluster for the fix loop below, and `$W/secrets.env` with it; `sky down` it
once a qualification passes or the user stops. `peak_memory_fraction` above 0.9
fails: lower the per-device batch in `ARGV` (raise gradient accumulation to keep the effective batch) and
repeat; no image rebuild. A spot cluster that the provider stopped during the task (`sky status` shows it
STOPPED, the launch exited nonzero) is not a failure of the stack: `sky down` it and launch again. Otherwise
read `failures` and the traceback, fix, and repeat; never go to the paid run with a failing qualification. A batch
or code change is a deviation in the automatic level: ask first; in any level a batch change carries the
reproducibility assessment (lifecycle `references/working-rules.md`).

After a pass, set these and report them to the user in one block:
- **Startup:** `startup_minutes` (paid again by every allocation of the main run). Set
  `recovery.startup_allowance_minutes` to the printed recommendation, plus the full dataset's copy time
  under `COPY` (its size divided by the copy rate measured at staging; the task copied only the subset, in
  seconds).
- **Memory:** `peak_memory_fraction`. With `notes: ["low_utilisation"]` (below 0.3) offer the options: a
  larger per-device batch (a hyperparameter change the user approves), a smaller or cheaper GPU, or fewer
  GPUs; in the automatic level continue and notify.
- **Saves:** `save_seconds` (steady state), `checkpoint_gb` and `upload_flush_seconds` (data skill: measure
  I/O), and the save interval they imply; set that interval in `ARGV`. Set `disk.size_gb` so that
  1.25 x `checkpoint_gb` + 10 GB fit beside the image and data. Both rules: `gpu-training-recover` →
  `references/contract.md#checkpoint-size-path-and-save-interval`.
- **Data:** `loader_wait_fraction` (or `read_mb_s` for `MOUNT` data): a large share means more loader
  workers or another mount mode before the paid run.
- **Time:** the expected training time is `step_seconds` x the target steps, plus the evaluations
  (`eval_seconds` scaled by the full eval set's size over the subset's); the observe runbook's planned waits
  use it.
- **Hang limits** *(offline only)*: copy the printed `recommended.watchdog` into the contract's `watchdog`; its
  `notes` and tuning: `gpu-training-observe` → `references/guard.md#stall-watchdog`. Set the collective timeout
  in `ARGV` too (entrypoint contract).
- **Deadline:** none unless the user set one; the budget is the hard stop.
Record the result (date, image digest, SkyPilot version, the journal-check line, `upload_flush_seconds`) in
`$W/qualification.json`.

The fix loop: a code fix found here is tried on the kept cluster without a rebuild
(`gpu-training-lifecycle` → `references/operating-model.md#code-dev-loop`), with a new tag each round so the
journal check sees two new starts:
```bash
U=$T$(date -u +%H%M%S)
python3 $R /abs/experiment.json --argv-json "$QARGV" --variant qualify --qualify-tag q$U --timeout-minutes 20 \
    --eval-scale $ES --secret WANDB_API_KEY > $W/tasks/qualify-$U.yaml
sky exec q-$T --workdir /abs/project $W/tasks/qualify-$U.yaml --secret-file $W/secrets.env -d
sky logs q-$T <job id printed by exec> 2>&1 | tee $W/qualify/qualify-$U.log; echo "exit ${PIPESTATUS[0]}"
```
`--workdir` copies the project, minus what its `.gitignore` (or a `.skyignore`) excludes (lifecycle
`references/experiment-contract.md#adjacent-artifacts`), into `~/sky_workdir` in the container, where `run:` starts; bucket mounts and
copied data stay as the launch left them. A `python -m` argv imports the copied code before the image's
copy; a path argv into the image (`python /app/train.py`) still runs the image's copy, so point it at
`~/sky_workdir` for this loop. A round takes about a minute, so it runs in the foreground; `sky logs` exits 0
when the job succeeded and 100 when it failed. Check: `exit 0` and the journal-check line as above, except that the startup figure
comes from the launch, not from an exec. Once it passes: `sky down`, rebuild the image once and run the qualification task
above on the new digest.

## 2. CPU rehearsal (fallback, only when GPU time is unaffordable; cents) *(offline only)*
```bash
python3 $R /abs/experiment.json --argv-json "$QARGV" --variant rehearsal --qualify-tag r$T --cpu-region <region with CPU quota> \
    --secret WANDB_API_KEY > $W/tasks/rehearsal-$T.yaml
$ST run rehearsal-$T -- sky launch -y -c q-r$T $W/tasks/rehearsal-$T.yaml --secret-file $W/secrets.env
$ST wait rehearsal-$T 60; cp ~/stages/rehearsal-$T.log $W/qualify/
sky down -y q-r$T
```
The same two starts on a CPU VM in a region with CPU-only quota (the control host's region, for example),
the data streamed, one process, GPU-only options dropped by the entrypoint. It proves storage and resume
wiring only; the GPU path stays unqualified until step 1 runs. Check: `stage rehearsal-$T exit 0` and the
journal-check line with `"ok": true` (no `startup_minutes`: the rehearsal runs no GPU probe).

## 3. Approval
```bash
python3 <lifecycle-skill>/scripts/validate_experiment.py /abs/experiment.json --phase launch
$P $G --init-config --contract /abs/experiment.json --job-id 0 --workdir $W \
    --notify-cmd '<json argv>' --dry-run
```
Check: `"valid": true` (read any `warnings`, e.g. what bounds spend if the guard host dies); the dry run
prints `runway_hours` (job time the budget allows at the ceiling; the validator already checked it covers
`budget.planned_job_hours`), the attempt limits, `deadline_utc` (null: none, the budget is the hard stop)
and carried-over spend and attempts. Before the dry run, add the spend made outside the guard so far
(qualification, data, control host) to `budget.controller_allowance` and note it in `qualification.json`.
Every spend figure here and in later reports is an upper bound at the on-demand price; the real spot cost
comes from the provider's bill. Approval at the interaction level: SKILL step 8. The notify command
receives one JSON message on stdin (for example `["curl","-sS","-d","@-","https://ntfy.sh/<private topic>"]`).

## 4. Submit and guard (Standard tier)
A new run revision (new `run_id`) therefore gets a new `$W`; the earlier run's guard bound is carried in `budget.controller_allowance`. Immediately before submitting, the dry run
must say ok, so a job is never left unguarded:
```bash
$P $G --init-config --contract /abs/experiment.json --job-id 0 --workdir $W --notify-cmd '<json argv>' --dry-run | grep -q '"runway_hours"' || { echo "guard config refused: fix it before submitting"; exit 1; }
S=$(date -u +%Y%m%dT%H%M%SZ)
python3 $R /abs/experiment.json --argv-json "$ARGV" --secret WANDB_API_KEY > $W/tasks/$RUN-$S.yaml
sky jobs launch -y -d -n $RUN $W/tasks/$RUN-$S.yaml --secret-file $W/secrets.env && JOB=$(sky jobs queue -o json |
    python3 -c 'import json,sys; print(max(j["job_id"] for j in json.load(sys.stdin) if j["job_name"] == sys.argv[1]))' $RUN)
echo "job $JOB"; rm $W/secrets.env
```
Check: one integer (job ids only grow, so the newest job named `$RUN` is this one). If the launch failed
or timed out without an id, do not resubmit: look for the job first (`sky jobs queue -o json`, name `$RUN`,
recent `submitted_at`).
```bash
$P $G --init-config --contract /abs/experiment.json --job-id $JOB --workdir $W --notify-cmd '<json argv>' \
    [--vm-count-cmd '["bash","-c","source /abs/provider-hooks.sh; hook_instance_ids \"$1\"","_"]']
for i in 1 2 3; do $P $G --config $W/guard/guard-job$JOB.json --once | grep '^{"tick"' | grep -v '"status": null' && break; sleep 10; done
```
Check: a tick line with a non-null `status`. If all three are null (the guard cannot see the job):
`sky jobs cancel -y $JOB`, wait until it is terminal, run one more `--once` to close its ledger, and report.
```bash
sudo systemd-run --unit campaign-guard-$JOB -p Restart=on-failure -p RestartSec=30 -p User=$USER \
    --setenv=HOME=$HOME --setenv=GRPC_VERBOSITY=ERROR \
    $P $G --config $W/guard/guard-job$JOB.json --loop
```
Check: `systemctl is-active campaign-guard-$JOB` prints `active`. The loop exits with code 1 when a
SkyPilot call stays blocked past `max_blind_minutes` (a silently dropped connection), and `Restart=on-failure`
brings back a clean process. Without systemd, run the loop through the controller skill's
`stage.sh run guard-$JOB -- ...` and restart it by hand when notified. Then continue with the observe runbook.

*(offline only)* With `budget.free_without_vm` (the provider bills compute only while a VM exists), `--vm-count-cmd` lets the
guard see that a waiting job has no VM, so that waiting is not charged; without it only PENDING time is free.

*(offline only)* Approved on-demand fallback: render it once with `--variant on-demand-fallback` to an absolute path and
add `--fallback-task <path> --secrets-cmd '["bash","/abs/secrets-env.sh"]'` to `--init-config`; the guard
re-reads the secrets when it fires.

Relaunch after a failure: the earlier job must be terminal and its guard ledger done
(`$P $G --close-job --workdir $W --job-id <old job>`). Before `--init-config`, recompute
`budget.controller_allowance` from what was spent outside the guard since the last launch (fix-loop
qualifications, the control host's hours); the guard's runway shrinks by it. `--init-config` then carries
the earlier job's spend bound and attempts into the new guard, and refuses when no attempt is left.

## Minimal tier *(offline only)*
```bash
S=$(date -u +%Y%m%dT%H%M%SZ)
python3 $R /abs/experiment.json --argv-json "$ARGV" --secret WANDB_API_KEY > $W/tasks/$RUN-$S.yaml
sky launch -y -d -c $RUN $W/tasks/$RUN-$S.yaml --secret-file $W/secrets.env
rm $W/secrets.env
```
Check: `sky status $RUN` shows autostop with autodown. There is no guard; the task's timeout and autodown
bound it.
