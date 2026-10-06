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

## 质量 Benchmark（Phase 9，`scenarios/benchmark.py`）

场景套件之上的**量化基线**：19 个任务按 维度×难度 分层，产出成功率 / 首次成功率 / 假接受 /
平均重跑·兜底 / 每任务耗时·token 的聚合指标（分 overall / dimension / tier）。同样 opt-in。

```bash
python -m scenarios.benchmark                       # 全部 19 个任务，各 1 次
python -m scenarios.benchmark --dimension planner   # 只跑某维度
python -m scenarios.benchmark --repeat 2            # 每个跑 2 次看稳定性
```

- 维度 `planner`（真实拆图）/ `worker`（明确指令固定图）/ `verifier`（确定性验收正负例）/
  `integration`（依赖链/重叠/冲突）。
- **假接受** = `integrated=True` 但产物断言没过（验证器放行了错产物），可精确测；**假拒绝**近似为
  "期望成功却未达成"，标注不能纯归因 verifier。
- 结果写 `scenarios/last_benchmark.json`（gitignore）。聚合是纯函数，见 `tests/test_benchmark.py`。

## Planner 规划质量探针（`scenarios/planner_probe.py`）

只调真实 `LlmPlanner.plan()`，采样拆图形状 + 依赖边 + **并发同文件写风险**（用依赖**传递闭包**判有序，
a→b→c 不误报）。`python -m scenarios.planner_probe --samples 5`。

## SWE-bench（未接，接入笔记）

全集需容器化 + 大量依赖，本期不接。接单个实例的路径：clone 实例 repo 当 sandbox →
用其 `FAIL_TO_PASS` 测试当 `CODEAGENT_VERIFY_CMD` → 跑 `/task` → 看确定性验收转绿。等核心更稳再接少量实例。




## 无模型费用的社区兼容矩阵

新增 `python -m scenarios.ecosystem_compatibility`，读取已固定的真实社区源码与已安装官方 MCP，生成包清单、目录和测试 fixture；不联网安装、不修改源包、不运行社区 Skill 脚本、不调用模型。准备输出目录必须尚不存在，避免覆盖历史。

具体版本、Time → Filesystem → Everything → Git 的判据、开关与复现命令见[工具与能力测试](../docs/testing/tools-and-skills.md)。`tests/test_ecosystem.py` 用于 Windows/VM 的加载与协议探测；`tests/test_ecosystem_podman.py` 只在独立 VM 验证运行调用及发布门禁。模拟 Provider 仅控制任务流程，MCP 服务与容器是真实实现。

另准备固定官方 Memory npm 安装前缀时，可以传入 `--memory-node-root <Memory安装前缀>`；省略该参数仍只发现原来的四个服务。对应 `tests/test_mcp_memory.py` 和 `tests/test_mcp_memory_podman.py` 分别检查配置/状态文件边界与真实跨调用、Worker 隔离、失败回收、发布门禁。运行时镜像必须另含相同 Memory 分发包；发现不会调用写工具或打开运行授权。

已显式安装固定官方 Fetch 源码与依赖时，可以增加 `--fetch`，只进行协议发现并记录来源版本，不发 HTTP 请求。配套 `tests/test_mcp_fetch.py` 与 `tests/test_mcp_fetch_podman.py` 验证域名/DNS/IP、robots、下载量、真实服务提取与分页、取消和失败回收。真实 HTTPS 用例仅在独立 VM 设置固定公开的 `MINDCODE_FETCH_LIVE_URL` 与受信 `MINDCODE_FETCH_IMAGE` 后执行；配置入口见[社区 MCP 接入](../docs/mcp-tools.md)。原始结果与失败现场保存在忽略目录。

## 沙箱运行约束

项目规定Linux测试只在独立Ubuntu VM运行，禁止使用WSL Ubuntu。运行模型前应显式设置
CODEAGENT_EXECUTION_BACKEND=podman、CODEAGENT_SANDBOX_IMAGE为已安装的完整SHA256 ID；
按场景配置独立验收命令。仅设置MINDCODE_PODMAN_TEST_IMAGE不会切换应用后端，默认后端仍为local。
真实模型必须确认use_stub_llm=False；模型凭据只放控制面，容器保持断网且不挂载宿主目录。

最新完整验收及历史真实模型回归的适用范围统一见[测试总览](../docs/testing/README.md)，
沙箱判据见[沙箱与恢复测试](../docs/testing/sandbox-and-recovery.md)。个人现场与原始报告不随仓库发布。
历史11场景回归不是19任务benchmark或SWE-bench成绩；verify_fail还需核对Worker确实完成、
验收拒绝、base不动与产物不存在，避免把没有发生的写入误报为隔离成功。
# Knowledge 离线检索对照

`python -m scenarios.knowledge_evaluation --root <冻结公开源码副本> --output <新私有输出目录>` 在真实 MindCode 源码执行 easy→medium→hard 六项查询，并与现有 grep 的同字面查询比较。基线直接使用现有 GrepTool，应只对不含私有内容的冻结公开源码副本执行；不要把实时私人工作目录作为基线输入。报告记录来源命中、引用完整文本校验、输出估算 Token、调用数、机器及时间，没有模型调用或模型质量结论。更多判据见[Knowledge 测试](../docs/testing/knowledge.md)。
