# Mapping native framework checkpoints to the verifier manifest

The verifier checks a small, framework-neutral manifest. The adapter's job is to list **every file the
framework wrote** in that manifest, assign roles, and declare anything the framework rebuilds rather than
saves. Never rename or re-pack native files in a way the framework cannot load, and never write placeholder
files to satisfy a role. File names and internals change with framework versions, so the skills name none:
read the actual directory the project's pinned version writes for one step (and its source, when a file's
contents are unclear), then map it.

## General rules
- Save, or copy the finished native checkpoint, into `$CHECKPOINT_DIR/<step>-<attempt>/payload/` on the
  checkpoint mount unchanged (no renames on the mount); list every file, including helper files the
  framework writes (role `metadata`).
- Roles: `model`, `optimizer`, `scheduler`, `scaler`, `training_state` (counters, epoch, step), `rng`,
  `data` (loader/sampler position), `metadata`. Use `roles: [...]` when one file holds several states.
- Per-rank files get `rank`; `rng` and `data` must cover every rank unless declared in `derived_roles`.
- `derived_roles` is only for state the framework genuinely rebuilds from saved counters or seeds
  (scheduler position from the step, sampler order from epoch and seed, a reseeded RNG). Describe the real
  semantics (`{"from": [...], "semantics": "..."}`); the restore test must show the continuation is acceptable.
- Record `image_digest`, `code_sha256` and `config_sha256` in every manifest.
- Outputs the user needs at finish but that resume does not (a "best" model, exported metrics, evaluation
  files) are not resume state: copy each onto the mount when the framework updates it (for example
  `final/best.<ext>`) and list it as a `finalize --artifact`. A best model kept only on the worker's disk is
  lost when a replacement takes over, and the replacement may never rewrite it (its restored best metric can
  already be higher).

## Common shapes (what to look for, not file names)
| Shape | Mapping |
|---|---|
| One file holds model, optimizer, scaler and counters | one entry, `roles: [model, optimizer, scaler, training_state]`; scheduler, RNG and data order usually rebuilt from the epoch/step and seed: `derived_roles` |
| A directory per step with separate model, optimizer, scheduler files and one RNG file per process | one entry per file; RNG files with `rank` |
| Per-rank optimizer shards (sharded optimizers) | each shard `optimizer` with `rank`; the model file may also hold engine counters (`training_state`) and the scheduler |
| A framework "latest" pointer or conversion script beside the step directory | `metadata` (the suite's `COMMITTED.json` decides completeness, not the pointer) |
| A framework's own finalization marker | useful evidence, `metadata`; `COMMITTED.json` remains the completion marker unless the adapter proves them equivalent |
| Data position inside a trainer state file (replay-and-skip by step) | `data` derived from the step and seed with a deterministic sampler; streaming or shuffled iterable data needs the loader state saved explicitly (data skill) |

## Adapter procedure
```text
files = list every path under payload/ after one real save (all ranks done, all files closed)
for each file: roles = what the file actually contains (inspect keys once, record them); rank if per-rank
derived_roles = {role: {from: [...], semantics: "..."}} for each state the framework rebuilds
commit(step_dir, fields(identity, step, world_size, derived_roles, ...), files)
test once per stack: interrupted-versus-uninterrupted continuation (contract.md#8-offline-test-plan-and-acceptance-criteria)
```

Hooks under multi-process launchers that rebuild the training object in each child:
`gpu-training-lifecycle` → `references/entrypoint.md#pseudo-code`.
