"""Role routing, bounded fallback and explicitly configured Worker cost thresholds."""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Protocol

from codeagent.context.token_estimator import HeuristicTokenEstimator, estimate_text
from codeagent.evidence.event_store import RawEventStore
from codeagent.evidence.models import AgentEvent, EventType
from codeagent.infra.ids import new_id
from codeagent.infra.metrics import Metrics
from codeagent.infra.trace import current_trace, trace_scope
from codeagent.llm.client import LlmClient, LlmError, LlmErrorKind
from codeagent.llm.message import Message
from codeagent.llm.observed_client import ObservedLlmClient
from codeagent.llm.pricing import CostConfig, amount, usd
from codeagent.llm.types import LlmResponse, ModelConfig, ToolSpec
from codeagent.orchestration.cost_store import CostStore, cost_run


class ModelRole(StrEnum):
    PLANNER = "planner"
    WORKER = "worker"
    LOCAL_VERIFIER = "local_verifier"
    GLOBAL_VERIFIER = "global_verifier"
    JUDGE = "judge"
    COMPACT_MAP = "compact_map"
    COMPACT_REDUCE = "compact_reduce"


@dataclass(frozen=True)
class ModelRoutingConfig:
    # None 保留调用方的 ModelConfig（包含原有 map_model 选择）。
    planner: str | None = None
    worker: str | None = None
    local_verifier: str | None = None
    global_verifier: str | None = None
    judge: str | None = None
    compact_map: str | None = None
    compact_reduce: str | None = None
    fallback: tuple[str, ...] = ()
    fallbacks: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for role in ModelRole:
            name = getattr(self, role.value)
            if name is not None:
                _validate_name(name)
        for role, chain in self.fallbacks.items():
            ModelRole(role)
            self._validate_chain(chain)
        self._validate_chain(self.fallback)

    @staticmethod
    def _validate_chain(chain: tuple[str, ...]) -> None:
        if len(chain) > 3:
            raise ValueError("最多配置 3 个备用模型")
        for name in chain:
            _validate_name(name)

    @classmethod
    def from_env(cls) -> ModelRoutingConfig:
        names = {role.value: os.environ.get(f"CODEAGENT_MODEL_{role.name}", "").strip() or None
                 for role in ModelRole}
        fallbacks = {}
        for role in ModelRole:
            key = f"CODEAGENT_MODEL_FALLBACK_{role.name}"
            if key in os.environ:
                fallbacks[role.value] = _chain(os.environ[key])
        return cls(**names, fallback=_chain(os.environ.get("CODEAGENT_MODEL_FALLBACK", "")),
                   fallbacks=fallbacks)


def _chain(raw: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in raw.split(";") if part.strip())


def _validate_name(name: str) -> None:
    if not name or name != name.strip() or any(c.isspace() for c in name):
        raise ValueError("模型名称不能为空或包含空白")
    if ":" in name:
        provider, _, model = name.partition(":")
        if not provider or not model:
            raise ValueError("模型前缀格式为 provider:model")


class ModelRouter(Protocol):
    def resolve(self, role: ModelRole | None, *, base: ModelConfig) -> ModelConfig: ...

    def candidates(
        self, role: ModelRole | None, *, base: ModelConfig,
    ) -> tuple[ModelConfig, ...]: ...


class StaticModelRouter:
    def __init__(self, config: ModelRoutingConfig) -> None:
        self.config = config

    def resolve(self, role: ModelRole | None, *, base: ModelConfig) -> ModelConfig:
        name = getattr(self.config, role.value) if role is not None else None
        return replace(base, model=name) if name else base

    def candidates(self, role: ModelRole | None, *, base: ModelConfig) -> tuple[ModelConfig, ...]:
        primary = self.resolve(role, base=base)
        chain = self.config.fallbacks.get(role.value, self.config.fallback) if role else ()
        return (primary, *(replace(primary, model=name) for name in chain))


_RECOVERABLE = {LlmErrorKind.RATE_LIMIT, LlmErrorKind.TIMEOUT, LlmErrorKind.UNAVAILABLE}


