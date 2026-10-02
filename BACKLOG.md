# MindCode 收尾清单（Backlog）

> 测试环境约束：后续 Linux 测试仅在独立 Ubuntu VM `mengx@192.168.100.128` 运行，禁止在 WSL Ubuntu 跑测试；已有 WSL 结果仅保留为历史记录。

> P0–P9 已落地（能力 → 失败安全 → 崩溃恢复 → 可量化基线）。本表记录剩余增强项，
> 按"该不该做、多急、多大"分档。来源是各期代码/文档/知识库明确标注过的边界，非新增需求。
> 勾选框方便逐项跟踪；每项标 **effort**（小/中/大）与 **依赖**。

## A. 真安全/正确性缺口（优先）

- [x] **A1 生产侧 LLM token 计量接线** — Session 指标已接通；O1 起由统一观测客户端记录
  成功、失败、取消与缓存 token。模型价格与输入/输出/缓存计价已由R1二期补齐，未知成本不当零，具体边界见末节。
- [x] **A2 post-promote 外部动作执行与恢复** — 合并前持久化动作清单，合并后逐条审批执行，
  执行前后状态及尝试预算落入 RunStore；恢复跳过成功动作，只重试有剩余预算的幂等动作。
  不可重试动作的中断结果记为 `unknown`，需人工核对；失败不回滚已合并代码。
  边界：无外部 exactly-once 保证；跨进程并发恢复仍属 C6；旧 run 未保存的动作无法补回。
- [x] **A3 REPL 交互审批接线** — REPL 已启用 `InteractiveApprovalPolicy`，单 Agent 外部命令
  经用户审批；非交互默认拒绝。

## B. 沙箱与隔离（做真产品必经，重）

- [x] **B4 容器沙箱（Linux 第一期）** — effort: 大，依赖容器运行时。
  第一期后端与 `/task` 接线已完成：Linux rootless Podman；文件工具和命令共用 Worker 容器；
  无宿主 bind mount，输入/输出采用有界快照；冻结、可信导出、销毁之后才回写隔离 Worker。
  失败/取消/不安全快照不发布，独立验收结果作为 promote 门禁。
  WSL2 一期结果仅作历史记录；后续Linux测试只在独立VM。跨进程私有资源账本与崩溃回收已补齐并在独立 Ubuntu VM 实测。干净/含未提交修改的 Git 与非 Git 普通交互已补齐并验收。非 Git `/task` 的快照候选/全局门禁与发布也已验收；第一期完成。后续平台扩展待做；非Git任务恢复与staging回收已补齐，见末节。
- [ ] **B5 网络策略真隔离** — effort: 中，通常与 B4 一起。
  Podman 模式已配置 network=none，导入项目之前校验实际网络接口与权限/资源限额；默认 local
  模式仍无网络隔离。独立 Ubuntu VM 已验证默认离线隔离。第一期受控 HTTPS 下载已实现：
  精确域名、逐跳公网 DNS/IP 固定/TLS、操作者授权、有限下载与审计、导入离线容器后独立验收。
  容器没有开放网络；通用出口策略、在线包管理仍待设计。详见 NETWORK_DOWNLOAD_DESIGN.md。
  沙箱模式不会在宿主执行 post-promote 外部动作，也不会因运行时不可用自动降级。

2026-10-01 验证记录：一期接线时 Windows **356 passed, 39 skipped**，Ruff/Pyright 通过。
之后补齐 Ubuntu WSL2 的 Podman 4.9.3、独立 venv 和固定摘要官方镜像；新增真容器拒绝用例后，
Linux 全量 **395 passed, 1 skipped**，Ruff/Pyright 通过，6 个真容器用例全部通过。
网络不可达、父目录/根目录写入拒绝、实际 cgroup 限额、tmpfs ENOSPC、快照回传与清理实测通过，
最终容器清单为空。唯一跳过是不支持平台拒绝测试，在 Linux 不适用。
追加独立 Ubuntu VM 全量 **415 passed, 1 skipped**，Ruff/Pyright 通过；8 个真容器用例与独立探针通过。
完整证据与复现见 [Linux 沙箱验收记录](LINUX_SANDBOX_ACCEPTANCE.md)。真实模型历史套件本轮未重跑。

## C. 恢复与并发的剩余角

