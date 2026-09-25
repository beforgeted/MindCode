from __future__ import annotations

import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from codeagent.config import AppConfig
from codeagent.context.profile import ContextProfile
from codeagent.llm.stub_client import StubLlmClient
from codeagent.orchestration.master_session import build_master
from codeagent.orchestration.planner import StaticPlanner
from codeagent.orchestration.task_graph import Step, TaskGraph
from codeagent.session import AgentSession

_HAS_GIT = shutil.which("git") is not None


def _init_repo(root: Path) -> None:
    def run(*args):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)

    run("init")
    run("config", "user.email", "t@t.com")
    run("config", "user.name", "t")
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    run("add", ".")
    run("commit", "-m", "init")


def _worktree_count(repo: Path) -> int:
    out = subprocess.run(
        ["git", "-C", str(repo), "worktree", "list"], capture_output=True, text=True
    ).stdout.strip()
    return len(out.splitlines())


def _config(repo: Path) -> AppConfig:
    return AppConfig(
        workspace_root=repo,
        home=repo / ".home",
        project_id="p",
        project_root=repo / ".home",
        model="stub",
        profile=replace(ContextProfile(), context_window=20_000, agent_max_concurrency=1),
        use_stub_llm=True,
    )


@pytest.mark.skipif(not _HAS_GIT, reason="git 不可用")
async def test_two_workers_write_in_worktrees_and_merge_back(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    config = _config(repo)

    # 串行（agent_max_concurrency=1）保证 stub 脚本顺序确定：
    # worker A → 写 a.txt → 收尾；worker B → 写 b.txt → 收尾。
    client = StubLlmClient(
        [
            [("write_file", {"path": "a.txt", "content": "AAA"})],
            "done A",
            [("write_file", {"path": "b.txt", "content": "BBB"})],
            "done B",
        ]
    )
    graph = TaskGraph(
        [Step("a", "default", "写 a.txt"), Step("b", "default", "写 b.txt")]
    )

    async with AgentSession(config, llm_client=client) as session:
        master = await build_master(
            config=config,
            llm_client=client,
            engine=session.engine,
            event_store=session.event_store,
            metrics=session.metrics,
            definition=session.definition,
            planner=StaticPlanner(graph),
        )
        final = await master.run("写两个文件", session_id=session.session_id)

    assert final.accepted
    assert final.scheduler is not None
    assert final.scheduler.completed == {"a", "b"}
    # 两个 Worker 在各自 worktree 里的改动都被提交并合并回了 base。
    assert (repo / "a.txt").read_text(encoding="utf-8") == "AAA"
    assert (repo / "b.txt").read_text(encoding="utf-8") == "BBB"
    assert len(final.merged_branches) == 2
    assert final.merge_conflicts == ()
    # worktree 已清理，只剩主工作树。
    assert _worktree_count(repo) == 1
