# Finish runbook

On the control host in bash after the run's last job ended, with the pinned client's `sky` (launch runbook),
`R=<launch-skill>/scripts/render_job.py`, `G=<observe-skill>/scripts/campaign_guard.py`, `P=~/sky-venv/bin/python`
(the guard imports SkyPilot), `ST=<controller-skill>/scripts/stage.sh`, `W=~/campaign/<run_id>`
(the run's campaign directory, launch runbook). Nothing here deletes
anything; retention is the user's decision. Steps marked *(offline only)* have not run in a live test yet; the rest ran live (`VALIDATION.md`).

## 1. Every job is terminal and its guard is closed
```bash
sky jobs queue -o json | python3 -c 'import json,sys; [print(r["job_id"], r["status"]) for r in json.load(sys.stdin) if r["job_name"]==sys.argv[1]]' $RUN
for j in <job ids>; do $P $G --close-job --workdir $W --job-id $j; done
```
Check: only terminal statuses, and every job reports `closed` (a job the guard cannot observe needs
`--reason`). Stop guard units that are still active: `systemctl is-active campaign-guard-<job> && sudo systemctl stop campaign-guard-<job>`.

## 2. Verify FINAL.json against the bucket (CPU, cents)
```bash
S=$(date -u +%Y%m%d%H%M%S); mkdir -p $W/evidence
python3 $R /abs/experiment.json --variant verify --cpu-region <region with CPU quota> > $W/tasks/verify-$S.yaml
$ST run verify-$S -- sky launch -y -c fin$S $W/tasks/verify-$S.yaml
$ST wait verify-$S 30; cp ~/stages/verify-$S.log $W/evidence/
sky down -y fin$S
```
Check: `stage verify-$S exit 0` and a `FINISH-VERIFY {..."ok": true, "outcome": ..., "final_step": ...}` line: FINAL.json
names the newest checkpoint that verifies and every listed artifact (best model, exports) is unchanged.
A nonzero exit prints the reason (no FINAL.json, a newer verified commit, a changed artifact): the outcome
is unproven; report it, never paper over it. `FINISH-JOURNAL` lines are the lineage of every attempt; its
`lineage_warnings` (a fresh start after an earlier attempt had committed: that progress was lost) go into
the report. `FINISH-PARTIAL <dir>` lines are checkpoint directories without a marker (saves cut off by a
preemption): list them, with the checkpoints older than the final one, in the inventory below.

**A run without FINAL.json** *(offline only)* (stalled with exit 93, cancelled, crashed): the verify task still
prints the `FINISH-PRUNE`, `FINISH-JOURNAL` (the lineage) and `FINISH-PARTIAL` lines, then `FINISH no readable
FINAL.json` and exit 3. Take the outcome from the observe runbook's triage (status, exit code, the `STALL` line).
The run's last durable state is the newest directory in `FINISH-PRUNE`'s `keep` list: complete by presence (its
marker and every manifest file at its manifest size), not hash-verified; a resume's `select` verifies it. An empty
`keep` list with no commit in `FINISH-JOURNAL` means nothing durable; otherwise check the bucket (a directory the
prune list cannot read is skipped). Report both.

## 3. Evidence
```bash
for j in <job ids>; do
  sky jobs logs --no-follow --tail 0 $j > $W/evidence/job$j.log 2>&1
  sky jobs logs --controller --no-follow $j > $W/evidence/job$j-controller.log 2>&1
done
cp -a $W/campaign.json $W/guard $W/tasks $W/qualification.json \
      $W/qualify $W/evidence/ 2>/dev/null; ls $W/evidence
```
Logs may contain secrets the application printed: review before sharing.

## 4. What still bills
```bash
bash -c 'set -euo pipefail; source "$1"; hook_inventory' _ /abs/provider-hooks.sh
sky status -u; sky storage ls
```
Check: no run-owned VM or disk left (qualification `q-*` and `fin*` clusters included), and none of the
network resources SkyPilot creates per cluster (security groups, IPs): a teardown can leave them behind, so
the inventory hook lists them. Report what remains (buckets with their partial and superseded checkpoint
directories, registry images, the control host) with its monthly cost; the user decides what to keep
(automatic level: the cleanup policy approved at intake).

Pruning: the verify task's `FINISH-PRUNE {...}` line lists the `prunable` checkpoint directories:
everything older than the newest three complete checkpoints (complete: the marker and every manifest file
present at its listed size), never FINAL.json's checkpoint and never anything newer. After the user
approves (automatic level: the cleanup policy), delete them from the control host, never from a worker:
```bash
for d in <prunable dirs>; do bash -c 'source "$1"; hook_storage_delete "$2"' _ /abs/provider-hooks.sh "<checkpoint uri>/$d/"; done
```
Never delete the final checkpoint or FINAL.json's artifacts.

Spend: report the last guard ledger's `spend_upper_usd` (each ledger includes earlier jobs) as what it is,
an upper bound at the on-demand price; the real cost comes from the provider's bill when it arrives.

Then write the closure report ([closure.md](closure.md)).
