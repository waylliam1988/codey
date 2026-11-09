# agent-desktop 源码审阅与 Codey 浏览器能力对照

日期：2026-11-09（Asia/Taipei）。本次只拉取参考仓库、阅读源码并记录分析，没有修改 Codey 的生产代码、配置或现有测试，没有安装或运行 agent-desktop，也没有操作用户正在使用的浏览器。

参考仓库：<https://github.com/vercel-labs/agent-desktop/>。
本地位置：`D:/codey/reference-projects/agent-desktop`。
以 `git clone --depth 1` 拉取，固定提交为 `b43ac4f6e892ef608f8851428613c4009f0e84c7`，工作树干净，workspace 版本为 `1.2.0`。项目自己的协议输出版本 `2.6` 与包版本是不同概念。

审阅覆盖 CLI 分发、Windows UI Automation 实现、快照和元素引用、动作验证和重试、会话存储、MCP 接口，以及直接相关的测试源码；没有逐行审阅整个仓库，也没有运行其平台验收。

## 判断

用户提出的“让 agent 使用一个完整、保留会话的真实浏览器”是可行的方向。对知乎这类网站，关键是 agent 能观察和操作用户已经能正常访问的浏览器环境，而不只是换一种 HTTP 下载器。

agent-desktop 对这个方向最有价值的贡献是：让调用方 agent 自己运行“观察 → 操作 → 再观察”的循环；操作对象具有明确的进程、窗口和快照身份；工具报告动作是否发出、是否验证，以及是否允许重试。

它通过操作系统无障碍接口控制桌面应用，包括浏览器窗口，但没有实现完整的网页自动化专用层。源码还明确把 Chromium 网页内容操作交接给 agent-browser 或其他 CDP 客户端。对 Codey 的网页任务，建议首先复用现有 Playwright/CDP，补齐模型可调用的浏览器交互和会话绑定；桌面菜单、系统对话框和无 CDP 浏览器窗口再考虑 agent-desktop。

## 1. 实际调用链

```text
调用方 agent 的现有循环
  → CLI 或 MCP 工具调用
  → 参数解析、命令策略、CommandContext
  → 同一个 dispatch
  → core 中各命令的 execute
  → PlatformAdapter
  → Windows UI Automation / macOS AX / Linux AT-SPI
  → JSON 观察结果或动作回执
  → 调用方 agent 决定下一步
```

入口见 [src/main.rs](../../reference-projects/agent-desktop/src/main.rs)、[dispatch/mod.rs](../../reference-projects/agent-desktop/src/dispatch/mod.rs) 和 [core/adapter/mod.rs](../../reference-projects/agent-desktop/crates/core/src/adapter/mod.rs)。

项目没有在桌面命令内部嵌入模型，也不替调用方另建一个推理循环。Windows 使用 `CUIAutomation8`，设置 connection/transaction timeout；见 [windows/tree/automation.rs](../../reference-projects/agent-desktop/crates/windows/src/tree/automation.rs)。这意味着接入时可以保留 Codey 现有 kernel、取消机制和工具回执主干。

## 2. 怎样观察和操作界面

### 快照是可操作的界面状态

`snapshot` 输出角色、名称、状态、可用动作和元素 ref，不需要模型先识别截图像素。截图是另外的能力。

它提供 `--skeleton` 浅层概览、`--root` 局部展开、`--compact` 和 `-i` 交互元素过滤。源码中 skeleton 把展示深度限制在最多 3 层；输出还携带 `complete`、`truncated` 和 `nodes_observed`，不会把部分观察表示成完整观察。

依据：[commands/snapshot.rs](../../reference-projects/agent-desktop/crates/core/src/commands/snapshot.rs)、[snapshot/mod.rs](../../reference-projects/agent-desktop/crates/core/src/snapshot/mod.rs)。

对网页阅读尤其要注意：`-i` 会过滤静态文字。Codey 如果只给模型交互树，文章正文可能消失。应同时支持精简的操作快照和明确范围的正文读取，并把正文交付范围保存在现有来源回执中。

### ref 绑定观察身份，执行前再次确认

输入 ref 必须是 `@<snapshot_id>:e<N>`，裸的 `@e1` 会被拒绝。解析实现见 [refs/ref_token.rs](../../reference-projects/agent-desktop/crates/core/src/refs/ref_token.rs)。

