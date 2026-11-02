# Research 请求减负与知识闭环：函数级收敛方案

日期：2026-11-02。本文是设计与实施顺序，不是已实现或已验收声明。最初审阅基线为 `de3edcb1` 加当时工作区；实施基线更新为 `751c6f46`（知识通道、个人归档和部分修复诊断已提交），不重复实现已经合入的部分。逐批实施与实机记录另见 `research-four-batch-implementation-20261102.zh-CN.md`。

**实施状态更新：A 已提交为 `13a756ea`。B 组合投影与后续最小导航实验均未证明整体收益，完整留档后退出活动代码；当前生产源码回到 A。C/D 暂缓，下文 B/C/D 保留为待验证设计，不能按原四批顺序自动继续。** 见 [实际实施与回测记录](research-four-batch-implementation-20261102.zh-CN.md) 和 [逐例实验数据](reports/research-navigation-experiment-20261102.json)。

**后续局部实施：在 A 上仅修改 `_repair_prompt()`，完整保留缺提交动作时的原正文。** 23 项定向、98 项扩大相关测试通过；12B 八例均正式交付，三例实际修复完整带回原稿。用户确认保留；这不代表 B 或整批 C 已验收，D 继续暂缓。见 [完整正文修复实测](research-prose-repair-experiment-20261102.zh-CN.md)。上段“生产源码回到 A”描述的是此次局部实施之前的状态。

随后完成 [旧审稿生成支线清理及 A/B](research-deadcode-cleanup-ab-20261102.zh-CN.md)：删除无产品入口的同模型审稿器与专用内核分支，历史读取继续保留；未重新启用本方案的 B/C/D。普通入口与仍有调用的 finding/inference 能力保持原边界。

源码采样记录见 [函数与指纹清单](reports/research-function-plan-baseline-20261102.json)，相应只读副本在 `.e2e-artifacts/research-function-plan-20261102/source/`。本轮仅新增设计文档、采样脚本及记录，不修改生产源码，不运行模型或改变服务状态。上一份分析中的“77 项通过”来自用户提供的另一轮审计，本轮没有重跑，不能作为本方案已通过的测试数。

## 1. 最终决策：改边界，不重写运行时

**内核需要少量修改，集中在请求冻结、失败响应投影和历史格式读取。现有迁移图、重试计数、effect 调度、共同完成 gate、取消与发布事务应保留。** 不新建 ResearchKernel、研究队列、后台提炼代理或图数据库。不把每个问题重新变成 finding/采用/coverage 流程。

目标路径为：

```text
原任务 + 稳定工作原则
    ↓
同一个模型：检索旧知识、阅读原文、必要时继续调查
    ↓
一次 submit_research_answer：正文 + 完整性声明 + 可选的明确知识更新
    ↓
原共同 gate：校验、编译、冻结全部提交内容
    ↓
原 publication effect / 原知识事务：来源、证据、知识更新和唯一 answer
    ↓
从原知识版本派生图谱、检索索引和展示
```

“一次提交”不表示整个研究只能读取一次，也不表示每篇答案必须产出知识更新。它表示普通整篇研究默认只有一个最终内容提交入口。用户明确要求积累知识时，更新本身属于交付要求；没有更新不能仅凭报告发布就称长期研究目标已经完成。

本文把此前“可选 knowledge_commit”的方向进一步收敛：它可验证原仓库支持长期对象，但直接把通用仓库协议暴露给 12B 仍重，而且存在新证据入库时序缺口。**推荐普通整篇研究最终以 submit 的可选知识批次承担积累，通用 knowledge_commit 保留给原有明确知识编辑用途，不让同一次普通研究同时用两种方式写同一批新结论。** 此合同修改放在输入减负验收之后，不能第一批一起改。

## 2. 当前工作区已做到什么，仍缺什么

| 检查点 | 当前工作区状况 | 实施处理 |
| --- | --- | --- |
| 知识入口 | ANSWER_TOOLS 已加入 knowledge_search/read/commit | 不重复接入口，检查真实冷启动链；后续统一普通研究写入口 |
| 无项目归档 | archive_scope 已默认 personal，保留显式限制范围 | 保留方向，补权限及混合范围用例，不事后批量扩公开权限 |
| 历史材料 | 新 knowledge_materials.read_material 将 source/evidence 精确版本带入 eN | 保留方向；与本轮来源共用引用和预算逻辑 |
| 阅读范围 | read_intervals 已表达中间缺口、first_unread_interval | 验收已有实现，不另建进度账本 |
| 遗漏窗口 | answer_context 已列出恢复位置 | 当前全部列表会随来源增长；正常请求应提供有界摘要与完整索引定位 |
| 协议失败 | 已记录响应 hash 和 600 字符 preview，并识别重复修复 | 有诊断进展，但预览不能保存长正文，仍需完整失败响应引用 |
| 引用图 | 按精确对象版本引用画边 | 保留边的语义 |
| 反向扩展 | neighbors SQL 仍只匹配 r.target | 补 target_revision，不改变“引用”与“支持”的区别 |
| 答案与旧结论 | knowledge_read 能给 fN；整篇 citation_occurrences 只解析 eN | 读取与实际采用尚未形成完整闭环 |
| 知识新增 | 通用 knowledge_commit 要求目标已存在；本轮证据通常在最终发布时才落库 | 不能把“旧证据可复用”的测试当作“新材料可立即积累”的证明 |
| 大资料 | 默认读取 6000，工具上限 12000，实际正文还预留 2500；答案视图预算 18000 | 必须联合处理，不能单改默认常数 |

