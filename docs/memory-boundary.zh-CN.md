# Codey Memory / Ghost 当前所有权边界

本页描述冷启动重写后的实现及本轮的日志、控制、纠正和反馈扩展。此前 Ghost 研究记录保留为历史证据，不代表当前算法、存储或生产合同。

```text
Codey Core → codey.memory.contracts → codey.ghost → Ghost SQLite / 检索算法
```

Ghost 仍叫 Ghost，位于 `codey/ghost`，和 Codey 一起发行。没有 entry point、独立安装包或插件管理运行时。
只有应用装配点 `codey/app/memory_setup.py` 导入具体后端；任务入口和 provider kernel 只使用中性合同。
Ghost 不导入 app、operations、durable、runtime。移除 Ghost 实现或设置 `AppContext(memory_enabled=False)`
后使用 Null 能力，普通任务仍走同一个 Core runtime。

## 单一事实来源

- `codey/memory/contracts.py` 是公开记忆合同的唯一来源。读取、观察、审阅、管理、维护、控制、生命周期和学习为独立能力；
  `MemoryCapabilities` 只组装能力，不增加方法或执行权。
- Core 的 durable task、工具回执、Evidence、Permission、CompletionProof 和队列仍由原所有者写入。
  Ghost 没有队列、claim/release/complete、模型调用、worker 或 scheduler。
- `finish_run` 从已提交任务和实际 assistant message 生成规范事件；Ghost 按 event_id 去重生成经验投影。
  事件时间来自原始终态 commit，重复通知复用相同身份、时间和载荷。投影失败不会回滚已完成任务，
  也不把失败任务投影成成功经验。没有旧观察存储双写。
  经验使用 submission 的最终 answer_entry_id，并绑定用户输入和助手答案各自的消息身份与文本 SHA-256；
  缺少完整来源时不生成经验，读取和发送前都要求两条消息仍在来源会话的当前分支中且摘要一致。
- 用户提案和决定写入 Core 对话记录；Ghost 仅存投影和稳定引用。确认必须匹配 proposal_id 与版本；
  决定查询在同一 Core mutation line 中覆盖所有会话和分支的持久记录，唯一性不依赖当前请求会话。
  投影失败后跨会话或重启重试仍复用原决定和原来源，不能以相反决定覆盖。
  助手文本不会自动成为长期偏好、目标或假设。提案、管理动作和清空请求支持调用方 request_id；
  投影失败重试复用原始时间与命令，相同身份不能改写载荷。已经提交的提案过期后重试不会变成新提案。

## 上下文安全边界

```text
MemoryItem → ContextCandidate → Core 渲染与预算 → ContextAdmission / ContextEpoch → provider payload
```

Ghost 返回带来源、scope、时间的结构化 item，不返回 prompt。Core 检查来源仍在当前对话分支、scope、
创建时间、有效期及数量，按完整 item 选取预算后渲染为历史数据。普通请求总记忆预算为 6000 字符，
定义在 `workspace/context_admission.py`。位置登记使失效候选只能在请求冻结前按所属区段移除。
物理发送继续检查真实 payload、来源有效性、scope、过期与预算；文本、原生工具结果续发和 compaction 共用边界。
这些候选不构成事实裁决、授权、执行收据或完成证明。
所有 `memory_*` 管理记录仅用于审计和来源校验；它们在历史编辑、预算及消息编码之前
就被排除，不能作为 system 消息或通过 payload 内的 context_edits 改写模型历史。

## 存储和维护

当前文件为 `<state_home>/memory/ghost/memory.sqlite3`，不读、不迁移旧 `ghost.sqlite3`。
`events` 保存规范化的完整 MemoryEvent、连续序号和哈希链；`journal_head` 检测尾部丢失。
`projections` 是这份日志的派生状态，包含 item、提案、四开关、删除水位、曝光、反馈、预测和结果引用。
事件、head 和投影使用同一 SQLite 事务；与 durable.sqlite3 没有共享表、外键或跨库事务。
重建先验证完整序列和 hash，再在单事务内清空和重放投影，使用事件自带时间、不读取当前时钟；
返回事件数及排序后的投影 digest。相同 event_id 的不同载荷被拒绝，损坏重建不替换原投影。
这是 MemoryEvent 日志的确定性重建，不是把 Ghost 日志当成 Core 执行事实的恢复源。
最新 200 条的检索窗口只限制经历；已确认偏好、目标、假设单独进入候选集，再共同检查有效期、排序和
应用最终数量与 Core 预算限制。新增经历不会隐式遗忘仍有效的长期项，也没有新增重复分类字段或存储。
删除 scope 或会话来源会清理相应投影及依赖它的反馈、预测、曝光和判断，保留删除水位与来源 tombstone，
防止延迟事件和重放复活。乱序的删除请求不能回退水位。清空投影不会擦除事件日志或 Core 原始对话；
需要删除本地全部数据时仍须处理其原始存储，不能把 reset 解释成物理擦除。
上一版仅有事件 ID 的测试库不迁移，明确报错要求重置测试状态。存储损坏不触发自动修复或猜测迁移。

