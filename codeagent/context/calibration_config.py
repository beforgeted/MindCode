"""Bound network sampling independently from chat retries and billing budgets."""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CalibrationConfig:
    enabled: bool = True
    boundary_ratio: float = 0.70
    timeout_seconds: float = 2.0
    min_interval_seconds: float = 60.0
    max_calls_per_cohort: int = 3
    max_total_calls: int = 16
    max_cohorts: int = 64
    cache_size: int = 128
    safety_ratio: float = 0.05

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool:
            raise ValueError('calibration enabled must be bool')
        for name in ('max_calls_per_cohort', 'max_total_calls', 'max_cohorts', 'cache_size'):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f'{name} must be a positive integer')
        for name in ('boundary_ratio', 'timeout_seconds', 'min_interval_seconds', 'safety_ratio'):
            value = getattr(self, name)
            if isinstance(value, bool) or not math.isfinite(value) or value < 0:
                raise ValueError(f'{name} must be finite and nonnegative')
        if not 0 < self.boundary_ratio <= 1 or self.timeout_seconds <= 0:
            raise ValueError('boundary_ratio must be in (0, 1], timeout must be positive')
