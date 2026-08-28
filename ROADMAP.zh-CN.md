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

这些能力共同提供了 0.4 需要的研究底座：

- `ghost/extractor.py`、`ghost/inbox.py`、`ghost/hebbian.py` 让长期偏好和纠错先进候选区，再被审计接受。
- `ghost/directive.py` 和 `context_source.py` 让本地连续性以有预算的中性文本进入 prompt，且不能授权工具。
- `ghost/learning_loop.py`、`ghost/continuity.py`、`ghost/sleep.py` 让 Codey 能记住长期主题、开放问题和用户偏好，但不保存完整聊天正文。
- `ghost/router.py` 让 Ghost 只提出意图；手动入口、PermissionProfile 和 TaskRunner 仍然优先。
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
       - TaskRunner
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

1. 0.3 已经把 Hebbian / Ghost 长期连续性落地；0.4 要把它约束进 Evidence Research Runtime，而不是让记忆替代证据。
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
14. Work item 可以建议下一步，但不能绕过用户显式入口、PermissionProfile 或 TaskRunner。
15. 版本号可以出现在 roadmap 标题或 release label 里，例如 `Tool Contract v2`
    和 `Action Policy Pipeline v1`；长期 API、类型、模块、字段名不要用
    `V2`、`Next`、`New` 这类迁移期命名。应该演进稳定名字，例如
    `ToolOutcome`，或使用有真实语义的名字。

## 0.3 详细计划已归档

`0.3.0` 到 `0.3.20` 已经全部落地。Roadmap 现在只保留上面的阶段总结和下面的
`0.4` 详细计划；逐版本实现细节、验证记录和发布说明分别留在：

```text
CHANGELOG.md
CHANGELOG.zh-CN.md
TEST_REPORT.md
```

这样 Roadmap 的阅读重心回到未来版本：0.4 的 Evidence Research Runtime。

## 0.4 主线 - General Evidence Research Runtime

0.4 的目标是把 Codey 从“会做 Research 的本地助手”升级成：

```text
低打扰、本地优先、可审计、有长期连续性的个人 Evidence Research Runtime
```

研究工具的核心价值不是“搜得多”，而是：

```text
问题被真正回答
结论能回到证据
弱证据不会被写成强结论
分析可以复查
同一主题可以连续追踪
用户不用管理复杂系统
```

### 0.4 泛化研究超越指标

0.4 的“超越 OpenScience”不是拼科学数据库数量、notebook UI、skills 生态或云端计算平台。
Codey 要赢的是个人通用研究工作流：

```text
证据合同更硬
长期研究更可靠
Research -> Code 落地更顺
用户更少被打扰
```

因此 0.4 的验收不能只看模块是否完成，还要看固定评测集上的质量指标：

```text
answer coverage：final claims 是否覆盖原问题的关键词、实体、关系和约束
claim grounding precision：关键 claim 是否都有 evidence 或明确 assumption
citation locator precision：citation 是否能定位到 opened source 的页码、offset、表格或行号
counterevidence coverage：强结论前是否有反证 / 限制搜索记录
stale update correctness：旧结论遇到新来源时是否被更新、降级或标 stale
analysis reproducibility：分析输入、脚本、环境、输出、日志和 hash 是否可复查
research-to-code handoff quality：研究结论是否变成明确实现约束、文件影响和测试建议
unsupported claim rate：无证据结论比例不能高于 baseline
UI interruption count：不新增主 UI、dashboard、profile selector、自动弹窗或常驻面板
```

0.4 的对标对象分两层：

```text
baseline web model：验证 Codey 的证据链和本地分析是否明显提高质量
OpenScience-style fixture：验证 Codey 在通用研究任务上是否达到更低打扰、更硬 proof、
更强长期连续性的目标
```

如果要发布“Codey 超越 OpenScience”的结论，必须至少有一次真实 OpenScience
head-to-head。该记录必须包含：

```text
OpenScience version / commit
Codey version / commit
provider / model
任务输入
运行日期
导出 artifact 或人工评分来源
评分 rubric
```

没有真实 OpenScience run 时，只能写：

```text
Codey passed OpenScience-style regression.
```

不能写：

```text
Codey surpassed OpenScience.
```

Regression Gate v1 用来证明 0.4 没有让研究质量回退：

```text
Ghost-as-evidence count = 0
citation locator precision >= 95%
answer coverage rate >= baseline
unsupported claim rate <= baseline
counterevidence coverage >= baseline
analysis reproducibility >= 90%
research-to-code handoff quality >= baseline
UI interruption count <= baseline
```

Superiority Gate v1 用来证明“超越”：

```text
必须基于 real OpenScience manual head-to-head 或明确标注的 baseline 对照
至少 4 个核心指标严格优于对照
所有 Regression Gate 指标仍然通过
unsupported claim rate < baseline
answer coverage rate > baseline
citation locator precision > baseline 或 >= 98%
counterevidence coverage > baseline
analysis reproducibility > baseline 或 >= 95%
research-to-code handoff quality > baseline
UI interruption count <= baseline，且不能新增主 UI / dashboard / profile selector
```

如果只通过 Regression Gate，只能写“没有回退”或“通过 OpenScience-style regression”，
不能写“surpassed baseline / OpenScience”。

### 0.4 总边界

必须守住：

```text
Ghost 只能提供连续性，不能当证据
Search result 不是 Evidence
只有打开 / 读取过的 Source 才能产生 Evidence
Claim 必须绑定 Evidence，或者明确标成 Assumption
AnalysisRun 必须走 ActionPolicy / ToolRuntime / Managed Outputs
Research 仍然只能由用户显式开启联网
Research Brief 仍然有上限，不能把整个 vault 注入 Writer
UI 不新增 dashboard、profile selector、provenance graph 或常驻面板
```

0.4 不做：

```text
不做插件系统
不做大 workspace UI
不做用户可管理 connector graph
不让 profile 放宽 PermissionProfile
不让 Ghost 自动后台联网研究
不把 raw prompt / raw stdout / 网页正文 / 源码正文写进 trace、ledger 或 UI
不做 TaskRunner big-bang rewrite；只在 proof、source、planner、analysis、review
等真实生命周期边界成熟后逐步抽离
```

### 0.4 横向主线：Research Planner + TaskRunner Decomposition

0.4 不能只继续做证据治理。0.4.0 / 0.4.1 已经有 Research Object Model
和 Evidence Ledger，后续要把它们变成真正提升研究能力的 planner loop：

```text
proof gap
  -> follow-up question / query rewrite candidates
  -> bounded research plan
  -> limited follow-up search / fetch
  -> proof review
  -> critic feedback
  -> longitudinal topic tracking
```

Codey 不追求复杂可见 UI 的 Deep Research，而是在后台用 proof gate 驱动
bounded planner，让研究自己补洞、验证、收敛，同时保持用户界面安静。

同时要逐步偿还 TaskRunner 架构债，但不能为了减少行数硬拆：

```text
0.4.2 抽 ResearchCompletionGate / proof_quality.py
0.4.3 抽 source_connectors.py / query_planner.py dry-run 边界
0.4.4 抽 ResearchPipeline v1，承接 bounded planner execution
0.4.5 抽 AnalysisRunStore / Reproducibility Capsule / Artifact metadata 边界
0.4.6 抽 A/B Observation Journal / Transcript Replay Cache / Provider Observation Log
0.4.7 抽 Evidence Runtime / ReviewFinding core
0.4.8 抽 Safe Context Epoch / Capability Boundary
0.4.9 抽 Research Contract Lite / Verified Completion Gate
0.4.10 抽 Domain Source Trust / ResearchBrief projection / impact contract
0.4.11 把 Research runtime 串成可回归 harness 和 comparison benchmark
0.4.12 抽 TopicContinuityService / Ghost Research Continuity
0.4.13 抽 Verified Completion Enforcement / Repair Context Admission
```

`_RunFrame` 的拆分要等真实所有权稳定后再做：

```text
SessionContext：request/session/conversation/snapshot
ProviderContext：provider/provider_id/preflight/fallback/supervisor state
TraceContext：run trace / prompt trace / ledger trace refs
ResearchContext：record/ledger/proof/plan/critic refs
EvaluationContext：experiment/journal/transcript/provider-observation refs
```

判断规则：如果一个字段只被 ResearchPipeline 使用，就迁到 ResearchContext；
如果还被 chat/review/writer 共用，就先留在 frame 里。

### 0.4 A/B 节奏

0.4 后半不再按奇偶版本机械设置 A/B。判断标准是行为边界：

```text
改变模型可见 prompt / tool result / Router / provider fallback / permission -> A/B
只做 schema / projection / ledger / trace / deterministic validator / 文档 / 架构测试 -> 不 A/B
可选保存 provider prompt/reply transcript -> 只属于实验层，不进入生产 RunTrace/EvidenceLedger
```

判断标准：

```text
改 Research prompt -> A/B
改 critic prompt -> A/B
改模型可见工具结果 -> A/B
改 Ghost continuity 注入 -> A/B
改 AnalysisRun 对模型的 tool/result contract -> A/B
启用自动 bounded follow-up search -> A/B
纯 schema / persistence / projection / deterministic validator / 文档 / 架构测试 -> 不 A/B
```

如果某个版本最终只做 projection、schema 或 read model，不改变 prompt / tool result /
model-visible contract，也可以降级为 deterministic parity + local smoke，不强行 A/B。

0.4.2 已启用 queued research/open_question completion gate，改变 queue done 语义，
所以需要小型 Research/Ghost queue A/B。0.4.4 只要启用自动 bounded follow-up
search，就必须做 Research A/B。

0.4.6 是后续 A/B 的观测地基，不改变模型行为；它需要 durability / recovery /
replay smoke，不需要质量对照 A/B。0.4.7 如果只做 deterministic ReviewFinding
projection，不需要 A/B；一旦启用 model critic、改变 repair prompt 或改变最终报告
收口，就需要小型 live A/B。0.4.8 只做 context/capability metadata 不需要 A/B；
任何模型可见 context admission 变化都需要 A/B。0.4.9 如果只做本地 completion
contract/proof refs，不需要 A/B；改变 queued done 语义或用户可见完成条件时需要
queue A/B。0.4.10 改 Writer 可见 brief 文案需要 A/B。0.4.12 Ghost continuity
进入模型可见 prompt 必须 A/B。

0.4.13 之后，0.4 进入 A/B stabilization / release evidence baseline，不再把
新能力、目录迁移或大型抽象混进同一阶段。具体执行纪律、落盘 schema、失败归因、
逐 provider / 逐 arm 停跑规则和 0.4 完成门槛固定在
[`docs/0.4_ab_stabilization_plan.zh-CN.md`](docs/0.4_ab_stabilization_plan.zh-CN.md)。

## 0.4.0 - Evidence Kernel / Research Object Model v1

状态：已落地。目标是让每次 Research 产出结构化对象，而不是只有 summary、note
和临时 ledger payload。v1 已按 deterministic projection 落地，不改 Research prompt、
工具 schema、模型可见 tool result、UI/SSE shape、Router、provider fallback 或权限。

### 做什么

新增稳定对象模型：

```text
ResearchQuestion
ResearchSource
ResearchEvidence
ResearchClaim
ResearchAssumption
ResearchClaimRelation
ResearchRecord
```

第一版先做 projection，不重写 ResearchRunner：

```text
research/ledger.py + ResearchRunResult
  -> research/object_model.py
  -> ResearchRecord
```

字段保持小而可审计：

```text
question_id
question_text_digest
answer_status
source_id
requested_url_ref
final_url_ref
host
title_digest
retrieved_at
content_hash
evidence_id
excerpt_digest
bounded_excerpt
claim_id
claim_text
claim_section
citation_numbers
evidence_refs
assumption_refs
relation_kind
locator
assumption_reason
unsupported_claim_count
run_id
session_id
project_ref
record_digest
```

### Claim Extraction Contract

`ResearchClaim` 不能只来自 `knowledge_write`。0.4.0 要先定义 claim 抽取合同，
后续 proof quality 和 critic 都依赖它：

```text
final report 结论 -> claim candidate
final report 关键证据 -> claim / evidence relation candidate
final report 反证与限制 -> refutes / limits relation candidate
knowledge_write evidence -> grounded evidence relation
ResearchRecord -> bounded claim graph projection
```

Claim graph 第一版支持：

```text
supports
refutes
updates
supersedes
conflicts_with
limits
```

0.4.0 v1 只自动生成：

```text
supports
refutes
limits
```

`updates`、`supersedes`、`conflicts_with` 等到 Evidence Ledger / longitudinal
tracking 有真实历史对象后再自动生成。

Claim evidence binding 和 relation direction 必须保守：

```text
同一 citation source 下的任意 evidence 不能自动支持所有 claim
只有 source_id 命中，且 final claim 与 evidence claim 或 bounded excerpt 匹配，才生成 supports relation
结论 / 关键证据段只接受 supports stance 的 evidence
反证与限制段只接受 contradicts / refutes / context stance 的 evidence
contradicts / refutes 在反证段生成 refutes relation，context 生成 limits relation
空 stance 可以按旧语义默认 supports，非空未知 stance 必须 fail-closed 为 unknown / unsupported
匹配不到时保留 citation_numbers，但 claim 仍然是 unsupported / assumption
claim.status 只表示 evidence_backed / unsupported / assumption
0.4.2 proof gate 必须以 relation_kind 判断支持、反证和限制方向，不能把 evidence_backed 当成主结论被支持
```

`answer_status` 必须从第一版就存在：

```text
answered
partial
insufficient_evidence
not_answered
```

这样 0.4.2 的 Proof Quality Gate 不会退化成“有 research:* 字符串就算完成”。

### 边界

- v1 不新增模型工具。
- v1 不新增 UI。
- v1 不改变 Research drawer / Run Details shape。
- v1 不改变 Writer prompt。
- v1 不把 Ghost memory 当 source。
- v1 不保存网页正文；只保存 hash、locator、短 excerpt 和 bounded metadata。
- requested_url_ref / final_url_ref 必须做 userinfo、token、client_secret、refresh_token、
  x-api-key、jwt、session_id、authorization、bearer、credential/session 变体和
  query-secret 后缀 redaction；query component 默认在 URL digest 前 fail-closed
  脱敏，不保留 raw key / value；畸形 URL / no-host URL / malformed userinfo head
  也不能让 digest 依赖 secret value。
- 本地项目和文件路径默认保存 `project_ref` / `path_ref`，不保存 raw absolute path。
- Run Trace 只能保存 `research_record:<16 hex>`、answer_status、计数和 record_digest，
  不能保存 raw question、raw report、raw URL、网页正文或 provider raw error。

### 顺手架构优化

```text
把 ResearchRunResult -> citation/evidence payload 的重复清理成 typed projection
保留 research.ledger 作为 per-run runtime ledger
新增 object_model.py，不能 import server/task_runner/provider/browser
为 proof quality 预留 claim/evidence/assumption 字段和测试
claim extraction 先做 deterministic projection，不新增模型工具
```

### 验证

```text
ResearchRecord id 在同一 run/input 下确定性稳定；跨 run 内容身份以后单独用 content digest
Source / Evidence / Claim id 稳定、可 hash
SearchResult 不能生成 Evidence
Ghost continuity 不能生成 Evidence
Evidence 必须来自 opened source
Claim 要么有匹配的 evidence_refs，要么标成 assumption / unsupported
同源多 evidence 不能全量支持任意 claim
反证 stance 不能支撑 conclusion / key-evidence claim
counter / limitations 段的 evidence-backed claim 方向必须只由 relation_kind 表达
非空未知 stance 不能 fail-open 成 supports
Assumption cap 后不能留下悬空 claim/relation refs
Claim graph relation kind 必须来自固定枚举
Claim status 必须来自固定枚举 evidence_backed / unsupported / assumption
answer_status 必须来自固定枚举
final report 的结论 / 关键证据 / 反证段能生成 bounded claim candidates
RunTrace 只接受 research_record:<16 hex> id
URL secret key 变体必须脱敏，digest 不能依赖 secret value
query key/value 都不能进入 URL digest
畸形 URL / no-host URL / malformed userinfo head 也必须先脱敏再 digest
object_model.py 不 import TaskRunner / providers / browser / server
```

### A/B

已按只做 projection、claim extraction contract 和 deterministic tests 的范围落地，
不需要实机 A/B。只有实际改 Research prompt、tool result 或 report repair 路径时，
才做小型 A/B。

## 0.4.1 - Evidence Ledger v2

状态：已落地。目标是让证据长期可追踪，不只存在单次 ResearchRunner 内存对象里。

### 做什么

已新增 durable read model：

```text
research/identity.py
research/evidence_ledger.py
```

它记录：

```text
source identity
evidence identity
claim identity
assumption identity
evidence locator
run refs
session / project scope
schema version
content-addressed refs
```

0.4.1 v1 的落地边界：

```text
ResearchRecord append-only 持久化到本地 evidence ledger
session / project scoped ledger path
schema_version / kind 校验
source/evidence/claim/assumption/relation map
locator_id / locator_hash / page / char_start / char_end / locator
counts / ledger_ref / record_id 的 Run Trace 摘要
坏 ledger / 写入失败 fail-open，不影响 Research 完成
map cap 裁剪时优先保留最新闭合 record，丢弃更旧断链风险 record
append_record 只接受 typed ResearchRecord，不接受裸 mapping fallback
malformed typed record 被裁掉时返回 skipped，不报告成功写入
append_record 必须先在 candidate payload 上试写；新 record 未保留时不能覆盖或删除已落盘好 record
candidate payload 写盘前必须通过完整 canonical validation
load 已落盘 ledger 时也必须 graph-validate；schema 正确但断链的 ledger fail-closed 为 unavailable
load-time schema 必须是 allow-list；未知 raw 字段、孤儿 map entry、map key / entry id 不一致都 fail-closed
load-time 已知标量字段必须保持 canonical / bounded 形状
evidence.locator.source_id 非空时必须等于 evidence.source_id
```

0.4.1 先持久化 0.4.0 已有 locator。更细来源的完整 locator fixture 随
0.4.3 connector 和 0.4.5 AnalysisRun / Reproducibility Capsule 继续补齐：

```text
HTML: char_start / char_end / locator_hash
PDF: page / char_start / char_end / page_text_hash
table: table_id / row / column / cell_hash
CSV/TSV: row_id / column / value_hash
JSON: json_pointer / value_hash
local file: path_ref / line_start / line_end / file_hash
```

这些 locator 只保存定位信息、hash 和短 excerpt，不保存 raw source body。

### 边界

- per-run `ResearchLedger` 继续存在，负责工具循环内状态。
- `EvidenceLedger` 是长期只读投影和 append-only 更新，不接模型调度。
- 不改变 Research prompt。
- 不新增 UI。
- 不改变 SSE / task receipt / Router / provider fallback / PermissionProfile。
- 不保存 raw prompt、raw model response、raw URL、raw absolute path、完整来源正文或
  provider raw error。

