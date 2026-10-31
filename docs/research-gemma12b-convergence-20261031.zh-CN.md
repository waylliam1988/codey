# Gemma 4 12B Research 收敛实施记录

2026-10-31，基于 `060656db` 和 [诊断方案](research-convergence-diagnosis-20261031.zh-CN.md) 逐项修改、验证、提交。
用户已授权使用当前本地 Gemma 4 12B 做真实验证；每个问题单独提交，保留原始输出。

本地 `/v1/models` 确认部署为 `koboldcpp/Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M`。
小模型成功是有价值的工程证据，不自动证明另一模型一定成功。

最新进展见 [取消强制自审与本轮验证报告](research-self-review-convergence-20261031.zh-CN.md)。
原五项失败已修复，原选集本次 228 项全部通过；强制自审已移除，最小真实研究 11 请求、84.9 秒完成。
最后一轮完整 WAL 为 22 请求、317.1 秒、no_progress，未发布；仍漏任务覆盖登记并存在解释错误。
按用户要求，本轮结束后提交并停止，不能宣称完整 Research 已解决。

第 34 项结束时的历史验收状态：最小单写者真实研究有一轮 done，最终文本经人工对照原文；
完整 WAL 最新一轮 `wal-full-json-03` 50 请求、约 662 秒，以 no_progress 失败，未发布。
该轮后的检索覆盖、阅读边界和修复引用投影已经分别提交并通过回归，尚未重新跑完整任务。
新四项实机校准中三项符合预期，条件转移负例仍被误放行。不能宣称复杂 Research 已解决，
也不能据这些不同任务/版本的样本计算相对 10 月 30 日的成功率增幅。
10 月 30 日已有网页读取、保存及固定材料的跨进程知识生命周期；本次新增证据是小任务完整验收、
完整任务分批评审可完成并进入修复，以及上述已定位工程缺陷的修复。

## 1. 评审输入去重与完成动作收敛

`task_working_context` 不再为评审重复整个 task/basis。评审提示沿用冻结工具契约，只要求提交 assessment，
移除该阶段不适用的 workspace 验证和普通 done 提示。没有改变执行循环、回执或发布条件。

验证：评审提示、评审范围及原生 codec 共 48 项测试通过，修改文件 Ruff 通过。
提示测试覆盖原生/文本与请求级契约两种组合，检查唯一 basis 和实际提交动作；尚未据此宣称模型语义通过。

## 2. 取消强制生成反证段

报告允许 `counter=[]`。编译器生成明确的本轮记录状态，且不把这句话当作来源事实送给语义评审。
有实际限制或反证时仍须正常提供支持；问题缺口继续保留在 coverage。

验证：空 counter、真实 finding 编译、引用覆盖与持久化语义评审共 37 项通过；修改文件 Ruff 通过。

## 3. 已读 passage 跨问题采用与来源解析

`save_finding` 可直接选择本轮已读的精确 passage，由该快照确定来源；不再要求模型重复填写 source_ref。
显式传入的 source_ref 仍作为断言校验，旧冻结调用使用同一写入实现，不产生第二套 writer。
同一 passage 可为另一问题保存/采用；相同 claim 的知识身份和修订复用，新的 fN 仅表示问题采用关系。
原读取回执的 question_ref 不变，不伪造第二次调查。coverage 继续拒绝把其他问题的 aN 当作本题实际调查。

验证：相关选集 48 项通过，Ruff 通过。另有 evidence_split 的两项旧夹具失败，已在独立
`e4334c24` 基线 checkout 复现相同失败（原文不含所写引文、未提供显式 evidence）；没有放宽来源校验。
跨问题测试实际执行一次读取、两次采用、回执恢复，确认没有新增知识修订或伪造读取。

## 4. 单份问题答案编译报告

新的 strict Research 冻结工具只接受 `done.report_sections.coverage`，不再要求 summary/evidence/counter 各写一遍。
结论、证据段和问题覆盖从相同 answer 与已选 finding 编译；语义评审只检查模型实际写的答案一次。
已有 FindingReport 的保存/恢复格式继续使用同一编译器与 digest。旧冻结的 expanded proposal 仍可被同一
纯编译器读取，但新 strict 工具契约拒绝提交该旧形状；没有新增运行模式或第二个写入入口。

验证：答案编译、原生/文本调用、历史精确版本、报告归档、语义评审和协议 profile 共 84 项通过；
额外 native/prompt/protocol 选集 31 项通过、9 项为已记载的旧 protocol fixture 失败；Ruff 通过。
测试已将当前 shared-driver 提交夹具迁移到当前答案契约，旧报告的只读编译覆盖仍保留。

## 5. 从原工具回执推导的小批审查

根 basis 冻结一次，每批至多 8 个判断，依次检查 finding、答案、任务要求和调查相关性。
模型只提交当前批；原 basis 身份由 driver 绑定，长 hash 不再必填。后续答案判断携带必要的原 finding
及已经落盘的 finding 判断；不能用被拒绝的 finding 批准答案。

