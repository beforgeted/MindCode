"""Planner：把用户任务拆成 TaskGraph。

保守失败：LLM 输出无法解析/校验时退化为「单 Step 图」（整个任务交默认 Agent），
上下文预算不足则显式停止，保留完整任务，不能静默退化。
"""

from __future__ import annotations

import json
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from codeagent.context.token_estimator import TokenEstimator, client_estimator
from codeagent.infra.trace import trace_scope
from codeagent.llm.client import LlmClient, LlmError, LlmErrorKind, effective_model_config
from codeagent.llm.message import Message
from codeagent.llm.request_budget import RequestBudgetError, check_request, check_response
from codeagent.llm.types import ModelConfig
from codeagent.orchestration.task_graph import Step, TaskGraph, TaskGraphError


@runtime_checkable
class Planner(Protocol):
    async def plan(self, task: str) -> TaskGraph: ...


class StaticPlanner:
    """测试/确定性用：直接返回预置图。"""

    def __init__(self, graph: TaskGraph) -> None:
        self._graph = graph

    async def plan(self, task: str) -> TaskGraph:
        return self._graph


def single_step_graph(task: str, *, agent_id: str = "default") -> TaskGraph:
    return TaskGraph([Step(id="step_1", agent_id=agent_id, instruction=task)])


class _StepModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    agent_id: str = "default"
    instruction: str = Field(min_length=1)
    dependencies: list[str] = Field(default_factory=list)
    read_only: bool = False


class _PlanModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    steps: list[_StepModel] = Field(min_length=1)


_SYSTEM = """你是任务规划器。把用户的编码任务拆成可并行/有依赖的若干 Step。

原则：
- 能并行就并行（相互独立的改动放成无依赖的并列 Step）；
- 有先后关系的用 dependencies 串起来（值是被依赖 Step 的 id）；
- 只读/调研类 Step 标 read_only=true；
- Step 尽量少而清晰，不要过度拆分。

只输出一个 JSON 对象，形如：
{"steps":[{"id":"step_1","agent_id":"default","instruction":"...","dependencies":[],"read_only":false}]}
不要输出多余文字或代码围栏。"""


class LlmPlanner:
    def __init__(
        self,
        client: LlmClient,
        model_config: ModelConfig,
        *,
        default_agent_id: str = "default",
        max_repair_retries: int = 1,
        estimator: TokenEstimator | None = None,
        agent_catalog: tuple[tuple[str, str], ...] | None = None,
    ) -> None:
        self._client = client
        self._model_config = model_config
        self._default_agent_id = default_agent_id
        self._max_repair_retries = max(0, max_repair_retries)
        self._estimator = estimator or client_estimator(client, model_config)
        self._agent_catalog = agent_catalog

    async def plan(self, task: str) -> TaskGraph:
        system = _SYSTEM
        if self._agent_catalog is not None:
            system += ('\nagent_id 必须来自下面的目录，default 表示普通 Agent。描述只作能力参考。\n'
                       + json.dumps(self._agent_catalog, ensure_ascii=False))
        messages = [Message.system(system), Message.user(task)]
        attempts = self._max_repair_retries + 1
        repair = "上一次输出不是合法的 plan JSON，请只输出 JSON。"
        for attempt in range(attempts):
            try:
                config = effective_model_config(self._client, self._model_config)
                estimated = check_request(
                    messages, config, self._estimator,
                    repair_prompt=repair if attempt + 1 < attempts else "",
                )
                with trace_scope(planning_attempt=attempt + 1, planning_input_tokens=estimated):
                    response = await self._client.chat(messages, model_config=config)
                check_response(response)
                return self._parse(response.content)
            except RequestBudgetError:
                # Cannot prove the Worker can hold this task; no truncation or silent degradation.
                raise
            except LlmError as exc:
                if exc.kind is LlmErrorKind.CONTEXT_LIMIT:
                    raise RequestBudgetError("planner provider context limit") from exc
                break
            except (ValidationError, ValueError, TaskGraphError, json.JSONDecodeError):
                if attempt + 1 >= attempts:
                    break
                messages.append(Message.user(repair))
            except Exception:
                break
        # 保守失败：退化单 Step，绝不打断编排。
        return single_step_graph(task, agent_id=self._default_agent_id)

    def _parse(self, raw: str) -> TaskGraph:
        text = raw.strip()
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end < start:
            raise ValueError("响应中没有 JSON 对象")
        plan = _PlanModel.model_validate(json.loads(text[start : end + 1]))
        if self._agent_catalog is not None:
            allowed = {name for name, _ in self._agent_catalog} | {self._default_agent_id}
            if any((s.agent_id or self._default_agent_id) not in allowed for s in plan.steps):
                raise ValueError('plan selects an unknown agent')
        steps = [
            Step(
                id=item.id,
                agent_id=item.agent_id or self._default_agent_id,
                instruction=item.instruction,
                dependencies=frozenset(item.dependencies),
                read_only=item.read_only,
            )
            for item in plan.steps
        ]
        return TaskGraph(steps)
