# 沙箱与恢复测试

验收基线与完整统计见[最新测试总览](README.md)；本页合并同类判据，不重复列开发阶段的旧全量数字。
运行机制见[沙箱与恢复](../sandbox-and-recovery.md)。

## 隔离与接收门禁

| 测试主题 | 最新套件覆盖的判据 | 用例入口 |
|---|---|---|
| 文件与命令视图 | 同容器写、读、搜索、shell 追加一致，合法结果可封存 | [Podman 集成](../../tests/test_podman_integration.py) |
| 输入与宿主隔离 | 越界路径失败，控制路径/凭据不进入输入，宿主哨兵与 socket 不可见 | [快照](../../tests/test_execution_snapshot.py)、[工作区](../../tests/test_sandbox_workspace.py) |
| 内核与容量 | 只读 root、离线接口、权限/seccomp 与 cgroup 检查；有限 tmpfs 写满拒绝 | [Podman 集成](../../tests/test_podman_integration.py) |
| 快照校验 | 不安全链接、特殊文件、保护路径和超限拒绝 | [快照](../../tests/test_execution_snapshot.py)、[发布](../../tests/test_workspace_publication.py) |
| 销毁与取消 | 等待进程清理，确认容器销毁后接收；失败/取消不回传 | [集成](../../tests/test_podman_integration.py)、[执行器](../../tests/test_execution_process.py) |
| 独立验收 | 拒绝时真实输入不推进，候选成果不发布；正常接受才发布 | [完整验收容器](../../tests/test_verification_podman.py)、[校准门禁容器](../../tests/test_token_calibration_podman.py) |

实际权限和内核探针有独立记录；读取限额或 ENOSPC 断言不等于全面 OOM/fork-bomb 压力测试，也不证明独立 AppArmor profile 保护。

## 交互、候选与文件发布

| 场景 | 判据 | 用例入口 |
|---|---|---|
| 干净 Git 普通交互 | 只读不提交，有改动独立验收后发布；上下文连续而容器不跨轮复用 | [普通交互](../../tests/test_interactive_sandbox.py) |
| dirty Git / 非 Git 普通交互 | 差异回写保留 HEAD 与暂存区，非 Git 不建仓库；prepared 恢复、applied 保留 | [共享交互](../../tests/test_interactive_sandbox.py)、[差异发布](../../tests/test_workspace_publication.py) |
| 非 Git `/task` | 前驱集成后解锁，重叠/未知读集基线变化重跑，冻结候选验收后发布 | [快照任务](../../tests/test_snapshot_tasks.py) |
| 发布失败与外部冲突 | 缺验收、明确拒绝、无法判定、用户修改或决定未知时不覆盖真实输入 | [交互](../../tests/test_interactive_sandbox.py)、[发布](../../tests/test_workspace_publication.py) |

真实 REPL 冒烟覆盖写入后连续读取、`/clear` 和退出，现场记录留在本地。
多个文件分别原子替换，不保证对外部编辑器的全局 CAS，测试也不把项目租约当作外部程序会遵守的锁。

## 受控下载

策略和控制面用例：[下载策略](../../tests/test_download_policy.py)、[受控下载](../../tests/test_controlled_download.py)。
真实 HTTPS 与容器用例：[Podman 下载](../../tests/test_podman_download.py)，**9 个用例包含在 36 个真容器用例中**。

覆盖未授权/空白名单不连接、非法 URL、混合公网/私网 DNS、逐跳重定向、TLS 失败、大小/时间/摘要限制、审计失败或卡死阻止请求，以及取消后无后台写者。
真实容器验证二进制导入、同内容复用、冲突和链接拒绝；下载成功后 shell 直接 TCP 仍失败。
`/task` 与普通交互的接受/拒绝用例确认下载数据仍须独立验收，拒绝结果不发布。

曾发现“一律拒绝二进制”挡住已有下载 SHA256 验收；当前实现使用冻结二进制完整元数据与确定性检查，相关回归已纳入最新套件。
这不表示二进制语义由模型证明，也不表示下载白名单自动保证来源可信。

## 跨进程互斥与恢复

| 测试主题 | 判据 | 用例入口 |
|---|---|---|
| run 租约 | 同 run 第二控制器拒绝，不同 run 可运行，正常/强杀退出后可接管 | [租约](../../tests/test_run_lock.py)、[恢复互斥](../../tests/test_run_lock_recovery.py) |
| SQLite 取消 | 取消与重复取消先排空后台操作，导出期间继续持 run 锁 | [RunStore](../../tests/test_run_store.py)、[恢复互斥](../../tests/test_run_lock_recovery.py) |
| Git 恢复 | 已推进候选不重复发布，自动恢复只清理所属 run 的资源 | [Master 集成](../../tests/test_master_integration.py)、[恢复互斥](../../tests/test_run_lock_recovery.py) |
| 非 Git 持久事实 | 原始快照、候选、目录/策略绑定与回执一致；旧记录、损坏、身份不符拒绝 | [快照存储](../../tests/test_snapshot_store.py)、[快照恢复](../../tests/test_snapshot_recovery.py) |
| 精确 staging 回收 | 只回收已记录且失去 owner 的目录，活动/未知/替换/链接保留 | [staging](../../tests/test_task_staging.py) |
| 容器回收 | 创建意图与身份匹配，控制器崩溃后只清理所属资源 | [资源账本](../../tests/test_sandbox_ledger.py)、[Podman 集成](../../tests/test_podman_integration.py) |
| 延后外部动作 | 成功不重放，幂等有限重试，不可确认状态保留 unknown | [外部动作](../../tests/test_deferred_execution.py) |

**3 个真实 Podman SIGKILL 恢复用例**位于[Podman 恢复](../../tests/test_podman_recovery.py)，覆盖：

1. prepared 日志写入前中断：原输入未变，恢复只发布已验收候选。
2. 首次文件差异写回后中断：先回滚 prepared，再发布候选。
3. applied 已落盘、SQL 回执未完成时中断：补记回执，不重复追加，保留之后人工修改。

每个场景由另一真实进程恢复，再次恢复仍不重复发布；最终 staging 与本次容器资源无残留。
SIGKILL 故障注入验证控制器中断窗口，不覆盖断电、磁盘损坏、网络盘或多主机协调。

## 复现与解释

按[总览](README.md)在新 VM 验证目录设置受信测试镜像后运行全量；单独运行主题文件只表示专项结果，不能替代全量。
mock Provider 固定模型步骤，真实容器与进程负责验证隔离、回收和发布。测试通过不能外推为真实模型语义完全正确或外部 exactly-once。
