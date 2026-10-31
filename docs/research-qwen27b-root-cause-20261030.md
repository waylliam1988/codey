# Qwen 27B Research 根因分析，2026-10-30

本次核对基于 HEAD `eb930b6e`、原始请求/响应、冻结证据包及生产代码。
没有新跑模型生成、修改生产代码、改写原始结果或运行全量 pytest。
新增验证包括生产解码器离线回放、评审解析器反例，以及当前本地服务的纯分词/模板渲染。

结论：这是三个问题叠加，不能归结为“Codey 没 bug”或“27B 参数不够”。

1. 当前具体 Qwen 部署在短材料上也会扩大主张、混淆比较对象，证据约束能力尚未达标。
2. Codey 的最终内容验收依赖同一模型的语义判断；结构、引用和回执校验不能兜住这种错误。
   原任务要求的完整性校验还存在可离线复现的缺口。
3. 最后一次网页运行直接失败于提供方返回的 `done` 缺字段。Codey 的通用原生协议提示
   同时描述两种完成形式，增加了歧义；但尚未通过单因素实验证明它是重复输出的唯一原因。

执行层此前确有多项代码 bug，已经分别修复。它们能解释特定运行的中断、参数丢失和取消缺陷，
不能解释绕开浏览器和 Research 调度后仍出现的短材料语义错误。

## 证据范围

`research-layered-review-20261030.md` 开头的 18 次固定材料诊断是 **Gemma 12B**，
不是 Qwen。其后半段记录了 Qwen、运行时和取消交接。Qwen 的对应原始数据如下。

| 材料 | 可以回答的问题 | 不能据此声称的结果 |
| --- | --- | --- |
| `artifacts/research-layered-qwen27b-20261030`，18 次 | 短材料理解、抽取、综合各自表现 | 网页端到端成功率或所有 Qwen 的能力 |
| `artifacts/research-live-wal-qwen27b-native-off-20261030`，24 次 | 一次机械完成的真实网页链路及其内容缺陷 | 严格语义验收通过 |
| `artifacts/research-convergence-quotes-20261030-b`，5 个探针 | 缩短为逐句判断后能否识别无支持子句 | 逐句引文足以保证正确 |
| `artifacts/research-convergence-review-final-20261030`，2 次真实评审 | 当前评审链路是否会错误放行归档报告 | 新网页研究成功；其归档导入是 synthetic receipt |
| `artifacts/research-convergence-final-web-20261030`，23 次 | thinking 开启时最后一次完整尝试的实际失败位置 | 成功、正常取消或评审已执行 |
| `artifacts/research-payload-clock-{original,merged}-20261030` | 两份冻结失败请求的单次输出 | 完整 Research 或 thinking/clock 的普遍因果结论 |
| `artifacts/research-convergence-web-final-off-20261030` | 取消前确实发生的搜索和分页读取 | 最终综合成功或失败；该轮没有 findings |

这里的“当前部署”是 `Qwen3.8-27B-TurboFCFusion-735-882-Here-Uncen-NEO-CODER-MAX-Q4_K_M`
配合 KoboldCpp 1.117.1、Jinja 原生工具。不是标准未修改 Qwen、未量化模型或其他推理后端的对照。

## 1. 语义问题在短输入上已存在

固定材料 runner 直接请求 `/chat/completions`，不经过 Research 的网页、知识保存、
多阶段续跑或最终报告编译。每条输入已要求只依据给定材料。

| 原始文件 | 提供方报告的输入 token | 原文支持 | 模型增加的判断 |
| --- | ---: | --- | --- |
| `research-layered-qwen27b-20261030/sqlite-concurrency-l3-r1.json` | 254 | 读写可并发，写者串行 | “能显著提升并发读性能”“整体吞吐优于默认回滚日志模式” |
| `research-layered-qwen27b-20261030/capecitabine-preclinical-l3-r1.json` | 374 | 交替方案相对于高剂量联合治疗的比较，单药相对 MTD 不显著 | 扩为可能优于单药，另加入“小样本量”“研究未探讨长期疗效” |
| `research-layered-qwen27b-20261030/retail-causal-pilot-l3-r2.json` | 291 | 店铺非随机、自愿参与且地区分布不同 | “自愿店本身销售更强” |

这些例子分别把机制变成工作负载性能保证、把一个比较对象换成另一个、把可能的选择机制写成事实。
标注“推论”并不能让缺少必要前提的判断成立；材料没有说明某项评估，也不等于研究没有做过。

更直接的证据是逐句审查的 `research-convergence-quotes-20261030-b/comparator.json`：
输入仅 785 token，提示明确要求核对比较对象，模型仍将错误的“交替用药比单药显著延长生存期”判为 supported。
它引用了真实的连续原文，但理由是：

