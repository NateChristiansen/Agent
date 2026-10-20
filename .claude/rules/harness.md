---
paths:
  - ".claude/**"
  - ".agents/**"
  - "src/**"
  - "bin/**"
  - "harness/**"
  - "tests/**"
---

# Changing the harness

- No changelog entry. The commit message carries what and why, attached to the diff that
  caused it. Architectural changes (a new hook event, a new file convention, a CLI surface
  change) get an ADR.
- Pick the mechanism with `/lesson` step 3; its table holds the current decision rule.
- Hooks are Python, in `src/agent_cli/hooks/` (one module per concern, `run.py` holds the
  name table), wired in `.claude/settings.json` and `.codex/hooks.json` through the node loader
  `bin/agent-hook.cjs` (each hook command is the loader followed by `agent hook <name>`, ADR-0031). A hook never raises and always exits 0; it prints only when it has
  something to say (session-start context, a PreToolUse deny). A PreToolUse hook that times
  out fails open, so keep hooks to file reads and small writes. The timeout incident and
  module order are in [[harness/maintenance#The CLI surface]]. Every hook has
  pytest coverage under `tests/`, and a new hook is wired on both clients where both have the
  event (`.claude/settings.json`, `.codex/hooks.json`).
- One process per client event and matcher: a new hook on a grouped event joins its group in
  `hooks/groups.py` as a `Member`. See [[harness/maintenance#Hook groups]].
- Models never run `bin/agent` in a shell (ADR-0033); the `cli-route` hook refuses it. See
  [[harness/maintenance#The CLI surface]].
- Never name a vault file `claude.md` in any case: it loads as instructions on a case-insensitive
  file system, and doctor's `nested instructions` check flags it. For a Claude/Codex pair of
  drafts, use names like `claude-proposal.md` and `codex-proposal.md`.
- `.agents/skills/` is the source of truth for skills. `.claude/skills/` holds symlinks only;
  it is untracked and gitignored, and setup makes the links.
  `obsidian-markdown` and `obsidian-bases` are vendored from kepano/obsidian-skills (MIT,
  `.agents/skills/LICENSE-obsidian-skills`); update them by re-vendoring, not by editing.
  `caveman` is a symlink into the `vendor/caveman` git submodule (JuliusBrussee/caveman, the
  skill is MIT); update it with `git submodule update --remote vendor/caveman` and commit the
  new pointer, never by editing under `vendor/`. `.claude/agents/` holds agent definitions.
- Obsidian config: change what `obsidian setup` writes in `src/agent_cli/obsidian.py`
  (and the snippet/template sources under `harness/obsidian/`), then ask the owner to run
  `bin/agent obsidian setup` (person-only). Do not hand
  edit `.obsidian/plugins/*/data.json` for the agent-owned entries; Obsidian may rewrite the
  core json files itself, which is fine.
- Bases, Dashboard.md and Index.md come from `src/agent_cli/vault/` (`bases.py`, `dashboard.py`).
  Change the templates there.
- AGENTS.md's Standing rules section is the owner's. Add to it only when the owner states a
  rule. `harness/voice.md` and `harness/owner/rules.md` are the owner's; do not edit them.
- Never add a `CLAUDE.md` back, at the root or above it: it makes Claude Code skip AGENTS.md
  entirely and both clients lose their shared context (ADR-0024). Doctor's `project
  instructions` check fails if one appears.
- CLI changes come with pytest coverage in `tests/`; before commit run `uv run ruff check`,
  `uv run ruff format` and `uv run pyright` on the touched files, and the selected tests
  (`uv run pytest tests/test_x.py`). The full suite runs once, at landing (ADR-0045); a
  target-less `pytest` is refused in a dispatch. CI runs all four; a `noqa` needs a reason on the
  same line.
