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
class VerificationTarget:
    """验收对象 = 真实集成产物（candidate 冻结态），不是 Worker 过程自述。"""

    revision: str | None = None
    changed_files: tuple[str, ...] = ()
    diff: str = ""
    deterministic_ok: bool | None = None  # 配了 verify 命令时的结果；None=未跑
    deterministic_detail: str = ""


@dataclass(frozen=True, slots=True)
class GlobalVerdict:
    accept: bool
    reason: str = ""
    replan_instruction: str = ""
    # 不可用/不可解析 → indeterminate：**不推进真实 base**（提交门禁 fail-closed）。
    indeterminate: bool = False


@runtime_checkable
class GlobalVerifier(Protocol):
    async def verify(
        self,
        task: str,
        graph: TaskGraph,
        results: SchedulerResult,
        target: VerificationTarget | None = None,
    ) -> GlobalVerdict: ...


class AcceptAllVerifier:
    async def verify(
        self,
        task: str,
        graph: TaskGraph,
        results: SchedulerResult,
        target: VerificationTarget | None = None,
    ) -> GlobalVerdict:
        return GlobalVerdict(accept=True)


class NoFailureVerifier:
    """确定性最小实现：无失败、无阻塞、确定性检查未失败才接受。"""

    async def verify(
        self,
        task: str,
        graph: TaskGraph,
        results: SchedulerResult,
        target: VerificationTarget | None = None,
    ) -> GlobalVerdict:
        if target is not None and target.deterministic_ok is False:
            return GlobalVerdict(
                accept=False,
                reason=f"确定性验收失败: {target.deterministic_detail}",
                replan_instruction="集成产物未通过验收命令，请修正",
            )
        if not results.failed and not results.blocked:
            return GlobalVerdict(accept=True)
        bad = sorted(results.failed | results.blocked)
        return GlobalVerdict(
            accept=False,
            reason=f"存在未完成 Step: {bad}",
            replan_instruction=f"以下 Step 未完成，请重规划：{bad}",
        )


_SYSTEM = """你是全局验收员。依据**真实集成产物**（改动文件与 diff）判断是否整体达成用户任务。
只输出 JSON：{"accept":true/false,"reason":"...","replan_instruction":"若不接受，给重规划指令"}。
不要输出多余文字或代码围栏。"""

_MAX_DIFF = 12_000


class LlmGlobalVerifier:
    def __init__(self, client: LlmClient, model_config: ModelConfig) -> None:
        self._client = client
        self._model_config = model_config

    async def verify(
        self,
        task: str,
        graph: TaskGraph,
        results: SchedulerResult,
        target: VerificationTarget | None = None,
    ) -> GlobalVerdict:
        # ① 确定性检查失败 → 直接 reject（不必再问 LLM）。
        if target is not None and target.deterministic_ok is False:
            return GlobalVerdict(
                accept=False,
                reason=f"确定性验收失败: {target.deterministic_detail}",
                replan_instruction="集成产物未通过验收命令，请修正",
            )
        # ② LLM 语义检查：喂真实产物（改动文件 + 有界 diff），不再只看过程摘要。
        summary = "\n".join(
            f"- {sid}: status={w.result.status} verified={w.verification.ok}"
            for sid, w in results.workers.items()
        )
        artifact = ""
        if target is not None:
            files = "\n".join(f"  {p}" for p in target.changed_files) or "  (无)"
            artifact = (
                f"\n\n集成后改动文件:\n{files}\n\n集成 diff（截断）:\n{target.diff[:_MAX_DIFF]}"
            )
        prompt = f"用户任务:\n{task}\n\n各 Step 结果:\n{summary}{artifact}"
        try:
            response = await self._client.chat(
                [Message.system(_SYSTEM), Message.user(prompt)],
                model_config=self._model_config,
            )
        except Exception:
            # 提交门禁 fail-closed：验证器不可用 → indeterminate，不推进真实 base。
            return GlobalVerdict(
                accept=False, indeterminate=True, reason="verifier unavailable"
            )
        return self._parse(response.content)

    def _parse(self, raw: str) -> GlobalVerdict:
        text = raw.strip()
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end < start:
            return GlobalVerdict(
                accept=False, indeterminate=True, reason="unparseable verifier output"
            )
        try:
            payload = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return GlobalVerdict(accept=False, indeterminate=True, reason="invalid verifier json")
        return GlobalVerdict(
            accept=bool(payload.get("accept", False)),
            reason=str(payload.get("reason", "")),
            replan_instruction=str(payload.get("replan_instruction", "")),
        )
