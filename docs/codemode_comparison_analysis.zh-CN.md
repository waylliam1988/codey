# CodeMode 对比分析（opencode、pi、Codey）

> 状态：仅分析，未修改 Codey 生产代码。  
> 目的：确认 `reference-projects/opencode` 与 `reference-projects/pi` 是否实现了 CodeMode，比较两者的共同点和差异，并评估 Codey 的借鉴路径。

## 1. 当前结论

`opencode` 和 `pi` 都实现了 CodeMode，而且都采用“模型只发出一次顶层脚本调用，脚本在受限运行时内编排多个工具，模型只收到最终输出”的路径。

两者解决的是同一个问题，但重点不同：

- `opencode` 的核心优势是语言和工具运行时的精细约束：自研 JavaScript 子集解释器、输入/输出 schema 校验、工具目录预算、内置搜索、执行诊断和 Effect 生命周期。
- `pi` 的核心优势是可部署的沙箱基础设施：QuickJS/WASM、Node/Bun worker、Cloudflare 远程 Durable Object、内存和中断预算、跨执行 store，以及可由宿主控制的确定性运行和重放。
- Codey 当前已有“批量工具协议”和 durable 工具效果账本，但还没有 CodeMode 脚本沙箱。当前 `read_files` / `parallel` 能把多个读请求压缩进一个模型回合，却不能等同于 CodeMode；而且实际 `_execute_slots()` 仍按 `for` 循环串行执行。

因此，Codey 可以同时吸收两边的优点，但不应直接把 CodeMode 视为普通工具执行器的旁路。CodeMode 的嵌套调用必须继续经过 Codey 现有的策略、效果意图、结算、恢复、工作区 provenance 和 provider 交付边界。

## 2. opencode：确实有 CodeMode

### 2.1 入口和执行模型

`reference-projects/opencode/packages/core/src/codemode/tool.ts` 是 opencode 的 Agent-facing 入口：

- `description` 明确说明 `execute` 在受限 Code Mode runtime 中执行 JavaScript，工具和 HTTP 请求由脚本编排；禁止 import、直接文件系统访问和 timers。
- `create()` 接受 `{ code }`，建立一次运行的调用计数、文件收集、调用状态和进度记录。
- 每个脚本内的工具调用通过 `runtime(...).execute(code)` 进入 `@opencode/codemode` 运行时。
- `progressHooks()` 把嵌套调用的 running/completed/error 状态投影给 TUI；脚本最终只返回一个结构化输出以及调用摘要和文件内容。

证据：`packages/core/src/codemode/tool.ts:62-107,143-239`。

### 2.2 自研受限解释器

`reference-projects/opencode/packages/codemode/README.md` 明确写明它是“lightweight, pure interpreter”，只实现支持的 JavaScript-like 语言特性，不尝试把任意 JavaScript 放进普通 sandbox。程序不能直接访问网络、文件系统、进程或应用 API；外部能力只能通过宿主注入的 tools 和 extensions。

运行时使用 `acorn` 解析，再由 `interpreter/execute.ts` 和 `interpreter/interpreter.ts` 执行。异步工具调用由自有 promise 管理器处理；未 await 的后台调用在程序结束时会被中断，并产生 warning。

这是一种语言级 capability confinement：攻击面小、行为可诊断，但实现成本高，且只能支持已经实现的语法和内置对象。

### 2.3 工具目录、搜索和 token 预算

`packages/core/src/codemode/catalog.ts` 和 `instructions.ts` 提供了完整的模型目录层：

- 工具按 namespace 组织，生成精确的 `tools.foo.bar(...)` 签名。
- 目录有 inline token budget；即使工具列表很大，也保留每个 namespace 的数量和摘要。
- pinned 工具可越过普通预算保留。
- 目录变化时发送语义增量，必要时发送完整替换，避免每次都重发整个目录。

`packages/codemode/src/tool-runtime.ts` 还提供内置同步 `search`：支持精确路径、namespace、排序、分页和描述匹配。目录过大时，模型先通过 `search()` 找到精确 path，再调用工具。

证据：`packages/core/src/codemode/catalog.ts:1-180`、`packages/core/src/codemode/instructions.ts:1-130`、`packages/codemode/src/tool-runtime.ts:190-314,340-454`。

### 2.4 运行限制和数据边界

