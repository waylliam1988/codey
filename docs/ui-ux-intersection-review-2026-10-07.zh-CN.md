# 五个参考项目的 UI/UX 交集与 Codey 缺项复核

研究日期：2026-10-07。判断顺序是：确认参考项目的共同点 → 检查当前 Codey 是否已有 → 对实际缺项判断收益、代价和气质适配。本文记录研究结果与候选建议，不修改产品界面或 DESIGN.md。

## 1. 范围与比较方式

五个路径并不对应五个独立设计体系：

| 本地项目 | 本地 HEAD | 比较时的定位 |
|---|---|---|
| deepseek-harness | `da00f7f535` | DSH 上游的 Web/桌面客户端体系 |
| dsh-desktop-anywhere-labs | `a1ff68b296` | 组合官方前端的桌面产品，额外拥有窗口、设置与初次配置流程 |
| dsh-desktop-fsw2781890522 | `1a08dfe` | DSH 个人桌面分支，额外处理原生窗口材质和布局 |
| opencodex | `93cdffd6a` | 模型代理的配置、连接和运行管理 dashboard |
| percho | `66df829` | Pi agent 的桌面聊天与项目工作界面 |
| Codey | `e4ce6e7b` | 本次对照的本地 AI 编程与研究工作台 |

anywhere-labs 的 `upstream.json` 明确记录 Stable/Beta 的上游版本，其桌面包依赖官方 ui-chat、ui-conversation、ui-settings、ui-theme 等前端包。不能因为三个 DSH checkout 出现同一行为，就把它当成三个团队分别验证的结论；也不能假定这些版本的每个细节都相同。

这里把证据分为两层：

- **五者交集**：五个产品都能找到对应的界面模式，允许根据产品定位比较同一原则的不同实现。例如会话编辑与配置编辑都需要正确的对象归属。
- **聊天类项目的共性**：DSH 家族与 Percho 共同采用，但 opencodex 的主 GUI 不具备同样的聊天主路径。不能把常驻 composer、按轮次过程折叠、会话草稿、聊天 diff 等写成五者交集。

阅读范围包括设计/样式文档、布局、设置、导航、输入、过程展示与状态代码，以及有关交互测试。查看了 anywhere-labs、fsw、Percho 和 opencodex 的仓库截图。截图只辅助判断外观：opencodex 的 `dashboard.png` 显示 v0.0.1，不能代表当前导航；也不能把模型回答中的 emoji 当作固定界面文案。

没有安装或启动五个参考产品，没有做真人使用测试。对 Codey 的两个具体缺口，使用现有 HTML/JS 与模拟 API 做了隔离 Chromium 检查；另计算了当前文字 tokens 的名义对比度。其余现状判断以源码为依据。

仓库已有 10 月 6 日的分析、综合建议和实施记录。当前 DESIGN.md 与实现已吸收其中大量建议；本报告以当前版本为准，不重复旧版本的缺项清单。

## 2. 先看真正的交集，再看 Codey

以下“共同采用”不表示参考产品在每条边缘路径上都实现得完善，也不表示五者的具体控件、颜色和尺寸相同。

