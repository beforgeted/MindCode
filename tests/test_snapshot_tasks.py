"""Non-Git task transaction contracts; in-memory domains are not isolation evidence."""
from __future__ import annotations

import asyncio
import sqlite3
import sys
from dataclasses import replace

import pytest

from codeagent.agent.models import FileChangeKind
from codeagent.execution.models import ExecutionPurpose, SandboxError
from codeagent.execution.publication import PublicationUncertain
from codeagent.llm.stub_client import StubLlmClient
from codeagent.orchestration.global_verifier import GlobalVerdict
from codeagent.orchestration.master_session import MasterSession
from codeagent.orchestration.planner import StaticPlanner
from codeagent.orchestration.run_store import AttemptState
from codeagent.orchestration.task_graph import Step, TaskGraph
from codeagent.workspace.snapshot import SnapshotWorkspaceManager
from tests.test_master_integration import _config
from tests.test_sandbox_wiring import MemorySandbox

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="POSIX task snapshots")


@pytest.fixture
def task(tmp_path, monkeypatch):
    return make_task_fixture(tmp_path, monkeypatch)


def make_task_fixture(tmp_path, monkeypatch):
    root = tmp_path / "plain"
    root.mkdir()
    (root / "seed.txt").write_text("user")
    (root / ".env").write_text("private")
    (root / ".venv").mkdir()
    (root / ".venv" / "python").symlink_to(sys.executable)
    config = replace(_config(root), home=tmp_path / "state", project_root=tmp_path / "state",
                     execution_backend="podman", sandbox_image="a" * 64, verify_command="check")
    config = replace(config, profile=replace(config.profile, master_max_replans=0,
                                            promote_max_retries=0))
    manager = MemorySandbox()
    monkeypatch.setattr("codeagent.orchestration.master_session.PodmanSandboxManager",
                        lambda *args, **kwargs: manager)
    return root, config, manager


def planner():
    return StaticPlanner(TaskGraph([Step("first", "default", "modify seed"),
                                   Step("second", "default", "read modified seed and create note",
                                        dependencies=frozenset({"first"}))]))


def client():
    return StubLlmClient([
        [("write_file", {"path": "seed.txt", "content": "candidate"})], "first done",
        [("read_file", {"path": "seed.txt"}),
         ("write_file", {"path": "note.txt", "content": "accepted"})], "second done",
    ])


@pytest.mark.parametrize("accept", [True, False])
async def test_non_git_task_acceptance_is_whole_candidate(task, accept):
    root, config, manager = task
    manager.validation_exit = 0 if accept else 1
    async with MasterSession(config, llm_client=client(), planner=planner()) as session:
        result = await session.run_task("edit plain project")
        assert result.accepted is accept and result.integrated is accept
        assert result.scheduler is not None
        second = result.scheduler.workers["second"]
        read = second.run.context.tool_runs[0].result
        assert read is not None and read.content == "candidate"
        for worker in result.scheduler.workers.values():
            assert worker.workspace.is_isolated and worker.workspace.root != root
            assert not worker.workspace.root.exists()
        assert not result.merged_branches
        assert not manager.domains
        assert ("command", ExecutionPurpose.VALIDATION) in manager.calls
        assert session.master is not None
        record = await session.master._run_store.load_run(result.master_run_id)
        assert record is not None and record.last_attempt is not None
        assert record.last_attempt.state == (AttemptState.PROMOTED if accept else
                                             AttemptState.DISCARDED)
        if accept:
            assert {f.path: f.change for f in result.files} == {
                "seed.txt": FileChangeKind.MODIFIED, "note.txt": FileChangeKind.CREATED,
            }
        else:
            assert not result.files
    assert (root / "seed.txt").read_text() == ("candidate" if accept else "user")
    assert (root / "note.txt").exists() is accept
    assert (root / ".env").read_text() == "private"
    assert (root / ".venv" / "python").is_symlink()
    assert not (root / ".git").exists()


