from __future__ import annotations

import re
from dataclasses import dataclass

from codeagent.memory.governance_models import CandidateStatus, FilterReason

_PEM_RE = re.compile(r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----")
_BEARER_RE = re.compile(r"\bAuthorization\s*:\s*Bearer\s+\S+", re.IGNORECASE)
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")
_AWS_KEY_RE = re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")
_API_KEY_RE = re.compile(r"\b(?:sk|ghp|github_pat|xox[baprs])[-_][A-Za-z0-9_-]{12,}\b")
_ASSIGNMENT_RE = re.compile(
    r"\b(?:password|passwd|secret|api[_-]?key|access[_-]?token|auth[_-]?token)\b"
    r"\s*[:=]\s*[\"']?([^\s\"']+)",
    re.IGNORECASE,
)
_CREDENTIAL_URI_RE = re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s:/]+:[^\s/@]+@", re.IGNORECASE)
_STACK_RE = re.compile(r"^Traceback \(most recent call last\):", re.MULTILINE)
_COMMAND_OUTPUT_RE = re.compile(r"^(?:stdout|stderr|exit code|return code)\s*:", re.IGNORECASE)
_PLACEHOLDERS = {
    "example",
    "placeholder",
    "redacted",
    "changeme",
    "your-password",
    "your-token",
    "<password>",
    "<token>",
    "***",
}


@dataclass(frozen=True, slots=True)
class PrefilterResult:
    accepted: bool
    status: CandidateStatus
    reason: FilterReason


def prefilter(content: str, *, max_bytes: int = 16 * 1024) -> PrefilterResult:
    clean = content.strip()
    if not clean:
        return PrefilterResult(False, CandidateStatus.FILTERED_NOISE, FilterReason.EMPTY)
    if len(clean.encode("utf-8")) > max_bytes:
        return PrefilterResult(False, CandidateStatus.FILTERED_NOISE, FilterReason.TOO_LARGE)
    if "\x00" in clean or _looks_binary(clean):
        return PrefilterResult(False, CandidateStatus.FILTERED_SENSITIVE, FilterReason.SENSITIVE)
    if _contains_secret(clean):
        return PrefilterResult(False, CandidateStatus.FILTERED_SENSITIVE, FilterReason.SENSITIVE)
    if _STACK_RE.match(clean) or _COMMAND_OUTPUT_RE.match(clean):
        return PrefilterResult(False, CandidateStatus.FILTERED_NOISE, FilterReason.NOISE)
    return PrefilterResult(True, CandidateStatus.PENDING_JUDGE, FilterReason.ACCEPTED)


def _contains_secret(content: str) -> bool:
    if any(
        pattern.search(content)
        for pattern in (_PEM_RE, _BEARER_RE, _JWT_RE, _AWS_KEY_RE, _API_KEY_RE, _CREDENTIAL_URI_RE)
    ):
        return True
    for match in _ASSIGNMENT_RE.finditer(content):
        value = match.group(1).strip().casefold()
        if value not in _PLACEHOLDERS and not value.startswith(("<", "${", "{{")):
            return True
    return False


def _looks_binary(content: str) -> bool:
    controls = sum(ord(char) < 32 and char not in "\n\r\t" for char in content)
    return controls > max(2, len(content) // 20)
