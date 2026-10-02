# R1 二期：显式定价与 Worker 成本阈值路由

上一阶段已提交92c88d1。本期实现USD token价格、RunStore同步成本记账及Worker阈值降级；
默认未配置时保留原有路由。当前不提供账单硬上限或通用模型能力协商。

## 配置

CODEAGENT_MODEL_PRICES指向本地JSON。CODEAGENT_WORKER_COST_THRESHOLD_USD配置单run已记录成本阈值，
CODEAGENT_MODEL_ECONOMY_WORKER配置显式provider:model。两者一起配置，阈值必须为正；
仅配置价格也可独立计价，不启用降级。

以下为测试用虚构价格和能力，不能当作供应商现价：

~~~json
{
  "version": 1,
  "currency": "USD",
  "models": {
    "anthropic:main": {
      "input": "3",
      "output": "15",
      "cache_read": "0.3",
      "cache_write": "3.75"
    },
    "anthropic:economy": {
      "input": "1",
      "output": "2",
      "cache_read": "0.1",
      "cache_write": "1.25",
      "context_window": 32768,
      "max_output_tokens": 4096,
      "tools": true
    }
  }
}
~~~

价格单位是USD/百万token，金额使用十进制字符串，最多六位小数；内部按整数picodollar计算，
不使用浮点累加。模型键必须含Provider前缀，缓存价格可缺省，但对应类别实际有token时成本为未知。
文件上限1MiB/256个模型，重复JSON键、非法金额/能力声明拒绝，不自动从网站猜价格。
操作者须根据实际端点、模型、服务层级和缓存策略核对价格；配置不热更新，已有成本不按新价格重算。

## 用量与记账

公式：input*输入单价 + output*输出单价 + cache_read*缓存读单价 + cache_write*缓存写单价，
各项除以百万。统一Usage约定四类token互不重叠。
Anthropic输入token不含缓存读写，参考其[官方缓存用量说明](https://platform.claude.com/docs/en/build-with-claude/prompt-caching)。
其他Provider适配器须先归一化用量，并显式标记LlmResponse.usage_complete；
未确认用量、错误、取消、缺价格、缓存价格缺失或非法token数均不能当成零成本。
本期没有拆分1小时缓存写价格；Anthropic报告1小时缓存写时保守标记用量不完整，金额未知。
账单层级、批处理折扣、地区费率、服务器工具费用和SDK内部重试费用不由此自动识别。

价格启用时，每个Master调用在Provider请求前先同步INSERT llm_cost意图到同一个runs.db；
成功/错误/取消后同步记录成本或未知。异步LLM_CALL事件负责观测，不作为预算决策来源。
落盘失败阻止新请求；请求已返回但结果记账失败时保留pending，不再将它当成未发生。
每个run的成本覆盖起点在规划前初始化；旧run没有历史成本覆盖时保留unknown缺口，恢复不能重置为零。

| 数据 | 职责 |
|---|---|
| RunStore llm_cost / llm_cost_origin | 调用意图、已知成本、未知调用、历史覆盖；预算读取的事实 |
| cost_scope | 显式关联当前Master run ID；不保存计数或余额 |
| LLM_CALL / MODEL_BUDGET_ROUTE事件 | 模型、角色、实际价格与切换原因的观测副本 |
| trajectory durable_cost | 从RunStore重建的已知成本下界和未知调用 |
| trajectory totals/by_role/by_model | 已落盘事件的计价投影，可能因日志缺失少于同步账本 |

成本是按配置估计的token金额，不是供应商已出账单。未知调用数可以包含旧历史覆盖缺口，
不能把它理解为精确的供应商请求次数。新配置只保证默认SQLite同路径装配；
定制RunStore若与配置路径不同或不是SQLite，则拒绝启用金额接线。

## 路由

每次Worker调用前读取本run同步账本。已知成本达到阈值，或存在未知/在途调用时，
尝试使用显式经济模型；Planner、LocalVerifier、GlobalVerifier、Judge与压缩角色保留原有模型。
不同run的成本互不混用，resume及replan不清零。
普通交互没有Master cost_scope，因此本期不会按任务阈值降级，但可在观测中计价。

经济模型必须声明上下文窗口、最大输出与工具能力。请求需要工具而模型不支持，或现有启发式估算的
输入加输出预算装不下时，保留原Worker模型并记录原因。使用全系统TokenEstimator，不在热路径调用
Provider精确计数。适用时截取最大输出到声明上限，保留温度等其余参数。
元数据是操作者可信配置，估算不是供应商精确计数，不代表已自动协商所有模型功能。

~~~mermaid
flowchart TD
    A[显式Master run关联与Worker调用] --> B[读取RunStore已知成本与未知项]
    B --> C{达到阈值或有未知项?}
    C -- 否 --> D[原角色模型与fallback]
    C -- 是 --> E{经济模型支持工具且上下文够用?}
    E -- 否 --> F[保留原模型; 记录不适用原因]
    E -- 是 --> G[经济Worker; 输出限额适配; 原显式fallback]
    D --> H[同步成本意图后请求Provider]
    F --> H
    G --> H
    H --> I[同步记已知成本或unknown; 异步观测]
~~~

阈值是软降级规则：并发在途调用可能已启动，验收器不降级，不适用的经济模型会保留原模型，
显式fallback仍可选择其他价位，所以实际金额可能超过阈值。它不阻止后续请求，也不提供跨账号额度限制。
硬上限需要请求前保守预留、原子准入、账单不确定性策略等另一套设计，当前不宣称完成。
配置指定的“经济”模型应由操作者核对价格与质量，本期不从名称推断便宜，也不自动寻找最低价模型。

## 验收

新增用例覆盖精确输入/输出/缓存计价、非法配置和缺价格、并发和未知在途记录、
恢复续计、旧历史缺口、错误fallback、取消、写意图失败阻止请求、工具/上下文拒绝、
端到端Master中的Planner计费、仅Worker降级及trajectory与同步账本对齐。
使用固定Provider模拟金额与角色，不新增真实LLM质量或成本节省实验；既有真实Podman/HTTPS能力单独回归。
最终平台结果见LINUX_SANDBOX_ACCEPTANCE.md末节。Linux只在独立Ubuntu VM验收，未用WSL。