opencode 的 `ExecutionLimits` 有三类限制：`timeoutMs`、`maxToolCalls`、`maxOutputBytes`。输入和输出可以使用 Effect Schema 做实际 decode/validate/safe copy；只有 JSON Schema 的工具只用于生成模型可见签名。

工具运行时会记录 `UnknownTool`、`InvalidToolInput`、`InvalidToolOutput`、`ToolCallLimitExceeded`、`TimeoutExceeded`、`ToolFailure` 等稳定诊断类别。工具调用的并发不由运行时强制限制，脚本可用 `Promise.allSettled` 并发执行相互独立的调用。

证据：`packages/codemode/src/codemode.ts:22-40,128-163`、`packages/codemode/src/interpreter/execute.ts:12-103`、`packages/codemode/src/tool-runtime.ts:384-454`。

### 2.5 HTTP extension

opencode 的 core 默认把 `fetch` 作为 CodeMode extension 注入，限制 `file:` URL，并有 30 秒超时；它不是脚本的直接网络权限，而是宿主明确选择暴露的 capability。

证据：`packages/core/src/codemode/web.ts:1-58`、`packages/core/src/codemode/tool.ts:62-68,239`。

## 3. pi：也有 CodeMode，而且更像独立沙箱产品

### 3.1 独立 `@earendil-works/pi-codemode` 包

`reference-projects/pi/packages/codemode/README.md:1-14` 明确说明：模型编写 JavaScript，代码在 QuickJS/WASM VM 中运行，唯一能力是调用注入的 tools；嵌套调用不会进入 LLM context，只有脚本 output 和 return value 会返回给模型。

脚本可使用 `tools`、`ALL_TOOLS`、`text`、`image`、`exit`、`store`、`load`，支持顶层 `await` 和 `return`。默认没有 Node、文件系统、网络、timers、process、require、modules 或 WebAssembly。

### 3.2 三种部署方式

pi 把“脚本运行位置”抽象成可替换 launcher，共享同一套 host/result 协议：

- `CodemodeSandbox`：Node/Bun 中每次执行一个 worker thread，避免忙循环阻塞宿主事件循环。
- `InlineCodemodeSandbox`：在调用线程执行，适合没有 worker 的运行时；通过 QuickJS interrupt budget 防止计算无限运行。
- `RemoteCodemodeSandbox`：VM 放到另一个 isolate，通常是独立部署的 Cloudflare Durable Object；工具和调用记录仍在 agent 所在 host。

证据：`packages/codemode/src/runtime/execution.ts:1-8,455-555`、`packages/codemode/src/runtime/inline.ts:1-123`、`packages/codemode/src/runtime/remote.ts:1-120,260-274`。

### 3.3 host/VM 分离和生命周期

pi 的 host 持有工具表、调用记录、deadline、abort signal、store 和工具执行；VM 只能通过消息协议发出 `call`，host 回传 `result`。执行结束、超时或 abort 时，host 取消 pending 工具，并要求 launcher 停止 VM。

它还提供：

- QuickJS heap `memoryLimitBytes`；
- worker admission 和实际 worker exit 追踪；
- 每次执行独立 VM；
- `store/load` 的 JSON 状态和成功写入增量；
- `start()` 的确定性运行：固定时间、随机数、UTC、结果序号和 VM fingerprint，宿主可以在重启后按相同结果顺序重放。

证据：`packages/codemode/src/runtime/execution.ts:178-239,455-555`、`packages/codemode/src/types.ts:112-245`、`packages/codemode/README.md:58-105,176-235`。

### 3.4 pi coding-agent 的模型集成

`reference-projects/pi/packages/coding-agent/src/extensions/codemode/tool.ts` 把 sandbox 包装成 AgentTool：

- `codemode` 是 model-only 工具，脚本不能递归启动另一个 codemode。
- 每个嵌套工具调用经 `ctx.executeTool`，所以普通工具的 validation、tool hooks 和 permission checks 继续生效。
- `mode: on` 允许工具同时直接暴露和被脚本调用；`mode: only` 只让模型看到 codemode，脚本拥有工具目录。
- `inlineBudget` 控制模型描述中内联的工具声明；未列出的工具可通过 `searchTools`、`describeTool`、`describeNamespace` 发现。
- 可选的 `models.*` 暴露 classifier/image generation，调用成本和 usage 会回收到 codemode details。

`execute.ts` 还负责 nested call rows、output truncation、image 保存、store entry 和 model usage 汇总。

