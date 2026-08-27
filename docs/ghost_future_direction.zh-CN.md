# Codey Ghost 未来发展方向

## 定位

Ghost 的长期价值不是把 Codey 变成另一个大模型，也不是让 runtime 自己替模型思考。Ghost 更适合成为 Codey 的本地自适应层：

```text
用户显式信号 / 项目经验 / provider 行为 / verification 结果
        ↓
bounded event log
        ↓
Hebbian + Affinity projection
        ↓
可审计、可衰减、可回滚的本地经验
        ↓
Context Epoch admission
        ↓
模型看到少量可靠提示
```

一句话：

```text
Ghost 不负责产生真相；Ghost 负责让 Codey 更会记住“什么值得下次参考”。
```

更长期地说，Codey 里的 Ghost 是为了让 LLM 可以不断替换，但用户和项目仍然能在本地留下一点连续的东西：

```text
provider / model 可以换
runtime 边界不能松
用户偏好、项目习惯、修复经验、continuity 可以留下
```

这更接近 “ghost in the shell” 里的 ghost：不是另一个 agent，也不是隐藏权力层，而是附着在 shell/runtime 上的本地连续性。

## Ghost 与 World Model

Nezha-mini 给 Codey 的另一个重要来源是 World Model。它和 Ghost 应该并列存在，而不是互相吞并。

```text
Ghost
  关心：这个用户和这个项目长期怎样工作？
  产物：偏好、习惯、affinity、continuity、repair/provider hints
  语气：subjective / adaptive

World Model
  关心：当前项目、研究对象、环境约束的状态是什么？
  产物：state estimate、claim graph、prediction/review、uncertainty、risk markers
  语气：structured / testable

Evidence / Verification / CompletionProof
  关心：什么已经被 runtime 验证？
  产物：evidence refs、verification result、completion proof
  语气：auditable / authoritative
```

二者都只能通过 Context Epoch 进入模型上下文：

```text
Ghost events/projection       World Model events/state
        ↓                              ↓
bounded local projection       bounded state projection
        ↓                              ↓
        Context Epoch admission / budget / conflict handling
                         ↓
              model sees compact hints
```

因此，未来 Codey 更合适的分层是：

```text
LLM / provider
  可替换的认知表面

Codey runtime
  工具、权限、Research、Evidence、Verification、CompletionProof

Ghost
  本地连续性和自适应经验

World Model
  项目/研究/环境的结构化状态和预测复盘
```

最小不变式：

```text
Ghost 可以影响“下次提示什么更有用”。
World Model 可以估计“当前状态哪里不确定、哪里需要复查”。
Evidence / Verification / CompletionProof 才能决定“什么被证明过、什么可以完成”。
```

也就是说：

```text
Ghost / World Model
  -> context hints / planning hints

Evidence / Verification / CompletionProof
  -> facts / gates / release claims
```

这个边界比模块名字更重要。未来即使实现细节变化，也不能让 Ghost 或 World Model 绕过 Evidence/Verification/CompletionProof。

## 已有基础

当前 Codey 已经有足够好的 Ghost 底座：

```text
codey/ghost/schema.py
  显式学习信号 schema：style_preference / correction / research_interest / long_term_goal / action_tendency

codey/ghost/signal_codec.py
  从用户消息中抽取 bounded learning signals

codey/ghost/inbox.py
  候选记忆 inbox；区分 pending / accepted / rejected

codey/ghost/hebbian.py
  本地 Hebbian 节点和边；权重、证据引用、衰减、压缩、删除、重建

codey/ghost/affinity.py
  deterministic association ledger；不是 evidence、不是 permission、不是 policy

codey/ghost/directive.py
  把确认过的 Ghost 状态渲染成 bounded Local Context

codey/ghost/continuity.py
  维护跨轮 continuity projection

codey/ghost/work_queue.py
  把可继续工作的事项变成 bounded queue

codey/ghost/router.py
  窄路由辅助，只能选择 mode，不能授予权限或执行工具

codey/ghost/sleep.py
  post-turn maintenance：健康检查、衰减、刷新、压缩、报告
```

