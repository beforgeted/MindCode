"""RunStore：MasterRuntime 编排的持久化与恢复（P6，P5+ 事务化后语义更新）。

持久化：planned graph 的 JSON + 每个 Step 的 `step_outcome`（崩溃安全的增量 checkpoint，
upsert 幂等）。恢复时重建 TaskGraph、**不重新 plan**，避免图漂移。

**恢复语义（P8 Attempt 级幂等恢复）**：持久化 Attempt 状态机（CREATED→…→PROMOTING→PROMOTED
/DISCARDED），`PROMOTING` 在调用 `git.promote` **之前**落库并带 `candidate_sha`。resume 时：
run 已 success 或末尾 attempt 已 PROMOTED → 幂等返回；末尾 attempt 处于 `PROMOTING`（崩溃危险窗口）
→ 比对真实 base HEAD 与 candidate_sha/original_base_sha 决定"已成功不重推 / 安全重试 / BASE_STALE"；
其它状态 → 回收孤儿 worktree/branch 后，从**持久化的** `original_base_sha`（非当前 HEAD）重开
新 Attempt 重跑整张图。BASE_STALE 与语义 replan 走**独立预算**。step_outcome 仍作审计/可观测。

设计：SQLite + WAL + `asyncio.to_thread`，风格对齐 memory/sqlite_store.py；默认 NullRunStore
（no-op）：单测 / 不需要持久化时零成本。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol, TypeVar, runtime_checkable

from codeagent.agent.models import FileChangeKind, FileState
from codeagent.evidence.models import EvidenceRef, EvidenceType
from codeagent.orchestration.task_graph import Step, TaskGraph

_T = TypeVar("_T")


class AttemptState(StrEnum):
    """一次 Attempt 的生命周期状态（P8 崩溃恢复）。转移在动作**发生前**持久化。"""

    CREATED = "created"  # candidate 已建
    RUNNING = "running"  # 正在调度 Worker
    CANDIDATE_FROZEN = "candidate_frozen"  # 冻结 candidate_sha，待验收
    VERIFYING = "verifying"  # 产物级验收进行中
    VERIFIED = "verified"  # 验收通过，待 promote
    PROMOTING = "promoting"  # 即将/正在 CAS 推进真实 base（危险窗口，必带 candidate_sha）
    PROMOTED = "promoted"  # 已原子推进真实 base（终态：成功）
    DISCARDED = "discarded"  # reject/indeterminate/BASE_STALE → 丢弃
    FAILED = "failed"  # 异常


@dataclass(frozen=True, slots=True)
class AttemptRecord:
    attempt_no: int
    state: str = AttemptState.CREATED
    original_base_sha: str | None = None
    candidate_branch: str | None = None
    candidate_sha: str | None = None
    verdict: str = ""

    @property
    def promoted(self) -> bool:
        return self.state == AttemptState.PROMOTED



@dataclass(frozen=True, slots=True)
class StepOutcome:
    step_id: str
    status: str  # "integrated" | "failed"（Phase 1；executed/verified/committed 预留）
    summary: str = ""
    files: tuple[FileState, ...] = ()
    evidence_refs: tuple[EvidenceRef, ...] = ()
    branch_name: str | None = None
    merged: bool = False

    @property
    def integrated(self) -> bool:
        return self.status == "integrated"

    @property
    def completed(self) -> bool:
        # 兼容旧语义;现以 integrated 为准（已并回 base 才算真正完成）。
        return self.status in ("integrated", "completed")


@dataclass(frozen=True, slots=True)
class RunRecord:
    master_run_id: str
    session_id: str
    task: str
    status: str
    graph: TaskGraph
    outcomes: dict[str, StepOutcome] = field(default_factory=dict)
    original_base_sha: str | None = None
    promoted_sha: str | None = None
    attempts: tuple[AttemptRecord, ...] = ()

    @property
    def last_attempt(self) -> AttemptRecord | None:
        return max(self.attempts, key=lambda a: a.attempt_no) if self.attempts else None



@runtime_checkable
class RunStore(Protocol):
    async def save_run(
        self,
        *,
        master_run_id: str,
        session_id: str,
        task: str,
        graph: TaskGraph,
        status: str,
        original_base_sha: str | None = None,
    ) -> None: ...

    async def update_run_status(
        self, master_run_id: str, status: str, *, promoted_sha: str | None = None
    ) -> None: ...

    async def record_step(self, master_run_id: str, outcome: StepOutcome) -> None: ...

    async def mark_merged(self, master_run_id: str, step_id: str, branch_name: str) -> None: ...

    async def save_attempt(self, master_run_id: str, attempt: AttemptRecord) -> None: ...

    async def update_attempt(
        self,
        master_run_id: str,
        attempt_no: int,
        *,
        state: str,
        candidate_sha: str | None = None,
        verdict: str | None = None,
    ) -> None: ...

    async def load_run(self, master_run_id: str) -> RunRecord | None: ...


class NullRunStore:
    async def save_run(
        self,
        *,
        master_run_id: str,
        session_id: str,
        task: str,
        graph: TaskGraph,
        status: str,
        original_base_sha: str | None = None,
    ) -> None:
        return None

    async def update_run_status(
        self, master_run_id: str, status: str, *, promoted_sha: str | None = None
    ) -> None:
        return None

    async def record_step(self, master_run_id: str, outcome: StepOutcome) -> None:
        return None

    async def mark_merged(self, master_run_id: str, step_id: str, branch_name: str) -> None:
        return None

    async def save_attempt(self, master_run_id: str, attempt: AttemptRecord) -> None:
        return None

    async def update_attempt(
        self,
        master_run_id: str,
        attempt_no: int,
        *,
        state: str,
        candidate_sha: str | None = None,
        verdict: str | None = None,
    ) -> None:
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
    original_base_sha TEXT,
    promoted_sha  TEXT,
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
CREATE TABLE IF NOT EXISTS attempt (
    master_run_id     TEXT NOT NULL,
    attempt_no        INTEGER NOT NULL,
    original_base_sha TEXT,
    candidate_branch  TEXT,
    candidate_sha     TEXT,
    state             TEXT NOT NULL,
    verdict           TEXT NOT NULL DEFAULT '',
    updated_at        TEXT NOT NULL,
    PRIMARY KEY (master_run_id, attempt_no)
);
"""

