# 中间对话区综合改造与实机验收

日期：2026-10-16。设计基线：[DESIGN.md](../DESIGN.md)。

## 已落地的交互

| 内容 | 最终行为 |
|---|---|
| 一轮对话 | 一处 Working，完成后显示服务端实测耗时；过程和连续正文减少重复作者标签 |
| Markdown | 粗体、斜体、删除线、任务列表、分隔线、表格、代码与图片共用解析器；代码保持单色 |
| 文件引用 | 普通行内文件名经服务端确认后可点击；`json.dumps`、私有符号、通配符保持普通代码 |
| 工具 | 收起为简短工作行；展开保留结果、读取范围、搜索数量或实际编辑差异；错误保留原始详情 |
| 产物 | 明确的交付文件列表成为紧凑文件行；Research 报告走同一文档入口，证据和使用到项目收进次级菜单 |
| 流式正文 | 原生 API 的实际文本增量直接到页面；完整块保持 DOM，改变尾部；最终回答在原位置结算 |
| 运行中补充 | Send 将草稿持久排队；可撤回草稿、删除，Stop 后明确暂停；响应不确定只查状态 |
| 用户提问 | 原生 `ask_user` 暂停工作；单选和自由回答；回答持久化后恢复原模型、协议历史和授权 |
| 消息调整 | 悬停可将原用户消息回填为新草稿；历史请求和已执行命令不被改写 |
| 长历史 | 先挂载尾部完整轮次，补历史保留已有节点和阅读锚点；可显示全部已保存消息用于查找 |
| 局部故障 | Markdown 和图片故障各自隔离；重新显示或重新读图片不会重跑任务 |
| 明确拒绝的排队输入 | `input_queue_full` / `input_not_found` 显示 `Not sent`，可恢复草稿；未知结果仍只能查询 |
| Research 历史卡片 | 使用消息自身 `runId` 打开对应报告；只有唯一历史运行且消息缺少 `runId` 时保留兼容回退 |
| 本地图片数量 | 所有确认图片进入最多 4 个并发的 worker pool，不再在第 20 张后静默截断 |
| 结构化失败 | provider、执行、输出和渲染失败通过有界 `failure` 投影；未知执行结果只提供状态查询，恢复动作按固定映射执行 |
| 实时命令输出 | shell 审批和普通 `run` 共用 execution identity、增量 UTF-8 观察和有界 stdout/stderr 快照；运行中同一命令卡更新，异常落为 `unknown` |
| 公式与图表 | 正文、Research、文档预览共用本地 KaTeX/Mermaid；不完整或失败块保留源码，异步图表校验块摘要和挂载节点 |
| 展示历史 | SQLite transcript archive 使用 epoch、稳定 message identity、游标分页、字面搜索；首屏合并归档尾页，清空同步删除 UI 投影并阻止迟到写回 |
| 提问草稿 | QuestionDraftStore 使用同 origin localStorage，支持刷新恢复、Clear draft、跨标签冲突处理和实例键；不承诺跨端口/浏览器 profile 同步 |

保留已确认的 10px 圆角、13px 审批标题、无 Reason、等待审批不折叠、复制悬停出现、图标无独立黑色悬停底，以及全文回答无 Collapse。

## 实际联调的范围

工作区使用 `E:\Selfie - 副本`。正式 TypeScript 构建经统一发布脚本进入 `codey/web`，浏览器读取这一份实际发布产物。

原生协议联调使用本机确定性模型服务。真实经过浏览器操作、Codey HTTP、原生 Chat Completions / Responses 请求、SSE、真实工作区读取和 UI 恢复；关键链路没有注入前端事件代替后端执行。没有调用用户的外部模型 API，也没有调用照片开放平台或重新执行其探测脚本。

已通过的实机路径：

1. 两种协议均在模型请求结束前显示第一段正文；最终更新保持原段落节点。
2. 运行中补充下一条消息，在源请求完成前保持排队，只发起一次后续模型请求；刷新后两条回答各保留一次。
3. 两种协议均原生发出问题，锁定普通 Send，确认答案后接续原始工具历史；实际读取 Selfie 的 `pyproject.toml`，展开可见真实内容。
4. 对原始问题重复提交同一答案不会重复调用读取工具；选择不会自动授予命令执行权限。
5. 原有真实 HTTP / 子进程审批回归继续覆盖允许、拒绝、非零退出、丢响应和恢复。
6. 点击真实报告可在右侧预览、看源文、复制、重新读取；Research 使用相同侧栏，次级菜单可进入证据。
7. 在实际打开的 Codey 页面点击 `beauty/cache.py`，成功解析为 `selfie/beauty/cache.py`，右侧显示真实源码。

