# Validation record

What has been tested, and how. Runbook steps marked *(offline only)* have not run in a live test. After a live run, remove the marker from the steps that ran.

## Offline tests (v0.9.4, macOS / Python 3.13)

359 tests pass:

| Suite | Tests |
|---|---:|
| Root release checks (skill layout, links and anchors, runbook commands) | 10 |
| `tests/controller/` network check, detached stages | 4 |
| `tests/image/` GPU preflight | 19 |
| `tests/launch/` task renderer | 60 |
| `tests/lifecycle/` experiment-contract validator | 60 |
| `tests/observe/` campaign guard, stall watchdog, PyTorch rank mode | 116 |
| `tests/recover/` checkpoint marker and verifier | 78 |
| `tests/select/` cost calculator | 12 |

- `test_runbook_commands.py` with `SKY_BIN` checks every `sky` command and flag in the skills against `--help` of an installed SkyPilot; it passes on SkyPilot 0.13.0 and `skypilot-nightly 1.0.0.dev20261001`.
- `tests/linux/run.sh` runs the stall watchdog's Linux scenarios (t1-t8: ticks, quiet limits, saves on a throttled disk, `torch_rank.py` under `torchrun`, Lightning) in a CPU container.

Reproduce:

```sh
python3 -m unittest discover -s tests -p 'test_*.py'
for d in tests/*/; do [ "$d" = tests/linux/ ] || python3 -m unittest discover -s "$d" -p 'test_*.py'; done
SKY_BIN=<path to a sky CLI> python3 -m unittest discover -s tests -p 'test_runbook_commands.py'
tests/linux/run.sh          # optional, Docker, about 15 minutes
```

## Live tests

All on one provider (Nebius) with L40S GPUs and spot capacity. Each found gaps that the next release fixed.

| Date | Version | Setup | What ran |
|---|---|---|---|
| 2026-10-02 | 0.5.0 | 2 GPUs, SkyPilot 1.0.0.dev20261001 | Checkpoint commit and select on a `MOUNT_CACHED` bucket; a provider stop right after a commit, replacement on a new spot VM and resume from that commit; W&B resume; guard observation and notifications |
| 2026-10-03 | 0.6.1 | 2 GPUs, a new project | The runbooks end to end: CPU rehearsal, GPU qualification (fresh start, then resume), guard notifications across two recoveries including a real spot preemption, finish verification |
| 2026-10-03 | 0.7.0 | 1-2 GPUs, a new project, automatic level | A user-style run from intake to finish |
| 2026-10-04 | 0.8.0, 0.8.1 | 1-2 GPUs, SkyPilot 0.13.0 | GPU probe, qualification and fix loop, subset copy, submit, guards under systemd, evented waits, the stall watchdog on an injected one-rank stall and on a real 2-GPU Hugging Face evaluation hang, exit-code paths through the wrapper, finish verification, inventory and pruning |
| 2026-10-05 | 0.9.0 | CPU VM and 2 GPUs, SkyPilot 0.13.0 | The silence-based stall watchdog: an 8 GB save through the bucket mount ran to the end, a frozen save was stopped (exit 93), rank mode under `accelerate launch`, an injected hang stopped with every rank's stack; finish verification through `stage.sh` |

## Not yet tested live

See the roadmap in the README.


- Guard cancel, blind and on-demand fallback paths; the worker deadline stop
- 8 GPUs on one node; multiple nodes (the goal, not supported yet); any provider other than Nebius; the Minimal tier
- Control-plane state backup and restore, duplicate-controller fencing
- Provider billing collection; the guard's spend figures are upper bounds, not a hard cap
