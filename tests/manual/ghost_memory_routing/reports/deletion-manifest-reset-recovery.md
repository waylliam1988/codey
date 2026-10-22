# Deletion Manifest Reset Recovery

这是一个离线生命周期实验，验证 `reset_all` 使用 durable deletion manifest 时，七个
Ghost store 的失败、重启恢复和 rebuild 边界。

实验 commit：`7440518b`。

## 目标和配置

每个 store 依次注入一次 reset failure。每个 cell 先 seed 七个真实 store，再执行
`reset_all`，重启后恢复同一 operation 的 pending stores，最后用新的 operation 在故障
解除后完成清理。cells 为七个 store，各 1 次；无模型、无 provider endpoint、无 live send。

## 验证命令和结果

```powershell
python -m pytest tests/test_ghost_deletion_manifest.py tests/test_ghost_deletion_manifest_recovery.py -q --tb=short
python -m mypy codey/ghost/deletion_manifest_coordinator.py tests/test_ghost_deletion_manifest_recovery.py --python-version 3.11
pyrefly check codey/ghost/deletion_manifest_coordinator.py tests/test_ghost_deletion_manifest_recovery.py --python-version 3.11
ty check codey/ghost/deletion_manifest_coordinator.py tests/test_ghost_deletion_manifest_recovery.py
python -m ruff check codey/ghost/deletion_manifest_coordinator.py tests/test_ghost_deletion_manifest_recovery.py
```

结果：`25 passed`；七个 reset failure cell 全部先进入 `blocked`，同一 operation 重启
后只处理 pending，failed/blocked store 调用次数保持 1；新的 reset operation 全部完成，
rebuild 后目标 Ghost 数据没有复活。脱敏指标见
[deletion-manifest-reset-recovery-metrics.json](deletion-manifest-reset-recovery-metrics.json)。

## 当前生产边界

- 是否经过真实 provider-send：否。
- 是否改变默认 prompt：否。
- 支持的模型 / 任务 / 配置：无模型；GhostSQLite 与真实七 store reset/replay。
- 生产合并结论：`EXPERIMENT_ONLY`。

## 限制和下一步

blocked store 在同一 operation 内保持终态，故障解除后需新 operation；实验没有证明默认
control surface 已接入 coordinator，也没有多进程 runtime acceptance。下一步继续做 manifest
并发冷启动和 reset/delete 混合 operation 的 fail-closed 矩阵，保持生产入口不变。
