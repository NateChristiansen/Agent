# Windows

Follow [getting started](01-getting-started.md) for installation and opening the vault. This chapter covers what differs on Windows.

## Keep notes linked

Put the notes repo on the same drive as the harness unless your account can create symlinks. Setup tries symlinks first; without that capability, it uses junctions for directories and hard links for files. Hard links cannot cross drives, and setup reports that case rather than making a copy.

A git update can replace a hard-linked file on the notes side. The harness reconnects a stale copy when it can confirm that copy was already committed. If it finds an uncommitted edit in the harness copy, it preserves both files and reports the conflict for resolution.

## Use Windows terminals

Setup configures PowerShell terminal profiles for the ribbon managers and Windows variants of palette actions that invoke the harness. For commands you enter yourself, [troubleshooting](14-troubleshooting.md#check-the-harnesss-health) shows the Windows launcher.

Worker verification runs through Git Bash. If it is missing, the launch preview flags it so you can install it before starting checked work.

For SSH server setup and its elevation requirements, follow [pairing two machines](11-pairing-two-machines.md#prepare-the-ssh-servers).

## Check your first run

Setup and configuration checks leave these parts of your Windows workflow to be confirmed in a live run:

- Worker launch through your chosen client and repo.
- Permission requests during the run.
- The committed result and supervisor checks.
- Cleanup after the worker finishes.
- For pairing, both routes and a message to each client you use.

Try a small feature before relying on unattended work, and tell the manager what you observe.

Next, see how approvals and worker access fit together in [safety](13-safety.md).
