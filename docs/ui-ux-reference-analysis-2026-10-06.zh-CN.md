# Codey UI/UX 参考项目分析与吸收建议

研究日期：2026-10-06。本文是设计研究与候选建议，不改变现行 DESIGN.md，也不代表功能已经实现或已通过用户测试。

## 1. 结论

Codey 值得吸收的是这些项目对**工作连续性、操作反馈、信息分层和结果检查**的处理。其现有的黑灰白、去气泡、少入口和本地工具气质已经成立，无需靠换主题、加卡片或增加模式来提升易用性。

建议优先让用户能够：

1. 直接读到完整结果。
2. 切换会话后继续自己的草稿和阅读位置。
3. 明确知道哪个会话正在工作、是否在等自己审批。
4. 发送失败后保住输入，并知道下一步怎么恢复。
5. 用键盘完成与鼠标相同的操作。
6. 在需要时找到旧会话、检查改动和研究来源。

设计取舍可以归纳为：**空闲时安静；工作时有事实反馈；需要用户行动时入口可见；细节按需展开；用户自己的输入与阅读位置受到保护。**

## 2. 研究范围与证据边界

阅读了五个本地 checkout 的 README、相关设计/样式文档、主要界面组件、状态管理代码及部分交互测试，并查看仓库现有截图。Codey 对照范围包括 DESIGN.md、PRODUCT_PRINCIPLES.zh-CN.md、README.md、codey/web/index.html、assets 中的渲染/输入/状态/抽屉模块与相关 UI 测试。

本次没有安装或启动五个参考产品，没有进行端到端实操或真实用户测试。因此下文区分“源码确认的行为”“截图观察”和“建议”。截图可能早于源码，尤其 opencodex 的 dashboard.png 显示 v0.0.1，不作为当前导航或组件细节的唯一依据。对于代码路径暴露的问题，下文给出可复核的触发场景，仍需在实施时用浏览器交互验证。

本地基准提交如下；它们不是联网查询的最新版本：

| 项目 | 本地 HEAD | 提交日期 | 产品定位与参考价值 |
|---|---|---|---|
| Codey | `02508d28` | 2026-10-05 | 本地 AI 编程与研究；本文的目标产品 |
| deepseek-harness | `da00f7f535` | 2026-10-03 | 通用 agent Web/桌面界面；输入、过程分层、工作区与阅读状态 |
| dsh-desktop-anywhere-labs | `a1ff68b296` | 2026-10-03 | 固定上游版本的社区桌面壳；桌面布局、初次配置和恢复流程 |
| dsh-desktop-fsw2781890522 | `1a08dfe` | 2026-09-11 | 同属 DSH 系的个人桌面分支；原生窗口与内容区域的整合 |
| opencodex | `93cdffd6a` | 2026-10-05 | 模型代理及配置 dashboard；配置归属、保存状态、故障恢复 |
| percho | `66df829` | 2026-09-30 | Pi agent 桌面 GUI；会话草稿、过程摘要、按轮次检查改动 |

### 三个 DSH 仓库需要作为同源家族看待

anywhere-labs 的 AGENTS.md 明确说明它围绕未修改的固定上游 checkout 构建。upstream.json 分别记录 Stable/Beta 上游版本；Next 也强调使用官方前端。fsw 分支保留同一套 packages/client 产品模块，另有桌面窗口与玻璃侧栏的改造。

所以“这三个项目都有同一种 composer”主要说明设计继承，不能当作三份独立验证。本文将它们统称为 **DSH 家族**；跨产品共性再与 Percho、opencodex 比较。

opencodex 的主要 GUI 是配置与运行管理面板，不是聊天工作台。不能把其他产品的“会话树、工具过程、composer”说成五者共有。

## 3. 各项目的 UI/UX 特征

### deepseek-harness：任务为中心，复杂性按层展开

- AppFrame 管理左侧导航、中间工作区和按需出现的右侧内容；左右区域支持宽度调整与窄窗口收敛。
- 工作区与会话形成主导航。WorkspaceBrowser 提供搜索，行菜单承载 rename、pin、fork、archive 等次级动作。
- composer 集中承载模型与任务输入；输入状态按 Session 持有，失败提交可以恢复输入，同时保护用户随后新打的文字。
- Chat 将过程和最终回答区别处理。符合条件的已完成过程可以折叠，最终回答保留。ui-chat 与 Trajectory 各有用途，不要求普通用户持续阅读所有运行细节。
- 阅读历史时管理语义锚点、跟随尾部状态和 Back to bottom；文件引用可进入右侧预览。
- ApprovalPanel 在待审批时占用 composer 位置，让用户直接看到当前所需动作。

