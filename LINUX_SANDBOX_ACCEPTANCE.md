# Linux Podman 分环境验收记录

2026-10-01 当前最终版本：独立 Ubuntu VM **415 passed、1 skipped**，8 个真容器用例与独立探针通过；Ruff / Pyright 通过，容器清单为空。

以下先保留此前 WSL2 一期结果，独立 VM 追加实现与证据见文末。

一期 WSL2 验收通过：**395 passed、1 skipped**，Ruff 通过，Pyright 0 errors。
包含 **6 个真实 Podman 用例全部通过**；独立内核 / 负向探针通过；验收结束容器清单为空。
此次不是历史 VM 真实模型套件的重跑，Worker 输出使用 StubLlmClient 固定，以隔离模型随机性。

## 环境与镜像来源

- Ubuntu 24.04.4 WSL2；内核 6.6.87.2-microsoft-standard-WSL2；Python 3.12.3。
- Podman 4.9.3 / crun 1.14.1；普通用户 meng（UID 1000）；rootless=true；cgroup v2 / systemd。
- 已安装 Podman、uidmap、slirp4netns、fuse-overlayfs、python3-venv / pip，依赖安装在独立 venv。
- Linux 原生验证副本：`/home/meng/mindcode-linux-validation.VXxwX4`，175 个源码 / 测试文件逐一核对 SHA256；不复制宿主凭据、Git 元数据或项目状态。
- 官方来源：`docker.io/library/python@sha256:44ff437bba879d4941b710a369a8f19266aea34b29002807f0c487fabc9eec9b`。
- 验收使用完整镜像 ID：`9e87977b867847e186d066f531ef783b006d582a985c341c269446088d90f2c4`。
- Docker Hub 直连超时后，经已有 Windows 回环代理下载 OCI 内容；TLS 校验开启，每块内容按 SHA256 校验后导入。
  不修改代理监听、不开新代理端口，验收容器依然断网。不以可变 tag 作为后端身份。
- 依赖版本：pytest 9.1.1、pytest-asyncio 1.4.0、Ruff 0.16.9、Pyright 1.1.414、
  anthropic 1.11.0、pydantic 2.13.5；完整清单见 `.codeagent/validation/linux-dependencies.txt`。

## 已验证的性质

| 性质 | 实测 / 断言 |
|---|---|
| 文件与命令视图一致 | 同容器写、读、搜索与 shell 追加，封存回传合法成果 |
| 父目录 / 宿主隔离 | `../../../note.txt` 写入失败；宿主哨兵路径、模拟环境标记和常见容器 socket 不可见 |
| 文件系统与网络限制 | root 只读；仅 lo 接口；TCP 连接返回 ENETUNREACH；16 MiB workspace 写满返回 ENOSPC |
| 实际进程权限 | UID=1000，CapEff=CapBnd=0，NoNewPrivs=1，seccomp=2 |
| 实际 cgroup 限额 | memory.max=134217728、memory.swap.max=0、pids.max=16、cpu.max=50000 100000 |
| 快照接收与销毁 | pause / 身份复核 / 可信导出 / 销毁确认 / 数据校验与回写路径通过；不安全 symlink 被拒 |
| 取消清理 | 观察到 setsid 后代，取消后容器删除，后代 PID 身份消失或进入退出状态 |
| 正常提交 | Worker 结果进入 candidate，独立容器验收通过后 promote；worktree 回收到只剩 base |
| 拒绝旧漏洞 | 固定 Worker 先正常写文件，再尝试旧越界命令；越界退出码非零；独立 `false` 验收拒绝；真实 base HEAD、seed 内容不变且 note.txt 不存在 |
| 无残留 | 全量开始和结束的 rootless 容器清单均为 `[]`，两次独立探针的各自容器也确认删除 |

内核 / 负向探针使用 0.5 CPU、128 MiB、16 进程、workspace 16 MiB、tmp 8 MiB 的配置。
读取资源文件与 ENOSPC 验证不等同于全面的 OOM / fork-bomb 压力测试。Podman 报 apparmorEnabled=false，
本次不声称有独立 AppArmor profile 保护；容器仍共享宿主内核。

## 检查结果与证据

- 首轮 POSIX 快照 / 发布 / 原 5 个真容器测试：120 passed、1 skipped，187.68 秒。
- 新增 `[reject_escape]` 后 Linux 全量：395 passed、1 skipped，259.31 秒；六个真容器用例全部通过。
- 唯一跳过：`test_unsupported_platform_fails_closed: unsupported-platform behavior`。这是用于不支持平台的拒绝测试，在 Linux 上不适用，非真容器跳过。
- 静态检查均 exit 0，类型检查 0 errors / 0 warnings。
- 首次独立探针全部断言通过，但 inline shell 退出码包装失败；保存 first JSON / log，
  改成独立脚本后重跑全部探针成功并记录 exit 0，没有放宽判据。

