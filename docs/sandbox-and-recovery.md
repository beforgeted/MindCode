# 沙箱、发布与恢复

本文合并容器、受控下载、共享工作区、恢复锁与非 Git 恢复说明，描述当前实现。
同类测试统一见[沙箱与恢复测试](testing/sandbox-and-recovery.md)，完整结果见[测试总览](testing/README.md)。

## 执行后端与环境

默认 `local` 后端在宿主执行；选择 `podman` 后要求 Linux rootless Podman、cgroup v2 和 CPU/memory/pids 控制器。
镜像须由操作者预先准备并信任，包含 Python、`/bin/sh` 和任务依赖，使用完整 SHA256 镜像 ID。
后端不自动拉取镜像，配置或内核检查失败时拒绝运行，不自动降级到本机。

在独立 Ubuntu 虚拟机中配置：

```bash
export CODEAGENT_EXECUTION_BACKEND=podman
export CODEAGENT_SANDBOX_IMAGE='<已安装的完整 SHA256 镜像 ID>'
export CODEAGENT_VERIFY_CMD='python -m pytest -q'
python -m codeagent.cli.app --workspace /path/to/project
```

Linux 测试禁止使用 WSL Ubuntu。连接账号、地址和原仓库路径只保存在本地隐私文档。

## 容器与快照边界

`read_file`、`write_file`、`grep` 和 `run_command` 共用当前容器 `/workspace`，新自定义工具须先适配沙箱。
Memory、Evidence、Artifact 的固定读取接口由控制面提供。
输入为有界数据快照，排除 `.git`、`.codeagent`、`.env` 等控制与凭据路径；不向容器挂载宿主 worktree。

容器默认无网络、根文件系统只读，项目和临时目录使用限额 tmpfs。
导入项目前检查实际 UID、capabilities、NoNewPrivs、seccomp、网络接口与 cgroup 限额。

| 限额 | 默认 |
|---|---:|
| CPU / 内存 / 进程 | 1 CPU / 512 MiB / 64 |
| workspace / 临时目录 | 256 MiB / 64 MiB |
| 单命令 / 输出 | 60 秒 / 4 MiB |
| 快照文件与目录数 | 4096 |
| 单文件 / 快照内容总量 | 8 MiB / 64 MiB |

Python 调用方可通过 `AppConfig.sandbox_limits` 调整容器限额。资源文件检查与写满测试不等于全面 OOM/fork-bomb 压力测试；容器共享 VM 内核。

## 接收与发布成果

Worker 和局部验收成功后，冻结容器 → 可信导出 → 确认销毁 → 校验快照 → 写入私有候选。
链接、特殊文件、保护路径、非法容量以及 `.gitattributes` / `.gitmodules` 改动会被拒绝。
失败、取消、封存或删除失败均不发布输出。

确定性验收在独立容器运行，其文件修改不回传。退出码失败或沙箱异常阻止发布；全局验收还须通过且可判定。

| 工作区 | 发布行为 |
|---|---|
| Git `/task` | 私有 worktree 集成到 candidate，验收后 CAS 推进真实 base |
| 干净 Git 普通交互 | 每轮隔离候选；有变更时独立验收，候选提交后 CAS fast-forward |
| 含未提交修改的 Git 普通交互 | 当前文件快照，验收后回写差异，保留暂存区与 HEAD，不自动提交 |
| 非 Git 普通交互 / `/task` | 数据快照与差异发布，不创建 Git 仓库 |

含写入的沙箱任务必须配置 `CODEAGENT_VERIFY_CMD`。前驱集成后才解锁后继；候选变化而 Worker 读集未知或重叠时重跑。
普通交互容器和执行预算不跨轮复用，上下文保留；`/clear` 只重置对话上下文。
`sandbox_turn_finished` 表示发布结果，不能用模型回复替代它。

共享写回持有项目租约，先落盘 prepared 日志，逐文件原子替换并 fsync，再写 applied 决定。
恢复回滚未完成写回；已 applied 的成果保留。冲突、损坏或决定未知时保留记录供核对。
该锁不能约束外部编辑器，多个文件的可见性也不是全局原子事务；文件与目录互换暂时拒绝。

## 受控 HTTPS 下载

配置精确小写 DNS 主机白名单后才注册 `download_file(url, sha256, path)`：