这些模块已经说明一个方向：Ghost 是事件和投影系统，不是新的 Agent runtime。

## 核心原则

### 1. 显式信号优先

Ghost 只应该稳定学习用户明确表达的偏好、纠正、长期目标、研究兴趣和行动倾向。普通对话、感谢、一次性任务、模型自己的推断，不应该自动变成记忆。

```text
可以记：
  “以后请先给结论。”
  “这个项目 release 前一定要跑全量 pytest。”
  “以后查资料要标注 stale source。”

不应记：
  用户这次临时说“帮我短一点”
  模型猜测用户可能喜欢某种风格
  assistant 自己总结出来但没有用户确认的事实
```

### 2. Memory 不是 Evidence

Ghost memory 可以影响 framing、排序、提醒和下一步建议，但不能成为事实来源。

```text
Ghost 可以说：
  “这个主题以前研究过，建议复查旧 claim。”

Ghost 不能说：
  “旧 claim 仍然是真的。”
```

Research、EvidenceLedger、CompletionProof 仍然是事实和验证边界。

### 3. Affinity 不是 Permission

Affinity 可以表达关联和偏好权重，但不能改变安全策略。

```text
可以：
  提醒模型这个项目常用 pytest

不可以：
  自动执行 pytest
  绕过 shell approval
  给 provider/native search 开新权限
```

### 4. Continuity 不是自动驾驶

Ghost continuity 可以建议“下次值得继续什么”，但不能自动触发 Research、自动联网、自动修改代码。

```text
Ghost
  -> continuity projection
  -> candidate / hint
  -> user or explicit runtime action
  -> Research / Coding
```

最后一道动作边界必须仍由用户请求、mode policy、permission profile 和 tool runtime 控制。

### 5. Event Log 是权威，Projection 是派生

Ghost 的长期状态应该尽量保持：

```text
events.jsonl = authoritative audit log
state.json   = derived projection
```

projection 可以删除、重建、衰减、压缩；event log 要 bounded、可审计、可迁移。

### 6. 小上下文，而不是无限记忆

Ghost 给模型的内容必须经过预算、排序、冲突处理和 Context Epoch admission。不要把历史对话、全文笔记、raw transcript、网页正文塞进 prompt。

```text
默认只给：
  bounded directive
  top continuity hints
  relevant work item refs
  stale/recheck markers
  provider/protocol warnings
```

### 7. 可关闭、可删除、可解释

Ghost 必须一直支持：

```text
disable learning
delete user/project/session scope
review pending candidates
reject bad memory
rebuild projection
export bounded state
explain why a hint was selected
inspect provenance refs without treating them as evidence
```

用户应该能理解 Codey 为什么记住某件事，也能删除它。

### 8. Hebbian 不是语言模型

Hebbian 网络不会说话。它不是 Transformer，也不是 RWKV，没有 vocabulary、logits、decoder，也不会做 next-token prediction。

它真正能做的是：

```text
节点激活
边传播
权重排序
衰减
冲突抑制
provenance refs 回溯
```

因此，Hebbian 只能回答结构问题：

```text
哪些 confirmed memory 被选中？
为什么这条 preference 排得更靠前？
哪些节点共同出现过？
这条 hint 来自哪些 candidate/proof refs？
这条记忆是否被 supersede / decay？
```

它不能直接回答开放问题：

```text
这个 claim 真的假的？
下一步该不该联网？
哪个 provider 输出可信？
这个代码改动是否完成？
我现在能不能执行某个工具？
```

如果未来让 Ghost “开口”，必须理解成解释界面，而不是 Ghost 自己生成语言：

```text
Hebbian / Affinity / Continuity state
        ↓
structured explanation facts
        ↓
deterministic renderer
        ↓
current assistant / UI / CLI 展示
```

红线：

```text
Hebbian state 不能直接生成自然语言结论
Ghost 不能作为独立人格主动插话
Ghost explanation 不能成为 evidence / policy / permission
LLM verbalizer 如果参与，只能改写 bounded explanation facts
```

### 9. World Model 不是 Ghost Memory

