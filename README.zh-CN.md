# Codey

**让网页版 AI 成为本地编程助手。**

[![版本](https://img.shields.io/badge/version-0.1.49-blue)](CHANGELOG.zh-CN.md)
[![许可证：GPL v2](https://img.shields.io/badge/license-GPL--2.0--only-blue)](LICENSE)
[![本地优先](https://img.shields.io/badge/local--first-web%20AI%20coding-2ea44f)](#安全模型)

[English](README.md)

Codey 可以连接你已经在用的网页版 AI，比如 DeepSeek、Qwen、小米 MiMo 和 GLM，然后给它们一个受控的本地编程循环：读文件、改文件、跑测试、看 diff、审查改动、必要时恢复。

它是一个本地优先、低成本、多网页模型兼容的 AI 编程工作台，适合不想为每个项目接入付费模型 API 的用户。

不需要 API key，不需要充值 API 额度。你只要能在 Edge 或 Chrome 里登录网页 AI，就可以用 Codey 开始写代码。

版本：`0.1.49`

[版本更新记录](CHANGELOG.zh-CN.md)

---

## 一眼看懂

- **使用你已经登录的网页 AI**：支持 DeepSeek、Qwen、小米 MiMo 和 GLM。
- **代码留在本机**：模型只能访问你选择的项目目录。
- **受控工具循环**：读取、编辑、测试、diff、Review 和 Restore 都有边界。
- **需要时多模型协作**：一个模型写代码，另一个模型审查最终 diff。
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
- 在同一个项目对话里讨论、查看和修改；只有明确要求时才改文件
- 让模型读取和修改你选择的项目目录
- 运行允许的测试、构建、lint 和类型检查，并把结果继续反馈给模型
- 显示红绿 diff
- 每次任务结束后显示一条克制的任务收据，例如 `DONE · 2 files changed · checks passed · restore available`
- 即使没有 Git，也能用 snapshot diff 和 restore 恢复改动
- 有 Git 时自动增强为 Git diff / commit 工作流
- 一个模型失败时，可以换另一个模型再试
- 已打开的其他模型可以作为隐藏顾问，参与普通聊天、空项目规划和项目只读审查
- 两个网页模型可以一起协作：一个写代码，另一个帮忙检查
- 用隐藏任务 brief 让 Writer 和 Reviewer 共享同一份有边界的意图
- 在模型真正读文件前，给 Writer、隐藏顾问和 Reviewer 一份有边界的本地项目地图
- 让模型在改某个符号前，先请求有边界的文本引用提示
- Python replacement edit 新引入语法错误时立即提示，但不自动回滚，也不冒充检查通过
- 长对话接近上限时，自动总结事实并在新对话里无感继续
- 记住真实运行成功的项目命令，后续任务不必重新猜测
- 只从通过本地检查的成功改动里沉淀最近变更事实
- 重启 Codey 或切换模型后，同一聊天可以通过精简事实 handoff 和最近可见对话自然继续
- 非 Git 项目的 diff 和 restore 在 Codey 重启后仍然可用
- 网页输入框或发送按钮改版时，先做有边界的本地发现，仍不确定则让健康兄弟模型
  从脱敏候选中选择；真实发送成功后才能保存、晋级或回滚恢复包
- 控件恢复仍不足时，只根据脱敏布尔事实恢复一条有边界的网页状态规则；当前 Qwen
  completion 必须观察到真实的“生成中 → stop 消失”转换
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
| Xiaomi MiMo Chat | 已实机测试 |
| Qwen Studio | 已实机测试 |
| GLM | 已实机测试 |

Codey 使用浏览器自动化，所以网页 AI 改版后可能会失效。当前架构把不同网站的适配代码隔离开，网页变了就修对应 adapter，不需要改 agent 核心。

如果网页改版，Codey 会先尝试上面的有边界恢复。仍然无法安全识别时，它才会安静地暂停，请你在网页里点一次那个控件。Codey 只保存最新一条经过验证的控件记录，不保存网页 DOM 或完整聊天，不会打扰主流程。

新控件只有在原消息只提交一次、并且成功读到新回答后，才会作为一组恢复包原子保存。
第一次成功只是 provisional，下一次自然发送成功后才晋级 active；明确的连续控件失败
会恢复上一版。健康 Provider 的正常发送不会调用兄弟模型。

同一恢复包现在还可以保存一条有边界的 Flow Recipe。它只能组合固定布尔事实，例如
回答稳定和经过验证的 stop 或 typing 状态转换，不能包含 selector、JavaScript、URL、
任意点击、网页正文或项目数据。Codey 绝不会只凭“文字暂时稳定”猜测回答已经结束。
Qwen 使用真实 stop 状态转换，MiMo 使用明确 typing 状态转换；GLM 在没有同等可靠的
终止证据时继续安全降级。

如果网页变化大到 adapter 代码本身也坏了，Codey 现在可以把这个 Provider 放入后台
自修复队列。自修复运行在独立 Python 进程中，只允许健康 helper 模型修改坏掉的
Provider adapter 文件，并在临时 sandbox 里通过策略检查、静态检查、对应 Provider
单测和中性 marker canary。候选 adapter 不会直接加载进主进程，而是通过子进程
Provider worker 运行；worker 使用同一个已登录 Codey 浏览器 profile 的后台新标签页，
不会复制 cookie，也不会阻塞你当前的编程任务。候选先是 provisional，自然成功后才
晋级 active，连续结构性失败会自动回滚。`agent.py`、`task_runner.py`、`tool_runtime.py`、
`server.py` 以及恢复/安全控制面不在 v1 自修改范围内。

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
  bounded_scan.py           共享的有边界本地文件遍历
  scan_report.py            紧凑的扫描遗漏事实和覆盖范围渲染
  tool_runtime.py           本地工具和结构化执行结果
  execution_evidence.py     有边界的内存执行证据账本
  references.py             有边界的文本引用提示
  project_map.py            确定性的有边界项目地图
  project_task_context.py   项目事实、地图、checkpoint 和验证上下文
  verification_map.py       Review 阶段的有边界验证候选
  change_brief.py           隐藏任务意图 brief
  review_coordinator.py     有边界的 diff review 生命周期
  task_runner.py            任务、会话、review 和收据编排
  browser.py                Chromium CDP 连接
  browser_worker.py         Playwright 线程调度
  changes.py                Git 与 snapshot diff / restore
  local_store.py            共享本地数据根目录和原子 JSON 写入
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

## 许可证

Codey 使用 GNU General Public License version 2 only（`GPL-2.0-only`）
发布。详见 [LICENSE](LICENSE)。
