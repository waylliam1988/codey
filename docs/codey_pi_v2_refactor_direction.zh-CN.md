# Codey Pi v2-inspired 重构方向

> 面向 Codey 0.5.8 的架构设计备忘录；当前实现候选已落地，待 review / release。
> 结论：Codey 应该学习 Pi v2 的内核纪律，但不应该照着 Pi 重写。

## 参考来源

本备忘录参考：

```text
https://github.com/earendil-works/pi
reference-projects/pi @ da840b621
reference-projects/pi/packages/agent/docs/harness.md
reference-projects/pi/packages/agent/docs/runtime-simplification.md
reference-projects/pi/packages/agent/docs/assistant-durability.md
reference-projects/pi/packages/agent/docs/tool-durability.md
reference-projects/pi/packages/agent/docs/work-packages/05-direct-durable-drive.md
reference-projects/pi/packages/agent/docs/work-packages/06-session-branch-lane-separation.md
```

旧讨论里提到的 `harness-v2.md` / `harness-v2-state-machine.md` 已不是当前
Pi 本地副本的主文档路径。现在应以 `harness.md` 和 work-package 文档为准。

## 一句话

Pi v2 最值得 Codey 学的不是 Lane、CBOR、RemoteSession 或插件外壳，而是：

```text
所有可恢复行为都必须落到明确、完整、可验证的 durable state machine。
```

Codey 已经有自己的优势：

```text
EffectIntent
Settlement
ReplayPolicy
PromptEnvelope
Evidence
CompletionProof
Research provenance
Ghost continuity
```

0.5.8 的目标不是推翻这些，而是给它们补一个更硬的运行时骨架：

```text
OperationState 负责说明“现在系统处在哪里、下一步只允许做什么”。
Effect Ledger 负责说明“实际发生过什么、哪些 effect 已经 settlement”。
CompletionProof / Evidence 负责说明“用户可见结论是否被证明”。
```

这三者不能互相替代。

## Codey 和 Pi 的关键差距

Codey 现在更像：

```text
TaskRun
  -> operation function
  -> effect intent
  -> settlement
  -> completion proof
  -> recovery
```

这已经比普通 agent loop 稳很多，但仍有一个风险：

```text
task_run.py / recovery.py / RuntimeOperationStore / EffectStore
分别拥有一部分“当前状态解释权”。
```

safe batch recovery 完成后会在同一条 mutation 里把 `tool_delivery_pending`
收敛回 `writer_running` / `repair_running`，避免下一次 restart 再重放同一批工具。

Pi v2 更进一步：

```text
Session
  -> AgentLane
  -> Operation
  -> total OperationState
  -> direct durable Drive
```

Crash 后不靠“看见几个事件，再猜运行到了哪里”，而是直接读一个完整 durable state：

```text
state = assistant.effect_pending
reserved_response_id = R
attempt = 2
frames_address = pendingAssistantFrames(operation_id, R)

=> 只能执行 provider unknown-outcome recovery
```

这正是 Codey 下一步最应该补上的内核纪律。

## Pi v2 里最值得吸收的原则

### 1. Operation 接受即持久化

Pi 的 `accept` 会 durable 地创建 operation。接受成功后，即使没有本地 driver，系统也知道：

```text
operation_id
lane
intent
starting point
current state
```

Codey 0.5.8 应把这个原则收紧到现有 `TaskRuntime` / `RuntimeMutationLine`：

```text
run_id -> operation_id
operation_started -> initial OperationState
后续 phase/effect/proof 都挂在同一个 operation_id 下
```

### 2. 当前 OperationState 是恢复入口

Pi 的核心规则是：

```text
每次 durable transition 都替换完整当前 state。
Recovery 读取 state，而不是 replay 全量事件推断。
```

Codey 现在已有 `RuntimeOperationState` phase，但 0.5.8 需要把它从“记录进度”提升为
“下一步 action 的事实源”。

