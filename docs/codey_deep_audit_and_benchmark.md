# Codey 源码审计与七个参考项目对比

本文保留原审计基线。后续实现、实机验证与尚未完成的七项目同题对照见 [后续机制验证](reports/followup-mechanisms-validation-20261122.md)。

审计基线为 Codey `1fb04490` 和文末列出的七个本地参考版本。依据是生产函数、调用链、现有回归和本地探针。产品范围是单 Agent 的编码、网页操作、深度研究，以及三者产生的知识和用户偏好。实现顺序、建议签名和验收场景见 [函数级改造指南](codey_function_level_upgrade_guide.md)。

## Codey 现状

Codey 已经能在一个任务内调用项目、网页和知识工具。可恢复的工具回执与版本化知识能回答两个重要问题：某个结论依据哪次观察，以及依据变更后哪些结论需要复核。近期最有价值的工作，是让这些机制贯穿一次真实任务，并在下次任务用得上。体验评估应以真实入口的混合场景为主，覆盖只读任务、明确授权的写入、浏览器动作不确定、验证失败和恢复。 [任务分派](../codey/operations/task_phases/dispatch.py#L244) · [工具结果](../codey/toolchain/runtime.py#L139) · [知识提交](../codey/knowledge/repository.py#L91)

| 主题 | 核对结果 | 应解决的问题 |
| --- | --- | --- |
| 编码、网页、研究衔接 | `run_task_mode()` 有工作流分支。`hybrid` 使用 `run_entry_kernel()`，已具备网页和知识授权。Research 在有项目和显式写入意图时有写入路径。浏览器直调的单动作限制与 CodeMode 编排分别存在。 | 以混合任务进入现有工具循环，使用同一次运行的来源、页面和代码回执收口。先测入口路由、权限与验收，按实际断点修改。 [入口](../codey/operations/task_entry.py#L570) · [权限](../codey/policies/task_policy.py#L251) · [直调](../codey/operations/kernel_protocol.py#L775) |
| Ghost | 终态候选走 `request_scope(project, session_id)`。同项目共享项目作用域，无项目才回落到会话。管理入口已有用户作用域。自动候选要求模型提供精确字符偏移。删除会话会清理依赖该会话来源的记忆。召回同时受文本相关度、反馈和作用域等过滤影响。 | 宿主定位原话，用户确认全局偏好时建立独立的确认记录。保留来源与删除语义，给稳定偏好单独的预算。 [采集](../codey/app/memory_setup.py#L116) · [作用域](../codey/workspace/context_admission.py#L137) · [候选](../codey/memory/candidates.py#L36) · [删除](../codey/ghost/projection.py#L182) · [召回](../codey/ghost/backend.py#L67) |
| API 上下文 | Provider 的 ContextLedger 可以是内存态。具备来源映射和版本条件时，`commit_context()` 会先写 durable `context_edit`。冷传输在部分路径与连接变化同判，目标模型输入预算过小时，durable 投影会裁历史并标出省略的条目。 | 分清冷恢复、真实切换和预算滚动。用历史回放验证已保存的任务事实、完整工具调用对和目标协议兼容性。 [会话计划](../codey/operations/conversation_plan.py#L135) · [持久化压缩](../codey/providers/api_context.py#L198) · [上下文投影](../codey/durable/context.py#L25) |
| 知识沉淀 | Project 的实施笔记走 `write_note()`。`search_notes()` 和版本化 `search()` 使用不同索引。临时项目探针已确认跨会话旧笔记能被前者找到，后者漏掉。Recall 已有列加权 BM25 和有界图遍历。 | 让项目和网页已验证事实进入同一版本化知识入口，再测召回缺口。 [工程笔记](../codey/operations/project_completion_flow.py#L346) · [知识查询](../codey/knowledge/store.py#L66) · [BM25](../codey/knowledge/recall.py#L54) |
| 浏览器 | 一次常规采集最多 80 个控件。控件列表截断后，可读名称绑定要求完整性，原始引用、局部观察和 diff 已有基础。同文档查询缺少结果区域时可能一直无法获得确认，文档变化能中断旧等待。快照有逐节点串行开销，耗时和往返次数取决于页面。 | 将范围选择暴露到原有快照工具，先保证可操作性。为待确认查询提供可诊断的终止状态。 [快照](../codey/automation/browser_snapshot.py#L114) · [引用完整性](../codey/operations/browser_references.py#L61) · [查询](../codey/automation/browser_runtime.py#L850) · [等待](../codey/operations/browser_followups.py#L219) |
| 编码验收 | 本地 5 万字符文件的完整读取回执受限，精确替换可用。单验证候选必须覆盖全部变更路径。Python 和 TypeScript 分别可选到检查，混合改动的探针返回空。已识别的定向测试可以按现有候选匹配规则处理。 | 优先实现多检查组合和可核验的文件版本。定向测试须有可追溯的覆盖依据。 [整文件基线](../codey/operations/edit_base.py#L15) · [验证选择](../codey/completion/verification_policy.py#L788) |
| 深度研究 | `ResearchPipeline.run()` 一次调用内部迭代，外层补跑已停用。内部工具循环可多次获取来源、提交报告和修订，已有冻结证据与引文检查。来源独立性的现有分组主要依赖域名。 | 在冻结报告前用缺口或反证驱动有界补证。把同源转载和逐字引语验证接到现有 evidence/passage。 [研究外层](../codey/research/pipeline.py#L100) · [提交入口](../codey/operations/task_execution.py#L920) · [来源分组](../codey/research/ledger.py#L653) |
| 网页数据边界 | 来源渲染和网络策略承担不同边界。现有网络策略包含代理兼容行为，浏览器、连接器实际建立连接的路径也需要一起审查。 | 验证来源内容与工具指令的隔离、重定向和最终连接约束。严重程度以实际生产调用链和防御回归判定。 [来源渲染](../codey/research/source_rendering.py#L18) · [网络决策](../codey/policies/network.py#L66) · [来源入口](../codey/research/source_gateway.py#L140) |

## 七个参考版本及可迁移机制

| 参考版本 | 生产源码做了什么 | 对 Codey 的迁移价值 |
| --- | --- | --- |
| Codex `970111f6cb` | 记忆分别执行提取与归并，Patch 支持逐级匹配并报告已应用的增量。 [提取](https://github.com/openai/codex/blob/970111f6cb/codex-rs/memories/write/src/phase1.rs#L55) · [归并](https://github.com/openai/codex/blob/970111f6cb/codex-rs/memories/write/src/phase2.rs#L50) · [匹配](https://github.com/openai/codex/blob/970111f6cb/codex-rs/apply-patch/src/seek_sequence.rs#L12) | 先提取带来源的待确认偏好，再通过既有维护批次归并。Patch 记录部分写入的范围，方便恢复。 |
| OpenCode `3ba597f776` | 上下文接近门槛且满足最小规模等条件时预压缩。影子 Git 提供比较和恢复。编辑插件的 LSP 写后通知仍是 TODO。 [压缩](https://github.com/anomalyco/opencode/blob/3ba597f776/packages/core/src/session/compaction.ts#L332) · [快照](https://github.com/anomalyco/opencode/blob/3ba597f776/packages/core/src/snapshot.ts#L128) · [写后通知](https://github.com/anomalyco/opencode/blob/3ba597f776/packages/core/src/tool/plugin/edit.ts#L258) | 优先实测哪个预算阶段丢关键信息，再接已有压缩调度。利用现有变更追踪验证回滚缺口。 |
| DeepSeek Harness `8bd073392e` | 多文件预检模拟顺序变更。LSP 查询使用短生命周期文档并支持取消。 [预检](https://github.com/deepseek-ai/deepseek-harness/blob/8bd073392e/packages/fs/tool-fs/src/patch/preflight.ts#L146) · [LSP](https://github.com/deepseek-ai/deepseek-harness/blob/8bd073392e/packages/lsp/lsp-stdio/src/instance.ts#L150) | 将预检与应用分开，写前再比对文件版本。只读语义查询用于定位和诊断，执行结果继续落回 Codey 回执。 |
| Pi `ed4f48840` | 模糊匹配后按原文回写未改行。跨模型转换成对处理调用 ID、思考签名和图像能力。 [原文保留](https://github.com/earendil-works/pi/blob/ed4f48840/packages/coding-agent/src/core/tools/edit-diff.ts#L132) · [消息转换](https://github.com/earendil-works/pi/blob/ed4f48840/packages/ai/src/api/transform-messages.ts#L91) | 定位在规范化视图，写回使用原文范围。协议转换在 Provider 重建视图时执行，保留原始回执和内部 ID。 |
| Agent-Desktop `b43ac4f6` | 截断区提供下钻锚点。动作回执区分是否投递并给出重试语义，几何探测给出可点击点。 [下钻](https://github.com/vercel-labs/agent-desktop/blob/b43ac4f6/crates/core/src/refs/ref_skeleton_anchors.rs#L12) · [投递](https://github.com/vercel-labs/agent-desktop/blob/b43ac4f6/crates/core/src/actions/delivery_semantics.rs#L6) · [可操作性](https://github.com/vercel-labs/agent-desktop/blob/b43ac4f6/crates/core/src/actionability/evaluate.rs#L160) | 优先解决大页面范围选择和动作不确定时的可观测恢复，再做实际落点与稳定性采样。 |
| Agent-Browser `4b0e9f0` | AX 树结合批量补充控件，隐藏表单控件可提升到快照。动作层输出快照差量，另有 Flow 录制及发送模块。 [快照](https://github.com/vercel-labs/agent-browser/blob/4b0e9f0/cli/src/native/snapshot.rs#L430) · [差量](https://github.com/vercel-labs/agent-browser/blob/4b0e9f0/cli/src/native/actions.rs#L6747) · [Flow](https://github.com/vercel-labs/agent-browser/tree/4b0e9f0/cli/src/native/flow) | 批量采集兼容现有文档与节点身份。Flow 的使用价值单独用重复、明确授权的只读查询评估。 |
| HyperResearch `aece65f` | 立场矩阵由 CLI 消费。来源独立性结合规范 URL、通稿和近重复内容。学术全文可补救，引文检查分别做局部支持分流和全 vault 引语 lint。 [立场矩阵](https://github.com/jordan-gibbs/hyperresearch/blob/aece65f/src/hyperresearch/core/claims.py#L342) · [独立性](https://github.com/jordan-gibbs/hyperresearch/blob/aece65f/src/hyperresearch/core/independence.py#L61) · [全文](https://github.com/jordan-gibbs/hyperresearch/blob/aece65f/src/hyperresearch/core/oa.py#L835) · [引文](https://github.com/jordan-gibbs/hyperresearch/blob/aece65f/src/hyperresearch/core/citecheck.py#L257) | 用带来源和条件的立场分组驱动工具循环补证。在 Codey 中按实际引用 passage 验证引语，数字的单位、分母、时间和实体一起对齐。 |

## 验证基线与取舍

- 局部探针确认了四个行为。项目跨会话同作用域，无项目时会话隔离。5 万字符文件仍能精确替换。工程笔记命中旧索引而漏出版本化召回。Python 与 TypeScript 的独立检查无法由单候选合并。
- 上一轮在同一 Codey 基线运行的三批相关回归，合计 259 通过、17 失败。失败包含浏览器入口测试替身参数与生产函数签名不同、Research 工具面断言差异和研究报告修订的读取次数分歧。复测性能或改变完成合同前，应先确认这些用例分别在检验哪个生产行为。 [浏览器候选](../tests/test_browser_entry_candidates.py#L82) · [工具面](../tests/test_browser_entry_surface.py#L18) · [报告修订](../tests/test_research_report_tail.py#L89)
- 跨供应商实机通过率、缓存收益、浏览器耗时倍数、七项目胜率属于后续测量。固定相同模型或记录模型差异，固定权限、输入、最大轮数、预算和完成判定。同时记录失败分类，避免把工具能力差异与模型选工具差异混算。 [同题评测方案](codey_function_level_upgrade_guide.md#p3-同题评测与后续投入)
