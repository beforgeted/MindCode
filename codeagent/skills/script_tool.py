from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
from typing import Any

from codeagent.llm.types import ToolSpec
from codeagent.skills import script_helper
from codeagent.skills.config import SkillDefinition
from codeagent.tool.base import BaseTool, ToolExecutionContext
from codeagent.tool.effects import EffectKind, RetryPolicy
from codeagent.tool.executor import SandboxExecutor
from codeagent.tool.mcp.bridge import load_json
from codeagent.tool.models import ToolCall, ToolConcurrencyMode, ToolResult


class SkillScriptTool(BaseTool):
    concurrency_mode = ToolConcurrencyMode.SERIAL
    effect_kind = EffectKind.WORKSPACE_WRITE
    retry_policy = RetryPolicy.NEVER

    def __init__(self, skill: SkillDefinition):
        if skill.package is None or not skill.scripts:
            raise ValueError('script tool requires a frozen package and operator script grants')
        self.skill = skill
        self._name = f'skill_{skill.id.replace("-", "_")}_script'
        self._source = Path(script_helper.__file__).read_text(encoding='utf-8')
        self._files = [(p, base64.b64encode(raw).decode()) for p, raw in skill.package.files]

    @property
    def name(self) -> str:
        return self._name

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(self.name, 'Run an operator-authorized frozen Python Skill script in '
                        'the current Podman workspace. Companion files are beside the script; '
                        'cwd is /workspace. No dependencies are installed.', {
            'type': 'object', 'properties': {
                'script': {'type': 'string', 'enum': list(self.skill.scripts)},
                'args': {'type': 'array', 'maxItems': 64,
                         'items': {'type': 'string', 'maxLength': 4096}},
            }, 'required': ['script'], 'additionalProperties': False,
        })

    async def execute(self, ctx: ToolExecutionContext, arguments: dict[str, Any]) -> ToolResult:
        call = ToolCall(ctx.call_id, self.name, arguments)
        script, args = arguments.get('script'), arguments.get('args', [])
        if (set(arguments) - {'script', 'args'} or script not in self.skill.scripts
                or not isinstance(args, list) or len(args) > 64
                or any(not isinstance(a, str) or len(a) > 4096 or '\x00' in a for a in args)):
            return ToolResult.error(call, 'invalid or unauthorized Skill script request')
        executor = ctx.command_executor
        if not isinstance(executor, SandboxExecutor) or ctx.workspace.root != executor.root:
            return ToolResult.error(call, 'Skill script runtime requires its Podman domain')
        ctx.cancellation.raise_if_cancelled()
        payload = json.dumps({'files': self._files, 'script': script, 'args': args,
                              'max_output_bytes': min(ctx.max_output_bytes, 1024 * 1024,
                                  max(1, (executor.manager.limits.output_bytes - 1024) * 3 // 4)),
                              }).encode()
        async with asyncio.timeout(ctx.timeout_seconds):
            output = await executor.manager.execute_python(
                executor.handle, self._source, payload, cancellation=ctx.cancellation,
                max_output_bytes=8 * 1024 * 1024,
            )
        data = load_json(output.stdout)
        if output.returncode or 'bridge_error' in data:
            return ToolResult.error(call, 'Skill script failed: '
                                    + str(data.get('bridge_error', 'ProtocolError')))
        factory = ToolResult.ok if data['exit_code'] == 0 else ToolResult.error
        body = b''.join(base64.b64decode(data[k], validate=True)
                        for k in ('stdout', 'stderr')).decode('utf-8', errors='replace')
        assert self.skill.package is not None
        return factory(call, body, exit_code=data['exit_code'],
                       metadata={'skill_package_sha256': self.skill.package.digest,
                                 'skill_script': script})
