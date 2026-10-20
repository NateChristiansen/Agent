"""The harness root is built once per session, not once per test.

`tmp_root` used to be function-scoped and spawned eight `git` subprocesses on
every test that took it — 265.74s for the suite against 140.03s once the root became a session
template each test copies. Nothing in the suite fails if that regresses; it just gets slow
again, quietly, which is how it went unnoticed in the first place. These two asserts are the
alarm.
"""

from __future__ import annotations

import inspect

import harness_root


def test_the_harness_root_template_is_session_scoped() -> None:
    # read from the source, not from the fixture object: pytest 8.4 replaced the
    # `_pytestfixturefunction` marker with a FixtureFunctionDefinition, and the decorator line
    # is the thing a future edit would actually change.
    source = inspect.getsource(harness_root)
    assert '@pytest.fixture(scope="session")\ndef _root_template' in source, (
        "_root_template must stay session-scoped: at function scope every test pays eight git "
        "subprocesses again (265.74s vs 140.03s for the suite)"
    )


def test_tmp_root_copies_the_template_instead_of_running_git() -> None:
    source = inspect.getsource(harness_root.tmp_root)
    assert "copytree" in source, "tmp_root hands out a copy of the session template"
    assert "git(" not in source, (
        "tmp_root must not spawn git: build it in _build_root, which runs once per session"
    )
