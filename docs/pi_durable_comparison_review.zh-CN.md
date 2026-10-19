# Codey 与 Pi durable 静态对照审查

> 审查方式：分段记录；当前阶段只做源码/spec 静态对照，不运行全量 pytest，不修改生产代码。
>
> Pi 参考：`reference-projects/pi/packages/durable/docs/spec.md` 与 `src/`。

## 0. 审查进度

- 当前段：总体基线与第一批证据。
- 下一段：Session mutation line、失败封存、close/reopen、跨进程 ownership。
- 后续顺序：scheduler/input、context/fork、artifact、旧路径清理、AppContext 边界、类型检查入口。
- 判定标准：只有 durable 事实拥有者、恢复路径和故障注入证据都收敛后，才讨论“全面超过 Pi”。

## 1. 当前已经有根据胜过 Pi 的部分

### 1.1 外部 effect 的安全恢复

Codey 已把 effect intent/settlement 接入 durable session，且恢复决策区分安全可重试 effect 与外部结果未知 effect。相关实现集中在：

- `codey/durable/session.py`
- `codey/durable/recovery.py`
- `codey/durable/reducer.py`

当前语义已经能表达 provider 响应未知、shell 写操作未知、取消屏障、attempt token 和 worker fencing。相对于 Pi 的通用 scheduler，Codey 在“未知外部副作用不得自动重跑”这一安全边界上更强。

### 1.2 Codey 特有的安全和验证边界

已有 durable 或稳定事实边界：

- 权限 snapshot 与审批生命周期；
- shell approval claim 与 effect intent 的原子提交；
- completion proof；
- verification/review；
- provider uncertainty；
- Research evidence ledger；
- artifact digest、scope 与 tamper 校验。

这些能力不应为了追求 Pi 的通用性而被删掉。后续工作应把它们接入同一 mutation line，而不是另起事实源。

### 1.3 Durable 基础已经成形

`DurableSession` 已有：

- session 内 mutation line；
- conversation revision CAS；
- task revision 与 attempt token；
- 全局 `commit_seq`；
- Memory/SQLite store contract；
- `request_id` 加 fingerprint 幂等；
- provider snapshot 冻结。

SQLite 初始化也已经采用 WAL、`synchronous=FULL`、冷启动 schema 校验和显式事务。这里的基础方向正确。

### 1.4 Ghost 分库是正确边界

Ghost 已独立使用 `ghost.sqlite3`，后台记忆/图计算不与主 durable task 数据争抢同一写锁。Ghost 内部 event、cursor、graph、memory 事实应继续保持独立；不能把它们为了“统一数据库”塞回 `durable.sqlite3`。

### 1.5 UI/SSE 主方向正确

UI transcript 主要从 durable projection 读取，`commit_seq`、database identity 和 replay cursor 已存在。`save_ui_state()` 不应恢复 transcript。SSE 也已经具备从 durable commit 序号重放的方向。

## 2. 当前不能宣布全面超过 Pi 的硬差距

### 2.1 Session 生命周期语义仍弱于 Pi

证据：`codey/durable/session.py:77-105, 225-233, 1237-1245`。

- `_commit()` 对 storage 异常会设置 `_sealed`，但 `_publish_commit()` 吞掉 listener 异常并只记录日志。
- listener 同时承担外部 projection 和 scheduler 唤醒，缺少 Pi 的同步 immutable publication 与失败/close 通知分层。
- `close()` 主要关闭 store；没有 Pi 那种失败原因、close listeners、后续操作统一拒绝和 reopen 语义。
- 多数读 API 没有统一的失败/关闭检查，不能直接对齐 Pi 的 `SessionFailed`/closed contract。

这会影响恢复证明：事务已经提交但 publication/scheduler adoption 失败时，当前 session 可能继续运行，调用方对“内存状态是否可继续使用”的判断不统一。

### 2.2 Durable canonical mutation 仍有旁路

证据：`codey/durable/store.py`、`codey/durable/sqlite.py:145-163, 259-329`、`codey/durable/conversation_ops.py:167-203`。

- `create_conversation()` 直接写 store，且没有明确 commit record/publication。
- `branch()` 虽然有 SQLite 事务，却没有 expected revision CAS；两个 writer 可以覆盖 head。
- `fork()`/`delete_conversation()` 是 store 专用事务，再由外层合成 `DurableMutation` publication。
- 同一 `DurableSession` 的主 mutation line 因此仍存在几个特殊旁路。

目标应是把 create/branch/fork/delete 纳入明确 mutation 类型，规定它们的 commit_seq 和 publication 语义，并给 branch 加 expected revision。

### 2.3 多 Session ownership fencing 不完整

证据：`codey/durable/session.py:77-80, 911-964`。

`_owner_id` 和 attempt token 能防止旧 worker 提交，但 `recover_scheduler_state()` 会把所有 durable `RUNNING` task 当作本进程启动前的遗留状态回收。若两个进程同时打开同一个 durable DB，第二个 Session 可能回收第一个仍在运行的 task，造成重复 effect。

需要明确选择并实现一种强语义：

1. state-home 级单实例 lease/open fencing；或
2. 明确拒绝第二个 Session 打开同一 durable DB。

仅靠 attempt token 不足以证明 scheduler ownership。

### 2.4 Scheduler 仍是轻量扫描器，不是 Pi 完整 scheduler kernel

证据：`codey/durable/scheduler.py:1-220`，以及 `codey/durable/session.py:766-1138`。

Codey 已有 pending/running/waiting/completing、owner_task_id、join policy、completion hold、workflow digest、restart recovery 和 attempt token；但仍以 `store.tasks()` 全量扫描来推导：

- owner/downward graph；
- waiting reverse index；
- cancellation candidates；
- finalization candidates；
- workflow reconcile。

与 Pi 的 `harness/scheduler.ts` 相比，缺少正式 `TaskInvocation` 生命周期、abort signal/bottom-up abort handler、background boundary、blocked task inspection、task graph projection、live index 和 migration failure 的持久化/可观察语义。

## 3. 第一段结论

当前 Codey 已在安全 effect 恢复、权限/审批、provider uncertainty、completion proof、Research evidence、Ghost 分库和 artifact 校验上有实质优势；但 durable 内核仍有四个先决缺口：

1. Session failure/sealing/close contract；
2. canonical mutation line 无旁路；
3. 同一 durable DB 的多 Session ownership fencing；
4. scheduler 的 invocation/index/abort/background 语义。

在这四项没有红测和实现前，不能从“功能更强”推导出“工程整体超过 Pi”。

## 4. 第二段：Session mutation line 与生命周期

### 4.1 Codey 已经做对的部分

`DurableSession._mutation_line` 用 `RLock` 把读、派生、提交包在同一条线程内；这比只依赖 SQLite 单事务的实现更接近 Pi 的“读写派生不可被另一 writer 插入”的语义。`transact_task()` 也强制 reducer 使用同一份 task/effect/head 读取，并校验 task revision 与 conversation revision。

`SQLiteDurableStore.commit()` 使用 `BEGIN IMMEDIATE`，在一次事务中处理 submission 幂等、entry、task、effect、commit record 和 head 更新；外部 provider/shell effect 没有放进事务，这一点符合 Pi spec 的外部 effect 边界。

### 4.2 与 Pi 的关键差距

Pi 的 Session 把以下动作放在同一条异步 mutation line 上：

- admission；
- transaction callback；
- storage settlement；
- committed-state adoption；
- internal observer；
- host observer enqueue；
- close/failure seal。

Pi 的 `session.ts:201-245, 483-569, 863-871` 明确区分：

- `observeCommits()`：Session 自己的 scheduler/adoption，抛错会 fail Session；
- `subscribeCommits()`：外部 projection，抛错只报告；
- `subscribeClose()`：close/failure 立即通知；
- `SessionFailed`：失败后所有后续操作统一拒绝。

Codey 当前 `session.py:225-233` 只有一个 `on_commit`，并且任何 listener 异常都被吞掉。由于 AppContext 的 `_publish_durable_commit()` 同时做 SSE emit 和 scheduler.notify，这两个不同可靠性等级的动作没有分层。scheduler 的内部状态落后 durable commit 时，Session 不会进入失败态，下一次恢复只能依赖扫描重新推导。

另外，`DurableSession.close()` (`session.py:1237-1245`) 只调用 store.close 并设置 `_closed`。SQLite store 的 `close()` (`sqlite.py:485-486`) 是 no-op，因为每次操作临时开连接；因此当前 close 更像“禁止部分写操作的标志”，不是 Pi 那种“封存 admission、停止内部观察、等待已接纳工作、再关闭 storage”的生命周期边界。

多数读取方法，例如 `head()`、`entries()`、`task()`、`context()` 和 `commits_after()`，也没有先调用 `_ensure_writable()`。如果产品语义要求 close 后只允许有限的已完成 submission 查询，就需要显式规定白名单；目前没有统一 contract。

### 4.3 旁路写入的具体问题

`create_conversation()`、`branch()`、`fork()`、`delete_conversation()` 直接经过 `DurableStore` 专用方法，而不是一个完整 `DurableMutation`：

- `create_conversation()` 没有 commit sequence，也没有 publication；
- `branch()` 在 store 内读取 revision 后直接加一，没有 caller supplied expected revision；
- `fork()` 的 conversation 创建和 fork commit 虽在一个 SQLite 事务，但 Session 只能事后合成 `DurableMutation(child_id, 0)` 通知；
- `delete_conversation()` 没有 commit record，SSE cursor 无法知道历史 scope 已删除。

这里不应通过再加一个通用 repository 抽象来解决。应增加少量明确 mutation 类型，或把 conversation lifecycle 作为 `DurableMutation` 的显式字段，并让 store 只实现这一条入口。

### 4.4 第二段应先写的红测

1. storage commit 抛错后：下一次写、读、scheduler dispatch、SSE publish 的预期错误类型统一；Session 不再继续当作健康状态运行。
2. internal scheduler publication 抛错后：Session 进入 failed/sealed；外部 audit/SSE listener 抛错不会改变已提交事实。
3. `close()` 开始后：新的 conversation/task/submission 操作拒绝；已完成 submission 的只读查询行为有明确测试。
4. close 等待已 claim worker；worker 在 close 期间 settlement 的行为与 publication 语义固定。
5. 两个 writer 同时 branch 同一 conversation：一个成功，一个 CAS conflict，不能静默覆盖 head。
6. create/fork/delete 的 commit sequence 能被 reconnect cursor 观察，且失败事务不会产生幽灵 publication。

### 4.5 第二段结论

Codey 的线程级 mutation line 和 SQLite 原子 commit 已经是良好内核，但还没有达到 Pi 的“Session failure 是一等状态、内部 adoption 失败必封存、close 是完整生命周期协议”的强语义。下一步优先级应是生命周期和 mutation 入口收敛，而不是继续增加新的状态字段。

## 5. 第三段：Scheduler 与 input/submission

### 5.1 Codey 已经具备的调度语义

Codey 的 task checkpoint 已经把逻辑 phase 与 scheduler status 分开，并覆盖 `pending`、`running`、`waiting`、`completing`、terminal 状态。当前还有：

- owner task 与等待依赖；
- `failFast`/`allSettled`；
- completion hold/finalization；
- workflow version/replay digest；
- restart recovery；
- attempt number、attempt token、attempt owner；
- cancellation barrier；
- safe effect 与 unsafe effect 的恢复区分。

这些已经超过一个只有简单 pending queue 的执行器，也保留了 Codey 对权限、审批和未知副作用的安全约束。

### 5.2 Scheduler 与 Pi 的硬差距