动作前重新确认目标。对 Windows/Linux 的非 retained-token 引用，捕获时必须证明元素身份在完整窗口观察中唯一，使用时再确认只有一个匹配项。身份依据包括角色及记录下来的稳定 ID、名称和描述；屏幕坐标和控件值不作为身份。

不存在返回 `STALE_REF`；多个匹配返回 `AMBIGUOUS_TARGET`；观察不完整、无法证明身份时不猜测。即使只是 skeleton 展示，也不能把未展开区域中的重复控件忽略掉。

依据：[refs/resolve_saved_ref.rs](../../reference-projects/agent-desktop/crates/core/src/refs/resolve_saved_ref.rs)、[refs/ref_capture_proof.rs](../../reference-projects/agent-desktop/crates/core/src/refs/ref_capture_proof.rs)。

Codey 可以借鉴这套身份约束，但浏览器端应利用自己的 page/document/frame/element 身份，不必照搬整棵 Windows UIA 树的匹配成本。导航后不能让旧元素引用误指向新页面。

### 动作完成与任务完成分开

Windows 的语义 click 使用 Invoke、Legacy default action 等能力；控件值、选择和展开状态走相应 UIA pattern。不会默认把每次动作都变成鼠标坐标点击。

状态动作通过 [actions/post_action.rs](../../reference-projects/agent-desktop/crates/core/src/actions/post_action.rs) 执行一次，再读取实时状态核验。普通 click 没有通用业务后置条件时，不会自动声称业务已完成。

同样，“输入框值已经变化”也不会被报告成“应用已保存”：回执明确保留 `application_commit: not_verified`。Codey 的“点击搜索”“页面打开”“已读取文章”“答案有依据”也应分别记录。

## 3. 重试为什么比一个 ok 布尔值更精确

[DeliverySemantics](../../reference-projects/agent-desktop/crates/core/src/actions/delivery_semantics.rs) 区分：

| 状态 | 含义 | 自动重试含义 |
|---|---|---|
| `not_delivered` | 目标动作未发出 | `safe` |
| `delivery_uncertain` | 可能已经发出 | `unsafe` |
| `delivered_unverified` | 已发出，结果尚未核验 | `unsafe` |
| `delivered_verified` | 已发出且验证了相应后置状态 | `unsafe` |
| `unknown` | 缺少足够投递信息 | `unknown` |

它的自动等待会在预算内重新检查可操作性，但只有确认未投递的可恢复定位失败才重新尝试；见 [ref_action_wait.rs](../../reference-projects/agent-desktop/crates/core/src/ref_action/ref_action_wait.rs)。

相关测试 `dispatch_failure_is_returned_without_repeating_the_action` 明确断言 dispatch 仅执行一次；见 [ref_action_exactly_once_tests.rs](../../reference-projects/agent-desktop/crates/core/src/ref_action/ref_action_exactly_once_tests.rs)。这是本次阅读到的测试断言，不代表本机已运行通过。

对 Codey 的实际意义：未来增加 click/type 等工具后，不能沿用“网页工具都是读取，因此可安全重放”的笼统分类。一个返回超时的提交操作可能已发生。已有 [ToolSpec](../../codey/toolchain/tool_spec.py) 的 `replay_class`、`parallel_safe` 与持久回执需要承接这种差异，同一标签页中的导航和动作还应按序执行。

## 4. 两个容易混淆的概念

### agent-desktop 的 headless 不等于无头浏览器

它所说的默认 headless，主要指语义操作不移动鼠标、不抢前台焦点。被操作的桌面浏览器可以本来就开着，Cookie 和登录状态仍由那个浏览器维护。

Windows 上部分动作仍需要 `--headed`，例如物理输入和聚焦。相关限制见 [Windows 平台指南](../../reference-projects/agent-desktop/skills/agent-desktop-windows/SKILL.md)。不能据此认为它能在任意后台服务、锁定桌面环境里完整代替人工操作。

### agent-desktop 的 session 不等于网站登录会话

session 管理的是快照引用命名空间、trace、artifact 和调用上下文。`RefStore::for_session` 把状态放到所选 session 目录；指定快照也不能跨 namespace 查找。

依据：[session/mod.rs](../../reference-projects/agent-desktop/crates/core/src/session/mod.rs)、[refs/refs_store.rs](../../reference-projects/agent-desktop/crates/core/src/refs/refs_store.rs)、[refs_store_session_tests.rs](../../reference-projects/agent-desktop/crates/core/src/refs/refs_store_session_tests.rs)。