### 顺手架构优化

```text
把 sanitize_research_url_ref / project_ref / path_ref / digest / stable_ref
抽成 research/identity.py，避免 object_model 和 evidence_ledger 复制 identity 规则
0.4.2 阶段 TaskRunner 只做薄接线：Research 完成后 append_record，再写 bounded
trace summary；0.4.4 起这段 Research persistence ownership 由 ResearchPipeline
闭环负责，TaskRunner 只消费最终结果和外围 trace/UI 生命周期
EvidenceLedgerStore 只消费 ResearchRecord，不读取网页正文或 Research prompt
```

### 验证

```text
ledger 文件有大小上限
schema_version / kind 校验
source/evidence/claim/assumption/relation id content-addressed 且稳定
evidence locator content-addressed 且稳定
retained records 的 source/evidence/claim/assumption/relation refs 必须全部可解析
claim.evidence_refs / claim.assumption_refs / assumption.claim_ref / evidence.source_id / evidence.locator.source_id 必须闭合
evidence.source_id 和 evidence.locator.source_id 不能指向不同 source
relation endpoints 必须落到 retained claim/evidence/assumption
超过 map cap 后最新 record 仍然 graph closed
Mapping 输入必须 invalid_record，不能让 nested raw URL / body-like 字段落盘
typed record to_jsonable 失败，包括 malformed nested object，必须 invalid_record，不能从 store 抛出
被 closure 裁掉的新 record 必须返回 record_pruned_for_ledger_closure
被 closure 裁掉且未写入新 payload 时，返回已加载 ledger 的既有 counts，不能暴露临时 payload counts
malformed replacement 不能删除已有同 record_id 的好 record 或其他好 record
malformed typed record 不能写出下一次 load 会判 unavailable 的 poisoned ledger
已落盘 ledger 不能携带 raw_url / raw_body / provider_raw_error / raw_prompt 这类未知字段
records 未引用的 orphan source/evidence/claim/assumption/relation map entry 必须 fail-closed
map key 必须和 entry 内部 id 一致
ledger_ref / session_ref / warnings / stance / status / host / bounded_excerpt 等合法字段值也必须 canonical
record.counts 必须等于当前 record refs 计数，unsupported_claims 不能超过 claims
source.content_hash 只能为空、16 位 lowercase hex 或 sha256:<64 hex>；malformed typed value 和伪 sha256 前缀不能原样落盘，也不能被重新 hash 后保存
digest ref 只接受 sha256:<64 hex>，伪 digest 必须重新 hash
坏 JSON、超大 ledger、schema 不符或 graph closure 不通过的 ledger fail-closed 到 unavailable，不影响普通 run
写入失败 fail-open，不影响 Research 完成
不会保存 raw prompt / raw model response / raw URL / raw path / raw source body
Run Trace 只保存 ledger_ref、record_id、counts、reason_code
TaskRunner 的用户可见 Research payload 不变
capability / event matrix 声明它是 quiet durable read model，不是模型工具或 UI
HTML / PDF / CSV / JSON / local file locator 的完整 fixture 随后续 connector/analysis 版本补齐
```

### A/B

不需要。实际落地是纯 identity / persistence / projection / deterministic tests，
没有改 Research prompt、模型可见 tool result、Router、provider fallback、权限、
UI 或 SSE。

## 0.4.2 - Research Proof Quality Gate + Planner Signals v0

状态：已落地。目标是解决 Research work item 的 proof 太弱的问题，并为后续
bounded Research Planner 产出第一版 deterministic gap signals。

现在 `research:*` proof 只能证明“有研究产物”。0.4.2 要证明：

```text
原问题被回答
答案有 citation
citation 来自本轮打开过的 source
citation 能定位到 source locator
关键 claim 有 evidence
final claims 覆盖 queued question 的关键词、实体、关系和约束
弱证据没有被写成强结论
没有把 assumption 写成事实
```

### 做什么

新增：

```text
research/proof_quality.py
research/completion_gate.py
```

核心结果：

```text
ResearchProofReview(
    ok
    answers_question
    answer_coverage_score
    coverage_gaps
    followup_questions
    query_rewrite_candidates
    citation_present
    citation_locator_verified
    support_relation_verified
    counterevidence_checked
    source_trust_warnings
    overclaim_warnings
    stale_warnings
    missing_evidence
    proof_ref
    question_digest
    record_id
    record_digest
)
```

新增 Answer Coverage Scorer：

```text
queued question
  -> key terms / entities / relations / constraints
  -> compare with final claim graph
  -> answered / partial / insufficient_evidence / not_answered
```

新增 Citation Integrity check：

```text
claim citation
  -> evidence ref
  -> supports/refutes/limits relation
  -> source id
  -> evidence locator
  -> locator source == evidence source
  -> durable EvidenceLedger record
```

接入：

```text
GhostWorkItem(kind=research/open_question) done
  -> ResearchCompletionGate
  -> ResearchProofReview
  -> complete only with research_proof:<16hex>
```

### 边界

- 不允许 Ghost 自己生成 proof。
- 不允许没有 ResearchRecord / durable EvidenceLedger record 的 research/open_question
  item 直接 done。
- 不要求每个普通 Research 都被阻塞；先重点约束 queue completion。
- 可以给出 deterministic follow-up questions / query rewrite candidates，但 0.4.2
  不自动执行递归搜索；自动递归研究要等 Planner 边界和 A/B 单独落地。
- source trust 第一版只做保守 warning，不做激进排序改写：官方/论文/一手资料优先、
  论坛/聚合/二手来源降权这类规则必须可解释、可测试。
- 错误文案要短、可修复、不中断普通 UI。
- Coverage scorer 第一版可以 deterministic + conservative，不要求语义完美；宁可 partial，
  不要把没回答的问题标 done。

### Deep Research 差距边界

Codey 当前 ResearchRunner 更接近 deterministic research pipeline：它有工具循环、
coverage、counterpoints、quality warnings 和 evidence ledger，但还不是
Open Deep Research 风格的 supervisor/researcher/think-tool 递归 planner。

0.4.2 不应该直接补完整 agent graph。正确顺序是：

```text
proof gate 先判断哪里没答好
输出 coverage_gaps / followup_questions / query_rewrite_candidates
Run Trace 只记录 bounded refs/counts/reason
后续 Research Planner v1 再决定是否后台补搜、并行探索或递归验证
```

这样 0.4.2 的收益会直接落在可靠性上，也为后续 Deep Research 式能力留下真实 seam：
`coverage gap -> follow-up research plan -> bounded research iteration -> proof review`。

### 顺手架构优化

```text
把 ghost/work_queue.py 里的 kind-specific proof 判断抽成 helper
Research proof validator 只读 ResearchRecord / EvidenceLedger，不读网页正文
TaskRunner 只调用 ResearchCompletionGate，不再自己判断 research proof 是否足够
不要在 0.4.2 大拆 TaskRunner.run；最多抽 Research proof/persistence 的薄边界
```

### 验证

```text
queued question 未被回答 -> cannot complete
queued question 只部分覆盖 -> partial / blocked
无 citation -> cannot complete
citation 不在 opened source -> cannot complete
citation 没有 locator -> cannot complete
locator excerpt/hash 不匹配 -> cannot complete
strong claim 只有弱 evidence -> warning 或 blocked
assumption 不得计作 evidence
conclusion/key-evidence claim 必须 status=evidence_backed 且 supports relation 指向自己的 evidence_refs
counterevidence / limitations 缺失不得 ok=True
answer_coverage_score bounded 且可解释
question_digest 进入 research_proof ref / RunTrace，但不保存 queued question 原文
research_proof ref bounded
legacy research:* 不能单独完成 queued research/open_question
RunTrace 只保存 proof summary / counts / reason codes，不保存 planner signal 原文
missing ResearchRecord 这类 blocked gate 也必须写入 proof_ref / question_digest / reason_codes，即使没有 record_id / record_digest
ok=True 的 proof review 必须带合法 record_id / record_digest；只有失败 review 可以省略
queued Research 已先记录过同一 proof review 时，completion gate block 不能留下重复 trace entry
```

### A/B

已启用 queue completion gate，所以 0.4.2 做小型 Research/Ghost queue A/B。2026-08-17
已按单 provider 原子落盘方式跑过 DeepSeek、Qwen、MiMo、StepFun、GLM：work queue
`research-item` 和 research-interest proof/no-proof gate 均通过。它仍然不需要大规模
provider/prompt A/B，因为没有改变 Research prompt、tool result、Router、provider
fallback、权限、UI 或 SSE。

## 0.4.3 - Source Connector Boundary + Query Planner Dry Run v1

状态：已完成（0.4.3）。本版落地 source connector 边界、ResearchPlan dry-run，
默认启用 PubMed/arXiv connector-aware search/fetch，并补上生产 `done` citation
compiler。planner 仍然只产出 dry-run 计划，不自动执行 bounded follow-up；
connector-aware search 只通过现有 Research 工具面暴露结果，不新增模型工具名；
done compiler 只做引用和 `来源` 收口，不替模型补语义支撑。

### 做什么

已新增内置 connector contract：

```text
SourceConnectorSpec
SourceHit
FetchedSource
SourceConnectorResult
SourceConnectorRegistry
ResearchPlan
QueryCandidate
SourcePreference
```

0.4.3 shipped fixture/local set：

```text
local_file
CSV / TSV
JSON
arXiv
PubMed
```

高杠杆 Connector Parity Pack 顺延为后续方向。它不是为了拼专业数据库数量，而是覆盖
通用研究最常见的来源：

```text
arXiv
PubMed
Semantic Scholar
Crossref
OpenAlex
EuropePMC
GitHub releases / issues
official docs
SEC EDGAR or FRED
local PDF folder
RSS feeds
```

0.4.3 的最小 connector fixture set 已经不是只落 catalog/unavailable，而是有
recorded fixtures 可用：

```text
local file
CSV / TSV
arXiv
PubMed
```

RSS 不作为 0.4.3 shipped 最小集 blocker。它对 changelog、blog、release feed 和
政策/安全公告仍然有价值，但现在不是用户最常直接点名的 Research 来源；可以先在
registry 里声明 optional/unavailable，或顺延到 0.5 之后的 Connector Pack v1。

OpenAlex 不放进 0.4.3 最小 fixture set。它更适合后续做 citation graph、
机构/作者/主题 discovery 和 source metadata enrichment；0.4.3 第一批先用
PubMed + arXiv 覆盖医学/生命科学和预印本/论文入口。

其余 connector 可以声明 unavailable，或顺延到 0.5 之后的 Connector Pack v1。
connector registry 从第一版就要能表达这些来源的 search/fetch 能力、rate limit、
source quality hint 和 failure mode。

0.4.3 按两层 release gate 落地：

```text
0.4.3a: registry + dry-run + recorded fixtures
0.4.3b: PubMed/arXiv connector hit 进入 Research search/open runtime 路径
```

0.4.3a 的 deterministic fixture/planner 测试已完成。0.4.3b 改变模型可见的
source selection，所以已做 connector smoke/A/B；默认启用只覆盖 PubMed/arXiv，
并且可以通过现有 browser/search fallback bounded 失败。

新增 Query Planner dry-run：

```text
ResearchPlan(
    question
    gaps
    query_candidates
    source_preferences
    max_depth
    max_queries
    max_sources
    reason_codes
)
```

Planner v0 只消费 0.4.2 的 `coverage_gaps / followup_questions /
query_rewrite_candidates` 和 connector registry metadata，输出 bounded plan；
不调用 web_search/open，不改变 ResearchRunner 主循环。proof 已通过且没有 gap 时，
planner 产出 no-op 的 `proof_ok_no_required_followup` plan，不再生成 follow-up query。

模型可见 controller action 面仍保持小，并避免重载同一个 open 动词：

```text
web_search
open_result
reopen_source
open_hit
source_search
knowledge_write
done
```

connector 细节由本地 runtime 处理，不暴露给模型。
PubMed/arXiv connector hit 已经通过现有工具面可达：`web_search` 返回稳定 locator，
`open_result` / `reopen_source` / `open_hit` 编译到 runtime open/fetch 后进入
opened-source ledger；connector hit 本身仍不是 evidence。
PubMed/arXiv live API query 必须从 shared safe terms 构造，raw secret、
`api key ...` / `password ...` / `client_secret=...` / `Authorization: Bearer ...`
这类 marker/value 窗口、URL 和本地路径不能出站；safe query 为空时跳过 connector，
只走 browser fallback。
Recorded PubMed/arXiv fixture parser 和 recorded fetch 还必须校验 connector-specific
host 和合法 source ID 形状；`SourceHit` audit metadata refs 必须过滤 secret-looking
值；`SourceConnectorResult.query_digest` 只能来自 sanitized query，safe query 为空时保持为空。
只实现 catalog 但没有 fixture 或真实工具路径的 connector，不能计入 shipped set。

生产 `done` 收口也在 0.4.3 落地：

```text
research/done_finalizer.py
source-id / numeric citation compiler
line-level source-section source-id gate
bounded RunTrace compilation summary
```

compiler 只编译可靠绑定：`[s1]` / `source_id=s1` 必须能映射到本轮已打开且有
evidence excerpt 的来源；旧数字引用必须能通过可解析 `来源` 行映射到 canonical
URL。重复旧编号如果指向不同 URL，直接交给质量修复；单来源且无歧义的编号漂移可以
顺手归一化。`来源` section 按行判定：真实来源标题里的 `[S1]` 不算内部 source id，
但非来源行里的 `note [s9]` 或来源行里的 `source_id=s9` 会被拦截。无可引用来源报告
会被重渲染成标准 section，前言不会进入最终答案。

### 边界

- 不做第三方 connector 插件。
- 不新增 connector UI。
- 不让 connector 改 prompt / Router / PermissionProfile。
- 本地文件 connector 必须走 ActionPolicy。
- URL connector 必须走 Research URL guard。
- Connector Pack 不能把每个来源变成一个新模型工具。
- connector failure 必须 bounded，不影响普通 web/html Research。
- 本地文件、CSV/TSV 和 local PDF connector 只允许来自用户显式选择的项目或文件范围。
- Query Planner dry-run 不能自动执行 query，不能把 plan 注入模型 prompt。
- Source trust 只能作为 plan metadata / warning；0.4.3b 只追加 PubMed/arXiv
  connector 结果，不引入复杂 ranking。
- browser/base search 先启动，connector lookup 只在短全局 budget 内补充结果；connector
  lookup 或 direct PubMed/arXiv URL fetch 失败时，必须 bounded fallback 到 browser 路径。
- done compiler 只做结构化引用编译和 `来源` 渲染；不能新增引用支撑，也不能把不可靠
  的 source-id 或旧数字编号猜成某个来源。

### 顺手架构优化

```text
把 browser_search / pdf_extract / source_document 的 fetch/read 输出收敛到 FetchedSource
SourceDocument 保留为模型无关的 normalized document
新增 research/query_planner.py，只产出 ResearchPlan，不执行 plan
新增 research/done_finalizer.py，只做 Research final report citation compiler
TaskRunner 不接 planner 分支；后续 0.4.4 再由 ResearchPipeline 消费 plan
```

### 验证

```text
connector id 稳定、snake_case
所有 connector 只读
connector registry 不 import server/task_runner/provider
Connector Pack id 集合稳定，未实现 connector 要明确 unavailable
fixture connector hit 必须有稳定 source_ref/source_id
进入真实工具面的 connector hit 必须能被 open_result/reopen_source/open_hit/source_search 打开或定位
local file 不能 escape workspace / allowed roots
private/local URL 仍然 deny
connector search hit 仍然不是 Evidence，必须 fetch/open 后才可引用
ResearchPlan id / query ids 稳定、bounded、可解释
ResearchPlan 不含 raw prompt / raw webpage body / raw absolute path
proof-ok/no-gap plan 只产出 no-op reason，不带无关 connector availability warning
planner dry-run 不改变 UI/SSE/prompt/tool result
PubMed/arXiv fetch 只接受 connector-specific public host
PubMed/arXiv fixture parser 拒绝 malformed source IDs
RunTrace 记录有界 connector fallback error summary，不保存 raw URL/query/error
普通 Research search 显式复用专用 Research profile/port；直接构造 BrowserSearchProvider 默认隔离
浏览器 CDP attach / port wait 上限保持 20 秒
TaskCancelled 不能被 isolated CDP 启动重试或 search page 导航重试吞掉
manual source connector A/B harness 必须走和生产 Research 一样的 non-isolated browser reuse
RunTrace 分开记录 controller_action_contract_hash 和 runtime_tool_contract_hash
planner 和 live connector 共用领域路由规则；unavailable/shipped/capability flag
必须约束真实 live path；connector budget 不得因 timeout rounding 超过 deadline；
safe scientific slash terms、path-like slash token 负例、PubMed ID、redaction marker/shape
和 neutral transport metadata 由测试锁住；常见 RAG/NLP/retrieval/benchmark 论文检索词
必须能路由到 arXiv connector。
Qwen provider 必须在 composer 可交互且未生成中才填入消息，点击后不得因为响应信号慢而
重复整轮发送，发送前不依赖固定 settle 窗口。
done compiler 必须不重绑混用 source-id/numeric 的引用，不改 `[2nd]` 这类非 citation
文本，不让来源标题中的 `[S1]` 触发泄漏误判，且要拦截 heading 前言、报告正文、无来源
报告和 `来源` 自由文本里的内部 source-id 泄漏。
```

### A/B

0.4.3a 不需要 provider A/B，因为它只做 registry / projection / planner dry-run /
recorded fixtures。0.4.3b 默认启用 PubMed/arXiv
connector-aware search/fetch，所以必须做 live connector smoke。

如果 connector 接入改变 search ranking、fetch 内容、tool result 文案或 source
selection，就做 connector parity smoke/A-B，因为这已经改变 Research 的模型可见输入。

0.4.3a 的测试重点是 planner 是否能在合适场景产出 PubMed/arXiv source preference
和 bounded query candidate。0.4.3b 的 live smoke/A-B 按行原子落盘：
DeepSeek、Qwen、MiMo、StepFun 和 GLM 逐个 provider 跑 PubMed、arXiv 和
opened-source guard；失败行只补跑缺失样本，不重跑已有成功行，避免同时打开多个
provider 浏览器页面。实机结果支持把 connector-aware PubMed/arXiv search 作为默认
路径启用：DeepSeek 的 PubMed 目标来源选择明显改善，MiMo/StepFun connector arm 能打开
PubMed 目标 host，Qwen 在 provenance 修复后改善 arXiv，arXiv 目标 host 在多个 provider
样本中可达。限制也记录在案：多条 run 仍停在 `max_turns` 或 protocol repair，GLM
PubMed 重测因 provider 限流暂停，所以这版 A/B 只作为 source-selection smoke，不宣称
proof-quality 全模型提升。done-stage A/B 只支持把 finalizer 作为格式收口器合入生产：
DeepSeek、MiMo、Qwen 的样本能把首次 `done` 通过率从“需要质量修复一轮”压到“一次过”，
但不把它解释成 proof quality 或 connector selection 的提升。新 controller action
`open_result` / `reopen_source` / `open_hit` 也通过这些实机样本验证，旧的
`open_url(result_id/source_id/hit_id)` 不再作为模型可见协议。