这里对诊断方案作了更小的架构收敛：批次使用现有单个 review invocation 内的普通工具轮次，
不新增 invocation 编号、持久化 cursor 或调度器。下一批从原 submit 工具回执确定性重建。
每批成功只记录判断；最后一批必须聚合并通过根 basis 的完整集合校验，才进入原有唯一发布事务。
未齐批次、重复单位、错 basis、改写原调用、改写先前依赖都被拒绝。最终证明引用所有批次效果。

验证包括原生/文本 shared driver、真实子进程在第一批提交后退出再恢复、最终知识事务前后恢复、
缺批与篡改回执。批次恢复不会重跑已 settled 判断、重复抓取或二次发布。
扩大选集首次得到 83 passed / 5 failed，其中 1 项为新增测试的错误消息断言，已修正；
另 4 项因为测试运行中修改生产类型标注，父子进程实现指纹不同而被恢复保护拒绝。
此轮失败不计为通过；冻结生产源码后对应复验选集 6 passed（覆盖全部 5 项原失败及另一恢复边界）。
后续运行期间保持生产源码冻结。

全生产树 Mypy（575 文件）、Pyrefly、ty 和 Ruff 均通过。

## 6. 评审请求的有界历史投影

在既有冻结 provider request 中记录 `tool_call_history_groups=2`。API provider 从完整原始历史选择
最近的完整协议组，再加入唯一当前批次观察；不写第二份历史，不删除原回执，不改变调用/返回 ID。
Responses 的 reasoning 与关联调用一起保留。下一普通请求仍可使用完整历史，这不是新的 provider 模式。
实际发送的投影视图仍通过原分词与准入路径检查；重启按冻结请求恢复相同选项。

验证：API 协议/上下文/JSON/native 选集 42 passed，另新增两项完整 Research driver + ApiProvider
分批、修复、发布测试通过（历史投影文件共 10 passed）。全树 Ruff、Mypy（576 文件）、Pyrefly、ty 通过。
同时将一项旧观察测试的夹具补齐为真实父问题 q1 及其 question_ref，不放宽执行校验。

## 实机 1：Gemma 原生工具协议阻塞，未完成

`artifacts/research-gemma12b-20261031/single-01`，生产版本 `61f2f6ac`，固定 seed=73、temperature=0.3、
thinking=false，实际模型是当前 Gemma 4 12B。共 8 个物理请求，搜索 2 次、打开 1 次、source_search 1 次、
save_finding 2 次；同一原文被两个问题采用，没有强制重读。第 8 次 done 未收到有效 API 返回，整轮失败。

原始生成已可从 KoboldCpp 诊断接口读取；它漏写 coverage.state、混用其他问题的 aN/fN，
且将单写者/共享内存解释绑到不直接支持该解释的 passage，不能判为研究通过。
py-spy 实际栈确认 KoboldCpp 卡在 `read_object → fix_unquoted_keys → parse_gemma4 → repack_toolcall_tags`，
与旧报告的服务端解析死循环相同。请求最后在约 526.7 秒以连接重置结束，Codey 保留结果未知且没有重发。
原始文本、栈、wire 和知识快照均保留；用户随后重启了服务，确认相同模型、idle=1、queue=0。

后续将使用现有生产 JSON constrained 协议进行独立新状态验证。它沿用同一冻结工具 schema、kernel、
effect 和知识事务，不新增解析执行循环，也不自动补 state 或替模型选择证据。
服务端原生解析器本身尚未修复；JSON 协议是否足够可靠仍须看实际结果。

## 实机 2：JSON 已返回 done，评审越界后生成失控，未完成

`artifacts/research-gemma12b-20261031/single-json-01`，生产版本 `404dc0d5`。
JSON 请求沿用当前单一 kernel；done 的 state 字段齐全，未再进入原生工具解析器。
模型把单一问题拆成三个重叠问题，修复跨问题 finding 采用后，旧规划器仍启动无目标补查和再次合成。
合成还因只检查本 invocation 的 required_tools 而重复搜索。第 18 个请求进入评审，实际输入 3859 tokens，
但模型越过当前批、把自己的中文 claim 当作原文引文，提交被拒绝。
第 19 个请求在协议修复中持续虚构 fN；180 秒客户端超时后服务仍在生成，未发布研究结果。
确认队列为零、仅本次遗留请求运行后，通过 KoboldCpp abort 终止，服务恢复 idle=1。
请求、响应、原始失控输出均保留。结构上收到 done 不代表评审或研究通过。

## 7. 严格 Research 补查收敛到语义评审

严格 Research 不再运行旧 source-count/反证启发式通用补查；由现有冻结语义评审提出具体缺口，
通过原 repair invocation 补证或收窄答案。非严格流程保留原行为，没有新增调度器或运行状态。
新增完整 driver 回归开启旧 planner 配置，验证仅出现初始研究、评审、修复、复评，且只搜索/打开/发布一次。

验证：语义评审及真实子进程续跑选集 44 passed，Ruff 通过；生产源码在测试期间冻结。

