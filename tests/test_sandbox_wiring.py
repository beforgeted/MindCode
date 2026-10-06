"""Control-plane wiring tests with an in-memory backend, not containment evidence."""
from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import pytest

from codeagent.config import AppConfig
from codeagent.execution.models import ExecutionPurpose, ProcessOutput, SandboxError, SandboxHandle
from codeagent.execution.podman import PodmanSandboxManager
from codeagent.execution.snapshot import SnapshotEntry, TreeSnapshot
from codeagent.llm.stub_client import StubLlmClient
from codeagent.orchestration.master_session import build_master
from codeagent.orchestration.planner import StaticPlanner
from codeagent.orchestration.task_graph import Step, TaskGraph
from codeagent.runtime.agent_runtime import AgentRuntime
from codeagent.runtime.local_verifier import VerificationResult
from codeagent.session import AgentSession
from codeagent.tool.builtin.write_file import WriteFileTool
from codeagent.tool.execution_manager import ExecutionScope
from codeagent.tool.executor import SandboxExecutor
from codeagent.tool.models import ToolCall
from codeagent.tool.sandbox import SandboxTools
from codeagent.workspace.context import WorkspaceContext


class MemorySandbox(PodmanSandboxManager):
    def __init__(self, *args, **kwargs):
        super().__init__("a" * 64)
        self.domains = {}
        self.calls = []
        self.fail_seal = False
        self.fail_close = False
        self.validation_exit = 0

    async def ensure_available(self):
        pass

    def is_active(self, handle):
        return handle.owner == self.owner and handle.container_id in self.domains

    async def open(self, snapshot, purpose=ExecutionPurpose.WORKER):
        cid = str(len(self.calls))
        handle = SandboxHandle(cid, cid, self.owner, purpose, 1, "1")
        self.domains[cid] = {e.path: e.data for e in snapshot.entries}
        self.calls.append(("open", purpose))
        return handle

    async def execute_python(self, handle, source, data, **kwargs):
        assert "def main()" in source
        payload = json.loads(data)
        name, args = payload["tool"], payload["arguments"]
        self.calls.append((name, handle.container_id))
        files = self.domains[handle.container_id]
        if name == "write_file":
            files[args["path"]] = args["content"].encode()
        content = files.get(args.get("path", ""), b"matches")
        return ProcessOutput(0, content, b"")

    async def execute(self, handle, command, **kwargs):
        self.calls.append(("command", handle.purpose))
        code = self.validation_exit if handle.purpose == ExecutionPurpose.VALIDATION else 0
        return ProcessOutput(code, b"sandbox command\n", b"")

    async def seal(self, handle):
        self.calls.append(("seal", handle.container_id))
        if self.fail_seal:
            raise SandboxError("invalid export")
        snapshot = TreeSnapshot(tuple(
            SnapshotEntry(p, d) for p, d in sorted(self.domains[handle.container_id].items())
        ))
        await self.close(handle)
        return snapshot

    async def close(self, handle):
        self.calls.append(("close", handle.container_id))
        if self.fail_close:
            raise SandboxError("cleanup failed")
        self.domains.pop(handle.container_id, None)


class IsolatedWorkspace:
    isolated = True

    def __init__(self, root):
        self.root = root

    async def create(self, run_id, *, base_ref=None):
        return WorkspaceContext(root=self.root, worktree_id=run_id, is_isolated=True)

    async def cleanup(self, workspace, *, keep=False):
        pass


@pytest.fixture
def wiring(tmp_path, monkeypatch):
    initial = TreeSnapshot((SnapshotEntry("seed.txt", b"seed"),))
    published = []
    manager = MemorySandbox()

    def capture(workspace, limits):
        return initial

    def publish(workspace, before, after, limits):
        assert not manager.domains, "domain must be destroyed before any host publication"
        assert before == initial
        published.append(after)

    monkeypatch.setattr("codeagent.runtime.worker_sandbox.capture_workspace", capture)
    monkeypatch.setattr("codeagent.runtime.worker_sandbox.publish_workspace", publish)
    config = AppConfig(tmp_path, tmp_path / ".codeagent", use_stub_llm=True,
                       execution_backend="podman", sandbox_image="a" * 64)
    return config, manager, published