## 0.4.4 - Bounded Research Planner v1

状态：已完成（0.4.4）。目标是让 Codey 在 proof gap 明确时，能在后台做有限补搜、
验证和再综合，而不是停留在一次流水线。

### 做什么

新增：

```text
research/context.py
research/pipeline.py
research/plan_executor.py
research/evidence_followup.py
research/record_merge.py
```

Bounded Evidence-Only Pipeline & Deterministic Merge v1：

```text
ResearchProofReview (with actionable gap)
  -> ResearchPlan
  -> PlanExecutor (fresh-material search/fetch with baseline deduplication)
  -> Evidence-Only Follow-up (single turn, knowledge_write only, URL whitelist)
  -> merge_evidence_patch (deterministic evidence graph merge + citation re-index)
  -> proof review again
  -> final ResearchRunResult
```

硬限制：

```text
max_followup_rounds = 1
max_queries_per_round = 3
max_sources_per_query = 2
max_total_sources = 6
reason_codes
stop_reason
```

### 边界

- 只有用户已经显式开启 Research 时，planner 才能补搜。
- Planner 不能绕过 PermissionProfile / ActionPolicy / URL guard。
- Planner 不能调用没有 connector contract 的来源。
- Planner 不能把 Ghost hint 当 evidence。
- Follow-up 阶段通过内存 Staging（`StagedKnowledgeStore` / `StagedKnowledgeChanges`）隔离副作用；具备完整 read-through 读穿透与补偿回滚能力；staged link 端点按普通 note id/title 解析，未被选中的补搜结果零写入主知识库与 changes。
- Follow-up 模型交互严格限制为单轮单动作，仅允许 `type='fact'` 的 `knowledge_write`；只接受 `type/title/body/sources/evidence` 这组最小参数面，`sources` 必须是非空 URL list，`evidence` 必须是非空 object list，且每条 evidence 必须显式使用 `source_url`；拒绝 `tags`、`relations`、`aliases`、`status`、自定义 id 等普通写入侧通道；严密校验来源归属（Provenance），提取的 `evidence[].source_url` 必须属于当前 note 声明的 `sources` 列表并属于允许的白名单；`PlanExecutor` 自动对重定向目标 URL 去重，并在 fresh-source 总预算已满时先停止、不再多打一轮 search，杜绝重复打开与预算浪费。
- Accepted 候选方案在 commit 阶段具备异常安全与补偿回滚护栏（`followup_commit_error`），若写入中途抛出异常，自动逆序清理已落盘文件（若已有 note 发生 folder/path 移动则删除新路径文件并字节级还原旧路径文件内容与时间戳，统一使用 `content_hash_bytes` 保持索引哈希防漂移）、恢复触及 staged/link endpoint note 的 SQLite links 关系，并通过公开的 `KnowledgeChanges.snapshot()` / `restore_snapshot()` 边界恢复 changes 状态，平稳回退并保留 initial 成功结果。
- 最终 ResearchRecord 由 `record_merge` 确定性合并与重新编号，实现严格的全段落（结论/证据/反证）citation 存在性与有效性校验，彻底过滤未引用或包含未映射悬空编号（如 `[99]`）的行；对 protocol/not_answered 但 staged ledger 已有 evidence 的候选，以及正文剪枝后没有有效结论/证据行的候选，直接从 evidence-backed claim、重新生成的中文 `来源质量` 与 `搜索覆盖` 构造最小报告，不继承旧模型段落；来源行解析复用 report-quality 的统一 citation parser，不在 merge 层维护第二套 Markdown 正则；同步全量元数据（`queries`、包含完整 `query/opened/final_url` 的 `search_results`、`notes_created`、`notes_updated`、`counterpoints`、稳定排序的 `source_urls`）并透出最新观测指标（`fresh_source_count`、`new_evidence_count`、`final_evidence_count`，以及无论候选方案是否被选中均完全可持久化审计的 `attempted_fresh_source_count`、`attempted_new_evidence_count`）。
- Planner 不设 wall-clock 质量 gate，执行时间仅作成本诊断指标；达到查询/来源有界预算必须停止并解释 gap。
- UI 不新增 planner 面板，不自动弹窗；Run Details 只显示 bounded summary。
- Run Trace 只记录 plan_ref、counts、stop_reason、gap refs，不保存 raw query transcript。

### 顺手架构优化

```text
抽 ResearchPipeline v1：负责 ResearchRunner + proof gate + planner retry 的编排
research/query_planner.py 保持 plan 生成；research/plan_executor.py 只执行 plan
TaskRunner 只调用 ResearchPipeline，不再拼 proof/planner 分支
_RunFrame 暂不拆；只把 research-only 状态迁入 ResearchContext
Provider/session/trace lifecycle 仍由 TaskRunner 管
```

实现约定：

```text
ResearchPipeline 是 Research 生命周期唯一编排 owner
TaskRunner 只构造 context、注入外围生命周期依赖并消费最终结果
_run_research_iteration 是单轮 Research primitive，不是兼容旧调用方的公共入口
ResearchIterationRun(result, tools) 只在 pipeline 迭代边界传递运行态工具
ResearchRunResult 不携带 runtime_tools 等隐式运行时依赖
search 的创建和关闭由 ResearchPipeline 统一管理
deterministic merge 不虚增 ResearchRunResult.turns；follow-up 成本通过 pipeline metrics 表达
```

这次没有保留 `_run_research_task`、`close_search` 或 `runtime_tools` 兼容层；
测试和手工 harness 直接 patch 当前主 seam。这样可以避免冷启动项目为了迁就旧
测试接口，把单轮 primitive 和最终 ResearchResult 混成一个隐式协议。

### 验证

```text
coverage gap 足够明确 -> 生成 bounded plan
coverage 达标 -> 不补搜
max rounds / queries / sources 生效
follow-up search 仍走 Research URL guard
follow-up evidence 必须来自打开/读取过的 source
follow-up 产生 iteration record / ledger candidate，不能直接 mutate final ResearchRecord
最终 ResearchRecord 必须从最终报告重新 projection
planner stop reason 可解释
RunTrace 不含 raw prompt / raw webpage body / raw query transcript
UI/SSE 既有字段不变；`task_done.research` 可追加 bounded pipeline metadata
ResearchPipeline deterministic fixture 可回归
ResearchPipeline 不依赖 TaskRunner、Server 或 provider adapter
单轮迭代工具状态不进入最终 ResearchRunResult
每次 Research pipeline 只写一次最终 EvidenceLedger record
```

### A/B

需要。只要启用自动 bounded follow-up search，就改变 Research 行为，
必须做小型 Research A/B，并记录 citation quality、answer coverage、
unsupported claim rate、UI interruption count、provider 流量和
follow-up usefulness。0.4.4 的 `bounded_research_planner_ab.py` 还必须按
provider send/reply 原子落盘，并成对记录 baseline -> planner 的 coverage、
unsupported claim、new sources/evidence、query/fetch/send/time delta，便于分析
prompt、协议、额外流量和最终回答质量的关系。`useful=true` 必须是保守口径：
baseline/planner 两行都成功、follow-up 实际执行、有新增 source/evidence、质量侧
有改善，并且 coverage/status/unsupported-claim/score 没有明显回退。

当前 harness 的 baseline arm 关闭 follow-up；planner arm 直接调用生产
`run_evidence_followup()` 和生产 `record_merge`，只保留 fixture material-phase
executor 用来控制隐藏来源暴露。2026-08-21 的 evidence-only3 trace 已回放到当前
生产 `run_evidence_followup()`：DeepSeek、MiMo、Qwen、StepFun、GLM 的 follow-up
reply 都符合显式 `{"tool":"knowledge_write","args":{...}}` schema，并各写入 1
条新 evidence。

生产合入后的 DeepSeek / Qwen / StepFun `widget_noop` 成对复跑继续使用同一生产
follow-up 路径。DeepSeek 从 score `5 -> 6` 且 `useful=true`；Qwen 也从
`5 -> 6` 并新增 1 个 source/evidence pair，但 unsupported claim rate 从
`0.333 -> 0.750`，所以按保守口径为 `useful=false`。StepFun 取到隐藏 fresh
source，但最终仍是 protocol/not-answered，candidate 未被选中，score `1 -> 1`。
这说明 0.4.4 的默认路径可以保留，但发布前仍要看真实 connector-backed case 和
provider 间的 unsupported-claim / protocol 稳定性。

## 0.4.5 - AnalysisRun + Reproducibility Capsule v1

状态：已完成（0.4.5，metadata / projection / local capsule）。
本版落地的是执行审计底座：`research/analysis_run.py`、`research/artifact_lineage.py`、
`research/reproducibility.py` 三个纯投影模块 + Run Trace 三个有界 section +
TaskRunner 统一的 `_handle_project_tool_event()` 缝隙。字段做了减法：v1 不含
`script_hash`、`dependency_fingerprint`、git 字段和 `sanitized_argv`（Windows 引号语义
无法用标准库可靠解析，等真实消费者出现再定义）；`reproduction_status` 只报告
`output_captured` / `output_not_captured` / `failed`。让 Research 报告引用
`analysis_run:<id>` 的要求推迟到后续版本——那会改变模型可见报告契约，
按 A/B 规则需要小型实机验证；v1 只记录内部支撑关系。

原始目标保持不变：让 Research 可以做可复查的本地分析，而不是只总结网页；
同时让报告、表格、图和分析输出有最小 lineage。

评审加固：`command_display` 对 secret-looking 命令脱敏（digest 仍为权威事实）；
只有带执行 timing 的真实执行才投影成 AnalysisRun（policy deny / cwd 非法 /
command not found 不进 trace，timeout 记为诚实失败）；managed output audit
透传 `stored_truncated`；`tool_id` 收紧为 UI/runtime instance id（`turn:index`），
`tool_name` 单独保留工具名；derived ref 收紧为形状校验，`derived_from` 只接受
list/tuple，artifact lineage 记录必须同时有合法 `artifact_id` 与 `version_id`；
候选选择的字典序 tuple 排序替换为显式 `ResearchCandidateScore` dataclass。

### 做什么

新增：

```text
research/analysis_run.py
research/reproducibility.py
research/artifact_lineage.py
```

记录：

```text
analysis_run_id
tool_id
tool_name
command
cwd_ref
input_refs
output_refs
log_handle
exit_code
script_hash
input_hashes
output_hashes
dependency_fingerprint
environment_fingerprint
git_commit
git_dirty
rerun_command_ref
sanitized_argv
command_digest
reproduction_status
started_at / finished_at
artifact_refs
```

Reproducibility Capsule：

```text
capsule_id
analysis_run_refs
input_refs
output_refs
artifact_refs
environment_ref
reproduction_status
warnings
```

最小 Artifact Lineage：

```text
artifact_id
version_id
version
artifact_kind
sha256
size
mime
origin_run_id
produced_by
capture_quality
created_at
input_refs
derived_from
```

Research 报告中的分析结论必须引用：

```text
analysis_run:<id>
```

v1 范围注记：这条要求推迟到后续版本。v1 只在 Run Trace 中记录内部支撑关系，
`analysis_run:<id>` 不进入模型可见 prompt / report；让模型写这个引用会改变
报告契约，按 A/B 规则届时需要小型实机验证。

### 边界

- 0.4.5 v1 只记录已有执行 / Managed Outputs 的 metadata。
- 如果新增模型可调用 analysis tool、自动分析，或改变模型可见 AnalysisRun
  tool/result contract，必须做小型 A/B。
- 不新造执行器，复用 ToolRuntime / ActionPolicy / Managed Outputs。
- 不开放任意 shell；仍走现有 run/shell approval 边界。
- 不把 raw stdout 塞进模型上下文；长输出走 managed output handle。
- 不做 artifact manager UI 或文件浏览器。
- artifact 仍然是本地 handle，不塞进模型上下文。
- Planner 可以建议本地分析，但不能绕过权限或自动执行危险动作。

### 顺手架构优化

```text
把 run command 的 audit metadata 和 managed output metadata 投影成 AnalysisRun input
复用 0.3.17 action policy decision refs
新增 Reproducibility Capsule helper，只处理 metadata，不接管执行
managed_outputs.py 继续负责存储和大小边界
research/artifact_lineage.py 只负责 metadata projection
```

### 验证

```text
analysis input/output/log 都有 bounded refs
失败分析不能支撑 claim
cwd/script_hash/input_hashes/output_hashes/exit_code 可审计
dependency/env fingerprint 有大小上限
rerun_command_ref / sanitized_argv / command_digest 经过 bounded display 和 action policy digest
完整复现命令只在用户显式 export artifact 时包含
git dirty state 只记录摘要，不保存 diff
raw stdout 不进入 trace / UI
workspace escape deny
artifact id / version_id 稳定，不能静默覆盖旧版本
artifact derived_from 只能引用 Source/Evidence/AnalysisRun/Run
坏 artifact metadata fail-open 到 unavailable，不影响模型 bounded result
```

### A/B

不需要，前提是只做 metadata / projection / local capsule，不改变模型可见
AnalysisRun tool/result contract。若让模型看到新的分析工具结果或自动执行分析，
顺延做小型 A/B。

## 0.4.6 - A/B Observation Journal + Transcript Replay Cache v1

状态：已完成（0.4.6，manual 实验层）。实际 scope：新增共享
`tests/manual/ab_journal.py`（ABJournalWriter/ABJournalReader/TranscriptReplayCache、
hash chain、tail recovery、identity fail-closed、按 event type 的 typed observation
facts schema），
并迁移 `bounded_research_planner_ab.py` 与 `source_connector_ab.py` 删除各自的
LiveTrace/原子写/send-reply 记录；`deep_research_core_ab.py` 的迁移推迟到后续
harness 版本。落盘结构为 `<stem>.trace/manifest.json + events.jsonl +
transcripts/<digest>.json`，transcript 默认 digest_only；archive 模式有显式
delete/prune helper。架构测试锁住：生产层
（run_trace/research/task_runner/server）不得 import journal，journal 不依赖生产
编排层，transcript 不能进入 EvidenceLedger/ObjectModel。

原始目标保持不变：先把 live A/B、provider observation 和可选 transcript replay
的事实层收住，避免后续 critic A/B、Ghost continuity A/B、longitudinal harness 和
provider adapter debug 继续各自复制 LiveTrace、原子写、send/reply 记录和断点续跑
逻辑。

这不是生产 RunTrace，也不是 Evidence Ledger。它是手动实验层的 durable journal：
可以保存 Codey 发给网页模型的 prompt 和网页模型返回的 reply，但 raw transcript
必须隔离在可选 TranscriptArchive 中，不能混进 RunTrace、Prompt Envelope、
Evidence Ledger、ResearchRecord 或 Citation/Evidence refs。

### 做什么

第一版已落在：

```text
tests/manual/ab_journal.py
```

如果出现两个以上真实消费者，再迁到：

```text
codey/evaluation/ab_journal.py
```

核心对象：

```text
ABJournalManifest
ABJournalEvent
ProviderTurnEvent
ProviderObservationEvent
CaseEvent
ABResultRow
TranscriptReplayCache
```

落盘结构：

```text
tests/manual/results/<run-id>.json
tests/manual/results/<run-id>.trace/
  manifest.json
  events.jsonl
  transcripts/
    <content-digest>.json
```

`manifest.json` 和 snapshot 类状态继续用 `local_store.write_json_atomic()` 的
temp + fsync + replace。事件流不要每次重写大 JSON；使用 single-writer
append-only JSONL：

```text
seq
ts
run_id
experiment_id
case_id
arm
provider
model
event_type
stage
prompt_digest
reply_digest
content_ref
failure_kind
facts
previous_digest
event_digest
```

每个 journal path 必须只有一个 writer；如果可能有多进程 harness，就必须用 lock
或显式 `writer_id` 拒绝并发写。单行 append 后 flush/fsync；读取时允许最后一行
损坏并截断恢复。这里的语义是 durable append + tail recovery，不承诺跨进程强
atomic append；hash chain 用来发现断链、重复 append、非尾部篡改和跨 run 混写。

Provider observation 只保存 typed facts：

```text
send_start
input_filled
submit_clicked
generation_started
reply_observed
timeout
adapter_failure
case_complete
```

网页状态来自 `FlowObservation` / `ProviderFailure.kind` / `stage` / bounded facts：

```text
input_empty
question_count_increased
response_count_increased
typing_true / typing_false
stop_visible / stop_hidden
response_stable
response_nonempty
response_chars
profile_hash
failure_kind
failure_stage
```

### Transcript 分层

```yaml
RunTrace:
  只存 digest / metadata / refs，不存 raw prompt、完整聊天或网页正文。

Visible UI history:
  只服务用户看得见的聊天恢复和继续会话。

TranscriptArchive:
  可选保存 provider prompt / reply 全文，用于 A/B replay、adapter debug 和离线分析。
```

TranscriptArchive 必须是本地可关闭功能，带大小上限、retention、删除入口、
digest 索引和 content_ref。0.4.6 已提供显式 `delete_transcript()` 和
`prune_transcripts()`，不做后台自动保留策略。旧 transcript 可以作为 prior observation / cache / hint，
不能成为 evidence、fresh source、citation、ResearchRecord source 或 completion proof。

### 边界

- 不接生产 UI。
- 不改变模型 prompt、tool result、Router、provider fallback 或权限。
- 不抓 DOM、Cookie、网页历史、用户在网页上已有的旧聊天或 provider raw error body。
- 不把 raw transcript 写入 RunTrace / EvidenceLedger / ResearchRecord / PromptEnvelope。
- 不把 transcript replay 当作 live A/B；改模型可见 prompt 或 tool result 后仍需少量 live A/B。
- provider 是否出问题靠 typed provider events，不靠聊天正文猜测。
- transcript 内容默认不进入后续 prompt；需要 replay 时只进入 manual scorer/replay harness。

### 顺手架构优化

```text
把 bounded_research_planner_ab.py / source_connector_ab.py / deep_research_core_ab.py
重复的 LiveTrace、原子写、send/reply event、case resume 逻辑收敛到共享 journal
统一 ABResultRow schema，减少每个 manual harness 自定义字段
把 provider observation 从临时内存事实投影成 bounded durable observation
```

