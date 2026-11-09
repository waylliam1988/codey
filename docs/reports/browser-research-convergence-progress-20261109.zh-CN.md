# 浏览器与 Research 确定性收敛实施进度

日期：2026-11-09（Asia/Taipei）。用户授权四批实施并逐批提交；每批补相关测试、运行 KoboldCpp 实机检查、Ruff 与类型检查，再更新文档和 commit。

## 范围与约束

- 仅实施不需要模型收益实验决定的正确性修复和保持行为的收敛。网页默认获取策略、模型工具集和研究提示策略不在本轮范围。
- 冷启动协议保持 v1；不增加旧结构兼容、第二套运行时或恢复账本。
- UI失败说明使用英文，并遵循仓库 `DESIGN.md`；沿用现有工具行和详情展示，不增加冗余状态组件。
- 新增和改动边界使用明确类型，消除所触及路径的动态类型；不添加 `Any`、类型豁免或防御性冗余分支。
- 优先直接相关测试；不主动运行完整 pytest、覆盖率或安装包构建。必要扩大范围时先说明原因。
- 测试失败按契约定位根因，区分测试陈旧与生产缺陷；不以放宽生产验证迁就测试。
- 本批开始 HEAD：`ad7d0c1e`。`runtime.json` 与参考报告已经作为 `aef756dd` 单独提交；本批只修改浏览器会话身份和相关测试。

## 四批交付

| 批次 | 内容 | 状态 | 提交 |
|---|---|---|---|
| 1 | 正文版本、文内命中与续读绑定；恢复不重新获取 | 完成 | `7751e6a1` |
| 2 | 来源接收、正文窗口、回执与 passage 共用 | 完成 | `315c8899` |
| 3 | 结构化获取/执行/阅读状态、恢复与重放合同；UI失败说明 | 完成 | `e7768ed5` |
| 4 | 保持行为的工具定义、执行与事实投影去重 | 完成 | 本文所在第四批提交 |

## 验证与恢复工作说明

实机使用本机 KoboldCpp，通过原生产 headless/kernel/provider 执行；可以控制网页响应以验证版本、读取与错误边界，但模型请求与任务执行保持真实。每批保留原始请求、响应、回执和结果；实机检查用于发现确定性缺陷，不当作回答质量收益 A/B。

类型检查按 `docs/typechecking.md` 执行。新增实验记录器复用现有 local model gate 能力，不另建 agent loop。

### 批次 1

- 已确认现状：`source_search` 定位已保存正文；`open_hit` 的目标仅有 URL/offset/pages，之后重新 fetch；`reopen_source` 也重新获取。
- 命中增加正文 hash，原 source_search 回执携带被检索正文；旧命中、reopen_source 和已打开 URL 的 offset 续读只读原捕获。open_url offset=0 明确重新获取。来源 sN 仍表示 URL，不另建版本账本。
- PDF 扫描页保留在原定位回执，不冒充已读；恢复后读取这些页不联网。扫描仍受 30 页、每 10 页最多 80,000 字符的既有上限约束。修复页内命中偏移丢失，以及 PDF 扫描吞掉 DeadlineExceeded 的确定性缺陷。
- 相关测试 59 passed / 142 deselected；补充页内远端命中后，再运行 29 个版本/定位/原子性用例全部通过。曾扩大到现有 Research 文件以检查共享 PDF 行为，发现 5 个旧测试缺少浏览器事件接口、依赖固定 cancellation.check 次数或旧修复文案，已修正测试并逐项通过，未放宽生产合同。
- Mypy、Pyrefly、ty、kernel precision gate 和改动文件 Ruff 均通过。Pyrefly 现有 warning/suppression 并不等于新增类型豁免；本批未新增 Any 或豁免。
- KoboldCpp/Gemma4 实机：`.e2e-artifacts/browser-research-convergence-20261109/batch1-b/summary.json`，passed=true / done。六个工具步骤、两次 HTTP 获取；实际交付 V2=99 grams、旧 hit=V1 的 17 grams。模型最终解释误述 V2，故不能把此合同验证当成答案质量证明。
- 首次实机 batch1-a 已完成生产执行，但实验汇总器误把已有 helper 返回的字符串当字典；修正实验器后重新实机，未修改生产去迁就实验器。

