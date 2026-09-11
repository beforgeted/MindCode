from __future__ import annotations

import asyncio
import sqlite3
import unicodedata
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TypeVar

from codeagent.evidence.models import EvidenceRef, EvidenceType
from codeagent.infra.ids import new_id
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

_SCHEMA_VERSION = 1
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
            if version < 1:
                conn.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (1, _now().isoformat()),
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
    action TEXT NOT NULL CHECK(action IN ('CREATE', 'DELETE')),
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
"""
