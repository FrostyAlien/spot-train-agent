# Local checkpoint verifier

The Python standard-library helper checks a deliberately small portable manifest format. It performs no cloud calls, uploads, deserialization or training. Use it on an immutable/quiescent downloaded directory with no concurrent writers. It rejects a symlinked checkpoint root and any symlink inside it, path traversal, missing commit markers, duplicate JSON keys, unexpected files and SHA-256 mismatches. Ancestor directories are canonicalized once (platform symlinks such as macOS `/tmp` → `/private/tmp` or Linux mount aliases are normal). It is not a security boundary against a malicious process concurrently swapping ancestor directories. Protect directory ownership and isolate concurrent writers.

Directory contents must be exactly manifest.json, COMMITTED.json, and the payload files named in the manifest (empty directories are harmless). Metadata is capped at 16 MiB. Payload hashing is streamed. No pickle or framework code is executed.

manifest.json fields:
- schema_version: 1
- run_id, data_version, model_version: expected immutable identities
- world_size: exact integer 1–8 (booleans/floats rejected)
- image_digest, code_sha256, config_sha256: the image, code and training-config identities that produced the checkpoint. Optional in the schema for older checkpoints, but a restore adapter must pass all three expected values (`--image-digest --code-sha256 --config-sha256`); a missing or different value is an identity mismatch, which blocks restore rather than falling back to an older checkpoint. Add `--require-identity` (`require_identity=True`) so a restore that forgets one of them fails closed instead of skipping that check.
- global_step: nonnegative integer
- optimizer_boundary: true
- scaler_used: boolean
- files: nonempty list of {path: "payload/relative-file", bytes: integer, sha256: lowercase hex SHA-256, exactly one of role: string or roles: [string, ...] (one file holding several states, e.g. a single-file trainer checkpoint), rank: integer when rank-specific}
- derived_roles (optional): {role: {from: [saved counters/fields], semantics: string}} for `scheduler`, `rng` or `data` only, when the framework rebuilds that state from saved counters/seeds instead of storing it (e.g. epoch-boundary resume that sets the scheduler from the epoch and reseeds the sampler). A role may not be both stored and derived; derived rng/data are exempt from per-rank coverage. The report lists `derived_roles`; treat any derivation as a weaker claim than stored state and test it with the adapter's interrupted-versus-uninterrupted continuation check.

Required roles: model, optimizer, scheduler, training_state, rng, data; scaler if scaler_used. RNG and data entries must cover every rank. Other backend-specific shards/metadata use additional roles. A generic manifest cannot prove a claimed role actually contains its state, so only the adapter-specific deserialization and continuation test establishes complete recovery. If an algorithm genuinely has no optimizer, use a different explicitly tested adapter/schema rather than fabricate files to pass this verifier. Never write placeholder payloads for scheduler/RNG/data; declare them in `derived_roles` with their real semantics instead.

COMMITTED.json fields: schema_version=1, run_id, global_step (exact nonnegative integer), manifest="manifest.json", manifest_sha256 computed over the exact manifest bytes. The publisher must create this last. Hashes establish integrity relative to the manifest, not authenticity: use trusted storage/transport and validate expected identities from an independent run record.

Example:

    python3 scripts/verify_checkpoint.py /path/to/checkpoint --run-id run-a --data-version sha256:DATA --model-version sha256:MODEL --world-size 8 \
      --image-digest sha256:IMAGE --code-sha256 CODE --config-sha256 CONFIG --require-identity

Exit 0 means local declared inventory/integrity verified. Exit 1 returns JSON error. Invalid CLI use exits 2. Keep stdout as the report artifact. The helper does not guarantee atomic multiobject publication, actual remote durability, correct optimizer tensors, scientific correctness, compatible software or bitwise continuation.

`verify(..., pending_commit=record)` checks a directory before its marker exists, as if `record` were its COMMITTED.json; `checkpoint_marker.py commit` uses it so a marker is only ever written for a checkpoint that already passes. `checkpoint_marker.py select` runs the verifier on mounted candidates newest first.

Adapter responsibilities and dated primary references: [contract](contract.md#2-training-checkpoint-content-and-boundaries). This file specifies an original local format, not a format any framework emits natively; an integration must inventory the framework's complete native checkpoint into it without discarding required files.

## Optional coordinator acceptance record (multi-writer setups only)
Single-node managed jobs have one writer and do not need this record. Use `--accepted-record /absolute/path/to/controller-record.json` when independently obtained from the protected controller journal. The record must be outside the downloaded checkpoint directory and contain schema_version=1, run_id, exact integer global_step, attempt_id, exact nonnegative integer fencing_generation, manifest_sha256 and commit_sha256 (SHA-256 of exact COMMITTED.json bytes). Both manifest and COMMITTED metadata must carry matching attempt_id and fencing_generation. Historical accepted generations may be valid; pass the record for this exact candidate, not merely the current attempt’s generation.

The helper matches this record but cannot establish its authenticity or whether a controller legitimately accepted it. Never fabricate it from the candidate itself. A valid high-step orphan with no accepted controller lineage must not become eligible. The report marks controller_acceptance as `record_matched` or `not_checked`; `restore_ready` remains false because candidate discovery, remote durability, runtime compatibility and actual framework restore are outside this helper. `ok=true` by itself means integrity only and must not bypass those gates. Legacy inventory-only calls still work.
