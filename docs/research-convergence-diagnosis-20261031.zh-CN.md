# 固定本地 27B 模型下的 Research 诊断与收敛方案

分析日期：2026-10-31（Asia/Taipei）。当前源码：`060656db25aa4dcd031561f1f4c5df10ec6ec26e`。

本次核对生产代码、既有诊断文档和原始 wire，并用当前纯函数重放评审校验。没有重新调用模型、修改生产代码或改写历史运行。下文的架构调整是待实施方案，不能当作已经获得实机成功率。

**判断：Codey 的确给模型增加了大量可以由程序承担的工作；但把这些负担去掉，也不能自动消除模型的证据解释错误。可行方向是在现有唯一运行时里，把 Research 改成小范围取证、小范围判断和确定性报告编译。**

## 1. 这次比旧文档多确认了什么

旧根因报告固定在较早提交，后来已修复阶段完成提示、问题覆盖契约、coverage 引用绑定、引文换行和浏览器关闭等问题，不能把旧问题一律算作当前缺陷。

这次重点检查了旧报告未充分展开的两个最新归档：

- `artifacts/research-ownership-minimal-verified-20261031`：真实网页最小任务机械完成，但内容仍有错误。
- `artifacts/research-ownership-wal-final-20261031`：完整 WAL 任务进入最后评审后超时。

它们记录了各自的 `runtime_implementation_id`，并非当前 HEAD 的新实机测试。当前仍存在的问题，由代码检查和本次离线回放另外确认。

| 归档任务 | 物理模型请求 | wire 首尾间隔 | 最后评审规模 | 实际结果 |
| --- | ---: | ---: | --- | --- |
| 单问题、单条发现 | 15 | 392.4 秒 | 29 个判断，输出参数 5,696 字符，159.4 秒 | done；同模型评审放过错误的来源范围判断 |
| 三问题的小任务 | 13 | 1,677.3 秒 | 66 个判断；三次输出约 1.28 万字符 | research_review_incomplete |
| 完整 WAL 六问题 | 24，含最后超时请求 | 3,522.2 秒 | 114 个判断 | 600 秒生成超时，未获得完整评审 |

统计脚本：[audit.py](../artifacts/research-audit-20261031/audit.py)。统计结果及 wire SHA-256：[audit.json](../artifacts/research-audit-20261031/audit.json)。历史流式响应没有完整 usage；本报告的“字符”不是 token，不据此宣称上下文超限。三问题归档中暴露的 coverage 编译绑定缺陷后来已经修复；该归档这里只用于测量整包评审成本。

## 2. 主要原因及其证据

### 2.1 问题归属被实现成重复读取义务

完整 WAL 运行第 2～6 次模型响应，每次都对同一个 URL、同一个 offset 分别提交 q1～q6 的六个 `open_url`。总共 30 次读取调用，只涉及 5 个不同 URL/分页窗口。这里统计的是工具调用，不能等同为 30 次实际网络下载。

这与当前约束直接吻合：

- `research/findings.py::_validate_passage_question` 要求选中的 passage 曾为同一个问题读取。
- `research/questions.py::validate_question_coverage` 要求 aN、fN 归属于相同 qN。
- `research/completion_guidance.py` 明确要求为同一个问题读取再保存。

问题归属有价值，但“同一份已读原文不能直接服务另一个问题”不是证据可靠性的必要条件。真实来源与知识版本可以共享，是否支持另一个问题应在**采用关系**上判断。当前契约让模型反复执行、反复接收相同文本，再手动维护 qN/aN/fN 的对应关系。

完整运行一次请求的消息正文达到 **274,756 字符**。`ResearchContextView` 自身的 14,000 字符上限只限制新增的状态视图，没有限制整段 provider 历史。因此“有 bounded view”不等于“整个模型输入有界”。

### 2.2 评审阶段确实重复发送了整份材料

当前 `operations/task_guidance.py:22` 的 `research_only` 只包含 research、evidence_followup，漏掉 research_review。

评审的 `session.task_text` 已经包含完整 `review_prompt(basis)`。它一方面进入主任务提示，另一方面又被 `kernel_prompt.py::working_context` 放进 `user_task` JSON。

完整 WAL 最后一次请求有三条消息：

