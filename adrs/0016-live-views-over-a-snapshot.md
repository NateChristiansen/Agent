---
id: 16
title: 'Live views: the CLI writes facts, Obsidian renders the views'
status: accepted
date: 2026-09-17
supersedes: null
superseded_by: null
tags:
- adr
links: []
---

# ADR-0016: Live views: the CLI writes facts, Obsidian renders the views

## Context

The owner wanted the Dashboard current while managers and workers run. It went stale between renders: the Managers table was spliced in by a hook, the `today` link and open count were baked in, and everything that changes during a run (dispatch records, the `.out` stream, session activity) lives in `.agent/` JSON, which Obsidian never indexes. The splice also kept the notes repo dirty. A spike showed DataviewJS can read `.agent/` and redraw on a timer.

## Decision

The CLI writes facts and Obsidian renders every view on the Dashboard; the CLI stops writing view content into notes, STATUS.md excepted.

- Facts are frontmatter, plan checkboxes, log callouts, and `.agent/live.json`: one snapshot of managers, live workers (last activity, last tool), leases and what needs the owner, built in Python from `dispatch.is_live`, `managers.rows` and `leases.status`. Hooks, the supervisor (attempt start, end and a 5 s heartbeat) and `status render` write it.
- Views are shared scripts in `harness/obsidian/views/` called with `dv.view`: Bases for frontmatter boards, DataviewJS for the snapshot, open tasks and the log. A script reads and formats; it holds no rule.
- STATUS.md, Dashboard.md and Index.md are plain generated files in the clone, not symlinks into the notes repo: Obsidian does not watch a symlinked file at the vault root, and can save a stale Dashboard back over a render.

## Consequences

- Amends ADR-0004: Dataview is required, with JavaScript queries on (`obsidian setup` writes it, `doctor` checks it); Bases stays for frontmatter views.
- Amends ADR-0015: the Managers splice is gone; manager state reaches the Dashboard through the snapshot, and the Managers list starts from Claude Code's own session list.
- Amends ADR-0012: the three root notes leave the private set; `status render` replaces an old link.
- Dashboard.md changes only when its layout does, so the notes repo no longer churns.
- STATUS.md stays generated markdown: sessions read it raw, not rendered.
- The views need Obsidian desktop (`require("fs")` for the stream tail) and undocumented Dataview internals (`dv.component`); both fail soft.
- Amended by [[adrs/0059-doc-audit-adr-corrections|ADR-0059]]: doctor no longer checks Obsidian or Dataview.

## Alternatives considered

- Mirror live state into handoff and session frontmatter for Bases: frequent writes to tracked notes.
- DataviewJS reading the raw dispatch and session JSON: a second copy of `is_live` in JS.
- Keep the symlinks and push each render into Obsidian with `obsidian eval`: depends on the CLI being on PATH and on an undocumented API.
