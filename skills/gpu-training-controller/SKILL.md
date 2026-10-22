---
name: gpu-training-controller
description: Chooses, connects, deploys, maintains or recovers the SkyPilot API server for occasional GPU training campaigns: a control host the user runs, an existing team or provider-managed server, or a local one. Use when the user needs a SkyPilot API server or control host, connects to an existing or hosted server, checks a shared server, or restores control-plane state.
---

# GPU training controller

Plans, deploys, maintains or recovers a SkyPilot (or approved equivalent) control plane that keeps managing a campaign after worker loss or client disconnect. The same client works against a local API server (development) and an always-on remote or provider-managed endpoint; bundled `scripts/` are relative to this skill's directory (`<skill-dir>`), and the operator machine is the laptop that creates and reaches the control host.

## Inputs
- `experiment.json`, selected provider and region, expected concurrency, current tool versions.
- Available reliable CPU hosts and the approved spending scope.

## Steps
Follow the [runbook](references/runbook.md) in order: it holds each command with its check. Stop at the first failed check.
0. **Choose the server.** Record `controller.mode` from the intake's SkyPilot API server answer ([which server](references/runbook.md#0-which-server)); for any mode but `self-hosted`, [connect](references/runbook.md#an-existing-provider-managed-or-local-server) instead of creating and setting up a host.
1. **Check the operator network.** Run `scripts/net_check.py` on the laptop ([runbook](references/runbook.md#1-operator-network-laptop)). A VPN or proxy that relays SSH breaks SkyPilot's readiness probe, so on `proxied` or `slow` create the host from the provider's side.
2. **Create the control host** once host permissions and spending approval cover it ([runbook](references/runbook.md#2-create-the-host)). Choose a reliable non-spot host, independent of the training worker, by total campaign cost and measured headroom ([choosing a control host](references/deployment-state.md#choosing-a-control-host)), so losing the worker never loses control. A host at another provider than the worker needs explicitly designed credentials, data flows and egress.
3. **Install Docker and the client** at the server's version, with the server image pinned by digest and flags taken from that version's `--help` ([runbook](references/runbook.md#3-install-the-pinned-client-and-docker-on-the-host); lifecycle `references/gates.md#skypilot-version-chosen-at-the-start-kept-for-the-run`).
4. **Have the user place SkyPilot's cloud credentials** ([runbook](references/runbook.md#4-skypilots-cloud-access); `gpu-training-lifecycle` → `references/credentials.md`). Reuse the verified provider account and managed-server login; never create credentials.
5. **Start the API server** ([runbook](references/runbook.md#5-api-server-consolidated-managed-jobs-control)) at the same version, pinned by digest, on loopback with no public ingress, with consolidated jobs control ([consolidated or separate](references/deployment-state.md#consolidated-or-separate-jobs-controller)). Parameterize the endpoint and verify it with `sky api info` and `sky check` before any job operation.
6. **Run the start checks** once per SkyPilot version, with the commands in the [runbook](references/runbook.md#6-start-checks-once-per-chosen-skypilot-version). The renderer's round trip runs later in the launch runbook.
7. **Hand off** ([runbook](references/runbook.md#7-hand-off)). Run long control-host work (builds, data) through `scripts/stage.sh run NAME -- CMD` and `stage.sh wait NAME`, so an SSH drop never kills it. Report which checks actually ran.
8. **Plan state and restore** *(optional: a long-lived or shared control plane only)*. Write the plan and state inventory, take an encrypted backup and prove it with an isolated restore and DR acceptance ([plan](references/deployment-state.md#plan-for-a-long-lived-or-shared-control-plane), [backup and upgrade](references/deployment-state.md#backup-and-upgrade-procedure), [acceptance](references/acceptance.md)). Before upgrading such a server, back up and rehearse a restore too.

## Outputs
- A version lock: client package, server image digest, chart version if used, and checked source URLs.
- After authorized deployment: verified endpoint, state location and resource IDs. Never store credentials in the manifest.
- The state-and-restore step only: `controller-plan.md`, `controller-state-inventory.json` and the restore-test result.

## Gates
- **Read `gpu-training-lifecycle` → `references/working-rules.md` before asking the user anything or deciding for them:** it sets the interaction levels.
- **Spend nothing until the user authorizes the specific provider, operation, spending cap and retention:** manifest approval fields are records, not authority. Planning needs no budget. Credential entry, new keys or grants, persistent access, rotation and destructive actions need the host's permission and secure user entry.
- **Keep control available while training or recovery is active,** so a local client exit never ends the recovery controller. Persist Docker state; an ephemeral sandbox or a sleeping laptop is not a production API server.
- **Provider lock, the spot-only default and checkpoint storage are lifecycle decisions** ([lifecycle gates](../gpu-training-lifecycle/SKILL.md#gates), `gpu-training-lifecycle` → `references/gates.md#acceptance-gates-by-area`): the controller carries them out and never changes them.
- **Treat the controller DB and backups as secrets,** because job specifications submitted with `secrets:` are stored there: encrypt them, restrict access, never copy them into evidence reports.
- **Keep broad controller credentials off workers,** because a worker's disk is not trusted; record how replacement workers reacquire only the access they need.
- **Never resubmit blindly:** after any restart, reconcile existing jobs and cloud resource IDs first. Fence the old controller before restoring a replacement, because two controllers must not own the same campaign.
- **Never delete a controller that still owns active jobs;** preserve shared or reusable authentication during cleanup.

## References
- [runbook.md](references/runbook.md): read before creating, connecting or setting up the server; it holds every command with its check, in order.
- [deployment-state.md](references/deployment-state.md): read when choosing the host or jobs-controller mode, or planning security, remote access, persistence, backups or upgrades; it holds the rules and their sources.
- [commands.md](references/commands.md): read for diagnostics, shared-host sizing and teardown; it holds the commands outside the deployment.
- [acceptance.md](references/acceptance.md): read before the state-and-restore step's DR acceptance or when verifying a restore; it holds the offline and live checks and the completion checklist.
