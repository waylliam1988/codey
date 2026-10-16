# Codey 中间对话区 UI/UX 参考分析

日期：2026-10-16。范围只包括对话正文、过程、工具、命令、审批、错误、消息操作及对话滚动；不评估左右侧栏。

## 结论

Codey 值得继续借鉴，但下一轮的主要收益已经不是再换一套图标，而是让中间栏成为可连续阅读、可检查、可恢复的对话记录。

优先处理四件事：正文不能误解析或丢显示内容；文件链接不能乱认；工具摘要不能替代可检查的结果；内层输出滚动不能改变外层阅读状态。随后再完善真实流式显示、按轮组织的操作和长会话性能。

“几个项目都有”只能说明值得考察，不足以证明设计优秀。下文区分共有模式、单个项目的具体实现，以及适用于 Codey 的判断。也不把框架、组件库或代码量当作体验优劣的证据。

## 核对方式与证据边界

- 读取指定的四个本地参考项目的组件、样式及相关测试，查看 Wish、Percho 仓库提供的截图。
- 主参照为指定的 Codex `26.928.3736.0` 安装包 `app/resources/app.asar`。只读提取有关组件作分析，不修改安装文件，也不让 Codey 依赖这些路径。
- Codex 包同时包含多种产品界面。本次主要沿 `local-conversation-thread` 导入的 `local-conversation-turn`、`user-message`、`virtualized-turn-list`、`thread-scroll-layout` 分析，未把 Business Agent 等无关模块的行为算作 Codex 聊天结论。
- 组件存在、进入导入链，不能证明每一个开关或账户都启用它。下文涉及 Codex 的结论以可确认的组件行为为限，不声称实机逐一体验了所有隐藏状态。
- 对 Codey 当前源码做了两组本地 DOM 探针：7 个正文样例，以及一次内层输出滚轮事件。未调用模型或外部 API，未运行四个参考项目的完整应用或测试套件。
- 本轮不修改产品代码或设计基线。探针及结果位于 [分析记录目录](/E:/codey/.e2e-artifacts/ui-audit-20261016)。

## 共有模式与 Codey 的差距

| 模式 | 参考中的证据 | Codey 当前状态 | 判断 |
|---|---|---|---|
| 答案与执行过程分层 | 五者都有独立的正文和过程/活动表示，具体分组边界不同 | 已有 Worked/Thinking、命令及审批卡 | 保留，继续统一层级，不重做 |
| 增量显示与稳定内容身份 | Wish 有流式状态；OpenCode 有文本步进及 Markdown worker；Percho 有稳定 key 和增量 Markdown；Harness 有冻结已完成块的解析；Codex 有运行中内容及虚拟化 turn | 对话前端的正文入口主要接收完整 `reply`；长文分块渲染不等于接收 token 流 | 明显缺口，但需要贯通协议，不能只做打字动画 |
| 正文按 Markdown 结构展示 | 四个开源项目有成熟解析或增量语法树；Codex 的正文/代码/附件有专门渲染组件 | 手写解析器支持基本标题、列表、代码和表格，但仍有确定性解析问题 | 优先于增加视觉特效 |
| 摘要可深入检查 | Codex 有活动详情；Wish 有 ProcessDetail；Percho 有工具参数/结果；OpenCode 有工具和错误详情；Harness 有命令 disclosure | shell 有独立结果卡；普通工具记录仍只是短文本 | 优先补普通工具详情边界 |
| 输出滚动与对话阅读相协调 | 多项目有独立跟随状态；OpenCode 明确隔离嵌套滚动区域 | 有阅读保留，但内层滚轮仍影响外层 following | 已复现，优先修 |
| 操作依附其对应的消息/轮次 | Codex 用户消息有复制、编辑；OpenCode 有消息动作和轮尾复制定位；Percho 轮尾正文有复制/分叉；Harness 有复制/分支并校验分支边界 | 已有复制、选区引用、失败重试；普通正文和每个思考片段各自挂复制 | 减少中间过程的操作噪音；新动作需要真实协议支持 |
| 需要用户决策时呈现专门控件 | Codex 审批/输入请求；Wish QuestionCard；OpenCode question；Harness ui-user-questions；Percho 审批 | shell 审批已有；一般澄清主要靠输入聊天文字 | 结构化提问值得后续补充，不等于再加审批 |
| 长记录避免一次挂载全部内容 | Codex 虚拟化 turn；Wish 虚拟列表；OpenCode 时间线虚拟化相关逻辑；Percho 先挂尾部再补历史；Harness 分页加载历史 | 当前缓存有限个会话 DOM，但变化后仍可能遍历整段消息重建 | 后续按真实长会话基准决定规模 |

