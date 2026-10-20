# Your notes repo

Setup keeps your work records in a separate git repo and connects them to the harness vault. You see one vault in Obsidian; the harness and your notes have separate histories.

## Know what lives there

The notes repo holds feature plans and logs, work decisions, the journal, usage records, your personal notes, and owner-specific guidance and voice settings. It also holds the repo registry in `agent.yaml` and the manager MCP configuration in `.mcp.json`.

It also holds the generated Bases used by Obsidian's feature views. The Dashboard, Index, and STATUS are built locally from these records, so the manager can refresh them whenever they fall behind.

## Choose how it syncs

Choose how this machine shares notes changes. For automatic publishing, tell the manager:

> Turn on automatic notes sync on this machine.

| Mode | What happens |
|---|---|
| off | Automatic notes sync is disabled |
| pull | Sessions fetch incoming notes without automatically publishing your changes |
| on | Sessions pull, then commit and publish notes changes when a turn ends |

The setting lives in `agent.local.yaml` and defaults to off. Give the manager the remote you want to use; it checks the notes repo's upstream configuration before enabling sync.

With sync on, a turn can commit all pending notes changes, including edits you made yourself.

## Handle a sync failure

An offline push keeps the local commit for later. A rebase conflict also keeps your local work and flags the failure to the manager.

Ask the manager to resolve the conflict. If sync reports a halt, it stays halted until the underlying git problem is fixed and the halt is cleared; the halt message names the command to clear it.

## Reuse existing notes

When setting up a replacement harness clone, give setup the existing notes repo's path. It links the records instead of creating another set. If setup reports a real file or folder where a link should go, it preserves it and reports the conflict. The manager can resolve it when you ask.

On Windows, put the notes repo on the same drive as the harness unless your account can create symlinks. See [Windows](12-windows.md) for the link behavior.

Using this repo from a second machine is covered in [pairing two machines](11-pairing-two-machines.md#use-your-notes-on-the-second-machine).

Next, see how to change repo and machine settings in [configuration](09-configuration.md).
