"""本地确定性去重（记忆 V2 §24）：低成本、无 LLM。

命中即 skip，不进后续冲突判断链。
"""

from __future__ import annotations

import re
import unicodedata

from codeagent.memory.models import MemoryItem

_CJK_RE = re.compile(r"[　-ヿ㐀-鿿豈-﫿]")
_LATIN_RE = re.compile(r"[A-Za-z0-9_.-]{2,}")


def _normalize(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).split()).casefold()


def _tokens(text: str) -> set[str]:
    normalized = unicodedata.normalize("NFKC", text).casefold()
    out: set[str] = set(_LATIN_RE.findall(normalized))
    cjk = "".join(_CJK_RE.findall(normalized))
    for index in range(len(cjk) - 1):
        out.add(cjk[index : index + 2])
    if len(cjk) == 1:
        out.add(cjk)
    return out


def token_jaccard(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


class MemoryDeduplicator:
    def __init__(self, *, jaccard_threshold: float = 0.9) -> None:
        self._threshold = jaccard_threshold

    def find_duplicate(self, content: str, items: list[MemoryItem]) -> MemoryItem | None:
        norm = _normalize(content)
        if not norm:
            return None
        for item in items:
            other = _normalize(item.content)
            if not other:
                continue
            if norm == other:
                return item
            if norm in other or other in norm:
                return item
            if token_jaccard(content, item.content) >= self._threshold:
                return item
        return None
