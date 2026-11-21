# 统一任务与浏览器：async worker 实施与验证

日期：2026-11-21，Asia/Taipei。接续提交基线：`e2ebbdcf`。

## 当前结论

函数级方案中的任务浏览器依赖反转、独立进程 async Playwright、frame、稳定元素引用、局部/delta 观察、原生输入、截图实际入模、持久化恢复及人工协助接续均已实现。研究报告继续作为组合任务中的交付物，不能替代项目修改和验证。

**完整方案的自然任务验收仍未全部通过，不能声明 P0–P8 全部验收完成。** 同一本地模型的视觉自然任务和模拟人工协助自然任务通过；研究后修改文件、检查失败后回头研究这两类组合任务仍失败。真实人工登录、任意网站成功、其他操作系统也不在已通过结论内。

实现依据：[函数级方案](codey-unified-agent-browser-function-plan-20261121.zh-CN.md)。本报告取代首批实施报告中“async worker、frame、截图入模尚待实现”的现状描述，原报告作为历史记录保留。

## 实际调用链与迁移边界

```text
统一 TaskSession / TaskPolicy / kernel / effect receipts
  → operations.browser_resources / ToolResources：任务持有 BrowserSession
  → 同步 BrowserSession facade
  → 有界结构化 IPC / 独立控制读取与取消
  → 子进程 BrowserRuntime / async Playwright
  → 当前 target / frame / loader / backend node

来源采集 TaskBrowserSource 借用同一 BrowserSession
截图 → managed binary artifact → ToolImageRef → 实际 Chat / Responses 请求
ask_user → 原任务 durable question → 原子回答 → 原页重新观察
```

- automation 不导入 research 或 operations。任务采集不再从研究 provider 反向取得 Page，不再使用全局同步搜索 worker。独立的旧来源 provider 仍供其独立入口使用，没有让同一任务同时持有两套浏览器 driver。
- 任务终态释放自有资源，等待用户或后台工具不提前释放父租约。冷恢复只连接已记录的浏览器实例、context 和 target；原实例或页面丢失时明确失败，不创建替代页面掩盖丢失。
- worker progress 是原 effect 的不可变 artifact，不是第二份 effect settlement。派发前拒绝、派发后未知、已经 settlement 的重交付继续区分；取消和崩溃不能把未知动作变成成功或自动重放。
- `ToolSpec` 继续提供参数、授权、执行器和 replay 合同。新增工具进入直接入口和 CodeMode 的同一冻结目录；只读、denied、显式 CodeMode-only 和旧实现身份检查继续有效。
- 方案中的部分拟定 helper 名称按现有模块职责收敛：字节存储实际使用 `write_binary_artifact`，对话框观察使用 `browser_dialog`；前提检查复用现有 dispatch、delegate 和 driver，没有再建立一套授权判定器。

## 函数方案与实际实现对应

| 方案批次 / 测试目标 | 实际落点与验收边界 |
| --- | --- |
| P0–P1 / T01–T04 | kernel_protocol、task_entry、task_handoff、kernel_control；统一授权、ToolSpec 和组合交付，沿用原 effect/完成检查 |
| P2 / T05 | browser_resources、ToolResources、TaskBrowserSource；任务拥有资源，来源借用，等待/终态/冷恢复回归 |
| P3 / T09–T10 | browser_refs、browser_frames、browser_snapshot、browser_query_async；真实 Chromium 身份及局部观察回归 |
| P4 / T06–T08 | browser_process、browser_ipc、browser_worker_main、browser_runtime；独立 async worker，控制取消、并发、崩溃与原 settlement |
| P5 / T12–T14 | browser_native_input、browser_followups、browser_next_step、browser_detail；输入、遮挡、无 ARIA 结果与待确认查询回归 |
| P6 / T15–T16 | search_provenance、TaskBrowserSource、source_gateway、ledger_receipts；搜索边界/来源 trace、错误页、HTTP/PDF 及恢复。专项通过，旧 test_research.py 扩大批次仍有 21 项未通过 |
| P7 / T11、T17–T20 | browser_images、ToolImageRef、managed_outputs、request_codec、context_checkpoint、UI coordinator/render；真实图片 wire、恢复、问答与动作耗时 |
| P8 / T21 | **未完成**：两类编码组合自然任务失败；视觉、模拟协助通过不能替代完整原始三任务和真实人工登录验收 |

