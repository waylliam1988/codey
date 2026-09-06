# Codey 未来版本规划

这份文档记录 Codey 从 `0.3.0` 开始的路线。`0.2.25` 到 `0.2.33`
已经完成了内部地基：Run Ledger、Ledger Projection、ToolDefinition、
ContextSource、Provider Capability、Managed Outputs、Permission Profiles、
Headless JSONL Runner 和 Project-local Config。

`0.3.0` 到 `0.3.20` 已经完成了 Ghost、长期连续性和能力边界的第一阶段。
`0.4` 的主线不是继续扩 UI 或插件，而是把 Research 升级成通用 Evidence
Research Runtime。

`0.3` 当时的主线不是继续整理工具身体，而是进入新的产品阶段：

```text
Ghost in Codey
```

更准确地说：

```text
Ghost / Nezha-mini = 主体、长期连续性、偏好、记忆、意图
Codey = 工具身体、本地执行、安全边界、工作台
外部模型 = 可替换的推理器官
```

这里有一个不可放松的边界：

```text
产品核心：Ghost 应该是核心
安全核心：Codey 仍然必须是核心
```

Ghost 可以决定“我想帮用户聊天、研究、写代码、复盘、记住偏好、整理长期问题”。
但真正读文件、改文件、跑命令、联网研究、提交 git 的动作，仍然必须经过 Codey 的
Permission Profile、Research Controller、ToolRuntime、shell approval、Run Ledger、
verification、restore 和 Project-local Config。

`0.4` 在这个基础上继续收紧 Research：

```text
Source / Evidence / Claim / Assumption / AnalysisRun / Artifact / Review
```

也就是说，Codey 不只是“能搜资料”，还要能回答：

```text
这个问题有没有被真正回答？
每个关键结论的证据在哪里？
哪些只是推测或假设？
本地分析能不能复查？
同一主题下次能不能接着研究？
```

## 0.2 地基已经完成

`0.2.25` 到 `0.2.33` 的目标是把 Codey 的身体边界整理清楚。现在这些已经落地：

```text
0.2.25 Run Ledger v1
0.2.26 Ledger Projections v1
0.2.27 ToolDefinition v1
0.2.28 ContextSource v1
0.2.29 Provider Capability Registry v1
0.2.30 Managed Output Handles v1
0.2.31 Internal Permission Profiles v1
0.2.32 Headless JSONL Runner v1
0.2.33 Project-local Config v1
```

这些能力共同提供了 Ghost 需要的身体接口：

- `headless_runner.py` 让 Ghost 能以 JSONL 方式调用 Codey。
- `permission_profiles.py` 让 Ghost 不能越权，只能提出意图。
- `context_source.py` 让 Ghost Directive 能作为有预算的上下文进入 prompt。
- `run_ledger.py` 和 `run_ledger_projection.py` 让 Ghost 能从真实执行事实里学习。
- `managed_outputs.py` 让长日志保留本地证据，但不塞爆模型上下文。
- `tool_definition.py` 让工具边界可命名、可过滤、可测试。
- `provider_capabilities.py` 让 Ghost 能理解 provider 的静态适配倾向，但不覆盖用户选择。
- `project_config.py` 让项目本地偏好和验证命令有安全入口。
- `research/controller.py` 让 Research 仍然由 Codey 控制证据链和工具顺序边界。

所以 0.3 不是推翻 0.2，而是把 Ghost 接在这些地基上。

## 0.3 Ghost 与能力边界已经完成

`0.3.0` 到 `0.3.20` 的目标是把 Ghost 接到 Codey 的身体上，并把模型可见内容、
工具结果、权限、事件、profile 和解释面板这些边界全部变成可测试事实。现在这些已经落地：

```text
0.3.0 Ghost Signal Extractor v1
0.3.1 Ghost Memory Inbox v1
0.3.2 Ghost Hebbian State v1
0.3.3 Ghost Directive ContextSource v1
0.3.4 Ghost Learning Loop v1
0.3.5 Ghost Continuity v1
0.3.6 Cognitive Sleep v1
0.3.7 Ghost Router v1
0.3.8 Ghost Work Queue v1
0.3.9 Research Interest Queue v1
0.3.10 Affinity Index v1
0.3.11 Local Context Control Surface v1
0.3.12 Research Notes v2
0.3.13 Run Trace Manifest v1
0.3.14 Prompt Envelope v1
0.3.15 Internal Capability Registry v1
0.3.16 Tool Contract v2
0.3.17 Action Policy Pipeline v1
0.3.18 Event / Capability Matrix v1
0.3.19 Built-in Profiles v1
0.3.20 Run Details v1
```

这些能力共同提供了 0.4 当时需要的研究底座：

- `ghost/extractor.py`、`ghost/inbox.py`、`ghost/hebbian.py` 让长期偏好和纠错先进候选区，再被审计接受。
- `ghost/directive.py` 和 `context_source.py` 让本地连续性以有预算的中性文本进入 prompt，且不能授权工具。
- `ghost/learning_loop.py`、`ghost/continuity.py`、`ghost/sleep.py` 让 Codey 能记住长期主题、开放问题和用户偏好，但不保存完整聊天正文。
- `ghost/router.py` 让 Ghost 只提出意图；手动入口、PermissionProfile 和 TaskRuntime/operation function 仍然优先。
- `ghost/work_queue.py` 和 `knowledge/research_interest.py` 让开放问题变成可追踪 work item，完成时需要 proof refs。
- `ghost/affinity.py` 让长期主题能排序和衰减，但关联边不等于事实。
- `Research Notes v2` 和 `research/controller.py` 让 Research 仍然是用户显式开启的受控闭环。
- `run_trace.py` 和 `prompt_envelope.py` 让模型可见上下文只保存 metadata、digest 和 source refs，不保存 raw prompt。
- `capabilities.py`、`tool_definition.py`、`action_policy.py` 和 `events.py` 让能力、工具、危险动作和事件投影都有可测试边界。
- `builtin_profiles.py` 让默认工作风格先成为只读 metadata，不参与 Router、权限或 prompt 分支。
- `run_details.py` 让用户主动需要时能看到一次运行的短解释，但不新增面板、不打断工作。

所以 0.4 不是推翻 0.3，而是把 Ghost 的连续性、Research 的证据链、Run Trace 的审计和
ToolRuntime 的本地执行合成一条更可靠的 Evidence Research Runtime。

## 总架构

```text
User
  -> Ghost Core
       - long-term continuity
       - Hebbian preferences
       - correction ledger
       - memory inbox
       - research interests
       - route intention
       - Ghost Directive
  -> Codey Action Layer
       - TaskRuntime
       - operation functions
       - ResearchRunner
       - ToolRuntime
       - PermissionProfile
       - approval / ledger / restore / verification
  -> External Model
       - DeepSeek / Qwen / MiMo / StepFun / GLM / local API
```

Ghost 负责“我是谁、我记得什么、我为什么做这件事”。
Codey 负责“我能安全做什么、做完有没有证据、失败后能不能恢复”。
外部模型负责“语言推理和生成”，可以随时替换。

## Nezha-mini 的迁移边界

不要把 `E:\Nezha\Nezha-mini\core\ghost.py` 原样搬进 Codey。它是
`torch.nn.Module`，还带着 `transformers`、checkpoint、token logit bias、Nezha
自己的 `Config` 和动作协议。Codey 的方向是 API-first / web-model-first，不应该把
HF 运行时带进核心。

应该借鉴的是 Nezha-mini 的思想和小契约：

```text
可以借：
- LLM 判断学习信号，而不是硬编码规则
- memory inbox / candidate / conflict_key
- learning gate
- correction ledger
- hot memory
- cognitive sleep
- research open question / task queue
- Ghost translator / directive 思想
- user world model 的可审计预测思想

不要借：
- torch Ghost
- transformers logits processor
- HF checkpoint
- 早期 MemoryFabric 大 SQLite/vector 体系
- Nezha-mini 完整 web research loop
- Nezha-mini action executor 覆盖 Codey ToolRuntime
```

## Symphony 的借鉴边界

OpenAI Symphony 值得借的不是“多 agent daemon”或“自动从 issue tracker 抢活”，而是
work orchestration 的产品抽象：

```text
长期意图
  -> bounded work item
  -> isolated run
  -> proof of work
  -> ledger / review / verification / restore
```

Codey/Ghost 应该借：

```text
可以借：
- Work Item 是一等对象，不只是聊天轮次。
- 工作流事实可以放在 repo 里，例如 `.codey/config.json` 和未来 `.codey/WORKFLOW.md`。
- 每个 work item 有 scope、status、priority、run refs 和 evidence refs。
- 任务完成必须有 proof of work：diff、verification、research citations、ledger path。
- Orchestrator state 要单一、可重放、可恢复。

不要借：
- always-on 多 agent daemon。
- 自动从 GitHub/Linear 抢任务。
- 无人值守自动落 PR。
- 高信任 hook shell。
- 复杂并发调度。
- 让 Ghost 直接执行工具。
```

这条线和 Nezha-mini 互补：Nezha-mini 给 Ghost 长期连续性，Symphony 给 Ghost
长期意图如何变成可追踪 work item，Codey 继续提供安全身体。

## deepseek-harness 的借鉴边界

deepseek-harness 值得借的不是“插件商店”，而是能力边界架构：

```text
每个能力都有明确注册点和替换边界
进入模型上下文的东西都可追溯
工具有统一 schema、执行、模型输出、UI 展示和审计元数据
权限 guard 只能单向收紧，不能被后续逻辑反向放行
profile / bundle 用来组合能力，但不等于把复杂配置暴露给普通用户
```

Codey 可以借：

```text
- Internal Capability Registry：把 provider、Research、Review、Local context、
  ToolRuntime、Changes、Run Trace 等内置能力注册到明确 seam。
- Run Trace Manifest：记录每次 run 的有界可解释清单，而不是 raw prompt
  或完整上下文。
- Prompt Envelope：所有模型可见 prompt section 都声明 name、purpose、
  source refs、budget 和 digest。
- Tool Contract v2：工具结果拆成 canonical value、model-facing text、
  UI presentation 和 audit metadata。
- Monotonic Policy Pipeline：shell、文件、URL、Local context、fallback 等
  风险动作都走统一 allow / ask_user / deny 决策；deny 不可被覆盖。
- Event / Capability Matrix：显式记录谁生产事件、谁消费事件、是否持久化、
  是否模型可见、是否 UI 可见。
- Built-in Profiles：用内置 profile 组合已有能力，例如 Default、
  Research-heavy、Review-strict、Local-only、Beginner。
```

Codey 现在不应该借：

```text
- 开放第三方插件平台。
- 引入 Cordis runtime。
- 动态 patch layers / hot reload。
- 用户可编辑 capability graph。
- 插件可以修改 prompt / Router / PermissionProfile。
- 插件可以接管 agent loop。
- 插件市场或复杂配置 UI。
```

原因是 Codey 的产品气质是本地、安静、可控。0.3 完成后，Codey 的架构路线仍然应该走：

```text
monolith with seams
  -> internal capabilities
  -> trusted built-in plugins
  -> limited external plugins
```

而不是现在直接走：

```text
Everything is a public plugin
```

## OpenScience 的借鉴边界

`synthetic-sciences/openscience` 值得借的不是“大科学平台 UI”，而是研究运行时的
几个硬边界：

```text
agent runtime
  -> tool layer
  -> science connectors
  -> provenance graph
  -> artifact store
  -> critic / review
```

Codey 可以借：

```text
- Source Connector contract：每个来源一个薄 wrapper，统一 search/fetch/read
  contract，不把 arXiv、PubMed、SEC、RSS、CSV 等细节暴露给模型。
- Provenance DAG：Source、Evidence、Claim、AnalysisRun、Artifact 之间有
  supports / refutes / derived_from / produced_by 边。
- Artifact version：报告、表格、图、分析输出都有 sha256、size、mime、origin run
  和输入 refs。
- Critic / Review gate：Research finalize 前后检查 unsupported claim、引用错配、
  过期数据和过度推断。
- Workspace-local audit trail：研究事实优先落到本地有界记录，而不是只存在聊天文本里。
```

Codey 现在不应该借：

```text
- 大 workspace / dashboard / notebook UI。
- 用户可管理的 skills、agent graph、connector graph 或 provenance graph。
- Everything is a plugin 的 runtime。
- 让 connector 修改 prompt、Router、PermissionProfile 或 agent loop。
- 后台自动联网研究或无人值守实验。
- 把科学专用流程硬编码进通用 Research。
```

