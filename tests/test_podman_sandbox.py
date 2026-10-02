from __future__ import annotations

import json
from dataclasses import replace

import pytest

from codeagent.execution.models import ExecutionLimits, ProcessOutput, SandboxError
from codeagent.execution.podman import _ATTEST, PodmanSandboxManager, _workdir
from codeagent.execution.snapshot import SnapshotEntry, TreeSnapshot, encode_snapshot

_IMAGE = "a" * 64
_CID = "b" * 64


class FakePodman(PodmanSandboxManager):
    def __init__(self):
        super().__init__(_IMAGE, limits=ExecutionLimits(output_bytes=1024))
        self._ready = True
        self.calls = []
        self.paused = False
        self.exists = True
        self.exec_failure = None
        self.output = TreeSnapshot((SnapshotEntry("result.txt", b"done"),))

    async def _control(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if args[0] == "create":
            return ProcessOutput(0, _CID.encode(), b"")
        if args[0] == "inspect":
            payload = [{"Id": _CID, "Config": {"Labels": {"io.mindcode.owner": self.owner}},
                        "State": {"Pid": 123, "Paused": self.paused}}]
            return ProcessOutput(0, json.dumps(payload).encode(), b"")
        if args[0] == "pause":
            self.paused = True
        if args[0] == "rm":
            self.exists = False
        if args[:2] == ("container", "exists"):
            return ProcessOutput(0 if self.exists else 1, b"", b"")
        if args[0] == "unshare":
            return ProcessOutput(0, encode_snapshot(self.output), b"")
        if args[0] == "exec" and args[-1] == _ATTEST:
            status = {"uid": 1000, "cap_eff": 0, "cap_bnd": 0, "no_new_privs": "1",
                      "seccomp": "2", "root_readonly": True, "interfaces": ["lo"],
                      "workspace_bytes": self.limits.workspace_mib * 1024 * 1024,
                      "temporary_bytes": self.limits.temporary_mib * 1024 * 1024,
                      "limits": {"memory.max": str(self.limits.memory_mib * 1024 * 1024),
                                 "memory.swap.max": "0", "pids.max": str(self.limits.pids),
                                 "cpu.max": "100000 100000"}}
            return ProcessOutput(0, json.dumps(status).encode(), b"")
        if args[0] == "exec" and args[1] != "-i":
            if self.exec_failure:
                raise self.exec_failure
            return ProcessOutput(7, b"command output", b"")
        return ProcessOutput(0, b"", b"")


@pytest.fixture
def manager(monkeypatch):
    monkeypatch.setattr("codeagent.execution.podman._process_start", lambda _: "42")
    return FakePodman()


def test_mutable_image_tags_rejected():
    with pytest.raises(ValueError):
        PodmanSandboxManager("python:latest")


@pytest.mark.parametrize("path", ["../base", "/home/user", "a/../b", "a\\b", "a//b", ""])
def test_workdir_is_canonical(path):
    with pytest.raises(ValueError):
        _workdir(path)


async def test_create_uses_no_binds_and_resource_limits(manager):
    handle = await manager.open(TreeSnapshot(()))
    args = manager.calls[0][0]
    assert "--volume" not in args and "--mount" not in args
    assert args[args.index("--network") + 1] == "none"
    assert "--read-only" in args and "--http-proxy=false" in args
    assert args[args.index("--cap-drop") + 1] == "all"
    assert args[args.index("--user") + 1] == "1000:1000"
    assert args[args.index("--pull") + 1] == "never"
    assert _IMAGE in args and "--memory" in args and "--pids-limit" in args
    assert handle.host_pid == 123 and handle.process_started == "42"
    await manager.close(handle)


async def test_user_command_is_container_shell_argument_not_host_command(manager):
    handle = await manager.open(TreeSnapshot(()))
    text = "printf hello > ../../../note.txt; echo '$HOME'"
    result = await manager.execute(handle, text)
    assert result.returncode == 7
    args, options = manager.calls[-1]
    assert args == ("exec", "--workdir", "/workspace", _CID, "/bin/sh", "-c", text)
    assert options["max_bytes"] == 1024
    await manager.close(handle)


async def test_forged_equal_handle_rejected(manager):
    handle = await manager.open(TreeSnapshot(()))
    with pytest.raises(SandboxError, match="句柄"):
        await manager.execute(replace(handle), "echo x")
    await manager.close(handle)


async def test_seal_freezes_before_capture_and_destroys_before_return(manager):
    handle = await manager.open(TreeSnapshot(()))
    snapshot = await manager.seal(handle)
    assert snapshot == manager.output
    ops = [args[0] for args, _ in manager.calls]
    assert ops.index("pause") < ops.index("unshare") < ops.index("rm")
    assert not manager.exists
    with pytest.raises(SandboxError):
        await manager.execute(handle, "echo x")


async def test_pid_reuse_refuses_snapshot_but_cleans_owned_container(manager, monkeypatch):
    handle = await manager.open(TreeSnapshot(()))
    monkeypatch.setattr("codeagent.execution.podman._process_start", lambda _: "99")
    with pytest.raises(SandboxError, match="替换"):
        await manager.seal(handle)
    assert not any(args[0] == "unshare" for args, _ in manager.calls)
    assert not manager.exists


async def test_exec_timeout_closes_whole_domain(manager):
    handle = await manager.open(TreeSnapshot(()))
    manager.exec_failure = TimeoutError("test timeout")
    with pytest.raises(TimeoutError):
        await manager.execute(handle, "sleep 100")
    assert not manager.exists


async def test_cleanup_failure_is_not_reported_as_closed(manager):
    handle = await manager.open(TreeSnapshot(()))
    original = manager._control

    async def failing(*args, **kwargs):
        if args[0] == "rm":
            return ProcessOutput(125, b"", b"test failure")
        return await original(*args, **kwargs)

    manager._control = failing
    with pytest.raises(SandboxError):
        await manager.close(handle)
    assert handle.container_id in manager._handles
