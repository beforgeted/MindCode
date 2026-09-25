from __future__ import annotations

import pytest

from codeagent.orchestration.task_graph import Step, TaskGraph, TaskGraphError


def test_ready_respects_dependencies():
    graph = TaskGraph(
        [
            Step("a", "default", "A"),
            Step("b", "default", "B", dependencies=frozenset({"a"})),
            Step("c", "default", "C", dependencies=frozenset({"a"})),
            Step("d", "default", "D", dependencies=frozenset({"b", "c"})),
        ]
    )
    assert {s.id for s in graph.ready(set())} == {"a"}
    assert {s.id for s in graph.ready({"a"})} == {"b", "c"}
    assert {s.id for s in graph.ready({"a", "b", "c"})} == {"d"}
    # 已完成的不再 ready。
    assert graph.ready({"a", "b", "c", "d"}) == []


def test_exclude_running_and_done():
    graph = TaskGraph([Step("a", "default", "A"), Step("b", "default", "B")])
    assert {s.id for s in graph.ready(set(), exclude={"a"})} == {"b"}


def test_cycle_detected():
    with pytest.raises(TaskGraphError):
        TaskGraph(
            [
                Step("a", "default", "A", dependencies=frozenset({"b"})),
                Step("b", "default", "B", dependencies=frozenset({"a"})),
            ]
        )


def test_missing_dependency_rejected():
    with pytest.raises(TaskGraphError):
        TaskGraph([Step("a", "default", "A", dependencies=frozenset({"ghost"}))])


def test_duplicate_id_rejected():
    with pytest.raises(TaskGraphError):
        TaskGraph([Step("a", "default", "A"), Step("a", "default", "again")])


def test_blocked_by_failure_is_transitive():
    graph = TaskGraph(
        [
            Step("a", "default", "A"),
            Step("b", "default", "B", dependencies=frozenset({"a"})),
            Step("c", "default", "C", dependencies=frozenset({"b"})),
            Step("d", "default", "D"),
        ]
    )
    assert graph.blocked_by({"a"}) == {"b", "c"}
    assert graph.blocked_by(set()) == set()
