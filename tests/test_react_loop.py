"""端到端：ReAct 主循环 + 真实工具 + Evidence 落盘。"""

from __future__ import annotations

from codeagent.agent.models import RunStatus
from codeagent.context.history.conversation_history import validate_tool_protocol
from codeagent.evidence.models import EventType
from codeagent.llm.stub_client import StubLlmClient
from codeagent.memory.models import MemoryType
from codeagent.session import AgentSession


async def test_single_tool_then_answer(config, workspace):
    (workspace / "hello.txt").write_text("line one\nline two\n", encoding="utf-8")
    client = StubLlmClient(
        [
            [("read_file", {"path": "hello.txt"})],
            "文件里有两行：line one 和 line two。",
        ]
    )
    async with AgentSession(config, llm_client=client) as session:
        result = await session.send("读一下 hello.txt")

    assert result.status is RunStatus.SUCCESS
    assert "line one" in result.summary or "两行" in result.summary
    assert result.iterations == 2
    validate_tool_protocol(session.run.history.messages)


async def test_parallel_tool_calls_keep_protocol(config, workspace):
    (workspace / "a.txt").write_text("aaa\n", encoding="utf-8")
    (workspace / "b.txt").write_text("bbb\n", encoding="utf-8")
    client = StubLlmClient(
        [
            [
                ("read_file", {"path": "a.txt"}),
                ("read_file", {"path": "b.txt"}),
                ("read_file", {"path": "missing.txt"}),
            ],
            "读完了。",
        ]
    )
    async with AgentSession(config, llm_client=client) as session:
        result = await session.send("并行读三个文件")
        history = session.run.history.messages

    assert result.status is RunStatus.SUCCESS
    tool_message = [m for m in history if m.tool_results][-1]
    # 3 个 tool_call -> 同一条消息里 3 个 tool_result，其中一个是 error
    assert len(tool_message.tool_results) == 3
    assert sum(1 for b in tool_message.tool_results if b.is_error) == 1
    validate_tool_protocol(history)


async def test_write_file_reported_as_file_state(config, workspace):
    client = StubLlmClient(
        [
            [("write_file", {"path": "out/new.py", "content": "print('hi')\n"})],
            "已创建 out/new.py。",
        ]
    )
    async with AgentSession(config, llm_client=client) as session:
        result = await session.send("创建 out/new.py")

    assert (workspace / "out" / "new.py").read_text(encoding="utf-8") == "print('hi')\n"
    assert [f.path for f in result.files] == [str((workspace / "out" / "new.py").resolve())]
    assert result.files[0].change == "created"


async def test_events_are_persisted(config, workspace):
    (workspace / "x.txt").write_text("x\n", encoding="utf-8")
    client = StubLlmClient([[("read_file", {"path": "x.txt"})], "done"])
    async with AgentSession(config, llm_client=client) as session:
        await session.send("读 x.txt")
        events = await session.event_store.query(session.session_id)

    types = [e.type for e in events]
    assert EventType.AGENT_RUN_STARTED in types
    assert EventType.TOOL_CALL in types
    assert EventType.TOOL_RESULT in types
    assert EventType.AGENT_RUN_FINISHED in types
    # 每条 tool 调用都能顺 tool_run_id 追回去
    tool_calls = [e for e in events if e.type == EventType.TOOL_CALL]
    assert all(e.tool_run_id for e in tool_calls)


async def test_max_iterations_terminates(config, workspace):
    from dataclasses import replace

    (workspace / "loop.txt").write_text("l\n", encoding="utf-8")
    client = StubLlmClient([[("read_file", {"path": "loop.txt"})]] * 20)
    async with AgentSession(config, llm_client=client) as session:
        session.definition = replace(session.definition, max_react_iterations=3)
        session.run = session._new_run()
        result = await session.send("无限循环")

    assert result.status is RunStatus.MAX_ITERATIONS
    assert result.iterations == 3
    validate_tool_protocol(session.run.history.messages)


async def test_clear_starts_new_context_but_keeps_events(config, workspace):
    client = StubLlmClient(["第一轮", "第二轮"])
    async with AgentSession(config, llm_client=client) as session:
        await session.send("hello")
        before = len(session.run.history)
        session.clear()
        assert len(session.run.history) < before
        await session.send("再来")
        events = await session.event_store.query(session.session_id)

    # Clear Context != Forget Evidence
    assert len([e for e in events if e.type == EventType.AGENT_RUN_STARTED]) == 2


async def test_memory_survives_clear_and_session_reopen(config, workspace):
    memory_id: str
    async with AgentSession(config, llm_client=StubLlmClient([])) as session:
        item, _ = await session.memory_service.add(
            "项目固定使用 Python 3.11",
            MemoryType.CONSTRAINT,
        )
        memory_id = item.id
        session.clear()
        assert (await session.memory_service.show(memory_id)) is not None

    async with AgentSession(config, llm_client=StubLlmClient([])) as reopened:
        restored = await reopened.memory_service.show(memory_id)
        assert restored is not None
        assert restored.content == "项目固定使用 Python 3.11"