`codey/durable/scheduler.py:1-220` 的对象主要拥有 `_pending`、`_inflight`、`_scheduled` 三个内存集合，并从 `session.pending_tasks()` 重新填充候选。`session.py:766-1138` 的等待、owner descendant、finalization 和 workflow reconcile 也主要通过 `store.tasks()` 全量扫描和临时字典推导。

Pi `harness/scheduler.ts` 则把以下东西作为 scheduler kernel 的稳定内存索引：

- task invocation（一次具体 run 的 signal、done、mode）；
- owner/downward graph；
- waiting reverse index；
- cancellation intent set；
- runnable set；
- finalization check set；
- failFast check set；
- blocked migration map；
- idle waiters 和 task waiters；
- background boundary。

这不是单纯性能优化，而是语义差异。Pi 可以证明“一个提交变化只重新检查受影响的等待者/owner/finalization 候选”；Codey 每次依赖全表扫描，状态变化的触发关系隐藏在多个 `reconcile_*()` 函数中，难以证明不会遗漏或重复处理。

Codey 还没有正式的 `TaskInvocation` 对象：dispatch 的 worker 主要由 `DurableTaskScheduler` 调用 host callback，attempt token 负责提交 fencing，但 signal、join、abort handler、close 时的 invocation 状态没有 durable/volatile 之间的清晰边界。Pi 的 abort 会先 durable mark，再 signal invocation，最后启动 abort invocation 并等待普通 owned work drain；Codey 当前更多是把 `cancel_requested` 写进 task state 后由下次扫描处理。

Pi 对缺少或无法迁移的 task definition 保持 `blocked`，并在 registry 变化时重新考虑；Codey 的 workflow readiness 主要返回 false 后从本轮候选移除，blocked reason 的持久化、观察和 registry-triggered retry 仍不完整。

### 5.3 Codey input/submission 的当前语义

`DurableConversationInputQueue` (`codey/app/input_queue.py`) 已经不再使用旧 queue 作为 AppContext 主路径，输入 admission 会调用 `durable.admit_input()`，因此 request identity 和 provider snapshot 是 durable 的，这是明显进步。

但它仍把 Pi 的 inbox 简化成 task projection：

- admission 时同时写 user entry 和 task；
- queued input 已经进入 conversation history；
- 没有独立的 queued/placed 边界；
- 没有 `steer`/`followUp`/`reject` 的正式队列策略；
- `DurableConversationInputQueue._items()` 通过扫描 task state 反推输入队列；
- 老的 `ConversationInputQueue` 仍存在并使用独立 SQLite 文件。

Pi 的 `harness/inbox.ts` 把 inbox 作为 conversation-scoped durable document：queued submission 先留在 inbox，边界根据 steering/follow-up mode 决定何时、以什么 head placement 放入 transcript；abort 只撤回 queued user input，queued passive writes 可以保留。这个边界使“输入已收到”与“输入已进入模型上下文”成为两个可恢复事实。

Codey 当前的 `before_entry_id` 和 `resume_entry_id` 更像在已经写入 history 后做 context 截断补偿。它能避免部分重复，但无法完整表达 Pi 的 placement 语义，尤其是同一 active run 中 steer、follow-up、passive write 的顺序和取消规则。

### 5.4 第三段应先写的红测

1. active task 运行时重复提交同一 request：只返回原 submission，不新增 entry/task/provider effect。
2. queued input 在 provider turn 边界前不出现在 active context；placed 后才产生 user entry。
3. steer/follow-up 的 first/all 策略和顺序稳定，重启后仍按同一队列顺序 placement。
4. conversation abort 只撤回 queued user input；queued passive write 的处理符合明确策略。
5. 两个 waiting task 同时等待一个 child：child settlement 只触发受影响 waiter 的 reconcile，不能重复 finalization。
6. task abort 先 durable mark，再 signal invocation；close/restart 后不能重新开始普通 work。
7. definition 缺失/版本过旧/migration 失败的 task 保持 blocked，可观察，并在 registry 更新后重新进入候选。
8. 两个 scheduler/session 同时看到同一 pending task：只有一个取得 durable attempt ownership，另一个不能 dispatch。

### 5.5 第三段结论

Codey 已经有可靠的 task checkpoint 和 effect fencing，但 scheduler 仍是“durable 状态 + 全量扫描 + host callback”的轻量协调器，input 仍是“submission/task projection”，不是 Pi 那种独立 inbox/placement 和 invocation kernel。要超过 Pi，下一步应先收敛 scheduler ownership、abort/invocation、blocked task 和 inbox placement 语义，再考虑性能索引。

## 6. 第四段：Context、provider replay 与 fork cutoff

### 6.1 Codey 已经胜出的部分

`codey/durable/context.py` 已经处理了许多容易遗漏的恢复细节：

- 从 conversation ancestry 读取路径；
- compaction entry；
- context edit 的 omit/replace；
- tool call/result 配对；
- 缺少结果时合成 error tool result；
- 排除 aborted/error/interrupted assistant；
- provider token counter；
- 多字节文本的估算入口；
- `ContextSnapshot.entry_ids/token_count/digest/omitted_entry_ids`。

`codey/operations/conversation_plan.py:126-165` 也已经把 durable snapshot 接入 provider plan：有 provider 原生 durable context loader 时传入 `ContextSnapshot`，否则生成带 digest 的 handoff；provider selection、model、protocol、参数和 tokenizer 由 task snapshot 冻结。这些是 Codey 相对于简单 transcript replay 的真实优势。

### 6.2 与 Pi context protocol 的差距

Codey 的 `build_context()` 仍然把输入视为一个已经排序好的 `EntryDraft` 序列：找到最近 compaction 后截取、应用 edits、从尾部按 token budget 选消息，再恢复 tool pairs。它没有 Pi `harness/context.ts` 的两个阶段：

1. 在 Session line 上捕获不可变的 `ContextBounds`（head marker + tail）；
2. 在 bounds 内按 range 派生 active entries、system baseline、section/tool delta、tool result ordering。

具体缺口：

- 没有一等 `head` marker 与 cutoff range；
- `before_entry_id` 是在完整 path 上做前缀截断，不能表达 compaction/reset 之后的 active range；
- system prompt 不是 durable positional section/state replay；
- tool schema 的增删和顺序变化没有作为上下文事实编码；
- provider final message format 只是 `_message_for()` 的统一简化格式，不能保证每个 provider 的 system/tool protocol 语义都能从历史精确重建；
- context cache/range 没有 Pi 那种同一 head marker 下增量扩展和历史 tail 截断语义。

这意味着 Codey 当前可以重建“多数对话消息”，但还不能证明“同一 provider 请求在 compaction、model/tool 变化、fork、重启后得到完全相同的 protocol window”。尤其是 system/tool schema 的位置变化可能破坏 provider prompt cache 或请求格式。

### 6.3 Fork 和历史读取的边界

`codey/durable/ancestry.py` 的 cap 语义已经清楚：child 读取 parent 到 `parent_entry_id`，child head 不会修改 parent head，旧路径仍可按 entry 读取。这部分方向正确。

但 `conversation_ops.context_at()` 只过滤 entry 的 `commit_seq`，conversation 本身没有 `created_seq`/`fork_seq`。因此在某个历史 cutoff 上：

- 可能读取一个在 cutoff 之后才创建的 child conversation；
- history cursor 只有 conversation id + entry id，没有 database identity 和 snapshot commit sequence；
- branch 后旧 cursor 的稳定语义依赖当前路径是否仍包含 entry，而不是固定的历史 snapshot；
- 从 tool call 中间 fork 时，是否保留 call、合成 error result 或排除，仍由通用 context pairing 补偿，而不是 fork transaction 明确定义。

Pi 的 context bounds、entry visibility 和 document snapshot 都绑定于可验证的历史位置；Codey 需要至少补 `conversation.created_seq`、fork commit sequence 和 cursor snapshot identity，才能让 `context_at()` 成为严格历史读取而不是“过滤当前 rows”。

### 6.4 第四段应先写的红测

1. head marker/reset 后，旧 entry 不得重新进入 provider context；重启后同一 cutoff 得到相同 digest。
2. system section/tool schema 变化后，provider request 的消息和 tools 顺序可从 durable entries 重建。
3. compaction 与新输入并发提交时，background/旧 compaction 不能覆盖更新的 head。
4. fork child 在 parent entry cap 处读取完整 tool call/result 对；切在未完成 call 上时，行为固定为合成 error 或明确排除。
5. `context_at(child, cutoff)` 在 child 尚未创建的 cutoff 上拒绝；在 fork commit 之后能读取 parent cap + child entries。
6. history cursor 包含 database identity/snapshot sequence，跨重启和 branch 后不会把新路径误当旧页。
7. provider 实际 tokenizer 返回值超过预算时，最终请求仍被截断或转入 compaction，不能只信估算器。

### 6.5 第四段结论

Codey 的 context 安全处理已经明显超过普通聊天历史实现，也已经接入真实 provider token counter 和 durable provider snapshot；但 Pi 的 head/range/system/tool replay 语义仍更完整。下一步不应继续堆更多消息 kind，而应把 context 从“entry list builder”升级为“可验证的 head/tail range protocol”，并把 fork cutoff 与 provider system/tool state 纳入 durable history。

## 7. 第五段：Artifact、旧路径、Ghost 与 AppContext

### 7.1 Artifact：文件完整性强，durable ownership 还不够明确

`codey/storage/managed_outputs.py` 已具备原子文件写入、sha256、scope、schema version、tamper detection 和受控读取。对 shell approval command artifact、tool 大输出和 completion proof，这些安全边界比 Pi 的通用 document 机制更直接。

但当前 durable schema 没有 `artifacts` 一等表。artifact handle/digest 主要出现在 task/effect payload 或其他 projection 中，因此仍有两个未闭合的 crash window：

- artifact 文件写成功，随后 durable commit 失败：孤儿文件如何标记/清理？
- durable commit 成功，随后 artifact 文件写失败或被篡改：task/effect 是否进入 error/unknown？

Pi 的 document record 在同一 Session commit 中拥有 scope、version、lifetime、retirement 和历史读取语义。Codey 不需要复制完整 Chord，但至少应让 `ArtifactRecord` 成为明确的 durable metadata owner，并定义 artifact 写入、校验、settlement 绑定和清理的状态机。

### 7.2 旧路径：主生产路径已切换，但代码层仍有死线风险

AppContext (`codey/app/context.py:140-171`) 已主要构造 durable session，`RuntimeEffectStore` 在有 durable 时从 SQLite 读取，shell approval JSONL 也被注释为 audit projection。这是正确方向。

但以下旧实现仍存在：

- `RuntimeSessionLog`；
- `RuntimeMutationLine` 的 session-log fallback；
- `RuntimeEffectStore` 的 `session_log` fallback；
- `ConversationInputQueue` 独立 SQLite queue；
- `ShellApprovalRecordStore` JSONL audit projection；
- 大量直接构造 JSONL runtime store 的旧测试和离线工具。

现在不能仅凭“AppContext 不构造它”就宣布旧事实源已经删除。必须逐项区分：

1. 只读审计/导出 projection，可保留但改名；
2. 测试专用 fixture，应迁移到 Memory/SQLite durable contract；
3. 生产代码仍可调用的 fallback，必须删除；
4. 恢复路径仍会读取的旧 JSONL，必须由架构测试禁止。

尤其 `RuntimeEffectStore.__init__(session_log=None, durable=None)` 这种双入口，会让调用者继续拥有选择错误事实源的能力。冷启动目标下，生产 effect store 应只接受 durable adapter；如果确实需要 JSONL 读审计，应叫 `RuntimeEffectAuditProjection`，且 API 不提供 recovery/settlement 方法。

