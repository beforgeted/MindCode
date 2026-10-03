# 最新测试总览

当前 `dev/skills` 从 `main` 的 `16c669b` 独立开发，已提交、未合入主线。最终 Skill 候选冻结 230 文件；清单 SHA256 `1f210f6c878f8487d8d7987ec3dfc12c29ab3827fbfbe4794f98fecf487a6581`。公开文档随后整理；218 个 Python/pyproject 文件清单 SHA256 `88c95542b4b569e6173394a2b88edfdb52844408d126ab07bb460f56ded912a9`，代码不变。

截至 **2026-10-04**，最终候选 Windows 完整回归 **694 passed、161 skipped**，Ruff/Pyright 通过；独立 Ubuntu VM **854 passed、1 skipped**，包含 **38 个真实 Podman 用例**；两端 Ruff/Pyright 均通过，本轮付费模型调用 0。详见[工具与能力测试](tools-and-skills.md)。基础主线的历史完整验收源码为 [`0335117`](https://github.com/beforgeted/MindCode/commit/0335117abf967fde916b87fc490fff3ac2428f05)，以下历史数字不能替代本次新代码验收。
本目录按主题维护当前结果，不追加每个开发阶段的流水；原始记录和个人现场留在本地忽略目录。

## Skill 最终完整结果

| 环境 | 收集 | 通过 | 跳过 | 失败 / 错误 | 静态检查 |
|---|---:|---:|---:|---:|---|
| Windows | 855 | **694** | **161** | 0 / 0 | Ruff / Pyright 通过 |
| 独立 Ubuntu VM | 855 | **854** | **1** | 0 / 0 | Ruff / Pyright 通过 |

VM Skill 专项 38 项（含 2 项真实容器）；全量真实 Podman 38 项（基础 36 + Skill 2），均已包含于 855 项。专项不可另加总。最终 26 文件 VM 归档摘要核对通过，SHA256 `e8c7baa79ca62c84e0dc99705e54cd095eb805a635a7c7c94db0cf1bf3f0620a`；原仓库与历史证据保持不变，容器全部清理，付费模型调用 0。实现通过不代表模型质量提升，后续自然任务对照需新的授权。

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
git checkout 0335117
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
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
| `skills-verified/windows/`、`skills-verified/vm/` | 最终 855 项完整验收与 38 项 Skill 专项，冻结源码及归档核对 |
| `windows-calibration-full.xml` 与对应日志 | Windows 817 个收集用例及静态结果 |
| `vm-calibration/vm-full.xml`、`vm-unit.xml` 与日志 | VM 全量与 211 项专项 |
| `vm-calibration/calibration-summary.json` | 汇总结果与真容器分类 |
| `vm-calibration/source-manifest.json` | 验收源码摘要 |
| 运行时 / 镜像 / 依赖 / 原仓库基线 / 容器清单 | 环境身份、原现场保护与清理核对 |

后续改变源码时，更新验收版本、对应环境结果和本次失败修复；不要用旧全量结果覆盖新版本。
源码未变的文档调整只做文档检查，明确说明未重跑测试。失败、跳过、模拟与真实执行必须分别说明。

Skill 一期独立验收已完成，已提交、未合入主线；下一项为 Knowledge 只读项目索引与查询。真实模型效果对照另行安排并取得新的数据与额度授权；既有校准消融留在实验分支。
