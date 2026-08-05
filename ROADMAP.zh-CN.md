# Codey 未来版本规划

这份文档记录 Codey 从 `0.3.0` 开始的路线。`0.2.25` 到 `0.2.33`
已经完成了内部地基：Run Ledger、Ledger Projection、ToolDefinition、
ContextSource、Provider Capability、Managed Outputs、Permission Profiles、
Headless JSONL Runner 和 Project-local Config。

接下来的主线不再是继续整理工具身体，而是进入新的产品阶段：

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

## 规划原则

1. Hebbian 网络是 0.3 的主线，不是后期装饰。
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

## 0.3 首版边界常量

第一版先写清量级，后续可以根据实测调整：

```text
MAX_GHOST_EVENTS = 5000
MAX_INBOX_ITEMS = 200
MAX_GHOST_NODES = 500
MAX_GHOST_EDGES = 2000
MAX_DIRECTIVE_CHARS = 1200
MAX_SIGNAL_TEXT_CHARS = 600
MAX_SIGNAL_QUOTE_CHARS = 240
MAX_SLEEP_INPUT_RUNS = 20
```

这些数字不是产品承诺，而是工程边界：Ghost 状态可以长期存在，但不能无限长大。

## 0.3.0 - Ghost Signal Extractor v1

### 做什么

第一步不是写 Hebbian 权重，而是先让 Codey 能用 LLM 判断“用户这句话是否包含显式学习信号”。

新增模块：

```text
codey/ghost/__init__.py
codey/ghost/schema.py
codey/ghost/signal_codec.py
codey/ghost/extractor.py
tests/test_ghost_signal_extractor.py
tests/manual/ghost_signal_extractor_ab.py
```

LLM extractor 输出固定 JSON：

```json
{
  "signals": [
    {
      "kind": "style_preference",
      "summary": "用户偏好回答先给结论",
      "scope": "user",
      "confidence": 0.86,
      "evidence_quote": "以后请先给结论"
    }
  ]
}
```

允许的 signal kind：

```text
style_preference
correction
long_term_goal
research_interest
action_tendency
boundary_preference
no_signal
```

### 为什么做

如果第一版靠正则判断：

```text
以后请...
不要...
我喜欢...
```

规则会很快变丑，而且会有大量漏洞。Nezha-mini 方向里最值得保留的一点就是：
语义判断交给 LLM，但永久写入权交给本地 gate。

### 边界

- extractor 只产生 candidate signal，不写长期记忆。
- `evidence_quote` 必须来自用户原文，不能凭空补。
- `confidence` 必须 bounded。
- 坏 JSON、未知 kind、缺 quote 都丢弃或进入 diagnostics，不写状态。
- 不从普通闲聊偷偷学习。
- 不进入 protocol repair prompt。
- 不影响 Research / Coding 执行。

### 运行策略

0.3.0 先走 shadow / manual / async-after-response 路径：

```text
用户主聊天先完成
  -> 后台或手动 extractor 读取本轮 user/assistant bounded text
  -> 生成 signal candidates
  -> 写 diagnostics 或 inbox candidate
```

首版不允许同步阻塞主聊天。extractor 失败等价于 `no_signal`。`state_home=None`
时禁用 Ghost 写入。用户或测试可以通过配置/CLI 关闭 extractor。

### A/B

新增 manual A/B：

```text
A: no extractor / fixture baseline
B: LLM signal extractor
```

Provider 覆盖：

```text
DeepSeek
Qwen
MiMo
StepFun
GLM
local API
```

指标：

```text
explicit preference recall
false memory rate
kind classification accuracy
evidence_quote groundedness
JSON compliance
provider disagreement
```

验收标准不是“LLM 很聪明”，而是：

```text
能抓住明确偏好和纠错
普通聊天误记率低
坏输出不会写入 Ghost 状态
```

## 0.3.1 - Ghost Memory Inbox v1

### 做什么

