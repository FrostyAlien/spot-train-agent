# Control host runbook

Exact commands with a check after each step; stop at the first failed check. Flags are for the SkyPilot
version chosen at the start: confirm them with that version's `--help`, and take `sky` details from the
official `skypilot` skill. The host needs the user's approval (size, region, disk, monthly cost) and
access set up by the user; never ask for secret values in chat. Steps marked *(offline only)* have not run in a live test yet; the rest ran live (`VALIDATION.md`).

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

## Using an existing or shared API server *(offline only)*
Instead of steps 2-5, a run may use an API server that already exists, possibly shared with other people.
Check, and record in the controller plan:
- **Version:** `sky api info` server version equals the client chosen at the start; record it, and check it
  again before every launch: another user may upgrade the server mid-run (that changes the recovery path;
  pause and rerun the start checks).
- **Identity and workspace:** your login, its role, and the active workspace (`sky api info`); the guard
  must use a login that may cancel your job, in the same workspace.
- **Cloud credentials are the server's**, not yours: spend lands on whoever owns them; confirm the account,
  the quota you may use and how cost is attributed; never change the server's cloud configuration.
- **Consolidated control on a shared host:** managed jobs of everyone run as processes on the server host;
  check its headroom (commands reference: shared or small control host) and its uptime policy.
- **The guard** still runs on a host you control with a durable ledger and a notify command; the job's
  logs and queue are visible to other users of the server: no secrets in logs or job names.
- Do not restart, upgrade or reconfigure a shared server; ask its owner.

## Status, any time
```bash
sudo docker ps --filter name=skypilot-api; sky api info
systemctl list-units --type=service 'campaign-guard-*'; df -h /
```
Between runs, stop (not delete) the host when no job or guard is active; deleting it needs the user's
approval and loses the API server state (a long-lived or shared control plane: inventory and backup first,
SKILL step 8).
