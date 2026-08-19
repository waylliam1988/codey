# 版本更新记录

[English version](CHANGELOG.md)

这里记录 Codey 从最早版本到现在的发布历史，最新版本排在最前面。

## 0.4.3 - Source Connector Boundary + Query Planner Dry Run v1

- 新增 `codey/research/source_connectors.py`：提供纯 source connector 边界，
  包含 `SourceConnectorSpec`、`SourceConnectorRegistry`、`SourceHit`、
  `FetchedSource` 和 `SourceConnectorResult`。内置 registry 现在 ship
  `local_file`、`csv_tsv`、`json_file`、`arxiv`、`pubmed` 的 fixture/local
  覆盖；`openalex` 明确后移，`rss` 只是 optional，所以两者不算 shipped connector。
- 新增 `tests/fixtures/research_connectors/` recorded fixtures，覆盖本地文本、
  CSV、TSV、JSON、arXiv Atom 和 PubMed XML。fixture 解析会生成稳定的
  `source_ref`、`connector_source` 和 `source_hit` refs。本地文件读取限制在显式
  allowed roots 内，CSV/TSV 使用 Python `csv` 标准库解析，URL-backed recorded hit
  在 fixture fetch 前仍会经过 Research URL guard。
- 新增 `codey/research/query_planner.py`：确定性生成 ResearchPlan dry-run。
  它只消费 proof-review gap 和 connector registry metadata，输出有界
  query candidate、source preference、max bounds、reason code、warning 和稳定
  `research_plan:<16 hex>` ref。医学/生命科学问题偏 PubMed；论文/预印本/ML
  问题偏 arXiv；本地表格、文件、JSON 问题偏 local connectors。
- Run Trace 新增有界 `research_plans` summary，在 proof review 后记录 plan ref、
  question digest、proof ref、query count、source preference ids、max bounds、
  warning 和 reason code。它不保存 query 文本、raw prompt、source body、抓取页面、
  raw URL 或 raw absolute path。
- Capability Registry 和 Event / Capability Matrix 新增 `research_source_connectors`
 、`research_connector_search` 与 `research_query_planner`。架构测试锁住
  connector/planner 模块不接 provider adapter、browser、tool runtime、
  server/TaskRunner runtime layer、Ghost runtime、subprocess 或 plugin loader。
- 默认启用 0.4.3b 的 PubMed/arXiv connector-aware search/fetch 路径，但仍复用
  现有 Research runtime open/fetch 执行层。controller 现在对模型暴露语义明确的
  `open_result`、`reopen_source`、`open_hit`，不再暴露重载的
  `open_url(result_id/source_id/hit_id)` 形状；这些动作会编译到 runtime 打开路径。
  connector hit 会作为普通搜索结果出现，只有经过 connector boundary fetch 并进入
  opened-source ledger 后才可能成为可引用 evidence。
- 在启用前收紧 connector 边界：PubMed recorded fetch 只接受
  `pubmed.ncbi.nlm.nih.gov`，arXiv recorded fetch 只接受 `arxiv.org`；
  arXiv fixture URL 统一 canonicalize 到 `https://arxiv.org/...`；fixture parser
  和 recorded fetch 都会拒绝 malformed PubMed/arXiv ID；`SourceHit` 审计
  metadata refs 会过滤 secret-looking 值，`SourceHit` 和 `FetchedSource` 的
  scalar 审计字段都改成 allow-list；connector catalog id/kind 会拒绝
  secret-looking 或非 canonical code，catalog hint 以及 connector result 的 warning/error
  code 会过滤 secret-looking 值；malformed limit 会回落到有界默认值；CSV/TSV 读取会多读
  一行再判断是否真的 truncated，避免刚好等于上限时误标。
- 收紧 `RunTrace.record_research_plan()` 和 planner trace payload：source preference
  只接受 connector-id 形状，list 字段只接受 list/tuple/set，字符串不会再被逐字符
  迭代，`None` 也不会抛出；reason/warning 会过滤 secret-looking 字符串。相邻的
  evidence-ledger 和 proof-review trace sink 也改用同一套 trace-safe
  reason/warning 规则，同时不会误删 `token_budget_exceeded`、
  `authorization_required` 这类合法审计 code。proof 已通过且没有 gap 时，planner
  现在产出 no-op 的 `proof_ok_no_required_followup` plan，不再生成 follow-up query
  candidates；no-op plan 不再带无关 warning。Run Trace 也把模型可见 controller
  action contract hash 和编译后的 runtime tool contract hash 分开记录，并记录有界的
  connector fallback error summary，不保存 raw request data。
- 浏览器 Research search 在普通 Research 运行中显式复用一个专用 Research
  profile/port；自定义 CDP port family 不再回落到默认网页模型 provider 端口池。
  直接构造 `BrowserSearchProvider()` 仍保持默认 isolated，浏览器 attach/端口等待也
  收回到 20 秒，让卡死失败路径更快返回。取消现在不会被 isolated CDP 启动重试或
  search page 导航重试吞掉；manual connector A/B harness 也改为和生产 Research
  一样复用 non-isolated Research browser。
- connector-aware live search 现在用和 dry-run planner 相同的 safe term 边界构造
  PubMed/arXiv API query；这个边界会遮掉高置信 secret marker/value 窗口，例如
  `api key ...`、`password ...`、
  `api key is ...`、`password is equal to ...`、`password is set to ...`、
  `api key called ...`、`api key named ...`、`client secret known as ...`、
  `password is configured as ...`、`password is configured as known as called ...`、
  `password - is - configured - as - known - as - called - ...` 这类过度填充或标点分隔
  connector phrase、`密码 是 ...`、`密钥等于 ...`、
  `private key is ...`、`client_secret=...`、`access_token ...`、`passphrase ...`、
  `token abcdef`、`cookie abcdef`、`jwt abcdef` 这类 value-shaped contextual marker、
  `Authorization: Bearer ...` 这类 marker/value 窗口；`api key one two three ...`、
  `password correct horse battery staple ...`
  这类明确 secret marker 后的多词 value 会遮到有界领域词边界。planner preview、
  connector digest 和 live PubMed/arXiv request 中都只保留清洗后的安全领域词；URL
  或本地路径会移除，清洗后没有安全 terms 时才跳过 connector lookup。live connector
  routing 和 request assembly 会复用同一个
  `SafeConnectorQuery`，不再从 raw text 重算。浏览器 search 会先于 connector lookup 启动，connector 请求有更短的全局预算，
  普通 browser 结果会保留 query string 区分；direct PubMed/arXiv URL 在 connector lookup
  失败时会回退到 browser fetch。connector result digest 也只基于 shared safe query，
  live connector 的 tool name 和
  User-Agent 使用不含产品名的中性标识。Research JSON codec 不再接受
  `open`/`fetch`、`queries`、`done.summary` 这类旧工具或参数 alias；fallback
  contract 本身也不再携带 alias 层，并且只接受恰好一个 plain JSON object，且顶层只能有
  `tool` + `args`，不再接受 `name`、顶层参数字段、额外顶层字段、额外 JSON object、
  array、markdown fence 或 prose wrapper。
  仍输出旧名字的 provider 可能多走一轮 repair，但 provider 怪癖应留在 provider
  adapter 或 repair prompt，而不是放回通用 parser 做隐式兼容。
- dry-run planner 和 live connector search 现在共用同一套领域路由词表，包含
  genetic/genomic 以及 RAG/NLP/retrieval/benchmark 等常见论文检索词。registry 的
  status、shipped 和 capability flag 现在是 live search/fetch 的权威边界；
  connector 剩余预算不会向上取整超过真实 deadline，`JAK/STAT` 这类安全科研斜杠
  术语会保留，`docs/ADR2026/research`、`Docs/ADR/Plan`、`ProjectX/ConfigV2`
  这类 path-like slash token 会在出站前丢弃。secret redaction helper 现在区分
  独立敏感词 marker 和密钥形状，`secreted`、`secretion` 不再被当成 `secret`。
  共用 Research shape helper 现在覆盖 connector id、generated ref、digest ref 和
  connector limit。
- 新增 `codey/research/done_finalizer.py`：一个很窄的 deterministic citation
  compiler，会在 Research 报告质量门前运行。它只编译可靠的 source-id/contextual ref
  和可解析旧来源表里的数字引用；最终 `来源` 表只从已经打开且保存过 evidence excerpt
  的来源生成；只打开但没有 evidence 的来源会被移除；缺少引用支撑的论断仍由质量门
  退回，不由 compiler 自动补引用。数字引用和 source-id 引用分开绑定，所以混用
  `[s1]` 和旧数字来源表时不会被陈旧来源表重绑；指向不同 URL 的重复旧编号会被拒绝，
  单来源且无歧义的编号漂移仍可归一化。source-id 泄漏检查覆盖 heading 前言和报告正文；
  `来源` section 改为逐行扫描，允许真实来源标题里的 `Analysis of [S1]`，同时拦截
  另起一行的 `note [s9]` 或 `source_id=s9` 这类上下文泄漏。无可引用来源报告会先被
  重渲染成标准 section 再进质量门，Run Trace 只记录有界 compilation summary。
- 新增共享的 `codey/research/citation_scanner.py` helper，让 done compiler 和
  report-quality gate 共用同一套 citation / source-id 扫描规则，避免后续分叉。report
  quality gate 也顺手拆成几个小 helper：missing section、source-id leak、no-citable
  report、provenance、source table、body citation 和 source-quality warnings。
- Qwen 现在只等待 composer 可交互且页面不在生成中，再填入消息；随后确认受控输入框
  仍保留完整文本且发送按钮可用。如果 hydration 已经清空草稿，就在点击前停止，不会
  发送空消息。这个路径去掉了发送前固定 composer settle 窗口，同时保留点击后不重复
  整轮发送的边界。浏览器 PDF 请求也改用中性 User-Agent。
- 已按 provider 串行跑 connector smoke/A-B，结果按行原子落盘。DeepSeek 显示
  PubMed connector 明显改善目标来源选择；MiMo 和 StepFun 的 connector arm 能打开
  PubMed 目标 host；Qwen 在 provenance 修复后改善 arXiv；DeepSeek/Qwen/MiMo/
  StepFun/GLM 至少有一个 recorded arm 能打开 arXiv 目标 host。部分 run 仍停在
  `max_turns` 或 protocol repair，GLM PubMed 多次尝试后因 provider 限流暂停，所以
  这里证明的是 source selection smoke，不声称 proof-quality 全模型提升。

## 0.4.2 - Research Proof Quality Gate + Planner Signals v0

- 新增 `codey/research/proof_quality.py`：对 `ResearchRecord` 和 durable
  Evidence Ledger read model 做 deterministic proof review。它检查 answer
  coverage、citation、已打开来源 evidence、locator/source 一致性、supports
  relation、assumption、反证/限制处理和 source-trust warning，不调用模型，也不读取
  raw source body。
- 新增 `codey/research/completion_gate.py`：把 Research queue completion 规则抽成很薄的
  边界。queued `research` / `open_question` work item 现在只有在 proof review 通过并
  生成 `research_proof:<16 hex>` 后才会完成；普通手动 Research 仍然会正常结束，只记录
  proof metadata，不被这个 queue gate 阻塞。
- `ghost/work_queue.py` 仍然不 import Research runtime。它只校验 research/open-question
  completion 必须带生成形状的 `research_proof:<16 hex>` primary proof；真正的 proof
  判断由 TaskRunner 调用 Research completion gate 完成。
- Run Trace 新增有界 `research_proof_reviews` summary，只保存 proof ref、
  queued-question digest、布尔结果、answer coverage score、计数、reason code，以及有
  record 时的 record id/digest；missing-record gate block 也会留下可审计 proof
  review。通过的 proof review 必须带合法 record id/digest，重复 proof summary 会按
  proof/question/reason identity 去重；trace 不保存 queued question 原文、planner signal
  文本、raw prompt、raw model response、raw URL、raw path、source text 或已抓取页面。
- queued Research proof review 会按 queued item title 重新计算，所以 strict continuation
  的包装文本不会稀释 answer coverage。gate 也不会信任 stale precomputed review，而是按当前
  `ResearchRecord` 和 durable ledger 状态重新判定。
- `research_proof:<16 hex>` ref 现在绑定 question digest、record id/digest 和 proof
  result，后续 audit/planner 能区分这份 proof 审的是哪个 queued question，但不会保存问题原文。
- proof 语义改成 fail-closed：结论/关键证据 claim 只有在 `status=evidence_backed`、
  自身有 `evidence_refs`、并且 `supports` relation 指向其中一个 ref 时，才算被支持。
  `ok=True` 还必须包含反证或限制处理。
- Capability Registry 和 Event / Capability Matrix 新增 `research_proof_quality`，声明它是
  deterministic completion gate 和 planner signal producer。架构测试锁住 proof 模块不接
  provider adapter、browser、tool runtime、server/TaskRunner runtime layer、Ghost runtime
  或 plugin loader。
- 本版不改变 Research prompt、工具 schema、模型可见 tool result、Router、provider
  fallback 顺序、权限、UI、task receipt 或 SSE payload shape。小型 Research/Ghost queue
  A/B 已按 provider 串行通过 DeepSeek、Qwen、MiMo、StepFun 和 GLM；这不是大规模
  provider/prompt A/B。

## 0.4.1 - Evidence Ledger v2

- 新增 `codey/research/identity.py`：把 Research projection 共用的有界 identity
  helper 抽出来复用。Research object 和 evidence ledger 现在共享同一套 URL
  redaction / digest 路径，包括畸形 URL、无 host URL、query key 和 query value。
  项目和文件路径继续只保存 basename + digest，不保存 raw absolute path。
- 新增 `codey/research/evidence_ledger.py`：把完成后的 `ResearchRecord` append 到
  本地有界 `research/evidence_ledgers/<session>/<project>.json` read model。它记录
  source/evidence/claim/assumption/relation id、locator ref、计数、schema version
  和 content-addressed refs，为后续 proof quality gate 提供长期证据索引。
- Evidence ledger 写入 fail-open：缺 record、坏 record、坏 JSON、超大 ledger 文件或
  写入失败都不会打断 Research run。Run Trace 只记录有界 evidence-ledger write summary。
- Ledger 裁剪现在保持 graph closure：source/evidence/claim/assumption/relation
  map 达到上限时，Codey 优先保留最新的完整 record，丢弃更旧 record，而不是保留带
  悬空 refs 的 record。已落盘 ledger 在 load 时也会先做 graph validation。
  闭合性包括 claim 内部 evidence/assumption refs、assumption.claim_ref、
  evidence.source_id、evidence.locator.source_id 和 relation endpoints。
  Load-time allow-list validation 会拒绝未知 raw 字段、孤儿 map entry，以及 map key
  和 entry 内部 id 不一致的数据；非 canonical 的标量字段、以及
  evidence.source_id 和 evidence.locator.source_id 不一致的数据也会 fail-closed。
  已落盘 record counts 必须和 retained refs 一致，source `content_hash` 也只保留
  canonical hash；伪 `sha256:` content hash 会被清空，不会被重新 hash 后保存。
- `EvidenceLedgerStore.append_record()` 只接受 typed `ResearchRecord`；mapping fallback
  会被拒绝，避免 nested refs 把 raw URL、raw path 或疑似 source body 字段落盘。Digest
  ref 只保留真正的 `sha256:<64 hex>` 字符串；伪 digest 字符串会被重新 hash。
- 如果 malformed typed record 因 ledger closure 被裁掉，`append_record()` 现在返回
  `skipped=True / record_pruned_for_ledger_closure`，不会再报告成功写入。Candidate
  write 会先和已加载 ledger 隔离，只有新 record 通过裁剪且 candidate payload 通过完整
  canonical validation 才落盘，所以 malformed replacement 不会删除已有好 record，也不会
  污染下一次 load；没有写入新 payload 时返回已加载 ledger 的既有计数。typed record 的
  `to_jsonable()` 失败，包括 malformed nested object，会返回 `invalid_record`，不会从
  store 抛出。
- `TaskRunner`、server state 和 headless JSONL runner 现在携带可选
  `EvidenceLedgerStore`。用户可见 Research payload、task receipt 形状、UI 和 SSE
  event 都保持不变。
- Capability Registry 新增 `research_evidence_ledger`，Event / Capability Matrix 新增
  `research.evidence_ledger`。架构测试锁住 identity / ledger 模块不接 provider
  adapter、browser、tool runtime、TaskRunner/server 编排、Ghost runtime 或 plugin loader。
- 本版不改变 Research prompt、工具 schema、模型可见工具输出、Router、provider
  fallback 顺序、权限、UI、task receipt 或 SSE payload shape。这个 persistence-only
  版本不需要生产 live provider A/B。

## 0.4.0 - Evidence Kernel / Research Object Model v1

- 新增 `codey/research/object_model.py`：把每次 Research run 的 ledger 和最终报告
  review 确定性投影成有界的 `ResearchRecord`，包含 question、source、evidence、
  claim、assumption 和 relation 对象。
- Research result 现在内部携带 `research_record`，但 TaskRunner 保持 UI/SSE 的
  `research` payload 形状不变。Run Trace 只保存有界 record summary：id、
  answer status、source/evidence/claim/assumption 计数、unsupported claim 计数和 digest。
