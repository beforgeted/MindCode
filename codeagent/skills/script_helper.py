"""Controller-owned Linux launcher. Never run Skill scripts on the host.

The whole frozen package is materialized in a fresh owned tmpfs directory. Python
isolated mode prevents startup imports from the mutable workspace or user site.
This is the same Worker domain, not an additional sandbox within that domain.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import signal
import sys
import tempfile
from pathlib import Path, PurePosixPath


async def run(payload: dict) -> dict:
    with tempfile.TemporaryDirectory(prefix='mindcode-skill-', dir='/tmp') as directory:
        root = Path(directory)
        files = payload['files']
        if not isinstance(files, list) or not 1 <= len(files) <= 1000:
            raise ValueError('invalid frozen package')
        total = 0
        for name, encoded in files:
            path = PurePosixPath(name)
            if (path.is_absolute() or any(p in ('..', '.') for p in path.parts)
                    or str(path) != name or '\\' in name):
                raise ValueError('invalid frozen package path')
            raw = base64.b64decode(encoded, validate=True)
            total += len(raw)
            if len(raw) > 1024 * 1024 or total > 10 * 1024 * 1024:
                raise ValueError('frozen package exceeds limit')
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open('xb') as stream:
                stream.write(raw)
        # Only the controller supplies this path; the Tool already checks its allowlist.
        if payload['script'] not in {name for name, _ in files}:
            raise ValueError('script missing from frozen package')
        env = {'PATH': os.environ.get('PATH', '/usr/local/bin:/usr/bin:/bin'),
               'HOME': '/tmp', 'LANG': 'C.UTF-8', 'PYTHONIOENCODING': 'utf-8'}
        process = await asyncio.create_subprocess_exec(
            sys.executable, '-I', str(root / payload['script']), *payload['args'],
            cwd='/workspace', env=env, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        size = 0
        async def bounded(stream):
            nonlocal size
            chunks = []
            while block := await stream.read(8192):
                size += len(block)
                if size > payload['max_output_bytes']:
                    raise ValueError('script output exceeds limit')
                chunks.append(block)
            return base64.b64encode(b''.join(chunks)).decode()
        try:
            async with asyncio.TaskGroup() as group:
                stdout = group.create_task(bounded(process.stdout))
                stderr = group.create_task(bounded(process.stderr))
                group.create_task(process.wait())
            return {'stdout': stdout.result(), 'stderr': stderr.result(),
                    'exit_code': process.returncode}
        finally:
            # Clean descendants even when the entry script exits successfully.
            try:
                getattr(os, 'killpg')(process.pid, getattr(signal, 'SIGKILL'))  # noqa: B009
            except ProcessLookupError:
                pass
            async def discard(stream):
                while await stream.read(8192):
                    pass
            await asyncio.gather(discard(process.stdout), discard(process.stderr), process.wait())


def main() -> None:
    try:
        raw = sys.stdin.buffer.read(16 * 1024 * 1024 + 1)
        if len(raw) > 16 * 1024 * 1024:
            raise ValueError('script request exceeds limit')
        result = asyncio.run(run(json.loads(raw)))
        print(json.dumps(result, ensure_ascii=True))
    except Exception as exc:
        print(json.dumps({'bridge_error': type(exc).__name__}))
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
