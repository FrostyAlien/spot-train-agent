# Official SkyPilot skill (required)

Spot Train Agent runs on SkyPilot, and its skills hand SkyPilot CLI, YAML, SDK and diagnostic details to SkyPilot's official `skypilot` skill instead of copying them. Install it together with this suite.

- **Source:** https://github.com/skypilot-org/skypilot, path `agent/skills/skypilot` (SkyPilot contributors, Apache-2.0). This repository does not redistribute it.
- **Claude Code:** `.claude-plugin/plugin.json` declares `skypilot@skypilot` as a dependency and `.claude-plugin/marketplace.json` allows that marketplace, so installing Spot Train Agent installs it once SkyPilot's marketplace is added. The plugin does not load without it. Commands: [INSTALL.md](INSTALL.md#2a-claude-code-install-the-plugins).
- **Codex and other hosts:** copy `agent/skills/skypilot` next to the nine skills, with SkyPilot's `LICENSE` ([INSTALL.md](INSTALL.md#2b-codex-or-claude-code-without-plugins-copy-the-skill-folders)).
- **Docs:** https://docs.skypilot.ai/en/latest/getting-started/skill.html

Installing the skill does not install the SkyPilot client, log in to a cloud or rent anything.

## How the two fit together

The lifecycle skill's `references/official-skypilot.md` says which official reference to read for what. The experiment's rules stay in charge where the official examples are broader: the provider and region are chosen once and kept, recovery stays on the chosen purchase mode unless an on-demand fallback was approved in advance, checkpoints count only once committed and verified, attempts, waiting and spend are bounded, secrets go only through SkyPilot's `secrets`, and cleanup touches exact IDs only. Recovery is SkyPilot managed jobs plus the container's restore step, not an agent that stays online.

The official skill's revision is not the installed SkyPilot client or server version: match runtime flags and provider behaviour to the SkyPilot version chosen at the start of a project.
