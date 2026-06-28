# Codey

**让网页版 AI 成为本地编程助手。**

[English](README.md)

Codey 可以连接你已经在用的网页版 AI，比如 DeepSeek、Qwen 和小米 MiMo，然后给它们一个受控的本地工具循环：读文件、改文件、跑测试、看 diff、恢复改动。

不需要 API key，不需要充值 API 额度。你只要能在 Edge 里登录网页 AI，就可以用 Codey 开始写代码。

版本：`0.1.0`

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

- 在本地控制面板里选择 DeepSeek、MiMo 或 Qwen
- 让模型读取和修改你选择的项目目录
- 运行测试，并把结果继续反馈给模型
- 显示红绿 diff
- 即使没有 Git，也能用 snapshot diff 和 restore 恢复改动
- 有 Git 时自动增强为 Git diff / commit 工作流
- 一个模型失败时，可以换另一个模型再试
- 如果另一个模型已经打开，可以无感作为代码 reviewer
- 底层记录很小的失败诊断信息，方便网页改版后定位问题

---

## 支持的模型

| 模型 | 状态 |
|---|---|
| DeepSeek Web | 已实机测试 |
| Xiaomi MiMo Chat | 已实机测试 |
| Qwen Studio | 已实机测试 |

Codey 使用浏览器自动化，所以网页 AI 改版后可能会失效。当前架构把不同网站的适配代码隔离开，网页变了就修对应 adapter，不需要改 agent 核心。最近的实机测试也加强了 MiMo 发送逻辑，确保点击真正的发送按钮，而不是旁边的上传按钮；同时也让 Qwen 更容易理解本地 JSON 工具协议。

---

## 双模型 Review

如果 Codey 发现 Edge 里已经打开了另一个支持的模型，它可以在主模型完成代码修改后，让第二个模型静默做一次 review。

review 模型不会直接改文件，只会阅读压缩后的 diff，然后返回结构化意见。如果它通过，任务结束；如果它指出具体问题，Codey 会把意见发回主模型，让主模型再修一次。如果 review 不可用，Codey 会自动退回单模型结果。

这不会把主界面变成群聊，也不会增加一个新开关。对新手来说，它只是“有第二个模型时更稳一点”。

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

### 4. 选择项目，然后用人话描述任务

例如：

```text
写一个 Python 贪吃蛇小游戏，放在 snake.py，一个文件就能运行。
运行方式是 python snake.py。
```

Codey 会让网页 AI 返回结构化工具调用，然后在本地真实读写文件，并显示改动。

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

这不代表 Codey 永远不会坏。它证明的是：当 Codey 出现可测试、可定位的问题时，它已经有机会依靠接入的网页 AI、本地工具、diff、restore 和测试，把自己修回来。

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
   |
Browser Session + provider DOM driver
```

`agent.py` 只认识 `ChatProvider`、`ProtocolCodec` 和工具调用，不知道具体网页 DOM。DeepSeek、Qwen 和 MiMo 的网页选择器分别在自己的驱动里。

---

## 项目结构

```text
codey/
  agent.py                  与模型网站无关的 agent runtime
  browser.py                Edge/CDP 连接
  browser_worker.py         Playwright 线程调度
  changes.py                snapshot diff 和 restore
  deepseek.py               DeepSeek 页面驱动
  mimo.py                   MiMo 页面驱动
  qwen.py                   Qwen 页面驱动
  provider_diagnostics.py   小型 provider 失败记录
  protocols/
    json_codec.py           JSON-only 工具协议
  providers/
    registry.py             模型注册表
    *_web.py                网页模型适配器
  server.py                 本地 HTTP + SSE 后端
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
