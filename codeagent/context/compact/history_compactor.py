from __future__ import annotations

import asyncio
from dataclasses import dataclass

from codeagent.context.compact.base import CompactionResult
from codeagent.context.compact.chunker import CompactionChunk, HistoryChunker
from codeagent.context.compact.map_summarizer import HistoryMapSummarizer
from codeagent.context.compact.models import TaskCheckpoint, TaskDelta
from codeagent.context.compact.reducer import TaskStateReducer
from codeagent.context.history.conversation_history import validate_tool_protocol
from codeagent.context.history.turn import TurnIdPartitioner, TurnStatus, system_messages
from codeagent.context.profile import ContextProfile
from codeagent.context.token_estimator import TokenEstimator
from codeagent.infra import metrics as M
from codeagent.infra.metrics import Metrics
from codeagent.llm.client import LlmClient
from codeagent.llm.message import ContextCategory, Message
from codeagent.llm.types import ModelConfig


@dataclass(frozen=True, slots=True)
class _MappedChunk:
    chunk: CompactionChunk
    delta: TaskDelta | None
    error: Exception | None = None


class ConversationHistoryCompactor:
    def __init__(
        self,
        client: LlmClient,
        estimator: TokenEstimator,
        model_config: ModelConfig,
        *,
        metrics: Metrics | None = None,
    ) -> None:
        self._estimator = estimator
        self._partitioner = TurnIdPartitioner()
        self._chunker = HistoryChunker(estimator)
        self._mapper = HistoryMapSummarizer(client, model_config)
        self._reducer = TaskStateReducer(client, model_config)
        self._metrics = metrics or Metrics()

    async def compact(
        self,
        messages,
        *,
        profile: ContextProfile,
        focus: str | None = None,
        checkpoint: TaskCheckpoint | None = None,
        turn_statuses: dict[str, TurnStatus] | None = None,
    ) -> CompactionResult:
        source = tuple(messages)
        tokens_before = self._estimator.estimate(source)
        try:
            async with asyncio.timeout(profile.compaction_timeout_seconds):
                return await self._compact(
                    source,
                    profile=profile,
                    focus=focus,
                    checkpoint=checkpoint,
                    turn_statuses=turn_statuses or {},
                    tokens_before=tokens_before,
                )
        except TimeoutError:
            self._metrics.incr(M.CONTEXT_COMPACTION_TIMEOUTS)
            return self._failed(source, tokens_before, "压缩超时")
        except Exception as exc:
            self._metrics.incr(M.CONTEXT_COMPACTION_FAILURES)
            return self._failed(source, tokens_before, f"压缩失败: {type(exc).__name__}: {exc}")

    async def _compact(
        self,
        source: tuple[Message, ...],
        *,
        profile: ContextProfile,
        focus: str | None,
        checkpoint: TaskCheckpoint | None,
        turn_statuses: dict[str, TurnStatus],
        tokens_before: int,
    ) -> CompactionResult:
        turns = self._partitioner.partition(source, statuses=turn_statuses)
        completed = [turn for turn in turns if turn.compactable]
        retain_count = min(profile.retain_recent_turns, len(completed))
        retained_ids = (
            {turn.turn_id for turn in completed[-retain_count:]}
            if retain_count
            else set()
        )
        candidates = [turn for turn in completed if turn.turn_id not in retained_ids]
        if not candidates:
            return self._failed(source, tokens_before, "没有可压缩的 completed turn")

        chunks = self._chunker.chunk(candidates, max_tokens=profile.map_chunk_tokens)
        mapped = await self._map_chunks(
            chunks,
            focus=focus,
            concurrency=profile.compaction_map_concurrency,
            max_output_tokens=profile.map_max_output_tokens,
        )
        successes = tuple(item.delta for item in mapped if item.delta is not None)
        failures = tuple(item for item in mapped if item.delta is None)
        self._metrics.incr(M.CONTEXT_COMPACTION_MAP_CHUNKS, len(mapped))
        self._metrics.incr(M.CONTEXT_COMPACTION_MAP_FAILURES, len(failures))
        if not successes:
            return self._failed(
                source,
                tokens_before,
                "所有 Map chunk 均失败",
                map_chunks=len(mapped),
                map_failures=len(failures),
            )

        next_checkpoint = await self._reducer.reduce(
            checkpoint,
            successes,
            max_output_tokens=profile.reduce_max_output_tokens,
            focus=focus,
        )
        candidate = self._assemble(source, mapped, retained_ids, next_checkpoint)
        tokens_after = self._estimator.estimate(candidate)
        checkpoint_tokens = self._estimator.estimate_message(next_checkpoint.to_message())
        if (
            tokens_after > profile.target_after_compression
            and checkpoint_tokens > profile.checkpoint_max_tokens
        ):
            strict_limit = max(
                512,
                min(
                    profile.reduce_max_output_tokens,
                    profile.checkpoint_max_tokens,
                    profile.target_after_compression
                    - (tokens_after - checkpoint_tokens),
                ),
            )
            next_checkpoint = await self._reducer.reduce(
                checkpoint,
                successes,
                max_output_tokens=strict_limit,
                focus=(focus or "")
                + "\nEmergency budget: keep every durable fact but make entries maximally concise.",
            )
            candidate = self._assemble(source, mapped, retained_ids, next_checkpoint)
            tokens_after = self._estimator.estimate(candidate)

        validate_tool_protocol(candidate)
        if tokens_after >= tokens_before:
            return self._failed(
                source,
                tokens_before,
                "候选历史没有释放 token",
                map_chunks=len(mapped),
                map_failures=len(failures),
            )
        if tokens_after > profile.hard_trigger:
            return self._failed(
                source,
                tokens_before,
                f"候选历史 {tokens_after} 仍超过 hard limit {profile.hard_trigger}",
                map_chunks=len(mapped),
                map_failures=len(failures),
            )
        return CompactionResult(
            compacted=True,
            messages=candidate,
            checkpoint=next_checkpoint,
            tokens_before=tokens_before,
            tokens_after=tokens_after,
            map_chunks=len(mapped),
            map_failures=len(failures),
            reason=(
                f"压缩 {len(successes)}/{len(mapped)} 个 chunks；"
                f"保留 {len(failures)} 个失败 chunk 原文"
            ),
        )

    async def _map_chunks(
        self,
        chunks: tuple[CompactionChunk, ...],
        *,
        focus: str | None,
        concurrency: int,
        max_output_tokens: int,
    ) -> tuple[_MappedChunk, ...]:
        semaphore = asyncio.Semaphore(max(1, concurrency))

        async def run(chunk: CompactionChunk) -> _MappedChunk:
            async with semaphore:
                try:
                    return _MappedChunk(
                        chunk,
                        await self._mapper.summarize(
                            chunk,
                            focus=focus,
                            max_output_tokens=max_output_tokens,
                        ),
                    )
                except Exception as exc:
                    return _MappedChunk(chunk, None, exc)

        return tuple(await asyncio.gather(*(run(chunk) for chunk in chunks)))

    def _assemble(
        self,
        source: tuple[Message, ...],
        mapped: tuple[_MappedChunk, ...],
        retained_ids: set[str],
        checkpoint: TaskCheckpoint,
    ) -> tuple[Message, ...]:
        systems = system_messages(source)
        failed_ids = {
            turn.turn_id
            for item in mapped
            if item.delta is None
            for turn in item.chunk.turns
        }
        kept = [
            message
            for message in source
            if message.role.value != "system"
            and message.category is not ContextCategory.CHECKPOINT
            and message.turn_id in (failed_ids | retained_ids)
        ]
        known_candidate_ids = {
            turn.turn_id for item in mapped for turn in item.chunk.turns
        } | retained_ids
        kept.extend(
            message
            for message in source
            if message.role.value != "system"
            and message.category is not ContextCategory.CHECKPOINT
            and message.turn_id not in known_candidate_ids
        )
        return tuple([*systems, checkpoint.to_message(), *kept])

    @staticmethod
    def _failed(
        source: tuple[Message, ...],
        tokens_before: int,
        reason: str,
        *,
        map_chunks: int = 0,
        map_failures: int = 0,
    ) -> CompactionResult:
        return CompactionResult(
            compacted=False,
            messages=source,
            tokens_before=tokens_before,
            tokens_after=tokens_before,
            map_chunks=map_chunks,
            map_failures=map_failures,
            reason=reason,
        )
