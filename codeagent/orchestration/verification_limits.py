"""Finite limits for complete artifact verification, configured independently of history."""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class VerificationLimits:
    max_batches: int = 32
    timeout_seconds: float = 120.0
    max_evidence_bytes: int = 8 * 1024 * 1024

    def __post_init__(self) -> None:
        for value in (self.max_batches, self.max_evidence_bytes):
            if type(value) is not int or value <= 0:
                raise ValueError("verification limits must be positive integers")
        if (isinstance(self.timeout_seconds, bool) or not math.isfinite(self.timeout_seconds)
                or self.timeout_seconds <= 0):
            raise ValueError("verification timeout must be finite and positive")
