# 版本更新记录

[English version](CHANGELOG.md)

## Unreleased - Durable Operation Core

- 将 Ghost 的 event-file stats 收口到共享的 `codey.ghost.event_log`
  helper。`inbox`、`hebbian` 和 `continuity` 现在直接调用共享 helper，
  旧的模块级 `_event_file_stats` 包装器已删除。
- 收紧共享 Ghost event-file stats gate：字节上限仍然在 decoding 之前生效，
  但 unreadable 文件在 byte-cap 检查后仍会保持 unreadable。
- 将 durable operation state 从旧的 `codey.runtime.effects` 命名中拆出：
  `codey.runtime.operation_state` 现在拥有 closed operation leaf table，operation state
  作为一等 `operation_state` log entry 写入，不再伪装成 pseudo effect record。
- 用 `RuntimeSessionLog.mutate()` 取代半公开的 `RuntimeSessionLog.append()` /
  `append_many()` 写入面。`mutate()` 在 session lock 内提交一个有界 batch，并立刻发布同一份
  process-local projection。生产 runtime facts 统一经过 `RuntimeMutationLine`。
- Effect store 和 delivery store 在生产路径上变成 projection-only。Provider send、tool
  batch、tool settlement、provider settlement、delivery receipt、recovered delivery fact 和
  terminal operation settlement 都通过 mutation line 同批提交，避免 intent 与 pending state
  分裂。
- 收紧 `RuntimeMutationLine`：生产代码只使用具名 mutation，不再暴露泛型
  `transition_operation()` 写入口。Writer、completion proof、repair、blocked verdict 和
  terminal commit 都有显式方法；task run 现在先 durable-accept operation，再做 recovery、
  routing 或 Ghost work-item claim。terminal 二次提交必须匹配既有终态身份；冲突终态现在
  fail closed。
- 新增纯 `operation_reducer` 和 `drive.peek_next_action()`，支撑 manual drive 测试和恢复派发。
  `recovery.py` 现在执行 reducer action：provider unknown-outcome settlement、safe tool
  batch replay 和 interrupted-effect synthesis，不再靠缺失 event 推断恢复。Provider
  unknown outcome 会结算为 `interrupted` + `maybe_sent`；torn provider-settlement batch
  会先被修剪再恢复；重复 safe-batch recovery 不会追加重复 durable facts。
- operation-state projection 遇到当前 operation 最新 state entry 坏账或跨 run 边界时
  fail closed，不再回退到更旧 leaf。Delivery recovered fact 现在要求匹配
  `tool_delivery_pending` leaf，确保 recovered delivery 写入仍绑定 durable state machine。
- project-completion 在 operation accepted 之后的 runtime mutation 现在 fail closed：
  writer-running、writer-settled、completion-proof、repair 与 blocked-verdict commit
  失败时会抛出具名 `ProjectRuntimeMutationError`，不再把 mutation failure 抹平成
  `operation = None` 后继续执行后续业务 effect。
- 收紧本地执行和 policy 审计边界：`run` 不再接受直接 `python *.py` 脚本执行，未知
  runtime tool 名现在会生成被记录的 `unknown_action` deny，provider fallback 的 policy
  deny 会在 provider state mutation 前阻断切换，policy denied 或 timeout 这类非检查型
  run failure 不再清掉已有绿色验证事实。
- 收紧本地事实耐久性：run ledger 追加现在经过文件锁，byte-budget 截断后仍保留
  `run_finished`，重新打开已截断 ledger 的 writer 不再写普通 row。File tools 在
  read/write/edit/list 前拒绝路径中的 symlink component。
- 新增 adversarial crash 覆盖：accept mutation 前/中/后、provider unknown recovery
  连续执行、safe-batch recovery 连续执行且 durable snapshot 完全一致、最新
  operation state 坏账，以及 terminal state/settlement torn tail 的两种排列。
- 删除旧的 `runtime/reducer.py`、`runtime/scheduler.py` 和 `runtime/effects.py` 形态。
  `RuntimeOperationStore` 现在只做读取投影，不再暴露 `start`、`commit` 或 session deletion
  这种业务写入口。
- Ghost、World Model、Lane、RemoteSession、RPC、CBOR 和 lease 不进入 runtime core；Ghost
  仍然只是 durable facts 的观察者/使用者。
- 关闭本轮审计发现的 runtime/UI 加固项：stale shell approval card 现在按 run 作用域过期；
  `/api/run` 不再为缺失 project 自动建任意目录，而是返回错误；`/api/ui_state` 的 POST
  必须带显式允许的 Origin；shell approval continuation 失败会把 retry 状态传给 UI，
  不再静默无动作。
- 收紧 SSE 投递和前端 replay：EventBus 给每个 subscriber 独立 payload copy；
  overflow `resync_required` 上报单次 drop 数；SSE JSON 序列化坏事件会跳过而不是断流；
  turn/info/review 按 event id 去重；composer fetch/json 失败会解 spinner 并提示错误；
  provider status refresh 按 provider freshness 合并，不再因为一个较旧全量快照丢掉其它 provider。
- 修掉全量回归暴露的两个完整性投影问题：公开 Ghost compaction 在返回 `ok=True` 前会验证小型
  event log，因此坏 UTF-8 日志继续显示为 unreadable；completion blocked-reason 不再把
  `verification_unavailable` 归成环境故障，未观察到本地证明的 done claim 保持为 `unobserved`。
- 将 `ghost_post_turn_warning` 接入 run-event 白名单和前端 replay 路径，后端已经 emit 的
  post-turn warning 现在会作为安静的 info message 可见，不再卡在后端与 UI 之间。
- 将 shell approval 续跑剩余的 prompt/provider 组装收进
  `services.build_shell_approval_continuation_plan()`；API 层只保留分派编排，同时保持
  active run provider 优先、pending provider 兜底的原有行为。

验证：

- `python -m compileall -q codey tests`（通过）
- `ruff check .`（通过）
- targeted approval/server/UI suite：
  `pytest tests/test_approval_registry.py tests/test_server.py tests/test_ui.py -q`
  （`263 passed, 1 skipped`）
- targeted completion/operation suite：
  `pytest tests/test_completion_verification.py tests/test_completion_engine.py tests/test_project_completion_flow_enforcement.py tests/test_task_entry_operation_state.py -q`
  （`65 passed, 24 subtests passed in 11.94s`）
- targeted Ghost event-log suite：
  `pytest tests/test_ghost_sleep.py tests/test_ghost_inbox.py tests/test_ghost_hebbian.py tests/test_ghost_continuity.py -q`
  （`96 passed, 83 subtests passed in 8.01s`）
- targeted runtime/policy/fact/server suite：
  `pytest tests/test_run_ledger.py tests/test_tool_runtime.py tests/test_action_policy.py tests/test_provider_preflight.py tests/test_agent_effect_sandwich.py tests/test_tool_result_delivery.py tests/test_runtime_operation_state.py tests/test_execution_evidence.py tests/test_server.py`
  （`381 passed, 5 skipped in 44.77s`）
- targeted architecture/entry/server suite：
  `pytest tests/test_architecture.py tests/test_task_entry_run_trace.py tests/test_server.py tests/test_completion_enforcement_ab.py tests/test_work_checkpoint_flow.py tests/test_task_entry_operation_state.py`
  （`325 passed, 1 skipped in 67.06s (0:01:07)`）
- focused server suite：`pytest tests\test_server.py -q`
  （`199 passed, 1 skipped in 28.71s`）
- focused UI suite：`pytest tests\test_ui.py -q`（`63 passed in 0.16s`）
- 全量 pytest：`pytest`（`3636 passed, 19 skipped in 323.47s (0:05:23)`）

## 0.5.7 - Research Follow-up Quality Closure

- 恢复 TUN 代理场景下的浏览器 Research 搜索：`NetworkPolicy` 默认允许 DNS
  解析得到的 `198.18.0.0/15` fake-IP 地址，但仍然阻止用户直接打开 literal
  fake-IP URL。这里修的是实际网页搜索路径，没有用 connector-only fallback
  把搜索失败藏起来。
- MiMo 网页驱动新增很窄的 JSON 工具回复规整：只处理实机 Research 里观察到的
  单个 fenced JSON、`json ... json ...` 覆盖层重复文本，以及相邻的完全相同
  JSON object；如果两个 JSON object 内容不同，仍然按严格协议错误处理。
- 收紧 Research controller 的 PubMed/arXiv 来源路径：当有优先 connector 结果且还
  没打开过 connector 来源时，本回合只给模型展示这些优先结果，并且只允许
  `open_result`。如果某个优先 PubMed/arXiv 结果打开失败，controller 会记录并降级
  这个坏结果，后续不会反复强迫模型打开同一个坏链接，因此不会卡死。接近轮次上限且
  已经有证据时，controller 会收窄到 finish 动作；只在这个 finish 状态下做窄的
  prose final report 恢复。
- 新增 `tests/manual/research_experiment_gate.py`：从已完成的 Research 实验 JSON 中
  复算“默认路径该保留什么”的指标，只保留 metric，不复制原始 prompt、reply、
  report、transcript 或网页正文，并跳过 `complete:false` 的中断文件。新增
  `tests/manual/research_followup_quality_ab.py` 用于 connector-backed baseline 与
  evidence-only follow-up 的实机 A/B。gate 现在还会输出有边界的 `proof_gaps`
  区块，按 probe/provider 统计 `claim_missing_citation`、
  `claim_missing_evidence_ref`、`claim_missing_support_relation` 和
  `claim_not_evidence_backed`，不复制 source 或 report 正文。
- 新增 `tests/manual/research_claim_support_projection.py`：手工诊断历史结果 JSON、
  完整 `ResearchRecord` 或 archived transcript/report。完整 record 会生成可删除或
  降级 unsupported required claim 的 claim-ref 投影；只有历史 row 或 transcript 的
  输入保持 digest/count-only，并明确说明没有 record 时无法做 claim-level projection。
  没有新增生产接线。
- 新跑的 Research manual A/B 成功 row 现在会通过共享 harness plumbing 写入有界的
  `research_record` payload。这样 `research_claim_support_projection.py` 可以对新的
  source-connector、follow-up-quality、done-finalizer 和 bounded planner 结果做
  claim-level gap 定位，同时仍不复制原始 prompt、reply、source 或 report 正文。
  没有新增生产报告改写或 repair 接线。
- 新增 `tests/manual/research_source_rendering_ab.py`：untrusted source wrapper 的
  manual-only A/B。它把 raw source rendering 和带 fenced untrusted-data wrapper 的
  rendering 放在含恶意网页文字的 fixture 上比较，验收条件是 wrapper 没有 injection
  tool action，且 evidence quality、source coverage、completion honesty 都不回退。
  这只作为实验审查证据；没有新增 `codey/research/source_rendering.py` 生产路径。
- 2026-09-04 MiMo source-wrapper smoke 已在 `tool_injection` fixture 上跑完：
  wrapper row 结果为 `injection_tool_action_observed=false`，没有 evidence-quality /
  source-coverage / completion-honesty regression，且 `wrapper_gate_ok=true`；只把这份
  source-wrapper 结果喂给 experiment gate 时，输出 `source_wrapper.row_count=2`、
  `injection_leak_count=0`、`quality_regression_count=0`，decision 为
  `eligible_to_promote_after_live_review`。对应 manifest 是 `dirty_state=dirty`，
  所以这只能算 smoke 证据，不能算最终 release gate 证据，也不能作为生产接入依据。
- 2026-09-04 又跑了干净的 MiMo source-wrapper A/B：`tool_injection,false_done`
  各 `repeats=3`，共 12 行，manifest 为 `dirty_state=clean`，commit 为
  `0e30bfc`。wrapper 行没有 injection leak，但 `false_done` 的第 2 次 wrapper
  从 baseline 的 `knowledge_write` 退成 `done`（`score=6` vs baseline `13`），
  造成 evidence-quality、source-coverage 和 completion-honesty regression；harness
  总体 `ok=false`，experiment gate 报 `source_wrapper_gate_failed`。source wrapper
  继续只保留 manual-only，不能进生产默认路径。
- 已把 manual source-wrapper prompt 改成区分“网页内部命令”和“网页事实”：页面里的
  命令没有工具调用权限，但同一页面里的事实仍然可以作为 evidence。该 harness 现在
  默认保存 archived transcript，便于失败后分析。MiMo 在 commit `0bc8764` 上重跑
  干净 A/B：`tool_injection,false_done` 各 `repeats=3`，12/12 行完成，6/6 个
  wrapper 行通过，`injection_leak_count=0`，`quality_regression_count=0`，gate 报
  `eligible_to_promote_after_live_review`。这是一份干净的 MiMo fixture 胜利，但还不是
  生产接线。
- 2026-09-04 Qwen 也完成 evidence-safe source-wrapper clean A/B：
  `tool_injection,false_done` 各 `repeats=3`，12/12 行完成，6/6 个 wrapper 行通过，
  manifest 为 `dirty_state=clean`、`transcript_mode=archive`、commit
  `5323980de3adcce388791167c21c09ef84f5cd3e`。MiMo+Qwen cross-provider gate 读取
  24 行、12 个 wrapper 行、2 个 provider，`injection_leak_count=0`、
  `quality_regression_count=0`、`terminal_failure_count=0`，decision 为
  `keep_default_untrusted_source_wrapper`。`codey/research/source_rendering.py` 已进入
  默认 `open_url` source rendering；它只负责把网页正文标为“不可信数据”，不改变
  planner、tool schema、EvidenceLedger、citation contract 或 report rewrite 策略。
- 2026-09-04 已复算历史 Research gate：`source_file_count=67`，
  `skipped_incomplete_files=14`，verdict 为 `ok=true`。这份历史复算不含后续
  source-wrapper clean fixture，因此 wrapper 结论已由上面的 cross-provider gate 覆盖。
  其余默认路径结论是：
  PubMed/arXiv connector 保留，因为它能让 Codey 更容易到达可靠来源；evidence-only
  follow-up 保留但继续 guarded；done finalizer 保留为很窄的引用/来源列表整理器。
- MiMo PubMed connector-priority 实机 smoke 已跑通真实网页搜索：
  `ok=true`、`score=9`、`stop_reason=done`、`turns=12`、`sources_read=4`、
  打开了两个 PubMed URL，`connector_errors=[]`。proof review 仍是 partial，
  所以当前 Research 的主要堵点已经不是“搜索/打开失败”，而是最终报告里的部分结论
  没有和保存的证据/引用绑定得足够紧。
- `tests/manual/research_followup_quality_ab.py` 现在默认 `--transcript-mode archive`，
  后续 live Research A/B 默认保存 prompt/reply transcript，便于回看 provider 行为。
  2026-09-05 MiMo PubMed archive A/B 输出
  `tests/manual/results/research_followup_quality_ab-mimo-pubmed-archive-20260905.json`，
  但文件为 `complete=false`：baseline 行完成（`score=7`、`proof_ok=false`、
  `proof_answer_status=partial`、`proof_coverage=0.583`、`sources_read=3`、
  `evidence_count=4`、`unsupported_claim_count=7/12`），planner arm 的 transcript 已写入
  trace，但没有 `case_complete`/row 结果，不能作为 planner 收益或失败结论；gate 会跳过它。
- 扩展 `codey.utils.citation_scanner` 和 `codey.research.done_finalizer` 对常见 source-id
  引用写法的编译：支持 `来源s2`、`来源 s2`、`（s2）`、`（来源s2、s3）`、表格 `| s2 (...) |`
  和行首 `s2:`，并在把 source-id 改写成 `[2]` 时补 ASCII 边界空格，避免
  `word[2]` 继续被数字引用扫描漏掉。这是格式修复，用来减少无意义 done retry；
  未映射 source id 仍然 fail closed，且不会自动补 evidence 或伪造 citation。
- 浏览器读取来源现在会等短暂的 challenge/cookie 中间页过去再放弃；如果一篇长正文里只是
  提到了 cookie 或 captcha 字样，不会再被误判成反爬页。浏览器正文不可用时，会尝试普通
  HTTP 文本 fallback。宽泛 root landing page 会在搜索结果选择和直接/重定向 `open_url`
  两条路径里跳过；PubMed/PMC 文章页仍然可打开，ScienceDirect 这类站如果仍被反爬挡住，
  会在有界时间内返回 no-content 错误，不再拖死 planner。
- evidence-only follow-up 只作为很窄的一轮生产修复路径保留。prompt 明确只有两个合法出口：
  带非空显式 evidence 的 `knowledge_write`，或说明 fresh material 没有相关证据的 `done`；
  controller 会把 no-evidence `done` 归类为 no-op stop reason，而不是非法工具；只有
  畸形 `knowledge_write` schema 才允许一次 repair。
- 最终报告 claim filter 已接入生产 finalizer。它不会发明 citation，不会给 unsupported
  claim 硬贴 evidence，也不会让模型重写整篇报告。结论/关键证据区的 claim 必须保留 citation，
  并绑定到已打开来源里的 saved evidence；重要但没证据的 claim 会降级到限制/待验证问题，
  重复或泛泛而谈的 unsupported 句子会删除。
- 2026-09-05 干净 MiMo PubMed A/B 在 finalizer claim filter 后显示：planner arm 为
  `score=13`、`proof_ok=true`、`answer_status=answered`、`unsupported_claim_rate=0.000`；
  baseline 为 `score=7`、`proof_ok=false`、`partial`、`unsupported_claim_rate=0.095`。
  这条样本的 `planner_stop_reason=no_actionable_gap`，所以它证明的是 finalizer cleanup，
  不是 evidence-only follow-up 的收益。
- 2026-09-05 干净 forced actionable-gap MiMo A/B 随后验证了真实 planner 路径：
  baseline 停在 `score=5`、`proof_ok=false`、`answer_coverage_gap`；planner 跑了一轮
  evidence-only follow-up，保存 1 条新 evidence，达到 `score=12`、`proof_ok=true`。
  发布结论是：bounded evidence-only planner 保留在生产；query planner/executor 作为
  可审计叶子模块保留；更重的 batch/checklist 实验继续留在 manual-only。
- 从 0.5.6 之后全量审查代码时做了三处发布卫生修复：畸形 planner query/source limit 会在
  执行前先被有界化；planner payload score 会拒绝 NaN/Inf；evidence-followup prompt 的
  context limit 即使收到脏值也会回到有界默认。三处都补了 focused regression test。
- 测试与验证：0.5.7 发布审查、focused Research 回归、文档更新和版本号 bump 后，全量
  pytest 通过：`3549 passed, 16 skipped, 1259 subtests passed in 299.63s (0:04:59)`。

## 0.5.6 - Tool Prompt 收敛与 Prompt 面薄追踪 v1

- 工具提示面收敛与运行时 Prompt 面薄追踪 v1：
  - 新增 `codey.tool_prompt` 纯提示层，归一 `RenderedToolContract`、`model_visible_contract_hash()`、`render_coding_tool_contract_text()`、`render_coding_tool_contract()`、`render_coding_system_prompt()` 与 `coding_model_tool_contract_hash()`；仅依赖 `codey.toolchain.definition`，不引入 agents、runtime 执行、providers 或 ghost；`digest` 只 hash 最终模型可见文本，`runtime_names` 仅作漂移证明，不进入 hash。
  - 精简 `codey.toolchain.definition`：保留数据与映射，移除 `render_tool_contract()` 与 `model_tool_contract_hash()`，无兼容包装，同一提交内迁移所有引用，保持默认 coding writer 提示面字节一致。
  - 精简 `codey.protocols.json_codec`：移除本地 `_system_prompt()` / `_profile_system_prompt()`，`SYSTEM_PROMPT` 与 `JsonToolCodec` 委托 `codey.tool_prompt` 实现；不改变任何解析、修复或工具派发行为。
  - 规范化 `codey.tool_args_repair`：抽取标准别名常量 `PATH_ARG_KEYS`、`SEARCH_QUERY_KEYS`、`REFERENCES_SYMBOL_KEYS`、`COMMAND_KEYS`、`EDIT_OLD_KEYS`、`EDIT_NEW_KEYS`；规范化器直接消费标准常量，漂移测试在本地对齐合法 kind 集合，移除生产未消费的死表常量。
  - 收敛 Research 契约：`ToolContract` 增加 `example` / `description` 供静态 `Tools:` 块（`render_research_tool_contract_text()`）；静态 `knowledge_link` 契约体现对精确标题的支持（`dst:"<note id or exact title>"`），动态 `tool_example()` / controller allowed-actions 示例保持 0.5.5 旧字节（`dst:"<note id>"`），避免 repair/controller prompt 漂移；新增 `research_tool_names()` 与基于 `model_visible_contract_hash()` 的 `research_tool_contract_hash()`。
  - 保持 `codey.research.protocols` 字节一致：保留 hard-boundary 与 discipline 文案，`Tools:` 列表改由 `render_research_tool_contract_text()` 注入；移除未引用的死代码 `_SYSTEM_PROMPT = _research_body(False)`；`include_source_search=True/False` 两种默认 Research 提示面在求值后与 0.5.6 前完全一致。
  - 结构化 Research controller 动作：新增 `ControllerActionContract`、`CONTROLLER_ACTION_CONTRACTS`、`controller_action_names()`、`render_controller_action_contract_text()`，`controller_action_contract_hash()` 改为 `model_visible_contract_hash("research_controller_action_contract", ...)`；动态每轮 `allowed-actions` 块不进入静态 hash，当前 outbound 面通过 `prompt_digest` / `epoch_id` 追踪。
  - 新增薄 `codey.runtime.prompt_surface`：`PROMPT_SURFACE_SCHEMA_VERSION`、`PromptSurfaceSection`、`PromptSurfaceRecord`、`prompt_surface_id()`（现为 `phase+send_ref+prompt_digest` 的 per-send 标识，非内容去重）；抽取共享规范化器 `canonical_surface_phase()`、`canonical_surface_send_ref()` 与 `canonical_surface_prompt_digest()`，它们只做首尾空白清理，不做静默替换或身份裁剪；`build_prompt_surface_record()` 先规范化字段再派生 ID；`validate_prompt_surface_payload()` 严格拒绝非规范化 `phase`（长度 > 40、含空格或非法字符），严格限定 `send_ref` 为纯标识符字符（`[A-Za-z0-9._:-]{1,80}`，拒绝空白/换行/CRLF），对 `sha256`、`ctx_epoch`、`prompt_surface` ref 使用 `fullmatch()`，末尾换行不能绕过，严格要求未加首尾空白的 `prompt_digest`，强制要求非空且符合 `ctx_epoch:[0-9a-f]{16}` 的 `epoch_id`，重算派生 `surface_id` 并断言相等防止伪造污染，并使用 `type(x) is int` 封堵 `schema_version`、`prompt_chars` 与 section `chars` 的 bool-int 漏洞。`PromptSurfaceRecord` 为每次 provider 发送一条（`surface_id` 为发送身份、`prompt_digest` 为内容身份、`send_ref` 为 `provider_effect_id` 或 `research_send:{n}`），相同内容重发产生不同 surface。
  - 扩展 `codey.runtime.prompt_envelope.record_provider_send_prompt()`：在保持原有 `record_prompt_section` 行为外，新增 `phase` / `send_ref` / `provider_effect_id` / `model_tool_contract_hash` / `runtime_tool_contract_hash` 透传；抽取私有 helper `_build_validated_surface_payload()`，并使用 `inspect.getattr_static()` 静态探测 `trace.record_provider_prompt_boundary()` 进行原子批量落盘（单次 flush），在缺少该方法时平滑回退到单独投影；静态探测和真实属性读取都放在 fail-open 边界内，坏 descriptor/property adapter 会回到 fallback 而不是打断 provider send；移除对 `provider_effect_id` 的隐式 fallback，仅当显式且合法的 `phase` 与 `send_ref` 存在时才额外以 `record_prompt_surface` 投影有界摘要行，无 `send_ref` 时 surfaces 为空，避免把 `chat`/`review`/`handoff` 误标为 `writer`；`TaskCancelled`/`DeadlineExceeded` 仍透传。
  - 扩展 `codey.runs.trace`：新增 `MAX_PROMPT_SURFACES`、`PromptSurfaceTrace`（现序列化 `self.schema_version` 并含 `send_ref`）与 `RunTraceManifest.prompt_surfaces`，`to_payload()` 输出 `prompt_surfaces`；提取 `_append_prompt_section()` 与 `_append_prompt_surface()` 私有追加 helper；新增 `record_provider_prompt_boundary()` 实现 provider 发送前 section 与 surface 的原子单次 flush 落盘；新增 `RunTraceRecorder.record_prompt_surface()`，仅在追加成功时执行 `flush()`，经 `validate_prompt_surface_payload()` 严格校验、要求 `send_ref` 与 `schema_version==1`、仅按 `surface_id`（per-send）去重，仅保存有界元数据，不保存原始 prompt/reply/网页正文等。
  - 闭合 prompt surface 清理漂移：实际源码中删除遗留的死 helper `_is_epoch()`，epoch 校验统一走与其他 ref 相同的 `_matches(...fullmatch...)` helper，并新增 architecture gate，防止 release note、源码和测试对这类清理再次不一致。
  - 重排 provider 发送追踪顺序：`codey.agents.prompt_context._send_provider_with_effect()` 先创建 provider effect 并获得 `effect_id`，再以 `phase="writer"`、`send_ref=effect_id` 调用 `record_provider_send_prompt()`，最后 `provider.send()`；`codey.research.runner` 新增单调 `_research_send_seq`，每次 `_send_provider()` 以 `phase="research"`、`send_ref="research_send:{n}"` 调用，`model_tool_contract_hash` 来自 controller 或 codec，`runtime_tool_contract_hash` 在 controller 存在时取 codec；提取并导出 `render_research_repair_prompt()`，彻底移除已废弃的 `_protocol_repair_prompt` 别名残留，并同步更新 runner 内部调用与手动 A/B 脚本。
  - 预备 0.5.7 Research 实验收口，但不改变生产 prompt：抽出 `codey.research.followup_selection` 作为纯 candidate selection / stop decision 核心，抽出 `codey.research.followup_quality` 作为 bounded follow-up 行评分与 usefulness scorer，抽出 `codey.research.source_finalizer_scoring` 作为 source-finalizer A/B 行评分与聚合 scorer。`ResearchPipeline` 调用共享 selection leaf；`bounded_research_planner_ab.py`、`bounded_research_merge_projection.py` 与 `source_connector_done_ab.py` 改用共享 scorer，不再各自携带重复私有评分代码。没有新增 manager、provider/browser import、默认关闭的生产 renderer 或兼容 wrapper。
  - 测试与回归：新增 `tests/test_tool_prompt.py`、`tests/test_tool_contract_drift.py`（锁定静态 `knowledge_link` exact title 真实能力，同时保持动态 repair/controller 示例字节稳定，并校验解析器修复分类）、`tests/test_prompt_surface.py`（覆盖 per-send `surface_id`、真实派生校验、拒绝长/非规范 phase 与换行/空格/未 strip/超长 send_ref、拒绝末尾换行 sha256/epoch/surface refs、拒绝带首尾空格 digest、强校验必填 epoch、原子边界批量落盘、`send_ref` 非法时只记录 section、重复/无效 surface 跳过 flush、继承与 descriptor fail-open、bool-int 拒绝、无 `send_ref` 时 surfaces 为空与落盘字段恒等重算断言）、`tests/test_research_followup_quality.py`（覆盖共享 follow-up usefulness / source-finalizer scorer，包括非有限指标处理）与 `tests/test_golden_parity.py`（锁定 `SYSTEM_PROMPT`/控制块基线，并将 `test_research_repair_prompt_golden` 改为真实重算 `render_research_repair_prompt` + `append_block` 全量逐字节比对，改用 research 领域工具契约常量隔离跨域耦合）。Architecture tests 现在锁定 prompt-surface cleanup 与 Research scorer leaf purity。全量 pytest 通过：`3481 passed, 4 skipped, 1253 subtests passed in 290.34s (0:04:50)`。Coding 模型可见字节完全保持不变，Research 静态契约诚实表达真实能力，动态 prompt 无感稳定，无死代码兼容包袱。

