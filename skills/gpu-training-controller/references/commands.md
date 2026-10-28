# Controller operation commands
Checked 2026-10-01. Commands are recipes, not authorization. Use exact pinned binaries/images, verified endpoint and dedicated campaign paths; never paste secrets into reports.

## Inspect and connect
```bash
python -c 'import sky; print(sky.__version__)'
sky api start --help
sky api info
sky status -u
```
If login is needed and permitted, the user runs `sky api login -e https://verified-control.example.org` in their own terminal, with the recorded actual endpoint (a password or token is never typed by the agent; redaction: [runbook](runbook.md#an-existing-provider-managed-or-local-server)). Then compare `sky api info` (health, version, commit) with the version lock. Do not silently create fresh cloud credentials or service accounts. Check selected existing cloud access with `sky check PROVIDER` only after choosing the correct provider/account; it updates enabled-cloud cache.

## Shared or small control host
Total host capacity is not free headroom. Before deployment inspect existing service CPU/RSS, peak load, available RAM, disk capacity/IOPS and network load over representative use. On Linux, `free -h`, `df -h`, `uptime`, and available process/container metrics are read-only starting points. Agree with the host owner on a CPU/memory/disk budget that preserves existing service headroom; avoid changing their settings or stopping their processes. Do not claim this host can support a controller until a representative one-job launch/recovery test fits that budget.

The runbook starts the container with explicit CPU and memory limits (`--cpus`, `--memory`) rather than giving it the whole host. Verify effective limits with the installed runtime and its cgroup support. SkyPilot `--deploy` assumes a dedicated host and may size worker pools aggressively: on a shared host, confirm the pinned release observes container limits and tune supported concurrency settings. If it cannot fit safely, choose a separate small non-spot host with approval rather than destabilizing existing services. Do not solve controller OOM by removing resource caps indiscriminately.

## State, restart and teardown
Consolidated control shares the API server's lifetime; a separate controller has its own (modes: [deployment and state](deployment-state.md#consolidated-or-separate-jobs-controller)); inspect it with `sky status -u`. `sky jobs queue -r` is not cost-free observation: it may restart a stopped controller and incur charges. `sky api stop` affects the local API server, not an arbitrary remote endpoint.

Before an approved restart/upgrade, follow the [backup procedure](deployment-state.md#backup-and-upgrade-procedure) for the state listed in controller-state-inventory.json. A `cp` of a live SQLite file is not a backup. Never run a restored clone against real resources in parallel.

After every managed job is terminal, logs/checkpoints are preserved, and cleanup permission covers the exact controller, the separate-controller command is `sky down CONTROLLER_NAME`. For the runbook's container, `sudo docker stop skypilot-api` stops only that container after approval and preserves its mounted files. Stopping Docker does not cancel training or guarantee no cloud charges. Avoid volume-deleting flags and never shut down unrelated host services.

Sources: https://docs.skypilot.ai/en/latest/reference/cli.html ; https://docs.skypilot.ai/en/latest/examples/managed-jobs.html ; https://docs.skypilot.ai/en/latest/reference/api-server/examples/api-server-in-docker.html ; https://docs.skypilot.ai/en/latest/reference/api-server/api-server-upgrade.html
