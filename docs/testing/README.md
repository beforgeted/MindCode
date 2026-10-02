# 最新测试总览

验收日期：**2026-10-03**。生产与测试源码基线：[`0335117`](https://github.com/beforgeted/MindCode/commit/0335117abf967fde916b87fc490fff3ac2428f05)。
后续 README 更新与本次文档整理未改变生产或测试源码，未重跑全量测试。
本目录按主题维护当前结果，不追加每个开发阶段的流水；原始记录和个人现场留在本地忽略目录。

## 完整结果与统计口径

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
| `windows-calibration-full.xml` 与对应日志 | Windows 817 个收集用例及静态结果 |
| `vm-calibration/vm-full.xml`、`vm-unit.xml` 与日志 | VM 全量与 211 项专项 |
| `vm-calibration/calibration-summary.json` | 汇总结果与真容器分类 |
| `vm-calibration/source-manifest.json` | 验收源码摘要 |
| 运行时 / 镜像 / 依赖 / 原仓库基线 / 容器清单 | 环境身份、原现场保护与清理核对 |

后续改变源码时，更新验收版本、对应环境结果和本次失败修复；不要用旧全量结果覆盖新版本。
源码未变的文档调整只做文档检查，明确说明未重跑测试。失败、跳过、模拟与真实执行必须分别说明。

下一项先建设成对对照报告，再在有预算约束的独立 VM 中运行小规模真实模型实验，量化质量、计数偏差、压缩、费用和延迟。
