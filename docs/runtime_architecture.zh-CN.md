# Codey Runtime 架构（living doc）

这份文档只写当前真相。历史演进去看 `CHANGELOG.md`，不要在这里考古。
改动 runtime 时同步维护本文；测试报告的结果必须等实际验证结束后再记录。

完整包结构与当前函数入口见[项目结构与职责地图](project_structure.zh-CN.md)。

## 工具执行生命周期（2026-10-25）

普通工具和 CodeMode 子调用共用 `execute_turn` 的授权、执行、结算和恢复。
`TaskSession.tool_dispatch` 只拥有会话内的临时准入队列；冻结 `ToolSpec.parallel_safe`
且可安全重放的叶工具可以并发，其余叶工具独占执行，直到工作区身份与原收据提交完成。
CodeMode 父调用是编排容器，不持有叶工具锁；子调用重新进入同一准入点。
此队列不持久化，不记录成功状态，不成为新的效果账本或第二执行运行时。

`cancellation.linked_scope` 保留外层 Stop 和最早截止时间，并允许当前调用独立取消。
CodeMode 的宿主线程显式继承这两个上下文；程序返回、报错、超时或取消时，父调用
必须收齐已经启动的宿主回调，再经原 `KernelEffectSink` 结算。模型遗漏 `await` 的
工具或状态调用使程序失败，已发生的效果仍保留原收据。取消结果也携带已经提交的
子效果引用；重启读取这些事实，不再次执行。已经提交的成功回执不会被后到的取消改写。

`store/load` 直接使用原 task checkpoint 中的 CodeMode state；移除了 Node 端和
`CodeModeResult` 中的写入镜像。模型必须等待每次状态操作；没有旧 worker 协议兼容分支。
调度器、线程和取消令牌是共享资源，事实投影不复制它们。

截止时间限制工作准入与协作执行，不授权遗弃仍在运行的线程。清理会等待现有工具
自己的取消/超时路径结束，因此清理耗时可能超过执行预算；不会在子调用仍运行时宣称完成。
未增加后台进程保活、跨重启进程恢复、自动重跑或新的模型重试策略。

## 协议修复及固定评测（2026-10-24，第 5 批）

原生协议修复复用原冻结工具快照。畸形 tool_calls 不会降级为正常答案或执行；
任务已经可以回答时，提示模型通过已声明 done 工具的 summary 字符串重新提交，
由原共同 gate 校验并保留用户格式。没有第二个 JSON 答案解析运行时。
跨领域评测直接调用生产 run_task_kernel、KernelEffectSink 和正式恢复入口；
比较器要求题集/运行器摘要、实际模型参数及完整场景集合一致，不把缺失场景当通过。

## 来源完成契约与交付（2026-10-24，第 4 批）

来源准入只读 completion_context 中的 committed research ledger，缺失时不再改用
session.opened_sources。后者继续作为工具别名和控制器的派生视图。来源 proof ref
绑定完整最终 URL、正文摘要和采集时间，不裁剪 URL 作为身份。
`research_citations` 复用既有引用扫描器和来源行解析器；严格研究要求引用，普通任务
仅在来源契约范围内校验已有引用，显式要求该检查时才强制至少一条引用。
未知来源/编号、未读页和非法页码范围不能通过；JSON 的数字数组不视为引用。
严格研究仍复用原报告编译、片段证据及质量检查，没有第二个 done 判断。
界面回执的依据与限制按已有 completion_proof 引用即时渲染，不增持久字段或改写回答。

## 请求时钟（2026-10-24，第 3 批）

`providers/request_context.py::clock_context` 是唯一的请求时钟渲染入口，给出 UTC、
主机本地时间/日期、UTC 偏移、主机时区名称及操作系统来源。它不猜用户时区。
API/local/Zen 的 `_admit_request` 每个逻辑生成捕获一次，计数和发送使用同一个 payload；
HTTP 安全重试复用原 payload。上下文只加入请求视图，不写入或改写语义历史。
工具续轮、评审与 compaction 经过相同 ApiProvider 边界。浏览器 provider 每条新发送
消息使用同一个渲染入口；远端已有历史保留原时间。时间上下文不成为外部事实证据。

## 观察提交与投影（2026-10-24，第 2 批）

