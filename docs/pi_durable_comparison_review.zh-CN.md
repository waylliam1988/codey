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

