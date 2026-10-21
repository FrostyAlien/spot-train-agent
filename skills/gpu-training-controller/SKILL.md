---
name: gpu-training-controller
description: Plan, deploy, maintain, or recover an independent control plane (the always-on control host with the SkyPilot API server and the campaign guard) for occasional GPU training campaigns. Use when the user needs a control host, a SkyPilot API server, a shared server check, or to restore control-plane state.
---

# GPU training controller

Plan, deploy, maintain or recover an independent SkyPilot (or approved equivalent) control plane that keeps managing a campaign after worker loss or client disconnect. The same client must work against a local Docker API server (development) and an always-on remote or compatible provider-managed endpoint. Bundled `scripts/` are relative to this skill's directory (`<skill-dir>`); the operator machine is the laptop that creates and reaches the control host.

## Inputs
- `experiment.json`, selected provider and region, expected concurrency, current tool versions.
- Available reliable CPU hosts and the approved spending scope.

## Steps
Follow the [runbook](references/runbook.md); stop at the first failed check.
1. **Check the operator network** (runbook §1).
2. **Create the control host** (runbook §2) once host permissions and spending approval cover it: a reliable non-spot host, independent of the training worker, chosen by total campaign cost and measured headroom ([deployment and state](references/deployment-state.md)).
3. **Install Docker and the pinned client** (runbook §3). Pin versions before writing executable commands: matching client/server, images by digest (never `latest`), schema/help checked against that version (no latest-docs fields on an older stable), release or exact nightly evidence recorded.
4. **Place SkyPilot's cloud credentials** (runbook §4): the user places them (`gpu-training-lifecycle` → `references/credentials.md`); reuse the verified provider/account and managed-server login; never create credentials.
5. **Start the API server** (runbook §5): the same version pinned by digest, on loopback, with consolidated jobs control (the other mode: deployment and state); no public ingress. Parameterize the endpoint and verify it with `sky api info` and `sky check` before job operations.
6. **Run the start checks** (runbook §6): the guard self-test and SDK check; the renderer's round trip runs in the launch runbook.
7. **Hand off** (runbook §7). Run long control-host work (builds, data) through `scripts/stage.sh run NAME -- CMD` and `stage.sh wait NAME`, so an SSH drop never kills it. Report which checks actually ran.
8. *Optional.* **Long-lived or shared control plane only:** plan, state inventory, encrypted backup and isolated restore, DR acceptance ([deployment and state](references/deployment-state.md), [acceptance](references/acceptance.md)). The plan covers the private endpoint, restart supervisor, durable state mount, log retention, health checks and backup/restore. Before upgrading such a server, back up and rehearse a restore too.

## Outputs
- A version lock: client package, server image digest, chart version if used, and checked source URLs.
- After authorized deployment: verified endpoint, state location and resource IDs. Never store credentials in the manifest.
- Step 8 only: `controller-plan.md`, `controller-state-inventory.json` and the restore-test result.

## Gates
- **Project-first decisions:** before asking the user anything or deciding for them, read `gpu-training-lifecycle` → `references/working-rules.md` (interaction levels included).
- Manifest approval fields are records, not authority. Plan without a budget; paid deployment needs the specific provider, operation, spending cap and retention authorized. Credential entry, new keys or grants, persistent access, rotation and destructive actions need the host's permission and secure user entry.
- Keep control available while training/recovery is active; local client exit must not end the recovery controller. Persist Docker state, but an ephemeral sandbox or sleeping laptop is not a production host.
- Provider lock, spot-only default (`gpu-training-lifecycle` SKILL.md gates) and checkpoint storage (`gpu-training-lifecycle` → `references/gates.md#acceptance-gates-by-area`) are lifecycle decisions. A control host elsewhere needs explicitly designed credentials, data flows and egress.
- The controller DB and backups are secret-bearing (job specifications submitted with `secrets:` are stored there): encrypt them, restrict access, never copy them into evidence reports.
- Broad controller credentials stay off workers; record how replacement workers reacquire only necessary access.
- Never resubmit blindly: after any restart, reconcile existing jobs and cloud resource IDs first. Fence the old controller before restoring a replacement; two controllers must not own the same campaign.
- Never delete a controller that still owns active jobs; preserve shared/reusable authentication during cleanup.

## References
- [runbook.md](references/runbook.md): read before creating or setting up the control host (exact commands and checks).
- [deployment-state.md](references/deployment-state.md): read when choosing host or mode, or planning security, remote access, persistence, backups or upgrades.
- [commands.md](references/commands.md): read for diagnostics, shared-host sizing and teardown (the runbook has the deployment commands).
- [acceptance.md](references/acceptance.md): read for step 8's DR acceptance or when verifying a restore.
