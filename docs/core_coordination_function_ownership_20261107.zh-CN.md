# 核心协调层：函数所有权与迁移合同

本文对应五阶段重构，按当前实现说明函数读取、写入、锁、事务、事件、调用方、恢复和所有者。
进度及验证结果见 [进度文档](core_coordination_refactor_progress_20261107.zh-CN.md)。
下面的合同编号用于末尾逐函数索引。一个函数继承其所属合同；专门说明优先于共同说明。
索引记录当前入口和实际委托方向，不把兼容代理算作第二个状态所有者。

## 依赖与权威边界

AppContext 和 app_runtime 是组合根：可创建具体实现、连接回调和注册 UI 动作。
领域操作依赖 SessionKernel；应用业务依赖具名能力；前端事件协调依赖具名 API 切片。
不新增通用服务定位器、通用事件总线或每领域独立数据库连接。

- durable task/submission/approval/effect 是执行与恢复事实。
- RunRegistry 是当前进程的运行槽位与 stop/provider 绑定；不能替代 durable 状态。
- 审批 registry、JSONL、EventBus、SSE、浏览器 terminalRuns 和 message indexes 是投影。
- SessionStateStore 持有浏览器会话/项目/消息状态，沿用 UiState 的 metadata 持久化。
- DOM、焦点、阅读位置和 disclosure 由视图拥有；不决定任务是否完成或是否允许执行。

## 同步与事务的共同合同

同一个 SessionKernel 拥有 store、一个 RLock、失败/关闭状态、attempt ContextVar 和提交订阅。
所有领域 operation_line 和查询 QueryLine 进入这一条同步边界。读取、推导、提交一起受保护，
不能只给 commit 加锁。该锁不提供跨进程保护；store 事务中的 revision/status/attempt CAS
继续处理竞争写入。查询在本进程内串行并不等于跨进程多次读取天然具有数据库快照隔离。

一个持锁操作可以提交多笔 mutation。任务完成、取消传播、恢复扫描本来就有可恢复阶段，
本重构保留这些阶段；不能以“统一内核”为理由强行合并。每笔 mutation 由 store 原子提交。

spawn gate 和应用协调锁的共同顺序为 gate → app lock；scheduler abort 在释放它们后执行。
DurableEventBridge 在 app lock 被占用时延迟发布，flush 先取出队列再在锁外发送。
kernel 内部订阅与外部投影订阅的失败语义不同，迁移不擅自挪动回调或调整顺序。

## 应用函数合同

| 合同 | 读取 | 写入 | 锁 | durable 事务 | 事件 | 调用方 | 重启恢复 | 唯一所有者 |
|---|---|---|---|---|---|---|---|---|
| A0 装配/资源访问 | state_home、配置、已创建服务 | 依赖引用、延迟资源缓存 | 延迟 store/cache 使用原 app lock | 不新增事务，连接原服务 | 只连接回调 | server、service、headless 启动 | 具体 durable 服务恢复事实 | AppContext，资源关闭归 AppLifecycle |
| A1 运行槽位 | RunRegistry、provider 绑定、stop flag | 预留/运行/释放、API selection | registry 自有锁；admission_line 为 gate → app lock；spawn_line 只持 gate | 不自行提交任务事实 | release 后唤醒输入 | task_submit、shell executor、abort handler | 槽位重建，任务依 durable | TaskRuntimeCoordinator / RunRegistry |
| A2 停止/终结 | 当前 run、durable checkpoint、关闭状态 | stop、审批失效、队列暂停、运行终结 | 原 gate/app lock 顺序；scheduler abort 在锁外 | 调用原审批/调度取消操作，不合并其事务 | terminal/shell result，最后 flush | Stop 路由、worker、scheduler abort、shutdown | durable barrier 与待决审批保留原恢复语义 | TaskLifecycleCoordinator |
| A3 审批协调 | durable approval/task、内存投影、命令 artifact、selection | 审批投影、durable 转换、ticket | 原 app lock；执行 claim 的调用方持 spawn gate | claim+effect intent 同一笔；审计后写 | 交回上层按原顺序发布 | approval workflow、HTTP、shell service | durable 记录重建；JSONL 不授予权限 | ShellApprovalCoordinator |
| A4 durable 事件桥 | 已成功提交的 CommitRecord | 延迟事件队列 | 保留原协调锁/提交线关系，flush 发送在 app lock 外 | 不写 durable | commit payload → app emit | session commit observer，生命周期 flush | 浏览器游标与 durable commits 重放 | DurableEventBridge |
| A5 运行投影 | registry、审批 workflow、durable 任务版本、research 可恢复项 | payload、最后 shell result 的规范化投影 | 研究/审批快照沿用 app lock；registry 自有同步 | 查询/原有 display 记录，不创造执行许可 | shell result 经 emit | /api/state、shell workflow、worker | durable/task 版本为权威 | RunStateProjection |
| A6 会话生命周期 | 会话 registry、durable snapshots、provider 和 UI 状态 | 保存快照、删除各投影、收集清理错误 | app/UI 原有锁；各 store 自有同步 | durable 会话操作原边界；跨 store 清理非一个大事务 | 审批过期结果与延迟事件 | new_chat、会话上下文、快照回调 | durable 快照；删除失败按返回结果处理 | ConversationCoordinator |
| A7 项目写租约 | 当前租约、SnapshotStore | FileLease acquire/release | 原 app lock 与底层文件租约 | 无 | 无 | task workspace、shutdown | OS/文件租约及现有 revision 语义 | WorkspaceLeaseCoordinator |
| A8 关闭/drain | close flags、执行计数、worker/service 状态 | 停止准入、执行计数、有序释放、释放成功后清引用 | shell 计数 Condition；Stop 复用同一个 gate | 不另建事务；先 drain 才关 store | 通过既有停止流程 | service/server close、shell execute finally | 未应答 durable 决策由下次启动恢复 | AppLifecycle / ResourceSlot |
| A9 应用事件发布 | run scope、task revision、conversation admission、历史显示 | 规范化 payload、历史显示、输入完成投影 | 补 scope 时原 app lock；durable 内部仍走 kernel | 只有既有 prepare/display 操作；不把发送当提交 | EventBus、附加 hook | app services、workers、DurableEventBridge | durable 历史与版本，SSE 可重建 | AppContext 发布管线 / ConversationRuntime |
| A10 专用 UI/资源适配 | provider、research、ghost、UI store、change tracking | 对应专用组件状态 | 对应组件既有锁 | 对应组件的原事务 | 由组件原合同决定 | API、后台回调、任务装配 | 由对应专用 store/服务恢复 | 索引中实际委托的专用组件 |

### 需要一起阅读的应用函数

- `reserve_run/replace_reserved_run/start_run/release_run` 保持会话串行槽位规则。
  `release_run` 先释放 registry 再唤醒输入；不把前置 stop 检查搬到 reserve 外面。
- `finish_run/request_stop/expire_pending_shell_approvals/expire_stale_shell_approvals/abort_approvals`
  属 A2，审批资源由 A3 实施。Scoped stop 找不到任务时不能退化成全局 Stop。
- `add_pending/claim/claim_locked/claim_ticket/resolve` 属 A3；内存卡片只能在 durable 准入成功后消费。
- `publish_commit/flush` 属 A4；外部事件只来自成功提交。发布失败不能撤销已成功 durable commit。
- `payload/pending_ui_event/record_shell_result` 属 A5；不从 busy/cache 缺失推导任务终态。
- `load_snapshot/save_snapshot/forget` 属 A6。跨投影删除会继续尝试清理并返回分项失败，不能伪装跨库原子性。
- `begin_shell_execution/end_shell_execution/wait_shell_executions/close` 属 A8；计数覆盖进程退出后的输出结算窗口。
  ResourceSlot 释放失败保留原引用供重试，不能提前清空引用。
- `emit_browser_event` 是 A9 的观察性出口：不调用普通 emit 的内部事实/历史完成处理。

审批恢复还额外使用一个窄的 `ApprovalRecoveryContext`：它只提供 durable approval/task
查询、managed artifact 读取、ApprovalRegistry 投影和审计追加能力。`claim_pending_shell_approval_locked`
和 `restore_pending_shell_approvals` 通过这个 capability 读取权威 durable 事实；审批 JSONL
只做审计输出。测试替身必须提供真实的 `TaskRuntimeCoordinator`/spawn gate 所有者，不能只
伪造 `AppContext._shell_spawn_gate` 私有字段。

## Durable 函数合同

| 合同 | 读取 | 写入 | 锁 | durable 事务 | 事件 | 调用方 | 重启恢复 | 唯一所有者 |
|---|---|---|---|---|---|---|---|---|
| K 内核 | store、当前 attempt、失败/关闭状态 | mutation 提交、attempt binding、订阅与关闭状态 | 唯一 RLock；execution 只在进入时校验，不跨外部工作持锁 | store.commit 校验 conversation/task/status/attempt 并原子写入 | 成功提交后按原顺序通知订阅 | 所有 operation/query、scheduler、runtime checkpoint | store 重开，attempt fencing 拒绝旧 worker | SessionKernel |
| Qc 会话查询 | head/entries/history/inbox/context/snapshot | 只构造返回投影 | 同一个 QueryLine | 无写事务；不另开连接 | 无 | history API、上下文、UI snapshot | durable entries 与 snapshots | ConversationQueryFacade |
| Qt 任务查询 | checkpoint/timeline/effects/artifacts/approval | 只构造返回投影 | 同一个 QueryLine；task_facts 联合读 | 无写事务 | 无 | scheduler、runtime、approval、history | durable task/effect facts | TaskQueryFacade |
| Dc 会话写 | 会话 revision、head、entries | 创建、显示消息、分支、fork、删除、snapshot | operation_line → kernel | 各操作原 mutation；fork helper 保留原边界 | kernel 发布 commit | facade、历史编辑、应用上下文 | committed conversation history | ConversationOperations |
| De 效果与 artifact | task、effect、artifact ownership、pending ids | intent、settlement、artifact、checkpoint | operation_line → kernel | intent/checkpoint 或 settlement/checkpoint/artifact 原子组 | kernel commit | runtime effects、approved shell、recovery | unknown outcome 显式处理，不臆测执行成功 | EffectOperations |
| Da 审批原子组 | approval、task、effect、conversation head | 审批转换、effect intent/settlement、工具结果、任务唤醒 | operation_line → kernel | claim+intent+checkpoint；settle+tool results+approval+wakeup 各一笔 | kernel commit，审计在外部 | ShellApprovalCoordinator/workflow、runtime adapters | durable approvals；不重跑 claimed 但未确认的命令 | ApprovalOperations |
| Dt 任务状态机 | task/submission、ownership tree、effects、policy/workflow | task phase/status、attempt、取消屏障、completion/join 状态 | 整条 operation_line 共用 RLock | 每个原 mutation 不变；完成/子树取消/扫描允许多次可恢复提交 | kernel commit，scheduler observer 唤醒 | scheduler、runtime、admission、recovery | recover_scheduler_state、waiting/finalization reconciliation | TaskLifecycleOperations |
| Ds 准入与边界 | submission 顺序、head/revision、活跃 task、已推导 mutation | admission、输入编辑/放置、边界 receipt、entries/task/submission 更新 | operation_line；merge 被外围 transact_task 包裹 | boundary 与 provider settlement 继续合入同一 mutation | kernel commit | provider boundary、input queue、task_submit、历史写入 | durable inbox 与 boundary receipts | SubmissionOperations / admission helpers |

### 不可拆开的 durable 原子组

`claim_approval_and_begin_effect`：校验源任务、expected approval status、shell pending intent；
更新审批、effect 和 checkpoint 一次提交。重复 claim 只在相同 effect 身份已归该审批时按原合同处理。
数据库提交与 OS Popen 不是跨系统原子事务，提交后仍依赖原执行和 unknown-outcome 恢复。

`settle_approval`：effect settlement、工具结果 entries、approval continuation 和原任务 PENDING
一起提交，不新建第二个任务。拒绝和已处理 continuation 保留原分支。

`settle_effect`：checkpoint pending-effect 状态、effect receipt 和 artifact 继续一起提交。
`settle_unknown_effect` 表达显式不确定结果，不以失败终结删除未确认执行记录。

