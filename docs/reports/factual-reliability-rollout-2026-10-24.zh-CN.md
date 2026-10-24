# 事实与恢复可靠性五批落地

五批按相关测试、故障边界、实机结果独立验收并合并本地 master。用户最后确认的
标准为明确功能收益且相对原版没有新增回归；不要求清理所有原有问题、全量 pytest
或性能对照。第一批已经做过的性能材料保留，其后没有继续以性能作为准入。

| 批次 | 实现与独立提交 | 验收记录 |
| --- | --- | --- |
| 1 | f6ee7c83：结构化观察、无损 canonical、删除展示/参数补事实 | structured-observations-batch1-2026-10-24.zh-CN.md |
| 2 | 501f0839：临时采集，回执提交后发布，共用实时/恢复投影 | committed-observations-batch2-2026-10-24.zh-CN.md |
| 3 | 0a0d3772：唯一主机时钟，请求计数与发送一致，历史不改写 | request-context-batch3-2026-10-24.zh-CN.md |
| 4 | 0d6c64dc：ledger 来源准入、引用/页码验证、共同 proof 渲染 | source-completion-batch4-2026-10-24.zh-CN.md |
| 5 | 本报告同批提交：固定跨领域对照及通用原生最终答案修复 | cross-domain-batch5-2026-10-24.zh-CN.md |

当前路径只有原 run_task_kernel：结构化采集 → 同一观察校验 → 临时会话/ledger 投影 →
原 KernelEffectSink 完整结果回执提交 → 发布；恢复从回执调用同一投影。来源准入只认
committed ledger，session 保留别名/控制器派生视图。原 provider 最终 payload 边界加入
唯一时间观察，共同完成 gate 产 proof，界面直接渲染该 proof 的依据/限制。
没有新增事实日志、持久账本、模型评委或模型工具运行时。

## 代码定位

- research/ledger_receipts.py 的 search_observation/source_observation/source_search_observation：
  校验字段和类型、来源身份、时间、正文摘要及页信息，生产与恢复共同消费。
- runtime/core/models.py 的 canonical_json_mapping：权威数据无损；presentation/audit
  仍使用原有有界展示投影。toolchain/runtime.py 复用相同 canonical 边界。
- operations/task_execution.py 的 _observation_tools：采集使用临时 ledger；知识读取资格
  从正式观察派生，不能由旧缓存冒充。
- operations/kernel_execution.py 的 settle：临时投影先校验，sink 提交成功才发布。
- operations/kernel_facts.py 的 project_result_observation：实时和恢复的共同投影。
- operations/kernel_session_recovery.py 的 restore_task_session：完整回执、整批发布与去重。
- providers/request_context.py 及 api_provider.py 的 _admit_request/_payload：唯一时间来源，
  实际计数与发送同一 payload，工具续轮和辅助生成共用。
- operations/research_completion_checks.py 与 research/provenance.py：来源和引用契约。
- frontend/src/render.ts 的 receiptSummary：读取原 proof，回答正文不变。
- operations/kernel_prompt.py 的 _repair_prompt：原生最终答案合法重新提交，不解释坏调用。

## 借鉴边界

- DeepSeek Harness packages/web/tool-web/src/search.ts:109：保留忠实结构化结果，展示是
  有损视图；packages/core/agent-loop/src/tool-calls.ts:146 的 commitReady 及 156–157 行：
  先提交结果，再接受上下文。
- Codex codex-rs/core/src/session/time_reminder.rs:104：读取运行时真实时钟；
  context/world_state/environment.rs:35：显式日期/时区。没有复制提醒状态机。
- OpenCode packages/core/src/session/system-prompt.ts:9：按可见工具生成指导。Codey 原有
  TurnSnapshot 已具备这一点，继续复用，没有引入另一份可用工具表。
- 完成门、证据 ledger、受管回执、恢复状态机与报告编译器沿用 Codey 自身机制。

## 结论边界

固定题集每版 18 条：回答检查均为 18/18，恢复由 16/18 改善至 18/18，最终无新增
回归。第五批首次发现的 JSON 原生修复循环已修复并完整重跑，失败材料未删除。
各批测试选择重叠，不把通过数累加成一个全量测试结果。原有 open_hit 跳过行为、
被裁坏的历史回执及开放世界事实语义核验仍是已记录的限制。
