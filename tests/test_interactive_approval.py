"""Phase 0 A3：REPL 交互审批接线。"""

from __future__ import annotations

from pathlib import Path

import pytest

from codeagent.config import AppConfig
from codeagent.context.profile import ContextProfile
from codeagent.llm.stub_client import StubLlmClient
from codeagent.session import AgentSession
from codeagent.tool.approval import (
    DenyExternalApprovalPolicy,
    InteractiveApprovalPolicy,
)
from codeagent.tool.command_policy import CommandDecision
from codeagent.tool.effects import EffectKind, RetryPolicy


def _config(tmp_path: Path, *, interactive: bool) -> AppConfig:
    return AppConfig(
        workspace_root=tmp_path,
        home=tmp_path / ".home",
        model="stub",
        profile=ContextProfile(context_window=20_000),
        use_stub_llm=True,
        interactive_approval=interactive,
    )


async def test_interactive_session_allows_external_and_uses_interactive_policy(tmp_path: Path):
    config = _config(tmp_path, interactive=True)
    async with AgentSession(config, llm_client=StubLlmClient([])) as s:
        assert s.run.allow_external_effects is True
        assert isinstance(s.execution_manager._approval, InteractiveApprovalPolicy)


async def test_noninteractive_session_blocks_external_and_denies(tmp_path: Path):
    config = _config(tmp_path, interactive=False)
    async with AgentSession(config, llm_client=StubLlmClient([])) as s:
        assert s.run.allow_external_effects is False
        assert isinstance(s.execution_manager._approval, DenyExternalApprovalPolicy)


@pytest.mark.parametrize(
    ("answer", "expected"),
    [("y", True), ("yes", True), ("n", False), ("", False)],
)
async def test_interactive_policy_reads_user_answer(monkeypatch, answer, expected):
    monkeypatch.setattr("builtins.input", lambda *_: answer)
    decision = CommandDecision(
        allowed=True, effect=EffectKind.EXTERNAL_SIDE_EFFECT,
        retry=RetryPolicy.NEVER, needs_approval=True, reason="外部副作用",
    )
    ok = await InteractiveApprovalPolicy().approve(decision, command="git push origin main")
    assert ok is expected