## 8. 评审批次去除无关答案和伪装成原文的重复 claim

证据对象只保留完整原始 excerpt 及其来源、定位和关系；模型 claim/conditions 仅在待审 finding 中出现。
纯 claim 批不再携带完整 coverage/报告路径；其他批 coverage 只保留问题状态与当前选中调查，
原数组位置保持不变，使缺口路径仍对应原报告。任务要求批仍能看到完整答案以检查漏答。
原根 basis、原报告、回执、最终聚合校验和唯一发布事务不变，未截断选中原文。

验证：批次与语义评审选集 23 passed，含批间真实进程退出恢复及发布事务恢复；Ruff 通过。

## 9. 评审 JSON 参数使用本批冻结工具契约

单工具 JSON 请求现在约束完整 args；评审工具从原批次推导数组数量、fN/path/rN/aN 及单位编号范围。
该 schema 同时用于提示、native/JSON 声明和 kernel 参数校验，不建立第二套协议规则。
未选类别必须为空，越界 finding 不能靠持续生成成为有效提交；语义、精确引文、重复判断和完整集合
仍由原校验器检查。多工具请求保留现有自由参数策略，避免未经实机验证地扩大 grammar 变更。

校准脚本同步到 coverage-only 报告，增加生产 JSON 协议、固定 seed、实际 payload 和延迟记录。
新增六个显式标注的合成正反例，覆盖单写者、工作负载过度推论、比较对象偷换、片段缺失推断全文缺失、
版本条件保留与遗漏。标签不进入模型提示，合成结果不冒充真实搜索验收。

验证：JSON/批次原生与文本/真实进程恢复选集 15 passed；修改文件 Ruff、生产改动 Mypy 通过。
开发中发现并修正 schema 共用 text 字典被 enum 修改及 mappingproxy 不可 deepcopy 两个问题，
最终冻结源码重跑通过，未把失败测试计为通过。实机 grammar 兼容性仍待下一轮验证。

## 10. 请求级生成长度上限

冻结 provider request 增加 `output_token_limit`：评审 2048，严格研究工具轮次 4096 tokens；
实际取该上限与用户 provider 输出预算的较小值。选项经原 send/result-delivery/恢复路径进入实际 API
payload，计数与发送一致，不修改持久配置或后续普通请求预算。原有截断/结果未知保护保留：
截断 JSON 不执行、不自动重发。上限只防止失控无限生成，不把截断输出补成成功结果。

验证：API/history/JSON 选集 21 passed，参数校验位置调整后边界与截断 3 passed；Ruff、改动文件 Mypy 通过。
完整 driver 测试同时验证冻结请求中保存上限，原生及文本评审 payload 都使用 2048。

## 实机 3 与 11. 采样 grammar 的字符串长度兼容性

`calibration-json-writer-01` 正例未完成：KoboldCpp 返回三次函数式文本而不是请求指定的 JSON，
原流程以 no_progress 拒绝，未卡死或发布。请求上限已生效，单次响应约 13–14 秒。
固定相同 prompt 的诊断表明：简单常量 schema 生效；完整嵌套长度约束没有约束输出；去除字符串
minLength/maxLength 后返回规范 JSON。只有移除 maxLength 的探针出现 HTTP IncompleteRead，
保留诊断快照，不据此宣称通过。服务保持空闲，未重试结果未知请求。

JSON 采样投影现移除字符串长度边界，原冻结工具契约及 kernel 仍严格验证长度；请求 token 上限
负责限制生成长度。没有新增解析器、补造参数或第二个执行入口。
验证：native/text 参数边界两项通过，明确测试采样可表达长字符串而 kernel 拒绝超过原 4000 字符限制；Ruff 通过。

## 12. 判断单位保留完整句子的条件、主体与比较对象

finding 和报告答案按完整句子切分，避免把“在 WAL 模式下，”或“Before 3.22.0,”拆成脱离谓语的
独立判断。reported conditions 仍单独审查；任务要求继续使用原 lossless clause 范围，未合并或省略义务。
每句仍要求所有子句都有支持，不以主结论正确替额外推论背书。

验证：单位覆盖、比较对象、原文引用和条件选集 43 passed；Ruff 通过。

## 实机 4 与 13. 评审引用范围由真实批次确定

`calibration-json-writer-02` 已稳定返回 JSON，完整句子也得到正确单写者判断；但连续虚构 evidence=2
（实际只有一个 excerpt），并曾引用不存在的 summary 路径，三轮后 max_turns，未通过。
进一步将实际 evidence 索引上界、问题编号、报告路径、已引用 finding 与 unit 范围投影到同一冻结
schema。它们都是现有事实的可选范围，不替模型做语义判断；精确匹配关系仍由原 parse_review 校验。

验证：引用越界、混合批及完整集合校验 5 passed，Ruff/Mypy 通过。

## 实机 5 与 14. 所有规模都拆开语义职责

