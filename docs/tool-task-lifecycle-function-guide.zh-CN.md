# 持续工具执行：函数级改造指南

这是一份基于当前 Codey 源码的实施设计，不是已经完成的功能。标记为“新增”的名称都是建议名称；示意代码表达边界和调用顺序，不能整段复制后直接运行。

目标是让模型使用原工具启动持续工作，通过公共工具观察、等待、输入和停止。复用现有 durable 任务、效果记录、调度器、权限及结果投影，不引入独立 jobs 运行时。

可以借鉴 Codex 的持续执行体验、DeepSeek Harness 的原工具启动与公共控制入口、Pi Durable 的归属及提交原则。最终行为仍必须在 Codey 内实现并验证，不能把参考项目的能力当成已经继承。

## 1. 先固定事实归属和身份

| 事实 | 唯一权威 |
| --- | --- |
| 子任务归属、调度、取消及任务终态 | `TaskCheckpoint` |
| 一次请求被接纳 | 原工具调用的 admission 收据，仍存入现有 `EffectRecord` |
| 实际工具执行的结果 | 工具子任务拥有的 execution 效果收据 |
| 日志正文 | `ExecutionOutputStore`，正式收据仅引用 |
| 进程、PTY、页面活句柄 | 对应执行服务的内存资源 |
| 研究来源、验证和完成判断 | 现有事实投影与 Completion |

必须区分两个事件：请求被接纳、实际执行结束。它们可以分别有收据，但只有 execution 收据包含这次实际操作的正式结果。不能把同一个效果先结算为“启动成功”，随后又结算一次为“操作成功”。

示例身份关系：

```text
用户任务 R（原 submission S）
  工具调用效果 A：接纳后台测试，结果引用 T
  子任务 T（owner_task_id=R，仍属于 S）
    执行效果 E：唯一测试结果
```

`T` 从原效果 A 的稳定身份导出；重复交付 A 返回同一个 T。输入内容有变化而身份没变，必须拒绝，不能当成重试。

CodeMode 内发起后台操作时，A 可以是 CodeMode 的嵌套调用效果，但 T 由原 durable 任务拥有。不要把 E 伪装成已结束 CodeMode 效果的嵌套执行：当前 `build_tool_batch_rows()` 要求这种嵌套的父效果仍为 pending。

## 2. 先让工具子任务不影响用户提交

入口：[durable/model.py](../codey/durable/model.py)、[durable/task_operations.py](../codey/durable/task_operations.py)。

**修改 `TaskCheckpoint`，增加派生属性 `execution_kind`。**

```python
# 新增到现有 TaskCheckpoint；旧任务默认保持 model 语义。
@property
def execution_kind(self) -> str:
    return str(self.state.get("execution_kind", "model"))
```

在现有状态校验中限制取值为 `model`、`tool`。工具子任务输入用一个版本化 JSON 合同定义一次，包含原工具调用、冻结授权引用、原效果身份及必要执行上下文。原参数只在这个输入里保存一份，其他位置引用它。

将 `execution_kind` 和工具子任务的不可变输入加入现有不可变校验。注意 `valid_frozen_task_state()` 当前允许补充原先不存在的键：不能让旧 model 任务通过补一个键变成 tool。旧数据缺省值也必须参加比较。

**修改以下现有生命周期函数：**

| 函数 | 修改要求 |
| --- | --- |
| `advance_task()` | tool 任务不得写 assistant 回答、不得改变所属 submission 的状态 |
| `start_task()` | tool 任务只进入 running，不把共享 submission 再次标成 RUNNING |
| `finish_task()` / `complete_task()` | tool 任务以效果引用结束，不生成用户答复；可增加无 answer 的 tool 完成分支 |
| `_cancel_terminal()` | 取消 tool 子任务时，只结束该子任务；不取消共享 submission |
| `fail_task()` | tool 执行失败只结束子任务，不把用户提交改成 UNANSWERED |
| `reconcile_waiting_tasks()` | failFast 原因优先从失败子任务/其效果读取；不能取共享 submission 的原因冒充子任务原因 |
| `reconcile_finalization()` | 保留“有活子任务就不能结束父任务”的规则，兼容 tool 完成分支 |

不要用 `owner_task_id is not None` 判断上述区别：已有的子任务可能仍负责自己的用户输入。判断依据是新加入的 `execution_kind`。