### 7.3 Ghost 与主 durable 的边界

Ghost 独立数据库的判断是正确的：后台睡眠守护进程的高频图/权重写入不应争抢主任务 SQLite 写锁。Ghost 不应为了 Pi 的统一 Session 形式并入 `DurableSession`；它拥有不同的生命周期、计算节奏和事实模型。

需要补的是边界审查：Ghost 只能读取主 durable 的稳定 projection 或显式输入，不得从 runtime JSONL 猜 task 状态，也不得让 Ghost commit 反向改变 durable task/effect。两库之间的引用应带 session/task/commit identity，避免后台记忆把已删除或 fork 前的事实重新复活。

### 7.4 AppContext：已有正确的第一步，但仍是生命周期巨型模块

`DurableAppServices` 已经把 durable session + scheduler 的 startup/shutdown 顺序抽出来，且没有退化成万能 ServiceContainer，这是好设计。

AppContext 仍同时拥有：

- durable/scheduler；
- provider registry/supervisor；
- UI state/event bus；
- shell approval workflow；
- Ghost daemon/stores；
- Research/evidence；
- knowledge indexer；
- run registry/trace/ledger；
- workspace leases；
- shutdown/self-repair。

后续拆分应按事实 owner/lifecycle owner，而不是按“每个属性建一个 service”：

- `DurableAppServices`：只负责 durable session、scheduler、open/close/fencing；
- `ShellRuntime`：approval/effect execution/continuation；
- `ResearchRuntime`：Research ledger 和证据投影；
- `ProjectionRuntime`：SSE/UI/audit adapters；
- `ApplicationLifecycle`：stop barrier、background worker、resource close。

不要再增加通用 `ServiceContainer`、repository factory 或跨域 facade。拆分后的每个对象必须能回答“谁拥有这个事实、谁负责关闭、谁能写入”。

### 7.5 第五段应先写的红测和静态门禁

1. artifact 文件写入和 durable commit 各类 crash window 都能恢复为可观察状态，不产生无主成功 artifact。
2. runtime recovery 模块禁止 import `RuntimeSessionLog`、JSONL effect store 或 UI state transcript。
3. `RuntimeEffectStore` 生产构造必须要求 durable；旧 fallback 只能存在显式 audit adapter。
4. Ghost 进程无法写 durable task/effect，也不会从 JSONL 推断恢复状态。
5. AppContext close 的顺序可单独测试：scheduler、shell worker、Ghost、projection、durable store 各有 owner。
6. `rg` 架构门禁能发现 transcript 双写、旧 queue 构造、恢复路径读 JSONL、`save_ui_state()` 写 transcript 等调用。

### 7.6 第五段结论

Codey 在 artifact 安全、Ghost 分库和 Codey 特有安全事实上有明显优势；当前主要问题是“事实已经切换，但旧入口仍可被调用”。全面超过 Pi 所需的最后一层卫生工作不是再加抽象，而是删除 fallback、改名 projection、收紧构造函数和把 AppContext 的生命周期边界拆清。

## 8. 当前阶段总评分（静态，不代表最终验收）

| 领域 | 当前判断 | 说明 |
| --- | --- | --- |
| 会话历史一致性 | 接近 Pi，尚未完全达到 | CAS/commit_seq/branch 已有；lifecycle 与历史 cursor 仍弱 |
| effect 安全恢复 | Codey 明显更强 | unsafe unknown fail-closed、approval、provider uncertainty |
| 权限/审批 | Codey 更强 | durable approval 与 completion/verification 结合 |
| provider 连续性 | Codey 有优势 | snapshot、真实 tokenizer、cold transport replay 已接入；system/tool replay 仍需补 |
| scheduler kernel | Pi 更强 | Codey 缺 invocation/index/abort/background/blocked 语义 |
| input/inbox | Pi 更强 | Codey submission 幂等有了，但 placement/inbox 仍简化 |
| context protocol | Pi 更强 | Codey 有安全配对和预算，缺 head/range/system/tool state protocol |
| artifact | Codey 安全校验更强，owner 较弱 | 缺 durable artifact metadata/crash protocol |
| Ghost/后台记忆 | Codey 边界更合理 | 独立 DB 正确，需静态禁止反向写主事实 |
| 代码整洁度 | 尚不能宣布超过 | AppContext、operation_state、session 和旧 projection 仍偏大 |

截至本记录，结论是“Codey 的安全和审计边界已胜出，durable kernel 仍需补齐 Pi 的生命周期、scheduler、inbox、context range 和清理工作”。

## 9. 第六段：runtime durable-only cutover

本段完成了第一批事实源收敛，且没有运行全量 pytest。

### 9.1 已完成

- `RuntimeMutationLine`、`RuntimeOperationStore`、`RuntimeEffectStore`、`ToolResultDeliveryStore`、`TaskRuntime` 只接受 durable session；旧 JSONL fallback 和 `session_log` 构造参数已删除。
- `ConversationRuntime` 只构造 `DurableConversationInputQueue`；旧独立 `ConversationInputQueue` 不再是生产入口。
- `drive.peek_next_action()` 改为读取 `DurableSession.task_facts()`，用 `checkpoint_entries()` 重建 bounded runtime view，不读取 JSONL。
- 局部类型检查暴露的两个真实未定义名已修复：`contextlib` 和 `LEAF_TERMINAL`。
- 输入队列的类内 `list` 类型遮蔽已用 `builtins.list` 修复，ty 与 ruff 同时通过。

### 9.2 测试迁移证据

旧测试中直接构造 `RuntimeSessionLog` 的用法已迁移到 `MemoryDurableStore`/`SQLiteDurableStore`。迁移过程中保留了故障语义：

- unsafe tool/provider effect 仍 fail closed；
- durable effect intent 缺少完整 runtime identity 时，测试改为提交规范 intent，而不是让生产恢复路径兼容残缺 JSONL；
- 没有 durable runtime operation 的旧 projection 测试改为验证“无恢复事实时不启动恢复”，不再伪造 JSONL authority。

本段聚焦结果：

```text
127 passed, 399 subtests passed
```

覆盖 `test_input_queue_authority`、`test_recovery_integration`、`test_runtime_transaction_owner`、架构边界、manual drive 和 legacy fallback。

### 9.3 类型与静态检查

```text
mypy     Success: no issues found in 145 source files
pyrefly  0 errors
ty       All checks passed
ruff     All checks passed
compileall passed
```

### 9.4 尚未完成

`codey/runtime/log/session_log.py` 仍作为旧 audit projection 文件存在，但当前生产 runtime/app/operations 路径已不再导入它。本段没有删除该模块，因为后续需要单独把它改名为只读审计投影并审查离线工具；它不能重新成为恢复或业务事实源。

本段结论：runtime durable-only cutover 已有代码和聚焦测试证据，旧写入路径不再可由生产构造器选择；下一段应处理 Session failure/sealing/close contract 与内部 publication 失败语义。

## 10. 第七段：Session failure、publication 与 close contract

### 10.1 Pi 对照结论

Pi Session 把 commit 后的内部 adoption 与外部 projection 分开：内部 observer 失败会使 Session 进入 `SessionFailed`，外部 listener 失败只报告；close 先封 admission、通知 close listeners，再等待已接纳工作和 storage 收尾。失败后的后续调用在到达 backend 前就拒绝，必须 close/reopen 后从 durable facts 恢复。

### 10.2 Codey 已补齐

- `DurableSession` 增加 `observe_commits()`、`subscribe_commits()`、`subscribe_close()` 三个明确入口。
- internal observer 在 durable commit 已成功后抛错，会记录 failure cause、封存 Session、通知 close listeners，并让当前调用抛出原始错误；后续调用抛 `SessionFailedError`。
- external projection listener 的错误只记录，不改变 durable commit 或 Session 健康状态。
- mutation line 的所有读写入口统一经过 usable check；close listener 触发前 admission 已封锁，重复 close 不会重复通知。
- `DurableAppServices` 把 scheduler notification 接到 internal observer，把 SSE/UI publish 留在 external subscriber，避免 scheduler adoption 失败被当成普通投影错误吞掉。
- SQLite task validation 的 ownership-cycle 和 existing-task CAS 检查拆成纯 helper，保持 transaction owner 不变并消除 ruff complexity 告警。

### 10.3 红测与验证

新增 `tests/durable/test_session_lifecycle_contract.py`，先锁定三种行为再实现：

1. internal adoption failure 后 durable task 已提交但 Session failed；
2. external projection failure 不封存 Session；
3. close 先封 admission、close listener 只运行一次。

本段聚焦结果：

```text
147 passed, 399 subtests passed
```

局部类型与 lint：

```text
mypy     Success: no issues found in 58 source files
pyrefly  0 errors
ty       All checks passed
ruff     All checks passed
```

### 10.4 尚未完成

当前 close 仍是同步轻量实现，SQLite backend 每次操作临时连接，因此没有 Pi `GuardedStorage` 那种“等待所有 underway backend calls 再关闭”的异步等待集合；这需要在后续 storage/ownership 阶段单独补测试。多进程同一 durable DB 的 open fencing 也尚未解决。

本段结论：Codey 已达到 Pi 的 failure latch、internal/external publication 分层和 close admission 语义；storage underway drain 与跨进程 ownership 仍是 durable 内核差距。

## 11. 第八段：branch head 的 revision CAS

Pi 的历史 head 变更必须在 Session mutation line 上携带调用方捕获的历史位置；两个 writer 对同一旧 head 操作时，后到者得到冲突，不能静默覆盖。

Codey 已为 `DurableSession.branch()`、`MemoryDurableStore.branch()` 和 `SQLiteDurableStore.branch()` 增加 `expected_revision`。CAS 检查在各 backend 的 branch 事务内部执行，成功的 branch 仍发布 durable commit；失败只抛 `CommitConflict`，不会产生新 commit 或改变 head。

红测覆盖 Memory 与 SQLite 两个 backend：两个 Session 使用同一 revision branch 时，一个成功、另一个冲突；同时验证 branch commit 的新 revision、head 和 publication。

本段聚焦结果：

```text
40 passed
```

局部检查：mypy、pyrefly、ty、ruff 全部通过。

本段仍未收敛 create/fork/delete 的 canonical mutation：它们还需要进入明确的 commit/publication 语义，后续再处理，避免把 branch CAS 和生命周期变更混成一个不可审查的大 patch。

## 12. 第九段：多 Session 的 live attempt ownership fencing

Pi durable 的 scheduler 恢复不能只看 `RUNNING` 状态；恢复者必须知道旧 attempt 是否仍被活跃执行者持有，否则第二个 Session 会把正在运行的外部 effect 当成进程崩溃后的 abandoned work。Codey 这一段采用 task attempt 级 fencing：

- `claim_task()` 在 checkpoint 中同时写入随机 `attempt_token`、Session `attempt_owner` 和 `attempt_process_id`；
- 默认 `recover_scheduler_state()` 只回收 owner 进程已经不存在的 running attempt；活跃 PID 永不被普通 scheduler 恢复抢占；
- `force=True` 是显式的行政/测试恢复入口，用于同一 pytest 进程模拟“进程已经退出”的 crash boundary；生产 scheduler 不使用它；
- `unclaim_task()`、`wait_task()`、approval continuation、cancel/fail terminalization 和用户问题唤醒统一清理 token、Session owner 与 process id；
- `TaskCheckpoint` 拒绝 token/owner/process id 不一致的 execution state，terminal release 仍通过同一 attempt CAS；
- `Runtime JSONL`、UI 和 trace 没有参与 ownership recovery 判断。