## 已落地的浏览器与图像能力

- frame 身份绑定 Chromium 的 frame、loader、execution world；跨 frame 的节点不能用另一个文档的 ref 代替。节点引用在语义和身份不变时延续；换节点、改语义、改所属记录或换文档仍拒绝。
- 观察支持 frame、局部根、selector、控件与正文截断、AX/DOM 补充以及 revision/delta。模型可用 locate、inspect、局部观察继续取得目标；delta 的原始回执保留完整观察，不能用派生展示代替权威状态。
- 原生 press、hover、select、check、局部滚动、popup、dialog 及 modifier/release 已接入。独立控制通道允许在等待或对话框暂停时观察、响应或取消；并发目标与同目标顺序、队列和网络请求上限分别受控。
- 普通 div 可以通过观察到的 `result_ref` 绑定查询结果；遮挡的点击在派发前拒绝，其他关闭/恢复控件仍可操作。无明确结果区域时，普通正文可以读取，但不能把页面变化冒充查询确认。
- 截图保留原始 PNG、尺寸、hash、观察身份和坐标映射；裁剪和标注生成独立像素身份，坐标点击验证原截图空间。图片通过 `ToolImageRef` 保存和恢复，在发送时校验 artifact 并编码为真实图像内容；保持 native call/result 配对。
- 本地模型视觉能力复用既有 `supports_images`，从服务器元数据冻结到 selection，恢复时不重新猜能力。实验 Provider 也复用本地 tokenizer 和输入预算配置，没有用增大上限解决预检失败。
- UI 不再把“协助问题已回答”合成为任务完成。浏览器耗时来自每个原 effect 的实际执行区间，不来自常驻浏览器进程寿命；worker phase/elapsed 进入原进展 artifact，结算耗时与 effect 引用进入回执、SSE/headless 和工具行展示。

## 减少模型抄写机制的核查与修复

不能只看函数是否还在。本轮核查的是 durable `provider_request.payload.snapshot`、实际 HTTP 请求和子 effect 回执。

| 机制 | 核查与处理 |
| --- | --- |
| 语义控件选择 | 直接/CodeMode 共用冻结 ref schema；第三版优先唯一的 role/name，只在需要时增加 context。fill 和 click 按控件角色区分，避免把输入框当作查询按钮；真实 driver 仍验证原节点 |
| fill 后自动观察、click 后固定 wait/read | 继续复用已有 lowering 和子回执。已完成的 fill/click 不因父程序失败而重做；return-only repair 仍复用原子回执 |
| 固定文件字段复制 | 复用现有读文件、外层 `result_ref` 和版本化 edit 程序。完整来源满足后，实际模型请求中提供 `{"tool":"codemode","args":{"path":"result.json"}}`，字段值和读回执由应用提供 |
| 完整查询区域的字段选择 | 原字段路径只认可整页，且 next-step 又要求再读整页，阻断了已经完整读取的结果区域。现在复用 `report_read_scope` 和同一 requirement evaluation；范围未覆盖、截断、错误文档仍拒绝。显式整页 research sequence 保留更强要求 |
| 结构化附件选择 | 新 worker 曾只读取 target.id，丢失原有唯一 name 选择，合法调用报 `ERROR: 'id'`。已恢复唯一名称选择、同名候选及当前 ID；复用 registry 的 ref 身份并复核原控件详情，避免重复生成不稳定的另一套身份 |
| 单个 return 属性修正 | 恢复既有的 `return window.xxx;` 修正；其他 return/任意脚本不做语义改写 |
| 验证命令提示 | 统一入口复用项目配置的验证候选发现，并展示首选候选；候选展示不等于认证该命令覆盖了本轮改动 |
| 继续提示 | 明确来源已就绪而文件仍未编辑；报告不会执行编辑。无结果区域的查询提示允许重新观察当前控件，避免只要求重复同一次读取 |

