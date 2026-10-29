# Operation 执行与恢复统一验收

## 阶段一：现有行为基线

生产基线：`1e3ea099`。本阶段只增加测试、测量夹具和本文档，不改变生产控制流。

`tests/support/operation_trace_probe.py` 通过真实 `run_task_kernel`、
`KernelRecordedProvider`、`execute_turn`、mutation line 和 durable generation boundary
运行确定性外部观察。它不实现另一个恢复执行器。脚本响应耗尽会报错，不伪造完成。

基线保存在 `tests/fixtures/operation_execution_baseline.json`，包含 21 种轨迹、
provider effect 数、工具执行/回执数、逻辑轮数、历史种类、pending effects、
数据库提交数、checkpoint 字节和耗时。正常轨迹重复三次验证语义相同；耗时取中位数，
只作同机诊断，不设跨机器阈值。

重测命令：

```powershell
python -m tests.manual.operation_execution_baseline
python -m pytest -q tests/test_operation_execution_traces.py
```

需要保持的行为：

- 文本及原生 read→done：两逻辑轮、一次工具执行；原生原 call id 进入结果交付。
- 协议修复及完成拒绝：保留原 call id；修复请求使用原声明；不产生工具副作用。
- completion gate 异常：对外沿用暂不能完成并继续的行为，内部故障分类后续补齐。
- 原生截断：无工具调用时只允许一次续写；合法工具调用的截断行为由既有测试锁定。
- 问题身份稳定；审批保持原调用位置并暂停，不运行命令。
- provider 返回后的取消阻止工具执行；已有工具结果仍正常记录和交付。
- 无进展观察、正常轮数、provider 调用数和协议关闭预算分别计算。
- 预算结束的原生收尾只回答调用，不执行收尾回复提出的新工具。

故障观察点：provider 回复返回、工具开始、post_tools 提交前和提交后。
当前夹具抛出 `BaseException` 绕过控制层异常映射，以保留真实已提交事实。
它是边界观测基线，不代表已验证真实进程重启；后续必须增加 SQLite 子进程强杀及双次恢复测试。

现有专项测试继续锁定工具注册变化、同轮冻结声明、provider 窗口、审批与完成证明；
durable approval/question 专项测试负责真正的等待记录持久化，内核事件探针不代替这些测试。

已确认需要修改、不能作为保持行为的缺口：

- 回复已返回但尚未跨 generation boundary 时，回复内容和待结算状态只在内存里。
- `TaskCheckpoint.phase`、operation leaf、正常 loop 和 durable recovery 各有执行决策。
- 协议修复、done、question、收尾辅助函数可以隐含请求 provider。
- 截断、无进展和关闭计数依赖当前进程；恢复不能重新获得预算。
- 恢复路径直接执行工具；最终应委托同一个 driver 和 `execute_turn`。

上述缺口修复可以改变相应故障结果和提交量，但必须单独说明，不能借机改变正常轮数、
授权边界或重复执行已结算副作用。

## 后续阶段的硬约束

只有 operation checkpoint 与 reducer 拥有当前执行位置。TaskStatus 继续表示调度所有权；
TaskCheckpoint 的 provider/tool phase 不得继续独立决定 operation 的下一步。
effect、delivery、submission/approval 和输出存储分别拥有各自事实，checkpoint 使用引用。
同一事务写入不同事实不属于双写；同一事实存在两套权威状态，即使原子更新也属于重复建模。

公开入口只可委托唯一 driver；旧格式只可做有版本、幂等迁移。禁止新实现失败后运行旧 loop、
从 trace 猜恢复状态、回执缺失后重执行、快照不匹配后换用最新实现，或生产 durable 缺失后转入内存执行。

附件中的 pytest 失败按现行契约逐项分类并回测相关文件；不恢复已删除的生产接口来迁就旧测试。
分阶段运行定向和重点 pytest；全量回归交给 GitHub CI。六阶段完成还需要本地 Gemma 原生/文本冒烟、
真实重启恢复及与 pi/DeepSeek 的等价场景比较，当前阶段未声称这些已完成。

## 阶段二进展：回复事实与停止完整性