共享工具内核的采集 facade 使用临时 ledger。`kernel_execution` 在临时 TaskSession
与 ledger 上调用 `kernel_facts.project_result_observation`，校验通过后提交原有
KernelEffectSink 完整回执，最后发布临时投影。提交异常不发布正式事实或执行索引。
`kernel_session_recovery` 调用同一个投影函数，并保留整批成功才发布的边界。
`TaskSession.clone_projection/commit_projection` 统一容器隔离，完整回执仍是唯一持久依据。
未执行命令的回执记录 execution_disposition，实时和恢复均不把它累计为失败验证。
读取/搜索缓存只从 committed ledger 派生；知识工具的采集视图据此重建读取资格。
知识文件、已发生的 edit 等外部效果并不因回执失败自动回滚；不确定执行仍走原恢复策略。

## 服务、控制与持久入场（2026-10-21）

`app/service.py::LocalService` 持有显式 AppContext、HTTP server、公共事件广播和服务 lease；
`app/desktop.py` 只连接窗口。桌面、CLI control、JSONL RPC 与可选 MCP 共用该服务，
不在 handler 中隐式初始化全局状态。独立的 `agent` headless 命令仍使用共用任务服务。

```text
桌面输入 / control HTTP ← CLI / RPC / MCP
             ↓
DurableSession 入场、请求指纹、原任务检查点与输入队列
             ↓
原 scheduler → 原 task runtime → 原模型工具内核
             ↓
持久聊天/执行事实 → 公共事件广播 → 桌面与外部观察
```

`durable/control.py` 保存控制配置与幂等回执，被动条目不成为第二套执行状态。
外部消息来源和返修关系经过原消息写口持久化；会话与提交查询通过 DurableSession 的锁读取。
独立验收绑定工作区候选摘要，不能由模型的完成总结代替。人工接管与停止推进控制代次。
广播只缓存有界公共字节，慢订阅独立断开，重连再从持久事实对账。

`launcher/` 在不可变 runtime 快照间受控切换：隔离验证、drain、进程替换、健康确认、
启动失败回退。只支持依赖与数据库 schema 不变，不回滚任务数据库。
`durable/sqlite.py` 用现有 OS lease 序列化冷启动初始化，包括 WAL 转换；不改变事务写入权威。
接口、启动方式与边界见[外部控制实现](external-control-implementation.zh-CN.md)。

## 文本协议边界（2026-10-12）

任务入口在 `task_loop._receive_turn_plan` 将文本模式的字符串交给
`protocols/text_turn.decode_text_turn`；已有 `AssistantTurn` 直接进入内核。
协议选择仍使用本轮冻结结果，不将文本调用伪装成带原生 ID 的 API 调用。
辅助讨论、私有评审等 `provider.send()` 不自动启用任务文本解码。

```text
网页驱动读取／既有网页修复 → 文本 codec ┐
文本 Local               → 文本 codec ├→ kernel_protocol → 执行／完成门
结构化 Local／Zen        → AssistantTurn┘
```

文本 codec 只产生有序 `TextRequests`、`TextAnswer` 或保留诊断的 `TextError`，
不读取项目、不授予权限、不执行、不判断完成。`done`、`read_files`、`parallel`
仍由内核进行控制判断和批次展开。内核校验冻结快照、权限、参数和调用身份，
拒绝原始字符串。旧 `JsonToolCodec` 无生产消费者，历史基线只在测试支持目录。

生产提示不变。整段工具形状 JSON 在 `done.summary` 之外仍可能被识别为调用；
合法外层 `done` 的摘要只作为答案正文处理，即使其中含有工具形状 JSON，也不会
被二次解析或执行。本轮没有解决摘要之外的 JSON 示例歧义。
网页补丁维持各驱动原触发条件、顺序、输出及失败处理，不强行统一修复流水线。
GLM 的代码弯引号修复即使外层 JSON 有效也可生效；Local、Zen 不进入网页修复链。

## 结构化来源观察（2026-10-24，第 1 批代码）

搜索、打开来源和页内搜索的生产者与恢复读取共用
`research/ledger_receipts.py` 的纯观察校验器。成功搜索必须携带结构化结果，零结果是
合法的空结果列表；缺少观察不是零结果。来源记录来自实际观察中的最终 URL、正文
摘要、读取时间及页信息，不再从展示文字、请求 URL 或别名表补出成功读取事实。
页内搜索的 hit ID 和 locator 从结构化命中生成，不反向解析显示行。

