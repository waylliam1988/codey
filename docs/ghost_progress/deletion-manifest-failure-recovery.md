# Ghost 实验：Deletion Manifest Failure Recovery

- 实验名称：Deletion Manifest Failure Recovery
- 状态：`EXPERIMENT_COMPLETE`（真实 store 离线失败矩阵；协调器仍未接入生产 control surface）
- 负责人：Codex
- 开始 / 更新日期：2026-10-22
- 关联 Roadmap 验收门：0.6.2 Scope Control and Four Learning Switches
- 实验 commit：`a92db8c9`（失败恢复矩阵与 manifest scope 单一来源）
- 生产合并结论：`EXPERIMENT_ONLY`

## 本次目标和假设

在七个真实 Ghost store 上注入一次中途失败，验证 deletion manifest 在失败、重启、恢复和
rebuild 中的行为。假设失败 store 会留下 durable `blocked`，不会伪造整个 operation 完成；
同一 operation 重启时只处理 `pending` store，已完成和 blocked store 不重复执行。故障解除
后用新的 operation 作为显式恢复边界，最终删除并 rebuild 后目标 project 不复活。

## 实际改动

- `DeletionManifestCoordinator.run()` 删除调用方传入的 project/session 参数，只使用已持久化
  manifest 的 scope，消除恢复时换作用域的风险。
- 新增七 store 真实失败、重启、pending 恢复、新 operation 和 projection rebuild 测试。
- 保存脱敏矩阵和离线报告；原始 state、完整日志和运行归档仍只留 ignored artifacts。

## 验证命令和结果

完整命令与脱敏报告见
[deletion-manifest-failure-recovery.md](../../tests/manual/ghost_memory_routing/reports/deletion-manifest-failure-recovery.md)。

结果：受影响 Ghost/SQLite 回归 `76 passed`；mypy、Pyrefly、ty 和 Ruff 对本实验目标文件
通过。七个失败 cell 全部进入 `blocked`；同一 operation 重启后只执行后续 pending，
failed/blocked store 调用次数保持 1；显式 recovery operation 全部完成，rebuild 后没有目标
project 复活。脱敏指标见
[deletion-manifest-failure-recovery-metrics.json](../../tests/manual/ghost_memory_routing/reports/deletion-manifest-failure-recovery-metrics.json)。

## 实验配置

- model / suite / endpoint：无模型；`deletion_manifest_failure_recovery_20261022`；无 endpoint。
- tasks / cells / reps：七个 store 各一个 failure cell，各 1 次；seed → fail → restart/resume
  → new-operation recovery → rebuild。
- on/off 对照：每格只对一个指定 store 注入一次 `OSError`；recovery operation 无故障注入。

## 当前生产边界

- 是否经过真实 provider-send：否。
- 是否改变默认 prompt：否。
- 支持的模型 / 任务 / 配置：无模型；本地真实七 store、GhostSQLite 和 event replay。
- 生产合并结论：`EXPERIMENT_ONLY`。

## 收益、限制和阴性结果

收益是失败不会被错误标记为 completed，重启调度不会重复已完成或 blocked store，且 scope
只由 durable manifest 决定。blocked store 在同一 operation 内保持终态；故障解除后必须
创建新的 operation，这增加了显式恢复步骤。实验没有 provider-send、模型语言收益、成本
或延迟证据，也没有证明默认 control surface 已经使用 coordinator。

## 未完成或阻塞

- coordinator 尚未接入 `GhostControlSurface` 默认 delete/reset 入口。
- 尚未完成多进程 runtime acceptance、reset_all failure matrix 和所有 rebuild 路径的完整
  tombstone 零复活矩阵。

## 下一步

继续做 reset_all 对照和多进程 manifest/runtime acceptance；保持生产 control surface 不变。
只有恢复、删除/rebuild 和并发证据完整后，才评估是否新增生产调度入口。
