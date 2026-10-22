# Ghost 实验：Deletion Manifest Candidate

- 实验名称：Deletion Manifest Candidate
- 状态：`EXPERIMENT_COMPLETE`（持久化候选；未接入生产 store 调度）
- 负责人：Codex
- 实验 commit：`76a3472f`（纯状态机）；`89e0cfd8`（SQLite 持久化与重启复放）；
  `5b6f2ee8`（七 store 协调器候选）；`a92db8c9`（七 store 失败恢复与 scope 收敛）
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
- 新增 `DeletionManifestCoordinator` 候选：只遍历 `pending_stores`，已完成/blocked store
  不重复调用；七类现有 store 的 `delete_scope` / `reset_all` 方法已通过 adapter fixture 绑定，
  失败会记录 bounded blocked detail 并停止当前批次。
- 新增 [Deletion Manifest Failure Recovery](deletion-manifest-failure-recovery.md)：七个真实
  store 各自注入一次失败，覆盖重启、pending-only resume、新 operation recovery 和 rebuild；
  协调器改为只使用 durable manifest 的 project/session scope。
- 新增 [Deletion Manifest Reset Recovery](deletion-manifest-reset-recovery.md)：`reset_all` 的
  七 store failure、重启、pending-only resume、新 operation recovery 和 rebuild 均通过，
  仍未接入默认 control surface。
- 新增 [Deletion Manifest Multiprocess Cold Start](deletion-manifest-multiprocess.md)：7 个
  独立进程在 8 次 repetition 中同时冷启动并提交同一 operation 的事件，schema 和 replay
  均收敛。

## 验证命令和结果

```powershell
python -m pytest tests/test_ghost_deletion_manifest.py tests/test_ghost_sqlite_store.py tests/test_ghost_corruption_contract.py -q --tb=short
python -m pytest tests/test_ghost_deletion_manifest_recovery.py -q --tb=short
python -m pytest tests/test_ghost_control_surface.py tests/test_ghost_controls.py tests/test_ghost_sqlite_store.py tests/test_ghost_graph_sqlite.py tests/test_ghost_corruption_contract.py tests/test_ghost_deletion_manifest.py -q --tb=short
python -m mypy codey/ghost/deletion_manifest.py codey/ghost/deletion_manifest_store.py codey/ghost/sqlite.py --python-version 3.11
pyrefly check codey/ghost/deletion_manifest.py codey/ghost/deletion_manifest_store.py codey/ghost/sqlite.py --python-version 3.11
ty check codey/ghost/deletion_manifest.py codey/ghost/deletion_manifest_store.py codey/ghost/sqlite.py
```

结果：manifest/coordinator 与 delete/reset failure recovery `25 passed`；并发冷启动 `8` 次
各 `1 passed`；上一轮含控制面、SQLite、图和损坏契约的受影响回归 `76 passed`；目标文件 mypy、Pyrefly
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

- 已持久化到 GhostSQLite，并能跨新的 `GhostSQLite` 实例恢复；协调器已能绑定七类现有 store
  的 delete/reset 方法，但尚未接入 control surface 默认调度入口。
- 七个 store 的 delete/reset 失败、重启、pending-only resume、新 operation recovery、部分
  rebuild 和 manifest 冷启动已验证；尚未证明真实 runtime writer/reader 混合并发及所有领域
  event replay/rebuild 中 tombstone 永久过滤。

## 下一步

下一步在不改变默认 control surface 行为的前提下，加入并发 failure/recovery 和混合
delete/reset runtime acceptance；只有所有 tombstone/replay 零复活和并发恢复证据成立后，
才评估是否新增生产调度入口，当前继续保持 `EXPERIMENT_ONLY`。
