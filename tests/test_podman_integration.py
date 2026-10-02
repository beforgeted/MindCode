from __future__ import annotations

import os
import shlex
import sys
from pathlib import Path

import pytest

from codeagent.execution.models import ExecutionLimits, SandboxError
from codeagent.execution.podman import PodmanSandboxManager
from codeagent.execution.snapshot import SnapshotEntry, TreeSnapshot, apply_snapshot

# 真容器测试显式启用。普通 pytest 不安装/拉取镜像、不改变主机配置。
pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or os.environ.get("MINDCODE_PODMAN_TEST_IMAGE") is None,
    reason="需要 Linux + 显式设置受信镜像 SHA256 的 MINDCODE_PODMAN_TEST_IMAGE",
)


def _proc_info(pid):
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    except FileNotFoundError:
        return None


@pytest.fixture
def image():
    return os.environ["MINDCODE_PODMAN_TEST_IMAGE"]


async def test_rootless_roundtrip_blocks_base_escape_and_seals_snapshot(image, tmp_path):
    base = tmp_path / "base"
    base.mkdir()
    (base / "sentinel.txt").write_text("untouched")
    manager = PodmanSandboxManager(image, limits=ExecutionLimits(
        memory_mib=128, workspace_mib=16, temporary_mib=8, cpus=.5, pids=16,
    ))
    handle = await manager.open(TreeSnapshot((SnapshotEntry("seed.txt", b"seed"),)))
    try:
        result = await manager.execute(handle, "printf hello > ../../../note.txt")
        assert result.returncode != 0
        result = await manager.execute(handle, f"test ! -e {str(base / 'sentinel.txt')!r}")
        assert result.returncode == 0
        result = await manager.execute(handle, "printf done > result.txt")
        assert result.returncode == 0
        snapshot = await manager.seal(handle)
        assert {entry.path: entry.data for entry in snapshot.entries} == {
            "seed.txt": b"seed", "result.txt": b"done",
        }
        staging = tmp_path / "private-staging"
        staging.mkdir(mode=0o700)
        apply_snapshot(snapshot, staging)
        assert (staging / "result.txt").read_bytes() == b"done"
        assert (base / "sentinel.txt").read_text() == "untouched"
        assert sorted(p.name for p in base.iterdir()) == ["sentinel.txt"]
    finally:
        await manager.aclose()


async def test_unsafe_symlink_snapshot_is_rejected_and_domain_removed(image):
    manager = PodmanSandboxManager(image)
    handle = await manager.open(TreeSnapshot(()))
    try:
        assert (await manager.execute(handle, "ln -s /etc/passwd escape")).returncode == 0
        with pytest.raises(SandboxError):
            await manager.seal(handle)
        assert handle.container_id not in manager._handles
    finally:
        await manager.aclose()


async def test_worker_file_tools_and_command_share_real_container(image, tmp_path):
    import asyncio
    import json

    from codeagent.tool import sandbox_file_helper

    manager = PodmanSandboxManager(image)
    source = await asyncio.to_thread(
        Path(sandbox_file_helper.__file__).read_text, encoding="utf-8",
    )
    handle = await manager.open(TreeSnapshot(()))
    try:
        payload = json.dumps({"tool": "write_file", "arguments": {
            "path": "note.txt", "content": "from tool",
        }}).encode()
        result = await manager.execute_python(handle, source, payload)
        assert result.returncode == 0
        result = await manager.execute(handle, "cat note.txt; printf ' appended' >> note.txt")
        assert result.returncode == 0 and result.stdout == b"from tool"
        for tool, arguments in [("read_file", {"path": "note.txt"}),
                                ("grep", {"pattern": "appended"})]:
            result = await manager.execute_python(handle, source, json.dumps({
                "tool": tool, "arguments": arguments,
            }).encode())
            assert result.returncode == 0 and b"from tool appended" in result.stdout
        result = await manager.execute_python(handle, source, json.dumps({
            "tool": "write_file", "arguments": {"path": "../escape", "content": "bad"},
        }).encode())
        assert result.returncode != 0
        assert not (tmp_path / "note.txt").exists()
        snapshot = await manager.seal(handle)
        assert snapshot == TreeSnapshot((SnapshotEntry("note.txt", b"from tool appended"),))
    finally:
        await manager.aclose()


async def test_cancellation_removes_setsid_descendant(image):
    import asyncio

    from codeagent.infra.cancellation import CancellationToken, CancelledByUser

    manager = PodmanSandboxManager(image)
    handle = await manager.open(TreeSnapshot(()))
    token = CancellationToken()
    script = (
        "import os, subprocess, sys, time; "
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)'], "
        "start_new_session=True); time.sleep(300)"
    )
    task = asyncio.create_task(manager.execute(
        handle, "python -I -c " + shlex.quote(script), cancellation=token,
    ))
    try:
        # Wait until the detached child is visible, not merely until a timer fires.
        for _ in range(40):
            result = await manager._checked("top", handle.container_id, "hpid")
            pids = [int(s) for s in result.stdout.decode().splitlines()[1:] if s.strip().isdigit()]
            if len(pids) >= 4:
                break
            await asyncio.sleep(.1)
        else:
            pytest.fail("未观察到 setsid 后代")
        states = {}
        for pid in pids:
            info = await asyncio.to_thread(_proc_info, pid)
            assert info is not None
            states[pid] = info[19]
        token.cancel()
        with pytest.raises(CancelledByUser):
            await asyncio.wait_for(task, 15)
        assert handle.container_id not in manager._handles
        for pid, start in states.items():
            state = await asyncio.to_thread(_proc_info, pid)
            if state is not None:
                assert state[19] != start or state[0] == "Z"
    finally:
        token.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await manager.aclose()