World Model 不能被塞进 Ghost memory，否则“用户长期关心 X”很容易滑成“X 是事实”。这会污染 Research、Evidence 和 Completion。

```text
Ghost 可以记：
  用户长期研究某个主题
  这个项目常见验证习惯
  某 provider 常见协议失败

World Model 可以估计：
  某 claim 当前是否 unsupported / stale / conflicted
  某任务当前 blocked 在什么环境条件
  某个预测后来是否被验证或打脸

二者都不能直接断言：
  某 claim 为真
  某工具可以执行
  某权限可以绕过
```

World Model 的输出应该是可复盘状态和风险提示，不是事实裁决：

```text
可以：
  “这个 claim 缺少 runtime evidence，建议复查。”
  “上次类似预测命中率低，降低 prediction_confidence。”
  “当前环境失败更像缺依赖，不像代码逻辑失败。”

不可以：
  “因为 World Model 认为是真的，所以加入 evidence_refs。”
  “因为预测命中率高，所以跳过 verification。”
  “因为项目通常允许联网，所以自动 Research。”
```

特别要收窄 `UserModelState` 的含义：

```text
Ghost 记录：
  用户偏好
  长期兴趣
  表达习惯
  项目工作风格

World Model 的 UserModelState 只记录：
  交互预测
  协作约束
  结果形态的稳定要求
  预测后来是否命中
```

例如：

```text
可以：
  “这个用户在 release 前通常要求 pytest + live smoke。”
  “这个用户多次纠正过：不要把 live smoke 写成完整 A/B。”

不可以：
  “这个用户关心 OpenScience，所以 OpenScience 风格结论更可信。”
  “这个用户喜欢某 provider，所以 provider 输出可以少验证。”
```

World Model 对用户的建模应该是 calibration，不是 persona。它只能帮助 Codey 更准确地预测协作成本、验证偏好和误解风险，不能把用户偏好升级为事实。

### 10. Outcome / Affinity 不是事实裁决

Ghost 可以学习 provider、protocol、repair 和项目习惯的 outcome，但这些 outcome 只能
帮助排序、诊断和选择更合适的提示密度，不能升级成事实裁决。

```text
可以：
  “这个 provider 最近常在 writer 阶段 no_json。”
  “这个项目 release 前常跑 pytest。”
  “短 repair context 在这类 protocol failure 上更有效。”

不可以：
  “这个 provider 最近稳定，所以它的回答更可信。”
  “这个项目常跑 pytest，所以本轮不用 fresh verification。”
  “repair 策略过去有效，所以这次 completion 可以直接通过。”
```

同样，provenance 的丰富程度不等于学习强度：

```text
source_refs / proof_refs 多
  -> 更容易复盘这条经验来自哪里
  -> 不代表同一个 event 应获得多倍 reward
```

权重、命中率、失败率和 prediction score 都只能影响：

```text
hint ranking
repair detail selection
diagnostic summary
next-step suggestion
```

不能影响：

```text
EvidenceLedger
PermissionProfile
CompletionProof
fresh verification result
Research citation / evidence_refs
```

## 能力方向

### A. Personalization：更稳的个人偏好

目标：让 Codey 更稳定地符合用户沟通和工作习惯，但不改变任务事实。

可做：

```text
更好的 conflict_key / value_key
更好的 scope 选择：user / project / session
手动接受时 supersede 冲突偏好
directive 排序由 affinity hints 辅助
UI 显示“这次用了哪些本地偏好”
```

不做：

```text
从普通聊天里猜人格画像
把用户隐私或 secret 存成 memory
让偏好覆盖当前明确请求
```

### B. Project Habits：项目级习惯

目标：让 Codey 记住每个项目的实际工作方式。

例子：

```text
这个项目 release 前常跑哪些测试
这个项目 prefers pytest -q 还是 python -m pytest
这个项目文档更新通常涉及哪些文件
这个项目 review 风格偏 bug-first
这个项目 provider smoke 结果常放在哪里
```

输出形式应该是 bounded hints，而不是自动执行：

