# Codey

**让网页版 AI 成为本地编程助手。**

[English](README.md)

Codey 可以连接你已经在用的网页版 AI，比如 DeepSeek、Qwen、小米 MiMo 和 GLM，然后给它们一个受控的本地工具循环：读文件、改文件、跑测试、看 diff、恢复改动。

它是一个本地优先、低成本、多网页模型兼容的 AI 编程工作台。

不需要 API key，不需要充值 API 额度。你只要能在 Edge 里登录网页 AI，就可以用 Codey 开始写代码。

版本：`0.1.19`

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

## 它能做什么？

- 用 New Chat 正常聊天，不向模型开放任何项目
- 在同一个项目对话里讨论、查看和修改；只有明确要求时才改文件
- 让模型读取和修改你选择的项目目录
- 运行测试，并把结果继续反馈给模型
- 显示红绿 diff
- 每次任务结束后显示一条克制的任务收据，例如 `DONE · 2 files changed · checks passed · restore available`
- 即使没有 Git，也能用 snapshot diff 和 restore 恢复改动
- 有 Git 时自动增强为 Git diff / commit 工作流
- 一个模型失败时，可以换另一个模型再试
- 已打开的其他模型可以作为隐藏顾问，参与普通聊天、空项目规划和项目只读审查
- 两个网页模型可以一起协作：一个写代码，另一个帮忙检查
- 长对话接近上限时，自动总结事实并在新对话里无感继续
- 记住真实运行成功的项目命令，后续任务不必重新猜测
- 重启 Codey 后，同一聊天可以通过精简事实摘要自然继续
- 非 Git 项目的 diff 和 restore 在 Codey 重启后仍然可用
- 网页模型小幅改版时，通过有边界、经过验证的发现机制自动恢复
- 及时停止正在等待的网页模型、Review、恢复流程或测试命令
- 长命令输出同时保留开头与结尾，不丢失末尾错误摘要
- UI 重连后自动恢复运行状态、审批或人工教学
- 网页提交结果不确定时绝不重复发送
- 底层记录很小的失败诊断信息，方便网页改版后定位问题

---

## 支持的模型

| 模型 | 状态 |
|---|---|
| DeepSeek Web | 已实机测试 |
| Xiaomi MiMo Chat | 已实机测试 |
| Qwen Studio | 已实机测试 |
| GLM | 已实机测试 |

Codey 使用浏览器自动化，所以网页 AI 改版后可能会失效。当前架构把不同网站的适配代码隔离开，网页变了就修对应 adapter，不需要改 agent 核心。

`0.1.19` 继续细化 MiMo 的完成判断，前端不变。MiMo 仍然把纸飞机 SVG 作为点击发送前的最后一道 provider-specific 防误点保险，但不再用这个图标判断回答是否已经完成。回答完成现在看最新答案 DOM：清理思考区后必须有最终文本，`data-is-typing` 不能处于活跃状态；如果答案附近已经出现 copy 按钮，就直接认为完成；如果没有 copy 按钮，则仍要求页面不在生成状态。这样 SVG 只负责“能不能点发送”，不再承担回答状态机。本版通过 452 项测试、聚焦 provider/protocol/server 回归、语法编译和 `git diff --check`，只有 CRLF warning。

`0.1.18` 保持前端不变，重点收紧网页 Provider 稳定性和本地结果的真实性。MiMo 现在只接受明确的纸飞机发送按钮，回答仍在生成时拒绝提交新消息，并在读取最终答案前移除 MiMo 的思考折叠块，避免误点上传按钮或在停止态误提交。Qwen 增加更严格的本地发送按钮兜底，隐藏 Review 阶段也会压制人工教学，私有 repair 轮不会再卡住等用户点击网页控件。GLM 可以处理回答文本被原地替换但数量不增加的情况，智能引号修复也仍然只限 GLM 回复。Local JSON 协议现在会拒绝藏在 `done(summary)` 里的嵌套工具调用，并要求模型用 `edit(content=...)`，不再走旧的 write 风格工具。Review 修复轮的收据也会区分本轮是否真的跑过检查，因此失败测试或未完成修复不会再继承上一轮的 `checks passed`。本版通过 450 项测试、语法编译和 `git diff --check`；真实 MiMo 与 DeepSeek/MiMo smoke 确认没有上传弹层、没有误停止、隐藏审查有效，Review 正常批准。

