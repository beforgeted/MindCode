"""工具副作用模型：两个正交的轴（Phase 7）。

`EffectKind`（操作对世界做了什么）与 `RetryPolicy`（能不能安全重跑）是**两个独立的轴**，
且都独立于 `ToolConcurrencyMode`（调度：能否并行）。不可混成一个字段：

- concurrency 管"能否并行"（READ_ONLY/EXCLUSIVE_RESOURCE/SERIAL）；
- effect 管"是否可被 candidate 隔离/回滚"；
- retry 管"是否可自动重跑/重放"。

核心用途：Master Attempt 的 candidate 只能回滚**仓库内文件**（WORKSPACE_WRITE）。
EXTERNAL_SIDE_EFFECT（树外写/网络/DB/发布）事务回滚不了；而四层收敛会自动重跑 Step，
retry=NEVER 的非幂等副作用一旦重放就会叠加。因此推测执行期间禁止不可回滚的外部副作用，
且 retry=NEVER 的步不进自动 rerun/replan。

对照：
| 操作 | EffectKind | RetryPolicy |
|---|---|---|
| read_file / grep / GET API | READ_ONLY | SAFE |
| 改源文件 / write_file / pytest | WORKSPACE_WRITE | SAFE（candidate 隔离） |
| echo >> file | WORKSPACE_WRITE | NEVER（只能靠 Attempt 回滚，不能重放） |
| DB 迁移 | EXTERNAL_SIDE_EFFECT | NEVER / IDEMPOTENT |
| 发消息 / 发布 / 部署 | EXTERNAL_SIDE_EFFECT | NEVER |
"""

from __future__ import annotations

from enum import StrEnum


class EffectKind(StrEnum):
    """操作对世界做了什么 —— 决定能否被 candidate 隔离/回滚。"""

    READ_ONLY = "read_only"  # 不改任何状态
    WORKSPACE_WRITE = "workspace_write"  # 只改仓库内文件 → candidate 可隔离/回滚
    EXTERNAL_SIDE_EFFECT = "external_side_effect"  # 树外/网络/DB/发布 → 事务回滚不了


class RetryPolicy(StrEnum):
    """能不能安全重跑 —— 决定是否允许自动 rerun/replan。"""

    SAFE = "safe"  # 无副作用或幂等到可随便重跑（read_file、pytest）
    IDEMPOTENT = "idempotent"  # 有副作用但幂等（PUT /resource/id、create-if-not-exists）
    NEVER = "never"  # 非幂等副作用（echo >>、发消息、发布、迁移）


__all__ = ["EffectKind", "RetryPolicy"]