1. 通用 working context：28,528 字符，包含整份评审提示和 basis。
2. 主评审提示：27,459 字符，再次包含同一 basis。
3. 时间上下文：425 字符。

合计 56,412 字符，另外还有 3,554 字符工具 schema。两份材料有同一个 basis digest。

本次用当前代码重建最小案例，`working_context.user_task == review_prompt(basis)` 仍为真，且这个通用 context 还让仅暴露 `submit_research_review` 的评审任务去 `call done`。这是可以直接删除的冗余和无关指令。

它是明确的输入膨胀来源，但目前没有单因素实机对照，不能声称仅修这一处就能消除超时或语义错误。

### 2.3 评审要求比写答案更复杂

完整 WAL 评审一次必须返回：

- 11 个原任务文本片段的覆盖判断；
- 6 个调查回执的相关性判断；
- 29 个 finding/condition 单位的判断及连续原文引文；
- 68 个报告文本单位的判断及 finding-unit 对应关系。

总计 **114 个判断**，而且必须全部齐全、不能重复、引用关系正确。`text_units` 按标点切分，属于无损文本范围，不是语义原子；一个单位仍可能包含多个判断，切得更碎也可能产生依赖前文的残句。

`operations/research_review.py` 给整份评审三次提交机会；任何一处结构错误都可能迫使模型重写整个大对象。三问题样本的三个评审响应分别用了 347.1、441.3、448.6 秒，合计约 20.6 分钟，仅仅在这个阶段重交 JSON。

完整任务最后的直接停止原因是单次生成超时。现有数据支持“任务和输出体量过大是重要工程问题”，尚不足以拆分计算出模型解码、服务排队、预填充各自占了多少时间。

### 2.4 相同语义被反复创作，扩大了出错面

模型先写 finding，再写 summary、evidence、counter、coverage.answer，然后评审这些重新表述的文字。即便最初抽取正确，后面每次改写也可能增加原因、比较对象、版本范围或适用性承诺。

`knowledge_schema.py::report_sections_schema` 还要求 evidence、counter 至少各有一段。单纯查“几个写者”的任务也必须填写 counter，容易诱导出“原文没讨论其他内容”这类填充语句。

实际最小完成样本写了：

> 原文仅陈述单写者约束，未讨论性能、版本历史或工作负载。

评审的支持理由是：

> The finding excerpt does not discuss performance, version history, or workload.

它把“所选摘录未涵盖”扩大成“原文未讨论”；归档的 WAL 页面并不只有这一段。当前 `parse_review` 重放该 assessment 仍然接受，`review_issues` 返回 `[]`。这不是解析器应该通过关键词就能识别的一般语义问题，而是说明结构正确和同模型自评不能保证结论正确。

### 2.5 模型能力边界也真实存在

固定短材料的 Qwen L3 样本只输入 254 token，就从“读写可以并发、写者串行”推到“显著提升并发读性能、整体吞吐优于默认回滚日志”。这个实验没有浏览器、长历史或 Research 调度，不能用 Codey 上下文负担解释全部错误。

另一方面，固定抽取 L2 的 6/6 输出通过 JSON、字段和引文定位的机械检查，医学样本正确保留了小鼠模型及比较对象。这个结果支持“优先采用抽取、小范围判断”的方向；不证明所有抽取语义都正确。

当前部署是归档里记录的融合微调、Q4_K_M、KoboldCpp 组合。没有受控对照，不能把问题单独归因于 27B 参数量、Q4 量化、模型家族或服务端。方案不要求更换模型。

### 2.6 历史执行层修复有用，但没有改变主要工作形态

EOF、回执配额、thinking 设置继承、阶段 done 协议、浏览器关闭、覆盖绑定和引文换行等修复都解决了真实问题。今天最新提交的 thinking/历史/重复工具修复也有真实小任务验证。

这些结果不能外推成严格网页 Research 已全面通过。继续只围绕某个错误追加提示、增加 schema 字段，会使一个固定能力模型需要同时满足更多约束。现在应调整模型与程序的职责分配。

## 3. 目标执行方式

建议保留现有 `ResearchPipeline → ResearchContinuation.kernel → run_task_kernel`，在这条链中调整 domain work 的粒度：

```text
原始任务及冻结要求
        ↓
围绕一个问题调查，复用真实已读来源
        ↓
从少量完整原文抽取小范围 finding
        ↓
对选定 finding / answer 做小批评审
        ↓
程序检查全部必需项是否有结果，缺口是否明确
        ↓
程序编译一次报告
        ↓
现有 publication effect / knowledge transaction
```