### 12.1 红测与验证

先新增 `tests/durable/test_live_owner_recovery.py`：owner Session claim task 后，observer Session 的普通 recovery 必须保留 `RUNNING`、`attempt_token` 和 `attempt_process_id`。随后把原来在同一进程内伪造 restart 的 fencing 测试改为显式 `force=True`，并把 storage failure 测试改为验证 Session failure latch 后由 observer 读取未改变的 pending checkpoint。

分段结果：

```text
test_live_owner_recovery.py   1 passed
test_execution_fencing.py    14 passed
test_worker_fencing.py        1 passed
test_scheduler.py            11 passed
```

局部检查：mypy、pyrefly、ty 和 ruff 全部通过。

Windows 下把 live-owner 文件和 execution 文件放在同一 pytest invocation 时，pytest 在第二个测试 fixture setup 阶段出现顺序相关 `KeyboardInterrupt`；反向顺序和各文件单独运行均通过。这是测试生命周期/运行器隔离问题，不能通过让生产 recovery 忽略活跃 PID 来迁就，后续应单独定位测试收尾与 runner 行为。

### 12.2 Pi 对照结论

Codey 已超过此前“无 owner 识别、无跨 Session recovery fence”的实现，也保留了 Codey 的 fail-closed effect 语义。当前仍有两个边界差距：跨进程数据库 open fencing 尚未实现，Session `close()` 也没有把本 Session 的 running attempts 做显式 graceful handoff；这两项留在 scheduler/storage ownership 段继续处理。本段不能宣布 scheduler 全面超过 Pi。

## 13. 第十段：conversation create/fork 接入 canonical mutation line

对照 Pi 的 mutation line 后，发现 Codey 还有一个容易被忽略的旁路：普通 `create_conversation()` 直接调用 store，`fork()` 通过 backend 专用方法再合成一个不带创建信息的 publication。这样 commit cursor 能看到某些历史 entry，却不能从同一 mutation 重建 conversation ancestry。

本段已完成：

- `DurableSession.create_conversation()` 改为提交 `DurableMutation(create_conversation=...)`，返回真实 `CommitResult`；已存在的相同 root conversation 保持幂等读取；
- `conversation_ops.fork()` 改为走同一个 `_commit()`，child conversation 的 parent conversation/entry cap 直接成为 mutation 事实；
- `mutation_projection()` 增加 `conversation_created` 投影，创建、输入 admission 和 fork 都能通过同一个 durable commit/SSE cursor 观察；
- provider/task admission 原有的“新 conversation + user entry + submission + task”原子提交继续复用同一字段，没有增加第二条写入路径；
- 原来依赖创建不占 commit 序号的测试已迁移到新的 durable 语义，不能让主代码倒退去迎合旧 cursor 假设。

红测先要求 Memory 与 SQLite 都产生 creation commit，并要求 fork publication 携带 parent cap；随后扩大到 commit replay、fork ancestry、store contract 和 session 测试：

```text
93 passed
mypy / pyrefly / ty / ruff 全部通过
```

本段仍有一个明确未收尾项：`delete_conversation()` 仍直接调用 backend，删除事实没有 durable commit，也没有可补发的 deletion projection。下一段只处理删除事务和 child/dependency 约束，不把 artifact 或 scheduler 语义混进来。

## 14. 第十一段：conversation delete 接入 durable commit

删除是另一条容易被忽略的恢复旁路。此前 `DurableSession.delete_conversation()` 直接调用 backend，删除成功后没有 `commit_seq`、没有 projection，SSE 断线客户端无法知道某个 conversation 已经被移除；同时 parent 有 child 时的拒绝只存在于 backend 专用路径。

本段已完成：

- 新增明确的 `DeleteConversation` mutation，禁止和 entry、task、effect、submission 或 approval 混在同一 mutation；
- Memory 与 SQLite 在各自事务内检查 child cap、写 `conversation_deleted` projection、清理该 conversation 的 entries/submissions/tasks/effects，再提交 deletion commit；
- `DurableSession.delete_conversation()` 读取当前 revision 后走 `_commit()`，因此 publication failure、SSE cursor 和 Session failure latch 与其他 durable mutation 一致；
- 删除 parent 的 child 约束在 commit 前检查，失败不会改变 head、sequence 或 source/child conversation；
- SQLite 开启 foreign keys 时按 effects → tasks → submissions → entries → conversation 的顺序删除，避免半删除状态。

红测与扩展结果：

```text
test_conversation_delete_commit.py + 相关 commit/fork/store contract
73 passed
mypy / pyrefly / ty / ruff 全部通过
```

旧的 backend `store.delete_conversation()` 原始方法仍保留给底层 contract 测试和离线维护入口；生产 `DurableSession` 已不再调用它。后续结构清理阶段需要把这些 backend 专用写入口收窄，避免形成第二条业务写入路径。

## 15. 第十二段：scheduler blocked reason 与 registry recheck

Pi 的 scheduler 对缺失、过旧或迁移失败的 task definition 返回派生的 blocked reason：任务保持 pending/waiting，不被错误 terminalize；registry 改变时重新考虑。Codey 原先只是在 `_workflow_ready()` 返回 false 后把 task 从本轮内存候选移除，外部无法区分“暂时 blocked”和“已经不再存在”。

本段已完成：

- `DurableTaskScheduler.blocked_tasks` 暴露当前 registry 下的 `{task_id: reason}`，reason 使用 `missing_task`、`task_too_old`、`migration_failed`；
- blocked reason 是 scheduler 派生状态，没有污染 `TaskCheckpoint` 或把 runtime registry 错写成 durable 事实；
- `set_workflows()` 提供窄的 registry replacement 入口，清除旧 blocked cache 并唤醒 scheduler；
- 任务仍保持 `PENDING`，定义恢复后重新进入候选并只 dispatch 一次；
- 原有 restart/abandon 测试改用显式 `force=True` 模拟同一 pytest 进程内的进程退出，生产 scheduler 仍走 live-PID 安全恢复。

测试与静态检查：

```text
test_scheduler.py + test_scheduler_blocked.py + task ownership/finalization
19 passed
mypy / pyrefly / ty / ruff 全部通过
```

这只补齐 Pi 的 blocked 可观察语义，尚未实现 Pi 的 `TaskInvocation`、abort invocation、background boundary 或 waiting reverse index；下一 scheduler 段应先处理 durable abort mark 与 active invocation signal 的边界。

## 16. 第十三段：scheduler invocation signal 与 abort 顺序

Pi 的 abort 顺序要求先提交 durable abort mark，再 signal 当前 `TaskInvocation`，避免 signal 先发生而 durable recovery 看不到取消事实。Codey 原先只有 task state 中的 `cancel_requested`，scheduler 没有可观察的 active dispatch invocation，host 也无法通过 scheduler 取得 attempt-bound signal。

本段已完成：

- 增加轻量 volatile `TaskInvocation`，绑定 `task_id`、`attempt_token`、`mode`、`signal` 和 `done`；它不成为 durable 事实源；
- scheduler 在 claim 成功后登记 invocation，dispatch callback 返回或抛错时标记 done 并移除；
- `abort_task()` 先调用 `DurableSession.request_cancel()` 写入取消屏障，再 signal active invocation；
- dispatch 返回后若发现 durable cancel barrier，scheduler 立即 terminalize cancellation，不把取消任务重新放回普通 pending 队列；
- 原有 worker attempt CAS 继续拒绝 barrier 之后的非 terminal mutation。

红测让 dispatch callback 持有 invocation signal：主线程调用 `abort_task()` 后，先观察 durable `cancel_requested`，再观察 callback 收到 signal，最后确认 task terminal cancelled。scheduler、blocked、ownership/finalization 合并聚焦结果：

```text
20 passed
mypy / pyrefly / ty / ruff 全部通过
```

这仍是 scheduler invocation 的第一层边界。Codey 还没有 Pi 的 abort handler invocation、background ownership boundary、close 时 join 全部 invocation，以及由 waiting reverse index 驱动的局部 reconcile；下一段再处理其中一个，不把所有 scheduler 语义一次性塞进 `DurableTaskScheduler`。

## 17. 第十四段：waiting reverse index 的局部唤醒

Pi 在 task settlement 后通过 waiting reverse index 只重新考虑受影响的 waiter。Codey 原先的 `notify(task_id)` 只把 task 自身放进候选；waiter 依赖全量 reconcile 或调用方手动触发，两个 waiter 共享 child 时尤其容易留下未唤醒的 pending 状态。

本段已完成：

- `DurableTaskScheduler.notify(task_id)` 从 durable task facts 派生依赖该 task 的 `WAITING` waiters，并把所有受影响 waiter 放入候选；
- terminal child 本身不再被无意义地重新排队；
- 现有 `reconcile_waiting_tasks()` 继续是状态转换 owner，reverse index 只负责局部唤醒，不复制 task 状态；
- scheduler thread 创建时释放 scheduler lock 后再启动，避免唤醒路径与 worker bootstrap 互相等待。

红测覆盖两个 waiter 等待同一个 child：child 完成后只调用 `notify("child")`，scheduler 就会依次 dispatch 两个 waiter。与 blocked/abort/scheduler/ownership 测试合并聚焦结果：

```text
21 passed
mypy / pyrefly / ty / ruff 全部通过
```

这仍不是完整 Pi index kernel：跨 Session 的 reverse index 需要从 durable commit 重建，等待关系的删除/修改也还没有独立索引表；当前实现优先保证语义和恢复正确，再在性能段决定是否需要持久化 placement/index。

## 18. 第十五段：queued input 的 durable abort

Pi 的 queued submission 可以在进入 active run 前撤回；撤回只改变 queued input 的提交状态，不创建第二个 task，也不依赖 UI 或旧 input SQLite。Codey 原先的 `DurableConversationInputQueue.remove()` 仍然直接抛错，queued input 只能通过较底层的 task control 处理，浏览器输入 facade 没有完整语义。

本段已完成：

- `remove(identity, revision)` 读取 durable input projection，校验 request identity、revision 和 queued 状态；
- 通过 `DurableSession.request_cancel(..., reason="aborted")` 在同一 durable task/submission 事实源提交取消；
- 取消后 projection 变为 `cancelled`，submission reason 为 `aborted`，task 数量不增加；
- 事件和 wakeup 仍由 input projection facade 发布，未恢复旧 `conversation_inputs.sqlite3`；
- 已 placed/running input 仍拒绝通过 queued remove 路径撤回，避免绕过 active task cancellation barrier。

红测与相关 task failure contract：

```text
27 passed
mypy / pyrefly / ty / ruff 全部通过
```

这只是 inbox parity 的第一步。Codey 仍没有 Pi 的独立 queued/placed document、steer/follow-up first/all placement 和 passive write 策略；下一段需要先定义 placement mutation 的事实边界，再决定是否扩展 submission 模型，不能重新引入第二个 queue store。

## 19. 2026-10-19 当前代码快照（基于已提交实现）

本节覆盖前面阶段性记录之后的实际代码。前文中把以下能力列为“待补”的描述已经过时：

> 本节是 placement 改造前的历史快照；第 20 至 22 段是后续提交后的权威状态，阅读当前结论时以后续段落为准。

- `DurableSession` 已区分 `observe_commits()`（内部一致性观察）和 `subscribe_commits()`（外部投影）；内部观察失败会封存 Session，外部投影失败只记录日志。
- conversation 的 create/fork/delete 已通过 durable commit 产生 commit sequence 和投影；branch 已有 revision CAS。
- TaskCheckpoint 已保存 attempt token、owner 和 owner process id；普通恢复不会回收仍存活的进程所有者。
- scheduler 已有 blocked task reason、volatile invocation signal、abort barrier，以及等待依赖的反向唤醒。
- queued input 的撤回已经写入 task/submission 的 durable cancellation，不再创建第二个 task 或依赖旧输入数据库。

