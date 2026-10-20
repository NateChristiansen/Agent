"""MCP call contracts stay additive within a slice.

A live session calls with the tool schemas it cached at connect, while `mcp/fresh.py` runs each
call on the current code. So a removed tool or parameter, a parameter made required, or a schema
that no longer accepts a call it accepted fails that session's next call (an earlier `unexpected
keyword argument`). `mcp_contract.json` holds each profile's tools at the slice base, read from
the schemas the server lists (`build_server`, as `test_mcp_server.py` checks them): per tool its
parameters, each with its requiredness and its canonical schema, and the top-level response keys
where a tool declares them (none does yet: every tool returns `dict[str, Any]`).

The checker is conservative. A parameter's schema passes only when, after canonicalising it
(`canonical`: walked as a schema, each `$ref` inlined with any sibling keywords kept beside it,
and the `title`, `description` and `default` keywords dropped from schema nodes only, since none
of them changes whether a cached call validates; property names and literal values are kept),
it is identical to the base's or the change is one of these recognised
additive forms (`_additive`):
- added `enum`/`const` values, everything else equal;
- new top-level `anyOf` branches, each old branch still there unchanged;
- `integer` widened to `number`, everything else equal;
- the same, branch by branch, between two `anyOf`s of equal length (an Optional Literal gaining a
  value).
A node holding both `const` and `enum` is opaque: JSON Schema applies both, so it passes only
unchanged. So is a whole parameter whose reachable schema holds any keyword outside the
modelled allowlist (`_MODELLED`: a few schema-bearing keywords and literal ones), any reference
other than `#/$defs/<Name>` or `#/definitions/<Name>` naming a root definition that exists, or
any reference left after resolving: it is kept raw with every root definition and
passes only byte-identical. The real snapshot has no opaque parameter (a test checks).
The input schema's root is compared byte-identical too, as each tool's `root`: its `type` and
any required name with no declared parameter; a root keyword outside `_ROOT_MODELLED` makes the
whole tool opaque. Every root in the real snapshot is `{"type": "object"}` with none.
A new optional parameter, a new tool, a required parameter made optional and a newly given
default pass too. Everything else breaks, including any change inside object properties, dict
values, array items or array alternatives. Either make the change additive, or add a reviewed
entry to `allow` in `mcp_contract.json`; the failure prints the exact entry.

Refresh at each slice base, from the base checkout (the allowlist starts empty again):
`AGENT_MCP_CONTRACT_WRITE=1 uv run pytest tests/test_mcp_contract.py`.
"""

from __future__ import annotations

import datetime
import json
import os
import re
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal

import anyio
import pytest
from pydantic import BaseModel, Field, WithJsonSchema

from agent_cli.mcp import server as mcp_server
from agent_cli.mcp.tools import Ctx

SNAPSHOT = Path(__file__).resolve().parent / "mcp_contract.json"
#: Annotation keywords of a schema node: dropped there, and only there (never a property name,
#: a `required` entry or a literal value).
_NOISE = {"title", "description", "default"}
#: The modelled keywords. Schema-bearing: a map of name to schema, a list of
#: schemas, or one schema; `canonical` walks these and nothing else.
_SCHEMA_MAPS = {"properties"}
_SCHEMA_LISTS = {"anyOf", "oneOf", "allOf", "prefixItems"}
_SCHEMA_ONE = {"items", "additionalProperties", "not"}
#: Literal keywords: their value is data, kept as it is (bar `_NOISE`), never walked.
_LITERAL = {
    *_NOISE,
    "type",
    "enum",
    "const",
    "required",
    "minLength",
    "maxLength",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "multipleOf",
    "pattern",
    "format",
    "minItems",
    "maxItems",
    "uniqueItems",
    "minProperties",
    "maxProperties",
    "examples",
}
#: Every keyword a parameter's reachable schema may hold and stay modelled. `$ref` is allowed
#: in its simple form only (`_SIMPLE_REF`); `$defs`/`definitions` only at the input schema's
#: root, which `tool_signature` takes apart before any parameter is walked.
_MODELLED = _SCHEMA_MAPS | _SCHEMA_LISTS | _SCHEMA_ONE | _LITERAL | {"$ref"}


