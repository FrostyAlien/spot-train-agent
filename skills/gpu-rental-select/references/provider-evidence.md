# Provider evidence: what to establish before comparing offers

No provider observations are kept here: they change faster than skills. Research the chosen and compared providers at decision time (`gpu-training-lifecycle` → `references/provider-research.md`), record dated findings in the project, and refresh official policy/pricing, the actual region/platform and the pinned launcher-adapter support before each experiment.

## Capability questions per provider
| Question | Why it matters |
|---|---|
| Is an interruptible (spot/preemptible/bid) mode offered for this exact GPU, preset and region, and how is it priced (flat, price-following, bid)? | purchase mode, `budget.ceiling_hourly_usd` |
| Notice period and signal, stop vs delete on interruption, what survives (boot disk, volumes, IP, local disk), whether restart must be orchestrated | recovery design, guard |
| Can the mode be changed in place, and does a stop release the GPU slot (restart may then find no capacity)? | recovery, finish |
| Marketplace specifics: per-host CPU/RAM/disk/bandwidth, offer vs instance IDs, contract expiry, bid floor (not a reservation) | fit, cleanup |
| Storage billing while running, stopped and as separate volumes; tiers and month accounting | cost model, retention |

**Interpretation:** provider interruption support is not orchestration-adapter support. A provider may support spot while the selected SkyPilot version's adapter does not, or sends different pricing fields. Require the versioned adapter-capability check before emitting a deployable plan. On-demand is not immunity from host, network or maintenance failure; a single-node job still needs checkpoints.

## Pricing semantics (no numbers: always fetch current quotes)
- Price pages can show old and new rates side by side around an effective date; record which one applies to the run's dates.
- An advertised spot "from" price is a floor, not a quote; price-following spot can change every few minutes and may reach the on-demand rate, so budget at the on-demand ceiling.
- Taxes/VAT are usually excluded; state the tax treatment in the comparison.
- Storage is often billed differently when running, stopped and as network volumes, sometimes with tiers; resolve the tier and month accounting before arithmetic.
- Marketplace providers price per host; compute, storage, ingress and egress are separate, and stopped instances still bill storage.
- Prepaid credit or top-ups are cash movements that need the user's approval, not budget headroom.

## Version and uncertainty gate
Preserve official URL, retrieval/effective times, raw snapshot hash and API/CLI/adapter version with each quote. Runtime capability and orchestration adapter support are different. Where current documentation omits or contradicts a capability, mark it unverified rather than asserting support or removal. Do not reuse historical prices as live offers. No source proves current quota, taxes, capacity or billing truth without the account's own read-only checks.

## Hard-filter and fit checklist
Require one physical node and 1–8 appropriate full GPUs or explicitly approved slices. Verify per-GPU VRAM, allocated CPU/RAM, disk capacity/bandwidth, network, container/driver compatibility and data access. Include tokenized expansion, caches and checkpoints in TB-scale disk sizing. Do not infer intra-node NVLink/NVSwitch from inter-node InfiniBand advertising; require measured topology/P2P/NCCL evidence before trusting multi-GPU throughput. Check offer expiry against staging, training, export and recovery margin. An unknown field is not zero or supported. Exclude hard constraint failures before arithmetic.

## Checkpoint storage: a hard filter
Require a selected-provider checkpoint service whose data survives worker release; prefer the same region and verify volume delete-on-termination/reattach restrictions or object-store semantics. If that provider offers no suitable durable service, mark the offer unsuitable under this requirement rather than silently choosing another provider’s store. Price checkpoint retention, requests and transfer. Checkpoint storage at a different provider (for example a GPU vendor without object storage plus S3 or R2) is not supported in this version: it needs its own egress, latency and credential design and is future work.

## Exact recovery capability gate
Before creating a task, follow the authoritative capability gate in `gpu-training-lifecycle` → `references/gates.md#exact-recovery-capability-gate`. Pin the actual client/server/adapter and chosen provider/region/GPU/purchase-mode tuple. For a spot run, verify Spot provisioning/reacquisition and actual billing mode; generic provider support is insufficient. An explicitly selected initial on-demand run stays on-demand and does not authorize mode switching.