已将 TaskCheckpoint.phase 收窄为调度生命周期，删除独立的 durable recovery 决策器。
operation schema 2 增加 provider_response_ready 与受管理输出引用；请求 artifact 保存
原始请求、逻辑轮次、冻结工具契约和协议窗口，回复 artifact 保留原始错误参数。
回复可靠保存后恢复使用原回复，不再依赖内存中的 pending provider settlement。
原生工具调用历史随回复先于工具结果提交，聊天投影不参与恢复结果判定。

停止调度不再制造 provider/tool 回执或清空 pending effect。writer、repair 和 terminal
结算共同核验原始回执；只有已知且完整的结果批次可以原子标记为 abandoned。
pending/unknown 状态阻止运行阶段被强行结算，提交失败保留 operation 引用及错误来源。
已增加 SQLite 子进程退出、提交成功但返回失败、重复恢复及终态完整性测试。

本次收口验证：相关停止/生命周期测试 125 通过，最终完整性测试 113 通过、38 subtests；
生产 mypy 检查 532 文件通过，相关 Ruff 与 git diff --check 通过。
这些结果不代表全部六阶段已经验收。

尚未完成的工作：唯一 driver；删除旧 loop、
隐含 provider 请求和恢复执行控制。特别是审批期间取消，当前诚实保留未结算事实，
仍需通过统一关闭动作完成原调用拒绝回执和 generation boundary，不能把此中间状态
当成已完整实现取消收尾。外部输出正文与历史投影的物理去重也需单独验收。

### 阶段二进展：有界执行计数

operation schema 3 保存协议错误、有限续写、关闭轮次以及有界进度归约，不增加第二个 phase。
协议错误与续写计数在下一请求接纳时，与 provider intent 原子提交；恢复重新解释旧回复
不会重复扣减计数。关闭次数在每次关闭请求接纳时消耗，重启不能重新获得四次额度。
请求接纳验证计数只能发生合法变化，不能覆盖已提交的批次进度或把已使用预算重置。

进度归约保留最多 256 个输出摘要和 16 个尝试，last_batch_id 引用现有交付批次，
让相同批次归约与提交可以幂等重试。停止判断从归约数据计算，没有再保存一个独立停止标志。
这些是按已提交执行事实计算的有界历史；无需重读任务所有回复和回执来决定下一步。
ToolAttemptRecord 的唯一数据定义移入纯 core，原 agents 模块继续导出调用方使用的同一类型。

真实进程退出测试覆盖第二次截断、协议错误、关闭进行一半和进度提交后退出，
验证原逻辑轮次、剩余关闭预算以及同一批次不重复累计。另修复修复/拒绝请求的逻辑轮次
记录：立即返回的下一条回复绑定下一逻辑轮，同时仍使用实际发送时的冻结工具快照。
正常轨迹的请求数量、工具次数和轮数保持不变。

旧格式缺少活动执行计数时明确阻断，不能通过填零发放新预算；尚未进入执行或不再恢复
原执行的生命周期位置可以作确定性格式迁移。该迁移不猜测旧进程中的 pending 字段。

相关回归首次 708 通过、5 跳过；发现的两个失败分别为新增模块后发布清单未更新，
以及 Windows 深任务目录中原子临时文件超过 MAX_PATH。已更新发布清单，并使用 Windows
文件系统路径语法保留完整随机临时名，真实进程恢复测试恢复通过。追加接纳约束后的专项
测试 91 通过、12 subtests，生产 mypy 与 Ruff 继续检查。

最终相关回归：736 通过、7 跳过、18 subtests；生产 mypy 533 文件通过，Ruff 与
git diff --check 通过。没有运行全量 pytest。

仍未宣称阶段二或后续阶段全部验收：最后一项工具回执提交后、进度归约提交前的恢复，
必须由统一 driver 识别未完成归约并补交；不能在旧恢复入口另添执行循环来掩盖此窗口。

## 阶段三进展：冻结请求与显式 provider 动作

operation schema 4 增加 `provider_request_ready`，没有独立 Kernel phase。
`request_ref` 指向已有 managed output 存储中的一份冻结请求：在外部 intent 接纳前，
仅靠 effect 还无法表达这份待发送工作，因此执行位置必须保留此引用。接纳后，
原 provider effect 的 `request_ref` 接替引用，operation 随阶段前进清除请求引用。
effect intent schema 2 只增加该引用，原 version 1 按原格式解码，不改写已有 effect 身份。

