# agent-browser 源码审阅与 Codey 浏览器能力对照

日期：2026-11-09（Asia/Taipei）。本次拉取参考仓库、阅读源码并记录分析，没有修改 Codey 生产代码、配置或测试，没有安装、编译或运行 agent-browser，也没有连接用户正在使用的浏览器。

上游：<https://github.com/vercel-labs/agent-browser/>。
本地：`D:/codey/reference-projects/agent-browser`。
拉取方式：`git clone --depth 1`。
固定提交：`4b0e9f01fa8d214d39bbfe5391257640634fb9a0`。
`package.json` 与 `cli/Cargo.toml` 版本均为 **0.41.2**。以下判断针对该提交，不把旧版本的架构描述套到当前源码。

审阅覆盖普通浏览器命令链、CDP 连接、浏览器生命周期、profile 与状态保存、标签页绑定、快照与 ref、交互与等待、传输重试、MCP 包装和相关测试源码。没有逐行审阅整个仓库，也没有对云浏览器、可选 chat、flow、WebMCP 等全部功能作验收。

## 1. 核心判断

**agent-browser 比 agent-desktop 更直接对应“让 Codey 使用完整浏览器”的需求。** 它持续维护浏览器连接与页面，让调用方模型执行“打开 → 观察 → 操作 → 等待 → 再观察 → 读取”的循环，而非每次只下载一个 URL。

但安装它并不会自动解决知乎 403。对于“我手动能看，agent 却不能看”，首先要确认两者是否使用同一浏览器实例、同一 profile/context、正确标签页，以及相同的登录和校验状态。新开可见浏览器、复制 Cookie、使用相同 session 名称，都不能直接证明这一点。

对 Codey 的建议是：先复用已有 Playwright/CDP 与浏览器工作线程，借鉴 agent-browser 的模型工具和状态语义；外接其 CLI 可以作为独立验证方案。不要为接浏览器再引入第二套模型推理循环。以下所有接入建议均未实施。

## 2. 当前源码实际怎样工作

当前核心是 **Rust 原生 CLI/daemon，通过 CDP 驱动 Chromium**。仓库存在 npm 分发和包装，但普通 Chrome 命令的核心不是旧资料中常见的 Node/Playwright daemon。

```text
Codey / 其他调用方 agent 的既有模型循环
  → 普通 CLI 命令，或 MCP 的具名工具
  → CLI 参数解析与 session 选择
  → 连接该 session 的常驻 daemon
  → DaemonState / BrowserManager
  → CDP WebSocket
  → 已启动或已连接的浏览器及标签页
  → JSON 结果、快照、正文或截图
  → 调用方模型决定下一步
```

主要入口：[main.rs](../../reference-projects/agent-browser/cli/src/main.rs)、[connection.rs](../../reference-projects/agent-browser/cli/src/connection.rs)、[native/daemon.rs](../../reference-projects/agent-browser/cli/src/native/daemon.rs)、[native/actions.rs](../../reference-projects/agent-browser/cli/src/native/actions.rs)、[native/browser.rs](../../reference-projects/agent-browser/cli/src/native/browser.rs)。原生架构也在上游 [AGENTS.md](../../reference-projects/agent-browser/AGENTS.md) 中明确说明。

短命 CLI 进程不是页面状态的主人。命令执行完退出，daemon 仍可保留浏览器、当前标签页和引用状态，下一次命令继续操作。其默认本地启动使用 Chrome for Testing，也支持显式 CDP 连接等其他路径。

普通浏览器命令不要求调用方使用上游的可选 chat 模式。仓库另有 [chat.rs](../../reference-projects/agent-browser/cli/src/chat.rs)，因此也不能笼统地说“整个项目没有模型功能”。对 Codey 有用的是浏览器执行层。

## 3. 怎样用到你已经能打开知乎的浏览器

### 3.1 显式 CDP 连接与自动发现

