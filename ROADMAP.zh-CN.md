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

## 0.2.26 - Ledger Projections v1

状态：第一版已落地为只读 ledger projection，并接入一条保守 receipt shadow-consume 路径。

### 做什么

新增 `run_ledger_projection.py`，把 0.2.25 写下来的 JSONL 账本投影成稳定摘要：

```text
run lifecycle / complete state
provider selected / switched / failed
model reply count and chars
tool call counts and errors
observed file_changed facts
verified commands
final changes_collected summary
task receipt candidate
```

`changes_collected` 增加顶层 `checks_passed` fact。Receipt projection 只使用
`changed_count`、`mode` 和 `checks_passed`，不从嵌套 legacy `receipt` 字典反推自己。

`TaskRunner` 在 `run_finished` 写入后、terminal event 发布前读取 projection。只有当
projection complete、ledger 未截断、有 final changes，且 projected receipt 的
`changed_count`、`restore_available`、`checks_passed` 与 legacy receipt 完全一致时，
才采用 projected receipt；否则原样回退 legacy receipt。

### 为什么做

如果只写 ledger 但没人读，它会变成额外维护成本。0.2.26 的目标是证明 ledger 能被稳定读取，并且能服务一条真实生产路径，但不急着接管 checkpoint、restore 或 UI。

### 对 Codey 的好处

- Run Ledger 从“飞行记录仪”变成可测试的读模型。
- receipt 的关键字段可以从统一事实投影出来，而不是永远依赖散落路径。
- provider failure/switch、工具调用、验证命令、最终 diff 统计有了同一个查询入口。
- 后续 checkpoint projection、export、headless JSONL、A/B metrics 可以复用同一层。
- projection 失败、账本不完整或字段不一致时自动回旧路径，用户体验不变。

### 验收标准

- `project_run_ledger(records)` 是纯函数，未知事件、坏行和未来 schema 安全忽略。
- projection 能回答 run 是否 complete、是否 truncated、最终 provider、失败/切换、工具统计、已观察修改文件、跑绿命令和最终 changes。
- `changes_collected.checks_passed` 是顶层 fact，receipt 投影不读取 nested `receipt`。
- terminal event 发布前至少有一条 project receipt shadow-consume projection。
- projection 不完整、截断或与 legacy receipt 不一致时回退旧路径。

### 暂不做

- 不迁移 `WorkCheckpoint`。
- 不替换 `ExecutionEvidence`。
- 不改 UI、SSE 或任务历史。
- 不做 API export、headless runner 或跨 session 查询。

## 0.2.27 - ToolDefinition v1

状态：第一版已落地为内部工具元数据层，并让 JSON codec 与 agent activity 读取同一份定义。

### 做什么

新增 `tool_definition.py`，把 coding JSON tools 的元数据集中到一个内部定义层。第一版只覆盖现有公开协议名，不新增工具名：

```text
list_dir
read_file
read_files
grep
find_references
parallel
edit
run
shell
done
```

Runtime tool names 保持现状：

```text
ls
read
search
references
edit
run
shell
```

每个工具定义包含：

```text
name
runtime_name
aliases
read_only
parallel_safe
permission
examples
description
output_facts
render_hint
repair_hint
```

`JsonToolCodec` 现在从 definition 层读取工具契约、alias、parallel-safe、result tool names 和 batch limit；`agent.py` 从 definition 层派生 supported runtime names、information follow-up names、repair 示例和 tool activity 行。`json_codec.py` 不再拥有或 re-export 工具定义表。

### 为什么做

Codey 现在已经有工具 runtime、JSON codec、typed repair、shell risk、Run Ledger 和 UI tool line，但“工具是什么”曾经主要藏在 codec 里。ToolDefinition v1 让工具元数据成为一等对象，同时不把 Codey 做成插件平台。

### 对 Codey 的好处

- 新增或修改工具时，工具名、alias、示例、parallel/read-only、权限描述和输出事实不再散落。
- typed repair 和 system prompt 可以复用同一批公共示例。
- agent 的 tool activity 行不再手写另一份工具分类。
- Run Ledger 能用测试锁住 `edit -> file_changed`、`run -> command_verified` 的声明一致性。
- 后续 permission profile、ContextSource、Provider Capability 可以围绕同一份工具定义扩展。

### 验收标准

- `tool_definition.py` 是唯一工具定义来源；codec 和测试都从它读取定义。
- `write` / `write_file` 仍然是 unknown tool，并继续 repair 到 `edit(content=...)`。
- 现有 JSON prompt contract fixture 不变，examples 全部能 parse。
- `parallel_safe` 必须同时是 `read_only`。
- runtime names 仍等于 agent 支持的工具集合。
- `edit` 和 `run` 的 `output_facts` 与 Run Ledger v1 实际事件一致。
- 现有 `JsonToolCodec._tool_call()` schema validation、read-before-edit guard、run allowlist、shell approval 都不被接管或放宽。

