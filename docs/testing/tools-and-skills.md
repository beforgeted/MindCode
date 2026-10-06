# MCP 与 Skill 真实社区兼容测试

当前 `main` 包含声明式 Skill 与社区 stdio MCP 兼容层，需显式配置及授权；发布源码验收检查点为 `e3f87c8`。使用说明见[Skill](../skills.md)与[MCP](../mcp-tools.md)。Linux 测试仅在独立 Ubuntu VM 运行，禁止 WSL。

## 固定输入与统计口径

| 输入 | 固定版本 | 用途 |
|---|---|---|
| [Agent Skills 官方参考](https://github.com/agentskills/agentskills/tree/69ef37e9424c0a7ea9dd2293b559e43ec8176379) | `69ef37e9424c0a7ea9dd2293b559e43ec8176379` | 规范与 skills-ref 验证器；该版本没有独立 Skill 样例库 |
| [JayRHa/AgentSkills](https://github.com/JayRHa/AgentSkills/tree/7ce3d8d6af7ca3905c688c649000b98e8e57db4a) | `7ce3d8d6af7ca3905c688c649000b98e8e57db4a` | 74 个顶层包，包含 1 个模板；另有 1 个嵌套 Skill 文件 |
| [matt-riley/agent-skills](https://github.com/matt-riley/agent-skills/tree/5048ebffd149e6bbfedb6b58f212a43407826518) | `5048ebffd149e6bbfedb6b58f212a43407826518` | 71 个当前包、4 个归档包；另有 14 个嵌套模板/样例/故障文件 |
| [MCP 官方仓库](https://github.com/modelcontextprotocol/servers/tree/f46d9578190b476b3501923ea8977d899e8db2cb) | `f46d9578190b476b3501923ea8977d899e8db2cb` | 对照源码；实测分发包版本独立固定，不冒充同一源码构建 |
| 官方 Time / Git Python 分发包 | 均 `2026.8.18` | 真实 initialize/list/call |
| 官方 Filesystem / Everything / Memory npm 分发包 | 均 `2026.8.31` | 真实 Schema、IO、协议与文件状态 |
| SDK / YAML / Schema | MCP `1.30.0`、PyYAML `6.0.3`、jsonschema `4.26.0` | 固定客户端依赖；官方参考另用 strictyaml `1.7.3` |
| 官方 Fetch 源码 | 上述 MCP 固定提交的 `src/fetch`，源码版本 `0.6.3` | 实际源码摘要匹配；不冒充同版本 PyPI wheel 构建 |
| Fetch 内容依赖 | readabilipy `0.3.0`、markdownify `1.2.3`、Protego `0.7.0`；其它依赖与平台 wheel 另存摘要 | 明确纯 Python HTML 路径；不运行期安装 Node/npm |

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
| Memory | 官方 npm 服务，9 工具发现、逐项授权 | 创建实体、追加观察、读图谱；跨调用与新域恢复；同域串行不丢更新、双域状态隔离；损坏/超限/链接、响应失败后废弃、服务关闭及验收发布 |
| Fetch | 官方固定源码 + 受控 HTTPS 与离线传输适配 | 真实工具发现、HTML 提取、raw/分页、URL 规范化；官方 robots 拒绝、未知跳转、Schema 漂移、混合 DNS、超限、超时/取消与 tmpfs 清理；官方 Python 文档真实 HTTPS |
| 运行与发布 | 官方 Filesystem / Everything / Time | MCP 写入通过独立验收才发布；失败保持 base/HEAD；超时/取消回收；Schema 改变拒绝；工作区同名模块和启动脚本不能替换服务 |

Resources/Prompts 已按精确 URI/名称授权为 Agent 工具：固定描述符、必填参数、未知参数、漂移及超限拒绝；结果保持普通 ToolResult。真实 Everything 交互反例经 Prompt 返回越权文字后，伪造写工具仍被 Skill 权限拒绝。动态模板 URI 尚未授权。客户端有目录分页保护，但本轮样本的实际分页触发情况单独记录；不能将实现支持写成已覆盖所有分页行为。sampling、elicitation、roots、tasks、订阅、远程 HTTP/OAuth、多媒体和有状态重连尚未验收或支持，不宣称完整 MCP Protocol compatibility。

## 最新完整验收

当前全量已包含 Knowledge 修复。`e3f87c8` 冻结 **256份代码、测试、场景与项目配置文件**，清单 SHA256 `d8e7696912f0ad386cf3a8bdaf0559773696c1442cc758f6b8855a8d14d60aad`，双平台逐文件匹配。发布收尾仅调整文档，验收源码不变，未重跑全量；历史功能版本摘要与结果见[Knowledge 测试](knowledge.md)。

| 环境 | 收集 | 通过 | 跳过 | 失败 / 错误 | 静态检查 |
|---|---:|---:|---:|---:|---|
| Windows | 1146 | **922** | **224** | 0 / 0 | Ruff / Pyright 通过 |
| 独立 Ubuntu VM | 1146 | **1145** | **1** | 0 / 0 | Ruff / Pyright 通过 |

全量中的生态相关用例 **154/154**（43真实容器）保持通过；Knowledge **67/67**（11真实容器），全量共 **95 个真实 Podman 用例**（既有84 + Knowledge11），均包含在1146项中。Windows跳过既有211项、Knowledge的11个容器及2个符号链接用例，共224；VM仅跳过一个不适用的平台用例。Fetch43/43、Memory28/28包含在全量中，不相加。

双平台全量使用相同冻结源码与矩阵依赖版本；最新VM证据归档 SHA256 `f1d96805f919ca9c8ab288ae6a6369b30747b83096a513b3e3cda87a05852d5c`，原始日志位于已忽略的 `.codeagent/validation/knowledge-fix/`。VM核对为独立Ubuntu VMware来宾，无WSL；原仓库/历史记录不变，容器为空，修复回归付费模型调用0。历史Fetch与Knowledge功能验收记录保留，原模型消融另见Knowledge专项。Windows148个社区包与VM完整匹配，另1包存在下述输入缺口，不将pytest通过冒充完整跨平台语料一致。

原始失败包括 SDK 将单个 ValueError 包装成 ExceptionGroup，以及私有 umask 导致打包后 Node 的目录/文件不可被容器非 root 用户读取执行。前者保留单一叶异常类别，后者以新镜像标签修复依赖权限；失败日志、首次镜像和原仓库现场保留在忽略目录，原有隔离约束没有放宽。

后续真实反例还复现了 Time 被工作目录同名 Python 模块替换；固定服务 cwd、导入环境与可执行路径，并拒绝可写目录启动脚本。旧候选通过后仍需随新反例重新验收。本轮还发现 retry=never 未被自动收敛链路消费，补齐反思、冲突、重规划、BASE_STALE 与未完成恢复门禁，增加 9 项回归。前候选 VM 全量主动中断、日志保留，不能算作本版本通过；最终重新完成双平台全量。Windows 首轮社区包读取出现一次 Bad file descriptor，同一来源针对复验通过，原因未确定，未新增自动重试掩盖异常。

本轮来源核对发现 Windows 的 vulnerability-triage 缺少 scripts/triage.py：目录仍有 SKILL.md，加载/安装测试因而通过，但不能证明整个固定社区包完整。用户提供的杀毒记录确认原样本被防护软件删除。核对该次 pytest 所列测试及包加载调用链，未发现执行或导入 triage.py 的路径；读取、复制脚本也会接触其内容，防护软件的“执行”分类不能单独作为脚本执行证据，也不能据此判定为误报。原始归档包含该文件且摘要正确；在新验证目录重建后，读取再次出现 Bad file descriptor，随后文件缺失，但该次缺失没有对应的防护事件记录，直接原因仍未确认。保留两处现场，没有改变防护设置或继续恢复该文件。其余 1337 个来源文件与固定清单一致；Windows 148 个包完整匹配、1 个输入不完整，VM 149 个包完整匹配。该缺口与 Memory 分发包和当前项目代码无关；最新报告明确保留此边界，不将全量 pytest 通过冒充完整跨平台语料一致。

## 复现与效果边界

社区源码、服务器依赖及原始日志均留在忽略目录。先下载上述固定提交，在独立环境安装固定包，并准备包含相同 Python/npm 依赖、Node 和 Git 的可信镜像；本轮 Node 使用官方 Linux `v22.23.3`，发行包 SHA256 `df450af89261115ef9f9e3830c3eeb2cc9213b63c720b1af623cb5dcbe2e02de`。

```bash
python -m pip install -e '.[dev,ecosystem]'
python -m scenarios.ecosystem_compatibility --sources <jay/portable/spec/mcp所在目录> --node <完整Node路径> --node-root <npm安装前缀> --memory-node-root <Memory安装前缀> --output .codeagent/validation/ecosystem-new
# 设置 MINDCODE_ECOSYSTEM_FIXTURE 指向新输出中的 fixture.json。
python -m pytest -q tests/test_ecosystem.py tests/test_ecosystem_runtime.py tests/test_mcp_memory.py
# 仅在独立 VM 设置 MINDCODE_ECOSYSTEM_IMAGE 为完整固定镜像 ID：
python -m pytest -q tests/test_ecosystem_podman.py tests/test_ecosystem_runtime_podman.py tests/test_mcp_memory_podman.py
```

准备器不联网安装、修改源包或调用模型；已有输出目录拒绝覆盖。来源目录名称与固定树结构应匹配；全量复验还需基础 Podman 镜像及受控下载测试的既有可信 fixture，不能因漏配而跳过后声称 VM 全量通过。

Fetch 增加 `--fetch` 仅发现已安装的官方服务。使用固定上游源码与 wheel 依赖构建独立受信镜像，构建本身断网；独立 VM 设置 `MINDCODE_FETCH_IMAGE` 为其完整 ID、`MINDCODE_FETCH_LIVE_URL=https://docs.python.org/3/library/urllib.parse.html`、`MINDCODE_FETCH_LIVE_EXPECTED="Parse URLs"`，执行 `tests/test_mcp_fetch.py` 与 `tests/test_mcp_fetch_podman.py`。正例正文标记与 URL 前缀不同，避免因结果重复 URL 而虚假通过。网页内容是动态输入，接收字节数、SHA256 与请求元数据逐次保存；完整正文仅服务本次调用，未持久保存，不声称内容永久相同或能够离线重现整个网页。

首次 Fetch 容器夹具重复创建目录，8 个用例未进入实际调用；按记录的镜像/所有者逐项回收残留容器，保留失败日志。真实 HTML 随后暴露 readabilipy 自动尝试 npm 安装，改为显式纯 Python 路径并断言不启动 Node/npm。GitHub 直连重置未算作下载超限；PyPI 项目页挑战未算作正文获取；PyPI JSON 被官方 robots 禁止，保留为正确拒绝，未绕过规则。最终改用 robots 允许的官方 Python 文档验证 HTTPS 与页面字节上限。离线的合成 HTML/重定向/故障响应检验真实服务及拒绝路径，不冒充真实公网跳转或浏览器渲染验收。

上述工程回归验证结构、协议、权限、状态、网络与Knowledge引用契约，没有付费模型调用。Knowledge一期已完成，历史离线对照与另行授权的人工Flash消融见[专项](knowledge.md)，不能据工程回归推断效果收益。没有长期进程缓存、状态语义合并或浏览器JavaScript渲染。
