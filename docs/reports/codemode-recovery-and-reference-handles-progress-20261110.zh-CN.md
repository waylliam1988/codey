# CodeMode 恢复与引用句柄实施进度

## 目标

保持一份 `ToolSpec.output_schema`、一个 `NodeWorkerRuntime`、一个 kernel receipt/durable ledger 和一个生产浏览器 source provider。模型负责工具选择与语义判断；程序负责结果解包、身份绑定、回执恢复、句柄解析和浏览器会话生命周期。

## 批次状态

| 批次 | 内容 | 状态 |
| --- | --- | --- |
| 1 | `unwrap` 合同测试 | 已完成 |
| 2 | schema 驱动 `unwrap` runtime | 已完成 |
| 3 | receipt-only repair 测试 | 已完成 |
| 4 | repair token 与精确回放 | 已完成 |
| 5 | receipt/source/evidence 短句柄测试 | 已完成 |
| 6 | 句柄 resolver | 已完成 |
| 7 | 已有浏览器会话与知乎 403 | 已完成 |

## 约束

- 不新增第二套 CodeMode runtime、receipt ledger、正文缓存或浏览器 provider。
- 不引入新的 `Any`；修改过的生产文件要同时减少动态类型。
- 每批先写确定性测试，再修改生产代码；只运行与改动直接相关的测试和 gate。
- CodeMode repair 只能读取已经 settled 的原始 receipt，不能重新调用 `execute_turn`。
- `r1` 等现有研究句柄不能与 receipt 句柄混用；最终需要有类型前缀的命名空间。

## 最终架构边界

- `ToolSpec.output_schema` 是唯一工具结果类型来源；catalog、TypeScript 声明和示例都从它生成。
- `NodeWorkerRuntime` 是唯一 CodeMode 执行 runtime；`unwrap` 只解包已有的 `Result<T>`，失败仍抛错。
- kernel effect/receipt durable ledger 是唯一事实账本；repair 只保存 token、工具名、参数摘要和原始 `result_ref`，不复制正文。
- repair 通过 `repair_of` 精确匹配原 session/run、child sequence、能力摘要和 settled receipt；参数或工具变化、未确定结果、能力变化都会 fail closed。省略 `repair_of` 就是 fresh execution。
- `rc1`、`r1`、`s1@version`、`e1@version` 是已有 task/ledger 投影的展示别名；resolver 负责权限、任务范围、来源版本和证据正文绑定，不维护第二张映射表。
- 知乎等受会话保护的页面只通过用户明确提供的既有 Edge/CDP 会话读取。全新会话的 403 是站点会话/风控事实，不能通过复制 cookie、伪造请求头或无限重试解决。

## 第一批记录

- 测试文件：`tests/test_codemode_unwrap_contract.py`、`tests/test_codemode_catalog_result_guidance.py`
- 生产修改：`guest_runner.js` 提供 `unwrap`；catalog 生成 `ToolResult<T>`、`Result<T>` 和 `unwrap` 声明；CodeMode 示例改用 `unwrap`；schema 类型渲染移除 `Any`
- 测试结果：相关 CodeMode/runtime/kernel 测试 `93 passed`
- 静态检查：`mypy`（修改的 schema/catalog）通过，`ruff` 通过，Node 语法检查通过
- KoboldCpp gate：已确认 `http://127.0.0.1:5001/v1` 可用；专用模型行为 gate 待在 repair 批次完成后统一接入
- commit：`7d10270c feat: add schema-driven unwrap runtime`

## 运行记录

每批完成后记录相关测试命令、失败根因、修复、ruff/type 检查、KoboldCpp 实机结果和 commit。测试失败必须区分测试合同过时与生产实现错误，不以迁就旧测试为默认修复方式。

## 第二批记录

- `guest_runner.js` 提供 `unwrap`，catalog 使用 `ToolSpec.output_schema` 生成 `Result<T>` 声明和示例
- 相关 CodeMode/runtime/kernel 测试已通过；没有增加第二套 runtime 或工具类型表
- commit：`7d10270c feat: add schema-driven unwrap runtime`

## 第三批记录