def canonical(schema: Any, defs: dict[str, Any], seen: tuple[str, ...] = ()) -> Any:
    """`schema` walked as a schema: each `$ref` inlined, annotation keywords dropped from schema
    nodes. A `$ref` with sibling keywords keeps them: `{"allOf": [<resolved>, <siblings>]}`. A
    recursive reference stays a `$ref` the second time round."""
    if not isinstance(schema, dict):
        return schema  # `true`/`false` schemas
    node: dict[str, Any] = dict(schema)
    ref = node.pop("$ref", None)
    out: dict[str, Any] = {}
    for key, value in sorted(node.items()):
        if key in _NOISE:
            continue
        if key in _SCHEMA_MAPS and isinstance(value, dict):
            out[key] = {name: canonical(child, defs, seen) for name, child in sorted(value.items())}
        elif key in _SCHEMA_LISTS and isinstance(value, list):
            out[key] = [canonical(child, defs, seen) for child in value]
        elif key in _SCHEMA_ONE:
            out[key] = (
                [canonical(child, defs, seen) for child in value]
                if isinstance(value, list)
                else canonical(value, defs, seen)
            )
        else:
            out[key] = value  # literal data
    if ref is None:
        return out
    match = _SIMPLE_REF.fullmatch(str(ref))
    if match is None:  # `param_schema` sends every other form down the opaque path
        raise ValueError(f"unsupported $ref {ref!r}")
    kind, name = match.groups()
    key = f"{kind}/{name}"
    target = defs.get(kind, {}).get(name, {})
    resolved = {"$ref": key} if key in seen else canonical(target, defs, (*seen, key))
    return {"allOf": [resolved, out]} if out else resolved


#: The one reference form resolved: a root definition by plain name (what Pydantic writes).
_SIMPLE_REF = re.compile(r"#/(\$defs|definitions)/([A-Za-z0-9_.-]+)")
#: Keys that must not survive canonicalisation anywhere, literal data included (belt and braces).
_REFERENCE_KEYS = {"$ref", "$dynamicRef", "$anchor"}


def _modelled(schema: Any, defs: dict[str, Any], seen: set[str]) -> bool:
    """Every node reachable from `schema`, through the schema-bearing keywords and simple refs,
    holds only `_MODELLED` keywords with the value shape the walk expects. Anything else (an
    `unevaluated*`, `if`/`then`/`else`, `contains`, `patternProperties`, `propertyNames`,
    `dependentSchemas`, anchors, ids, dynamic or other refs, nested definitions, a vocabulary
    extension) makes the parameter opaque."""
    if isinstance(schema, bool):
        return True
    if not isinstance(schema, dict) or not schema.keys() <= _MODELLED:
        return False
    for key, value in schema.items():
        if key in _SCHEMA_MAPS:
            if not isinstance(value, dict) or not all(_modelled(c, defs, seen) for c in value.values()):
                return False
        elif key in _SCHEMA_LISTS:
            if not isinstance(value, list) or not all(_modelled(c, defs, seen) for c in value):
                return False
        elif key in _SCHEMA_ONE and not _modelled(value, defs, seen):
            return False  # includes the old list form of `items`
    if "$ref" in schema:
        match = _SIMPLE_REF.fullmatch(str(schema["$ref"]))
        if match is None or match.group(2) not in defs.get(match.group(1), {}):
            return False
        key = "/".join(match.groups())
        if key not in seen:
            seen.add(key)
            return _modelled(defs[match.group(1)][match.group(2)], defs, seen)
    return True


def _keys_anywhere(value: Any) -> set[str]:
    if isinstance(value, dict):
        found = set(value.keys())
        for child in value.values():
            found |= _keys_anywhere(child)
        return found
    if isinstance(value, list):
        return set().union(*(_keys_anywhere(child) for child in value)) if value else set()
    return set()


def param_schema(schema: Any, defs: dict[str, Any]) -> Any:
    """The parameter's contract form: `canonical` when every reachable keyword is modelled and
    no reference is left after resolving (a recursive one is), or else the raw schema with every
    root definition, `{"opaque": ...}`, which passes only unchanged."""
    if _modelled(schema, defs, set()):
        form = canonical(schema, defs)
        if not _REFERENCE_KEYS & _keys_anywhere(form):
            return form
    return {"opaque": {"schema": schema, "defs": defs}}


def _values(schema: dict[str, Any]) -> tuple[dict[str, Any], list[Any] | None]:
    """`schema` without its value list, and that list (`const` read as a one-value enum)."""
    rest = {k: v for k, v in schema.items() if k not in ("enum", "const")}
    if "const" in schema:
        return rest, [schema["const"]]
    return rest, (list(schema["enum"]) if "enum" in schema else None)


