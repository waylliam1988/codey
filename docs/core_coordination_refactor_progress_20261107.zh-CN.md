# 核心协调层重构进度

本文件记录 `AppContext`、`DurableSession` 和前端运行时的职责收敛。
目标是降低跨持久化、调度、审批、执行效果和 UI 投影的理解成本，同时保持现有
事务、锁、恢复和事件顺序。每个阶段独立验证并提交；不运行全量 pytest。

## 不可改变的约束

- durable 写入继续经过同一个 mutation line 和底层 store transaction。
- 读取、推导、提交仍然共享 `DurableSession` 的写锁。
- approval claim 与 effect intent 继续在一个 durable mutation 中完成。
- Stop 继续使用 shell spawn gate，并保持 gate、AppContext lock 和 scheduler abort 的顺序。
- durable commit 是权威事实；JSONL、EventBus、SSE 和浏览器状态都是投影。
- 前端继续按数据库身份、commit sequence、task revision 和事件序列处理恢复与去重。
- 新增和迁移代码使用明确类型，不使用 `Any`；不得通过防御性 fallback 隐藏契约错误。

## 阶段

### 第一阶段：契约边界

- [x] 为任务准入、审批和效果结算建立精确 Protocol。
- [x] 为 durable execution fencing 和 commit observer 建立精确 Protocol。
- [x] 用运行时结构化类型测试确认 `DurableSession` 满足 durable 业务契约。
- [x] 记录当前调用方和事务/锁边界。

第一阶段实现了 `codey.durable.contracts`。协议只描述能力，不拥有锁或 store；
`DurableSession` 仍然是唯一的事务协调者。下一批会让 AppContext 的协调器依赖这些
能力切片，并保留旧入口作为兼容代理。

### 第二阶段：AppContext 协调器

- [x] 抽出任务运行协调器。
- [x] 抽出审批协调器。
- [x] 抽出 durable event bridge。
- [x] 抽出任务终结、停止和审批过期协调器。
- [x] 让 `task_submit.py` 的 admission/spawn/slot 路径停止访问内部锁和私有字段。
- [ ] 保留兼容代理，迁移完成后再删除。

第二阶段首批先收敛运行槽位、输入唤醒和 shell gate 的所有权；随后任务生命周期批次
又迁移了 `request_stop`、`finish_run` 和审批过期流程，旧入口仍由 `AppContext` 代理。

第二阶段首批回归：`tests/test_task_runtime_coordinator.py`、
`tests/test_task_submit.py`、`tests/test_task_submit_durable.py` 和
`tests/durable/test_scheduler_abort_handler.py` 共 34 passed。第一次命令误用了
不存在的 `tests/test_task_submission.py`，按实际路径重跑后通过；属于测试命令路径问题。

审批协调器批次把 shell approval 的内存投影、durable fact bridge、claim 和 receipt
恢复从 `AppContext` 移到 `ShellApprovalCoordinator`；`AppContext` 保留同名兼容代理。
审批相关定向回归（含任务提交和 shell approval workspace 保持测试）：43 passed；
相关 Ruff 与 mypy 通过。

durable event bridge 批次把 commit record 的延迟发布队列和 flush 逻辑移到
`DurableEventBridge`；`AppContext` 仍保留 `_publish_durable_commit` 和
`_flush_deferred_durable_events` 兼容代理，以保持 `DurableAppServices` 装配接口稳定。
桥接单测与 EventBus/server 回归共 212 passed；相关 Ruff 与 mypy 通过。

任务生命周期批次把 `finish_run`、`request_stop`、全量审批过期和旧运行审批过期
移到 `TaskLifecycleCoordinator`；`AppContext` 保留旧入口。协调器只持有显式依赖，
并通过动态 gate 访问器保留 shell executor 与 Stop 对同一把 gate 的线性化语义。
停止流程仍在 gate 和 `AppContext.lock` 内完成取消/审批变更，在释放两把锁后调用
scheduler abort，再发布 shell result 和延迟 durable 事件。任务生命周期、审批、停止、
任务提交和 scheduler abort 扩大回归共 111 passed；相关 Ruff 和局部 mypy 通过。

生命周期批次复查发现审批过期代理传入 `output=None`，覆盖 registry 默认的过期文案。
新增 `test_expiry_preserves_the_default_shell_result_output` 先复现 `None` 输出，再将接口
默认值恢复为明确的字符串。该问题属于本次迁移引入的生产回归，不修改原有预期。
生命周期与 scheduler abort 回归 15 passed，相关 Ruff、mypy 和 Pyrefly 通过。

重启审批测试的间歇失败已在未修改基线 `39516652` 复现。失败数据库证明 source task
从测试构造的 RUNNING 被启动恢复回收成 PENDING，审批 claim 按合同拒绝新 effect。
生产会先 park source task，测试漏了这一状态转换；现补上 `wait_task`，并在重启后断言
WAITING，再验证只执行未 claim 的命令一次。属于测试夹具未反映生产流程，不放宽效果准入。
更新后任务生命周期、审批、Stop 和 scheduler abort 回归 49 passed。

