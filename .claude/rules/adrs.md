---
paths:
  - "adrs/**"
---

# ADRs

- Created only by the `adr_new` tool (number allocation, feature linking). Then fill the body
  by hand.
- One decision per ADR. State the decision in the first sentence of the Decision section.
- Sections: Context, Decision, Consequences, Alternatives considered. Keep it under 40 lines.
- To change a decision, write a new ADR with `adr_new`'s `supersedes`. Never rewrite the old one; the
  CLI links both ways and marks the old one superseded.
- To correct a paragraph that went stale while the decision stands (a count, a command that
  was deleted, an open item that got answered), write one amending ADR that names the lines
  and what they now read as, and add a one-line pointer under the old ADR's Consequences.
  Nothing else in the old ADR changes. ADR-0011 is the first of these.
- A public harness ADR (`adr_new` scope `harness`) carries no feature. A private decision
  names its feature.
- `adrs/README.md` is generated. Do not edit it.