`calibration-json-writer-03` 正例通过，随后另外五例中，比较对象偷换被拒绝、版本条件正例通过；
但 workload_overreach、excerpt_absence、historical_overreach 三个反例被误放行。
这些只在显式 synthetic 校准存储中发布，不能算生产研究质量通过；尤其版本反例的 reason 已指出
“无条件”与原文有条件不符，verdict 却仍为 supported，显示混合职责下的宽松批准问题。

删除“小于 8 单位合并四种职责”的分支。任何规模都按 claims、paragraphs、requirements、
investigations 分批；每批提示只包含本职责规则，并用“证据全真而答案仍可能为假”的检查约束蕴含关系。
仍使用原单一 review invocation 和原工具回执，完整聚合后才可发布。

验证：选集首次 23 passed / 7 failed，七项均为夹具轮次数预期仍按旧混合批，实际已完成且无重复发布。
修正为该夹具的三种非空职责（调查数组原为空）后相关选集 8 passed；Ruff 通过。
实机 workload_overreach-02 已在答案批正确指出吞吐量必然提升无证据，但选错 basis=gap，校验拒绝，
因此结果是 incomplete 而非已通过反例验收。后续继续收敛此结构歧义。

## 15. 证据不足与报告明确声明缺口分开

若当前答案没有任何合法的显式 coverage gap，冻结 schema 的 basis 只允许 findings。
拒绝缺乏支持的事实句使用 findings+unsupported，不能用 gap 表示“证据不足”。原校验反馈也给出
可执行的字段修正；有真实报告缺口时仍允许 gap，不自动改写模型输出。

验证：范围、引用、缺口与批次纯投影选集 44 passed，Ruff 通过。

## 实机 6 与 16. 覆盖状态只允许原报告实际存在的缺口

`calibration-separated-*-01` 六例结果：两个正例 passed，比较偷换、片段缺失推全文缺失、历史条件
遗漏三个反例 failed；工作负载过度推论的答案批已正确拒绝，但覆盖批重复选择 acknowledged_gap，
整轮 incomplete。这轮没有误放行反例，但尚未达到六例全部正确完成。

当原 coverage 没有任何显式 gap 时，冻结 requirements.state 只允许 addressed/missing，禁止把评审
发现的问题说成报告已承认的缺口。有真实 gap 时保留原三种状态，仍由原完整校验检查对应问题。
验证：引用与批次选集 5 passed，Ruff 通过。上一轮并行 API/提示选集 25 passed，全生产 Mypy 576 文件通过。

## 17. 历史已发布判断保持原单位边界

新 assessment 标记 schema=2，对应完整句子单位；旧 schema=1 的已发布判断仍用原 clause 边界作只读
投影校验。新的冻结 basis 为 schema=3。两者复用相同 scope/校验函数，不重跑旧评审，不改旧原文或回执。
验证：旧单位不被新切分偷换，以及 native/text 完整 driver 发布/历史投影共 3 passed；Ruff 通过。

## 当前校准结果（不是完整网络研究验收）

Gemma 12B、JSON、thinking=false、seed=73。`calibration-separated-workload_overreach-02` 现正确 failed，
答案批与要求批都指出机制不证明本应用性能收益。其余采用 `calibration-separated-*-01` 原结果：

| 合成案例 | 预期 | 实际 |
| --- | --- | --- |
| writer_supported | passed | passed |
| workload_overreach | failed | failed |
| comparator_transfer | failed | failed |
| excerpt_absence | failed | failed |
| historical_supported | passed | passed |
| historical_overreach | failed | failed |

六例已各有一次完整、符合标签的实际结果，保留此前误放行与 incomplete 的原始证据。
这是限定样本校准，不证明同模型评审可靠性，也不替代下一步真实搜索、保存、修复与发布验收。

## 18. 根任务 required_tools 复用原提交回执

后续合成/修复从同一 run、此前已完成 writer invocation 的原工具效果恢复只读 prerequisite 投影。
只选择当前 required_checks 对应工具及其原父子调用；使用原 receipt 校验摘要、参数、成功状态。
完成 gate 合并该投影进行原有精确参数/结果判断，proof 引用原 effect ID；当前 session.executed 不伪造
早期行动。模型提示也明确这些是前一阶段实际完成的要求。没有第二份持久执行日志或双写。

验证：带精确 query 约束的真实 driver 修复/重入测试通过，整个 run 只搜索一次且保留原搜索 effect ID。
另有参数、父子调用和答案观察要求选集 53 passed；Ruff、改动文件 Mypy 通过。

## 19. 研究提示限定实质问题，保留推论的显式不确定性

research_plan 明确注册不同的事实问题，搜索/引用/撰写作为执行要求，不单独膨胀成重叠问题；
一个事实问题通常只需一个 qN。这是规划指导，不用关键词替模型裁剪问题。
精简后的 claim 提示同时保留原有 inference 规则：评审其前提、条件与显式不确定性，
不把明确标注为假设的推论误当成已观察事实，也不要求它伪造直接实验。

验证：scope/提示选集 44 passed，Ruff 通过。下一轮开始完整真实网络研究，源码冻结。

