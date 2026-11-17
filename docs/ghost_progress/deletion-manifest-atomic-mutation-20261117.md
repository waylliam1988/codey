# Ghost Manifest 与真实删除的原子性候选

- 状态：`EXPERIMENT_COMPLETE`（限定同库原子 mutation/ack）；完整 Manifest 调度仍未完成。
- 比较基线：`e9fb6500` 的[同操作并发负结果](deletion-manifest-same-operation-20261117.md)。
- 生产结论：`EXPERIMENT_ONLY`；Manifest 尚未进入默认控制面调度。
- 范围：同一个 ghost.sqlite3 的七个真实 store；不跨 canonical 数据库或其他 Ghost 路径。
- 实现及验收提交：与本记录、测试及指标同提交，`git log --grep="commit store deletions with manifest acknowledgements"` 定位。

## 目标与主要变量

让每个 store 的 pending 权威复查、真实删除与 completed event 共用一个 SQLite write
transaction。失败整笔回滚，再另记 blocked；不得修改 scorer 或只忽略 result detail 冲突。
不同线程/进程由 BEGIN IMMEDIATE 串行化，完成的 store 跳过；嵌套写入用 savepoint 回滚。

旧探针把两个 wrapper 同时进入 mutation 设为有效性前提；原子性会禁止这一交错，因此
不拿其超时作为新实现正确的证据。保留原探针和负报告，另用明确的 stale pending fixture：
第二 coordinator 在已完成 start、释放数据库事务后持有 pending 快照；第一执行删除，
在 ack 前暂停；第二在 ack 前尝试恢复，或在整个操作完成且新请求学习后才恢复。
冻结 old/fresh 请求都经真实 InputQueue、canonical 执行、settlement 与 Sleep。

## 冻结验收

- 4 种 action/scope × 2 个恢复时序：仅一次 observations mutation，无事件冲突，新输入保留。
  ack 前独立 reader 仍看到旧 observation，不能看到未提交删除。
- 七 store × delete/reset × mutation 后异常/False/真实 SQLite ack 失败：42 项；store 表、
  event/cursor、settings、输入退役、来源退役和 generation 必须全部恢复到操作前，manifest
  单独持久记 blocked；不把异常误报为 completed。
- 4 种 action/scope × hard exit 在 mutation 后/ack 插入后/commit 后：12 项 Windows spawn；
  exit 23 才有效。前两种数据和 event 一起回滚，后一种一起持久；重开恢复后新任务可学习，
  相同 operation 重试不能删除新任务。
- 4 种 action/scope × 两独立 Windows spawn 进程：均在 start 后持有 pending 快照，第一
  完成后新请求学习，才释放第二；合计只执行一次 observations mutation，新请求保留。
- 两项事务边界：同一路径不同 owner 的嵌套异常只回滚自身 savepoint，事务内新建 owner
  不得通过 schema executescript 提前 commit；不同 Ghost 路径从不共用连接或原子提交。

额外运行受影响的 graph/record/observation、删除 fence、intake receipt 与控制面回归，
之后完整静态门和本轮测试/源文件类型审计。不调用模型，不跑全量 pytest。

## 待验证边界

此处不代替不同 operation 的混合恢复、完整 retention、升级 receiptless 输入、身份/scope
异常或 World Model。正常控制面接入和默认启用另需端到端实验，不以候选模块存在代替。

## 执行中的失败归属

首批 62 项中 54 passed / 8 failed：8 个并发 fixture 在 ack 前读数据时重新构造 GhostSQLite，
其 schema 初始化需要 write lock，和故意暂停的删除事务竞争，故触发 database locked。
该读取修正为预先构造 owner 的独立 SQLite connection，不改变生产初始化行为或缩短事务。
修正后原 62 项全部通过；之后增加上述四个进程案例和两项事务边界，完整相关回归另记。

首次扩大回归 **627 passed / 1 failed / 135 subtests passed**；失败是既有 `ghost list`
CLI 轻量加载契约（工具 runtime 意外加载）。用 Git 对象内存加载 `e9fb6500` 的本轮六个
Ghost 文件复验，基线同样 `tool_runtime=true`；未复制或切换 worktree。原测试的断言有效，
没有按新行为改成 true。调用栈定位到 `RunLedger → RunEvent → ToolOutcome` 的注解导入；
改为 TYPE_CHECKING，另将 SQLite 的 canonical receipt reader 延迟到实际校验输入时导入。
单独延迟 receipt 的一次复验仍失败；完成注解导入修复后，原 CLI 与事件渲染 **4 passed /
6 subtests passed**。其间提前启动的一次重复广批已主动停止，不作为通过或新增负结果。

事件文件同时清理已有 Any：参数/元数据先按 string-key object 字段解码；UI payload 为
`dict[str, object]`，display args 为只读 Mapping；不改正常事件与工具结果的语义，亦不引入
cast、ignore 或新豁免。事件和 ledger 回归纳入最终批次。

## 最终验证

固定生产源码后执行以下相关批次：