### 7.1 当前已经明确胜出的部分

这些结论现在有代码和局部测试作为依据：

1. **effect 安全恢复**：intent、settlement、未知外部结果和 replay class 已进入 durable 状态；shell/provider 的未知结果不会被普通重启自动重跑。
2. **权限与验证边界**：权限快照、审批状态、provider snapshot、verification、completion proof 和 Research evidence 保持独立语义，强于 Pi 的通用任务内核。
3. **提交可观察性**：创建、分支、fork、删除、task/effect/submission 更新都能产生 durable commit sequence；SSE 可以按 sequence 补读事实投影。
4. **执行 ownership**：attempt token 与 process fencing 组合后，旧 worker 的提交不能覆盖新 attempt；live owner 不会被普通恢复路径误判为遗留工作。
5. **取消传播**：取消先写 durable barrier，再 signal 当前 invocation；等待中的相关 task 能被 settlement 反向唤醒，queued input 的 abort 也遵守同一事实源。
6. **Ghost 边界**：认知/记忆使用独立 `ghost.sqlite3`，没有把后台高频写入混进主任务数据库。

### 7.2 仍然低于 Pi 的硬差距

1. **Inbox/placement 语义**：Codey admission 仍在提交时创建 user entry，queued input 通过 task state 扫描反推；Pi 先把 submission 放入 conversation-scoped inbox，在 boundary 按 steer/follow-up/write 策略 placement。Codey 尚无正式的 `placement`、`steer`、`followUp`、`passive write` 状态和同一队列的恢复顺序。
2. **Scheduler kernel**：Codey 已具备关键安全边界，但 runnable、waiting reverse index、owner graph、finalization/fail-fast 候选主要仍由 `store.tasks()` 全量扫描派生；Pi 将这些索引和 invocation 生命周期作为 scheduler kernel 的稳定边界，并有 abort handler、background ownership 和 close 时 join 语义。
3. **Context range**：Codey 有 ancestry、compaction、tool pairing、provider tokenizer 和 digest，但还没有 Pi 的一等 head marker、ContextBounds 的 head/tail 不变量、created/fork sequence cutoff、system section positional replay 和 provider-specific context range cache。
4. **Artifact ownership**：`ManagedOutputStore` 有原子文件写入、digest、scope 和 tamper detection，但 durable schema 没有一等 artifact record；文件成功而 commit 失败、commit 成功而文件落盘失败的恢复契约仍需明确。
5. **多进程 open fencing**：单个 Session 内的 attempt process fencing 已完成；但同一 durable DB 被两个独立应用 Session 同时打开时，仍需 state-home lease 或明确拒绝第二个 owner，不能只依赖启动扫描。
6. **旧路径和结构卫生**：`RuntimeSessionLog`、旧 runtime store API、独立 `ConversationInputQueue`、JSONL approval/audit projection、巨型 `AppContext`/`operation_state.py`/`DurableSession` 仍需要逐项判定是只读投影、测试 fixture、生产 fallback 还是死代码。只有删除生产 fallback 后，恢复事实源才真正唯一。

### 7.3 本段结论

当前 Codey 已经可以在 effect fail-closed、权限/审批、provider uncertainty、completion proof、Research 证据、attempt fencing 和取消安全上明确胜过 Pi；它还不能在 durable 内核整体上宣布胜出。下一段应优先审查 inbox/placement，而不是继续堆新的 scheduler 状态字段：先证明“admitted 但未 placed”的语义，再决定是否需要改变 submission schema 和 context 入口。

## 20. 第十六段：inbox placement 第一批实现

本段按 Pi `harness/inbox.ts` 的 admitted/placed 分界收敛 Codey 输入路径，保持冷启动 `SCHEMA_VERSION = 1`。没有增加旧数据库 migration，也没有恢复旧 input SQLite 写路径。

这部分对应提交 `403dbaed`。该提交已经完成代码和测试，但本次审查补上按提交记录的结果，避免只在代码里留下 placement 语义而没有比较结论。

### 20.1 已完成

- `SubmissionDraft`/`SubmissionRecord` 增加明确的 `placement`、`mode` 和 queued content；queued submission 可以没有 `entry_id`。
- `submissions.entry_id` 允许为空，并保存 placement、mode、content、admission commit sequence；schema 仍是冷启动版本 1。
- `DurableSession.admit_input(placement="queued")` 只提交 submission + task，不写 conversation entry。
- `DurableSession.place_submission()` 在一个 durable commit 中追加 user entry、更新 task 的 `input_state/input_entry_id`，并把 submission 置为 placed；重复 placement 不会追加第二个 entry。
- Memory 和 SQLite 的 idempotency、submission update、placement 校验保持一致。
- `DurableConversationInputQueue` 在 conversation 有 active task 时 admission 为 queued；空闲 conversation 立即 placed。
- scheduler 在 active task 结束后才 placement queued input，然后取得普通 task attempt；queued input 不会被直接 dispatch。
- input projection 改为以 durable `input_state` 和 terminal status 为准，修复了 `MappingProxyType` 被误判为非 mapping 的旧问题。

### 20.2 验证

```text
inbox/placement + input authority + store contract: 55 passed
provider/scheduler/recovery 受影响集合（含 placement）：85 passed
mypy: Success
pyrefly: 0 errors
ty: All checks passed
ruff: All checks passed
```

### 20.3 与 Pi 仍有差距

当前 placement 是“conversation 空闲后放置”的最小 durable 语义，还没有 Pi 的：

- steer/follow-up 的 first/all boundary 策略；
- 非 display-only 的完整 passive write；
- reset/head placement 和 stale write 规则；
- placement 与 active generation boundary 的同一事务衔接。

因此本段只完成了事实边界和恢复顺序的基础，不宣布 inbox parity。下一段应把 boundary selection 做成明确 mutation，而不是继续依赖 scheduler 的全量扫描猜测。

## 21. 第十七段：steer/follow-up boundary selection

本段把 `403dbaed` 留下的第一处行为缺口接入 scheduler：同一 conversation 在 active task 终止后，queued input 按模式分组选择，顺序固定为 `write → steer → follow_up`。每一类内部保持 durable admission 顺序；`steering_mode` 和 `follow_up_mode` 分别决定一次放置一个还是全部放置。若本 boundary 有 steer，follow-up 留在 queued 状态，等待下一 boundary，避免较早 admission 的 follow-up 越过较晚到达的 steer。

同时调整 `DurableAppServices` 的内部 commit observer：task 进入终态时触发全量 scheduler reconcile，因为释放 conversation 的 task 与待放置 submission 不是同一个 task；普通 input/task 控制提交仍按 task 定点唤醒。这样 queued input 的放置不依赖 UI、JSONL 或额外的内存队列。

### 21.1 验证

本段先运行新增 steer 优先测试确认红，再修改生产代码变绿：

```text
steer/follow-up placement + input authority + scheduler: 24 passed
durable service lifecycle regression: 2 passed
mypy：Success: no issues found in 58 source files
pyrefly：0 errors
ty：All checks passed
ruff：All checks passed
SCHEMA_VERSION = 1
```

### 21.2 当前相对 Pi 的结果

- Codey 已有 admitted/queued/placed 的 durable 事实边界，并能在终态 boundary 后恢复放置；
- steer 与 follow-up 的选择顺序、每类 `all/one-at-a-time` 策略已成为 scheduler 的显式规则；
- scheduler 唤醒不再只依赖当前终态 task 的 task id，减少同 conversation queued 输入卡死的恢复旁路。

仍低于 Pi 的部分：

- `write` 已能表达无 task 的 display-only entry，但还没有 Pi 的完整 passive write、head/reset 和 stale placement；
- placement 仍由 scheduler 调用 `place_submission()` 分步完成，尚未与 provider generation 的 post-tools/final boundary 共用一个事务；
- queued input 的跨进程 scheduler ownership、完整 invocation/background/abort kernel 仍未达到 Pi 的完整度。

## 22. 第十八段：display-only passive write

本段补上 Pi inbox 中最小但容易遗漏的一类提交：不触发 generation、没有 task、可以在 active task 期间落地的 display-only write。

### 22.1 已完成

- `DurableSession.admit_write()` 使用和 input 相同的 request-id 幂等入口，但不创建 `TaskCheckpoint`；
- placed write 在 submission、entry 和 commit sequence 同一事务中直接进入 `DONE`；queued write 只保存 submission content，等待 scheduler placement；
- Memory/SQLite 都能列出 queued submissions，scheduler 会放置 display-only write，即使 conversation 仍有 active task；
- display-only entry 通过 durable payload 标记，在 `ContextBuilder` 中被排除，不会伪装成 provider system message；
- 重复 request-id 返回原提交，不会重复 entry；冷启动 schema 仍为 `SCHEMA_VERSION = 1`，没有 migration 或旧写入 fallback。

### 22.2 验证

```text
inbox placement + display-only write + scheduler + store contract: 74 passed
新增 display-only write 专项：4 passed
mypy：Success
pyrefly：0 errors
ty：All checks passed
ruff：All checks passed
```

### 22.3 与 Pi 仍有差距

- 当前 `admit_write()` 只实现 display-only payload，尚未支持带 model/head/edits 的完整 passive entry draft；
- `head`/reset/stale 的第一层语义已在下一段补齐，但仍未与完整 passive entry draft 和 postTools/final 事务边界合并；
- queued write 的提交顺序已经 durable，但还没有 Pi `pi.inbox` document 那样的 position-based removal 和 fork-local inbox snapshot。

## 23. 第十九段：head range 与 stale write

本段把 write 从“只会追加普通 entry”推进到可验证的 context range 语义。head 信息作为 submission content 和 entry payload 的 durable 事实保存，恢复时不依赖内存窗口或 JSONL 推断。

### 23.1 已完成

- `DurableSession.admit_write(..., head="self")` 创建 head marker；`conversation_ops.context()` 和 `context_at()` 始终应用最新 marker，只暴露 marker 及其后的当前 range；
- queued head write 在 placement 前重新读取当前 durable context；目标 entry 已不在当前 range 时，不追加 entry，而是原子结算 `SubmissionStatus.UNANSWERED`、`reason="stale"`；
- direct placed head write 也在 admission commit 内完成同样的 stale settlement，不会先写 entry 再补偿删除；重复 request-id 返回原 stale commit；
- 当前 range 内的 head write 正常 placement，并与 submission `DONE` 在同一 commit 中完成；
- head write 禁止标记为 display-only，避免 UI-only entry 意外改变 provider context；
- Memory/SQLite 都通过相同的 head cutoff、stale settlement 和重启可读性测试；schema 仍严格为 `SCHEMA_VERSION = 1`。

### 23.2 验证

```text
inbox/head/stale + scheduler + context + fork + session 回归：143 passed
mypy：Success
pyrefly：0 errors
ty：All checks passed
ruff：All checks passed
```

### 23.3 当前相对 Pi 的结果

Codey 现在已经具备 Pi head marker 的核心恢复性质：旧 context 不会在 reset 后重新进入 provider 请求，过期 queued summary 不会制造幽灵 entry，并且 stale 原因属于 durable submission 状态。仍未完成的是完整 `EntryDraft` 的 `model/head/edits` 结构、position-based inbox removal、postTools/final 同事务 boundary，以及 fork-local inbox document。

## 24. 第二十段：generation boundary 接入实际 kernel