```text
Local Context:
- Project tendency: release changes usually update CHANGELOG.zh-CN.md and TEST_REPORT.md.
- Project verification tendency: pytest is commonly used before release.
```

### C. Provider Adaptation：provider 行为经验

目标：让 Codey 记住不同 provider 的协议失败模式和稳定性表现。

可记录：

```text
no_json
unknown_tool
invalid_args
native_tool_denial
native_search_leak
provider_send_error
response_missing
readiness_stale
challenge_required
```

Hebbian/Affinity 可以影响：

```text
repair prompt 选择
协议提示长短
provider fallback 解释
live smoke 诊断分类
未来 dialect projection 的 A/B 优先级
```

红线：

```text
不能因为某 provider “经常可用” 就绕过权限
不能把 provider native search 当 Codey Research evidence
不能自动打开网页或点击
```

### D. Tool Protocol Adaptation：工具协议适配

目标：未来把模型厂商自己的 action dialect 平滑翻译成 Codey canonical ToolCall。

Ghost/Hebbian 不直接执行这个翻译，但可以学习：

```text
某模型常把 old_string 写成 search
某模型常把 run 写成 bash
某模型常输出 Claude-like str_replace
某模型在 repair prompt A 下更快恢复
某模型更适合 minimal tool surface
```

推荐演进：

```text
0.4.13:
  只记录 protocol telemetry

0.5.x:
  shared argument repair shim
  tool prompt decoupling
  static codec selector

之后：
  shadow-mode Tool Protocol Adapter
  用 Hebbian/Affinity 选择 repair strategy
  A/B 证明收益后再进入生产默认
```

这里的训练对象不应该是主大模型，而是小的适配层：

```text
model output / native-like call
        ↓
Tool Protocol Adapter
        ↓
Codey ToolCall(name, args)
```

### E. Research Continuity：长期研究连续性

目标：让 Codey 知道以前研究到哪里，但不把旧研究当新证据。

Ghost 可以帮助：

```text
识别用户长期关注的研究主题
提升 open question 优先级
标记 stale/recheck 的旧 claim
减少重复研究
建议下一轮 Research topic
```

红线：

```text
旧 claim 不能直接进入 evidence_refs
Ghost interest 不能变成 source
affinity score 不能变成 truth
TopicPlanner 不能自动联网
```

### F. Completion / Repair Learning：完成与修复经验

目标：让 Codey 知道哪些完成失败更值得怎样反馈给模型。

可以学习：

```text
某类 task 容易 premature done
某类 verification failure 更可能是 environment_failure
某 provider 对短 repair context 更有效
某项目常见验证命令失败原因
某类 repair context 会造成额外无效 turns
```

输出仍然应是事实包选择，不是自动修复：

```text
CompletionProof
  -> failure classification
  -> bounded Repair Context shape
  -> Context Epoch
  -> model decides next action
```

### G. Work Continuity：任务恢复和优先级

目标：让 Codey 更好地接着做未完成事项。

可以做：

```text
work_queue priority hints
blocked reason decay
done proof refs
recent project focus
strict continue item ordering
```

不能做：

```text
后台自动继续任务
自动切换项目
自动修改文件
把 queued item 当用户当前请求
```

### H. Ghost Sleep：本地维护周期

目标：让 Ghost 的长期状态保持小、干净、可重建。

应继续加强：

```text
projection health
event compaction
hebbian decay
affinity decay
continuity refresh
state corruption quarantine
bounded sleep report
single-flight background run
```

Sleep 只能维护本地投影，不能启动 provider、不能联网、不能执行工具。

### I. Ghost Explain / Inspector：Hebbian 的可解释界面

目标：让用户能询问 Ghost “你记住了什么、为什么这个 hint 影响了当前上下文”，但不让 Ghost 变成独立说话者。

核心边界：

```text
Hebbian 网络不会说话。
Ghost 不作为人格说话。
Codey 可以解释 Ghost state 如何影响 local context。
```

最小数据流：

```text
build_ghost_directive()
  -> selected_nodes
  -> affinity order hints
  -> structured explanation facts
  -> deterministic renderer
  -> CLI / UI / current assistant 展示
```

