# Observe and cancel exact jobs (diagnostics, offline only)
The [runbook](runbook.md) has the commands that ran live. Checked 2026-10-01 against current official CLI. Confirm all flags with the locked version's `--help`; syntax changes across releases.

## Read state and logs
```bash
sky api info
sky jobs queue --help
sky jobs queue
sky jobs queue -a
sky status -u
sky api status REQUEST_ID
sky jobs logs JOB_ID --no-follow
sky jobs logs --controller JOB_ID --no-follow
sky cost-report
sky cost-report -a
```
Use recorded exact request/job IDs in place of placeholders. Queue/request/cluster state are different layers: correlate them with provider instance IDs. Cost report is estimated. A provider inventory query is needed to confirm paid resources actually disappeared. Omit refresh flags unless approved: `sky jobs queue -r` and `sky jobs logs -r` may restart a stopped remote controller.

Tail logs only when useful: `sky jobs logs JOB_ID` follows by default. Scrub credentials from diagnostic artifacts; raw application logs can contain secrets. Before any authentication step use the exact verified endpoint and host permission: `sky api login -e https://verified-control.example.org` is only a placeholder recipe, not an actual endpoint. Then verify `sky api info` and version lock.

## Observer access
Ask only for the missing verified destination/scope. During an access outage, use the declared outage policy and preserve required checkpoint truth.

## Cancel within explicit operation scope
```bash
sky jobs cancel JOB_ID
```
Cancel the managed job, not only its worker. Wait for terminal state, then verify provider inventory and retention charges. Do not use a global cancel/down command, wildcards, or an ambiguous display name.

Current releases also document `sky jobs cancel JOB_ID --graceful --graceful-timeout 120`. Confirm pinned support and approved grace/cost first. This waits for queued cached-mount uploads after job cancellation (a plain cancel and a preemption do not); it does not guarantee the application creates a new complete training checkpoint. An omitted graceful timeout can wait indefinitely. Persist a checkpoint through the application protocol before cancelling when feasible, without exceeding the agreed shutdown reserve.

Controller teardown: `gpu-training-controller` → `references/commands.md#state-restart-and-teardown`. Never infer permission to delete data from permission to stop compute. Do not delete storage, shared services or the API server as a substitute for job cancellation.

Sources: https://docs.skypilot.ai/en/latest/reference/cli.html ; https://docs.skypilot.ai/en/latest/examples/managed-jobs.html
