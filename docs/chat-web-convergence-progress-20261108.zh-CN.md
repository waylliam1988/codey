# Chat 联网收敛实施记录

日期：2026-11-08。起点：`45ba2dec`。目标按用户指定的五批实施、验证并分别提交；后续用户追加第六批，独立提交。

## 约束与归属

- 不创建 worktree，不排查电脑卡顿；用户已有 `codey/runtime.json` 修改保持原样，不提交。
- 入口一次解析联网许可及明确要求；下游读取冻结 TaskPolicy，恢复不扩大旧授权。
- 工具快照一次生成契约，原生/兼容协议、示例和调用校验共同消费；不增加 Chat 执行器分支。
- 页面获取复用 gateway/provider，事实由已结算回执投影，完成检查与上下文共享查询。
- 不增加前置语义分类模型、第二套执行循环、重试器、来源状态库或通用语义裁判。
- 搜索不自动产生打开所有候选页面的义务；结构化必需来源继续检查，自然语言理解单独实机评估。
- 关闭 Web 工具不等于对子进程强制断网，Shell 沿用现有审批与网络策略。
- 新增和迁移边界使用明确类型，不引入 Any、无类型容器或新的类型检查豁免。
- 每批运行相关测试、Ruff、类型检查和一次对应 KoboldCpp 实机；不主动运行完整 pytest、覆盖率或打包测试。

## 批次状态

| 批次 | 内容 | 状态 | 提交/证据 |
|---|---|---|---|
| 1 | 统一入口策略解析及各入口传递 | 已提交 | `15bf50ed`；入口/CLI/控制/恢复回归、Ruff、三类检查器、precision 通过；实机暴露的后续问题已保留 |
| 2 | 统一本轮工具契约、question_ref、错误反馈 | 已提交 | `1eaa8714`；97 项主回归、42 项补充回归；实机首次直接搜索，答案质量失败单独记录 |
| 3 | 合并 Web 获取分支及来源事实计算 | 已提交 | `463c6f7a`；65 项相关测试、Ruff、三类类型检查和 precision 通过；受控 SPA 实机取得渲染正文 |
| 4 | 调整完成要求、精简重复提示 | 完成待提交 | 33 项相关测试、全部静态检查通过；受控美食实机读取正文，答案无夹具外店铺/价格 |
| 5 | 原始会话验收：行为与答案质量分别审阅 | 待实施 | 待记录 |
| 6 | 来源失败收尾及原生 run 异常执行回执 | 待实施 | 用户追加：指定 URL blocked 回归、无 URL 强制来源失败收尾、超时/管道错误回执、旧 source_gaps 提示 |

## 当前发现

- Chat/auto 联网授权依赖 `explicit_web_intent` 词表，原句“搜索一下”漏判。
- `allow_web=False` 需要与未指定严格区分；headless 尚未传递该字段，控制入口只传 task/intent。
- 普通 Chat 的可选 question_ref 示例仍填写 q1，未注册时会被拒绝。
- `sources_open_required` 与是否允许联网已有独立字段，继续复用，不新增三态 UI。
- 当前已配置 provider 的页面读取由 gateway 调用；第三批先审计现状，删除实际重复，不为凑改动增加抽象。

## 继续工作入口

第四批完成后先实施用户追加的第六批，修复已确认的生产回归，再进行第五批最终会话验收。第六批仍独立提交，避免最终验收重复撞到已知故障。

## 第一批：入口授权

- `derive_entry_auth` 唯一解析默认许可：Chat/auto 默认 web.read，project 等显式请求；不再用任务关键词授予 Web 权限或生成 source-open 要求。
- `allow_web=False` 优先于默认和 requested；允许调用不等于必须打开页面。
- HTTP 继续使用同一解析函数；headless 新增可选 allow_web；CLI 区分未指定、--allow-web 与 --no-web。
- 控制协议复用 allow_web 布尔值：会话可保存默认，消息可显式覆盖；后续未指定不会把会话关闭设置改回默认。
- TaskPolicy 持久化格式未变，恢复仍读取原策略，不应用新默认。无 UI/打包配置变化。
- ty 发现既有 `_launch_browser` 用 `dict[str, Any]` 展开 Popen 参数造成返回类型歧义；改为显式参数，去除该边界的 Any，浏览器启动对应 2 项回归通过，运行行为保持一致。

### 验证

- 入口、headless、只读约束、工作区完成相关 81 项通过；控制入口新增 2 项通过。
- CLI、原策略恢复、浏览器启动相关 47 项通过；CLI 新增关闭区分及原始问法脚本相关 19 项通过（包含重复执行的 CLI 测试，不相加成独立用例数）。
- Ruff 通过；mypy 全生产树通过；Pyrefly 0 errors（保留原有 suppressed/warnings）；ty 在移除 Popen Any 后通过；precision gate 通过。
- 实机：`.e2e-artifacts/chat-web-convergence-20261108/batch1-nearby`，原句“东百中心 ？你帮我搜索一下，这里附近有什么好玩的”，KoboldCpp Gemma，生产 headless/kernel，真实网络，未增加工具提示。
- 结果：工具权限已可用，成功执行 3 次搜索，未再出现 snapshot 缺 web_search。第一次 question_ref 错误仍待第二批契约修复。
- **答案质量失败**：RSS 返回“东”字百科等无关结果，模型没有读取正文，最终把地点认定为济南章丘并推荐景点，证据不支持。原始产物保留，不计绿色验收。
- 此样本是独立单句，未带前轮福州对话；第五批必须增加同会话完整追问，不能把独立单句当作完整原始会话对照。