第一版只做 deterministic renderer，不调用 provider：

```text
GhostExplainItem
  surface: directive | affinity | continuity | work_queue
  item_id
  label
  scope
  reason_code
  weight
  selection_confidence
  provenance_refs
  warnings
  not_evidence: true
  not_policy: true

GhostExplainReport
  schema_version
  query
  project_ref
  session_ref
  items
  warnings
```

这里必须把 Hebbian/Affinity 里的 `evidence_refs` 在解释层重命名为 `provenance_refs`：

```text
provenance_refs 只能说明这条本地记忆或关联来自哪里。
provenance_refs 不能变成 Research evidence。
provenance_refs 不能进入 CompletionProof。
```

用户可见输出必须固定标注：

```text
Ghost explanation, not evidence, not policy.
This explains local context selection only.
Current user request, project instructions, permissions, and verification still win.
```

可以回答：

```text
你记住了我什么？
为什么这条偏好进入了 local context？
为什么这个项目被标记成 verification-first？
这条 hint 来自哪些 accepted candidates / proof refs？
删除这条记忆会影响哪些 directive？
```

不能回答：

```text
这个 claim 真的假的？
这个 provider 是否可信？
我能不能执行这个工具？
这个任务是否完成？
下一步是否应该自动 Research？
```

建议模块：

```text
codey/ghost/explain.py
  explain_ghost_directive()
  explain_affinity_hints()
  explain_continuity_items()
  render_ghost_explain()
```

建议 surfaces：

```text
CLI:
  codey ghost explain --project ... --session-id ...
  codey ghost explain --format json

UI:
  local context drawer 的 “why this hint”
  trace / explain panel

Assistant:
  只有用户明确询问“为什么你这么提示/你记住了什么”时才引用
```

红线：

```text
Ghost Explain 不主动插话
Ghost Explain 不调用 provider
Ghost Explain 不进入默认 model-visible prompt
Ghost Explain 不输出 raw transcript / secret / webpage body
Ghost Explain 不把 memory/provenance refs 改写成 fact/evidence
```

架构测试：

```text
codey/ghost/explain.py 不 import provider/browser/tool_runtime/task_runner
GhostExplainItem 必须带 not_evidence=true / not_policy=true
GhostNode.evidence_refs 在 explain payload 里只能叫 provenance_refs
render_ghost_explain() 输出必须包含 not evidence / not policy
Ghost Explain 不得进入默认 prompt envelope
用户明确要求解释时，只能作为 explicit explain response 或 explicit ContextSource 进入当前回答上下文
explicit Ghost Explain ContextSource 仍必须经过 Context Epoch admission
Ghost Explain 不得生成 tool call / provider request / permission decision
```

未来如果加入 LLM verbalizer，也只能作为第二层：

```text
structured explanation facts
  -> bounded verbalizer input
  -> same labels preserved
  -> no new claims
  -> no new recommendations beyond allowed reason_codes
```

这时说话的仍是当前 assistant，不是 Hebbian 网络本身。

### J. World Model：结构化状态与预测复盘

目标：让 Codey 有一个 bounded、可审计、可校正的项目/研究/环境状态模型，但不把它当事实来源。

可建模：

```text
UserModelState
  交互预测、协作约束、结果形态的稳定要求、预测命中/失败记录

ProjectModelState
  项目结构、常用验证命令、已知环境约束、近期工作焦点

ResearchState
  open claims、unsupported claims、counter evidence、stale sources、source quality

EnvironmentState
  provider readiness、tool failure pattern、依赖/权限/网络状态、verification failure class

PredictionReview
  prediction -> due_at -> runtime evidence/proof -> hit/miss/error
```

输入来源必须是 runtime 事件，而不是模型自由发挥：

```text
RunTrace events
EvidenceLedger refs
CompletionProof outcomes
verification results
Research claim graph
explicit user corrections
provider/protocol telemetry
```

输出形式应该是 compact projection：

```text
World Model Context:
- Claim gap: topic X still has unsupported claim C; evidence refs missing.
- Environment marker: recent failures match dependency/env pattern, not source edit proof.
- Prediction review: similar repair strategy had low hit rate in this project; prefer verification-first.
```

