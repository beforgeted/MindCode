from __future__ import annotations

from pathlib import Path

from codeagent.cli.memory import handle_memory_command
from codeagent.evidence.event_store import NullEventStore
from codeagent.memory.index_projector import MemoryIndexProjector
from codeagent.memory.service import MemoryService
from codeagent.memory.sqlite_store import SqliteMemoryStore


async def test_memory_cli_round_trip(tmp_path: Path):
    store = SqliteMemoryStore(tmp_path / "memory.db")
    service = MemoryService(
        store,
        MemoryIndexProjector(tmp_path, store, "p"),
        NullEventStore(),
        project_id="p",
        session_id="s",
    )
    await service.start()

    added = await handle_memory_command(
        service,
        'add --type constraint --tag runtime "项目固定使用 Python 3.11"',
    )
    assert "[已记住]" in added
    memory_id = added.split()[1]
    assert memory_id.startswith("mem_")
    assert memory_id in await handle_memory_command(service, "list")
    assert memory_id in await handle_memory_command(service, "search Python")
    shown = await handle_memory_command(service, f"show {memory_id}")
    assert "user_explicit" in shown
    assert "项目固定使用" in shown
    assert "软删除" in await handle_memory_command(service, f"delete {memory_id}")
    assert "没有匹配" in await handle_memory_command(service, "list")


async def test_memory_cli_help_and_errors(tmp_path: Path):
    store = SqliteMemoryStore(tmp_path / "memory.db")
    service = MemoryService(
        store,
        MemoryIndexProjector(tmp_path, store, "p"),
        NullEventStore(),
        project_id="p",
        session_id="s",
    )
    await service.start()
    assert "usage:" in await handle_memory_command(service, "")
    assert "参数错误" in await handle_memory_command(service, "add --type invalid value")
