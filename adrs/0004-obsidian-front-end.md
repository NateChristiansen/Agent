---
id: 4
title: Obsidian is the front end; the CLI generates the vault artifacts
status: accepted
date: 2026-09-03
supersedes: null
superseded_by: null
tags:
- adr
links: []
---

# ADR-0004: Obsidian is the front end; the CLI generates the vault artifacts

## Context

the owner needs one place to read the harness's notes, run claude sessions, and see what is in flight. code editors are built for code; the harness is markdown. the owner already likes obsidian. in 2026 it has a core database feature (bases), a CLI, a mature terminal plugin, and official agent skills from its maintainer. the risk is a second source of truth: obsidian plugins that rewrite frontmatter or files the CLI owns.

## Decision

the agent repo opens as an obsidian vault, and obsidian is the primary front end. the CLI stays the only writer of generated files, and now also generates the vault-facing ones: `Dashboard.md`, `bases/*.base` views over frontmatter, typed callouts for log entries, `tags:` and `links:` frontmatter on features and ADRs, and the curated `.obsidian/` config (`bin/agent obsidian setup`). sessions run in the Terminal plugin, so every hook fires. the obsidian CLI is wrapped fail-open for open/daily/search and auto-open. plugins are chosen by downloads, age, and last update. anything that rewrites CLI-owned files is excluded or configured off: linter on save, bases toolbox, kanban, tasknotes, templater's new-file trigger, git auto-commit.

## Consequences

- Amended by [[adrs/0016-live-views-over-a-snapshot|ADR-0016]]: Dataview is required and renders the live Dashboard views.
- Amended by [[adrs/0059-doc-audit-adr-corrections|ADR-0059]]: doctor no longer checks the vault or the generated surface.
- the vault renders the record (board, tables, graph, backlinks) with zero token cost.
- one more generated surface to keep in sync; `agent status render` owns it and doctor checks it.
- `.obsidian/` is partially committed (curated config), the rest ignored; obsidian's own
  rewrites of the core json files cause harmless churn.
- the log format changed to callouts; earlier logs were migrated once.
- the owner installs plugins and enables the obsidian CLI by hand; setup reports what is missing.

## Alternatives considered

- claude code desktop app: good session sidebar, no markdown reading, does not see CLI sessions.
- vscode + extension: nothing obsidian lacks; weaker linking and no database views.
- nimbalyst / conductor: bring their own tracking, which duplicates the CLI's record.
- dataview-only dashboards: works, but bases is core, faster, and the CLI can generate it.
- terminal agent tabs plugin: 572 downloads; the plain terminal plugin has 407k.