- [x] **C6 跨进程并发恢复（同主机 SQLite 一期）** — 新任务及恢复全程持有同 run 内核租约；
  Windows/Linux 真实进程互斥与强杀接管通过。取消先排空 SQLite 操作，恢复只清理本 run 已记录资源。
  不是分布式锁，不支持不同状态库路径的统一接管；非Git恢复与staging回收已另行补齐。
- [x] **C7 Worker 候选抽取器默认接线** — `EventWorkerHarvester` 已默认接入，
  仅成功 promote（非 Git 为 accept）的 Attempt 由 Supervisor 集中暂存候选。

## D. Benchmark 深化（质量度量）

- [ ] **D8 SWE-bench 实接** — effort: 中–大，依赖环境。
  现仅接入笔记。接 3–5 个 SWE-bench-Lite 实例跑通，拿"真实 PR 修复"的数字（现 19 个项目内任务
  成功率 100% 有偏简单成分，不能外推）。
- [ ] **D9 精确假拒绝（FN）归因** — effort: 中。
  现近似为"期望成功却未达成"，不能纯归因 verifier；需产物级 ground-truth 对照，分清
  "Worker 没做出来" vs "verifier 误拒"。

## E. 调参/精度挂账项（低优先，见 `MindCode_实现设计_V1.md §8`）

- [ ] **E10 启用 `CalibratedTokenEstimator` + 校准启发式系数** — 依赖 A1 先把计量接对。
- [ ] **E11 `soft/hard/target` 比例（0.80/0.92/0.55）按 benchmark 调**（现为照文档抄）。
- [ ] **E12 `ImagePayloadPruner` 接真 vision 描述**（字段已留）。
- [ ] **E13 `_tests_from()` 测试计数可能漏**（影响 `AgentRunResult.tests` 完整性，不影响正确性）。
- [ ] **E14 `TurnIdPartitioner` 换成纯 `turn_id` 驱动**（现对无 `turn_id` 消息启发式补，单 Agent 够用）。

## 建议排序

**A1 / A3 / C7 / A2、O1、R1 第一期已完成**。B4/B5 的 WSL2 Linux 实测已完成，跨进程容器回收已通过独立 VM 验收，普通交互已通过独立 VM 验收，共享工作区普通交互已完成，非 Git `/task` 已完成，历史真实模型场景复验已通过，B5 受控下载一期已完成，C6 同主机 SQLite 一期已完成，非Git持久化恢复与staging回收已完成，R1二期定价/Worker成本阈值路由也已完成，
再按需推进 D8/D9 真实任务评估与内部优化方案后续项。E（调参）依真实会话数据推进；需要多个进程
恢复同一 run 前，必须先完成 C6。

## 优化进度

- [x] **O1 观测导出与归因** — 默认模型调用按角色/模型及 run/Attempt/Step/Worker 关联；
  RunStore 同事务记录 Attempt 状态历史，保留分 Attempt 的 Step 执行结果。
  `/trajectory <mrun_id>` 可重建报告，任务结束自动导出 JSON；支持跨 Session 恢复汇总。
  边界：旧记录/未落盘日志不补造，金额采用显式定价/未知状态，OTel 与日志索引后续按需接入。
- [x] **R1 第一期：角色配置与可控 fallback** — 七类角色独立配置；显式备用模型链，
  仅限流/超时/暂时不可用时切换；Provider 注册、去重、候选数上限与 O1 切换事件已接入。
- [x] **R1二期：显式定价与Worker成本阈值降级** — 同RunStore同步成本，恢复续计、未知保守处理；Verifier保留原模型。
- [x] **R1三期第一部分：模型能力门禁与Worker上下文适配** — 五项显式能力、备用跳过、参数省略、成本组合与轨迹已验收。
- [x] **R1三期第二部分：Map/Reduce窗口与分批适配** — 实际序列化请求预算、完整轮次、顺序Reduce、共享批次上限与原子发布已验收。
- [ ] **R1后续：通用任务特征与完整能力适配** — 硬预算准入、多价阶/完整模型参数协商及真实质量/成本评测待做。

2026-10-02：基线已提交 `645afdc`；普通交互阶段现已提交 `4128098`。独立 VM 全量428 / 1，
十个真容器用例与真实 REPL 冒烟通过；Windows356 / 73，Ruff / Pyright通过。


