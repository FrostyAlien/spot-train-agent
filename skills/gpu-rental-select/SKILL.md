---
name: gpu-rental-select
description: Compare fresh GPU rental offers and calibrated total experiment costs before selecting one provider for a single-node training run with up to eight GPUs. Use for cloud GPU selection, spot versus on-demand tradeoffs, and pre-run budgeting.
---

# Select a GPU rental

Choose the cheapest suitable completed experiment for one single-node run with 1–8 GPUs, not the lowest advertised GPU-hour; recommend, never purchase. Bundled `scripts/` and `references/` paths are relative to this skill's directory (`<skill-dir>`), not the training repository; call helpers and pass project paths by absolute path.

## Inputs
- `experiment.json` (`schema_version: 1`); preserve all its existing fields.
- Total budget and any material data/residency restriction.
- Optional: the project's pinned environment; matched historical measurements.

## Steps
1. **Pin the experiment:** code/config, Docker digest, framework/CUDA, dataset/tokenizer revision, precision, optimizer, batch, sequence length, target useful tokens/steps and scientific quality criterion. Ask for a missing budget or data/residency restriction; continue read-only discovery meanwhile.
2. **Stay inside the pinned environment:** if the project pins one, recommend rentable GPUs within the range its pinned wheels and extensions support (compiled architectures, CUDA version, platform), per current vendor documentation, not a hand-written table.
3. **Check platform coverage:** if a candidate GPU or host CPU platform (e.g. Grace-based arm64) is outside the qualified image, apply `gpu-training-image`'s GPU-change and legacy-upgrade rules before ranking it: reuse, rebuild or upgrade, each with the user's approval.
4. **Refresh provider facts:** official policy, region/platform availability, pricing and launcher/adapter version, starting from [provider evidence](references/provider-evidence.md).
5. **Get current offers** from SkyPilot's optimizer, not by hand-picking from `sky gpus list`: write a probe task with the GPU type/count, purchase mode and `resources.any_of` over the acceptable clouds (and regions, where data or checkpoints must stay), then run `sky launch --dryrun probe.yaml`; it prints the chosen and considered cloud/region/instance/price without provisioning. Use provider CLIs ([provider CLI](references/provider-cli.md)) only for what the optimizer cannot show (quotas, preset CPU/RAM, providers SkyPilot does not cover). Retain quote source, retrieval time, units and freshness limit.
6. **Exclude hard-constraint failures** before any arithmetic (provider evidence checklists).
7. **Normalize the surviving quotes** ([calculator](references/calculator.md)) and run each named base/stress scenario: `python3 <skill-dir>/scripts/compare_costs.py quotes.json` (one argument, no `--help`; the input format is in the calculator reference).
8. **Calibrate throughput** from those measurements, or from the qualification task (or a separately budgeted calibration run) of the exact image/config/data.
9. **Rank** by cost per committed useful nonpadding token/optimizer step and time-to-quality.
10. **Pass the exact recovery capability gate** (provider evidence) for the actual client/server/adapter and provider/region/GPU/purchase-mode tuple.
11. **Lock the choice:** set `purchase_model` to the selected offer's `spot` or `on-demand`, and lock the chosen cloud and region as `provider`/`region` (pinning `infra:` is SkyPilot's documented choice when the user asks for a fixed cloud; here it keeps data and checkpoints with the compute).
12. **Report,** leaving deployment approval pending; refresh quotes immediately before any authorized launch.

## Outputs
Selected offer or "no suitable offer"; all-in estimate/range and unknowns; quote freshness; hard-filter evidence; ranked alternatives with rejection reasons; calibration confidence; spot risk/bid cap; deadline; retained-resource costs; explicit `purchase_model`.

## Gates
- **Project-first decisions:** before asking the user anything or deciding for them, read `gpu-training-lifecycle` → `references/working-rules.md` (interaction levels included).
- Never infer purchasing authority from a recommendation, manifest, prior experiment or price ceiling. Query without purchasing, saving credentials or accepting terms. No paid calibration run without authority.
- **Purchase mode** (lifecycle spot-only default): set `purchase_model` before launch, never silently; no automatic spot-to-on-demand fallback (an explicit initial on-demand run stays on-demand; ask about a pre-approved fallback during planning).
- **Provider lock** (lifecycle gate): provider and region stay fixed once a run starts (`recovery.allow_provider_change` stays false). Never migrate or reselect a running experiment: report the authorized recovery options or ask for a new experiment decision; a provider move is a separately approved successor experiment.
- Keep the controller on separate inexpensive non-spot CPU infrastructure, or use an existing or provider-managed SkyPilot server. The `controller` cost category covers a control host, a server fee, the jobs-controller VM a local server creates, and short-lived build or data VMs.
- Historical spot support is no live capability guarantee; compare on-demand and interruptible offers only where current product and adapter support are verified.

## References
- [provider-evidence.md](references/provider-evidence.md): read before selecting a provider; includes the hard filters and recovery capability gate.
- [provider-cli.md](references/provider-cli.md): read before provider calls: which read-only checks to look up for the chosen provider (no per-provider commands are kept here); no authority for setup or cloud mutations.
- [calculator.md](references/calculator.md): read when writing `quotes.json`; the script only computes costs.
- `gpu-training-lifecycle` → `references/experiment-contract.md` and `assets/experiment.example.json`: read when creating or changing the record.