`0.1.17` 增加隐藏的 MoA 层，但不增加任何前端控件。只要已经打开了其他支持的模型网页，`New Chat` 会先让当前选中的模型给出私有 draft，再让最多两个其他模型对这个 draft 挑错、补漏或提出替代方向，最后仍由当前选中的模型输出一条最终回答。空项目或只有占位文件的项目也使用这种“主模型先判断”的隐藏规划流程，适合从零开始集思广益，同时避免主模型只变成替其他模型整理文字的秘书。已有项目保持另一条边界：顾问先做有边界的只读审查，Writer 后面仍然会自己读取真实文件并验证报告。项目审查顾问可以列目录、搜索、读取自己认为相关的非敏感文件，但不能改文件、不能运行命令、不能请求 Shell 审批，也不能读取项目外内容。dotfile、env 文件、像密钥的路径、被排除的依赖 / 构建目录、key / 证书文件、锁文件、二进制文件、符号链接和过大的文件都不会发给隐藏项目审查顾问。draft-first 顾问失败时，会安静使用主模型 draft 作为兜底；如果 final synthesis 在 draft 后失败，Codey 不会重新发送原始问题。Review 仍然完全分离：只有真实改过文件后才看最终 Diff。本版的确定性 UI E2E 已覆盖普通聊天共识、项目审查共识、空项目规划，以及原有写入、测试、Review、Restore 全链路，前端没有新模式。

`0.1.16` 保留 `New Chat` 作为安全的普通聊天入口，不向模型开放项目；项目聊天则可以在同一条线里先讨论、再读代码、再修改代码，不需要切换模式。只读的项目问题会直接显示所选模型的完整回答，不再显示没有帮助的 `No files changed` 收据。只有本次任务真的写过文件时，Codey 才继续显示最终回答、测试收据、Diff 入口，并按原有规则触发第二模型 Review。前端没有增加按钮或新概念；本版通过 388 项测试、真实 Edge UI E2E 全部 19 项检查，以及一次真实 DeepSeek 项目讨论测试，1 轮完成且没有创建文件。

`0.1.15` 增加 GLM 作为第四个模型，但不增加新的使用流程。它的 Profile 会验证真正的非空白发送状态，只读取正式答案而不混入旁边的思考区，并复用其他模型已有的单次提交、取消、恢复和 ProfileDoctor 边界。一条仅对 GLM 生效的小型格式提示会让工具回复使用一个带 ASCII 引号的 raw local-runner JSON object，但不会强迫普通聊天返回 JSON。Provider 构造和 smoke 选项现在统一来自一份注册表；原有的黑白模型选择器只增加一行 `GLM`。Qwen 现在会等待网页的模型初始化完成，不再把过早出现的输入框误判为可发送；真实需要的草稿沉降和 A/B 选择处理仍然保留。本版通过 384 项测试和真实 Edge UI E2E 全部 16 项检查；GLM 已多次完成实机编辑任务并通过独立 unittest 验证，还在另一项实机任务中作为只读 Reviewer 批准了 DeepSeek 的 Diff。Qwen 两次从全新对话完成实机编辑任务，分别使用 4 轮和 5 轮。

`0.1.14` 保持前端不变，让本地工具协议更一致、更安全，也更节省任务过程中的上下文。一份小型工具契约统一提供给模型的名称、别名、示例、只读属性和结果名称。`parallel` 只接受数量受限的 `list_dir`、`read_file` 和 `grep`；批次中只要混入写文件、运行命令或嵌套批处理，就会在执行任何一项前整体拒绝。大文件按完整代码行分页，文件正文使用 16,000 字符预算，并明确给出下一页位置；小文件仍返回与之前完全相同的内容。一次 `edit` 最多可以对同一文件执行 8 个替换，但只有全部替换都在内存中验证成功后才会写入。本版通过 363 项测试、真实 Edge UI E2E 全部 16 项检查，以及 DeepSeek、Qwen、MiMo 三站各 4 轮的实机编辑任务。

