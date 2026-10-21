# Finish runbook

On the control host (or, without one, the operator's machine) in bash after the run's last job ended, with the pinned client's `sky` (launch runbook),
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

**A run without FINAL.json** *(offline only)* (stalled with exit 93, cancelled, crashed, or stopped by a budget or attempt limit and not continued): the verify task still
prints the `FINISH-PRUNE`, `FINISH-JOURNAL` (the lineage) and `FINISH-PARTIAL` lines, then `FINISH no readable
FINAL.json` and exit 3. Take the outcome from the observe runbook's triage (status, exit code, the `STALL` line).
The run's last durable state is the newest directory in `FINISH-PRUNE`'s `keep` list: complete by presence (its
marker and every manifest file at its manifest size), not hash-verified; a resume's `select` verifies it. An empty
`keep` list with no commit in `FINISH-JOURNAL` means nothing durable; otherwise check the bucket (a directory the
prune list cannot read is skipped). Report both.

**A run the user stops early** *(offline only)*: cancel its job by id, gracefully (``observe `references/commands.md` ``),
and wait until it is terminal. When the user wants the newest committed checkpoint as the result, run step 2 and
skip its `sky down`: its exit 3 with `FINISH no readable FINAL.json` is expected here, and its cluster stays up
with the helpers and the bucket mounted until its 10-minute idle autodown. Right after that, write FINAL.json there (`finalize` always names the newest committed
checkpoint that verifies, with the identity of its manifest):
```bash
sky exec fin$S -- 'R=<CHECKPOINT_DIR in $W/tasks/verify-$S.yaml>; H=/opt/gpu-skills/checkpoint_marker.py
D=$(python3 $H prune-list "$R" | python3 -c "import json,sys; print(json.load(sys.stdin)[\"keep\"][0])")
read MV CS FS < <(python3 -c "import json,sys; m=json.load(open(sys.argv[1])); print(m[\"model_version\"], m.get(\"code_sha256\",\"-\"), m.get(\"config_sha256\",\"-\"))" "$R/$D/manifest.json")
python3 $H finalize "$R" --mount-root /checkpoints --run-id <run id> --data-version <dataset version> \
  --world-size <GPU count> --image-digest <image digest> --model-version "$MV" --code-sha256 "$CS" \
  --config-sha256 "$FS" --outcome stopped'
```
Add `--artifact <path>` to the `finalize` line for each file the user needs that is already in the run directory
(an exported model). Then check it with `sky exec fin$S $W/tasks/verify-$S.yaml` (`FINISH-VERIFY` reports
`"outcome": "stopped"`) and `sky down -y fin$S`. To keep an older
checkpoint instead, do not finalize: report its directory so the user can copy it.

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
Check: no run-owned VM or disk left (qualification `q-*`, `fin*` and `build-*` clusters included), and none of the
network resources SkyPilot creates per cluster (security groups, IPs): a teardown can leave them behind, so
the inventory hook lists them. On a server shared with other people `sky status -u` lists their clusters too:
judge only your own (names from this run), and never stop or delete a server you do not run. Save the managed-job
logs before a hosted server's log retention ends. Report what remains (buckets with their partial and superseded checkpoint
directories, registry images, the control host) with its monthly cost; the user decides what to keep
(automatic level: the cleanup policy approved at intake).

Pruning: the verify task's `FINISH-PRUNE {...}` line lists the `prunable` checkpoint directories:
everything older than the newest three complete checkpoints (complete: the marker and every manifest file
present at its listed size), never FINAL.json's checkpoint and never anything newer. After the user
approves (automatic level: the cleanup policy), delete them from the control host or the operator's machine, never from a worker:
```bash
for d in <prunable dirs>; do bash -c 'source "$1"; hook_storage_delete "$2"' _ /abs/provider-hooks.sh "<checkpoint uri>/$d/"; done
```
Never delete the final checkpoint or FINAL.json's artifacts.

Spend: report the last guard ledger's `spend_upper_usd` (each ledger includes earlier jobs) as what it is,
an upper bound at the on-demand price; the real cost comes from the provider's bill when it arrives.

Then write the closure report ([closure.md](closure.md)).
