# Codey

**把网页版 AI 变成本地优先的编程、研究和可控记忆工作台。**

[![版本](https://img.shields.io/badge/version-0.3.11-blue)](CHANGELOG.zh-CN.md)
[![许可证：GPL v2](https://img.shields.io/badge/license-GPL--2.0--only-blue)](LICENSE)
[![本地优先](https://img.shields.io/badge/local--first-AI%20workspace-2ea44f)](#安全模型)

[English](README.md)

Codey 可以连接你已经在用的网页版 AI，比如 DeepSeek、MiMo、StepFun、Qwen 和
GLM，也可以连接本地 OpenAI-compatible 模型，然后给它们受控的本地工作闭环：
聊天、带证据的 Research、读文件、改文件、跑测试、看 diff、审查改动、必要时
恢复，以及可检查、可导出、可删除、可禁用的本地有界记忆 / continuity / affinity。

它是一个本地优先、低成本、多网页模型兼容的 AI 编程、研究和可控记忆工作台，
适合不想为每个项目接入付费模型 API 的用户。

网页版 provider 不需要 API key，不需要充值 API 额度。你只要能在 Edge 或 Chrome 里登录网页 AI，就可以用 Codey 开始写代码。如果你运行 LM Studio、Ollama、llama.cpp 或其他 OpenAI-compatible 本地 endpoint，可以选择 **Local**，填写一次 base URL 和模型名。

版本：`0.3.11`

[版本更新记录](CHANGELOG.zh-CN.md)

[未来版本规划](ROADMAP.zh-CN.md)

---

## 一眼看懂

- **使用你已经登录的网页 AI**：支持 DeepSeek、MiMo、StepFun、Qwen 和 GLM。
- **自动选对入口**：在自动模式下，Codey 会先判断该走普通聊天、只读规划、
  Research、Writer、Hybrid 还是 Review；手动选择和权限边界仍然优先。
- **记忆可控**：显式偏好和很短的 continuity context 会保存在本地有界文件里，
  可以预览、导出、删除、重置、禁用，并在任务结束后安静维护。
- **按需审计本地上下文**：从 topbar `...` 菜单打开 `Local context`，可以检查、
  导出、删除、重置或禁用有界本地状态；它不新增常驻 sidebar，也不打断任务流。
- **自然继续待办**：Codey 有本地排队的后续任务时，你说“继续”就能认领一条，
  走对应的 Research、Writer 或 Review，并用本地 proof 收尾。
- **先研究再动手**：点击 `Research`，Codey 可以搜索网页、打开 HTML/PDF 来源、保存证据笔记、可视化局部 note/source 关系图，并生成带引用、反证/限制、来源质量和搜索覆盖的 synthesis。
- **代码留在本机**：模型只能访问你选择的项目目录。
- **写代码时不容易忘事**：每次本地工具结果后，Codey 会提醒模型已经读过哪些
  文件、改了哪些文件，以及当前最该跑哪条验证命令。
- **把研究带进项目**：研究结束后选择项目文件夹，Codey 只把带引用和限制条件的有边界 Research Brief 注入 Writer，不把整个 vault 塞进去。
- **受控工具循环**：读取、编辑、测试、diff、Review 和 Restore 都有边界。
- **可选本地模型**：`Local` 可以连接 OpenAI-compatible endpoint，并支持可选 API key。
- **改完后再审查**：一个模型写代码，另一个模型审查最终 diff；如果没有第二个可用模型，也可以让写代码的模型做一次明确标注的 self-review。
- **对新手友好**：有 Git 会增强体验，但没有 Git 也能开始。

---

## 为什么做这个？

AI 编程不应该只属于买得起高价 API 或昂贵订阅的人。

Codey 想解决的是一个很朴素的问题：

- 用网页 AI，而不是强制 API key
- 代码留在你自己的电脑上
- 每次改了什么都能看到
- 不满意可以恢复
- 不懂 Git 的新手也能先开始

它不是魔法，也不是要替代专业 IDE。它更像一座小桥：把一个想法，带到一个能运行、能修改、能测试的本地项目里。

---

## 理念

我希望 Codey 能做一点“平权”的事。

如果 AI 编程只对能负担昂贵 API 费用的人好用，那很多新手会被挡在门外。Codey 选择了一条更简单的路：把大家已经能访问的网页 AI，谨慎地接到本地文件、测试、diff 和 restore 上，让更多人能更早开始编程和创造。

它不需要夸张的口号。只要一个普通人能打开它、说出想法、看到改动、运行程序、失败后还能恢复，这就已经很有意义。

---

## 它能做什么？

- 用 New Chat 正常聊天，不向模型开放任何项目
- 在输入框上下文里点 `Research`，让 Codey 搜索、读取 HTML 和文本型 PDF、写笔记，并生成带编号引用、evidence snippet、反证/限制、来源质量和搜索覆盖的研究结论
- 可以先普通聊天讨论方案，再从输入框上方的项目上下文选择文件夹，把同一个聊天接到项目任务
- 研究结束后选择项目，把 synthesis 压成有边界的 Research Brief 交给 Writer 落地
- 通过 Research drawer 的 `Evidence`、`Sources`、`Graph`、`Notes` 四个 tab 查看本轮证据、来源、统一图和落盘笔记，而不是只看一条 receipt；PDF 页码定位和搜索覆盖都放在现有证据/来源视图里
- 项目实现和验证成功后，可以把“做了什么、为什么、跑过什么检查”沉淀成实现/验证记忆，而不是把源码全文塞进 vault
- 在同一个项目对话里讨论、查看和修改；只有明确要求时才改文件
- 让模型读取和修改你选择的项目目录
- 运行允许的测试、构建、lint 和类型检查，并把结果继续反馈给模型
- 显示红绿 diff
- 每次任务结束后显示一条克制的任务收据，例如 `DONE · 2 files changed · checks passed · restore available`
- 即使没有 Git，也能用 snapshot diff 和 restore 恢复改动
- 有 Git 时自动增强为 Git diff / commit 工作流
- 一个模型失败时，可以换另一个模型再试
- 已打开的其他模型可以作为隐藏顾问，参与普通聊天、空项目规划和项目只读审查
- 两个网页模型可以一起协作：一个写代码，另一个帮忙检查；没有第二个可用模型时，会用写代码的模型在临时 self-review 标签页里再检查一次，而不是完全跳过 Review
- 用隐藏任务 brief 让 Writer 和 Reviewer 共享同一份有边界的意图
- 在模型真正读文件前，给 Writer、隐藏顾问和 Reviewer 一份有边界的本地项目地图
- 让模型在改某个符号前，先请求有边界的文本引用提示
- Python replacement edit 新引入语法错误时立即提示，但不自动回滚，也不冒充检查通过
- 长对话接近上限时，自动总结事实并在新对话里无感继续
- 记住真实运行成功的项目命令，后续任务不必重新猜测
- 只从通过本地检查的成功改动里沉淀最近变更事实
- 重启 Codey 或切换模型后，同一聊天可以通过精简事实 handoff 和最近可见对话自然继续
- 连续 Research 或项目内 Hybrid Research 会带上同一聊天的有边界前文，所以“继续查刚才那个方案”不会丢上下文
- 非 Git 项目的 diff 和 restore 在 Codey 重启后仍然可用
- 显式学习信号会先进入本地 Ghost 候选箱；只有 accepted typed 偏好才会变成很短的
  中性 `Local Context`，用于普通 Chat 和只读 planning
- 普通 Chat 回合结束后可以 best-effort 学习明确的风格偏好；extractor 使用 fresh
  provider tab，不污染当前聊天，`ghost disable` 会停止后续学习
- Continuity 只从 accepted memory、短任务焦点、run ledger 和 Research 标题 /
  结构化 `open_questions`
  里取有界事实，不保存完整聊天、源码、Research body 或网页正文
- 可以通过 topbar `... -> Local context` 或 `python -m codey ghost ...`
  预览、导出、删除、重置或禁用本地状态
- 成功任务结束后，Codey 会无感做本地 Ghost 维护：健康检查、到期衰减、
  continuity refresh 和 event compaction；不调用网页模型、不跑 shell、不改 UI，
  也不暴露额外 sleep 控制入口
- Codey 会维护一个有界本地待办队列；只有“继续 / 下一个 / 处理待办”这类严格
  continuation 才会认领一条，并且完成时必须写入本地 proof
- Research 里的结构化 `open_questions` 和有支持的概念缺口可以变成研究待办，但不会后台自动联网
  搜索，也不会把猜测当事实
- 维护一个有界本地 Affinity Index，把 accepted 偏好、任务类型、项目、研究概念和
  provider outcome kind 连成低风险排序账本；它不是证据、权限或自动执行系统
- 网页输入框或发送按钮改版时，先做有边界的本地发现，仍不确定则让健康兄弟模型
  从脱敏候选中选择；真实发送成功后才能保存、晋级或回滚恢复包
- 控件恢复仍不足时，只根据脱敏布尔事实恢复一条有边界的网页状态规则；不同网页的
  completion 信号仍隔离在各自 adapter 里
- Writer 遇到明确网页故障时，从本地 checkpoint 把未完成任务交给健康兄弟模型，
  同一任务最多切换两次，不会向提交状态不确定的旧模型重发
- 用小型健康熔断区分控件故障、临时错误、限流、登录和验证码状态
- 当控件恢复和 Flow 恢复都不够时，可以在后台 sandbox 里修坏掉的 Provider
  adapter 代码；候选必须通过安全策略、静态检查、Provider 单测和中性 canary，
  之后才会通过子进程 worker 启用
- 及时停止正在等待的网页模型、Review、恢复流程或测试命令
- 对完整测试/构建套件给更长超时，同时保持快速命令有界
- 长命令输出同时保留开头与结尾，不丢失末尾错误摘要
- 大文件只读到一页时，直接告诉模型下一页应该调用的 `read_file` JSON 参数
- grep 命中过多被截断时，提示模型缩小查询或指定子目录
- 把 Python 和 Node 报错里的明显依赖库调用栈折叠掉，让用户代码错误更容易留在上下文里
- 助手回复支持克制的 Markdown 基础渲染，代码块带复制按钮，不加语法高亮或新颜色
- UI 重连后自动恢复运行状态、审批或人工教学
- 网页提交结果不确定时绝不重复发送
- 底层记录很小的失败诊断信息，方便网页改版后定位问题
- 默认启动 Edge，必要时回退 Chrome；原生 WebView 打不开时仍保留本地 HTTP UI

---

## 支持的模型

| 模型 | 状态 |
|---|---|
| DeepSeek Web | 已实机测试 |
| 小米 MiMo | 已实机测试 |
| StepFun Chat | 已实机测试 |
| Qwen Studio | 已实机测试 |
| GLM | 已实机测试 |
| Local OpenAI-compatible | 可选；在 Codey 中配置 endpoint 和模型名 |

Codey 使用浏览器自动化，所以网页 AI 改版后可能会失效。当前架构把不同网站的适配代码隔离开，网页变了就修对应 adapter，不需要改 agent 核心。

如果网页改版，Codey 会先尝试上面的有边界恢复。仍然无法安全识别时，它才会安静地暂停，请你在网页里点一次那个控件。Codey 只保存最新一条经过验证的控件记录，不保存网页 DOM 或完整聊天，不会打扰主流程。

新控件只有在原消息只提交一次、并且成功读到新回答后，才会作为一组恢复包原子保存。
第一次成功只是 provisional，下一次自然发送成功后才晋级 active；明确的连续控件失败
会恢复上一版。健康 Provider 的正常发送不会调用兄弟模型。

同一恢复包现在还可以保存一条有边界的 Flow Recipe。它只能组合固定布尔事实，例如
回答稳定和经过验证的 stop 或 typing 状态转换，不能包含 selector、JavaScript、URL、
任意点击、网页正文或项目数据。Provider 专属 completion 逻辑仍留在各自 adapter 中；
Codey 只有在当前布尔事实能证明规则成立时，才会晋级学到的 Flow Recipe。

如果网页变化大到 adapter 代码本身也坏了，Codey 现在可以把这个 Provider 放入后台
自修复队列。自修复运行在独立 Python 进程中，只允许健康 helper 模型修改坏掉的
Provider adapter 文件，并在临时 sandbox 里通过策略检查、静态检查、对应 Provider
单测和中性 marker canary。候选 adapter 不会直接加载进主进程，而是通过子进程
Provider worker 运行；worker 使用同一个已登录 Codey 浏览器 profile 的后台新标签页，
不会复制 cookie，也不会阻塞你当前的编程任务。候选先是 provisional，自然成功后才
晋级 active，连续结构性失败会自动回滚。`agent.py`、`task_runner.py`、`tool_runtime.py`、
`server.py` 以及恢复/安全控制面不在 v1 自修改范围内。

---

## Research

`Research` 是 Codey 的研究工作闭环。它不是第二个 app，也不会偷偷自动联网。
当你需要 Codey 查资料、读来源、写证据笔记时，明确点击输入框上下文里的 `Research`。

主界面仍然是一条很轻的上下文：

```text
Choose folder · Research
```

- `Choose folder` 把当前聊天接到项目目录。
- `Research` 让当前消息进入研究闭环。
- 输入框下方的 provider picker 选择当前 provider；选 `Local` 时会打开本地 endpoint 配置弹窗。

Research 可以使用网页 provider，也可以使用 `Local`。搜索、打开网页、URL policy、
笔记写入、restore 和 evidence review 都由 Codey 本地工具执行。模型没有隐藏联网权。
最终 synthesis 只能引用本轮 Codey 实际打开过的来源。Research provider 也会被要求
每轮只选择一个本地 JSON 工具；如果模型一次吐出多个 action，Codey 会把它当作协议错误，
要求模型重答，而不是直接执行一串工具。

从 0.2.20 开始，生产 Research 使用一个很薄的 controller，而不是每轮都把完整工具菜单
交给模型。Codey 会读取当前 Research ledger，只展示这一轮合理的 allowed tools，并给
搜索结果、已打开来源和 source_search 命中分配稳定的 run-global ID：
`result_id`、`source_id`、`hit_id`。模型可以选择 ID，不需要手抄 URL、PDF 页码或
HTML offset。这不是硬线性状态机：本地记忆搜索和 web_search 仍然可用，模型可以回头
查反证或补更好的来源。已有 typed tool contract 和 report quality gate 仍然负责决定
什么能执行、什么能保存。

从 0.2.19 开始，网页搜索和打开页面会运行在独立的 Research 浏览器 profile 和 CDP
端口里，不再和 DeepSeek、MiMo、StepFun、Qwen、GLM 的聊天页共用同一个浏览器上下文。
这样 Bing 搜索页、结果页和文章页不会再抢网页模型的标签页或前台，修复了网页模型
Research 中“模型其实快回了，但 Codey 一直等，Stop 后才看到 JSON”的卡住问题。
Codey 也会对短暂的 `Page.content` 导航抖动做重试；如果模型尝试 `done` 但被质量门或
私有 evidence review 退回，UI 会显示 `Turn N (done)`，不再像空白 turn。

从 0.2.18 开始，Research JSON tool call 会先经过本地 typed contract 检查再执行。
Codey 现在能区分没有 JSON、未知工具、一次多个工具、参数不合法、直接写报告、
疑似使用聊天网站自带搜索这些错误，并给模型一个更具体、可照抄的修复格式。
最终报告必须通过 `done` 返回；Codey 会在质量门通过后自己保存 synthesis。

Provider 的适用场景很重要。Codey 暂时不会按角色自动切换模型；你可以按当前任务手动选择：

| Provider | 更适合 | 当前注意点 |
|---|---|---|
| DeepSeek / Qwen / GLM | 通用写代码、Review 和 Research | 网页每日额度可能打断长任务 |
| MiMo | 强模型额度不够时做代码编辑/实现；加上 one-tool 边界后可跑小型 Research | 严格 JSON-tool Research 的波动仍更高；Codey 会等 MiMo 回答尾部按钮稳定后再发下一轮，但长研究仍优先 DeepSeek、Qwen、StepFun 或 Local |
| StepFun | 带证据的 Research 和本地 JSON-tool probe | adapter 现在会等 StepFun 回答尾部按钮稳定后再进入下一轮；暂不建议作为从零新建项目的主 Writer |
| Local | 私有/offline 任务和额度兜底 | 质量取决于你的本地模型；Gemma4-12B 通过了 fixture probe，但更重的 prompt 仍可能压低 JSON 遵守 |

MiniMax 也做过 probe，但没有被选中，因为它的 Agent 页面首轮就脱离本地 JSON-tool 协议，
改用自己的网页/agent 行为。

Manual Deep Research A/B harness 已经测过 DeepSeek、StepFun、Qwen 和本地
Gemma4-12B endpoint。当前一致结论是：在已经打开的来源内部做确定性的
`source_search`，已经值得进入生产 Research。更重的 `deep_core`
plan/coverage prompt 仍只保留在 A/B，不默认进入生产链路。

MiMo 在加入 one-tool Research 边界后重新做过实机补测。fresh-tab 的
`long-official-doc/source_search` 跑满 10 轮并完成：使用了 `source_search`，
打开目标 offset，保存精确 evidence，并通过 report quality。没有这个边界的早期 MiMo
probe 仍会一次输出多个搜索调用，所以这里记录为 Research 纪律增强，而不是自动角色路由。

0.2.18 又补测了一次 MiMo：加上 typed tool-contract repair 和 MiMo 本地的回答尾部
稳定等待后，连续两轮长消息 submit probe 没有 timeout；同一个
`long-official-doc/source_search` fixture 在 9 轮内 `done=True`。Qwen 在同一类
source_search fixture 中 JSON 格式很干净，但 10 轮内仍把时间花在中间 note 写入上，
没有走到 `done`。

Manual A/B harness 里还有一个 `thin_gate` probe arm，它为 0.2.20 生产
controller 提供了依据。它追加“当前允许工具”和稳定的 `result_id` / `source_id`
选择。MiMo 实机
`long-official-doc/thin_gate` probe 用 8 轮完成，`done=True`、`quality_score=11`、
0 次 protocol repair，并发生 4 次 ID rewrite。这支持的方向是 allowed-tools 和
stable IDs，而不是很硬的线性 controller 或 Deep Research Core。

生产 Research 现在可以在 `open_url` 后调用 `source_search`。它只搜索 Codey
已经打开过的来源，并返回定位预览，不是 evidence。HTML 命中会提示模型先打开对应
offset 再引用；PDF 页码证据仍走硬性质量门：必须先 `open_url pages="N"`，
才能引用 `[n p.N]` 或保存该页 evidence。

从 0.2.4 开始，Research 会维护 Evidence Ledger，并在保存最终 synthesis 前通过确定性的
报告质量门。报告必须包含：

- `结论`
- `关键证据`
- `反证与限制`
- `来源质量`
- `搜索覆盖`
- `来源`

正文里的 `[1]` 这类编号引用必须对应本轮 Codey 实际打开过的 final URL。
每个被引用来源也必须至少有一条已保存的 evidence snippet，而且 snippet 必须真实出现在
打开过的网页正文里；search result 在 `open_url` 之前不算证据。

PDF 是同一个 `open_url` source intake 的能力，不是新工具、新模式或新按钮。
当 URL 指向可提取文本的 PDF 时，Codey 默认只读取有边界的前几页，记录页数、
已读页、是否截断等元信息，并允许报告用 `[1 p.4]` 这类页码引用。只有 Codey
实际读过第 4 页，并且保存了来自该页的 snippet，这个页码引用才会通过质量门。
扫描版、超大或提取失败的 PDF 会成为中性的 `SKIPPED`，不会污染 opened sources。

质量门接受常见报告格式，比如 `1. 结论`、`一、结论`，以及 `[1] [Title](https://...)`
这种 Markdown link 来源行；但不会放宽来源 provenance 或 snippet 原文匹配。显式 URL
引用仍必须匹配 Codey 实际打开过的 final URL；来源质量里的裸站点域名更自然：
打开 `docs.python.org` 后可以写 `python.org`，但只打开 `python.org` 不能反过来声称
已经打开 `docs.python.org`。

如果某个结果是 Codey 暂时无法读取的来源，比如扫描版或过大的 PDF，Research 会把
工具结果标成中性的 `SKIPPED`，然后继续读取其他可用来源。若模型给了改写过的
evidence excerpt，Codey 会替换成打开来源中的真实短摘录，并带 warning 保存 note。

典型流程是：

```text
先聊想法
-> 点 Research，提出研究问题
-> Codey 搜索、打开网页、记录 evidence、保存带引用的 synthesis
-> 选择项目文件夹
-> Codey 把有边界的 Research Brief 注入项目 Writer
-> 实现和验证成功后，可以把实现事实沉淀回本地记忆
```

Research drawer 有四个轻量 tab：

- `Evidence`：claim、snippet、PDF 页码定位、counterpoints、质量 warning 和搜索覆盖
- `Sources`：citation map、source title、final URL、来源质量提示，以及 PDF 已读页/截断元信息
- `Graph`：一个有边界的统一图，最上层是虚拟概念，中间是当前 synthesis/report 和相关笔记，depth 3 展开到 source URL；Open Questions 只作为 concept 节点文本，标注 "unproven; not facts"
- `Notes`：synthesis、note id、source URL 和 restore 状态

Coverage 作为支持性的审计信息放在 `Evidence` 里，不做第一层用户概念。`Graph`
是展示层 read model，不是新数据库，也不是全 vault 知识图谱：声明的概念关系保持虚拟，
证据关系仍然是 note/source 关系，tag 边只连接当前可见笔记和概念。

Vault 存在 Codey 本地状态目录里，底层是 Markdown notes 和可重建的 SQLite FTS
索引。项目源码不会被复制进 vault；implementation note 记录的是做了什么、为什么、
关联哪个 synthesis/decision、跑过什么检查，以及当前限制。Project Writer 收到的是
有边界的 brief，包含关键结论、citation map、evidence items、counterpoints 和
source-quality risks，而不是整个 vault。

---

## 隐藏 MoA 顾问

MoA（Mixture of Agents，多模型协作）是 Codey 的隐藏顾问层，不增加按钮、模式或面板。在 New Chat 中，当前选中的主模型先写一份私有草稿，最多两个已经打开的其他模型在后台挑错和补充，最后仍由主模型生成你看到的唯一回答。空项目或只有占位文件的项目也使用这种主模型优先的规划方式；已有项目则让顾问在 Writer 动手前做有边界的只读审查。

顾问不能改文件、运行命令、请求 Shell 审批、访问项目外内容，也不能读取敏感或被排除的路径。顾问报告只是建议，Writer 必须回到真实文件验证。顾问失败时，Codey 会安静退回主模型单独工作。

这层隐藏 MoA 和下面的“第二模型 Diff Review”彼此独立：MoA 在主模型思考或动手前提供建议；Diff Review 在代码真实改完后检查最终改动。

---

## 双模型协助

一个 AI 可以写代码，但它也可能漏掉小错误。两个 AI 的意义不是把界面变复杂，而是让流程更稳：一个模型专心写，另一个模型像第二双眼睛一样帮你检查刚改过的代码。

你不需要学习一个新模式。只要你在 Codey 的浏览器窗口里打开两个支持的网页 AI，Codey 就可以自动把它们配合起来：

- 你在 Codey 里选择的模型，负责写代码。
- 另一个已经打开的支持模型，自动负责检查。
- 写代码的模型会读文件、改文件、跑测试。
- 检查模型不会直接碰你的文件；它会看 diff 和一段短小、有边界的
  Impact Map，了解变化的 symbol、可能受影响的 caller 和相关测试，然后指出具体问题。
- 如果检查通过，任务就结束。
- 如果检查模型发现真实问题，Codey 会把意见发回写代码的模型，让它再修一次。

只有所选模型在本次任务里真的改了文件，第二个模型才会开始检查。项目内的
普通问答和只读分析会直接显示所选模型的完整回答，不会把聊天强行变成 Review。

如果没有可用的不同 reviewer 模型，Codey 仍然可以打开一个临时 fresh tab，用同一个
Writer 模型做 same-model self-review。这不是独立的第二意见，但会让写代码的模型用同一套有边界的
Review prompt、Impact Map、Verification Map 和执行证据，再认真检查最终 diff。若 self-review
也失败，Codey 才会安静地保留原来的单模型结果。

一句话：两个不同模型最好，像第二位老师帮忙检查；只有一个模型时，也可以让同一位老师换张纸再认真看一遍；如果 Review
都不可用，就退回原结果。没有群聊界面，没有额外开关，也不会把主界面变复杂。

---

## 快速开始

### 1. 安装依赖

```powershell
pip install -r requirements.txt
```

### 2. 启动 Codey

```powershell
python -m codey
```

你会看到类似：

```text
[codey] UI ready: http://127.0.0.1:43210/
```

Codey 会打开一个本地控制面板。

### 3. 第一次登录网页 AI

第一次运行任务时，Codey 会打开一个专用浏览器窗口。你需要在里面手动登录所选模型。

这个浏览器 profile 和你日常用的浏览器 profile 是分开的：

```text
C:\Users\<你>\.codey\edge-profile
C:\Users\<你>\.codey\chrome-profile
```

登录一次后，后面通常不用重复登录。

这个模型浏览器窗口可以一直留着。你关闭再重启 Codey UI 时，Codey 会尽量安静地连回已经打开的 CDP 浏览器和模型标签页。

### 4. 选择项目，然后用人话描述任务

例如：

```text
写一个 Python 贪吃蛇小游戏，放在 snake.py，一个文件就能运行。
运行方式是 python snake.py。
```

Codey 会让网页 AI 返回结构化工具调用，然后在本地真实读写文件，并显示改动。

你也可以先在 **New Chat** 里普通聊天、讨论方案，不向模型开放项目；准备动手时，
点击输入框上方的项目上下文（`Choose folder`）选择文件夹，把同一个聊天接到项目。
如果输入框里已经有草稿，点击同一个上下文会保留草稿，并在选完文件夹后发送。
如果只想普通聊天、不让模型接触任何项目，就继续使用 **New Chat**。

如果要先研究再写代码，在同一条上下文里点击 `Research`，然后提出你要的资料、
来源、对比、API 背景或方案调研。Codey 会写本地 research notes，并生成带 evidence、
counterpoints、来源质量和搜索覆盖的 synthesis。之后再选择项目文件夹时，Writer
收到的是有边界的 Research Brief，而不是整个 vault。

如果要使用本地模型，在模型菜单里选择 `Local`。Codey 会弹出 OpenAI-compatible
配置框，填写 base URL、model id 和可选 API key。key 留空会保留已有 key；
填入新 key 后点击 `Connect` 会覆盖旧 key。

任务结束后，Codey 会用一行很轻的收据总结本地事实：

```text
DONE · 2 files changed · checks passed · restore available        View diff
```

点击 `View diff`，右侧栏会打开具体的红绿 diff。

---

## 项目本地配置

项目可以选择放一个 `.codey/config.json`，声明本地事实和偏好，例如验证命令候选、
扫描时忽略的项目根相对路径前缀，以及更小的 Project Map 预算。Codey 不会自动创建这个文件。

配置里的命令只是建议，不是授权。它们仍必须通过可执行文件、cwd 在项目内，以及
`tool_runtime` run allowlist 检查；shell approval 和 safe-path 守门不变。
provider 偏好本版只作为 future hint 解析，不会覆盖你明确选择的 provider。

---

## 安全边界

Codey 不是无限制 shell。

- 文件读写限制在你选择的项目目录里。
- 正常改动会显示 diff。
- 没有 Git 也能用 snapshot restore 恢复。
- Git 是增强功能，不是入门门槛。
- shell 命令需要你确认后才会执行；setup/install 类审批会显示风险说明，
  批准后再把只读的本地环境、manifest 事实和有边界的下一步提示回传给 Writer。
- 失败时 UI 保持简单：`ERROR · Could not send the message  Retry`。

你仍然应该在保留改动前检查 diff。

---

## Git 是增强功能，不是门槛

Codey 按这个思路设计：

| 环境 | 行为 |
|---|---|
| 没装 Git | 可以创建/修改文件、显示红绿 diff、保存本地快照、恢复本轮修改 |
| 装了 Git，但当前目录不是仓库 | 继续使用本地 diff，并提示以后可以初始化 Git |
| 当前目录是 Git 仓库 | 支持 Git diff、commit、branch 和更可靠的历史追踪 |

对新手来说，最重要的是先看到“我改了什么”，然后再慢慢理解“为什么要提交”。

---

## 自举证明

Codey 已经测试过用 DeepSeek、MiMo、StepFun 和 Qwen 修复被故意弄坏的 Codey 临时副本。

每个模型都完成了这个闭环：

1. 运行失败测试；
2. 阅读 Codey 自己的源码；
3. 修改坏掉的代码；
4. 重新运行测试；
5. 测试通过后结束。

见 [BOOTSTRAP_PROOF.md](BOOTSTRAP_PROOF.md)。

当前版本还包含 [TEST_REPORT.md](TEST_REPORT.md)，记录最近一次单模型、双模型、MoA 和自举 smoke 的实机结果。

这不代表 Codey 永远不会坏。它证明的是：当 Codey 出现可测试、可定位的问题时，它已经有机会依靠接入的网页 AI、本地工具、diff、restore 和测试，把自己修回来。

## 端到端测试

真实 Edge UI 流程可以用确定性的测试 Provider 重放，并自动检查项目选择、模型切换、SSE、文件修改、测试、review、任务收据、diff 和 restore：

```powershell
python -B tools/ui_e2e.py --artifacts .e2e-artifacts --json
```

当 Edge CDP 已打开并登录四个模型网页时，可以运行真实 Provider 矩阵。每个结果都会在 Agent 结束后再经过独立功能断言和 unittest 验证：

```powershell
python -B tools/live_smoke.py --provider all --case edit --port 9222 --max-turns 10 --json
```

显式 MoA 贪吃蛇 flow 放在 `tests/` 下，因为它是真实 smoke 测试，不是通用工具。它会把断点和耗时日志写到目标项目自己的 `.codey/smoke/moa-snake-flow` 目录：

```powershell
python -B tests\moa_snake_flow.py --project E:\snake --reset --json
```

---

## 示例任务

生成一个小程序：

```text
Write a complete classic Snake game in pygame as a single file snake.py.
The file must run with: python snake.py
```

修复一个 bug：

```text
There is a file buggy.py with a subtle bug. Read it, fix the bug,
write the corrected version back to buggy.py, then run the test.
```

---

## 命令行用法

不打开控制面板也可以用：

```powershell
# 单次聊天，不写文件
python -m codey chat "用一句话解释 Python 的 GIL"

# 指定 Qwen
python -m codey chat --provider qwen "用一句话解释 Python 的 GIL"

# 直接运行 agent
python -m codey agent --provider qwen --project E:\my-project --max-turns 10 "修复失败的测试"

# 输出 JSONL 事件流，方便脚本、CI 或 benchmark 消费
python -m codey agent --json --provider qwen --project E:\my-project "修复失败的测试"
```

---

## 架构

```text
UI / CLI
   |
Server / Orchestrator
   |
Agent Runtime -- JsonToolCodec
   |
ChatProvider -- DeepSeekWebProvider
             -- MimoWebProvider
             -- StepFunWebProvider
             -- QwenWebProvider
             -- GlmWebProvider
             -- LocalOpenAIProvider
   |
Browser Session + provider DOM driver
```

`agent.py` 只认识 `ChatProvider`、`ProtocolCodec` 和工具调用，不知道具体网页 DOM。DeepSeek、MiMo、StepFun、Qwen 和 GLM 的网页选择器分别在自己的驱动里。

---

## 项目结构

```text
codey/
  agent.py                  与模型网站无关的 agent runtime
  models.py                 共享的工具调用和协议数据模型
  cancellation.py           共享的任务级取消和进程清理
  events.py                 结构化运行事件和日志渲染
  text_budget.py            有上限的命令输出头尾截取
  bounded_scan.py           共享的有边界本地文件遍历
  scan_report.py            紧凑的扫描遗漏事实和覆盖范围渲染
  tool_definition.py        内部 coding 工具元数据和渲染提示
  permission_profiles.py    内部工具/上下文 permission profile
  context_source.py         命名且有边界的 prompt 上下文装配
  tool_runtime.py           本地工具和结构化执行结果
  execution_evidence.py     有边界的内存执行证据账本
  run_ledger.py             append-only 项目任务运行事实账本
  run_ledger_projection.py  run ledger 的只读摘要和 receipt 投影
  references.py             有边界的文本引用提示
  change_set.py             结构化 diff 文件、hunk 和 rename/copy 事实
  changed_symbols.py        从可见 diff 提取变化的 symbol
  project_map.py            确定性的有边界项目地图
  project_config.py         严格项目本地配置事实和 warning
  project_task_context.py   项目事实、地图、checkpoint 和验证上下文
  ghost/                    Ghost 信号抽取、记忆状态、continuity、路由、本地待办队列、affinity 账本和本地上下文控制面
  knowledge/                本地 Markdown vault、FTS 索引、restore 和 Research Brief
  research/                 Research controller/runner、隔离网页/source search 工具、evidence ledger 和 report quality gate
  verification_map.py       Review 阶段的有边界验证候选
  review_impact_map.py      只给 Review 使用的 caller/test 影响提示
  change_brief.py           隐藏任务意图 brief
  review_coordinator.py     有边界的 diff review 生命周期
  task_runner.py            任务、会话、review 和收据编排
  headless_runner.py        复用 TaskRunner 的 JSONL 脚本/CI 入口
  browser.py                Chromium CDP 连接
  browser_worker.py         Playwright 线程调度
  changes.py                Git 与 snapshot diff / restore
  local_store.py            共享本地数据根目录和原子 JSON 写入
  managed_outputs.py        被裁剪命令输出的 run 级本地 handle
  project_facts.py          经过成功运行验证的项目事实
  work_checkpoint.py        未完成执行的持久事实检查点
  conversation_store.py     有上限的对话事实持久化
  provider_profiles.json    支持模型网页的版本化选择器
  provider_profiles.py      经过验证的 Profile 加载
  provider_discovery.py     有边界的 DOM 候选发现和评分
  provider_controls.py      经过验证的恢复、记忆和人工教学
  provider_flow.py          有边界的网页布尔状态规则
  provider_revival.py       控件恢复包的原子保存、晋级和回滚
  provider_submission.py    共享的单次远程提交边界
  provider_send_loop.py     网页模型发送循环生命周期 helper
  provider_timeouts.py      共享的 provider deadline 和导航超时 helper
  provider_capabilities.py  provider 静态适配提示和 fallback 排序
  provider_supervisor.py    被动健康熔断、Writer 选择和 canary
  adapter_overrides.py      本地 adapter 候选、晋级和回滚
  adapter_repair.py         sandbox 中的 Provider adapter 修复执行器
  repair_policy.py          严格的 adapter 修复文件与代码策略
  repair_sandbox.py         adapter 修复用临时源码副本
  repair_journal.py         有边界的本地 adapter 修复日志
  self_repair.py            去重的后台自修复队列
  self_repair_worker.py     修复子进程入口和 helper 选择
  provider_worker.py        父进程侧 adapter worker 包装
  provider_worker_child.py  子进程 adapter 运行器
  profile_doctor.py         单次脱敏候选选择
  json_tool_reply.py        最终 JSON tool reply 的宽容检测和修复
  web_clipboard.py          有边界的复制按钮剪贴板事务 helper
  deepseek.py               DeepSeek 页面驱动
  mimo.py                   MiMo 页面驱动
  stepfun.py                StepFun 页面驱动
  qwen.py                   Qwen 页面驱动
  glm.py                    GLM 页面驱动
  provider_diagnostics.py   小型 provider 失败记录
  receipt.py                任务完成收据
  protocols/
    json_codec.py           JSON-only 工具协议
  providers/
    registry.py             模型注册表和同一 CDP 标签页借用
    local_openai.py         OpenAI-compatible 本地模型 provider
    *_web.py                网页模型适配器
  server.py                 本地 HTTP + SSE 传输和运行状态
  web/
    index.html              UI 核心：state、SSE、composer、boot
    assets/                 零构建 CSS tokens/样式和普通脚本 UI 模块
```

---

## 限制

- 网页 AI 改版可能导致自动化失效。
- 不同模型质量不同。
- 网页模型有时会写得啰嗦或不够干净。
- Codey 是本地开发工具，不是安全沙箱。
- 生成代码仍然需要你检查 diff 后再保留。

---

## 许可证

Codey 使用 GNU General Public License version 2 only（`GPL-2.0-only`）
发布。详见 [LICENSE](LICENSE)。
