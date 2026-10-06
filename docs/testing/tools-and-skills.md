# MCP 与 Skill 真实社区兼容测试

当前验收对象为 `dev/agent-ecosystem`，基于 Skill 检查点 `2416a72`，整合 MCP 基础并新增社区兼容层，尚未合入 `main`。使用说明见[Skill](../skills.md)与[MCP](../mcp-tools.md)。Linux 测试仅在独立 Ubuntu VM 运行，禁止 WSL。

## 固定输入与统计口径

| 输入 | 固定版本 | 用途 |
|---|---|---|
| [Agent Skills 官方参考](https://github.com/agentskills/agentskills/tree/69ef37e9424c0a7ea9dd2293b559e43ec8176379) | `69ef37e9424c0a7ea9dd2293b559e43ec8176379` | 规范与 skills-ref 验证器；该版本没有独立 Skill 样例库 |
| [JayRHa/AgentSkills](https://github.com/JayRHa/AgentSkills/tree/7ce3d8d6af7ca3905c688c649000b98e8e57db4a) | `7ce3d8d6af7ca3905c688c649000b98e8e57db4a` | 74 个顶层包，包含 1 个模板；另有 1 个嵌套 Skill 文件 |
| [matt-riley/agent-skills](https://github.com/matt-riley/agent-skills/tree/5048ebffd149e6bbfedb6b58f212a43407826518) | `5048ebffd149e6bbfedb6b58f212a43407826518` | 71 个当前包、4 个归档包；另有 14 个嵌套模板/样例/故障文件 |
| [MCP 官方仓库](https://github.com/modelcontextprotocol/servers/tree/f46d9578190b476b3501923ea8977d899e8db2cb) | `f46d9578190b476b3501923ea8977d899e8db2cb` | 对照源码；实测分发包版本独立固定，不冒充同一源码构建 |
| 官方 Time / Git Python 分发包 | 均 `2026.8.18` | 真实 initialize/list/call |
| 官方 Filesystem / Everything npm 分发包 | 均 `2026.8.31` | 真实 Schema、IO 与协议能力 |
| SDK / YAML / Schema | MCP `1.30.0`、PyYAML `6.0.3`、jsonschema `4.26.0` | 固定客户端依赖；官方参考另用 strictyaml `1.7.3` |

两个 Skill 仓库合计发现 164 个 `SKILL.md` 文件，其中 **149 个顶层/归档包、15 个嵌套文件**。149 个包全部通过当前加载器和固定官方参考验证器。模板、归档包和嵌套反例不等同于 164 项真实业务能力，也不等同于 149 个 pytest 用例。

Skill 回归分别逐包安装、核对完整冻结内容、重新加载，并确认已有目录不能覆盖；额外用真实 code-tour 包验证 script 配套读取、磁盘修改后仍读取旧冻结内容及权限拒绝。进一步使用同一固定 code-tour 的真实 validate_tour.py，在 Podman 内执行有效与无效输入、冻结版本和发布门禁；其它脚本依赖或外部应用尚未验收。社区工作方法未作为本轮开发指令执行。

## 从容易到难的实际判据

| 层次 | 真实输入或反例 | 判据与边界 |
|---|---|---|
| 格式基线 | 官方参考 + 两个真实 Skill 库 | 标准 YAML、较长描述/正文、metadata、多层配套和普通隐藏文件；字节保留、摘要与资源冻结 |
| Skill 权限 | 真实配套包 + 恶意负例 | 包声明不能自授工具；资源精确路径；重复 YAML、别名、对象标签、凭据与链接拒绝 |
| Skill 脚本 | 真实 code-tour 验证器 | 有效 tour 通过；坏 JSON、越界行号和不匹配正则失败；加载后改包、工作区同名脚本/模块不能替换入口 |
| Time | 官方 Python 服务 | initialize、tools/list、实际 UTC 时间调用；真实 Schema 缺必填项在启动服务前拒绝 |
| Filesystem | 官方 npm 服务 | 当前快照读写；树外及私有路径失败；真实读写结果进入已有 ToolResult |
| Everything | 官方协议测试服务 | 13 工具、7 资源、4 Prompt；资源/模板发现，实际资源读与带参数 Prompt；structuredContent 通过，图片结果显式拒绝 |
| Git | 官方 Python 服务 | 12 工具发现与实际 git_status；Podman 使用临时独立仓库，主机 `.git` 仍不进入快照 |
| 运行与发布 | 官方 Filesystem / Everything / Time | MCP 写入通过独立验收才发布；失败保持 base/HEAD；超时/取消回收；Schema 改变拒绝；工作区同名模块和启动脚本不能替换服务 |

Resources/Prompts 已按精确 URI/名称授权为 Agent 工具：固定描述符、必填参数、未知参数、漂移及超限拒绝；结果保持普通 ToolResult。真实 Everything 交互反例经 Prompt 返回越权文字后，伪造写工具仍被 Skill 权限拒绝。动态模板 URI 尚未授权。客户端有目录分页保护，但本轮样本的实际分页触发情况单独记录；不能将实现支持写成已覆盖所有分页行为。sampling、elicitation、roots、tasks、订阅、远程 HTTP/OAuth、多媒体和有状态重连尚未验收或支持，不宣称完整 MCP Protocol compatibility。

## 最新完整验收

最终候选冻结 **251 文件**，全源码清单 SHA256 `a943eed48819d3ef8921759c0248440f1282e641dd371a630b1eec059702f57f`；**238 个 Python/pyproject 文件**清单 SHA256 `042dc3f2de17c6a68a301b6a785471e377cf3dd34d08d186ee88e5c71bdab769`。公开文档随后整理，最终代码摘要单独绑定。

| 环境 | 收集 | 通过 | 跳过 | 失败 / 错误 | 静态检查 |
|---|---:|---:|---:|---:|---|
| Windows | 979 | **790** | **189** | 0 / 0 | Ruff / Pyright 通过 |
| 独立 Ubuntu VM | 979 | **978** | **1** | 0 / 0 | Ruff / Pyright 通过 |

VM 生态专项 **83/83**，含 60 项加载/协议/权限/重放与 **23 个真实 Podman 用例**；全量共 **64 个真实 Podman 用例**（基础 36 + Skill 2 + 随附 MCP 3 + 社区 10 + 可执行能力 13），均已包含在 979 项中，不能相加。Windows 的跳过包括基础 159、新增 28 项容器测试及 2 项不可用的符号链接测试；VM 仅跳过一个不适用的平台拒绝用例。

双平台代码清单完全一致，149 包内容摘要与依赖版本一致；33 份 VM 证据逐项核对，归档含摘要清单共 34 文件，SHA256 `a084fd4a7448578e7334e86cbab3dcb8dd45f6a0e312234064ebdc08a894cc16`。原仓库和历史记录保持不变，容器清单为空，付费模型调用 0。原始日志保存在已忽略的 `.codeagent/validation/ecosystem-runtime/`，源码、来源与失败现场保存在本地私有验证目录。

原始失败包括 SDK 将单个 ValueError 包装成 ExceptionGroup，以及私有 umask 导致打包后 Node 的目录/文件不可被容器非 root 用户读取执行。前者保留单一叶异常类别，后者以新镜像标签修复依赖权限；失败日志、首次镜像和原仓库现场保留在忽略目录，原有隔离约束没有放宽。

后续真实反例还复现了 Time 被工作目录同名 Python 模块替换；固定服务 cwd、导入环境与可执行路径，并拒绝可写目录启动脚本。旧候选通过后仍需随新反例重新验收。本轮还发现 retry=never 未被自动收敛链路消费，补齐反思、冲突、重规划、BASE_STALE 与未完成恢复门禁，增加 9 项回归。前候选 VM 全量主动中断、日志保留，不能算作本版本通过；最终重新完成双平台全量。Windows 首轮社区包读取出现一次 Bad file descriptor，同一来源针对复验通过，原因未确定，未新增自动重试掩盖异常。

## 复现与效果边界

社区源码、服务器依赖及原始日志均留在忽略目录。先下载上述固定提交，在独立环境安装固定包，并准备包含相同 Python/npm 依赖、Node 和 Git 的可信镜像；本轮 Node 使用官方 Linux `v22.23.3`，发行包 SHA256 `df450af89261115ef9f9e3830c3eeb2cc9213b63c720b1af623cb5dcbe2e02de`。

```bash
python -m pip install -e '.[dev,ecosystem]'
python -m scenarios.ecosystem_compatibility --sources <jay/portable/spec/mcp所在目录> --node <完整Node路径> --node-root <npm安装前缀> --output .codeagent/validation/ecosystem-new
# 设置 MINDCODE_ECOSYSTEM_FIXTURE 指向新输出中的 fixture.json。
python -m pytest -q tests/test_ecosystem.py tests/test_ecosystem_runtime.py
# 仅在独立 VM 设置 MINDCODE_ECOSYSTEM_IMAGE 为完整固定镜像 ID：
python -m pytest -q tests/test_ecosystem_podman.py tests/test_ecosystem_runtime_podman.py
```

准备器不联网安装、修改源包或调用模型；已有输出目录拒绝覆盖。来源目录名称与固定树结构应匹配；全量复验还需基础 Podman 镜像及受控下载测试的既有可信 fixture，不能因漏配而跳过后声称 VM 全量通过。

本轮验证的是社区结构、协议行为、隔离和发布契约，没有调用付费模型，也没有做 Skill 效果消融。兼容通过不证明第三方提示词提升任务成功率。下一阶段优先处理服务生命周期及 Memory 跨调用状态与 Worker 隔离，再处理 Fetch 受控网络；Knowledge 顺延。
