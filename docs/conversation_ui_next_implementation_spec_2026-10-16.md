# 中间对话区下一阶段实施规格：错误、复制、图表、草稿、实时输出、完整历史

日期：2026-10-16。交接对象：实现与测试同事。

**本文是待实施规格，不是已完成功能声明。** 当前源码入口已经核对；下文新增的类型、表和接口是建议契约，实施时应与现有边界合并，不要照着再造平行框架。运行时记录版本保持 1；本文不授权修改模型协议历史，也不授权自动重跑命令。

## 0. 顺序、共同约束与交付方法

### 2026-10-16 实施状态（实测）

本规格已完成 A-F 的首轮工程实现和本地 Codey HTTP/Playwright 验收。A 的 provider 公开投影同时保留旧 `provider_failure` 诊断，并增加有限 `failure` 映射；未知 provider 类型仍按连接失败显示，未经生命周期确认的 effect 不会生成重跑动作。D 已覆盖审批 shell 和普通 `run`，但当前观察接口仍采用客户端轮询，未承诺 SSE 专用输出通知；stdout/stderr 快照分开保存，前端为显示方便可在同一命令卡中合并复制。E 使用本地锁定 KaTeX/Mermaid，复杂图形超出预算或渲染失败保留源码。F 使用独立展示 archive，不改变模型协议历史；旧版本已经被裁掉且未进入 archive 的消息不能恢复，首屏仍为有界 DOM 分批挂载。

运行架构补充：每个 session 是独立的 admission、事件和取消主键；同一 session 内保持串行，不同 session 可并行建立模型流。Stop 只接受目标 `run_id`，项目写任务仍受独立 writer lease 保护。真实三会话浏览器回归已覆盖 Chat Completions 和 Responses 两种协议；停止目标会话时，provider 的任务取消事件和阻塞 HTTP 读取都会被传递并及时中止。

最新定向红测/绿测记录：`tests/test_execution_output.py`、`tests/test_transcript_store.py`、`frontend/src/rich_blocks.test.ts`、`frontend/src/command_card.test.ts`、`frontend/src/history_window.test.ts`、`frontend/src/user_questions.test.ts` 均通过；`tests/test_ui_manual_retry.py` 现为 23 项通过；2026-10-17 全量 pytest 为 **8784 passed / 8 skipped / 1520 subtests passed**，前端正式检查为 34 个测试文件、132 项通过。正式构建、`publish_frontend.py --check` 和真实 UI workflow/history browser 组通过。未覆盖外部模型 API、跨端口草稿迁移、所有 Mermaid 图种、完整虚拟列表和真实 Selfie 业务接口调用。

2026-10-16 后续核对发现：普通 `run` 在没有 `managed_outputs`、仅配置 `execution_outputs` 时原先没有进入观察链路；已先以独立红测复现，再修复为调用 `run_command_raw` 并传递执行身份。实时输出仍采用客户端轮询，尚未承诺专用输出 SSE；observer 合并节流、命令卡 stdout/stderr 独立呈现及重建后的连续性仍是未完成边界。

最新复核补充：不同 session 的并发 admission、按 run Stop 和浏览器 worker lane 已通过定向后端及真实三会话 HTTP/Playwright 回归；同一 session 仍拒绝第二个 active run。正式前端 34 个测试文件/132 项、typecheck、格式、构建及发布检查通过。初始化期间 composer 现在保持 disabled，待 UI state、问题和输入队列恢复完成后才解禁，避免草稿恢复与发送竞态。手工重试整组 23 项通过；首次发送准备态、接受响应后的权威确认和晚到事件均已补上红测/绿测。2026-10-17 全量 pytest 为 8784 passed / 8 skipped / 1520 subtests passed。

先核对上一轮四项缺陷的现状，不重复实现已经存在的修复。2026-10-16 本次源码核对结果（不是本轮测试通过声明）：

| 项目 | 当前源码事实 | 后续验收 |
|---|---|---|
| 确定拒绝的队列恢复 | InputOutbox 已有 Not sent、Return to draft／Remove 和拒绝后的解除阻塞路径 | 复核原回归；未通过才补独立红测和修复 |
| Research 绑定自身 run | 报告入口已通过 forRun(sessionId, runId) 查找；证据入口仍只向 ResearchDrawer 传 sessionId | 报告、证据及相关操作均绑定原 run，不能打开较新研究的内容 |
| Markdown 保留代码字面量 | 已有 token 级加粗修复及表格代码管道处理 | 覆盖代码围栏、行内代码和原文复制，不凭实现形式宣布问题已解决 |
| 图片预算 | 已取消前 20 张的硬截断，目前四路并发读取；候选文件确认仍最多 64 项 | 区分总量、并发及按需加载；未处理内容不得静默消失，预算耗尽需继续入口或解释 |

每个尚未解决的问题先提交能独立失败的测试，再修复并看到相同测试通过。不要与新功能混成一次无法归因的大改动。

之后拆成六个独立改动：优先 A 错误语义、C 提问草稿，其次 D 实时输出；B 复制范围先补身份再改入口；E Mermaid/数学分开验收；F 历史存储独立交付，不作为前五项的前置条件。不要因文件数量限制把状态机塞进 app_runtime.ts。

共同约束：

