# Research q8 缺口文本编译修复，2026-10-31

后续 r6/q1/浏览器修复、验证及仍未解决的边界见
[最新收敛报告](research-r6-q1-browser-20261031.zh-CN.md)。以下保留 q8 轮次的历史结果。

## 问题与范围

完整 WAL 历史运行 `deterministic-wal-full-03` 的 q8 被模型标为
`not_investigated`，`findings=[]`、`investigations=[]`，但 answer 肯定了本机读多写少
应用的性能优势，并写出“写入速度极快”。这不是本机测量结论。
旧编译器把同一条 answer 用于结论、问题覆盖和局限，错误也随之进入段落绑定。

保存的最终原始回复现已逐字段核对并摘录为
`tests/fixtures/research_live/q8_uninvestigated_answer.json`。独立诊断进程加载
修改前 `7a7782e6` 的原报告提案实现，再编译该输入，确认错误原话同时存在于
报告正文、coverage 和 paragraph_bindings。原始历史文件未被修改。

## 实现

当前 strict Research 只接受逐问题 coverage。该提案形成时，程序把
`not_investigated` 和 `unanswered_after_search` 的 answer 编译为带 qN 的固定缺口说明。
这一替换发生在形成结论、段落及引用绑定之前，因此报告正文、coverage、局限、
知识 answer、合成笔记和恢复回放共用同一份编译内容。

原始模型请求及执行回执继续保留原话，编译不修改传入参数；proposal digest 仍绑定
原始请求。新回归还检查原话确实存在于原 provider/execution artifact，而不会进入
成功发布的知识版本。没有新增运行时、写入口、事实存储或强制自审。

`partial` 和 `evidence_found` 的回答保留原样；部分可回答的问题应选 `partial`
并关联真实 findings。缺口状态仍要求空 findings；调查回执的存在、问题归属和状态
一致性继续校验。通过输入校验后，缺口采用该问题在原 ledger 中的全部真实调查回执；
已有尝试但模型漏报时，程序投影为 `unanswered_after_search`，不再宣称从未调查。
失败的调查回执也只是尝试，不会被升级为证据或答案。不会为了完成而补造记录或放宽引用检查。
所有问题都只报未知时，
原有“没有选中任何证据”拒绝条件继续有效。

工具指导与纠错文案同步说明：缺口 answer 只是占位，最终缺口说明由程序生成。
接口没有增加模型必填字段。已归档报告及历史 expanded 提案继续按原契约读取，
不把历史数据重新解释为新报告；当前 strict 生成入口仍拒绝 expanded 输入。

本项约束的是缺口状态中的肯定性正文传播。模型把错误结论标成有证据状态、
条件偷换或版本选错，仍需要后续单独验证和修复。

## 离线验证

原始 q8 反例覆盖两种缺口状态，以及与 `partial` / `evidence_found` 正常答案共存的情况。
测试核对报告、段落、诊断 basis、序列化恢复、知识发布，以及 native/JSON 两种协议。
现有五个真实进程退出边界也带入虚假的本机测量原话，验证恢复后继续使用缺口说明、
复用原发布事务，且不增加模型请求。

最终定点选集 **35 passed，53.81 秒**；覆盖真实成功/失败调查的漏报、两个协议下的发布与
恢复，以及未采用证据时的拒绝。生产 Ruff、Mypy（576 文件）、Pyrefly、ty 均通过；
三个类型检查器均使用 `codey --python-version 3.11` 范围。

原始输出目录：`.e2e-artifacts/research-q8-gap-20261031/`。

另行扩大到 golden 测试时，`test_controller_control_block_golden` 的旧快照缺少
已有 question_ref，147 项通过、该项失败。独立进程加载修改前的原工具契约及指导后，
同一项仍失败；本提交保留该无关失败证据，没有改写此快照。

另一次完整运行两份提示相关测试文件，90 项通过、6 项失败；这六项均不在原 Research
扩大选集中，独立进程加载修改前的四份生产源码后全部同样失败。它们仍依赖已移除的
提示、描述或 expanded 完成示例。本轮只同步与此次提示修改直接相关的摘要与短语断言，
不宣称整个仓库的所有测试通过。证据为 `baseline-extra-prompt-tests.log`。

