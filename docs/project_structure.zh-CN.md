# 项目结构与职责地图

[English](project_structure.md)

本文描述当前源码。历史方案与实验报告只描述当时的提交，不作为当前 API 说明。

```text
codey/
  __init__.py   包版本的唯一来源
  __main__.py   python -m codey 入口
  app/          HTTP/桌面/CLI 入口、注册表、任务提交与资源生命周期
  automation/   本地自动化作业与调度
  task/         用户提交与任务类型数据
  policies/     任务授权、动作、网络与命令边界
  operations/   任务入口、共同内核、执行适配、完成与恢复接线
  agents/       项目请求/结果、提示、上下文、审查及审批辅助
  providers/    网页适配器、共享 API runtime、协议 codec、连接包与健康状态
  protocols/    共用 JSON/native 计划 codec 与消息 framing
  toolchain/    共同工具定义、schema 校验与项目工具实现
  completion/   完成契约、引擎、验证与编辑完整性
  research/     来源、证据、报告检查、pipeline 策略与研究记录
  runtime/      持久操作状态、事实、effect、写口与只读观察
  workspace/    项目路径、版本、地图、变更、事实与上下文
  storage/      原子 I/O、文件锁、对话与受管输出
  knowledge/    本地笔记、图与知识变更
  ghost/        有界本地经历、记忆、工作队列与控制面
  reviews/      审查契约、安全输入、身份、产物、显式复用与协调
  repairs/      provider 修复作业与监督
  runs/         运行 ledger、trace、检查点、详情与收据
  utils/        共用文本、引用与扫描小工具
  web/          随包发布的本地 UI、JavaScript/CSS
tests/          行为回归、架构锁、压力测试与本地/浏览器 E2E
  support/      仅测试使用的夹具与历史对照适配器
  manual/       实机实验与历史报告
tools/          发布门槛、诊断、parity 与开发工具
docs/           当前架构、发布说明与带日期的审查记录
```

## 顺着一次任务阅读

```text
HTTP / CLI → 共用任务服务 + 入口授权
           → task submission → task_run + task_phases
           → task_entry → TaskPolicy + TaskSession + KernelRunRequest
           → task_loop.run_task_kernel
                → TurnSnapshot → provider 回复 → 规范工具计划
                → 授权 → execute_turn → intent / 结果结算
                → 交付结果 → completion_gate → 最终结果
```

编程、Research、规划与获授权的混合任务共用模型工具内核。领域策略可以安排
后续动作，不另建模型工具循环。Research 按钮选择严格证据/报告要求；普通编程
查过网页不会因此被要求创建研究笔记。