实机证明这些机制在实际请求中生效，也证明它们不能保证模型遵循合同：第九、十次组合任务的请求均包含 path-only 复制入口，模型仍跳过它或提交错误 edit 参数。没有因此放宽字段来源、文件版本、命令策略或完成门。

## 本轮发现的确定性问题

1. 来源导航的 delivery 与来源 acquisition 枚举不一致、PDF browser→HTTP trace 丢失；已按同一来源合同转换并保留两个真实 attempt。
2. 导航成功回执漏传 HTTP 状态，导致多轮页面前提误判；现在与 capture 复用同一份文档响应事实。
3. durable metadata 恢复后为 FrozenMapping，进展恢复只接受 dict 导致资源元数据丢失；恢复现在接受并严格解码 Mapping。
4. 原 target 丢失时，部分连接产生无效资源绑定，把目标丢失覆盖成 worker 丢失；失败回执保留原绑定，重复导航继续拒绝替代原目标。
5. Chromium 退出时删除 lockfile、晚写 cache 导致临时 profile 清理的 FileNotFound/目录非空竞态；对已复现的退出竞态在原有期限内完成清理，其他错误仍报告失败。
6. 附件输入回读只能证明文件选择，不能证明服务器接收；已恢复 `effect_verified=false` 的业务语义，保留选择回读的 verified delivery。
7. 轮次耗尽时摘要直接采用未接受的完成草稿，可能展示“测试通过”；现在明确标为任务未完成及 Unaccepted draft，原文仍可审阅。

## 搜索范围与 provenance 补齐（T15）

最终核对函数计划 10.1 时发现，任务搜索虽已检查最终 URL 的查询参数，但 `site:` 的 hostname 边界及完整成功 provenance 尚未贯穿 ledger。本轮已补齐：

- 任务 worker 和独立浏览器搜索共享正规化 hostname、IDNA、尾点和子域边界校验；`example.com.evil.test`、用户信息和 URL 查询字符串不能冒充指定站点。连接器结果和 gateway 同样执行该范围，不将站外结果升级为合格命中。
- 结果区域优先；fallback 只扫描 main/搜索容器内可见链接，排除导航与已标识广告，广告块自身及其后代均被排除。该规则不保证识别网站所有未标识广告。
- 每个尝试保留 requested query、实际最终页面、引擎、提取范围、可观察查询一致性、接受及拒绝 URL、失败分类。URL/页面输入框查询不一致时不返回旧结果；独立入口在提取前后核对最终页面。连接器只有 metadata，不冒充网站页面或已读正文。
- 成功记录随现有 SearchRecord/原研究回执恢复，零命中也保留拒绝诊断；失败诊断保留在原 effect 的 `search_attempts`，不产生研究证据。旧回执缺少 provenance 时按未观察恢复，不猜测匹配。没有新增状态库或双写 settlement。
- 为串起包装层，清理了 `connector_search.py` 原有 Any/cast 与 JSON/HTTP 的隐式动态类型；测试使用明确的响应类型，未让生产流接口迁就动态 Mock。

## 回归与首败保留

没有运行仓库全量 pytest。选择文件、collection、每批日志、原请求/响应/回执及 SHA256 见 [机器清单](data/codey-unified-worker-validation-20261121.json)。批次有重叠，不把下表数字相加冒充独立覆盖数。