`ToolResult` 和 `ToolOutcome` 的 canonical 数据通过同一个无损 JSON 边界复制；
不支持的对象、非字符串键和非有限数直接拒绝，不转换成描述字符串。presentation
和 audit 继续使用原有有界展示投影。完整 canonical 的存储限制由现有结果回执和
ManagedOutputStore 执行，超过限制明确失败，不裁掉正文后继续沿用原摘要。

本批不改变工具结算、ledger 发布顺序、任务授权或完成门语义。已有的
ledger/session 来源替代准入以及模型跳过要求的后续读取，仍需要后续批次处理；
结构完整和恢复一致不代表答案的语义已经得到验证。本批的实测和晋级状态见
[验收记录](reports/structured-observations-batch1-2026-10-24.zh-CN.md)。

## 一句话

durable entries 进来，经过一次标准解析，变成决策或写入，观察层只能看。

```text
Entries
   │
   ▼
SessionView ──┬──► Drive ──► 恢复动作
              └──► MutationLine ──► SessionLog
                                        │
Observe ◄───────────────────────────────┘（只读投影）
```

## 五个包

```text
runtime/core/     operation / state / reducer / ports / outcome / models / cancellation
                  api_selection / operation_payload
runtime/log/      session_log / projection / session_view / compaction
runtime/effects/  effect_records / delivery / replay_* / safe_tool_replay
runtime/write/    mutation_line / drive / task_runtime
                  provider_effects / tool_batches / delivery_recovery
runtime/observe/  events / evidence / prompt_* / terminalizer
```

- `core/`：状态机 + 纯决策 + 契约。reducer 不做 IO，不认识业务层。
- `log/`：持久化 + 读模型。`session_view` 是唯一的 canonical read projection。
- `effects/`：外部效应账本。`intent -> effect -> settlement`，恢复只读
  committed state，不从事件缺失推断。
- `write/`：唯一的写口。`mutation_line.py` 是 facade，只做 admit + commit；
  真正的行构造是三个纯 builder 模块。
- `observe/`：丢了可重建的投影。`write/` 禁止 import 它，它也禁止 import
  `write/`（`tests/test_architecture.py` 锁死）。

## 连续执行与结果交付（2026-10-09）

- 工具完成先投影工作区身份、验证和 WorkCheckpoint，再发布可触发外部中断的完成事件。
  原有存储故障处理仍适用；该顺序不能保证磁盘写入失败时也持久成功。
- 正式恢复在整批回执校验成功后填充已有的 TaskSession 结果缓存；模型可读取原结果，
  不需要重跑命令，也不新建一份持久化记录。继续写入的完成范围合并旧检查点与本次实际编辑。
- `read_tool_result` 在任务所属、digest 验证通过的结果上支持有界 literal query，
  仍不接受任意路径。受管命令捕获采用现有 2 MiB 存储上限，避免先被普通 256 KiB
  捕获截掉中段；模型预览另行裁剪。超过上限仍明确标为不完整，未找到不代表原输出不存在。
- 明确的全局只读要求在入口落实到原 TaskPolicy；局部“不要改测试/某文件”不扩大为全局只读。
  每轮工作事实携带验证身份及 current/stale/unknown 状态，不能由叙述覆盖或提升为完成证明。
- 原生 API 的提示只引用线上的工具 schema；文本模式继续使用现有 JSON 工具协议。
  服务端已接受输入、但回复解码失败时，合法输入和已执行结果仍提交，坏调用不进入历史；
  取消后的迟到响应仍不得提交，未知传输结果仍不得自动重放。
- API 终止回执本地配对并提交，不再为停止或完成启动新生成。继续回答走正式发送路径；
  网页交付与 Zen 的实际内部闭合请求仍遵循各自协议。
- CLI `task_done.summary` 是最终交付，独立上限为 64,000 字符；超限明确携带
  `summary_truncated: true`。进度预览保持 1,000 字符，工具结果预览保持 200 字符。

## 候选身份与完成证据（2026-10-11）

