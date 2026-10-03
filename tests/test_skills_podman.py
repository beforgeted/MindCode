from __future__ import annotations

import os
import sys
from dataclasses import replace

import pytest

from codeagent.llm.message import ToolResultBlock
from codeagent.llm.stub_client import StubLlmClient
from codeagent.orchestration.master_session import MasterSession
from codeagent.orchestration.planner import StaticPlanner
from codeagent.orchestration.task_graph import Step, TaskGraph
from codeagent.skills.config import SkillConfig
from tests.test_master_integration import _config, _git_out, _init_repo, _worktree_count
from tests.test_skills import review

pytestmark = pytest.mark.skipif(
    sys.platform != 'linux' or not os.environ.get('MINDCODE_PODMAN_TEST_IMAGE'),
    reason='requires independent Ubuntu VM and explicitly trusted Podman image',
)


@pytest.mark.parametrize('failure', [False, True])
async def test_skill_worker_uses_container_and_independent_gate(tmp_path, failure):
    _init_repo(tmp_path)
    original = _git_out(tmp_path, 'rev-parse', 'HEAD')
    skill = review(id='tests', tools=('write_file', 'read_file'), instructions='Add a test.')
    cfg = replace(_config(tmp_path), skills=SkillConfig((skill,)), execution_backend='podman',
                  sandbox_image=os.environ['MINDCODE_PODMAN_TEST_IMAGE'],
                  verify_command='python -c "raise SystemExit(' + ('1' if failure else '0') + ')"')
    cfg = replace(cfg, profile=replace(cfg.profile, master_max_replans=0))
    client = StubLlmClient([
        [('write_file', {'path': 'test_added.py', 'content': '# container test'})],
        [('read_file', {'path': 'test_added.py'}), ('run_command', {'command': 'echo forbidden'})],
        'done',
    ])
    graph = TaskGraph([Step('s', 'skill.tests', 'add test')])
    async with MasterSession(cfg, llm_client=client, planner=StaticPlanner(graph)) as session:
        final = await session.run_task('add test')
        assert final.integrated is not failure
        assert (tmp_path / 'test_added.py').exists() is not failure
        assert (_git_out(tmp_path, 'rev-parse', 'HEAD') != original) is not failure
        results = [b for m in client.seen_calls[-1] for b in m.blocks
                   if isinstance(b, ToolResultBlock)]
        assert '# container test' in results[-2].content and not results[-2].is_error
        assert results[-1].is_error and '未获准' in results[-1].content
        assert final.scheduler is not None
        assert final.scheduler.workers['s'].run.definition.id == 'skill.tests'
        assert session.master is not None and session.master._sandbox is not None
        assert not session.master._sandbox._handles
        assert _worktree_count(tmp_path) == 1