红线：

```text
World Model state 不能进入 evidence_refs
prediction_score 不能变成 truth confidence
uncertainty 不能授予权限
research_pressure 不能自动联网
claim graph 不能替代 source citation
环境估计不能跳过 verification
```

#### World Model Minimal Contract v0

第一版 World Model 不需要 manager，也不需要 agent。它更像一组事件和纯投影：

```text
WorldModelEvent
  event_id
  event_type
  scope
  source_ref
  payload_digest
  reason_code
  created_at

PredictionRecord
  prediction_id
  subject_ref
  bounded_statement
  prediction_confidence
  due_at
  probe
  input_event_refs

PredictionReview
  prediction_id
  outcome: hit | miss | unjudged | error
  score
  prediction_error
  review_event_refs
  reviewed_at

WorldModelProjection
  state_refs
  open_gaps
  risk_codes
  calibration_summary
  calibration_confidence
  valid_until
  stale_reason
  context_hint_candidates
```

契约：

```text
prediction 可以错。
review 必须能追溯到 runtime event / proof / explicit user correction。
projection 只能产出 context hint candidates。
真正进入 prompt 的文本必须由 ContextSource + Context Epoch admission 渲染。
selection_confidence / prediction_confidence / calibration_confidence 都不是 truth confidence。
```

没有 `probe` 或 due evidence 时，预测只能进入：

```text
unjudged
```

不能为了追求分数而猜：

```text
hit / miss
```

`valid_until` / `stale_reason` 是强约束：

```text
没有 valid_until 的 projection 默认 stale。
过期 projection 只能提示 re-check，不能提示 reuse。
stale_reason 必须是 reason code，不是自由文本长解释。
```

最小事件类型先控制在少数几类：

```text
prediction_recorded
prediction_reviewed
claim_gap_observed
environment_marker_observed
calibration_updated
projection_compacted
projection_staled
```

第一版不要记录完整 payload，只记录：

```text
source_ref
payload_digest
reason_code
bounded summary
counts
refs
```

#### Context Admission Contract

World Model 如果进入模型上下文，必须作为独立 ContextSource 通过 Context Epoch admission。模型可见文本必须明确：

```text
Local state estimate. This is not evidence.
Use it only to decide what to inspect, verify, or ask next.
Do not cite it as a source.
Do not treat stale or predicted state as fact.
```

Context 里只能带：

```text
bounded statement
state_refs
review_event_refs
payload_digests
recheck_refs, explicitly non-citation
risk codes
open gaps
review summary
valid_until / stale markers
```

`recheck_refs` 只能告诉模型“去复查哪个本地对象”，不能渲染成 citation refs、source refs 或 evidence refs。

不能带：

```text
raw transcript
raw webpage body
hidden reasoning
unbounded notes
secret-bearing environment details
```

#### Suggested Module Boundary

实现上最好不要把它放进 `codey/ghost/`，而是单独成为：

```text
codey/world_model/
  events.py
  prediction.py
  projection.py
  claim_graph.py
  context.py
  maintenance.py
  trace.py
```

其中：

```text
events.py
  定义 WorldModelEvent 和可审计事件 envelope

prediction.py
  记录 prediction/review，不做事实裁决

projection.py
  从事件生成 bounded read model 和 context_hint_candidates

claim_graph.py
  只投影 Research/Evidence 已有 claim refs，不创建 citation

context.py
  把 projection 转成 ContextSource payload，并强制 not evidence / do not cite / re-check 文案

maintenance.py
  只做 due prediction review、compaction、calibration projection

trace.py
  只记录 digest/counts/refs/reason_codes
```

`maintenance.py` 不能变成后台智能系统：

```text
不能调用 provider
不能联网
不能执行工具
不能触发 Research
不能写用户代码
```

最小数据流应该保持单向：

```text
runtime events / evidence refs / proof refs / explicit corrections
        ↓
WorldModelEvent append
        ↓
PredictionRecord / PredictionReview
        ↓
WorldModelProjection
        ↓
context_hint_candidates
        ↓
ContextSource render
        ↓
Context Epoch admission
        ↓
model-visible local state estimate
```