def _json(value: Any) -> str:
    """`value` as canonical JSON text. Every comparison here goes through it, never Python `==`,
    which takes `1 == True` and `0 == False`: JSON tells them apart."""
    return json.dumps(value, sort_keys=True)


def _same(a: Any, b: Any) -> bool:
    return _json(a) == _json(b)


def _simple_additive(old: Any, new: Any) -> bool:
    """Equal, an added enum/const value, or `integer` widened to `number`; no recursion."""
    if _same(old, new):
        return True
    if not isinstance(old, dict) or not isinstance(new, dict):
        return False
    if any("const" in s and "enum" in s for s in (old, new)):
        return False  # both apply, so the accepted set is their intersection: opaque
    old_rest, old_values = _values(old)
    new_rest, new_values = _values(new)
    if old_values is not None and new_values is not None and _same(old_rest, new_rest):
        return {_json(v) for v in old_values} <= {_json(v) for v in new_values}
    if (
        old_values is None
        and new_values is None
        and _same(old.get("type"), "integer")
        and _same(new.get("type"), "number")
    ):
        return _same(
            {k: v for k, v in old.items() if k != "type"}, {k: v for k, v in new.items() if k != "type"}
        )
    return False


def _additive(old: Any, new: Any) -> bool:
    """`new` accepts every call `old` did, by one of the recognised forms (module docstring)."""
    if any(isinstance(s, dict) and "opaque" in s for s in (old, new)):
        return _same(old, new)  # unresolved references: unchanged or a break
    if _simple_additive(old, new):
        return True
    if not isinstance(old, dict) or not isinstance(new, dict) or "anyOf" not in new:
        return False
    old_rest = {k: v for k, v in old.items() if k != "anyOf"}
    new_rest = {k: v for k, v in new.items() if k != "anyOf"}
    new_branches: list[Any] = list(new["anyOf"])
    branch_texts = {_json(b) for b in new_branches}
    if "anyOf" in old:
        old_branches: list[Any] = list(old["anyOf"])
        if not _same(old_rest, new_rest):
            return False
        if all(_json(b) in branch_texts for b in old_branches):
            return True  # new branches added, the old ones unchanged
        return len(old_branches) == len(new_branches) and all(
            _simple_additive(o, n) for o, n in zip(old_branches, new_branches, strict=True)
        )
    return not new_rest and _json(old) in branch_texts  # one schema became a branch of a union


#: Keywords the input schema's root may hold and stay modelled. Any
#: other root keyword (`additionalProperties`, a root `allOf`/`anyOf`/`oneOf`/`not`, `if`,
#: `minProperties`, `dependentRequired`, ...) constrains the whole request, so the tool is opaque.
_ROOT_MODELLED = {"type", "properties", "required", "$defs", "definitions", "title", "description"}


def tool_signature(input_schema: dict[str, Any], output_schema: dict[str, Any] | None) -> dict[str, Any]:
    """Per parameter, its requiredness and `param_schema`; per tool, `root`: the input schema's
    own constraints, compared byte-identical. That is its `type` and any required name that is
    not a declared parameter, or, when the root holds an unmodelled keyword, the whole raw input
    schema as `{"opaque": ...}`."""
    defs = {kind: input_schema[kind] for kind in ("$defs", "definitions") if kind in input_schema}
    required = set(input_schema.get("required", []))
    properties: dict[str, Any] = input_schema.get("properties", {})
    params = {
        name: {"required": name in required, "schema": param_schema(schema, defs)}
        for name, schema in properties.items()
    }
    if input_schema.keys() <= _ROOT_MODELLED:
        root: dict[str, Any] = {
            "type": input_schema.get("type"),
            "undeclared_required": sorted(required - properties.keys()),
        }
    else:
        root = {"opaque": input_schema}
    response = sorted((output_schema or {}).get("properties", {}))
    return {"params": params, "root": root, **({"response": response} if response else {})}


def signatures(root: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, dict[str, Any]]:
    """Every profile's tools as the server lists them, by `tool_signature`."""
    worker = Ctx(root=root, profile="worker", feature="1", dispatch_id="d-1")
    monkeypatch.setattr(mcp_server, "worker_ctx", lambda _root: worker)

    async def listed(profile: str) -> dict[str, Any]:
        server = mcp_server.build_server(root, profile)
        return {t.name: tool_signature(t.input_schema, t.output_schema) for t in await server.list_tools()}

    return {profile: anyio.run(listed, profile) for profile in sorted(mcp_server.PROFILES)}


