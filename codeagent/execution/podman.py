"""rootless Podman 执行域；不挂载真实 Git worktree，不自动拉镜像或退回本机执行。"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from pathlib import Path, PurePosixPath
from uuid import uuid4

from codeagent.execution.ledger import ResourceRecord, SandboxResourceLedger
from codeagent.execution.models import (
    ExecutionLimits,
    ExecutionPurpose,
    ProcessOutput,
    SandboxError,
    SandboxHandle,
    SandboxUnavailable,
)
from codeagent.execution.process import run_bounded
from codeagent.execution.snapshot import (
    SnapshotLimits,
    TreeSnapshot,
    decode_snapshot,
    encode_snapshot,
)
from codeagent.infra.cancellation import CancellationToken

# 初始导入前无模型进程。此代码来自可信控制面，不 import 项目中的模块。
_IMPORT = """
import base64, json, os, pathlib, sys
payload = json.load(sys.stdin)
root = pathlib.Path('/workspace')
assert not any(root.iterdir()), 'workspace must start empty'
for entry in payload['entries']:
    path = root / entry['path']
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('xb') as handle:
        handle.write(base64.b64decode(entry['data'], validate=True))
    path.chmod(0o755 if entry['executable'] else 0o644)
"""


_ATTEST = """
import json, os, socket
from pathlib import Path
s = dict(line.split(':', 1) for line in Path('/proc/self/status').read_text().splitlines())
print(json.dumps({
    'uid': os.getuid(), 'cap_eff': int(s['CapEff'].strip(), 16),
    'cap_bnd': int(s['CapBnd'].strip(), 16), 'no_new_privs': s['NoNewPrivs'].strip(),
    'seccomp': s['Seccomp'].strip(), 'interfaces': [name for _, name in socket.if_nameindex()],
    'root_readonly': bool(os.statvfs('/').f_flag & os.ST_RDONLY),
    'workspace_bytes': os.statvfs('/workspace').f_blocks * os.statvfs('/workspace').f_frsize,
    'temporary_bytes': os.statvfs('/tmp').f_blocks * os.statvfs('/tmp').f_frsize,
    'limits': {k: Path('/sys/fs/cgroup/' + k).read_text().strip()
               for k in ('memory.max', 'memory.swap.max', 'pids.max', 'cpu.max')},
}))
"""


class PodmanSandboxManager:
    """每个 handle 是本实例发出的能力凭证，不接受模型传入的容器 ID。

    持久模式只回收本项目账本中失去控制器租约的精确资源，绝不扫描删除容器。
    """

    def __init__(
        self, image: str, *, limits: ExecutionLimits | None = None,
        snapshot_limits: SnapshotLimits | None = None,
        binary: str = "podman", host_python: str = sys.executable,
        ledger_directory: Path | None = None, project_id: str | None = None,
    ) -> None:
        if re.fullmatch(r"(?:sha256:)?[0-9a-f]{64}", image) is None:
            raise ValueError("必须使用已安装、受信的镜像 SHA256 ID，而非可变 tag")
        self.image = image.removeprefix("sha256:")
        self.limits = limits or ExecutionLimits()
        self.snapshot_limits = snapshot_limits or SnapshotLimits()
        self.binary, self.host_python = binary, host_python
        self.owner = uuid4().hex
        self._handles: dict[str, SandboxHandle] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._states: dict[str, str] = {}
        self._ready = False
        if ledger_directory is not None and not project_id:
            raise ValueError("durable sandbox ledger requires project_id")
        self._ledger_directory, self._project_id = ledger_directory, project_id
        self._ledger: SandboxResourceLedger | None = None
        self._closed = False
        self._setup_lock = asyncio.Lock()
        self._recovery_fds: tuple[int, ...] = ()

    async def _control(self, *args: str, **kwargs) -> ProcessOutput:
        # start/exec must not pass the lease into long-lived conmon processes.
        if self._ledger is not None and args[0] in ("create", "rm"):
            kwargs["pass_fds"] = (self._ledger.fd, *self._recovery_fds)
        return await run_bounded((self.binary, *args), **kwargs)

    async def _checked(self, *args: str, **kwargs) -> ProcessOutput:
        result = await self._control(*args, **kwargs)
        if result.returncode:
            # 原始 stderr 留给调试层；不要将 CLI 内部信息当授权或补救命令。
            raise SandboxError(f"Podman {args[0]} 失败 (exit {result.returncode})")
        return result

    async def ensure_available(self) -> None:
        async with self._setup_lock:
            await self._ensure_available()

    async def _ensure_available(self) -> None:
        if self._closed:
            raise SandboxUnavailable("sandbox manager is closed")
        if self._ready:
            return
        if sys.platform != "linux":
            raise SandboxUnavailable("rootless Podman 后端仅支持 Linux；禁止自动退回本机执行")
        try:
            info = json.loads((await self._checked("info", "--format", "json")).stdout)
            host = info["host"]
            if not host["security"]["rootless"] or host["cgroupVersion"] != "v2":
                raise SandboxUnavailable("需要 rootless Podman 和 cgroup v2")
            if not {"cpu", "memory", "pids"} <= set(host["cgroupControllers"]):
                raise SandboxUnavailable("cgroup 资源控制器不可用")
            image = json.loads((await self._checked("image", "inspect", self.image)).stdout)[0]
            if image["Id"].removeprefix("sha256:") != self.image:
                raise SandboxUnavailable("镜像身份与配置不一致")
        except (OSError, KeyError, ValueError, IndexError) as exc:
            raise SandboxUnavailable("沙箱后端或镜像不可用") from exc
        if self._ledger_directory is not None:
            assert self._project_id is not None
            ledger = SandboxResourceLedger(self._ledger_directory, self._project_id, self.owner)
            self._ledger = ledger
            try:
                await self.recover_orphans()
            except BaseException:
                ledger.close()
                self._ledger = None
                self._closed = True
                raise
        self._ready = True

    async def _inspect(self, container_id: str) -> dict:
        records = json.loads((await self._checked("inspect", container_id)).stdout)
        if not isinstance(records, list) or len(records) != 1:
            raise SandboxError("容器身份查询不唯一")
        record = records[0]
        if record["Id"] != container_id or record["Config"]["Labels"].get(
            "io.mindcode.owner"
        ) != self.owner:
            raise SandboxError("容器不属于本执行域")
        if self._ledger is not None:
            registered = next((r for r in self._ledger.records(self.owner)
                               if r.container_id == container_id), None)
            if registered is None:
                raise SandboxError("container has no creation intent")
            self._validate_resource(record, registered)
        return record

    def _validate_resource(self, actual: dict, resource: ResourceRecord) -> None:
        assert self._ledger is not None
        labels = actual["Config"]["Labels"]
        if (actual["Name"].removeprefix("/") != resource.name
                or actual["Image"].removeprefix("sha256:") != resource.image
                or labels.get("io.mindcode.owner") != resource.owner
                or labels.get("io.mindcode.purpose") != resource.purpose
                or labels.get("io.mindcode.ledger") != self._ledger.scope
                or labels.get("io.mindcode.project") != self._project_id
                or (resource.container_id is not None and actual["Id"] != resource.container_id)
                or re.fullmatch(r"[0-9a-f]{64}", actual["Id"]) is None):
            raise SandboxError("container identity differs from durable creation intent")

    async def _recover_resource(self, resource: ResourceRecord) -> None:
        assert self._ledger is not None
        target = resource.container_id or resource.name
        exists = await self._control("container", "exists", target)
        if exists.returncode == 1:
            self._forget_resource(resource)
            return
        if exists.returncode != 0:
            raise SandboxError("cannot determine recorded container existence")
        records = json.loads((await self._checked("inspect", target)).stdout)
        if not isinstance(records, list) or len(records) != 1:
            raise SandboxError("recorded resource identity is not unique")
        actual = records[0]
        self._validate_resource(actual, resource)
        cid = actual["Id"]
        await self._checked("rm", "--force", cid)
        if (await self._control("container", "exists", cid)).returncode != 1:
            raise SandboxError("orphan container removal was not confirmed")
        self._forget_resource(resource)

    def _forget_resource(self, resource: ResourceRecord) -> None:
        assert self._ledger is not None
        self._ledger.forget(resource.name, resource.owner)
        # Failed initialization may have issued a handle before attestation/import.
        for cid, handle in tuple(self._handles.items()):
            if handle.name == resource.name and handle.owner == resource.owner:
                self._handles.pop(cid)
                self._locks.pop(cid, None)
                self._states.pop(cid, None)

    async def recover_orphans(self) -> None:
        if self._ledger is None:
            return
        for owner in self._ledger.owners():
            fd = self._ledger.lock_orphan(owner)
            if fd is None:
                continue
            try:
                self._recovery_fds = (fd,)
                for resource in self._ledger.records(owner):
                    await self._recover_resource(resource)
                self._ledger.finish_owner(owner)
            finally:
                self._recovery_fds = ()
                os.close(fd)

    async def open(
        self, snapshot: TreeSnapshot, purpose: ExecutionPurpose = ExecutionPurpose.WORKER,
    ) -> SandboxHandle:
        purpose = ExecutionPurpose(purpose)
        payload = encode_snapshot(snapshot, self.snapshot_limits)
        await self.ensure_available()
        name = "mindcode-" + uuid4().hex
        extra_labels: tuple[str, ...] = ()
        if self._ledger is not None:
            self._ledger.reserve(name, purpose.value, self.image)
            extra_labels = ("--label", f"io.mindcode.ledger={self._ledger.scope}",
                            "--label", f"io.mindcode.project={self._project_id}")
        args = (
            "create", "--name", name, "--label", f"io.mindcode.owner={self.owner}",
            "--label", f"io.mindcode.purpose={purpose.value}",
            *extra_labels,
            "--pull", "never", "--network", "none", "--read-only", "--read-only-tmpfs=false",
            "--tmpfs", f"/workspace:rw,nosuid,nodev,size={self.limits.workspace_mib}m,mode=1777",
            "--tmpfs", f"/tmp:rw,nosuid,nodev,size={self.limits.temporary_mib}m,mode=1777",
            "--memory", f"{self.limits.memory_mib}m", "--memory-swap", f"{self.limits.memory_mib}m",
            "--cpus", str(self.limits.cpus), "--pids-limit", str(self.limits.pids),
            "--cap-drop", "all", "--security-opt", "no-new-privileges",
            "--user", "1000:1000", "--http-proxy=false", "--env", "HOME=/tmp",
            "--workdir", "/workspace", "--entrypoint", "python", self.image,
            "-I", "-c", f"import time; time.sleep({self.limits.lifetime_seconds})",
        )
        container_id = ""
        try:
            container_id = (await self._checked(*args)).stdout.decode().strip()
            if re.fullmatch(r"[0-9a-f]{64}", container_id) is None:
                raise SandboxError("Podman 未返回有效容器 ID")
            if self._ledger is not None:
                self._ledger.bind(name, container_id)
            await self._inspect(container_id)
            await self._checked("start", container_id)
            record = await self._inspect(container_id)
            host_pid = int(record["State"]["Pid"])
            started = await asyncio.to_thread(_process_start, host_pid)
            handle = SandboxHandle(container_id, name, self.owner, purpose, host_pid, started)
            self._handles[container_id] = handle
            self._locks[container_id] = asyncio.Lock()
            self._states[container_id] = "active"
            await self._attest(container_id)
            await self._checked("exec", "-i", container_id, "python", "-I", "-c", _IMPORT,
                                data=payload, timeout_seconds=self.limits.timeout_seconds)
            return handle
        except BaseException:
            # name 是在创建前分配的精确唯一值；即使 create 输出丢失也只查询这个名字。
            try:
                if self._ledger is not None:
                    resource = next(r for r in self._ledger.records(self.owner) if r.name == name)
                    await asyncio.shield(self._recover_resource(resource))
                else:
                    result = await self._control("inspect", container_id or name)
                    if result.returncode == 0:
                        record = json.loads(result.stdout)[0]
                        if record["Config"]["Labels"].get("io.mindcode.owner") == self.owner:
                            await asyncio.shield(self._remove(record["Id"]))
            except Exception:
                pass  # 不扩大清理范围，也不覆盖原始错误；资源标签用于后续受控回收。
            raise

    async def _attest(self, container_id: str) -> None:
        # 在任何项目文件进入之前检查实际内核状态，不能把命令行 flags 当作限额已生效的证据。
        output = await self._checked("exec", container_id, "python", "-I", "-c", _ATTEST)
        try:
            status = json.loads(output.stdout)
            limits = status["limits"]
            quota, period = map(int, limits["cpu.max"].split())
            enforced = (
                status["uid"] == 1000 and status["cap_eff"] == 0 and status["cap_bnd"] == 0
                and status["no_new_privs"] == "1" and status["seccomp"] == "2"
                and status["root_readonly"] is True and set(status["interfaces"]) <= {"lo"}
                and int(limits["memory.max"]) == self.limits.memory_mib * 1024 * 1024
                and limits["memory.swap.max"] == "0" and int(limits["pids.max"]) == self.limits.pids
                and period > 0 and abs(quota / period - self.limits.cpus) < .00001
                and status["workspace_bytes"] == self.limits.workspace_mib * 1024 * 1024
                and status["temporary_bytes"] == self.limits.temporary_mib * 1024 * 1024
            )
        except (KeyError, ValueError, TypeError) as exc:
            raise SandboxUnavailable("无法确认执行域内核限制") from exc
        if not enforced:
            raise SandboxUnavailable("执行域的权限、网络或资源限制未生效")

    def is_active(self, handle: SandboxHandle) -> bool:
        """Controller lifecycle state; sealed, removed or foreign handles cannot execute."""
        return (self._handles.get(handle.container_id) is handle and handle.owner == self.owner
                and self._states.get(handle.container_id) == "active")

    def _lock(self, handle: SandboxHandle) -> asyncio.Lock:
        if self._handles.get(handle.container_id) is not handle or handle.owner != self.owner:
            raise SandboxError("无效或已关闭的执行域句柄")
        return self._locks[handle.container_id]

    def _active(self, handle: SandboxHandle) -> None:
        if self._states.get(handle.container_id) != "active":
            raise SandboxError("执行域已封存或关闭")

    async def execute(
        self, handle: SandboxHandle, command: str, *, cwd: str = ".",
        cancellation: CancellationToken | None = None,
        max_output_bytes: int | None = None,
    ) -> ProcessOutput:
        if not command.strip():
            raise ValueError("命令不能为空")
        return await self._execute_argv(
            handle, ("/bin/sh", "-c", command), cwd=cwd,
            cancellation=cancellation, max_output_bytes=max_output_bytes,
        )

    async def execute_python(
        self, handle: SandboxHandle, source: str, data: bytes, *,
        cancellation: CancellationToken | None = None,
        max_output_bytes: int | None = None,
    ) -> ProcessOutput:
        """Run a controller-supplied helper; arguments travel as data on stdin."""
        return await self._execute_argv(
            handle, ("python", "-I", "-c", source), data=data,
            cancellation=cancellation, max_output_bytes=max_output_bytes,
        )

    async def _execute_argv(
        self, handle: SandboxHandle, argv: tuple[str, ...], *, cwd: str = ".",
        data: bytes | None = None, cancellation: CancellationToken | None = None,
        max_output_bytes: int | None = None,
    ) -> ProcessOutput:
        workdir = _workdir(cwd)
        output_limit = self.limits.output_bytes
        if max_output_bytes is not None:
            if max_output_bytes <= 0:
                raise ValueError("output limit must be positive")
            output_limit = min(output_limit, max_output_bytes)
        async with self._lock(handle):
            self._active(handle)
            try:
                await self._inspect(handle.container_id)
                return await self._control(
                    "exec", *(("-i",) if data is not None else ()),
                    "--workdir", workdir, handle.container_id, *argv,
                    timeout_seconds=self.limits.timeout_seconds, max_bytes=output_limit,
                    cancellation=cancellation, data=data,
                )
            except BaseException:
                self._states[handle.container_id] = "closing"
                await asyncio.shield(self._remove(handle.container_id))
                raise

    async def seal(self, handle: SandboxHandle) -> TreeSnapshot:
        async with self._lock(handle):
            self._active(handle)
            self._states[handle.container_id] = "sealing"
            try:
                await self._checked("pause", handle.container_id)
                await self._verify_frozen(handle)
                helper = Path(__file__).with_name("snapshot_helper.py")
                # S0 证明 podman cp 无法导出此 tmpfs；从可信控制面读取冻结的命名空间。
                result = await self._checked(
                    "unshare", self.host_python, "-I", str(helper),
                    str(handle.host_pid), handle.process_started,
                    timeout_seconds=self.limits.timeout_seconds,
                    max_bytes=self.snapshot_limits.max_total_bytes * 2
                    + self.snapshot_limits.max_files * 2048,
                )
                await self._verify_frozen(handle)
                return decode_snapshot(result.stdout, self.snapshot_limits)
            finally:
                self._states[handle.container_id] = "closing"
                await asyncio.shield(self._remove(handle.container_id))

    async def _verify_frozen(self, handle: SandboxHandle) -> None:
        record = await self._inspect(handle.container_id)
        if not record["State"]["Paused"] or int(record["State"]["Pid"]) != handle.host_pid:
            raise SandboxError("执行域未冻结或进程身份已变化")
        started = await asyncio.to_thread(_process_start, handle.host_pid)
        if started != handle.process_started:
            raise SandboxError("容器进程已被替换")

    async def _remove(self, container_id: str) -> None:
        record = await self._inspect(container_id)
        await self._checked("rm", "--force", container_id)
        result = await self._control("container", "exists", container_id)
        if result.returncode != 1:
            raise SandboxError("未确认执行域已销毁")
        if self._ledger is not None:
            self._ledger.forget(record["Name"].removeprefix("/"), self.owner)
        self._states.pop(container_id, None)
        self._handles.pop(container_id, None)
        self._locks.pop(container_id, None)

    async def close(self, handle: SandboxHandle) -> None:
        if handle.container_id not in self._handles:
            return
        async with self._lock(handle):
            self._states[handle.container_id] = "closing"
            await self._remove(handle.container_id)

    async def aclose(self) -> None:
        try:
            for handle in tuple(self._handles.values()):
                await self.close(handle)
            if self._ledger is not None:
                for resource in self._ledger.records(self.owner):
                    await self._recover_resource(resource)
                self._ledger.finish_owner(self.owner)
        finally:
            self._closed = True
            if self._ledger is not None:
                self._ledger.close()
                self._ledger = None


def _workdir(value: str) -> str:
    if value == ".":
        return "/workspace"
    path = PurePosixPath(value)
    if (not value or path.is_absolute() or "\\" in value or "\x00" in value
            or any(part in ("", ".", "..") for part in value.split("/"))):
        raise ValueError("cwd 必须是工作区内规范化的相对目录")
    return "/workspace/" + value


def _process_start(pid: int) -> str:
    if pid <= 0:
        raise SandboxError("无效的容器进程")
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return fields[19]
    except (OSError, IndexError) as exc:
        raise SandboxError("容器进程已退出") from exc
