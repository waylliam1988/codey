# 经历双角色预算：运行前契约（2026-11-18）

本轮新增的用户可感知行为：接续项目经历时，即使历史用户请求很长，仍能看到同一回合
助手记录的选择原因或结果片段。消费者为既有普通 Chat 与跨会话 project writer 的
`ghost_experiences`，不新增消费者、偏好确认或模型调用。

## 假设与范围

当前 `observation_index._render_single_block` 先让 user_text 使用全部剩余预算，
assistant_text 只有剩余空间才显示。评分却同时读取两个字段。因此助手字段独有的
检索命中可选中一条只显示无关用户前缀的经历，阻塞正常主调用理解过去的结果。

主要变量仅为单条经历内双方文本的预算分配：存在双方文本时保留两个有标签片段，
先均分内容空间，短字段剩余空间让给另一方；空间不足以表示双方至少一个字符时跳过。
保留前缀截断和省略号、整个 block 字符上限、原排序/条数/检索 scope/源内容。
不承诺检索命中在超长字段尾部时仍被展示，不添加语义摘要或 query 改写。

## 冻结验收

- 固定合成经历：长用户请求 3000 字符；助手首部包含独有的 `quartzstore` 及选择原因。
- 固定预算 400、900、1800；Chat/project 两种经历模式；双方独有标记必须都在片段内，
  且完整渲染不超预算。另验短字段让位、单角色、极小预算、多条累计、原始输入不变。
- 基线与修复使用同一断言和样本；先保存未修生产的失败，再实现。没有开发/留出选参，
  这是确定性格式与实际投递契约修复，独立样本量不用于模型统计。
- 普通 App/InputQueue → 实际结算保存 → 冷重开 → Chat（同会话）或 project writer
  （新会话/同项目）→ ContextSource/admission → ApiProvider → loopback HTTP。
  原文片段必须在实际 outbound payload 中；请求数单列，不用拼接 helper 代替入口。
- 补禁用 retrieve/admit、其他 project、删除/重开、发送前撤回与消费者相关的边界。
  复用既有删除、receipt、窗口、过期与恢复回归；无新增状态，不另造过期协议。
- 失败归因：生产契约失败保留；fixture/runtime 接口误用单列，修夹具不删行为断言。
- 成功条件：原失败断言全部通过、相关生产回归及全树静态门通过；缺运行不称通过。
  不跑全量 pytest；不加载 HF/embedding，不调用外部生成服务。

目标模型：本轮无实机行为实验；loopback 确定性响应仅验证传输，模型名称使用隔离测试值。
不声称模型行为净收益、个人学习或 World Model 已完成。修复默认沿用既有检索入口；
关闭 retrieve_projections/admit_hints 可停止消费，回滚只需撤销渲染改动，不迁移/删除 state。

## 消费者依赖与后续

真实链路：InputQueue canonical receipt → settlement append_completed → SQLite committed
window → scope 检索/可选显式 embedding → observation_index → ghost_context →
ContextSource/PromptEnvelope → shared kernel Attempt Gate → ApiProvider；post-turn
continuity/queue/Affinity 为独立派生消费者。人工确认偏好走 inbox → Hebbian → directive。
本修复没有新增写回理解或反馈入口；后续仍需跨会话实际行为对照与一个有来源、情境、
期限及可归因反馈的用户理解预测，比较无历史和相同经历检索，不重跑旧 World Model 候选。

受限维护窗口、旧身份、混版本/retention、真实 pending 交接等原缺口继续登记在
[逐记录维护交接](selective-maintenance-handoff-20261118.md)，不扩大本轮修复。

## 基线记录

生产仍为 `597a2204` 时，修正夹具后的同一22项验收为13通过/9失败：六项双模式/三预算、
长用户字段不给短助手留位、极小预算的单侧丢失，以及跨会话project普通入口。
最后一项还暴露独立接线缺口：`task_entry._entry_context` 默认 session 检索；旧
`project_writer_phase` 的显式 project scope 不再代表普通 shared kernel 的实际行为。
该项断言保留，不能单靠渲染修复就宣称跨会话消费者交付。应独立修复接线并验收。

首轮14失败/8通过还混有夹具问题：单字符词不进入分词、未扣标签就要求240字符，
以及把正常Chat历史中保留的旧回答误当成Ghost关闭失效。修正后只检查独立Ghost块，
不删除原行为断言。两轮完整日志/XML保存在本地
`.e2e-artifacts/ghost-experience-role-budget-20261118/`。本文件与复现测试先提交，
基线生产缺陷不作为服务阴性、模型收益或已通过生产验收。

进一步追踪后的归属纠正：普通 project 经 `project_completion_flow → project_writer_phase →
project_adapter`，writer 已正确传 project scope；实际丢失点是 `permissions.py` 没有登记
`ghost_experiences`，且 `project_adapter._project_ghost_sources` 显式传空经历。
因此前段把普通 project 归因于 `task_entry._entry_context` 不准确。保留原22项断言与
13/9基线；双角色修复后21通过/1失败。尝试过的 task_entry 类型整理及错误归属产生的
candidate日志保留，本轮已撤回全部自有 task_entry 改动，不将它们纳入生产提交。
新增 project 经历投递属于实际启用新消费者，须另冻结单模型行为对照及相关准入边界，
不能靠改scope或权限名单就宣称生产完成。
