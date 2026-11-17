# Ghost 原始领域事件物理删除防护

- 日期：2026-11-17；关联0.6.3，完整版本仍未完成。
- 状态：`PRODUCTION_MERGED`（本记录随独立生产修复提交到本地master）。
- 原始负结果：`7c9ad01f96a1693697cc68c0c6a7a9d3e99c186b`，
  [混版本warm owner基线](mixed-schema-history-baseline-20261117.md)。
- 模型/endpoint：不使用；不运行已停止语言批次，不主张模型行为收益。

## 生产问题与实现

schema7的真实长期owner在当前runtime升级并建立窗口或执行reset后，仍可物理删除原始
事件。42项原矩阵全部有效，14项旧schema正对照通过、28项升级处理在最后历史比较失败。
仅构造期拒绝新schema不能约束已经实例化的旧owner。

在既有GhostSQLite初始化事务为memory_events、graph_events、observation_events分别创建
BEFORE DELETE触发器，拒绝实际原始行的物理删除；错误为Ghost original history is retained。
当前领域窗口推进、投影替换、永久身份退役和派生向量清理不删除这些表，继续正常工作。
失败沿原事务回滚，旧observation append会返回False，旧record/graph写口报告SQLite拒绝。

无新schema版本、表、来源、UDF、模型调用或默认关闭开关；schema7→8和已有schema8的
正常构造都安装约束。已有相同schema8数据与窗口无需重写；不将元数据缺失改为启动修复。
约束也作用于未注册应用UDF的普通SQLite连接，覆盖本次旧owner实际DELETE方法。
这是一项原历史保留的生产可靠性收益，不声称隔离旧reader或任意管理SQL。

## 验收矩阵与候选结果

保留原42项矩阵和历史scorer，只抽取子进程启动/收尾的类型明确helper供新验收复用。
新增13项：三表×当前/普通SQLite连接共6项失败回滚；三表×升级前缓存DELETE/已有读快照
共6项实际schema7升级交错；已有schema8缺旧约束的库补装三guard且不改历史/窗口共1项。
缓存语句必须在升级后因新guard拒绝；旧读快照写入必须由SQLite BUSY_SNAPSHOT拒绝，
两者分开归因，不把SQLite既有快照保护称为本次新增功能。新fixture仅移除新增guard以重现
前一schema8布局，不降低user_version、不修改历史、窗口或原断言。

首轮候选 **225 passed**，原42项14→42全部通过，新增13项及已有窗口、reset和损坏检查
全通过。随后仅强化升级处理的旧操作必须明确报告rejected的验收断言；生产源码未变，
更强断言包含在扩大矩阵；原14/28负结果保留。

扩大回归 **1088 passed / 136 subtests passed**，无失败、错误或skip；原42项与新增13项
从JUnit逐case核对均通过。期间生产Python保持冻结，包含CLI/认证HTTP、Manifest故障/
并发/硬退出、canonical receipt、旧投影准入、容量维护、七领域消费者和durable重启。

其后全树Ruff、mypy、Pyrefly、ty和precision以及三个实验/测试文件的三种类型检查均通过，
但额外Any审计发现新增测试直接将sqlite3.Row类赋给row_factory时存根带入1项Any。
改用生产已有、参数与返回值明确的_row_factory；生产SQL与逻辑未改，不用cast、ignore或豁免。
最后受影响测试 **13 passed**；生产源码与1088项扩大批次完全相同，测试仅更换同等行语义
的类型明确回调。225、1088/136与最后13项是独立批次，不相加、不把扩大批次说成最后测试
行工厂修改后的全范围复验。

随后全树Ruff、mypy（702个生产模块）、Pyrefly、ty与precision依次全部通过；Pyrefly为
0 errors、35项既有suppressed、158 warnings。三个实验/测试文件另经三种类型检查通过。
四个owned Python文件额外`--disallow-any-expr`审计零诊断；未修改依赖188文件4066项诊断
使扩展命令exit1，不能称为全树Any审计通过。首轮1项owned问题和此前基线5项问题均保留，
没有cast、ignore、新豁免或弱化检查。

[脱敏逐cell报告](../../tests/manual/ghost_memory_routing/reports/history-delete-guards-production-20261117.json)
保存42项原矩阵、13项新增验收、独立批次结果与LF规范化源摘要；不保留原始事件或state。
未运行全量pytest、模型或停止的语言批次，批次耗时仅作复现信息，不主张性能收益。

## 复现命令

