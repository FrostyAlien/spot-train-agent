---
name: gpu-training-image
description: Prepare and validate a portable Docker runtime for single-node training on 1–8 rented GPUs, including pinned dependencies, CUDA compatibility and startup checks. Use when building or pushing the training image, writing the entrypoint, changing GPU type, or checking an image against a host.
---

# Prepare a GPU training image

Wrap the user-owned training code in a pinned Docker runtime proven on the target GPUs: generate project-specific Docker, entrypoint and preflight files around it, never rewriting the model or adding a resident AI process. Without a real project, deliver templates and an integration checklist, not a production-ready claim. Bundled `scripts/` and references are relative to this skill's directory (`<skill-dir>`); call helpers by absolute path.

## Inputs
experiment.json (planning state, never authorization), source commit, existing pins or requirements, GPU family/count, backend, model revision, training entrypoint.

## Steps
1. **Decide the environment** ([environment decision](references/contract.md#environment-decision)). Project pins win: changing torch/CUDA/framework pins needs the user's permission and a comparison test. Without pins, pick a vendor-tested framework+CUDA combination (CUDA follows the framework release, e.g. a PyTorch or JAX CUDA build) from current vendor documentation; ask when ambiguous. Never restrict the researcher's framework or libraries.
2. **Fix the platform:** image CPU architecture must match the GPU host; record it in the image lock.
3. **Ask which Linux builder** the user has: CI, CPU VM, remote builder or user-owned GPU workstation, preferably native to the platform. Never a rented, billed GPU allocation; never the target image on a Mac. A Mac-only user can use hosted Linux CI or a small rented CPU VM, not package installs at job start.
4. **Pin** a trusted base by digest and a tested, hashed lock (the chosen metrics SDK included, if any), resolved against the base's own packages: inside the base image, or with its `pip freeze` as constraints. Resolve model/tokenizer revisions separately; bake in no datasets or mutable model downloads.
5. **Compile extensions:** prefer matching prebuilt wheels; a source build sets target architectures explicitly through its build system (`TORCH_CUDA_ARCH_LIST` for PyTorch extensions, `CMAKE_CUDA_ARCHITECTURES` or the project's own flag), recorded in the image lock and tag.
6. **Write the entrypoint** per the entrypoint contract (`gpu-training-lifecycle` → `references/entrypoint.md`; PyTorch: also its `references/pytorch.md`): inputs, `RUN_MODE`, journal, commit line, progress ticks, exit codes; every tunable in argv, never baked in; signal-propagating, one distributed launcher per node, read-only data, separate output and cache paths, no host IPC unless necessary and authorized. Copy the five stdlib helpers into the image at `/opt/gpu-skills/`: `checkpoint_marker.py`, `verify_checkpoint.py` (recover skill), `gpu_preflight.py` (this skill), `progress_watch.py` and, for PyTorch rank mode, `torch_rank.py` (observe skill).
7. **Wire the chosen metrics tool** (W&B, MLflow, TensorBoard or none): credentials only via an approved runtime secret channel; only reviewed telemetry, no automatic model, dataset, private-code or raw-console uploads (`gpu-training-observe` → `references/wandb.md`).
8. **Build once, push** only with upload approval, on the control host following the [runbook](references/runbook.md): pin the base by digest, build detached through the controller skill's `stage.sh`, check versions and GPU coverage on CPU, log in through the provider hooks (password on stdin), push, and record the pushed digest as image.digest (and image.reference) in experiment.json, leaving other fields unchanged.
9. **Before switching GPU type**, check coverage: `python3 <skill-dir>/scripts/gpu_preflight.py coverage --arch-list "<recorded list>" --capability X.Y` ([GPU change](references/contract.md#gpu-change)).
10. **Qualify the stack once** on already authorized resources ([checklist](references/contract.md#stack-qualification-checklist); the launch skill's GPU qualification task); requalify only after major interface or dependency changes.
11. **Preflight every attempt** before data download or training: `python3 <skill-dir>/scripts/gpu_preflight.py probe --expected-gpus N --expected-platform <locked-platform> [--min-free-gb PATH=GB]` (the disk budget, data skill). It fails fast instead of retrying; then run the quick readiness checks ([job-start preflight](references/contract.md#job-start-preflight-and-deeper-diagnostics); other frameworks: [probe flags](references/contract.md#frameworks-other-than-pytorch)).

## Outputs
Dockerfile, .dockerignore, hashed dependency lock, image-lock.json (reference/digest, platform, CUDA/framework/compiler/metrics-SDK versions, compute capabilities), reusable qualification record, preflight-report.json. Report digest verification, qualification identity, readiness (GPU count/memory/architecture, driver/runtime/kernels, /dev/shm, scratch) and untested items.

## Gates
- Ask before unapproved paid allocation, registry upload or credential/persistent-access setup; never allocate just to satisfy a checklist. Keep provider/region and the approved budget.
- One qualified digest per run: no dependency installs at launch or recovery, no CUDA/package changes during automatic recovery.
- No tokens in arguments, manifests, logs or image layers; no secrets in build ARG/ENV.
- Docker does not supply the host driver; verify compatibility. CPU-only inspection is not GPU validation.
- Block incompatible images and missing required secrets safely.
- **Project-first decisions:** before asking the user anything or deciding for them, read `gpu-training-lifecycle` → `references/working-rules.md` (interaction levels included).
- **Access preparation:** prepare only this stage's access (`gpu-training-lifecycle` → `references/credentials.md`); never request secret values in chat.

## References
- [Runtime contract](references/contract.md): read when choosing pins, writing the Dockerfile, integrating SkyPilot or diagnosing GPU portability.
- [Runbook](references/runbook.md): read before building and pushing (exact commands and checks).
- [tools.md](references/tools.md): read only to check the image on a GPU workstation the user owns (offline only).