冻结请求由 task 拥有；原始回复仍由已接纳的 effect 拥有。不先制造一个 effect 来满足
artifact 外键，也不建立第二张动作账本。冻结内容中的 intent 是待接纳动作的定义，
只有现有 effect 表中的 intent 才表示已经接纳。正文先原子落盘，再在现有 durable
事务内登记 artifact 和 operation 引用；失败前留下的未登记文件不成为执行事实。

`operations/kernel_driver.py` 接替 provider wrapper 原来的接纳、调用和结果提交职责。
正常请求和恢复到冻结请求都由现有 reducer 选择同一组明确动作：

```text
provider_request_ready
  → 必要时 CommitGenerationBoundary(before_provider_request)
  → SubmitProviderRequest
  → provider_effect_pending
  → 保存原始回复
  → provider_response_ready
```

该内部边界只关闭上一代，不放置用户 submission。需要关闭上一代时，operation
暂时保留其原 response_ref；结算后只清除该旧引用，不能丢掉下一请求。请求的稳定
effect 身份、原调用方式、参数、声明、窗口和预算接纳条件都随冻结内容恢复。
恢复不会用当前工具注册表替换请求声明；原工具执行继续使用已有快照校验机制。

只有确实新接纳的调用方拿到外部执行权。接纳成功但回执丢失时，可以识别原 intent，
不会创建新 effect 或再次请求；没有可靠回复的原 intent 继续按未知规则处理。
外部调用后的 CAS 重试仅重提结果，不包含 provider 调用。

已删除 `KernelRecordedProvider._send` 隐含结算上一代的代码、内存 send_index，
以及 `defer_generation_settlement` 开关和立即结算成功回复的另一条路径。
所有成功回复均先保存，业务结算由明确边界负责。恢复装载器遇到待发送请求时，
只转交原请求，随后由同一 provider driver 接纳和执行。

真实 SQLite 子进程退出覆盖首次冻结、后续请求冻结、上一代刚结算、intent 刚接纳、
回复刚保存五个边界，并重复恢复两次。另验证丢失提交确认、损坏请求、冻结后的取消、
原生 bound 调用参数/声明/窗口，以及内部边界不会提前放置插话。

对照 `fca2f35d` 的不可变代码副本，同机串行测量 21 条轨迹：请求、工具执行、回执、
轮数、历史和停止结果均未变化。三次测量的中位数仅作本机诊断：

| 场景 | 提交次数（前→后） | 耗时秒（前→后） | 最终 checkpoint 字节 |
|---|---:|---:|---:|
| 文本 read→done | 9→11 | 0.153→0.170 | 2489→2489 |
| 原生 read→done | 12→15 | 0.220→0.249 | 2489→2489 |
| 原生协议修复 | 9→12 | 0.209→0.222 | 1316→1316 |

每个 provider 动作多一次冻结提交，换取 intent 前与上一代结算之间的可恢复位置。
这些数据不代替最终性能验收，也不代表完成 pi/DeepSeek 的等价故障比较。

本阶段仍未完成整个主链迁移：旧 task loop、控制分支、恢复层直接执行工具和外层
生命周期选择尚需移交统一 driver。最后一项工具回执与进度归约之间的故障窗口、
审批取消收尾及输出/历史的物理去重仍保留为未验收事项。

## 阶段三：主链进入唯一 operation driver

`task_loop.py` 已替换为直接委托入口；删除内存 pending 控制槽与旧 for 循环。
现有 operation schema 5 增加 plan/completion/control/closing/kernel_settled 执行位置，
没有第二个 phase。`logical_turn` 保存原逻辑预算位置；现有 `turn` 仍是 provider
或工具 effect 的坐标。`work_ref` 仅引用已形成的控制决定与完成检查结果，
正文及 proof 存在现有 managed artifact，供确认失败后恢复而无需重做完成检查。

