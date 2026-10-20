---
id: 61
title: Remove the scrub check; export identity moves to agent.yaml
status: accepted
date: 2026-10-18
supersedes: null
superseded_by: null
tags:
- adr
links: []
---

# ADR-0061: Remove the scrub check; export identity moves to agent.yaml

## Context

The scrub check matched a private term list, `scrub.yaml` in the notes repo, against tracked files, commit metadata and pushed commits. It ran in `agent export`, in the pre-push hook, in an optional standalone pre-push hook, in `lesson draft_check`, and as the `scrub_check` and `scrub_pre_push` MCP actions. It existed to keep private detail out of public harness history. An export rehearsal before the public start showed the terms file held one placeholder term, so every clean result checked nothing. `scrub.yaml` also supplied export's public author identity.

## Decision

The scrub check is removed: its modules, the pre-push step, the standalone hook installer, export's file and metadata checks, the `lesson draft_check` field, both MCP actions, setup's empty `scrub.yaml`, and the docs. Setup removes an installed standalone hook marked `agent-push-scrub`, because it calls a command that no longer exists.

Export's public author identity moves to a shared `export.identity` key (`name`, `email`) in `agent.yaml`. Export refuses without it and never falls back to git config, so a private address cannot author the public commit.

Export keeps its structural checks: the private set (`notes.PRIVATE_SET`) must be absent from the tree, and `--remote` may not name the source repository. The pre-push hook keeps the export-only gate (ADR-0026, ADR-0060) and the pass-record check (ADR-0041, ADR-0047).

The transcript credential filter `raw scrub` is a separate mechanism with no term list. It stays, renamed `raw redact`.

## Consequences

Nothing checks for private words in public files any more. Keeping private content out is now structural: private records live in the notes repo, and export refuses a tree that carries the private set. Review of each change is the remaining guard for prose.

ADR-0026 now reads: export squashes, restores gitlinks and checks the private set is absent; it no longer scrubs files or commit metadata, and the standalone scrub hook is gone. ADR-0060 now reads: a public clone whose registry lists the harness gets the pass-record gate; no scrub runs, and owner machines keep no terms file.

## Alternatives considered

- Fill `scrub.yaml` with real terms: a term list catches only words someone thought of, and the rehearsal showed nobody kept it current.
- Keep the identity in `scrub.yaml` alone: one surviving key would keep the file and its loader alive for no check.
- Read the identity from git config: a checkout's git config may hold a private address, which would then author the public commit.