| 记录 | 结果与解释 |
| --- | --- |
| `final-actions-frozen-first` | 394 passed / 3 failed：一个实验期间源码变化；一个旧 fixture 仍向已退出任务路径的同步 provider 注入页面；一个新测试误把文件选择当作服务器效果。对应文件在 `final-repairs-timing-first` 全部重跑通过 |
| `final-observe-frozen-second` | 190 passed / 2 failed：composed query 提供完整区域，旧脚本却立刻提交显式要求整页的研究 sequence。补真实整页读取，`final-composed-sequence-second` 61 passed；没有放宽生产完整性 |
| `final-entry-sources-frozen-second` | 112 passed / 13 failed：包括旧控件展示版本、只读 CDP 不支持实验使用的布局读取、实验源码变化，以及一次 search deadline。旧版本/布局实验迁移后，`navigation-presentation-repairs-third` 56 passed / 1 deadline；该场景后在完整受影响文件重跑通过 |
| `final-repairs-timing-first` | 89 passed；包含上述 choice、copy-upgrade、native 文件选择、navigation、冻结展示版本及独立动作耗时 |
| `structured-upload-second` | 10 passed；包含唯一名称、重复名称、过期 ID、提前选择、父 return 修复及未认证服务器接收 |
| `cleanup-budget-final-first` | 10 passed；真实 worker 恢复/清理、已复现的两个 profile 竞态和耗尽轮次草稿 |
| `final-contract-entry-first` | 124 passed；CodeMode、冻结目录、研究提交和统一入口相关回归 |
| `final-images-sources-first` | 184 passed；来源/PDF、图像协议、请求恢复、单一 ToolSpec、UI/headless 语义；另保留一个历史 mock worker 未释放造成的 pytest 临时锁目录清理 warning |
| `final-question-events-first` | 49 passed / 10 failed；旧测试要求 strict 全局隐藏 done 和已授权 finding。按统一任务合同迁移，并移除所改文件对未类型化旧测试 runtime 的依赖；`question-ownership-final-third` 24 passed |
| `final-delivery-regression-first` | **197 passed**；最后冻结实现后的动作计时、图像回执、worker/清理、统一入口、字段复制、组合交付、问题所有权与最终交付投影 |
| `search-provenance-first` → `second` | 119 passed / 1 failed → **120 passed**。首败为旧动态 HTTP Mock 不满足字节响应合同，测试已迁移；新范围/错查询/来源恢复用例通过 |
| `search-recovery-second` | **68 passed**；gateway、来源账本、恢复、acquisition 与新职责位置的工厂边界 |
| `search-expanded-first` | **199 passed / 22 failed，6 subtests passed**。其中工厂定位断言已修复并纳入上述 68 项；`test_research.py` 中其余 **21 项仍未迁移/通过**，不将专项重跑当成该扩大批次全绿 |
| `search-ad-regression-first` | **49 passed**；结果区本身标为广告及广告祖先都排除，不只处理 anchor 祖先 |
| `search-final-regression-first` | **189 passed**；最终冻结后的搜索范围、provider 包装、失败回执、gateway、ledger/replay 和来源恢复。此批有重叠，不与 197 项相加 |
| 前端 | TypeScript、39 项相关 Vitest、格式检查、Vite 生产构建、publish/check 均通过 |

扩大批次 `test_research.py` 的失败主要包括：旧 FakePage 缺少 Playwright 的 `is_connected`/`text_content`；对 callback 位置参数和整页 `a[href]` selector 的实现形状断言；要求 strict 研究全局隐藏已授权工具；旧 done/控制器脚本未使用当前 submit 协议，及其失败后数据库未关闭造成的临时目录锁。该文件未在本轮改写，仍是交付限制。慢测试曾保存 py-spy 栈；本次批次最终在 109.49 秒退出，并非挂死。完整输出及首次失败均保留，不以“旧脚本”为由抹掉失败。

最终受影响 Python 文件的再验证结果由机器清单记录。首败没有覆盖；超时或源码在实验中变化没有归为模型成功。仍在推进的慢测试曾用 py-spy 保存实际栈，没有把等待中的正常执行误报为死锁并跳过。

## 静态结果