新增 contracts 接替旧入口的依赖数据类型；actions 接替旧 loop 的工具接线与提示构造；
context 接替资源装载、动作输入恢复与执行；control 接替旧 done/question/close 辅助流程。
reducer 统一选择工具执行、进度、边界、交付动作，恢复装载入口不再执行工具。
原生会话已重放的工具结果不会被当成新的工具结果再次发送。

阶段验证：原行为轨迹 22 项、主入口进程恢复和 headless 生命周期 3 项、新增 SQLite
子进程退出与重复恢复 4 项通过。新故障点覆盖 plan、tool intent、最后回执和完成决定。
这些结果只是本阶段主链验证；审批等待、取消事务、完成与插话竞态、旧测试接线迁移、
旧恢复实现删除、完整回归及 Gemma 实机尚未完成，不代表六阶段全部验收。

## 阶段四：控制决定、取消与事务边界

完成检查结果与问题 continuation 保存为现有输出 artifact 的引用。问题在通知和确认前
由原 durable question store 保存；通知失败可重放同一问题。已知未发送的完成确认重试
原 call id，不重做完成检查；不确定发送继续阻断。completion_check 故障保留内部分类。
问题存储实现迁到 durable 层，没有保留 app 兼容副本。

取消作为显式 reducer 输入；取消屏障仍拒绝新工具，只允许保存已获得事实与关闭回执。
final boundary 在同一事务完成 generation 结算、输入放置和 Kernel 结算；若放置插话，
同一提交撤销旧完成推进。结构化边界回执保存 submission 和 conversation revision 引用。

新增控制测试覆盖内存/SQLite 的响应后取消、通知失败、确认未发送、完成期间取消，
以及完成与插话的顺序。原生关闭次数仍由 operation counters 限制。下一阶段继续统一
真正的等待恢复、审批 reconciliation 与外层生命周期；全量回归及实机结果尚待验收。

## 阶段五：等待恢复与生命周期收口

恢复装载器仅核验 operation 并恢复已结算观察，删除旧 pending-effect 执行循环与
独立 CodeMode 恢复执行器。工具、CodeMode 容器及恢复均进入同一个 execute_turn。
审批的原命令、参数摘要和回执核对由 driver 的 ReconcileApproval 动作完成；
原审批记录仍拥有授权，driver 不获得新的命令执行权。问题回答经同一 reducer
选择继续动作，保留原任务、已消耗逻辑轮数和授权。

审批拒绝结果引用修复为随原审批结算原子保存。取消一个仍有协议/工具待处理
事实的暂停任务时，调度器只重新认领清理工作，由同一 driver 完成原调用拒绝
和有界关闭；不提前把它当作已干净结束。无未完成操作的取消保持原终止语义。

本阶段相关测试 83 项通过（另一个旧关闭辅助函数测试待迁移），生产 mypy
538 文件、Ruff 通过。最后阶段继续删除失去职责的 wrapper/发送辅助函数，
迁移旧测试接线、核验回复与历史引用、执行重点回归和 Gemma 实机；全量留给 GitHub CI。


## 阶段六进展：删除旧执行接线与重点验收

生产入口已经删除 recovered DTO、独立恢复 pre-pass、内存 pending 槽以及
KernelRecordedProvider 和隐含 provider 发送辅助函数。恢复测试改为先经真实 driver
提交原请求/回复/intent/receipt，再中断并从同一 SQLite 和同一 driver 继续。
只读 receipt 检查器留在 tests/support，不执行恢复，也不再被生产路径调用。
手工安全重放探针同样改为原 intent 提交后中断，不向 AgentRequest 注入恢复结果。

定向回归发现并修复：冷启动原生历史重复交付、成功审批回执 error=None 的错误拒绝、
knowledge_write 证据被两次投影、首次和续跑请求遗漏工作观察、已接纳安全重放的审计计数、
以及 Windows 深路径下原子输出/工作区快照/安装升级编译失败。
变更采集失败保留原始失败观察并交给完成 gate 产生 blocked proof，避免把观察到的编辑
当成净空 diff。测试资源一律显式提供 durable 和 managed outputs，没有生产内存降级。

本轮分组结果（有交叠，不能相加当作独立用例总数）：