长历史另有正式浏览器用例：400 条已保存消息先挂载 100 条，再补 100 条和显示全部，既有尾部节点保留。此处使用固定测试历史，以便复跑；没有把它伪装成模型实际执行记录。

## 最终验收结果

- 前端：34 个测试文件、**132 项通过**；TypeScript 和 Biome 格式检查通过。
- 最终正式浏览器组：**76 项通过**，包括上述真实工作区/协议链路、图片键盘放大与局部失败、长历史、审批、复制的悬停/键盘/触屏操作。
- 最终后端与架构组：定向 execution/transcript/UI 回归通过；2026-10-17 最终全量 pytest 为 **8784 项通过、8 项跳过、1520 个子测试通过**。
- Ruff 通过；全树 mypy、Pyrefly 和 ty 检查 431 个源文件无错误；正式构建、发布一致性和 Git 空白检查通过。
- 扩大回归遍历非 manual、非 stress 集合，得到 **8645 项通过、8 项跳过、1520 个子测试通过**，并报出 8 项失败。逐项核对后，同步了问题工具目录、提示词哈希、工具详情字段、桌面启动替身和消息操作 DOM 的既有契约；其中触屏复制不可点击以及复制反馈离开鼠标后消失属于实际样式问题，补测后修复。所有这 8 项失败均已定向复测通过，并包含在最终验收组内。修复后没有再次重跑这整个扩大集合，不把该次扩大运行描述为一次完整全绿运行。
- 后续四项对话区缺陷按 TDD 先测红再修绿：输入队列明确拒绝恢复、Research 运行身份、Markdown 代码字面量和第 21 张本地图片均已定向通过；真实 Codey HTTP 服务与 Playwright 页面验证也已通过。
- 额外的浏览器扩大组已有 443 项通过；其 4 个消息复制旧断言由上述新的复制行为验收替代并验证。没有忽略失败测试来取得通过结果。

实际页面中的正文与工具详情截图：

![实际 Codey 正文与工具详情](../.e2e-artifacts/conversation/screenshots/live-conversation.jpg)

两种协议的流式/队列与问题恢复截图位于同目录的 `stream-queue-*.png`、`question-*.png`。

## 测红后修绿的缺陷

| 失败现象 | 修复 |
|---|---|
| 读取结果只剩 200 字摘要 | 后端分开投影摘要与有界保留详情，并分别标识截断 |
| 模型粗体结束前的空格使星号原样显示 | 解析器专门修正该边界，保留代码字面量 |
| 行内代码管道符破坏表格单元格 | 只保护表格代码跨度，代码围栏保持原样 |
| 内层命令滚轮也关闭外层跟随 | 按实际可滚方向决定滚动归属 |
| 流式增量重建已完成段落或重复注册选区监听 | 保留稳定块，仅更新尾部，每个容器只登记一次选区监听 |
| 有界流式前缀与完整长答案形成重复消息 | 最终答案替换相同流中的前缀 |
| 工具详情重建后关闭、无实测耗时 | 按工具身份保存展开状态，服务端测量任务时间 |
| 无效编辑仍改变排队记录 | 校验先于状态变更，编辑使用当前修订 |
| 已完成收据占满待发容量 | 分开计算待发容量，保留数据库中的幂等身份，内存只缓存近期完成记录 |
| 快速结算淘汰缓存后提交回调找不到原记录 | 从同一持久收据读取权威状态 |
| 旧的 Resume 请求清掉 Stop | 修订校验成功后才清除停止信号 |
| 提交成功但重启后没有最终记录，界面把它隐藏 | 恢复为可见的未确认状态，不自动重发 |
| 问题回答恢复时误读模型选择参数 | 保存并使用已准入的类型化连接快照，保持原连接修订和模型 |
| 项目模式或只读条件在回答后丢失 | 保存原模式、授权与预算；HTTP 授权从规范输入的真实文字推导 |
| 旧待答问题被较新的 256 条历史挡住 | 待答与待投递记录独立于历史窗口查询 |
| 更新其他问题重建正在输入的表单 | 未变化的修订和交互阶段不重新渲染 |
| Windows 临时目录因 SQLite 句柄无法清理 | 事务独占连接并确定关闭；活跃队列任务继续持有其应用生命周期 |
| 触屏用户消息复制不可点击、鼠标离开后复制反馈消失 | 清除操作组的多余事件拦截，反馈规则覆盖相同操作组；12 项复制浏览器回归通过 |
| 提交后的服务器 500 被当成确定未送达 | 保留为未确认状态，只能查询；不提供可能重复执行的自动重发 |
| 新声明文件使运行时源码测试转译失败 | 类型声明明确排除出可执行源码集合 |