`BrowserManager` 提供 `connect_cdp`、`connect_cdp_when_listening`、`connect_cdp_with_headers` 与 `connect_auto`。见 [browser.rs](../../reference-projects/agent-browser/cli/src/native/browser.rs) 中这些函数。

`auto_connect_cdp` 的实现见 [cdp/chrome.rs](../../reference-projects/agent-browser/cli/src/native/cdp/chrome.rs)：

1. 遍历已知 Chrome 系浏览器配置目录，寻找 `DevToolsActivePort`。
2. 优先解析文件中对应的 WebSocket 地址。
3. 如果浏览器正在等待远程调试批准，报告等待批准，停止继续探测，避免重复弹出连接请求。
4. 否则尝试常见端口 **9222、9229**；找不到则报错。

这不是对任意已打开浏览器的无条件接管。浏览器必须提供可连接的 CDP，适用时还需在浏览器端批准。普通窗口可见、本机安装了 Chrome/Edge，都不足以证明能连接。

还有一个具体的 Codey 差异：上游 Windows 自动发现的目录列表包括 Chrome、Canary、Chromium 和 Brave，当前列表没有 Edge 默认目录，也没有 Codey 自定义的 `research-edge-profile`。Codey 研究浏览器的首选端口是 **9262**，不是上述两个回退端口。因此若评估外接，应该显式传递 Codey 实际使用的 endpoint，不能直接假设 `--auto-connect` 会找到正确实例。

Codey 的端口与 profile 定义见 [browser_search.py](../../codey/research/browser_search.py)，底层连接与启动逻辑见 [automation/browser.py](../../codey/automation/browser.py)。端口可能因已有实例或冲突而改变，9262 是首选值，不是每次运行的保证值。

### 3.2 session、profile、状态文件不是同一件事

| 机制 | 实际保留/隔离什么 | 对“复用手动能访问的环境”的意义 |
| --- | --- | --- |
| `--session` | daemon 的命令状态、当前页面与相关绑定；本地独立启动时可对应独立浏览器 | 名称相同不能证明网站身份相同；两个 session 接到同一 context 也不会自动隔离 Cookie |
| 显式 `--cdp` | 连接现有浏览器 | 最接近保留正在工作的实例；仍要选对 context 和标签页 |
| `--profile` 传目录路径 | 本地启动使用该 user-data 目录 | 可长期保留浏览器数据；不能同时把被另一进程占用的目录视作可随意复用 |
| `--profile` 传 Chrome 配置名称 | 解析名称，把对应配置复制到临时目录，再启动副本 | 不是连接原窗口，也不是把新变化写回原 profile |
| `state save/load`、`--state`、`--restore` | 保存/恢复选定的网站存储状态 | 便于跨运行复用身份，但不是完整浏览器环境的镜像 |

profile 路径分支在 [chrome.rs](../../reference-projects/agent-browser/cli/src/native/cdp/chrome.rs) 的 `build_chrome_args` 相关实现；名称分支在 `launch_chrome` 的预处理与 `copy_chrome_profile`。不指定 profile 时创建随机临时 user-data 目录。名称副本会跳过部分目录，不能把它称为原 profile 的完全复制。

网站状态文件的数据结构见 [state.rs](../../reference-projects/agent-browser/cli/src/native/state.rs)：Cookie、各 origin 的 localStorage 和 sessionStorage。它没有把 IndexedDB、缓存、Service Worker 等作为这份状态格式的完整组成。`load_state` 在恢复部分 origin 存储失败时返回 skipped origins，也不能只看到命令有响应就认定所有状态已恢复。

同一 context 的 Cookie/localStorage 通常可供多个标签页共享；sessionStorage 有标签页作用域。即便连到了同一浏览器，新标签页也未必拥有成功文章页的一切状态。因此最有说服力的验证是连接正确实例，并读取那张已经可见的文章页；其次才是专用持久 profile 中手动登录后继续使用。

### 3.3 浏览器所有权处理值得直接借鉴

