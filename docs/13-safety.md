# Safety

You decide the scope, access, and publishing for your work. The launch preview makes those choices concrete, and the manager brings back the result with its checks and review.

## Approve a concrete launch

Before a worker starts, you receive a preview of the repo, starting point, allowed file changes, model and budget, extra tools, and planned verification.

The manager checks permissions and client capabilities before launch. Missing access appears in the preview and needs resolving before the worker starts.

If broader access is needed, the manager proposes the exact change and its reason for your approval.

## Understand the worker boundary

Workers commit locally; the manager publishes with your approval.

The harness refuses overlapping worker file scopes in the same repo and checks returned changes against the approved scope.

Tool permissions, filesystem restrictions, and network access depend on the selected client and repo. Extra MCP servers have their own permissions, so review the tools selected for a task that needs a browser or external API. [Configuration](09-configuration.md#give-a-worker-extra-tools) explains how they are added.

## Read the completion evidence

The supervisor runs the configured checks on the worker's commit. The manager brings you their results, including any failures, exemptions, or incomplete checks.

A fresh reviewer checks the work against its acceptance criteria. The manager includes that review when it [checks the result against the plan](04-reviewing-and-shipping.md#check-the-result-against-the-plan).

## Protect work records

The manager maintains work records and refreshes their generated views. Don't rename or move feature or decision folders in Obsidian; it leaves links dangling, and the harness has no rename command. See [your notes repo](08-your-notes-repo.md).

## Treat peer access as a separate decision

Peer messages do not approve work on your behalf. Across machines, restricted peer keys reach the message receiver, while pairing itself uses your own login to install that access.

Enabling SSH is a machine-level change with its own login policy. Read the warnings in [pairing two machines](11-pairing-two-machines.md) before doing it, and review the OS service separately when removing a peer.

Next, use [troubleshooting](14-troubleshooting.md) when setup, a worker, or a vault view needs attention.