`advance_task/start_task/finish_task`：答案 entry 和 submission outcome 保持原子对应。
`complete_task/_hold_completion/reconcile_finalization` 共同维护子任务排空；不得分散状态机。
`request_cancel/cancel_task/_cancel_terminal` 分别表达 durable 停止屏障和收尾后的终结，不能合并成设置 flag。
`wait_task/reconcile_waiting_tasks` 保留 WAITING/PENDING、join policy 和已回答问题的恢复行为。
`claim_task/unclaim_task/recover_scheduler_state/execution` 保留 attempt 身份与失效规则。
`record_task_policy/record_reviewer_selection/_record_immutable_task_state` 保留首次捕获后的不可变性。

`place_boundary_inputs/commit_generation_boundary/merge_generation_boundary/_prepare_boundary_mutation/
_prepare_boundary_placement/_prepare_boundary_write/_place_write_submission/_settle_stale_write`
共同维护输入顺序与历史父子链。merge 只合并 mutation；外围 checkpoint `transact_task` 最终提交，
不能改成“先放置输入，再单独提交 provider 结算”。

`transact_task` 的 derive 不启动进程或网络工作，读取 checkpoint/effects/revision 后验证推导结果。
`_publish_commit` 的内部 observer 失败会封闭内核；外部投影 listener 失败记录日志，不撤销已提交事实。
`_fail/close` 的状态对全部 facade 同时生效，直接调用领域对象也不能绕过。

## 前端函数合同

| 合同 | 读取 | 写入 | 锁/顺序 | durable 事务 | 事件/UI | 调用方 | 重启/重连恢复 | 唯一所有者 |
|---|---|---|---|---|---|---|---|---|
| F0 装配与 UI 绑定 | DOM、state/projection、已有模块 API | 连接依赖、事件监听、按钮/视图状态 | 浏览器事件循环；异步完成检查 owner scope | 不直接写 checkpoint | 调用专用视图/业务入口 | boot、DOM callbacks、test bridge | UiState metadata + 服务端历史/快照 | app_runtime 组合根及其视图适配 |
| Fs 会话/项目状态 | sessions/projects/activeId/provider、服务端删除响应 | 会话/项目、消息、索引、UI metadata | 同步变更后 invalidation；异步删除确认后基于当前状态处理 | 通过既有 HTTP 操作；不建立客户端事务权威 | 精确 session/project/message invalidation | UI 命令、RunEventCoordinator、Requests 兼容入口 | UiState 与 durable history；消息不持久化成另一份执行事实 | SessionStateStore |
| Fm 消息写入 | queued input/request/tool/shell/run/stream 身份 | 去重、合并、request status 前插入、消息索引 | store 先写，再视图更新；既有 stream 身份恢复顺序 | 无独立 durable 写 | first/append/replace/drop invalidation | 事件协调、composer、request owner | durable answer identity 与 history refresh | SessionStateStore.addMessage/replaceMessage/dropMessages |
| Fv 增量视图 | invalidation、active scope、挂载 DOM、消息 | DOM 节点、滚动与视图同步 | 后台会话消息不重绘当前 transcript | 无 | 回复节点复用、tool/approval 替换、sidebar/composer 更新 | store invalidation、组合层 | 会话 view snapshot；DOM 不授予执行权限 | SessionStateViews / ConversationUI |
| Fl 生命周期投影 | task revision、event sequence、source、最新 request attempt、fork 接受范围 | disposable RunProjection、terminalRuns、request 展示状态 | 先接受 transition，再派生写入；snapshot barrier 保留 | 无 | 生命周期消息、状态栏/composer invalidation | task events、snapshot、HTTP admission、history task projection | snapshot 权威修正；缓存缺失不代表完成 | RunEventCoordinator + RunProjection/RunLifecycleProjection |
| Fe 事件投影 | decoded event、branch/run/request 接受条件 | stream buffer、消息/审批/队列/问题投影 | 保留 inventory/commit/stream 特殊前缀；其余业务事件前 flush stream；terminal 先 lifecycle | 无 | 调用 store 和具名 owner；不操作 DOM | SSE 重放、last events、queued input terminal | 按 revision/sequence/identity 重放去重 | RunEventCoordinator |
| Ft 传输恢复 | EventSource、database_id/commit_seq、event id、state response | 连接、cursor、快照期间缓冲、重连状态 | 先 snapshot 再重放缓冲；处理失败不确认缺口 | HTTP 接受事实，不另建任务状态机 | 交给协调器应用事件/快照 | boot、resync、HTTP response handoff | database 身份变化和 commit gap 按既有规则重取 | sse.ts，协调器仅提供具名适配 |
| Fr 专用 UI 命令 | research/provider/workspace、当前 scope | drawer/tab、provider 选择、界面动作或 HTTP 结果投影 | 异步回调保留原 owner-session 判断 | 交给原 API/store | 专用视图刷新 | DOM callbacks、终结事件适配 | 对应模块与服务端状态 | 已有 ResearchRuns/Composer/ProviderUI/Workspace 等 |

### 前端函数的顺序与特殊情况

- `applyLifecycle` 同时过滤 branch、旧 request attempt 和版本；只有 transition 接受后才更新
  terminalRuns、request lifecycle 与展示消息。terminalRuns 是有限派生索引。
- `handleReplyStream` 只更新被接受且仍活跃 run 的流；`handleServerEvent` 保留允许晚到的
  shell result/final reply 等事件，不把所有事件统一要求 active。
- `applyRunState/projectTaskStates` 先处理 task states 与 owner 集合，再处理审批和末次事件。
  只有 follow-up snapshot 带 task_states 且未确认 admission 时才能撤回临时 HTTP admission；
  已确认任务事件不能因快照缺项被当成结束。
- `observeQueuedInput` 保留 consumed 刷新历史、已代表输入去重、failed/unconfirmed request 状态
  和 completed terminal 路由；`observeUserQuestion` 保留 question identity 与 revision 展示键。
- `addMessage` 拥有 queued 用户消息去重、shell 结果合并和最终答案/stream 身份；挂载身份仅由
  视图查询提供，新增消息及索引始终由 store 写入。Requests 的公开追加入口只代理同一操作。
- `replaceMessage/dropMessages` 保留事件水位和索引更新语义；不把单次渲染作为业务执行判断。
- `switchSession/deleteSession/clearMessages/deleteProject` 保留 UiState 接受服务端响应和失败处理。
  多会话删除不是跨 HTTP 请求事务，本重构不改变部分失败策略。
- `scrollToMessage/captureView/readingAnchor` 在跳转未完成时保存请求的语义锚点及目标滚动位置，
  防止切换会话捕获动画中间位置；用户主动滚动和 scrollend 清理临时 jump。
- `boot` 负责模块初始化、视图绑定、恢复 UI、刷新已有问题/队列并连接 SSE；业务判断归上述 owner。

## 逐函数入口索引

下表列出三个原核心文件与新增 owner 的具名函数。合同列引用上面的全部八项责任；
箭头表示实际委托，空缺委托的兼容入口按合同中的所有者解释。回调/lambda 继承其装配点合同。

### codey/app/context.py