def breaks(
    base: dict[str, Any], head: dict[str, Any], allow: list[dict[str, Any]] | None = None
) -> list[str]:
    """Each way `head` could fail a client that cached `base`, unless `allow` lists it (by
    profile, tool and param); empty when the change is additive. Each line ends with the
    allowlist entry that would accept it."""
    allowed = {(a.get("profile"), a.get("tool"), a.get("param")) for a in allow or []}
    out: list[str] = []

    def found(profile: str, tool: str, param: str | None, why: str) -> None:
        if (profile, tool, param) in allowed:
            return
        entry = {
            "profile": profile,
            "tool": tool,
            "param": param,
            "reason": "<why a cached call still works>",
            "date": datetime.date.today().isoformat(),
        }
        where = f"{profile}/{tool}" + (f".{param}" if param else "")
        out.append(f"{where}: {why}; if reviewed, add to `allow`: {json.dumps(entry)}")

    for profile, tools in base.items():
        for tool, old in tools.items():
            new = head.get(profile, {}).get(tool)
            if new is None:
                found(profile, tool, None, "tool removed")
                continue
            if not _same(old.get("root"), new.get("root")):
                found(profile, tool, None, "input schema root changed (compared byte-identical)")
            for name, was in old["params"].items():
                now = new["params"].get(name)
                if now is None:
                    found(profile, tool, name, "parameter removed (keep the old name as an alias)")
                    continue
                if now["required"] and not was["required"]:
                    found(profile, tool, name, "became required")
                if not _additive(was["schema"], now["schema"]):
                    found(profile, tool, name, "schema changed in a way not known to be additive")
            for name, now in new["params"].items():
                if name not in old["params"] and now["required"]:
                    found(profile, tool, name, "new parameter is required")
            if lost := set(old.get("response", [])) - set(new.get("response", [])):
                found(profile, tool, None, f"response keys removed {sorted(lost)}")
    return out


def test_the_slice_heads_mcp_contracts_are_additive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    head = signatures(tmp_path, monkeypatch)
    if os.environ.get("AGENT_MCP_CONTRACT_WRITE") == "1":
        text = json.dumps({"allow": [], "tools": head}, indent=1, sort_keys=True) + "\n"
        SNAPSHOT.write_text(text, encoding="utf-8")
        pytest.skip(f"wrote {SNAPSHOT.name}; commit it at the slice base")
    snapshot = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    found = breaks(snapshot["tools"], head, snapshot["allow"])
    assert not found, "\n".join(found)


# ---------------------------------------------------------------- the checker, on real schemas


def _signature(fn: Callable[..., dict[str, Any]]) -> dict[str, Any]:
    """`fn` registered as a tool the way `build_server` registers one, as a one-tool contract."""
    from mcp.server import MCPServer

    server = MCPServer("probe")
    server.add_tool(fn, name="dispatch", structured_output=True)

    async def listed() -> dict[str, Any]:
        [tool] = await server.list_tools()
        return tool_signature(tool.input_schema, tool.output_schema)

    return {"manager": {"dispatch": anyio.run(listed)}}


def _broken(old: Callable[..., dict[str, Any]], new: Callable[..., dict[str, Any]]) -> list[str]:
    return breaks(_signature(old), _signature(new))


def base_tool(action: Literal["list", "show"], dispatch_id: str | None = None) -> dict[str, Any]:
    return {}


def _drop_show(action: Literal["list"], dispatch_id: str | None = None) -> dict[str, Any]:
    return {}


def _drop_param(action: Literal["list", "show"]) -> dict[str, Any]:
    return {}


def _required(action: Literal["list", "show"], dispatch_id: str) -> dict[str, Any]:
    return {}


def _not_null(action: Literal["list", "show"], dispatch_id: str = "") -> dict[str, Any]:
    return {}


def _new_required(
    action: Literal["list", "show"], mode: str, dispatch_id: str | None = None
) -> dict[str, Any]:
    return {}


def _additive_tool(
    action: Literal["list", "show", "consume"] = "list",
    dispatch_id: str | int | None = None,
    generation: int | None = None,
) -> dict[str, Any]:
    return {}