- v1 的 claim evidence binding 保守处理：claim 引用某个来源，不等于该来源下所有
  evidence 都能连接到它。只有 final claim 与 evidence claim 或精确有界摘录匹配时，
  才生成 `supports` relation，并且 evidence stance 必须适合对应报告段落。Claim
  `status` 只允许 `evidence_backed`、`unsupported`、`assumption`；支持、反证和限制
  方向由 relation kind 表达。反证 evidence 不能支撑 conclusion / key-evidence claim，
  非空未知 stance 也不能支撑结论。
- Search result、Ghost/local memory 和未打开来源都不算 evidence；evidence 必须来自
  本轮 Codey 实际打开过的来源。
- Research object 的 URL ref 会在 digest 前脱敏 userinfo 和 secret query-key 变体，
  包括 `client_secret`、`refresh_token`、`x-api-key`、`jwt` 以及 token/api-key
  后缀。query component 会在 URL digest 前 fail-closed 脱敏，包括 query key；
  畸形 URL、无 host URL 和畸形 userinfo head 也走同一边界。本地路径只保存
  basename 和 digest ref，不保存 raw absolute path。
- Capability Registry 新增 `research_object_model`，Event / Capability Matrix 新增
  `research.object_model`。架构测试锁住 object model 不接 server、TaskRunner 编排、
  provider adapter、browser、tool runtime、Ghost runtime、plugin loader 或文件写入。
- 本版不改变 Research prompt、工具 schema、模型可见工具输出、Router、provider
  fallback 顺序、权限、UI、task receipt 或 SSE payload shape。

## 0.3.20 - Run Details v1

- 新增 `codey/run_details.py`：把 RunLedger 和 RunTrace metadata 投影成短的、
  只读的用户可理解运行说明。它只报告工作类型、模型、使用的上下文、本地动作、
  安全决策、模型 fallback 和验证结果，不返回 raw prompt、raw tool output、
  源码正文、网页正文或 provider 错误 dump。
- 新增只读接口 `GET /api/run_details?session_id=...&run_id=...`。没有可用本地
  详情时返回 `available=false`，不会抛出给 UI，也不会写入状态。
- Run Details 读取 trace manifest 时复用有界本地 JSON reader，带 `MAX_TRACE_BYTES`
  上限，并校验 Run Trace 的 schema version 和 kind 后才使用 trace metadata。
- 新增 `codey/web/assets/run_details.js`，在任务终态 status/receipt 行加一个低调的
  `Details` 文本入口。Details 懒加载、在原地展开、只做内存缓存，不写入持久 chat state。
- Capability Registry 新增 `run_details` 能力，Event / Capability Matrix 新增
  `run_details.summary`。架构测试锁住它只是只读投影，不接 runtime dispatch、
  provider adapter、TaskRunner 编排、浏览器代码、插件加载器、raw trace viewer 或新
  SSE event shape。
- UI 设计基线新增 Run Details 规范：它是 inline receipt expansion，不是 drawer；
  保持单色、无背景卡片、无圆角容器、无彩色 warning 样式、无 topbar 入口，也不暴露
  RunTrace、PromptEnvelope、Policy Pipeline、Router、Ghost 或 Provider 等内部词。
- 本版不改变 prompt 文本、工具 schema、模型可见工具输出、Router、provider fallback
  顺序、权限、Research/Writer/Review 语义、task receipt 或 SSE payload shape。

## 0.3.19 - Built-in Profiles v1

- 新增 `codey/builtin_profiles.py`：Codey 内置默认策略边界的只读目录，固定声明
  `default`、`research_heavy`、`review_strict`、`local_only` 和 `beginner`。
- 新增 `docs/codey_builtin_profiles.md` 和 `tests/test_builtin_profiles.py`，锁住稳定
  profile id、JSON 导出、fingerprint、capability 引用、permission 默认值、
  provider scope、fallback posture、Local context 默认枚举、UI detail level 和安静的
  用户可见文案；`local_only` profile 明确不声明 Research permission default。
- `server.State` 现在持有内置 profile registry，`TaskRunner` 只携带这份 metadata。
  Profile 不参与 Router、provider fallback、permission 选择、prompt 组装、工具调度、
  UI、SSE、receipt 或 project config。
- Capability Registry 新增 `builtin_profiles` 能力声明；它仍然只是 metadata。
  架构测试会拒绝 built-in profile 模块出现 plugin loader 或 runtime host 形状。
- 本版不新增 profile picker、配置平台、插件系统、动态 import、prompt patch、
  provider 覆盖、mode 覆盖、权限放宽或 UI 改动。

## 0.3.18 - Event / Capability Matrix v1

- 新增 `docs/codey_event_matrix.md`：用可测试矩阵记录事件生产者、消费者、
  持久状态、模型可见性、UI 可见性、policy 要求、trace 要求、隐私边界和关联能力。
- 新增 `tests/test_event_matrix.py`，锁住 event id 唯一、capability / durable state
  必须来自已知清单、模型可见行必须接 Prompt Envelope / Run Trace、policy 行必须声明
  policy 边界，并防止 raw payload 边界回流。
- 矩阵单独声明由 `RunEvent` 历史渲染出来的 Review recent log 是模型可见投影，并接入
  Prompt Envelope / Run Trace；`run_event.*` 行只描述 UI/SSE 和 ledger 投影。
- Web/SSE 的 `RunEvent` 投影移到 `codey.events.run_event_ui_payload()`，
  Research 工具展示名映射移到 `codey.events.display_tool()`；`TaskRunner` 只调用共享投影，
  不再自己维护重复的 `_ui_event` / `_display_tool`。
- `run_event_payload()` 和 RunLedger 投影继续分开，因为它们服务不同消费者。本版不新增
  事件总线、运行时调度器、插件系统、Run Details UI，也不改变 Router、provider fallback、
  prompt、权限或 UI/SSE payload shape。

## 0.3.17 - Action Policy Pipeline v1

- 新增 `codey/action_policy.py`：本地动作的单调 policy 管线，统一输出
  `allow` / `ask_user` / `deny`。首批覆盖本地文件动作、run command、shell
  approval、Research URL、provider fallback 审计、Local context action 边界和
  managed-output artifact 限额。
- run command allowlist 现在以 action policy 模块为单一真源。`tool_runtime`
  仍然负责实际执行和结果投影，但 sink-level policy 检查必须显式接收
  permission profile，policy 拒绝会结构化写入
  `ToolOutcome.audit["policy_decision"]`。
- Research URL 检查保留现有 `check_fetch_url()` API 和用户可见拒绝文案，内部复用
  共享 action policy URL guard；畸形 URL 端口会作为 policy reason 拒绝，不再作为解析异常冒出。
- managed-output artifact 写入现在经过 size/count policy guard，并要求 writer
  verification profile。超限 artifact 不再保留 handle，但模型看到的有界结果文本不变。
- 未知 action kind 现在由 policy pipeline 直接 deny，不再落到 default allow。
- action policy 模块的 `__all__` 只保留窄公共面；低层 run-command helper 是内部实现细节。
- Run Trace manifest 新增有界 `policy_decisions`，只记录 kind、decision、guard id、
  reason code、phase、subject ref 和 display digest；不保存 raw command、URL、
  stdout/stderr、源码正文、网页正文或 prompt 文本；mapping fallback 也必须是
  digest/ref 形状。
- provider fallback policy decision 只做 trace 审计，不改变 provider 选择、fallback
  排序、Router 行为、prompt 文本、工具 schema、UI/SSE payload shape 或 task receipt。
- `policy_guard` capability metadata 现在声明 `action_policy_boundary`；Capability
  Registry 仍然是只读能力地图，不参与运行时调度。

## 0.3.16 - Tool Contract v2

- `ToolOutcome` 和 `ToolResult` 现在只用 `model_text` 表示模型可见的工具结果文本。
  旧 `output` 字段和顶层 managed-output metadata 字段已直接删除，没有保留兼容层。
- 工具结果现在拆成 `presentation`、`audit`、`canonical` 三个投影：UI/SSE/receipt、
  RunLedger/本地审计、程序内部结构化事实不再从模型文本里猜。
- `presentation`、`audit` 和 `canonical` 会在 `ToolOutcome` / `ToolResult` 边界被
  转成有界 JSON-safe mapping。不支持的值会变成短 marker 字符串并加入 projection
  warning，不会让后续审计/导出序列化失败。
- managed-output audit metadata 在消费时会做 schema 级规范化：只接受
  `out_[A-Za-z0-9_.-]{1,80}` 格式的 handle，坏 byte count 归零，只保留
  64 位小写 hex `sha256`，畸形 audit 不会打崩 UI/SSE event 渲染。
- managed output handle 现在放在 `audit["managed_output"]` 下；模型仍收到同一条有界
  footer，说明完整输出只保存在本地用于审计/导出，不是新工具。
- Coding 和 Research codec 只渲染 `model_text`。测试锁住
  `presentation`、普通 `audit` 和 `canonical` 哨兵不会进入 prompt。
- Run event、TaskRunner SSE payload 和 RunLedger 投影现在从 `presentation` / `audit`
  helper 取字段，不再读取旧顶层 output 字段。
- 本版不新增工具系统、插件系统、运行时调度器、Router 行为、provider fallback 行为、
  权限行为、UI 入口或工具 schema prompt。

## 0.3.15 - Internal Capability Registry v1

- 新增 `codey/capabilities.py`：Codey 内置能力边界的只读 registry。
- Registry 声明每个内部能力的 id、提供的边界、消费的边界、是否模型可见、
  是否需要 policy、UI surface、持久状态、permission profiles、owner module，
  以及是否允许第三方代码或覆盖用户选择。
- 第一版内置能力地图覆盖 provider factory、provider capability hints、agent runner、
  tool runtime、Research runner、Review runner、Local context、changes presenter、
  RunLedger、Run Trace、Prompt Envelope 和 policy guard。
- `server.State` 现在持有内置 registry，`TaskRunner` 只携带这份 metadata。它不参与
  provider 选择、Router 决策、permission profile 选择、prompt 组装、工具调度、
  UI、SSE、receipt 或 fallback 行为。
- 新增 capability 和 architecture 测试，拒绝未知依赖、未知 permission profile、
  未知 UI surface、未知持久状态、第三方标记、覆盖用户选择标记，以及任何 plugin-loader
  形状。
- 本版只是 metadata 和架构约束，不需要 live provider A/B。

## 0.3.14 - Prompt Envelope v1

- 新增 `codey/prompt_envelope.py`：为模型可见 section 提供一个很小的内部
  prompt envelope 和 fail-open trace sink。
- Coding、chat、Research、review、consensus、project-audit 的模型边界现在通过
  统一 sink 在实际 provider.send 前记录 prompt section metadata，不再在各处手写
  `trace_call`。
- Run Trace 的 prompt section payload 新增有界 `purpose`、`model_visible` 和
  source-ref fallback；仍然只保存 digest、字符数、预算、截断标记和 refs。
- Research intro 组装改用 prompt envelope，渲染文本保持字节级等价。Coding 保留
  现有 prompt 形状，包括 `User task` 前的单换行边界。
- provider-send prompt section 仍在真实模型调用前即时落盘；TaskRunner 的二级输入片段
  改用非边界 `secondary_input_prepared` metadata，非模型边界 metadata 继续 checkpoint batching。
- trace 关闭时，local-context 和 secondary-input helper 现在会直接早退，不再扫描
  section。
- Chat consensus 路径不再记录实际没有发送的 `chat_outbound_prompt`；project-audit
  advisor prompt refs 带 advisor id，重复 advisor prompt 仍可审计。
- `PromptEnvelope` 不再依赖 provider control 代码；control teaching 的取消信号按异常名透传。
  Run Trace prompt-section 去重键现在包含 `purpose`。
- `PromptEnvelope` v1 保持最小 API 面：section 通过构造传入后渲染，不保留未使用的
  mutable builder convenience。
- 本版不改 UI、SSE、Router、provider fallback、权限、Writer、Review 或 Research
  行为。prompt parity 测试通过时不需要 live provider A/B。

## 0.3.13 - Run Trace Manifest v1

- 新增 `codey/run_trace.py`：每次 run 生成一个有界审计 sidecar，保存到
  `run_traces/<session>/<run>.json`。它通过 `run_id` 关联 RunLedger，但不替代
  RunLedger，也不成为第二套执行事实流。
- Run trace 现在记录 mode、provider、permission profile、结构化 Router 结果、
  prompt section 的 digest/字符数、模型可见工具契约 hash、Local context item refs、
  Research note/source refs、provider failure 分类和 provider fallback switch。
- Hybrid run 会保留 Research / Writer 的分阶段 profile 和工具契约记录；consensus、
  project audit、review 这类二级模型调用也只按 digest 记录输入摘要。
- Research source ref 只保存 hostname，不保存 URL userinfo 或端口；review trace 复用
  实际传给 reviewer prompt 的同一份 impact map。
- 高频 trace metadata 走 checkpoint batching；终态里程碑仍即时落盘。
- provider send 和二级模型调用的 prompt digest 会在模型边界即时落盘。
- Prompt trace 只保存 digest，不保存 raw prompt、聊天全文、源码正文、网页正文、
  Research note body、evidence excerpt、provider raw error 或完整 diff。
- 新增 context source metadata helper，以及 coding/Research 工具契约稳定 hash helper；
  同时用 prompt parity 测试保证发给模型的 prompt 文本不变。
- 删除/忘记会话时会清理对应 session 的 run trace sidecars。
- 0.3.13 不需要 live provider A/B，因为它不改 prompt、Router 决策、Research/Writer
  行为、provider fallback 策略、工具权限、UI、SSE event 或任务完成收据。

## 0.3.12 - Research Notes v2

- Research drawer 的 `Notes` tab 从 note id / excerpt 纯文本升级为可读笔记卡片，
  分组为 `Selected note`、`Synthesis`、`Created notes`、`Updated notes`。空分组不渲染；
  全空时只显示一条克制的 `No notes recorded`。
- Notes 现在通过 Codey 现有安全 Markdown renderer 渲染有界预览，支持 heading、
  paragraph、list、bold、inline code、code fence 和 blockquote。raw HTML 仍会被转义，
  note body 不会作为可信 HTML 直接插入。
- 每条 note 下方新增安静的 source chips。chips 只来自本地已保存 provenance：
  `note.sources`、`citationMap`、`openedSources`、`sourceUrls`，并且只允许
  `http:` / `https:` URL 用 `noopener,noreferrer` 打开。
- 长 note body 默认有界截断，并提供本地 `Show more` / `Show less` 切换。展开只影响
  当前 drawer DOM，不写本地状态。
- Notes tab 删除单独的 source URL section；来源仍可在 `Sources` tab 和每条 note 的
  source chips 里追溯。
- 0.3.12 不需要 live provider A/B，因为它不改 Research prompt、runner、provider 行为、
  Router、Writer 路径或权限模型。

## 0.3.11 - Local Context Control Surface v1

- 新增 `codey/ghost/control_surface.py`：给网页 UI 使用的有界 presenter 和
  action dispatcher。`GET /api/ghost/summary`、`POST /api/ghost/action`、
  `GET /api/ghost/export` 提供本地审计控制；summary 不返回完整聊天、
  Research body、网页/source snippet、源码、prompt、provider raw reply 或
  provider raw error。
- 新增 topbar `... -> Local context` 审计 drawer。它复用 Changes/Research
  的右侧 drawer 语言，并且三者互斥；不新增 sidebar 常驻入口、badge、toast 或
  任务完成收据噪音。
- Drawer 是单页分组视图：`Recent focus`、`Pending review`、`Active
  preferences`、`Follow-ups`、`Health`。用户可见文案不暴露 Ghost、Memory、
  Affinity、Hebbian、Directive 等内部词。
- Local context 空状态现在只显示一条克制的整体 empty state，不再展示多个空分组；
  Settings 区域也和审计内容有清晰分隔。
- Research Notes 不再复用 diff/code block 样式，笔记正文改用普通 Research note
  文本样式。
- Composer context row 现在只显示 `Choose folder · Research`；当前
  provider/model 只保留在输入框下方的 provider picker。
- v1 支持 accept/reject candidate、queue/reject work item、enable/disable
  updates、delete current chat/project data、reset all 和 export。不做 demote，
  不提供 prompt/provider/router/tool permission 控制，也不允许手写任意记忆直接进状态。
- Drawer 绑定加载时的 session/project scope；用户切换 chat/project 时关闭。
  后端 action 也会校验目标 candidate/work item 是否属于请求 scope，stale scope
  不写本地状态。
- Local context loading 会在 summary 请求发出前绑定请求 scope，stale loading/error
  回调不会留下或更新旧 drawer。
- provider/model 选择完全收敛到输入框下方的 provider picker 后，旧的 `ctx-provider`
  composer-context 兼容路径已移除。
- 修复 Affinity replay 幂等性：Hebbian evidence refs 会先展开再进入 bounded refs，
  避免把不稳定的 generator object 字符串写成本地关联 evidence。
- 新增 `tests/test_ghost_control_surface.py`，并扩展 server/UI/architecture 覆盖。
  0.3.11 不需要 live provider A/B，因为它不改模型可见 prompt、Router、
  Research/Writer 路径、provider fallback 或权限边界。

## 0.3.10 - Affinity Index v1

