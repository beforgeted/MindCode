"""Phase 7c：LocalExecutor 加固 —— env 过滤 + 进程树终止。"""

from __future__ import annotations

import asyncio
import os
import sys

import pytest

from codeagent.evidence.artifact_store import FileArtifactStore
from codeagent.infra.cancellation import CancellationToken
from codeagent.tool.executor import LocalExecutor, filtered_env


def test_filtered_env_drops_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-leak")
    monkeypatch.setenv("MC_SECRET", "nope")
    env = filtered_env()
    assert "ANTHROPIC_API_KEY" not in env
    assert "MC_SECRET" not in env
    assert "PATH" in env  # 白名单变量保留


async def test_executor_child_env_has_no_secret(
    artifact_store: FileArtifactStore, workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MC_SECRET", "leak-me")
    from codeagent.workspace.context import WorkspaceContext

    ws = WorkspaceContext.local(workspace)
    code = "import os,sys; sys.stdout.write('YES' if 'MC_SECRET' in os.environ else 'NO')"
    result = await LocalExecutor().run(
        command=f'"{sys.executable}" -c "{code}"',
        cwd=ws.root,
        cancellation=CancellationToken(),
        artifact_store=artifact_store,
        max_output_bytes=1 << 20,
    )
    assert result.exit_code == 0
    assert "NO" in result.content
    assert "YES" not in result.content


@pytest.mark.skipif(sys.platform == "win32", reason="进程组语义 POSIX 专属")
async def test_kill_tree_reaps_grandchild(workspace) -> None:
    """启动一个后台子进程的 shell，杀进程组后，后代进程应一并消失。"""
    from codeagent.tool.executor import _kill_tree

    proc = await asyncio.create_subprocess_shell(
        "sleep 30 & echo $! ; wait",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=str(workspace),
        start_new_session=True,
    )
    assert proc.stdout is not None
    line = await asyncio.wait_for(proc.stdout.readline(), timeout=5)
    child_pid = int(line.strip())
    _kill_tree(proc)
    await asyncio.sleep(0.3)
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)  # 后台孙子进程已被随组杀掉
