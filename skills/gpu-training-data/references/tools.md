# Dataset tool quick reference (offline only)

Verified against primary documentation on 2026-10-01; the `hf` commands need `hf` >= 1.0 (the older `huggingface-cli download` has no `--dry-run`). Use the existing environment, check help for its pinned version, and fill uppercase variables from verified project inputs. Commands are examples, not authorization to download TBs, transmit data or create access.

```sh
hf version
hf --help
hf download --help
hf auth whoami
# Inspect one approved file at a full immutable commit before deciding transfer scope.
hf download "$REPO_ID" "$FILE" --repo-type dataset --revision "$FULL_COMMIT" --dry-run
# Choose ONE destination style after capacity/scope approval:
hf download "$REPO_ID" "$FILE" --repo-type dataset --revision "$FULL_COMMIT" --local-dir "$DEST"
# OR preserve Hub cache layout:
hf download "$REPO_ID" "$FILE" --repo-type dataset --revision "$FULL_COMMIT" --cache-dir "$HUB_CACHE"
```

`auth whoami` reports account/organization identity; do not print token files or dump environment variables. Authentication changes are separate from checking status. Omit `--token` arguments. `--local-dir` creates download metadata under its `.cache/huggingface` directory; it is a materialized checkout, not the same layout as `--cache-dir`. Neither replaces the immutable dataset manifest and hash validation. Use an explicitly selected file or reviewed list rather than accidentally downloading a whole repository. Model/tokenizer snapshots use the appropriate model repository type, not `dataset`.

## Optional existing S3-compatible storage

```sh
aws --version
aws s3 cp help
# Download ONE verified manifest using an existing authorized profile and approved endpoint.
aws --profile "$PROFILE" --endpoint-url "$VERIFIED_HTTPS_ENDPOINT" \
  s3 cp "$APPROVED_MANIFEST_S3_URI" "$LOCAL_MANIFEST" --only-show-errors
```

Verify profile identity, provider endpoint and source URI before requesting private data. For ordinary AWS S3, omit the endpoint override and use the approved region/profile. Never guess a compatible service endpoint or send credentials to an unverified host. Copying an object is not a durable multi-file commit; validate hashes after transfer.

## Upload a reviewed local dataset inventory

Upload rules (approval, no-clobber, hash checks): [staging](staging.md#get-data-into-durable-storage-without-a-big-disk).

```sh
# One reviewed local shard and a fresh, immutable destination key; no recursive sync.
aws --profile "$PROFILE" --region "$REGION" \
  s3 cp "$APPROVED_LOCAL_SHARD" "$APPROVED_SHARD_S3_URI" --dryrun
# Only after scoped upload approval and inspection of the dry run:
aws --profile "$PROFILE" --region "$REGION" \
  s3 cp "$APPROVED_LOCAL_SHARD" "$APPROVED_SHARD_S3_URI" --only-show-errors
# Repeat only for the explicit reviewed inventory, then verify every object.
# Publish the final manifest only after all listed shards are durable and verified:
aws --profile "$PROFILE" --region "$REGION" \
  s3 cp "$APPROVED_LOCAL_MANIFEST" "$APPROVED_MANIFEST_S3_URI" --only-show-errors
```

For an approved S3-compatible destination, add only its verified HTTPS endpoint and validate its checksum/version semantics.

Sources: [HF CLI](https://huggingface.co/docs/huggingface_hub/guides/cli), [AWS S3 cp](https://docs.aws.amazon.com/cli/latest/reference/s3/cp.html).