- 新增 `codey/ghost/affinity.py`：本地有界关联账本。`affinity_events.jsonl`
  是审计真源，`affinity.json` 是可重建 projection；events 不可读或超过 byte
  上限时会阻断 mutating sync。
- Affinity 只从已有有界本地事实同步：accepted Hebbian memory、Work Queue
  row、Research Interest candidate、Router 审计元数据、provider failure kind 和
  task outcome 摘要。它不保存完整聊天、Research body、网页/source snippet、源码、
  prompt、provider raw reply 或 raw error message。
- Affinity 不是 truth、不是权限、不是路由授权，也不是自动执行系统。Research
  判断仍必须靠 evidence/citation；显式 provider/mode/project 仍然优先；shell /
  tool / file 权限边界不变。
- 默认只启用低风险排序消费：Ghost Directive 只重排已经可渲染的 typed memory
  node；Work Queue 的严格 `continue` claim 顺序可获得小幅 affinity boost；
  Research Interest priority 可被提升，但 concept 仍不是 evidence。
- Ghost Directive 的模型可见 header 保留中性的 `Local Context`，但不暴露内部
  memory system 词。
- event log 不可读、超限，或 projection 存在但 events 缺失时，hint 消费 fail
  closed。显示 refs 被 cap 后仍用有界 hash 维持重放幂等；权重强化只按本轮新增
  refs 计，不重复计算历史 refs。
- `ghost export`、`ghost reset --yes`、`ghost delete-scope`、server
  `forget_conversation()` 和 Cognitive Sleep maintenance 都覆盖 Affinity。
  `ghost disable` 会阻止自动 sync 和 hint 消费，但不影响 export/reset/delete-scope。
- 新增 `tests/test_ghost_affinity.py`、`tests/test_task_runner_affinity.py`、
  架构边界测试、`tests/manual/ghost_affinity_ab.py`，以及用于同一指标排序 uplift
  检查的 `tests/manual/ghost_affinity_quality_ab.py`。

## 0.3.9 - Research Interest Queue v1

- 新增 `codey/knowledge/research_interest.py`：有界 research-interest
  candidate builder。它把 Research note 的结构化 `open_questions` 和结构化
  Concept Graph missing link 转成已有 Ghost Work Queue 的候选来源。
- Research synthesis / decision note 新增 typed `open_questions` frontmatter
  字段，并缓存到可重建 SQLite index。Research Interest harvesting 只读这个字段，
  不解析 Markdown section heading。
- Concept missing link 现在有结构化数据，不再从 UI excerpt 文本里反解析。
  UI 仍然渲染文本；队列 harvesting 使用 `MissingConceptLink` 的 related concepts、
  shared neighbors 和 support note refs。
- 0.3.9 不新建第二套 Research 队列。候选映射到已有 `GhostWorkItem`：
  结构化 Research note 问题和强支持的概念缺口可以成为 queued Research item；
  弱概念缺口只保留为 candidate open question。
- TaskRunner 的 post-turn Work Queue sync 现在会从本地 knowledge store
  deterministic harvesting research candidates。不改 Router、不改 Research prompt、
  不改 Directive / Continuity prompt、不改 UI、不改权限，也不改变 provider adapter。
- Research-interest item 完成时仍必须有 `research:*` proof。Concept ref 只能说明
  “为什么值得查”，不能证明“已经查清”。
- 新增 `tests/test_research_interest_queue.py` 和
  `tests/manual/ghost_research_interest_queue_production_ab.py`。

## 0.3.8 - Ghost Work Queue v1

- 新增 `codey/ghost/work_queue.py`：受 Symphony 启发的本地有界 work item
  状态机，包含 claim / running / done / blocked 等状态。`work_events.jsonl`
  是审计真源，`work_items.json` 是可重建 projection。
- Work item 只从已有有界事实同步：continuity open question、Research note
  开放问题、未完成 checkpoint、run ledger 失败 projection、review follow-up。
  它不读取完整聊天、源码文件、网页正文、Research raw body 或 prompt。
- 自动消费非常窄：只有 `intent=auto`，并且用户说“继续 / 下一个 / 处理待办 /
  continue / next item”这类严格 continuation 时，才会认领一条 queued item。
  有明确内容的新请求继续走 Router 或原 baseline。
- 被认领的 item 映射到现有执行模式：research / open question 走 Research，
  coding / project follow-up 走 Project Writer，review 走 review-only。队列不能
  授权权限、批准 shell、选择工具参数，也不能自己执行。
- 完成 item 必须有本地 proof refs，来自 task event、run ledger、receipt、diff、
  Research report 或 review 结果。没有 proof 就标记 blocked，不会假装完成。
- 现有 Ghost 控制覆盖 work queue：`ghost export` 包含 work items/events，
  `ghost reset --yes` 删除它们，`ghost delete-scope` 过滤匹配队列项。CLI 另有
  很薄的检查/控制入口：`ghost work-list`、`ghost work-queue`、`ghost work-reject`。
- Cognitive Sleep 现在会检查 work queue projection/event 健康，并只在超过上限时
  compact work queue events。Sleep 仍不执行任务、不调用 provider、不改 prompt、
  不 emit UI 事件，也不生成新任务。
- 新增 `tests/test_ghost_work_queue.py`、`tests/test_task_runner_work_queue.py`、
  `tests/test_ghost_work_queue_ab.py` 和
  `tests/manual/ghost_work_queue_production_ab.py`。

## 0.3.7 - Ghost Router v1

- 新增 `codey/ghost/router.py`：`intent=auto` 时的有界自动路由层。
  在 `task_start` 前，Codey 可以用 fresh provider tab 判断本轮该走
  `chat`、`planning_readonly`、`research`、`project`、`hybrid` 还是
  `review`。
- Router 不是权限系统。手动 intent 永远优先；shell / tool approval 不变；
  Router 不能决定工具参数、授予权限，也不能让 Research 和 Writer 串权限。
- 本地安全兜底会拒绝多个 JSON、正文包裹 JSON、数组包裹 JSON 这类不干净的
  route 回复；用户明确要求“只聊天 / 不访问项目文件”时，也不会接受会读写项目的
  路径。
- 生产 `TaskRunner` 会真实消费路由结果；`task_start`、run ledger mode、
  provider ranking 和模式分发都会使用最终 route。
- 新增独立 review-only 模式：只收集当前 diff 并调用 reviewer，不启动 Writer、
  不自动 repair、不写文件，也不连接主聊天 provider。
- 新增有界审计文件：`state_home/ghost/router_events.jsonl` 和
  `state_home/ghost/router_state.json`。审计只保存 route 元数据、hash、模式、
  confidence、本地 reason code 和有界 diagnostics；不保存完整用户任务、完整
  router prompt、raw reply 或模型返回的自然语言 reason。
- 收紧 fail-open 边界：用户取消会停止任务，不会 fallback 继续跑；provider
  解析/超时失败会回退现有 baseline；event audit 失败时 route 不改变行为。
  Event append 成功后的 projection / compaction 失败只追加 warning。任何会重写
  router events 的路径都以 `router_events.jsonl` 为真源；events 缺失时先用
  projection bootstrap，再追加新的审计。
- CLI / headless 支持：`python -m codey agent --json --auto` 可以显式启用
  auto routing。现有 `ghost export`、`ghost reset --yes` 和
  `ghost delete-scope` 现在覆盖 router 审计文件。
- 新增 `tests/test_ghost_router.py`、`tests/test_task_runner_router.py`、router
  A/B fixture，以及 router-only / production-spine 两套 manual A/B harness。

## 0.3.6 - Cognitive Sleep v1

- 新增 `codey/ghost/sleep.py`：成功任务结束后短暂运行的本地 Ghost
  维护周期。它会检查 projection / event 健康、只在到期时执行 Hebbian decay、
  从已有有界本地来源刷新 continuity、超过上限时压缩 Ghost event log，并写入
  有界 sleep report。
- Cognitive Sleep 不是后台 agent：不调用 provider、不浏览网页、不跑 shell、
  不生成新 memory candidate、不创建新的 prompt-visible 自由文本，也不改变
  `Local Context` 格式。它在 UI 中不可见，也不 emit SSE 事件。
- 新增 `state_home/ghost/sleep_state.json` 和
  `state_home/ghost/sleep_events.jsonl`。Report 只存 cycle 元数据、step 名称、
  counts、warnings、耗时、取消状态和 run/session/project 引用；不存用户任务原文、
  assistant reply、prompt、Research body、网页正文、source snippet 或源码。
- Sleep 是 single-flight，并且只在 step 边界取消。新的用户任务优先占用主任务槽；
  sleep 失败会 fail-open，不影响任务完成。
- 现有 Ghost 隐私控制覆盖 sleep 文件：`ghost export` 包含 sleep state/events，
  `ghost reset --yes` 会删除它们，`ghost delete-scope` 会过滤匹配的
  session/project/user report 引用。
- Hebbian decay 支持最小维护间隔；如果没有到期的权重 / 状态变化，就不重写
  projection，也不追加审计 event，避免每回合制造账本噪声。
- 新增 `tests/test_ghost_sleep.py`，并扩展 server、CLI、UI、architecture 和
  Hebbian 测试。这个版本不需要 live web A/B，因为它不改变模型可见 prompt、
  provider adapter 或 UI 行为。

## 0.3.5 - Ghost Continuity v1

- 新增 `codey/ghost/continuity.py`：从已有可审计事实生成有界 continuity
  projection，而不是保存完整聊天。它可以投影最近关注点、开放问题、活跃项目、
  fresh correction、刚强化的偏好和长期目标。
- Continuity 状态写入 `state_home/ghost/continuity.json`，小型审计/重建日志写入
  `state_home/ghost/continuity_events.jsonl`，并支持 export、reset、delete-scope
  和显式 rebuild。
- Runtime continuity 读取只看 projection：不会因为 prompt 渲染而 rebuild 缺失文件、
  quarantine 坏文件、追加事件、调用 provider 或扫描项目源码。
- 模型可见文本继续使用中性的 `Local Context`：bounded local continuity 不是新用户输入，
  不是 Research evidence，不能授权工具、绕过审批、覆盖当前请求或项目指令。内部
  Ghost 命名、敏感文本、危险指令层级语言、raw model reply、raw Research body、网页正文
  和源码片段都不会渲染。
- 普通 Chat 和 `planning_readonly` 可以读取 continuity context；consensus 只把它放进
  owner prompt。Project Writer、Reviewer、Research 和 protocol repair prompt 仍然不接收
  Ghost context。
- 任务结束后会在 learning loop 后做 best-effort 本地 continuity sync，不调用 provider。
  Chat 只贡献极短 user focus excerpt；planning 可以同时贡献有界 run ledger projection
  事实。新的 continuity context 是 eventual-consistent：应以 post-turn
  `ghost_continuity_done` 事件完成后为稳定生效点，而不是 `task_done` 发出的瞬间。
  `ghost disable` 会阻止自动 continuity sync，但 preview/export/delete/reset 控制继续可用。
- Research synthesis / decision note 只贡献标题和有界 `Open questions` section 行；
  raw note body、证据段、来源片段和网页正文都不会渲染进模型可见 continuity。
- 扩展 Ghost CLI：新增 `python -m codey ghost continuity` 和
  `python -m codey ghost rebuild-continuity --yes`；`export`、`reset` 和
  `delete-scope` 现在也覆盖 continuity 文件。
- 新增 `tests/test_ghost_continuity.py` 和
  `tests/manual/ghost_continuity_ab.py`。manual probe 使用固定临时 `continuity.json`
  种子，实机 A/B 只测试 context 行为，不把 learning extractor 引入变量。

## 0.3.4 - Ghost Learning Loop v1

- 新增 `codey/ghost/typed_fields.py`：把 signal extractor、deterministic gate
  和 directive renderer 共用的 typed memory field allowlist 收到一个地方。
  模型可见记忆文本仍只从已知 slot/value 模板生成；未知字段或 protected 字段
  不会渲染。
- 新增 `codey/ghost/learning_loop.py`：在普通 Chat 回合结束后 best-effort
  跑显式学习闭环，顺序是 `GhostSignalExtractor` -> raw signal audit ->
  inbox/gate -> Hebbian sync。Provider/browser 访问由外层注入，`codey/ghost`
  仍不 import provider、browser、Research 或 tool runtime。
- 普通 Chat 会在 `task_done` 发出后触发 learning loop。Extractor 使用 server
  注入的 fresh provider tab，因此不会把 extractor JSON 合同输入到用户当前聊天页。
  `planning_readonly` 有代码和测试覆盖，但默认不启用自动学习。
- Auto-accept 更严格：高置信 `style_preference` 只有在包含 grounded 且可安全渲染的
  已知 typed field 时才会 accepted。未知 style field 保持 candidate；
  `correction` 和 `action_tendency` 不会自动 reinforced。
- `ghost disable` 现在会阻止 post-turn extractor 调用；list/export、directive
  preview、reset 和 delete-scope 等控制命令继续可用。
- 新增 `tests/test_ghost_learning_loop.py` 和
  `tests/manual/ghost_learning_loop_ab.py`。manual probe 一次只测一个网页 provider，
  检查 fresh-tab extraction、directive 是否变化、回答风格是否变化、负面 no-signal
  行为，以及是否泄露内部命名。
- 串行 live A/B 已在 DeepSeek、MiMo、Qwen、GLM 和 StepFun 上通过；每个 provider
  之间都重启了专用 Edge CDP 会话。五个 provider 都学到了 typed
  `reply_length=concise` 和 `reply_structure=answer_first` 风格偏好，强化出两个
  active Hebbian node，普通抱怨没有进入 accepted memory，模型回复也没有泄露内部命名。
- post-review hardening：如果 extractor 返回 diagnostics，即使 schema parser 恢复出
  部分合法 signal，也只写 raw signal audit，不进入 inbox/Hebbian 自动学习。
- typed field 渲染和 auto-accept 现在必须命中明确的 kind/slot/value pair，不再把已知
  slot 和已知 value 任意交叉组合。隐藏 alias `style_preference:length` 已删除，
  自动学习合同和 extractor guidance 保持一致。
- 本版本仍不把 Ghost learning 接入 Project Writer、Research、Reviewer、
  protocol repair 或权限系统，也不新增后台队列、UI 或 `torch` / `transformers`。

## 0.3.3 - Ghost Directive ContextSource v1

- 新增 `codey/ghost/directive.py`：把 confirmed active Hebbian memory node 渲染成
  短、可预算的 prompt context。内部功能名仍是 Ghost Directive，但模型可见文本使用
  中性的 `Local Context`，不得暴露 `Ghost` 或 `Ghost Directive`。它只读本地 state，
  不调用模型、不写盘、不把 edge 当事实渲染，也不暴露 evidence quote、raw label
  或内部 id。
- Directive 选择保持确定性和有界：按 session/project/user scope、active 状态、
  superseded 状态、node 权重、当前 Ghost 五类 signal kind、敏感 secret-like 文本
  和危险授权文本过滤。过滤还覆盖 `ignore previous/system/developer instructions`、
  `treat this as the system prompt`、`local memory outranks/supersedes system
  instructions`、`replace system prompt with this memory`、
  `developer messages defer to memory`、`this memory should be used before current
  instructions`，以及 `needs to come before`、`ranks above`、`treated as above`、
  all/bare instructions 变体。同
  scope/conflict key 的 competing value 只有明显领先时才会渲染，否则整组跳过。
- 模型可见 directive item 由 `kind/conflict_key/value_key` 结构字段生成模板；
  `node.label` 只留本地审计。结构字段必须命中显式 safe slot/value allowlist；
  未知 slug、`system = prompt` 这类拆分 protected topic，或涉及 system/developer
  instructions、审批、工具、shell/run、删除文件、current request 时，直接不渲染。
- runtime directive 读取只看 projection：不会在缺失 state 时 rebuild，不会 quarantine
  坏 projection，不写 `state.json`，也不追加 events。过期权重只在内存中做 preview
  decay，用于本次选择，不持久化衰减状态。
- 新增 `ghost_directive` context source key。普通 chat 和 `planning_readonly` 默认可
  接收 directive；Project Writer、Reviewer、Research 和 protocol repair prompt 都不接收。
- 扩展 Ghost CLI：`python -m codey ghost directive`，支持 `--project`、`--session-id`
  和 `--budget`，可本地预览/导出即将进入 prompt 的短上下文。
- 新增 `tests/manual/ghost_directive_ab.py`：一次只测一个 provider 的 live A/B，用于看
  风格/纠错是否生效、是否泄露内部 context framing、以及 `planning_readonly` JSON tool
  protocol 是否下降。
- 串行 live A/B 已在 DeepSeek、MiMo、Qwen、GLM 和 StepFun 上通过：directive arm
  能把本地记忆后端纠正为 bounded JSON projection + JSONL audit，不泄露内部 Ghost
  命名，并保持 `planning_readonly` JSON protocol 合规。
- 本版本仍不新增学习循环，不把 Ghost 注入 Research 或 Project Writer，不改变权限，
  不让 Ghost memory 执行工具，也不引入 `torch` / `transformers`。

## 0.3.2 - Ghost Hebbian State v1