### 新证据的时序缺口为什么重要

`ExecutionDelegate._commit_research_knowledge()` 不允许模型提交 source/evidence/answer；`ResearchTools.knowledge_commit()` 要求引用目标已在库中或在本批 mutations 中。新网络材料对应的 source/evidence 却通常由 `prepare_evidence()` 在最终发布时生成。模型无法用一个 eN，直接填入尚不存在的数据库 evidence ID。

现有 `test_research_long_term_loop.py` 的主要循环先完成一次研究，再复用其已经入库的证据保存 claim/inference。它能验证跨研究复用的部分边界，不能单独覆盖首次研究的新材料积累。解决方式不应是让模型手写来源快照，或旁路提前保存另一份证据。

## 3. 批次 A：先修确定性正确性与历史读取

这批不改变模型提示与提交合同，便于独立评估。

### A1. codey/knowledge/recall.py — KnowledgeRecall.neighbors()

修改反向 SQL：

```sql
WHERE r.target = ? AND r.target_revision = ? AND ...
```

参数增加 `version.revision`。现有 `(target,target_revision)` 索引已经存在，不必新建关系表。保留访问范围、状态过滤和当前“入边来源取 head”的策略；该策略与目标版本匹配是两件事，不应顺便把所有历史版本灌入默认检索。

补充在合并出边和入边后按 `(id, revision, relation)` 去重，再按预算返回。同对象其他版本如需展示，应明确标为版本历史，不作为当前版本的精确邻居混入。

配套检查 `KnowledgeRecall.search()` 与 `build_version_graph()` 使用同一精确版本邻居逻辑；不在前端再补一份过滤规则。

### A2. codey/operations/kernel_receipts.py — restore_result_receipt()

保留 JSON artifact 新写入方式；补明确旧格式读取：

- `out_` 旧受管文本回执：使用 `ManagedOutputStore.read_tool_output()` 读取原始字符串；校验截断标志、session/run、摘要与原 effect 绑定。
- 新 JSON artifact：继续使用 `read_json_artifact()`，核对 schema、范围和摘要。
- 未知句柄或版本明确拒绝；不能捕获任何错误后盲试另一读取器。

**旧摘要必须用旧原始字符串核验，不能先 parse 再按 sort_keys 重新序列化。** `71624a43` 同时改变了存储载体和 JSON 排序，新旧字节并不必然相同。读入后共用现有字段、参数摘要、call ID、结果状态、工作区归属等验证。

`result_receipt_fields()` 只写新格式。若旧 metadata 不能提供充分归属证明，明确拒绝或要求离线迁移，不能弱化成“句柄存在即可恢复”。

### A3. codey/operations/kernel_contracts.py — KernelResult.to_payload()/result_from_payload()

建立明确形状分发：已知旧六字段结果、当前七字段结果，以及带显式版本的新结果。旧六字段只补语义确定的 `research_report=None`，不推断已经发布、不补成功 proof。

每个版本分别检查完整字段集合和类型，拒绝未知字段或未知版本；不能把现在的严格检查替换为一串宽松 `.get()`。相关 `validate_decision()`、done/closing 读取共用同一解析函数。

保留 implementation_id 的执行恢复限制。支持读取历史数据，不等于允许任意旧代码任务跨实现继续执行。

### A4. codey/research/answer_archive.py — archive_scope()

验收当前 personal/project 默认范围；已有显式 session/project/personal 限制继续继承。混合互不兼容的限制范围应拒绝或走已有明确授权机制，不能简单选一个范围后泄露另一份资料。

不自动把所有旧 session 数据迁成 personal/global。历史迁移是独立、可核查的数据操作，不应藏在一次研究读取中。

## 4. 批次 B：请求角色分层，第一阶段保持 submit 合同不变

### B1. 请求应该长什么样

```text
system/developer：固定研究工作原则、当轮工具合同
user：原任务原文及已授权的真实后续补充
assistant：实际发生的工具调用
tool：对应调用返回的获准原文、eN、来源版本、范围和完整/截断状态
资料上下文：仍需保留的较早证据与旧知识，明确标成数据
必要的修复反馈：仅在本次失败时出现
```

第一批仍提交 `{markdown, state}`。不要复制 OS 的全部 26K 字符提示，也不同时改成自然正文完成。

### B2. codey/operations/kernel_preparation.py — PreparedKernelTurn/prepare_kernel_turn()

把当前一个大 prompt 的职责拆成纯投影值：固定工作原则、真实任务消息、必要的数据上下文。可以增加一个版本化的不可变 `request_projection` 字段；它是当次请求的派生输入，不是新的任务状态。

