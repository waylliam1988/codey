# 固定 12B 模型的 Research 减负：代码复核与实施设计

本次复核 HEAD：`4d54b12661e434a9e3cda371cbb9cbd5990ab98f`。结论是：**还有值得验证的工程空间，但下一轮应改变模型实际执行的工作单位和结束契约。继续整理内核、追加全局提示或扩充语义关键词，不是当前最有证据的主线。**

本轮只新增分析文档和离线审计资料；未改生产逻辑、既有测试、模型配置，也未调用模型。下面带“建议”的接口、类型和流程尚未实现。

## 1. 对已有判断的修正

我赞成已有方案的主要方向，补充四点。

1. 内核的执行事实、发布快照和恢复路径已经更清楚。它能忠实保存和恢复一个错误解释，这同时说明执行一致性有价值、语义质量仍需单独验收。没有证据支持为此推翻当前 kernel。
2. 最新错误在第三次请求就形成，当时输入是 7,778 token；不能把所有错误都归结为末期约 30K 的长上下文。原五题实验也各只有一个注册的 q1，因此“只显示一个 qN”不是完整解法。
3. 缩小工作单位、限制实际输入、允许缺口结束、改善定位材料，解决的是不同问题。最好分别测量，不能把其中一个当成另三个的替代。
4. 12B 成功不逻辑蕴含 27B 一定成功。两个部署的指令遵循、量化、模板等可能不同。可以以 12B 为工程压力目标，再在实际采用的部署上独立验收，无须换模型。

当前可合理追求的是：小范围回答更忠实、有证据时能完整回答、没有证据时能留下明确缺口、研究过程能够结束且恢复一致。工程方案尚不能保证任意研究结论正确。

## 2. 本轮独立核对到的证据

### 2.1 最新单写者错误：抓到了完整网页，没有把直接原句送给模型

原始运行位于 `artifacts/kernel-refactor-20261101/batch3-research/`。

| 核对项 | 实际结果 |
| --- | --- |
| 请求数 | 9 |
| 各次输入 token | 3496、4664、7778、8475、8695、8902、9212、9459、10652 |
| 首次打开的来源全文 | 31,603 字符，保存在原工具回执中 |
| 实际展示的原文窗口 | `[0, 5991)` |
| 直接单写者原句的起始位置 | 7505，位于未展示部分 |
| 九次实际请求是否含直接原句 | 全部没有；检查时合并了空白与换行 |
| 错误出现位置 | 第三次请求的 `save_finding` |
| 最后报告 | 逐字采用该错误 finding |

模型写下：

> 官方文档并未明确保证同一时刻只有一个写者。……因此读取和写入可以并发进行。

其选择的 `p2eb61e37342e` 是 `[3830, 4797)`，说明回滚日志与 WAL 提交机制，并以 “Multiple transactions can be appended to the end of a single WAL file.” 结束；它没有建立 writer 数量限制。

这至少包含两个推理错误：把读者与写者的并发当成写者之间的并发；把当前所选片段没有陈述的事情扩大成官方文档没有保证。后续主要修 coverage、原任务登记和真实搜索要求，并没有再定位单写者原句。

`tools.py::_open_document` 已经明确区分模型窗口与完整回执，窗口末尾也明确显示可继续读取。这里不是文件下载失败或恢复丢字；是已有定位路径没有被充分使用。`source_rendering.py` 也已有章节位置索引，不需要重新发明一套全文索引。

我对同一冻结来源运行了现有 `source_search.search_text`：`one writer` 可以把目标段落排到第一项，中文“单写者限制”没有匹配。这是离线定位能力检查，不代表模型实机自行选对了查询。现有工具是词项定位，不能自动完成跨语言检索或语义判断。

### 2.2 当前负向检查确实会放过这条原始错误

把原运行的精确 claim 和 excerpt 交给当前 `validate_answer_focus`，它没有拒绝。

原因能直接从代码解释：