- 生产流程不再从“删除 ASCII 标点”要求生成专用准入、标点探针或聚合行为检查。
  未实现通用语义验收替代；模型审核与已登记测试通过不能证明所有自然语言要求都满足。
- `project_review_phase.validate_candidate()` 仍是首次完成及有界修复共用入口。
  停滞候选复用原授权验证，修复后的候选需要当前代码的真实验证和新审核，
  再交既有完成门；取消、权限、累计预算和当前审核身份约束保留。
- `read_tool_result` 只读取已有工具执行回执，正式恢复通过原回执校验重建缓存。
  工具名不赋予未登记归档额外权限；所属任务、digest 和有界查询校验保留。
- `explicit_execution_requirements.py` 从有限原始要求投影执行约束：相同工作区的显式 once
  命令重复引用既有结果；要求读取保存结果时，写入与完成前必须有真实读取。
  停滞已触发后，明确要求修复、禁止写入且当前验证真实失败时可表达 blocked；
  取消、未知或过期观察不能被这个投影改写。状态继续归属现有 TaskSession。
- 原子写入只重试真实 WinError 32，沿用同一临时文件；期间目标身份改变则停止覆盖。
  无第二套状态机、存储或厂商依赖。执行回执与历史结果继续使用普通 Codey 数据。

## 三个硬锁（测试里）

1. `SessionView` 只有 `state / effects / batches` 三个字段，不准长出
   messages、evidence、ghost、completion。
2. `.mutate()` 全仓只允许 `write/mutation_line.py` 和
   `log/session_log.py` 调用。
3. 平铺残留 `codey/runtime/*.py` 被断言不存在；`write/` 与 `observe/`
   互禁 import。

## Pending 是派生的，不是存的

`RuntimeOperationState` 只存 leaf / driver / task / turn 等坐标，不存
`pending_effect_*` / `pending_delivery_batch_id`（payload schema v1，其他版本
直接 fail closed）。在飞事实一律现场算：

- `pending_for(view)` 只看 `state.turn` 当 turn 的 unsettled intents。
- delivery pending 优先选同 turn 未动过的 fresh batch（failover 契约），
  真含糊才 fail closed，绝不随便抓第一个。
- `completion_proof_satisfied` 从 proof status 派生（`complete` 即满足），
  不持久化；畸形 proof 在 project completion 提交边界 fail closed。

## 外部 effect 纪律

每个真实外部效果先写 intent，效果后写 settlement；safe 读可重放，
unsafe 永不自动重复。storage/session 层不知道 agent 语义，progress 和
outcome 不自动变成完成证据，完成只认 proof + verification。

API 任务的 `model_selection` 在 admit 时写入正式 operation state，独立
Reviewer 的 `reviewer_selection` 在发送前冻结。两者只保存连接版本、模型、协议、
能力和参数；凭据由连接配置提供，不写入日志或前端。重启恢复不从当前 Settings
替换原选择；原连接失效就明确阻塞。协议历史归 Provider，不成为第二套任务事实。

API 的文本、原生首轮、工具结果及结束交付都走同一个 `_exchange`；锁、取消代次、
候选提交只由 ApiProvider 拥有。构造时选定无状态 codec，由它准备协议历史、编码和
解码；共享层不查询 Local 能力。HTTP 失败、取消后的迟到结果、不完整文本和意外
文本通道原生调用都不提交候选历史。连接工厂提供完整预算。

本次选择冻结模型容量、计数能力与预算来源，API 不读取静态网页容量，也不使用
字符累计值触发 rollover。ApiProvider 统一编码后计数、按 codec 的完整交换边界
裁剪、重新计数并准入，最终发送同一份 payload。Local 拥有 KoboldCpp 完整模板
计数；Local 与 Zen 各自解释原始 usage。transport 只交付原始事件，每次物理请求
在回答解码前规范化记录到现有 RunTrace；缺失用量不记成零。运行详情只读普通
记录，移除 Zen 不影响 Local 与历史统计。统计属于观察，不成为完成证据或第二套
任务事实。详见[请求预算与 token 统计](token-accounting.zh-CN.md)。

### 可追溯的模型上下文

已接纳协议原文归 `ContextLedger`，可替换视图归 Provider；任务与执行事实仍归
既有运行时，不能把协议归档当成第二套执行真相。检查点保留来源 digest 和计数，
持久化 head 成功后才更新内存。后台候选允许保留并重计数新追加尾部，来源变化、
取消或模型身份变化则不提交；紧急候选随正常回答原子提交。

