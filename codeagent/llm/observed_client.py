"""统一记录一次逻辑 LLM 调用；不保存 prompt、响应正文或异常消息。"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from dataclasses import asdict
from datetime import UTC, datetime

from codeagent.evidence.event_store import RawEventStore
from codeagent.evidence.models import AgentEvent, EventType
from codeagent.infra.ids import new_llm_call_id
from codeagent.infra.metrics import Metrics
from codeagent.infra.trace import current_trace, trace_scope
from codeagent.llm.client import LlmClient, LlmError, effective_model_config
from codeagent.llm.message import Message
from codeagent.llm.pricing import CostConfig, usd
from codeagent.llm.types import LlmResponse, ModelConfig, ToolSpec
from codeagent.orchestration.cost_store import CostStore, cost_run


class RoleLlmClient:
    def __init__(self, client: LlmClient, role: str) -> None:
        self._client = client
        self._role = role

    def effective_config(self, model_config: ModelConfig) -> ModelConfig:
        with trace_scope(role=self._role):
            return effective_model_config(self._client, model_config)

    async def chat(
        self, messages: Sequence[Message], *, model_config: ModelConfig,
        tools: Sequence[ToolSpec] = (),
    ) -> LlmResponse:
        with trace_scope(role=self._role):
            return await self._client.chat(messages, model_config=model_config, tools=tools)

    async def count_tokens(
        self, messages: Sequence[Message], *, model_config: ModelConfig,
        tools: Sequence[ToolSpec] = (),
    ) -> int | None:
        with trace_scope(role=self._role):
            return await self._client.count_tokens(messages, model_config=model_config, tools=tools)


class ObservedLlmClient:
    def __init__(
        self, client: LlmClient, *, metrics: Metrics, events: RawEventStore, session_id: str,
        costs: CostConfig | None = None, cost_store: CostStore | None = None,
    ) -> None:
        # 装配可重复，不能嵌套包装导致双计数。
        self._client = client._client if isinstance(client, ObservedLlmClient) else client
        self._metrics = metrics
        self._events = events
        self._session_id = session_id
        self._costs = costs or CostConfig()
        self._cost_store = cost_store
        # Provider 的历史计量接口仍可独立使用；装配后由本层统一负责会话计量。
        bind = getattr(self._client, "bind_metrics", None)
        if callable(bind):
            bind(Metrics())

    async def chat(
        self, messages: Sequence[Message], *, model_config: ModelConfig,
        tools: Sequence[ToolSpec] = (),
    ) -> LlmResponse:
        started_at = datetime.now(UTC).isoformat()
        started = time.perf_counter()
        call_id = new_llm_call_id()
        run_id = cost_run()
        provider = str(current_trace().get('provider', 'anthropic'))
        if self._costs.prices and run_id is not None and self._cost_store is not None:
            # Synchronous intent precedes any possibly billed provider request.
            await self._cost_store.begin(call_id, run_id, provider, model_config.model)
        response = None
        status, error_type = "success", None
        error_kind = None
        try:
            response = await self._client.chat(messages, model_config=model_config, tools=tools)
            return response
        except asyncio.CancelledError:
            status, error_type = "cancelled", "CancelledError"
            raise
        except Exception as exc:
            status, error_type = "error", type(exc).__name__
            error_kind = exc.kind if isinstance(exc, LlmError) else None
            raise
        finally:
            price = self._costs.prices.get(f'{provider}:{model_config.model}')
            charge = (price.charge(response.usage) if price is not None and response is not None
                      and response.usage_complete else None)
            cost_status = ('known' if charge is not None else 'unknown' if self._costs.prices
                           else 'pricing_not_configured')
            if self._costs.prices and run_id is not None and self._cost_store is not None:
                await self._cost_store.finish(call_id, charge, cost_status)
            elapsed_ms = (time.perf_counter() - started) * 1000
            trace = current_trace()
            role = str(trace.get("role", "unattributed"))
            self._metrics.incr("llm.attempts")
            self._metrics.observe("llm.call_ms", elapsed_ms)
            if response is not None:
                self._metrics.incr("llm.calls")
                for name, value in asdict(response.usage).items():
                    self._metrics.incr(f"llm.{name}", value)
            else:
                self._metrics.incr(f"llm.{status}")
            try:
                self._events.append_nowait(AgentEvent(
                    type=EventType.LLM_CALL,
                    session_id=str(trace.get("session_id", self._session_id)),
                    agent_run_id=str(trace["agent_run_id"]) if "agent_run_id" in trace else None,
                    payload={
                        "trace": trace,
                        "call_id": call_id,
                        "provider_call_id": response.llm_call_id if response else None,
                        "model": model_config.model, "role": role,
                        "provider": trace.get("provider"),
                        "started_at": started_at, "elapsed_ms": elapsed_ms,
                        "status": status, "error_type": error_type, "error_kind": error_kind,
                        "usage": asdict(response.usage) if response else None,
                        "cost_usd": usd(charge) if charge is not None else None,
                        "cost_pico_usd": str(charge) if charge is not None else None,
                        "cost_status": cost_status,
                        "pricing": asdict(price) if price is not None else None,
                    },
                ))
            except Exception:
                self._metrics.incr("observability.event_failures")

    async def count_tokens(
        self, messages: Sequence[Message], *, model_config: ModelConfig,
        tools: Sequence[ToolSpec] = (),
    ) -> int | None:
        return await self._client.count_tokens(messages, model_config=model_config, tools=tools)

    def effective_config(self, model_config: ModelConfig) -> ModelConfig:
        return effective_model_config(self._client, model_config)
