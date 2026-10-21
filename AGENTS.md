# AGENTS.md

Guidance for coding agents and human contributors changing this repository. For what the project is and how to use it, read [README.md](README.md).

The project is early stage (pre-1.0). Today the skills cover one node with up to eight GPUs; multi-node training is the goal. Changes may reshape skills, but every change keeps the tests green and the release records honest.

## Layout

- `skills/<name>/` is the product: nine skills installed into users' projects. Each has `SKILL.md` (the router), `references/` (read on demand), optional `scripts/` (stdlib Python helpers), `assets/` (examples, icon) and `agents/openai.yaml` (Codex UI metadata only).
- `tests/` is never installed. `tests/<skill>/` holds unit tests for that skill's scripts; root `tests/test_*.py` are release checks (skill layout, links, runbook commands); `tests/linux/` runs the stall watchdog in Docker.
- `commands/start.md` is the plugin's `/spot-trainer:start` shortcut into the lifecycle skill (plugin only; copied skills start from plain words).
- `.claude-plugin/` makes the repository a Claude Code plugin and its own marketplace (`source: "./"`, skills from `skills/`). The plugin depends on SkyPilot's official `skypilot@skypilot` plugin (allowed through `allowCrossMarketplaceDependenciesOn`), which is required. Codex installs the same plugin from `.claude-plugin/marketplace.json` but skips the dependency, so INSTALL.md has Codex users install `skypilot@skypilot` first. `INSTALL.md` is the install guide written for agents: the host installs by prompt (plugin, its own skill installer, or copying `skills/<name>/` folders). There is no install script.
- `VALIDATION.md`, `SHA256SUMS` and the version line at the top of `README.md` are release records.

## Commands

```sh
# all offline checks; run before every commit
python3 -m unittest discover -s tests -p 'test_*.py'
for d in tests/*/; do [ "$d" = tests/linux/ ] || python3 -m unittest discover -s "$d" -p 'test_*.py'; done

# when a runbook's sky command or flag changes (needs a SkyPilot CLI)
SKY_BIN=<path to sky> python3 -m unittest discover -s tests -p 'test_runbook_commands.py'

# when progress_watch.py or torch_rank.py changes (Docker, CPU, about 15 minutes)
tests/linux/run.sh
```

No third-party packages are needed for the unit tests. Set `PYTHONDONTWRITEBYTECODE=1` or delete `__pycache__/` before packaging.

## Rules for the skills

These come from the maintainer's decisions during live tests; follow them unless an issue changes them.

- **Stay generic.** For each change ask: would a different project on a different provider need this? Fixes for one library's bug, one test project, one operator machine or the test harness stay out of the skills.
- **No vendor tables.** No GPU-architecture tables, prices or provider fact files in `skills/`, except dated provider profiles in `gpu-training-lifecycle/references/providers/` (Nebius today): each fact carries a check date and a source, and the agent re-checks every row it uses. Write procedures and research checklists; the agent researches the provider when the user picks it and records dated findings in the user's project. Provider-specific commands belong in the project's provider hooks file (template: `gpu-training-lifecycle/assets/provider-hooks.example.sh`).
- **Code only where it runs unattended or checks deterministically** (guard, watchdog, checkpoint marker/verifier, validator, renderer, cost calculator, preflight). Orchestration is a runbook of CLI commands with a check after each step. No template training code; examples in references are pseudo-code.
- **Never pin a SkyPilot version in the skills.** The agent picks it at the start and keeps it for the run. Defer `sky` CLI/YAML detail to SkyPilot's docs and the official `skypilot` skill.
- **Lean checks.** Compatibility checks run once at the start of a project and after an upgrade, not before every launch. Do not add per-launch gates or optional guard features without a concrete failure that needs them.
- **Follow SkyPilot's defaults** for credentials and security; offer hardening only as a pointer to SkyPilot's docs. Keep the basics: no secrets in chat, files or images; ask before spending; exact-ID cleanup only.
- **Helpers use only the Python standard library** (3.10+), take absolute paths and are covered by tests in `tests/<skill>/`.
- **Helpers copied into the training image** (`checkpoint_marker.py`, `verify_checkpoint.py`, `gpu_preflight.py`, `progress_watch.py`, `notify.py`, `torch_rank.py`; image skill step 6) run inside users' containers at `/opt/gpu-skills/`. Keep them self-contained single files.
- **Do not claim what did not run.** A runbook step that has not run live keeps its *(offline only)* marker; VALIDATION.md records only checks that were performed.

## Writing skill text

- `SKILL.md` frontmatter has `name` (the directory name) and `description` (what it does, then "Use when ..."). The description drives routing; keep it specific and avoid negations.
- `SKILL.md` stays short: Inputs, Steps, Outputs, Gates, References. Detail goes in `references/`.
- Every file in `references/` is listed in its skill's `SKILL.md` with a line that says when to read it (`tests/test_links.py` enforces this).
- Cross-skill pointers use `` `gpu-xxx` → `references/name.md#anchor` `` or the short form ``lifecycle `references/name.md` ``; links and anchors must resolve (also checked by `tests/test_links.py`). Pointers by step number ("§4") are not checked, so prefer anchors.
- One fact lives in one place; elsewhere, point to it. When moving text, check nothing was lost.
- Plain, short sentences. Say what to do, then the condition. Commands appear exactly as they run.

## Commits and releases

- Commit by topic with a conventional prefix and the version in parentheses, for example `fix(recover): ... (v0.9.5)` or `docs: ...`.
- A release updates, in this order: the skills and tests; `VALIDATION.md` (test counts, live tests; short, no review or benchmark narratives); the version line in `README.md` and `version` in `.claude-plugin/plugin.json` (plugin users only get an update when it changes); then `SHA256SUMS` as the last commit:

  ```sh
  git ls-files | grep -vx -e SHA256SUMS -e .gitignore | xargs shasum -a 256 > SHA256SUMS
  ```

- Tag `vX.Y.Z` (annotated) on the checksum commit and build the ZIP from the tag:

  ```sh
  git archive --format=zip --prefix=spot-trainer/ -o spot-trainer-vX.Y.Z.zip vX.Y.Z
  ```

- Build each release on its own branch, `release/vX.Y.Z`, in a separate worktree (the shared checkout stays on `main`). Merge it into `main` through a pull request only after the tests, the review and the live test pass (a merge commit, no squash); tag on `main`. Live-test fixes land on the release branch first.
- Each release's scope goes in the release notes on the tag (the GitHub release text), written for users: what changed and why, without links to private records.
- Research notes, reviews, benchmarks and live-test logs stay out of this repository. The maintainer keeps them in the gitignored `dev/` folder; never force-add it, and never run `git clean -x` or `-X`, which deletes it.
