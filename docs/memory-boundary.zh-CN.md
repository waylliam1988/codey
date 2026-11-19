# Codey Memory / Ghost 当前所有权边界

本页描述冷启动重写后的实现。此前 Ghost 研究记录保留为历史证据，不代表当前算法、存储或生产合同。

```text
Codey Core → codey.memory.contracts → codey.ghost → Ghost SQLite / 检索算法
```

Ghost 仍叫 Ghost，位于 `codey/ghost`，和 Codey 一起发行。没有 entry point、独立安装包或插件管理运行时。
只有应用装配点 `codey/app/memory_setup.py` 导入具体后端；任务入口和 provider kernel 只使用中性合同。
Ghost 不导入 app、operations、durable、runtime。移除 Ghost 实现或设置 `AppContext(memory_enabled=False)`
后使用 Null 能力，普通任务仍走同一个 Core runtime。

## 单一事实来源

- `codey/memory/contracts.py` 是公开记忆合同的唯一来源。读取、观察、审阅、管理、维护为独立能力；
  `MemoryCapabilities` 只组装能力，不增加方法或执行权。
- Core 的 durable task、工具回执、Evidence、Permission、CompletionProof 和队列仍由原所有者写入。
  Ghost 没有队列、claim/release/complete、模型调用、worker 或 scheduler。
- `finish_run` 从已提交任务和实际 assistant message 生成规范事件；Ghost 按 event_id 去重生成经验投影。
  投影失败不会回滚已完成任务，也不把失败任务投影成成功经验。没有旧观察存储双写。
  经验使用 submission 的最终 answer_entry_id，并绑定用户输入和助手答案各自的消息身份与文本 SHA-256；
  缺少完整来源时不生成经验，读取和发送前都要求两条消息仍在来源会话的当前分支中且摘要一致。
- 用户提案和决定写入 Core 对话记录；Ghost 仅存投影和稳定引用。确认必须匹配 proposal_id 与版本；
  决定查询在同一 Core mutation line 中覆盖所有会话和分支的持久记录，唯一性不依赖当前请求会话。
  投影失败后跨会话或重启重试仍复用原决定和原来源，不能以相反决定覆盖。
  助手文本不会自动成为长期偏好、目标或假设。

## 上下文安全边界

```text
MemoryItem → ContextCandidate → Core 渲染与预算 → ContextAdmission / ContextEpoch → provider payload
```

Ghost 返回带来源、scope、时间的结构化 item，不返回 prompt。Core 检查来源仍在当前对话分支、scope、
创建时间、有效期及数量，按完整 item 选取预算后渲染为历史数据。普通请求总记忆预算为 6000 字符，
定义在 `workspace/context_admission.py`。位置登记使失效候选只能在请求冻结前按所属区段移除。
物理发送继续检查真实 payload、来源有效性、scope、过期与预算；文本、原生工具结果续发和 compaction 共用边界。
这些候选不构成事实裁决、授权、执行收据或完成证明。
`memory_proposal` 和 `memory_decision` 仅用于审计和来源校验；它们在历史编辑、预算及消息编码之前
就被排除，不能作为 system 消息或通过 payload 内的 context_edits 改写模型历史。

## 存储和维护

当前文件为 `<state_home>/memory/ghost/memory.sqlite3`，不读、不迁移旧 `ghost.sqlite3`。
Ghost 自己的事件去重、item 和提案使用同一 SQLite 事务；与 durable.sqlite3 没有共享表、外键或跨库事务。
最新 200 条的检索窗口只限制经历；已确认偏好、目标、假设单独进入候选集，再共同检查有效期、排序和
应用最终数量与 Core 预算限制。新增经历不会隐式遗忘仍有效的长期项，也没有新增重复分类字段或存储。
删除 scope 或会话来源会删除投影，保留 event_id 防止重放复活。存储损坏不触发自动修复或猜测迁移。

当前算法是本地词法匹配、排序和时间衰减的冷启动基线。旧图、Hebbian、Hopfield、自动提取和历史学习
没有保留等价实现，也没有在本轮重新证明模型质量收益。后续研究在 Memory 合同后演进。
维护由 Core 显式调用 `memory.maintain(as_of=...)`；目前 CLI 提供入口，没有第二套 daemon 或自动周期维护。

```text
python -m codey ghost export --state-home <目录>
python -m codey ghost propose preference "使用简洁示例" --session-id <已有会话>
python -m codey ghost accept <proposal_id> <version> --session-id <已有会话>
python -m codey ghost reject <proposal_id> <version> --session-id <已有会话>
python -m codey ghost delete_scope --session-id <已有会话> --project <项目目录>
python -m codey ghost maintain --state-home <目录>
```

HTTP 及抽屉保留 Ghost 产品名称，DTO 为 items/pending；没有旧控制面或旧队列 DTO 兼容。
管理失败返回可见错误，不能声称已经保存。导出是本地全量管理操作。

## 当前限制

记忆与 Core 同进程，隔离是依赖与能力边界，不是进程隔离。SQLite 锁等待有界，算法接受协作取消；
任意第三方实现永久阻塞不能靠 Protocol 或异常捕获强行终止。检索、观察、维护异常会省略或记录失败。
冻结后、物理发送前发生来源撤回或后端失效时，发送边界保守拒绝该请求；不会修改已冻结请求字节，
但也不保证这一竞态下任务自动重建并无缝继续。恢复冻结请求时仍需当前来源验证。

自动化验收入口是 `tests/test_memory_boundary.py`、`tests/test_memory_attempt_gate_lifecycle.py`，以及任务、
审批、完成、研究恢复和进程故障回归。本轮未发起真实模型质量对照，未声称新基线达到旧算法收益。
