# 工具任务下一阶段：P1、终端输入、PTY 与浏览器后台的函数级修改指南

核对日期：2026-11-20。源码基线：`9cdada0d`。

本文是实施教程，尚未实现这里建议的生产功能。标为“新增”的函数名是建议；代码块是接口或控制流示意，不能整段复制后直接运行。上一份 `tool-task-lifecycle-function-guide.zh-CN.md` 描述的是早期方案；实际修改应以当前源码和本篇核对结果为准。

## 1. 先确定改动边界

推荐顺序：**P1 → 管道输入 → PTY → 浏览器后台等待**。每步各自提交、各自验收。

始终维持这些归属：

| 内容 | 唯一事实来源／所有者 |
| --- | --- |
| 子任务归属、attempt、运行和取消状态 | 现有 durable 任务及其事务 |
| 原工具调用、执行结果、输入是否交付 | 现有 effect intent 与 settlement |
| 验证先后顺序 | 现有事务历史中的 `commit_seq` |
| 输出正文和读取游标 | `ExecutionOutputStore` |
| 活进程、输入句柄、PTY、浏览器对象 | 应用持有的临时资源；浏览器对象只在所属线程使用 |
| 工具名、参数、可见性、权限声明 | 原 `ToolDefinition`／`ToolSpec` 生成链 |
| 验证是否有效、研究证据是否成立、能否完成 | 原验证、Research 与 Completion 模块 |

新增一个临时句柄容器不等于新增运行时。它只能帮助找到资源，不保存另一份持久任务状态，不自行执行重试、调度、结算或完成判断。UI、CLI、CodeMode 也不得再维护一套业务状态。

当前已有两个容易遗漏的实现细节：

- `run` 不带 `run_in_background` 时，生产 durable 路径已经会建立工具子任务，先等一段时间，必要时返回引用；显式 `false` 仍可走前台执行路径。
- 原 `run` 的回执可能只是引用子任务正式结果。`kernel_facts.project_result_observation()` 已跳过 `accepted` 和 `canonical.tool_task`；扩展后保留这一规则。

## 2. P1：先修旧成功覆盖新失败

### 2.1 当前问题确实存在

入口：[KernelContext.restore_facts](../codey/operations/kernel_context.py)。

现在的收集顺序相当于：

```python
effects = parent_effects
for child in owned_children:
    effects.extend(child_effects)
```

这表示“父任务在前，子任务在后”，不表示“先结算在前，后结算在后”。而 `TaskSession.record_verification()` 直接追加，`latest_verification_observations()` 和无编辑场景的 `session_checks_passed()` 都依赖顺序。

本轮用真实子进程在当前基线复现，全程未调用模型：

1. 测试读取项目目录外的标志文件；项目源码保持不变。
2. 后台执行 `python -m pytest -q`，通过。
3. 在项目外写入标志文件。
4. 同一命令显式 `run_in_background=false`，退出码为 1。
5. 新建 `TaskSession` 和 `KernelContext`，两次执行 `restore_facts()`。

输出：

```json
{"later_inline_ok":false,"later_exit_code":1,"restored_order":[[false,1,1],[true,0,1]],"restored_latest_passed":true}
```

重复恢复没有重复追加，但恢复出的顺序仍然错误。这里复现的是验证事实错误，不声称已经复现整个产品界面错误宣告完成。

### 2.2 第一步：增加一个有一致读取边界的历史查询

修改：

- [TaskQueryFacade](../codey/durable/task_queries.py)：新增 `owned_settled_effects(owner_task_id)`。
- [DurableSession](../codey/durable/session.py)：新增同名薄转发。
- 复用 [TaskTransition](../codey/durable/timeline.py)、`DurableStore.commit_sequence()`、`task_history(..., through=...)`，不加结果表。

这个查询一次进入 `_query_line`，完成以下全部工作：

```text
读取 cutoff = store.commit_sequence()
读取 owner 与其直属工具子任务
读取各任务 task_history(task_id, through=cutoff)
从历史重建 cutoff 时刻的任务和 effect
检查 owner、conversation、submission、origin effect、原参数摘要
提取每个 effect 第一次出现正式 settlement 的位置
按 settlement 位置合并返回
```

返回值可以是内部不可变读视图：`cutoff` 加 `(commit_seq, EffectRecord)` 元组；确有原始事务内顺序时再携带该位置。它不是新持久合同，也不能反向写入另一份结果。

关键要求：

