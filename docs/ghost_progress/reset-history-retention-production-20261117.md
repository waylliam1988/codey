# Ghost 普通 reset 保留历史的生产修复

- 日期：2026-11-17；关联0.6.3，完整版本仍未完成。
- 原始负结果commit：`b64ab6ef16f0bb0c881c3fd2a24c3c17e99856b4`；[冻结矩阵与结果](reset-history-retention-baseline-20261117.md)。
- 状态：`PRODUCTION_MERGED`（本记录随独立生产修复提交到本地master）。
- 模型/endpoint：不使用；存储可靠性修复不增加模型调用，不恢复已停止语言批次。

## 问题、收益与实现

普通控制面reset_all默认Manifest声明tombstone，但七领域原实现物理删除原始事件。
原63项有效矩阵的21项只读对照全通过，42项reset处理均在最后原历史比较失败；入口、
清空可见状态和重开/rebuild均先通过。本次收益为履行已声明历史保留合同，不以架构改造
或模型行为改进替代可复查的生产可靠性结果。

沿用schema8和已有领域表，不增加第二套日志、schema或默认关闭的开关：

- 四record领域保留memory_events，清空memory_records，将现有重放窗口推进到原领域尾部，
  将consumer cursor确认到同一尾部。候选身份与continuity/work queue来源退役仍在同事务。
- 两graph领域保留graph_events，沿用既有精确replace_rows清空节点/边并确认cursor；
  重放窗口推进到reset时的原领域尾部。
- observation保留全部原始事实并继续永久退役原run/origin身份，窗口推进到事实尾部；
  另显式清空派生memory_embeddings，不能再依赖物理删除事实的cascade清缓存。
- 明确reset可从原始领域尾部恢复损坏/缺失窗口和损坏cursor；普通读取和checkpoint仍
  保持原fail-closed检查。缺窗口的冷启动仍拒绝，未增加悄悄修复的启动fallback。

这些操作继续加入既有Manifest mutation/ack同库事务；completed操作不重复执行。
既有显式关闭与reset保留设置语义不变，不增加模型可见内容或Evidence/Permission/
CompletionProof权力。原始事件中的退役内容只能作为历史留存，不能被普通重建或检索复用。

## 候选结果与失败归属

第一轮 **250 passed**，包含原63项reset矩阵、已有窗口、普通删除历史、输入fence和来源
退役。原reset矩阵从21/42变为63全部通过。

新增50项reset边界验收：21项损坏窗口恢复、6项损坏cursor恢复、7项窗口写失败的完整状态
回滚、7项实际容量以上reset、3项completed重试/新输入/再次reset、4项独立关闭控制保留、
2项embedding期间reset。保留原63项矩阵，不改变原scorer或阈值。

第二轮为 **180 passed / 1 failed**。失败来自新增fixture把实际控制retrieve_projections
误写为不存在的update_projections；改为正确控制名，不修改生产允许值、不放宽断言。
七容量项使用真实阈值；observation容量准备复用真实schema合成事实，不伪造canonical receipt。
损坏恢复仅验证已持有有效owner后的显式reset，不声称缺元数据的冷启动自动恢复。

扩大回归 **906 passed / 136 subtests passed**，无失败、错误或skip；含真实CLI、认证HTTP、
并发与硬退出恢复，以及输入接收、删除fence、七领域消费者和durable重启。未运行全量pytest。

随后全树Ruff发现本次新增import在两生产文件及新增测试中的3处格式问题；已仅按isort要求
换行/排序，无行为修改。原检查日志保留为production-ruff-initial.txt。

最终源码相关复验 **125 passed**，包含原63项reset矩阵、50项新增reset边界及12项durable
恢复/启动检查；无失败、错误或skip。906项扩大与最后125项为不同批次，不相加，也不将
906项说成最后格式修改后的全范围复验。两批JUnit均逐项核对原矩阵及新增50项全部通过。

最后串行全树Ruff、mypy（702个生产模块）、Pyrefly、ty和precision全部通过。
Pyrefly为0 errors、35项既有suppressed、158 warnings；两个新增测试的三个类型检查均通过，
其中Pyrefly为0 errors。六个owned Python文件额外`--disallow-any-expr`审计零诊断；
未修改依赖187文件4065项诊断使扩展命令exit1，不能称为全树Any审计通过。
未新增cast、ignore、检查豁免或动态unchecked访问。

