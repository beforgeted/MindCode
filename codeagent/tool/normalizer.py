"""ToolResultNormalizer：tool 边界的有界化兜底。

为什么这一层必须先于 HistoryCompactor 落地（这是我对文档排期的一处调整）：

上下文文档 §29 已经承认"单条 300K tool 输出"Compactor 解决不了；而 §17 的
token-aware chunking 又要求"保持 Turn 原子性"。如果一个 Turn 里躺着 300K 的
tool result，就永远切不出 ≤20K 的 chunk —— 也就是说 Normalizer 不是优化项，
而是 token-aware chunking 能够成立的**前提**。

产物形状（对应上下文文档 §12.2）：

    tool: execute_command
    exitCode: 1
    tests: passed=127 failed=1
    keyErrors:
      - expected balance=80
      - actual balance=70
    完整内容: artifact://tool-results/art_xxx
    <有界预览>
"""

from __future__ import annotations

from codeagent.context.profile import ContextProfile
from codeagent.context.token_estimator import TokenEstimator, estimate_text
from codeagent.evidence.artifact_store import ArtifactStore
from codeagent.infra import metrics as M
from codeagent.infra.metrics import Metrics
from codeagent.infra.text import (
    bounded_preview,
    count_lines,
    extract_key_lines,
    extract_test_counts,
)
from codeagent.tool.models import ToolResult

_ARTIFACT_KIND = "tool-results"


class ToolResultNormalizer:
    def __init__(
        self,
        *,
        estimator: TokenEstimator,
        artifact_store: ArtifactStore,
        metrics: Metrics | None = None,
    ) -> None:
        self._estimator = estimator
        self._artifacts = artifact_store
        self._metrics = metrics or Metrics()

    async def normalize(self, result: ToolResult, *, profile: ContextProfile) -> ToolResult:
        limit = profile.max_tool_result_tokens
        if estimate_text(result.content) <= limit:
            return result

        raw = result.content
        artifact = result.artifact
        if artifact is None:
            artifact = await self._artifacts.save_text(
                _ARTIFACT_KIND,
                raw,
                metadata={
                    "tool": result.tool_name,
                    "call_id": result.call_id,
                    "status": str(result.status),
                    "exit_code": result.exit_code,
                },
            )

        summary = _build_summary(result, artifact_uri=artifact.uri, char_budget=limit * 3)
        # 中文内容下 tokens≈chars，3x 预算可能超；收敛一到两次。
        for _ in range(2):
            actual = estimate_text(summary)
            if actual <= limit:
                break
            ratio = limit / max(actual, 1)
            summary = _build_summary(
                result,
                artifact_uri=artifact.uri,
                char_budget=int(len(summary) * ratio * 0.9),
            )

        self._metrics.incr(M.TOOL_NORMALIZED)
        self._metrics.incr(
            M.TOOL_OFFLOADED_TOKENS, max(0, estimate_text(raw) - estimate_text(summary))
        )

        return ToolResult(
            call_id=result.call_id,
            tool_name=result.tool_name,
            status=result.status,
            content=summary,
            exit_code=result.exit_code,
            artifact=artifact,
            evidence=result.evidence,
            truncated=True,
            raw_bytes=result.raw_bytes or len(raw.encode("utf-8")),
            metadata=result.metadata,
        )


def _build_summary(result: ToolResult, *, artifact_uri: str, char_budget: int) -> str:
    raw = result.content
    lines: list[str] = [f"tool: {result.tool_name}", f"status: {result.status}"]
    if result.exit_code is not None:
        lines.append(f"exitCode: {result.exit_code}")
    command = result.metadata.get("command")
    if command:
        lines.append(f"command: {command}")
    if result.raw_bytes > len(raw.encode("utf-8")):
        # 工具已经自己流式落盘并只交回预览，这里报告真实体量而不是预览体量。
        lines.append(
            f"rawSize: {result.raw_bytes} 字节（完整输出已落盘）/ 本层可见 {len(raw)} 字符"
        )
    else:
        lines.append(f"rawSize: {len(raw)} 字符 / {count_lines(raw)} 行")

    counts = extract_test_counts(raw)
    if counts:
        lines.append("tests: " + " ".join(f"{k}={v}" for k, v in counts.items()))

    key_errors = extract_key_lines(raw, limit=8)
    if key_errors:
        lines.append("keyErrors:")
        lines.extend(f"  - {line}" for line in key_errors)

    lines.append(f"完整内容: {artifact_uri}（用 read_artifact 回读）")

    header = "\n".join(lines)
    body_budget = max(0, char_budget - len(header) - 16)
    if body_budget <= 0:
        return header
    return f"{header}\npreview:\n{bounded_preview(raw, body_budget)}"
