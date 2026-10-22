Research checked: 2026-10-01 UTC. Design guidance; verify APIs against the project’s pinned versions.

Contents: [Docker contract](#minimal-portable-docker-contract) ([SkyPilot integration](#skypilot-docker-runtime-integration), [secrets](#secrets)); [runtime pitfalls](#common-runtime-pitfalls); [environment decision](#environment-decision); [an existing image](#an-existing-image); [builder and platform](#builder-and-platform); [GPU change](#gpu-change); [stack qualification](#stack-qualification-checklist); [job-start preflight](#job-start-preflight-and-deeper-diagnostics); [other frameworks](#frameworks-other-than-pytorch).

## Minimal portable Docker contract

Build once, run by immutable image digest. Pin the base, Python, the framework and every library the project uses together (for example PyTorch or JAX, Transformers, Accelerate, Datasets, DeepSpeed, NumPy, tokenizers, custom extensions and the chosen metrics SDK, such as `wandb==<tested-version>` or `mlflow==<tested-version>`) in a dependency lock with hashes, not a startup `pip install`, and test the combination. Record compiler/ABI and CUDA/cuDNN details. A pinned base tag is weaker than a digest; deliberately refresh security updates between experiments, not during recovery. [Docker build practices](https://docs.docker.com/build/building/best-practices/)

The host supplies the NVIDIA driver and configured GPU container runtime. A container does not eliminate driver compatibility requirements. [NVIDIA runtime installation](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html). Verify the exact CUDA release's driver matrix and required features; minor-version compatibility has PTX and feature limitations, so a generic major-version lower bound is not sufficient validation for all workloads. [CUDA compatibility](https://docs.nvidia.com/deploy/cuda-compatibility/minor-version-compatibility.html)

Compile custom CUDA extensions for explicit target compute capabilities (variables and why: [builder and platform](#builder-and-platform)). PTX fallback improves flexibility but is not guaranteed optimal or compatible with old drivers. [PyTorch extensions](https://docs.pytorch.org/docs/2.14/cpp_extension.html). Key any extension cache by image/toolchain/architecture; test actual kernels after initialization.

Allocate `/dev/shm` deliberately for multiprocessing loaders; Docker's default is 64 MiB. Prefer explicit `--shm-size` over host IPC unless host IPC is actually needed and approved. [Docker run](https://docs.docker.com/engine/containers/run/)

Container overhead ([NVIDIA GPU containers](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/overview.html), [Docker storage drivers](https://docs.docker.com/engine/storage/drivers/)):
- Do not promise a generic Docker performance percentage. Containers use the host kernel and GPU driver; actual overhead depends on runtime configuration, GPU/NVLink visibility, NCCL topology, shared memory, CPU/NUMA limits and mounted data/checkpoint I/O.
- Prefer explicit data/cache/output mounts over heavy I/O in the writable image layer.
- If overhead is a concern, qualify a representative direct-versus-container workload once with matched versions, devices and storage, measuring collective bandwidth and end-to-end throughput. Reuse those results for the unchanged stack rather than benchmarking on every recovery.
- Do not weaken isolation as a generic performance fix.

Illustrative, non-executable-until-filled template:
```dockerfile
FROM <trusted-cuda-pytorch-base>@sha256:<verified-digest>
WORKDIR /app
COPY requirements.lock /app/requirements.lock
RUN python -m pip install --require-hashes -r requirements.lock
COPY training/ /app/training/
ENTRYPOINT ["python", "-m", "training.entrypoint"]
```
The entrypoint launches one agreed distributed command per worker node, propagates signals, and exits nonzero for restore/checkpoint failures. Set GPU count, CPU/RAM limits, shared memory, mounts and run spec outside the image. Mount prepared data read-only; output and caches use distinct paths. Keep the GPU count explicit and size shared memory for the measured loader workload. Cache paths (e.g. HF Hub/Datasets) come from the task's envs or argv, never image ENV ([why](#skypilot-docker-runtime-integration); data skill: cache rules).

### SkyPilot Docker runtime integration
The renderer uses `resources.image_id: docker:...`, which is SkyPilot's runtime-environment mode ([SkyPilot Docker modes and requirements](https://docs.skypilot.ai/en/latest/examples/docker-containers.html); check these requirements and the bootstrap behaviour against the pinned SkyPilot release):
- **SkyPilot overrides Docker `ENTRYPOINT` with `/bin/bash`;** it does not invoke the entrypoint above. Put the reviewed wrapper explicitly in task `run`/renderer argv, for example `["/opt/train/bin/python", "-m", "project.runtime"]`, replacing the path/module with those actually installed in the image.
- **One launcher:** the wrapper performs readiness and required restore, then launches exactly one distributed launcher for the node (exit codes 3 and 4 come from this wrapper, before the launcher starts; lifecycle skill: entrypoint contract). Do not wrap a wrapper that already starts `torchrun` in a second `torchrun`.
- **Image requirements:** a Debian-based image and root or passwordless sudo.
- **No image ENV or WORKDIR:** the task's `run:` shell in the container starts in `~/sky_workdir` and does not inherit the image's ENV, and SkyPilot may start a fresh conda environment, so bare `python`/`torchrun` may not select the pinned training environment. Install the project as an importable package and use a verified absolute interpreter/wrapper path rather than relying on Docker WORKDIR, ENV or shell activation.
- **Qualify the actual SkyPilot invocation,** not only `docker run`: test imports, all-rank GPU access, collective communication, storage and restore through that exact command.
- **Setup cost:** SkyPilot runtime bootstrap and image pulls may still happen on paid workers; prebuilding training dependencies does not guarantee zero setup cost. Measure those costs separately.

### Secrets
Do not put datasets, secrets, SSH keys, provider credentials, or HF tokens into image layers or build context. Build secrets use secret mounts, not Docker `ARG`/`ENV`, which can persist. [Docker secrets](https://docs.docker.com/build/building/secrets/). Runtime secrets come from the chosen provider's restricted secret channel/file mount, not the run manifest, command line, logs or image. No credential creation or persistent-access setup is implied by this research.

Metrics tool: pin its SDK in the lock; its key only at runtime; upload and privacy settings: `gpu-training-observe` → `references/wandb.md`.

## Common runtime pitfalls
- **CUDA wheel versus host driver**: framework wheels (e.g. PyPI PyTorch) pull a specific CUDA runtime/NCCL build that needs a minimum host driver (NVIDIA's compatibility guide, read at decision time). Check the provider's host image driver and log `nvidia-smi` in the preflight.
- **Framework runtime downloads**: some frameworks download weights, fonts or check models at first use (some skip a safety check silently when offline). Bake such assets at build time with the framework's own resolver, record SHA-256, and point the framework's settings at them so workers never download at boot.
- **Heredocs in Dockerfiles** need BuildKit syntax 1.4+ (`# syntax=docker/dockerfile:1.7`).
- Check the project's provider research record for image contents that break SkyPilot mounts on that provider.

## Environment decision
1. **Project pins win.** An existing Dockerfile, lock file, conda environment or requirements pins define the environment. Changing torch, CUDA or framework versions needs the user's permission and a comparison test, because releases can change numerics and defaults (PyTorch does not guarantee reproducibility across releases).
2. **No pins:** choose a framework release and one of its published CUDA builds (for example a PyTorch CUDA wheel variant or a JAX `cuda` extra), or a vendor image (NGC, official framework images, a cloud deep-learning container). CUDA follows the framework version. The researcher may use any framework and libraries; the skills only require that the choice is pinned and recorded. Check the vendor's current compatibility documentation for the target GPU and the provider's host driver at decision time, and record the URLs and date in the image lock. Keep no hand-maintained GPU table.
3. **Env first, then GPU:** with a pinned environment, derive its supported GPUs from the pinned wheels and extensions (compiled architectures, CUDA version) and recommend currently rentable GPUs within that range.
4. **Legacy environment with no rentable GPU:** (a) first test it unchanged; newer drivers run older CUDA runtimes, and PTX for an older architecture can JIT on newer GPUs (prove it with the preflight). (b) Otherwise make the smallest upgrade that supports an available GPU, not the newest, and move dependent pins consistently. (c) Verify against a baseline: old checkpoints load, outputs on fixed inputs match within stated tolerances, a short training run tracks recorded curves; set TF32/determinism flags explicitly; use the old environment (even on CPU) as the reference when it still runs. (d) Treat the result as a new experiment revision with user approval, a new environment lock and image, and results labelled as an upgraded environment.

## An existing image
A user who already trains with an image (a registry reference, a local image, or a Dockerfile that builds) keeps it when it passes these checks, all on CPU:
- Its platform matches the target host (`docker image inspect --format '{{.Os}}/{{.Architecture}}'`); a local image or a Dockerfile gets pushed and its digest recorded ([runbook](runbook.md#4-log-in-and-push)).
- Framework and CUDA versions, read inside the image, are supported by the provider's host driver; the compiled architectures cover the target GPU (`scripts/gpu_preflight.py coverage`).
- The exact training argv parses with the framework's own parser ([runbook](runbook.md#3-build-detached)).
- The entrypoint contract (lifecycle `references/entrypoint.md`) and the helpers listed in the SKILL's entrypoint-and-helpers step are present at `/opt/gpu-skills/`. They usually are not: add them as a thin layer (`FROM <image>@sha256:...`, copy the helpers, the entrypoint and the code at the recorded commit ([runbook](runbook.md#2-code-at-a-recorded-commit)), code layer last) instead of rebuilding the environment.
Record the result in the image lock like a new build. A failed check is reported with the smallest fix; replacing the environment needs the user's permission ([environment decision](#environment-decision): project pins win).

## Builder and platform
- Choose the build machine with the user. With a control host, recommend it: it exists anyway, it is a Linux CPU VM, usually in the provider's network, so pushing a 5-15 GB CUDA image to the provider's registry is fast, and it costs nothing extra. Without one (an existing SkyPilot API server, or a local one on the user's machine), recommend a short-lived CPU VM launched through that SkyPilot in the training region, deleted after the push ([runbook](runbook.md#build-on-a-short-lived-cpu-vm)); it bills only while it builds. Build locally instead when the user has a Linux machine of the target platform with good upload bandwidth, best of all one with its own GPU (it can also run the local test), or existing CI with Linux runners. With no preference, use the control host, or the short-lived VM when there is none.
- Never build on a rented, billed GPU allocation: it pays GPU rates to compile, repeats on every replacement, and can produce a different environment per attempt. Never install packages at job start instead of building: it pays GPU rates and repeats on every replacement.
- Do not build the target image on a Mac. Match the builder's CPU architecture to the target host where practical (native `linux/arm64` for Grace-based GH200/GB200/GB300, `linux/amd64` otherwise); emulated cross-builds work for wheel installs but make CUDA source builds very slow. Record `platform` in the image lock and check it against the instance type before launch.
- Prefer prebuilt wheels that match torch, CUDA, Python, C++ ABI and platform (flash-attn's installer tries one before compiling). For source builds use a CUDA devel base in a multi-stage build and set the target architectures explicitly through the build system: `TORCH_CUDA_ARCH_LIST` for PyTorch extensions, `CMAKE_CUDA_ARCHITECTURES` for CMake projects, or the extension's own variable. Without it, PyTorch extensions build for the GPUs visible during the build, which is none on a CPU builder and the wrong one on a workstation with a different card.
- Record in the image lock: base image digest, framework/CUDA versions, platform, compiled architecture list, decision sources and date. Put the architecture list in the image tag when it was compiled from source (for example `...-sm89`).
- Some kernels still compile at first use on the GPU (Triton, `torch.compile`, DeepSpeed ops that were not prebuilt). Prebuild them where the tool allows, or persist their cache, and record the startup time they add.

## GPU change
Before launching on a different GPU type, compare its compute capability (from vendor documentation at selection time) with the image's recorded architecture list:
```bash
python3 <skill-dir>/scripts/gpu_preflight.py coverage --arch-list "<recorded list>" --capability X.Y
```
Native coverage means reuse the image; PTX-only means expect a slower first start and requalify; unsupported means build a new image for that GPU and requalify, with the user's approval. Check the host CPU platform too. A GPU change never happens inside a running experiment.

## Stack qualification checklist
Qualify the reusable stack once on already authorized resources: actual CUDA forward/backward, custom kernels, collectives, loader throughput, durable checkpoint commit and fresh-process restore, and metrics-run continuation when history is ahead of the checkpoint. Record exact versions, architecture and tested integration outside the worker. Rerun full qualification after major changes to these interfaces or dependencies, not for every experiment using the same stack.

## Job-start preflight and deeper diagnostics
Every allocation runs the probe before data download or training:
```bash
python3 <skill-dir>/scripts/gpu_preflight.py probe --expected-gpus N --expected-platform <locked-platform> [--min-free-gb PATH=GB]
```
- **What it checks:** device count, image platform (pass the image-lock platform with `--expected-platform`), GPU capability against the image's compiled architectures, driver/CUDA initialisation, uncorrected ECC, a small kernel per GPU, NCCL all-reduce and optional extension smoke calls. `--min-free-gb PATH=GB` (repeatable) fails it when the disk budget does not fit (data skill: staging and storage).
- **Fail fast:** it takes seconds to about a minute and exits nonzero rather than retrying. CPU machines cannot test CUDA, the driver, NCCL or multi-GPU behaviour, so this is where those risks are caught.
- **Then the quick readiness checks** against the stack's qualification: immutable digest and pins, GPU/driver compatibility, tiny CUDA/collective test, required configuration and secret availability, data access, same-provider durable checkpoint access, and scratch/shm capacity (scoped probe objects, never over a committed checkpoint; `nvidia-smi` alone is not enough). New experiment inputs still require their own data/config validation.
- **A replaced worker** never triggers a full benchmark or a rebuild. Do not allocate resources merely to satisfy this checklist without approval.

Deeper diagnostics (DCGM level 2 and above, nccl-tests bandwidth sweeps, burn-in) are optional: consider them at a stack's first qualification or when throughput drops, and skip them by default for short single-node runs. No separate canary run: the first bounded qualification run of a stack plays that role. CPU tests of the training code itself are the project's own development practice, never a pipeline stage: on CPU the pipeline only parses the argv and imports the project.

## Frameworks other than PyTorch
The job-start probe works for any framework *(offline only: the live tests ran PyTorch)*: `gpu_preflight.py probe --framework none --arch-list "<image lock list>" --smoke <module:function>` reads devices, compute capability, driver and ECC from `nvidia-smi`, checks architecture coverage against the recorded list, and runs the project's own GPU and collective smoke functions (for example a small JAX matmul and all-reduce). Without `--arch-list` or `--smoke` it reports those checks as not run. Stacks that compile kernels at runtime (JAX/XLA, Triton-only code) have no compiled-architecture list: leave out `--arch-list` and let `--smoke` run a real compiled operation on the target GPU instead. Scope: NVIDIA GPUs (CUDA, `nvidia-smi`); AMD ROCm and TPUs are not covered by these helpers.