@pytest.mark.parametrize(
    ("changed", "broken"),
    [
        (_drop_show, "dispatch.action: schema changed"),  # A two-value Literal to one
        (_drop_param, "dispatch.dispatch_id: parameter removed"),
        (_required, "dispatch.dispatch_id: became required"),
        (_not_null, "dispatch.dispatch_id: schema changed"),
        (_new_required, "dispatch.mode: new parameter is required"),
    ],
)
def test_a_breaking_change_is_named(changed: Callable[..., dict[str, Any]], broken: str):
    found = _broken(base_tool, changed)
    assert any(broken in line for line in found), found


def test_additive_changes_pass():
    """A new action (`consume`) with a default, a new union branch, a new optional parameter, a
    relaxed requirement and a new tool: what slice 1's other builders add."""
    head = _signature(_additive_tool)
    head["manager"]["dispatch_read"] = {"params": {}}
    assert breaks(_signature(base_tool), head) == []


# Three changes a semantic comparison missed, each now a break.


def _dict_str(hashes: dict[str, str] | None = None) -> dict[str, Any]:
    return {}


def _dict_list(hashes: dict[str, list[str]] | None = None) -> dict[str, Any]:
    return {}


def _mixed(tags: list[str | int]) -> dict[str, Any]:
    return {}


def _homogeneous(tags: list[str] | list[int]) -> dict[str, Any]:
    return {}


class _OldItem(BaseModel):
    kind: Literal["a", "b"]


class _NewItem(BaseModel):
    kind: Literal["a"]


def _nested_old(item: _OldItem) -> dict[str, Any]:
    return {}


def _nested_new(item: _NewItem) -> dict[str, Any]:
    return {}


@pytest.mark.parametrize(
    ("old", "new"),
    [(_dict_str, _dict_list), (_mixed, _homogeneous), (_nested_old, _nested_new)],
    ids=["dict value type", "mixed list to list alternatives", "nested object enum"],
)
def test_changes_inside_objects_and_arrays_break(
    old: Callable[..., dict[str, Any]], new: Callable[..., dict[str, Any]]
):
    found = _broken(old, new)
    assert found and "schema changed" in found[0], found


def _int(limit: int) -> dict[str, Any]:
    return {}


def _float(limit: float) -> dict[str, Any]:
    return {}


def test_integer_widened_to_number_passes():
    """A cached integer still validates as a number. Narrowing back breaks."""
    assert _broken(_int, _float) == []
    assert _broken(_float, _int) != []


def test_a_reviewed_allowlist_entry_accepts_a_break_and_the_failure_prints_it():
    [line] = _broken(_dict_str, _dict_list)
    entry = json.loads(line.split("add to `allow`: ", 1)[1])
    assert (entry["profile"], entry["tool"], entry["param"]) == ("manager", "dispatch", "hashes")
    assert set(entry) == {"profile", "tool", "param", "reason", "date"}
    assert breaks(_signature(_dict_str), _signature(_dict_list), [entry]) == []


def test_a_removed_tool_is_named():
    [line] = breaks(_signature(base_tool), {"manager": {}})
    assert line.startswith("manager/dispatch: tool removed")


# Annotations dropped from property maps and literal values, and `$ref`
# siblings discarded. Each change below rejects a call the old schema accepted.


class _StrFields(BaseModel):
    name: str
    title: str
    description: str
    default: str


class _IntName(BaseModel):
    name: int
    title: str
    description: str
    default: str


class _IntTitle(BaseModel):
    name: str
    title: int
    description: str
    default: str


class _IntDescription(BaseModel):
    name: str
    title: str
    description: int
    default: str


class _IntDefault(BaseModel):
    name: str
    title: str
    description: str
    default: int


def _fields(item: _StrFields) -> dict[str, Any]:
    return {}


def _int_name(item: _IntName) -> dict[str, Any]:
    return {}


def _int_title(item: _IntTitle) -> dict[str, Any]:
    return {}


def _int_description(item: _IntDescription) -> dict[str, Any]:
    return {}


def _int_default(item: _IntDefault) -> dict[str, Any]:
    return {}


@pytest.mark.parametrize(
    "new",
    [_int_name, _int_title, _int_description, _int_default],
    ids=["name", "title", "description", "default"],
)
def test_a_nested_field_changing_type_breaks_whatever_it_is_called(new: Callable[..., dict[str, Any]]):
    found = _broken(_fields, new)
    assert found and "item: schema changed" in found[0], found


