"""Trusted container-side bounded binary write with dirfd/O_NOFOLLOW anchoring."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
import sys
from uuid import uuid4


def main() -> None:
    request = json.load(sys.stdin)
    path = request['path']
    if not isinstance(path, str) or not path or len(path.encode()) > 4096 or '\\' in path:
        raise ValueError('invalid relative download path')
    parts = path.split('/')
    if len(parts) > 128 or any(p in ('', '.', '..') or ':' in p or '\x00' in p for p in parts):
        raise ValueError('invalid relative download path')
    if any(p.casefold() in ('.git', '.codeagent', '.env') or p.casefold().startswith('.env.')
           for p in parts):
        raise ValueError('protected path')
    data = base64.b64decode(request['data'], validate=True)
    if len(data) > request['max_bytes'] or hashlib.sha256(data).hexdigest() != request['sha256']:
        raise ValueError('invalid download bytes')
    directory_flag, nofollow_flag, nonblock_flag = (
        getattr(os, name) for name in ('O_DIRECTORY', 'O_NOFOLLOW', 'O_NONBLOCK')
    )
    flags = os.O_RDONLY | directory_flag | nofollow_flag
    parent = os.open('/workspace', flags)
    temporary = ''
    try:
        for part in parts[:-1]:
            try:
                os.mkdir(part, 0o700, dir_fd=parent)
            except FileExistsError:
                pass
            next_fd = os.open(part, flags, dir_fd=parent)
            os.close(parent)
            parent = next_fd
        try:
            existing = os.open(parts[-1], os.O_RDONLY | nofollow_flag | nonblock_flag,
                               dir_fd=parent)
        except FileNotFoundError:
            existing = None
        if existing is not None:
            try:
                info = os.fstat(existing)
                if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                        or info.st_size > request['max_bytes']):
                    raise ValueError('destination is not a bounded regular file')
                old = bytearray()
                while chunk := os.read(existing, min(65536, request['max_bytes'] - len(old) + 1)):
                    old.extend(chunk)
                    if len(old) > request['max_bytes']:
                        raise ValueError('destination exceeds byte limit')
                if hashlib.sha256(old).hexdigest() != request['sha256']:
                    raise ValueError('destination already exists with different content')
                print(json.dumps({'path': path, 'bytes': len(old), 'reused': True}))
                return
            finally:
                os.close(existing)
        temporary = '.mindcode-download-' + uuid4().hex
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow_flag,
                     0o600, dir_fd=parent)
        with os.fdopen(fd, 'wb') as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        # Atomic creation without following/overwriting any concurrent destination.
        os.link(temporary, parts[-1], src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
        os.unlink(temporary, dir_fd=parent)
        temporary = ''
        print(json.dumps({'path': path, 'bytes': len(data), 'reused': False}))
    finally:
        if temporary:
            os.unlink(temporary, dir_fd=parent)
        os.close(parent)


if __name__ == '__main__':
    main()
