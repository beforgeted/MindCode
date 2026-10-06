from __future__ import annotations

import argparse
import json
from pathlib import Path

from codeagent.skills.package import install_package, load_package


def main() -> None:
    parser = argparse.ArgumentParser(description='Inspect/install local Agent Skills packages')
    parser.add_argument('action', choices=['inspect', 'install'])
    parser.add_argument('source', type=Path)
    parser.add_argument('--root', type=Path, default=Path('.codeagent/skills'))
    args = parser.parse_args()
    package = load_package(args.source)
    result = {'name': package.name, 'description': package.description,
              'sha256': package.digest, 'files': [p for p, _ in package.files]}
    if args.action == 'install':
        result['installed'] = str(install_package(args.source, args.root))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
