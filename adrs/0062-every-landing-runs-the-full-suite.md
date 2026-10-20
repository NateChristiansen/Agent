---
id: 62
title: Every landing runs the full suite
status: accepted
date: 2026-10-18
supersedes: [47, 53]
superseded_by: null
tags:
- adr
links: []
---

# ADR-0062: Every landing runs the full suite

Supersedes ADR-0047 and ADR-0053 in full, and the small landing tier that grew on ADR-0045.
Amends ADR-0045.

## Context

Two cheaper landing tiers sat beside the full pass record. The notes-only tier (ADR-0047, widened
by ADR-0053) certified a prose-only change with scoped tests chained to a full record. The small
tier (`land_tier.py`) certified a scoped change with its selection plus a smoke set
(`tests/smoke.toml`), up to four links after a full pass, when no broad or sensitive path changed
and the estimated cost beat the median full run. Both kept their own records, chain walks,
recovery and push branches, and their tests. Feature 083 is cutting the full suite to about two
minutes. A full run that short leaves the tiers little to save, while their code and tests stay a
cost of their own. The owner decided on 2026-10-18 to remove both tiers.

## Decision

Every `agent land`, prose included, certifies the exact candidate tree with a full pass record
(ADR-0041). An existing applicable record for that tree is reused. A batch still gets one full
certificate for its combined tree.

- Removed: `land_tier.py` with its K=4 link cap, chain walk and cost estimate; `tests/smoke.toml`;
  `notes_only.py`; `harness/notes-only.toml`; `agent verify full-if-due`; the journal's
  `full_because` and `small_links` fields; the small link in `agent verify show`.
- Recovery rolls a landing forward only on a full record. The pre-push gate, doctor and session
  start accept only a full record on a tip, the rule they already applied to one.
- Small and notes-only records left under `.agent/verify/` are never read, so none certifies a
  landing or a push.
- Dispatch scoped checks are unchanged: `tests/selection.toml` keeps its bundles, fallback and
  `[[broad]]` rules, which dispatch selection reads.

## Consequences

- A prose-only landing waits for the full suite. A slow suite slows every landing, so suite time
  becomes the number to watch.
- One certificate kind remains, plus the retry-passed variant of a full run. Land, recovery and the
  push gate share one rule.
- ADR-0045 keeps its rule that dispatches run scoped checks and the full suite runs once, at
  landing.

## Alternatives considered

- Keep the notes-only tier for lessons alone: it would keep the chain, record and push branches
  for a two-minute saving.
- Keep the small tier with a lower link cap: its eligibility rules and cost estimate were the
  complexity, and the cap did not change them.