- `_BOUND` 在错误答案中匹配到了“只有一”，没有理解它处于“并未明确保证”的否定范围。
- `_BOUND` 在依据中匹配到了 `single WAL`，没有区分“一个 WAL 文件”和“一个 writer”。

该检查自己也明确声明不证明主体、条件或数值正确。可以保留有限负向提示，但不能把它继续扩充成通用语义验收器。

本轮 helper 重放仅检验这个函数，不是完整 gate 重放。原始报告已经实际发布，是归档运行的事实；不能用本轮 helper 调用替代发布证据。

### 2.3 实际请求仍会增长

另行复算旧完整 WAL 运行：23 请求，输入 token 从 3,499 增至 29,977，与已有诊断一致。

当前路径仍为：

- `context_view.py::build_research_context_view` 默认只限制工作投影为 14,000 字符。
- `kernel_context.py::freeze` 只给 research_review 设置 `tool_call_history_groups=2`；普通 strict research 只设置输出预算 4096。
- `api_provider.py::_payload` 只有收到该选项才调用完整协议组投影。

因此有界 view 没有使实际请求有界。反过来，直接加 `history_groups=2` 也不完整：view 尚未重建保存前的候选 passage 和可继续使用的搜索定位；`bounded_history` 只自动保留历史 system/developer，并不保证保留最初的 user 任务。

### 2.4 修复提示存在真实冲突

`kernel_prompt.py::_completion_followup` 给所有 structured report 拒绝追加“保留原问题答案，修 coverage 字段”。这不适合需要替换错误 claim 或缩短 finding 的情况。

`ResearchIssue.__str__` 又根据一个 `requires_acquisition` 布尔值选择提示。默认会说：

> This field repair does not require a new search or write.

但 `report_selection_too_large` 的正文同时允许“保存简短、受支持的替代 finding”。本轮已用纯函数复现这组矛盾文字。修复动作需要区分“重新选择”“依原文另存”“补读”，不能只区分是否采集。

## 3. 根因应该怎样分层

| 层次 | 当前问题 | 最直接的工程动作 | 仍不能保证什么 |
| --- | --- | --- | --- |
| 材料可达 | 直接答案在全文中，却不在本轮证据窗口 | 局部定位、补读、保留完整上下文及继续入口 | 命中词不等于证据支持 |
| 工作范围 | 一个 qN 内仍要求多个版本、动作和逻辑关系 | 在原问题下设置少量关系目标 | 拆题本身也可能遗漏要求 |
| 输入负担 | 全历史、全局要求、全工具继续累积 | 冻结局部作用域，投影实际请求 | 短输入不保证正确推理 |
| 结束压力 | 局部缺证据没有独立的正常提交边界 | 局部可无 finding 结束，父报告继续严格检查 | 模型声明 answered 不等于已证明 |
| 报告选编 | 字数发现太晚，模型反复提交全局选择 | 提前精确计数、错误定位到局部选择 | 短版仍可能漏条件 |

这些机制会相互放大：先看到邻近片段，过早形成错误 finding；旧结论进入历史；后续全局格式返工不断重现旧结论；编译器对精确版本进行忠实采用。当前资料没有提供各机制的独立错误占比，不能编造百分比。

## 4. 目标架构与事实所有权

建议沿用这一条生产执行路径：

```text
原始任务 + research_plan 原回执
  → 纯函数从计划、settled receipts 和当前拒绝推导下一局部工作
  → 原 kernel 冻结作用域、证据投影及工具合同
  → 原执行器检索 / 阅读 / 保存候选 / 提交局部选择
  → 原 effect settlement + 原 live/recovery projector
  → 程序组装全局提案
  → 原 completion gate 编译、检查
  → 唯一接受后的 FindingReport compilation
  → 原 publication effect 与知识事务
```

