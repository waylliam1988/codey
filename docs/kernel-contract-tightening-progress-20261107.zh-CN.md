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
- 第 6 批：已将 provider 持久化调用解码边界从 `Any` 容器收紧为 `object` 容器，保留原有六种方法、参数顺序、摘要和恢复路径；完整判别调用对象待第 7 批完成。
- 第 7 至第 8 批：未开始。
- 最近验证：上述测试外，provider capability/native boundary 测试 7 passed；provider receipt/native delivery 测试 32 passed、3 skipped；相关 Ruff 通过。曾发现并修正窗口引用过窄导致的 2 个回归。
- 已知外部问题：本地 mypy 导入用户已有的附件改动时，在 `codey/task/attachments.py:45` 和 `codey/durable/conversation_ops.py:492` 报错；未修改这些文件，待后续与用户改动一起单独处理。