在同一次准备中确定可见证据和冻结 scope，保证二者一致。内容来自原任务记录、已接受的用户边界消息、原读取回执和冻结声明；不从旧渲染 prompt 用正则反向提取任务。

### B3. codey/operations/kernel_prompt.py — kernel_prompt_for_session()

抽出纯函数，例如 `research_instructions()`，承载通用的证据边界、逐项回答、来源是数据、显式引用和提交方式。移除原任务与相同完整指导在多个块里的重复拼装。

原函数继续作为既有文本 provider 的渲染入口；渲染相同投影为文本，不维护另一套任务规则。编码任务及显式旧计划合同不在这批重写。

### B4. codey/research/answer_context.py — answer_context()/investigation_history()

把当前“证据 + 全部导航 + 回执 + 历史 + 原问题 + 完整指导”的大字符串拆成：

- 可见证据的数据块；
- 已读旧知识的数据块；
- 少量当前有用的定位/错误；
- 省略项的数量、稳定恢复入口。

原任务不再从这里重复输出；稳定工作原则不再从这里追加。机器回执仍在原日志，正常请求不需要展示整个调查历史。

完整恢复索引仍从回执派生，可通过原 `read_tool_result` 或既有来源定位获得；不能为缩短提示而让较早材料彻底失去入口，也不能把所有省略窗口无限展开到每个请求。

### B5. codey/operations/task_guidance.py — task_result_context()

对最近一次真实来源读取，在对应 tool 内容里呈现获准原文和稳定 eN，替代只有“正文在另一视图”的指针。与历史资料上下文做确定性去重：同一获准窗口在实际 payload 中只展示一次。

必须继续核对当前冻结 scope，不能因为原 tool 回执里有完整抓取文档，就展示尚未实际读取的全部正文。裁掉的范围不能继续假装显示。

### B6. codey/operations/kernel_context.py — freeze()/prepare_turn()/deliver_results()

这里保留执行编排，只负责把领域投影接入现有请求冻结：

1. 领域函数生成投影；
2. freeze 将投影作为原 provider request artifact 的一部分固定；
3. 原 prepare_provider_request 承担原子准入与发送；
4. 恢复读原请求，不重新从今天的知识 head 生成旧输入。

不把 source/claim 解析规则继续堆进 KernelContext。已有 `tool_call_observations` 在普通整篇路径逐步替换为结构化投影，不能新旧两份数据都发。

### B7. codey/providers/api_request_view.py — bounded_history()

保留完整 call/result 协议组。新增的任务前缀通过明确投影字段进入当前请求，不依赖“第一个 user 一定是真实原任务”的启发式，也不把所有 user 都保护下来。

对实际保留的 tool 组使用对应数据呈现。更早的获准资料用数据上下文承载，不伪造 tool_call_id、不把旧证据塞成最后一次调用的新结果。

### B8. codey/providers/api_provider.py — send*/_exchange()/_generate()/_payload()/_admit_request()

在现有方法链传递单一版本化请求投影；`GenerationSettings`/`ApiCodec` 按需要扩展。provider 只编码消息和执行预算，不理解 eN 的研究语义。

- `_payload()` 按 codec 支持的角色编码前缀、真实工具组和数据块；来源数据不得进入 system。
- 不通过永久修改 `self.system_prompt` 实现，否则会污染其他任务。
- `_admit_request()` 对完整最终 payload 计数，包括 system、原任务、工具 schema、材料和输出预留。
- **冻结后不得静默删证据而保留原 scope 的引用许可。** 若需要缩小证据，必须在发送前形成新的同一请求准备结果并重新冻结；不是改写已冻结请求。
- 冻结时钟放入明确环境上下文，避免在末尾反复制造像新任务的 user 消息；仍使用原已冻结观察值。

`prepare_provider_request()`/`decode_provider_invocation()` 只增加这个值对象的序列化与版本验证，不增加新的 provider effect 类型。相关 provider 的方法签名、文本降级和冷启动读取必须一起更新，不能只让热进程有新字段。

### B9. 本批不变的边界

普通整篇仍使用 submit；不自动执行正文里的伪工具调用；不把缺少工具动作的自然文本默认发布；不新增研究规划或审核轮次。这样可以单独测量输入界面是否改善内容。

## 5. 批次 C：修复要看到完整失败响应，阅读要共享真实预算

### C1. kernel_prompt._repair_prompt()

目前 600 字符 preview 可以帮助识别错误，但不能保护长答案。将“错误原因”和“失败响应正文”分开：错误原因简短；原完整响应从已保存 provider response 引用进入下一请求的数据部分。

优先保留已有 assistant 消息；若 codec 不能回放畸形工具消息，将它按原响应字节作为明确的被拒数据呈现，不能变成可执行调用。不要同时展示历史完整正文和另一份完整拷贝。

需要明确告诉模型本次是缺工具动作、参数格式错误、引用不合法，还是内容交付缺项。只有结构错误时不要求重新开展研究，但也不能保证模型复制一定保持正文一致。

### C2. KernelContext.interpret()/retry()；kernel_control.prepare_control_delivery()