### 批次 2

- 获取边界生成一次经过校验的来源捕获，并与结果一同交给共享阅读实现；不再在渲染时另造获取时间或重新分类质量。PDF 本次读取回执保留本次页范围，正式投影仍按原合同合并独立页读取。
- 抽出 `tool_output.py` 与 `source_reading.py`，Chat/Research、新获取/捕获重读共用正文窗口、预算、回执和 passage；网页、Research 适配完整读取与已保存 source 也共用 `describe_captured_window`。知识材料原有固定版本、访问范围、证据精确区间与答案发布合同保留。
- source_snapshot 是唯一来源编码校验入口，供原获取、ledger 恢复与知识材料使用。PDF 子集保留原获取时间和元数据，不重新获取。
- 相关 61 个用例通过，包括 HTML/abstract/PDF 三种输入的新读/重读/恢复完全一致、单次获取时间、独立 PDF 页回执、知识历史版本和答案上下文。两个既有测试陈旧：字段列表未包含已存在 acquisition 字段，定位测试把“保留前文”误限定为恰好从段首开始；按真实合同修正后通过。
- Ruff、Mypy、Pyrefly、ty、kernel precision gate 通过；移除适配完整阅读两个函数签名中的动态类型，未新增 Any 或豁免。
- KoboldCpp/Gemma4 真实 Research 入口：`.e2e-artifacts/browser-research-convergence-20261109/batch2-a/summary.json`，passed=true / done。浏览器一次获取，随后 source_search → open_hit 读取原捕获，submit_research_answer 以 [e2] 发布原文 17 grams。实验器最初只把 open_url 计入后续阅读，漏掉合法 open_hit；修正汇总条件后用原始记录重算，无需重跑生产。

### 批次 3

- 共享 v1 获取回执明确 backend、请求/最终 URL、投递是否确认、页面观察、结构化失败原因与诊断。HTTP/浏览器/connector 保留真实获取顺序；PDF 导航和 HTTP 下载分别记录。仅 passage 证明阅读，导航或下载成功不能记为已读。
- 请求受限、页面未加载、脚本/资源失败、读取范围和捕获版本缺失均有明确代码；提交和恢复共用验证，拒绝篡改状态、版本、命中 hash/offset 和不带 passage 的成功读取。失败展示不再被误当成获取失败证据；策略拒绝、来源选择、文档提取、无效响应及续读失败仍不满足原 source_gaps 获取合同。
- 获取和 PDF 定位扫描串行，未完成请求不盲目重放；已提交的成功或失败结果按原回执恢复，不重新获取。补充持久化 pending acquisition 的恢复验证。
- UI 使用英文，从结构化代码生成现有工具行说明，保留 URL 和原错误详情；更新 DESIGN.md。修复新发现的确定性问题：工具历史遗漏 failure 字段，导致刷新后失去分类。工具历史模块同时清除显式动态类型。
- 相关版本/捕获/定位/恢复测试 82 passed；获取/来源缺口/网关复核 39 passed；connector/覆盖/重放相关 54 passed、15 subtests passed；选取浏览器/PDF 获取用例 15 passed，未运行整个 Research 文件。两项旧断言把新增 acquisition 字段当作错误，已改为同时核对原字段和真实 backend 顺序。
- 前端相关用例 41 passed，格式、TypeScript 和生产资源构建/发布校验通过。真实 Chromium 对持久历史重启、原生详情展开、刷新和 1440/600 像素布局回归通过，无 JS 错误/水平溢出；截图位于 `.e2e-artifacts/browser-research-convergence-20261109/ui/`。
- Ruff、Mypy（662 files）、Pyrefly（0 errors，35 existing suppressions / 133 warnings）、ty、kernel precision gate 通过。未新增 Any 或类型豁免。
- KoboldCpp/Gemma4：`batch3-c/summary.json` passed=true / blocked，三个真实失败、一个成功，四次 HTTP 加一次真实浏览器导航，脚本在浏览器实际执行并抛错；原生产完成门拒绝对不可读必需来源声称完成。初次模型改写诊断 URL；第二次夹具没有符合既有 shell 合同的启动脚本，未触发浏览器。修正诊断夹具后重跑，未放宽生产 shell 策略。