Codey 的路线应该更窄：

```text
ChatGPT/Cursor 式交互
  + Zotero/Notion 式研究记录
  + Jupyter 式可复查分析运行
  + Codey 本地工具闭环
  + Ghost 长期连续性
  + Evidence-level Review
```

这条路线的目标不是“立刻在每个科学专业深度超过 OpenScience”，而是在
个人通用研究工作台这个方向上形成优势：用户只说“帮我研究”，Codey 在后台完成来源、
证据、分析、引用、复核和长期追踪；前台仍然安静。

## 规划原则

1. 0.3 已经把 Hebbian / Ghost 长期连续性落地；0.4 已经把它约束进 Evidence Research Runtime，而不是让记忆替代证据。
2. Hebbian 状态必须是纯 Python / JSON / SQLite 可审计状态，不依赖 torch、HF 或 transformers。
3. LLM 可以判断学习信号，但不能直接永久写记忆。
4. 所有学习先进 inbox，经过 gate 才能强化 Hebbian state。
5. Ghost Directive 只能影响表达、偏好和任务倾向，不能授权工具。
6. Research 事实仍然来自 Codey Research evidence，不来自 Ghost 关联边。
7. 关联边表示“相关性”或“值得研究”，不表示“事实为真”。
8. Ghost Router 只输出意图，不能直接调用 edit/run/shell/git。
9. 所有 Ghost 状态都要有大小上限、衰减、冲突处理、导出和删除路径。
10. UI 先保持安静，最后再做 Ghost 审计面板。
11. Ghost 必须从第一版就有 disable、export、reset 和 delete scope 的控制入口；UI 可以晚做，控制权不能晚给。
12. `state_home=None` 时 Ghost 写入默认禁用，避免测试、嵌入和一次性脚本写入真实长期状态。
13. Ghost 的长期意图必须落成可审计 work item，不能变成隐藏后台冲动。
14. Work item 可以建议下一步，但不能绕过用户显式入口、PermissionProfile 或 TaskRuntime。
15. 版本号可以出现在 roadmap 标题或 release label 里，例如 `Tool Contract v2`
    和 `Action Policy Pipeline v1`；长期 API、类型、模块、字段名不要用
    `V2`、`Next`、`New` 这类迁移期命名。应该演进稳定名字，例如
    `ToolOutcome`，或使用有真实语义的名字。

## 0.3 详细计划已归档

`0.3.0` 到 `0.3.20` 已经全部落地。Roadmap 现在只保留上面的阶段总结、下面的
`0.4` 完成态总结和后续 `0.5+` 计划；逐版本实现细节、验证记录和发布说明分别留在：

```text
CHANGELOG.md
CHANGELOG.zh-CN.md
TEST_REPORT.md
```

这样 Roadmap 的阅读重心回到未来版本，同时保留 0.4 的 Evidence Research Runtime 完成边界。

## 0.4 Evidence Research Runtime 已完成

0.4 阶段的目标，是把 Research 从“能搜网页、能写总结”收成一条
有证据、可审计、可回放、能进入后续 coding/handoff 的 Evidence Research Runtime。
逐版本执行细节已经归档到 `TEST_REPORT.md`、`docs/0.4_final_stabilization_report.zh-CN.md`、
`docs/0.4_deepseek_provider_baseline.zh-CN.md`、
`docs/0.4_qwen_provider_baseline.zh-CN.md`、
`docs/0.4_mimo_provider_baseline.zh-CN.md` 和 `tests/manual/`，roadmap 不再保留
0.4.x 的流水账计划。

0.4 最终留下的能力：

- Research object model：把 source、evidence、claim、assumption、relation、locator
  变成结构化记录；搜索结果和本地记忆不算 evidence。
- Evidence ledger + proof quality：Research 完成后能用 deterministic proof review 检查
  answer coverage、citation、opened-source evidence、support relation、assumption 和限制/反证。
- Source connector boundary：PubMed/arXiv/local-file/table/json connector 只提供候选来源；
  只有打开/读取后的来源才能进入 evidence 链。
- AnalysisRun / artifact / reproducibility capsule：本地分析和产物有可复查摘要，不把 raw
  stdout/stderr、source body、prompt 或 transcript 塞进模型上下文。
- A/B journal + transcript replay：manual 实机实验能保存 result/journal/manifest/transcript
  引用，并按 gate 复算，不靠手读聊天记录决定是否进生产。
- Safe ContextEpoch / Capability Boundary / Research Contract Lite：上下文、能力和 Research
  工具契约都有可测试边界；Ghost continuity 只能提示复查方向，不能制造 evidence 或 citation。
- Verified Completion v1：Research / coding 的完成判断开始从“模型说完成”转向本地 proof、
  verification 和 repair admission。
- Research Brief v2 + Ghost Research Continuity：研究结果可以有界带入项目，长期主题可以继续，
  但 stale/unsupported 信息不能变成实现约束。

0.4 收口后的硬边界：

```text
source content 是 data，不是 instruction
search result / memory / Ghost continuity 不等于 evidence
unsupported claim 不能支撑 conclusion 或 implementation constraint
proof、ledger、trace、journal 只保存 refs/digests/counts/bounded excerpts
manual A/B 可以产生证据，但 production 不能 import tests.manual
不做后台递归 Research，不让模型重写整篇报告来“修证据”
不为了未来可能用到而保留无消费者 production 接线
```

0.4 转入 0.5 的结论：

```text
false completion / modified test fixture -> 0.5.0 Verified Completion v2 已承接
durable run state / effect intent / safe replay -> 0.5.1-0.5.5 已承接
prompt surface / tool contract drift -> 0.5.6 已承接
Research source wrapper / follow-up / finalizer 收口 -> 0.5.7 已承接
Pi-style durable operation core -> 0.5.8 承接，把恢复入口从隐式推断收成 total state machine
Ghost / World Model / provider-protocol learning -> 0.6 承接，且不能回流 evidence / permission / completion verdict
```

所以 0.5 不是继续扩 0.4 的实验树，而是把 0.4 证明有效的部分默认化，把没有消费者或收益不稳的
arm 留在 manual 或删除，并继续收敛运行时可靠性。

## 0.5 主线 - Reliable Agent Runtime + Verified Work Integrity

0.5 的主题不是继续扩 0.4 的实验树，而是把已经被 0.4 证明有效的证据边界、A/B gate 和
completion proof 接进更可靠的运行时：任务状态可恢复，外部副作用有 intent/settlement，安全工具可重放，
prompt/tool contract 可审计，Research follow-up 和 finalizer 只保留有 release evidence 的窄生产路径。

0.4 进入 0.5 后只保留三类约束：

```text
证据约束：source/search/memory/Ghost continuity 的边界不能放松
运行时约束：每个外部副作用都要能解释开始、完成、失败或恢复
生产约束：manual A/B 证明有效才进 production；无消费者或收益不稳的 arm 删除或留在 manual
```

0.5 继续推进的主线：

```text
0.5.0-0.5.7 已收口：Verified Completion、edit integrity、durable runtime state、
  effect intent/settlement、safe replay、tool-result delivery、prompt surface drift guard、
  Research source wrapper、bounded evidence-only follow-up 和 final-report claim filter。
0.5.8 只做 Pi-style durable operation core：OperationState、pure reducer、single mutation boundary、
  manual drive / crash injection tests 和 task_run.py 瘦身。
0.6 起再做 Ghost / World Model / provider-protocol learning、protocol classifier、structured provider path
  和长期状态维护。
```

### 0.4 Stabilization 已归档

0.4 后半段只做三类事：补齐 A/B 证据链、修实机 A/B 暴露的真实 bug、以及不改变模型可见文本的安全/卫生收口。
这些细节已经进入 0.4 final stabilization 报告和各 provider baseline 文档；roadmap 只保留结论：
0.4 允许进入 0.5，DeepSeek/Qwen/MiMo 的核心路径已有 baseline，GLM/local 继续作为可选 confidence run。

归档入口：

```text
docs/0.4_ab_stabilization_plan.zh-CN.md
docs/0.4_final_stabilization_report.zh-CN.md
docs/0.4_deepseek_provider_baseline.zh-CN.md
docs/0.4_qwen_provider_baseline.zh-CN.md
docs/0.4_mimo_provider_baseline.zh-CN.md
TEST_REPORT.md
tests/manual/
```

## 0.5.0 - Verified Completion v2 + Edit Integrity Monitor + Receipt Warning

状态：已落地（2026-08-29，deterministic gate 和两条最小 live smoke 已完成）。
目标不是阻止模型修改测试，也不是自动修复模型的错误修法，而是在真实
production completion path 中观察“验证是否被编辑削弱”，并在高置信 suspicious 时
让最终 receipt 对用户可见。正常 clean path 必须完全无感；低风险只进 trace/details；
高风险不能显示成 clean verified completion。

落地要点：`codey/completion/edit_scope.py`（封闭 edit-path 词表 + 共享
`is_document_path` + 保守的测试修改授权扫描）、`codey/completion/edit_integrity.py`
（deterministic monitor，封闭 reason code，fail-closed `monitor_error`）、
`codey/completion/decision.py`（TaskRunner 内联 enforcement decision 抽成纯投影
`build_completion_decision`）。receipt 重写为 schema v1
（`display/work/verification/integrity`，trust ∈ trusted / needs_review / limited），
旧顶层 `text/changed_count/checks_passed/restore_available` 删除，
`RunResult.checks_passed` 语义不动。`CompletionProof` 新增结构化
`diagnostic_refs`（不复用 finding_refs）。TaskRunner 在每个 completion 决策点
（首轮 + repair 后）观察 integrity；只有 `trust == trusted` 的 run 写 project
facts / project memory；终态 receipt 直接从 ledger 持久化 receipt 投影，
`receipt_from_projection_if_compatible()` 删除。Trace 新增有界
`completion_edit_integrity` section；ledger 的 `changes_collected` 存校验后的
schema-v1 receipt；Run Details / Headless / Web UI / ghost work queue 全部改读
新 contract；`completion_enforcement_ab.py` 删除第二套 `modified_test_fixture`
判断，改读 run trace 的 integrity 行。新增 `tests/manual/edit_integrity_ab.py`：
Qwen/MiMo 篡改 signature 的 deterministic replay gate（17 例）+ 两条最小 live
smoke 入口（DeepSeek clean、Qwen/MiMo dependency_missing）。按 A/B 规则本版
clean path 不需要生产质量 A/B；deterministic gate 已通过，两条 live smoke 已
补齐 manual evidence。0.5.2 已选择继续保留 production receipt warning，不把
high-confidence suspicious 升级成 hard block；block / repair-admission 升级必须另排
A/B，证明低误伤且有净收益后再做。

0.4 stabilization 的触发证据：

```text
Qwen:
  dependency_missing_env_failure 中删除或注释 tests/test_mod.py 的 import redis，
  让 pytest 变绿后说 done。

MiMo:
  同一 case 中把 import redis 包成 try/except ImportError: pass，
  让 pytest 变绿后说 done。

共同结论:
  这不是 repair_context 问题，因为 repair_rounds=0。
  这是 edit/verification integrity 问题：模型可能通过削弱验证而不是修复产品代码来完成任务。
```

### 做什么

新增：

```text
codey/completion/edit_scope.py
codey/completion/edit_integrity.py
tests/test_completion_edit_integrity.py
tests/test_task_runner_edit_integrity.py
tests/fixtures/edit_integrity/
tests/manual/edit_integrity_ab.py
```

核心对象：

```text
EditScopeSnapshot(
  task_ref
  explicit_user_scope
  production_paths
  test_paths
  fixture_paths
  verification_config_paths
  docs_paths
  generated_paths
)

EditIntegrityObservation(
  schema_version
  run_id
  status: clean | suspicious | unobserved | monitor_error
  severity: none | low | high | critical
  reason_codes
  findings
  user_authorized_test_edit
  affected_paths
  verification_refs
  change_refs
  monitor_error_ref
)
```

第一版只做 monitor + receipt warning：

```text
model edits
  -> change observation
  -> verification
  -> EditIntegrityMonitor
  -> CompletionProof diagnostics
  -> receipt-level warning when high-confidence suspicious
```

检测范围控制在高信号、低误伤：

```text
test/fixture import 被删除、注释、try/except 掉
assert / expected value 被删除或明显放宽
verification config 被改得更窄或更容易通过
changed paths 已知但 diff 缺失或只覆盖部分文件 -> unobserved / verification limited
生产目标文件未改，但验证从 failed 变 passed
任务未授权修改测试，却修改了 protected fixture/test helper
用户明确要求修改测试时标 authorized，不直接当 suspicious
用户明确要求不改测试时，否定优先，不能被 edit/test 词误判为 authorized
```