@pytest.mark.parametrize("accept", [True, False], ids=["promote", "reject_escape"])
async def test_real_master_worker_validation_and_publication(image, tmp_path, accept):
    from dataclasses import replace

    from codeagent.llm.stub_client import StubLlmClient
    from codeagent.orchestration.master_session import MasterSession
    from codeagent.orchestration.planner import StaticPlanner
    from codeagent.orchestration.task_graph import Step, TaskGraph
    from tests.test_master_integration import _config, _git_out, _init_repo, _worktree_count

    _init_repo(tmp_path)
    original_head = _git_out(tmp_path, "rev-parse", "HEAD")
    seed = (tmp_path / "seed.txt").read_bytes()
    config = replace(_config(tmp_path), execution_backend="podman", sandbox_image=image,
                     verify_command="test $(cat note.txt) = sandbox" if accept else "false")
    config = replace(config, profile=replace(config.profile, master_max_replans=0))
    client = StubLlmClient([
        [("write_file", {"path": "note.txt", "content": "sandbox"})],
        [("run_command", {"command": "cat note.txt" if accept else
                          "printf escaped > ../../../note.txt"})],
        "done",
    ])
    async with MasterSession(
        config, llm_client=client,
        planner=StaticPlanner(TaskGraph([Step("s", "default", "write note.txt")])),
    ) as session:
        result = await session.run_task("write note.txt")
        assert result.integrated is accept
        assert result.scheduler is not None
        worker = result.scheduler.workers["s"]
        assert worker.result.ok and worker.verification.ok
        if accept:
            assert (tmp_path / "note.txt").read_text() == "sandbox"
        else:
            commands = [run for run in worker.run.context.tool_runs
                        if run.call.name == "run_command"]
            assert len(commands) == 1 and commands[0].result is not None
            assert commands[0].result.exit_code not in (None, 0)
            assert _git_out(tmp_path, "rev-parse", "HEAD") == original_head
            assert not (tmp_path / "note.txt").exists()
            assert (tmp_path / "seed.txt").read_bytes() == seed
        assert _worktree_count(tmp_path) == 1
        assert session.master is not None and session.master._sandbox is not None
        assert not session.master._sandbox._handles


@pytest.mark.parametrize("window", ["unbound_create", "paused"])
async def test_killed_controller_reclaims_only_its_recorded_container(image, tmp_path, window):
    import asyncio
    import sqlite3

    directory = tmp_path / "ledger"
    live = PodmanSandboxManager(image, ledger_directory=directory, project_id="recovery-test")
    survivor = await live.open(TreeSnapshot(()))
    child_source = '''
import asyncio, sys
from pathlib import Path
from codeagent.execution.podman import PodmanSandboxManager
from codeagent.execution.snapshot import TreeSnapshot
class Controller(PodmanSandboxManager):
    async def _checked(self, *args, **kwargs):
        result = await super()._checked(*args, **kwargs)
        if args[0] == "create" and sys.argv[3] == "unbound_create":
            print("READY", flush=True)
            await asyncio.sleep(300)
        return result
async def main():
    manager = Controller(sys.argv[2], ledger_directory=Path(sys.argv[1]),
                         project_id="recovery-test")
    handle = await manager.open(TreeSnapshot(()))
    await manager._checked("pause", handle.container_id)
    print("READY", flush=True)
    await asyncio.sleep(300)
asyncio.run(main())
'''
    child = await asyncio.create_subprocess_exec(
        sys.executable, "-I", "-c", child_source, str(directory), image, window,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    recovery = PodmanSandboxManager(image, ledger_directory=directory, project_id="recovery-test")
    try:
        assert child.stdout is not None
        assert await asyncio.wait_for(child.stdout.readline(), 45) == b"READY\n"
        child.kill()  # No Python finally / ledger.close / container cleanup.
        await asyncio.wait_for(child.wait(), 10)
        with sqlite3.connect(directory / "resources.db") as db:
            rows = db.execute(
                "SELECT name, container_id FROM resources WHERE owner != ?", (live.owner,),
            ).fetchall()
        assert len(rows) == 1
        name, cid = rows[0]
        if window == "unbound_create":
            assert cid is None
        assert (await recovery._control("container", "exists", cid or name)).returncode == 0
        await recovery.ensure_available()
        assert (await recovery._control("container", "exists", cid or name)).returncode == 1
        assert (await live._control("container", "exists", survivor.container_id)).returncode == 0
        assert recovery._ledger is not None
        assert set(recovery._ledger.owners()) == {live.owner, recovery.owner}
    finally:
        if child.returncode is None:
            child.kill()
        await child.wait()
        await recovery.aclose()
        await live.aclose()