最有价值的是状态和信息分层。蓝色强调、shimmer、复杂模式、统计 dock、Trajectory 常驻入口和大型预览体系都不适合直接移植到 Codey。

### dsh-desktop-anywhere-labs：把前端纳入桌面操作流程

- AdvancedFrame/ExtendedFrame 通过桌面自己的布局状态组合上游 sidebar/main/rightbar；针对平台处理标题栏、收起和拖动。
- onboarding 依据 setup/account/restart 的实际状态显示下一步；可跳过账号配置，设置完成与需要重启是不同反馈。
- 除主聊天界面外，产品还拥有 Profiles、恢复界面、插件和远程控制等入口。

可借鉴“显示实际未完成步骤”和“明确设置何时生效”。Codey 不应因此引入 Profile 体系、社区市场或远程操作入口。截图中的上游聊天气泡和技术注入文本也不是 Codey 的目标语言。

### dsh-desktop-fsw2781890522：窗口外壳与工作区的视觉整合

- 桌面 README 和 window.png 展示标题栏、侧栏相连的玻璃外壳，以及不透明的主体内容区域。
- AppFrame.module.css 明确将透明与背景采样限制在侧栏外壳，避免聊天正文受到桌面背景影响。
- 仍然使用 DSH 的会话、模型引导、composer、审批及工具过程模块。

值得吸收的原则是“正文阅读面稳定、平台按钮不挡交互、外壳与内容职责清楚”。不建议复制 Acrylic、壁纸采样、混合大圆角或半透明侧栏；它们增加视觉变量和平台维护成本，对 Codey 的编程闭环帮助有限。

### opencodex：给配置明确的归属与状态

- GUI 使用统一 tokens、字阶、outline 图标与导航组；其信息架构面向 Providers、Models、Connect、Usage & Logs 等配置任务。
- 设计文档要求账号管理只出现在指定 Provider 的 Accounts 中，减少重复入口；高级模型分配折叠，只有一个页面时不展示多余 tab strip。
- ClaudeCode 页面有固定保存栏，区分未保存、保存中和当前无更改。刷新与迟到的保存响应不会直接覆盖用户正在编辑的 draft。
- 字体不依赖外部 CDN；窄屏行为、菜单与 listbox 的键盘移动、关闭后的焦点恢复都有明确要求。
- 加载失败、空结果、未认证、正在切换等状态不依赖颜色单独传达。

Codey 应吸收“一个设置只在一个地方管理”“表单输入不被刷新覆盖”“保存与连接是不同事实”。不应复制首页指标卡、代理路由、账号池、费用 dashboard 或多层管理导航。

### Percho：把会话工作做到连续、可回看

- 左栏区分日常对话和项目，会话搜索、置顶和分批显示降低历史增长后的查找负担。
- drafts.ts 按会话保存文本与引用；新会话页有独立草稿 key。这里主要是内存态，不应误称为已经永久保存全部草稿。
- MetaGroup 聚合工具与思考过程；正文仍是独立的主要阅读内容。
- MessageList 区分跟随尾部和阅读历史，提供回到底部操作，并处理历史加载造成的锚点变化。
- DiffSidebar 按轮次分组，区分全部和最近一轮，能够从消息中的改动摘要定位到具体内容。
- ErrorNote 将错误类别与适用动作关联，例如重试、配置、复制细节；ApprovalDock 把待审批事项放在输入位置。

草稿、过程摘要、结果定位最值得借鉴。宠物、背景图、ThinkingOrb、彩色状态点、权限档位、顶栏置顶胶囊和 UI 插件体系都不适合直接进入 Codey。ApprovalDock 的全局审批快捷键也不能照搬；Codey 的发送 Enter 与审批确认应有清楚的焦点隔离。

## 4. 共同点：哪些是跨产品原则，哪些只是同源功能