| 所有者 | 职责 |
| --- | --- |
| `codey/app/operator_auth.py`、`web/assets/operator_auth.js` | 进程内 HTTP/SSE 操作者凭据及 UI 启动认证；独立于模型/任务授权 |
| `codey/runtime/core/models.py` | 不可变 `ToolResult`，必须携带精确布尔状态；展示文字不决定状态 |
| `codey/app/task_services.py`、`task/entry_auth.py` | 桌面/headless 的审查与顾问服务只有一个组合入口；HTTP/CLI 授权只有一套推导规则 |
| `codey/operations/task_state.py` | `TaskState` 与类型化提交资源包 |
| `codey/operations/task_run.py`、`task_phases/` | 运行资源生命周期、provider 接入、回调与终态结算 |
| `codey/operations/task_entry.py`、`task_session.py` | 入口策略与任务事实 |
| `codey/operations/task_loop.py` | 唯一生产模型工具循环，依赖分为 transport/execution/observation |
| `codey/operations/task_guidance.py`、`research/completion_guidance.py` | 任务组装入口选择领域拥有的完成指导；内核接收文本，不定义 Research 工作流 |
| `codey/operations/project_prompt_context.py`、`workspace/coding_context.py` | 经现有验证判定准备不可变编码事实，再无 I/O 地渲染 JSON/native 上下文 |
| `codey/operations/kernel_prompt.py`、`research/tool_contract.py` | 组合已提供的指导、上下文及协议说明；逻辑 note ID 用法由共用工具定义负责 |
| `codey/operations/kernel_preparation.py` | Auto/kernel 共用冻结快照与实际 prompt 的一次准备；已收到的 native 首轮复用原输入并绑定原 TaskSession |
| `codey/toolchain/tool_spec.py` | 工具定义、schema 与按权限生成的本轮快照 |
| `codey/operations/kernel_protocol.py` | JSON/native 归一及当前快照校验 |
| `codey/operations/auto_loop.py`、`task_entry.py::prepare_auto_provider` | 首调用记录原授权/effect，native 回复与冻结快照经 InitialNativeTurn 交给同一 kernel；保留原预算、延迟写锁 |
| `codey/operations/kernel_execution.py`、`task_execution.py` | 执行边界及领域适配器 |
| `codey/operations/completion_gate.py` | 最终完成证明组合；模型的 done 只是候选 |
| `codey/operations/project_completion_checks.py`、`research_completion_checks.py` | 项目、来源与严格 Research 检查提供者 |
| `codey/completion/behavioral_checks.py` | 有限原文准入、冻结行为定义与聚合完成检查；无模型调用或 I/O |
| `codey/operations/behavioral_verification.py`、`behavioral_probe_worker.py` | 授权后的有界 Python 函数观察、代码身份与受管结果；固定执行器不接收脚本或表达式 |
| `codey/operations/project_review_phase.py::validate_candidate` | 首次完成及修复后的共同候选验证入口，重新观察并重新审查当前补丁 |
| `codey/operations/explicit_execution_requirements.py` | 从原始要求与既有回执投影 once、先读后写及明确只读修复阻塞；无独立状态 |
| `codey/operations/kernel_session_recovery.py`、`kernel_receipts.py` | 恢复原策略/事实/结算结果，并验证收据身份 |
| `codey/providers/local_response_codec.py` | 本地响应信封与模型方言，在进入内核前归一 |
| `codey/providers/base.py` | 协议中立工具声明/结果与明确的回复结束状态 |
| `codey/providers/api_provider.py`、`api_transport.py` | 统一最终请求计数/裁剪/准入、锁、取消与候选提交；传输交付原始 usage 事件，不重发未知结果 |
| `codey/providers/api_codec.py` | 无状态 codec 契约及生成参数；连接工厂解析预算，Provider 构造时选定协议 |
| `codey/providers/api_chat.py`、`api_responses.py` | 各自拥有工具、结果、wire 历史、完整上下文单元、调用配对验证和回复解码；Responses 保留 reasoning items 并使用 call_id |
| `codey/providers/context_ledger.py` | 已接纳协议原文、哈希链来源与可替换视图的原子 head |
| `codey/providers/context_checkpoint.py`、`compaction.py` | 完整范围选择、可信回执输出缩减、来源验证和前后台上下文事务 |
| `codey/operations/tool_result_reader.py` | 当前任务已有回执只读、托管输出完整性与有界分页；不执行命令 |
| `codey/providers/api_connections.py`、`local_connection.py` | lazy 连接工厂与冻结 Local 配置；共享 runtime 不导入 Zen |
| `codey/providers/token_accounting.py`、`api_metering.py` | 独立的预算、上下文计数与服务端用量契约；计数端口和每请求 usage collector，无厂商字段解释 |
| `codey/providers/local_tokens.py`、`local_usage.py` | Local 自有 KoboldCpp 完整模板计数能力及两种协议的 usage 解释 |
| `codey/runs/trace.py`、`details.py` | 规范化请求记录、有界明细与完整已知量汇总；显示上次请求上下文和不完整用量，不依赖连接包 |
| `codey/providers/model_preferences.py` | 带 revision 的原子来源/模型偏好；目录观察不改变选择 |
| `codey/app/model_settings.py`、`provider_services.py` | 通用来源描述、显式发现、使用中模型保护与已选范围服务 |
| `codey/web/assets/models.js`、`model_settings.js` | 前端权威快照与共用暂存编辑器；可选连接提供数据，不增加专属 UI 分支 |
| `codey/web/assets/ui_state.js`、`composer.js`、`codey/storage/ui_state_store.py` | 草稿随聊天通过现有 UI 状态存储持久保存，校验 UTF-16 选区；发送只消费 revision 仍匹配的快照 |
| `codey/web/assets/conversation_ui.js`、`conversation_nav.js` | 明确阅读意图、有界缓存与阅读锚点、当前回答选区动作；引用编辑现有草稿 |
| `codey/web/assets/changes_drawer.js` | 唯一拥有 Changes 项目、显示数据、新鲜度和操作；局部刷新保留阅读现场，旧数据禁止恢复 |
| `codey/web/assets/settings.js` | 独立连接字段 baseline 与保存生命周期；模型偏好修改判断和来源行更新留在 model_settings.js |
| `codey/providers/zen/` | 动态免费目录、限定合作身份与访问观察；`usage.py` 独占 Zen 两种协议字段映射，`declarations.py` 拥有临时不可用 read/shell 网络声明，`connection.py` 拥有有界文本调用拒绝；不授予任务权限，删除生产包和注册时还需清理专用测试、gate 和文档 |
| `codey/runtime/core/api_selection.py`、`operation_payload.py` | 无秘密的冻结 API 选择，以及严格接纳/交付 payload 校验 |
| `codey/research/source_gateway.py`、`tools.py` | 显式来源/工具结果；取消及截止异常直接传播，不继续 fallback 获取 |
| `codey/app/context.py`、`event_bus.py`、`event_payloads.py` | 公共出口补全运行身份和模式、严格状态；总线负责重放，纯投影模块生成有界机器事件与收据 |
| `codey/app/headless_runner.py`、`cli.py`、`web/assets/sse.js` | 消费公共事件，负责 JSONL、CLI 文字与网页协调，不另行推断成功 |
| `tools/machine_contract_gate.py` | CI/本地必跑契约，缺失、失败或 skip 都不通过 |
| `tests/manual/behavioral_verification_local_holdout_ab.py` | 不同函数/文件及正确实现对照；复用原生 Codey/Pi 比较循环与独立评分 |
| `tools/local_model_gate_recovery.py` | 发布门专用进程中断注入与独立校验；使用正式入口，不拥有另一个模型循环 |
| `codey/operations/research_iteration.py` | 函数 `run_research_iteration`，pipeline 对共同内核的适配入口 |

