# Codey 应该向 Pi 学什么，以及 Codey 最终理想架构

> 面向 Codey 0.4.20 的架构设计备忘录。
> 核心结论：**Pi 教 Codey 如何把执行过程做得更简单；Codey 保留自己在 verification、evidence、completion proof、workspace semantics 上的优势。**

## 1. 核心结论

理想 Codey 不是“功能更多的 Pi”，而是：

**Pi 式 Execution Runtime + Codey 式 Verified Work Layer。**

```text
                         Task
                          |
                          v
                 +-----------------+
                 |  Task Contract  |
                 +--------+--------+
                          |
                          v
                 +-----------------+
                 |    Operation    |
                 +--------+--------+
                          |
             +------------+------------+
             |            |            |
             v            v            v
          Agent        Research    Verification
        Operation      Operation     Operation
             |            |            |
             +------------+------------+
                          |
                          v
                 +-----------------+
                 |    Workspace    |
                 | artifacts/facts |
                 +--------+--------+
                          |
                          v
                 +-----------------+
                 |    Evidence     |
                 +--------+--------+
                          |
                          v
                 +-----------------+
                 | Completion Proof|
                 +--------+--------+
                          |
                    +-----+-----+
                    v           v
                 COMPLETE    NOT DONE
```

---

# 2. Codey 最应该向 Pi 学什么

## 2.1 Operation abstraction：最重要的一项

Codey 当前很多复杂度最终都表现为“正在做一件事情”：

```text
runner
task_flow
handoff
ghost
research
verification
checkpoint
completion
writer
review
policy
```

这些都可以被统一到：

```text
Operation
```

例如：

```text
AgentOperation
ResearchOperation
VerificationOperation
RepairOperation
ReviewOperation
CompactionOperation
```

每个 Operation 都拥有明确生命周期：

```text
created
  -> running
  -> suspended
  -> running
  -> completed / failed / aborted
```

并产生明确的：

```text
OperationOutcome
```

这样可以避免每个 subsystem 自己发明一套生命周期。

---

## 2.2 不要让 TaskFlow 成为“操作系统”

危险的长期演化是：

```text
TaskFlow
 ├── provider
 ├── policy
 ├── agent
 ├── research
 ├── knowledge
 ├── ghost
 ├── handoff
 ├── checkpoint
 ├── verification
 ├── completion
 ├── review
 ├── writer
 └── telemetry
```

理想状态：

```text
Task
  |
  v
Scheduler
  |
  +-- schedule(Operation)
  +-- await(Operation)
  +-- update(TaskState)
```

TaskFlow 只应该是 task submission 到 runtime operation 的 adapter；runtime scheduler 和 operation log 才是生命周期事实源。

---

## 2.3 学习 Pi 的 Lane / Queue 语义

把不同类型的待处理消息明确分开：

```text
Lane
 ├── current
 ├── steer
 ├── follow_up
 └── next
```

语义：

- `steer`：改变当前 Operation 的方向
- `follow_up`：当前 Operation 完成后执行
- `next`：新的独立 Operation

不要把这些都混成一个 `pending`。

---

## 2.4 学习 SuspendedOperation

恢复应该是一等概念，而不是散落在 checkpoint、handoff、ghost、runner 中。

建议：

```text
SuspendedOperation
{
    operation_id
    reason
    state_snapshot
    continuation
}
```

例如：

```text
user_deferred
provider_failure
missing_capability
crash
verification_blocked
resource_unavailable
```

恢复就是：

```text
SuspendedOperation
        |
        v
     resume()
        |
        v
    Operation
```

---

## 2.5 学习 Outcome，而不是大量 boolean

危险：

```text
success = True
failed = False
done = True
verified = False
blocked = False
```

容易产生非法组合。

应该把 runtime 生命周期压缩成：

```text
OperationOutcome
    completed
    failed
    aborted
    suspended
```

而 Task 层另有：

```text
TaskCompletion
    complete
    complete_with_limitations
    incomplete
    blocked
    failed
```

**Runtime outcome 与 Task completion 必须分离。**

---

## 2.6 学习 Single Source of Truth

Codey 有很多有价值的状态：