1. 使用**同一个 cutoff**，不能分别查询父、子任务后用各自“最新”结果拼接。当前查询线与写入线共同串行化应用内读写；如果另一个进程也能写同一库，要同时确认这一部署边界，必要时在 SQLite 同一读事务中完成查询。
2. 子任务关系也按 cutoff 校验；当前读取到、但 cutoff 后才创建的子任务不能进入本次视图。
3. 取第一次从“未结算”进入“已结算”的 commit。后续 `complete_task()` 不能把旧结果改成一个更新的结果。
4. 同一 effect 后续再次出现相同结算不另造观察；出现矛盾结算应报恢复错误，不能挑一条有利结果。
5. 同一事务内若有多个结算，只能使用原始事务明确定义的顺序。注意 `task_history()` 已按 task 过滤 `effect_changes`，不能拿过滤后各自的数组下标充当跨任务顺序。当前单结果结算用 seq 即可；若出现同事务中无法证明先后的冲突验证，应报告歧义。若以后确需更细顺序，从原 workflow envelope 的完整 effects 读取，不另加一个伪造排序号。
6. 不用时间戳、turn、子任务创建顺序或 effect ID 排序。这里定义的是**正式结算顺序**，不是猜测外部进程实际在几时结束。

可在 `timeline.py` 增加纯函数 `first_settled_effects(transitions)`，只提取位置、去重、校验终态不可变。Memory 与 SQLite 都调用相同逻辑，存储不需要增加另一份结算序号字段。

该纯函数的核心可以写成下面这样，跨任务合并与同事务歧义检测由上层查询完成：

```python
def first_settled_effects(
    transitions: tuple[TaskTransition, ...],
) -> tuple[tuple[int, EffectRecord], ...]:
    first: dict[str, tuple[int, EffectRecord]] = {}
    for transition in transitions:
        for effect in transition.effect_changes:
            if effect.settlement is None:
                continue
            previous = first.get(effect.effect_id)
            if previous is None:
                first[effect.effect_id] = (transition.commit_seq, effect)
            elif previous[1].settlement != effect.settlement:
                raise ValueError("conflicting terminal effect settlement")
    return tuple(first.values())
```

补齐原身份、intent 不变性检查，并在边界转换成项目现有恢复错误。不要把这个函数中的 dict 插入顺序当作父子任务合并顺序；最后统一按正式 seq 合并。

### 2.3 第二步：验证投影改为可重建的有序视图

只把 `restore_facts()` 中的列表排序还不够：运行中的 session 可能已经看到新失败，之后才收到旧成功；`restored_effect_ids` 会让重排后的历史跳过已投影项，旧成功仍可能追加到末尾。

推荐在 [kernel_facts.py](../codey/operations/kernel_facts.py) 新增：

```python
def rebuild_verification_projection(
    session: TaskSession,
    ordered_receipts: Sequence[CommittedToolReceipt],
) -> list[VerificationRecord]:
    ...
```

`CommittedToolReceipt` 是本教程指代的内部读视图类型，包含 effect 身份、结算位置与恢复后的原 `ToolResult`，实际命名可随项目约定；不定义第二套工具结果格式。

最容易审查的实现方法：

1. 建立一个只用于推导的临时 `TaskSession`，保留原 policy、project、任务种类等必要上下文，编辑和验证容器从空开始。
2. 按正式历史顺序，只重放 `edit`、真正执行的 `run` 对验证所需的事实。
3. 复用 `record_facts_for_result()`、`_record_edit_fact()`、`_record_run_fact()`、`_record_run_verification()` 的原规则；不要调用整套 Research 投影。
4. 跳过 `accepted`、`canonical.tool_task` 引用，以及“执行前拒绝”的 run；已经尝试但结局未知的 run 必须保留为失败／不完整观察。
5. workspace 身份读取原回执，不把恢复时的当前工作区身份借给旧结果。编辑 epoch 也从该历史范围的原编辑事实推导，不用当前 session 最大 revision 给所有历史结果补值。
6. 返回临时 session 的验证列表。在真实 session 的 staged projection 上**整体替换这一份派生列表**，保留其他事实和原锁对象。

这不是把所有业务重新执行一遍，只是从已提交收据计算列表。不能调用工具执行器，不能重复登记研究来源、笔记或 investigations。

为保持一个生产写入口，给以下现有函数沿调用链增加一个明确的 `include_verification` 参数：

```text
project_result_observation()
  → record_facts_for_result()
    → _record_run_fact()
```

durable 路径的普通增量投影传 `False`，保留 run 的说明、执行记录等原行为；验证列表统一由 `rebuild_verification_projection()` 写入。没有 durable 的现有独立执行／单元测试路径仍可使用原顺序投影，二者共享相同解析规则。不要在生产里先追加一次，再依靠另一个函数“纠正”一次。

如果调用方有不属于当前 durable 任务历史的验证输入，应先明确原有授权事实范围，再纳入同一投影输入，不能静默删除它，也不能把另一任务的成功混入。

### 2.4 第三步：同时接热路径和恢复路径