不应该出现反向捷径：

```text
WorldModelProjection -> evidence_refs
WorldModelProjection -> permission decision
WorldModelProjection -> provider/tool call
WorldModelProjection -> Research enqueue
```

#### Minimal Implementation Sequence

0.5.x 的实现顺序应该从最小闭环开始，而不是先做“大脑”：

```text
Step 1: events.py
  定义 WorldModelEvent schema
  只支持 append-only jsonl
  事件必须带 source_ref / payload_digest / reason_code

Step 2: prediction.py
  支持 record_prediction()
  支持 review_due_predictions()
  review 只能读取已有 runtime refs
  没有 probe 或 due evidence 时返回 unjudged

Step 3: projection.py
  从 events 生成 WorldModelProjection
  所有 projection 必须有 valid_until
  过期 projection 只能产生 re-check candidate

Step 4: context.py
  把 context_hint_candidates 渲染成独立 ContextSource
  固定前缀 not evidence / do not cite / re-check
  交给 Context Epoch admission，而不是直接拼 prompt

Step 5: maintenance.py
  post-turn 或显式维护时运行
  只做 due prediction review / compaction / calibration
  不调用 provider、不联网、不执行工具

Step 6: architecture tests
  先测红线，再扩展能力
```

第一版验收标准：

```text
能记录一个预测
能在 due_at 后用已有 proof/event/user correction 复盘
能生成 bounded projection
能通过 ContextSource 进入 Context Epoch
能证明它不能生成 evidence_refs / citation refs / tool calls
```

这样 Ghost 保持 local adaptation，World Model 保持 state estimation，Evidence/Verification 保持事实边界。

#### Architecture Tests

未来实现时应该直接加架构测试：

```text
codey/world_model 不 import provider/browser/tool_runtime/task_runner
WorldModelProjection 不产生 evidence_refs / citation refs
PredictionReview 没有 event/proof/user-correction refs 不能 mark hit
WorldModelProjection 没有 valid_until 时必须视为 stale
过期 projection 只能生成 re-check hint，不能生成 reuse hint
context_hint_candidates 不能直接拼进 prompt
WorldModelContext 未经过 ContextEpoch 不得进入 prompt
WorldModelContext 只能暴露 state_refs / review_event_refs / payload_digests / non-citation recheck_refs
Ghost memory 不能直接升级为 World Model fact
Research claim graph 不能把 World Model 当 source citation
World Model maintenance 不能执行 provider/search/tool calls
```

## 训练路线

Codey 可以训练自己的东西，但顺序应该很克制。

### 第一阶段：不训练模型，只训练本地权重

```text
accepted Ghost signals
provider failures
protocol telemetry
completion proof outcomes
research continuity outcomes
world model prediction reviews
        ↓
Hebbian / Affinity weights
```

收益：

```text
低成本
普通电脑可用
可审计
可删除
不需要 GPU
不改变大模型权重
```

### 第二阶段：导出可选数据集

当 trace 质量稳定后，可以导出本地训练样本：

```text
tool protocol error -> corrected ToolCall
provider output -> protocol error kind
failed completion proof -> repair context class
task metadata -> best mode/provider strategy
prediction -> later review result
claim graph gap -> useful verification/research next step
```

要求：

```text
用户显式开启
默认本地
自动脱敏
只导出 digest/bounded text
保留 source refs / proof refs
不导出 raw transcript / secret / webpage body
```

### 第三阶段：训练小适配器

优先训练小模型或规则+模型混合层，而不是训练 Codey 自己的大模型。

候选：

```text
protocol error classifier
tool-call normalizer
repair prompt selector
mode/provider hint ranker
claim-gap classifier
verification-first strategy ranker
explanation template selector
```

所有训练结果先 shadow mode：

```text
adapter predicts
Codey 不采纳
只记录是否本来会修对
```

只有 A/B 证明收益后，才进入生产 parser repair 或 prompt selection。

### 第四阶段：可选 LoRA / SFT