可以新增一个内部 helper 统一生成 submission 更新，避免 start、fail、cancel 各自重复判断。但 helper 必须保留 `SubmissionRecord` 的既有约束。

工具子任务复用父任务的 `submission_id`，不新建假用户消息、不调用 `admit_input(text="后台任务")`。当前存储允许任务引用已提交的同会话 submission；生命周期函数需要上面这些配套修改。

还要检查 model 父任务失败时的路径：当前 `fail_task()` 不会像完成路径那样自动等待活后代。引入持续工具后，父任务失败也必须先请求取消并排空其 tool 后代，保留原失败原因后再完成终结；可由现有 finalization 机制增加失败待清理分支。不能让异常路径遗留进程。

**先验收：**停止或失败一个 tool 子任务，父任务仍可继续答复；父任务取消则能传到全部 tool 后代。

## 3. 在原 ToolSpec 上声明持续执行能力

入口：[toolchain/tool_spec.py](../codey/toolchain/tool_spec.py)、[toolchain/definition.py](../codey/toolchain/definition.py)。

**修改 `ToolSpec`，增加 `supports_background: bool = False`。**

编码工具的声明仍从 `ToolDefinition` 汇入 `_coding_specs()`；浏览器从 `_browser_specs()` 汇入；研究能力沿原合同汇入 `_research_specs()`。不要另外建立 `BACKGROUND_TOOLS` 名单。声明在各工具原定义中存在一次，`ToolSpec` 是它们的统一投影。

先启用少数有完整实现的工具，例如 `run` 和经过拆分的 `browser_wait`。Shell 还必须接通原审批路径后才能启用。不要直接给所有工具打开开关。

建议统一生成可选参数 `run_in_background`：

| 函数 | 修改要求 |
| --- | --- |
| `_all_specs()` | 在统一汇聚阶段为支持的工具生成参数，拒绝与原参数重名 |
| `freeze_spec_parameters()` | 连同执行能力一起冻结 |
| `_schema_for_spec()` / `native_tools_for_snapshot()` | 从同一个有效 spec 生成原生合同 |
| `validate_args_with_spec()` | 验证当前冻结 spec 的参数；不支持的工具不能接受后台选项 |
| `kernel_protocol._validate_tool_args()` | 业务专用校验前分离已经验证的执行选项，避免现有 coding/research 校验把它判为多余字段 |

新增一个共享的 `split_execution_options()` helper，返回“原业务参数”和“执行选项”。业务执行器接收前者，身份摘要应覆盖完整的原请求，两者不能混用。

快照持久化也要改：[operations/provider_receipts.py](../codey/operations/provider_receipts.py) 的 `snapshot_description()`、`_restore_spec()`、`restore_snapshot()`。升级快照合同版本，旧快照缺省为不支持后台，继续校验实现兼容性。不序列化 Python 闭包，不在恢复时偷偷查实时注册表替换原绑定。

**验收：**原生协议、文本协议和 CodeMode 宣布的参数一致；注册表中途变化不能改变本轮授权和执行器。

## 4. 把“已接纳”加入现有结果合同

入口：[runtime/core/models.py](../codey/runtime/core/models.py)、[operations/kernel_result.py](../codey/operations/kernel_result.py)、[operations/kernel_receipts.py](../codey/operations/kernel_receipts.py)。

**建议扩展现有 `ToolResult`，而不是增加平行的 `BackgroundToolResult`。**例如新增默认值为 `completed` 的 `kind` 字段，允许 `accepted`。`accepted` 只能由内核在持久接纳完成后生成，普通执行器不能自行声明。

示意的模型返回值：

```json
{"kind":"accepted","task_ref":"T"}
```

`ok=true` 只表示本次接纳调用成功。它不是测试通过、页面已检查或研究来源已接纳。

需要一起修改的函数：

| 函数 | 修改要求 |
| --- | --- |
| `kernel_result.result_ok()` | 区分接纳成功与实际工具成功；completed 的 run 仍要求可靠退出码 0 |
| `_consistent_tool_result()`、归一化函数 | 拒绝普通 executor 伪造 accepted；验证任务引用与原调用的 durable 关系 |
| `result_receipt_fields()` | 将 kind 写入现有正式收据，升级收据版本 |
| `restore_result_receipt()`及恢复构造函数 | 还原 kind，旧收据默认 completed |
| `kernel_facts.project_result_observation()` | accepted 不建立业务完成事实 |
| `kernel_facts.project_execution_record()` | accepted 只能记录接纳 disposition，不能让 required-tool/completion 把它视为执行成功 |

