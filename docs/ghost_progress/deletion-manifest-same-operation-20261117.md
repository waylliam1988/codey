# Ghost 同一删除操作并发恢复

- 状态：`EXPERIMENT_COMPLETE`（有效负结果），候选控制面实验。
- 基线：`47410ec6`。
- 生产结论：`EXPERIMENT_ONLY`，不改现有默认行为。
- 关联：0.6.2 / 0.6.3，Deletion Manifest 生产调度前置。

## 冻结目标

两个独立 coordinator 恢复同一 operation，已 completed 的 store 不得再执行；旧 pending
快照不能在整个操作 completed 后删除新请求产生的数据。不是要求删除开始前的快照语义：
本实验的新输入严格在第一个 coordinator 返回 completed，且独立 reader 确认之后接收。

主要变量是第二个 coordinator 的恢复时序。使用真实 AppContext InputQueue、canonical
claim/start、确定性 provider、settlement 和后台 Sleep；控制候选绑定七个真实 store。
在 observations（最后一个 store）调用边界用 Event 调度，不改生产函数或 SQLite。

矩阵：delete_scope × user/project/session，reset_all × all；每项三种时序，重复两次，共 24 cell。

1. sequential：第一操作完成、新请求学习后，再重试同 operation，正常幂等对照。
2. before_ack：两 coordinator 均读到 observations pending；第一删除后，第二删除，
   第一记录 completed；新输入学习后第二才记录完成，检查返回值差异导致的事件冲突。
3. after_completion：两 coordinator 均读到 pending；第二停在 store 调用前，第一完成；
   新请求学习后才放行第二，检查旧 pending 快照是否误删新输入。

无模型、HTTP 或浏览器，无语言 on/off treatment，不恢复已停止的实机批次。

## 有效性与 scorer

有效 cell 必须：旧任务完成、第一删除返回并独立读为 completed、新任务在其后接收完成、
Sleep 已完成且新 observation 确实存在；并发 cell 必须两个 wrapper 已进入且按冻结时序放行。
超时、未达到边界或新输入未学习判 invalid，不能计为正确删除或无收益。

通过要求：同 operation 的 observations mutation 总次数为一、两个 coordinator 均正常返回
completed、新请求 observation 在第二 coordinator 结束和 reopen 后仍存在。
记录调用次数、删除返回值、异常类型、事件数、前后新 observation；不以最终 manifest
显示 completed 单独判通过。生产补丁前保留基线负结果，另作提交。

## 执行结果

复现：

```powershell
python -m tests.manual.ghost_memory_routing.manifest_same_operation_probe --output tests/manual/ghost_memory_routing/reports/manifest-same-operation-baseline-20261117.json
```

[脱敏报告](../../tests/manual/ghost_memory_routing/reports/manifest-same-operation-baseline-20261117.json)：
24 valid / 0 invalid；8 个 sequential 全部通过，16 个并发 cell 全部失败。
8 个 after_completion 全部误删新请求 observation；6 个 before_ack delete_scope 因第一次
返回 1、第二次返回 0 导致 completed 事件冲突，随后 blocked 事件也与已完成状态冲突；
两个 before_ack reset_all 虽正常返回，仍执行了两次 mutation。最终各 cell 的 manifest
均有 8 个事件，事件计数与 completed 状态本身无法发现重复删除。

原首次执行因脚本错误把 `read_all()` 的 dict 当成属性记录，首 cell 在并发前抛
AttributeError；无有效 cell 或报告，已修 fixture 为 `.get("run_id")`。不计作生产负结果。

相关既有 manifest 纯逻辑、七 store delete/reset 恢复与 spawn 冷启动测试 **26 passed**。
这些测试未覆盖本次两个协调器实际执行同一 store，不能代替本次负结果。
脚本通过 Ruff、mypy、Pyrefly、ty；完整生产树 Ruff、mypy（701 模块）、Pyrefly
（0 errors / 35 既有 suppressions / 159 warnings）、ty 和 kernel precision gate 通过。
未跑全量 pytest，没有修改生产默认状态。
额外 mypy `--disallow-any-expr` 审计中本轮脚本零诊断；传递依赖仍有 354 个未修改文件的
11283 项诊断，整体退出码 1，不宣称全仓零 Any。完整日志留本地
`.e2e-artifacts/ghost-manifest-same-operation-20261117/probe-any-audit.txt`。

## 结论和下一步

不能把候选直接接入默认删除调度。需在真实 store mutation 前重新读取权威状态，并让
检查、删除、完成确认具有进程间互斥或同事务原子性；只容忍 detail 冲突或只在最终
append 处去重会继续误删新输入。还需验证崩溃释放、失败恢复及不同 operation 交错，
完整生产接线另作提交。本次不证明模型收益或 World Model 完成。