新增本地候选箱。所有学习信号先进入 inbox，不直接写永久状态。

新增模块：

```text
codey/ghost/store.py
codey/ghost/inbox.py
codey/ghost/gate.py
tests/test_ghost_inbox.py
```

状态位置：

```text
~/.codey/ghost/events.jsonl
~/.codey/ghost/inbox.json
~/.codey/ghost/state.json
```

控制入口必须在这一版就存在，即使 UI 延后：

```text
disable ghost learning
export ghost state
reset all ghost state
delete scope: user / project / session
list pending candidates
```

候选类型：

```text
preference_candidate
correction_candidate
goal_candidate
research_interest_candidate
action_tendency_candidate
boundary_candidate
```

候选状态：

```text
candidate
accepted
rejected
expired
superseded
```

### Gate 规则

gate 不做复杂智能，但做本地安全审计：

```text
schema valid
quote grounded
scope valid: user / project / session
confidence above threshold
conflict_key not silently overwriting active memory
size within budget
has event/run/session ref
```

第一版策略：

- 高置信、明确 style preference 可以自动接受。
- correction 默认保留 candidate，除非用户非常明确地说“记住，正确是...”。
- long-term goal 和 research interest 默认 candidate。
- boundary preference 默认 candidate，避免把临时抱怨永久化。

### Scope 优先级

Ghost 记忆按 scope 分层：

```text
session > project > user
```

规则：

- session correction 优先于 project/user correction。
- project scope 只能在同一个项目根内生效，不能泄漏到其他项目。
- user scope 是默认长期偏好，但不能覆盖明确的 session correction。
- 删除 scope 必须只删除对应范围，不得顺手清空其他层。

### Schema / Migration

长期状态必须有版本纪律：

- 每个持久文件必须有 `schema_version`。
- 不兼容 state 要 quarantine，不要静默读错。
- events 必须能重放生成 state projection。
- 坏 JSON、半写文件、未来 schema 都有测试。
- migration 失败不能影响 Codey 的非 Ghost 功能。

### 验收

- 原子写和坏文件恢复。
- candidate 去重和 conflict_key 合并。
- accepted/rejected/superseded 状态可重放。
- 单条和总文件都有大小上限。
- inbox 写入失败 fail-open，不影响 Codey 正常任务。

## 0.3.2 - Ghost Hebbian State v1

### 做什么

把 accepted signals 强化成本地 Hebbian state。

新增模块：

```text
codey/ghost/hebbian.py
tests/test_ghost_hebbian.py
```

核心数据：

```text
GhostNode
- id
- kind
- label
- weight
- scope
- confidence
- evidence_refs
- created_at
- updated_at
- last_reinforced_at

GhostEdge
- source
- target
- relation
- weight
- evidence_refs
- created_at
- updated_at
- last_reinforced_at
```

节点类型：

```text
style_preference
correction
long_term_goal
research_interest
action_tendency
boundary_preference
project_affinity
provider_affinity
```

更新规则：

```text
node.weight = decay(old_weight) + learning_rate * reward * confidence
edge.weight = decay(old_edge_weight) + learning_rate * coactivation
```

### 边界

- 纯 Python，不引入 torch。
- 不修改任何模型权重。
- edge 表示相关性，不表示事实。
- correction 节点优先级高于 style preference。
- correction 是用户纠错/偏好事实，不等同于 Research-verified external truth。
  涉及外部世界事实时，Directive 只能说“用户纠正过...”，不能把它当证据结论。
- 同一 evidence ref 不能重复强化。
- 权重 bounded，衰减 deterministic。
- state 可导出、可删除、可重建。

### 验收

- 明确偏好多次出现会强化。
- 久不用会衰减。
- 冲突偏好不会静默覆盖，生成 competing node 或 superseded 事件。
- 相同 evidence 不重复加权。
- 关联边不会渲染成事实断言。
- 无 `torch` / `transformers` import。

## 0.3.3 - Ghost Directive ContextSource v1

