# Codey Runtime 架构（living doc）

这份文档只写当前真相。历史演进去看 `CHANGELOG.md`，不要在这里考古。
改动 runtime 时先更新这份文档，再改代码。

## 一句话

durable entries 进来，经过一次标准解析，变成决策或写入，观察层只能看。

```text
Entries
   │
   ▼
SessionView ──┬──► Drive ──► 恢复动作
              └──► MutationLine ──► SessionLog
                                        │
Observe ◄───────────────────────────────┘（只读投影）
```

## 五个包

```text
runtime/core/     operation / state / reducer / ports / outcome / models / cancellation
runtime/log/      session_log / projection / session_view / compaction
runtime/effects/  effect_records / delivery / replay_* / safe_tool_replay
runtime/write/    mutation_line / drive / task_runtime
                  provider_effects / tool_batches / delivery_recovery
runtime/observe/  events / evidence / prompt_* / terminalizer
```

- `core/`：状态机 + 纯决策 + 契约。reducer 不做 IO，不认识业务层。
- `log/`：持久化 + 读模型。`session_view` 是唯一的 canonical read projection。
- `effects/`：外部效应账本。`intent -> effect -> settlement`，恢复只读
  committed state，不从事件缺失推断。
- `write/`：唯一的写口。`mutation_line.py` 是 facade，只做 admit + commit；
  真正的行构造是三个纯 builder 模块。
- `observe/`：丢了可重建的投影。`write/` 禁止 import 它，它也禁止 import
  `write/`（`tests/test_architecture.py` 锁死）。

## 三个硬锁（测试里）

1. `SessionView` 只有 `state / effects / batches` 三个字段，不准长出
   messages、evidence、ghost、completion。
2. `.mutate()` 全仓只允许 `write/mutation_line.py` 和
   `log/session_log.py` 调用。
3. 平铺残留 `codey/runtime/*.py` 被断言不存在；`write/` 与 `observe/`
   互禁 import。

## Pending 是派生的，不是存的

`RuntimeOperationState` 只存 leaf / driver / task / turn 等坐标，不存
`pending_effect_*` / `pending_delivery_batch_id`（payload schema v2，v1
直接 fail closed）。在飞事实一律现场算：

- `pending_for(view)` 只看 `state.turn` 当 turn 的 unsettled intents。
- delivery pending 优先选同 turn 未动过的 fresh batch（failover 契约），
  真含糊才 fail closed，绝不随便抓第一个。
- `completion_proof_satisfied` 从 proof status 派生（`complete` 即满足），
  不持久化；畸形 proof 在 project completion 提交边界 fail closed。

## 外部 effect 纪律

每个真实外部效果先写 intent，效果后写 settlement；safe 读可重放，
unsafe 永不自动重复。storage/session 层不知道 agent 语义，progress 和
outcome 不自动变成完成证据，完成只认 proof + verification。