建议在 [toolchain/result_schema.py](../codey/toolchain/result_schema.py) 增加共享 helper，从原最终结果 schema 生成模型侧的“接纳或完成”返回 schema。原最终结果 schema 仍只定义一次。

CodeMode 的 `successful_tool_value()` 当前会按最终 schema 直接读取字符串或 `canonical.value`，必须修改。`catalog.build_catalog_from_specs()`、`build_codemode_catalog()` 和声明生成也要使用同一套派生返回合同；不能只改 TypeScript 文本而不改宿主解析。

catalog 的合同摘要也必须覆盖新增能力和实际模型返回合同，保证重放不会把新旧接口当作相同。`ToolResult` 的复制、收据恢复和 CodeMode 子结果恢复都要保留 kind。

对于支持后台的工具，可统一向模型返回带 kind 的 envelope；是否包裹 inline 的返回值，需要在这次合同版本中固定。不要用“没有 exit_code，所以大概还在运行”判断。

**验收：**后台测试刚接纳时没有验证通过事实；accepted 收据往返存储后仍是 accepted；每个原生 call_id 只有一个配对结果。

## 5. 原调用接纳和子任务创建必须一次提交

入口：[operations/task_effects.py](../codey/operations/task_effects.py)、[runtime/write/mutation_line.py](../codey/runtime/write/mutation_line.py)、[durable/checkpoint.py](../codey/durable/checkpoint.py)。

**建议新增两个接线函数：**

```python
# KernelEffectSink：薄适配，使用当前冻结请求上下文。
def admit_tool_task(self, identity, call, *, snapshot): ...

# RuntimeMutationLine：唯一持久接纳入口。
def admit_tool_task(self, session_id, run_id, *, origin_effect_id, tool_input): ...
```

它们是同一条调用链上的适配和写入入口，不是两份接纳合同。唯一版本化输入类型供两层共用。

具体顺序：

1. 确认原效果已经通过 `KernelEffectSink.begin_turn()` 接纳。
2. 由原效果身份导出子任务 ID；重试时校验已有任务输入是否完全相同。
3. 生成 tool 子任务，复用父 submission，绑定 owner、授权及原效果引用。
4. 构造原效果的 accepted 收据，并推进父任务原有的工具交付状态。
5. 在**同一个** `DurableMutation` 中更新父 checkpoint、结算原效果、创建 `task_updates` 中的子任务。
6. 提交后才能发布唤醒，实际执行在调度后发生。

底层复用 `DurableSession.transact_task()`。将 `mutate_checkpoint()` 中已经存在的“派生 checkpoint/effects mutation”部分提取为纯 builder，供普通结算和这次接纳共用；接纳只在这个 mutation 上增加合法子任务。

不能先 `settle()` 一次，再单独 commit 创建 child。否则崩溃会留下“模型拿到引用，但任务不存在”的窗口。

当前 mutation 约束只允许一次 mutation 的 effects 归属一个任务。因此接纳事务先写父效果和 child checkpoint；child 的执行效果待 child 获得 running 身份后，经现有 `record_effect_intent()` 写入。不要顺手把父、子效果都塞进 effects 数组。

存储后端继续共用现有校验。检查 `MemoryDurableStore._validate_task()` 与 SQLite `_validate_task()`：对 tool 子任务增加共享的校验 helper，核对 owner、submission、conversation、来源效果和不可变输入。不要分别抄写同一套规则。

**验收：**在提交前失败没有外部操作；提交后、唤醒前崩溃仍能找到原 child；同一 A 接纳多次只有一个 T。

## 6. 修改 execute_turn 的接线，抽出共同叶子执行路径

入口：[operations/kernel_execution.py](../codey/operations/kernel_execution.py)。

**修改 `_execute_slots()`。**保留现有重复交付、恢复、冻结授权和取消检查；根据已经校验的执行选项选择 inline 执行或持久接纳。

```text
恢复已有收据 → 校验调用与权限 → 检查取消
  ├─ inline：进入共同叶子执行路径 → 正式结算
  └─ background：原子接纳 child 和 accepted 回执 → 返回引用
```