当前已接入 reply 和重复修复检测，应保留这部分进展。进一步让修复投影绑定：

- 失败 provider response 的原引用；
- 对应冻结 snapshot/scope；
- 具体校验错误；
- 下一次重试使用的原子计数。

对纯协议修复，不能因进入下一次发送就重新解释原引用许可。若有新用户输入或新调查，则按原边界流程形成新请求，不能混作“原样重发”。

重复检测使用规范化错误和失败内容摘要，忽略随机 call ID；沿原重试上限停止。必要诊断都由原回执派生，不新建失败答案表。相同 seed 的无信息变化重发不得持续占用研究预算。

若完整失败响应在当前上下文预算内放不下，应明确停止格式修复并保留未交付正文及原因，或使用现有可定位读取机制；不能只显示前 600 字符却声称保留了全部答案。

### C3. source_gateway 常数/ResearchTools.open_url()/_open_document()

当前 `OPEN_DEFAULT_LIMIT=6000`、`OPEN_MAX_LIMIT=12000`，`_open_document()` 又固定减去 2500。不要仅把 6000 改成 20000。

抽出一个纯预算函数，例如 `choose_read_window(document, requested_offset, requested_limit, budget)`：

- 用户/模型显式指定的 limit 不擅自扩展；缺省读取才可选择能放下的完整小中型文档。
- 大文档给真实连续窗口，尽量停在自然段/句子边界。
- 元数据、正文、当前已有材料、工具 schema 和输出预算共同计入。
- 明确区分抽取结果 EOF 与远端来源完整；上游 truncated 不能被改成“整篇已读”。

预算来自原请求/任务配置并被固定，不新增一个可写的材料预算账本。可以先用保守字符上限控制工具输出，再由实际 token 准入核对；若要自适应扩大，最终发送前必须完成实际计数和 scope 一致性验证。第一实现宁可选择保守窗口，不能读取后静默裁许可。

`ResearchSourceGateway.open()` 自身也会按上限裁 limit，`tool_contract` 的 open_url 参数声明也必须一致更新。不能只改 `_open_document()`，否则上游仍会按旧值处理，或模型无法按新上限调用。上限由共同配置派生，避免再增加互相矛盾的三个常数。

### C4. ExecutionDelegate._execute_research() 与 knowledge_materials.read_material()

当前 `_execute_research()` 在 open_url 分支把缺省 limit 直接填成 6000。若引入自动整篇，必须保留“省略 limit”与“显式 limit”的差别，不能只改 ResearchTools 默认参数。

历史 source/evidence 与网络来源共用窗口选择与展示逻辑；历史读取始终固定到原版本，不能为多读一段就换成实时 URL。长历史 evidence 若超预算，应提供其固定 source 的续读定位，不复制出一个新来源事实。

### C5. answer_scope()/read_evidence_projection()/source_rendering.section_locators()

- `answer_scope()` 分开处理稳定标签分配和当前可见窗口选择；标签由原回执顺序派生，窗口完整保留，引用集合严格等于实际展示集合。
- 现有 18000 字符不是通用真理。统一读窗口与可见材料预算，否则刚读出的完整文档仍可能整窗落选。
- 当前 `read_intervals()` 的中间缺口逻辑应保留。定位分组必须包含精确来源身份/版本，不能只按 URL 归并；相同 URL 的旧知识版本不能被新抓取覆盖。
- 目录明确标出总章节数、已显示与省略数量；首尾摘要只作导航，全文章节查询复用现有 source_search 的文本定位能力。
- `source_search`/`open_hit` 恢复旧来源时，应携带原来源版本/窗口身份，在已归档文本内定位；不能用旧定位查询新网页。

不添加关键词硬编码“第44节”，不让框架根据评分答案替模型选证。

## 6. 批次 D：一次提交同时积累明确知识，保住图谱能力

这是模型合同变化，须在 B/C 固定后单独验证。推荐普通研究的唯一积累入口为 submit 的可选字段，避免通用 knowledge_commit 再要求模型管理数据库 ID、revision、commit_id、scope_id 和完整 KnowledgeObject。

### D1. 推荐的模型提交形状

```json
{
  "markdown": "取消 gather 会传播到尚未完成的子任务，且不受 return_exceptions 影响 [e1]。这是本次保存的规则 [k1]。",
  "state": "answered",
  "knowledge_updates": [
    {
      "key": "k1",
      "kind": "claim",
      "content": "取消 gather 会取消尚未完成的子任务；传播不受 return_exceptions 影响。",
      "conditions": {
        "version": {"state": "reported", "value": "Python 3.12"},
        "operation": {"state": "reported", "value": "取消 gather 本身；只传播给尚未完成的子任务"}
      },
      "references": [{"ref": "e1", "relation": "supported_by"}]
    }
  ]
}
```

普通答复不需要 `knowledge_updates`。`eN` 仍表示实际阅读证据；`fN` 复用现有已读旧结论短引用；`kN` 只表示本次提交中的局部条目，不新增持久化别名注册表。模型可以在正文中明确引用采用的旧/新结论；没引用的知识可以保存，但不自动成为整篇答案的依赖。