### 验证

```text
events.jsonl hash chain 可验证
最后一行损坏可以恢复
重复 seq / 断链 / 跨 run 混写会被拒绝或标 warning
manifest 原子写不留下半文件
TranscriptArchive 关闭时只保存 digest/ref
TranscriptArchive 开启时 raw prompt/reply 不进入 RunTrace/EvidenceLedger
旧 transcript replay 不能生成 evidence/citation/completion proof
provider observation 不含 DOM/Cookie/raw webpage body
未知 provider observation fact 字段 fail closed，不能靠 value heuristic 过界
manual harness 中断后能从 last completed case resume
```

### A/B

不需要质量对照 A/B。0.4.6 不改变模型行为，只需要 durability / recovery /
replay smoke。后续所有 live A/B 默认复用这个 journal。

## 0.4.7 - Evidence Runtime + ReviewFinding Core v1

状态：已落地（0.4.7，deterministic projection + bounded trace）。实际 scope：
新增 `research/evidence_runtime.py`（全部 runtime ref 的统一校验入口 +
EvidenceRuntimeSnapshot 读模型）和 `research/review_finding.py`
（ReviewFindingRecord / PlannerGap / append-only finding lifecycle）；
`proof_quality.py` 在 hard-failure reason code 不变的基础上新增带 refs 的
ProofDiagnostic；Run Trace 新增 `research_review_findings` /
`research_planner_gaps` 两个有界 section（cap 16，只存 refs 和 reason codes）；
ResearchPipeline 只在 final proof review 后投影一次并写 trace，planner 行为、
prompt、tool result、报告契约全部不变。artifact lineage 的 derived ref 校验
委托共享 validator（行为不变），收掉一处真实重复。model critic 未启用，
按 A/B 规则本版不需要实机验证。

原始目标保持不变：把 Research、AnalysisRun、Artifact、Review 和 planner gap
放到同一套证据引用语义里，再在其上建立 ReviewFinding lifecycle。先做
deterministic projection；model critic 只作为后续 A/B 候选，不默认启用。

### 做什么

新增或收敛 Evidence Runtime projection：

```text
Source
Evidence
Claim
Assumption
AnalysisRun
ArtifactRef
ReviewFinding
PlannerGap
CompletionProof
```

新增：

```text
research/review_finding.py
research/evidence_runtime.py
```

输出：

```text
ReviewFinding(
    finding_id
    kind: unsupported_claim / citation_mismatch / stale_source / overreach /
          missing_counterevidence / contradictory_sources / source_conflict /
          failed_analysis_support / qualified_support
    severity
    status: open / addressed / confirmed / rejected
    claim_ref
    evidence_ref
    source_ref
    analysis_run_ref
    artifact_ref
    reason_codes
    addressed_by
    confirmed_by
    message
)
```

Planner feedback projection：

```text
unsupported claim -> follow-up question
stale source -> refresh query
weak source -> stronger source request
missing counterevidence -> counterevidence search gap
citation mismatch -> locator verification gap
failed analysis support -> rerun / replace evidence gap
```

Finding lifecycle：

```text
open
  -> addressed
  -> confirmed
```

`confirmed` 必须来自后续 verification event，来源可以是 deterministic check、
successful AnalysisRun、opened-source evidence 或 reviewer pass，不能来自模型自称
“已修复”。

### 边界

- Evidence Runtime 不保存 raw prompt、raw webpage body、raw stdout/stderr 或 transcript content。
- ReviewFinding 只读 Evidence Runtime。
- deterministic critic 不能调用 web_search/open_url。
- model critic 默认不进生产链路；启用前必须走 0.4.6 journal A/B。
- finding 可以产出 planner gap，但不能自己执行 search/fetch。
- failed AnalysisRun 不能 support claim。
- TranscriptReplayCache 不能成为 evidence source。

### 顺手架构优化

```text
report_quality.py 逐步从格式检查变成 deterministic Evidence Critic layer
ResearchPipeline 只消费 finding refs 和 planner gap refs
review_coordinator 消费同一套 ReviewFinding projection，不重复解析 Research summary
finding 状态变更只追加事件，不原地覆盖旧 finding
```

### 验证

```text
unsupported claim 被抓出
citation number mismatch 被抓出
过期 source 给 stale warning
过度推断给 overreach warning
contradictory_sources / source_conflict 被抓出
counterevidence search 缺失会产生 finding
failed AnalysisRun 不能支撑 claim
critic finding 能生成 planner gap refs
planner gap refs 不含 raw prompt / raw webpage body / transcript
open finding 不能被模型自称修复直接 confirmed
model critic 输出坏 JSON fail-closed 到 deterministic result
```

### A/B

不需要，前提是只做 deterministic projection 和 local finding lifecycle。启用 model
critic、改变 repair prompt、改变 final report contract 或让 finding 影响模型可见
输出时，必须做小型 live A/B，并使用 0.4.6 journal。

## 0.4.8 - Safe Context Epoch + Capability Boundary v1

状态：已落地（0.4.8，metadata-only projection + trace）。实际 scope：新增
`codey/context_epoch.py`（纯 stdlib leaf 投影：`ContextEpoch` /
`ContextAdmission` / `ContextSnapshot`、content-addressed `ctx_epoch:<16hex>`
id、`context_source_ref()`（空/不可用 key fail closed）、单一共享投影
`admission_from_rendered_source()` 同时供 snapshot 与 RunTrace context source
行使用；无 I/O、不 import 任何 codey 模块，架构测试锁死）；
`ContextSource` / `RenderedContextSource` 增加 `capability_id` /
`admission_reason` 元数据（默认空，渲染行为与 prompt 字节不变）；prompt
envelope section 增加同样的三个可选字段；新增共享
`record_provider_send_prompt()`，把 agent / server / task_runner / research
runner / consensus 共 9 处重复的 provider-send trace 记录收敛为一个入口，并
补上 conversation rollover 内部 summary prompt 的 digest-only provider-send
行；所有这些行都会盖上 provider_send freshness、epoch id 和固定 admission reason。
provenance 闭环：project_intro 先渲染最终 prompt 算出 epoch，再把该 turn 的
envelope sections、context source 行（`record_context_sources(...,
epoch_id=...)`）和外发 prompt 绑到同一个 `ctx_epoch:` id 上；工具结果轮的
coding_current_context 行延迟到发送时绑定，rollover 整体替换 prompt 时丢弃
未发送的 prepared 行。chat 外发 prompt 带 `chat_runner` provenance，rollover
summary prompt 带 `conversation_handoff` provenance。
epoch id 标识 turn 内容而非编号调用：相同字节重复发送共享 id 并去重，字节
差异产生新 epoch。Run Trace 的 `PromptSectionTrace` 增加可选 epoch / admission /
capability 字段，只在有值时序列化，其余 manifest 形状不变。Capability
Registry v1 补全 roadmap 字段（`trace_sections` / `context_sources` /
`evidence_producer` / `enabled_by_default`），补登记 0.4.7 的
`research_evidence_runtime` / `research_review_finding` 与本版的
`context_epoch` / `conversation_handoff` / `chat_runner` /
`consensus_advisors`，并给 agent_runner /
local_context / policy_guard 等补事实归属；架构测试锁定生产代码里出现的
每个 capability_id 引用都必须是注册能力。model critic、planner 消费
finding、插件系统、skill 加载全部未做。按 A/B 规则本版不需要实机验证。

原始目标保持不变：吸收 OpenCode 的 provider-turn 边界和 Pi 的 core 克制：
上下文变化只在安全 provider turn 边界进入模型；capability registry 只描述
内置边界，不演变成插件系统或大配置平台。

### 做什么

新增或收敛：

```text
ContextSource
ContextEpoch
ContextSnapshot
ContextAdmission
CapabilityBoundary
```

每个模型可见 section 必须有：

```text
stable source key
purpose
budget
source refs
digest
admission reason
epoch id
```

Context sources：

```text
Project instructions
Local context
Research brief
Review input
Ghost continuity
Skill guidance
Provider health hint
```

Capability Registry v1 只声明内置能力事实：

```text
capability id
model_visible
durable_state
policy_profile
evidence_producer
trace_sections
ui_projection
enabled_by_default
```

Pi-style skill/extension boundary：

```text
skill descriptions 可以进入 guidance
完整 skill instructions 按需读取
LSP / MCP / browser / subagent / domain connector 都是 optional capability
默认不扩大 core tool surface
```

### 边界

- 不加载第三方代码。
- 不做插件市场。
- 不新增 profile/config UI。
- 不改变用户显式 provider / mode。
- 不放宽 PermissionProfile。
- Context source 变化不能唤醒 idle session。
- Context 只能在 safe provider-turn boundary admission，不能异步插入当前 provider turn。
- Capability Registry 不是 dispatcher、permission engine 或 Router。

### 顺手架构优化

```text
PromptEnvelope section metadata 和 RunTrace section metadata 复用 stable source refs
Local context / Research brief / Review input / Ghost continuity 共享 ContextSource contract
Capability Matrix 从文档事实变成可测试 registry projection
```

### 验证

```text
context source key 稳定且不重复
context admission 只发生在 provider-turn boundary
model-visible section 都有 source refs / digest / budget
capability registry 不加载第三方代码
capability 不能 relax PermissionProfile
skill body 不默认进入 prompt
context snapshot 不保存 raw prompt / raw source body
```

### A/B

不需要，前提是只做 metadata、trace 和 deterministic admission tests。任何模型可见
context 拼接、section 文案或 admission 时机改变，都需要小型 A/B。

## 0.4.9 - Research Contract Lite + Verified Completion Gate v1

状态：已落地。0.4.9 按"验证而不是扩充"的思路实现：contract 是纯投影，
不是模型工具，也不是 CompletionManager。共享原语在 `codey/completion_contract.py`
（纯投影 leaf，只复用 `codey/refs.py` / `codey/redaction.py` 的领域中立
有界身份 helpers）：
`CompletionContract` / `CompletionCheck` / `CompletionProof`，硬门槛派生
（fail -> failed；required-but-not-run -> blocked；pass + limitations ->
complete_with_limitations；否则 complete）。v1 只有 clean `complete` 才是
`satisfied=True`；`complete_with_limitations` 必须保留为非 satisfied 的受限完成，
避免后续 enforcement 把"未完全验证"误当成 clean proof。satisfied proof
永远不携带 blocked_reason，junk 输入 fail closed。research 侧投影在
`codey/research/contract.py`：ReviewFinding 的 open critical finding 会阻止
clean complete（结构性等价：critical finding 都是 hard proof failure 的投影，
因此 queued research 的完成结果与 0.4.8 完全一致，不需要 A/B）。

### 做什么

新增：

```text
completion_contract.py      共享原语（domain-neutral 纯投影）
research/contract.py        research contract 投影
```

CompletionContract（v1 落地字段；v1 不设独立 Requirement 对象，
requirement 与 check 恒为 1:1，两个平行列表是重复状态）：

```text
contract_id
domain                      coding / research / experiment
subject_ref
checks[]                    check_id + status(pass/fail/not_run/not_applicable) + reason_code
evidence_refs
limitation_refs
finding_refs
analysis_run_refs
artifact_refs
external_refs               ledger:/receipt:/diff:/research:/research_proof:
```

CompletionProof（失败、blocked、受限也落 proof，status 决定是否 satisfied）：

```text
proof_id
contract_id
status
satisfied
blocked_reason              仅在未 satisfied 时存在
reason_codes
checks summary
refs only
```

Verified Completion Gate（v1 落地范围）：

```text
Queued research done -> ResearchProofReview -> contract proof refs（外部行为不变）
Coding done + changed files -> shadow completion proof（trace-only，不改 done/receipt/prompt）
Research conclusion -> Evidence / Source / Claim relation
Experiment conclusion -> AnalysisRun / ArtifactRef（producer 仍缺，proof 留位）
```

契约状态：

```text
pending
running
blocked
complete
complete_with_limitations    必须携带 limitation_refs
failed
```

### 边界

- 不是模型工具。
- 不新增 workflow UI。
- 不替代 ResearchPipeline。
- 不让模型自己给 completion 盖章；coding 侧"未本地观察到验证事实"最多
  complete_with_limitations（verification_not_locally_observed），不能凭模型
  自述 clean complete。
- 不保存 raw transcript、raw prompt 或 raw source body。
- TranscriptReplayCache 可以帮助离线 scorer，但不能成为 contract proof。
- Contract 不能绕过 ActionPolicy、PermissionProfile 或 Research max rounds。

### 顺手架构优化

```text
completion_gate 的 _blocked_reason 字符串语义移入 research/contract.py 投影
safe_run_ref 上移到 completion_contract.py（research/coding 共享一个 run-ref 清洗器）
通用 ref 词表正式中立化：codey/refs.py + codey/redaction.py 两个 stdlib leaf，
  research/identity.py 只留 URL/project/path 特有 helper，全部 importer 更新、无 shim
task_runner 的 select_verification_candidate + check_covers_selected_candidate 收敛为单一求值点，
  receipt 判定与 shadow proof 共用同一份事实；proof 另用三态 fresh_pass/fresh_fail/unobserved
  表达本地验证新鲜度，receipt 的既有覆写语义保持不变（改它属于需要 A/B 的行为变化）
unobserved 永不升格成 failed：checks_passed 假值可能是默认/被 edit 重置，
  只能 blocked(verification_not_locally_observed)；failed 保留给真实观察到的失败
analysis_run_refs 只引用决定性命令（覆盖 selected candidate 且决定状态的那次），
  并用与 AnalysisRun 投影同源的 project-relative path digest 做 cwd-aware 匹配
contract_id 哈希覆盖全部 refs 组（finding/analysis/artifact/external），
  proof_id 由 contract_id 派生、trace 按 proof_id 去重，content-address 必须完整
capabilities 登记 metadata-only completion_contract（model_visible=False）
ResearchCompletionGate 产生的 research CompletionProof 会写入 RunTrace completion_proofs，
  成功和 blocked 路径都可审计，不只把 proof_refs 写回 queue item
RunTrace 新增 bounded completion_proofs section（proof row cap 8，per-proof check cap
  与 CompletionContract 共用 MAX_COMPLETION_CHECKS=12，refs-only）
RunTrace 不信任 raw satisfied；satisfied 只能由 status 派生，避免
  status=failed/satisfied=true 这类不一致 payload 污染 trace
RunTrace raw mapping 入口强制 contract 形状：没有有效 checks 的 proof 丢弃；
  complete_with_limitations 必须带有效 limitation_refs
```

### 验证（v1 已测）

```text
缺 evidence refs 的 strong claim 不能 complete
failed AnalysisRun 不能 complete experiment check（projection 就绪，producer 待接）
unsupported open finding 阻止 clean complete
complete_with_limitations 必须列出 limitation refs
complete_with_limitations 不是 satisfied；未来消费者必须以 status == complete
  判断 clean completion，不能把 satisfied 当作"足够完成"的宽松别名
contract proof refs 可解析（completion_contract:/completion_proof: 16hex）
contract 不含 raw prompt / transcript / webpage body
queued research done 不能绕过 contract gate（gate 行为与 0.4.8 等价）
queued research completion proof 成功/失败都写入 completion_proofs
satisfied proof 永远不携带 blocked_reason
RunTrace 从 status 派生 satisfied，忽略 raw mapping 里的 satisfied
CompletionContract 与 RunTrace 的 per-proof check cap 保持一致
RunTrace raw mapping 不能用空 checks 或缺 limitation_refs 的
  complete_with_limitations 绕过 CompletionContract 约束
coding docs-only change 不要求 code test（not_applicable + docs_only_change）
read/search 型 tool event 不会把未跑验证误记成失败（unobserved 三态锁定）
unobserved + checks_passed=False 只能 blocked，不能 failed（False 可能是默认值）
fresh_fail proof 只携带决定性失败命令自己的 AnalysisRun ref（cwd 不同不串引）
任何 refs 组不同 => 不同 contract_id/proof_id（全字段 content-address 锁定）
refs.py / redaction.py 是纯 stdlib leaf（架构测试锁定）
```

### A/B

local-only contract/proof refs、shadow coding proof、trace-only section 都不需要 A/B：
prompt、planner、done/receipt 语义全部不变。后续让 proof 阻止 done、把 completion
failure 自动回给模型 repair、或改变 queued done / 用户可见完成条件时，必须先用
0.4.6 journal 做 queue/live A/B，核心指标看 False Completion Rate。

## 0.4.10 - Domain Source Trust + Research Brief v2

状态：已落地（0.4.10，catalog + deterministic projection + bounded trace）。实际 scope
比规划更克制：`research/domain_profiles.py` 落地 `EvidenceProfile`
证据标准向量 + 六个原子 profile + 按维度 merge（无组合 profile、无继承、
unknown label 回落 general + warning）；`research/source_trust.py` 落地
16 类 source class taxonomy 的 deterministic projection，并把 proof review 的
source-trust 警告规则收编为单一实现；`research/brief_projection.py` 落地
refs-only brief 投影 + Research-to-Code impact contract（unsupported claim
只能进 risk_notes）；RunTrace 新增 `research_source_trust` /
`research_brief_projections` 有界 section。planner 只新增可选
evidence_profile 参数（默认行为 byte-identical）。

Release 前 hardening：Evidence ledger 的 record capsule integrity 不只在 load
时校验，也在 append 时阻止共享 map 同 id 不同 canonical 内容改写旧 capsule
输入；source row 区分身份字段（已知 final URL ref、host、content hash、
content kind）和观测字段（retrieved_at、pages_read、truncated、quality hint），
合法重复抓取合并观测字段，身份冲突 record 以 `ledger_id_collision` 跳过，
旧 payload 不重写。

Groundwork 边界：profiles/impact contract/render_handoff 目前只有测试和
trace 消费；生产路径尚不应用 profile。Writer 可见 handoff 文案已切换为
共享 `report_sections` 解析的结构化短摘要（raw excerpt 与 related-id 移除、
长行截断不丢弃），启用该文案的 release 必须先跑专用 live A/B：
`tests/manual/research_to_code_ab.py`（baseline=0.4.9 风格渲染 vs
projection=结构化渲染，确定性评分含 unsupported 陷阱 claim 误用检测；
退出码即 gate 判定；默认哈希链 journal + transcript 归档可复盘，
两臂顺序按 repeat 交错）。profile 参与 planner 偏好、以及任何
enforcement 仍需各自 A/B 后再启用。

原规划目标：把不同领域的证据标准、来源可信度和 Research-to-Code handoff
收束到同一个 refs-only projection 里。Domain rules 不单独做 UI，也不提前变成
Router/provider fallback。

### 做什么

Domain evidence profiles：

```text
general
science
finance
legal
market
software_research
```