def test_annotations_are_dropped_only_as_schema_keywords():
    schema = {
        "type": "object",
        "title": "Item",
        "description": "dropped",
        "properties": {"title": {"type": "string", "title": "Title", "default": "x"}},
        "required": ["title"],
    }
    assert canonical(schema, {}) == {
        "properties": {"title": {"type": "string"}},
        "required": ["title"],
        "type": "object",
    }


def test_a_title_inside_an_object_valued_enum_is_literal_data():
    def tool(enum_title: str) -> dict[str, Any]:
        param = {"enum": [{"title": enum_title, "n": 1}]}
        return tool_signature({"properties": {"choice": param}, "required": ["choice"]}, None)

    base = {"manager": {"dispatch": tool("a")}}
    found = breaks(base, {"manager": {"dispatch": tool("b")}})
    assert found and "choice: schema changed" in found[0], found


# Python `==` takes 1 for true and 0 for false; JSON Schema does not.


def _one_or_auto(mode: Literal[1, "auto"]) -> dict[str, Any]:
    return {}


def _true_or_auto(mode: Literal[True, "auto"]) -> dict[str, Any]:
    return {}


def test_a_literal_number_becoming_a_boolean_breaks():
    found = _broken(_one_or_auto, _true_or_auto)
    assert found and "mode: schema changed" in found[0], found


def _raw(param: dict[str, Any]) -> dict[str, Any]:
    """A one-parameter contract from a hand-written schema."""
    return {"manager": {"dispatch": tool_signature({"properties": {"x": param}, "required": ["x"]}, None)}}


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ({"const": 1}, {"anyOf": [{"const": True}, {"type": "string"}]}),
        ({"enum": [0, "x"]}, {"enum": [False, "x"]}),
        ({"enum": [1]}, {"enum": [True, 2]}),
        ({"const": 1}, {"const": True}),
        ({"enum": [{"n": 1}]}, {"enum": [{"n": True}]}),
        ({"const": [1, 0]}, {"const": [True, False]}),
        ({"anyOf": [{"const": 1}, {"type": "null"}]}, {"anyOf": [{"const": True}, {"type": "null"}]}),
    ],
    ids=[
        "anyOf added string branch",
        "enum",
        "enum superset",
        "const",
        "object enum",
        "array const",
        "branch",
    ],
)
def test_a_number_and_a_boolean_are_never_the_same_value(old: dict[str, Any], new: dict[str, Any]):
    found = breaks(_raw(old), _raw(new))
    assert found and "x: schema changed" in found[0], found


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ({"const": 1}, {"const": 1, "enum": [2]}),  # accepted 1, now accepts nothing
        ({"const": 1, "enum": [1, 2]}, {"const": 1, "enum": [1, 2, 3]}),  # opaque even when wider
        ({"enum": [1, 2]}, {"const": 2, "enum": [1, 2]}),
    ],
    ids=["const gains enum", "both, enum widened", "enum gains const"],
)
def test_a_node_with_const_and_enum_is_opaque(old: dict[str, Any], new: dict[str, Any]):
    found = breaks(_raw(old), _raw(new))
    assert found and "x: schema changed" in found[0], found
    assert breaks(_raw(new), _raw(new)) == []


def _one(mode: Literal[1]) -> dict[str, Any]:
    return {}


def _one_and_enum_two(mode: Annotated[Literal[1], Field(json_schema_extra={"enum": [2]})]) -> dict[str, Any]:
    return {}


def test_a_literal_given_an_extra_enum_breaks():
    """The SDK spelling of the same probe: a one-value Literal is `const`, the extra is `enum`."""
    schema = _signature(_one_and_enum_two)["manager"]["dispatch"]["params"]["mode"]["schema"]
    assert schema["const"] == 1 and schema["enum"] == [2], schema
    found = _broken(_one, _one_and_enum_two)
    assert found and "mode: schema changed" in found[0], found


def test_json_aware_comparison_still_passes_what_is_additive():
    assert breaks(_raw({"enum": [1]}), _raw({"enum": [1, True]})) == []
    assert breaks(_raw({"const": 1}), _raw({"anyOf": [{"const": 1}, {"type": "string"}]})) == []
    assert breaks(_raw({"type": "integer"}), _raw({"type": "number"})) == []


class _Word(StrEnum):
    a = "a"
    bb = "bb"


def _word(word: _Word) -> dict[str, Any]:
    return {}


