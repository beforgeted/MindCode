# MindCode 收尾清单（Backlog）

> 测试环境约束：后续 Linux 测试仅在独立 Ubuntu VM `mengx@192.168.100.128` 运行，禁止在 WSL Ubuntu 跑测试；已有 WSL 结果仅保留为历史记录。

> P0–P9 已落地（能力 → 失败安全 → 崩溃恢复 → 可量化基线）。本表记录剩余增强项，
> 按"该不该做、多急、多大"分档。来源是各期代码/文档/知识库明确标注过的边界，非新增需求。
> 勾选框方便逐项跟踪；每项标 **effort**（小/中/大）与 **依赖**。

## A. 真安全/正确性缺口（优先）

- [x] **A1 生产侧 LLM token 计量接线** — Session 指标已接通；O1 起由统一观测客户端记录
  成功、失败、取消与缓存 token。金额成本的模型价格/缓存计费规则仍待补，不能将 token 当费用。
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
  WSL2 一期结果仅作历史记录；后续Linux测试只在独立VM。跨进程私有资源账本与崩溃回收已补齐并在独立 Ubuntu VM 实测。干净/含未提交修改的 Git 与非 Git 普通交互已补齐并验收。非 Git `/task` 的快照候选/全局门禁与发布也已验收；第一期完成。后续平台扩展、非 Git 任务恢复与 staging 崩溃回收仍需专项实现。
- [ ] **B5 网络策略真隔离** — effort: 中，通常与 B4 一起。
  Podman 模式已配置 network=none，导入项目之前校验实际网络接口与权限/资源限额；默认 local
  模式仍无网络隔离。离线策略已在 WSL2 Linux 实测；按需网络白名单待设计。
  沙箱模式不会在宿主执行 post-promote 外部动作，也不会因运行时不可用自动降级。

2026-10-01 验证记录：一期接线时 Windows **356 passed, 39 skipped**，Ruff/Pyright 通过。
之后补齐 Ubuntu WSL2 的 Podman 4.9.3、独立 venv 和固定摘要官方镜像；新增真容器拒绝用例后，
Linux 全量 **395 passed, 1 skipped**，Ruff/Pyright 通过，6 个真容器用例全部通过。
网络不可达、父目录/根目录写入拒绝、实际 cgroup 限额、tmpfs ENOSPC、快照回传与清理实测通过，
最终容器清单为空。唯一跳过是不支持平台拒绝测试，在 Linux 不适用。
追加独立 Ubuntu VM 全量 **415 passed, 1 skipped**，Ruff/Pyright 通过；8 个真容器用例与独立探针通过。
完整证据与复现见 [Linux 沙箱验收记录](LINUX_SANDBOX_ACCEPTANCE.md)。真实模型历史套件本轮未重跑。

## C. 恢复与并发的剩余角

- [ ] **C6 跨进程并发恢复** — effort: 中。
  P8 是单机崩溃重开；两个进程同时 resume 同一 run 未加 run 级锁。单机单进程使用可不做，标注即可。
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

**A1 / A3 / C7 / A2、O1、R1 第一期已完成**。B4/B5 的 WSL2 Linux 实测已完成，跨进程容器回收已通过独立 VM 验收，普通交互已通过独立 VM 验收，共享工作区普通交互已完成，非 Git `/task` 已完成，历史真实模型场景复验已通过，下一步按需推进网络策略，
再按需推进 D8/D9 真实任务评估与内部优化方案后续项。E（调参）依真实会话数据推进；需要多个进程
恢复同一 run 前，必须先完成 C6。

## 优化进度

- [x] **O1 观测导出与归因** — 默认模型调用按角色/模型及 run/Attempt/Step/Worker 关联；
  RunStore 同事务记录 Attempt 状态历史，保留分 Attempt 的 Step 执行结果。
  `/trajectory <mrun_id>` 可重建报告，任务结束自动导出 JSON；支持跨 Session 恢复汇总。
  边界：旧记录/未落盘日志不补造，金额成本暂缺定价，OTel 与日志索引后续按需接入。
- [x] **R1 第一期：角色配置与可控 fallback** — 七类角色独立配置；显式备用模型链，
  仅限流/超时/暂时不可用时切换；Provider 注册、去重、候选数上限与 O1 切换事件已接入。
- [ ] **R1 后续：预算与任务特征路由** — 金额预算降级需先补模型定价，模型能力/上下文窗口适配待做。

2026-10-02：基线已提交 `645afdc`；普通交互阶段现已提交 `4128098`。独立 VM 全量428 / 1，
十个真容器用例与真实 REPL 冒烟通过；Windows356 / 73，Ruff / Pyright通过。


2026-10-02 共享工作区阶段历史验收（现已提交890bae7）：普通交互支持 dirty Git（保护暂存区/HEAD）与非 Git 目录；写回日志和 SIGKILL
恢复、并发修改拒绝、结果未知处理均通过。独立 VM **454 / 1**，14 个真容器用例；真实 REPL 两类目录
冒烟通过，无残留。Windows **356 / 99**，Ruff/Pyright通过。当时 B4 仍待非 Git `/task`，本轮完成情况见下文。
优化方案：Phase 0 核心完成，Phase 1 O1/R1一期完成、动态预算路由待做；提前推进的 Phase 4 已完成
上述隔离范围，网络白名单与真实模型历史回归待做；Phase 2/3 专项尚未推进，同 run 恢复互斥仍属 C6。


2026-10-02 非 Git `/task`：共享交互阶段已提交 `890bae7`；新任务沙箱已完成提交前复验。
独立 VM **475 / 1**，18 个真容器用例，真实 REPL `/task` 与后续普通 send 冒烟通过，无容器残留；
Windows **357 / 119**，Ruff/Pyright通过。DAG可见性、读写过期、整批拒绝/重试、取消排空和快照释放均覆盖。
B4 Linux一期完成，B5 按需网络策略未完成。非 Git resume 暂时明确拒绝，后续需持久化候选与发布回执，
不能直接套 Git HEAD 判定；私有磁盘 staging 的崩溃回收待做。C6同run跨进程互斥仍待做。
历史真实模型11/11与非Git真实模型2/2复验通过；建议下一项据实际需求推进网络白名单。Phase1动态预算路由、Phase2/3未推进。

提交前复验（独立VM，deepseek-flash）：历史11/11、非Git正/负例2/2与后续普通交互通过；
VM重新全量475 / 1，Windows357 / 119，Ruff/Pyright通过，容器无残留。证据见Linux沙箱验收记录末节。