Profile 只声明：

```text
source_quality_threshold
freshness_expectation
counterevidence_required
primary_source_preference
analysis_required_when_data_claim
source_trust_rules
preferred_connector_kinds
disfavored_source_patterns
```

Source trust v1：

```text
official / primary source
peer-reviewed / preprint
dataset / filing / standard
repository / issue / release
news / secondary source
forum / social / aggregator
unknown
```

Research Brief v2：

```text
claim_refs
evidence_refs
assumption_refs
analysis_run_refs
artifact_refs
open_question_refs
impact_refs
proof_review_refs
planner_gap_refs
review_finding_refs
contract_refs
```

Research-to-Code Impact Contract：

```text
affected_files
implementation_constraints
test_suggestions
risk_notes
out_of_scope_items
decision_refs
```

Writer 看到的是短 handoff，不是 raw vault：

```text
what was concluded
why it is supported
what remains uncertain
which gaps were checked or deferred
what files / implementation choices this affects
```

### 边界

- 不新增 profile selector UI。
- 不覆盖用户选择的 provider / mode。
- 不放宽 PermissionProfile。
- 不自动联网 Research。
- Source trust 不能直接删除 evidence，只能影响 warning、planner preference 或 proof 阈值。
- 不把整个 KnowledgeStore 注入 Writer。
- 不把网页正文或 transcript 注入 Writer。
- Research Brief 不能授权工具。
- unsupported claim 只能进入 uncertainty/risk，不能进入 confirmed constraint。

### 顺手架构优化

```text
builtin_profiles.py 保持 metadata-only
research/domain_profiles.py 只做 quality rule catalog
research/source_trust.py 只做 deterministic source trust projection
knowledge/brief.py 改成消费 Evidence Runtime / Contract refs
减少从 note body / citation map 反解析的逻辑
新增 impact projection，不让 Writer 自己从长报告里猜实现约束
```

### 验证

```text
finance freshness 更严格
legal primary source preference 更严格
science citation/source quality 更严格
planner source_preferences 随 domain rule 变化但 bounded 可解释
profile 不参与 Router/provider fallback/permission
brief 有大小上限
brief 不含 raw webpage/source body/transcript
brief 中 refs 可解析
unsupported claim 不进入 confirmed section
impact refs 必须来自 verified supports relation / declared assumption with risk_note / AnalysisRun
test_suggestions 不能授权工具，只能作为 Writer context
```

### A/B

catalog / metadata / deterministic projection 不需要 A/B。启用 enforcement、改变 proof
gate/critic 阈值/repair 路径，或改变 Writer 模型可见 handoff 文案时，需要 eval/live
smoke；影响模型输出时做小型 A/B。

## 0.4.11 - Longitudinal Research Harness + Comparison Benchmark v1

状态：已完成（deterministic v1；未宣称现实正确性证明）。0.4.11 落地的是评测
脊柱：固定 fixture 套件、纯 read-model 回归门（`codey/research/regression_gate.py`）、
纵向研究 harness、comparison benchmark、manual A/B 共用层。它只证明 Codey
观察到了什么、验证了什么、哪些指标没有回退；"surpassed OpenScience" 措辞由
代码门禁控制，必须存在真实 head-to-head artifact 才允许出现。
本版默认不联网；发布前只补了一轮有限 Qwen provider smoke 来检查既有 live
harness 的 provider/journal 路径。`research_to_code_ab.py` Qwen smoke 通过；
`bounded_research_planner_ab.py` 的 paired Qwen smoke 暴露了 provider 状态问题
（planner row 第一次 send 后 Qwen Studio 卡在原生网页搜索、无模型回复），
planner-only 重跑通过。`longitudinal_research_harness_ab.py` 和
`research_comparison_benchmark_ab.py` 在 0.4.11 仍是 deterministic-only，没有
provider/live 模式。

### 做什么

新增 deterministic fixture + optional live smoke：

```text
同一主题多轮 Research
旧 claim 重新验证
新 source 更新 freshness
stale evidence 被标记
AnalysisRun 可复查
ResearchProofReview 能解释为什么 done
ResearchPlan 能解释为什么补搜或停止
Planner follow-up 不造成 UI interruption
AB Observation Journal 能 replay scorer / parser / proof review
TranscriptReplayCache 能节省 deterministic scorer 流量
```

新增 Comparison Benchmark：

```text
Codey vs baseline web model
Codey vs OpenScience-style fixture suite
Codey vs real OpenScience manual head-to-head
```

对照维度：

```text
Pi dimension:
  default tool surface 是否变小
  skill progressive disclosure 是否有效

OpenCode dimension:
  context admission 是否只发生在 provider-turn boundary
  tool output 是否有界且可 replay
  provider/session events 是否可复查

OpenScience dimension:
  evidence refs 是否 verified
  provenance graph 是否闭合
  review finding 是否能 lifecycle closure

Codey dimension:
  coding / research / analysis / verification 是否进入同一个 evidence loop
```

评测维度：

```text
answer coverage rate
claim grounding precision
citation locator precision
counterevidence coverage
unsupported claim rate
stale update correctness
reproducible analysis rate
research-to-code handoff quality
longitudinal topic tracking score
planner gap closure rate
planner unnecessary follow-up rate
UI interruption count
```

适合固定任务集的主题：

```text
行业 / 公司 / 金融指标连续追踪
论文主题进展追踪
开源项目生态变化追踪
政策 / 法规变化追踪
本地 CSV / PDF 混合分析
unsupported claim 注入测试
stale source 注入测试
conflicting source 注入测试
```

### 边界

- harness 不是后台自动任务。
- 不默认联网。
- 不新增 UI。
- live smoke 手动触发，不占日常 provider 流量。
- 不要求自动运行 OpenScience；但发布“surpassed OpenScience”结论前必须做一次
  real OpenScience manual head-to-head。
- 没有真实 OpenScience run 时，只能标记为 OpenScience-style regression。
- benchmark 输出只保存 bounded metrics、refs 和摘要；raw prompt/reply 只能进入
  可选 TranscriptArchive，不能进入 benchmark summary、RunTrace 或 EvidenceLedger。

### 顺手架构优化

```text
把 AB Journal / Evidence Runtime / Contract / Brief / Proof Quality / Critic
串成一个可回归测试的端到端 read model
```

### 验证

```text
同一 topic 的旧 claim 能被重新定位
stale source 能被标记
新 evidence 能修订旧 conclusion
unsupported old claim 不会被带入新 brief
Codey 在固定 fixture 上通过 Regression Gate
Planner 能提升 answer coverage / counterevidence coverage，且 unsupported claim 不上升
Planner unnecessary follow-up rate 低于阈值
需要宣称超过 baseline / OpenScience 时，必须满足 Superiority Gate
real OpenScience head-to-head 记录 version/commit/provider/model/task/date/artifact/rubric
live smoke 输出不含 raw prompt / raw webpage body
TranscriptArchive 关闭时 live smoke 仍可生成 digest-only metrics
```

### A/B

需要 deterministic A/B / self-test。生产代码实机 A/B 不需要，因为 0.4.11 不改
prompt、tool schema、Research 默认路径、Writer handoff、planner 默认路径或
done 行为。live smoke 只用于诊断 provider/journal 路径；要宣称超过 baseline /
OpenScience 时，才需要正式 comparison / head-to-head。

## 0.4.12 - Ghost Research Continuity + Topic Planner v1

状态：已完成（0.4.12）。目标是让 Codey 可以连续追踪长期研究主题，并把开放
问题转成 topic-level plan，但不让记忆污染事实。发布证据见 `TEST_REPORT.md`；
真实 provider smoke 只证明 provider/journal 路径和失败归因，不宣称统计显著性、
现实正确性或超过 OpenScience。

落地形态比原规划更薄：

```text
codey/research/topic_continuity.py   纯 read-model projection（stdlib-only leaf）
  interest_hints + bounded Ghost continuity items + prior claim refs
    -> TopicContinuityItem / TopicPlannerCandidate / TopicContinuityProjection
    -> bounded prompt_text + digest-only payload

TaskRunner._build_research_topic_continuity()
  permission gate（research profile 只放行 research_topic_continuity）
  -> candidate_to_topic_hint()（knowledge 层自有的中立投影）
  -> build_ghost_continuity().selected_items（bounded items，不读 raw store）
  -> evidence ledger claim_refs（只取 refs，全部永久 stale）

TaskRunner._build_research_context()
  ResearchContext.topic_continuity_context / topic_continuity_payload

ResearchRunner._intro()
  ContextSource -> profile allow-list -> render_context_sources_with_metadata
    -> research_topic_continuity prompt section
  _send_provider()（发送边界统一投影）
    -> record_context_sources(..., epoch_id=context_epoch_id(outbound))
  （controller 追加 action block 后字节才定稿，sections / admitted sources /
   outbound prompt 共享同一个按发送字节计算的 provider-turn epoch）

ResearchPipeline 初始 iteration 只转发 bounded text；
RunTraceRecorder.record_research_topic_continuity 写入真实的
`research_topic_continuity` manifest section（digest 去重，refs/counts/codes
only，无原始文本字段；必填且格式校验的 `ctx_epoch:<16 hex>` `epoch_id`
无默认值，空/畸形 fail closed 不写 row，projection sink 也不暴露 continuity
writer，admitted row 结构上只能来自 runner 的发送边界绑定）。
projection 异常 fail-open 回空 baseline，并在 run trace 留下
`research_topic_continuity_projection_failed` warn code。
```

没有新增 TopicManager / TopicStore / ResearchContinuityRuntime；`research/`
四个核心模块（context/pipeline/runner/topic_continuity）不 import Ghost。
prompt 使用独立 `research_topic_continuity` section，不复用 follow-up 的
`research_iteration_context`；文案直说 not evidence / re-check / do not cite，
且不出现 Ghost、Work Queue、Concept Graph 内部词。

### 做什么

Ghost 提供：

```text
topic refs
open question refs
previous claim refs
user preference hints
stale evidence reminders
topic plan candidates
```

Research prompt 只接收 bounded continuity：

```text
This is local context, not evidence. Re-check sources before making factual claims.
```

### 边界

- `GhostHint != Evidence` 必须是类型和测试边界。
- Ghost 不能生成 citation。
- Ghost 不能让 Research 自动联网。
- Ghost 不能把旧结论当本轮事实。
- UI 不显示 Ghost / Memory / Work Queue 等内部词。
- Topic Planner 只能建议“下次该继续研究什么”，不能后台自动联网执行。
- 旧 claim 进入 planner 时必须带 stale/risk 标记，不能进入 evidence_refs。
- Ghost continuity 只能作为 ContextSource，经 Safe Context Epoch admission。
- live smoke 必须区分 provider 状态失败和 planner 质量失败；Qwen 这类原生网页
  搜索卡住时，只能记为 provider/journal 诊断，不得当作 Research planner 证据。

### 顺手架构优化

```text
把 Research Interest Queue 的 open_question refs 映射到 Evidence Runtime question refs
Ghost work queue completion 只接受 Contract proof refs
抽 TopicContinuityService：只产出 topic/open_question/stale refs，不读 raw note body
ResearchPipeline 消费 bounded continuity refs，不直接读 Ghost store
```

同周期加固（已落地，详见 CHANGELOG）：

```text
shell approval 续跑不再吞 Stop；/api/new_chat、/api/changes/restore 加 busy guard
codey/ghost/numbers.py 统一全部 Ghost store 的 finite unit-float 校验（bool/NaN/inf 拒绝）
Ghost work 手动 requeue 重置 retry_count，MAX_WORK_RETRIES 阻塞项可重新认领
StepFun fallback 改为 provider 本地 newest-first 扫描，不走通用 tail-first locate_response
override worker 使用每个 provider 稳定的专属 profile；父端保留有界 stderr tail
providers/web_driver.py 统一五个 web wrapper 的 deadline 覆盖与 response_missing 归类
evidence excerpt 超 360 字时保持精确匹配文本；删除单来源 citation 静默推断
```

仍延后：

```text
provider profile 增加 response_order = newest_first | oldest_first 元数据，
通用 locate_response() 按 profile 决定扫头还是扫尾
抽取 ghost/store_common.py：JSONL event-store / projection rebuild /
compact / quarantine / bounded warnings 基础件给 hebbian 与 affinity 共用
（本轮已落地第一片 numbers.py）；等"是否改变排序"的观测数据足够，
再评估把学习公式简化成朴素 priority hint projection
ghost_research_continuity_ab.py 可再抽共享 seed fixture helper（当前 seeding/
proof helpers 与确定性测试有受控重复；评测脚本层，无生产影响）
```

### 验证

```text
Ghost hint 不可进入 evidence_refs
旧 claim 必须重新验证或标 stale
topic plan candidate 不会自动触发 Research
Research continuity prompt 不出现 Ghost 内部词
disable Ghost 后 Research 行为回到 baseline
Ghost continuity admission 受 Safe Context Epoch 约束
provider live smoke 至少能把 native-search stuck / send_error 与 Codey tool loop 区分开
```

### A/B

需要。它改变了 Research prompt 的模型可见 continuity，因此 release 证据必须
至少包含 deterministic/self-test 和 narrow provider smoke 或明确的 provider-state
诊断；更大的统计 A/B、真实 OpenScience head-to-head 仍只在需要发布相应 claim
时执行。

## 0.4.13 - Verified Completion Enforcement + Repair Context Admission v1

状态：已完成（0.4.13）。live provider A/B（control_done / proof_only_block /
repair_context 四臂）是 0.4 收尾打磨和证明净收益的下一道门，harness 见
`tests/manual/completion_enforcement_ab.py`。目标是在 0.4.9 的 shadow
completion proof 和 0.4.11 的 harness 证据足够稳定之后，才让 completion
proof 第一次影响 coding 行为：阻止明显未验证的 `done`，并把最小的失败事实
作为下一轮 repair context admission。

实现落点（与规划一致）：

- `codey/completion_verification.py`：coding verification 分类从
  task_runner 抽成纯投影（tri-state、provenance、proof 构建、确定性失败
  分类）；TaskRunner 只收集事实和接线。
- legacy `checks_passed` 继承债已拆成显式 provenance：
  `stance = fresh_pass / fresh_fail / inherited_pass / unverified` 与
  `source = local_run / checkpoint / none`。inherited pass 保持 receipt 绿色
  但 proof 记录 limitation（`inherited_verification_not_fresh`），不再被当作
  本轮 clean verification fact；模型自报 pass 不再产生任何事实。
- `codey/completion_repair_context.py`：纯 projection leaf，只消费已生成的
  CompletionProof payload（不 import completion_contract），admit 仅限
  failed + product_failure 且必须存在筛后幸存的 decisive check facts
  （否则以 `refused_no_safe_check_facts` 拒绝 admit）；输出 facts-only
  提示文本 + digest-only trace payload；`minimal` detail 用于压缩对照臂。
- admission 走 `ContextSource -> profile gate -> ContextEpoch ->
  PromptEnvelope`（fresh intro 与普通 continuation 都字面经过 envelope）；
  trace row `record_completion_repair_context(payload, *, epoch_id)` 无默认
  epoch、fail closed，绑定 outbound send bytes。
- RunTrace 新增 trace-only 的 `protocol_telemetry` 区块：按 phase 记录
  codec 身份与 contract hash、按 kind 的协议错误/repair prompt 计数、可解析
  plan 的 valid turns；未知工具只落 digest + 可选安全短标识符，raw 文本无
  字段。四个 `record_protocol_*` recorder 接入 coding writer 与 research
  runner，纯观测，release A/B 结果因此更可解释。
- enforcement 决策点位于 writer/review 收尾之后、receipt/ledger/project
  facts 之前；最终 outcome 唯一驱动 durable state；`max_repair_rounds = 1`；
  repair turn 预算就是共享的剩余预算，用尽即以 `turn_budget_exhausted`
  blocked，绝不越界多发；停止条件显式枚举（unobserved / max_repair_rounds /
  turn_budget_exhausted / environment_failure / provider_failure /
  repair_context_unavailable / repair_not_admitted），blocked 是诚实
  stop_reason。失败分类按行首锚定、带 reason code 的闭合签名词表识别
  环境/依赖类失败——签名必须位于诊断行的行首（剥掉 runner 横幅与小写工具
  名头部后），每次匹配给出 reason code 与决定性短语，仅引用这些词的断言
  差异仍判 product failure；changes 收集产生不了可用结论而本地观察到
  edit 时按观察证据划 scope，实测净空 diff 保持 run 在 scope 之外。
- 无 RepairManager / CompletionManager / 新工具 / critic / 多轮 scheduler；
  架构测试锁死 projection leaf 边界与 payload 词表。

这不是新工具、新 critic 或新 repair framework。它只把已经存在的本地事实闭环起来：

```text
Tool execution
  -> Verification facts / AnalysisRun
  -> CompletionCheck
  -> CompletionProof
  -> done enforcement or repair context admission
```

### 前置条件

必须先完成：

```text
0.4.9 shadow proof 三态准确：fresh_pass / fresh_fail / unobserved
CompletionProof id 覆盖所有 proof refs，避免 content-addressed proof 漂移
coding proof 能引用相关 AnalysisRun / artifact refs
Receipt verification provenance 减债：在 enforcement 前把 legacy
  `checks_passed` 继承逻辑拆成显式来源字段（例如 fresh_pass / fresh_fail /
  inherited_pass / unverified 与 local_run / checkpoint / none），冷启动不保留
  无意义兼容；旧的 inherited pass 不能被当作本轮 clean verification fact
0.4.11 harness 能衡量 False Completion Rate
```

如果这些条件不满足，本版本只能继续保持 trace-only。

### 做什么

Verified Completion Enforcement：

```text
模型尝试 done
  -> CompletionProof.status == complete
      -> 允许 done
  -> complete_with_limitations / failed / blocked / stale verification
      -> 不立刻宣布完成
      -> 生成 bounded repair context
```

Repair Context Admission：

```text
admit:
  current failed check summary
  relevant AnalysisRun ref and bounded command status
  fresh blocking ReviewFinding refs
  current CompletionProof reason_codes

reject:
  resolved findings
  stale epoch facts
  unrelated research evidence
  raw stdout/stderr
  raw prompt/reply
  full diff/source body
```

上下文进入模型时仍走 0.4.8 的 ContextAdmission / ContextEpoch / PromptEnvelope
边界；不能新增独立 ContextManager、CompletionManager 或 RepairManager。

### 边界

- 不新增工具。
- 不新增 model critic。
- 不新增自动多轮 repair scheduler。
- 不让模型自证 completion。
- 不把所有 ReviewFinding 变成 ContextSource。
- 不把 CompletionProof 变成评分器。
- 不保存 raw prompt、raw transcript、raw stdout/stderr、raw diff 或 raw source body。
- repair context 只能解释失败事实，不能替模型决定修复方案。