| 共同点 | DSH 上游 | anywhere-labs | fsw 分支 | opencodex | Percho | 当前 Codey | 是否需要补 |
|---|---|---|---|---|---|---|---|
| 左侧导航与主工作区分开 | 工作区/会话 + Conversation | 官方前端与桌面 Frame | 工作区/会话 + 原生 Frame | 管理导航 + 配置正文 | 项目/会话 + 正文 | 已有 260px 侧栏与正文 | 不补第二套导航或三栏工作台 |
| 中性表面承载大部分内容，文字层级区分正文和元信息 | 主题语义色与 typography | 跟随官方主题 | 跟随主题，侧栏可透明 | surface/text/muted 等 tokens | canvas/ink 等 tokens | 已有四级灰阶、sans/mono 分工 | 保留；检查关键小字可读性 |
| 统一 tokens 和共享控件 | ui-theme、ui-primitives | 官方控件 + 桌面设置样式 | 同源 tokens/控件 | design-system、ui.tsx | globals.css 与 UI 组件 | tokens.css 与共享 class/module | 已有；不照搬 React/组件库实现 |
| 编辑和选择属于具体对象 | 会话输入与设置 namespace | 官方状态 + 桌面设置对象 | 会话/设置状态 | 配置 draft/baseline 与保存状态 | bySession 草稿与配置表单 | 会话草稿、模型/effort、抽屉范围已隔离 | 基础已补齐；设置关闭后的编辑保留可另评估 |
| 控制按作用范围分层 | 会话模型在 composer，连接在 Settings | 官方作用范围，桌面项在设置 | 同源分层 | 连接/账号/模型有明确管理页面 | 会话模型与全局设置分开 | 模型/effort 属于聊天，地址/密钥在 Settings | 已有，不增加重复入口 |
| 复杂内容逐步披露 | 过程展开与自定义配置 details | 官方披露 + 分步设置 | 过程与 ProviderEditor details | Advanced、详情、分组页面 | MetaGroup、错误 details、设置面板 | Process/Thinking、Details、抽屉、Advanced | 已有；终答继续默认展开 |
| 操作有加载、进行中、失败等反馈，失败留在相关操作附近 | 模型菜单错误、设置提交错误 | 设置 loading/failed/saved 与 Retry | 模型设置 busy/failure | Notice、Retry、保存栏状态 | submitting、store.error、ErrorNote | 主任务已有；Settings 首次加载和发送受理失败仍不完整 | **优先补边缘路径** |
| 较大的对象集合能搜索/筛选，并区分空集合与无匹配 | 工作区/会话与模型筛选 | 官方集合搜索 | WorkspaceBrowser 搜索 | Models 等页面筛选 | Sidebar 与 ModelPicker 搜索 | 已有 Search，但空项目名称匹配有遗漏 | **修正搜索语义**，不扩成全局搜索平台 |
| 考虑键盘、语义按钮和隐藏/弹层行为 | primitives、菜单焦点与 reduced motion | 官方交互 + 设置键盘路径 | 官方交互与 reduced motion | Select、导航语义与焦点返回 | Sidebar inert、按钮、模型菜单 Escape | 菜单/对话框/抽屉已覆盖不少规则，行控件语义仍有遗漏 | **补一致性**；不宣称参考产品都已完整无障碍 |
| 支持浅色/深色主题 | Light/Dark/System | ThemePresenter 跟随官方主题 | 同源主题，原生背景适配 | 主题切换与双套 tokens | Light/Dark/System | DESIGN 明确 Dark mode only | 真实缺项，但当前不建议补 |
| 固定界面文案有语言选择/本地化 | locale-owned UI | 官方 locale + 桌面中英文文案 | locale 与中英文文案 | 多语言字典 | zh/en 设置 | DESIGN 明确固定 UI 英文 | 真实缺项；按用户群另做产品决定 |

五者的视觉交集是**中性底色、清楚的层级与统一控件**，并不是“严格单色”“完全去气泡”“统一 760px”或“都没有装饰”。DSH 有品牌色及状态动效，fsw 有原生透明侧栏，Percho 有用户气泡、字标与可选装饰，opencodex 有语义色和管理卡片。

这些差异说明 Codey 的黑灰白、去气泡、760px 正文属于自己的有效设计选择，不需要因参考产品不同而改掉。

## 3. 聊天类共性：Codey 大部分已经吸收

这一组来自 DSH 家族与 Percho，单列以免混入五者交集。

