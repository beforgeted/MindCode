# C6：同 run 跨进程恢复互斥

同一 `SqliteRunStore` 的同一个 `master_run_id` 只允许一个控制器调用持有执行权。
锁从新任务规划或恢复记录读取之前取得，覆盖编排、孤儿清理、验收、promote、延后外部动作、
轨迹导出及退出清理。竞争者立即返回未接受/未集成，不读取恢复记录、不执行动作、不导出同名轨迹。
锁不可用时拒绝运行；不等待、不抢占、不以超时或 PID 猜测控制器是否已死亡。

## 实现与命名

SQLite 路径在构造时解析成绝对路径；旁边的 `.<数据库文件名>.run-locks/` 是锁命名空间。
文件名为 run ID 的完整 SHA256，避免将命令行 ID 用作文件路径。Linux 用非阻塞 `flock`，
Windows 用 `msvcrt.locking` 的非阻塞单字节锁；内核保持执行权，控制器关闭描述符或退出后释放。
描述符不继承到普通子进程。锁文件保留，不在释放时删除，避免新旧 inode 同时被持锁。
同 DB 不同 run 的锁互不排斥；这里没有将所有任务串行化，也没有新增恢复状态权威。

Linux 要求目录/文件私有、当前 UID 所有；拒绝目录链接、文件链接/多硬链接/非普通文件。
Windows 校验稳定普通文件与链接，但依赖操作者保护控制目录 ACL。控制目录不能被模型修改。
内核锁失败不退回进程内锁。默认非持久化 NullRunStore 仅有同 Runtime 进程内互斥；
自定义持久化 RunStore 必须显式注入共享命名空间的 `RunLeaseManager`，不能自动宣称有跨进程保证。

## 生命周期与清理

取消 asyncio 协程不会停止 `to_thread` 的 SQLite 操作。RunStore 在取消及重复取消后先等待该线程
结束，才释放 asyncio 互斥和外层 run 租约；不让旧线程在新控制器接管后继续提交状态。
短 SQLite 事务仍只覆盖一次读写，不在等待模型或审批期间保持数据库写锁。

取得 run 租约并不授权清理整个项目。Master 恢复只回收 RunRecord 中记录的 candidate/Worker 分支，
并核对 worktree 位于配置的私有目录内；不做项目全局 prune，不删除其他 run 的候选或未知归属的
detached/尚未记账资源。未知资源保留，完整 Git 资源创建意图/回收账本属于后续增强。

## 保证边界

- 范围是同一主机、同一规范数据库路径和受保护的本地控制目录；不支持跨主机分布式锁、网络盘保证，
  也不把复制/硬链接到不同路径的状态库当成同一命名空间。
- 内核锁解决两个控制器并行恢复；P8 的 PROMOTING/HEAD 判断继续解决合并结果窗口，A2 的持久化
  running/unknown/尝试预算继续解决外部动作未知结果。互斥不等于外部 exactly-once。
- SIGKILL 后控制器锁释放，不证明所有旧子进程均已退出；进程树终止、Podman 资源租约与恢复判断
  仍各负其责。非幂等动作未知时仍不自动重放。
- C6只负责互斥；非Git默认SQLite装配已补齐持久化恢复与staging回收，见NON_GIT_RECOVERY_DESIGN.md。
- 原始 `reclaim_orphans` 的显式无范围调用仍是人工/测试使用的清理 API；Master 自动恢复始终传入
  当前 run 的已知分支集合，不把缺失归属当成可删证据。

## 验收判据

分别在 Windows 与独立 Ubuntu VM 验证真实进程持锁、不同 run 不阻塞、正常退出与强杀释放。
真实双进程恢复一个已 promote 且有待执行 append 的 run：A 在审批时持锁，B 被拒绝且不改状态；
A 完成后再次恢复只保留一次 append；A 在请求发出前被强杀后，新控制器能接管并执行一次。
这组强杀覆盖的是执行前窗口，执行后结果未落盘的 unknown 判据继续由既有 A2 故障注入覆盖。
另测取消/重复取消时后台 SQLite 操作排空、导出期持锁、内核锁故障拒绝，以及恢复不误删其他 run。

实现入口：`orchestration/run_lock.py`、`master_runtime.py`、`run_store.py` 和
`workspace/git_worktree.py`。机制参考 Python 官方
[flock](https://docs.python.org/3/library/fcntl.html) 与
[Windows 字节锁](https://docs.python.org/3/library/msvcrt.html)。

## 当前验收结果

独立 Ubuntu VM **580 passed、1 skipped**，27 个真容器用例通过；Windows **449 passed、132 skipped**，两端 Ruff/Pyright 通过。18个C6用例计入全量；独立VM恢复专项41项不另加总。
最终VM现场 `/home/mengx/mindcode-c6-final.LEwEue`，本地证据在
`.codeagent/validation/vm-c6-final/`。本期使用确定性步骤/恢复记录，不宣称有新真实模型质量证据。
以上是C6阶段证据；后续非Git恢复已完成，最新数字见LINUX_SANDBOX_ACCEPTANCE.md末节。新改动尚未提交。
