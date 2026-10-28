---
name: gpu-rental-select
description: Compares fresh GPU rental offers by the calibrated total cost of the whole experiment and recommends one provider, region and purchase model for a single-node training run with up to eight GPUs. Use when choosing a cloud GPU, weighing spot against on-demand, or budgeting a run before launch.
---

# Select a GPU rental

Recommends the cheapest suitable completed experiment for one single-node run with 1–8 GPUs, not the lowest advertised GPU-hour; it recommends and never purchases. Bundled `scripts/` and `references/` paths are relative to this skill's directory (`<skill-dir>`), not the training repository; call helpers and pass project paths by absolute path.

## Inputs
- `experiment.json` (`schema_version: 1`); preserve all its existing fields.
- Total budget and any material data/residency restriction.
- Optional: the project's pinned environment; matched historical measurements.

## Steps
1. **Pin the experiment.** Record code/config, Docker digest, framework/CUDA, dataset/tokenizer revision, precision, optimizer, batch, sequence length, target useful tokens/steps and the scientific quality criterion. Ask for a missing budget or data/residency restriction, and continue read-only discovery meanwhile.
2. **Stay inside the pinned environment.** When the project pins one, recommend only rentable GPUs that its pinned wheels and extensions support (compiled architectures, CUDA version, platform), per current vendor documentation, not a hand-written table (image `references/contract.md#environment-decision`).
3. **Check platform coverage.** For a candidate GPU or host CPU platform (e.g. Grace-based arm64) outside the qualified image, apply the image skill's GPU-change and legacy-upgrade rules (image `references/contract.md#gpu-change`) before ranking it: reuse, rebuild or upgrade, each with the user's approval.
4. **Refresh provider facts.** Research official policy, region/platform availability, pricing and launcher/adapter version at decision time. Read [provider evidence](references/provider-evidence.md) first: it holds the questions and the pricing semantics.
5. **Get current offers** from SkyPilot's optimizer with `sky launch --dryrun probe.yaml`, which provisions nothing, rather than by hand-picking from `sky gpus list`. Use provider CLIs only for what the optimizer cannot show. Read [offer discovery](references/provider-cli.md#offers-from-skypilots-optimizer) before the first query: it holds the probe task and the quote fields to keep.
6. **Exclude hard-constraint failures** ([hard filters](references/provider-evidence.md#hard-filter-and-fit-checklist)) before any arithmetic. An offer without durable [checkpoint storage](references/provider-evidence.md#checkpoint-storage-a-hard-filter) at the same provider is unsuitable.
7. **Compute costs** for the surviving quotes under named base and stress scenarios: `python3 <skill-dir>/scripts/compare_costs.py quotes.json` prints a JSON ranking by total cost, or exits 2 with `Invalid input: ...`. Add the control plane's cost to the [`controller` category](references/calculator.md#cost-arrays). Read the [calculator](references/calculator.md) and its [normalizing rules](references/calculator.md#normalizing-quotes-before-calculation) before writing `quotes.json`.
8. **Calibrate throughput** from matched measurements, or from the qualification task (or a separately budgeted calibration run) of the exact image/config/data ([calibration accounting](references/calculator.md#calibration-accounting)).
9. **Rank** by cost per committed useful nonpadding token or optimizer step and by time-to-quality, not by utilization alone.
10. **Pass the exact recovery capability gate** for the actual client/server/adapter and provider/region/GPU/purchase-mode tuple: lifecycle `references/gates.md#exact-recovery-capability-gate`.
11. **Lock the choice.** Set `purchase_model` to the selected offer's `spot` or `on-demand`, and lock the chosen cloud and region as `provider`/`region`. Pinning `infra:` is SkyPilot's documented choice for a fixed cloud; here it keeps data and checkpoints with the compute. The choice stays fixed once the run starts (lifecycle provider lock): for a running experiment, report the authorized recovery options instead of reselecting.
12. **Report,** leaving deployment approval pending; refresh quotes immediately before any authorized launch, because prices and capacity change between selection and launch.

## Outputs
Selected offer or "no suitable offer"; all-in estimate/range and unknowns; quote freshness; hard-filter evidence; ranked alternatives with rejection reasons; calibration confidence; spot risk/bid cap; deadline; retained-resource costs; explicit `purchase_model`.

## Gates
- **Read `gpu-training-lifecycle` → `references/working-rules.md` before asking the user anything or deciding for them:** it sets the interaction levels.
- **Recommend, never purchase:** a recommendation, manifest, prior experiment or price ceiling grants no purchasing authority. Query without purchasing, saving credentials or accepting terms; run no paid calibration without authority.
- **Set `purchase_model` explicitly before launch, never silently,** because the lifecycle's spot-only default allows no automatic spot-to-on-demand fallback. An explicit initial on-demand run stays on-demand; ask about a pre-approved fallback during planning.
- **Treat historical spot support as unverified,** because provider products and adapters change: compare on-demand and interruptible offers only where current product and adapter support are verified.

## References
- [provider-evidence.md](references/provider-evidence.md): read before researching providers and excluding offers; it holds the capability questions, pricing semantics and hard filters.
- [provider-cli.md](references/provider-cli.md): read before the first offer query or provider call; it holds the optimizer probe and the read-only checks to look up (no per-provider commands, no authority for cloud mutations).
- [calculator.md](references/calculator.md): read when writing `quotes.json` and calibrating; it holds the input format, normalizing rules and calibration accounting.
- `gpu-training-lifecycle` → `references/experiment-contract.md` and `assets/experiment.example.json`: read when creating or changing the record.
