---
id: 60
title: Public clones push directly; export is the one-time flip
status: proposed
date: 2026-10-12
supersedes: null
superseded_by: null
tags:
- adr
links: []
---

# ADR-0060: Public clones push directly; export is the one-time flip

## Context

ADR-0026 made `agent export` the only public route. The source checkout held private history, and its `origin` was the public repo, so its pre-push refused every push. The public start exports one squash into a new repo, named by `agent export`'s required `--remote`, which refuses the source repository. Each owner machine then runs from a fresh clone of that repo. Private records live in the linked notes repo and never enter harness history, so a public clone has nothing to hold back. Setup already marks such a clone: it finds the tracked `.harness-export` marker and sets `agent.exportOnly=false` (`gitgate.settle_export_only`).

## Decision

After the public start, the owner's clones of the public repo push `main` directly to it, and `agent export` is not run again for ordinary work. A direct push passes the same pre-push hook as before, minus the export-only refusal. Where the registry lists the checkout, that hook requires each pushed tip to carry a full pass record (ADR-0041) or a notes-only record chained to one (ADR-0047). It then runs the private-terms scrub when terms are configured. `push-guard` and Claude's push ask rule still apply.

The gate fails closed on a clone of the private source repo. When `agent.exportOnly` is unset, no marker is tracked at HEAD and the checkout's harness role is `source`, setup sets `agent.exportOnly=true`, so a push is refused and the export route is the only way out. A tracked marker sets false. Only a readable consumer role leaves the setting unset; consumer pushes are already refused. A missing or unparseable `agent.local.yaml` reads as `source`, so it fails closed: the setting becomes true.

Settle preserves an existing value, so a public clone preset to true stays refused. The owner's maintainer clones need harness role `source`; setup defaults to `consumer`, and a consumer clone of the public repo is left unset.

Known limit: the marker is a tracked-path convention, not proof of ancestry. A private history that tracks `.harness-export` would settle false. No export path writes the marker into the source tree.

## Consequences

The landing route is unchanged: `agent land` moves local main, and a push publishes it. The old private repo is archived and receives no pushes. The export stays as code for a second flip or a recovery, not a routine step. ADR-0026's Consequences say "A public clone gets the same hooks, and its pre-push does nothing", and its ADR-0041 pointer says "a public clone's pre-push still does nothing". Both now read: a public clone whose registry lists the harness gets the pass-record and scrub gates; a clone with no such registry gets the scrub only; a consumer install (ADR-0043) never pushes. The scrub is the last line against a private term in a public push, so each owner machine keeps its terms file.
Amended by [[adrs/0061-remove-scrub-check|ADR-0061]]: the scrub is removed; a registered public clone gets the pass-record gate only, and no terms file is kept.

## Alternatives considered

- Keep exporting every release from a private source: two histories to keep in step, and every change waits for a squash.
- Push from the old checkout with `AGENT_ALLOW_DIRECT_PUSH=1`: that checkout's history is private, which is what the export exists to hold back.
