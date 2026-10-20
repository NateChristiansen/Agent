---
id: 25
title: Caveman lite is held by an output style and a Stop hook, not a reminder
status: accepted
date: 2026-09-24
supersedes: null
superseded_by: null
tags:
- adr
links: []
---

# ADR-0025: Caveman lite is held by an output style and a Stop hook, not a reminder

## Context

Sessions here were set to caveman full, and a UserPromptSubmit hook injected the reminder on
every prompt. Over a long manager session, full held for a few hours and then collapsed: articles
per 100 words and mean sentence length rose, and one filler phrase family ("worth
noting/saying/flagging") recurred. The reminder fired on every one of those turns. AGENTS.md also asked for full in chat and
lite on disk, so the session carried two contradicting instructions. The owner asked for lite
everywhere by default, held without drift. A Claude manager and a Codex manager designed this
independently, and it reconciles the two.

## Decision

Caveman lite is carried by a project output style and enforced only where enforcement is
decidable. The `caveman-lite` output style in `.claude/output-styles/` is set as `outputStyle`
in the checked-in `.claude/settings.json`, so it goes out with every request as Claude Code's
own instructions. It keeps the coding instructions. `.caveman.json` defaults to `lite`, so the
plugin's reminder agrees with it. The `style-guard` Stop hook blocks a reply once when it
contains a phrase from `style.TIC_PATTERNS`, names the phrase, and lets the rewritten reply
through. Sentence length, articles and reply length are measured by `agent style drift`, not
gated.

## Consequences

- The binding part is narrow on purpose. It covers phrases that are wrong anywhere, not
  signals that are only suspicious.
- A good first reply proves nothing, because the old setup also looked fine for hours.
  Verification is a long session with `agent style drift` flat throughout, plus a
  deliberate tic that the hook blocks once and does not loop on.
- If the soft signals still drift with the style on, that is the evidence for a stronger
  mechanism. Until then they stay measured, per ADR-0024.
- This is Claude only. Codex has no output styles.
- Amended by [[adrs/0058-harness-state-after-audit-5|ADR-0058]]: `style-guard`, `agent style drift` and the repo's caveman hooks are retired; the output style remains.

## Alternatives considered

- **A stronger reminder.** Rejected on the measurement. The reminder already fired every turn.
- **Gating filler words and sentence length.** Rejected. Filler was never the problem in the
  data, and a length cap blocks legitimate technical replies full of paths and commands.
- **Staying on full.** Rejected by the owner. Lite reads better and is what persisted text
  already used.
