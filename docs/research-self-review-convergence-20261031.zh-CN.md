# Research 验收收敛与本地验证，2026-10-31

## 1. 历史回归的五项失败

用户要求先查明 `220 passed, 5 failed` 的原因，再调整同模型强制自审。
本项没有运行模型，没有修改历史原始记录。

| 失败 | 定位 | 修改 |
| --- | --- | --- |
| incomplete_done_frame_gets_specific_repair_without_executing | 测试过期：普通任务却期待旧版 Research 字段指导 | 保留不执行畸形调用的断言，检查当前普通 done 的 summary 修复指导 |
| manual_knowledge_refs_are_rejected_by_the_frozen_report_contract | 测试过期：先带入已移除的 summary，尚未到达待测 knowledge_refs 字段 | 使用当前 coverage 唯一报告输入，再验证 knowledge_refs 被拒绝 |
| headless_rejects_displayed_commit_then_completes_after_real_read_and_commit | **生产 schema 与领域对象不一致**：KnowledgeObject 默认 title=""，to_dict 合法输出空标题，但 mutation_schema 要求非空 | 工具 schema 允许空标题；原真实入口测试继续验证实际读取、提交与引用成功，其他必填内容仍非空 |
| canonical_split_preserves_valid_ledger | 测试过期：原文有 `2026`，引文擅自删除，过去依赖自动换引文 | 测试提供连续真实原文，生产仍拒绝伪造/拼接引文 |
| knowledge_write_split_preserves_save | 测试过期：URL 被当作充分证据，未提供显式 evidence | 提供原文、来源、主张及关系，生产仍不自动生成证据 |

本项只调整一处生产 schema 及三份旧测试；真实提交测试无需更改，即可覆盖空标题的生产修复。

验证：原同组 225 项全部通过（35.62 秒），Python socket 连接已禁用，无连接尝试；Ruff 与 git diff --check 通过。

后续验收方向：确定性校验约束来源、原文、精确版本、任务要求交代和发布回执；同模型正面自评不得提升为独立内容认证。执行完成和语义正确应明确区分。

## 2. 取消强制自审，保留确定性完成条件

严格 Research 的 writer 完成后，直接走原 `_publish_research_answer` 效果及知识事务。
`run_research_pipeline` 不再自动进入 `review_and_repair`，该强制自审/重写循环已删除。
没有增加另一套运行时、任务状态或写入口；恢复继续读取原始 provider、工具和发布回执。

原本的来源读取、连续原文、精确版本、qN/fN/aN 归属及报告结构校验继续有效。
新增 `coverage.task_refs`，指向从原任务文本确定性切分的 rN；当前冻结工具 schema、
JSON 采样 schema 和实际编译共用同一字段契约。所有原任务文本单位必须在答案或具体缺口中交代，
漏掉、捏造 rN 或在一条记录内重复 rN 都会拒绝完成。它证明模型显式登记了覆盖，
不证明答案真正满足原任务，也不把背景、方法、可选建议强行变成独立研究问题。

完成证明从 writer 的原完成证明、原报告 compilation digest、原结果引用和成功发布身份投影。
队列消费这一份证明，并重查任务、run/session/project、报告和发布的绑定；不会另跑旧质量评估来批准。
`done` 表示研究流程完成，允许如实保留未研究问题；不表示每个问题都有答案或结论获独立认证。

同模型评审仅保留显式诊断入口，至多三次调用；评审回执标注 `diagnostic_feedback_only`，
不能发布、生成新的完成证明或触发自动反复修复。`reviewed_completion` 明确拒绝模型自评授予完成权限。
新报告和知识 provenance 保留 `unreviewed` / `model_reported_not_semantically_verified`，
报告显示“所采用的主张与推论仍未经独立语义审查”。

生成指导要求一次围绕一个具体问题、少量直接相关的完整片段形成答案，保留主体、动作、条件及比较对象。
这是减负指导，尚未宣称能完全消除重复规划或复杂条件误解。原有语义评审契约和历史原始记录保留，
但其正面结果不再是当前研究发布的授权依据。

## 3. 本次实机：最小单写者任务

使用当前本地 Gemma 4 12B、JSON 采样、thinking=false、temperature=0.3、seed=73，
全新独立 state/knowledge，真实搜索及网页读取，生产 headless 入口和 kernel。
原始数据：`artifacts/research-gemma12b-20261031/deterministic-single-01/`。

结果：`done`，11 个正式 API 请求，约 84.9 秒，无 `submit_research_review` 调用。
实际发生搜索、打开页面、页内检索、打开命中、保存三项发现和一次最终发布。
两次过早 done 仍被原证据/问题归属检查拒绝，模型补齐本题 finding 后才完成。
人工对照保存的完整 excerpt，单写者及读写并发结论有相应原句。
三个规划问题仍重叠，报告有重复内容；原机械来源分类仍将 SQLite 页标成 secondary，不能代替人工质量判断。

此前同题 `single-json-06` 为 20 请求、约 162 秒；本轮调用数减少约 45%，耗时约减半。
这是不同代码版本的各一次观测，不是一般成功率提升或对 27B 的保证。

## 4. 完整 WAL 首轮与扩大回归的边界

