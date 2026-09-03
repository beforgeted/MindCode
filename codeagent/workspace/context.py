"""WorkspaceContext。

P1 只有"当前仓库目录"这一种形态。P5 接 git worktree 时补 WorkspaceManager，
但**注意**：Multi-Agent 文档 §8 把 worktree 排到第三期、把 Agent 并行排到第一期，
这个顺序是危险的 —— 一旦开了 AgentRun 并行又允许写操作，两个 Agent 同时写
同一个工作目录必然互相破坏。所以 worktree 必须和并行同期，或者第一版只允许
只读 Step 并行、写操作串行。

另一个坑：Multi-Agent 文档 §8 在 `finally` 里无条件 cleanup workspace，
而 §7 说最终合并由 Master/Merge 阶段处理 —— 合并发生在 AgentRun 返回之后，
所以成功路径上 worktree 会在被合并之前就被删掉。cleanup 的所有权应该在
MasterRuntime（合并之后），而不是 AgentRuntime。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from codeagent.infra.ids import new_id


@dataclass(frozen=True, slots=True)
class WorkspaceContext:
    root: Path
    worktree_id: str
    branch_name: str | None = None
    is_isolated: bool = False

    @classmethod
    def local(cls, root: Path | str) -> WorkspaceContext:
        """P1：直接用当前目录，不隔离。"""
        return cls(root=Path(root).resolve(), worktree_id=new_id("ws"), is_isolated=False)

    def resolve(self, relative: str) -> Path:
        """把工具参数里的相对路径解析到 workspace 内，并阻止越界。"""
        given = Path(relative)
        candidate = given.resolve() if given.is_absolute() else (self.root / given).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError as exc:
            raise PermissionError(f"路径越出 workspace: {relative}") from exc
        return candidate
