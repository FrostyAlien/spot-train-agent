# Exact-ID provider lifecycle reference

No per-provider commands are kept here. For the chosen provider, record in the project's provider record the read-state, stop and delete commands for every resource type the run created (VMs, disks/volumes, IPs, buckets, registries), with the CLI version and date; the provider hooks hold the VM find/stop and the inventory used by the runbooks.

These are command syntax, not authority:
- Run them only with an already authorized CLI/profile, the exact verified resource ID, the applicable action permission and the artifact/data-loss checks from the SKILL's gates.
- Inspect the installed version's help first.
- Never suppress confirmation prompts without authority or treat a failed read as a successful deletion; a bulk delete built from inventory output breaks the SKILL's exact-ID gate.

Semantics to establish per provider before acting:
- Stop through the provider's API or CLI: a guest `shutdown`/`halt` can reboot the VM or keep it billing on some providers.
- Which disks are managed with the VM (deleted with it) and which are standalone volumes retained and billed separately; inspect the actual attachment policy before approval.
- Marketplaces distinguish the offer ID from the returned instance/contract ID; never use an offer ID as a cleanup target. Stopping may retain billable disk; destroying removes instance data.
- Container disk vs volume disk vs network volume: deleting the instance may leave network volumes and their bill.

## Checks after each mutation
After an authorized stop, poll that exact ID until provider-reported stopped/exited state or a bounded timeout requiring diagnosis. After deletion, confirm terminal operation success and inventory absence in the verified same account/project/region; distinguish not-found from authorization/network failure. Inventory disks, snapshots, volumes, public IPs and controller separately. Keep each residual item, owner, retention decision and hourly/monthly charge in the closure report. Verify the provider's exact official lifecycle commands and disk-retention effects before emitting a command.