它没有替浏览器创建知乎登录态，也没有靠 session ID 自动复制用户 Cookie。能沿用登录态，是因为它操作的是已有的那个应用。

Codey 需要分别表达：聊天/任务身份、浏览器连接与 profile、context、标签页、当前 document、观察快照。一个会话 ID 不能代替这些身份。

## 5. 它怎样与网页自动化交接

[commands/launch.rs](../../reference-projects/agent-desktop/crates/core/src/commands/launch.rs) 有实际 CDP 支持：

1. `launch --cdp` 配置 remote-debugging 端口和 loopback 地址。
2. 启动后探测 DevTools endpoint，并核实监听端口属于预期进程实例。
3. 返回 endpoint，建议调用 `agent-browser connect <port>`，或使用 Playwright/Puppeteer。
4. 原生菜单、窗口和系统对话框继续由 agent-desktop 操作。

对于已经运行但没有 DevTools 端口的应用，该路径返回 `cdp_requires_fresh_launch`，并提示使用现有窗口的 accessibility 命令，或另行重启启用 CDP。它并不能凭空给任何已打开的浏览器加上 CDP 连接。

因此，用户“我已经能打开知乎”的事实支持两条实际路线：

- 直接操作那个已登录窗口：UIA 路线可以不要求浏览器预先开放 CDP，但要验证该窗口实际暴露的树和动作能力。
- 操作明确配置的持久浏览器：通过 Playwright/CDP 驱动，在该 profile 中完成一次正常登录或验证，后续持续使用同一 context。

两条路线都需要确认 agent 操作的确实是预期窗口或 context。仅仅换成同品牌浏览器，或新建临时 context，不会自动继承原来的访问状态。

## 6. Windows 的空树与等待经验

agent-desktop 也会遇到“界面看得到，工具暂时读不到”。其 Windows Chromium 适配识别 `Chrome_WidgetWin_1`，区分完整深度观察下的薄树，与人为深度裁剪导致的浅树。

依据：[windows/tree/chromium.rs](../../reference-projects/agent-desktop/crates/windows/src/tree/chromium.rs)。共享层对 renderer accessibility 激活使用有界退避观察；见 [tree/renderer_accessibility.rs](../../reference-projects/agent-desktop/crates/core/src/tree/renderer_accessibility.rs)。

平台指南记录冷启动可能需要 10–25 秒才能得到完整树，也说明遮挡、深度裁剪和首次连接缓存可能造成不同症状。这些是上游文档和源码的经验，本次没有在本机复测，不能直接当成 Codey 所有网页的等待参数。

值得借鉴的是按观察状态决定下一步：加载中、观察被裁剪、登录页、校验页、定位失效各自表达；不要都压成 `page_blank`。用户处理验证后应重新观察同一页面，而不是立即丢弃页面与会话。

## 7. CLI、MCP 共用实现，但 Codey 不能只加一段 MCP 配置

[mcp/catalog.rs](../../reference-projects/agent-desktop/src/mcp/catalog.rs) 从 clap 命令树生成 MCP 工具名和 schema；[mcp/call.rs](../../reference-projects/agent-desktop/src/mcp/call.rs) 把工具参数构造为 argv，运行同一个二进制，再把 JSON envelope 转成 MCP 输出。MCP 没有另写一套桌面执行逻辑。

Codey 可以借鉴“工具定义只有一份、不同入口复用同一执行与回执”。现有 ToolSpec 和 kernel 应继续承担模型工具身份、权限、取消与恢复。

本次检查到的 [codey/control/mcp.py](../../codey/control/mcp.py) 是把 Codey 暴露给外部调用者的 MCP server；没有在 Codey 生产树中找到可直接加载任意第三方 MCP server 的通用 client。因此不能把“上游提供 MCP”写成“在 Codey 添加配置就能使用”。

## 8. Codey 当前已有的基础与缺口

