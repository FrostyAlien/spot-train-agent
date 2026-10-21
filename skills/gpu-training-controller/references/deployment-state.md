# Deployment/state reference
Checked 2026-10-01 (remote access 2026-10-02); recheck against the actual version before executing.

## Consolidated or separate jobs controller
An explicit `jobs.controller.consolidation_mode` wins (`false`: a separate controller; restart the API server after a change). If unset, a `--deploy` server consolidates unless jobs-controller clusters exist, but a **local** API server never does: the first managed job launches a controller VM on an enabled cloud (default about 4 vCPU, 50 GB disk, 10-minute idle autostop); budget it. Consolidated, jobs monitoring runs on the API server host, so its uptime is the recovery's uptime; tune the API server, not the remote-controller resource and autostop settings. Never silently migrate an active controller: the migration cancels running jobs. Confirm in the version chosen at the start (`sky/jobs/utils.py::setup_consolidation_mode_on_startup`).
Sources: https://docs.skypilot.ai/en/latest/examples/managed-jobs.html and https://docs.skypilot.ai/en/latest/reference/config.html

## Security
The runbook follows the official Docker example (versioned image, `tini`, `sky api start --deploy --foreground`) but binds the port to loopback and mounts a dedicated control home, never the user's whole home or the Docker socket. Treat the control home as secret-bearing.

Remote access goes only through an SSH tunnel or authenticated TLS; never embed credentials in endpoint URLs or logs. VM deployment is unauthenticated by default. Authentication configured by a reverse proxy must not be bypassable at the backend. The default RBAC new-user role is admin, and documented RBAC depends on SSO. Avoid granting trainer API access unless required (`api_server_access: false`). On Kubernetes follow SkyPilot's Helm docs.
Sources: https://docs.skypilot.ai/en/latest/reference/api-server/examples/api-server-in-docker.html ; https://docs.skypilot.ai/en/latest/reference/api-server/api-server-admin-deploy.html ; https://docs.skypilot.ai/en/latest/reference/auth.html

## No resident-agent dependency
Run the API server, persistent state and the campaign guard with explicit restart/service policies; a conversation or AI polling loop is not the recovery mechanism. The scheduler restores compute; the reviewed container entrypoint and the project's training adapter restore scientific state. Store their immutable script/config hashes and test their state transitions. A deployment is incomplete until those exist, not when these skills or a SkyPilot YAML are installed. A restart policy is not disaster recovery.

## Choosing a control host
Prefer a reliable non-spot control host independent of the training worker. Compare total campaign cost: an existing login host or modest CPU Docker deployment may beat a new Kubernetes estate. Use existing Kubernetes/Helm when its reliability and operations are already available. Size for the selected release and measured workload, not a guessed micro-VM minimum. On a shared host, measure free headroom and existing-service peaks, set tested resource limits, and avoid interference; total host capacity is not available capacity (procedure in `commands.md` → shared or small control host).

## Backup and upgrade procedure
Versions differ on what state persists. Inventory the installed version's DB and files in `controller-state-inventory.json` (DB tables, job YAML/uploads, SSH identities, credentials/config, controller logs, filesystem state); only an isolated restore proves a backup.

Back up a consistent DB state with all of these; a DB alone is not a verified backup. Encrypt protected backups, define retention and rehearse isolated restore with cloud mutations blocked. Preserve controller-owned identity only through an explicitly approved, access-controlled encrypted backup or a documented secure reacquisition path; include that protected identity in the restore test.

Before upgrades: stop new submissions, inventory active requests/jobs, back up, choose a maintenance window and verify compatibility. Upgrades interrupt requests: keep exact image/client/chart pins and configuration; a downgrade may need a matching DB snapshot, not merely the old image. For occasional work, prefer idle upgrades over experimental rolling deployment. After an ambiguous submission response, find the original request/job ID and provider resources before a retry (reconciling and fencing: SKILL.md gates).
Source: https://docs.skypilot.ai/en/latest/reference/api-server/api-server-upgrade.html