class RoutingLlmClient:
    def __init__(
        self, providers: Mapping[str, LlmClient], router: ModelRouter, *,
        default_provider: str = "anthropic", metrics: Metrics | None = None,
        events: RawEventStore | None = None,
        session_id: str = "",
        costs: CostConfig | None = None, cost_store: CostStore | None = None,
    ) -> None:
        self.providers = dict(providers)
        self.router = router
        self.default_provider = default_provider
        self._metrics, self._events = metrics, events
        self._session_id = session_id
        self.costs = costs or CostConfig()
        self.cost_store = cost_store

    async def _budget_target(self, role, targets, messages, tools):
        if self.costs.worker_threshold_usd is None or role != ModelRole.WORKER:
            return targets
        run_id = cost_run()
        if run_id is None:
            return targets  # This phase applies only to explicitly scoped Master runs.
        if self.cost_store is None:
            raise LlmError('预算路由缺少持久化成本存储', kind=LlmErrorKind.CONFIGURATION)
        total, unknown = await self.cost_store.total(run_id)
        if total < amount(self.costs.worker_threshold_usd) and not unknown:
            return targets
        assert self.costs.economy_worker is not None
        price = self.costs.prices[self.costs.economy_worker]
        assert price.context_window is not None and price.max_output_tokens is not None
        primary = targets[0][2]
        estimated = HeuristicTokenEstimator().estimate(messages) + sum(
            estimate_text(repr((tool.name, tool.description, tool.input_schema))) for tool in tools)
        output_limit = min(primary.max_output_tokens, price.max_output_tokens)
        reason = 'cost_unknown' if unknown else 'cost_threshold'
        if tools and not price.tools:
            reason = 'economy_tools_unsupported'
        elif estimated + output_limit >= price.context_window:
            reason = 'economy_context_too_small'
        else:
            economy = self._target(replace(primary, model=self.costs.economy_worker,
                                           context_window=price.context_window,
                                           max_output_tokens=output_limit))
            targets = [economy, *(t for t in targets[1:]
                                   if (t[0], t[2].model) != (economy[0], economy[2].model))]
        if self._events is not None:
            try:
                self._events.append_nowait(AgentEvent(
                    EventType.MODEL_BUDGET_ROUTE,
                    str(current_trace().get('session_id', self._session_id)),
                    payload={'trace': current_trace(), 'reason': reason,
                             'known_cost_usd': usd(total), 'unknown_cost_calls': unknown,
                             'estimated_input_tokens': estimated,
                             'selected_provider': targets[0][0],
                             'selected_model': targets[0][2].model},
                ))
            except Exception:
                if self._metrics is not None:
                    self._metrics.incr('observability.event_failures')
        return targets

    def _target(self, config: ModelConfig) -> tuple[str, LlmClient, ModelConfig]:
        _validate_name(config.model)
        provider, separator, model = config.model.partition(":")
        if not separator:
            provider, model = self.default_provider, config.model
        client = self.providers.get(provider)
        if client is None:
            raise LlmError(f"未注册模型 Provider: {provider}", kind=LlmErrorKind.CONFIGURATION)
        return provider, client, replace(config, model=model)

    @staticmethod
    def _role() -> ModelRole | None:
        try:
            return ModelRole(current_trace().get("role"))
        except (ValueError, TypeError):
            return None

    async def chat(
        self, messages: Sequence[Message], *, model_config: ModelConfig,
        tools: Sequence[ToolSpec] = (),
    ) -> LlmResponse:
        if self.costs.economy_worker is not None:
            self._target(replace(model_config, model=self.costs.economy_worker))
        targets = []
        seen = set()
        # 先验证完整路由，配置错误不能在已经调用主模型后才暴露。
        for config in self.router.candidates(self._role(), base=model_config):
            provider, client, effective = self._target(config)
            key = (provider, effective.model)
            if key not in seen:
                seen.add(key)
                targets.append((provider, client, effective))
        if len(targets) > 4:
            raise LlmError("最多允许 4 个候选模型", kind=LlmErrorKind.CONFIGURATION)
        targets = await self._budget_target(self._role(), targets, messages, tools)
        route_id = new_id("route")
        for index, (provider, client, config) in enumerate(targets):
            with trace_scope(route_id=route_id, route_attempt=index + 1, provider=provider):
                try:
                    return await client.chat(messages, model_config=config, tools=tools)
                except LlmError as exc:
                    if exc.kind not in _RECOVERABLE or index + 1 == len(targets):
                        raise
                    self._record_fallback(config.model, targets[index + 1], exc.kind)
        raise LlmError("模型路由为空", kind=LlmErrorKind.CONFIGURATION)

    def _record_fallback(self, model: str, next_target: tuple, kind: LlmErrorKind) -> None:
        if self._metrics is not None:
            self._metrics.incr("llm.fallbacks")
        if self._events is not None:
            trace = current_trace()
            try:
                self._events.append_nowait(AgentEvent(
                    EventType.MODEL_FALLBACK, str(trace.get("session_id", self._session_id)),
                    payload={"trace": trace, "from_model": model,
                             "to_provider": next_target[0], "to_model": next_target[2].model,
                             "reason": kind},
                ))
            except Exception:
                if self._metrics is not None:
                    self._metrics.incr("observability.event_failures")

    async def count_tokens(
        self, messages: Sequence[Message], *, model_config: ModelConfig,
        tools: Sequence[ToolSpec] = (),
    ) -> int | None:
        config = self.router.resolve(self._role(), base=model_config)
        _, client, config = self._target(config)
        return await client.count_tokens(messages, model_config=config, tools=tools)


def attach_routing(
    client: LlmClient, config: ModelRoutingConfig, *, metrics: Metrics,
    events: RawEventStore, session_id: str,
    costs: CostConfig | None = None, cost_store: CostStore | None = None,
) -> RoutingLlmClient:
    if isinstance(client, RoutingLlmClient):
        providers, default_provider = client.providers, client.default_provider
        router = client.router if config == ModelRoutingConfig() else StaticModelRouter(config)
    else:
        providers, default_provider = {"anthropic": client}, "anthropic"
        router = StaticModelRouter(config)
    if costs is None and isinstance(client, RoutingLlmClient):
        costs, cost_store = client.costs, client.cost_store
    observed = {key: ObservedLlmClient(value, metrics=metrics, events=events, session_id=session_id,
                                     costs=costs, cost_store=cost_store)
                for key, value in providers.items()}
    return RoutingLlmClient(observed, router, default_provider=default_provider,
                            metrics=metrics, events=events, session_id=session_id,
                            costs=costs, cost_store=cost_store)
