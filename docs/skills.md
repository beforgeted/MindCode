# Skill：声明式专项 Agent

当前 `main` 已包含声明式 Skill 与社区包接入，需显式安装依赖和配置，默认普通会话不加载 Skill。发布源码验收检查点为 `e3f87c8`；测试见[工具与能力测试](testing/tools-and-skills.md)。

## 解决的问题与取舍

代码审查与补测试需要不同的工作方法和工具范围。原先每次只能在任务里重新说明，现在可以复用一个静态 AgentDefinition；每次运行仍创建独立 AgentRun，不共享历史、重试次数或工具结果。

操作者通过本地 JSON 显式启用能力。原有内联声明继续兼容，新增 `packages` 可直接加载 Agent Skills 的 `SKILL.md` 目录；不扫描仓库自动授权，也不运行安装钩子。第三方工作方法可以影响模型行为，但不能扩大运行时工具权限。

## 使用已有社区 Skill

安装可选依赖后，选择已下载仓库中的一个完整 Skill 目录。以下使用真实社区 `code-reviewer` 包；社区仓库、模板和嵌套样例的统计口径见测试文档。

```bash
python -m pip install -e ".[dev,ecosystem]"
python -m codeagent.skills inspect <下载目录>/AgentSkills/code-reviewer
python -m codeagent.skills install <下载目录>/AgentSkills/code-reviewer --root .codeagent/skills
```

安装会保留 `SKILL.md`、scripts、references、assets、examples 及其它配套文件的原始字节；返回内容摘要。已有目标目录拒绝覆盖，新版本安装到新的根目录；没有自动升级或依赖安装。Git 下载和包版本选择由操作者完成，第三方源码与私有配置放在忽略目录中。

在 `.codeagent/skills.json` 中加入：

```json
{
  "version": 1,
  "active": "review",
  "packages": [{
    "id": "review",
    "path": "skills/code-reviewer",
    "sha256": "<inspect/install 返回的内容摘要>",
    "tools": ["read_file", "grep", "skill_review_resource"]
  }]
}
```

相对包路径以配置文件所在目录为基准；`sha256` 可固定包版本，摘要不符拒绝加载。`tools` 是操作者实际授予的工具集合，包内 `allowed-tools` 仅保留为元数据，不能自行授权。配套读取工具只有被显式列入集合才注册；允许后仍与基础 Agent、操作者总上限取交集。

模型可用 `skill_review_resource` 读取 `references/...` 或 `scripts/...`，按行分页读取冻结文本。包的全部内容在加载时冻结，修改磁盘上的文件不影响当前会话。二进制资源保留但暂不支持模型读取。目录保留和文本读取通过不等于脚本依赖、跨 Skill 引用或外部应用已经可运行。

标准包使用 YAML 安全解析，拒绝重复键、别名、对象标签、未知标准字段和非法元数据。标准名称遵循固定官方参考实现的 Unicode 规范化与目录匹配；运行时 id 仍由操作者指定，使用下面的 ASCII 规则。标准描述最多 1024 字符，`SKILL.md` 最多 64KiB；每个包最多 1000 文件、单文件 1MiB、总计 10MiB。允许普通隐藏配套文件，拒绝凭据/私有目录、链接、reparse point 和特殊文件。读取资源时只接受冻结清单中的精确路径，不能越出包。

## 显式运行社区 Python 脚本

操作者审查脚本后，既要授权入口文件，也要将执行工具加入 `tools`。以下使用固定社区 `code-tour` 的真实验证器：

```json
{
  "version": 1,
  "active": "tour",
  "packages": [{
    "id": "tour",
    "path": "skills/code-tour",
    "sha256": "<已核对的包摘要>",
    "scripts": ["scripts/validate_tour.py"],
    "tools": ["skill_tour_resource", "skill_tour_script"]
  }]
}
```

启用 Podman 后，模型可调用 `skill_tour_script`，参数为 `{"script":"scripts/validate_tour.py","args":["guide.tour","--repo-root","/workspace"]}`。只接受最多 16 个冻结清单中的 `scripts/*.py` 精确入口；模型不能选择解释器、传入源码或启动另一个未授权入口。参数为最多 64 个字符串，每个最多 4096 字符，不能包含 NUL。缺少任一授权时不会获得脚本执行能力；包内文字或 `allowed-tools` 无法补授权。

完整冻结包在当前容器 `/tmp` 的独立临时目录展开，配套路径保持不变；使用镜像中的 Python `-I` 启动，cwd 为 `/workspace`，stdin 关闭，参数直接作为 argv 数据传入。工作区同名模块不能替换启动脚本或标准库。脚本可按 `__file__` 定位配套资源；不会自动将配套目录加入 Python 搜索路径。依赖必须事先放入受信镜像，不执行安装钩子或在线装包；当前入口仅支持 Python。

脚本可以修改当前候选，所以执行工具保守标为 `workspace_write`、`retry=never`，串行执行；不能把验证器的只读用途推广为任意脚本只读。操作者的文件白名单是启动授权，脚本内部不是逐命令 CommandPolicy 审批；它受到当前 Podman 域的断网、无宿主挂载和资源限制。授权一个脚本意味着授权其代码在该域内运行，文件白名单不限制脚本内部调用其它镜像程序。

`retry=never` 对社区能力有实际门禁：当前 Agent 的有效工具范围含脚本执行器或被操作者标为 never 的 MCP 工具时，禁止本地反思重跑、冲突重跑/Integrator、全局重新规划和 BASE_STALE 全图重跑。未完成的 `/task` 恢复若需要重跑此能力，同样拒绝；已经发布的成果仍可按原有幂等恢复路径确认。判定基于授权范围，所以即使这一次还没调用该工具也会保守停止自动重放。只读 Skill 不因另一 Skill 拥有脚本权限而失去重试能力。模型在当前 ReAct 运行内显式再次调用工具、新建用户任务不属于这项自动重放门禁；它不提供脚本的全局 exactly-once 保证。

stdout/stderr 合计最多 1MiB，并受当前工具和执行器输出上限约束；超限显式失败，非零退出码保留为工具错误。正常结束清理临时包和同进程组子进程，超时或取消由外层执行器销毁容器。脚本没有独立的二级隔离，不能据此保证恶意代码不影响同一 Worker 的候选文件；最终变更仍须独立验收并销毁容器后发布。工具失败可由模型修复，脚本返回成功也不代替独立验收。

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

配置来自操作者明确授权的本地文件；当前提供本地包安装和摘要校验，没有下载市场、签名信任链或动态热更新。[Knowledge 索引](knowledge.md)单独开启，Skill 仍需显式授权其工具。本轮没有运行真实模型消融；实现通过只能证明兼容格式、加载、选择、权限和执行门禁，不能证明任务成功率提高。
