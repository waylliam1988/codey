# Provider Send Scope Authority

状态：EXPERIMENT_COMPLETE；请求作用域安全修复已合并为 `3d013af1`。
完整 0.6.1 仍 INCOMPLETE；不是 language 晋级。

## 假设与对照

候选上下文不能自行授权。比较 `7d76b812` 生产基线与当前候选，统一使用实际
planning builder → shared kernel → ApiProvider → HTTP 接收端。模型文本不是评分对象。

## 离线证据

`tests/test_ghost_provider_send_boundary.py` 的 text/native 矩阵包括合法 on/off、异项目、
异会话、全局/项目混合、预算、过期、无效过期、缺少字节、禁用、重复 key、trace 故障和空请求 scope。
使用真实 RunTraceStore 验证 source ref、digest、scope、budget、epoch 与 HTTP payload 一致；
拒绝项断言零 HTTP 请求且具有具体拒绝原因。

首次冻结 29 项：9 failed / 20 passed。跨项目、跨会话及 trace 故障下跨项目来源进入 HTTP，
合法的 global + project 混合来源则被误拒绝。扩展两项长身份测试确认截断 scope 存在别名。
最终完整边界文件 33 项；和其他相关入口、控制、回归合计 91 passed。
最终扩大范围后 290 passed、399 subtests passed；kernel/parity 另一组 750 passed，
两组存在重叠，不将计数相加为唯一测试样本量。

初版 fixture 使用 coding_writer，而它当前有意排除 Ghost；改用 planning_readonly，
没有放宽生产 permission profile。首次修复从 execution workspace 推导 scope，入口测试揭示它
误拒绝未绑定项目的聊天；最终由用户请求生成 scope，经 transport 显式传入 kernel。
生产 validator 只有 workspace 中的一份，trace 不再执行准入决策，没有兼容导出或 fallback。

## Live 结构 smoke

- endpoint：`http://127.0.0.1:5001/v1`；KoboldCpp 1.117.1。
- model：`koboldcpp/Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M`，context 262144。
- suite：`scope_authority_20261021`；baseline / candidate-final；每阶段 on/off，各一条任务一次重复。
- task：synthetic greeting；planning_readonly；policy 仅 control；max turns 1；native tools true。
- temperature 0，thinking false，output 256，reserve 32768，keep recent 32000；timeout 180s，attempts 1。
- 显式调用生产 `project_adapter.run`，没有经过 HTTP control service 提交或持久化调度；不宣称覆盖外部 AI 控制入口。

| cell | baseline 秒 | candidate-final 秒 | 请求次数 | 完整请求摘要相同 |
|---|---:|---:|---|---|
| on | 1.461 | 0.914 | 各 1 | 是 |
| off | 0.642 | 0.684 | 各 1 | 是 |

均为 done。on/off prompt 长度分别 1176 / 1151 characters，提示占用差 25 characters。
这不是 tokenizer token 数；实际 provider usage 缺失，未填 0。n=1/cell 不能证明延迟或语言收益。
首次 candidate 的两次 smoke 也成功，但入口回归后实现变化，另存 candidate-final，
最终四次比较不重复计入旧 candidate。未发生 KoboldCpp 故障或自动重试。

脱敏数字与摘要见 [scope-authority-live-metrics.json](scope-authority-live-metrics.json)。
全部原始请求/回复、events、usage、manifest 留在 ignored 本地 archive。
candidate-final manifest 保存执行时生产模块 SHA-256；运行时 Git HEAD 是实验 commit，
候选为未提交 working tree patch，最终生产 commit 是 `3d013af1`，不能把 baseline HEAD 冒充候选实现。
live 后仅移除重复局部 import 以满足原有文件行数上限；其余 runtime 行为未变。

## 复现

```powershell
python -m pytest tests/test_ghost_provider_send_boundary.py tests/test_ghost_context_admission.py tests/test_chat_workspace_tools_and_answer_completion.py tests/test_research_iteration_topic_continuity.py tests/test_ghost_controls.py tests/test_runtime_execution_receipt_projection.py -q --tb=short
python -m pytest tests/test_kernel_legacy_parity.py tests/test_kernel_parity_regressions.py tests/test_research_iteration_topic_continuity.py tests/test_task_guidance_reaches_production_entries.py tests/test_production_kernel_calls_use_typed_request.py tests/test_kernel_dependency_boundary.py tests/test_kernel_dependency_direction.py tests/test_headless_real_kernel_lifecycle.py tests/durable/test_kernel_boundary.py -q --tb=short
python -m pytest tests/test_architecture.py tests/test_task_entry_cutover.py tests/test_task_entry_kernel_prod.py tests/test_task_entry_run_trace.py tests/test_research_iteration_topic_continuity.py tests/test_chat_workspace_tools_and_answer_completion.py tests/test_ghost_provider_send_boundary.py tests/test_ghost_context_admission.py tests/test_ghost_controls.py tests/test_runtime_execution_receipt_projection.py tests/durable/test_scheduler.py -q --tb=short
python tests/manual/ghost_memory_routing/scope_authority_smoke.py --phase baseline
python tests/manual/ghost_memory_routing/scope_authority_smoke.py --phase candidate --attempt final
python tests/manual/ghost_memory_routing/scope_authority_report.py --candidate-attempt final --output artifacts/ghost_memory_routing/scope-authority-summary.json
```

历史 baseline 使用 `7d76b812`，candidate-final 使用本实验生产修复 commit；分别在对应代码上运行，
project 和 suite 路径保持一致。新复现必须给 baseline/candidate 两条命令传入相同的新 `--suite`，
报告器用 `--archive artifacts/ghost_memory_routing/<suite>`，已有 phase 目录不允许覆盖。
若发生故障立刻停止，保留 failure/manifest，只在用户恢复后处理缺失或损坏 cell。

## 边界与下一步

收益是拒绝跨请求作用域来源，及解除合法混合来源的误拒绝；不扩大工具权限，
不改变 Evidence / Permission / CompletionProof 规则。受测有效请求字节一致，无额外模型调用。
`admit_hints=False` 的独立 off 路径已验证，scope 无旧格式兼容层。
如需撤销，单独 revert 生产修复 commit；这会恢复已知 scope 缺陷，应先禁用 Ghost hint admission，
保留回归测试使问题继续可见。

仍缺 native tool-result/history 的逐发送生命周期检查、跨入口完整控制面矩阵和完整 language on/off 评估。
World Model 继续 SHADOW，无已验证生产消费者，不接入默认 prompt。
完整历史重发已由后续负结果探针确认仍有 expiry 缺口，见 [history-expiry.md](history-expiry.md)。
全量验证的中止、架构行数失败及未复现 scheduler 线索均在进度文件中保留，不声称全量通过。
