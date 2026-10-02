"""Controller-owned source executed with python -I inside the container only.

Never import this module in the controller; the project cannot replace its source.
Container output is untrusted evidence, not an authoritative snapshot.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path


def main() -> None:
    payload = json.load(sys.stdin)
    name, args = payload["tool"], payload["arguments"]
    root = Path("/workspace")
    raw = str(args.get("path") or ("." if name == "grep" else ""))
    if not raw or "\\" in raw or "\x00" in raw:
        raise ValueError("invalid workspace path")
    parts = raw.split("/")
    if raw != "." and any(p in ("", ".", "..") or ":" in p for p in parts):
        raise ValueError("path must be canonical relative POSIX")
    for part in parts:
        folded = part.casefold()
        if folded in (".git", ".codeagent", ".env") or folded.startswith(".env."):
            raise ValueError("protected path")
    path = (root / raw).resolve()
    path.relative_to(root)
    if name == "write_file":
        content = args.get("content")
        if content is None:
            raise ValueError("missing content")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(content), encoding="utf-8")
        print(f"写入 {raw}（{len(str(content))} 字符）")
    elif name == "read_file":
        offset = max(1, int(args.get("offset", 1)))
        limit = max(1, min(2000, int(args.get("limit", 2000))))
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        print(f"{raw} (行 {offset}-{min(len(lines), offset + limit - 1)} / 共 {len(lines)} 行)")
        for i, line in enumerate(lines[offset - 1:offset - 1 + limit], offset):
            print(f"{i:>6}\t{line[:2000]}")
    elif name == "grep":
        pattern = str(args.get("pattern", ""))
        if not pattern:
            raise ValueError("missing pattern")
        regex = re.compile(pattern)
        maximum = max(1, min(2000, int(args.get("max_results", 200))))
        hits = scanned = 0
        for file in path.rglob(str(args.get("glob") or "*")):
            if file.is_symlink() or not file.is_file() or file.stat().st_size > 2 * 1024 * 1024:
                continue
            file.resolve().relative_to(root)
            if any(p in ("__pycache__", "node_modules", ".git") for p in file.parts):
                continue
            scanned += 1
            lines = file.read_text(encoding="utf-8", errors="replace").splitlines()
            for i, line in enumerate(lines, 1):
                if regex.search(line):
                    print(f"{file.relative_to(root)}:{i}: {line.strip()[:300]}")
                    hits += 1
                    if hits >= maximum:
                        print(f"[已达上限 {maximum}，结果可能不完整]")
                        return
        print(f"共 {hits} 条匹配（扫描 {scanned} 个文件）")
    else:
        raise ValueError("unsupported file tool")


if __name__ == "__main__":
    main()
