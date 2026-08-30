# Codey 从 Codex 借鉴什么、避免什么

> 核心判断：**Codey 不应该复制 Codex，而应该吸收 Codex 已经验证有效的工程思想，同时避免在快速功能扩张中形成过重的 runtime、provider 特化和状态复杂度。**

## 1. 总原则

LLM 是不确定的，但 Agent 面对的工具、文件、进程和真实用户副作用必须尽可能确定。

因此 Codey 应追求：

- 更清晰的状态
- 更明确的 effect boundary
- 更可靠的 verification
- 更诚实的 completion
- 更少的隐藏行为

而不是单纯堆 prompt、provider 特例、fallback 和 agent mode。

## 2. 最值得从 Codex 借鉴的东西

### 2.1 Effect / Operation 边界

不要把：

```text
LLM response = action completed
```

而应区分：

```text
intent → operation → effect → settlement
```

真实副作用应有明确生命周期；不要因为模型说 done 就认为 effect 已完成。

原则：

> 只有存在真实副作用、重放风险或恢复需求的操作，才值得拥有明确 operation state。

### 2.2 Runtime Operation State / Durability

重点不是增加很多状态，而是让系统在 crash、disconnect、retry 后知道自己到底发生了什么。

至少测试：

- provider send 前后崩溃
- edit 前后崩溃
- verification 后、receipt 前崩溃
- 网络/UI/process 中断

要求：

```text
不会把未完成说成完成
不会把已完成重复执行
不会把不确定状态伪装成确定状态
```

### 2.3 Replay / Recovery Policy

建立最小化 ReplayPolicy：

- 纯查询等无副作用操作：可重试
- 可证明幂等的操作：条件重试
- 无法确认 settlement 的非幂等 effect：默认不可盲重试

核心原则：

> **Unknown ≠ Failed。**

### 2.4 Verification 独立于模型

正确结构：

```text
LLM claim
  ↓
Codey verification
  ↓
actual result
  ↓
CompletionProof
```

必须区分：

```text
model confidence ≠ system verification
```

### 2.5 Verified Completion v2

至少区分：

```text
task executed
task verified
task complete
```

并记录 verification quality / integrity status。

不要让：

```text
pytest = green
```

自动变成：

```text
task = trustworthy
```

## 3. Codey 可以继续领先的方向：Edit Integrity

真实风险是：

```text
模型修改测试/fixture
↓
pytest green
↓
LLM: done
```

应观察：

```text
test_fixture_modified
assertion_removed
import_guarded
verification_config_changed
production_unchanged_tests_passed
user_authorized_test_edit
```

正确判断不是“测试一改就禁止”，而是：

```text
test changed?
+ user authorized?
+ semantics weakened?
+ production behavior independently verified?
```

先 Monitor + Receipt Warning，后续由 A/B 决定是否 enforcement。

## 4. Tool Protocol Friction

可以吸收 Codex 对工具协议严谨性的经验，建立很薄的：

```text
canonical ToolCall
```

必要时做：

```text
args normalization / repair
```

但：

> **修复协议错误 ≠ 修复模型意图。**

可以修结构、类型和明确缺省值；不要偷偷替模型决定危险 effect 或真实意图。

先 shadow / A-B，只有证明：

```text
protocol errors ↓
repair turns ↓
unsafe actions 不增加
false completion 不增加
```

才进入默认路径。

## 5. Provider Abstraction：统一接口，不统一行为

Codey 可以测试：

```text
Qwen / DeepSeek / MiMo / Claude / GPT / ...
```

但不要形成：

```text
if qwen: ...
elif deepseek: ...
elif mimo: ...
```

Provider-specific behavior 必须经过真实 A/B 证明，否则不要进入核心 runtime。

Codey 应寻找的是：

> **跨模型成立的最大公约数。**

## 6. 不要复制 Codex 的快速功能扩张

Codey 的目标不是“功能最多”，而是：

```text
慢
稳定
可验证
可删
```

每个新功能都应回答：

- 解决什么真实问题？
- 能否用 deterministic test 证明？
- 增加多少长期维护成本？
- 有没有更简单的实现？
- 将来能否删除？

## 7. 避免 fallback 地狱

警惕：

```text
A → B → C → compatibility → legacy
```

Fallback 必须：

- 有明确触发条件
- 可测试
- 可观测
- 有删除条件

长期没有触发的 fallback 应考虑删除。

## 8. 避免冷启动阶段的过度兼容

不要为了：

```text
未来可能支持
旧版本可能存在
某 provider 可能返回
某平台可能需要
```

提前建立大量 abstraction。

更健康：

```text
真实需求
↓
最小实现
↓
真实失败
↓
再抽象
```

## 9. Architecture：稳定内核 + 可替换外围

理想结构：

```text
                    Codey Core
                        │
        ┌───────────────┼────────────────┐
        ↓               ↓                ↓
   Verification      Evidence        Runtime
        │               │                │
        └───────────────┼────────────────┘
                        ↓
                 Canonical Events
                        ↓
        ┌───────────────┼────────────────┐
        ↓               ↓                ↓
     Provider          Ghost          World Model
```

外围可以实验，核心必须稳定。

## 10. Ghost：学习，但不能污染 Evidence

Ghost 可以学习：

```text
用户习惯
项目模式
历史行为
关联关系
预测
```

但必须严格：

