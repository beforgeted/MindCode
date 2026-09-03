from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path

from codeagent.context.profile import ContextProfile


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass(frozen=True, slots=True)
class AppConfig:
    workspace_root: Path
    home: Path
    model: str = "claude-sonnet-5"
    max_tool_concurrency: int = 8
    profile: ContextProfile = field(default_factory=ContextProfile)
    use_stub_llm: bool = False

    @classmethod
    def from_env(cls, workspace_root: Path | str | None = None) -> AppConfig:
        workspace = Path(workspace_root or os.getcwd()).resolve()
        home = Path(os.environ.get("CODEAGENT_HOME") or (workspace / ".codeagent")).resolve()
        window = _env_int("CODEAGENT_CONTEXT_WINDOW", 200_000)
        profile = replace(ContextProfile(), context_window=window)
        return cls(
            workspace_root=workspace,
            home=home,
            model=os.environ.get("CODEAGENT_MODEL") or "claude-sonnet-5",
            max_tool_concurrency=_env_int("CODEAGENT_TOOL_CONCURRENCY", 8),
            profile=profile,
            use_stub_llm=not os.environ.get("ANTHROPIC_API_KEY"),
        )


DEFAULT_SYSTEM_PROMPT = """你是 MindCode，一个在用户本地代码库里工作的编码 Agent。

工作方式：
- 先用 read_file / grep 了解现状，再动手改。不要凭猜测写代码。
- 改完用 run_command 跑构建或测试来验证。
- 工具输出如果显示「完整内容: artifact://...」，说明结果已被截断落盘；
  需要细节时用 read_artifact 回读，不要假设省略部分的内容。
- 修改文件用 write_file。破坏性操作先说明再执行。

回答简洁，直接给结论和改动，不要复述已经做过的事。
"""
