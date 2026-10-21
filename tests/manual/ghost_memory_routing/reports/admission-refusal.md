# Admission Refusal Settlement — offline report

- 日期：2026-10-21；负责人：Codex。
- 实验 commit：`b1a787234a1e039de361f77573e3bf1d23705414`。
- 决策：EXPERIMENT_ONLY / INCOMPLETE；无生产补丁，未改默认 prompt。
- 假设：provider 内拒绝发生在 durable intent 之后，直接 GhostError 被误视为 MAYBE_SENT；
  现有 RequestPrepError 可表达明确的 HTTP 前拒绝，不需要放宽未知网络错误分类。

## 配置和真实路径

synthetic-payload-placement，临时 loopback endpoint；无 live 模型或 KoboldCpp 调用。
Completions/Responses × lease expiry/revoke × GhostError/RequestPrepError，8 项各一次。
suite 分别为 `final_admission_refusal_ghost_20261021` 和 `final_admission_refusal_prep_20261021`。
相同 builder、kernel、final-attempt validator、producer 声明、真实 scoped GhostInboxStore，
唯一对照变量是明确拒绝后的异常类型。当前 kernel predicate 在实验中被替换，没有叠加 validator。

实际 AppContext SQLite、RuntimeMutationLine、KernelEffectSink 和 KernelRecordedProvider；
真实创建并结算 provider_send intent，sent_state 直接从 RuntimeEffectStore 读取，没有 mock verdict。
context 65536、reserve 8192、keep recent 12000、output 128、temperature 0、timeout 5s、max turns 1。
时间、pressure counter 与 lease 触发为 deterministic fixture，不作模型容量或延迟测量。

## 结果

| 拒绝表达 | cells | HTTP | provider effects | sent_state | acquire/release |
|---|---:|---:|---:|---|---|
| GhostAdmissionError | 4 | 0 | 每项 1，已 error 结算 | 全部 maybe_sent | 每项 1/1 |
| RequestPrepError | 4 | 0 | 每项 1，已 error 结算 | 全部 not_sent | 每项 1/1 |

8/8 完整。两个 codec、expiry 和 revoke 均一致。拒绝是预期 provider_failure，不是服务故障、
模型行为阴性或语言评分。没有 pending/missing effect 被当成零；所有 lease 释放。
直接抛 source ValueError 的 prototype 虽然网络安全，但会丢失明确 NOT_SENT 的恢复依据。
prep adapter 修正这个表达，未改变任何未知网络故障分类，也未改生产 classifier。

## 复现、限制与下一步

```powershell
python tests/manual/ghost_memory_routing/payload_admission_placement.py --suite admission_refusal_ghost_repro --placement attempt_declared_json --case lease_expiry lease_revoke --durable --metrics-out artifacts/ghost_memory_routing/admission-refusal-ghost-repro.json
python tests/manual/ghost_memory_routing/payload_admission_placement.py --suite admission_refusal_prep_repro --placement attempt_declared_json --case lease_expiry lease_revoke --durable --refusal-type request_prep --metrics-out artifacts/ghost_memory_routing/admission-refusal-prep-repro.json
```

指标：[Ghost error](admission-refusal-ghost-metrics.json)、[prep error](admission-refusal-prep-metrics.json)。
已有 suite 不覆盖；raw/state/manifest/policy/log 只在 ignored artifacts；无原始内容提交。
真实 provider-send 方法已调用，HTTP=0 是受测 gate 的拒绝结果；不能写成真实模型已响应。

```powershell
python -m pytest tests/test_provider_not_sent_settlement.py tests/test_native_delivery.py tests/test_tool_result_delivery.py tests/test_settled_delivery_recovery.py tests/test_restart_preserves_facts_and_pending_delivery.py -q --tb=short
```

结果：47 passed、21 skipped、4 subtests passed（12.33s）；Ruff 与 diff check 通过。
跳过项来自已有 retired JSONL/projection fixture，不是本轮新增 skip，不算通过；现有回归
不能替代新 candidate 的 native delivery/restart battery。生产源码未改，类型检查引用同轮
placement 记录（mypy 495 files clean、Pyrefly 0 errors/29 suppressed/74 warnings、ty clean）。

本轮证明 durable 拒绝表达，不证明完整 native delivery 的同 batch 重试、跨 run 恢复、
canonical source lineage、web 远端 history 或生产 trace/wire parity。无模型成本/延迟收益结论。
保留实验 adapter 和负结果，无生产回滚需求；不能把所有 Ghost/网络错误一概改为 NOT_SENT。
下一步补来源继承与派生预算，再整合单一 gate 并验证真实 entry/native 恢复。World Model 保持 SHADOW。
