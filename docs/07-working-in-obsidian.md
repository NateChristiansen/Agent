# Working in Obsidian

The vault puts your manager conversations beside the plans and records they use. Start with the Dashboard for current work, and use the Index for the history.

## Read the Dashboard

The Dashboard brings current work into one live pane, followed by Journal and Log. Start with **Needs you** for decisions, then follow a feature or worker link to its plan, handoff, or terminal view.

- **Needs you** shows questions, blocked workers, and results whose responsible manager is no longer live.
- **Managers** shows what each session is doing and the worker results it owes.
- **Workers** shows state, latest activity, goal, and cost or tokens when available.
- **Open features** and **Open tasks** show the current plans and tasks, updating as the manager marks work complete.

A stale warning means the displayed state has expired. The manager can check the run. Results without a live manager include a **resume feature manager** action.

## Check what needs attention

The Dashboard's **Health** panel summarizes the harness's reports. When everything reported is healthy, it reads **Health: all clear**, with an expandable line such as **4 ok: Notes sync, Hooks, Vault display, lessons: 6 open**. A signal is named once when every machine reporting it is ok. If another machine has a **needs action** or **unknown** row for that signal, each ok row keeps its machine name, such as **Notes sync · mac**, where **· mac** is a machine tag. The **N ok** count is the number of names listed. Click that line to see the individual signals and their meaning.

Rows marked **needs action** or **unknown** stay visible, and the heading counts each kind. Each row explains what the signal means and gives a next step after an arrow. **Needs action** points to a reported problem; **unknown** means the panel cannot establish the current state, for example because a report is missing or incomplete. Follow the row's explanation: an unknown report may need refreshing rather than a repair.

**Notes sync** shows whether work records reached [your notes repo](08-your-notes-repo.md). A **conflict**, **halted**, **error**, **config**, or **claims** report needs the manager's help. For a halt, fix the underlying problem and run the clear command named in the halt message. An offline or busy sync retries by itself as work continues. Leave it to retry, and check the network if it stays offline. **Hooks** reports today's errors, which may have left a tab icon, usage record, or sync unfinished. Tell the manager, “Show today's hook errors and what failed.” For **Vault display** failures, follow the row's advice to reload the Agent Terminal plugin and check that Obsidian's CLI is enabled; [troubleshooting](14-troubleshooting.md) covers display problems.

If you use pairing, **Pairing link** shows the last message's delivery status. An accepted message is send history, not proof that the other machine is reachable now. A message still undelivered after ten minutes needs a check of SSH and whether receiving is enabled; see [pairing two machines](11-pairing-two-machines.md).

The **lessons: N open** count is the queue of findings waiting to be routed because they have no open feature. At ten or more, the row is marked **needs action** and **review needed**, and feature closure waits for the queue to be reviewed and routed. Tell the manager, “Review the queued lessons and show me which need a fix, a task, or an upstream issue.” The row can also read **lessons: unknown — snapshot stale**; wait for the next snapshot or ask the manager to refresh it. [Day to day](05-day-to-day.md#record-something-that-went-wrong) explains how those findings become fixes.

Machine reports become stale after fifteen minutes. On this machine, expired signals turn **unknown** with their last report; the next completed manager turn reports again. For another machine, **no report** shows how long it has been silent, with any previous problems retained as **last report** rows. Those are history to check, rather than confirmation that the problem persists. Nothing needs doing if that machine is off; otherwise open a manager there so it reports. Follow the same advice for **no report today**: open a manager on that machine if it is on. If a row says the lesson queue or another source is unreadable, have the manager check it before relying on the count.

## Find plans and history

The feature views include an open table, a board grouped by status, and a closed table. The Index carries closed features, decisions, document views, handoffs, and the journal archive. You can move from a feature's plan to its log and worker results without searching by session name.

The manager updates tasks and refreshes these generated views from the records. Keep feature and decision folders at their existing names; moving them in Obsidian can break their links.

## Use manager tabs

The ribbon has a launcher for each installed client. Hover over a button to find its **New … manager** label. A launcher opens a pinned terminal tab in the harness folder.

The manager state appears at the start of its tab title:

| Icon | What it means |
|---|---|
| ✋ | A question or approval waits on you |
| ⏳ | Work the session started is still pending |
| ⚙️ | The manager is running a turn |
| ✅ | The manager is idle |

A question mark after the icon means its state is stale. Finished results waiting for the manager appear as **owes N** in the Managers table.

Plans and work orders arrive with a vault link in the manager conversation. Open the note to read it before approving. If the work order changes, the manager brings you a new preview.

Click a manager tab to focus its terminal. After an Obsidian restart, open managers again from the ribbon and name the feature you want to continue.

## Adjust the vault

Setup installs the vault's plugins and settings. Configure your own ribbon shortcuts and hotkeys in Obsidian. The Git plugin's starting settings disable automatic commits, pushes, and pulls; [notes sync](08-your-notes-repo.md) handles the work records separately.

Agent Hidden Files shows the harness's selected dot folders while keeping large caches out of the index. Leave Show Hidden Files off for normal use. If a view or terminal stops updating, use [troubleshooting](14-troubleshooting.md).

Next, see where your work records live in [your notes repo](08-your-notes-repo.md).
