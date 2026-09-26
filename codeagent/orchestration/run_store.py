"""RunStore：MasterRuntime 编排的持久化与恢复（P6）。

目标（V1 §3 P6，深度=「跳过已完成 Step」）：`/task` 编排中断后可 resume——
已完成 / 已合并的 Step 不重跑、不重复合并，只重跑未完成的。

设计：
- 持久化 planned graph 的 JSON。恢复时直接重建 TaskGraph，**不重新 plan**，避免图漂移。
- 每个 Step 完成即落 `step_outcome`（崩溃安全的增量 checkpoint），upsert 幂等。
- SQLite + WAL + `asyncio.to_thread`，风格对齐 memory/sqlite_store.py。
- 默认 NullRunStore（no-op）：单测 / 不需要持久化时零成本。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, TypeVar, runtime_checkable

from codeagent.agent.models import FileChangeKind, FileState
from codeagent.evidence.models import EvidenceRef, EvidenceType
from codeagent.orchestration.task_graph import Step, TaskGraph

_T = TypeVar("_T")


@dataclass(frozen=True, slots=True)
class StepOutcome:
    step_id: str
    status: str  # "completed" | "failed"
    summary: str = ""
    files: tuple[FileState, ...] = ()
    evidence_refs: tuple[EvidenceRef, ...] = ()
    branch_name: str | None = None
    merged: bool = False

    @property
    def completed(self) -> bool:
        return self.status == "completed"


@dataclass(frozen=True, slots=True)
class RunRecord:
    master_run_id: str
    session_id: str
    task: str
    status: str
    graph: TaskGraph
    outcomes: dict[str, StepOutcome] = field(default_factory=dict)


@runtime_checkable
class RunStore(Protocol):
    async def save_run(
        self, *, master_run_id: str, session_id: str, task: str, graph: TaskGraph, status: str
    ) -> None: ...

    async def update_run_status(self, master_run_id: str, status: str) -> None: ...

    async def record_step(self, master_run_id: str, outcome: StepOutcome) -> None: ...

    async def mark_merged(self, master_run_id: str, step_id: str, branch_name: str) -> None: ...

    async def load_run(self, master_run_id: str) -> RunRecord | None: ...


class NullRunStore:
    async def save_run(
        self, *, master_run_id: str, session_id: str, task: str, graph: TaskGraph, status: str
    ) -> None:
        return None

    async def update_run_status(self, master_run_id: str, status: str) -> None:
        return None

    async def record_step(self, master_run_id: str, outcome: StepOutcome) -> None:
        return None

    async def mark_merged(self, master_run_id: str, step_id: str, branch_name: str) -> None:
        return None

    async def load_run(self, master_run_id: str) -> RunRecord | None:
        return None


# --- 序列化：frozen dataclass ↔ JSON（不引 pydantic）---


def graph_to_json(graph: TaskGraph) -> str:
    return json.dumps(
        [
            {
                "id": s.id,
                "agent_id": s.agent_id,
                "instruction": s.instruction,
                "dependencies": sorted(s.dependencies),
                "read_only": s.read_only,
            }
            for s in graph.steps
        ],
        ensure_ascii=False,
    )


def graph_from_json(raw: str) -> TaskGraph:
    data = json.loads(raw)
    steps = [
        Step(
            id=d["id"],
            agent_id=d["agent_id"],
            instruction=d["instruction"],
            dependencies=frozenset(d.get("dependencies", ())),
            read_only=bool(d.get("read_only", False)),
        )
        for d in data
    ]
    return TaskGraph(steps)


def _files_to_json(files: tuple[FileState, ...]) -> str:
    return json.dumps([{"path": f.path, "change": str(f.change)} for f in files])


def _files_from_json(raw: str | None) -> tuple[FileState, ...]:
    if not raw:
        return ()
    return tuple(
        FileState(path=d["path"], change=FileChangeKind(d["change"])) for d in json.loads(raw)
    )


def _evidence_to_json(refs: tuple[EvidenceRef, ...]) -> str:
    return json.dumps(
        [
            {
                "type": str(r.type),
                "event_id": r.event_id,
                "session_id": r.session_id,
                "agent_run_id": r.agent_run_id,
                "tool_run_id": r.tool_run_id,
                "artifact_uri": r.artifact_uri,
            }
            for r in refs
        ]
    )


def _evidence_from_json(raw: str | None) -> tuple[EvidenceRef, ...]:
    if not raw:
        return ()
    return tuple(
        EvidenceRef(
            type=EvidenceType(d["type"]),
            event_id=d.get("event_id"),
            session_id=d.get("session_id"),
            agent_run_id=d.get("agent_run_id"),
            tool_run_id=d.get("tool_run_id"),
            artifact_uri=d.get("artifact_uri"),
        )
        for d in json.loads(raw)
    )


class RunStoreError(RuntimeError):
    pass


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS master_run (
    master_run_id TEXT PRIMARY KEY,
    session_id    TEXT NOT NULL,
    task          TEXT NOT NULL,
    status        TEXT NOT NULL,
    graph_json    TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS step_outcome (
    master_run_id TEXT NOT NULL,
    step_id       TEXT NOT NULL,
    status        TEXT NOT NULL,
    summary       TEXT NOT NULL DEFAULT '',
    files_json    TEXT,
    evidence_json TEXT,
    branch_name   TEXT,
    merged        INTEGER NOT NULL DEFAULT 0,
    updated_at    TEXT NOT NULL,
    PRIMARY KEY (master_run_id, step_id)
);
"""


