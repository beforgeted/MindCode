"""MemoryJudge（记忆 V2 §22）：判断候选是否值得长期记忆，而非做总结。

保守失败（§40 / §50 原则 6）：LLM 输出无法解析/校验且修复重试仍失败时，
返回 should_remember=False —— 宁可漏记，不可错写污染未来所有 Session。
"""

from __future__ import annotations

import json
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from codeagent.llm.client import LlmClient
from codeagent.llm.message import Message
from codeagent.llm.types import ModelConfig
from codeagent.memory.governance_models import MemoryCandidate
from codeagent.memory.models import MemoryScope, MemoryType


class JudgeVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    should_remember: bool
    scope: MemoryScope = MemoryScope.PROJECT
    type: MemoryType = MemoryType.FACT
    content: str = ""
    importance: int = Field(default=5, ge=1, le=10)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    rationale: str = ""


@runtime_checkable
class MemoryJudge(Protocol):
    async def judge(self, candidate: MemoryCandidate) -> JudgeVerdict: ...


_SKIP = JudgeVerdict(should_remember=False, rationale="judge unavailable")

_SYSTEM = """你是记忆治理评审员。判断一段会话内容是否值得作为长期记忆保存。

只有满足以下条件才 shouldRemember=true：
- 未来其它 Session 仍然有用，且不能从代码/文件直接重新得到；
- 是稳定的事实、约束、决策、偏好或经验，而不是一次性的临时状态。

临时任务（"这次先把日志调成 debug"）、助手的猜测、可从代码重得的信息 → shouldRemember=false。

只输出一个 JSON 对象，字段：
shouldRemember(bool), scope(project/session/user/agent),
type(fact/preference/constraint/decision/failure/workflow/tool_insight/reference),
content(string), importance(1-10), confidence(0-1), rationale(string)。
不要输出任何多余文字或代码围栏。"""


class LlmMemoryJudge:
    def __init__(
        self,
        client: LlmClient,
        model_config: ModelConfig,
        *,
        max_repair_retries: int = 1,
    ) -> None:
        self._client = client
        self._model_config = model_config
        self._max_repair_retries = max(0, max_repair_retries)

    async def judge(self, candidate: MemoryCandidate) -> JudgeVerdict:
        messages = [
            Message.system(_SYSTEM),
            Message.user(_prompt(candidate)),
        ]
        attempts = self._max_repair_retries + 1
        for attempt in range(attempts):
            try:
                response = await self._client.chat(
                    messages, model_config=self._model_config
                )
                return _parse(response.content)
            except (ValidationError, ValueError, json.JSONDecodeError):
                if attempt + 1 >= attempts:
                    return _SKIP
                messages.append(Message.user("上一次输出不是合法 JSON verdict，请只输出 JSON。"))
            except Exception:
                # LLM 调用本身失败：保守失败，不写。
                return _SKIP
        return _SKIP


def _prompt(candidate: MemoryCandidate) -> str:
    return (
        f"来源: {candidate.source}\n"
        f"建议 scope: {candidate.proposed_scope}\n"
        f"建议 type: {candidate.proposed_type}\n"
        f"内容:\n{candidate.content}"
    )


def _parse(raw: str) -> JudgeVerdict:
    text = raw.strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("响应中没有 JSON 对象")
    payload = json.loads(text[start : end + 1])
    # 兼容 camelCase / snake_case。
    normalized = {
        "should_remember": payload.get("should_remember", payload.get("shouldRemember")),
        "scope": payload.get("scope", "project"),
        "type": payload.get("type", "fact"),
        "content": payload.get("content", ""),
        "importance": payload.get("importance", 5),
        "confidence": payload.get("confidence", 0.5),
        "rationale": payload.get("rationale", ""),
    }
    return JudgeVerdict.model_validate(normalized)


class FakeMemoryJudge:
    """测试用：按内容子串匹配返回预置 verdict，未命中则默认接受为 PROJECT/FACT。"""

    def __init__(
        self,
        verdicts: dict[str, JudgeVerdict] | None = None,
        *,
        default: JudgeVerdict | None = None,
    ) -> None:
        self._verdicts = verdicts or {}
        self._default = default or JudgeVerdict(
            should_remember=True,
            scope=MemoryScope.PROJECT,
            type=MemoryType.FACT,
            content="",
            importance=6,
            confidence=0.8,
        )

    async def judge(self, candidate: MemoryCandidate) -> JudgeVerdict:
        for needle, verdict in self._verdicts.items():
            if needle in candidate.content:
                return verdict
        return self._default
