# Codex and Claude Code compatibility

Checked 2026-10-01 against official documentation:
- Codex: https://developers.openai.com/codex/skills (redirects to https://learn.chatgpt.com/docs/build-skills)
- Claude Code: https://code.claude.com/docs/en/skills
- Shared format: https://agentskills.io/specification

The common payload is a directory per skill containing `SKILL.md` with `name` and `description`, and local `references/`, `assets/`, `scripts/` when useful. `agents/openai.yaml` is optional Codex UI metadata and is not required for execution in Claude Code. No proprietary MCP or assistant-memory tool is required by this suite.

For project-local installation, put complete skill directories in `.agents/skills/` for Codex and `.claude/skills/` for Claude Code. Make actual copies or use a managed installation method appropriate to that host; do not assume cross-host symlink handling. Do not overwrite existing skills without inspecting them and user approval. Keep the complete relative file tree; copying SKILL.md alone drops the referenced helpers.

Invoke Codex with `$gpu-training-lifecycle` and Claude Code with `/gpu-training-lifecycle`; natural-language discovery depends on each host. Confirm discovery in a fresh session. Installed host paths may differ or be renamed; discover companions by frontmatter names. Commands inside a skill must resolve its own bundled resource directory, not assume the caller's current directory.

Python helpers require Python 3.10+ and otherwise declare dependencies individually. They are offline validators/calculators, not cloud agents. SkyPilot, cloud CLIs, Docker and framework versions must be checked separately on the execution host. The suite does not install software or grant cloud permissions merely by being loaded.

The source instructions are English for portability; explanations and reports should follow the user's language. Return exact command output and errors without translating machine-readable identifiers.
