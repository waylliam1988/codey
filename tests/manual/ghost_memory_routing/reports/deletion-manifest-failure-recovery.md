# Deletion Manifest Failure Recovery

这是一个离线生命周期实验，验证七个现有 Ghost store 在删除操作中途失败时的
durable manifest、重启恢复、pending 调度和 replay/rebuild 边界。

实验 commit：`a92db8c9`。

## 目标和假设

每个 store 依次注入一次 bounded failure。假设 manifest 会在该 store 处保存
`blocked`，不会伪造整个删除成功；重启后只调用同一 operation 的 `pending` store，
已完成和 blocked store 不重复调用。故障解除后用新的 operation 作为显式恢复边界，
最终所有 store 删除并 rebuild 后目标 project 不应复活。

## 配置和对照

- model / suite / endpoint：无模型；`deletion_manifest_failure_recovery_20261022`；无 endpoint。
- tasks / cells / reps：七个 store 各一 cell，各 1 次；每格先 seed 七 store，再注入一个失败，
  重启 resume，最后执行一个无故障 recovery operation。
- on/off：每格只有一个指定 store 在第一次调用抛出 `OSError`；recovery operation 不注入故障。
- store：inbox、hebbian、continuity、sleep、work_queue、affinity、observations。

## 验证命令和结果

```powershell
python -m pytest tests/test_ghost_deletion_manifest.py tests/test_ghost_deletion_manifest_recovery.py `
  tests/test_ghost_sqlite_store.py tests/test_ghost_control_surface.py tests/test_ghost_controls.py `
  tests/test_ghost_corruption_contract.py tests/test_ghost_graph_sqlite.py -q --tb=short
python -m mypy codey/ghost/deletion_manifest.py codey/ghost/deletion_manifest_store.py `
  codey/ghost/deletion_manifest_coordinator.py codey/ghost/sqlite.py `
  tests/test_ghost_deletion_manifest_recovery.py --python-version 3.11
pyrefly check codey/ghost/deletion_manifest.py codey/ghost/deletion_manifest_store.py `
  codey/ghost/deletion_manifest_coordinator.py codey/ghost/sqlite.py `
  tests/test_ghost_deletion_manifest_recovery.py --python-version 3.11
ty check codey/ghost/deletion_manifest.py codey/ghost/deletion_manifest_store.py `
  codey/ghost/deletion_manifest_coordinator.py codey/ghost/sqlite.py `
  tests/test_ghost_deletion_manifest_recovery.py
python -m ruff check codey/ghost/deletion_manifest.py codey/ghost/deletion_manifest_store.py `
  codey/ghost/deletion_manifest_coordinator.py codey/ghost/sqlite.py `
  tests/test_ghost_deletion_manifest_recovery.py
```

结果：专项与受影响回归 `76 passed`。七个失败 cell 全部先进入 `blocked`；同一
operation 重启后只运行其余 `pending` store，failed/blocked store 调用次数保持 1；
新的 recovery operation 全部完成。重启后对 hebbian、continuity、work_queue、affinity
执行 rebuild，七个 store 均未复活目标 project。脱敏指标见
[deletion-manifest-failure-recovery-metrics.json](deletion-manifest-failure-recovery-metrics.json)。

## 结果、限制和生产边界

收益是删除失败不会被错误报告为 completed，且恢复调度不会重复执行已完成或 blocked
store。协调器现在只从 durable manifest 读取 project/session scope，调用方不能在恢复时
换作用域。实验没有 provider-send、模型行为、成本或延迟证据，也没有证明 tombstone 在
尚未接入 coordinator 的 production control surface 中自动生效。

- 是否经过真实 provider-send：否。
- 是否改变默认 prompt：否。
- 支持的模型 / 任务 / 配置：无模型；真实七个 Ghost store 的本地 SQLite/event replay。
- 生产合并结论：`EXPERIMENT_ONLY`。

## 未完成和下一步

blocked store 在同一 operation 内是终态；故障解除后的恢复必须创建新的 operation，
这是当前候选的明确人工恢复边界。尚未把 coordinator 接入默认 control surface，尚未完成
多进程 runtime acceptance、tombstone 跨所有 rebuild 路径的完整矩阵和 reset_all 对照。
