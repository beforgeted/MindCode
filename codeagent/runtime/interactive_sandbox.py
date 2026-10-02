"""One sandboxed conversational turn with independently verified publication.

Clean Git roots use disposable worktrees and fast-forward publication. Shared
dirty Git and non-Git workspaces use journaled deltas without changing the index.
History belongs to the caller; containers and private staging belong to the turn.
"""
from __future__ import annotations

import asyncio
import hashlib
import tempfile
from dataclasses import replace
from pathlib import Path

from codeagent.agent.models import AgentRunResult, FileChangeKind, FileState, RunStatus
from codeagent.agent.run import AgentRun
from codeagent.config import AppConfig
from codeagent.execution.models import ExecutionPurpose, SandboxError, SandboxUnavailable
from codeagent.execution.podman import PodmanSandboxManager
from codeagent.execution.publication import PublicationUncertain, WorkspacePublication
from codeagent.execution.shared_workspace import accepted_output, capture_shared, git_guard
from codeagent.execution.snapshot import apply_snapshot
from codeagent.execution.workspace import capture_workspace
from codeagent.infra.cancellation import CancelledByUser
from codeagent.llm.message import ContextCategory, Message, Role, ToolResultBlock
from codeagent.runtime.react_engine import ReActEngine
from codeagent.runtime.worker_sandbox import WorkerSandbox
from codeagent.workspace.context import WorkspaceContext
from codeagent.workspace.git_worktree import GitWorktreeWorkspaceManager, _run_git
from codeagent.workspace.manager import build_workspace_manager


