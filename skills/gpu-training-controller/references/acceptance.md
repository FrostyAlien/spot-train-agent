# Acceptance
Offline: parse template; assert exact image digest, dedicated paths and no public port; inspect pinned package CLI/schema; rehearse backup integrity and restore in isolated storage with cloud access blocked. Never report these as cloud recovery proof.

Optional for occasional single-node campaigns; recommended before relying on a long-lived shared control plane. Separately authorized live campaign: reboot control host; terminate container process; interrupt during STARTING, RUNNING, RECOVERING and CANCELLING; restore state on replacement host after fencing old control. Verify same job ID, cloud/region, completed-checkpoint identity, one writer, preserved retry/spend counters and successful cancellation. Use a small workload with explicit maximum cost, duration and cleanup scope. Stop on duplicate ownership, missing state, unknown billing exposure or identity mismatch. Restore must not start a second job merely because status is stale.

Measure restore RTO/RPO and verify checkpoint age independently from controller DB age. Keep last known-good backup until the new deployment and resource inventory are confirmed.

The qualification task covers worker loss; controller-host loss is untested unless a DR test is approved.

## Completion checklist
Verify healthy API, the run's SkyPilot version (chosen at the start), original run identities, persistent files, one active controller, resumed management and retained logs. A health endpoint alone does not prove recovery. Report which offline and live checks were actually performed. Preserve state until resource reconciliation finishes (controller deletion: SKILL.md gates).
