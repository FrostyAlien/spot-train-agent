---
name: gpu-training-finish
description: Closes a completed, failed or cancelled single-node GPU training experiment by verifying the final checkpoint and artifact provenance, reconciling cost and cleaning up only the exact resources the run created. Use when training ends, or when the user asks to finish, export or stop a run.
---

# Finish a GPU training experiment

Closes a completed, failed or cancelled run: verify outcome and artifacts, stop billing, reconcile cost and resources. It grants no blanket authority to delete cloud resources.

## Inputs
experiment.json, preserving schema version 1 and lifecycle fields (`gpu-training-lifecycle` → `references/experiment-contract.md`, `assets/experiment.example.json`). Record, budget and approval fields are evidence to reconcile with actual authorization, not authority.

## Steps
Run the [runbook](references/runbook.md) on the control host or, without one, on the operator's machine.
1. **Establish the terminal outcome** from actual job and provider state, and record it ([terminal outcome](references/closure.md#terminal-outcome)). If the run still runs, first confirm whether the task is observation, graceful completion or cancellation. To stop it early on the user's request, follow the runbook's [user-stop path](references/runbook.md#a-run-the-user-stops-early-offline-only).
2. **Close every job and its guard** ([terminal jobs](references/runbook.md#1-every-job-is-terminal-and-its-guard-is-closed)), so nothing writes a newer checkpoint while it is verified.
3. **Verify the final checkpoint** and requested outputs with the [verify task](references/runbook.md#2-verify-finaljson-against-the-bucket-cpu-cents): it checks FINAL.json against the bucket from a small CPU cluster and deletes nothing. A nonzero exit leaves the outcome unproven: report it. A run without FINAL.json (stalled, cancelled, crashed) follows the runbook's [path without FINAL.json](references/runbook.md#a-run-without-finaljson-offline-only). For exports outside the bucket and loading checkpoints safely, read the [artifact contract](references/closure.md#artifact-contract). Record failures and preserve recoverable data.
4. **Collect the evidence** ([evidence](references/runbook.md#3-evidence)) and review the logs before sharing them: the application may have printed secrets.
5. **Stop billable training compute** promptly within existing authority ([exact-ID lifecycle](references/provider-lifecycle.md)). If that would lose the only checkpoint, explain that risk and request a retention/stop decision.
6. **Inventory what still bills** ([what still bills](references/runbook.md#4-what-still-bills), [cleanup inventory](references/closure.md#exact-cleanup-inventory)): this run's resource IDs with ownership evidence, and each planned action's consequences.
7. **Act on exact IDs:** stop, terminate or delete only IDs covered by current authority, and prune checkpoints only after the user approves (automatic level: the cleanup policy approved at intake). Read [provider lifecycle](references/provider-lifecycle.md) before any stop or delete: it holds the stop-versus-delete semantics to establish for the provider.
8. **Verify provider state** after each mutation, then audit residual resources and controller services against live billing.
9. **Reconcile cost:** compare the estimate with metered actuals; the guard ledger's spend is only an upper bound until the provider's bill arrives.
10. **Write the closure report** ([report fields](references/closure.md#closure-report-fields)) and update benchmark evidence for the next experiment; keep this run's record immutable.

## Outputs
Closure report: outcome, artifact location and verification, resources stopped/deleted/retained, spend versus budget, residual bills, decisions needed. Label delayed billing preliminary, with its bounded reconciliation condition.

## Gates
- **Read `gpu-training-lifecycle` → `references/working-rules.md` before asking the user anything or deciding for them:** it sets the interaction levels.
- **Prepare only this stage's access** (`gpu-training-lifecycle` → `references/credentials.md`) and never request secret values in chat.
- **Verify before deleting:** never delete an output's only copy before it verifies, or unique artifacts without explicit applicable permission; a deletion cannot be undone.
- **Missing logs are never success:** the outcome comes from job, provider and bucket state.
- **Exact-ID cleanup only:** no account-wide filters, wildcard destroy or "terminate all", which reach resources outside the run; shared or unknown resources, commitments and the controller stay unless in scope.
- **Stop is not delete:** retained volumes keep billing, so a stopped VM alone is not zero charges; a zero-balance event is not cleanup.
- **A blocked export never silently leaves GPUs running indefinitely:** they bill every hour; report the blocker and ask for the stop decision.
- **Provider and region stay fixed:** no replacement training or provider migration during closure, which only ends the run.
- **Done means** terminal state, verified outputs or a documented loss/blocker, and a reconciled inventory; anything less leaves unknown bills or an unproven result.

## References
- [runbook.md](references/runbook.md): read before closing the jobs; it holds every finish command with its check, in order, including the paths without FINAL.json and for a run the user stops early.
- [closure.md](references/closure.md): read when establishing the outcome, checking exports outside the bucket, inventorying resources or writing the closure report; it holds the artifact contract and the inventory and report fields.
- [provider-lifecycle.md](references/provider-lifecycle.md): read before any stop or delete; it holds the semantics to establish per provider and the checks after each mutation.