[browser.rs](../../reference-projects/agent-browser/cli/src/native/browser.rs) 的 `close_with_timeout` 只在自身拥有 `browser_process` 时发送 `Browser.close`；外部 `--cdp`/`--auto-connect` 连接只断开，不关闭用户整个浏览器。

相关测试源码是 `test_close_external_manager_disconnects_without_closing_browser`。本次阅读了实现和相关测试，没有执行测试。Codey 已有类似的 attached/owned 区分，接入时应延续同一套所有权规则。

## 4. 它也有 HTTP 路径：最容易误接的地方

**`agent-browser read URL` 并不是在已登录浏览器中打开并读取 URL。**

[actions.rs](../../reference-projects/agent-browser/cli/src/native/actions.rs) 的 `handle_read` 明确分流：

| 调用 | 实际读取路径 |
| --- | --- |
| `read <URL>` | `read::run_read`，独立 HTTP 客户端 |
| 先 `open <URL>`，再普通 `read`，不带 URL | 读取当前页面 HTML，再转换成正文/类 Markdown |
| 不带 URL 的 `read`，但启用 llms 或 require-md 模式 | 仍按当前 URL 重新走 HTTP |
| `snapshot` | 当前浏览器页面的无障碍树 |
| `screenshot` | 当前页面的视觉结果 |

HTTP reader 使用 reqwest，见 [read.rs](../../reference-projects/agent-browser/cli/src/read.rs) 的 `run_read`。当前页读取通过 `handle_content` 获取 HTML；[browser.rs](../../reference-projects/agent-browser/cli/src/native/browser.rs) 的 `get_content` 求值 `document.documentElement.outerHTML`，再交给 `read_json_from_active_html`。

普通当前页读取的输出标记 `source: active-tab-html`，raw 模式为 `active-tab-raw`。这些标记比仅记录“使用了 agent-browser”更能说明数据从哪里来。

对本次知乎问题，如果 Codey 只是把现有 `open_url` 包装成 `agent-browser read URL`，依然可能落回独立 HTTP，原来的环境差异并没有消除。应该把“导航页面”和“读取当前页”定义成明确、不同的工具语义。

同时，outerHTML 也不等于全部可见信息：尚未滚动加载的正文、跨 frame 内容、Shadow DOM 或画布文字，不能一概视为已完整包含。必要时要选定 frame、滚动、重新观察或查看截图，并如实记录实际取得的范围。

## 5. 遇到 403 时，两种产品语义有何不同

agent-browser 的 [browser.rs](../../reference-projects/agent-browser/cli/src/native/browser.rs) `navigate` 调用 `Page.navigate`，检查 CDP 导航错误，按需要等待生命周期，再返回真实 URL、title、targetId。该主路径没有一条“网站 HTTP 状态码 ≥400 就关闭页面”的统一判断。`navigate_active_page` 另有其自身域名过滤器阻止导航的处理，不能混同为网站 403。

所以，网站返回一个可渲染的 403 校验页时，工具仍有机会保留页面，让模型通过 snapshot/read/screenshot 看见页面要求。**导航成功只证明导航操作完成，不证明拿到了文章。** 登录页、校验页、封禁说明也可以成功显示。

Codey 当前 [browser_search.py](../../codey/research/browser_search.py) 的 `_fetch_on_browser_thread` 则在主文档 HTTP ≥400 时 discard 页面并返回失败；检测到需要登录也会 discard。这适合一次性内容抓取，但无法表达“页面仍可观察，等待后续交互或用户在此页完成登录”的状态。

这与此前失败还有一个层次差别：当前 [chat_web.py](../../codey/research/chat_web.py) 的 `fetch` 先调用 `fetch_http_document`，只有 `render_required is True` 才调用浏览器。搜索本身虽然使用浏览器，打开搜索结果未必沿用浏览器。原有四条链接的排查中，失败文章走的是 HTTP 获取，不能据此判定真实浏览器也无法访问。

对知乎，接上正确浏览器后仍可能显示访问限制；源码没有提供必然消除网站校验的保证。对头条脚本页面，持续执行 JavaScript 并观察渲染结果更符合问题本身，但仍需验证正文是否出现。应分别记录：HTTP 状态、页面观察、正文可读性和最终证据状态。