> high-dose combination therapy, which is a form of single-drug treatment.

这是明确的概念错误。正确比较对象的阳性对照也被判 supported，说明该探针没有成功区分这对正反例。
原文定位正确、输出 schema 正确，均不能证明引用蕴含主张。

同时应保留正面证据：Qwen 固定抽取的 6/6 原始输出符合裸 JSON/字段契约及引文定位检查；
医学抽取保留了小鼠与正确对照。这说明能力随任务形态变化，不能说它完全读不懂或完全不会 JSON。
但抽取表现不能外推为自由综合、证据审查可靠。

## 2. 缺字段循环的直接原因与 Codey 的促成因素

失败运行 `run_bf2fcea3a381457fb818d3fcb3a5afd8` 的原始 wire 可分为三段：

| 物理请求 | 实际行为 |
| --- | --- |
| 1..15 | 登记问题、读页、保存四条候选；第 12 次只交 summary，第 13..15 次已经交出 report_sections，并逐步修正标题和引用 |
| 16..18 | 补查阶段三次调用同一 web_search，没有新增 finding |
| 19..23 | 新窗口最终综合，连续五次返回相同的 summary-only done，最终 protocol |

这比“模型一直不会结构化输出”更准确：同一轮早期已经生成过完整字段，后来进入最终综合才卡住。
补查阶段的 `done` 合法地只要求 summary，最终报告阶段则要求 summary 和 report_sections。
两个阶段的实际 schema 正确，且最终综合使用了新 provider 窗口，不能直接断言旧 schema 泄漏。

本轮进一步验证：

- 原始上游 `generation_response` 的参数确实缺字段；用生产 `api_chat.decode_turn` 回放第
  12..15、19..23 次响应，参数逐项一致，Codey 解码器没有删掉 report_sections。
- 最后五次调用 id 不同，但 arguments 和 reasoning 分别逐字相同。五次生成时间合计约 562.5 秒。
  arguments SHA-256 为 `1dd7b3fd101494e6140949366dcf7fb27645c8cddebba0da9c17669fd036bae6`。
  不能把固定 seed 下的相同输出直接证明为服务缓存 bug。
- 实际 schema 的 required 始终包含 `summary, report_sections`，反馈明确写出缺少 report_sections。
  最后几次均以 tool_calls 结束，生产输出预算 32768，并非 finish_reason=length。
- 最终综合从只有三个消息的新窗口开始；不是必须先处理整轮几十次工具历史才能答题。
- 当前相同模型名/服务版本的 `/api/extra/tokencount` 重渲染，第 19/23 次请求分别为
  5248/10164 token；后者的实际 prompt 包含四次明确修复指令和 required schema。
  角色分隔符为真实换行。未观察到修复消息被当前 Jinja 模板丢弃。
  这是本轮当前服务的重渲染，不是补录历史运行的 usage 或模板快照。

这里定位到的是**提供方 HTTP 返回层**。wire 没有保存 KoboldCpp 解析原生工具调用前的原始生成文本，
因此还不能完全区分“模型没有生成字段”与“服务端原生工具解析遗漏字段”。
直接冻结请求探针绕过了 Codey 调度/解码，但没有绕过 KoboldCpp 的生成与解析；
要继续区分两者，需要服务端解析前输出或受控的另一传输/推理后端对照。

Codey 确实还有一个值得修正的提示问题：
`codey/operations/kernel_prompt.py:37` 的 `_NATIVE_FINAL_RULE` 同时描述
“Without report_sections, summary contains the complete response”和结构化报告形式。
`_repair_prompt` 在原生模式下列出必填字段后，又附上这个通用规则；具体完整报告样例只在文本协议分支补充。
最终综合的推理文本恰好仍决定把完整结论放入 summary。

这构成**可见的协议提示歧义和未经验证的促成因素**。不能因为相邻就断言唯一因果；
早期修复曾成功、关闭 thinking 的冻结探针仍失败，都要求保留其他解释。
适当改法是根据当前冻结 schema 只描述本阶段有效的完成形式，并给本阶段样例；仍应拒绝缺字段的输出。
不能把任意 summary 自动填成一份通过验收的研究报告。

## 3. 保存的“证据链”不等于主张被证实

`codey/research/findings.py:15` 检查来源、已读 passage、版本、条件形状等，
并把所选摘录和模型主张作为支持关系保存；它不判断每个子句的蕴含关系。
返回明确标为 `interpretation_review=unreviewed`，这层设计本身是诚实的候选保存契约。

问题发生在后续把整条候选当成已获支持的前提时：