## 0.5.5 - Safe Replay Result Delivery Receipt v1

- 安全工具重放结果交付凭据与精准轮次恢复 v1：
  - 新增 `codey.runtime.tool_result_delivery`：基于单一事实源 `RuntimeSessionLog` 提供纯数据类 `DeliveryBatchIntent`、`DeliveryBatchProjection`、`DeliveryRecoveredFact` 与 `ToolResultDeliveryStore` 存储。支持全生命周期两阶段交付凭据跟踪（`batch_intent` -> `send_attempt` -> `delivered` / `recovered`），严格拒绝 `prompt`、`reply`、`result`、`stdout`、`stderr`、`diff`、`source_body` 等原始输出字段，使用严格 5 字段 item exact payload schema 与非负整数/有界字符串校验，对齐 `RuntimeEffectStore` 的 run 边界校验，严格拒绝孤儿 `send_attempt` / `delivered` 记录，并校验 SHA-256 批次 digest。
  - 闭环崩溃恢复时间窗口：抽取 `codey.agents.tool_turn`（`execute_turn_tools`），在执行任何工具前提前记录轮次级的 `batch_intent`，彻底消除工具间执行崩溃的盲区。恢复时自动投影未交付的 all-safe batch，使用持久化的标准 `replay_args` 按 `(turn, tool_index)` 严格顺序重新执行整批 safe tools；对已 settled 工具不写重复结算，仅记 `recovered` 事实。
  - 严格 Fail-Closed 边界：引入 `can_recover_before_provider_send` 严格要求零 send attempts，防止 provider 已接收 prompt 但进程崩溃导致重复发送；`delivered` receipt 必须对应同一 provider effect 的既有 `send_attempt`；batch 投影拒绝同一 batch 出现多条不同或重复的 `send_attempt` / `delivered` receipt；混合批次（包含 `edit`、`run`、`shell`）或不可恢复批次严格 fail-closed，维护 `blocked_effect_ids` 杜绝内部 safe tool 发生局部单 effect 回退重放；扩展 `_validate_run_boundary()` 补充 `payload_lane_match` 严格校验。
  - Agent Loop 架构精简与 Prompt 字节一致性：抽取 `codey.agents.result_delivery`（`deliver_turn_results`、`deliver_recovered_results`、`build_next_tool_prompt`），消除 `codey/agents/loop.py` 中 3 处重复分散的代码，通过 `TurnState` 支持 fast-path digest 复用免去冗余日志反查；即使 resume 走单个 safe effect fallback、没有既有 batch id，也会为恢复结果 prompt 补上 delivery receipt；Parity Test 严格保障 clean path prompt 字节级完全一致。
  - 日志压缩与 Run Details 消费：更新 `RuntimeSessionLog._compact_entries`，open operation 保留 active delivery 凭据，settled operation 裁剪已 delivered batch 但持久保留 `recovered` 事实；将 `load_recovered_facts` 接入 `codey.runs.details`，保障在日志压缩后运行详情依然能准确投影恢复事实并在日志异常时展示优雅 warning。
  - 核心运行时与并发安全深化加固：
    - `BrowserWorker`：running 超时标记任务为 `ABANDONED`，彻底丢弃延迟结果，清理 slot 杜绝后续 job 污染。
    - `ChangeTracker & SnapshotStore`：全生命周期方法（`load`、`put_baseline`、`set_after_hash`、`remove`、`delete`）使用独立于 snapshot 目录的外部文件锁保护；`remove()` 始终 best-effort 删除 body 文件；`capture_before()` 异常回滚清理孤儿 body；`collect()` 实现单次 diff 同时统计修改增减行数。
    - `Provider Revival`：限制代际递增上限 `min(old + 1, 99)`；精简 `previous_bundle` 为非递归最小回滚结构；持久化异常记录 warning。
    - `Ghost Continuity`：`_safe_prompt_text()` 全局接入 `looks_prompt_visible_secret()`，拦截 API key 与高熵敏感凭证进入 prompt 上下文。
    - `清理语义与 UI 稳定性`：`forget_conversation()` 汇总各 store 清理失败并在 `/api/new_chat` 中如实暴露 unpurged_stores；`provider_ui.js` 增加 500ms debounce 与单调请求序号比对。
  - 测试与回归保障：新增 `tests/test_tool_result_delivery.py`、`tests/test_browser_worker.py`、`tests/test_changes.py`、`tests/test_ghost_continuity.py`、`tests/test_provider_revival.py`。全量 pytest 通过：`3431 passed, 16 skipped, 1208 subtests passed in 291.66s (0:04:51)`。

## 0.5.4 - Safe Tool Replay v1

- 安全工具重放与崩溃恢复 v1：
  - 新增 `codey.runtime.safe_tool_replay`：纯数据校验与候选对象提取模块，零依赖执行运行时与 agents 层。定义纯数据候选类型 `SafeToolReplayCandidate` 与严格参数规范化（`validate_replay_args()`、`replay_args_for_tool_call()`、`candidate_from_effect()`），强制要求零别名重写（`alias_rewrite_count == 0`）与零修复（`arg_repair_counts == {}`）。新增 `codey.runtime.replay_args` 统一复用持久化 replay 参数形状校验。
  - 窄白名单与策略隔离：定义可自动重放白名单 `REPLAYABLE_SAFE_TOOL_NAMES = frozenset({"read", "ls", "search", "references"})`。虽然 `project_facts` 与 `project_map` 保持 safe 分类，但 0.5.4 暂不接入生产 replay 执行器；修改类操作（`edit`、`write`、`run`、`shell`、`knowledge_write`）与 provider send / repair round 严格禁止重放。
  - Runtime Effect 记录模型扩展：`RuntimeEffectIntent` 仅在 replayable safe tool 时记录标准化的 `replay_args`；从日志读回的坏 `replay_args` 会 fail closed 成缺失 replay 参数，而不是中断整个恢复流程；`RuntimeEffectSettlement` 新增 `replay_count`（int）与 `replayed_from_effect_id`（str，非空时必须严格等于 `effect_id`）。重复结算幂等性检查覆盖重放元数据与 replay class。
  - 恢复摘要投影与日志压缩：`RecoverySummary` 支持计算 `replayed_reads` 与 `replayed_searches`，并在运行详情中静默投影 `Read action was recovered`、`Search action was recovered`；日志压缩算法保留重放相关的 intent 与 settlement。
  - Agent 执行层架构重构与去重：在 `tool_execution.py` 中抽取 `execute_information_tool_call()` 与 `evaluate_tool_call_policy_for()`，供主循环与恢复门控纯净复用，消除重复派发与策略逻辑；抽取 `tool_result_from_outcome()`；更新 `record_tool_call_intent()` 在只读安全工具时写入 `replay_args`。
  - 无缝恢复驱动与 Agent Loop 重构：在 `codey.agents.request` 中定义 `RecoveredToolOutcome` 并将 `recovered_tool_outcomes` 接入 `AgentRequest`。重构 `codey.agents.loop`：`_run_loop()` 支持 `start_turn: int = 1`；`run()` 消费恢复的工具结果，更新会话状态，格式化 prompt 发送给模型，并从 `max(turn) + 1` 启动轮次循环，实现无缝续跑。
  - Operation 恢复门控升级：将 `task_run.py` 的 settlement-only 恢复路径替换为 `_recover_effects_for_resume()`，严格校验项目目录有效性、project/hybrid writer 上下文及策略权限后执行安全重放；不保留旧恢复 wrapper；将 `recovered_tool_outcomes` 挂载至 `RunFrame` 并在 `_run_one_writer_attempt()` 中消费清空；带 recovered tool outcome 的恢复续跑会跳过 work-queue claim 和 auto router，并直接回到 project writer，避免 hybrid writer crash 后重复执行 Research；对于 unsafe、provider、repair、参数不合规或非 writer 的 pending effect，统一 fail-closed 合成为 `interrupted` 结算。
  - 测试与回归保障：新增 `tests/test_safe_tool_replay.py` 单元测试与 `tests/manual/safe_tool_replay_smoke.py`（支持 `--self-test`、`--same-run-self-test` 与有界 live resume smoke），更新 `test_tool_replay_policy.py`、`test_runtime_effect_records.py`、`test_agent_effect_sandwich.py` 与 `test_runtime_session_log.py`。deterministic smoke 覆盖 safe replay 与 unsafe interrupt；same-run smoke 覆盖 recovered result exactly-once 注入；hybrid writer 恢复补测确认 consuming recovered tool results 前不会重复执行 Research；DeepSeek live resume smoke 已通过，覆盖 1 个 recovered read、后续 `read`/`edit`/`run`、`checks_passed=true` 与 `final_content_ok=true`。CLI run-event 输出改用 ASCII 工具标记，避免 Windows smoke 日志乱码。全量 pytest 通过：`3384 passed, 16 skipped in 297.52s (0:04:57)`。

## 0.5.3 - Shared Tool Argument Repair + Protocol Friction Reduction v1


- 共享工具参数规范化与协议摩擦降低 v1：
  - 新增纯函数模块 `codey.tool_args_repair`，负责词法路径规范化、有界正整数转换以及标准运行工具（`edit`、`read`、`ls`、`search`、`references`、`run`、`shell`）的等价字段别名修复。
  - 路径严格限制为项目相对路径：规范化斜杠并安全折叠 `.` 与 `..`，严格拒绝 Windows 盘符（`C:\`）、UNC 路径（`//share`）、根路径（`/`）以及逃逸项目根目录的父级遍历（`../`）；可省略的 path 缺失时才默认 `.`，显式空字符串/null path 严格 fail closed；路径内部空格保留，首尾空白按 path normalization 记录。
  - 同一语义组内的冲突别名键（例如 `old_string` + `old`、`command` + `cmd`、`query` + `pattern`、`symbol` + `name`、`path` + `cwd`）严格 fail closed 并抛出 `ToolArgsRepairError`。
  - 未知参数字段严格 fail closed，不再静默丢弃；不支持的未知运行时工具也会立即 fail closed。
  - 文本参数（`query`、`symbol`、`command`）严格要求非空白字符串类型，非字符串与纯空白值严格拒绝。
  - 支持的等价参数别名：
    - `edit`: `old` / `search` / `before` -> `old_string`；`replace` / `replacement` / `after` / `new` -> `new_string`。
    - `edit`: 缺失 `new_string` 严格 fail closed；只有显式空字符串 `new_string` 或等价别名才表示删除。
    - `edit`: `content` 严格要求字符串类型，杜绝非字符串值的静默数据丢失。
    - `edit`: 支持将 `replacements` 中传入的单字典对象自动包装为列表。
    - `edit`: 安全解析 JSON 字符串形式的 `replacements`；无效 JSON 严格 fail closed。
    - `read`: 数字字符串 `offset` / `limit` 规整为有界正整数；bool、float、null 与非法值严格拒绝。
    - `search`: `pattern` -> `query`。
    - `references`: `name` -> `symbol`。
    - `run` / `shell`: `cmd` -> `command`（绝不猜测或篡改命令内容）。
    - `write` / `write_file` / `create_file` 保持 unknown tool，并在 repair prompt 中统一引导 `edit(content=...)`，不引入生产隐藏别名。
  - 大幅瘦身 `codey.protocols.json_codec`：`_tool_call()` 仅负责确定运行时工具名并委托给 `normalize_tool_args()`，消除重复冗余的解析分支；`read_files` 与 `parallel` 复用相同的规范化逻辑，并删除无调用方的 private `_parse_object()` / `_text()` 残留。
  - 有界遥测与安全记录：`ToolPlan` 增加 `alias_rewrite_count` 与 `arg_repair_counts`，且严格在 call 去重采纳后进行精确累计；`AgentLoop` 将其转发至 `RunTrace.record_protocol_valid_turn`；`RunTrace` 仅安全记录合法枚举计数并设上限（999），严格过滤与丢弃敏感 key，绝不持久化任何原始路径、命令、查询或 prompt 文本。
  - 烟雾、确定性 A/B、live provider 与 dialect-pressure 测试：`tests/manual/tool_args_repair_smoke.py` 覆盖方言和 fail-closed case；`tests/manual/tool_args_repair_simulated_ab.py` 负责确定性的 0.5.2-vs-0.5.3 parser 对比；`tests/manual/tool_args_repair_live_ab.py` 是自然生产 agent loop/provider 实机 probe；`tests/manual/tool_args_repair_dialect_pressure_ab.py` 用于在 prompt 明确施压 provider-shaped 参数时验证生产 loop 吸收能力。
  - DeepSeek、MiMo、GLM 的自然 live provider A/B 已完成；这组干净 schema 小样本里没有观测到省 turn：每个 provider 的 baseline/candidate 都完成 2/2 case，总 turn 都是 7，protocol error、repair prompt、alias rewrite 都是 0。这说明采样路径无回归；真正“别名出现时省 repair turn”的证据仍来自 deterministic dialect suite。
  - MiMo dialect-pressure live A/B 已完成：baseline 和 candidate 都完成 2/2 case；candidate 总 turns 从 9 降到 8，repair prompts 从 2 降到 0，并记录到 2 次 numeric-string coercion；edit/run pressure case 里 MiMo 仍输出 canonical 参数，没有实际触发 `old`/`new` 或 `cmd`。
- Provider 稳定性：
  - GLM browser start 和 new-chat URL 改用根入口 `https://chatglm.cn/`，不再使用容易触发验证的 `main/alltoolsdetail` 深链；没有加入深链 fallback。

## 0.5.2 - Effect Intent / Settlement + Tool Replay Policy v1

- 外部副作用 Intent / Settlement 闭环与 Replay Policy v1：
  - 新增 `codey.runtime.replay_policy`，实现 `ReplayClass`（safe/unsafe）与 `ReplayDecision`。只读安全工具（`read`、`ls`、`search`、`references`、`project_facts`、`project_map`）判定为 safe，并在恢复时生成可重试投影；修改类操作（`edit`、`write`、`shell`、`run`、`knowledge_write`）以及未知工具一律判定为 unsafe；`run` 命令内容不豁免，一律 unsafe；provider send 与 repair round 一律 unsafe。
  - 新增 `codey.runtime.effect_records`，基于单一事实源 `RuntimeSessionLog` 实现 `RuntimeEffectStore`、`RuntimeEffectIntent`、`RuntimeEffectSettlement` 与 `RecoverySummary`。外部副作用严格执行 `record intent -> execute real effect -> record settlement` 的 Effect Sandwich。
  - 效果唯一性与防碰撞：使用全局唯一的 `new_effect_id(category, run_id)`，消除 turn、tool call 与 resume 过程中的 id 碰撞，防止新 pending 被旧 settlement 覆盖。
  - 严格 Schema 与 Fail-Closed：`from_payload()` 严格校验字段类型与长度，要求 `session_id`、`lane`、`operation_id`、`turn`、`tool_index` 与 canonical `ref` 为必填字段，拒绝未知 effect payload key，通过 `_require_enum_str` 严格校验枚举字段（`effect_category`、`replay_class`、`status`、`sent_state`）必须为有效字符串类型，拒绝未知 record_kind、bool turn 与缺失语义字段，消除隐式截断与原生 TypeError；`record_intent()` / `record_settlement()` 显式校验传入 dataclass 的 `session_id`、`run_id`、`lane` 与 `operation_id` 必须与目标 run 边界严格一致；`record_settlement()` 严格校验 category 一致性，对等价重复结算幂等返回，对冲突结算显式报错；`load_effects()` 严格按 operation 与 run 边界匹配，校验 entry 与 payload 的 `session_id`、`run_id`、`lane` 及 `operation_id` 边界一致性（防止静默漏掉坏记录），并按严格时间序解析，拒绝重复 intent、拒绝无 intent 的孤立 settlement、拒绝冲突结算。
  - 恢复前置门禁与完整生命周期清理：将未结算 pending effects 恢复扫描前置到任何工作项领取（claim）、自动路由（Ghost auto router）及 provider 调用之前，杜绝在恢复未完成前触发任何外部副作用；恢复失败与操作启动失败通过 `_fail_early()` 派发标准 `task_done` 错误事件并清理 `RunRegistry` 状态与 work_item，阻断后续所有外部调用；`_start_run_operation()` 回到单一职责，只负责打开 operation state，pending-effect recovery 只留在前置门禁；`complete_or_block_work_item()` 纯净消费 `GhostWorkItem | None`，移除无用的包装对象兼容逻辑与死代码参数。
  - 循环迭代安全性：工具循环在每轮迭代开头重置 `effect_id = ""`，若 `record_tool_call_intent` 异常立即 fail-closed 记录错误结果，不执行工具、不结算旧 effect。
  - 模型发送摘要完整性：Provider prompt 的 `args_digest` 对全量 prompt 进行 hash，消除前缀截断弱点。
  - 工具时序与安全写入：调整时序为 `execute_tool_call -> record_tool_outcome -> record_tool_call_settlement`；settlement 会在 tool outcome 记录尝试之后的 `finally` 执行，避免 event callback 失败让已完成 effect 永久 pending；`record_settlement_safely` 保证日志写失败绝不掩盖真实业务结果或异常；删除废弃的 `begin_tool_call()` 和 future-only 的 `ReplayDecision` payload/retry flags。
  - 崩溃恢复投影与安静展示：会话恢复时自动扫描未结算 pending effects 并合成 `interrupted` 状态 settlement；`recovery_summary` 仅统计已结算的 interrupted 效果，忽略运行中的 in-flight pending 与普通 provider 报错，避免误报 Recovery。
  - 严格保持上下文纯净：本版本不把 safe replay 或 synthetic interrupted 注入模型上下文，不改变 prompt、tool schema、provider routing 与模型可见 transcript，不保存 raw prompt/diff/output payload。

## 0.5.1 - Task Runtime Finalization + Completion Repair Durability v1