- durable 定向组 645 通过、3 失败、5 跳过；三个旧断言迁移后均已在重点组复验通过。
- 入口/工具结果/审批恢复组 66 通过。
- 生命周期等重点组 169 通过、2 失败；两项检查器断言纠正后已复验通过。
- 原故障相关组 30 通过，覆盖变更采集、恢复与真实 wheel 安装升级。
- 最后重点组 94 通过，覆盖故障边界、审批 HTTP 续跑与完成证明。
- 最后核心组 129 通过、44 subtests，覆盖 reducer、快照、取消、完成证明、审批与入口。
- research 现有定向组 39 通过；这不代表外层多 invocation 的进程恢复已经验收。
- Ruff 通过；mypy 538 文件通过；Pyrefly 0 errors（34 suppressed，77 warnings）；ty 通过。

Gemma4 12B / KoboldCpp 的原生协议 smoke 4/4 通过，证据位于
`artifacts/operation-native-20261029-final2`：聊天、读取、真正跨进程恢复、shell 批准续跑。
重启用例在 edit 回执提交后退出（exit 75），由不同 PID 恢复，edit_executions=1，
原结果与原 call id 保留，验证命令通过。审批用例在原 run 继续，shell_results=1。
这些是选定的 smoke，不是完整发布矩阵。

文本协议 smoke 的聊天和读取通过；重启用例未进入注入故障点。原始回包连续四次为
Gemma 的 `<|tool_call>` 格式，系统按协议错误停止。证据位于
`artifacts/operation-json-20261029-final2`。当时归因于模型未遵循 JSON 契约；下节根因复核
发现测试连接遗漏了正式本地连接已有的解码器。这项旧结果仍为失败，原归因已更正。

### 阶段六收口：research 多 invocation 恢复

已补完上述外层恢复缺口。operation schema 6 只增加 `continuation_ref`，指向现有 managed
output 中的有界引用索引；当前执行位置仍是 `leaf`，当前 invocation 仍由 `writer_attempt`
标识。索引只引用原 Kernel 结果和原 acquisition 材料，不复制回复正文、effect 或 delivery
账本。该引用不能从当前 `work_ref` 推导，因为进入下一 invocation 后后者已经指向新工作。

`research_continuation.py` 接替旧外层反复调用 main/followup 时丢失的重建职责：已完成的
invocation 读取原结果和回执来重建投影，当前 invocation 进入原 driver。PlanExecutor 已经
提交的搜索和来源正文从原材料恢复；未形成可靠材料前的只读 acquisition 仍保留原中断语义。
原结果归档、前一批次合法终结和下一 invocation 接纳经过同一 mutation line 事务提交。
提交成功但调用方没有看到成功时，可以识别同一提交，不重新执行外部动作。

followup 笔记提议随原 knowledge_write 回执保存，恢复只重建原 staging。笔记已经落盘时，
按原 ID 和完整内容比对，仅允许首次写入产生的 updated 时间差异；相同内容不重写，冲突
明确停止，不覆盖。材料先在克隆 ledger 上完整校验，再发布投影。缺失引用、损坏材料或
无法提交 continuation 会传播恢复故障，不能被 pipeline 吞掉后返回旧 best 结果。

同时修复了另一个实测差异：已结算 Kernel 重启后丢失原 done 参数。现在从原请求绑定的
回复恢复提议投影；不重新评估 completion gate，也不把收尾回复当成新的完成或工具执行。
进程内使用的 provider 投影缓存可完全重建，不是新的权威状态。

本次新增／复验结果（各组有交叠）：

- research、协议、迭代与 operation 状态组：113 passed，12 subtests。
- SQLite 重启、控制、原回复、请求接纳、预算、边界与无 fallback 组：115 passed。
- reducer、delivery、审批 HTTP 重启、完成、旧轨迹与 fencing 组：104 passed，26 subtests。
- 冻结快照、协议修复、恢复错误与新 continuation 契约组：47 passed。
- 新增五个 research 子进程退出位置：材料提交后、修复回复保存后、工具回执后、笔记落盘后、
  第二轮 followup 回复后。均双次恢复，模型请求、搜索、来源打开和笔记写入次数保持不变。
