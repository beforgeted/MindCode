# Linux Podman 分环境验收记录

2026-10-02 当前最终版本：独立 Ubuntu VM **475 passed、1 skipped**，18 个真容器用例通过；另完成真实模型历史11/11与非Git2/2复验；Ruff / Pyright 通过，容器清单为空。

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

以下命令仅为 WSL 历史复现记录，禁止再次执行；当前 VM 复现目录见文末：

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


## 2026-10-02 追加：dirty Git / 非 Git 普通交互与写回恢复

先提交普通交互阶段 `4128098`，再实现本节。独立 VM 为 mengx@192.168.100.128，内核
6.8.0-142-generic；最终目录 `/home/mengx/mindcode-shared-accepted.gman3d`，182 个源码文件哈希逐一核对，
原 `/home/mengx/MindCode` 与历史现场保留。没有 WSL 测试、没有重新安装依赖或拉取镜像。

最终全量 **454 passed、1 skipped**（253.578 秒）；14 个真容器用例全部通过，Ruff/Pyright通过。
Windows **356 passed、99 skipped**；新增 Linux 专属用例在 Windows 跳过。针对性45项包含在全量内。
真实 REPL 分别测试 dirty Git 与非 Git：写42、下一轮读、clear、退出，保留用户文件、Git暂存与HEAD；
非Git不创建.git。全量前后与 REPL 结束的容器清单均为空。Stub固定输出，不代表历史真实LLM场景已复验。

新增实测：暂存/工作区不同版本与未跟踪文件的输入、验收接受与拒绝、并发工作文件/暂存修改拒绝；
真实SIGKILL覆盖prepared、临时文件、部分替换、applied四窗口。IO故障回滚原数据与新目录；新用户修改
或损坏日志保留并报告待核对。applied标记替换后持久化确认异常也报告待核对，不能宣称未发布。
当前共享发布不改变Git暂存区和HEAD；干净Git原有自动候选提交/CAS路径保留。日志仅负责文件发布，
不是新增任务编排恢复权威；容器账本负责资源回收，RunStore负责Attempt恢复，C6互斥仍未实现。

边界：flock仅约束MindCode写者，快照与逐文件复查不能提供针对外部编辑器的全局原子CAS。
外部修改与回滚冲突时保留记录供人工核对，不强行覆盖。文件/目录互换拒绝；非Git `/task`、按需网络、
同run跨进程resume锁与真实模型历史套件仍待做。默认local不因此获得容器隔离。

失败现场：`mindcode-shared-validation.vYNFcS/first-failed-unit` 保留非Git用例误放Git子目录与静态问题；
该目录首轮全量4失败因真实read_file带行号而模拟输出无行号，发布条件未放宽，修正断言后重跑。
`mindcode-shared-final.bQfUUP` 保存补持久化边界前版本；最终以accepted目录为准，不累加各轮数字。

证据 `.codeagent/validation/vm-shared/`：vm-full.xml/log、vm-unit.xml/log、vm-ruff.log、vm-pyright.log、
vm-exit-codes.json、source-manifest.json、vm-cli-smoke.json、vm-cli-dirty-git/plain.log、容器前后清单、
内核/Podman/受信镜像信息与acceptance-summary.json。代码和测试哈希与最终源码一致，后续文档不属于清单。
复现：SSH进入最终VM目录，设置 MINDCODE_PODMAN_TEST_IMAGE 为上述完整镜像ID，用 .venv/bin/python
执行 pytest -q、ruff check . 和 pyright --pythonpath "$PWD/.venv/bin/python"。本轮新改动尚未提交。


## 2026-10-02 追加：非 Git /task 的候选事务

先提交共享交互阶段890bae7，再实现本节。VM mengx@192.168.100.128，内核6.8.0-142-generic，
最终源码在 `/home/mengx/mindcode-nongit-final.PMwLn7`，184文件SHA256逐一核对。运行时、受信镜像与
依赖复用现有资源，没有安装或拉取；原仓库、历史失败现场及各轮新验证目录保留。未使用WSL。

最终VM全量 **475 passed / 1 skipped**（313.682秒），静态检查通过；18个真容器用例通过。
Windows357 / 119，Ruff/Pyright通过。专项40项纳入全量，不与全量相加。真实REPL `/task`写42、普通send
读实际成果、clear、退出通过，RunStore记录success/promoted与snapshot内容摘要，未创建.git，用户seed/env保留。
全量前后与REPL结束容器清单均为空；用Stub固定模型输出，历史真实模型套件本轮未重跑。

新增四个真实Podman用例：非Git两步DAG候选接受/拒绝、全局先拒绝再重跑的追加不重复、执行中取消排空
Worker容器。单元覆盖缺验收、封存失败、Worker异常、验收销毁失败、用户并发编辑、只读无需验收、
并行读写重叠/未知读/不相交集成、resume拒绝且旧记录不变、发布未知、取消状态与整份快照及时释放。

机制：普通文件夹捕获原始有界数据；每Attempt候选、每Worker私有副本；Step集成只改候选。内容摘要冻结后
在独立验收容器检查真实候选，全局门禁接受才用已有日志回写项目。拒绝Attempt重新从原快照开始。
进程取消先取消并await所有正在运行的Worker，再释放私有目录；已集成Worker及时删除目录并释放无引用快照。
源.env/控制面/常见根虚拟环境与缓存不导入；输出过滤发生在候选集成和独立验收前。

边界：snapshot修订与内部引用不是Git对象，不创建Git仓库。非Git任务候选未跨进程持久化，当前明确拒绝
resume并保留旧记录，不用内容相同推断本任务已发布；需要持久化候选和发布回执才能进一步实现。
资源账本回收容器，publication修复文件写回，RunStore记录编排；磁盘staging无跨进程自动回收，C6互斥待做。
逐文件共享发布不提供针对外部编辑器的全局原子CAS；真实冲突/决定未知保留日志供人工核对。

