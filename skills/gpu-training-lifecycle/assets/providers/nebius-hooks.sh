#!/usr/bin/env bash
# Provider hooks for Nebius (lifecycle references/provider-hooks.md; profile references/providers/nebius.md).
# Dated starting point: Nebius CLI 0.12.283, checked 2026-10-21; the first five ran live in October 2026,
# hook_price_cap has not (offline only). Copy to <project>/provider-hooks.sh and re-test each function.
# hook_price_cap runs inside the worker: the image then needs this file (with NEBIUS_PROJECT_ID set in it, not a
# secret), the nebius CLI and jq, and a CLI profile that uses the service-account credentials SkyPilot copies to
# its VMs (verify in the live test).
# NEBIUS_PROFILE: the CLI profile of the service account (default "sa"); NEBIUS_PROJECT_ID: the GPU region's project.
PROJECT=${NEBIUS_PROJECT_ID:?set NEBIUS_PROJECT_ID}
NEB=${NEBIUS_CLI:-$HOME/.nebius/bin/nebius}
NP=(--profile "${NEBIUS_PROFILE:-sa}")

hook_registry_username() { echo iam; }

hook_registry_password() {            # an IAM access token, valid about 12 h: a static key outlives longer runs
  "$NEB" "${NP[@]}" iam get-access-token
}

# A failed CLI call (an expired token, say) must fail the hook, never read as "no VMs": each listing is captured
# first and parsed only when the call succeeded.
hook_instance_ids() {                 # $1 = SkyPilot cluster name; Nebius VM names start with it
  local out
  out=$("$NEB" "${NP[@]}" compute instance list --parent-id "$PROJECT" --format json) || return 1
  jq -r --arg p "$1" '.items[]? | select(.metadata.name | startswith($p)) | .metadata.id' <<<"$out"
}

hook_instance_stop() {                # $1 = VM ID: graceful provider stop (SIGTERM, then STOP)
  "$NEB" "${NP[@]}" compute instance stop --id "$1" --async >/dev/null
}

hook_inventory() {                    # what still bills in the GPU region's project
  local vms disks
  vms=$("$NEB" "${NP[@]}" compute instance list --parent-id "$PROJECT" --format json) || return 1
  disks=$("$NEB" "${NP[@]}" compute disk list --parent-id "$PROJECT" --format json) || return 1
  jq -r '.items[]? | "instance \(.metadata.id) \(.metadata.name) \(.status.state)"' <<<"$vms"
  jq -r '.items[]? | "disk \(.metadata.id) \(.metadata.name) \(.spec.size_bytes // "?") bytes"' <<<"$disks"
}

hook_price_cap() {                    # $1 = pricing policy ID; runs inside the worker (offline only)
  # This VM, found by its private IP. TODO(live test): confirm the JSON path and that the container sees the VM's IP.
  local ip id spec out
  ip=$(hostname -I | awk '{print $1}')
  out=$("$NEB" "${NP[@]}" compute instance list --parent-id "$PROJECT" --format json) || return 1
  id=$(jq -r --arg ip "$ip" '.items[]? | select([.status.network_interfaces[]?.ip_address.address // empty | split("/")[0]] | index($ip)) | .metadata.id' <<<"$out")
  [ -n "$id" ] || { echo "no VM with IP $ip in $PROJECT" >&2; return 1; }
  spec=$("$NEB" "${NP[@]}" compute instance get --id "$id" --format json) || return 1
  # Not a spot VM (an on-demand fallback): nothing to cap. Already under this cap: done.
  if [ "$(jq -r '.spec.preemptible // empty' <<<"$spec")" = "" ] ||
     [ "$(jq -r '.spec.spot_pricing_policy.id // empty' <<<"$spec")" = "$1" ]; then echo capped; return 0; fi
  "$NEB" "${NP[@]}" compute instance update --id "$id" --patch --spot-pricing-policy-id "$1" --stopped >/dev/null &&
    echo switched
}
