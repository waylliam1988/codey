# Gemma 4 12B Research 收敛实施记录

2026-10-31，基于 `060656db` 和 [诊断方案](research-convergence-diagnosis-20261031.zh-CN.md) 逐项修改、验证、提交。
用户已授权使用当前本地 Gemma 4 12B 做真实验证；每个问题单独提交，保留原始输出。

本地 `/v1/models` 确认部署为 `koboldcpp/Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M`。
小模型成功是有价值的工程证据，不自动证明另一模型一定成功。

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