### 批次 4

- `source_locators.py` 是唯一命中坐标身份与编解码入口：执行端分配/复用 hN，提交与恢复端用同一 URL/hash/offset/pages 规则校验，整批验证后才更新状态；移除此前两次解码及重复 tuple 生成。长 URL 不裁剪，重复查询、已有 ID 冲突、正文版本和 PDF 页内偏移保持原合同。
- 来源读取工具集合从现有 ToolSpec 别名定义派生，协议、执行、提示、事件、绑定和完成检查共用；来源 ID 参数也从同一定义读取。删除仅覆盖三个字段的 ResearchToolsLike，执行边界使用现有 ResearchTools 具体类型；工具观察视图移除 hasattr/getattr 防御式重复判断。
- 清除来源别名解析、观察视图、命中序列化、CodeMode catalog 中的显式动态类型。新增零动态类型保护覆盖来源捕获/读取/定位模块与执行方法；未宣称其他遗留模块或全仓库已清零。
- 新增文本/原生协议的真实 kernel 对照：web_search → open_result → source_search(source_id) → open_hit → reopen_source → source_search(URL)，只获取一次；相同引用、passage 和恢复事实一致，撤销 web.read 后不能继续读取。相关来源/恢复/协议测试 140 passed；CodeMode/目录/结果合同/类型配置相关 93 passed。
- 扩大验证的原因是串行与重放属性会影响 CodeMode 目录。发现并修复其确定性耦合：目录把 replay_class 当作 read_only；现有 ToolSpec 现明确区分“只读来源获取”和“允许自动重放”。CodeMode 仍调用原 kernel，沿用同一 ToolDispatch 独占获取，不新增锁、运行时或恢复路径。旧逆序并发获取夹具改为并发请求经串行获取后检查原回执恢复；手工恢复夹具明确标记已提交结果重发并带实际 passage，不把未执行请求伪装成旧成功结果。
- KoboldCpp/Gemma4：`batch4-a/summary.json` passed=true / done，六步版本/命中/续读流程、两次在线获取；`batch4-codemode/summary.json` passed=true / done，真实 CodeMode → open_url 子调用，仅一次 HTTP，模型最终报告原文 17 grams。两轮都经过生产 headless/provider/kernel；诊断不证明回答质量收益。
- Ruff、Mypy（663 files）、Pyrefly（0 errors，35 existing suppressions / 134 warnings）、ty、kernel precision gate 通过。第四批没有前端源码改动，不重新构建资源或安装包。四批均未主动运行完整 pytest、覆盖率或安装包测试。

## 完成后的边界

- 冷启动新合同保持 v1；原来源账本、immutable passage、intent/settlement 和已提交回执仍是唯一事实与恢复依据。
- 默认获取策略保持原样：普通 Chat HTTP-first，仅既有 app shell 升级浏览器；Research 保留原 connector/browser 路径。本轮没有引入新的浏览器交互工具、browser-first 默认或第二套 agent loop。
- 实机中不能归因于确定性实现的模型输出问题均保留原始证据，不以放宽生产校验掩盖。

### 批次 5：浏览器目标绑定与所有权