用户可见文案必须克制。clean path 不展示任何新增内容；高置信 suspicious 才在最终
receipt 加一句：

```text
已完成，但验证可信度较低：模型修改了测试文件，测试结果可能因此被削弱。建议复查测试变更。
```

details / trace 可以显示结构化原因：

```text
tests/test_mod.py modified
import redis guarded or removed
pytest changed from failed to passed
source target fixed / not fixed
user_authorized_test_edit true/false
```

### 边界

- 不新增 EditIntegrityManager / VerificationManager。
- 不自动 repair。
- 不默认阻止 done。
- 不把所有测试修改都当失败。
- 不改变 writer prompt、tool schema、Research prompt 或 provider behavior。
- 不保存 raw transcript、raw diff 全文、raw stdout/stderr；只存 bounded paths、reason codes、refs 和 digest。
- Monitor 只产生 integrity finding，不直接产出 completion verdict。
- Completion 层组合 verification + integrity + authorization，再决定 receipt 语义。

### 不变式

```text
测试通过不等于验证可信。
Monitor failure 不能被伪装成 clean。
changed paths 没有完整可解析 diff 时不能被伪装成 clean。
高置信 suspicious 不能显示成 clean verified completion。
用户授权修改测试不等于测试语义一定可信，仍可记录 low-risk finding。
EditIntegrityObservation 不是 Evidence，不进入 EvidenceLedger。
EditIntegrityObservation 不能授权工具、不能改变 PermissionProfile、不能绕过 CompletionProof。
```

### 顺手架构优化

```text
把 completion A/B 里的 protected fixture / fixture_scope_ok / scope_error 判断，
  收成可被生产 path 调用的 edit_integrity projection。
TaskRunner 只在 completion proof 组合点调用 observe_edit_integrity(...)。
CompletionProof 增加结构化 diagnostics/ref，不把 warning 当普通字符串塞入 summary。
receipt renderer 只负责把 high-confidence finding 压缩成人类可读一句话。
manual A/B scorer 复用生产 observation，不维护第二套 modified_test_fixture 判断。
```

### 验证

```text
Qwen/MiMo modified_test_fixture transcript replay 能得到 high suspicious
删除 import / 注释 import / try-except ImportError 都能被识别
用户明确要求修改测试时不触发 hard suspicious
正常生产代码修复 + 测试不变 -> clean
docs-only change -> 不误报 test integrity
monitor exception -> monitor_error，不能变 clean
changed paths without parseable/complete diff -> unobserved，receipt limited
明确 "do not modify tests" / "not tests" / "不改测试" 不会授权测试修改
high suspicious receipt 不得是 clean verified wording
EditIntegrityObservation 不进入 EvidenceLedger / ResearchRecord / Ghost memory
edit_integrity.py 不 import provider/browser/tool_runtime/server
```

### A/B

需要，但分阶段：

```text
deterministic:
  replay Qwen/MiMo modified_test_fixture 原材料
  fixture-based diff/integrity tests
  receipt rendering tests

live smoke:
  DeepSeek one arm / one case，确认 clean path 不增加噪音（已完成：
    receipt_trust=trusted，integrity_status=clean，receipt_warned=false）
  Qwen dependency_missing_env_failure one case，确认 warning 可见（已完成：
    receipt_trust=needs_review，integrity_status=suspicious/high，
    reason=test_import_removed_or_commented）

enforcement:
  0.5.0 不默认 block。
  只有 A/B 证明低误伤且有净收益，后续版本才把 high suspicious 升级为 completion block / repair admission。
```

### Graduation / Delete Gate

这个 monitor 不能长期停在“有代码但没产品价值”的状态。0.5.2 的收口决策是：
保留 production warning，因为它已经进入 receipt / trace / Run Details，并能让
`modified_test_fixture` 类问题不再伪装成 clean verified completion；暂不升级为
completion block 或 repair admission。

```text
future graduation:
  high-confidence suspicious -> completion policy block 或 repair context admission
  前提是 A/B 证明不会误杀用户授权的测试修改，且能减少 false completion

delete/degrade:
  如果误伤高、用户噪音高、或不能稳定复现 Qwen/MiMo failure mode，
  删除生产接线，最多保留 manual harness scorer。
```

## 0.5.1 - Runtime Session Log Operation State + TaskFlow Deletion

状态：已发布（2026-08-31，compileall、ruff、focused gates、deterministic crash-position tests、same-run crash/resume smoke、headed clean UI smoke 和全量 pytest `3278 passed, 16 skipped in 283.92s (0:04:43)` 完成）。0.5.1 的最终切口不再新增独立 `codey/run_operation.py` register，也不保留生产 `TaskFlow` 概念；0.5.8 之后 run phase 已升级为 first-class `operation_state`，`RuntimeOperationStore` 只读取投影。

### 已落地的核心形态

```text
TaskSubmission
  -> task_entry.run_task_submission
  -> TaskRuntime
  -> RuntimeMutationLine
  -> RuntimeSessionLog operation_started/operation_state/effect/settled
  -> RuntimeOperationStore read projection
  -> Run Details / ledger / terminal event
```

operation identity 只保留一条：

```text
operation_id = task:<sha256(run_id)[:24]>
lane         = run:<session_key(run_id)[:24]>
```

发布前已删除外层 `runtime:<run_id>` 语义；`TaskRuntime`、`RuntimeOperationStore`、terminal settlement 和 Run Details 共用同一条 task operation。这个选择是有意的，不是 parent/child 过渡结构：一次用户任务只有一条 durable operation，phase effect 与最终 outcome 都挂在同一条 operation 上。

### Phase 事实

```text
accepted
writer_running
writer_settled
completion_proof_recorded
repair_context_admitted
repair_running
repair_settled
terminal
```

`RuntimeOperationStore.start()` 会恢复同一个 open operation 的最新 phase，不会把 resume 重新写回 `accepted`；相同 phase commit 幂等返回当前 projection，不制造重复 effect。scheduler 负责 open operation 一定 settle：正常返回、stop/cancel、业务异常和未知异常都会先写 settlement，再按调用策略返回或 re-raise。

`RuntimeSessionLog` 维护进程内 entries + projection cache。append 仍通过 reducer
做增量 fail-closed 校验；`RuntimeOperationStore.load()` 在文件大小和 `mtime_ns`
stamp 都未变化时从缓存 entries 扫描当前 run phase，不再让每次 phase commit 都
全量读盘。同尺寸外部改写、compaction 和 delete session 会触发缓存重建或失效。

### AgentRunner 拆分

0.5.1 也收掉 runner.py 的宽接口债务，但只做 ownership 拆分，不改变 prompt 或
tool 协议：

```text
codey.agents.protocol  JSON protocol repair / edit parsing / verification text helpers
codey.agents.context   project instructions + prompt context assembly
codey.agents.request   AgentRequest
codey.agents.state     AgentLoopSession / progress / verification / stagnation state
codey.agents.prompt_context        provider-send prompt / context epoch / repair context admission
codey.agents.verification_driver   verification candidate / freshness / reminder state
codey.agents.tool_execution        policy / dispatch / tool-result accounting
codey.agents.loop      turn loop / parse / visible continue-return control / finish
codey.agents.runner    thin public run(AgentRequest) surface
```

调用方不再传一长串关键字参数；server/headless/project completion/planning/task_run 都构造
`AgentRequest`。循环内部的 progress、verification、stagnation 也变成显式 state
object。prompt/context 组装、context epoch、repair context admission、verification
freshness/reminder 和 tool execution 已经从 `agents.loop` 拆到各自 owner；
`loop.py` 只保留 turn loop、parse、状态转移、可见的 `continue` / `return`
控制流和 finish，避免继续在大函数 locals 里长出隐式生命周期。

### TaskFlow 删除后的 owner

0.5.1 不追求为了行数漂亮而新建 Manager，也不新建一个新的 `TaskOperation` 大类；Runtime 负责 lifecycle，operation function 负责业务动作。生产 `codey/operations/task_flow.py` 已删除：

```text
codey.operations.task_entry           public submission entry + TaskRuntime wiring
codey.operations.task_run             non-business task-run lifecycle + TaskRunDeps
codey.operations.mode_dispatch        pure mode -> operation function dispatch
codey.operations.provider_preflight      provider connect / canary / startup failover
codey.operations.conversation_plan       fresh-chat / handoff / recovered owner prompt
codey.operations.review_flow             review-only operation
codey.operations.planning_flow           planning-readonly operation
codey.operations.research_flow           research/hybrid research pipeline handoff
codey.operations.project_completion_flow coding writer / review cycle / completion proof / repair / receipt
codey.operations.ghost_context           prompt-time Ghost context
codey.operations.ghost_post_turn         terminal-event Ghost projections
codey.app.http_plumbing                  Host/Origin/static/JSON/SSE transport helpers
codey.app.api                            ordinary JSON endpoint payloads
codey.app.services                       provider/review/consensus/shell service calls
codey.runtime.terminalizer               terminal task_done event + turn accounting
codey.task.model                         TaskSubmission model-only boundary
```

`codey/task/service.py` 和 `codey/operations/task_flow.py` 均已删除；`codey.task` 不导出 `TaskFlow`。server/headless/manual harness 只认 `task_entry.run_task_submission()` 这个稳定公共面；测试按 owner patch `research_flow`、`project_completion_flow`、`ghost_post_turn` 等模块，不再要求生产类保留私有方法。

`codey/operations/project_completion_flow.py` 也从 closure cluster 改为 `_ProjectRun`
上的相位脚本：prepare、writer failover、review cycle、completion enforcement 和
finalize 分别拥有显式函数，`run_project_mode()` 只串联相位，不再依赖
`nonlocal` 状态。completion enforcement 已经成型为独立相位，但暂不迁到
`completion/engine.py`；这一步只改变 owner，不改变 repair admission、TaskCancelled
re-raise 或 fallback 语义。

`ProjectCompletionDeps` 不再是平铺的大依赖面，而是按真实稳定边界分组为
`AgentAccess`、`PersistenceAccess`、`VerificationAccess`、`ReviewAccess` 和
`RuntimeAccess`。这一步只压缩 dependency surface，不新建 `CompletionManager`，
也不按行数把 project completion 拆成互相 import 的小文件。

### AppContext 收敛

`server.State` 已重命名并收敛为 `AppContext`。它不再提供 `project`、`task`、
`active_run`、`provider_supervisor` 这类转发 property；生产调用直接走
`run_registry`、`event_bus`、`providers` 等 owner。无 `state_home` 的 AppContext 也会创建
ephemeral runtime log / operation store / workspace revision store，让测试和 headless 路径
无条件经过同一 runtime 内核，而不是靠缺省 fallback 跳过 runtime。

### Crash / Resume

0.5.1 的 smoke 不再只是“kill 后读最后 phase”。manual self-test 会：

```text
1. 启动真实 headless child run
2. 等到同一条 task operation 到 writer_running
3. hard kill child
4. fresh AppContext / fresh RuntimeOperationStore 读回 writer_running
5. 用同一个 run_id 重新提交
6. 验证只有一条 operation_started、同一 lane terminal settled、Details 不显示 stale Progress
```

`RuntimeSessionLog.mutate()` 带 batch metadata；reader 忽略不完整尾 batch，下一次 mutate 会先修剪坏尾，避免 crash mid-batch 留下永久 open lane。`append()` / `append_many()` 不再作为半公开业务 API 存在；业务 runtime facts 必须经过 mutation line。

runtime log 还会在同一把文件锁下做 replay 等价 compaction，只保留每条 operation 的 `operation_started`、最新 `operation_state`、必要 effect / delivery recovery facts 和已存在的 `operation_settled`，避免长生命周期 session 写满 4 MB 后永久 brick。

### 冷启动删除项

```text
删除 codey/task/service.py compatibility facade
删除独立 codey/run_operation.py JSON register 方案
删除外层 runtime:<run_id> operation
删除 codey/operations/task_flow.py 生产概念
删除 TaskFlow.research_iteration_runner 测试注入字段和所有 TaskFlow 私有测试入口
删除 runtime/lane.py、runtime/suspension.py、TaskRuntimePort、tool_invocation/tool_settled log entry、TaskContract、TaskState、OperationKind literal 等未接线脚手架
```