每一块仍是同一 provider、同一运行时中的请求。阶段可以有多个，执行状态和恢复机制只有一个。

模型承担：搜索词、相关性判断、忠实抽取、有条件的归纳。程序承担：稳定身份、来源版本、引用关系、待完成项、格式、机械覆盖、恢复和发布。

“完成”允许真实未解问题。对于没有测量过的工作负载，明确未知就是有效研究结果；不得为凑齐答案而从并发机制推出性能收益。

## 4. 具体修改顺序

### 第一批：删除重复输入和无关输出要求

修改 `operations/task_guidance.py`、`kernel_prompt.py`、相关 prompt/contract 测试：

- 将 research_review 纳入不输出通用 workspace context 的领域任务；评审 basis 只从冻结评审输入进入一次。
- 评审窗口只描述当前可提交的评审动作；没有 done 工具时，不生成要求调用 done 的通用完成指令。
- 保留原始任务、全部被评审原文和其 digest；只删除重复副本，不能把证据截断后仍称完整。
- 以实际 admitted payload 测试完整输入，而非只测某个 prompt helper 的长度。

修改 `research/knowledge_schema.py`、`report_proposal.py`、`finding_report.py`：

- 允许 counter 为空。没有额外反证时由程序省略内容或输出“本轮未记录额外反证”这种运行事实。
- 区分“没有查到”“没有调查”“材料没有提供”“原文明确否定”；前两项由真实回执决定，最后一项需要原文支持。
- 保持 coverage 明示未解问题，不强迫模型为每个栏目创造新的事实句。

这批可以先落地验证，不必等完整重构。它不会单独解决语义可靠性。

### 第二批：共享证据，减少模型维护的字段

修改 `research/findings.py`、`questions.py`、`references.py`、`tool_contract.py`、`operations/task_execution.py` 和对应 receipt projection：

1. 将“来源曾如何获得”与“现在被哪个问题采用”区分。原读取回执的 question_ref 保持原样；第二个问题使用该材料时，记录显式采用关系，不伪造第二次采集。
2. 同一授权范围内已完整读取、版本明确的 passage 可以被另一个问题选择。对这个问题的语义相关性仍需判断；不能因为共享就自动认为它回答了所有问题。
3. 采用关系由现有 save/read 工具回执承载，恢复时按原回执投影；复用同一知识版本，不新增一份同文 finding 或第二张权威采用表。
4. `passage_ref` 已能唯一解析来源和版本，移除要求模型同时填 `source_ref` 的冗余必填字段。最终来源由 passage 的原始快照解析，不做猜测或字符串近似匹配；多 passage 仍校验同一来源版本。
5. 逐问题执行时，把当前 question_ref 冻结在现有 invocation 输入中。模型工具可以省去重复携带它，由 host 从该冻结输入生成有效执行参数，并保留原始调用与绑定依据。不能把模型错误 qN 偷偷改成正确 qN。

这一批改变的是数据契约，应同步修改 schema、执行校验、回执、恢复和测试，替换旧约束。不要同时留两套 save_finding 写入口。

### 第三批：让报告围绕一份答案数据编译

修改 `research/report_proposal.py`、`finding_report.py`、`report_projection.py`、`operations/research_flow.py`、`research/pipeline.py`：

- 每个问题只产生一份有引用的 answer，必要时进一步拆成少量完整主张。它是模型语义输出的唯一主表示。
- 证据段由这些 answer 实际采用的 finding 和原始摘录生成；coverage 指向相同 answer，不让模型再写另一份长答案。
- summary 优先选取已经审核的 answer 单位进行确定性组织。需要新的跨问题推论时，将其作为新的候选推论和独立受检单位，不能免审自由扩写。
- 来源表、检索记录、版本未知、问题状态继续由真实数据投影。
- 调整当前“先交一份完整报告→补查→再交完整报告”的策略：先补齐问题证据/明确缺口，再编译报告。格式错误只修对应 proposal 字段，不重新搜索或重复保存。
- 继续使用 `FindingReport` 的冻结 compilation 和 digest；展示、归档、恢复都从同一编译结果读取。不要在发布时按最新知识重编译另一份正文。