- **固定 UI 文案必须是英文**，遵守 DESIGN.md:16。本文中文说明是交接解释，不是可直接粘贴的产品字符串。用户消息、模型回答、问题正文和原始工具内容保留源语言。预览之外的中文场景名称/前后对比说明不属于产品 UI。
- **沿用现有 disclosure**：无左侧原生三角，文本/命令之后紧跟同一个 12px trailing chevron；closed 向下，open 向上。命令为 `Ran` / `Running` + 紧凑单色 command excerpt + chevron，不能整行变成 code，也不能加左侧终端图标。正式实现使用 Codey 自有 chevron 资产；不要另造浏览器默认 marker。
- `View source`、`Details` 等真正有展开正文的入口也用相同尾部 chevron。等待审批/回答是静态状态，不添加 disclosure。验收必须同时检查固定英文、marker 不存在、chevron 在内容之后且 open/closed 方向正确。
- 主对话仅有一处运行进度；等待审批/回答不是仍在执行的动画。
- 沿用现有 10px 公共圆角、13px 审批标题、单色代码、图标无黑色独立 hover 底。不要新增 Reason，不重新给助手全文加 Collapse。
- 复制只在 hover/focus 时出现，触屏保持可操作。恢复动作是用户需要看到的操作，不与复制一起隐藏。
- 正式实现复用现有组件和设计变量，不复制模拟稿 CSS：正文 14px、控件 13px，已有 12px 过程摘要保持不变；输入框沿用纸飞机发送图标、模型/努力选择和原布局；问题卡沿用 Dismiss / Confirm。新增能力不得顺带重做输入框或问题卡。
- 控制信息按需出现：草稿保存/恢复成功短暂反馈后消退；历史条目范围只在加载/定位反馈中出现；输出连接状态只在命令卡内显示。不能把三者变成常驻状态栏。持久失败及必要恢复动作保留可见。
- 每个动作绑定 session/run/call/execution 的具体身份，不能从“当前最新记录”临时推断。
- 原始输出、Markdown、图表源码是内容，不是执行授权。浏览器缓存不是审批或执行结算的权威来源。
- 显示故障、网络响应丢失与业务失败分开。重试显示不执行任务；查询状态不启动任务；重新执行必须进入现有准入/审批链路。
- 新字段必须经过 Python 投影、持久化清洗、TS 类型、恢复和正式发布产物链路。仅改类型或仅改 DOM 都不算完成。
- 建议的预算是初始工程参数，不是从基准测试得到的性能保证；实施后测量再调整。

每个改动的交付记录写清：红测名称和失败原因、绿测结果、扩大回归范围、实际 HTTP/浏览器覆盖、没覆盖什么。不能把局部模拟当作外部模型实测。

## A. 错误分类与恢复动作

### A1. 当前入口与职责

- `codey/providers/diagnostics.py` 已有 ProviderFailure、ProviderActionError、结构化阶段与事实；在这里扩展归一化，不创建另一套 provider 错误推断器。
- `codey/operations/kernel_result.py`、`kernel_execution.py` 是工具结果归一化入口；执行器应在知道异常类型/策略原因时写结构化错误。
- `codey/app/event_payloads.py` 负责有界公开投影，不能在这里猜测是否执行过。
- `frontend/src/render.ts` 现在对 command_not_allowed 特判，其余工具多为 Tool failed。移除这层基于英文文本的业务推断，改为消费明确字段。
- `frontend/src/composer.ts` 的 recoveryActions、现有 shell 审批查询与 continuation 重试是实际动作入口。保留其语义，不另写直接执行器。
- `codey/storage/ui_state_store.py` 的 MESSAGE_KEYS/_clean_message 与 `frontend/src/types/domain.ts` 必须同步；嵌套对象需专门校验，不能走默认字符串转换。

### A2. 先定义信息，再定义文案

建议错误展示字段为 `failure`，单一结构覆盖工具和运行级消息；内部可以是 frozen dataclass，公开为以下 JSON。字段使用 snake_case 通过边界转换一次，别在组件内混用：

```json
{
  "schema_version": 1,
  "code": "filesystem.not_found",
  "category": "missing_resource",
  "scope": "tool",
  "phase": "read",
  "effect": "not_started",
  "session_id": "s1",
  "run_id": "r1",
  "call_id": "c1",
  "resource": {"kind": "workspace_file", "path": "selfie/beauty/cache.py"},
  "incident_id": "failure_<opaque>",
  "diagnostic": "sanitized bounded diagnostic"
}
```

code 是有限稳定机器码；category 用于分组，不取代具体 code。effect 只能是 not_started / applied / unknown / not_applicable，由实际生命周期提供。权限拒绝可能出现在写入前，也可能发生在读取结算文件时，不能仅按 category 推断 effect。

不要把任意服务端字符串当作标题、按钮或 URL。前端用 code 到固定文案的字典；未知 code 显示“操作未完成”，保留有界诊断。连接信息只保留 source/model 的安全 ID；不要显示 key、Authorization、带凭证 URL、整段 prompt。

### A3. 文案与动作矩阵

| 条件 | 主行 | 可见动作 | 不应提供 |
|---|---|---|---|
| provider.connection_failed，且未受理 | Could not connect to model | Connection settings；符合准入条件才提供 Retry request | 自动重复提交 |
| provider.authentication_failed | Model authentication failed | Connection settings | 无条件 Retry |
| provider.rate_limited | Model rate limit reached | 有合法时间时显示 Retry after；用户主动重试 | 用轮询不断生成请求 |
| filesystem.permission_denied | Permission denied for this file | View path / Details；仅可读操作可 Reload | OS 权限错误自动改成 shell |
| filesystem.not_found | File not found or moved | View path / Reload（只读场景） | 伪造空文件或自动新建 |
| policy.approval_required | This command requires approval | View approval，定位真实卡；无记录时仅解释 shell 入口 | 在 UI 创建一个没有后端记录的 Allow |
| execution.uncertain | Command result unconfirmed | Check status / Details | Retry command |
| continuation.failed，且原命令已结算 | Command completed; continuation failed | Retry continuation | 重新执行已结算命令 |
| output.unavailable | Output unavailable | Reload output | 重新运行命令来补输出 |
| render.failed | Could not display this content | Render again / View source / Copy source | 重新生成整条回答 |

同一失败至多一个主动作，其余放详情或次级菜单。工具失败若模型仍在处理，只标该工具，不把整轮切为 Failed。状态查询、只读重读、重新显示与继续任务必须使用不同动词。

