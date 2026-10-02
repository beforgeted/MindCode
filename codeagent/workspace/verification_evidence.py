"""Revision-bound, complete diff units. Unsupported evidence never implies coverage."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class EvidenceUnit:
    id: str
    path: str
    text: str
    kind: str = "text"


@dataclass(frozen=True, slots=True)
class VerificationEvidence:
    base_revision: str
    revision: str
    changed_files: tuple[str, ...]
    units: tuple[EvidenceUnit, ...] = ()
    complete: bool = False
    detail: str = "evidence unavailable"

    @property
    def digest(self) -> str:
        payload = [self.base_revision, self.revision, self.changed_files,
                   [(u.id, u.path, u.text, u.kind) for u in self.units], self.complete, self.detail]
        return hashlib.sha256(json.dumps(payload, ensure_ascii=True).encode()).hexdigest()

    def validate(self, revision: str | None, files: tuple[str, ...]) -> None:
        if not self.complete:
            raise ValueError(self.detail)
        if self.revision != revision or set(self.changed_files) != set(files):
            raise ValueError("evidence revision or file manifest mismatch")
        if len(set(files)) != len(files) or {u.path for u in self.units} != set(files):
            raise ValueError("evidence does not cover every changed file")
        if len({u.id for u in self.units}) != len(self.units):
            raise ValueError("duplicate evidence unit")
        for unit in self.units:
            if (unit.kind not in ("text", "binary") or not unit.text
                    or unit.id != unit_id(unit.path, unit.text, unit.kind)):
                raise ValueError("invalid evidence unit digest")

    @property
    def binary_files(self) -> tuple[str, ...]:
        return tuple(sorted({u.path for u in self.units if u.kind == "binary"}))


def unit_id(path: str, text: str, kind: str = "text") -> str:
    return hashlib.sha256(json.dumps([path, text, kind], ensure_ascii=True).encode()).hexdigest()


def binary_unit(
    path: str, old: bytes | None, new: bytes | None, *, old_mode: str, new_mode: str,
) -> EvidenceUnit:
    def metadata(data: bytes | None, mode: str) -> dict:
        return {"exists": data is not None, "mode": mode,
                "bytes": len(data) if data is not None else 0,
                "sha256": hashlib.sha256(data).hexdigest() if data is not None else None}

    text = json.dumps({"binary_file": path, "old": metadata(old, old_mode),
                       "new": metadata(new, new_mode),
                       "requires_independent_deterministic_check": True}, ensure_ascii=False)
    return EvidenceUnit(unit_id(path, text, "binary"), path, text, "binary")


def diff_units(path: str, text: str) -> tuple[EvidenceUnit, ...]:
    if not text.strip():
        raise ValueError(f"missing diff: {path}")
    if any(line.startswith(("Binary files ", "GIT binary patch")) for line in text.splitlines()):
        raise ValueError(f"binary change requires a dedicated verifier: {path}")
    # A content line always starts with +, - or a space; @@ headers remain atomic.
    starts = [m.start() for m in re.finditer(r"(?m)^@@ ", text)]
    if not starts:
        pieces = [text]  # mode-only, empty file creation/deletion, rename metadata
    else:
        header = text[:starts[0]]
        ends = [*starts[1:], len(text)]
        pieces = [header + text[start:end] for start, end in zip(starts, ends, strict=True)]
    return tuple(EvidenceUnit(unit_id(path, piece), path, piece) for piece in pieces)


def collect_evidence(
    base: str, revision: str, files: tuple[str, ...], diffs: list[tuple[str, str]],
    *, max_bytes: int,
    binary: tuple[EvidenceUnit, ...] = (),
) -> VerificationEvidence:
    units: list[EvidenceUnit] = []
    used = 0
    try:
        if type(max_bytes) is not int or max_bytes <= 0:
            raise ValueError("evidence byte limit must be a positive integer")
        for unit in binary:
            used += len(unit.text.encode("utf-8"))
            units.append(unit)
        if used > max_bytes:
            raise ValueError("complete evidence exceeds byte limit")
        for path, text in diffs:
            parts = diff_units(path, text)
            used += sum(len(u.text.encode("utf-8")) for u in parts)
            if used > max_bytes:
                raise ValueError("complete evidence exceeds byte limit")
            units.extend(parts)
        evidence = VerificationEvidence(base, revision, files, tuple(units), True, "")
        evidence.validate(revision, files)
        return evidence
    except ValueError as exc:
        return VerificationEvidence(base, revision, files, detail=str(exc))
