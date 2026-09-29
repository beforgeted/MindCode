"""DeferredAction：推测执行期间被拦下的外部副作用（Phase 7e）。

外部副作用（网络/发布/DB/部署）在推测执行阶段被守卫拦下后，不是简单丢弃，而是记录成
DeferredAction 进 run 级队列；验收通过 + CAS promote 成功后再由上层按 ApprovalPolicy 处理
（最小实现：非交互默认**只上报、不执行**）。这样既守住"推测期不做不可回滚副作用"，又不
丢失"任务确实需要这个外部动作"的信息。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from codeagent.infra.ids import new_id
from codeagent.tool.effects import EffectKind, RetryPolicy


@dataclass(frozen=True, slots=True)
class DeferredAction:
    command: str
    effect: EffectKind
    retry: RetryPolicy
    reason: str = ""
    cwd: str = ""
    # 幂等键：post-promote 执行时用于去重（崩溃恢复不重复执行已成功条目）。
    id: str = field(default_factory=lambda: new_id("defer"))


__all__ = ["DeferredAction"]
