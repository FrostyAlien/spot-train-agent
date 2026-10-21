# Official SkyPilot foundation and experiment overlay

Use the official `skypilot` skill for operation-specific CLI, YAML, SDK and diagnostic knowledge. Discover it by frontmatter name; installed directories may be renamed. Reviewed upstream: [skypilot-org/skypilot](https://github.com/skypilot-org/skypilot/tree/a72a88173f8ac438e9ef21d88549f6fdc43ca41e/agent/skills/skypilot), commit `a72a88173f8ac438e9ef21d88549f6fdc43ca41e`, checked 2026-10-01. Its skill revision is not the installed SkyPilot client/server/adapter version. Match all runtime flags and provider behavior to the actual version lock.

## Loading the official skill
For SkyPilot-specific CLI/YAML/SDK operations, discover the installed official `skypilot` skill by its frontmatter name and read only the relevant reference. Read this file before adapting its examples. Official operational guidance complements the experiment contract; generic examples cannot relax fixed-provider Spot-only recovery, checkpoint integrity, budget, secret handling or permissions. The official skill is a required companion of this suite and is installed with it (the repository's INSTALL.md). If it is absent, tell the user to install it; until then use the pinned official docs, and never bootstrap it implicitly.

## Read the narrow reference needed
- CLI/YAML/SDK operations: official `references/cli-reference.md`, `yaml-spec.md`, or `python-sdk.md` instead of copying their large command catalogs here.
- Pending/failing job: official `references/job-investigation.md`; use structured queue details and bounded recent task/controller logs supported by the installed version. Do not confuse live observation with the campaign guard.
- Managed Jobs/recovery/storage: official `references/advanced-patterns.md`, alongside this suite's launch and recovery contracts.
- Provider setup/debugging: official troubleshooting, subject to actual setup authorization; skill installation alone does not authorize CLI installation, credentials or paid compute.

## Decisions preserved by this suite
1. Official defaults favor optimizer-selected clouds/regions and broad resource alternatives. This user's run selects and locks one capable provider before creation, fixes region/GPU/purchase mode, and allows same-provider Spot-only recovery (a pre-approved on-demand fallback is a separate task only the campaign guard launches). Inspect nested candidates and provider billing semantics; never let a generic fallback example broaden the lock.
2. Official guidance correctly separates Managed Jobs infrastructure recovery from application checkpoint restoration. Its illustrative newest-directory checkpoint shell snippet is not this suite's implementation: use COMPLETE commits, full state, hashes/identities and classified errors. Never select by mtime or interpret failed listing as permission to train fresh.
3. Official examples with special retry exit codes can bypass application-error counters. Keep total attempts, capacity wait, elapsed budget and costs in the campaign guard. Do not copy unlimited retry or on-demand-serving fallback examples into this training run.
4. Inline secret examples describe syntax, not a safe storage policy. Keep metrics-tool/provider/registry credentials out of source, YAML, CLI arguments and images; use the approved runtime secret channel. W&B is telemetry, not checkpoint truth.
5. Broad workdir wipes, stop/down commands or a resource name alone do not authorize deletion. Use run-owned exact IDs, preserve durable artifacts and require applicable confirmation. Prefer immutable images/versioned inputs over wiping unknown state to solve stale-sync issues.
6. Setup/pip-install snippets illustrate workflows; build and qualify this reusable image before renting. Normal retries run fast readiness plus real restore checks, without rebuilding or requiring a resident AI.
7. A skill's generic Docker/provider support description is not proof of the exact adapter/GPU/region combination. Preserve the existing capability gate, including the provider's current spot pricing fields in the adapter and actual instance-status verification before work.

No wholesale copy of official reference catalogs is needed in these nine skills. Keep this suite's project lifecycle, cost comparison, durable publication/restore, metrics replay/privacy and closure specifics. Official skill content remains upstream-authored and is installed separately. Recheck examples when upgrading either the official skill or runtime.

## Installation and portability
[Official installation documentation](https://docs.skypilot.ai/en/latest/getting-started/skill.html) provides the Claude Code plugin route and a generic installer. Use the host's supported skill installation mechanism; do not overwrite an existing customized skill or execute cloud bootstrap merely to load instructions. The reviewed generic INSTALL has `.agents/.skills` versus `.agents/skills` path inconsistency, so verify the actual host-discoverable directory rather than applying its symlink recipe blindly. Without it, the suite falls back to pinned official documentation until the user installs it.
