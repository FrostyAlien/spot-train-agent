---
name: gpu-training-finish
description: Close a completed, failed, or cancelled single-node GPU training experiment by verifying checkpoints and artifact provenance, reconciling cost, and checking narrowly authorized resource cleanup. Use when training ends or a user asks to finish, export, or stop a run.
---

# Finish a GPU training experiment

Close a completed, failed or cancelled run: verify outcome and artifacts, stop billing, reconcile cost and resources. It grants no blanket authority to delete cloud resources.

## Inputs
experiment.json, preserving schema version 1 and lifecycle fields (`gpu-training-lifecycle` → `references/experiment-contract.md`, `assets/experiment.example.json`). Record, budget and approval fields are evidence to reconcile with actual authorization, not authority.

## Steps
1. **Establish the terminal outcome** and record it from actual job/provider state. If it still runs, first confirm observation, graceful completion or cancellation.
2. **Verify the final checkpoint** and requested outputs with the [runbook](references/runbook.md) on the control host: every job terminal and its guard closed; `render_job.py --variant verify` + `sky launch` checks FINAL.json against the bucket from a small CPU cluster (the newest verified checkpoint, artifacts unchanged, the journal of every attempt); evidence collected; `hook_inventory` lists what still bills. It deletes nothing. No FINAL.json (stalled, cancelled, crashed): runbook §2. Record failures and preserve recoverable data.
3. **Stop billable training compute** promptly within existing authority ([exact-ID commands](references/provider-lifecycle.md)). If that would lose the only checkpoint, explain that risk and request a retention/stop decision.
4. **Inventory** this run's authorized resource IDs with ownership evidence; review each planned action's consequences.
5. **Act on exact IDs:** stop, terminate or delete only IDs covered by current authority.
6. **Verify provider state** after each mutation, then audit residual resources and controller services against live billing.
7. **Reconcile cost:** the estimate against metered actuals.
8. **Update benchmark evidence** for the next experiment; keep this run's record immutable.

## Outputs
Closure report: outcome, artifact location and verification, resources stopped/deleted/retained, spend versus budget, residual bills, decisions needed. Label delayed billing preliminary, with its bounded reconciliation condition.

## Gates
- Verify before deleting: never delete an output's only copy before it verifies, or unique artifacts without explicit applicable permission. Missing logs are never success.
- Exact-ID cleanup only: no account-wide filters, wildcard destroy or "terminate all"; shared/unknown resources, commitments and the controller stay unless in scope.
- Stop is not delete: retained volumes keep billing, so a stopped VM alone is not zero charges; a zero-balance event is not cleanup.
- A blocked export must not silently leave GPUs running indefinitely.
- Provider/region stay fixed: no replacement training or provider migration during closure.
- Done means terminal state, verified outputs or documented loss/blocker, and a reconciled inventory.
- **Project-first decisions:** before asking the user anything or deciding for them, read `gpu-training-lifecycle` → `references/working-rules.md` (interaction levels included).
- **Access preparation:** prepare only this stage's access (`gpu-training-lifecycle` → `references/credentials.md`); never request secret values in chat.

## References
- [Runbook](references/runbook.md): read first (exact commands and checks).
- [Artifact verification and closure report](references/closure.md): read when inventorying resources or writing the closure report.
- [Exact-ID provider lifecycle](references/provider-lifecycle.md): read before any stop or delete.