- 新增 `codey/ghost/hebbian.py`：把 accepted Ghost inbox candidate 强化成有界的
  本地 Hebbian 记忆权重账本，包含加权 `GhostNode` 和 `coactivated_with`
  `GhostEdge`。Edge 只表示本地共同出现，不表示外部事实关系。
- 补齐 inbox review 和 value 语义。Candidate 现在包含 `value_key`、
  `evidence_refs`、review 元数据和 `superseded_by`；同 scope/ref/conflict/value
  会合并证据，同 scope/ref/conflict 但不同 value 会作为 competing candidates
  保留，不再静默覆盖。
- 已 accepted 的候选不会被后续较弱的 candidate/rejected ingest 降级。用户显式
  `accept` 新值时，可以把同 scope/conflict 下旧 accepted 值标成 superseded；
  普通 ingest 不能把 superseded 旧值复活。
- Hebbian state 写入 `state_home/ghost/state.json`，独立事件日志写入
  `state_home/ghost/hebbian_events.jsonl`。Projection 和事件日志都有上限；坏
  projection 会 quarantine，坏 event 行会跳过并记录 warning，state 可从 events 重建。
- 强化算法保持确定性和本地化：权重有界、同 evidence ref 去重、连续且幂等的
  half-life 衰减、edge fanout 上限、user/project/session scope 隔离，并支持
  export/reset/delete-scope。写入失败 fail-open，不影响 chat、coding 或 Research。
- 扩展 Ghost CLI：
  `python -m codey ghost accept/reject/state/rebuild-state`，并让 `export`、`reset`
  和 `delete-scope` 覆盖 Hebbian state 与事件日志。`accept` 会在 sibling node
  已存在时补同 run coactivation edge；`reject` 会从 active Hebbian log 移除对应
  node 和相连 edge。`sync_from_inbox()` 会 reconcile rejected/superseded inbox row，
  不只是强化 accepted row。
- Coactivation edge evidence 改成 candidate pair/run 级别，所以同一 run 里的同一对
  candidate 不会因为 A->B / B->A 遍历顺序重复加权。
- Hebbian node kind 仍严格对齐当前 Ghost 五类 signal；future affinity/boundary
  node kind 要等 extractor 和 gate 路径存在后再开放。
- `server.State` 在 `state_home` 存在时创建 `ghost_hebbian`；裸 `State()` 仍禁用
  Ghost 写入。
- 本版本仍不生成 Ghost Directive、不注入 prompt、不接 TaskRunner、不做自动日常学习、
  不改变 chat/coding/Research 行为、不加 UI，也不引入 `torch` / `transformers`。

## 0.3.1 - Ghost Memory Inbox v1

- 新增 `codey/ghost/inbox.py` 和 `codey/ghost/gate.py`：把 0.3.0 抽出的
  `GhostSignal` 投影成可审计的本地 memory inbox candidate，并用纯本地 deterministic
  gate 决定 `accepted`、`candidate` 或 `rejected`。这里的 `accepted` 只表示未来
  0.3.2 Hebbian State 可消费，不会在本版影响模型。
- Ghost 状态现在分层为 `signals.jsonl`、`events.jsonl`、`inbox.json` 和
  `settings.json`。`events.jsonl` 是 append-only 真源，`inbox.json` 是可重建
  projection；坏 projection 或未来 schema 会 quarantine，坏 event 行会跳过并记录
  warning。
- `events.jsonl` 现在同时按行数和字节数做 compact；如果事件日志超限但 projection
  坏掉，Codey 会保留 `events_too_large` warning，而不是静默重建成空 projection。
- 0.3.1 不写 `state.json`，它保留给 0.3.2 Hebbian State。候选类型严格来自
  0.3.0 的五类 signal，不新增 `boundary_candidate`。
- Gate 保持保守：高置信 `style_preference` 可以自动 `accepted`；`correction`、
  `research_interest`、`long_term_goal` 和 `action_tendency` 默认留在候选箱等待后续
  控制或强化。Gate 不使用中文/英文短语硬编码来自动接受 correction 或划分偏好语义。
- `conflict_key` 使用结构化 `metadata.conflict_key` / `conflict_key_hint` 或稳定文本
  指纹生成，不靠本地语言词表猜 `tone`、`reply_structure` 等语义。相同 scope 和
  conflict key 的候选会合并并增加 `reinforcement_count`。
- 新增本地控制入口：`python -m codey ghost list/export/reset/delete-scope/enable/disable`。
  `export` 现在同时导出 inbox projection 和 raw `signals.jsonl` audit。`reset`
  / `delete-scope` 会同步清理 raw signal audit 与 inbox/events active store，不只是留下
  tombstone。`reset` 和 `delete-scope` 需要 `--yes`。
- `disable` 只阻止未来 ingest，不影响 list/export/delete。`enable/disable` 的 audit event
  写入失败会返回 false，而不是静默当成成功。
- `server.State` 在 `state_home` 存在时创建 `ghost_inbox`；裸 `State()` 仍禁用 Ghost
  写入，保持嵌入和测试路径不写真实长期状态。
- 本版本仍不生成 Ghost Directive、不更新 Hebbian 权重、不注入 prompt、不接入默认
  TaskRunner 日常学习循环、不改变 chat/coding/Research 行为，也不引入 `torch` /
  `transformers`。

## 0.3.0 - Ghost Signal Extractor v1

- 新增 `codey/ghost/`：provider-neutral 的 Ghost 信号抽取层，只识别显式学习信号。
  v1 候选类型包括 `style_preference`、`correction`、`research_interest`、
  `long_term_goal` 和 `action_tendency`。
- 新增 `GhostSignalCodec`：一个很窄的 JSON 合同，让外部 provider 返回有边界的
  signal candidates。`evidence_quote` 必须来自当前用户原文；编造 quote、未知 kind、
  非法 scope、非法 confidence、坏 JSON 和多个 JSON object 都会进入 diagnostics。
  疑似密码、API key、bearer token、私钥或高熵密钥的 candidate signal 会在写入本地
  signal log 前被拒绝。
- 新增 `GhostSignalExtractor`：fail-open 的 provider wrapper，只用于 manual/shadow。
  provider 报错等价于没有信号，不影响 chat、coding 或 Research 执行。
- 新增 `GhostSignalStore`：写入 `state_home/ghost/signals.jsonl` 的 append-only
  候选事件日志。它只保存有边界的 candidate summary、quote、diagnostics 和 metadata，
  不保存完整 transcript，也不代表长期记忆已被接受。裸 `State()` 会禁用该 store。
- 新增 `tests/manual/ghost_signal_extractor_ab.py`：一次只测一个 provider 的 live probe，
  并带 self-test，覆盖显式偏好、纠错、研究兴趣、行动倾向和 no-signal control。
  连接失败会写入有边界的 failure row，不会再被 probe 自己的二次异常盖住原始
  provider/CDP 错误。
- 根据 live A/B 暴露的 CDP 问题加固浏览器生命周期：Ghost manual probe 即使保留
  provider tab，也会释放非 isolated Playwright 自动化连接；非 isolated 浏览器启动失败
  会终止新子进程。Codey 不会在 Playwright attach 失败后静默切到另一个 provider 端口；
  正确恢复方式是重启那个 CDP 浏览器会话。
- 本版本不注入 Ghost directive、不写 accepted memory、不更新 Hebbian 权重、不改变
  TaskRunner 行为、不改 Research/coding 工具协议、不改 UI，也不引入 `torch` /
  `transformers`。包根保持轻量：schema/store 导入不会加载 provider/browser 代码，
  只有显式 extractor 路径才会加载 provider wrapper。

## 0.2.33 - Project-local Config v1

- 新增 `codey/project_config.py`：严格解析项目内显式存在的
  `.codey/config.json`。项目配置只是有边界的事实/偏好来源，不是授权系统。
- 项目配置可以声明验证命令候选、扫描忽略路径前缀、`project_map_chars`
  预算提示，以及未来 provider 偏好。provider 偏好本版只解析和校验，不影响
  provider 选择。
- 配置里的验证命令会进入现有 verification candidate 流水线，优先级低于历史成功检查、
  高于 manifest 自动发现。它们仍必须通过可执行文件、cwd 在项目内，以及
  `tool_runtime` run allowlist 检查。
- `scan.ignored_paths` 使用项目根相对前缀语义，并作用于 Project Map 顶层列表、
  symbol overview、focused subtree 和 verification discovery；现有 secret、hidden、
  symlink 和默认排除规则不会被削弱。
- 配置 warning 会以很短的 ContextSource block 进入 Writer / readonly planning prompt，
  让模型知道配置有部分没生效。协议 repair prompt 仍保持短小，不夹带配置上下文。
- `context.budget_hints.project_map_chars` 只能降低 Project Map 渲染预算，并有下限；
  项目配置不能扩大 prompt 预算。
- 网页 provider smoke 加固：StepFun 现在会等 composer 文本熬过页面 late hydration
  后再提交，并把缺失发送按钮准确报成 send-button failure；manual submit probe
  不再把复用 tab 的 Playwright CDP 会话留开；`tools/live_smoke.py --provider all`
  只跑网页 provider，不再混入 `local`。
- Review 加固：超大的 `.codey/config.json` 现在会先用文件元数据挡掉，不会先读完整
  body；项目配置用轻量静态 capability 表校验 provider hint，不再导入网页 adapter；
  config warning 的 omitted 计数变成真实可达路径；StepFun 在稳定 composer gate 之后
  不再保留真实路径不可达的 Enter 提交兜底。
- 本版本不做 workflow DSL、不做项目本地权限矩阵、不做 shell 自动批准、不自动写配置、
  不做 Research headless 配置、不改 UI，也不放宽任何 runtime 安全守门。

## 0.2.32 - Headless JSONL Runner v1

- 新增 `codey/headless_runner.py`：一个很薄的机器可读 runner，复用生产
  `TaskRunner`，不是第二套 agent loop。
- `python -m codey agent --json` 现在走 headless TaskRunner 路径。本版本里普通
  `python -m codey agent` 仍保留旧的直接 CLI 路径，降低行为变更范围。
- Headless JSONL 输出有边界的事件投影：task start、status/info、turn、
  tool start/finish、shell rejection 和 task done。它不原样 dump UI-only state、
  完整命令日志或完整模型回复。
- Project coding 的 headless run 复用 UI 同一条编排主干里的 Run Ledger、
  Managed Outputs、provider fallback 排序、change tracking 和 receipt 生成。
  第一版 headless review callback 是 no-op，不会静默多开 reviewer 模型。
- 新增 JSONL 模式的 `--readonly`。它映射到内部 `planning_readonly` profile，
  只暴露读/搜索/引用类工具，不收集 diff，不创建 Work Checkpoint，也不写
  ProjectFacts。
- Headless shell approval 默认拒绝：`shell_request` 会被投影成
  `shell_rejected`，reason 为 `headless_default_deny`，不会审批，也不会等待用户。
- TaskRunner 现在有明确的内部 `planning_readonly` task kind。terminal event 里
  投影为 `planning`，不再含混当作普通 Project Writer run。
- 本版本不做后台 agent、不做 Research headless 自动搜索、不做 shell 自动批准、
  不做 CI 发布/安装/删除动作、不改 UI，也不新增 provider 选择产品界面。

## 0.2.31 - Internal Permission Profiles v1

- 新增 `codey/permission_profiles.py`：一个很小的内部 runtime 阶段边界注册表，
  包含 `chat`、`research`、`coding_writer`、`reviewer` 和 `planning_readonly`。
- Coding tool definition 现在可以按 profile 过滤。`JsonToolCodec()` 仍默认渲染完整
  Project Writer 合同；`JsonToolCodec(permission_profile="planning_readonly")` 不再展示
  `edit`、`run` 和 `shell`。
- Coding 协议错误现在区分“全局不存在的工具”和“工具存在但当前 profile 不允许”。
  `write_file` 仍然是 `unknown_tool`；`planning_readonly` 里的 `edit` 是
  `disallowed_tool`。
- `parallel` 现在同时检查 `parallel_safe` 和当前 profile，readonly profile 不能通过
  batch wrapper 偷带不允许的工具。
- 空 coding tool definition 集合会渲染为空 contract；非 coding profile 如果被误用于构造
  coding codec，会直接 fail-fast。测试也锁住 `coding_writer` 必须覆盖当前全部 coding
  tool definition。
- `agent.run()` 接受 `permission_profile`，用于默认 codec 创建和 ContextSource 过滤；
  但测试或 manual probe 显式传入 codec 时不会被替换。
- 私有 consensus/project-audit codec 现在使用 `planning_readonly`，与它已有的只读执行边界一致。
- Project Writer 调用显式绑定 `coding_writer`。Research 和 Reviewer profile 已声明并测试，
  但本版本不重写它们已经稳定的 runtime。
- 本版本不做用户可见 mode switch、不做项目本地权限配置、不做新安全系统、不做 headless
  执行，也不放宽 `tool_runtime`、shell approval、safe path、Research 或 run allowlist 守门。

## 0.2.30 - Managed Output Handles v1

- 新增 `codey/managed_outputs.py`：为被模型可见 `run` 结果裁剪掉的命令输出保存
  run 级本地 handle。
- `run_command()` 现在内部拆成 raw/projection 两层。默认公开行为不变；生产
  Project Writer run 可以在依赖栈 pruning 和 prompt clipping 前保存 raw stdout/stderr。
- 只有 projected `run` 结果实际 `truncated=True` 时才写 managed output。短命令输出不会
  被保存成本地日志。
- managed output metadata 明确区分生产 `tool_id`、`original_bytes`、`stored_bytes`、
  保存文本的 `sha256` 和 `stored_truncated`。单个输出和单 run handle 数都有上限，
  路径被限制在 Codey state 目录下，写入失败 fail-open；超过保存上限的输出会保留
  head/tail，并插入 omission marker。
- `ToolOutcome` 和 `ToolResult` 增加可选 handle metadata。JSON tool codec 只渲染一行
  短 footer，说明 handle 是 local audit/export 用途，不是工具；完整输出不会被注入 prompt。
- Run Ledger 的 `tool_finished` 事件记录 handle id、原始/保存字节数和保存输出 hash，
  但不保存完整命令输出。
- `State()` 只有在传入 `state_home` 时启用 managed outputs，和 Run Ledger 纪律一致，
  避免裸测试/嵌入场景写真实 `~/.codey`。
- 本版本不做 UI、不做 `/api/output`、不做 `read_output`、不做全文搜索/RAG、不保存
  Research 网页正文，也不让模型自动读取 handle。

## 0.2.29 - Provider Capability Registry v1

- 新增 `codey/provider_capabilities.py`：静态内部 provider 能力注册表，记录
  JSON 可靠性、coding/research/review 适配度、上下文预算提示、网页原生工具干扰
  风险、canary 提示、bounded failure families 和备注。
- `rank_providers()` 是纯确定性排序 helper。它保留输入顺序作为 tie-break，
  明确 selected/preferred provider 优先，`avoid` 只表示“有替代时排后”，不会禁用，
  unknown provider 走 default capability。通用 hybrid 排序取 Research 和 Coding 中更严的适配度。
- `TaskRunner` 只在必须找替代 provider 时消费 mode-aware capability 排序：selected
  provider 不可用、connect failure、canary failure 和 Writer failover。hybrid 启动
  fallback 按 Research 排序，因为第一阶段先跑 Research；hybrid 进入 Writer 后的
  failover 按 Project 排序。用户明确选择的 provider 不会被 capability 偷换。
- `reviewer_candidates()` 现在也会把候选 reviewer 走一次 review mode 静态排序，但仍然
  过滤 writer/local/unavailable provider，并且不向 UI 暴露 capability 术语。
- `ProviderSupervisor` 仍然只负责 runtime health、cooldown 和 canary。静态 capability
  不写入 `provider-health.json`，runtime failure 也不会修改 capability。
- 测试会约束 `failure_families` 必须属于真实 `ProviderFailure` kind 词表。
  `context_budget_hint` 本版本仍然只是静态 hint，不进入生产策略消费。
- 本版本不做 provider 排名 UI、不让模型自选 provider、不做 Research 中途 failover、
  不做 live A/B、不做 runtime capability 学习，也暂不消费 capability 里的 canary 字段。

## 0.2.28 - ContextSource v1

- 新增 `codey/context_source.py`：一个很小的 prompt 装配层，用命名
  context source 渲染上下文块，并为每段 source 声明字符预算、freshness、
  why_included、heading 和明确的 failure policy。
- `agent.py` 现在把已有项目 prompt 块包装成 `ContextSource`：project
  instructions、verified project facts、Research Brief、Project Map、Work
  Checkpoint 和 initial listing。`ProjectTaskContextBuilder` 仍然负责 facts、
  knowledge、map、checkpoint 和验证候选的业务加载。
- `Coding current local context` 也通过 `ContextSource` 渲染，但仍然只在本地
  tool result 之后追加；不会进入初始 project prompt，也不会进入 protocol repair
  prompt。
- 可选 context source 的普通失败会 fail-open，但 `TaskCancelled` 和
  `DeadlineExceeded` 会继续抛出，避免用户 Stop 或 provider deadline 在 prompt
  装配阶段被吞掉。
- Work Checkpoint 的 context budget 现在从 `work_checkpoint.py` 的生产者上限推导，
  避免有边界的 checkpoint 在 source-level clipping 时丢掉 changed files 列表。