2026-10-02 共享工作区阶段历史验收（现已提交890bae7）：普通交互支持 dirty Git（保护暂存区/HEAD）与非 Git 目录；写回日志和 SIGKILL
恢复、并发修改拒绝、结果未知处理均通过。独立 VM **454 / 1**，14 个真容器用例；真实 REPL 两类目录
冒烟通过，无残留。Windows **356 / 99**，Ruff/Pyright通过。当时 B4 仍待非 Git `/task`，本轮完成情况见下文。
优化方案：Phase 0 核心完成，Phase 1 O1/R1一期及成本阈值路由完成；提前推进的 Phase 4 已完成
上述隔离范围，网络白名单与真实模型历史回归待做；Phase 2/3 专项尚未推进，同 run 恢复互斥仍属 C6。


2026-10-02 非 Git `/task`：共享交互阶段已提交 `890bae7`；新任务沙箱已完成提交前复验。
独立 VM **475 / 1**，18 个真容器用例，真实 REPL `/task` 与后续普通 send 冒烟通过，无容器残留；
Windows **357 / 119**，Ruff/Pyright通过。DAG可见性、读写过期、整批拒绝/重试、取消排空和快照释放均覆盖。
B4 Linux一期完成，B5 按需网络策略未完成。非 Git resume 暂时明确拒绝，后续需持久化候选与发布回执，
不能直接套 Git HEAD 判定；私有磁盘 staging 的崩溃回收待做。C6同run跨进程互斥仍待做。
历史真实模型11/11与非Git真实模型2/2复验通过；建议下一项据实际需求推进网络白名单。Phase1动态预算路由、Phase2/3未推进。

提交前复验（独立VM，deepseek-flash）：历史11/11、非Git正/负例2/2与后续普通交互通过；
VM重新全量475 / 1，Windows357 / 119，Ruff/Pyright通过，容器无残留。证据见Linux沙箱验收记录末节。


2026-10-02 B5 第一期受控 HTTPS 文件下载（本期改动尚未提交，上一阶段已提交 `6ae0341`）：
独立 Ubuntu VM **562 passed、1 skipped**，27 个真容器用例通过；Windows **435 passed、128 skipped**，两端 Ruff / Pyright 通过，新增 9 个真容器下载用例计入全量，不与全量相加。
真实 HTTPS 公共 wheel 的官方 SHA256 与受信镜像构建 wheel 对齐；容器直接 TCP 仍失败，任务/交互拒绝均不发布。
下载审计卡死的故障注入确认有限等待、不发请求；超时/取消排空可信子进程。
本期用 Stub 固定模型步骤，未重跑真实模型11场景；原仓库与历史现场不变、容器无残留。
B5 保持未勾选：交付的是受控下载代理，非通用联网容器。下一项 C6，再推进非 Git 候选/回执持久化与 staging 回收。


2026-10-02 C6 完成（B5/C6 新改动尚未提交，上一提交 `6ae0341`）：
独立 Ubuntu VM **580 passed、1 skipped**，27 个真容器用例通过；Windows **449 passed、132 skipped**，两端 Ruff/Pyright 通过。VM 恢复专项41项计入全量，C6新增18项，其中真实进程锁/双进程恢复的正常与强杀4项。
Windows全量中4个POSIX路径保护用例跳过；内核互斥、真实进程强杀接管与动作不重复均通过。
取消/重复取消时 SQLite 线程排空，忙碌调用不读取恢复业务状态、不导出轨迹；恢复不删除其他run的资源。
原VM仓库与历史012现场不变，容器无残留；本期未重跑真实LLM套件。边界见 RUN_RECOVERY_LOCKING.md。
下一项：非 Git 候选/发布回执持久化和 staging 崩溃回收；B5通用网络、R1预算与Phase2/3仍待推进。


2026-10-02 非Git持久化恢复与staging回收完成（新改动尚未提交）：

- [x] 原始/最新冻结快照持久化到RunStore；目录身份、策略、限额与内容摘要绑定。
- [x] 未发布Attempt从保存输入/DAG重跑；PROMOTING重试已验收候选；applied只补记、不重复写回。
- [x] 文件交接日志保留到SQL FULL回执事务落盘；无回执的相同内容不能认作成功。
- [x] 非Git私有磁盘资源创建意图与owner租约；只回收记录过且已退出的资源目录。

