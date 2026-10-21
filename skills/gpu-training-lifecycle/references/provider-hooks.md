# Provider hooks

The runbooks are provider-neutral. The few operations that need the provider's own API live in one
project file, `<project>/provider-hooks.sh`, that the agent writes from the provider research record
(and the provider's official agent skills when used). It defines bash functions; runbook steps source it
(`bash -c 'source "$1"; hook_...' _ /abs/provider-hooks.sh`) and call only the functions they need. Start from
[the template](../assets/provider-hooks.example.sh). Keep it under version control with the project; it holds
commands, never secret values (read them from the provider CLI's own login or files at call time).

| Function | Arguments | Prints | Used by |
|---|---|---|---|
| `hook_registry_username` | — | registry user name | image runbook (push), the project's secrets script (when `image.registry_login` is true) |
| `hook_registry_password` | — | a registry token valid for at least the campaign's recovery window (a short-lived token is fine for a run shorter than its lifetime; the guard's fallback re-reads it) | same |
| `hook_instance_ids` | SkyPilot cluster name (`current_cluster_name` in `sky jobs queue -v -o json`; when the queue leaves it empty, as under consolidated jobs control, the managed job's cluster is `<job name>-<job id>`, the job name shortened and hashed by SkyPilot when long) | IDs of the provider VMs of that cluster, one per line, stopped ones included (a stopped VM still bills its disk and SkyPilot may restart it); for a running job, an empty list means the hook or the name is wrong, never that nothing bills (while the job waits for capacity, empty is the guard's no-VM case). SkyPilot derives VM names from the cluster name, truncated to the provider's limit and suffixed: match the truncated prefix or SkyPilot's VM labels, and test it on a real cluster | recover runbook (find the replacement VM), manual stop, the guard's `vm_count_cmd` (waiting without a VM is free under `budget.free_without_vm`) |
| `hook_instance_stop` | one VM ID | nothing; requests a stop through the provider API (stopping by hand when SkyPilot is unreachable) | manual stop |
| `hook_inventory` | — | resources that still bill or linger in the run's project/region (VMs, disks, IPs, and the security groups and other network resources SkyPilot created per cluster, which it may leave behind), one per line; empty = none | finish runbook |
| `hook_storage_delete` | an object-store URI prefix ending in `/` | nothing; deletes every object under it | finish runbook (pruning approved checkpoint directories) |
| `hook_price_cap` *(optional, offline only)* | the provider's price-cap object (for example a pricing-policy ID) | `capped` when this VM already runs under the cap; `switched` after it put this VM under the cap and asked the provider to stop it (so SkyPilot restarts the same VM, now capped) | the entrypoint's wrapper at each start ([spot price cap](#optional-spot-price-cap)) |

## Optional spot price cap
Some providers let a spot VM carry a maximum price (above it the provider stops the VM instead of billing more),
but SkyPilot creates spot VMs without one and restarts a stopped VM without changing its pricing. Where the provider
allows changing a stopped VM's pricing, and SkyPilot's FAILOVER recovery restarts the stopped VM after a stop, each
new VM can cap itself: before any training the wrapper calls `hook_price_cap <cap id>` *inside the worker*, so the
hook and its provider CLI or API call are in the image, and the VM's credentials (SkyPilot copies the cloud's
credentials to its VMs by default) must allow updating that VM. Contract: `budget.spot_price_cap`
(`per_gpu_hour`, `cap_id`), created by the user or with their approval before the run. `switched` costs one restart
per new VM and no progress (nothing trained yet); those starts never count as allocations without progress. A cap
below the current price keeps the VM from starting until the price drops: a capacity wait. The spend bound keeps the
on-demand ceiling. Only the recovery strategy FAILOVER restarts the same VM; with EAGER_NEXT_REGION (a fallback
without the guard) every recovery is a new VM that caps itself again. Prove it once on the provider (a small GPU spot
VM, a few minutes) before relying on it.

Rules: each function is idempotent and quiet on success, writes errors to stderr and exits nonzero on
failure; it must not print secrets except `hook_registry_password`, whose output the scripts pipe straight
into the consumer. Test each one read-only (or on a throwaway resource) during the start checks.
