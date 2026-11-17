# Ghost 普通 reset 的历史保留基线

- 日期：2026-11-17；冻结生产基线：`0cdb9137`。
- 状态：`EXPERIMENT_COMPLETE`（有效基础设施负结果）；关联 0.6.3。
- 模型与 endpoint：不使用。本实验验证存储合同，不恢复已停止的语言批次。

## 冻结目标与矩阵

普通控制面 reset_all 的 Manifest 声明 retention_class=tombstone；路线图要求保留原始领域
事件，物理 purge 为独立操作。假设普通 reset 在清空消费者可见状态后，仍保留原领域历史。

七个存储 × user/project/session 三种 seeded 来源 scope × 只读重开/reset后重开/reset后重建，
共63项确定性cell，每项一次。reset本身scope=all；这里的scope仅指准备的来源范围。
复用已验证的合法seed与既有reducer；不绑定应用、不伪造receipt、不修改实际用户state。

主要变量为普通控制面reset是否执行，以及随后是否调用既有reducer重建。
每项首先检查原历史非空、消费者可见；reset项还检查普通dispatch成功、Manifest completed且
tombstone、reset后不可见、重开/重建后不可见。只有到达最后原始SQL历史前缀断言，才记为
有效历史保留负结果。比较原sequence、event_id及payload，observation比较完整原始行。
主指标为原始历史改写cell数，目标0；对照为21项只读重开，处理为42项reset。

准备或入口错误单独保留，不能混入历史负结果；完成63项后停止采样，保留失败证据并判断归属。
本矩阵不证明高容量reset、损坏元数据恢复、跨版本并发、物理purge或完整生命周期。

## 冻结命令

```powershell
python -m pytest tests/test_ghost_reset_history_retention.py -q --tb=short --junitxml=.e2e-artifacts/ghost-reset-history-20261117/baseline.xml
```

## 结果

63项全部有效：**21 passed / 42 failed**。21项只读重开对照全通过；七存储、三来源scope、
两种reset后消费方式的42项全部在最后历史前缀断言失败。入口成功、Manifest completed且
tombstone、reset后不可见、重开/重建后不可见均先通过；不声称本轮发生删除来源复活。

归属为生产reset物理删除领域事件，违背本入口声明的tombstone合同，不通过修改历史保留
断言、改成purge标签或关闭reset掩盖。最小候选为保留原领域日志，原子清空派生状态并将
现有重放窗口推进至领域尾部；仍需保留身份退役、设置和Manifest确认/恢复语义。

[脱敏逐cell报告](../../tests/manual/ghost_memory_routing/reports/reset-history-retention-baseline-20261117.json)
不含原始事件正文、临时路径或state；完整JUnit、日志和失败state仅留本地
`.e2e-artifacts/ghost-reset-history-20261117/`及pytest保留目录。

既有相关回归 **287 passed**，无失败：

```powershell
python -m pytest tests/test_ghost_scope_history_retention.py tests/test_ghost_event_windows.py tests/test_ghost_graph_sqlite.py tests/test_ghost_corruption_contract.py tests/test_ghost_manifest_production_wiring.py tests/test_ghost_manifest_atomic_mutation.py tests/test_experience_memory.py -q --tb=short --junitxml=.e2e-artifacts/ghost-reset-history-20261117/baseline-related.xml
```

随后串行执行全树Ruff、mypy、Pyrefly、ty及kernel precision gate，全部通过；mypy检查702个
生产模块，Pyrefly为0 errors、35项既有suppressed、158 warnings。新增测试文件另经三种类型
检查通过，额外mypy `--disallow-any-expr` 的owned诊断为0；未修改依赖187文件4065项诊断
使扩展命令exit1，不能称为全树Any审计通过。日志在同目录`baseline-{ruff,mypy,pyrefly,ty,
precision,owned-mypy,owned-pyrefly,owned-ty,owned-any}.txt`。

未运行全量pytest、未调用模型。下一步生产修复独立提交，不把本次负结果记为生产晋级。
