from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum


class SandboxError(RuntimeError):
    """执行域未建立、约束不满足或生命周期操作失败。"""


class SandboxUnavailable(SandboxError):
    pass


class ExecutionPurpose(StrEnum):
    WORKER = "worker"
    VALIDATION = "validation"
    INTERACTIVE = "interactive"
    INTERACTIVE_READONLY = "interactive_readonly"


@dataclass(frozen=True, slots=True)
class ExecutionLimits:
    cpus: float = 1.0
    memory_mib: int = 512
    workspace_mib: int = 256
    temporary_mib: int = 64
    pids: int = 64
    timeout_seconds: float = 60.0
    output_bytes: int = 4 * 1024 * 1024
    lifetime_seconds: int = 3600

    def __post_init__(self) -> None:
        for value in (self.memory_mib, self.workspace_mib, self.temporary_mib,
                      self.pids, self.output_bytes, self.lifetime_seconds):
            if type(value) is not int or value <= 0:
                raise ValueError("执行限额必须为正整数")
        for value in (self.cpus, self.timeout_seconds):
            if not math.isfinite(value) or value <= 0:
                raise ValueError("CPU/超时限额必须是有限正数")


@dataclass(frozen=True, slots=True)
class SandboxHandle:
    container_id: str
    name: str
    owner: str
    purpose: ExecutionPurpose
    host_pid: int
    process_started: str


@dataclass(frozen=True, slots=True)
class ProcessOutput:
    returncode: int
    stdout: bytes
    stderr: bytes