## 实机 7 与 20. 每批固定一条 finding 或一段答案

`single-json-02`，版本 `4f736aa1`，真实网络 18 个请求、约 238 秒；搜索 2 次、打开官方 isolation.html
一次、保存 3 个 finding。未再触发通用补查，API 均及时返回，没有服务解析卡死。
模型仍把简单问题拆成三个近似问题，跨问题采用需要几轮修正。评审输入已缩到约 3k tokens，
但一批有不同长度的 f1/f2/f3 单位，纠错时发生串号；f3 末尾的 [f1] 还被当作独立待审事实。
四次评审未提交完整有效判断，以 research_review_incomplete 结束，未发布。

claims/paragraphs 改为每批一条 finding/一段答案（长条目仍每批最多 8 单位），故数量、索引、证据范围
都能精确投影到该条目的同一冻结 schema。要求和调查仍按 8 条分组，没有新调度或游标。
验证：精确条目范围、最后预算轮完成、native/text 和批间真实重启选集 7 passed；Ruff 通过。

## 21. 引用标记不作为独立事实，派生报告不重复引用

当前 finding 的语义单位沿用答案的引用标记处理：[fN] 只作为展示标记，不是需要源文证明的独立命题。
实际支持关系仍仅来自保存时绑定的精确 passage/version，标记不能引入任何新证据。原 finding 内容保留；
旧 schema=1 单位不变。报告派生证据段不暴露内部标记，答案中已有的相同引用不重复追加。

验证：scope/finding 选集 50 passed，另有原文未改写、虚构标记不增加 evidence、渲染引用不重复的回归通过；Ruff 通过。

## 22. 批次投影类型检查收口

全生产静态检查发现第 20 项的条件表达式让 Mypy 将 deepcopy 的列表参数误推断成字典。
拆开原有两个赋值分支，运行语义不变；没有新增状态或类型忽略。
修改前冻结源码运行 batches/scope/semantic_review 全组选集：70 passed（200.57 秒）。
修改后全生产 Ruff、Mypy、Pyrefly、ty 均通过。

## 实机 8 与 23. 答案只能采用实际保存并审查的 finding 单位

`single-json-03`，版本 `8bdd29fc`，真实网络 19 个请求、约 158 秒，8 批评审一次全部提交，最终 done。
无 API 卡死或 coverage 缺失字段；发现和答案分批后没有再出现单位串号。但人工审查发现：
f2 只保存单写者结论，答案额外声称读写可并发；答案评审从 supporting_findings 内的原始 excerpt
借用了未被 f2 采用的另一句话，错误宣称 f2 支持该句。因此只记录机械完成，不记为质量验收通过。
结束时仍有 Playwright 未观察 TargetClosedError，需下一轮开启诊断定位，不能声称清理问题完全解决。

答案批的 supporting_findings 现在只投影真实 ref/kind/units，先前判断只投影 unit/verdict。
完整原文、判断理由和引文仍在冻结根 basis、finding 评审批次和原回执中，根聚合校验不变；
不复制新事实，不让答案绕过 finding 扩大来源采用范围。增加相应输入隔离回归和合成负例。

仅隔离输入的 `calibration-separated-unadopted_source_fact-01` 仍误放行：模型以背景知识补全了读写并发。
继续要求答案判断的 reason 先列出未被 cited units 建立的命题，明确“已知为真”不等于本次引用支持，
不得借用记忆中前一批或领域常识。`*-02` 已明确指出 finding 只约束写者数，未建立读写并发，返回 failed。
此前错误原样保留，新的结果仅是一个负例通过，不能证明一般语义可靠性。
批次依赖仍以原始完整 assessment 的摘要绑定，防止精简显示后丢失回执依赖校验。
验证：投影/范围/回执 6 passed、native/text driver 2 passed、真实批间重启 1 passed；Ruff/Mypy 通过。

## 24. 规划示例与单一事实问题一致

两轮最小实机仍将“写者限制 / 文档怎么说 / 适用范围”拆为三个重叠问题；research_plan 示例本身
却示范两个问题，抵消了先前的一问指导。示例改为一个包含证据范围的问题，描述只在用户请求独立主题时
拆分，避免把同一问题的依据另列为问题。原 1..8 问契约和完整覆盖校验不变，不自动合并模型问题。
这属于提示修正，实际效果由下一轮验证；Ruff 通过。

## 实机 9 与 25. 已确定的支持关系失败直接进入原修复流程

`single-json-04`，版本 `739ab5f4`，21 个请求、250.6 秒，最终 research_review_incomplete。
规划仍拆成三问，提示修正尚未解决重复规划；保存时选中的首段讲读写并发，却用背景知识补出单写者。
初审曾误放行该 finding，复评判为 uncertain；随后答案持续 supported 并引用该 uncertain unit，
卡在“supported paragraph cannot adopt a rejected finding unit”，未能完成失败评审并进入下一次补证。
开启 asyncio debug 后本轮没有复现 TargetClosedError，尚未找到偶发清理异常的可靠原因。

