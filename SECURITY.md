# Security

## Report a vulnerability

Use GitHub's private vulnerability reporting on this repository: open the Security tab and choose
"Report a vulnerability". Do not open a public issue for a vulnerability.

## Alpha limit

Workers are not isolated from your account. A worker runs with your own filesystem and network
access, inside its client's sandbox where the client provides one. Claude Code and Codex differ in
what they sandbox. Treat a worker as running as you.

Building worker isolation is tracked work. It is not done. [harness/safety.md](harness/safety.md)
covers what can go wrong when an agent dispatches other agents, and what stops it.