### 暂不做

- 不开放第三方工具。
- 不做插件市场。
- 不把工具定义暴露到主 UI。
- 不迁移 Research tools。
- 不让 ToolDefinition 接管 schema validation 或 runtime dispatch。

## 0.2.28 - ContextSource v1

状态：已落地。第一版是 prompt 上下文装配层，不接管业务 loader，不改变模型能力目标。

### 做什么

把现有 prompt 上下文块统一成命名的 `ContextSource`：

```text
project_instructions
verified_facts
research_brief
project_map
work_checkpoint
initial_listing
coding_current_context
```

每个 source 至少声明：

```text
key
loader
budget
freshness
why_included
failure_policy
```

`source` 的 metadata 先只给代码、测试和未来 projection 使用，不渲染进模型 prompt。
`ProjectTaskContextBuilder` 继续负责 verified facts、Research Brief、Project Map、
Work Checkpoint 和验证候选的业务加载；`ContextSource` 只负责把已经拿到的内容
做命名、预算、heading 和失败降级。

### 为什么做

Codey 已经有很多上下文块，但它们都在回答同一个问题：这段信息为什么进入 prompt、来自哪里、预算多少、什么时候刷新。ContextSource 让这些问题变成代码契约，而不是手写拼接习惯。

### 对 Codey 的好处

- prompt 更可测试，少出现重复、过期或越界上下文。
- Project Map、checkpoint、verified facts、Research Brief 的预算可以统一管理。
- provider 接管时能更干净地重建“只包含本地事实”的新会话提示。
- 未来 headless runner 可以复用同一套上下文装配，而不是另写一套 prompt。

### 验收标准

- Project instructions、Verified Facts、Research Brief、Project Map、Work
  Checkpoint、Initial Listing 和 Coding Current Context 走 ContextSource。
- 每个 source 有字符预算、freshness、why_included 和失败策略。
- 普通 loader 失败 fail-open；`TaskCancelled` 和 `DeadlineExceeded` 不被吞掉。
- `coding_current_context` 仍只在 tool result 后追加，不进入初始 prompt 或 repair prompt。
- prompt 输出与旧行为语义兼容，source metadata 不进入 prompt。

### 暂不做

- 不做向量索引。
- 不做自动长期记忆注入。
- 不把整个 Research vault 注入 Writer。
- 不新增 UI 模式。
- 不做 provider routing。
- 不迁移 checkpoint/restore。
- 不做 live A/B 作为本版本开工门槛。

## 0.2.29 - Provider Capability Registry

状态：已落地。第一版只做静态 provider 能力提示和 fallback 排序，不做调度系统。

### 做什么

把网页模型和本地模型的静态运行倾向数据化，形成内部 provider capability registry。

能力可以包括：

```text
json_reliability
context_budget_hint
research_fit
coding_fit
review_fit
failure_families
native_tool_interference_risk
needs_canary_by_default
```

这不是排行榜，也不是用户选择器里的复杂说明；它只服务内部 fallback 排序和后续策略收敛。
`ProviderSupervisor` 继续只负责 runtime health、cooldown 和 canary；runtime failure
不会改写静态 capability。

### 为什么做

Codey 的特殊价值是兼容网页 AI。网页模型不像 API 模型那样有稳定能力声明，所以 Codey 需要自己的、本地验证过的 capability 层。

### 对 Codey 的好处

- Research fallback 有替代 provider 时，可以避开静态标记为 `research_fit=avoid` 的 provider。
- Writer 接管的候选顺序可以逐步基于 task mode，而不是只看“是否在线”。
- Review helper 候选可以共享同一套静态排序，而不向 UI 暴露复杂术语。
- provider repair、canary 和 prompt 策略后续可以围绕同一份 capability 扩展。
- 本地 OpenAI-compatible 模型也能纳入同一套能力判断。

### 验收标准

- registry 有静态内置能力和运行时健康状态的清晰分界。
- `rank_providers()` 是纯函数，unknown provider 走 default，`avoid` 只排后不禁用；
  通用 hybrid 排序取 Research/Coding 中更严的适配度。
- `TaskRunner` 只在 selected provider 不可用、connect failure、canary failure 和
  Writer failover 这些必须换 provider 的路径消费 capability 排序。
- hybrid 启动 fallback 按 Research 排序；进入 Writer 后的 failover 按 Project 排序。
- `failure_families` 必须被测试约束在真实 `ProviderFailure` kind 词表内。
- provider 选择 UI 不增加复杂技术文案。
- runtime failure 不会修改静态 capability。

### 暂不做

