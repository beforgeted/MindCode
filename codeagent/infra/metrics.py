"""最小可观测性。

上下文文档 §10 说压缩比例"应通过 Benchmark 调整，而不是硬编码为最终结论"
—— 没有度量就没法 benchmark，所以这个模块在 P0 就要有，且被
ContextManager / ToolExecutionManager 无条件写入。
"""

from __future__ import annotations

import time
from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field


@dataclass
class Metrics:
    counters: dict[str, float] = field(default_factory=lambda: defaultdict(float))
    gauges: dict[str, float] = field(default_factory=dict)
    histograms: dict[str, list[float]] = field(default_factory=lambda: defaultdict(list))

    def incr(self, name: str, value: float = 1.0) -> None:
        self.counters[name] += value

    def gauge(self, name: str, value: float) -> None:
        self.gauges[name] = value

    def observe(self, name: str, value: float) -> None:
        self.histograms[name].append(value)

    @contextmanager
    def timer(self, name: str) -> Iterator[None]:
        start = time.perf_counter()
        try:
            yield
        finally:
            self.observe(name, (time.perf_counter() - start) * 1000.0)

    def snapshot(self) -> dict[str, dict[str, float]]:
        hist: dict[str, float] = {}
        for name, values in self.histograms.items():
            if not values:
                continue
            ordered = sorted(values)
            hist[f"{name}.count"] = float(len(ordered))
            hist[f"{name}.avg"] = sum(ordered) / len(ordered)
            hist[f"{name}.p95"] = ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]
        return {
            "counters": dict(self.counters),
            "gauges": dict(self.gauges),
            "histograms": hist,
        }

    def reset(self) -> None:
        self.counters.clear()
        self.gauges.clear()
        self.histograms.clear()


# 约定的指标名，集中在此避免拼写漂移。
CONTEXT_TOKENS_BEFORE = "context.tokens.before"
CONTEXT_TOKENS_AFTER_PRUNE = "context.tokens.after_prune"
CONTEXT_TOKENS_AFTER_COMPACT = "context.tokens.after_compact"
CONTEXT_IMAGE_TOKENS_REMOVED = "context.image_tokens_removed"
CONTEXT_TOOL_TOKENS_REMOVED = "context.tool_tokens_removed"
CONTEXT_PREPARE_MS = "context.prepare_ms"
CONTEXT_COMPACTION_COUNT = "context.compaction.count"
CONTEXT_COMPACTION_SKIPPED = "context.compaction.skipped_no_compactor"

TOOL_RUNS = "tool.runs"
TOOL_ERRORS = "tool.errors"
TOOL_TIMEOUTS = "tool.timeouts"
TOOL_CANCELLED = "tool.cancelled"
TOOL_NORMALIZED = "tool.normalized"
TOOL_OFFLOADED_TOKENS = "tool.offloaded_tokens"
TOOL_EXEC_MS = "tool.exec_ms"

LLM_CALLS = "llm.calls"
LLM_INPUT_TOKENS = "llm.input_tokens"
LLM_OUTPUT_TOKENS = "llm.output_tokens"
LLM_CALL_MS = "llm.call_ms"

REACT_ITERATIONS = "react.iterations"
