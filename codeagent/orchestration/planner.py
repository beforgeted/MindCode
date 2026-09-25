"""Planner：把用户任务拆成 TaskGraph。

保守失败：LLM 输出无法解析/校验时退化为「单 Step 图」（整个任务交默认 Agent），
绝不抛异常打断编排。
"""

from __future__ import annotations

import json
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from codeagent.llm.client import LlmClient
from codeagent.llm.message import Message
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
    ) -> None:
        self._client = client
        self._model_config = model_config
        self._default_agent_id = default_agent_id
        self._max_repair_retries = max(0, max_repair_retries)

    async def plan(self, task: str) -> TaskGraph:
        messages = [Message.system(_SYSTEM), Message.user(task)]
        attempts = self._max_repair_retries + 1
        for attempt in range(attempts):
            try:
                response = await self._client.chat(messages, model_config=self._model_config)
                return self._parse(response.content)
            except (ValidationError, ValueError, TaskGraphError, json.JSONDecodeError):
                if attempt + 1 >= attempts:
                    break
                messages.append(Message.user("上一次输出不是合法的 plan JSON，请只输出 JSON。"))
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
