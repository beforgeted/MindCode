"""可复现的真实-LLM 场景套件（知识文档"验证要针对真实产物"的落地）。

每个 Scenario = 初始仓库快照 + /task 目标 + 确定性判据。runner 在隔离 git sandbox 里
跑真实 MasterRuntime，跑完对**真实 base**（promote 后的工作树）打分，并汇总收敛指标
（reruns / stale / integrations / attempts / conflicts）。

这是测试金字塔的第 ② 层：单测守控制面不变式（确定性），本套件守"真实模型下四层收敛
是否按预期工作、改 prompt/换模型后有没有退化"。正式 benchmark（SWE-bench 子集）是第 ④ 层，
待核心稳定后再上。

用法：
    python -m scenarios.runner                 # 跑全部，各 1 次
    python -m scenarios.runner --only dep_chain overlap_append
    python -m scenarios.runner --repeat 3      # 每个场景跑 3 次看稳定性
    python -m scenarios.runner --keep          # 保留 sandbox 供排查
"""