async def test_worker_routes_all_workspace_tools_to_same_domain(wiring):
    config, manager, published = wiring
    client = StubLlmClient([
        [("write_file", {"path": "note.txt", "content": "new"})],
        [("read_file", {"path": "note.txt"}), ("grep", {"pattern": "new"})],
        [("run_command", {"command": "echo sandbox"})],
        "done",
    ])
    async with AgentSession(config, llm_client=client) as session:
        runtime = AgentRuntime(react_engine=session.engine,
                               workspace_manager=IsolatedWorkspace(config.workspace_root),
                               sandbox_manager=manager)
        worker = await runtime.run(session.definition, Step("s", "default", "edit"), session_id="s")
        assert worker.result.ok and worker.verification.ok
        assert worker.run.sandbox is None
        assert {e.path: e.data for e in published[0].entries}["note.txt"] == b"new"
        assert not (config.workspace_root / "note.txt").exists()
        tool_calls = [(name, cid) for name, cid in manager.calls
                      if name in ("write_file", "read_file", "grep")]
        assert len(tool_calls) == 3 and len({cid for _, cid in tool_calls}) == 1
        assert ("command", ExecutionPurpose.WORKER) in manager.calls


@pytest.mark.parametrize("failure", ["seal", "close", "verification", "exception", "cancel"])
async def test_failed_worker_never_publishes(wiring, failure, monkeypatch):
    config, manager, published = wiring
    manager.fail_seal = failure == "seal"
    manager.fail_close = failure == "close"

    class Reject:
        async def verify(self, run, result):
            return VerificationResult(ok=False)

    async with AgentSession(config, llm_client=StubLlmClient(["done"])) as session:
        if failure in ("exception", "cancel"):
            async def broken(*args):
                if failure == "cancel":
                    raise asyncio.CancelledError()
                raise RuntimeError("engine failure")
            monkeypatch.setattr(session.engine, "run_turn", broken)
        runtime = AgentRuntime(react_engine=session.engine,
                               workspace_manager=IsolatedWorkspace(config.workspace_root),
                               local_verifier=Reject() if failure == "verification" else None,
                               sandbox_manager=manager)
        definition = replace(session.definition, max_reflection_count=0)
        if failure == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await runtime.run(definition, Step("s", "default", "edit"), session_id="s")
        else:
            worker = await runtime.run(definition, Step("s", "default", "edit"), session_id="s")
            assert not worker.result.ok or not worker.verification.ok
        assert not published
        if failure != "close":
            assert not manager.domains


async def test_missing_domain_and_custom_tools_cannot_fall_back_to_host(wiring):
    config, manager, _ = wiring

    class Custom(WriteFileTool):
        async def execute(self, ctx, arguments):
            pytest.fail("custom host tool executed")

    async with AgentSession(config, llm_client=StubLlmClient(["done"])) as session:
        scope = ExecutionScope("r", "s", session.run.workspace, session.run.cancellation,
                               session.profile)
        outcome = await session.execution_manager.execute_batch(
            scope, [ToolCall("c", "write_file", {"path": "host.txt", "content": "bad"})],
        )
        assert "没有绑定" in outcome.results[0].content
        assert not (config.workspace_root / "host.txt").exists()
        await session.send("inspect")
        assert not (config.workspace_root / "host.txt").exists()
        handle = await manager.open(TreeSnapshot(()))
        session.registry._tools["write_file"] = Custom()
        scope = replace(scope, sandbox=SandboxTools(SandboxExecutor(
            manager, handle, config.workspace_root,
        )))
        outcome = await session.execution_manager.execute_batch(
            scope, [ToolCall("c2", "write_file", {})],
        )
        assert "尚未接入" in outcome.results[0].content
        await manager.close(handle)


