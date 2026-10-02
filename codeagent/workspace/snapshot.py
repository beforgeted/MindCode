"""Private non-Git task candidates; shared files are touched only at acceptance.

Revisions are content hashes, and references are private bookkeeping identifiers.
They are not Git objects. Recovery restores bounded original/frozen snapshots
and a RunStore publication receipt; matching filesystem bytes are never a receipt.
"""
from __future__ import annotations

import asyncio
import difflib
import hashlib
import json
import os
import shutil
from dataclasses import asdict
from pathlib import Path

from codeagent.agent.models import FileChangeKind, FileState
from codeagent.execution.models import SandboxError
from codeagent.execution.publication import WorkspacePublication
from codeagent.execution.shared_workspace import accepted_output, capture_shared
from codeagent.execution.snapshot import (
    SnapshotLimits,
    TreeSnapshot,
    apply_snapshot,
    decode_snapshot,
    encode_snapshot,
)
from codeagent.execution.task_staging import TaskStaging
from codeagent.execution.workspace import capture_workspace, publish_workspace
from codeagent.infra.ids import new_id
from codeagent.orchestration.snapshot_store import SnapshotCheckpoint
from codeagent.workspace.context import WorkspaceContext
from codeagent.workspace.manager import _is_git_worktree
from codeagent.workspace.verification_evidence import (
    EvidenceUnit,
    VerificationEvidence,
    binary_unit,
    collect_evidence,
)


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
        self.staging_directory = state_root / 'task-staging' / key
        self._publication: WorkspacePublication | None = None
        self._staging: TaskStaging | None = None
        self.checkpoint: SnapshotCheckpoint | None = None
        self.run_id: str | None = None
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

    def binding(self) -> dict:
        if self._publication is None:
            raise SandboxError('snapshot publication lease unavailable')
        info = os.fstat(self._publication.root_fd)
        return {'root': str(self.root), 'identity': [info.st_dev, info.st_ino],
                'limits': asdict(self.limits), 'excluded': sorted(self.excluded)}

    def begin(
        self, run_id: str | None = None, checkpoint: SnapshotCheckpoint | None = None,
    ) -> None:
        if self._publication is not None:
            raise SandboxError("同一非Git任务管理器已有执行中的任务")
        if _is_git_worktree(self.root):
            raise SandboxError("项目已成为Git工作区，需要重新装配任务")
        self._publication = WorkspacePublication(
            self.root, self.directory, limits=self.limits, task_run_id=run_id,
        )
        try:
            self.run_id, self.checkpoint = run_id, checkpoint
            if checkpoint is not None and checkpoint.binding != self.binding():
                raise SandboxError('恢复目录身份、排除策略或快照限额与记录不符')
            self.initial = (self.capture() if checkpoint is None else
                            decode_snapshot(checkpoint.initial, self.limits))
            self.published = False
            self.output = None
            self._remember(self.initial)
            if checkpoint is not None and checkpoint.candidate is not None:
                self._remember(decode_snapshot(checkpoint.candidate, self.limits))
            self._staging = TaskStaging(self.staging_directory, str(self.root))
        except BaseException:
            self.end()
            raise

    def end(self) -> None:
        try:
            if self._staging is not None:
                self._staging.close()
        finally:
            self._staging = None
            if self._publication is not None:
                self._publication.close()
            self._publication = None
            self._snapshots.clear()
            self._refs.clear()
            self._workspaces.clear()
            self.initial = self.output = None
            self.published = False
            self.checkpoint = None
            self.run_id = None

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
        if self._staging is None:
            raise SandboxError("非Git任务尚未开始")
        identity = new_id(kind)
        path = self._staging.root / identity
        path.mkdir(mode=0o700)
        apply_snapshot(snapshot, path, self.limits)
        revision = self._remember(snapshot)
        workspace = WorkspaceContext(
            root=path, worktree_id=identity, branch_name=identity,
            is_isolated=True, base_revision=revision,
            handoff_staging_root=self._staging.root,
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
        if self.checkpoint is not None and self.checkpoint.candidate_revision is not None:
            retained.add(self.checkpoint.candidate_revision)
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

    async def verification_evidence(
        self, a: str, b: str, *, max_bytes: int,
    ) -> VerificationEvidence:
        return await asyncio.to_thread(self._verification_evidence, a, b, max_bytes)

    def _verification_evidence(self, a: str, b: str, max_bytes: int) -> VerificationEvidence:
        before = {e.path: e for e in self._snapshots[a].entries}
        after = {e.path: e for e in self._snapshots[b].entries}
        files = tuple(sorted(name for name in before.keys() | after.keys()
                             if before.get(name) != after.get(name)))
        diffs: list[tuple[str, str]] = []
        binary: list[EvidenceUnit] = []
        try:
            if type(max_bytes) is not int or max_bytes <= 0:
                raise ValueError("evidence byte limit must be positive")
            used = 0
            for name in files:
                old_entry, new_entry = before.get(name), after.get(name)
                old = old_entry.data if old_entry else b""
                new = new_entry.data if new_entry else b""
                if len(old) + len(new) > max_bytes:
                    raise ValueError(f"diff input exceeds evidence limit: {name}")
                is_binary = b"\0" in old or b"\0" in new
                try:
                    old_text, new_text = old.decode("utf-8"), new.decode("utf-8")
                except UnicodeError:
                    is_binary = True
                    old_text = new_text = ""
                if is_binary:
                    binary.append(binary_unit(
                        name, old if old_entry else None, new if new_entry else None,
                        old_mode=str(bool(old_entry and old_entry.executable)),
                        new_mode=str(bool(new_entry and new_entry.executable)),
                    ))
                    continue
                text = (
                    f"snapshot file {json.dumps(name)}\n"
                    f"old exists={old_entry is not None} "
                    f"executable={bool(old_entry and old_entry.executable)}\n"
                    f"new exists={new_entry is not None} "
                    f"executable={bool(new_entry and new_entry.executable)}\n"
                )
                lines = difflib.unified_diff(
                    old_text.splitlines(True), new_text.splitlines(True),
                    fromfile=name, tofile=name,
                )
                text += "".join(
                    line if line.endswith("\n") else line + "\n\\ No newline at end of file\n"
                    for line in lines
                )
                used += len(text.encode("utf-8"))
                if used > max_bytes:
                    raise ValueError("complete evidence exceeds byte limit")
                diffs.append((name, text))
            return collect_evidence(a, b, files, diffs, max_bytes=max_bytes, binary=tuple(binary))
        except ValueError as exc:
            return VerificationEvidence(a, b, files, detail=f"evidence collection failed: {exc}")

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
            transaction=self.checkpoint.transaction(self.run_id) if self.checkpoint is not None
            and self.checkpoint.publication_state == 'prepared'
            and self.run_id is not None else None,
        )
        self.output = self._snapshots[candidate_sha]
        self.published = True
        return True

    def frozen_bytes(self, revision: str) -> bytes:
        return encode_snapshot(self._snapshots[revision], self.limits)

    def has_applied_receipt(self) -> bool:
        if self.checkpoint is None or self.checkpoint.candidate_revision is None:
            return False
        assert self._publication is not None and self.initial is not None
        assert self.run_id is not None
        return self._publication.applied_transaction(
            self.checkpoint.transaction(self.run_id), self.initial,
            self._snapshots[self.checkpoint.candidate_revision],
        )

    def acknowledge(self) -> None:
        assert self.checkpoint is not None and self.run_id is not None
        assert self._publication is not None and self.checkpoint.candidate_revision is not None
        self.output = self._snapshots[self.checkpoint.candidate_revision]
        self.published = True
        self._publication.acknowledge(self.checkpoint.transaction(self.run_id))

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