支持关系的确定性失败（supported finding 无引文、supported 答案无支持单位或引用已拒绝/无引文单位）
从格式重提条件移到原 review_issues。原模型 verdict/reason 仍完整保存，issues 说明 host 的失败依据；
完整评审返回 failed，由同一 review_and_repair 补证。错引用、越界、伪造引文、缺字段仍是协议错误。
发布前以及历史展示投影均重查 review_issues，故这些情况依然绝不能发布成成功报告。
没有自动把模型结论改成 supported，也没有新评审器、调度器或状态表。

验证：42 passed，包含原始判断未改写、伪造 passed 标记仍被完成 gate 拒绝、
完整 driver 无协议重写地进入修复且只发布一次；Ruff/Mypy 通过。

## 26. finding 评审先找未建立的断言

finding 判断与答案判断采用同样的证据边界指导：reason 先指出 excerpt 未建立的断言，不以背景常识
补齐来源，也不把相关词语当作支持整句话。新增来自实机的 `background_finding` 负例。
`calibration-separated-background_finding-01` 明确拒绝从读写并发片段推出最多一个写者；
答案批仍选择 supported，但第 25 项的原依赖校验直接将完整评审判 failed，三批完成，无协议重写。
验证：实机负例完成且标签正确；Ruff/Mypy 通过。八例各已有一次符合预期结果，仍不代表普遍可靠。

## 实机 10 与 27. 引文使用原文有限选项

`single-json-05`，版本 `3fb9f8bb`，34 请求、380.1 秒。初审正确拒绝错误的 passage 绑定，
进入补证后实际找到单写者原句，并保存新 finding。复评第三批复制引文时重复生成同一段，
2048 token 上限使 API 正常返回 length，Codey 以 OutputLengthError 停止，未发布；不是原生解析线程卡死。
最新 batches/semantic_review 回归 31 passed，含实际恢复测试；本轮 debug 未复现浏览器清理异常。

评审工具的 quote enum 直接从本批完整 excerpt 的原句/连续片段生成，每个选项最多 1000 字符；
只归一化原先校验已允许的排版空白。全部原文仍在 basis 中，选择不能引入原文以外的字句，
原 quote 与 evidence 索引对应校验继续执行。同一冻结 schema 约束 native、JSON 采样及 kernel，
没有新增引用写入口或解析器。这样避免一个自由字符串里无限续写引文。

验证：batch 全组 11 passed（含两种协议、实际重启），Ruff/Mypy 通过。
`calibration-quote-choice-01` 使用失败请求的原 claim/excerpt 作显式合成重放；三批完成且无重复生成，
但评审 failed：末尾右括号被句子切分器独立切成事实，且任务范围说明不足。此重放只验证生成收敛，
不计正例验收通过；括号问题下一项修复。

## 28. 句子边界保留闭合括号和引号

完整句子切分把句末闭合标点保留在前一句中，不把 `）`、引号等作为额外事实；保留真实起止偏移。
新 basis=4、assessment=3。历史 schema=2 用原句子边界，schema=1 用原 clause 边界，
共用同一个 scope/投影校验入口，只读旧数据，不重跑或重写历史判断。
验证：scope 与两种协议完整发布选集 47 passed，生产 Ruff/Mypy 576 文件通过。
有限引文 schema 下的 `calibration-quote-choice-writer-01` 正例三批完成并 passed，原句支持校验保留。

## 实机 11 与用户配置落地

`single-json-06`，版本 `ce926dc0`，seed=74、预算 60，20 次请求，约 162 秒，最终 done。
完整采集、8 批评审和发布均完成，无引文失控、传输卡死或 TargetClosedError。
人工核对原知识中的完整 passage，确有单写者、读写并发及单 WAL 文件原因，结论与该原文一致。
仍存在三个重叠问题以及重复证据展示；同模型答案评审也仍会把主结论支持泛化到附加语句，
所以本次仅确认这份最终文本有对应原文，不宣称严格 finding-unit 语义判断全面可靠。

用户实际 `C:\Users\Administrator\.codey\local-openai.json` 已通过原配置模块将 native_tools_mode
从 auto 改为 json，thinking=false；连接身份 revision 更新，地址、模型偏好、密钥和预算保留。
通过生产 connect_local 只读连接检查确认仍选到原 Gemma 4 12B，native_tools=false、text_tool_format=json_schema。
这不再仅限测试 harness 配置。已有冻结对话保留其原协议；重新启动 Codey 后新建任务使用新代码/新配置。
KoboldCpp 原生解析器本身没有被修改，解决方式仍是同一 Codey API/kernel 中已存在的 JSON 协议。

## 实机 12～13 与 29. 多工具 JSON 也绑定完整参数契约

`wal-full-json-01` 在第一个请求超时，服务诊断仍显示上一轮结果；py-spy 确认请求线程阻塞在
`utfprint -> print`（服务输出输入日志，尚未 transform/generate）。这是外部服务日志输出阻塞，
不是本轮工具解析或模型推理失败。栈已保存；用户随后主动重启服务，模型不变。

