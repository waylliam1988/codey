# Ghost 实验：Deletion Manifest Candidate

- 实验名称：Deletion Manifest Candidate
- 状态：`EXPERIMENT_COMPLETE`（持久化候选；未接入生产 store 调度）
- 负责人：Codex
- 实验 commit：`76a3472f`（纯状态机）；`89e0cfd8`（SQLite 持久化与重启复放）
- 关联 Roadmap 验收门：0.6.2 Scope Control and Four Learning Switches
- 生产合并结论：`EXPERIMENT_ONLY`

## 本次目标和假设

为 scope delete/reset 设计一个 control-plane deletion manifest：它必须携带 operation id、
scope、requested time、retention class、tombstone digest 和每个 store 的状态；同一事件重放
必须幂等，顺序错误、schema 错误和冲突事件必须 fail closed。先验证纯状态机，避免把未验证
的跨 store 事务直接接入 Ghost production stores。

## 实际改动

- `codey/ghost/deletion_manifest.py` 保持纯状态机，并修正 blocked store 在后续其它 store
  转换后不会被错误覆盖为 pending/completed。
- 新增 `codey/ghost/deletion_manifest_store.py`，把 manifest 和事件流持久化到同一个
  `ghost.sqlite3` 控制面 owner；同一 operation id、event id 重试幂等，存储内容与事件流
  不一致时 fail closed。
- Ghost SQLite 从 schema v1 增量升级到 v2，新增 manifest 表；旧数据库冷启动会创建新表，
  不增加第二个 JSONL owner。
- 测试覆盖 digest、全 store 完成、blocked、损坏事件、完成前置条件、重启恢复、重复事件、
  operation id 冲突和 v1→v2 schema upgrade。

## 验证命令和结果

```powershell
python -m pytest tests/test_ghost_deletion_manifest.py tests/test_ghost_sqlite_store.py tests/test_ghost_corruption_contract.py -q --tb=short
python -m pytest tests/test_ghost_control_surface.py tests/test_ghost_controls.py tests/test_ghost_sqlite_store.py tests/test_ghost_graph_sqlite.py tests/test_ghost_corruption_contract.py tests/test_ghost_deletion_manifest.py -q --tb=short
python -m mypy codey/ghost/deletion_manifest.py codey/ghost/deletion_manifest_store.py codey/ghost/sqlite.py --python-version 3.11
pyrefly check codey/ghost/deletion_manifest.py codey/ghost/deletion_manifest_store.py codey/ghost/sqlite.py --python-version 3.11
ty check codey/ghost/deletion_manifest.py codey/ghost/deletion_manifest_store.py codey/ghost/sqlite.py
```

结果：专项 `13 passed`；受影响 Ghost/SQLite 回归 `64 passed`；目标文件 mypy、Pyrefly
（0 errors）、ty 和 Ruff 全部通过。没有把 manifest 调度接入七个生产 store。

## 实验配置

- 模型 / suite / endpoint：无模型；纯 deterministic unit；无 endpoint。
- tasks / cells / reps：13 个 manifest/schema 状态案例；受影响 Ghost/SQLite 回归 64 项；各 1 次。
- on/off 对照：pending→completed、pending→blocked、重复事件和损坏/乱序事件。

## 当前生产边界

- 是否经过真实 provider-send：否。
- 是否改变默认 prompt：否。
- 支持的模型 / 任务 / 配置：无；仅 Ghost control-plane 状态机候选。
- 生产合并结论：`EXPERIMENT_ONLY`。

## 未完成或阻塞

- 已持久化到 GhostSQLite，并能跨新的 `GhostSQLite` 实例恢复；尚未和 inbox、hebbian、
  continuity、sleep、work_queue、affinity、observations 七个 store 的 delete/reset 调用链绑定。
- 尚未证明 tombstone 在各领域 event replay/rebuild 中永久过滤，也没有失败注入和并发 runtime 矩阵。

## 下一步

下一步在不改变默认 control surface 行为的前提下，做真实七 store 的 operation-id 重试、
部分失败、重启和 rebuild 对照；只有 tombstone/replay 零复活和并发恢复证据成立后，才评估
是否新增生产调度入口，当前继续保持 `EXPERIMENT_ONLY`。
