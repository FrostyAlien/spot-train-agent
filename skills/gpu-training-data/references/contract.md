Research checked: 2026-10-01 UTC. Design guidance; verify APIs against the project’s pinned versions.

Contents: [1. Dataset lifecycle and interface](#1-dataset-lifecycle-and-interface) ([durable truth](#durable-truth), [prepare before GPU allocation](#prepare-before-gpu-allocation), [launch readiness evidence](#launch-readiness-evidence), [cache rules](#cache-rules)); [3. Exactly one owner of data advancement](#3-exactly-one-owner-of-data-advancement) ([streamed data](#primary-reference-for-streamed-data))

## 1. Dataset lifecycle and interface

### Durable truth

Store immutable raw/processed shards, the dataset manifest, tokenizer/processor snapshot, preprocessing recipe, and dataset checks in durable storage independent of any GPU worker. Pin Hub inputs to full commit revisions, not a movable branch name; Hub download APIs accept revisions including commit IDs. [Hub download guide](https://huggingface.co/docs/huggingface_hub/guides/download)

Prefer the training provider's same-region durable object storage for processed data and checkpoint prefixes, with separate immutable namespaces and access rules. Record provider, region, retention and replacement-worker access. An exception needs explicit placement/data-transfer approval where applicable and a transfer/egress budget; do not silently copy to another provider. Worker NVMe and metrics-tool artifacts are not the authoritative checkpoint store. What the metrics tool may upload: `gpu-training-observe` → `references/wandb.md#privacy-outages-and-qualification`.

The manifest records split membership, row/token counts, the ordered shard inventory, byte lengths, SHA-256, schema and preprocessing hashes. Proposed dataset manifest (illustrative values and URIs, not resources to create):

```yaml
schema: training.dataset/v1
dataset_id: corpus-tokenized-v3
manifest_sha256: "<hash of canonical manifest excluding this field>"
source_revision: "<immutable commit/version>"
preprocessing:
  code_commit: "<git commit>"
  image_digest: "sha256:<digest>"
  tokenizer_revision: "<full commit>"
  tokenizer_sha256: "<snapshot hash>"
  config_sha256: "<packing/truncation/filter/split config hash>"
format: parquet
schema_sha256: "<schema hash>"
num_rows: 1000000
num_tokens: 1000000000
shards:
  - uri: "object://dataset/version/shard-00000.parquet"
    bytes: 123456789
    sha256: "<content hash>"
    rows: 10000
    tokens: 10000000
splits: {train: "<explicit shard list or deterministic split recipe>", eval: "<...>"}
```

Do not infer dataset identity from an HF cache fingerprint alone. The contract identifies actual bytes plus preprocessing semantics. Avoid a mutable directory wildcard whose shard membership can change between attempts.

### Prepare before GPU allocation

On CPU-capable preparation infrastructure or the user's existing workstation: resolve immutable sources, check licenses/access, tokenize and pack, validate schema/sample lengths, produce ordered shard inventory and checksums, measure compressed/uncompressed size, verify sufficient scratch/RAM, and publish the manifest only after outputs verify. GPU startup receives a completed manifest; it should not discover that a multi-hour preprocessing job or multi-terabyte download is required.

Reuse a completed immutable dataset version when source/preprocessing contracts match. Qualify loader resume behavior once per reusable stack; still validate each new manifest, schema and preprocessing configuration. Full sequence-continuity tests repeat after major loader/preprocessing/framework/topology changes, while each attempt performs only quick access, integrity and cache-readiness checks. Version changes never justify skipping dataset-specific validation.

Select one access plan (mount mode) before launch: [staging](staging.md#which-storage-for-what).

The whole dataset need not fit on each worker. Size shard boundaries to limit resume re-read, exploit parallel reads, and avoid tiny-object overhead. Benchmark rather than prescribe a universal shard size.

### Launch readiness evidence

Before GPU launch require a verified immutable manifest, completed preprocessing, readable representative shards, a bounded cache/scratch plan, a throughput measurement and a restart test with sample IDs. Report missing access or untested integration as blockers before GPU launch.

### Cache rules

Cache keys include dataset manifest digest and shard hash. Write downloads to temporary files, verify, then rename locally. Never treat an incomplete download as a hit. Cache eviction cannot remove a shard currently being read. Keep HF Hub and Datasets caches separate from checkpoint truth and authentication files. Worker-local cache is expendable; regional persistent volumes remain caches unless their lifecycle and replacement attachment guarantees are explicitly verified.

HF Hub downloads and Datasets Arrow/index caches are separate: configure `HF_HUB_CACHE` and `HF_DATASETS_CACHE` deliberately, or a common `HF_HOME`, in the task's envs or argv (never image ENV). [HF cache management](https://huggingface.co/docs/datasets/cache). Keep tokens outside a cache tree that will be archived/shared; `HF_HOME` can also house authentication state, and `HF_TOKEN_PATH` controls the token file. [HF environment variables](https://huggingface.co/docs/huggingface_hub/package_reference/environment_variables)

Proposed adapter:
```python
class DataAdapter:
    def prepare(self, manifest, cache_root): ...
    def validate(self, manifest) -> dict: ...
    def create_loader(self, rank, world_size, loader_config): ...
    def snapshot(self) -> dict: ...   # corresponding to next unconsumed batch
    def restore(self, state: dict): ...
    def position_fingerprint(self) -> str: ...
```
`prepare` must be idempotent. `snapshot` includes rank/worker sampler state, epoch, seeds/generators, pending packing leftovers, transform RNG, and relevant prefetch state, or uses a proven replay-based design that reconstructs them.

## 3. Exactly one owner of data advancement

Choose `data_resume_mode` (`replay_and_skip` or `stateful_cursor`) and record it, with its capability, in data-readiness.json (SKILL Outputs):

**Replay-and-skip:** rebuild the same deterministic dataset ordering, sampler and transformations, then have one training-loop owner skip to the committed position. Replaying TB-scale data may be expensive; initialization/prefetch must not perturb the restored training RNG sequence.

**Stateful cursor:** restore loader/dataset/worker state and continue directly. Disable any second batch-skipping path (a framework's own skip flag alone does not make a correct stateful integration). Verify epoch/set_epoch interactions and the exact loader lifecycle in the pinned version. Framework specifics: the framework's reference (PyTorch: `gpu-training-lifecycle` → `references/pytorch.md#loader-state-on-resume`).

Never restore a dataset cursor AND apply the framework's normal skip-by-step (e.g. Trainer, Accelerate) on the restored stream. Never save only the parent dataset object's state when multiprocessing workers own independent cursors. Save each rank's loader state under its own identity.

For exact continuity, prefer deterministic pre-shuffled/packed immutable shards or a custom adapter with serialized shuffle buffer and RNG. Standard HF streaming `.shuffle()` does not satisfy this stronger contract (below). If accepting non-exact data continuity, declare it explicitly and record the difference; do not describe it as full scientific replay.

### Primary reference for streamed data
Checked 2026-10-01. Exact behavior is version-dependent; validate the project's pinned implementation.
- **HF streaming:** iterable state tracks shard and position, avoiding replay of completed shards, but re-reads the current shard up to its cursor. `.shuffle()` buffers are not preserved: buffered examples are lost and replaced when resuming. A saved dataset cursor alone therefore cannot establish exact shuffled-stream continuity. [HF streaming cursor and shuffle limitation](https://huggingface.co/docs/datasets/stream#save-a-dataset-checkpoint-and-resume-iteration)