注意：不同项目的滚动阈值、默认展开状态、复制按钮可见性并不相同，不能把它们的任意一个参数说成共同标准。

## 一、正文阅读：先保证内容被正确显示

当前 Codey 的 [render.ts](/E:/codey/frontend/src/render.ts:116) 用正则处理行内内容，表格按分隔符拆列。对实际渲染函数执行探针后得到：

| 输入 | 当前实际输出 | 分类 |
|---|---|---|
| 表格单元格 `` `a|b` ``，后接另一个值 `x` | 拆成 `` `a `` 与 `` b` ``，原本的 `x` 没有显示出来 | 确定性解析缺陷，影响报告数据 |
| `[说明](https://example.test/docs(foo))` | 链接地址缺少末尾右括号，另一个括号留在正文 | 确定性链接解析缺陷 |
| `![流程图](https://example.test/diagram.png)` | `!` 加普通文字链接，没有图片 | 图片语法缺口/错误降级 |
| `` `json.dumps` 和 `os.path` `` | 两个都变成工作区文件按钮 | 行内符号与文件识别混淆 |
| `*斜体*`、`~~删除~~` | 原样显示标记 | 尚未支持的格式，不应称为已支持 |
| `- [x]` / `- [ ]` | 普通列表中的方括号文字 | 尚未支持的任务列表 |
| 粗体包裹行内文件代码 | 正常生成粗体与文件按钮 | 已修部分的正向对照，应该保留 |

原始结果：[render-probe.json](/E:/codey/.e2e-artifacts/ui-audit-20261016/render-probe.json)。它验证的是当前渲染函数输出，不包含模型或网络因素。

可借鉴的边界：

- Wish 用 MarkdownIt，再清理不可信输出；不是不断为某个截图增加正则。
- OpenCode 对流式代码、普通正文、图片和本地文件链接分开处理，并保留已稳定内容。
- Percho 使用 markstream-react，明确区分流式状态与最终状态，避免历史回复重新播放动画。
- Harness 的 GFM/数学语法与渲染路径有明确区分，特别处理中文粗体、图片及宽表格。

建议先确定 Codey 要支持的 Markdown 子集和安全约束，再选择统一解析方式。第一轮至少覆盖常用 GFM、中文混排、图片、复杂链接和宽表格。公式、Mermaid 可独立排期，不必为了“与参考一样”全部同时引入。是否给代码上彩色高亮仍应遵守 Codey 的单色设计偏好。

## 二、文件链接：看起来像路径，不代表就应该可点击

上次修复的唯一后缀匹配，解决了 `beauty/cache.py` 到 `selfie/beauty/cache.py` 的定位。但 [document_links.ts](/E:/codey/frontend/src/document_links.ts:6) 仍可能把带点的程序符号当文件。这次的 `json.dumps` / `os.path` 探针说明问题没有完全结束。

OpenCode 的 [markInlineCode](/E:/codey/reference-projects/opencode/packages/session-ui/src/components/markdown.tsx:383) 是一个很有针对性的参考：区分 URL 和路径，文件存在确认后才给行内代码赋予文件动作；流式尾部尚未完整时不急于判断。

Codey 可采用：显式文件链接按引用处理；普通行内代码仅在可确认是当前工作区文件时显示文件动作；不存在或不确定的符号保持普通代码。重复文件名要给明确选择，不能悄悄打开任意一个。文件已删除后的失败仍需可理解的提示。

这主要是中间正文的可信交互，不是右侧栏的装饰问题。

## 三、工具详情：摘要应当是入口，不是结果的替代品

Codey 的 [工具事件处理](/E:/codey/frontend/src/app_runtime.ts:1043) 目前先把普通工具 `result` 截为 200 字，随后存进消息。展开 Process 只是在看这条摘要，无法从该工具条目恢复已经截掉的部分。另有 Run details，但它是运行级信息，并不等同于逐次工具的完整参数与结果。

参考项目的不同实现有共同的职责分离：

- Codex：活动摘要与独立活动正文；无正文时可不提供 disclosure。
- Wish：ProcessGroup 中保持有序步骤，点击后检查 ProcessDetail，显示参数、结果、搜索项或文件修改。
- Percho：工具行只放工具名和目标，展开后分别显示参数、输出。
- OpenCode：工具类型有专门 renderer；错误可看详情并复制；shell 输出按游标读取有界尾部。
- Harness：多行命令结果按需展开，语义状态与正文分开。

