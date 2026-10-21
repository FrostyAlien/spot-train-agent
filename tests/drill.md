# Interruption drill: a tool for developing these skills *(offline only: not run since v0.6.1)*

Kept in the bundle repository, not installed (moved from the recover skill's runbook). A deliberate worker
interruption, to validate the skills' recovery path on a new provider or SkyPilot version. It is not part of
a project run. On the control host in bash, with the pinned client's `sky` (launch runbook),
`HOOKS=/abs/provider-hooks.sh`, `W=~/campaign/<run_id>`, `recovery.planned_drills >= 1` in experiment.json before the launch (so
the guard allows the attempt).

## 0. The job's cluster and its VM
`sky jobs queue -v -o json` may leave `current_cluster_name` empty (consolidated jobs control); SkyPilot
names the managed job's cluster `<job name>-<job id>` (job names over 25 characters are shortened and
hashed by SkyPilot: use run ids up to 25 characters for a drill), and the provider's VM names start with it:
```bash
CL=$(sky jobs queue -v -o json | python3 -c 'import json,sys; r=next(r for r in json.load(sys.stdin) if r["job_id"]==int(sys.argv[1])); print(r.get("current_cluster_name") or r["job_name"] + "-" + str(r["job_id"]))' $JOB)
IDS=$(bash -c 'set -euo pipefail; source "$1"; hook_instance_ids "$2"' _ $HOOKS "$CL"); echo "$IDS"
[ -n "$IDS" ] || echo "no VM found for $CL: do not continue"
```
Check: exactly the worker VM's ID(s); an empty list means the hook or the name is wrong, never that there
is nothing to stop.

## 1. Stop after a durable commit
A `CHECKPOINT_COMMITTED` line means committed on the worker; the upload follows. A stop right after the
first commit line can lose the upload race, and the replacement then starts fresh (nothing durable). So
wait for the second commit and for its marker to be listed in the bucket (the provider's object-storage
CLI or `rclone lsf` on `<checkpoint uri>/<run>/<step dir>/COMMITTED.json`), then stop the VM:
```bash
bash -c 'set -euo pipefail; source "$1"; for id in $(hook_instance_ids "$2"); do hook_instance_stop "$id"; echo "stopped $id"; done' _ $HOOKS "$CL"
date -u +%Y-%m-%dT%H:%M:%SZ
```
Check: `stopped <id>` and the stop time. A drill at the first commit line instead tests the fallback
(previous checkpoint, or fresh): acceptable when that is what you want to see, and recorded as such.

## 2. Recovery
The guard notifies `recovering` and then `running again`. Only after `running again` (the queue shows
`RUNNING` with a higher recovery count; before that the log can still be the old attempt's), wait for the
first commit of the new attempt:
```bash
grep -m1 CHECKPOINT_COMMITTED <(sky jobs logs --tail 0 $JOB 2>&1)
```
Check: the new attempt's `CHECKPOINT_JOURNAL` selection line shows `resume` from the newest committed step
that verifies (the interrupted step may be complete, skipped as damaged, or uncommitted: all acceptable,
record which); step, optimizer/scheduler and data position continue; the metrics continue in the same run
without a jump (observe runbook: training quality at milestones).

## 3. Record
Append to `$W/drills.json`: job, cluster, stopped VM IDs and whether SkyPilot restarted the stopped
VM or created a new one (`hook_instance_ids` again), the times stop -> running again -> next commit, the
resumed step and the interrupted checkpoint's outcome.
