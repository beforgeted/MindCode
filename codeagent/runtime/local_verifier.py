"""LocalVerifier：单个 Worker 结果的就地验证（记忆 V2 / 并行文档 §22-23）。

失败时给 feedback，AgentRuntime 据此做 reflection 重试。
验证器故障、证据超限和无效输出均为无法判定，不能回传变更。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

from codeagent.agent.models import AgentRunResult, RunStatus
from codeagent.agent.run import AgentRun
from codeagent.context.token_estimator import HeuristicTokenEstimator, TokenEstimator
from codeagent.infra.trace import trace_scope
from codeagent.llm.client import LlmClient, effective_model_config
from codeagent.llm.message import Message
from codeagent.llm.request_budget import check_request, check_response
from codeagent.llm.types import ModelConfig


@dataclass(frozen=True, slots=True)
class VerificationResult:
    ok: bool
    reason: str = ""
    feedback: str = ""
    indeterminate: bool = False


@runtime_checkable
class LocalVerifier(Protocol):
    async def verify(self, run: AgentRun, result: AgentRunResult) -> VerificationResult: ...


class AlwaysPassVerifier:
    async def verify(self, run: AgentRun, result: AgentRunResult) -> VerificationResult:
        return VerificationResult(ok=True)


class StatusLocalVerifier:
    """确定性最小实现：只看 RunStatus，SUCCESS 才算过。"""

    async def verify(self, run: AgentRun, result: AgentRunResult) -> VerificationResult:
        if result.status is RunStatus.SUCCESS:
            return VerificationResult(ok=True)
        return VerificationResult(
            ok=False,
            reason=str(result.status),
            feedback=result.error or f"运行未成功: {result.status}",
        )


_SYSTEM = """你是 Worker 结果的验证员。判断这次执行是否达成了指令目标。
只输出 JSON：{"ok":true/false,"reason":"...","feedback":"给 Worker 的纠正建议"}。
不要输出多余文字或代码围栏。"""


class _LocalPayload(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    ok: bool
    reason: str = ""
    feedback: str = ""


class LlmLocalVerifier:
    def __init__(
        self, client: LlmClient, model_config: ModelConfig, *,
        estimator: TokenEstimator | None = None,
    ) -> None:
        self._client = client
        self._model_config = model_config
        self._estimator = estimator or HeuristicTokenEstimator()

    async def verify(self, run: AgentRun, result: AgentRunResult) -> VerificationResult:
        if not result.ok:
            return VerificationResult(
                ok=False, reason=str(result.status), feedback=result.error or "",
            )
        if not run.context.instruction:
            return VerificationResult(ok=False, indeterminate=True, reason="missing step goal")
        prompt = (
            f"原始步骤目标:\n{run.context.instruction}\n执行状态={result.status}。\n"
            f"结果摘要:\n{result.summary}\n"
            f"改动文件: {[f.path for f in result.files]}\n"
            f"测试: {[(t.name, str(t.outcome)) for t in result.tests]}"
        )
        try:
            config = effective_model_config(self._client, self._model_config)
            messages = [Message.system(_SYSTEM), Message.user(prompt)]
            estimated = check_request(messages, config, self._estimator)
            with trace_scope(verification_phase="local", verification_input_tokens=estimated):
                response = await self._client.chat(messages, model_config=config)
            check_response(response)
            return self._parse(response.content)
        except Exception as exc:
            return VerificationResult(
                ok=False, indeterminate=True,
                reason=f"local verification unavailable: {type(exc).__name__}",
            )

    def _parse(self, raw: str) -> VerificationResult:
        text = raw.strip()
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end < start:
            raise ValueError("unparseable verifier output")
        payload = _LocalPayload.model_validate(json.loads(text[start : end + 1]))
        return VerificationResult(
            ok=payload.ok, reason=payload.reason, feedback=payload.feedback,
        )
