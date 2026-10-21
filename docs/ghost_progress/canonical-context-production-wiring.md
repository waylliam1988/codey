# Canonical Context Production Wiring

实验名称：Canonical Context Production Wiring

状态：INCOMPLETE（生产代码已进入本地 master；完整验收仍待完成）

负责人：Codex

实验 commit：已有 `3d928e51`、`0c5f9993`、`890580cc`；本候选生产 commit 见 git log。

关联 Roadmap 验收门：0.6.1 Context Epoch admission；Canonical Checkpoint Unification。

## 本次完成

- `conversation_plan` 不再读取或拼接 `provider.export_work_state()`；新窗口只从 durable context 恢复。
- durable context snapshot 将 semantic message 到 Completions/Responses protocol item 的来源映射显式保存，Responses 的 function call 展开不会错配来源。
- compaction 提交先以 `expected_revision + expected_head` 写入 durable `context_edit`，成功后才提交 provider ledger；冲突时 provider view 不变。
- `before_entry_id` 截断会折叠带完整 `source_entry_ids` 且仅作用于 cutoff 之前事实的后提交 edit；未来来源不会倒灌。
- provider 的唯一 Attempt Gate 位于每次物理 HTTP attempt、lease 之后、transport 之前；拒绝统一为 `RequestPrepError`，保持 `NOT_SENT` 语义，retry 会重新检查，auxiliary provider 继承 gate。
- 修正 dispatch 对 `bind_durable_context` 的已绑定方法调用，恢复路径不再因 wiring 错误提前失败。

## 验证命令和结果

```powershell
python -m pytest tests/durable tests/test_ghost_context_admission.py tests/test_ghost_provider_send_boundary.py tests/test_ghost_control_contract.py tests/test_ghost_controls.py tests/test_ghost_checkpoint_unification_report.py tests/test_ghost_protocol_source_lifetime_report.py tests/test_ghost_canonical_source_fence_report.py tests/test_conversation_resume_reuses_bound_window.py tests/test_protocol_window_persistence.py tests/test_context_compaction_transactions_and_lifecycle.py tests/test_context_compaction_within_long_tool_turn.py tests/test_compaction_source_encoding_and_summary_contract.py tests/test_compaction_source_reduction_and_packing.py tests/test_compaction_counted_tool_views.py tests/test_compaction_request_token_accounting.py tests/test_responses_compaction_preserves_complete_exchanges.py tests/test_zen_compaction_auxiliary_request_boundary.py tests/test_native_boundary_message_range.py tests/test_api_fresh_transport_recovers_facts_without_old_calls.py tests/test_provider_attempt_gate.py tests/test_provider_durable_compaction_binding.py -q --tb=short
```

结果：669 passed，5 skipped。

```powershell
python -m pytest tests/test_provider_attempt_gate.py tests/test_ghost_provider_send_boundary.py tests/durable/test_crash_process_recovery.py::test_real_process_restart_delivers_settled_tool_without_reexecution -q --tb=short
python -m mypy codey --python-version 3.11
pyrefly check codey --python-version 3.11
ty check codey --python-version 3.11
python -m ruff check codey tests/test_provider_attempt_gate.py tests/test_provider_durable_compaction_binding.py tests/durable/test_conversation_plan_no_provider_export.py tests/durable/test_context_edit_folding.py
```

结果：Attempt Gate 2 passed，Ghost boundary 36 passed，恢复 1 passed；mypy 495 files clean；Pyrefly 0 errors（29 suppressed，76 warnings）；ty 和 Ruff 通过。

## 实验配置

- 模型 / suite / endpoint：fixture / durable focused + Ghost boundary；loopback/localhost synthetic provider；无 KoboldCpp live 样本。
- tasks / cells / reps：durable/runtime 回归 669 tests；专项 gate 2 cells；恢复 1 cell。
- on/off 对照：Ghost boundary 既有 enabled/disabled/scoped control；durable source mapping 与 stale revision/head 对照。

## 当前生产边界

- 是否经过真实 provider-send：是，loopback HTTP、native/Completions/Responses、恢复路径均经过生产 builder/kernel/provider-send；不是 live 模型行为证据。
- 是否改变默认 prompt：否。仅改变恢复事实来源、来源绑定和发送前 gate。
- 支持的模型 / 任务 / 配置：ApiProvider 的 Completions/Responses；有 Ghost sources 的 shared task kernel；无 source 的 provider 保留原 kernel admission。
- 生产合并结论：`PRODUCTION_MERGED`（仅本记录覆盖的 durable context / Attempt Gate 接线）；整体 0.6.1 仍为 `INCOMPLETE`。

## 当前 commit 上的后续边界验证

在 `bfac20d8` 上重跑了两个此前的负结果探针。它们都经过真实 task entry、builder、kernel、
ApiProvider 和临时 loopback HTTP；没有调用 KoboldCpp，也没有模型行为评分。

- `history_expiry_probe.py --suite ghost_history_expiry_repro_20261021`：text/native ×
  不过期/过期共 4 cell；不过期两项各发送 2 次，过期两项均在第二次 HTTP 前以
  `provider_failure` 拒绝，`expired_hint_sent=false`。
- `control_revocation_probe.py --suite ghost_control_revocation_repro_20261021`：
  text/native × enabled/disabled_initially/disabled_after_first 共 6 cell；初始关闭不发送
  hint，首轮后关闭两项均在第二次 HTTP 前明确拒绝，`revoked_hint_resent=false`。

脱敏指标保存在 [history expiry](../../tests/manual/ghost_memory_routing/reports/attempt-gate-history-expiry-20261021.json)
和 [control revocation](../../tests/manual/ghost_memory_routing/reports/attempt-gate-revocation-20261021.json)。
这证明当前 Attempt Gate 闭合了这两个具体 loopback 缺口，不等于完整 0.6.1 通过。

## 收益、限制和阴性结果

- 收益：旧 provider 内存 work-state 不再进入新窗口；durable compaction 可跨重启重建；stale source edit 被 revision/head 拒绝；lease/retry 时物理 attempt 重新做 gate。
- 限制：没有 live KoboldCpp 语言收益、成本或延迟结论；history 中非显式 `source_entry_ids` 的旧 context_edit 不会被 cutoff 追溯应用；辅助 prompt 只检查已声明的原文/JSON fragment 表示。
- 阴性结果：仅删除 exporter 的窄预算失忆仍保留在既有实验报告中，未合并为生产实现。

## 未完成或阻塞

- 远端 `origin/master` 尚未与当前本地 `master` 同步；本记录只声明本地 master 已包含 `3d025cce`。
- 仍缺 repair、receipt、resume、compaction、并发 scope、完整 live/off 和成本/延迟证据。
- World Model 仍为 `SHADOW`，没有第二套历史、默认 prompt 或生产消费者。

## 下一步

- 继续补齐真实 provider payload 的 repair、receipt、resume、compaction 和 scope acceptance；保留回滚点。
- 运行 KoboldCpp live smoke 时先记录 endpoint、model ID、context、suite、配置；故障只归档，不重试污染样本。
- 继续研究 command/World Model，仅在有明确生产消费者和真实 send/admission/recovery 证据后晋级。