# 旧库兼容：master_run 早期没有这两列，缺则补（ADD COLUMN 幂等靠捕获重复列错误）。
_MIGRATIONS = (
    "ALTER TABLE master_run ADD COLUMN original_base_sha TEXT",
    "ALTER TABLE master_run ADD COLUMN promoted_sha TEXT",
)


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
            for stmt in _MIGRATIONS:
                try:
                    conn.execute(stmt)
                except sqlite3.OperationalError:
                    pass  # 列已存在
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
        self,
        *,
        master_run_id: str,
        session_id: str,
        task: str,
        graph: TaskGraph,
        status: str,
        original_base_sha: str | None = None,
    ) -> None:
        now = datetime.now(UTC).isoformat()
        graph_json = graph_to_json(graph)

        def op(conn: sqlite3.Connection) -> None:
            conn.execute(
                """INSERT INTO master_run(
                    master_run_id, session_id, task, status, graph_json,
                    original_base_sha, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(master_run_id) DO UPDATE SET
                    task=excluded.task, status=excluded.status,
                    graph_json=excluded.graph_json,
                    original_base_sha=COALESCE(excluded.original_base_sha,
                                               master_run.original_base_sha),
                    updated_at=excluded.updated_at""",
                (master_run_id, session_id, task, status, graph_json, original_base_sha, now, now),
            )

        await self._run(op)

    async def update_run_status(
        self, master_run_id: str, status: str, *, promoted_sha: str | None = None
    ) -> None:
        now = datetime.now(UTC).isoformat()

        def op(conn: sqlite3.Connection) -> None:
            conn.execute(
                """UPDATE master_run SET status=?, updated_at=?,
                   promoted_sha=COALESCE(?, promoted_sha) WHERE master_run_id=?""",
                (status, now, promoted_sha, master_run_id),
            )

        await self._run(op)

    async def save_attempt(self, master_run_id: str, attempt: AttemptRecord) -> None:
        now = datetime.now(UTC).isoformat()

        def op(conn: sqlite3.Connection) -> None:
            conn.execute(
                """INSERT INTO attempt(
                    master_run_id, attempt_no, original_base_sha, candidate_branch,
                    candidate_sha, state, verdict, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(master_run_id, attempt_no) DO UPDATE SET
                    original_base_sha=excluded.original_base_sha,
                    candidate_branch=excluded.candidate_branch,
                    candidate_sha=excluded.candidate_sha,
                    state=excluded.state, verdict=excluded.verdict,
                    updated_at=excluded.updated_at""",
                (
                    master_run_id, attempt.attempt_no, attempt.original_base_sha,
                    attempt.candidate_branch, attempt.candidate_sha,
                    attempt.state, attempt.verdict, now,
                ),
            )

        await self._run(op)

    async def update_attempt(
        self,
        master_run_id: str,
        attempt_no: int,
        *,
        state: str,
        candidate_sha: str | None = None,
        verdict: str | None = None,
    ) -> None:
        now = datetime.now(UTC).isoformat()

        def op(conn: sqlite3.Connection) -> None:
            conn.execute(
                """UPDATE attempt SET state=?, updated_at=?,
                   candidate_sha=COALESCE(?, candidate_sha),
                   verdict=COALESCE(?, verdict)
                   WHERE master_run_id=? AND attempt_no=?""",
                (state, now, candidate_sha, verdict, master_run_id, attempt_no),
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
            attempts: list[AttemptRecord] = []
            for a in conn.execute(
                "SELECT * FROM attempt WHERE master_run_id=? ORDER BY attempt_no", (master_run_id,)
            ):
                attempts.append(
                    AttemptRecord(
                        attempt_no=a["attempt_no"],
                        state=a["state"],
                        original_base_sha=a["original_base_sha"],
                        candidate_branch=a["candidate_branch"],
                        candidate_sha=a["candidate_sha"],
                        verdict=a["verdict"] or "",
                    )
                )
            keys = row.keys()
            return RunRecord(
                master_run_id=row["master_run_id"],
                session_id=row["session_id"],
                task=row["task"],
                status=row["status"],
                graph=graph_from_json(row["graph_json"]),
                outcomes=outcomes,
                original_base_sha=row["original_base_sha"] if "original_base_sha" in keys else None,
                promoted_sha=row["promoted_sha"] if "promoted_sha" in keys else None,
                attempts=tuple(attempts),
            )

        return await self._run(op)


__all__ = [
    "AttemptRecord",
    "AttemptState",
    "NullRunStore",
    "RunRecord",
    "RunStore",
    "RunStoreError",
    "SqliteRunStore",
    "StepOutcome",
    "graph_from_json",
    "graph_to_json",
]
