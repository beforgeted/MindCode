"""Phase 0 A1：LLM 计量接线 —— session.metrics 与 client 是同一实例。"""

from __future__ import annotations

from pathlib import Path

from codeagent.config import AppConfig
from codeagent.context.profile import ContextProfile
from codeagent.infra.metrics import LLM_CALLS, LLM_INPUT_TOKENS
from codeagent.llm.stub_client import StubLlmClient
from codeagent.session import AgentSession


def test_stub_client_bind_metrics_records_calls() -> None:
    from codeagent.infra.metrics import Metrics

    client = StubLlmClient(["hi"])
    metrics = Metrics()
    client.bind_metrics(metrics)
    import asyncio

    from codeagent.llm.types import ModelConfig

    asyncio.run(client.chat([], model_config=ModelConfig()))
    counters = metrics.snapshot()["counters"]
    assert counters.get(LLM_CALLS, 0) == 1
    assert counters.get(LLM_INPUT_TOKENS, 0) >= 0


async def test_session_binds_metrics_into_client(tmp_path: Path) -> None:
    config = AppConfig(
        workspace_root=tmp_path,
        home=tmp_path / ".home",
        model="stub",
        profile=ContextProfile(context_window=20_000),
        use_stub_llm=True,
    )
    client = StubLlmClient(["回复"] * 3)
    async with AgentSession(config, llm_client=client) as session:
        await session.send("你好")
        counters = session.metrics.snapshot()["counters"]
    # session.send 至少触发一次 LLM 调用，且计数落在 session.metrics（同一实例）。
    assert counters.get(LLM_CALLS, 0) >= 1