每次实际内核请求前投影当前任务、文件及命令回执；`read_tool_result` 只读当前
任务已有结果，校验托管输出的 session/run 归属和 SHA-256。未知状态不冒称完成，
过期验证不能证明当前工作区；读取失败不自动启动命令。保留现有同步执行流程，
不新增后台进程监视器。辅助模型请求是独立有界生命周期，Zen 仅解释自己的外壳，
不进入核心上下文数据类型。

旧工具输出的候选选择归 `context_checkpoint.py`：无回执时用可逆重复/行模板编码，
保留所有差异值与换行；有当前任务可信回执时使用只读定位。正文引用的旧回执不能
替换运行时最后追加的结果 ID，末尾 ID 不可用时保留正文。最新完整工具往返保持原文。
协调器只接纳连接实际计数更小的候选，复用来源/候选计数，并在原子提交前重新计数
包含新增尾部的实际请求。原始事件与执行事实不因这些视图变化而改写。

无回执的可逆缩减可提前进行；有回执的缩减在输入压力达到 60% 且至少两份旧结果时
批处理，减少缓存前缀改写；80% 触发语义维护。压力只用于调度，最终准入仍用连接
计数。这些是当前回放支持的内部启发式，不是所有模型的最优阈值。
辅助请求用独立系统指令把来源视为历史数据，输出简洁工作状态，避免回答历史问题或
重复空字段。Zen 继续只解释连接差异；可逆编码、回执与工作状态不依赖 Zen 类型。
详见[最新优化与对比](context-compaction-optimization-2026-10-09.zh-CN.md)。

`final_delivery` 与 completion proof 分开记录。发送结束工具结果仍是有界生成请求，
并非专用网络 ACK；返回的新工具不能执行，只能在有限预算内关闭调用链。
交付 failed/unknown 不丢弃完成证明或已执行变更，恢复处理交付事实，不重新写文件。
取消、无进展、协议阈值及预算终止的关闭请求继续使用本轮实际授权的工具声明，
不重新生成完整工具集，不执行结束阶段的新调用。TLS EOF 只有在确认尚未发送
HTTP 生成字节时允许一次连接重试；代理 CONNECT 不算生成提交，共享总 deadline。
已发送/结果未知的生成不重发，证书失败也不重连。

## 统一 kernel 的行为边界与 parity

Chat、Coding 与 Research 共用 `task_loop`、`kernel_protocol`、`kernel_execution` 和
completion gate。`project_adapter` 保留 AgentRequest/RunResult 边界；
`research_iteration.run_research_iteration` 负责知识库与 synthesis 交付，不保留
旧迭代类。`run_task_kernel` 只接收正式 `KernelRunRequest`，其依赖分为 transport、
execution、observation；不再提供旧 keyword 适配。运行回调由 `task_phases/hooks`
的一次运行实例持有，任务提交资源由 `TaskSubmissionStores` 显式捕获。

严格 Research 指导由 `research/completion_guidance` 定义，领域入口按原策略选择并
通过 `KernelRunRequest.task_guidance` 传入。`kernel_prompt` 只组合文字，不选择
Research 工作流，也不刷新验证或扫描工作区。`project_prompt_context` 在编排阶段
取得现有验证判定并准备不可变 `CodingContext`；渲染器按 JSON/native 展示。
报告标题与 note ID 规则复用各自正式契约，不维护第二份章节或工具参数规则。

- 文本 `read_files / parallel` 在解析期整批校验后降低为有序只读调用；native
  只用各自显式 ID，不为 wrapper 编造 child ID。执行和恢复仍按每个 effect 结算。
- `KernelProgress` 只观察变化、结果指纹与 SeenInfo，不生成 completion proof。
- verification candidate 是提示；结构化 exit code、相关范围和当前 workspace
  身份才是验证证据。禁止验证可为 not_applicable，不能伪装 pass。
- provider 响应转换仍由显式 adapter hook 完成；会话/durable 包装器显式转交，
  不信任动态属性，也不在 kernel 猜模型模板。