新增的后台分支不能直接 `threading.Thread(target=delegate.execute)`，也不能绕过 shell 审批。

**从现有路径提取共同函数，例如 `execute_tool_operation()`。**由 inline 和 tool child 共用：

- 原 delegate/显式 executor 的选择与策略检查；
- Shell 授权、路径及资源检查；
- `_run_via_delegate_or_fn()` 的实际分派；
- 结果归一化、可信 workspace 身份以及编辑结算规则。

不要让子任务重新跑完整 `execute_turn()`：它会再建模型交付 batch，并可能递归创建后台 child。后台子任务执行的是剥离调度选项后的原业务调用。

`execute_turn()` 内嵌的 `settle()` 可拆出共享的结果准备/投影 helper，但子任务的唯一正式收据归 child effect，不能再给原 A 写一次最终结果。

**修改 ToolDispatch 的锁范围。**当前 `_execute_slots()` 用 `ToolDispatch.admit()` 让非并行调用独占整个 session。把这个锁原封不动交给后台命令，会挡住查询、停止及其他工具。

在 [runtime/core/tool_dispatch.py](../codey/runtime/core/tool_dispatch.py) 扩展现有 admission 为按资源协调，资源身份由已有 executor、effect、项目和页面绑定派生，不另维护工具权限表。至少区分项目 workspace、浏览器资源、进程输入。相同资源冲突仍排队；不相关资源可推进。

公共观察、等待和取消不应申请被观察工作的独占执行锁。输入使用目标进程自己的串行输入锁。不能靠删掉全部锁解决卡住。

`supports_background` 不代表 `parallel_safe`。无法证明不会修改项目的 Shell 命令，不能自动与项目编辑并行；测试结果还需绑定实际执行时的代码版本。

**验收：**后台工作运行时能查询和停止；允许的浏览器工作能推进；同页冲突操作和项目冲突写入仍受约束。

## 7. 调度器复用，应用分派识别 tool child

入口：[app/task_submit.py](../codey/app/task_submit.py)、[app/durable_services.py](../codey/app/durable_services.py)。

**修改 `_dispatch_durable_task()`。**保留控制撤销检查，随后在 `active_run_for()`、`reserve_run()` 和用户文本校验之前识别工具任务：

```python
# 示意：保留原 control_service.permits_dispatch() 检查。
if checkpoint.execution_kind == "tool":
    return dispatch_tool_task(checkpoint, state=state)
# 后面继续现有模型任务分派。
```

工具子任务不能占用户模型任务的第二个会话运行槽。不要重新调用 `run_task()`，也不要伪造 `TaskSubmission` 让模型再推理一次。

**新增 `dispatch_tool_task()` 和 `run_tool_task()`，作为现有执行体系的分支。**

- dispatch 只安排有界执行资源，快速返回 accepted；不能阻塞 scheduler 线程。
- worker 中重新进入 `durable.execution(child_id, attempt_token)`；dispatch 线程的上下文不会自动安全继承到新线程。
- 从已接纳输入恢复 executor 和原授权；校验当前撤销、取消和资源前置条件。
- child 进入 running，写入唯一执行效果 intent，提交后执行共同叶子函数。
- 使用现有结果收据编码、`EffectOperations.settle_effect()` 及任务生命周期完成结算。
- worker 排队失败返回 false，让原 scheduler 重试；不能丢掉已接纳工作。

worker/资源句柄允许留在内存，索引键至少含 child ID 和 attempt token；不能在那里另存一套业务 status、result 和 ownership。限制并发和排队量，不为每次查看或轮询创建新线程。

**修改 `DurableAppServices.open()` 的 commit observer。**它目前按 `input_identity`、`resume_input`、`workflow_origin`、`input_state` 等输入字段唤醒。应遍历 mutation 中的 `task` 和 `task_updates`，识别新 tool child；终态仍唤醒现有依赖与父任务。不能为了唤醒而给 tool child 伪造 `input_state`。

父任务的控制代次、委托归属和撤销条件需要随 child 原授权绑定，防止 child 因缺少 `controller_actor_id` 逃过 `permits_dispatch()`。

**验收：**父任务已经占着会话运行槽时，child 仍可启动；调度器重试不会产生第二个 worker；旧 attempt 的迟到结果不能覆盖新状态。

## 8. 公共工具只读/控制现有任务