建议把 Codey 的 read/search/run 等普通工具统一为：类型与目标的短摘要 → 可展开结果 → 按需复制或打开保留记录。必须区分“界面收起”“摘要省略”“执行器已经截断”。结果已截断时，应显示保留范围，不能声称能复制完整输出。

保留你已经确认的简洁风格：不要给每条工具行再加大边框、终端图标或一排常驻按钮。

## 四、错误：读得懂，而且知道下一步

当前普通工具错误仍可能直接展示 `ERROR: project guard denied run: command_not_allowed ...`。这对排查有价值，但作为主要信息会让中间栏像调试日志。

OpenCode 的 ToolErrorCard 有短摘要、展开正文和复制；Percho 的 ErrorNote 区分凭证、上下文溢出、一般错误及对应动作。Codex 的活动、已拒绝动作和需要用户回应的请求也有各自语义，不把所有问题都压成一个通用失败。

适合 Codey 的形式是短解释在前，例如：`This command requires approval`，必要动作紧邻其后；原始错误、阶段、引用 ID 放在详情中。保留以下区别：单次工具失败、整轮失败、等待审批、用户停止、命令结果不确定、结果已保存但继续失败。

不应该从界面层自动把被拒绝的 run 改成 shell 并执行，也不应该只把技术错误字符串删除。工具错误若仍在自动处理，不要让整轮过早显示 Failed。所有可执行恢复动作都绑定原 request/run/call。

## 五、流式与版面稳定：不是让已经完成的文字慢慢打出来

Codey 的 [reply 接收](/E:/codey/frontend/src/app_runtime.ts:1232) 当前把 `data.text` 追加成一条 asst 消息；`renderMarkdownChunked` 解决大文本渲染阻塞，但不代表前端已经接通增量正文。

参考项目强调的是：内容一边产生一边出现；已完成的段落、代码块和消息节点尽量不重建；最终收尾不丢已显示内容。OpenCode 有文本步进和独立 Markdown worker；Percho 的流式消息固化保留 key；Harness 只重新解析未稳定的尾部块；Codex 的本地 turn 组件区分 completed 内容，线程用稳定的 turn 列表。

建议先确认模型协议能可靠提供哪些展示片段，再定义增量事件、消息身份和最终结算。不能通过把完整答案拆成假 token 来掩盖上游等待，也不能显示未经协议确认的工具 JSON 片段。历史回复应立即展示，停止后的部分内容应有明确状态。

## 六、滚动：内层阅读不能误伤外层跟随

Codey 已有保持阅读位置、会话恢复、回到最新及选区保留。这些是优点，不应推翻。

但 [chat-area 的 wheel 监听](/E:/codey/frontend/src/conversation_ui.ts:785) 对任意冒泡上来的向上滚轮都调用 `stopFollowing()`，未区分 shell 输出和整段对话。DOM 探针在 `.shell-output` 派发向上 wheel，`isFollowing()` 从 true 变成 false。结果见 [scroll-probe.json](/E:/codey/.e2e-artifacts/ui-audit-20261016/scroll-probe.json)。此探针确认的是事件及状态变化，不是完整浏览器触控板几何测试。

OpenCode 的 [create-auto-scroll](/E:/codey/reference-projects/opencode/packages/ui/src/hooks/create-auto-scroll.tsx:117) 明确检查嵌套的 `data-scrollable`，不让工具输出的内部滚动退出对话跟随。可以借鉴“谁拥有当前滚动”的边界，而不是抄某个距底阈值。

后续验收需覆盖：输出框中部上下滚动、内部到边界后的滚动交接、宽表格横滚、鼠标拖选、触摸、窗口缩放及图片延迟加载。不能为了维持跟随而禁止用户看历史。

## 七、按一轮组织操作，降低重复标签和按钮

Codey 的 [assistant 消息](/E:/codey/frontend/src/messages.ts:80) 每段创建 Codey 标签和复制按钮，[thinking](/E:/codey/frontend/src/process.ts:248) 每个片段也附带复制。这些在逻辑上可用，但多步骤任务展开后仍会形成许多重复操作。

OpenCode 的 [copyContentID](/E:/codey/reference-projects/opencode/packages/session-ui/src/timeline/session-timeline-row.tsx:133) 在轮次完成后定位最后正文；Percho 的 [turnFinalTextIds](/E:/codey/reference-projects/percho/packages/shared/src/transcript/chat-rows.ts:122) 也只给该轮末段正文提供主要动作。Codex 的本地 turn 对 completed assistant 内容提供 copy text，其用户动作在 hover/focus 后出现。

