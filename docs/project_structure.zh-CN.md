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
  providers/    网页/本地适配器、回复 codec、会话、worker 与健康状态
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
HTTP / CLI → task submission → task_run + task_phases
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
| `codey/operations/task_state.py` | `TaskState` 与类型化提交资源包 |
| `codey/operations/task_run.py`、`task_phases/` | 运行资源生命周期、provider 接入、回调与终态结算 |
| `codey/operations/task_entry.py`、`task_session.py` | 入口策略与任务事实 |
| `codey/operations/task_loop.py` | 唯一生产模型工具循环，依赖分为 transport/execution/observation |
| `codey/toolchain/tool_spec.py` | 工具定义、schema 与按权限生成的本轮快照 |
| `codey/operations/kernel_protocol.py` | JSON/native 归一及当前快照校验 |
| `codey/operations/kernel_execution.py`、`task_execution.py` | 执行边界及领域适配器 |
| `codey/operations/completion_gate.py` | 最终完成证明组合；模型的 done 只是候选 |
| `codey/operations/project_completion_checks.py`、`research_completion_checks.py` | 项目、来源与严格 Research 检查提供者 |
| `codey/operations/kernel_session_recovery.py`、`kernel_receipts.py` | 恢复原策略/事实/结算结果，并验证收据身份 |
| `codey/providers/local_response_codec.py` | 本地响应信封与模型方言，在进入内核前归一 |
| `codey/agents/context_compaction.py`、`providers/local_openai.py` | 压缩/发送前精确配对原生历史；实际序列化请求的诊断观察点 |
| `codey/research/source_gateway.py`、`tools.py` | 显式来源/工具结果；取消及截止异常直接传播，不继续 fallback 获取 |
| `codey/app/context.py`、`event_bus.py`、`event_payloads.py` | 公共出口补全运行身份和模式、严格状态；总线负责重放，纯投影模块生成有界机器事件与收据 |
| `codey/app/headless_runner.py`、`cli.py`、`web/assets/sse.js` | 消费公共事件，负责 JSONL、CLI 文字与网页协调，不另行推断成功 |
| `tools/machine_contract_gate.py` | CI/本地必跑契约，缺失、失败或 skip 都不通过 |
| `tools/local_model_gate_recovery.py` | 发布门专用进程中断注入与独立校验；使用正式入口，不拥有另一个模型循环 |
| `codey/operations/research_iteration.py` | 函数 `run_research_iteration`，pipeline 对共同内核的适配入口 |

## 持久 runtime 与存储

### 审查职责

| 所有者 | 消费者 / 边界 |
| --- | --- |
| `reviews/core.py`、`findings.py` | 有界回复解析、可执行问题与公共审查事件元数据 |
| `reviews/input.py`、`identity.py` | 实际安全 prompt/范围、本地模型配置身份、有界文件与 Git 基准快照 |
| `reviews/persistence.py`、`reuse.py` | 单次校验读取、已完成 ledger 来源链、显式同会话/项目复用 |
| `app/review_service.py` | 单次准备、实际 Reviewer 选择、一次格式修复及保存；未知发送失败不切 Reviewer 重发 |
| `reviews/coordinator.py`、`operations/project_review_phase.py` | 当前快照有效的问题进入既有一次 Writer repair |
| `operations/review_flow.py`、`app/headless_runner.py` | 通过已配置连接器只读审查；嵌入 headless project 可显式接入既有自动审查阶段，默认 CLI/headless project 不新增 Reviewer 调用 |
| `runs/ledger_projection.py`、`runs/details.py`、`app/api.py` | 有界展示及经过认证的 `/api/run_review` 结构化冷读 |
| `tools/local_model_gate_review.py` | 真请求、文件不变、产物/ledger/事件一致性；不评判模型找问题能力 |
| `tools/local_model_gate_project_review.py` | 同一次正式 project：编码/验证、隔离本地自动自审及既有可选修复；独立核验文件、测试及真实请求 |

审查修复仅在 Writer 成功结算且尚未记录最终证明时，以更新的 attempt 回到
`writer_running`，修复后结算再进入完成检查。不另建工具循环，也不把旧结算裁决
当作可执行新 effect 的授权。详见[自动本地自审验收](automatic-local-review-2026-10-04.zh-CN.md)。

模型身份描述配置的目标/模型/参数，不证明模型权重不可变。快照扫描和文件读取有明确
预算；读取失败或超预算不授予信任。这些边界不构成全库无 bug 的证明，也不能消除任意
并发外部写入造成的竞争。详见[本轮审计](review-audit-2026-10-04.zh-CN.md)。

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

手工 gate recorder 在本地 provider 请求边界观察实际字节。请求 hash 用于诊断变化，
不授予权限、不证明语义等价或任务完成。逻辑 exchange 与显式 `urlopen` 尝试分别计数，
后者包含现有 transport retry。

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