| 共性 | 证据范围 | 用户获得的收益 | Codey 的吸收方式 |
|---|---|---|---|
| 固定导航与当前内容分开 | DSH、Percho、opencodex | 不需要反复判断自己在哪 | 保留 sidebar + main；不要新增首页 dashboard |
| 统一 tokens、字阶、图标和交互控件 | 三个产品家族 | 相同操作的外观和行为可预测 | 保留 tokens.css；补统一的 focus/menu/disclosure 行为 |
| 普通路径简单，高级信息按需展开 | 三个产品家族，具体承载不同 | 降低第一次使用时的理解成本 | 结果可见、过程折叠、细节点击查看；不用复制卡片 |
| 输入/编辑状态归属于当前对象 | DSH、Percho 的会话；opencodex 的配置对象 | 减少丢字、串台和重复输入 | 草稿按会话隔离；Local 配置与聊天草稿各自拥有状态 |
| 错误有明确反馈和对应恢复动作 | 三个产品家族 | 用户知道下一步做什么 | 短错误行 + 适用的文本动作；重要失败不只放 toast |
| 项目组织会话，搜索处理历史增长 | DSH、Percho；不适用于 opencodex 会话树 | 容易回到以前的任务 | 在既有左栏增加轻量搜索；置顶和 archive 后置 |
| composer 是任务输入及必要交互的焦点 | DSH、Percho | 不必到多个区域操作当前任务 | 保留唯一模型入口；待审批时在附近显示事实入口 |
| 过程与答案分层 | DSH、Percho | 可了解发生了什么，同时快速读结果 | 复用已有只读工具分组，结果默认展开 |
| 阅读历史时避免被新输出打断 | DSH、Percho | 长任务仍能查阅旧内容 | 补会话阅读锚点和 Back to latest |
| 结果/文件有独立检查面 | DSH 的预览、Percho 的 diff；opencodex 是配置 detail | 能检查答案背后的实际对象 | 强化现有 Changes/Research 抽屉与精确定位 |
| 键盘焦点和窄窗口状态受到处理 | 三个产品家族，完整程度不同 | 更少鼠标依赖和不可达控件 | 原生按钮、focus-visible、焦点返回、隐藏区域 inert |

它们并不共同采用 Codey 的“严格单色”或“去气泡”：DSH 和 Percho 有用户消息气泡，参考项目普遍存在彩色状态/强调，且可支持浅色主题。应借鉴交互机制，不以外观出现次数决定是否吸收。

## 5. Codey 已有的优势与实际缺口

DESIGN.md 和产品原则支持一个小而稳的本地工作台：自然语言发起工作，改动可见、来源可追溯、错误可恢复，避免平台工程和多 agent 群聊 UI。

### 已有，不应再算成新增方案

- 260px 可收起侧栏、项目与非项目对话分组、760px 正文与 composer 最大宽度。
- 项目 breadcrumb、会话级模型选择、会话级 Research。
- 单色 hover/active、次级操作菜单、去气泡消息、sans 正文与 mono 技术内容。
- inline rename 和两步危险操作确认；不是仍在用 prompt() 的待办。
- Markdown、消息复制、代码复制。
- DONE/ERROR/PAUSED/Limit 与 Details、View diff、Retry、Continue 等动作。
- Changes、Research、Local context 三个互斥抽屉。
- SSE 重连与状态对账；不是完全没有恢复机制。
- `scrollChat()` 已有离开底部时不强制追尾的基础策略。
- 连续同类成功只读工具 `read/ls/search/references` 已经折叠；pending、error、write、edit、shell 不在该分组规则内。

DESIGN.md §11 仍把工具折叠、inline rename 和 Markdown 列作 future work，已经落后于实现。应更新文档的状态归类，避免下一次设计重复建设。

### 需要先处理的缺口