- 取消后不启动新 effect；已有 delivery/recovery 仍优先使用原始结算，未执行槽
  结算错误，native terminal 的已知 ID 及有界后续 ID 收到错误回执。
- Research gate 检查并返回同一份 evidence 编译答案，随后该正文进入 synthesis。

固定旧版源码 oracle 与精确差异门见
[确定性 parity 审计](kernel_parity.zh-CN.md)。旧循环只允许由独立子进程 probe
导入 reference tree，禁止回流为生产兼容模块。


## 原策略和恢复事实的当前所有者（2026-10-01）

- 原策略在 dispatch 的首个模型/工具调用前记录；MutationLine 首次设置后拒绝不同值，相同值幂等。
- runtime 保存策略原 JSON，领域 schema 只由 TaskPolicy 校验；恢复缺失或非法就阻塞，不从新请求补授权。
- delivery recovered 只表示已重建，不推进为 delivered；只有实际交付确认或 abandoned 才终止该批次。
- 已交付工具观察仍投影为 TaskSession 事实；项目和共同入口共用 `kernel_session_recovery.restore_task_session`。
- Research 观察在同一工具收据 canonical 内；`ledger_receipts` 恢复完整正文、页和证据。整批暂存，成功才发布，不建立第二持久日志。
- Chat、Project 的首轮都直接进入共享内核；正常回答和工具调用使用原策略、窗口与累计预算。
- `kernel_preparation` 一次准备 prompt 和工具快照。原生回复中的文字不扫描成工具，
  文本调用必须是契约规定的裸 JSON；代码围栏和普通 `name` 字段不执行。
- 未绑定目录的 Chat 使用 `workspace/session.py` 解析的受管根目录，应用状态不授予
  文件工具。原文件夹关联保留在 request 中，Shell 审批续跑不把临时根目录冒充项目。
- 普通回答不抢写锁，实际 edit/run 取得现有写锁才执行；失败关闭原 native call IDs。
- command/cwd/URL 是完整身份；展示边界可以裁剪，事实和完成关联不裁剪。

可执行检查与准确的证明边界见 [kernel_invariants.zh-CN.md](kernel_invariants.zh-CN.md)。

## Shell 审批交付与展示恢复（2026-10-13）

`ResolveShellApproval` 从服务端已校验的持久结果构建文字 provider 的 continuation，
同时更新正式 request/task；不会因结构化输入重新解析而退回没有工具结果的通用提示。
原生 provider 继续按原 call ID 配对交付，审批不强制新建聊天窗口。

执行结算与 UI 展示分开：UI 状态存储保留 shellStatus、命令预览截断/摘要及工具 status；
exitCode 只接受真实整数并保留 POSIX 负值，非法值保持未知。历史缺失状态不推断为 Failed，
也不推断为成功。UI 历史不能替代服务端审批事实或授权。

回归与边界见[测试报告](../TEST_REPORT.md)及[维护审计](ui_maintenance_audit_2026-10-13.zh-CN.md)。

## Shell 解释器合同（2026-10-24）

任务创建时取得不可变 `ShellSpec`：方言、实际路径、实测版本、启动模式和入口二进制
摘要。覆盖 PowerShell 7/5.1、Bash、Zsh、Sh、CMD；默认仍为 Windows CMD、其他系统
`/bin/sh`。显式配置只在新任务准入时发现，批准后不切换解释器；启动参数由类型生成。

唯一 `analyze_shell` 按方言选解析器，统一记录命令/参数、管道/连接、重定向、展开和动态
执行。无法确认的语法为 opaque，只能逐次审批；无效语法拒绝。分析绑定完整命令和
解释器摘要，与 ShellSpec 一起进入原动作策略、命令 artifact、授权、恢复和执行票据。
恢复只校验已提交分析，不从文字推测，不重跑解析器。旧合同或缺失分析不自动补权。

执行继续由原 cancellation/process-tree、输出、审批回执和续跑链路负责。执行前检查
入口二进制摘要；不宣称冻结所有依赖。允许验证的 `run` 仍直接执行 argv，明确区分其
语义。没有增加第二执行器、权限系统、持久日志、命令翻译或解释器重试。
详细流程与平台验证边界见 [shell 审批生命周期](shell_approval_lifecycle.md) 和
[三批验收记录](reports/shell-dialects-2026-10-24.zh-CN.md)。
