from __future__ import annotations

import asyncio
import json
import sqlite3
import unicodedata
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TypeVar

from codeagent.evidence.models import EvidenceRef, EvidenceType
from codeagent.infra.ids import new_id
from codeagent.memory.governance_models import (
    CandidateReceipt,
    CandidateStatus,
    MemoryCandidate,
    StageResult,
)
from codeagent.memory.models import (
    DeleteResult,
    MemoryItem,
    MemoryListQuery,
    MemoryNotFoundError,
    MemoryScope,
    MemorySearchHit,
    MemorySearchQuery,
    MemorySource,
    MemoryStatus,
    MemoryType,
    MemoryUnavailableError,
    NewMemoryItem,
)

_SCHEMA_VERSION = 2
_T = TypeVar("_T")


class SqliteMemoryStore:
    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._lock = asyncio.Lock()
        self._started = False
        self._closed = False

    async def start(self) -> None:
        async with self._lock:
            if self._started:
                return
            if self._closed:
                raise MemoryUnavailableError("MemoryStore 已关闭")
            self._path.parent.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(self._initialize)
            self._started = True

    async def create(self, draft: NewMemoryItem, *, event_id: str | None = None) -> MemoryItem:
        return await self._run(lambda conn: self._create(conn, draft, event_id))

    async def get(
        self,
        project_id: str,
        memory_id: str,
        *,
        include_deleted: bool = False,
    ) -> MemoryItem | None:
        return await self._run(
            lambda conn: self._get(conn, project_id, memory_id, include_deleted),
            write=False,
        )

    async def list(self, query: MemoryListQuery) -> list[MemoryItem]:
        return await self._run(lambda conn: self._list(conn, query), write=False)

    async def search(self, query: MemorySearchQuery) -> list[MemorySearchHit]:
        return await self._run(lambda conn: self._search(conn, query), write=False)

    async def soft_delete(
        self,
        project_id: str,
        memory_id: str,
        *,
        event_id: str | None = None,
    ) -> DeleteResult:
        return await self._run(
            lambda conn: self._soft_delete(conn, project_id, memory_id, event_id)
        )

    async def revisions(self) -> tuple[int, int]:
        return await self._run(self._revisions, write=False)

    async def mark_indexed(self, revision: int) -> None:
        await self._run(lambda conn: self._set_meta(conn, "indexed_revision", str(revision)))

    # --- P4 治理：候选暂存 / 游标 ---

    async def governance_cursor(self, project_id: str, session_id: str) -> int:
        return await self._run(
            lambda conn: self._governance_cursor(conn, project_id, session_id),
            write=False,
        )

    async def stage_event_batch(
        self,
        *,
        project_id: str,
        session_id: str,
        expected_ordinal: int,
        next_ordinal: int,
        last_event_id: str | None,
        candidates: tuple[MemoryCandidate, ...],
        receipts: tuple[CandidateReceipt, ...],
    ) -> StageResult:
        return await self._run(
            lambda conn: self._stage_event_batch(
                conn,
                project_id,
                session_id,
                expected_ordinal,
                next_ordinal,
                last_event_id,
                candidates,
                receipts,
            )
        )

    async def list_pending_candidates(
        self,
        project_id: str,
        session_id: str,
        *,
        limit: int = 100,
    ) -> list[MemoryCandidate]:
        return await self._run(
            lambda conn: self._list_pending_candidates(conn, project_id, session_id, limit),
            write=False,
        )

    async def stage_shared_candidates(
        self, candidates: tuple[MemoryCandidate, ...]
    ) -> int:
        """Supervisor 集中 staging Worker 候选（P6）。

        与 Session-End harvest 走同一张候选表 + 同一 candidate_key，`INSERT OR IGNORE`
        天然去重；不动 governance 游标（游标是 event-scan 的进度，与集中 staging 无关）。
        """
        if not candidates:
            return 0
        return await self._run(
            lambda conn: self._stage_shared_candidates(conn, candidates)
        )

    async def finalize_candidate(
        self,
        candidate_key: str,
        *,
        outcome: CandidateStatus | str,
        reason: str,
    ) -> None:
        await self._run(
            lambda conn: self._finalize_candidate(conn, candidate_key, str(outcome), reason)
        )

    async def supersede_and_create(
        self,
        old_id: str,
        draft: NewMemoryItem,
        *,
        event_id: str | None = None,
    ) -> MemoryItem:
        return await self._run(
            lambda conn: self._supersede_and_create(conn, old_id, draft, event_id)
        )

    async def aclose(self) -> None:
        async with self._lock:
            self._closed = True

    async def _run(
        self,
        operation: Callable[[sqlite3.Connection], _T],
        *,
        write: bool = True,
    ) -> _T:
        async with self._lock:
            if self._closed:
                raise MemoryUnavailableError("MemoryStore 已关闭")
            if not self._started:
                raise MemoryUnavailableError("MemoryStore 尚未启动")
            try:
                return await asyncio.to_thread(self._with_connection, operation, write)
            except (MemoryNotFoundError, MemoryUnavailableError):
                raise
            except sqlite3.Error as exc:
                raise MemoryUnavailableError(f"SQLite Memory 不可用: {exc}") from exc

    def _with_connection(
        self,
        operation: Callable[[sqlite3.Connection], _T],
        write: bool,
    ) -> _T:
        conn = self._connect()
        try:
            if write:
                conn.execute("BEGIN IMMEDIATE")
            result = operation(conn)
            if write:
                conn.commit()
            return result
        except BaseException:
            if write:
                conn.rollback()
            raise
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("PRAGMA synchronous = NORMAL")
        return conn

    def _initialize(self) -> None:
        conn = self._connect()
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("BEGIN IMMEDIATE")
            conn.executescript(_SCHEMA_SQL)
            row = conn.execute("SELECT MAX(version) AS version FROM schema_migrations").fetchone()
            version = int(row["version"] or 0)
            if version > _SCHEMA_VERSION:
                raise MemoryUnavailableError(
                    f"Memory DB schema {version} 高于当前支持版本 {_SCHEMA_VERSION}"
                )
            for pending in range(version + 1, _SCHEMA_VERSION + 1):
                conn.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (pending, _now().isoformat()),
                )
            self._set_meta(conn, "content_revision", self._meta(conn, "content_revision", "0"))
            self._set_meta(conn, "indexed_revision", self._meta(conn, "indexed_revision", "0"))
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _create(
        self,
        conn: sqlite3.Connection,
        draft: NewMemoryItem,
        event_id: str | None,
    ) -> MemoryItem:
        memory_id = new_id("mem")
        now = _now()
        conn.execute(
            """INSERT INTO memory_items(
                id, project_id, scope, scope_id, type, content, source, status,
                confidence, importance, created_at, updated_at, expires_at, version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)""",
            (
                memory_id,
                draft.project_id,
                str(draft.scope),
                draft.scope_id,
                str(draft.type),
                draft.content,
                str(draft.source),
                str(MemoryStatus.ACTIVE),
                draft.confidence,
                draft.importance,
                now.isoformat(),
                now.isoformat(),
                _iso(draft.expires_at),
            ),
        )
        conn.executemany(
            "INSERT INTO memory_tags(memory_id, tag) VALUES (?, ?)",
            ((memory_id, tag) for tag in draft.tags),
        )
        conn.executemany(
            """INSERT INTO memory_evidence(
                memory_id, ordinal, evidence_type, event_id, session_id,
                agent_run_id, tool_run_id, artifact_uri
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                (
                    memory_id,
                    ordinal,
                    str(ref.type),
                    ref.event_id,
                    ref.session_id,
                    ref.agent_run_id,
                    ref.tool_run_id,
                    ref.artifact_uri,
                )
                for ordinal, ref in enumerate(draft.evidence_refs)
            ),
        )
        self._audit(
            conn,
            memory_id,
            draft.project_id,
            "CREATE",
            draft.source,
            None,
            MemoryStatus.ACTIVE,
            1,
            event_id,
        )
        self._increment_revision(conn)
        item = self._get(conn, draft.project_id, memory_id, True)
        assert item is not None
        return item

    def _get(
        self,
        conn: sqlite3.Connection,
        project_id: str,
        memory_id: str,
        include_deleted: bool,
    ) -> MemoryItem | None:
        sql = "SELECT * FROM memory_items WHERE project_id = ? AND id = ?"
        params: list[object] = [project_id, memory_id]
        if not include_deleted:
            sql += " AND status != ?"
            params.append(str(MemoryStatus.DELETED))
        row = conn.execute(sql, params).fetchone()
        return self._row_to_item(conn, row) if row else None

    def _list(self, conn: sqlite3.Connection, query: MemoryListQuery) -> list[MemoryItem]:
        sql = """SELECT * FROM memory_items
                 WHERE project_id = ? AND status = ?
                   AND (expires_at IS NULL OR expires_at > ?)"""
        params: list[object] = [query.project_id, str(query.status), _now().isoformat()]
        if query.type is not None:
            sql += " AND type = ?"
            params.append(str(query.type))
        sql += " ORDER BY updated_at DESC, id LIMIT ? OFFSET ?"
        params.extend((max(1, min(query.limit, 200)), max(0, query.offset)))
        return [self._row_to_item(conn, row) for row in conn.execute(sql, params)]

    def _search(
        self,
        conn: sqlite3.Connection,
        query: MemorySearchQuery,
    ) -> list[MemorySearchHit]:
        text = _normalize(query.text)
        if not text:
            return []
        limit = max(1, min(query.limit, 100))
        common = [query.project_id, str(MemoryStatus.ACTIVE), _now().isoformat()]
        type_clause = ""
        if query.type is not None:
            type_clause = " AND m.type = ?"
            common.append(str(query.type))
        if len(text) >= 3:
            sql = f"""SELECT m.*, bm25(memory_fts) AS rank
                      FROM memory_fts
                      JOIN memory_items m ON m.rowid = memory_fts.rowid
                      WHERE memory_fts MATCH ?
                        AND m.project_id = ? AND m.status = ?
                        AND (m.expires_at IS NULL OR m.expires_at > ?)
                        {type_clause}
                      ORDER BY rank, m.updated_at DESC, m.id LIMIT ?"""
            params = [_fts_phrase(text), *common, limit]
            rows = conn.execute(sql, params).fetchall()
            return [
                MemorySearchHit(self._row_to_item(conn, row), 1.0 / (index + 1))
                for index, row in enumerate(rows)
            ]
        pattern = f"%{_escape_like(text)}%"
        sql = f"""SELECT m.* FROM memory_items m
                  WHERE m.content LIKE ? ESCAPE '\\'
                    AND m.project_id = ? AND m.status = ?
                    AND (m.expires_at IS NULL OR m.expires_at > ?)
                    {type_clause}
                  ORDER BY m.updated_at DESC, m.id LIMIT ?"""
        rows = conn.execute(sql, [pattern, *common, limit]).fetchall()
        return [
            MemorySearchHit(self._row_to_item(conn, row), 1.0 / (index + 1))
            for index, row in enumerate(rows)
        ]

    def _soft_delete(
        self,
        conn: sqlite3.Connection,
        project_id: str,
        memory_id: str,
        event_id: str | None,
    ) -> DeleteResult:
        item = self._get(conn, project_id, memory_id, True)
        if item is None:
            raise MemoryNotFoundError(f"Memory 不存在: {memory_id}")
        if item.status is MemoryStatus.DELETED:
            return DeleteResult(item, already_deleted=True)
        now = _now()
        conn.execute(
            """UPDATE memory_items
               SET status = ?, deleted_at = ?, updated_at = ?, version = version + 1
               WHERE project_id = ? AND id = ?""",
            (str(MemoryStatus.DELETED), now.isoformat(), now.isoformat(), project_id, memory_id),
        )
        self._audit(
            conn,
            memory_id,
            project_id,
            "DELETE",
            item.source,
            item.status,
            MemoryStatus.DELETED,
            item.version + 1,
            event_id,
        )
        self._increment_revision(conn)
        deleted = self._get(conn, project_id, memory_id, True)
        assert deleted is not None
        return DeleteResult(deleted)

    def _governance_cursor(
        self,
        conn: sqlite3.Connection,
        project_id: str,
        session_id: str,
    ) -> int:
        row = conn.execute(
            """SELECT next_ordinal FROM memory_governance_cursor
               WHERE project_id = ? AND session_id = ?""",
            (project_id, session_id),
        ).fetchone()
        return int(row["next_ordinal"]) if row else 0

    def _stage_shared_candidates(
        self,
        conn: sqlite3.Connection,
        candidates: tuple[MemoryCandidate, ...],
    ) -> int:
        staged = 0
        for candidate in candidates:
            cursor = conn.execute(
                """INSERT OR IGNORE INTO memory_candidates(
                    candidate_key, project_id, session_id, content, content_sha256,
                    source, proposed_scope, proposed_type, evidence_json,
                    source_event_ids_json, reason, extractor_version, contract_version,
                    status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    candidate.candidate_key,
                    candidate.project_id,
                    candidate.session_id,
                    candidate.content,
                    candidate.content_sha256,
                    str(candidate.source),
                    str(candidate.proposed_scope),
                    str(candidate.proposed_type),
                    _dump_evidence(candidate.evidence_refs),
                    json.dumps(list(candidate.source_event_ids)),
                    candidate.reason,
                    candidate.extractor_version,
                    candidate.contract_version,
                    str(candidate.status),
                    candidate.created_at.isoformat(),
                ),
            )
            if cursor.rowcount:
                staged += 1
        return staged

    def _stage_event_batch(
        self,
        conn: sqlite3.Connection,
        project_id: str,
        session_id: str,
        expected_ordinal: int,
        next_ordinal: int,
        last_event_id: str | None,
        candidates: tuple[MemoryCandidate, ...],
        receipts: tuple[CandidateReceipt, ...],
    ) -> StageResult:
        current = self._governance_cursor(conn, project_id, session_id)
        if current != expected_ordinal:
            # CAS 失败：有并发 pass 已推进游标，本批不落库，交由上层用新游标重取。
            return StageResult(staged=0, receipts=0, duplicates=0, next_ordinal=current)
        staged = 0
        duplicates = 0
        for candidate in candidates:
            cursor = conn.execute(
                """INSERT OR IGNORE INTO memory_candidates(
                    candidate_key, project_id, session_id, content, content_sha256,
                    source, proposed_scope, proposed_type, evidence_json,
                    source_event_ids_json, reason, extractor_version, contract_version,
                    status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    candidate.candidate_key,
                    candidate.project_id,
                    candidate.session_id,
                    candidate.content,
                    candidate.content_sha256,
                    str(candidate.source),
                    str(candidate.proposed_scope),
                    str(candidate.proposed_type),
                    _dump_evidence(candidate.evidence_refs),
                    json.dumps(list(candidate.source_event_ids)),
                    candidate.reason,
                    candidate.extractor_version,
                    candidate.contract_version,
                    str(candidate.status),
                    candidate.created_at.isoformat(),
                ),
            )
            if cursor.rowcount:
                staged += 1
            else:
                duplicates += 1
        stored_receipts = 0
        for receipt in receipts:
            cursor = conn.execute(
                """INSERT OR IGNORE INTO memory_candidate_receipts(
                    candidate_key, project_id, session_id, content_sha256,
                    source_event_ids_json, outcome, reason, extractor_version, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    receipt.candidate_key,
                    receipt.project_id,
                    receipt.session_id,
                    receipt.content_sha256,
                    json.dumps(list(receipt.source_event_ids)),
                    str(receipt.outcome),
                    str(receipt.reason),
                    receipt.extractor_version,
                    receipt.created_at.isoformat(),
                ),
            )
            if cursor.rowcount:
                stored_receipts += 1
        conn.execute(
            """INSERT INTO memory_governance_cursor(
                project_id, session_id, next_ordinal, last_event_id, updated_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(project_id, session_id) DO UPDATE SET
                next_ordinal = excluded.next_ordinal,
                last_event_id = excluded.last_event_id,
                updated_at = excluded.updated_at""",
            (project_id, session_id, next_ordinal, last_event_id, _now().isoformat()),
        )
        return StageResult(
            staged=staged,
            receipts=stored_receipts,
            duplicates=duplicates,
            next_ordinal=next_ordinal,
        )

    def _list_pending_candidates(
        self,
        conn: sqlite3.Connection,
        project_id: str,
        session_id: str,
        limit: int,
    ) -> list[MemoryCandidate]:
        rows = conn.execute(
            """SELECT * FROM memory_candidates
               WHERE project_id = ? AND session_id = ? AND status = ?
               ORDER BY created_at, candidate_key LIMIT ?""",
            (project_id, session_id, str(CandidateStatus.PENDING_JUDGE), max(1, limit)),
        ).fetchall()
        return [self._row_to_candidate(row) for row in rows]

    def _finalize_candidate(
        self,
        conn: sqlite3.Connection,
        candidate_key: str,
        outcome: str,
        reason: str,
    ) -> None:
        row = conn.execute(
            "SELECT * FROM memory_candidates WHERE candidate_key = ?",
            (candidate_key,),
        ).fetchone()
        if row is None:
            return
        conn.execute(
            """INSERT OR IGNORE INTO memory_candidate_receipts(
                candidate_key, project_id, session_id, content_sha256,
                source_event_ids_json, outcome, reason, extractor_version, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                row["candidate_key"],
                row["project_id"],
                row["session_id"],
                row["content_sha256"],
                row["source_event_ids_json"],
                outcome,
                reason,
                row["extractor_version"],
                _now().isoformat(),
            ),
        )
        conn.execute("DELETE FROM memory_candidates WHERE candidate_key = ?", (candidate_key,))

    def _supersede_and_create(
        self,
        conn: sqlite3.Connection,
        old_id: str,
        draft: NewMemoryItem,
        event_id: str | None,
    ) -> MemoryItem:
        old = self._get(conn, draft.project_id, old_id, True)
        if old is None:
            raise MemoryNotFoundError(f"Memory 不存在: {old_id}")
        if old.status is MemoryStatus.ACTIVE:
            now = _now()
            conn.execute(
                """UPDATE memory_items
                   SET status = ?, updated_at = ?, version = version + 1
                   WHERE project_id = ? AND id = ?""",
                (str(MemoryStatus.SUPERSEDED), now.isoformat(), draft.project_id, old_id),
            )
            self._audit(
                conn,
                old_id,
                draft.project_id,
                "SUPERSEDE",
                old.source,
                old.status,
                MemoryStatus.SUPERSEDED,
                old.version + 1,
                event_id,
            )
        return self._create(conn, draft, event_id)

    def _row_to_candidate(self, row: sqlite3.Row) -> MemoryCandidate:
        return MemoryCandidate(
            candidate_key=row["candidate_key"],
            project_id=row["project_id"],
            session_id=row["session_id"],
            content=row["content"],
            content_sha256=row["content_sha256"],
            source=MemorySource(row["source"]),
            proposed_scope=MemoryScope(row["proposed_scope"]),
            proposed_type=MemoryType(row["proposed_type"]),
            evidence_refs=_load_evidence(row["evidence_json"]),
            source_event_ids=tuple(json.loads(row["source_event_ids_json"])),
            reason=row["reason"],
            extractor_version=row["extractor_version"],
            contract_version=row["contract_version"],
            status=CandidateStatus(row["status"]),
            created_at=_parse_time(row["created_at"]),
        )

    def _row_to_item(self, conn: sqlite3.Connection, row: sqlite3.Row) -> MemoryItem:
        tags = tuple(
            item["tag"]
            for item in conn.execute(
                "SELECT tag FROM memory_tags WHERE memory_id = ? ORDER BY tag", (row["id"],)
            )
        )
        evidence = tuple(
            EvidenceRef(
                type=EvidenceType(item["evidence_type"]),
                event_id=item["event_id"],
                session_id=item["session_id"],
                agent_run_id=item["agent_run_id"],
                tool_run_id=item["tool_run_id"],
                artifact_uri=item["artifact_uri"],
            )
            for item in conn.execute(
                "SELECT * FROM memory_evidence WHERE memory_id = ? ORDER BY ordinal",
                (row["id"],),
            )
        )
        return MemoryItem(
            id=row["id"],
            project_id=row["project_id"],
            scope=MemoryScope(row["scope"]),
            scope_id=row["scope_id"],
            type=MemoryType(row["type"]),
            content=row["content"],
            source=MemorySource(row["source"]),
            status=MemoryStatus(row["status"]),
            evidence_refs=evidence,
            tags=tags,
            confidence=row["confidence"],
            importance=row["importance"],
            created_at=_parse_time(row["created_at"]),
            updated_at=_parse_time(row["updated_at"]),
            expires_at=_parse_optional_time(row["expires_at"]),
            deleted_at=_parse_optional_time(row["deleted_at"]),
            version=row["version"],
        )

    def _audit(
        self,
        conn: sqlite3.Connection,
        memory_id: str,
        project_id: str,
        action: str,
        source: MemorySource,
        before: MemoryStatus | None,
        after: MemoryStatus,
        version: int,
        event_id: str | None,
    ) -> None:
        conn.execute(
            """INSERT INTO memory_audit(
                audit_id, memory_id, project_id, action, actor_source,
                before_status, after_status, item_version, event_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                new_id("maud"),
                memory_id,
                project_id,
                action,
                str(source),
                str(before) if before else None,
                str(after),
                version,
                event_id,
                _now().isoformat(),
            ),
        )

    def _increment_revision(self, conn: sqlite3.Connection) -> int:
        revision = int(self._meta(conn, "content_revision", "0")) + 1
        self._set_meta(conn, "content_revision", str(revision))
        return revision

    def _revisions(self, conn: sqlite3.Connection) -> tuple[int, int]:
        return (
            int(self._meta(conn, "content_revision", "0")),
            int(self._meta(conn, "indexed_revision", "0")),
        )

    @staticmethod
    def _meta(conn: sqlite3.Connection, key: str, default: str) -> str:
        row = conn.execute("SELECT value FROM memory_meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    @staticmethod
    def _set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
        conn.execute(
            """INSERT INTO memory_meta(key, value) VALUES (?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
            (key, value),
        )


def _now() -> datetime:
    return datetime.now(UTC)


def _dump_evidence(refs: tuple[EvidenceRef, ...]) -> str:
    return json.dumps(
        [
            {
                "type": str(ref.type),
                "event_id": ref.event_id,
                "session_id": ref.session_id,
                "agent_run_id": ref.agent_run_id,
                "tool_run_id": ref.tool_run_id,
                "artifact_uri": ref.artifact_uri,
            }
            for ref in refs
        ]
    )


def _load_evidence(raw: str) -> tuple[EvidenceRef, ...]:
    return tuple(
        EvidenceRef(
            type=EvidenceType(item["type"]),
            event_id=item.get("event_id"),
            session_id=item.get("session_id"),
            agent_run_id=item.get("agent_run_id"),
            tool_run_id=item.get("tool_run_id"),
            artifact_uri=item.get("artifact_uri"),
        )
        for item in json.loads(raw)
    )


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _parse_optional_time(value: str | None) -> datetime | None:
    return _parse_time(value) if value else None


def _normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", text).strip().casefold()


def _fts_phrase(text: str) -> str:
    return '"' + text.replace('"', '""') + '"'


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS memory_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS memory_items (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    scope TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    type TEXT NOT NULL,
    content TEXT NOT NULL CHECK(length(trim(content)) > 0),
    source TEXT NOT NULL,
    status TEXT NOT NULL,
    confidence REAL,
    importance INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    expires_at TEXT,
    deleted_at TEXT,
    version INTEGER NOT NULL CHECK(version >= 1)
);
CREATE INDEX IF NOT EXISTS memory_items_active_project_idx
ON memory_items(project_id, scope, scope_id, status, updated_at DESC);
CREATE TABLE IF NOT EXISTS memory_tags (
    memory_id TEXT NOT NULL REFERENCES memory_items(id) ON DELETE CASCADE,
    tag TEXT NOT NULL,
    PRIMARY KEY(memory_id, tag)
);
CREATE TABLE IF NOT EXISTS memory_evidence (
    memory_id TEXT NOT NULL REFERENCES memory_items(id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL,
    evidence_type TEXT NOT NULL,
    event_id TEXT,
    session_id TEXT,
    agent_run_id TEXT,
    tool_run_id TEXT,
    artifact_uri TEXT,
    PRIMARY KEY(memory_id, ordinal)
);
CREATE INDEX IF NOT EXISTS memory_evidence_event_idx ON memory_evidence(event_id);
CREATE INDEX IF NOT EXISTS memory_evidence_artifact_idx ON memory_evidence(artifact_uri);
CREATE TABLE IF NOT EXISTS memory_audit (
    audit_id TEXT PRIMARY KEY,
    memory_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    action TEXT NOT NULL CHECK(action IN ('CREATE', 'DELETE', 'SUPERSEDE')),
    actor_source TEXT NOT NULL,
    before_status TEXT,
    after_status TEXT NOT NULL,
    item_version INTEGER NOT NULL,
    event_id TEXT,
    created_at TEXT NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
    content,
    content='memory_items',
    content_rowid='rowid',
    tokenize='trigram'
);
CREATE TRIGGER IF NOT EXISTS memory_items_ai AFTER INSERT ON memory_items BEGIN
    INSERT INTO memory_fts(rowid, content) VALUES (new.rowid, new.content);
END;
CREATE TRIGGER IF NOT EXISTS memory_items_ad AFTER DELETE ON memory_items BEGIN
    INSERT INTO memory_fts(memory_fts, rowid, content) VALUES ('delete', old.rowid, old.content);
END;
CREATE TRIGGER IF NOT EXISTS memory_items_au AFTER UPDATE OF content ON memory_items BEGIN
    INSERT INTO memory_fts(memory_fts, rowid, content) VALUES ('delete', old.rowid, old.content);
    INSERT INTO memory_fts(rowid, content) VALUES (new.rowid, new.content);
END;
CREATE TABLE IF NOT EXISTS memory_candidates (
    candidate_key TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    content TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    source TEXT NOT NULL,
    proposed_scope TEXT NOT NULL,
    proposed_type TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    source_event_ids_json TEXT NOT NULL,
    reason TEXT NOT NULL,
    extractor_version TEXT NOT NULL,
    contract_version INTEGER NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS memory_candidates_pending_idx
ON memory_candidates(project_id, session_id, status, created_at);
CREATE TABLE IF NOT EXISTS memory_candidate_receipts (
    candidate_key TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    source_event_ids_json TEXT NOT NULL,
    outcome TEXT NOT NULL,
    reason TEXT NOT NULL,
    extractor_version TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS memory_governance_cursor (
    project_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    next_ordinal INTEGER NOT NULL,
    last_event_id TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(project_id, session_id)
);
"""