- 另测 continuation 提交成功但回执丢失、重复接纳无重复状态转移、损坏材料不发布部分事实、
  笔记冲突阻断、版本严格解码及 schema 5→6 幂等迁移。
- Ruff、mypy（539 文件）、Pyrefly（0 errors，35 suppressed，77 warnings）、ty 均通过。

最后一次 Gemma4 12B 原生 smoke 再次 4/4 通过，证据位于
`artifacts/operation-native-20261029-research-final`。恢复使用不同 PID，edit_executions=1，
原结果和 call id 保留；审批续跑 shell_results=1。这仍是选定 smoke，不是完整发布矩阵。
另一次真实 research smoke 通过，证据位于 `artifacts/operation-research-20261029-final`：
同一个 run/session 完成 web_search→open_url→知识证据写入→done，项目文件未改变。
主研究与 followup 共 12 次真实请求，无 HTTP 重试；这项验证协议执行，不评定答案质量。

按本次约定，六阶段工程迁移以定向／重点 pytest 和四项静态检查验收，全量 pytest 留给
GitHub CI。此前文本协议失败样本仍保留，归因由下节实测更正。pi/DeepSeek 的等价故障
比较尚未进行，不将缺少数据写成比较结论。

## JSON 恢复根因复核（2026-10-29）

失败包含三个连续暴露的缺口，不能只归因于模型不遵约。

1. **测试连接遗漏正式本地解码器。** 2026-10-08 的 `7c751f26` 将 Gemma 解码改为
   `ApiProvider.text_decoder` 显式配置，正式 `local_connection` 同时接入，但
   `RecordingProvider` 漏接。旧失败回包是完整的
   `<|tool_call>call:edit{args:{content:"VALUE = 42\n",path:"checkpoint.py"}}<tool_call|>`。
   原有 `normalize_local_reply` 可以原样解出正确内容和稳定 call id；测试连接却将其
   直接送入 JSON 边界。已补上同一个现有解码器，未新增宽松解析或第二套执行路径。
2. **冷启动窗口与原请求不一致。** `463ff32b` 迁移后的 driver 严格校验原回复窗口，
   但入口仍给冷 provider 打开新窗口。文本结果交付读取原快照时因此失败。现在唯一
   driver 根据 operation 的请求／回复引用装载原请求窗口，校验 session 和完整 provider
   binding 后，仅重建 provider 的内存投影。已接受的 live history 不能换绑；原
   `load_response` 的授权和窗口相等检查继续保留。没有新增持久化字段或恢复账本。
3. **文本后续请求依赖丢失的首轮契约。** 首轮执行 prompt 带工具签名、JSON 示例和规则，
   durable 对话的用户消息保存原用户任务。冷启动恢复该语义投影后，后续请求只有任务、
   结果和工作观察，没有完整工具契约。模型连续将 `run` 放入 Markdown fence；这些示例
   按既有规则不会执行，完成检查一直缺少验证，最终耗尽预算。现在文本后续请求在
   `freeze` 时都从本次冻结快照渲染契约，正常执行和恢复共用同一路径，替换原先
   “仅工具列表变化时补契约”的分支。完成拒绝提示补充示例不执行的说明；JSON 解码、
   fence 不执行及 completion gate 的判定没有放宽。首轮 prompt 的既有 hash 保持不变。

真实请求 A/B 位于 `artifacts/operation-json-contract-ab-20261029`：回放同一条失败的
恢复请求，保持模型、历史消息、时间上下文、temperature=0 和 max_tokens=8192；仅给
该请求补上原冻结快照的工具契约及 JSON 输出规则。原请求输出 fenced `run`，补契约后
输出裸 JSON `run`。
该实验只请求模型，不执行工具。完整端到端成功样本位于
`artifacts/operation-json-frozen-contract-20261029`：exit 75 后由另一 PID 恢复，原窗口、
call id、工具结果均保留，edit_executions=1，实际执行 unittest 后 done，3 次请求、无重试。

恢复检查器过去将 `delivery_mode` 固定写成 `same_window_native_resume`，却只核验 call id
和结果，没有比较窗口身份。现按实际协议标注，并将 `protocol_window_preserved` 纳入通过
条件；旧成功样本不能作为窗口身份未变化的证明。