查询门面首批把 `conversation_ids`、head/存在性、conversation tree/lineage、
history/navigation/window、inbox、context 和 snapshot 查询移到
`ConversationQueryFacade`。`DurableSession` 只保留兼容代理；所有查询统一通过
`_query_line` 共享原有 `RLock`。这也修复了 `history_window` 原本绕过 mutation line
的确定性并发缺口。durable 查询与历史回归共 143 passed；相关 Ruff、mypy 和
Pyrefly 通过。扩大回归中有一个前端 Playwright 滚动/会话切换断言失败，单独运行也能
复现；尚未完成基线对照与根因分析，不能据此断言是波动或与重构无关。

### 第三阶段：DurableSession 查询门面

- [x] 抽出 conversation/history 查询。
- [x] 抽出 task/effect/artifact 查询。
- [x] 保留统一 session kernel 和 mutation line。

任务查询批次新增 `TaskQueryFacade`，拥有 task/timeline/historical snapshot、effect、
artifact、approval 和 task/effect 一致性联合读取；pending tasks 同归该门面。
两个门面共用具名 `QueryLine` 契约，由 session 执行全部读操作，未新增锁或数据库连接。
任务查询门面依赖 store 和 query line，不反向依赖 `DurableSession`。
task/effect/approval/artifact/fencing 定向回归 121 passed；Ruff、mypy、Pyrefly 通过。
SQLite 并发写入与重启审批补充回归 2 passed，未运行全量 pytest。

共享内核批次将 store、唯一 RLock、失败/关闭状态、attempt binding、commit publication
及订阅归属到 `SessionKernel`。`DurableSession` 继承同一个内核作为兼容桥，并未创建
第二个锁或连接。会话操作只依赖内核，消除了 `conversation_ops.py` 的显式 Any。
新增直接调用领域查询门面的测试，确认关闭和存储失败会同时封闭所有门面。
内核定向回归 56 passed，扩大 durable 回归 208 passed；新增封闭状态用例 4 passed。
全生产树 mypy（633 files）通过；相关 Ruff、Pyrefly 和 diff 检查通过。

### 第四阶段：DurableSession 写入门面

- [x] 抽出 effect/artifact 写入。
- [x] 抽出 approval 操作。
- [x] 抽出 task lifecycle 操作。
- [ ] 最后处理 submission 和 generation boundary。

效果与审批批次新增 `EffectOperations`、`ApprovalOperations`。两者通过
`SessionOperations` 加入同一个 kernel control line；不依赖公共 session，也不各自
创建锁。兼容入口逐一转发，审批 claim/intent 和 settlement/tool result/wakeup 的
单次 mutation 原样保留。attempt 释放提取为纯函数供后续生命周期操作共享。
新增直接门面/兼容入口的 Memory/SQLite 原子性覆盖，以及关闭/失败后写入拒绝测试。
扩大效果、artifact、审批与 fencing 回归 74 passed、2 skipped；新增和审批回归
27 passed。相关 Ruff、mypy、Pyrefly 通过。

任务生命周期批次把状态元数据、不可变 policy/reviewer、工作流迁移、调度 claim/recovery、
取消 barrier、owned subtree drain 和 completion/finalization 收敛到
`TaskLifecycleOperations`。其内部相互调用保持原状态机，读取和提交全过程继续持有
kernel mutation line；公共 session 仅代理。任务所有权、生命周期、fencing、工作流、
session、审批与恢复回归 83 passed。扩大 mypy 暴露了原审批测试的 optional store 与
未标注 fixture 问题；补充具体 AppContext/返回值类型和测试前置断言后检查通过，
没有增加 Any 或改变生产合同。

### 第五阶段：前端状态与运行投影

- [x] sidebar 状态归属。
- [x] run projection 归属。
- [x] stream buffer 归属。
- [ ] 抽出会话/项目状态 store。
- [ ] 抽出 SSE、snapshot 和运行事件协调器。
- [ ] 让 `app_runtime.ts` 主要负责装配和 UI 绑定。

## 基线验证

2026-11-07：

- durable 定向测试：44 passed。
- 第一阶段契约回归：45 passed。
- 相关 Python Ruff：通过。
- 前端 TypeScript：通过。
- 前端既有 projection/sidebar 定向测试：6 passed。

## 失败分类规则

测试失败后先在当前基线复现：

1. 如果失败在未修改的基线中同样出现，检查测试假设是否落后于当前合同；
2. 如果只在修改后出现，先按生产回归处理；
3. 只有当前行为与已冻结合同不一致时才更新测试；
4. 每个修复记录根因、选择的验证集和是否改变生产行为。