`deterministic-wal-full-01` 使用原 `wal-full.txt`、同配置、预算 60：17 请求、292.6 秒，
最终 `no_progress`，answer 版本数为 0，没有发布，也没有运行自审。
模型只注册 q1..q5，却在最终 coverage 添加 q6；原校验反复拒绝 unknown q6。
模型试图给报告格式要求分配新问题，未通过 research_plan 注册。
当前采样 schema 仍只限制 q1..q8，未收窄到本轮真实问题集合，下一项单独修复这个工程缺口。

草稿还把已标注 deleted text 的旧大事务限制当成当前结论，并给 SQLITE_BUSY 增添未建立的解释。
原文删除标记仍在，未被提取器丢失；这是仍然存在的解释错误，不能通过放宽来源或覆盖检查解决。
本轮说明强制自审已不再成为额外负担，但不能据此宣称复杂研究内容已正确。

扩大选集 `pytest tests --ignore=tests/manual -k 'research or text_tool_json_format'`：
1228 passed、66 failed、241 errors（另有 323 subtests passed），376.62 秒。
在独立 `9d4844c5` checkout 对同名 66 项复跑，66 项均失败；
241 个 error 来自同一个历史 parity probe 使用已移除的 `ToolContract.optional`，独立基线代表项也复现。
这些结果记录在 `.e2e-artifacts/research-self-review-20261031/`，不宣称全仓回归通过。
其中归档诊断测试仍依赖已移除的强制评审场景，现迁移到新的确定性场景，保留真实归档读取/无采集断言。

当前功能选集最终 106 passed（57.51 秒），覆盖 native/JSON、漏 rN、错 fN、预算耗尽、
错误解释不被自评认证、五个真实进程退出边界的恢复、只发布一次及队列复用原证明。
生产 Ruff、Mypy（576 文件）、Pyrefly、ty 与 `git diff --check` 均通过。

## 5. 本轮冻结 done 契约约束真实问题编号

针对完整任务首轮的 unknown q6，`done_spec_for_purpose` 从原 session.research_questions
投影当前 qN enum；没有复制问题表或增加任何写入。JSON 采样与 native/kernel 都使用同一冻结定义。
新注册的问题仅改变下一轮 schema，旧快照不能被后续问题注册悄悄改写。
原完整集合、finding 和 investigation 归属校验继续保留，不能凭编号合法性证明语义正确。

指导和字段纠错明确：背景、方法、格式及可选线索应关联现有答案，不因此在 done 中创造新问题。
回归使用实机的“五题加虚构 q6”形状，分别校验 JSON/native 拒绝、真实注册后才允许及旧快照不可变。
编号/引用/发布恢复选集 56 passed（52.55 秒），生产 Ruff、Mypy、Pyrefly、ty 均通过。

原“220 通过、5 失败”所在的完整协议/证据选集，本次最终 228 passed（34.32 秒）；
较 225 项多了三个契约测试。测试进程禁用 Python socket 连接，实际连接尝试为 0。
原始日志、JUnit 和阻断计数为 `original-group.log/xml/json`，没有覆盖历史审核原始数据。

`deterministic-wal-full-02` 在首个请求因外部服务中断而停止，0 个工具轮次、未发布。
服务生成计数仍是上一轮的 28，诊断栈停在 `utfprint -> do_POST`，未开始本轮生成；
随后 KoboldCpp 进程从 28508 变更并重启，API 连接以 WinError 10054 断开。
Codey 保留 GenerationUnknownError，没有重发结果未知的请求。
这不是 qN 修复的成功或失败样本；同一 Gemma 12B 重新就绪后，另用全新目录运行第三轮。
外部服务输出阻塞仍会复发，Codey 内部自审改造没有修复 KoboldCpp 的 stdout。

## 6. 最后一轮结果与本次停止位置

`deterministic-wal-full-03`：原完整任务、同模型/JSON/thinking=false/seed=73、预算 60，
22 个正式 API 请求、317.1 秒，最终 `no_progress`，answer 版本数为 0，没有发布。
实际完成 1 次搜索、1 次打开结果、4 次页内检索、4 次打开命中、6 次保存发现，
没有 `submit_research_review` 调用，没有再次出现未知 qN。

最终反复漏掉 `r6`：原文为“给出约 800 字的中文研究结论，”。
模型没有将这一格式要求关联到已有答案，确定性校验拒绝后仍未补上，触发原无进展停止条件。
这表明新任务文本覆盖登记本身仍会增加小模型的负担；本次保留失败证据，未自动补填 r6 或放宽完成校验。

内容也未通过人工核对：f6 把只读 WAL 版本边界写成 prior to 3.7.0，
所选完整 excerpt 实际给出的边界为 3.22.0；同一 finding 还包含该 excerpt 未建立的 WAL-reset bug 断言。
删除标记、原文及精确引用均已保留，这些属于解释/选证错误，不能把“引用存在”当成“支持关系正确”。
q8 标成 not_investigated，却在答案中写了未经本机测量的肯定性性能表述，也不能算内容正确。

本次结论：强制自审已移除，同模型正面自评不能再授予完成或发布权限；
最小真实任务完成且调用减少。完整任务仍未跑通，复杂语义、缺口措辞与任务覆盖登记仍有失败，
外部 KoboldCpp 日志阻塞也仍可能发生，不能宣称 Research 已全面解决。

按用户最后指示，本轮结束后提交并停止；不再启动新的实机、追加修复或更改模型/服务配置。
