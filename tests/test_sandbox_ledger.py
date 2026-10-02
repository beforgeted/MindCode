from __future__ import annotations

import asyncio
import json
import os
import sys
from uuid import uuid4

import pytest

from codeagent.execution.ledger import SandboxResourceLedger
from codeagent.execution.models import ProcessOutput, SandboxError
from codeagent.execution.podman import _ATTEST, PodmanSandboxManager
from codeagent.execution.process import run_bounded
from codeagent.execution.snapshot import TreeSnapshot

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux controller leases")
IMAGE = "a" * 64


class Backend:
    def __init__(self):
        self.containers = {}
        self.calls = []
        self.fail_create_output = False
        self.fail_rm = False
        self.exists_error = False

    async def call(self, manager, *args, **kwargs):
        self.calls.append(args)
        if args[0] == "info":
            return ProcessOutput(0, json.dumps({"host": {"security": {"rootless": True},
                "cgroupVersion": "v2", "cgroupControllers": ["cpu", "memory", "pids"]}}
            ).encode(), b"")
        if args[:2] == ("image", "inspect"):
            return ProcessOutput(0, json.dumps([{"Id": IMAGE}]).encode(), b"")
        if args[0] == "create":
            assert manager._ledger is not None
            name = args[args.index("--name") + 1]
            assert any(r.name == name for r in manager._ledger.records(manager.owner))
            labels = dict(args[i + 1].split("=", 1)
                          for i, arg in enumerate(args) if arg == "--label")
            cid = uuid4().hex * 2
            self.containers[cid] = {"Id": cid, "Name": name, "Image": IMAGE,
                "Config": {"Labels": labels}, "State": {"Pid": 123, "Paused": False}}
            if self.fail_create_output:
                raise TimeoutError("create completed but its output was lost")
            return ProcessOutput(0, cid.encode(), b"")
        target = args[-1]
        record = next((r for r in self.containers.values()
                       if target in (r["Id"], r["Name"])), None)
        if args[:2] == ("container", "exists"):
            return ProcessOutput(125 if self.exists_error else (0 if record else 1), b"", b"")
        if args[0] == "inspect":
            return ProcessOutput(0 if record else 125,
                                 json.dumps([record]).encode() if record else b"", b"")
        if args[0] == "rm":
            if self.fail_rm:
                return ProcessOutput(125, b"", b"removal failed")
            self.containers.pop(target)
        if args[0] == "exec" and args[-1] == _ATTEST:
            return ProcessOutput(0, b"{}", b"")
        return ProcessOutput(0, b"", b"")


class ManagedFake(PodmanSandboxManager):
    def __init__(self, directory, backend, project="project"):
        super().__init__(IMAGE, ledger_directory=directory, project_id=project)
        self.backend = backend

    async def _control(self, *args, **kwargs):
        return await self.backend.call(self, *args, **kwargs)

    async def _attest(self, container_id):
        pass


@pytest.fixture
def backend(monkeypatch):
    monkeypatch.setattr("codeagent.execution.podman._process_start", lambda _: "42")
    return Backend()


def abandon(manager):
    assert manager._ledger is not None
    manager._ledger.close()
    manager._ledger = None


async def test_live_manager_is_not_reclaimed_even_in_same_process(tmp_path, backend):
    first = ManagedFake(tmp_path / "ledger", backend)
    handle = await first.open(TreeSnapshot(()))
    second = ManagedFake(tmp_path / "ledger", backend)
    try:
        await second.ensure_available()
        assert handle.container_id in backend.containers
        assert not any(call[0] == "rm" for call in backend.calls)
    finally:
        await second.aclose()
        await first.aclose()


async def test_dead_owner_recovered_without_scanning_other_containers(tmp_path, backend):
    first = ManagedFake(tmp_path / "ledger", backend)
    handle = await first.open(TreeSnapshot(()))
    foreign = {"Id": "f" * 64, "Name": "mindcode-unregistered"}
    backend.containers[foreign["Id"]] = foreign
    abandon(first)
    second = ManagedFake(tmp_path / "ledger", backend)
    await second.ensure_available()
    assert handle.container_id not in backend.containers
    assert backend.containers == {foreign["Id"]: foreign}
    assert not any(call[0] in ("ps", "list") for call in backend.calls)
    await second.aclose()


async def test_creation_output_loss_is_recovered_by_reserved_exact_name(tmp_path, backend):
    backend.fail_create_output = True
    manager = ManagedFake(tmp_path / "ledger", backend)
    with pytest.raises(TimeoutError):
        await manager.open(TreeSnapshot(()))
    assert not backend.containers
    assert manager._ledger is not None and not manager._ledger.records(manager.owner)
    await manager.aclose()


