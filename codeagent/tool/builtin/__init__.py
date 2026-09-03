from codeagent.tool.builtin.grep import GrepTool
from codeagent.tool.builtin.read_artifact import ReadArtifactTool
from codeagent.tool.builtin.read_file import ReadFileTool
from codeagent.tool.builtin.run_command import RunCommandTool
from codeagent.tool.builtin.write_file import WriteFileTool

__all__ = [
    "GrepTool",
    "ReadArtifactTool",
    "ReadFileTool",
    "RunCommandTool",
    "WriteFileTool",
]


def default_tools() -> list:
    return [
        ReadFileTool(),
        GrepTool(),
        WriteFileTool(),
        RunCommandTool(),
        ReadArtifactTool(),
    ]
