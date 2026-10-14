# Codey 网页自动化是否全面迁移 TypeScript：代码评估

日期：2026-10-14。检查起点 HEAD：`2f845c4d6529b608b6082f41b050be5239c589d5`。结论依据当前工作区，而不是纯 HEAD；工作区已有用户进行中的前端迁移改动，本次没有修改这些文件。

## 结论

**目前不建议让 TypeScript 接管全部网页自动化。** 对“更好适应网页模型的页面变化”这个目标，当前优先级应该是补齐页面观察证据、候选判断和真实 DOM 回归验证；完整语言迁移的额外成本明显，而它没有直接解决这些限制。

建议的目标架构是：Python 保留浏览器会话、发送合同、任务调度、修复事务、诊断与监督；需要增强的浏览器内观察逻辑可以独立为 TS 源码，编译后由 Python 注入。这个职责划分可以是最终架构，不是必须继续迁移的临时过渡。

若以后明确要做独立 Node 浏览器引擎、多标签页持续观察和独立交付，全面 TS 才具有更充分的理由。现在没有做 TS 引擎 A/B 实验，因此本报告是基于代码和验证证据的工程判断，不是性能或维护成本的实测证明。

## 1. 检查与验证范围

检查关键调用链：

```text
Python 任务 / CLI / 审阅
  → providers.registry
  → WebChatProvider
  → browser.Session / CDP 页面
  → web_driver 的超时与诊断边界
  → 五个站点驱动
  → controls / discovery / submission / send_loop / flow
  → profile doctor / 用户教学 / revival 保存与回滚

适配器源码修复
  → repair surface / policy / sandbox
  → Python 静态检查与站点测试
  → override generation
  → JSONL worker / 隔离浏览器 profile / canary
```

统计的 22 个直接相关 Python 文件共 8,958 行。五个站点驱动合计 3,058 行：DeepSeek 434、GLM 552、MiMo 845、Qwen 643、StepFun 584。这个数字是检查范围规模，不表示所有行都必须迁移；自动修复、剪贴板和应用层调用者还在统计范围之外。

这 22 个文件中，按名称包含 JS 的字符串常量统计，至少有 14 个页面脚本块、271 行脚本。统计没有覆盖所有内联 evaluate 字符串，不能理解成“全部浏览器 JS 只有 271 行”。

运行两组现有测试：

```powershell
python -m pytest -q tests/test_provider_profiles.py tests/test_provider_controls.py tests/test_profile_doctor.py tests/test_provider_revival.py tests/test_provider_flow.py tests/test_provider_flow_fault_injection.py tests/test_provider_send_loop.py tests/test_provider_submission.py tests/test_revival_coldstart.py tests/test_glm.py tests/test_qwen.py tests/test_deepseek.py tests/test_mimo.py tests/test_stepfun.py tests/test_browser_worker.py tests/test_worker_local_fixes.py

python -m pytest -q tests/test_browser.py tests/test_adapter_self_repair.py
```

结果：第一组 326 passed、33 subtests passed，21.07s；第二组 133 passed、6 subtests passed，8.15s。合计 459 项测试、39 项子测试通过，没有 reported skip 或失败。这是选定的相关测试，不是全项目测试。

另运行独立 headless Chromium 的四个合成页面实验。使用新建浏览器，不连接用户 CDP、不访问真实网页模型、不发送消息。脚本和结果位于 `.e2e-artifacts/automation-assessment/`。

## 2. 当前已经具备的抗改版能力

### 配置与语义发现

`profiles.json` 把站点 host 和多个选择器放在数据文件中。`controls.locate_control()` 依次尝试内置 profile、当前暂存控件、已学习控件、语义发现，再按条件进入用户教学。

`discovery.control_candidates()` 在页面采集标签、role、可编辑属性、aria-label、placeholder、文本、位置、大小和输入框距离；Python 根据这些证据评分。输入框和发送按钮候选要求最低分与领先分差，避免相近候选直接被选中。

依据：[controls.py](/E:/codey/codey/providers/controls.py:200)、[discovery.py](/E:/codey/codey/providers/discovery.py:38)。

### 已有浏览器侧 DOM 变化观察

