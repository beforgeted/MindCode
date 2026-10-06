# Knowledge 功能与检索验收

当前对象为 `dev/agent-ecosystem` 中新增的 Knowledge 候选，实现保存在功能分支，尚未合入主线。使用说明见[Knowledge](../knowledge.md)。全量源码冻结267文件，清单 SHA256 `e4492177600f7393e2ccaa8b7bade5289050bf8746808d3f412b0cbde874d523`；252代码文件清单 `bd61cbfd9b7061f1d79cc19122df1a971fd737d96aaebf02e1cbaf345b7d595f`。验收后仅修正评估器的Windows路径统计，251个其它代码/测试文件未变，单独复验修正后的双平台检索对照及静态检查；最终252代码清单 `ed3dc650d8f42c8d9a20dc6508722d9387f5de7739db91bd64fdaf81f277af87`。

| 层次 | 判据 |
|---|---|
| 基础查询 | 路径、限定 Python 类/函数、中文文档段落；结果行号与真实文本对应 |
| 版本与增量 | 相同 mtime 的改动、删除、重命名、模拟分支内容切换；未变文件复用，旧引用及 expected_version 拒绝 |
| 权限与隐私 | 隐藏/凭据/忽略路径在读取前排除；链接拒绝；未知参数在进程启动前拒绝；Agent 白名单与默认关闭 |
| 真实容器 | 读取候选而非宿主；实际命令修改/删除/重命名后更新；双 Worker 缓存隔离；关闭句柄拒绝读取；seal 无索引文件 |
| 编排发布 | 固定模型驱动真实 Worker 写后查；独立验收通过才发布，失败保持原 HEAD |
| 检索对照 | 在冻结的真实 MindCode 源码上执行同字面查询；与现有 grep 比较来源命中、引用有效性、输出估算 Token、调用数及耗时 |

```bash
python -m pytest -q tests/test_knowledge.py
# 以下仅在独立 Ubuntu VM，显式设置已安装可信镜像的完整 SHA256 ID：
python -m pytest -q tests/test_knowledge_podman.py
python -m scenarios.knowledge_evaluation --root <冻结MindCode源码目录> --output <新私有输出目录>
```

基准包含 easy→medium→hard 六个明确查询，期望文件由评估器独立声明，不反馈给查询工具；全部返回引用再次读取，并核对实际 SHA256 和完整行文本。相同 query 作为 grep 的字面正则输入，多词项查询不会替 grep 自动编写更好的正则，因此该对照不是穷尽专家 grep 策略的比较。输出估算调用既有 TokenEstimator，不是模型实际计费 Token；getter 验证调用与搜索调用分开计数，不宣称端到端任务质量或消融收益。

原始日志、候选摘要和评估报告保存在已忽略的 `.codeagent/validation/knowledge/` 与本地私有验证目录。测试使用仓库外独立临时目录，防止非 Git 测试误识别父仓库；历史失败记录保留。Linux 只在独立 Ubuntu VM 验证，没有 WSL 或付费模型调用。

## 最新验收结果

| 环境 | 收集 | 通过 | 跳过 | 静态检查 |
|---|---:|---:|---:|---|
| Windows | 1105 | 883 | 222 | Ruff/Pyright通过 |
| 独立 Ubuntu VM | 1105 | 1104 | 1 | Ruff/Pyright通过 |

Knowledge专项 **55/55**（46非容器、9真实Podman），包含于全量。Windows该模块44通过、11跳过（9容器、2符号链接），不是完整Linux隔离验收。全量共93个真实Podman用例。原仓库与历史记录不变，容器清单为空，付费调用0；Windows既有社区包缺脚本的边界见[生态验收](tools-and-skills.md)。

双平台检索对照使用完全相同的 **266文件、1661203字节**公开源码内容，六项查询均为Top-1 **4/6**、Top-5 **6/6**、引用 **27/27** 有效。首次解析266文件，后续查询复用266文件，但仍重新读取/哈希整个范围。

| 查询 | 层次 | 期望文件 | 索引Top-1 / Top-5 | grep含期望路径（双平台） | Windows索引/grep估算输出Token |
|---|---|---|---|---|---:|
| `SandboxExecutor` | easy | `codeagent/tool/executor.py` | 是 / 是 | 是 | 1679 / 1930 |
| `read_project_file` | easy | `codeagent/tool/mcp/project_server.py` | 是 / 是 | 是 | 772 / 93 |
| `ControlledFetcher` | medium | `codeagent/execution/fetch.py` | 是 / 是 | 是 | 1955 / 367 |
| `SnapshotEntry` | medium | `codeagent/execution/snapshot.py` | 是 / 是 | 是 | 1201 / 2286 |
| `Fetch robots` | hard | `docs/mcp-tools.md` | 否 / 是 | 否 | 1814 / 41 |
| `候选` | hard | `docs/architecture.md` | 否 / 是 | 是 | 1690 / 7778 |

索引六次搜索、27次独立引用验证；grep六次查询。索引返回5项、grep最多200条，输出预算不同。这批索引查询耗时高于grep，部分查询输出也更多；单次时间在共享测试资源下取得，不能据此承诺任意规模速度或模型成本下降。完整各平台时间、输出估算及缓存统计留在原始报告，不把这些数字当成真实模型消融收益。

VM归档含清单共 **30文件**，SHA256 `9228680357ae89981f2c3d9bc1064394acf1369040666b507f0e21a296ee8d03`，下载后逐项验证。首次Windows夹具因父目录不存在未进入实际测试；仓库内临时目录导致非Git夹具误识别；VM首次快照断言误比较条目顺序。均保留失败现场，最终候选复验通过。全量之后发现Windows评估器比较了POSIX路径与原生反斜杠路径，修正统计后分别复验，没有修改生产代码或测试用例，也没有拿旧错误统计冒充结果。