[脱敏逐cell报告](../../tests/manual/ghost_memory_routing/reports/reset-history-retention-production-20261117.json)
保存原63项矩阵与50项边界、各独立批次结果、源文件LF规范化摘要和失败归属；未运行模型、
全量pytest或已停止的语言批次。批次耗时只作为复现信息，不声称独立的性能/延迟收益。

## 复现命令与本地产物

```powershell
python -m pytest tests/test_ghost_reset_history_retention.py tests/test_ghost_event_windows.py tests/test_ghost_scope_history_retention.py tests/test_ghost_deleted_input_fence.py tests/test_ghost_deleted_source_lifetime.py -q --tb=short --junitxml=.e2e-artifacts/ghost-reset-history-20261117/candidate.xml
python -m pytest tests/test_ghost_reset_history_retention.py tests/test_ghost_reset_boundaries.py tests/test_ghost_event_windows.py tests/test_ghost_scope_history_retention.py tests/test_ghost_capacity_history_retention.py tests/test_ghost_graph_sqlite.py tests/test_ghost_sqlite_store.py tests/test_ghost_corruption_contract.py tests/test_ghost_control_surface.py tests/test_ghost_control_mutation_failures.py tests/test_ghost_manifest_atomic_mutation.py tests/test_ghost_manifest_production_wiring.py tests/test_ghost_manifest_discovery.py tests/test_ghost_deleted_input_fence.py tests/test_ghost_deleted_source_lifetime.py tests/test_ghost_inflight_input_lifetime.py tests/test_ghost_received_input_lifetime.py tests/test_ghost_projection_delete_fence.py tests/test_ghost_vectors.py tests/test_experience_memory.py tests/test_ghost_inbox.py tests/test_ghost_hebbian.py tests/test_ghost_continuity.py tests/test_ghost_work_queue.py tests/test_ghost_affinity.py tests/test_ghost_sleep.py tests/durable/test_operation_driver_restart.py tests/durable/test_legacy_chat_rescue.py -q --tb=short --junitxml=.e2e-artifacts/ghost-reset-history-20261117/production-complete.xml
python -m pytest tests/test_ghost_reset_history_retention.py tests/test_ghost_reset_boundaries.py tests/durable/test_operation_driver_restart.py tests/durable/test_legacy_chat_rescue.py -q --tb=short --junitxml=.e2e-artifacts/ghost-reset-history-20261117/production-final.xml
python -m ruff check .
python -m mypy codey --python-version 3.11
pyrefly check codey --python-version 3.11
ty check codey --python-version 3.11
python -m tools.kernel_type_precision_gate
python -m mypy tests/test_ghost_reset_history_retention.py tests/test_ghost_reset_boundaries.py --python-version 3.11
pyrefly check tests/test_ghost_reset_history_retention.py tests/test_ghost_reset_boundaries.py --python-version 3.11
ty check tests/test_ghost_reset_history_retention.py tests/test_ghost_reset_boundaries.py --python-version 3.11
python -m mypy codey/ghost/event_windows.py codey/ghost/record_store.py codey/ghost/graph_store.py codey/ghost/observations.py tests/test_ghost_reset_history_retention.py tests/test_ghost_reset_boundaries.py --python-version 3.11 --disallow-any-expr --no-error-summary
```

完整日志、JUnit和失败state只留本地`.e2e-artifacts/ghost-reset-history-20261117/`及pytest
保留目录。报告只提交脱敏cell、计数与失败归属，不保存原始正文或临时路径。

## 默认入口与回滚边界

普通API/CLI控制面reset_all及各领域现有reset_all直接调用上述实现，无实验开关和额外
部署接线。reset继续由用户显式确认；相关Ghost消费者保留主动关闭、隔离与Manifest失败
记录。新来源仍可正常接收，重复completed操作应保留后来新增输入。

回退优先关闭或隔离受影响消费者、保留数据库与永久退役登记。不能回退至会物理删除
历史的旧reset，不能直接归零重放窗口或降低schema版本，否则可能再次采用归档输入。
物理purge/磁盘retention仍是独立待验证操作；原始日志磁盘占用并未因此有界。

后续仍需验证已运行旧进程的混版本行为、其余历史重写路径、异常身份/scope和完整生命周期，
继续补齐admission/controls边界、显式时间/replay和真实World Model闭环。
