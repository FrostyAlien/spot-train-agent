#!/usr/bin/env bash
# Provider hooks for <project> (see the lifecycle skill's references/provider-hooks.md).
# Written from <project>/provider-notes.md on <date>; provider CLI <version>. Replace every TODO with the
# chosen provider's documented command; delete functions this project does not use.

hook_registry_username() {
  echo "TODO: registry user name"            # e.g. a fixed user the registry documents for token logins
}

hook_registry_password() {
  echo "TODO: print a registry token" >&2    # e.g. the provider CLI's access-token command
  return 1
}

hook_instance_ids() {                        # $1 = SkyPilot cluster name
  echo "TODO: list VM IDs whose name starts with $1" >&2
  return 1
}

hook_instance_stop() {                       # $1 = VM ID
  echo "TODO: stop VM $1 through the provider API" >&2
  return 1
}

hook_inventory() {
  echo "TODO: list VMs/disks/IPs and leftover security groups in the run's project/region" >&2
  return 1
}

hook_storage_delete() {                      # $1 = object-store URI prefix ending in /
  echo "TODO: delete every object under $1 with the provider's storage CLI" >&2
  return 1
}

hook_price_cap() {                           # $1 = the provider's price-cap object ID (optional; runs inside the worker)
  echo "TODO: print capped if this VM runs under cap $1; else put it under the cap, request a stop, print switched" >&2
  return 1
}
