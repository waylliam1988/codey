# Codey Memory / Ghost 当前所有权边界

本页描述当前生产所有权。新增候选、默认消费、维护、恢复、研究脚本与实机限制见[本轮交付](ghost_progress/memory-completion-20261121.zh-CN.md)。此前 Ghost 研究记录保留为历史证据，不代表当前算法、存储或生产合同。

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
MemoryEvent、连续序号、哈希链和 journal_head 是持久更新路径；projections、控制、学习统计和游标由它派生。
每次更新与投影共用 SQLite 事务，不与 durable.sqlite3 共享表或外键。

维护注册在现有 Core scheduler 的中性 idle deadline 上；没有 Ghost daemon、线程或队列。
每次使用确定逻辑时间、身份、策略版本、处理上限与日志内游标；正常批次通过 SQL LIMIT 有界读取。
Core 从自己的提交记录补读失败的可选终态投影，消费成功后推进日志内观察游标。
`start_scheduler=False` 禁用自动维护；维护错误不回滚已结算任务。

保留策略和压缩建立版本化恢复基点，原子安装后截断旧事件正文。默认保留 1000 条/scope 经历、180 天经历、
30 天归档、1000 个有界重试身份摘要；已确认 active 条目保留。旧命令在重试窗口外明确拒绝。
压缩后从基点与后续日志恢复当前状态，不再承诺重放已经物理清除的原始历史。
SQLite secure_delete 与 VACUUM 处理 Ghost 文件；reset 是逻辑删除，物理清理需 compact。
Core 对话、请求工件和用户备份仍由原所有者删除，Ghost 不跨库删表。
完整基点和显式全局压缩仍随必要状态增长，不宣称常量内存或任意负载下的硬容量上限。

## 四开关与纠正生命周期

user、project、session 可分别管理 `accept_events`、`retrieve_projections`、`run_maintenance`、`admit_hints`。
请求同时检查适用的 user/project/session 控制，更窄 scope 不能覆盖父级 deny。
接收关闭时任务仍由 Core 正常结算，Ghost 不消费为经验；重新开启不会重学当时拒收的事件。
检索关闭不影响本地管理导出；投递关闭仍可检索但不进入 Context Epoch；维护按每条记录原始控制 scope 检查，
显式删除、清空和用户管理动作仍可执行。发送前还会复核投递控制和候选快照。

提案可带 `slot`、规范 `value`、`context`、`expires_at`、`supersedes`。只有显式确认才进入 active；同 kind/scope/slot/context
的旧项变为 superseded，显式替代目标必须属于同 kind 和 scope。跨情境项可以并存，限定情境不符时不投递。
还支持 revoked、quarantined、expired；被替代或已过期项不能直接 restore 绕过新确认。
规范槽位产生确定性关系；自由文本/同次模型建议只产生待确认 suspected_conflict，不能自动撤回。
已确认声明与真实经历默认参加正常消费，不要求用户先做类别晋级或填写实验报告。类别仍支持独立降级、隔离、删除和有证据的重新晋级；发送前复核状态。

## 请求解释与反馈

Core 从已结算 provider 请求工件读取真实曝光，从 settlement 读取有界的工具/provider 状态；未知保持 unknown。
每次冻结请求保存逐候选 trace，包含来源、scope、family 标签、理由、score、rank 和字符预算。
`explain` 是当前查询预览；`explain_run` 读取原始请求 trace，不用今天的检索结果冒充过去的投递。
family 有独立状态机；trace 冻结版本、状态、策略、冲突、预算和挤出关系。Core 另存真实物理尝试的 admission、payload 摘要和字符位置，explain_run 连接展示，保持原始冻结记录不可变。

曝光身份绑定原始请求、item 和完整版本摘要；反馈可以进一步绑定同请求的可观察行为。
整体 run/item 反馈只表示 exposure-only，不能冒充行为归因。重复反馈不重复计数，没有反馈不增加满意数。
`correction` 隔离相应 active item；情境理解仍走待确认候选，不自动覆盖用户确认。

同次结构化主调用提供可选 memory_candidates；严格校验原始用户文本和区间，只投影实际已交付完成回答。
元数据失败不触发正文协议重试；整个 JSON 损坏仍失败。经历来自真实终态，不允许模型自行提案为 experience。
受测本地模型尚不能可靠提取合法候选，人工提案/确认正常可用；不能把助手承诺理解成保存成功。

预测目标支持反馈、代码围栏语言行为和正式验证观察，匹配版本、请求、时间窗口与工作区版本。
到期维护自动产生 supported/contradicted/unjudged 复盘；没有观测保持 unjudged。
修复经验使用已有失败/action/check 引用和显式 credit link，延迟回归复核相同 check 与版本。
它们可以产生待确认假设和可追溯 hint，不能提供新的任务完成证明。

生产检索使用词法、时间衰减和有界明确反馈统计。Hebbian、eligibility、循环状态等研究机制均只在
`tools/memory_plasticity_mechanisms.py` 与独立 probe 中，不在生产包、不装配后端或生产路由。
默认生产路径已完成八组成对小规模实机复核，记忆行为指标 5/8、对照 0/8；原始失败、成本及限制见[本轮报告](ghost_progress/memory-completion-20261121.zh-CN.md)。

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
DTO 包括 items/pending/records/controls/exposures、精确行为、类别、预测和修复状态；没有旧控制面或旧队列 DTO 兼容。
管理失败返回可见错误，不能声称已经保存。导出是本地全量管理操作。

## 当前限制

记忆与 Core 同进程，隔离是依赖与能力边界，不是进程隔离。SQLite 锁等待有界，算法接受协作取消；
任意第三方实现永久阻塞不能靠 Protocol 或异常捕获强行终止。检索、观察、维护异常会省略或记录失败。
冻结后、物理发送前发生来源撤回或后端失效时，发送边界保守拒绝该请求；不会修改已冻结请求字节，
但也不保证这一竞态下任务自动重建并无缝继续。恢复冻结请求时仍需当前来源验证。

验收覆盖正常 headless/API 同次提取、确认、使用、纠正、重建与删除，以及真实 scheduler 空闲维护。
本轮验证与剩余的模型可靠性、通用预测、长期效果限制见[交付报告](ghost_progress/memory-completion-20261121.zh-CN.md)和根目录 TEST_REPORT.md。
