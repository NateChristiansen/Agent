# Pairing two machines

Two machines can use the same notes repo and send messages between their manager sessions. Notes sync carries plans and results; pairing adds the message link. Set up the notes first, then pair if you want managers to coordinate across machines.

If you only use one machine, skip this chapter.

## Use your notes on the second machine

Publish the notes repo to a remote you can reach from both machines. On the second machine, clone the harness and that notes repo, then run setup from the harness folder with the notes clone's path:

```sh
./setup.sh --notes ../Agent-notes
```

On Windows from cmd or PowerShell, use:

```bat
setup.cmd --notes ..\Agent-notes
```

Setup connects the existing records to this vault. Open the harness folder in Obsidian and complete the remaining setup steps on this machine too.

Give the manager this machine's repo paths and a distinct label, such as `mac` or `windows`, to distinguish its worker records. Labels use one to eight lowercase letters and digits, starting with a letter. Choose one machine to write the journal; the manager records that choice in the shared `journal.machine` setting.

Configure notes sync on each machine as described in [your notes repo](08-your-notes-repo.md). Workers stay on the machine that launched them; its manager handles their questions and results. Their handoffs sync with the notes.

## Prepare the SSH servers

Pairing uses your own accounts on the two machines, with SSH reachable in both directions. Run the enrollment commands yourself in an Obsidian terminal tab or an OS terminal; the manager can help interpret the reports.

**Warning:** enabling the SSH server exposes the machine's login service under its existing SSH policy. That access is broader than the restricted message keys pairing will add. Review the report and your network access before proceeding.

On macOS, run from the harness folder as your normal user:

```sh
bin/agent peer-net setup-server
```

It enables Remote Login with sudo if needed, checks host keys, and checks the SSH shell. Enter your password at sudo's prompt. If macOS requires Full Disk Access, follow the report or enable Remote Login in **System Settings → General → Sharing** and rerun. It does not change a firewall rule on macOS.

On Windows, use an elevated PowerShell in the harness folder:

```powershell
bin\agent.cmd peer-net setup-server
```

It installs OpenSSH Server if missing, sets sshd to start automatically, starts it, and checks host keys. If the named OpenSSH firewall rule is absent, it creates an inbound TCP 22 rule for Private networks. It also creates a missing administrators authorized-keys file with access limited to SYSTEM and Administrators, and checks the network, account, and SSH shell.

**Warning:** an existing firewall rule is left as it stands and may allow wider access. To narrow its profile to Private and enable it, explicitly run:

```powershell
bin\agent.cmd peer-net setup-server --firewall private-only
```

Review the report's warnings about the network category, existing keys-file permissions, and SSH login policy; the command leaves those settings unchanged. Pairing needs cmd.exe as the Windows SSH shell; on macOS it accepts sh, bash, or zsh. For Linux, configure the SSH server with your system's tools.

## Initialize each machine

This guide's examples pair a Mac with a Windows machine. On the Mac:

```sh
bin/agent peer-net init --device mac
```

On the Windows machine:

```powershell
bin\agent.cmd peer-net init --device windows
```

Each command creates that device's peer key and prints its public key and host fingerprints. Keep the host fingerprint output available on the machine it came from for the next step.

## Pair with your own login

From the Mac in this example, substitute the other machine's account, host, and absolute harness path:

```sh
bin/agent peer-net pair windows youruser@your-windows-host --repo 'C:\code\Agent' --enable
```

**Warning:** pair logs into the other machine once with your own account password. Enter it at SSH's prompt, never in the manager conversation. The harness does not read, store, or log the password. An existing personal login key can be selected instead with the login-key option; a peer key cannot be used for this login.

Compare the displayed host fingerprint with the one printed on the **other** machine during initialization, then confirm only if they match. Pairing runs the peer acceptance command there, exchanges public keys and host keys, installs restricted message access in both directions, and tests both routes. Private keys stay on their own machines.

The enable option turns receiving on at both ends. Check that both route results are verified before using the link. If either is unverified, resolve the reported problem and rerun pairing.

## Use or remove the link

Open a manager on each machine and ask one to list the remote sessions, then send a specific request. The peer keys are restricted to receiver status, session listings, and queued messages with wake attempts. Follow [the messaging guidance](06-running-several-sessions.md#have-managers-coordinate) when you need an answer before proceeding.

To inspect enrollment on either machine, use its launcher:

```sh
bin/agent peer-net show
```

To stop receiving while keeping enrollment and queued records:

```sh
bin/agent peer-net off
```

To remove the other device's access and route here:

```sh
bin/agent peer-net revoke windows
```

Revoke on both machines to remove the pair in both directions, using the opposite alias on the second. Turning receiving off or revoking a peer does not disable the OS SSH server. Review that service separately if you no longer need it.

Next, see the setup details and first-run checks for [Windows](12-windows.md).