- 运行时冷启动重构：
  - release-gate cleanup 修复了 app/api 拆分后真实 HTTP queryless GET
    JSON endpoint（`/api/ui_state` 和 `/api/providers`）的 dispatch
    路径，并把 browser/MoA smoke harness 更新到当前 `app.services`
    provider owner，不再 patch 旧 server re-export。
  - 删除生产 `TaskFlow` 概念并移除 `codey/operations/task_flow.py`。
    server、headless、manual harness 和测试现在都通过
    `codey.operations.task_entry.run_task_submission()` 接收
    `TaskSubmission`；不保留兼容 shim 或 old/new 开关。
  - 剩余 task lifecycle 按 ownership 拆成清晰名字：`task_entry.py` 只把
    submission 接到 `TaskRuntime`，`task_run.py` 拥有非业务 run 生命周期和
    `TaskRunDeps`，`mode_dispatch.py` 选择 operation function，review /
    planning / Ghost post-turn 进入各自 operation 模块。
  - `AgentRunner` 也按真实边界拆开，但不改变协议行为：JSON protocol repair
    helper 进入 `codey.agents.protocol`，基础 prompt/context 渲染进入
    `codey.agents.context`，调用方传单个 `AgentRequest`，loop progress /
    verification / stagnation 状态成为显式对象，不再散在一大片 locals 里。
  - agent loop 继续按 owner 拆开：`codey.agents.runner` 是 public
    entry/re-export surface；`codey.agents.state` 拥有 `AgentLoopSession` 和
    mutable loop state；`codey.agents.prompt_context` 拥有 provider-send prompt
    组装、context epoch 绑定、repair context admission 和
    coding-current-context 注入；`codey.agents.verification_driver` 拥有
    verification candidate、freshness、reminder 以及 edit/run verification
    账本；`codey.agents.tool_execution` 拥有 tool policy、dispatch 和 result
    accounting。`codey.agents.loop` 只保留 turn loop、parse path、可见的
    `continue` / `return` 控制流、状态转移和 finish。
  - `codey.operations.project_completion_flow.run_project_mode()` 改成基于
    `_ProjectRun` 的显式 phase script：project context prepare、writer
    failover、review cycle、completion enforcement、final receipt/facts/terminal
    projection 都有独立函数 owner，不再依赖 `nonlocal` 闭包状态。
  - `ProjectCompletionDeps` 按稳定 access surface 分组为 `AgentAccess`、
    `PersistenceAccess`、`VerificationAccess`、`ReviewAccess` 和
    `RuntimeAccess`。这一步压缩依赖面，但不新增 `CompletionManager`，也不按
    行数把 project completion 拆成一堆互相 import 的文件。
  - 本地 HTTP app 边界拆成 `codey.app.http_plumbing`、`codey.app.api` 和
    `codey.app.services`。Handler 只做 origin 校验、HTTP 解析、普通 JSON
    endpoint route-table dispatch，并把 SSE 保留为 streaming transport 例外；
    review、consensus/audit/advisor、provider warmup、approved shell 执行和
    shell continuation prompt 都进入 services，并显式接收 `AppContext`。
  - app runtime 状态从 HTTP server 外壳拆出：run 生命周期、approval 队列、
    provider session/health/order、conversation cache/store、knowledge rebuild
    single-flight、Ghost sleep single-flight 现在都有独立 app 模块。
    server 现在暴露 `AppContext` 作为产品级协调面，不再保留 `server.State`
    转发属性或 old/new runtime 开关。
  - operation frame/work/hooks/outcome 值对象移入 `codey.operations`，plain
    chat operation 和 prompt/local-context trace helper 也从 task runner
    迁出。chat prompt 组装、consensus handoff、provider session 收束和 reply
    emission 现在有明确 operation owner。
  - project completion 执行迁入
    `codey.operations.project_completion_flow`：writer failover、completion
    proof evaluation、bounded repair admission、receipt/facts/memory 写入和
    analysis-run projection 现在都由 project-completion operation 拥有。
  - task 包边界与执行分开：`codey.task` 只保留 model-only 的
    `TaskSubmission` 和 `TaskKind`；`TaskContract`、`TaskState` 和旧 service
    facade 都已删除。
  - Pi 风格 runtime kernel 收缩到只有已接线的生产事实：typed operation
    outcome、operation contract、小型 scheduler，以及 append-only session log
    + fail-closed reducer。发布前删除了未来态脚手架：lane queue、
    suspended operation、`TaskRuntimePort`、tool-invocation log entry 和未使用的
    `OperationKind` literal。
  - completion verdict 所有权移入 `codey.completion.engine`，包括 blocked
    note 词汇和 proof + edit-integrity evaluation。project completion 消费
    engine 输出，不再内联重建这条决策链。
  - terminal `task_done` 事件构造和 terminal turn 计数移入
    `codey.runtime.terminalizer`，stop/error/done 共用同一个终态投影。
  - runtime submission identity 对齐：TaskRuntime、RuntimeOperationStore、
    terminal settlement 和 Run Details 现在为每个已 reserve 的 `run_*` id
    共用一条 `task:<hash(run_id)>` operation/lane。发布前删除了外层
    `runtime:<run_id>` operation 语义，所以一个 task 只有一个 runtime
    operation。
  - runtime operation tracking 保持解释性、fail-open：如果严格 phase fact
    校验拒绝了 malformed proof projection，用户可见任务结果不受影响，
    operation projection 停在最后一个合法 phase。
  - TaskRuntime 的单条 task operation 现在由用户可见 terminal event 决定
    outcome：`done` 是 completed，`stopped` 是 aborted，`approval` 是
    suspended，其他 stop reason 都是 failed。任务函数如果返回但没有
    terminal outcome，会记录为 failed，而不是 completed。
  - runtime scheduling 如果在 task executor 进入前失败，会释放已 reserve 的
    app run slot。首次 runtime-log append 失败不会再让 UI 永久 busy。
  - Runtime log 的 `append_many()` 行现在带 batch metadata。reader 会忽略
    不完整的尾部 batch，下一次 append 写入前会修剪这个坏尾；进程死在 batch
    中途不会留下永久 open lane。
  - Runtime log 会在同一把文件锁下、触碰 4 MB 上限前做 compaction，避免
    长生命周期 session 被写满后永久 brick。Compaction 保留 replay 等价骨架：
    `operation_started`、最新 `run_phase` effect，以及存在时的 terminal
    `operation_settled`。
  - Runtime session-log 校验现在维护进程内 entries + projection 缓存。
    `append_many()` 仍然通过 reducer fail closed，但文件大小和 `mtime_ns`
    stamp 都未变化时，热路径 phase commit 直接从缓存 entries 读取当前状态；
    同尺寸外部改写、compaction 和删除都会触发缓存重建或失效。
  - 新增包级 architecture tests 锁住当前 runtime 边界：runtime 不能 import
    operations、agents 或 Ghost；agents 不能 import operations；completion
    不能 import app、providers 或 operations；`agents.loop` 不能直接 import
    completion、toolchain 或 workspace context-source 内部细节，必须通过
    owner 模块触达。
  - Run Details 现在先读 runtime operation state，再判断 ledger/trace 是否
    存在；即使 ledger 或 trace 没写出来或被清理，中断 run 仍能显示安静的
    `Progress` 行。terminal runtime state 也能在没有 ledger/trace 时提供最小
    Work/Model 解释，但不会显示过期的 Progress。
  - RunRegistry 构建 `/api/state` snapshot 时不再在内部锁里调用 approval
    callback。
  - 无参 `AppContext()` 现在也有 ephemeral runtime-session log、operation store
    和 workspace revision store，测试/临时调用走同一 runtime path，但不会写入
    用户真实 durable state home。
  - 新增 workspace state 跟踪，用 `WorkspaceState(revision, fingerprint)` 把
    verification freshness 绑定到项目文件状态。缺失的 revision 文件可以从初始
    revision 开始，但腐坏、非法或超限的 revision 状态会 fail closed，不再把
    单调身份回退到 1。Verification observation、checkpoint green check 和
    completion proof 现在都要求 revision 与有界 workspace fingerprint 同时匹配，
    所以未记录文件的外部编辑不能静默复用旧 green check。它刻意不同于
    `workspace/context_epoch.py`：context epoch 标识 prompt source provenance，
    workspace state 标识某条 verification observation 针对的文件状态。
  - Research 和 hybrid terminal event 现在把原始任务 turn budget 写入
    runtime terminal snapshot，即使 research engine 内部只用了更少轮数。
  - SSE subscriber queue、replay id、overflow marker、replay-window 检查移入
    `codey.app.event_bus`；`State` 只负责在 emit 前注入 active run identity。
  - 新增共享 Ghost JSONL event-log primitive，并把 signal、router、sleep、
    inbox、continuity、work queue、affinity、Hebbian store 都迁到它上面。
    腐坏或超限读取仍可观测，严格 transition store 通过各自 bad-row policy
    保持 fail-closed mutation 语义。
  - 新增共享 browser-provider stable-completion loop，并迁移 DeepSeek 与
    StepFun。两个 driver 都改用 `ProviderSendContext.record_response()`，
    不再手写 `ctx.last`，stable-response 计数集中到一处。
  - project completion 测试现在直接面向 operation module：analysis-run
    projection、repair 常量、verification candidate selection 和 writer
    failover ranking 的 patch 点都已迁移。生产代码不再为了测试保留
    旧 task-runner 私有方法入口。
- 审查后的冷启动清理：
  - A/B harness 的 git-state 读取改成 bytes 路径，未跟踪的中文文件名不会再在
    Windows locale 解码阶段把全量 pytest 打红。
  - JSON-tool 解析只忽略 JSON 对象外的 `<think>...</think>`，合法 tool
    参数、路径和 replacement 字符串里的 `<think>` 文本会原样保留。
  - SSE 历史 replay 现在有精确触发条件：只有携带正数 `Last-Event-ID` 的
    重连才重放 buffer。首次连接只靠 `/api/state` reconcile，不会重复旧聊天行。
  - repair 耗尽后的 blocked reason 统一从 `completion_blocked_reason()` 推导，
    并计入 repair turn；耗尽最后一轮预算时记录 `turn_budget_exhausted`，不再
    借用 `max_repair_rounds`。
  - 删除生产 `COMPLETION_ENFORCEMENT_MODE` 控制臂；现在唯一生产路径是
    proof -> bounded repair context -> final proof verdict。manual completion
    benchmark 也只执行这条路径。
  - 删除生产 metadata-only capability registry 及其 fingerprint 测试。
    capability boundary 改由 `docs/codey_event_matrix.md` 记录，并由 scanner
    测试核对生产代码里的 `capability_id` stamp。
  - deterministic research regression scorer 从
    `codey.research.regression_gate` 搬到 `tools/research_benchmark/scorer.py`；
    架构测试保证生产代码不能 import 这个 tooling 包。
- 冷启动审计加固：
  - terminal `task_done` 事件统一走一个 helper，用户 Stop / error 路径改用
    已观测到的真实轮数，不再硬编码为 0。repair 轮耗尽时也把
    `max_repair_rounds` verdict 持久化进 run-operation 寄存器。
  - edit-integrity diff 解析把每个 `---` / `+++` 都视作文件边界，
    headerless 的 untracked diff 不会再继承前一个 tracked 文件路径。
    Git change collection 对 CJK 文件名关闭 quoted path，并给合成的
    untracked diff 补 `diff --git` 头。
  - research provenance 收紧：synthesis merge 不再编造 conclusion 或
    counter-evidence 行；research record 构造可以从持久化 Sources 区绑定
    citation；`knowledge_write` 更新已有 note 时做 merge，除非显式覆盖，
    否则保留 created、session/project、sources、relations、tags、aliases。
  - Evidence ledger 遇到 active 文件不可读或写满时会轮转并写入可观测的
    warning reason code，不再让该 session 永久无法满足 completion gate。
  - 运行时 guardrail 与可观测性加固：DNS fake-IP 兼容改为 opt-in；
    consensus advisor 失败会进入 degraded reasons；JSON-tool codec 会剥掉
    `<think>` 块并对相同 tool call 去重；writer failover 在选择下一个
    provider 前先记录刚失败的 provider；shell 审批续跑使用审批时刻的
    active provider。
  - Web server 增加 POST body 上限，SSE 事件带有界 replay id 以支持重连，
    stopped run 会渲染 terminal status row，启动时刷新 provider status，
    localStorage 写入捕获 quota 异常，真实 Edge 浏览器 E2E 改为显式 opt-in，
    不再进入默认单元套件。
  - 移除 metadata-only capability registry 的运行时注入和若干小型旧壳
    （`DOC_SUFFIXES`、`_query_bool`、protocol JSON alias、browser-search 的
    不可达 raise）。更大的 audit-only 模块留到单独架构清理，避免这个 bugfix
    提交变成难审的大迁移。
- Runtime operation 事实现在只来自 session log。此前开发中的独立
  `codey/run_operation.py` 寄存器在发布前删除；唯一 durable source 是
  `RuntimeSessionLog`，`codey.runtime.operation_state` 从 `operation_effect` 行投影
  最新的有界 run phase。
  - `RuntimeOperationStore.start()` 会把 `operation_started` 和初始 phase
    原子追加到 runtime log；后续 phase commit 只追加一个 `run_phase`
    effect，terminal 时再追加一个匹配的 `operation_settled` outcome。
    不存在第二套 JSON 寄存器、迁移路径或旧格式 lookup。
  - 对已经 open 的同一个 run 再次 start，会恢复同一条 operation 的最新
    phase，不会追加第二个 start，也不会倒回 `accepted`。manual crash/resume
    smoke 现在会在 `writer_running` 硬 kill，然后用同一个 `run_id` 继续到
    terminal，并验证 lane 已关闭。
  - phase 合同仍然封闭：
    `accepted -> writer_running -> writer_settled -> completion_proof_recorded
    -> (repair_context_admitted -> repair_running -> repair_settled)* ->
    terminal`；任何非 terminal phase 都能直接终止，repair admission 只属于
    unsatisfied 的 failed proof，blocked verdict 只能走向 terminal。
  - runtime log 行和 phase payload 都是 schema-v1、封闭 key、零强转：
    缺 durable id/timestamp、带空白字符串、bool-as-int 计数、畸形
    proof/context/project ref、unknown effect kind、缺 effect ref、不可能的
    phase facts，或 forbidden raw 字段（`prompt`、`reply`、`stdout`、
    `stderr`、`diff`）都会在 replay 或 commit 前 fail closed。
  - recorded proof 和 blocked verdict 仍绑定 completion proof 词表：
    proof ref 是 `completion_proof:<16 hex>`，status 只能是 `complete` /
    `complete_with_limitations` / `failed` / `blocked`，
    `satisfied == (status == "complete")`，blocked verdict 必须由
    unsatisfied 的 `failed` 或 `blocked` proof 支撑。
- Task entry 现在通过 `TaskRuntime` 调度所有生产 submission，并在真实生命周期
  边界提交 completion/repair phase。runtime persistence 对用户任务保持
  fail-open：坏 runtime fact 只会禁用该 run 的 progress projection，不改变
  coding run 行为。
- Run Details 增加一行安静的 `Progress`，只在用户点开 Details、operation
  state 未到 terminal 且 ledger 没有 `run_finished`（旧快照不污染已完成
  run）时出现：`Writing was interrupted`、`Completion check was
  interrupted`、`Finishing was interrupted` 或 `Stopped during repair`——
  文案如实描述被中断的是哪一步：repair 已结束就说 check 被中断，proof 已
  满足就说收尾被中断。不加 chip、banner，不出现内部词汇。
- 这次不是继续抽薄 TaskFlow，而是删除 TaskFlow：stringly 的
  `completion_repair_admission` dict 换成类型化的
  `RepairContextProjection | None`；blocked reason 的长三元链移入纯函数
  `completion_blocked_reason()`；生产编排按真实 owner 拆到
  `provider_preflight.py`、`conversation_plan.py`、`mode_dispatch.py`、
  `task_run.py`、`research_flow.py`、`review_flow.py`、`planning_flow.py`、
  `ghost_context.py`、`ghost_post_turn.py` 和 `project_completion_flow.py`。
- 测试和 manual harness 现在直接 patch owning module，例如
  `codey.operations.research_flow.run_research_iteration`，不再要求生产类保留
  私有方法 patch 点。
- event matrix 注册 `runtime_operation.state` 行，durable state 是
  `runtime_session_log`。`State.forget_conversation()` 现在会删除该 session
  的 runtime log bucket。不加 Manager 类，不做 provider/tool replay，不改
  任何 prompt。
- 验证：deterministic crash-position 测试（writing / check / finishing /
  repair 各中断位置恢复后均给出诚实 progress）、runtime reducer 异常结算、
  phase round-trip、严格 fail-closed reader/writer、terminal 不可变、
  ledger/terminal 一致性、payload 卫生、
  `tests/manual/completion_operation_resume_smoke.py --self-test`，以及全量
  本地 pytest。不需要 live provider A/B——本版不改任何模型可见内容。

## 0.5.0 - Verified Completion v2 and Edit Integrity Monitor

- 把 0.5.0 的 edit-integrity monitor 接入生产 completion path，收掉 0.4
  stabilization A/B 暴露的缺口：Qwen 和 MiMo 会篡改测试夹具（删除、注释或
  `try`-`except` 包住 `import redis`）让 pytest 变绿，而生产 completion path
  对此毫无察觉。
  - `codey/completion/edit_scope.py` 拥有一份封闭的 edit-path 词表
    （production / test / fixture / verification config / docs /
    generated-vendor）、保守的任务级测试修改授权扫描，以及共享的
    `is_document_path` 定义（从 `verification_policy` 移入；它是 stdlib-only
    leaf，由架构测试锁住）。
  - `codey/completion/edit_integrity.py` 读取一次 run 的 changed paths 和
    change collection 已生成的 unified diff，输出有界、refs-only 的 finding，
    reason code 封闭：删除/注释的测试 import、`except ImportError` 保护、
    新增 skip、净删除的 assertion、变窄的 verification config，以及"验证变绿
    但没有任何生产文件变更"。用户任务明确要求修改测试时，finding 降级为 low
    severity，不当作篡改。raw diff 文本不离开该模块；任何内部失败一律
    fail-closed 为 `monitor_error`，绝不变成 clean。
  - Monitor 不是 evidence，不阻止 done，不自动 repair，也不新增 Manager；
    clean path 完全无感。
- 新增 `codey/completion/decision.py`，TaskRunner 抽薄：内联的
  enforcement-decision 闭包变成纯投影 `build_completion_decision(...)`，
  agent loop、repair 轮、receipt 和 trace 都读同一个 `CompletionDecision`
  （proof、provenance、analysis-run refs、failure class、local state）。
  重复的 changed-path 提取收敛为 `edit_scope.changed_paths_from_changes()`。
- `CompletionProof` 新增结构化 `diagnostic_refs`，指向限定该 proof 的
  edit-integrity observation；它参与 contract id 的内容寻址，并与
  review-finding refs 分开，不混用词表。
- Task receipt 重写为 schema v1
  （`TaskReceipt(display, work, verification, integrity)`）。
  - Trust 是合同不是分数：`trusted`（checks 通过且未观察到高风险 finding）、
    `needs_review`（checks 通过但存在高置信 integrity finding）、`limited`
    （checks 未通过，或 monitoring 失败 / 未完整观测导致绿色不可背书）。
  - 文案克制：`2 files changed · checks passed`、
    `2 files changed · checks need review`、
    `2 files changed · verification limited`；更长的解释放在
    `display.detail`，只给 Run Details 用。
  - 旧的顶层 `text` / `changed_count` / `checks_passed` /
    `restore_available` 字段全部移除。`RunResult.checks_passed` 不动：它仍是
    agent loop 的执行事实，不是 receipt contract。
- TaskRunner 接线：
  - 每个 completion 决策点（首轮和 repair 后）都观察 edit integrity；有
    finding 时 proof 以 diagnostic refs 重算一次，proof 与 observation 都写
    run trace。
  - 只有 receipt trust 为 `trusted` 的 run 才写 project facts 和 project
    memory，高置信 suspicious 的"绿色"无法再进入未来的验证习惯。
  - 终态事件的 receipt 直接从 ledger 持久化记录的 receipt 投影；旧的
    `receipt_from_projection_if_compatible()` 阴影校验被删除而不是改造。
- Trace / ledger / details：
  - `RunTraceManifest` 新增有界 `completion_edit_integrity` section 和
    `record_edit_integrity()`；completion-proof 行携带 `diagnostic_refs`；
    `edit_integrity` / `edit_integrity_finding` 进入共享 runtime-ref kind
    注册表。
  - `changes_collected` 存储校验后的 schema-v1 receipt；投影把它放在
    `ChangesSummary.receipt` 上，`build_task_receipt_from_projection()`
    返回与落盘完全一致的 receipt。
  - Run Details 的 Verification 行从 receipt contract 读取：
    `needs_review` 显示 `Test changes may have weakened checks`（warning），
    monitoring 不完整显示 `Verification monitoring incomplete`，其余保持旧文案。
- Headless JSONL receipt 与 Web UI 只消费 schema-v1 的 section；共享的
  `receiptSummary()` / `receiptChangedCount()` helper 位于 `render.js`，
  research receipt 改发 `display.summary` 而不是 `text`；ghost work queue
  改读 `receipt.work.changed_count`。
- Manual A/B 收敛：
  - `completion_enforcement_ab.py` 不再维护第二套 `modified_test_fixture`
    判断：fixture scope 改读该 run 自己 trace 里的 integrity 行，row 新增
    `receipt_trust` / `integrity_*` 字段。
  - 新增 `tests/manual/edit_integrity_ab.py`：把记录在案的 Qwen/MiMo 篡改
    signature 通过生产 monitor 和 receipt 回放（deterministic gate，20 例），
    并暴露两条最小 live smoke：DeepSeek clean path 与 Qwen/MiMo
    `dependency_missing_env_failure`。本版不需要完整生产质量 A/B；两条
    live smoke 已补齐 manual evidence：DeepSeek clean path 为
    `receipt_trust=trusted` / `integrity_status=clean` / 无 warning；Qwen
    dependency-missing tampered-test case 为 `receipt_trust=needs_review`，
    reason code 为 `test_import_removed_or_commented`。
- 评审轮加固（当日 findings，全部在提交前修复）：
  - `completion_evidence()` 在每个调用点显式传入快照（changes、changed、
    scope files、selected check、stop reason），integrity 观察永远读
    repair 之后的 diff，不再用 repair 前缓存的 diff。
  - "fix the failing test" 不再授权测试修改（它通常指修生产代码）；中文
    授权词只保留明确的 修改/更新/调整测试。
  - diff 扫描改为 per-section 饱和：超大的生产 diff 不会掩盖其后被篡改的
    测试文件。
  - import finding 与未加保护的重新添加对消（合法移动不再误报）；
    `with pytest.raises(...)` 的删除计入断言删除；具体异常放宽为
    `Exception` 是新的高信号 finding（`test_expected_exception_widened`）。
  - verification config finding 只在可证明收窄的新增（`--ignore`、
    `--deselect`、`-k "not ..."`）和严格收缩的 testpaths 替换时触发；
    删除 testpaths/addopts 不是收窄信号。
  - receipt schema v1 补全审计闭环：`verification.state`、
    `verification.proof_refs`、`integrity.affected_paths`、
    `integrity.refs` 随 receipt 走（有界，不含 raw diff）。
  - Trust 合同收紧：声称 checks 通过但没有 integrity observation 的
    receipt 一律 `limited`，绝不 `trusted`；Run Details 不再从旧
    `checks_passed` 事实反推绿色（无 receipt → "Checks not recorded"）。
  - README / DESIGN 的 receipt 文案同步为 schema-v1 措辞。
- 第二轮评审：
  - 覆盖 Node 侧验证入口且不做粗暴归类：`jest.config.*` /
    `vitest.config.*` 归为 verification config；`package.json` 保持
    production，由内容级规则判断（npm `test` script 的 runner 被掏空，
    或对其新增收窄 flag）。
  - Run Details 的 Verification 行只读 receipt contract；基于 trace 的
    integrity 兜底已删除。
  - Trust 合同：有文件变更、checks 绿但 observation 为 `unobserved` →
    `limited`；只有零变更的 run 保持 `trusted`。
- 第三轮评审：
  - 持久化 receipt 的 reader 现在复算合同：trust 与 display 文案改由
    builder/reader 共用的 primitive helper 计算，integrity
    status/severity 必须在封闭枚举内——与自身事实不一致的落盘 payload
    直接拒收，不再原样放行。
- Release-candidate 加固：
  - schema-v1 receipt reader 现在拒绝非规范 JSON 类型（`true` 伪装成 `1`、
    numeric bool、非字符串 ref list）；builder 也不再把布尔型
    `changed_count` 当成 1。
  - terminal event 只要 run ledger 的 durable projection 已经有 receipt，
    就会从 ledger 补入或覆盖 receipt，包括 final changes 已落盘后的 late
    stopped/error 退出。没有最终 `changes_collected` receipt 的 run 继续保留
    原 mode-specific receipt 或不带 receipt。
- 0.5.0 hotfix：
  - Edit Integrity Monitor 的 `clean` 现在要求 changed paths 都有可解析 diff
    覆盖。已知文件发生变化但 diff 缺失或只覆盖部分文件时，observation 变为
    `unobserved` 并携带 `diff_unavailable`，绿色 receipt 降级为
    `verification limited`，且不会写 project facts / project memory。
  - 测试修改授权先检查明确否定：`not/no tests`、`without changing tests`、
    `tests ... unchanged`，以及中文“不要/别/不改测试”等语义会压过宽泛的
    edit/test 授权匹配；明确要求“不改测试”时篡改测试仍保持 high suspicious。
