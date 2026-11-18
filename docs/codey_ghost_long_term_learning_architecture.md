# Codey / Ghost 长期学习架构：从经验到可塑性认知网络

> 2026-11-19：冷启动重写后的现行实现见 [Memory / Ghost 所有权边界](memory-boundary.zh-CN.md)。本页此前实现说明与实验结论保留为历史记录，不是当前生产接口。

> 文档类型：长期研究愿景，不是当前已实现的功能清单或发布验收标准。
> 与[Ghost 方向](ghost_future_direction.zh-CN.md)一起阅读；当前能力见
> [能力说明](codey_capabilities.zh-CN.md)。
> 2026-10-19 校准：本文的可塑性、预测和语言结构都是研究假设，未被偏好投递与预算路由实验验证。
> 近期工程顺序以 [Roadmap](../ROADMAP.zh-CN.md)为准，实验事实与晋级限制见
> [Ghost 决策基线](ghost_future_direction.zh-CN.md#偏好投递与预算路由的实验决策基线2026-10-19)。
> 2026-11-18 修订：产品主线是共同经历的连续性与个人适应；下文神经机制为独立研究候选，
> 不要求先实现整套网络或协议适配器，产品运行不加载本地 HF 模型。此次仅修改文档，不恢复生产实现或新增实机实验；另行获准的 Hopfield 独立实验已完成。

> 目标：设计一个可以用多年时间持续研究、逐步增强的 Ghost。
>
> 核心思想不是“现在就做一个 Hebbian LLM”，而是先让 Codey 给 Ghost 留下稳定、干净、可演化的学习接口，使未来可以逐步实验高维神经元、recurrent connections、temporal Hebbian learning、inhibition、normalization、synaptic decay、eligibility traces、neuromodulation、hierarchical representations、sparse distributed representations、winner-take-all 和 context-dependent connections。
>
> **Codey 负责可靠地观察现实；Ghost 负责从经验中形成可塑的内部结构。**

## 个人适应的状态与学习契约（2026-11-18）

产品问题是“一次共同经历如何改变下一次与你相处的方式”。相同目标适用于代码协作、
研究与日常交流。当前实现保存有界的已完成回合，在正常主调用前检索；自动结构化抽取与
确认已停止。以下理解状态、反馈归因和学习接口是研究契约，不是已落地 schema 或新总线要求。

| 层次 | 语义与最小信息 | 更新边界 |
|---|---|---|
| 经历 | 已发生的用户表达、实际行为与结果；时间、scope、run/来源标识及有界语义内容 | 事实与后续解释分开；失效、撤回、删除按现有生命周期执行，不能靠解释改写结果 |
| 当前理解 | 一个关于近期处境、目标或相处方式的有条件判断；依据、适用情境、不确定性、更新时间与有效期 | 当前请求优先；临时状态不过度泛化；允许纠正、替代、过期与撤回 |
| 学习状态 | 由可归因反馈更新的统计量、关联或小参数；机制/表示版本及可重放来源 | 权重变化须能关联到反馈及行为收益，不把关联强度当作用户事实或偏好确认 |

先通过已有经历检索让正常主调用理解上下文，不要求每条消息都形成长期偏好或让用户
逐条审批短期交流。需要持久化的新推断作为候选，遵守明确的准入和确认规则；
现有人工确认边界继续生效，自动接受必须另行定义，不能以“学习”名义绕过。

一个最小适应实验的契约是：

```text
观察：用户在一次方案讨论中明确要求先分析再修改
判断：在探索方案的情境下，先讨论可能减少重复纠正（来源、scope、有效期）
消费：下一次相关讨论使用此上下文，记录实际采取的协作方式
反馈：绑定该交互/行为的明确评价或纠正；没有可归因观测为 unjudged
更新：修正、维持或撤回判断；用户明确要求实施时不沿用旧的临时限制
```

这个例子也可能只测到通用指令遵循，不能单独证明个人适应。首个对照应固定当前问题与
任务条件，为各自独立的个人历史设置“先比较选项”与“先给推荐”等不同明确反馈，
比较无历史、仅检索相同经历与新增状态的行为；再测试情境变化、偏好反转和临时要求到期。
当前明确指令覆盖旧理解作为共同边界检查，与依赖个人经历的净收益分别报告。

候选至少能关联判断来源、实际消费的交互/行为和之后的反馈，先在现有领域事件中落地所需信息。
`hit/miss` 必须满足预先定义的观测条件；沉默、技术失败、日志缺失和复盘执行错误不是用户负反馈。
正常结束的讨论可以保存失败实验；错误/中断回合当前不可检索，未来恢复线索独立设计。
用户理解不产生任务授权、Evidence 或 CompletionProof。

### 离线基础学习与本地个人适应

离线基础学习研究经验表示、条件化预测、适应和遗忘方法；本地个人适应只更新这个用户的
真实经历所支持的状态。两者均不同于训练外部语言模型，也不同于协议错误分类/修复适配器。
产品运行不加载本地 HF 模型；离线研究可独立选择训练工具，不能成为上线前置。
模型无关的有界文字、明确情境与小规模统计可作起点；向量表示须说明产生方式与请求成本。

训练或评估样本需要有界语义内容、事件顺序、情境、实际行为、反馈和来源；digest 用于
身份及完整性校验，不能代替训练内容。沿用导出授权与脱敏规则，原始对话/回复/state 不入仓。
先有可学习任务与低成本部署证据，再考虑扩大数据规模；不以 TB 数据量作为进展指标。

评估按用户和时间划分，个体内按时间推进；预测/回答只能读取当时已经存在的状态，
后续反馈只用于评分和之后的更新。冻结留出集，不用未来纠正、复盘结果或其他用户的
私有状态填补当时输入。分别报告有反馈覆盖率、hit/miss 与 unjudged，避免只选有利反馈。

先比较无相关记忆、检索相同经历、固定理解、简单统计和学习机制；比较学习算法时固定
语义输入、预算、位置、反馈可用性与输出通道。Hebbian、遗忘和漂移方法可以替换，
不能将多候选生成、更多上下文或另一种提示形式的收益计为关联学习收益。
主要指标是重复纠正、变化后的适应速度、临时处境误固化率及跨会话连续性；
同时报告额外请求、输入/输出 tokens、首字和总延迟、磁盘增长，不以记忆条数代替效果。

已有[现代 Hopfield 独立实验](ghost_progress/hopfield-confirmatory-result-20261118.md)在受测
固定表示与合成经历下未展示超越简单关联主基线的替换收益，保持 `EXPERIMENT_ONLY`。
它支持当前继续工程主线，不能证明 Hebbian 整体最优，也不替代上述真实用户时序评估。

### 运行与迁移

Ghost 不增加独立生成调用、第二遍最终回答或多候选重排；正常主调用理解经历与可靠地
写回新理解分别验收。单次调用候选字段只是可选信号出口，不支持或失效时退回检索与
显式确认，不重试、不阻塞正文。可选 embedding 请求独立核算；维护本身不新增生成。

换生成 provider/model 保留经历、用户身份和合法作用域；状态迁移通过不代表新模型行为收益相同。
更换 embedding 或特征版本需验证兼容性并按授权来源重建，不能混用坐标空间或清空用户经历。
新增派生状态的删除、撤回、过期和重建须随来源传播，维护沿用显式时间。
当前有界窗口不等于无限期保存全部共同经历；长期保留策略与个人状态摘要仍需按容量、
来源有效性和删除语义单独验证，不承诺无限记忆。

## 研究愿景与产品路线的边界

语言偏好投递对照已经验证：在受测模型 modelA 和投递配置下，语言偏好可以改变输出，
送达 19/20、未送达 0/20。它没有验证 Hebbian 学习带来的长期能力提升，
没有证明分层排序整体胜过简单新近排序，也没有证明能脱离 LLM 完成开放任务。

| 轨道 | 当前要回答的问题 | 进入下一步的证据 |
|---|---|---|
| 近期个人适应主线 | 共同经历能否改变下一次相处，明确纠正后能否调整？ | 按 Roadmap 近期一/二完成真实消费者、时序反馈、行为收益与所涉生产边界；不以所有其他版本完成为前置 |
| 0.6 产品工程 | 有效偏好能否经生产准入、正确排位与预算稳定送达？ | 一个实际模型的收益与相关真实发送、当前请求覆盖、冲突/删除/replay等边界通过后，完成正常入口并默认启用受支持的兼容路径 |
| 0.7 泛化与选择性晋级 | 换任务和预算后哪些 family 仍有收益，是否发现具体不兼容？ | 单模型任务与对照矩阵、留出任务、必需数据完整；可选扩模型以扩大效果证据，不作为通用功能开启前置 |
| 0.8 协议适配 | 某个有消费者的小适配器是否减少协议摩擦？ | 独立错误/修复数据、shadow 与 live A/B，不自动扩大到通用学习 |
| 0.9+ 可塑性研究 | 可学习的关系是否优于静态/新近基线？ | 固定语义与输出通道后的长期预测、检索、遗忘、迁移基准 |

每个研究机制都允许失败和终止；产品价值不以实现本文全部阶段为前提。
现有领域事件与投影是算法替换边界，0.6 不需要先造统一的 GhostExperience 总线、
高维网络或 World Model。本文的字段和 API 示例是研究草案，按真实消费者逐步收敛。

language首次于2026-11-17在Gemma普通Chat启用，2026-11-18的[通用入口](ghost_progress/portable-context-production-20261118.md)
已取消模型名、API地址、流式及输出上限白名单；command directive维持SHADOW，intent层只是待验证宿主。
后续统一遵守[生产晋级与默认启用规则](ghost_production_promotion.zh-CN.md)：单模型收益与相关边界通过后，
默认覆盖受支持的兼容路径，实测效果范围单列。实验128/8192等不构成生产精确门槛；具体不兼容或技术依赖才限制。
本次规则修订不重判已有实验或自动晋级尚无收益的新候选。
format/length/answer-first 没有稳定精确服从增益，稳定事实与代码偏好需独立任务正确性评估。
投递位置、其他行内容和长度会干扰收益；研究算法比较必须固定这些变量，不能把 prompt
变化引起的效果算成关系学习收益。Hebbian/Affinity 在产品里只提供复现、关联与解释特征，
不决定事实、权限或最终投递资格。

---

# 1. 最终愿景

长期可以探索这样的结构：

```text
                         Codey
                           │
             ┌─────────────┼─────────────┐
             ↓             ↓             ↓
        Conversation    Research      Code Changes
             │             │             │
             └─────────────┼─────────────┘
                           ↓
                    Experience Stream
                           │
                           ↓
                    Ghost Learning API
                           │
                  ┌────────┴────────┐
                  ↓                 ↓
             World Model        Ghost Memory
                  │                 │
                  └────────┬────────┘
                           ↓
                   Plastic Network
                           │
             ┌─────────────┼─────────────┐
             ↓             ↓             ↓
          Prediction    Association    Context
             │             │             │
             └─────────────┼─────────────┘
                           ↓
                       Retrieval
                           ↓
                       Codey / LLM
                           │
                           ↓
                        Outcome
                           │
                           └──────────────→ Ghost
```

这里最重要的闭环是：

```text
Observe
  ↓
Encode
  ↓
Associate
  ↓
Predict
  ↓
Act
  ↓
Observe outcome
  ↓
Learn
  ↺
```

Ghost 不是一个静态数据库。

它应该逐渐成为一个：

> **可以通过长期经验改变自身内部关系的系统。**

---

# 2. 第一原则：Ghost 不应该直接学习“原始世界”

不要让 Ghost 直接吃：

```text
raw transcript
raw git diff
raw webpage
raw tool output
raw terminal log
```

然后随意修改网络。

应该经过一个稳定的经验接口：

```text
Reality
  ↓
Observation
  ↓
Normalization
  ↓
Experience
  ↓
Learning
```

原因非常重要：

如果 Ghost 直接消费原始数据，那么未来任何 UI、provider、journal 或 transcript 格式变化，都可能污染 Ghost。

所以 Codey 与 Ghost 之间应该存在一个稳定的：

```text
Experience Contract
```

---

# 3. Codey 应该给 Ghost 留什么接口

推荐的核心接口不是：

```python
ghost.learn(text)
```

而是结构化 experience：

```python
GhostExperience(
    id=...,
    timestamp=...,
    kind=...,
    subject=...,
    context=...,
    observation=...,
    action=...,
    outcome=...,
    confidence=...,
    provenance=...,
)
```

可以抽象成：

```text
GhostExperience

├── identity
│   ├── experience_id
│   └── timestamp
│
├── context
│   ├── project
│   ├── task
│   ├── environment
│   └── participants
│
├── observation
│   ├── facts
│   ├── events
│   └── state
│
├── action
│   ├── actor
│   ├── tool
│   └── effect
│
├── outcome
│   ├── success
│   ├── failure
│   └── verification
│
├── learning signals
│   ├── reward
│   ├── surprise
│   ├── novelty
│   └── recurrence
│
└── provenance
    ├── source
    ├── evidence
    └── trace_id
```

这样未来 Ghost 的算法怎么变，Codey 都不用大改。

---

# 4. Experience 类型

Ghost 至少应该能接收以下几类经验。

## 4.1 Conversation Experience

例如：

```text
用户：
把 taskrunner 再简化一点。

Codey：
完成某次修改。

用户：
这个版本比之前好。
```

不要只保存文本。

可以形成：

```text
ConversationExperience

context:
    topic = taskrunner architecture

request:
    simplify taskrunner

action:
    refactor

outcome:
    user_positive_feedback

learning_signal:
    positive
```

长期 Ghost 可能形成：

```text
user preference:
    simplicity ↑
    unnecessary abstraction ↓
```

但这仍然只是：

```text
Ghost hypothesis
```

不是事实。

---

# 5. 研究内容 Experience

例如用户研究：

```text
Hebbian learning
Transformer
Codex
Ghost
World Model
```

Ghost 不需要把整篇文章变成一个“记忆”。

可以抽象成：

```text
ResearchExperience

concept:
    Hebbian learning

relations:
    online_learning
    plasticity
    recurrent_network

source:
    paper / discussion / experiment

claim:
    ...

confidence:
    ...

status:
    hypothesis / observed / established
```

这样未来可以形成：

```text
Hebbian
   │
   ├── plasticity
   ├── temporal learning
   ├── recurrent network
   └── continual learning
```

这就是 Ghost 的语义关系网络开始生长。

---

# 6. Code Change Experience

这是 Codey 最有价值的学习数据之一。

每次代码变更都可以形成：

```text
CodeChangeExperience

project
commit
files_changed
symbols_changed
diff_summary
tests_changed
verification
outcome
```

例如：

```text
taskrunner.py
    ↓
removed abstraction X
    ↓
lines -80
    ↓
tests pass
    ↓
runtime behavior unchanged
```

Ghost 可以逐渐学习：

```text
taskrunner
    ├── frequently touched
    ├── historically fragile
    ├── abstraction-heavy
    └── simplification often successful
```

注意：

> Ghost 不是自动把这些变成事实，而是形成可验证的关联和预测。

---

# 7. Verification Experience

这是非常重要的一层。

例如：

```text
edit
↓
pytest
↓
pass
↓
manual review
↓
later regression
```

最终：

```text
VerificationExperience
```

可以告诉 Ghost：

```text
某种修改
+
某种测试
+
某种上下文
```

以后到底是否真的可靠。

这会让 Ghost 学到：

> **“什么样的绿色测试是真正可信的。”**

而不是只记：

> pytest passed。

---

# 8. Outcome 是学习系统最重要的数据

如果只有：

```text
input → action
```

Ghost 学不到真正的因果结构。

应该尽可能形成：

```text
state
 ↓
action
 ↓
outcome
```

例如：

```text
refactor taskrunner
↓
pytest pass
↓
两周后没有 regression
```

这个经验比：

```text
refactor taskrunner
↓
pytest pass
```

价值高得多。

因此 Codey 应尽量保留：

```text
short-term outcome
long-term outcome
```

---

# 9. Ghost 的核心不是“记忆”，而是关系

传统 memory：

```text
User likes X.
```

Ghost 更应该形成：

```text
X
 │
 ├── often co-occurs with Y
 ├── followed by Z
 ├── succeeds under C
 ├── fails under D
 └── strongly associated with E
```

也就是说：

> **Ghost 的核心数据结构应该允许关系随时间改变。**

这才适合 Hebbian learning。

---

# 10. 高维神经元

不要把一个 neuron 简单理解成：

```text
一个词
```

更适合：

```text
neuron = distributed feature
```

例如一个神经元可能响应：

```text
“用户正在讨论架构简化”
```

另一个：

```text
“用户倾向删除不必要 abstraction”
```

另一个：

```text
“当前代码修改具有高风险”
```

多个神经元共同表示：

```text
context
```

即：

```text
Sparse Distributed Representation
```

---

# 11. Sparse Distributed Representations

不要让每个概念对应一个节点：

```text
redis = neuron 123
```

而应该：

```text
redis
 ↓
[12, 89, 441, 9021, ...]
```

一个概念由一组稀疏神经元表示。

优势：

- 容错
- 泛化
- 组合
- 分布式表示
- 能表达相似但不相同的经验

这非常适合长期 Ghost。

---

# 12. Recurrent Connections

普通关联：

```text
A → B
```

recurrent：

```text
A → B
↑   ↓
└───┘
```

这意味着 Ghost 不只是：

> “看到什么就激活什么。”

而可以形成：

```text
当前状态
 ↓
内部状态
 ↓
下一时刻状态
 ↓
继续预测
```

于是它开始具备：

> **temporal context。**

这对：

- 对话
- 项目演化
- debugging
- research
- 用户习惯

都很重要。

---

# 13. Temporal Hebbian Learning

普通 Hebbian：

```text
A 和 B 同时激活
↓
A-B connection ↑
```

Temporal Hebbian：

```text
A 在 t
B 在 t+1
↓
A → B connection ↑
```

这样 Ghost 可以学习：

```text
event A
通常导致
event B
```

例如：

```text
修改依赖
↓
测试失败
↓
检查环境
↓
发现版本冲突
```

经过大量经验后：

```text
dependency change
        ↓
environment issue
```

关联强度增加。

这已经开始接近：

> **预测系统。**

---

# 14. Eligibility Traces

这是非常值得研究的机制。

问题：

```text
A
↓
几秒后
↓
B
↓
最终 reward
```

如果只看最后时刻，很难知道：

> 到底哪个连接应该强化？

Eligibility trace 可以保留：

```text
“这个连接最近参与过。”
```

然后：

```text
reward
↓
强化最近有资格的连接
```

对 Codey 特别有用。

例如：

```text
某个 refactor
↓
多个 tool calls
↓
tests
↓
最终成功
```

Ghost 可以学习：

> 哪些早期行为与最终成功相关。

---

# 15. Synaptic Decay

Ghost 必须能够遗忘。

否则：

```text
10 年经验
=
无限增长
```

最终会变成：

```text
巨大
混乱
陈旧
```

因此：

```text
weight(t+1)
=
weight(t) × decay
+
learning
```

长期不使用的关系逐渐衰减。

这让 Ghost：

> **越来越关注真正持续存在的规律。**

---

# 16. Inhibition

如果只有 excitation：

```text
A → B ↑
A → C ↑
A → D ↑
A → E ↑
...
```

最后所有东西都互相相关。

需要 inhibition：

```text
A
├── B ↑
├── C ↓
└── D ↓
```

让系统形成竞争。

这可以帮助：

- 去噪
- 稀疏激活
- 防止联想爆炸
- 形成更清晰的 representation

---

# 17. Winner-Take-All

在一个候选集合：

```text
A = 0.8
B = 0.7
C = 0.6
D = 0.2
```

可以让：

```text
A
```

成为 winner。

其余受到 inhibition。

这样 Ghost 不会每次都激活整个网络。

也有利于：

```text
sparse computation
```

---

# 18. Normalization

长期学习最容易出现：

```text
某些连接越来越强
↓
继续得到更多激活
↓
越来越强
↓
network collapse
```

因此需要 normalization。

例如约束：

```text
每个 neuron
总 incoming weight ≈ bounded
```

或者：

```text
activity normalization
```

目标：

> **让网络长期运行而不会数值失控。**

---

# 19. Neuromodulation

这是 Ghost 很值得研究的一层。

普通 Hebbian：

```text
co-activation
↓
learning
```

Neuromodulation：

```text
context
+
surprise
+
reward
+
importance
↓
决定“现在应该不应该学习”
```

例如：

```text
普通聊天
→ low learning rate

用户明确纠正：
“不是这样。”
→ high learning signal

长期成功的代码修改
→ positive reinforcement

重大失败
→ strong update
```

这样 Ghost 不会：

> 每句话都同样认真地学习。

---

# 20. Surprise / Novelty

Ghost 应计算：

```text
surprise
```

例如：

```text
预测：
pytest 应该通过

实际：
pytest failed
```

那么：

```text
prediction error ↑
```

这个事件应该获得更高学习权重。

因此：

```text
learning_strength
≈
novelty × importance × outcome
```

这是非常适合长期学习的机制。

---

# 21. Hierarchical Representations

Ghost 最终不能只有一层：

```text
token → neuron
```

应该逐渐形成：

```text
Level 1
具体事件

Level 2
局部模式

Level 3
任务模式

Level 4
项目模式

Level 5
用户模式

Level 6
抽象概念
```

例如：

```text
删除 try/except
↓
测试变绿
↓
verification weakened
↓
false completion pattern
↓
agent reliability risk
```

低层是具体事件。

高层是抽象模式。

---

# 22. Context-dependent Connections

同一个概念：

```text
“修改测试”
```

在不同 context 下含义不同：

```text
用户要求修改测试
        → 正常

模型偷偷修改测试
        → suspicious
```

因此连接应该受到 context 调制：

```text
weight(A,B | context)
```

而不是固定：

```text
weight(A,B)
```

这会大幅增强 Ghost 的实际可用性。

---

# 23. Ghost 不应该直接生成“事实”

这是整个架构最重要的边界之一。

Ghost 可以说：

```text
prediction:
    这个文件可能容易出问题。

association:
    类似修改过去经常导致 pytest failure。

hypothesis:
    当前 bug 可能与依赖版本有关。
```

但不能直接把它写成：

```text
fact:
    当前项目一定有这个 bug。
```

事实仍然必须来自：

```text
Observation
Evidence
Verification
```

因此：

```text
Ghost
    ↓
Hypothesis / Prior / Prediction
    ↓
Codey
    ↓
Verification
    ↓
Evidence
```

---

# 24. Codey → Ghost 的推荐接口层

建议未来保持类似：

```text
codey/
    ghost/
        api.py
        types.py
        events.py
        encoder.py
        learner.py
        retrieval.py
        state.py
```

Codey 核心只依赖：

```text
Ghost API
```

例如概念上的接口：

```python
ghost.observe(experience)
ghost.predict(context)
ghost.retrieve(context)
ghost.feedback(outcome)
ghost.snapshot()
```

Codey 不应该知道：

```text
Hebbian
STDP
recurrent network
WTA
neuromodulation
```

这些属于 Ghost 内部。

这样未来可以：

```text
Ghost v1
    associative memory

Ghost v2
    Hebbian

Ghost v3
    temporal Hebbian

Ghost v4
    recurrent plastic network
```

而 Codey 不需要重写。

---

# 25. Event Stream 比直接调用 learn 更重要

Codey 最好建立：

```text
Canonical Event Stream
```

例如：

```text
conversation.started
conversation.message
research.observation
tool.called
tool.completed
file.edited
test.started
test.completed
verification.completed
task.completed
task.failed
user.corrected
git.committed
```

Ghost 消费这些事件：

```text
Event Stream
     ↓
Ghost
```

而不是：

```text
Codey 某个模块
↓
直接修改 Ghost 内部结构
```

这样架构更干净。

---

# 26. Event 必须包含 provenance

例如：

```text
event:
    test.completed

result:
    passed

provenance:
    command = python -m pytest
    exit_code = 0
    trace_id = ...
```

Ghost 学到：

```text
真实观察
```

而不是：

```text
某个模型说 pytest passed。
```

这是非常重要的卫生原则。

---

# 27. Ghost 的学习管道

推荐：

```text
Codey Event
      ↓
Event Normalizer
      ↓
Experience Builder
      ↓
Importance / Surprise
      ↓
Encoder
      ↓
Sparse Representation
      ↓
Plastic Network
      ↓
Memory / State
```

然后：

```text
Context
   ↓
Activation
   ↓
Prediction
   ↓
Codey / LLM
```

---

# 28. 不要一开始就做“大脑”

Ghost v1 应该非常小。

例如：

```text
v1
├── event stream
├── structured experience
├── simple associative graph
├── decay
└── retrieval
```

先验证：

```text
长期经验
是否真的提高预测？
```

然后：

```text
v2
temporal Hebbian

v3
sparse representation

v4
recurrent

v5
neuromodulation

v6
hierarchy
```

每一步都做实验。

---

# 29. 每一种机制都应该有可验证的问题

下列问题属于 0.9+ 独立研究基准。开始前先保留无记忆、静态规则、新近排序与当前生产
基线；固定经验编码、候选槽位、渲染、排位和预算，每次只改一种机制。
若候选集合或投递通道也变化，应拆开消融，不能把“能表达中文”算作 Hebbian 改进。
先预先约定停止条件和最低收益，再运行长期 replay 与留出序列；阴性结果可以结束该分支。

不要：

> “Hebbian 很酷，所以加进去。”

而应该：

### 高维神经元

问题：

> 表示维度增加是否提高泛化？

### Recurrent

问题：

> 是否提高多轮任务预测？

### Temporal Hebbian

问题：

> 是否能够学习事件顺序？

### Inhibition

问题：

> 是否降低错误联想？

### Normalization

问题：

> 是否提高长期稳定性？

### Synaptic decay

问题：

> 是否减少陈旧记忆？

### Eligibility trace

问题：

> 是否提高 delayed reward learning？

### Neuromodulation

问题：

> 是否减少无价值学习？

### Hierarchy

问题：

> 是否形成可迁移抽象？

### Sparse representation

问题：

> 是否降低计算并提高容量？

### Winner-take-all

问题：

> 是否提高检索精度？

### Context-dependent connections

问题：

> 是否减少 context collision？

---

# 30. Ghost 的性能目标

真正值得研究的是：

```text
能力 / 计算成本
```

而不是：

```text
网络参数越大越好
```

应该记录：

```text
active neurons
active synapses
learning updates
memory size
retrieval latency
prediction accuracy
energy
```

特别关注：

```text
total network size
        vs
active network size
```

如果：

```text
10B synapses
```

但每次只有：

```text
1M synapses
```

参与计算，

那么 Ghost 才真正开始展现：

> **大网络 + 小局部计算**

的潜力。

---

# 31. GPU 不应该是唯一目标

早期研究可以直接：

```text
CPU
```

因为：

```text
稀疏
局部
在线
```

不一定天然适合 dense GPU。

未来再研究：

```text
GPU
CPU
NPU
neuromorphic hardware
```

不要预先假设硬件。

---

# 32. Ghost 的数据分层

建议至少：

```text
Raw
 ↓
Observation
 ↓
Experience
 ↓
Learned Association
 ↓
Prediction
```

严格禁止：

```text
Prediction
↓
Observation
```

也就是说不能出现：

> “Ghost 以前预测过 X，所以现在 X 就成为事实。”

---

# 33. 用户纠正是极高价值学习信号

例如：

```text
Ghost:
“你可能喜欢方案 A。”

用户：
“不，我更喜欢 B。”
```

这是：

```text
high-information feedback
```

应该产生强 learning signal。

类似：

```text
用户否定
用户确认
任务成功
任务失败
后续 regression
```

都可以成为 neuromodulatory signal。

---

# 34. Codey 应记录长期 outcome

例如：

```text
Day 1:
Code change succeeded.

Day 7:
No regression.

Day 30:
Still stable.

Day 90:
Refactor remains.
```

那么 Ghost 可以学：

```text
pattern X
→ high long-term reliability
```

这比单次测试结果强很多。

---

# 35. Ghost 最终可能形成“经验动力学”

随着时间：

```text
association strength
```

不再是静态数据库字段。

它可能：

```text
形成
增强
竞争
衰减
迁移
组合
重组
```

最终形成：

```text
dynamic cognitive state
```

这才是长期研究的真正目标。

---

# 36. 与 World Model 的关系

建议：

```text
World Model
    =
当前世界状态

Ghost
    =
从历史经验形成的动态先验
```

例如：

```text
World Model:
taskrunner.py 当前有 620 行

Ghost:
过去 taskrunner 每次减少复杂度后维护性通常提高
```

前者需要绑定当前代码观测，是：

```text
observed / estimated（按实际来源区分）
```

后者是：

```text
learned prior
```

用户侧同样区分：Ghost 保存共同经历与当前理解，World Model 检验有情境和时间范围的
交互预测；对用户的理解不限于验证习惯，但反馈判据不能与项目测试混用。
共用契约不等于新建统一权威库，具体参考见[Nezha 迁移清单](ghost_future_direction.zh-CN.md#nezha-mini-参考清单2026-11-18)。

---

# 37. 与 LLM 的关系

不要让 Ghost 一开始就试图替代 LLM。

更合理：

```text
Ghost
 ↓
context / prediction / retrieval
 ↓
LLM
 ↓
language reasoning
```

随着 Ghost 增强：

```text
Ghost
 ↓
越来越多预测
 ↓
越来越少需要 LLM
```

最终实验：

```text
LLM unavailable
↓
Ghost 能做什么？
```

这是可选的长期研究 benchmark，不是唯一科学指标，也不是 0.6 的产品验收条件。
LLM 不可用时，经验检索、确定性解释、删除与重建仍然有价值；不能因此宣称系统已有
开放语言推理。研究关联结构有无增益，要与静态/新近基线单独比较。

---

# 38. 一个长期实验路线

以下 Phase 是机制研究候选，不是产品交付的必经顺序。近期用户连续性与个人适应按
[Roadmap 主线](../ROADMAP.zh-CN.md#用户连续性与个人适应主线2026-11-18)推进，
无需等到 Phase 5 才验证适应，也无需实现每一种神经机制。

本节 Phase 是可独立停止的研究假设，不是顺序开发承诺；不得绕过 Roadmap 的
生产准入和 family 晋级。新机制只有在独立基准胜过简单方案后才申请 shadow，
不能因“实现了下一 Phase”自动进入默认 prompt。

## Phase 0：接口

```text
Codey Event Stream
GhostExperience
provenance
snapshot
```

## Phase 1：Associative Ghost

```text
co-occurrence
decay
retrieval
```

## Phase 2：Temporal Ghost

```text
temporal Hebbian
eligibility traces
sequence prediction
```

## Phase 3：Sparse Ghost

```text
high-dimensional neurons
SDR
WTA
inhibition
```

## Phase 4：Dynamic Ghost

```text
recurrent connections
normalization
synaptic decay
```

## Phase 5：Adaptive Ghost

```text
neuromodulation
surprise
reward
context-dependent learning
```

## Phase 6：Hierarchical Ghost

```text
event
 ↓
pattern
 ↓
concept
 ↓
task
 ↓
project
 ↓
user
```

## Phase 7：Ghost + World Model

```text
current state
+
long-term experience
+
prediction
```

## Phase 8：Language Core experiments

研究：

```text
Ghost 能否形成语言结构？
```

不是假设一定能，而是实验。

---

# 39. 最重要的 benchmark

产品与研究使用不同的评分口径。产品先测每 family 的 on/off 净收益、当前请求覆盖、
模型/任务/排位/预算分层、成本、冲突率及删除复活。一个目标模型的效果与相关边界通过后，
按统一规则正式默认启用受支持的兼容路径；跨模型验证扩展效果证据，不将实验参数变成产品白名单。
稳定事实用任务正确性，命令用建议/选择/审批/执行分别判分，不能混成一个 decisive 总分。

研究机制升级再测下面的长期指标；语言服从率不是 temporal prediction 的替代证据。

### Memory

能否记住长期事实？

### Association

能否发现关系？

### Temporal prediction

能否预测下一事件？

### Adaptation

用户改变偏好后多久适应？

### Forgetting

旧信息能否自然衰减？

### Generalization

能否从 A/B 经验迁移到 C？

### Stability

长期运行是否发散？

### Efficiency

单位计算获得多少学习？

### Integrity

是否把 prediction 错当 fact？

---

# 40. 最终架构

长期可以形成：

```text
                          CODEY
                            │
          ┌─────────────────┼─────────────────┐
          ↓                 ↓                 ↓
    Conversation         Research         Coding
          │                 │                 │
          └─────────────────┼─────────────────┘
                            ↓
                    Canonical Events
                            ↓
                    Experience Builder
                            ↓
                  ┌─────────┴─────────┐
                  ↓                   ↓
             World Model           Ghost
                  │                   │
             current state      plastic state
                  │                   │
                  └─────────┬─────────┘
                            ↓
                       Prediction
                            ↓
                         LLM
                            ↓
                         Action
                            ↓
                       Verification
                            ↓
                         Outcome
                            ↓
                    Event / Feedback
                            ↺
```

---

# 41. 最重要的架构边界

必须保持：

```text
Codey
=
reality interface
```

```text
Ghost
=
learning system
```

```text
World Model
=
state model
```

```text
LLM
=
language / reasoning engine
```

```text
Evidence
=
verified reality
```

不要把它们混成：

```text
巨大 AI 类
```

那会迅速失去可维护性。

---

# 42. Ghost 的长期哲学

不要追求：

> “让 Ghost 看起来像 LLM。”

应该追求：

> **让 Ghost 越来越会从经验中改变自己。**

不要追求：

> “Ghost 记住所有东西。”

应该追求：

> **Ghost 知道什么值得记、什么应该忘、什么只是猜测。**

不要追求：

> “网络越来越大。”

应该追求：

> **网络越来越有结构。**

不要追求：

> “每次都重新计算。”

应该追求：

> **只激活真正相关的部分。**

---

# 43. 长期目标：按证据推进，不按年份晋级

最理想的研究路径不是：

```text
2026 → 巨型 Hebbian LLM
```

而是：

```text
稳定 Experience API
       ↓
Associative Ghost
       ↓
Temporal Learning
       ↓
Sparse / Recurrent
       ↓
Neuromodulation
       ↓
Hierarchical representations
       ↓
Ghost + World Model
       ↓
长期在线学习
       ↓
Language experiments
       ↓
???
```

每个箭头都表示“上一项有收益后，才值得考虑的下一个问题”，不是必然进化链。
研究可跳过、调整或终止；产品不会为维持十年叙事而保留无收益机制。

最后的 `???` 必须保留。

因为真正的研究不是把预先写好的答案实现出来。

---

# 44. 最终原则

如果未来 Codey 要给 Ghost 留一条十年以上的生命线，最重要的不是现在写多少神经元代码。

而是现在把这个接口做干净：

```text
                     Codey
                       │
                       │
                Canonical Events
                       │
                       ↓
                GhostExperience
                       │
                       ↓
                  Ghost API
                       │
                       ↓
              ┌─────────────────┐
              │   Ghost Engine  │
              │                 │
              │  future-proof   │
              │  plastic        │
              │  replaceable    │
              └─────────────────┘
```

未来 Ghost 内部可以从：

```text
graph
```

变成：

```text
Hebbian network
```

再变成：

```text
recurrent plastic network
```

甚至：

```text
未知的新型计算系统
```

但 Codey 不应该因此被迫重写。

**这就是最重要的设计：**

> **Codey 提供干净、带 provenance 的经验接口；Ghost 内部算法可替换，
> 每次替换都需独立验证，生产投递仍由显式 family 策略与 Context Epoch admission 决定。**

这样十年以后，Codey 可能仍然是一个很薄的 runtime，而 Ghost 已经完全不是今天的 Ghost 了。