证据：`packages/coding-agent/src/extensions/codemode/tool.ts:1-18,90-136,224-391`、`packages/coding-agent/src/extensions/codemode/execute.ts` 中 `executeCodemode()` 和 `createDiscoveryGlobals()`。

### 3.5 pi 与 opencode 的一个关键差别

pi 的 JSON Schema 主要用于生成 TypeScript declarations；值经过 JSON round trip，但 sandbox 本身不会像 opencode Effect Schema 那样自动做输入/输出 schema validation。实际授权仍由 host 的工具执行函数决定。

## 4. 两者的共同点

1. **模型只发一个顶层 CodeMode 工具调用。** 代码在本地/隔离运行时编排多个 nested tool calls。
2. **nested results 不进入 LLM context。** 脚本可以先取结果、过滤、聚合、并发或链式调用，最终只把必要信息返回给模型。
3. **能力由宿主注入。** 运行时没有任意文件、进程或网络能力；需要 HTTP、模型服务、MCP 等能力时，必须显式注入 extension/global/tool。
4. **支持异步编排。** 独立调用可以并发；依赖关系可以用普通代码表达；未完成的 promise 在脚本结束时被取消或发出 warning。
5. **有模型可见工具目录。** 两者都把工具签名/描述放入 CodeMode 说明，并针对大目录做预算和发现机制。
6. **有宿主侧执行边界。** 超时、取消、输出限制、调用记录、结构化结果和错误都由 host 控制，而不是让脚本自行决定。

## 5. 两者的主要差异

| 维度 | opencode | pi |
|---|---|---|
| 运行引擎 | 自研 JavaScript 子集解释器 | QuickJS 编译到 WASM |
| 隔离重点 | 语言级 capability confinement | VM + worker/isolate 运行隔离 |
| 运行位置 | 主要是本地 Effect runtime | Node/Bun worker、inline、远程 Durable Object |
| schema | Effect Schema 可实际校验 input/output；JSON Schema 可只用于渲染 | JSON Schema 主要生成 declarations；运行时不自动校验 |
| 工具发现 | 内置同步 `search`，精确路径、分页、排名 | `ALL_TOOLS`、`searchTools`、`describeTool`、`describeNamespace` |
| 目录管理 | namespace summary、pinned、语义增量、预算 | namespace、deferred exposure、inlineBudget、动态 MCP loadout |
| 限制 | timeout、maxToolCalls、maxOutputBytes、pending promise/值深度等 | timeout、heap memory、interrupt budget、worker admission、输出 token budget（agent 层） |
| 持久状态 | 没有通用内建 store/replay API | `store/load` 和 `start()` 确定性运行/重放 |
| 网络 | core 明确注入受限 `fetch` extension | 默认无网络，需要 host global 显式注入 |
| Agent UI | core progress hooks、文件附件收集 | nested call details、image 文件、output spill、model usage |
| 依赖和成本 | 解释器实现复杂，运行时轻量 | WASM/QuickJS/worker 依赖更重，但脚本兼容性和部署面更广 |

## 6. Codey 当前已经有什么

### 6.1 已有的批量/并发相关能力

Codey 已经有两个模型可见的只读批量 wrapper：

- `read_files`：一次提交多个路径；
- `parallel`：最多四个 `list_dir`、`read_file`、`grep` 的只读调用。

证据：`codey/toolchain/definition.py:11,39-160`、`codey/operations/kernel_protocol.py:428-475`。

这些 wrapper 会在协议层展开为多个逻辑 `ToolCall`，因此能减少“模型发一个调用、等待一个结果”的 LLM 回合数。但它们仍是预定义 JSON wrapper，不是模型编写的通用程序，也不能表达任意过滤、循环、分支和跨工具依赖。

更关键的是：

- provider payload 目前显式设置 `parallel_tool_calls=False`：`codey/providers/api_chat.py:116`、`codey/providers/api_responses.py:130`；
- `codey/runtime/write/file_mutation_queue.py:4-6` 明确说当前 loop 仍串行执行，分组只是为未来并发准备；
- `codey/operations/kernel_execution.py:_execute_slots()` 逐个调用 `_run_via_delegate_or_fn()`。

所以 Codey 当前能做到“少一些模型回合”，但不能宣称已经做到“本地并发执行并缩短全部工具墙钟时间”。

### 6.2 Codey 不应被 CodeMode 绕过的强项