## 用户追加：第六批范围

- 用户报告相关 114 项中 1 项失败：`test_real_entry_delivers_failed_page_answer_without_repeating_fetch` 实际 done，预期 blocked。应保留断言，确认指定 URL 义务不依赖已移除的关键词授权，不把新默认误当作已满足要求。
- 强制查证但未指定 URL 时，覆盖计算不能丢掉搜索发现页面的真实失败；必需 URL 覆盖与实际获取尝试分别表达，同一 gate 决定收尾。
- 原生 run 超时、管道读取失败、排空超时统一生成 ExecutionReceipt，记录 process 作用域、已知失败分类和未知退出码。
- 删除 `research_completion_feedback` 独立扫描并要求复制 source_gaps 的旧规则，复用统一来源事实；必需 A 失败但 B 未尝试时先继续 B。
- 第六批单独提交，运行用户给出的 9 个相关测试文件及新增异常回执测试，并进行对应 KoboldCpp 实机；不扩大到完整 pytest。

## 第二批：本轮有效工具契约

- 删除静态注册表两处统一注入 question_ref/q1 的逻辑；基础工具定义不包含问题绑定。
- 普通 Chat 不暴露 research_plan，不因获取预算启用问题绑定；evidence/现有 scoped research 在快照阶段加入必填字段、已注册 qN 枚举及对应示例。未计划的绑定流程只先展示计划入口。
- whole Research 的契约不再需要事后剥离 question_ref 示例/字段，减少重复解释。
- 删除执行委托中按模式再次检查 question_ref 的分支，已有 normalize_turn 与 execute_turn 消费同一个冻结 schema；领域内知识引用归属校验保留。
- 修复 source_search 别名白名单放行未声明 question_ref 的确定性缺口；别名额外键与实际声明合并，避免拒绝本轮声明的新参数。迁移修改的校验边界使用明确 ToolSpec/ToolArgs。
- 工具不可用反馈不再要求重发原调用；参数错误仍给出参数修复指导。现有重复诊断停机机制保持单一归属。

### 验证

- 契约/问题归属/冻结执行/研究导航与 finding 97 项通过；通用参数、数组递归、冻结绑定、导航等补充 42 项通过（与前组有重叠，不累加为独立用例数）。
- Ruff、mypy、Pyrefly、ty、precision gate 通过。新增代码无 Any；删除了已有校验函数的动态类型参数。
- 实机：`.e2e-artifacts/chat-web-convergence-20261108/batch2-food`，原始福州美食问法，真实 KoboldCpp/网络/生产入口。
- 契约行为通过：首轮直接成功 web_search，无 research_plan/question_ref 错误，无参数修复循环。
- **答案质量未通过**：只搜索未读正文，最终给出超出摘要的店铺/做法细节且未附引用。第五批继续逐项审阅；第四批以任务指导改善正文核验及未知边界，不引入“搜过就一律必须开正文”的门禁。

## 第三批：获取链审计与来源事实展示收敛

- 审计确认前序改动已让指定 URL、搜索结果和 SPA 共用 gateway/provider；没有为凑接口再增加获取层。浏览器自身的 PDF 及 HTTP 回退仍属于浏览器策略，未改成 HTTP 先行的反向循环。
- 删除 task_guidance 独立读取 session.opened_sources 的第二份摘要，统一由 source_gap_context/evaluate_source_coverage 提供来源事实及继续阅读指导。
- 没有指定 URL 的 Chat 也可显示已读来源；ledger 的请求/重定向地址现在同样进入工作视图，查询不修改 session 或 ledger。
- 修正 gateway 模块说明：后端选择与渲染升级归 provider，gateway 负责网络规则、文档与来源记录。
- 65 项相关测试通过；Ruff、mypy（657 files）、Pyrefly、ty、precision 通过。两次启动 pytest 因误写不存在的测试文件而未收集测试，随后使用实际文件完成验证，非生产故障。
- 实机产物：`.e2e-artifacts/chat-web-convergence-20261108/batch3-spa`。真实 KoboldCpp、生产入口与浏览器执行 JavaScript，网络响应为固定夹具，不能表述成真实网站验证。
- 行为：一次 open_url 成功、done；正文确实交付。人工核对回答的多模型、套餐、客户端、配额均可追溯至 87 字夹具，没有虚构价格或稳定性。仍缺显式来源标注，第四批改善指导；单个样本不能证明整体质量提升。

## 第四批：证据使用与完成义务边界

- Chat 的联网指导统一在现有 task_guidance_for_policy，按冻结授权加入；没有新增意图分类调用、关键词匹配或执行循环。
- 明确语义选工具、执行用户的搜索要求、复用会话地点；搜索摘要可以足够找官网，超出摘要的细节应核对正文，链接遵守输出格式，失败保留未知。
- 没有新增“调用过搜索就必须打开正文”的完成门禁。新增行为测试确认普通问候、仅搜索找官网、可选获取失败均可正常完成；指导不泄漏到关闭 Web 的 Chat 或其他模式。
- 33 项相关测试通过；Ruff、mypy、Pyrefly、ty、precision 通过。
- 实机：`.e2e-artifacts/chat-web-convergence-20261108/batch4-food`，真实 KoboldCpp、生产执行链、固定来源响应。模型搜索后打开正文，最终仅介绍鱼丸、肉燕、锅边糊并给出来源链接，未编造夹具未提供的店铺、地址、价格或排名。行为与本样本事实核对通过，不推断所有真实网络查询均已改善。
