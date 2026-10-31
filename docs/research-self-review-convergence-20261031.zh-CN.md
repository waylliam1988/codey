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

## 7. 原 66 个失败及 241 个错误的离线修复

本节记录后续收到“修好、回测、commit”的授权后开展的独立离线修复。
前文历史失败与实机原始记录保留；本节不把离线通过解释为 Gemma 12B 或 Qwen 27B 的实机成功。

### 根因分类

- **241 errors 是同一个测试夹具故障**：parity 探针还读取已删除的
  `ToolContract.optional/required`。当前探针直接读取生产 `input_schema`；独立旧版本分支
  仍按原定义读取，不复制旧实现、不重新导出历史基线。
- **原 66 项主要是测试未跟上契约**：旧脚本省略 research_plan、question_ref，
  在严格研究里调用不再开放的 knowledge_write，或给 done 提交旧 summary/完整报告。
  另有不完整的会话假对象、没有 managed outputs 的 AppContext、旧提示快照和评审状态断言。
  现迁移为显式规划、真实读原文、save_finding、只提交 coverage；负例仍验证实际拒绝和修正，
  没有 skip/xfail、自动补规划、自动填答案或放宽生产契约。
- **生产代码也有问题，不能全部归咎于测试**：纯文本完成被拒绝时，纠错文案仍要求旧的
  `summary + report_sections`，已改成 `done.report_sections.coverage`。
  PDF 成功保存正例还暴露了页码丢失：可选段落包含生成的 `[page N]` 标签，
  引文无法匹配页正文，最终 page 为 null。现在按页正文切分精确段落，引用仍指向原文坐标和版本。
  页码由段落坐标确定，重复正文的不同页也能区分；新写入与幂等恢复使用同一计算。
  页标签作为引文外的显示元数据保留，原有 PDF 读取和页内定位测试的页码显示断言继续成立。

PDF 修复没有对引文做模糊替换。UI 脚本先提交未读的段落引用并收到拒绝，
再显式选择已读 PDF 正文段落，最终保存原始正文及页码。
新增单页、两页相同正文、编译引用和回执恢复测试，防止只修显示而丢失归档事实。

### 收敛与历史 parity

固定历史 baseline 的 682 个 case 和原始 observations 保持不变。
逐项登记 35 个新增有意差异，修正 2 个已过时的差异条目；最终 502 项行为相同、
180 项具有精确 before/after 的有意差异，不用通配放行。
差异覆盖显式参数投影、严格规划/问题归属/完成契约，以及原本就拒绝未验证完成的
无进展停止时点。旧研究脚本保持原样被拒绝，另加显式现代脚本验证确实能保存证据和发布报告。

离线 A/B 工具夹具也不再自行实现 open_url 的读窗口和回执：只在来源提取边界注入固定文档，
段落生成、版本绑定和保存全部经过生产实现。恢复用例通过真实知识库与原工具回执，
分别覆盖已确认/未确认交付；恢复后禁止重新搜索、抓取、保存，逐字段核对问题、段落和引用。
生产没有新增运行时、事实存储或双写路径。

### 验证记录

- 原始 66 个 node id 原样重跑：**66 passed，48.76 秒**。
- 完整历史 parity 与对应回归文件：**731 passed，60.78 秒**，包含原先报错的参数化用例。
- 原扩大命令 `pytest tests --ignore=tests/manual -k 'research or text_tool_json_format'`：
  **1539 passed、0 failed、0 errors，另 323 subtests passed**；9546 deselected，
  404.92 秒。此为原范围的扩大选集，不宣称全仓测试通过。
- 受影响文件回归 **116 passed**；PDF、精确段落及页码相关回归 **30 passed**；
  最终真实完成 oracle 与协议定点回归 **34 passed**。各选集有重叠，不相加。
- 生产 Ruff、Mypy（576 文件）、Pyrefly、ty，以及修改的 Python 测试 Ruff 均通过。

本轮原始输出位于 `.e2e-artifacts/research-regression-repair-20261031/`：
`original66-final.log/xml`、`parity.log/xml`、`pdf.log`、`affected-files.log/xml`、
`expanded-verified.log/xml`、`network-audit.json`。扩大回归期间禁止连接常用本地模型端口，
仅使用固定模型回复、固定网页/PDF 与临时知识库；不启动实机研究或修改 KoboldCpp。
网络审计中的模型端口连接尝试为 0。完成上述验证后提交并停止，不追加实机实验。

## 8. 后续授权：q8 缺口内容与实际调查回执

收到“先修 q8、补测试、回测、实机验证后提交”的授权后，当前逐问题报告提案在生成
任何报告视图之前编译两种缺口正文，并从原 ledger 投影真实调查回执。缺口中模型自由
填写的肯定性结论不再进入报告、知识 answer、合成笔记或恢复；原始请求仍保留。
部分有依据的回答继续使用 partial，没有新增运行时、写入口或强制自审。

最终定点 **35 passed**；原扩大范围最终 **1558 passed、0 failed、0 errors，另 323 subtests passed**。
生产 Ruff、Mypy、Pyrefly、ty 通过。扩大到其他提示/golden 测试时仍有已在修改前源码上
复现的无关失败，详见新报告，不宣称全仓测试通过。

三次真实 12B 定点运行均为 12 请求：首版 done 发现漏报实际调查；最终代码 seed=73
因 rN 登记产生重复 q1 而 no_progress、未发布；seed=74 在约 121 秒后发布，q8 类缺口
正文及存储一致性核对通过。该轮 q1 仍将单写者问题答成读写并发，整份内容核对不通过。
首轮退出还复现 TargetClosedError，浏览器退出仍须继续定位。

完整经过、原始数据、范围限制及下一步建议见
[q8 修复与实机报告](research-q8-gap-20261031.zh-CN.md)。下一项建议先处理 r6，再单独验证 f6。

## 9. 后续授权：r6、重复覆盖记录、q1 答偏与浏览器退出

已将独立、明确的报告格式要求移出逐问题登记，原文/位置和格式检查保留；实际问题数量
进入同一冻结 schema，补 rN 时不能增加覆盖行。q1 已知错配增加有限负检查，仍不证明
主体、条件、数字和版本的语义支持关系。隔离浏览器先关闭自有 context 再断开协议，
同一密集路由反例从 12 轮 288 条未取回 future 降到 0。

扩大离线选集最终 1586 passed，另 325 subtests passed；四项静态检查通过。
约 100 字的格式定点实机能在超长拒绝后改原答案至 119 字完成，未登记格式 r6，
但人工发现其选证不支持单写者边界，已补最终反例。原始完整 WAL 任务仍因重复保存失败；
最终纠错实机复跑又在首请求 120 秒超时。因此完整 Research 内容仍未验收通过，
最终检查后的模型修复能力仍缺实机成功证据。经过、原始资料及下一步见
[r6/q1/浏览器收敛报告](research-r6-q1-browser-20261031.zh-CN.md)。