- source metadata 不渲染进模型 prompt。本版本保持 prompt 内容目标等价，只是让
  上下文块变成有名字、有预算、可测试、可审计的内部边界。
- 本版本不做 live A/B、不改 UI、不做 provider routing、不做向量记忆、不自动注入
  Research vault、不迁移 checkpoint/restore，也不新增模型能力。

## 0.2.27 - ToolDefinition v1

- 新增 `codey/tool_definition.py`，作为 coding tools 的唯一内部元数据来源。
  第一版只覆盖现有公开 JSON 工具名：`list_dir`、`read_file`、`read_files`、
  `grep`、`find_references`、`parallel`、`edit`、`run`、`shell` 和 `done`。
- `JsonToolCodec` 现在从 tool definition 读取工具契约渲染、alias、
  parallel-safe 判断、tool result 公共名称和 batch 限制。Codec 不再拥有或
  re-export 工具定义表。
- `agent.py` 现在从 definition 层派生 supported runtime tool names、需要
  follow-up 的 information tools、repair 示例和 tool activity 行。实际 dispatch
  loop、schema validation、read-before-edit guard、shell approval 和 run allowlist
  都保持不变。
- `edit` 声明 `file_changed` ledger fact，`run` 声明 `command_verified`；测试会
  确认这些声明和 Run Ledger v1 实际事件一致。`write`、`write_file` 仍然是
  unknown tool，并继续通过 repair prompt 引导到 `edit(content=...)`。
- Shell tool-start activity 现在使用更清楚的 `Requesting shell approval for ...`
  文案；测试会锁住这个有意的可见文案变化。
- 这是小型内部重构，不是插件系统。不迁移 Research tools，不改 UI 控件、
  permission UI、runtime 安全守门、checkpoint、restore 或 provider 行为。

## 0.2.26 - Ledger Projections v1

- 新增 `codey/run_ledger_projection.py`：对 Run Ledger JSONL 记录做纯只读投影，
  汇总 run 生命周期、provider 选择/切换/失败、模型回复次数、工具次数和错误、
  已观察到的文件修改、跑绿的命令、最终 changes 统计，以及 complete/truncated 状态。
- `changes_collected` 现在把 `checks_passed` 作为顶层 bounded fact 保存。Receipt
  投影只读取 `changed_count`、`mode` 和 `checks_passed`，不会从 ledger 里嵌套的
  legacy `receipt` 字典反推自己。
- `TaskRunner` 现在会在写入 `run_finished` 之后 shadow-consume projection。只有当
  ledger 完整、未截断、有 final changes，且投影出的 `changed_count`、
  `restore_available`、`checks_passed` 与 legacy receipt 完全一致时，Codey 才采用
  projected receipt；否则直接回退旧路径。
- 这一版不改 UI、checkpoint、restore、`ExecutionEvidence`、Research ledger、
  API export 或 headless 行为。Projection 失败仍然 fail-open。
- 新增 focused projection 测试，并补 TaskRunner 覆盖，验证 terminal event 发布前
  确实读取了 complete ledger projection。

## 0.2.25 - Run Ledger v1

- 新增有边界的 `Run Ledger`：项目 coding run 现在会在本地 state 目录下写入
  append-only JSONL 事实账本，记录 `run_started`、`provider_selected`、
  `model_reply`、`tool_started`、`tool_finished`、`file_changed`、
  `command_verified`、最终 `changes_collected` 和 `run_finished` 等事件。
- 这是 observe-only 层：不改 `agent.py` 主循环，不改变网页模型 JSON 协议，
  不替代 UI/SSE、`ExecutionEvidence`、`WorkCheckpoint`、receipt 或 restore。
  `TaskRunner` 只是把已经存在的本地事实同步投影进 ledger。
- Ledger 不保存完整模型回复、完整源码、完整 shell 输出、网页 DOM 或网页正文。
  `model_reply` 只记录回复字符数和 bounded note；工具结果只保存短首行；长 run
  仍按现有工具输出规则返回给模型和 UI。
- Ledger 大小预算从语义常量推导：`MAX_LEDGER_EVENTS * LEDGER_BYTES_PER_EVENT_BUDGET`，
  当前约 512 KiB。超过预算只写一次 `ledger_truncated` 后停止追加；写入失败会
  fail-open，不影响正在执行的任务。
- 终态错误路径现在会在 `run_finished` 前写入 bounded `provider_failure` 事件；
  `State()` 没有 durable `state_home` 时会禁用 run ledger，避免测试或嵌入场景把
  project run ledger 写到真实用户 `~/.codey`。
- 覆盖新增单测和回归：路径逃逸防护、模型回复不落全文、edit/run 事实投影、
  byte budget 截断、append 失败 fail-open，以及 TaskRunner project run 的端到端
  ledger 写入。

## 0.2.24 - Coding Current Context

- Coding 现在会在本地工具结果后追加一段有边界的 `Coding current local
  context`。它告诉网页模型：本轮已经读过哪些文件、哪些已存在文件适合做精确
  edit、哪些改动文件还没验证或已经被验证覆盖，以及未验证时当前最适合跑的
  验证命令。
- 这段 context 只是提示本地事实，不是 allowed-tools gate，也不是硬状态机；
  不改变 coding 对多个顶层 JSON 工具对象的历史兼容行为。协议 repair prompt
  仍保持短，不混入 context。
- edit 之后会在下一次工具 prompt 前刷新一次 verification candidate，所以模型
  在准备结束前就能看到建议测试命令。“已验证”只会在最新 edit 之后跑过能覆盖
  当前 selected candidate 的成功命令时显示；一旦 fresh，context 不再展示可直接
  复制运行的验证 JSON，避免模型重复跑已经绿了的检查。
- Qwen 提交逻辑现在会同时等待 composer 保留文本和 send button 可用。这修掉了
  实机 A/B 里暴露的问题：页面还在 late hydration 时 Codey 已经输入，等页面渲染
  完 composer 被清空，导致发送失败。
- 贴近生产的实机 A/B 支持这个改动：DeepSeek、MiMo、Qwen 都保持 `2/2` 成功；
  context 组减少了泛化默认验证提醒回合（DeepSeek `-2`、MiMo `-1`、Qwen
  `-2`），并用更少 turn 完成。代价是每个 provider 两个 case 合计多发约 2K
  字符。

## 0.2.23 - Coding Protocol Typed Repairs

- Coding JSON 协议错误现在会带 typed `protocol_error_kind`：
  `no_json`、`unknown_tool`、`invalid_args`、`direct_answer`、
  `native_tool_denial`、`nested_tool_in_done`。这复用了 Research 已经验证过的
  `ToolPlan.protocol_error_kind` 字段。
- Coding run loop 不再对所有协议错误都发同一段泛化 JSON 提醒，而是按错误类型给
  具体 repair：`write_file` 会被纠正到 `edit(content=...)`；混用 edit 模式会说明
  一次只能选一种模式；`read_file offset=0` 会说明 offset 是 1-based；直接 prose
  回答会被要求放进 `done.summary`；网页原生“工具不可用”提示会被纠正回本地 runner
  JSON；把工具 JSON 包在 `done.summary` 里会被要求直接调用那个工具。
- 实机 manual A/B 现在直接测生产 repair renderer。把 prompt 收紧为“从上一条无效
  JSON 本身生成保留原意的示例”后，六个故意损坏的 coding 回复里，DeepSeek 从
  `clean_repair=5/6` 提升到 `6/6`，Qwen 从 `4/6` 到 `6/6`（其中一次 baseline
  网页 transient send failure 已单独补跑），MiMo 从 `5/6` 到 `6/6`。早期
  prototype wording 也显示同方向，但因为嵌入了理想修复形状，只作为偏强方向证据。
- 范围保持克制：coding 仍保留“多个顶层 JSON 工具对象”的历史兼容行为；本版本不加
  coding allowed-tools gate、不加 verification candidate ID，也不注入 concept
  context 到 prompt。
- Manual-only Research probe 补档：`concept_context_ab.py` 记录 Concept Context
  注入的负面/中性实验结果，仍然不进入生产 Research prompt。

## 0.2.22 - Concept Graph Seed

- 知识库之上的概念层：笔记现在可以在 `knowledge_write` 中声明带类型的概念
  关系（`relations: [{src, dst, kind}]`，kind 支持
  affects/uses/causes/part_of/enables/relates）。关系由新增的
  `knowledge/concept_schema.py` 做标准化（小写化、URL / 机器 tag / 年份噪声
  过滤、自环与重复去除、每笔记最多 8 条），存进笔记 front-matter
  （Markdown 依然是权威数据），并缓存到可重建的 `concept_edges` SQLite 表，
  每条边带 note 级 provenance。关系端点会自动并入笔记的 tags。
- Concept Graph read model：新增 `knowledge/concepts.py`，构建虚拟概念图——
  概念永远不会变成真实 Markdown 笔记。声明关系成为边（label 带支持数），
  最近的 synthesis 笔记通过弱化的 `tagged` 边挂到概念 tag 上，当前 session
  的概念会被 focus 高亮。co-tags 永不生成边；missing-link 候选（两个概念
  共享一个 declared 邻居但彼此没有 declared 边）只以文本形式出现在概念
  节点上，最多 6 条并标注 "unproven; not facts"。Evidence Graph
  （`knowledge/graph.py`）完全不动。
- 新增 `GET /api/research/concept_graph` 诊断端点，同时 Research drawer
  对用户只保留一个统一 `Graph` 标签页。这个图由概念、当前 synthesis/report、
  相关笔记和 source URL 组合而成，depth 1/2/3 逐层展开；concept endpoint
  继续用于确定性测试和诊断。
- synthesis 笔记现在只从本次 run 的 active notes 聚合 top 概念 tags，而不再
  只有机器 tag，让报告接上概念层，同时不会把 contradicted/stale note 的 tag
  重新带回图里。
- 契约纪律：`knowledge_write.relations` 必须是对象列表（单个对象会归一化为
  单项列表；非对象项返回 typed `invalid_args`）；宽松清洗放在工具层，并在
  工具结果里给出显式 warning。研究 prompt 要求模型只声明引用来源真正陈述的关系。
- 评审加固：概念节点详情按 Outgoing/Incoming 分组展示 declared relations，
  并显示支撑笔记标题（画布仍是无向线）；missing-link 文案改成 Open Questions，
  继续标注 "unproven; not facts"；只有 `status='active'` 的笔记会进入概念层；
  node/edge limit 是硬边界——synthesis 挂接只花剩余预算，直接调用时的坏 limit
  参数会被容错，Concept Graph 会先按 declared relation 成对保留端点，再用剩余
  空间补 tag-only 概念，Concept Graph 的边选择会优先保留由当前 session 笔记支撑的
  关系，而不是靠共享概念名猜测当前关系；`concept_edge_rows` / `tag_concept_rows`
  会先取目标 session rows，再用全库 rows 回填，避免旧 run 被后续知识库增长截断；
  统一 Graph 会先保住当前 evidence spine，再把剩余预算分给全库概念；默认 Research
  controller 的 prompt 和 `knowledge_write` JSON shape 现在也教 tags + relations；
  空 Graph 显示 builder 的引导文案，而不是泛化的 "No graph yet"。
- 实机 provider 加固：MiMo 的代码块 overlay 现在会忽略隐藏的重复层，所以肉眼只有
  一个 JSON 工具调用时，不会因为 DOM 暴露两份文本而误判成 `too_many_tools`；
  controller repair 会先把当前阶段禁止的工具（例如过早 `knowledge_write`）
  归类为 `disallowed_tool`，不再反过来教它该工具的参数格式；Research drawer
  继续保持克制，只暴露一个统一 `Graph` 标签页，而不是拆成 evidence/concept
  两个图入口。
- Source 节点展示打磨：source URL 节点会优先使用 synthesis source ledger
  中恢复出的新闻标题；graph 节点详情使用现有零构建 Markdown renderer 渲染短 excerpt。
- 本版本有意不做：不向研究 prompt 注入概念上下文、不做 co-tag 推理、
  不做 edge 点击 UI、不做 alias/embedding 合并。

## 0.2.21 - UI Asset Modularization

- 零构建资产模块化：web UI 不再是一个巨大的 `index.html`。CSS 变量拆到
  `assets/tokens.css`，其余样式拆到 `assets/app.css`；可复用 UI 逻辑拆成
  普通脚本 IIFE 模块：`render.js`（纯 markdown/tool-line helper）、
  `research_drawer.js`、`changes_drawer.js`、`provider_ui.js`，加上原有的
  `research_graph.js`。不上 npm、不上 bundler、不上 ESM——脚本仍按固定顺序
  同步加载。
- 安全资产服务：服务端把手写资产 dict 换成路径 resolver，只允许提供 assets
  目录内的 `/assets/*.js` 和 `/assets/*.css`；目录穿越、目录本身、未知扩展
  一律 404。`index.html` 在响应时替换 `__CODEY_VERSION__`，所有资产引用带
  `?v=<version>` 做缓存失效。
- 薄核心、薄 wrapper：`index.html` 只保留 HTML 骨架、state/storage、session
  操作、SSE ingestion/reconciliation、composer 发送链和 boot 接线。拆出的
  模块在 boot 时通过 `init(deps)` 注入依赖；原调用点走同名薄 wrapper，所以
  DOM 结构、视觉、`/api/*`、SSE reconciliation 和 provider 行为都没有变。
- 架构 ratchet：新增 `tests/test_ui_architecture.py`，强制 inline `<style>`
  为 0 行、inline `<script>` 预算只降不升（当前预算 1950 行，实际 1915 行）、
  每个资产模块只声明一个 `window.Codey*` 命名空间、资产引用带版本号且文件
  必须存在、脚本加载顺序固定。

## 0.2.20 - Research Controller v1

- 生产 Research controller：Research 现在用很薄的 ledger read-model，根据当前
  研究状态只暴露这一轮合理的工具，而不是每次都把所有 Research 工具格式交给模型。
- 稳定 ID：搜索结果、已打开来源和 source_search 定位命中会得到 run-global 的
  `result_id`、`source_id`、`hit_id`。Controller 会先把这些 ID 改写成普通的
  `url` / `pages` / `offset` 参数，再交给 0.2.18 的 typed tool contract 校验。
- 写 evidence 更省复制：controller 模式下，`knowledge_write` 可以使用
  `sources:["s1"]` 和 `evidence.source_url:"s1"`；Codey 会在保存前改写成已打开的
  final URL。
- 非线性门禁：这不是硬状态机。`knowledge_search`、`knowledge_read`、`web_search`
  仍然可用，模型可以回头查本地记忆、反证或更好的来源。`open_url`、
  `source_search`、`knowledge_write`、`knowledge_link`、`done` 只在 ledger 状态
  让它们有意义时开放。
- done 纪律：通常只有保存过 evidence 后才允许 `done`；接近 turn 上限时保留一个很窄的
  “证据不足/无可引用来源”报告出口。确定性的 report quality gate 不放宽。
- 边界克制：没有把 Deep Research Core prompt 进生产，没有 provider 自动路由，没有新增
  UI 模式，也没有放宽 provenance/evidence 规则。

## 0.2.19 - Research Browser Isolation and Thin-Gate Probe

- Research 浏览器隔离：浏览器版 `web_search` 和 HTML `open_url` 现在默认使用独立的
  Research Edge profile 和 CDP 端口，不再和 provider 聊天浏览器共用同一个上下文。
  搜索页、结果页和文章页不会再和 DeepSeek、MiMo、StepFun、Qwen、GLM 的聊天标签页
  混在 9222 浏览器里。
- Isolated CDP 卫生：隔离浏览器 session 选择空闲端口时，不再参考旧的 active/saved
  provider 端口，避免本该隔离的 session 又接回共享 provider 浏览器。
- 页面抓取鲁棒性：HTML fetch 在 `Page.content()` 遇到页面仍在导航或替换内容时会短暂
  重试，避免动态新闻页的一次 DOM 抖动直接变成 `open_url` 错误。
- Research UI 可观测性：事件桥、UI 状态存储和 TURN 分隔线现在都会保留 `(done)`
  这类协议 note，以及 `(direct_answer)` 这类 typed protocol note；如果 `done`
  被质量门或私有 evidence review 退回，或模型需要协议修复，不再显示成空白 turn。
  Runner 也会在要求模型修最终报告前，先把质量门失败原因显示出来。
- Manual A/B thin gate：`tests/manual/deep_research_core_ab.py` 新增 manual-only
  `thin_gate` arm，带 state-aware allowed tools、稳定的 `result_id` / `source_id`
  rewrite，以及原子 `send_start` trace。MiMo 实机 `long-official-doc/thin_gate`
  probe 用 8 轮完成，`done=True`、`quality_score=11`、0 次 protocol repair、
  4 次 ID rewrite。
- 边界克制：还没有把完整 Research controller 进生产，没有自动 provider 路由，没有把
  Deep Research Core prompt 进主链路，也没有新增 UI 模式。thin-gate 只是为 0.2.20
  的 allowed-tools / stable-ID controller 提供证据。

## 0.2.18 - Research Tool Contract and Typed Repairs

- Research 工具合同：所有 Research JSON 工具现在都会先经过本地 typed 参数校验，
  再真正执行，包括 `knowledge_search`、`knowledge_read`、`knowledge_write`、
  `knowledge_link`、`web_search`、`open_url`、`source_search` 和 `done`。