class SqliteRunStore:
    """`state_root/runs.db`，WAL，阻塞 I/O 一律 to_thread。"""

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)
        self._lock = asyncio.Lock()
        self._started = False

    async def start(self) -> None:
        async with self._lock:
            if self._started:
                return
            self._path.parent.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(self._initialize)
            self._started = True

    def _initialize(self) -> None:
        conn = self._connect()
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(_SCHEMA_SQL)
            conn.commit()
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("PRAGMA synchronous = NORMAL")
        return conn

    async def _run(self, operation: Callable[[sqlite3.Connection], _T]) -> _T:
        async with self._lock:
            if not self._started:
                raise RunStoreError("RunStore 尚未启动")
            try:
                return await asyncio.to_thread(self._with_connection, operation)
            except sqlite3.Error as exc:
                raise RunStoreError(f"SQLite RunStore 不可用: {exc}") from exc

    def _with_connection(self, operation: Callable[[sqlite3.Connection], _T]) -> _T:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            result = operation(conn)
            conn.commit()
            return result
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    async def save_run(
        self, *, master_run_id: str, session_id: str, task: str, graph: TaskGraph, status: str
    ) -> None:
        now = datetime.now(UTC).isoformat()
        graph_json = graph_to_json(graph)

        def op(conn: sqlite3.Connection) -> None:
            conn.execute(
                """INSERT INTO master_run(
                    master_run_id, session_id, task, status, graph_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(master_run_id) DO UPDATE SET
                    task=excluded.task, status=excluded.status,
                    graph_json=excluded.graph_json, updated_at=excluded.updated_at""",
                (master_run_id, session_id, task, status, graph_json, now, now),
            )

        await self._run(op)

    async def update_run_status(self, master_run_id: str, status: str) -> None:
        now = datetime.now(UTC).isoformat()

        def op(conn: sqlite3.Connection) -> None:
            conn.execute(
                "UPDATE master_run SET status=?, updated_at=? WHERE master_run_id=?",
                (status, now, master_run_id),
            )

        await self._run(op)

    async def record_step(self, master_run_id: str, outcome: StepOutcome) -> None:
        now = datetime.now(UTC).isoformat()

        def op(conn: sqlite3.Connection) -> None:
            conn.execute(
                """INSERT INTO step_outcome(
                    master_run_id, step_id, status, summary, files_json,
                    evidence_json, branch_name, merged, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(master_run_id, step_id) DO UPDATE SET
                    status=excluded.status, summary=excluded.summary,
                    files_json=excluded.files_json, evidence_json=excluded.evidence_json,
                    branch_name=excluded.branch_name, merged=excluded.merged,
                    updated_at=excluded.updated_at""",
                (
                    master_run_id,
                    outcome.step_id,
                    outcome.status,
                    outcome.summary,
                    _files_to_json(outcome.files),
                    _evidence_to_json(outcome.evidence_refs),
                    outcome.branch_name,
                    1 if outcome.merged else 0,
                    now,
                ),
            )

        await self._run(op)

    async def mark_merged(self, master_run_id: str, step_id: str, branch_name: str) -> None:
        now = datetime.now(UTC).isoformat()

        def op(conn: sqlite3.Connection) -> None:
            conn.execute(
                """UPDATE step_outcome SET merged=1, branch_name=?, updated_at=?
                   WHERE master_run_id=? AND step_id=?""",
                (branch_name, now, master_run_id, step_id),
            )

        await self._run(op)

    async def load_run(self, master_run_id: str) -> RunRecord | None:
        def op(conn: sqlite3.Connection) -> RunRecord | None:
            row = conn.execute(
                "SELECT * FROM master_run WHERE master_run_id=?", (master_run_id,)
            ).fetchone()
            if row is None:
                return None
            outcomes: dict[str, StepOutcome] = {}
            for r in conn.execute(
                "SELECT * FROM step_outcome WHERE master_run_id=?", (master_run_id,)
            ):
                outcomes[r["step_id"]] = StepOutcome(
                    step_id=r["step_id"],
                    status=r["status"],
                    summary=r["summary"] or "",
                    files=_files_from_json(r["files_json"]),
                    evidence_refs=_evidence_from_json(r["evidence_json"]),
                    branch_name=r["branch_name"],
                    merged=bool(r["merged"]),
                )
            return RunRecord(
                master_run_id=row["master_run_id"],
                session_id=row["session_id"],
                task=row["task"],
                status=row["status"],
                graph=graph_from_json(row["graph_json"]),
                outcomes=outcomes,
            )

        return await self._run(op)


__all__ = [
    "NullRunStore",
    "RunRecord",
    "RunStore",
    "RunStoreError",
    "SqliteRunStore",
    "StepOutcome",
    "graph_from_json",
    "graph_to_json",
]
