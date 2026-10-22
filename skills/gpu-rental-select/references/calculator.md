# Offline calculator input

Run `python3 <skill-dir>/scripts/compare_costs.py quotes.json` with exactly one argument (`--help` prints the usage). It prints a JSON ranking sorted by total cost, or exits 2 with `Invalid input: ...`.
- JSON only, max 2 MiB. No credentials, network, subprocesses or cloud mutations.
- All charges must already be normalized into the declared comparison currency; the script performs no FX conversion.
- Reject unknown costs instead of supplying zero.
- The output is an arithmetic ranking, not a suitability or purchase decision: the script does not check budget or its authority, quote freshness, topology, capacity, billing truth or provider policies.

## Top level
- `schema_version`=1, `currency` (three uppercase letters), `offers` (nonempty list). All offers must target the same useful quantity and unit.
- Every offer needs a unique `id`, `currency`, `gpu_count` (integer 1–8), `useful_units` and `useful_unit` (`tokens` or `steps`), `compute_segments`, and five cost arrays: `storage`, `ingress`, `egress`, `controller`, `other`.
- Use an empty array only when verified none/free, and record why in the quote evidence.

## Compute segments
- Fields: `label`, `rate` (decimal string), `rate_unit` (`node_hour` or `gpu_hour`), `hours` (decimal string), `billing_quantum_seconds`, `minimum_seconds`.
- Segments are nonoverlapping, already classified billable durations: setup, useful training, checkpoint/eval overhead, replay/recovery, export. Include boot/image pull/compile/staging and the shutdown tail where billed.
- For dynamic prices, split into effective rate intervals; do not apply the per-instance minimum twice.
- A rate unit already bundles CPU/RAM when the provider does; do not add them again.

## Cost arrays
- Each item: `label`, `quantity` (decimal string), `unit`, `rate` (decimal string), `rate_unit`, `currency`. Accepted unit pairs: GB/GB, GiB/GiB, GB_hour/GB_hour, GiB_hour/GiB_hour, node_hour/node_hour, request/request, item/item.
- Normalize monthly storage quotes to explicit hourly billing using the provider's actual convention first; keep the original amount/unit and the conversion provenance outside this calculator.
- Ingress and egress are separate arrays, including the dataset-origin provider.
- `controller` covers a control host, a server fee, the jobs-controller VM a local server creates, and short-lived build or data VMs, including idle allocation.
- Qualification or calibration charges may go in `other` or in separate compute segments, without double counting.
- Include taxes in `other` if applicable; unknown taxes make the input incomplete.

## Numbers and currency
- Write money and quantities as bounded decimal strings (<=18 significant digits, 12 decimal places); only finite nonnegative values.
- Mismatched currencies are rejected even if an offer claims normalization: replace all normalized currency fields, and keep the timestamped FX source elsewhere, before passing the input.
- `useful_units` excludes replayed work, so replay stays only in the cost numerator; zero returns a null unit cost.

## Example
```json
{"schema_version":1,"currency":"USD","offers":[{"id":"synthetic-only","currency":"USD","gpu_count":8,"useful_units":"1000000","useful_unit":"tokens","compute_segments":[{"label":"training plus setup","rate":"2","rate_unit":"gpu_hour","hours":"2","billing_quantum_seconds":"1","minimum_seconds":"0"}],"storage":[],"ingress":[],"egress":[],"controller":[],"other":[]}]}
```

Result: USD 32 compute. This synthetic example is not a current quote or a complete real experiment estimate.

## Normalizing quotes before calculation
- Distinguish GPU-hour from whole-node rates, source egress from destination ingress, running from stopped storage, current spot price from max bid, and quote expiry from rental duration.
- Price both transfer endpoints even if one advertises free networking.
- Model setup, useful training, replay/recovery, storage, both transfer endpoints, controller, other charges and taxes.
- Run one input per named scenario (zero-interruption, base, stress) rather than inventing interruption probabilities; unknown interruption probabilities cannot yield a calibrated confidence interval.
- Quote tax and FX uncertainty explicitly; make no unconditional all-in claim with unknown charges.

## Calibration accounting
- Use matched historical measurements, or the qualification task (or a separately budgeted calibration run) of the exact image/config/data, paid only with authority.
- Separate boot, pull, staging and compile from steady-state throughput; measure checkpoint upload/restore, CPU/dataloader stalls, memory and collective performance.
- Count discarded runs in the experiment cost.
- Reuse qualification and throughput evidence for an unchanged image/GPU/topology/data/store tuple; budget a new run only for material changes or stale/insufficient evidence, not every deployment.
