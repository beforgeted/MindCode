"""Trace ID 体系。

日志与事件的核心标识不是 worker-1 / worker-2，而是：

    session_id
      └── agent_run_id
           ├── llm_call_id
           ├── tool_run_id
           └── turn_id
"""

from __future__ import annotations

from contextvars import ContextVar
from uuid import uuid4


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:12]}"


def new_session_id() -> str:
    return new_id("ses")


def new_agent_run_id() -> str:
    return new_id("run")


def new_tool_run_id() -> str:
    return new_id("tr")


def new_llm_call_id() -> str:
    return new_id("llm")


def new_turn_id() -> str:
    return new_id("turn")


def new_event_id() -> str:
    return new_id("ev")


def new_artifact_id() -> str:
    return new_id("art")


# 仅用于日志装饰，不作为业务参数传递。业务参数一律显式传入，
# 避免 Tool 从隐式全局状态里找当前 run。
current_session_id: ContextVar[str | None] = ContextVar("current_session_id", default=None)
current_agent_run_id: ContextVar[str | None] = ContextVar("current_agent_run_id", default=None)
