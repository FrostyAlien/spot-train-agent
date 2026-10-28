---
name: gpu-training-data
description: Prepares versioned terabyte-scale training data for single-node GPU jobs, with immutable manifests, bounded worker caches and exactly resumable loaders. Use when a dataset must reach the provider's bucket or be read from a pinned source, when choosing the mount mode or the worker disk, or when a loader must resume exactly.
---

# Prepare durable training data

Turns the project's dataset into an immutable, verified version that any worker can read and resume. The user's model and training script stay authoritative: add a data adapter, never silently replace their dataset semantics.

## Inputs
experiment.json, dataset sources and access limits, immutable source/tokenizer revisions, preprocessing/packing settings, scratch limits, rank/worker counts.

## Steps
1. **Pin the inputs.** Resolve immutable input bytes and revisions (full commits, not branch names). Reuse a verified dataset version whose source and preprocessing contracts match ([durable truth](references/contract.md#durable-truth), [reuse](references/contract.md#prepare-before-gpu-allocation)).
2. **Choose the staging path.** Read [staging](references/staging.md#get-data-into-durable-storage-without-a-big-disk) before choosing: it holds the four options (stream copy, shard pipeline, no copy from a pinned source such as `hf://datasets/<org>/<name>@<commit>`, the provider's transfer service), their disk needs and the measurement to make first. The user decides; without an answer, copy into the provider's bucket, so training and every recovery stop depending on the source being up.
3. **Convert on CPU.** Tokenize, pack, validate and shard (the shard pipeline) on CPU-capable infrastructure before requesting GPU time ([prepare before GPU allocation](references/contract.md#prepare-before-gpu-allocation)): a GPU that finds a multi-hour preprocessing job left bills for every hour of it. Upload only the approved inventory, to existing authorized storage; read [tools](references/tools.md) before running the commands.
4. **Manifest.** Publish it last, after every shard verifies, so no reader sees a half-written version ([manifest](references/contract.md#durable-truth)). The manifest is immutable and is the dataset identity; a cache fingerprint alone is not, because it does not prove the bytes and preprocessing. Stage the [qualification subset](references/staging.md#qualification-subset) beside it and record its prefix as `dataset.qualify_uri`.
5. **Mount mode.** Choose `dataset.mount_mode` (`COPY`, `MOUNT` or `MOUNT_CACHED`) from capacity and measured throughput, and size the worker disk. Read [mount modes](references/staging.md#which-storage-for-what) and [disk budget](references/staging.md#disk-budget) before choosing: they hold the startup each mode adds to every new VM and what the disk must hold.
6. **Data-position owner.** Choose one owner (replay-and-skip or stateful cursor) and record it as `data_resume_mode` in data-readiness.json. Never combine a restored cursor with framework batch skipping (e.g. Trainer), which advances the data twice; a framework flag alone is not a stateful integration. Claim no exact continuity from a stream whose shuffle buffer is not saved (e.g. HF streaming `.shuffle()`): a resume loses the buffered examples. Read [the owner rules](references/contract.md#3-exactly-one-owner-of-data-advancement) before choosing.
7. **Loader qualification.** Check once per stack that, after a restart, the next sample/token sequence matches across shard boundaries and workers. The GPU qualification task then measures the loader wait and read rate ([measure I/O](references/staging.md#measure-io-in-the-qualification-task)). Later attempts run only the entrypoint's and adapter's quick access, integrity and cache checks ([per-attempt checks](references/contract.md#prepare-before-gpu-allocation), [cache rules](references/contract.md#cache-rules)).

## Outputs
dataset-manifest.json, preprocessing-lock.json, data-readiness.json and a tested data-adapter contract. In experiment.json, dataset.uri is the manifest location and dataset.version its immutable digest/version; preserve other fields. data-readiness.json records `data_resume_mode` and its capability (`stateful_recovery` versus `exact_data_continuity`); the recover skill reads them there.

## Gates
- **Read `gpu-training-lifecycle` → `references/working-rules.md` before asking the user anything or deciding for them:** it sets the interaction levels.
- **Upload, create credentials or spend only after the destination and cost are approved:** experiment.json grants none of these.
- **Prepare only this stage's access** (`gpu-training-lifecycle` → `references/credentials.md`) and never request secret values in chat. Keep private-data access limits and never widen sharing to simplify loading; no secrets in artifacts, which outlive the run and get shared.
- **Durable data lives in storage independent of workers,** so losing a worker loses no unique data.
- **Pin world size and worker count for automatic recovery:** the saved loader state is per rank and per loader worker, so another count cannot restore it.
- **GPU launch needs the [readiness evidence](references/contract.md#launch-readiness-evidence):** report missing access or untested integration as blockers before GPU time is spent.

## References
- [staging.md](references/staging.md): read before choosing the staging path, the mount mode or the worker disk, and before staging the qualification subset; it holds the staging options, upload rules, storage roles, disk budget and the I/O measures.
- [contract.md](references/contract.md): read before writing the manifest, the cache plan or the adapter, and before choosing the data-position owner; it holds the manifest fields, readiness evidence, cache rules, the adapter interface and the resume modes.
- [tools.md](references/tools.md): read before running a download or upload command; it holds `hf` and `aws s3` examples whose placeholders come from verified inputs. Examples grant no permission for paid or external actions.