- Typed protocol repair：Research 现在会把协议错误分成 `no_json`、
  `unknown_tool`、`too_many_tools`、`invalid_args`、`direct_answer`、
  `native_search_leak`，然后给模型更具体的修复提示和一个可照抄的 JSON 格式。
- 参数更安全：可选参数只有缺失时才补默认值；`offset="abc"` 这类错误数字会被拒绝，
  不会静默默认；`queries`、`summary` 等 alias 会规范化，模型乱塞的额外字段不会
  继续传给工具。
- 最终报告纪律：`knowledge_write type="synthesis"` 会被拒绝，并提示改用 `done`；
  Codey 会在最终报告通过质量门后自己保存 synthesis。
- Research 浏览器隔离：网页搜索和打开页面现在使用独立的 Research browser worker，
  不再重入 provider 聊天用的 browser worker，修复实机 Research 中出现的
  Playwright sync API / asyncio loop 错误。
- MiMo 发送稳定性：MiMo 现在会在读到回复后短暂等待回答尾部 copy action 稳定，再发
  下一轮，和 StepFun 的节奏修复类似，避免页面正文已稳定但 composer/action 区还没收尾时抢发。
- 实机 provider 检查：Qwen 的 JSON 格式很干净，但 10 轮内仍没走到 `done`；
  MiMo 先被 typed contract 抓到一次 `too_many_tools`，随后通过连续长消息 submit
  probe，并在 footer 等待和最终报告说明修正后，用 9 轮完成
  `long-official-doc/source_search`，`done=True`。

## 0.2.17 - Source Search Production and Research Tool Boundary

- 生产 Research 工具：`source_search` 现在进入默认 Research JSON 协议。它只搜索
  已经通过 `open_url` 打开的来源，返回 locator、offset、PDF 页码和短 preview。
- Research 工具边界：Research prompt 现在明确禁止使用聊天网站自带搜索、浏览、
  插件或外部知识。所有网页和知识访问都必须通过 Codey 本地 JSON 工具完成。
- 单 action 纪律：Research 现在要求 provider 每轮只选择一个工具；Research JSON
  parser 也会拒绝一轮多个 tool call，让 MiMo 这类工具洪水走现有 protocol repair。
- Evidence 边界：`source_search` 不写 evidence、不更新 PDF `pages_read`、不放宽
  report quality。HTML 走软性的 locator 纪律；PDF 页码引用仍必须先
  `open_url pages="N"`。
- PDF 有界扫描：某个 PDF URL 至少打开过一次后，`source_search` 可以重新 fetch
  同一个已打开 URL，并在有边界的前若干页里找 locator；这些页不会被记录成已读证据页。
- Manual A/B 卫生：baseline 可以使用 `JsonToolCodec(include_source_search=False)`，
  避免生产 source_search 污染无 source_search arm；manual harness 现在复用生产
  source-search 匹配逻辑，并提供 manual-only 的 `--single-tool-boundary` probe 开关。
- MiMo 实机补测：没有 single-tool 边界时，MiMo 会反复一次输出多个搜索调用；在 fresh
  tab 加上该边界后，MiMo 用 10 轮完成 `long-official-doc/source_search` fixture，
  `quality_score=11`、`done=True`、0 次 protocol repair，打开目标 offset，保存精确
  evidence，并通过 report quality。
- 边界克制：没有把 `deep_core` prompt 进生产，没有 UI 改动，没有角色路由，没有向量索引，
  也没有给 HTML 增加复杂的 range hard gate。

## 0.2.15 - Source Search Research Hygiene

- Qwen 发送稳定性：Qwen adapter 现在会确认受控输入框在一个短暂稳定窗口里确实保留了
  完整消息；如果页面 hydration 把草稿清空，会有限次数重新填入后再提交。
- Research 协议容错：`web_search`、`knowledge_search` 和 manual A/B 里的
  `source_search` probe 现在都能从 `query` 或常见模型错误 `queries` 中取第一条
  非空 query。
- Research 报告质量：`1. https://final-url - Title` 这种 URL 在前的编号来源行，
  现在会走和 title-first 来源行一样的 provenance 检查。
- Manual A/B harness 卫生：fresh-tab 和 keep-open 诊断参数现在有安全默认值，
  旧的脚本调用不会因为少传 keyword 断掉。
- A/B 证据：DeepSeek、StepFun、Qwen 和本地 Gemma4-12B probe 现在给出一致结论：
  在已打开来源内部做确定性的 `source_search`，能提升长文档/PDF 的证据定位。
  更重的 `deep_core` plan/coverage prompt 仍只保留在 manual A/B。
- 边界克制：没有加重默认 Research prompt，没有加入角色路由，没有 UI 改动，也还没有把
  source-search 工具直接接入生产链路。

## 0.2.14 - StepFun Submit Stability

- StepFun 发送稳定性：更新当前页面的 `custom-icon-send-outline` 发送按钮选择器，
  并用 StepFun 回答尾部的 reload action 做收尾等待。Codey 现在会等回答区域
  渲染稳定后再进入下一轮发送，避免 StepFun 还在收尾时把后续 prompt 吞掉。
- 提交确认：StepFun 不再把 textarea 里的换行或文本变化当成“已经发送”。如果点击后
  不能通过输入框清空或新回答活动确认提交，adapter 会用现有 `SubmissionUncertain`
  边界快速失败，而不是假等到超时。
- Manual probe：新增低发送量 provider submit probe，并给 Deep Research A/B harness
  增加 fresh-tab / 保留错误页诊断参数，方便一个 arm 一个 arm 地实机排查网页模型，
  不必每次消耗完整 research run。
- 边界克制：没有加入 provider 角色路由，没有 UI 改动，没有加重通用 prompt，也没有改
  provider-independent 的 coding / review / research 核心。

## 0.2.13 - Provider Fit Update

- Provider 列表：新增 StepFun，同时保留小米 MiMo。UI、provider registry、
  browser warmup、provider profile、repair policy 和 worker 端口偏移现在同时覆盖
  `mimo` 和 `stepfun`。
- StepFun adapter：新增 `codey/stepfun.py` 和 `StepFunWebProvider`，连接
  `https://chat.stepfun.com/chats/`。adapter 使用 StepFun 的普通 textarea
  输入框，读取最新 markdown 回答并避开 reasoning 区，发送/读取逻辑仍隔离在
  provider 专属代码里。
- 选择依据：实机 probe 显示 StepFun 能按本地 JSON-tool 协议一轮一个 action 地工作，
  这点在 Research fixture 里表现更好。它通过了一个小型 edit-and-test 代码 smoke，
  但前几轮需要现有 protocol nudge 把非 JSON tool-call 标记拉回；从零创建项目的 smoke
  仍卡在 Python 语法修复上，所以暂不把它宣传成最强默认项目 Writer。MiMo 仍适合代码
  编辑/实现，但实机 probe 后不建议用于严格 JSON-tool Research。MiniMax 没有被选中，
  因为它的 Agent 页面首轮就忽略本地 JSON-tool 协议，改用自己的网页/agent 行为。
- 边界克制：没有加入角色路由，没有新增 UI 模式，没有为了某个 provider 加重其它 provider
  prompt，也没有改 provider-independent agent/tool/review 核心。

## 0.2.12 - Research A/B and Provider Parsing Hygiene

- Manual Deep Research A/B：实机 probe 默认使用低发送量的 `cheap` profile，
  明确要求网页模型只通过本地 JSON tools 访问 fixture source，并在每次 provider
  回复后原子写入 `.trace.json`，方便不重新消耗模型额度也能检查 protocol /
  quality-gate 失败。
- Research 诊断：A/B row 现在记录 send/reply 次数、done 尝试次数、protocol /
  quality repair prompt 次数、已打开来源、evidence items、原始回复预览和最后一次
  `done` 的质量门结果。
- Provider 解析：DeepSeek 遇到稳定但格式略坏的 JSON-tool-shaped 回复时，会尽快把
  回复交给 Research protocol repair loop 修，而不是一直等到 timeout。
- Research 质量卫生：接受 `结论[1]` 这类中文紧贴引用；URL provenance 解析也把中文
  括号、反引号和引号视为 URL 边界。
- 运行时容错：CDP attach timeout 延长，以适应负载较高的浏览器会话；warmup timeout
  保持不变。

## 0.2.11 - Provider Readiness Self-Repair

- Provider 诊断：新增克制的 `readiness_stale` failure kind，让 adapter 可以表达
  “安全 DOM facts 显示页面可能可用，但 readiness 信号已经过时”。
- 安全 failure facts：`ProviderFailure` 现在可以携带一小包经过白名单清洗的
  facts。facts 使用显式 readiness allowlist：`composer_visible`、`send_visible`、
  `model_selector_text_present`、`response_count`、`question_count`、`waited_for`；
  普通 failure 的 payload 仍不会输出空 facts。
- Self-repair 路由：`readiness_stale` 作为结构性 provider failure 参与 circuit
  和 self-repair，但仍保持原有纪律：只有 provider circuit 进入 `OPEN` 后才入队。
- Adapter repair prompt：self-repair worker 现在会把 failure kind、stage 和清洗后
  的 facts 传给 adapter repair prompt，让修复模型更容易区分 stale readiness
  check 和真正的控件缺失。
- 边界克制：没有 UI 变化、没有访问用户项目、没有抓取网页正文、没有新增 repair
  framework，也没有绕过现有 fresh-chat marker canary。

## 0.2.10 - Research Quality and Provider JSON Hygiene

- 网页 provider JSON 卫生：把 JSON tool reply 的识别和修复抽到
  `codey/json_tool_reply.py`，DeepSeek 和 Qwen 复用同一套逻辑。Qwen 不再依赖
  已过时的 `/api/v2/models/` bootstrap 信号；稳定输出 JSON tool reply 时能更快
  收尾，并且在复制文本过旧或不完整时优先使用 DOM 里的 JSON。
- Research 质量门：最终报告现在接受 `[1] https://final-url`、带
  `Available at:` 的编号来源等常见格式，但正文引用的来源 URL 仍必须是本轮打开过、
  且有 evidence 支撑的 final URL。`结论` / `关键证据` / `来源质量` / `来源`
  仍严格；`反证与限制` 和 `搜索覆盖` 可以把未打开的搜索结果域名当作限制说明。
- 无可引用来源报告：当 Research 已经搜索但确实没有可引用的已打开来源时，Codey
  现在可以接受明确标注的“无可引用来源”报告，不会逼模型编造引用。
- Research 修复体验：质量门退回时会说明具体硬性要求，不再只给泛泛的 continue
  提示；Research 系统提示也改成中性的“local research agent”，不再写旧项目名。
- 普通聊天显示：聊天回复现在带 `run_id`，`task_done` 会把 chat answer 放进
  summary，前端会对 `reply` / `task_done` 的同一答案去重；普通聊天回复能更可靠地
  恢复并显示在界面里。
- Manual Research A/B：新增 `tests/manual/deep_research_core_ab.py`，用于实机网页
  provider 测试 source-search / plan / coverage 实验。这个 probe 不改变默认生产
  Research 行为。

## 0.2.9 - Runtime Responsiveness Hygiene

- SSE 可靠性：subscriber queue 满时，Codey 现在会丢弃队列里最旧的事件，
  再放入最新事件；不再静默丢掉最新状态更新。
- Research restore 响应性：恢复 Research note changes 后，knowledge index rebuild
  改为合并式后台任务，`/api/research/restore` 不再等待完整 vault scan 才返回。
- Review 响应性：Review 拒绝后进入 repair 时，复用同一个 review cycle 中已经
  刷新的 project map，避免重复刷新。
- 边界克制：没有 API payload 变化、frontend 变化、增量 indexer、workspace watcher
  或后台任务框架。

## 0.2.8 - Research TaskRunner Hygiene

- TaskRunner 整理：把 `TaskRunner.run()` 中 chat、research、hybrid、project
  四条执行路径拆成私有 helper；run reserve/start、provider lifecycle、
  cancellation 和最终 `finish_run()` 仍留在主 orchestration 方法里。
- 边界克制：只在 `task_runner.py` 内部增加很小的 frame/work/hook data carrier；
  没有新增 router、strategy system、task mode 或 module split。
- 兼容性：task events、payloads、provider failover、review flow、project memory
  writes 和 Research handoff 行为保持不变。

## 0.2.7 - Research Server Hygiene

- Research API 卫生：把 `/api/research/graph`、`/api/research/note` 和
  `/api/research/restore` 的响应组装收进 `server.py` 内的小 helper，统一返回
  `(status, payload)`。
- Run submit 卫生：把 `/api/run` 的校验和提交响应组装收进对应 helper，
  `_submit_task()` 和任务执行行为保持不变。
- 边界克制：helper 仍留在 `server.py`；没有新增 router、`server/research.py`
  模块、schema 变化，也没有改变 payload 或 status code。

## 0.2.6 - Frontend Research Graph Split

- 前端卫生：把 Research drawer Graph 实现从单体 `index.html` script 中拆出，
  放到 `codey/web/assets/research_graph.js`。主 HTML 现在只保留 drawer wrapper
  以及 depth、note handoff、source opening 的 callbacks。
- 静态资源边界：新增 `/assets/research_graph.js` 白名单路由；Codey 仍不会把
  `codey/web` 暴露成通用静态目录。
- 兼容性：保留现有 Graph UI、canvas layout、CSS、callbacks 和全局浏览器形态。
  没有 ES module 迁移、bundler、CSS 拆分或前端框架改造。
- 缓存卫生：HTML 通过 `/assets/research_graph.js?v=0.2.6` 加载 graph script，
  让 webview/browser session 在本次 release 中拿到拆分后的模块。

## 0.2.5 - Research Graph

- Research drawer Graph：新增 Obsidian-like 的局部 Graph tab，把当前 Research
  run 里的 notes/source 节点和 `derives` / `supports` / `contradicts` /
  `implements` / `verifies` 关系可视化。第一版使用轻量 canvas force layout，
  支持 hover 高亮、点击详情、打开 source URL、Depth 1/2 和 Reset。
- Graph read model：新增 `codey/knowledge/graph.py`，并给 index 补上 notes、
  incoming/outgoing links、note sources 的有边界查询。Markdown notes 仍是事实源，
  SQLite 仍是可重建 cache；Graph 通过 `/api/research/graph` 按需加载。
- Provenance 展示：URL sources 会派生成虚拟 `source_url` 节点和虚拟 `cites`
  边，只用于 Graph 展示。`cites` 不加入持久 note link kind。
- Counterpoint 卫生：只有当前 run 有 counterpoints payload、且没有真实
  `contradicts` link 时，才生成虚拟 counterpoint 节点。Graph 不解析 synthesis
  Markdown section。
- UI 语言：drawer graph 保持 Codey 的 monochrome 设计，默认是灰白点线；
  `--ok-dot` 只作为交互 accent，用在 hover 节点及其相连边。

## 0.2.4 - Research PDF Intake

- PDF source intake：`open_url` 现在可以直接读取文本型 PDF。没有 `open_pdf`
  工具、PDF 模式或额外按钮；PDF 只是 Research 的一种来源类型。
- 有边界提取：Codey 默认只读取有边界的页码范围，PDF 下载会 streaming 读取并带硬
  byte cap，同时限制提取文本长度。扫描版、超大、空文本或提取失败的 PDF 会返回
  中性的 `SKIPPED`。PDF redirect 会由 Codey 手动逐跳处理，每次发起下一跳请求前
  都先经过 URL policy，公开 PDF URL 不能悄悄跳到本机或内网地址。
- 页码级证据：Evidence Ledger 会记录 `content_kind`、MIME type、总页数、
  已读页、截断状态和 snippet 的页码定位。`knowledge_write` 可以接收
  `evidence.page`，也可以从 snippet 自动推断页码；如果 excerpt 不匹配，
  Codey 会替换成打开过的 PDF 页里的真实短摘录。
- 报告质量门：最终报告可以用 `[1 p.4]` 或 `[1 pp.4-5]` 引用 PDF 页码证据。
  只有对应 PDF 页真的读过，并且该页有 snippet-backed evidence，页码引用才会通过。
- UI 与项目衔接：Research drawer 的 `Evidence` 会显示 PDF 页码定位，`Sources`
  会显示 PDF/已读页/截断元信息。Synthesis note 和 Project Brief 会带上同一份
  页码级证据，但仍不会把整个 vault 注入 Writer。
- 依赖：新增 `pypdf>=6.0,<7`，用于纯 Python PDF 文本提取。

## 0.2.3 - Research Provenance 收口

- Research provenance：显式 URL 引用仍然严格匹配实际打开过的 final URL；但
  `来源质量` 这类文本里的裸站点域名现在更自然。比如打开了 `docs.python.org`
  后可以写 `python.org`，但只打开 `python.org` 不能反过来声称已经打开
  `docs.python.org`。
- Research 质量门：URL 片段不再参与裸域名扫描，所以 `pathlib.html` 这类路径不会被
  误判成没有打开过的来源域名。
- 项目记忆：新增集成回归，锁住验证通过后的实现记忆路径，包括 implementation note、
  verification note，以及从 research synthesis 指向实现的 `implements` link、从实现指向
  验证记录的 `verifies` link。
