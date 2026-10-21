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

Rules: each function is idempotent and quiet on success, writes errors to stderr and exits nonzero on
failure; it must not print secrets except `hook_registry_password`, whose output the scripts pipe straight
into the consumer. Test each one read-only (or on a throwaway resource) during the start checks.
