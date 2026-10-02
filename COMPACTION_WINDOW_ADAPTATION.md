# 历史压缩模型窗口适配

本期补齐 R1 三期的压缩侧适配。Worker 窗口适配和调用门禁已由 `9451e8e` 提交；本期让 Map/Reduce 在调用前形成能装入主候选窗口的请求。

## Map：按实际请求规划完整轮次

原先 `HistoryChunker` 只按历史消息估算与 `map_chunk_tokens` 分块。但历史会再转为 JSON，加入字段、转义、System 提示词、Focus 和 token 数说明；原始历史装得下，不代表最终请求装得下。

现在每次尝试加入完整 turn 时，同时满足两个条件：

- 原历史估算不超过配置的 `map_chunk_tokens`；单个原始超大 turn 仍独立成块。
- 实际序列化请求估算 + 一条 JSON 修复提示预留 + 有效输出上限，严格小于有效模型窗口。

多个 turn 放不下则在完整 turn 边界分块。单 turn 无法满足请求窗口时不拆工具 exchange、不截取用户文本、不发送该 Map 请求，按失败 chunk 保留原文。其它 chunk 可继续；最终候选仍必须释放 token 并通过 hard limit 和工具协议校验。

请求使用现有 TokenEstimator，加入相同的 Map System/Focus/JSON；输出按角色主候选的真实能力上限收缩。Map 的显式备用模型仍通过原路由重新检查，不为了备用模型提前损失更多历史。

## Reduce：顺序分批与一次发布

根据实际 `existing_checkpoint + deltas + Focus + System + 输出 + 修复预留`，选出能容纳的有序 delta 前缀，发送一次 Reduce。响应作为下一批的临时检查点，重新计算剩余请求预算，每批至少消费一个 delta。

所有批次按历史顺序处理；Map 可以并发完成，但 `gather` 保持输入顺序，分批不改变较新文件/测试状态的覆盖方向。Reducer 提示词保留原有约束、决策、失败与证据，传入前批完整检查点，不在程序中删除字段以适配窗口。

整次压缩公开目标版本固定为 `旧版本 + 1`，或首次为 1。中间批也必须返回这个目标版本，而不是每批递增公开版本。临时结果没有调用 History.apply_compaction；所有批完成、候选通过校验后，才由 ContextManager 原子替换历史、检查点并增加一次 compaction_count。

单个 delta、旧检查点、固定提示词或中间检查点增长到不能继续时明确失败。后批 JSON/schema/版本错误、输出截断、超时或预算耗尽也不发布中间结果；保留本次压缩输入和旧检查点。正常与应急更严格 Reduce 都从相同旧检查点/原 delta 重新处理，并共享批次预算。

失败回退的是传给压缩器的输入；ContextManager 之前完成的图片裁剪、工具输出 offload 不因此撤销。旧原始输出仍可通过已有证据/Artifact 访问。

## 配置与有限调用

`ContextProfile.compaction_reduce_max_batches` 默认 32，也可通过 `CODEAGENT_COMPACTION_REDUCE_MAX_BATCHES` 配置。必须是正整数，压缩前拒绝非正批次限制。

上限计算的是逻辑 Reduce 批次：正常合并和应急重试共享一个计数。每批至多两次 JSON 尝试，每个实际请求仍可能走最多四个显式路由候选；这不是 Provider 请求数、token 或金额的硬上限。全流程受原 `compaction_timeout_seconds` 限制，Map 仍受 `compaction_map_concurrency` 控制。

规划时预留一次修复提示；`_call_json` 每次发送前还检查实际初始/修复请求。修复不能越过窗口门禁。生产装配经 RoleLlmClient/ObservedLlmClient 同步查询主候选参数，保留 Provider 前缀，不新增精确计数网络请求或额外费用意图。

## 观测、验收与限制

Map/Reduce 的 LLM 调用共享 `compaction_id`，trace 包含 Map chunk 编号/token 估算，以及 Reduce 批号、delta 数与目标版本；继承既有 Session/run/角色关联。已有 trajectory 的 llm_calls 可直接读取这些字段。指标 `context.compaction.reduce_batches` 记录本次尝试过的批次数，成功结果说明实际批数。

本期验证使用固定 Provider 控制响应，涵盖完整工具轮次、序列化转义、窗口收缩、分批顺序、已有版本延续、后批异常/截断、预算耗尽、检查点膨胀、修复预留、外部取消和并发排空、真实 Session 轨迹装配。平台全量及真实容器证据在 [Linux 验收记录](LINUX_SANDBOX_ACCEPTANCE.md) 末节，Windows 与独立 Ubuntu VM 分别记录；禁止 WSL。

这些测试证明请求规划、状态交接和失败回退，不证明真实模型语义无损或真实成本节省。启发式估算仍不是供应商精确 tokenizer，不覆盖所有内部协议开销；超长原子 turn 或不断增长的事实集合仍可能无法压缩。不会通过丢状态或无限层级重试保证成功。完整模型参数协商、Planner/Verifier 长输入重建和真实质量/费用实验仍待推进。