| 缺口 | 源码依据与触发场景 | 判断 |
|---|---|---|
| 长回答默认折叠，与设计直接相反 | render.js 超过 400 字符或至少 6 个换行即视为长回答；renderAssistantBody 先显示 preview 并设 collapsed/Expand；DESIGN.md §5.5 要求默认展开 | 先修到设计基线，比新增显示模式更有收益 |
| 草稿仍由一个共享 textarea 持有 | switchSession/newSession 更换 activeId 并重绘聊天，没有存取会话草稿；ui_state 的 session 对象也没有独立 draft | A 输入后切到 B，文字仍在但归属已变；这是错投风险，不应简单描述为“切换必丢字” |
| 发送在成功受理前消费输入 | sendTaskFromSession 调 onSendStarted、添加 user 行后才 POST；失败添加泛化错误 | 文字有聊天记录，不是彻底消失；但继续编辑与重试的成本高，应保留明确的提交恢复状态 |
| 选择文件夹会顺带发草稿 | composer 的 ctx-folder 在有文字时传 sendDraft=true；attachCurrentChatToPickedProject 随后发起任务 | 这是现有测试锁定的行为；建议改为选目录后仍由 Send 发送，需要有意识地修订行为和测试 |
| 切换会话总是追到尾部 | switchSession 调 renderChat(true) | 已有基础追尾控制，但缺少返回旧会话时的阅读位置恢复 |
| 运行状态与当前会话的关系不够明显 | updateSend、provider 禁用与 Stop 依据全局 runningSessionId；任务状态显示 Running | 查看 B 时可能受到 A 的运行状态限制；应明确当前操作目标 |
| 待审批主要存在于消息历史里 | shell_request 添加一条 shell-card；该事件分支没有独立的待审批定位反馈 | 用户读历史或切走时可能不知道任务在等自己；需浏览器复现可发现性 |
| 一些可点击行和展开控件缺少键盘语义 | sessionNode 用 onclick div；tool-group-summary 与长回答 toggle 也是 onclick div | 鼠标能点不等于键盘能用；已有复制和 Research 的焦点处理应延伸到其余控件 |
| 行操作只在 hover 显示 | project-more 是 display:none，session-more 是 visibility:hidden；没有对应 focus-within reveal | 需要键盘和无 hover 设备的替代可见规则 |
| 抽屉隐藏与焦点管理还不完整 | setDrawerOpen 只改 class 与 aria-hidden；隐藏主要依赖 transform；侧栏收起主要改宽度 | aria-hidden 不负责阻止 Tab；应补 inert/焦点恢复，并在实操时验证 |
| 抽屉打开后仍覆盖阅读面 | 固定右侧宽度 min(560px,92vw)，main 没有为它腾出轨道 | 大屏可研究并排；中小屏需要明确单面阅读策略 |
| 抽屉的对象归属可能和主区不同 | Changes 绑定 activeChangesProject；切会话的统一 scope hook 目前只通知 Local context | 不应直接宣称 Restore 会写错项目；实际问题是用户容易误读“现在显示谁的改动” |
| 模型状态不能据灰点推断故障原因 | provider_tab_availability 对网页 provider 主要检测已打开 tab；UI 主要消费 available 布尔值 | 提升文案需要新增可靠状态事实，不能猜“没登录/限流/已就绪” |

## 6. 吸收优先级与具体改造

这里的 P0/P1/P2 表示设计实施顺序，不是安全漏洞等级。

### P0：先让既有路径可靠、可发现

**A. 最终回答默认完整可读。**

按现行 DESIGN.md 修正长回答默认态，底部保留安静的 `Collapse`。工具过程仍可默认折叠。长文性能继续复用已有分块渲染；不要为了性能把结果变成需要额外点击才能读的预览。展开/收起使用原生 button 与 aria-expanded，避免重绘时重置用户选择。

**B. 按会话隔离草稿，明确提交状态。**

先做文本草稿与光标/选区的内存隔离；切换回来恢复 A 的输入，新会话有自己的草稿，删除会话释放它。需要跨重启保留时，再明确本地保存范围与清理规则。

Codey 当前一次只运行一个任务，最小方案是短暂 `Sending…` 状态、避免重复提交、成功受理后消费该提交快照；失败保留可编辑文本。若未来采用乐观清空，应像 DSH 一样拥有明确的待提交记录和恢复规则，不能覆盖后来新输入。

目录选择只改变输入的项目归属，发送由 Send/Enter 明确触发。这会改变现有行为，需同步更新 ctx-folder 相关测试；本报告没有实施这一改动。

**C. 补完整的键盘操作和焦点规则。**

- 会话主动作、过程展开、回答展开使用语义按钮；含 `⋯` 的整行不要做嵌套 button。
- `⋯` 在 hover 或 focus-within 显示；无 hover 设备保留可达入口。
- 菜单打开后有可预测的初始焦点、方向键操作；Escape 返回触发器。
- 隐藏 sidebar/drawer 使用 inert；关闭抽屉把焦点返回原来的 View diff/Open。
- 全局 focus-visible 使用灰白描边，不增加第二种强调色。
- 延伸已有 IME Enter 防误发送到 rename、搜索和其他编辑场景。
- `prefers-reduced-motion` 下收敛抽屉/侧栏过渡；spinner 需要保留文字状态作为替代信息。