重启后的 `wal-full-json-02` 已正常搜索/打开官方 WAL 页，但第 9～13 个响应连续漏掉 source_search.url，
五次相同纠错后 protocol 停止（13 请求、73.1 秒）。原多工具采样 schema 只限制 tool，args 无字段约束；
这个缺口使模型可一直提交明确缺字段的 JSON。

text_tool_schema 现在将每个可见工具与其同一冻结参数 schema 绑定为 oneOf 分支，保留 tool-before-args 顺序。
单工具评审格式不变；多工具也约束必填字段、状态 enum、数组边界；原字符串长度仍由 kernel 校验。
没有引入独立工具清单、第二个解析器或重试执行入口。普通 prose 请求不变。

验证：JSON/provider 测试 10 passed，含 source_search 缺 URL、工具/参数错配、规划超过 8 项、
strict done 缺 coverage.state 全部被采样契约拒绝。服务实际编译规划 4 工具、采集 11 工具 schema 成功。
使用第 13 个失败请求的原始 prompt、同 seed=73，仅替换采样 schema 的诊断重放，1.64 秒返回
带原官方 URL 的正确 source_search 参数；未执行工具，不计端到端成功。Ruff/Mypy 通过。
此前完整 API payload、原搜索要求复用及四个真实恢复边界选集 7 passed。

## 实机 14 与 30. 页内检索保留不同查询词的命中

`wal-full-json-03`，版本 `8df8fe03`，50 请求、约 662 秒，最终 no_progress，未发布。
多工具参数约束已生效，所有请求正常返回；初审能完整提交并进入原修复流程。
但两次查询 `SQLITE_BUSY WAL mode vs rollback journal` 的前六条都只有 WAL/mode/rollback/journal，
真正的 BUSY 段落被相同词组合挤掉。原文偏移 25433 处确有对应章节，故不是资料缺失。
修复阶段还混淆 passage 引用并重复保存错误证据；评审把一些方法/上下文误当遗漏问题，
且误放行 checkpoint 条件外推。这些均保留为失败证据，不能把协议正常视为质量通过。

页内排序在保留正文优先、原匹配分数和稳定位置顺序的同时，优先补齐尚未出现在所选结果中的查询词。
HTML 继续去除相邻重复命中；PDF 仍每页至多一条，但在选择页面时保留该页候选窗口，
避免预先只留下常见词窗口。无需模型、主题关键词规则、外部索引或新的检索运行时。

验证：页内检索、gateway 与原回执恢复回归 21 passed，Ruff/Mypy 通过。
对本轮已保存的完整原文运行生产 search_text，同一查询的第二条已包含 SQLITE_BUSY 章节，
重放结果保存在 `source-search-coverage-replay.json`；这是只读检索验证，不计端到端成功。

## 31. 搜索命中从完整上下文开始，阅读窗口不留下可避免的残句

实机命中直接从 `checkpoints frequently` 开始，丢失前面的读性能条件；另一个窗口尾部只剩
`There is write permission ... -shm a`，模型却将该残句保存为版本结论的证据。
新的 source_search 定位在命中前 600 字符内回到段首或句首，显式 open_url 偏移仍保持原义。
当窗口中已有完整句子时，尾端停在最后完整句边界，下一次读取使用真实结束偏移，
不跳过被收回的残句。没有句界的长文本仍有界显示；不凭空补全源文。
passage 身份仍由同一来源快照和真实起止范围生成，恢复仍只读原回执。

验证：搜索、读取、引用和回执恢复回归 33 passed，Ruff/Mypy 通过。
失败原文的 offset=11938 命中现在回到包含 read performance 条件的句首；尾部不再给截断的
权限条件分配可选 passage。相同段落的多个词命中在实际阅读偏移上去重。

## 32. 修复上下文保留可用的原 passage 和逐问题引用

`wal-full-json-03` 修复先把 f1 当作 passage，遭拒后选择了提示末尾另一个无关 pN。
原因之一是新 writer 的上下文只显示 claim/excerpt，遗漏了实际已经持久化的 passage 定位；
最近八项调查之外的 aN 也无法从当前视图确定归属。

原 evidence preview 现在只在 ledger 已含对应读取、且 URL/完整来源快照/片段文本均一致时，
显示可复用 passage_ref；知识来源自身仍不伪装为本轮读取。工作视图按原 qN 列出完整 fN/aN 绑定，
每条 finding 携带可用 passage_refs，继续保留上下文总预算和截断标记。
这些都是原知识和回执的只读投影，不新增引用库、执行记录或保存入口。

验证：真实 dispatch 修复/恢复、上下文预算及来源快照回归 23 passed，Ruff/Mypy 通过。
新增测试从原读取/保存回执重建全新 session，直接用投影 passage 修正 finding，禁止联网仍成功；
另一轮只有历史知识而没有读取回执时，不会获得伪造的可复用 passage。

## 33. 要求评审区分上下文、方法和实质问题

