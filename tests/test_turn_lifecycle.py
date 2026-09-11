from __future__ import annotations

import pytest

from codeagent.context.history.conversation_history import ConversationHistory
from codeagent.context.history.turn import TurnIdPartitioner, TurnStatus
from codeagent.llm.message import Message


def test_turn_lifecycle_is_owned_by_history():
    history = ConversationHistory(session_id="ses", agent_run_id="run")
    turn_id = history.begin_turn()
    history.append(Message.user("work", turn_id=turn_id))

    assert history.turn_statuses[turn_id] is TurnStatus.RUNNING
    assert not TurnIdPartitioner().partition(
        history.messages, statuses=history.turn_statuses
    )[0].compactable

    history.end_turn("success")

    assert history.turn_statuses[turn_id] is TurnStatus.COMPLETED
    assert TurnIdPartitioner().partition(
        history.messages, statuses=history.turn_statuses
    )[0].compactable


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("cancelled", TurnStatus.CANCELLED),
        ("failed", TurnStatus.FAILED),
        ("max_iterations", TurnStatus.FAILED),
        ("unknown", TurnStatus.FAILED),
    ],
)
def test_non_success_turns_are_never_compactable(raw: str, expected: TurnStatus):
    history = ConversationHistory(session_id="ses", agent_run_id="run")
    turn_id = history.begin_turn()
    history.append(Message.user("work", turn_id=turn_id))
    history.end_turn(raw)

    turn = TurnIdPartitioner().partition(
        history.messages, statuses=history.turn_statuses
    )[0]
    assert turn.status is expected
    assert not turn.compactable


def test_unknown_turn_status_defaults_to_running():
    message = Message.user("legacy", turn_id="legacy")
    turn = TurnIdPartitioner().partition([message])[0]
    assert turn.status is TurnStatus.RUNNING
    assert not turn.compactable


def test_cannot_begin_overlapping_turns():
    history = ConversationHistory(session_id="ses", agent_run_id="run")
    history.begin_turn()
    with pytest.raises(RuntimeError, match="尚未结束"):
        history.begin_turn()