class InteractiveSandbox:
    def __init__(self, config: AppConfig, manager: PodmanSandboxManager):
        self.config, self.manager = config, manager

    async def send(self, run: AgentRun, engine: ReActEngine, user_input: str) -> AgentRunResult:
        git = await build_workspace_manager(
            self.config.workspace_root, worktree_root=self.config.state_root / "interactive",
        )
        if not isinstance(git, GitWorktreeWorkspaceManager):
            return await self._send_shared(run, engine, user_input, git=False)
        top = await asyncio.to_thread(_run_git, git.repo_root, "rev-parse", "--show-toplevel")
        if git.repo_root != Path(top):
            raise SandboxUnavailable("Podman 普通交互需要项目根目录")
        # A partial publication can happen to leave a clean-looking tree.
        try:
            with WorkspacePublication(
                git.repo_root, self._publication_directory(git.repo_root),
                limits=self.manager.snapshot_limits, guard=lambda: git_guard(git.repo_root),
            ):
                pass
        except OSError as exc:
            raise SandboxError(f"工作区发布锁或恢复记录不可用：{exc}") from exc
        if not await self._clean(git):
            return await self._send_shared(run, engine, user_input, git=True)
        await self.manager.ensure_available()
        original = await git.base_revision()
        candidate = await git.create_candidate(original)
        workspace = run.workspace
        run.workspace = candidate
        run.allow_external_effects = False
        try:
            run.cancellation.raise_if_cancelled()
            async with WorkerSandbox(self.manager, run, ExecutionPurpose.INTERACTIVE) as sandbox:
                result = await engine.run_turn(run, user_input)
                if not result.ok:
                    self._feedback(run, "本轮未完成，隔离执行域的改动不会发布。")
                    return replace(result, files=())
                await sandbox.publish()
            assert sandbox.initial is not None
            if capture_workspace(candidate, self.manager.snapshot_limits) == sandbox.initial:
                return replace(result, files=())
            if not self.config.verify_command:
                raise SandboxError("本轮包含文件改动，但未配置 CODEAGENT_VERIFY_CMD，改动未发布")
            if not await git.commit(candidate, message="codeagent interactive changes"):
                raise SandboxError("本轮改动未进入 Git 候选，改动未发布")
            revision = await git.head(candidate.root)
            changed = await git.changed_files(original, revision)
            before = {entry.path for entry in sandbox.initial.entries}
            after = {entry.path for entry in capture_workspace(
                candidate, self.manager.snapshot_limits,
            ).entries}
            await self._validate(git, run, revision)
            run.cancellation.raise_if_cancelled()
            if not await self._clean(git):
                raise SandboxError("工作区在交互期间被修改，改动未发布")
            # A cancelling caller must not race cleanup against an in-flight Git merge.
            promotion = asyncio.create_task(git.promote(revision, expected_base=original))
            try:
                accepted = await asyncio.shield(promotion)
            except asyncio.CancelledError:
                accepted = await promotion
                if not accepted:
                    raise
            if not accepted:
                raise SandboxError("工作区版本在交互期间变化，改动未发布")
            self._feedback(run, "本轮改动已通过独立验收并发布到项目工作区。")
            return replace(result, files=tuple(
                FileState(path, FileChangeKind.CREATED if path not in before else
                          FileChangeKind.DELETED if path not in after else FileChangeKind.MODIFIED)
                for path in sorted(changed)
            ))
        except CancelledByUser:
            run.status = RunStatus.CANCELLED
            self._finish_interrupted_history(run)
            self._feedback(run, "本轮已取消，改动未发布。")
            return AgentRunResult(run.run_id, RunStatus.CANCELLED, "", error="本轮已取消")
        except asyncio.CancelledError:
            run.status = RunStatus.CANCELLED
            self._finish_interrupted_history(run)
            self._feedback(run, "本轮已取消，改动未发布。")
            raise
        except Exception as exc:
            run.status = RunStatus.FAILED
            self._finish_interrupted_history(run)
            self._feedback(run, "本轮执行或验收失败，改动未发布。后续操作以项目实际文件为准。")
            return AgentRunResult.failed(run.run_id, f"隔离交互失败：{type(exc).__name__}: {exc}")
        finally:
            run.workspace = workspace
            await asyncio.shield(git.cleanup(candidate))

    async def _clean(self, git: GitWorktreeWorkspaceManager) -> bool:
        paths = [".", ":(exclude).codeagent", ":(exclude,glob)**/.env",
                 ":(exclude,glob)**/.env.*"]
        try:
            state = self.config.state_root.resolve().relative_to(git.repo_root)
        except ValueError:
            pass
        else:
            if not state.parts:
                raise SandboxError("交互状态目录必须独立于项目根目录")
            paths.append(f":(exclude){state.as_posix()}")
        status = await asyncio.to_thread(
            _run_git, git.repo_root, "status", "--porcelain", "--untracked-files=all", "--", *paths,
        )
        return not status

    async def _send_shared(
        self, run: AgentRun, engine: ReActEngine, user_input: str, *, git: bool,
    ) -> AgentRunResult:
        root = run.workspace.root
        excluded = set() if git else {
            ".venv", "venv", "__pycache__", ".pytest_cache", ".ruff_cache",
        }
        try:
            state = self.config.state_root.resolve().relative_to(root.resolve())
        except ValueError:
            pass
        else:
            if not state.parts:
                raise SandboxError("交互状态目录必须独立于项目根目录")
            excluded.add(state.as_posix())
        omitted = frozenset(excluded)
        guard = (lambda: git_guard(root)) if git else (lambda: "")
        def capture():
            return capture_shared(root, self.manager.snapshot_limits, git=git, excluded=omitted)

        directory = self._publication_directory(root)
        workspace = run.workspace
        try:
            with WorkspacePublication(root, directory, limits=self.manager.snapshot_limits,
                                      guard=guard) as publication:
                expected = guard()
                initial = capture()
                if guard() != expected:
                    raise SandboxError("输入快照期间项目版本或暂存区变化")
                await self.manager.ensure_available()
                with tempfile.TemporaryDirectory(prefix="mindcode-interactive-") as staging:
                    apply_snapshot(initial, Path(staging), self.manager.snapshot_limits)
                    run.workspace = WorkspaceContext(root=Path(staging), worktree_id="interactive",
                                                     is_isolated=True)
                    run.allow_external_effects = False
                    run.cancellation.raise_if_cancelled()
                    async with WorkerSandbox(
                        self.manager, run, ExecutionPurpose.INTERACTIVE,
                    ) as domain:
                        result = await engine.run_turn(run, user_input)
                        if not result.ok:
                            self._feedback(run, "本轮未完成，隔离改动不会发布。")
                            return replace(result, files=())
                        await domain.publish()
                    output = accepted_output(
                        root, capture_workspace(run.workspace, self.manager.snapshot_limits),
                        git=git, excluded=omitted,
                    )
                    if output == initial:
                        return replace(result, files=())
                    if not self.config.verify_command:
                        raise SandboxError("本轮包含改动但未配置 CODEAGENT_VERIFY_CMD，改动未发布")
                    handle = await self.manager.open(output, ExecutionPurpose.VALIDATION)
                    try:
                        check = await self.manager.execute(
                            handle, self.config.verify_command, cancellation=run.cancellation,
                        )
                        if check.returncode:
                            raise SandboxError("独立验收未通过，改动未发布")
                    finally:
                        await asyncio.shield(self.manager.close(handle))
                    run.cancellation.raise_if_cancelled()
                    publication.publish(initial, output, capture=capture, expected_guard=expected)
                    before = {entry.path: entry for entry in initial.entries}
                    after = {entry.path: entry for entry in output.entries}
                    changed = sorted(name for name in before.keys() | after.keys()
                                     if before.get(name) != after.get(name))
                    self._feedback(
                        run, "本轮改动已验收并回写；原有暂存区与HEAD保留，没有自动提交。",
                    )
                    return replace(result, files=tuple(
                        FileState(name, FileChangeKind.CREATED if name not in before else
                                  FileChangeKind.DELETED if name not in after else
                                  FileChangeKind.MODIFIED) for name in changed
                    ))
        except PublicationUncertain as exc:
            run.status = RunStatus.FAILED
            self._finish_interrupted_history(run)
            self._feedback(run, "本轮写回结果待核对；不能认为全部成功或全部回滚。恢复记录已保留。")
            return AgentRunResult.failed(run.run_id, str(exc))
        except CancelledByUser:
            run.status = RunStatus.CANCELLED
            self._finish_interrupted_history(run)
            self._feedback(run, "本轮已取消，改动未发布。")
            return AgentRunResult(run.run_id, RunStatus.CANCELLED, "", error="本轮已取消")
        except asyncio.CancelledError:
            run.status = RunStatus.CANCELLED
            self._finish_interrupted_history(run)
            self._feedback(run, "本轮已取消，改动未发布。")
            raise
        except Exception as exc:
            run.status = RunStatus.FAILED
            self._finish_interrupted_history(run)
            self._feedback(run, "本轮执行或验收失败，未接受本轮改动。")
            return AgentRunResult.failed(run.run_id, f"隔离交互失败：{type(exc).__name__}: {exc}")
        finally:
            run.workspace = workspace

    def _publication_directory(self, root: Path) -> Path:
        key = hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:24]
        return self.config.state_root / "publication" / key

    async def _validate(
        self, git: GitWorktreeWorkspaceManager, run: AgentRun, revision: str,
    ) -> None:
        validation = await git.create_validation(revision)
        try:
            handle = await self.manager.open(
                capture_workspace(validation, self.manager.snapshot_limits),
                ExecutionPurpose.VALIDATION,
            )
            try:
                assert self.config.verify_command is not None
                output = await self.manager.execute(
                    handle, self.config.verify_command, cancellation=run.cancellation,
                )
                if output.returncode != 0:
                    raise SandboxError("独立验收未通过，改动未发布")
            finally:
                await asyncio.shield(self.manager.close(handle))
        finally:
            await asyncio.shield(git.cleanup(validation))

    @staticmethod
    def _feedback(run: AgentRun, text: str) -> None:
        run.history.append(Message.internal_context(text, ContextCategory.OTHER))

    @staticmethod
    def _finish_interrupted_history(run: AgentRun) -> None:
        messages = run.history.messages
        if messages and messages[-1].role == Role.ASSISTANT and messages[-1].tool_uses:
            run.history.append(Message.tool([
                ToolResultBlock(
                    block.id, "执行已中断；未确认的工具结果不得视为成功。", is_error=True,
                )
                for block in messages[-1].tool_uses
            ], turn_id=run.history.current_turn_id))
        run.history.end_turn(str(run.status))
