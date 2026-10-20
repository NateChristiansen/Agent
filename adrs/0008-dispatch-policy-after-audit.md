---
id: 8
title: 'The harness never answers for the owner: no auto-resume of a question, no budget raise, preflight and effort before launch, one worktree per feature and repo'
status: accepted
date: 2026-09-08
supersedes: null
superseded_by: null
tags:
- adr
links: []
---

# ADR-0008: The harness never answers for the owner: no auto-resume of a question, no budget raise, preflight and effort before launch, one worktree per feature and repo

## Context

[[adrs/0006-dispatch-and-handoffs|ADR-0006]] made workers headless claude sessions in per-dispatch worktrees, with a supervisor that "auto-resumes once at 1.5× budget on a budget hit or a first question". A review of the first dispatches showed what that clause cost. In one dispatch, the auto-resume told a worker that had correctly stopped on a denied command to proceed anyway, producing unverifiable work. In another, it answered a design question on the owner's behalf and raised the cap they had approved by half. Separately, two dispatches were blocked by permission rules that could not match the command they were written for. No dispatch ever chose a model or an effort. Every dispatch into a repo with a heavy dependency install paid that install again in a fresh worktree. This ADR amends ADR-0006; the rest of it stands.

## Decision

The harness never answers for the owner. A `question` return ends the attempt in state `question` and waits. A budget hit ends in `blocked` with a resume hint. The budget is only ever raised by `dispatch resume --budget N --reason`. The one automatic retry left is a timeout, once, at the same budget. Before launch, every command a work order names (fenced blocks, `$ ` lines, backticked commands) is run against the worker's permission rules with Claude Code's documented matching. A launch refuses on a miss unless `--allow-unmatched`. Every dispatch records `model`, `effort` and a `choice_reason` from `harness/dispatch-rubric.md`, and passes `--effort` to the worker. One worktree serves a feature and repo pair. A new dispatch reuses it when the last dispatch on it is terminal, not waiting on the owner, and the tree is clean. Otherwise it gets its own, as before.

## Consequences

- A worker that asks waits for a human; that is the point. Nothing spends past the number the
  owner approved.
- A dead permission rule is found in the preview, not by a worker mid-run. Extraction is
  best-effort: a command written as prose is invisible to it.
- The rubric is a guess until `/retro` fills its calibration rows; the preview says which row
  and why, so the guess is visible.
- Worktree reuse means node_modules survives between dispatches; `resume` checks the branch
  before continuing, so a repurposed worktree fails loudly.
- `claude --bg` as the launcher, named as a fallback in ADR-0006, was never built and is
  dropped; the transport question goes to an Agent SDK spike.

## Alternatives considered

- Keep auto-resume for `question` with a smarter prompt: rejected; the worker asked because
  it lacked information, and no prompt from the harness adds any.
- A raise ceiling (auto up to 2× the approved budget): rejected; the approved number is the
  ceiling or it is not a number.
- Preflight as a warning only: rejected; a warning in a preview the owner approves in one word is
  the same as no check. `--allow-unmatched` is the explicit override.
- One worktree per feature regardless of state: rejected; a blocked dispatch's worktree is the
  one the owner is told to resume into, and a later dispatch stealing it would switch its branch
  underneath.
