# MindCode 收尾清单（Backlog）

> P0–P9 已落地（能力 → 失败安全 → 崩溃恢复 → 可量化基线）。本表记录剩余增强项，
> 按"该不该做、多急、多大"分档。来源是各期代码/文档/知识库明确标注过的边界，非新增需求。
> 勾选框方便逐项跟踪；每项标 **effort**（小/中/大）与 **依赖**。

## A. 真安全/正确性缺口（优先）

- [ ] **A1 生产侧 LLM token/成本计量接线** — effort: 小。
  `AnthropicLlmClient` 用私有 `Metrics()`，`AgentSession` 另建一个 → REPL `/metrics`、真实会话的
  token/成本**至今显示 0**。P9 只在 benchmark 观测层 rebind 打了补丁。正解：session 把 `self.metrics`
  注入 client（改 `AgentSession` 构造 + `cli._build_client`）。**解锁 A10 校准与 D8 成本度量。**
- [ ] **A2 post-promote 外部动作真正执行** — effort: 中。
  P7e 只到"拦下 + 记 `DeferredAction` + 上报"。补：验收通过后经 `ApprovalPolicy` 逐条执行、标注不可
  重试；想清楚"执行失败但 base 已 promote"如何如实告知。
- [ ] **A3 REPL 交互审批接线** — effort: 小。
  `InteractiveApprovalPolicy` 已写但没挂进活 REPL；单 Agent 会话也是 `allow_external_effects=False`，
  交互下外部命令永远被拦/延后、不问用户。给 REPL 接交互审批 + 让单 Agent 会话可选放行 external。

## B. 沙箱与隔离（做真产品必经，重）

- [ ] **B4 容器沙箱** — effort: 大，依赖容器运行时。
  `SandboxExecutor` 目前是 `NotImplementedError` 占位；`run_command` 仍在宿主跑模型给的 shell
  （P7 有命令策略/进程树终止/env 过滤，但无真隔离）。排在最后、且先定目标环境。
- [ ] **B5 网络策略真隔离** — effort: 中，通常与 B4 一起。
  现仅"标记 + 审计"，未真拦网络。

## C. 恢复与并发的剩余角

- [ ] **C6 跨进程并发恢复** — effort: 中。
  P8 是单机崩溃重开；两个进程同时 resume 同一 run 未加 run 级锁。单机单进程使用可不做，标注即可。
- [ ] **C7 Worker 候选抽取器默认接线** — effort: 小–中。
  `AgentRuntime.candidate_harvester` 可注入但默认不注入 → Worker 产出的记忆候选目前不进共享 Memory 链路。

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

先做 **A1**（便宜、且是 E10/D8 的前置——没有可信 token 数，校准和成本度量都是空的），顺带 **A3**
让 P7 的审批能力在交互下真正可用。之后按需 **A2 → C7 → D8/D9**。B（沙箱）与 E（调参）留到有明确
部署目标或真实会话数据时再动。
