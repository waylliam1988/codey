# Codey 开放网页操作能力差距分析

日期：2026-11-21（Asia/Taipei）。范围：生产浏览器执行、模型工具表面、观察与恢复、来源证据管线；对照工作区 Codex、agent-browser 源码及本机 Codex 安装。未修改生产实现。

## 0. 目标修正：统一任务中的共享能力

用户进一步明确：查资料、研究、操作浏览器、写代码与运行验证，应当能够在同一任务中自然往返，不需要切换一个浏览器模式或先选择 Hybrid。本文所述“常规浏览器入口”仅指当前实现中模型可调用的路径，不是建议保留这种产品分区。

目标应为一个任务执行循环，共享项目文件、浏览器会话、来源与观察记录、工具回执和用户协助状态。研究是完成任务时的活动及证据质量要求；浏览器是所有已授权任务可以组合使用的能力。最终交付可以同时包含代码修改、测试结果、来源说明和报告，不需要先完成独立研究任务再转入代码任务。

代码事实需要区分：默认 project/hybrid/chat/research 已共享浏览器授权，`_entry_executors` 也能在同一入口构建项目与 ResearchTools 资源，`BrowserSession` 复用来源采集 provider。因此当前不是所有能力完全隔离；仍然存在的是 task_kind、strict_research 与工具展示、工具白名单、交接和完成合同的耦合。

补充探针只改变 task_kind / strict_research，保持相同的项目读写、验证与浏览器授权，调用生产 `build_turn_snapshot` 与 `offer_browser_tools`：

| 任务类型 | 项目 read_file/edit/run 仍在工具 inventory | 浏览器叶子数 | 调用通用 offer 后出现的直接浏览器工具 |
| --- | --- | --- | --- |
| project | 是 | 13 | 0 |
| hybrid | 是 | 13 | 5 |
| 严格 research | 否 | 13 | 0 |

这是工具快照层面的对照，不是完整入口的模型实验。严格 research 的项目工具丢失来自 controller 白名单；project 与 hybrid 的直接工具展示差异来自 offer 函数的任务类型条件。研究另有专门交接，所以表中的 0 不表示研究不能操作浏览器。

下一步架构调整优先于按网站加功能：

1. 统一工具规划基础：已授权工具和当前资源共同决定能力集合，不因模型当前在研究、编码或页面操作而撤销已授权能力。观察到的 ref 等真实前提仍需满足。
2. 将研究要求与工具权限解耦：引用、来源读取、报告结构属于交付要求；不以研究阶段白名单排除项目编辑或验证。研究本身也不自动授予未经用户授权的修改能力。
3. 统一任务资源生命周期：在浏览器与代码操作之间转换时，保留原页面、来源与项目修改上下文；只有任务结束、取消或明确资源释放时清理。
4. 同一执行器完成所有动作：浏览器恢复和取证机制服务于整个任务，复用现有身份、账本和授权，不另起一套浏览器 agent。
5. 以跨能力完整任务验收：读项目 → 在线查文档 → 浏览器验证细节 → 修改代码 → 跑测试 → 回到浏览器检查 → 补充研究 → 最终交付。无需用户补工具名称、切模式或选择固定中间动作序列。