在现有 ToolSpec 汇聚中新增一组公共工具。建议先用三个入口，名字可以再调整：

| 工具 | 参数语义 |
| --- | --- |
| `task_output` | task_ref、wait_ms、cursor、输出上限 |
| `task_input` | task_ref、输入内容、是否结束输入 |
| `task_cancel` | task_ref、可选等待清理时间 |

先不增加模型侧 list：引用丢失恢复确有需要时，再从原任务索引生成列表。工具定义只在原注册体系声明一次。

建议在 operations 层新增控制适配，提供 `observe_tool_task()`、`wait_tool_task()`、`send_tool_task_input()`、`cancel_tool_task()`。它们不拥有任务，不建立 registry。

所有操作先校验引用、当前会话、任务所有权和调用者授权；“知道 ID”不等于有权操作，处在同一 conversation 也不自动获得跨分支权限。

`observe_tool_task()` 复用 `TaskQueryFacade.task_facts()` 取得一致的 checkpoint/effects，再读对应输出。实际结果从 execution 收据恢复；不要每次查看都复制到父效果里。

`wait_tool_task()` 使用现有 commit 订阅和有界等待：先订阅再复查，避免刚读到 running 就错过终态。等待不能持有 durable 写锁、项目锁或 BrowserWorker。超时/取消的是本次等待，不是目标任务。

当前 `TaskLifecycleOperations.wait_task()` 是“把 durable 任务挂起并登记依赖”，不能直接当成“阻塞等待 10 秒”的函数调用。短观察等待与真正释放父执行位置的 durable join 要区分。

`cancel_tool_task()` 调用原 scheduler 的取消入口，先持久写取消意图，再触达物理资源；返回时尚未清理完，就表达“正在停止”。`abort_task()` 的 handler 本身也必须有界，不能在这里无限等进程。

输入同样是一次有身份的效果，防止重复交付重复写入。发送完但来不及落收据时是结果不确定，不能自动再发送一次。对 Python、PowerShell 等输入可能执行新代码，命令级审批不能自然扩大成任意终端输入权限。

**验收：**跨任务越权被拒绝；读日志不重复执行；等待超时不杀任务；重复输入身份不会写两次。

## 9. 在现有输出库补游标，不新建日志权威

入口：[storage/execution_output_store.py](../codey/storage/execution_output_store.py)。

| 现有函数/新增函数 | 修改要求 |
| --- | --- |
| `append()` / `append_text()` | 保存有绝对位置的保留片段，统一递增游标；字符解码边界明确 |
| `replace_capture()` | 收敛到同一条输出写入路径，避免它与 append 各写一份正文 |
| `snapshot()` | 由相同正文生成头尾展示，不作为任务终态的另一权威 |
| 新增 `read_since()` | 返回新增片段、next_cursor、当前保留范围和丢失范围 |
| `ExecutionOutputObserver.offer()` / `_drain()` | 保持一个采集所有者；合并通知可以，不能合并后丢失输出位置 |
| `settled()` / `unavailable()` | 区分采集完成/不可读取与业务任务结果；任务成功只由正式收据判断 |

游标按 stdout/stderr 分别携带。模型、UI、CLI 各自传自己的 cursor，不保存一个全局“读到哪里”。游标还应绑定 execution 身份，不能拿进程甲的 cursor 读取进程乙。

当前保存的是有界头尾；被裁掉的中间日志必须返回 gap，不能声称读到了完整增量。先扩展现有存储表示以保留明确片段位置；不要求为此建立一个独立日志服务。

`ExecutionOutputReference` 带 revision 和 hash，表示某次固定观察。不要把旧引用解释成“总是最新”。`task_output(task_ref, cursor)` 走最新观察；正式历史收据仍保留固定引用。若要保证旧正文可重读，必须保留相应不可变内容，不能只留下 hash 后宣称内容可恢复。

迁移旧输出时只保留实际已有的片段和缺失范围。不要一边继续写旧快照正文，一边再把同一正文写到新 chunks 表。

**验收：**两位读者互不消耗输出；UTF-8 分块不损坏；裁剪后报告 gap；旧收据不会悄悄变成新日志。

## 10. Shell 先复用现有进程生命周期，再接输入和 PTY

入口：[runtime/core/cancellation.py](../codey/runtime/core/cancellation.py)、[toolchain/runtime.py](../codey/toolchain/runtime.py)、[app/shell_service.py](../codey/app/shell_service.py)。