```text
RunLedger
Trace
Checkpoint
Evidence
Facts
Knowledge
Workspace
CompletionProof
```

但长期风险是多个对象同时声称自己是真相。

必须定义少量 canonical state：

```text
TaskState
OperationState
WorkspaceState
```

其他东西：

```text
Trace
Evidence
Receipt
Telemetry
UI model
Analytics
Knowledge projection
```

尽量成为 derived / append-only / projection，而不是第二套事实数据库。

---

## 2.7 学习 Pi 的 Session / Tree 思想

Session 解决：

> “我从哪一个历史节点重新走一条路径？”

Workspace 解决：

> “项目现在是什么状态？”

二者应该分开。

```text
Session
  └── Entry Tree
       ├── root
       ├── branch A
       ├── branch B
       └── current leaf
```

---

# 3. Codey 不应该向 Pi 学什么

## 3.1 不要削弱 Completion Contract

Codey 最有价值的原则：

```text
Model claim != System fact
```

Agent 说：

```text
done
```

不能直接变成：

```text
Task = complete
```

正确链路：

```text
Claim
  -> Observation
  -> Evidence
  -> Verification
  -> Completion Proof
```

---

## 3.2 Agent completion != Task completion

这是整个系统必须坚持的 invariant。

```text
AgentOutcome.completed
```

只代表：

> Agent loop 正常结束。

不代表：

```text
TaskCompletion.complete
```

真正完成必须经过：

```text
Task
  -> Contract
  -> Required conditions
  -> Evidence
  -> Verification
  -> Proof
  -> Complete
```

---

# 4. Codey 应继续保持 Evidence / Provenance 优势

Verification 不应该只输出：

```text
tests_passed=True
```

而应该能够回答：

> 为什么相信它通过？

例如：

```text
VerificationObservation
{
    status: fresh_pass
    source: local_run
    command: pytest tests/
    observed_at: ...
    workspace_epoch: 42
}
```

Evidence 应尽量包含：

```text
id
operation_id
workspace_epoch
kind
source
observation
timestamp
```

---

# 5. 0.4.20 的 verification epoch 应成为核心 invariant

设：

```text
E = latest relevant edit epoch
V = verification observation epoch
```

只有：

```text
V >= E
```

才能证明 verification 针对当前工作状态。

错误：

```text
edit #1
run #1 -> PASS

edit #2
```

不能继续使用旧 PASS。

正确：

```text
edit #2
run #2 -> PASS
```

才能产生：

```text
fresh_pass
```

这个 invariant 应该同时在 runtime、verification、completion 和 tests 中保护。

---

# 6. 理想 Completion Architecture

```text
                   Task Contract
                        |
                        v
                Completion Engine
                        |
             +----------+----------+
             |          |          |
             v          v          v
          Evidence   Verification  Claims
             |          |          |
             +----------+----------+
                        |
                        v
                 Completion Proof
```

Completion Engine 不执行 Agent。

Agent 也不决定 completion。

Verification 只回答：

> 在某个 workspace state 下观察到了什么？

Completion Proof 才回答：

> 任务是否满足 contract？

---

# 7. 理想 Agent Architecture

Agent 应该非常小：

```text
Agent
 ├── Context
 ├── Model
 ├── Tools
 ├── Message queue
 └── Loop
```

核心循环：

```text
while operation.active:

    request = model(context)

    if tool_call:
        result = execute_tool(tool_call)
        append(result)

    elif assistant_done:
        return AgentOutcome.completed

    elif error:
        return AgentOutcome.failed
```

不要把这些塞进 Agent Loop：

```text
verification policy
completion proof
research orchestration
workspace knowledge
task contract
```

---

# 8. 理想 Operation Architecture

```text
Operation
 ├── id
 ├── kind
 ├── state
 ├── input
 ├── context
 ├── started_at
 ├── outcome
 └── suspension
```

统一生命周期：

```text
created
   |
   v
running
   |
   +--> suspended --> running
   |
   +--> completed
   |
   +--> failed
   |
   +--> aborted
```

---

# 9. 理想 Task Architecture

Task 不是 Agent：

```text
Task
 ├── contract
 ├── state
 ├── operations[]
 ├── artifacts
 ├── evidence[]
 └── completion_proof
```