## 6. 模型怎样找到元素并操作页面

### 6.1 无障碍快照与正文分工

[snapshot.rs](../../reference-projects/agent-browser/cli/src/native/snapshot.rs) 的 `take_snapshot` 启用 DOM/Accessibility，通过 `Accessibility.getFullAXTree` 获取语义树，支持 selector 子树、interactive、compact、深度等选项。模型可以用角色、名称和 `@eN` 引用选择下一步动作，不必每次只依赖截图坐标。

[actions.rs](../../reference-projects/agent-browser/cli/src/native/actions.rs) 的 `handle_snapshot` 返回 snapshot、origin、refs、removedRefs，还支持 delta。它在新观察中明确列出消失的引用，可减少模型继续使用旧元素。

这里有一个接口差异：普通 daemon snapshot 的 interactive 默认 false；[mcp.rs](../../reference-projects/agent-browser/cli/src/mcp.rs) 的 `snapshot_command_args` 默认开启 `-i`。因此接 MCP 后，如果不显式请求 `interactive: false` 或另外读取正文，静态文章文字可能被过滤。操作快照不能代替文章证据。

### 6.2 ref 的生命周期有实现，不是随手编的编号

[element.rs](../../reference-projects/agent-browser/cli/src/native/element.rs) 的 `RefMap` 使用递增编号；可持久引用关联 page/frame、CDP session、document loader 和 backend node。`observe_document` 在文档或 frame 身份变化时更换对应映射，导航也会失效相关引用。没有 DOM backend identity 的虚拟节点不能据此获得跨观察的同等稳定性。

同一文档内存活的 DOM 节点可以在后续快照中保持引用；这不等于所有 ref 跨页面、跨 daemon 都有效。它展示的是裸 `@eN`，与 agent-desktop 的 `@<snapshot_id>:eN` 不是同一格式，不能把两套解析和生命周期规则直接混用。

元素解析优先使用 backendNodeId；允许回退的引用可按 role/name 重新查找。但原观察存在同名多元素时拒绝恢复，当前多个匹配时也拒绝。要求精确 backend 节点的引用不会退化为按名称随意选择。相关实现为 `resolve_element_center`、`find_node_id_by_role_name` 和 `select_unique_node_id`。

这套规则仍有工程取舍：按 role/name 恢复可能找到语义相同但已替换的节点，不能称为绝对的原节点证明。Codey 若采用，应明确精确引用和可恢复引用的边界。

### 6.3 操作、等待、结果验证分开

[interaction.rs](../../reference-projects/agent-browser/cli/src/native/interaction.rs) 的普通 click 先解析元素、滚动、计算位置并检查遮挡，再发送输入；frame/OOPIF 有对应 session 处理。fill、键盘、滚动、选项等有各自实现，并非把所有动作都降级为一段任意 JavaScript。

[actions.rs](../../reference-projects/agent-browser/cli/src/native/actions.rs) 的 `handle_wait` 支持文本、selector 及其状态、URL、函数条件、生命周期和单纯延时。`navigate` 也区分新 loader 与同文档导航，避免对 hash 跳转等待一个不会出现的新页面加载事件。

因此 Codey 可以让模型根据观察等待“正文出现”“登录提示消失”等具体条件，不必把所有网站都塞进一次固定 settle 窗口。现有研究 fetch 的总预算为 16 秒、导航 10 秒、settle 4 秒，面向的是抓取动作，不足以承担任意多轮人工登录/页面交互任务。

普通 `clicked` 回执仍不是业务后置条件。点击后是否跳到目标文章、弹出窗口、加载成功，要继续观察。上游对 dialog 有专门结果字段，但不能据此声称每种点击都自动验证最终业务成功。

## 7. 标签页与传输语义，是 Codey 借鉴的重点

### 7.1 固定任务标签页，避免选错页面