这属于完成 DESIGN.md 的 Accessibility 要求，视觉气质无需改变。§5.3 的“hidden until hover”应改为“hover or keyboard focus；无 hover 设备可见”。

**D. 明确工作所属会话与待审批事项。**

只有 runningSessionId 对应会话显示灰色 spinner；切到其他会话时，顶栏用一行 `Running in <chat> · Open` 指明当前任务。Stop 的可访问名称也应指出目标。不要解锁其他会话发送来假装已有并发支持。

有未决审批时，以 `Approval required · Review command` 在 composer 附近提示并定位到现有审批卡；若审批在另一个会话，先打开该会话。复用同一个审批状态和处理函数。第一阶段不必复制一份新的审批表单，也不必完全替换 composer。

仍然保持 Deny 左、Allow 右、工作目录和命令清楚可读。不得用全局 Enter 自动变成批准；Enter 只在明确聚焦的 Allow 按钮上确认。普通发送与审批失败都需要明确的未成功状态。

### P1：降低查找和检查成本

**E. 会话搜索先于置顶、归档和顶部标签。**

DSH 与 Percho 都用搜索处理历史增长。Codey 先提供一个灰色 Search chats 入口，搜索当前已有会话标题与项目名。搜索结果带项目归属，搜索时临时展开，不覆写用户原有折叠状态。

若以后搜索正文，再明确加载范围与结果限额；只搜索已载入消息时要说明范围。不要一次加入标签系统、置顶 tab strip、智能文件夹和多种排序。

**F. 保存阅读位置，提供 Back to latest。**

复用现有离底阈值策略，增加按会话的消息锚点与偏移记录。离开尾部且有新内容时才出现灰色 `Back to latest`。返回 B 再回 A，仍能读 A 的原位置；新内容、复制或展开工具不强制改为跟随尾部。

扩展现有 test_ui_inplace_render 的用户场景，不只检查某段 CSS 或函数名称。

**G. 失败文案与下一步动作匹配。**

保留现有 status-row，不引入彩色 error card。任务失败留在消息里；重命名/复制等瞬时操作可以用灰色 toast。

用户-facing 文案尽量表达事实和可做的下一步。例如已确认未登录时显示 `Sign in in the browser`；连接不可用时提供 `Retry`；本地配置错误显示在原表单旁。Retry 应绑定造成该错误的原提交，而不是任意取当前最后一条用户消息；用户已有新草稿时不能先覆写它再重发。

仅凭网页 tab 的 available 布尔值不能承诺“模型已经可服务”。如果没有类型化故障原因，保留 `Connection unavailable`，把灰点与可靠的详细状态区别处理。

**H. 强化现有 Changes/Research 抽屉的对象归属。**

- 抽屉标题旁明确显示当前绑定项目/研究会话的名字；完整路径按需查看。
- 切换主会话时，优先关闭旧抽屉，降低同时展示两个项目对象的误读。若未来保留旧抽屉，必须显示旧对象，并让所有动作继续绑定它。
- 大屏的 Changes/Research 可研究让 main 为抽屉腾出宽度；760px 是剩余主区的最大宽度，不是强制宽度。
- 如果宽度不足以并排阅读，就使用现有覆盖抽屉或单面查看，不把主区压成很窄一条。是否引入大屏停靠应通过 1024/1280/1440/1920 等窗口实测决定。
- 初期保留固定宽度，不急着增加拖动、dock、多 tab、浏览器、终端和编辑器。

大屏停靠将改变 DESIGN.md §4/§5.7 的 fixed-right 描述，若实施应在同一变更说明理由。Percho 的 DiffSidebar 实际也是覆盖层，不能把“所有参考都并排”作为改造理由。

**I. 检查改动时区分当前工作区与某次任务。**

Percho 按轮次记录改动很有价值，但 Codey 当前 Changes 读取的是项目 Git/Snapshot diff，不能直接加 `This run` 标签冒充按次隔离。

第一阶段明确 `Working tree` / `Snapshot` 范围，并从结果摘要打开已有检查面。只有后台能提供真实的 run before/after 证据后，才加入 `This run` 与 `All changes`。Restore 同时标明影响范围；普通 Git 模式目前隐藏 Restore，不能为了与参考产品一致而随意补一个按钮。

Research 同样应加强 Sources/Notes/Evidence 间的定位和返回，但保留已存 provenance 的边界，不从任意答案文本猜来源，也不把主入口改名为 Vault/Knowledge。

### P2：需要使用证据再做

