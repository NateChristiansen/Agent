---
id: 41
title: Tree-keyed pass records gate main and publication
status: accepted
date: 2026-10-05
supersedes: null
superseded_by: null
tags:
- adr
links: []
---

# ADR-0041: Tree-keyed pass records gate main and publication

## Context

Every dispatch attempt ran the full suite, yet commits made directly on main were pushed with no
test gate. A full run proves something about one tree under one toolchain, nothing more or less.

## Decision

A full pass of every `dispatch.verify` command against one commit's tree, in an isolated detached
checkout under the existing verifier runner, is stored as a tree-keyed pass record, and both
`agent land` and the pre-push hook require one for each tree they move main to or publish.

- Store: `<main>/.agent/verify/v1/<tree>/<key>.json`; the key digests the environment (uv, git,
  Python, OS and arch, git config, locale and time zone) and the policy (record version, verify
  configuration, and the verify commands' environment by value, minus shell plumbing). Run evidence (terminal snapshot, lifecycle, output log) sits in `runs/<run_id>/`,
  hashed in the record. Reuse is by identical tree only, with per-commit provenance.
- Never full: scoped, exempt, no-diff, filtered (`-k`, `-m`, `--ignore`, test paths), failed,
  interrupted or dirty runs, and records whose evidence is missing, mismatched or not a clean pass.
  Each fails closed. A tree whose attributes name a `filter` is refused.
- The supervisor relies on an applicable record for a clean harness head (verification mode
  `record`, rechecked at acceptance), and a no-diff exemption names the record it relied on.
  This amends ADR-0018: reusing a record for the exact final head is a route to done.
- Workers are refused any command that names `.agent/verify` or runs `agent verify`.

## Consequences

- Codex workers cannot write the store, which is outside their sandbox roots. Claude workers have
  no OS sandbox and can write anywhere the user can, so a record fabricated together with
  consistent run evidence passes until worker containment lands. Records prove an honest run.
- Records are machine-local. Per-session values stay out of the digest, so a record written by
  `agent land` still applies to the owner's later push; the codex version is recorded, not keyed.
- A record run takes as long as the suite and is serialized by a lock.
- ADR-0045: a dispatch never runs the full suite; the landing certificate is the one full run.
- ADR-0047: a notes-only change lands on scoped tests, chained to a full pass record.

## Alternatives considered

- Records in the git common dir: Codex workers can write it.
- Certifying a tree from a run on another tree that differs only in inert paths: a modified run
  proves nothing about the original. Certifying every pushed commit: only each tip is.