- 不做自动成本优化平台。
- 不做 provider 排名页。
- 不让模型自己决定切换 provider。
- 不把 capability 当作训练数据长期上传。
- 不做 Research 中途 failover。
- 不做 live A/B 作为本版本开工门槛。
- 暂不消费 capability 里的 canary 字段。
- 暂不消费 `context_budget_hint`；未来使用前必须先补测量或实机证据。

## 0.2.30 - Managed Output Handles

状态：已落地。第一版只覆盖 Project Writer 的 `run` 工具长输出；只有模型可见
projection 实际被裁剪时才保存本地 handle。

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

- 长 `run_command` 输出在 projected result `truncated=True` 时生成 handle，工具结果只返回 bounded projection。
- handle 有 run-scoped 生命周期、单输出大小上限和单 run 数量上限。
- handle 路径不会逃出 Codey state 目录。
- 默认不会把 handle 内容注入后续 prompt。
- Run Ledger 只记录 handle、原始/保存字节数和保存输出 hash，不记录完整输出正文。

### 暂不做

- 不做 UI 或 `/api/output`。
- 不做 `read_output` 工具。
- 不做全文搜索数据库。
- 不做自动 RAG。
- 不保存无限期日志。
- 不把网页正文长期混入项目事实。

## 0.2.31 - Internal Permission Profiles

状态：已落地。第一版只把已有 runtime 阶段边界命名和数据化，不新增用户可见
mode，也不替换现有安全系统。

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

- profile 只影响内部 allowed tools 和 context sources；默认 Project Writer 行为保持等价。
- `planning_readonly` codec 不展示或执行 `edit`、`run`、`shell`。
- `unknown_tool` 和 `disallowed_tool` 被明确区分。
- `parallel` 不能绕过当前 profile。
- 主 UI 不新增 `mode`、`agent profile`、`planner` 等技术词。
- tests 覆盖 chat 无项目权限、research 有网页工具但无项目写入、reviewer 不写文件。

### 暂不做

- 不做多 agent 图形编排。
- 不做用户可配置权限矩阵。
- 不做后台自主 agent。

## 0.2.32 - Headless JSONL Runner

状态：已落地。第一版只覆盖 Project coding 和 readonly planning；`agent --json`
已迁到 TaskRunner-backed headless path，普通 `agent` CLI 暂时保留旧直接路径。

### 做什么

在 Run Ledger、ToolDefinition 和 ContextSource 稳定后，新增一个 headless JSONL runner，用于可重复任务、smoke、A/B 和外部脚本调用。

输入输出复用生产 TaskRunner 和有边界事件投影：

```text
input: task, project, provider, max turns, readonly flag
output: bounded JSONL run events, receipt, ledger path, exit status
```

`python -m codey agent --json` 现在走 `codey/headless_runner.py`。这条路径复用：

```text
TaskRunner
TaskRequest
Run Ledger
Managed Outputs
Provider fallback ordering
Permission Profiles
Change tracking and receipt generation
```

`--readonly` 映射到内部 `planning_readonly` profile，只给读/搜索/引用类工具，
不创建 Work Checkpoint、不收集 diff、不写 ProjectFacts。Project coding headless
第一版使用 no-op review callback，不静默多开 reviewer 模型。

### 为什么做

Headless runner 是用户可见能力，但它必须建立在清楚的内部事实模型上。否则它会复制一套 agent loop、prompt 拼接和事件格式，最终让架构更散。

### 对 Codey 的好处

- 自动化 smoke 和回归测试更容易。
- 用户可以在 CI 或脚本里运行受控 Codey 任务。
- A/B harness 可以从临时脚本变成正式能力。
- 未来 export/fork 可以基于同一份 ledger。

### 验收标准

- headless 输出 bounded JSONL，不原样 dump UI-only state、完整模型回复或完整命令日志。
- 可以跑只读 planning/explain 类任务，也可以跑受控 Project coding task。
- shell 审批在 headless 下默认拒绝，输出 `shell_rejected`，不等待用户。
- 不绕过 Codey 的项目边界、工具权限和 Research 显式开关。
- `agent --json` 走 headless；普通 `agent` 本版本暂不迁移。

### 暂不做

- 不做无人值守的全电脑 agent。
- 不默认允许安装、发布、删除或外部账号操作。
- 不把 headless 做成新的产品主入口。
- 不做 Research headless 自动搜索。
- 不做 shell 自动批准。
- 不改 UI。

## 0.2.33 - Project-local Config ✅ 已完成

### 做什么

新增克制的项目本地配置文件，用来声明项目级安全、验证和上下文偏好。
第一版已落地为 `.codey/config.json`，由 `codey/project_config.py` 严格解析。

第一版只考虑少量字段：

```text
verification commands
ignored paths
project_map_chars context budget hint
preferred provider policy (parsed/validated only; not consumed)
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
- provider preferred policy 本版只解析和校验，不覆盖用户明确选择。

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