这些问题均保留对应回归测试。运行时记录、问题、队列和 UI 状态仍使用版本 1。

新 Markdown 路径替代了旧的手写解析器与无调用方的分块/代码块辅助出口；没有为了旧测试保留一套备用渲染器。

## 复跑入口

```powershell
# 前端
cd E:\codey\frontend
npm run typecheck
npm run format:check
npm test
npm run build
cd ..
python -X utf8 tools/publish_frontend.py
python -X utf8 tools/publish_frontend.py --check

# 真实工作区、正式页面及两种原生协议
$env:UI_SMOKE_PROJECT = 'E:\Selfie - 副本'
$env:UI_SMOKE_ARTIFACT_DIR = 'E:\codey\.e2e-artifacts\conversation\screenshots'
python -X utf8 -m pytest tests/test_conversation_live_browser.py tests/test_document_browser.py tests/test_conversation_history_browser.py -q

# 原生问题暂停与恢复、队列、授权、详情和图片
python -X utf8 -m pytest tests/test_conversation_question_http.py tests/test_conversation_questions.py tests/test_conversation_input_queue.py tests/test_conversation_input_routes.py tests/test_conversation_entry_auth.py tests/test_conversation_edit_preview.py tests/test_document_preview.py -q

# 静态与架构约束
python -X utf8 -m ruff check .
python -X utf8 -m mypy codey
python -X utf8 -m pyrefly check codey --python-version 3.13
```

测试输出和浏览器截图位于 `.e2e-artifacts/conversation/`。目录为本机验收产物，不进入应用运行依赖。

## 适用边界

- 公式和 Mermaid 已接入本地依赖；复杂或不合法图表超过预算时保留源码，不宣称所有 Mermaid 图种均支持。当前历史仍采用有界 DOM 分批挂载，没有引入完整虚拟列表。
- 原生支持流式的连接显示真实增量；网站模型和非原生文本协议不制造打字动画。
- UI 快照仍保留最近 500 条以控制首屏；完整展示历史写入独立 transcript archive，首次保存会导入 500 条窗口之外的 incoming messages。已被旧版本裁掉且从未进入 archive 的内容无法凭空恢复。
- 图片文件上限为 2 MiB；错误只影响该图片，SVG 不作为可执行内容进入页面。
- 编辑差异基于实际写入前后内容。超出预览预算或缺少可靠旧内容时明确显示不可用，不用模型参数拼造差异。
- 长历史、控制状态和丢响应使用确定性本机用例；不据此声称已触发全部外部模型的实现差异。
- 已完成真实 Codey HTTP + Playwright 回归：新聊天创建后可立即发送，切换会话和刷新保持 session identity；归档分页、搜索、实时 shell 输出和普通 run execution snapshot 使用隔离状态目录验证。未连接外部模型 API，也未修改 `E:\Selfie - 副本` 业务文件。
- 发布后的 bundle 与源码元数据已经重新校验；提交与推送状态以仓库当前 Git 记录为准。

## 2026-10-16 后续核对

本轮重新核对了工作区中的未提交实现。此前两个定向失败（shell close gate、plain-chat terminal reply）在当前源码上各自重跑通过。新增的普通 `run` 观察 wiring 红测先因 `managed_tool_fns()` 在没有 `managed_outputs` 时直接返回 `None` 而失败，随后改为仅有 `execution_outputs` 时仍走 `run_command_raw` 并传递 session/run/execution identity；该测试与 execution/transcript/structured-failure 定向组共 14 项通过。

本轮实际复跑：前端 34 个测试文件 132 项通过，`typecheck`、Biome、正式构建和 `publish_frontend.py --check` 通过；`tests/test_ui_manual_retry.py` 23 项通过；隔离状态的真实 Codey HTTP/Playwright live/history、问题、队列、授权和文档预览组通过。2026-10-17 全量 pytest 为 **8784 passed / 8 skipped / 1520 subtests passed**。未调用外部模型 API。