`discovery.py` 的 `_START_RESPONSE_WATCH_JS` 已创建 MutationObserver，观察 childList、subtree 和 characterData，记录发送期间变化的元素。`_READ_RESPONSE_WATCH_JS` 读取变化区域，过滤不可见、断开连接和过大文本。

因此“采用 TS 后才能观察页面 DOM 变化”的判断不成立；当前 Python 驱动已经通过页面 JS 做这件事。

### 验证后学习与回滚

`controls.revival_send()` 将一次发送包成恢复事务。发现的控件先 pending/staged，操作得到确认后标记 verified，发送成功时提交学习结果；失败时清理未提交状态。`revival.py` 管理 provisional、验证、失败计数和回滚。

`profile_doctor.py` 不是让模型随意生成一段脚本：它提供有限候选，限制标签和字段，要求返回已知 candidate ID。`app/sibling_probe.py` 通过健康 sibling provider 进行判断，并保留用户点击教学作为补充。

这套机制迁移时必须重现，不能把它简化成“找不到按钮就让模型看一下”。

## 3. 抗改版的具体限制

### 3.1 GLM 通用完成恢复缺少足够的观察事实

`glm._generation_complete()` 使用 profile 中固定的 idle_button 选择器。GLM 的 `_chat()` 在循环中构造 FlowObservation 时主要提供 response_stable 与 response_nonempty，没有像 Qwen 那样提供 stop_visible/stop_hidden 的变化历史。

`flow.py` 的完成恢复要求文本稳定之外，还有已观察到的生成→结束变化。例如先见过 stop_visible，随后 stop_hidden；或先 typing_true，随后 typing_false。

现有测试 `test_stepfun_and_glm_text_stability_cannot_start_recovery` 明确验证：仅文本稳定不能启动恢复。这是防止把思考停顿当完成的正确限制，但同时说明 GLM 内置完成标记失效后，可用恢复证据不足。

应补充可靠、允许未知状态的观察事实和变化记录，不能删掉此测试放宽完成判定。换成 TS 而仍只提供这两个 bool，恢复能力不会增加。

代码位置：glm.py:227、glm.py:429；flow.py:76、flow.py:233；tests/test_provider_flow_fault_injection.py:230。

### 3.2 发现的覆盖范围决定能否找到新控件

当前 `_DISCOVER_CONTROLS_JS` 查询 button、role=button、若干 send/submit/enter class、textarea/input/role=textbox/contenteditable。

新页面如果用无 role、无可识别 class 的自定义 div 图标按钮，即使它在输入框旁边，当前采集器也可能根本不把它交给评分器。换语言不会让候选列表自动变完整。

应该基于真实失效样本扩展候选采集，并校验唯一性、作用域和动作确认；不是无限扩大选择器然后点击评分最高者。

### 3.3 回复候选缺少与控件相同的歧义拒绝

`select_control_candidate()` 要求领先第二名至少 12 分；`select_response_candidate()` 目前只检查第一名达到 48 分，然后选择第一名。

合成页面中，两个相同特征、同分 129 的新回复区域仍会产生选择。这个实验只证明算法没有拒绝该歧义，不证明真实站点当前一定读错。

可以增强 reply 的当前发送归属、用户消息邻接、容器关系、生成轨迹和候选差异证据。不能机械把控件的 12 分阈值套过来：父/子候选可能属于同一回复，需要先归并。

代码位置：discovery.py:71、discovery.py:220。

### 3.4 观察仍然分散，DOM observer 本身也有生命周期限制

多个站点各自轮询 count、text、stop、footer、typing 等信号，有些操作分多次 Playwright 调用。这些读取之间页面可能变化；一次统一快照会让同一轮决策依据更一致。

观察器的 changed 数组在当前回复 watch 生命周期内积累元素，并没有显式元素数量上限；见 `_START_RESPONSE_WATCH_JS`。WeakSet 防重复，但不能限制大量不同节点的增长；读取时有文本长度过滤也不等于候选集合有数量预算。

这只是代码层面发现的潜在长回复/频繁重绘风险，本次没有做内存压力测量。可增加区域限制、批次预算、导航重装和销毁验证，Python 注入 JS 与 TS 都能实现。

### 3.5 通用 flow 是有意受限的规则系统

