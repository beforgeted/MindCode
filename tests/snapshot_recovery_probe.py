"""Actual process termination at non-Git publication boundaries, in disposable roots."""
from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from dataclasses import replace
from pathlib import Path

from codeagent.execution.publication import WorkspacePublication
from codeagent.llm.stub_client import StubLlmClient
from codeagent.orchestration.master_session import MasterSession
from codeagent.orchestration.planner import StaticPlanner
from codeagent.orchestration.task_graph import Step, TaskGraph
from tests.test_master_integration import _config
from tests.test_snapshot_recovery import NoPlanner


def config_for(root: Path, image: str):
    config = replace(_config(root), home=root / '.codeagent',
                     project_root=root / '.codeagent', execution_backend='podman',
                     sandbox_image=image, verify_command=(
                         'test "$(cat seed.txt)" = userX && test "$(cat note.txt)" = verified'))
    return replace(config, profile=replace(config.profile, master_max_replans=0,
                                           promote_max_retries=0))


async def main(root: Path, image: str, window: str, run_id: str = '') -> None:
    config = config_for(root, image)
    marker = root.parent / 'probe.json'
    if window != 'resume':
        save, write = WorkspacePublication._save, WorkspacePublication._write
        identity = {}

        def kill():
            marker.write_text(json.dumps(identity))
            os.kill(os.getpid(), getattr(signal, 'SIGKILL'))  # noqa: B009 (Linux-only probe)

        def save_gate(self, record):
            transaction = record.get('transaction')
            if transaction is not None:
                identity.update(transaction)
                if window == 'before_journal' and record['state'] == 'prepared':
                    kill()
            save(self, record)
            if transaction is not None and window == 'applied' and record['state'] == 'applied':
                kill()

        def write_gate(self, *args, **kwargs):
            write(self, *args, **kwargs)
            if window == 'partial' and identity:
                kill()

        WorkspacePublication._save = save_gate
        WorkspacePublication._write = write_gate
    client = StubLlmClient([] if window == 'resume' else [
        [('run_command', {'command': 'printf X >> seed.txt; printf verified > note.txt'})],
        'done',
    ])
    planner = NoPlanner() if window == 'resume' else StaticPlanner(
        TaskGraph([Step('one', 'default', 'append once and create note')]))
    async with MasterSession(config, llm_client=client, planner=planner) as session:
        assert session.master is not None
        if window == 'resume':
            result = await session.master.run('resume', session_id=session.session.session_id,
                                              resume_master_run_id=run_id)
        else:
            result = await session.run_task('append exactly once')
        print(json.dumps({'accepted': result.accepted, 'integrated': result.integrated,
                          'reason': result.reason, 'no_workers': result.scheduler is None}),
              flush=True)


if __name__ == '__main__':
    asyncio.run(main(Path(sys.argv[1]), *sys.argv[2:]))
