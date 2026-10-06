"""Bounded, immutable Agent Skills packages. Package content is data, not permission."""
from __future__ import annotations

import hashlib
import os
import shutil
import stat
import tempfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MAX_FILE = 1024 * 1024
MAX_PACKAGE = 10 * MAX_FILE


@dataclass(frozen=True, slots=True)
class SkillPackage:
    name: str
    description: str
    body: str
    files: tuple[tuple[str, bytes], ...]
    digest: str
    # Frontmatter is preserved verbatim in SKILL.md. It cannot grant tools.


def _frontmatter(text: str) -> dict[str, Any]:
    try:
        import yaml
    except ImportError:
        raise ValueError('Agent Skills packages require pip install codeagent[ecosystem]') from None
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != '---':
        raise ValueError('SKILL.md requires YAML frontmatter')
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == '---'), None)
    if end is None:
        raise ValueError('unclosed YAML frontmatter')
    header = ''.join(lines[1:end])
    if len(header.encode()) > 16384:
        raise ValueError('frontmatter exceeds 16KiB')

    class UniqueLoader(yaml.SafeLoader):
        def compose_node(self, parent, index):
            if self.check_event(yaml.AliasEvent):
                raise ValueError('YAML aliases are unsupported')
            return super().compose_node(parent, index)

        def construct_mapping(self, node, deep=False):
            result = {}
            for key_node, value_node in node.value:
                key = self.construct_object(key_node, deep=deep)
                if not isinstance(key, str) or key in result:
                    raise ValueError('YAML requires unique string keys')
                result[key] = self.construct_object(value_node, deep=deep)
            return result

    try:
        data = yaml.load(header, Loader=UniqueLoader)
    except yaml.YAMLError:
        raise ValueError('invalid YAML frontmatter') from None
    if not isinstance(data, dict):
        raise ValueError('frontmatter must be a mapping')
    data['_body'] = ''.join(lines[end + 1:]).strip()
    return data


def load_package(root: Path) -> SkillPackage:
    root = Path(root).absolute()
    files: list[tuple[str, bytes]] = []
    total = 0
    for directory, dirs, names in os.walk(root, followlinks=False):
        current = Path(directory)
        for entry in [current, *(current / n for n in dirs + names)]:
            mode = entry.lstat()
            if entry.is_symlink() or getattr(mode, 'st_file_attributes', 0) & 0x400:
                raise ValueError('package links and reparse points are unsupported')
            if not (stat.S_ISDIR(mode.st_mode) or stat.S_ISREG(mode.st_mode)):
                raise ValueError('package entries must be regular files or directories')
            if entry != root and (entry.name in {'.git', '.private-docs', '.codeagent',
                                                '.ssh', '.aws', '.credentials'}
                                  or entry.name == '.env' or entry.name.startswith('.env.')):
                raise ValueError('private package entries are unsupported')
        for name in sorted(names):
            path = current / name
            if path.stat().st_size > MAX_FILE:
                raise ValueError('package file exceeds 1MiB')
            with path.open('rb') as stream:
                raw = stream.read(MAX_FILE + 1)
            total += len(raw)
            if len(raw) > MAX_FILE or total > MAX_PACKAGE or len(files) >= 1000:
                raise ValueError('package exceeds file/count/total limits')
            files.append((path.relative_to(root).as_posix(), raw))
    content = dict(files).get('SKILL.md')
    if content is None or len(content) > 65536:
        raise ValueError('package requires SKILL.md at most 64KiB')
    data = _frontmatter(content.decode('utf-8-sig'))
    if set(data) - {'name', 'description', 'license', 'compatibility', 'metadata',
                    'allowed-tools', '_body'}:
        raise ValueError('unknown Agent Skills frontmatter field')
    name, description = data.get('name'), data.get('description')
    if isinstance(name, str):
        name = unicodedata.normalize('NFKC', name)
    if (not isinstance(name, str) or not name or len(name) > 64 or name != name.lower()
            or name.startswith('-') or name.endswith('-') or '--' in name
            or not all(c.isalnum() or c == '-' for c in name)
            or name != unicodedata.normalize('NFKC', root.name)):
        raise ValueError('standard name must match its directory and use lowercase hyphenated text')
    if not isinstance(description, str) or not description.strip() or len(description) > 1024:
        raise ValueError('description must contain 1..1024 characters')
    if not data['_body']:
        raise ValueError('skill body is empty')
    for key in ('license', 'compatibility', 'allowed-tools'):
        if key in data and not isinstance(data[key], str):
            raise ValueError(f'{key} must be text')
    if len(data.get('compatibility', '')) > 500:
        raise ValueError('compatibility exceeds 500 characters')
    metadata = data.get('metadata', {})
    if not isinstance(metadata, dict) or any(not isinstance(v, str) for v in metadata.values()):
        raise ValueError('metadata must map strings to strings')
    frozen = tuple(sorted(files))
    digest = hashlib.sha256()
    for path, raw in frozen:
        digest.update(path.encode() + b'\0' + hashlib.sha256(raw).digest())
    return SkillPackage(name, description, data['_body'], frozen, digest.hexdigest())


def install_package(source: Path, destination_root: Path) -> Path:
    package = load_package(source)
    destination_root = destination_root.absolute()
    destination_root.mkdir(parents=True, exist_ok=True)
    destination = destination_root / package.name
    if destination.exists() or destination.is_symlink():
        raise FileExistsError('installed package already exists; choose a new installation root')
    staging = Path(tempfile.mkdtemp(prefix='skill-install-', dir=destination_root))
    try:
        for path, raw in package.files:
            target = staging / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
        # No metadata is added to the upstream package; its bytes remain unchanged.
        staging.rename(destination)
        return destination
    finally:
        if staging.exists():
            shutil.rmtree(staging)
