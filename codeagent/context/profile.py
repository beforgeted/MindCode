"""ContextProfile：所有阈值集中在此，且全部可配置。

上下文文档 §10 明确说比例"应通过 Benchmark 调整，而不是硬编码为最终结论"，
所以这里一个常量都不许散落到别的模块里。

不再用旧的 `window - summaryReserve - buffer` 公式（那个公式在 1M 窗口下
会到 96.7% 才触发压缩），改成三档比例 + 预测式触发。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ContextProfile:
    context_window: int = 200_000

    # 三档比例（P2 的压缩目标）
    soft_trigger_ratio: float = 0.80
    hard_trigger_ratio: float = 0.92
    target_ratio: float = 0.55

    # 预测式触发的加项（上下文文档 §11）
    output_reserve: int = 16_000
    safety_margin: int = 5_000
    expected_tool_burst: int = 20_000

    # 压缩参数（P2）
    retain_recent_turns: int = 3
    map_chunk_tokens: int = 20_000

    # Memory 注入预算（P3）
    max_memory_injection_tokens: int = 8_000

    # Tool 结果治理（P1）
    # tool 边界硬上限：超过这个数一律 offload 到 artifact
    max_tool_result_tokens: int = 6_000
    # 单条 tool 输出读取上限，防止 300K 输出把内存吃掉
    max_tool_output_bytes: int = 4 * 1024 * 1024
    # 生命周期：最近 N 个 turn 内的结果保持完整（HOT）
    tool_result_hot_turns: int = 2
    # HOT 之后、WARM 之内做轻度压缩
    tool_result_warm_turns: int = 6
    tool_result_warm_max_chars: int = 2_000
    # 更老的（COLD）只留结构化摘要 + artifact 引用
    tool_result_cold_max_chars: int = 400
    # 图片 payload 只在最近 N 个 turn 内保留本体
    image_payload_hot_turns: int = 1

    # 超时
    tool_timeout_seconds: float = 60.0
    agent_run_timeout_seconds: float = 300.0
    compaction_timeout_seconds: float = 120.0

    @property
    def soft_trigger(self) -> int:
        return int(self.context_window * self.soft_trigger_ratio)

    @property
    def hard_trigger(self) -> int:
        return int(self.context_window * self.hard_trigger_ratio)

    @property
    def target_after_compression(self) -> int:
        return int(self.context_window * self.target_ratio)

    def with_window(self, window: int) -> ContextProfile:
        from dataclasses import replace

        return replace(self, context_window=window)
