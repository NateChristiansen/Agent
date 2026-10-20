from __future__ import annotations

import pytest

from agent_cli.records.dispatch import denials_of, result_of, scopes_of, thread_of
from agent_cli.records.narrow import (
    as_dict,
    as_dict_or_none,
    as_feature_id,
    as_float,
    as_float_or_none,
    as_int,
    as_list,
    as_list_or_none,
    as_str,
    dict_list,
    dig,
    str_list,
)
from agent_cli.records.tabs import attempts_of, painted_of

WRONG = [None, "x", 3, 1.5, True, [1], {"a": 1}, ("a",), object()]


@pytest.mark.parametrize("v", WRONG)
def test_scalar_helpers_total(v: object) -> None:
    assert as_str(v, "d") == (v if isinstance(v, str) else "d")
    assert as_int(v, 7) == (v if type(v) is int else 7)
    assert as_float(v, 7.5) == (float(v) if type(v) in (int, float) else 7.5)  # pyright: ignore[reportArgumentType]


@pytest.mark.parametrize("v", [x for x in WRONG if not isinstance(x, dict)])
def test_dict_helpers_wrong_type(v: object) -> None:
    assert as_dict(v) == {}
    assert as_dict_or_none(v) is None


@pytest.mark.parametrize("v", [x for x in WRONG if not isinstance(x, list)])
def test_list_helpers_wrong_type(v: object) -> None:
    assert as_list(v) == []
    assert as_list_or_none(v) is None
    assert str_list(v) == []
    assert dict_list(v) == []


def test_dict_and_list_return_same_object() -> None:
    d: dict[str, object] = {"a": 1}
    items: list[object] = ["a"]
    assert as_dict(d) is d
    assert as_dict_or_none(d) is d
    assert as_list(items) is items
    assert as_list_or_none(items) is items
    assert as_list_or_none([]) == []
    as_dict(d)["b"] = 2
    as_list(items).append("b")
    assert d == {"a": 1, "b": 2}
    assert items == ["a", "b"]


def test_dict_list_keeps_same_row_objects() -> None:
    row: dict[str, object] = {"k": 1}
    out = dict_list([row, "x", 3, None])
    assert out == [row]
    assert out[0] is row


def test_str_list_drops_non_str() -> None:
    assert str_list(["a", 1, None, "b", ["c"]]) == ["a", "b"]


def test_as_str() -> None:
    assert as_str("a") == "a"
    assert as_str(1) == ""
    assert as_str(None, "d") == "d"


def test_as_int() -> None:
    assert as_int(5) == 5
    assert as_int("5") == 0
    assert as_int(1.5, 7) == 7
    assert as_int(True) == 0
    assert as_int(False, 9) == 9


def test_as_float() -> None:
    assert as_float(2) == 2.0
    assert isinstance(as_float(2), float)
    assert as_float(1.5) == 1.5
    assert as_float("1.5") == 0.0
    assert as_float(True) == 0.0
    assert as_float(None, 3.5) == 3.5
    assert as_float(10**400, 2.5) == 2.5


def test_as_float_or_none() -> None:
    assert as_float_or_none(2) == 2.0
    assert isinstance(as_float_or_none(2), float)
    assert as_float_or_none(0) == 0.0
    assert as_float_or_none(1.5) == 1.5
    assert as_float_or_none("1.5") is None
    assert as_float_or_none(True) is None
    assert as_float_or_none(None) is None
    assert as_float_or_none(10**400) is None


def test_as_feature_id() -> None:
    assert as_feature_id(33) == 33
    assert as_feature_id("33") == 33
    assert as_feature_id("033") == 33
    assert as_feature_id("abc") is None
    assert as_feature_id("3x") is None
    assert as_feature_id("-3") is None
    assert as_feature_id("") is None
    assert as_feature_id("٣") is None
    assert as_feature_id(True) is None
    assert as_feature_id(3.7) is None
    assert as_feature_id(None) is None
    assert as_feature_id([]) is None


def test_dig() -> None:
    d = {"a": {"b": {"c": 1}}, "s": "x"}
    assert dig(d, "a", "b", "c") == 1
    assert dig(d, "a", "b") == {"c": 1}
    assert dig(d) is d
    assert dig(d, "a", "z", "c") is None
    assert dig(d, "s", "c") is None
    assert dig("x", "a") is None
    assert dig(None, "a") is None
    assert dig({"a": [{"b": 1}]}, "a", "b") is None


def test_malformed_data_documented_behaviour() -> None:
    # A string where a list belongs is empty, not a sequence of characters.
    record: dict[str, object] = {"files_touched": "abc", "result": ["x"]}
    assert as_list(record["files_touched"]) == []
    assert as_dict(record["result"]) == {}
    yaml_like: object = {1: "x"}
    assert as_dict(yaml_like) == {1: "x"}


def test_result_of() -> None:
    ret = {"status": "done", "summary": "s"}
    assert result_of({"result": ret}) is ret
    assert result_of({}) == {}
    assert result_of({"result": None}) == {}
    assert result_of({"result": ["x"]}) == {}


def test_thread_of() -> None:
    row = {"from": "worker", "text": "t"}
    out = thread_of({"thread": [row, "bad", None]})
    assert out == [row]
    assert out[0] is row
    assert thread_of({}) == []
    assert thread_of({"thread": "x"}) == []


def test_denials_of() -> None:
    rows: list[object] = [{"tool": "Bash"}, "Edit"]
    assert denials_of({"permission_denials": rows}) == rows
    mixed: list[object] = [{"tool": "Bash"}, "Edit", 3, None, ["x"]]
    assert denials_of({"permission_denials": mixed}) == mixed
    assert denials_of({}) == []
    assert denials_of({"permission_denials": 3}) == []


def test_painted_of_and_attempts_of() -> None:
    painted = {"watch-1": {"label": "L", "name": "n"}}
    attempts = {"watch-1": {"label": "L", "count": 2}}
    state: dict[str, object] = {"painted": painted, "attempts": attempts}
    assert painted_of(state) is painted
    assert attempts_of(state) is attempts
    assert painted_of({}) == {} and attempts_of({}) == {}
    assert painted_of({"painted": ["x"]}) == {} and attempts_of({"attempts": "x"}) == {}


def test_scopes_of() -> None:
    scope = {"name": "x"}
    out = scopes_of({"verification_scopes": [scope, 1]})
    assert out == [scope]
    assert out[0] is scope
    assert scopes_of({}) == []
    assert scopes_of({"verification_scopes": {"a": 1}}) == []
