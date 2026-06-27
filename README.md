# Codey · 让网页版 AI 帮你在本地写代码

不需要 API key，不需要充值。只要你能在浏览器里登录 DeepSeek 或 Qwen，Codey 就能把它当成一个能读你硬盘、改你代码的本地编程助手。

---

## 这是什么？

Codey 启动后会做三件事：

1. 按你选择的模型打开**专属的 Edge 浏览器窗口**（跟你日常用的 Edge 完全分开，互不干扰）。
2. 开一个本地网页 `http://127.0.0.1:5173/`，是 Codey 的控制面板。
3. 你在 Codey 控制面板里选择 DeepSeek 或 Qwen，再用大白话描述要做什么。模型返回的受控工具调用会在你的项目目录里**真的执行**。

整个过程不联任何 API，全是浏览器自动化。

---

## 第一次使用，跟着做 5 步

### 1. 装 Python 依赖（一次性）

打开 PowerShell，粘贴并回车：

```powershell
pip install playwright
```

> Playwright 自带的浏览器**不需要**下载，Codey 是去连你电脑上已经装好的 Microsoft Edge。

### 2. 启动 Codey

```powershell
cd E:\codey
python -m codey
```

你会看到：

```
[codey] UI ready: http://127.0.0.1:5173/
```

**两件事会同时发生**：
- 你的默认浏览器自动打开 `http://127.0.0.1:5173/` —— 这是 Codey 的控制面板。
- 还**没有**弹出新的 Edge 窗口。Edge 是在你第一次点「运行」时才启动的。

### 3. 在控制面板里填两栏

- **项目目录**：你想让 Codey 把代码写到哪里。比如 `E:\my-snake-game`。不存在的话 Codey 会自动建。
- **任务**：用人话描述。比如：
  > 写一个完整的贪吃蛇游戏，pygame 实现，单文件 snake.py，能用 `python snake.py` 直接运行。

点 **运行**。

### 4. 第一次：在弹出的 Edge 窗口里登录所选模型

第一次点「运行」时，会弹一个**新的** Edge 窗口，打开所选的 `chat.deepseek.com` 或 `chat.qwen.ai`。

⚠️ 这个 Edge 跟你平常用的 Edge 不是同一个 profile —— 它用的是 `C:\Users\<你>\.codey\edge-profile`，登录信息只存这里，跟你的日用 Edge 互不污染。

**手动在那个新 Edge 窗口里登录一次所选模型**，然后**不用关掉**，回到 Codey 控制面板。

> 以后再用都不用登录了 —— 这个 profile 会一直记住登录态。

### 5. 看 Codey 干活

回到控制面板，下方会**实时**显示：

- 模型每一轮回复的原文（灰色方块）
- Codey 实际执行的工具调用（`search` / `read` / `edit` / `write` / `run` 等）
- 最终的 `done` 完成信号（绿色方块）

任务结束后，你的项目目录里就有真实的代码文件了。

---

## 已经验证可以跑通的示例

### 示例 A：从零生成贪吃蛇

**项目目录**：`E:\demo_snake`
**任务**：
> Write a complete classic Snake game in pygame as a single file snake.py. The file must run with: python snake.py

结果：生成了 277 行的 `snake.py`，用 `python snake.py` 可直接运行（前提是装了 `pip install pygame`）。

### 示例 B：让它读 + 改一个有 bug 的程序

**准备**：在 `E:\demo_fix\buggy.py` 放：

```python
def average(numbers):
    total = 0
    for n in numbers:
        total += n
    return total / len(numbers) + 1   # 错误的 +1
```

**项目目录**：`E:\demo_fix`
**任务**：
> There is a file buggy.py with a subtle bug. Read it, fix the bug, write the corrected version back to buggy.py. Then call done.

Codey 自动走两轮：
- 第 1 轮：`read buggy.py` → DeepSeek 看到了文件内容
- 第 2 轮：`write buggy.py`（去掉了那个 `+ 1`） → `done`

跑 `python buggy.py` 输出 `average is 25.0`，bug 修好了。

---

## Codey 的"工具"

Codey 会把模型回复解析成一组受控工具调用。**你不用记协议**，正常描述需求即可：

