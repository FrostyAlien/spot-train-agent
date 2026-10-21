# Image build and push runbook

On the control host (or the user's Linux builder), never on a rented GPU. Each step ends with a check.
Registry upload needs the user's approval (registry, visibility, contents). The image holds code and
dependencies only: data, checkpoints, secrets and tunable hyperparameters stay outside it. Prefix
`docker` with `sudo` when the user is not in the `docker` group. Steps marked *(offline only)* have not run in a live test yet; the rest ran live (`VALIDATION.md`).

## 1. Pin the base by digest
```bash
docker pull -q --platform linux/amd64 <base image>:<tag>
docker image inspect <base image>:<tag> --format '{{index .RepoDigests 0}}'
```
Check: a `repo@sha256:...` reference; the Dockerfile takes it as `ARG BASE_IMAGE` (no `latest`). Use the
target host's platform (`linux/arm64` for Grace-based hosts).
Resolve the dependency lock against this base, inside it (`docker run --rm <base>@sha256:... pip ...`) or
with its `pip freeze` as constraints, so the lock never replaces what the base ships (torch, CUDA libraries).

## 2. Code at a recorded commit
Where the code lives was asked at intake (`code.repo`):
```bash
# a git URL (public, or private with a read-only deploy key or token on this build machine only)
git clone <repo url> ~/build/<name> && git -C ~/build/<name> checkout <commit>
# a local project copied to this machine: build from its checkout
git -C <project> rev-parse HEAD; git -C <project> status --porcelain
```
Record `code.repo` and `code.commit` in experiment.json. Check: a clean tree at that commit (or record the
diff's hash), and `.dockerignore` keeps data, checkpoints, credentials and `.git` out of the build context.
The code is baked into the image; SkyPilot's `workdir: {url, ref}` would make every recovery depend on the
git host and a token on the workers, so it is not used for training runs. Hyperparameters stay out of the
image (argv), so tuning never needs a rebuild.

## 3. Build (detached)
```bash
<controller-skill>/scripts/stage.sh run image -- docker build --platform linux/amd64 \
    --build-arg BASE_IMAGE='<base>@sha256:...' -t <name>:local <project>
<controller-skill>/scripts/stage.sh wait image 60
```
Check: exit 0. Then, still on CPU:
```bash
docker run --rm --entrypoint python <name>:local -c 'import torch, sys; print(sys.version, torch.__version__, torch.version.cuda, torch.cuda.get_arch_list())'
python3 <skill-dir>/scripts/gpu_preflight.py coverage --arch-list "<list printed above>" --capability <GPU X.Y>
docker run --rm --entrypoint ls <name>:local /opt/gpu-skills
```
(adapt the first line to the framework). Check: versions match the lock, coverage is `native` or an
accepted fallback, the project's modules import inside the image, and `ls /opt/gpu-skills` lists the four
helpers of SKILL step 6, plus `torch_rank.py` for rank mode *(offline only)*. Then parse the exact training argv
with the framework's own argument parser inside the image (a short `python -c` that imports the project's
parser and parses the reviewed `ARGV`, without loading data or a model): a renamed or removed option, or a
value the framework rejects, is found here in seconds instead of on a rented GPU. Run that check the way
SkyPilot runs `run:`: from another directory and without the image's `ENV` variables (SkyPilot's shell in the
container starts in `~/sky_workdir` and does not inherit them), for example
`docker run --rm -w /tmp --entrypoint env <name>:local -i PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin <ARGV with --help>`
(the argv names its interpreter as the task will find it: on that `PATH`, like `python3`, or by absolute
path, like `/opt/venv/bin/python`; not a venv's `python` that only the image's `ENV` puts on the path).
Nothing heavier runs on CPU (no training steps, no fresh-then-resume): that is the GPU qualification task's job.

## 4. Log in and push
```bash
bash -c 'set -euo pipefail; source /abs/provider-hooks.sh; hook_registry_password | docker login <registry host> --username "$(hook_registry_username)" --password-stdin'
TAG=$(date -u +%Y%m%dT%H%M%SZ)
docker tag <name>:local <registry>/<repo>:$TAG && docker push -q <registry>/<repo>:$TAG
docker image inspect <registry>/<repo>:$TAG --format '{{index .RepoDigests 0}}'
```
Check: a `repo@sha256:...` digest; record it as `image.reference` and `image.digest` in experiment.json
(and `image.registry_login: true` for a private registry). `docker buildx imagetools inspect <digest ref>`
resolves it from the registry.

Reuse the digest across runs and recoveries; rebuild only when code or dependencies change, then requalify
(launch runbook: the qualification task).