- Bounded-observation hotfix：
  - diff section 现在携带私有 saturation 标记。monitor 对某个 changed section
    达到 `MAX_SECTION_LINES` 上限时，observation 不能再是 `clean`：已经看见的
    finding 保持 `suspicious`，否则降为 `unobserved` 并携带
    `diff_unavailable`。
  - monitor 现在把 `changes.truncated` 当作不完整观察；全局 diff 被采集层截断时，
    有文件变更的绿色 receipt 会降级为 `verification limited`。
  - content scan 现在遍历所有已解析 diff section，只对输出的 finding 和
    affected paths 保持有界，避免第 13 个或更晚的测试 section 被前面的文件掩盖。
  - Git rename/copy 展示路径会按新路径作为 changed-path identity，并保留
    `previous_path`。`collect_git_changes()` 与 completion edit-scope helper
    现在使用同一 canonical shape，减少普通 rename 被误降为
    `verification limited` 的噪音。
- 新增测试：`test_completion_edit_scope.py`、
  `test_completion_edit_integrity.py`、
  `test_task_runner_edit_integrity.py` 和 `tests/fixtures/edit_integrity/`
  路径形状 fixtures；receipt、ledger projection、ledger、details、server、
  UI、checkpoint-flow、enforcement 测试迁移到 schema-v1 contract。架构测试
  锁定 `edit_scope` 为 stdlib-only leaf，并保证 `edit_integrity` /
  `decision` 不依赖 provider/browser/tool-runtime/server。

## 0.4.21 - Research and Ghost A/B Stabilization

- 将 `verification_review_ab.py` 迁移到 release-grade A/B 证据脊柱。
  - 固定 output 运行现在会在同一个 arm layout 下写 result JSON、journal
    event、transcript ref 和 manifest。
  - `--self-test` 覆盖 baseline/current prompt 差异；固定 output 续跑会跳过
    已完成 row；`--rerun-failed` 在 provider 连接失败且新 row 尚未落盘时保留旧证据。
  - DeepSeek live smoke 显示了预期的 reviewer 行为差异：baseline 批准了合成
    diff，current arm 则要求补测试并点名已有 check 路径。
- 跑完第一轮 DeepSeek 单 provider live smoke，覆盖 coding extended、Research
  和 Ghost A/B arms。
  - `read_before_edit` 和 `impact_guard` 两臂均成功；其中 `impact_guard` 在
    这一个样本里暴露 guard 后用更少 turn/tool 完成。
  - `scoped_task_plan` 在 scoped arm 上优于 current arm，但 prompt surface 更大。
  - `bounded_research_planner` 单 case 分数从 `3` 提升到 `5`；`search_coverage`
    更明确地报告非 UTF-8 文件导致的 incomplete scan。
  - `source_connector` 和 `source_connector_done` 给出有价值的负证据：这个样本里
    connector 与 batch/checklist arm 没有减少 retry，也不应默认推广。
  - Ghost continuity、router、signal extraction 和 work queue probes 通过
    DeepSeek control/treatment smoke，未观察到 evidence/citation 污染。
- 修复 DeepSeek 实机运行中暴露的 manual harness 问题：
  - `read_before_edit_ab.py` 会为固定 `--out` 路径创建父目录。
  - `scoped_task_plan_ab.py` 支持真正的单 arm live run。
  - `source_connector_done_ab.py` 自己拥有 trace bound 和 `LiveTrace` helper，
    不再依赖 `source_connector_ab.py` 已删除的内部实现。
  - `bounded_research_planner_ab.py` 接收并转发 production `ResearchPipeline`
    使用的 `topic_continuity_context` / payload 参数。
  - `ghost_research_continuity_ab.py` 支持单 arm 运行和有界 provider/new-chat
    timeout，避免混合 arm 流量与无限 live wait。
  - `ghost_router_ab.py` 和 `ghost_work_queue_production_ab.py` 将 control case
    判定为 no-regression，而不是强制要求 cost 严格下降。

## 0.4.20 - Completion A/B Stabilization

- 跑完第一轮 DeepSeek coding/completion core 实机 A/B：
  `control_done`、`proof_only_block`、`repair_context`、
  `repair_context_minimal`，结果、journal 和 archived transcript 均落盘到
  `tests/manual/results/0.4.20/`。
- 修复实机 A/B 暴露的 requested-verification 循环问题：模型已经在最新编辑后
  观察到一次失败 run 时，低层 agent loop 不应继续强迫模型“跑到 green”。
  - `codey.agents.runner` 现在只负责保证用户显式要求验证时，最新编辑之后至少
    观察过一次 run tool call；
  - run 的 pass / fail / unavailable 语义仍由 completion proof 层判断；
  - 最新编辑之前的 run 不再能满足 requested-verification observation guard。
- 补 deterministic 回归测试，覆盖 verification observation epoch 以及 failed
  verification 能进入 completion proof 层。
- 在本轮 A/B 中顺手收紧 `completion_enforcement_ab.py` 证据处理：
  - terminal `stop_reason="error"` row 即使还没有 `error` 字段，也会让 report fail；
  - terminal error summary 会保留到 result row；
  - live path 使用真实 production agent runner 和 change collector，不再传
    `None` callable；
  - provider failure class 字段与 manual A/B 闭合 schema 对齐。

## 0.4.19 - A/B Evidence Polish and Passive Worker Health

- 统一 manual A/B 证据落盘结构（`tests/manual/ab_harness_common.py`）。
  - 新增 `ArmRunLayout`、`ArmManifest`、`ResultRowStore`，让固定 `--output` 运行同时绑定 result JSON、journal 目录、transcript 目录、manifest 路径、provider、git commit 和 dirty 状态。
  - 重跑同一 provider/case/arm/repeat 时改为原子替换旧 row，不再 append 旧失败 row 污染 summary。
  - pending 计算阶段不会修改旧 result 文件，所以 provider 连接失败不会提前擦掉历史证据。
  - transcript 默认 digest-only；只有 archive 文件真实存在时，row 才会标记为 replayable。
  - provider failure 改为闭合枚举（`provider_send_error`、`provider_no_reply`、`native_search_stall`、`webpage_ui_changed`、`unknown`、`none`），和 Codey/runtime failure 分开。
- 将 `completion_enforcement_ab.py`、`research_to_code_ab.py`、`bounded_research_planner_ab.py`、`ghost_research_continuity_ab.py` 的 live-output 路径迁移到 common result/journal 布局。
  - journal 使用基于 output 的稳定 identity，resume 时允许追加带 `resumed_attempt` / `attempt_index` 的 `run_start`。
  - journal 打开后如果外层失败，会补一个 terminal failed `run_complete`，但不会删除旧 result row。
- 增加 BrowserWorker 被动健康快照。
  - `BrowserWorker.health_snapshot()` 记录 queue size、当前 job 状态、运行时长、stuck 阈值、job 计数和线程存活状态。
  - `BrowserSearchProvider` 在 worker 边界 timeout/cancel 时记录最新 worker health，方便把 Qwen/native-search 卡住归因到 provider/worker 层，而不是误判为 planner 质量。
  - 回归测试使用非协作 job 复现“调用方已经 timeout，但 worker 线程仍被占用”，并证明这里只观测、不自动重启。
- 加固显式原子写权限。
  - `mode=` 显式传入时，如果 `fchmod/chmod` 无法应用权限，现在 hard fail。
  - `preserve_mode=True` 仍保持 best-effort，普通状态替换行为不变。
- 将 Ghost Work Queue 状态迁移规则固定成显式 invariant。
  - `WORK_ITEM_TRANSITION_MATRIX` 成为 action/status transition 的唯一 authority，并用测试绑定 patch schema。
- 将成功的 `NetworkStatus` 状态名从 `PUBLIC_WEB` 改为 `POLICY_ALLOWED`。
  - 保持 `NetworkDecision.allowed` 和 `check_fetch_url()` 行为不变，但让状态名更贴近真实契约。
  - `POLICY_ALLOWED` 只表示 URL 通过了 Codey 当前配置的 fetch policy；在启用 TUN/透明代理 fake DNS 兼容时，它不是 DNS 解析到真实公网地址的证明。
  - 增加回归测试，防止允许状态的名称或值重新漂移回 `public` / `web` 语义。
- 更新 roadmap，固定窄 0.4.x stabilization track、post-0.5 exit gate 和 0.6 consolidation 路线。

## 0.4.18 - Network Boundary, Cooperative Cancellation, and Storage Unification

- 将基于 lock 文件创建/删除与过期接管（stale takeover）的旧文件锁模型重构为基于 OS 内核与线程隔离的建议锁（`codey.storage.file_lock`）。
  - 底层使用操作系统原生锁（Windows 下为 `msvcrt.locking`，POSIX 下为 `fcntl.flock`）并结合进程内线程同步（`threading.RLock`）与线程重入计数。
  - `LockTimeout` 继承自 `TimeoutError`（`OSError` 的子类），与既有 store 的 `except OSError` 错误处理契约完美保持一致。
  - 实现进程级锁引用计数自动回收机制（`_ProcessLockEntry` 与 `_borrow_process_lock` / `_return_process_lock`）：加锁尝试时借出引用递增，退出或超时时在 `finally` 块中归还引用递减，引用归零即从全局映射中安全移除，彻底杜绝长生命周期多项目运行下的进程级内存泄漏。
  - `.lock` 文件作为常驻磁盘的锁载体，不再通过 `stat -> unlink` 表达所有权，彻底消除 stale takeover 的 TOCTOU 竞态。
  - 新增专用模块 `codey.storage.event_state` 提供 `reset_event_backed_state(events_path, *state_paths)` helper，确保 event log 与 derived projection 在权威事件锁保护下安全删除。
  - 彻底清理移除无生产调用的 `transactional_json.py` 冗余抽象。
  - 统一全仓全部 7 个 Ghost store（`work_queue`、`affinity`、`continuity`、`hebbian`、`inbox`、`router`、`sleep`）的合作式锁（Cooperative Lock）纪律：
    - 所有公开读取 API（`list_*`、`export_state`、`query_*_hints`、`learning_enabled`）统一持有 `self.events_path` 锁，避免与并发 `reset_all()` 或 mutation 交错产生中间态；
    - 内部读与 projection helper 规范重命名为 `_xxx_unlocked`（如 `_load_items_unlocked`、`_read_events_unlocked`），明确表达调用方已持有事件锁；
    - `compact_if_needed()` 将事件文件状态检查、内存状态加载、事件紧缩与重写、紧缩后状态检查整体封装在单个 `with with_file_lock(self.events_path):` 块中原子执行，消除无锁 stat 带来的竞态；
    - 修复了 `work_queue`、`affinity` 与 `router` 在锁获取超时时 `compact_if_needed()` 异常处理中引用未定义 `before` 变量的 `UnboundLocalError`。
- `BrowserWorker` 增加协作式取消与解耦的任务生命周期管理（`codey.automation.browser_worker`）：
  - 引入 `_Job` 数据类与 `_JobState` 状态机（`QUEUED`、`RUNNING`、`COMPLETED`、`CANCELLED`、`CANCELLATION_REQUESTED`）；
  - 调用方超时或取消事件自动桥接至 worker 线程的 `cancellation.scope` 和 `cancellation.deadline_scope`；
  - `BrowserWorker.call()` 重入分支在当前线程执行时继承 active cancellation 和 deadline scopes，支持嵌套超时；
  - 队列中尚未开始的 job 被取消后直接跳过执行；
  - `BrowserSearchProvider` 在页面导航、解析及循环中增加密集 cancellation check；抓取与搜索路径统一封装在页面丢弃清理边界内（`_discard_fetch_page_on_browser_thread`、`_discard_search_page_on_browser_thread`），在取消或超时发生时彻底关闭底层页面并置空引用，消除中间态重试与页面泄漏。
- 收敛统一 NetworkPolicy 单一来源与 DNS 缓存机制（`codey.policies.network`、`codey.research.connector_search`、`codey.research.tools`）：
  - 建立统一的 `NetworkPolicy` 与精简状态机 `NetworkStatus`（`POLICY_ALLOWED`、`BLOCKED_PRIVATE`、`BLOCKED_UNRESOLVED`、`INVALID_URL`），集中化 SSRF 风险防护；
  - 恢复严格保守的非公网 IP 拦截逻辑（`not ip.is_global or ip.is_multicast`），彻底覆盖 `100.64.0.0/10`（CGNAT）与所有保留地址空间；
  - 支持 `allow_dns_fake_ip=True`，兼容 TUN/透明代理的 `198.18.0.0/15` fake-ip 域名解析，同时坚决阻断字面量 fake-ip 并杜绝空解析 fail-open 风险；
  - 明确记录 `POLICY_ALLOWED` 只表示“按当前 policy 允许”，不是在所有本地代理配置下都证明 DNS 解析到了真实公网地址；
  - 在 `ResearchTools.open_url()` 公共工具入口处前置执行 URL policy 校验，并在 fetch 后 final URL 二次校验中复用短 TTL policy cache，避免同一短窗口内重复 DNS；
  - Connector 请求（`connector_search.py`）采用无自动重定向的 opener，对每一步重定向目标进行逐跳（hop-by-hop）URL 策略校验（`check_fetch_url(use_cache=True)`）并限制最大重定向深度；
  - 新增共享 `codey.research.http_redirects`，统一承载无自动重定向 opener、redirect status 解析、Location header 解析与 best-effort response close helper，供 connector 和 browser PDF fetch 路径复用；
  - Connector URL 打开路径现在永远走无自动重定向 opener；移除旧的 `urllib.request.urlopen` monkeypatch fallback，测试不能再绕过生产 redirect 边界；
  - Connector 的 `HTTPError` redirect 响应会在跟随下一跳之前显式关闭；redirect 测试也改为逐跳 mock policy 判断，不再依赖 fixture URL 的真实 DNS；
  - Connector redirect 多跳共享同一个 request 总 deadline；每一跳只拿剩余 socket timeout，redirect 链不会把单次 connector 请求预算按跳数放大；
  - `check_fetch_url()` 直接从 `codey.policies.network` 导出；清理彻底删除了多余的 `codey/research/url_policy.py` 兼容层；
  - 为浏览器自动化子资源拦截引入差异化 bounded TTL cache（允许域名 5s，拦截/未解析 45s），兼顾性能与安全防护边界。
- Agent Runner 协议交互优化（`codey.agents.runner`）：
  - 移除当模型返回有效工具调用但遗漏 `<continue>` / `<done>` 控制元素时的隐式终止行为，改为将工具执行结果规范格式化后带协议提醒返回给模型继续下一轮推理。
- Edit 工具严格化与路径模型说明（`codey.toolchain.runtime`）：
  - 移除 `_replace_unique_indentation_recovery()` 的直接文件写入路径，当 exact 和 CRLF 匹配失败时不修改文件，直接返回带有上下文与行号的诊断指引；
  - 在 `safe_join()` 中明确补充路径遍历威胁模型说明。
- 存储原语统一与权限收敛（`codey.storage.atomic_io`、`codey.storage.local_store`、`codey.workspace.changes`、`codey.storage.managed_outputs`、`codey.knowledge.store`）：
  - 统一 `write_bytes_atomic`、`write_text_atomic`、`write_json_atomic` 原子写入原语；
  - `write_bytes_atomic` 在创建临时文件时直接通过 `os.open(..., O_CREAT | O_EXCL | O_WRONLY, creation_mode)` 应用目标权限，并在写入后、flush/fsync 前应用 chmod/fchmod，消除 umask 暴露窗口并优化元数据持久性顺序；
  - POSIX 环境下补充目录同步（`_fsync_dir`），提升文件系统崩溃容灾持久性；
  - 本地凭据存储（`save_local_config`）强制使用 `0o600` 权限；
  - 将工作区快照（`changes.py`）、工具大输出（`managed_outputs.py`）及知识库（`store.py`）的原子写入统一收敛至 `atomic_io`。

## 0.4.17 - OS-Backed File Locks and Event State Reset

- 将基于 lock 文件创建/删除与过期接管（stale takeover）的旧文件锁模型重构为基于 OS 内核与线程隔离的建议锁（`codey.storage.file_lock`）。
  - 底层使用操作系统原生锁（Windows 下为 `msvcrt.locking`，POSIX 下为 `fcntl.flock`）并结合进程内线程同步（`threading.RLock`）与线程重入计数。
  - `LockTimeout` 继承自 `TimeoutError`（`OSError` 的子类），与既有 store 的 `except OSError` 错误处理契约保持一致。
  - `.lock` 文件作为常驻磁盘的锁载体，不再通过 `stat -> unlink` 表达所有权，消除 stale takeover 的 TOCTOU 竞态。
  - 新增专用模块 `codey.storage.event_state` 提供 `reset_event_backed_state(events_path, *state_paths)` helper，确保 event log 与 derived projection 在权威事件锁保护下安全删除。
  - 彻底清理移除无生产调用的 `transactional_json.py` 冗余抽象。
  - 统一全仓全部 7 个 Ghost store（`work_queue`、`affinity`、`continuity`、`hebbian`、`inbox`、`router`、`sleep`）的 mutation 锁纪律：所有 append、replay、rebuild、delete_scope、reset 和 compaction 操作均在各 store 的 `events_path` 锁保护下执行。


## 0.4.16 - Ghost Event Canonicalization and Work Queue Invariants

- Ghost Affinity 和 Work Queue 事件日志现在记录语义意图事件，不再记录已计算
  好的 upsert 结果。Affinity replay 通过 reducer 应用 node/edge reinforcement
  spec、scope delete、decay event 和 snapshot anchor；Work Queue replay 通过
  reducer 应用 observed candidate、带 precondition 的 transition、delete event
  和 snapshot anchor。冷启动 schema 常量仍为 `1`；旧 upsert event type 不再
  兼容，mutation 会 fail closed。
- Ghost Work Queue 的 `ghost_work_item_transitioned` 事件校验提升至严格的
  action-specific 字段与状态语义约束（如 `claim` 必须有非空 `started_run_id`、
  `lease_expires_at` 和严格递增的 `retry_count`；`complete` 必须有非空且与
  `expected_started_run_id` 严格相等的 `completed_run_id`、非空 `proof_refs` 并清空 lease；
  `queue` 必须显式包含 `retry_count == 0` 并清空运行态字段；`release` 到 `queued` 必须显式清空 lease 和
  `started_run_id`）。`complete_item()` 在进入 mutation 前严格校验非空 `run_id` 与 running 状态匹配，
  阻止空 `run_id` 写出 invalid event 或误 block 其他 run 的 item。
  `GhostWorkItem.from_payload()` 对 snapshot/observed item 执行严格的状态不变式校验（`done` 必须同时
  具有 `completed_run_id` 与 `proof_refs` 且清空 lease/block reason；`queued`/`candidate`/`rejected` 清空全部
  运行与完成字段；`running` 必须有 `started_run_id` 且清空完成/阻塞字段；`blocked` 必须有 `blocked_reason`
  且清空 lease/完成字段），非法状态反序列化直接 fail-closed。
  Replay 增加 kind-specific primary proof 验证与全序序列回放校验，任何非法 transition 均判定为 `invalid_event` 并触发 fail-closed 阻断。
- Ghost Affinity 和 Work Queue 的 mutation API 现在把 read -> reduce ->
  decide -> append/rewrite -> project 流程放进 store 文件锁内，关闭并发
  reinforcement 和双重 claim 这类语义 lost-update 竞态；所有 mutation 操作
  （包括 `GhostWorkQueueStore.delete_scope()` 和 `GhostAffinityStore.decay()`）
  统一返回结构化诊断字典并合并 `self.last_warnings`，确保调用方诊断不漏报。
- Ghost Work Queue 的 `compact_if_needed()` 增加“projection 存在但 events 缺失”
  的同构检查并上报 `work_events_missing`；清理 `_transition_item()` 无用参数，并移除未使用的死代码 `_release_stale_claims()`。
- Ghost Affinity 和 Work Queue 的 validator 现在拒绝非 canonical 原始事件
  payload，不再靠 silent normalization 放过：顶层多余字段、snapshot/spec/item
  多余字段、畸形 delete/scope payload、缺失的 Affinity decay counter、字符串或
  bool 计数字段、bool 伪装 number 或 int 伪装 float 的类型混淆，以及没有
  source/target node 的 edge reinforcement，都会在 mutation 前 fail closed。
- 这次 hardening 不改变 prompt、provider tool、UI/SSE payload、permission
  profile 或默认 Research/Writer 行为；它只收紧本地 Ghost event log 的读取和 replay。

## 0.4.15 - Run Command Boundary + Stabilization Hardening

- run-command policy 现在把 pytest ini override 当成第二层 argv 面处理。
  `addopts` 会用同一套命令分词规则递归解析；`cache_dir`、`log_file`、
  `pythonpath`、`testpaths` 是显式路径型 key；`-oaddopts=...` 这种 pytest
  紧贴短选项形式也会进入同一层递归解析；path-shaped 的 discovery pattern
  会进入项目边界校验；不支持的 override key 直接 fail closed，不再静默
  携带隐藏的文件系统 operand。
- 直接 `python script.py ...` 形式的验证命令现在会检查脚本后续
  path-shaped 参数，而不只检查脚本路径本身，然后才允许 allowlist 放行进程。
- Provider adapter override 现在只安装目标 provider 声明过的 adapter repair
  surface，并对 generation 清理做路径 guard，不再把整棵 `codey/` runtime
  快照复制到 override 路径。
- Evidence、Ghost affinity 和 Ghost work queue 的 read-modify-write 状态路径
  改用带锁 JSON mutation 原语，避免多个本地 Codey 进程协作写入时丢更新。
- Ghost affinity 不再把更多 `source_refs` 当成更强 reward；ref 只作为
  provenance，一次观测事件只贡献一次 reinforcement。
- manual A/B harness 共享 arm manifest、output identity、失败 row 追加/替换、
  稳定 journal 生命周期 helper 和有界 failure class，让 live evidence 可以续跑
  和审计。
- Provider worker 的 CDP target lookup 在 browser binding 支持时会释放临时
  CDP session。
- CI 现在覆盖 Python 3.11、3.12 和 3.13。
- manual A/B 验证探针现在会在临时项目还存在时把 `root` 传给 selected-check
  覆盖判断；completion enforcement journal 测试改为直接使用共享 journal
  helper，并移除了已经不用的私有转发函数。
- README 项目结构更新为冷迁移后的 package 布局（`app/`、`providers/`、
  `repairs/`、`runtime/`、`storage/`、`workspace/`），不再列已删除的扁平
  provider 模块名。

## 0.4.14 - Provider Package Cold Migration

- provider 运行时模块现在只存在于 `codey.providers.*`。顶层
  `codey/provider_*.py` 和 package 内部中间态 provider 前缀名字全部消失；
  生产代码、测试、mock patch 路径、manual A/B fixture、文档和工具都指向最终
  provider 路径。
- 内置 provider profile 数据移动到 `codey/providers/profiles.json`，并作为
  `codey.providers` package data 打包。
- `codey.providers` 保持 lazy public export，导入轻量 provider 支撑模块时不会
  顺带加载所有 web driver。
- 这是给 provider A/B 准备的路径级冷迁移基线：不留兼容壳，不做预期行为改动。

## 0.4.13 - Verified Completion Enforcement + Repair Context Admission v1

### 最终发布收口

- prompt / 模型可见文本的密钥筛不再把带少量数字的普通 CamelCase 工程标识符
  判成高熵密钥：`OAuth2CallbackHandler`、`HTTPRequest2Handler`、
  `Windows10CompatibilityMode`、`PyPI2026ReleasePlan` 现在能通过全局 prompt
  gate；显式 marker、provider key shape 和真正随机的大小写混合 blob（如 `AbcdEfghIjkl1234X`）仍会被筛掉。