## 四开关与纠正生命周期

user、project、session 可分别管理 `accept_events`、`retrieve_projections`、`run_maintenance`、`admit_hints`。
请求同时检查适用的 user/project/session 控制，更窄 scope 不能覆盖父级 deny。
接收关闭时任务仍由 Core 正常结算，Ghost 不消费为经验；重新开启不会重学当时拒收的事件。
检索关闭不影响本地管理导出；投递关闭仍可检索但不进入 Context Epoch；维护按每条记录原始控制 scope 检查，
显式删除、清空和用户管理动作仍可执行。发送前还会复核投递控制和候选快照。

提案可带 `slot`、`context`、`expires_at`、`supersedes`。只有显式确认才进入 active；同 kind/scope/slot/context
的旧项变为 superseded，显式替代目标必须属于同 kind 和 scope。跨情境项可以并存，限定情境不符时不投递。
还支持 revoked、quarantined、expired；被替代或已过期项不能直接 restore 绕过新确认。
这是显式槽位冲突处理，没有声称自由文本的语义矛盾已能自动识别。

## 请求解释与反馈

Core 从已结算 provider 请求工件读取真实曝光，从 settlement 读取有界的工具/provider 状态；未知保持 unknown。
每次冻结请求保存逐候选 trace，包含来源、scope、family 标签、理由、score、rank 和字符预算。
`explain` 是当前查询预览；`explain_run` 读取原始请求 trace，不用今天的检索结果冒充过去的投递。
family 当前仅为解释标签，没有实现整族晋级/隔离状态机。实际物理发送仍由既有 admission 决定。

明确反馈必须绑定某个已完成 run 中实际曝光的 item，一项记忆在同一 run 只接受一次判断；请求重试不重复记账。
`correction` 隔离相应 active item；helpful/unhelpful 保存可归因反馈，不把一次满意认定为事实、授权或任意奖励。
预测须绑定 active item、预期反馈和未来窗口，到期复盘必须引用窗口内同 item/scope 的反馈；
无观测保持 unjudged，不能自行标记 supported。这是用户反馈的有限预测记账，不是完整 World Model。

当前算法是本地词法匹配、排序和时间衰减的冷启动基线。旧图、Hebbian、Hopfield、自动提取和历史学习
没有保留等价实现。新基线已做一次六组成对实机试验，记忆组行为代理指标 6/6、对照 3/6；
样本与成本、首次失败、原始请求回执及剩余缺口见[本轮报告](ghost_progress/memory-evolution-20261121.zh-CN.md)。
这不证明统计泛化或学习机制优势。后续研究在 Memory 合同后演进。
维护由 Core 显式调用 `memory.maintain(as_of=...)`；目前 CLI 提供入口，没有第二套 daemon 或自动周期维护。

```text
python -m codey ghost export --state-home <目录>
python -m codey ghost propose preference "使用简洁示例" --session-id <已有会话>
python -m codey ghost accept <proposal_id> <version> --session-id <已有会话>
python -m codey ghost reject <proposal_id> <version> --session-id <已有会话>
python -m codey ghost delete_scope --session-id <已有会话> --project <项目目录>
python -m codey ghost maintain --state-home <目录>
python -m codey ghost control admit_hints off --scope-kind user --session-id <已有会话>
python -m codey ghost explain "本次任务" --session-id <已有会话>
python -m codey ghost explain_run --run-id <run> --session-id <已有会话>
python -m codey ghost feedback <item> --run-id <run> --rating correction --session-id <已有会话>
python -m codey ghost rebuild --session-id <已有会话>
```

HTTP 及抽屉保留 Ghost 产品名称；支持 scope 选择、四开关、提案有效期/情境/替代、停用/恢复和曝光反馈。
DTO 使用 items/pending/records/controls/exposures；没有旧控制面或旧队列 DTO 兼容。
管理失败返回可见错误，不能声称已经保存。导出是本地全量管理操作。

## 当前限制

记忆与 Core 同进程，隔离是依赖与能力边界，不是进程隔离。SQLite 锁等待有界，算法接受协作取消；
任意第三方实现永久阻塞不能靠 Protocol 或异常捕获强行终止。检索、观察、维护异常会省略或记录失败。
冻结后、物理发送前发生来源撤回或后端失效时，发送边界保守拒绝该请求；不会修改已冻结请求字节，
但也不保证这一竞态下任务自动重建并无缝继续。恢复冻结请求时仍需当前来源验证。

自动化验收入口还包括 `tests/test_memory_lifecycle.py`、`tests/test_memory_feedback.py`，以及任务、
审批、完成和研究恢复回归。自动同次候选提取、全自动个人适应、整族晋级、延迟回归/repair 学习及
Hebbian 等研究机制仍未实现，不能把本次基础设施和显式反馈记账称作整份路线图完成。