`MAX_RECIPE_STAGES = 1`，恢复选择来自已有谓词和有限模板。模型无法创造采集器没有提供的新信号。

所以遇到新的页面机制，首先要更新观察能力，再决定是否扩展 flow 的表达范围。把现有有限规则翻译成 TS 并不会变成通用网页代理。

## 4. 四个真实 DOM 实验

| 合成变化 | 当前结果 | 说明 |
|---|---|---|
| 去掉旧输入框 class，保留 textarea 和语义标签 | 自动发现成功 | 现有 Python 已能适应这种变化 |
| 发送变成无 role、opaque class 的 div SVG 图标 | 自动发现失败 | 缺少候选采集证据 |
| 两个同特征变化回复区域，分数同为 129 | 仍选择一个 | 回复歧义处理可加强 |
| GLM 的旧 idle DOM 改成新 composer | `_generation_complete()` 返回 false | 固定完成标记失效需要新状态证据 |

实验最后一项不表示对任意新页面都应该自动判断完成，只验证原有标记失效后的函数行为。

结果：`E:\codey\.e2e-artifacts\automation-assessment\dom-probe-result.json`。复现：`python .e2e-artifacts/automation-assessment/dom_probe.py`。

## 5. 全面 TS 会碰到的真实集成边界

### 浏览器与登录状态

`automation/browser.py` 负责 Edge/Chrome 查找、profile、CDP 端口记忆、host 匹配、已有标签页复用、fresh tab 与会话关闭。它把模型浏览器当作持久用户状态。

迁移必须明确普通连接只释放本地控制连接、临时页面可以关闭、何时可以终止自己启动的浏览器进程。不能照抄测试框架的 browser.close() 生命周期。

### 执行线程与取消

`BrowserWorker` 把 sync Playwright 放在专用线程，并有有界队列、任务状态、deadline、取消、abandoned 和健康状态。

Node async 模型可以让控制消息在等待浏览器操作时被处理，这是潜在架构收益。但 Python 也有 async Playwright；它不属于 TS 独占能力。Node 仍需保持同会话单次发送、处理取消后晚到结果和资源释放。

当前 controls/flow 使用 threading.local。迁移到共享 Node event loop 后不能简单换成模块级对象：多个任务或 sibling recovery 可能共享错误的上下文，需要显式 per-run/per-page 状态。

### 模型辅助修复会借用真实 Page

`registry.borrow_open_provider()` 通过 `owner_page.context.pages` 找 sibling tab。`sibling_probe.handle_profile_doctor()`、flow recovery 和用户教学请求也包含真实 Page 对象。

完整 Node worker 必须用 page ID/CDP target ID 和数据协议替代这些对象，支持教学请求/恢复结果的双向通信。还要保证浏览器等待模型辅助时，worker 不会因单请求锁而阻塞回调。

### 现有适配器源码自修复绑定 Python

`repairs/adapter_surface.py` 的允许范围列出 Python 驱动和共享文件；`policy._text_files()` 只枚举 py/json；`adapter_repair._run_static_checks()` 执行 py_compile、Ruff、站点 unittest，并按影响扩展 import/schema 检查。

`adapter_overrides.py` 把修复文件安装到独立 Python 包路径；`WorkerChatProvider` 设置 PYTHONPATH、启动 Python worker_child，管理 override generation、稳定 profile、stdout/stderr、请求边界和 canary。

全面 TS 不仅需要 tsc，还要重做源修复允许范围、依赖与构建约束、候选产物、JS worker、错误协议和回滚。只迁移 glm.py 会留下原修复链无法修复或验证它。

### Python 特有的响应处理

GLM 的 `_normalize_python_edit_content()` 用 compile() 比较原始与修复引号后的 Python 源码有效性。Node 无法直接等价执行这个 Python 判断；可以把它留给主程序的响应后处理，但要明确责任，而不是换一套 JS 引号规则代替。

## 6. 需要修正的部署成本判断

“迁移后用户必然多装一个 Node”不准确。项目已经有 `providers/zen/node_http.py:24`，使用 Python Playwright 随包提供的 Node，找不到时才查系统 Node。

因此 Node 可执行文件分发有现成基础，成本没有从零增加一个运行时那么大。

