# Kernel 契约收敛进度

本文件记录 kernel 核心调用链的类型收敛工作。目标是让请求、执行器、完成上下文、provider 调用和持久化恢复具有可读且可验证的契约，并阻止后续通过 `Any` 或动态透传重新放宽。

## 约束

- 保留 `transport / execution / observation` 三组依赖及现有 durable 执行、提交、恢复路径。
- 保留当前持久化格式、摘要、effect 归属、窗口身份和回执语义。
- 每批只运行相关测试、Ruff 和类型检查，不运行全量 pytest。
- 测试失败先判断是生产代码问题还是测试假设落后，再修复根因或更新测试。
- 工作区已有与输入附件相关的改动，不纳入本次提交。

## 字段清单（初版）

| 组 | 字段 | 当前事实 | 目标 |
|---|---|---|---|
| transport | `provider` | provider wrapper 和多种协议的动态组合 | 第五阶段由 provider 适配协议承载 |
| transport | `run_id`, `effect_scope` | 字符串身份 | 保持 `str` |
| transport | `provider_id`, `user_task` | 入口可能传入任意对象 | 入口规范化为 `str` |
| transport | `stop_flag` | 只消费 `is_set()` | `StopSignal \| None` |
| transport | `window_ref` | 协议窗口身份 | `ProtocolWindowRef \| None` |
| execution | `executors` | executor 实际必须返回 `ToolResult` | `Mapping[str, ToolExecutor] \| None` |
| execution | `project_path` | 项目工具使用路径对象或路径兼容值 | 入口统一为 `Path \| None` |
| execution | `tool_fns` | 使用 `AgentToolFns` 方法 | `AgentToolFns \| None` |
| execution | `workspace_revision_store` | 使用 `WorkspaceRevisionStore` | 明确类型 |
| execution | `workspace_ignored_paths` | 作为路径序列消费 | `tuple[str, ...]` |
| observation | `on_event` | 接收 `RunEvent` | `Callable[[RunEvent], None] \| None` |
| observation | `on_shell_request` | 接收 `ShellApprovalRequest` | 明确回调类型 |
| observation | `completion_context` | 字典或属性对象，字段由消费者隐式读取 | 第三阶段统一具名上下文 |
| result | `delivery` | 校验只允许四个字符串 | `DeliveryStatus` |

## 分批计划

1. 字段清单、基础依赖、executor 和 kernel 内部返回类型。
2. snapshot、冻结工具定义和研究资源能力。
3. 统一完成上下文，移除核心双表示方式。
4. 明确 sink 的准入资源和必需性。
5. provider 能力协议、wrapper 显式接口和 attempt gate。
6. 类型化 provider 持久化调用及兼容 codec。
7. 结果、决策和领域 payload 的类型化边界。
8. 精度门禁、静态负例、文档和最终分批验证。

## 状态

