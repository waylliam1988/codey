# Codey · 让网页版 DeepSeek 帮你在本地写代码

不需要 API key，不需要充值。只要你能在浏览器里登录 DeepSeek，Codey 就能把它当成一个能读你硬盘、改你代码的本地编程助手。

---

## 这是什么？

Codey 启动后会做三件事：

1. 开一个**专属的 Edge 浏览器窗口**（跟你日常用的 Edge 完全分开，互不干扰），自动打开 `chat.deepseek.com`。
2. 开一个本地网页 `http://127.0.0.1:5173/`，是 Codey 的控制面板。
3. 你在 Codey 控制面板里用大白话描述要做什么，它会把指令发给 Edge 里的 DeepSeek，DeepSeek 回什么代码、Codey 就在你的项目目录里**真的把文件写出来**。

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

### 4. 第一次：在弹出的 Edge 窗口里登录 DeepSeek

第一次点「运行」时，会弹一个**新的** Edge 窗口，里面是 `chat.deepseek.com`。

⚠️ 这个 Edge 跟你平常用的 Edge 不是同一个 profile —— 它用的是 `C:\Users\<你>\.codey\edge-profile`，登录信息只存这里，跟你的日用 Edge 互不污染。

**手动在那个新 Edge 窗口里登录一次 DeepSeek**（手机号也好、微信也好），然后**不用关掉**，回到 Codey 控制面板。

> 以后再用都不用登录了 —— 这个 profile 会一直记住登录态。

### 5. 看 Codey 干活

回到控制面板，下方会**实时**显示：

- DeepSeek 每一轮回复的原文（灰色方块）
- Codey 实际执行的工具调用（紫色 `write` / `read` / `ls` 标签）
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

Codey 教 DeepSeek 用 4 个工具，每个都是一段带魔法注释的代码块。**你不用记**，DeepSeek 自己会用：

| 工具 | 作用 |
|------|------|
| `write path=foo.py` | 把整个代码体写进 `foo.py`（覆盖） |
| `read path=foo.py`  | 让 DeepSeek 读这个文件 |
| `ls path=.`         | 列目录内容 |
| `done` / `continue` | 告诉 Codey 任务结束 / 还要再来一轮 |

⚠️ **目前没有 shell 工具** —— Codey 不会替你执行 `pip install` 之类的命令。需要装的依赖你手动装。这是有意的安全设计。

---

## 常见问题

**Q：弹出的 Edge 窗口我能关掉吗？**
A：Codey 进程还在跑的时候不要关，关了就断连了。任务结束、退出 Codey 之后关就行。

**Q：可以同时开自己的 Edge 吗？**
A：可以。Codey 用的是独立 profile，跟你日用 Edge 是两个进程，谁也不影响谁。

**Q：DeepSeek 网页改版了，Codey 报错找不到输入框？**
A：选择器写在 `codey/deepseek.py` 顶部三行：`INPUT` / `SEND_READY` / `RESPONSE`。F12 看一下页面 DOM 改一改就行。

**Q：能换成 ChatGPT / Qwen / Gemini 吗？**
A：理论上能。你只需要写一份对应的 `xxx.py`（参照 `deepseek.py` 写选择器和文本提取），然后在 `browser.py` 里换 URL。但**目前只跑通了 DeepSeek**。

**Q：我想让 DeepSeek 写更复杂的项目，比如一个完整的 Flask 后端？**
A：可以。建议在「任务」栏说清楚要哪些文件。如果它要看现有代码，会自己 `read` —— Codey 已经处理好多轮循环了。默认最多 12 轮，不够的话改 `max_turns`。

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
    ├── browser.py         启动并连接 Edge（CDP）
    ├── deepseek.py        DeepSeek 的输入框、发送按钮、回复提取
    ├── agent.py           工具协议解析 + 多轮循环
    ├── server.py          本地 HTTP + SSE 服务（控制面板的后端）
    └── web\
        └── index.html     控制面板前端（单文件，无构建步骤）
```

总共 7 个 Python 文件 + 1 个 HTML，没有别的。改起来一目了然。

---

## 进阶：不用网页 UI 也能跑

```powershell
# 单次问一句（不写文件）
python -m codey chat "用一句话介绍 Python 的 GIL"

# 不开 UI，直接跑 agent
python -m codey agent --project E:\my-project --max-turns 10 "你想干的事"
```

输出全部打到终端，方便接 PowerShell 脚本或调试。
