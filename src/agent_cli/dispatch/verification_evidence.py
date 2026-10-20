"""Versioned supervisor evidence and pure acceptance checks (see ADR-0018).

No process execution or persistence happens here. A runner must write supervisor-owned,
atomic snapshots independently of worker return data. Frozen values prevent accidental
in-process mutation; this format does not authenticate a file writable by an adversary.
The final gate must supply a fresh tree observation and current binding/authorization.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass
from typing import Any, Literal

from ..records.narrow import as_dict, as_dict_or_none, as_list_or_none
from ..repos import VerificationConfig, verification_config
from .ids import ID_RE  # legacy NNN-SS and tagged NNN-<tag>-SS (see ADR-0036)

SCHEMA_VERSION = 1
EvidenceState = Literal["running", "complete", "cancelled", "interrupted", "exempt"]
AssessmentStatus = Literal["verified", "exempt", "incomplete", "unverified", "failed"]
INFRASTRUCTURE = "WARNING: verification infrastructure: "


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
    if not isinstance(value, str) or re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", value) is None:
        raise ValueError("HEAD/final_sha must be a full lowercase Git object ID")


def _fields(raw: object, names: str) -> dict[str, Any]:
    fields = as_dict_or_none(raw)
    if fields is None or set(fields) != set(names.split()):
        raise ValueError(f"expected exactly these fields: {names}")
    return fields


def config_digest(config: VerificationConfig | None) -> str:
    """SHA-256 of the versioned, ordered, effective command and timeout configuration."""
    value = None
    if config is not None:
        # Re-parse even manually constructed configs; their type hints alone are no guard.
        checked = verification_config(
            {
                "verify": [asdict(command) for command in config.commands],
                "verify_timeout_seconds": config.timeout_seconds,
            }
        )
        if checked is None:  # Both required keys were supplied above.
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
    """A run ID is unique per verification execution, even within one worker attempt."""

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
        if re.fullmatch(r"[0-9a-f]{64}", _text(self.config_digest, "config_digest")) is None:
            raise ValueError("invalid config_digest")
        _text(self.run_id, "run_id")


def binding_of(raw: object) -> Binding:
    """Raises TypeError on a value that is not an object."""
    if as_dict_or_none(raw) is None:
        raise TypeError("binding must be an object")
    fields: dict[str, Any] = as_dict(raw)  # unchecked here: Binding validates each field
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
    """An immutable snapshot, not the mutable verifier process/liveness record.

    Running snapshots can contain a completed prefix of commands. A terminal snapshot is
    never edited in place: recovery binds a fresh run ID before executing again. PID/process
    group, cancellation and completion belong to a separate runner lifecycle, never to the
    model worker's `finished` flag. Persist that lifecycle before spawning (G08/G09).
    """

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
        if not isinstance(self.binding, Binding):  # pyright: ignore[reportUnnecessaryIsInstance] -- evidence is read from disk
            raise ValueError("invalid evidence binding")
        if self.state not in ("running", "complete", "cancelled", "interrupted", "exempt"):
            raise ValueError("invalid evidence state")
        for tree in (self.before, self.after):
            if tree is not None and not isinstance(tree, TreeState):  # pyright: ignore[reportUnnecessaryIsInstance] -- evidence is read from disk
                raise ValueError("invalid tree state")
        if not isinstance(self.commands, tuple) or not all(  # pyright: ignore[reportUnnecessaryIsInstance] -- evidence is read from disk
            isinstance(c, CommandOutcome)  # pyright: ignore[reportUnnecessaryIsInstance] -- evidence is read from disk
            for c in self.commands
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
        """JSON-safe detached copy; serializing cannot mutate the frozen snapshot."""
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
            CommandOutcome(**_fields(row, "index outcome exit_code elapsed_seconds")) for row in rows
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
        """Exempt is deliberately false. The gate must handle its authorization separately."""
        return self.status == "verified"

    @property
    def infrastructure(self) -> bool:
        """Only a named infrastructure cause permits done with a warning."""
        return self.status == "incomplete" and any(r.startswith(INFRASTRUCTURE) for r in self.reasons)


def _failure_cause(output: str) -> str | None:
    """Name an infrastructure fault in one failure summary, or a log without summaries."""
    lower = output.lower()
    if "no machine label was reserved" in lower:
        return "machine label reservation refused (no machine label was reserved)"
    if "filename too long" in lower or "max_path" in lower or "winerror 206" in lower:
        return "path too long (MAX_PATH)"
    missing = re.search(
        r"(?:KeyError: ['\"](HOME|USERNAME|USERPROFILE)['\"]|"
        r"(?:missing|unset)\s+(HOME|USERNAME|USERPROFILE)\b|"
        r"\b(HOME|USERNAME|USERPROFILE)\s+(?:is\s+)?(?:missing|unset|not set))",
        output,
        re.I,
    )
    if missing:
        name = next(g for g in missing.groups() if g).upper()
        return f"verifier environment missing {name}"
    if "pytest-of-unknown" in lower and re.search(
        r"(?:exit(?: status| code)?|returned non-zero exit status)\s*128\b", lower
    ):
        return "verifier environment missing HOME/USERNAME/USERPROFILE (pytest-of-unknown; git exit 128)"
    return None


def infrastructure_cause(
    evidence: Evidence,
    config: VerificationConfig,
    *,
    output: str = "",
    lifecycle_reason: str = "",
) -> str | None:
    """Recognize bounded supervisor faults without changing the stored evidence format.

    Exit 128 alone is not an environment diagnosis. Keep unrelated assertions and tool
    diagnostics failed even when infrastructure also failed in the same pytest invocation.
    When pytest supplies a short summary, every failing line must name infrastructure.
    """
    if evidence.state in {"running", "cancelled", "exempt"}:
        return None
    if re.search(
        r"AssertionError|TypeError|AttributeError|\bFailed:|^E\s+assert\b| - error: |"
        r"\b[1-9]\d* errors?, \d+ warnings?, \d+ informations?\b|"
        r"^.+:\d+:\d+: [A-Z]+\d+\b|^[A-Z]+\d+ |^error\[",
        output,
        re.M,
    ):
        return None
    failed = [c for c in evidence.commands if c.outcome == "exited" and c.exit_code != 0]
    if failed:
        summaries = re.findall(r"^(?:FAILED|ERROR) .+? - (.+)$", output, re.M)
        causes: list[str] = []
        for failure in summaries or [output]:
            cause = _failure_cause(failure)
            if cause is None:
                return None
            if cause not in causes:
                causes.append(cause)
        return "; ".join(causes)
    if "verification interrupted before command activation" in output:
        return "verifier interrupted before command activation"
    for result in evidence.commands:
        if result.index < len(config.commands):
            command = config.commands[result.index]
            if result.outcome == "timeout" or result.elapsed_seconds > command.timeout_seconds:
                return (
                    f"`{command.command}` timed out after {result.elapsed_seconds:.0f} s (command time bound)"
                )
    if evidence.elapsed_seconds > config.timeout_seconds:
        return "total verification time bound exceeded"
    if lifecycle_reason == "guardian_interrupted" and not evidence.commands:
        return "verifier interrupted before command activation"
    return None


def _ended_by(evidence: Evidence, config: VerificationConfig) -> Assessment | None:
    """Classify command failure as failed, naming command, exit and run. Cancellation, lost
    parent and unconfirmed cleanup remain incomplete.
    """
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
    """Explain which recorded binding changed, or return None. Preserve old evidence, but a pass
    cannot certify a different current verification configuration.
    """
    if recorded == expected and expected.config_digest == current_config:
        return None
    if recorded == expected:
        return (
            f"the verification config changed after this run (ran under {recorded.config_digest[:12]}, "
            f"now {current_config[:12]}): its result is kept but does not apply to the new config; reverify"
        )
    fields = ("dispatch_id", "attempt", "final_sha", "config_digest", "run_id")
    changed = [f for f in fields if getattr(recorded, f) != getattr(expected, f)]
    return "stale verification evidence: recorded " + ", ".join(
        f"{f} {str(getattr(recorded, f))[:12]} differs from {str(getattr(expected, f))[:12]}" for f in changed
    )


def assess(
    raw: Any,
    *,
    expected: Binding,
    config: VerificationConfig | None,
    current_tree: TreeState | None,
    authorized_exemption: str | None = None,
    output: str = "",
    lifecycle_reason: str = "",
) -> Assessment:
    """Validate stored evidence against *current* trusted inputs; execute nothing.

    The caller obtains expected binding, config and exemption from supervisor-owned dispatch
    state, never from the worker result or this evidence. A recorded exemption cannot grant
    itself authority. Missing/unknown legacy schemas stay unverified. A successful result
    is evidence validation, not proof of protected storage, process death, or lease ownership.
    """
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
    cause = infrastructure_cause(evidence, config, output=output, lifecycle_reason=lifecycle_reason)
    if evidence.state != "complete":
        if cause:
            return Assessment("incomplete", (INFRASTRUCTURE + cause,))
        if failed := _ended_by(evidence, config):
            return failed
        return Assessment("incomplete", (f"verification is {evidence.state}",))
    if evidence.before is None or evidence.after is None or current_tree is None:
        return Assessment("incomplete", ("pre/post/final tree observations are required",))
    if not all(tree.clean_at(expected.final_sha) for tree in (evidence.before, evidence.after, current_tree)):
        return Assessment("unverified", ("HEAD, tracked files or index differ from the verified commit",))
    if len(evidence.commands) != len(config.commands) or tuple(c.index for c in evidence.commands) != tuple(
        range(len(config.commands))
    ):
        return Assessment("incomplete", ("command outcomes are incomplete or out of order",))
    if sum(c.elapsed_seconds for c in evidence.commands) > evidence.elapsed_seconds:
        return Assessment("failed", ("total verification time is invalid",))
    if cause:
        return Assessment("incomplete", (INFRASTRUCTURE + cause,))
    if evidence.elapsed_seconds > config.timeout_seconds:
        return Assessment("failed", ("total verification time exceeds its bound with check failures",))
    failures = tuple(
        f"command {result.index} did not pass within its bound"
        for result, command in zip(evidence.commands, config.commands, strict=True)
        if result.outcome != "exited"
        or result.exit_code != 0
        or result.elapsed_seconds > command.timeout_seconds
    )
    return Assessment("failed", failures) if failures else Assessment("verified", ())
