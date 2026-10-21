# Version and evidence discipline

Research baseline: 2026-10-01. Recheck volatile prices, SKUs, quotas, stock, APIs and integration behavior at execution time. Preserve URL, checked-at timestamp, pinned software version and exact claim. `latest` documentation is not a release guarantee.

## Known boundaries to recheck
- Providers change spot pricing models with dated cutoffs (for example from flat preemptible rates to price-following spot). SkyPilot's `latest` docs can describe price-taking behaviour with no maximum bid while a stable release's adapter still sends the old request; `max_hourly_cost` is not an ongoing bill cap. Do not certify a package version solely from docs.
- SkyPilot consolidated remote API server and traditional standalone managed-job controller have different lifecycle/idle-stop behavior. Do not pay for an accidental duplicate controller or stop the active server.
- Historical spot material of a provider does not establish current availability of a spot API; verify live or mark unknown.
- Marketplace interruptible same-host resumption differs from managed cross-host replacement; local volumes do not follow replacement machines.
- HF streaming state restore is not automatically bitwise/data-order identical, especially with shuffle buffers and framework-dependent dataloader checkpoint support.

## Sources
- SkyPilot managed jobs: https://docs.skypilot.ai/en/stable/examples/managed-jobs.html
- SkyPilot YAML: https://docs.skypilot.ai/en/latest/reference/yaml-spec.html
- SkyPilot cloud setup (per cloud): https://docs.skypilot.ai/en/latest/getting-started/installation.html
- HF Trainer: https://huggingface.co/docs/transformers/en/main_classes/trainer
- HF streaming: https://huggingface.co/docs/datasets/stream

## Validation ladder
1. File/frontmatter/link/schema lint.
2. Deterministic local unit tests and mocked failure artifacts.
3. Independent scenario exercises, with no cloud calls.
4. The authorized GPU qualification task on the selected version/backend.
5. Optional: a preemption/recovery drill (skill development only; bundle repository, not installed) and a control-plane restore drill.
6. Longer run with budget and data-integrity observations.

Passing levels 1–3 does not establish levels 4–6. These skills must refuse to claim real cloud recovery tested without actual job IDs, logs and checkpoint continuity evidence.

The skills' own test history is in VALIDATION.md of the skills repository (https://github.com/FrostyAlien/spot-train-agent); recheck version-specific behaviour for the SkyPilot version chosen at the start.
