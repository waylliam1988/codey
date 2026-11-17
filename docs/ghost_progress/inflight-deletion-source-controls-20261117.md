# Ghost 在途删除与维护来源控制

- 日期：2026-11-17
- 基线：`b7de8046`
- 实验状态：`EXPERIMENT_COMPLETE`（限定在途删除复现与来源控制负结果）
- 生产结论：`INCOMPLETE`；来源控制修复与在途输入寿命分开验收。
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

## 下一步

先单独闭合 Sleep 的当前 scoped 外部来源控制，使用相同 8 cell 复测，保留启用状态的
在途输入失败作为待办；随后在真实 task entry 与恢复 owner 上解决来源寿命。不能把
accept_events 关闭后的零写入等同于删除在开启状态下已安全，也不能宣称 0.6.2/0.6.3 完成。