先用简单任务和三类已有固定材料做同题对照；若需要保留旧报告读取，只允许读取旧版本，新的写入只产生新契约。

### 第四批：分批评审，但只有一个全局验收和发布

主要修改 `research/semantic_review.py`、`knowledge_schema.py`、`operations/research_review.py`、`research_continuation.py`。

实施要点：

1. 冻结一份根 basis，按依赖顺序形成小批次：先 finding/conditions，再依赖这些 finding 的 answer，最后任务要求与缺口。保留必要的相邻句和完整被选证据，不把无上下文残句当独立语义。
2. 每批先试 4～8 个判断、约 600～1,200 输出 token 的目标范围；这是待实测的起点，不是模型可靠性的既定阈值。过长的证据包必须显式再分工作，不能静默截断。
3. 模型只返回这一批的判断。basis_digest、目标路径和全局身份由冻结输入绑定；无需反复让模型复制长 hash 和全局关系图。模型仍须明确作出支持/不支持/不确定判断。
4. 聚合器确定性检查：批次和根 basis 一致、每个应审单位恰好出现一次、没有越权支持引用、依赖的 finding 已通过、没有遗漏、不完整批次不得默认通过。
5. 单批结构错误只重做该批。若正文或证据发生变化，对应根 digest 改变；旧判断不能直接用来批准新文本。只有身份和完整依赖输入完全一致的判断才可显式复用。
6. **必须移走 `_submission_executor` 中“当前 assessment 没 issues 就发布”的行为。** 分批后每个 submit 只提交本批回执；只有全局聚合完成才进入唯一的 publication effect。
7. 同模型评审继续标记 `same_provider_model_assessment`。它是有用但可错的筛查，不能升级知识为独立验证。

架构上还必须处理 `ResearchInvocation` 当前固定编号：review 使用 `8 + 2*cycle`，repair 使用 `9 + 2*cycle`，continuation index 上限 16。小批评审不能另开一个 batch scheduler 来绕开它。

应在**现有** continuation 版本内替换这组位置编码：invocation 保存明确 purpose、冻结 unit/batch 输入引用，执行位置仍由同一个 operation checkpoint 拥有；下一项从冻结计划和已 settled 结果推导。总预算沿用一个权威入口。不能再维护一份 `current_batch/status/retries` 可写状态。

旧运行只能按冻结版本恢复或明确拒绝不兼容恢复；不能把旧执行中途解释成新工作流。读取旧 artifact 的兼容不等于保留两个可写运行时。

### 第五批：小窗口和定向修复

- 每个问题/小批次的新窗口只包含原任务必要范围、当前问题、完整候选证据、冻结输出契约及最近错误。原始 provider 历史继续保留在 durable receipts，模型工作视图可重新投影。
- 复用当前 `provider.new_chat` 和冻结 provider context 机制；不要手工剪掉一半 tool call/result 或跨窗口遗漏 thinking/原生调用关联。
- 以当前部署分词统计来选择上下文目标。服务支持 262K 是容量上限，不是每次应填满的工作预算。
- 对反复出现的同类缺陷，在现有 `KernelProgress` 中依据未解决单位和有效新增证据判断进展，避免仅改几个字就一直重交整篇报告。保留原有恢复和停止语义。
- 本地生成超时的 maybe_sent/结果未知状态继续按既有边界处理，不能为“跑通”把未知请求自动重发或记录成未执行。

## 5. 单一事实来源和单一运行时的具体边界

单一事实来源不是强迫所有领域数据塞在一个对象里，而是每一种事实只有一个有权修改它的来源。

| 事实 | 权威来源 | 允许的派生内容 |
| --- | --- | --- |
| 当前执行位置、预算、取消 | 现有 durable operation/checkpoint | UI 进度、待执行工作视图 |
| 实际调用及结果 | 原 provider/tool effect receipt | aN、调用统计、问题采集记录 |
| 原文和来源版本 | 原始 source snapshot 与已保存的精确版本引用 | 有界摘录视图、引文定位 |
| 候选知识及修订 | 现有 KnowledgeStore 事务 | fN、图视图、问题采用引用 |
| 评审判断 | 普通 submit 工具的 settled receipt | 按根 basis 聚合的检查结果 |
| 研究报告 | 唯一冻结的 FindingReport compilation | Markdown、UI、EvidenceLedger 历史投影 |
| 已发布知识 | 既有 publication effect 对应的唯一知识事务 | 发布状态、报告索引 |