`0.1.13` 保持前端不变，只收紧底层结构。Git 和 snapshot 的改动处理统一收到 `changes.py`；本地运行数据共用一个存储根目录；UI、CLI 和 smoke 中的每项任务都有明确的 Provider 上下文，无论成功、取消、连接失败还是 CDP 关闭失败都会清理。Qwen 现在用一次真实尾随按键提交网页输入状态，并在发送前验证实际文本。本版通过 344 项测试、真实 Edge UI E2E 全部 16 项检查，以及 DeepSeek、Qwen、MiMo 三站实机编辑任务。

`0.1.12` 让 UI 在刷新或 SSE 短暂中断后，仍能与后端的真实任务状态对齐。后端原子创建唯一 `run_id`，只保留一份有上限的内存快照；前端会无感恢复 Stop、Shell 审批、控件教学和最终收据，并且不重复显示 `task_done`。同一时间只运行一个对账请求；更新的 SSE 事件会短暂缓冲，在快照应用后按顺序重放，因此较慢的旧快照不会把已经完成的任务重新变成 Running。清空聊天也会同步撤销后端保留的旧收据。断线在 5 秒内不显示任何东西；只有持续更久时，才复用顶部状态行显示 `Reconnecting…`。Shell 审批结果同时通过 HTTP 响应和 SSE 送达，并使用同一去重键；最新一条结果也保留在有上限的快照里，收到结果后会移除旧审批卡。对账会保持真实执行顺序，Shell 结果会先于它继续的任务完成收据出现。DeepSeek、Qwen 和 MiMo 共用一个单次提交边界：操作前先确定发送方式，远程提交最多一次；结果不确定时不重发，而是在完整回答窗口内继续等待原回答。本版没有自动重发，也没有增加新界面概念；通过 331 项测试、真实 Edge UI E2E 全部 16 项检查，以及三站单次提交实机探针。

`0.1.11` 让现有 Stop 在网页模型轮询、恢复、Review 和受控测试命令期间及时生效。一个共享的任务级取消信号会中断这些等待；停止后丢弃当前模型会话，下一次任务从新对话开始。单次同步的网页导航、点击或填写仍会遵循 Playwright 自身的超时。Codey 不会点击网页的“停止生成”，前端也没有增加新概念。过长的 `run` 和批准 Shell 输出会在同一容量上限内同时保留开头和结尾，不再丢失末尾的异常总结。本次发布通过 310 项测试、真实 Edge UI E2E 全部 10 项检查，以及 DeepSeek、Qwen、MiMo 三站的真实取消探针。

`0.1.10` 增加了无感的第二层恢复 ProfileDoctor。本地有边界发现无法安全选择时，一个已经打开且可用的其他模型只会收到最多 8 个经过严格脱敏的结构候选，并且只能返回一个候选编号。它不能提供选择器、代码、正文或坐标；每次恢复只调用一次，也不能递归求助。候选仍要通过原有的填写、发送或回答读取验证后才会保存；模型放弃或失败时，人类点击教学仍是最后兜底。前端没有增加任何概念。本次发布通过 294 项测试、完整 Edge UI E2E，以及 DeepSeek、Qwen、MiMo 三站的 ProfileDoctor 发送故障注入。

`0.1.9` 增加了有边界的网页小改版恢复能力，不增加任何 UI。DeepSeek、Qwen 和 MiMo 共用版本化选择器 Profile；固定选择器失效时，Codey 会克制地重新发现输入框、发送按钮和最新回答。只有可操作控件唯一匹配，并且填写、发送或读取结果得到实际验证后，才会记住新控件；连续失败会自动遗忘。自动判断没有把握时，原有的人类点击教学仍是最后兜底。本次发布通过 277 项测试、完整 Edge UI E2E、三站正常实机任务，以及三站核心选择器强制失效后的自动恢复测试。

