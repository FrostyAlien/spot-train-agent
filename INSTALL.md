# Install and first use (for agents)

You are a coding agent and the user asked you to install Spot Train Agent (https://github.com/FrostyAlien/spot-train-agent). Follow these steps in order. Installing only copies skill files (these nine and SkyPilot's official `skypilot` skill): do not install the SkyPilot client, log in to a cloud, create credentials or rent anything now. Those happen later, inside the skills, with the user's approval.

## 1. Ask the user two things

- **Which agent host:** Claude Code, Codex, or both.
- **Where:** this project only (recommended), or every project on this machine (Claude Code plugin, user scope).

If they have no preference, install for the host you are running in, in the current project.

## 2a. Claude Code: install the plugins

Spot Train Agent needs SkyPilot's official `skypilot` skill, and its plugin does not load without it. From the user's project directory (Claude Code 2.1.275 or later), add SkyPilot's marketplace first, then install; the install pulls in the `skypilot` plugin as a dependency:

```sh
claude plugin marketplace add https://github.com/skypilot-org/skypilot.git --sparse .claude-plugin agent --scope project
claude plugin install spot-train-agent --marketplace https://github.com/FrostyAlien/spot-train-agent.git --scope project
```

Use `--scope user` on both for every project. `--sparse` skips checking out the whole SkyPilot repository. The HTTPS URLs work on machines without GitHub SSH keys.

Check: `claude plugin list` shows `spot-train-agent@spot-train-agent` and `skypilot@skypilot` enabled; `claude plugin details spot-train-agent` lists the nine skills plus `start`, and `claude plugin details skypilot` one skill. They load in the next session (or after `/reload-plugins`). If `spot-train-agent` shows `failed to load` with a missing `skypilot@skypilot`, the first command was skipped: run it, then `/reload-plugins`.

Update later with `claude plugin update spot-train-agent@spot-train-agent`; remove with `claude plugin uninstall spot-train-agent@spot-train-agent` (then `claude plugin prune` removes the `skypilot` dependency if nothing else uses it).

## 2b. Codex, or Claude Code without plugins: copy the skill folders

Install ten skill folders: the nine under `skills/` of https://github.com/FrostyAlien/spot-train-agent, and SkyPilot's `skypilot` skill at `agent/skills/skypilot` of https://github.com/skypilot-org/skypilot. If your host has its own skill installer (for example one that installs skills from a GitHub repository), use it on those folders. Otherwise copy them yourself:

1. Fetch both repositories into a temporary directory, for example `git clone --depth 1 https://github.com/FrostyAlien/spot-train-agent.git "$TMPDIR/spot-train-agent"` and, for SkyPilot, a sparse clone of `agent/skills/skypilot` (`git clone --depth 1 --filter=blob:none --sparse https://github.com/skypilot-org/skypilot.git "$TMPDIR/skypilot"`, then `git -C "$TMPDIR/skypilot" sparse-checkout set agent/skills/skypilot`).
2. Pick the target folder: `.agents/skills/` for Codex, `.claude/skills/` for Claude Code, under the project root (or the host's personal skills folder if the user wants every project).
3. Show the user the ten folders and the target, and wait for their OK.
4. Copy each skill folder whole, keeping its `references/`, `scripts/`, `assets/` and `agents/` beside its `SKILL.md`. Copy SkyPilot's `LICENSE` (Apache-2.0) into the `skypilot` folder. If a folder of the same name already exists with different contents, stop and ask the user; never overwrite it.
5. Delete the temporary clones.

Check: the nine `gpu-*` folders and `skypilot` exist in the target, each with a `SKILL.md`. The host discovers them in the next session. To update later, compare each installed folder with the new version, ask before replacing a folder the user changed, then copy again.

Use the plugin or the copied skills in Claude Code, not both.

## 3. Tell the user how to start

Report what you installed and where, then tell them how to start. The bundled helper scripts run with Python 3.10+ (standard library only) when a stage needs them, not at install time.

Starting needs no special syntax: the user asks in plain words, for example "Plan a spot training run for this repository", and the `gpu-training-lifecycle` skill picks it up. With the Claude Code plugin, `/spot-train-agent:start` does the same and continues a run already in progress.

What happens next, in one paragraph for the user: the lifecycle skill reads their repository, asks its intake questions once (budget, GPUs, provider, data, how much the agent may do without asking), then walks through provider selection, a small control host, the image and data, one short GPU qualification run, the launch, monitoring and the finish. The agent asks before anything that costs money or deletes something. While training runs, a small guard on the control host enforces the budget and sends notifications, so no chat session has to stay open; the agent comes back to read progress, judge the training at planned evaluations and handle incidents.

## Which skill does what

Start with `gpu-training-lifecycle`; it calls the others. Each one can also be used directly:

| Skill | Use it to |
|---|---|
| `gpu-training-lifecycle` | Plan an experiment, or continue one, across all stages |
| `gpu-rental-select` | Compare current GPU offers and total cost, spot or on-demand |
| `gpu-training-controller` | Set up or check the control host (SkyPilot API server, guard) |
| `gpu-training-image` | Build and check the training image |
| `gpu-training-data` | Put the dataset in the provider's storage and read it from training |
| `gpu-training-launch` | Run the qualification task and launch the job with its guard |
| `gpu-training-observe` | Check how a run is going: progress, spend, preemptions, stalls |
| `gpu-training-recover` | Wire checkpoint resume into training code, or diagnose a failed resume |
| `gpu-training-finish` | Verify outputs, reconcile cost and clean up exactly what the run created |