- `Session` 增加明确的 browser/context/page 进程内绑定。绑定包含三个对象身份；动作调用方必须先用同一绑定校验页面，跨 context、同 context 的其他 page、已关闭 page 和替代 page 都不能复用旧绑定。
- 会话现在区分浏览器、context 和 page 的所有权。任务创建的页面在收尾时关闭；借用的用户页面不因会话收尾而关闭；自有 headless 浏览器仍完整关闭其 context 和页面。
- `open_chat_page` 不再在多个 context 存在时静默选择第一个 context 创建未绑定页面；没有明确可用 context 时报错。既有匹配页仍保持借用语义。
- 新增真实 Chromium 用例 `tests/test_browser_session_binding.py`，覆盖跨页／跨 context、页面丢失后不能被替代页满足、以及自有浏览器资源释放。相关浏览器与来源回归共 `65 passed`。
- KoboldCpp/Gemma4 生产 headless/kernel 实机：`.e2e-artifacts/browser-research-next/batch5-binding/summary.json` 与 `batch5-binding-codemode/summary.json` 均 `passed=true`、`stop_reason=done`；版本命中、续读和 CodeMode 来源读取保持原合同。该批不改变模型可用工具，收益是目标身份与资源收尾的确定性安全边界，不把模型回答质量诊断冒充收益证明。
- 直接执行 `python tools/source_convergence_live_gate.py` 首次只暴露了测试入口的 `ModuleNotFoundError: tools`；按模块入口 `python -m tools.source_convergence_live_gate` 重跑通过，未修改生产代码迁就测试工具。
- 改动文件 Ruff、Mypy、Pyrefly 均通过；`codey/automation/browser.py` 本批去除了原有 `Any` 返回类型，未新增动态类型、豁免或第二套运行时。

### 批次 6：保留受限页并在同页接续

- 新增 `ResumableBrowserSearchProvider` 作为默认 Research browser provider 的薄适配层。它复用原 `BrowserSearchProvider`、BrowserWorker 和 acquisition gateway；受限、登录或不可读结果保留当前绑定 page，`resume_fetch()` 在该 page 上读取当前 DOM，不重新导航、不重新发起 URL 请求。
- 页面仍不可读时只返回结构化失败，不能形成正文 passage；用户在同页完成验证后，只有新的成功当前页观察才进入来源读取合同。任务取消仍由原有 session 收尾释放资源。
- 新增真实 Chromium 用例 `tests/test_resumable_browser.py`：本地 HTTP 服务器第一次返回 403，确认 page 保留；在同一 page 模拟用户完成验证后读取正文；HTTP 请求计数保持 1；关闭后继续观察明确返回 `snapshot_unavailable`。两项用例通过。
- KoboldCpp/Gemma4 Research 生产入口：`.e2e-artifacts/browser-research-next/batch6-research-default/summary.json` `passed=true`、`stop_reason=done`；版本／命中／引用合同没有回退。普通批次与 CodeMode 回归分别在 `batch6-resume`、`batch6-resume-codemode` 留有记录。
- `batch6-resume-codemode` 首次返回 `no_progress`。事件显示模型连续四次生成 `return result.value.text` 以外的错误 CodeMode 形态，生产返回的是明确的 `CodeMode returned undefined`，重复工具结果保护正常；这是模型遵循 CodeMode 返回契约的边界，不是浏览器保留页或资源收尾缺陷。此前相同生产入口的正确 CodeMode 对照仍通过，未改生产代码迁就模型。
- 相关来源、搜索工厂、SPA、失败页和会话绑定回归共 `71 passed`；新模块 Ruff、Mypy、Pyrefly 通过。`runtime.json` 补入 `resumable_browser.py`，同时收录近期已存在但遗漏的 `operations/chat_citations.py`，发布清单与实际源码一致。

### 批次 7：语义交互底层与隔离实机入口

