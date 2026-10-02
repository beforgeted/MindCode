# 模型能力与上下文适配（R1 三期第一部分）

本期处理“换了模型名，却仍沿用主模型的窗口、输出和参数”的问题。角色配置、备用链及成本阈值决定候选模型；能力目录判断这些候选是否能接收当前请求。目录不决定任务质量，也不自动选择新的 Provider。

## 配置

设置 `CODEAGENT_MODEL_CAPABILITIES` 为本地 JSON 文件路径。未设置时兼容原行为；显式启用后，当前调用候选链及已配置经济模型必须有准确的 `provider:model` 条目，缺失即配置错误，当前 chat 在任何 Provider 请求前失败。普通会话和 Master 装配均接入；包装已配置的 RoutingLlmClient 会保留其能力目录。

下面是虚构模型和测试配置，数值不代表供应商现价或官方能力。Provider 必须另外注册。

```json
{
  "version": 1,
  "models": {
    "anthropic:example-main": {
      "context_window": 64000,
      "max_output_tokens": 8000,
      "tools": true,
      "images": true,
      "temperature": true
    },
    "anthropic:example-small": {
      "context_window": 16000,
      "max_output_tokens": 4000,
      "tools": true,
      "images": false,
      "temperature": false
    }
  }
}
```

五个字段均必填。窗口/输出为正整数，输出小于窗口，布尔值不能用字符串或数字代替。文件限 1 MiB、256 模型、严格 version 1，重复键、空 models 和额外字段均拒绝。加载后复制为只读映射，不热更新。操作者负责根据实际端点验证能力；同名模型在不同 Provider 下分别声明。

目录要覆盖所有实际使用的角色，包括 Planner、Worker、两层 Verifier、Judge 和压缩 Map/Reduce。未覆盖的后续角色会在调用时失败；本期没有在启动时穷举所有未来请求。默认 `map_model` 也可能产生不同模型名。模型定价独立配置；同一模型在价格文件中已有窗口/输出/工具字段时必须与目录一致，冲突拒绝装配。

## 调用规则

1. 校验完整候选链、Provider 注册和能力元数据；去重及最多四候选沿用原策略。
2. 每个候选的窗口、输出均取调用配置与目录的较小值；不支持 temperature 的候选用 `None`，Anthropic 适配层完全省略该参数。
3. 工具声明或历史 tool_use/tool_result 要求 tools=true；实际图片 payload 要求 images=true。已裁掉 payload 的图片摘要可以作为文本传给不支持图片的模型，不会在路由层主动删除图片或工具。
4. 用现有 HeuristicTokenEstimator 估算消息，并计入工具声明与输出预留。达到或超过窗口视为不适用，不通过缩小用户消息或删除 System 来强行适配。
5. 主候选不适用即报错；备用候选不适用则跳过。主请求仅遇限流、超时或暂时不可用时，才尝试剩余的显式备用候选。认证、非法请求、上下文错误、未知错误和取消仍不切换。
6. 成本阈值触发时，经济模型也要满足图片、工具和窗口要求；不适用则保留主模型，记录原因。切换不扩大操作者原有窗口/输出限制。软成本阈值仍可能超支。

跳过的候选不调用 Provider，也不会产生成本请求意图；被实际调用的候选仍通过原 ObservedLlmClient 计量。目录与费用存储互不替代。

`tools=true` 只表示模型能接收工具协议，不授予工具权限；Agent 的 allowed_tools、执行审批和 Podman 隔离继续决定工具能做什么。

## 上下文准备

Worker 每轮准备历史前，按主候选的有效窗口计算：

`消息容量 = min(ContextProfile 窗口, 有效模型窗口) - 输出上限 - 工具声明估算`

消息容量送入 ContextManager 的 soft/hard/target 压缩阈值。output_reserve 不超过有效输出上限，预测用 safety_margin/expected_tool_burst 不超过消息容量的 10%，避免小窗口仍使用原大窗口的固定预测预留。原不可变定义与 Profile 不被修改，热路径没有新增精确计数网络请求。

准备主候选上下文，不为了小备用模型预先压掉更多历史。备用及经济候选再次对最终消息做适用性检查，容量不足则跳过/保留主模型。压缩 Map/Reduce、Planner 和 Verifier 的实际请求也经过能力门禁，但本期未按各角色窗口自动重分 Map chunk、拆 Planner 输入或重建验收请求；超限仍明确失败。

启发式估算不是供应商精确 tokenizer，也不含所有端点内部协议开销，不能承诺永不触发 Provider 上下文拒绝。精确计数校准、协议开销校准及长输入切分属于后续工作。

## 观测和边界

`model_capability_route` 记录选择/跳过/拒绝原因、有效窗口/输出/temperature、能力快照和现有角色/run/Step trace；trajectory 增加 `capability_routes`。事件写失败只计观测故障，不更改路由或放行不兼容请求。

本期实现五项显式能力适配；未实现 reasoning 参数、temperature 数值范围、多模态 MIME/尺寸、tool_choice、流式输出、完整参数协商、自动发现、硬预算准入或通用任务难度路由。没有新增付费模型质量/成本评测。

验证结果与独立 Ubuntu VM 证据见 [Linux 验收记录](LINUX_SANDBOX_ACCEPTANCE.md) 末节；Windows 与 VM 分别记录，禁止用 WSL 结果替代。
