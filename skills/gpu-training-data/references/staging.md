# Staging large data and choosing storage

## Get data into durable storage without a big disk
A machine that moves a dataset needs disk for the work in progress, not for the dataset. Present the
options that fit, with a recommendation; **the user decides, and without an answer copy into the
provider's bucket** (A or B). Run the copy on a CPU machine near the bucket (the control host, or without one a short-lived CPU VM launched
through the user's SkyPilot in the bucket's region, as the image runbook's build VM, deleted afterwards), never on a
rented GPU.

| Option | How | Local disk | Use when |
|---|---|---|---|
| A. Stream copy | `rclone copyurl URL remote:bucket/key` per file, or `rclone copy src: dst:` between object stores: data flows through memory | close to none | source files are already large shards (Parquet, tar, WebDataset) and need no change |
| B. Shard pipeline | each worker loops: download one shard -> convert or pack -> upload -> verify -> delete; a done marker per shard makes it resumable; the manifest is written last | parallel shards x shard size x ~3, plus margin | data needs converting or packing (many small files into ~0.5-2 GB shards) |
| C. No copy | read the source at a pinned revision during training: SkyPilot mounts `hf://datasets/<org>/<name>@<commit>` (read-only, `MOUNT` or `COPY`), or the loader streams it | the read cache only | the user accepts that training and every recovery depend on the source being up and not throttling |
| D. Provider transfer service | the provider copies server-side from the source | none | the provider offers one (research it when choosing the provider) |

Before approval, copy one shard and measure: estimated time = dataset size / measured rate; requests =
object count (millions of small files cost listing time and per-request fees: pack them, option B). Record
what was copied (files, sizes, the source revision) as the manifest. Reading the copy back to compare
checksums is optional: do it when the source publishes per-file hashes and the copy is small enough to
re-read cheaply; otherwise the copy tool's own checks are enough. For subsets of large ZIP archives on
HTTP hosts that support range requests (for example Hugging Face), members can be extracted remotely without
downloading the whole archive; record the archive revision and member hashes as usual.
Scale out with several small CPU VMs on disjoint shard ranges; bandwidth, not disk, is the limit
(1 PB at 10 Gbit/s is about 9 days). Very large data with one node of at most eight GPUs: check that
training will actually read it all (epochs x size / read rate) before copying everything.

Upload rules:
- Confirm the exact files, destination (provider, account, bucket, version prefix), access/visibility,
  retention and transfer/storage budget first; an existing login does not grant permission to transmit a new
  dataset.
- Reuse an already published verified version rather than uploading on every experiment or recovery.
- A copy command is neither no-clobber nor an atomic multi-object publication: prevent concurrent writers and
  overwrites of published version keys (storage policy or a commit protocol), and keep incomplete prefixes
  unpublished (no cleanup without deletion scope).
- Verify remote byte lengths and hashes (whole-object checksums or read-back; multipart ETags are not SHA-256)
  and record object version IDs where supported.
- Never upload broad cache or home directories, authentication files, private code or unreviewed data; no
  public ACLs.

With option C: pin the full commit (`@<40-hex>`), not a branch. A source outage or rate limit during a
recovery blocks or retries, never starts fresh. A private or gated dataset needs an HF token
(`gpu-training-lifecycle` → `references/credentials.md`, Hugging Face row). The qualification task measures
the read rate (below).

## Which storage for what
| Storage | Role | Notes |
|---|---|---|
| Object storage (buckets) in the GPU provider and region | durable truth: dataset shards, manifest, checkpoints, final artifacts | survives every worker; read through SkyPilot mounts; large immutable objects; same-region traffic is usually free |
| Boot disk / local NVMe of the worker | cache only: mount caches, a local checkpoint staging copy, dataloader scratch | may survive a stop and restart, but SkyPilot deletes it when recovery falls back to a new VM: a cache keyed by the verified dataset version; size it ([disk budget](#disk-budget)) |
| Block volumes | fast POSIX scratch or a cache that survives a stop | zone-bound, attach rules differ by provider; never the only copy of checkpoints unless replacement attachment is verified |
| Shared filesystems (NFS, Lustre-like) | many small random reads | extra service and cost; rarely needed on one node |

Mount modes for the dataset (`dataset.mount_mode`): `COPY` (fully staged: download and verify before
training; the dataset must fit the disk budget; fastest reads), `MOUNT_CACHED` (hybrid: reads cached on local
disk, or a custom loader cache with prefetch; good for multi-epoch data larger than disk, a sensible TB-scale
default to benchmark), `MOUNT` (streamed with bounded read-ahead and retry; for data read once or far larger
than disk; measured throughput must stay comfortably above what training consumes). State the choice
at intake with the startup it adds to every new VM, recoveries included: `COPY` downloads everything
first; a loader that reads or converts the whole dataset at start pays about the same in any mode, and
only a streaming loader avoids it.

## Disk budget
The worker's boot disk (`disk.size_gb` in the contract, SkyPilot's default otherwise) must hold: the
image (about twice its compressed size while pulling), the dataset when `COPY`, the read cache for
`MOUNT_CACHED` data, checkpoints not yet uploaded (at least 1.25 x checkpoint + 10 GB: `gpu-training-recover`
→ `references/contract.md#checkpoint-size-path-and-save-interval`), and a margin. The job-start
preflight checks free space (`gpu_preflight.py probe --min-free-gb`) and fails fast (exit 4) instead of
crashing hours later.

## Qualification subset
Stage a few hundred samples beside the dataset, under `<dataset uri>/qualify/`, with their own manifest,
prepared exactly like the full data: the same file format and layout, metadata files included. A dataset card
or index that declares split sizes must match the samples it lists, in the subset and in the full data. The
qualification never reads the full data; the launch runbook checks it on CPU first
(`gpu-training-launch` → `references/runbook.md#1-gpu-qualification-once-per-new-stack-inside-the-approved-qualification-cap`). Choose worst cases: the largest samples by the project's cost driver
(tokens, pixels after resize, boxes or masks per image), samples with no labels and samples with the most.
Give the eval split `N·b + 1` samples (N ranks of the planned run, b its per-device eval batch), so every
way of sharding it ends in an uneven batch. Record the prefix as `dataset.qualify_uri`; the qualification
task mounts the subset instead of the full dataset, in the same mode and with the same disk.
A subset is needed for `COPY` (SkyPilot copies the whole source before the task starts, so an argv limit
saves nothing) and for loaders that read everything at start (annotation files, a non-streaming
`load_dataset`). For lazily read `MOUNT` data an argv sample limit is enough; check the logged sample count,
since some scripts accept a limit and never apply it. An `hf://` dataset with `COPY` downloads in full. The
entrypoint fails on a missing manifest, so a wrong prefix (which mounts or copies nothing) stops at once.

## Measure I/O in the qualification task
The qualification task journals, on rank 0: `save_seconds` (how long each save blocks training;
`journal-check` reports the largest after the first save, the steady state), `checkpoint_gb` (the
checkpoint size) and `loader_wait_fraction` (the share of step time spent waiting for data; for `MOUNT`
data also the read rate `read_mb_s`, compared with what the GPUs consume per second). A large loader wait
means more loader workers, `MOUNT_CACHED` or `COPY`, or larger shards. The full data
can read slower than the subset; the first planned evaluation checks the rate (`gpu-training-observe` →
`references/runbook.md#wait-for-a-milestone-blocking-no-polling`).

The save interval these imply, and a different checkpoint path when uploads are too slow:
`gpu-training-recover` → `references/contract.md#checkpoint-size-path-and-save-interval`.
