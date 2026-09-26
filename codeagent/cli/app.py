"""最小 REPL。

`input()` 是阻塞调用，放进 `asyncio.to_thread` —— 这不是洁癖，而是这套架构
在 asyncio 下的基本纪律：任何同步阻塞调用留在事件循环里，都会卡住所有
并发的 AgentRun（P5 之后尤其致命）。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from codeagent.agent.models import RunStatus
from codeagent.cli.memory import handle_memory_command
from codeagent.cli.report import render_context_report
from codeagent.config import AppConfig
from codeagent.context.manager import ContextOverflowError
from codeagent.llm.stub_client import StubLlmClient
from codeagent.session import AgentSession

BANNER = """MindCode CodeAgent (P0-P5 单/多 Agent)
命令: /context /compact /memory add|list|search|show|delete|harvest
      /task <目标> /clear /metrics /quit
"""


def _build_client(config: AppConfig):
    if config.use_stub_llm:
        print("[未检测到 ANTHROPIC_API_KEY，使用 StubLlmClient —— 不会真的调用模型]")
        return StubLlmClient(["这是 stub 回复。设置 ANTHROPIC_API_KEY 后可接真实模型。"] * 50)
    from codeagent.llm.anthropic_client import AnthropicLlmClient

    return AnthropicLlmClient()


async def _handle_command(session: AgentSession, line: str, *, config, client) -> bool:
    """返回 False 表示退出。"""
    command, _, rest = line.partition(" ")
    rest = rest.strip()

    if command in ("/quit", "/exit"):
        return False

    if command == "/task":
        if not rest:
            print("用法: /task <目标>")
            return True
        await _run_task(session, config, client, rest)
        return True

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


async def _run_task(session: AgentSession, config: AppConfig, client, goal: str) -> None:
    """P5/P6 Multi-Agent：规划 → 并行 Worker → 验收 → 合并。复用当前活着的 session。

    `/task --resume <mrun_id>`：从 RunStore 恢复，跳过已完成 Step，只重跑未完成的。
    """
    from codeagent.orchestration.master_session import build_master
    from codeagent.orchestration.run_store import SqliteRunStore

    resume_id: str | None = None
    parts = goal.split()
    if len(parts) >= 2 and parts[0] == "--resume":
        resume_id = parts[1]
        goal = goal[len("--resume") :].strip()[len(resume_id) :].strip()

    run_store = SqliteRunStore(config.state_root / "runs.db")
    await run_store.start()

    master = await build_master(
        config=config,
        llm_client=client,
        engine=session.engine,
        event_store=session.event_store,
        metrics=session.metrics,
        definition=session.definition,
        memory_store=session.memory_store if session.memory_service.available else None,
        run_store=run_store,
    )
    final = await master.run(
        goal, session_id=session.session_id, resume_master_run_id=resume_id
    )
    # 只呈现任务级结果：integrated 全绿才算完成;冲突/分支属内部细节,不让用户处理。
    if final.integrated:
        print(f"\n[任务完成] {final.reason}")
    else:
        print(f"\n[任务未完成] {final.reason or '内部集成未通过'}")
    sched = final.scheduler
    if sched is not None:
        print(
            f"Step: 已集成 {len(sched.integrated)} / 失败 {len(sched.failed)} / "
            f"阻塞 {len(sched.blocked)}，最大并行 {sched.max_parallel}"
        )
    if final.files:
        print("改动文件:")
        for state in final.files:
            print(f"  {state.change}: {state.path}")
    # 调试信息（非用户待办）：内部集成冲突/分支,仅供排查。
    if final.merge_conflicts:
        print(f"[调试] 内部集成冲突: {'; '.join(final.merge_conflicts)}")
    print(f"[调试] master_run_id={final.master_run_id}（未完成可 /task --resume 续跑）")


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
                if not await _handle_command(session, line, config=config, client=client):
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


def _load_dotenv(*candidates: Path) -> Path | None:
    """极简 .env 加载：KEY=VALUE 逐行读入 os.environ。

    - 不引第三方依赖；支持 `export KEY=VALUE`、`#` 注释、可选引号。
    - **.env 优先**：会覆盖会话里已存在的同名变量（用户在 .env 里配了就以它为准，
      避免被外层残留的 ANTHROPIC_API_KEY / ANTHROPIC_BASE_URL 悄悄顶掉）。
    - 返回实际加载的文件路径（未找到则 None）。
    """
    for path in candidates:
        if not path or not path.is_file():
            continue
        try:
            for raw in path.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[len("export ") :].lstrip()
                key, sep, value = line.partition("=")
                if not sep:
                    continue
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key:
                    os.environ[key] = value
        except OSError:
            continue
        return path
    return None


def _repair_ca_env() -> None:
    """conda on Windows 常把 SSL_CERT_FILE 指到不存在的路径（缺 Library 段），
    导致 httpx 建 TLS context 时 FileNotFoundError。指向的文件不存在就删掉该变量，
    让 Python/httpx 回退到默认 CA，避免真实 LLM 调用一上来就崩。
    """
    for name in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE"):
        value = os.environ.get(name)
        if value and not Path(value).is_file():
            os.environ.pop(name, None)
            print(f"[已忽略无效的 {name}（路径不存在）→ 回退默认 CA]")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="codeagent")
    parser.add_argument("--workspace", type=Path, default=None, help="工作目录，默认当前目录")
    parser.add_argument(
        "--env", type=Path, default=None, help=".env 路径，默认在工作目录/当前目录查找"
    )
    args = parser.parse_args(argv)
    workspace = args.workspace or Path.cwd()
    loaded = _load_dotenv(args.env, workspace / ".env", Path.cwd() / ".env")
    if loaded is not None:
        print(f"[已加载 {loaded}]")
    _repair_ca_env()
    config = AppConfig.from_env(args.workspace)
    try:
        return asyncio.run(run_repl(config))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
