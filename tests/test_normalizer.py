"""ToolResultNormalizer / 大输出治理 —— 上下文文档 §37.6。

要点：100K token 的 tool 输出必须由 tool 边界的有界化处理掉，
而不是等它进历史后触发整段 History Summary。
"""

from __future__ import annotations

from codeagent.context.profile import ContextProfile
from codeagent.context.token_estimator import HeuristicTokenEstimator, estimate_text
from codeagent.tool.models import ToolCall, ToolResult, ToolResultStatus
from codeagent.tool.normalizer import ToolResultNormalizer

_MAVEN_TAIL = """
Tests run: 128, Failures: 1, Errors: 0, Skipped: 0
[ERROR] PaymentConcurrentTest.shouldNotOverdraw:83 expected balance=80 but was actual balance=70
[ERROR] BUILD FAILURE
"""


def _normalizer(artifact_store) -> ToolResultNormalizer:
    return ToolResultNormalizer(
        estimator=HeuristicTokenEstimator(), artifact_store=artifact_store
    )


async def test_small_result_untouched(artifact_store, profile):
    call = ToolCall("tu_1", "echo", {})
    result = ToolResult.ok(call, "short output")
    out = await _normalizer(artifact_store).normalize(result, profile=profile)
    assert out is result
    assert not out.truncated


async def test_huge_result_is_bounded_and_offloaded(artifact_store, profile):
    raw = "\n".join(f"[INFO] downloading dependency number {i} from repo" for i in range(20_000))
    assert estimate_text(raw) > 100_000

    call = ToolCall("tu_1", "run_command", {})
    result = ToolResult(
        call_id=call.id,
        tool_name=call.name,
        status=ToolResultStatus.OK,
        content=raw,
        exit_code=0,
        metadata={"command": "mvn test"},
    )
    out = await _normalizer(artifact_store).normalize(result, profile=profile)

    assert out.truncated
    assert out.artifact is not None
    assert estimate_text(out.content) <= profile.max_tool_result_tokens * 1.2
    assert "mvn test" in out.content
    assert out.artifact.uri in out.content

    # 完整内容可回读 —— Context 是 Working Set，Storage 才是 Full Evidence。
    restored = await artifact_store.load_text(out.artifact.uri)
    assert restored == raw


async def test_key_errors_survive_normalization(artifact_store, profile):
    raw = ("[INFO] noise line\n" * 5_000) + _MAVEN_TAIL
    call = ToolCall("tu_1", "run_command", {})
    result = ToolResult(
        call_id=call.id,
        tool_name=call.name,
        status=ToolResultStatus.ERROR,
        content=raw,
        exit_code=1,
        metadata={"command": "mvn test"},
    )
    out = await _normalizer(artifact_store).normalize(result, profile=profile)

    assert "exitCode: 1" in out.content
    assert "keyErrors" in out.content
    # LLM 真正需要的是"失败了什么"，不是 5000 行 INFO。
    assert "PaymentConcurrentTest" in out.content or "expected balance=80" in out.content


async def test_normalization_scales_with_profile(artifact_store):
    from dataclasses import replace

    raw = "错误日志内容 " * 5_000
    call = ToolCall("tu_1", "echo", {})
    result = ToolResult.ok(call, raw)

    tight = replace(ContextProfile(), max_tool_result_tokens=200)
    loose = replace(ContextProfile(), max_tool_result_tokens=2_000)
    normalizer = _normalizer(artifact_store)

    tight_out = await normalizer.normalize(result, profile=tight)
    loose_out = await normalizer.normalize(result, profile=loose)
    assert len(tight_out.content) < len(loose_out.content)
