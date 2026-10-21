# Provider research

Provider facts (presets, spot behaviour, pricing, adapters) change faster than skills, and any provider SkyPilot supports may be chosen. Dated profiles exist for some providers (`references/providers/`: Nebius) as a starting point; every other provider is researched from scratch, and a profile's rows are re-checked like any finding. When the user picks a provider, and again whenever the SkyPilot version changes, research these items from the provider's current official documentation and the adapter source of the SkyPilot version chosen at the start. Record the findings with the check date, version and source in the project (for example `<project>/provider-notes.md`, linked from the launch evidence), not in the skills. Label each item documented, locally checked or live tested; an unknown item stays unknown and blocks only the step that depends on it. When the plan relies on a profile row, it cites that row with the date and source of your re-check from the project's record; a row you did not re-check counts as unknown.

| Item | What to find out | Used by |
|---|---|---|
| GPU offers | Platforms/presets for the GPU and count, which regions list them, whether counts are sold only as fixed presets (then record `gpu.instance_type`) | selection, launch |
| Spot semantics | Product name, notice period and signal, stop vs delete on preemption, what survives (boot disk, IP, local SSD), restart rules | recovery, campaign guard |
| Spot pricing in the adapter | The exact request fields the chosen SkyPilot version's adapter sends for spot; whether the provider needs a pricing model or bid; the highest price a spot VM can bill | capability gate, `budget.ceiling_hourly_usd` |
| Billing | Granularity, what bills while stopped, whether compute bills only while a VM exists (waiting for capacity free), the node's on-demand rate | `budget.ceiling_hourly_usd`, `budget.free_without_vm`, finish |
| Durable storage | Object storage service and endpoint format, whether SkyPilot can mount it (`MOUNT_CACHED`/`MOUNT`), same-region egress cost, whether storage is shared across regions at zero cost | checkpoint protocol, region policy |
| Credentials | How SkyPilot authenticates to the provider (its cloud-setup page), a service-account option for unattended control and its token lifetime | credentials, launch |
| Registry | Registry host format and how workers authenticate image pulls | image |
| Direct stop | The provider CLI command that stops a VM, and how to find the job's VMs from SkyPilot's cluster name (VM name prefix or label), for stopping by hand when SkyPilot is unreachable | manual stop, finish |
| Account state | Billing/payment status and whether the account, tenant or projects are active (not suspended), read-only | start checks |
| Account model | How projects/subscriptions map to regions, and which one SkyPilot will use (its config keys) | credentials, launch |
| Quota, every resource | Quota for **every** planned resource in its region: GPUs and spot instances, but also CPU-only vCPUs for the control host and builder, disks, buckets, registries, public IPs; a GPU region can allow no CPU-only VMs | selection, controller |
| Operator network | Whether SkyPilot can reach new VMs from the operator's machine: run the controller skill's `scripts/net_check.py`; a proxied result means create the control host from the cloud side or set the SSH proxy command first | controller bootstrap |
| Known issues | Adapter or mount issues (for example image contents that break SkyPilot mounts), with links and the version checked | image, launch |
| Official agent skills | Whether the provider publishes official agent skills (a GitHub repo or its docs); if so, record repo, commit, license and CLI version floor, and use them as below | research, finish |

From these findings, write the project's **provider hooks** file ([provider-hooks.md](provider-hooks.md)): the few provider operations the runbooks need (registry login, find and stop a job's VMs, list what still bills). That keeps every provider command in the project, next to its research record.

## Optional official provider skills
Some providers publish agent skills (for example a provider's docs, quota/capacity, billing-calculator, inventory or service-account skills). When the chosen provider has them, they may speed up the rows above:
- Load selectively: read-only, documentation, quota/capacity, pricing-calculator, inventory and credential-setup skills. Never load a provider's own job/executor/provisioning/serving skills while this suite drives the run: they compete for the same requests and bypass SkyPilot, the guard and the checkpoint contract.
- Pin the commit (and the provider CLI version) at the start and keep it for the run, like the SkyPilot version.
- Precedence: the experiment contract and these skills' gates, then SkyPilot's documented behaviour, then the add-on's guidance. A stricter add-on rule (for example refusing deletes or credential issuance) wins: the user runs that command; an add-on rule that conflicts with the executor loses and is recorded.
- Never delegate to an add-on: launch or purchase decisions, recovery and checkpoint selection, budget enforcement, deletion, secret values, provider or region changes.
- Record each finding's source as `add-on:<skill>@<commit>` in the provider record, and list the add-ons used in the closure report.