```bash
export CODEAGENT_DOWNLOAD_HOSTS='files.pythonhosted.org'
# 默认 prompt：交互逐次审批，非交互拒绝。
# 受信自动化可显式设置 CODEAGENT_DOWNLOAD_APPROVAL=allowlist。
```

下载由可信控制面隔离子进程执行，容器仍断网。只允许 HTTPS:443 GET，无凭据、query、fragment 或任意请求头。
每个跳转重新检查 URL、白名单和公网 DNS，连接固定到已检查 IP，TLS 按原域名校验。
默认最多 8 MiB、30 秒和 3 次跳转；子进程不继承 API 密钥或代理环境。

请求前持久审计意图，等待上限 5 秒；审计失败或卡死时不发请求。结束记录跳转、IP、状态和字节，不记录正文。
SHA256 校验通过后才导入容器；目标为受校验相对路径，同内容可复用，不覆盖不同内容或跟随链接。
下载成果仍经过独立验收；`downloaded` 只表示字节已校验，摘要本身不是供应链认证。

此能力不开放 shell 网络、在线 pip/npm、POST 或通用 TCP。已发出的 GET 不能被候选回滚撤销，通用出口策略尚未实现。

## 三种租约与资源回收

| 租约 / 账本 | 职责 |
|---|---|
| 同 run 内核租约 | 新任务与恢复在读状态前持锁，覆盖编排、验收、发布、外部动作及导出 |
| 项目 publication 租约 | 串行化 MindCode 共享目录写回与恢复 |
| 容器 / staging-owner 租约与资源账本 | 记录创建意图和身份，精确回收已失去所有者的资源 |

run 锁基于规范 SQLite 路径共享命名空间；Linux 用 flock，Windows 用非阻塞字节锁。
竞争者立即返回忙碌，锁不可用时拒绝；锁文件保留，不用 PID/TTL 猜测接管，也不删锁文件抢占。
SQLite 取消和重复取消先排空后台线程，再释放租约。不同 run 的执行锁不相互阻塞。

容器创建前记意图，恢复核对项目、标签和身份，只删除同账本且失去所有者的容器。
staging 先持 owner 租约、记意图，再创建私有目录并绑定 device/inode；候选、Worker、validation 和交接临时目录均属于 owner 根。
活动/未知资源、替换路径、链接、身份不符或删除失败保留，不扫描 `/tmp` 或所有容器猜归属。
Master 只回收当前 run 已记录的 Git 分支/worktree，不对项目执行全局 prune。

## 非 Git 持久化恢复

默认 SQLite 装配持久保存原始输入、最新冻结候选、目录身份/策略绑定及发布回执。
快照修订为 `snapshot:<SHA256>`，不能套用 Git HEAD 判定。

| 持久事实 | `/task --resume` 行为 |
|---|---|
| 尚未 PROMOTING，目录仍等于原输入 | 使用保存 DAG，从原始输入重跑未完成 Attempt |
| PROMOTING，候选/绑定匹配，尚未发布 | 复用已验收候选，只重试发布 |
| 有匹配 applied 文件交接记录 | 补记 SQLite FULL 回执，再退休日志，不重复回写 |
| SQL 回执已 applied | 返回完成结果，保留发布后的人工修改 |
| 缺快照、损坏、身份不符或无回执但目录已变化 | 拒绝，保留文件与记录 |

prepared 未完成的日志先回滚；applied 文件决定在 SQL 确认前不删除，避免跨存储窗口丢失发布事实。
未确认的交接会阻止其他任务写入，提示先恢复所属 run。NullRunStore 或缺快照适配器的定制存储不支持该恢复。
每 run 仅保留原始与最新快照，跨 run 归档与配额尚未实现。

## 延后外部动作

`local` 模式可在发布前持久化动作、发布后逐项审批。pending 重新审批；succeeded 不重放；running 中断仅在幂等且有剩余预算时重试，否则 unknown；failed/skipped/unknown 不自动重试。
`NEVER` 最多一次，`IDEMPOTENT` 累计最多两次。执行后失败不回滚已经发布的代码，外部操作没有 exactly-once 保证。
Podman 模式不会将 post-promote 外部动作交给宿主执行，中断的未知状态继续保留。

上述恢复保证限于同主机、同规范状态库路径和受保护的本地目录，不包含分布式锁、网络盘、停电或硬件故障保证。