Codey 已有 `start_process()`、`wait_process()`、`run_process()`、Windows Job Object 和进程树清理。先复用它们，第一批后台长命令不必为了“后台”重写进程引擎。

| 函数 | 修改要求 |
| --- | --- |
| `run_command_raw()` | 接受由正式执行身份绑定的 execution_id，避免重试临时生成第二个输出身份；复用原策略和 command canonicalization |
| `AgentToolFns.execute_run_command()` | 传递执行上下文，不旁路原工具函数 |
| `start_process()` | 第二批增加可选 stdin pipe；保留最终取消检查、Windows 挂 Job 后恢复进程的顺序 |
| `wait_process()` | 仍只有一个 worker 调用、采集和清理；不能每次 task_output 再调用一次 |
| `terminate_process_tree()` | 继续作为已有进程树停止实现，保持幂等和正确句柄归属 |
| `execute_shell_ticket()`及其 spawn gate | 后台 Shell 仍消费原审批票据，不能另外 Popen 绕过审批 |
| `abort_task_resources()` | 按 child 身份通知其活资源；原 provider/审批取消逻辑保留 |

新增最小的活资源适配，例如按 `(task_id, attempt_token)` 查找 process/stdin/stop signal/输入锁。它只持有操作系统资源，不决定任务事实。正常退出、取消和异常都在一个 finally 中解除绑定。

`task_input` 写入目标 stdin 后返回“输入已发送”，不是“该命令已成功”。支持半关闭 stdin；Ctrl+C 与关闭整个进程分别处理。

PTY 是同一 Shell 生命周期下的另一种 I/O 后端。Windows 需要实际验证 ConPTY；POSIX 使用 PTY。不要拿 PIPE 冒充 PTY，也不要把已有 `subprocess.Popen` 专用清理函数无条件用于不同原生句柄。平台适配共用上层权限、任务、效果和清理合同。

PTY 经常合并输出流，必须在输出合同中说明，不能伪造独立 stderr。第一版可让启动的交互程序本身作为长生命周期 tool 任务，模型反复输入；以后若增加“一个终端里多条命令”的语义，必须分清终端仍存活和命令已经结束。

**验收：**启动后持续输出、连续输入、EOF、中断、子进程清理；Windows 上验证孙进程和取消/启动竞争。

## 11. 浏览器等待要释放浏览器线程

入口：[automation/browser_session.py](../codey/automation/browser_session.py)、[automation/browser_worker.py](../codey/automation/browser_worker.py)。

当前 `BrowserSession._wait_on_browser_thread()` 在同一个浏览器线程循环检查并 sleep。仅把外层调用放后台，模型虽能拿到引用，后续点击仍会排在整个等待后面。

**提取一次检查函数，例如 `_check_wait_on_browser_thread()`。**只做本次条件检查和页面身份确认，不长时间循环。让原 `wait()` 复用该检查；background child 在浏览器线程之外控制截止时间和下一次检查，每次经原 BrowserWorker 回到所属线程检查。

等待需要的页面/文档身份继续使用原资源绑定；导航后按原等待合同决定继续或失效，不能无声换页。瞬时检查可以与允许的浏览器操作交错；动作冲突仍需协调。

不能从新线程直接调用 Playwright 对象。不能在 BrowserWorker 内部等待一个还需要同一个 BrowserWorker 执行的 child。

下载不是简单轮询就全部解决；若要启用后台下载，还必须让事件注册、文件完成和清理留在正确线程并有正式收据。在此之前，不宣布它支持 background。

研究任务继续复用 `ExecutionDelegate._observation_tools()` 的临时账本原则及原来源接纳；不得让后台线程直接修改父任务活账本。新增哪些研究工具的后台能力，应逐项验证其资源及结果投影。

**验收：**等待页面条件时，允许的浏览器操作仍能执行；终态和取消最终只有一份结果；研究来源重复读取不重复入账。

## 12. 正式结果进入原事实恢复链，完成门要能看见活任务

入口：[operations/kernel_context.py](../codey/operations/kernel_context.py)、[operations/kernel_facts.py](../codey/operations/kernel_facts.py)、[operations/kernel_control.py](../codey/operations/kernel_control.py)。

