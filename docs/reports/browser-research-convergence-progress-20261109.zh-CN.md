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