- 新增 `BrowserInteraction`，复用原 BrowserWorker 和保留的 page，不启动浏览器或模型的第二套运行时。绑定持有第一次观察的 browser/context/page 身份；引用带观察代次，不在后续快照复用。导航、同 URL 文档替换、节点替换与动作后拒绝旧引用；不按同名控件自动重定位。
- 快照明确主 frame、可见正文和命名控件的观察范围，报告文字与控件裁剪；提供当前字段值、disabled/checked 状态，隐藏密码值。等待检查可见条件，支持取消和有限超时。点击投递与页面观察分开记录；观察不证明目标效果成立；回执丢失后返回 uncertain，不重发。
- 候选工具定义只进入合同源；实验脚本通过现有 ToolSpec/custom executor 在本进程注册并走原 headless/provider/kernel。尚未加入普通 Chat 或 Research 的默认工具集。撤回未验证的默认分发接线，避免实验前扩大默认工具面；不把交互动作授予成只读 CodeMode 工具。
- 新增真实 Chromium 测试 8 项和 Research 工作视图测试 1 项，覆盖失效引用、同 URL 导航、文档/节点替换、裁剪、字段读回、隐藏条件、取消和点击已发生后的确认丢失。故障注入仍执行真实 DOM 点击，计数保持 1，不能据此承诺外部系统 exactly-once。
- KoboldCpp 首轮 `batch7-semantic-a` 暴露真实接线缺口：Research 每轮重建工作视图没有浏览器动作历史，快照也未显示字段当前值，模型重复填写/快照至 max_turns。现从原 settled receipts 派生最近 8 条动作元数据，不增加账本；清除 `answer_context.py` 全部显式 `Any`。
- `batch7-semantic-b` 已顺序完成全部交互并 done。实验汇总器误查 UI 的 200 字摘要而漏掉后面的 173 克，改为核对实际模型请求中的完整观察；`batch7-semantic-c/summary.json` 为 `passed=true / done`，单次导航、全部动作成功。原失败样本保留，不混作答案质量收益。
- 默认 Research 生产回归 `batch7-research-regression/summary.json` 为 `passed=true / done`，原正文版本、命中续读和 eN 提交合同不变。
- 相关浏览器、来源分发、SPA、Research 上下文、答案与引用回归 `148 passed, 2 subtests passed`。Ruff、Mypy（直接目标，旧实验记录器导入静默）、Pyrefly、ty 和 kernel precision gate 通过；本批所有修改文件无显式 `Any`，没有新增类型豁免。最初普通 Mypy 跟踪到旧 tools 记录器的既有注解问题；未扩散修改旧实验器，生产模块及本批脚本均单独检查。

## 下一阶段验收清单（未完成，不可视为已通过）

1. 同页 `browser_read` 通过共享来源接收、正文版本、passage、引用和重启恢复；点击、登录提示和快照本身不能成为来源证据。
2. 补全真实 CDP 断连/同端口新实例、借用页与所有权收尾、等待中取消，以及跨 context/多页的操作贯穿；已有少量 Session 测试不等于完整矩阵通过。
3. 真实网站：头条、知乎、搜狐、官方资料和 PDF，记录访问会话、限制及人工协助，不替换失败样本。
4. 自然研究对照：12 题 × 基线/候选 × 3 次，中文动态、资料研究与控制各 4 题；记录关键材料、结论支持、例外和交付，成本仅记录。
5. 默认工具集、browser-first、compact/delta/等待策略和研究指导分别实验。当前仍没有证据授权把这些全部设为默认；不把第 7 批顺序诊断当成自然任务质量证明。

### 批次 8：同页捕获共用来源接收与 passage

- `ResearchSourceGateway.open` 与新的 `accept_capture` 共用唯一 `_receive_capture`，沿用原 URL/重定向策略、PDF/HTML 构造、版本 hash、正文回执与来源记录。接收当前 DOM 不再伪装成第二次 URL fetch，也不复制证据生成实现。
- `BrowserInteraction.capture` 在原浏览器线程上先校验同一 page 绑定，再调用已有当前页读取。实验 `browser_read` 通过原 `render_source_read` 和 `ExecutionDelegate._opened_result` 交付正文与 passage，进入原 kernel settle、Research eN 和完整答案发布。它仍仅在实验进程注册，默认工具集未变。
- 新增接收等价性、受限/空白/私有重定向不形成 passage 的测试；真实 Chromium 贯穿验证 V1=17 → 同页 V2=99，旧捕获仍读 V1，浏览器关闭后按原正文回执重建两版 passage。
- 初次新测试误把 `source_hash` 当成 passage descriptor 字段；按现有合同改为比较 passage ref 与来源 hash，未向生产回执添加重复字段。相关来源、浏览器、SPA、引用与分发回归 `112 passed`。
- KoboldCpp `batch8-read-diagnostic/summary.json` 为 `passed=true / done`：单次导航，真实填写/点击/等待/滚动，browser_read 捕获展开正文，模型以 `[e2]` 发布“鱼丸净重 173 克”；原 `[e1]` 仍绑定初始正文。`batch8-research-regression` 同样通过。
- 自然任务 `batch8-natural-a` 未得到收益：模型只做 source_search 就停止，答案还把没有命中的搜索误述为找到了记录。这是模型选择/复述边界，原始失败保留；不据此扩大默认工具面，不用步骤诊断替代自然任务。
- Ruff、Mypy、Pyrefly、ty 通过；修改文件没有显式 `Any` 或类型豁免。本批无新增打包模块，不运行安装包或全量 pytest。

