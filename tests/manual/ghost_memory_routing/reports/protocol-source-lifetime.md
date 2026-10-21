# Protocol Source Lifetime：真实恢复宿主和重复 handoff 消融

结论：当前旧 hint 滞留于 live protocol history；同一 provider 重开时又经
`export_work_state → handoff` 送回。实验中删除额外导出可消除受测 reopen 泄漏，
但不能修复 warm history，且尚未验证 compaction/checkpoint 语义，维持 **EXPERIMENT_ONLY / INCOMPLETE**。

## 配置与样本

- 负责人 Codex，2026-10-21；关联 0.6.1，生产源码、默认 prompt 均不改。
- 初次归属矩阵：`603f01ff459ff4bb87e28c3120d5a087e40a7f82`，16 cells / 32 tasks / 32 HTTP。
  [初次指标](protocol-source-lifetime-metrics.json) 独立保存，不与后续对照合并分母。
- 对照与候选：`36566995a0d8c7455a95182641792cc1b14236b5`，**每臂 16 cells / 32 tasks / 32 HTTP**。
- 指标、复算器和破坏证据测试：`7c26a1052724e472b4f95567e4e2b1dd2b8e10cc`。
- 模型 synthetic-source-lifetime；临时 loopback `/v1`，Completions/Responses，native tools。
- 四种生命周期 × 第二任务 on/scoped revoke × 两种协议；各 cell 一次，无模型随机采样。
- context 65536 / reserve 8192 / recent 12000 / output 128 / temperature 0 / timeout 5s / max_turns 1。
- 每 cell 两个真实独立 StartTask；第一轮始终开启。固定 directive producer 和不回显的原生 done 响应。
- 实际 AppContext、build_task_deps、run_task_submission、纯 planner/apply/activation、builder、
  admission、recorded provider、HTTP、generation/terminal settlement；不伪造 replay、gate 或结算。
- 每任务结算 send_turn 和本地 acknowledge_tool_results 两项 effect；后者不发送 HTTP。
  每臂 64 个已成功结算 effect，不能将其计成 64 次 HTTP。
- window_reopen 由真实 provider registry 显式失效触发；**没有模拟 token-pressure rollover**。

## 匹配结果

下表各分母 2 = 两种 codec 各一次；“健康 hint”是第二轮保持 on。

| 生命周期 | baseline 撤回后 hint 发送 | durable replay 消融 | baseline / 消融健康 hint |
|---|---:|---:|---:|
| warm，相同 provider/window | 2/2 | 2/2 | 2/2 / 2/2 |
| 同 provider window reopen | 2/2 | 0/2 | 2/2 / 2/2 |
| 新 provider，同 AppContext | 0/2 | 0/2 | 2/2 / 2/2 |
| AppContext + provider 完整重启 | 0/2 | 0/2 | 2/2 / 2/2 |

两臂所有第二轮 HTTP 均含上一轮 user task / result 的 fixture 标记；这仅证明受测 markers 保留，
不等于任意任务的完整语义等价。96 个归属观测点（32 cells × 3）中 durable entries/context
均不含 hint，conversation snapshot 同样无 hint。reopen baseline handoff 含 hint，
候选 handoff 不含 hint；fresh provider/restart 的 durable replay 均不含 hint。

候选仅在实验 submit 中将 `provider.export_work_state` 替换为空字符串；真实 planner
仍选择并激活 canonical SQLite context，所有其他生产发送行为保持实际执行。
未按内容过滤、未新建来源数据库、未恢复被删除的 admission.py，未叠加第二个 gate。
原始库/日志可能在 audit 中记录请求摘要，本结论限于测量的 conversation entries/context 和 snapshot，
不声称所有本地文件绝不含 hint。

## 复现与结果完整性

```powershell
python tests/manual/ghost_memory_routing/protocol_source_lifetime.py --suite protocol_lifetime_baseline_repro --metrics-out artifacts/ghost_memory_routing/lifetime-baseline-repro.json
python tests/manual/ghost_memory_routing/protocol_source_lifetime.py --suite protocol_lifetime_replay_repro --handoff-source durable_replay --metrics-out artifacts/ghost_memory_routing/lifetime-replay-repro.json
python tests/manual/ghost_memory_routing/protocol_source_lifetime_report.py --baseline artifacts/ghost_memory_routing/lifetime-baseline-repro.json --replay artifacts/ghost_memory_routing/lifetime-replay-repro.json
python tests/manual/ghost_memory_routing/protocol_source_lifetime_report.py
python -m pytest tests/test_ghost_protocol_source_lifetime_report.py -q --tb=short
```

前三条重跑实际 HTTP；第四条直接从随仓库的脱敏指标复算，不需 KoboldCpp 或 raw state。
归档 suite 分别为 protocol_source_lifetime_matched_baseline_20261021、protocol_source_lifetime_replay_20261021。
setup 两次在实验 effect 断言处失败，第三次 warm smoke 完整；均不纳入上述分母，见进度文件。
同名 suite 拒绝覆盖；raw requests/events/state/manifest/console 仅保留 ignored artifacts。

完整性检查会拒绝缺失 off、重复 cell、缺 HTTP/ack、丢失历史任务标记或不同 commit 的对照。
六项破坏证据测试 **6 passed (0.72s)**。生产回归分别 **108 passed (6.76s)** 与
**120 passed、21 skipped、4 subtests passed (14.79s)**；完整命令见进度记录。
21 skipped 是现有 retired JSONL/projection fixture，不计通过，不证明候选恢复通过。
类型：mypy 495 文件 clean；Pyrefly 0 errors / 29 suppressed / 74 warnings；ty clean；Ruff/diff clean。

[baseline 指标](protocol-source-lifetime-baseline-metrics.json)、
[候选指标](protocol-source-lifetime-replay-metrics.json)、[复算摘要](protocol-source-lifetime-summary.json)。

## 限制、收益和下一步

收益是定位实际生产消费者，否定“需要为 Ghost 加一套磁盘 journal”的先验，并证明去掉
受测重复导出可防 reopen 复活。production 合并结论仍为 EXPERIMENT_ONLY：warm 泄漏未修，
没有 live/language 效果、token 成本/延迟、token-pressure rollover、未完成 native 工具恢复、
compaction 派生、远端 history 或模型回显传播的证据。payload_bytes 含两臂不同的 fixture 路径，
仅诊断用，不据此推断隔离的字节收益、token 或成本。

下一项先测 compacted work-state 在 durable replay 裁剪后是否仍有唯一信息；若存在，
不能直接全局删除 exporter。warm 来源应随已有协议视图及其变换保留，最终复用唯一发送 gate；
不能将当前候选升级为 canonical facts。无生产改动，无生产回滚；移除实验 patch 即回 baseline。
World Model 保持 SHADOW，KoboldCpp 本轮未调用，无服务故障。