def _long_word(word: Annotated[_Word, Field(min_length=2)]) -> dict[str, Any]:
    return {}


def test_a_ref_keeps_its_sibling_constraints():
    sig = _signature(_long_word)["manager"]["dispatch"]["params"]["word"]["schema"]
    assert sig == {"allOf": [{"enum": ["a", "bb"], "type": "string"}, {"minLength": 2}]}, sig
    found = _broken(_word, _long_word)
    assert found and "word: schema changed" in found[0], found


# References resolved by their last path segment hid changes. Only a
# plain root-definition ref is resolved; any other form makes the parameter opaque.


def _with_defs(param: dict[str, Any], defs: dict[str, Any]) -> dict[str, Any]:
    schema = {"properties": {"x": param}, "required": ["x"], "$defs": defs}
    return {"manager": {"dispatch": tool_signature(schema, None)}}


def _typed(kind: str) -> dict[str, Any]:
    return {"type": kind}


@pytest.mark.parametrize(
    ("ref", "defs"),
    [
        ("#/$defs/Outer/properties/v", lambda t: {"Outer": {"properties": {"v": _typed(t)}}}),
        ("#/$defs/a~1b", lambda t: {"a/b": _typed(t)}),
        ("#word", lambda t: {"W": {"$anchor": "word", **_typed(t)}}),
        ("#/$defs/Missing", lambda t: {"Other": _typed(t)}),
    ],
    ids=["nested pointer", "escaped name", "anchor", "absent name"],
)
def test_an_unresolved_reference_makes_the_parameter_opaque(ref: str, defs: Callable[[str], Any]):
    old, new = _with_defs({"$ref": ref}, defs("string")), _with_defs({"$ref": ref}, defs("integer"))
    assert "opaque" in old["manager"]["dispatch"]["params"]["x"]["schema"]
    found = breaks(old, new)
    assert found and "x: schema changed" in found[0], found
    assert breaks(old, old) == []


def _dynamic(min_length: int) -> dict[str, Any]:
    return {
        "$dynamicRef": "#w",
        "$defs": {"W": {"$dynamicAnchor": "w", "type": "string", "minLength": min_length}},
    }


def _short_word(word: Annotated[str, WithJsonSchema(_dynamic(1))]) -> dict[str, Any]:
    return {}


def _longer_word(word: Annotated[str, WithJsonSchema(_dynamic(2))]) -> dict[str, Any]:
    return {}


def test_a_dynamic_ref_change_through_the_sdk_breaks():
    """Codex's SDK probe: `$dynamicRef` to a nested anchored definition, minLength 1 -> 2."""
    sig = _signature(_short_word)["manager"]["dispatch"]["params"]["word"]["schema"]
    assert sig["opaque"]["schema"]["$dynamicRef"] == "#w", sig
    found = _broken(_short_word, _longer_word)
    assert found and "word: schema changed" in found[0], found


def test_the_real_contract_resolves_every_reference():
    """Opaque parameters still pass when unchanged, but say little; the snapshot has none."""
    assert '"opaque"' not in SNAPSHOT.read_text(encoding="utf-8")


# A `$ref` under `unevaluatedProperties`/`unevaluatedItems` was read as
# literal data. The rule is inverted: any keyword outside `_MODELLED` makes the parameter opaque.


@pytest.mark.parametrize("keyword", ["unevaluatedProperties", "unevaluatedItems"])
def test_a_ref_under_an_unevaluated_keyword_breaks(keyword: str):
    old = _with_defs({keyword: {"$ref": "#/$defs/V"}}, {"V": {"type": "string"}})
    new = _with_defs({keyword: {"$ref": "#/$defs/V"}}, {"V": {"type": "integer"}})
    assert "opaque" in old["manager"]["dispatch"]["params"]["x"]["schema"]
    found = breaks(old, new)
    assert found and "x: schema changed" in found[0], found