- 全树 Ruff 通过。
- 全生产树 mypy、pyrefly、ty 通过，目标 Python 3.11；mypy 检查 734 个文件。
- 本轮 150 个 Python 文件的 mypy、pyrefly、ty 通过。AST 未发现 Any、cast、未标注函数或 automation 对 research/operations 的反向导入；没有新增检查豁免。Pyrefly 的仓库历史 suppression/warning 与本轮错误清零是不同事实。
- 已执行 `codey tests tools` 全源码检查，**这一扩大范围仍未通过**：mypy 18,287 errors / 1,270 files（检查 2,557 文件），pyrefly 21,427 errors，ty 5,063 diagnostics。大量未修改历史测试和工具仍有类型债；完整输出在机器清单中，不将生产树/修改文件通过说成整个仓库类型检查通过。

## 自然任务与真实网站

模型保持 `koboldcpp/Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M`，seed=123456789、temperature=0。每次使用新目录，保留原始请求和回执。

| 自然任务 | 真实结果 |
| --- | --- |
| 研究→先报告→result.json→项目检查 | 截至 `natural-original-tenth` 仍失败。模型已能查询页面、提交报告和执行配置里的 unittest，但跳过实际编辑或使用错误 ref，文件仍不正确。不能计为闭环 |
| 先检查失败→研究→修复→再验证 | 截至 `natural-repair-third` 仍失败；有检查失败与页面查询，但未完成真实文件修复及成功验证。不能计为闭环 |
| 登录受限→提问→人工处理→接续 | `natural-assistance-first` 失败；修正单一 pending 反馈后，`natural-assistance-second` 完成。原任务和浏览器绑定不变，回答后重新 snapshot、搜索、打开记录、读取并引用实际正文。人工处理是明确记录的 fixture DOM 模拟，不能称为真实网站真人登录验收 |
| 只有 canvas 像素的记录 | 首次模型未截图便编造答案；第二次暴露直接能力恢复校验遗漏；第三次通过。最新 `natural-vision-fourth` 再次通过，真实 PNG data URI 进入 HTTP 请求，准确读出 173 / QUARTZ_851 |

自然任务以各 probe 的 `natural_task_closed` 判定，并核对原始请求与回执。视觉任务复用的基础 gate 仍保留其 `passed=false`：该旧指标还要求填写、点击等另一类任务步骤，不能替代视觉目标判据；原字段未覆盖或删去。

最早的两次及第四次失败仍见原[根因报告](codey-unified-natural-root-cause-20261121.zh-CN.md)，没有用后续视觉或协助通过抵消它们。新实验使用真实本地预算计数，不能将旧 estimated 预检拒绝改称服务端实际上下文溢出。

真实站点使用原采集工具，区分网站环境与工程合同：Python/Playwright 官方资料及 PDF 已读；PDF 保留 browser 发现和 HTTP 下载两段 trace；小红书 explore 可以获取公开页面文本，这不证明登录后的搜索/私有正文可用；PubMed 指定文章在本机 HTTP/browser 两路均受 HTTP 403 限制；知乎限制也如实保留。没有伪造登录或 passage。

## 剩余边界

- P8 的两类编码组合自然任务未通过，完整自然验收未完成；不能用固定脚本回归替代模型决策可靠性，也不能用更换模型或放宽合同掩盖失败。
- 三个完整自然任务中，官方资料→修改实现→回到浏览器验证的更长链路及真实人工网站登录仍需实际验收；当前 JSON 记录组合 probe 和模拟协助的覆盖边界已明确。
- 扩大研究回归中 `test_research.py` 的 21 项失败仍未修复，不能声明相关扩大回归全部通过。
- 扩大到所有历史测试/工具的类型门禁仍为红色；本轮没有扩展为全仓库无关类型债重构。
- 实际运行环境为 Windows / Python 3.12，静态目标为 Python 3.11；没有据此声明其他系统、解释器或任意网站均验证通过。

全程在原工作区修改，未复制或创建 worktree。