Codey 的现有 kernel 已经比两个参考项目更强调 durable correctness：

- 每个工具效果有 intent/settlement identity；
- 工具结果交付有 batch intent/delivered receipt；
- safe tool replay 和 mixed batch fail-closed；
- policy、approval、controller snapshot 在执行前冻结；
- edit 后有 workspace revision/fingerprint provenance；
- 大输出可以通过 managed output/artifact 引用保存；
- native provider call id 和 tool result delivery 有协议窗口保护；
- cancel/recovery/attempt fencing 都有 durable 边界。

CodeMode 如果直接调用 `tool_fns.read_file()`、`tool_fns.edit_file()` 或 `subprocess`，会破坏这些保证，也会产生第二套执行真相源。正确的嵌入点应是现有 kernel/delegate/effect pipeline，而不是底层工具函数。

## 7. Codey 可以借鉴什么

### 7.1 优先吸收 opencode 的能力

这些能力与 Codey 的 Python/durable 架构最匹配：

1. **受限脚本语言或受限 AST。** 只允许变量、数组/对象、条件、循环、函数、`await`、`Promise.allSettled` 和 `tools.*`；禁止 filesystem/process/import/eval/network 默认能力。
2. **精确工具声明。** 从 Codey 现有 `ToolDefinition` 生成 script-visible signatures、输入字段说明和输出类型。
3. **目录摘要 + 搜索。** 复用 namespace、预算、pinned、增量更新思路；大工具集只内联摘要，脚本内通过同步/异步 search 获取精确签名。
4. **输入/输出边界校验。** CodeMode 结果应进入现有 `ToolResult`/`ToolOutcome` projection，不能直接把任意 Python object 带进脚本或 provider。
5. **稳定诊断。** 将 parse、unknown tool、invalid input、tool failure、timeout、output truncated、approval pending 等统一成可恢复的错误类别。
6. **嵌套调用进度。** UI 显示 parent `codemode` 与 child rows，但模型只收到最终 bounded output。

### 7.2 优先吸收 pi 的能力

这些能力适合 Codey 的 durable/recovery 方向：

1. **host/VM 分离。** 脚本运行时只发 call message；host 负责真正的 Codey kernel 调用。
2. **worker/remote 可替换 launcher。** 本地可用 worker；无 worker 环境可用 inline；未来可把计算密集脚本放到独立 worker。
3. **内存和中断预算。** 不能只依赖 wall-clock timeout；需要防止纯计算、超大数组和超深递归阻塞宿主。
4. **store/load 增量。** 将脚本的小型状态作为 branch-local durable custom entry 或任务状态更新，限制大小并保持幂等。
5. **确定性 run/replay。** 对“脚本已发出哪些 nested calls、宿主已经结算哪些结果”建立可重放协议；复用 Codey 的 effect identity、batch digest 和 recovery gate。
6. **工具 admission 统计。** worker/VM 生命周期要和并发配额绑定，不能在脚本返回后立即释放仍在退出的执行资源。
7. **动态工具暴露。** 允许 MCP/第三方工具以 deferred/codemode exposure 进入脚本目录，但每次执行仍使用冻结的 turn snapshot。

## 8. 对 Codey 的推荐架构方向（仍不改代码）

### 阶段 A：先做“CodeMode kernel contract”，不先选语言

定义一个脚本执行抽象，至少包括：

- `code`、工具快照、权限/策略快照、run/turn identity；
- host 发出的 nested call：`parent_call_id + child_index + tool_name + args_digest`；
- host 返回 structured result 或 approval-pending；
- output、logs、child call rows、store writes；
- timeout、abort、memory/step budget；
- durable batch intent、每个 child effect settlement、最终 result delivery receipt。

这样可以先验证 Codey 的 durable 语义，再决定底层使用 QuickJS/WASM、受限 Python AST，还是两者之一。

### 阶段 B：优先只开放只读工具

第一版只允许 `read`、`ls`、`search`、`references` 以及必要的纯研究查询。禁止脚本直接 `edit`、`run`、`shell`。理由是只读调用最适合并发，且不会引入写入顺序、approval pause 和 workspace provenance 的复杂组合。

脚本完成后只返回过滤后的摘要、文件引用或 bounded artifact；不要把十个完整文件正文自动拼回模型上下文。

### 阶段 C：接入已有并发规划和 durable batch

