"""最小 REPL。

`input()` 是阻塞调用，放进 `asyncio.to_thread` —— 这不是洁癖，而是这套架构
在 asyncio 下的基本纪律：任何同步阻塞调用留在事件循环里，都会卡住所有
并发的 AgentRun（P5 之后尤其致命）。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from codeagent.agent.models import RunStatus
from codeagent.cli.memory import handle_memory_command
from codeagent.cli.report import render_context_report
from codeagent.config import AppConfig
from codeagent.context.manager import ContextOverflowError
from codeagent.llm.stub_client import StubLlmClient
from codeagent.session import AgentSession

BANNER = """MindCode CodeAgent (P0-P4 单 Agent)
命令: /context  /compact  /memory add|list|search|show|delete|harvest  /clear  /metrics  /quit
"""


def _build_client(config: AppConfig):
    if config.use_stub_llm:
        print("[未检测到 ANTHROPIC_API_KEY，使用 StubLlmClient —— 不会真的调用模型]")
        return StubLlmClient(["这是 stub 回复。设置 ANTHROPIC_API_KEY 后可接真实模型。"] * 50)
    from codeagent.llm.anthropic_client import AnthropicLlmClient

    return AnthropicLlmClient()


async def _handle_command(session: AgentSession, line: str) -> bool:
    """返回 False 表示退出。"""
    command, _, rest = line.partition(" ")
    rest = rest.strip()

    if command in ("/quit", "/exit"):
        return False

    if command == "/context":
        print(
            render_context_report(
                session.last_prepared,
                session.profile,
                session.metrics,
                message_count=len(session.run.history),
                compaction_count=session.run.history.compaction_count,
            )
        )
    elif command == "/compact":
        try:
            prepared = await session.context_manager.prepare(
                session.run.history, session.profile, force_compact=True, focus=rest or None
            )
        except ContextOverflowError as exc:
            print(f"[compact 失败] {exc}")
            return True
        session.run.context.last_prepared = prepared
        c = prepared.compaction
        if c is None or not c.compacted:
            print(f"[未压缩] {c.reason if c else 'compactor 未触发'}")
        else:
            print(f"[已压缩] 释放 {c.tokens_released:,} tokens")
    elif command == "/clear":
        session.clear()
        print("[已开新 Session Context。Raw Events 与 Durable Memory 不受影响]")
    elif command == "/memory":
        if rest.strip() == "harvest":
            print(await session.run_governance())
        else:
            print(await handle_memory_command(session.memory_service, rest))
    elif command == "/metrics":
        snapshot = session.metrics.snapshot()
        for group, values in snapshot.items():
            if not values:
                continue
            print(f"{group}:")
            for key, value in sorted(values.items()):
                print(f"  {key:<40}{value:,.2f}")
    else:
        print(f"未知命令 {command}")
    return True


async def run_repl(config: AppConfig) -> int:
    client = _build_client(config)
    print(BANNER)
    print(f"workspace: {config.workspace_root}")
    print(f"project:   {config.effective_project_id}")
    print(f"home:      {config.state_root}\n")

    async with AgentSession(config, llm_client=client) as session:
        while True:
            try:
                line = (await asyncio.to_thread(input, "> ")).strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not line:
                continue
            if line.startswith("/"):
                if not await _handle_command(session, line):
                    break
                continue
            try:
                result = await session.send(line)
            except ContextOverflowError as exc:
                print(f"\n[上下文溢出] {exc}\n")
                continue
            except KeyboardInterrupt:
                session.run.cancellation.cancel()
                print("\n[已请求取消]")
                continue

            print()
            if result.status is RunStatus.SUCCESS:
                print(result.summary)
            else:
                print(f"[{result.status}] {result.error or result.summary}")
            if result.files:
                print("\n改动文件:")
                for state in result.files:
                    print(f"  {state.change}: {state.path}")
            if result.tests:
                print("\n测试:")
                for test in result.tests:
                    print(f"  {test.outcome}: {test.name} ({test.detail})")
            print()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="codeagent")
    parser.add_argument("--workspace", type=Path, default=None, help="工作目录，默认当前目录")
    args = parser.parse_args(argv)
    config = AppConfig.from_env(args.workspace)
    try:
        return asyncio.run(run_repl(config))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
