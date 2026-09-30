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


@pytest.mark.skipif(not _HAS_GIT, reason="git 不可用")
@pytest.mark.parametrize("window", ["before_promote", "after_promote", "save_failure", "reject"])
async def test_deferred_manifest_survives_promote_crash(tmp_path: Path, window, monkeypatch):
    from codeagent.evidence.artifact_store import FileArtifactStore
    from codeagent.orchestration.global_verifier import GlobalVerdict, NoFailureVerifier
    from codeagent.orchestration.run_store import SqliteRunStore
    from codeagent.tool.approval import AllowExternalApprovalPolicy
    from codeagent.tool.deferred import DeferredAction
    from codeagent.tool.effects import EffectKind, RetryPolicy

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    config = _config(repo)
    store = SqliteRunStore(repo / ".home" / "runs.db")
    await store.start()
    head_before = _git_out(repo, "rev-parse", "HEAD")
    action = DeferredAction("echo once >> effect.txt", EffectKind.EXTERNAL_SIDE_EFFECT,
                            RetryPolicy.NEVER)
    saved_ids = []
    save = store.save_deferred

    async def save_manifest(run_id, attempt_no, actions):
        saved_ids.append(run_id)
        if window == "save_failure":
            raise RuntimeError("manifest failure")
        await save(run_id, attempt_no, actions)

    monkeypatch.setattr(store, "save_deferred", save_manifest)
    client = StubLlmClient([[('write_file', {"path": "a.txt", "content": "A"})], "done"])

    class RejectVerifier:
        async def verify(self, task, graph, results, target=None):
            return GlobalVerdict(accept=False, reason="reject")

    async with AgentSession(config, llm_client=client) as session:
        master = await build_master(
            config=config, llm_client=client, engine=session.engine,
            event_store=session.event_store, metrics=session.metrics,
            definition=session.definition,
            planner=StaticPlanner(TaskGraph([Step("a", "default", "write a.txt")])),
            global_verifier=RejectVerifier() if window == "reject" else NoFailureVerifier(),
            run_store=store, artifact_store=FileArtifactStore(repo / ".home"),
        )
        master._max_replans = 0
        master._approval = AllowExternalApprovalPolicy()
        schedule = master._scheduler.run

        async def with_action(*args, **kwargs):
            result = await schedule(*args, **kwargs)
            next(iter(result.workers.values())).run.deferred_actions.append(action)
            return result

        monkeypatch.setattr(master._scheduler, "run", with_action)
        promote = master._wsm.promote  # type: ignore[attr-defined]

        async def crashing_promote(*args, **kwargs):
            assert saved_ids and await store.load_deferred(saved_ids[0], 1)
            if window == "after_promote":
                assert await promote(*args, **kwargs)
            raise RuntimeError("promote crash")

        monkeypatch.setattr(master._wsm, "promote", crashing_promote)
        if window == "reject":
            result = await master.run("write a", session_id=session.session_id)
            assert not result.integrated and not saved_ids
        else:
            with pytest.raises(RuntimeError):
                await master.run("write a", session_id=session.session_id)
        assert not (repo / "effect.txt").exists()
        if window in ("reject", "save_failure"):
            assert _git_out(repo, "rev-parse", "HEAD") == head_before
            return
        monkeypatch.setattr(master._wsm, "promote", promote)
        reopened = SqliteRunStore(repo / ".home" / "runs.db")
        await reopened.start()
        master._run_store = reopened
        for _ in range(2):
            result = await master.run("", session_id=session.session_id,
                                      resume_master_run_id=saved_ids[0])
            assert result.integrated and result.deferred_executed == 1
        assert (repo / "effect.txt").read_text().split() == ["once"]
        assert (repo / "a.txt").read_text() == "A"


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


