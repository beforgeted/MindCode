"""CommandExecutor：把子进程执行从 run_command 工具里抽出去（Phase 7c）。

工具只负责"分类 + 守卫 + 组装结果"；真正 spawn/流式/杀进程的机制在这里，可替换：
- LocalExecutor：本机执行，边读边落 artifact，加固（进程树终止 / env 过滤 / 输出上限）。
- SandboxExecutor：绑定当前 Worker 的 Podman 执行域，不运行宿主 shell。
- ValidationExecutor 语义已由 git_worktree.run_check 承担（在冻结 candidate 上跑验收）。

加固要点：
- **进程树终止**：POSIX 用 start_new_session（独立进程组）+ killpg；Windows 用
  CREATE_NEW_PROCESS_GROUP + taskkill /T。取消/超时杀**整棵子孙进程**，而非仅 shell 父。
- **env 过滤**：默认只透传最小白名单（PATH/HOME/LANG…），绝不把 ANTHROPIC_API_KEY 等
  敏感变量交给模型给出的命令。
- 输出/字节上限、工作目录限定沿用既有实现。
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from codeagent.evidence.artifact_store import ArtifactStore
from codeagent.evidence.models import ArtifactRef
from codeagent.execution.models import ProcessOutput, SandboxHandle
from codeagent.execution.podman import PodmanSandboxManager
from codeagent.infra.cancellation import CancellationToken, CancelledByUser
from codeagent.infra.text import TRUNCATION_MARKER
from codeagent.knowledge.state import KnowledgeState

_CHUNK = 64 * 1024
_PREVIEW_BYTES = 128 * 1024

# 透传给子进程的环境变量白名单（跨平台）。其余一律不传，避免泄露密钥。
_ENV_ALLOW = frozenset(
    {
        "PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TERM", "TMPDIR", "TMP", "TEMP",
        "USER", "LOGNAME", "SHELL", "PWD",
        # Windows 必需
        "SYSTEMROOT", "SystemRoot", "COMSPEC", "ComSpec", "PATHEXT", "WINDIR",
        "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "APPDATA", "LOCALAPPDATA",
    }
)


@dataclass(frozen=True, slots=True)
class CommandResult:
    exit_code: int
    content: str  # 有界预览（head + 截断标记 + tail）
    artifact: ArtifactRef | None
    total_bytes: int
    capped: bool
    truncated: bool


def filtered_env(extra_allow: frozenset[str] = frozenset()) -> dict[str, str]:
    allow = _ENV_ALLOW | extra_allow
    return {k: v for k, v in os.environ.items() if k in allow}


@runtime_checkable
class CommandExecutor(Protocol):
    async def run(
        self,
        *,
        command: str,
        cwd: Path,
        cancellation: CancellationToken,
        artifact_store: ArtifactStore,
        max_output_bytes: int,
        env: Mapping[str, str] | None = None,
        metadata: dict[str, str] | None = None,
    ) -> CommandResult: ...


class LocalExecutor:
    """本机执行 + 加固。流式落 artifact，取消/超时杀整棵进程树。"""

    def __init__(self) -> None:
        self.knowledge = KnowledgeState()

    async def run(
        self,
        *,
        command: str,
        cwd: Path,
        cancellation: CancellationToken,
        artifact_store: ArtifactStore,
        max_output_bytes: int,
        env: Mapping[str, str] | None = None,
        metadata: dict[str, str] | None = None,
    ) -> CommandResult:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(cwd),
            env=dict(env) if env is not None else filtered_env(),
            **_new_group_kwargs(),
        )
        head = bytearray()
        tail: deque[bytes] = deque()
        tail_bytes = 0
        total = 0
        capped = False
        try:
            meta = metadata or {}
            async with artifact_store.open_writer("tool-results", metadata=meta) as writer:
                assert proc.stdout is not None
                while True:
                    if cancellation.cancelled:
                        _kill_tree(proc)
                        raise CancelledByUser("run_command cancelled")
                    chunk = await proc.stdout.read(_CHUNK)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total <= max_output_bytes:
                        await writer.write(chunk)
                    else:
                        capped = True
                    if len(head) < _PREVIEW_BYTES:
                        head.extend(chunk[: _PREVIEW_BYTES - len(head)])
                    tail.append(chunk)
                    tail_bytes += len(chunk)
                    while tail_bytes - len(tail[0]) >= _PREVIEW_BYTES:
                        tail_bytes -= len(tail.popleft())
                artifact = writer.ref
            exit_code = await proc.wait()
        except (CancelledByUser, asyncio.CancelledError):
            _kill_tree(proc)
            raise

        tail_bytes_joined = b"".join(tail)
        content = _preview(bytes(head), tail_bytes_joined, total, capped, max_output_bytes)
        return CommandResult(
            exit_code=exit_code or 0,
            content=content,
            artifact=artifact,
            total_bytes=total,
            capped=capped,
            truncated=total > len(head) + len(tail_bytes_joined),
        )


class SandboxExecutor:
    """One run's executor. The host path is only a lexical cwd mapping."""

    def __init__(self, manager: PodmanSandboxManager, handle: SandboxHandle, root: Path):
        self.manager, self.handle, self.root = manager, handle, root
        self.knowledge = KnowledgeState()

    async def run(
        self, *, command: str, cwd: Path, cancellation: CancellationToken,
        artifact_store: ArtifactStore, max_output_bytes: int,
        env: Mapping[str, str] | None = None, metadata: dict[str, str] | None = None,
    ) -> CommandResult:
        if env is not None:
            raise ValueError("sandbox does not accept host environment overrides")
        relative = cwd.relative_to(self.root).as_posix()
        output = await self.manager.execute(
            self.handle, command, cwd=relative, cancellation=cancellation,
            max_output_bytes=max_output_bytes,
        )
        return await sandbox_result(output, artifact_store, metadata=metadata)


async def sandbox_result(
    output: ProcessOutput, artifacts: ArtifactStore, *, metadata: dict[str, str] | None = None,
) -> CommandResult:
    """Podman already bounds the combined byte stream before returning it."""
    data = output.stdout + output.stderr
    async with artifacts.open_writer("tool-results", metadata=metadata) as writer:
        await writer.write(data)
        ref = writer.ref
    truncated = len(data) > _PREVIEW_BYTES
    preview = data
    if truncated:
        half = _PREVIEW_BYTES // 2
        preview = data[:half] + TRUNCATION_MARKER.encode() + data[-half:]
    return CommandResult(
        output.returncode, preview.decode("utf-8", errors="replace"), ref,
        len(data), False, truncated,
    )


def _new_group_kwargs() -> dict[str, Any]:
    """让子进程成为新进程组/会话的首领，便于按组杀掉整棵子孙。"""
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def _kill_tree(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    pid = proc.pid
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True,
                check=False,
            )
        else:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass


def _preview(head: bytes, tail: bytes, total: int, capped: bool, cap: int) -> str:
    notes = []
    if capped:
        notes.append(f"[输出超过 {cap} 字节上限，artifact 只保存了前 {cap} 字节]")
    if total <= len(head):
        body = head.decode("utf-8", errors="replace")
    else:
        omitted = total - len(head) - len(tail)
        body = (
            head.decode("utf-8", errors="replace")
            + f"\n{TRUNCATION_MARKER.format(omitted=max(0, omitted))}\n"
            + tail.decode("utf-8", errors="replace")
        )
    return ("\n".join(notes) + "\n" + body) if notes else body


__all__ = ["CommandExecutor", "CommandResult", "LocalExecutor", "SandboxExecutor", "filtered_env"]
