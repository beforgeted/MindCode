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


def _status_porcelain(repo: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"], capture_output=True, text=True
    ).stdout


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
    assert final.integrated
    # worktree 已清理，只剩主工作树。
    assert _worktree_count(repo) == 1


@pytest.mark.skipif(not _HAS_GIT, reason="git 不可用")
async def test_merge_conflict_aborts_and_keeps_base_clean(tmp_path: Path):
    """两个 Worker 写同一个文件 → 第二个分支合并冲突。

    修复点：冲突必须 `git merge --abort` 回滚，base 不能留在半完成 merge 状态
    （MERGE_HEAD / 冲突标记）；且 accepted 可能为 True，但 integrated 必须为 False。
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    config = _config(repo)  # agent_max_concurrency=1，串行确定顺序

    client = StubLlmClient(
        [
            [("write_file", {"path": "shared.txt", "content": "AAA"})],
            "done A",
            [("write_file", {"path": "shared.txt", "content": "BBB"})],
            "done B",
        ]
    )
    graph = TaskGraph(
        [Step("a", "default", "写 shared.txt"), Step("b", "default", "再写 shared.txt")]
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
        final = await master.run("写两次同一个文件", session_id=session.session_id)

    # 第一个分支合并成功，第二个冲突。
    assert len(final.merged_branches) == 1
    assert len(final.merge_conflicts) == 1
    # 关键：验收通过但未集成。
    assert final.integrated is False
    # base 必须干净——没有半完成的 merge，没有冲突标记。
    assert not (repo / ".git" / "MERGE_HEAD").exists()
    porcelain = _status_porcelain(repo)
    assert "UU" not in porcelain and "AA" not in porcelain
    content = (repo / "shared.txt").read_text(encoding="utf-8")
    assert "<<<<<<<" not in content
    assert content == "AAA"  # 停在第一个分支合并后的一致状态
