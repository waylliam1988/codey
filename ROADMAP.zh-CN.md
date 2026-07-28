# Codey 未来版本规划

这份文档从 `0.2.24` 之后开始规划 Codey 的近期版本。它不是功能愿望清单，而是内部架构路线图：先把 Codey 已经具备的能力整理成更清楚的事实、工具、上下文和权限边界，再在这个地基上做导出、headless、项目配置等用户可见能力。

## 总方向

Codey 不应该变成 Pi 或 OpenCode 那样的大平台。Codey 的优势仍然是：

> 网页 AI 负责思考，本地 Codey 负责边界、工具、来源、diff、恢复和验证。

未来几个版本要做的核心事情，是把这些边界变成可命名、可投影、可测试的数据：

```text
agent run
tool definition
permission / trust event
session facts
context source
provider capability
managed output
diff / snapshot / restore id
```

这样做的目标不是增加主界面概念，而是让 Codey 内部更稳：同一份事实可以同时服务 checkpoint、receipt、review、restore、provider debug、A/B report 和未来的 headless runner。

## 规划原则

1. 每个版本只收拢一个核心边界，避免大重构。
2. 新增架构对象先只服务内部，不增加用户需要理解的新模式。
3. 不保存完整源码、完整网页正文、完整 DOM、Cookie 或完整长期聊天 transcript。
4. Research 仍然必须由用户显式开启；Project Writer 默认不联网。
5. shell、edit、restore 等本地能力继续保持保守审批和可恢复边界。
6. 所有新状态都要有生命周期、大小上限和失败降级策略。
7. UI 继续保持安静：用户看到的是 Chat、Research、Project、Review、Diff、Restore，而不是 registry、profile、epoch、ledger 这类内部词。

## 0.2.25 - Run Ledger v1

状态：第一版已落地为 observe-only coding run ledger。

### 做什么

新增一个 append-only 的运行事实账本，先覆盖 coding run，不重写现有 agent loop，不改 UI 大结构。

第一版只记录有边界事实：

```text
run_started
provider_selected
model_reply
tool_started
tool_finished
file_changed
command_verified
changes_collected
provider_failure
provider_switched
run_finished
```

账本复用现有 `RunEvent`、`WorkCheckpoint`、changes tracker、provider supervisor 和 receipt 产生的事实，但不要求这些模块一次性迁移。`model_reply` 只记录回复字符数和 bounded note，不保存模型回复正文；`changes_collected` 只记录最终用于 receipt 的那份 changes 统计，不制造 snapshot id。

Hybrid run 会在 run lifecycle 开始时打开 ledger；Research 阶段不记录 Research tool events，只有进入 Project Writer 后才记录 coding tool facts。如果 Research 阶段提前失败，ledger 可能只包含 `run_started`、`provider_selected` 和 `run_finished` 这类生命周期事实。

### 为什么做

Codey 已经很重视 diff、restore、verification、provider failure、Research evidence 和 A/B report，但这些事实现在分散在不同模块里。Run Ledger 让一次任务有一个统一事实来源。

### 对 Codey 的好处

- checkpoint 可以从真实事件恢复，而不是只依赖最新摘要。
- review 和 restore 能引用同一批 diff / snapshot 事实。
- provider 故障可以关联到具体 run、turn 和发送阶段。
- A/B harness 可以直接比较事件指标，而不是再解析散文日志。
- 未来 export、fork、headless 都有干净的数据基础。

### 验收标准

- coding run 结束后能落盘一份 bounded ledger。
- ledger 写入失败不能破坏正在执行的任务；关键 snapshot 写入失败仍按现有安全策略阻止对应写入。
- ledger 不保存完整模型回复正文、完整文件内容或完整 shell 输出。
- 现有 UI、receipt、checkpoint 行为保持兼容。

### 暂不做

- 不做完整聊天 JSONL 导出。
- 不做训练 episode。
- 不做 headless runner。
- 不迁移 Research ledger，也不记录 Research tool events。

## 0.2.26 - Ledger Projections

### 做什么

