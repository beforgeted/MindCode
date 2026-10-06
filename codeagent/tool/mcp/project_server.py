"""Standalone bundled MCP stdio server. Only stdlib; also runs inside trusted images.

This service implements initialization, tool discovery and two text-only tools.
It never executes project code, follows links, accepts server commands or writes files.
"""
from __future__ import annotations

import ast
import json
import os
import stat
import sys
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

VERSIONS = ('2025-11-25', '2025-06-18')
MAX_FILE_BYTES = 256 * 1024
MAX_MESSAGE_BYTES = 1024 * 1024
CATALOG = [
    {'name': 'read_text', 'description': '读取授权项目内文本，返回行号；拒绝隐藏路径和链接。',
     'inputSchema': {'type': 'object', 'properties': {
         'path': {'type': 'string', 'minLength': 1, 'maxLength': 1024},
         'offset': {'type': 'integer', 'minimum': 1, 'maximum': 1000000},
         'limit': {'type': 'integer', 'minimum': 1, 'maximum': 200},
     }, 'required': ['path'], 'additionalProperties': False}},
    {'name': 'python_symbols', 'description': '静态解析授权项目的 Python 文件，查询定义及行号。',
     'inputSchema': {'type': 'object', 'properties': {
         'path': {'type': 'string', 'minLength': 1, 'maxLength': 1024},
     }, 'required': ['path'], 'additionalProperties': False}},
]


def validate_arguments(name: str, arguments: Any) -> dict:
    allowed = {'path', 'offset', 'limit'} if name == 'read_text' else {'path'}
    if (name not in {t['name'] for t in CATALOG} or not isinstance(arguments, dict)
            or set(arguments) - allowed):
        raise ValueError('unsupported tool or arguments')
    path = arguments.get('path')
    if (not isinstance(path, str) or not 1 <= len(path) <= 1024
            or '\\' in path or '\x00' in path or ':' in path
            or PurePosixPath(path).is_absolute() or PureWindowsPath(path).drive
            or any(not p or p.startswith('.') for p in path.split('/'))):
        raise ValueError('only relative non-hidden project paths are allowed')
    for key, maximum in [('offset', 1000000), ('limit', 200)]:
        if key in arguments and (type(arguments[key]) is not int
                                 or not 1 <= arguments[key] <= maximum):
            raise ValueError('invalid line range')
    return arguments


def read_project_file(path: str) -> str:
    parts = path.split('/')
    if os.name == 'posix':
        directory = os.open('.', os.O_RDONLY | os.O_DIRECTORY)
        try:
            for part in parts[:-1]:
                next_dir = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                   dir_fd=directory)
                os.close(directory)
                directory = next_dir
            fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                         dir_fd=directory)
        finally:
            os.close(directory)
        stream = os.fdopen(fd, 'rb')
    else:
        candidate = Path.cwd()
        for part in parts:
            candidate /= part
            info = candidate.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                raise ValueError('links and reparse points are unsupported')
        stream = candidate.open('rb')
    with stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_BYTES:
            raise ValueError('requires a regular text file <=256KiB')
        data = stream.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES or b'\x00' in data:
        raise ValueError('file is oversized or binary')
    return data.decode('utf-8')


def invoke(name: str, arguments: Any) -> dict:
    try:
        arguments = validate_arguments(name, arguments)
        text = read_project_file(arguments['path'])
        if name == 'read_text':
            offset, limit = arguments.get('offset', 1), arguments.get('limit', 200)
            lines = text.splitlines()
            body = '\n'.join(f'{i + offset}: {line[:2000]}'
                             for i, line in enumerate(lines[offset - 1:offset - 1 + limit]))
        else:
            if not arguments['path'].endswith('.py'):
                raise ValueError('python_symbols requires a .py file')
            tree = ast.parse(text)
            nodes = (n for n in ast.walk(tree)
                     if isinstance(n, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)))
            symbols = []
            for node in nodes:
                symbols.append({'name': node.name, 'kind': type(node).__name__,
                                'line': node.lineno})
                if len(symbols) == 200:
                    break
            body = json.dumps({'path': arguments['path'], 'symbols': symbols}, ensure_ascii=False)
        return {'content': [{'type': 'text', 'text': body}], 'isError': False}
    except (ValueError, OSError, SyntaxError, UnicodeError) as exc:
        # Do not return host paths, file contents or exception messages on failure.
        message = f'project tool rejected: {type(exc).__name__}'
        return {'content': [{'type': 'text', 'text': message}],
                'isError': True}


def main() -> None:
    initialized = False
    negotiated = False
    while True:
        line = sys.stdin.buffer.readline(MAX_MESSAGE_BYTES + 1)
        if not line:
            return
        if len(line) > MAX_MESSAGE_BYTES or not line.endswith(b'\n'):
            return
        request = json.loads(line)
        method, request_id = request.get('method'), request.get('id')
        if request_id is None:
            if method == 'notifications/initialized' and negotiated:
                initialized = True
            continue
        error = None
        result = {}
        if method == 'initialize':
            requested = request.get('params', {}).get('protocolVersion')
            version = requested if requested in VERSIONS else VERSIONS[0]
            result = {'protocolVersion': version, 'capabilities': {'tools': {}},
                      'serverInfo': {'name': 'mindcode-project', 'version': '1.0'}}
            negotiated = True
        elif not initialized:
            error = {'code': -32000, 'message': 'not initialized'}
        elif method == 'tools/list':
            result = {'tools': CATALOG}
        elif method == 'tools/call':
            params = request.get('params', {})
            result = invoke(params.get('name'), params.get('arguments', {}))
        elif method == 'ping':
            result = {}
        else:
            error = {'code': -32601, 'message': 'unsupported method'}
        response = {'jsonrpc': '2.0', 'id': request_id}
        response['error' if error else 'result'] = error if error else result
        sys.stdout.write(json.dumps(response, ensure_ascii=True) + '\n')
        sys.stdout.flush()


if __name__ == '__main__':
    main()