Codey 可以进一步明确三个角色：过程摘要、过程中的解释、最终回答。常用操作集中于最终回答；过程内容的复制留在对应详情里。连续同一助手片段可减少重复 Codey 标签，但不能把不同轮次、不同来源的文字误合并。

“整段复制”也需定义是该段、最终回答，还是整轮记录；命令复制只包含命令，结果复制不混入按钮文字或行号。Codey 现有复制成功/失败反馈和选区复制应保留。

不建议加一排点赞、导出、重试、分叉、朗读按钮来显得功能多。你偏好的悬停才显示，仍是 Codey 的约束。

## 八、用户纠正与澄清：比装饰更能减少重复输入

Codex 用户消息有 Edit/Cancel/Send；OpenCode 提供消息回退动作；Percho 提供 Recall 和 Fork；Harness 的分支动作还区分是否为合法完成边界。这些不是完全相同的语义，但都让“从某条已有消息继续”成为明确操作。

Codey 当前有失败重试、正文选区引用及 Continue，但没有与上述同等的普通消息编辑/分叉体验。后续可以先做“取回这条消息到草稿”，保留原记录；真正重写历史或分叉需先有会话及工具结果的可靠边界，不能把 Retry 改名成 Edit。

另一个值得新增的方向是结构化澄清：Wish QuestionCard、OpenCode question、Harness ui-user-questions，以及 Codex 用户输入请求，都可让用户选项回答而不必重新描述。对于语音输入尤其有用。Codey 的 shell 审批并不等于一般提问系统，不能复用 Allow/Deny 表示选项答案。

## 九、时长和改动收据：有用，但不应复制成新的状态墙

Codex 有 `Worked for {time}`；Percho 有按轮时长和文件增删摘要；OpenCode 的时间线也计算用户轮次完成时长，并把过多改动文件藏在 Show all 后面。Wish 在中间过程提供文件改动入口。

Codey 已有 Changes / View diff，不应重复做“已改文件列表”。值得补的是轮次和结果的关联：耗时来自真实时间，文件数量来自该轮确认的变更；可在完成摘要后低调显示。不要用浏览器渲染耗时冒充模型/任务耗时，也不要把未结算的改动算成已完成。

这项优先级低于正文准确性、工具详情和滚动。模型名、tokens、成本、时钟不必同时常驻每段文字下方。

## 十、长会话：先测量，再采用有界挂载

Codey 的 [renderChat](/E:/codey/frontend/src/conversation_ui.ts:332) 在不能复用缓存时遍历所有消息，并重建 DOM。六个会话的 DOM 缓存有价值，但单个超长会话仍可能很重。

Codex 使用 virtualized turn list；Wish 使用 `@tanstack/vue-virtual` 并以稳定 key 复用行；OpenCode 有虚拟时间线和 Markdown 生命周期测试；Percho 先挂尾部窗口再向上补；Harness 有加载更早历史与阅读锚点协议。共有目标是“历史多，也不破坏当前阅读”，不是某个虚拟列表库。

建议先以短会话、数百条工具记录、上千条消息、长代码/表格、往返会话切换测量首次可用时间和滚动停顿。确定瓶颈后选最小方案。尤其不能在下屏后自动收起用户打开的工具详情，不能丢原生选区，也不能让浏览器查找只能找到挂载窗口却不向用户说明。

## Codey 已有且应保留的部分

- 答案与可展开过程分离；shell 命令和输出可单独复制。
- 命令退出状态、截断提示及未知结果区分。
- 一处动态进度；等待审批静态显示；审批卡不受折叠影响。
- 审批提交锁定、网络响应不确定后的状态查询、独立 continuation 重试。
- 已确认的单色层次、10px 公共圆角、无图标黑色 hover 框。
- 复制按钮的 hover/focus 可见性、失败反馈。
- 原生选区和选区引用；阅读位置保留、回到最新及导航。
- 工作区文件和 Research 文档的统一打开入口。

本轮不再把这些能力列为“Codey 缺失”。

## 不建议照搬

- Percho 的常驻复制按钮、hover 背景，以及中心动画：与你的偏好冲突。
- Wish 的整体气泡/配色、OpenCode 的错误卡色块：可参考信息结构，不更改 Codey 已确认的单色语言。
- Codex 对超长用户消息的 Show more/less：只是可选的用户输入处理，不是重新给助手答案加 Collapse；你已明确取消后者。
- 思考文字强制跑马灯、token 逐字动效、过多 shimmer：优先内容稳定和可读。
- 没有后端语义的分叉、广域审批、排队或自动重试按钮。
- 所有代码都加编辑器、所有工具都加 JSON 卡片、所有回复都加一排动作。

## 推荐实施顺序与验收