本段继续沿用冷启动 `SCHEMA_VERSION = 1`，没有改动或回滚 `01bb0e09`。目标是把前面已经存在的 queued/placed 事实接到实际 provider 轮次，而不是只让 scheduler 在 task 终态后补放置。

### 24.1 已完成

- `DurableSession.place_boundary_inputs()` 成为明确的 `post_tools`/`final` placement 入口；它按 durable submission 顺序选择 write、steer、follow-up，并复用现有 `place_submission()` 的原子 entry/task/submission 提交。
- `DurableTaskScheduler` 的空闲 conversation placement 改为调用同一 boundary API，删除 scheduler 内重复的 mode 分组和选择逻辑；active task 期间的 display-only write 仍可独立落地。
- `KernelObservationDeps.on_boundary` 接入 `run_task_kernel()`：工具轮次结束调用 `post_tools`，done handoff 调用 `final`。entry kernel 为真实 durable session 构造 callback，文本 provider 会把新 placed user input 放进下一次 prompt。
- steer 继续拥有当前 boundary；因此较晚到达的 steer 不会被较早 follow-up 越过。follow-up 在下一 boundary 再放置。这是 Codey 在交互安全上的明确策略，和 Pi 在 final boundary 同时选择两类输入的通用策略不同。
- callback 失败会按 provider failure 收口，不能吞掉 durable placement 或继续向 provider 发送下一轮。

### 24.2 红测与验证

先加入 generation boundary 红测，再实现 durable API 和 kernel callback。当前聚焦结果：

```text
inbox placement + scheduler + text kernel + native regression: 58 passed
mypy: Success
pyrefly: 0 errors
ty: All checks passed
ruff: All checks passed
SCHEMA_VERSION = 1
```

### 24.3 相对 Pi 的结果和未完成项

已经更强或更清楚的地方：

- placement 现在有唯一的 durable boundary API，scheduler 和真实 task kernel 不再各自猜一套队列规则；
- 文本 provider 的工具轮次后 steer 输入可以在下一次请求前进入 prompt，placement 不依赖 UI、trace 或 JSONL；
- callback 错误会 fail closed，保持 Codey 的 provider/effect 安全边界。

仍低于 Pi 的地方：

- 当前每个选中的 submission 仍通过一个独立 durable commit 放置；Pi 在 generation 的 boundary transaction 中同时完成 inbox、tool round、下一 generation checkpoint 和 live state 更新。Codey 尚未把这些事实合并为一个多任务 boundary mutation；
- native provider 为了保持未完成 tool-call 的 protocol receipt 链，当前只提交 durable placement，尚未把 queued user entry 编入同一 native request 的最终 provider message range；
- fork-local inbox 目前只有 queue 查询隔离测试，还没有 Pi `InboxDoc` 那样的 fork snapshot/history 语义；
- scheduler 仍缺 Pi 的完整 owner graph、background boundary、abort handler invocation 和持久化 reverse index。

本段结论：Codey 已把 admitted/queued/placed 从“只在 scheduler 终态补偿”推进到真实文本 kernel 的 generation boundary；但要达到 Pi 的完整 generation 原子性，下一段必须把 tool settlement、boundary placement 和下一次 task checkpoint 收进同一 durable mutation，并补 native protocol window 的 context 重建测试。

## 24. 第二十段：完整 passive entry metadata

本段继续收敛 Pi inbox 的 passive write 语义。此前 `admit_write()` 只能把 `data`、`display_only` 和第一层 `head` 保存下来；这会丢失 provider/model 归属和 context edit，queued placement 也无法证明重启后仍是同一条 entry draft。本段保持冷启动 `SCHEMA_VERSION = 1`，不增加迁移、不恢复旧写入路径。

### 24.1 已完成

- `DurableSession.admit_write()` 增加可选的 `model` 与 `edits`，并在 admission fingerprint 中纳入两者，重复 `request_id` 不能以不同 metadata 重写原提交；
- `SubmissionRecord.content` 和 placed `EntryDraft.payload` 同时保存 `model`、`edits`、`head`，direct placement 与 queued placement 使用同一份规范化字段；
- queued write 在 scheduler placement 或重启后重新打开 SQLite 时仍保留完整 metadata，placement 仍与 submission、entry 在一个 durable commit 内完成；
- passive edits 在 admission 时校验 target/action/replacement message 结构；display-only write 不允许携带会改变 provider context 的 edits；
- `EntryDraft` 提供只读的 `model`、`head`、`edits` 访问器，context builder 继续按 durable edits 应用 omit/replace，避免调用方重复解析 payload；
- Memory 和 SQLite 共用相同的规范化与恢复契约，没有增加第二个 inbox 或 projection 写入者。

### 24.2 验证

```text
inbox placement + context + fork：50 passed
新增 passive metadata 与 SQLite restart：27 passed
mypy codey/durable：Success
pyrefly check codey/durable：0 errors
ty check codey/durable：All checks passed
ruff：All checks passed
SCHEMA_VERSION = 1
```

### 24.3 与 Pi 的结果

在 passive entry 的 `model/head/edits` 持久化和 context edit 重建这一段，Codey 已达到 Pi 语义所需的事实完整性，并额外保留 request-id 幂等、CAS、stale settlement 和双存储 contract。这个结果可以把此前“完整 passive write”从硬差距中移除，但不能据此宣布 inbox 或 durable kernel 全面超过 Pi。

仍未完成：

- inbox queued item 的 position-based removal，以及 fork 后独立的 inbox snapshot；
- passive placement 与 provider postTools/final boundary 的同一事务衔接；
- provider-specific system/tool protocol、ContextBounds tail/head 不变量和 context range cache；
- scheduler 的 background ownership、完整 abort handler、close join 和跨进程 DB open fencing；
- artifact 一等 durable metadata、旧 projection 清理以及 AppContext/巨型模块的结构审查。

## 25. 第二十一段：按 durable position 撤回 queued submission

本段对照 Pi `InboxDoc.items` 的数组语义，修正 Codey queued submission 的顺序和撤回边界。此前 Memory 依赖字典插入顺序，SQLite 依赖 `rowid`；两者虽然通常一致，却没有把 inbox position 写成可证明的 durable 规则。本段仍只使用 `submissions.commit_seq`，没有新增第二个 inbox 表，也没有改变 `SCHEMA_VERSION = 1`。

### 25.1 已完成

- `MemoryDurableStore` 和 `SQLiteDurableStore` 的 queued 查询都按 admission commit sequence 排序，并支持 conversation scope；顺序不再依赖 Python 字典或 SQLite `rowid`；
- `DurableSession.withdraw_queued_submission_at(conversation_id, position, expected_submission_id=...)` 经过 mutation line 读取当前队列，位置变化时用 submission ID CAS 拒绝误删；
- queued write 按位置撤回时在一个 durable commit 中结算 `unanswered/aborted`，不创建 entry；
- queued input 按位置撤回时同时把 task 置为 terminal `cancelled`、submission 结算为 `unanswered/aborted`，不会留下可调度的孤儿 task；
- 位置撤回保留 queued write 的显式事实，active task 的普通 abort 路径仍不会误删同 conversation 的 queued writes；
- Memory/SQLite 共用同一顺序、CAS 和撤回 contract，重启后仍按原 admission 顺序读取。

### 25.2 验证

```text
inbox + context + scheduler + input authority：61 passed
mypy：Success
pyrefly：0 errors
ty：All checks passed
ruff：All checks passed
SCHEMA_VERSION = 1
```

### 25.3 与 Pi 的结果

Codey 已在 durable 内核层补上 Pi positional inbox removal 的核心性质：位置来自持久化提交序号，撤回和 submission/task 结算不可分离，且重复或错位操作会 fail closed。相对于 Pi，Codey 还额外保留 task CAS、attempt fencing 和安全取消语义。

仍未达到完整 Pi inbox document 的部分：

- Web `DurableConversationInputQueue` 仍是输入投影 facade，尚未把 queued passive writes 显示为同一 inbox view；
- fork 后没有独立的 fork-local inbox snapshot/document；
- postTools/final boundary 仍由 scheduler 分步调用 placement，尚未在同一个 provider generation mutation 中完成；
- scheduler reverse index、background ownership、close join 和跨进程 open fencing 仍未完成。

## 26. 第二十二段：artifact 一等 durable metadata

本段把 Pi durable 对外部大输出的“引用可恢复”原则接入 Codey 的主 durable commit。正文继续留在 `ManagedOutputStore` 文件中；SQLite 只保存经过校验的元数据和 owner 关系，避免把大正文塞进会话表，也避免恢复时依赖旁边的 JSON metadata。

### 26.1 已完成

- 新增 `ArtifactRecord`，固定保存 `handle`、sha256、字节数、存储路径、创建时间、metadata，以及 `owner_task_id`/`owner_effect_id`；没有 owner 的 artifact 在模型层拒绝；
- `DurableMutation.artifacts` 接入 Memory/SQLite，同一 mutation 可同时写 task checkpoint、effect settlement 和 artifact 引用；artifact owner 必须已存在或在同一 mutation 中创建，handle 不能重复；
- 新增 `artifacts` SQLite 表和 task/effect 索引，冷启动 `SCHEMA_VERSION` 仍严格为 `1`，没有迁移或旧数据库 fallback；Memory/SQLite 读取契约一致，重启后可读取；
- conversation 删除会清理其 task/effect 所拥有的 artifact metadata；事务失败时 metadata 不可见；
- `ManagedOutputStore.durable_record()` 只在读取并校验文件和 digest 后生成 durable metadata；工具结算和恢复结算都把该引用与 effect settlement 放进同一个 durable commit；大正文仍由文件存储负责原子写入；
- `DurableSession.settle_effect(..., artifacts=...)`、`RuntimeMutationLine` 和 checkpoint adapter 已贯通这条路径。

### 26.2 验证

```text
artifact Memory/SQLite contract、owner 校验、settlement 原子性、回滚、重启、删除、ManagedOutputStore 校验：12 passed
effect identity、commit events、conversation delete 回归：41 passed
mypy：Success
pyrefly：0 errors
ty：All checks passed
ruff：All checks passed
SCHEMA_VERSION = 1
```

本段还观察到一批旧 runtime JSONL 测试仍按已删除的 `RuntimeMutationLine(log)` 构造方式运行；这些是测试脚本滞后于当前 durable-only API 的架构不适应，不能用兼容参数把旧事实源接回生产代码，后续应迁移测试到 `RuntimeMutationLine(durable=...)`。

### 26.3 与 Pi 的结果

Codey 现在拥有比 Pi 更明确的 artifact 安全边界：外部 bytes 不进入 SQLite payload，引用必须绑定 task/effect，settlement 与引用同一事务提交，未知或失败时不会留下半个 durable artifact 事实，并保留 Codey 的 effect replay、权限和验证语义。

仍未完成：

- `postTools`/`final` provider generation boundary 的同一 mutation placement；
- fork-local inbox、ContextBounds 的 provider-specific 编码和 range cache；
- scheduler reverse index、background ownership、abort/close join、跨进程 DB open fencing；
- Runtime JSONL 旧 projection 的最终清理，以及 AppContext、`operation_state.py` 和 DurableSession 的结构拆分审查。

## 27. 第二十三段：fork-local inbox 隔离

本段补上对 Pi fork 语义的实际回归证明。Codey 的 submission 本来就带有 `conversation_id`，fork 只创建带 parent cap 的 child conversation，不复制父 conversation 的 queued submissions；因此可以得到 Pi 所需的最小 fork-local inbox 语义，而不引入共享 entry 引用或级联垃圾回收。

### 27.1 已完成