| 项目 | 当前源码表现 | 对完整浏览器的影响 |
|---|---|---|
| 搜索 | ChatWebProvider 使用 headed、非 isolated 的 BrowserSearchProvider | 已经不是 RSS 搜索 |
| 正文入口 | HTTP 优先，只在 `render_required=True` 时渲染 | 普通 403、脚本空页不会交给浏览器 |
| 浏览器基础 | Playwright/CDP、持久 profile、BrowserWorker 已有 | 可以复用，不必重建浏览器栈 |
| profile 与目标 | 默认启动使用 `research-edge-profile`；也会搜索可复用 CDP endpoint | 不等于已明确绑定用户手动浏览器的 profile/context/tab |
| 页面失败处理 | 浏览器导航响应 >=400 时立即丢弃读取页；识别登录后也丢弃 | 模型没有留下来观察、等待处理后继续的交互路径 |
| 模型工具 | web_search、open_url、source_search 等来源工具 | 没有完整 navigate/snapshot/click/type/scroll/wait 标签页操作面 |
| 来源证据 | SourceGateway、ledger、正文窗口和工具回执已有 | 浏览器观察所得正文可以继续接入原主干 |

依据：[chat_web.py](../../codey/research/chat_web.py)、[browser_search.py](../../codey/research/browser_search.py)、[automation/browser.py](../../codey/automation/browser.py)、[browser_worker.py](../../codey/automation/browser_worker.py)、[tool_spec.py](../../codey/toolchain/tool_spec.py)、[source_gateway.py](../../codey/research/source_gateway.py)、[research/tools.py](../../codey/research/tools.py)。

本会话前一轮同机实测还提供了具体证据：头条文章的 HTTP 可见文字为零，但直接调用现有浏览器 fetch 在 4.66 秒返回约 3722 字符。知乎在无既有会话的临时浏览器中仍返回访问限制。因此头条已证明有可用获取能力未被普通聊天调用；知乎仍需验证预期的真实会话，而不能承诺切换引擎必然成功。

## 9. 建议的实现方向，尚未实施

第一步应把普通网页搜索和正文访问放进同一个明确绑定的持久浏览器环境。对一般 HTML 页面，浏览器导航可以直接成为主要路径；PDF、文件下载保留专门下载器。这个方向无需恢复 RSS，也不依赖逐站添加 User-Agent 或页面结构补丁。

第二步给现有 agent 循环提供完整但精简的交互能力，例如连接/选择标签页、导航、观察快照、元素操作、滚动、等待、正文读取。它们应注册为现有 ToolSpec，通过现有 kernel 执行。本文列举的是能力边界，具体名称与参数尚未设计或实现。

第三步让目标与观察都有身份：浏览器连接、context、page、document generation、snapshot、元素 ref。登录或验证属于实际页面状态；保留页面，允许用户在同一浏览器处理后继续观察。HTTP 403 可以作为诊断信息，但不能独自替代浏览器最终可观察状态。持续不可用时仍应如实报告。

第四步补充动作投递与验证事实，复用现有 ToolResult canonical 和持久回执。输入或点击之后先观察实际结果；恢复时读取已经提交的回执，不自动重复不确定动作。网页内容仍作为不可信来源输入，UI 成功与研究证据分别投影。

第五步再决定是否接入 agent-desktop：优先覆盖原生文件对话框、菜单、无 CDP 的已有桌面窗口。若直接复用外部程序，CLI 子进程可作为评估入口；长期接入仍需转换为现有工具回执。没有证据要求把 Codey 全部网页访问改成 UIA。

## 10. 后续验收应证明什么

- 同一篇知乎文章：用户能读取的目标窗口/context 与 agent 实际观察到的是同一个；记录登录/验证状态及真实正文，不用临时浏览器结果代替。
- 同一篇头条：真实浏览器路径获取正文，保留状态转换和正文交付范围。
- 校验与登录中断：保留目标标签页，处理后继续观察；明确区分仍受限和已读正文。
- 页面导航与重复控件：旧 ref 不误点，新快照能重新定位；歧义时反馈具体原因。
- 操作后超时：不盲目重复点击或输入；恢复后核对实际页面状态。
- 长文与懒加载：模型能滚动、定位、继续阅读，并给出已实际读取的来源链接；工具摘要和正文证据分别核对。
- 普通静态网页和 PDF：保留可用性，记录耗时、模型轮数与工具调用成本，避免把更多操作本身当成收益。

本报告完成的是源码分析和设计对照。尚未安装运行 agent-desktop、接入新的浏览器工具、修改生产获取策略，也没有证明其在当前用户会话中能够自动读取知乎。
