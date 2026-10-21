# Offline calculator input

Run `python3 <skill-dir>/scripts/compare_costs.py quotes.json`. JSON only, max 2 MiB. No credentials, network, subprocesses or cloud mutations. All charges must already be normalized into the declared comparison currency; the script performs no FX conversion. Reject unknown costs instead of supplying zero. Output is an arithmetic ranking, not a suitability or purchase decision.

Top-level: schema_version=1, currency (three uppercase letters), offers (nonempty list). All offers must target the same useful quantity and unit. Every offer needs unique id, currency, gpu_count (integer 1–8), useful_units and useful_unit (`tokens` or `steps`), compute_segments, and five cost arrays: storage, ingress, egress, controller, other. Use an empty array only when verified none/free and record why in quote evidence. Include taxes in other if applicable; unknown taxes make input incomplete.

Each compute segment: label, rate (decimal string), rate_unit (`node_hour` or `gpu_hour`), hours (decimal string), billing_quantum_seconds, minimum_seconds. Segments are nonoverlapping, already classified billable durations: setup, useful training, checkpoint/eval overhead, replay/recovery, export. Include boot/image pull/compile/staging and shutdown tail where billed. For dynamic prices split into effective rate intervals; do not apply per-instance minimum twice. Rate unit already bundles CPU/RAM when provider does; do not add them again.

Each cost-array item: label, quantity (decimal string), unit, rate (decimal string), rate_unit, currency. Accepted pairs: GB/GB, GiB/GiB, GB_hour/GB_hour, GiB_hour/GiB_hour, node_hour/node_hour, request/request, item/item. Monthly storage quotes must be normalized to explicit hourly billing using the provider's actual convention first; preserve original amount/unit and conversion provenance outside this calculator. Transfer ingress and egress are separate arrays, including the dataset-origin provider. Controller includes idle allocation; qualification or calibration charges may be in other or separate compute segments without double counting.

Use money/quantities as bounded decimal strings (<=18 significant digits, 12 decimal places); only finite nonnegative values. Reject mismatched currencies even if an offer claims normalization; replace all normalized currency fields and retain the timestamped FX source elsewhere before passing input. `useful_units` excludes replayed work; zero returns null unit cost.

Example:
```json
{"schema_version":1,"currency":"USD","offers":[{"id":"synthetic-only","currency":"USD","gpu_count":8,"useful_units":"1000000","useful_unit":"tokens","compute_segments":[{"label":"training plus setup","rate":"2","rate_unit":"gpu_hour","hours":"2","billing_quantum_seconds":"1","minimum_seconds":"0"}],"storage":[],"ingress":[],"egress":[],"controller":[],"other":[]}]}
```

Result: USD 32 compute. This synthetic example is not a current quote or complete real experiment estimate. Call multiple inputs with named zero-interruption/base/stress assumptions; unknown interruption probabilities cannot yield a calibrated confidence interval. The script does not enforce budget, quote freshness, topology, capacity or billing truth.

## Normalizing quotes before calculation
Distinguish GPU-hour from whole-node rates, source egress from destination ingress, running from stopped storage, current spot price from max bid, and quote expiry from rental duration. Price both transfer endpoints even if one advertises free networking. Model setup, useful training, replay/recovery, storage, both transfer endpoints, controller, other charges and taxes. Show base/stress scenarios rather than inventing interruption probabilities. Quote tax and FX uncertainty explicitly; make no unconditional all-in claim with unknown charges. The script only computes costs; it cannot validate capacity, budget authority, topology or provider policies.

## Calibration accounting
Use matched historical measurements, or the qualification task (or a separately budgeted calibration run) of the exact image/config/data (paid only with authority). Separate boot, pull, staging and compile from steady-state throughput; measure checkpoint upload/restore, CPU/dataloader stalls, memory and collective performance. Count discarded runs in experiment cost. Reuse qualification and throughput evidence for the unchanged image/GPU/topology/data/store tuple; budget a new run only for material changes or stale/insufficient evidence, not every deployment. Compare cost per committed useful nonpadding token/optimizer step and time-to-quality, not just utilization; replay stays only in the cost numerator.
