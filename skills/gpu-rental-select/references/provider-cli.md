# Provider CLI: what to look up, read-only

The skills keep no per-provider commands: CLIs change faster than skills. For the chosen provider, find the commands below in its current official CLI reference (or its official agent skills, `gpu-training-lifecycle` → `references/provider-research.md`), read the installed version's `--help`, and record the exact commands with the CLI version and date in the project's provider record. Commands are examples for read-only checks, not permission to install, sign in, create credentials, purchase, upload, stop or delete.

## Prerequisites and first check

Identify the execution machine and its existing CLIs. If missing, report the missing prerequisite and use the official installation instructions only after authorization. Use existing authentication for read-only checks. If it is expired/missing, pause for the appropriate secure setup rather than creating an API key or logging in silently. Read-only account commands may return private account/balance data; keep outputs private and redact unrelated fields in artifacts.

The training provider and controller provider are separate decisions. Reuse an authorized inexpensive non-spot CPU controller if appropriate; record its account/project/region/resource ID separately. Never rent a GPU as the default controller or transfer datasets through it.

## Read-only checks to find for any provider
| Check | Why | Caution |
|---|---|---|
| CLI version and active profile/account | pin the version; confirm the approved account, project and region | a profile listing does not prove live access |
| Account/billing state | a suspended or unpaid account cannot create anything | read-only status commands only |
| GPU platforms/presets per region | GPU count, allocated CPU/RAM, whether spot is allowed for the preset | a listing is not a capacity reservation |
| Quota allowances, every resource type | GPUs, spot instances, CPU-only vCPUs (control host, builder), disks, buckets, registries, IPs, per region | a GPU region may allow no CPU-only VMs |
| Capacity advice, if the provider has it | current availability for the preset and purchase mode | advice, not a guarantee |
| Price calculator, if the provider has one | the on-demand list rate for `budget.ceiling_hourly_usd`; cross-check SkyPilot's catalog | never create a resource to obtain a price |
| Instance list/get by ID or name prefix | find a job's VMs from SkyPilot's cluster name; verify cleanup | IDs differ from offer or machine IDs on marketplaces |

Avoid commands whose names suggest diagnostics but that configure credentials or SSH keys (some CLIs' `doctor`/`init`/`setup`). Spot pricing policies and bids are provisioning decisions, not discovery. A marketplace's search-offer filters, storage units and contract expiry need the provider's own reference. The operations the runbooks need (registry login, find/stop a job's VMs, list what still bills) go into the project's provider hooks (`gpu-training-lifecycle` → `references/provider-hooks.md`).

## Lifecycle handoff

Selection changes nothing; cleanup is `gpu-training-finish`.