```text
Ghost prediction ≠ Evidence
```

Ghost 可以影响：

```text
hint / prior / suggestion / context
```

不能直接决定：

```text
permission / completion / evidence / fact
```

## 11. World Model：事实状态与预测分离

World Model 可以维护：

```text
当前项目状态
文件关系
运行状态
依赖状态
环境状态
任务状态
```

同时可以产生 prediction，但必须区分：

```text
observed state
predicted state
```

不能把 prediction 写成 fact。

## 12. Ghost + World Model 的长期方向

长期可以形成：

```text
World Model
“现在是什么？”

Ghost
“过去通常怎样？”

LLM
“基于这些，我应该怎么推理？”

Verification
“现实真的如此吗？”

Outcome
“结果是什么？”

        ↓

Ghost / World Model 更新
```

这可能成为 Codey 区别于普通 Agent 的长期核心，但不应在早期过度实现。

## 13. 性能

优先观察 Codey 自己制造的 runtime friction，而不是把 provider 慢误判成 Codey 慢：

```text
provider wait
tool latency
verification latency
unnecessary subprocess
serialization
filesystem scanning
journal writes
duplicate work
```

特别区分：

```text
LLM wait time
```

和：

```text
Codey overhead
```

## 14. Receipt / Trace 应成为事实账本

系统应能回答：

```text
发生了什么？
谁做的？
什么时候做的？
修改了什么？
验证了什么？
验证是否被污染？
哪些状态确定？
哪些状态不确定？
```

目标不是日志越来越多，而是：

> **每一个关键结论都有可追溯依据。**

## 15. A/B 是 Codey 的重要武器

利用网页模型做：

```text
同一个任务
+
不同 provider
+
不同 arm
+
相同 scorer
```

不要只看成功率，还应看：

```text
false completion
verification integrity
unsupported claim
protocol errors
repair turns
tool count
latency
provider wait
```

寻找：

> **跨模型仍然成立的工程规律。**

## 16. 功能进入标准

建议：

```text
Idea
 ↓
deterministic fixture
 ↓
baseline
 ↓
minimal implementation
 ↓
local replay
 ↓
single-provider A/B
 ↓
multi-provider A/B
 ↓
production observation
 ↓
default
```

如果不能证明：

```text
benefit > complexity
```

就不要合并。

## 17. Graduation-or-delete

任何 shadow / monitor / experimental capability 都必须有：

```text
graduation-or-delete gate
```

例如：

```text
0.5.0
Edit Integrity Monitor

0.5.x
真实 production observation

0.6
证明有效 → enforcement
没有证明 → 删除或降级为 manual harness
```

避免：

```text
实验代码
↓
没人敢删
↓
永久存在
↓
架构腐化
```

## 18. Codey 不应该从 Codex 学什么

不应该机械复制：

1. 大量 provider-specific workaround
2. 为未来需求提前抽象
3. 无限 fallback
4. 把模型行为写死进 runtime
5. 为功能而功能
6. 复杂状态机蔓延
7. 把成功率当唯一指标

尤其要避免：

```text
模型越来越聪明
↓
harness 越来越薄
```

更合理的是：

```text
模型越强
↓
可执行 action 越复杂
↓
真实副作用越多
↓
runtime 的确定性要求越高
```

## 19. Codey 最值得从 Codex 学习的最终原则

可以浓缩成：

```text
LLM action ≠ effect
effect ≠ settlement
settlement ≠ verification
verification ≠ integrity
integrity ≠ user-visible completion
```

因此：

```text
Model
 ↓
Intent
 ↓
Controlled Operation
 ↓
Effect
 ↓
Settlement
 ↓
Verification
 ↓
Integrity
 ↓
CompletionProof
 ↓
Receipt
```

每一层都尽可能：

```text
小
明确
可测试
可观测
可删除
```

## 20. 长期路线

不要：

```text
Codey
 ↓
复制 Codex
```

而应该：

```text
Codex
 ├── runtime durability
 ├── effect boundary
 ├── recovery / replay
 └── 工程成熟度

        ↓

Codey
 ├── Evidence
 ├── Verification
 ├── Edit Integrity
 ├── Provider-agnostic evaluation
 ├── Ghost
 └── World Model
```

最终目标不是：

> “成为另一个 Codex。”

而是：

> **成为一个模型无关、长期可靠、能够验证自己的 AI 工作环境。**

## 21. 一句话原则

> **不要因为模型聪明，就减少确定性边界。**

> **不要因为功能有趣，就增加长期复杂度。**

> **不要因为某个 provider 表现好，就围绕它设计核心。**

> **不要把测试绿色当作真实完成。**

> **不要把 prediction 当作 evidence。**

> **不要让 fallback 比主路径更复杂。**

> **不要保留没有证明价值的实验代码。**

> **让 Codey 越来越强，但越来越薄。**

最终希望：

```text
         任意足够好的 LLM
                 ↓
              Codey
                 ↓
       ┌─────────┼─────────┐
       ↓         ↓         ↓
 Verification  Evidence   Runtime
       │         │         │
       └─────────┼─────────┘
                 ↓
            Ghost / WM
                 ↓
          Long-term learning
```

**模型负责“聪明”。**

**Codey 负责“可靠”。**

**Ghost 负责“长期经验”。**

**World Model 负责“现在”。**

**Evidence / Verification 负责“现实”。**
