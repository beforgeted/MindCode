"""Stdlib-only controller-captured helper, executed with Python -I.

No repository imports or project writes. Cache is controller-owned derived data;
results are untrusted evidence, never a publication or recovery authority.
"""
from __future__ import annotations

import ast
import fnmatch
import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path
from typing import Any

VERSION = 'knowledge-v1'
MAX_FILES = 1024
MAX_ENTRIES = 8192
MAX_FILE_BYTES = 256 * 1024
MAX_TOTAL_BYTES = 8 * 1024 * 1024
MAX_ENVELOPE = 4 * 1024 * 1024
SKIP_DIRS = {'node_modules', 'venv', 'dist', 'build', 'target', '__pycache__'}
PRIVATE_NAMES = {'credentials', 'credentials.json', 'secrets', 'secrets.json',
                 'id_rsa', 'id_ed25519', 'private_key', 'private-key'}
PRIVATE_SUFFIXES = {'.pem', '.key', '.p12', '.pfx', '.keystore'}
DOCUMENT_SUFFIXES = {'.md', '.rst', '.txt'}


class StaleIndex(ValueError):
    """Citation must be refreshed against the current content version."""


def protected(path: str) -> bool:
    return any(part.startswith('.') or part.casefold() in SKIP_DIRS | PRIVATE_NAMES
               or Path(part).suffix.casefold() in PRIVATE_SUFFIXES for part in path.split('/'))


def canonical(path: Any) -> bool:
    return (isinstance(path, str) and 1 <= len(path) <= 1024 and '\\' not in path
            and ':' not in path and '\x00' not in path
            and all(p not in ('', '.', '..') for p in path.split('/')))


def digest(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r'[0-9a-f]{64}', value) is not None


def validate_arguments(action: str, args: Any) -> dict:
    if not isinstance(args, dict):
        raise ValueError('arguments must be an object')
    if action == 'search':
        if (set(args) - {'query', 'kind', 'limit', 'expected_version'}
                or not isinstance(args.get('query'), str)
                or not 1 <= len(args['query'].strip()) <= 256
                or not re.findall(r'\w+', args['query'], flags=re.UNICODE)
                or args.get('kind', 'all') not in ('all', 'path', 'symbol', 'document')
                or type(args.get('limit', 10)) is not int
                or not 1 <= args.get('limit', 10) <= 20):
            raise ValueError('invalid knowledge search')
        if 'expected_version' in args and not digest(args['expected_version']):
            raise ValueError('invalid expected content version')
    elif action == 'get':
        if (set(args) != {'path', 'start_line', 'end_line', 'file_sha256', 'index_version'}
                or not canonical(args.get('path')) or protected(args['path'])
                or not digest(args['file_sha256']) or not digest(args['index_version'])
                or type(args['start_line']) is not int or type(args['end_line']) is not int
                or not 1 <= args['start_line'] <= args['end_line'] <= 1000000
                or args['end_line'] - args['start_line'] >= 40):
            raise ValueError('invalid knowledge citation')
    else:
        raise ValueError('unsupported knowledge action')
    return args


def _link(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, 'st_file_attributes', 0) & 0x400)


def read_file(root: Path, name: str) -> bytes:
    """POSIX descriptor traversal; Windows reparse points rejected per component."""
    if not canonical(name):
        raise ValueError('invalid indexed path')
    parts = name.split('/')
    if os.name == 'posix':
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        directory = os.open(root, flags)
        try:
            for part in parts[:-1]:
                next_dir = os.open(part, flags, dir_fd=directory)
                os.close(directory)
                directory = next_dir
            fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                         dir_fd=directory)
        finally:
            os.close(directory)
        stream = os.fdopen(fd, 'rb')
    else:
        candidate = root
        for part in parts:
            candidate /= part
            if _link(candidate.lstat()):
                raise ValueError('links are not index input')
        stream = candidate.open('rb')
    with stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise ValueError('requires a regular, unshared file')
        if before.st_size > MAX_FILE_BYTES:
            raise OverflowError('indexed file exceeds byte limit')
        data = stream.read(MAX_FILE_BYTES + 1)
        after = os.fstat(stream.fileno())
    if len(data) > MAX_FILE_BYTES:
        raise OverflowError('indexed file exceeds byte limit')
    if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_size, after.st_mtime_ns, after.st_ctime_ns):
        raise ValueError('file changed during indexing')
    return data