- **最后失败网页运行的 f4**：选择的摘录只包含独占锁场景，却一次主张独占锁、关闭清理、崩溃恢复三种场景。
  后两项可能在该网页其他段落成立，但没有被这条 finding 所选证据覆盖。
- **归档报告的 f4**：所选摘录只支持默认 1000 页 checkpoint 阈值，主张又增加 PASSIVE/FULL/RESTART 及其行为。
  最终 evidence 段缩短了，但 coverage.answer 和被采用 finding 仍含扩展。
- **归档 f2**：摘录支持长读者阻碍 checkpoint 进度，没有包含主张里的完整 end-mark 因果说明。

代码已经支持 `additional_passage_refs`，不必再发明另一种多片段保存接口。
缺口在于模型没有正确选择全部支持材料，审查也没有挡住；另一种合规处理是缩小主张。
`evidence_complete` 只表示证据预览没有截掉已选内容，不能解释成“该主张的所有子句均获支持”。

## 4. 评审没有提供独立纠错能力

冻结 basis 和 assessment 展示了具体错误，而不只是一个可疑的 passed 标签：

- 对归档 f4，评审只解释“1000 pages”这半句，就把含其他模式的整条主张判为 supported。
- summary 把网络文件系统限制归为旧版本限制，评审因为引用里确实出现 network filesystem 就接受了错误分类。
- source_quality 声称全部证据只来自 wal.html，basis 实际列了 wal.html 和 BEGIN CONCURRENT 两个来源，仍被通过。
- source_quality 的“当前稳定版”被解释成“本轮刚获取，所以合理”。检索时间不是版本身份依据。
- “区分当前保证、旧版本限制和还需要测量的工作负载因素”被评为 delivery，question_refs=[]，因此漏掉的版本研究问题未被识别。

评审提示其实已明确要求检查每个子句、当前/旧版区别和虚构 stable/latest 标签
（`codey/research/semantic_review.py:132`）。因此不能把失败解释成代码从未要求核查这些内容。
同一模型即使在新窗口中评审，也可能保留相同的概念和推理错误；换一个角色名不构成独立证据。

逐句探针有局部收益：f2 的无支持因果子句被识别出来；f4 的内容也识别了缺少模式，
但把对象数组编码成字符串，违反协议。医学错误比较仍获通过。不能把这种探针宣称为已解决语义闸门。

## 5. 可离线复现的 Codey 验收缺口

最实质的代码问题位于 `codey/research/semantic_review.py:48`：

- claims 和 paragraphs 必须恰好覆盖冻结集合，这是有用的确定性校验。
- requirements 只要求 1..24 条，每条 task_excerpt 是原任务的不同原文片段。
- 没有被冻结的原任务要求集合可供检查“是否全部出现”；kind 又由评审模型自行选择。
- `review_issues` 只收集模型明确列出的 missing 或非 supported 项。

本轮把原始 assessment 复制到内存后，调用生产 `parse_review` 和 `review_issues`，结果如下：

| 输入变体 | requirements 数量 | 解析结果 | issues |
| --- | ---: | --- | --- |
| 原始错误通过的评审 | 5 | 接受 | [] |
| 删除包含版本区别的整条要求 | 4 | 接受 | [] |
| 仅保留第一条写者并发要求 | 1 | 接受 | [] |

这是**评审输入校验与问题投影层的实测反例**，不是本轮新执行了一次发布。
但生产提交路径 `codey/operations/research_review.py:93` 确实使用这两个函数，
在没有 issues 且其他完成条件满足时发布。因此它不能为“原任务已完整覆盖”提供独立保证。

同样，`codey/research/questions.py:24` 只保证模型登记的 qN 全部有记录，
不能证明这些 qN 已经覆盖原任务所有要求。作者漏版本问题，评审又把版本判断当格式约束，
两层就共同留下盲区。解决方向是形成显式冻结的任务要求契约及稳定 ID，
要求覆盖恰好该集合，并审计契约本身与原任务的对应；不是简单把最少行数从 1 改大。

其他内容验收边界也应明确：

- 引用绑定按段落建立，不能机械证明段落所有子句都被引用支持。
- 评审 reason 是自由文本，不要求可定位的支持片段，也不验证推理是否正确。
- `review_basis` 包含原任务、主张、完整所选摘录、段落、coverage 和来源，
  但不包含 investigations 的实际回执内容。它无法独立核验实际检索动作及其相关性。
- 发布保存了 `same_provider_model_assessment` 身份，候选主张也仍为 unreviewed，这些是正确的限制标记。
  然而目标若是按证据验收的可靠报告，同模型的正面自评仍然不足以作为唯一的内容通过条件。

## 6. 已修 bug 与剩余语义问题的关系