### 3. Effect 前写 intent，Effect 后写 settlement

Pi 的 assistant/tool 都遵循：

```text
prepare
-> publish intent
-> perform effect
-> publish outcome
```

Codey 已经实现 `RuntimeEffectIntent` / `RuntimeEffectSettlement`，应保留并强化。
0.5.8 不应该删除 Effect Ledger，也不应该把 effect 结果塞进 OperationState 里。

正确分工：

```text
OperationState:
  phase、control、attempt、pending effect ref、repair/proof progress

Effect Ledger:
  effect_id、category、args digest、replay class、settlement status、replay count
```

### 4. Single mutation boundary

Pi 通过 Session mutation line 保证所有状态修改串行提交。Codey 不一定要复制 Pi 的
Session/Branch/Lane 存储，但应该建立同等纪律：

```text
所有 runtime state / effect / terminal mutation
必须通过一个 serialized mutation boundary。
```

这可以先是轻量函数或小类，不要一开始做大 manager。

### 5. Snapshot 不是 Event

Pi 把 snapshot 用于“现在是什么”，event 用于“给观察者看发生了什么”。Codey 也应坚持：

```text
Durable state / proof / evidence 是事实源。
RunEvent / UI event / trace line 是观察面。
```

不要让 UI event 反过来参与 recovery 或 completion 判定。

### 6. Tool outcome_ready 很有价值

Pi 把 tool 的完成分成：

```text
effect_pending
-> outcome_ready
-> completed/materialized
```

原因是并行工具可能先后完成，但 transcript 必须按 assistant source order 放置。
Codey 如果未来支持更复杂工具并发，应借这个思想；0.5.8 先不引入并行 Lane，但可以把
“effect 已完成但 delivery/materialization 还没完成”的状态显式化。

### 7. Assistant partial 不是完成证据

Pi 会持久化 assistant stream frame，用于 crash 后恢复可见 partial，但它明确不是完成证据。
Codey 的 completion proof 也必须保持这个边界：

```text
partial reply / model final text / trace event
都不能自动等于 Task done。
```

done 仍然必须经过 CompletionProof / Evidence / Verification。

## 不应该照搬 Pi 的部分

0.5.8 不做：

```text
完整 Lane 系统
RemoteSession
RPC / CBOR
Chord / plugin service framework
Session tree rewrite
多 host lease
SQLite backend 迁移
完整 assistant frame persistence
完整 tool checkpoint API
Ghost 进入 runtime core
World Model 进入 completion gate
```

原因很简单：这些不是 Codey 当前的瓶颈。现在的瓶颈是 runtime correctness 的状态边界仍不够集中。

Pi 的 README 也提醒了另一个边界：Pi 默认不是一个 permission/sandbox 系统。Codey 已经有
PermissionProfile、tool contract、prompt surface hardening 和 completion proof，这些不能为了
“像 Pi”而削弱。

## 0.5.8 目标架构

目标不是新增大框架，而是把当前 runtime 文件整理成一个更明确的小内核：

```text
codey/runtime/
  operation.py              # Operation identity / intent / outcome contracts
  operation_state.py        # total durable operation state; operation_state log entry
  operation_reducer.py      # pure: state + durable facts -> RuntimeAction
  drive.py                  # peek_next_action(), no side effects
  mutation_line.py          # serialized production mutation boundary
  effect_records.py         # intent / settlement ledger, projection + entry builders
  replay_policy.py          # safe/unsafe replay policy
  tool_result_delivery.py   # delivery receipt ledger, projection + entry builders
  session_projection.py     # session-log projection; renamed from reducer.py
  session_log.py            # mutate() + repair/compaction storage adapter
```

冷启动实现不保留 `runtime/effects.py`、`runtime/reducer.py`、`runtime/scheduler.py`
兼容壳；这些名字会误导未来维护者。

`task_run.py` 的目标形态：