@pytest.mark.parametrize("field", ["owner", "project", "ledger", "purpose", "name", "image"])
async def test_mismatched_orphan_identity_is_retained(tmp_path, backend, field):
    first = ManagedFake(tmp_path / "ledger", backend)
    handle = await first.open(TreeSnapshot(()))
    actual = backend.containers[handle.container_id]
    if field in ("name", "image"):
        actual[field.capitalize()] = "other"
    else:
        actual["Config"]["Labels"]["io.mindcode." + field] = "other"
    abandon(first)
    second = ManagedFake(tmp_path / "ledger", backend)
    with pytest.raises(SandboxError, match="identity"):
        await second.ensure_available()
    assert handle.container_id in backend.containers
    assert not any(call[0] == "rm" for call in backend.calls)


async def test_failed_removal_keeps_intent_and_next_manager_retries(tmp_path, backend):
    first = ManagedFake(tmp_path / "ledger", backend)
    handle = await first.open(TreeSnapshot(()))
    backend.fail_rm = True
    with pytest.raises(SandboxError):
        await first.aclose()
    assert handle.container_id in backend.containers
    backend.fail_rm = False
    second = ManagedFake(tmp_path / "ledger", backend)
    await second.ensure_available()
    assert not backend.containers
    await second.aclose()


async def test_unknown_existence_never_forgets_intent(tmp_path, backend):
    first = ManagedFake(tmp_path / "ledger", backend)
    handle = await first.open(TreeSnapshot(()))
    abandon(first)
    backend.exists_error = True
    second = ManagedFake(tmp_path / "ledger", backend)
    with pytest.raises(SandboxError, match="existence"):
        await second.ensure_available()
    assert handle.container_id in backend.containers
    backend.exists_error = False
    third = ManagedFake(tmp_path / "ledger", backend)
    await third.ensure_available()
    assert not backend.containers
    await third.aclose()


async def test_reservation_failure_prevents_create(tmp_path, backend, monkeypatch):
    manager = ManagedFake(tmp_path / "ledger", backend)
    await manager.ensure_available()
    assert manager._ledger is not None

    def fail(*args):
        raise OSError("ledger disk failure")

    monkeypatch.setattr(manager._ledger, "reserve", fail)
    with pytest.raises(OSError):
        await manager.open(TreeSnapshot(()))
    assert not any(call[0] == "create" for call in backend.calls)
    await manager.aclose()


async def test_failed_attestation_clears_handle_after_confirmed_removal(
    tmp_path, backend, monkeypatch,
):
    manager = ManagedFake(tmp_path / "ledger", backend)

    async def fail(container_id):
        raise SandboxError("attestation failed")

    monkeypatch.setattr(manager, "_attest", fail)
    with pytest.raises(SandboxError, match="attestation"):
        await manager.open(TreeSnapshot(()))
    assert not backend.containers
    assert not manager._handles and not manager._states and not manager._locks
    assert manager._ledger is not None and not manager._ledger.records(manager.owner)
    await manager.aclose()


async def test_surviving_control_client_keeps_owner_lease(tmp_path):
    owner = uuid4().hex
    first = SandboxResourceLedger(tmp_path / "ledger", "project", owner)
    marker = tmp_path / "started"
    client = asyncio.create_task(run_bounded([
        sys.executable, "-I", "-c",
        "import pathlib,sys,time; pathlib.Path(sys.argv[1]).write_text('ready'); time.sleep(.7)",
        str(marker),
    ], pass_fds=(first.fd,)))
    try:
        for _ in range(100):
            if marker.exists():
                break
            await asyncio.sleep(.01)
        else:
            pytest.fail("control client did not start")
        first.close()
        second = SandboxResourceLedger(tmp_path / "ledger", "project", uuid4().hex)
        try:
            assert second.lock_orphan(owner) is None
            await client
            fd = second.lock_orphan(owner)
            assert fd is not None
            os.close(fd)
        finally:
            second.close()
    finally:
        await client
        first.close()


@pytest.mark.parametrize("unsafe", ["symlink_directory", "symlink_database", "shared_directory"])
def test_ledger_refuses_unsafe_control_state(tmp_path, unsafe):
    directory = tmp_path / "ledger"
    if unsafe == "symlink_directory":
        target = tmp_path / "target"
        target.mkdir(mode=0o700)
        directory.symlink_to(target, target_is_directory=True)
    elif unsafe == "symlink_database":
        directory.mkdir(mode=0o700)
        target = tmp_path / "other.db"
        target.write_bytes(b"untouched")
        (directory / "resources.db").symlink_to(target)
    else:
        directory.mkdir(mode=0o755)
        directory.chmod(0o755)  # umask=077 must not turn this negative case private.
    with pytest.raises((SandboxError, OSError)):
        SandboxResourceLedger(directory, "project", uuid4().hex)
    if unsafe == "symlink_database":
        assert (tmp_path / "other.db").read_bytes() == b"untouched"


def test_shared_directory_cannot_change_project_namespace(tmp_path):
    first = SandboxResourceLedger(tmp_path / "ledger", "project-a", uuid4().hex)
    try:
        with pytest.raises(SandboxError, match="another project"):
            SandboxResourceLedger(tmp_path / "ledger", "project-b", uuid4().hex)
    finally:
        first.close()
