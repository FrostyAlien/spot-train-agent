---
name: gpu-training-lifecycle
description: Plan and coordinate the full lifecycle of cost-efficient, resumable single-node GPU research training on spot (preemptible) or on-demand instances with PyTorch, Hugging Face, JAX or other ML framework, Docker, and SkyPilot. Use for a new rental experiment, continuing an existing project, lifecycle planning, or coordinating selection, preparation, launch, recovery, observation, and cleanup across the companion skills.
---
# GPU research training lifecycle

Turn an experiment into a reproducible, budgeted run minimizing total cost of useful training (not advertised GPU-hour price), using existing code and repository conventions.

The skills are usable in any order: the steps below are the reference order of a first run, each one is also a direct entry point, and the agent starts from whatever the project already holds ([operating model](references/operating-model.md#operating-model-where-things-run-and-what-runs-them)).

Scope: one SkyPilot-launched cloud VM with up to eight GPUs; flag larger or distributed scope rather than silently applying these defaults. Kubernetes and Slurm backends are [out of scope](references/operating-model.md#supported-execution-scope). Bundled scripts and references are relative to this skill's directory (`<skill-dir>`); call helpers by absolute path with absolute project paths.

## Inputs
- The training repository, its instructions, any existing `experiment.json` or run lock.
- The user's [working-rules](references/working-rules.md) intake answers.

## Steps
1. Read the project's instructions; inspect its entrypoint, dependency pins, dataset format/size, checkpoint implementation and tools before editing.
2. **Choose the starting point, the interaction level and a tier with the user** ([working rules](references/working-rules.md#questions-to-the-user)); they decide.
   - Starting point: new, or continue from the project's existing progress.
   - Interaction level (`approval.mode`): automatic, gated (default) or steps.
   - Tier: Standard (`tier: standard`, default: Managed Jobs recovery, campaign guard) or Minimal (`tier: minimal`: one self-bounding on-demand `sky launch` cluster for a short run the user watches). Both share image, data and checkpoint rules.
3. Start the project's `experiment.json` from [the example](assets/experiment.example.json) per [the contract](references/experiment-contract.md); no credentials; preserve existing decisions.
4. Compare providers before launch; research the chosen one ([checklist](references/provider-research.md), including account state, quota for every planned resource and any official provider agent skills) and record the findings in the project; write the project's [provider hooks](references/provider-hooks.md); then record [the launch selection](references/experiment-contract.md#what-to-record-before-launch).
5. Validate offline (`--phase launch` before a proposed paid launch; `--locked previous-run-contract.json` for recovery or post-launch changes):
   `python3 <skill-dir>/scripts/validate_experiment.py <project>/experiment.json`
6. Before each stage, every run, prepare only its access per [credential preparation](references/credentials.md): reuse valid credentials, set up only missing/expired access, cover replacement workers, keep reusable access at cleanup, never request key values in chat.
7. Load only the next stage's skill by frontmatter name (directories may be renamed; order of a first run: [operating model](references/operating-model.md#operating-model-where-things-run-and-what-runs-them)); if absent, report it and continue safe analysis, never inventing tools or installing software.

   | Stage | Skill | Expected output |
   | ---|---|--- |
   | Compare offers and effective cost | `gpu-rental-select` | timestamped shortlist, workload benchmark plan, chosen offer |
   | Keep recovery independent of GPU preemption | `gpu-training-controller` | running non-spot control host: pinned client and API server, verified endpoint, start checks passed (state/restore plan only for a long-lived or shared server) |
   | Prepare a portable runtime before renting | `gpu-training-image` | pinned image/build recipe, dependency record, preflight result |
   | Prepare TB-scale data and cache | `gpu-training-data` | immutable data manifest, staging and cache plan |
   | Launch the fixed-provider experiment | `gpu-training-launch` | reviewed job config, confirmed job/resource IDs, run lock |
   | Resume after preemption or interruption | `gpu-training-recover` | verified complete checkpoint and continuation evidence |
   | Enforce limits while unattended | `gpu-training-observe` | deployed campaign guard (spend ceiling, attempts, capacity wait, optional deadline, notifications) |
   | Observe progress and investigate | `gpu-training-observe` | latest metrics, incidents, bounded decisions |
   | Export results and release resources | `gpu-training-finish` | verified artifacts, cleanup inventory, realized-cost report |
8. [Qualify each new software/hardware/storage stack once](references/operating-model.md#qualify-a-new-stack-once), with the entrypoint written per [its contract](references/entrypoint.md): the training argv parsed inside the image on CPU, then one GPU qualification task (fresh, then resume, with the measurements); the CPU rehearsal only when GPUs are unaffordable; a qualified stack skips this. The stack includes the SkyPilot version chosen at the start; run its [start checks](references/gates.md#skypilot-version-chosen-at-the-start-kept-for-the-run) once, not per launch.
9. Deploy the [runtime](references/operating-model.md#build-once-and-reuse) once as reviewed scripts and callbacks, and operate the run on the control host with each stage skill's runbook ([operating model](references/operating-model.md#operating-model-where-things-run-and-what-runs-them)): wait for notifications, never poll; never change code on your own; only crashes, the stall watchdog and the guard's limits stop a run. Waits and quality checks at the planned evaluations: `gpu-training-observe`.

## Outputs
- Validated `experiment.json` and adjacent contract artifacts.
- Report: decision summary, generated files, checks actually run, remaining live checks, next approval; label conclusions **documented**, **locally tested**, **live tested** or **unverified**; never call an offline template production-validated.

## Gates
- **Provider lock:** fixed once training starts; region too, unless an approved recovery plan permits that provider's other regions. Recovery never silently changes GPU count/type, dataset revision, image, optimizer or learning-rate schedule.
- **Spot-only default:** no silent on-demand fallback. If spot capacity is not reacquired within the declared wait/attempt limits, stop and report; only the campaign guard may switch to a user-approved, same-provider on-demand fallback (`recovery.allow_on_demand` + `recovery.on_demand_fallback`), as a separate task announced to the user.
- **Budget approval:** approve vendor/service, payment basis, spending ceiling and recovery range before paying. Manifests, `approved` values and validators authorize nothing and cap no spending. Research or skill-creation requests authorize no cloud launch or paid drill.
- **Capability and acceptance:** pass the [exact recovery capability gate](references/gates.md#exact-recovery-capability-gate) before creating a task, and the [acceptance gates by area](references/gates.md#acceptance-gates-by-area).
- **No silent fresh start:** restore the latest COMPLETE verified checkpoint from the GPU provider's durable storage under the recorded policy; authentication/network failures block restoration, never justifying a fresh start.
- **No rented GPU for CPU work:** never build images, download assets/datasets or preprocess on a rented GPU ([ask which machine instead](references/operating-model.md#keep-cpu-work-off-rented-gpus)); rented allocations only pull the qualified image, fetch prepared data, run the job-start preflight and train.
- **Checkpoints are recovery truth:** training state is restored only from verified durable checkpoints; W&B or any other metrics backend is observation, never a recovery store or a reason to start fresh.
- **No resident AI:** the assistant is never scheduler, checkpoint selector, budget guard or a required recovery dependency; deploy and supervise recovery/monitoring processes separately.
- **Project-first decisions:** Follow the shared working rules in [references/working-rules.md](references/working-rules.md) (project settings are authoritative; ask with options and a recommendation; no unapproved change to science, budget, placement, purchase mode or scope).

## References
- [experiment-contract.md](references/experiment-contract.md): read when editing `experiment.json`.
- [credentials.md](references/credentials.md): read when a stage needs access.
- [operating-model.md](references/operating-model.md): read when planning where things run or continuing a project.
- [gates.md](references/gates.md): read before the first launch of a stack, when the SkyPilot version changes, or when provider, region, GPU or purchase mode changes.
- [official-skypilot.md](references/official-skypilot.md#loading-the-official-skill): read before using the official `skypilot` skill for CLI/YAML/SDK specifics.
- [provider-research.md](references/provider-research.md): read when the user picks a provider or the SkyPilot version changes.
- [provider-hooks.md](references/provider-hooks.md): read when writing the project's provider hooks for the runbooks.
- [entrypoint.md](references/entrypoint.md): read when writing or reviewing the training image's entrypoint.
- [pytorch.md](references/pytorch.md): read with entrypoint.md when the project uses PyTorch (its collective timeout setting, rank mode for scripts without hooks, hang dumps, checkpoints and restore).
- [evidence.md](references/evidence.md): read before pinning integrations or labelling evidence.
- [compatibility.md](references/compatibility.md): read when installing on Codex/Claude Code.
- [working-rules.md](references/working-rules.md): read before asking the user anything.
