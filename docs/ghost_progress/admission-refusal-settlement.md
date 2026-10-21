# Ghost 实验：Admission Refusal Settlement

- 实验名称：Admission Refusal Settlement
- 状态：INCOMPLETE
- 负责人：Codex
- 日期：2026-10-21
- 实验 commit：`b1a787234a1e039de361f77573e3bf1d23705414`
- 关联 Roadmap 验收门：0.6.1 / provider-send 拒绝与恢复
- 生产合并结论：EXPERIMENT_ONLY

## 目标和假设

Final Payload Admission Placement 将 gate 放在 provider 内，实际拒绝已晚于 durable provider intent。
假设直接抛 GhostAdmissionError 即使 0 HTTP，也会被当前 KernelRecordedProvider 归类 MAYBE_SENT；
使用已有 RequestPrepError 契约可准确结算 NOT_SENT，而不修改全局错误分类或保留第二个 kernel gate。

## 实验配置

- 模型 / suite / endpoint：synthetic-payload-placement /
  final_admission_refusal_ghost_20261021、final_admission_refusal_prep_20261021 /
  临时 loopback HTTP，无 live 模型。
- tasks / cells / reps：两种 codec × lease expiry/revoke × 直接 GhostError / RequestPrepError，8 项各一次。
- on/off 对照：实际 scoped store 原本开启，lease 时到期或关闭；两组只改变拒绝错误类型。
- actual builder/kernel/ApiProvider、GhostInboxStore、AppContext durable SQLite、KernelEffectSink、
  KernelRecordedProvider 与实际 provider intent/settlement。无 mocked durable verdict。
- 使用实验 producer 声明 arm；旧 kernel predicate 被替换，不叠加校验。所有状态仅本地归档。
- context 65536 / reserve 8192 / keep recent 12000 / output 128 / temperature 0 / timeout 5s / max turns 1。
- counter 和 lease 时点为显式 fixture；不作 tokenizer、语言或成本/延迟推广。
- 拒绝 expected 为 provider_failure，仍必须实际生成并关闭 provider intent、平衡 lease。

## 验证命令和结果

脚本复用 payload_admission_placement 的显式 durable/refusal-type 参数，不修改生产。
8/8 完整，两个 codec 的 expiry/revoke 结论一致：

| 拒绝类型 | cells | 实际 HTTP | durable error_code | durable sent_state |
|---|---:|---:|---|---|
| 直接 GhostAdmissionError | 4 | 0 | GhostAdmissionError | maybe_sent（4/4） |
| 现有 RequestPrepError 契约 | 4 | 0 | RequestPrepError | not_sent（4/4） |

每个 cell 恰好创建并结算一个真实 provider_send effect，status=error；acquire/release 均为 1/1。
没有缺失 settlement 被当成零，没有 mock sent_state，也没有修改生产全局分类器。
源拒绝类型由同一个 validator 产生，实验 adapter 只在明确拒绝且没有 HTTP 时将错误表达为
RequestPrepError；这个结论不得推广为所有 Ghost 错误、未知网络失败或已发请求一律 NOT_SENT。

```powershell
python tests/manual/ghost_memory_routing/payload_admission_placement.py --suite admission_refusal_ghost_repro --placement attempt_declared_json --case lease_expiry lease_revoke --durable --metrics-out artifacts/ghost_memory_routing/admission-refusal-ghost-repro.json
python tests/manual/ghost_memory_routing/payload_admission_placement.py --suite admission_refusal_prep_repro --placement attempt_declared_json --case lease_expiry lease_revoke --durable --refusal-type request_prep --metrics-out artifacts/ghost_memory_routing/admission-refusal-prep-repro.json
python -m ruff check tests/manual/ghost_memory_routing/payload_admission_placement.py
```

报告保存时使用上面配置段的 suite，metrics-out 分别为
`tests/manual/ghost_memory_routing/reports/admission-refusal-ghost-metrics.json`、
`tests/manual/ghost_memory_routing/reports/admission-refusal-prep-metrics.json`。
新 suite 拒绝覆盖，exit 0 是矩阵完整，不代表端到端恢复完成。
脱敏 [Ghost error 指标](../../tests/manual/ghost_memory_routing/reports/admission-refusal-ghost-metrics.json)、
[prep error 指标](../../tests/manual/ghost_memory_routing/reports/admission-refusal-prep-metrics.json)、
[离线报告](../../tests/manual/ghost_memory_routing/reports/admission-refusal.md)。
原始请求/state/policy/manifest/result/log 只归档 ignored `artifacts/ghost_memory_routing/`。

```powershell
python -m pytest tests/test_provider_not_sent_settlement.py tests/test_native_delivery.py tests/test_tool_result_delivery.py tests/test_settled_delivery_recovery.py tests/test_restart_preserves_facts_and_pending_delivery.py -q --tb=short
```

结果：47 passed、21 skipped、4 subtests passed（12.33s），脚本 Ruff / diff check 通过。
21 skipped 是现有 retired JSONL/projection 注入 fixture，不是本轮新增跳过，不按通过计数。
这组是现有 native/durable 恢复回归，不能冒充本轮 candidate 的完整 delivery/restart 验收。
本轮仍只改实验代码/报告，生产类型检查结果见 Final Payload Admission Placement 的本轮记录。

## 当前生产边界

- 是否经过真实 provider-send：实际 recorded provider-send → ApiProvider 方法；所有 cell 在 HTTP 前拒绝，HTTP=0。
- 是否改变默认 prompt：否。
- 支持模型 / 任务 / 配置：两个 API codec 的确定性拒绝边界；无模型效果结论。
- 生产合并结论：EXPERIMENT_ONLY。

## 未完成或阻塞 / 下一步

已验证拒绝的 durable sent_state；不能把无 HTTP 或 NOT_SENT 当作完整恢复已通过。
收益是明确迁移必须使用已有 prep 错误契约，无须新增 provider/Ghost 兼容异常分类器。
当前 adapter 仅实验代码，不是生产收益合并；未测模型成本/延迟，无生产回滚需求。
本轮不证明完整 native tool delivery 重试、跨 run 恢复、canonical lineage 或 web 远端历史。
下一步先复用 ContextLedger/checkpoint 来源关系，保证派生项不增加 origin 预算；将表示与来源契约
一起接入单一 final-attempt validator，再验证真实 entry/native delivery/恢复、精确 wire trace 和回滚。