- 卫生：删除 task runner 里残留的未使用 `EvidencePack` import。

## 0.2.2 - Research 报告质量门

- Evidence Ledger：每次 Research 现在会记录搜索 query、排序后的搜索结果、实际打开的
  requested/final URL、读取时间、来源质量提示，以及短 evidence snippet。
- 报告质量门：最终 Research 报告必须包含 `结论`、`关键证据`、`反证与限制`、
  `来源质量`、`搜索覆盖` 和 `来源`。正文里的编号引用必须对应本轮 Codey 实际打开过的
  final URL，并且每个被引用来源都必须有至少一条从打开页面原文复制出来的已保存
  evidence snippet。
- 证据约束：note 里附带的 evidence snippet 必须真实出现在打开过的页面正文中。
  search result 仍然不算证据，必须先 `open_url`；不合格报告会被退回 Researcher
  修订，而不是直接保存为 synthesis。
- Research UX 恢复：PDF 这类不可读来源现在显示为中性的 `SKIPPED` 工具结果，
  Researcher 可以继续改读 HTML 来源；如果模型给了改写过的 evidence excerpt，
  Codey 会替换成打开页面里的真实短摘录，同时继续保持质量门严格。
- 报告解析：质量门现在接受 `1. 结论` / `一、结论` 这类常见编号标题，也接受
  Markdown link 形式的来源行，同时保持引用 provenance 严格。
- 顾问：Research MoA advisor 现在会收到更完整但只读的 EvidencePack，包含 citations、
  evidence items、coverage、notes 和 source URLs；advisor 仍然不能浏览网页，也不能写 vault。
- UI 与项目衔接：Research drawer 现在分为 `Evidence`、`Sources`、`Notes` 三个 tab，
  搜索覆盖作为支持性的审计信息放在 `Evidence` 里。Project handoff 带入有边界的
  Research Brief，包含 citation map、evidence items、counterpoints 和 source-quality
  risks，而不是整个 vault。

## 0.2.1 - Research 收口与 UI 体验修正

- Local provider：移除显式清除 saved key 的勾选框。API key 留空会保留旧 key；
  输入新 key 后点击 `Connect` 会覆盖旧 key。
- Research evidence flow：引用搜索结果 URL 但还没有打开时，现在返回中性的
  `NEEDS_OPEN`，不再显示红色错误。`NEEDS_OPEN` 是 `needs_action` 状态，
  不算 saved note，也不算 changed tool result；通用事件和 Web/SSE 生产事件路径都
  透传同一状态。
- UI：`Research` 选中态只让文字变亮，不加边框、背景或字重。Assistant 回答默认展开，
  长回答提供 `Collapse` 手动折叠。
- Markdown：assistant 报告渲染支持 `#` 到 `######` 标题和基础嵌套列表，
  仍然不引入依赖，并保持单色设计。

## 0.2.0 - Research、Knowledge 与本地模型

这是一次重要工作流升级。Codey 不再只是本地编程循环：它可以显式 Research 一个问题，
保存有来源约束的本地笔记，把有边界的 synthesis 带进项目，并把验证过的实现事实继续沉淀下来。

- Research：输入框上下文现在有 `Research`。开启后，Codey 可以搜索网页、打开页面、
  执行 URL policy、写 source/fact/synthesis notes，并拒绝最终报告引用本轮没有打开过的来源。
- Knowledge：研究笔记存入本地 Markdown vault，配套可重建 SQLite FTS 索引、
  单次 run restore、note links，以及给项目 handoff 使用的有边界 Research Brief。
  项目源码不会被复制进 vault。
- 项目衔接：研究结束后选择项目文件夹，Writer 收到的是 Research Brief，而不是整个 vault。
  连续 Research 和项目内 Hybrid Research 会带上有边界的同一聊天前文，所以“继续查刚才那个方案”
  不会丢上下文。
- Local provider：`Local` 可以连接 LM Studio、Ollama、llama.cpp 等 OpenAI-compatible
  endpoint。轻量配置弹窗支持 base URL、model id、可选 API key 保留；输入新 key
  后点击 `Connect` 会覆盖旧 key。
- 安全与可靠性：网页版 provider send 仍留在 browser-worker 线程里；只有明确线程安全的
  Local send 使用可取消后台发送。browser-worker call 支持 reentrant，避免 Research 搜索自锁。
  Codey 运行路径里的隐藏浏览器启动代码已删除。
- UI：Research 是 `Choose folder` 和模型名旁边的轻量 composer token，不是第二个 app，
  也不是模型选择旁边的新按钮。Research drawer 会显示 notes、来源 URL、synthesis 和 restore 状态。

## 0.1.63 - 单 Provider Self-Review

- Review：当没有可用的不同 provider 来做最终 diff review 时，Codey
  现在会为 Writer 的同一个 provider 打开一个临时 fresh tab，并运行明确标注的
  self-review。
- 修复链路：self-review 的 finding 继续复用现有 Reviewer 到 Writer 的
  repair 流程，但 Writer follow-up 文案不再声称是“第二模型”审查。
- 安全：真正的双模型 review 仍然优先。self-review 不会清空 Writer 的
  provider session，临时 reviewer tab 会在 `finally` 中关闭；如果
  self-review 也失败，仍然沿用原来的单模型结果降级。

## 0.1.62 - Review Impact Map

- Review：最终 diff review 现在会在 ChangeSet 摘要后收到一段短小、有边界
  的 Review Impact Map。它列出明显变化的 symbol，以及本地 caller/test
  引用提示，让 reviewer 更容易检查影响半径。
- 可靠性：changed symbol 提取集中到 `changed_symbols.py`，并被 Verification
  Map 复用。rename 场景会用旧 symbol 名做引用扫描，同时保留 ChangeSet 解释
  出来的新文件路径。
- 安全：这张 map 只给 Review 使用，不包含源码正文，失败时静默跳过，并明确标注
  不是 coverage proof。Writer 行为、UI、工具、provider 逻辑和 `/api/changes`
  都不变。

## 0.1.61 - ChangeSet 锚点审查

- Review：最终 diff review 现在会先收到结构化 ChangeSet 摘要，再看到原始
  diff；摘要包含 changed files 和解析出来的 hunk 范围。
- 可靠性：reviewer finding 可以带可选的 `hunk_index`、`new_line` 或
  `old_line` 锚点。Codey 会先用真实 changed hunks 校验这些锚点，再交给
  Writer；只包含 path 的 finding 仍然有效。
- 兼容性：`/api/changes`、UI diff drawer、receipt、restore，以及
  `changes.py` 的底层 dict 输出都不变。Git rename 的 `old.py -> new.py`
  展示标签只会在 ChangeSet 解释层规范成新路径，让 review hunk 能正确挂到
  rename 后的文件上。

## 0.1.60 - CLI Agent JSONL

- CLI：`python -m codey agent --json ...` 现在会在 stdout 按 JSONL
  输出事件流，包括 session header、agent start/end、turn、status/info、
  以及有边界的 tool start/result 记录。
- 集成：JSONL 输出给脚本、CI wrapper、benchmark 和外部启动器使用，让它们
  不用解析给人看的 stderr 日志，也能稳定读取进度和最终结果。
- 安全：普通 CLI 模式不变。JSONL 的工具记录只包含紧凑 result 摘要、
  有边界的文本字段和 command/status 元数据；provider、server、UI、
  agent 和工具执行行为都没有变化。

## 0.1.59 - Package Manager Setup 提示

- 可靠性：setup context 现在和可信验证发现使用同一套 Node package manager
  识别规则：先看 `packageManager`，再看当前 lockfile，再向上找 lockfile，
  最后才退回 `npm`。
- UX：setup 提示现在会给出更具体的安装命令，比如 `pnpm install`、
  `yarn install`，或 `npm ci or npm install`，不再只是泛泛地说 package install。
- 一致性：shell 审批后的 follow-up 现在复用统一的 verification candidate
  formatter，带 cwd 的检查命令格式会和 Project Map / Verification Map 保持一致。

## 0.1.58 - 成功改动检查保留工作目录

- 可靠性：successful-change facts 现在会保留本地检查命令的工作目录，所以 scoped
  验证会渲染成 `backend/: npm test`，不再丢掉“这个检查是在 backend 里跑的”。
- 兼容性：旧的 `successful_changes[].checks` 字符串格式仍然可以读取，并默认
  `cwd="."`；新写入会使用 `{command, cwd}` 结构化 check 记录。
- 安全：非检查命令、敏感命令和不安全的工作目录仍然会被过滤，不会进入持久项目事实。

## 0.1.57 - 验证候选命令来源统一

- 可靠性：Project Map 和 Review Verification Map 现在都从同一套可信的
  `verification_policy` 发现路径接收候选检查命令，不再由 Project Map 自己
  根据 manifest 另猜一套命令。
- Review：Verification Map 只会把唯一选中、和本次改动相关的检查命令标为
  `Recommended local check candidates`；没有唯一选择时，其他命令仍会放在更弱的
  broader candidate 标签下。
- 清理：直接调用 `render_project_map()` 不再推断候选命令。需要生产上下文的
  manual probe 现在改用 `ProjectTaskContextBuilder`，让评测脚本和真实 Writer
  路径保持一致。

## 0.1.56 - Composer 文件夹文案收敛

- UX：无项目聊天里的 composer context 现在始终显示 `Choose folder`，即使输入框里有
  草稿也不再显示更长的发送文案。草稿发送行为仍然只通过同一个明确的文件夹点击触发，
  但不会占用可见 composer 文案。
- 安全：项目访问行为不变。用户仍然必须明确选择文件夹；在无项目聊天里按 Enter
  仍然只是普通聊天发送。

## 0.1.55 - 草稿接入项目发送

- UX：普通 New Chat 现在可以从输入框上方的项目上下文原地接到一个项目文件夹。
  如果输入框里已有草稿，同一个明确的文件夹点击会保留草稿，并在用户选完文件夹后
  用同一个 session 发送。
- 连续性：chat -> project 的切换现在会保留之前普通聊天里的 handoff 和最近可见对话事实，
  Writer 不会丢掉“先讨论方案，再落到项目里执行”的上下文。
- 安全：没有自然语言意图识别，也不会自动开放项目访问。用户必须明确点击文件夹上下文；
  在无项目聊天里按 Enter 仍然只是普通聊天发送。

## 0.1.54 - 可信验证发现增强

- 可靠性：改完代码后的可信验证发现现在能识别更多本来就已被本地 `run`
  工具允许的安全检查，包括按 `packageManager`/lockfile 选择的 package
  scripts、`pytest` 配置、`tests/` unittest discovery、`ruff`/`mypy` 配置，
  以及简单安全的 Makefile target。
- 选择策略：completion 阶段的验证候选现在有一层很小的命令优先级，避免发现
  `test`、`typecheck`、`lint`、`build` 和 Makefile target 后让常见项目变成
  ambiguous。更具体的生态命令会优先于 Makefile fallback。
- 安全：没有 UI 改动，没有自动安装，没有扩大 shell 权限，也没有新增自动执行行为。

## 0.1.53 - CDP 浏览器预热

- UX：启动 UI 后会排队执行一次 best-effort 浏览器预热；当没有任何 provider
  标签页可见时，Codey 会准备受控 CDP 浏览器并打开 DeepSeek、Qwen、MiMo 和 GLM。
- 安全：预热不会检查登录态、不会发送测试消息、不会改 UI，也不会绕过 provider
  supervisor 的健康状态过滤。已有 provider 标签页会被复用，不会重复打开 provider 页面。
- 可靠性：预热运行在统一 browser worker 上，使用较短超时，不复用无关外部 CDP
  浏览器；慢加载但已到达目标 URL 的 provider 页会保留，失败的空白预热页会关闭。

## 0.1.52 - Provider Send Loop 收拢

- 可维护性：新增共享的 provider send-loop 小原语，收拢 response watch 生命周期、
  响应稳定状态、completion flow 检查、flow response 读取和标准 timeout recovery。
- 范围：GLM、Qwen、DeepSeek 和 MiMo 都已迁到共享 helper，但各自的提交、
  completion、重试和 response 读取差异仍保留在对应网页 driver 内。
- 安全：没有 UI 改动，没有 selector 改动，没有 provider 基类，也没有引入宽泛的
  `run_send_flow` callback 框架。

## 0.1.51 - Shell 审批后续提示

- 后续提示：已批准的 shell 结果现在会附带短小的内部提示，提醒 Writer 注意命令失败、
  输出截断、PATH 刷新、dev server 歧义、发布确认，以及相关的可信本地检查。
- 安全：这些提示不会自动执行命令、重试安装或改变 UI。Writer 仍然必须显式请求下一次
  工具调用或 shell 审批。

## 0.1.50 - Setup-aware Shell 审批

- UX：Shell 审批卡现在使用中性的 `Approval required`，并针对依赖安装、系统安装、
  外部源码获取、发布、开发服务器和普通 shell 命令显示简短风险说明。
- 上下文：用户批准 setup 类 shell 命令后，Codey 会给 Writer 回传一段有界只读的
  `Setup Context`，包含本机工具可用性、项目 manifest、lockfile 和带目录作用域的
  setup 提示。它不是新的模型工具，也不会注入普通 prompt。
- 安全：Setup Context 本身不会安装、clone、写文件或联网。它复用敏感路径过滤，
  不暴露工具绝对路径，会说明列表上限，并继续把 shell 执行放在现有用户审批之后。

## 0.1.49 - Tool Start 可见性

- UX：Agent 工具现在会在本地执行开始前发出轻量 `tool_started` 事件。
  Web UI 会先显示安静的 pending 工具行，例如 `read app.py -> Reading app.py`，
  工具完成后再替换成最终结果。
- 设计：生产工具执行仍然保持串行和可观察。pending 行复用现有单色
  `.tool-line` 样式；不加入 spinner、progress 系统、并发 runner 或 ToolSpec registry。
- 安全：`tool_started` 只服务 UI/CLI 可见性，不计入 execution evidence、
  reviewer recent log，也不参与任务完成进度判断。

## 0.1.48 - 工具函数注入和并行 Probe

- 改进：Agent runtime 现在支持显式 `AgentToolFns` 注入，测试和手工 probe 可以替换
  工具函数，不再 monkeypatch `codey.agent` 全局函数。
- UX 决策：生产 Codey 默认保持 `read`、`ls`、`search` 串行执行。deterministic
  probe 证明只读并发 batch 可以缩短本地 wall-clock，但串行 tool event 更可观察，
  更符合 Codey 作为安静本地开发工具的气质。
- 安全：bounded file scan 和 search 的长循环现在会检查协作式 cancellation。
- 测试/probe：手工 A/B probe 现在使用显式工具函数注入。只读 parallel probe
  保留为脚本内实验，并记录为什么生产 Codey 默认不启用只读并发。

## 0.1.47 - Search 遗漏提示

- Bugfix：`grep` / `search` 现在会报告非 UTF-8 文件和不可读文件，不再把这些被省略的
  文件静默当成“完整搜索后没有匹配”。这个修复只作用在 Writer 的 search 工具上：
  oversized、read budget、bounded scan budget 的旧提示保持不变，也不迁移 hidden advisor
  search。

## 0.1.46 - Coverage-aware References

`find_references` 现在会说明有边界的文本引用扫描跳过了哪些可能仍包含引用的文件。
底层引用扫描器只收集紧凑的 `ScanReport` 事实：oversized 文件、不可读文件、
非 UTF-8 文件；不会暴露这些文件的源码内容。Writer 可见的工具层会把这些事实渲染成
很短的 `Scan coverage` 提示，并把工具结果标记为 truncated，让 JSON 工具协议继续提醒
模型不要把省略内容当成安全或干净。

这是一个刻意收窄的 production slice。隐藏 project-audit advisor 仍然使用旧的低层
reference 输出；Project Map、Verification Map、持久索引、缓存层和 ScanPolicy profile
都没有扩张。手工 scan-coverage A/B probe 现在会重建旧的低层 baseline，并和生产 Writer
coverage renderer 对比。

DeepSeek、MiMo、Qwen 和 GLM 的实机 A/B 已确认这个行为。GLM 的旧 baseline 仍会误称扫描完整，
并给出自信的 unused 结论；coverage arm 会明确指出跳过的 oversized 文件，并产出安全的
“扫描不完整，不能确定”回答。

## 0.1.45 - Provider Adapter 自修复

当网页变化大到 Provider adapter 代码本身也失效，并且控件级恢复和 Flow 级恢复都不够时，
Codey 现在可以尝试有边界的后台自修复。明确的结构性 Provider 故障会进入去重队列，
不会阻塞新的用户任务；失败的修复不会直接丢失，而是进入冷却期后等待下次重试。

修复运行在独立 Python 子进程中。健康 helper 模型只会看到坏掉的 Provider id、
有边界的故障上下文、允许修改的 adapter 源码和只读 Provider 测试。第一版只允许
在临时 sandbox 里修改目标 Provider 的 adapter 文件；核心文件、测试文件、registry、
tool runtime、server、supervisor、恢复模块和安全控制面都不在自修改范围内。