### 顺手架构优化

```text
把 coding completion proof 的 verification 分类从 task_runner.py 抽成纯函数
CompletionProof / RunTrace 共用同一套 proof-ref identity helper
把 run_ref sanitizer 从 research/contract.py 移到 domain-neutral projection helper
repair context admission 复用 ContextSource / ContextEpoch，不新增 framework
```

### 验证

```text
模型说 tests passed 不能通过 enforcement
stale verification 不能 clean complete
fresh failed AnalysisRun 阻止 done，并进入 repair context
fresh passed relevant check 允许 done
docs-only change 仍可 complete_with_limitations
complete_with_limitations 不等价 clean complete；是否允许用户可见 done 必须在
  A/B treatment 中显式定义
resolved finding 不进入 repair context
repair context 不含 raw stdout/stderr/prompt/transcript/source body
prompt 变化只发生在明确 A/B treatment 中
```

### A/B

需要。它会改变用户可见 `done` 行为，并把 completion failure 重新送回模型。
必须复用 0.4.6 journal 做 control/treatment，对照：

```text
control: 旧 done 行为
treatment: Verified Completion Enforcement + repair context admission
```

核心指标：

```text
False Completion Rate
task success
repair count
verification success
time to completion
token usage
latency
user interruption count
```

## 0.5 主线 - Durable Runtime + Local Adaptation + Protocol Portability

0.4.15 到 0.4.19 是 0.4 进入 A/B stabilization 前的安全、证据和命名语义收口：

```text
run command argv 文件系统 operand 边界闭合
provider override 安装面收窄到 adapter repair surface
本地 read-modify-write 状态引入 locked JSON mutation
manual A/B manifest / journal / failed-row 续跑语义统一
Ghost affinity 不再把 ref 数量当 reward 强度
Ghost Affinity / Work Queue event log 只接受 canonical event shape，畸形本地记录 fail closed
NetworkStatus.POLICY_ALLOWED 替代容易误解的 PUBLIC_WEB 命名
```

这些改动不改变模型可见 prompt，不新增 TaskRunner/core facts 抽象，目标是让
0.4 后续 live A/B 有更干净的安全边界和证据基线。剩余长期问题进入 0.5：

```text
Research untrusted source wrapper -> 0.5.3 prompt-surface / injection hardening
TaskRunner convergence point -> 0.5 横向架构线，按真实 phase 抽 RunOperationState / EffectLog
Trace/Ledger/Proof/Evidence 概念收敛 -> 0.5 横向架构线，先定义不变式再抽象
Provider/protocol outcome learning -> 0.5.4，不回流 evidence / permission / completion verdict
```

剩余 review finding 的版本归属固定如下，避免后续把架构债混进 A/B 修复：

| finding | 0.5 归属 | 文档原则 |
| --- | --- | --- |
| Research source content 进入模型后的 prompt-injection 边界 | 0.5.3 prompt surface；先 A/B，再改默认生产渲染 | source content 是 data，不是 instruction；wrapper 不能降低 evidence quality |
| TaskRunner 继续作为 subsystem convergence point | 0.5 横向架构线；按 phase 抽 RunOperationState / EffectLog | 拆 state transition，不新增 Manager |
| RunTrace / Ledger / Evidence / Proof / Review 概念数量偏多 | 0.5 横向架构线；先定义 source-of-truth 不变式 | Action / Observation / Artifact / Verification / Decision 优先，projection 后置 |
| Provider / protocol outcome 学习 | 0.5.4 | outcome hint 只能影响 repair strategy / diagnosis，不能成为 evidence、permission 或 completion verdict |
| World Model | 0.5.7 / 0.5.8 | belief / prediction / calibration 都不是 truth confidence |
| 本地 read-modify-write 状态并发写保护 | 0.5.0 / 0.5.1 继续推广 locked mutation | atomic write 不是 transaction；event append / projection rebuild 要有临界区 |
| command/action 语义 IR | 0.5.2 起作为 protocol portability 地基 | command allowed 不是 command safe；cwd 在项目内不代表 argv operand 在项目内 |
| NetworkPolicy allow 语义 | 0.4.19 已收口，0.5 只维护 regression | policy allowed 不是 public-internet proof；调用方判断允许访问用 `decision.allowed` |

已在 0.4.15 收口的 provider override、CDP session lifecycle、CI matrix、README
结构文档问题，已在 0.4.16 收口的 Ghost event-log canonical ingestion，以及已在
0.4.19 收口的 NetworkPolicy 允许状态命名，不再作为
0.5 能力项；后续只在 regression test 或 release evidence 中维护。

Research untrusted source wrapper 的顺序必须是 A/B-first：

```text
1. 写 deterministic malicious-source fixture 和 scorer，先跑旧生产 prompt 作为 baseline。
2. 增加 manual Research A/B arm，只改变 source-content rendering，不改 planner/tool/runtime。
3. baseline + treatment 都落 result JSON / journal / transcript，确认 injection 命令没有变成 tool action。
4. 只有 treatment 不降低 evidence quality / source coverage / completion honesty，才改默认生产渲染。
5. 改生产后重跑 deterministic tests + 同一 Research live smoke，作为 release 证据。
```

也就是说，不先把 wrapper 直接塞进生产 prompt；先用 A/B 证明它减少 prompt-injection
风险且没有明显损害 Research 质量。

### 0.4.x Stabilization Track

0.4.18 之后的 0.4.x 不再继续堆系统。它只允许三类改动：

```text
1. A/B 证据链更完整
2. A/B 暴露的真实 bugfix
3. 不改变模型可见文本的安全/卫生收口
```

不做：

```text
RunOperationState
EffectLog
ReplayPolicy
World Model
TaskRunner 大拆
插件化 / hooks / lanes
默认 prompt 大改
```

版本节奏固定为：

```text
0.4.19：A/B evidence polish + 非模型可见安全/命名卫生收口（已交付）
0.4.20：Coding / Completion A/B bugfix，第一轮 DeepSeek core 已交付；后续只修 transcript/journal 证明的真实问题
0.4.21：Research / Ghost A/B bugfix，第一轮 DeepSeek provider baseline 已冻结；只修 evidence 污染、stale、citation、stall 归因问题
0.4.22：Qwen coding/completion、Research、Ghost core smoke 已收口；最终 0.4 report 已产出
```

0.4.19 的交付边界：

```text
tests/manual/ab_harness_common.py
tests/manual/ab_journal.py
tests/manual/completion_enforcement_ab.py
tests/manual/research_to_code_ab.py
tests/manual/bounded_research_planner_ab.py
tests/manual/ghost_research_continuity_ab.py
docs/0.4_ab_stabilization_plan.zh-CN.md
TEST_REPORT.md
```

已收紧：

```text
固定 --output 续跑语义
同 provider / suite / arm / case 重跑替换旧 row，而不是 append 污染 summary
旧 row 只在新 row 原子写入时被替换，不在 pending / provider connect 阶段提前删除
result JSON / journal / transcript / manifest 互相有 ref
journal 打开后外层失败会写 terminal failed run_complete
transcript 只有 archive 文件真实存在时才算 replayable
journal 只记录事件，不决定 arm verdict
BrowserWorker stuck 只做 passive health observation，不自动重启
显式 mode= 原子写权限应用失败时 hard fail，preserve_mode 仍 best-effort
Ghost Work Queue 状态迁移矩阵成为唯一 transition authority
NetworkStatus.POLICY_ALLOWED 避免把 policy allow 误读成公网证明
```

0.4.20 只跑并修 Coding / Completion 核心；第一轮 DeepSeek
`control_done` / `proof_only_block` / `repair_context` /
`repair_context_minimal` 已作为 release evidence 落盘，extended arms 只有在
对应 harness 具备 result / journal / transcript / manifest 绑定后才进入
release-grade A/B：

```text
control_done
proof_only_block
repair_context
repair_context_minimal
read_before_edit
scoped_task_plan
verification_review
impact_guard
```

允许修的生产问题仅限：

```text
false completion 漏过
unobserved 被当 failed
environment failure 被当 product failure
repair context 太吵或太少
repair 多跑 / 不该跑
read-before-edit 漏判
verification candidate 选错
```

0.4.21 只跑并修 Research / Ghost。第一轮 DeepSeek smoke 已经覆盖下列脚本；
其中 `source_connector` connector arm 已用 `--transcript-mode archive` 补跑并证明
connector 可走通，但不证明优于 baseline；`source_connector_done` batch/checklist arm
没有净收益，暂不默认推广。DeepSeek 第一 provider baseline 已冻结，详见
`docs/0.4_deepseek_provider_baseline.zh-CN.md`，后续不再全量重跑 DeepSeek：

```text
bounded_research_planner_ab.py
longitudinal_research_harness_ab.py
research_comparison_benchmark_ab.py
source_connector_done_ab.py
search_coverage_ab.py
ghost_continuity_ab.py
ghost_research_continuity_ab.py
ghost_router_ab.py
ghost_signal_extractor_ab.py
ghost_work_queue_production_ab.py
```

允许修的问题仅限：

```text
SearchResult 被当 Evidence
open_url failure 被写成事实
stale claim 没标注
unsupported claim 进 brief
citation 和 claim 对不上
native_search_stall 没归因
memory 被当 evidence
旧事实变 citation
Ghost 自动扩大任务范围
work queue 误触发 Research
continuity hint 影响事实结论
```

0.4.22 产出最终稳定化报告，至少记录：

```text
每个 provider 跑了哪些 arm
哪些 arm pass / pass_with_provider_noise / fail_codey_bug / fail_harness_or_provider
每个 fail 的根因
哪些 bug 已修
哪些留到 0.5
transcript 是否可 replay
journal 是否完整
```

0.4.22 当前状态：

```text
Qwen coding/completion core 已逐 case 跑完 control_done、proof_only_block、repair_context、repair_context_minimal
Qwen multi-case 同进程执行不稳定，后续继续使用 one case / one process / fixed output / archive transcript
已修 Codey 侧 no-run verification、fixture scope scorer、failed-row report gate、docs-only fixture、verification-forbidden limited completion
缺失依赖 case 反复变成 modified_test_fixture，归类为 provider/model false completion，由 scorer/report gate 捕获
Qwen Research core 最小 smoke 已完成：bounded planner 无净收益，source connector 改善 source reach 但未完成 report，done batch 无净收益，search coverage hint 有明确安全收益
Qwen Ghost core 最小 smoke 已完成：continuity 作为 stale/recheck hint，不污染 evidence；当前请求覆盖 continuity；work queue 不误触发 Research，显式新请求不消费旧队列
0.4.22 final stabilization report 与 Qwen provider baseline 已产出：docs/0.4.22_final_stabilization_report.zh-CN.md、docs/0.4_qwen_provider_baseline.zh-CN.md
下一步决定是否进入 0.5 或先补第三 provider 最小 cross-check
```

进入 0.5 的门槛：

```text
DeepSeek 第一 provider baseline 已冻结，且没有未归因 Codey bug
至少第二个 provider 通过 coding + research 核心
Qwen native-search stall 有明确分类
Ghost 不污染 evidence
所有 Codey bug 都有 deterministic test
每个 arm 都能从 JSON + journal + transcript 复盘
```

0.5 不做插件系统，也不做 UI 扩张。0.5 的目标是把 0.4 已经完成的
Evidence / Completion / Repair / Ghost / Protocol telemetry，收成一条更耐用的
内部运行时主线：

```text
verified completion
  -> durable operation state
  -> effect intent / settlement
  -> replay policy
  -> provider / protocol / project habit learning
  -> bounded context admission
  -> quieter but more reliable runs
```

这条线吸收 Pi Harness 的运行语义，但不照搬 session tree、lanes、hooks 或
完整 Storage conformance。Codey 只需要先让当前单 run、单 project writer、
Research pipeline 和 repair loop 更可恢复、更可证明、更少协议摩擦。

### 0.5 总边界

必须守住：

```text
Ghost / World Model 只能产出 hint，不能产出 evidence、permission 或 completion verdict
World Model prediction_confidence 不是 truth confidence
Protocol adapter 只能 lower 到 Codey canonical ToolCall
不同 tool dialect 不能改变 ToolRuntime / ActionPolicy / CompletionProof 语义
所有 model-visible 新上下文仍必须走 ContextSource / ContextEpoch / PromptEnvelope
所有 repair/provider/tool 效果必须能在 RunTrace / RunLedger / RunOperationState 中解释
UI interruption count 不上升；不新增 dashboard、profile selector、常驻面板或自动弹窗
```

0.5 不做：

```text
不做 WorldModelManager / WorldModelPlanner / Ghost persona
不让 Ghost 主动插话或后台自动 Research / Coding
不开放 hooks API、第三方插件、插件市场或 agent-loop 接管
不把 evidence / completion / repair 变成模型可调用工具
不默认导出训练数据，不保存 raw transcript / secret / webpage body
不按 provider 复制 runtime；provider 差异只存在协议边界和 telemetry
不在没有 A/B 证明时默认切换 tool dialect
```

旧文档里 `0.6+` 的“小型 Tool Protocol Adapter、local classifier、可选训练导出、
multi-provider dialect A/B、Ghost Explain verbalizer、World Model shadow ranker、
claim-gap / verification strategy evaluation”，全部并入 0.5.xx。合并方式不是提前接线，
而是每个小版本都交付一个能单独改善 Codey 的能力。

### 0.5 横向架构线

```text
TaskRunner.run
  -> RunOperationState：只管当前 run 的 durable program counter
  -> EffectLog：只管 provider/tool/repair intent + settlement
  -> ReplayPolicy：只管恢复时 safe / unsafe / interrupted
  -> ProtocolAdaptation：只管 ToolCall 参数和方言 lower，不碰 runtime 语义
  -> Ghost / WorldModel projections：只管 hints 和解释，不碰 evidence verdict
```

拆分规则：

```text
一个版本只能沿真实生命周期边界抽离
抽离后本版本必须立刻被生产路径消费
不能只增加 selector / registry / adapter 空槽
不能改变 UI/SSE/receipt shape，除非 A/B 明确覆盖
不能因为减少 task_runner.py 行数而拆模块
```

## 0.5.0 - Run Operation State + Completion Repair Durability v1

状态：计划。目标是把 0.4.13 的 verified completion / bounded repair 从
`_run_project_mode` 的函数栈状态，收成一个最小 durable program counter。第一版只覆盖
coding writer 收尾、CompletionProof、repair admission 和 terminal outcome，不碰
Research lanes、hooks 或多并发。

### 做什么

新增：

```text
codey/run_operation.py
tests/test_run_operation.py
tests/test_task_runner_operation_state.py
```

核心对象：

```text
RunOperationState(
  schema_version
  run_id
  session_id
  project_ref
  phase
  writer_attempt
  provider_id
  turn_budget
  turns_used
  completion_proof_ref
  repair_rounds
  repair_context_ref
  blocked_reason
  terminal
)
```

第一版 phase 控制在少数几类：

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

落盘方式统一使用 `storage.file_lock.with_file_lock()` 包住 read/compute/write，并通过
`write_json_atomic()` 写入；纯一次性快照仍可用 atomic write。路径限定在：

```text
state/run_operations/<session_key>/<run_id>.json
```

收益必须在本版直接体现：

```text
crash / stop / provider failure 后能看到最后一个 committed phase
repair round 不再只能靠局部变量推断是否跑过
blocked reason、turn budget、proof ref 和 repair admission ref 绑定到同一个 run state
用户恢复或查看 run details 时能解释“停在 writer、proof、repair 还是 terminal”
```

### 边界

- 不新增 CompletionManager / RepairManager。
- 不恢复 provider stream。
- 不重放工具。
- 不改变 0.4.13 的 prompt、tool result、receipt、SSE 或 UI。
- 不保存 raw prompt、raw reply、raw stdout/stderr、raw diff 或 source body。
- terminal 后可以保留一个 bounded terminal snapshot；不要把 operation state 变成长历史。

### 顺手架构优化

```text
把 completion enforcement 中的 repaired_once / blocked_reason / remaining_turns
  收成 RunOperationState writer 方法
TaskRunner 只在 phase 边界调用 state.commit(...)
_record_completion_proof_trace() 继续是 trace 写入；RunOperationState 只存 proof ref / status
RunLedger 的 run_finished 和 RunOperationState terminal 必须一致
```

### 验证

```text
每个 phase 都能 round-trip
bad schema / wrong run_id / oversize state fail closed
completion proof failed 后必须先进入 repair_context_admitted 才能 repair_running
repair_rounds 不能超过 MAX_COMPLETION_REPAIR_ROUNDS
terminal state 和 final event stop_reason 一致
crash 在 writer_settled / completion_proof_recorded / repair_running 后重启时能给出诚实恢复摘要
state payload 不含 raw prompt / reply / stdout / diff / source body
RunOperationState 不 import agent/provider/tool_runtime/server/ghost
```

### A/B

不需要 live provider A/B。它不改变模型可见内容或工具语义。需要做 deterministic
crash-position tests 和一条手工 stop/resume smoke。

## 0.5.1 - Effect Intent / Settlement + Tool Replay Policy v1

状态：计划。目标是把 Pi 的 effect sandwich 落到 Codey 当前最危险的三个边界：
provider send、tool run、completion repair round。每个真实外部效果前写 intent，
效果后写 settlement；恢复时不从事件缺失推断，而是读取最后一个 committed state。

### 做什么

新增：

```text
codey/effect_log.py
codey/replay_policy.py
tests/test_effect_log.py
tests/test_tool_replay_policy.py
tests/test_agent_effect_sandwich.py
```

Intent / settlement 类型：

```text
provider_send_intent
provider_send_settlement
tool_call_intent
tool_call_settlement
repair_round_intent
repair_round_settlement
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
provider send 失败能区分 never_sent / maybe_sent / settled
edit/run 崩溃后不会被自动重复执行
unsafe tool 崩溃恢复时产生 synthetic interrupted result，让对话保持一 call 一 result
safe read/search 崩溃恢复时可以按 persisted args 重新跑，减少“读到一半死掉”的丢失
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
RunLedger 的 tool_started/tool_finished 继续服务事实流；EffectLog 服务恢复语义
```

### 验证

```text
任何 provider/tool/repair effect 开始前必须已有 intent
settlement 只能引用已存在 intent
safe tool effect_pending 恢复会重新执行
unsafe tool effect_pending 恢复生成 interrupted result，不重复执行
provider maybe_sent 恢复不会伪造 done
RunOperationState phase 与 EffectLog 最新 settlement 一致
policy denied tool 没有真实 effect，只能有 immediate settlement
tool args digest 稳定且不含 raw secret
未知工具默认 unsafe
```

### A/B

