---
id: 9
title: Workers are driven through the Agent SDK; the homegrown stream-json and single-shot drivers go
status: accepted
date: 2026-09-08
supersedes: null
superseded_by: null
tags:
- adr
links: []
---

# ADR-0009: Workers are driven through the Agent SDK; the homegrown stream-json and single-shot drivers go

## Context

[[adrs/0006-dispatch-and-handoffs|ADR-0006]] made a worker a headless `claude -p` process driven by our own supervisor. A later change added a second driver (stream-json with stdin held open) beside the single-shot one. An audit found that the launch, stream, envelope-recovery and resume-relaunch code, roughly 450 lines of `dispatch.py`, was where the two costliest bug classes lived. That code had been patched after one dispatch failure after another. The owner's aim is fewer homegrown lines that track the CLI. A spike ran a real worker through `claude-agent-sdk` 0.2.152, with the harness's own worktree, permissions, MCP profile, schema and contract. Nearly every requirement verified on the first run, for well under a dollar. The steer landed inside the running turn. The budget stop looked like the CLI's.

## Decision

Workers are driven through the Agent SDK's `ClaudeSDKClient`. Both homegrown drivers (`_launch_claude`'s `communicate` path and `_drive_stream`) are deleted in the same change, along with `build_argv`, the stdin helpers, `parse_envelope`/`stream_results`/`_json_object`, and the `--resume` relaunch. `cli_path` is the `claude` on PATH; the SDK's bundled CLI is not used. The supervisor keys off each result's `subtype` and `structured_output.status`, never off a count of results, with a watchdog after any result. `allowed_tools` and `disallowed_tools` keep today's rule strings on day one. Everything that is policy stays where it is: worktrees and reuse, the budget rules of [[adrs/0008-dispatch-policy-after-audit|ADR-0008]], preflight, `evaluate()`'s decision table, the schema and nonce check, `_synthesize`, handoffs, notifications, the detach wrapper, `transcript_path()`.

## Consequences

- One runtime dependency becomes two (`pyyaml`, `claude-agent-sdk`), against
  [[adrs/0002-python-over-rust|ADR-0002]]'s preference; the trade is protocol code we no
  longer own.
- The fake `claude` in `tests/conftest.py` becomes a fake transport or a CLI fake that speaks
  the SDK's control protocol. That is most of the rewrite's cost.
- A steer reaches the worker mid-turn. `dispatch say` gets cheaper and the worker contract's
  line about mid-run messages gets truer.
- The return is still null on an interrupt or a budget hit, and the cap is still checked at
  turn boundaries; neither is new.
- The SDK version and the PATH CLI version can drift; `doctor` reports both, and a mismatch
  is a fact to record, not a reason to use the bundled binary.
- Open until the first migrated dispatch: whether the session shows in `claude agents --json`
  and takes an `@<name>` message.
- amended by [[adrs/0011-harness-state-after-final-audit|ADR-0011]] (2026-09-15): the open
  item is answered by a probe (yes to both), and `can_use_tool` is adopted as a deny-only
  recorder behind the rule strings.
- amended by [[adrs/0059-doc-audit-adr-corrections|ADR-0059]] (2026-10-12): doctor no longer reports the SDK and PATH CLI versions.

## Alternatives considered

- Stay on stream-json and delete only the single-shot path: smaller this month, one
  dependency, protocol code stays ours, no mid-turn steer. Rejected on the aim.
- `claude --bg`: rejected in the stream-json design on losing
  the schema, the budget and the cost record; unchanged.
- An ACP adapter: the SDK with a second protocol layer and a new dependency; rejected there
  too.
- Moving permission decisions into `can_use_tool` now: deferred. Whole-tool allow entries shadow the callback. That would change behaviour on day one for no gain, until the rule strings are redesigned.
