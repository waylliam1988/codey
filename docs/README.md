# 文档索引

产品能力、当前架构、未来方向和历史测量分开维护。日期较旧不自动意味着失效；
历史测试数字也不代表当前工作树已通过验证。当前测试与实机状态以根目录
[TEST_REPORT.md](../TEST_REPORT.md)为准，界面规则以[DESIGN.md](../DESIGN.md)为准。

## 产品、架构与验证入口

| 文档 | 内容 |
|---|---|
| [codey_capabilities.md](codey_capabilities.md) | Product capabilities (English) |
| [codey_capabilities.zh-CN.md](codey_capabilities.zh-CN.md) | 产品能力说明 |
| [project_structure.md](project_structure.md) | Source ownership (English) |
| [project_structure.zh-CN.md](project_structure.zh-CN.md) | 项目结构与职责地图 |
| [runtime_architecture.zh-CN.md](runtime_architecture.zh-CN.md) | 当前 Runtime 架构 |
| [api_request_reliability_architecture.zh-CN.md](api_request_reliability_architecture.zh-CN.md) | API 请求、取消、连接与恢复边界 |
| [model-management.md](model-management.md) | 模型管理与可选连接拆除 |
| [token-accounting.zh-CN.md](token-accounting.zh-CN.md) | 请求预算与 token 用量归属 |
| [kernel_invariants.zh-CN.md](kernel_invariants.zh-CN.md) | 内核不变量；文内历史验证只对应记录版本 |
| [kernel_parity.zh-CN.md](kernel_parity.zh-CN.md) | 固定 oracle、差异分类和 parity 检查 |
| [codey_event_matrix.md](codey_event_matrix.md) | 事件与能力矩阵；测试直接读取，不作为普通历史材料删除 |
| [release_gate.zh-CN.md](release_gate.zh-CN.md) | 发布验收规则与实机范围 |
| [typechecking.md](typechecking.md) | Python 三检查器、目标版本和前端类型边界 |
| [zen_request_profile_2026-10-08.md](zen_request_profile_2026-10-08.md) | Zen 请求适配边界；实测数字限于记录范围 |
| [conversation_ui_implementation_2026-10-16.md](conversation_ui_implementation_2026-10-16.md) | 中间对话区实现、TDD 回归与真实页面验收记录 |

## 未来方向与实施基线

这些文档保留未完成目标、约束或研究方向，不是当前 API 说明或功能承诺。
实施新工作前先核对现行源码与验收项，不按旧路径恢复已经删除的模块。

- [设计原则与后续建议](codey_from_codex_principles.md)
- [Codey Roadmap：0.6 语言闭环、后续按族晋级与独立研究](../ROADMAP.zh-CN.md)
- [Ghost 本地经验路由：偏好投递与预算路由实验、限制与晋级协议](ghost_future_direction.zh-CN.md)
- [Ghost 长期学习研究愿景：可停止的研究假设，不作为 0.6 前置条件](codey_ghost_long_term_learning_architecture.md)
- [Pi 启发的历史重构讨论与后续建议](codey_pi_v2_refactor_direction.zh-CN.md)
- [跨版本工具协议策略](tool_protocol_portability_plan.zh-CN.md)
- [传输／协议窗口验收基线，仍需逐项核对完成定义](tdd_api_transport_and_protocol_window_plan.zh-CN.md)

## 仍被引用的版本和专题记录

这些材料保留原始结论与验证边界；不将历史测量更新成未经执行的新结果。

- [0.5.10 版本提交记录](release_0.5.10.zh-CN.md)
- [0.5.11 发布验收记录](release_0.5.11.zh-CN.md)
- [上下文压缩优化的本地对比、失败与限制](context-compaction-optimization-2026-10-09.zh-CN.md)
- [模型和 CI 问题的复现与修复记录](provider_ci_diagnosis_2026-10-13.zh-CN.md)
- [UI 维护与连接拆除审计记录](ui_maintenance_audit_2026-10-13.zh-CN.md)
- [网页自动化语言迁移评估；与已完成的前端迁移不同](web-automation-typescript-assessment.md)

## 历史实验与验收证据

