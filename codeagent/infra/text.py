"""文本有界化工具。ToolResultNormalizer 和 ToolResultOffloader 共用。"""

from __future__ import annotations

import re

_ERROR_PATTERNS = (
    r"^\s*Traceback \(most recent call last\)",
    r"^[A-Za-z_.]*Error\b",
    r"^[A-Za-z_.]*Exception\b",
    r"\berror\b\s*:",
    r"\[(?:ERROR|FATAL)\]",
    r"\bFAILED\b",
    r"\bFAILURE\b",
    r"\bFailures:\s*[1-9]",
    r"\bFAIL\b",
    r"\bAssertionError\b",
    r"\bexpected\b.*\bactual\b",
    r"\bexpected\b.*\bbut was\b",
    r"^\s*E\s{2,}",
    r"\bfatal\b",
    r"\bcompilation (failed|error)",
)
_ERROR_RE = re.compile("|".join(_ERROR_PATTERNS), re.IGNORECASE | re.MULTILINE)

_TESTS_RE = re.compile(
    r"(?:Tests? run:\s*(?P<run>\d+).*?Failures:\s*(?P<failures>\d+))"
    r"|(?:(?P<passed>\d+)\s+passed)"
    r"|(?:(?P<failed>\d+)\s+failed)",
    re.IGNORECASE,
)

TRUNCATION_MARKER = "…[中间已省略 {omitted} 字符]…"


def bounded_preview(text: str, max_chars: int, *, head_ratio: float = 0.65) -> str:
    """头尾保留式截断。中间省略并明确标注省略量。

    永不静默丢内容：省略多少字符写在标记里，完整内容在 artifact 里。
    """
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    marker_budget = 48
    usable = max(0, max_chars - marker_budget)
    head_len = int(usable * head_ratio)
    tail_len = usable - head_len
    head = text[:head_len]
    tail = text[-tail_len:] if tail_len > 0 else ""
    omitted = len(text) - head_len - tail_len
    return f"{head}\n{TRUNCATION_MARKER.format(omitted=omitted)}\n{tail}"


def extract_key_lines(text: str, *, limit: int = 12, max_line_chars: int = 300) -> list[str]:
    """抽出看起来像错误/失败的行。

    上下文文档 §12.2 想要的 keyErrors —— LLM 需要的是"执行了什么、成功还是失败、
    关键输出是什么"，不是 2000 行日志。
    """
    out: list[str] = []
    seen: set[str] = set()
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or not _ERROR_RE.search(stripped):
            continue
        clipped = stripped[:max_line_chars]
        if clipped in seen:
            continue
        seen.add(clipped)
        out.append(clipped)
        if len(out) >= limit:
            break
    return out


def extract_test_counts(text: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for match in _TESTS_RE.finditer(text):
        for key in ("run", "failures", "passed", "failed"):
            value = match.group(key)
            if value is not None:
                counts[key] = int(value)
    return counts


def count_lines(text: str) -> int:
    if not text:
        return 0
    return text.count("\n") + (0 if text.endswith("\n") else 1)
