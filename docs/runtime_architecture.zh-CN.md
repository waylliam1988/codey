# Codey Runtime 架构（living doc）

这份文档只写当前真相。历史演进去看 `CHANGELOG.md`，不要在这里考古。
改动 runtime 时同步维护本文；测试报告的结果必须等实际验证结束后再记录。

完整包结构与当前函数入口见[项目结构与职责地图](project_structure.zh-CN.md)。

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
`pending_effect_*` / `pending_delivery_batch_id`（payload schema v1，其他版本
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

## 统一 kernel 的行为边界与 parity

Coding 与 Research 共用 `task_loop`、`kernel_protocol`、`kernel_execution` 和
completion gate。`project_adapter` 保留 AgentRequest/RunResult 边界；
`research_iteration.run_research_iteration` 负责知识库与 synthesis 交付，不保留
旧迭代类。`run_task_kernel` 只接收正式 `KernelRunRequest`，其依赖分为 transport、
execution、observation；不再提供旧 keyword 适配。运行回调由 `task_phases/hooks`
的一次运行实例持有，任务提交资源由 `TaskSubmissionStores` 显式捕获。

- 文本 `read_files / parallel` 在解析期整批校验后降低为有序只读调用；native
  只用各自显式 ID，不为 wrapper 编造 child ID。执行和恢复仍按每个 effect 结算。
- `KernelProgress` 只观察变化、结果指纹与 SeenInfo，不生成 completion proof。
- verification candidate 是提示；结构化 exit code、相关范围和当前 workspace
  身份才是验证证据。禁止验证可为 not_applicable，不能伪装 pass。
- provider 响应转换仍由显式 adapter hook 完成；会话/durable 包装器显式转交，
  不信任动态属性，也不在 kernel 猜模型模板。
- 取消后不启动新 effect；已有 delivery/recovery 仍优先使用原始结算，未执行槽
  结算错误，native terminal 的已知 ID 及有界后续 ID 收到错误回执。
- Research gate 检查并返回同一份 evidence 编译答案，随后该正文进入 synthesis。

固定旧版源码 oracle 与精确差异门见
[确定性 parity 审计](kernel_parity.zh-CN.md)。旧循环只允许由独立子进程 probe
导入 reference tree，禁止回流为生产兼容模块。


## 原策略和恢复事实的当前所有者（2026-10-01）

- 原策略在 dispatch 的首个工具/auto 调用前记录；MutationLine 首次设置后拒绝不同值，相同值幂等。
- runtime 保存策略原 JSON，领域 schema 只由 TaskPolicy 校验；恢复缺失或非法就阻塞，不从新请求补授权。
- delivery recovered 只表示已重建，不推进为 delivered；只有实际交付确认或 abandoned 才终止该批次。
- 已交付工具观察仍投影为 TaskSession 事实；项目和共同入口共用 `kernel_session_recovery.restore_task_session`。
- Research 观察在同一工具收据 canonical 内；`ledger_receipts` 恢复完整正文、页和证据。整批暂存，成功才发布，不建立第二持久日志。
- auto 工具 ACTION 和拒绝完成的回答均继续同一窗口/会话/预算；模型标签不改变原授权或要求。
- command/cwd/URL 是完整身份；展示边界可以裁剪，事实和完成关联不裁剪。

可执行检查与准确的证明边界见 [kernel_invariants.zh-CN.md](kernel_invariants.zh-CN.md)。