修改已有知识使用 `update_ref:"fN"`，目标 ID 和 expected_revision 由冻结绑定决定。新条目支持引用同批 kN，程序检查唯一 key、有效关系类型、作用范围和有向依赖；不能允许一个推论通过自己证明自己。

第一批知识类型可限制为 claim/inference/question，覆盖结论、机制推论、待核验问题，避免把实体本体建模也塞给 12B。机制不是必须新增的数据库 kind：已有 inference、conditions、depends_on 足以表达。后续有明确需要再扩展 observation/entity。

**“新证据反驳旧结论”要遵守仓库现有关系方向：旧 claim 的新修订 `refuted_by` 指向证据；新旧观点不能随意用一条 repository 不支持的 claim→claim 反驳边代替。** 前端可显示反向关系，数据库仍保留原语义。

### D2. answer_contract.AnswerScope/answer_scope()/answer_spec()

保留现有 eN→passage 绑定；增加冻结的旧知识短引用绑定，来自本轮确实完整读取的 `ResearchReference`/读取回执，不能由检索摘要自动准入。fN 绑定包含精确对象版本、种类和原读取依据。

`AnswerScope.to_payload()` 当前已经是手写字段列表；新增字段必须显式更新它、`from_payload()` 和 `schema_version`，不能以为改 dataclass 就会自动冻结。相关 provider snapshot 的写入/恢复也必须按所捕获合同分发。

`answer_spec()` 对 submit 增加可选、数量有界的 `knowledge_updates`，删除让模型手填系统元数据的字段。普通整篇 ANSWER_TOOLS 不再同时默认暴露原始知识批量写合同；已有独立知识编辑入口继续使用原 knowledge_commit，不删除底层 API。

这不是把同一个动作做成两种 runtime：普通研究选定一个提交合同，旧会话按其冻结版本读取，其他已有任务的显式知识编辑合同按原授权执行。

### D3. citation_occurrences()/submit_answer()/validate_submission()

按类型解析 eN/fN/kN，并继续排除代码块、代码示例和转义，不把正文里随便出现的标签当执行指令。eN 只能来自当前可见原文，fN 只能来自冻结已读知识，kN 必须在本次显式条目中定义。

当前“有证据才可声明 answered/partial”的底线保留，引用旧结论不能自动替代阅读它的依据；未读的原证据不因为 fN 出现就被授权。

`submit_answer()` 校验并冻结模型原始知识条目与引用，不在这里写库、不从 markdown 自动抽取结论。摘要覆盖正文、状态、全部更新和精确绑定。`validate_submission()` 按原版本复验，禁止后来多加知识条目或切换旧结论版本。

### D4. 新增纯函数 compile_knowledge_updates()，建议位于 answer_archive 或独立小模块

输入：冻结提交、原 ledger、已固定的旧知识引用、store 的只读精确版本访问、稳定提交回执身份。

输出：明确的 KnowledgeMutation 列表、局部 key→精确新版本映射、每个新条目的原提交位置。**函数不得联网、不得调用模型、不得写库。**

职责：

1. 使用原 `prepare_evidence()` 的共同证据准备逻辑解析被条目引用的 eN；不要自己再实现来源归档。
2. 同时准备来源、证据和显式 claim/inference/question，使新 evidence 不必提前旁路入库。
3. 新对象 ID 由原提交身份和局部 key 稳定生成；更新对象使用 fN 冻结的 ID/revision，不能重新读取今天的 head 来改期望版本。
4. 系统设置 origin、scope、commit 身份；模型提供现有条件结构的简短 state/value，保持 reported/not_reported/not_extracted/not_applicable 的区别。reported 只声明来源报告了该条件，不能把用户目标或模型假设自动转换成 reported；不确定保持 not_extracted，不从常识补齐，也不能将未提取误写成资料没有报告。unreviewed 不能代替条件出处。
5. 模型解释保持 unreviewed，新的语义条目保守为 candidate；结构有效、存在证据引用均不等于内容已经证实。
6. 检查关系方向/类型、悬空引用、依赖循环、事务上限，允许同批引用即将写入的精确版本。
7. 不做语义去重。不同会话提出同义结论时，只有明确选定更新/关联对象才合并，不能因文本相似覆盖旧知识。

### D5. answer_archive.prepare_evidence()

提取共同的“按明确选择准备证据”纯函数，选择集合来自：正文 eN 引用，以及显式知识条目对 eN 的引用。两者取并集，同一来源/窗口只有一个稳定身份。

历史证据复用原版本；新证据和来源准备为同一 publication command 的 mutations。不要保存每个读过的片段作为答案依赖；只有实际引用才建立对应边。

### D6. answer_report.compile_answer_report()/AnswerReport.from_payload()

编译顺序为：

```text
验证原提交 → 准备被选证据 → 编译显式知识条目
→ 解析正文引用的旧/新知识 → 构造冻结 AnswerReport
```

当前 from_payload 的“只允许 source/evidence mutations”不能简单删除。新版本应允许**有原模型提交条目与绑定证明**的 claim/inference/question；无法找到原条目的额外 mutation 一律拒绝。渲染、正文引用位置、关系目标与编译摘要必须一致。