- 测试文件：`tests/test_codemode_repair_replay.py`
- 红测试结果：`4 failed, 2 passed`；失败对应 `repair_of` 尚未进入协议、失败父回执尚未生成 token、repair 尚未走 receipt-only host
- 测试使用真实 `build_execute_turn_executor`、`execute_turn` 和 `ResearchTools`；source provider 是确定性的脚本源，只用于控制版本和调用次数
- 生产修改：`repair_of` token 解析、原始 settled child receipt 精确回放、能力指纹绑定、一次性 token 使用保护；repair 路径不调用 `execute_turn`
- catalog 校验只绑定实际回放工具的完整能力描述，避免打开来源后新增 `reopen_source` 等无关工具造成误拒绝；工具撤权、参数或输出 schema 改变仍 fail closed
- 测试结果：`tests/test_codemode_repair_replay.py` 7 passed；与 CodeMode 相关回归共 76 passed
- 静态检查：ruff、mypy、`git diff --check` 通过
- KoboldCpp gate：待专用 gate 脚本完成后执行
- commit：`c9c35a73 feat: replay settled CodeMode child receipts`

## 第四批记录

- 生产修复：kernel 参数规范化保留 `repair_of`；此前真实 task kernel 会丢弃该字段，使 repair 请求退化成 fresh execution 并重新获取来源。
- 新增 durable restart 测试：第一次失败父 CodeMode 先通过现有 provider delivery 路径合法 settle，关闭并重开 SQLite durable store，修改源文件为 V2 后以 `repair_of` 修复，结果仍为 V1 且没有第二次获取。
- 测试结果：`tests/test_codemode_repair_replay.py` 10 passed；CodeMode/runtime/catalog 回归 64 passed。
- 专用 gate：脚本模式和 KoboldCpp 均完成，`source_fetches=1`、`codemode_open_url_children=1`、`repair_replayed=true`。
- 静态检查：相关 Python 文件 ruff 通过；修改生产模块的 mypy strict 检查通过。
- 追加确定性修复：repair token 的摘要和 metadata 显式绑定 `session_id`；跨 session 复制原始内存回执后提交同一 token 会被拒绝。真实运行中若 sink 已提供 session ID，kernel 自动使用该身份；不新增回执存储。
- commits：`6d06d9bc fix: preserve CodeMode repair token during normalization`、`37909022 fix: bind CodeMode repairs to their session`。

## 第五至第六批记录

- 测试文件：`tests/test_receipt_handle_resolution.py`。
- `ReferenceHandleResolver` 只从现有 `_memory_results`、`ResearchLedger` 和 task source IDs 派生句柄；`rc`、`r`、`s`、`e` 命名空间互相拒绝，来源和证据句柄必须匹配正文版本，权限撤销或任务外引用 fail closed。
- `read_tool_result` 和 prompt surface 接受短 receipt 句柄，内部仍解析到原始 durable receipt；没有第二个 receipt store，也没有正文复制。
- 测试结果：11 项句柄/浏览器生命周期测试通过。
- commits：`a2d45228 test: define receipt and source handle resolution`、`a97f8770 feat: resolve typed receipt and source handles`。

## 第七批记录

- `BrowserSessionConfig(require_existing=True)` 明确要求使用已有 CDP/Edge 会话，`open_if_missing=False`，允许在用户浏览器 context 创建 task page，并在任务结束时只关闭借用的 task page。
- 测试覆盖已有会话接入、借用页面生命周期、浏览器重启/页面替换和跨 context 隔离；没有引入 cookie 复制或 HTTP 绕过。
- 知乎全新会话仍返回 403 属于站点访问策略；可行路径是使用用户已登录、可见的 Codey Edge 会话并保持其 context/page 身份，若会话失效则要求用户重新观察或重新连接。
- commits：`d46a7149 test: define existing browser session acquisition`、`74eeba66 feat: support explicit existing browser sessions`。

## 类型边界收敛记录

- 对本批修改过的生产文件做了显式 `Any` 审计；`codey/codemode/kernel.py`、`codey/codemode/protocol.py`、`codey/toolchain/tool_spec.py` 已移除全部 `Any`。
- `kernel` 现在使用现有的 `TaskSession`、`TurnSnapshot`、`ToolExecutor`、`KernelEffectSink`、`ManagedOutputStore` 和 durable session 类型；CodeMode replay 只通过只读 Protocol 接入，不新增运行时或账本。
- `tool_spec` 的策略能力使用 `ToolPolicy.allows` Protocol，自定义执行器使用现有 `ToolCall -> ToolResult` 合同；schema 动态值统一为明确的 `object`/`Mapping[str, object]` 边界。
- 旧测试对废弃的 `{ok:true, value}` 提示文本的断言已更新为当前 `Result<T>` 合同；没有恢复旧 prompt。
- 验证：直接相关 CodeMode、回放、句柄、浏览器会话测试 `50 passed`；工具注册表、快照和合同回归 `36 passed`；上述三个生产文件 Ruff 与严格 Mypy 通过。
