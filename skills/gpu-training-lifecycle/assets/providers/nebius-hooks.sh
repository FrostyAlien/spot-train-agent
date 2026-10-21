#!/usr/bin/env bash
# Provider hooks for Nebius (lifecycle references/provider-hooks.md; profile references/providers/nebius.md).
# Dated starting point: Nebius CLI 0.12.283, checked 2026-10-21; the first five ran live in October 2026,
# hook_price_cap has not (offline only). Copy to <project>/provider-hooks.sh and re-test each function.
# NEBIUS_PROFILE: the CLI profile of the service account (default "sa"); NEBIUS_PROJECT_ID: the GPU region's project.
PROJECT=${NEBIUS_PROJECT_ID:?set NEBIUS_PROJECT_ID}
NEB=${NEBIUS_CLI:-$HOME/.nebius/bin/nebius}
NP=(--profile "${NEBIUS_PROFILE:-sa}")

hook_registry_username() { echo iam; }

hook_registry_password() {            # an IAM access token, valid about 12 h: a static key outlives longer runs
  "$NEB" "${NP[@]}" iam get-access-token
}

hook_instance_ids() {                 # $1 = SkyPilot cluster name; Nebius VM names start with it
  "$NEB" "${NP[@]}" compute instance list --parent-id "$PROJECT" --format json |
    jq -r --arg p "$1" '.items[]? | select(.metadata.name | startswith($p)) | .metadata.id'
}

hook_instance_stop() {                # $1 = VM ID: graceful provider stop (SIGTERM, then STOP)
  "$NEB" "${NP[@]}" compute instance stop --id "$1" --async >/dev/null
}

hook_inventory() {                    # what still bills in the GPU region's project
  "$NEB" "${NP[@]}" compute instance list --parent-id "$PROJECT" --format json |
    jq -r '.items[]? | "instance \(.metadata.id) \(.metadata.name) \(.status.state)"'
  "$NEB" "${NP[@]}" compute disk list --parent-id "$PROJECT" --format json |
    jq -r '.items[]? | "disk \(.metadata.id) \(.metadata.name) \(.spec.size_bytes // "?") bytes"'
}

hook_price_cap() {                    # $1 = pricing policy ID; runs inside the worker (offline only)
  # This VM, found by its private IP. TODO(live test): confirm the JSON path and that the container sees the VM's IP.
  local ip id policy
  ip=$(hostname -I | awk '{print $1}')
  id=$("$NEB" "${NP[@]}" compute instance list --parent-id "$PROJECT" --format json |
    jq -r --arg ip "$ip" '.items[]? | select([.status.network_interfaces[]?.ip_address.address // empty | split("/")[0]] | index($ip)) | .metadata.id')
  [ -n "$id" ] || { echo "no VM with IP $ip in $PROJECT" >&2; return 1; }
  policy=$("$NEB" "${NP[@]}" compute instance get --id "$id" --format json | jq -r '.spec.spot_pricing_policy.id // empty')
  if [ "$policy" = "$1" ]; then echo capped; return 0; fi
  "$NEB" "${NP[@]}" compute instance update --id "$id" --patch --spot-pricing-policy-id "$1" --stopped >/dev/null &&
    echo switched
}
