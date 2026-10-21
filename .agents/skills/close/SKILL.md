---
name: close
description: "Close a feature: walk its Verification and Tasks, write the final hindsight entry, feature_close. User-invoked only."
user-invocable: true
disable-model-invocation: true
allowed-tools: mcp__agent__*, Read, Edit, AskUserQuestion
---

# /close

Input: $ARGUMENTS (feature ref) or the current feature.

A request record does not use this skill. The manager completes it once the ask is delivered:
`request` tool, action `complete`, with the delivered `result` (and `status: abandoned` when the
owner drops it). It is refused, with nothing changed, while any of its dispatches is queued or
running, waits on an answer, an unread verdict or a branch decision, or has worker or verifier
cleanup pending, and while an added task is open. It needs no retro, no lesson-queue room and no
journal step; `feature_close` on a request applies the same checks. Cleanup, transcript copying,
logs and handoffs work as for a feature.

1. Read the feature's plan.md. Walk `## Verification` and `## Tasks`.
2. Anything unchecked or unverified: ask the owner, close anyway or not yet. Do not close on a
   guess.
3. If `features/NNN-slug/retro.md` does not exist, run `/retro <n>` now. The retro is the
   hindsight; the closing note below points at it. Skip only if the owner says the feature had no
   runs worth reviewing (a one-line config change, an abandoned stub); put their reason in the
   closing entry. `feature_close` does not check, and session start lists closed features with
   no retro.
4. Final log entry: what worked, what we would do differently, anything measured, and the
   retro path. `log` tool, kind `decision`, text "closing: ...", `feature: <n>`. (The same
   kind `feature_close`'s own `note` parameter writes; one closing entry, one kind.)
5. `feature_close` tool with `feature: <n>` (and `status: abandoned` when it is). The CLI
   sets the closed date, copies the feature's transcripts into `.agent/raw/NNN-slug/transcripts/`,
   cleans up worktrees, and re-renders Dashboard.md.
6. Confirm the feature is gone from Dashboard.md's open list.
7. On the journal machine, run `/journal today`: a close is a journal milestone, and nobody else
   will write it. Another machine skips this step; its `feature_close` result carries no journal hint. A failed
   journal does not undo the close; say so and move on.
8. Report in three lines.
