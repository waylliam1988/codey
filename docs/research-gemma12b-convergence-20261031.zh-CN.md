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
