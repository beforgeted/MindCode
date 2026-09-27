"""Integrator：重跑预算耗尽仍不收敛时的兜底（Phase 3）。

定位:Phase 2 的"过期→在最新基线重跑"用**原指令**重跑;若多次仍冲突/过期,说明模型没意识到
"基线已变、要与已集成的改动兼容"。Integrator 给该 Step 生成一版**带冲突现场的增强指令**,
让它在最新 candidate 基线上重跑时先读现状、再与已集成改动协调。

关键:Integrator 的产物仍进 **candidate**,并和其它改动一起经 Master Attempt 的**产物级全局验收**
才 CAS 推进真实 base —— **绝不绕过全局验收直接写 base**（这正是必须先有 Master Attempt
Transaction 的原因）。

本实现是确定性的指令增强（真正的协调由 Worker 重跑时自己的 LLM + read_file 完成），
无需额外 LLM plumbing;需要更强的"直接产出合并版"策略时可换成 LLM 版 Integrator。
"""

from __future__ import annotations

from dataclasses import replace
from typing import Protocol, runtime_checkable

from codeagent.orchestration.task_graph import Step


@runtime_checkable
class Integrator(Protocol):
    async def reconcile(
        self, step: Step, *, conflict: str | None = None, overlap: tuple[str, ...] = ()
    ) -> Step | None: ...


class NullIntegrator:
    """默认不兜底：返回 None → 交回调度器判失败。"""

    async def reconcile(
        self, step: Step, *, conflict: str | None = None, overlap: tuple[str, ...] = ()
    ) -> Step | None:
        return None


class InstructionIntegrator:
    async def reconcile(
        self, step: Step, *, conflict: str | None = None, overlap: tuple[str, ...] = ()
    ) -> Step | None:
        files = "、".join(overlap) if overlap else "（部分文件）"
        note = (
            f"[集成协调] 你上一版改动与已集成到当前基线的改动重叠/冲突，涉及：{files}。"
            "请先用 read_file 查看这些文件的**当前内容**，在此基础上完成原目标，"
            "确保与已集成的改动兼容，不要覆盖或破坏它们。\n原目标："
        )
        return replace(step, instruction=note + step.instruction)


__all__ = ["InstructionIntegrator", "Integrator", "NullIntegrator"]