以下材料包含仍被 roadmap、测试说明或比较结论引用的证据，
以及尚未决定移除的已完成实验，暂时保留。
如果后续清理，先保留必要决策与失败边界，再同步处理引用和关联 JSON。

- [Ghost 偏好投递与预算路由实验：脚本、历史报告与脱敏测量](../tests/manual/ghost_memory_routing/README.md)
  可独立复算各 cell 的分子/分母；原始回复与隔离 state 仅在被 Git 忽略的本地产物目录保存。

### 0.4 阶段矩阵与 provider 基线（5）

- [0.4_ab_stabilization_plan.zh-CN.md](0.4_ab_stabilization_plan.zh-CN.md)
- [0.4_deepseek_provider_baseline.zh-CN.md](0.4_deepseek_provider_baseline.zh-CN.md)
- [0.4_final_stabilization_report.zh-CN.md](0.4_final_stabilization_report.zh-CN.md)
- [0.4_mimo_provider_baseline.zh-CN.md](0.4_mimo_provider_baseline.zh-CN.md)
- [0.4_qwen_provider_baseline.zh-CN.md](0.4_qwen_provider_baseline.zh-CN.md)

### 旧内核／入口／本地模型验收快照（7）

- [kernel_convergence_review.zh-CN.md](kernel_convergence_review.zh-CN.md)
- [kernel_review_2026-10-02.zh-CN.md](kernel_review_2026-10-02.zh-CN.md)
- [kernel_review_2026-10-03.zh-CN.md](kernel_review_2026-10-03.zh-CN.md)
- [local_model_gate_2026-10-02.zh-CN.md](local_model_gate_2026-10-02.zh-CN.md)
- [local_native_protocol_2026-10-02.zh-CN.md](local_native_protocol_2026-10-02.zh-CN.md)
- [automatic-local-review-2026-10-04.zh-CN.md](automatic-local-review-2026-10-04.zh-CN.md)
- [desktop-cli-task-parity-2026-10-04.zh-CN.md](desktop-cli-task-parity-2026-10-04.zh-CN.md)

### 已完成的对比／审核实验（9）

- [codey-opencode-pi-comparison-2026-10-10.zh-CN.md](codey-opencode-pi-comparison-2026-10-10.zh-CN.md)
- [codey-vs-opencode-agent-stability-2026-10-10.zh-CN.md](codey-vs-opencode-agent-stability-2026-10-10.zh-CN.md)
- [codey-vs-pi-agent-stability-2026-10-09.zh-CN.md](codey-vs-pi-agent-stability-2026-10-09.zh-CN.md)
- [codey-vs-pi-agent-stability-fixes-2026-10-09.zh-CN.md](codey-vs-pi-agent-stability-fixes-2026-10-09.zh-CN.md)
- [context-compaction-ab-2026-10-09.zh-CN.md](context-compaction-ab-2026-10-09.zh-CN.md)
- [counterexample-review-quality-12b-ab-2026-10-11.zh-CN.md](counterexample-review-quality-12b-ab-2026-10-11.zh-CN.md)
- [fact-only-review-quality-12b-ab-2026-10-11.zh-CN.md](fact-only-review-quality-12b-ab-2026-10-11.zh-CN.md)
- [named-probe-review-quality-12b-ab-2026-10-11.zh-CN.md](named-probe-review-quality-12b-ab-2026-10-11.zh-CN.md)
- [open-code-review-12b-self-review-ab-2026-10-11.zh-CN.md](open-code-review-12b-self-review-ab-2026-10-11.zh-CN.md)

关联的脱敏实测 JSON 位于 [reports/](reports/)。它们是证据，不是运行时配置。

## 文档维护规则

- 新增功能修改对应的现行说明；不要每次实现都再建一份永久阶段报告。
- 方案中尚未完成的目标与已经实现的行为分开写，不把绿色测试等同于全部方案完成。
- 当前测试数字集中在 TEST_REPORT；原始大日志放入忽略的产物目录。
- 删除文档时同步更新本索引及所有入站引用；被测试读取的文档需要保持其契约。
- 已移除的前端迁移阶段记录、旧 UI 实施账本、退役机制实验和测试用研究记录仍可从 Git 历史恢复。
