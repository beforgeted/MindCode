# Skill：声明式专项 Agent

当前一期位于从基础 `main`（`16c669b`）创建的 `dev/skills`，已提交、未合入主线。MCP 一期独立保存在 `dev/mcp-tools` 的 `1b8323a`，本分支未包含 MCP 实现。测试见[工具与能力测试](testing/tools-and-skills.md)。

## 解决的问题与取舍

代码审查与补测试需要不同的工作方法和工具范围。原先每次只能在任务里重新说明，现在可以复用一个静态 AgentDefinition；每次运行仍创建独立 AgentRun，不共享历史、重试次数或工具结果。

一期采用操作者明确指定的本地 JSON 配置，只加载声明式内容，不扫描仓库、不执行插件脚本、不自动安装依赖。这是 MindCode 自己的 Skill 格式；不等同于桌面应用的 SKILL.md 格式或通用第三方技能标准。

## 配置与使用

将以下示例保存为本地 `.codeagent/skills.json`，通过 `CODEAGENT_SKILLS_CONFIG` 指定路径。该目录已忽略，不提交个人配置。

```json
{
  "version": 1,
  "allowed_tools": ["read_file", "grep", "write_file", "run_command", "read_artifact"],
  "skills": [
    {
      "id": "review",
      "name": "代码审查",
      "description": "阅读源码，定位缺陷并给出来源；不修改文件。",
      "instructions": "先读取相关代码和调用方，再检查边界条件。给出文件位置、触发条件和影响；证据不足时说明未知。",
      "tools": ["read_file", "grep", "read_artifact"],
      "max_react_iterations": 12
    },
    {
      "id": "tests",
      "name": "补测试",
      "description": "补充覆盖实际失败条件的测试并运行验证。",
      "instructions": "先理解实现和现有测试，选择能暴露缺陷的输入，补充测试，再执行相关测试；报告实际运行结果。",
      "tools": ["read_file", "grep", "write_file", "run_command", "read_artifact"]
    }
  ]
}
```

```powershell
$env:CODEAGENT_SKILLS_CONFIG = ".codeagent/skills.json"
python -m codeagent.cli.app --workspace .
```

`/skill list` 查看已加载能力，`/skill review` 切换，`/skill default` 回到普通 Agent。切换仅允许在空闲且未关闭的会话进行；新建运行上下文，保留原始事件和 Durable Memory。可用顶层 `"active": "review"` 指定初始能力；不设置时启动普通 Agent。

`/task` 的 Planner 获得能力名称和有界描述，通过 `agent_id: "skill.review"` 或 `"skill.tests"` 选择 Worker。当前单会话选择不强制所有 `/task` Worker 使用同一能力；Planner 仍能选择目录中的其他能力与 default。

配置读取后冻结在 AppConfig 中，模型修改配置文件不会改变正在运行的会话；重启并重新加载才能应用修改。文件缺失或格式错误显式拒绝启动，避免悄悄以更大权限继续。移除环境变量并新建会话即可关闭功能。

## 权限为何要在执行时复核

有效工具范围为 **Skill 声明 ∩ 操作者上限 ∩ 基础 Agent 上限 ∩ 实际注册工具**。声明未知或未启用的工具不会注册新工具。

旧 AgentDefinition 的空工具列表表示“全部注册工具”，为兼容既有调用保留这一含义。Skill 编译为 `tools_restricted=True`，空交集表示“禁止全部工具”。ReActEngine 仅向模型提供有效声明，ToolExecutionManager 再拒绝模型伪造的越权调用；失败和成功调用仍各自得到对应 tool_result，保留批次协议完整性。

允许 run_command 表示可以请求命令执行，命令仍经过原有 CommandPolicy、外部副作用审批与沙箱约束；这不能被当作只读能力。Skill 文字无法扩大工具集合、迭代次数、模型窗口、全局费用设置或发布门禁。它仍可能影响模型回答，权限约束不等于已经证明提示词效果。

模型配置继承操作者 Worker 路由、能力适配、备用链和费用策略；一期不支持每个 Skill 自带任意模型或预算。模型不支持工具时，现有能力门禁会在调用 Provider 前停止。

## 失败与运行边界

配置最多 64KiB、16 个 Skill；名称、描述、工作方法分别最多 80、500、8000 字符，工具列表最多 64 个。id 使用小写字母开头的字母、数字、下划线或连字符，最多 48 字符，不能为 CLI 保留名称 list 或 default。重复 JSON 键、重复 id、未知字段、非法类型、未知初始选择拒绝加载。迭代数只能为 1–25，且最终不超过基础 Agent 的上限。

Planner 选择未知名称会走现有一次协议修复；仍失败则保留原任务并交给受操作者权限上限约束的 default。上下文预算错误停止任务，不截断后回退。自定义或恢复计划里的未知 Agent 在调度层产生失败 Worker，不启动执行域、不继承普通 Agent 的权限；失败可在任务结果中观察。

启用 Skill 的 Worker 沿用原有容器、独立验收和事务发布流程。单次工具错误可以被模型处理，不必自动判整个任务失败；最终能否发布仍由独立验收决定。skill. 命名空间始终保留，关闭功能或删除配置后，旧图中的 Skill 名称同样显式失败；不能退回权限更大的普通 Agent。其它旧 Agent 名称在功能关闭时保留原来的回退语义，执行前工具白名单复核始终生效。

跨进程恢复使用本次由操作者加载的配置；一期未持久化 Skill 配置版本以重放旧工作方法。删除能力后安全拒绝是已验证边界，完全重现旧定义仍待后续版本管理。

配置来自操作者明确授权的本地文件，第一期没有第三方下载、签名与动态热更新。没有新增 Knowledge 索引，也没有运行真实模型消融；实现通过只能证明加载、选择、权限和执行门禁按设计工作，不能证明成功率提高。
