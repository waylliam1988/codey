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
id、`context_source_ref()`、`snapshot_from_rendered_sources()`；无 I/O、
不 import 任何 codey 模块，架构测试锁死）；`ContextSource` /
`RenderedContextSource` 增加 `capability_id` / `admission_reason` 元数据（默认
空，渲染行为与 prompt 字节不变）；prompt envelope section 增加同样的三个可选
字段；新增共享 `record_provider_send_prompt()`，把 agent / server /
task_runner / research runner / consensus 共 9 处重复的 provider-send trace
记录收敛为一个入口，并自动盖上 provider_send freshness、epoch id 和固定
admission reason。Run Trace 的 `PromptSectionTrace` 增加可选 epoch /
admission / capability 字段，只在有值时序列化，其余 manifest 形状不变。
Capability Registry v1 补全 roadmap 字段（`trace_sections` /
`context_sources` / `evidence_producer` / `enabled_by_default`），补登记
0.4.7 的 `research_evidence_runtime` / `research_review_finding` 与本版的
`context_epoch` / `consensus_advisors`，并给 agent_runner / local_context /
policy_guard 等补事实归属；架构测试锁定生产代码里出现的每个 capability_id
引用都必须是注册能力。model critic、planner 消费 finding、插件系统、skill
加载全部未做。按 A/B 规则本版不需要实机验证。

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

状态：规划。目标是吸收 OpenScience 的 research contract 思想，但不照搬成
模型可随意调用的 `research_contract` 工具。Codey 的 contract 是本地 pipeline
contract，用来决定什么才算完成、阻塞或需要补证据。

### 做什么

新增：

```text
research/contract.py
```

Research Contract Lite：

```text
contract_id
question_ref
deliverables
checks
limits
evidence_refs
analysis_run_refs
artifact_refs
review_finding_refs
planner_gap_refs
completion_status
blocked_reason
proof_ref
```

Verified Completion Gate：

```text
Research conclusion -> Evidence / Source / Claim relation
Experiment conclusion -> AnalysisRun / ArtifactRef
Coding conclusion -> diff / test / review evidence
Queued research done -> ResearchProofReview + contract proof refs
```

契约状态：

```text
pending
running
blocked
complete
complete_with_limitations
failed
```

### 边界

- 不是模型工具。
- 不新增 workflow UI。
- 不替代 ResearchPipeline。
- 不让模型自己给 completion 盖章。
- 不保存 raw transcript、raw prompt 或 raw source body。
- TranscriptReplayCache 可以帮助离线 scorer，但不能成为 contract proof。
- Contract 不能绕过 ActionPolicy、PermissionProfile 或 Research max rounds。

### 顺手架构优化

```text
ResearchCompletionGate 只消费 Contract / ProofReview / EvidenceRuntime refs
Ghost work queue completion 只接受 Contract proof refs，不接受 research:* 字符串
RunTrace 只记录 bounded contract summary 和 proof_ref
```

### 验证

```text
缺 evidence refs 的 strong claim 不能 complete
failed AnalysisRun 不能 complete experiment check
unsupported open finding 阻止 clean complete
complete_with_limitations 必须列出 limitation refs
contract proof refs 可解析
contract 不含 raw prompt / transcript / webpage body
queued research done 不能绕过 contract gate
```

### A/B

local-only contract/proof refs 不需要 A/B。改变 queued done 语义、用户可见完成条件、
Research repair prompt 或模型可见 final answer contract 时，需要 queue/live A/B。

## 0.4.10 - Domain Source Trust + Research Brief v2

状态：规划。目标是把不同领域的证据标准、来源可信度和 Research-to-Code handoff
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

状态：规划。目标是做出能证明 0.4 价值的真实连续研究 harness，并用固定任务集验证
Codey 在泛化个人研究工作流上超过 baseline 和 OpenScience-style 对照。

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

需要。它验证 0.4 的纵向研究能力和对照评测结果，而不是只验证单点模块。

## 0.4.12 - Ghost Research Continuity + Topic Planner v1

状态：规划。目标是让 Codey 可以连续追踪长期研究主题，并把开放问题转成
topic-level plan，但不让记忆污染事实。这个版本必须晚于 Safe Context Epoch、
Research Contract Lite 和 `GhostHint != Evidence` 架构测试。

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

### 顺手架构优化

```text
把 Research Interest Queue 的 open_question refs 映射到 Evidence Runtime question refs
Ghost work queue completion 只接受 Contract proof refs
抽 TopicContinuityService：只产出 topic/open_question/stale refs，不读 raw note body
ResearchPipeline 消费 bounded continuity refs，不直接读 Ghost store
```

### 验证

```text
Ghost hint 不可进入 evidence_refs
旧 claim 必须重新验证或标 stale
topic plan candidate 不会自动触发 Research
Research continuity prompt 不出现 Ghost 内部词
disable Ghost 后 Research 行为回到 baseline
Ghost continuity admission 受 Safe Context Epoch 约束
```

### A/B

需要。它会改变 Research prompt 的模型可见 continuity，必须复用 0.4.6 journal，
并记录 UI interruption、unsupported claim、stale-source handling 和 follow-up usefulness。

## 0.5 之后的开放边界

0.4 先把 Evidence Research Runtime 做扎实。有限插件化应该放在 0.5 之后再考虑。
顺序应该是：

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

## 验证体系

0.3 已完成 Ghost 专用验证和能力边界验证：Run Trace、Prompt Envelope、
Capability Registry、Tool Contract、Action Policy、Event Matrix、Built-in Profiles
和 Run Details 都必须有 deterministic tests，不能靠 live provider A/B 发现架构回退。

0.4 需要新增 Evidence Research Runtime 验证。Research 质量不能只看最终 summary
是否像样，还要验证 source、evidence、claim、assumption、analysis run、artifact、
critic finding 和 Ghost continuity 的边界。

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
0.4.11 Longitudinal Research Harness + Comparison Benchmark（需要 live/comparison harness）
0.4.12 Ghost Research Continuity（模型可见 continuity 必须 A/B）
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
```

## 架构债边界

0.4 会继续让 Research、Evidence、Review、Ghost、Provider fallback 和本地持久化在
TaskRunner / Server 周围汇合。这个压力是真实的，但不能为了“看起来架构更好”做
big-bang rewrite。

需要承认并逐步收敛的债务：

```text
TaskRunner.run 承担 provider setup、routing、research、review、writer、ledger、trace
_RunFrame 已经包含 provider / conversation / trace / handoff / preflight / snapshot 等生命周期状态
后续 planner、multi-browser、recursive research 会继续放大这个 runtime context
```

正确拆分顺序：

```text
ResearchPipeline：只在 proof quality / evidence ledger / follow-up research 边界成熟后抽
ProviderPipeline：只在 provider setup / preflight / fallback / canary 边界稳定后抽
ReviewPipeline：只在 review input / fix loop / finding lifecycle 边界稳定后抽
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