[tab_binding.rs](../../reference-projects/agent-browser/cli/src/native/tab_binding.rs) 用 CDP targetId 持久化 session 的标签页绑定，文件可跨 daemon 重启保留；不依赖每次重新列举页面时的“第一个页面”。绑定通过临时文件与 rename 等方式写入，损坏时报告错误。

`--pin-tab` 启用严格绑定并在该 session 内保持。首次 attach 且无绑定时创建新标签页，不自动采用已有页；已绑定页面被关闭时返回 `tab_gone`，不会悄悄改去其他页面；其他 session 对被运行中 owner 固定的页面进行切换会遇到 `tab_locked`。恢复时仍保留显式 tab list/new/switch 等入口。若要读取用户已经打开的文章页，还需显式选择并绑定该页，不能把首次 pin 当成已选中了它。

行为与边界见 [cdp-mode.mdx](../../reference-projects/agent-browser/docs/content/docs/cdp-mode.mdx)、[browser.rs](../../reference-projects/agent-browser/cli/src/native/browser.rs) 的目标绑定方法，以及 [actions.rs](../../reference-projects/agent-browser/cli/src/native/actions.rs) 的 `apply_tab_binding_on_attach_or_rollback` 与标签页切换逻辑。

这不是默认开启的全部行为。上游文档明确说明不 pin 时仍保留部分旧的活动页选择行为，多个 session 可能冲突。固定标签页也不隔离同 context 下的 Cookie、账号和网站数据。

对 Codey，连接身份应至少包含实际 endpoint、context/页面身份、targetId、所有权及本次观察身份；用户切换前台标签页不应该改变任务的目标页。若用户要交接已可读的知乎页，应显式绑定那张页，而不是仅匹配域名后任选一页。

### 7.2 结果不明时，不自动重复点击

[connection.rs](../../reference-projects/agent-browser/cli/src/connection.rs) 的 `exchange` 区分请求没有发出与已发送后未拿到可靠响应；`send_with_retry` 依据命令是否只读决定能否重试。

对 click、fill、eval、导航等有状态动作，请求可能已到达 daemon、响应丢失时，返回 `outcome_unknown`，而不是自动再次执行。只读请求和明确未发出的瞬时错误可以按条件重试。相关测试是 `send_command_does_not_resend_when_response_is_lost`。

这不是端到端 exactly-once 保证，也不证明网站完成了业务处理。它的价值是把“传输失败”和“确定未执行”分开。Codey 的 [tool_spec.py](../../codey/toolchain/tool_spec.py) 已有 `replay_class` 和 `parallel_safe`；新增浏览器动作时应逐项分类，不能因为网页属于 web 工具，就沿用现有读取工具的 safe 重放语义。同页导航与读取也需要串行，防止读到另一动作切换后的页面。

## 8. MCP 接口可以参考，但 Codey 不是配置后就能直接使用

[mcp.rs](../../reference-projects/agent-browser/cli/src/mcp.rs) 提供 open、read、snapshot、click、fill 等具名工具，按 core/debug 等 profile 控制工具目录。它维护工具 schema 和参数映射，通过普通 CLI 路径执行命令，而不是建立一套完全不同的浏览器实现。

`call_cli_tool` 检查参数与 session，`run_cli` 启动当前可执行文件执行对应 CLI 命令，`tool_result_from_run` 保留结构化 JSON、退出码和 stderr，并把失败映射为 MCP `isError`。这也说明 MCP 层仍需处理子进程超时与执行层结果，不能把 MCP 调用结束当成页面任务结束。

Codey 当前 [control/mcp.py](../../codey/control/mcp.py) 是把 Codey 能力向外暴露的 MCP server，不是通用的第三方 MCP client。不能因为 Codey 有 MCP 模块，就说只加一个 agent-browser server 配置便完成接入。

若选择外接 CLI，可以先用参数数组调用并读取 `--json` 结果，不依赖拼接 shell 字符串；若选择 MCP，则还需要 client、会话生命周期与工具目录接入。两者最后都必须进入 Codey 现有取消、工具回执和执行分类机制。