```powershell
python -m pytest tests/test_ghost_mixed_schema_history.py tests/test_ghost_history_delete_guards.py tests/test_ghost_corruption_contract.py tests/test_ghost_event_windows.py tests/test_ghost_reset_history_retention.py tests/test_ghost_reset_boundaries.py -q --tb=short --junitxml=.e2e-artifacts/ghost-mixed-schema-20261117/candidate.xml
python -m pytest tests/test_ghost_mixed_schema_history.py tests/test_ghost_history_delete_guards.py tests/test_ghost_event_windows.py tests/test_ghost_scope_history_retention.py tests/test_ghost_capacity_history_retention.py tests/test_ghost_reset_history_retention.py tests/test_ghost_reset_boundaries.py tests/test_ghost_sqlite_store.py tests/test_ghost_corruption_contract.py tests/test_ghost_graph_sqlite.py tests/test_ghost_control_surface.py tests/test_ghost_control_mutation_failures.py tests/test_ghost_manifest_atomic_mutation.py tests/test_ghost_manifest_production_wiring.py tests/test_ghost_manifest_discovery.py tests/test_ghost_deletion_manifest_recovery.py tests/test_ghost_deletion_manifest_multiprocess.py tests/test_ghost_canonical_input_receipts.py tests/test_ghost_legacy_projection_admission.py tests/test_ghost_deleted_input_fence.py tests/test_ghost_deleted_source_lifetime.py tests/test_ghost_inflight_input_lifetime.py tests/test_ghost_received_input_lifetime.py tests/test_ghost_projection_delete_fence.py tests/test_ghost_vectors.py tests/test_experience_memory.py tests/test_ghost_inbox.py tests/test_ghost_hebbian.py tests/test_ghost_continuity.py tests/test_ghost_work_queue.py tests/test_ghost_affinity.py tests/test_ghost_sleep.py tests/durable/test_operation_driver_restart.py tests/durable/test_legacy_chat_rescue.py -q --tb=short --junitxml=.e2e-artifacts/ghost-mixed-schema-20261117/production-complete.xml
python -m pytest tests/test_ghost_history_delete_guards.py -q --tb=short --junitxml=.e2e-artifacts/ghost-mixed-schema-20261117/production-final.xml
python -m ruff check .
python -m mypy codey --python-version 3.11
pyrefly check codey --python-version 3.11
ty check codey --python-version 3.11
python -m tools.kernel_type_precision_gate
python -m mypy tests/test_ghost_mixed_schema_history.py tests/test_ghost_history_delete_guards.py tests/manual/ghost_memory_routing/mixed_schema_worker.py --python-version 3.11
pyrefly check tests/test_ghost_mixed_schema_history.py tests/test_ghost_history_delete_guards.py tests/manual/ghost_memory_routing/mixed_schema_worker.py --python-version 3.11
ty check tests/test_ghost_mixed_schema_history.py tests/test_ghost_history_delete_guards.py tests/manual/ghost_memory_routing/mixed_schema_worker.py --python-version 3.11
python -m mypy codey/ghost/sqlite.py tests/test_ghost_mixed_schema_history.py tests/test_ghost_history_delete_guards.py tests/manual/ghost_memory_routing/mixed_schema_worker.py --python-version 3.11 --disallow-any-expr --no-error-summary
```

完整日志、JUnit、失败state只留本地`.e2e-artifacts/ghost-mixed-schema-20261117/`及pytest
保留目录；仓库报告只保留脱敏指标、逐cell状态和失败归属，不包含原始正文或临时路径。
最终静态门日志为production-final-{ruff,mypy,pyrefly,ty,precision,owned-mypy,owned-pyrefly,
owned-ty,owned-any}.txt，首轮日志为production-*，另保留baseline及baseline-final的负结果。

## 默认消费者、回滚与限制

普通API/CLI和各领域owner构造默认安装并使用约束，无实验开关或无人调用模块。
已有用户关闭、故障隔离、scope删除、普通reset、Manifest确认和恢复继续使用现有入口。
回退优先隔离受影响消费者并保留数据库、原日志与永久退役登记；不应为兼容旧破坏性reset
删除这些guard。物理purge仍需独立显式可审计操作，不能临时关约束冒充普通tombstone删除。
恢复使用支持当前窗口的runtime，保留失败Manifest与原数据，不能把旧写口的拒绝记录改写成成功。

本次不防DROP TABLE、触发器修改或其他管理SQL，不限制合法observation同run幂等更新，
不宣称完整append-only事实不可修改。原始日志磁盘占用未有界；旧reader、旧版本整个App、
模型发送及其他混版本事务仍需独立验收。完整0.6.3和World Model闭环仍未完成。