- 第 1 批：已完成基础契约定义，并把 `KernelContext` 的 view、state、decision、plan 和执行动作签名收紧。
- 第 2 批：已完成 `TurnSnapshot` 的 policy、tool spec、custom executor 和 provider window 类型收紧；研究资源能力仍待后续批次处理。
- 第 3 批：已建立 `CompletionContext` 及单次兼容转换边界；完成检查支持该具名上下文，旧 mapping/属性对象仍在边界转换。
- 第 4 批：已将生产入口的 sink 放入 execution 依赖；保留 observation sink 兼容字段，并修复重启时替换 sink 的确定性回归。
- 第 5 批：已建立 provider capability source 协议并明确窗口引用是 opaque identity；未改变现有 provider 方法分派，待下一批处理。
- 第 6 批：已将 provider 持久化调用解码为带 `ProviderMethod` 判别字段的 `ProviderInvocation`，保留原有六种方法、参数顺序、摘要和恢复路径。
- 第 7 批：已为 kernel result、question 和 decision result 建立具名 TypedDict/字面量边界；`result_from_payload` 已改为从 `object` 入口验证；provider invocation 通过定向测试和 mypy。
- 第 8 批：已加入 kernel 类型精度基线、pytest machine contract 和独立 gate。当前基线保留迁移债务，但禁止目标模块新增显式 `Any`。
- 收尾修复：`kernel_driver.py` 在 `--follow-imports=skip` 下从局部导入和跨模块方法调用得到 `Any`，导致两个 `no-any-return` 诊断；已在消费点用目标领域类型收窄为 `KernelResult` / `KernelResult | None`，没有扩大动态接口。
- 三套检查的补强：Pyrefly 暴露了动作状态和 staged/base 研究资源联合类型的未收窄路径；已在动作入口验证运行状态和已准入 sink，在研究 staging、搜索 provider、change tracker 和 provider 回复适配边界完成显式收窄。兼容 provider 的运行时返回行为保留，核心消费类型明确为 `AssistantTurn | str`。
- 最近验证：上述测试外，provider capability/native boundary 测试 7 passed；provider receipt/native delivery 测试 32 passed、3 skipped；结果/决策/恢复测试 43 passed；kernel contracts 与 question 的定向 mypy（follow-imports=skip）通过；类型精度 gate 与 machine contract 相关测试 4 passed；相关 Ruff 通过；kernel/provider durable 测试 54 passed；native/completion 测试 26 passed；research contract 测试 106 passed；machine contract `1441 passed, 14 subtests passed`。
- 一次错误指定了不存在的 `tests/test_research_tools.py` 和 `tests/test_research_tool_contract.py`，没有执行任何测试；随后按仓库实际路径重跑 research contract 选择集并通过，属于测试命令路径问题，不是生产代码问题。
- 最新类型验证：Pyrefly `0 errors`、mypy（四个核心模块，`--follow-imports=skip`）通过、ty 通过、相关 Ruff 通过；provider normalization 测试 51 passed；research staging/lifecycle 测试 46 passed；kernel restart/parity 测试 44 passed。
- 精度基线验证：provider_session 的显式 `Any` 从 34 降至 32，已作为有意收窄更新基线；precision gate、其 3 个测试均通过。
- 静态负例：新增真实导入核心 `ToolExecutor` / `DeliveryStatus` 的错误样例，分别由 mypy、Pyrefly 和 ty 拒绝，并要求诊断指向样例本身；machine contract gate 更新后为 `1444 passed, 14 subtests passed`。
- 后续第一批：收紧 `kernel_execution.execute_turn`、delegate、settlement 回调和 `_execute_slots` 的具名执行上下文；`linked_scope` 同步接受最小取消信号协议。`kernel_execution.py` 的显式 `Any` 从 62 降至 8，保留运行时 `ToolResult` 校验和 settlement 顺序。
- 后续第二批：收紧 `kernel_actions.py` 的执行调用、审批回调、attempt gate、completion evidence 和联合返回值；兼容 provider 的能力探测仍限定在边界函数。`kernel_actions.py` 的显式 `Any` 从 42 降至 1。
- 后续第三批：收紧 `kernel_protocol.py` 的 session view、policy、冻结工具规格和 native provider call；`normalize_turn()` 现在只接受结构化 provider 回复和明确的 snapshot/policy 契约。同步把 `KernelContext` 的 sink、durable、window 和状态消费收窄，并让 `KernelResult.to_payload()` 返回具名 `KernelResultPayload`。本批没有扩大动态边界；`kernel_protocol.py` 显式 `Any` 从 31 降至 17。
- 第三批验证：protocol/context/action/execution 定向 mypy、Pyrefly、ty 和 Ruff 通过；类型精度 gate 通过；native termination、durable decision/control 和 shell approval 定向测试 `32 passed`。
- 收尾静态门禁：新增 provider call 结构的三工具静态负例，并增加精度 gate 对新增动态逃生的失败测试；静态契约与精度测试 `8 passed`。固定 machine contract 首轮为 `1447 passed, 14 subtests passed, 1 failed`；失败暴露了执行器在失败的研究采集上仍提交 provider-owned ledger 的确定性问题，已改为只提交成功采集，相关研究投影测试 `7 passed`。

## 当前边界

本次 8 批已完成第一轮契约收敛和防退化门禁。后续补强已将 provider wrapper 的六种 kernel 方法改为显式方法，并把 `ResearchTools` 的主要资源字段收紧为真实/分阶段实现联合类型；兼容边界仍保留在 wrapper 的第三方属性透传和研究开放 JSON 字段。精度 gate 已覆盖这两个模块，禁止新增显式 `Any`。
- 已知外部问题：本地 mypy 导入用户已有的附件改动时，在 `codey/task/attachments.py:45` 和 `codey/durable/conversation_ops.py:492` 报错；未修改这些文件，待后续与用户改动一起单独处理。

