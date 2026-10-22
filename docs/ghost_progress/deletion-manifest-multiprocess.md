# Ghost 实验：Deletion Manifest Multiprocess Cold Start

- 实验名称：Deletion Manifest Multiprocess Cold Start
- 状态：`EXPERIMENT_COMPLETE`（8 次独立并发复验；仍是候选实验）
- 负责人：Codex
- 开始 / 更新日期：2026-10-22
- 关联 Roadmap 验收门：0.6.2 Scope Control and Four Learning Switches
- 实验 commit：`1418ed90`
- 生产合并结论：`EXPERIMENT_ONLY`

## 本次目标和假设

让 7 个独立进程同时冷启动同一个 GhostSQLite，并对同一个 deletion operation 并发写入
各自 store transition。假设 schema 初始化不会读到半成品，manifest/event stream 在进程
竞争后仍能完整 replay 为 completed。

## 实际改动

新增 Windows `spawn` 多进程测试：7 worker 同时执行 manifest `start`，随后各自提交一个
不同 store 的 completed event。测试不改变生产代码或默认 prompt。

## 验证命令和结果

详见离线报告
[deletion-manifest-multiprocess.md](../../tests/manual/ghost_memory_routing/reports/deletion-manifest-multiprocess.md)。

8 次独立 repetition 均通过：每次 0 个子进程错误、最终 `completed`、8 条完整事件。mypy、
Pyrefly、ty、Ruff 对目标文件全部通过。脱敏指标见
[deletion-manifest-multiprocess-metrics.json](../../tests/manual/ghost_memory_routing/reports/deletion-manifest-multiprocess-metrics.json)。

## 实验配置

- model / suite / endpoint：无模型；`deletion_manifest_multiprocess_20261022`；无 endpoint。
- tasks / cells / reps：7 workers × 1 operation × 8 reps。
- on/off 对照：无模型 on/off；同一 manifest 的并发 `start` 与不同 event append。

## 当前生产边界

- 是否经过真实 provider-send：否。
- 是否改变默认 prompt：否。
- 支持的模型 / 任务 / 配置：Windows spawn、GhostSQLite manifest/event owner。
- 生产合并结论：`EXPERIMENT_ONLY`。

## 未完成或阻塞

尚未覆盖真实 delete/reset store mutation 与 manifest writer/reader 同时运行、并发失败注入、
混合 operation 和 runtime acceptance；当前没有生产消费者或模型收益证据。

## 下一步

扩大并发 failure/recovery 矩阵；若出现 schema lock 或格式错误，停止重复重试并单独记录为
基础设施故障，不计作 Ghost 行为阴性。