修改 [kernel_execution.py](../codey/operations/kernel_execution.py) 中 `execute_turn()` 内的 `settle()`：

```text
校验结果，生成可丢弃的 staged 投影
调用原 intent_sink.settle()，正式提交
从共同的 owned_settled_effects() 读取有序正式结果
在 staged 上重建验证视图
提交原 session 投影
```

仍保持 `session.projection_lock` 的发布边界。查询 durable 后再恢复收据正文，不在 durable 写入锁里执行 I/O、浏览器操作或模型调用。检查现有锁顺序，禁止提交回调反向等待 session 锁；回调只通知。

修改 `KernelContext.restore_facts()`：

- 原 effect／Research 增量投影仍按身份去重。
- 工具结果采用共同历史查询的正式顺序。
- **即使所有 effect 都已在 `executed` 或 `restored_effect_ids` 中，也重新计算验证视图**；不能让这两个集合跳过验证重建。
- 中断的子任务仍通过原 `tool_execution_result()` 解释，不伪造退出码。
- 子任务正式结果和父任务的观察回执只记一次验证。

浏览器绑定的恢复顺序也可能依赖回执顺序；排序时保留原 provider proposal 恢复、研究 turn scope 和来源校验，不要用一段新循环覆盖掉这些规则。

### 2.5 第四步：外层完成证据也必须能撤回旧成功

修改 [project_completion_checks._evidence_with_session_facts](../codey/operations/project_completion_checks.py)。

目前它依次向 `ExecutionEvidence.observe_check()` 写入观察。若最新观察不完整，只跳过这个 key，并不会必然撤销 evidence 对象中已经存在的旧成功。

在 [ExecutionEvidence](../codey/runtime/observe/execution_evidence.py) 增加例如 `replace_check_projection(...)`：

- 输入为同一批已排序、已解析的验证观察和该范围的原拒绝／环境诊断。
- 在临时 check 容器中复用 `observe_check()` 的失败、容量和环境策略，然后一次替换对应派生 check 视图。
- 最新不完整观察要移除该 key 的旧成功，并返回原 gap 诊断；缺失退出码不能变成 0 或 1。
- 只替换这段权威历史负责的检查事实。保留 reads、searches、changed_files、edit epoch、当前 workspace 等其他 evidence。
- 原始命令证据与 session 投影不得轮流追加、相互覆盖；统一由这里导出的检查视图对外提供当前结论。拒绝和环境诊断也从原收据保留，不凭空消失。

`verification_observations()`、`latest_verification_observations()` 保持原域规则：输入顺序权威，最新不完整结果阻止旧成功复活。

[project_candidate_validation.py](../codey/operations/project_candidate_validation.py) 的 `validate_stopped_candidate()`、`submit_readonly_completion_candidate()` 消费修正后的证据即可；不要另写“成功优先”分支，不要靠加跑一次测试掩盖恢复错误。

### 2.6 P1 验收

建议扩展现有 `test_tool_task_background.py`，以及 latest-result、unknown-verification、completion evidence 相关测试。

| 用例 | 必须得到的结果 |
| --- | --- |
| 后台旧通过 → 前台新失败 | 重启后最新仍失败 |
| 前台旧失败 → 后台新通过 | 重启后最新通过，仍满足 workspace 适用性 |
| 子任务先创建但后结算 | 以结算位置决定先后 |
| 热 session 先投影新失败，随后才读旧通过 | 最新仍失败 |
| 旧通过 → 已执行但结果未知／输出不完整 | 不得复用旧成功 |
| 执行前拒绝 | 不伪装成一次真正执行后的失败 |
| 同源码指纹、同命令、同 cwd | 仍由正式结算顺序决定最新结果 |
| 不同 cwd 或代码变化 | 继续遵守原适用范围与新鲜度规则 |
| 重复恢复、重复 task_output、父观察回执 | 不增加验证条目 |
| evidence 已有旧成功，再出现不完整结果 | 旧成功被撤回；完成门仍阻止错误完成 |

## 3. 终端输入：先在现有管道进程上打通

### 3.1 先扩展单一工具定义

修改 [definition.py](../codey/toolchain/definition.py) 中 `run` 的 `ToolDefinition`：建议增加 `stdin: boolean`，默认关闭。

示意调用：

```json
{"tool":"run","args":{"command":"原来已获准的命令","stdin":true,"run_in_background":true}}
```

这里的 command 是语义占位，不是可以复制执行的示例。原参数必须继续经过原命令政策和审批；打开 stdin 不能把验证工具变成任意 shell。

同时检查 [ExecutionDelegate._execute_project](../codey/operations/task_execution.py)、`kernel_execution.execute_tool_operation()`、`kernel_tool_args._validate_tool_args()`、`AgentToolFns.run_command` 签名和 [run_command_raw](../codey/toolchain/runtime.py) 的传递链。当前它们主要传 command、path；只改底层 `Popen`，模型参数并不会自动传到那里。

