from __future__ import annotations

from pathlib import Path

from codeagent.agent.models import AgentDefinition, AgentRunResult
from codeagent.orchestration.task_graph import Step
from codeagent.runtime.agent_runtime import AgentRuntime
from codeagent.runtime.local_verifier import VerificationResult
from codeagent.workspace.manager import LocalWorkspaceManager

_DEFN = AgentDefinition(id="default", name="D", system_prompt="", max_reflection_count=3)
_STEP = Step("s1", "default", "做点事")


def _resolved(path: Path) -> Path:
    return path.resolve()


class FakeEngine:
    def __init__(self):
        self.calls = 0
        self.inputs: list[str] = []

    async def run_turn(self, run, user_input: str) -> AgentRunResult:
        self.calls += 1
        self.inputs.append(user_input)
        return AgentRunResult.success(run.run_id, f"done:{user_input}")


class FlakyVerifier:
    """前 fail_times 次判失败，之后通过。"""

    def __init__(self, fail_times: int):
        self._fail_times = fail_times
        self.calls = 0

    async def verify(self, run, result) -> VerificationResult:
        self.calls += 1
        if self.calls <= self._fail_times:
            return VerificationResult(ok=False, feedback="再试一次")
        return VerificationResult(ok=True)


async def test_reflection_retries_until_pass(tmp_path: Path):
    engine = FakeEngine()
    verifier = FlakyVerifier(fail_times=2)
    runtime = AgentRuntime(
        react_engine=engine,
        workspace_manager=LocalWorkspaceManager(tmp_path),
        local_verifier=verifier,
    )
    worker = await runtime.run(_DEFN, _STEP, session_id="s")
    assert worker.verification.ok
    assert worker.run.reflection_count == 2
    assert engine.calls == 3  # 1 次初始 + 2 次反思
    assert engine.inputs[0] == "做点事"
    assert engine.inputs[1].startswith("[验证反馈]")


async def test_reflection_budget_capped(tmp_path: Path):
    engine = FakeEngine()
    verifier = FlakyVerifier(fail_times=99)  # 永远失败
    runtime = AgentRuntime(
        react_engine=engine,
        workspace_manager=LocalWorkspaceManager(tmp_path),
        local_verifier=verifier,
    )
    worker = await runtime.run(_DEFN, _STEP, session_id="s")
    assert not worker.verification.ok
    assert worker.run.reflection_count == 3  # max_reflection_count
    assert engine.calls == 4  # 1 + 3


async def test_workspace_is_injected(tmp_path: Path):
    engine = FakeEngine()
    runtime = AgentRuntime(
        react_engine=engine,
        workspace_manager=LocalWorkspaceManager(tmp_path),
    )
    worker = await runtime.run(_DEFN, _STEP, session_id="s")
    assert worker.workspace.root == _resolved(tmp_path)
    assert worker.run.workspace.root == _resolved(tmp_path)
    assert worker.verification.ok  # AlwaysPassVerifier 默认