def _glob(pattern: str, name: str) -> bool:
    """Component globs: * cannot cross /; ** can. Negation never widens scope."""
    left, right = pattern.split('/'), name.split('/')
    def match(i: int, j: int) -> bool:
        if i == len(left):
            return j == len(right)
        if left[i] == '**':
            return match(i + 1, j) or (j < len(right) and match(i, j + 1))
        return j < len(right) and fnmatch.fnmatchcase(right[j], left[i]) and match(i + 1, j + 1)
    return match(0, 0)


def ignored(name: str, rules: list[tuple[str, str]]) -> bool:
    for base, raw in rules:
        if base and not name.startswith(base + '/'):
            continue
        rel = name[len(base) + 1:] if base else name
        anchored = raw.startswith('/')
        pattern = raw.strip('/')
        parts = rel.split('/')
        candidates = ['/'.join(parts[:i]) for i in range(1, len(parts) + 1)]
        if '/' not in pattern and not anchored:
            if any(fnmatch.fnmatchcase(part, pattern) for part in parts):
                return True
        elif any(_glob(pattern, candidate) for candidate in candidates):
            return True
    return False


def collect(root: Path) -> tuple[dict[str, bytes], dict[str, str]]:
    files: dict[str, bytes] = {}
    policies: dict[str, str] = {}
    total = visited = 0
    def walk(directory: Path, inherited: list[tuple[str, str]], depth: int) -> None:
        nonlocal total, visited
        if depth > 32:
            raise ValueError('index directory depth exceeds limit')
        rel_dir = directory.relative_to(root).as_posix()
        if rel_dir == '.':
            rel_dir = ''
        rules = list(inherited)
        ignore_path = directory / '.gitignore'
        if ignore_path.exists() or ignore_path.is_symlink():
            name = '/'.join(filter(None, (rel_dir, '.gitignore')))
            raw = read_file(root, name)
            if len(raw) > 16384:
                raise ValueError('ignore policy exceeds limit')
            policies[name] = hashlib.sha256(raw).hexdigest()
            for row in raw.decode('utf-8').splitlines():
                row = row.rstrip()
                if not row or row.startswith('#') or row.startswith('!'):
                    continue
                if '\\' in row or len(row) > 256:
                    raise ValueError('unsupported ignore policy syntax')
                rules.append((rel_dir, row))
        for child in sorted(directory.iterdir()):
            visited += 1
            if visited > 8192:
                raise ValueError('index entry count exceeds limit')
            name = child.relative_to(root).as_posix()
            if protected(name) or ignored(name, rules):
                continue
            info = child.lstat()
            if _link(info):
                continue
            if stat.S_ISDIR(info.st_mode):
                walk(child, rules, depth + 1)
            elif stat.S_ISREG(info.st_mode):
                if not canonical(name) or info.st_nlink != 1:
                    continue
                if info.st_size > MAX_FILE_BYTES:
                    raise OverflowError('eligible file exceeds index limit')
                data = read_file(root, name)
                total += len(data)
                if len(files) >= MAX_FILES or total > MAX_TOTAL_BYTES:
                    raise OverflowError('index scope exceeds file/byte limit')
                if b'\x00' in data:
                    continue
                try:
                    data.decode('utf-8')
                except UnicodeError:
                    continue
                files[name] = data
    if _link(root.lstat()):
        raise ValueError('index root must not be a link')
    walk(root, [], 0)
    return files, policies


def extract(path: str, data: bytes) -> tuple[list[dict], bool]:
    lines = data.decode('utf-8').splitlines()
    rows = []
    def add(kind: str, name: str, start: int, end: int) -> None:
        excerpt = '\n'.join(lines[start - 1:end])
        rows.append({'kind': kind, 'name': name, 'start_line': start, 'end_line': end,
                     'excerpt': excerpt[:2000], 'excerpt_truncated': len(excerpt) > 2000})
    if lines:
        add('path', path, 1, 1)
    invalid_python = False
    if path.endswith('.py'):
        try:
            tree = ast.parse(data.decode('utf-8'))
        except (SyntaxError, ValueError):
            invalid_python = True
        else:
            def visit(node: ast.AST, parents: tuple[str, ...]) -> None:
                for child in ast.iter_child_nodes(node):
                    if isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                        qualified = (*parents, child.name)
                        add('symbol', '.'.join(qualified), child.lineno,
                            min(child.end_lineno or child.lineno, child.lineno + 19))
                        visit(child, qualified)
                    else:
                        visit(child, parents)
            visit(tree, ())
    if Path(path).suffix.casefold() in DOCUMENT_SUFFIXES:
        start = None
        for i, line in enumerate([*lines, ''], 1):
            if start is not None and (not line.strip() or line.startswith('#') or i - start == 20):
                add('document', lines[start - 1][:160], start, i - 1)
                start = None
            if line.strip() and start is None:
                start = i
    return rows, invalid_python