| 候选 | 适合的轻量版本 | 后置原因 |
|---|---|---|
| @ 文件补全 | 在当前项目内补全文本路径，预览相对路径；不做大型附件系统 | 新增读取/引用语义需要后端范围与模型能力支持，不能只画一个 chip |
| 忙碌时的 follow-up | 先允许保留下一条草稿，结束后用户显式发送 | 自动排队、steer 需要任务中断与提交顺序支持；不能仅解禁 Send |
| 会话 pin/archive | 少量文本菜单操作和左栏排序 | 先确认搜索仍无法解决真实查找需求；不先加顶栏标签 |
| 字体/缩放 | 全局有限的正文尺度，保持 tokens 与横向布局 | 值得做可读性验证，当前没有使用证据决定需要何种设置入口 |
| 文件预览 | 点击一个明确文件链接后按需查看 | 大型文件树、多文件编辑器、终端和内嵌浏览器容易改变 Codey 体量 |

## 7. 建议怎样补 DESIGN.md

不需要改色板、去气泡、English chrome、唯一模型入口或唯一 Research 入口。建议在认可后补以下行为条款；本文只是候选文本。

### 候选条款：工作连续性

> Composer drafts belong to a chat. Switching chats preserves each chat's text and selection. Choosing a folder updates context; it does not submit the draft. Failed submissions preserve editable input and do not overwrite later edits.

### 候选条款：状态可发现性

> Idle states stay empty. Active work identifies its chat when another chat is being viewed. Pending user action has one visible entry near the composer. Status is derived from recorded runtime facts, never inferred from decorative indicators.

### 候选条款：渐进披露

> Final answers are expanded by default. Repeated successful read-only actions may fold into a plain summary. Pending approvals, failures, writes, and shell results remain discoverable. Disclosure controls work with both keyboard and pointer input.

### 候选条款：焦点和隐藏区域

> Secondary actions appear on hover or keyboard focus; devices without hover retain an accessible entry. Hidden panels are inert. Escape dismisses the active surface and returns focus to its trigger. Enter never approves a command globally.

### 候选条款：阅读状态

> Reading position belongs to a chat. New output follows the tail only while the reader is following it. A quiet Back to latest action appears when useful; opening details does not force the transcript to the bottom.

### 候选条款：检查范围

> Every inspection surface identifies the chat, project, or run it displays. A working-tree diff must not be labeled as a run-specific diff without verified run snapshots. Recovery actions state their actual scope.

### 文档维护

- 将 §11 已完成的工具折叠、inline rename、Markdown 移入当前能力说明。
- 保持 §5.5 长回答默认展开，修正相反实现。
- 为 §5.3 次级操作补 focus/no-hover 规则。
- 扩展 §10 Accessibility，而不是用“单色”替代交互可达性。
- 只有决定实施停靠式抽屉时才修改布局条款；目前继续遵守 fixed-right。

## 8. 建议的最小实施批次与验收

**第一批：回答、输入与键盘。** 修正长回答默认态；按会话草稿；发送失败恢复与防重复；目录选择/发送分开；sidebar/menu/disclosure 的键盘操作。界面布局和主要入口基本不用增加。

**第二批：工作状态与阅读。** 活跃任务归属、待审批定位、会话阅读锚点、Back to latest、明确错误恢复动作。

**第三批：历史与检查。** 标题搜索、抽屉对象归属、真实 diff 范围；实测后再决定大屏停靠。@ 引用、队列、pin/archive 留给使用证据。

实施后的验收应看用户行为，而不仅是静态样式：

1. A 写了一段尚未发送的需求，切到 B 写另一段，再回 A：文本、光标、模型与项目归属正确；选择文件夹不触发执行。
2. 发送时模拟离线/409，期间又输入新文字：旧提交可恢复，新文字不被覆盖；不产生未经说明的重复任务。
3. A 运行时查看 B：能知道工作属于 A，Stop 目标明确，B 的输入可保留但不会绕开单任务限制。
4. 阅读历史时出现 shell 审批：用户看得到待处理入口；跳转后 cwd/命令/范围清楚；Enter 输入行为不会批准它。
5. 用 Tab/方向键/Enter/Escape 完成切会话、菜单、工具展开、抽屉查看与返回；隐藏面板不会吸走焦点；IME 提交不误触动作。
6. 一个较长回答，无需额外点击即可读到结论；收起再展开仍完整，代码围栏和 Markdown 正确。
7. 上翻历史后切会话再返回：仍在原位置；新输出不抢位置；Back to latest 只在需要时出现。
8. 在窄窗口、短窗口与不同缩放下，模型菜单、drawer header 和 Allow/Deny 都可见可达；大屏检查面有足够阅读空间。
9. A 的 Changes/Research 打开后切到 B：不会无标识地显示 A 的对象；迟到响应不把 B 的检查面覆写成 A。
10. 展示 This run 或执行 Restore 时，能证明该标签/动作的实际范围；无可靠数据时不补假统计。

