from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, cast

from codeagent.agent.models import (
    DecisionState,
    FailedAttempt,
    FileChangeKind,
    FileState,
    TestOutcome,
    TestState,
)
from codeagent.evidence.models import EvidenceRef, EvidenceType
from codeagent.llm.message import ContextCategory, Message


class CompactionPayloadError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class TaskDelta:
    goal: str | None = None
    constraints: tuple[str, ...] = ()
    decisions: tuple[DecisionState, ...] = ()
    completed_work: tuple[str, ...] = ()
    files: tuple[FileState, ...] = ()
    tests: tuple[TestState, ...] = ()
    failed_attempts: tuple[FailedAttempt, ...] = ()
    open_issues: tuple[str, ...] = ()
    next_steps: tuple[str, ...] = ()
    evidence_refs: tuple[EvidenceRef, ...] = ()

    @classmethod
    def from_json(cls, text: str) -> TaskDelta:
        return cast(TaskDelta, _from_dict(cls, _parse_json_object(text)))


@dataclass(frozen=True, slots=True)
class TaskCheckpoint:
    goal: str | None = None
    constraints: tuple[str, ...] = ()
    decisions: tuple[DecisionState, ...] = ()
    completed_work: tuple[str, ...] = ()
    files: tuple[FileState, ...] = ()
    tests: tuple[TestState, ...] = ()
    failed_attempts: tuple[FailedAttempt, ...] = ()
    open_issues: tuple[str, ...] = ()
    next_steps: tuple[str, ...] = ()
    evidence_refs: tuple[EvidenceRef, ...] = ()
    version: int = 1
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @classmethod
    def from_json(cls, text: str) -> TaskCheckpoint:
        return cast(TaskCheckpoint, _from_dict(cls, _parse_json_object(text)))

    def to_json(self) -> str:
        return json.dumps(_json_value(self), ensure_ascii=False, sort_keys=True)

    def to_message(self) -> Message:
        return Message.internal_context(self.render(), ContextCategory.CHECKPOINT)

    def render(self) -> str:
        lines = ["# Task Checkpoint", f"Version: {self.version}"]
        if self.goal:
            lines.extend(("", "## Goal", self.goal))
        _render_list(lines, "Constraints", self.constraints)
        _render_list(lines, "Decisions", (_decision_text(x) for x in self.decisions))
        _render_list(lines, "Completed work", self.completed_work)
        _render_list(lines, "Files", (f"{x.change}: {x.path}" for x in self.files))
        _render_list(lines, "Tests", (_test_text(x) for x in self.tests))
        _render_list(
            lines,
            "Failed attempts",
            (f"{x.attempt} — {x.why_failed}" for x in self.failed_attempts),
        )
        _render_list(lines, "Open issues", self.open_issues)
        _render_list(lines, "Next steps", self.next_steps)
        _render_list(lines, "Evidence", (str(x) for x in self.evidence_refs))
        return "\n".join(lines)


_ALLOWED_FIELDS = {
    "goal",
    "constraints",
    "decisions",
    "completed_work",
    "files",
    "tests",
    "failed_attempts",
    "open_issues",
    "next_steps",
    "evidence_refs",
}


def _parse_json_object(text: str) -> dict[str, Any]:
    raw = text.strip()
    if raw.startswith("```"):
        lines = raw.splitlines()
        if len(lines) < 3 or lines[-1].strip() != "```":
            raise CompactionPayloadError("JSON code fence 不完整")
        raw = "\n".join(lines[1:-1]).strip()
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CompactionPayloadError(f"无效 JSON: {exc.msg}") from exc
    if not isinstance(value, dict):
        raise CompactionPayloadError("压缩输出必须是 JSON object")
    return value


