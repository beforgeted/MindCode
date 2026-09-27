"""具体场景集合。用 StaticPlanner 固定 DAG 的场景专测四层收敛机制；
graph_factory=None 的场景测真实 LlmPlanner（第 ③ 层：规划质量，目前最大盲区）。
"""

from __future__ import annotations

from codeagent.orchestration.task_graph import Step, TaskGraph
from scenarios.model import (
    CheckResult,
    Scenario,
    file_contains,
    file_exists,
    file_line_multiset,
)


def _dep_chain() -> TaskGraph:
    a = Step(
        id="a",
        agent_id="default",
        instruction="在仓库根创建 lib.py，内容恰好一行：VERSION = 7",
    )
    b = Step(
        id="b",
        agent_id="default",
        instruction=(
            "先 read_file 读 lib.py 里的 VERSION 值，再创建 version.txt，"
            "内容就是那个数字（这里是 7），不要任何多余字符"
        ),
        dependencies=frozenset({"a"}),
    )
    return TaskGraph([a, b])


def _two_appends(target: str) -> TaskGraph:
    def step(sid: str, tag: str) -> Step:
        return Step(
            id=sid,
            agent_id="default",
            instruction=(
                f"向仓库根的 {target} 追加一行 {tag}"
                "（用 >> 追加，文件不存在就创建；绝不覆盖已有内容）"
            ),
        )

    return TaskGraph([step("a", "from-a"), step("b", "from-b")])


def _conf_conflict() -> TaskGraph:
    a = Step(
        id="a",
        agent_id="default",
        instruction="把 conf.py 里的 TIMEOUT 改成 30，其它不动",
    )
    b = Step(
        id="b",
        agent_id="default",
        instruction="在 conf.py 末尾追加一行：RETRIES = 3，不要动已有内容",
    )
    return TaskGraph([a, b])


def _single(instruction: str) -> TaskGraph:
    return TaskGraph([Step(id="s", agent_id="default", instruction=instruction)])


_UTILS_SRC = (
    "def upper(s):\n    return s.upper()\n\n\n"
    "def add(a, b):\n    return a + b\n"
)


def _deep_chain() -> TaskGraph:
    a = Step(
        id="a",
        agent_id="default",
        instruction="在仓库根创建 base.txt，内容恰好是数字 10（只有这一个数字）",
    )
    b = Step(
        id="b",
        agent_id="default",
        instruction=(
            "先 read_file 读 base.txt 的数字，再创建 plus.txt，"
            "内容是该数字 +1（这里是 11），只写这一个数字"
        ),
        dependencies=frozenset({"a"}),
    )
    c = Step(
        id="c",
        agent_id="default",
        instruction=(
            "先 read_file 读 plus.txt 的数字，再创建 final.txt，"
            "内容是该数字 +1（这里是 12），只写这一个数字"
        ),
        dependencies=frozenset({"b"}),
    )
    return TaskGraph([a, b, c])


def _fanout_appends(target: str, tags: tuple[str, ...]) -> TaskGraph:
    def step(sid: str, tag: str) -> Step:
        return Step(
            id=sid,
            agent_id="default",
            instruction=(
                f"向仓库根的 {target} 追加一行 {tag}"
                "（用 >> 追加，文件不存在就创建；绝不覆盖或删除已有内容）"
            ),
        )

    return TaskGraph([step(t, t) for t in tags])



def _absent(rel: str):
    def check(repo):
        ok = not (repo / rel).exists()
        return CheckResult(ok, "" if ok else f"{rel} 不该存在（Attempt 应被丢弃）")

    return check


