"""Private non-Git task candidates; shared files are touched only at acceptance.

Revisions are content hashes, and references are private bookkeeping identifiers.
They are not Git objects. Task resume is deliberately rejected: filesystem content
alone cannot prove ownership of a previous publication after its journal retires.
"""
from __future__ import annotations

import difflib
import hashlib
import shutil
import tempfile
from pathlib import Path

from codeagent.agent.models import FileChangeKind, FileState
from codeagent.execution.models import SandboxError
from codeagent.execution.publication import WorkspacePublication
from codeagent.execution.shared_workspace import accepted_output, capture_shared
from codeagent.execution.snapshot import (
    SnapshotLimits,
    TreeSnapshot,
    apply_snapshot,
    encode_snapshot,
)
from codeagent.execution.workspace import capture_workspace, publish_workspace
from codeagent.infra.ids import new_id
from codeagent.workspace.context import WorkspaceContext
from codeagent.workspace.manager import _is_git_worktree


class SnapshotWorkspaceManager:
    def __init__(self, root: Path, state_root: Path, *, limits: SnapshotLimits):
        self.root = root.resolve()
        self.limits = limits
        excluded = {".venv", "venv", "__pycache__", ".pytest_cache", ".ruff_cache"}
        try:
            state = state_root.resolve().relative_to(self.root)
        except ValueError:
            pass
        else:
            if not state.parts:
                raise SandboxError("任务状态目录必须独立于项目根目录")
            excluded.add(state.as_posix())
        self.excluded = frozenset(excluded)
        key = hashlib.sha256(str(self.root).encode()).hexdigest()[:24]
        self.directory = state_root / "publication" / key
        self._publication: WorkspacePublication | None = None
        self._temporary: tempfile.TemporaryDirectory | None = None
        self._snapshots: dict[str, TreeSnapshot] = {}
        self._refs: dict[str, str] = {}
        self._workspaces: dict[str, WorkspaceContext] = {}
        self.initial: TreeSnapshot | None = None
        self.published = False
        self.output: TreeSnapshot | None = None

    @property
    def isolated(self) -> bool:
        return True

    def capture(self) -> TreeSnapshot:
        return capture_shared(self.root, self.limits, git=False, excluded=self.excluded)

    def begin(self) -> None:
        if self._publication is not None:
            raise SandboxError("同一非Git任务管理器已有执行中的任务")
        if _is_git_worktree(self.root):
            raise SandboxError("项目已成为Git工作区，需要重新装配任务")
        self._publication = WorkspacePublication(self.root, self.directory, limits=self.limits)
        try:
            self.initial = self.capture()
            self.published = False
            self.output = None
            self._remember(self.initial)
            self._temporary = tempfile.TemporaryDirectory(prefix="mindcode-task-")
        except BaseException:
            self.end()
            raise

    def end(self) -> None:
        try:
            if self._temporary is not None:
                self._temporary.cleanup()
        finally:
            self._temporary = None
            if self._publication is not None:
                self._publication.close()
            self._publication = None
            self._snapshots.clear()
            self._refs.clear()
            self._workspaces.clear()
            self.initial = self.output = None
            self.published = False

    def _remember(self, snapshot: TreeSnapshot) -> str:
        revision = "snapshot:" + hashlib.sha256(encode_snapshot(snapshot, self.limits)).hexdigest()
        self._snapshots[revision] = snapshot
        return revision

    def _snapshot(self, workspace: WorkspaceContext) -> TreeSnapshot:
        if self._workspaces.get(workspace.worktree_id) != workspace:
            raise SandboxError("任务工作目录归属不符")
        return accepted_output(
            self.root, capture_workspace(workspace, self.limits),
            git=False, excluded=self.excluded,
        )

    def _create(self, snapshot: TreeSnapshot, kind: str) -> WorkspaceContext:
        if self._temporary is None:
            raise SandboxError("非Git任务尚未开始")
        identity = new_id(kind)
        path = Path(self._temporary.name) / identity
        path.mkdir(mode=0o700)
        apply_snapshot(snapshot, path, self.limits)
        revision = self._remember(snapshot)
        workspace = WorkspaceContext(
            root=path, worktree_id=identity, branch_name=identity,
            is_isolated=True, base_revision=revision,
        )
        self._workspaces[identity] = workspace
        self._refs[identity] = revision
        return workspace

    async def base_revision(self) -> str:
        if self.initial is None:
            raise SandboxError("非Git任务尚未捕获输入")
        return self._remember(self.initial)

    async def create_candidate(self, base_rev: str) -> WorkspaceContext:
        return self._create(self._snapshots[base_rev], "candidate")

    async def create(self, run_id: str, *, base_ref: str | None = None) -> WorkspaceContext:
        if base_ref is None or base_ref not in self._refs:
            raise SandboxError("非GitWorker必须从当前任务候选创建")
        return self._create(self._snapshots[self._refs[base_ref]], "worker")

    async def cleanup(self, workspace: WorkspaceContext, *, keep: bool = False) -> None:
        if keep or self._workspaces.get(workspace.worktree_id) != workspace:
            return
        # Delete only the exact private directory created and recorded by this manager.
        shutil.rmtree(workspace.root)
        self._refs.pop(workspace.worktree_id, None)
        self._workspaces.pop(workspace.worktree_id, None)
        retained = set(self._refs.values()) | {
            w.base_revision for w in self._workspaces.values() if w.base_revision is not None
        }
        if self.initial is not None:
            retained.add(self._remember(self.initial))
        self._snapshots = {key: value for key, value in self._snapshots.items() if key in retained}

    async def head(self, root: Path) -> str:
        workspace = next((w for w in self._workspaces.values() if w.root == root), None)
        if workspace is None:
            raise SandboxError("未知任务候选")
        revision = self._remember(self._snapshot(workspace))
        self._refs[workspace.worktree_id] = revision
        return revision

    async def changed_files(self, from_rev: str, to_rev: str) -> set[str]:
        before = {e.path: e for e in self._snapshots[from_rev].entries}
        after = {e.path: e for e in self._snapshots[to_rev].entries}
        return {name for name in before.keys() | after.keys()
                if before.get(name) != after.get(name)}

    async def diff_text(self, a: str, b: str, *, max_bytes: int = 20_000) -> str:
        before = {e.path: e.data for e in self._snapshots[a].entries}
        after = {e.path: e.data for e in self._snapshots[b].entries}
        text = ""
        for name in sorted(await self.changed_files(a, b)):
            # Bound both the diff input and its resulting model-facing summary.
            old = before.get(name, b"")[:4000].decode("utf-8", errors="replace").splitlines(True)
            new = after.get(name, b"")[:4000].decode("utf-8", errors="replace").splitlines(True)
            text += "".join(difflib.unified_diff(old, new, fromfile=name, tofile=name))
            if len(text) >= max_bytes:
                break
        return text[:max_bytes]

    async def integrate(
        self, worker: WorkspaceContext, candidate: WorkspaceContext, *,
        read_set: set[str], reads_unknown: bool,
    ) -> tuple[str, tuple[str, ...]]:
        if worker.base_revision is None:
            raise SandboxError("Worker缺少快照基线")
        output = self._snapshot(worker)
        output_revision = self._remember(output)
        current_revision = await self.head(candidate.root)
        intervening = await self.changed_files(worker.base_revision, current_revision)
        changed = await self.changed_files(worker.base_revision, output_revision)
        overlap = intervening & (changed | read_set)
        if intervening and (overlap or reads_unknown):
            return "stale", tuple(sorted(overlap or intervening))
        current = self._snapshots[current_revision]
        merged = {e.path: e for e in current.entries}
        new = {e.path: e for e in output.entries}
        for name in changed:
            if name in new:
                merged[name] = new[name]
            else:
                merged.pop(name, None)
        snapshot = TreeSnapshot(tuple(merged[name] for name in sorted(merged)))
        publish_workspace(candidate, current, snapshot, self.limits)
        self._refs[candidate.worktree_id] = self._remember(snapshot)
        await self.cleanup(worker)
        return "integrated", ()

    async def create_validation(self, candidate_sha: str) -> WorkspaceContext:
        return self._create(self._snapshots[candidate_sha], "validation")

    async def promote(self, candidate_sha: str, *, expected_base: str) -> bool:
        if self._publication is None or self.initial is None:
            raise SandboxError("任务发布租约不可用")
        if _is_git_worktree(self.root) or expected_base != await self.base_revision():
            return False
        if self.capture() != self.initial:
            return False
        self._publication.publish(
            self.initial, self._snapshots[candidate_sha], capture=self.capture, expected_guard="",
        )
        self.output = self._snapshots[candidate_sha]
        self.published = True
        return True

    def published_files(self) -> tuple[FileState, ...]:
        if not self.published or self.initial is None or self.output is None:
            return ()
        before = {e.path: e for e in self.initial.entries}
        after = {e.path: e for e in self.output.entries}
        return tuple(
            FileState(name, FileChangeKind.CREATED if name not in before else
                      FileChangeKind.DELETED if name not in after else FileChangeKind.MODIFIED)
            for name in sorted(before.keys() | after.keys()) if before.get(name) != after.get(name)
        )
