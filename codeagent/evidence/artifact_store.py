"""ArtifactStore：超大 Tool 输出 / diff / test log 的完整落盘。

关键点是 `open_writer()` 这个**流式**写入口。`run_command` 必须边读子进程
输出边往 artifact 写，不能先在内存里攒完整个 buffer 再交给 Normalizer ——
否则 P1 想解决的"单条 300K token 输出"在进 Normalizer 之前就已经把内存吃掉了。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import TracebackType
from typing import Protocol, runtime_checkable

from codeagent.evidence.models import ArtifactRef
from codeagent.infra.ids import new_artifact_id


@runtime_checkable
class ArtifactStore(Protocol):
    async def save_text(
        self, kind: str, text: str, *, metadata: dict | None = None
    ) -> ArtifactRef: ...

    def open_writer(self, kind: str, *, metadata: dict | None = None) -> ArtifactWriter: ...

    async def load_text(self, uri: str, *, max_bytes: int | None = None) -> str: ...


class ArtifactWriter:
    """流式写入。`async with` 使用，退出时自动 close。"""

    def __init__(self, ref: ArtifactRef, metadata: dict | None = None) -> None:
        self._ref = ref
        self._metadata = metadata or {}
        self._handle = None
        self._bytes = 0

    async def __aenter__(self) -> ArtifactWriter:
        self._ref.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = await asyncio.to_thread(self._ref.path.open, "wb")
        return self

    async def write(self, chunk: bytes) -> None:
        if self._handle is None:
            raise RuntimeError("writer 未打开")
        await asyncio.to_thread(self._handle.write, chunk)
        self._bytes += len(chunk)

    @property
    def bytes_written(self) -> int:
        return self._bytes

    @property
    def ref(self) -> ArtifactRef:
        return ArtifactRef(
            artifact_id=self._ref.artifact_id,
            kind=self._ref.kind,
            path=self._ref.path,
            size_bytes=self._bytes,
            media_type=self._ref.media_type,
        )

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._handle is not None:
            await asyncio.to_thread(self._handle.close)
            self._handle = None
        meta_path = self._ref.path.with_suffix(self._ref.path.suffix + ".meta.json")
        payload = {
            **self._metadata,
            "size_bytes": self._bytes,
            "artifact_id": self._ref.artifact_id,
        }
        await asyncio.to_thread(
            meta_path.write_text, json.dumps(payload, ensure_ascii=False, indent=2), "utf-8"
        )


class FileArtifactStore:
    def __init__(self, root: Path) -> None:
        self._root = Path(root) / "artifacts"
        self._root.mkdir(parents=True, exist_ok=True)

    def _path(self, kind: str, artifact_id: str) -> Path:
        return self._root / kind / f"{artifact_id}.txt"

    def open_writer(self, kind: str, *, metadata: dict | None = None) -> ArtifactWriter:
        artifact_id = new_artifact_id()
        ref = ArtifactRef(
            artifact_id=artifact_id,
            kind=kind,
            path=self._path(kind, artifact_id),
            size_bytes=0,
        )
        return ArtifactWriter(ref, metadata)

    async def save_text(
        self, kind: str, text: str, *, metadata: dict | None = None
    ) -> ArtifactRef:
        async with self.open_writer(kind, metadata=metadata) as writer:
            await writer.write(text.encode("utf-8"))
            return writer.ref

    async def load_text(self, uri: str, *, max_bytes: int | None = None) -> str:
        kind, artifact_id = ArtifactRef.parse_uri(uri)
        path = self._path(kind, artifact_id)
        if not path.exists():
            raise FileNotFoundError(f"artifact 不存在: {uri}")
        return await asyncio.to_thread(self._read, path, max_bytes)

    @staticmethod
    def _read(path: Path, max_bytes: int | None) -> str:
        with path.open("rb") as handle:
            data = handle.read() if max_bytes is None else handle.read(max_bytes)
        return data.decode("utf-8", errors="replace")
