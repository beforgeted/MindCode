"""LocalVerifier：单个 Worker 结果的就地验证（记忆 V2 / 并行文档 §22-23）。

失败时给 feedback，AgentRuntime 据此做 reflection 重试。
保守失败：LLM 验证异常时默认放行（ok=True），不因验证器故障卡住整个编排。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from codeagent.agent.models import AgentRunResult, RunStatus
from codeagent.agent.run import AgentRun
from codeagent.llm.client import LlmClient
from codeagent.llm.message import Message
from codeagent.llm.types import ModelConfig


@dataclass(frozen=True, slots=True)
class VerificationResult:
    ok: bool
    reason: str = ""
    feedback: str = ""


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


class LlmLocalVerifier:
    def __init__(self, client: LlmClient, model_config: ModelConfig) -> None:
        self._client = client
        self._model_config = model_config

    async def verify(self, run: AgentRun, result: AgentRunResult) -> VerificationResult:
        prompt = (
            f"指令目标已由 Worker 执行，状态={result.status}。\n"
            f"结果摘要:\n{result.summary}\n"
            f"改动文件: {[f.path for f in result.files]}\n"
            f"测试: {[(t.name, str(t.outcome)) for t in result.tests]}"
        )
        try:
            response = await self._client.chat(
                [Message.system(_SYSTEM), Message.user(prompt)],
                model_config=self._model_config,
            )
            return self._parse(response.content)
        except Exception:
            # 保守失败：验证器故障不卡编排。
            return VerificationResult(ok=True, reason="verifier unavailable")

    def _parse(self, raw: str) -> VerificationResult:
        text = raw.strip()
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end < start:
            return VerificationResult(ok=True, reason="unparseable verifier output")
        payload = json.loads(text[start : end + 1])
        return VerificationResult(
            ok=bool(payload.get("ok", True)),
            reason=str(payload.get("reason", "")),
            feedback=str(payload.get("feedback", "")),
        )