def _git_out(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True
    ).stdout.strip()


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
async def test_base_stale_and_replan_budgets_are_separate(tmp_path: Path):
    """P8e：BASE_STALE 消耗 promote 重试预算，reject 消耗 replan 预算，互不侵占。"""
    from codeagent.orchestration.global_verifier import GlobalVerdict

    async def _run_case(*, reject: bool, promote_fails: bool) -> dict:
        repo = tmp_path / ("rej" if reject else "stale")
        repo.mkdir()
        _init_repo(repo)
        base_cfg = _config(repo)
        profile = replace(
            base_cfg.profile, master_max_replans=1, promote_max_retries=1, agent_max_concurrency=1
        )
        config = replace(base_cfg, profile=profile)
        client = StubLlmClient(
            [[("write_file", {"path": "a.txt", "content": "AAA"})], "done"] * 6
        )
        graph = TaskGraph([Step("a", "default", "写 a.txt")])

        class _RejectVerifier:
            async def verify(self, task, graph, results, target=None) -> GlobalVerdict:
                return GlobalVerdict(accept=False, reason="不接受", replan_instruction="重来")

        async with AgentSession(config, llm_client=client) as session:
            master = await build_master(
                config=config, llm_client=client, engine=session.engine,
                event_store=session.event_store, metrics=session.metrics,
                definition=session.definition, planner=StaticPlanner(graph),
                global_verifier=_RejectVerifier() if reject else None,
            )
            if promote_fails:
                async def _never(candidate_sha, *, expected_base):
                    return False
                master._wsm.promote = _never  # type: ignore[attr-defined]
            final = await master.run("写 a", session_id=session.session_id)
            counters = session.metrics.snapshot()["counters"]
        return {"final": final, "counters": counters}

    # reject 路径：吃 replan 预算，不动 promote 预算
    rej = await _run_case(reject=True, promote_fails=False)
    assert not rej["final"].integrated
    assert rej["counters"].get("master.replans", 0) == 1
    assert rej["counters"].get("master.promote_retries", 0) == 0

    # BASE_STALE 路径：吃 promote 预算，不动 replan 预算
    stale = await _run_case(reject=False, promote_fails=True)
    assert not stale["final"].integrated
    assert stale["counters"].get("master.promote_retries", 0) == 1
    assert stale["counters"].get("master.replans", 0) == 0
    assert "promote 重试预算" in stale["final"].reason


@pytest.mark.skipif(not _HAS_GIT, reason="git 不可用")
async def test_reclaim_orphans_removes_leftover_worktrees_and_branches(tmp_path: Path):
    """P8d：崩溃遗留的 candidate/worker worktree 与分支应被回收，只剩主工作树。"""
    from codeagent.workspace.git_worktree import GitWorktreeWorkspaceManager

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    base = _git_out(repo, "rev-parse", "HEAD")
    wsm = GitWorktreeWorkspaceManager(repo, repo / ".home" / "wt")

    cand = await wsm.create_candidate(base)
    await wsm.create("run_orphan", base_ref=cand.branch_name)
    assert _worktree_count(repo) == 3  # main + candidate + worker

    removed = await wsm.reclaim_orphans(keep_branches=set())

    assert removed == 2
    assert _worktree_count(repo) == 1  # 只剩主工作树
    branches = _git_out(repo, "branch", "--list", "codeagent/*")
    assert branches == ""  # codeagent/* 分支全部清除


@pytest.mark.skipif(not _HAS_GIT, reason="git 不可用")
async def test_recover_promote_succeeded_but_state_lost_is_idempotent(tmp_path: Path):
    """P8c④：promote 已成功但状态未落库 → resume 识别为已完成，绝不重复推进 base。"""
    from codeagent.orchestration.run_store import AttemptState, SqliteRunStore

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    config = _config(repo)
    store = SqliteRunStore(repo / ".home" / "runs.db")
    await store.start()

    def _head() -> str:
        return subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True
        ).stdout.strip()

    client = StubLlmClient([[("write_file", {"path": "a.txt", "content": "AAA"})], "done A"])
    graph = TaskGraph([Step("a", "default", "写 a.txt")])

    async with AgentSession(config, llm_client=client) as session:
        master = await build_master(
            config=config, llm_client=client, engine=session.engine,
            event_store=session.event_store, metrics=session.metrics,
            definition=session.definition, planner=StaticPlanner(graph), run_store=store,
        )
        final = await master.run("写 a", session_id=session.session_id)
        assert final.integrated
        mrun = final.master_run_id
        head_after_promote = _head()
        record = await store.load_run(mrun)
        assert record is not None and record.promoted_sha == head_after_promote
        last_no = record.last_attempt.attempt_no  # type: ignore[union-attr]

        # 伪造"promote 成功但状态未落库"：把 attempt 退回 PROMOTING、run 退回 running。
        await store.update_attempt(
            mrun, last_no, state=AttemptState.PROMOTING, candidate_sha=head_after_promote
        )
        await store.update_run_status(mrun, "running")

        final2 = await master.run("写 a", session_id=session.session_id, resume_master_run_id=mrun)

    assert final2.integrated
    assert "未重推" in final2.reason
    assert _head() == head_after_promote  # base 未被二次推进
    assert (repo / "a.txt").read_text(encoding="utf-8") == "AAA"
    assert _worktree_count(repo) == 1
    record2 = await store.load_run(mrun)
    assert record2 is not None and record2.status == "success"


