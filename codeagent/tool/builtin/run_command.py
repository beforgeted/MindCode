"""run_command：分类 → 守卫 → 交 CommandExecutor（Phase 7c 起变薄）。

工具本身只负责：命令分类（CommandPolicy）、守卫（拒危险命令）、组装 ToolResult。
真正的 spawn / 流式落 artifact / 进程树终止 / env 过滤在 CommandExecutor（executor.py）。

无界输出仍是**主防线**：executor 边读边落 artifact，内存只留 head/tail 预览，
需要细节用 read_artifact 回读（JIT 取证）。

安全说明：执行模型给出的 shell 是最大风险面。7b 起用 CommandPolicy 分类、拒明显危险命令；
7c 起 env 过滤 + 进程树终止；external 副作用的推测期禁止/审批见 7d。完整容器沙箱（SandboxExecutor）
尚未实现——在完全不受信任的环境仍需补沙箱。
"""

from __future__ import annotations

from typing import Any

from codeagent.llm.types import ToolSpec
from codeagent.tool.base import BaseTool, ToolExecutionContext
from codeagent.tool.effects import EffectKind, RetryPolicy
from codeagent.tool.models import ToolCall, ToolConcurrencyMode, ToolResult


class RunCommandTool(BaseTool):
    concurrency_mode = ToolConcurrencyMode.SERIAL
    # 类属性是**保守默认**：任意 shell 命令按"改工作区、不可安全重放"对待。
    # 真实的按命令分类（read_only / workspace_write / external）由 CommandPolicy 完成。
    effect_kind = EffectKind.WORKSPACE_WRITE
    retry_policy = RetryPolicy.NEVER

    @property
    def name(self) -> str:
        return "run_command"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=self.name,
            description=(
                "在 workspace 根目录执行 shell 命令（构建、测试、git 等）。"
                "输出完整落盘，返回值只包含有界摘要与 artifact 引用。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "cwd": {"type": "string", "description": "相对 workspace 的子目录，可选"},
                },
                "required": ["command"],
            },
        )

    async def execute(self, ctx: ToolExecutionContext, arguments: dict[str, Any]) -> ToolResult:
        call = ToolCall(ctx.call_id, self.name, arguments)
        command = str(arguments.get("command", "")).strip()
        if not command:
            return ToolResult.error(call, "缺少参数 command")
        decision = ctx.command_policy.classify(command)
        if not decision.allowed:
            return ToolResult.error(call, f"命令被拒绝执行（{decision.reason}）: {command}")
        if decision.effect is EffectKind.EXTERNAL_SIDE_EFFECT:
            if not ctx.allow_external_effects:
                # 推测执行阶段：外部副作用 candidate 回滚不了，一律禁止（应延后到验收通过后）。
                return ToolResult.error(
                    call,
                    f"外部副作用在推测执行阶段禁止（{decision.reason}），"
                    f"已阻止，请改为验收通过后执行: {command}",
                )
            if not await ctx.approval.approve(decision, command=command):
                return ToolResult.error(call, f"外部副作用未获批准（{decision.reason}）: {command}")

        try:
            cwd = ctx.workspace.resolve(str(arguments.get("cwd") or "."))
        except PermissionError as exc:
            return ToolResult.error(call, str(exc))

        metadata = {"command": command, "cwd": str(cwd), "effect": str(decision.effect)}
        outcome = await ctx.command_executor.run(
            command=command,
            cwd=cwd,
            cancellation=ctx.cancellation,
            artifact_store=ctx.artifact_store,
            max_output_bytes=ctx.max_output_bytes,
            metadata=metadata,
        )

        status_line = f"$ {command}\nexitCode: {outcome.exit_code}  输出 {outcome.total_bytes} 字节"
        result_text = f"{status_line}\n{outcome.content}" if outcome.content else status_line
        factory = ToolResult.ok if outcome.exit_code == 0 else ToolResult.error
        return factory(
            call,
            result_text,
            exit_code=outcome.exit_code,
            artifact=outcome.artifact,
            raw_bytes=outcome.total_bytes,
            truncated=outcome.truncated,
            metadata=metadata,
        )
