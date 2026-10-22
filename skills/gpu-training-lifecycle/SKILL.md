---
name: gpu-training-lifecycle
description: Plans and coordinates resumable, budgeted single-node GPU research training on spot (preemptible) or on-demand cloud VMs with SkyPilot and Docker, for PyTorch, Hugging Face, JAX or any ML framework. Use when planning or starting a rental training experiment, continuing an existing project, or coordinating selection, image, data, controller, launch, recovery, observation and cleanup across the companion skills.
---
# GPU research training lifecycle

Turns an experiment on one cloud VM with up to eight GPUs into a reproducible, budgeted run that minimizes the total cost of useful training (not the advertised GPU-hour price), using the project's existing code and conventions. Bundled `scripts/` and `references/` paths are relative to this skill's directory (`<skill-dir>`); call helpers by absolute path with absolute project paths.

## Inputs
- The training repository, its instructions, any existing `experiment.json` or run lock.
- The user's intake answers.

## Steps
A first run goes: intake and selection → the SkyPilot API server → in parallel the data, the image build and push, the provider hooks and the secrets script → the GPU qualification → approval → submit → wait for notifications → finish. Read the [operating model](references/operating-model.md#operating-model-where-things-run-and-what-runs-them) before planning the run: it holds that order with what each stage needs (a fresh quote and quota before approval). Each stage is also an entry point, so start from what the project holds. Flag multi-node or larger scope instead of applying these defaults; Kubernetes and Slurm backends are [out of scope](references/operating-model.md#supported-execution-scope).
1. **Read the project.** Inspect its instructions, entrypoint, dependency pins, dataset format and size, checkpoint code and tools before editing; project settings are authoritative.
2. **Run the intake.** Ask the whole [intake checklist](references/working-rules.md#questions-to-the-user) once, early. The user decides the starting point (new or continue), the [interaction level](references/working-rules.md#interaction-level-approvalmode) (automatic, gated by default, or steps) and the tier (Standard by default).
3. **Write the contract.** Start `experiment.json` from [the example](assets/experiment.example.json) per the [contract](references/experiment-contract.md); preserve existing decisions, no credentials.
4. **Research the provider.** Compare providers, research the chosen one with the [checklist](references/provider-research.md) (quota for every planned resource) and record dated findings in the project; Nebius has a [profile](references/providers/nebius.md) to re-check. Write the [provider hooks](references/provider-hooks.md) and record [the launch selection](references/experiment-contract.md#what-to-record-before-launch).
5. **Validate offline.** Run `python3 <skill-dir>/scripts/validate_experiment.py <project>/experiment.json`; add `--phase launch` before a proposed paid launch and `--locked previous-run-contract.json` for recovery or post-launch changes.
6. **Prepare access.** Set up each stage's access when it needs it ([credentials](references/credentials.md#small-repeatable-flow)): reuse valid credentials, set up only what is missing, cover replacement workers, keep reusable access at cleanup, never ask for key values in chat.
7. **Write the entrypoint.** Follow [its contract](references/entrypoint.md) before the image build, which bakes it in (PyTorch: also [pytorch.md](references/pytorch.md)); the stack's qualification and start checks belong to `gpu-training-launch`.
8. **Hand over to the stage skill.** Load only the next stage's skill, by frontmatter name (directories may be renamed). If it is absent, report it and continue safe analysis; never invent tools or install software.

   | Stage | Skill |
   | ---|--- |
   | Compare offers and effective cost | `gpu-rental-select` |
   | Run a SkyPilot API server independent of GPU preemption | `gpu-training-controller` |
   | Build a portable image before renting | `gpu-training-image` |
   | Prepare TB-scale data and cache | `gpu-training-data` |
   | Qualify the stack, then launch on the fixed provider | `gpu-training-launch` |
   | Resume from a verified checkpoint | `gpu-training-recover` |
   | Enforce the run's limits in the worker | `gpu-training-recover` |
   | Run the optional campaign guard | `gpu-training-observe` |
   | Observe progress and investigate | `gpu-training-observe` |
   | Export verified results, release resources, report cost | `gpu-training-finish` |
9. **Operate the run.** Deploy the [runtime](references/operating-model.md#build-once-and-reuse) once as reviewed scripts and callbacks, then run each stage's runbook from the control host, or without one from the operator's machine. Wait for notifications instead of polling.

## Outputs
- Validated `experiment.json` and adjacent contract artifacts.
- Report: decisions, generated files, checks actually run, remaining live checks, next approval. Label conclusions **documented**, **locally tested**, **live tested** or **unverified**; never call an offline template production-validated.

## Gates
- **Provider lock:** fix the provider once training starts (`recovery.allow_provider_change` stays false), and the region unless an approved recovery plan allows others, because checkpoints, data and the approved price live there. Recovery never silently changes GPUs, dataset, image, optimizer or schedule; copying data or checkpoints elsewhere needs transfer approval, and a provider move is a separately approved successor run.
- **Spot-only default:** never fall back to on-demand silently, because it bills more than the approved plan: only a fallback the user approved, announced and in the same provider (launch `references/launch-gates.md#recovery-semantics`); without one, stop and report when spot capacity does not return.
- **Budget approval:** approve vendor, payment basis, ceiling and recovery range before paying, because a record is not consent: manifests, `approved` values and validators authorize nothing and cap no spending; research or skill-creation requests authorize no launch or paid drill.
- **Capability and acceptance:** pass the [capability gate](references/gates.md#exact-recovery-capability-gate) before creating a task, because a support matrix does not prove spot recovery, and the [acceptance gates](references/gates.md#acceptance-gates-by-area).
- **No silent fresh start; checkpoints are recovery truth:** restore the latest COMPLETE verified checkpoint from durable storage; an auth or network failure blocks, because a fresh start discards paid progress. Metrics (W&B or another tool) are observation, never a recovery store or a reason to start fresh.
- **No rented GPU for CPU work:** build, download and preprocess on CPU machines ([which](references/operating-model.md#keep-cpu-work-off-rented-gpus)); a GPU allocation only pulls the qualified image, fetches prepared data, preflights and trains.
- **No resident AI:** the assistant is never scheduler, checkpoint selector, budget guard or recovery dependency, because the run must outlive the session; recovery and monitoring run as separate processes.
- **Project-first:** no unapproved change to science, budget, placement, purchase mode or scope ([working rules](references/working-rules.md#project-first-decisions)).

## References
- [working-rules.md](references/working-rules.md): read before asking the user anything; it holds the intake and the interaction levels.
- [experiment-contract.md](references/experiment-contract.md): read when editing `experiment.json`; it holds every field.
- [operating-model.md](references/operating-model.md): read when planning where things run or continuing a project; it holds hosts, order and roles.
- [gates.md](references/gates.md): read before a stack's first launch, or when the SkyPilot version, provider, region, GPU or purchase mode changes; it holds the capability, acceptance and SkyPilot version gates.
- [credentials.md](references/credentials.md): read when a stage needs access; it holds the reuse flow and each consumer's access.
- [provider-research.md](references/provider-research.md): read when the user picks a provider or the SkyPilot version changes; it holds the research checklist.
- [provider-hooks.md](references/provider-hooks.md): read when writing provider hooks or the optional spot price cap; it holds the hook functions and the cap.
- [providers/nebius.md](references/providers/nebius.md): read when the provider is Nebius; it holds dated rows, its managed server and price cap.
- [entrypoint.md](references/entrypoint.md): read when writing or reviewing the entrypoint; it holds inputs, ticks, run modes and exit codes. [pytorch.md](references/pytorch.md): read with it for PyTorch (collective timeout, rank mode, hang dumps, restore).
- [official-skypilot.md](references/official-skypilot.md#loading-the-official-skill): read before using the official `skypilot` skill's examples; it holds the decisions this suite keeps.
- [evidence.md](references/evidence.md): read before pinning integrations or labelling evidence; it holds what to recheck and the validation ladder.
- [compatibility.md](references/compatibility.md): read when installing on Codex or Claude Code; it holds install paths and invocation.