## 持久 runtime 与存储

### 审查职责

| 所有者 | 消费者 / 边界 |
| --- | --- |
| `reviews/core.py`、`findings.py` | 有界回复解析、可执行问题与公共审查事件元数据 |
| `reviews/input.py`、`identity.py` | 实际安全 prompt/范围、API 模型配置身份、有界文件与 Git 基准快照 |
| `reviews/persistence.py`、`reuse.py` | 单次校验读取、已完成 ledger 来源链、显式同会话/项目复用 |
| `app/review_service.py` | 单次准备、实际 Reviewer 选择、一次格式修复及保存；未知发送失败不切 Reviewer 重发 |
| `reviews/coordinator.py`、`operations/project_review_phase.py` | 当前快照有效的问题进入既有一次 Writer repair |
| `operations/review_flow.py`、`app/headless_runner.py` | 共用审查服务：只读 review 与桌面/CLI/headless 默认自动 project review；测试门可显式固定 Reviewer 连接器 |
| `runs/ledger_projection.py`、`runs/details.py`、`app/api.py` | 有界展示及经过认证的 `/api/run_review` 结构化冷读 |
| `tools/local_model_gate_review.py` | 真请求、文件不变、产物/ledger/事件一致性；不评判模型找问题能力 |
| `tools/local_model_gate_project_review.py` | 同一次正式 project：编码/验证、隔离本地自动自审及既有可选修复；独立核验文件、测试及真实请求 |