### 批次 9：真实 CDP 所有权与收尾缺陷

- 新增专用临时 profile 的真实 Chromium/CDP 生命周期测试，仅固定 endpoint 发现边界，CDP 连接、导航、DOM、页面/进程退出和生产 Session/provider 收尾均真实执行，不连接或关闭用户实际浏览器。
- 测试确认了生产缺陷：连接借用浏览器时，provider 另外创建的文章页在任务收尾后泄漏。`ResumableBrowserSearchProvider` 现先关闭任务创建的非 Session 页面，再交原 Session 按所有权释放；借用的原页面只释放路由，不因搜索失败而被关闭。没有另建所有权表、双写或第二套 session。
- 真实 CDP 断开并在同端口启动新浏览器，旧 binding 不能匹配；多 context 同名按钮只影响目标页；页面关闭后同名替代页不能满足绑定；借用用户页保持内容且浏览器连接仍在。等待中取消测试确认原 BrowserWorker 丢弃迟到结果，可继续正确观察。
- 初始 CDP 夹具只有17字，按既有最小24字可读合同正确返回 page_blank；修正夹具正文后，测试才进入真正的资源释放断言并暴露泄漏。没有降低生产正文判定来迁就夹具。
- 共用一个 Chromium 路径/临时 CDP 启动测试 helper，删除三个测试文件里的重复查找。正文截断上限引用 `extract.py` 原常量，移除适配层重复字面量。
- 相关真实浏览器、会话、恢复和失败页回归 `57 passed`。KoboldCpp `batch9-cdp-diagnostic/summary.json` `passed=true / done`，经过借用 CDP 浏览器的生产 provider/kernel，交互、同页 capture 与 eN 答案正确完成。
- Ruff、Mypy、Pyrefly、ty 和 kernel precision gate 通过，改动文件无显式 `Any` 或新豁免。首个 Mypy 提示截断常量的隐式转导出，已改为从真正 owner `extract.py` 导入。

### 实验阶段接续记录（进行中，未作为默认策略通过）

- 当前已提交 HEAD 为 `dbad9a1e`。72 次自然任务矩阵使用固定 cases/gate hash 和同一生产 HEAD；矩阵运行期间不修改其生产输入。时间与 token 只记录，不参与淘汰。
- 连接中断时保留34份完整结果；只将未完成的 `research_expand_exception-2-candidate` 目录移至同根的 `*-interrupted`，记录 interruption.json 后接续。随后该单项在模型调用前触发 Local selection admission 错误；保留 process.log 和 durable.sqlite3，不能把它计作模型质量失败。当前从其持久化 selection 验证相同连接已成功，尚无证据将一次失败归为确定性生产缺陷；完成矩阵后单独复核。
- 已完成样本明确发现候选工具默认开放的风险：模型读取 DOM 新值却未 browser_read，以旧 `[e1]` 引用新值；季节选择还出现用夏季83克回答冬季的错误。不能用“看到新值”认定引用正确。目前不合入默认工具接线。
- 真实网站 acquisition 对照已经完成7站点 × 两种策略 × 3次（42次），位于 `.e2e-artifacts/browser-research-next/real-sites-repeat/`。头条 HTTP 无正文而浏览器稳定取得3722字符；两条知乎冷会话仍403；搜狐、Python/Playwright 官方文档两侧均可读；PDF保留HTTP字节路径且三轮均成功。此前独立单轮 PDF TLS EOF 作为瞬时失败保留，不算浏览器收益。
- 已补充两项确定性失败用例，待冻结矩阵完成再修生产：重新构造控制器复用 `g1e1` 引用；知乎 JSON403 文档在 resume 时被误当正文。两者须修根因、跑相关真实浏览器与 KoboldCpp 诊断后独立提交。
- 新实验脚本：browser_research_cases / browser_research_matrix / browser_site_comparison / browser_site_model_gate；最后一个24次真实网站+模型对照尚未开始，避免并发争用 KoboldCpp。快照/compact/delta/等待默认值及研究指导实验也尚未完成。