候选必须通过修复策略、`py_compile`、Ruff、对应 Provider 单测和中性 marker canary，
才会成为本地 provisional override。Override 只通过子进程 Provider worker 加载，
不会直接进入 Codey 主进程。子进程使用同一个已登录 Codey 浏览器 profile 的后台新标签页，
不需要复制 cookie，也不会抢用户当前聊天页。worker 卡住时，父进程会先按 CDP target id
关闭临时标签页，再清理子进程。

本地 adapter override 会记录内置 Codey base hash，先 provisional，只有自然成功后才
晋级 active；连续结构性失败会回滚。第一个 helper 产出非法候选、policy 失败、测试失败
或 canary 失败时，会继续尝试下一个健康 helper。

最终共享主 profile fresh-tab 路径已在 DeepSeek、Qwen、MiMo 和 GLM 上做过实机 smoke：
repair helper 和 candidate worker canary 都能真实发送并读回中性 marker。Qwen 暴露了
这次关键设计修正：独立 profile 可以看起来已经登录，却无法真正提交；复用主 Codey
登录 profile 的后台新标签页后可以正常发送。

## 0.1.44 - Focused Project Map

Project Map 现在会在有任务时加入一个有边界的 `Focused subtree` 小节，用来帮助
模型在深层仓库里更快找到相关模块。它在固定的文件数、目录数、单文件大小、总字节数
和输出字符预算内扫描源码文件，只展示最高分模块的相对路径、source/test 标记和符号
签名。它不展示源码体、不建索引、不持久化、不增加 UI，也不会额外调用 planner 模型。

Focused subtree 只在存在 task 且普通 Symbol overview 可能受大仓预算影响时出现。它出现时
会替代普通 Symbol overview，让 Project Map 把 token 留给深层任务相关模块，而不是继续展示
低相关的前序文件符号。

Qwen readiness 也更稳：Codey 现在会等聊天输入框、bootstrap 信号，以及模型选择器文本
连续两次读取相同且非空后才发送。输入框清空不再被当作提交成功；Qwen 提交确认只认 stop
出现或回答数量增加。

手工 probe 也记录了两条未采纳路线：两段式 scoped planner 和本地 deterministic
pre-scope 都没有证明值得进入生产。最终保留的是更轻的分层 map。四个网页模型在深层
synthetic monorepo 上从 `0/16` top1 提升到 `16/16`，同时 prompt 字符从 `53,424`
降到合并后实机复测的 `33,564`。

内部上，项目任务上下文准备逻辑已从 `TaskRunner` 抽到
`codey/project_task_context.py`。这个 builder 负责 verified facts、Project Map、
checkpoint 恢复/启动、checkpoint prompt 和初始验证候选；`TaskRunner` 仍然负责
Writer、Review、Receipt、会话状态、Provider 接管，以及显式的 evidence seed /
invalidate 调用。

Diff Review 生命周期也已抽到 `codey/review_coordinator.py`。Coordinator 负责 Review 前
diff 重试、是否值得 Review、Review 不可用时降级、Reviewer 反馈生成 Writer followup、
repair 后 diff dirty 状态，以及 review repair 后极窄的绿色检查继承规则。Reviewer
连接、Writer 接管、Receipt、ProjectFacts 和会话状态仍然留在 `TaskRunner`。

## 0.1.43 - 安静的 UI 持久化与侧边栏打磨

UI 状态持久化现在会在 SSE 热路径上节流。连续的 turn、tool、info 事件会合并
localStorage 和服务器端全量保存；用户主动操作和任务终态事件仍然立即 flush。
这能减少长任务中的隐藏序列化、网络 POST 和原子写盘开销，同时不改变持久化数据结构。

侧边栏不再使用浏览器原生 `prompt()` 和 `confirm()`。聊天和项目重命名改为就地输入框，
删除/清空等危险操作改为现有单色菜单内的安静二次确认。

连续的只读工具行现在会在渲染层折叠为更紧凑的分组，例如 `read · 5 files`。
单独一条 read/search/list/reference 仍然正常显示；只有连续同类安全工具行才会分组。
edit、run、shell 和错误行始终展开。

内部上，Writer Provider 接管逻辑已从 `TaskRunner` 抽到一个小型、可测试的状态机。
这个重构不改变 Provider 协议或用户可见行为，但让接管、共享 turn 预算、canary、
checkpoint refresh 和 Stop 优先级更容易证明正确。

## 0.1.42 - 更多检查命令与克制 Markdown

受控 `run` 现在可以执行更多常见验证命令，而不需要走不安全的 shell 路径：
`ruff check`、`ruff format --check`、`mypy`、`python -m mypy`、
`python -m ruff`、安全的 `make` 目标、`bun test` 或允许的 `bun run` 脚本，
以及安全的 Deno test/lint/check/fmt 形式。会修改文件或安装依赖的形式仍然拒绝，
例如 `ruff --fix`、没有 `--check` 的 `ruff format`、`mypy --install-types`、
`make deploy`、`bun install` 和 `deno run`。

完整测试/构建套件现在有 300 秒超时，快速命令继续使用 90 秒预算。超时反馈会明确
说明这是 timeout，不是测试断言失败，并提示 Writer 运行更小的子集，而不是猜测代码
修复。字面 grep 达到匹配上限时，也会提示缩小查询或指定子目录。

本地 UI 的助手回复现在支持一小部分单色 Markdown：代码块、行内代码、粗体、标题和
简单列表。代码块带安静的复制按钮。仍然没有语法高亮、没有新配色，也没有新增模式或
面板。

## 0.1.41 - 智能分页提示

`read_file` 只返回大文件的一页时，现在会直接附带下一页的 JSON 工具调用。
Codey 仍然保持原来的完整行分页和 `next offset` 文案，但额外给出同一路径、下一
offset 和当前有效 limit 的 `read_file` 调用，让 Writer 不必自己推断怎样继续读。

这个提示使用 JSON 转义，只在确实还有下一页时出现，不改变读取预算、文件内容、
工具协议或截断语义。

## 0.1.40 - 有边界的 Stacktrace 降噪

受控 `run` 输出现在会在原有中间截断预算前，先折叠明显的依赖库调用栈。
Python 会折叠来自 `site-packages`、`dist-packages`、`.venv` 和 `venv` 的依赖
frame，并一起折叠紧随其后的依赖源码行。Node 只折叠明确的 `at ...` stack
entry，且路径必须带有 `:line:column` 并位于 `node_modules` 或 `.pnpm` 内。

项目源码 frame、assertion 信息、异常摘要、测试名和普通日志都会保留。如果没有
可折叠的依赖调用栈，输出会字节级保持不变。这个能力不改变工具协议、退出码、
`ok`、`changed` 或 `truncated` 语义。

## 0.1.39 - MiMo Typing Flow 与中性网页标记

MiMo completion 恢复现在复用网页明确提供的 `data-is-typing` 状态转换。
Flow 观察严格区分 true、false 和不可用三种状态，因此属性缺失或 DOM 读取异常
绝不会被当成完成。恢复仍要求先观察到 typing，再明确转换为 false，同时回答
非空且文字稳定；内置完成判断始终优先。

短回答、长代码和深度思考实机 probe 均观察到所需转换，Flow 判定完成后回答没有
继续增长。强制 Flow 测试在第一次发送后保存 provisional 规则，并在下一次自然
发送中晋级 active。网页可见的验证 marker、临时 DOM 属性、页面全局变量和剪贴板
sentinel 现已全部使用中性名称；仅存在于本机的配置名称保持不变。

## 0.1.38 - 有边界的 Provider Flow 恢复

Provider 恢复包除了经过验证的控件，现在还能携带一条有边界的网页状态规则。
Flow Recipe 只能使用固定的布尔观察，不包含 selector、JavaScript、URL、任意动作、
网页正文或项目数据，并且复用现有的 provisional、晋级、失败计数和回滚生命周期。

completion 恢复必须同时看到稳定非空回答，以及从生成证据到终止证据的真实转换；
只凭文字稳定绝不会判定完成。Qwen 首先使用 stop 从可见到消失的转换进行安全试点。
MiMo 和 GLM 没有同等可靠终止证据时，继续使用内置完成逻辑并安全降级。四家
Edge/CDP 控件故障注入全部通过；更严格的 Qwen 实机测试关闭内置完成判断后仍能
完成回答，并在下一次发送中复用 Flow、从 provisional 晋级 active。

## 0.1.37 - Python 语法回归提示

Python 文件的 replacement edit 成功后，如果原文件语法有效、最终修改结果却无法解析，
Codey 现在会立即返回一条有边界的语法回归提示。文件仍然正常写入，edit 仍然成功；
Codey 不会自动回滚、运行命令，也不会把提示算成绿色检查。原文件已经损坏、修改后
语法有效、非 Python 文件，以及超过 128K 字符解析预算的文件都不会产生提示。

DeepSeek、Qwen、MiMo、GLM 的实机 A/B 均避免了一次失败测试，并保持最终代码正确、
独立测试通过；其中三家还减少了轮次或工具调用。四家的合法编辑 control 均为零提示。

## 0.1.36 - Provider Revival 与 Writer 接管

Provider 恢复现在以一次有边界的事务覆盖输入框、发送按钮和回答读取。本地发现
无法确定时，Codey 最多依次询问三个健康的兄弟模型，让它们只从脱敏后的结构候选
中选择；候选必须经过一次真实发送验证，才会原子保存为 provisional 恢复包。
下一次自然发送成功后恢复包晋级 active，明确的连续控件失败则自动回滚上一版。

新增被动 Provider 健康熔断，区分结构故障、临时错误、限流、提交状态不确定、
登录和验证码状态。Writer 遇到明确的网页 Provider 故障后，可以在严格的新会话中
把未完成任务交给健康兄弟模型，并且只传递有边界的本地 checkpoint 事实。切换次数
和总轮次有上限；Stop、普通工具失败、协议失败和提交不确定都不会触发危险重发。
DeepSeek、Qwen、MiMo、GLM 的 Edge/CDP 故障注入均验证了自动恢复和持久复用。

## 0.1.35 - Default Post-edit Verification

代码发生修改后，如果 Codey 能确定存在唯一、可运行且与变更文件匹配的检查命令，
现在会在完成前提醒 Writer 处理一次验证。候选只来自历史成功检查，或项目明确配置的
pytest、npm、Cargo 和 Go 入口。最后一次修改后的绿色检查会直接复用；仅文档变更、
命令歧义、执行程序缺失或跨技术栈误配都不会启用门槛。Codey 不会自动安装依赖或
自行执行命令，默认检查失败后也不会无限重复。
完成边界会重新读取当前 manifest，因此任务中修改检查脚本后不会继续使用过期命令。

## 0.1.34 - Bounded Edit Failure Context

精确 replacement 失败后，如果能证明存在唯一词法锚点，Codey 会返回有边界的
当前文件证据；匹配不唯一时，最多返回三个真实起始行。写入判定仍然完全精确：
不会自动采用 closest match，不会把超长行截断后伪装成可复制代码，也不会把内存中
尚未落盘的半成品当作磁盘证据。正常成功 edit 不增加 prompt 成本或输出。

## 0.1.33 - Read-before-edit Guard

新增本轮 agent run 内的读后编辑保护：Writer 对已有文件做精确替换前，必须先在
本轮成功读过该文件。`content` 全量写入只允许创建新文件；已有文件必须使用精确
replacement。本轮创建或修改过的文件会被视为已知，后续可继续做替换编辑。这样
Symbol overview 仍只是导航提示，不会变成跳过真实文件检查的理由。DeepSeek 和
GLM 也新增了可见限流重试按钮的短冷却后自动点击。初始项目提示不再暴露绝对
临时路径，也不再显示空 instructions 小节；只有实际存在 `AGENTS.md` 或
`CLAUDE.md` 时才加入仓库说明。

## 0.1.32 - Bounded Symbol Overview

在现有 Project Map 里加入 task-aware 的 Symbol overview，让 Writer
第一次读文件前先看到更准确的文件和符号导航提示。它仍然是有边界、本地
只读的小节：不增加 UI、不增加公开工具、不做缓存/索引/embedding/LSP，
也不把源码体塞进 prompt。Qwen 还新增了窄范围恢复：容忍重定向
`net::ERR_ABORTED`，并对一次确认提交后无回复的卡顿做单次重试。

## 0.1.31 - Structured Execution Evidence

新增有边界的内存执行证据账本，让 Verification Map、Review、任务收据和成功项目事实共同使用同一份读取、搜索、编辑、截断和最后一次编辑后检查记录。

## 0.1.30 - 简化导航工具

实机评估显示 Project Map、字面量 `grep`、`find_references` 和带 offset 的 `read_file` 更稳定，因此完整移除了已撤回的 `outline_file`。

## 0.1.29 - Verification Map

新增隐藏且有边界的测试候选与检查证据，供 Reviewer 判断验证是否充分；它不声称能证明影响范围或测试覆盖率。

## 0.1.28 - Durable Execution Checkpoint

为未完成的项目任务保存同 session 的恢复事实：改动文件 hash、仍然有效的成功检查、最后一次 edit/run 和中断原因。

## 0.1.27 - Find References 与有边界扫描

新增有边界的文本引用提示，并让 references、grep 和隐藏审查共用流式扫描器，扫描不完整时会明确说明。

## 0.1.26 - Outline File 实验

曾加入 `outline_file` 作为有边界的导航实验；自然使用评估显示采用率较低，该工具最终在 0.1.30 完整移除。

## 0.1.25 - Hidden Project Map

新增有边界、只读的项目结构图，供 Writer、隐藏顾问和 Reviewer 使用，不索引源码、不引入 RAG，也不增加 UI。

## 0.1.24 - Hidden Change Briefs

新增 Writer 与 Reviewer 共用的私有 ChangeBrief，并只从真实改动和检查沉淀验证过的成功事实。

## 0.1.23 - 浏览器启动健壮性

加入 Edge 优先、Chrome 回退的浏览器发现，改善 WebView 启动失败说明，并明确标记工具和 Review 结果截断。

## 0.1.22 - Durable Conversation Handoff

网页模型上下文不再可信时，在事实 handoff 中加入有边界的近期可见对话摘录。

## 0.1.21 - Durable Chat State

持久化有边界的侧边栏和聊天状态，增加低干扰复制按钮，并在重启后对齐 Send/Stop 状态。

## 0.1.20 - Quiet Chat Controls

继续收紧紧凑聊天控件和交互状态，不增加新的工作流或模式。

## 0.1.19 - MiMo Answer Completion

把 MiMo 发送按钮识别与回答完成判断分开，改用回答 DOM 避免过早判定完成。

## 0.1.18 - Provider Reliability

收紧 MiMo、Qwen、GLM 的网页状态处理、本地 JSON 协议验证，以及 Review 修复后的检查新鲜度。

## 0.1.17 - Hidden MoA Layer

新增隐藏的主模型优先多模型建议：普通聊天与新项目使用草稿评议，已有项目使用有边界的只读顾问审查。

## 0.1.16 - Plain Chat 与项目讨论

New Chat 保持无项目权限，同时让同一项目对话可以自然地从讨论进入读取和编辑。

## 0.1.15 - GLM Provider

加入第四个网页模型 GLM，并统一 Provider 注册和 smoke 选择。

## 0.1.14 - 协议效率与安全

统一本地工具契约，限制安全并行读取，分页读取大文件，并让多替换 edit 保持原子性。

## 0.1.13 - Runtime Ownership Cleanup

统一 Git 与 snapshot 改动处理、集中运行时存储，并明确 Provider session 的生命周期所有权。

## 0.1.12 - Resilient Run Reconciliation

加入有边界的后端运行快照，在刷新或短暂断线后按顺序恢复 UI 真实状态。

## 0.1.11 - Responsive Stop

让 Stop 能中断 Provider 等待、恢复、Review 和受控命令，并同时保留长输出的开头与结尾。

## 0.1.10 - ProfileDoctor Recovery

新增有边界、严格脱敏的第二层恢复：可让已打开模型从结构候选中选择网页控件。

## 0.1.9 - Bounded Provider Recovery

加入版本化 Provider Profile，并对变化后的输入框、发送按钮和回答做保守、经过验证的重新发现。

## 0.1.8 - Durable Local Continuity

持久保存少量已验证项目命令、有边界的事实聊天快照，以及非 Git 项目的恢复基线。

## 0.1.7 - Structured Runtime

引入结构化工具结果与事件，分离任务编排和 HTTP 传输，并让 UI 不再解析散文日志。

## 0.1.6 - Hidden Context Handoff

接近共享上下文预算时，生成有边界的事实摘要并在新网页对话中继续。

## 0.1.5 - Control Teaching Cleanup

收紧用户教学网页控件后的恢复和清理，同时保持教学只作为低干扰的最后兜底。

## 0.1.4 - Task Receipts

新增紧凑任务收据，显示改动文件、检查状态和是否可恢复。

## 0.1.3 - Durable CDP Browser Reuse

Codey UI 重启后优先复用已有 Edge CDP 浏览器和模型网页，再决定是否启动新浏览器。

## 0.1.2 - Provider Status 与输入快捷键

改善 Provider 状态反馈和以键盘为主的消息输入操作。

## 0.1.1 - Stability Smoke

为最初的本地网页模型工作流补充发布级稳定性 smoke 验证。

## 0.1.0 - 首个双语版本

发布首个中英文 Codey：把支持的网页 AI 聊天克制地连接到本地文件编辑、检查、Diff 和恢复。
