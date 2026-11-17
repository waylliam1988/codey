# Ghost 在途删除与维护来源控制

- 日期：2026-11-17
- 基线：`b7de8046`
- 实验状态：`EXPERIMENT_COMPLETE`（限定在途删除复现与来源控制负结果）
- 实验提交：`823a2a73`
- 生产结论：`PRODUCTION_MERGED`（仅 Sleep 来源控制；在途输入寿命仍 `INCOMPLETE`）。
- 模型 / endpoint：无。使用确定性 provider transport，无 HTTP、无真实模型调用，
  不恢复用户已停止的 `/api/run` 语言批次。

## 假设、矩阵与停止条件

删除时尚未生成 Ghost 投影的旧请求，在稍后真实结算时也不能重新消费被删输入；
`accept_events=false` 时，维护可以处理已接受的内部状态，但不能从 runtime/knowledge
新消费外部来源。`retrieve_projections` 和 `admit_hints` 的关闭不应阻断允许的事件消费。

脚本调用真实 `run_task_submission(build_task_deps(...), TaskSubmission(...))`，使用
真实 AppContext、durable lifecycle、settlement、post-turn 和后台 Sleep。仅 provider 的
`send` 由 Event 阻塞：确认 Ghost observation/continuity 为空，执行删除或 reset，设置
当前 scoped `accept_events`，再允许正常回复并等待维护结束。不是 HTTP 或模型收益实验。

冻结矩阵为 session delete / reset × accept_events 开 / 关 × 两次重复，共 8 cell。
每 cell 必须删除前无投影、删除成功、任务正常完成；否则是无效 fixture，不能计为安全通过。
测量旧 run 的 observation 与 continuity 行数，非零即记录负结果，不通过放宽 scorer 掩盖。
本轮不宣称覆盖 project/user 在途删除、多进程恢复或完整持久化来源寿命。

## 基线实测与归属

```powershell
python -m tests.manual.ghost_memory_routing.inflight_delete_probe --output tests/manual/ghost_memory_routing/reports/inflight-delete-baseline-20261117.json
python -m pytest tests/test_ghost_sleep_source_controls.py -q --tb=short
```

运行时报告：[脱敏基线指标](../../tests/manual/ghost_memory_routing/reports/inflight-delete-baseline-20261117.json)。
8 cell 全部有效：

| 当前 accept_events | cell 数 | 每 cell 旧 observation | 每 cell 旧 continuity | 有旧输入 cell |
|---|---:|---:|---:|---:|
| 开 | 4 | 1 | 3 | 4 |
| 关 | 4 | 0 | 1 | 4 |

这证明两个独立缺口：projection generation 捕获晚于任务输入，无法识别首次投影前的旧请求；
另外 Sleep 从外部 run projection 新生成 continuity/affinity 时未检查当前 scoped accept_events。
后者不是 run_maintenance 的关闭语义，不能用关闭整轮维护代替独立控制。

新增 15 项来源控制回归，以真实 Ghost store 和 RunLedgerProjection 验证 user/project/session
的开、关、缺控制 owner、读取报错；另覆盖 retrieval/admission 独立与内部状态维护。
修正 fixture 后的基线为 **9 failed / 6 passed**，失败均为三个 scope 的 off/missing/error 泄漏。
最初 fixture 误读不存在的 GhostInboxStore.path，引起九项 AttributeError；该问题已修正，
这些 fixture 异常不算生产缺陷，也未据此修改生产 API。

复现脚本和新增测试经 mypy、Pyrefly、ty 检查通过；无新增 Any、cast、忽略或配置豁免。
当前静态门通过不表示上述行为已修复。

## 生产修复与验收

`GhostSleepStore.run_once` 的 affinity_sync 和 continuity_refresh 在消费外部 run projection /
knowledge 前读取当前 user/project/session 的 accept_events。关闭、缺 owner、读取报错时
不向下传递这些外部来源；缺失或报错附有 bounded warning，报告记录 external_sources_allowed。
已有接受状态的同步、衰减、压缩和报告仍可运行；retrieval/admission 独立。

这是正常后台 Sleep 的默认行为，没有新增实验开关，没有新增 provider 调用或改变 hint 晋级范围。
Sleep 原文件及新测试、脚本保持明确类型；没有 cast、type ignore 或检查配置变更。

相同 8 cell 复测：

```powershell
python -m tests.manual.ghost_memory_routing.inflight_delete_probe --output tests/manual/ghost_memory_routing/reports/inflight-delete-sleep-controls-20261117.json
```

[复测指标](../../tests/manual/ghost_memory_routing/reports/inflight-delete-sleep-controls-20261117.json)：
8 cell 全有效；关闭的 4 cell 均为 **0 observation / 0 continuity**，泄漏从 4 降为 0；
开启的 4 cell 仍各有 **1 observation / 3 continuity**，在途删除负结果保留。

受影响生产回归共 **139 passed**：

```powershell
python -m pytest tests/test_ghost_sleep_source_controls.py tests/test_ghost_sleep.py tests/test_ghost_post_turn_affinity.py tests/test_ghost_post_turn_shared_workspace_and_explicit_workflows.py tests/test_ghost_projection_delete_fence.py tests/test_ghost_deleted_source_lifetime.py tests/test_ghost_controls.py tests/test_ghost_control_mutation_failures.py -q --tb=short
python -m ruff check .
python -m mypy codey --python-version 3.11
pyrefly check codey --python-version 3.11
ty check codey --python-version 3.11
python -m tools.kernel_type_precision_gate
python -m mypy tests/test_ghost_sleep_source_controls.py tests/manual/ghost_memory_routing/inflight_delete_probe.py --python-version 3.11
pyrefly check tests/test_ghost_sleep_source_controls.py tests/manual/ghost_memory_routing/inflight_delete_probe.py --python-version 3.11
ty check tests/test_ghost_sleep_source_controls.py tests/manual/ghost_memory_routing/inflight_delete_probe.py --python-version 3.11
```

静态门按以上顺序串行通过。完整 mypy 覆盖 699 个生产模块；Pyrefly 为 0 errors，
35 个既有 suppression、158 个 warning；kernel Any counts 为零，两个新文件分别通过三个 checker。
未运行全量 pytest，也未创建或复制 worktree。

生产提交：`git log --grep="honor scoped source controls during Sleep"`。
回滚仅回退本轮 Sleep 逻辑，保留 schema 5、generation、retirement 和用户控制设置；
回退会重新暴露外部来源绕过关闭的问题，不应删除 fence 或清库降级。

## 限制与下一步

在真实 task entry 与恢复 owner 上解决来源寿命，跨后台队列携带有效边界；不能仅依赖
projection 方法进入时的 generation 或线程本地状态。不能把关闭后的零写入等同于删除
在开启状态下已安全，也不能宣称 0.6.2/0.6.3 完成。

本轮只验证进入 source step 时的控制；控制读取与来源提交之间的撤回并发、已排队维护
是否在实际启动时复查 run_maintenance、knowledge 的独立来源矩阵仍待专项验证。完整
manifest 调度、混合恢复与显式时间 replay 继续按路线图推进。
