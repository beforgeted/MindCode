# 模型、上下文与验收预算

本文合并角色路由、价格与能力目录、Map/Reduce 窗口、Planner/Verifier 完整请求和 E10 校准说明，描述当前实现。
测试判据见[模型与上下文测试](testing/model-and-context.md)，最新全量结果见[测试总览](testing/README.md)。

## 角色路由与能力目录

七类角色可分别指定模型：Planner、Worker、LocalVerifier、GlobalVerifier、Judge、CompactMap、CompactReduce。
`CODEAGENT_MODEL_<ROLE>` 覆盖模型名；不配置时沿用调用方配置。Map 仍有独立默认模型，能力目录须覆盖实际使用的所有角色模型。

`CODEAGENT_MODEL_FALLBACK` 为默认显式备用链，`CODEAGENT_MODEL_FALLBACK_<ROLE>` 覆盖它；角色备用链显式为空可禁用默认链。
最多三个备用名称，规范化去重，完整候选链和 Provider 注册在请求前校验。
仅限流、超时或暂时不可用时切换；认证、非法请求、上下文错误、未知错误和取消不会触发切换。

`CODEAGENT_MODEL_CAPABILITIES` 指向本地 JSON，例如以下虚构模型：

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
    }
  }
}
```

五项字段必填，窗口与输出为正整数且输出小于窗口。拒绝重复键、非法布尔、额外字段和缺条目；上限 1 MiB / 256 模型。
能力按 `provider:model` 区分，同名模型跨 Provider 分别声明；配置不热更新。
若价格文件声明同一模型的窗口/输出/工具能力，与目录冲突时拒绝装配。

实际候选取调用方与目录中较小的窗口和输出，不支持 temperature 时省略参数。
工具声明或工具历史要求 tools=true，图片 payload 要求 images=true；已裁掉图片的文本摘要不要求图片能力。
主候选不兼容明确失败；备用不兼容则跳过。路由层不会删除任务、System、图片或工具协议来强行适配。
这不是完整参数协商，尚不涵盖 reasoning、temperature 数值范围、多模态尺寸/MIME、tool_choice 等能力。

## 同步成本与 Worker 软阈值

`CODEAGENT_MODEL_PRICES` 指向由操作者核对的本地价格 JSON，单位为 USD/百万 token。
支持 input、output、cache_read、cache_write，十进制字符串至多六位小数，内部用整数 picodollar 累计。
价格示例只表示格式，不代表供应商现价：

```json
{
  "version": 1,
  "currency": "USD",
  "models": {
    "anthropic:example-main": {
      "input": "3", "output": "15", "cache_read": "0.3", "cache_write": "3.75"
    },
    "anthropic:example-economy": {
      "input": "1", "output": "2",
      "context_window": 32768, "max_output_tokens": 4096, "tools": true
    }
  }
}
```

Provider 适配器须将四类用量归一化为互不重叠的数值。
请求前向同一 runs.db 同步记录成本意图，结束后记录已知金额或未知项；写意图失败阻止调用，结果写失败保留 pending。
成本决策读取同步账本，异步事件只用于观测。旧 run 覆盖缺口、缺价格、失败或不完整用量不能当作零；恢复不清零。

`CODEAGENT_WORKER_COST_THRESHOLD_USD` 与 `CODEAGENT_MODEL_ECONOMY_WORKER` 成对启用。
已知成本达到阈值或存在未知/在途项时，后续 Worker 尝试指定经济模型；能力或窗口不适用时保留主模型。
Planner、Verifier、Judge 和压缩角色保留各自配置，普通交互无 Master scope，不按该任务阈值降级。

软阈值可能因并发、备用链或其他角色超支，不提供账单硬上限。
当前未完整识别一小时缓存写价阶、合同折扣、服务器工具及 SDK 重试费用；计数接口费用也未进入生成金额表。
RunStore 已知成本下界与事件投影可能不同，应保留未知状态。

## Worker 请求预算

ContextManager 的消息容量按实际主模型计算：

`min(Profile 窗口, 有效模型窗口) - 输出上限 - 工具声明估算`。

soft/hard/target 比例作用于消息容量，预测预留适配小窗口。图片裁剪、工具结果降级后才进行边界校准和压缩决策。
主模型上下文准备不因小备用模型提前丢掉更多历史；实际备用/经济候选发送前再次检查完整消息、工具和输出预算。

## Map/Reduce 压缩

Map 在完整 turn 边界规划，计入序列化 JSON、转义、System、Focus、输出和一次 JSON 修复预留。
原历史 chunk 限额与实际窗口都须满足；单 turn 过大时不拆工具 exchange、不截断任务、不发该 Map 请求，按失败 chunk 保留原文。

Reduce 按历史顺序分批，每批用当前临时检查点和剩余 delta 重新估算实际请求。
中间状态仅在内存，全部批次成功且候选通过协议与压缩效果检查后才替换 History，公开版本只增加一次。
后批异常、截断、版本错误、检查点膨胀、超时或预算耗尽均保留原历史；此前已完成的图片裁剪和工具 offload 不因此撤销。

`CODEAGENT_COMPACTION_REDUCE_MAX_BATCHES` 默认 32，正常与应急 Reduce 共享逻辑批次预算。
每批可有限修复并走显式备用链，因此批次上限不是 Provider 调用次数或金额硬上限。
Map 仍使用既有并发限制；压缩取消须排空运行任务。

## Planner 与完整证据验收

Planner 请求保留完整原始任务和修复预留。任务超窗明确停止，不裁剪要求，也不退化为绕过预算的单 Step。
普通无效计划仍可有限修复或退回携带完整任务的单 Step；重规划追加反馈但保留最初任务供最终验收。

LocalVerifier 保留原始 Step 目标，严格解析 JSON 布尔，输出截断不能接受：

| 判定 | 行为 |
|---|---|
| 明确通过 | 允许后续封存与候选集成 |
| 明确未达标 | 有预算时按反馈反思 |
| 缺目标、超限、不可用或格式无效 | 无法判定，不反思、不回传 |

全局证据绑定冻结版本、文件清单和完整块摘要。Git 读取完整 diff，禁用外部 diff/textconv，使用字面路径；非 Git 从原始和冻结快照生成完整 diff。
按完整 hunk 分块，不裁剪大文件尾部或超大单块；空文件、模式、创建和删除也纳入证据。
二进制绑定存在性、大小、模式和完整 SHA256，必须有独立确定性检查通过，模型只判断元数据。

先规划全部批次，每批保留任务、步骤和证据清单，并校验 checked_ids 的完整唯一覆盖；所有批通过后再整体验收任务与跨文件一致性。
证据缺失、超限、编号遗漏/重复、后批失败或结果无法判定均不发布。任一批通过不能替代整体验收。
编号覆盖只证明输入进入检查流程，不证明模型理解或验收命令覆盖充分。

| 配置 | 默认 |
|---|---:|
| `CODEAGENT_VERIFICATION_MAX_BATCHES` | 32，不含最后整体验收 |
| `CODEAGENT_VERIFICATION_TIMEOUT_SECONDS` | 120 秒，仅全局语义阶段 |
| `CODEAGENT_VERIFICATION_MAX_EVIDENCE_BYTES` | 8388608 字节，含块包装 |

证据字节额度不是 token、总内存或账单上限；采集命令有单独超时。

## E10：按模型保守校准

原始启发式与校准倍率分开；`Message.token_estimate` 只缓存原始值，完整工具 JSON 进入估算基数。
会话状态按实际 `provider:model` 与文本/工具/图片协议类别隔离；角色可共享同一模型和协议类别，备用模型使用自己的计数接口与倍率。

请求输入加输出位于窗口 70% 至不足 100% 时限频采样；已知超窗直接拒绝，低于边界通常只同步估算。
同类别采样正在进行时，并发请求等待后重查预算。指纹含完整消息顺序、角色、内容及工具声明，只缓存 SHA256 与整数精确值，不保存载荷。

`guard = max(previous_guard, 1, exact / raw × 1.05)`。

预算倍率只上调；平滑倍率用于观测。低样本不能降低安全余量，这可能增加压缩或拒绝，不能宣称已节省 token。
非法、失败或超时样本保留原估算/已知倍率；不支持接口则该类别只探测一次。取消向上传播并释放采样锁。
计数失败不触发生成备用链；实际生成候选仍独立检查窗口。

| 限制 | 默认 / 配置 |
|---|---|
| 自动校准 | 启用；`CODEAGENT_TOKEN_CALIBRATION=0` 关闭 |
| 单次超时 | 2 秒；`CODEAGENT_TOKEN_COUNT_TIMEOUT_SECONDS` |
| 同类别间隔 | 60 秒；`CODEAGENT_TOKEN_COUNT_INTERVAL_SECONDS` |
| 每类别 / 每会话接口尝试 | 3 / 16；会话上限由 `CODEAGENT_TOKEN_COUNT_MAX_CALLS` 配置 |
| 类别 / 缓存上限 | 64 / 128，`CalibrationConfig` 可配置 |
| 安全余量 | 5%，`CalibrationConfig.safety_ratio` |

缓存 LRU 淘汰，类别状态不淘汰以免重置倍率和额度；新会话或重启重新采样。
限额针对计数接口尝试，不覆盖 SDK 内部 HTTP 重试，也不是费用硬上限。显式 count_exact 可跳过边界比例但保留额度限制，直接底层 Provider 调用不在自动限额中。

## 观测与后续评测

`/trajectory` 关联模型、角色、Attempt、Step 和调用；`capability_routes`、`token_calibrations` 以及压缩/验收 trace 分别记录路由、采样和批次。
报告不复制完整 prompt 或响应正文；事件可能有覆盖缺口，恢复仍读取 RunStore。

目前测试以固定 Provider 验证控制流，真实容器验证门禁，尚无新一轮真实模型质量/费用对照。
下一阶段固定任务基线、Provider、模型和参数，比较关闭/开启校准，记录估算偏差、压缩、产物质量、费用与延迟；未知或不支持项明确标记。
再依据数据调整阈值（E11）。精确采样不保证所有未采样输入都准确，完整参数协商、通用任务特征路由及硬预算仍待实现。