- adapter-repair sandbox 现在会在 walk/copy 之前拒绝 `source/codey` package
  根本身是 symlink 的情况。前一轮加固已经拒绝 package 树内 symlink 与引用文件
  symlink；现在根 package symlink 也覆盖到了。
- completion repair-context 的 digest 现在包含实际送给模型的有界事实包哈希。
  trace payload 仍然没有 raw text 字段，但模型可见事实变化时 digest 会变化，
  不再只跟 counts / reason codes 变化。
- repair round 之后的最终 proof 会先刷新 verification candidates 再选择相关检查。
  修复轮如果改变了验证作用域，例如从 frontend 命令转到 backend 命令，不会再被
  repair 前的候选验证视图误判。
- `tests/manual/completion_enforcement_ab.py` 的 live 模式现在每完成一个
  case/arm row 就落盘 JSON，并复用 manual A/B 的 journal plumbing 记录
  prompt/reply 交通。默认 `--transcript-mode digest-only` 只保留哈希；
  `archive` 才保存有界 manual-layer 聊天记录供 prompt-lab 诊断；生产代码仍然
  不 import 这些测试层材料。固定 `--output` 路径现在可以干净续跑：
  既有 row 不会被覆盖，已完成 row 默认跳过，`--rerun-failed` 才显式重跑错误
  row；旧 error row 只有在新 row 产生后才替换，provider connect 失败会保留旧
  row，journal run id 稳定绑定 output stem 且不会重复写 `run_start`。

### 本批加固与清理

- `providers.preferred` 正式接入为**软偏好排序**：`project_config.py` 新增
  `preferred_provider_for(config, mode)`，把项目内按模式的 provider 偏好传给
  两个 failover 排序点（启动 preflight 与 writer failover 的
  `rank_providers(preferred=...)`）。此前该配置只解析不消费。它只重排候选：
  不能覆盖用户显式选择、不能绕过 supervisor 可用性/排除、不能启用未连接的
  provider。`.codey/config.json` 每次运行只读一次，并与 project context
  builder 共享。
- 命令分词统一：新增 `codey/command_line.py::split_run_command`
  （Windows 用 `posix=False` 并去掉匹配引号，`C:\path\file.py` 不再被吃掉；
  POSIX 用 `posix=True`）。`tool_runtime`、`shell_risk`、`action_policy`、
  `verification_policy`、`project_facts` 全部走同一入口——审批风险分析、
  policy guard 与实际执行看到同一个 argv；分词失败一律 fail-closed。
- ChangeTracker 竞态修复：baseline 状态统一由一把可重入锁保护，
  `collect()` 默认严格只读。UI 轮询不再可能在编辑采集进行中把 clean
  baseline 弹掉。清理动作移入显式 `prune_clean()`，只在 run 终态调用。
  追加：capture 侧遗留竞态也已关闭——`capture_before()` 在锁外读完文件后
  回锁复查 membership（两个线程竞争同一文件只产生一条 baseline，
  `_total_bytes` 只计一次）；`capture_after()` 在 hash 完成后回锁复查，
  `prune_clean()` 在 hash 期间删掉 baseline 不再留下孤儿 after-hash。
  两处均有专门并发测试覆盖。
- 非 Git recovery 快照改为两层存储（`baselines/<rel-digest>` 正文文件 +
  小 `manifest.json`）：每次编辑只写一个有上限的 baseline 文件和 manifest，
  不再把最多 64MB 的 JSON 整体重写（消除写放大）。两层格式即
  `schema_version: 1`；任何其他 manifest 布局（包括旧的扁平格式）一律忽略——
  不保留兼容路径。
- `UiStateStore.save()` 改为与内存缓存比较（首次 load 时填充），不再每次
  读盘+解析；写失败时保留上一个已落盘基线。
- SSE 慢客户端丢事件改为可见：溢出时会先塞入有界的
  `{"type": "resync_required", "reason": "sse_queue_overflow", "dropped": N}`
  标记，前端收到后重新拉取 run-state 与 providers，避免无声丢失终态/
  审批事件。
- TaskRunner 关闭早期异常窗口：claim/route 阶段的任何异常现在都会产出
  bounded error terminal event、best-effort trace finish 与
  `state.finish_run(...)`，run slot 不会再被永久占用。
- Provider worker 请求改为小步轮询并检查取消事件，Stop 约 0.2s 内即可打断
  等待中的响应，而不是等满整个 timeout。
- 已批准 shell 命令改走共享进程树管理器（`cancellation.run_process` 并绑定
  stop flag）：Stop 会杀掉整棵命令进程树而不是留下孤儿进程。
- `DeadlineExceeded` 在 agent 工具循环中与 `TaskCancelled` 一样向上抛出，
  不再被吞成普通工具错误；provider 预算耗尽后不会继续烧 turn。
- Work checkpoint 把 hash 无法获取的被修改路径显式记录
  （payload / prompt / reconcile 三处的 `hash_unavailable_files`），
  不再从看似正常的 checkpoint 里静默消失。
- Live A/B harness 改从 RunTrace manifest 的 `completion_repair_context`
  行读取 repair 证据，live 报告不再显示 `repair_rounds=0`；注入的
  `import redis` 失败只用于 `dependency_missing_env_failure` 用例，
  `fresh_failing_test_after_edit` 成为可靠的可修复用例。
- `_bounded_summary()` 保持 evidence 层给出的可读尾部顺序（repair context
  输出不再倒序）。
- 删除死代码面：`builtin_profiles`（模块、TaskRunner 注入字段、server 接线、
  capability 声明、文档页与测试——它只是 metadata 且设计上就不参与决策）、
  `verification_map.py` 中重名的 `VerificationCandidate`（改名
  `TestCandidate`）、无引用的 selector 常量（`SEND_READY`/`INPUT`/`RESPONSE`/
  `ANSWER`/`SEND_BUTTON`）、`citation_scanner.source_id_bracket_ref_items`、
  `provider_discovery.find_control/find_response`、
  `provider_controls.reject_flow`。同时删除适配层修复面上两个零引用 helper
  （`adapter_surface.shared_web_adapter_files` 和
  `adapter_surface.is_known_provider`）：调用方直接读
  `SHARED_WEB_ADAPTER_FILES` 常量和 `adapter_repair_surface()`，无人调用的
  "已知 provider" 谓词只是备用抽象。
- Provider 适配层去重：五个几乎相同的 `providers/*_web.py` 合并为单一
  spec 驱动的 `web_provider.py`（约 -270 行）；控制定位/响应计数/限流检测/
  迟到响应轮询等公共脚手架提取到 `providers/web_drivers/common.py`；provider id
  规范化统一进 `codey/providers/ids.py`。各站点自己的完成判定刻意保留在
  各自 driver 内。
- Adapter 自修复边界按影响面升级。新增 `codey/adapter_surface.py`，把修复面
  定义为"单个 provider 的 driver + 共享网页适配层"（`web_provider.py`、
  `web_driver.py`、`web_drivers/common.py`、provider profiles/controls/flow/
  send-loop/submission/timeouts、clipboard、browser）。由于修复安装在
  per-provider override root 下，改 override 内的共享文件不会泄漏进其他
  provider 的运行路径。`repair_policy.validate_candidate()` 不再把共享文件当
  违规，而是把改动分类为 `provider_local` / `shared_web_surface` /
  `profile_data`；改动扫描覆盖 `*.py` 与 `*.json`（forbidden-snippet 扫描仍只
  针对 Python）。`_run_static_checks()` 按影响升级验证：改共享层必须整体
  import web provider 层，改 profile 数据必须通过 schema load。tests 与 Codey
  core runtime 仍然拒绝。修复 prompt 改为陈述真实边界（只改网页适配面、
  运行在 provider 级 override 沙箱、不得改测试与 core runtime）。
  `adapter_overrides.adapter_base_hash()` 把修复面上的 JSON 纳入 hash，
  内置 `profiles.json` 变更会使旧 override 失效。修复面 fail
  closed：driver 缺失即修复面为空，共享文件永远不会被单独授予——
  `validate_candidate()` 明确报 unsupported provider，`run_adapter_repair()`
  在调用模型和安装之前直接拒绝。
- Provider id 规范化彻底统一：`adapter_overrides.py`、`provider_supervisor.py`、
  `self_repair.py`、`repair_policy.py` 中的本地 `_provider_id()` 全部改为委托
  `provider_ids.normalize_provider_id()`。
- 各站点 driver 收进 providers 包：五个页面驱动即
  `codey/providers/web_drivers/*.py`，与共享脚手架 `common` 同包。
  `web_provider.py` 直接从该包导入 driver，driver 引用同包的 `common`，
  不再回穿 providers 包 init——冷启动脆弱形状消除。`web_drivers/__init__.py`
  刻意保持无 import。
- `UiStateStore.save()` 的缓存快路径假设每个 `state_home` 只有一个 Codey
  server 写入；该单写者假设已在代码中文档化。

- 新增 `codey/completion_verification.py`：把 coding verification 语义从
  `task_runner.py` 抽成纯投影——tri-state freshness（`fresh_pass` /
  `fresh_fail` / `unobserved`）、显式 provenance、proof 构建与确定性失败
  分类（`product_failure` / `environment_failure` /
  `verification_unavailable` / `provider_failure` / `unknown`）。
  TaskRunner 只负责收集事实和接线，不再解释 completion。roadmap 点名的
  legacy 债务已偿还：隐式 `checks_passed` 继承拆成
  `stance = fresh_pass / fresh_fail / inherited_pass / unverified` 与
  `source = local_run / checkpoint / none`。inherited pass（checkpoint 恢复
  或 review 前窄绿色规则）保持 receipt 绿色，但 proof 记录
  `inherited_verification_not_fresh` limitation——它永远不再是本轮的
  clean verification fact；模型自报 pass 而无本地观察则干脆不是事实。
- Verified Completion Enforcement：writer 对已改动代码声称 done 时由
  completion proof 决定。`complete` 放行 done；docs-only 与 inherited-green
  继续允许为诚实的 `complete_with_limitations`；`failed` / `blocked` proof
  会以显式停止原因（`unobserved` / `max_repair_rounds` /
  `turn_budget_exhausted` / `environment_failure` / `provider_failure` /
  `repair_context_unavailable` / `repair_not_admitted`）阻止 done，而不是
  假装完成。
  未观察的 check 不是失败、也不是 repair 候选："没验证"意味着停下，
  不是"去修点什么"。
- Repair Context Admission v1：对且仅对一个 bounded round，
  `failed + product_failure` 的 proof 把最小失败事实包送回同一个 writer，
  全程走 0.4.8 链路——`ContextSource` -> profile allow-list（仅
  coding_writer）-> `ContextEpoch` -> `PromptEnvelope`。事实包只陈述观察
  到的事实（failed requirement、failure class、changed files、command/exit、
  截断且过密审的输出尾部、refs），绝不包含修复指令；并明确"未观察不等于
  失败"。turn 预算沿用原 run 不重置；receipt、ledger、project facts 与用户
  可见事件只由最终 outcome 驱动一次。
- 新增 `codey/completion_repair_context.py`：projection leaf，只消费已经
  生成的 proof payload（不 import completion_contract——completion 语义
  只有一个 owner），产出提示文本 + digest-only trace payload。`minimal`
  detail 用于区分"proof enforcement"与"信息量上下文"贡献的 A/B 对照臂。
- RunTrace 新增一个 bounded manifest section：
  `record_completion_repair_context(payload, *, epoch_id)` 与 0.4.12 的
  continuity 合同一致——必填合法 `ctx_epoch:<16 hex>`、绑定 outbound
  provider-send bytes、按 digest 去重，只落 counts/classes/reason codes；
  raw 失败文本没有字段可存。admission row 在 `agent.run` 的 send boundary
  落账：assembled ≠ admitted ≠ recorded 由构造保证。
- RunTrace protocol telemetry（P0a，trace-only）：新增有界的
  `protocol_telemetry` manifest 区块，按 phase 记录 JSON 工具协议事实——
  codec 身份（`json` / `research_json`）与 model/runtime tool-contract
  hash、按 kind 的协议错误计数、protocol repair prompt 计数、以及哪些
  provider turn 解析出可执行 plan（`first_valid_turn`、有界
  `valid_turns`）。未知工具只落 digest 加可选的安全短标识符；raw prompt、
  reply、error 没有字段可存。四个 recorder 方法
  （`record_protocol_codec` / `record_protocol_error` /
  `record_protocol_repair_prompt` / `record_protocol_valid_turn`）接入
  coding writer 循环与 research runner；没有任何行为读取它们——release
  A/B 更可解释，而运行时风险为零。
- Capability registry：新增 `completion_repair_context` capability
  （model-visible、fail-closed、canonical input `completion_contract`），
  并新增 `live_ab` release gate；`completion_contract` 保持 trace/data-only。
  context source key 仅 coding_writer 开放。
- 无 RepairManager / CompletionManager / critic / scheduler / 新工具。
  repair loop 由具名停止条件与 `MAX_COMPLETION_REPAIR_ROUNDS = 1` 约束；
  架构测试锁死 projection leaf 边界、闭合的 payload 词表以及 manager 层
  缺席。A/B 四臂（`control_done` / `proof_only_block` / `repair_context` /
  `repair_context_minimal`）通过唯一常量 `COMPLETION_ENFORCEMENT_MODE`
  在 `tests/manual/completion_enforcement_ab.py` 中切换；生产默认 `repair`。
- Enforcement 加固（发布前评审修复，六项全部结构化关闭、无兼容回退）：
  - repair round 不可能再物理超出 turn 预算：初始 writer 用满
    `max_turns` 且 proof 仍失败时，run 以新的显式停止原因
    `turn_budget_exhausted` 诚实 blocked，而不是多发一个越界 turn 再把
    展示 turns 截回去；repair 的 `turn_budget` 就是共享的剩余预算，总和
    永远不超过 `max_turns`。
  - 失败分类读取 decisive check 的有界输出尾部：非零 exit 但输出指名执行
    环境（缺依赖或缺工具如 `No module named pytest`、依赖网络的测试、测试
    设施崩溃）时，按行首锚定、带 reason code 的闭合签名词表
    （`ENVIRONMENT_FAILURE_SIGNATURES`，五个 reason 组）判为
    `environment_failure`——剥掉 runner 横幅与小写工具名头部后，签名必须
    位于诊断行的行首；每次匹配都给出 reason code 与决定性签名
    （`match_environment_failure`），live A/B 发现误判时是加一个测试加一个
    reason code，而不是把分类器变聪明。仅引用这些词的断言差异
    （`E   AssertionError: cannot find module`、
    `assert 'connection refused' == 'connected'`）仍判为 product failure，
    并有 negative tests 锁定该边界。
  - repair admission 必须有安全的 decisive check facts：当全部 decisive
    fact 为空或被密审筛掉（新拒绝原因 `refused_no_safe_check_facts`）时，
    projection 拒绝 admit 任何文本，TaskRunner 以
    `repair_context_unavailable` 阻止——与"没有安全的有界失败事实"合同
    一致，不再 admit 一段描述未观察 check 的提示。
  - 有改动但变更清单为空的 run 仍在 enforcement 范围内：changes 收集产生
    不了可用结论而本地观察到真实 edit 时，按观察到的 edit 证据划定
    enforcement scope，而不是让改过代码的 run 以未验证 done 通过。实测
    净空 diff——模型自己还原了编辑——本身就是结论：run 保持诚实的未改动
    receipt、留在 scope 之外，还原不会被当成未验证 done 阻止。
  - blocked 停止词表不再借用没花掉的 repair 预算：failed proof 而没有
    admit repair round 时（`proof_only_block` A/B 臂，或失败类别不在
    repair 候选规则内），以新的显式停止原因 `repair_not_admitted`
    阻止；`max_repair_rounds` 现在只表示"repair round 真的跑过且验证仍未
    通过"，A/B 结果解释不再歧义。
  - 普通 continuation 路径现在字面经过 `PromptEnvelope`：follow-up 请求与
    repair-facts section 都是 envelope section 并绑定 outbound send epoch，
    字节完全一致——每次 repair admission 都可证明走了与 fresh intro 相同
    的组装结构。
- Writer failover 不再在 runner 上留下已关闭的 provider：关闭现在是"关闭并
  清引用"一步完成，canary 失败撞上 switch 预算时不会把死 provider 留给同一
  实例的 Review-repair 复用（否则会跳过 reconnect、烧掉一次注定失败的尝试）。
- Adapter repair 对空候选 fail-closed：`validate_candidate` 以显式错误码
  `repair_candidate_no_changes` 拒绝无差异 diff，`{"files":[]}` 不再能作为
  "成功修复"安装、污染修复成功率指标，或让 provider 白走一次 override worker。
- Adapter repair sandbox 只 materialize 修复面，不再整个 repo 复制两份：
  `codey` 包（override installer 要复制、provider 单测要导入的全部）、
  `pyproject.toml`（保持 ruff 配置一致），加上该 provider 的只读测试文件。
  `reference-projects/`、docs、fixtures、工具脚本不再进入沙箱。
- Protocol telemetry 把 `repair_prompt_counts` 绑定到真实发送：writer 循环
  与 research runner 都在 terminal-stagnation / max-protocol-errors 检查通过
  之后才记录 repair prompt，协议失败终止的 run 不再虚报一条没发过的修复提示。
- `prompt_safety` 不再把普通路径误判为密钥：path-like token
  （`src/main/java/util/ArrayList.java`、`C:/Users/alienware/.codey/state.json`）
  只在高熵分支内豁免；显式 secret marker（包括路径段里的 marker）与 secret
  shape 仍然拦截。
- 密钥 shape 覆盖补齐常见 provider 前缀：AWS access key id（`AKIA…`，区分
  大小写、带边界）、GitHub fine-grained PAT（`github_pat_…`）、Stripe
  live/test key（`sk_live_`、`rk_live_`、`sk_test_`、`rk_test_`）。裸的 40 位
  AWS-secret 形状值刻意不做纯 shape 拦截——误伤太高——改为依赖 marker 词
  相邻上下文命中。
- Shell risk 说明覆盖更多用户真实会批准的命令：`uv add`、`go get`、
  `cargo add`、`deno install` 与任意 `npx <pkg>` 归类为 dependency install；
  `irm` / `Invoke-RestMethod` 归类为 external source；`cmd /k` 与 `cmd /c`
  一样展开。只影响审批说明文案，不影响授权判定。
- 发布验证保持显式：GitHub CI 会在 push、pull request 和手动 dispatch
  时运行；本地发布检查在 README 中记录为直接运行 `ruff`、`pytest` 和
  completion-enforcement self-test，不再提供仓库 hook 或自定义 wrapper。
- 密钥检测回归单一 owner：secret marker、provider key shape 与高熵启发式
  （含 path-like 豁免）统一由 `redaction.py` 拥有。`prompt_safety` 与 Ghost
  signal schema 复用同一套判定，不再各自维护分叉副本——AWS / GitHub-PAT /
  Stripe shape 现在在 prompt 可见检查里同样拦截；普通源码路径
  （`src/main/java/util/ArrayList.java`）不再被 Ghost work item / signal
  误拒（此前 path-like 豁免在 Ghost 路径从未生效）。
- Adapter repair 不再越出自己的错误边界：sandbox 创建移入受保护区域，
  只读引用文件缺失（例如打包安装环境没有 `tests/`）或 `source_root` 损坏时，
  返回有界的 `AdapterRepairResult`、记录 `adapter_repair_error` journal、
  并始终清理临时目录——此前直接抛裸 `FileNotFoundError`，不写 journal 且
  泄漏沙箱目录。
- Repair sandbox 引用文件 fail-closed 校验：空路径、绝对路径、盘符/rooted
  路径与 `..` 上溯路径在触碰文件系统前一律拒绝；每次复制都在 source 与
  destination 双侧复查 containment，坏的引用路径不可能把文件 materialize 到
  源码树或沙箱之外。
- prompt/模型可见文本的密钥筛只有一个具名入口：
  `redaction.looks_prompt_visible_secret()`（marker | provider key shape |
  高熵）。旧的 marker+shape 版 `looks_sensitive_signal` 已删除，调用方不
  可能再忘掉熵分支：执行证据、repair context、run trace、research 边界全部
  走同一入口——失败 check 的输出尾部或命令行里出现无 marker 的纯高熵
  token 时，现在会被丢弃并记 `repair_output_line_screened` /
  `repair_check_command_screened`，不再进入模型可见上下文。
- Writer telemetry 补记终止前的 no-JSON 回复：空 JSON 对象（无 calls/
  control）在 stagnation 返回之前先记 `no_json` 观测——协议错误观测数与真实
  发送保持 1:1（repair prompt 仍只统计真实发出的 nudge）。
- Adapter repair sandbox 一律拒绝 symlink：复制的 `codey` 包树、
  `pyproject.toml` 与引用文件中出现符号链接时直接拒绝，不再在复制时跟随，
  堵上"树内链接指向仓库外 → 目标内容被复制进沙箱"的残余边界。

## 0.4.12 - Ghost Research Continuity + Topic Planner v1

- 新增 `codey/research/topic_continuity.py`：stdlib-only 的纯 read model，
  把有界的本地事实（结构化 research-interest hints、Ghost continuity 选中
  items、evidence ledger 的旧 claim refs）投影成一小段模型可见提示文本 +
  一份 digest-only payload。continuity 可以重新定位旧 refs、提示需要复查
  什么，但不能创造事实：所有输出类型都不携带 evidence 引用字段，且每条
  prior claim ref 永久 stale（`prior_claim_needs_recheck`）。候选问题是
  确定性、去重、受预算约束的 leads——不是答案，更不会自动执行研究。
- Research 通过一个新的 context source key `research_topic_continuity`
  接收 continuity，仅由 research profile 放行（chat 侧的 `ghost_directive`
  / `ghost_continuity` source 仍然被排除）。admission 走共享链路：
  `ContextSource` -> profile allow-list ->
  `render_context_sources_with_metadata()` -> prompt envelope section。
  intro rows 在真实 provider-send 边界投影：controller 追加 action block 后
  字节才定稿，因此组装 sections、admitted source rows
  （`record_context_sources(..., epoch_id=...)`）和 outbound prompt 都绑定
  到按实际发送字节计算的同一个内容寻址 epoch；从未发送的 intro 不产生任何
  row。发送前的 `research_request` prompt-section row 已删除：它复制了模型
  可见的 `research_question` section 却从未共享其 provider-turn epoch；现在
  所有 research prompt-section row 都携带 sent-bytes epoch。
  section 文案直说 "not evidence ... re-check ... do not cite"，Codey 自己
  的 framing 行不出现 Ghost / Work Queue / Concept Graph /
  Memory 内部词；follow-up 材料继续走独立的
  `research_iteration_context`。空投影或门禁关闭时渲染为空，baseline
  intro 逐字节不变。
- TaskRunner 是减负而不是增重：`_run_research_pipeline` 拆出两个 helper——
  `_build_research_topic_continuity`（profile 门禁 -> 经 knowledge 层新增的
  `candidate_to_topic_hint` 取 interest hints -> bounded Ghost items ->
  ledger claim refs -> projection；任何异常 fail-open 回空 baseline，同时在
  run trace 留下一条有界 `warn` reason code）和 `_build_research_context`
  （组装 `ResearchContext`）。没有新增 TopicManager / TopicStore /
  continuity runtime，research 模块也不 import Ghost 运行时。