| 工具 | 作用 |
|------|------|
| `search` | 在项目里搜索关键词，先定位文件 |
| `edit` | 精准替换文件中的一小段内容 |
| `write path=foo.py` | 把整个代码体写进 `foo.py`（覆盖） |
| `read path=foo.py`  | 让 DeepSeek 读这个文件 |
| `ls path=.`         | 列目录内容 |
| `run` | 运行受允许的测试 / 构建命令 |
| `shell` | 申请执行其它命令，必须先由你确认 |
| `done` / `continue` | 告诉 Codey 任务结束 / 还要再来一轮 |

`run` 只允许常见测试和构建命令。`shell` 不会自动执行，会先在界面里显示命令并等待你批准。

---

## 常见问题

**Q：弹出的 Edge 窗口我能关掉吗？**
A：Codey 进程还在跑的时候不要关，关了就断连了。任务结束、退出 Codey 之后关就行。

**Q：可以同时开自己的 Edge 吗？**
A：可以。Codey 用的是独立 profile，跟你日用 Edge 是两个进程，谁也不影响谁。

**Q：模型网页改版了，Codey 报错找不到输入框？**
A：站点选择器分别放在 `codey/deepseek.py` 和 `codey/qwen.py`。网页 DOM 变化后需要更新对应驱动并重新跑实机闭环。

**Q：支持哪些网页模型？**
A：目前已实机跑通 DeepSeek Web 和 Qwen Studio。核心 agent 不依赖具体网站；接入 ChatGPT 或 Gemini 时只需新增 provider adapter 和站点驱动。

**Q：我想让 DeepSeek 写更复杂的项目，比如一个完整的 Flask 后端？**
A：可以。建议在「任务」栏说清楚要哪些文件。如果它要看现有代码，会自己 `read` —— Codey 已经处理好多轮循环了。默认最多 50 轮；到上限时 UI 会显示「继续此任务」。

---

## 内部架构

```text
UI / CLI
   ↓
Server / Orchestrator
   ↓
Agent Runtime ── XmlToolCodec
   ↓
ChatProvider ─┬─ DeepSeekWebProvider
              └─ QwenWebProvider
   ↓
Browser Session + provider DOM driver
```

`agent.py` 只认识 `ChatProvider`、`ProtocolCodec` 和 `ToolCall`，不知道 Playwright 页面和站点选择器。`browser.py` 负责通用 Edge/CDP 连接；DeepSeek 和 Qwen 驱动分别处理各自 DOM。两者共用剪贴板保护事务，从网页的“复制回复”动作获取未被 Markdown 渲染破坏的原始 XML。

---

## 项目结构

```
E:\codey\
├── README.md
├── requirements.txt
└── codey\
    ├── __init__.py
    ├── __main__.py        程序入口：python -m codey
    ├── cli.py             命令行调度
    ├── browser.py         通用 Edge/CDP 页面连接器
    ├── deepseek.py        DeepSeek 的输入框、发送按钮、回复提取
    ├── qwen.py            Qwen 的发送、偏好选择、回复提取
    ├── web_clipboard.py   原始回复复制与剪贴板恢复
    ├── agent.py           Provider 无关的 agent runtime 和本地工具
    ├── models.py          ToolCall / ToolPlan / ToolResult
    ├── protocols\
    │   ├── base.py        ProtocolCodec 接口
    │   └── xml_codec.py   XML-only 工具协议
    ├── providers\
    │   ├── base.py        ChatProvider 接口
    │   ├── registry.py    Provider 注册与创建
    │   ├── deepseek_web.py DeepSeek 网页适配器
    │   └── qwen_web.py    Qwen 网页适配器
    ├── server.py          本地 HTTP + SSE 服务（控制面板的后端）
    └── web\
        └── index.html     控制面板前端（单文件，无构建步骤）
```

---

## 进阶：不用网页 UI 也能跑

```powershell
# 单次问一句（不写文件）
python -m codey chat "用一句话介绍 Python 的 GIL"

# 指定 Qwen Studio
python -m codey chat --provider qwen "用一句话介绍 Python 的 GIL"

# 不开 UI，直接跑 agent
python -m codey agent --provider qwen --project E:\my-project --max-turns 10 "你想干的事"
```

输出全部打到终端，方便接 PowerShell 脚本或调试。