`0.1.8` 增加了一层隐藏的本地连续性：每个项目只保存少量真实验证过的运行命令，每个近期聊天只保存一份有上限的事实摘要，非 Git 项目的当前恢复基线会在写文件前原子落盘。它们不会在主界面增加按钮或提示，也不会保存 Cookie、网页 DOM 或完整聊天。本次发布通过 260 项测试、真实 Edge UI 全链路和 DeepSeek / Qwen / MiMo 编辑矩阵。

`0.1.7` 保持产品体验不变，同时让底层更容易维护。本地工具改为返回结构化结果，Agent 使用统一的结构化事件，任务编排与 HTTP 传输分离，前端也不再解析给人看的日志。本次发布通过了 224 项自动测试，以及真实 Edge、三模型、双模型 review、上下文接力和自举修复流程。

`0.1.6` 增加了隐藏的上下文安全网。接近统一的上下文预算时，Codey 会让当前模型生成一份精简的事实总结，打开新对话并继续，不在主界面增加按钮或提示。总结失败时会退回本地任务事实；新对话或第一条接力消息失败时，旧预算和总结仍会保留，方便重试。

`0.1.4` 增加了对话流里的轻量任务收据，让新手不用打开新面板，也能知道这次改了几个文件、检查是否通过、是否可以恢复。`0.1.3` 让模型浏览器可以长期保留：重启 Codey UI 后，会优先复用已经存在的 Edge CDP 浏览器和模型网页；如果没有可用的 CDP 浏览器，才会自动打开新的模型浏览器。

如果网页改版，Codey 会先尝试上面的有边界恢复。仍然无法安全识别时，它才会安静地暂停，请你在网页里点一次那个控件。Codey 只保存最新一条经过验证的控件记录，不保存网页 DOM 或完整聊天，不会打扰主流程。

---

## 双模型协助

一个 AI 可以写代码，但它也可能漏掉小错误。两个 AI 的意义不是把界面变复杂，而是让流程更稳：一个模型专心写，另一个模型像第二双眼睛一样帮你检查刚改过的代码。

你不需要学习一个新模式。只要你在 Edge 里打开两个支持的网页 AI，Codey 就可以自动把它们配合起来：

- 你在 Codey 里选择的模型，负责写代码。
- 另一个已经打开的支持模型，自动负责检查。
- 写代码的模型会读文件、改文件、跑测试。
- 检查模型不会直接碰你的文件，只会看 diff，然后指出具体问题。
- 如果检查通过，任务就结束。
- 如果检查模型发现真实问题，Codey 会把意见发回写代码的模型，让它再修一次。

只有所选模型在本次任务里真的改了文件，第二个模型才会开始检查。项目内的
普通问答和只读分析会直接显示所选模型的完整回答，不会把聊天强行变成 Review。

如果你只打开了一个模型网页，Codey 就保持单模型模式。如果第二个模型没有打开、没有登录、或者中途失败，Codey 会安静地退回单模型结果。

一句话：简单任务开一个模型就够；想更稳一点，就多打开一个支持的模型网页。没有群聊界面，没有额外开关，也不会把主界面变复杂。

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

第一次运行任务时，Codey 会打开一个专用 Edge 窗口。你需要在里面手动登录所选模型。

这个 Edge profile 和你日常用的 Edge 是分开的：

```text
C:\Users\<你>\.codey\edge-profile
```

登录一次后，后面通常不用重复登录。

这个模型 Edge 窗口可以一直留着。你关闭再重启 Codey UI 时，Codey 会尽量安静地连回已经打开的 CDP 浏览器和模型标签页。

### 4. 选择项目，然后用人话描述任务

例如：

```text
写一个 Python 贪吃蛇小游戏，放在 snake.py，一个文件就能运行。
运行方式是 python snake.py。
```

Codey 会让网页 AI 返回结构化工具调用，然后在本地真实读写文件，并显示改动。

