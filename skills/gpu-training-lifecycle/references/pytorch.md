# PyTorch specifics: entrypoint, stall watchdog, checkpoints and restore

Read when the project trains with PyTorch or a framework built on it. The entrypoint contract
([entrypoint.md](entrypoint.md)) is framework-neutral. This file adds what only PyTorch has.

Contents: [collective timeout](#collective-timeout); [scripts without hooks: rank mode](#scripts-without-hooks-rank-mode-offline-only);
[ticks](#ticks); [diagnosis on a hang](#diagnosis-on-a-hang); [checkpoints and restore](#checkpoints-and-restore)
([loader state on resume](#loader-state-on-resume)).

## Collective timeout
Plain PyTorch sets it with `torch.distributed.init_process_group(timeout=...)`. A framework built on PyTorch has
its own setting for the same value; find it in its docs. Values: [entrypoint.md](entrypoint.md#inputs).

## Scripts without hooks: rank mode *(offline only)*
A PyTorch script with its own loop and no callbacks gets its `train` ticks (E1) from rank mode. The script is not
edited: the launcher starts the helper in each rank in place of the script.

```bash
torchrun --nproc_per_node=N /opt/gpu-skills/torch_rank.py --step-hook -- train.py <args>
accelerate launch --multi_gpu --num_processes N /opt/gpu-skills/torch_rank.py --step-hook -- train.py <args>
```
- Any launcher that runs a Python script or module per rank works (`-- -m package.module <args>` for a module).
  Arguments a launcher adds before `--` (`--local_rank=N`) reach the script.
- `--step-hook` uses PyTorch's global optimizer step hook (PyTorch >= 2.0, every `torch.optim.Optimizer`
  subclass): one `train` tick after each optimizer step. The helper also registers the stack dump on SIGUSR2.
- It sees training steps only. An evaluation inside the loop is one long train gap: pass `--eval-scale` to the
  qualification, and `journal-check` widens the train limit for it (its note says so). Hangs are then found later.
- Ranks that a script spawns itself (`torch.multiprocessing.spawn`) do not run the helper. Use the framework's
  callbacks there, or `watchdog.enabled: false`. Never combine rank mode with callbacks that tick.
- Vendor `torch_rank.py` (observe skill, `scripts/`) at `/opt/gpu-skills/` with the other helpers.

## Ticks
Pass a step counter the framework keeps as a Python int (a trainer's global step, a batch index), never a CUDA
tensor (rules: [entrypoint.md](entrypoint.md#inputs)).

## Diagnosis on a hang
- `PYTHONFAULTHANDLER=1` ([entrypoint.md](entrypoint.md#inputs)) prints the Python stack of a rank that an NCCL abort
  kills after a collective timeout.
- PyTorch's flight recorder (on by default) keeps a ring buffer of the last collectives per rank. It dumps on a
  collective timeout and when asked. The rendered task points its dumps at `$CHECKPOINT_DIR/debug/<start>/fr-<rank>`
  and its request pipes into the heartbeat directory (`TORCH_NCCL_DEBUG_INFO_PIPE_FILE`, `STALL_DUMP_PIPES`). On a
  stall the watcher writes to every rank's pipe before the stack dumps. The dump names the last collective each
  rank entered, so a rank that stopped issuing collectives stands out. Read it with PyTorch's flight-recorder tools.
- The stacks under a `STALL` line show where Python waits. That is often one step after the operation that hung
  (the next GPU copy or `.item()`), because CUDA runs asynchronously. Read them with the dump.
- NCCL on one node moves no bytes the stall watchdog counts; `gloo` collectives over sockets do.

## Checkpoints and restore
Checked 2026-10-01 against primary documentation; verify against the project's pinned versions. The generic
rules (complete state, commit, select, fail-closed restore) stay in `gpu-training-recover` →
`references/contract.md`; the data position in `gpu-training-data` →
`references/contract.md#3-exactly-one-owner-of-data-advancement`. Load strictly: a load that reports missing or
unexpected keys is a restore failure (`blocked:`, exit 3; entrypoint.md), never a warning to continue past.

- **HF Trainer:** resumable checkpoints need `save_only_model=False`; model-only exports
  (`save_model`/`save_pretrained`) omit optimizer, scheduler and RNG state. Resume from an explicit
  verified path (below). `ignore_data_skip=False` replays and skips seen
  data; setting it true changes ordinary resume. Stateful callbacks need their restoration option. Current
  docs also offer SIGTERM just-in-time checkpointing, which needs shutdown time for the iteration plus the save;
  do not assume the pinned version has it or that the provider gives notice. An `on_save` hook may notify
  publication only after the pinned Trainer/backend behaviour and rank participation are verified.
  [Trainer API](https://huggingface.co/docs/transformers/main_classes/trainer)
- **Accelerate:** `save_state`/`load_state` keep model, optimizer, RNG and scaler, and expect the same training
  script. Register custom stateful objects, including an otherwise unmanaged scheduler, with
  `register_for_checkpointing`; test prepared-scheduler handling on the pinned version, never double-register.
  [Checkpointing](https://huggingface.co/docs/accelerate/usage_guides/checkpoint)
- **FSDP and DCP:** use the native distributed checkpoint APIs, never an unwrapped model-only state dict. DCP
  writes several files and rank shards and can reshard on load; the model allocates destination storage first.
  Cross-version compatibility is not guaranteed. Await an async save before claiming durable completion.
  Resharding does not remap rank RNG or streaming cursors.
  [Distributed Checkpoint](https://docs.pytorch.org/docs/2.14/distributed.checkpoint.html)
- **DeepSpeed:** all ranks call save and load with the same tag. `load_checkpoint` returns a path that is `None`
  on failure: that blocks the attempt. ZeRO-3 loads into a freshly initialized engine, not a still-partitioned
  post-save model. Weight consolidation is an export, not optimizer/data-state restoration; universal
  checkpoints (topology conversion) need their own tested path.
  [Engine source](https://raw.githubusercontent.com/deepspeedai/DeepSpeed/master/deepspeed/runtime/engine.py),
  [checkpoint docs](https://deepspeed.readthedocs.io/en/latest/model-checkpointing.html)

Launchers: one per node, never nested, a fixed process count; backend, precision and topology stay in the
locked configuration. A launcher neither replaces a worker nor restores a checkpoint. Never install or upgrade
libraries during recovery.
```sh
python -c 'import importlib.metadata as m; print({p:m.version(p) for p in ("torch","transformers","accelerate")})'
torchrun --help; accelerate launch --help
torchrun --standalone --nnodes=1 --nproc-per-node="$N" "$USER_TRAIN_SCRIPT" <the user's arguments>
# or, for an existing validated Accelerate configuration:
accelerate launch --config_file "$LOCKED_ACCELERATE_CONFIG" --num_machines 1 --num_processes "$N" "$USER_TRAIN_SCRIPT" <the user's arguments>
```

Restore, after `checkpoint_marker.py select` verified the directory (pseudo-code to integrate in the user's
script; run only the branch of the saved backend):
```python
trainer.train(resume_from_checkpoint=str(verified_dir))       # HF Trainer, with the tested data-resume mode
accelerator.load_state(str(verified_dir))                     # OR Accelerate: same objects prepared, custom state registered first
load_path, client_state = engine.load_checkpoint(str(verified_dir), tag=verified_tag)  # OR DeepSpeed: a pristine engine
if load_path is None:
    raise RuntimeError("Required checkpoint restore failed")
```
Then check optimizer, scheduler, RNG, data and custom state with all-rank agreement before the first update.
Sources: [torchrun](https://docs.pytorch.org/docs/2.14/elastic/run.html),
[Accelerate CLI](https://huggingface.co/docs/accelerate/package_reference/cli).

### Loader state on resume
One data-position owner (`gpu-training-data` → `references/contract.md#3-exactly-one-owner-of-data-advancement`). Replay-and-skip: a Trainer's ordinary `ignore_data_skip=False`. Stateful cursor with a custom Trainer integration: `ignore_data_skip=True` may be needed to prevent double advancement, but the flag alone is not a correct stateful integration; custom Accelerate is often clearer when stateful streaming matters.
- **TorchData:** `StatefulDataLoader` exposes state save/restore and aggregates worker-level dataset state; iterable restoration requires the same worker count. It is a building block, not proof that arbitrary transforms, rank sharding, packing or HF integrations are correct. [TorchData worker-state requirements](https://meta-pytorch.org/data/beta/stateful_dataloader_tutorial.html)
- **Trainer:** [ignore_data_skip semantics](https://huggingface.co/docs/transformers/main_classes/trainer).
