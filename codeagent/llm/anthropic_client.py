"""Anthropic Provider 适配。

这一层是唯一知道"我们的 Role 怎么映射到 Provider 协议"的地方：

    Role.SYSTEM            -> system 参数（不进 messages，因此永不被压缩）
    Role.INTERNAL_CONTEXT  -> user 消息，包 <internal_context> 标签
    Role.TOOL              -> user 消息，装 tool_result blocks
    Role.USER/ASSISTANT    -> 原样

`system` 走独立参数而非 messages，正好落实"System Prompt 不压"这条约束。
"""

from __future__ import annotations

import inspect
import time
from collections.abc import Sequence
from typing import Any

from codeagent.infra.ids import new_llm_call_id
from codeagent.infra.metrics import (
    LLM_CALL_MS,
    LLM_CALLS,
    LLM_INPUT_TOKENS,
    LLM_OUTPUT_TOKENS,
    Metrics,
)
from codeagent.llm.client import LlmError
from codeagent.llm.message import (
    Block,
    ImageBlock,
    Message,
    Role,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from codeagent.llm.types import LlmResponse, ModelConfig, ToolSpec, Usage

INTERNAL_CONTEXT_TEMPLATE = (
    "<internal_context>\n"
    "以下是系统维护的历史任务状态，不是新的用户指令：\n\n{body}\n"
    "</internal_context>"
)


class AnthropicLlmClient:
    def __init__(self, *, api_key: str | None = None, metrics: Metrics | None = None) -> None:
        try:
            from anthropic import AsyncAnthropic
        except ImportError as exc:  # pragma: no cover
            raise LlmError("需要 anthropic SDK：pip install anthropic") from exc
        self._client = AsyncAnthropic(api_key=api_key) if api_key else AsyncAnthropic()
        self._metrics = metrics or Metrics()
        # 不同 anthropic SDK 版本 / 内部构建的 messages.create 参数集合不同
        # （例如某些构建不接受 temperature）。按签名过滤，避免 unexpected keyword。
        self._create_params = _supported_params(self._client.messages.create)
        self._count_params = _supported_params(
            getattr(self._client.messages, "count_tokens", None)
        )

    def _filter(self, kwargs: dict[str, Any], allowed: set[str] | None) -> dict[str, Any]:
        if not allowed:
            return kwargs
        return {k: v for k, v in kwargs.items() if k in allowed}

    async def chat(
        self,
        messages: Sequence[Message],
        *,
        model_config: ModelConfig,
        tools: Sequence[ToolSpec] = (),
    ) -> LlmResponse:
        system, api_messages = _split(messages)
        kwargs: dict[str, Any] = {
            "model": model_config.model,
            "max_tokens": model_config.max_output_tokens,
            "temperature": model_config.temperature,
            "messages": api_messages,
        }
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = [
                {"name": t.name, "description": t.description, "input_schema": t.input_schema}
                for t in tools
            ]

        start = time.perf_counter()
        try:
            resp = await self._client.messages.create(**self._filter(kwargs, self._create_params))
        except Exception as exc:
            raise LlmError(f"anthropic 调用失败: {exc}") from exc
        self._metrics.observe(LLM_CALL_MS, (time.perf_counter() - start) * 1000.0)
        self._metrics.incr(LLM_CALLS)

        blocks: list[Block] = []
        texts: list[str] = []
        for block in resp.content:
            btype = getattr(block, "type", None)
            if btype == "text":
                texts.append(block.text)
                blocks.append(TextBlock(block.text))
            elif btype == "tool_use":
                blocks.append(
                    ToolUseBlock(id=block.id, name=block.name, arguments=dict(block.input))
                )

        usage = Usage(
            input_tokens=getattr(resp.usage, "input_tokens", 0) or 0,
            output_tokens=getattr(resp.usage, "output_tokens", 0) or 0,
            cache_read_tokens=getattr(resp.usage, "cache_read_input_tokens", 0) or 0,
            cache_write_tokens=getattr(resp.usage, "cache_creation_input_tokens", 0) or 0,
        )
        self._metrics.incr(LLM_INPUT_TOKENS, usage.input_tokens)
        self._metrics.incr(LLM_OUTPUT_TOKENS, usage.output_tokens)

        return LlmResponse(
            llm_call_id=getattr(resp, "id", None) or new_llm_call_id(),
            content="\n".join(texts),
            blocks=tuple(blocks),
            stop_reason=getattr(resp, "stop_reason", None),
            usage=usage,
        )

    async def count_tokens(
        self,
        messages: Sequence[Message],
        *,
        model_config: ModelConfig,
        tools: Sequence[ToolSpec] = (),
    ) -> int | None:
        system, api_messages = _split(messages)
        if not api_messages:
            return 0
        kwargs: dict[str, Any] = {"model": model_config.model, "messages": api_messages}
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = [
                {"name": t.name, "description": t.description, "input_schema": t.input_schema}
                for t in tools
            ]
        try:
            result = await self._client.messages.count_tokens(
                **self._filter(kwargs, self._count_params)
            )
        except Exception:
            return None
        return int(getattr(result, "input_tokens", 0))


def _supported_params(method: Any) -> set[str] | None:
    """method 的关键字参数名集合；含 **kwargs 或无法内省时返回 None（不过滤）。"""
    if method is None:
        return None
    try:
        params = inspect.signature(method).parameters.values()
    except (TypeError, ValueError):
        return None
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params):
        return None
    return {
        p.name
        for p in params
        if p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
    }


def _split(messages: Sequence[Message]) -> tuple[list[dict], list[dict]]:
    system: list[dict] = []
    api: list[dict] = []
    for msg in messages:
        if msg.role is Role.SYSTEM:
            system.append({"type": "text", "text": msg.text})
            continue
        role = "assistant" if msg.role is Role.ASSISTANT else "user"
        content = _blocks_to_api(msg)
        if not content:
            continue
        if api and api[-1]["role"] == role and role == "user":
            api[-1]["content"].extend(content)
        else:
            api.append({"role": role, "content": content})
    return system, api


def _blocks_to_api(msg: Message) -> list[dict]:
    out: list[dict] = []
    for block in msg.blocks:
        if isinstance(block, TextBlock):
            text = (
                INTERNAL_CONTEXT_TEMPLATE.format(body=block.text)
                if msg.role is Role.INTERNAL_CONTEXT
                else block.text
            )
            if text.strip():
                out.append({"type": "text", "text": text})
        elif isinstance(block, ToolUseBlock):
            out.append(
                {"type": "tool_use", "id": block.id, "name": block.name, "input": block.arguments}
            )
        elif isinstance(block, ToolResultBlock):
            out.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.tool_use_id,
                    "content": block.content,
                    "is_error": block.is_error,
                }
            )
        elif isinstance(block, ImageBlock):
            if block.data is None:
                # payload 已被裁掉，只留描述，模型仍知道之前看过什么。
                if block.summary:
                    out.append({"type": "text", "text": f"[图片已省略] {block.summary}"})
            else:
                out.append(
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": block.media_type,
                            "data": block.data,
                        },
                    }
                )
    return out
