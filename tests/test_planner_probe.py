"""Phase 9a：Planner 探针的并发同文件判定用传递闭包（消除只看直接边的假阳性）。"""

from __future__ import annotations

from codeagent.orchestration.task_graph import Step, TaskGraph
from scenarios.planner_probe import _concurrent_same_file, _dep_closure


def test_transitive_chain_same_file_not_flagged() -> None:
    # a→b→c，a 与 c 都碰 utils.py，但 c 传递依赖 a → 有先后，不算并发风险
    a = Step("a", "default", "改 utils.py 的 x")
    b = Step("b", "default", "读 utils.py 写 mid.py", dependencies=frozenset({"a"}))
    c = Step("c", "default", "再改 utils.py 的 y", dependencies=frozenset({"b"}))
    assert _concurrent_same_file(TaskGraph([a, b, c])) == set()


def test_truly_concurrent_same_file_flagged() -> None:
    # 两步无任何依赖关系、都碰 conf.py → 真并发同文件写，命中
    a = Step("a", "default", "改 conf.py 的 TIMEOUT")
    b = Step("b", "default", "改 conf.py 的 RETRIES")
    assert _concurrent_same_file(TaskGraph([a, b])) == {"conf.py"}


def test_dep_closure_is_transitive() -> None:
    a = Step("a", "default", "A")
    b = Step("b", "default", "B", dependencies=frozenset({"a"}))
    c = Step("c", "default", "C", dependencies=frozenset({"b"}))
    closure = _dep_closure(TaskGraph([a, b, c]))
    assert closure["c"] == {"a", "b"}  # c 传递依赖 a 和 b
    assert closure["a"] == set()
