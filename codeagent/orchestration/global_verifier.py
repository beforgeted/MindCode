"""GlobalVerifier：跨 Worker 的全局验收（并行文档 §23）。

判断整批结果是否达成用户任务；不通过时给 replan_instruction 供 MasterRuntime 重规划。
保守失败：LLM 验证异常时默认接受（不因验证器故障反复 replan）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from codeagent.llm.client import LlmClient
from codeagent.llm.message import Message
from codeagent.llm.types import ModelConfig
from codeagent.orchestration.step_scheduler import SchedulerResult
from codeagent.orchestration.task_graph import TaskGraph


@dataclass(frozen=True, slots=True)
class GlobalVerdict:
    accept: bool
    reason: str = ""
    replan_instruction: str = ""


@runtime_checkable
class GlobalVerifier(Protocol):
    async def verify(
        self, task: str, graph: TaskGraph, results: SchedulerResult
    ) -> GlobalVerdict: ...


class AcceptAllVerifier:
    async def verify(
        self, task: str, graph: TaskGraph, results: SchedulerResult
    ) -> GlobalVerdict:
        return GlobalVerdict(accept=True)


class NoFailureVerifier:
    """确定性最小实现：无失败、无阻塞才接受。"""

    async def verify(
        self, task: str, graph: TaskGraph, results: SchedulerResult
    ) -> GlobalVerdict:
        if not results.failed and not results.blocked:
            return GlobalVerdict(accept=True)
        bad = sorted(results.failed | results.blocked)
        return GlobalVerdict(
            accept=False,
            reason=f"存在未完成 Step: {bad}",
            replan_instruction=f"以下 Step 未完成，请重规划：{bad}",
        )


_SYSTEM = """你是全局验收员。判断这批 Worker 的结果是否整体达成了用户任务。
只输出 JSON：{"accept":true/false,"reason":"...","replan_instruction":"若不接受，给重规划指令"}。
不要输出多余文字或代码围栏。"""


class LlmGlobalVerifier:
    def __init__(self, client: LlmClient, model_config: ModelConfig) -> None:
        self._client = client
        self._model_config = model_config

    async def verify(
        self, task: str, graph: TaskGraph, results: SchedulerResult
    ) -> GlobalVerdict:
        summary = "\n".join(
            f"- {sid}: status={w.result.status} verified={w.verification.ok}"
            for sid, w in results.workers.items()
        )
        prompt = f"用户任务:\n{task}\n\n各 Step 结果:\n{summary}"
        try:
            response = await self._client.chat(
                [Message.system(_SYSTEM), Message.user(prompt)],
                model_config=self._model_config,
            )
            return self._parse(response.content)
        except Exception:
            return GlobalVerdict(accept=True, reason="verifier unavailable")

    def _parse(self, raw: str) -> GlobalVerdict:
        text = raw.strip()
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end < start:
            return GlobalVerdict(accept=True, reason="unparseable verifier output")
        payload = json.loads(text[start : end + 1])
        return GlobalVerdict(
            accept=bool(payload.get("accept", True)),
            reason=str(payload.get("reason", "")),
            replan_instruction=str(payload.get("replan_instruction", "")),
        )
