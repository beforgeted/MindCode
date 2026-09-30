"""仅用于观测的协程上下文；不参与调度、授权或恢复决策。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_trace: ContextVar[dict[str, str | int] | None] = ContextVar("observation_trace", default=None)


def current_trace() -> dict[str, str | int]:
    return dict(_trace.get() or {})


def update_trace(**values: str | int) -> None:
    _trace.set({**current_trace(), **values})


@contextmanager
def trace_scope(**values: str | int) -> Iterator[None]:
    token = _trace.set({**current_trace(), **values})
    try:
        yield
    finally:
        _trace.reset(token)
