# 最新测试总览

当前 `dev/agent-ecosystem` 保留 E1/E2 提交 `8cfa764`，本轮提交官方 Memory，尚未合入主线。候选冻结 253 文件，清单 SHA256 `b5bbda91d094e07fc8b7c3e37422a1bdaac440e4bba7608f43565d409d1e91e5`；240 个 Python/pyproject 文件清单 SHA256 `047ec74d4669214533bf1daa77d30a088c3f9ebcf3092e5d86b31b9738ae2542`。公开文档随后整理，代码摘要单独核对。

截至 **2026-10-06**，当前候选 Windows **805 passed、202 skipped**；独立 Ubuntu VM **1006 passed、1 skipped**，两端 Ruff/Pyright 通过，VM 包含 **75 个真实 Podman 用例**，付费模型调用 0。真实社区矩阵、判据和边界见[工具与能力测试](tools-and-skills.md)。基础主线历史完整验收源码为 [`0335117`](https://github.com/beforgeted/MindCode/commit/0335117abf967fde916b87fc490fff3ac2428f05)，以下历史数字不能替代本次新代码验收。
本目录按主题维护当前结果，不追加每个开发阶段的流水；原始记录和个人现场留在本地忽略目录。

## MCP / Skill 生态最终完整结果

| 环境 | 收集 | 通过 | 跳过 | 失败 / 错误 | 静态检查 |
|---|---:|---:|---:|---:|---|
| Windows | 1007 | **805** | **202** | 0 / 0 | Ruff / Pyright 通过 |
| 独立 Ubuntu VM | 1007 | **1006** | **1** | 0 / 0 | Ruff / Pyright 通过 |

固定语料包含 149 个真实社区顶层/归档包，此前与官方参考验证器一致；本轮 VM 的 149 个包完整匹配，Windows 148 个完整匹配、vulnerability-triage 缺 scripts/triage.py，因此该包不能算 Windows 完整兼容通过。项目 pytest 通过不等于所有测试输入完整。Time、Filesystem、Everything、Git、Memory 为真实官方服务；固定 Provider 仅控制任务流程。专项包含在全量中，不能相加；实现通过不代表模型质量提升。

VM 生态专项111/111，含34项真实容器；全量75项真实容器包括基础36、Skill2、随附MCP3、社区10、可执行能力13、Memory11。双平台代码与矩阵依赖一致，148个社区包内容一致，另1个Windows包存在已记录的输入缺口；42份VM证据逐项核对，归档含清单共43文件，SHA256 `f056e65008e640b625fde144e1594ff0c4abb4b7de2a9b84bc10b9581db8bde0`。原仓库与历史不变，容器已清理。Memory专项28/28验证跨调用状态、域内串行、双域隔离、关闭、失败废弃与独立验收发布；完整1007项已包括该专项。

## 基础主线历史完整结果与统计口径

| 环境 | 收集用例 | 通过 | 跳过 | 失败 / 错误 | 静态检查 |
|---|---:|---:|---:|---:|---|
| Windows | 817 | **658** | **159** | 0 / 0 | Ruff / Pyright 通过 |
| 独立 Ubuntu VM | 817 | **816** | **1** | 0 / 0 | Ruff / Pyright 通过 |

Windows 与 VM 分别记录，不能相加。VM 的唯一跳过为 Linux 上不适用的“不支持平台应拒绝”用例。
Windows 跳过项主要是 POSIX 与真实容器路径，不代表这些能力已在 Windows 验收。

- VM 全量包含 **36 个真实 rootless Podman 用例**：18 个隔离/流程、9 个下载、3 个强杀恢复、4 个完整验收和 2 个校准门禁。
- 14 个 mock Podman 单元用例不计入真实容器数量。
- VM 专项 **211 passed**、新增 38 个校准单元用例和 2 个真容器用例均包含在全量中，不另加总。
- 最终核对 217 份源码/配置/测试/场景文件摘要，受信镜像身份与运行时有记录；原仓库和历史失败现场保留，容器清单最终为空。

## 测试主题

| 文档 | 当前能力与验收判据 |
|---|---|
| [沙箱与恢复测试](sandbox-and-recovery.md) | 文件/命令隔离、下载、普通交互、候选发布、进程互斥、崩溃恢复与资源清理 |
| [模型与上下文测试](model-and-context.md) | 路由、成本、能力、窗口、Map/Reduce、完整证据和校准 |
| [工具与能力测试](tools-and-skills.md) | 标准 Skill 包、真实 MCP、配套资源、权限、服务进程与发布门禁 |
| [真实模型场景使用](../../scenarios/README.md) | 显式启动场景、Planner 探针和 19 任务 benchmark |

普通 pytest 的模型行为由固定 Provider/SDK 替身控制，验证预算、状态与门禁。
Podman、Git/文件系统、HTTPS 与强杀操作按相应用例真实执行；这不等于真实模型的语义质量或省钱实验。

此前真实 deepseek-flash 场景 **11/11**、非 Git 正/负例 **2/2**通过，只是历史模型回归，不是源码基线 `0335117` 的最新模型质量验收。
当前尚无新一轮关闭/开启校准的付费模型对照结论，也不能把项目内 benchmark 推广为 SWE-bench 成绩。

## 复现约束

Linux 测试**只在独立 Ubuntu 虚拟机运行，禁止 WSL Ubuntu**。
创建新的验证目录与 pytest 临时目录，保留原仓库、凭据、Git 状态及历史失败现场。
公开文档使用占位路径；实际 SSH 信息、镜像身份、依赖清单和个人现场记录仅在本地保存。
下面是复现入口，运行前需准备运行时、任务依赖与受信镜像，不能用空镜像或跳过容器测试宣称 VM 全量通过。

Windows 本地检查示例（在所需源码版本的工作区运行）：

```powershell
$testOutput = Join-Path $env:TEMP ("mindcode-tests-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $testOutput | Out-Null
python -m pytest -q --basetemp (Join-Path $testOutput "tmp") --junitxml (Join-Path $testOutput "pytest.xml")
python -m ruff check .
python -m pyright
```

独立 Ubuntu VM 检查示例：

```bash
# 仅在独立 VM；源仓库路径替换为实际位置，不改变源仓库。
VALIDATION_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/mindcode-validation.XXXXXX")"
git clone --no-hardlinks /path/to/source/repo "$VALIDATION_ROOT/source"
cd "$VALIDATION_ROOT/source"
# 使用已提交的待验收版本；未提交候选需复制冻结源码，不能只克隆分支。
git checkout <待验收commit>
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev,ecosystem]'
export MINDCODE_PODMAN_TEST_IMAGE='<已安装的完整 SHA256 镜像 ID>'
.venv/bin/python -m pytest -q --basetemp "$VALIDATION_ROOT/pytest-tmp" --junitxml "$VALIDATION_ROOT/pytest.xml"
.venv/bin/python -m ruff check .
.venv/bin/python -m pyright --pythonpath "$PWD/.venv/bin/python"
podman ps -a --format json
```

该命令安装的是版本范围内依赖，不能冒充已记录环境的逐版本复刻。精确复现须核对保留的依赖清单、源码摘要和镜像检查记录。
`MINDCODE_PODMAN_TEST_IMAGE` 是测试配置；应用仍需显式设置 `CODEAGENT_EXECUTION_BACKEND` 与 `CODEAGENT_SANDBOX_IMAGE`。
容器清单核对本次资源是否全部清理，不要求或授权删除不属于本次测试的容器。

## 原始证据与更新规则

原始日志与产物位于已忽略的 `.codeagent/validation/`，公开页提供结论与判据，不把原始日志作为远端可访问的链接。

| 本地证据 | 用途 |
|---|---|
| `ecosystem-runtime/` | 当前双平台、真实脚本/资源/Prompt/重放验收及固定来源/镜像记录 |
| `agent-ecosystem/` | E1 社区结构/协议历史验收，前版失败与通过现场均保留 |
| `skills-verified/` | Skill 基础检查点的历史完整验收，不能替代当前生态版本 |
| `windows-calibration-full.xml` 与对应日志 | Windows 817 个收集用例及静态结果 |
| `vm-calibration/vm-full.xml`、`vm-unit.xml` 与日志 | VM 全量与 211 项专项 |
| `vm-calibration/calibration-summary.json` | 汇总结果与真容器分类 |
| `vm-calibration/source-manifest.json` | 验收源码摘要 |
| 运行时 / 镜像 / 依赖 / 原仓库基线 / 容器清单 | 环境身份、原现场保护与清理核对 |

后续改变源码时，更新验收版本、对应环境结果和本次失败修复；不要用旧全量结果覆盖新版本。
源码未变的文档调整只做文档检查，明确说明未重跑测试。失败、跳过、模拟与真实执行必须分别说明。

MCP/Skill 生态 E1/E2 已保存于功能分支，E3 的 Memory 状态与生命周期部分完成。下一项 Fetch 受控网络，随后 Knowledge。真实模型效果对照另行安排并取得新的数据与额度授权；既有校准消融留在实验分支。