你也可以先在同一个项目聊天里讨论方案，之后再让它写代码。如果只想普通聊天、
不让模型接触任何项目，就使用 **New Chat**。

任务结束后，Codey 会用一行很轻的收据总结本地事实：

```text
DONE · 2 files changed · checks passed · restore available        View diff
```

点击 `View diff`，右侧栏会打开具体的红绿 diff。

---

## 安全边界

Codey 不是无限制 shell。

- 文件读写限制在你选择的项目目录里。
- 正常改动会显示 diff。
- 没有 Git 也能用 snapshot restore 恢复。
- Git 是增强功能，不是入门门槛。
- shell 命令需要你确认后才会执行。
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

Codey 已经测试过用 DeepSeek、MiMo 和 Qwen 修复被故意弄坏的 Codey 临时副本。

每个模型都完成了这个闭环：

1. 运行失败测试；
2. 阅读 Codey 自己的源码；
3. 修改坏掉的代码；
4. 重新运行测试；
5. 测试通过后结束。

见 [BOOTSTRAP_PROOF.md](BOOTSTRAP_PROOF.md)。

当前版本还包含 [TEST_REPORT.md](TEST_REPORT.md)，记录最近一次单模型、双模型和自举 smoke 的实机结果。

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
             -- QwenWebProvider
             -- MimoWebProvider
             -- GlmWebProvider
   |
Browser Session + provider DOM driver
```

`agent.py` 只认识 `ChatProvider`、`ProtocolCodec` 和工具调用，不知道具体网页 DOM。DeepSeek、Qwen、MiMo 和 GLM 的网页选择器分别在自己的驱动里。

---

## 项目结构

```text
codey/
  agent.py                  与模型网站无关的 agent runtime
  cancellation.py           共享的任务级取消和进程清理
  events.py                 结构化运行事件和日志渲染
  text_budget.py            有上限的命令输出头尾截取
  tool_runtime.py           本地工具和结构化执行结果
  task_runner.py            任务、会话、review 和收据编排
  browser.py                Edge/CDP 连接
  browser_worker.py         Playwright 线程调度
  changes.py                Git 与 snapshot diff / restore
  local_store.py            共享本地数据根目录和原子 JSON 写入
  project_facts.py          经过成功运行验证的项目事实
  conversation_store.py     有上限的对话事实持久化
  provider_profiles.json    支持模型网页的版本化选择器
  provider_profiles.py      经过验证的 Profile 加载
  provider_discovery.py     有边界的 DOM 候选发现和评分
  provider_controls.py      经过验证的恢复、记忆和人工教学
  provider_submission.py    共享的单次远程提交边界
  profile_doctor.py         单次脱敏候选选择
  deepseek.py               DeepSeek 页面驱动
  mimo.py                   MiMo 页面驱动
  qwen.py                   Qwen 页面驱动
  glm.py                    GLM 页面驱动
  provider_diagnostics.py   小型 provider 失败记录
  receipt.py                任务完成收据
  protocols/
    json_codec.py           JSON-only 工具协议
  providers/
    registry.py             模型注册表和同一 CDP 标签页借用
    *_web.py                网页模型适配器
  server.py                 本地 HTTP + SSE 传输和运行状态
  web/
    index.html              单文件控制面板
```

---

## 限制

- 网页 AI 改版可能导致自动化失效。
- 不同模型质量不同。
- 网页模型有时会写得啰嗦或不够干净。
- Codey 是本地开发工具，不是安全沙箱。
- 生成代码仍然需要你检查 diff 后再保留。

---

## 理念

我希望 Codey 能做一点“平权”的事。

如果 AI 编程只对能负担昂贵 API 费用的人好用，那很多新手会被挡在门外。Codey 选择了一条更简单的路：把大家已经能访问的网页 AI，谨慎地接到本地文件、测试、diff 和 restore 上，让更多人能更早开始编程和创造。

它不需要夸张的口号。只要一个普通人能打开它、说出想法、看到改动、运行程序、失败后还能恢复，这就已经很有意义。
