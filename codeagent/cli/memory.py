from __future__ import annotations

import argparse
import shlex
from datetime import datetime

from codeagent.memory.models import (
    MemoryItem,
    MemoryNotFoundError,
    MemoryStatus,
    MemoryType,
    MemoryUnavailableError,
    MemoryValidationError,
)
from codeagent.memory.service import MemoryService


async def handle_memory_command(service: MemoryService, argument_line: str) -> str:
    parser = _parser()
    try:
        args = parser.parse_args(shlex.split(argument_line))
    except (argparse.ArgumentError, ValueError) as exc:
        return f"[memory 参数错误] {exc}"
    if args.action is None:
        return parser.format_help().strip()
    try:
        if args.action == "add":
            item, index = await service.add(
                args.content,
                MemoryType(args.type.casefold()),
                tags=tuple(args.tag),
            )
            suffix = f"\n[警告] {index.warning}" if index.warning else ""
            return f"[已记住] {item.id} ({item.type}){suffix}"
        if args.action == "list":
            memory_type = MemoryType(args.type.casefold()) if args.type else None
            status = MemoryStatus(args.status.casefold())
            items = await service.list(memory_type=memory_type, status=status, limit=args.limit)
            return _render_list(items)
        if args.action == "search":
            memory_type = MemoryType(args.type.casefold()) if args.type else None
            hits = await service.search(args.query, memory_type=memory_type, limit=args.limit)
            return _render_list([hit.item for hit in hits])
        if args.action == "show":
            item = await service.show(args.id)
            if item is None:
                raise MemoryNotFoundError(f"Memory 不存在: {args.id}")
            return _render_item(item)
        if args.action == "delete":
            result, index = await service.delete(args.id)
            state = "已是删除状态" if result.already_deleted else "已停止检索与注入（软删除）"
            suffix = f"\n[警告] {index.warning}" if index.warning else ""
            return f"[{state}] {result.item.id}{suffix}"
    except ValueError as exc:
        return f"[memory 参数错误] {exc}"
    except (MemoryUnavailableError, MemoryValidationError, MemoryNotFoundError) as exc:
        return f"[memory 错误] {exc}"
    return "[memory 错误] 未知命令"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="/memory", exit_on_error=False, add_help=True)
    sub = parser.add_subparsers(dest="action")
    add = sub.add_parser("add", exit_on_error=False)
    add.add_argument("--type", required=True, choices=[str(value) for value in MemoryType])
    add.add_argument("--tag", action="append", default=[])
    add.add_argument("content")
    listing = sub.add_parser("list", exit_on_error=False)
    listing.add_argument("--type", choices=[str(value) for value in MemoryType])
    listing.add_argument(
        "--status",
        default="active",
        choices=[str(value) for value in MemoryStatus],
    )
    listing.add_argument("--limit", type=int, default=20)
    search = sub.add_parser("search", exit_on_error=False)
    search.add_argument("query")
    search.add_argument("--type", choices=[str(value) for value in MemoryType])
    search.add_argument("--limit", type=int, default=20)
    show = sub.add_parser("show", exit_on_error=False)
    show.add_argument("id")
    delete = sub.add_parser("delete", exit_on_error=False)
    delete.add_argument("id")
    return parser


def _render_list(items: list[MemoryItem]) -> str:
    if not items:
        return "没有匹配的 Memory。"
    return "\n".join(
        f"{item.id:<18} {item.type!s:<14} {item.status:<10} {_preview(item.content)}"
        for item in items
    )


def _render_item(item: MemoryItem) -> str:
    fields = [
        ("id", item.id),
        ("scope", f"{item.scope}:{item.scope_id}"),
        ("type", item.type),
        ("source", item.source),
        ("status", item.status),
        ("version", item.version),
        ("created", _time(item.created_at)),
        ("updated", _time(item.updated_at)),
        ("tags", ", ".join(item.tags) or "-"),
        ("evidence", "; ".join(str(ref) for ref in item.evidence_refs) or "-"),
        ("content", item.content),
    ]
    return "\n".join(f"{key:<10} {value}" for key, value in fields)


def _preview(text: str) -> str:
    return " ".join(text.split())[:100]


def _time(value: datetime) -> str:
    return value.isoformat()