```text
TaskRuntime accepts operation
OperationState says next action
task_run executes business action
runtime records state/effect/settlement
terminalizer records final outcome
```

它不再继续吸收：

```text
provider recovery interpretation
repair lifecycle interpretation
delivery pending interpretation
proof state interpretation
Ghost / World Model state
```

## 建议的 OperationState v1

0.5.8 不需要一次做到 Pi 的 13 个 leaf，但状态必须 total、closed、可测试。

建议第一版：

```text
accepted
writer_running
provider_effect_pending
tool_effect_pending
tool_delivery_pending
writer_settled
completion_proof_recorded
repair_context_admitted
repair_running
repair_settled
terminal
```

每个 state 必须回答：

```text
operation_id 是谁
lane 是谁
leaf 是什么
driver 是 writer 还是 repair
pending_effect_ids 是哪些
pending_delivery_batch_id 是谁
turn / tool_index 到哪里
proof / repair / delivery 的最小 refs 是什么
terminal 是否已经不可逆
```

禁止：

```text
靠缺少 settlement 猜状态
靠最后一条 UI event 猜状态
用多个 boolean 组合出非法状态
让不同模块各自维护“当前 phase”
```

## Pure reducer 和 Drive

需要新增或收紧两个边界。

### Pure reducer

输入：

```text
OperationState
pending Effect intents/settlements
delivery receipts
completion proof refs
```

输出：

```text
RuntimeAction
```

示例：

```text
continue_writer
continue_operation
settle_provider_unknown
replay_safe_tool_batch
synthesize_interrupted_effects
terminal
fail_invariant
```

Reducer 不能：

```text
调用 provider
调用工具
写文件
写 log
读网页
产生模型可见 prompt 正文
```

### Drive

Drive 只做一件事：

```text
peek_next_action(session_log, session_id, run_id)
  -> RuntimeAction
```

第一版不做庞大的 public action interpreter。`recovery.py` 读取 `RuntimeAction` 后执行
provider unknown-outcome settlement、safe tool batch replay 或 interrupted synthesis。

这样以后测试可以写成：

```text
accept operation
peek -> continue_operation
execute until provider_effect_pending / tool_effect_pending
simulate crash
restore
peek -> settle_provider_unknown or replay_safe_tool_batch
execute
assert terminal/proof/delivery
```

这比 `sleep + kill process + grep trace` 稳很多。

## 迁移顺序

### Phase 1：盘点事实源

列出当前所有 runtime truth：

```text
RuntimeSessionLog
RuntimeOperationState
RuntimeMutationLine
RuntimeEffectIntent / Settlement
ToolResultDelivery
CompletionProof
RunEvent
RunTrace
RunLedger
```

标出哪些是 source of truth，哪些只是 projection/observation。

### Phase 2：收紧 OperationState

把当前 phase table 变成 closed transition table：

```text
非法 phase 拒绝
非法 transition 拒绝
terminal 后不能再写 business phase
state payload 不存 raw prompt/reply/stdout/source body
operation state 是 operation_state entry，不是 operation_effect(run_phase)
RuntimeSessionLog 只暴露 mutate()，不暴露 append()/append_many()
RuntimeOperationStore 只读，不暴露 start()/commit()/delete_session()
```

### Phase 3：补 reducer

先覆盖最容易出 bug 的恢复点：

```text
provider intent 已写但 settlement 缺失
tool intent 已写但 settlement 缺失
全安全、未发送的 tool batch 可重放
不可安全重放的 pending tool effect 诚实中断
tool_delivery_pending
completion proof failed 后 repair 是否允许
max_turns / invalid_tool_called / cancelled 的 terminal 分类
```

### Phase 4：task_run.py 瘦身

把状态解释逻辑搬到 runtime reducer/drive：

```text
task_run.py 负责 wiring 和业务 dispatch
runtime reducer 负责下一步 action
terminalizer 负责终态事件
completion proof 负责 done 是否可信
```