例如：

```text
Task
 ├── AgentOperation
 ├── VerificationOperation
 ├── RepairOperation
 └── ReviewOperation
```

---

# 10. 理想 Workspace Architecture

Workspace 是事实边界：

```text
Workspace
 ├── filesystem
 ├── git
 ├── project facts
 ├── artifacts
 └── epoch
```

不要让 Workspace 变成一个巨大的 service locator。

---

# 11. Workspace Epoch

所有影响 verification 的 mutation：

```text
edit
create
delete
rename
apply patch
checkout
reset
```

都导致：

```text
epoch += 1
```

Evidence 绑定 epoch：

```text
Evidence.workspace_epoch = 17
```

当前 Workspace 已经是 18 时，这个 evidence 自动被视为 stale。

这是非常强的设计。

---

# 12. 理想 Verification Architecture

```text
VerificationRequest
       |
       v
VerificationRunner
       |
       v
VerificationObservation
```

状态可以是：

```text
fresh_pass
fresh_fail
unavailable
stale
```

并记录：

```text
epoch
command
exit_code
stdout
stderr
duration
```

Verification 不负责决定整个 Task 是否完成。

---

# 13. 理想 Evidence Architecture

Evidence 应尽量 append-only：

```text
Evidence
{
    id
    operation_id
    workspace_epoch
    kind
    source
    observation
    timestamp
}
```

类型可以包括：

```text
ToolEvidence
VerificationEvidence
GitEvidence
TestEvidence
BuildEvidence
ReviewEvidence
```

不要让 Evidence 同时成为 mutable state store。

---

# 14. 理想 Completion Proof

```text
CompletionProof
{
    task_id
    contract
    satisfied_conditions
    unsatisfied_conditions
    evidence_ids
    verification_ids
    status
}
```

状态：

```text
complete
complete_with_limitations
incomplete
blocked
failed
```

必须能够回答：

```text
为什么 complete？
```

如果不能提供有效 evidence，则不允许 complete。

---

# 15. Policy 应该在哪里

Policy 不应该散落在 Agent、Tool、Runner。

推荐：

```text
PolicyEngine
 ├── action policy
 ├── permission policy
 ├── network policy
 ├── filesystem policy
 └── completion policy
```

但 PolicyEngine 只负责：

```text
allow
deny
require_confirmation
```

不负责执行。

---

# 16. Tool Architecture

```text
Agent
  |
  v
ToolCall
  |
  v
Policy
  |
  v
ToolExecutor
  |
  v
ToolResult
  |
  v
Evidence
```

Agent 不需要知道权限系统内部实现。

---

# 17. Research / Knowledge 应降级为 Operation

不要继续：

```python
if research:
    ...
elif ghost:
    ...
elif handoff:
    ...
```

而应该：

```text
ResearchOperation
```

研究结果进入：

```text
Evidence / Facts
```

而不是直接污染 Agent context。

---

# 18. Ghost / Handoff 统一

不要继续增加：

```text
ghost
ghost queue
handoff
continuation
deferred
resume
```

统一为：

```text
Operation
   |
   v
SuspendedOperation
   |
   v
Continuation
```

例如：

```text
handoff
```

只是：

```text
Continuation.target = another operation
```

而不是一个特殊的全局机制。

---

# 19. Ideal Scheduler

最终 Codey 最上层应该很简单：

```text
Scheduler
    |
    +-- start(operation)
    +-- suspend(operation)
    +-- resume(operation)
    +-- cancel(operation)
    +-- await(operation)
```

TaskFlow 最终只是：

```text
Task scheduler adapter + task lifecycle adapter
```

---

# 20. 完整理想架构