脚本的独立 calls 先生成逻辑调用图，再由 Codey 的 scope planner 分组：

- 不同文件的只读调用可并发；
- 同一文件的 read/edit 或任何 write/verify/approval 形成 barrier；
- mixed safe/unsafe batch 在恢复时 fail closed；
- 结果按脚本 source order 归并，不能按完成先后改变模型看到的序列。

这一步才能把“CodeMode 少 LLM 回合”和“本地并发减少墙钟时间”同时兑现。

### 阶段 D：再考虑写入和可恢复脚本

写入脚本必须能暂停在 approval 或 effect boundary，并在重启后继续，而不是重新运行整段脚本。这里应借鉴 pi 的 host-controlled `start()/deliver()` 思路，但把每次 deliver 接到 Codey 的 durable effect settlement 和 provider delivery receipt。

## 9. 必须避免的误区

1. **CodeMode 不会自动消除所有 LLM 往返。** 它把可预先写成程序的工具链压缩成一次脚本调用；如果需要模型根据中间结果重新推理，仍需新的模型回合。
2. **`Promise.allSettled` 不等于 Codey 已经并发。** 当前 Codey wrapper 展开后仍由 `_execute_slots()` 串行执行；需要真正的 executor concurrency 和 durable ordering 才能得到墙钟收益。
3. **不能把脚本沙箱当作授权层。** 沙箱只能限制语言和 host capability；真正的路径、权限、approval、workspace 和 durable policy 仍必须由 Codey host/kernel 决定。
4. **不能把所有 nested output 原样交回模型。** 应沿用 Codey 的 bounded model text、managed output 和 artifact ref 机制。
5. **不能用一次性重跑替代恢复。** 脚本已经产生部分副作用时，重启后从头运行可能重复 edit、run 或外部请求；必须记录 child effect identity 和已交付结果。
6. **不能未经 A/B 直接打开全量 CodeMode。** 对 Codey 现有 coding writer，先测 read/search exploration 的 turns、tool calls、wall time、token、恢复和误操作率，再决定默认暴露范围。

## 10. 参考文件索引

- opencode Agent 入口：`reference-projects/opencode/packages/core/src/codemode/tool.ts`
- opencode 目录和提示：`reference-projects/opencode/packages/core/src/codemode/catalog.ts`、`instructions.ts`
- opencode runtime API：`reference-projects/opencode/packages/codemode/src/codemode.ts`、`tool-runtime.ts`
- opencode 解释器执行：`reference-projects/opencode/packages/codemode/src/interpreter/execute.ts`
- opencode 说明：`reference-projects/opencode/packages/codemode/README.md`
- pi sandbox 说明：`reference-projects/pi/packages/codemode/README.md`
- pi host 执行：`reference-projects/pi/packages/codemode/src/runtime/execution.ts`
- pi worker/inline/remote：`reference-projects/pi/packages/codemode/src/runtime/worker.ts`、`inline.ts`、`remote.ts`
- pi Agent 集成：`reference-projects/pi/packages/coding-agent/src/extensions/codemode/tool.ts`、`execute.ts`
- Codey 工具契约：`codey/toolchain/definition.py`
- Codey batch protocol：`codey/operations/kernel_protocol.py`
- Codey 当前串行执行：`codey/operations/kernel_execution.py` 的 `_execute_slots()`
- Codey provider 并行开关：`codey/providers/api_chat.py`、`api_responses.py`
- Codey 未来并发规划：`codey/runtime/write/file_mutation_queue.py`
- Codey durable tool batch：`codey/runtime/write/tool_batches.py`

## 11. 实现后网页模型能否使用 CodeMode

可以，但网页模型走的是 Codey 的**文本协议适配层**，不是 API provider 的原生 tool calling 通道。

### 11.1 网页模型的调用形态

网页模型当前由 `WebChatProvider` 通过浏览器发送普通文本；Codey 的系统提示要求模型返回一个 JSON 对象，例如：

```json
{"tool":"codemode","args":{"code":"const xs = await Promise.allSettled([...]); return xs;"}}
```

因此实现 CodeMode 后，只要把 `codemode` 加入当前 turn 的模型可见工具契约，网页模型就可以输出这个顶层 JSON 调用。解析结果仍进入现有 `normalize_turn()`、policy snapshot、`execute_turn()` 和 durable settlement 流程。