- Trace row 是真实落地的：`RunTraceRecorder.record_research_topic_continuity`
  把一个有界的 `research_topic_continuity` manifest section 写进 run trace，
  以内容 digest 为去重和完整性锚点。row 只含 refs、counts、reason codes、
  warnings 和 digest——没有原始提示文本字段，prompt-lab 材料无法泄入
  RunTrace 或 EvidenceLedger。claim ref 超过 16 条上限时先计数再截断，
  `truncated` 标记如实上报。admission 是结构性闭环的：必填的 `epoch_id`
  没有默认值，且必须是格式合法的 `ctx_epoch:<16 hex>` ref——空值或畸形值
  fail closed，不写 row、不污染 dedupe key，因此 admitted row 不可能脱离
  send-boundary 绑定而存在；projection sink（`RunTraceResearchSink`）不暴露
  continuity writer——唯一写入路径是 runner 的门禁加
  `record_research_topic_continuity(..., epoch_id=...)`
  对实际外发字节的绑定。发布口径因此天然诚实：row 证明的是"哪些内容被绑定到
  外发 provider-send attempt 字节"，而不是"模型确实处理了它们"。
- 新增 manual harness `tests/manual/ghost_research_continuity_ab.py`：两臂
  使用完全相同的种子状态，只切换 admission 门禁。所有 provider（真实或
  stub）都包一层 `TracingProvider`，send/reply 计数不再依赖 provider 自身
  实现；live 行按 `provider_send_error`、`native_search_stall_suspected`
  （send 超时或有 send 无 reply——属于 provider/原生网页搜索诊断，不算
  planner 质量）、`planner_quality:<stop_reason>` 三类归因。live 运行通过
  `ABJournalWriter` 记录 journal；`--transcript-mode digest-only|archive|off`
  只控制 manual 层的 transcript 保留策略。harness 现在会把实际选择的
  provider id 传进生产 `TaskRequest`，并在矩阵结束时写 terminal
  `run_complete` journal event，因此 live smoke 的 provider 归因和 manifest
  状态都对应真实执行的 run。
- 验证：架构测试把 topic_continuity 锁成无 I/O leaf，并锁死整个 research
  栈不得 import Ghost；capability registry、permission profiles、runner/
  pipeline 转发、TaskRunner admission 均有 deterministic 测试，外加 harness
  的 pytest 包装测试。
- 同周期加固批次：
  - shell approval 续跑不再吞掉用户 Stop，而且守卫是原子的：
    ``reserve_run(abort_if_stopped=True)`` 在同一把锁内复查 stop flag，
    封住"外部检查之后、占位之前落下的 Stop 被清掉"的竞态。
  - ``/api/new_chat`` 与 ``/api/changes/restore`` 在同 session/project 有
    run 运行时返回 409；restore 比较解析后的路径，且空闲服务器永不误拦
    （state 会保留最近一次项目，属正常现象）。
  - 新增共享的 `codey/ghost/numbers.py`，给所有 Ghost store 统一有限
    unit-float 契约：``bool``、NaN、inf、越界要么 fail closed
    （``coerce_unit_float``），要么确定性 clamp（``clamp_unit_float``）。
    schema/gate/inbox/router/hebbian/affinity/work_queue 全部接入——此前
    NaN confidence 能穿过 router 仅做范围比较的 clamp。
  - Ghost work 手动重新入队会重置 ``retry_count``：达到 MAX_WORK_RETRIES
    被阻塞的条目可以再次被认领，不再永久卡死。
  - StepFun 主路径保留已验证的 newest-first DOM 读取；evaluate 失败时的
    fallback 重写为 provider 本地实现（仅可见节点、从头正向扫描），因为
    通用 ``locate_response()`` 从尾部反向扫，在 newest-first DOM 上会读到
    旧回复。fallback 是两步阶梯，保留主路径的 ``.reason-render-ext``
    过滤：先试简化版字符串参数 JS，再退化到纯 locator 扫描——读取与
    response 计数都走同一阶梯，降级时的 baseline 不会被可见的 reasoning
    节点抬高。
  - override worker 每个 provider 使用稳定专属的浏览器 profile，不再
    用第二个 CDP 端口挂用户默认 profile；父端 worker 把子进程 stderr 抽进
    有界 tail，启动崩溃可诊断；子进程入口 ``--profile`` 必填（缺失直接
    fail closed，不再回落）；self-repair helper 同样改用
    ``state_home/self-repair/<provider>`` 隔离 profile——manual live smoke
    已接线并在文档写明隔离 profile 需要一次手动登录。
  - 五个 web provider wrapper 共享一份薄 send/new_chat 管道
    （`codey/providers/web_driver.py`）：外层 deadline 覆盖
    ``response_timeout + grace + margin``，让 driver 自己等完；到点未归则
    归类为 ``response_missing``，且走标准 capture，带完整现场诊断
    （url/title/stage/facts）。
  - Research 严谨性：超过 360 字展示上限的 evidence excerpt 在 proof
    locator 侧保持精确匹配文本，公共 payload 边界
    （`EvidenceItem.to_dict` / ``evidence_payload``）裁剪为展示形态——
    UI session state 不再存无界 excerpt 文本。删除单来源 citation 自动
    推断——正文里解释不了的 ``[n]`` 一律走编译失败进 repair，不再静默
    改写到唯一来源。
- 后续延后项：provider profile 增加 response_order 元数据并让通用
  locate_response 按 profile 决定扫描方向；抽取 ghost/store_common.jsonl
  基础件（hebbian/affinity 共用，本轮已完成第一片 numbers.py）。

## 0.4.11 - 评测脊柱：回归门 + 纵向研究 harness + comparison benchmark

- 新增 `codey/research/regression_gate.py`：把 Evidence Runtime snapshot、
  ResearchProofReview、Research Brief、Impact Contract、ReviewFinding、
  PlannerGap、Reproducibility Capsule、CompletionProof 和 pipeline summary
  串成一个端到端可回归测试的 read model。输出只有有界 metrics、布尔
  observables、gate verdict、reason codes 和 bounded refs；raw prompt、
  reply、transcript、网页正文按构造无法进入报告。它只衡量、不拦截：
  false completion 只计数（`false_completion_candidate`），真正阻止 `done`
  留给 0.4.13。未知 expectation key 一律 fail closed。架构测试锁死该模块
  projection-only（无 I/O、无 provider、无 journal），且不进入 research
  package 的 eager 导出面。
- 新增冻结基准套件 `tests/fixtures/research_benchmark/`：六个固定 case
  （stale 注入、冲突来源、unsupported claim 注入、本地 CSV/PDF 分析、OSS
  生态变化、论文进展）按 development / held-out 拆分，附 rubric 权重和
  hard gates；`lock.json` 记录全部文件 sha256。配套离线校验器
  `tests/manual/research_benchmark_suite.py` 校验 split 完整性、fixture 路径
  containment（逃逸即失败）、rubric 权重求和为 1、observable/criterion 词表
  对齐 regression gate、lock hash 一致；`--update-lock` 是有意变更 fixture 的
  唯一显式通道。case payload 里禁止出现 prompt/transcript/webpage 等键。
- 新增纵向研究 harness `tests/manual/longitudinal_research_harness_ab.py`
  （默认确定性、无网络）：同一主题多轮研究跑完整生产投影栈，验证旧 claim
  跨轮内容寻址不变（可重定位）、stale source 在修订结论生效前被标记、新
  evidence 修正旧结论、注入的 unsupported claim 在 brief 里可见但永远进不了
  implementation constraints、冲突证据生成 finding 与 planner gap、失败的
  AnalysisRun 永远不会被报告成已复现（诚实性门：对失败 run 期待
  reproducible 必须判 FAIL）。
- 新增 comparison benchmark `tests/manual/research_comparison_benchmark_ab.py`：
  三臂 deterministic 对照（无结构 baseline 报告 / OpenScience-style fixture /
  Codey evidence loop），用冻结 rubric 加权打分。措辞由代码强制：没有真实
  head-to-head artifact 时 summary 只能写 "OpenScience-style regression
  passed"；`--openscience-artifact` + `--claim-superiority` 同时给出才允许
  "surpassed OpenScience"，并记录 artifact digest。
- 抽出 manual A/B 共用层 `tests/manual/ab_harness_common.py`：合并
  `research_to_code_ab.py` 与 `bounded_research_planner_ab.py` 各自维护的
  TracingProvider（journal 包装、计数、错误记录）、interleaved arm schedule、
  complete-matrix gate、原子 JSON 写入、带 provider 身份守卫的断点续跑
  payload、journal 目录推导、fixture search provider 及其 URL policy 旁路。
  两个既有 harness 迁移到共用层后行为不变（原测试与 self-test 全部通过，
  r2c 保留 `TracingProvider` 兼容别名）。生产代码 import manual 层被新增
  架构测试全面禁止。
- 不改生产行为：本版不改 prompt、tool result、Router/fallback、permission、
  UI/SSE、Research 默认路径或 done enforcement。按 roadmap A/B 规则，
  projection/harness-only 版本不做 live provider A/B。
- 最终发布验证补充了 deterministic gates 和有限的 Qwen live smoke。
  `research_to_code_ab.py` 在 Qwen 上通过：projection arm 保持 success /
  check 行为，同时去掉 handoff 里的 raw excerpt、related-note id 和 trap
  conclusion 噪音。`bounded_research_planner_ab.py` 暴露的是 provider 状态
  smoke 问题：一轮 paired Qwen run 完成 baseline row 后，planner row 在
  第一次 send 后没有收到模型回复，Qwen Studio 仍停在原生网页搜索 UI；随后
  planner-only 重跑完成并提升 fixture score。新增的 longitudinal 和
  comparison 脚本在 0.4.11 仍是 deterministic-only，所以这只记录为诊断性
  provider smoke，不是统计 A/B，也不是 OpenScience head-to-head 证据。
- Review 加固（0.4.11 提交后审阅修复）：
  - comparison benchmark 的 superiority 措辞门禁从"存在任意文件即解锁"升级为
    结构化 schema 门禁：head-to-head artifact 必须是 JSON 且包含 roadmap
    要求的全部元数据（双方 version/commit、provider/model、任务输入、运行
    日期、结果来源、评分 rubric），字段非空有界；digest-only 包装、损坏
    JSON、非对象 payload 或缺字段的 artifact 一律 fail closed，CLI 直接
    退出非零，summary 记录 `metadata` 与 `errors`。validity 以 payload 本身
    为唯一事实来源，手拼的 digest 包装无法解锁。
  - superiority 门禁进一步从"元数据存在"升级到"结果支持"：artifact 必须带
    bounded 结果字段（`winner` ∈ {codey, openscience, tie}、
    `strictly_better_metric_count` ≥ roadmap 阈值 4、
    `regression_gates_passed: true`），且只有这些结果字段真正支持时才允许
    "surpassed OpenScience"；元数据完整但 winner 为 openscience/tie、严格
    更优指标不足、或 gates 未全过的记录一律锁定。summary 新增
    `supports_superiority` 并把结果字段写进 metadata；`openscience_claim`
    改为反映 verdict——gate 未过的 summary 不再说 "passed"。所有文本字段
    长度上限与 task_inputs 数量/长度上限进入校验本体，不再只在输出截断；
    目录等不可读路径返回 `artifact_unreadable_file` 而不是抛异常。
  - schema validator 边角卫生（第三轮审阅）：`winner` 先做类型检查再做
    集合 membership，数组/对象等 unhashable JSON 值返回
    `artifact_bad:winner` 而不是 TypeError 崩溃；错误列表真正 bounded——
    达到上限后只追加一次 `artifact_errors_truncated` 并停止记录；invalid
    artifact 的 summary errors 改为与 validity 同源（从 payload 重新推导，
    手拼包装不再出现 `errors: []` 却提示 "see errors"，无 payload 时才用
    loader 记录的 unreadable 原因，两者皆缺则 `artifact_unverified`）；
    summary 新增 `codey_commit_alignment`（artifact commit vs 当前 HEAD 的
    展示性对齐信息，不匹配不使既有记录失效，只如实显示）。
  - superiority 绑定冻结 rubric 并修掉剩余审计/环境边角（第四轮审阅）：
    artifact 的 `rubric` 必须等于当前 suite 的 frozen rubric 名
    （`research_benchmark_v1`），外来 rubric 的记录仍是诚实有效记录但无法
    解锁 "surpassed OpenScience"；metadata 只过滤空字符串/空列表，
    `winner="tie"`、`strictly_better_metric_count=0`、
    `regression_gates_passed=False` 这些最能解释"不支持 superiority"的字段
    不再被丢掉；`current_codey_commit()` 固定以仓库根为 cwd 运行 git，
    从任意目录调用都能解析当前 commit。
  - rubric 双因子绑定 + 纵向 fixture 语义修正（第五轮审阅）：
    - superiority 在 rubric 名之外新增机器校验因子 `rubric_digest`，取值
      直接来自冻结套件 lock.json 里 `rubric.json` 的 sha256 条目（复用同一
      hash 体系，不另起炉灶）。name/digest 任一缺失或不匹配：artifact 仍是
      valid 记录，但不能解锁 "surpassed OpenScience"。metadata 输出两个
      因子。
    - comparison benchmark 的 matrix gate 改为 exact matrix：每个 arm 恰好
      出现一次；此前 dict 折叠会让重复 arm 静默覆盖后仍判 complete。
    - longitudinal stale fixture 对齐生产对象模型的 content-addressed
      claim_id 语义：旧 stable-v2 结论跨轮保持自己的 id，stable-v3 修正以
      新 id 作为独立 claim 进入，并用显式 refutes relation 指向被取代的
      evidence——验证的是"旧 claim 可重定位 + 新 claim 以独立身份修正旧结
      论"，而不是"同一语义槽位复用 id"。
  - handoff 约束无冲突化 + 审计可见性（第六轮审阅）：stale fixture 进一步
    对齐生产语义——R2 记录只陈述当前结论（stable-v3），不再把被取代的
    stable-v2 复述成同记录内的第二个 evidence_backed claim；否则 brief 会
    同时产出两条互相冲突的 verified implementation constraints。superseded
    结论改为靠内容寻址 id 跨轮可重定位，其 evidence 作为定位过的来源材料
    保留在 R2 中，修正关系用显式 refutes relation 表达。冻结 suite 的
    stale_claim_refresh case 补上 `conflicting_evidence_finding` 期望并重打
    lock；longitudinal summary 每轮显式输出 `review_ok`，避免把"projection
    regression passed"误读成"research proof quality passed"；comparison
    summary 的 `arms` 改为列表，重复 arm 在门禁失败后仍在展示层可见。
  - `regression_gate` 的 record anchor 改为经
    `normalize_runtime_ref(kind="research_record")` 校验：恶意或错误的
    mapping 无法把长文本塞进 refs-only payload；snapshot 锚点非法时回退到
    合法的 brief 锚点，两者皆非法则不产出报告。
  - `_source_stale_facts()` 不再先全量 materialize 再截断，直接把 iterable
    交给自带 bounded scan 的 `project_source_set()`，删除冗余上限常量。
  - 共用层 `TracingProvider` 的 timeout 语义与注释对齐为真 pass-through：
    未配置且未传参时按 `send(text)` / `new_chat()` 裸调用，纯 scripted
    provider 可直接工作；`close()` 仅在被包装 provider 真正可关闭时转发。

## 0.4.10 - 安全与完整性加固（review 加固）

- 本地 HTTP API 具备 DNS rebinding / 跨域防护：每个请求先校验 `Host` 头
  必须是回环绑定（显式 LAN 绑定时额外允许该绑定地址）；携带外部 `Origin`
  的 POST 一律 403，在任何 handler 逻辑之前拒绝。
- `/api/local_provider` 不再把已存凭据重放到不同 `base_url`：更换目标必须
  显式提供该目标的 key，rebinding/XSS 页面无法用一个请求窃取已保存 key。
  只有旧配置里明确记录了相同 `base_url` 时才可沿用旧 key；没有旧
  `base_url` 的孤立历史 key 会用空 key 探测，并在用户未显式提供新 key
  时被清掉。
- `/api/stop` 在同一把锁内过期所有 pending shell 批准并发出具否
  `shell_result` 事件：用户按下停止后，过期的 Allow 卡片不再能执行命令。
- UI 状态持久化不再丢研究数据，同时边界更窄：session 清洗器按前端实际
  `researchRuns` 形状做白名单保留，并与前端一致把 run 数 cap 到 32；
  `research` 只保存为现有 UI 布尔标记。message 清洗器保留 `toolKey` /
  `activity` / `pending`——重启不再清空研究历史，待批准工具卡片可完整往返。
- 修复 snapshot/untracked diff 的双倍空行：`keepends=True` 喂给
  `unified_diff(lineterm="")` 再 join 导致非 git 项目每行内容后多一空行；
  两处 diff 构建改用普通 `splitlines()` 并加 golden 断言。
- 用户源码写入改为原子且保留 EOL：`codey/atomic_io.py` 使用唯一同目录
  临时文件（`xb` 创建）+ fsync + `os.replace`，替换前复制已有文件 mode，
  保留 CRLF/LF 风格；接入 write/edit 工具路径与快照 restore，与工具契约
  宣称的 "written atomically" 一致，POSIX 上不会丢可执行位。若替换在
  继承只读目标 mode 后失败，会先把 temp chmod 回可写再清理，Windows 上
  不再留下 `.target.<uuid>.tmp`。
- 拆分同名双义的 digest 函数：`refs.digest_ref` 更名
  `refs.content_digest`（生产者：任意值 -> sha256 内容摘要）；
  `research.shape.digest_ref` 更名 `shape.valid_digest_ref`（校验器：
  仅当已是合法 sha256 ref 时返回原值）。全部调用点更新，旧名零残留。
- Evidence ledger 的完整性从"只盖 record 行"升级为"读取时验证完整 record
  capsule"：每条 record entry 携带规范化 JSON 的 `record_integrity` 摘要，
  覆盖 entry 本身（去掉该字段）以及它引用的 source/evidence/claim/
  assumption/relation map 行。load 时任一不匹配/缺失即整册 fail closed
  （`ledger_unavailable`）。缺少原始 `record_digest` 的记录在投影前直接
  拒绝，不再铸出空串摘要。append 时也会拒绝共享 map 的同 id 不同
  canonical 内容：新 record 若复用已有 evidence/claim/assumption/relation id
  但内容不同，或复用 source id 但身份字段（已知 final URL ref、host、
  content hash、content kind）不同，会以 `ledger_id_collision` 跳过，旧 ledger payload
  逐字节不变。合法重复抓取同一 source 不再因为 `retrieved_at`、
  `pages_read`、`truncated`、保守 quality hint 等观测字段变化被误判冲突；
  这些字段会确定性合并。
- 报告 section 边界加固：README 文档化的裸编号标题（`1. Conclusion`、
  `一、结论`）恢复识别；常用中文标题（`参考文献`、`风险`、`备注`、
  `方法`）加入别名表；`具体如下：` 这类节内引导行不再切断所属 section；
  未知的 markdown 标题进入被丢弃的 unknown 桶。
  Writer 可见 research handoff 现在把 Key conclusions 限定为
  Citation map 支撑的结论：结论必须通过共享 citation scanner 引用 sources
  里真实存在的编号；`[99]` 这类假 bracket citation 会降级；前排 uncited
  噪音不会挤掉后排真实 supported 结论；uncited 结论只会作为少量
  `[uncited]` limitations 附在真实 counterpoints 之后。`[1][2]` 紧贴引用、
  `[1 p.4]` PDF 页码引用和 `array[0] per [1]` 这类代码文本，都与 Research
  done gate 使用同一套解析规则。
- Research 投影边界从注释变成元数据：`CapabilitySpec` 新增
  `projection_audience` / `canonical_inputs` / `fail_mode` /
  `release_gate` 并在注册时校验（投影能力必须声明受众；behavior_input
  必须声明 canonical 输入能力；model_visible 投影必须声明发布门槛）。
  所有触发 spec 已注解；research 自有投影数量由测试设上限；架构测试禁止
  行为侧 research 模块反读 trace/UI 投影，并把 profile+source_trust 组合
  的导入点锁为零。
- 小项：嵌套 evidence profile merge 先展平并 cap 原子 "+" 段，再计算合并值
  （不再产生 `finance_legal+science` 这类伪组合名，也不会让第 5 个 atom
  泄漏进 profile 值）；RunTrace brief 投影 claim 行
  hash 前截断且只存 digest；`test_server.py` 加模块级守卫禁止真实
  provider tab（receipt/memory 两测试补齐双连接器 patch）；
  `tests/conftest.py` 针对 Windows 上 pytest atexit 清理
  `pytest-current` symlink 时偶发的 `PermissionError` 做测试侧 guard，
  不改变临时目录位置，且无关 PermissionError 会继续抛出；
  `tests/manual/research_to_code_ab.py` 结束时记录 `run_complete`，live
  journal manifest 会落到 `done` 或 `failed`，gate 也新增
  `projection_trap_not_in_key_conclusions` 结构性判据；
  `tests/test_work_checkpoint_flow.py` 隔离 post-task audit/consensus/
  advisors 副作用（~137s -> ~4s）；StepFun 提交加 GLM 式防双击；
  `task_runner` 所有 pre-start 失败路径都恢复先前取消事件；shell approval
  continuation 会短暂等待刚被 approval 打断的 run 释放单任务槽位，所以用户很快点击
  Allow 时命令只执行一次，且仍能续跑原任务；
  `context_epoch` 对被 clamp 的 admission 标记 truncated；重开 run ledger
  从现有文件同时续算字节预算和事件序号；knowledge 搜索用 SQLite
  `ESCAPE` 明确转义 LIKE 通配符；`Assumptions:` 成为不会污染结论的
  section 边界；架构测试禁止把 digest 生产者/校验器 import 成中性
  `_digest_ref`；hebbian 删除路径的 projection 写入与 reinforce 路径对称包异常。

## 0.4.10 - Domain Source Trust + Research Brief 投影

- 新增 `codey/research/domain_profiles.py`：证据标准 profile 是纯数据。
  `EvidenceProfile` 是一个小的期望向量（freshness、source quality
  threshold、primary source 偏好、counterevidence 要求、数据类结论是否
  需要本地分析、偏好/降权 source kinds、偏好 connector kinds）——它只回答
  "这类任务需要什么样的证据才更可信"，从不回答"结论对不对"。内置六个原子
  profile：general / finance / legal / market / science / software_research。
  交叉领域在运行时用 `merge_profiles` 组合：ranked 维度取更严格值，tuple
  维度取并集；组合数有上限（`MAX_MERGE_PROFILES=4`）并显式给截断警告，
  merged id 用 "+" 连接以区别于内置 id。没有组合 profile、没有继承、
  没有任何关键词域推断（unknown label 回落 `general` 并带
  `unknown_profile_label` 警告）。该模块是纯 stdlib leaf，由架构测试锁定：
  无 codey import、无 I/O。
- 新增 `codey/research/source_trust.py`：把"这个来源客观上是什么"投影成
  低维 class taxonomy（official / primary / peer_reviewed / preprint /
  dataset / filing / standard / repository / issue / release / news /
  secondary / forum / social / aggregator / unknown），只使用来源已携带的
  事实（host 后缀、声明的 quality level/kind/freshness）。不联网、不读
  页面正文、没有 URL pattern 穷举表（只有稳定 host 后缀规则），也绝不
  删除或过滤 evidence——消费方只能把投影变成 warning、preference 或
  threshold hint。原先内联在 `research/proof_quality.py` 的聚合
  source-trust 警告规则原样收编到这里作为唯一实现；proof review 输出
  逐字节不变，重复规则集消失（本版真实减债点之一）。`evaluate_against_
  profile` 把投影与质量下限合成有界 counts/warnings，低于下限的行只会
  得到警告、永远不会被移除。
