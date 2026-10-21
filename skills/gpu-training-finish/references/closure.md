# Artifact verification and closure report

## Terminal outcome

Establish the terminal outcome from actual job/provider state: completed target and evaluation, failed, cancelled, or blocked. Record exit status, last committed step/useful tokens, checkpoint lineage and reason. Never convert missing logs into success. If the process is still running, confirm whether the task is observation, graceful completion or cancellation before changing it.

## Artifact contract

The runbook's verify task proves the final checkpoint and listed artifacts. It does not cover:
- **Exports outside the bucket:** hash the local source before upload, then compare the destination checksum or download and hash it after transfer; both must refer to the same immutable generation. An ETag (a multipart one especially) is not a whole-file hash. Verify the destination is accessible under existing permissions; never widen access or publish artifacts.
- **Pickle safety:** load only trusted run artifacts; arbitrary pickle checkpoints can execute code. Prefer safe tensor formats and documented restricted loading. A restore test never authorizes buying another GPU.

## Exact cleanup inventory

For each resource record provider, region, account/project, type, exact immutable ID, run ownership evidence, shared flag, requested action, current state, approval reference, data-loss consequence and post-action observed state. Include failed launch leftovers and detached volumes. Stop releases compute but may erase ephemeral storage and usually does not stop all storage charges. Permanent deletion requires applicable explicit approval; preserve resources whose ownership or intended retention is unclear. Use no broad terminate-all or wildcard destructive commands.

Verify provider state after each mutation and audit remaining GPUs, disks, volumes, snapshots, buckets (versions, incomplete multipart uploads), IPs, endpoints,
load balancers/NAT, databases and controller services. Reconcile the inventory with live billing/usage and report residual hourly/monthly costs, retention owner/date and any blocked cleanup. Do not claim zero ongoing charges from a stopped VM alone.

Account for the control host's idle spend; it stays unless in scope (SKILL.md gates). Do not revoke shared credentials, destroy shared datasets, or cancel commitments as inferred cleanup.

## Closure report fields

- Outcome and completion criterion; exit code; final committed tokens/steps; quality/evaluation result
- Artifact manifest URI/hash, safe load/restore verification and unresolved omissions
- Worker/provider terminal state evidence and timestamps
- Per-resource action, authorization, verification and retained billing
- Estimated versus observed cost by compute, boot/staging/compile, replay/recovery, qualification, storage, ingress/egress at both ends, controller, observability, taxes and credits
- Actual cost per million committed nonpadding tokens / committed optimizer step; undefined when no useful work completed
- Scientific comparability: same task/config/evaluation or explicit differences; no ranking solely by GPU utilization
- Preliminary/final billing status and next evidence needed; provider billing can arrive after termination
- Retention owner/date, ongoing cost estimate, blocked actions and next decision

Count replay once in spend, never as useful completed work. Distinguish consumed credits from card top-ups and prepaid commitments. Compare measured performance with the pre-run estimate to improve the next quote comparison; keep the completed run's record immutable. Failed and cancelled runs still receive closure records and residual-resource verification.

## Metrics run
A metrics run (W&B or another tool) marked finished, a summary metric or an artifact link is not proof of a complete training checkpoint. Reconcile the last durable optimizer step with the metrics run's training/global_step and replay/attempt history; keep the run link without uploading private code, datasets or model weights. Keep the stack qualification evidence, so the next unchanged run does not repeat paid tests.