| 共性 | 当前 Codey 证据 | 判断 |
|---|---|---|
| 输入与模型/发送动作组成一个 composer | `.composer-box` 已包含 textarea 和操作栏 | 不再建议“新建共同外框” |
| 输入属于当前会话，切换不串稿 | composer.js 的 drafts Map、文本与选区恢复 | 切换保护已实现；刷新/重启保护是另一需求 |
| 提交失败保留输入，提交过程中防重复 | sendingSessionId、受理后消费快照、revision 检查 | 基础已有，主要缺失败原因与恢复动作 |
| 过程摘要与最终回答分开 | process.js、DESIGN §5.5 | 已有，不再新增 Worked 常驻统计 |
| 历史阅读不被新输出打断 | conversation_ui.js 的 follow/scroll/cache、Back to latest | 已有，不把它列为新功能 |
| 模型选择在会话输入附近 | provider_ui.js，独立 effort 菜单 | 已有，不增加顶栏第二个模型入口 |
| 待用户决定的事项可发现 | composer-notice 定位原审批卡，跨会话运行有 Open | 已有，不用全局 Enter 接管审批 |
| 改动可从结果进入检查面 | Changes 抽屉、Working tree/Snapshot、文件定位 | 已有，不需要常驻 diff 三栏 |

刷新后恢复草稿在 DSH 上游有明确实现；Percho 当前 `drafts.ts` 是内存 store，不能用这个文件证明它也支持重启恢复。因此“所有参考产品都持久化聊天草稿”不成立。

同样，模型目录搜索不是“能搜索集合”这一共性的唯一实现。Codey 当前模型菜单直接列出短目录，且已有用户复检记录明确移除了 Search models，不应借本次分析恢复它。

## 4. 实际值得补的缺项

### 4.1 优先：让失败状态结束，并提供与原因相符的下一步

这是五者操作反馈共性在 Codey 中尚未贯彻完整的地方，收益高于新增界面结构。

**Settings 首次读取失败。** `settings.js` 打开对话框时设置 `Loading connection…` 并禁用表单；GET 失败后只写 error，没有替换 summary，也没有就地重试入口。

隔离 Chromium 让 `/api/local_provider` 返回 503，得到：

```text
summary: Loading connection…
error: Could not load connection. Close and try again.
Address disabled: true
Save disabled: true
Close enabled: true
```

用户并非完全无法恢复，但必须关闭重开，同时页面上“仍在加载”和“已失败”相互冲突。

建议保持同一个 Settings 对话框，在读取失败时把 summary 改成事实性的 `Could not load connection`，提供安静的 `Retry` 文本动作。重试只重新读取连接，不应发送任务或凭空使用未加载成功的默认配置。保存失败保留表单，这一行为当前已经有，继续保持。

**任务受理失败原因被抹平。** `composer.js` 在 `/api/run` 的所有非成功响应上调用同一个 `addSendError`；后者在 index.html 中固定写 `Could not send the message`。但是后端已经区分 `busy`、`browser worker busy`、本地选择无效、项目错误等，返回不同 status/error/hint。

建议对已知、稳定的错误类别提供固定英文短句和对应动作，例如任务占用时解释现有运行并提供 `Open`，本地选择失效时引导重新选择模型，临时忙碌时提供 `Retry`。继续保留原草稿与原提交对象。未知错误使用通用文案即可；不要通过猜测 raw error 文本来决定动作，更不要直接把内部 exception/路径详情倒进主界面。

这里是“已有错误信息 → 正确的用户下一步”的投影补齐，不需要队列、自动重发或新权限系统。

**连接缺失时的提示。** 本地连接未配置时，选中 Local 已会打开 Settings，不是完全没有引导。网页连接主要靠灰/绿状态点和运行后的错误，缺少一处简短的准备提示。

可在明确检测不到所选网页连接时显示一行 `Open the selected model website, then try again`；本地连接则复用 Settings。只在条件成立时出现，恢复后消失。不能把 tab 检测成功写成“已登录”“模型就绪”，也不应仅凭检测不到就禁止一切重试。

适配 Codey：保留灰阶，错误只染文字；动作就在 Settings 或原错误行，不新增彩色通知卡。无需通用 toast 系统才能完成上述修正。

验收：加载失败不再同时显示 Loading；一次 Retry 不重复打开对话框；已知拒绝说明原因；新草稿不被重试覆盖；动作指向产生错误的会话/连接。

### 4.2 优先：让 Search 完整兑现“聊天标题与项目名称”

DESIGN §5.3 明确说 Search 过滤聊天标题和项目名称。当前 `matchesProject(p)` 却只检查该项目中是否存在匹配的聊天。