独立 Ubuntu VM **611 passed、1 skipped**，30 个真容器用例通过；Windows **459 passed、153 skipped**，两端 Ruff/Pyright 通过。新增31项和专项62项包含在全量；3项新增真容器SIGKILL验证
日志前、部分写回后、applied后窗口，恢复不重跑Worker、不重复追加并保留后续人工编辑。
未知/活动目录保留，替换inode/链接/删除失败拒绝，旧run无快照拒绝且记录不变。
原VM仓库、env、Git与历史012保持原样，最终无容器残留；本期没有新真实LLM评测。
旧日期段落保留当时结果，最新进度以本页末节为准；设计见NON_GIT_RECOVERY_DESIGN.md。
下一项：R1定价/动态预算路由；B5通用网络、跨run归档与Phase2/3仍待推进。


2026-10-02 R1二期历史验收（本期现已提交dbae3a7）：

独立 Ubuntu VM **647 passed、1 skipped**，30 个真容器用例通过；Windows **495 passed、153 skipped**，两端 Ruff/Pyright 通过。新增36项和66项专项包含在全量；30个真容器是原能力回归。
金额/角色使用固定Provider，没有新真实LLM质量/成本评测，示例价格仅为测试数据。

- [x] 显式Provider/模型价格，输入/输出/缓存类别独立计价，整数picodollar。
- [x] RunStore同步调用意图、金额/unknown及覆盖起点；并发不丢计量，resume/replan不清零。
- [x] Worker动态成本阈值选择经济模型；工具/窗口不适配保留原模型，验收器不预算降级。
- [x] 轨迹导出已知成本/未知项和切换原因；异步事件不可用不改变同步决策。

初轮装配与测试接口问题已修正；审查补齐事件故障/费用写失败边界后，在新VM目录最终全量复验。
原VM仓库、env、Git和历史012保持原样，无容器残留，未使用WSL。
边界：软阈值不是账单硬上限，普通交互暂不按run阈值降级，1小时缓存写等未识别价格保守unknown。
后续通用任务特征/完整模型能力协商、真实质量成本评测、硬预算准入、B5通用网络及Phase2/3待做。


2026-10-03 R1三期第一部分历史验收（已提交9451e8e）：

独立 Ubuntu VM **685 passed、1 skipped**，30 个真容器用例通过；Windows **533 passed、153 skipped**，两端 Ruff/Pyright 通过。38项新增和104项专项包含在全量。
当前候选按独立目录适配窗口/输出/工具/图片/temperature，Worker准备预算也按有效主模型收缩；
缺元数据/冲突明确拒绝，小备用不适用跳过，经济模型图片/工具/窗口不适用保留主模型；轨迹保存能力快照。
原VM仓库/env/Git与历史现场不变，容器无残留；Linux仅独立VM，没有新真实LLM实验。
五项门禁完成不代表完整能力协商；精确估算、Map/Reduce chunk与Planner/Verifier长输入拆分、任务特征、硬预算仍待做。
设计见MODEL_CAPABILITIES.md；下一项先按压缩模型窗口适配chunk，再推进质量成本实测与Phase2/3。


2026-10-03 R1三期第二部分完成（上一阶段9451e8e，本期已验收并随本次提交归档）：

独立 Ubuntu VM **716 passed、1 skipped**，30 个真容器用例通过；Windows **564 passed、153 skipped**，两端 Ruff/Pyright 通过。新增31项与131项专项均计入全量。
Map真实请求预算与完整turn边界；Reduce滚动有序批次、共享上限、固定公开目标版本、失败保留本次输入和旧检查点。
单turn/单delta/检查点过大不通过删状态或无限重试保证成功；估算仍非供应商精确tokenizer。
两端全量后仅修正新测试的trace整数类型标注，受影响31项及静态复查通过；生产源码未变，前后清单/日志保留。
原VM/env/Git及历史012不变，容器无残留，Linux仅独立VM，无新真实LLM评测。
下一项Planner/Verifier长输入规划；任务特征、硬预算、真实质量/费用评测与Phase2/3待做。
设计见COMPACTION_WINDOW_ADAPTATION.md。