在不扩大 ledger 内容的前提下，先让一部分现有结果从 Run Ledger 投影出来：

```text
task receipt
work checkpoint
verification summary
provider debug summary
manual A/B metrics
```

这一步的重点是证明 ledger 不是空架构，而是真的能减少重复事实通道。

### 为什么做

如果只写 ledger 但没人读，它会变成额外维护成本。第二个版本应该立即让核心读模型开始消费 ledger。

### 对 Codey 的好处

- 任务收据更可靠：`files changed`、`checks passed`、`restore available` 来自统一事件。
- checkpoint 更可解释：能知道最后一次 edit、最后一次 run、最后一次绿色检查分别发生在哪里。
- provider debug 不再只看当前健康状态，也能回看导致熔断的 run 事实。
- A/B 报告能更稳定地统计 turn、tool、repair、verification、failure。

### 验收标准

- receipt 至少有一项字段来自 ledger projection。
- checkpoint 能在不读取完整聊天的情况下恢复关键任务事实。
- A/B harness 可以读取 ledger 统计基础指标。
- projection 失败时能回退到现有路径。

### 暂不做

- 不把所有旧状态一次性删除。
- 不改变用户的任务历史 UI。
- 不做跨 session 复杂查询。

## 0.2.27 - ToolDefinition v1

### 做什么

把 coding tools 的契约收拢成内部 `ToolDefinition`，先覆盖现有核心工具：

```text
read_file
edit_file
write_file
list_directory
search_files
find_references
run_command
done
```

每个工具定义包含：

```text
name
schema
permission
output_projection
render_hint
repair_hint
```

这只是内部对象，不是插件系统。

### 为什么做

Codey 现在已经有工具 runtime、JSON codec、typed repair、shell risk、UI tool line，但“工具是什么”仍然分散。ToolDefinition 让同一份工具契约同时服务协议提示、参数校验、权限判断、ledger 事件、UI 渲染和 repair prompt。

### 对 Codey 的好处

- 新增或修改工具时，不必同时记得改 prompt、parser、UI、测试和权限说明。
- typed repair 可以从工具定义生成更一致的示例。
- ledger 里的工具事件可以带稳定 tool kind 和输出投影。
- UI tool line 可以少解析散落字段。
- 后续 ContextSource 和 permission profile 可以按工具定义组合。

### 验收标准

- coding tools 都有内部定义。
- 现有 JSON tool protocol 对模型保持兼容。
- 现有 `run` whitelist 和 read-before-edit guard 不被放宽。
- tests 能覆盖工具 schema、错误 repair 和 tool event payload。

### 暂不做

- 不开放第三方工具。
- 不做插件市场。
- 不把工具定义暴露到主 UI。
- 暂不迁移 Research tools。

## 0.2.28 - ContextSource v1

### 做什么

把现有 prompt 上下文块统一成命名的 `ContextSource`：

```text
project_map
coding_current_context
work_checkpoint
verified_facts
research_brief
project_instructions
provider_repair_hint
```

每个 source 至少声明：

```text
key
loader
budget
freshness
renderer
why_included
failure_policy
```

### 为什么做

Codey 已经有很多上下文块，但它们都在回答同一个问题：这段信息为什么进入 prompt、来自哪里、预算多少、什么时候刷新。ContextSource 让这些问题变成代码契约，而不是手写拼接习惯。

### 对 Codey 的好处

- prompt 更可测试，少出现重复、过期或越界上下文。
- Project Map、checkpoint、verified facts、Research Brief 的预算可以统一管理。
- provider 接管时能更干净地重建“只包含本地事实”的新会话提示。
- 未来 headless runner 可以复用同一套上下文装配，而不是另写一套 prompt。

### 验收标准

- 至少 Project Map、Coding Current Context、Work Checkpoint 和 Verified Facts 走 ContextSource。
- 每个 source 有字符预算和失败降级。
- tests 能证明普通聊天不会注入项目上下文，Project Writer 默认不会注入网页搜索能力。
- prompt 输出与旧行为语义兼容。