这些验收是后续实施建议。本次没有执行它们，也没有声称参考产品的所有实现都满足它们。

## 9. 证据索引

下面的 reference-projects 链接面向当前本地工作区；这些 checkout 被 .gitignore 排除，报告上传后读者需要自行取得对应源码。DESIGN.md 和 Codey 源码是仓库内可持续引用的依据。

### Codey

- [设计基线](../DESIGN.md)：§4 布局；§5.3 行操作；§5.5 消息/长回答；§5.6 composer；§5.7–5.9 抽屉；§10 无构建与可访问性；§11 future work。
- [产品原则](../PRODUCT_PRINCIPLES.zh-CN.md)：低门槛、可见可控、自然语言触发、少量强工具、拒绝插件市场和群聊式多模型 UI。
- [当前界面与动作](../codey/web/index.html)：sessionNode 392、renderChat 499、scrollChat 761、switchSession 770、attachCurrentChatToPickedProject 924、retryTask 1460、scope hook 1535 附近。
- [输入与发送](../codey/web/assets/composer.js)：updateSend 31、clearDraftIfUnchanged 61、sendTaskFromSession 69、目录入口 160 附近。
- [回答与工具分组](../codey/web/assets/render.js)：长文阈值 101、renderAssistantBody 310、foldable kinds 343、createToolGroup 369、appendOrFoldTool 403 附近。
- [状态与抽屉切换](../codey/web/assets/ui_state.js)：defaultSession 与 setDrawerOpen。
- [样式](../codey/web/assets/app.css)：project/session more 的 hover reveal；changes-drawer 的 fixed/transform；现有复制动作的 focus-within。
- [模型显示状态](../codey/web/assets/provider_ui.js)、[tab availability](../codey/providers/registry.py)：UI available 布尔值与网页 tab 检测的实际含义。
- [Changes](../codey/web/assets/changes_drawer.js)、[Research](../codey/web/assets/research_drawer.js)、[SSE](../codey/web/assets/sse.js)：现有检查面、范围与重连对账。
- [UI 静态测试](../tests/test_ui.py)：已有 copy、inline rename、readonly fold 与选择目录后提交的行为约束。
- [原位渲染测试](../tests/test_ui_inplace_render.py)：已有 DOM/选区保留与离底不追尾场景。阅读测试不等于本次运行测试。

### DSH 上游

- [样式规范](../reference-projects/deepseek-harness/docs/web-styling.md)：统一 token、菜单、focus/reduced motion 与字体规则。
- [AppFrame](../reference-projects/deepseek-harness/packages/client/ui-layout/src/client/AppFrame.tsx)：三列、ResizeObserver、宽度求解和拖动。
- [Chat 文档](../reference-projects/deepseek-harness/packages/client/ui-chat/README.md)：最终回答与过程区别、Scroll ownership、隐藏内部注入行、结果检查。
- [Conversation 文档](../reference-projects/deepseek-harness/packages/client/ui-conversation/README.md)：per-session input、提交 echo、失败输入恢复、Queue/Steer 和 composer takeover。
- [WorkspaceBrowser](../reference-projects/deepseek-harness/packages/client/ui-workspace/src/client/rows/WorkspaceBrowser.tsx)：工作区会话导航和搜索。
- [ModelSelect](../reference-projects/deepseek-harness/packages/client/ui-model-selection/src/client/ModelSelect.tsx)：唯一 composer 模型位、分组/条件搜索、菜单错误反馈。
- [ApprovalPanel](../reference-projects/deepseek-harness/packages/client/ui-approval/src/client/ApprovalPanel.tsx)：待审批 takeover 与焦点范围内快捷键。
- [工具呈现文档](../reference-projects/deepseek-harness/packages/client/ui-tool/README.md)：工具生命周期、collapsed summary 与逐项检查。

### DSH 两个桌面项目