证据：`codey/providers/web_provider.py` 只提供浏览器-backed `send()`；`codey/toolchain/tool_prompt.py:1-80,169-286` 规定网页/文本模型返回本地 runner JSON；`codey/operations/kernel_protocol.py:537-632` 负责把文本协议转成 `ToolPlan`。

### 11.2 与原生 API 模型的差别

API/native 模型可以收到结构化 function schema，并返回带 call id 的原生调用；网页模型只能看到提示词中的 JSON contract，并在网页文本框里生成 JSON。两者可以共享同一个 `codemode` 工具定义和 host executor，但解析与失败模式不同：

- native：参数结构由 provider 协议承载，Codey 还可以绑定 protocol window 和 call id；
- web/text：依赖模型严格输出一个 JSON 对象，代码字符串还要正确转义换行、引号和反斜杠；markdown fence、截断或多余解释都会触发协议修复或失败。

所以“网页模型可用”不等于“网页模型与原生工具调用同样稳定”。首版应优先要求短脚本、只读工具和严格 JSON；不要一开始让网页模型在脚本中执行 edit/run/shell。

### 11.3 网页模型真正能得到的收益

如果脚本执行器能把多个只读 nested calls 真正并发执行，网页模型也能获得两层收益：

1. 模型只需输出一次 `codemode` JSON，减少模型与浏览器之间的多轮往返；
2. CodeMode host 在本地并发读取/搜索，再只返回过滤后的结果，减少网页模型上下文和页面交互次数。

但如果 CodeMode 内部仍串行调用工具，网页模型只能获得第一层“少模型回合”收益，不能获得工具墙钟时间收益。

### 11.4 实现时需要加入的兼容边界

- `codemode` 必须出现在 web/text protocol 的 tool contract 中，不能只注册到 API native schema。
- `code` 应有明确长度、行数和嵌套调用预算；网页聊天页面的输入/输出截断比 API 更常见。
- JSON parser 只接受一个顶层对象；不要从任意自然语言中猜测或执行脚本。
- 脚本语法错误、JSON 转义错误、网页输出截断和 CodeMode runtime 错误要分别归类，避免把协议错误误判成工具失败。
- 网页模型的 `shell`/approval、取消和浏览器 tab 断连必须通过 Codey durable boundary 处理；不能让脚本绕开现有 provider/session 生命周期。

## 12. 对“Pi + OpenCode + Codey”整合方案的评审

这份整合方案的**战略方向成立**：Codey 应吸收 Pi 的 host/VM 分离、资源限制、可恢复运行，以及 OpenCode 的受限语言、工具目录、搜索和 hooks；最终把 nested calls 接入 Codey 自己的 durable kernel。

但下面几处需要收紧，否则实现时会把参考项目的能力边界误判得过强。

### 12.1 Pi 不是“完整 ES2023 且具备操作系统级隔离”

Pi 使用 QuickJS/WASM，确实比自研 AST 解释器覆盖更广的 JavaScript，也支持 `eval` 和 `Function`。但它仍然刻意删除了 timers、`fetch`、`process`、`require`、modules、WebAssembly 等外部能力；项目没有把它宣称为完整 ES2023 兼容实现。

Node/Bun 的 worker 是线程级运行隔离，Cloudflare remote 是 isolate 级部署隔离；这能防止脚本阻塞宿主并提供 heap/interrupt 限制，但不应写成操作系统级 sandbox。安全边界仍然来自 QuickJS VM 加上 host 只注入有限 capability。

证据：`reference-projects/pi/packages/codemode/README.md:49-62,128-159`。

### 12.2 Pi 的 `store/load` 是宿主管理的小状态，不是自动的大对象持久化

Pi 的 sandbox 本身不持久化 store。宿主需要在下一次执行时把旧值传入 `options.store`，再把成功结果中的 `storeWrites` 应用到自己的存储。

它还明确限制单值约 256 KiB、总 JSON 约 1 MiB，并建议 store 只保存 ID、摘要等小状态。大依赖矩阵或大文件内容应进入 Codey 的 managed output/artifact store，脚本 store 只保存 handle、digest、分页游标或小型索引。

证据：`reference-projects/pi/packages/codemode/README.md:82-92`、`reference-projects/pi/packages/codemode/src/runtime/prelude-source.ts:34-35,280-313`。

### 12.3 Pi 的 declarations 是提示字符串，不是实际 `.d.ts` 编译链