隔离 Chromium 调用当前实际函数，项目名为 `alpha`、搜索词为 `alpha`、没有任何聊天时，返回 `false`。这个遗漏不是搜索功能整体缺失，而是已承诺的对象范围没有完整实现。

建议项目本身的名称匹配也能显示项目，即使聊天数为零；保留匹配聊天带出所属项目的行为。真正没有结果时只显示搜索空态，不同时堆叠 `No matching chats` 与未过滤场景的 `No chats`。让 accessible name/placeholder 说明实际搜索范围，例如 `Search chats and projects`，默认可见入口仍保持 `Search`。

适配 Codey：不改原位展开、不加边框、不恢复搜索焦点环、不增加常驻筛选标签。它完全符合现有 DESIGN，不需要新增主入口。

验收：空项目名称、聊天标题、项目名带出的聊天、无匹配、Escape、搜索后恢复原分组展开状态都有一致结果。

### 4.3 优先：保持安静，同时确保关键提示能读到、选择状态能被识别

参考项目共同使用正文/次要文字分层与语义控件。不能由此证明它们都达到某个完整无障碍标准；但 Codey 也不应把“低视觉噪声”理解为关键内容必须很暗。

当前 tokens 计算所得名义对比度如下：

| 文字与背景 | 对比度 |
|---|---:|
| `--muted` 在 `--bg` 上 | 3.33:1 |
| `--muted` 在 `--bg-2` 上 | 3.20:1 |
| `--muted` 在 `--panel` 上 | 3.06:1 |
| `--faint` 在 `--bg` 上 | 2.00:1 |
| `--text-dim` 在 `--bg` 上 | 6.79:1 |

`--muted` 目前不仅用于装饰，也用于 11.5px 的 Settings 操作说明与 composer 上的上下文操作；`--faint` 还用于 12px 的空态信息。这些正常小字未达到常用的普通文字 4.5:1 目标。禁用内容和纯装饰有不同判断，不应该因此整体提亮所有分隔线与禁用提示。

建议让重要的下一步、连接配置说明和需要点击的文字使用现有 `--text-dim`，仍用字号、间距和位置表达次级层级；装饰与真正禁用项继续保持暗灰。先调整语义角色与用途，再决定是否需要改全局 token，避免把整界面同时提亮。

Codey 现有菜单键盘、Settings 原生模态、隐藏区域 inert、灰色 focus-visible 已经成立。剩余补齐包括：项目展开按钮有名称与 `aria-expanded`；当前聊天有明确的选中语义；模型菜单 accessible name 包含可确认的连接状态，而不只依赖被 `aria-hidden` 隐藏的状态点。这里补的是状态语义，不是重写菜单或添加快捷键面板。

Search 无可见焦点环是当前 DESIGN 和用户复检的明确选择，本建议不反转该决定。

适配 Codey：全部使用原灰阶，不引入蓝色 focus 或绿色 active；背景、去气泡和正文宽度不变。对比度只是 token 计算，不是已完成的全产品无障碍认证。

验收：关键小字在真实窗口缩放下能辨读；键盘与辅助技术能知道对象、选中和展开状态；隐藏区域不进入 Tab 顺序；焦点返回仍指向原触发控件。

### 4.4 后续候选：Settings 编辑连续性

DSH 的配置表单有提交与冲突处理，opencodex 有 dirty/baseline 和迟到响应保护，Percho 的自定义连接表单失败后保留。它们共同重视编辑连续性，但不能推导出五者都采用“关闭设置也保留所有未保存字段”。

Codey 已做到保存失败保留、请求期间防重复、旧对话框请求不覆盖新对话框。`close()` 允许关闭，下一次 `open()` 再从服务端填写字段，因此未保存编辑会消失。

这是候选增强，不应直接定性为 bug：用户主动关闭设置可能就是放弃编辑。若经常误按 Escape/点背景，应优先考虑在本次应用运行期间保留非敏感字段草稿，提供明确的 `Discard`；或只在有改动时提示。不要每次关闭都确认，也不要持久化 API key 草稿。当前只有一个小连接表单，不值得照搬 opencodex 的复杂保存栏。