### 做什么

让 Hebbian state 真正影响外部模型说话方式。

新增模块：

```text
codey/ghost/directive.py
tests/test_ghost_directive.py
```

输出短 prompt：

```text
Ghost Directive:
- Prefer: concise, direct, answer-first Chinese.
- Avoid: marketing tone, vague reassurance.
- Corrections: ...
- Current long-term focus: ...
```

接入：

```text
ContextSource key = ghost_directive
```

默认策略：

- Chat 默认开启。
- `planning_readonly` 默认开启。
- Project Writer 默认生产不注入 Ghost Directive，直到 A/B 证明不破坏 JSON tool compliance。
  在此之前只允许 shadow 或显式实验开关开启。
- Research 默认不开，避免污染证据判断。
- Protocol repair prompt 永远不夹 Ghost Directive。

### 边界

Ghost Directive 不能：

```text
授权工具
改变 shell approval
改变 edit/run allowlist
覆盖 Research evidence
覆盖 project config
进入 JSON repair prompt
```

### A/B

新增：

```text
tools/ghost_directive_ab.py
tests/fixtures/ghost_directive_cases.json
```

指标：

```text
style adherence
correction hit rate
answer length
forbidden-tone hit rate
JSON tool compliance
coding smoke success
research citation quality
```

验收：

```text
B 更像用户偏好
B 不破坏工具协议
B 不降低 coding/research smoke 成功率
```

## 0.3.4 - Ghost Learning Loop v1

### 做什么

打通第一条闭环：

```text
user input
  -> signal extractor
  -> inbox candidate
  -> gate
  -> Hebbian reinforce
  -> next turn Ghost Directive changes
```

这一步开始让 Ghost 在日常 Chat 中真实学习，但仍然只学习通过 gate 的显式信号。

### 验收

- 用户说“以后短一点”，下一轮同 provider 回答变短。
- 切换 provider 后，方向仍然一致。
- “你错了”这种普通抱怨不会直接写成偏好。
- correction 不会污染 style preference。
- 学习失败不影响 Codey 正常聊天或任务。

## 0.3.5 - Ghost Continuity v1

### 做什么

让 Ghost 记得长期上下文，但不保存完整聊天全文。

来源：

```text
accepted memory candidates
correction ledger
run ledger projection
research synthesis notes
recent task summaries
project-local config
```

输出：

```text
recent_focus
long_term_goals
active_projects
open_questions
fresh_corrections
recently_reinforced_preferences
```

### 边界

- 不保存完整 transcript。
- 不保存完整源码。
- 不保存完整网页正文。
- 只从已有 bounded facts 和 accepted memory 构建 continuity summary。
- 可清空、可导出、可审计。

## 0.3.6 - Cognitive Sleep v1

### 做什么

借 Nezha-mini `cognitive_sleep.py` 的思想，但改成 Codey 风格的 bounded maintenance。

输入：

```text
最近 N 个 run ledger
最近 Research synthesis notes
Concept Graph / Unified Graph
accepted / rejected candidates
provider failure summaries
```

输出：

```text
new memory candidates
decay updates
merge suggestions
research open questions
provider/action tendency candidates
```

### 边界

- 手动触发或任务结束后短暂执行。
- 不后台无限 wander。
- 不自动永久写高风险事实。
- 所有结果先进 inbox。
- 可取消、bounded、fail-open。

## 0.3.7 - Ghost Router v1

### 做什么

Ghost 开始判断用户意图，但不直接执行工具。

输出只能是：

```text
chat
research
project
review
planning_readonly
```

Codey 仍然负责执行：

```text
TaskRunner
ResearchRunner
PermissionProfile
ToolRuntime
approval
verification
ledger
restore
```

### 第一版策略

先 shadow：

```text
ghost_suggested_route
actual_route
agreement / disagreement
reason
```

稳定后再考虑让 UI 的“自动模式”消费。

### 验收