不需要质量 A/B。需要 fault-injection tests 和 live smoke：杀进程位置覆盖
`before intent / after intent / during effect / after settlement`。

## 0.5.2 - Shared Tool Argument Repair + Protocol Friction Reduction v1

状态：计划。目标是把 coding `JsonToolCodec` 里散落的参数别名、编辑参数宽容和
常见 provider 方言误差，收成所有 coding codec 共用的薄 repair shim。这个版本会直接
降低 unknown/invalid args repair 次数。

### 做什么

新增：

```text
codey/tool_args_repair.py
tests/test_tool_args_repair.py
tests/test_protocols.py
tests/manual/tool_args_repair_ab.py
```

支持的保守修复：

```text
search / old / before -> old_string
replace / replacement / after -> new_string
single replacement object -> replacements[...]
write_file / create_file + content -> edit content for new file
JSON string replacements -> parsed replacements, invalid JSON fail closed
numeric string offset/limit -> bounded int
path normalization -> project-relative only, escape fail closed
```

直接收益：

```text
模型输出接近 Codey 工具语义但字段名稍偏时，少打一轮协议修复
protocol_telemetry 记录 alias_rewrite_count / repair_kind
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

需要小型 live A/B，因为 parser 接受范围变宽。指标：

```text
invalid_args_rate
protocol_repair_count
first_valid_tool_rate
edit_success
verification_success
unsafe_action_count
false_completion_rate
```

## 0.5.3 - Tool Contract Drift Guard + Prompt Surface Decoupling v1

状态：计划。目标是让 coding 和 research 的模型可见工具说明由同一套 contract renderer
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

本版还承接 Research untrusted source wrapper 的验证，但必须按 A/B-first 顺序落地：

```text
tests/fixtures/research_prompt_injection/
tests/test_research_source_rendering.py
tests/manual/research_source_rendering_ab.py
codey/research/source_rendering.py（只有 A/B 通过后才接入默认 open_url 渲染）
```

wrapper 只能改变网页来源内容的模型可见边界，不改变 Research planner、tool schema、
EvidenceLedger、citation contract 或 completion gate。默认生产路径必须先保持旧渲染，
直到 baseline / treatment 都有 result JSON、journal 和 transcript，且 treatment 证明：

```text
source injection text 没有变成 tool action
source body 被明确标记为 data / untrusted source
evidence quality 不降
source coverage 不降
completion honesty 不降
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

默认 parity 不需要 A/B。若本版顺手压缩工具说明、改变模型可见文案，或启用
Research untrusted source wrapper 的生产默认渲染，必须先走
`tests/manual/tool_protocol_portability_ab.py` 或专用 Research source-rendering A/B。

## 0.5.4 - Provider / Protocol Affinity + Repair Outcome Learning v1

状态：计划。目标是让 Ghost 学会“哪个 provider 常在哪类协议摩擦上失败、哪类
repair prompt 更有效”，但只影响 repair strategy 和诊断，不自动换 provider、不授权工具。

### 做什么

新增或扩展：

```text
codey/ghost/protocol_affinity.py
codey/ghost/affinity.py
codey/ghost/sleep.py
tests/test_ghost_protocol_affinity.py
tests/test_provider_protocol_learning.py
```

学习输入：

```text
protocol_telemetry
provider_failure
completion_proof outcome
repair_context admitted/refused
repair_result stop_reason
tool_args_repair counts
```

输出：

```text
ProviderProtocolHint(
  provider_id
  phase
  dominant_error_kind
  effective_repair_detail
  protocol_stability_score
  provenance_refs
  not_evidence=true
  not_policy=true
)
```

直接收益：

```text
同一个 provider 连续 no_json / native_tool_denial 时，repair prompt 可选更短或更明确
Run Details / trace 能解释 provider 协议失败趋势
manual A/B 能按 provider 优先挑高风险 case，而不是盲跑
```

### 边界

- 不自动切换 provider。
- 不放宽 PermissionProfile。
- 不把 provider 稳定性当结果可信度。
- 不把 protocol affinity 注入默认 writer prompt；只允许进入 repair prompt 策略选择。
- Ghost 只存 bounded counts、reason codes、refs，不存 raw prompt/reply。

### 顺手架构优化

```text
Ghost sleep 读取 RunTrace projection，而不是 TaskRunner 内存对象
repair context detail(full/minimal) 选择收成小函数，不散在 task_runner.py
provider failure classification 和 protocol error counts 共用 reason-code vocabulary
```

### 验证

```text
protocol affinity 不进入 EvidenceLedger / CompletionProof
not_evidence / not_policy 必填
没有足够样本时不输出 hint
hint 过期后衰减
repair prompt 策略选择有 deterministic fallback
Ghost protocol_affinity 不 import provider/tool_runtime/task_runner
```

### A/B

需要。它改变 repair prompt strategy。指标：

```text
protocol_repair_success_rate
repair_turns
first_valid_tool_rate_after_repair
completion_proof_status
false_completion_rate
provider_failure_rate
```

## 0.5.5 - Project Verification Habit Projection v1

状态：计划。目标是让 Codey 记住项目实际验证习惯，帮助模型更容易选择正确验证命令，
但不自动执行，也不把习惯当 completion proof。

### 做什么

新增：

```text
codey/ghost/project_habits.py
tests/test_project_verification_habits.py
tests/test_task_runner_project_habits.py
tests/manual/project_habits_ab.py
```

输入：

```text
successful CheckEvidence
configured verification commands
completion proof failures
review inherited checks
project config warnings
manual user corrections
```

输出为 ContextSource 候选：

```text
Project habit hint:
- This project often verifies release changes with `python -m pytest`.
- This is a habit hint, not proof. Run a relevant check before claiming done.
```

直接收益：

```text
新 run 更容易选择本项目常用验证命令
stale / inherited verification 更容易触发 fresh check
减少 premature done 和错误命令尝试
```

### 边界

- 不自动运行验证。
- 不覆盖 `.codey/config.json` 明确命令。
- 不把历史 successful check 当本轮 fresh pass。
- 不在没有 task_changed / verification scope 时强行提示。
- 所有 model-visible hint 必须经过 ContextEpoch。

### 顺手架构优化

```text
ProjectTaskContextBuilder 只负责选取 habit hints，不负责学习
ghost/project_habits.py 从 ledger/trace projection 学习
verification_candidate_lines 和 habit hints 分层渲染，避免混成 hard contract
```

### 验证

```text
history check 只能生成 habit，不生成 fresh verification
explicit config 优先于 habit
过期或失败率高的 habit 不进入 context
ContextEpoch admission 绑定 outbound bytes
habit payload 不含 raw stdout/stderr
```

### A/B

需要。它改变 writer model-visible context。指标：

```text
fresh_verification_rate
wrong_command_rate
premature_done_rate
completion_repair_count
task_success
sent_chars
```

## 0.5.6 - Ghost Explain v0 + Provenance-Safe Inspector

状态：计划。目标是让用户和开发者能解释“这次 Ghost 为什么选了这些 hint”，但不让
Ghost 成为独立说话者，也不改主 UI。第一版只做 deterministic renderer 和 CLI/JSON。

### 做什么

新增：

```text
codey/ghost/explain.py
tests/test_ghost_explain.py
tests/test_cli_ghost_explain.py
```

CLI：

```text
codey ghost explain --project ... --session-id ... --format json
codey ghost explain --project ... --session-id ... --format text
```

核心 payload：

```text
GhostExplainItem(
  surface
  item_id
  label
  scope
  reason_code
  weight
  selection_confidence
  provenance_refs
  warnings
  not_evidence=true
  not_policy=true
)
```

直接收益：

```text
能解释 directive / affinity / continuity / work_queue 的选择原因
坏记忆、过期偏好和错误关联更容易定位
为 delete/reset/export 提供可审计 ref，不需要新增 UI
```

### 边界

- Ghost Explain 不调用 provider。
- Ghost Explain 不进入默认 prompt。
- Ghost Explain 不生成工具调用。
- `GhostNode.evidence_refs` 在 explain payload 中必须改名为 `provenance_refs`。
- 输出固定标注：not evidence / not policy。

### 顺手架构优化

```text
build_ghost_directive() 暴露 selected_nodes 的 bounded metadata
affinity hint provenance 和 directive provenance 使用同一 renderer
RunTrace 只存 explain report digest/counts，不存正文
```

### 验证

```text
not_evidence / not_policy 必填
renderer 输出包含 not evidence / not policy
payload 不含 raw transcript / secret / webpage body
ghost/explain.py 不 import provider/browser/tool_runtime/task_runner
默认 PromptEnvelope 不包含 Ghost Explain section
```

### A/B

不需要。它不改变默认模型行为。需要 CLI smoke。

## 0.5.7 - World Model Event Log + Prediction Review v0

状态：计划。目标是落地最小 World Model 合同：记录项目/研究/环境状态预测，并用已有
runtime evidence、proof 或用户纠正复盘命中/失败。第一版不进入 prompt，先用于
blocked summary、RunTrace 和本地诊断。

### 做什么

新增：

```text
codey/world_model/events.py
codey/world_model/prediction.py
codey/world_model/projection.py
codey/world_model/trace.py
tests/test_world_model_events.py
tests/test_world_model_prediction.py
tests/test_world_model_projection.py
```

对象：

```text
WorldModelEvent
PredictionRecord
PredictionReview
WorldModelProjection
```

事件类型：

```text
prediction_recorded
prediction_reviewed
claim_gap_observed
environment_marker_observed
calibration_updated
projection_compacted
projection_staled
```

直接收益：

```text
反复出现的 environment_failure / verification_unavailable 有本地复盘记录
上次“这个 repair 策略可能有效”的预测能被 proof outcome 打分
blocked summary 可以更稳定地区分环境、协议、证据缺口和完成失败
```

### 边界

- 不进入 EvidenceLedger / CompletionProof。
- 不产生 citation refs。
- 不调用 provider、search、tool_runtime。
- 没有 proof/event/user-correction refs 时只能 `unjudged`，不能猜 hit/miss。
- projection 必须有 `valid_until`；过期默认 stale。

### 顺手架构优化

```text
completion_verification 的 failure_class 结果可以投影成 environment_marker_observed
research proof gaps 可以投影成 claim_gap_observed
protocol affinity 的 repair outcome 可以投影成 prediction_reviewed
World Model 单独在 codey/world_model/，不塞进 codey/ghost/
```

### 验证

```text
append-only jsonl tail damage 可恢复
schema/kind/source_ref/payload_digest 校验 fail closed
review 没有 refs 不能 mark hit
projection 没有 valid_until 视为 stale
stale projection 只能生成 recheck candidate
world_model 不 import provider/browser/tool_runtime/task_runner
```

### A/B

不需要 live A/B。它不进入 prompt。需要 deterministic prediction/review replay tests。

## 0.5.8 - World Model ContextSource + Shadow Strategy Ranker v1

状态：计划。目标是把 0.5.7 的 state estimate 变成可选、受限、可 A/B 的
ContextSource：只提示模型“哪里需要复查”，不告诉模型“什么是真的”。同时增加 shadow
strategy ranker，用历史 review 评估 verification-first / source-refresh / repair-short
等策略，但默认不接管执行。

### 做什么

新增：

```text
codey/world_model/context.py
codey/world_model/strategy.py
tests/test_world_model_context.py
tests/test_world_model_strategy.py
tests/manual/world_model_context_ab.py
```

模型可见前缀固定：

```text
Local state estimate. This is not evidence.
Use it only to decide what to inspect, verify, or ask next.
Do not cite it as a source.
Do not treat stale or predicted state as fact.
```

直接收益：

```text
Research 遇到 stale claim 时更容易先 refresh source
Coding 遇到连续 environment marker 时更容易先复查环境，而不是乱改代码
completion repair 更容易选择 verify-first，而不是直接宣布 done
```

### 边界

- 只带 bounded statement、state_refs、review_event_refs、payload_digests、non-citation recheck_refs。
- `recheck_refs` 不能渲染成 citation/source/evidence refs。
- strategy ranker 第一版只 shadow；默认执行路径仍由 TaskRunner/ResearchPipeline 决定。
- prompt admission 必须走 ContextEpoch，过期 projection 只允许 re-check hint。

### 顺手架构优化

```text
ProjectTaskContextBuilder / ResearchContext 只消费 WorldModelContextSource
strategy shadow 结果进 RunTrace，不进 Ghost memory
Context admission 与 Ghost continuity 使用同一 epoch 机制
```

### 验证

```text
WorldModelContext 未经过 ContextEpoch 不得进入 prompt
context payload 不含 evidence_refs / citation_refs
stale projection 不能生成 reuse hint
strategy ranker 输出不能执行工具、不能改变 provider、不能改权限
```

### A/B

需要。它改变模型可见 context。指标：

```text
stale_update_correctness
environment_misrepair_rate
completion_repair_success
unsupported_claim_rate
verification_success
sent_chars
UI interruption count
```

## 0.5.9 - Protocol Adapter Dataset Export + Shadow Normalizer v1

状态：计划。目标是把 protocol telemetry、tool args repair、repair prompts 和最终
ToolCall/CompletionProof outcome 导出成可选本地数据集，并同时跑一个 shadow normalizer
评估“如果用了 adapter 会不会更早得到合法 ToolCall”。这不是默认训练，也不是空槽：
本版直接给 Codey 一个离线诊断和回归能力。

### 做什么

新增：

```text
codey/evaluation/protocol_dataset.py
codey/protocols/shadow_adapter.py
tests/test_protocol_dataset.py
tests/test_shadow_protocol_adapter.py
tests/manual/tool_protocol_portability_ab.py
```

导出样本：

```text
protocol_error -> corrected ToolCall
invalid args -> repaired canonical args
provider output digest -> protocol error kind
failed completion proof -> repair context class
research native_search_leak -> local Research tool correction
```

直接收益：

```text
能离线比较 provider 之间的协议失败类型
能回放 parser / shim / shadow adapter，不需要重新打 live provider
能发现某类 provider native-like output 是否值得做生产 adapter
```

### 边界

- 默认关闭，用户显式开启导出。
- 默认 digest/bounded text，不导出 raw transcript。
- 不导出 secret、cookie、DOM、webpage body、source body。
- shadow adapter 不影响生产执行。
- 导出样本不能进入 EvidenceLedger / CompletionProof。

### 顺手架构优化

```text
ABJournal TranscriptReplayCache 只供 manual/evaluation 层读取
生产 RunTrace 提供 digest/counts/refs；dataset exporter 负责显式 materialize
shadow adapter 只消费 protocol telemetry 和 saved transcript refs
```

### 验证

```text
export disabled 时无文件写入
archive disabled 时只有 digest/ref
raw transcript 不进入生产 RunTrace/EvidenceLedger
shadow adapter output 必须再过 runtime validator
dataset schema 稳定且可 prune/delete
```

### A/B

本版新增 manual A/B harness，但不改变生产行为，不需要 release-blocking live A/B。

## 0.5.10 - Local Protocol Classifier + Repair Strategy Selector v1

状态：计划。目标是把 0.5.9 的数据和 0.5.4 的 affinity 用起来：训练或规则化一个小型
本地 classifier，选择已有 repair prompt strategy、tool-args repair strictness 和
protocol hint 长度。它不训练主模型，也不改变安全语义。

### 做什么

新增：

```text
codey/protocols/classifier.py
codey/protocols/repair_strategy.py
tests/test_protocol_classifier.py
tests/test_protocol_repair_strategy.py
tests/manual/protocol_repair_strategy_ab.py
```

策略：

```text
minimal_json_reminder
schema_focused
unknown_tool_specific
native_tool_denial_specific
research_native_search_specific
completion_repair_minimal
completion_repair_full
```

直接收益：

```text
不同 provider/phase 使用更合适的 repair prompt
减少重复 no_json / unknown_tool 循环
Research native_search_leak 更快回到 Codey Research 工具
```

### 边界

- classifier 只在 protocol/repair 边界生效。
- 不能选择 provider，不能授权工具，不能跳过 verification。
- 坏 classifier 输出 fail closed 到默认 repair prompt。
- 训练/更新模型必须本地可删除、可回滚；默认可用规则 baseline。

### 顺手架构优化

```text
_protocol_repair_prompt() 接受 RepairStrategy，而不是散落 if provider/phase
Research protocol repair 与 coding protocol repair 共用 strategy vocabulary
RunTrace 记录 strategy_id / classifier_version / shadow_vs_chosen
```

### 验证

```text
每个 strategy 都只改 repair prompt，不改 tool schema
classifier malformed output 回退默认
native_search_leak 不会启用 provider native search
strategy_id 进入 trace，但 raw prompt 不进入 trace
```

### A/B

需要。它改变 repair prompt。指标：

```text
protocol_repair_success_rate
repair_turn_count
first_valid_tool_rate
research_done_before_evidence_rate
native_search_leak_count
completion_proof_status
```

## 0.5.11 - Conditional Tool Projection + One Proven Dialect v1

状态：计划。目标是只在 A/B 证明收益后，为一个 provider/model family 启用一个
替代模型可见工具面，并 lower 到 Codey canonical ToolCall。这个版本不能提前预设赢家；
如果没有赢家，就不发布生产默认，只保留 A/B 报告。

### 做什么

候选 dialect：

```text
minimal_primitives
claude_like_str_replace
codex_like_patch_shape
research_minimal_surface
```

直接收益：

```text
对已证明更适合某方言的 provider，降低 protocol_error_rate
内部执行仍走同一 ToolRuntime / ActionPolicy / Evidence / CompletionProof
用户不需要知道方言存在
```

### 边界

- 单次 prompt 只展示一种 dialect。
- 没有 A/B 胜出就不启用生产默认。
- Patch dialect 只有在有安全、原子、可验证 patch parser 后才能启用。
- `bash` 永远 lower 到 Codey run/shell policy，不绕过 approval。
- Research native search/browse 不能绕过 source ledger/opened source gate。

### 顺手架构优化

```text
ProtocolCodec 增加 lower_to_canonical() 的 explicit result shape
Tool contract hash 标记 dialect_id
Effect intent 记录 canonical tool 和 model_visible_tool_digest 的对应关系
```

### 验证

```text
dialect parser bad payload fail closed
same semantic tool lower 后 canonical ToolCall 一致
permission/profile/policy 对所有 dialect 结果一致
dialect prompt 不混用多套工具名
completion/evidence 工具不得出现
```

### A/B

必须。`tests/manual/tool_protocol_portability_ab.py` 覆盖：

```text
read-edit-run-done
create-file
exact-replacement
multi-file-read
failed-test-then-repair
approval-required-command
premature-done
research search/open/knowledge_write/done
native-search-leak repair
```

发布默认阈值：

```text
protocol_error_rate 下降
first_valid_tool_rate 上升
false_completion_rate 不升
unsafe_action_count = 0
verification_success 不降
research evidence bypass = 0
```