其它功能的固定文案基线：`Copy message`、`Copy final answer`、`Copy received content`、`Copied`、`Could not copy`；`Draft saved`、`Draft restored`、`Clear draft`、`Dismiss`、`Confirm`；`Running`、`Ran`、`Reload output`、`Copy retained output`；`View source`、`Diagram unavailable`、`Render again`；`Load earlier messages`、`Search this conversation`、`Back to latest`。复制范围通过 aria-label/tooltip 说明，不增加常驻文字。草稿提示不宣称换地址或换设备也能恢复。中文规格里出现的对应表述仅说明语义，不改变此英文基线。

### A4. 动作如何算出来

建议新增纯函数 `recoveryForFailure(failure, authoritativeState)`，返回有限 action descriptor，不返回可执行 JS，不接收服务端任意 URL：

```ts
type RecoveryAction =
  | {kind: 'open_connection'; sourceId: string}
  | {kind: 'focus_approval'; approvalId: string}
  | {kind: 'check_execution'; executionId: string}
  | {kind: 'retry_continuation'; approvalId: string; revision: number}
  | {kind: 'reload_resource'; resourceId: string}
  | {kind: 'retry_request'; requestId: string};
```

1. 按消息身份读取权威状态；未知、不匹配或过期时只给安全的查询/详情。
2. `effect=unknown` 时绝不产生 retry_request 或重新执行命令动作。
3. retry_continuation 必须满足现有审批状态：已结算且 continuation=failed 等允许条件。
4. focus_approval 必须有实际 approvalId；没有记录时显示说明，不能用当前最后一张卡代替。
5. 检查目标所属 session、run；切换会话后回调不能操作新会话。
6. 点击后只锁该动作。响应丢失查询同一身份；旧 revision 的回调不得覆盖新状态。
7. 主行用静态 role=status；需要用户立即处理的失败用 role=alert 一次，不在每次渲染重复朗读。

### A5. 红测与验收

- 表驱动单测：上述每行，code 未知、effect unknown、缺少身份、旧 revision。
- policy 拒绝与 OS PermissionError 是不同类别；诊断包含相同英文单词也不能误分。
- 查询失败不出现重跑按钮；已结算命令的继续失败只能进入 continuation。
- 连接设置按钮只打开设置，不触发请求；只读重读不调用 /api/run。
- HTTP：公开投影不泄漏凭证；failure 经保存/重启仍为对象，所有字段预算受控。
- 浏览器：切会话、重复点、断网和旧回调；只改变对应行，不让全部工具展开或整轮提前失败。

## B. 按轮组织复制：先补内容身份

### B1. 必须消除的语义歧义

“复制本条消息”取一个现有助手消息片段的原始 Markdown，可包含多个自然段、表格和代码；不拆成逐自然段复制按钮。“复制最终回答”取该轮已明确标记的最终回答原文。不是复制整个 DOM，也不是把 Thinking、命令输出、按钮文字、步骤拼进去。“复制整轮记录”不在本次范围。

当前 `Message.finalAnswer` 已用于流式最终结算；它不等同于“用户这一轮所有过程已经结束”。先核对实际事件生产方，不能直接把 stream complete 当作 turn complete。

### B2. 消息字段与数据更新

建议增加 `turnId`、`contentRole: commentary | final`、`answerId`、`answerPartIndex`。同一用户输入（包括持久队列实际准入的输入）获得一个稳定 turnId；工具续接运行保持该 turnId；真正新用户输入获得新 turnId。runId 仍表示一次运行，不用 turnId 替代它。

1. 正文生产方明确区分过程解释与最终回答；中间模型轮次的 stream 完成不自动赋 final。
2. 同一段 stream 的部分/最终消息共享身份并原位更新；不能把末次内容另插一条。
3. 最终答案若确实分多部分，按 answerId 和 partIndex 聚合，固定使用两个换行连接；拒绝重复 partIndex 和非同一 turn 的内容。
4. 推荐后端结算出单一 canonical final Markdown，前端按 answerId 读取，减少拼接规则。超过已有消息预算时明确保留范围；不在前端静默裁剪后仍叫完整回答。
5. 不支持角色区分的协议仍提供 Copy message；不要靠“它是最后一条”伪装成最终回答。
6. 将新字段加入事件投影、TS Message 和持久化白名单。版本仍为 1。旧记录缺乏角色事实时保持消息片段语义，不猜造 final 身份。
7. ask_user 的正式回答若经队列恢复原任务，应显式继承原 turnId；用户独立补充的新请求才建立新 turn。不能仅因出现一条用户消息或新 run 就改变归属。

### B3. DOM 与交互

- 保留 `messages.ts` 中每条正文消息容器。中间解释的复制图标 aria-label/tooltip 为 Copy message；一条含多个自然段的消息仍只有一个复制入口。
- 最终回答末尾只设置一个主要复制入口，aria-label/tooltip 为 Copy final answer。默认仍只见图标，不额外挂常驻工具栏。
- 将 `addMessageCopyButton` 接收的参数改为 `{getText, scopeLabel}`；点击时读取当前 canonical 文本，避免闭包仍持有流式早期前缀。
- 加 copy-pending 状态防重复。成功只短暂反馈；失败显示可理解提示并允许再次点击。不要在失败时清空选区。
- 流式中可允许“复制当前内容”，必须如此命名；最终结算后原按钮变为最终语义。中断时保持“复制已收到内容”。
- 原生选区复制保持优先，不拦截 Ctrl+C。键盘 focus-visible 可发现复制；触屏按现有明确入口显示。

### B4. 红测

过程解释两段 + 两次工具 + 最终回答：复制最终回答只得到最终 Markdown。代码中的反斜杠、换行、管道符、公式和 Mermaid 源码逐字一致。中断/最终更新后复制最新内容；续接 run 仍属于原 turn；队列下一输入不并入前轮。最终部分重复投递不重复复制。跨刷新身份不丢，hover/focus/触屏和反馈沿用现有浏览器回归。

