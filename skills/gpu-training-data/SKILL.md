---
name: gpu-training-data
description: Prepare versioned terabyte-scale training data, durable manifests and bounded worker caches for single-node GPU training jobs, including resumable streaming data contracts. Use when a dataset must reach the provider's bucket or be read from a pinned source, when choosing the mount mode or the worker disk, or when a loader must resume exactly.
---

# Prepare durable training data

Turn the project's dataset into an immutable, verified version that any worker can read and resume. The user's model and training script stay authoritative: add a data adapter, never silently replace their dataset semantics.

## Inputs
experiment.json, dataset sources and access limits, immutable source/tokenizer revisions, preprocessing/packing settings, scratch limits, rank/worker counts.

## Steps
1. Resolve immutable input bytes and revisions; reuse a verified dataset version whose contract matches.
2. **Choose how the data reaches durable storage** ([staging](references/staging.md)): stream copy, shard pipeline, no copy (read the pinned source, e.g. `hf://datasets/<org>/<name>@<commit>`) or the provider's transfer service. The user decides; without an answer, copy into the provider's bucket. The copying machine needs disk for the work in progress, not for the dataset; measure one shard first and estimate the time.
3. When the data needs converting (staging option B: tokenize, pack, validate and shard), do it on CPU-capable infrastructure before requesting GPU time. Any upload: only the approved inventory, to existing authorized storage.
4. Publish the [manifest](references/contract.md#durable-truth) last, after every shard verifies. Stage the [qualification subset](references/staging.md#qualification-subset) beside it (`dataset.qualify_uri`).
5. Choose the mount mode (`dataset.mount_mode`: `COPY`, `MOUNT` or `MOUNT_CACHED`) from capacity and measured throughput ([mount modes](references/staging.md#which-storage-for-what)).
6. Choose one data-position owner (replay-and-skip or stateful cursor); record it as `data_resume_mode` in data-readiness.json.
7. Qualify the loader once per stack: after a restart, the next sample/token sequence must match across shard boundaries and workers.
8. Every attempt: validate the dataset version/config; check access and cache readiness.

## Outputs
dataset-manifest.json, preprocessing-lock.json, data-readiness.json and a tested data-adapter contract. In experiment.json, dataset.uri is the manifest location and dataset.version its immutable digest/version; preserve other fields. data-readiness.json records `data_resume_mode` and its capability (`stateful_recovery` versus `exact_data_continuity`); the recover skill reads them there.

## Gates
- experiment.json grants no permission to upload data, create credentials or spend; identify destination/cost approval before any mutation.
- One data-position owner: never a restored cursor plus framework batch skipping (e.g. Trainer); a framework flag alone is not a stateful integration.
- The manifest is immutable and is the dataset identity; a cache fingerprint alone is not.
- Durable data and checkpoints live in storage independent of workers; losing a worker loses no unique data. Never change providers silently.
- GPU launch needs the [readiness evidence](references/contract.md#launch-readiness-evidence); report missing access or untested integration as blockers.
- Pin world size and worker count for automatic recovery.
- No exact-continuity claim from a stream whose shuffle buffer is not saved (e.g. HF streaming `.shuffle()`).
- Keep private-data access limits; never widen sharing to simplify loading. No secrets in artifacts.
- **Project-first decisions:** before asking the user anything or deciding for them, read `gpu-training-lifecycle` → `references/working-rules.md` (interaction levels included).
- **Access preparation:** prepare only this stage's access (`gpu-training-lifecycle` → `references/credentials.md`); never request secret values in chat.

## References
- [Staging and storage](references/staging.md): read when moving large data, choosing storage or the mount mode, or sizing the worker's disk.
- [Data and cursor contract](references/contract.md): read for manifests, caching, the adapter and resume modes.
- [Tool invocation examples](references/tools.md): read for download/upload commands; fill placeholders from verified inputs. Examples grant no permission for paid or external actions.