`renderDeclarations()` 会把 JSON Schema 渲染成 TypeScript declaration 文本，供模型查看和生成代码；它不会生成、编译或加载真正的 `.d.ts` 文件。并且 Pi 明确说明这些 schema 只塑造声明，运行时不负责 schema validation。

因此 Codey 可以借鉴“从 ToolDefinition 生成脚本声明”，但必须把声明层和现有 kernel 的实际参数验证分开。

证据：`reference-projects/pi/packages/codemode/src/declarations.ts:1-41`、`reference-projects/pi/packages/codemode/README.md:252-264`。

### 12.4 “每个子调用挂在同一个 DurableMutation 事务里”不可直接实现

CodeMode 的 child tool call 可能是外部网络、文件系统或进程操作。不能在这些外部操作持续期间持有一个 SQLite transaction，也不能把“执行外部副作用”和“数据库提交”伪装成一个原子事务。

Codey 应采用现有 effect sandwich：

1. parent CodeMode execution admission，记录脚本、工具快照、策略快照和 parent identity；
2. 每个 child call 执行前单独记录 effect intent；
3. 外部工具完成后单独记录 settlement、bounded result 和 artifact refs；
4. 脚本终止后记录 parent output、store writes 和最终 delivery receipt。

多个 child intent 可以放在同一个“batch intent”中，但每个 child 的结算仍必须有独立 identity。Codey 已有 `codey/runtime/write/tool_batches.py`、`codey/durable/session.py` 的对应事实模型，应该扩展它们，而不是持有长事务。

### 12.5 Ghost Hebbian 目前不能直接宣称会自动学习 CodeMode 脚本

`codey/ghost/hebbian.py` 确实存在 Hebbian graph 和 accepted candidate 流程，但当前源码证据只能证明它维护经过接纳的 Ghost memory candidates；没有现成的“捕获任意 CodeMode source、抽象工具组合、自动生成模板并注入下一次执行”的闭环。

将 CodeMode pattern 接入 Ghost 应作为后续明确设计：保存脱敏的 tool sequence、依赖关系、成功/失败类别、策略类别、延迟和 digest；经过用户或 release gate 接纳后，才作为建议模板或检索提示使用。不要把原始脚本、文件正文、secret 或未经验证的工具动作直接写入 Hebbian memory，也不要让 Ghost 自动执行新组合。

### 12.6 OpenCode 的“无 fetch”描述需要限定范围

`@opencode/codemode` 基础 runtime 没有直接网络权限，但 opencode core 明确注入了受限 `fetch` extension，并拒绝 `file:` URL。准确说法应是“网络必须由 host 显式注入”，而不是“两者都绝对没有 fetch”。

证据：`reference-projects/opencode/packages/core/src/codemode/tool.ts:62-68,160-174`、`reference-projects/opencode/packages/core/src/codemode/web.ts:13-42`。

## 13. 更稳妥的 Codey 落地版本

建议把方案收敛成下面四层：

### 第一层：CodeMode host contract

定义脚本 source、frozen tool snapshot、policy snapshot、parent identity、child call protocol、bounded output、store writes、timeout/abort 和 runtime diagnostics。先固定这个协议，再选 VM。

### 第二层：只读脚本沙箱

第一版只开放 `read`、`ls`、`search`、`references` 和纯研究查询。脚本不能直接访问 Python、文件系统、subprocess 或网络；所有能力都通过 host adapter 进入现有 `execute_turn()` / `ExecutionDelegate`。

### 第三层：并发与 durable batch

独立只读 calls 才允许并发；写入、验证、shell、approval 和同路径冲突形成 barrier。结果按脚本 source order 归并，child settlement 按独立 effect identity 持久化，恢复时对 mixed safe/unsafe batch fail closed。

### 第四层：状态、写入和 Ghost

小状态使用 branch-local durable store；大数据使用 artifact handle。写入工具要等 approval、effect settlement 和 workspace provenance 全部接入后再开放。Ghost pattern 只在有成功率、延迟、恢复和安全 A/B 证据后进入推荐层。

在 VM 选型上，首选应是一个可替换的 JavaScript host adapter：先评估 QuickJS/WASM worker 的部署成本，再决定是否需要受限 Python。不要把 `RestrictedPython` 或 Pyodide 直接当成安全边界；它们需要额外的 capability、资源和导入审计，不能仅凭“禁用 builtins”证明安全。

