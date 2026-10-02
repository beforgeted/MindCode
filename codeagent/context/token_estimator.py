"""TokenEstimator：全系统唯一的 token 估算入口。

P0 决策（上下文文档 §9）：不允许存在第二套估算规则。任何需要 token 数的地方
都走这个接口。

Python 特有的双轨设计：
- 热路径用启发式，结果缓存在 Message 上（历史每轮全量重估是 O(n^2)）；
- 精确计数（Anthropic 的 count_tokens）是**网络调用**，只在压缩决策边界
  和校准时用。CalibratedTokenEstimator 用精确值反过来修正启发式的系数。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import time
from collections import OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from typing import Protocol, runtime_checkable

from codeagent.context.calibration_config import CalibrationConfig
from codeagent.llm.client import LlmClient
from codeagent.llm.message import ImageBlock, Message, TextBlock, ToolResultBlock, ToolUseBlock
from codeagent.llm.types import ModelConfig, ToolSpec

# 中文≈1 token/字，英文/代码≈3.6 字符/token。
_CJK_TOKENS_PER_CHAR = 1.0
_LATIN_CHARS_PER_TOKEN = 3.6
_MESSAGE_OVERHEAD = 8
_TOOL_USE_OVERHEAD = 16
# 没有图片尺寸时的保守估计（Anthropic 约 (w*h)/750）。
_IMAGE_FALLBACK_TOKENS = 1600


def _is_cjk(ch: str) -> bool:
    code = ord(ch)
    return (
        0x3000 <= code <= 0x303F
        or 0x3040 <= code <= 0x30FF
        or 0x3400 <= code <= 0x4DBF
        or 0x4E00 <= code <= 0x9FFF
        or 0xF900 <= code <= 0xFAFF
        or 0xFF00 <= code <= 0xFFEF
    )


def estimate_text(text: str) -> int:
    if not text:
        return 0
    cjk = sum(1 for ch in text if _is_cjk(ch))
    latin = len(text) - cjk
    return int(cjk * _CJK_TOKENS_PER_CHAR + latin / _LATIN_CHARS_PER_TOKEN) + 1


@runtime_checkable
class TokenEstimator(Protocol):
    def estimate_message(self, message: Message) -> int: ...

    def estimate(self, messages: Sequence[Message]) -> int: ...

    async def count_exact(
        self,
        messages: Sequence[Message],
        *,
        model_config: ModelConfig,
        tools: Sequence[ToolSpec] = (),
    ) -> int | None: ...


class HeuristicTokenEstimator:
    def estimate_message(self, message: Message) -> int:
        if message.token_estimate is not None:
            return message.token_estimate
        total = _MESSAGE_OVERHEAD
        for block in message.blocks:
            if isinstance(block, TextBlock):
                total += estimate_text(block.text)
            elif isinstance(block, ToolUseBlock):
                total += _TOOL_USE_OVERHEAD + estimate_text(block.name)
                total += estimate_text(repr(block.arguments))
            elif isinstance(block, ToolResultBlock):
                total += estimate_text(block.content) + 8
            elif isinstance(block, ImageBlock):
                if block.data is None:
                    total += estimate_text(block.summary or "")
                else:
                    # base64 长度 / 750 是对像素数的粗略反推
                    total += min(_IMAGE_FALLBACK_TOKENS, max(200, len(block.data) // 750))
        message.token_estimate = total
        return total

    def estimate(self, messages: Sequence[Message]) -> int:
        return sum(self.estimate_message(m) for m in messages)

    async def count_exact(
        self,
        messages: Sequence[Message],
        *,
        model_config: ModelConfig,
        tools: Sequence[ToolSpec] = (),
    ) -> int | None:
        return None


def estimate_tools(tools: Sequence[ToolSpec]) -> int:
    return sum(estimate_text(json.dumps(asdict(t), ensure_ascii=False, sort_keys=True))
               for t in tools)


def estimator_for_model(estimator: TokenEstimator, config: ModelConfig,
                        tools: Sequence[ToolSpec] = ()) -> TokenEstimator:
    bind = getattr(estimator, 'for_model', None)
    value = bind(config, tools) if callable(bind) else estimator
    if not isinstance(value, TokenEstimator):
        raise TypeError('for_model must return TokenEstimator')
    return value


def estimate_request(estimator: TokenEstimator, messages: Sequence[Message],
                     config: ModelConfig, tools: Sequence[ToolSpec] = ()) -> int:
    view = estimator_for_model(estimator, config, tools)
    full_request = getattr(view, 'estimate_request', None)
    if callable(full_request):
        value = full_request(messages, tools)
        if type(value) is not int or value < 0:
            raise TypeError('estimate_request must return a nonnegative integer')
        return value
    tool_estimate = getattr(view, 'estimate_tools', estimate_tools)
    return view.estimate(messages) + tool_estimate(tools)


def client_estimator(client: LlmClient, config: ModelConfig) -> TokenEstimator:
    factory = getattr(client, 'token_estimator', None)
    value = factory(config) if callable(factory) else HeuristicTokenEstimator()
    if not isinstance(value, TokenEstimator):
        raise TypeError('token_estimator must return TokenEstimator')
    return value


@dataclass
class _Calibration:
    scale: float = 1.0
    smoothed: float = 1.0
    calls: int = 0
    next_sample: float = 0.0
    unsupported: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class CalibratedTokenEstimator:
    """Session-local, bounded cohorts; cached Message values always stay unscaled.

    Exact samples only raise a conservative high-water multiplier. They do not
    certify other requests, reduce reserves, or constitute a billing hard limit.
    """

    def __init__(self, client: LlmClient, *, base: TokenEstimator | None = None,
                 smoothing: float = 0.3, config: CalibrationConfig | None = None,
                 record: Callable[[dict], None] | None = None) -> None:
        if not math.isfinite(smoothing) or not 0 < smoothing <= 1:
            raise ValueError('smoothing must be in (0, 1]')
        self._client = client
        self._base = base or HeuristicTokenEstimator()
        self._smoothing = smoothing
        self.config = config or CalibrationConfig()
        self._record = record
        self._states: dict[tuple[str, str], _Calibration] = {}
        self._cache: OrderedDict[tuple[tuple[str, str], str], int] = OrderedDict()
        self.calls = 0
        self.calibrations = 0

    def for_model(self, config: ModelConfig, tools: Sequence[ToolSpec] = ()) -> ModelTokenEstimator:
        return ModelTokenEstimator(self, config, tuple(tools))

    def estimate_message(self, message: Message) -> int:
        return self._base.estimate_message(message)

    def estimate(self, messages: Sequence[Message]) -> int:
        return self._base.estimate(messages)

    @staticmethod
    def _key(config: ModelConfig, messages: Sequence[Message], tools: Sequence[ToolSpec]):
        images = any(isinstance(b, ImageBlock) and b.data is not None
                     for m in messages for b in m.blocks)
        tool_protocol = bool(tools) or any(m.tool_uses or m.tool_results for m in messages)
        return config.model, f'v1:tools={tool_protocol}:images={images}'

    def scale_for(self, config: ModelConfig, messages: Sequence[Message],
                  tools: Sequence[ToolSpec] = ()) -> float:
        state = self._states.get(self._key(config, messages, tools))
        return state.scale if state is not None and self.config.enabled else 1.0

    async def sample(self, messages: Sequence[Message], *, model_config: ModelConfig,
                     tools: Sequence[ToolSpec] = (), client: LlmClient | None = None,
                     count_config: ModelConfig | None = None, force: bool = False) -> int | None:
        raw = self._base.estimate(messages) + estimate_tools(tools)
        key = self._key(model_config, messages, tools)
        state = self._states.get(key)
        if state is not None and state.lock.locked():
            async with state.lock:
                pass
        estimated = math.ceil(raw * self.scale_for(model_config, messages, tools))
        if not self.config.enabled or (not force and
                (estimated + model_config.max_output_tokens <
                 model_config.context_window * self.config.boundary_ratio
                 or estimated + model_config.max_output_tokens >= model_config.context_window)):
            return None
        if state is None:
            if len(self._states) >= self.config.max_cohorts:
                return None  # Do not evict a learned conservative bound.
            state = self._states[key] = _Calibration()
        if state.unsupported:
            return None
        digest = hashlib.sha256()
        for message in messages:
            digest.update(json.dumps((str(message.role), [asdict(b) for b in message.blocks]),
                                     ensure_ascii=False, sort_keys=True).encode())
        digest.update(json.dumps([asdict(t) for t in tools],
                                 ensure_ascii=False, sort_keys=True).encode())
        cached_key = key, digest.hexdigest()
        if cached_key in self._cache:
            self._cache.move_to_end(cached_key)
            return self._cache[cached_key]
        if (time.monotonic() < state.next_sample
                or state.calls >= self.config.max_calls_per_cohort
                or self.calls >= self.config.max_total_calls):
            return None
        # No await before reserving the global and cohort call budgets.
        async with state.lock:
            state.calls += 1
            self.calls += 1
            state.next_sample = time.monotonic() + self.config.min_interval_seconds
            result = {'model': model_config.model, 'format': key[1], 'raw': raw,
                      'estimated': estimated, 'exact': None, 'scale': state.scale,
                      'call': self.calls}
            try:
                async with asyncio.timeout(self.config.timeout_seconds):
                    exact = await (client or self._client).count_tokens(
                        messages, model_config=count_config or model_config, tools=tools)
                if exact is None:
                    state.unsupported = True
                    result['status'] = 'unsupported'
                    return None
                if type(exact) is not int or exact < 0 or (raw > 0 and exact == 0):
                    result['status'] = 'invalid'
                    return None
                if raw > 0:
                    observed = exact / raw
                    state.smoothed = ((1 - self._smoothing) * state.smoothed
                                      + self._smoothing * observed)
                    state.scale = max(state.scale, 1.0,
                                      observed * (1 + self.config.safety_ratio))
                    self.calibrations += 1
                self._cache[cached_key] = exact
                if len(self._cache) > self.config.cache_size:
                    self._cache.popitem(last=False)
                result.update(status='success', exact=exact, deviation=exact - estimated,
                              scale=state.scale,
                              smoothed_scale=state.smoothed)
                return exact
            except asyncio.CancelledError:
                result['status'] = 'cancelled'
                raise
            except TimeoutError:
                result['status'] = 'timeout'
                return None
            except Exception as exc:
                result.update(status='error', error_type=type(exc).__name__)
                return None
            finally:
                if self._record is not None:
                    try:
                        self._record(result)
                    except Exception:
                        pass  # Observability must not change the admission verdict.

    async def count_exact(self, messages: Sequence[Message], *, model_config: ModelConfig,
                          tools: Sequence[ToolSpec] = ()) -> int | None:
        return await self.sample(messages, model_config=model_config, tools=tools, force=True)


class ModelTokenEstimator:
    def __init__(self, owner: CalibratedTokenEstimator, config: ModelConfig,
                 tools: tuple[ToolSpec, ...] = (),
                 resolve: Callable[[], ModelConfig] | None = None):
        self.owner, self.config, self.tools, self.resolve = owner, config, tools, resolve

    def _config(self) -> ModelConfig:
        return self.resolve() if self.resolve is not None else self.config

    def for_model(self, config: ModelConfig, tools: Sequence[ToolSpec] = ()) -> ModelTokenEstimator:
        return self.owner.for_model(config, tools)

    def estimate_message(self, message: Message) -> int:
        return math.ceil(self.owner._base.estimate_message(message) *
                         self.owner.scale_for(self._config(), (message,), self.tools))

    def estimate(self, messages: Sequence[Message]) -> int:
        # Scale by the whole request's protocol, including tool declarations.
        scale = self.owner.scale_for(self._config(), messages, self.tools)
        return sum(math.ceil(self.owner._base.estimate_message(m) * scale)
                   for m in messages)

    def estimate_tools(self, tools: Sequence[ToolSpec]) -> int:
        return math.ceil(estimate_tools(tools) *
                         self.owner.scale_for(self._config(), (), tools))

    def estimate_request(self, messages: Sequence[Message], tools: Sequence[ToolSpec]) -> int:
        return math.ceil((self.owner._base.estimate(messages) + estimate_tools(tools)) *
                         self.owner.scale_for(self._config(), messages, tools))

    async def count_exact(self, messages: Sequence[Message], *, model_config: ModelConfig,
                          tools: Sequence[ToolSpec] = ()) -> int | None:
        return await self.owner.count_exact(messages, model_config=model_config, tools=tools)