#: One schema per keyword family outside the allowlist; each, anywhere reachable, is opaque.
UNMODELLED = {
    "unevaluatedProperties": {"type": "object", "unevaluatedProperties": False},
    "unevaluatedItems": {"type": "array", "unevaluatedItems": False},
    "if/then/else": {"if": {"type": "string"}, "then": {"minLength": 1}, "else": {"type": "integer"}},
    "contains": {"type": "array", "contains": {"type": "string"}},
    "patternProperties": {"type": "object", "patternProperties": {"^a": {"type": "string"}}},
    "propertyNames": {"type": "object", "propertyNames": {"maxLength": 3}},
    "dependentSchemas": {"type": "object", "dependentSchemas": {"a": {"required": ["b"]}}},
    "dependentRequired": {"type": "object", "dependentRequired": {"a": ["b"]}},
    "anchor": {"$anchor": "a", "type": "string"},
    "id": {"$id": "https://example.com/x", "type": "string"},
    "dynamic ref": {"$dynamicRef": "#a"},
    "nested defs": {"$defs": {"A": {"type": "string"}}, "type": "string"},
    "vocabulary extension": {"type": "string", "x-maxBytes": 10},
    "items as a list": {"type": "array", "items": [{"type": "string"}]},
    "nested in properties": {"type": "object", "properties": {"a": {"contains": {}}}},
    "inside a referenced definition": {"$ref": "#/$defs/Odd"},
    "recursive reference": {"$ref": "#/$defs/Tree"},
}
_DEFS = {
    "Odd": {"type": "object", "patternProperties": {"^a": {}}},
    "Tree": {"type": "object", "properties": {"child": {"$ref": "#/$defs/Tree"}}},
}


@pytest.mark.parametrize("family", sorted(UNMODELLED))
def test_an_unmodelled_keyword_makes_the_parameter_opaque(family: str):
    schema = _with_defs(UNMODELLED[family], _DEFS)["manager"]["dispatch"]["params"]["x"]["schema"]
    assert "opaque" in schema, schema


def test_modelled_keywords_stay_transparent():
    schema = {
        "type": "object",
        "properties": {"a": {"type": "string", "minLength": 1, "pattern": "^x"}},
        "required": ["a"],
        "additionalProperties": False,
        "allOf": [{"$ref": "#/$defs/Odd2"}],
    }
    form = _with_defs(schema, {"Odd2": {"maxProperties": 3}})["manager"]["dispatch"]["params"]["x"]
    assert "opaque" not in form["schema"], form


# Constraints at the input schema's root were thrown away.

_ROOT = {"type": "object", "properties": {"action": {"type": "string"}}, "required": ["action"]}


def _root(**extra: Any) -> dict[str, Any]:
    return {"manager": {"dispatch": tool_signature({**_ROOT, **extra}, None)}}


@pytest.mark.parametrize(
    "extra",
    [
        {"allOf": [{"properties": {"action": {"const": "list"}}}]},
        {"if": {"properties": {"action": {"const": "show"}}}, "then": {"required": ["id"]}},
        {"maxProperties": 0},
        {"required": ["action", "undeclared"]},
    ],
    ids=["root allOf", "root if/then", "maxProperties", "required undeclared property"],
)
def test_a_root_constraint_added_to_a_tool_breaks(extra: dict[str, Any]):
    """Codex's four valid-schema probes: each rejects a cached request the old root accepted."""
    found = breaks(_root(), _root(**extra))
    assert found and "dispatch: input schema root changed" in found[0], found


@pytest.mark.parametrize(
    "keyword",
    ["additionalProperties", "allOf", "anyOf", "oneOf", "not", "if", "minProperties", "dependentRequired"],
)
def test_a_root_keyword_outside_the_allowlist_makes_the_tool_opaque(keyword: str):
    value: Any = {"additionalProperties": False, "minProperties": 1, "dependentRequired": {"a": ["b"]}}.get(
        keyword, [{}] if keyword.endswith("Of") else {}
    )
    root = _root(**{keyword: value})["manager"]["dispatch"]["root"]
    assert "opaque" in root, root
    assert breaks(_root(**{keyword: value}), _root(**{keyword: value})) == []  # unchanged passes


def test_a_root_allof_on_an_sdk_listed_tool_breaks(tmp_path: Path):
    """The SDK-listed form of Codex's probe, on the real `harness` tool: a root `allOf` that
    pins `action` rejects every other cached action, so it must not pass."""

    async def listed() -> dict[str, Any]:
        tools = await mcp_server.build_server(tmp_path, "manager").list_tools()
        return next(t.input_schema for t in tools if t.name == "harness")

    schema = anyio.run(listed)
    base = {"manager": {"harness": tool_signature(schema, None)}}
    pinned = {**schema, "allOf": [{"properties": {"action": {"const": "doctor"}}}]}
    found = breaks(base, {"manager": {"harness": tool_signature(pinned, None)}})
    assert found and "harness: input schema root changed" in found[0], found