保留首轮单位失败：`/home/mengx/mindcode-nongit-task.9OYfPw/first-failed-unit`，FileState字段名误用与
重载TaskGraph对象按身份比较导致测试失败，类型检查还发现Verifier签名；修正断言与测试协议后39项通过。
该目录首轮全量474 / 1、18真容器通过；最终另补释放测试、取消记录与准确CLI提示后全量475 / 1。
各轮数字不相加，最终以final目录为准。证据 `.codeagent/validation/vm-nongit-task/` 下full/unit XML与log、
静态检查、退出码、源码manifest、环境/镜像、容器清单、CLI-smoke和acceptance-summary。

复现：SSH进入final目录，设置MINDCODE_PODMAN_TEST_IMAGE为前述完整镜像ID，执行.venv/bin/python
的pytest -q、ruff check .、pyright --pythonpath "$PWD/.venv/bin/python"。本节新改动未提交。


## 2026-10-02 追加：真实模型复验与提交前检查

本节在独立 Ubuntu VM `mengx@192.168.100.128` 的
`/home/mengx/mindcode-real-revalidation.XACDvV` 执行，全程未调用 WSL。184 个源码/测试文件
SHA256 校验通过；原仓库仍为 5083609，已有凭据文件与历史 012 的失败产物哈希未变。
历史 10 PASS / 1 FAIL 的报告和 note.txt 保留，不用新结果覆盖旧结论。

真实模型为 VM 已配置的 **deepseek-flash**，强制 `execution_backend=podman`，禁止用 Stub 成绩
代替复验。沿用历史 11 个场景的 DAG、任务目标与产物判据，各跑一次，**11/11 PASS**；默认增加独立
容器 `python -m compileall -q .` 检查，verify_fail/verify_pass 保留原有 false/grep 验收命令。
固定 DAG 的场景仅固定规划，Worker 与本地/全局验证仍调用真实模型；三个自由规划场景使用真实 Planner。

| 场景 | 结果 | 尝试 | stale / 重跑 / Integrator |
|---|---|---:|---|
| `dep_chain` | PASS | 1 | 0 / 0 / 0 |
| `overlap_append` | PASS | 1 | 1 / 1 / 0 |
| `strong_conflict` | PASS | 1 | 1 / 0 / 1 |
| `verify_fail` | PASS | 2 | 0 / 0 / 0 |
| `simple_create` | PASS | 1 | 0 / 0 / 0 |
| `planner_freeform` | PASS | 1 | 0 / 0 / 0 |
| `deep_chain` | PASS | 1 | 0 / 0 / 0 |
| `wide_fanout` | PASS | 1 | 3 / 3 / 0 |
| `verify_pass` | PASS | 1 | 0 / 0 / 0 |
| `planner_trap` | PASS | 1 | 0 / 0 / 0 |
| `split_utils_e2e` | PASS | 2 | 1 / 1 / 0 |


关键负例 verify_fail：两次 Attempt 均有真实 Worker 完成并进入候选，而独立 false 验收拒绝；
integrated=False、base_moved=False，项目没有 note.txt。不能仅凭“没有产物”把调用失败当成正确拒绝。
这次模型轨迹与旧轨迹不同；对旧越界命令的确定性重现仍由 reject_escape 真容器测试覆盖，
本次 PASS 不替代权限、实际内核约束与跨进程回收证据。

额外非 Git 真实模型 **2/2 PASS**：两步依赖链通过后 version.txt=7；同会话普通交互再读取并写
receipt.txt=received-7；负例有完成的候选但 false 验收拒绝，两次 Attempt 后 lib.py/version.txt 均未发布。
两个目录均不创建 .git，用户哨兵 .env 与原文件保留。人工哨兵用于测试，不复制真实凭据到执行域。

验收镜像从既有固定 Python 3.12-slim ID 构建，pytest=9.1.1、packaging=26.3、pluggy=1.6.0、
iniconfig=2.3.0、Pygments=2.21.0；wheel 经 TLS 从 PyPI 获取后记录内容哈希，构建安装断网。
本轮实际固定镜像 ID：`0f8a779567c977206c4e1a3985a37fa46cf25d93d767ed93dd866c8670c140a5`。模型 API 仅在可信控制进程调用；Worker 和验收容器保持断网、
无宿主 bind mount，凭据不导入。镜像制作不是扩大运行时网络权限。

随后使用该镜像重新执行 VM 全量：**475 passed / 1 skipped**，18 个真容器用例全部通过，
Ruff/Pyright通过；Windows重新回归 **357 passed / 119 skipped**，Ruff/Pyright通过。
真实套件与全量前后容器清单均为空，记录到 58 个实际执行域。各类通过数不相加。
普通 pytest 继续不触发真实模型请求；单次项目内套件不能证明模型普遍质量提升或 SWE-bench 表现。

证据 `.codeagent/validation/vm-real-revalidation/`：suite-report.json、nongit-real-report.json、
real-summary.json、real-suite.log、configuration.json、execution-domains.json、evidence-audit.json、
verify-fail-events.json、ordinary-read-audit.json、source-manifest.json、image-inspect.json、image-id.txt、固定依赖/构建与下载日志、
VM全量XML/log、静态检查、退出码及容器清单。完整场景目录和控制面日志留在上述VM目录。
本节文档更新晚于源码打包；源码/测试哈希对应被验收版本，历史阶段记录保持原样。

当前非Git/task提交门槛通过。下一项按实际需求推进B5网络策略；非Git候选持久化/发布回执、磁盘staging
崩溃回收与C6同run恢复锁仍未实现，Phase1动态预算路由、Phase2/3专项未完成。
