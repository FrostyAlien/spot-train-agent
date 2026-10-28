---
name: gpu-training-image
description: Prepares and validates a portable Docker runtime for single-node training on 1–8 rented GPUs, with pinned dependencies, CUDA compatibility and startup checks. Use when building or pushing the training image, writing the entrypoint, changing GPU type, or checking an image against a host.
---

# Prepare a GPU training image

Wraps the user's training code, unchanged, in a pinned Docker image proven on the target GPUs, with project-specific Dockerfile, entrypoint and preflight files. Bundled `scripts/` and `references/` paths are relative to this skill's directory (`<skill-dir>`); call helpers by absolute path.

## Inputs
experiment.json (planning state, never authorization), source commit, existing pins or requirements, GPU family/count, backend, model revision, training entrypoint.

## Steps
1. **Existing image.** Check the user's working image, if any, before building one; build only what it lacks, usually a thin layer ([existing image](references/contract.md#an-existing-image) holds the checks).
2. **Environment.** Follow the [environment decision](references/contract.md#environment-decision). Project pins win: changing torch, CUDA or framework pins needs the user's permission and a comparison test, because releases change numerics. Without pins, pick a vendor-tested framework and CUDA build; ask when it is ambiguous. Never restrict the researcher's framework or libraries.
3. **Builder and platform.** Choose the builder with the user from the [options](references/contract.md#builder-and-platform). Recommend the control host, or without one a short-lived CPU VM through the user's SkyPilot. Never build on a rented, billed GPU, which pays GPU rates to compile, nor the target image on a Mac, which emulates the usual `linux/amd64` platform and makes CUDA builds very slow. Match the image's CPU architecture to the GPU host and record it.
4. **Pins.** Pin a trusted base by digest and a tested, hashed lock resolved against the base's own packages so it never replaces what the base ships ([runbook](references/runbook.md#1-pin-the-base-by-digest)). Resolve model and tokenizer revisions separately; bake in no datasets or mutable model downloads.
5. **Extensions.** Prefer matching prebuilt wheels. A source build sets its target architectures explicitly, because the default follows the GPUs visible at build time, none on a CPU builder ([builder and platform](references/contract.md#builder-and-platform) names the variables and what to record).
6. **Entrypoint and helpers.** Write the entrypoint to the entrypoint contract, `gpu-training-lifecycle` → `references/entrypoint.md` (PyTorch: also its `references/pytorch.md`). Keep every tunable in argv, so tuning needs no rebuild; the [Docker contract](references/contract.md#minimal-portable-docker-contract) holds the runtime rules. Copy the six stdlib helpers into the image at `/opt/gpu-skills/`: `checkpoint_marker.py`, `verify_checkpoint.py` (recover skill), `gpu_preflight.py` (this skill), `progress_watch.py`, `notify.py` and, for PyTorch rank mode, `torch_rank.py` (observe skill). The image's CPU checks confirm they are there, and the qualification runs these same copies, so it proves what the main run uses. The entrypoint runs `gpu_preflight.py probe` on every attempt before data download, so a bad host fails fast ([job-start preflight](references/contract.md#job-start-preflight-and-deeper-diagnostics) holds the command and the checks after it; other frameworks: [probe flags](references/contract.md#frameworks-other-than-pytorch)).
7. **Metrics tool.** Wire the chosen tool (W&B, MLflow, TensorBoard or none) per `gpu-training-observe` → `references/wandb.md`: its key only through an approved runtime secret channel, and only reviewed telemetry: no automatic model, dataset, private-code or raw-console uploads.
8. **Build and push.** Build once on the chosen builder and push, following the [runbook](references/runbook.md) (each command with its check). Record the pushed digest as `image.digest` and `image.reference` in experiment.json, leaving other fields unchanged.
9. **GPU change.** Before switching GPU type, check the image's coverage of it ([GPU change](references/contract.md#gpu-change) holds the `gpu_preflight.py coverage` command and when to reuse, requalify or rebuild).
10. **Local test (optional).** Run the qualification's fresh start and resume on the user's own GPU machine when they have one ([runbook](references/runbook.md#5-local-test-on-the-users-gpu-optional-offline-only)). It is free and catches most code and image bugs before renting. Push any fix and record its digest before the stack qualification.
11. **Stack qualification.** Qualify the stack once through the launch skill's GPU qualification task ([checklist](references/contract.md#stack-qualification-checklist)); `gpu-training-lifecycle` → `references/operating-model.md#qualify-a-new-stack-once` says when to qualify again.

## Outputs
Dockerfile, .dockerignore, hashed dependency lock, image-lock.json (reference/digest, platform, CUDA/framework/compiler/metrics-SDK versions, compute capabilities), reusable qualification record, preflight-report.json. Report digest verification, qualification identity, readiness (GPU count/memory/architecture, driver/runtime/kernels, /dev/shm, scratch) and untested items. Without a real project, deliver templates and an integration checklist, not a production-ready claim.

## Gates
- **Read `gpu-training-lifecycle` → `references/working-rules.md` before asking the user anything or deciding for them:** it sets the interaction levels.
- **Ask before a paid allocation, a registry upload or credential and persistent-access setup,** because each spends money or exposes the project.
- **One qualified digest per run:** no dependency installs at launch or recovery, no CUDA or package changes during automatic recovery, so every replacement runs the qualified stack.
- **No secrets in the image:** none in arguments, manifests, logs, layers or build `ARG`/`ENV`, which persist. Prepare only this stage's access (`gpu-training-lifecycle` → `references/credentials.md`); never request secret values in chat.
- **CPU-only inspection is not GPU validation:** Docker does not supply the host driver, so verify its compatibility.
- **Block an incompatible image or a missing required secret** before a paid attempt finds it.

## References
- [contract.md](references/contract.md): read when choosing the environment, pins or builder, writing the Dockerfile, integrating SkyPilot, changing GPU type or diagnosing GPU portability; it holds the runtime contract and its checks.
- [runbook.md](references/runbook.md): read before building and pushing, and for the optional local test; it holds the exact commands and checks.
- [tools.md](references/tools.md): read for a quick one-GPU smoke check on a GPU machine the user owns, before the [local test](references/runbook.md#5-local-test-on-the-users-gpu-optional-offline-only); it holds the `docker run` smoke command and its limits.