`knowledge_refs` 建立答案实际使用的依赖，不等于本次保存的所有对象。`knowledge_mutations` 可以包含没有被正文采用但用户明确要求保存的候选问题/假设；两种集合不强行相等。

知识条目校验失败时不得静默丢弃，继续发布正文并声称知识积累成功。按原 gate 返回具体错误及原正文，允许明确修订提交；若用户放弃积累，必须是可观察的新提交。

### D7. publication_command()/publish_synthesis()/_commit()/KnowledgeStore.publish_note()

publication_command 将来源、证据、明确知识更新及唯一 answer 作为同一冻结命令。继续通过原 `_publish_research_answer` effect 与 `publish_note()` 的 SQLite 事务执行；不增加第二次后台知识写入。

已提交的事务先查原 commit 身份和内容摘要。冷启动不重新调用编译器、不重新选择知识 head、不补一次“提炼”；取消仍遵循现有“已经提交的事实不能描述成未发生”的规则。

若冻结后其他研究更新了同一知识对象，按原 expected_revision 冲突拒绝，不能悄悄覆盖或在恢复时自动 rebase。需要继续研究时形成明确的新修订任务。

答案正文只有一个权威对象，展示笔记仍从它投影。独立 claim 是显式结构化知识，不再保存另一份可独立改写的报告正文。

### D8. knowledge_background()/ResearchTools.knowledge_read()/references.bind_version()

保留当前历史检索接入、owner 范围及禁止旧答案充当独立证据的原则。搜索结果是 locator；完整读取旧 claim/inference 才给 fN，并带条件、unreviewed 和 dependencies_changed。

fN 采用由正文显式引用决定，读过不自动 uses，保存过不自动 adopted。`reference_evidence_preview` 的预览不应冒充原来源阅读；需要作实证回答时仍从原知识 source/evidence 版本进入 eN。

一次新研究应能形成：旧结论@1→读取原依据→补充新证据→显式更新为结论@2；旧推论仍固定依赖结论@1，显示前提后来变化，不能自动改指向结论@2。

### D9. build_version_graph()/version_detail()/前端

继续从 KnowledgeObject.references 生成语义边。答案 uses、claim supported_by、inference depends_on、evidence from_source 各保留原义，不因处于同一研究就补 supports。

条件可先显示在结论节点详情中，无需为了“条件也有一个圆圈”新增永久实体与关系表。可视化节点、反向箭头、相似关联可以派生，但不能写回为已经验证的关系。

本轮不需要重写 frontend/research_graph.ts 的布局引擎；重点是后端给出的版本、节点内容和边正确。

### D 中的新纯函数接口草案

以下是建议接口，不是仓库已存在的函数或第二套执行入口：

```python
def build_research_request_projection(session, snapshot, ledger, *, failed_response=None):
    # 输出一个请求投影值，原 request artifact 负责冻结；函数不写入。
    ...

def choose_read_window(document, *, offset, requested_limit, budget):
    # 选择并标明真实连续范围；不获取来源、不授权未显示文本。
    ...

def prepare_selected_evidence(selection, ledger, store, *, project, session_id):
    # 从原 prepare_evidence 提取的共同逻辑，仅准备 mutations/bindings。
    ...

def compile_knowledge_updates(submission, scope, evidence_bindings, store, *, submission_ref):
    # 显式条目 -> 原 KnowledgeMutation；不抽取、不写入、不调模型。
    ...
```

优先放入现有领域模块；只有函数体过大时才抽独立纯模块。它们都不持有可变任务游标，不承担调度。

### D10. 合同版本必须一次规划

本批涉及 AnswerScope、submit envelope、AnswerReport、引用类型和 publication command 的新版本。分别给出明确版本与旧读取分支；旧提交不含知识更新就保持原语义，不能在读取时补抽取。

`FindingReport.from_payload()` 已能分发整篇报告；扩展新 AnswerReport 分发即可，不要误判它完全不支持整篇报告。`prepare_research_completion()`/`GateVerdict` 的类型声明应同步，但不需要新增完成 gate。

## 7. 内核究竟改哪些，不改哪些

| 位置 | 必要修改 | 不应扩大到 |
| --- | --- | --- |
| KernelContext.freeze/prepare_turn/deliver_results | 传递并冻结单一请求投影 | 新研究驱动循环 |
| KernelContext.interpret/retry | 原失败响应依据、规范化重复诊断 | 语义评分器、另一套重试账本 |
| prepare_control_delivery | 修复携带原响应与合同 | 根据今日知识重新写旧答案 |
| prepare_provider_request/decode_provider_invocation | 值对象版本化读写与恢复 | 新 provider effect 生命周期 |
| kernel_receipts | 明确旧格式读取 | 回退到附件配额内存放必要回执 |
| kernel_contracts | 已知结果版本解码 | 缺字段就默认成功 |
| completion gate | 接收领域编译结果及具体结构错误 | 新的“语义已证实”完成标记 |
| 原 publication effect/事务 | 增加同一命令中显式知识 mutations 的验证 | 第二份 answer、发布后提炼队列 |

