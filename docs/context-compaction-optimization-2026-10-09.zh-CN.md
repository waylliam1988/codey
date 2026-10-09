# 上下文压缩优化、根因与联合回放（2026-10-09）

本轮用 TDD 修正正常增长成本，增加真实回执读取和压缩完成后的独特证据追问，再按同一冻结代码重跑旧 Codey、OpenCode、Pi。当前本地回放中，Codey 的事实保留和成功配对 token 成本领先两个参考算法；这不是所有模型、所有任务或完整竞争产品的全面领先证明，也不是全局最优算法证明。

产品代码只修改 `api_provider.py`、`compaction.py`、`context_checkpoint.py`。原始账本、执行回执、协议 codec 和连接能力继续各自负责现有状态；Zen 仍可按连接包移除。

## 之前正常增长为什么更贵

上一轮诊断的三个 seed 合计：Codey 104718 token、OpenCode 96837、Pi 99090，分别多 8.1% 和 5.7%。这个差额有可核对的来源：

| 用量 | Codey | OpenCode | Pi |
| --- | ---: | ---: | ---: |
| 正常请求输入 | 85854 | 85964 | 83352 |
| 正常请求输出 | 1404 | 1404 | 1404 |
| 摘要输入 | 17304 | 8448 | 12552 |
| 摘要输出 | 156 | 1021 | 1782 |

前七次正常输入逐轮完全相同。相对 OpenCode 的主要差额是多 8856 摘要输入；相对 Pi 则是多 4752 摘要输入和最后两轮多 2502 正常输入。单纯延迟压缩阈值不能解决这个问题。

增长输出有 36 行、4129 字符，行号各不相同。旧 Codey 只编码连续完全相同行，因而仍把完整编号行和协议 JSON 送给摘要模型。两个参考序列化器在摘要来源中限制工具正文为 2000 字符，降低成本，却不能把“没有进入摘要的尾部”视为已保留。原评分又反复提示四个固定答案，没有检查旧输出中间和末尾的独特事实。

本轮所有算法都在统一的新驱动下重新测量；上表属于历史诊断，不能把它与本轮绝对 token 数混算改善比例。

## 最终修改

| 函数 / 归属 | 行为与验收 |
| --- | --- |
| `summary_source()` / `context_checkpoint.py` | 相同文本用重复次数，编号行用公共前后缀与全部差异值；分段处理不同结构，保留原始换行、Unicode、中间/末尾值。编码本身可精确还原原文。 |
| `_summarize_context()` / `api_provider.py` | 编码与原文经所选连接计数比较；字符短但 token 多时选择原文。独立系统指令明确历史问题是数据，只写简洁工作状态，省略空字段和套话。 |
| `_old_outputs()`、`reduce_old_outputs()` | 只处理较早闭合工具结果，保留最新完整往返。无回执时做可逆编码；有可信回执时保存只读定位，不重复携带前后各 600 字正文。 |
| `_result_ref()` | 使用运行时最后追加的行级结果 ID。正文引用另一个已知回执不能覆盖它；末尾回执不可用时保留正文，不借用前面的引用。 |
| `_schedule_maintenance()` | 无回执的低成本视图提前维护；有回执的视图在 60% 输入压力、至少两份旧结果时批处理；80% 触发语义维护。压力估算只调度，不代替准入计数。 |
| `CompactionCoordinator._reduce_outputs()` | 候选须实际计数更小；复用来源与候选计数，提交前仍对当前尾部重新计数。低成本事务保留必要的三次计数，不重复探测同一内容。 |

60% / 80% 是当前策略，不是所有模型的最佳比例。原始事件和摘要来源仍可追溯；可逆编码不等于保证模型的语义摘要永不遗漏。命令退出码、验证是否过期、结果是否可读继续由运行时决定。

没有新增产品界面；维护不抢焦点、不改变阅读位置。Run details 继续计入辅助请求服务端 usage，而最后正常请求上下文不被辅助请求覆盖。核心不增加 Zen 类型或未来厂商框架。

