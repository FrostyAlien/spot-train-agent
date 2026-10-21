---
name: gpu-training-launch
description: Render, preflight, and submit an approved single-node GPU training experiment with immutable placement, versioned data and images, and bounded spending. Use when qualifying a new stack (the GPU qualification task), rendering a task, preparing the secrets file, submitting a managed job or attaching the campaign guard.
---

# GPU training launch

Render, preflight and submit one approved single-node SkyPilot task, a managed job (Standard tier) or a self-bounding plain cluster (Minimal tier), with fixed placement, purchase mode, image and data, and bounded spending. Bundled `scripts/` and `references/` paths are relative to this skill’s directory, not the training repository; call helpers and pass project paths by absolute path.

## Inputs
- `experiment.json` (`schema_version` 1), the user's actual approval and the reviewed application argv (JSON array).
- Image, data and checkpoint readiness reports; the controller plan's endpoint and version lock.

## Steps
Run everything below on the control host, or, without one, on the operator's machine as the SkyPilot client (`gpu-training-lifecycle` → `references/operating-model.md`).
1. Resolve one provider and an explicit `purchase_model` (`spot` or `on-demand`).
2. Set the recovery strategy explicitly: `FAILOVER` for the fixed manifest region; `EAGER_NEXT_REGION` for user-chosen multi-region recovery within this provider, over `recovery.allowed_regions` with evidence of zero-cost shared storage covering checkpoints, data and image, and for an approved on-demand fallback without the guard (`ordered`, spot first; [launch gates](references/launch-gates.md)).
3. Check image platform/architecture coverage of the host CPU and GPU with `gpu-training-image` → `scripts/gpu_preflight.py coverage`; a GPU outside the image's qualification needs the image skill's GPU-change decision first.
4. Validate 1 node, 1–8 GPUs, exact image digest, immutable dataset version, unique run/checkpoint ownership, application-tested resume path and selected-provider durable checkpoint storage. A private registry sets `image.registry_login: true` (SkyPilot needs `SKYPILOT_DOCKER_USERNAME/PASSWORD/SERVER` together as secrets).
5. The entrypoint follows the entrypoint contract (`gpu-training-lifecycle` → `references/entrypoint.md`) (`RUN_MODE`, journal, the `CHECKPOINT_COMMITTED` line, exit codes; every tunable in argv, so an out-of-memory fix never needs an image rebuild). It starts with `gpu_preflight.py probe` before data download or training.
6. **Qualify a new stack once** (new image, framework, provider, storage or SkyPilot version) before its first paid run, following the [runbook](references/runbook.md): one GPU qualification task (`render_job.py --variant qualify`, then `sky launch`) on the approved GPUs and world size with the real image and buckets, 20 steps on the qualification subset: fresh to its second save, then a resume to the end and `finalize`; bounded by a timeout and autodown, inside the approved qualification cap. It recommends `recovery.startup_allowance_minutes` and the watchdog limits, and fails above 90% peak GPU memory. A failed qualification keeps its cluster for the fix loop (`sky exec --workdir`, no rebuild); one rebuild and a qualification on the new digest end the loop. The CPU rehearsal (`--variant rehearsal --cpu-region R`) is the fallback when GPU time is unaffordable. Repeat until it passes; a qualified stack skips this.
7. Connect to the API server on the control host; verify `sky api info`. Run the pre-submission environment checks ([launch gates](references/launch-gates.md)) and prepare only this run's access (lifecycle credentials reference): secrets reach SkyPilot only through `--secret-file` at launch, from the project's secrets script.
8. Bind the estimate to measured useful throughput (launch gates). Add the spend made outside the run so far (qualification, data, control host) to `budget.controller_allowance`. The validator prints the budget runway check; the rendered task carries the run's limits for the worker; with the optional guard (`monitoring.guard: external`), `campaign_guard.py --init-config ... --dry-run` (observe skill) prints the guard runway too. Show the runway (hours of job time the budget allows at the ceiling; also each allocation's runtime timeout), the attempt limits and the deadline, if any. Gated and steps levels: show these with the exact operation, the all-in estimate and the fix-and-relaunch cycles the same budget allows, for approval. Automatic level: proceed when they fit the plan approved at intake, otherwise stop and ask.
9. **Standard: submit** ([runbook](references/runbook.md) step 4): the notification check once per channel, render the task (secret names only, the run's limits, `--scheduling-timeout` when the start check accepted it), `sky jobs launch -d ... --secret-file F` and the job id. The worker enforces the limits from then on; no host has to stay online. **With the optional guard** (`monitoring.guard: external`): a last `--init-config --dry-run` before the submission, then `campaign_guard.py --init-config` for that job (carries earlier jobs' spend and attempts; a new run revision gets its own campaign directory), a first `--once` tick that must observe the job (otherwise cancel it), the guard loop under systemd; an approved on-demand fallback is then rendered once and handed to the guard.
   Minimal: render with `scripts/render_job.py` (adds autostop with autodown and a `timeout` wrapper) and `sky launch -d -c RUN_ID task.yaml --secret-file F`; there is no guard.
10. Apply the after-submission gates; Minimal: confirm `sky status` shows autostop with autodown. Then hand over to the observe skill (wait for milestones, do not poll).

## Outputs
`<workdir>/tasks/*.yaml` (rendered tasks), `<workdir>/guard/guard-job<ID>.json` and its ledger, `<workdir>/campaign.json` (jobs of this run, for relaunch accounting), `<workdir>/qualification.json` (the qualification result and the stack tuple it proves); the launch record has the job ID, guard unit, runway and deadline (if any).

## Gates
- **Project-first decisions:** before asking the user anything or deciding for them, read `gpu-training-lifecycle` → `references/working-rules.md` (interaction levels included).
- The manifest's approval field never grants authority. Research and rendering may proceed; spend nothing until provider, GPU configuration, total limit, controller allowance, runtime, recovery scope and retention are approved; `-y` never substitutes. Credential transmission, account grants and other sensitive actions need the host's permission; a valid login is not spending or data-sharing approval.
- Never default `purchase_model` silently; null cannot render a launch artifact. A spot primary task keeps `use_spot: true` in every candidate unless the user approved an on-demand fallback: then `ordered` spot-first resources without the guard, or the separate `--variant` task the guard launches after the approved capacity wait.
- Training never switches providers. Reject wildcard placement and cross-provider or cross-region candidates, including any in `any_of`/`ordered` or command-line overrides.
- `max_restarts_on_errors` comes from `recovery.max_app_restarts` (default 0), never `max_attempts`.
- Cloud credentials on VMs follow SkyPilot's defaults (lifecycle credentials reference). No secret values in YAML, arguments or manifests.
- Never blindly resubmit: reconcile an uncertain response by existing request/job/run ID and provider resource IDs.
- Never hard-code localhost or start a second control plane because a remote endpoint is temporarily down. Submission and fixed container resume scripts must work without the submitting agent connected.

## References
- [runbook.md](references/runbook.md): read before qualifying or launching (exact commands and checks).
- [launch-gates.md](references/launch-gates.md): read before rendering a task or changing a gate (rationale and sources).
- [commands.md](references/commands.md): read for diagnostics (a lost submit response) and before adapting official `skypilot` skill examples.
