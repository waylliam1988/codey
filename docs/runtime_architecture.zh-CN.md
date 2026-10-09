# Codey Runtime 架构（living doc）

这份文档只写当前真相。历史演进去看 `CHANGELOG.md`，不要在这里考古。
改动 runtime 时同步维护本文；测试报告的结果必须等实际验证结束后再记录。

完整包结构与当前函数入口见[项目结构与职责地图](project_structure.zh-CN.md)。

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

Coding 与 Research 共用 `task_loop`、`kernel_protocol`、`kernel_execution` 和
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

- 原策略在 dispatch 的首个工具/auto 调用前记录；MutationLine 首次设置后拒绝不同值，相同值幂等。
- runtime 保存策略原 JSON，领域 schema 只由 TaskPolicy 校验；恢复缺失或非法就阻塞，不从新请求补授权。
- delivery recovered 只表示已重建，不推进为 delivered；只有实际交付确认或 abandoned 才终止该批次。
- 已交付工具观察仍投影为 TaskSession 事实；项目和共同入口共用 `kernel_session_recovery.restore_task_session`。
- Research 观察在同一工具收据 canonical 内；`ledger_receipts` 恢复完整正文、页和证据。整批暂存，成功才发布，不建立第二持久日志。
- auto 工具 ACTION 和拒绝完成的回答均继续同一窗口/会话/预算；模型标签不改变原授权或要求。
- native auto 首调用经 `prepare_auto_provider` 记录原策略和发送 effect，使用授权的
  `TurnSnapshot`。`InitialNativeTurn` 连同回复、快照及 prompt 交给同一 kernel，
  首轮计入原预算，不二次生成；Auto/普通启动共用 `kernel_preparation`，收到首轮后
  不再次刷新上下文或渲染 prompt，并校验其 owner_session 是同一 TaskSession。
  完整直接回答仍经过共同完成门。网页文本 ACTION
  路径保留，普通问候不抢写锁，编辑调用收到后取得写锁才执行，失败关闭原 call IDs。
- command/cwd/URL 是完整身份；展示边界可以裁剪，事实和完成关联不裁剪。

可执行检查与准确的证明边界见 [kernel_invariants.zh-CN.md](kernel_invariants.zh-CN.md)。
