from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any


@dataclass
class KnowledgeState:
    """Ephemeral derived data owned by one executor, never candidate/recovery state."""

    cache: dict[str, Any] = field(default_factory=dict)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