**修改 `KernelContext.restore_facts()`。**它目前只遍历当前 run 的效果。扩展为同时读取明确属于当前任务、能由原 admission 追溯的 tool 子任务执行收据。复用 `restore_result_receipt()`，按正式 execution effect ID 去重，并使用相同的 `project_result_observation()` / `project_execution_record()`。

在阶段边界合并结果，不由 child 线程随时写父 `TaskSession`。沿现有 staged projection、projection lock、`restored_effect_ids` 和账本提交规则处理。查询结果本身不结算效果、不重复投影来源。

恢复顺序应可重复，使用既有提交顺序；还要保留发起时绑定的 workspace 身份、研究作用域和来源调用信息。不能把子任务的局部 turn/index 当成父轮次，套用现有研究身份过滤后丢掉结果。

**修改 `evaluate_completion()` 及原完成检查。**必要结果仍 pending 时，返回明确的等待/继续工作要求；开发服务器等长活资源先请求清理再结束父任务，避免父任务卡在 COMPLETING 而模型已经退出。

完成通知从既有 durable commit 生成引用，模型下一安全边界接收即可。通知不另存一份结果；丢了通知仍能从正式状态查询。第一版不需要为了推送通知恢复一个新的模型运行时。

**展示与控制查询接线：**`durable.task_projection.task_lifecycle()` 增加任务类别和 owner 引用；`app.task_lifecycle_projection.snapshot_tasks()` 从已有归属索引补入相关 tool 子任务。`app.control_queries.run_snapshot()` 当前按用户 run 组织回答、审批和验证，需要区分 tool 观察，不能给子任务显示父任务的回答。桌面和 CLI 都复用同一观察投影，不能各自推算终态。检查 `DurableTaskScheduler._place_ready_inputs()` 对活任务的统计，明确 tool child 不是另一条用户输入，同时保留父任务尚有活工作的占用规则。

**重启接线：**在既有 scheduler 恢复流程中区分“已接纳但尚无执行 intent”和“执行 intent 已存在”。前者在原授权仍有效时可以首次执行；后者如无法确认外部结果，复用 `interrupted_effect()` / `settle_unknown_effect()` 记录中断，不能自动再次执行。

只有内存句柄丢失不证明外部程序已退出。若无法确认清理，保留未知清理事实；PID 相同也不是足够的进程身份，不盲目按旧 PID 杀进程。

`recover_scheduler_state()`、`_reconcile_task()` 和 `kernel_cleanup_required()` 的判断需要检查 tool 任务：恢复后的 running 不能被当成可安全重跑；已请求取消也不能在资源仍存活时直接标成已清理。`abandon_on_restart` 若不保留所需诊断，不能直接替代这些恢复规则。

## 13. 按小步提交和验收

| 步骤 | 本步闭环 | 必测行为 |
| --- | --- | --- |
| 1 | tool 子任务的归属与生命周期 | 子任务失败/取消不改用户提交；父取消向下传播 |
| 2 | 单一 spec、accepted 收据与原子接纳 | 三协议一致；同一请求一个 child；提交前无副作用 |
| 3 | 原调度器 + 长命令 + 公共观察取消 | 父运行槽不挡 child；查询/停止不被独占锁挡住；有界排队 |
| 4 | 增量输出及恢复 | 独立 cursor、缺失范围、旧引用、崩溃各窗口不重跑 |
| 5 | stdin 和 PTY | 输入重复交付、EOF、Ctrl+C、Windows 进程树清理 |
| 6 | 浏览器和研究接入 | 浏览器线程不被长等待占死；来源投影一次；过期验证不能通过 |
| 7 | 完成通知和端到端 | 修改代码→启动网站→浏览器检查→测试→研究读取→清理→完成 |

可从现有测试扩展：`tests/durable/test_scheduler_ownership.py`、`test_scheduler_abort_handler.py`、`tests/test_kernel_execution_snapshot_guards.py`、`test_execution_output.py`、`test_cancellation.py`、`test_browser_worker.py`、`test_codemode_host.py`。新增针对原子接纳与跨任务结果恢复的场景测试，不用仅检查“方法有没有被调用”的镜像测试。

最终应能验证：一次外部工作只有一个执行效果与正式结果；接纳、观察、输入、取消分别保留自己的真实语义；换 UI/CLI/模型入口、重复查询或重启不会复制结果或偷偷重做外部操作。

本指南未修改运行代码，也未运行上述功能测试。