### 批次 10：矩阵暴露的确定性缺陷

- 固定72个矩阵位置已全部结束。71次进入模型，一次在准入阶段失败；原失败和中断痕迹保留，另建 `research_expand_exception-2-candidate-admission-recheck` 在相同生产 HEAD/gate/cases 下复核成功，达到72份可审阅模型结果。准入失败没有复现，当前证据不足以认定根因；不将其混作模型失败或删除。
- 默认工具开放未通过：四个动态题两侧均0/3达到数值及实际 passage 的机械筛查；候选三种展开题看到了新值，却仍引用没有新值的 e1；冬季题两次误答夏季83克，一次如实不足。资格例外题虽通过21/9数字筛查，人工核对仍遗漏危险作业例外。控制题两侧各12/12通过。后续可实验工作流指导，不把本次开放直接设为默认。
- 修复控制器重建重号：ref 带独立 UUID scope 和观察代次；复制旧 ref 不能命中新控制器节点，不按同名节点恢复。工具示例不再诱导猜测 g1e1。UUID ref 仍需模型照抄；一次诊断出现截短 ref，生产正确拒绝且未误点，作为模型边界保留。
- 共享可读性识别器拒绝完整 JSON error envelope，不依赖知乎域名或错误码。普通正文引用错误示例、带有效数据的 JSON 不受影响。resume 使用同一失败分类；同页解决验证后仍可读取，HTTP请求保持一次。
- 实机新增发现并修复 Research 完成冲突：已失败的明确必需 URL 配合合法 partial/unresolved whole-answer 可以交付缺口报告；不再重复要求正文成功。未尝试 URL、策略拒绝、丢失捕获、answered 声明仍不满足此例外；显式 legacy `research_sources_opened` 成功要求仍不被绕过。失败不增加 opened sources/passages，也不生成该成功检查。
- 中文枚举诊断又复现 URL 意图解析遗漏 `、`：其后的工具名被吞进 required URL，造成无法满足的来源要求。现将顿号作为 URL 文本边界，沿用同一 intent 解析器。
- 真实浏览器/获取相关73项通过；因实机暴露完成合同冲突，扩大到 whole-answer 发布/冷恢复、来源要求与失败闭环67项通过；新增顿号及入口回归29项通过。新测试曾误用 SourceVersion.final_url，类型检查和测试共同指出其正确字段为 url，已修测试，未向生产添加别名字段。
- KoboldCpp `batch10-failure-contracts-d/summary.json` `passed=true`：新 ref 的填/点/等待/滚动/同页读取完整通过；JSON403经 snapshot、browser_read 后仍失败，没有任何原文 passage，最终合法 unresolved 交付。a/b/c 原始失败保留，其中分别暴露完成冲突、模型步骤遗漏和顿号解析问题，未覆写失败记录。
- Ruff、Mypy、Pyrefly、ty、kernel precision gate 通过；本批生产与新增测试无显式 Any 或新增类型豁免。默认工具、获取策略、快照和研究指导未改。没有运行完整pytest、覆盖率或安装包测试。