不要在前端、文本协议、native schema、CodeMode 各写一次参数。继续由原定义生成；输入选项保留在原 args 摘要与 frozen snapshot 中，不放进一个新的“后台参数合同”。

### 3.2 修改底层三个函数

文件：[cancellation.py](../codey/runtime/core/cancellation.py)。

| 现有函数 | 具体修改 |
| --- | --- |
| `start_process()` | 增加受控 stdin 模式；交互时 `stdin=subprocess.PIPE`，非交互使用明确的兼容策略；保留进程组、Windows Job 与 suspended→attach→resume 顺序 |
| `run_process()` | 接收输入模式及内部“资源已启动”绑定回调；spawn 后注册资源，再进入原 `wait_process()`；失败路径确保回收 |
| `wait_process()` | 继续唯一拥有 wait、stdout/stderr 采集、observer、进程树清理；把 stdin 撤销、关闭和注销纳入统一 finally |

新增输入后，还需修改 `_close_pipes()` 和 `_close_owned_pipes()` 所在的失败／正常收尾路径：它们现在主要处理输出管道。不能在另一个线程正写入时盲目 `BufferedWriter.close()`，否则清理会卡在内部锁上。

建议由后端提供有界 `write_input()` 和 `close_input()`，处理部分写入、BrokenPipe、取消和关闭竞争。限单次 UTF-8 字节数、限耗时，写不完返回实际交付字节数及不完整状态。不能用一个无界的阻塞 `stdin.write()` 长时间持有全局锁。

### 3.3 增加临时活资源绑定，绑定 task 与 attempt

建议新增 `runtime/core/process_resources.py`；由现有 `TaskRuntimeCoordinator` 持有，沿 `TaskSubmissionStores` 注入执行与控制路径。

内部接口示意：

```python
class ProcessResources:
    def register(self, task_id, attempt_token, execution_id, endpoint): ...
    def acquire(self, task_id, attempt_token): ...  # 有生命周期保护的短租约
    def unregister(self, task_id, attempt_token, endpoint): ...
```

保存 endpoint 和输入串行锁，不另存 `running/succeeded/cancelled`。输入半关闭等仅为句柄操作必要的临时 I/O 信息，不作为正式任务状态。

`endpoint` 必须绑定三件事：`child.task_id`、`child.attempt_token`、`execution_effect_id(child.task_id)`。模型只提交 `task_ref`，不接收 PID 或原始句柄。

注册与停止使用已有的 spawn／取消屏障和明确锁顺序。清理前先禁止取得新的输入租约，再终止／关闭资源；旧 attempt 的迟到注销不能删掉新 attempt 的绑定。

修改 [tool_tasks._run_admitted_operation](../codey/operations/tool_tasks.py) 的 run 闭包，把当前 child 身份、attempt 和注册回调传到 `run_command_raw()`；无需新增一个管理进程的调度线程池。

### 3.4 在现有控制工具集合增加 task_input

修改 [task_control_specs.py](../codey/toolchain/task_control_specs.py)：

```text
task_input(
    task_ref: string,
    data?: string,
    eof?: boolean = false,
)
```

要求 data 非空或 eof=true；明确 UTF-8 编码，不自动补换行。`eof=true` 是“先交付本次数据，再关闭管道写端”。进程不支持输入、已结束、取消中或资源尚未注册，要返回具体原因；尚未就绪不等于执行失败，也不能偷偷排队后无限期投递。

该 `ToolSpec` 设 `replay_class="unsafe"`。其输入交付成功只说明写入完成，不说明子进程的命令／测试已成功。

`grant="control"` 只能提供控制入口；执行时仍需检查原目标工具权限、当前 controller、原审批允许的交互范围。Python、PowerShell 等解释器收到 data 可能执行新代码，不能只凭“任务归我”授权。

### 3.5 接通执行与去重

修改 `kernel_execution._execute_slots()`：目前对 `task_output/task_cancel` 的显式分支要纳入 task_input，并将**当前调用的 effect identity**传给控制路径。可以按 frozen `spec.executor == "task_control"` 分派，但不能绕过原 schema、policy 和 controller 检查。

扩展 `tool_tasks.control_tool_task()`，新增内部 `input_tool_task()`：

```text
校验原控制调用参数与权限
owned_tool_task() 确认同 owner/conversation/submission
读取当前 durable attempt 与 cancel 状态
当前调用 intent 已正式记录，检查其 replay／settlement
取得同 attempt 的活资源短租约
在输入串行锁下再次检查 fence／取消状态
有界交付 data／EOF
返回包含交付字节数和交付状态的原 ToolResult
由现有 settle() 结算这次控制调用
```