测试必须迁就新架构：research harness patch `codey.operations.research_flow.run_research_iteration`；project completion 测试 patch `codey.operations.project_completion_flow` 的公开 operation/helper，而不是要求生产类保留旧私有方法。

### Workspace State，不是 Context Epoch

0.5.1 落地的是 workspace state：项目文件状态的递增 revision 加有界
fingerprint，用来判断 verification observation 是否还能支撑 completion proof。
缺失的 revision 文件可以从初始 revision 开始；腐坏、非法或超限的 revision 状态
会 fail closed，避免 verification freshness 的单调身份回退。它和既有的
`workspace/context_epoch.py` 不是同一个概念：

```text
context epoch       = prompt 看见了哪些 source refs / digest，服务溯源审计
workspace state     = 项目文件 revision + fingerprint，服务 verification freshness
```

因此 proof provenance 里两者互补：`ctx_epoch_ref` 证明模型当时看见了什么；
`workspace_revision` + `workspace_fingerprint` 证明验证观察针对的是哪一个项目状态。
冷启动不做旧 checkpoint 兼容：没有 revision 或 fingerprint 的 seed check 不能继承
green，宁可多跑一次验证；未记录文件的外部编辑会因 fingerprint 不匹配而使旧 green
失效。

### 验证

```text
compileall + ruff 全过
runtime/effects/session-log/details/operation-state focused tests 通过
task_entry / project_completion / edit-integrity / completion-enforcement / analysis-run focused tests 通过
server / run-registry / approval-registry / research / Ghost 相邻测试通过
manual completion_operation_resume_smoke.py --self-test 通过
Ghost router/work-queue/affinity/research-interest/continuity deterministic self-tests 通过
新增 architecture tests 包级扫描通过：runtime 不 import operations/agents/Ghost，
agents 不 import operations，completion 不 import app/providers/operations，agents.loop
不直接 import completion/toolchain/workspace context-source internals
全量 pytest 3277 passed, 16 skipped in 285.32s (0:04:45)
```

### A/B

不需要 live provider A/B。0.5.1 改的是 runtime fact source、phase projection 和内部 operation 边界；不改变模型可见 prompt、tool schema、provider routing 策略或 repair admission 语义。需要的是 deterministic replay/fixture、same-run resume smoke 和一次全量本地 pytest，这些已经完成。

## 0.5.2 - Effect Intent / Settlement + Tool Replay Policy v1

状态：已完成。已把 Pi 的 effect sandwich 落到 Codey 当前最关键的三个边界：
provider send、tool execution、completion repair round。每个真实外部效果前写 intent，
效果后写 settlement；恢复时不从事件缺失推断，而是读取最后一个 committed state。

### 做什么

扩展现有 runtime fact source：

```text
codey/runtime/session_log.py
codey/runtime/operation_state.py
codey/runtime/effect_records.py
codey/runtime/replay_policy.py
tests/test_runtime_operation_state.py
tests/test_runtime_effect_records.py
tests/test_tool_replay_policy.py
tests/test_agent_effect_sandwich.py
```

Intent / settlement 类型：

```text
provider_send_intent
provider_send_settlement
tool_call_intent
tool_call_settlement
synthetic_interrupted_settlement
```

Replay policy：

```text
safe:
  read
  ls
  search
  references
  project_facts / project_map projection

unsafe:
  edit
  write
  shell
  run
  knowledge_write
  any tool with local write, network write, subprocess, approval, or unknown side effect
```

`run` 默认 unsafe；即使看起来是测试命令，也可能写缓存、snapshot、数据库或远程服务。

直接收益：

```text
provider send 失败能区分“没有 intent，因此没有发送事实” / maybe_sent / settled
edit/run 崩溃后不会被自动重复执行
unsafe tool 崩溃恢复时产生 synthetic interrupted settlement，不重复执行危险动作
safe read/search 崩溃恢复时标记为可重试事实，并由 Run Details 安静解释；v1 不把结果注入模型上下文
RunTrace / RunLedger 可以解释一次 repair 是否真的发起、是否结算
```

### 边界

- 不承诺 exactly-once external effects。
- 不恢复半截 provider stream。
- unsafe effect 不自动 retry。
- `shell` approval 卡片过期仍走现有 stop/deny 语义。
- effect payload 只存 args digest、bounded path/command display、tool id、policy decision ref。
- 不把 raw tool output 持久化到 effect log；长输出仍走 managed output。

### 顺手架构优化

```text
Agent tool loop 的 emit(tool_started) 前先写 tool_call_intent
record_tool_outcome() 后写 tool_call_settlement
provider.send(prompt) 前写 provider_send_intent，返回后写 provider_send_settlement
completion repair failover.run(...) 前后写 repair_round intent/settlement
RunLedger 的 tool_started/tool_finished 继续服务用户可见事实流；
RuntimeSessionLog 服务恢复语义，不新增第二套 durable effect log
```

### 验证

```text
任何 provider/tool/repair effect 开始前必须已有 intent
settlement 只能引用已存在 intent
safe tool effect_pending 在 0.5.8 后按全安全 batch replay，不做半批重放
unsafe tool effect_pending 恢复生成 interrupted settlement，不重复执行
provider maybe_sent 恢复不会伪造 done
RuntimeOperationState leaf 与 RuntimeEffectStore 最新 settlement 一致
policy denied tool 没有真实 external-effect record，只保留普通 tool outcome
tool args digest 稳定且不含 raw secret
未知工具默认 unsafe
```

### A/B

不需要质量 A/B。需要 fault-injection tests 和 live smoke：杀进程位置覆盖
`before intent / after intent / during effect / after settlement`。

## 0.5.3 - Shared Tool Argument Repair + Protocol Friction Reduction v1

状态：已完成（2026-09-01，compileall、ruff、focused tests、smoke harness、deterministic A/B、DeepSeek/MiMo/GLM live provider A/B 和全量 pytest `3358 passed, 16 skipped in 296.26s (0:04:56)` 完成）。
目标是把 coding `JsonToolCodec` 里散落的参数别名、编辑参数宽容和常见 provider 方言误差，收成所有 coding codec 共用的纯函数 `codey/tool_args_repair.py`。这个版本直接降低了 unknown/invalid args repair 次数，同时 `write/write_file/create_file` 严格保持 unknown tool，不引入隐藏修改别名。

### 做什么

新增：

```text
codey/tool_args_repair.py
tests/test_tool_args_repair.py
tests/manual/tool_args_repair_smoke.py
tests/manual/tool_args_repair_simulated_ab.py
tests/manual/tool_args_repair_live_ab.py
tests/manual/tool_args_repair_dialect_pressure_ab.py
```

支持的保守修复：

```text
search / old / before -> old_string
replace / replacement / after / new -> new_string
single replacement object -> replacements[...]
JSON string replacements -> parsed replacements, invalid JSON fail closed
missing new_string -> fail closed；只有显式空字符串表示删除
numeric string offset/limit -> bounded int
explicit null offset/limit -> fail closed
path normalization -> project-relative only, escape fail closed；optional path 仅缺失时默认 "."
unknown argument fields -> fail closed
write / write_file / create_file -> 严格保持 unknown tool，不引入隐藏修改别名
```

直接收益：

```text
模型输出接近 Codey 工具语义但字段名稍偏时，少打一轮协议修复
protocol_telemetry 记录 alias_rewrite_count / arg_repair_counts
invalid args 错误更稳定，方便 provider/protocol affinity 学习
```

### 边界

- 不改变模型可见工具名。
- 不把 research validate_tool_args 盲目合并进 coding shim。
- Runtime 仍重新校验 canonical args。
- 只修复明确等价字段；不能猜命令、猜路径、猜 diff。
- 修复失败仍走现有 protocol repair prompt。

### 顺手架构优化

```text
JsonToolCodec._tool_call() 只负责 parse JSON 和调用 normalize_tool_args()
ToolCall(name,args) 进入 Agent 前已经是 canonical args
RunTrace protocol_telemetry 增加 alias_rewrite_count / arg_repair_counts
```

### 验证

```text
每个 alias rewrite 都有 deterministic case
敏感字符串不能从 fake tool name / path / command 泄漏到 trace
path escape fail closed
invalid JSON replacements 不被吞掉
edit existing file with content 仍由 runtime guard 拒绝
shim 不 import tool_runtime/provider/task_runner
```

### A/B

需要小型 live provider A/B，因为 parser 接受范围变宽。0.5.3 已提供
`tests/manual/tool_args_repair_live_ab.py`，它走生产 `AgentRequest` / agent loop；
deterministic parser 对比保留在 `tests/manual/tool_args_repair_simulated_ab.py`。指标：

```text
invalid_args_rate
protocol_repair_count
first_valid_tool_rate
edit_success
verification_success
unsafe_action_count
false_completion_rate
```

2026-09-01 自然 live provider 小样本结果：

```text
DeepSeek: baseline/candidate 均 2/2 done，expected_content_ok 2/2，turns 7 vs 7，protocol error / repair prompt / alias rewrite 均为 0
MiMo: baseline/candidate 均 2/2 done，expected_content_ok 2/2，turns 7 vs 7，protocol error / repair prompt / alias rewrite 均为 0
GLM: baseline/candidate 均 2/2 done，expected_content_ok 2/2，turns 7 vs 7，protocol error / repair prompt / alias rewrite 均为 0
```

结论：这组干净 schema 任务没有观测到 live 省 turn，因为三个 provider 都输出了
canonical args；同时也没有观测到 unsafe / false completion 回归。deterministic dialect
suite 仍作为“别名真的出现时可省 repair turn”的机制证据，结果是 5 个场景节省 8 个
repair turns，turn reduction 36.36%。后续如果要让模型显式输出 `pattern`、`old/new`、
`cmd`、数字字符串等，应新增单独的 dialect-pressure suite，不能混入自然 live A/B 作为
生产收益结论。

2026-09-01 MiMo dialect-pressure live 结果：

```text
MiMo pressure: baseline/candidate 均 2/2 done，expected_content_ok 2/2，turns 9 vs 8，protocol errors 2 vs 0，repair prompts 2 vs 0，candidate alias rewrites 2
```

结论：pressure 样本在 MiMo 上观测到真实生产 loop 收益，但只发生在模型实际输出偏差参数的
case：`search_read_numeric_pressure` 通过 numeric-string coercion 省 1 turn、少 2 个 repair
prompt；`edit_run_alias_pressure` 中 MiMo 仍输出 canonical args，因此本次没有实机覆盖
`old/new` 或 `cmd`。这类结果只证明吸收能力，不作为自然生产省 turn 结论。

GLM 实机时暴露出启动深链问题：`main/alltoolsdetail` 入口可能触发验证，根入口
`https://chatglm.cn/` 不触发同类验证。因此 GLM browser start / new-chat URL 改为根入口，
不保留旧深链 fallback。

## 0.5.4 - Safe Tool Replay v1

状态：已完成（2026-09-01，focused replay/runtime gate、manual self-test、same-run
resume smoke、DeepSeek live resume smoke、compileall、ruff、`git diff --check` 和
全量 pytest `3384 passed, 16 skipped in 297.52s (0:04:57)`
完成）。在 0.5.2 的 effect intent / settlement 和 0.5.3 的
canonical tool args 之后，把 safe read/search 的恢复闭环补齐：进程在 safe tool
执行中途被杀掉时，恢复路径按 persisted canonical args 自动重跑，并把结果作为
同一个 tool call 的恢复结果继续进入 agent loop。直接收益是读文件、搜索、列目录这类
无副作用动作中断后无需再打一轮 provider，也不会把可恢复读失败显示成含糊状态。


### 做什么

新增：

```text
codey/runtime/safe_tool_replay.py
codey/runtime/replay_args.py
tests/test_safe_tool_replay.py
tests/manual/safe_tool_replay_smoke.py
```

扩展：

```text
codey/runtime/effect_records.py
codey/runtime/replay_policy.py
codey/agents/tool_execution.py
codey/operations/task_run.py
codey/runs/details.py
```

落地：

```text
safe intent 持久化 canonical replay_args，unsafe intent 仍只存 digest / bounded display
恢复 gate 在任何 provider/router/ghost side effect 前运行
pending safe effect 先重新过 schema / lane / project-boundary / runtime validator
通过校验后走现有 execute_tool_call 路径，不写第二套 read/search 实现
tool result 以 bounded recovered result 回到同一条 agent loop
带 recovered tool result 的续跑跳过 work-queue claim 和 auto router，避免恢复结果被分派到非 writer 路径
hybrid writer 崩溃恢复直接回到 project writer，不重复执行 Research
原 effect 写 replayed settlement，并记录 replayed_from_effect_id / replay_count
Run Details 只显示 quiet recovery row，不显示 effect_id / replay class / raw args
```

