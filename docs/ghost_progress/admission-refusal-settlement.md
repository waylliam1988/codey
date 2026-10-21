# Ghost 实验：Admission Refusal Settlement

- 实验名称：Admission Refusal Settlement
- 状态：INCOMPLETE
- 负责人：Codex
- 日期：2026-10-21
- 实验 commit：待记录
- 关联 Roadmap 验收门：0.6.1 / provider-send 拒绝与恢复
- 生产合并结论：EXPERIMENT_ONLY

## 目标和假设

Final Payload Admission Placement 将 gate 放在 provider 内，实际拒绝已晚于 durable provider intent。
假设直接抛 GhostAdmissionError 即使 0 HTTP，也会被当前 KernelRecordedProvider 归类 MAYBE_SENT；
使用已有 RequestPrepError 契约可准确结算 NOT_SENT，而不修改全局错误分类或保留第二个 kernel gate。

## 实验配置

- 模型 / suite / endpoint：synthetic-payload-placement / 待运行 / 临时 loopback HTTP，无 live 模型。
- tasks / cells / reps：两种 codec × lease expiry/revoke × 直接 GhostError / RequestPrepError，8 项各一次。
- on/off 对照：实际 scoped store 原本开启，lease 时到期或关闭；两组只改变拒绝错误类型。
- actual builder/kernel/ApiProvider、GhostInboxStore、AppContext durable SQLite、KernelEffectSink、
  KernelRecordedProvider 与实际 provider intent/settlement。无 mocked durable verdict。
- 使用实验 producer 声明 arm；旧 kernel predicate 被替换，不叠加校验。所有状态仅本地归档。
- context 65536 / reserve 8192 / keep recent 12000 / output 128 / temperature 0 / timeout 5s / max turns 1。
- counter 和 lease 时点为显式 fixture；不作 tokenizer、语言或成本/延迟推广。
- 拒绝 expected 为 provider_failure，仍必须实际生成并关闭 provider intent、平衡 lease。

## 验证命令和结果

待运行。脚本复用 payload_admission_placement 的显式 durable/refusal-type 参数，不修改生产。

## 当前生产边界

- 是否经过真实 provider-send：计划真实 recorded provider-send 方法，安全拒绝 cell 预期 0 HTTP。
- 是否改变默认 prompt：否。
- 支持模型 / 任务 / 配置：两个 API codec 的确定性拒绝边界；无模型效果结论。
- 生产合并结论：EXPERIMENT_ONLY。

## 未完成或阻塞 / 下一步

先验证 durable sent_state；不能把无 HTTP 当作 runtime 恢复已通过。
本轮不证明完整 native tool delivery 重试、跨 run 恢复、canonical lineage 或 web 远端历史。