补充复核：Read/Search 分组箭头与 Working/Ran 共用 `chevron-down.svg`、`--text-dim` 静态亮度和 `--text` 悬停亮度，并覆盖 Chromium `-webkit-mask`。当一个会话运行时，其他会话的 composer 仍可直接 Send；Stop 仅显示在运行所属会话。同一会话的后续输入才进入 InputOutbox。正式 bundle 已重新发布；`tests/test_ui_workflow.py` 的跨会话队列浏览器回归通过。

本轮继续做了真实三会话并发验收：在隔离状态的 Codey HTTP 服务和 Playwright 浏览器中，A/B/C 三个 session 同时建立本地确定性模型的原生 Chat Completions 与 Responses 流（每种协议各 3 个请求），没有出现 `Another chat is running`。切换到 B 点击真实 Stop 后，后端只结束 B 的 `run_id`，A/C 继续完成。该回归位于 `tests/test_conversation_live_browser.py::test_real_three_sessions_run_concurrently_and_stop_only_targets_selected_session`。

## 2026-10-17 真实 A-F 联调收尾

新增 `tests/test_uncommitted_live_browser.py`，使用正式 `codey/web` 产物、真实 `CodeyHTTPServer`、Playwright、隔离状态目录和本地确定性模型 HTTP 对端，覆盖 provider 结构化失败与固定恢复入口、两段最终回答复制的原始 Markdown、公式/Mermaid、归档历史搜索、问题草稿刷新恢复，以及获批 shell 在子进程退出前显示已 flush 输出。该组 5 项（含两种协议参数）通过；原有 live、审批、文档、历史浏览器组再通过 16 项。

联调中发现并修复两个后端生命周期问题：公开结构化事件投影移到 SSE 边界，避免改变内部事件订阅者的字段；shell `Running` 观察事件通过独立浏览器事件发布，不写入结算记录、不触发重复执行，最终 `shell_result` 仍以同一 approval id 原位更新。前端同时补齐了正式消息层的 session 列表依赖，确保 `Copy final answer` 汇总最新权威部分。

最后一次扩大回归实际结果：`8784 passed, 8 skipped, 1520 subtests passed`。前端 `npm run check`、正式 `npm run build`、`python -X utf8 -m tools.publish_frontend` 和相关真实浏览器组均通过。测试使用本地确定性模型服务，未调用外部模型 API，也未修改 `E:\Selfie - 副本` 业务文件。

这次实测还发现并修复了 Stop 的后端缺陷：任务取消事件没有进入 API provider，且阻塞的 HTTP body 读取没有主动被取消。修复后，run-scoped stop event 会传到 provider 和 transport cancellation watcher，确保目标流立即中止；先红后绿的 provider 回归位于 `tests/test_api_stream_terminal_and_cancelled_generation.py`。

实时输出 D 仍是部分完成：shell 审批和普通 run 已有 execution identity、增量 UTF-8 观察和有界快照，但当前前端仍以轮询读取，没有专用 `execution_output_available` SSE；observer 仍可能按 chunk 写 SQLite，命令卡的 stdout/stderr 展示和 DOM 重建后的选区/展开连续性也未完成规格中的全部验收。因此不能把 D 或六项整体标为全部完成。

## 2026-10-16 并发与恢复复核

本次继续复核后，运行契约保持为“同一 session 串行、不同 session 并行”。`RunRegistry`、HTTP 提交、浏览器 worker、SSE 状态投影和 Stop 均按 `session_id`/`run_id` 分开；没有恢复全局单任务锁。定向后端与资源回归 `17 passed`，三会话真实 HTTP/Playwright 回归仍通过；正式前端 `34 files / 132 tests`、typecheck、Biome、build 和 `publish_frontend.py --check` 通过。

新增的 composer ready 闸门修复了真实浏览器竞态：服务端 UI 状态、问题和输入队列完成初始恢复前，textarea 不可发送；恢复完成后才解禁，避免恢复响应覆盖用户刚输入的草稿。对应的单项发送、草稿刷新和正文浏览器回归已通过。

旧的全量日志不再代表当前结果：架构阈值、全局忙状态、Shell 单一 `.shell-output` 节点和旧审批文案已按现行契约更新。手工重试整组的顺序敏感失败已先红测复现，再修复首次发送准备态、接受响应后的权威状态确认、晚到 reply/本地告警投影；`tests/test_ui_manual_retry.py` 现为 23 项通过。测试发布步骤固定为 `npm run build` 后执行 `python -X utf8 -m tools.publish_frontend` 和 `--check`，浏览器读取 `codey/web` 正式产物。
