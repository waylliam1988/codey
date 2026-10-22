# Ghost 实验：Deletion Manifest Reset Recovery

- 实验名称：Deletion Manifest Reset Recovery
- 状态：`EXPERIMENT_COMPLETE`（真实 store 离线 reset failure 矩阵；未接入生产调度）
- 负责人：Codex
- 开始 / 更新日期：2026-10-22
- 关联 Roadmap 验收门：0.6.2 Scope Control and Four Learning Switches
- 实验 commit：`7440518b`
- 生产合并结论：`EXPERIMENT_ONLY`

## 本次目标和假设

验证 `reset_all` 使用同一 deletion manifest owner 时，任意一个真实 store 失败都不会被
错误标记为 completed；重启只处理 pending，completed/blocked 不重复，故障解除后新的
operation 能完成 reset，projection rebuild 不复活数据。

## 实际改动

新增七个真实 store 的 reset failure、重启 pending 恢复、新 operation recovery 和 rebuild
测试。没有接入 `GhostControlSurface` 默认 reset 入口，也没有新增生产 fallback。

## 验证命令和结果

详见离线报告
[deletion-manifest-reset-recovery.md](../../tests/manual/ghost_memory_routing/reports/deletion-manifest-reset-recovery.md)。

结果：专项与 deletion manifest 基础测试 `25 passed`；mypy、Pyrefly、ty、Ruff 全部通过。
七个 failure cell 均为 `blocked → pending-only resume → new operation completed`，rebuild
后无目标 Ghost 数据复活。脱敏指标见
[deletion-manifest-reset-recovery-metrics.json](../../tests/manual/ghost_memory_routing/reports/deletion-manifest-reset-recovery-metrics.json)。

## 实验配置

- model / suite / endpoint：无模型；`deletion_manifest_reset_recovery_20261022`；无 endpoint。
- tasks / cells / reps：seed → reset failure → restart/resume → recovery reset → rebuild；7 × 1。
- on/off 对照：每格仅指定一个 store 第一次 reset 抛出 `OSError`；恢复 operation 无故障。

## 当前生产边界

- 是否经过真实 provider-send：否。
- 是否改变默认 prompt：否。
- 支持的模型 / 任务 / 配置：无模型；真实七 store 和 GhostSQLite。
- 生产合并结论：`EXPERIMENT_ONLY`。

## 未完成或阻塞

仍缺多进程 manifest/runtime acceptance、混合 delete/reset 并发矩阵，以及 coordinator 接入
默认 control surface 后的真实 runtime 回归。blocked store 需要新的 operation 才能恢复，
这是当前候选的显式恢复边界。

## 下一步

推进 manifest 并发冷启动与混合 operation 的 fail-closed 实验；生产入口保持不变。