### 4.5 真实缺项，但必须单独决定：主题与界面语言

**浅色主题。** 五者都有主题机制，Codey 没有。浅色灰阶本身可以符合“本地开发工具、无装饰”的气质；它与品牌装饰不是一回事。但当前 DESIGN 明确 dark-only，支持第二套主题需要检查图、diff、菜单、对话框和异常态，维护成本远大于放一个开关。目前先改善暗色关键文字，比新主题更能直接解决已确认问题。只有持续出现明亮环境阅读或明确可读性需求时再考虑。

**界面本地化。** 五者均有本地化，Codey 固定英文。对于主要使用中文的初学者，`Restore`、`Compatibility`、`Context limit` 等术语可能有实际门槛。这里有价值，不应因为当前英文规则就断言“不符合气质”：气质主要来自层级、克制和用词，中文也能保持这些特点。

但是现行 DESIGN §1/§7 明确英文 UI。如果确认目标用户需要中文，可正式修订这一规则，在现有 Settings 内放一个语言选择，短标签、灰阶和结构保持一致，只翻译固定应用文案。用户输入、模型回答、Thinking、模型 ID、代码、路径与命令保留原文。不要混用半中文半英文的控件，不添加顶栏语言图标。缺少目标用户证据时先列为产品候选，不塞进本轮基础修复。

## 5. 不因“别人有”就补

| 候选 | 是否五者交集 | 对 Codey 的判断 |
|---|---|---|
| 三栏常驻工作台、可拖动多面板 | 否，opencodex 主结构是导航 + 管理正文，Percho diff 也可覆盖展示 | 已有按需抽屉，先保持正文空间 |
| `@` 文件引用、`/` 命令、附件簇 | 聊天类共性，不是五者主 GUI 交集 | 需要真实引用/执行语义，不能只加外观按钮 |
| Queue/Steer、运行中发送 | 聊天类能力，非五者交集 | 需要后端生命周期支持；先解释单任务占用 |
| Turn rail、多会话 tab、固定会话 | 部分项目采用 | 会增加导航状态；现有 Search 足以先应对增长 |
| 常驻 token/quota/速度统计 | 形式和用户目的不同，不能视作相同必需交互 | Codey 已有按需 Details，不再常驻复制 |
| 彩色状态卡、彩色 Send、玻璃、背景图、宠物/字标动画 | 不是统一外观交集 | 不吸收；违反或偏离现行视觉意图 |
| Always allow、权限模式选择 | 非五者一致策略 | 不能因灰色外观就认为符合 Codey 的审批边界 |
| 持久化聊天草稿、自动保存一切编辑 | 不是已证实的五者交集 | 重启丢稿可单独验证用户需求，但不伪装成共性结论 |
| 插件市场、agent 编排、管理 dashboard | 否 | 与产品原则的少概念、核心闭环优先不符 |

## 6. 对 DESIGN.md 的建议：补交互条款，少扩视觉条款

当前 design 已包含大部分值得吸收的原则。建议未来实施时同步补这几处，而不是重新定义一套设计语言：

1. **§5.3 Search**：明确空项目也能按名称匹配，搜索空态与真实空集合区别；当前承诺本身保留。
2. **§5.11 Settings**：补完整 `loading / loaded / load failed / saving / save failed` 状态，首次失败有就地 Retry，不残留 Loading；保存失败保留编辑。
3. **§5.5/§7 失败文案**：已知受理拒绝显示经过归类的短句和有效动作；通用 Retry 不代表所有错误都应重发。
4. **§2/§3 文字用途**：重要说明与可操作文字不能因“次要”而不可读；灰阶层级通过角色、大小、位置共同实现。
5. **§10 Accessibility**：补对象选中、展开和连接状态的可访问语义；保留 Search 当前样式和既有焦点返回规则。

若未来采用主题或语言，再分别修订 dark-only 和 English UI。不要用“吸收交集”作为绕过明确设计决定的理由，也不要把这些决定当成永不可修改。

优先级建议：