相关位置：[默认共享网页授权](../../codey/policies/task_policy.py#L175)、[共同构建执行资源](../../codey/operations/task_entry.py#L193)、[研究工具白名单](../../codey/operations/kernel_protocol.py#L247)、[直接浏览器工具展示条件](../../codey/operations/kernel_protocol.py#L199)、[按研究 scope 分支的交接](../../codey/operations/kernel_context.py#L395)。

## 1. 结论与证据边界

Codey 已有真实 Chromium、点击、填写、导航、页面绑定、来源账本和 CodeMode。当前差距在于：模型得到的网页观察有缺失，常用操作原语不全，部分交互恢复被查询证据状态锁住，并且工具展示与确认路径增加了决策负担。

“底层能执行 click”与“在未知网站可靠完成任务”之间，还缺少足够通用的观察、动作、反馈和恢复闭环。本轮有两个真实 Chromium 夹具不经过模型，直接复现了生产方法的限制，因此这些问题不能全部归因于本地模型。

本轮没有对两个模型做同输入 A/B，也没有跑完整生产任务入口，因此不声称已经量化了模型能力与运行环境各自贡献。

## 2. 我上轮实际依赖的能力

用户看到的 `{"action":"click",...}` 是临时 Python 观察脚本接收的 JSON，不是本会话原生的独立 browser.click 工具。

完整链条为：通用命令执行启动 Python → Playwright 创建可见浏览器 → 常驻进程通过 write_stdin 接收命令 → 保存 DOM、截图和生产快照 → view_image 把截图送入当前模型 → 修改下一步操作。

我同时使用了：

- 检查网页实际 DOM 属性、class 与位置；必要时使用观察到的 CSS selector。
- 查看截图，识别生产快照没有列出的关闭图标。
- Playwright `locator.click()`、`fill()`、`press('Enter')`，以及任意必要的只读诊断代码。
- 跨工具调用保持同一个 Python 进程、浏览器 context 和 page。
- 操作失败后改变方法，而不是要求先完成一个预先定义好的查询结果证据流程。

这与 Codey 只通过当前常规浏览器表面执行的条件并不相同。截图是实际送入模型的图像；只在用户电脑上打开窗口，并不会自动让模型获得同样的视觉输入。

上轮各条 Running 文案来自同一常驻进程的输入。脚本内 click 超时设置为 5000ms，普通动作后另外等待 2500ms；不能仅凭界面上的“3m 50s”认定每次点击都执行了这么久。计时展示的具体归属未另作 UI 源码诊断。

## 3. 对照了哪些 Codex 实现

- 本机 `codex --version`：`codex-cli 0.171.0-alpha.13`。
- 本机扩展 `openai.chatgpt`：版本 `26.1117.41037`；安装包包括编译后的 codex.exe 和打包 extension.js。仅据安装包不能重建全部运行时源代码。
- 工作区 `reference-projects/codex`：提交 `970111f6cb`，提交日期 2026-10-23。它不是已验证与本机二进制完全一致的源码。
- 工作区 `reference-projects/agent-browser`：提交 `4b0e9f0`，提交日期 2026-11-07。

Codex 参考源码中，unified_exec 管理常驻进程与 stdin 续接；view_image 返回真正的图像内容块。这能解释我本轮使用的通用执行、会话保持和看图能力，但不能证明当前本机安装包内置一个同名网站点击工具。

参考代码：

- [进程会话身份](../../reference-projects/codex/codex-rs/core/src/tools/handlers/unified_exec.rs#L28)
- [stdin 续接](../../reference-projects/codex/codex-rs/core/src/tools/handlers/unified_exec/write_stdin.rs#L69)
- [图片作为工具输出](../../reference-projects/codex/codex-rs/core/src/tools/handlers/view_image.rs#L240)

agent-browser 更直接提供浏览器执行方面的参考：AX 树、DOM 补充可点击元素、按区域观察、原生按键/悬停/选择、iframe 会话和点击遮挡反馈。这里没有宣称安装 agent-browser 就能解决模型决策、网站登录或证据质量。

## 4. 无模型参与的机械复现

使用真实 headless Edge、生产 BrowserSession 和生产点击准入方法。页面由本地脚本 set_content 安装，无外部网站及模型决策。

复现命令：

```powershell
python -u .e2e-artifacts/browser-open-world-audit-20261121/probe.py
```

结果：[results.json](../../.e2e-artifacts/browser-open-world-audit-20261121/results.json)。

### A. 页面已经出现结果，查询确认仍失败

页面用普通按钮查询，结果写入普通 `<div id="output">`。没有 aria-controls、article、role=region 或 aria-live。

- 生产点击成功，delivery=delivered。
- 页面实际出现 `Current inventory: 417 units.`。
- 生产 browser.capture 返回 status=ok，文本包含该结果。
- 查询确认的 baseline.regions=[]。
- 生产 capture_query_result 返回 `query_update_unconfirmed`。

原因是结果区域默认只观察 article、role=status、role=region、aria-live，显式控制关系主要依赖原生 details 或单个 aria-controls。普通网站不一定采用这些标记。

这不等于结果应该未经核实自动变成正确业务答案，而是说明：需要一个可以继续检查并选定真实结果区域的通道，不能让“没有预期 DOM 语义”变成永远不能确认。

代码：[结果区域与确认](../../codey/automation/browser_session.py#L43)、[显式控制关系](../../codey/automation/browser_query.py#L14)。

### B. 查询点击被遮罩拦截后，关闭遮罩也被拒绝

页面包含查询按钮、结果 section 和覆盖页面的登录层；登录层有一个普通、可命名的 Close login 按钮，避免把失败归于图标漏检。

- 查询按钮点击超时，错误明确包含 pointer interception。
- 生产方法统一分类为 delivery=uncertain。
- 按真实回执结构保存 query_baseline 后，重新 snapshot 可观察到 Close login。
- pending_click 仍存在。
- 执行常规 browser_click 关闭遮罩，被准入逻辑直接拒绝为 not_delivered，提示先观察；但刚才已经观察。

这证明该查询状态下常规路径缺少有效的恢复出口。更强模型可能改用 browser_eval 等路径绕过，但不会因此让这一常规工具路径本身变得正确。

代码：[统一 uncertain 分类](../../codey/automation/browser_session.py#L379)、[后续点击准入](../../codey/operations/task_execution.py#L321)、[pending 查询投影](../../codey/operations/browser_followups.py#L185)。

## 5. 其他明确差距

### 观察能力与实际浏览器不对等

上轮小红书实页里，关闭图标未进入控件快照，遮罩后的搜索框却被当作普通控件；七次观察全部达到 80 控件上限。将真实首屏快照交给 BrowserReferences.observe，实际返回 `Control binding requires complete observed controls and contexts.`。

截断不会让所有原始 ref 失效，但会让现有按 role/name/context 选择的描述式绑定不可用。一张列表较长的正常网页就可能失去这一便利，且盲目提高 max_elements 会被现有 80 上限截住。

快照主要是 main-frame 文本和有限命名控件。需要补充前景层、作用区域、控件原始属性、可点击性和可选视觉观察。Codey 浏览器回执当前未提供截图图像通路；ProviderToolResult.content 是字符串，增加一个“截图路径”不等于把图片交给模型。

对照：[Codey 快照](../../codey/automation/browser_session.py#L295)、[描述式绑定](../../codey/operations/browser_references.py#L58)、[工具回执](../../codey/providers/base.py#L61)、[agent-browser AX 树与 DOM 补充](../../reference-projects/agent-browser/cli/src/native/snapshot.rs#L430)。

### 现有 browser_eval 不能替代完整原生操作

生产工具清单有 navigate、snapshot、fill、click、read、scroll、wait、eval、tabs、upload、download；没有独立 press、hover、select、screenshot 或 frame 切换入口。browser_click 主要接受已观察 ref，snapshot 不接受区域参数。

browser_eval 是 page.evaluate，即网页内 JavaScript；它不是一个拥有 Playwright Page 的脚本环境。DOM 合成键盘事件不等同真实按键，跨域 frame 与原生文件/浏览器交互也有边界。

scroll 目前使用 window.scrollBy，不能直接指定笔记评论面板等内部滚动容器。这些问题可能通过特定 eval 处理，但要求模型额外发现并编写代码。

对照：[实际工具盘点](../../.e2e-artifacts/browser-open-world-audit-20261121/inventory-and-binding.json)、[agent-browser 按键](../../reference-projects/agent-browser/cli/src/native/interaction.rs#L346)、[下拉选择](../../reference-projects/agent-browser/cli/src/native/interaction.rs#L461)、[遮挡反馈](../../reference-projects/agent-browser/cli/src/native/element.rs#L1115)。

### 模型接口与任务过程有额外负担

本轮构造普通 Chat 的真实初始工具快照，外层有 codemode、web_search、open_url 等，浏览器叶子在嵌套目录；offer_browser_tools 后才出现直接 snapshot、fill、click、read、wait。

直接动作仍可能降为 CodeMode，带自动跟进；click 的 purpose 又区分 action、controls、result，涉及不同确认语义。底层共享执行器有合理性，但模型需要理解的接口与失败语义应尽量稳定。

不能仅凭这些层次就断言它们是全部失败的根因。已有 direct-step A/B 中，简单隐藏自由 JS 后完成率反而从 10/16 降到 6/16，不能再以“取消 CodeMode”作为未经验证的通用答案。

代码：[工具展示](../../codey/operations/kernel_protocol.py#L199)、[已有 A/B](browser-direct-step-ab-20261119.zh-CN.md)。

### 人工协助和成功判定仍需独立处理

来源抓取失败会关闭页面；入口返回后还会清理资源。需要保留可继续处理的页面，并把 ask_user 等待与资源所有权接起来。

已有模型实测也记录了未查询就猜值、选错区域、把网页翻页当正文 offset 等失败，其中部分生产 done 被实验独立判为失败。这既包含模型错误，也说明开放任务的真实完成不能只看控制协议 done。

完成检查应关注用户要求的事实和动作是否有支持；不能把“网页上有文字”“工具返回成功”“模型主动结束”合并成业务任务完成。

## 6. 建议改造方向

沿现有 BrowserSession、浏览器绑定、工具执行器和账本推进，不为小红书另建一套运行时。

1. **先修两个确定性恢复问题。** 区分确认未派发与交付不确定；在保留原动作历史的同时，允许观察、检查遮挡层和适当恢复动作。查询未确认不能全局阻止关闭弹窗等无关恢复行为。补充普通 DOM 结果区域的观察与确认路径，仍要求实际内容与目标相符。
2. **补通用观察与必要动作。** 区域化 snapshot、前景优先、无名可点击元素、遮挡信息；原生 press，随后按场景补 hover、select 和容器滚动。截图需要从浏览器到 provider 的真正图像输出，并在模型不支持视觉时明确降级。frame 支持按真实任务覆盖推进。
3. **提供会话内的进一步检查能力。** 用现有 CodeMode 组合受任务资源约束的浏览器工具；需要时检查 DOM、定位观察到的区域或元素。保留脚本能力与稳定简单动作两种入口。不要让普通 Chat 必须获得无限制 Python/系统权限才能操作网页。
4. **把观察、动作与来源证明分清。** 导航和 DOM 检查能够继续，即使尚未取得可引用正文；动作交付成功不等于业务结果正确；正式 browser_read 再通过既有来源账本记录可引用内容。不能为修交互放宽证据真实性。
5. **简化模型每轮需要理解的界面。** 浏览任务提供稳定、短小的核心工具和统一结果语义；复杂脚本仍可用。反馈包含发生了什么、当前可观察什么、哪项确认仍缺失。授权范围、效果身份和不可重复动作继续由底层负责。
6. **完成通用人工接管。** 登录、验证、权限选择和其他需要用户处理的阻挡使用同一 ask_user 等待机制；保留原 session/page；恢复后读取当前 UI，再决定是否重试原意图。

这里的重点是提高环境可观察性和可恢复性，而不是持续为网站名字、按钮文案或某次失败补一条规划规则。

## 7. 验证顺序

第一阶段先把本轮两个机械失败转为生产回归，并检查修复没有允许重放未知效果的查询。随后覆盖长列表、无名图标、遮罩、内层滚动、只支持 Enter 的搜索、无 ARIA 结果区与 SPA 状态变化。

第二阶段固定原本地模型、模型参数、任务和页面，分别比较观察增强、动作增强、恢复修复的效果；保留全部失败。避免把换模型、换提示、换网页和换工具一次混在一起。

第三阶段进行自然任务与未知页面变化：顺序/文案变化、弹窗出现与不出现、延时加载、分页和必要 iframe。验收以外部结果与实际阅读范围为准，允许不同合理动作序列，不能用预先声明的一条工具序列代替用户目标。

第四阶段再测真实小红书、知乎、PubMed 等网站，记录网络、登录与站点限制。更好的运行环境可以移除一批确定性障碍，但不能保证任何模型在任意网站成功。

本轮产物为源码审计、工具快照探针、两个真实浏览器机械复现和方案；没有修改生产代码，没有宣称完整模型链已经通过。