- Ghost 不能输出 edit/run/shell。
- Ghost 不能批准 shell。
- Ghost 不能让 Project Writer 默认联网。
- route disagreement 有 ledger 记录。
- 手动入口优先于 Ghost 建议。

## 0.3.8 - Research Interest Queue v1

### 做什么

把“战争-氦气、战争-铜，之后发现铜和氦可能有关”变成可审计的研究问题队列。

数据：

```text
question
related_concepts
shared_neighbors
why_now
status
source_refs
priority
```

例子：

```text
helium ? copper
reason: shared neighbor: semiconductor supply chain
status: open_question
source: concept co-activation
```

### 边界

- open question 不是事实。
- missing edge 不落入 knowledge note。
- 只有用户显式启动 Research，Codey 才查证据。
- Research Controller 仍然负责工具边界和 citation quality。

## 0.3.9 - Affinity Index v1

### 做什么

把 Hebbian state 扩展成更完整的本地关联网络。

节点：

```text
user preference
project
research concept
correction
action tendency
provider behavior
task type
```

影响：

```text
Ghost Directive
Research query seed
Router tendency
Review strictness
Provider fallback hint
Project planning style
```

### 边界

- affinity 不是 truth。
- 事实判断仍然走 Research evidence。
- provider hint 不覆盖用户明确选择。
- routing hint 不覆盖 PermissionProfile。

## 0.3.10 - Ghost Control Surface v1

### 做什么

最后再做 UI。它不是人格面板，而是审计面板。

显示：

```text
Ghost 记住了什么
候选记忆有哪些
偏好权重最高的是什么
纠错规则有哪些
这次 Ghost Directive 为什么这样写
哪些 open questions 等待研究
```

操作：

```text
accept
reject
delete
demote
export
reset scope
```

### 边界

- 不做花哨人格编辑器。
- 不让用户在 UI 里放宽工具权限。
- 不显示内部技术词过多。
- 所有修改写入 Ghost events。

## 验证体系

0.3 需要新增一组 Ghost 专用验证，而不是只靠现有 coding/research tests。

### 单元测试

```text
tests/test_ghost_signal_extractor.py
tests/test_ghost_inbox.py
tests/test_ghost_hebbian.py
tests/test_ghost_directive.py
tests/test_ghost_learning_loop.py
tests/test_ghost_continuity.py
tests/test_cognitive_sleep.py
tests/test_research_interest_queue.py
tests/test_affinity_index.py
```

### 架构测试

必须锁住：

```text
codey/ghost 不 import torch
codey/ghost 不 import transformers
Ghost 不能 import tool_runtime 执行函数
ToolRuntime 不 import Ghost
Research evidence 不依赖 Ghost affinity
repair prompt 不包含 Ghost Directive
PermissionProfile 仍然是执行边界
```

### A/B 测试

逐步新增：

```text
tests/manual/ghost_signal_extractor_ab.py
tests/manual/ghost_directive_ab.py
tests/manual/ghost_router_ab.py
tests/manual/research_interest_queue_ab.py
```

每个 A/B 都要记录：

```text
provider
case_id
baseline output
ghost output
pass/fail metrics
protocol compliance
side effects
```

首版通过阈值方向：

```text
evidence_quote groundedness 必须 100%
false memory rate 必须低于明确阈值
JSON tool compliance 不能低于 baseline
coding smoke 不能回退
research citation quality 不能回退
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
Research quality unaffected when directive disabled
```

## 成功定义

0.3 做完后，Codey 应该从：

```text
一个可靠的本地 coding/research workbench
```

变成：

```text
一个有长期连续性的 Ghost，使用 Codey 作为安全身体工作和研究
```

用户体验上不应该变复杂。理想状态是：

```text
你继续自然聊天、研究、写代码
Ghost 慢慢更懂你
Codey 仍然可靠、可恢复、可验证
外部模型可以换，但“懂你的东西”留在本地
```

这是 0.3 的核心目标。