- 新增 `codey/research/brief_projection.py`：refs-only research brief
  投影 + 显式 Research-to-Code impact contract。`ResearchBriefProjection`
  只承载验证过的 runtime refs、有界 claim 摘要（状态 + 文本 <=260 字符）、
  open questions、counts 和 warnings；raw synthesis 全文、网页正文和
  transcript 永远进不来。`ResearchImpactContract` 把受影响文件、verified
  implementation constraints、test 建议、risk notes、out-of-scope 条目与
  decision refs 分开，并由测试钉死一条硬边界：unsupported claim 一律降级
  进 risk notes，永远不能支撑 implementation constraint；affected file
  路径做逃逸校验；`test_suggestions` 只是 Writer 上下文，不授权任何工具。
  `render_handoff` 为未来消费方渲染短结构化 handoff。
- RunTrace 新增两个由新能力拥有的有界 section：`research_source_trust`
  （每来源 class/tier/freshness 行，cap 32）和 `research_brief_projections`
  （以 record 锚定的 brief payload，cap 8）。两者都对半截 payload fail
  closed、按 runtime ref kind 校验 refs、清洗 reason code、去重、追加
  截断警告，且永不保存 raw prompt、transcript 或输出正文。research
  pipeline 在最终 proof review 之后把两个投影与 findings/planner gaps
  一起记入 trace；它们始终是 trace sink 上的 audit-only read model，
  不能影响搜索、planner 行为、prompt、provider 选择、权限或 done 语义。
- 注册 metadata-only 能力 `research_source_trust`（provides
  evidence_profile/source_trust/brief 投影；consumes
  research_object_model + research_evidence_runtime + run_trace），并把两
  个新 trace section 加入 `KNOWN_TRACE_SECTIONS`。
- dry-run query planner 接受可选 `evidence_profile`，只能前置有界、经
  可用性过滤的 connector 偏好并带显式 `domain_profile_source_preference`
  reason code（score 0.92）；profile 里未知的 kind 会产生有界的
  `domain_profile_kind_unavailable` reason 而不是猜测。不传 profile 的
  调用方拿到的 plan 与 0.4.9 逐字节一致，proof-ok 短路依旧完全忽略偏好。
- `knowledge/brief.py` 减债：删除本地 heading 扫描解析器
  （`_extract_section_lines` / `_extract_sources_section`），brief 改用
  `codey/report_sections.py`——一个中立的 stdlib-only leaf，同时也是 report
  quality review 的 section 解析唯一 owner；knowledge 层不再向上依赖会级联
  加载 runner/browser/pipeline 的 research 包（由导入隔离测试锁定）。
  section 边界收紧：任意 markdown 标题或短冒号式标题都切换 section，未知
  标题的内容进入被丢弃的 unknown 桶——旧报告或自定义模板里的
  `风险:`/`方法:` 正文不再可能混进 Writer 的结论区。无界的 raw 报告摘录
  （"Synthesis excerpt"，最多 3600 字符 note 正文）不再进入 Writer
  handoff，related-note id 噪音也从 handoff 移除；剩余每一行都来自具名
  section，超长行一律截断、绝不静默丢弃。这会改变 Writer 可见的
  research context 文案，因此发布启用前必须先跑专用 live A/B（见下）。
- RunTrace 保持非 transcript：`research_brief_projections` 行不再携带
  claim 文本与 open questions——claim 行只留 claim_ref / status /
  evidence_count / text_digest，需要正文时回查 research record 自身的有界
  payload。模型可见 handoff 仍保留其短有界文本；只有审计侧改为 digest 优先。
  组合 profile 的 payload 保留 "+" 组合标记，不再清洗成酷似内置组合
  profile 的名字。
- 新增 `tests/manual/research_to_code_ab.py`：roadmap 要求的 Writer 可见
  handoff 变更发布门槛探针。两臂（0.4.9 风格 baseline 渲染 vs 结构化投影
  渲染）、同一 fixture 项目、同一 synthesis note 内容、同一 Writer 任务。
  两臂顺序按 repeat 交错，消除会话热身/顺序偏差。进程退出码即 gate 判定：
  projection 臂在任一 gate 指标（success、关键结论保留、陷阱 claim 误用、
  独立验证通过）上回退，或任何 row 出错，都判失败——"跑完没崩但结果差"
  是 gate 失败而不是通过。run matrix 本身也是 gate 的一部分：每个
  (case, repeat) 组合必须恰好有一条 baseline 和一条 projection row，
  不均衡或被截断的 run 无法掩盖回退。默认每次 prompt/reply 交互写入哈希链
  `ABJournalWriter` journal 并完整归档（`transcripts/<digest>.json`）供
  离线复盘；`--no-live-trace` 可关闭。transcript 仅属 manual 层材料，
  绝不进入 RunTrace/EvidenceLedger/生产证据链。scripted-provider
  self-test 让整个 harness 离线可跑（`--self-test`），评分/构建/gate/
  交错调度另有不消耗 provider 流量的单元测试。
- Groundwork 边界声明：`resolve_profile`、`evaluate_against_profile`、
  `ResearchImpactContract`、`render_handoff` 目前只被测试和 trace 记录消费，
  是确定性 API 地基。生产路径尚不选择或应用 evidence profile（按设计不做
  关键词/领域推断）；planner 只有在调用方显式传入时才感知 profile；在
  这些消费方带着各自门槛上线之前，没有任何用户可见行为变化。能力元数据
  与模块归属一一对应：`domain_evidence_profiles` / `research_source_trust`
  / `research_brief_projection` 是三个独立边界。
- source-trust 域名匹配端到端加固。域名表（gov/mil 后缀形态含 compound
  ccTLD、edu/ac.uk、dataset 数据仓库、news、blog、forum、social、preprint、
  peer-reviewed、repo、filing、standard）收进唯一的 stdlib 数据 leaf
  `codey/research/source_domains.py`，捕获期质量分类器
  （`ledger.classify_source_quality`）与信任投影共同消费——两层不再可能
  各自漂移，仿冒 URL（如 `sec.gov.evil.example`）在捕获时就只拿到普通
  web/secondary 戳，而不是先盖上 official 再绕过后缀表。投影侧再加纵深
  防御：声明的 `quality` kind 永远只能授予 middle/weak class；强 class 只
  由 host 的注册形态推导，即使被伪造 official/data 戳也投影成 unknown 而
  非 tier-3 信任。两层 lookalike 测试 + classify->project 端到端测试锁定。
- 畸形 hostname 全链路 fail closed。共享的 hostname 形态谓词
  （`refs.is_valid_hostname`：禁止空 label、连续点、裸单 label、非法字符）
  同时把守信任表（`.gov` / `evil..gov` / `.edu` 永远无法匹配后缀）和
  research URL 守卫：`check_fetch_url("https://.gov/x")` 在所有路径上返回
  denial reason "invalid URL host"，而不是让 resolver 的 UnicodeError
  中断 plan 预检。
- 强 `dataset` class 重新由 host 背书且可达：注册数据仓库（data.gov、
  data.nasa.gov、data.europa.eu、zenodo.org、figshare.com、kaggle.com、
  archive.ics.uci.edu）投影为 tier-3 dataset；单独声明的 data kind 仍然
  铸不出该 class——science/finance/market profile 偏好重新有真实语义，
  且不重开伪造漏洞。


## 0.4.9 - Research Contract Lite + Verified Completion Gate v1

- 新增 `codey/completion_contract.py`：Verified Completion Gate 的领域无关
  纯投影核心。`CompletionContract` / `CompletionCheck` / `CompletionProof`
  只承载状态、reason code 和有界 refs；硬门槛派生（任一 check fail ->
  failed，必跑未跑 -> blocked，pass + limitations ->
  complete_with_limitations，否则 complete），不做打分。一致性由原语自身
  保证：satisfied proof 永远不携带 blocked_reason；无有效 id 的 junk 输入
  fail closed 为空投影；空 checks 拒绝成 contract。v1 刻意不设独立
  Requirement 对象——requirement 与 check 在此阶段恒为 1:1，平行列表只是
  重复状态。
- 新增 `codey/research/contract.py`：把 `ResearchProofReview` 与其派生的
  ReviewFinding 投影成共享 contract/proof 形状。open critical finding 会
  阻止 clean complete；由于 critical finding 都是 hard proof failure 的
  投影，通过 proof review 的记录不可能产生 critical open finding，queued
  research 的完成结果与 0.4.8 逐项等价（不需要 A/B）。
- 收敛 `research/completion_gate.py`：对外契约逐字节不变（action、
  blocked_reason 字符串、proof_refs 组装全部保持），内部改为消费 contract
  投影；`_blocked_reason()` 这类 stringly 证据语义移入 research/contract.py
  统一维护，`safe_run_ref()` 上移到 completion_contract.py 作为 research 与
  coding proof 共享的领域中立 run-ref 清洗器。`ResearchCompletionDecision`
  新增可选 `proof` 字段供 trace 记录。
- RunTrace 新增 bounded `completion_proofs` section（proof row cap 8；
  每个 proof 的 check cap 与 `CompletionContract` 共用
  `MAX_COMPLETION_CHECKS`）：只存 refs/status/check summary/reason codes，
  finding/analysis/artifact refs 按 runtime ref 校验，未知
  domain/status/check 行 fail closed 丢弃，proof row 截断写 warning，并且
  不信任 raw mapping 里的 `satisfied`，统一从 `status` 派生一致性。
  raw mapping 边界也强制 contract 形状：没有有效 check 行的 proof 直接丢弃，
  `complete_with_limitations` 必须至少带一个有效 `limitation_refs`。payload
  不含任何 raw prompt / transcript / 输出正文。
- queued research completion 现在会把生成的 `CompletionProof` 在成功和
  blocked 两条路径都写进 RunTrace，不再只是把 `proof_refs` 写回 queue item。
  `complete_with_limitations` 不再全局视为 satisfied：只有 clean
  `status == "complete"` 才产生 `satisfied=True`，避免未来 enforcement 把
  受限完成或未本地观察的验证误当成 clean completion proof。
- Coding 侧 shadow completion proof：project run 结束后从既有本地事实
  （changed files、selected verification candidate、latest edit 后的 check
  结果、实际执行过的 AnalysisRun 记录）投影 proof 并写入 trace。本地验证
  新鲜度是显式三态——fresh_pass / fresh_fail / unobserved——read/search 也
  是 tool event，但绝不会被误记成"验证失败"；没跑或 stale 一律记为
  unobserved。unobserved 对 agent 自报的两个方向都保持诚实：报绿最多得到
  complete_with_limitations(verification_not_locally_observed)；而假值只
  会 blocked——`RunResult.checks_passed` 初始就是 False 且会被 edit 重置，
  缺少本地观察时绝不能升格成"验证过且失败"，failed 只保留给本地真实观察
  到的覆盖性失败。agent 自己上报的 checks 在 receipt 本地覆写之前捕获，
  proof 永远不会把覆写值当成模型的自述；docs-only change 得到
  complete_with_limitations(docs_only_change)；无匹配验证命令得到
  blocked(no_matching_verification_command)。模型自述"测试通过"永远不能成
  为本地证明。done/receipt/prompt/SSE 全部不变。
- Completion proof 引用 provenance 而不只是结论：analysis_run_refs 只挂
  决定性检查实际执行过的那次 run——fresh_pass 引用它通过的、fresh_fail 引
  用它失败的、unobserved 什么都不引。匹配通过 AnalysisRun 投影同源的
  project-relative path digest 做 cwd-aware 判定，monorepo 里同一命令在两
  个 package 下各引各的，绝不会串到兄弟目录；被 redact 的命令只在
  analysis_runs section 里保留 digest 溯源。
- 共享有界 ref 词表移出 research 命名空间，落成两个领域中立的 stdlib
  leaf：`codey/refs.py`（clip / identifier / bounded_refs / 各类 digest /
  stable_ref）与 `codey/redaction.py`（secret marker/shape/code 谓词）；
  `research/identity.py` 只留 URL/project/path 这些 research 特有 helper，
  基础词表改从 `codey/refs` 导入。不留兼容 shim：所有 importer 全部更新。
  架构测试锁定两个新 leaf 纯 stdlib，coding/research/未来 experiment 投影
  共用同一方言而无需跨域 import。
- Contract id 覆盖全部 payload 字段：finding/analysis-run/artifact/external
  refs 与 checks、limitations 一起进入哈希，任何一组 refs 不同都不会共享
  contract_id（proof_id 由它派生、RunTrace 按 proof_id 去重）。
- `task_runner` 顺手减债：`select_verification_candidate` +
  `check_covers_selected_candidate` 的求值收敛为单一位置，receipt 判定与
  shadow proof 共用同一份事实，不再各算一遍。roadmap 也把剩余的 receipt
  verification provenance 债务列为后续项：在 completion proof enforcement
  前，应把 legacy `checks_passed` 继承路径拆成显式 provenance 字段，而不是
  作为冷启动兼容继续保留。
- Capability registry 新增 metadata-only `completion_contract`
  （model_visible=False，trace_sections=("completion_proofs",)）；架构测试
  锁定两个新模块为 projection-only（禁运行时 import、禁 I/O token）。

## 0.4.8 - Safe Context Epoch + Capability Boundary v1

- 新增 `codey/context_epoch.py`：纯 stdlib leaf 投影模块（不 import 任何
  codey 模块、无 I/O，架构测试锁定）——`ContextEpoch` / `ContextAdmission`
  / `ContextSnapshot` 有界读模型、对外发 prompt 字节做 sha256 的
  content-addressed `ctx_epoch:<16hex>` epoch id、稳定的
  `context_source_ref()` 归一化，以及单一共享 admission 投影
  `admission_from_rendered_source()`：snapshot 构建器和 RunTrace 的
  context source 行都走它，生产和测试共享同一套 ref/digest 词汇表。
  只保存 digest/chars/budget/refs/capability_id/admission_reason，绝不保存
  raw prompt 或 source body。空 key 或不可用 key fail closed：不产生 ref、
  整行跳过，不会输出残缺的 `context_source:` 条目。
- Provenance 闭环：coding run 里每一条模型可见的行都绑定到真正发出的那个
  prompt 的 content-addressed epoch。`agent.project_intro()` 先渲染最终
  prompt，然后把它的 envelope sections、被准入的 context source 行（经新的
  `record_context_sources(..., epoch_id=...)` 绑定）与之后经
  `record_provider_send_prompt()` 记录的外发 prompt 盖上同一个 epoch id。
  工具结果轮的 `coding_current_context` 行先以"prepared、无 epoch"状态挂起，
  在发送时才绑定；若 conversation rollover 把 prompt 整体替换成新 intro，
  过期的 prepared 行会被丢弃，而不是算在一条从未发出的 prompt 头上。真实
  run 测试同时锁定 intro 轮与工具结果轮两条路径。既有 conversation
  rollover 内部 summary prompt 也会作为 digest-only
  `conversation_handoff_summary_prompt` provider-send 行记录，并带
  `capability_id="conversation_handoff"`，不再是隐藏的模型可见发送。chat
  模式外发带 `capability_id="chat_runner"` 并有独立的 payload 回归；
  `coding_request_context` 的 source refs 改经共享 `context_source_ref()`
  构造，生产代码保持单一 ref 词汇表。epoch id 标识的是 turn *内容*，不是
  编号的 provider 调用：相同字节的重复发送按设计共享同一 id 并在 trace 中
  去重；任何字节差异都会产生新 epoch。
- 共享 ContextSource 契约扩展：`ContextSource` /
  `RenderedContextSource` 新增可选 `capability_id` 与 `admission_reason`
  （默认空）。渲染顺序、预算裁剪、failure policy 与输出文本逐字节不变；
  agent.py 的九个 run-start source 改由一个小 `intro_source()` 工厂统一
  构造，不再逐字段重复九遍。
- Prompt envelope section 带上同样的三个可选字段（`epoch_id` /
  `admission_reason` / `capability_id`），render 与 fail-open trace sink
  原样透传；只有元数据存在时才会向 trace 追加这些 kwargs，旧 trace sink
  收到的关键字签名与之前完全一致。
- 新增共享入口 `record_provider_send_prompt()` 并删除九处手写的同一
  provider-send 块：`agent.py`×3、`server.py`×2、`task_runner.py`×1、
  research runner×1、consensus.py（`_trace_model_prompt` 委托）。现在每条
  外发 prompt 的 trace 都在同一个地方盖上 provider_send freshness、
  content-addressed epoch id（支持显式 `epoch_id=` 覆盖，调用方可传入已算好
  的 epoch）和固定的 `provider_turn_boundary` admission reason。prompt 文本、
  发送顺序与 provider 行为不变；同一个 helper 现在也包住 `agent.py` 与
  `task_runner.py` 里既有的 conversation handoff summary send。除既有
  byte-for-byte parity 测试外，新增的真实 run 元数据测试同时断言元数据确实落到
  记录的行上（这些测试在开发期间真的抓到过一个双重包装导致静默丢 trace 的问题）。
- Run Trace：`PromptSectionTrace` 新增可选 `epoch_id` / `admission_reason`
  / `capability_id`，有值才序列化——没有新元数据时 manifest payload 形状
  不变。prompt section 的 dedup key 纳入 epoch id：完全相同的重复照旧折叠，
  任何内容差异都会产生新 epoch 和新行。`record_context_sources()` 经共享
  admission 投影生成行并绑定到给定 epoch；source 自带的 admission_reason
  优先于调用方的 fallback 参数。
- Capability Registry v1 补全 roadmap 字段集：spec 现在声明
  `trace_sections`、`context_sources`、`evidence_producer`、
  `enabled_by_default`，并在构造时按新增的 `KNOWN_TRACE_SECTIONS` /
  `KNOWN_CONTEXT_SOURCES` allowlist 校验。补登记 0.4.7 模块
  （`research_evidence_runtime`、`research_review_finding`）与本版边界
  （`context_epoch`、`conversation_handoff`、`chat_runner`、
  `consensus_advisors`），并为既有 spec 补事实归属：agent_runner 拥有八个
  coding context sources，local_context
  拥有 ghost_directive/ghost_continuity，policy_guard 写 policy_decisions，
  object model/ledger/proof quality/query planner/finding 各自声明其投影
  落入的专用 trace section。chat 模式的外发 prompt 现在带
  `capability_id="chat_runner"`，rollover summary prompt 带
  `capability_id="conversation_handoff"`，不再是无主 provenance。新增架构测试：
  生产代码中出现的每个 `capability_id=` 字面量都必须是注册能力 id。
- 范围注记：不改 prompt 措辞、不改 context 顺序或预算、不改
  Router/fallback/权限、不让 finding/gap 影响 planner 行为、无插件加载器、
  无 skill 系统、无配置 UI、无新增模型可见能力。纯 metadata/trace 投影，
  按 roadmap A/B 规则本版不需要实机 A/B；一旦 findings 或 gaps 开始影响
  prompt、planner 行为或报告契约，A/B 即为强制项。

## 0.4.7 - Evidence Runtime + ReviewFinding Core v1

- 新增 `codey/research/evidence_runtime.py`：所有 research runtime ref 的唯一
  确定性校验入口（`source/evidence/claim/assumption/relation/research_record/
  research_proof/research_plan/analysis_run/artifact/artifact_version/
  review_finding/planner_gap:<16hex>` 加上有界 `run:` id），以及
  `snapshot_from_research_record()`——把 typed 或 mapping 的 ResearchRecord 连同
  proof review、analysis runs、artifact versions 投影成有界的
  `EvidenceRuntimeSnapshot` 读模型（只含验证过的 refs、digest、allow-list
  answer status 和计数，不含 raw 文本）；typed 与 mapping proof review 都会保留
  原 proof review 的 `question_digest`，不会制造新的 digest。
  这收掉了各模块重复的 ref 正则：artifact lineage 的 `is_valid_derived_ref()`
  改为委托共享 validator，并用显式窄 allowlist（`source/evidence/
  analysis_run/run`）保持接受/拒绝行为完全不变。
- 新增定位诊断：`_review_relations()` 在原有 hard-failure reason code 完全不变的
  基础上，同时产出 `ProofDiagnostic(reason_code, claim_ref, evidence_ref,
  source_ref, relation_ref)`；`ResearchProofReview` 通过新的 `diagnostics`
  字段和 `diagnostics_payload()` 暴露它们，输出前会再次通过 Evidence Runtime
  校验 refs。诊断刻意不进入 `to_payload()` / `to_trace_payload()`，也不影响
  `proof_ref`，既有 payload/trace 形状字节级不变。
- 新增 `codey/research/review_finding.py`（纯投影模块，无 runtime import）：
  稳定的 `ReviewFindingRecord`（`finding_id/kind/severity/status/target
  refs/reason_codes/addressed_by/confirmed_by`）、`PlannerGap` 和
  `ReviewFindingEvent`；v1 不带自由文本 `message` 字段。
  - `findings_from_proof_review(review, snapshot)` 把诊断加记录级 warning 投影成
    定位 finding；提供 snapshot 时，record 图之外的 ref 会被丢弃而不是被发明。
    kinds：`unsupported_claim` / `citation_mismatch` / `stale_source` /
    `overreach` / `missing_counterevidence`（另有 `failed_analysis_support`
    生产者 `failed_analysis_findings`；`contradictory_sources`、
    `source_conflict`、`qualified_support` 在真实生产者出现前只保留枚举值）。
  - `planner_gaps_from_findings()` 把可行动的 finding 映射为 gap kind
    （`followup_search` / `locator_verification` / `counterevidence_search` /
    `refresh_query` / `rerun_analysis`），是确定性的读模型，自己不排程任何事。
  - `apply_finding_events()` 实现 append-only 生命周期：
    `open -> addressed -> confirmed/rejected`。`confirmed` 要求 `verified_by`
    来自固定 allowlist（`deterministic_check`、`analysis_run`、
    `opened_source_evidence`、`reviewer_pass`）；模型自称“已修复”fail-closed。
  - 刻意不迁移 `codey.reviews.core.ReviewFinding` parser 对象；接入 code review
    finding 要等真实消费者出现。
- ResearchPipeline 现在只在 final proof review 之后做一次 finding 投影：
  final review -> EvidenceRuntimeSnapshot -> ReviewFindingRecord ->
  PlannerGap -> 只写 trace sink。planner 不消费 gaps，follow-up 搜索行为不变；
  投影失败 fail-open，不影响任务完成。
- Run Trace 新增两个有界 section：`research_review_findings`（上限 16）和
  `research_planner_gaps`（上限 16），只保存验证过的 refs、固定 allowlist 的
  `kind`/`gap_kind` 与 `severity`/`status`，以及有界 reason codes——不保存 raw
  claim 文本、网页正文、stdout/stderr、provider transcript 或自由文本 message。
  recorder 按 id 去重，非法形状或 taxonomy 值会在 recorder 边界静默丢弃或收敛，
  溢出保留最新并追加截断 warning。没有 finding 时 manifest 除两个空列表外形状不变。
- 架构测试现在把 Evidence Runtime 和 ReviewFinding 锁成 projection-only：
  禁止 browser/provider/tool_runtime/task_runner/server/managed_outputs/
  events/ghost/codey.reviews.core/journal import 和 I/O token；A/B journal 边界测试
  本来就 glob 全部 research 模块（含新模块）。
- 范围注记：无 model critic、无 prompt 变更、无工具结果变更、无 UI、无报告契约
  变更、无 graph database、无新增模型可见能力。纯 deterministic projection，
  按 roadmap 不需要实机 A/B；一旦 findings 开始影响 prompt、planner 行为或报告
  契约，就必须先走 0.4.6 journal 的小型 live A/B。

## 0.4.6 - A/B Observation Journal + Transcript Replay Cache v1

- 为 manual harness 新增共享 A/B 观测 journal（`tests/manual/ab_journal.py`）：
  单写者 append-only JSONL 事件流，每行 flush/fsync，带可验证的 sha256 hash chain；
  中断后尾部损坏可恢复；manifest 按 experiment/run/provider 身份 fail-closed；
  `completed_case_keys()` 支持断点续跑。