| 函数/入口 | 合同 | 实际委托或责任 |
|---|---|---|
| [`_close_if_discarded`](../codey/app/context.py#L70) | A0 | 释放装配阶段的双重检查失败资源 |
| [`AppContext.__init__`](../codey/app/context.py#L83) | A0 | 装配共享依赖；不创建第二份状态所有者 |
| [`AppContext._dispatch_durable_task`](../codey/app/context.py#L278) | A1 | scheduler dispatch 适配到任务提交协调 |
| [`AppContext._abort_durable_task`](../codey/app/context.py#L283) | A2 | scheduler abort 适配到任务资源终止协调 |
| [`AppContext.register_active_provider`](../codey/app/context.py#L286) | A1 | → `self.task_runtime.register_active_provider` |
| [`AppContext.unregister_active_provider`](../codey/app/context.py#L290) | A1 | → `self.task_runtime.unregister_active_provider` |
| [`AppContext._publish_durable_commit`](../codey/app/context.py#L294) | A4 | → `self.durable_event_bridge.publish_commit` |
| [`AppContext._flush_deferred_durable_events`](../codey/app/context.py#L297) | A4 | → `self.durable_event_bridge.flush` |
| [`AppContext._load_durable_conversation_snapshot`](../codey/app/context.py#L300) | A6 | → `self.conversation_coordinator.load_snapshot` |
| [`AppContext._save_durable_conversation_snapshot`](../codey/app/context.py#L305) | A6 | → `self.conversation_coordinator.save_snapshot` |
| [`AppContext.acquire_project_writer`](../codey/app/context.py#L310) | A7 | → `self.workspace_leases.acquire` |
| [`AppContext.release_project_writer`](../codey/app/context.py#L313) | A7 | → `self.workspace_leases.release` |
| [`AppContext.knowledge_store`](../codey/app/context.py#L318) | A0 | 按 A0 合同实施；完整顺序见函数体 |
| [`AppContext.knowledge_store`](../codey/app/context.py#L338) | A0 | 按 A0 合同实施；完整顺序见函数体 |
| [`AppContext.build_task_submission_stores`](../codey/app/context.py#L345) | A0 | 按 A0 合同实施；完整顺序见函数体 |
| [`AppContext.task_submission_stores`](../codey/app/context.py#L363) | A0 | 按 A0 合同实施；完整顺序见函数体 |
| [`AppContext.self_repair`](../codey/app/context.py#L368) | A0 | 按 A0 合同实施；完整顺序见函数体 |
| [`AppContext.self_repair`](../codey/app/context.py#L388) | A0 | 按 A0 合同实施；完整顺序见函数体 |
| [`AppContext.load_ui_state`](../codey/app/context.py#L392) | A10 | 通过 UI store 读取带 metadata 的状态 |
| [`AppContext.save_ui_state`](../codey/app/context.py#L396) | A10 | 通过 UI store 执行 revision/CAS 保存 |
| [`AppContext.change_tracker_for`](../codey/app/context.py#L401) | A10 | 按 A10 合同实施；完整顺序见函数体 |
| [`AppContext.reserve_run`](../codey/app/context.py#L412) | A1 | → `self.task_runtime.reserve_run` |
| [`AppContext.active_run_for`](../codey/app/context.py#L443) | A1 | → `self.task_runtime.active_run_for` |
| [`AppContext.current_run`](../codey/app/context.py#L456) | A1 | → `self.task_runtime.current_run` |
| [`AppContext.active_runs`](../codey/app/context.py#L459) | A1 | → `self.task_runtime.active_runs` |
| [`AppContext.stop_flag_for`](../codey/app/context.py#L462) | A1 | → `self.task_runtime.stop_flag_for` |
| [`AppContext.is_busy`](../codey/app/context.py#L465) | A1 | → `self.task_runtime.is_busy` |
| [`AppContext.run_status`](../codey/app/context.py#L468) | A1 | → `self.task_runtime.run_status` |
| [`AppContext.replace_reserved_run`](../codey/app/context.py#L471) | A1 | → `self.task_runtime.replace_reserved_run` |
| [`AppContext.has_active_run_for_project`](../codey/app/context.py#L474) | A1 | → `self.task_runtime.active_run_for(project=...)` |
| [`AppContext.start_run`](../codey/app/context.py#L478) | A1 | → `self.task_runtime.start_run` |
| [`AppContext.set_run_status`](../codey/app/context.py#L481) | A1 | → `self.task_runtime.set_run_status` |
| [`AppContext.switch_run_provider`](../codey/app/context.py#L484) | A1 | → `self.task_runtime.switch_run_provider` |
| [`AppContext.release_run`](../codey/app/context.py#L487) | A1 | → `self.task_runtime.release_run` |
| [`AppContext.finish_run`](../codey/app/context.py#L490) | A2 | → `self.task_lifecycle.finish_run` |
| [`AppContext.record_shell_result`](../codey/app/context.py#L493) | A5 | → `self.run_state_projection.record_shell_result` |
| [`AppContext.request_stop`](../codey/app/context.py#L496) | A2 | → `self.task_lifecycle.request_stop` |
| [`AppContext.expire_pending_shell_approvals`](../codey/app/context.py#L509) | A2 | → `self.task_lifecycle.expire_pending_shell_approvals` |
| [`AppContext.expire_stale_shell_approvals`](../codey/app/context.py#L519) | A2 | → `self.task_lifecycle.expire_stale_shell_approvals` |
| [`AppContext._expire_shell_results_locked`](../codey/app/context.py#L524) | A3 | → `self.shell_approval_coordinator.expire_locked` |
| [`AppContext.add_pending_shell_approval`](../codey/app/context.py#L532) | A3 | → `self.shell_approval_coordinator.add_pending` |
| [`AppContext.record_shell_approval_fact`](../codey/app/context.py#L535) | A3 | → `self.shell_approval_coordinator.record_fact` |
| [`AppContext._commit_durable_shell_approval`](../codey/app/context.py#L538) | A3 | → `self.shell_approval_coordinator.commit_fact` |
| [`AppContext._claim_pending_shell_approval_locked`](../codey/app/context.py#L541) | A3 | → `self.shell_approval_coordinator.claim_locked` |
| [`AppContext.claim_pending_shell_approval`](../codey/app/context.py#L547) | A3 | → `self.shell_approval_coordinator.claim` |
| [`AppContext.resolve_shell_approval`](../codey/app/context.py#L553) | A3 | → `self.shell_approval_coordinator.resolve` |
| [`AppContext.approval_generation`](../codey/app/context.py#L557) | A3 | → `self.shell_approval_coordinator.generation` |
| [`AppContext.claim_shell_ticket`](../codey/app/context.py#L560) | A3 | → `self.shell_approval_coordinator.claim_ticket` |
| [`AppContext.pending_shell_approvals`](../codey/app/context.py#L567) | A3 | → `self.shell_approval_coordinator.pending` |
| [`AppContext.add_pending_teach`](../codey/app/context.py#L571) | A10 | 按 A10 合同实施；完整顺序见函数体 |
| [`AppContext.pop_pending_teach`](../codey/app/context.py#L575) | A10 | 按 A10 合同实施；完整顺序见函数体 |
| [`AppContext.resume_pending_teach`](../codey/app/context.py#L579) | A10 | 按 A10 合同实施；完整顺序见函数体 |
| [`AppContext.cancel_pending_teach`](../codey/app/context.py#L583) | A10 | 按 A10 合同实施；完整顺序见函数体 |
| [`AppContext.pending_teach_requests`](../codey/app/context.py#L587) | A10 | 按 A10 合同实施；完整顺序见函数体 |
| [`AppContext.record_research_changes`](../codey/app/context.py#L591) | A10 | 按 A10 合同实施；完整顺序见函数体 |
| [`AppContext.restore_research_changes`](../codey/app/context.py#L611) | A10 | 按 A10 合同实施；完整顺序见函数体 |
| [`AppContext._schedule_knowledge_rebuild`](../codey/app/context.py#L630) | A10 | 按 A10 合同实施；完整顺序见函数体 |
| [`AppContext.kick_ghost_sleep`](../codey/app/context.py#L633) | A10 | 按 A10 合同实施；完整顺序见函数体 |
| [`AppContext._run_ghost_sleep_once`](../codey/app/context.py#L667) | A10 | 按 A10 合同实施；完整顺序见函数体 |
| [`AppContext.wait_for_ghost_sleep`](../codey/app/context.py#L686) | A10 | 按 A10 合同实施；完整顺序见函数体 |
| [`AppContext.run_state_payload`](../codey/app/context.py#L689) | A5 | → `self.run_state_projection.payload` |
| [`AppContext._pending_ui_event`](../codey/app/context.py#L692) | A5 | → `self.run_state_projection.pending_ui_event` |
| [`AppContext.emit`](../codey/app/context.py#L695) | A9 | 按 A9 合同实施；完整顺序见函数体 |
| [`AppContext.emit_browser_event`](../codey/app/context.py#L724) | A9 | 按 A9 合同实施；完整顺序见函数体 |
| [`AppContext._on_event_emitted`](../codey/app/context.py#L736) | A9 | 按 A9 合同实施；完整顺序见函数体 |
| [`AppContext._event_with_run_fields`](../codey/app/context.py#L739) | A9 | 按 A9 合同实施；完整顺序见函数体 |
| [`AppContext.subscribe`](../codey/app/context.py#L768) | A9 | 按 A9 合同实施；完整顺序见函数体 |
| [`AppContext.unsubscribe`](../codey/app/context.py#L771) | A9 | 按 A9 合同实施；完整顺序见函数体 |
| [`AppContext.replay_events_after`](../codey/app/context.py#L774) | A9 | 按 A9 合同实施；完整顺序见函数体 |
| [`AppContext.get_provider`](../codey/app/context.py#L785) | A10 | 按 A10 合同实施；完整顺序见函数体 |
| [`AppContext.provider_failover_order`](../codey/app/context.py#L791) | A10 | 按 A10 合同实施；完整顺序见函数体 |
| [`AppContext.conversation_for`](../codey/app/context.py#L794) | A6 | → `self.conversation_coordinator.conversation_for` |
| [`AppContext.forget_conversation`](../codey/app/context.py#L797) | A6 | → `self.conversation_coordinator.forget` |
| [`AppContext.provider_session_changed`](../codey/app/context.py#L803) | A10 | 按 A10 合同实施；完整顺序见函数体 |
| [`AppContext.set_provider_session`](../codey/app/context.py#L807) | A10 | 按 A10 合同实施；完整顺序见函数体 |
| [`AppContext.shutdown_requested`](../codey/app/context.py#L811) | A8 | → `self.lifecycle.shutdown_requested` |
| [`AppContext._begin_shell_execution`](../codey/app/context.py#L814) | A8 | → `self.lifecycle.begin_shell_execution` |
| [`AppContext._end_shell_execution`](../codey/app/context.py#L817) | A8 | → `self.lifecycle.end_shell_execution` |
| [`AppContext._wait_shell_executions`](../codey/app/context.py#L820) | A8 | → `self.lifecycle.wait_shell_executions` |
| [`AppContext.closed`](../codey/app/context.py#L824) | A8 | → `self.lifecycle.closed` |
| [`AppContext._wait_background_workers`](../codey/app/context.py#L827) | A8 | 按 A8 合同实施；完整顺序见函数体 |
| [`AppContext.close`](../codey/app/context.py#L837) | A8 | → `self.lifecycle.close` |
| [`AppContext._close_requested`](../codey/app/context.py#L841) | A8 | → `self.lifecycle.close_requested` |
| [`AppContext._close_requested`](../codey/app/context.py#L845) | A8 | → `self.lifecycle.close_requested` |
| [`AppContext._resources_closed`](../codey/app/context.py#L849) | A8 | → `self.lifecycle.resources_closed` |
| [`AppContext._resources_closed`](../codey/app/context.py#L853) | A8 | → `self.lifecycle.resources_closed` |
| [`AppContext._shell_executions`](../codey/app/context.py#L857) | A8 | → `self.lifecycle.shell_executions` |

### codey/app/task_runtime.py

| 函数/入口 | 合同 | 实际委托或责任 |
|---|---|---|
| [`TaskRuntimeCoordinator.__init__`](../codey/app/task_runtime.py#L22) | A1 | 装配共享依赖；不创建第二份状态所有者 |
| [`TaskRuntimeCoordinator.admission_line`](../codey/app/task_runtime.py#L38) | A1 | 按 A1 合同实施；完整顺序见函数体 |
| [`TaskRuntimeCoordinator.spawn_line`](../codey/app/task_runtime.py#L44) | A1 | 按 A1 合同实施；完整顺序见函数体 |
| [`TaskRuntimeCoordinator.abort_provider`](../codey/app/task_runtime.py#L49) | A1 | 按 A1 合同实施；完整顺序见函数体 |
| [`TaskRuntimeCoordinator.register_active_provider`](../codey/app/task_runtime.py#L60) | A1 | 按 A1 合同实施；完整顺序见函数体 |
| [`TaskRuntimeCoordinator.unregister_active_provider`](../codey/app/task_runtime.py#L63) | A1 | 按 A1 合同实施；完整顺序见函数体 |
| [`TaskRuntimeCoordinator.reserve_run`](../codey/app/task_runtime.py#L66) | A1 | 按 A1 合同实施；完整顺序见函数体 |
| [`TaskRuntimeCoordinator.active_run_for`](../codey/app/task_runtime.py#L85) | A1 | 按 A1 合同实施；完整顺序见函数体 |
| [`TaskRuntimeCoordinator.current_run`](../codey/app/task_runtime.py#L93) | A1 | 按 A1 合同实施；完整顺序见函数体 |
| [`TaskRuntimeCoordinator.active_runs`](../codey/app/task_runtime.py#L96) | A1 | 按 A1 合同实施；完整顺序见函数体 |
| [`TaskRuntimeCoordinator.stop_flag_for`](../codey/app/task_runtime.py#L99) | A1 | 按 A1 合同实施；完整顺序见函数体 |
| [`TaskRuntimeCoordinator.stop_flag`](../codey/app/task_runtime.py#L103) | A1 | 按 A1 合同实施；完整顺序见函数体 |
| [`TaskRuntimeCoordinator.session_for`](../codey/app/task_runtime.py#L106) | A1 | 按 A1 合同实施；完整顺序见函数体 |
| [`TaskRuntimeCoordinator.request_stop`](../codey/app/task_runtime.py#L109) | A1 | 按 A1 合同实施；完整顺序见函数体 |
| [`TaskRuntimeCoordinator.clear_stop`](../codey/app/task_runtime.py#L112) | A1 | 按 A1 合同实施；完整顺序见函数体 |
| [`TaskRuntimeCoordinator.bind_api_selection`](../codey/app/task_runtime.py#L115) | A1 | 按 A1 合同实施；完整顺序见函数体 |
| [`TaskRuntimeCoordinator.is_busy`](../codey/app/task_runtime.py#L118) | A1 | 按 A1 合同实施；完整顺序见函数体 |
| [`TaskRuntimeCoordinator.run_status`](../codey/app/task_runtime.py#L121) | A1 | 按 A1 合同实施；完整顺序见函数体 |
| [`TaskRuntimeCoordinator.replace_reserved_run`](../codey/app/task_runtime.py#L124) | A1 | 按 A1 合同实施；完整顺序见函数体 |
| [`TaskRuntimeCoordinator.start_run`](../codey/app/task_runtime.py#L127) | A1 | 按 A1 合同实施；完整顺序见函数体 |
| [`TaskRuntimeCoordinator.set_run_status`](../codey/app/task_runtime.py#L130) | A1 | 按 A1 合同实施；完整顺序见函数体 |
| [`TaskRuntimeCoordinator.switch_run_provider`](../codey/app/task_runtime.py#L133) | A1 | 按 A1 合同实施；完整顺序见函数体 |
| [`TaskRuntimeCoordinator.release_run`](../codey/app/task_runtime.py#L136) | A1 | 按 A1 合同实施；完整顺序见函数体 |
| [`TaskRuntimeCoordinator.wait_for_slot`](../codey/app/task_runtime.py#L140) | A1 | 按 A1 合同实施；完整顺序见函数体 |

### codey/app/task_lifecycle.py

| 函数/入口 | 合同 | 实际委托或责任 |
|---|---|---|
| [`ShellApprovalExpiry.__call__`](../codey/app/task_lifecycle.py#L26) | A2 | 按 A2 合同实施；完整顺序见函数体 |
| [`TaskLifecycleCoordinator.__init__`](../codey/app/task_lifecycle.py#L47) | A2 | 装配共享依赖；不创建第二份状态所有者 |
| [`TaskLifecycleCoordinator.finish_run`](../codey/app/task_lifecycle.py#L80) | A2 | 按 A2 合同实施；完整顺序见函数体 |
| [`TaskLifecycleCoordinator.request_stop`](../codey/app/task_lifecycle.py#L106) | A2 | 按 A2 合同实施；完整顺序见函数体 |
| [`TaskLifecycleCoordinator.expire_pending_shell_approvals`](../codey/app/task_lifecycle.py#L149) | A2 | 按 A2 合同实施；完整顺序见函数体 |
| [`TaskLifecycleCoordinator.abort_approvals`](../codey/app/task_lifecycle.py#L158) | A2 | 按 A2 合同实施；完整顺序见函数体 |
| [`TaskLifecycleCoordinator.expire_stale_shell_approvals`](../codey/app/task_lifecycle.py#L166) | A2 | 按 A2 合同实施；完整顺序见函数体 |

### codey/app/shell_approval_coordinator.py

| 函数/入口 | 合同 | 实际委托或责任 |
|---|---|---|
| [`ShellApprovalCoordinator.__init__`](../codey/app/shell_approval_coordinator.py#L33) | A3 | 装配共享依赖；不创建第二份状态所有者 |
| [`ShellApprovalCoordinator.add_pending`](../codey/app/shell_approval_coordinator.py#L36) | A3 | 按 A3 合同实施；完整顺序见函数体 |
| [`ShellApprovalCoordinator.record_fact`](../codey/app/shell_approval_coordinator.py#L66) | A3 | 按 A3 合同实施；完整顺序见函数体 |
| [`ShellApprovalCoordinator.commit_fact`](../codey/app/shell_approval_coordinator.py#L71) | A3 | 按 A3 合同实施；完整顺序见函数体 |
| [`ShellApprovalCoordinator.expire_locked`](../codey/app/shell_approval_coordinator.py#L76) | A3 | 按 A3 合同实施；完整顺序见函数体 |
| [`ShellApprovalCoordinator.claim_locked`](../codey/app/shell_approval_coordinator.py#L96) | A3 | 按 A3 合同实施；完整顺序见函数体 |
| [`ShellApprovalCoordinator.claim`](../codey/app/shell_approval_coordinator.py#L111) | A3 | 按 A3 合同实施；完整顺序见函数体 |
| [`ShellApprovalCoordinator.resolve`](../codey/app/shell_approval_coordinator.py#L150) | A3 | 按 A3 合同实施；完整顺序见函数体 |
| [`ShellApprovalCoordinator.claim_ticket`](../codey/app/shell_approval_coordinator.py#L156) | A3 | 按 A3 合同实施；完整顺序见函数体 |
| [`ShellApprovalCoordinator.generation`](../codey/app/shell_approval_coordinator.py#L169) | A3 | 按 A3 合同实施；完整顺序见函数体 |
| [`ShellApprovalCoordinator.pending`](../codey/app/shell_approval_coordinator.py#L173) | A3 | 按 A3 合同实施；完整顺序见函数体 |

### codey/app/durable_event_bridge.py

| 函数/入口 | 合同 | 实际委托或责任 |
|---|---|---|
| [`DurableEventBridge.__init__`](../codey/app/durable_event_bridge.py#L14) | A4 | 装配共享依赖；不创建第二份状态所有者 |
| [`DurableEventBridge.publish_commit`](../codey/app/durable_event_bridge.py#L23) | A4 | 按 A4 合同实施；完整顺序见函数体 |
| [`DurableEventBridge.flush`](../codey/app/durable_event_bridge.py#L33) | A4 | 按 A4 合同实施；完整顺序见函数体 |

### codey/app/run_state_projection.py

| 函数/入口 | 合同 | 实际委托或责任 |
|---|---|---|
| [`RunStateProjection.__init__`](../codey/app/run_state_projection.py#L21) | A5 | 装配共享依赖；不创建第二份状态所有者 |
| [`RunStateProjection.record_shell_result`](../codey/app/run_state_projection.py#L34) | A5 | 按 A5 合同实施；完整顺序见函数体 |
| [`RunStateProjection.payload`](../codey/app/run_state_projection.py#L59) | A5 | 按 A5 合同实施；完整顺序见函数体 |
| [`RunStateProjection.pending_ui_event`](../codey/app/run_state_projection.py#L71) | A5 | 按 A5 合同实施；完整顺序见函数体 |

### codey/app/conversation_coordinator.py

| 函数/入口 | 合同 | 实际委托或责任 |
|---|---|---|
| [`SessionScopeStore.delete_scope`](../codey/app/conversation_coordinator.py#L22) | A6 | 按 A6 合同实施；完整顺序见函数体 |
| [`forget_scope`](../codey/app/conversation_coordinator.py#L25) | A6 | 按 A6 合同实施；完整顺序见函数体 |
| [`forget_trace`](../codey/app/conversation_coordinator.py#L30) | A6 | 按 A6 合同实施；完整顺序见函数体 |
| [`ConversationCoordinator.__init__`](../codey/app/conversation_coordinator.py#L36) | A6 | 装配共享依赖；不创建第二份状态所有者 |
| [`ConversationCoordinator.conversation_for`](../codey/app/conversation_coordinator.py#L58) | A6 | 按 A6 合同实施；完整顺序见函数体 |
| [`ConversationCoordinator.load_snapshot`](../codey/app/conversation_coordinator.py#L61) | A6 | 按 A6 合同实施；完整顺序见函数体 |
| [`ConversationCoordinator.save_snapshot`](../codey/app/conversation_coordinator.py#L67) | A6 | 按 A6 合同实施；完整顺序见函数体 |
| [`ConversationCoordinator.forget`](../codey/app/conversation_coordinator.py#L72) | A6 | 按 A6 合同实施；完整顺序见函数体 |

### codey/app/workspace_leases.py

| 函数/入口 | 合同 | 实际委托或责任 |
|---|---|---|
| [`WorkspaceLeaseCoordinator.__init__`](../codey/app/workspace_leases.py#L15) | A7 | 装配共享依赖；不创建第二份状态所有者 |
| [`WorkspaceLeaseCoordinator.acquire`](../codey/app/workspace_leases.py#L20) | A7 | 按 A7 合同实施；完整顺序见函数体 |
| [`WorkspaceLeaseCoordinator.release`](../codey/app/workspace_leases.py#L40) | A7 | 按 A7 合同实施；完整顺序见函数体 |
| [`WorkspaceLeaseCoordinator.close`](../codey/app/workspace_leases.py#L51) | A7 | 按 A7 合同实施；完整顺序见函数体 |

### codey/app/lifecycle.py

| 函数/入口 | 合同 | 实际委托或责任 |
|---|---|---|
| [`release_optional_resource`](../codey/app/lifecycle.py#L15) | A8 | 按 A8 合同实施；完整顺序见函数体 |
| [`ResourceSlot.__init__`](../codey/app/lifecycle.py#L25) | A8 | 装配共享依赖；不创建第二份状态所有者 |
| [`ResourceSlot.close`](../codey/app/lifecycle.py#L34) | A8 | 按 A8 合同实施；完整顺序见函数体 |
| [`AppLifecycle.__init__`](../codey/app/lifecycle.py#L56) | A8 | 装配共享依赖；不创建第二份状态所有者 |
| [`AppLifecycle.shell_executions`](../codey/app/lifecycle.py#L75) | A8 | 按 A8 合同实施；完整顺序见函数体 |
| [`AppLifecycle.begin_shell_execution`](../codey/app/lifecycle.py#L79) | A8 | 按 A8 合同实施；完整顺序见函数体 |
| [`AppLifecycle.end_shell_execution`](../codey/app/lifecycle.py#L86) | A8 | 按 A8 合同实施；完整顺序见函数体 |
| [`AppLifecycle.wait_shell_executions`](../codey/app/lifecycle.py#L92) | A8 | 按 A8 合同实施；完整顺序见函数体 |
| [`AppLifecycle.close`](../codey/app/lifecycle.py#L102) | A8 | 按 A8 合同实施；完整顺序见函数体 |

### codey/durable/session.py

| 函数/入口 | 合同 | 实际委托或责任 |
|---|---|---|
| [`DurableSession.conversation_ids`](../codey/durable/session.py#L41) | Qc | → `self.conversation_queries.conversation_ids` |
| [`DurableSession.submission`](../codey/durable/session.py#L45) | Qt | 通过统一查询线读取提交记录 |
| [`DurableSession.__init__`](../codey/durable/session.py#L48) | A0 | 装配共享依赖；不创建第二份状态所有者 |
| [`DurableSession.create_conversation`](../codey/durable/session.py#L63) | Dc | → `self.conversation_operations.create_conversation` |
| [`DurableSession.head`](../codey/durable/session.py#L66) | Qc | → `self.conversation_queries.head` |
| [`DurableSession.has_conversation`](../codey/durable/session.py#L69) | Qc | → `self.conversation_queries.has_conversation` |
| [`DurableSession.record_display_message`](../codey/durable/session.py#L72) | Dc | → `self.conversation_operations.record_display_message` |
| [`DurableSession.navigation_page`](../codey/durable/session.py#L76) | Qc | → `self.conversation_queries.navigation_page` |
| [`DurableSession.history_window`](../codey/durable/session.py#L80) | Qc | → `self.conversation_queries.history_window` |
| [`DurableSession.conversation_tree`](../codey/durable/session.py#L86) | Qc | → `self.conversation_queries.conversation_tree` |
| [`DurableSession.conversation_lineage`](../codey/durable/session.py#L89) | Qc | → `self.conversation_queries.conversation_lineage` |
| [`DurableSession.select_idle_branch`](../codey/durable/session.py#L92) | Dc | → `self.conversation_operations.select_idle_branch` |
| [`DurableSession.pending_tasks`](../codey/durable/session.py#L95) | Qt | → `self.task_queries.pending_tasks` |
| [`DurableSession.queued_submissions`](../codey/durable/session.py#L99) | Qc | → `self.conversation_queries.queued_submissions` |
| [`DurableSession.inbox_doc`](../codey/durable/session.py#L102) | Qc | → `self.conversation_queries.inbox_doc` |
| [`DurableSession.inbox_history`](../codey/durable/session.py#L105) | Qc | → `self.conversation_queries.inbox_history` |
| [`DurableSession.withdraw_queued_submission_at`](../codey/durable/session.py#L108) | Ds | → `self.submission_operations.withdraw_queued_submission_at` |
| [`DurableSession.tasks`](../codey/durable/session.py#L117) | Qt | → `self.task_queries.tasks` |
| [`DurableSession.revise_queued_input`](../codey/durable/session.py#L121) | Ds | → `self.submission_operations.revise_queued_input` |
| [`DurableSession.transform_task_state`](../codey/durable/session.py#L127) | Dt | → `self.task_operations.transform_task_state` |
| [`DurableSession.update_task_state`](../codey/durable/session.py#L133) | Dt | → `self.task_operations.update_task_state` |
| [`DurableSession.admit_input`](../codey/durable/session.py#L142) | Ds | → `self.submission_operations.admit_input` |
| [`DurableSession.admit_write`](../codey/durable/session.py#L165) | Ds | → `self.submission_operations.admit_write` |
| [`DurableSession.place_submission`](../codey/durable/session.py#L182) | Ds | → `self.submission_operations.place_submission` |
| [`DurableSession.place_boundary_inputs`](../codey/durable/session.py#L185) | Ds | → `self.submission_operations.place_boundary_inputs` |
| [`DurableSession.commit_generation_boundary`](../codey/durable/session.py#L195) | Ds | → `self.submission_operations.commit_generation_boundary` |
| [`DurableSession.merge_generation_boundary`](../codey/durable/session.py#L206) | Ds | → `self.submission_operations.merge_generation_boundary` |
| [`DurableSession.reconcile_workflows`](../codey/durable/session.py#L218) | Dt | → `self.task_operations.reconcile_workflows` |
| [`DurableSession.fork_task`](../codey/durable/session.py#L221) | Ds | → `self.submission_operations.fork_task` |
| [`DurableSession.entries`](../codey/durable/session.py#L227) | Qc | → `self.conversation_queries.entries` |
| [`DurableSession.history_page`](../codey/durable/session.py#L230) | Qc | → `self.conversation_queries.history_page` |
| [`DurableSession.search_history`](../codey/durable/session.py#L239) | Qc | → `self.conversation_queries.search_history` |
| [`DurableSession.task`](../codey/durable/session.py#L248) | Qt | → `self.task_queries.task` |
| [`DurableSession.task_timeline`](../codey/durable/session.py#L251) | Qt | → `self.task_queries.task_timeline` |
| [`DurableSession.task_at`](../codey/durable/session.py#L254) | Qt | → `self.task_queries.task_at` |
| [`DurableSession.effects`](../codey/durable/session.py#L257) | Qt | → `self.task_queries.effects` |
| [`DurableSession.artifact`](../codey/durable/session.py#L260) | Qt | → `self.task_queries.artifact` |
| [`DurableSession.artifacts_for_task`](../codey/durable/session.py#L263) | Qt | → `self.task_queries.artifacts_for_task` |
| [`DurableSession.artifacts_for_effect`](../codey/durable/session.py#L266) | Qt | → `self.task_queries.artifacts_for_effect` |
| [`DurableSession.register_artifacts`](../codey/durable/session.py#L269) | De | → `self.effect_operations.register_artifacts` |
| [`DurableSession.task_facts`](../codey/durable/session.py#L272) | Qt | → `self.task_queries.task_facts` |
| [`DurableSession.effect`](../codey/durable/session.py#L276) | Qt | → `self.task_queries.effect` |
| [`DurableSession.approval`](../codey/durable/session.py#L279) | Qt | → `self.task_queries.approval` |
| [`DurableSession.create_approval`](../codey/durable/session.py#L282) | Da | → `self.approval_operations.create_approval` |
| [`DurableSession.transition_approval`](../codey/durable/session.py#L285) | Da | → `self.approval_operations.transition_approval` |
| [`DurableSession.claim_approval_and_begin_effect`](../codey/durable/session.py#L295) | Da | → `self.approval_operations.claim_approval_and_begin_effect` |
| [`DurableSession.settle_approval`](../codey/durable/session.py#L304) | Da | → `self.approval_operations.settle_approval` |
| [`DurableSession.record_task_policy`](../codey/durable/session.py#L314) | Dt | → `self.task_operations.record_task_policy` |
| [`DurableSession.record_reviewer_selection`](../codey/durable/session.py#L317) | Dt | → `self.task_operations.record_reviewer_selection` |
| [`DurableSession.advance_task`](../codey/durable/session.py#L322) | Dt | → `self.task_operations.advance_task` |
| [`DurableSession.start_task`](../codey/durable/session.py#L335) | Dt | → `self.task_operations.start_task` |
| [`DurableSession.request_cancel`](../codey/durable/session.py#L339) | Dt | → `self.task_operations.request_cancel` |
| [`DurableSession.cancel_task`](../codey/durable/session.py#L345) | Dt | → `self.task_operations.cancel_task` |
| [`DurableSession.claim_task`](../codey/durable/session.py#L348) | Dt | → `self.task_operations.claim_task` |
| [`DurableSession.recover_scheduler_state`](../codey/durable/session.py#L351) | Dt | → `self.task_operations.recover_scheduler_state` |
| [`DurableSession.unclaim_task`](../codey/durable/session.py#L354) | Dt | → `self.task_operations.unclaim_task` |
| [`DurableSession.wait_task`](../codey/durable/session.py#L357) | Dt | → `self.task_operations.wait_task` |
| [`DurableSession.finish_task`](../codey/durable/session.py#L367) | Dt | → `self.task_operations.finish_task` |
| [`DurableSession.complete_task`](../codey/durable/session.py#L370) | Dt | → `self.task_operations.complete_task` |
| [`DurableSession.reconcile_waiting_tasks`](../codey/durable/session.py#L373) | Dt | → `self.task_operations.reconcile_waiting_tasks` |
| [`DurableSession.reconcile_finalization`](../codey/durable/session.py#L376) | Dt | → `self.task_operations.reconcile_finalization` |
| [`DurableSession.fail_task`](../codey/durable/session.py#L379) | Dt | → `self.task_operations.fail_task` |
| [`DurableSession.record_effect_intent`](../codey/durable/session.py#L389) | De | → `self.effect_operations.record_effect_intent` |
| [`DurableSession.record_effect_intents`](../codey/durable/session.py#L392) | De | → `self.effect_operations.record_effect_intents` |
| [`DurableSession.settle_effect`](../codey/durable/session.py#L397) | De | → `self.effect_operations.settle_effect` |
| [`DurableSession.settle_unknown_effect`](../codey/durable/session.py#L407) | De | → `self.effect_operations.settle_unknown_effect` |
| [`DurableSession.append_message`](../codey/durable/session.py#L410) | Dc | → `self.conversation_operations.append_message` |
| [`DurableSession.branch`](../codey/durable/session.py#L418) | Dc | → `self.conversation_operations.branch` |
| [`DurableSession.fork`](../codey/durable/session.py#L427) | Dc | → `self.conversation_operations.fork` |
| [`DurableSession.delete_conversation`](../codey/durable/session.py#L430) | Dc | → `self.conversation_operations.delete_conversation` |
| [`DurableSession.context`](../codey/durable/session.py#L433) | Qc | → `self.conversation_queries.context` |
| [`DurableSession.context_at`](../codey/durable/session.py#L443) | Qc | → `self.conversation_queries.context_at` |
| [`DurableSession.context_snapshot`](../codey/durable/session.py#L448) | Qc | → `self.conversation_queries.context_snapshot` |
| [`DurableSession.context_snapshot_at`](../codey/durable/session.py#L462) | Qc | → `self.conversation_queries.context_snapshot_at` |
| [`DurableSession.save_context_snapshot`](../codey/durable/session.py#L476) | Dc | → `self.conversation_operations.save_context_snapshot` |
| [`DurableSession.latest_context_snapshot`](../codey/durable/session.py#L481) | Qc | → `self.conversation_queries.latest_context_snapshot` |

### codey/durable/session_kernel.py

| 函数/入口 | 合同 | 实际委托或责任 |
|---|---|---|
| [`_mutation_line`](../codey/durable/session_kernel.py#L32) | K | 共享同步、提交、fencing、失败/关闭或订阅内核 |
| [`SessionKernel.database_id`](../codey/durable/session_kernel.py#L49) | K | 共享同步、提交、fencing、失败/关闭或订阅内核 |
| [`SessionKernel.control_line`](../codey/durable/session_kernel.py#L53) | K | 共享同步、提交、fencing、失败/关闭或订阅内核 |
| [`SessionKernel.__init__`](../codey/durable/session_kernel.py#L63) | K | 装配共享依赖；不创建第二份状态所有者 |
| [`SessionKernel._query_line`](../codey/durable/session_kernel.py#L82) | K | 共享同步、提交、fencing、失败/关闭或订阅内核 |
| [`SessionKernel._commit`](../codey/durable/session_kernel.py#L89) | K | 共享同步、提交、fencing、失败/关闭或订阅内核 |
| [`SessionKernel._materialize_approval`](../codey/durable/session_kernel.py#L121) | K | 共享同步、提交、fencing、失败/关闭或订阅内核 |
| [`SessionKernel._ensure_writable`](../codey/durable/session_kernel.py#L126) | K | 共享同步、提交、fencing、失败/关闭或订阅内核 |
| [`SessionKernel._ensure_usable`](../codey/durable/session_kernel.py#L129) | K | 共享同步、提交、fencing、失败/关闭或订阅内核 |
| [`SessionKernel._fail`](../codey/durable/session_kernel.py#L139) | K | 共享同步、提交、fencing、失败/关闭或订阅内核 |
| [`SessionKernel.transact_task`](../codey/durable/session_kernel.py#L146) | K | 共享同步、提交、fencing、失败/关闭或订阅内核 |
| [`SessionKernel.execution`](../codey/durable/session_kernel.py#L170) | K | 共享同步、提交、fencing、失败/关闭或订阅内核 |
| [`SessionKernel._publish_commit`](../codey/durable/session_kernel.py#L183) | K | 共享同步、提交、fencing、失败/关闭或订阅内核 |
| [`SessionKernel.observe_commits`](../codey/durable/session_kernel.py#L205) | K | 共享同步、提交、fencing、失败/关闭或订阅内核 |
| [`SessionKernel.subscribe_commits`](../codey/durable/session_kernel.py#L219) | K | 共享同步、提交、fencing、失败/关闭或订阅内核 |
| [`SessionKernel.subscribe_close`](../codey/durable/session_kernel.py#L233) | K | 共享同步、提交、fencing、失败/关闭或订阅内核 |
| [`SessionKernel._notify_close`](../codey/durable/session_kernel.py#L245) | K | 共享同步、提交、fencing、失败/关闭或订阅内核 |
| [`SessionKernel.close`](../codey/durable/session_kernel.py#L257) | K | 共享同步、提交、fencing、失败/关闭或订阅内核 |
| [`SessionKernel.commit_sequence`](../codey/durable/session_kernel.py#L270) | K | 共享同步、提交、fencing、失败/关闭或订阅内核 |
| [`SessionKernel.commits_after`](../codey/durable/session_kernel.py#L274) | K | 共享同步、提交、fencing、失败/关闭或订阅内核 |

### codey/durable/conversation_queries.py

| 函数/入口 | 合同 | 实际委托或责任 |
|---|---|---|
| [`ConversationQueryFacade.__init__`](../codey/durable/conversation_queries.py#L25) | Qc | 装配共享依赖；不创建第二份状态所有者 |
| [`ConversationQueryFacade.conversation_ids`](../codey/durable/conversation_queries.py#L29) | Qc | 按 Qc 合同实施；完整顺序见函数体 |
| [`ConversationQueryFacade.head`](../codey/durable/conversation_queries.py#L32) | Qc | 按 Qc 合同实施；完整顺序见函数体 |
| [`ConversationQueryFacade.has_conversation`](../codey/durable/conversation_queries.py#L39) | Qc | 按 Qc 合同实施；完整顺序见函数体 |
| [`ConversationQueryFacade.navigation_page`](../codey/durable/conversation_queries.py#L45) | Qc | 按 Qc 合同实施；完整顺序见函数体 |
| [`ConversationQueryFacade.history_window`](../codey/durable/conversation_queries.py#L58) | Qc | 按 Qc 合同实施；完整顺序见函数体 |
| [`ConversationQueryFacade.conversation_tree`](../codey/durable/conversation_queries.py#L72) | Qc | 按 Qc 合同实施；完整顺序见函数体 |
| [`ConversationQueryFacade.conversation_lineage`](../codey/durable/conversation_queries.py#L75) | Qc | 按 Qc 合同实施；完整顺序见函数体 |
| [`ConversationQueryFacade.queued_submissions`](../codey/durable/conversation_queries.py#L78) | Qc | 按 Qc 合同实施；完整顺序见函数体 |
| [`ConversationQueryFacade.inbox_doc`](../codey/durable/conversation_queries.py#L81) | Qc | 按 Qc 合同实施；完整顺序见函数体 |
| [`ConversationQueryFacade.inbox_history`](../codey/durable/conversation_queries.py#L97) | Qc | 按 Qc 合同实施；完整顺序见函数体 |
| [`ConversationQueryFacade.entries`](../codey/durable/conversation_queries.py#L130) | Qc | 按 Qc 合同实施；完整顺序见函数体 |
| [`ConversationQueryFacade.history_page`](../codey/durable/conversation_queries.py#L133) | Qc | 按 Qc 合同实施；完整顺序见函数体 |
| [`ConversationQueryFacade.search_history`](../codey/durable/conversation_queries.py#L146) | Qc | 按 Qc 合同实施；完整顺序见函数体 |
| [`ConversationQueryFacade.context`](../codey/durable/conversation_queries.py#L159) | Qc | 按 Qc 合同实施；完整顺序见函数体 |
| [`ConversationQueryFacade.context_at`](../codey/durable/conversation_queries.py#L164) | Qc | 按 Qc 合同实施；完整顺序见函数体 |
| [`ConversationQueryFacade.context_snapshot`](../codey/durable/conversation_queries.py#L177) | Qc | 按 Qc 合同实施；完整顺序见函数体 |
| [`ConversationQueryFacade.context_snapshot_at`](../codey/durable/conversation_queries.py#L197) | Qc | 按 Qc 合同实施；完整顺序见函数体 |
| [`ConversationQueryFacade.latest_context_snapshot`](../codey/durable/conversation_queries.py#L217) | Qc | 按 Qc 合同实施；完整顺序见函数体 |

### codey/durable/task_queries.py

| 函数/入口 | 合同 | 实际委托或责任 |
|---|---|---|
| [`read_task_snapshot`](../codey/durable/task_queries.py#L16) | Qt | 按 Qt 合同实施；完整顺序见函数体 |
| [`TaskQueryFacade.__init__`](../codey/durable/task_queries.py#L26) | Qt | 装配共享依赖；不创建第二份状态所有者 |
| [`TaskQueryFacade.tasks`](../codey/durable/task_queries.py#L30) | Qt | 按 Qt 合同实施；完整顺序见函数体 |
| [`TaskQueryFacade.pending_tasks`](../codey/durable/task_queries.py#L33) | Qt | 按 Qt 合同实施；完整顺序见函数体 |
| [`TaskQueryFacade.task`](../codey/durable/task_queries.py#L36) | Qt | 按 Qt 合同实施；完整顺序见函数体 |
| [`TaskQueryFacade.task_timeline`](../codey/durable/task_queries.py#L42) | Qt | 按 Qt 合同实施；完整顺序见函数体 |
| [`TaskQueryFacade.task_at`](../codey/durable/task_queries.py#L45) | Qt | 按 Qt 合同实施；完整顺序见函数体 |
| [`TaskQueryFacade.effects`](../codey/durable/task_queries.py#L51) | Qt | 按 Qt 合同实施；完整顺序见函数体 |
| [`TaskQueryFacade.artifact`](../codey/durable/task_queries.py#L54) | Qt | 按 Qt 合同实施；完整顺序见函数体 |
| [`TaskQueryFacade.artifacts_for_task`](../codey/durable/task_queries.py#L60) | Qt | 按 Qt 合同实施；完整顺序见函数体 |
| [`TaskQueryFacade.artifacts_for_effect`](../codey/durable/task_queries.py#L63) | Qt | 按 Qt 合同实施；完整顺序见函数体 |
| [`TaskQueryFacade.task_facts`](../codey/durable/task_queries.py#L66) | Qt | 按 Qt 合同实施；完整顺序见函数体 |
| [`TaskQueryFacade.effect`](../codey/durable/task_queries.py#L72) | Qt | 按 Qt 合同实施；完整顺序见函数体 |
| [`TaskQueryFacade.approval`](../codey/durable/task_queries.py#L78) | Qt | 按 Qt 合同实施；完整顺序见函数体 |

### codey/durable/conversation_operations.py

| 函数/入口 | 合同 | 实际委托或责任 |
|---|---|---|
| [`ConversationOperations.create_conversation`](../codey/durable/conversation_operations.py#L23) | Dc | 按 Dc 合同实施；完整顺序见函数体 |
| [`ConversationOperations.record_display_message`](../codey/durable/conversation_operations.py#L33) | Dc | 按 Dc 合同实施；完整顺序见函数体 |
| [`ConversationOperations.select_idle_branch`](../codey/durable/conversation_operations.py#L56) | Dc | 按 Dc 合同实施；完整顺序见函数体 |
| [`ConversationOperations.append_message`](../codey/durable/conversation_operations.py#L62) | Dc | 按 Dc 合同实施；完整顺序见函数体 |
| [`ConversationOperations.branch`](../codey/durable/conversation_operations.py#L71) | Dc | 按 Dc 合同实施；完整顺序见函数体 |
| [`ConversationOperations.fork`](../codey/durable/conversation_operations.py#L83) | Dc | 按 Dc 合同实施；完整顺序见函数体 |
| [`ConversationOperations.delete_conversation`](../codey/durable/conversation_operations.py#L87) | Dc | 按 Dc 合同实施；完整顺序见函数体 |
| [`ConversationOperations.save_context_snapshot`](../codey/durable/conversation_operations.py#L92) | Dc | 按 Dc 合同实施；完整顺序见函数体 |

### codey/durable/effect_operations.py

| 函数/入口 | 合同 | 实际委托或责任 |
|---|---|---|
| [`EffectOperations.register_artifacts`](../codey/durable/effect_operations.py#L22) | De | 按 De 合同实施；完整顺序见函数体 |
| [`EffectOperations.record_effect_intent`](../codey/durable/effect_operations.py#L30) | De | 按 De 合同实施；完整顺序见函数体 |
| [`EffectOperations.record_effect_intents`](../codey/durable/effect_operations.py#L34) | De | 按 De 合同实施；完整顺序见函数体 |
| [`EffectOperations.settle_effect`](../codey/durable/effect_operations.py#L54) | De | 按 De 合同实施；完整顺序见函数体 |
| [`EffectOperations.settle_unknown_effect`](../codey/durable/effect_operations.py#L87) | De | 按 De 合同实施；完整顺序见函数体 |

### codey/durable/approval_operations.py

| 函数/入口 | 合同 | 实际委托或责任 |
|---|---|---|
| [`ApprovalOperations.create_approval`](../codey/durable/approval_operations.py#L29) | Da | 按 Da 合同实施；完整顺序见函数体 |
| [`ApprovalOperations.transition_approval`](../codey/durable/approval_operations.py#L40) | Da | 按 Da 合同实施；完整顺序见函数体 |
| [`ApprovalOperations.claim_approval_and_begin_effect`](../codey/durable/approval_operations.py#L62) | Da | 按 Da 合同实施；完整顺序见函数体 |
| [`ApprovalOperations.settle_approval`](../codey/durable/approval_operations.py#L111) | Da | 按 Da 合同实施；完整顺序见函数体 |

### codey/durable/task_operations.py

| 函数/入口 | 合同 | 实际委托或责任 |
|---|---|---|
| [`TaskLifecycleOperations.transform_task_state`](../codey/durable/task_operations.py#L34) | Dt | 按 Dt 合同实施；完整顺序见函数体 |
| [`TaskLifecycleOperations.update_task_state`](../codey/durable/task_operations.py#L46) | Dt | 按 Dt 合同实施；完整顺序见函数体 |
| [`TaskLifecycleOperations.reconcile_workflows`](../codey/durable/task_operations.py#L78) | Dt | 按 Dt 合同实施；完整顺序见函数体 |
| [`TaskLifecycleOperations.record_task_policy`](../codey/durable/task_operations.py#L117) | Dt | 按 Dt 合同实施；完整顺序见函数体 |
| [`TaskLifecycleOperations.record_reviewer_selection`](../codey/durable/task_operations.py#L133) | Dt | 按 Dt 合同实施；完整顺序见函数体 |
| [`TaskLifecycleOperations._record_immutable_task_state`](../codey/durable/task_operations.py#L145) | Dt | 按 Dt 合同实施；完整顺序见函数体 |
| [`TaskLifecycleOperations.advance_task`](../codey/durable/task_operations.py#L187) | Dt | 按 Dt 合同实施；完整顺序见函数体 |
| [`TaskLifecycleOperations.start_task`](../codey/durable/task_operations.py#L276) | Dt | 按 Dt 合同实施；完整顺序见函数体 |
| [`TaskLifecycleOperations._owned_descendants`](../codey/durable/task_operations.py#L284) | Dt | 按 Dt 合同实施；完整顺序见函数体 |
| [`TaskLifecycleOperations._live_owned_descendants`](../codey/durable/task_operations.py#L300) | Dt | 按 Dt 合同实施；完整顺序见函数体 |
| [`TaskLifecycleOperations.request_cancel`](../codey/durable/task_operations.py#L305) | Dt | 按 Dt 合同实施；完整顺序见函数体 |
| [`TaskLifecycleOperations.cancel_task`](../codey/durable/task_operations.py#L366) | Dt | 按 Dt 合同实施；完整顺序见函数体 |
| [`TaskLifecycleOperations._cancel_terminal`](../codey/durable/task_operations.py#L393) | Dt | 按 Dt 合同实施；完整顺序见函数体 |
| [`TaskLifecycleOperations.claim_task`](../codey/durable/task_operations.py#L427) | Dt | 按 Dt 合同实施；完整顺序见函数体 |
| [`TaskLifecycleOperations.recover_scheduler_state`](../codey/durable/task_operations.py#L448) | Dt | 按 Dt 合同实施；完整顺序见函数体 |
| [`TaskLifecycleOperations.unclaim_task`](../codey/durable/task_operations.py#L489) | Dt | 按 Dt 合同实施；完整顺序见函数体 |
| [`TaskLifecycleOperations.wait_task`](../codey/durable/task_operations.py#L500) | Dt | 按 Dt 合同实施；完整顺序见函数体 |
| [`TaskLifecycleOperations.finish_task`](../codey/durable/task_operations.py#L573) | Dt | 按 Dt 合同实施；完整顺序见函数体 |
| [`TaskLifecycleOperations.complete_task`](../codey/durable/task_operations.py#L585) | Dt | 按 Dt 合同实施；完整顺序见函数体 |
| [`TaskLifecycleOperations._hold_completion`](../codey/durable/task_operations.py#L598) | Dt | 按 Dt 合同实施；完整顺序见函数体 |
| [`TaskLifecycleOperations.reconcile_waiting_tasks`](../codey/durable/task_operations.py#L615) | Dt | 按 Dt 合同实施；完整顺序见函数体 |
| [`TaskLifecycleOperations.reconcile_finalization`](../codey/durable/task_operations.py#L654) | Dt | 按 Dt 合同实施；完整顺序见函数体 |
| [`TaskLifecycleOperations.fail_task`](../codey/durable/task_operations.py#L685) | Dt | 按 Dt 合同实施；完整顺序见函数体 |

### codey/durable/submission_operations.py

| 函数/入口 | 合同 | 实际委托或责任 |
|---|---|---|
| [`SubmissionOperations.withdraw_queued_submission_at`](../codey/durable/submission_operations.py#L38) | Ds | 按 Ds 合同实施；完整顺序见函数体 |
| [`SubmissionOperations.revise_queued_input`](../codey/durable/submission_operations.py#L105) | Ds | 按 Ds 合同实施；完整顺序见函数体 |
| [`SubmissionOperations.admit_input`](../codey/durable/submission_operations.py#L144) | Ds | 按 Ds 合同实施；完整顺序见函数体 |
| [`SubmissionOperations.admit_write`](../codey/durable/submission_operations.py#L177) | Ds | 按 Ds 合同实施；完整顺序见函数体 |
| [`SubmissionOperations.place_submission`](../codey/durable/submission_operations.py#L208) | Ds | 按 Ds 合同实施；完整顺序见函数体 |
| [`SubmissionOperations.place_boundary_inputs`](../codey/durable/submission_operations.py#L265) | Ds | 按 Ds 合同实施；完整顺序见函数体 |
| [`SubmissionOperations.commit_generation_boundary`](../codey/durable/submission_operations.py#L292) | Ds | 按 Ds 合同实施；完整顺序见函数体 |
| [`SubmissionOperations.merge_generation_boundary`](../codey/durable/submission_operations.py#L332) | Ds | 按 Ds 合同实施；完整顺序见函数体 |
| [`SubmissionOperations._prepare_boundary_mutation`](../codey/durable/submission_operations.py#L392) | Ds | 按 Ds 合同实施；完整顺序见函数体 |
| [`SubmissionOperations._prepare_boundary_placement`](../codey/durable/submission_operations.py#L485) | Ds | 按 Ds 合同实施；完整顺序见函数体 |
| [`SubmissionOperations._prepare_boundary_write`](../codey/durable/submission_operations.py#L535) | Ds | 按 Ds 合同实施；完整顺序见函数体 |
| [`SubmissionOperations._place_write_submission`](../codey/durable/submission_operations.py#L593) | Ds | 按 Ds 合同实施；完整顺序见函数体 |
| [`SubmissionOperations._settle_stale_write`](../codey/durable/submission_operations.py#L658) | Ds | 按 Ds 合同实施；完整顺序见函数体 |
| [`SubmissionOperations.fork_task`](../codey/durable/submission_operations.py#L673) | Ds | 按 Ds 合同实施；完整顺序见函数体 |

### codey/durable/operations.py

| 函数/入口 | 合同 | 实际委托或责任 |
|---|---|---|
| [`SessionOperations.__init__`](../codey/durable/operations.py#L19) | K | 装配共享依赖；不创建第二份状态所有者 |
| [`operation_line`](../codey/durable/operations.py#L23) | K | 共享同步、提交、fencing、失败/关闭或订阅内核 |


### frontend/src/app_runtime.ts

| 函数/入口 | 合同 | 实际委托或责任 |
|---|---|---|
| [`adoptChatFork`](../frontend/src/app_runtime.ts#L132) | F0 | 按 F0 合同；状态与执行判断归对应 owner |
| [`openDocument`](../frontend/src/app_runtime.ts#L277) | F0 | 按 F0 合同；状态与执行判断归对应 owner |
| [`liveDefaultProvider`](../frontend/src/app_runtime.ts#L335) | Fr | 按 Fr 合同；状态与执行判断归对应 owner |
| [`activeSession`](../frontend/src/app_runtime.ts#L379) | Fs | → `sessionState.activeSession` |
| [`projectById`](../frontend/src/app_runtime.ts#L382) | Fs | → `sessionState.projectById` |
| [`sessionProject`](../frontend/src/app_runtime.ts#L385) | Fs | → `sessionState.sessionProject` |
| [`activeProject`](../frontend/src/app_runtime.ts#L388) | Fs | → `sessionState.activeProject` |
| [`currentProjectPath`](../frontend/src/app_runtime.ts#L391) | Fs | → `sessionState.currentProjectPath` |
| [`currentProviderId`](../frontend/src/app_runtime.ts#L394) | Fs | → `sessionState.currentProviderId` |
| [`syncProviderUI`](../frontend/src/app_runtime.ts#L398) | Fr | 按 Fr 合同；状态与执行判断归对应 owner |
| [`applyProviderStatus`](../frontend/src/app_runtime.ts#L401) | Fr | 按 Fr 合同；状态与执行判断归对应 owner |
| [`refreshProviderStatus`](../frontend/src/app_runtime.ts#L404) | Fr | → `runtime.ProviderUI.refreshStatus` |
| [`updateComposerContext`](../frontend/src/app_runtime.ts#L408) | F0 | 按 F0 合同；状态与执行判断归对应 owner |
| [`syncResearchUI`](../frontend/src/app_runtime.ts#L426) | Fr | 按 Fr 合同；状态与执行判断归对应 owner |
| [`currentIntentForSession`](../frontend/src/app_runtime.ts#L436) | Fr | 按 Fr 合同；状态与执行判断归对应 owner |
| [`renameInputEl`](../frontend/src/app_runtime.ts#L444) | F0 | 按 F0 合同；状态与执行判断归对应 owner |
| [`commitSessionRename`](../frontend/src/app_runtime.ts#L471) | F0 | 按 F0 合同；状态与执行判断归对应 owner |
| [`commitProjectRename`](../frontend/src/app_runtime.ts#L474) | F0 | 按 F0 合同；状态与执行判断归对应 owner |
| [`renderSidebar`](../frontend/src/app_runtime.ts#L504) | F0 | 按 F0 合同；状态与执行判断归对应 owner |
| [`disarmDangerButtons`](../frontend/src/app_runtime.ts#L507) | F0 | 按 F0 合同；状态与执行判断归对应 owner |
| [`armDanger`](../frontend/src/app_runtime.ts#L518) | F0 | 按 F0 合同；状态与执行判断归对应 owner |
| [`renderChat`](../frontend/src/app_runtime.ts#L533) | F0 | 按 F0 合同；状态与执行判断归对应 owner |
| [`statusRow`](../frontend/src/app_runtime.ts#L539) | F0 | 按 F0 合同；状态与执行判断归对应 owner |
| [`appendMessageNode`](../frontend/src/app_runtime.ts#L579) | F0 | 按 F0 合同；状态与执行判断归对应 owner |
| [`scrollChat`](../frontend/src/app_runtime.ts#L583) | F0 | 按 F0 合同；状态与执行判断归对应 owner |
| [`switchSession`](../frontend/src/app_runtime.ts#L588) | F0 | 按 F0 合同；状态与执行判断归对应 owner |
| [`openProject`](../frontend/src/app_runtime.ts#L591) | F0 | 按 F0 合同；状态与执行判断归对应 owner |
| [`newSession`](../frontend/src/app_runtime.ts#L594) | F0 | 按 F0 合同；状态与执行判断归对应 owner |
| [`forgetSessionState`](../frontend/src/app_runtime.ts#L597) | F0 | 按 F0 合同；状态与执行判断归对应 owner |
| [`deleteSession`](../frontend/src/app_runtime.ts#L611) | Fs | → `sessionState.deleteSession` |
| [`renameSession`](../frontend/src/app_runtime.ts#L614) | F0 | 按 F0 合同；状态与执行判断归对应 owner |
| [`clearMessages`](../frontend/src/app_runtime.ts#L617) | Fs | → `sessionState.clearMessages` |
| [`renameProject`](../frontend/src/app_runtime.ts#L620) | F0 | 按 F0 合同；状态与执行判断归对应 owner |
| [`deleteProject`](../frontend/src/app_runtime.ts#L623) | Fs | → `sessionState.deleteProject` |
| [`ensureProject`](../frontend/src/app_runtime.ts#L626) | Fs | → `sessionState.ensureProject` |
| [`pickProjectPath`](../frontend/src/app_runtime.ts#L630) | Fr | 按 Fr 合同；状态与执行判断归对应 owner |
| [`attachSessionToProject`](../frontend/src/app_runtime.ts#L657) | Fs | → `sessionState.attachSessionToProject` |
| [`pickProject`](../frontend/src/app_runtime.ts#L660) | F0 | 按 F0 合同；状态与执行判断归对应 owner |
| [`attachCurrentChatToPickedProject`](../frontend/src/app_runtime.ts#L671) | F0 | 按 F0 合同；状态与执行判断归对应 owner |
| [`pushMsgToSession`](../frontend/src/app_runtime.ts#L683) | Fm | → `sessionState.pushMessage` |
| [`openMenuAt`](../frontend/src/app_runtime.ts#L688) | F0 | 按 F0 合同；状态与执行判断归对应 owner |
| [`closeAllMenus`](../frontend/src/app_runtime.ts#L700) | F0 | 按 F0 合同；状态与执行判断归对应 owner |
| [`openProjectMenu`](../frontend/src/app_runtime.ts#L705) | F0 | 按 F0 合同；状态与执行判断归对应 owner |
| [`openSessionMenu`](../frontend/src/app_runtime.ts#L711) | F0 | 按 F0 合同；状态与执行判断归对应 owner |
| [`openTopMenu`](../frontend/src/app_runtime.ts#L717) | F0 | 按 F0 合同；状态与执行判断归对应 owner |
| [`isSessionRunning`](../frontend/src/app_runtime.ts#L799) | F0 | → `runProjection.isSessionRunning` |
| [`setStatus`](../frontend/src/app_runtime.ts#L803) | F0 | 按 F0 合同；状态与执行判断归对应 owner |
| [`applyLifecycle`](../frontend/src/app_runtime.ts#L824) | Fl | → `runEvents.applyLifecycle` |
| [`handleServerEvent`](../frontend/src/app_runtime.ts#L833) | Fe | → `runEvents.handleServerEvent` |
| [`recordResearchRun`](../frontend/src/app_runtime.ts#L841) | Fr | 按 Fr 合同；状态与执行判断归对应 owner |
| [`currentResearchRun`](../frontend/src/app_runtime.ts#L850) | Fr | → `runtime.ResearchRuns.current` |
| [`restoreResearchRun`](../frontend/src/app_runtime.ts#L854) | Fr | 按 Fr 合同；状态与执行判断归对应 owner |
| [`markResearchRestoreAvailability`](../frontend/src/app_runtime.ts#L880) | Fr | 按 Fr 合同；状态与执行判断归对应 owner |
| [`projectTaskStates`](../frontend/src/app_runtime.ts#L888) | Fl | → `runEvents.projectTaskStates` |
| [`applyRunState`](../frontend/src/app_runtime.ts#L892) | Fl | → `runEvents.applyRunState` |
| [`reconcileRunState`](../frontend/src/app_runtime.ts#L896) | Ft | → `runEvents.reconcileRunState` |
| [`acceptRunResponse`](../frontend/src/app_runtime.ts#L900) | Ft | → `runEvents.acceptRunResponse` |
| [`connectEvents`](../frontend/src/app_runtime.ts#L904) | Ft | → `runEvents.connectEvents` |
| [`addToSession`](../frontend/src/app_runtime.ts#L908) | Fm | → `sessionState.addMessage` |
| [`observeQueuedInput`](../frontend/src/app_runtime.ts#L912) | Fe | → `runEvents.observeQueuedInput` |
| [`observeUserQuestion`](../frontend/src/app_runtime.ts#L915) | Fe | → `runEvents.observeUserQuestion` |
| [`replaceSessionMessage`](../frontend/src/app_runtime.ts#L919) | Fm | → `sessionState.replaceMessage` |
| [`dropSessionMessages`](../frontend/src/app_runtime.ts#L923) | Fm | 按 Fm 合同；状态与执行判断归对应 owner |
| [`removeTeachMessage`](../frontend/src/app_runtime.ts#L926) | F0 | 按 F0 合同；状态与执行判断归对应 owner |
| [`removeShellRequest`](../frontend/src/app_runtime.ts#L929) | F0 | 按 F0 合同；状态与执行判断归对应 owner |
| [`addSendError`](../frontend/src/app_runtime.ts#L933) | F0 | 按 F0 合同；状态与执行判断归对应 owner |
| [`resumeTeaching`](../frontend/src/app_runtime.ts#L937) | Fr | 按 Fr 合同；状态与执行判断归对应 owner |
| [`sessionProjectPath`](../frontend/src/app_runtime.ts#L949) | Fr | 按 Fr 合同；状态与执行判断归对应 owner |
| [`fetchChanges`](../frontend/src/app_runtime.ts#L955) | Fr | 按 Fr 合同；状态与执行判断归对应 owner |
| [`fetchChangeFile`](../frontend/src/app_runtime.ts#L966) | F0 | 按 F0 合同；状态与执行判断归对应 owner |
| [`restoreChanges`](../frontend/src/app_runtime.ts#L977) | Fr | 按 Fr 合同；状态与执行判断归对应 owner |
| [`maybeAddChangesSummary`](../frontend/src/app_runtime.ts#L991) | Fr | 按 Fr 合同；状态与执行判断归对应 owner |
| [`openChangesDrawer`](../frontend/src/app_runtime.ts#L1004) | Fr | → `runtime.ChangesDrawer.open` |
| [`closeChangesDrawer`](../frontend/src/app_runtime.ts#L1008) | Fr | 按 Fr 合同；状态与执行判断归对应 owner |
| [`openLocalContextDrawer`](../frontend/src/app_runtime.ts#L1012) | F0 | 按 F0 合同；状态与执行判断归对应 owner |
| [`closeLocalContextDrawer`](../frontend/src/app_runtime.ts#L1016) | F0 | 按 F0 合同；状态与执行判断归对应 owner |
| [`handleLocalContextScopeChanged`](../frontend/src/app_runtime.ts#L1020) | F0 | 按 F0 合同；状态与执行判断归对应 owner |
| [`closeOtherDrawers`](../frontend/src/app_runtime.ts#L1027) | F0 | 按 F0 合同；状态与执行判断归对应 owner |
| [`approveCommand`](../frontend/src/app_runtime.ts#L1052) | F0 | 按 F0 合同；状态与执行判断归对应 owner |
| [`resizeTask`](../frontend/src/app_runtime.ts#L1061) | F0 | → `runtime.Composer.resizeTask` |
| [`updateSend`](../frontend/src/app_runtime.ts#L1064) | F0 | → `runtime.Composer.updateSend` |
| [`toggleResearchForActive`](../frontend/src/app_runtime.ts#L1068) | Fr | → `runtime.Composer.toggleResearchForActive` |
| [`openLocalProviderConfig`](../frontend/src/app_runtime.ts#L1073) | Fr | → `runtime.ProviderUI.openLocalConfig` |
| [`closeLocalProviderConfig`](../frontend/src/app_runtime.ts#L1077) | Fr | 按 Fr 合同；状态与执行判断归对应 owner |
| [`setActiveProvider`](../frontend/src/app_runtime.ts#L1081) | Fr | → `runtime.Composer.setActiveProvider` |
| [`continueTask`](../frontend/src/app_runtime.ts#L1086) | F0 | → `runtime.Composer.continueTask` |
| [`syncResearchUseProjectButton`](../frontend/src/app_runtime.ts#L1094) | Fr | 按 Fr 合同；状态与执行判断归对应 owner |
| [`boot`](../frontend/src/app_runtime.ts#L1139) | F0 | 按 F0 合同；状态与执行判断归对应 owner |

### frontend/src/session_state_store.ts

| 函数/入口 | 合同 | 实际委托或责任 |
|---|---|---|
| [`constructor`](../frontend/src/session_state_store.ts#L48) | Fs | 按 Fs 合同；状态与执行判断归对应 owner |
| [`activeSession`](../frontend/src/session_state_store.ts#L50) | Fs | → `this.sessions.find` |
| [`projectById`](../frontend/src/session_state_store.ts#L54) | Fs | 按 Fs 合同；状态与执行判断归对应 owner |
| [`sessionProject`](../frontend/src/session_state_store.ts#L58) | Fs | 按 Fs 合同；状态与执行判断归对应 owner |
| [`activeProject`](../frontend/src/session_state_store.ts#L62) | Fs | → `this.sessionProject` |
| [`currentProjectPath`](../frontend/src/session_state_store.ts#L66) | Fs | 按 Fs 合同；状态与执行判断归对应 owner |
| [`projectPathFor`](../frontend/src/session_state_store.ts#L70) | Fs | 按 Fs 合同；状态与执行判断归对应 owner |
| [`projectTerminalRun`](../frontend/src/session_state_store.ts#L74) | Fs | 按 Fs 合同；状态与执行判断归对应 owner |
| [`currentProviderId`](../frontend/src/session_state_store.ts#L83) | Fs | 按 Fs 合同；状态与执行判断归对应 owner |
| [`renameSession`](../frontend/src/session_state_store.ts#L88) | Fs | 按 Fs 合同；状态与执行判断归对应 owner |
| [`renameProject`](../frontend/src/session_state_store.ts#L99) | Fs | 按 Fs 合同；状态与执行判断归对应 owner |
| [`switchSession`](../frontend/src/session_state_store.ts#L109) | Fs | 按 Fs 合同；状态与执行判断归对应 owner |
| [`newSession`](../frontend/src/session_state_store.ts#L115) | Fs | 按 Fs 合同；状态与执行判断归对应 owner |
| [`openProject`](../frontend/src/session_state_store.ts#L123) | Fs | 按 Fs 合同；状态与执行判断归对应 owner |
| [`deleteSession`](../frontend/src/session_state_store.ts#L132) | Fs | 按 Fs 合同；状态与执行判断归对应 owner |
| [`clearMessages`](../frontend/src/session_state_store.ts#L143) | Fs | 按 Fs 合同；状态与执行判断归对应 owner |
| [`deleteProject`](../frontend/src/session_state_store.ts#L155) | Fs | 按 Fs 合同；状态与执行判断归对应 owner |
| [`ensureProject`](../frontend/src/session_state_store.ts#L172) | Fs | 按 Fs 合同；状态与执行判断归对应 owner |
| [`attachSessionToProject`](../frontend/src/session_state_store.ts#L190) | Fs | 按 Fs 合同；状态与执行判断归对应 owner |
| [`pushMessage`](../frontend/src/session_state_store.ts#L202) | Fm | 按 Fm 合同；状态与执行判断归对应 owner |
| [`addMessage`](../frontend/src/session_state_store.ts#L225) | Fm | 按 Fm 合同；状态与执行判断归对应 owner |
| [`replaceMessage`](../frontend/src/session_state_store.ts#L301) | Fm | 按 Fm 合同；状态与执行判断归对应 owner |
| [`dropMessages`](../frontend/src/session_state_store.ts#L314) | Fm | 按 Fm 合同；状态与执行判断归对应 owner |

### frontend/src/session_state_views.ts

| 函数/入口 | 合同 | 实际委托或责任 |
|---|---|---|
| [`constructor`](../frontend/src/session_state_views.ts#L25) | Fv | 按 Fv 合同；状态与执行判断归对应 owner |
| [`mountedStreamId`](../frontend/src/session_state_views.ts#L27) | Fv | 按 Fv 合同；状态与执行判断归对应 owner |
| [`invalidate`](../frontend/src/session_state_views.ts#L34) | Fv | 按 Fv 合同；状态与执行判断归对应 owner |
| [`replaceMessage`](../frontend/src/session_state_views.ts#L84) | Fm | 按 Fm 合同；状态与执行判断归对应 owner |

### frontend/src/run_event_coordinator.ts

| 函数/入口 | 合同 | 实际委托或责任 |
|---|---|---|
| [`constructor`](../frontend/src/run_event_coordinator.ts#L73) | Fe | 按 Fe 合同；状态与执行判断归对应 owner |
| [`removeTeachMessage`](../frontend/src/run_event_coordinator.ts#L83) | Fe | 按 Fe 合同；状态与执行判断归对应 owner |
| [`applyLifecycle`](../frontend/src/run_event_coordinator.ts#L87) | Fl | 按 Fl 合同；状态与执行判断归对应 owner |
| [`handleReplyStream`](../frontend/src/run_event_coordinator.ts#L145) | Fe | 按 Fe 合同；状态与执行判断归对应 owner |
| [`handleServerEvent`](../frontend/src/run_event_coordinator.ts#L165) | Fe | 按 Fe 合同；状态与执行判断归对应 owner |
| [`projectTaskStates`](../frontend/src/run_event_coordinator.ts#L563) | Fl | 按 Fl 合同；状态与执行判断归对应 owner |
| [`applyRunState`](../frontend/src/run_event_coordinator.ts#L575) | Fl | 按 Fl 合同；状态与执行判断归对应 owner |
| [`reconcileRunState`](../frontend/src/run_event_coordinator.ts#L608) | Ft | → `this.deps.transport.reconcileRunState` |
| [`acceptRunResponse`](../frontend/src/run_event_coordinator.ts#L612) | Ft | → `this.deps.transport.acceptRunResponse` |
| [`connectEvents`](../frontend/src/run_event_coordinator.ts#L616) | Ft | → `this.deps.transport.connect` |
| [`observeQueuedInput`](../frontend/src/run_event_coordinator.ts#L620) | Fe | 按 Fe 合同；状态与执行判断归对应 owner |
| [`observeUserQuestion`](../frontend/src/run_event_coordinator.ts#L657) | Fe | 按 Fe 合同；状态与执行判断归对应 owner |
| [`recordRunState`](../frontend/src/run_event_coordinator.ts#L676) | Fl | 按 Fl 合同；状态与执行判断归对应 owner |

### frontend/src/run_projection.ts

| 函数/入口 | 合同 | 实际委托或责任 |
|---|---|---|
| [`constructor`](../frontend/src/run_projection.ts#L17) | Fl | 按 Fl 合同；状态与执行判断归对应 owner |
| [`primaryRun`](../frontend/src/run_projection.ts#L19) | Fl | 按 Fl 合同；状态与执行判断归对应 owner |
| [`size`](../frontend/src/run_projection.ts#L24) | Fl | 按 Fl 合同；状态与执行判断归对应 owner |
| [`isActive`](../frontend/src/run_projection.ts#L28) | Fl | → `this.activeRuns.has` |
| [`isSessionRunning`](../frontend/src/run_projection.ts#L32) | Fl | → `[...this.activeRuns.values()].includes` |
| [`runIdForSession`](../frontend/src/run_projection.ts#L36) | Fl | 按 Fl 合同；状态与执行判断归对应 owner |
| [`entries`](../frontend/src/run_projection.ts#L43) | Fl | 按 Fl 合同；状态与执行判断归对应 owner |
| [`restoreTerminal`](../frontend/src/run_projection.ts#L47) | Fl | 按 Fl 合同；状态与执行判断归对应 owner |
| [`transition`](../frontend/src/run_projection.ts#L51) | Fl | 按 Fl 合同；状态与执行判断归对应 owner |
| [`forgetAdmission`](../frontend/src/run_projection.ts#L62) | Fl | 按 Fl 合同；状态与执行判断归对应 owner |
| [`turnIdForRun`](../frontend/src/run_projection.ts#L68) | Fl | 按 Fl 合同；状态与执行判断归对应 owner |
| [`ensureTurnId`](../frontend/src/run_projection.ts#L80) | Fl | 按 Fl 合同；状态与执行判断归对应 owner |

### frontend/src/run_stream_buffer.ts

| 函数/入口 | 合同 | 实际委托或责任 |
|---|---|---|
| [`constructor`](../frontend/src/run_stream_buffer.ts#L17) | Fe | 按 Fe 合同；状态与执行判断归对应 owner |
| [`enqueue`](../frontend/src/run_stream_buffer.ts#L25) | Fe | 按 Fe 合同；状态与执行判断归对应 owner |
| [`flush`](../frontend/src/run_stream_buffer.ts#L30) | Fe | 按 Fe 合同；状态与执行判断归对应 owner |

### frontend/src/run_lifecycle.ts

| 函数/入口 | 合同 | 实际委托或责任 |
|---|---|---|
| [`isRunSuspension`](../frontend/src/run_lifecycle.ts#L3) | Fl | 按 Fl 合同；状态与执行判断归对应 owner |
| [`isTerminalRun`](../frontend/src/run_lifecycle.ts#L7) | Fl | 按 Fl 合同；状态与执行判断归对应 owner |
| [`lastRunState`](../frontend/src/run_lifecycle.ts#L12) | Fl | 按 Fl 合同；状态与执行判断归对应 owner |
| [`current`](../frontend/src/run_lifecycle.ts#L37) | Fl | → `this.rows.get` |
| [`forgetAdmission`](../frontend/src/run_lifecycle.ts#L41) | Fl | 按 Fl 合同；状态与执行判断归对应 owner |
| [`restoreTerminal`](../frontend/src/run_lifecycle.ts#L46) | Fl | 按 Fl 合同；状态与执行判断归对应 owner |
| [`transition`](../frontend/src/run_lifecycle.ts#L60) | Fl | 按 Fl 合同；状态与执行判断归对应 owner |
| [`lifecycleState`](../frontend/src/run_lifecycle.ts#L116) | Fl | 按 Fl 合同；状态与执行判断归对应 owner |
| [`eventSequence`](../frontend/src/run_lifecycle.ts#L125) | Fl | 按 Fl 合同；状态与执行判断归对应 owner |

### frontend/src/sse.ts

| 函数/入口 | 合同 | 实际委托或责任 |
|---|---|---|
| [`saveCommitCursor`](../frontend/src/sse.ts#L38) | Ft | 按 Ft 合同；状态与执行判断归对应 owner |
| [`rememberCommitCursor`](../frontend/src/sse.ts#L49) | Ft | 按 Ft 合同；状态与执行判断归对应 owner |
| [`handleAndAcknowledge`](../frontend/src/sse.ts#L55) | Ft | 按 Ft 合同；状态与执行判断归对应 owner |
| [`init`](../frontend/src/sse.ts#L60) | Ft | 按 Ft 合同；状态与执行判断归对应 owner |
| [`displayRunStatus`](../frontend/src/sse.ts#L64) | Ft | 按 Ft 合同；状态与执行判断归对应 owner |
| [`clearReconnectTimer`](../frontend/src/sse.ts#L70) | Ft | 按 Ft 合同；状态与执行判断归对应 owner |
| [`scheduleReconnectStatus`](../frontend/src/sse.ts#L76) | Ft | 按 Ft 合同；状态与执行判断归对应 owner |
| [`reconcileRunState`](../frontend/src/sse.ts#L84) | Ft | 按 Ft 合同；状态与执行判断归对应 owner |
| [`ingestServerEvent`](../frontend/src/sse.ts#L125) | Ft | 按 Ft 合同；状态与执行判断归对应 owner |
| [`eventKey`](../frontend/src/sse.ts#L143) | Ft | 按 Ft 合同；状态与执行判断归对应 owner |
| [`acceptRunResponse`](../frontend/src/sse.ts#L156) | Ft | 按 Ft 合同；状态与执行判断归对应 owner |
| [`connect`](../frontend/src/sse.ts#L183) | Ft | 按 Ft 合同；状态与执行判断归对应 owner |