## C. 提问草稿跨刷新

### C1. 保存对象与归属

当前 `frontend/src/user_questions.ts` 的 drafts 是内存 Map。建议独立 `QuestionDraftStore`，把持久化从组件移走。第一轮只承诺同一浏览器/同一 Codey 数据实例跨刷新，不承诺换电脑同步。

key 使用 `codey:question-draft:<instanceId>:<sessionId>:<questionId>`。instanceId 是服务端数据实例稳定 ID，不能用随机端口；同浏览器不同 Codey 实例不能互相读草稿。

**存储边界：** 第一轮若使用 localStorage，仅保证同一 browser origin（协议、主机、端口均一致）下刷新恢复。稳定 instanceId 只区分键名，不能绕过不同端口的存储隔离；不同浏览器 profile/桌面 WebView 数据目录也不互通。若验收要求启动端口变化后仍恢复，应在实施前改用同一数据实例的服务端私有草稿存储或稳定应用 origin，不能事后叠加猜测恢复的 fallback。先确定一种权威存储，再实现 QuestionDraftStore。

```ts
interface QuestionDraft {
  schema_version: 1;
  question_revision: number;
  selected: string | null;
  custom: boolean;
  text: string;
  edit_revision: number;
  writer_id: string;
  saved_at: number;
}
```

只有问题仍 pending、question revision 与选项内容匹配时恢复。后端 question revision 若包含纯状态改变，应另外暴露稳定的 question content revision/digest；不要把 UI 的一次重新渲染当作问题变更。

### C2. 按事件写出实现

1. `card()` 先读取当前权威问题；没有确认状态时不自动提交任何草稿。
2. 从 store 读取并做字段/长度校验。自由文本最多沿用 8000 字符，选中值必须仍在 options；自由输入需 allow_free_text。
3. 写入 textarea.value 和 radio.checked；不要把草稿用 innerHTML 插入。
4. input/change 只更新草稿；300ms 防抖保存，pagehide/会话切换同步 flush。IME composition 不触发提交，正在输入时不从远端覆盖表单。
5. storage 抛错时保留内存输入，显示一次“草稿未能保存，刷新可能丢失”，不阻止用户正常作答。
6. 提交答案时 flush 并锁表单；POST 响应丢失仍保留草稿，查询原 questionId。
7. 只有服务端确认 answer_accepted/answered/cancelled 才删除相应草稿。用户点击 Clear draft 仅清除选定权威存储中的草稿，不取消问题。
8. 另一标签页 storage 变化：本页未修改则更新；本页正在编辑则提示存在另一个草稿，不静默覆盖。提供“保留当前”/“使用另一份”，不自动提交。
9. 删除会话、清空会话时清理所属 key；仅对服务端确认已关闭的草稿做垃圾回收。不能只按 7 天 TTL 丢掉仍待回答的问题。
10. 草稿不放入模型上下文、日志、事件广播；仅正式提交答案才走现有授权推导。
11. 正常保存使用同一处轻量反馈，防抖后短暂显示 Draft saved，1.2s 后消退；恢复仅显示一次 Draft restored 后消退，不每键朗读或增加永久状态栏。提交仍使用现有 Dismiss / Confirm；Clear draft 是次级操作，不取代 Dismiss。未保存及跨标签冲突在问题卡内保持可见直到处理，不弹模态框。

### C3. 红测

输入中文→flush→销毁组件→用同一存储新建→值恢复。选项/自由模式同时恢复；不同 session/instance 同 questionId 不串。回答成功删、丢响应不删、刷新后只查不重发。存储 quota/损坏、IME、pagehide、两标签冲突和用户清空。另一问题更新不重建当前 textarea，不丢焦点/选区。成功反馈消退且不占固定空行，失败恢复动作仍可见；跨端口按选定存储契约验收，不以同端口刷新冒充重启换端口恢复。

## D. 长命令实时输出

### D1. 不要另起执行器

共享启动/等待入口在 `codey/runtime/core/cancellation.py` 的 start_process、wait_process、run_process、_pump_stream；现有 `output_capture.py` 负责有界 head+tail 和 UTF-8 边界。审批 shell 在 `codey/app/shell_service.py:execute_shell_ticket` 使用同一链路。

扩展共享链路的**观察能力**，不改变命令准入、Stop、Windows Job、管道回收和最终结算。run 和 shell 都可用同一显示通道；是否允许执行仍按各自策略。

### D2. 身份、生命周期和读取契约

为每次实际执行生成 executionId，关联 sessionId/runId/callId/approvalId（可选）。在 claim 成功、Popen 前将执行身份持久记录。executionId 不等于 command hash；相同命令执行两次必须有不同身份。

建议新增只读接口 `GET /api/executions/<id>/output?after_revision=N`：

```json
{
  "schema_version": 1,
  "execution_id": "e1",
  "revision": 12,
  "execution_state": "running",
  "delivery_state": "current",
  "stdout": {"head": "...", "tail": "...", "total_bytes": 120000, "omitted_bytes": 0},
  "stderr": {"head": "...", "tail": "...", "total_bytes": 0, "omitted_bytes": 0},
  "exit_code": null,
  "result_revision": null
}
```

这是有界**快照**，不是要求前端补齐无限字节增量。unchanged 可返回 304；revision 是输出快照修订，不是已读字节偏移。定义 head/tail 不重叠，omitted_bytes 真实计数；字节计数始终指原始输入流，不用 JS 字符数代替。

新增轻量 SSE 通知 `execution_output_available {execution_id, revision}`，不把每个 chunk 原文复制进全局事件和聊天持久消息。浏览器收到通知拉快照；重连也能直接拉最新快照，不依赖消息从未丢失。只为当前展开的执行卡订阅读取，收起后低频/停止拉取；结算通知仍处理。

