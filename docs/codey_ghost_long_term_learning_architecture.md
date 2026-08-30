# Codey / Ghost 长期学习架构：从经验到可塑性认知网络

> 目标：设计一个可以用多年时间持续研究、逐步增强的 Ghost。
>
> 核心思想不是“现在就做一个 Hebbian LLM”，而是先让 Codey 给 Ghost 留下稳定、干净、可演化的学习接口，使未来可以逐步实验高维神经元、recurrent connections、temporal Hebbian learning、inhibition、normalization、synaptic decay、eligibility traces、neuromodulation、hierarchical representations、sparse distributed representations、winner-take-all 和 context-dependent connections。
>
> **Codey 负责可靠地观察现实；Ghost 负责从经验中形成可塑的内部结构。**

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

前者是：

```text
observed
```

后者是：

```text
learned prior
```

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

这才是有科学意义的 benchmark。

---

# 38. 一个长期实验路线

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

每次 Ghost 升级都应该测试：

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

# 43. 十年目标

最理想的研究路径不是：

```text
2026 → 巨型 Hebbian LLM
```

而是：

```text
2026
稳定 Experience API
       ↓
2027
Associative Ghost
       ↓
2028
Temporal Learning
       ↓
2029
Sparse / Recurrent
       ↓
2030
Neuromodulation
       ↓
2031
Hierarchical representations
       ↓
2032
Ghost + World Model
       ↓
2033
长期在线学习
       ↓
2034
Language experiments
       ↓
2035+
???
```

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

> **Codey 给 Ghost 提供持续、干净、带 provenance 的经验流；Ghost 自己决定如何学习。**

这样十年以后，Codey 可能仍然是一个很薄的 runtime，而 Ghost 已经完全不是今天的 Ghost 了。
