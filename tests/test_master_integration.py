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
async def test_overlapping_sibling_detected_stale_keeps_base_clean(tmp_path: Path):
    """两个并行兄弟写同一个文件；关掉重跑预算时,过期兄弟被判 stale→失败,base 保持干净。

    验证：先集成的 a 落地,b 因读写重叠被判过期(不 merge 过期分支);base 只含 a、无
    MERGE_HEAD/冲突标记;integrated=False。这条也守住"绝不把坏 base 留下"的下限。
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    # 关掉重跑与 replan：让过期成为终态,便于稳定断言 base 一致性。
    config = _config(repo)
    config = replace(
        config,
        profile=replace(config.profile, master_max_replans=0, agent_max_reruns=0),
    )

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

    assert final.scheduler is not None
    assert final.scheduler.integrated == {"a"}  # 只有 a 落地
    assert "b" in final.scheduler.failed  # b 过期且无重跑预算 → 失败
    assert final.integrated is False
    # base 必须干净——没有半完成的 merge，没有冲突标记。
    assert not (repo / ".git" / "MERGE_HEAD").exists()
    porcelain = _status_porcelain(repo)
    assert "UU" not in porcelain and "AA" not in porcelain
    content = (repo / "shared.txt").read_text(encoding="utf-8")
    assert "<<<<<<<" not in content
    assert content == "AAA"


@pytest.mark.skipif(not _HAS_GIT, reason="git 不可用")
async def test_stale_sibling_reruns_and_converges(tmp_path: Path):
    """过期兄弟在最新基线上**自动重跑并收敛**（Phase 2 主路径,用户无感）。

    a 先写 shared.txt=AAA 集成;b 首次基于旧 base 写 BBB → 检测到读写重叠 → 判过期 →
    在含 AAA 的最新 HEAD 上重跑,重跑再写 BBB → 干净并回。最终 a、b 都 integrated,
    base = BBB(b 在 a 之上重跑覆盖),无冲突、无需人工。
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    config = _config(repo)  # agent_max_reruns 默认 2

    client = StubLlmClient(
        [
            [("write_file", {"path": "shared.txt", "content": "AAA"})],
            "done A",
            [("write_file", {"path": "shared.txt", "content": "BBB"})],
            "done B",
            # b 过期重跑：在含 AAA 的最新 base 上再写一次 BBB
            [("write_file", {"path": "shared.txt", "content": "BBB"})],
            "done B rerun",
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

    assert final.scheduler is not None
    assert final.scheduler.integrated == {"a", "b"}  # 都收敛落地
    assert final.integrated is True
    assert final.merge_conflicts == ()
    assert not (repo / ".git" / "MERGE_HEAD").exists()
    content = (repo / "shared.txt").read_text(encoding="utf-8")
    assert "<<<<<<<" not in content
    assert content == "BBB"  # b 在 a 之上重跑,覆盖为 BBB