@pytest.mark.parametrize("failure", ["no_check", "seal", "worker", "validator_close"])
async def test_task_failures_never_accept_partial_candidate(task, failure, monkeypatch):
    root, config, manager = task
    if failure == "no_check":
        config = replace(config, verify_command=None)
    manager.fail_seal = failure == "seal"
    close = manager.close

    async def fail_close(handle):
        await close(handle)
        if failure == "validator_close" and handle.purpose == ExecutionPurpose.VALIDATION:
            raise SandboxError("validation destruction uncertain")

    monkeypatch.setattr(manager, "close", fail_close)
    scripted = client()
    if failure == "worker":
        def explode(messages):
            raise RuntimeError("second worker failed")
        scripted = StubLlmClient([
            [("write_file", {"path": "seed.txt", "content": "candidate"})], "first done",
            explode,
        ])
    async with MasterSession(config, llm_client=scripted, planner=planner()) as session:
        result = await session.run_task("edit")
        assert not result.integrated and not result.files and not manager.domains
    assert (root / "seed.txt").read_text() == "user"
    assert not (root / "note.txt").exists()


async def test_rejected_attempt_restarts_from_original_snapshot(task, monkeypatch):
    root, config, manager = task
    config = replace(config, profile=replace(config.profile, master_max_replans=1))
    execute = manager.execute

    async def append(handle, command, **kwargs):
        if handle.purpose == ExecutionPurpose.WORKER:
            manager.domains[handle.container_id]["seed.txt"] += b"X"
        return await execute(handle, command, **kwargs)

    monkeypatch.setattr(manager, "execute", append)

    class RejectOnce:
        calls = 0

        async def verify(self, task, graph, results, target=None):
            self.calls += 1
            return GlobalVerdict(accept=self.calls > 1, reason="retry")

    script = StubLlmClient([
        [("run_command", {"command": "printf X >> seed.txt"})], "done",
        [("run_command", {"command": "printf X >> seed.txt"})], "done",
    ])
    async with MasterSession(
        config, llm_client=script,
        planner=StaticPlanner(TaskGraph([Step("one", "default", "append")])),
        global_verifier=RejectOnce(),
    ) as session:
        result = await session.run_task("append once")
        assert result.integrated and result.replans == 1
    assert (root / "seed.txt").read_text() == "userX"


async def test_concurrent_editor_change_is_preserved(task, monkeypatch):
    root, config, manager = task
    execute = manager.execute

    async def edit(handle, command, **kwargs):
        if handle.purpose == ExecutionPurpose.VALIDATION:
            (root / "seed.txt").write_text("editor")
        return await execute(handle, command, **kwargs)

    monkeypatch.setattr(manager, "execute", edit)
    async with MasterSession(config, llm_client=client(), planner=planner()) as session:
        result = await session.run_task("edit")
        assert not result.integrated and not result.files
    assert (root / "seed.txt").read_text() == "editor"
    assert not (root / "note.txt").exists()


async def test_readonly_task_does_not_require_check(task):
    root, config, manager = task
    config = replace(config, verify_command=None)
    async with MasterSession(
        config, llm_client=StubLlmClient([[("read_file", {"path": "seed.txt"})], "read"]),
        planner=StaticPlanner(TaskGraph([Step("one", "default", "read", read_only=True)])),
    ) as session:
        result = await session.run_task("read")
        assert result.integrated and not result.files
        assert ("command", ExecutionPurpose.VALIDATION) not in manager.calls
    assert (root / "seed.txt").read_text() == "user"