| 顺序 | 工作 | 收益 | 范围 |
|---|---|---|---|
| 1 | Settings 加载失败的真实终态与就地 Retry；已知任务受理失败的原因/动作 | 用户能理解失败并修正，不反复盲试 | 现有状态和入口补齐 |
| 2 | 空项目名称搜索与搜索空态 | 找得到已有对象，兑现 design 承诺 | 小型语义修正 |
| 3 | 关键说明的灰阶可读性、行控件状态语义 | 保持克制，同时能读、能定位 | 局部用途与属性调整 |
| 4 | 根据实际使用验证 Settings 草稿与中文 UI 需求 | 降低误关闭损失与术语门槛 | 后续产品决定 |

没有足够依据把浅色主题、重启草稿、复杂输入或多栏工作台放在上述基础补齐之前。

## 7. 复核证据

### Codey 的设计与当前实现

- [DESIGN.md](../DESIGN.md)：§1 气质、§2 灰阶、§5.3 搜索、§5.5 阅读/过程、§5.6 草稿与模型、§5.11 设置、§10 无障碍、dark-only。
- [产品原则](../PRODUCT_PRINCIPLES.zh-CN.md)：低门槛、可见可控、恢复、安静 UI、禁止为了概念扩张主界面。
- [conversation_ui.js](../codey/web/assets/conversation_ui.js)：matchesSession/matchesProject、scroll/cache、Search 与审批入口。
- [settings.js](../codey/web/assets/settings.js)：open/close/save、generation、读取错误、保存失败保留。
- [composer.js](../codey/web/assets/composer.js)：drafts Map、revision、sendingSessionId、非成功响应统一处理。
- [provider_ui.js](../codey/web/assets/provider_ui.js)：模型/effort 菜单、连接状态点、accessible name、本地模型元数据。
- [index.html](../codey/web/index.html)、[messages.js](../codey/web/assets/messages.js)、[changes_drawer.js](../codey/web/assets/changes_drawer.js)：导航行、固定发送错误、Retry、scope 与加载/空态。
- [tokens.css](../codey/web/assets/tokens.css)、[app.css](../codey/web/assets/app.css)：对比度源值与文字实际用途。
- [api.py](../codey/app/api.py)：任务受理的 400/409/503 错误与 hint。
- [10 月 6 日实施记录](ui-ux-implementation-2026-10-06.zh-CN.md)：已实施的阅读/草稿/输入/搜索/审批/抽屉，以及用户要求的菜单与 Search 样式调整。

### DSH 家族

- 上游：[样式参考](../reference-projects/deepseek-harness/docs/web-styling.md)、[AppFrame](../reference-projects/deepseek-harness/packages/client/ui-layout/src/client/AppFrame.tsx)、[Theme](../reference-projects/deepseek-harness/packages/client/ui-theme/README.md)。
- 上游：[Settings](../reference-projects/deepseek-harness/packages/client/ui-settings/README.md)、[Settings shell](../reference-projects/deepseek-harness/packages/client/ui-settings-general/README.md)、[ProviderEditor](../reference-projects/deepseek-harness/packages/client/ui-settings-models/src/client/ProviderEditor.tsx)、[ModelSelect](../reference-projects/deepseek-harness/packages/client/ui-model-selection/src/client/ModelSelect.tsx)。
- 上游：[Conversation](../reference-projects/deepseek-harness/packages/client/ui-conversation/README.md)、[Chat](../reference-projects/deepseek-harness/packages/client/ui-chat/README.md)、[WorkspaceBrowser](../reference-projects/deepseek-harness/packages/client/ui-workspace/src/client/rows/WorkspaceBrowser.tsx)。
- anywhere-labs：[upstream.json](../reference-projects/dsh-desktop-anywhere-labs/upstream.json)、[桌面包依赖](../reference-projects/dsh-desktop-anywhere-labs/dsh-plugin-desktop/package.json)、[AdvancedFrame](../reference-projects/dsh-desktop-anywhere-labs/dsh-plugin-desktop/src/client/AdvancedFrame.tsx)、[ThemePresenter](../reference-projects/dsh-desktop-anywhere-labs/dsh-plugin-desktop/src/client/theme-presenter.ts)。
- anywhere-labs：[DesktopSettingsSection](../reference-projects/dsh-desktop-anywhere-labs/dsh-plugin-desktop/src/client/DesktopSettingsSection.tsx)、[onboarding](../reference-projects/dsh-desktop-anywhere-labs/dsh-plugin-desktop/src/client/onboarding.tsx)。桌面设置中可复核 loading/failed/saved、role=status 与 Retry。
- fsw：[AppFrame 样式](../reference-projects/dsh-desktop-fsw2781890522/packages/client/ui-layout/src/client/AppFrame.module.css)、[ProviderEditor](../reference-projects/dsh-desktop-fsw2781890522/packages/client/ui-settings-models/src/client/ProviderEditor.tsx)、[DeepSeekOnboardingDialog](../reference-projects/dsh-desktop-fsw2781890522/packages/client/ui-settings-models/src/client/DeepSeekOnboardingDialog.tsx)、[WorkspaceBrowser](../reference-projects/dsh-desktop-fsw2781890522/packages/client/ui-workspace/src/client/rows/WorkspaceBrowser.tsx)。