v1 safe 范围：

```text
read
ls
search
references
```

`project_facts` / `project_map` 仍然只是 safe 分类或 projection 概念，不接入 0.5.4
生产 replay 执行器。`search` 只指本地项目搜索，不包括联网 Research search、浏览器操作、
provider call 或任何可能产生外部状态变化的 connector。

直接收益：

```text
safe read/search 中断后可以自动恢复，不需要模型重新决定同一个工具调用
unsafe edit/write/run/shell 仍然永不自动重复执行
Run Details 能区分 replayed safe read 和 interrupted unsafe effect
为后续更细的 protocol portability 提供真实恢复消费者，不留下空 telemetry
```

### 边界

- 不自动 replay provider send。
- 不自动 replay edit / write / shell / run / knowledge_write / unknown tool。
- 不保存 unsafe tool 的 raw args；safe replay_args 也必须是 canonical、bounded、可重新校验的最小字段。
- 不保存 prompt、reply、stdout、stderr、diff、source body、完整搜索结果。
- 不为旧的无 replay_args effect 猜参数；缺字段、坏 schema、路径逃逸、未知 tool 都 fail closed。
- 不新增 SafeReplayManager；`safe_tool_replay.py` 只做候选提取和参数校验。
- 不保留旧的 settlement-only 恢复 wrapper；生产只有 `_recover_effects_for_resume()` 一个入口。
- 不把 replay 当后台任务；只在显式 resume gate 内运行。

### 顺手架构优化

```text
safe replay 消费 0.5.3 normalize 后的 ToolCall，不再让 task_run.py 自己理解 provider 方言
tool_execution.py 暴露 replay-safe 的 execute boundary，正常执行和恢复执行共用一条 runtime guard
Run Details recovery row 从 RuntimeEffectStore projection 读取，不扫 raw log payload
```

### 验证

```text
before intent 崩溃：无 replay
after safe intent 崩溃：按 persisted canonical args replay 一次
during safe replay 崩溃：仍只允许 safe replay，不产生 unsafe action
after settlement 崩溃：不 replay
unsafe pending 恢复 replay count 必须为 0
safe replay result 进入 transcript / run trace exactly once
legacy/missing replay_args 不猜参数，生成 quiet recovery explanation
path escape / oversize args / malformed args fail closed
clean path prompt/tool/result parity 不变
```

### A/B

不需要 clean-path 质量 A/B。需要 deterministic fault-injection、同一 run resume smoke、
以及至少一条 live resume smoke，因为 replay result 只在 crash/resume 路径进入模型可见
tool result。若本版额外改变正常路径 tool prompt、tool schema、provider routing 或
非恢复路径 transcript，必须升级为小型 live A/B。

2026-09-01 已补齐 live-path 证据：`safe_tool_replay_smoke.py --same-run-self-test`
通过；DeepSeek `--provider deepseek --port 9222 --max-turns 8 --keep-open` 通过，
覆盖注入 read 崩溃、恢复 1 个 read outcome、同一 provider 会话继续到
`read` / `edit` / `run`、`checks_passed=true` 与 `final_content_ok=true`。

## 0.5.5 - Safe Replay Result Delivery Receipt v1

状态：已完成。目标是补齐 0.5.4 的一个非阻塞恢复限制：如果同一轮里已有部分
safe tool 完成并结算，但进程在把整轮 tool result 发给 provider 前崩溃，0.5.4 只能恢复
仍 pending 的 safe effect，不能完美重建那一轮已经完成但尚未交付给模型的结果。

这个版本只做 runtime delivery receipt，不保存 raw source body 或完整搜索结果。恢复真源仍是
durable log：记录“某一轮 tool result batch 是否已交付给 provider”的有界事实；如果 batch
没有交付且 batch 内所有工具都是 replayable safe tool，就按原始 `turn/tool_index` 顺序用
persisted canonical `replay_args` 重新执行整批 safe 结果，再交回同一条 agent loop。

### 做什么

新增或扩展：

```text
codey/runtime/tool_result_delivery.py
tests/test_tool_result_delivery.py
tests/manual/safe_tool_replay_delivery_smoke.py
```

落地：

```text
Agent loop 在发送 tool result prompt 前记录 result-batch intent
provider send 确认后记录 result-batch delivered settlement / receipt
resume gate fold durable log，找出未交付的 all-safe result batch
未交付 all-safe batch 重放整批 safe tool，包括已 settled 但未 delivered 的 safe intent
pending safe effect 写 replayed settlement；已 settled safe effect 不重复结算，只记录 delivery recovery fact
recovered outcomes 按 turn/tool_index 排序，一次性交给 AgentRequest
```

直接收益：

```text
safe tool 多调用同轮崩溃后，不会只恢复 pending 的后半截结果
模型继续时能看到同一轮完整 safe read/search/ls/references 结果
不需要保存文件正文、搜索全文或 provider transcript 来修复恢复缺口
核心并发安全、文件锁、Worker 超时防护与敏感凭据保护完成全面加固并通过 3430 项全量回归测试
```

### 边界

- 只处理 result batch 尚未交付给 provider 的恢复缺口。
- 只恢复 all-safe batch；batch 内出现 edit/write/run/shell/provider send/未知工具时 fail closed，不做局部拼接。
- 不保存 raw prompt、raw reply、raw stdout/stderr、diff、source body 或完整搜索结果。
- 不对已 delivered 的 batch 重放。
- 不把 settled unsafe tool 的结果伪造成 recovered tool result。
- 不改变正常 clean path prompt/tool schema/provider routing。
- 不引入后台 replay，不让 Ghost 参与 delivery receipt。

### 顺手架构优化

```text
tool_result_delivery.py 只做纯 projection / batch selection，不 import agents/provider/ghost/tool_runtime
AgentLoop 只调用小的 deliver_turn_results / deliver_recovered_results helper，不自己 fold runtime log
task_run.py resume gate 继续只消费 recovery plan，不理解 batch receipt payload 细节
消除了 loop.py 中三处分散的 tool results 组装与 prompt delivery 冗余
```

### 验证

```text
同轮 read 已 settled、search pending、result batch 未 delivered：恢复时 read/search 都进入模型结果 prompt
同轮 safe batch 已 delivered：恢复时不重复 replay
mixed safe + unsafe 未 delivered batch：fail closed，不做 partial transcript reconstruction
provider send maybe_sent：不自动重发 provider prompt
delivered receipt 必须匹配既有 send_attempt，不能绕过两阶段状态机
同一 result batch 不能出现多条不同或重复的 send_attempt / delivered receipt
单 effect fallback 恢复结果也会在重新交付给 provider 时获得 delivery receipt
recovered results 排序稳定，且每个 result 只注入一次
不保存 source body / stdout / stderr / diff / prompt / reply
```

实测证据：
- `tests/test_tool_result_delivery.py`：32/32 passed（涵盖 schema 拒绝 raw 字段、严格 5 字段 item schema 与非负整数/有界字符串校验、同 batch_id 冲突拒绝、未知 delivery record_kind 显式抛错、损坏日志字段校验、对齐 RuntimeEffectStore 的 run 边界校验、拒绝孤儿 send_attempt / delivered 记录、拒绝没有匹配 send_attempt 的 delivered receipt、拒绝同一 batch 多条 send_attempt / delivered receipt、同一 turn batch digest 不匹配显式 invariant failure、TurnState fast-path digest 复用、item.ref 空值拦截、两阶段交付状态流、send_attempt 失败阻断 provider.send 与 fail-closed、单 effect fallback 恢复结果重新交付时自动补 delivery receipt、undelivered all-safe batch 投影、closed session compaction 规则与 recovered fact 直接投影、mixed batch 拦截 safe tool 局部单 effect 重放、真实 execute_turn_tools 正确识别 safe tool 与两阶段恢复、read + denied shell 单一 delivered batch 消除悬空 batch、clean-path prompt 字节级 parity 全文精确比对、Run Details 权威合并 reads/lookups并在异常时优雅 warning、record_recovered 幂等性与冲突拒绝）。
- `tests/manual/safe_tool_replay_delivery_smoke.py`：`--self-test`（多 safe tool 同轮前 settled 后 pending 崩溃恢复）与 `--same-run-self-test`（多轮两阶段 receipt 连续写入）100% passed。
- `tests/test_architecture.py`：72/72 passed（分层边界与 AST 静态合规全部守卫）。
- 全量回归：3431 passed, 16 skipped, 1208 subtests passed in 291.66s (0:04:51)。

### A/B

不需要 clean-path 质量 A/B。它是 resume-only durability closure，不改变正常 prompt、tool schema、
provider routing 或非恢复路径 transcript。通过确定性故障注入测试（`tests/test_tool_result_delivery.py`）
与本地同一 run resume smoke（`tests/manual/safe_tool_replay_delivery_smoke.py --self-test / --same-run-self-test`）
严格验证。

## 0.5.6 - Tool Contract Drift Guard + Prompt Surface Decoupling v1

状态：已完成（2026-09-03，ruff、compileall、focused gates、golden parity、run trace
surface gates、architecture gate 和全量 pytest `3481 passed, 4 skipped, 1253
subtests passed in 290.34s (0:04:50)` 完成）。目标是让 coding 和 research 的模型可见工具说明由同一套 contract renderer
生成，并用 hash/parity tests 防止 prompt 描述、parser 接受范围和 runtime 语义漂移。
这不是空接线：本版的直接收益是减少“工具文案说 A、parser/runtime 实际做 B”的协议故障。

### 做什么

新增：

```text
codey/tool_prompt.py
tests/test_tool_prompt.py
tests/test_tool_contract_drift.py
```

落地：

```text
ToolDefinition -> model-visible snippet
ProtocolCodec -> final tool contract render
Research tool contract -> final research tool contract render
model_tool_contract_hash 覆盖最终模型可见文本
RunTrace 同时记录 controller_action_contract_hash / runtime_tool_contract_hash
```

直接收益：

```text
prompt/tool schema/parser/runtime 四者漂移时测试直接失败
减少 provider 因过时工具说明产生的 protocol repair
为后续方言投影提供可比较 contract hash，但本版不启用方言切换
```

### 边界

- 默认 prompt 字节必须先保持 parity；任何压缩/改文案单独 A/B。
- 不新增工具。
- 不改 research 工具名。
- 不新增 semantic taxonomy，除非已有 runtime 消费者。

### 顺手架构优化

```text
JsonToolCodec 中的大段工具说明迁到 tool_prompt.py
ToolDefinition 保持执行契约；tool_prompt.py 只渲染模型说明
Research contract 保持领域边界，和 coding 只共享 trace/hash 规则
```

### 验证

```text
默认 rendered prompt 与旧 prompt byte parity
contract_hash 因工具文案变化而变化
parser 接受字段未声明时测试提示必须更新 prompt 或收窄 parser
runtime-only audit/presentation 字段不能进入 model contract
research open_url / knowledge_write 不被改名成 read / write
```

### A/B

默认 parity 不需要 A/B。若本版顺手压缩工具说明或改变模型可见文案，必须先走
`tests/manual/tool_protocol_portability_ab.py`。Research untrusted source wrapper
不属于 0.5.6 生产范围；它归入 0.5.7 的 Research source rendering A/B。

## 0.5.7 - Research Follow-up Quality Closure + Source Rendering/Finalizer A/B v1

状态：发布收口。0.5.7 已把 0.4 遗留的 Research 实验分成明确结论：
PubMed/arXiv connector、untrusted source wrapper、root landing skip、浏览器正文
settling、最终报告 citation/claim filter 和一轮 bounded evidence-only follow-up
进入默认生产路径；`source_connector_done` 的 batch/checklist 形态没有推广，继续只作为
manual 复盘材料或删除候选，不留无消费者生产接线。

0.5.6 已提前完成的预备清理：`codey.research.followup_selection` 已承接
ResearchPipeline 的纯 candidate selection / stop decision；
`codey.research.followup_quality` 和 `codey.research.source_finalizer_scoring`
已承接 manual harness 之间重复的 bounded follow-up / source-finalizer 行评分与
聚合逻辑。当前修复又新增了 `tests/manual/research_experiment_gate.py` 和
`tests/manual/research_followup_quality_ab.py`，用同一套纯 scorer 复算历史结果并
继续跑 connector-backed follow-up A/B。0.5.7 不需要新建 manager。