| 信息 | 唯一权威事实 | 可以存在的派生视图 |
| --- | --- | --- |
| 原任务和要求 | 原始任务文本及原偏移 | rN、格式约束、局部必要背景 |
| 研究问题和局部目标 | 原 research_plan 的成功回执 | 计划索引、下一工作 |
| 原文、已读范围、搜索结果 | 原采集 effect 回执；已归档内容使用原知识版本 | ReadPassage、搜索定位索引 |
| 候选主张 | 原 KnowledgeStore 精确版本 | fN 绑定、局部候选列表 |
| 局部选择和声明 | 新提交动作的 settled receipt | 当前有效选择、问题汇总 |
| 执行位置和已观察计数 | 原 checkpoint | 下一原内核动作 |
| 已接受交付内容 | 原 decision 中冻结的 FindingReport compilation | UI、导出、归档和恢复展示 |

单一事实来源不是禁止一切视图或快照，而是每种事实只有一个可写权威，派生物不另获修改权。冻结请求保存当时采用的投影，是原有请求审计事实；它不能成为可单独修改的另一份答案。

不要新建 `ResearchRunnerV2`、另一个 while 循环、可写的 current_unit 表、另一份答案正文或新的报告发布入口。`ResearchLedger` 与 `TaskSession` 的新增字段如果确有必要，也应由同一个回执投影器重建，不能在 live 路径直接写一次、恢复路径再另算一次。

## 5. 第一步：在原计划下定义局部目标

工作单位应是“一个需要回答的关系”，而不是一个句号、一条证据或新的顶层研究问题。

对现有 WAL 任务，可以先固定如下拆分验证：

| 原问题内的局部目标 | 必须共同保留的内容 |
| --- | --- |
| WAL 的引入边界 | 引入动作与版本；没有要求日期就不额外增加日期义务 |
| 只读 WAL 的许可边界 | 版本、适用对象、三个替代条件及例外；三个条件留在同一目标 |
| checkpoint 的停止边界 | reader end mark、为何停止 |
| checkpoint 的继续机制 | wal-index 记住进度、下次调用从何处继续 |
| writer 回绕复用 | 回绕动作自己的完整前提 |
| 两种动作的条件关系 | 两边原文和局部事实齐备后，再判断能建立什么关系 |

父 qN 和原始 rN 都保留。拆出来的 unit 是父问题的内部研究目标，不消耗新的 qN；也不是第二份可独立变更的计划。

建议扩展原 `research_plan` 契约，例子如下，**这不是当前可执行参数**：

```json
{
  "tool": "research_plan",
  "args": {
    "questions": [
      {
        "question": "原始复合问题的完整措辞",
        "task_refs": ["r2", "r3"],
        "work_units": [
          "确定引入 WAL 的版本",
          "确定只读打开 WAL 的支持版本及完整替代条件"
        ]
      }
    ]
  }
}
```

qN 和 `q1.u1` 等身份由程序按原计划稳定顺序分配，模型不再给局部编号起名。第一版保持顺序列表即可，不需要通用依赖图或关系 DSL；比较目标排在所需事实之后。没有必要拆分的问题只含一个 unit。

具体修改：

- `questions.py`：解析、校验计划，保留父问题及要求归属；已有问题和已开始工作的目标不可被悄悄替换或重排。需要追加时仍走原计划动作，按明确的追加合同处理。
- `tool_contract.py`：替换新 strict research 的规划 schema、说明和示例。删除与关系目标冲突的旧提示，但保留“不把找来源、写报告注册成事实问题”。
- `tools.py::research_plan`、`task_execution.py`：将新计划写入原规划工具回执。
- `ledger_receipts.py`、`kernel_facts.py`：让 live/recovery 从同一版本化规划回执投影。不要只修改 schema 而让恢复继续只接受 `questions: list[str]`。
- `task_requirements.py`：rN 仍从原文推导；实质要求的归属在计划中声明一次。能验证的实际执行要求继续看真实工具/effect 回执；格式约束继续由报告层检查。背景、方法限制不能因为没有识别成格式要求而丢弃。

程序能查归属是否合法、是否遗漏 rN，却不能证明某个自然语言目标真的覆盖了原问题。固定拆分先通过，再单独评估模型自主规划。

## 6. 第二步：冻结一个真正约束执行的工作作用域