- fork 后 child 的 queued inbox 为空，父 conversation 的 queued item 保持原样；
- child 新增 queued input 只出现在 child，不能污染 parent queue；
- parent/child 的 queued position 查询都按各自 durable admission 顺序读取；
- 该隔离在 Memory/SQLite fork ancestry contract 中由回归测试锁定。

### 27.2 验证与边界

```text
fork ancestry + compaction + fork-local inbox：9 passed
```

这移除了“fork 后完全没有 local inbox 隔离”的硬差距。仍未完成的是 queued submission placement 与 provider `postTools`/`final` boundary 的同一事务，以及跨 conversation 的更复杂共享引用和回收语义；后者按 Pi 的最小 parent-cap 目标暂不提前引入。

## 28. 第二十四段：DurableAppServices 的 DB open fencing

本段补上 Pi 单一 Harness ownership 对应的应用边界。此前 Codey 只有 task attempt token/process fencing；两个独立 `DurableAppServices` 仍可同时打开同一个 `durable.sqlite3`，第二个实例启动恢复时可能误判第一个实例的 running task。现在在 services 层持有已有 OS-backed `acquire_lease()`，lease 覆盖整个 scheduler/session 生命周期。

### 28.1 已完成

- 第一个 `DurableAppServices.open(database_path)` 持有数据库 sidecar lease；同一进程或跨进程的第二个 open 立即以 `LockTimeout` 失败；
- 第一个 services 完整 close 后 lease 释放，数据库可以重新打开；
- store/session 直接构造仍可用于 Memory/SQLite contract 和 observer 测试，ownership fencing 只约束应用级 services，避免把测试或只读检查错误地变成全局单实例；
- lease 获取失败或 schema open 失败时会释放已获取的句柄；`SCHEMA_VERSION = 1` 不变，用户要求保留的 `01bb0e09` schema reset 路径未修改。

### 28.2 验证

```text
DurableAppServices lifecycle + same-database open fence：3 passed
mypy codey/app/durable_services.py：Success
pyrefly：0 errors
ty：All checks passed
ruff：All checks passed
```

### 28.3 与 Pi 的结果

Codey 已消除“第二个应用 Session 可以无条件接管同一 durable DB”的硬风险，并保留 task attempt fencing 作为更细粒度的 worker 保护。相对 Pi 仍不足的是完整 scheduler invocation/abort/join 生命周期：lease 只禁止第二个应用 owner，不能替代 Pi 对任务级 live index、abort handler、background ownership 和 completing/finalization 的完整协调。

## 29. 第二十五段：删除已失效的旧 input queue 测试入口

本段是结构卫生清理。生产代码已经只保留 `DurableConversationInputQueue`，但旧测试仍导入不存在的 `ConversationInputQueue`，在 pytest collection 阶段失败。不能为了旧测试重新引入独立 input SQLite 或兼容 facade。

### 29.1 已完成

- 删除 `tests/test_conversation_input_queue.py` 这套针对已删除生产类的测试；
- durable input 的 admission、restart、queued abort 和不创建第二 task 的行为继续由 `tests/durable/test_input_queue_authority.py` 与 inbox contract 覆盖；
- 生产代码没有新增 fallback、旧写入路径或测试专用兼容层；
- `SCHEMA_VERSION = 1` 保持不变。

### 29.2 与 Pi 的结果

这一步没有增加运行时能力，但消除了一个会误导维护者的死测试入口，测试现在跟随 durable-only 主架构。它使代码卫生更接近 Pi 的单一 input owner，也避免把旧 queue 误认为仍受支持。

仍未完成：

- `RuntimeSessionLog` 相关旧测试和手工脚本仍需逐项迁移为只读 audit projection 或 durable contract；
- generation boundary 的多事实原子提交、native context range、scheduler 完整生命周期仍是主要差距。

## 30. 第二十六段：generation boundary 的多事实原子提交

本段把上一段仍未完成的 boundary placement 差距收窄。此前 `place_boundary_inputs()` 虽然每个输入都通过 durable mutation，但一个 Pi generation boundary 选中多个 steer/follow-up 时会产生多个 commit。现在一个 boundary 预先构造所有 entry、task checkpoint 和 submission update，再通过一个 `DurableMutation` 提交。

### 30.1 已完成

- `DurableMutation` 支持同一事务中的多个 `task_updates` 和 `submission_updates`，并校验 task/submission 身份不重复且属于当前 conversation；
- Memory/SQLite 都在同一 commit sequence 写入全部 entry、task checkpoint 和 submission placement/update；任何校验或写入失败都不会留下部分 boundary；
- 每个 queued input 的 entry 保留自己的 `by_task_id`，不会把一批输入错误归属于某个主 task；
- commit projection 会为同一 boundary 发布全部 task progress 和 submission completion 投影；
- SQLite timeline 同一 commit 可携带多个 task checkpoint，`task_history()` 会按 task 选择对应 checkpoint；旧的单 task timeline payload 仍可读取；
- `post_tools`/`final` 的 steer 优先、follow-up 延迟、passive write 排序和 stale write 语义保持不变；
- 保持冷启动约束：`SCHEMA_VERSION = 1`，没有旧 schema migration、fallback 或恢复旁路。

### 30.2 验证

```text
tests/durable/test_inbox_placement.py：35 passed
tests/durable/test_store_contract.py：46 passed
mypy：Success: no issues found in 6 source files
pyrefly：0 errors
ty：All checks passed
ruff：All checks passed
SCHEMA_VERSION = 1
```

### 30.3 与 Pi 的结果

Codey 现在达到 Pi generation boundary 的核心 mutation 语义：一个 boundary 的多项输入不会被拆成多个 durable commit，entry、task checkpoint、submission placement 和 commit cursor 可以一起恢复。Codey 额外保留了 task attempt fencing、权限/审批、provider snapshot、verification 和 completion proof。

仍低于 Pi 或尚未接通的部分：

- native provider 的 queued input 还没有进入同一 native protocol message range；
- tool effect settlement、当前 task checkpoint 和下一 generation checkpoint 还需在真实 provider 执行路径中证明同一事务边界；
- scheduler 的 owner graph、background ownership、abort handler、close join 和持久化 reverse index 仍未完整实现；
- fork-local inbox 还没有 Pi `InboxDoc` 的 snapshot/history 语义；
- Runtime JSONL、AppContext、`operation_state.py` 和 DurableSession 的最终结构卫生审查仍待完成。

## 31. 第二十七段：native provider boundary message range

本段接通上一段尚未完成的 native provider 差距。此前 durable boundary 的返回值只被拼进 text provider 的下一轮 prompt，native provider 会直接把它丢掉。现在 queued user input 作为同一批 tool result 的边界附加消息编码，沿原生 provider 的 `send_tool_results` 路径发送。

### 31.1 已完成

- `ProviderToolResult` 可携带可选的 boundary user content，默认路径和既有 tool result 身份不变；
- Chat Completions 编码为 `tool` 消息后紧接 `user` 消息；
- Responses 编码为 `function_call_output` 后紧接 user `message` item；
- native transport 只把 boundary input 附加在最后一个 tool result 上，保持原有 call/result 配对和单次 provider exchange；
- text provider 继续使用普通 prompt 拼接，native provider 不再依赖 text fallback；
- 保留 protocol window、call ID、attempt fencing、权限和 durable placement 语义。

### 31.2 验证

```text
tests/test_native_boundary_message_range.py：1 passed
native API/chat/Responses、local receipt、native cancel 回归：34 passed
mypy：Success
pyrefly：0 errors
ty：All checks passed
ruff：All checks passed
SCHEMA_VERSION = 1
```

### 31.3 与 Pi 的结果

Codey 已补上 native provider 对 queued input 的 message-range 接线，并且保留了 Pi 没有的 provider window fencing、权限、审批和 effect audit。仍未完成的是：真实 provider 运行中 boundary placement、tool settlement 与下一 checkpoint 的统一 durable transaction 故障注入；scheduler 完整生命周期和 InboxDoc snapshot/history 仍是差距。

## 32. 第二十八段：scheduler close 的 invocation join

本段处理 Pi scheduler 生命周期中的一个明确边界：关闭 scheduler 时，正在运行的 dispatch invocation 必须收到停止信号，并在 close 返回前等待 cooperative worker 收尾。此前 Codey 只设置 scheduler closed flag 并 join 线程，active invocation 没有收到 signal，close 可能超时而留下正在执行的 host callback。

### 32.1 已完成

- `DurableTaskScheduler.close()` 在设置 close barrier 后捕获当前 invocation 快照并 signal 每个 active `TaskInvocation`；
- close 仍不伪造 durable cancellation，task attempt 的 durable checkpoint 保持可恢复，真正的用户取消继续使用 `abort_task()`；
- scheduler thread 在 timeout 内等待 dispatch callback 结束，cooperative callback 可证明 close 返回时已释放；
- signal 发生在 scheduler lock 释放后，避免 worker unwind 与 scheduler 状态锁互相等待；
- 原有 abort 顺序、attempt fencing、waiting reverse wakeup 和 restart recovery 语义保持不变。

### 32.2 验证

```text
test_scheduler.py + test_scheduler_abort_signal.py + test_waiting_reverse_index.py + test_task_ownership_and_finalization.py：21 passed
mypy：Success
pyrefly：0 errors
ty：All checks passed
ruff：All checks passed
SCHEMA_VERSION = 1
```

### 32.3 与 Pi 的结果

Codey 现在在 scheduler close 的 cooperative invocation join 上达到 Pi 的关键生命周期语义，并额外保留 durable attempt recovery 与 fail-closed effect 规则。仍低于 Pi 的 scheduler 部分是持久化 live/reverse index、background ownership、abort handler 的分层调用，以及 completing/finalization 的完整 scheduler kernel 语义。

## 33. 第二十九段：持久化 waiting reverse index

本段补上 scheduler 依赖唤醒的 durable index。此前 `notify(child)` 通过 `session.tasks()` 全量扫描找出等待 child 的 waiter；这在小规模测试中可用，但 scheduler 重启或任务图扩大后仍依赖扫描推导。现在 task checkpoint 的 `waiting_on` 在同一 SQLite 事务中维护 `task_waiters` 关系表，Memory 也使用同语义的反向索引。

### 33.1 已完成

- 冷启动 schema 1 新增 `task_waiters(waited_task_id, waiter_task_id)` 及双向索引；没有 migration、旧库修复或兼容写入路径；
- task 新建和 checkpoint 更新会原子替换该 task 的 waiter edges，task 删除通过外键/内存清理移除边；
- `DurableStore.waiting_dependents()` 成为明确的 durable 读取契约；SQLite 重启后仍能读取相同 waiter 集合；
- scheduler `notify(child)` 直接使用 durable reverse index，只唤醒 `WAITING` waiter；
- Memory/SQLite 的 waiting、settlement、依赖解除和重启行为已由测试覆盖。

### 33.2 验证

```text
test_waiting_reverse_index.py：3 passed
schema/store/scheduler 回归集合：62 passed
mypy：Success
pyrefly：0 errors
ty：All checks passed
ruff：All checks passed
SCHEMA_VERSION = 1
```

### 33.3 与 Pi 的结果

Codey 已在 waiting dependency 的持久化反向唤醒上接近并达到 Pi scheduler 的核心语义，同时保留 task CAS、attempt fencing、未知 effect fail-closed 和权限边界。仍低于 Pi 的 scheduler 生命周期是完整 live invocation index、background ownership、abort handler 分层传播，以及更完整的 completing/finalization 调度图。

## 34. 第三十段：branch 接入唯一 durable mutation line