原扩大范围 `pytest tests --ignore=tests/manual -k 'research or text_tool_json_format'`
最终为 **1558 passed、0 failed、0 errors，另 323 subtests passed**；9546 deselected，
429.35 秒。输出为 `expanded-verified.log/xml`；离线模型端口连接尝试为 0。
初轮 1552 passed、2 failed 的两处失败均为本次指导变更后的旧摘要/短语断言，已同步后
通过定点及最终扩大复跑。`expanded-final.log` 是为补齐实机发现的回执投影而主动停止的
中间运行，不计为验收结果；所有历史日志均保留。

## 12B 实机与内容核对

复用生产 headless 入口、同一 kernel/知识发布事务及原本的
`tests.manual.persistent_research_local` 采集边界。全新独立 state/knowledge，真实搜索与
官方网页读取；模型仍为当前 Gemma 4 12B Q4_K_M，JSON 模式、thinking=false、temperature=0.3。
未修改用户模型/服务配置，没有自审调用。任务原文为
`artifacts/research-q8-gap-20261031/question.txt`，含一个单写者问题及两个本机性能缺口问题。
这里的 q8 指历史错误类型；定点任务中对应的是 q2/q3。

| 运行 | 代码阶段 / seed | 请求 / 耗时 | 结果与边界 |
| --- | --- | --- | --- |
| live-01 | 首版缺口编译 / 73 | 12 / 100.86 秒 | done；发现 q2 已执行搜索却漏报回执，旧编译仍写“未调查”，促成本轮回执投影修复；退出复现 TargetClosedError |
| live-02 | 最终代码 / 73 | 12 / 109.33 秒 | no_progress，未发布；补 rN 时重复提交 q1，现有唯一问题编号校验拒绝；没有自动补填或放宽校验 |
| live-03 | 最终代码 / 74 | 12 / 121.16 秒 | done；q2 为未调查缺口，q3 为有真实 a6/a7 的调查后缺口；二者均采用程序正文，知识 answer、合成笔记、报告及段落绑定一致 |

第三轮只有一份发布的 answer，支持状态的 q1 原文回答未被改成未知；两个缺口没有加入
任何本机加速比、肯定性性能结论或“整个官方页面没有讨论”的断言。原模型 answer
保留在 wire 中，发布使用编译后的缺口说明。`live-03/gap-verification.json` 记录了
这些定点核对及存储/恢复格式一致性；该轮退出未出现 TargetClosedError。

**整份定点任务的内容核对仍不通过。** q1 问的是同一时刻是否只有一个写者，模型所选
完整 excerpt 和最终 finding 却只建立读者/写者并发；它没有选中明确给出单写者边界的
原句，也没有在答案中交代排他限制。不能把正文里的“与一个写入者并发”当成“最多一个”。
较完整的内容检查失败记录为 `live03-full-content.log`；q8 定点通过不替代这一失败。
没有据此宣称三题内容全部正确、原完整 WAL 任务验收通过，或 12B 已能稳定完成复杂研究。

## 状态与下一步

当前 strict 逐题报告路径中，缺口状态的自由肯定性 answer 不再进入发布内容；已有调查
也不能仅因模型漏报 aN 而被写成从未调查。此为本轮 q8 工程修复的验收范围。
已标成 `partial` / `evidence_found` 的错误理解仍不会被此规则识别。

下一项建议先处理 r6 类任务覆盖负担：保留原要求和位置，谨慎分离明确的报告格式约束，
混合或不明确要求仍保留实质覆盖；避免让模型为了全局要求重复创造研究问题记录。
再单独处理 f6 类局部答偏/选证错误，继续保持完整条件和原文版本的关联。

浏览器路由等待修复的既有记录保留，但 live-01 的退出警告证明不能再宣称完全闭环。
KoboldCpp 仍使用 JSON 规避原生工具解析路径；本轮没有修复服务端解析器或日志输出。
