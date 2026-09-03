"""`/context` 渲染。

上下文文档 §31 / 记忆 V2 §35 想要的效果：用户能真正看见 token 花在哪里、
为什么会触发 Compact。Compaction 不能是个不可观察的黑盒。
"""

from __future__ import annotations

from codeagent.context.manager import ContextPreparationResult
from codeagent.context.profile import ContextProfile
from codeagent.infra.metrics import Metrics
from codeagent.llm.message import ContextCategory

_LABELS = {
    ContextCategory.SYSTEM: "System / Rules",
    ContextCategory.CHECKPOINT: "TaskCheckpoint",
    ContextCategory.MEMORY: "Injected Memory",
    ContextCategory.CONVERSATION: "Recent Conversation",
    ContextCategory.TOOL_RESULT: "Tool Results",
    ContextCategory.IMAGE: "Images",
    ContextCategory.OTHER: "Other",
}


def render_context_report(
    prepared: ContextPreparationResult | None,
    profile: ContextProfile,
    metrics: Metrics,
    *,
    message_count: int = 0,
    compaction_count: int = 0,
) -> str:
    lines: list[str] = []
    add = lines.append

    add(f"Context Window          {profile.context_window:>10,}")
    if prepared is None:
        add("尚未发起任何 LLM 调用。")
        return "\n".join(lines)

    p = prepared.prediction
    add(f"Current Input           {p.current_tokens:>10,}   ({p.usage_ratio:.1%})")
    add(f"Predicted Next Round    {p.predicted_tokens:>10,}")
    add(f"Soft Trigger            {p.soft_trigger:>10,}   ({profile.soft_trigger_ratio:.0%})")
    add(f"Hard Trigger            {p.hard_trigger:>10,}   ({profile.hard_trigger_ratio:.0%})")
    add(
        f"Target After Compact    {profile.target_after_compression:>10,}"
        f"   ({profile.target_ratio:.0%})"
    )
    add("")
    add(f"触发判定  {p.reason}")
    add("")

    add("Breakdown")
    add("-" * 46)
    total = sum(prepared.breakdown.values()) or 1
    for category, tokens in sorted(prepared.breakdown.items(), key=lambda kv: -kv[1]):
        label = _LABELS.get(category, str(category))
        add(f"{label:<24}{tokens:>10,}   {tokens / total:>6.1%}")
    add("-" * 46)
    add(f"{'Total':<24}{sum(prepared.breakdown.values()):>10,}")
    add("")

    add("本轮裁剪")
    add(f"  图片 payload 释放      {prepared.image_tokens_removed:>10,} tokens")
    add(f"  tool 结果降级释放      {prepared.tool_tokens_removed:>10,} tokens")
    add(f"  裁剪前 -> 裁剪后        {prepared.tokens_before:,} -> {prepared.tokens_after_prune:,}")

    if prepared.compaction is not None:
        c = prepared.compaction
        add("")
        add("Compaction")
        add(f"  已压缩        {c.compacted}")
        add(f"  释放          {c.tokens_released:,} tokens")
        add(f"  map chunks    {c.map_chunks}")
        add(f"  说明          {c.reason}")

    add("")
    add(f"消息条数 {message_count}   累计 compact {compaction_count} 次")

    snapshot = metrics.snapshot()
    tool_runs = int(snapshot["counters"].get("tool.runs", 0))
    if tool_runs:
        add("")
        add(
            f"工具  调用 {tool_runs}  "
            f"错误 {int(snapshot['counters'].get('tool.errors', 0))}  "
            f"超时 {int(snapshot['counters'].get('tool.timeouts', 0))}  "
            f"归一化 {int(snapshot['counters'].get('tool.normalized', 0))}  "
            f"offload 释放 {int(snapshot['counters'].get('tool.offloaded_tokens', 0)):,} tokens"
        )
    return "\n".join(lines)