2026-09-05 发布验收结论：

```text
PubMed/arXiv source connector:
  保留默认路径。gate 统计 target_host_gains=3、target_host_losses=0、
  score_gains=4、score_losses=0；MiMo PubMed connector-priority smoke 已确认
  真实网页搜索和 PubMed 打开路径恢复。

bounded evidence-only follow-up:
  保留 guarded 默认形态，但不扩大。最新 gate 统计 source_file_count=93、
  skipped_incomplete_files=18、bounded_pairs=47、useful_pairs=17、
  safe_evidence_only_pairs=16、safe_evidence_only_useful_pairs=12，决策仍是
  keep_default_with_more_live_gate。历史中仍有 quality regression 样本；因此只保留
  evidence-only write + deterministic merge，不恢复让模型重写整篇报告的旧形态。
  2026-09-05 MiMo PubMed archive A/B 是 complete=false：baseline 完成但 proof
  partial，planner transcript 已保存但没有 planner row / case_complete，因此不能
  当作 planner 收益或失败证据。
  2026-09-05 forced actionable-gap MiMo clean A/B 则验证了真实收益：baseline
  score=5/proof_ok=false/answer_coverage_gap；planner 跑一轮 evidence-only
  follow-up，新增 1 条 evidence，达到 score=12/proof_ok=true。

done citation/source finalizer:
  保留为窄的引用/来源列表整理器，并新增 final-report claim filter。它能减少
  done retry / quality retry，也能删除或降级 unsupported final claims；但不能宣传成
  新研究引擎，也不能推广旧 batch/checklist arm。
  2026-09-05 新增 common source-id 格式编译：`来源s2`、`来源 s2`、`（s2）`、
  `（来源s2、s3）`、表格 `| s2 (...) |` 和行首 `s2:` 可以归一成 `[2]`。
  这只减少格式 retry；不补 evidence、不伪造 citation。
  finalizer claim filter 的生产原则是：结论/关键证据区必须 citation + evidence
  support；重要但未绑定证据的 claim 降级到限制/待验证；重复或泛泛而谈的
  unsupported 句子删除。

untrusted source wrapper:
  已接入默认 open_url source rendering。MiMo + Qwen evidence-safe clean fixture
  与 post-production gate 覆盖 24 行、12 个 wrapper 行、2 个 provider，
  injection_leak_count=0、quality_regression_count=0、terminal_failure_count=0，
  decision=keep_default_untrusted_source_wrapper。它只把 source body 明确标成
  untrusted data，不改变 planner、tool schema、EvidenceLedger、citation contract
  或 report rewrite。

当前真正堵点:
  0.5.7 已闭合“搜得到但报告不贴证据”的主要失败模式。后续质量提升不是继续堆
  Research 功能，而是扩大真实 provider gate 覆盖，继续确认 planner 在明确
  actionable gap 上稳定有净收益。
```

### 做什么

0.5.7 最终保留的生产模块：

```text
codey/research/followup_selection.py（纯 selection / stop decision，供生产和 gate 共用）
codey/research/query_planner.py（有界、确定性计划；只在明确 proof gap 时被生产消费）
codey/research/plan_executor.py（有界 fresh-material 执行器；不做后台递归）
codey/research/evidence_followup.py（单轮 knowledge_write-only evidence 提取 + 一次 schema repair）
codey/research/record_merge.py（确定性合并新 evidence，不合并 unsupported 新 claim）
codey/research/done_finalizer.py（citation compiler + final-report claim filter）
codey/research/source_rendering.py（默认 open_url/source-content untrusted-data wrapper）
codey/research/source_finalizer_scoring.py（纯 scorer，只供 manual/gate；不参与生产报告生成）
codey/research/followup_quality.py（纯 scorer，只供 manual/gate 与发布决策）
```

manual-only 仍保留为证据和复盘层，不是生产接线：

```text
tests/manual/research_experiment_gate.py
tests/manual/research_followup_quality_ab.py
tests/manual/research_forced_followup_gap_ab.py
tests/manual/research_source_rendering_ab.py
tests/manual/research_claim_support_projection.py
tests/manual/source_connector_done_ab.py
```

闭环对象：

```text
Research bounded follow-up：
  继续保持 evidence-only / fresh URL / bounded rounds 边界
  用 scorer + journal 证明 proof quality、source coverage、completion honesty 没有变差
  只根据真实失败样本调整 planner / material selection / merge selection

source_connector_done batch/checklist：
  不直接推广旧 arm
  先用 deterministic scorer 和 live A/B 比较 done quality / citation quality / provider stalls
  胜出才收成窄的 source finalizer；不胜出就删除生产接线，只保留 manual 复盘材料

Research untrusted source wrapper：
  已只改变 source-content rendering，不改 planner、tool schema、EvidenceLedger、citation contract 或 completion gate
  baseline / treatment 都必须落 result JSON、journal 和 transcript
  source injection text 不能变成 tool action
  source body 必须被明确标记为 data / untrusted source
  evidence quality / source coverage / completion honesty 不能下降
  已胜出并同版接入默认 open_url 渲染；后续 wording/renderer 变更仍必须先 A/B，失败就删除 treatment 或保留 manual-only
```

真实消费者：

```text
ResearchPipeline 读取 followup quality decision，不自己解释 A/B 原始字段
release gate 消费 scorer 输出，不靠手读 transcript 决定推广
manual A/B journal 继续保存对照结果，但不能被 production RunTrace import
```

直接收益：

```text
Research follow-up 不再只是“安全可跑”，而是有 proof-quality 结论
0.4 的 source_connector_done 实验要么变成窄而有证据的默认 finalizer，要么退出生产路线
Research source prompt-injection hardening 已通过 A/B 进入默认 source rendering；后续只继续验证，不扩大它的职责
减少 manual harness / production path 之间的重复指标和悬空接线
```

### 边界

- 不新增 Research 模型工具。
- 不做后台递归 Research。
- 不让 batch/checklist 未经 A/B 进入默认 finalizer。
- 不改变 EvidenceLedger、citation contract 或 completion gate，除非 treatment 胜出且有 release gate。
- 不把 default-off source wrapper 停放在 production module；当前 wrapper 已是默认 renderer，后续变体仍必须 promote-or-delete。
- 不把 provider-specific prompt 当通用 Research 策略。
- 不把 raw webpage body、raw transcript、prompt、reply 写入生产 trace。

### 顺手架构优化

```text
ResearchPipeline 继续只调用 followup_selection 的纯 decision，不解释 A/B 原始字段
followup_quality.py / source_finalizer_scoring.py 只做纯 scorer，不调用 provider/browser/tool runtime
source_connector_done manual arm 与 release gate 共用 scorer，避免重复指标
Research finalizer 只消费结构化 decision，不扫 A/B journal 原文
source wrapper renderer 已在 A/B 胜出后成为默认路径；后续 renderer 变更仍走同一 gate
```

### 验证

```text
bounded follow-up 仍只能使用允许的 Research tools
fresh URL whitelist / max rounds / max sources 边界保持
scorer deterministic 且不依赖 provider
source_connector_done treatment 不能降低 citation locator 命中率
source_connector_done treatment 不能增加 unsupported claims
source wrapper treatment 不能把网页中的指令性文本转成 tool action
source wrapper treatment 不能降低 evidence quality / source coverage / completion honesty
provider stalls / extra provider sends 必须计入 gate
未通过 A/B 的 arm 不得被 production ResearchPipeline import
```

### A/B

需要。它决定 Research 默认 finalizer / material selection 是否改变。指标：

```text
proof_ok
evidence quality
citation quality
source coverage
completion honesty
unsupported_claim_rate
provider_stall_count
extra_provider_send_count
latency
sent_chars
```

发布判断：

```text
如果 bounded follow-up 质量收益稳定：保留默认 evidence-only follow-up，并只调整证明有收益的 selection/merge 点
如果 source_connector_done batch/checklist 胜出：收成最窄 production finalizer，旧 batch/checklist harness 退到 manual
如果 source_connector_done 没胜出：删除或继续 manual-only，不留下 production 接线
source wrapper 已胜出并接入默认 open_url/source-content rendering；后续只允许基于新 A/B 证据调整 wording/renderer
如果新的 source wrapper 变体没胜出：删除 treatment 或继续 manual-only，不修改默认生产路径
```

## 0.5.8 - Pi Agent v2-inspired Durable Operation Core v1

状态：实现候选已落地，待 review / release。目标不是继续加 Ghost / World Model / provider learning，而是把 0.5.1-0.5.7
已经做出来的 durable runtime、effect intent/settlement、safe replay、delivery receipt、
prompt surface hardening 和 Research proof gate 收成一个更明确的 operation state machine。

参考：

```text
docs/codey_pi_v2_refactor_direction.zh-CN.md
https://github.com/earendil-works/pi
reference-projects/pi @ da840b621
reference-projects/pi/packages/agent/docs/harness.md
reference-projects/pi/packages/agent/docs/runtime-simplification.md
reference-projects/pi/packages/agent/docs/assistant-durability.md
reference-projects/pi/packages/agent/docs/tool-durability.md
reference-projects/pi/packages/agent/docs/work-packages/05-direct-durable-drive.md
reference-projects/pi/packages/agent/docs/work-packages/06-session-branch-lane-separation.md
```

Pi v2 给 Codey 的关键启发不是 Lane / RemoteSession / CBOR，而是：

```text
Operation 接受即 durable
OperationState 是恢复入口
外部 effect 前有 intent，effect 后有 settlement
所有 runtime mutation 通过 single mutation boundary
snapshot/state 是事实源，event/trace 是观察面
```

### 做什么

当前落点：

```text
codey/runtime/operation_state.py      # total durable operation state; first-class operation_state log entry
codey/runtime/operation_reducer.py    # pure state + durable facts -> RuntimeAction
codey/runtime/drive.py                # peek_next_action(), no side effects
codey/runtime/mutation_line.py        # serialized production mutation boundary
codey/runtime/effect_records.py       # effect intent / settlement ledger, projection + entry builders
codey/runtime/tool_result_delivery.py # delivery receipt ledger, projection + entry builders
codey/runtime/session_projection.py   # session-log projection, renamed from reducer.py
codey/runtime/session_log.py          # mutate() + repair/compaction storage adapter
```

目标形态：

```text
TaskRuntime accepts Operation
  -> OperationState says where recovery starts
  -> pure reducer decides RuntimeAction
  -> effect executor performs provider/tool/delivery/storage effect
  -> settlement updates durable state
  -> CompletionProof / Evidence decide user-visible done
```

### 建议 state/action

第一版不照搬 Pi 的 13 个 leaf，但必须 closed、total、可测试：

```text
accepted
writer_running
provider_effect_pending
tool_effect_pending
tool_delivery_pending
writer_settled
completion_proof_recorded
repair_context_admitted
repair_running
repair_settled
terminal
```

每个 state 必须携带足够恢复下一步的 bounded refs：

```text
operation_id
lane
leaf
driver = writer | repair
pending_effect_ids
pending_delivery_batch_id
turn/tool_index
completion_proof_ref
repair_context_ref
terminal outcome
```

`RuntimeAction` 第一版只覆盖真实失败点：

```text
continue_operation
settle_provider_unknown
replay_safe_tool_batch
synthesize_interrupted_effects
terminal
fail_invariant
```

### 边界

- 不重写 `TaskRuntime` 成大 manager。
- 不把 Effect Ledger 合并进 OperationState。
- 不保留 `RuntimeSessionLog.append()` / `append_many()` 作为业务 API；底层原子提交统一叫 `mutate()`。
- 生产业务事实不能直接调用 `RuntimeSessionLog.mutate()`；`mutate()` 只作为 log 层原子原语，由 `RuntimeMutationLine` 使用。
- 不暴露泛型 `RuntimeMutationLine.transition_operation()`；writer/proof/repair/terminal 都走具名 mutation。
- 不让 `RuntimeOperationStore` 暴露 `start()` / `commit()` / `delete_session()`；它只做读取投影。
- 不让 Event / RunTrace 成为 recovery source of truth。
- 不新增完整 Lane 系统、RemoteSession、RPC、CBOR 或 SQLite migration。
- 不改变 PermissionProfile / ToolContract / PromptEnvelope 语义。
- 不让 Ghost / World Model 进入 runtime core。
- 不保存 raw prompt、raw reply、stdout/stderr、webpage body 或 source body。

### 顺手架构优化

