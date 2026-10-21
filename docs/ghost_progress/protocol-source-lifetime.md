# Ghost 实验：Protocol Source Lifetime

- 实验名称：Protocol Source Lifetime
- 状态：INCOMPLETE
- 负责人：Codex
- 日期：2026-10-21
- 实验 commit：`603f01ff459ff4bb87e28c3120d5a087e40a7f82`（归属矩阵）、
  `36566995a0d8c7455a95182641792cc1b14236b5`（重复导出消融）、
  `7c26a1052724e472b4f95567e4e2b1dd2b8e10cc`（脱敏指标、严格复算与破坏证据测试）。
- 关联 Roadmap 验收门：0.6.1 / history 生命周期与撤回
- 生产合并结论：EXPERIMENT_ONLY

## 目标和假设

先前提出复用 ContextLedger 磁盘 journal 保存来源的建议需要更正：生产 ApiProvider 初始化和
bind_context 均构造无 root 的内存 ContextLedger，两个磁盘工厂目前只有测试消费者。
真实 restart authority 是 DurableSession SQLite。不能将 Ghost 候选另存一套历史，
也不能先将候选作为 canonical conversation fact 持久化。

本实验假设旧 hint 主要滞留于 warm protocol view 和 export_work_state → handoff，
而非原始 durable user message。通过真实 task submission / plan / activation / builder /
admission / recorded provider / HTTP / settlement 比较四种生命周期。

## 实验配置

- 模型 / suite / endpoint：synthetic-source-lifetime / protocol_source_lifetime_20261021、
  protocol_source_lifetime_matched_baseline_20261021、protocol_source_lifetime_replay_20261021 / 临时 loopback HTTP。
- Completions、Responses 两种 codec；native tools；四种 lifecycle × on/revoke，共 16 cells。
- 每 cell 两个独立 StartTask，各一次 HTTP，max_turns=1；reps=1。
- 首任务开启，第二任务保持开启或真实 session-scoped admit_hints=False。
- warm：相同 provider、相同 window；window_reopen：生产 registry 显式失效后由真实 planner 重开；
  fresh_provider：保留 AppContext/SQLite、更换空 provider；app_restart：close/reopen AppContext 和 provider。
- window_reopen 不等于 token-pressure rollover，不据此声称后者通过。
- context 65536 / reserve 8192 / keep_recent 12000 / output 128 / temperature 0 / timeout 5s。
- 固定 synthetic directive producer、非回显原生 done；不 patch gate、replay、window 决策或 settlement。
- 注入 connect_provider 使用既有服务依赖入口，避免 synthetic 模型触发真实模型发现。

## 本次完成

建立 `tests/manual/ghost_memory_routing/protocol_source_lifetime.py`，捕获每次真实规划的
window action、handoff/replay 是否含 hint，并读取 durable entries/context、conversation snapshot、
transport/export 和实际 effect settlement。只导出布尔、计数和摘要；raw 请求/事件/state 本地归档。

## 验证命令和结果

初次 16 cell 归属矩阵完整，32 次 HTTP。随后同 commit 的 baseline / durable_replay 消融各 16 cells、
32 tasks、32 HTTP、64 个已结算 effect；后者包含 32 个本地 acknowledge_tool_results，不能算 HTTP。
初次矩阵和 setup 不与匹配对照合并分母。两 codec 完全一致：

| 生命周期 | baseline 撤回后发送 | 消融后发送 | 两臂健康 on |
|---|---:|---:|---:|
| warm | 2/2 | 2/2 | 各 2/2 |
| 同 provider reopen | 2/2 | 0/2 | 各 2/2 |
| fresh provider | 0/2 | 0/2 | 各 2/2 |
| AppContext restart | 0/2 | 0/2 | 各 2/2 |

所有第二轮请求保留上一轮 task/result 的 fixture 标记。两臂全部 96 个观测点的 durable
entries/context 均无 hint；reopen baseline handoff 含 hint，候选不含。canonical SQLite replay
不需要再持久化这份 Ghost 注入。候选仅实验替换 export_work_state 为空，不改 gate/replay/结算。
这不证明 checkpoint/compaction 的完整语义等价，生产仍 INCOMPLETE。

setup 第一、二次因实验断言错误停止：provider_send 属于 effect_category，而非 tool_name；
成功原生 done 同时结算 send_turn 和本地 acknowledge_tool_results，后者不发 HTTP。
生产未为脚本增加兼容；失败归档为 source_lifetime_setup_20261021、source_lifetime_setup2_20261021。
第三次 setup warm on/revoke 两项完整，2 tasks/cell；revoked hint 第二次仍发送，durable 无该 hint。
setup 不是正式矩阵样本，不混合计数。

