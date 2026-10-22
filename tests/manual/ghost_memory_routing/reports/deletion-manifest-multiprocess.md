# Deletion Manifest Multiprocess Cold Start

这是一个离线并发实验，验证 7 个独立进程同时冷启动同一个 `ghost.sqlite3`，并对同一
operation 并发提交不同 store 的 transition event 时，schema、manifest 和事件流不会损坏。

实验 commit：`1418ed90`。

## 目标和配置

- model / suite / endpoint：无模型；`deletion_manifest_multiprocess_20261022`；无 endpoint。
- tasks / cells / reps：7 worker、同一 operation、每个 worker 一个 distinct store event；8 次
  独立 repetition。
- 对照：同一 manifest、同一 scope、不同进程并发 `start`/`append`；不是模型行为 A/B。

## 验证命令和结果

```powershell
python -m pytest tests/test_ghost_deletion_manifest_multiprocess.py -q --tb=short
```

该命令在 8 个独立 repetition 中均为 `1 passed`。每次都得到 0 个子进程错误、最终
manifest `completed`、完整 8 条事件（1 started + 7 transitions）。目标文件的 mypy、
Pyrefly、ty、Ruff 也全部通过。脱敏指标见
[deletion-manifest-multiprocess-metrics.json](deletion-manifest-multiprocess-metrics.json)。

## 当前生产边界和限制

- 是否经过真实 provider-send：否。
- 是否改变默认 prompt：否。
- 支持的模型 / 任务 / 配置：无模型；Windows spawn 多进程与 GhostSQLite manifest owner。
- 生产合并结论：`EXPERIMENT_ONLY`。

这证明了当前 manifest schema 初始化和同一 operation 事件并发的本地收敛，不能替代真实
runtime 中 delete/reset 与其他 Ghost writer 同时运行的 acceptance，也没有 provider 或
模型收益证据。

## 下一步

扩大到 delete/reset 混合 operation、并发失败注入和 runtime writer/reader 对照；保持默认
control surface 不接入 coordinator。