### Phase 5：manual drive / crash injection

新增 deterministic tests：

```text
test_runtime_drive_provider_unknown_outcome
test_runtime_drive_repeated_provider_unknown_outcome
test_runtime_drive_safe_tool_replay
test_runtime_drive_repeated_safe_tool_replay_snapshot_stability
test_runtime_drive_unsafe_tool_interruption
test_runtime_drive_delivery_pending_recovery
test_runtime_mutation_line_accept_crash_matrix
test_runtime_session_log_torn_terminal_tail
test_project_completion_runtime_mutation_fail_closed
test_runtime_drive_invalid_tool_no_followup
test_runtime_drive_cancel_to_terminal
```

这些测试不需要 live provider。

### Phase 6：只删除证明确实多余的旧路

冷启动原则：

```text
不为旧 shape 留长期兼容
不保留无消费者字段
不保留未接线 manager
不保留只在想象中会用的 fallback
```

但删除前必须确认没有 production import 和 regression test 仍需要它。

## 0.5.8 Release Gate

0.5.8 只有在以下条件满足时才算完成：

```text
OperationState 是 closed/total transition table
RuntimeAction reducer 是 pure function
provider/tool/delivery/recovery 的关键路径都能从 state 推出下一步
provider unknown outcome 重复 recovery 不追加重复 settlement
safe tool batch 重复 recovery 不改变 durable operation/effect/delivery snapshot
accept mutation 前/中/后 crash 能收敛到唯一 operation state
terminal state/settled torn tail 能被修剪回 canonical state 后再 reducer dispatch
project completion 内部 required runtime mutation 失败不会被抹平成 operation=None 后继续业务 effect
RuntimeSessionLog 只有 mutate()，没有 append()/append_many()
生产代码只有 RuntimeMutationLine 调用 RuntimeSessionLog.mutate()
RuntimeMutationLine 不暴露泛型 transition_operation() public surface
RuntimeOperationStore 是 projection-only，没有 start()/commit()/delete_session()
task_run.py 不再新增业务外的 runtime state 分支
EffectIntent / Settlement 没有被 OperationState 取代
CompletionProof / Evidence 的边界不变
Ghost / World Model 没有进入 runtime core
manual drive tests 能模拟 crash/restart
架构测试禁止 production import tests.manual
全量 pytest 通过
```

额外建议：

```text
先做 deterministic tests，再跑 live provider smoke。
0.5.8 不需要证明“模型更聪明”，只需要证明 runtime 更可恢复、更少猜状态。
```

## 0.6 的位置

0.6 才适合继续做：

```text
Ghost Explain
World Model Event Log
World Model ContextSource
Provider / Protocol Affinity
Project Verification Habit
Protocol classifier / structured provider path
Training export / optional tiny adapter
```

原因：

```text
Ghost / World Model / provider learning 都是 runtime core 的用户。
它们应该观察 durable facts，而不是参与定义 durable facts。
```

如果 0.5.8 没先把状态机边界收紧，0.6 再加 Ghost/World 只会把 state ownership 搞散。

## 最终原则

1. Codey 学 Pi 的纪律，不复制 Pi 的外壳。
2. OperationState 回答“现在在哪里、下一步允许什么”。
3. Effect Ledger 回答“外部世界实际发生了什么”。
4. CompletionProof / Evidence 回答“用户可见结论是否被证明”。
5. Event/trace 是观察面，不是恢复事实源。
6. Single mutation boundary 比多个聪明 manager 更重要。
7. Ghost 和 World Model 只能 observe / suggest / explain，不能进入 runtime core。
8. 冷启动不保留无消费者兼容层。
9. 先有 deterministic state-machine tests，再谈 live A/B。
10. Codey 的长期优势是可信、可验证、可恢复的本地经验，不是变成 Pi 的克隆。
