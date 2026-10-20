---
tags: [doc]
---

# Testing changes in order

When a change moves work earlier or later, assert what is true **before** the later step runs.
An end-state assertion only proves that both steps happened. Use a call log, a paused worker,
or a state read between steps so reversing their order makes the test fail.

One regression happened when `dispatch new` ran setup in the foreground. Existing tests checked only
that setup eventually ran, so they passed while the command blocked for minutes. The regression
test in `tests/test_dispatch_launch.py::test_setup_runs_in_the_supervisor_so_new_returns_at_once`
calls `new` with the supervisor detached, then asserts `setup_pending` is true and the setup
marker does not exist. Under the broken foreground implementation, the marker exists **before**
the supervisor runs and the second assertion fails. That is the before-state to preserve.

# Tests never touch live state

The suite fences itself (`tests/isolation.py`, autouse). `GIT_CEILING_DIRECTORIES` stops git's
upward search at the pytest temp root, so a fixture harness that is not a git repository can
never resolve an enclosing checkout as its main checkout. `AGENT_TEST_SINK_FENCE` makes the
telemetry recorder refuse any sink outside the temp root; the refusal is noted and fails the test
that caused it. Both variables reach child processes. `CODEX_HOME` points at an empty directory,
so the owner's Codex config never decides a test.

Tests marked `live_config` read the checkout's own `.agent/` state or private notes. In an
isolated checkout (a verifier run, CI) that state is absent, and they read nothing or skip.

Never point a real signal path at a made-up pid: a kill on an invented process group hits
whatever group has that number on the machine running the suite. Patch the kill function
instead. There is no suite-wide guard on `os.kill`, because telling a planted orphan from an
unrelated process needs the inventory the tests themselves exercise.
