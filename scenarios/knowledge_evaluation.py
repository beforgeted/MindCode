"""Offline lexical retrieval comparison on the frozen MindCode repository.

Same explicit queries for the index and current grep; no task/model quality claim.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import platform
import time
from pathlib import Path

from codeagent.context.token_estimator import estimate_text
from codeagent.evidence.artifact_store import FileArtifactStore
from codeagent.infra.cancellation import CancellationToken
from codeagent.knowledge import index
from codeagent.knowledge.tool import KnowledgeTool
from codeagent.tool.base import ToolExecutionContext
from codeagent.tool.builtin.grep import GrepTool
from codeagent.workspace.context import WorkspaceContext

TASKS = [
    ('easy', 'SandboxExecutor', 'symbol', 'codeagent/tool/executor.py'),
    ('easy', 'read_project_file', 'symbol', 'codeagent/tool/mcp/project_server.py'),
    ('medium', 'ControlledFetcher', 'symbol', 'codeagent/execution/fetch.py'),
    ('medium', 'SnapshotEntry', 'symbol', 'codeagent/execution/snapshot.py'),
    ('hard', 'Fetch robots', 'document', 'docs/mcp-tools.md'),
    ('hard', '候选', 'document', 'docs/architecture.md'),
]


async def evaluate(root: Path, output: Path) -> dict:
    if await asyncio.to_thread(output.exists):
        raise ValueError('evaluation output already exists')
    await asyncio.to_thread(output.mkdir, parents=True)
    ctx = ToolExecutionContext(
        agent_run_id='offline-evaluation', session_id='knowledge', tool_run_id='retrieval',
        call_id='query', workspace=WorkspaceContext.local(root),
        cancellation=CancellationToken(), artifact_store=FileArtifactStore(output / 'artifacts'),
    )
    rows = []
    search, getter, grep = KnowledgeTool('search'), KnowledgeTool('get'), GrepTool()
    for level, query, kind, expected in TASKS:
        started = time.perf_counter()
        result = await search.execute(ctx, {'query': query, 'kind': kind, 'limit': 5})
        elapsed = (time.perf_counter() - started) * 1000
        if result.is_error:
            raise ValueError('knowledge evaluation query failed')
        data = json.loads(result.content)
        hits = data['result']['matches']
        valid = 0
        for hit in hits:
            args = {k: hit[k] for k in ('path', 'start_line', 'end_line',
                                       'file_sha256', 'index_version')}
            read = await getter.execute(ctx, args)
            if read.is_error:
                raise ValueError('evaluation returned a stale citation')
            content = await asyncio.to_thread((root / hit['path']).read_bytes)
            lines = content.decode('utf-8').splitlines()
            expected_text = '\n'.join(lines[hit['start_line'] - 1:hit['end_line']])
            assert json.loads(read.content)['result']['excerpt'] == expected_text
            assert hashlib.sha256(content).hexdigest() == hit['file_sha256']
            valid += 1
        started = time.perf_counter()
        baseline = await grep.execute(ctx, {'pattern': query, 'max_results': 200})
        baseline_ms = (time.perf_counter() - started) * 1000
        assert not baseline.is_error
        rows.append({
            'level': level, 'query': query, 'expected_path': expected,
            'knowledge_top1': bool(hits and hits[0]['path'] == expected),
            'knowledge_top5': any(h['path'] == expected for h in hits),
            'valid_citations': valid, 'citations': len(hits),
            'knowledge_ms': round(elapsed, 3), 'grep_ms': round(baseline_ms, 3),
            'knowledge_estimated_tokens': estimate_text(result.content),
            'grep_estimated_tokens': estimate_text(baseline.content),
            'grep_contains_expected_path': expected + ':' in baseline.content.replace('\\', '/'),
            'index_stats': data['stats'], 'index_version': data['index_version'],
            'knowledge_search_calls': 1, 'citation_validation_calls': valid, 'grep_calls': 1,
        })
    files, policies = await asyncio.to_thread(index.collect, root)
    manifest = {name: hashlib.sha256(value).hexdigest() for name, value in files.items()}
    report = {'repository': 'MindCode frozen candidate', 'files': len(files),
              'bytes': sum(map(len, files.values())), 'source_manifest': manifest,
              'ignore_policies': policies, 'platform': platform.platform(),
              'python': platform.python_version(), 'rows': rows, 'paid_model_calls': 0,
              'boundary': 'lexical comparison; no model task success or exact token billing claim'}
    await asyncio.to_thread((output / 'report.json').write_text,
                            json.dumps(report, indent=2, ensure_ascii=False), encoding='utf-8')
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = asyncio.run(evaluate(args.root.resolve(), args.output.resolve()))
    print(json.dumps({'queries': len(result['rows']), 'files': result['files'],
                      'top5': sum(row['knowledge_top5'] for row in result['rows']),
                      'valid_citations': sum(row['valid_citations'] for row in result['rows'])}))


if __name__ == '__main__':
    main()