### 暂不做

- 不做向量索引。
- 不做自动长期记忆注入。
- 不把整个 Research vault 注入 Writer。
- 不新增 UI 模式。

## 0.2.29 - Provider Capability Registry

### 做什么

把网页模型和本地模型的运行能力数据化，形成内部 provider capability registry。

能力可以包括：

```text
json_reliability
send_readiness
completion_detection
context_budget_hint
research_fit
coding_fit
review_fit
failure_families
native_tool_interference_risk
needs_canary
```

这不是排行榜，也不是用户选择器里的复杂说明；它服务内部路由、prompt 收紧和故障接管。

### 为什么做

Codey 的特殊价值是兼容网页 AI。网页模型不像 API 模型那样有稳定能力声明，所以 Codey 需要自己的、本地验证过的 capability 层。

### 对 Codey 的好处

- 不同 provider 可以拿到更合适的 prompt 和等待策略。
- Research 可以避开明显不适合严格 JSON tool loop 的 provider。
- Writer 接管可以基于能力和健康状态，而不是只看“是否在线”。
- provider repair 和 canary 可以更有针对性。
- 本地 OpenAI-compatible 模型也能纳入同一套能力判断。

### 验收标准

- registry 有静态内置能力和运行时健康状态的清晰分界。
- provider failure 能回写 bounded 统计，但不保存聊天正文。
- provider 选择 UI 不增加复杂技术文案。
- 至少一处 prompt / run policy 使用 capability，而不是硬编码 provider id。

### 暂不做

- 不做自动成本优化平台。
- 不做 provider 排名页。
- 不让模型自己决定切换 provider。
- 不把 capability 当作训练数据长期上传。

## 0.2.30 - Managed Output Handles

### 做什么

为长输出建立 managed output handle：完整内容保存在本地受控位置，模型只看到摘要、head/tail、hash、大小和 handle。

覆盖对象可以从 coding run 开始：

```text
long shell output
large test logs
large search results
large file scan reports
```

Research 后续也可以复用：

```text
opened webpage text
PDF extracted text
source_search long hits
```

### 为什么做

Pi 和 OpenCode 都有类似思路：长输出不能全部塞进上下文，但也不能直接丢。Codey 现在已经会截断输出，下一步应该保留全文 handle，让验证和 debug 更可追溯。

### 对 Codey 的好处

- 模型上下文更短，仍能在需要时按 handle 读取局部。
- 测试失败、构建日志、网页证据都能保留可审计来源。
- ledger 可以记录输出 hash 和 handle，receipt/checkpoint 不再丢关键证据。
- A/B 报告能比较真实输出大小和截断情况。

### 验收标准

- 长 `run_command` 输出会生成 handle，工具结果只返回 bounded projection。
- handle 有生命周期和大小上限。
- handle 路径不会逃出 Codey state 目录。
- 默认不会把 handle 内容注入后续 prompt。

### 暂不做

- 不做全文搜索数据库。
- 不做自动 RAG。
- 不保存无限期日志。
- 不把网页正文长期混入项目事实。

## 0.2.31 - Internal Permission Profiles

### 做什么

在内部定义少量 permission profile，用于组合工具和上下文边界：

```text
chat
planning_readonly
coding_writer
reviewer
research
```

这些 profile 由运行时选择，不作为复杂模式 UI 暴露给用户。

### 为什么做

Codey 的用户不应该先理解 agent mode 才能工作，但运行时需要更明确地知道当前任务允许什么。permission profile 可以把“Research 能联网但不写项目”“Reviewer 只读 diff 和检查”“Writer 能 edit 但默认不联网”这些规则数据化。

### 对 Codey 的好处

- 权限边界更容易测试。
- Research 和 Project Writer 的隔离更清楚。
- Review 不会意外获得写权限。
- 未来高频 action 可以复用 profile，而不是新增散落 if/else。

### 验收标准

- profile 只影响内部 allowed tools 和 context sources。
- 主 UI 不新增 `mode`、`agent profile`、`planner` 等技术词。
- tests 覆盖 chat 无项目权限、research 有网页工具但无项目写入、reviewer 不写文件。