def invoke(root: Path, action: str, arguments: Any, cache: dict | None = None) -> dict:
    args = validate_arguments(action, arguments)
    files, policies = collect(root)
    hashes = {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}
    manifest = {'algorithm': VERSION, 'files': hashes, 'ignore_policies': policies}
    version = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
    expected = args.get('expected_version', args.get('index_version'))
    if expected is not None and expected != version:
        raise StaleIndex('knowledge citation/version is stale; search again')
    old = cache.get('files', {}) if cache and cache.get('algorithm') == VERSION else {}
    new: dict[str, dict] = {}
    rebuilt = reused = records = invalid = 0
    for name, data in files.items():
        previous = old.get(name)
        if previous and previous.get('sha256') == hashes[name]:
            entry = previous
            reused += 1
        else:
            rows, bad_python = extract(name, data)
            entry = {'sha256': hashes[name], 'rows': rows, 'invalid_python': bad_python}
            rebuilt += 1
        records += len(entry['rows'])
        invalid += bool(entry['invalid_python'])
        if records > MAX_ENTRIES:
            raise OverflowError('index record count exceeds limit')
        new[name] = entry
    state = {'algorithm': VERSION, 'files': new}
    stats = {'files': len(new), 'records': records, 'rebuilt': rebuilt, 'reused': reused,
             'removed': len(set(old) - set(new)), 'invalid_python': invalid,
             'hashed_bytes': sum(len(data) for data in files.values())}
    if action == 'get':
        name = args['path']
        if name not in files or hashes[name] != args['file_sha256']:
            raise StaleIndex('knowledge source missing or changed')
        lines = files[name].decode('utf-8').splitlines()
        if args['end_line'] > len(lines):
            raise ValueError('citation line range is invalid')
        excerpt = '\n'.join(lines[args['start_line'] - 1:args['end_line']])
        if len(excerpt.encode('utf-8')) > 16384:
            raise OverflowError('citation text exceeds limit')
        result = {**args, 'excerpt': excerpt}
    else:
        terms = re.findall(r'\w+', args['query'].casefold())
        matches = []
        for name, entry in new.items():
            for row in entry['rows']:
                if args.get('kind', 'all') not in ('all', row['kind']):
                    continue
                label, path = row['name'].casefold(), name.casefold()
                excerpt = row['excerpt'].casefold()
                if not all(t in label or t in path or t in excerpt for t in terms):
                    continue
                score = sum((40 if t == label.split('.')[-1] else 12 if t in label else 0)
                            + (8 if t in path else 0) + (1 if t in excerpt else 0) for t in terms)
                matches.append({'path': name, 'file_sha256': entry['sha256'],
                                'index_version': version, 'score': score, **row})
        matches.sort(key=lambda m: (-m['score'], m['path'], m['start_line'], m['kind']))
        result = {'matches': matches[:args.get('limit', 10)], 'total_matches': len(matches)}
    response = {'index_version': version, 'result': result, 'stats': stats, 'cache': state}
    if len(json.dumps(response, ensure_ascii=False).encode()) > MAX_ENVELOPE:
        raise OverflowError('derived index exceeds transport limit')
    return response


def main() -> None:
    try:
        raw = sys.stdin.buffer.read(MAX_ENVELOPE + 1)
        if len(raw) > MAX_ENVELOPE:
            raise OverflowError('index request exceeds limit')
        payload = json.loads(raw)
        root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path('/workspace')
        response = invoke(root, payload['action'], payload['arguments'], payload.get('cache'))
    except (OSError, ValueError, OverflowError, RecursionError) as exc:
        response = {'error': type(exc).__name__}
    sys.stdout.buffer.write(json.dumps(response, ensure_ascii=False).encode('utf-8'))


if __name__ == '__main__':
    main()
