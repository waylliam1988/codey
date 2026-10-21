# Ghost 实验：Deletion Manifest Candidate

- 实验名称：Deletion Manifest Candidate
- 状态：`EXPERIMENT_COMPLETE`（纯状态机候选；未接入生产 store）
- 负责人：Codex
- 实验 commit：本次实验 commit
- 关联 Roadmap 验收门：0.6.2 Scope Control and Four Learning Switches
- 生产合并结论：`EXPERIMENT_ONLY`

## 本次目标和假设

为 scope delete/reset 设计一个 control-plane deletion manifest：它必须携带 operation id、
scope、requested time、retention class、tombstone digest 和每个 store 的状态；同一事件重放
必须幂等，顺序错误、schema 错误和冲突事件必须 fail closed。先验证纯状态机，避免把未验证
的跨 store 事务直接接入 Ghost production stores。

## 实际改动

- 新增 `codey/ghost/deletion_manifest.py`：确定性 digest、started/store transition 事件、
  pending/completed/blocked 状态、幂等 replay 和冲突拒绝。
- 新增 `tests/test_ghost_deletion_manifest.py`：覆盖 digest、全 store 完成、blocked、损坏事件、
  完成前置条件。

## 验证命令和结果

```powershell
python -m pytest tests/test_ghost_deletion_manifest.py -q --tb=short
python -m mypy codey --python-version 3.11
pyrefly check codey --python-version 3.11
ty check codey --python-version 3.11
```

结果：`5 passed`；mypy `496 source files` 无问题；Pyrefly `0 errors`；ty `All checks passed`。
没有通过类型和行为检查前不得接入生产 store。

## 实验配置

- 模型 / suite / endpoint：无模型；纯 deterministic unit；无 endpoint。
- tasks / cells / reps：5 个状态/损坏/冲突案例，各 1 次。
- on/off 对照：pending→completed、pending→blocked、重复事件和损坏/乱序事件。

## 当前生产边界

- 是否经过真实 provider-send：否。
- 是否改变默认 prompt：否。
- 支持的模型 / 任务 / 配置：无；仅 Ghost control-plane 状态机候选。
- 生产合并结论：`EXPERIMENT_ONLY`。

## 未完成或阻塞

- 尚未持久化到 SQLite，也未和 inbox、hebbian、continuity、sleep、work_queue、affinity、
  observations 七个 store 的 delete/reset 调用链绑定。
- 尚未证明 tombstone 在各领域 event replay/rebuild 中永久过滤，也没有失败注入和并发 runtime 矩阵。

## 下一步

先为现有 GhostSQLite 增加向后兼容的 manifest schema 和单一 owner，再做真实七 store 的
operation-id 重试、部分失败、重启和 rebuild 对照；在这些证据完成前保持 `EXPERIMENT_ONLY`。