建议给原 `TurnSnapshot` 增加一个不可变的 `research_scope`。它来自原计划及 settled receipts，不从“最近提到了哪个 qN”猜测。

它至少绑定：计划回执身份、父问题、unit 身份及目标、必要原要求、精确可用 passage/finding 绑定、当前待替换的选择回执，以及本轮是否为哪一处拒绝修复。

作用域是一个请求的合同。`next_work(plan, receipts, rejection)` 可以是 Research 领域里的纯函数；它决定下一次模型工作的范围，不拥有执行线程、重试计数、数据库或网络能力。原 operation 状态机继续决定何时发送、执行、交付和关闭。

**优先保留 `question_ref` 字段，冻结成当前 qN 的单值 enum。** 它已经由许多合同和回执使用。先消除选择负担，不必一开始就增加省略参数、有效参数、隐式绑定三套表示。如果后续实验表明省略字段值得做，再通过同一冻结作用域显式补齐，并保存原参数与绑定依据；模型显式给错 qN 必须拒绝。

本轮可见 passage/finding 也应来自这个冻结集合。保持已读 passage 跨问题复用：采集归属和新采用归属不同；共享原文不需要重开网页或复制知识对象。需要范围外材料时，允许明确补读/采用，再由下一请求扩大合法集合，不能静默选取没有呈现的内容。

关键修改位置：

- `kernel_protocol.py::TurnSnapshot/build_turn_snapshot`：同一 scope 生成模型合同、JSON/native schema、解析规则和执行约束。
- `provider_receipts.py::snapshot_description/restore_snapshot`：当前严格识别快照字段和版本，新增字段必须版本化。旧请求按旧合同恢复，不按新 scope 重新解释；实现不兼容时继续明确拒绝。
- `task_execution.py::ExecutionDelegate`：执行时读取原请求的 scope 校验，不能用模型回包时的可变 session.current_unit。通用 schema 校验之外，仍验证精确引用和真实归属。
- 原请求 provenance 与结果回执：保留 scope 关联，使保存、阅读和局部提交都能追溯到哪个原目标。它是 effect 的上下文，不是给 KnowledgeStore 另建 unit 所有权表。

不要只做 UI 隐藏。也不要只改 `model_tool_names`：这能减少呈现，但真正的授权、参数限制和执行检查仍必须受冻结合同约束。

## 7. 第三步：完整证据投影完成后，再裁剪实际历史

修改 `context_view.py`，增加可恢复的局部工作输入。建议一轮主要呈现：

1. 当前目标、完整父问题，以及原任务中影响它的背景/限制；全局语言、篇幅、禁止测量等约束不能丢。
2. 少量完整候选原文、来源精确版本、范围、passage_ref；保留必要相邻段和列表引导句。
3. 本目标的候选 finding、精确正文、当前有效选择；跨目标比较时明确加入所需两边证据。
4. 最近一次具体执行失败或拒绝，以及允许的修复动作。
5. 其他问题的简短进度；完整详情按需读取。

候选重建使用现有 `ledger.read_passages`、真实调查回执中的 passage_refs，以及 `session.search_results/hit_targets/source_ids` 等已有投影。冻结清单指向原 effect/source 版本。不要从缩略 display 文本反向提取 pN，也不要从 URL 重新联网读取以恢复旧证据。

### 完整证据与字数预算

现有 `_bounded` 最多显示部分 excerpt，并附截断标记，适合作为目录预览。局部最终判断不能把这种预览当作完整依据。`passages.py::describe_window` 还会在约 1000 字符处分段；完整条件可能跨 passage，不能假设“有 pN”就意味着一个关系完整。

优先利用现有 `additional_passage_refs` 连同必要相邻 passage，保持 OR/AND、例外、否定和跨句恢复机制。若材料放不下，应减少候选包规模、安排继续阅读或明确扩大必要预算；不要裁掉条件尾部。不能靠固定字符切分自动认定语义边界。