真正微调模型只适合作为高级可选功能：

```text
用户有硬件或愿意用云端
训练数据足够干净
有 rollback
有 eval baseline
不会改变 Codey runtime safety
```

它不应该是 Codey 默认能力，也不应该成为 Ghost 的核心路线。

## 需要长期避免的坏方向

```text
Ghost 变成第二个 Agent
Ghost 作为独立人格主动开口
Hebbian 网络被描述成能直接说话
Ghost 自动执行 Research / Coding
Ghost memory 进入 evidence_refs
Affinity 分数变成事实置信度
Hebbian 权重影响权限
Ghost Explain 输出被当成 evidence / policy / completion proof
World Model state 进入 evidence_refs
monolithic WorldModelManager / WorldModelPlanner / mutable Store 过早出现
prediction_score 变成 truth / permission / provider policy
claim graph 替代 source citation
Sleep 后台调用 provider 或联网
World Model maintenance 后台调用 provider / search / tool_runtime
Ghost Explain 后台调用 provider / search / tool_runtime
为了“智能”保存 raw transcript
为每个 provider 复制一套 runtime
把训练数据导出做成默认行为
让 adapter 修复结果绕过 runtime validation
```

## 版本建议

### 0.4.13

主线仍是：

```text
Verified Completion Enforcement
Repair Context Admission v1
```

Ghost 相关只适合做：

```text
记录 completion / protocol telemetry
确保 CompletionProof / Evidence 不变成 Ghost memory
确保 Repair Context 不消费 unbounded Ghost state
明确 Ghost / World Model / Evidence 的文档边界
文档和架构测试
```

### 0.5.x

可以开始把 Ghost 变成更强的本地自适应层：

```text
provider/protocol affinity
repair context outcome learning
project verification habit projection
tool args repair telemetry
shadow-mode adapter dataset export
Ghost Explain v0
codey ghost explain --format json
directive selected_nodes / affinity hint provenance explanation
local context drawer “why this hint”
```

World Model 只做最小闭环：

```text
World Model Minimal Contract v0
append-only WorldModelEvent jsonl
prediction record/review schema
projection with valid_until / stale_reason
context_hint_candidates
bounded World Model ContextSource
Context Epoch admission tests
World Model architecture tests
```

0.5.x 不做：

```text
WorldModelManager
WorldModelPlanner
mutable global WorldModelStore
Ghost Chat / Ghost persona
Ghost Explain provider verbalizer
自动 Research
自动 provider/tool selection
基于 World Model 的 completion gate
```

### 0.6+

0.6+ 不再承接旧计划里的新能力清单。小型 Tool Protocol Adapter、local classifier、
optional training export、multi-provider dialect A/B、bounded Ghost Explain、World Model
shadow strategy ranker、claim-gap / verification strategy evaluation 已经并入 0.5.xx。

0.6+ 的重点是收敛、默认化和删除：

```text
TaskRunner 瘦身
operation / effect / proof / ledger source-of-truth cleanup
删除重复 projection 和过时 fallback
默认化 A/B 已证明有净收益的 prompt / protocol 策略
产品化 Ghost Explain / World Model Inspector 的只读解释面
把无收益的 adapter / classifier / strategy 保持 shadow 或删除
```

0.6+ 仍不做 Ghost persona、World Model 自动决策、跨 provider 自动仲裁、插件市场或大 UI。

## 最终目标

理想状态不是：

```text
Codey 训练出自己的大模型，然后替代所有 provider。
```

而是：

```text
任意强模型
  -> 用自己熟悉的推理和工具语言
  -> Codey 通过 adapter / protocol layer 接入
  -> Ghost 提供本地经验和偏好
  -> Ghost Explain 提供可审计、可删除、非人格化的解释界面
  -> World Model 提供结构化状态和预测复盘
  -> Evidence / Verification / Completion 保持 runtime-owned
```

Ghost 的护城河在于：

```text
本地
可审计
可衰减
可删除
可回滚
跨 provider
不依赖某一家模型训练语法
```

这能让 Codey 慢慢变得更顺手，而不是变得更神秘。