必须保留：`run_task_kernel`、effect/receipt 边界、KnowledgeStore 的幂等事务、精确 revision 和 compilation digest。

应删除或收敛：重复评审 context；问题归属导致的重复读取义务；整份 assessment 反复生成；同一答案在多个栏目独立创作；新流程中不再需要的固定 attempt 算术。

不新增：本地模型专属 ResearchRuntime、独立调度数据库、另一份可写 EvidenceCard 存储、同时写旧报告和新报告的两个 writer。将证据展示成卡片可以是纯投影，不应变成新的知识事实源。

目前 `EvidenceLedgerStore.append_accepted_report` 已从冻结编译结果投影历史，这个方向应保留。索引/缓存若落盘，必须可由权威 artifact 重建，并且不能反过来覆盖原报告或决定一次已完成运行的发布结果。

## 6. 如何验证，才能停止反复改提示

保留现有原始失败样本作为测试资料，不拿“工具没报错”和“模型说 passed”替代内容验收。

**机械验证：**

- 一个评审 payload 只含一份完整 basis，不携带无关 workspace 完成指令。
- 同一来源窗口服务六个问题时不强制读取六遍；实际获取与后来采用可分别追溯。
- 根 basis 任意漏一批、重复一批、伪造引用、依赖被拒 finding 或混用旧 digest 均不能发布。
- 杀进程后恢复不重查已 settled 来源、不重跑已提交判断、不增加重复知识修订。
- 在最终知识事务提交前后分别模拟中断，保证只发布一次；取消不得由后继阶段重新激活。
- 修复一个 answer 后，旧评审不批准其新版本，报告和归档正文始终来自相同 compilation。

**模型内容验证：**

- WAL：读写并发不等于多写者并发，不等于具体吞吐必然提升。
- 医学：A 对 B 的优势不能变成 A 对 C；小鼠不能写成人体疗效。
- 零售：自愿参与和非随机分组不能直接推成已知的基线销售优势。
- 范围：摘录没提到不等于整篇没研究；抓取时间不等于版本发布日期。
- 保留正确正例，避免“全部 uncertain/全部拒绝”看起来零错误。

本地模型固定；先固定采样、thinking、模板和材料做单因素对照，再用多个 seed 检查稳定性。每题至少进行三次独立新状态运行作为初步门槛，明确这仍是小样本。

记录：各阶段结构通过率、错误放行/错误拒绝、原任务覆盖、实际未解问题、请求次数、输入输出大小、总耗时、重试原因。冻结材料的人工标签可以作为开发验收基准，不要求每次线上研究都再调用更大的模型。

仅在这些正反例有明确改善后，再跑“单问题→三问题→原始完整 WAL”实机链路。最小任务的 success 不能替代完整任务，旧源码成功不能替代新源码验收。

## 7. 本次验证记录与实施优先级

已运行只读脚本，确认当前重复输入路径、历史请求规模、最小错误评审仍可被当前解析器接受，以及固定 L2 的机械结果。所有派生输出保存在独立 audit 目录，原始研究资料没有修改。

定向回归 **102 passed in 184.56s**，覆盖评审范围、问题归属、引用覆盖、持久化续跑和重复调用反馈：

```powershell
.venv/Scripts/python.exe -m pytest -q tests/test_research_review_scope.py tests/test_research_question_ownership.py tests/test_research_reference_coverage.py tests/durable/test_research_continuation_restart.py tests/test_repeated_tool_feedback.py
```

这些结果验证测试覆盖的程序边界，不代表自然语言结论正确；没有运行全量测试或新模型生成。

实施顺序建议：**先去重和取消强制填充；再改证据共享与答案唯一表示；然后实现同一运行时中的分批评审和唯一聚合发布；最后做原题实机验收。** 每批都应删除被替代的生产入口，不能长期保留“新旧模式各跑一套”。

在不能换模型的前提下，可以合理争取的是：较小而忠实的研究结果、明确的未解问题、可恢复的执行和真实可追溯的证据。当前资料不足以承诺任意开放研究都能完全正确完成，但已足够定位上述可改的工程问题，并给出不增加第二套架构的实施路径。