SCENARIOS: list[Scenario] = [
    Scenario(
        name="dep_chain",
        description="P1 依赖门控：b 依赖 a，必须看得到 a 已集成的产物",
        task="创建 lib.py 定义 VERSION=7，再据此写 version.txt",
        graph_factory=_dep_chain,
        checks=[file_contains("lib.py", "VERSION = 7"), file_line_multiset("version.txt", ["7"])],
    ),
    Scenario(
        name="overlap_append",
        description="P2 乐观并发：两并行步各追加一行，过期重跑收敛、无重复",
        task="两个独立步骤各向 log.txt 追加一行",
        graph_factory=lambda: _two_appends("log.txt"),
        checks=[file_line_multiset("log.txt", ["from-a", "from-b"])],
    ),
    Scenario(
        name="strong_conflict",
        description="P3 Integrator 兜底：关重跑预算，两步改同一文件，靠增强指令合一",
        task="改 conf.py 的 TIMEOUT 并追加 RETRIES",
        seed_files={"conf.py": "TIMEOUT = 10\n"},
        graph_factory=_conf_conflict,
        checks=[file_contains("conf.py", "TIMEOUT = 30"), file_contains("conf.py", "RETRIES = 3")],
        profile_overrides={"agent_max_reruns": 0},
    ),
    Scenario(
        name="verify_fail",
        description="事务 + fail-closed：验收命令恒失败，每次 reject → 真实 base 分毫不动",
        task="创建 note.txt 内容 hello",
        graph_factory=lambda: _single("创建 note.txt，内容为 hello"),
        checks=[_absent("note.txt")],
        expect_integrated=False,
        verify_command="false",
    ),
    Scenario(
        name="simple_create",
        description="基线：单步建实现 + 测试文件",
        task="实现 fizzbuzz 并写测试",
        graph_factory=lambda: _single(
            "创建 fizzbuzz.py 实现 fizzbuzz(n) 返回 1..n 的 FizzBuzz 列表；"
            "再创建 test_fizzbuzz.py 用 assert 覆盖 3、5、15、1 四个点"
        ),
        checks=[file_exists("fizzbuzz.py"), file_exists("test_fizzbuzz.py")],
    ),
    Scenario(
        name="planner_freeform",
        description="第③层 真实 LlmPlanner：自然语言目标 → 自主拆图 → 完成",
        task="创建 add.py 实现 add(a,b) 返回 a+b；再创建 test_add.py 用 pytest 断言 add(2,3)==5",
        graph_factory=None,  # 走真实 LlmPlanner
        checks=[file_exists("add.py"), file_exists("test_add.py")],
    ),
    Scenario(
        name="deep_chain",
        description="①依赖门控三层链：a→b→c 每层都必须看到上一层已集成的产物",
        task="三层依赖：base=10 → plus=11 → final=12",
        graph_factory=_deep_chain,
        checks=[
            file_line_multiset("base.txt", ["10"]),
            file_line_multiset("plus.txt", ["11"]),
            file_line_multiset("final.txt", ["12"]),
        ],
    ),
    Scenario(
        name="wide_fanout",
        description="②乐观并发扇出=3：三并行步各追加一行，多次过期重跑仍收敛、无重复",
        task="三个独立步骤各向 log.txt 追加一行",
        graph_factory=lambda: _fanout_appends("log.txt", ("from-a", "from-b", "from-c")),
        checks=[file_line_multiset("log.txt", ["from-a", "from-b", "from-c"])],
    ),
    Scenario(
        name="verify_pass",
        description="④确定性验收 ACCEPT：验收命令读真实产物内容通过 → 正确 promote",
        task="创建 answer.txt，内容为 42",
        graph_factory=lambda: _single(
            "在仓库根创建 answer.txt，内容恰好是数字 42（只有这一个数字）"
        ),
        checks=[file_line_multiset("answer.txt", ["42"])],
        verify_command="grep -qx 42 answer.txt",
    ),
    Scenario(
        name="planner_trap",
        description="第③层 规划易拆错：诱导拆成多步改同一文件，考察系统仍收敛",
        task="创建 utils.py 实现 add(a,b) 返回 a+b，并在同一文件里给 add 补一段文档字符串",
        graph_factory=None,  # 走真实 LlmPlanner（可能过度拆分）
        checks=[file_exists("utils.py"), file_contains("utils.py", "def add")],
    ),
    Scenario(
        name="split_utils_e2e",
        description="③→②闭环：真实 Planner 拆 utils.py（探针测得同文件重叠），运行时靠重跑收敛",
        task="重构 utils.py：把它拆成 string_utils.py 和 math_utils.py 两个文件，各放对应函数",
        seed_files={"utils.py": _UTILS_SRC},
        graph_factory=None,  # 真实 LlmPlanner：探针显示它会造出多个碰 utils.py 的无依赖步
        checks=[file_exists("string_utils.py"), file_exists("math_utils.py")],
    ),
]