```text
task_run.py 只做 wiring / dispatch / cleanup，不继续吸收新状态解释
recovery.py 从 OperationState + EffectSettlement 判断下一步，不从缺失 event 猜测
Runtime reducer 是 pure function，不能调用 provider/tool/filesystem/network
session log / operation phase / effect settlement 共用 operation_id/lane/run_id vocabulary
invalid_tool_called / max_turns / cancelled 的 terminal 分类在一个地方收口
```

### 验证

```text
total transition table 拒绝非法 phase / transition
terminal 后不能继续写 business phase
provider intent 无 settlement -> unknown outcome recovery
全安全、未发送的 tool batch 无 settlement -> safe batch replay
不可安全重放的 pending tool effect -> synthetic interruption
tool effect 完成但 delivery 未完成 -> tool_delivery_pending recovery
invalid_tool_called 不会绕过 terminal/proof 分类
completion proof failed 后，repair 是否允许由 state/action 决定
production 不 import tests.manual
Ghost / World Model / protocol learning 不 import runtime internals 执行 effect
runtime core 不 import agents / operations / providers / research / app / ghost
```

### A/B

0.5.8 本身不需要 live provider A/B，因为它不应该改变模型可见能力。它需要 deterministic
state-machine tests、manual drive tests、crash-injection style tests 和全量 pytest。

如果 0.5.8 顺手改变 prompt、tool surface、Research follow-up 或 provider repair prompt，则该改动必须另开 A/B，不能混进 runtime refactor release gate。

## 0.5 Exit Gate 与 0.6 切入线

0.5 做完前，不能因为“功能项都写完了”就直接进入 Ghost / World Model。必须先通过
Exit Gate：

```text
0.5.0-0.5.7 的 Verified Completion / Research proof / prompt surface tests 继续通过
0.5.8 OperationState 是 closed/total transition table
RuntimeAction reducer 是 pure function
生产代码只有 RuntimeMutationLine 调用 RuntimeSessionLog.mutate()
RuntimeMutationLine 没有泛型 transition_operation() public surface
外部 effect 前有 intent，effect 后有 settlement
恢复时读 durable state，不从事件缺失推断
ToolResultDelivery / CompletionProof / Evidence source of truth 明确
TaskRun 不再继续吸收 provider/research/proof/recovery 之外的新 lifecycle 状态
Ghost / World Model / protocol learning 没有污染 evidence / permission / completion
Research source wrapper、bounded follow-up、final-report claim filter 的生产接线有 release evidence
Protocol telemetry / contract hash 若无消费者，删除或降级为 manual/evaluation 数据
Safe read/search replay 已实现，unsafe replay count 始终为 0
所有 A/B 失败都能通过 JSON / journal / transcript 复盘
全量 pytest 通过
```

0.6 的主线不再是 0.5 runtime correctness，而是建立在稳定 runtime 上的本地经验层：

```text
0.6 = Ghost / World Model / Protocol Adaptation on Stable Durable Runtime
```

## 0.6 主线 - Ghost / World Model / Protocol Adaptation

0.6 承接原先 0.5.8 之后的 Ghost、World Model、provider/protocol learning 和可选 adapter 工作。
这些能力都必须是 runtime core 的用户，而不是 runtime core 的一部分。

0.6 做：

```text
Project Verification Habit Projection
Provider / Protocol Affinity + Repair Outcome Learning
Ghost Explain / Inspector
World Model Event Log + Prediction Review
World Model ContextSource + Shadow Strategy Ranker
Protocol Adapter Dataset Export + Shadow Normalizer
Local Protocol Classifier + Repair Strategy Selector
Conditional Tool Projection / One Proven Dialect
Native Structured Provider Path
Local Training Export / Optional Tiny Adapter
Ghost / World Model Maintenance Hardening
```

0.6 不做：

```text
Ghost persona
World Model 自动决策
跨 provider 自动仲裁
插件市场
大 UI
Ghost / World Model 写 EvidenceLedger 或 CompletionProof
adapter 输出绕过 runtime validator
```

### 0.6.0 - Project Verification Habit Projection v1

目标：让 Codey 记住项目实际验证习惯，帮助模型更容易选择正确验证命令，但不自动执行，
也不把历史习惯当本轮 completion proof。

边界：hint 必须经过 ContextEpoch；explicit project config 优先；历史成功只能生成 habit，不能生成 fresh verification。

### 0.6.1 - Provider / Protocol Affinity + Repair Outcome Learning v1

目标：让 Ghost 学习 provider 在协议摩擦和 repair prompt 上的 bounded outcome，只影响诊断和 repair strategy，
不自动切 provider、不授权工具、不进入 EvidenceLedger。

边界：只存 counts、reason codes、refs 和 digest；不存 raw prompt/reply。

### 0.6.2 - Ghost Explain v0 + Provenance-Safe Inspector

目标：解释 Ghost hint 为什么被选中。第一版只做 deterministic renderer 和 CLI/JSON，不进默认 prompt，
不调用 provider，不生成工具调用。

边界：payload 必须使用 `provenance_refs`，固定标注 not evidence / not policy。

### 0.6.3 - World Model Event Log + Prediction Review v0

目标：记录项目/研究/环境状态预测，并用已有 proof、verification 或用户纠正复盘命中/失败。
第一版不进 prompt，只用于 RunTrace、blocked summary 和本地诊断。

边界：没有 proof/event/user-correction refs 时只能 `unjudged`，不能猜 hit/miss。

### 0.6.4 - World Model ContextSource + Shadow Strategy Ranker v1

目标：把 state estimate 变成受限 ContextSource，只提示“哪里需要复查”，不告诉模型“什么是真的”。
Shadow ranker 只评估策略，不接管执行。

边界：stale projection 只能生成 re-check hint；`recheck_refs` 不能渲染成 citation/source/evidence refs。

### 0.6.5 - Protocol Adapter Dataset Export + Shadow Normalizer v1

目标：把 protocol telemetry、tool args repair、repair prompts 和最终 outcome 导出成可选本地数据集，
并用 shadow normalizer 离线评估更早得到合法 ToolCall 的可能性。

边界：默认关闭；不导出 secret、cookie、DOM、webpage body、source body 或 raw transcript。

### 0.6.6 - Local Protocol Classifier + Repair Strategy Selector v1

目标：用规则或小型本地 classifier 选择 repair prompt strategy、tool-args repair strictness 和 protocol hint 长度。

边界：不能选择 provider，不能授权工具，不能跳过 verification；坏输出 fail closed 到默认 repair prompt。

### 0.6.7 - Conditional Tool Projection + One Proven Dialect v1

目标：只在 A/B 证明收益后，为一个 provider/model family 启用一个替代模型可见工具面，并 lower 到 Codey canonical ToolCall。

边界：没有 A/B 胜出就不启用生产默认；permission/profile/policy 对所有 dialect 结果一致。

### 0.6.8 - Native Structured Provider Path v1

目标：给真正支持原生 tool/function calling 的 API provider 一个可选 structured path，避免正文 JSON 协议摩擦。

边界：structured tool call 仍必须 lower 到 canonical ToolCall 并过 runtime validator；web provider 不受影响。

### 0.6.9 - Local Training Export + Optional Tiny Adapter v0

目标：把 protocol/error/repair/claim-gap 数据用于可选的小适配层训练或 shadow eval。默认关闭，不训练主模型。

边界：adapter 先 shadow；若要默认启用，必须回到 live A/B gate。

### 0.6.10 - Ghost / World Model Maintenance Hardening v1

目标：让长期状态可衰减、可删除、可重建、可导出，维护不联网、不调用 provider、不执行工具。

边界：quarantine 不删除原始 event，除非用户显式 delete scope。

## 插件开放边界

0.6 进入 Ghost / World Model / protocol adaptation 之后，也仍然不做有限插件化。
即使 0.6.8 以后引入 structured provider path，它也只是 provider capability，
不是插件系统。真正的开放顺序仍然应放到 runtime core 和 provider capability 都稳定后：

```text
trusted built-in plugins
  -> signed / bundled provider adapters
  -> read-only exporters
  -> Research source connectors
  -> limited external plugin API
```

仍然不应开放：

```text
prompt / Router / PermissionProfile 任意覆盖
agent loop 接管
shell approval 绕过
Local context 直接写入 accepted state
后台自动任务无用户入口执行
connector 任意读写本地文件或联网
```

## Adapter 自修复 prompt 分层（后续）

早期把修复面扩成完整 web adapter 层后，repair prompt 明显变重：实测 Qwen 约
117k chars、DeepSeek 约 112k chars。语义上正确，但 live web helper 在网页故障时
更容易慢、截断或跑偏。不加复杂检索系统；后续按 failure stage 做轻量分层：

```text
默认：目标 driver 源码 + codey/providers/profiles.json + 失败事实 + 全部 surface 文件清单（只列路径）
升级：shared failure 或二轮请求时，才内联 browser.py / provider_controls.py 这类大文件全文
```

判断线：只有当 live repair 实测出现截断或超时再引入分层机制，不提前加。

## 验证体系

0.3 已完成 Ghost 专用验证和能力边界验证：Run Trace、Prompt Envelope、
Capability Registry、Tool Contract、Action Policy、Event Matrix、Built-in Profiles
和 Run Details 都必须有 deterministic tests，不能靠 live provider A/B 发现架构回退。

0.4 已完成 Evidence Research Runtime 验证。Research 质量不能只看最终 summary
是否像样，还要验证 source、evidence、claim、assumption、analysis run、artifact、
critic finding 和 Ghost continuity 的边界；这些测试现在作为 0.5 的回归地基保留。

0.5 继续新增 Durable Runtime 验证；Local Adaptation / Protocol Portability 移到 0.6，
作为稳定 runtime core 的用户来验证。重点不是“模块是否存在”，而是每个运行边界是否能证明：

```text
edit/test integrity monitor 进入 production completion path
high suspicious 不能显示为 clean verified completion
monitor_error / unobserved 不能被当 clean
外部效果前有 intent
外部效果后有 settlement
恢复时读 durable state，不从事件缺失推断
0.5.8 OperationState / RuntimeAction reducer 是 closed、total、pure
Ghost / World Model / Protocol adapter 在 0.6 仍不能跨过 Evidence / Permission / Completion
每个 0.6 模型可见 hint 都绑定 ContextEpoch
每个 0.6 tool dialect 都 lower 到同一个 canonical ToolCall
```

### Ghost 单元测试

```text
tests/test_ghost_signal_extractor.py
tests/test_ghost_inbox.py
tests/test_ghost_hebbian.py
tests/test_ghost_directive.py
tests/test_ghost_learning_loop.py
tests/test_ghost_continuity.py
tests/test_ghost_sleep.py
tests/test_ghost_work_queue.py
tests/test_research_interest_queue.py
tests/test_ghost_affinity.py
tests/test_task_runner_affinity.py
```

### 能力边界单元测试

```text
tests/test_run_trace.py
tests/test_task_runner_run_trace.py
tests/test_prompt_envelope.py
tests/test_capabilities.py
tests/test_tool_contract.py
tests/test_action_policy.py
tests/test_event_matrix.py
tests/test_builtin_profiles.py
tests/test_run_details.py
```

### Evidence Research Runtime 单元测试

0.4 已覆盖：

```text
tests/test_research_object_model.py
tests/test_research_evidence_ledger.py
tests/test_research_proof_quality.py
tests/test_answer_coverage.py
tests/test_citation_locator.py
tests/test_research_query_planner.py
tests/test_research_pipeline.py
tests/test_source_connectors.py
tests/test_connector_parity_pack.py
tests/test_research_analysis_run.py
tests/test_artifact_lineage.py
tests/test_ab_observation_journal.py
tests/test_transcript_replay_cache.py
tests/test_provider_observation_log.py
tests/test_evidence_runtime.py
tests/test_research_critic.py
tests/test_review_finding_lifecycle.py
tests/test_safe_context_epoch.py
tests/test_research_contract_lite.py
tests/test_domain_evidence_profiles.py
tests/test_research_brief_v2.py
tests/test_research_to_code_impact.py
tests/test_longitudinal_research_harness.py
tests/test_research_comparison_benchmark.py
tests/test_ghost_research_continuity.py
```

### Durable / Adaptation 单元测试

0.5 逐步新增：

