"""Disposable cross-process MasterRuntime probe; never targets a user repository."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from codeagent.orchestration.run_store import SqliteRunStore
from codeagent.tool.approval import AllowExternalApprovalPolicy
from tests.test_deferred_execution import _master


async def main(repo: Path, hold: bool) -> None:
    store = SqliteRunStore(repo / '.home/runs.db')
    await store.start()
    class Gate:
        async def approve(self, decision, *, command):
            (repo / 'gate-owned').write_text('owned')
            for _ in range(3000):
                if (repo / 'gate-release').exists():
                    break
                await asyncio.sleep(0.01)
            else:
                raise TimeoutError('test gate was not released')
            return True
    master = _master(repo, Gate() if hold else AllowExternalApprovalPolicy(), store)
    final = await master.run('', session_id='child', resume_master_run_id='m')
    print(json.dumps({'integrated': final.integrated, 'executed': final.deferred_executed,
                      'unknown': final.deferred_unknown, 'reason': final.reason}), flush=True)


if __name__ == '__main__':
    asyncio.run(main(Path(sys.argv[1]), sys.argv[2] == 'hold'))