输入控制的 effect 属于**调用者自己的这次工具调用**；不要塞进目标 child。当前 `tool_execution_result()` 要求目标子任务只有一个主执行 effect。额外往 child 追加 input effect 会破坏这一前提。

去重语义：

- 同 effect 已结算：返回原控制收据，不再次写入。
- 同 effect 已有 intent 但交付后尚未结算就崩溃：标记未知／中断，不自动重发。
- 同进程内并发收到同 effect：利用原 effect 执行排他机制，确保只有一次进入输入；若当前机制覆盖不到该控制分支，应在现有 effect 协调层补齐。
- 两个不同 effect 输入相同文字：是两次获准操作，不能按文字内容去重。

普通管道不能与数据库组成一个原子事务，所以承诺应是“重复交付不盲目重发；不确定就如实报告”，不能承诺跨崩溃外部输入 exactly-once。

### 3.6 特别避免项目锁死锁

当前 `_run_admitted_operation()` 用项目资源锁包住整个命令。若 task_input 也先拿同一把锁：

```text
命令持锁，等待 stdin
task_input 等锁，无法写 stdin
```

所以输入控制只走目标资源租约、输入串行锁与原权限／取消屏障，不参与这个长命令项目锁。`task_output/task_cancel` 也要保持能及时推进。

stdin 输入测试程序可以直接在底层进程测试里启动 Python fixture；生产路径若该程序需要 shell 审批，则通过原审批验收，不能为了测试放宽 `run` 白名单。

## 4. PTY：把终端作为同一执行器的另一种 I/O 后端

### 4.1 先收敛进程 I/O，再接平台实现

建议在 `runtime/core/process_io.py` 定义一个小型内部接口：

```python
class ProcessIO(Protocol):
    # 伪代码：精确类型按项目的捕获与错误类型补齐
    def write_input(self, data: bytes, *, deadline: float) -> InputDelivery: ...
    def close_input(self) -> None: ...
    def send_interrupt(self) -> None: ...
    def resize(self, cols: int, rows: int) -> None: ...
    def wait_exit(self, timeout: float) -> int: ...
    def terminate_tree(self) -> None: ...
    def close(self) -> None: ...
```

还需提供输出流／读取端与捕获元数据；上面只展示主要控制动作。它管理进程 I/O，不管理 TaskCheckpoint、effect 重试或模型调用。

新增 `PipeProcessIO` 包装现有 Popen／Job；新增 POSIX PTY 和 Windows ConPTY 后端。把现有 `wait_process()` 的平台资源操作提取到后端，保留同一个采集与收尾算法。

接口迁移时保持原 `run_process()` 的对外捕获语义；不要把非 Popen 的 native handle 假装成 Popen，继续交给 `_resume_windows_process()`、`_terminate_process_tree()` 或 `_WindowsJob` 的旧强类型路径。

### 4.2 工具参数仍从原定义产生

在同一个启动工具定义增加 `tty`、`cols`、`rows`。建议：

- `tty=false`：原 stdout/stderr 分离模式。
- `tty=true`：输入通道开启，支持终端尺寸与中断。
- cols／rows 必须是有界正整数；只有 tty 模式允许。
- 明确处理 `tty=true, stdin=false` 冲突，不静默猜测。
- 不支持的平台请求 tty 时明确报错，不退回普通管道并宣称 PTY 成功。

若通过已批准 `shell` 启动交互解释器，要同时把这组参数接入原 shell 定义和审批 payload。shell 与 run 的业务权限仍然不同，共用的是生命周期和 I/O 后端。

### 4.3 POSIX 后端要解决什么

创建 master/slave PTY；为子进程建立 session、控制终端及前台进程组；正确继承／关闭文件描述符。仅给 Popen 接上一个 slave fd 并不自动完成所有作业控制语义。

resize 更新终端窗口并通知对应终端／前台组。interrupt 面向终端前台作业；最终取消面向整个受管进程树。PTY 读到平台特定 EIO 时，结合子进程状态按已定义规则判断 EOF，不能吞掉任意读取错误。

### 4.4 Windows ConPTY 后端要解决什么

新增 `runtime/core/conpty.py`，具体函数建议：

| 新增函数 | 职责 |
| --- | --- |
| `create_conpty()` | 创建输入／输出管道和 pseudo console，失败立即按逆序释放 |
| `spawn_conpty_process()` | 通过 STARTUPINFOEX 的 pseudo-console 属性连接 ConPTY；受控创建子进程，挂入原等价 Job 树所有权后恢复运行 |
| `resize_conpty()` | 调用 `ResizePseudoConsole`，验证尺寸和资源未关闭 |
| `close_conpty()` | 协调写入停止、进程树、输出排空与 HPCON／管道关闭，防止关闭顺序导致死锁 |