```text
+------------------------------------------------+
|                   USER / UI                    |
+------------------------+-----------------------+
                         |
                         v
+------------------------------------------------+
|                     TASK                       |
|          Contract / State / Completion         |
+------------------------+-----------------------+
                         |
                         v
+------------------------------------------------+
|                   SCHEDULER                    |
|       Operation lifecycle / queue / recovery   |
+------------------------+-----------------------+
                         |
               +---------+---------+
               |         |         |
               v         v         v
          +---------+ +--------+ +-------------+
          | Agent   | |Research| |Verification |
          |Operation| |Operation| | Operation  |
          +----+----+ +----+---+ +------+------+
               |           |            |
               +-----------+------------+
                           |
                           v
                  +----------------+
                  |   WORKSPACE    |
                  | files/git/facts|
                  | artifacts/epoch|
                  +-------+--------+
                          |
                          v
                  +----------------+
                  |    EVIDENCE    |
                  +-------+--------+
                          |
                          v
                  +----------------+
                  |  VERIFICATION  |
                  +-------+--------+
                          |
                          v
                  +----------------+
                  | COMPLETION PROOF|
                  +-------+--------+
                          |
                    +-----+-----+
                    v           v
                 COMPLETE    NOT DONE
```

---

# 21. 核心依赖方向

必须尽量保持：

```text
UI
 |
 v
Task
 |
 v
Scheduler
 |
 v
Operation
 |
 +--> Agent
 +--> Research
 +--> Verification
 |
 v
Workspace
 |
 v
Evidence
 |
 v
Completion
```

尤其应该禁止：

```text
Agent -> Completion
```

更合理的是：

```text
Agent -> Outcome
Evidence -> Verification
Verification -> Completion
```

---

# 22. 冷启动项目最应该做的事：删除无用兼容层

Codey 没有几十万用户和十年历史包袱，因此应该大胆：

```text
delete
rename
break internal API
rewrite state format
collapse abstraction
```

不要为了假想的未来保留：

```text
legacy_x
fallback_y
compat_z
```

除非真的存在需要支持的历史数据或真实消费者。

原则：

> **内部 API 宁可干净地破坏，也不要永久背负没有真实消费者的兼容层。**

---

# 23. 如何判断代码是否卫生

每个模块都问：

1. 它有没有唯一职责？
2. 它有没有自己的状态？
3. 这个状态是不是 canonical？
4. 它是否知道了不应该知道的东西？
5. 删除它会不会导致多个 subsystem 同时失效？

如果一个类：

```text
知道太多
保存太多状态
调用太多 service
有大量 boolean
```

就是高风险。

---

# 24. 真正应该控制的是概念数量

不要追求：

```text
代码越少越好
```

应该追求：

```text
核心概念越少越好
state transition 越少越好
implicit behavior 越少越好
```

例如：

```text
Task
Operation
Evidence
Proof
```

四个清晰概念，可能比六个互相隐式依赖的类简单得多。

---

# 25. 最终核心概念建议稳定在这些

```text
Task
Contract
Operation
Workspace
Evidence
Verification
CompletionProof
```

其他：

```text
Agent
Research
Review
Repair
Handoff
Ghost
Compaction
```

都是这些核心概念的 specialization。

---

# 26. 推荐模块布局

```text
codey/
|
+-- task/
|   +-- model.py
|   +-- contract.py
|   +-- state.py
|
+-- runtime/
|   +-- operation.py
|   +-- scheduler.py
|   +-- lane.py
|   +-- outcome.py
|   +-- suspension.py
|
+-- agent/
|   +-- agent.py
|   +-- loop.py
|   +-- context.py
|   +-- tools.py
|
+-- workspace/
|   +-- workspace.py
|   +-- filesystem.py
|   +-- git.py
|   +-- epoch.py
|   +-- facts.py
|
+-- verification/
|   +-- runner.py
|   +-- observation.py
|   +-- rules.py
|
+-- evidence/
|   +-- model.py
|   +-- store.py
|   +-- provenance.py
|
+-- completion/
|   +-- engine.py
|   +-- proof.py
|   +-- policy.py
|
+-- research/
|   +-- operation.py
|
+-- review/
    +-- operation.py
```

这是一张**目标架构图**，不是要求一次性重构。

---

# 27. 推荐重构顺序

## Phase 1：建立 Operation

抽出：

```text
Operation
Outcome
SuspendedOperation
```

先不改变行为。

## Phase 2：Agent 成为 Operation

将：

```text
AgentRunner
```

变成：

```text
AgentOperation
```

## Phase 3：薄化 TaskFlow

