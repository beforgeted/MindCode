"""Interactive publication and cancellation contracts; fake domains are not isolation evidence."""
from __future__ import annotations

import asyncio
import sys
from dataclasses import replace

import pytest

from codeagent.agent.models import RunStatus
from codeagent.context.history.conversation_history import validate_tool_protocol
from codeagent.execution.models import ExecutionPurpose, SandboxError
from codeagent.llm.message import Message, ToolUseBlock
from codeagent.llm.stub_client import StubLlmClient
from codeagent.session import AgentSession
from tests.test_master_integration import _config, _git_out, _init_repo, _worktree_count
from tests.test_sandbox_wiring import MemorySandbox

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="POSIX snapshot publication")


@pytest.fixture
def interactive(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    config = replace(_config(repo), home=tmp_path / "state", project_root=tmp_path / "state",
                     execution_backend="podman", sandbox_image="a" * 64, verify_command="check")
    manager = MemorySandbox()
    monkeypatch.setattr("codeagent.session.PodmanSandboxManager", lambda *a, **k: manager)
    return repo, config, manager


@pytest.mark.parametrize("accept", [True, False])
async def test_interactive_validation_gates_publication(interactive, accept):
    repo, config, manager = interactive
    before = _git_out(repo, "rev-parse", "HEAD")
    manager.validation_exit = 0 if accept else 1
    client = StubLlmClient([[('write_file', {"path": "note.txt", "content": "A"})], "done"])
    async with AgentSession(config, llm_client=client) as session:
        result = await session.send("write note")
        assert result.ok is accept
        assert session.run.workspace.root == repo
        assert session.run.sandbox is None and not manager.domains
        assert ("open", ExecutionPurpose.INTERACTIVE) in manager.calls
        assert ("command", ExecutionPurpose.VALIDATION) in manager.calls
        assert _worktree_count(repo) == 1
        if accept:
            assert (repo / "note.txt").read_text() == "A"
            assert _git_out(repo, "rev-parse", "HEAD") != before
        else:
            assert not (repo / "note.txt").exists()
            assert _git_out(repo, "rev-parse", "HEAD") == before
            assert not result.files
        validate_tool_protocol(session.run.history.messages)


async def test_readonly_and_multiturn_budget_and_clear(interactive):
    repo, config, manager = interactive
    config = replace(config, verify_command=None)
    client = StubLlmClient([
        [("read_file", {"path": "seed.txt"})], "first",
        [("read_file", {"path": "seed.txt"})], "second", "third",
    ])
    async with AgentSession(config, llm_client=client) as session:
        session.definition = replace(session.definition, max_react_iterations=2)
        session.clear()
        run = session.run
        first = await session.send("read seed")
        length = len(run.history)
        second = await session.send("read it again")
        assert first.ok and second.ok and second.iterations == 2
        assert session.run is run and len(run.history) > length
        assert not first.files and not second.files
        assert not any(call == ("command", ExecutionPurpose.VALIDATION) for call in manager.calls)
        session.clear()
        assert session.run is not run
        assert (await session.send("hello")).ok
        assert not manager.domains and _worktree_count(repo) == 1


@pytest.mark.parametrize("failure", ["no_check", "seal", "model", "cancel"])
async def test_failed_turn_never_publishes(interactive, failure, monkeypatch):
    repo, config, manager = interactive
    before = _git_out(repo, "rev-parse", "HEAD")
    if failure == "no_check":
        config = replace(config, verify_command=None)
    manager.fail_seal = failure == "seal"
    client = StubLlmClient([[('write_file', {"path": "note.txt", "content": "A"})], "done"])
    async with AgentSession(config, llm_client=client) as session:
        if failure == "model":
            async def fail(*args):
                raise RuntimeError("provider down")
            monkeypatch.setattr(session.engine, "run_turn", fail)
        if failure == "cancel":
            session.run.cancellation.cancel()
        result = await session.send("write note")
        assert not result.ok and not result.files
        assert not (repo / "note.txt").exists()
        assert _git_out(repo, "rev-parse", "HEAD") == before
        assert not manager.domains and _worktree_count(repo) == 1
        validate_tool_protocol(session.run.history.messages)


async def test_dirty_base_refused_before_tools(interactive):
    repo, config, manager = interactive
    (repo / "seed.txt").write_text("user edit")
    async with AgentSession(config, llm_client=StubLlmClient(["done"])) as session:
        with pytest.raises(SandboxError, match="未提交"):
            await session.send("edit")
    assert not manager.calls
    assert (repo / "seed.txt").read_text() == "user edit"


@pytest.mark.parametrize("change", ["dirty", "head"])
async def test_base_changes_during_validation_are_preserved(interactive, change, monkeypatch):
    repo, config, manager = interactive
    execute = manager.execute
    user_head = None

    async def modify_base(handle, command, **kwargs):
        nonlocal user_head
        if handle.purpose == ExecutionPurpose.VALIDATION:
            (repo / "seed.txt").write_text("user edit")
            if change == "head":
                _git_out(repo, "add", "seed.txt")
                _git_out(repo, "commit", "-m", "user edit")
            user_head = _git_out(repo, "rev-parse", "HEAD")
        return await execute(handle, command, **kwargs)

    monkeypatch.setattr(manager, "execute", modify_base)
    client = StubLlmClient([[('write_file', {"path": "note.txt", "content": "A"})], "done"])
    async with AgentSession(config, llm_client=client) as session:
        assert not (await session.send("edit")).ok
        assert (repo / "seed.txt").read_text() == "user edit"
        assert not (repo / "note.txt").exists()
        assert _git_out(repo, "rev-parse", "HEAD") == user_head
        assert not manager.domains and _worktree_count(repo) == 1


async def test_task_cancellation_repairs_protocol_and_next_turn_recovers(interactive, monkeypatch):
    repo, config, manager = interactive
    entered = asyncio.Event()
    async with AgentSession(config, llm_client=StubLlmClient(["next"])) as session:
        original = session.engine.run_turn

        async def block(run, text):
            turn = run.history.begin_turn()
            run.history.append(Message.user(text, turn_id=turn))
            run.history.append(Message.assistant([
                ToolUseBlock("pending", "write_file", {"path": "note.txt", "content": "A"}),
            ], turn_id=turn))
            entered.set()
            await asyncio.Future()

        monkeypatch.setattr(session.engine, "run_turn", block)
        task = asyncio.create_task(session.send("edit"))
        await asyncio.wait_for(entered.wait(), 10)
        with pytest.raises(RuntimeError, match="执行期间"):
            session.clear()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert session.run.status == RunStatus.CANCELLED
        validate_tool_protocol(session.run.history.messages)
        assert session.run.history.current_turn_id is None
        assert not manager.domains and _worktree_count(repo) == 1
        monkeypatch.setattr(session.engine, "run_turn", original)
        assert (await session.send("continue")).ok
        assert not (repo / "note.txt").exists()