可以使用经过项目依赖评估的成熟封装；若自己绑定 Win32 API，要为属性列表、HANDLE 和 HPCON 明确唯一所有者。选择封装不等于另起任务引擎。

重点检查：启动失败的每个中间步骤、Stop 与 spawn 竞争、继承句柄泄漏、子孙进程清理、reader 未退出时的超时诊断。ConPTY 的 `ClosePseudoConsole` 可能等待输出处理，不能在不再排空输出的情况下直接阻塞关闭。

### 4.5 中断、EOF 和取消分开

继续在原 task controls 中加入：

```text
task_signal(task_ref, signal="interrupt")
task_resize(task_ref, cols, rows)
```

`task_signal` 是有外部副作用的 unsafe 操作。resize 可设计成同 attempt 上的幂等设置；同样走原 effect 收据和授权。

- Ctrl+C／interrupt：按该平台终端模式发送中断，可能只结束前台命令，shell 仍活着；不能直接令任务 cancelled。
- `task_cancel`：请求任务取消，终止受管树、回收所有资源，最终由 durable 确认结束。
- 管道 EOF：关闭写端。
- 终端 EOF：不能统一等价为“发送 Ctrl+D”。POSIX cooked mode、raw mode 和 Windows 控制台语义不同；明确后端能力，不支持的 EOF 返回不支持，原始字符可通过 data 原样发送。

不要把 ConPTY 的中断简单写成无条件 `GenerateConsoleCtrlEvent`：控制台连接、前台程序和输入模式会改变行为，需要真实平台测试。

### 4.6 输出继续用一份存储

PTY 常把 stdout/stderr 合并成一个终端字节流。建议通过现有输出元数据表达 `mode=terminal`、`streams_merged=true`，在原输出存储的 stdout 通道保存此唯一流，stderr 标为不可独立获取；这两个元数据名是建议。

保留原游标单位、截断和 gap 规则，不能按字符、UTF-8 字节来回切换。按原增量解码策略处理跨块多字节字符；ANSI／CR 等终端控制序列不靠伪造行边界解决。为模型生成有界文本视图，原终端流仍只有一份。

若 UI 以后需要可视终端渲染，它消费同一输出事实；不由 terminal widget 自己启动另一个进程或另写一个结果日志。

### 4.7 交互 shell 不是一条测试

检查 [shell_service.py](../codey/app/shell_service.py) 的 `claim_shell_ticket()`、`execute_shell_ticket()`、`_execute_shell_ticket()`。

把已批准 shell 的进程创建和收集也收敛到同一 ProcessIO。若它要受 task controls 管理，必须在现有批准任务分派内使用同一子任务接纳入口，取得原 owner 和 attempt；不要从 UI HTTP handler 偷偷创建无 owner 会话。

审批事实与工具子任务关联起来，沿现有执行事务接纳；不要复制一个新的“终端审批已允许”布尔值，也不要消费一次性 ticket 后在崩溃恢复中再消费／再启动。

在 `tool_execution_result()`、`tool_task_snapshot()` 和验证投影中，分别保留：进程生命周期结果、shell 执行结果、真正验证命令的结果。一个长期 shell 最终 `exit 0` 不证明里面每条测试都通过；输入返回“已写入”更不能产生验证成功。

## 5. 浏览器后台：先只开放 browser_wait

### 5.1 当前线程结构要沿用

[BrowserSession._browser_call](../codey/automation/browser_session.py) 调用 [browser_search._search_browser_call](../codey/research/browser_search.py)，实际页面操作在独立 Research BrowserWorker 上。

应用任务提交另有 BrowserWorker 使用场景；不能仅凭类名相同就认定模型等待一定占住了页面线程，或大改整个任务分派。

真正需要拆的是 `BrowserSession._wait_on_browser_thread()`：当前在页面线程里最多循环等待 30 秒，期间其他页面工作不能正常排队推进。

### 5.2 扩展原 ToolTaskInput，保留原参数身份

修改 [ToolTaskInput](../codey/durable/tool_task.py) 的 `to_payload()`／`from_payload()`：

- 从仅允许 `call_name == "run"`，改成结构解析后再按原 frozen spec 校验后台能力。
- project 改为依执行器需要；不能给浏览器任务造一个假项目路径。command 执行器仍必须有真实项目和权限。
- 增加原浏览器资源和文档绑定引用所需的输入，例如可选 `resource_binding` 字段，或引用现有冻结资源快照。具体选一种，整条链只保留一处权威声明。
- 绑定内容复用 `BrowserBinding.payload/read` 与现有 document identity，不发明一套 browser ID。
- 版本升级并显式读取旧 v1/v2；保留 v1 `run_in_background=true` 的迁移规则。
- 原 `call_name/args` 不变，身份摘要仍基于原完整参数。不要转换成 `background_browser_wait` 再定义第二份参数。

