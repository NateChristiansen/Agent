---
name: agent-config
description: "Inspect or edit the harness configuration in agent.yaml and agent.local.yaml: the owner, forge, worker MCP profiles, or one repo. Detect repo settings from a fresh clone and ask for unresolved values."
allowed-tools: mcp__agent__*, Read, Edit, Write, Glob, Grep, AskUserQuestion, Bash(git clone:*), Bash(git fetch:*), Bash(git ls-remote:*), Bash(git ls-tree:*), Bash(git show:*), Bash(git remote get-url:*), Bash(git config:*), Bash(git -c:*), Bash(git ls-files:*), Bash(git status:*), Bash(gh repo view:*), Bash(glab repo view:*)
---

# agent-config

Invoke as `/agent-config` in Claude Code, or `$agent-config` (or select it through `/skills`)
in Codex. Input is the arguments or the owner's request. Read [[harness/agent-yaml]] before
editing: it defines the keys, defaults, local overrides, and migration. This skill changes
configuration; product changes still go through `/dispatch`.

## Choose the section

| Arguments | Action |
|---|---|
| none | Summarize the effective configuration and ask what to change. Write nothing yet. |
| `owner` | Set `owner.name` to the name the owner supplies. |
| `forge` | Set the shared default forge (`github` or `gitlab`). Explain which repos inherit it. |
| `mcp <profile>` | Add or edit that one `worker_mcp` profile. |
| `repo [name, path or URL]` | Follow [the repo procedure](references/repos.md), stages S0-S8 in order. Ask its one decision batch in consecutive `AskUserQuestion` calls of at most four questions (Codex: one numbered list), same questions in the same order. Write and validate below are its S6-S7. |
| another section named by the owner | Use the guide to edit that section; ask if the intent is unclear. |

For an overview, call the `repos_list` tool to validate both files. Read the shared configuration
and local overrides, then summarize the owner, effective default forge, MCP
profile names, and repos. For each repo show the effective forge, default branch, resolved
clone path and whether the clone exists. Distinguish defaults from missing requirements:
omitted top-level `forge` means `github`; an omitted repo `forge` inherits that top-level value.
Neither omission is an error. Flag placeholder owner and example repos. Show
profile names and whether prerequisites exist; keep credentials and environment values out of
the summary. Report validation errors before offering an edit.

## Edit one section

Restate the requested change. Ask for values that cannot be detected; do not replace an
existing setting merely because another value looks more conventional. Changing the default
forge affects worker permissions for every inheriting repo: show that set before the edit.

For MCP edits, read [[harness/worker-permissions]] and [[harness/provider-gaps]]. Ask for the
server command, pinned package/version where applicable, arguments, required files, and tool
access that cannot be established from existing configuration. Preserve existing allow/deny
rules and environment settings outside the requested change. Show permission changes for the
owner's decision before writing them. Configuring a profile does not launch its server or
authorize a dispatch, login, external write, or credential capture.

Shared values belong in the notes repo's `agent.yaml`. Call the `harness` tool with
`action="notes_root"` and `action="notes_status"` to identify the canonical file and link state before editing.

Machine-specific `path` and `worktrees_dir` belong in the clone's `agent.local.yaml`. It is
never linked into the notes repo. Use the repo's short consistently. Relative paths resolve
from the clone root; absolute paths and leading `~` are allowed locally. Each supplied value
is a non-empty string; omitted keys inherit and `null` fails. The file's other top-level keys
are `notes_sync`, `machine`, `maintenance`, `hooks` and `harness` (`config_file.LOCAL_KEYS`);
leave them as they are. `dispatch new` takes its id tag from `machine:` or, without it, the
enrolled device name, and refuses when neither exists (ADR-0036).

A repo entry and every other section of `agent.yaml` are changed the same way, through the
`harness` tool's `notes_write` action, never with Edit or `apply_patch` on `agent.yaml` itself.
It is a symlink into the notes repo (a hard link on plain Windows), and both tools replace the
file, which severs a hard link. Call `notes_write` with `target="agent.yaml"` for a scratch
draft, make a narrow change to the draft with Edit (Codex: `apply_patch`), call it with
`from_file=<draft>` for the diff, then with `apply=True` and the preview's `before_sha256` as
`expect_sha256`. The result's `link` says whether the link held; `notes_status` shows a severed
one. `agent.local.yaml` is a plain file: edit it directly. Show the owner the diff before a
shared change. Preserve unrelated sections, comments, defaults and permissions. Do not rewrite
YAML by dumping the whole mapping. Inspect the result; never delete a recovered copy or overwrite
an unexpected local edit to make validation pass.

## Validate and report

Call the `repos_list` tool after the edit: it loads and validates both files. Re-read the
changed section and inspect the diff. If validation fails, fix only errors introduced by this
change; report pre-existing errors for an owner decision. Use the `harness` tool with `action="doctor"` when resolved
paths or environment health need checking, and distinguish its unrelated findings. For a repo,
also run `notes_status` and `dispatch new dry_run=True` per intended provider, copy refusals
verbatim, and finish with `configured` or `blocked: <named prerequisite>`, never "ready".

Record the decision in the mapped feature. Report the changed keys, which file holds them,
the validation result, and any unresolved prerequisites. No commit or push is implied by
invoking this skill. Shared private changes are staged in the notes repo when authorized;
`agent.local.yaml` stays untracked.