@pytest.mark.skipif(not _HAS_GIT, reason="git 不可用")
async def test_attempt_state_sequence_persisted_on_success(tmp_path: Path):
    """P8b：成功一次跑完后，attempt 记录应到达 PROMOTED，run 记 promoted_sha。"""
    from codeagent.orchestration.run_store import AttemptState, SqliteRunStore

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    config = _config(repo)
    store = SqliteRunStore(repo / ".home" / "runs.db")
    await store.start()
    client = StubLlmClient([[("write_file", {"path": "a.txt", "content": "AAA"})], "done A"])
    graph = TaskGraph([Step("a", "default", "写 a.txt")])

    async with AgentSession(config, llm_client=client) as session:
        master = await build_master(
            config=config, llm_client=client, engine=session.engine,
            event_store=session.event_store, metrics=session.metrics,
            definition=session.definition, planner=StaticPlanner(graph), run_store=store,
        )
        final = await master.run("写 a", session_id=session.session_id)

    assert final.integrated
    record = await store.load_run(final.master_run_id)
    assert record is not None
    assert record.status == "success"
    assert record.original_base_sha is not None
    assert record.promoted_sha is not None
    last = record.last_attempt
    assert last is not None
    assert last.state == AttemptState.PROMOTED
    assert last.candidate_sha == record.promoted_sha


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
    """两个并行兄弟写同一文件;关掉重跑预算时过期兄弟失败 → 整个 Attempt 丢弃。

    事务语义:reject/失败 → candidate 不 promote,真实 base **完全不变**(shared.txt 都不存在),
    绝不留坏 base、也不半推。
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    config = _config(repo)
    config = replace(
        config,
        profile=replace(
            config.profile,
            master_max_replans=0,
            agent_max_reruns=0,
            agent_max_integrations=0,
        ),
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

    def _head() -> str:
        return subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True
        ).stdout.strip()

    original = _head()
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
    assert "b" in final.scheduler.failed  # b 过期且无重跑预算 → 失败
    assert final.integrated is False
    # 事务全有或全无:真实 base 完全没变,shared.txt 根本没落地。
    assert _head() == original
    assert not (repo / "shared.txt").exists()
    assert not (repo / ".git" / "MERGE_HEAD").exists()


@pytest.mark.skipif(not _HAS_GIT, reason="git 不可用")
async def test_verifier_indeterminate_does_not_promote(tmp_path: Path):
    """提交门禁 fail-closed：验证器返回 indeterminate（不可用/不可解析）时,绝不推进真实 base。"""
    from codeagent.orchestration.global_verifier import GlobalVerdict

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    config = _config(repo)
    config = replace(config, profile=replace(config.profile, master_max_replans=0))

    class IndeterminateVerifier:
        async def verify(self, task, graph, results, target=None) -> GlobalVerdict:
            return GlobalVerdict(accept=False, indeterminate=True, reason="verifier down")

    client = StubLlmClient(
        [[("write_file", {"path": "x.txt", "content": "hi"})], "done"]
    )
    graph = TaskGraph([Step("a", "default", "写 x.txt")])

    def _head() -> str:
        return subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True
        ).stdout.strip()

    original = _head()
    async with AgentSession(config, llm_client=client) as session:
        master = await build_master(
            config=config,
            llm_client=client,
            engine=session.engine,
            event_store=session.event_store,
            metrics=session.metrics,
            definition=session.definition,
            planner=StaticPlanner(graph),
            global_verifier=IndeterminateVerifier(),
        )
        final = await master.run("写 x.txt", session_id=session.session_id)

    assert final.integrated is False  # 未推进
    assert _head() == original  # 真实 base 完全没变
    assert not (repo / "x.txt").exists()


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