### opencodex

- [Foundations](../reference-projects/opencodex/gui/design-system/foundations.md)、[Components](../reference-projects/opencodex/gui/design-system/components.md)：主题、字体、控件、导航、窄窗口与语义状态。
- [nav-groups.ts](../reference-projects/opencodex/gui/src/nav-groups.ts)、[App.tsx](../reference-projects/opencodex/gui/src/App.tsx)、[ui.tsx](../reference-projects/opencodex/gui/src/ui.tsx)：管理导航、隐藏/焦点与 Select。
- [ClaudeCode.tsx](../reference-projects/opencodex/gui/src/pages/ClaudeCode.tsx)、[保存栏测试](../reference-projects/opencodex/gui/tests/claudecode-save-bar.test.tsx)：dirty/baseline、加载失败 Retry、保存中的后续编辑与旧读取保护。
- [Models.tsx](../reference-projects/opencodex/gui/src/pages/Models.tsx)、[语言设置](../reference-projects/opencodex/gui/src/i18n/shared.ts)：搜索与无匹配、界面语言。

### Percho

- [globals.css](../reference-projects/percho/packages/desktop/src/renderer/src/styles/globals.css)、[Sidebar](../reference-projects/percho/packages/desktop/src/renderer/src/components/sidebar/Sidebar.tsx)：双主题 tokens、字体、导航/筛选/空态/inert。
- [drafts.ts](../reference-projects/percho/packages/desktop/src/renderer/src/stores/drafts.ts)、[Composer](../reference-projects/percho/packages/desktop/src/renderer/src/components/composer/Composer.tsx)、[ModelPicker](../reference-projects/percho/packages/desktop/src/renderer/src/components/composer/ModelPicker.tsx)：会话输入与模型选择。
- [MetaGroup](../reference-projects/percho/packages/desktop/src/renderer/src/components/chat/MetaGroup.tsx)、[ErrorNote](../reference-projects/percho/packages/desktop/src/renderer/src/components/chat/ErrorNote.tsx)：过程披露与错误分类动作。
- [CustomProviderForm](../reference-projects/percho/packages/desktop/src/renderer/src/components/settings/providers/CustomProviderForm.tsx)、[GeneralPanel](../reference-projects/percho/packages/desktop/src/renderer/src/components/settings/GeneralPanel.tsx)、[AppearancePanel](../reference-projects/percho/packages/desktop/src/renderer/src/components/settings/AppearancePanel.tsx)：保存失败保留、语言与主题。

### 本次检查的边界

两个隔离浏览器场景使用当前 Codey HTML/JS、模拟 API 和临时页面，没有连接真实模型，没有执行命令，没有修改用户会话。项目搜索检查复用当前函数，仅注入空会话数据；Settings 检查加载当前模块并模拟首次 GET 失败。没有把这些有限检查写成全部界面通过验收，也没有把现有测试文件的内容当作本次测试执行结果。

本次未修改 DESIGN.md、产品源码或五个参考项目；新增的是此研究文档。