逐步让 TaskFlow 只负责 task submission、task lifecycle adapter 和面向 UI 的薄投影；`codey.task` 保持 model-only 边界。

## Phase 4：统一 Workspace Epoch

所有 mutation：

```text
-> epoch++
```

Verification 绑定 epoch。

## Phase 5：Completion Proof 独立

让：

```text
Completion Engine
```

完全脱离 Agent loop。

## Phase 6：Evidence append-only

统一 trace、receipt、verification result 的边界。

## Phase 7：删除旧 compatibility

确认没有真实消费者后删除。

---

# 28. 测试策略

不要只追求测试数量，要测试 invariant。

## Runtime

```text
Operation lifecycle
suspend/resume
abort
queue semantics
concurrent operations
```

## Agent

```text
tool call
tool error
model error
abort
streaming
context overflow
```

## Workspace

```text
epoch increment
mutation tracking
git state
artifact tracking
```

## Verification

```text
fresh pass
fresh fail
stale result
unavailable
environment failure
```

## Completion

```text
model claim cannot complete task
stale verification cannot satisfy contract
fresh verification can satisfy contract
partial completion
limitations
blocked
```

最关键的回归：

```text
edit
-> old verification
-> MUST NOT complete
```

以及：

```text
edit
-> fresh verification
-> MAY complete
```

---

# 29. 如何判断架构真正收敛

不要看“新增了多少功能”。

看新功能接入时需要改多少核心模块。

如果新功能需要：

```text
runner
ghost
handoff
completion
verification
ledger
trace
knowledge
```

一起修改：

> 架构还没有收敛。

如果新功能只需要：

```text
NewOperation
```

或者：

```text
NewEvidence
```

就能接入：

> 架构开始成熟。

---

# 30. Codey 与 Pi 的最终分工

可以把两者浓缩成两个问题：

```text
Pi:
How do I reliably execute an agent?

Codey:
How do I reliably prove that work is complete?
```

理想 Codey：

```text
Pi's execution discipline
            +
Codey's verification discipline
            =
Verified Agent Runtime
```

---

# 31. 最终十条架构原则

1. **Agent completion != Task completion。**
2. **Model claim != System fact。**
3. **Operation 是 runtime 的基本单位。**
4. **Task 是 orchestration 的基本单位。**
5. **Workspace 是事实边界。**
6. **Evidence 是证明材料。**
7. **Verification 负责观察，不负责决定整个 Task。**
8. **Completion Proof 决定任务是否完成。**
9. **Canonical state 必须少，derived state 可以多。**
10. **能删的 abstraction 永远比能加的 abstraction 更有价值。**

---

# 32. 最终理想状态

当 Codey 成熟时，不应该出现：

```text
“这个 feature 要同时修改 runner、ghost、handoff、
completion、verification、ledger、trace……”
```

而应该是：

```text
我要增加一种 Operation。
```

或者：

```text
我要增加一种 Evidence。
```

或者：

```text
我要增加一种 Completion condition。
```

如果一个新功能需要修改八个核心 subsystem：

> 架构仍然没有收敛。

---

# 33. 最终目标不是“更强”，而是“更少”

Codey 最危险的方向：

```text
more memory
more research
more agents
more policies
more evidence
more fallback
more automation
more modes
```

真正应该追求：

```text
fewer concepts
fewer states
fewer sources of truth
fewer special cases
fewer implicit transitions
fewer fallbacks
```

同时：

```text
stronger invariants
stronger evidence
stronger verification
stronger recovery
```

---

# 34. 最终判断

Codey 不应该成为：

> “一个比 Pi 功能更多的 Pi。”

它真正有机会成为：

> **一个把 Agent 工作变成可验证、可恢复、可审计状态机的 Verified Work Runtime。**

Pi 最值得 Codey 学的是：

**执行模型的克制。**

Codey 最应该坚持的是：

**完成语义的严谨。**

最终理想架构不是：

```text
Pi + Codey features
```

而是：

```text
Pi
-> 简洁的 Agent Runtime

Codey
-> 严格的 Verified Work Runtime
```

最终目标：

**更少的核心概念 + 更强的 invariant + 更薄的 orchestration + 更可信的 completion。**