## TDD 与调参过程

新行为先失败，再修复相同用例，随后定向回归。有效红测包括：变动行无法编码、独特证据丢失、字符短却实际计数更贵、摘要回答历史问题、低成本维护触发过晚、重复计数、回执逐条改写、错误引用覆盖末尾回执、评分把局部退步掩盖成整体收益。

五个准确命名的新脚本共 29 个用例：

- `test_compaction_source_encoding_and_summary_contract.py`：11 个。
- `test_compaction_counted_tool_views.py`：12 个。
- `test_compaction_held_out_evidence_recovery.py`：4 个。
- `test_compaction_benchmark_request_timing.py`：1 个。
- `test_compaction_benchmark_policy_calibration.py`：1 个。

已有评分脚本新增两个配对用例，联合比较脚本新增一个，共增加 32 项全量测试。失败夹具、语法和服务器故障不冒充有效红测。

| 实验发现 | 后续处理 |
| --- | --- |
| 旧追问重复答案，缺乏独特证据恢复 | 新追问在维护完成后选择目标；中间和尾部标记由 seed/模块生成，不放进追问或输出 schema。 |
| 原文中旧问题影响摘要；空字段使小场景更贵 | 系统指令隔离来源、末尾重申当前任务；工作状态明确简洁且不把归档观察当待办。 |
| 逐条回执缩减省 token 但增加前缀重算和请求成本 | 低压力不逐条改写；至少两份结果、达到压力再批处理。 |
| 对已存正文再编码、同时试两份候选造成冗余计数 | 可信回执只使用定位候选；保留来源、候选、提交三个必要计数。 |
| 正文引用旧回执，可能恢复错误输出 | 两个确定性测试先红，修复末尾引用归属；最终联合回放包含此修复。 |

早期 v1–v11 包含调参、无效 JSON、夹具问题及中止的试验，未作为最终比较数字。v12 是修复引用归属前的完整试验，也不混入最终表格。所有最终比较均重新冻结并运行；评分按 `(case, seed)` 配对，任何原来通过而修改后失败都判回退。

## 最终联合回放

基线 Codey：`a28df7beb41fb3d1ff767b67c2c109848e05de2b`。最终 Codey 包 SHA-256：
`0eaa6f93252b138a6ce8ba03c10263e7d449f0cc3c3a7793b127d04bc7fd9f91`。

OpenCode 固定为 `ecc4916b5a9608c30e6dd58a67f2137b594407ca`，执行 `packages/core/src/session/compaction.ts` 的选择和摘要提示纯函数；保留该源码自己的摘要预算拒绝逻辑。Pi 固定为 `a276dabe57911253350bffb93cb7d7aff6a73261`，执行实际准备/压缩纯函数，包括长轮次拆分与两个摘要计划。它们使用共同传输，并非完整 agent。

本地 KoboldCpp 1.117.1、Jinja 开启，模型为 `koboldcpp/Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M`。服务端运行窗口 262144；逻辑窗口统一 8192、输出预留 1024、安全预留 0、近期目标 2000；temperature 0，seed 41/42/43。只有 loopback 模型调用，无付费 API 推理。token 取实际生成 usage，包含辅助摘要和回执读取。

八类 × 三 seed × 两侧 × 三组对照，共 144 条记录，没有基础设施错误。Codey 每组 24/24，共 72/72；它们是相同 24 个条件在三组对照中的重复回放，不是 72 种独立任务。

| 场景 | 旧 Codey | OpenCode | Pi | 最终 Codey（每组） |
| --- | ---: | ---: | ---: | ---: |
| 大型重复历史 `repeated` | 3/3 | 0/3 | 0/3 | 3/3 |
| 用户修正 `correction` | 3/3 | 0/3 | 0/3 | 3/3 |
| 已完成测试结果 `test-result` | 3/3 | 0/3 | 0/3 | 3/3 |
| 重复工具正文 `tool-heavy` | 3/3 | 3/3 | 3/3 | 3/3 |
| 分段增长 `segmented` | 3/3 | 0/3 | 0/3 | 3/3 |
| 正常八轮增长 `incremental` | 3/3 | 3/3 | 3/3 | 3/3 |
| 真实托管回执恢复 `receipt-recovery` | 3/3 | 1/3 | 3/3 | 3/3 |
| 没有回执的独特事实 `unrecoverable-facts` | 0/3 | 0/3 | 0/3 | 3/3 |

