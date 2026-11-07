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
- [x] 让 `task_submit.py` 的 admission/spawn/slot 路径停止访问内部锁和私有字段。
- [ ] 保留兼容代理，迁移完成后再删除。

第二阶段首批只收敛运行槽位、输入唤醒和 shell gate 的所有权；复杂的
`request_stop`、`finish_run` 和审批投影仍保留在 `AppContext`，下一批处理。

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

### 第三阶段：DurableSession 查询门面

- [ ] 抽出 conversation/history 查询。
- [ ] 抽出 task/effect/artifact 查询。
- [ ] 保留统一 session kernel 和 mutation line。

### 第四阶段：DurableSession 写入门面

- [ ] 抽出 effect/artifact 写入。
- [ ] 抽出 approval 操作。
- [ ] 抽出 task lifecycle 操作。
- [ ] 最后处理 submission 和 generation boundary。

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
