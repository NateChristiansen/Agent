# Day to day

The Dashboard gives you the current work; the manager can explain it and bring the records up to date. Use the same conversation to check progress, review spend, and decide what comes next.

## Pick up where you left off

Open a manager from the ribbon and name the work you want to continue:

> Pick up the login-test feature. Show me what is finished and what still needs me.

The manager picks up the plan and pending results, then reports what needs your attention. For a view across the vault, ask:

> Give me the current status.

It refreshes status and summarizes active work, blockers, and quiet features. You can pause or stop work in the same conversation.

The Dashboard's **Needs you** section separates decisions waiting on you from results a manager still needs to handle. The **Managers** table shows those results as **owes N**. [Working in Obsidian](07-working-in-obsidian.md) explains the rest of the view.

## Keep a daily record

The journal draws from feature logs, worker handoffs, decisions, captured requests, and git history. Ask for today's entry or another date:

> Write today's journal from the records.

The manager writes a short entry from those records in the generated part of the daily note. Your own Notes section remains yours. The note also includes spend and worker-run counts.

Milestones prompt a journal reminder, and closing a feature includes a journal update. When you use several machines, one configured machine writes the journal; see [pairing two machines](11-pairing-two-machines.md).

## Check what work cost

Ask for the cost of a feature, a change request, or the day's activity:

> What has this feature cost, including its retries?

Worker cost includes all attempts. Daily totals also include manager activity, reviewers, and probes, with a completeness indicator when sources or machine records are missing.

The manager can refresh stale figures. You can authorize a larger budget, narrow the next task, or stop the run.

## Record something that went wrong

Tell the manager what went wrong and point at the run, error, or result:

> We spent three attempts on a command the worker could never run. Record the lesson and fix the launch check.

The manager records what happened and where the fix belongs. Findings stay with the current feature or go into a lesson queue when there is no open feature to hold them.

The preferred fix is a deterministic check that catches the mistake before it happens again: a test, validation, hook, or permission boundary. When judgment is needed, guidance goes in the skill read at that decision. A fix can also belong in the touched repo's own setup, improving how future work runs there.

The manager reports the fix and what remains tracked. To revisit the queue:

> Review the queued lessons and show me which need a fix, a task, or an upstream issue.

Filing a lesson upstream needs your approval of the proposed issue.

## Close a feature

Ask the manager to close it when the outcome is verified:

> Close the login-test feature.

It walks the tasks and verification, reviews the runs in a retrospective unless you ask to skip it, and records the outcome. Every task must be done or superseded with a reason before closure. A feature you no longer want can be closed as abandoned.

Closed features leave the open list but keep their plans and history in the Index.

Next, see how to work across [several sessions](06-running-several-sessions.md).
