# Shared working rules for all nine skills

## Project-first decisions
Read the project's training entrypoint, supplied configuration and experiment protocol before proposing changes. Preserve existing model and scientific choices. Ask only about missing consequential decisions; offer two or three options with a recommended default and cost/speed/complexity/reproducibility tradeoffs. If hyperparameters are absent, ask whether to use project defaults, accept a configuration file or co-design a configuration. Do not silently change learning rate, effective global batch, steps, splits, seeds or precision to improve utilization. Microbatch/gradient-accumulation changes need a reproducibility assessment, not an assumption of numerical equivalence. Resolve routine implementation details without repeated questions; budget, placement, purchase-mode and scope changes require approval.

## Questions to the user
Ask once, early, with this intake checklist rather than stage by stage. Ask every item, in as many rounds as the question tool needs; an item the user skips takes its recommendation, and the plan says so.
1. **Code:** where it lives (a local path, or a git URL and commit; private or not). Recommendation: the project's current commit.
2. **Starting point:** new, or continue from what the project already holds. Recommendation: continue when a contract, image or checkpoints exist.
3. **Interaction level** (below). Recommendation: gated.
4. **Tier:** Standard or Minimal. Recommendation: Standard; Minimal only for a short on-demand run the user watches (gated or steps); automatic means unattended, so Standard.
5. **GPUs:** type and count (the world size). Recommendation: what the project's recipe and per-device batch were tuned for; one GPU when the code has never run on several.
6. **Budget cap:** the total the run may spend. Recommendation: proposed after a price dry run (selection skill, seconds); without one, the figure is labelled an unpriced guess.
7. **Deadline:** only when the result is needed by a date. Recommendation: none; the budget is the hard stop.
8. **Provider, purchase mode** and whether an on-demand fallback is pre-approved. Recommendation: spot, no fallback.
9. **Image and build machine:** an existing working image to reuse (checked, not rebuilt), or where to build one (never a rented GPU). Recommend a builder as [the operating model](operating-model.md#keep-cpu-work-off-rented-gpus) says. Data preparation runs where the data skill's staging says.
10. **Owned GPU** for testing the image locally before renting and for the code dev loop. Recommendation: use it when the user has one and is willing; otherwise a failed qualification keeps its cluster for fixes.
11. **Data:** where it is, how it reaches storage (copy into the provider's bucket by default, or read the pinned source) and its mount mode with the startup it adds to every new VM (a loader that reads everything at start pays it in any mode). Recommendation: copy into the bucket; `COPY` when it fits the disk.
12. **Notifications** (Standard): the channel and its format (text, Slack, Discord or JSON); its URL goes into the secrets file as `NOTIFY_URL`, never into chat. Recommendation: a private push topic the user already reads. The worker sends the notices itself. Also ask about the optional campaign guard (`monitoring.guard: external`; what it adds: `gpu-training-observe` → `references/guard.md#campaign-guard`). Recommendation: no guard; add it when the provider bills while waiting without a VM.
13. **Metrics tool:** W&B (entity, history policy: `gpu-training-observe` → `references/wandb.md#history-policy-the-user-chooses`), another tool, or none. Recommendation: what the project already uses.
14. **Hyperparameters** the project does not fix: its defaults, a configuration file, or co-designed. Recommendation: the project's defaults.
15. **SkyPilot API server:** a control host the user runs, an existing server (a team's, shared with other people or not), a provider-managed one, or a local one on the operator's machine (`gpu-training-controller` → `references/runbook.md#0-which-server`). Recommendation: an existing or provider-managed server when the user has one (no control host needed), otherwise a private control host.
16. **Cleanup:** what to keep at the end (final model, checkpoints, dataset copy, images, control host). Recommendation: keep the final model and its checkpoint; delete the rest after verification.

User messages that arrive while a stage runs are read at each stage boundary, before the next stage starts.

## Interaction level (`approval.mode`)
Ask which level the user wants; recommend gated. Each level supports a new project and continuing an existing one.
- **automatic**: the whole plan is approved once at intake.
  - The plan: provider and purchase mode, control host, buckets and registry, the qualification cap, the main run's budget, deadline (none by default) and recovery limits, and what to keep or delete at the end.
  - Before any spend, show the plan as one block that restates every checklist answer (GPU type and count, data mount mode, cap, deadline or "none") and the decided limits (qualification cap, attempts, capacity wait, the collective timeout; the stall watchdog's quiet limits come from the qualification and are reported then); the user approves that block. Then every stage runs without asking.
  - Stop and ask only on a deviation: a failed check that needs a code or configuration change, spend beyond an approved cap, a change of scope, provider or purchase mode, or missing access. Notifications, and the guard when attached, still run.
- **gated** (default): approvals only where money is committed or something is deleted: the setup envelope at selection (control host, buckets and registry, qualification cap, data mount mode), the main run after qualification (with the measured numbers), and the finish deletions. Everything else runs; new spend, access or scope outside the envelope still asks.
- **steps**: approve each stage before it starts and see each result.

In every level, if an approved plan item cannot be done (for example a price cap the provider refuses), tell the user at once. Continue without asking only when the plan names a fallback; otherwise the automatic level treats it as a deviation and stops.
Record the level in `approval.mode` and the approved plan in `approval.scope` (both descriptive; the user's words are the authorization). In any level, never ask for secret values in chat.
