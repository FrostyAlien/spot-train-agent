# Nebius profile (dated starting point)

A starting point for the provider research ([provider-research.md](../provider-research.md)) when the provider is
Nebius, not a maintained fact sheet. Every row has a check date; re-check each one you use against the source or
the live account, and record the result in the project's provider record, as for any provider.

## Managed SkyPilot API server
Checked 2026-10-08 (Nebius docs and tutorials); used live 2026-10-21 (login, `sky api info`, CPU and GPU launches).
- Nebius offers a hosted SkyPilot API server, one per account, in Preview: no SLA; the terms say free during
  Preview while the tutorials list its VM as billed (check the billing page). Use it as `controller.mode:
  provider-managed` (controller runbook: existing server).
- Version: chosen by Nebius, not published; read it with `sky api info` and install that exact client.
- Login: basic auth with the password in the URL (the client stores it in `~/.sky/config.yaml`): the user logs in
  in their own terminal, and `sky api info` is read only through the redaction filter. Service-account tokens:
  not documented for it.
- Jobs controller: probably consolidated (the default for a remote server when unset); check `sky status -u`.
- The server also lists every Managed Kubernetes cluster of the project: keep `infra: nebius/<region>` (the renderer
  does), so jobs stay on VMs.
- A configuration change restarts it, and a restart interrupts pending and running requests; managed-job logs are
  kept 7 days by default.

## Research rows, as a dated starting point
| Item | Finding | Checked |
|---|---|---|
| Spot semantics | Preemptible VMs get SIGTERM about 60 s before a STOP; SkyPilot then recovers (FAILOVER first restarts the stopped VM) | live 2026-10-02 |
| Spot pricing | Spot VMs need a pricing model: `follows_spot_price` (no maximum) or `spot_pricing_policy` (a maximum, below). SkyPilot 0.14.0 and later write `follows_spot_price`; older stable releases cannot launch Nebius spot VMs since 2026-10-08 | source 2026-10-21 |
| Spot price | Set by Nebius per GPU platform, changes as often as every 15 minutes, always below the pay-as-you-go price: `budget.ceiling_hourly_usd` at the on-demand rate is a safe bound | docs 2026-10-21 |
| Billing | Per second; a stopped VM bills no compute (its disks still bill), so `budget.free_without_vm: true` | docs 2026-10-02 |
| Storage | Object storage with an S3-compatible endpoint per region; `nebius://` and `s3://` mounts work with SkyPilot (`MOUNT_CACHED`, `MOUNT`, `COPY`); `jobs.bucket` rejects `nebius://` (#10387) | live 2026-10-02 |
| Registry | `cr.<region>.nebius.cloud/<registry>`; workers pull with `SKYPILOT_DOCKER_USERNAME`/`PASSWORD`/`SERVER` as secrets (user `iam` and an IAM token valid about 12 h, or a static key for longer runs) | live 2026-10-02 |
| Account and quota | One default project per region; a region can allow no CPU-only vCPUs (a control host or build VM then goes to another region) | live 2026-10-02 |
| Credentials | SkyPilot reads `~/.nebius/credentials.json` (service account) and copies it to its VMs by default; CLI tokens last about 12 h, so use a service account for unattended control | docs 2026-10-02 |
| Known issues | #10965 "No project found for region eu-west1" on 0.14.0; #10387 above | 2026-10-08 |

Hooks for these operations: [nebius-hooks.sh](../../assets/providers/nebius-hooks.sh), checked with Nebius CLI
0.12.283; re-test each read-only one in the start checks ([provider hooks](../provider-hooks.md)).

## Optional spot price cap *(offline only)*
A pricing policy holds a maximum spot price per GPU-hour for one GPU platform in one project; above it Nebius stops
the VM. Its price can change only while no running VM uses it. Only some platforms take a policy, and the set depends on the region (live 2026-10-21, eu-north1: only
`gpu-h100-sxm` and `gpu-h200-sxm`; `gpu-l40s-a` was refused with `PlatformNotAvailable`). When the GPU is
chosen, check Nebius's pricing-policy docs for the chosen platform and region before the cap goes into the plan.
The `create` below is the final check: a `PlatformNotAvailable` refusal means no cap on this platform; tell the
user. With the user's approval, before the run:
```bash
nebius billing pricing-policy create --parent-id <project id> --name <run id>-cap \
  --compute-instance-spec-v1-platform <gpu platform> --pricing-max-price-v1-max-price <price per GPU-hour>
```
Record the returned ID as `budget.spot_price_cap.cap_id` (and the price as `per_gpu_hour`); the worker then caps its
own VM ([how, and the one-time proof](../provider-hooks.md#optional-spot-price-cap)). On Nebius that proof covers the
hook's update (`compute instance update --patch --spot-pricing-policy-id <id> --stopped` in nebius-hooks.sh), the
VM's own credentials, SkyPilot's restart of the stopped VM and the cap on the restarted VM.

## Nebius's own agent skills
`nebius/skills` (public since 2026-10-02) has billing, capacity-quotas, compute-inventory and public-docs skills,
useful for the research rows above; load them selectively. Never its compute-provision or serverless-jobs skills
while this suite drives a run ([optional official provider skills](../provider-research.md#optional-official-provider-skills)).