### D3. 逐层实现顺序

1. 在管道 pump 内保留现有 capture.feed；增加 `observer.offer(channel, bytes)`。offer 只能写入有界内存槽，不能做 HTTP、磁盘 I/O、拿应用大锁或等待订阅者。
2. **验证读取延迟**：当前 _pump_stream 使用 read(8192)。带缓冲的读取可能等更多字节；为有 fileno 的真实 subprocess pipe 选择不会等待凑足 8KiB 的读取路径（例如 read1/os.read，按实际管道对象统一实现）。先用子进程输出很短一行并 flush、随后等待测试锁定，不能只测一次输出 100KiB。
3. stdout/stderr 各自使用增量 UTF-8 解码器；跨 read 的半个汉字留在解码器中，EOF 再 flush。非法 UTF-8 沿用现有明确的损失提示，不偷偷回退 GBK。ANSI 控制符按有限文本策略处理，不把输出当 HTML。
4. observer 的单一发布线程每最多 100ms 合并一份快照，第一次有字节及时发布；高输出时覆盖尚未发布的中间快照，绝不无限排队。建议沿用每流 256KiB 保留预算；终端最终 capture 的预算不能因为加了实时显示变小。
5. 快照存储在独立有界 ExecutionOutputStore 中，可复用项目已有可靠的 SQLite/原子文件设施。每次先提交 revision 与快照，再通知浏览器。不要每个字节写磁盘，也不要把完整原文追加到无限日志。
6. observer/存储失败只令 `delivery_state=unavailable`，不能杀掉执行中的进程或使已经执行的命令被标为“没执行”。记录一次有界诊断；最终执行证据仍走原结算链路。查询失败界面显示“输出暂不可用”，不声称进程停止。
7. 正常结束：最后一次 drain→保存最终结果/结算事实→保存关联 result_revision 的最终展示快照→发布 shell_result/完成状态。最终卡片按 executionId 原位结算；不得再新增一张 Ran 卡。
8. Stop：调用现有终止入口，等待既有进程树/pipe 清理；有多少保留多少。timeout、read error、drain timeout 沿用原状态区分，不能都叫 exit 1。
9. 重启：旧记录仍 running 但没有可靠执行/结果证据时标未知并查询原审批恢复机制；不能重建一个进程，也不能只因缓存中无 PID 就算失败。
10. 输出查询验证 session 所属与工作区绑定，ID 不当作文件路径拼接。会话删除时清理相应输出；执行进行中的删除策略必须明确，不能一边删证据一边让 worker 写回复活。

### D4. 前端组件

commandCard 接收同一 executionId 的视图模型，拆成 create/update，保留 details.open、输出滚动位置和当前选区。

- 折叠行仍沿用现有 Running/Ran 命令摘要，不新增大边框。
- 展开后：Shell、命令、stdout/stderr（有内容才显示）、一行运行状态；公共圆角不变。
- 用户在输出底部时跟随；往上看时停止跟随并出现“有新输出 · 回到底部”。外层聊天跟随不受内部滚动干扰。
- 正在运行的按钮语义是“复制已接收输出”；截断后是“复制保留输出”。完成后未截断才是“复制输出”。
- stdout/stderr 分开保留，不把并发读取顺序宣传为进程真实时间顺序。若展示合并视图，只能注明为捕获顺序。
- 已 settled 的最终结果 revision 优先；迟到 running 快照不能把 Ran 改回 Running。
- 失联主行显示“输出连接中断 · 命令状态待确认”，按钮“重新读取输出”；不能再增加一处全局 Working。
- 正常流入不反复提示 Output received，不另造状态栏；运行状态仅在原命令卡内更新。stdout/stderr 只有有内容时才标识，复制仍悬停出现；输出面板原位结算，不重置手动展开、选区、内层或外层阅读锚点。

### D5. 必须先红的实测

本机脚本采用 Python -u：先 print 一行 flush，等待一个由测试控制的同步信号，再退出。**退出前**浏览器必须已看到首行。不要用只睡 20ms 的脆弱断言；设合理超时，用同步屏障证明顺序。

另测：一个汉字拆成三次写；两个流各 10MiB 不死锁且内存有界；无换行输出；空输出长运行；ANSI/无效字节；慢浏览器/断开连接不阻塞子进程；重连取快照不重复；Stop/超时/继承 pipe 的子孙进程；最终先落盘后事件；迟到事件；不确定结果没有重跑入口。run 与批准后的 shell 分别走真实 HTTP/子进程，统计 Popen 次数始终为 1。

## E. Mermaid 和数学公式：共享正文能力

### E1. 接入位置和依赖

当前 `frontend/src/markdown.ts` 统一渲染正文，`render.ts:updateMarkdown` 保持稳定节点，文档预览也用同一正文能力。图表/数学在这条链路接入，不为 Research 创建第二套解析器。

将 Mermaid、KaTeX 及需要的 sanitizer 作为**本地打包依赖**，选择当时受维护的确切版本并锁 lockfile；本文不指定未经安装验收的版本。字体也打入前端产物。不用运行时 CDN，不读取 Codex 安装目录，不在用户机器执行 Mermaid CLI。

官方行为依据：