历史可确认的较早文本任务通过记录是 2026-09-30 的
`tests/manual/KERNEL_UNIFICATION_AB_REPORT.zh-CN.md` 中 r3/r4；报告明确未注入重启。
已检查的本地记录没有找到更早的等价 JSON 进程恢复通过证据，因此不推断该用例最后一次
通过的具体日期。旧本地 provider 默认带解码器，也解释了为什么以前能执行这些文本帧。

最终验证：

- 定向 provider／prompt／窗口／重启组：199 passed。
- 定向控制／审批／完成／恢复边界组：106 passed，6 subtests。
- Ruff 通过；mypy 539 文件通过；Pyrefly 0 errors（35 suppressed，77 warnings）；ty 通过。
- Gemma4 12B / KoboldCpp JSON smoke 3/3：chat、read、recovery，证据位于
  `artifacts/operation-json-root-cause-final-20261029`。
- 同模型原生 smoke 2/2：recovery、shell_approve，证据位于
  `artifacts/operation-native-root-cause-final-20261029`。
- 两种协议的真实恢复均保留原窗口、原结果及原 call id，edit_executions=1，三次请求且
  无 HTTP 重试；原生批准续跑 shell_results=1。全量 pytest 按约定留给 GitHub CI。

文本后续请求现在每次携带冻结声明，单次请求增加了相应契约 token；这是消除对进程内
首轮历史依赖的明确成本。以上是选定 smoke，不是完整发布矩阵或性能基准。

## NOT_SENT 恢复缺口复核（2026-10-29）

本次修复评审指出的两个恢复问题：

- 工具回执已经保存，但结果交付明确未发送时，provider 失败结算曾将 operation 退回
  `writer_running`。现在同一事务将其恢复为原批次的 `tool_delivery_pending` 和原逻辑轮次，
  保留进度与 provider 调用计数。再次接纳发送时，从该批次的唯一有效 attempt 读取旧 effect，
  严格验证其属于原批次且具有 error / NOT_SENT 回执，再原子提交已有 `send_superseded`、
  新发送 intent 与 operation。原工具 effect、完整结果、call id 和 batch 身份不变。
- 普通协议修复和 done 拒绝请求曾因不是终止 acknowledge 而漏存 `work_ref`。现在只要存在
  控制 continuation，就冻结其关联和原回复引用；连续两次 NOT_SENT 后仍能使用同一个决定。
  原回复关联沿请求引用恢复，不重做已提交的完成评估，也不生成新的原始回复副本。

工具交付和控制恢复共用从 effect / delivery 事实解析未发送 continuation 的只读函数，
并从原冻结请求重建 provider 的内存窗口。未知发送先由 reducer 阻断，不进入窗口恢复或
重发。旧 checkpoint 若已形成“writer/repair 可开始生成，但原批次仍未交付”的矛盾状态，
reducer 明确报告 invariant 错误；不猜测旧执行位置，也不自动重新生成或执行工具。
此次没有新增持久化字段、运行驱动、执行器、事实账本或 fallback。

新增 28 项回归（其中一个测试含两个状态子用例），覆盖同 sink、新 sink、SQLite 重开、
原生绑定窗口、文本协议、成功与失败工具结果、连续两次未发送、协议修复、done 拒绝，
以及 MAYBE_SENT 不重发、替换尝试提交后中断、错误回执／跨批次关联拒绝和矛盾执行位置。

最终定向回测：289 passed、40 subtests passed，3 项既有历史 fixture 跳过；未运行全量 pytest。
Ruff、mypy（539 文件）、Pyrefly（0 errors，35 suppressed，77 warnings）、ty 和差异检查通过。
Gemma4 12B / KoboldCpp 普通进程恢复 smoke：原生、JSON 各 1/1 通过，均保留原窗口、原结果
和 call id，edit_executions=1，三次真实请求、无 HTTP 重试。证据分别位于
`artifacts/operation-not-sent-native-20261029` 和 `artifacts/operation-not-sent-json-20261029`。
这两次实机 smoke 验证原恢复链路未回退；NOT_SENT 故障由上述确定性测试注入。
