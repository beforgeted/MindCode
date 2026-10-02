from __future__ import annotations

import json
from dataclasses import replace

from codeagent.context.compact.chunker import CompactionChunk
from codeagent.context.compact.models import CompactionPayloadError, TaskDelta
from codeagent.context.compact.request_budget import (
    REPAIR_PROMPT,
    CompactionBudgetError,
    request_fits,
)
from codeagent.context.token_estimator import HeuristicTokenEstimator, TokenEstimator
from codeagent.llm.client import LlmClient, effective_model_config
from codeagent.llm.message import Message
from codeagent.llm.types import LlmResponse, ModelConfig

_MAP_SYSTEM = """You extract durable task-state changes from old agent history.
Return exactly one JSON object and no commentary. Never copy large tool output.
Use only these keys: goal, constraints, decisions, completed_work, files, tests,
failed_attempts, open_issues, next_steps, evidence_refs. Missing lists are [].
Nested shapes: decisions {decision,rationale}; files {path,change}; tests
{name,outcome,detail}; failed_attempts {attempt,why_failed}; evidence_refs
{type,event_id,session_id,agent_run_id,tool_run_id,artifact_uri}.
Allowed file changes: created, modified, deleted. Allowed test outcomes: pass, fail,
skipped, unknown. Preserve explicit constraints, decisions, failures and evidence IDs."""


class HistoryMapSummarizer:
    def __init__(
        self,
        client: LlmClient,
        model_config: ModelConfig,
        estimator: TokenEstimator | None = None,
    ) -> None:
        self._client = client
        self._model_config = model_config
        self._estimator = estimator or HeuristicTokenEstimator()

    def _config(self, max_output_tokens: int) -> ModelConfig:
        return effective_model_config(self._client, replace(
            self._model_config, model=self._model_config.map_model,
            max_output_tokens=max_output_tokens, temperature=0.0,
        ))

    def fits(self, chunk: CompactionChunk, *, focus: str | None, max_output_tokens: int) -> bool:
        return request_fits(self._messages(chunk, focus), self._config(max_output_tokens),
                            self._estimator)

    async def summarize(
        self,
        chunk: CompactionChunk,
        *,
        focus: str | None,
        max_output_tokens: int,
    ) -> TaskDelta:
        config = self._config(max_output_tokens)
        messages = self._messages(chunk, focus)
        if not request_fits(messages, config, self._estimator):
            raise CompactionBudgetError('单个完整 Map chunk 无法容纳模型窗口与修复预留')
        return await _call_json(self._client, messages, config, TaskDelta.from_json,
                                estimator=self._estimator)

    @staticmethod
    def _messages(chunk: CompactionChunk, focus: str | None) -> tuple[Message, ...]:
        payload = _serialize_messages(chunk.messages)
        focus_text = focus or "Preserve all durable task state."
        prompt = (
            f"Focus: {focus_text}\nChunk tokens (estimated): {chunk.tokens}\n\n"
            f"HISTORY_JSON:\n{payload}"
        )
        return (Message.system(_MAP_SYSTEM), Message.user(prompt))


async def _call_json(client, messages, config, parser, *, estimator=None):
    estimator = estimator or HeuristicTokenEstimator()
    last_error: Exception | None = None
    retry_messages = messages
    for attempt in range(2):
        if not request_fits(retry_messages, effective_model_config(client, config), estimator,
                            reserve_repair=False):
            raise CompactionBudgetError('压缩请求或 JSON 修复请求超过有效模型窗口')
        response = await client.chat(retry_messages, model_config=config)
        try:
            _validate_stop(response)
            return parser(response.content)
        except CompactionPayloadError as exc:
            last_error = exc
            if attempt:
                break
            retry_messages = (
                *messages,
                Message.user(REPAIR_PROMPT),
            )
    assert last_error is not None
    raise last_error


def _validate_stop(response: LlmResponse) -> None:
    if response.stop_reason == "max_tokens":
        raise CompactionPayloadError("压缩输出被 max_tokens 截断")
    if response.stop_reason not in {None, "end_turn", "stop_sequence"}:
        raise CompactionPayloadError(f"压缩调用异常停止: {response.stop_reason}")
    if not response.content.strip():
        raise CompactionPayloadError("压缩调用返回空内容")


def _serialize_messages(messages) -> str:
    rows = []
    for message in messages:
        rows.append(
            {
                "role": str(message.role),
                "turn_id": message.turn_id,
                "text": message.text,
                "tool_uses": [
                    {"id": block.id, "name": block.name, "arguments": block.arguments}
                    for block in message.tool_uses
                ],
                "tool_results": [
                    {
                        "tool_use_id": block.tool_use_id,
                        "content": block.content,
                        "is_error": block.is_error,
                        "artifact_uri": block.artifact_uri,
                    }
                    for block in message.tool_results
                ],
            }
        )
    return json.dumps(rows, ensure_ascii=False, separators=(",", ":"))
