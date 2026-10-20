---
name: adr
description: "Record a decision that changes a contract or rule others rely on; private notes by default, harness scope only for public design."
user-invocable: true
allowed-tools: mcp__agent__*, Read, Edit, AskUserQuestion
---

# /adr

Input: $ARGUMENTS, or the decision under discussion.

1. Write an ADR only when a made decision changes a contract or rule others rely on.
   If the owner has not chosen, ask. Log other decisions with the `log` tool instead.
2. Decide where the decision belongs. The default `adr_new` scope is private `notes`: it writes
   `decisions/NNNN-slug.md` through the linked notes repo and links the private feature. Pass
   `scope: harness` only for a decision about this public harness that a contributor should
   read beside the code. The public `adrs/` record never names a private feature or path; a
   private feature may link one way to it. A product architecture decision belongs in that
   product repo and goes through `/dispatch`.
3. Fill the body: Context, Decision (decision in the first sentence), Consequences,
   Alternatives considered. Under 40 lines. The owner's voice.
4. `log` tool, kind `decision`: name `ADR-NNNN`, for both private notes and
   the public harness, followed by the title and one-line summary.
5. Report the file path and the one-line summary. An accepted ADR is edited only to add one
   "Superseded by" line linking its replacement. No rewording passes.