运行迁移图、原子计数存储位置、effect 调度、取消协议和 repository 的版本事务机制均不需要为本方案重写。

## 8. 实施顺序与验证，不再凭样题追加规则

按用户批准，A（确定性修复）→ B（输入投影）→ C（修复/阅读）→ D（知识批次）分别提交和验收，每批一个 commit。

实施硬约束：发送准入前冻结实际编码后的消息、材料窗口、引用许可、工具声明与预算；恢复使用该冻结输入，不重新跑角色编码/裁剪。实现身份限制继续有效。历史已读与当次完整可见、允许引用是两个集合，不能混用。修复验收分别检查原响应完整可恢复、必要正文实际进入修复请求、最终关键内容是否保留；前两项确定性检查不等于第三项通过。

知识存取之外另验收跨问题关联：新问题不用旧关键词时，通过已有实体别名、about/depends_on 等明确路径找到旧知识，提示适用条件并区分待核验类比与直接证据。不能仅以同义词检索成功宣称具备这种能力。

### 第一组：无需模型的行为验证

沿现有测试补真实行为，不写仅检查源码包含某句话的测试：

| 范围 | 必须证明 |
| --- | --- |
| 版本邻居 | source@2 不返回只引用 source@1 的 evidence；权限与预算过滤仍生效 |
| 旧回执/结果 | 有效旧格式可读；字节篡改、范围不符、截断或未知版本明确失败 |
| 请求布局 | 原任务恰好一份；完整 tool 对；材料不进 system；实际展示与 scope 相等 |
| 协议修复 | 超过 600 字符的关键尾句仍在修复输入；重启后相同；随机 call ID 不制造新诊断 |
| 材料预算 | 中间缺口、尾章节、中文/英文 token 差异、元数据很长、上游截断和历史版本续读 |
| 新材料积累 | 首次读新来源，提交两个关联条目和答案，一次事务写入，引用无悬空 |
| 历史依赖 | 读 f1 不自动采用；正文明确引用才有 answer→旧结论@固定版本 |
| 显式条目 | 不可从正文偷加 claim；条目无效时不能假装积累成功；循环/越权/版本冲突拒绝 |
| 冷启动/取消 | 发生在编译、gate 接受、发布准入、事务提交及回执前后，恢复不重编译或重复写入 |
| 单一事实来源 | 删掉可重建图/索引后结果可再生；答案编辑不能与展示笔记形成两份可写正文 |

现有测试位置包括 test_knowledge_version_graph.py、test_kernel_receipt_integrity.py、test_research_scoped_request_projection.py、test_research_whole_answer.py、test_research_whole_kernel.py、test_research_long_term_loop.py、test_research_repair_and_navigation.py。复用原冷启动夹具和真实 provider codec，不靠手写另一套研究模拟器证明新架构。

### 第二组：固定本地模型的有限对照

复用 tests/manual/research_framework_comparison.py 和已有透明 wire 记录代理，冻结模型、服务版本、采样、工具输出上限与材料，不另建 runner。

1. B 先保持原 submit 形状，检验角色分层及原任务保留；记录首次正文和最终交付，不只看最后报告。
2. C 检验协议失败、全文可达性和重复读取是否减少，不把这些指标混成内容正确率。
3. D 比较无知识要求的普通回答与明确要求积累的任务；分别评内容、有效知识更新、跨会话复用及错误关系。
4. 开发旧题只验证已知机制；新材料、新对象名和事先冻结评分用于看泛化。不能按本轮结果事后修改必要项。
5. 12B 是工程压力目标；27B 最终也要独立验收，不从参数量推出必然通过。

每一批先设请求/时间预算、回退单位和停止条件。修改只稳定改善传输却没有内容收益，就如实记录；知识批次若拖累普通回答，不强行宣布成功，应停止该合同改动并重新评估。不同阶段不能互借成绩。

### 当前不实施的方案

自然正文直接完成、额外审稿模型、固定逐题拆解、后台答案二次提炼、自动把相似关系写成事实边、为了恢复而放宽摘要/权限验证，都不属于本次推荐主线。

只有上述输入与知识合同验证后仍有明确的工具终结瓶颈，才单独评估自然正文作为原 kernel 的另一种完成输入；缺少完整性声明不能默认 answered。

## 9. 完成标准

完成标准不是“代码更少”“图上点更多”或“发布成功”。应同时看到：

- 模型读得到必要原文，原问题不随工作视图变化而移位或消失；
- 格式修复不再无信息重写，必要失败正文可恢复；
- 正文内容在预先冻结的新题上有可重复收益；
- 本次明确积累的结论带条件和证据，后续会话能找到并正确引用其版本；
- 反例或修订进入原知识版本链，旧依赖不会被悄悄改写；
- 同一套内核、回执与知识事务完成全部工作，没有第二运行时和第二份可独立修改的答案。

## 附录：现有函数定位

以下行号对应采样版本；审阅期间工作区继续变化，七个文件的后续差异也已记录，主要为类型与回执校验调整，未改变上述关键缺口。实现时以所选固定版本为准。新建议函数不在此表冒充已有实现。