| 已观察到的实现缺陷 | 直接影响 | 能否解释短材料语义错误 |
| --- | --- | --- |
| 网页 EOF/越界读仍成功，`ed910cbc` 修复 | 误导连续翻页 | 不能 |
| 默认窗口未跟随已确认服务容量，`974eb3b7` 修复 | 任务预算与配置不一致 | 不能 |
| 必需工具回执受 32 附件配额影响，`71624a43` 修复 | 工具结果持久化失败 | 不能 |
| thinking 设置在默认选择时丢失，`bd8092f8` 修复 | 实际请求与实验声明不一致 | 不能；固定材料直接发送 false |
| browser context 关闭后的准备路径，`dcb9d05a` 修复 | 部分页面读取失败 | 不能 |
| 非法 Unicode 结果、取消后的 pending Effect，`aec467ee`/`eb930b6e` 修复 | 执行错误与生命周期不一致 | 不能 |

`TargetClosedError` 关闭警告是尚存资源管理问题。在一轮已完成任务后仍出现，
短材料又不使用 Playwright；没有证据把它当作错误版本判断或错误比较对象的原因。
既有 13 个失败测试需要单独处理，但目前没有把它们与这些语义失败连接起来的证据。
150 项通过和静态检查只验证其覆盖的程序行为，未测量模型在真实证据上的判断准确率。

## 7. 应如何继续定位与修复

优先顺序按对研究内容可靠性的影响排列，以下尚未实施。

1. **先收紧内容通过的含义。** 保留同模型评审身份；已有错误对照必须能拦下，
   才能声称自动语义验收可用。需要独立人工或经相同冻结材料校准的其他评审能力。
   换模型本身也不能免除正反例验收。
2. **固定任务要求和证据支持单位。** 在合成前冻结任务要求 ID，审查覆盖完整性；
   将可检验主张拆到足够细，每个判断携带确切引文/范围及条件。
   检查引文存在、比较对象、模态、版本边界；不能只比文字是否在页面里。
   当前逐句探针已证明精确引文本身仍不足以解决语义错误。
3. **优先消除可确定计算的自由文本错误。** 来源清单、抓取时间、版本依据来自结构化事实；
   没有版本证据就保留 unknown，不从 fresh 推出 stable，不让自由 prose 覆盖实际两来源清单。
4. **单独做协议最小复现。** 使用同一个冻结合成包和参数，只改变一项：
   通用完成规则改为本阶段规则、补本阶段原生样例、原生/文本形式、或模型部署。
   同时保留正例和错误例。通过标准仍包括结构和内容，不扩大为一次直接重跑完整网页任务。
5. **再做新鲜网页端到端验收。** 原题、全新知识库、冻结源码，逐项审计最终报告、
   selected excerpts、版本差异与知识版本。取消的 off 运行没有给出这一步的结果。

当前证据足以排除“纯粹只差更多上下文/更多轮数/关闭 thinking”作为完整解释。
但没有标准基模、其他量化或另一服务端的受控比较，不能进一步把部署的语义问题
单独归因于参数量、Q4、融合微调、底层模型家族或 KoboldCpp。

## 修复前验证复现

以下是 `eb930b6e` 上的历史反例；后续修复已替换评审契约，不能用当前解析器直接读取旧评审。
在该历史提交的仓库环境运行下面的只读代码，可以重现 requirements 完整性缺口；不调用模型、不写知识库。

```python
import copy
import json
from pathlib import Path

from codey.research.semantic_review import parse_review, review_issues

root = Path("artifacts/research-convergence-review-final-20261030")
basis = json.loads((root / "basis.json").read_text(encoding="utf-8"))
original = json.loads((root / "assessment.json").read_text(encoding="utf-8"))["assessment"]
omitted = copy.deepcopy(original)
omitted["requirements"] = [
    row for row in omitted["requirements"] if "区分当前保证" not in row["task_excerpt"]
]
single = copy.deepcopy(original)
single["requirements"] = single["requirements"][:1]
for name, value in (("original", original), ("omitted", omitted), ("single", single)):
    parsed = parse_review(json.dumps(value), basis)
    print(name, len(parsed["requirements"]), review_issues(parsed))
```

实际输出：`original 5 []`、`omitted 4 []`、`single 1 []`。
本轮未运行 pytest；生产文件未修改，旧测试结果未重新宣称通过。

## 后续实现

上文“未修改生产代码、未运行 pytest”只描述最初根因分析，不描述随后的修复。
后续实现、定向测试和实机结果见 `docs/research-repairs-20261030.md`。
当前代码冻结原任务 rN 文本范围，要求逐项覆盖，并逐单位评审 finding 和报告段落；
原有只保留一条 requirements 仍被接受的契约已移除。