完整来源中的未读段可以提供章节/命中定位，但没有经过原读取动作时，不能凭本轮 view 自动生成新的“已读 passage”。新读证据仍走原工具路径。已有已读证据重新投影则不需要重复采集。

### 实际请求必须同时收窄

- `task_guidance.py`：按 scope 渲染本目标必要要求，避免每轮全量 rN 加全局 finding；原任务全文仍在原始事实中可读。
- `kernel_prompt.py`：规划时保留完整原任务；局部请求用明确的任务范围、必要原文和硬约束。裁剪后仍需显式携带任务锚点，不能依赖第一条 user 消息留在历史里。
- `kernel_context.py::freeze`：在局部证据可恢复后，为 strict research 使用原 `tool_call_history_groups`，开始可试最近 1～2 个完整协议组。组数本身不是 token 预算。
- 继续复用 `api_request_view.bounded_history` 和原 codec。native call/result、Responses reasoning 关联必须成组保留；禁止直接 `messages[-4:]`。
- 移除当前研究范围不适用的工作区编辑、测试和 shell 说明；保持原任务策略所需内容。原工具合同与 schema 从同一冻结定义生成，避免再维护“12B 专用工具协议”。

首轮可用“全部实际输入约 4K～8K token”作为试验起点，计入合同、schema、原文、历史和反馈；不是新的强制截断标准。协议完整组和必要证据可能使某轮更大，必须记录原因。验收读取真正 `admitted_payload` 和提供方 usage，不能只统计 view.render()。

## 8. 第四步：局部选择与缺口提交

建议增加普通领域动作 `submit_research_answer`，结束当前冻结 unit 的一次回答选择。名字可以沿用这个建议；身份由 scope 绑定，不让模型再交整份 coverage。

建议新合同只要求 `state` 和 `findings`，局部 state 可收窄为三种声明：

```json
{"tool":"submit_research_answer","args":{"state":"answered","findings":["f3"]}}
```

```json
{"tool":"submit_research_answer","args":{"state":"partial","findings":["f3"]}}
```

```json
{"tool":"submit_research_answer","args":{"state":"unresolved","findings":[]}}
```

这是建议的新局部枚举，不替换知识状态，也不直接替换最终报告四种 coverage 状态。`answered` 只表示模型自称完整回答；程序只能检查结构、身份、精确依据和可执行义务。

- answered/partial 需要非空 finding。
- unresolved 需要 findings=[]，不要求创建邻近事实来换取结束。
- 真正没有任何候选时，从调查阶段也能提交 unresolved；不能把提交工具藏到“必须先成功 save”之后。
- 阅读、知识采用和必要恢复路径始终可达，避免严格阶段限制把模型锁在错误材料上。
- 提交动作应独占本次工具批次，避免同一回包先提交并跳到下一 unit 后又执行旧 scope 下的其他动作。

提交成功回执只保存选择事实：原计划和 unit 身份、精确 finding 绑定、状态、原请求身份、被替换的旧选择回执。正文继续仅存在原知识版本里。

重新选择使用显式的前一回执身份：原拒绝冻结了待修选择，成功新回执替代它；重放确定相同的有效版本。不要就地覆写选择表，也不要没有前提地“取最后一条随便覆盖”。重复恢复同一 effect 不应再推进一次。

### 父问题汇总

程序从有效局部回执组装父 qN：

| 局部结果 | 父 coverage 的派生方式 |
| --- | --- |
| 所有必要 unit 均声明 answered | evidence_found，仍标明未做语义认证 |
| 部分回答成立、某些 unit partial/unresolved | partial，列出未完整回答的原目标 |
| 没有选中任何 finding，有真实调查尝试 | unanswered_after_search |
| 没有选中任何 finding，也没有调查 | not_investigated |

调查状态依据真实回执，不能因为保存过一个其他 unit 的 finding 就声称本目标调查过。若需要显示 unit 级调查轨迹，沿原请求 scope 及采集回执关联派生；重新投影旧证据不能伪造一次新的 web_search。父 qN 的调查列表可以继续使用原真实 qN 回执的并集。

