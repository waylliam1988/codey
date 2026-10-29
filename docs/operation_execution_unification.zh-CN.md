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
分阶段运行相关测试，最终执行完整回归。六阶段完成还需要本地 Gemma 原生/文本冒烟、
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
