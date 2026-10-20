---
paths:
  - "features/**"
---

# Feature files

- `plan.md` body sections, in order: Context, Decisions, Tasks, Open questions, Verification,
  Handoffs, Notes. Keep the headings; leave a section empty rather than deleting it. Handoffs
  holds a static Dataview view over the feature's handoff files. Dispatch never writes the plan.
- Tasks are checkboxes with a verifiable outcome. The first unchecked task shows up in
  the Dashboard as "next task", so keep the list ordered. Add and tick them with the `task_add`,
  `task_done` and `task_undo` tools, never by editing the checkbox; tick before you report
  something as done.
- Frontmatter is CLI-owned where a tool covers the field. `status` goes through `feature_set`
  and `feature_close`. `title`, `tickets`, `repos`, `adrs` and `tags` go through `feature_set`.
  Hand-edit only fields no tool covers, such as `links`. The CLI refuses a malformed field
  when it next reads it.
- `log.md` is never hand-edited. The hook denies it. Use the `log` tool. Raw hook captures
  no longer live under a feature's own directory; they are at `.agent/raw/NNN-slug/`, also
  never hand-edited.
- Write Context and log entries for the owner in six months: what, why, what we would do
  differently. Short sentences. No play-by-play.
