"""Phase 7a：工具副作用模型（EffectKind / RetryPolicy）声明的验收。"""

from __future__ import annotations

from codeagent.tool.base import BaseTool
from codeagent.tool.builtin import default_tools
from codeagent.tool.builtin.read_file import ReadFileTool
from codeagent.tool.builtin.run_command import RunCommandTool
from codeagent.tool.builtin.write_file import WriteFileTool
from codeagent.tool.effects import EffectKind, RetryPolicy


def test_basetool_defaults_read_only_safe() -> None:
    class _T(BaseTool):
        pass

    assert _T.effect_kind is EffectKind.READ_ONLY
    assert _T.retry_policy is RetryPolicy.SAFE


def test_read_file_is_read_only_safe() -> None:
    tool = ReadFileTool()
    assert tool.effect_kind is EffectKind.READ_ONLY
    assert tool.retry_policy is RetryPolicy.SAFE


def test_write_file_is_workspace_write_safe() -> None:
    tool = WriteFileTool()
    # 只改仓库内文件（candidate 可回滚），覆盖写幂等 → 可安全重跑。
    assert tool.effect_kind is EffectKind.WORKSPACE_WRITE
    assert tool.retry_policy is RetryPolicy.SAFE


def test_run_command_conservative_default_never() -> None:
    tool = RunCommandTool()
    # 任意 shell 命令的保守默认：改工作区、不可自动重放（真实分类见 CommandPolicy）。
    assert tool.effect_kind is EffectKind.WORKSPACE_WRITE
    assert tool.retry_policy is RetryPolicy.NEVER


def test_all_default_tools_declare_effect_and_retry() -> None:
    for tool in default_tools():
        assert isinstance(tool.effect_kind, EffectKind)
        assert isinstance(tool.retry_policy, RetryPolicy)