- Mermaid strict 会编码标签 HTML 并禁用 click：[securityLevel](https://mermaid.js.org/config/schema-docs/config-properties-securitylevel.html)。
- Mermaid 图内配置可能覆盖站点配置，需要锁住安全项：[secure](https://mermaid.js.org/config/schema-docs/config-properties-secure.html)。
- KaTeX 的 trust、throwOnError、maxExpand、maxSize 等选项：[options](https://katex.org/docs/options)；错误消息也可能含输入源码：[security](https://katex.org/docs/security)。

### E2. Token 和流式阶段

1. fenced code 的 info 首词严格为 mermaid 时创建 diagram block；其它 fenced code 继续原路径。源码保存在 view-model/WeakMap，不塞进未转义 HTML 属性。
2. 给块稳定 contentId/source digest。流式围栏未闭合时先显示源码，不反复尝试渲染语法不完整的图。
3. 围栏完成且该块稳定后入渲染队列；最终结算同一 digest 不再渲染。每次结果返回核对 contentId、digest 和 host.isConnected。
4. 关闭文档/切换正文时取消待处理队列，释放结果 URL；迟到结果不能填到另一个消息。
5. 错误只影响该块，展示“流程图暂时无法显示 / 查看源码 / 重新显示”。重试只做本地渲染。

### E3. 图表执行隔离与预算

Mermaid 需要 DOM；不能凭空声称把它放入普通 Worker 就能渲染。建议用独立本地 sandbox iframe 渲染桥，allow-scripts 且不加 allow-same-origin，渲染资源预打包；桥的 CSP 明确禁止外部网络、嵌入和表单，资源只允许所需本地构建文件。先验收桌面/web 两种宿主均能加载，再提交方案。

桥通过 postMessage 返回结果，只信任指定 frame.contentWindow、每任务随机 nonce、匹配 contentId；opaque origin 不能用宽泛 origin 判断代替 source/nonce 验证。不要接受返回的任意 HTML。

站点固定 strict、禁用 HTML labels/交互点击，安全项不能由图中 init/frontmatter 放宽。第一轮仅支持实际验证的图种（至少 flowchart 和 sequence），不支持的类型明确显示源码。拒绝外部图像和 URL 动作，不用一个字符串正则宣称解析了全部 Mermaid 语法。

建议初始预算：每图 20k 字符、200 个节点/边量级（按具体图种可实际检查的指标定义）、同时一个任务、渲染 2s 超时。单纯 Promise.race 不会停止主线程计算；sandbox iframe 的安全隔离也不等于独立 CPU/线程。移除重建 frame 作为待验收的取消机制，不能未经实测就声称可在 2s 强制终止。先用复杂图证明渲染期间输入、滚动可响应，超时后下一张图仍能显示；达不到则收紧渲染前预算或更换真正可取消的宿主方案，未验收的图仅展示源码。超过预算显示原因和源码。

返回 SVG 先经维护中的 sanitizer 的 SVG 配置及项目明确白名单校验，禁止 script、事件属性、foreignObject、外部引用。优先作为 blob SVG image 显示，不把任意 SVG innerHTML 填入主应用。提供文字替代、查看源码和复制源码；不自动开放 SVG 导出/外链。成功切换和移除时 revokeObjectURL。

### E4. 数学语法与 KaTeX

第一轮明确支持 `\(...\)` 行内、`\[...\]` 和独占行的 `$$...$$` 块。单美元 `$...$` 暂不自动解释，避免金额/代码误识别；若产品需要再单独定义转义与货币规则。

- 用 markdown-it 的 token 规则，先排除 inline code、fence、链接目标等区域；不对最终 HTML 全局正则替换。
- 生成数学 token 后 KaTeX 仅渲染它的源码。建议 trust=false、throwOnError=true、每次新 macros 对象、不启用 HTML 宏；设置有限 maxExpand/maxSize（例如 1000、10，作为初始配置）。
- ParseError 在本块捕获，错误详情通过 textContent 展示。不能把错误消息当 HTML，不能让整个回答消失。
- 输出包括 MathML 可访问结构；视觉公式与源码复制分开，整条回答复制仍返回原 Markdown。
- 宽公式局部横滚，不能把聊天或文档撑出屏幕。图表放大是只读展示，不出现新的执行按钮。

### E5. 红测

正文/文档/Research 同一样例结果一致；代码围栏内公式不渲染；金额不误识别；不完整流式围栏不报一屏错误；final 不重建已显示图；未知图种/无效公式局部失败；源码复制逐字一致。HTML 标签/click/init 覆盖/外链图像/危险 SVG 被隔离；测试监听 network 确认不发外部请求。超大图超时后新图可渲染，主输入不冻结。离线打包字体存在；320px、键盘和减弱动态偏好可用。

## F. 超过 500 条的历史：需要存储职责调整

### F1. 现状与目标

`codey/storage/ui_state_store.py` 的 _clean_messages 只留末尾 500 条，整个 UI state 还有 16MiB 上限。`frontend/src/conversation_ui.ts` 的 historyBoundary/addHistoryLoader 只是在已加载数组里补 DOM。把 500 改成 50000，不能解决完整历史、并发更新、首次加载和搜索。

**UI 展示历史、模型协议历史、执行日志是三个职责。** 新 archive 只用于展示与查找，不能把历史正文重新喂给模型当作已授权工具结果。已有被裁掉的消息不能承诺恢复；有可核验旧日志时才做单独导入，不在此混入猜测重建。

### F2. 新存储建议

新增 `codey/storage/transcript_store.py`；用现有 conversation_database 风格创建 SQLite store，不强制合并与生命周期不一致的现有数据库。建议逻辑表：

```sql
CREATE TABLE transcript_sessions (
  session_id TEXT PRIMARY KEY, epoch TEXT NOT NULL,
  next_seq INTEGER NOT NULL, revision INTEGER NOT NULL,
  cleared_at TEXT
);
CREATE TABLE transcript_messages (
  session_id TEXT NOT NULL, epoch TEXT NOT NULL,
  message_id TEXT NOT NULL, ordinal INTEGER NOT NULL,
  revision INTEGER NOT NULL, turn_id TEXT,
  kind TEXT NOT NULL, payload TEXT NOT NULL,
  PRIMARY KEY(session_id, epoch, message_id),
  UNIQUE(session_id, epoch, ordinal)
);
CREATE INDEX transcript_turn ON transcript_messages(session_id, epoch, turn_id, ordinal);
CREATE TABLE transcript_events (
  session_id TEXT NOT NULL, epoch TEXT NOT NULL,
  event_id TEXT NOT NULL, message_id TEXT NOT NULL,
  PRIMARY KEY(session_id, epoch, event_id)
);
```

epoch 是清空代次，防止旧标签页/旧 worker 把清空前消息写回来；不是 schema version。opaque UUID 可以作为 epoch，但不在客户端随意生成新代次。

### F3. 写入顺序与唯一权威

1. 先列出所有消息生产路径：用户请求、队列准入、正文 partial/final、工具、审批、问题、Research、Done/Error。为每类确定服务端 event identity；展示一条消息可多次更新，但不重复插入。
2. 现有用户消息可能先在客户端乐观显示：使用 requestId/queuedInputId 映射服务器 messageId，服务端受理后合并，不另插一份。拒绝记录也要有明确的展示归属。
3. 在服务端同一事务内去重 eventId、分配 ordinal、写 message revision、递增 session revision；提交后再发布展示事件。失败不得把“已保存历史”发布给 UI。
4. 流式对同一 messageId 更新；按有界间隔保存 partial，final 必须落盘。运行中崩溃只能显示最后已保存的 partial 并标中断，不伪称完整。
5. SSE payload 统一带 messageId/ordinal/messageRevision/epoch。客户端较旧 revision、旧 epoch 直接丢弃，不重排所有历史。
6. UI state 此后保存侧栏、草稿、视图位置、最近展示缓存与 transcript cursor；不再用浏览器上传的整段 messages 覆盖 archive。模型 protocol history 继续走原 ConversationStore。
7. 现有最多 500 条 UI messages 可做**一次有标记的基线导入**：先备份，按稳定顺序生成导入身份，事务完成后写 import_complete；新事件接入必须设明确 cutover/barrier，不能导入与实时追加并行导致重复。遇异常保留原数据并报错，不偷偷开启双写 fallback。
8. 版本仍为 1。一次性基线导入只处理实际存在的数据，不做通用旧版协议兼容框架；完成后的运行路径只有一条。
9. 删除会话/清空消息由服务端事务更新 epoch 并删记录/索引/相关展示缓存；停止/拒绝旧 epoch 写入。清空是否同时 Stop 活跃任务必须沿用或明确产品语义，不能无声让旧运行重新填满聊天。

### F4. 分页读取契约

建议 `GET /api/sessions/<id>/messages?before=<opaque_cursor>&limit=100` 返回：

```json
{
  "schema_version": 1,
  "session_id": "s1",
  "epoch": "ep1",
  "snapshot_revision": 2400,
  "items": [],
  "before_cursor": "opaque",
  "has_more": true
}
```

cursor 绑定 session/epoch/snapshot 和最早 ordinal，服务端校验并预算 limit；用 keyset（ordinal < before），不用 OFFSET。回给前端的 items 始终正序。加载首屏后实时新增不会让上翻页面重复/漏项。

历史 payload 可更新，snapshot_revision 不是天然 MVCC：第一轮可以明确“分页成员位置固定，字段使用最新修订”，前端按 messageRevision 合并。若承诺历史内容时间快照，就需要版本化记录，不能只有一个数字假装做到了。

分页不能为凑完整轮次无界扩展。可以返回 partial-turn 标志；加载更早后按 turnId 合并已有过程组，不重复创建 Worked。特别大的单轮仍受 page budget 约束。

### F5. 前端加载与搜索

- 新建 `HistoryRepository` 负责请求、cursor、去重、取消和 session generation；ConversationUI 只负责挂载与锚点，不直接管理 HTTP 细节。
- 进入会话先取尾页，不下载全档。加载更早只允许一个并发请求；旧会话请求迟到不得插到新会话。
- prepend 前记录首个可见 messageId 及其相对滚动容器的 top；插入后恢复同一位置。图片/图表晚加载也应保持锚点，并与现有 ConversationUI 滚动所有权协调。
- 替换“显示全部”含糊语义：小历史可保留“显示已加载内容”；完整归档提供“搜索此对话”，不一次挂载几十万节点来迁就 Ctrl+F。
- 增加服务端有界搜索：query 长度/返回数受限、literal 参数绑定、cursor 分页；FTS 可后续优化，先不要把用户输入当 SQL/FTS 运算表达式。正文搜索结果不混入执行日志/凭证。
- 搜索结果给 messageId、turnId、有界纯文本 excerpt、命中位置和时间；高亮用 text nodes，禁止插入原文 HTML。点击后 around(messageId) 加载上下文并定位，带“回到最新”。
- 搜索命中尚未挂载的结果也可打开。原生 Ctrl+F 保留浏览器行为，并说明其只查已挂载页面；不要劫持所有浏览器快捷键。
- 数据请求失败保留已读内容和位置，顶部显示“未能加载更早消息 · 重试”，不清空聊天、不切整轮 Failed。
- Load earlier messages 沿用现有顶部轻量入口；Search this conversation 通过现有会话菜单按需打开，不常驻一排工具栏。条目范围仅在实际加载/定位后短暂反馈，不永久显示计数；搜索定位继续使用已有圆形 Back to latest，不新增文字跳转按钮或第二个回到底部控件。

### F6. 红测与容量验收

HTTP/SQLite：1200+ 条跨重启完整；同 event 重投不重复；消息修订与 ordinal 稳定；并发写事务；导入两次只一份；导入中断恢复；清空后旧 epoch 请求不复活；session cursor 混用被拒；limit/输入预算生效；模型历史没有被 UI 历史替换。

浏览器：尾页先可读，上翻不跳；新增同时分页无重复；单轮超过 100 条；跨页 Worked/工具组正确合并；搜索命中第 1 条并可定位；图片/图表改变高度；切会话/断网/请求迟到；键盘/选区不丢。

用固定生成器做 1k、10k、50k 消息，另做少量极长代码/表格。分别记录 DB 大小、首屏接口耗时/响应字节、首次可操作时间、DOM 节点数、峰值内存、分页和搜索耗时。先记录基线，再定硬预算；不能写一个未在 CI/实机测量过的“永远 100ms”。确实瓶颈在 DOM 后才引入完整虚拟列表。

## G. 同事实施时的文件与验收清单

| 改动 | 现有修改入口 | 建议新增的小模块 | 首个红测 |
|---|---|---|---|
| A 错误语义 | diagnostics / kernel_result / event_payloads / render / composer / domain / ui_state_store | failure presentation + recovery policy | uncertain 不生成重跑动作 |
| B 复制范围 | 正文事件生产方 / messages / render / process / domain / 持久化 | answer model/selectors | 两段解释不混进最终复制 |
| C 草稿 | user_questions / 删除会话入口 | question_draft_store | 重建组件后自由回答恢复 |
| D 实时输出 | cancellation / output_capture / shell_service / server / app_runtime / command_card | execution output store + view model | 子进程退出前能见短行 |
| E 图表公式 | markdown / render / document preview / 构建 | diagram bridge + math token rules | 代码中的公式不能被替换 |
| F 完整历史 | ui_state_store / server / 消息投影 / ui_state / conversation_ui | transcript store/routes/repository | 1200 条保存重启后仍完整 |

推荐测试文件名可以是 failure_presentation.test.ts、answer_copy.test.ts、question_draft_store.test.ts、execution_output.test.ts、rich_blocks.test.ts、history_repository.test.ts 以及对应 pytest HTTP/browser 文件；不强制名字，重要的是覆盖真实契约。

每批顺序：独立失败测试 → 确认失败不是测试接线错误 → 最小实现 → 相同测试绿 → 相邻模块回归 → 正式构建/发布一致性 → 实际浏览器链路。无需为每个 CSS 属性写镜像测试；重点测试用户动作、身份、数据保真、时序和恢复。

最后统一跑前端、相关后端与正式浏览器组，并在最后一次修改后再跑扩大 pytest，不能沿用此前“8 项失败已定向修复”的扩大记录作为新版本全绿证明。保持版本 1；不新增 Codex 安装路径依赖，不保留无人使用的旧渲染器或双轨持久化。

### 2026-10-17 实际完成状态

A、B、C、E、F 的当前契约已接入正式前后端并通过单元、HTTP 和浏览器验证；D 已完成共同 execution identity、增量有界快照、stdout/stderr 观察和同卡 Running/Ran 原位更新。D 仍明确采用浏览器按需轮询快照，未宣称每个 chunk 的 SSE 推送或所有复杂图种支持。结构化失败的公开投影只在 SSE 边界执行，内部事件仍保留完整字段。最后一次全量 pytest 为 `8784 passed, 8 skipped, 1520 subtests passed`；未覆盖的外部模型差异和旧版本从未归档的数据仍不作承诺。

## H. 前后对比稿的解释范围

同事交接的独立页面：[conversation-next-comparison.html](conversation-next-comparison.html)。所需脚本已内嵌，可把此 HTML 与本规格一起交接，不需要 Codex 安装目录。页面仅演示本地状态，不连接 Codey 后端，不执行命令。HTML 中的说明与演示控制位于产品示意之外，不能照搬进正式界面。

本轮对比稿以当前交互语义做简化重现，不是 Codey 截图，也没有接入真实执行器。修改后状态是本文待实现设计；草稿刷新、输出推进、恢复动作和历史搜索均为局部模拟，不证明后端已经实现。图表和公式示例是排版示意，不冒充已接入 Mermaid/KaTeX。

命令对比必须使用相同命令、相同执行阶段和相同输出样本。默认阶段是已完成：左右均为 `Ran` + `python -m pytest tests -q` + 尾部 chevron，先收起，点击展开可滚动保留输出；保留现有 composer、CODEY 标签和单色层次。现有 commandCard 在 exit 已确认时就是 Ran，这不是本轮待新增功能。截图中的 `Shell · Retained output` 是保留输出的呈现语义；当前组件源码标题为 `Shell`，对比稿不把截图的具体标签冒充源码原文。

拟议的 `Running` 仅用于尚未结算的实时输出阶段；不替换已完成记录的 Ran。对比稿把“运行中”作为独立可选阶段，并明确标为新增能力。不得把当前的已完成态与提案的运行中态并列，再把两者差异说成视觉改进。`-u` 可以用于确定性实时输出测试脚本，不得为了演示改变用户命令或给人实现会自动插入参数的印象。

可检查六个场景：错误动作、复制范围、图表与公式、问题草稿、运行输出、完整历史。它们保留现有简洁命令行、公共圆角、悬停复制和一处 Working，不改变左右侧栏。

### H1. 本次更新的视觉验收边界

- 模拟页只更新这份交接参考，不改写此前已确认的历史对比文件，也不表示产品功能已实现。
- 六个场景均使用同一个输入框示意：纸飞机、模型与努力选择、10px 圆角；问题卡保留 Dismiss / Confirm；正文 14px，控件 13px，过程摘要 12px。模拟中的模型、努力选择不连接真实设置。
- 切换场景、模拟刷新/保存失败/图表失败、推进输出等演示控制放在产品框外。正式 UI 只保留真实用户动作。
- 草稿成功反馈会消退；消息复制只改变语义、不增加逐段按钮；历史搜索按需展开，范围计数不常驻，返回最新沿用单一圆形按钮。
- 输出阶段默认完成、两侧同内容同状态；运行中仅增加观察能力。图表与公式仍为排版示意，不冒充 Mermaid/KaTeX 验证。
- 交付前检查 HTML 内联脚本语法、六场景交互、窄屏与键盘状态；文件无需 Codex、运行时 CDN 或产品后端。原有正式产品测试不能被模拟稿检查替代。