局部 partial 或 unresolved 对应的目标必须进入父报告缺口，而不只是“尚未完整回答”的泛化一句话；目标本身来自计划，不需要模型再写一份无依据的补充答案。

父问题内部可以对同一个精确 finding 版本重复采用去重。不同 finding 即使相似，也不能自动视为重复；共享来源更不能推出内容相同。去重后的计数与正式编译必须同源。

### 整份报告与本题结束分开

不要每结束一个 unit 就调用 `compile_finding_report`；当前编译器要求整份报告至少选择一个 finding，这是父报告准入，不是局部准入。

所有局部结束后，程序再组装完整父 coverage，进入原 gate。如果所有问题都缺证据，局部仍可全部结束，但严格报告继续不能发布成功。程序应以真实缺证据原因走现有未完成关闭路径，留下原调查回执，不能再次要求保存一条事实从而制造无限循环。

具体接线位置：

- `tool_contract.py`、工具注册与 `task_execution.py`：新增一个领域动作；不新增执行器家族。
- `kernel_facts.py` 和原 receipt 校验：同一 live/recovery 投影局部选择。
- `kernel_context.py::deliver_results`：借鉴已有 settled research_review 提交触发完成检查的边界，继续使用原状态机。不是照搬“review_complete=true 就结束研究”；必须确认所有必要 unit 均有有效提交。
- `kernel_control.py` 与 `research_completion_checks.py::prepare_research_completion`：显式接收“程序由哪些回执组装的提案”；不能塞入 `last_done_args` 冒充模型提交过全局 done。
- `report_proposal.py`、`finding_report.py`：复用同一个 coverage 校验、绑定和编译入口。组装提案的 digest 绑定原计划身份、有效局部回执和采用的精确版本。
- 原接受路径仍冻结一份 compilation；恢复已接受结果直接读取它，不从今天的局部回执或最新知识重新编译。

## 9. 第五步：字数、替换与局部修复

先提取一个共同计数函数，正式检查和过程反馈都调用它。当前合同计数的是结论的非空白字符，排除引用；不是整份 Markdown 字数，也不是模型 token。

每次保存回执可返回本条 count；局部选择回执可返回采用后的父问题 count 与当前报告预估。中途报告尚未完成时，低于总字数下限不应拒绝局部提交。局部预算先作为软目标，最终由正式 compiler 检查上下限。

字数必须包括最终会进入结论的程序缺口、partial 通知和去重结果；不要建立另一份手算预算缓存。预算改变时，重新从原选择及知识版本计算。

具体错误反馈应类似：

```text
结论 1443 字；允许 640～960；至少需减少 483 字。
q1 选入 f1/f2/f7，共 346；q3 的 f4 占 381；q4 的 f5 占 296。
当前修复范围：q1 的选择。
可以减少相同版本的重复采用；或在完整原文上保存简短替代 finding。
新内容须保持原条件。重新选择成功后再检查全局长度。
```

上述数值来自旧完整 WAL 的已有选择重建，不是建议系统自动判定 f1/f2/f7 语义等价。先报告相同引用、相同文本、共享证据等可核验信息；不能按相似度自动删掉条件不同的主张。

建议扩展领域诊断为小型、可序列化的修复说明，例如 `code/path/unit_ref/allowed_actions/details`。`allowed_actions` 表示可修途径，不是新的状态机：

| 错误 | 指向的修复 |
| --- | --- |
| coverage/原要求登记 | 修改已有归属或选择，不重写正文、不重搜 |
| 非法引用/版本 | 读取合法精确版本或重选 |
| 缺直接证据 | 当前目标继续定位与补读，或者局部 unresolved |
| 过长/语言不符 | 当前选择调整；必要时依据原文保存替代 finding |
| 实际执行义务未完成 | 只补缺少的已授权动作 |

让 `ResearchIssue` 保留结构，`prepare_research_completion` 不要立即包成普通 ValueError 而丢掉 code 和动作；`GateVerdict` 传递这份领域诊断，`_completion_followup` 按它生成提示。未知异常继续沿原失败处理，不从错误字符串猜控制动作。