审查修复仅在 Writer 成功结算且尚未记录最终证明时，以更新的 attempt 回到
`writer_running`，修复后结算再进入完成检查。不另建工具循环，也不把旧结算裁决
当作可执行新 effect 的授权。详见[自动本地自审验收](automatic-local-review-2026-10-04.zh-CN.md)。

模型身份描述配置的目标/模型/参数，不证明模型权重不可变。快照扫描和文件读取有明确
预算；读取失败或超预算不授予信任。这些边界不构成全库无 bug 的证明，也不能消除任意
并发外部写入造成的竞争。详见[本轮审计](review-audit-2026-10-04.zh-CN.md)。

API project Writer 在同一连接有可用独立模型时接纳另一个模型；独立只读 Review
使用用户选定模型本身。Reviewer 在发送前保存到正式操作日志。
独立 API Review 在考虑打开的网站前固定所选模型；project 的网页优先、require-web、
自审策略与最多一次修复仍生效。
普通文本访问观察是短期服务事实，不是永久能力；未知请求不会换模型重发。

共享 API runtime 不含 Zen 分支。Local 显式启用自己的文本工具帧解码器；Responses
在提交历史前拒绝明确未完成的 output item。详见[Zen 请求适配](zen_request_profile_2026-10-08.md)。

`runtime/core` 管状态/契约，`runtime/log` 管规范日志投影，`runtime/effects`
管 effect/结果交付，`runtime/write` 管获准变更，`runtime/observe` 管只读观察。
runtime 不依赖任务编排层。细节见 [runtime 架构](runtime_architecture.zh-CN.md)。

新 provider 窗口恢复时保留原任务提示并追加结算事实；同窗口原生结果仍按原 ID 交付。
任务事实从原日志/收据重建，Research 证据是领域投影，不竞争成为第二任务日志。
受管输出和恢复收据可能在本地保留来源正文/工具输出；审计摘要与模型窗口另行限长。

工具状态贯通结算与恢复，包括失败结果；收据状态必须与 settlement 一致。
Research fetch 适配器返回 `status`（`ok/error/skipped`），失败时还须提供 `detail`；
`ResearchToolOutput.ok` 保持显式，直到转换为共同 `ToolResult`。
每组原生工具历史要求：每个唯一 call ID 恰有一个配对结果。

接纳时把 `model_selection`、必要时的 `reviewer_selection` 写入 canonical
operation state，保存连接版本、模型、协议、能力和生成参数，不保存凭据。
冷启动沿用原选择，Settings 改动不覆盖旧任务；原连接不可用就阻塞恢复。
协议历史由各适配器在内存中拥有，没有可复用历史时，新窗口接收原任务要求和
已结算事实，不重复已结算写入。`final_delivery` 单独保存 success/failed/unknown，
交付失败不会擦掉已成立的完成证明或已执行效果。

手工 gate recorder 在本地 provider 请求边界观察实际字节。请求 hash 用于诊断变化，
不授予权限、不证明语义等价或任务完成。逻辑 exchange 与显式 `urlopen` 尝试分别计数，
后者只有单次有界 HTTP 尝试。HTTP 结果未知、明确输出截断后的有限续写和任务恢复
是三个独立机制，不共享一个含糊的 retry。

Ghost 工作队列与 affinity 已分别拆成 `*_model`、`*_sources`、`*_events` 和
Store 模块；它们是领域所有者，不是新的通用框架。continuity 与 Hebbian 保留
现有职责连贯的模块。

## 修改后去哪里验证

- [测试索引](../tests/README.md)：行为与归属回归。
- [发布门槛](release_gate.zh-CN.md)：静态、确定性、机器契约及实机要求。
- [内核不变量](kernel_invariants.zh-CN.md)：可执行检查与有限证明范围。
- [测试报告](../TEST_REPORT.md)：真实结果和环境限制。

旧编程/Research 循环与生产 `ResearchIteration` 类已删除；同名对照适配器只在
`tests/support/`。当前消费者使用函数适配器与正式所有者，历史夹具不构成生产兼容承诺。