前六类是固定事实压力回放；最后两类增加八波观察中的中间/尾部独特标记，完成维护后才选模块追问。有回执时通过真实 `ExecutionDelegate`、`ManagedOutputStore` 和 `read_tool_result` 读取；无回执时不能虚构可读 ID。恢复请求和回答都计入用量。这个只读场景不提供重新运行命令的工具，因此其零重复不能外推为完整 agent 执行安全证明。

### 双方都成功的配对成本

| 对照 | 成功配对数 | 对照 token → Codey token | token 减少 | 对照耗时 → Codey耗时 |
| --- | ---: | ---: | ---: | ---: |
| 旧 Codey | 21 | 301718 → 215874 | 28.5% | 447.845s → 344.357s |
| OpenCode | 7 | 153347 → 115096 | 24.9% | 188.594s → 140.390s |
| Pi | 9 | 233886 → 184041 | 21.3% | 297.640s → 190.656s |

正常八轮增长三 seed：Codey **77841**，OpenCode **98724**，Pi **101802**；分别少 **21.2%**、**23.5%**。同一新驱动下旧 Codey 为 **108666**，改善 **28.4%**。三方每个 seed 均成功，没有以失去事实换取这个正常场景的评分收益。

重复工具正文三 seed：Codey **2832**，OpenCode **14772**，Pi **14463**；分别少 **80.8%**、**80.4%**。这与大型重复用户历史 `repeated` 是两个不同场景，不能混用。

### 全部条件与耗时取舍

| 对照 | 全部成功数 | 对照全部 token → Codey全部 token | 对照全部耗时 → Codey全部耗时 |
| --- | ---: | ---: | ---: |
| 旧 Codey | 21/24 → 24/24 | 419326 → 306077 | 559.627s → 438.529s |
| OpenCode | 7/24 → 24/24 | 336446 → 306422 | 373.737s → 447.249s |
| Pi | 9/24 → 24/24 | 337606 → 305849 | 426.938s → 438.046s |

参考算法在部分超限条件下没有生成，所以是零生成 token、快速失败；不能把它们与成功回答比较成“更便宜、更快”。保留全部条件表可防止只选成功行，也不能用成功配对表代替总体质量。

旧 Codey 的回执恢复是明确取舍：三 seed **130469 → 103596 token（少 20.6%）**，总耗时 **76.250s → 77.906s（多 2.2%）**。两边各 48 次 tokenizer 请求、30 次生成；生成 HTTP 时间 **59.701s → 61.923s**。因此剩余差异不来自多发 tokenizer 请求；缓存、输出和服务端推理波动仍会影响延迟，不能把它们都精确归因于一个因素。

A/A 同一旧代码两侧：**130469 → 130572 token**，**75.952s → 76.250s**，均 3/3 成功。一个 seed 出现不同输出，证明 seed/temperature 不是严格确定性保证。三轮样本不足以建立统计置信结论。全程未重置服务端缓存；每轮交替运行顺序。whole-wall 包含 worker/Node 启动；JSON 另记录各 HTTP 路径耗时，不把适配器启动费用都算成算法收益。

联合矩阵的 `dominates_all_measured_metrics` 如实为 **false**：部分执行指标未测，失败条件的零消耗不能与成功等价，旧 Codey 回执恢复也没有延迟领先。当前证据支持“这些条件里对 OpenCode/Pi 的事实保留更稳、成功配对成本更低”，不支持“所有指标、所有负载全面胜出”。

## 最终快照的补充运行任务