`KernelProgress` 仍由原 checkpoint 持久化。对局部提交和拒绝增加稳定的作用域/选择/错误身份，确切重复和短循环继续有界。对仅仅换了措辞的新 finding，程序不能直接声称它没有语义进展；但可以在同一修复 episode 设置明确的已有轮数预算，且保存新 finding 不重置该预算。若要新增计数，应放入原版本化 counters 并原子提交，不增加另一张 retry 表。

不建议只调大 max_turns。工作包变小可能需要更多调用，应先测量合理预算，再按真实需要调整原预算；无限纠错只会增加费用和重复材料。

## 10. 跨动作比较与语义检查的边界

先提取 checkpoint 停止/继续事实和 writer 回绕事实，再在一个专门的比较 unit 中同时给出两边完整原文及精确 finding。比较目标仍来自用户原问题。

程序不能从两个局部回答直接推导必要、充分、当且仅当。某一边缺失时，比较保留缺口。

已有 `save_inference` 可保存跨前提推论，明确 candidate/unreviewed 和 limitations；不同来源间的比较优先沿现有 inference 路径，不为了合并材料放宽 `save_finding` 的同一来源版本限制。当前 inference 要求前提属于当前 qN，跨问题采用应通过已有精确 knowledge_read 绑定，复用知识对象；不能偷偷改写原前提归属。

不把强制全篇自审重新塞回默认流程。同模型局部核查是否有增益，可以作为后续受控实验：只看当前目标、claim 和完整依据，检查主体/否定/条件/比较范围。它仍是模型判断，会相关失误，成功回执不能升级为语义证明。第一批不必为了这个实验再增加强制评审轮。

## 11. 建议提交顺序和验收

| 批次 | 具体交付 | 合并前重点检查 |
| --- | --- | --- |
| A | 统一计数；修复动作反馈消除矛盾；保存前候选原文的恢复投影 | 既有格式边界不变，完整条件不被预览替代，恢复不联网 |
| B | 原计划内 unit、冻结 scope、同源工具范围、局部提交 | 空证据 unit 能结束并进入下一项；错 scope 拒绝；无第二控制循环 |
| C | 用 scope 渲染局部请求；完整协议组历史投影 | 实际请求不持续按总历史增长，任务锚点和硬约束不丢，JSON/native 一致 |
| D | 原 gate 组装全局提案、局部选择替换、字数修复、发布恢复 | 原 qN/rN 都有回答或缺口；接受后恢复完全使用原 compilation |

范围窄的第一批可先评估；B/C/D 构成同一目标流程，不能把其中间状态对外宣称已解决 Research。新 strict 合同上线后替换旧的全局 coverage 往返；保留旧冻结请求的版本化读取，不长期维护两套可选生产运行器。

需要的关键测试是行为边界，不是给每个 helper 再写一份相同实现：

1. 同一 qN 两个 unit：前者 answered，后者空 finding unresolved，第三个目标仍可继续；父 qN=partial，明确显示后者目标。
2. 全部 unresolved：全部局部可结束，无伪造 finding，无成功研究发布，不在完成检查里无限重试。
3. 原文只有开头片段、目标在后文：能经 source_search/open_hit 补读；没有补读时不得伪造新 pN。
4. OR 列表、跨句恢复机制、同来源多 passage：完整保留；预算不够时显式继续或扩大，而非静默裁尾。
5. scope 冻结后出现新回执/新知识：原回包继续按原 scope 校验；错误 qN、范围外 fN/pN、过期替换基准被拒绝。
6. 原网页已经变化：恢复使用原 source receipt/knowledge revision；此前跨题读过的 passage 仍可复用，无重复知识正文。
7. native/JSON、原生多工具批次、调用与返回配对一致；提交不与其他动作混批。
8. 在局部提交 settlement、下一请求冻结、全局 gate 接受、发布前后分别中断：每项只提交一次，计数不重复，发布正文一致。
9. 字数刚好等于上下限、含 partial/缺口通知、相同版本重复采用：过程预估与正式检查一致。