@pytest.mark.parametrize("reason", ["overlap", "unknown", "disjoint"])
async def test_parallel_worker_snapshot_staleness(task, reason):
    root, config, manager = task
    wsm = SnapshotWorkspaceManager(root, config.state_root, limits=manager.snapshot_limits)
    wsm.begin()
    try:
        candidate = await wsm.create_candidate(await wsm.base_revision())
        a = await wsm.create("a", base_ref=candidate.branch_name)
        b = await wsm.create("b", base_ref=candidate.branch_name)
        (a.root / "seed.txt").write_text("A")
        (b.root / "note.txt").write_text("B")
        assert (await wsm.integrate(a, candidate, read_set=set(), reads_unknown=False))[0] == \
            "integrated"
        status, _ = await wsm.integrate(
            b, candidate, read_set={"seed.txt"} if reason == "overlap" else set(),
            reads_unknown=reason == "unknown",
        )
        assert status == ("integrated" if reason == "disjoint" else "stale")
        assert (candidate.root / "note.txt").exists() is (reason == "disjoint")
        assert (root / "seed.txt").read_text() == "user"
    finally:
        wsm.end()


async def test_resume_receipt_preserves_success_record(task):
    _, config, _ = task
    async with MasterSession(config, llm_client=client(), planner=planner()) as session:
        result = await session.run_task("edit")
        assert result.integrated and session.master is not None
        before = await session.master._run_store.load_run(result.master_run_id)
        recovered = await session.master.run(
            "resume", session_id=session.session.session_id,
            resume_master_run_id=result.master_run_id,
        )
        assert recovered.integrated and recovered.scheduler is None
        after = await session.master._run_store.load_run(result.master_run_id)
        assert before is not None and after is not None
        assert after.status == before.status == "success"
        assert after.attempts == before.attempts and after.outcomes == before.outcomes
        assert after.original_base_sha == before.original_base_sha
        assert after.promoted_sha == before.promoted_sha
        assert after.graph.steps == before.graph.steps


async def test_publication_unknown_is_not_reported_rolled_back(task, monkeypatch):
    root, config, _ = task

    def uncertain(*args, **kwargs):
        raise PublicationUncertain("test uncertain result")

    monkeypatch.setattr("codeagent.execution.publication.WorkspacePublication.publish", uncertain)
    async with MasterSession(config, llm_client=client(), planner=planner()) as session:
        result = await session.run_task("edit")
        assert not result.integrated and "待核对" in result.reason and not result.files
        assert session.master is not None
        record = await session.master._run_store.load_run(result.master_run_id)
        assert record is not None and record.status == "unknown"
    assert (root / "seed.txt").read_text() == "user"


async def test_task_cancellation_drains_workers_before_staging_cleanup(task, monkeypatch):
    root, config, manager = task
    entered = asyncio.Event()
    running_roots = []
    async with MasterSession(config, llm_client=client(), planner=planner()) as session:
        async def block(run, user_input):
            running_roots.append(run.workspace.root)
            entered.set()
            await asyncio.Future()

        monkeypatch.setattr(session.session.engine, "run_turn", block)
        execution = asyncio.create_task(session.run_task("edit"))
        await asyncio.wait_for(entered.wait(), 5)
        assert manager.domains
        execution.cancel()
        with pytest.raises(asyncio.CancelledError):
            await execution
        assert not manager.domains
        assert all(not path.exists() for path in running_roots)
        with sqlite3.connect(config.state_root / "runs.db") as db:
            assert db.execute("SELECT status FROM master_run").fetchone()[0] == "cancelled"
    assert (root / "seed.txt").read_text() == "user"


async def test_completed_workers_do_not_accumulate_snapshot_copies(task):
    root, config, manager = task
    wsm = SnapshotWorkspaceManager(root, config.state_root, limits=manager.snapshot_limits)
    wsm.begin()
    try:
        candidate = await wsm.create_candidate(await wsm.base_revision())
        for i in range(15):
            worker = await wsm.create(str(i), base_ref=candidate.branch_name)
            (worker.root / "seed.txt").write_text(str(i))
            assert (await wsm.integrate(
                worker, candidate, read_set=set(), reads_unknown=False,
            ))[0] == "integrated"
            assert not worker.root.exists()
            assert len(wsm._snapshots) <= 2 and len(wsm._workspaces) == 1
        assert (candidate.root / "seed.txt").read_text() == "14"
    finally:
        wsm.end()
    assert not wsm._snapshots and not wsm._workspaces and wsm.initial is None