## 0.5.12 - Native Structured Provider Path v1

状态：计划。目标是给真正支持原生 tool/function calling 的 API provider 一个可选
structured path，避免正文 JSON 的协议摩擦。网页 provider 仍走现有 prompt/reply。

### 做什么

新增或扩展：

```text
codey/providers/base.py
codey/providers/local_openai.py
codey/protocols/structured.py
tests/test_structured_provider_path.py
tests/test_provider_capabilities.py
```

可选 provider 能力：

```text
send_structured(messages, tools) -> assistant_message_with_tool_calls
```

直接收益：

```text
local/API provider 可以用原生 tool channel
减少正文 JSON parse failure
tool call ids、args 和 finish_reason 更清楚
```

### 边界

- 不影响 DeepSeek/Qwen/GLM/MiMo/StepFun web provider。
- structured tool call 仍必须 lower 到 canonical ToolCall 并过 runtime validator。
- 不把 provider 原生搜索/浏览变成 Codey evidence。
- 不新增模型可调用 completion/evidence 工具。

### 顺手架构优化

```text
ProviderCapability 声明 structured_tools 支持
Protocol telemetry 区分 text_json / structured_tool_channel
Effect intent 对 structured call 仍记录 canonical ids
```

### 验证

```text
structured provider missing capability 时回退 text JSON
bad structured args fail closed
structured/native tool names 不绕过 permission
usage / finish reason 进入 bounded trace
web provider 不导入 structured-only SDK
```

### A/B

需要 API/local provider A/B；web provider 不作为本版 blocker。指标：

```text
no_json_rate
invalid_args_rate
tool_call_success
verification_success
latency
token usage
```

## 0.5.13 - Local Training Export + Optional Tiny Adapter v0

状态：计划。目标是把 0.5 的 telemetry 和 dataset 用于可选的小适配层训练：protocol
error classifier、tool-call normalizer、repair prompt selector、claim-gap classifier。
它不是默认能力，不训练主模型，不需要 GPU。

### 做什么

新增：

```text
codey/evaluation/training_export.py
codey/protocols/tiny_adapter.py
tests/test_training_export.py
tests/test_tiny_adapter_policy.py
```

可训练对象：

```text
protocol error classifier
tool-call normalizer
repair prompt selector
mode/provider hint ranker shadow
claim-gap classifier
verification-first strategy ranker
explanation template selector
```

直接收益：

```text
高级用户可以本地生成小适配器并在 shadow mode 评估
Codey 可以用固定 eval 判断 adapter 是否真的减少协议失败
训练数据和 adapter 可删除、可回滚、可审计
```

### 边界

- 默认关闭。
- 不导出 raw transcript / secret / webpage body。
- adapter 先 shadow mode，不直接生产默认。
- adapter 输出必须过 runtime validator。
- LoRA/SFT 仍是高级可选，不进入默认 release gate。

### 顺手架构优化

```text
training export 复用 protocol_dataset，不重新读取生产 state 私有路径
adapter eval 复用 manual AB journal 和 regression fixtures
RunTrace 只记录 adapter_id / eval summary，不记录训练样本正文
```

### 验证

```text
explicit opt-in 才能导出
delete/prune/export 路径可测试
adapter malformed output fail closed
shadow eval 可重复
adapter 不 import tool_runtime/provider/task_runner
```

### A/B

不作为默认生产路径时不需要 release-blocking A/B。若某 adapter 要默认启用，必须回到
0.5.10/0.5.11 的 provider live A/B gate。

## 0.5.14 - Ghost / World Model Maintenance Hardening v1

状态：计划。目标是把 0.5 新增的 Ghost protocol affinity、project habits、World Model
projection、dataset refs 做成可衰减、可删除、可重建、可导出的本地状态。它给用户
带来的实质收益是长期状态更干净，坏 hint 更容易移除，维护不打扰工作。

### 做什么

扩展：

```text
codey/ghost/sleep.py
codey/ghost/store.py
codey/world_model/maintenance.py
tests/test_ghost_sleep.py
tests/test_world_model_maintenance.py
tests/test_local_state_delete_export.py
```

维护项：

```text
projection health
event compaction
hebbian / affinity decay
project habit decay
world model stale marking
prediction due review using existing refs
corruption quarantine
bounded maintenance report
```

直接收益：

```text
长期使用不会让 Ghost / World Model state 无限增长
坏 projection 被 quarantine 而不是污染下一次 prompt
用户可以按 user/project/session scope 删除或导出本地适应状态
```

### 边界

- maintenance 不调用 provider。
- maintenance 不联网、不执行工具、不触发 Research。
- maintenance 不修改项目文件。
- quarantine 不删除原始 event，除非用户显式 delete scope。

### 顺手架构优化

```text
Ghost sleep 和 WorldModel maintenance 共享 single-flight guard
state corruption 统一 reason codes
delete/export scope 使用同一 session_key/project_ref helper
```

### 验证

```text
single-flight background run
oversize state compaction
bad json quarantine
delete user/project/session scope
export 不含 raw prompt/reply/secret
maintenance 不 import provider/browser/tool_runtime/task_runner
```

### A/B

不需要。它不改变模型可见内容。需要长跑 smoke 和 corruption fixture。

## 0.5 Exit Gate 与 0.6 收敛线

0.5 做完前，不能因为“功能项都写完了”就直接进入下一条大能力路线。必须先通过
Exit Gate：

```text
RunOperationState / EffectLog / ReplayPolicy 已经稳定
TaskRunner 不再继续吸收新生命周期状态
CompletionProof / Evidence / Verification 的 source of truth 明确
Ghost / World Model / Protocol affinity 没有污染 evidence / permission / completion
Research untrusted source wrapper 只有 A/B 通过后才默认启用
至少两个 provider 完成 coding + research + ghost 核心 A/B
所有 A/B 失败都能通过 JSON / journal / transcript 复盘
```

0.6 的主线应是 consolidation，不是继续加能力：

```text
0.6 = Operation Runtime Consolidation + Source-of-Truth Cleanup
```

0.6 做：

```text
TaskRunner 瘦身
operation / effect / proof / ledger 语义统一
重复 projection 删除
过时 fallback 删除
core fact model 文档化
A/B 发现的高收益 prompt / protocol 策略默认化
```

0.6 不做：

```text
新 Manager
新 autonomous planner
插件市场
World Model 自动决策
跨 provider 自动仲裁
大 UI
```

判断原则：

```text
0.5 增加 durable runtime 的必要语义
0.6 删除、收敛、默认化 A/B 证明有效的部分
0.6+ 不再承接旧文档里的 Tool Protocol Adapter / classifier / World Model shadow ranker 等能力项
这些能力已经并入 0.5.xx；0.6+ 只承接稳定化、产品化和复杂度回收
```

## 0.5 插件开放边界

0.5 完成前仍不做有限插件化。即使 0.5.12 引入 structured provider path，
它也只是 provider capability，不是插件系统。真正的开放顺序仍然应放到 0.5 稳定后：

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

## 0.4.13-0.4.19 收口与 0.5 切入

0.4.13 已经完成核心：一轮 bounded repair、CompletionProof provenance、
repair context admission、protocol telemetry。0.4.14 完成目录冷迁移，0.4.15 完成
run-command boundary、provider override、locked JSON mutation 和 A/B evidence hygiene
收口。0.4.16 完成 Ghost Affinity / Work Queue event-log canonical ingestion 和
fail-closed replay 收口。0.4.17-0.4.18 完成 OS-backed file locks、event-state reset、
network boundary、cooperative cancellation 和 redirect/DNS 性能边界收口。0.4.19 进一步完成
A/B evidence polish 和非模型可见卫生收口：固定 output 的 result/journal/transcript/manifest
绑定、failed row 原子替换、resume attempt 显式记录、BrowserWorker stuck 被动观测、
显式 `mode=` 原子写权限 hard fail、Ghost Work Queue transition matrix，以及将
`NetworkStatus.PUBLIC_WEB` 这类容易误读的状态命名改为 `NetworkStatus.POLICY_ALLOWED`。
Pi-style durable harness 不再回塞 0.4；它属于 0.5 的运行时耐久性主线。

0.4 收口只保留这些 release boundary：

```text
0.4.13：proof enforcement + repair admission + telemetry
0.4.14：冷迁移后的结构基线
0.4.15：A/B 前安全与证据卫生基线
0.4.16：A/B 前 Ghost 本地事件日志 canonical replay 基线
0.4.17-0.4.19：A/B 前运行、存储、网络、worker health 和证据语义收口
0.4 stabilization：只修 A/B 暴露的真实 bug，不再堆能力
```

三件 Pi 借鉴能力的具体修改落点如下，版本归属应放到 0.5.0 / 0.5.1：

```text
1. repair/provider/tool 显式 operation phase
   -> 新增 codey/run_operation.py
   -> TaskRunner._run_project_mode 在 writer start/settle、proof record、
      repair admission/start/settle、terminal 处 commit phase
   -> state 只存 refs/status/counts/reason，不存 raw 文本

2. provider/tool intent -> effect -> settlement
   -> 新增 codey/effect_log.py
   -> provider.send(prompt) 前 commit provider_send_intent，返回后 commit settlement
   -> agent tool loop 在真实 tool 执行前 commit tool_call_intent，结果后 commit settlement
   -> repair failover.run(...) 前后 commit repair_round_intent/settlement

3. tool replay policy
   -> 新增 codey/replay_policy.py
   -> read/ls/search/references/project_facts 标 safe
   -> edit/write/shell/run/knowledge_write/unknown 默认 unsafe
   -> unsafe effect_pending 恢复时 synthetic interrupted result，不重复执行
```

判断线：

```text
0.4.13：完成 verified completion 行为闭环，不引入恢复语义
0.5.0：让 completion/repair 状态可恢复、可解释
0.5.1：让 provider/tool effects 有 intent/settlement 和 replay policy
```

## Adapter 自修复 prompt 分层（后续）

0.4.13 把修复面扩成完整 web adapter 层后，repair prompt 明显变重：实测 Qwen 约
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

0.4 需要新增 Evidence Research Runtime 验证。Research 质量不能只看最终 summary
是否像样，还要验证 source、evidence、claim、assumption、analysis run、artifact、
critic finding 和 Ghost continuity 的边界。

0.5 继续新增 Durable Runtime / Local Adaptation / Protocol Portability 验证。重点不是
“模块是否存在”，而是每个运行边界是否能证明：

```text
外部效果前有 intent
外部效果后有 settlement
恢复时读 durable state，不从事件缺失推断
Ghost / World Model / Protocol adapter 都不能跨过 Evidence / Permission / Completion
每个模型可见 hint 都绑定 ContextEpoch
每个 tool dialect 都 lower 到同一个 canonical ToolCall
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

0.4 逐步新增：

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
tests/test_tool_prompt.py
tests/test_tool_contract_drift.py
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
EffectLog 不能保存 raw prompt/reply/stdout/diff/source body
外部 provider/tool/repair effect 启动前必须已有 intent
unsafe tool effect_pending 恢复不能重复执行
ReplayPolicy 未知工具默认 unsafe
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

Ghost 行为改变仍然需要 A/B。0.3 后半段的经验是：如果只改 trace、envelope
结构、capability seam、tool contract、action policy pipeline 或只读 UI，不需要
live provider A/B；只有实际改变模型可见 prompt、Router、Research prompt、
Writer prompt、provider fallback 策略或工具权限时，才需要 provider A/B。

0.4 后半改成行为触发式 A/B，不再按奇偶版本默认 live 对照：

```text
0.4.0 Evidence Kernel / Research Object Model（已完成；projection-only，不需要 A/B）
0.4.2 Research Proof Quality Gate
0.4.4 Bounded Research Planner
0.4.6 A/B Observation Journal（durability / replay smoke，不需要质量对照 A/B）
0.4.7 Evidence Runtime + ReviewFinding（deterministic-only 不需要 A/B；model critic 需要 A/B）
0.4.8 Safe Context Epoch（metadata-only 不需要 A/B；改变模型可见 context 需要 A/B）
0.4.9 Research Contract Lite（local-only 不需要 A/B；改变 completion 语义需要 queue A/B）
0.4.10 Domain Source Trust + Research Brief（projection-only 不需要 A/B；改变 Writer brief 文案需要 A/B）
0.4.11 Longitudinal Research Harness + Comparison Benchmark（已完成：deterministic harness + comparison gate；live/comparison smoke 后续增量）
0.4.12 Ghost Research Continuity（模型可见 continuity 必须 A/B）
0.4.13 Verified Completion Enforcement（阻止 done / repair context admission 必须 A/B）
0.4.15 Run Command Boundary / A-B Evidence Hygiene（不改 prompt；deterministic + self-test gate，live A/B 用于后续稳定化）
0.4.16 Ghost Event Canonicalization（不改 prompt；deterministic gate，live A/B 用于后续稳定化）
0.5.0 RunOperationState（durability-only，不需要 live A/B）
0.5.1 Effect Intent / Replay Policy（fault-injection，不需要质量 A/B）
0.5.2 Tool Args Repair（parser 接受范围变宽，需要 A/B）
0.5.3 Tool Prompt Decoupling（默认 parity 不需要 A/B；改文案需要 A/B）
0.5.4 Provider / Protocol Affinity（改变 repair strategy，需要 A/B）
0.5.5 Project Verification Habit（改变 writer context，需要 A/B）
0.5.6 Ghost Explain（默认不进 prompt，不需要 A/B）
0.5.7 World Model Event Log（不进 prompt，不需要 A/B）
0.5.8 World Model ContextSource（改变 context，需要 A/B）
0.5.9 Protocol Dataset / Shadow Adapter（默认 shadow，不需要 release-blocking A/B）
0.5.10 Local Protocol Classifier（改变 repair prompt，需要 A/B）
0.5.11 Conditional Tool Projection（启用任何生产 dialect 必须 A/B）
0.5.12 Native Structured Provider Path（API/local provider 需要 A/B）
0.5.13 Local Training Export（默认关闭，不需要 A/B；默认启用 adapter 必须 A/B）
0.5.14 Maintenance Hardening（不进 prompt，不需要 A/B）
```

0.4.1、0.4.3、0.4.5 以及后续任何只做 schema、ledger、projection、
connector boundary、planner dry-run、metadata、durable journal 或 deterministic validator
的版本，不做 live provider A/B。

逐步新增：

```text
tests/manual/ghost_signal_extractor_ab.py
tests/manual/ghost_directive_ab.py
tests/manual/ghost_learning_loop_ab.py
tests/manual/ghost_router_ab.py
tests/manual/ghost_work_queue_ab.py
tests/manual/ghost_research_interest_queue_production_ab.py
tests/manual/ghost_affinity_ab.py
tests/manual/ghost_affinity_quality_ab.py
tests/manual/research_proof_quality_ab.py
tests/manual/bounded_research_planner_ab.py
tests/manual/research_analysis_run_ab.py
tests/manual/ab_observation_journal_smoke.py
tests/manual/transcript_replay_cache_smoke.py
tests/manual/provider_observation_log_smoke.py
tests/manual/research_critic_ab.py
tests/manual/safe_context_epoch_ab.py
tests/manual/research_contract_completion_ab.py
tests/manual/research_brief_v2_ab.py
tests/manual/ghost_research_continuity_ab.py
tests/manual/longitudinal_research_harness_ab.py
tests/manual/research_comparison_benchmark_ab.py
tests/manual/completion_operation_resume_smoke.py
tests/manual/effect_sandwich_fault_smoke.py
tests/manual/tool_args_repair_ab.py
tests/manual/tool_protocol_portability_ab.py
tests/manual/project_habits_ab.py
tests/manual/world_model_context_ab.py
tests/manual/protocol_repair_strategy_ab.py
```

每个 A/B 都要记录：

```text
provider
case_id
baseline output
candidate output
pass/fail metrics
protocol compliance
side effects
UI interruption count
OpenScience version / commit（仅 real head-to-head）
artifact / rubric source（仅 real head-to-head）
journal run_id / event_digest chain（live A/B）
transcript mode: digest_only / archive_enabled
```

首版通过阈值方向：

```text
evidence_quote groundedness 必须 100%
false memory rate 必须低于明确阈值
JSON tool compliance 不能低于 baseline
coding smoke 不能回退
research citation quality 不能回退
research proof quality 不能回退
answer coverage rate 不能低于 baseline
citation locator precision 不能低于 baseline
counterevidence coverage 不能低于 baseline
analysis reproducibility 不能低于 baseline
research-to-code handoff quality 不能低于 baseline
unsupported claim rate 不能高于 baseline
Ghost continuity 不能被当成 evidence
extractor failure 必须 fail closed / no_signal
operation resume 不能伪造 done
unsafe tool replay count 必须为 0
protocol args repair 不能提高 unsafe_action_count
project habit 不能降低 fresh verification rate
World Model context 不能产生 citation/evidence refs
conditional dialect 不能提高 false completion / evidence bypass
structured provider path 不能影响 web provider baseline
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
TaskRunner / Server 周围汇合。0.5 会继续增加 RunOperationState、EffectLog、
ReplayPolicy、ProtocolAdaptation 和 World Model。这个压力是真实的，但不能为了
“看起来架构更好”做 big-bang rewrite。

需要承认并逐步收敛的债务：

```text
TaskRunner.run 承担 provider setup、routing、research、review、writer、ledger、trace
_RunFrame 已经包含 provider / conversation / trace / handoff / preflight / snapshot 等生命周期状态
后续 planner、multi-browser、recursive research 会继续放大这个 runtime context
0.5 还会加入 operation phase、effect intent/settlement、replay policy 和 protocol strategy
```

正确拆分顺序：

```text
RunOperationState：先覆盖 completion/repair terminal，再扩到 provider/tool effects
EffectLog：只在 provider/tool/repair 三个真实效果边界稳定后引入
ReplayPolicy：先保守 safe/unsafe，再谈自动恢复
ResearchPipeline：只在 proof quality / evidence ledger / follow-up research 边界成熟后抽
ProviderPipeline：只在 provider setup / preflight / fallback / canary 边界稳定后抽
ReviewPipeline：只在 review input / fix loop / finding lifecycle 边界稳定后抽
ProtocolAdaptation：先参数 repair，再 prompt contract，再方言投影
WorldModelProjection：先事件/复盘，再 ContextSource，再策略 shadow
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
一个可恢复、可解释、跨 provider 更稳的本地 agent runtime
```

并且每个增强都不要求用户理解内部系统：

```text
provider/tool/repair 的不确定窗口有 intent 和 settlement
edit/run 崩溃不会被静默重复执行
safe read/search 可以恢复，unsafe effect 会诚实中断
CompletionProof / RepairContext 有 durable phase，而不是函数局部变量
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