在 [KernelEffectSink.admit_tool_task](../codey/operations/task_effects.py) 接纳前，用原绑定入口取得可序列化的资源身份；必要的页面线程读取在 durable 事务之外完成，接纳／执行时再校验有效性。

### 5.3 修改 frozen spec 与接纳判断

在 [tool_spec._browser_specs](../codey/toolchain/tool_spec.py) 中只给首批 `browser_wait` 设置 `supports_background=True`。`_execution_parameters()` 会继续生成原 `run_in_background` 参数。

修改：

- `kernel_execution._admit_background()`：去掉所有工具都必须有 project 的假设，按 frozen spec 执行器检查必要资源。
- `KernelEffectSink.admit_tool_task()`：保存原工具和原资源引用。
- `tool_tasks._run_admitted_operation()`：按 frozen `spec.executor` 构造执行依赖。

`_automatic_command()` 保持 command 的现有产品策略；首版浏览器用显式 `run_in_background=true`，不必顺便改变全部浏览器动作的默认行为。

`complete_wait_args()` 等补全仍须在 admission 之前完成，并使用当轮冻结的 stage／declared timeout。不能后台执行时按新一轮 stage 再补一次参数。

参数校验、授权、intent／settlement、cancel 与 attempt fence 保留在一条通用执行骨架中；只让“构造资源依赖、执行叶子操作”按现有 executor 分支。不要复制一整个 `run_browser_task()` 调度器。

### 5.4 BrowserSession.wait 改为线程外协调、线程内短检查

修改 `BrowserSession.wait()` 与 `_wait_on_browser_thread()`；建议把后者拆成：

```python
def _check_wait_on_browser_thread(
    self, condition, value, expected_binding,
) -> WaitObservation:
    # 所属浏览器线程：校验绑定，只检查一次，返回纯数据
    ...

def wait(self, condition, value, timeout_ms):
    # 协调发生在原工具执行线程，不在页面线程长时间循环
    deadline = original_absolute_deadline(timeout_ms)
    while True:
        cancellation.check()
        observation = _browser_call(self._check_wait_on_browser_thread, ...)
        if observation.is_terminal:
            return original_browser_output(observation)
        cancellation.wait(short_interval_bounded_by(deadline))
```

以上 `WaitObservation` 和辅助函数名是示意；最终只保留一个实际使用的内部结果类型，外部仍返回原 `BrowserToolOutput`。

要求：

- deadline 只计算一次，队列等待也计入总预算；不能每次检查都重新获得完整 timeout。
- 原条件仍是 text／selector／url_contains，不在 `task_output` 里再解释条件。
- 一次检查必须短，不调用长 `wait_for_*`；数量过大的 selector 扫描也要有边界。
- 检查、事件解绑、页面访问都在所属 BrowserWorker；不把 Playwright Page／Locator 丢到子任务线程直接用。
- 明确禁止协调循环从所属页面线程内进入，否则所谓拆分后仍会占线程。当前真实执行入口保持线程分工；测试要验证线程 ID。
- 返回值仍经过 `ExecutionDelegate._execute_browser()`／`_execute_browser_operation()` 包装，保留原审计、reference failure、query wait document 等信息。

### 5.5 保留页面绑定，并明确导航时的语义

复用 `BrowserSession.resource_binding()`、`document_identity()`、`prepare_from_receipts()` 和原 provider 绑定恢复路径。不能在后台随意新建 BrowserSession 然后选一个“当前页”。

建议为现有 BrowserSession／provider 生命周期加入短期资源租约：子任务持有期间，父任务释放不能提前关闭它；最后一个持有者退出后走原关闭路径。租约不定义另一份任务状态。

等待条件区别处理：

- text／selector：绑定原 target 和 document；同 URL reload 的 loader 变化也属于文档变化，报告失效，不能悄悄改看新文档。
- url_contains：允许同一 target 的预期导航，不能把本来要等待的 loader 变化误判为失败；仍拒绝切换到另一个 target。
- 父任务切页、关页、重新绑定：按原资源身份规则报告失效／关闭。
- 取消等待只清理该等待者；不自动关闭用户页面或取消其他允许操作。

短检查期间使用页面操作原协调机制；被动等待的整个期间不持有阻止点击的独占页锁，否则“等点击后出现文字”永远不可能完成。

### 5.6 让任务执行器拿到原浏览器依赖

修改 `TaskSubmissionStores` 或它引用的既有资源服务，把原 browser binding 的受限租约解析能力提供给子任务。这里需要传递的是服务能力，不是另建一份浏览器会话仓库。