完整任务初审把“本机读多写少”的背景、“保存证据”和“约 800 字”等判为 missing，
理由竟是“这是上下文/方法/格式，不是单独问题”；可选的二手线索也被当成未完成义务。
要求批现在从同一冻结 basis 展示原调查动作的 ref/tool/args/ok，供其判断实际动作；
不新增待审 aN、不重复长网页。指导将背景/方法/格式映射到它们约束的原问题和报告段落，
不因未另设标题而报缺项，可选建议仍按可选含义判断；真实缺失的动作和答案仍须拒绝。
同时针对实机将 WAL 回绕条件转移为 checkpoint 完成条件的误判，明确检查条件所属动作与蕴含方向。

验证：batches/scope/semantic_review 全组 78 passed（212.04 秒），含真实子进程恢复。
生产 Ruff/Mypy（576 文件）及 ty 通过；Pyrefly 发现上一项上下文投影的两个空列表类型推断错误，
将在下一项独立修正，不改动验证期间的生产实现。

Gemma 12B、JSON、thinking=false、seed=73，四个合成案例分别经过真实评审 kernel：

| 案例 | 预期 | 实际 |
| --- | --- | --- |
| task_context_optional_method | passed | passed |
| task_missing_substantive_answer | failed | failed，指出遗漏长读/checkpoint 回答 |
| condition_transfer | failed | **passed，误放行** |
| condition_preserved | passed | passed |

原始数据位于 `calibration-task-conditions-<case>-01`。条件转移负例的理由已经承认原文约束
回绕而非转移/同步，却仍输出 supported。增加条件方向指导并未解决这一语义缺陷，
同模型评审不能视为可靠认证；这个短输入反例也说明问题并非全部来自 Codey 长上下文。
这四例只用于诊断，不能把三例符合预期称作一般 75% 成功率。

## 34. 上下文引用列表的类型检查收口

将无 ledger 时的两个空列表明确为 list[str]，不改变运行行为或增加状态。
全生产 Ruff、Mypy（576 文件）、Pyrefly（0 errors）及 ty 均通过。
在此前 78 项包含恢复的测试和四项实机校准全部结束后才修改源码，未改变运行中的实现指纹。
截至本次用户要求提交，所有实验均已结束，没有继续启动新的完整实机任务。

## 35. 取消强制自审，原发布事务承接确定性完成

`9d4844c5` 修复原五项失败，其中一项为空标题的生产 schema 缺陷，四项为旧测试契约。
`729627f4` 移除自动 review_and_repair，writer 完成后仅经原 kernel 回执和知识发布事务保存一次。
同模型诊断无发布权限，不得把 passed 升级为独立内容认证。报告保留未经独立语义审查的状态。
原任务 rN 登记由同一冻结工具/schema/编译器校验，队列复用原报告及发布凭证。

功能选集 106 passed，含五个真实进程退出恢复边界及只发布一次；
生产 Ruff、Mypy、Pyrefly、ty 通过。最小实机 11 请求、84.9 秒完成，之前同题一轮为 20 请求、162 秒，
这不是一般成功率测量。完整任务首轮仍失败：只注册五题却在 done 虚构 q6。

## 36. 最终提交前收窄问题编号，保留完整失败结论

冻结 done schema 从原 session.research_questions 投影实际 qN enum，JSON/native/kernel 共用，
旧快照不随后续注册改变；背景/格式要求应关联现有答案。相关选集 56 passed，
原协议/证据完整选集 228 passed，禁用 Python socket 且实际连接尝试为 0，生产静态检查通过。

完整实机第二轮在首请求遇到外部日志输出阻塞及服务重启；没有重发结果未知的请求。
重启后最后一轮 `deterministic-wal-full-03` 不再出现未知 qN，也没有自审，
但 22 请求、317.1 秒后因反复漏 r6（约 800 字中文结论的格式要求登记）以 no_progress 停止，未发布。
f6 的只读版本边界及所选证据支持关系仍错误，q8 缺口答案仍含未经验证的性能断言。
新覆盖登记也仍增加小模型负担，不能把拒绝错误完成等同于已解决研究能力。

扩大回归另有 66 failed、241 errors，已在修改前 checkout 复现失败及共享 parity 夹具错误；
详细范围、原始文件和局限见最新报告。按用户要求，结束本轮后提交并停止，不再追加实验。

## 37. 后续离线修复原扩大回归

收到后续修复授权后，已迁移旧测试契约并修复共享 parity 探针；原 66 项全部通过，
完整 parity 回归 731 项通过。另修正生产完成纠错提示及 PDF 段落包含生成页标记导致的页码丢失，
新增重复页正文与恢复验证；没有恢复强制自审、增加运行时或双写。
根因、范围、最终扩大回归与原始记录见
[后续修复报告第 7 节](research-self-review-convergence-20261031.zh-CN.md#7-原-66-个失败及-241-个错误的离线修复)。
本轮仅做离线验证，前述模型理解、选证和完整实机失败的结论保持有效。
