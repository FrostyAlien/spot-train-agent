# Control host runbook

Exact commands with a check after each step; stop at the first failed check. Flags are for the SkyPilot
version chosen at the start: confirm them with that version's `--help`, and take `sky` details from the
official `skypilot` skill. The host needs the user's approval (size, region, disk, monthly cost) and
access set up by the user; never ask for secret values in chat. Steps marked *(offline only)* have not run in a live test yet; the rest ran live (`VALIDATION.md`).

Contents: [0 which server](#0-which-server) · [1 operator network](#1-operator-network-laptop) ·
[2 create the host](#2-create-the-host) · [3 client and Docker](#3-install-the-pinned-client-and-docker-on-the-host) ·
[4 cloud access](#4-skypilots-cloud-access) · [5 API server](#5-api-server-consolidated-managed-jobs-control) ·
[6 start checks](#6-start-checks-once-per-chosen-skypilot-version) · [7 hand-off](#7-hand-off) ·
[existing, provider-managed or local server](#an-existing-provider-managed-or-local-server) · [status](#status-any-time)

## 0. Which server
From intake item 15, recorded as `controller.mode` in the contract:
- **`self-hosted`**: a control host the user runs (steps 1-7 below; the SKILL's state-and-restore step only for a long-lived one).
- **`existing`** (a server someone else runs, such as a team's) or **`provider-managed`** (a cloud provider's hosted
  server): no control host; connect to it ([existing server](#an-existing-provider-managed-or-local-server)).
- **`local`**: the API server on the operator's machine (`sky api start`), for development and small runs. Managed
  jobs then run their controller on a cloud VM SkyPilot creates
  ([consolidated or separate](deployment-state.md#consolidated-or-separate-jobs-controller)); budget it. Recovery does not depend on the laptop staying awake.
Without a control host the runbooks run on the operator's machine as the SkyPilot client, long builds and data
copies run on a short-lived CPU VM through the same SkyPilot (image runbook; data skill: staging), and the run's
limits run in the worker (no guard needed).

## 1. Operator network (laptop)
```bash
python3 <skill-dir>/scripts/net_check.py
```
Check: `"verdict": "ok"`. On `proxied` or `slow` (a VPN or proxy relays SSH, which breaks SkyPilot's readiness
probe), create the host with the provider's own CLI or console, not with SkyPilot from this laptop. To launch
from the laptop anyway, set `<cloud>.ssh_proxy_command: nc %h %p` and restart the local API server first (a
cluster that failed in INIT keeps its old config until `sky down`).

## 2. Create the host
A small always-on CPU VM (4 vCPU, 16 GB, a disk sized for image builds and data work in progress) in
any region with CPU quota, created with the provider's CLI or console so its lifecycle is independent of
SkyPilot. Check: `ssh HOST true` and `nproc; free -g; df -h /`.

## 3. Install the pinned client and Docker (on the host)
```bash
sudo apt-get update -q && sudo apt-get install -y -q docker.io python3-venv jq
sudo systemctl enable --now docker
python3 -m venv ~/sky-venv && ~/sky-venv/bin/pip install -q '<skypilot or skypilot-nightly>[<cloud>]==<version chosen at the start>'
```
Check: `~/sky-venv/bin/sky --version` prints the chosen version; `sudo docker info` works.
Nothing else goes into `~/sky-venv`: data preparation and other tools get their own venv or container,
so an install for them never changes the SkyPilot that the API server and the guard run.

## 4. SkyPilot's cloud access
The user places SkyPilot's documented credential files for the cloud (the provider's SkyPilot setup page)
on the host, or approves copying them from the operator's machine. Copy only what SkyPilot's setup page
names for this cloud: from a file shared with other clouds or accounts (a profile file), only that cloud's
profile, never the whole file. Put them into the API server's home and keep them private:
```bash
sudo mkdir -p /opt/skypilot-control/home
sudo cp -a ~/.<cloud-config-dir> /opt/skypilot-control/home/       # each file SkyPilot uses for the cloud
sudo chmod -R go-rwx /opt/skypilot-control/home
```

## 5. API server (consolidated managed-jobs control)
The API server image of the same version, pinned by digest, on loopback only. The
client treats a loopback server as local and does not upload local file mounts to it, so the server
container must see the project and the skills directory at the same path (the finish verify task
mounts the checkpoint helpers from the skills directory); drop the second `-v` when the skills are
inside the project:
```bash
printf 'jobs:\n  controller:\n    consolidation_mode: true\n' | sudo tee /opt/skypilot-control/skypilot.yaml
sudo docker run -d --name skypilot-api --restart unless-stopped --cpus 2 --memory 6g \
  -p 127.0.0.1:46580:46580 -v /opt/skypilot-control/home:/root \
  -v <project dir>:<project dir>:ro -v <skills dir>:<skills dir>:ro \
  -v /opt/skypilot-control/skypilot.yaml:/etc/skypilot.yaml:ro -e SKYPILOT_GLOBAL_CONFIG=/etc/skypilot.yaml \
  --entrypoint tini '<server image>@sha256:...' -- sky api start --deploy --foreground
~/sky-venv/bin/sky api login -e http://127.0.0.1:46580
```
Check: `sky api info` reports a healthy server whose version equals the client's; `sky check <cloud>`
shows the cloud enabled. The server sees the files the tasks mount (a dry run does not check this):
```bash
sudo docker exec skypilot-api ls <project dir>/experiment.json <skills dir>/gpu-training-recover/scripts/checkpoint_marker.py
```
Check: both paths listed.

## 6. Start checks (once per chosen SkyPilot version)
```bash
~/sky-venv/bin/python <observe-skill>/scripts/campaign_guard.py --self-test
~/sky-venv/bin/python <observe-skill>/scripts/campaign_guard.py --check-sdk
```
Check: `{"self_test": "ok"}` and `"ok": true`. The renderer's round trip and the dry run need the image
digest, so they run in the launch runbook once the image is pushed. A local API server on the operator's
machine that has been running since an earlier project is restarted once at the start (`sky api stop; sky
api start`): a long-lived one can fail every request while `sky api info` still says healthy. Whenever a
`sky` command stays silent for two minutes, read the server log (`~/.sky/api_server/server.log` on the
machine that runs it, or `docker logs skypilot-api`) before retrying.

## 7. Hand-off
Copy to the host: the project (or `git clone` it at the recorded commit), `experiment.json`, the provider
hooks file and the project's secrets script (launch runbook), and the skills. Use `~/sky-venv/bin/python`
for every helper that imports SkyPilot (the guard, `render_job.py --validate-with-skypilot`). Run long
work (image builds, data preparation) through `scripts/stage.sh run NAME -- CMD` and `stage.sh wait NAME`,
so an SSH drop never kills it.

## An existing, provider-managed or local server
*(offline only for an existing or local server; a provider-managed one ran live)*
Instead of [steps 2-5](#2-create-the-host) (host, client, credentials, API server). The client must match the server's version (SkyPilot supports only adjacent client and server
versions; a server the user does not run picks its own version):
```bash
python3 -m venv ~/sky-venv && ~/sky-venv/bin/pip install -q '<skypilot or skypilot-nightly>[<cloud>]==<the server version>'
```
The user logs in, in their own terminal, never in chat or in a command the agent runs:
- preferred, if the server offers it: a service-account token with an expiry that covers the campaign
  (`sky api login -e <endpoint> --token <token>`, typed by the user);
- otherwise the server's documented login, for example basic auth with the password inside the URL. The client then
  stores the URL, password included, in `~/.sky/config.yaml`: never print that file, and read `sky api info` only
  through the redaction filter below.
```bash
sky api info -o json | sed -E 's#://[^@]*@#://<redacted>@#g'
sky check <cloud>
sky status -u | grep -i sky-jobs-controller || echo "no separate jobs controller (consolidated, or none yet)"
```
Check: the server version equals the installed client; the cloud is enabled; your login, role and active workspace;
the jobs-controller mode. Record in the contract `controller.endpoint` (the host and port only, no user, password or
query: the validator refuses them) and `controller.server_version`; `--locked` compares it, so a server upgrade
reruns the start checks before a relaunch (requalify only if the release notes touch the recovery path).
- **Cloud credentials are the server's**, not yours: spend lands on whoever owns them; confirm the account, the quota
  you may use and how cost is attributed; never change the server's cloud configuration. A stop by hand uses the
  provider hooks, which need your own provider CLI login on the operator's machine.
- **Shared with other people:** managed jobs of everyone run on the server (or its controller); check its headroom
  ([shared or small control host](commands.md#shared-or-small-control-host)) and its uptime policy. Job lists and logs are visible to other
  users: no secrets in logs or job names. Use only your own jobs and clusters: take the job id `sky jobs launch`
  prints, and cancel by id only, never `-a` or `-u` with `sky jobs cancel` or `sky down`.
- **Server down or restarted by its owner:** the job's VMs keep billing; the worker's limits still stop the run.
  Tell the user; nobody restarts, upgrades or reconfigures a server they do not run.
- **Logs:** a hosted server may keep managed-job logs only for a while (for example 7 days); the finish runbook
  saves them before that.
- **`hf://` mounts** of a private or gated dataset need the HF token on the server's host, which you cannot reach:
  the data loader reads `HF_TOKEN` from the launch's secrets instead (data skill: staging).
- **The optional guard** (`monitoring.guard: external`) runs where observe `references/guard.md#campaign-guard` says,
  with a login that may cancel your job in the same workspace.
- The SKILL's state-and-restore step (backup and restore) does not apply to a server the user does not run.

## Status, any time
```bash
sudo docker ps --filter name=skypilot-api; sky api info
systemctl list-units --type=service 'campaign-guard-*'; df -h /
```
Between runs, stop (not delete) the host when no job or guard is active; deleting it needs the user's
approval and loses the API server state (a long-lived or shared control plane: inventory and backup first,
the SKILL's state-and-restore step).