同一最终包另跑三 seed × 两侧 × 三类，共 18 条记录；它们只对比旧 Codey，不作为
完整 OpenCode/Pi agent 的任务完成率。

| 场景 | 旧 Codey / 最终 Codey 成功数 | token 合计 | 耗时合计 |
| --- | ---: | ---: | ---: |
| 真实内核读取 `app.py`、把 RATE 5 改为 7、执行 pytest、独立验证 | 3/3 / 3/3 | 38486 → 37632 | 72.563s → 68.453s |
| 注入摘要失败，验证有效历史仍保留 | 3/3 / 3/3 | 0 → 0（失败在调用前注入） | 7.031s → 6.937s |
| 普通工作状态重新连接同一已加载模型 | 3/3 / 3/3 | 2478 → 2247 | 25.859s → 22.312s |

真实任务两侧共六次完成，agent 每次只执行一次 pytest，重复命令数为零；另由
测试脚本执行独立 oracle 验证，不把 oracle 的运行记成 agent 重复。重复判定包含
命令、cwd 和工作区 revision，修文件后重测不误算重复。此任务 token 减少 2.2%。
摘要失败是确定性故障注入，不模拟服务端已消耗 token 后断流；重连也没有加载另一
个模型。记录明确标注这两个范围，不能外推异步执行恢复或真正的跨模型实验。

## 复现

先运行确定性测试，真实推理另行选择：

```powershell
python -m pytest -q tests/test_compaction_source_encoding_and_summary_contract.py tests/test_compaction_counted_tool_views.py tests/test_compaction_held_out_evidence_recovery.py tests/test_compaction_benchmark_request_timing.py tests/test_compaction_benchmark_policy_calibration.py
python -m tests.manual.context_compaction_joint_ab --cases repeated,correction,test-result,tool-heavy,segmented,incremental,receipt-recovery,unrecoverable-facts --repeats 3 --output-dir artifacts/context-compaction-ab/optimization-final
python -m tests.manual.context_compaction_benchmark_ab --cases real-task,summary-failure,model-change --repeats 3 --output artifacts/context-compaction-ab/optimization-final-runtime-v2.json
python -m tests.manual.context_compaction_benchmark_ab --control --cases receipt-recovery --repeats 3 --output artifacts/context-compaction-ab/optimization-aa.json
```

推理必须串行使用本地模型；脚本冻结源码和比较驱动，记录模型、来源与参考摘要哈希。A/A 两侧实际运行同一基线，不把冻结工作区元数据误当成两种算法。输出 JSON schema 只限制语法/形状，不给期望值，不用于辅助摘要或真实改码任务。

## 最终检查与可证明边界

Ruff、mypy（402 文件）、Pyrefly（0 errors，既有 68 suppressed / 832 warnings）、compileall、20 个 JS 资产语法和 diff 检查通过。必跑契约 **798 passed，169.29s**。最终全量 **8091 passed、7 skipped、1503 subtests passed，898.23s（14 分 58 秒）**。

首次全量为 2 failed / 8089 passed：usage 测试的部分 codec 替身遗漏 `closed_spans`。夹具改为只包装真实解码方法，18 项定向回归通过，并把该文件加入必跑契约；随后重新检查和全量通过。生产代码在两次全量及最终 A/B 期间保持冻结。这个夹具修正不冒充 TDD 红测。

文档在最终全量完成后更新。版本保持 0.5.11，无 tag/release。完整数值及来源哈希见[脱敏记录](reports/context-compaction-optimization-2026-10-09.json)，验证索引见[TEST_REPORT](../TEST_REPORT.md)。

实际测到的是一个本地模型、英文合成历史、Chat 协议和小型编码任务。没有测真实跨模型切换、异步仍在运行的 pytest、崩溃后大输出恢复，或完整 OpenCode/Pi agent。普通不可压缩独特正文仍可能需要语义取舍，不能从可逆模板的成功外推所有历史都无损。原始数据和执行事实的保留是恢复基础，不等于每个模型都会主动正确恢复。
