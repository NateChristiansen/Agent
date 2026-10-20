# Working in Obsidian

The vault puts your manager conversations beside the plans and records they use. Start with the Dashboard for current work, and use the Index for the history.

## Read the Dashboard

The Dashboard brings current work into one live pane, followed by Journal and Log. Start with **Needs you** for decisions, then follow a feature or worker link to its plan, handoff, or terminal view.

- **Needs you** shows questions, blocked workers, and results whose responsible manager is no longer live.
- **Managers** shows what each session is doing and the worker results it owes.
- **Workers** shows state, latest activity, goal, and cost or tokens when available.
- **Open features** and **Open tasks** show the current plans and tasks, updating as the manager marks work complete.

Use **Open live dashboard** in the command palette to keep the pane in its own tab beside a plan or conversation.

A stale warning means the displayed state has expired. The manager can check the run. Results without a live manager include a **resume feature manager** action.

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