| 批次 | 现有函数 | 源码 |
| --- | --- | --- |
| A | `KnowledgeRecall.neighbors` | [codey/knowledge/recall.py](../codey/knowledge/recall.py:119) |
| A | `KnowledgeRecall.search` | [codey/knowledge/recall.py](../codey/knowledge/recall.py:47) |
| A | `build_version_graph` | [codey/knowledge/version_graph.py](../codey/knowledge/version_graph.py:57) |
| A | `restore_result_receipt` | [codey/operations/kernel_receipts.py](../codey/operations/kernel_receipts.py:79) |
| A | `result_from_payload` | [codey/operations/kernel_contracts.py](../codey/operations/kernel_contracts.py:31) |
| A/D | `archive_scope` | [codey/research/answer_archive.py](../codey/research/answer_archive.py:14) |
| B | `prepare_kernel_turn` | [codey/operations/kernel_preparation.py](../codey/operations/kernel_preparation.py:18) |
| B | `kernel_prompt_for_session` | [codey/operations/kernel_prompt.py](../codey/operations/kernel_prompt.py:76) |
| B | `task_result_context` | [codey/operations/task_guidance.py](../codey/operations/task_guidance.py:77) |
| B | `KernelContext.freeze` | [codey/operations/kernel_context.py](../codey/operations/kernel_context.py:307) |
| B | `KernelContext.prepare_turn` | [codey/operations/kernel_context.py](../codey/operations/kernel_context.py:368) |
| B | `KernelContext.deliver_results` | [codey/operations/kernel_context.py](../codey/operations/kernel_context.py:530) |
| B | `bounded_history` | [codey/providers/api_request_view.py](../codey/providers/api_request_view.py:9) |
| B | `ApiProvider._exchange` | [codey/providers/api_provider.py](../codey/providers/api_provider.py:478) |
| B | `ApiProvider._payload` | [codey/providers/api_provider.py](../codey/providers/api_provider.py:740) |
| B | `ApiProvider._admit_request` | [codey/providers/api_provider.py](../codey/providers/api_provider.py:784) |
| B | `prepare_provider_request` | [codey/operations/kernel_driver.py](../codey/operations/kernel_driver.py:50) |
| B | `decode_provider_invocation` | [codey/operations/kernel_driver.py](../codey/operations/kernel_driver.py:112) |
| B | `answer_context` | [codey/research/answer_context.py](../codey/research/answer_context.py:26) |
| C | `_repair_prompt` | [codey/operations/kernel_prompt.py](../codey/operations/kernel_prompt.py:186) |
| C | `KernelContext.interpret` | [codey/operations/kernel_context.py](../codey/operations/kernel_context.py:399) |
| C | `prepare_control_delivery` | [codey/operations/kernel_control.py](../codey/operations/kernel_control.py:159) |
| C | `ResearchTools.open_url` | [codey/research/tools.py](../codey/research/tools.py:150) |
| C | `ResearchTools._open_document` | [codey/research/tools.py](../codey/research/tools.py:154) |
| C | `ExecutionDelegate._execute_research` | [codey/operations/task_execution.py](../codey/operations/task_execution.py:501) |
| C | `read_material` | [codey/research/knowledge_materials.py](../codey/research/knowledge_materials.py:17) |
| C | `read_intervals` | [codey/research/context_view.py](../codey/research/context_view.py:34) |
| C | `read_evidence_projection` | [codey/research/context_view.py](../codey/research/context_view.py:55) |
| C | `section_locators` | [codey/research/source_rendering.py](../codey/research/source_rendering.py:61) |
| C/D | `answer_scope` | [codey/research/answer_contract.py](../codey/research/answer_contract.py:104) |
| D | `answer_spec` | [codey/research/answer_contract.py](../codey/research/answer_contract.py:192) |
| D | `citation_occurrences` | [codey/research/answer_contract.py](../codey/research/answer_contract.py:138) |
| D | `submit_answer` | [codey/research/answer_contract.py](../codey/research/answer_contract.py:152) |
| D | `validate_submission` | [codey/research/answer_contract.py](../codey/research/answer_contract.py:174) |
| D | `prepare_evidence` | [codey/research/answer_archive.py](../codey/research/answer_archive.py:35) |
| D | `publication_command` | [codey/research/answer_archive.py](../codey/research/answer_archive.py:115) |
| D | `compile_answer_report` | [codey/research/answer_report.py](../codey/research/answer_report.py:94) |
| D | `AnswerReport.from_payload` | [codey/research/answer_report.py](../codey/research/answer_report.py:50) |
| D | `ResearchTools.knowledge_read` | [codey/research/tools.py](../codey/research/tools.py:236) |
| D | `bind_version` | [codey/research/references.py](../codey/research/references.py:35) |
| D | `knowledge_background` | [codey/operations/knowledge_context.py](../codey/operations/knowledge_context.py:10) |
| D | `publish_synthesis` | [codey/operations/research_publication.py](../codey/operations/research_publication.py:23) |
| D | `_commit` | [codey/operations/research_publication.py](../codey/operations/research_publication.py:91) |