## 12. 真实模型实验：怎样知道方案有效

先固定两个复合题：版本与只读条件、checkpoint 停止与恢复；加入缺直接证据的单写者反例、肯定/否定和主体替换反例、本机测量范围题。评分仍按原完整问题，不能拆小后降低通过要求。

原脚本 `research_evidence_local.py` 用同一个 seed 既洗牌候选又控制模型。应将采样 seed 与候选顺序控制分开；源快照、模型、参数、工具结束合同、最终评分规则保持一致。

建议先记录原版基线 R0，再在**同一新局部提交合同**上做四组实验：

| 配置 | 目标拆分 | 实际请求投影 |
| --- | --- | --- |
| A | 一个复合 unit | 完整历史 |
| B | 多个关系 unit | 完整历史 |
| C | 一个复合 unit | 局部有界投影 |
| D | 多个关系 unit | 局部有界投影 |

如果 A 沿用旧 done、D 换了新局部提交，就同时改变了第三个因素，不能把差异全归于拆题/上下文。R0 用来比较整体改善；A/B/C/D 用来区分两个设计因素。首轮每配置、每题至少 3 个固定采样 seed；这只是筛查，不能当统计充分的成功率保证。

依次推进：给定正确完整证据 → 有邻近干扰的候选材料 → 自主检索 → 原完整 WAL → 中断恢复。固定证据仍出错，就暂停扩大完整任务，把错误定位在理解/表达/选择环节；候选层才出错则重点改定位与呈现，不继续改发布机制。

至少分别记录：

- 完整条件与原始目标的覆盖率；完整回答的整题通过数。
- 明确错误肯定、把局部缺失扩大为整页不存在、答非所问。
- 缺证据的合理 unresolved，以及明有答案却过度保留未知。
- 自主定位能否把直接依据实际送进生成请求。
- 实际输入 token 的中位数/最大值、累计输入、调用数、生成与端到端耗时。
- 重复保存、重复拒绝、选编返工和未发布的原因。

不要用“done 多了”“全部 unknown”或离线测试通过当作内容改善。分母按完整原题保留，partial、正确缺口、错误回答分别报告。若 D 在完整证据和明确单关系上仍反复错，就应承认这套部署在这些关系上仍有限制；工程可以提高可诊断性和诚实结束能力，不能预先承诺靠更多协议消除限制。

## 13. 本轮验证和资料

本轮审计资料：`.e2e-artifacts/research-local-scope-audit-20261101/`。

- `audit.py`、`audit.json`：原始请求计数、直接原句可见性、精确 claim/excerpt 的 helper 重放、定位实验、源码和历史文件哈希。
- `run_offline.py`、`pytest.xml`、`network-audit.json`：当前相关既有测试 **105 passed，6.12 秒**；网络连接尝试 **0**。
- 审计前后 578 个生产 Python 文件指纹相同。检查 HEAD 未变化；本轮不覆盖原诊断报告或原运行资料。

运行命令：

```powershell
python -X utf8 .e2e-artifacts/research-local-scope-audit-20261101/audit.py
python -X utf8 .e2e-artifacts/research-local-scope-audit-20261101/run_offline.py tests/test_research_context_priority.py tests/test_research_question_ownership.py tests/test_research_gap_compilation.py tests/test_research_report_requirements.py tests/test_research_answer_focus.py tests/test_research_report_snapshot.py tests/test_kernel_prompt_task_guidance_ownership.py tests/test_kernel_provider_protocol_boundary.py -q --junitxml=.e2e-artifacts/research-local-scope-audit-20261101/pytest.xml
```

105 项验证的是当前行为边界，不是新方案已实现或实机收益已获证明。本轮最优先的实现目标是：**完整证据的局部作用域、允许空证据局部结束、实际请求投影，配套明确的局部选编修复**。这四项应在同一原 kernel 和原事实链上完成。