但仍需选择受支持的 Node Playwright 库分发方式、版本配套和 wheel/package-data，不能把 Python Playwright 的内部私有模块路径当成稳定公开依赖。开发期 npm 的 Node 版本也不等于用户运行时随 Python 包安装的 Node 版本。

这是降低迁移成本的有利证据，不足以抵消协议、修复体系和语义回归的工作。

## 7. 收益是否足以抵消成本

| 目标 | 全面 TS 的实际作用 | 当前判断 |
|---|---|---|
| 抵抗 class/层级改动 | 提供更好的源码组织；核心依赖采集和定位规则 | 无需全面迁移 |
| 跨站点统一观察和状态机 | TS 适合；Python 也能实现 | 先定义统一快照与证据合同 |
| 持续 DOM 观察 | 已有 MutationObserver；TS 可改善开发体验 | 页面观察模块 TS 化有价值 |
| 浏览器测试与调试 | Playwright Test 有便利工具 | Python 也有浏览器测试与 tracing 能力 |
| 多标签页并发与控制响应 | Node async 有设计优势 | 目前没有测到 sync worker 是目标瓶颈 |
| 自动修复源码 | 可加入类型检查 | 现有完整修复与验证体系需要迁移 |
| 运行速度 | 可能减少部分语言桥开销 | 无性能证据，模型等待常是主要时延 |
| 交付维护 | 独立引擎可独立发布 | 当前 Python 集成与安装方式会变复杂 |

Playwright 官方说明，各语言共享底层实现、核心浏览器功能都支持，差异主要在测试生态；Python 支持 sync/async API。来源：[语言支持](https://playwright.dev/docs/languages)、[Python library](https://playwright.dev/python/docs/library)。

## 8. 推荐投入的具体改进

1. **统一观察快照**：按 page/run 标识一次采集当前 composer、候选动作、新消息、回复归属和生成状态；对未知状态显式表示 unknown，不把找不到 stop 控件直接等同停止生成。
2. **补齐 GLM 等站点的完成证据**：保持“文本稳定不足以恢复完成”的保护，增加真实可观察的生成→结束转换。
3. **强化候选判定**：扩展已证明需要的新控件形式；回复候选先合并父子区域，再检查归属、歧义和变化轨迹。
4. **真实 DOM 故障测试**：class 随机变化、DOM 层级变化、多个 composer、多个回答区域、contenteditable、无 aria 图标、思考暂停、重新导航和重绘。用本地 HTML fixture 验证，外部站点再做独立 live 验收。
5. **观察器模块化**：将嵌入字符串整理成 TS browser-side 模块，建立字段校验、生命周期和数量预算。保持 Python 控制侧，不要求 Node Playwright 全面接管。
6. **清晰的修复证据**：记录失效阶段、使用了哪些观察信号、为何候选被接受、何时学习、何时回滚。避免只能从 timeout 推测网页发生了什么。

这不是承诺小改动即可解决所有网页改版；它把投入直接放在本次检查找到的限制上。

## 9. 什么时候重新考虑全面 TS

以下条件同时明确时再评估：

- 自动化层要作为独立产品/进程长期维护，而不是仅更换站点脚本语言。
- 持续多页面事件观察、控制消息响应或 Python sync worker 已成为可复现的瓶颈。
- Page 对象已经被稳定的命令/事件合同替代，包括 doctor、用户教学、取消、失败和 generation。
- TS 引擎在相同 DOM 故障集和真实站点验收中，降低人工修复、误发送或错误完成率。
- Node Playwright 的资源和依赖能在干净安装环境正常交付。
- 原有 Python 修复事务、验证、回滚和关键语义有对应证据。

如果决定迁移，仍可在隔离分支中一次全部完成后交付；本报告不要求长期维护两套控制引擎。

## 10. 评估限制

本次没有操作真实 GLM/Qwen/DeepSeek 等账户，没有测试当前线上 DOM，没有测量长回复内存、运行时性能或 TS 实现维护工时。合成页面能验证具体算法边界，不能直接推导外部站点的失败率。

459 项测试证明当前选定合同仍成立，不证明所有网页改版都能恢复，也不证明 TS 实现一定更差。当前“不全面迁移”的结论是：没有发现必须借助 TS 才能解决的阻碍，但发现大量完整迁移必须保留的既有能力。