原始证据在 `.codeagent/validation/`（本地生成文件，不自动提交）：
`linux-full.xml`、`linux-full.log`、`linux-ruff.log`、`linux-pyright.log`、`linux-full-exit-codes.json`、
`source-manifest.json`、`linux-podman-info.json`、`linux-image-inspect.json`、`trusted-image-provenance.json`、
`kernel-isolation-probe.json`、`kernel-isolation-probe-exit-code.txt`、`full-containers-before.json`、
`full-containers-after.json`、`linux-acceptance-summary.json`。SHA256 清单对应验收源码快照，后续文档更新不属于该快照。

## 复现

已准备环境可直接在 Ubuntu 以 meng 用户执行：

```bash
cd /home/meng/mindcode-linux-validation.VXxwX4
export MINDCODE_PODMAN_TEST_IMAGE=9e87977b867847e186d066f531ef783b006d582a985c341c269446088d90f2c4
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check .
.venv/bin/python -m pyright --pythonpath "$PWD/.venv/bin/python"
```

应用运行需另行显式配置 `CODEAGENT_EXECUTION_BACKEND=podman`、`CODEAGENT_SANDBOX_IMAGE` 和任务验收命令；
测试变量不会自动把默认 local 或普通单 Agent 交互切成沙箱。

## 剩余边界

本次完成 B4/B5 一期的 WSL2 Linux 运行验收；B4 整项仍不勾选：一期时跨进程容器资源账本与崩溃回收待做（本轮追加结果见下）；
普通交互与非 Git 接线尚未完成。B5 默认离线限制已实测，按需网络白名单仍待设计。
同一 run 跨进程恢复锁、外部 exactly-once 和真实模型完整场景套件重跑也不在此次验收完成范围。
本轮不提交 / 推送现有工作区改动，历史 VM 012 失败现场保持原记录。


## 2026-10-01 追加：独立 Ubuntu 虚拟机与跨进程回收

**环境单独核对**：此前 395 / 1 是 WSL2 Ubuntu 的一期结果。本轮通过 SSH 进入用户独立虚拟机
`mengx@192.168.100.128`，Ubuntu 24.04，内核 `6.8.0-142-generic`；不是 WSL。
普通用户 UID 1000，rootless Podman 4.9.3 / crun 1.14.1，cgroup v2 / systemd，固定镜像 ID 同上。
原 `/home/mengx/MindCode` 及 R1 历史失败现场保持原样；新源码快照在
`/home/mengx/mindcode-sandbox-validation.VzlQxx`。177 个源码 / 测试文件逐一验证 SHA256。
独立 venv 只读复用原 venv 的已有依赖；主进程和 `python -I` 子进程均核对加载新快照。

**结果**：独立 VM 全量 **415 passed、1 skipped**；Ruff / Pyright 通过，8 个真容器用例全部通过。
验收前后容器清单为空；另跑实际内核探针通过。唯一跳过仍是不支持平台的拒绝测试。
Windows 当前回归 356 passed、60 skipped，Ruff / Pyright 通过；新增 Linux 账本与真容器用例不在 Windows 执行。
真实模型历史 11 场景套件本轮没有重跑，原 verify_fail 现场与结论不改写。

### 实现与证据对应

| 窗口 / 风险 | 机制与断言 |
|---|---|
| create 成功但 ID 未记录即 SIGKILL | 创建前 SQLite FULL/WAL 持久化唯一名字、用途和镜像；恢复精确查名并核对 labels / image / ID |
| 已启动并暂停即 SIGKILL | 内核 flock 判断控制器死亡，另一个实例启动时回收；同时保留仍持租约的活跃容器 |
| CLI 创建进程比控制器活得久 | 创建客户端继承 lease fd；客户端退出前不得把未完成创建判为不存在 |
| conmon 错误继承租约 | start / exec 不继承 lease，仅短期 create / rm 继承；真实暂停崩溃测试覆盖 |
| 错项目、镜像或标签 | 身份不符拒绝删除；未入账容器不扫描、不删 |
| 删除失败 / exists 返回未知 | 保留记录并拒绝继续；后续控制器可重试，不把未知当不存在 |
| 控制目录不可信 | 私有 caller-owned 目录 / 普通文件，拒绝符号链接、共享权限和跨项目复用 |