## 9. 对 Codey 的具体借鉴方案

| 方面 | Codey 当前已有 | agent-browser 提供的参考 | 建议 |
| --- | --- | --- | --- |
| 浏览器驱动 | Playwright/CDP、profile、独立工作线程 | 原生 CDP、常驻 daemon | 先复用当前驱动与线程，避免新增第二个浏览器所有者 |
| 模型操作 | 网页搜索与来源读取 | 导航、快照、点击、输入、等待、标签页、截图 | 在现有 agent 循环中增加具名浏览器工具 |
| 登录环境 | Codey 自用 profile，可能连接已有 CDP | 显式 attach、持久 profile、状态文件 | 清楚显示实际连接身份；优先使用确认可读的实例/页 |
| 失败处理 | 一次 fetch 返回正文或失败 | 可继续观察页面并作下一步动作 | 保留可观察的登录/校验页，单独判断正文是否可读 |
| 元素目标 | 现有抓取不向模型提供交互 ref | AX 快照、document/frame/node 引用 | 引入有作用域、可失效的元素引用和明确 stale 错误 |
| 并发与恢复 | 工作线程、取消、重放分类 | target pin、tab_gone、outcome_unknown | 同页串行，关闭/丢失后显式恢复，不猜目标、不重复动作 |
| 来源证据 | source ledger、阅读窗口、回执 | 当前页 HTML/快照/截图 | 浏览器读取结果继续进入现有证据主干 |

Codey 可复用的实现入口还包括 [browser_worker.py](../../codey/automation/browser_worker.py)、[source_gateway.py](../../codey/research/source_gateway.py)、[research/tools.py](../../codey/research/tools.py) 和 [task_execution.py](../../codey/operations/task_execution.py)。

建议最小工具面为：连接/选择会话、导航、页面观察、点击、输入、条件等待、读取当前页、标签页操作与截图。命名只是建议，不表示这些工具已在 Codey 中落地。截图或任意 evaluate 是否默认暴露，应依据实际任务需要决定，不必一次搬入上游全部命令。

读取当前页时，继续记录最终 URL、title、页面与观察身份、提取方法、正文及截断范围；取得正文后再写入既有 source/read-window 体系。`open` 成功、snapshot 有按钮、截图非空，都不能单独变成“文章已读”的来源证据。

## 10. 两个参考项目的分工，以及下一步应验证什么

| 能力 | agent-browser | agent-desktop |
| --- | --- | --- |
| Chromium 页面 DOM/AX、frame、标签页、网页输入 | 更直接 | 不是网页专用驱动层 |
| 复用网站登录环境 | 通过正确 CDP 实例/context 或持久 profile | 操作已打开窗口仍需正确识别窗口与页面 |
| 浏览器原生菜单、系统文件对话框等 | CDP 的覆盖范围有限 | 通过操作系统无障碍接口补充 |
| 对 Codey 的共同价值 | 持续观察与操作、目标身份、动作结果边界 | 持续观察与操作、目标身份、动作结果边界 |

前一份对照报告见 [agent-desktop 源码审阅](agent-desktop-source-review-20261109.zh-CN.md)。两者可以互补；为解决当前网页获取问题，浏览器专用层应优先。

后续若进入实现或实验阶段，应分开验证三件事：

1. **连接一致性**：能否连接实际可读知乎的浏览器，并绑定正确文章页；连接不可用时是否明确失败。
2. **读取一致性**：当前页读出的是否是屏幕上可见的文章正文，是否误走 `read URL` 的 HTTP 路径，是否只获得校验页或交互树。
3. **恢复一致性**：导航、用户关页、daemon/连接中断、响应丢失后，是否保留正确目标与结果语义，并进入 Codey 原有证据回执。

本次没有执行上述浏览器实验；上游相关测试源码支持理解其设计，不构成本机验证。拉取与报告只为提供可审阅的依据，不能据此宣称已解决知乎访问或已完成 Codey 接入。