`_run_admitted_operation()` 的 browser 分支：

```text
恢复原 frozen snapshot，检查 browser_wait 后台能力与授权
解析接纳时的原资源绑定，取得租约
构造包含原 browser_session 的 ExecutionDelegate
在原 child/attempt 下记录唯一 execution intent
调用同一个 execute_tool_operation()
写唯一正式 settlement，释放租约，完成／取消 child
```

当前该函数的 `Path(request.project)`、项目锁、workspace 扫描和仅 `AgentToolFns(run_command=...)` 都是 command 专用假设，需移入 command 分支。浏览器任务不要做虚假的 workspace 扫描。

对浏览器任务，`tool_task_snapshot()` 的进程日志可以缺省；最终结果使用同一个 child receipt。`_task_outcome()` 不得要求浏览器必须有 process exit code。`tool_execution_result()` 仍只读取主执行 effect。

`dispatch_durable_task()` 当前已有 `execution_kind == "tool"` 入口，继续共用它，无需新增浏览器后台队列或第二个 scheduler。

### 5.7 研究证据和重复观察

`browser_wait` 成功只建立等待条件事实，不自动建立“已阅读来源”。后台结果仍从子任务原收据经过 `project_result_observation()`；重复 task_output 只返回观察，不能再登记一遍研究来源或浏览器成功证据。

首版不要同时把 click、fill、navigate、download 全标后台：点击／填写有交付不确定性，导航有重定向政策与页面变化，下载还有 artifact 落盘和事件处理器清理。先用 browser_wait 验证共用生命周期，再逐个扩展真正需要的操作。

重启后进程句柄和旧等待协调器不复活。已结算结果可重投影；未结算且无法证明状态的操作按原 interrupted／unknown 处理，不静默重发。后续即便允许纯观察等待重接，也必须是原任务明确的恢复规则。

## 6. 按五个提交验收

| 提交 | 修改范围 | 最小验收 |
| --- | --- | --- |
| 1：P1 | 历史查询、统一验证投影、外层 evidence | 上述真实复现变为 `[通过,失败]`；热恢复、未知结果与重复读取不复活旧成功 |
| 2：stdin | 原参数链、pipe 输入、资源绑定、task_input | 两轮连续输入、无自动换行、EOF、重复 effect、部分写入、退出后输入、取消／关闭竞争、旧 attempt |
| 3：I/O 收敛 | PipeProcessIO 与统一采集／收尾 | 原 command 和批准 shell 的 timeout、捕获、cancel、Job 清理行为保持正确 |
| 4：PTY | POSIX／ConPTY、signal、resize、输出元数据 | isatty、无换行提示符、Unicode／ANSI、尺寸读取、Ctrl+C、前台命令结束而 shell 保留、最终树清理 |
| 5：浏览器等待 | 原资源绑定、按 executor 构造依赖、短检查 | 等待时允许动作可执行；导航语义、同 URL reload、关页、cancel、重复读取、重启、页面线程约束 |

补充跨层验收：

1. 子任务等待输入时，模型能调用 task_input、task_output 和 task_cancel，不发生项目锁死锁。
2. PTY 内跑了一条失败测试后 shell 继续存在，不把输入回执或 shell 退出成功算成验证通过。
3. 运行测试后修改代码，旧结果不能验证新代码。
4. 浏览器等待期间点击触发 DOM 更新，等待能结束；取消等待后另一项页面操作正常完成。
5. 桌面、CLI、native、文本协议、CodeMode 使用同一 schema 与结果引用。
6. 热重试／服务重启时不重复启动进程、不重放不确定 stdin、不重复登记来源。
7. 真正的 Windows ConPTY 测试不能用 mock 通过代替；无平台条件时明确记录未验收，不声称 Windows 可用。

可分批复用的现有测试包括：

```powershell
python -m pytest -q tests/test_tool_task_background.py tests/test_tool_task_entry_authorization.py tests/test_completion_verification_latest_result_wins.py tests/test_kernel_unknown_verification_invalidates_success.py tests/test_execution_evidence.py
python -m pytest -q tests/test_cancellation.py tests/test_execution_output.py tests/test_kernel_execution_snapshot_guards.py
python -m pytest -q tests/test_browser_worker.py tests/test_browser_interaction.py tests/test_browser_snapshot_recovery.py tests/test_browser_query_binding.py
```

这些是修改后的建议回归，不代表本轮全部执行通过；还需新增上述真正缺失的回归场景。每步完成后运行对应检查即可，不因本篇文档变动跑全量模型实验。

本文本轮验证仅包括：当前源码核对、上述无模型真实子进程 P1 复现，以及文档路径和格式检查。生产代码未修改，stdin／PTY／浏览器后台能力尚未在本轮实现或验收。
