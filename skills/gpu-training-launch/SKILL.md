---
name: gpu-training-launch
description: Renders, preflights and submits an approved single-node GPU training experiment with fixed placement, versioned data and images, and bounded spending. Use when qualifying a new stack (the GPU qualification task), rendering a task, preparing the secrets file, submitting a managed job or attaching the campaign guard.
---

# GPU training launch

Qualifies a new stack once, then submits one approved single-node SkyPilot task: a managed job (Standard tier) or a self-bounding plain cluster (Minimal tier). Bundled `scripts/` and `references/` paths are relative to this skill's directory; call helpers and pass project paths by absolute path.

## Inputs
- `experiment.json` (`schema_version` 1), the user's actual approval and the reviewed application argv (JSON array).
- Image, data and checkpoint readiness reports; the controller plan's endpoint and version lock.

## Steps
Run everything on the control host or, without one, on the operator's machine as the SkyPilot client (`gpu-training-lifecycle` → `references/operating-model.md`).
1. **Placement.** Resolve one provider, one fixed region unless the user chose more within it, and an explicit `purchase_model` (`spot` or `on-demand`); the renderer sets the recovery strategy from these. Read launch gates [recovery semantics](references/launch-gates.md#recovery-semantics) and [region policy](references/launch-gates.md#region-policy-is-a-provider-and-user-choice) before choosing: they hold the fallback rules and the shared-storage evidence that more than one region needs.
2. **Image coverage.** Run `gpu-training-image` → `scripts/gpu_preflight.py coverage` for the host CPU and GPU; a GPU outside the image's qualification needs the image skill's [GPU-change decision](../gpu-training-image/references/contract.md#gpu-change) first.
3. **Contract.** Check that the run has 1 node, 1–8 GPUs, an exact image digest, an immutable dataset version, unique run and checkpoint ownership, a tested resume path and durable checkpoint storage in the selected provider. The entrypoint follows `gpu-training-lifecycle` → `references/entrypoint.md`; every tunable lives in argv, so an out-of-memory fix needs no rebuild.
4. **Secrets and start checks.** From here on, follow the [runbook](references/runbook.md#0-secrets-file-before-each-launch-never-in-chat-yaml-or-arguments): it holds each command with its check. Write the secrets file with the project's secrets script; secrets reach SkyPilot only through `--secret-file`. Run the start checks once per SkyPilot version.
5. **Qualify a new stack once** ([what counts as new](../gpu-training-lifecycle/references/operating-model.md#qualify-a-new-stack-once)) before its first paid run, inside the approved qualification cap ([runbook](references/runbook.md#1-gpu-qualification-once-per-new-stack-inside-the-approved-qualification-cap)). Its two short starts on the approved GPUs measure what the run's settings need (startup allowance, save interval, disk size, watchdog limits). A failure keeps its cluster for the fix loop (no rebuild); never go to the paid run with a failing one. The [CPU rehearsal](references/runbook.md#2-cpu-rehearsal-fallback-only-when-gpu-time-is-unaffordable-cents-offline-only) is the fallback when GPU time is unaffordable.
6. **Approval** ([runbook](references/runbook.md#3-approval)). Run the [pre-submission checks](references/launch-gates.md#pre-submission-environment-checks), base the [estimate](references/launch-gates.md#cost-fields-and-the-estimate) on the measured rate and add spend made outside the run to `budget.controller_allowance`. Show the runway, the per-allocation timeout, the attempt limits and any deadline. At the gated and steps levels, show them with the exact operation, the all-in estimate and the fix-and-relaunch cycles the budget allows, and ask for approval. At the automatic level, proceed when they fit the plan approved at intake; otherwise stop and ask.
7. **Submit.** Standard ([runbook](references/runbook.md#4-submit-standard-tier-then-the-optional-guard)): run `sky jobs launch`; the worker enforces the run's limits, so no host has to stay online. The optional guard (`monitoring.guard: external`) attaches right after; its first tick must see the job, or cancel it. Minimal ([runbook](references/runbook.md#minimal-tier-offline-only)): run `sky launch` of a plain task with a timeout and autodown.
8. **After submission.** Verify the actual resources and run identity ([launch gates](references/launch-gates.md#after-submission)); Minimal: `sky status` shows autostop with autodown. Hand over to `gpu-training-observe` (wait for milestones, do not poll).

## Outputs
`<workdir>/tasks/*.yaml` (rendered tasks), `<workdir>/guard/guard-job<ID>.json` and its ledger, `<workdir>/campaign.json` (jobs of this run, for relaunch accounting), `<workdir>/qualification.json` (the qualification result and the stack tuple it proves); the launch record has the job ID, guard unit, runway and deadline (if any).

## Gates
- **Read `gpu-training-lifecycle` → `references/working-rules.md` before asking the user anything or deciding for them:** it sets the interaction levels.
- **Spend nothing until the user approves** provider, GPU configuration, total limit, controller allowance, runtime, recovery scope and retention. The manifest's approval field, `-y` and a valid login never grant it; research and rendering may proceed. Credential transmission, account grants and data sharing need the host's permission.
- **Never default `purchase_model`:** null cannot render a launch artifact. A spot task keeps `use_spot: true` in every candidate unless the user approved an on-demand fallback, so SkyPilot never picks on-demand by itself.
- **Render only the approved provider and regions** (the lifecycle provider lock): reject wildcard, cross-provider and unapproved cross-region candidates, also in `any_of`/`ordered` and command-line overrides.
- **`max_restarts_on_errors` comes from `recovery.max_app_restarts`** (default 0), never `max_attempts`: each restart re-rents a GPU for what is usually the same error.
- **No secret values in YAML, arguments or manifests,** which end up in logs and the server's records; cloud credentials on VMs follow SkyPilot's defaults (lifecycle `references/credentials.md`).
- **Never blindly resubmit:** a job name is not an idempotency key; reconcile a lost response by request, job and provider IDs first ([commands](references/commands.md)).
- **Never hard-code localhost or start a second control plane** when a remote endpoint is down: a second server splits the run's job state, and submission and resume work without the agent connected.

## References
- [runbook.md](references/runbook.md): read before writing the secrets file; it holds every qualification and launch command with its check, in order.
- [launch-gates.md](references/launch-gates.md): read before choosing placement and recovery, rendering a task or changing a gate; it holds the rules and their sources.
- [commands.md](references/commands.md): read when a submit response is lost, and before adapting official `skypilot` skill examples; it holds the diagnostics.