@pytest.mark.parametrize("backend", ["unknown", "podman"])
def test_invalid_backend_config_fails_early(tmp_path, backend):
    with pytest.raises(ValueError):
        AppConfig(tmp_path, tmp_path, execution_backend=backend)


@pytest.mark.parametrize("exit_code", [0, 1])
async def test_master_validation_uses_separate_domain_and_gates_promote(
    tmp_path, monkeypatch, exit_code,
):
    from tests.test_master_integration import _config, _git_out, _init_repo

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    original = _git_out(repo, "rev-parse", "HEAD")
    config = replace(_config(repo), execution_backend="podman", sandbox_image="a" * 64,
                     verify_command="echo validation")
    manager = MemorySandbox()
    manager.validation_exit = exit_code
    monkeypatch.setattr("codeagent.orchestration.master_session.PodmanSandboxManager",
                        lambda *a, **kw: manager)

    def capture(workspace, limits):
        data = (workspace.root / "seed.txt").read_bytes()
        return TreeSnapshot((SnapshotEntry("seed.txt", data),))

    def publish(workspace, initial, output, limits):
        assert not manager.domains
        for entry in output.entries:
            (workspace.root / entry.path).write_bytes(entry.data)

    monkeypatch.setattr("codeagent.runtime.worker_sandbox.capture_workspace", capture)
    monkeypatch.setattr("codeagent.runtime.worker_sandbox.publish_workspace", publish)
    monkeypatch.setattr("codeagent.orchestration.master_runtime.capture_workspace", capture)
    client = StubLlmClient([[('write_file', {"path": "a.txt", "content": "A"})], "done"])
    async with AgentSession(config, llm_client=client) as session:
        master = await build_master(
            config=config, llm_client=client, engine=session.engine,
            event_store=session.event_store, metrics=session.metrics, definition=session.definition,
            planner=StaticPlanner(TaskGraph([Step("s", "default", "edit")])),
        )
        master._max_replans = 0
        result = await master.run("edit", session_id=session.session_id)
        assert result.integrated == (exit_code == 0)
        assert ("command", ExecutionPurpose.VALIDATION) in manager.calls
        assert not manager.domains
        if exit_code:
            assert _git_out(repo, "rev-parse", "HEAD") == original
            assert not (repo / "a.txt").exists()
        else:
            assert (repo / "a.txt").read_text() == "A"


async def test_sandbox_deferred_actions_never_execute_on_host(tmp_path):
    from codeagent.evidence.artifact_store import FileArtifactStore
    from codeagent.orchestration.global_verifier import NoFailureVerifier
    from codeagent.orchestration.master_runtime import MasterRuntime
    from codeagent.tool.approval import AllowExternalApprovalPolicy
    from codeagent.tool.deferred import DeferredAction, DeferredRecord, DeferredState
    from codeagent.tool.effects import EffectKind, RetryPolicy
    from codeagent.workspace.git_worktree import GitWorktreeWorkspaceManager
    from tests.test_master_integration import _init_repo

    _init_repo(tmp_path)
    action = DeferredAction(
        "echo bad > host.txt", EffectKind.EXTERNAL_SIDE_EFFECT, RetryPolicy.NEVER,
    )
    master = MasterRuntime(
        planner=StaticPlanner(TaskGraph([])), scheduler=None,  # type: ignore[arg-type]
        global_verifier=NoFailureVerifier(),
        workspace_manager=GitWorktreeWorkspaceManager(tmp_path, tmp_path / "worktrees"),
        approval_policy=AllowExternalApprovalPolicy(),
        artifact_store=FileArtifactStore(tmp_path / "state"), sandbox_manager=MemorySandbox(),
    )
    records = await master._execute_deferred(
        (DeferredRecord(action),), None, master_run_id="m", attempt_no=1,
    )
    assert records[0].state == DeferredState.SKIPPED
    assert not (tmp_path / "host.txt").exists()
