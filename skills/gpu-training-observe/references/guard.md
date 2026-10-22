# Campaign guard and stall watchdog
Contents: [Campaign guard](#campaign-guard); [Stall watchdog](#stall-watchdog).
Items marked *(offline only)* have not run in a live test yet (`VALIDATION.md`).

## Campaign guard
Attach the guard when the contract's `monitoring.guard` is `external`. The worker already enforces the run's
budget, deadline and attempt limits (recover
`references/contract.md#4-crash-safe-checkpoint-publication-on-a-skypilot-bucket-mount`, item 6) and sends the
notices. The guard adds what no worker can see: waiting for capacity after a preemption, the spend of a billed wait,
a timed on-demand fallback, a wedged VM and notices while no VM exists.

With `external`, `--init-config` leaves the deadline, the attempt limits and the evaluation notices to the worker,
so those fields below stay inactive in the guard, and the guard cancels at the budget only while the job waits
(internals: the `worker_limits` paragraph of the `scripts/campaign_guard.py` docstring). Whichever limit comes
first stops the run.

- **Host:** `scripts/campaign_guard.py` runs on the control host, or without one on a machine that stays awake and
  online ([operating model](../../gpu-training-lifecycle/references/operating-model.md#operating-model-where-things-run-and-what-runs-them)), independent of the GPU worker. It uses the submitter's
  SkyPilot API server in the same active workspace as the job, with a login allowed to cancel it, and looks the job
  up by id across SkyPilot users, so it works from its own host. Run it as `--loop` under systemd
  (`gpu-training-launch` → `references/runbook.md#4-submit-standard-tier-then-the-optional-guard`), or as a
  cron/systemd timer with `--once`.
- **Config:** `--init-config` writes the config from the contract and the campaign ledger (`campaign.json`);
  `--dry-run` prints the runway for the approval. The fields:
  - `deadline_utc`: optional, none by default (the budget is the hard stop); when set, the run stops at it.
  - `max_attempts_without_progress` (default 10): stops a run whose allocations keep ending without a new
    checkpoint. The guard reads the job log for the `CHECKPOINT_COMMITTED` line while an allocation runs. An
    unreadable log never counts against the run; a commit in the last poll interval before a preemption can be
    missed and counts that allocation as stalled.
  - `max_attempts`: an optional absolute cap on infrastructure allocations; leave it null for long spot runs with
    many preemptions.
  - `max_capacity_wait_minutes`: continuous PENDING/SUBMITTED/STARTING/RECOVERING time beyond
    `startup_allowance_minutes`, the stack's normal allocation start (provision, image pull, mounts, data copy;
    measured by the qualification). Without the allowance a large image or dataset reads as "no capacity" and a
    healthy job is cancelled.
  - `budget_usd`, and `ceiling_hourly_usd` set to the on-demand rate (which a price-following spot VM may reach).
    Size the budget at that rate.
  - `reserved_usd` (controller, storage), `spent_before_usd` and `attempts_before` (upper bounds and attempts of
    earlier jobs of the same campaign; they carry a relaunch's history instead of hand edits).
  - `notify_cmd` for `monitoring.notify.channel`: required unless `--attended`; without it the guard warns at every
    start.
  - `fallback`: the optional approved on-demand task (below).
  - `log_read_seconds` *(offline only)*: on a small or shared control host (for example 120) the guard reads the
    log less often; by default every RUNNING tick reads it. A skipped read is no evidence of a wedge.
- **Spend:** every non-terminal minute from the job's submission (SkyPilot `submitted_at`), waiting and blind time
  included, is charged at the ceiling, plus `reserved_usd` and `spent_before_usd`: a hard cap that stops early rather
  than late, not a billing report. Every notification carries it as "spend <= X USD (upper bound at the on-demand
  price)"; the real spot cost comes from the provider's bill.
- **Billing rules** *(offline only)*: with `budget.free_without_vm` (from the provider research: compute billed only while a VM
  exists), PENDING time is free, and STARTING/RECOVERING time is free while the project's `vm_count_cmd` (its
  `hook_instance_ids`) reports no VM for the job's cluster. An unknown answer, blind time, RUNNING and any time a
  replaced primary may still be alive after a fallback are charged. Once per allocation the hook must also find the
  running job's VM: when it finds none, the hook or the cluster name is wrong, the user is told once and no wait
  counts as free from then on.
- **Cancel** *(offline only)*: at a limit (budget, attempts, capacity wait, deadline) the guard cancels gracefully
  first (`graceful_cancel_seconds`, within the shutdown reserve, lets queued cached-mount uploads finish; a plain
  cancel or a preemption does not wait), then plain on later ticks. It cancels only the job it tracks (and the
  primary it replaced) and re-issues a pending cancel every tick.
- **Ledger and notices:** one `state_path` per job; the guard refuses a ledger that belongs to another job. It
  persists the ledger, runs one tick at a time (file lock) and notifies once per event. Each tick prints one
  observation line (status, attempts, wait, spend bound); status changes go to the ledger's `history`. With
  `notify_milestones` (default on) the user hears when the job is running, recovering and running again, so nobody
  polls. With `notify_evals` (default on; off when `--init-config` leaves the evaluations to the worker) the guard reads the log on every RUNNING tick (the last 4000 lines) and
  forwards the newest `EVAL step=<n> {...}` line, each step once.
- **Relaunch:** `--init-config` refuses a relaunch when earlier jobs used every allowed attempt. `--close-job` closes
  a terminal job's ledger (an unobservable one only with a person's `--reason`).