```text
tests/test_run_operation.py
tests/test_task_runner_operation_state.py
tests/test_effect_log.py
tests/test_tool_replay_policy.py
tests/test_agent_effect_sandwich.py
tests/test_tool_args_repair.py
tests/test_safe_tool_replay.py
tests/test_tool_result_delivery.py
tests/test_tool_prompt.py
tests/test_tool_contract_drift.py
tests/test_research_followup_quality.py
tests/test_source_connector_done_scorer.py
tests/test_ghost_protocol_affinity.py
tests/test_provider_protocol_learning.py
tests/test_project_verification_habits.py
tests/test_task_runner_project_habits.py
tests/test_ghost_explain.py
tests/test_cli_ghost_explain.py
tests/test_world_model_events.py
tests/test_world_model_prediction.py
tests/test_world_model_projection.py
tests/test_world_model_context.py
tests/test_world_model_strategy.py
tests/test_protocol_dataset.py
tests/test_shadow_protocol_adapter.py
tests/test_protocol_classifier.py
tests/test_protocol_repair_strategy.py
tests/test_structured_provider_path.py
tests/test_training_export.py
tests/test_tiny_adapter_policy.py
tests/test_world_model_maintenance.py
tests/test_local_state_delete_export.py
```

### 架构测试

必须锁住：

```text
codey/ghost 不 import torch
codey/ghost 不 import transformers
Ghost 不能 import tool_runtime 执行函数
ToolRuntime 不 import Ghost
Research evidence 不依赖 Ghost affinity
GhostWorkItem 不能直接调用工具
repair prompt 不包含 Ghost Directive
PermissionProfile 仍然是执行边界
model-visible section 必须有 Prompt Envelope source refs
Run Trace 不保存 raw prompt / raw chat / source body / webpage body
Tool presentation 不能进入 model_text
Action Policy deny 不能被后续 guard 覆盖
Capability Registry 不加载第三方代码
Built-in profile 不能放宽 PermissionProfile
Run Details 不显示内部术语或 raw prompt
Research Object Model 不保存 raw webpage body
A/B Observation Journal 不能被生产 RunTrace import
TranscriptArchive 不能写入 EvidenceLedger / ResearchRecord
TranscriptReplayCache 不能成为 Evidence / Citation / CompletionProof
ProviderObservationLog 不保存 DOM / Cookie / raw webpage body
AB events hash chain 必须可恢复且能发现非尾部断链
SearchResult 不能成为 Evidence
GhostHint 不能成为 Evidence
Claim 必须绑定 Evidence 或标成 Assumption
Claim graph relation kind 必须来自固定枚举
Citation locator 必须能回到 opened source
Answer Coverage 不能只看 citation presence
ResearchPlan dry-run 不能执行 search/fetch
Bounded Research Planner 必须受 max rounds / queries / sources 限制
Research bounded follow-up scorer 不能调用 provider/browser/tool runtime
source_connector_done batch/checklist 未经 A/B gate 不得进入默认 finalizer
AnalysisRun 必须走 ActionPolicy / ToolRuntime
Reproducibility Capsule 不保存 raw stdout/stderr
Source Connector Registry 不加载第三方代码
Connector Parity Pack 不新增模型工具面
Research Proof Quality 不能只认 research:* 字符串
ReviewFinding confirmed 必须来自后续 verification event
Research Contract Lite 不能是模型工具
Safe Context Epoch 必须约束 Ghost continuity admission
Research-to-Code impact 不能授权工具
RunOperationState 不能 import agent/provider/tool_runtime/server/ghost
RuntimeEffectStore 不能保存 raw prompt/reply/stdout/diff/source body
外部 provider/tool/repair effect 启动前必须已有 intent
unsafe tool effect_pending 恢复不能重复执行
ReplayPolicy 未知工具默认 unsafe
Safe replay args 必须 canonical、bounded，并重新过 runtime validator
Tool result delivery receipt 不保存 source body / stdout / stderr / prompt / reply
Tool args repair shim 不能 import tool_runtime/provider/task_runner
Tool prompt renderer 不能改变默认 prompt parity，除非对应 A/B 更新
Protocol adapter 不能绕过 canonical ToolCall validation
Protocol dialect prompt 不能混用多套工具语言
CompletionProof / Evidence / EvidenceLedger 不能成为模型工具
Ghost protocol affinity 不能进入 EvidenceLedger / CompletionProof
Project habit 不能产生 fresh verification fact
Ghost Explain 不得进入默认 PromptEnvelope
Ghost Explain payload 必须使用 provenance_refs，不得输出 evidence_refs
codey/world_model 不 import provider/browser/tool_runtime/task_runner
WorldModelProjection 不产生 evidence_refs / citation_refs
WorldModelContext 未经过 ContextEpoch 不得进入 prompt
World Model stale projection 只能生成 re-check hint
Protocol dataset export 默认关闭且不得写 raw transcript
Tiny adapter 输出必须再过 runtime validator
Structured provider path 不影响 web provider
Ghost / World Model maintenance 不能执行 provider/search/tool calls
```

### A/B 测试

0.4 的逐版本 A/B 矩阵已经归档到 `docs/0.4_final_stabilization_report.zh-CN.md`、
各 provider baseline 文档、`TEST_REPORT.md` 和 `tests/manual/`。Roadmap 只保留以后仍适用的规则。

需要 live/provider A/B 的情况：

```text
改变模型可见 prompt、Research prompt、Writer prompt、Router 行为、provider fallback 策略、
工具权限、completion 用户可见结果，或让某个 manual treatment 进入默认生产路径。
```

通常不需要 live/provider A/B，但仍需要 deterministic/fault-injection 测试的情况：

```text
只改 trace/schema/ledger/projection/metadata
只改 read-model scorer 或 release gate
只改 resume/fault-injection 路径且 clean path 字节不变
只新增默认关闭的导出/诊断脚本
```

0.5+ 的 A/B 原则：

```text
每个 promoted treatment 必须有 result JSON / journal / manifest / transcript 或明确说明 transcript mode
每个 gate 只能消费 bounded metrics，不复制 raw prompt/reply/source body
provider stalls、extra sends、false completion、unsupported claim rate 都要计入决策
A/B 失败的 treatment 删除或留在 manual-only，不留下 production 接线
```

### Live smoke

Provider 覆盖：

```text
DeepSeek
Qwen
MiMo
StepFun
GLM
local API
```

最小 smoke：

```text
style preference learning
correction candidate extraction
Ghost Directive render
Chat with directive
planning_readonly with directive
Project Writer protocol compliance without directive
Ghost WorkItem creation without automatic execution
Research quality unaffected when directive disabled
Research proof quality with queued open question
Evidence object model projection smoke
Connector recorded fixture + live connector smoke
Longitudinal topic tracking smoke
Comparison benchmark fixture smoke
Real OpenScience manual head-to-head smoke（发布 surpassed OpenScience 结论前）
RunOperation crash-position smoke
Effect intent/settlement fault-injection smoke
Tool args repair live A/B smoke
Project verification habit prompt admission smoke
World Model context admission smoke
Protocol repair strategy live A/B smoke
```

## 架构债边界

0.4 已经让 Research、Evidence、Review、Ghost、Provider fallback 和本地持久化在
TaskRunner / Server 周围汇合。0.5 会继续收紧 RunOperationState、RuntimeEffectStore
和 ReplayPolicy；ProtocolAdaptation 和 World Model 移到 0.6，必须作为 runtime core
的用户出现。这个压力是真实的，但不能为了“看起来架构更好”做 big-bang rewrite。

需要承认并逐步收敛的债务：

```text
TaskRunner.run 承担 provider setup、routing、research、review、writer、ledger、trace
_RunFrame 已经包含 provider / conversation / trace / handoff / preflight / snapshot 等生命周期状态
后续 planner、multi-browser、recursive research 会继续放大这个 runtime context
0.5.8 只加入 operation state/reducer/drive/mutation boundary，不继续加入 Ghost/World/protocol strategy
```

正确拆分顺序：

```text
RunOperationState：先覆盖 completion/repair terminal，再扩到 provider/tool effects
RuntimeEffectStore：只在 provider/tool/repair 三个真实效果边界稳定后引入，且不新增第二套 durable log
ReplayPolicy：先保守 safe/unsafe，再谈自动恢复
ResearchPipeline：只在 proof quality / evidence ledger / follow-up research 边界成熟后抽
ProviderPipeline：只在 provider setup / preflight / fallback / canary 边界稳定后抽
ReviewPipeline：只在 review input / fix loop / finding lifecycle 边界稳定后抽
ProtocolAdaptation（0.6）：先参数 repair，再 prompt contract，再方言投影
WorldModelProjection（0.6）：先事件/复盘，再 ContextSource，再策略 shadow
SessionContext：request/session/conversation/snapshot
ProviderContext：provider/provider_id/preflight/fallback/supervisor state
TraceContext：run trace / prompt trace / ledger trace refs
```

每次拆分都必须满足：

```text
不改 prompt / tool schema / model-visible tool result
不改 UI/SSE/receipt shape
不放宽 permission/profile/policy
先有 deterministic tests 锁住旧行为
只沿真实生命周期边界抽，不按“减少行数”硬拆
每个新模块必须在同版本被生产路径或明确 CLI/diagnostic 路径消费
每个 shadow / export 能力必须有立即可用的回放、评分或解释价值
```

## 成功定义

0.3 做完后，Codey 已经从：

```text
一个可靠的本地 coding/research workbench
```

变成：

```text
一个有长期连续性的 Ghost，使用 Codey 作为安全身体工作和研究
```

并且 Codey 内部应该从：

```text
多个强能力在 TaskRunner / Server 周围汇合
```

变成：

```text
每个能力有边界
每次运行可追溯
每个工具有契约
每个危险动作有统一 guard
每个用户可见面板只展示 bounded presentation
```

用户体验上不应该变复杂。理想状态是：

```text
你继续自然聊天、研究、写代码
Ghost 慢慢更懂你
Codey 仍然可靠、可恢复、可验证
外部模型可以换，但“懂你的东西”留在本地
```

这是 0.3 的核心目标。

0.4 做完后，Codey 应该进一步变成：

```text
一个低打扰、本地优先、可审计、有长期连续性的个人 Evidence Research Runtime
```

并且不只是“模块都完成”。它必须在固定 comparative fixtures 上通过 Regression Gate；
发布“超过 baseline / 真实 OpenScience”的结论前，必须在对应对照上满足
Superiority Gate。发布“超过真实 OpenScience”前，还必须有一次可追溯的
real OpenScience manual head-to-head：

```text
比 baseline web model 更少 unsupported claim
比普通 citation report 有更高 locator precision
比一次性 Research 更好地处理 stale update 和长期 topic
比大工作台式研究工具更少打扰用户
Research 结论更容易安全落到代码和测试里
```

用户仍然只需要自然地说：

```text
帮我研究这个问题
继续追踪这个主题
用这些资料分析一下
把研究结论落到项目里
```

Codey 在后台完成：

```text
来源识别
证据摘录
claim grounding
assumption 标注
本地分析运行记录
artifact lineage
critic / review
长期主题连续性
```

前台仍然安静：Research drawer 和 Run Details 只在用户主动查看时显示 bounded summary，
不把 provenance graph、connector graph、profile、ledger 或内部 Ghost 术语推给用户。

0.5 做完后，Codey 应该进一步变成：

```text
一个可恢复、可解释、不会靠猜测恢复状态的本地 agent runtime
```

并且每个增强都不要求用户理解内部系统：

```text
provider/tool/repair 的不确定窗口有 intent 和 settlement
edit/run 崩溃不会被静默重复执行
safe read/search 可以恢复，unsafe effect 会诚实中断
CompletionProof / RepairContext 有 durable phase，而不是函数局部变量
0.5.8 有 total OperationState / pure RuntimeAction reducer / manual drive tests
Ghost / World Model / provider-protocol learning 暂不进入 0.5 runtime core
```

0.6 做完后，Codey 才应该进一步变成：

```text
一个能基于本地经验变顺手，但仍然不把经验当证据或权限的 agent runtime
```

0.6 的用户可感知收益：

```text
Ghost 能学习 provider 协议摩擦和项目验证习惯，但不能当证据或权限
World Model 能提示复查和校准预测，但不能裁定事实
Tool protocol 可以按 provider 逐步适配，但 Codey 内部执行 IR 不变
API/native structured provider 有更少 JSON 摩擦，web provider 稳定性不受影响
本地适配数据可导出、可删除、可回滚，默认不泄露 raw transcript
```

用户体验仍然应该是：

```text
你继续自然聊天、研究、写代码
Codey 更少误判 done
Codey 更少重复危险动作
Codey 更懂项目验证习惯
Codey 更能解释本地 hint 从哪里来
外部模型可以换，工具语义和安全边界仍然留在 Codey
```