```powershell
python tests/manual/ghost_memory_routing/protocol_source_lifetime.py --suite protocol_lifetime_baseline_repro --metrics-out artifacts/ghost_memory_routing/lifetime-baseline-repro.json
python tests/manual/ghost_memory_routing/protocol_source_lifetime.py --suite protocol_lifetime_replay_repro --handoff-source durable_replay --metrics-out artifacts/ghost_memory_routing/lifetime-replay-repro.json
python tests/manual/ghost_memory_routing/protocol_source_lifetime_report.py --baseline artifacts/ghost_memory_routing/lifetime-baseline-repro.json --replay artifacts/ghost_memory_routing/lifetime-replay-repro.json
python tests/manual/ghost_memory_routing/protocol_source_lifetime_report.py
python -m pytest tests/test_ghost_protocol_source_lifetime_report.py -q --tb=short
python -m ruff check tests/manual/ghost_memory_routing/protocol_source_lifetime.py tests/manual/ghost_memory_routing/protocol_source_lifetime_report.py tests/test_ghost_protocol_source_lifetime_report.py
python -m pytest tests/test_conversation_lifecycle.py tests/test_conversation_registry.py tests/test_conversation_resume_reuses_bound_window.py tests/test_protocol_window_persistence.py tests/test_context_checkpoint_commit_and_recovery.py tests/durable/test_generation_boundary_commit.py tests/durable/test_provider_sink_generation_boundary.py tests/durable/test_provider_context_authority.py tests/test_api_provider_request_policy_lifecycle.py tests/test_ghost_context_admission.py tests/test_ghost_controls.py tests/test_ghost_control_contract.py tests/test_ghost_control_surface.py -q --tb=short
python -m pytest tests/test_api_chat_native_turns_and_history.py tests/test_api_fresh_transport_recovers_facts_without_old_calls.py tests/test_api_exchange_lifecycle_consistency.py tests/test_api_request_context_counting.py tests/test_context_compaction_transactions_and_lifecycle.py tests/test_compaction_source_encoding_and_summary_contract.py tests/test_compaction_source_reduction_and_packing.py tests/test_provider_not_sent_settlement.py tests/test_native_delivery.py tests/test_tool_result_delivery.py tests/test_settled_delivery_recovery.py tests/test_restart_preserves_facts_and_pending_delivery.py -q --tb=short
python -m mypy codey --python-version 3.11
pyrefly check codey --python-version 3.11
ty check codey --python-version 3.11
```

结果：报告从已提交指标可独立复算；完整性破坏测试 6 passed (0.72s)；两组生产回归依次
108 passed (6.76s)、120 passed / 21 skipped / 4 subtests passed (14.79s)。21 skipped 是现有
retired JSONL/projection fixture，不记通过。mypy 495 文件 clean，Pyrefly 0 errors / 29 suppressed /
74 warnings，ty clean，Ruff/diff clean。未运行全量 pytest，按研究相关性扩大验证范围。

脱敏 [报告](../../tests/manual/ghost_memory_routing/reports/protocol-source-lifetime.md)、
[初次矩阵](../../tests/manual/ghost_memory_routing/reports/protocol-source-lifetime-metrics.json)、
[baseline](../../tests/manual/ghost_memory_routing/reports/protocol-source-lifetime-baseline-metrics.json)、
[消融](../../tests/manual/ghost_memory_routing/reports/protocol-source-lifetime-replay-metrics.json)、
[复算摘要](../../tests/manual/ghost_memory_routing/reports/protocol-source-lifetime-summary.json)。

## 当前生产边界

- 是否经过真实 provider-send：是，实际 ApiProvider/HTTP；确定性 loopback，不是 live 模型。
- 是否改变默认 prompt：否，只有实验 synthetic producer，生产源码不改。
- 支持模型 / 任务 / 配置：限定上述确定性 native done、两任务生命周期；无 language 或模型收益结论。
- 生产合并结论：EXPERIMENT_ONLY；World Model 保持 SHADOW。

## 未完成或阻塞

受测矩阵已完整，不把它当作 0.6.1 生产完成。warm revocation 仍失败；不证明 compaction 派生、token-pressure rollover、native 未完成工具恢复、
远端 web history 或模型回显后的来源传播。无模型成本/延迟测量、无生产候选晋级。
payload_bytes 包含各臂不同 fixture 路径，只作诊断，不能计为隔离的成本收益。
无 KoboldCpp 调用或服务故障，无生产回滚需求；移除实验 exporter patch 即回 baseline。

## 下一步

先测 compaction checkpoint / durable replay 裁剪是否有只能从 export 恢复的工作状态，再决定
重复 handoff 删除的生产边界；不能仅凭两个 markers 保留就全局删除 exporter。
warm history 的 metadata 应依附真实 live protocol view/变换，origin 不获得重复预算；
不新建 persistence root、不把 Ghost hint 写作 canonical user fact、不叠加第二个 admission。
按用户最新目标，持久会话事实继续以 durable SQLite 为唯一 authority；协议视图只承担发送表示，
不为了来源追踪再增加可恢复历史、兼容路径或 fallback。