| 顺序 | 工作 | 先锁定的验收场景 |
|---|---|---|
| 第一批 | 修正文解析、文件误链接、内外滚动冲突 | 本报告的表格/链接/图片/点号符号样例；输出框内上滚不改变外层跟随；先红后绿 |
| 第二批 | 普通工具摘要/详情与错误展示 | 超过 200 字的错误仍能检查保留详情；摘要不声称完整；需要用户动作始终可见 |
| 第三批 | 真实增量正文与稳定消息节点 | 首段内容及时出现、停止后不丢内容；历史不重播；复制和阅读选区在更新后仍可信 |
| 第四批 | 轮尾操作、真实耗时和改动摘要 | 不重复标签/复制入口；耗时与原 run 关联；重试不污染原记录 |
| 按需新增 | 结构化提问、消息取回/分叉、长会话挂载 | 明确的协议身份、关闭/重连状态、合法分支边界和长历史基准 |

前三批不会通过单纯统一 CSS 完成。建议每批先给可点击的真实组件样例，再按你确认的交互走 TDD；无需因此更换整个前端框架。

## 主要源码入口

- Codex：指定安装目录的 `app/resources/app.asar`，已定位导入链和消息文案；对应 [证据索引](/E:/codey/.e2e-artifacts/ui-audit-20261016/codex-evidence.json)。关键模块：`local-conversation-thread-202832e5d9b0.js`、`local-conversation-turn-dc4d2ba50bdd.js`、`user-message-7c7a7aa7c1df.js`、`tool-activity-disclosure-4c100a457e83.js`、`collapsed-turn-disclosure-eaab8efbe079.js`、`virtualized-turn-list-be4a8ce764ec.js`。
- Wish：[正文](/E:/codey/reference-projects/wish-web/src/features/sessions/entries/AssistantEntry.vue)、[过程](/E:/codey/reference-projects/wish-web/src/features/sessions/entries/ProcessGroup.vue)、[详情](/E:/codey/reference-projects/wish-web/src/features/sessions/entries/ProcessDetail.vue)、[提问](/E:/codey/reference-projects/wish-web/src/features/sessions/entries/QuestionCard.vue)、[列表](/E:/codey/reference-projects/wish-web/src/features/sessions/ChatLog.vue)、[Markdown](/E:/codey/reference-projects/wish-web/src/ui/markdown.ts)。
- OpenCode：[时间线](/E:/codey/reference-projects/opencode/packages/session-ui/src/timeline/session-timeline-row.tsx)、[Markdown](/E:/codey/reference-projects/opencode/packages/session-ui/src/components/markdown.tsx)、[工具错误](/E:/codey/reference-projects/opencode/packages/session-ui/src/components/tool-error-card.tsx)、[跟随滚动](/E:/codey/reference-projects/opencode/packages/ui/src/hooks/create-auto-scroll.tsx)、[shell 输出](/E:/codey/reference-projects/opencode/packages/session-ui/src/tools/shell-output.ts)。
- Percho：[行投影](/E:/codey/reference-projects/percho/packages/shared/src/transcript/chat-rows.ts)、[消息列表](/E:/codey/reference-projects/percho/packages/desktop/src/renderer/src/components/chat/MessageList.tsx)、[Markdown](/E:/codey/reference-projects/percho/packages/desktop/src/renderer/src/components/chat/Markdown.tsx)、[错误](/E:/codey/reference-projects/percho/packages/desktop/src/renderer/src/components/chat/ErrorNote.tsx)、[消息动作](/E:/codey/reference-projects/percho/packages/desktop/src/renderer/src/components/chat/message-actions.tsx)。
- Harness：[聊天视图](/E:/codey/reference-projects/deepseek-harness/packages/client/ui-chat/src/client/chat/ChatView.tsx)、[正文](/E:/codey/reference-projects/deepseek-harness/packages/client/ui-chat/src/client/chat/AssistantMarkdown.tsx)、[增量 Markdown](/E:/codey/reference-projects/deepseek-harness/packages/client/ui-primitives/src/markdown/MarkdownText.tsx)、[语法](/E:/codey/reference-projects/deepseek-harness/packages/client/ui-primitives/src/markdown/parse.ts)、[消息动作](/E:/codey/reference-projects/deepseek-harness/packages/client/ui-chat/src/client/chat/MessageIconActions.tsx)、[命令详情](/E:/codey/reference-projects/deepseek-harness/packages/client/ui-chat/src/client/chat/GenericCommandCard.tsx)。

本报告不宣称 Codey 全面优于或劣于任何参考项目，也不把源码分析等同于它们的实机端到端验收。