本段收敛此前仍绕过统一提交入口的 branch 路径。原先 `conversation_ops.branch()` 直接调用 store 专用写入，再发布一个不包含 branch 事实的合成 mutation；这使 branch 的 head、revision、commit cursor 和 projection 不能由同一个 mutation 描述。现在 branch 是 `DurableMutation.branch_entry_id`，Memory/SQLite 的公开 `store.branch()` 只作为窄适配器委托给 `commit()`。

### 34.1 已完成

- `DurableMutation` 明确表达 branch head 重定位，并拒绝与 entry、task、effect、submission、artifact、create/delete 等其他事实变更混合；
- `conversation_ops.branch()` 经过 `DurableSession._commit()`，由同一 mutation line 执行 revision CAS、entry 归属验证、head 更新和 commit 发布；
- Memory/SQLite 都以一次 durable commit 将 branch projection、head 和 revision 原子提交；branch revision 每次递增一次，旧 entries 保留；
- projection 统一为 `conversation_branched`，SSE/commit cursor 可以从 durable commit 重建 branch 事件；
- 两个 backend 的 store contract、branch CAS、context/fork 回归均通过；
- 冷启动约束保持：`SCHEMA_VERSION = 1`，没有旧 branch 写入路径或兼容 fallback。

### 34.2 验证

```text
test_branch_revision_cas.py：3 passed
conversation mutation line + commit events + store contract：62 passed
context + fork ancestry + workflow fork：28 passed
mypy：Success: no issues found in 6 source files
pyrefly：0 errors
ty：All checks passed
ruff：All checks passed
SCHEMA_VERSION = 1
```

### 34.3 与 Pi 的结果

Codey 现在在 branch 的 mutation ownership、CAS、commit sequence 和 SSE projection 上达到 Pi durable 的单一提交语义，并保留自己的 session sealing、权限边界和 task/effect fencing。此前“branch 需要从多个日志或合成事件推断”的差距已消除。

仍低于 Pi 或尚未接通的部分：

- 真实 provider 执行中的 tool settlement、boundary placement 和下一 checkpoint 仍需完整故障注入证明；
- scheduler 的 live invocation index、background ownership、abort handler 分层传播和 completing/finalization 调度图仍未完成；
- fork-local `InboxDoc` snapshot/history、Runtime JSONL 旧路径清理、AppContext/巨型模块最终结构审查仍待进行。

## 35. 第三十一段：scheduler background ownership

本段把 scheduler 的 owner 从“仅靠应用层文件 lease 或 task attempt token 间接推断”收敛为 durable store 的明确生命周期事实。`DurableAppServices` 仍保留已有数据库文件 lease；新增的 store owner 保护低层直接创建多个 `DurableTaskScheduler` 的场景，避免两个 scheduler 同时把同一数据库当作自己的后台调度源。

### 35.1 已完成

- schema 1 新增单行 `scheduler_owners`，只保存 owner identity、process id 和 claim 时间；冷启动直接创建，不增加旧 schema migration；
- Memory/SQLite 都提供同一 `acquire_scheduler_owner()` / `release_scheduler_owner()` 契约；
- `DurableTaskScheduler.start()` 在 recovery、finalization 和 notify 前取得 owner，取得失败不会启动后台线程；
- `close()` 只有在 invocation/thread 完整退出后释放 owner，超时返回 `False` 时仍保留 owner，允许调用方继续 close；
- 同一 owner 可幂等重入，live PID 的其他 owner 被拒绝，已不存在的 PID 可接管；
- task attempt token 仍负责任务级 fencing，scheduler owner 负责后台调度器级 fencing，两层语义互不替代；
- 不影响 AppServices 已有 OS-backed database lease，也没有恢复旧 scheduler 写入路径或第二事实源。

### 35.2 验证

```text
test_scheduler_ownership.py：4 个场景分别通过（单 owner、close release、dead PID reclaim、Memory/SQLite contract）
scheduler + abort + waiting reverse index + services lifecycle：19 passed
schema/store contract：49 passed
mypy：Success
pyrefly：0 errors
ty：All checks passed
ruff：All checks passed
SCHEMA_VERSION = 1
```

注：Windows pytest 在连续创建临时目录时偶发卡在 pytest 自身 `tmp_path` 路径解析；单个 ownership 测试和同一批实际断言均已独立运行通过，这属于测试运行环境的临时目录问题，不是生产 scheduler 失败。没有为迁就该脚本向生产代码加入 fallback。

### 35.3 与 Pi 的结果

Codey 现在在后台 scheduler ownership 上具备明确的 durable claim/release/reclaim 边界，补上了此前“只能从 AppServices lease 和 task attempt 间接推断”的缺口，并保留更严格的 task CAS、attempt fencing、未知 effect fail-closed、权限和审批语义。相对 Pi 仍未完全达到的部分是 abort handler 的独立 invocation、完整 background boundary/task graph index，以及真实 provider generation 的 settlement/placement/checkpoint 故障事务证明。

## 36. 第三十二段：abort handler invocation

本段补上 scheduler 取消生命周期中剩余的一个明确边界。此前 `abort_task()` 只写 durable cancellation barrier 并 signal 普通 `TaskInvocation`；host 没有独立的 abort cleanup invocation，无法在普通 work unwind 后执行专门的取消收尾。

### 36.1 已完成

- `DurableTaskScheduler` 接受窄的可选 `abort_handler`，`DurableAppServices.open()` 将该依赖显式传入；
- `abort_task()` 严格先调用 `DurableSession.request_cancel()`，再 signal 普通 invocation；
- 只有存在 active 普通 invocation 且配置了 handler 时才创建独立的 `AbortInvocation`，不把取消过程伪造为新的 task 或 entry；
- handler 收到的 `TaskCheckpoint` 已包含 `cancel_requested` barrier，并且 scheduler 等待 handler 完成后才返回；
- abort handler 异常不会撤销已经提交的 durable cancellation，异常向调用方传播；
- scheduler close 会 signal 并等待未完成的 abort invocation，再释放 scheduler owner；
- `AbortInvocation` 仍是 volatile lifecycle 对象，恢复事实继续只来自 task checkpoint/effect settlement。

### 36.2 验证

```text
test_scheduler_abort_handler.py：1 passed
scheduler + abort signal + ownership 回归：15 passed
mypy：Success
pyrefly：0 errors
ty：All checks passed
ruff：All checks passed
SCHEMA_VERSION = 1
```

### 36.3 与 Pi 的结果

Codey 现在具备 durable abort mark、普通 invocation signal、独立 abort handler 和 close 等待的完整顺序，并保留 task attempt fencing、取消传播和未知 effect fail-closed。scheduler 生命周期已不再只有“下次扫描才处理取消”的隐式路径。

仍低于 Pi 或尚未完成的部分：

- owner graph 的局部 finalization/fail-fast index 仍主要由 durable task 查询派生；
- abort handler 尚未在真实 provider/shell host 中接入具体清理动作，当前先锁定 scheduler contract；
- provider generation 的 tool settlement、boundary placement、下一 checkpoint 仍需故障注入证明；
- InboxDoc snapshot/history、Runtime JSONL 清理、AppContext/巨型模块结构审查仍待完成。

## 37. 第三十三段：owner graph durable reverse index

本段收敛 scheduler kernel 的另一处全量扫描差距。此前 `DurableSession._owned_descendants()` 每次取消或 completion finalization 都调用 `store.tasks()`，临时构造 owner-to-child 图；这让 owner 传播依赖扫描推导，重启后也没有明确的 durable graph 查询入口。

### 37.1 已完成

- schema 1 新增 `task_owners(owner_task_id, child_task_id)` 及双向索引；
- Memory/SQLite 在 task 新建和 checkpoint 更新的同一事务中维护 owner edge；
- SQLite 重启后可直接读取 `owned_dependents()`，删除 task 时由外键清理边；Memory 使用同语义的 reverse index；
- `DurableStore.owned_dependents()` 成为明确窄契约；`_owned_descendants()` 只沿 immediate owner edges 递归，不再全量读取所有 tasks；
- parent cancellation cascade、completion hold/finalization、waiting join 的既有行为保持不变；
- 冷启动仍是 `SCHEMA_VERSION = 1`，没有 migration、旧 graph fallback 或重复的 runtime owner projection。

### 37.2 验证

```text
test_owner_reverse_index.py：2 passed
task ownership/finalization + waiting reverse index + store contract：56 passed
mypy：Success: no issues found in 5 source files
pyrefly：0 errors
ty：All checks passed
ruff：All checks passed
SCHEMA_VERSION = 1
```

### 37.3 与 Pi 的结果

Codey 现在在 owner/downward graph 的 durable reverse lookup 上达到 Pi scheduler kernel 的关键语义，并保留 task ownership cycle 校验、CAS、attempt fencing、取消屏障和 fail-closed effect 规则。取消传播与 completion finalization 不再依赖全量 task scan。

仍低于 Pi 或尚未完成的部分：

- finalization/fail-fast 的专用候选集合和 commit-driven 局部 reconcile 仍可继续收敛；
- provider generation 的 settlement、boundary placement、下一 checkpoint 故障事务仍未完成；
- abort handler 尚未在真实 shell/provider host 中接入；
- InboxDoc snapshot/history、Runtime JSONL 清理、AppContext/巨型模块结构审查仍待进行。

## 38. 第三十四段：commit-driven local finalization reconcile

本段继续收敛 owner/waiting graph 的调度语义。此前 reverse index 已经存在，但 scheduler 每次 reconcile 仍调用无参数 `reconcile_finalization()`，内部再次扫描所有 tasks；因此索引只减少了唤醒扫描，未真正限制状态重算范围。

### 38.1 已完成

- `reconcile_waiting_tasks(task_id)` 支持针对单个 durable join 候选重算，依赖 child 通过窄的 `store.task()` 查询读取；
- `reconcile_finalization(task_ids)` 支持候选集合，只有显式无参数启动恢复/管理操作才执行全量 reconcile；
- scheduler 从 durable pending checkpoints、waiting reverse index 和 owner edge 收集 finalization 候选，然后只提交这些 task 的 waiting/finalization 状态转换；
- child settlement 触发 owner parent 的局部 finalization，completion hold 不再依赖全量 task scan；
- 既有全量入口仍保留给冷启动恢复和显式 reconcile，生产运行路径使用候选集合；
- 故障注入测试禁止 local reconcile 调用 `store.tasks()`，仍能完成 parent completion；
- `SCHEMA_VERSION = 1` 保持不变，没有新增投影、兼容分支或旧恢复路径。

### 38.2 验证

```text
local reconcile 故障测：1 passed
ownership/finalization + scheduler + waiting reverse index：22 passed
mypy：Success
pyrefly：0 errors
ty：All checks passed
ruff：All checks passed
SCHEMA_VERSION = 1
```

### 38.3 与 Pi 的结果

Codey 现在不仅拥有 Pi 风格的 owner/waiting reverse index，还让实际 scheduler 状态重算遵循 commit-driven local candidate 语义；parent cancellation、completion hold 和 fail-fast join 的受影响范围更容易证明。Codey 继续保留 Pi 没有的 attempt fencing、权限/审批、未知 effect fail-closed 和 completion proof。

仍低于 Pi 或尚未完成的部分：

- finalization/fail-fast 候选目前由 scheduler durable snapshot 组织，尚未形成独立持久化 finalization queue；
- provider generation 的 settlement、boundary placement、下一 checkpoint 故障事务仍待完成；
- abort handler 尚未在真实 shell/provider host 中接入；
- InboxDoc snapshot/history、Runtime JSONL 清理、AppContext/巨型模块结构审查仍待进行。