- [anywhere-labs 仓库规则](../reference-projects/dsh-desktop-anywhere-labs/AGENTS.md)、[上游固定版本](../reference-projects/dsh-desktop-anywhere-labs/upstream.json)：独立桌面所有权与同源关系。
- [AdvancedFrame](../reference-projects/dsh-desktop-anywhere-labs/dsh-plugin-desktop/src/client/AdvancedFrame.tsx)、[ExtendedFrame](../reference-projects/dsh-desktop-anywhere-labs/dsh-plugin-desktop/src/client/ExtendedFrame.tsx)：上游 slot 与桌面布局组合。
- [桌面 onboarding](../reference-projects/dsh-desktop-anywhere-labs/dsh-plugin-desktop/src/client/onboarding.tsx)：setup/account/restart 的分步事实反馈。
- [anywhere-labs 聊天截图](../reference-projects/dsh-desktop-anywhere-labs/assets/desktop-chat-en.png)：对外观作补充，不代替当前源码。
- [fsw 桌面 README](../reference-projects/dsh-desktop-fsw2781890522/apps/desktop/README.md)、[窗口截图](../reference-projects/dsh-desktop-fsw2781890522/apps/desktop/docs/window.png)：窗口与内容面整合。
- [fsw AppFrame 样式](../reference-projects/dsh-desktop-fsw2781890522/packages/client/ui-layout/src/client/AppFrame.module.css)：侧栏 glass、正文稳定表面、拖动与 reduced motion。
- [fsw 模型初次引导](../reference-projects/dsh-desktop-fsw2781890522/packages/client/ui-settings-models/src/client/DeepSeekOnboardingDialog.tsx)：依 readiness 决定是否需要配置。
- [fsw Conversation 文档](../reference-projects/dsh-desktop-fsw2781890522/packages/client/ui-conversation/README.md)：继承的提交恢复、运行时输入和审批承载。

### opencodex

- [Foundations](../reference-projects/opencodex/gui/design-system/foundations.md)、[Components](../reference-projects/opencodex/gui/design-system/components.md)：单一 design system、导航归属、配置层次、焦点与窄屏行为。
- [当前导航组](../reference-projects/opencodex/gui/src/nav-groups.ts)：dashboard 与管理导航，而非项目会话树。
- [ClaudeCode](../reference-projects/opencodex/gui/src/pages/ClaudeCode.tsx)、[保存栏交互测试](../reference-projects/opencodex/gui/tests/claudecode-save-bar.test.tsx)：dirty/clean、迟到保存响应与编辑状态保护。
- [共享 UI](../reference-projects/opencodex/gui/src/ui.tsx)：select 的键盘和焦点恢复。
- [历史 dashboard 截图](../reference-projects/opencodex/assets/dashboard.png)：外观补充，版本较早。

### Percho

- [项目 README](../reference-projects/percho/README.md)、[左栏截图](../reference-projects/percho/docs/assets/img/chat_002.png)、[diff 截图](../reference-projects/percho/docs/assets/img/chat_003.png)：产品范围和界面观察。
- [草稿状态](../reference-projects/percho/packages/desktop/src/renderer/src/stores/drafts.ts)：bySession 与新会话草稿。
- [Sidebar](../reference-projects/percho/packages/desktop/src/renderer/src/components/sidebar/Sidebar.tsx)、[SessionRow](../reference-projects/percho/packages/desktop/src/renderer/src/components/sidebar/SessionRow.tsx)：导航、分批、inert、语义按钮和状态点。
- [MetaGroup](../reference-projects/percho/packages/desktop/src/renderer/src/components/chat/MetaGroup.tsx)、[MessageList](../reference-projects/percho/packages/desktop/src/renderer/src/components/chat/MessageList.tsx)：过程摘要、阅读/跟随尾部与回到底部。
- [ApprovalDock](../reference-projects/percho/packages/desktop/src/renderer/src/components/session/ApprovalDock.tsx)、[ErrorNote](../reference-projects/percho/packages/desktop/src/renderer/src/components/chat/ErrorNote.tsx)：审批承载和分类恢复动作。
- [DiffSidebar](../reference-projects/percho/packages/desktop/src/renderer/src/components/diff/DiffSidebar.tsx)、[ModelPicker](../reference-projects/percho/packages/desktop/src/renderer/src/components/composer/ModelPicker.tsx)：真实的按轮次 diff、覆盖式抽屉、模型搜索与高度约束。
