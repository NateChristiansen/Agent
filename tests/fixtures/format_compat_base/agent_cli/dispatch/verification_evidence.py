# ruff: noqa
from __future__ import annotations
import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass
from typing import Any, Literal
from ..records.narrow import as_dict, as_dict_or_none, as_list_or_none
from ..repos import VerificationConfig, verification_config
from .ids import ID_RE

SCHEMA_VERSION = 1
EvidenceState = Literal["running", "complete", "cancelled", "interrupted", "exempt"]
AssessmentStatus = Literal["verified", "exempt", "incomplete", "unverified", "failed"]


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValueError(f"{name} must be a non-empty string without NUL")
    return value


def _integer(value: Any, name: str, minimum: int | None = None) -> int:
    if type(value) is not int or (minimum is not None and value < minimum):
        raise ValueError(f"invalid {name}")
    return value


def _seconds(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("elapsed_seconds must be a finite nonnegative number")
    try:
        result = float(value)
    except OverflowError as exc:
        raise ValueError("elapsed_seconds must be finite") from exc
    if not math.isfinite(result) or result < 0:
        raise ValueError("elapsed_seconds must be a finite nonnegative number")
    return result


def _sha(value: Any) -> None:
    if not isinstance(value, str) or re.fullmatch("(?:[0-9a-f]{40}|[0-9a-f]{64})", value) is None:
        raise ValueError("HEAD/final_sha must be a full lowercase Git object ID")


def _fields(raw: object, names: str) -> dict[str, Any]:
    fields = as_dict_or_none(raw)
    if fields is None or set(fields) != set(names.split()):
        raise ValueError(f"expected exactly these fields: {names}")
    return fields


def config_digest(config: VerificationConfig | None) -> str:
    value = None
    if config is not None:
        checked = verification_config(
            {
                "verify": [asdict(command) for command in config.commands],
                "verify_timeout_seconds": config.timeout_seconds,
            }
        )
        if checked is None:
            raise ValueError("verification configuration is missing")
        value = asdict(checked)
    encoded = json.dumps(
        {"schema_version": SCHEMA_VERSION, "config": value},
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class Binding:
    dispatch_id: str
    attempt: int
    final_sha: str
    config_digest: str
    run_id: str

    def __post_init__(self) -> None:
        if ID_RE.fullmatch(_text(self.dispatch_id, "dispatch_id")) is None:
            raise ValueError("invalid dispatch_id")
        _integer(self.attempt, "attempt", 1)
        _sha(self.final_sha)
        if re.fullmatch("[0-9a-f]{64}", _text(self.config_digest, "config_digest")) is None:
            raise ValueError("invalid config_digest")
        _text(self.run_id, "run_id")


def binding_of(raw: object) -> Binding:
    if as_dict_or_none(raw) is None:
        raise TypeError("binding must be an object")
    fields: dict[str, Any] = as_dict(raw)
    return Binding(**fields)


@dataclass(frozen=True)
class TreeState:
    head: str
    tracked_clean: bool
    index_clean: bool

    def __post_init__(self) -> None:
        _sha(self.head)
        if type(self.tracked_clean) is not bool or type(self.index_clean) is not bool:
            raise ValueError("tree cleanliness must be observed booleans")

    def clean_at(self, sha: str) -> bool:
        return self.head == sha and self.tracked_clean and self.index_clean


@dataclass(frozen=True)
class CommandOutcome:
    index: int
    outcome: Literal["exited", "timeout", "cancelled", "error"]
    exit_code: int | None
    elapsed_seconds: float

    def __post_init__(self) -> None:
        _integer(self.index, "command index", 0)
        if self.outcome not in ("exited", "timeout", "cancelled", "error"):
            raise ValueError("invalid command outcome")
        if self.outcome == "exited":
            _integer(self.exit_code, "exit_code")
        elif self.exit_code is not None:
            raise ValueError("only exited commands have an exit_code")
        _seconds(self.elapsed_seconds)


@dataclass(frozen=True)
class Evidence:
    schema_version: int
    binding: Binding
    state: EvidenceState
    before: TreeState | None
    after: TreeState | None
    commands: tuple[CommandOutcome, ...]
    elapsed_seconds: float
    exemption: str | None = None

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != SCHEMA_VERSION:
            raise ValueError("unsupported verification evidence schema_version")
        if not isinstance(self.binding, Binding):
            raise ValueError("invalid evidence binding")
        if self.state not in ("running", "complete", "cancelled", "interrupted", "exempt"):
            raise ValueError("invalid evidence state")
        for tree in (self.before, self.after):
            if tree is not None and (not isinstance(tree, TreeState)):
                raise ValueError("invalid tree state")
        if not isinstance(self.commands, tuple) or not all(
            (isinstance(c, CommandOutcome) for c in self.commands)
        ):
            raise ValueError("commands must be an immutable tuple of CommandOutcome")
        _seconds(self.elapsed_seconds)
        if self.exemption is not None:
            _text(self.exemption, "exemption")
        if (self.state == "exempt") != (self.exemption is not None):
            raise ValueError("only exempt evidence has an exemption reason")
        if self.state == "exempt" and (self.commands or self.before or self.after or self.elapsed_seconds):
            raise ValueError("exempt evidence must not pretend commands or observations ran")

    def to_dict(self) -> dict[str, Any]:
        raw = asdict(self)
        raw["commands"] = [asdict(command) for command in self.commands]
        return raw

    @classmethod
    def from_dict(cls, raw: Any) -> Evidence:
        raw = _fields(raw, "schema_version binding state before after commands elapsed_seconds exemption")
        binding = Binding(**_fields(raw["binding"], "dispatch_id attempt final_sha config_digest run_id"))

        def tree(value: Any) -> TreeState | None:
            return None if value is None else TreeState(**_fields(value, "head tracked_clean index_clean"))

        rows = as_list_or_none(raw["commands"])
        if rows is None:
            raise ValueError("commands must be a JSON list")
        commands = tuple(
            (CommandOutcome(**_fields(row, "index outcome exit_code elapsed_seconds")) for row in rows)
        )
        return cls(
            raw["schema_version"],
            binding,
            raw["state"],
            tree(raw["before"]),
            tree(raw["after"]),
            commands,
            raw["elapsed_seconds"],
            raw["exemption"],
        )


@dataclass(frozen=True)
class Assessment:
    status: AssessmentStatus
    reasons: tuple[str, ...]

    @property
    def verified(self) -> bool:
        return self.status == "verified"


def _ended_by(evidence: Evidence, config: VerificationConfig) -> Assessment | None:
    if evidence.state == "cancelled" or not evidence.commands:
        return None
    last = evidence.commands[-1]
    command = config.commands[last.index].command if last.index < len(config.commands) else "?"
    where = f"run {evidence.binding.run_id}"
    if last.outcome == "exited" and last.exit_code not in (0, None):
        return Assessment("failed", (f"`{command}` exited {last.exit_code} ({where})",))
    if last.outcome == "timeout":
        return Assessment("failed", (f"`{command}` timed out after {last.elapsed_seconds:.0f} s ({where})",))
    return None


def stale(recorded: Binding, expected: Binding, current_config: str) -> str | None:
    if recorded == expected and expected.config_digest == current_config:
        return None
    if recorded == expected:
        return f"the verification config changed after this run (ran under {recorded.config_digest[:12]}, now {current_config[:12]}): its result is kept but does not apply to the new config; reverify"
    fields = ("dispatch_id", "attempt", "final_sha", "config_digest", "run_id")
    changed = [f for f in fields if getattr(recorded, f) != getattr(expected, f)]
    return "stale verification evidence: recorded " + ", ".join(
        (
            f"{f} {str(getattr(recorded, f))[:12]} differs from {str(getattr(expected, f))[:12]}"
            for f in changed
        )
    )


def assess(
    raw: Any,
    *,
    expected: Binding,
    config: VerificationConfig | None,
    current_tree: TreeState | None,
    authorized_exemption: str | None = None,
) -> Assessment:
    if raw is None:
        return Assessment("unverified", ("verification evidence is missing",))
    try:
        evidence = Evidence.from_dict(raw.to_dict() if isinstance(raw, Evidence) else raw)
    except (TypeError, ValueError) as exc:
        return Assessment("unverified", (f"invalid verification evidence: {exc}",))
    if why := stale(evidence.binding, expected, config_digest(config)):
        return Assessment("unverified", (why,))
    if evidence.state == "exempt":
        if authorized_exemption and authorized_exemption == evidence.exemption:
            return Assessment("exempt", (f"exemption: {evidence.exemption}",))
        return Assessment("unverified", ("exemption is not currently authorized",))
    if authorized_exemption is not None:
        return Assessment("unverified", ("current exemption differs from the recorded verification mode",))
    if config is None:
        return Assessment("unverified", ("runnable verification is not configured",))
    if evidence.state != "complete":
        if failed := _ended_by(evidence, config):
            return failed
        return Assessment("incomplete", (f"verification is {evidence.state}",))
    if evidence.before is None or evidence.after is None or current_tree is None:
        return Assessment("incomplete", ("pre/post/final tree observations are required",))
    if not all(
        (tree.clean_at(expected.final_sha) for tree in (evidence.before, evidence.after, current_tree))
    ):
        return Assessment("unverified", ("HEAD, tracked files or index differ from the verified commit",))
    if len(evidence.commands) != len(config.commands) or tuple((c.index for c in evidence.commands)) != tuple(
        range(len(config.commands))
    ):
        return Assessment("incomplete", ("command outcomes are incomplete or out of order",))
    if (
        evidence.elapsed_seconds > config.timeout_seconds
        or sum((c.elapsed_seconds for c in evidence.commands)) > evidence.elapsed_seconds
    ):
        return Assessment("failed", ("total verification time is invalid or exceeds its bound",))
    failures = tuple(
        (
            f"command {result.index} did not pass within its bound"
            for result, command in zip(evidence.commands, config.commands, strict=True)
            if result.outcome != "exited"
            or result.exit_code != 0
            or result.elapsed_seconds > command.timeout_seconds
        )
    )
    return Assessment("failed", failures) if failures else Assessment("verified", ())
