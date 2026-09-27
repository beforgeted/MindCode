# 场景套件（真实-LLM 可复现测试）

测试金字塔的第 ② 层。单元测试（`tests/`）守控制面不变式、是确定性的；本套件在**真实
LLM** 下验证四层收敛机制是否按预期工作，以及改 prompt / 换模型后有没有行为退化。

每个场景 = 初始仓库快照 + `/task` 目标 + 确定性判据。runner 为每个场景开一个隔离的临时
git 仓库（sandbox，`CODEAGENT_HOME` 也在其内，互不污染），跑真实 `MasterRuntime`，跑完对
**promote 后的真实 base 工作树**打分，并汇总收敛指标。

## 跑法

```bash
conda activate mindcode
python -m scenarios.runner                         # 全部，各 1 次
python -m scenarios.runner --only dep_chain         # 只跑某几个
python -m scenarios.runner --repeat 3               # 每个跑 3 次看稳定性（弱模型有随机性）
python -m scenarios.runner --keep                   # 保留 sandbox 供排查
python -m scenarios.runner --env /path/to/.env      # 指定 .env
```

需要真实模型：先在仓库根放 `.env`（`ANTHROPIC_API_KEY` / `ANTHROPIC_BASE_URL` / `CODEAGENT_MODEL`）。
没配 key 会走 Stub，Worker 不会真的写文件，除 `verify_fail` 外基本会 FAIL。

跑完在 `scenarios/last_report.json` 写一份 JSON 明细（已 gitignore）。

## 内置场景

| 名称 | 层 | 判据 |
|---|---|---|
| `dep_chain` | ① 依赖门控 | b 读到 a 已集成的 `lib.py` → `version.txt=7` |
| `overlap_append` | ② 乐观并发重跑 | 两步各追加一行，`log.txt` 恰好两行不重复 |
| `strong_conflict` | ③ Integrator 兜底（关重跑预算） | `conf.py` 同时含 `TIMEOUT=30` 与 `RETRIES=3` |
| `verify_fail` | ④ 事务 + fail-closed | 验收恒失败 → 真实 base HEAD 分毫不动、产物不残留 |
| `simple_create` | 基线单步 | 生成实现 + 测试文件 |
| `planner_freeform` | 真实 LlmPlanner | 自然语言目标自主拆图并完成 |
| `deep_chain` | ① 三层依赖链 | a→b→c 每层看到上一层产物，`final.txt=12` |
| `wide_fanout` | ② 扇出=3 重跑 | 三并行步追加，`log.txt` 恰好三行不重复 |
| `verify_pass` | ④ 确定性验收 ACCEPT | 验收命令读真实产物通过 → 正确 promote |
| `planner_trap` | 真实 Planner 抗拆错 | 诱导多步改同一文件，系统仍收敛 |
| `split_utils_e2e` | ③→②闭环 | 真实 Planner 拆 `utils.py`，运行时靠 stale→重跑收敛 |

> 说明一处**设计边界**：读/写语义过期只在"写方先于读方集成"时可检出。若无声明依赖、
> 读方恰好先集成，则该读方基于旧值的结果不会被追溯判过期——这类隐患的正解是声明依赖
> （`deep_chain` 那样），而非靠乐观并发兜。因此本套件不放"读方先集成"的不确定场景当回归项。

`graph_factory` 有值的场景用 `StaticPlanner` 固定 DAG（只钉图，Worker 仍走真实 LLM），
把"规划质量"变量隔离掉、专测集成收敛；`graph_factory=None` 的场景（`planner_freeform`）
才交给真实 `LlmPlanner`，专测规划质量——目前最大的盲区。

## 加场景

在 `scenarios/suite.py` 的 `SCENARIOS` 里追加一个 `Scenario`。判据构造器在 `scenarios/model.py`
（`file_exists` / `file_contains` / `file_line_multiset`），也可写任意 `(repo: Path) -> CheckResult`。
`expect_integrated=False` 的场景，runner 会自动追加"真实 base 必须不推进"的断言。

## 与 CI 的关系（显式启用，普通 pytest 不跑）

本套件**不是** pytest 用例（`scenarios/` 下没有 `test_*.py`），普通 `pytest` / CI 不会收集或
运行它——真实 LLM 调用永远只在显式 `python -m scenarios.runner` 时发生。定位：

- 普通 CI：只跑 `tests/`（确定性单测，守控制面不变式），零 LLM。
- 夜间 / 手工在 VM：显式跑本套件 + `scenarios/planner_probe.py`（第③层规划质量探针）。

## 判据哲学：正确性用断言，收敛指纹用观察

弱模型下并发完成顺序、模型行为会合理波动，所以**断言只压"必须成立的正确性不变式"**，
收敛指标（reruns/stale/integrations）作为**观察值**汇总进表、不做精确相等断言：

| 类别 | 怎么处理 |
|---|---|
| 最终产物正确 | 断言（`file_*` 判据读 promote 后的真实 base） |
| base 一致 / 未通过验收不落 base | 断言（`expect_integrated=False` 自动追加 base 不变） |
| 无重复副作用 | 断言（`file_line_multiset` 抓重复行） |
| 重试未超预算 / 未绕过验收 | 断言（超预算→Step 失败→`integrated=False` 被 expect 捕获） |
| reruns/stale/integrations 的具体值 | **观察**（表内汇总；作漂移信号，不作精确断言） |

`reruns == 1` 这类是很好的回归信号，但更适合"落在预期区间"而非钉死精确值——模型换代后
个别场景多重跑一次仍算健康，真正不能变的是上面几条正确性不变式。

