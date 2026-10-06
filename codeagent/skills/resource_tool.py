from __future__ import annotations

from typing import Any

from codeagent.llm.types import ToolSpec
from codeagent.skills.package import SkillPackage
from codeagent.tool.base import BaseTool, ToolExecutionContext
from codeagent.tool.models import ToolCall, ToolResult


class SkillResourceTool(BaseTool):
    def __init__(self, skill_id: str, package: SkillPackage):
        self._name = f'skill_{skill_id.replace("-", "_")}_resource'
        self._package = package

    @property
    def name(self) -> str:
        return self._name

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(self.name, 'Read a frozen companion file of this Skill; no execution.', {
            'type': 'object', 'properties': {'path': {'type': 'string'},
                                          'offset': {'type': 'integer', 'minimum': 0},
                                          'limit': {'type': 'integer', 'minimum': 1,
                                                    'maximum': 200}},
            'required': ['path'], 'additionalProperties': False,
        })

    async def execute(self, ctx: ToolExecutionContext, arguments: dict[str, Any]) -> ToolResult:
        call = ToolCall(ctx.call_id, self.name, arguments)
        ctx.cancellation.raise_if_cancelled()
        path, offset, limit = (arguments.get('path'), arguments.get('offset', 0),
                               arguments.get('limit', 100))
        if (set(arguments) - {'path', 'offset', 'limit'} or not isinstance(path, str)
                or type(offset) is not int or offset < 0 or type(limit) is not int
                or not 1 <= limit <= 200):
            return ToolResult.error(call, 'invalid resource request')
        raw = dict(self._package.files).get(path)
        if raw is None:
            return ToolResult.error(call, 'resource is not in this frozen package')
        try:
            lines = raw.decode('utf-8').splitlines()
        except UnicodeError:
            return ToolResult.error(call, 'binary resource preserved but text reading unsupported')
        body = '\n'.join(f'{i + 1}: {lines[i][:2000]}'
                         for i in range(offset, min(offset + limit, len(lines))))
        body = body.encode()[:ctx.max_output_bytes].decode('utf-8', errors='ignore')
        return ToolResult.ok(call, body, metadata={'skill_package_sha256': self._package.digest,
                                                  'lines': len(lines)})