### 暂不做

- 不做多 agent 图形编排。
- 不做用户可配置权限矩阵。
- 不做后台自主 agent。

## 0.2.32 - Headless JSONL Runner

### 做什么

在 Run Ledger、ToolDefinition 和 ContextSource 稳定后，新增一个 headless JSONL runner，用于可重复任务、smoke、A/B 和外部脚本调用。

输入输出都应尽量复用 ledger event schema：

```text
input: task, project, provider policy, max turns, research flag
output: bounded run events, receipt, ledger path, exit status
```

### 为什么做

Headless runner 是用户可见能力，但它必须建立在清楚的内部事实模型上。否则它会复制一套 agent loop、prompt 拼接和事件格式，最终让架构更散。

### 对 Codey 的好处

- 自动化 smoke 和回归测试更容易。
- 用户可以在 CI 或脚本里运行受控 Codey 任务。
- A/B harness 可以从临时脚本变成正式能力。
- 未来 export/fork 可以基于同一份 ledger。

### 验收标准

- headless 输出 JSONL 与 Run Ledger schema 对齐。
- 可以跑只读 explain / review 类任务，也可以跑受控 coding task。
- shell 审批在 headless 下有明确策略：默认拒绝或要求预先 allowlist。
- 不绕过 Codey 的项目边界、工具权限和 Research 显式开关。

### 暂不做

- 不做无人值守的全电脑 agent。
- 不默认允许安装、发布、删除或外部账号操作。
- 不把 headless 做成新的产品主入口。

## 0.2.33 - Project-local Config

### 做什么

新增克制的项目本地配置文件，用来声明项目级安全、验证和上下文偏好。

第一版只考虑少量字段：

```text
verification commands
safe run allowlist
ignored paths
context budget hints
preferred provider policy
```

### 为什么做

Codey 已经能从项目事实里学习成功命令，但有些项目需要显式配置：例如 monorepo 的正确测试命令、不能扫描的生成目录、默认检查预算。项目配置可以减少模型猜测。

### 对 Codey 的好处

- 新项目更快进入正确验证路径。
- 大仓上下文预算更稳定。
- 团队可以共享安全的默认检查命令。
- headless runner 可以使用项目声明，而不是依赖临时参数。

### 验收标准

- 配置文件必须项目内显式存在，不自动写入。
- 危险 shell 命令不能仅因配置存在就绕过审批。
- 无配置项目保持现有行为。
- 配置解析失败有清楚错误，不导致隐式放权。

### 暂不做

- 不做插件配置。
- 不做复杂 workflow DSL。
- 不做云同步。
- 不把用户私密 provider 登录状态写入项目。

## 全部做完后的 Codey 会变成什么

这些版本完成后，Codey 表面不会变成更复杂的产品。用户仍然看到一个安静的本地 AI 编程与研究工作台：

```text
Chat
Research
Project
Review
Diff
Restore
```

但内部会发生明显变化：

1. 一次任务会有清楚的事实账本，所有收据、checkpoint、review、restore、debug 都能回到同一份 run facts。
2. 工具会有统一定义，协议提示、权限、输出、UI 和测试不再各写一份。
3. Prompt 上下文会由命名 source 组成，每段信息为什么出现、预算多少、何时刷新都可证明。
4. Provider 的网页特性和失败模式会数据化，接管、修复、Research 选择会更稳。
5. 长输出不再二选一地“塞爆上下文”或“直接丢掉”，而是保留本地 handle，只给模型有界视图。
6. Review、diff、restore、checkpoint 会围绕同一批 snapshot/diff id 协作。
7. Headless 和 export 可以自然出现，而不是另建一套 runner。

最终效果应该是：

> Codey 看起来仍然小而安静，但内部每一步都更可追溯、更可恢复、更可验证。

这也是 Codey 区别于全能 agent 平台的方向：不追求把所有能力暴露给用户，而是把本地边界、来源、验证、恢复这些基本功做得越来越扎实。