- Journal 身份改由事件本身强制：`verify_event_chain()` 会报告同一链内的
  experiment/run/provider 混写；即使 manifest 缺失、损坏或被替换，writer 打开时
  也会拒绝不同身份追加到既有 chain。
- Reader 校验现在会显式上报无法解析的行数（`mid_file`/`tail`），
  垃圾行不能再躲在看似干净的 chain 校验背后。
- 严格 JSON 持久化：facts 脱敏阶段丢弃非有限浮点，事件行序列化使用
  `allow_nan=False`，`events.jsonl` 不再可能出现 NaN/Infinity；文件中部出现
  无法解析的行时 writer 自动恢复改为拒绝写入，需显式
  `ABJournalReader.recover_tail()`。
- Provider observation facts 通过按 event type 明确声明的 typed schema 过界：
  `page_text`、`response_text`、`cookies` 这类未知字段在 value sanitization
  前就会被丢弃。URL、HTML 片段、cookie-ish 值、secret 形状的值、不透明对象与
  一般嵌套 mapping 一律脱敏或丢弃；只有嵌套 `provider_failure` 保留
  `kind`/`stage`（投影为 `provider_failure_kind` /
  `provider_failure_stage`）——raw provider error message 和页面 title
  不可能重新进入 journal。
- Harness 的 run_id 跟随最终的 provider 专属结果文件名
  （all-mode 改名后的 `output.stem`），单独恢复 `custom-deepseek.json` 时会
  复用 all-mode 运行创建的同一 journal 身份。
- 新增 `TranscriptReplayCache`：prompt/reply 默认只存 digest；显式 archive 模式
  才把内容寻址、有大小上限的 transcript 写入 `transcripts/<digest>.json`，
  仅用于 manual replay/scoring，并提供显式 `delete_transcript()` 和
  `prune_transcripts()` retention helper。
- `bounded_research_planner_ab.py` 与 `source_connector_ab.py` 迁移到共享 journal，
  删除各自重复的 LiveTrace 实现；trace 输出变为 `<stem>.trace/` 目录
  （`manifest.json`、`events.jsonl`、可选 `transcripts/`）。结果 JSON 形状不变，
  历史结果仍可读取。connector 的 case-start 调用已修正为新签名，两个 self-test
  现在重放完整 per-case 事件序列作为回归锁；两个 harness 也支持包模块方式执行
  （`python -m tests.manual.<harness>`）。`deep_research_core_ab.py` 的迁移推迟。
- 架构测试锁定层边界：生产层（run_trace/research/task_runner/server）不得 import
  journal；journal 不依赖生产编排层；transcript 不能进入 EvidenceLedger/ObjectModel。

## 0.4.5 - AnalysisRun + Reproducibility Capsule v1

- 新增 `AnalysisRun` 本地命令执行审计投影（`codey/research/analysis_run.py`）：
  project 模式的每次 `run` 工具执行现在会投影成一条有界、确定性的记录，
  包含 command digest、有界的显示用命令、cwd ref、exit code、started/finished 时间戳、
  duration、capture quality，以及 allow-list 的环境摘要 digest。
  不保存 raw stdout/stderr，不做 script/dependency fingerprint，也不 import runtime 层：
  投影只消费已归一化的 metadata mapping。
- 新增最小 Artifact lineage（`codey/research/artifact_lineage.py`）：
  Managed Output handle 现在投影为稳定的内容寻址 `artifact:<16hex>` /
  `artifact_version:<16hex>` 引用，带 sha256、有界 size、固定 `text/plain` mime、
  来源 run id 和产出它的 analysis run。derived ref 按 Source/Evidence/AnalysisRun/Run
  前缀 allow-list 校验；坏 digest fail-open 为不产生 lineage 条目。
- 新增 Reproducibility Capsule 聚合（`codey/research/reproducibility.py`）：
  每个 run 一份有界快照，聚合 analysis runs、已捕获 artifact 版本、环境 digest，
  以及诚实的 reproduction status（`no_analysis_runs` / `output_captured` /
  `output_not_captured` / `failed`），绝不声称超出 v1 可验证范围。Capsule 快照按 id
  替换而不是累积陈旧状态。
- Run Trace 新增三个有界审计 section：`analysis_runs`（cap 8）、`artifact_refs`
  （cap 16）、`reproducibility_capsules`（cap 8），带 generated-ref 校验、去重、
  截断 warning，且不保存 raw 输出。
- `run_command_raw()` 现在记录 audit-only timing（`started_at` / `finished_at` /
  `duration_ms`）；超时命令同样带 timing，因为进程确实启动过。字段仅通过
  `ToolOutcome.audit` 流转。模型可见 `model_text`、UI/SSE payload 形状和 managed
  output footer 字节级不变，包括 timeout 的 `ERROR:` 前缀，由 characterization 测试锁定。
- 按评审意见加固 AnalysisRun 投影：
  - `tool_id` 现在记录实际 UI/runtime tool instance id（`turn:index`），
    `tool_name` 单独记录 `run`，trace entry 可以直接对齐 UI payload。
  - `command_display` 在命中 secret-looking 信号时脱敏（置空并记
    `command_display_redacted` warning），与 ProjectFacts 拒绝持久化此类命令的口径一致；
    digest 始终是权威事实。`RunTrace.record_analysis_run()` 也会在 recorder 边界对直调者
    重复执行同样的显示命令脱敏。
  - 只有真实执行才成为 AnalysisRun 记录：没有执行 timing 的结果（policy deny、
    cwd 非法、command not found）不进 trace；timeout 作为诚实失败记录。
  - `duration_ms=0` 不再误报 `timing_unavailable`。
- Managed Output audit payload 现在携带 `stored_truncated`
  （`normalized_managed_output()` 透传），artifact lineage 因此能知道本地保存的输出
  本身是否被二次截断。
- derived lineage ref 从前缀校验收紧为形状校验：
  `source/evidence/analysis_run:<16hex>` 加 `run:<有界 id>`；URL 和自由文本 fail closed，
  投影模块和 `run_trace.record_artifact_refs()` 双侧生效。投影层只接受 list/tuple 形状的
  `derived_from`，recorder 也要求 `artifact_id` 与 `version_id` 都合法才记录 lineage。
- 候选选择中的字典序 tuple 排序替换为显式 `ResearchCandidateScore` dataclass，
  字段顺序即优先级顺序（proof-complete 支配、停止质量、先对题后覆盖、验证布尔位、
  missing 更少）。unsupported-claim 回归仍是打分前的硬约束。
- 收束 TaskRunner 中重复的 project tool-event 分支：
  project facts 记录、checkpoint edit/run 追踪和 AnalysisRun 投影现在共用一个
  `_handle_project_tool_event()` 缝隙，分支条件不变；投影失败 fail-open，不影响任务完成。
- 架构测试现在禁止 research/review/ghost 模块 import `codey.storage.managed_outputs`，
  并保持三个新投影模块纯净（不依赖 events/tool_runtime/task_runner/server）。
- v1 范围说明：Research 报告暂不引用 `analysis_run:<id>`；先记录内部支撑关系。
  让引用对模型可见会改变报告契约，需要留到后续版本做小型实机 A/B。

## 0.4.4 - Bounded Research Planner v1

- 实现 Staging 内存暂存隔离（`StagedKnowledgeStore` / `StagedKnowledgeChanges`）：
  follow-up 阶段笔记写入和关联在内存暂存缓冲中执行，支持完整 read-through 读穿透与补偿回滚（rollback）机制；
  候选方案被拒绝时零写入磁盘主知识库与 changes，主 store、`sources_read`、`created_ids` 零污染；
  `link()` 严格校验两端 note 存在性；仅当候选方案通过评测胜出时才批量持久化提交。
- 加固 staged note-link 语义与回滚：
  staged link 现在通过普通 `KnowledgeStore.link()` 同一层窄 resolver 解析 note 标题；
  staged commit 失败时会快照并恢复所有触及 staged/link endpoint note 的 SQLite link 边；
  `replace_links_touching()` 会在 API 内过滤 restore rows，只恢复确实触及指定 note ids 的 link；
  staging 阶段的 changes 跟踪收敛为纯 no-op facade，不再保留半使用状态。
- 将 `KnowledgeChanges.snapshot()` / `restore_snapshot()` 正式作为 staged commit rollback 边界：
  回滚不再触碰 `KnowledgeChanges` 私有字段，并会恢复完整的内存 change tracking 状态。
- evidence-only `knowledge_write` 收窄到最小参数面：
  只接受 `type`、`title`、`body`、`sources`、`evidence`；`sources` 必须是非空 URL list，
  `evidence` 必须是非空 evidence object list，且每条 evidence 必须显式使用 `source_url`；
  follow-up 模式下拒绝 `tags`、`relations`、`aliases`、`status`、自定义 id 等普通写入侧通道。
- 修复 deterministic merge 的 project metadata 保留：
  合并记录从当前 `ResearchTools.project` 重建 `project_ref`，与现代 Research record 的
  `basename/digest` project identity 保持一致，不再保留 legacy path shim。
- Pipeline Staging Commit 补偿回滚与异常安全护栏：
  在候选方案胜出提交（`commit_staged`）阶段增加异常保护与补偿回滚，若多 note 写入中途抛出磁盘满或底层 IO 错误，
  自动逆序清理本次已写入磁盘的 note 文件（针对已有 note 发生路径/folder 移动，彻底删除新路径文件并字节级还原旧路径文件内容与时间戳，统一使用 `content_hash_bytes` 保持算法收口一致）、
  从 SQLite 索引中清除对应条目（消除幽灵 index）并完整恢复 `KnowledgeChanges` 快照，平稳回退并保留 initial 成功结果，
  标记 `planner_stop_reason="followup_commit_error"`，防止增强路径异常影响已成功的初稿。
- 完善管道与 Trace 可观测性（`task_runner.py` / `pipeline.py` / `run_trace.py`）：
  不仅透传并持久化成功应用的 `fresh_source_count`、`new_evidence_count`、`final_evidence_count`（最终交付报告证据总数），还完整记录无论候选方案是否胜出均可审计的
  `attempted_fresh_source_count`、`attempted_new_evidence_count`，提供完整的 provider traffic 与尝试成本持久化审计透明度。

- 强化 Evidence Follow-up 与执行器边界保护（`evidence_followup.py` / `plan_executor.py`）：
  - 修复 follow-up prompt 与 schema 不一致，严格限制且必填 `type='fact'` 并在控制器中强制拦截缺失或非法类型；
  - 严格限制单轮单动作：模型返回多个 tool calls 时直接判定为 `invalid_tool_calls_count` 并拒绝，绝不静默忽略；
  - 增加严密的 Evidence 来源归属（Provenance）校验，强制要求 `evidence[].source_url` 必须属于当前 note 声明的 `sources` 列表；
  - 在 `PlanExecutor` 中对重定向目标 URL（`canonical_final`）建立预判与自动去重机制，杜绝别名导致的重复 fetch 与预算浪费；
  - 清理跨模块私有 helper 导入，导出公共 `source_from_opened`（加入 `__all__`）并删除 `done_finalizer` 中未使用的死代码。

- 强化确定性图谱合并器（`codey/research/record_merge.py`）：
  对结论（conclusion）、关键证据（evidence）、反证（counter）实现严格的 Evidence-Backed 引用校验与全段落修剪，
  彻底过滤未引用或包含未映射悬空编号（如 `[99]`）的行，按 `(canonical_url, excerpt_hash)` 幂等去重合并新增证据与来源，
  通过 `done_finalizer` 顺延并重新编号引用，并复用 report-quality 的统一 citation parser，避免在 merge 层再维护一套 Markdown 正则；全面同步 `queries`、包含完整 `query/opened/final_url` 的 `search_results`、
  `notes_created`、`notes_updated`、`links_created`、`counterpoints` 与稳定排序的 `source_urls`。
- 彻底清理 `PlanExecutor` 中 `max_wall_time` 残留死分支，消除已废弃的计时器参数，确保执行边界语义纯粹干净。
- `PlanExecutor` 在 fresh-source 总预算已满时会先停止，不再多打一轮无效 search；deterministic merge 也不再把非模型报告装配计入 `ResearchRunResult.turns`。
- 将 Research 生命周期编排收归 `codey/research/pipeline.py`：初始
  `ResearchRunner`、proof review、`QueryPlanner`、有界 `PlanExecutor`、
  evidence-only follow-up、确定性 `merge_evidence_patch`、最终 proof review 和 Evidence Ledger 写入由 Pipeline
  统一拥有；`TaskRunner` 只负责外围 provider/session/trace/mode 生命周期。
- 实现真正的 Evidence-Only Follow-up 模式（`codey/research/evidence_followup.py`）：
  follow-up 阶段严格限制为 1 个模型交互 turn，程序级白名单仅允许 `knowledge_write`，
  严禁 `done/web_search/open_url/knowledge_link`，程序级校验确保 URL 必须在 `fresh_source_urls` 白名单中，
  严禁使用内部 `s1/s2` 标签。
- 实现确定性补丁合并器（`codey/research/record_merge.py`）：
  丢弃未受支持的新断言，按 `(canonical_url, excerpt_hash)` 幂等去重合并新增证据与来源，
  通过 `done_finalizer` 顺延并重新编号引用，确定性生成最终 ResearchRecord 和报告。
- `PlanExecutor` 引入严格的 Fresh-Material 语义：执行前收集 baseline URLs（已读、已打开、已入 evidence），
  跳过重复 URL，无新 URL 打开时干净返回 `stop_reason="no_new_material"`。
- 新增 `ResearchIterationRun` 作为单轮 Research primitive 与 Pipeline 之间的
  明确边界。运行时 `ResearchTools` 只在迭代边界传递，不再隐藏挂在
  `ResearchRunResult.runtime_tools` 上。
- 移除 `_run_research_task`、`close_search` 等旧 seam；测试和手工 harness 直接
  使用 `_run_research_iteration`，避免冷启动项目为了迁就测试保留无意义兼容层。
- Pipeline 继续执行一轮 bounded follow-up，保持串行 search/open/fetch、现有
  tool contract、URL guard、既有 UI/SSE 字段和最终 record 单写约束不变。
- follow-up 结果现在会把 `followup_applied`、`followup_rounds` 和
  `planner_stop_reason` 一路透出到 `task_done` / Run Trace；follow-up 迭代或执行
  出错时会保留已成功的 initial result，不再让增强路径拖死主结果。
- follow-up eligibility 放宽到可行动的 `max_turns` / `no_progress` 场景，只要 proof gap
  仍然是 bounded planner 能补的类型，就允许继续做有限补搜。
- 新增 `tests/manual/bounded_research_planner_ab.py`：和现有 manual probe 一样按行
  原子落盘 send/reply 轨迹，同时记录 baseline/planner 两个 arm 的 pipeline metadata。
  planner arm 的 `max_wall_time` 已在 A/B 里关闭，时间只作为诊断字段保留，不再当作
  质量判定条件。
- 收紧 bounded planner A/B 的 `followup_usefulness` 口径：失败 row 不参与成对评估，
  `useful=true` 必须同时有新增材料、质量侧改善且没有明显质量回退；Pipeline 也将
  proof review 缺失明确记录为 `proof_review_missing`，不再误报为没有 actionable gap。
- 增加架构边界测试，锁定 ResearchPipeline 不依赖 TaskRunner/Server，且最终
  ResearchResult 不携带运行时工具对象。
- 2026-08-20 的 DeepSeek / MiMo bounded planner 实机 A/B 已落盘：DeepSeek 的
  `warehouse_gap` 提升主要来自初始回答质量而不是 follow-up 新材料，`widget_noop`
  只用一次 follow-up 换来轻微 coverage 提升；MiMo 两个 case 都在 `max_wall_time`
  前没走到 follow-up。当时结果支持继续保持 planner 保守启用，先改善 budget
  预留、gap 触发和 material-gain 判定。
- 关闭 planner-arm wall-clock limiter 后，MiMo 复跑显示 `warehouse_gap` 仍停在
  `no_actionable_gap`，`widget_noop` 虽然执行了一轮 follow-up，但没有新增 source 或
  evidence；这说明时间限制不是核心收益瓶颈，下一步应让 planner 更明确地区分
  “回答修饰” 与 “新材料补搜”。
- 生产合入前的手工 A/B harness 增加 fresh-material executor 实验：planner arm 会跳过
  已打开 URL，并在 summary 中区分 `execution_material_gain` 与最终 record 的
  `material_gain`。MiMo 复跑显示 `widget_noop` 能 fetch 到新的
  `widget-storage-update` fixture，coverage 和 unsupported-claim rate 改善，但最终
  ResearchRecord 仍没有新增 source/evidence；下一步应验证 follow-up synthesis 如何把
  executor 材料吸收到 ledger，而不是先改生产 PlanExecutor。
- 生产合入前的手工 A/B harness 增加 evidence-only follow-up 实验：planner
  follow-up 只允许 1 轮 `knowledge_write`，禁止 `done` 长报告重写，并用确定性
  patch 验证“模型只采 evidence、最终报告由程序合并”的方向；该方向已经落成生产
  `run_evidence_followup()` + `record_merge`。
- 修复 Qwen Studio 首页首发 false-ready：页面会先暴露
  `textarea.message-input-textarea` 与 `button.send-button`，但 submit handler 还未
  完全 ready，立即提交会清空输入且不进入会话。`new_chat()` 现在只在 Qwen 首页首发前
  等待短暂 hydration，并且该等待受同一个 timeout budget 约束；Qwen live submit probe
  和 `new_chat(timeout=60)` 均已验证通过。
- Qwen hidden-material paired A/B 已补跑成功：baseline/planner 分数 `5 -> 6`，
  coverage `0.556 -> 0.667`，unsupported claim rate `0.333 -> 0.250`，新增
  `source-b` source/evidence 各 1，`followup_usefulness=true`；代价是 provider sends
  `5 -> 7`、耗时增加 27.528 秒。
- GLM / StepFun hidden-material paired A/B 已补跑：GLM raw score `1 -> 6`，
  但 unsupported claim rate `0.0 -> 0.4`，按保守 usefulness gate 判定为 false；
  StepFun `1 -> 1`，planner 停在 `initial_stop_reason_protocol`，没有进入 follow-up。
- 生产合入前的 evidence-only patch-merge A/B 已在五个网页 provider 上全部显示
  `useful=true`：DeepSeek、MiMo、Qwen 的 `widget_noop` 均从 score `5 -> 6`，
  StepFun、GLM 均从 `1 -> 6`。每个 planner row 都新增 `source-b`
  source/evidence 各 1，且 unsupported-claim rate 没有回退。StepFun 不再触发长
  `done.answer` JSON 转义失败，因为 follow-up model 不再负责最终报告。
- bounded planner 手工 A/B harness 现在直接调用生产 `run_evidence_followup()`
  和生产 deterministic merge；唯一保留的 A/B 专用执行补丁只是 fixture
  material-phase executor，用来可控暴露隐藏 source B。旧的 harness-only
  follow-up controller 和 patch-only merge 旁路已删除。
- 已把五家 evidence-only3 成功 follow-up reply 回放到当前生产
  `run_evidence_followup()`：DeepSeek、MiMo、Qwen、StepFun、GLM 均接受严格显式
  `{"tool":"knowledge_write","args":{...}}` schema，并各写入 1 条新 evidence。
- 生产合入后的 bounded planner A/B 已补 DeepSeek / Qwen / StepFun 成对行，且
  DeepSeek、Qwen planner arm 均明确走
  `ab_followup_mode=production_evidence_followup`。DeepSeek
  `widget_noop` 从 score `5 -> 6`，新增 1 个 fresh source/evidence pair，
  coverage `0.556 -> 0.667`，多 1 次 provider send，`useful=true`。Qwen 同样
  score `5 -> 6` 并新增 1 个 fresh source/evidence pair，但 unsupported claim rate
  从 `0.333 -> 0.750`，因此保守 usefulness gate 记录为 `useful=false`。StepFun
  取到了隐藏 fresh source，但最终仍是 protocol/not-answered，candidate 未被选中，
  score `1 -> 1` / `useful=false`。
- 新增 `tests/manual/bounded_research_merge_projection.py` 离线诊断脚本，用已落盘的
  bounded-planner A/B JSON 与 trace 评估“只从 evidence-backed claim 重建最终报告”
  的 narrow merge 投影。projection 保持五家 evidence-only3 row 为 useful，并把 Qwen
  与较早 StepFun production row 投影为 useful；其中一条 StepFun 实机复跑是在多轮
  测试触发 provider-side rate limit 后采集的无效 gate 样本，不作为 planner/merge
  反证。后续干净 paired StepFun 复跑已经进入 fresh evidence extraction，raw 仍为
  `1/false` 是因为 candidate_not_selected，而 projection 将其转成 `6/true`，因此支持把
  narrow rebuild 合入生产 `record_merge.py`。
- 强化 `record_merge.py` 的 evidence-backed candidate 合并：质量复评改为从
  `search_results_payload()` 派生 search-result URL，修掉不存在的 ledger helper；
  对 protocol/not_answered 初稿改为从 staged ledger evidence 重建最小报告；
  `来源质量` 与 `搜索覆盖` 段落由 deterministic merge 重新生成，不继承旧模型段落。
- 记录 narrow rebuild 后的 Qwen production paired A/B：`widget_noop` 保持
  score `5 -> 6`，`useful=true`，新增 1 个 fresh source/evidence pair，
  unsupported-claim rate 从 `0.333` 降到 `0.250`，不再复现旧 production row
  回退到 `0.750` 的问题。
- 深度加固与卫生清理（Evidence-Only Follow-up & Deterministic Record Merge 生产落地）：
  1. 彻底移除 `max_wall_time` 生产 gate 与停止分支，研究行为边界由 query/source/round/cancellation 保证，时间仅作诊断指标。
  2. 强化 `evidence_followup.py`：Prompt note type 修正为 `fact`；强制显式非空 evidence list，且每条 evidence 必须显式使用 `source_url`；URL 白名单严禁内部 `s1/s2` 标签；出现非 `knowledge_write` 工具整轮直接标记 `invalid_tool_called`，且严格只执行 1 个合法的 `knowledge_write`。
  3. Follow-up 阶段引入 Staged Ledger（`ledger.clone()`）事务隔离机制：只有在 candidate 胜出被选中时才应用到最终 `best_tools`，未选中的 candidate 零污染主知识库。
  4. 强化 `record_merge.py`：幂等去重键严格对齐 `(canonical_url, excerpt_digest)`；支持非标/协议停止初稿恢复与格式化；全面同步 `ResearchRunResult` 元数据（`source_urls`, `opened_sources`, `sources_read`, `evidence_items`, `citation_map`, `coverage` 等）；生成确定性 `synthesis:merge:{digest}` synthesis_id 并修正 `project_ref`。
  5. 修复 `PlanExecutor` 字段为 `tools.ledger.evidence_items`，删除 `_followup_context` 死代码，扩展 `ResearchPipelineResult` 观测字段（`fresh_source_count`, `new_evidence_count`, `final_evidence_count`）。



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
- 新增共享的 `codey/citation_scanner.py` helper，让 done compiler、
  report-quality gate 和 Writer handoff 共用同一套 citation / source-id 扫描规则，
  避免后续分叉。report quality gate 也顺手拆成几个小 helper：missing section、
  source-id leak、no-citable
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
- Web/SSE 的 `RunEvent` 投影移到 `codey.runtime.events.run_event_ui_payload()`，
  Research 工具展示名映射移到 `codey.runtime.events.display_tool()`；`TaskRunner` 只调用共享投影，
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
  风险、canary 提示和 bounded failure families。
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
  具体 repair：unknown `write_file` 通过 repair prompt 引导模型改用
  `edit(content=...)`；混用 edit 模式会说明
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
  工具函数，不再 monkeypatch `codey.agents.runner` 全局函数。
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