def _from_dict(cls: type[TaskDelta] | type[TaskCheckpoint], data: dict[str, Any]):
    allowed = _ALLOWED_FIELDS | ({"version", "updated_at"} if cls is TaskCheckpoint else set())
    unknown = set(data) - allowed
    if unknown:
        raise CompactionPayloadError(f"未知字段: {sorted(unknown)}")
    try:
        kwargs: dict[str, Any] = {
            "goal": _optional_string(data.get("goal")),
            "constraints": _strings(data.get("constraints")),
            "decisions": tuple(_decision(x) for x in _objects(data.get("decisions"))),
            "completed_work": _strings(data.get("completed_work")),
            "files": tuple(_file(x) for x in _objects(data.get("files"))),
            "tests": tuple(_test(x) for x in _objects(data.get("tests"))),
            "failed_attempts": tuple(
                _failed_attempt(x) for x in _objects(data.get("failed_attempts"))
            ),
            "open_issues": _strings(data.get("open_issues")),
            "next_steps": _strings(data.get("next_steps")),
            "evidence_refs": tuple(
                _evidence_ref(x) for x in _objects(data.get("evidence_refs"))
            ),
        }
        if cls is TaskCheckpoint:
            kwargs["version"] = _positive_int(data.get("version", 1))
            kwargs["updated_at"] = _datetime(data.get("updated_at"))
        return cls(**kwargs)
    except (KeyError, TypeError, ValueError) as exc:
        raise CompactionPayloadError(f"压缩 schema 不合法: {exc}") from exc


def _strings(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
        raise TypeError("列表字段必须是 string array")
    return tuple(dict.fromkeys(x.strip() for x in value if x.strip()))


def _objects(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(x, dict) for x in value):
        raise TypeError("结构字段必须是 object array")
    return value


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError("goal 必须是 string 或 null")
    return value.strip() or None


def _decision(value: dict[str, Any]) -> DecisionState:
    _reject_unknown(value, {"decision", "rationale"})
    return DecisionState(str(value["decision"]), _optional_string(value.get("rationale")))


def _file(value: dict[str, Any]) -> FileState:
    _reject_unknown(value, {"path", "change"})
    return FileState(str(value["path"]), FileChangeKind(str(value["change"])))


def _test(value: dict[str, Any]) -> TestState:
    _reject_unknown(value, {"name", "outcome", "detail"})
    return TestState(
        str(value["name"]),
        TestOutcome(str(value["outcome"])),
        _optional_string(value.get("detail")),
    )


def _failed_attempt(value: dict[str, Any]) -> FailedAttempt:
    _reject_unknown(value, {"attempt", "why_failed"})
    return FailedAttempt(str(value["attempt"]), str(value["why_failed"]))


def _evidence_ref(value: dict[str, Any]) -> EvidenceRef:
    fields = {
        "type",
        "event_id",
        "session_id",
        "agent_run_id",
        "tool_run_id",
        "artifact_uri",
    }
    _reject_unknown(value, fields)
    return EvidenceRef(
        type=EvidenceType(str(value["type"])),
        event_id=_optional_string(value.get("event_id")),
        session_id=_optional_string(value.get("session_id")),
        agent_run_id=_optional_string(value.get("agent_run_id")),
        tool_run_id=_optional_string(value.get("tool_run_id")),
        artifact_uri=_optional_string(value.get("artifact_uri")),
    )


def _reject_unknown(value: dict[str, Any], allowed: set[str]) -> None:
    unknown = set(value) - allowed
    if unknown:
        raise TypeError(f"未知嵌套字段: {sorted(unknown)}")


def _positive_int(value: Any) -> int:
    result = int(value)
    if result < 1:
        raise ValueError("version 必须大于 0")
    return result


def _datetime(value: Any) -> datetime:
    if value is None:
        return datetime.now(UTC)
    if not isinstance(value, str):
        raise TypeError("updated_at 必须是 ISO datetime")
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _json_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "__dataclass_fields__"):
        return {key: _json_value(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def _render_list(lines: list[str], title: str, values) -> None:
    items = tuple(values)
    if not items:
        return
    lines.extend(("", f"## {title}"))
    lines.extend(f"- {item}" for item in items)


def _decision_text(value: DecisionState) -> str:
    return value.decision + (f" — {value.rationale}" if value.rationale else "")


def _test_text(value: TestState) -> str:
    return f"{value.outcome}: {value.name}" + (f" — {value.detail}" if value.detail else "")