- **Blind** *(offline only)*: every SkyPilot call has a deadline (`sdk_timeout_seconds`, default 120), and a silent
  API server reads as blind. A job missing from the queue counts as unobserved, not finished. While blind the guard
  never cancels or falls back; it notifies the user once per episode and keeps charging the ceiling. A call still
  blocked after `max_blind_minutes` makes `--loop` notify and exit 1, so its supervisor restarts a clean process.
  The worker's runtime timeout (each allocation stops after the budget's runway at the ceiling) and the deadline,
  when set, are the last defence; a person stops the VM with the provider's own stop command if needed (provider
  research record).
- **Fallback** *(offline only)*: keep the task at an absolute path on the guard host.
  `fallback.after_capacity_wait_minutes` must be less than `max_capacity_wait_minutes` (a wait that passed the
  maximum, for example while the guard was blind, cancels instead), and waiting that long at the ceiling must fit
  the budget; the guard refuses a config whose fallback could never fire. It records the fallback's start, then
  launches it with secrets re-read from `fallback.secrets_cmd`. A launch the API server accepted but did not confirm
  (lost reply, guard restart) is found by name among the guard's own jobs (on a shared server another user's job can
  carry the same name) and adopted, never repeated. The waiting primary is cancelled only
  once the fallback is observed alive; a fallback that ends at once returns the guard to the primary, and a failed
  launch keeps the primary (three tries). The fallback job counts as an attempt and is watched under the same
  limits.
- **SDK:** the adapter is written against `jobs.queue_v2`/`jobs.cancel`/`jobs.launch`; `--check-sdk` verifies it in
  the start checks.

## Stall watchdog
On the worker, `scripts/progress_watch.py` wraps the launcher, and the entrypoint ticks from its hooks. It ends a job
that has gone silent: no rank ticked and its processes moved no bytes for longer than the quiet limit of the phase in
force. Default quiet limits: 3 minutes in training, 10 in an evaluation, a save or end-of-training work, 30
before the first tick. The mechanism (byte thresholds, limits that follow the step gaps, the dumps) is in the
script's docstring.
- **Lines:** a stall prints one `STALL` line and every rank's stack, then exits 93 (triage: [runbook](runbook.md#triage-a-job-that-did-not-succeed),
  row 93). A `PROGRESS` line comes every 5 minutes. A collective timeout shorter than the quiet limit ends a
  collective hang first (exit 1 or 134).
- **From the worker itself:** with the notification secret (`NOTIFY_URL`, `NOTIFY_FORMAT`) the stall is sent through
  `scripts/notify.py` before the stop. With `CHECKPOINT_DIR` set, the watchdog also keeps this allocation's alive
  file on the checkpoint mount (rewritten with every `PROGRESS` line), which the run's limits read (recover
  `references/contract.md#4-crash-safe-checkpoint-publication-on-a-skypilot-bucket-mount`, item 6).
- **Guard:** it notifies the `STALL` line when a tick reads it in time (otherwise `job terminal: FAILED`).
  *(offline only)* A RUNNING job whose readable log has no `PROGRESS` line for three periods (the VM or the watchdog
  froze) is notified as "wedged" and cancelled after `wedged_grace_minutes` (15). An unreadable log is only notified.
- **Not caught:** a loop that keeps ticking without finishing (the budget and the timed evaluation waits are the
  backstop); a hang while a background writer still moves bytes, until it ends; a hang in a save while another tenant of a shared
  host flushes to disk; slow training and non-finite losses (the evaluation reports); `watchdog.enabled: false`
  (only the collective timeout and the budget).
- **Tuning** (the contract's `watchdog`): copy the qualification's `recommended.watchdog`. Its `notes` flag saves the
  byte counters cannot see (the save limit then follows the save's duration) and an evaluation hidden in train gaps
  (the train limit then covers it). When a compiler recompiles on new input shapes, raise `limits.train` to the
  longest recompile seen. Raise a phase's limit before the relaunch for healthy work silent longer than it (a long
  metric computation, a large state gather). Set `ceilings` (hard caps on one `eval`, `save` or `final` phase) only
  when the user asks. The qualification runs with the default limits: when `STALL` stops it during healthy silent
  work, shorten that work on the subset.
