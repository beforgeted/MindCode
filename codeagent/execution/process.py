"""可信控制面进程调用：固定 argv、有界输出、超时/取消回收客户端进程。

这不负责杀容器进程；调用方必须在 Podman exec 失败/取消后关闭整个执行域。
"""
from __future__ import annotations

import asyncio
import os
from collections.abc import Sequence
from typing import Any

from codeagent.execution.models import ProcessOutput, SandboxError
from codeagent.infra.cancellation import CancellationToken, CancelledByUser

_ENV_KEYS = frozenset({
    "PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE",
    "XDG_RUNTIME_DIR", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "DBUS_SESSION_BUS_ADDRESS",
    "SYSTEMROOT", "SystemRoot", "WINDIR", "COMSPEC", "ComSpec", "TEMP", "TMP",
})


async def run_bounded(
    argv: Sequence[str], *, data: bytes | None = None, timeout_seconds: float = 30,
    max_bytes: int = 4 * 1024 * 1024, cancellation: CancellationToken | None = None,
    pass_fds: tuple[int, ...] = (),
) -> ProcessOutput:
    if timeout_seconds <= 0 or max_bytes <= 0:
        raise ValueError("timeout/max_bytes 必须为正数")
    if cancellation is not None:
        cancellation.raise_if_cancelled()
    subprocess_options: dict[str, Any] = {"pass_fds": pass_fds} if pass_fds else {}
    proc = await asyncio.create_subprocess_exec(
        *argv, stdin=asyncio.subprocess.PIPE if data is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env={key: value for key, value in os.environ.items() if key in _ENV_KEYS},
        **subprocess_options,
    )
    size = 0

    async def read(stream: asyncio.StreamReader | None) -> bytes:
        nonlocal size
        assert stream is not None
        chunks = []
        while chunk := await stream.read(65536):
            size += len(chunk)
            if size > max_bytes:
                raise SandboxError("执行输出超出字节上限")
            chunks.append(chunk)
        return b"".join(chunks)

    async def feed() -> None:
        if proc.stdin is not None:
            try:
                proc.stdin.write(data or b"")
                await proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                proc.stdin.close()

    readers = [asyncio.create_task(read(proc.stdout)), asyncio.create_task(read(proc.stderr))]
    feeder = asyncio.create_task(feed())
    exited = asyncio.create_task(proc.wait())
    result = asyncio.gather(readers[0], readers[1], feeder, exited)
    cancelled = asyncio.create_task(cancellation.wait()) if cancellation is not None else None
    try:
        watched: set[asyncio.Future[Any]] = {result}
        if cancelled is not None:
            watched.add(cancelled)
        done, _ = await asyncio.wait(watched, timeout=timeout_seconds,
                                     return_when=asyncio.FIRST_COMPLETED)
        if cancelled is not None and cancelled in done:
            raise CancelledByUser("执行域调用已取消")
        if result not in done:
            raise TimeoutError("执行域调用超时")
        await result
        return ProcessOutput(proc.returncode or 0, readers[0].result(), readers[1].result())
    finally:
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        for task in (*readers, feeder, exited, cancelled):
            if task is not None and not task.done():
                task.cancel()
        if not result.done():
            result.cancel()
        finished: list[asyncio.Future[Any]] = [result, *readers, feeder, exited]
        if cancelled is not None:
            finished.append(cancelled)
        await asyncio.gather(*finished, return_exceptions=True)
        try:
            await asyncio.wait_for(proc.wait(), timeout=3)
        except TimeoutError:
            # 客户端后代可能仍持有管道；容器调用方继续做执行域级回收。
            pass