```powershell
python -m pytest tests/test_ghost_manifest_atomic_mutation.py tests/test_ghost_deletion_manifest.py tests/test_ghost_deletion_manifest_recovery.py tests/test_ghost_deletion_manifest_multiprocess.py tests/test_ghost_sqlite_store.py tests/test_ghost_graph_sqlite.py tests/test_ghost_control_mutation_failures.py tests/test_ghost_control_surface.py tests/test_ghost_control_contract.py tests/test_ghost_controls.py tests/test_ghost_inbox.py tests/test_ghost_hebbian.py tests/test_ghost_continuity.py tests/test_ghost_work_queue.py tests/test_ghost_affinity.py tests/test_ghost_sleep.py tests/test_ghost_canonical_input_receipts.py tests/test_ghost_received_input_lifetime.py tests/test_ghost_inflight_input_lifetime.py tests/test_ghost_deleted_input_fence.py tests/test_ghost_deleted_source_lifetime.py tests/test_ghost_projection_delete_fence.py tests/test_ghost_projection_exception_boundaries.py tests/test_ghost_sleep_source_controls.py tests/test_ghost_post_turn.py tests/test_ghost_post_turn_affinity.py tests/test_ghost_post_turn_work_queue.py tests/test_events.py tests/test_run_ledger.py tests/test_run_ledger_projection.py -q --tb=short --junitxml=.e2e-artifacts/ghost-manifest-same-operation-20261117/atomic-regression-final.xml
```

**652 passed / 141 subtests passed**，含新增原子性文件 **68 项**；不把前一批或单文件
结果重复相加。脱敏[指标及逐案例结果](../../tests/manual/ghost_memory_routing/reports/manifest-atomic-mutation-20261117.json)
从该 JUnit 生成；793 的 JUnit tests 属性包含 141 subtests，不是 793 个独立测试函数。

额外恢复/启动批次 **48 passed**：

```powershell
python -m pytest tests/durable/test_operation_driver_restart.py tests/durable/test_provider_response_receipts.py tests/durable/test_research_continuation_restart.py tests/durable/test_legacy_chat_rescue.py -q --tb=short
```

| 冻结检查 | 结果 |
|---|---|
| stale pending × user/project/session/reset × ack 前/完成后 | 8/8，通过；一次 mutation，新输入保留，未提交删除对独立 reader 不可见 |
| 七 store × delete/reset × 异常/False/SQLite ack 故障 | 42/42，通过；全部领域数据与 generation 回滚，blocked 单独持久 |
| Windows spawn hard exit 23 × 三窗口 × 四 scope/action | 12/12，通过；mutation/event 同时回滚或同时提交 |
| 两独立进程持有同操作 pending 快照 | 4/4，通过；第一调用一次，第二零次，新输入保留 |
| nested savepoint 与不同 Ghost 路径隔离 | 2/2，通过 |

之后依次通过完整门：

```powershell
python -m ruff check .
python -m mypy codey --python-version 3.11
pyrefly check codey --python-version 3.11
ty check codey --python-version 3.11
python -m tools.kernel_type_precision_gate
```

mypy 覆盖 701 模块；Pyrefly 0 errors / 35 既有 suppressions / 159 warnings。
本轮两个测试/探针文件另做三种类型检查。额外 mypy `--disallow-any-expr` 审计七个修改
生产文件及两个测试/探针文件，9 个文件零 owned 诊断；传递依赖仍有 351 个未修改文件的
11186 项诊断、整体退出码 1，不宣称全仓零 Any。日志留本地
`.e2e-artifacts/ghost-manifest-same-operation-20261117/atomic-any-audit.txt`。
未修改检查范围或新增豁免；未跑全量 pytest、模型调用或前端构建。
`python -m tools.publish_frontend --check` 与 `git diff --check` 通过，未新增生产模块或改变
runtime 文件清单。此前记录的其他懒加载、架构和 Research fixture 未闭合项不因此自动消失，
本批未运行其全套，不能据此宣称整个仓库所有测试通过。

## 实际接线、回滚及下一步

正常 observation 写入/输入登记默认使用同路径 write transaction；已有 graph/record
方法在显式外层事务内复用连接，平时继续拥有自己的事务。SQLite 原生 WAL、FULL、
busy_timeout、外键和原有 canonical receipt/fence 保留，没有新 schema 或第二日志。

Manifest coordinator 每个 store 在 BEGIN IMMEDIATE 后重新读取权威状态，真实 store
mutation 和完成记录同 commit；失败回滚后另写 blocked，不能覆盖竞争者的 completed。
这里不是七 store 一笔大事务，不跨 canonical 数据库；正常应用控制面仍使用原调度，
因此本次不能标 `PRODUCTION_MERGED` 或宣称 Manifest 默认启用。已有中文偏好启用范围不变。

回滚可回退本候选提交，无 schema 降级；回退会恢复已确认的并发/崩溃缺口，故在问题闭合前
不可把旧 coordinator 直接作为默认调度使用。下一实验继续验证不同 operation、失败恢复、
writer/rebuild/Sleep 混合生命周期和生产入口接线；完整删除、显式时间 replay 与 World Model
保持任务范围，不以本次限定通过替代。