账本位于 `state_root/sandbox/<project_id 的 SHA256 前 24 位>/resources.db`；与 RunStore 分离。
它记录执行资源的归属和创建意图，不保存模型工作状态，不替代同一 run 的跨进程恢复互斥。
正常 CLI / MasterSession 结束和装配失败均关闭管理器；恢复只在使用同项目账本的管理器启动时触发，
没有独立常驻回收服务。迁移 / 删除状态目录会失去自动恢复依据；手工无账本装配仍仅进程内管理。

### 保留的失败与修复

WSL 首轮暂停窗口失败：start 把租约 fd 传入 conmon，控制器死亡后 lease 仍被持有。
原日志 / XML / 遗留清单保留，按精确 ID 和完整归属核对后清理该测试容器；修复继承范围。
VM 首轮只失败共享权限负向用例：umask=077 将 mkdir(0755) 收紧为0700；显式 chmod(0755)
确保反例成立，程序拒绝策略未放宽，重跑全量通过。不同阶段数字不相加。

原始 VM 证据：`.codeagent/validation/vm-recovery/` 下 `vm-full.xml`、`vm-full.log`、
`vm-ruff.log`、`vm-pyright.log`、`vm-exit-codes.json`、`source-manifest.json`、
`vm-podman-info.json`、`vm-image-inspect.json`、`vm-kernel.txt`、`vm-containers-before.json`、
`vm-containers-after.json`、`kernel-isolation-probe.json`、`vm-first-failed.log/xml`。
WSL 追加回归与首轮失败记录为 `.codeagent/validation/recovery-*`。

复现：SSH 进入 VM，在上述独立目录设置 `MINDCODE_PODMAN_TEST_IMAGE` 为完整镜像 ID，
运行 `.venv/bin/python -m pytest -q`、`.venv/bin/python -m ruff check .` 和
`.venv/bin/python -m pyright --pythonpath "$PWD/.venv/bin/python"`。
普通单 Agent 交互、非 Git 沙箱、按需网络策略、同 run 跨进程互斥仍待完成，B4 不整体勾选。

最终修复补充：内核校验失败后，确认删除资源时同步移除内存句柄，避免随后 aclose 误查已删除容器。
新增异常回归纳入 VM 全量415 / 1；WSL2此前全量414 / 1，最终账本专项18 passed与静态检查通过。

## 2026-10-02 追加：Git 普通单 Agent 交互

已先提交恢复与路由基线 `645afdc`。本轮在独立 Ubuntu VM `mengx@192.168.100.128` 的
`/home/mengx/mindcode-interactive-validation.09tZaZ` 验收；未调用 WSL，原仓库与历史失败现场保持原样。
179 个源码 / 测试文件逐一核对 SHA256；复用已有受信镜像 ID 与只读依赖路径，验证子进程加载新源码。

独立 VM 全量 **428 passed、1 skipped**（150.22秒），Ruff / Pyright通过，10个真容器用例通过。
Windows356 / 73，静态检查通过。交互与工具门禁专项23项通过，包含在全量内，不相加。
真实 `run_repl` 另做确定性输入与真实容器冒烟：写answer.txt=42、下一轮读取、/clear、普通输入、/quit；
项目成果正确，只剩base工作树，最终容器清单为空。这里使用Stub固定模型输出，不声称历史真实模型套件已复验。

每轮候选隔离 → INTERACTIVE容器 → 成功封存 / 销毁 → 私有候选回写 → Git候选提交 → 独立VALIDATION容器
→ 再次核对工作区状态与HEAD → CAS合并。验收容器修改不回传。无改动的轮次不提交，不强求验收命令。
写轮次缺验收命令、验收失败、取消或清理异常均不发布；返回失败时不把隔离写入列为已发布文件。
会话保留history；每轮预算重置，取消后可继续；任务级取消补齐未返回的tool_result，关闭当前turn。
`sandbox_turn_finished`记录事务最终结果，区别于ReAct模型回复完成。会话send串行化，执行期间拒绝clear。

边界：当前要求干净Git根工作区，成功写轮次会自动提交并合并；非Git、用户未提交改动的交互支持待做。
没有同run跨进程恢复互斥、后台回收守护服务或联网白名单。跨进程账本保护的是资源归属，不替代这些能力。
证据 `.codeagent/validation/vm-interactive/`：vm-full.xml/log、vm-unit.xml/log、vm-ruff.log、vm-pyright.log、
vm-exit-codes.json、source-manifest.json、vm-cli-smoke.json/log、vm-containers-before/after.json及运行环境文件。
本轮新改动尚未提交，后续文档更新不属于被测源码清单。
