**Codey API 传输与协议窗口生命周期：完整 TDD 修改方案**

> **实施状态（2026-10-13）**：本轮完成确定性传输取消、响应收尾、ResumeRun 窗口复用、durable approval、DeliveryCommit witness 和 release gate scope 修复，并通过最终全量 pytest **8506 passed、30 skipped、1508 subtests**。Ruff、Pyrefly、machine contract 通过；严格 mypy 仍受仓库既有 tools 基线错误阻塞。第 1–12 批的完整架构迁移仍以本文后续验收项为准，不能据此宣称全部完成；证据见 `TEST_REPORT.md`。

设计基线：`6f4671e9`。本文件同时作为实施方案、TDD 验收基线和后续维护约束；其中的新类型、测试、默认值和错误码已经按实施结果落地，现有代码中的名称仍会明确标为现有入口。

目标：修复连接所有权及收尾问题；让 Local 和 Zen 的工具审批续接直接产生回答；严格恢复原协议窗口；保持 Zen 可删除；用实际行为证明修改成立。

**0．范围、约束与实施纪律**

包含：共享 API 传输、请求策略、Zen 资源生命周期、协议窗口、任务输入、Shell 审批和结果投递、相关客户端及回归。

保留：CodeyRequests 的展示职责、ledger/checkpoint/compaction 的现有分工、现有运行日志和效果记录、连接器注册与延迟加载。没有需求支持新任务队列、插件框架、通用工作流引擎或上下文总管。

冷启动假设用于删除旧的内部接口与字段组合，不用于删除本机历史数据。旧磁盘记录缺少新窗口身份时，禁止推测身份后恢复原生调用；已有可见对话仍可查看。本轮不实现自动历史升级或跨模型移交。

一个小周期：写行为测试 → 单独运行确认因目标缺陷失败 → 最小生产修改 → 同一测试通过 → 邻近回归通过 → 重构 → 再次通过。一个验收批次可以由多个小周期组成。

不能把 import/collection 错误当业务测红，不能添加 xfail、skip、放宽断言或生产测试类型判断来取得绿色。已经正确的行为可以先建立绿色约束测试，不必人为破坏它制造红色。

缺陷复现先使用现有生产接口；迁移接口时可以改测试装配，但必须保留原行为断言。测试辅助代码可以先完成，不能预先完成生产修复再补测试。

每次测红记录：基线 SHA、测试 nodeid、命令、退出码、关键断言。修绿记录：同一 nodeid、命令、结果、邻近回归。实现结束交付证据；本设计文档不冒充这些证据。

**1．必须成立的不变量**

| 编号 | 不变量 | 可观察证据 |
|---|---|---|
| T1 | 每次借用连接产生独立租约；已交出的旧租约不能操作新租约的连接 | A 归还、B 借用、A Stop，B 正常回答 |
| T2 | 取消和归还竞争只允许一个拥有者取得物理关闭或归还权 | 固定两种竞争顺序，无串扰、重复归还或泄漏 |
| T3 | 最后一个 session 引用释放后，池永久关闭 | 迟到归还直接关闭，retain/acquire 不复活 |
| T4 | 代理认证不会作为源站请求头发送 | CONNECT 有认证；HTTPS 源站没有该认证 |
| T5 | 新冷却不能缩短旧边界；旧成功不能清除新版本 | 长短限流和旧试探成功交错 |
| T6 | 重试只由策略决定；所有实际尝试受同一预算约束 | TLS EOF、429、明确过载混合序列有准确次数 |
| T7 | 任意失败都释放独立可释放资源；内部清理失败明确可见 | 锁、登记、试探、连接及错误均可观察 |
| W1 | session_id、run_id、window_id 是不同身份 | 新 run 不改变窗口；reset 改变 window_id |
| W2 | 一个聊天只有一个当前主协议窗口 | 切换后不能从旧模型目录恢复旧调用 |
| W3 | 原生工具结果只能投递到产生调用的窗口 | window_ref 和 pending call 验证失败时 HTTP 为零 |
| W4 | 有未完成调用或发送状态未知时，禁止自动重开 | reset、模式切换、模型切换均拒绝 |
| W5 | 重启恢复使用持久化窗口 ID；压缩保留 ID 和开放调用 | 销毁对象后恢复、跨压缩续接 |
| W6 | 命令执行和结果投递是不同效果 | 已执行结果的恢复只投递，不再执行命令 |
| W7 | 不确定发送不能自动重发 | 服务端收到请求后断连，后续自动尝试为零 |
| W8 | ledger 已提交而回执缺失时可以对账 | 崩溃重启补回执，HTTP 和 Shell 都不重跑 |
| Z1 | 公共层不依赖 Zen 包 | 隔离子进程移除注册并阻止导入，完整 Local 续接通过 |

这里的“恰好一次”仅限本地领取和本地可验证投递记录。网络请求发送与远端生成之间不存在本地可实现的全局原子提交；未知状态必须保留。

**2．目标职责与文件落点**

| 文件 | 修改后的责任 |
|---|---|
| codey/providers/api_http.py | session 引用、关闭状态、空闲连接边界、每次借用的租约 |
| codey/providers/api_transport.py | 一次实际传输、写前状态、读字节/帧、代理建连、租约取消 |
| codey/providers/api_request_policy.py | 通用尝试预算、重试指令、请求登记及资源凭证 |
| codey/providers/api_provider.py | 请求准备、实际尝试调度、协议提交、取消/重置/关闭、窗口绑定 |
| codey/providers/api_codec.py、api_chat.py、api_responses.py | 协议编码解码、开放调用查询、批次配对校验 |
| codey/providers/context_ledger.py | 一个聊天的主窗口身份、协议视图、原子提交及投递提交凭证 |
| codey/providers/zen/reliability.py | Zen 冷却、错误分类、排队政策、coordinator 与 session 注册表 |
| codey/providers/zen/connection.py | 合作绑定、声明转换、公共生命周期接口的明确转发 |
| codey/runtime/core/protocol_window.py（新增） | 无 I/O、无连接器导入的窗口/绑定/调用归属值类型 |
| codey/task/model.py | 明确任务输入、公共提交选项；无原始 Shell 结果字段 |
| codey/operations/conversation_plan.py | 纯规划：复用还是打开、原因和明确的上下文种子 |
| codey/operations/conversation_window.py（新增） | 主窗口激活、严格恢复、reset 和弃置的唯一协调入口 |
| codey/runtime/effects/shell_approval_records.py（新增） | 接入现有 runtime log 的审批事实与投影；不另建数据库 |
| codey/runtime/effects/tool_result_delivery.py | 同窗口批次投递、已有 send_attempt/delivered 回执和对账 |
| codey/app/approval_registry.py | 审批投影缓存与 UI 查询；不再是唯一事实源 |
| codey/app/shell_service.py | 领取裁决、执行一次、保存结果；不拼原生续接伪用户任务 |
| codey/app/api.py、task_submit.py | 严格解析/提交明确输入，错误投影；不复制 Shell 结果字典 |
| codey/operations/task_run.py、task_entry.py、task_phases/dispatch.py | 恢复来源、激活窗口后构造运行 frame；不修改意图布尔值 |
| codey/operations/kernel_recovery.py、kernel_transport.py、task_loop.py | 按已记录调用来源投递；不根据 ID 是否为空猜协议 |

新增模块是现有职责的具体实现，不引入全局 WindowManager、WorkflowEngine 或第二套运行状态机。

**3．传输修改：连接租约必须覆盖已取得快照的取消**

现状：AttemptControl 保存裸连接和 response；_PooledResponse.close 把连接放回池；旧 control 尚未 finish。因此取消即使稍后清除列表，已经取得的旧快照仍可能关闭连接。

设计：池中存放空闲物理连接；每次 acquire 都创建新 ConnectionLease。AttemptControl 在池化路径只保存该次租约的取消能力，不保存裸连接，也不保存可绕过租约关闭连接的 response。

ConnectionLease 的内部字段：

- lease_id：进程内唯一借用标识。
- session、key：归还目标。
- connection：只在租约仍持有资源时非空。
- response：取得 HTTP response 后由本租约绑定。
- state：ACTIVE、RETURNING、CANCELLING、RELEASED、CLOSED。
- lock：只保护状态和资源指针；网络 I/O 不在该锁内等待。

状态竞争：

1. 正常 close 完成帧判断，在租约锁内把 ACTIVE 改为 RETURNING，取走 connection/response 指针。取消此后看到 RETURNING，只能放弃对该租约的物理关闭权。
2. 关闭旧 HTTPResponse 缓冲对象，完成后才把物理连接归还 session。session 若已关闭则销毁连接。最终状态 RELEASED 或 CLOSED。
3. 取消若先把 ACTIVE 改为 CANCELLING，则取走资源指针，先 shutdown socket，再 close response 和 connection；正常归还不得发生。
4. 正常 response 关闭发生异常、响应未完整、服务端要求 Connection: close、取消或超时，全部走销毁路径。
5. 归还不是从租约把 connection 暴露给别的所有者后再 close response；必须先完成旧 response 清理再入池。

已取得 cancel 快照的线程最终调用 lease.cancel()，仍会重新检查租约状态。旧快照不包含裸连接，无法越过状态检查。

abort_response 在池化 response 上调用明确的 abort 能力，由租约做关闭；不得先通过 _socket_for(old_response) 找出可能已被新租约使用的 socket 再 shutdown。_PooledResponse.close/abort/sock 不能保留绕过归属的通道。

非池化路径也包装为本次连接拥有者，支持 connect 阶段注册和关闭；不为池化保留一套不同的取消语义。裸 socket 的低层关闭仅在拥有者内部。

**3.1．session 引用与关闭**

ApiHttpSession 增加 _closed。retain 在关闭后报 SessionClosed；acquire 在关闭后不提供连接；release 到零只执行一次 OPEN→CLOSED，抽走 idle 列表，然后锁外关闭。

release_connection 在锁内判断状态：关闭时加入待销毁列表，不放入 idle。连接 close 永远不放在 session 或 registry 锁内。

引用重复 release 应暴露所有权错误，不再 max(0, refs-1) 静默掩盖。Provider.close、辅助 provider.close 和 registry detach 各自幂等，确保不会误触该错误。

acquire 先从所有 key 收集过期连接，再选择可用连接；锁外关闭过期项。release_connection 同样执行过期清理和容量约束。空组删除，不新增维护线程。

容量在正确性批次之后实现。建议初始可配置默认：每 key 3 个 idle、全 session 16 个 idle，保留 idle_ttl=60s。它们是防止无限保留的初始边界，不是性能测量结论；连接器可明确传入更合适的上限。超过上限关闭最老空闲连接；在用连接不按 idle TTL 驱逐。

**3.2．代理协议与认证隔离**

在环境代理或显式代理选定后、连接池 checkout 和任何 socket 建连前，解析代理 scheme。仅支持 http://；https://、socks:// 及未知协议明确返回 RequestPrepError/not_sent，不尝试明文回退。

错误信息不能含用户名、密码或完整有凭据的 URL。池 key 及日志不得泄露原始认证信息；代理认证的隔离标识可使用连接工厂提供的不透明身份。

HTTP 目标通过 HTTP 代理：absolute-form 请求，仅代理收到 Proxy-Authorization。

HTTPS 目标通过 HTTP 代理：先 CONNECT，认证只出现在 tunnel headers；成功后建立目标 TLS，源站 request headers 排除大小写任意的 Proxy-Authorization。TLS 主机验证针对源站。

源站 headers 和代理 headers 分别构造；不要复制同一字典后靠偶然顺序删除字段。非池化和池化都使用相同代理校验，避免另一路仍接受 HTTPS 代理。

**3.3．提交状态和统一重试**

TransportSubmission 只表达生成请求的提交状态：NOT_STARTED、WRITE_STARTED、RESPONSE_RECEIVED。

- TCP connect、代理 CONNECT、TLS 握手不把生成 POST 标记为已写入。
- 即将第一次向 socket 写入生成请求的 headers/body 前，将状态改为 WRITE_STARTED。sendall 出错时不能断言写入零字节。
- getresponse 成功后标记 RESPONSE_RECEIVED。
- 重用连接也经过同一 send 边界；不能按 request() 开始就把握手 EOF 错判成 unknown。

api_transport 一次调用只执行一次连接/生成尝试。删除非池化 _open_request_sync 中的 TLS 重连循环；移到 Provider 的统一调度。

GenerationNotSentError 携带稳定 reason_code，例如 pre_submission_tls_eof。GenerationUnknownError 携带提交阶段和 reason_code，不据异常字符串判断。

RequestPolicy 提供 attempt_limits 和 decide_retry(context, attempt_receipt, failure, budget_snapshot)。返回 RetryDirective，包括 retry、not_before、reason；策略不暗中发送请求，也不自行扣预算。

Provider 在实际开始每次 transport 尝试前唯一扣减一次预算。TLS 重连同样是一种实际尝试；禁止 Provider 预扣一次、transport 又重复扣一次。

初始设计：每逻辑请求最多 3 次实际传输尝试，最多 1 次写前 TLS EOF 重连；由策略的不可变 limits 明确提供，删除 isinstance(LocalRequestPolicy) 分支。需要更大预算只能显式配置并测试，不能恢复隐藏循环。

默认安全规则：

- 明确 NOT_STARTED 的 TLS EOF，剩余预算和 deadline 允许时，最多重连一次。
- 证书错误不重试；普通连接拒绝不自动重试；WRITE_STARTED 后 EOF、reset、超时不自动重试。
- Zen 的已确认短窗口 429 继续由 Zen 分类器处理，不扩大白名单。
- 通用 503 不足以证明安全重试。Local 默认也不凭 status=503 自动重试；有可验证的完整过载拒绝合同才由连接器策略放行。这是明确的行为收紧，需要文档及测试，不能宣称完整保留旧 503 行为。
- 不完整/超限错误正文不能分类成完整拒绝。有限读取完整错误 body，再提取有界诊断摘要；达到错误正文上限而未完成帧则 unknown，不重试。建议 MAX_ERROR_BODY_BYTES=64KiB，诊断摘要保持小上限。
- HTTP redirect 不自动跟随生成 POST。

Local 和 Zen 可以调用共享的写前 EOF 判断，但合作错误名称不能出现在公共策略。

登记只发生一次；每次尝试取得容量 lease、读完释放容量，backoff 不占容量。pending 登记与试探资格必须持有到解码和协议提交/失败结算结束。

**4．Zen 冷却、注册表与公平等待**

publish_cooldown 在 condition 锁内读取旧边界：until=max(old_until, new_until)，版本每次发布仍增加。若旧期限更晚，保留说明该期限的原因；另外的发布信息只做诊断，不能改变有效边界。

试探凭证使用 (registration_token, cooldown_version)，不使用可复用的 id(pending)。settle 只释放自己持有的试探；成功只清除同版本冷却。最后 pending 释放还必须清除自己遗留的试探，不能释放别人的新试探。

把 coordinator.observe_success 与 policy.observe_success 的重复结算删除，生产只保留 policy.settle(context, receipt, outcome)。成功、拒绝、取消、unknown、解码失败等结果在这个入口结算；可选日志观察另行执行。

registry 的 coordinator 与 session 分别持有：

1. get_or_create 增加使用者计数；有保留 coordinator 而 session 缺失时，创建新 session。
2. session 初始一个 registry 引用；每个 runtime/auxiliary retain 各自引用。
3. 最后 provider 使用者离开时，无条件从 _sessions 删除 session；在 registry 锁外 release registry 引用。
4. 需要保留冷却或等待试探时，coordinator 保留且使用者计数为零；这不构成 session 拥有权。
5. 下次创建 provider 复用 coordinator，但取得全新的 session。
6. close 顺序允许 provider 引用和 registry 引用先后释放；迟到归还受到 session._closed 保护。

registry 创建失败也要对称释放：取得 coordinator/session 后若 ApiProvider 构造或包装失败，释放已经取得的引用。不能只覆盖成功创建后的 close。

FIFO 在正确性修复之后完成：每次 acquire 进入有顺序的 waiter 队列；选择最早“当前可准入”的 waiter，同模型不能插队；某个模型冷却不阻塞其他已可准入模型。排队上限仍按逻辑 pending 计数，重试不会增加 pending。

wait 时间取取消检查上限、剩余 deadline、未来 cooldown 边界的最小值；cooldown 已到期但 probe 被占用时，等待 condition 通知或取消检查周期，不退化到 1ms。取消、释放容量、发布冷却、结算试探都 notify_all。

保留现有 30 秒 Zen body_idle_timeout 必须标为待实机确认的政策；离线测试只能证明按实际字节活动计算静默，不能证明长推理一定兼容。没有实机证据，不报告“无感更顺滑”。

**5．_exchange 异常安全与取消/提交竞争**

在尝试开始时建立一个 ExchangeResources，保存本次 registration、容量 lease、control 和 outcome；不要跨请求复用 _request_context/_request_receipt 的模糊生命周期。对象是本次交换的资源集合，不是新工作流。

_exchange 的具体阶段：

1. 非阻塞取得 _send_lock；失败立即 busy。
2. 在状态锁下取得 generation、history_revision、window_ref 和候选历史；做纯配对验证。
3. 进行准入/预算/网络尝试；每次容量 lease 在尝试 finally 释放。
4. 解码；按原有规则保留已接受的合法输入，不能把坏回答作为有效输出提交。
5. 窗口与 generation 核对，领取提交权，写 ledger，再更新内存。
6. 生成明确 outcome；返回回答或原始失败。
7. 所有路径执行一次 correctness settle；再无条件释放 registration 的结构性资源；再清除活跃 control；再释放 _send_lock。
8. 已提交后的可选 usage/诊断/maintenance 单独观察，不能抛回普通发送失败路径。

清理用逐层 finally 或等价的资源栈。先捕获主异常，再按必要资源逐项清理；一个清理错误不阻止后续清理。保留主异常，并记录清理异常链。没有主异常而 correctness settle 失败时，报告明确“协议已提交但本地结算异常”，不能伪装 not_sent，也不能重发。

registration 的核心 detach 在 coordinator condition 锁内只做内部集合/凭证变更，不执行外部 callback；再运行可能失败的关闭通知。测试的 pending 关闭故障应注入真实的可失败通知/释放步骤，确认结构性 detach 和其他清理已执行。

若把整个核心 unregister 替换为“什么也不做只抛错”，不可能同时保证已注销。此类不变量故障应明确标记 cleanup_failed，阻止复用损坏 coordinator，并确保发送锁/连接等独立资源仍释放；不能假装全部资源已释放。测试区分这两类失败，不建立不可能兑现的契约。

取消与提交必须有明确定序：在一个短状态锁下，ACTIVE 可以变成 CANCELLED，或被提交路径领取为 COMMITTING。取消先赢，后续不能写入回答；提交先赢，取消不能撤销该提交，也不能把其结果报告为未发送。磁盘失败不更新内存，提交失败明确保留发送事实。

transport shutdown 立即执行，不等待 registry、ledger 磁盘 I/O 或 maintenance。不要为了“原子”把慢网络读取塞进状态锁。

**6．窗口身份与单一持久化权威**

新增 runtime/core/protocol_window.py，类型仅用标准库：

```python
@dataclass(frozen=True)
class ProviderBinding:
    connection_id: str
    connection_revision: str
    model_id: str
    protocol: str
    history_contract_id: str

@dataclass(frozen=True)
class ProtocolWindowRef:
    session_id: str
    binding: ProviderBinding
    window_id: str
```

history_contract_id 是规范化非秘密配置的稳定摘要：至少覆盖 endpoint、原生/文本解释方式、声明名称转换及协议合同版本、影响历史解释的 system 配置。认证/账户若影响会话归属，使用本地不透明身份标识，不能把 API key 写入记录。connection_revision 和合作版本由连接器提供，核心不解释。

generation 用于进程内取消提交；history_revision 用于当前视图 CAS；window_id 用于跨进程归属。三者不可互相代替。

窗口 ID 在第一次打开和明确重开时创建；恢复和压缩沿用。同一个 session_id + model_id 也可能经历多个不同窗口。

当前 ledger 按模型 identity 分目录，不能作为“当前主窗口”的唯一权威：切换模型后，旧目录仍可能有开放调用。目标使用一个稳定的聊天级主 ledger 路径：

```text
context_history/<session_key>/protocol/
    head.json
    000000000001.json
    ...
```

模型 binding 是窗口元数据，不再决定主 ledger 的存储路径。辅助总结、review、探针使用隔离 runtime/ledger，不写这个主 head。

冷启动的新格式从 schema_version=1 开始，不预设第二版或升级迁移。head 保存 schema_version=1、session_id、window_ref、sequence、chain、view、view_digest。journal 每个事务携带 window_ref 和 view_digest；打开/重开事务另携带 window_open_reason 和可验证的 seed 引用。身份和视图摘要必须被 journal digest 覆盖，head 与最后已提交事务的身份/摘要一致。

ContextLedger 关键变更：

- for_session(home, session_id) 取得稳定路径；去掉由当前 runtime 身份选择根目录的行为。
- restore(expected_ref) 验证当前 head；expected_ref 不等于当前 ref 则 WindowMismatch，不扫描旧目录找“能配上”的窗口。
- commit_exchange(expected_ref, expected_revision, view, events, checkpoints, delivery_commit) 完成一个协议事务。
- open_window(binding, reason, seed, expected_ref, reset_permit) 在同一 journal/head 提交新 ref 与新视图。
- 原 commit 的写 segment → 原子替换 head → 更新内存顺序保留，新增身份随同事务提交。
- head 写失败时，旧 ref/旧 view/旧 revision 保留；孤立未提交 segment 不能被当成已接受历史。
- 原 portable_state 按新 journal 的明确窗口边界取得语义摘要，不再遍历旧模型目录推断“最新窗口”。

旧数据不被删掉或自动猜测恢复。UI 可见历史沿用现有展示源。确实要迁移旧协议记录，应以后做独立显式工具，不能在 bind_context 里加旧字段猜测 fallback。

**6.1．开放调用与批次**

codec 新增 pending_calls(view) 查询，返回结构化 PendingCall，不制造工具结果：call_id、tool_name、arguments_digest、所在原生批次及顺序。codec 验证同批所有结果齐全、无重复、不引用未知调用。

必须检查当前开放批次而不是“历史里曾经出现过 ID”。重复 ID、重复回答、跨 window ID 相同也均拒绝。保留现有 prepare/validate_view 校验作为最后边界。

原生调用缺 ID 立即协议错误。文本调用在解码入口就得到 TextInvocation 来源；不能因为缺 ID 而把原生调用改判文本。Zen 若明确支持文本请求约定，由 Zen 文本适配器提供来源；公共层不猜 ID。

```python
@dataclass(frozen=True)
class NativeInvocation:
    window_ref: ProtocolWindowRef
    call_id: str
    batch_id: str

@dataclass(frozen=True)
class TextInvocation:
    window_ref: ProtocolWindowRef
    batch_id: str
    tool_index: int
```

来源随 ToolEffect/审批事实保存。一个投递批次必须来源一致。supports_native_tools 是能力，不代表任意解析出的 JSON 动作自动成为原生调用。

**7．任务输入与运行 frame**

task/model.py 改为：

```python
@dataclass(frozen=True)
class StartTask:
    text: str

@dataclass(frozen=True)
class ResumeRun:
    source_run_id: str
    instruction: str = ""

@dataclass(frozen=True)
class ResolveShellApproval:
    approval_id: str

TaskInput = StartTask | ResumeRun | ResolveShellApproval
```

TaskSubmission 保留 session_id、project、max_turns、intent、run_id、授权相关选项和 review_source_run_id；用 input: TaskInput 替换 task/continue_task/previous_run_id/initial_shell_results 的组合。普通任务可带已解析模型选择；ResumeRun 和 ResolveShellApproval 的原选择从源记录读取，客户端不得覆盖。

ResumeRun 的 source_run_id 明确存在并属于该聊天；恢复事实不隐含重开。新 run 的策略和授权从源 run 重建，不能通过 instruction 偷加能力。

ResolveShellApproval 不携带命令、结果、call_id、provider 或任意窗口 ref。入口从审批事实加载这些值，窗口精确恢复失败直接报错。

ResetWindow 保留为单独控制操作，至少携带 expected_ref。普通 reset 拒绝开放调用与 unknown send。明确放弃是另一个授权操作：先持久化调用/审批/未投递批次弃置，再形成绑定 expected_ref 的 reset permit，才允许新窗口。禁止伪造 tool output 关闭旧调用。

HTTP 普通提交体使用明确 input.kind=start_task/resume_run。审批 HTTP 仍提交 approval_id 和 approved，随后内部只提交 ResolveShellApproval。未知 kind、缺字段、旧 continue_task、跨类型结果字段应在入口明确拒绝。

CLI 用 --resume-run <id> 表达恢复，删除仅凭 --continue + session 猜来源的内部语义；headless 使用同一输入类型；UI Continue 传入可验证 source_run_id。server.py 包装函数签名同步迁移。

恢复得到内部 ResolvedTask：original_task_text、source_run_id、source_selection、entry_policy、ready_result_refs、invocation_origin。这个派生对象不是客户端可构造的另一个输入。

RunFrame 删除 fresh_chat 和 provider_session_changed；带 activated_window 和 ResolvedTask。进入 mode adapter 前已经完成窗口激活；adapter 不能再次 new_chat。

ProviderRegistry 的进程内缓存以 (session_id, window_id, role) 关联 runtime；不能把同一个 ApiProvider 实例同时 bind 给两个聊天。已有 provider_id→session_id 布尔比较删除，缓存 ref 只能加速查询，磁盘 head 才是当前主窗口权威。生命周期 gate 按 session_id 定位；同一聊天切换 binding 也使用同一 gate，不能按 model_id 分锁。

task_run.py 删除恢复后 replace(request, continue_task=True) 和 _shell_redelivery_rows(initial_shell_results)；恢复来自明确输入与持久记录。Ghost 的恢复请求也提交 ResumeRun，不能偷偷设置旧布尔字段。

**8．规划与激活的具体顺序**

ConversationPlan 使用一个 WindowAction：

```python
@dataclass(frozen=True)
class ReuseWindow:
    ref: ProtocolWindowRef

@dataclass(frozen=True)
class OpenWindow:
    reason: WindowOpenReason
    seed: WindowSeed | None = None
```

WindowOpenReason 是枚举：FIRST_OPEN、USER_RESET、MODEL_CHANGE、CONTEXT_ROLLOVER 等合法原因。不可用窗口在恢复任务中是错误，不是 OpenWindow 的兜底理由。

WindowSeed 只包含可移植语义资料引用、source_window_id 和明确原因；不装旧 function_call/function_call_output 或任意原始协议项目。当前普通跨模型摘要流程若保留，必须只用于没有未完成调用的明确打开动作。

conversation_plan 只读 snapshot，不调用 conversation.clear、不进行 new_chat、不提交 ledger。现有 ConversationContext.plan_request 中模式变化时 clear 的副作用需要移出。

普通任务激活：

1. 严格解析输入，保留原授权决策。
2. 按普通模型选择连接；必要启动 failover 只能发生在没有已有待投递原调用时。
3. 取得聊天级生命周期 gate，读取主 ledger 当前 ref。
4. 加载 current view、pending calls、未结算投递状态。
5. 规划 ReuseWindow 或 OpenWindow；有开放调用时拒绝自动重开、模式切换和模型切换。
6. 执行动作，完成持久化，更新 ProviderRegistry 缓存与 ConversationContext 展示快照。
7. 得到 ActivatedWindow，交给 frame/内核；结束后释放 gate。

审批续接激活：

1. 读取已裁决审批和持久结果。
2. 原模型/连接 selection 直接从原 run 读取；preflight 禁止切换，包括没有 model_selection 字典的情况。
3. 生命周期 gate 下 restore_exact(original_ref)。
4. 验证调用仍待回答、来源匹配、批次完整，或发现已提交 witness 并进入对账。
5. 构造原生结果或既定文本协议结果，发送并提交。
6. 完成回执和 UI 更新；续接运行允许有新的 run_id，window_id 不变。

gate 是现有 registry/运行协调中的 session 级互斥能力，无网络锁外重置窗口的通道。阻止 reset 与发送并发，但 Stop 使用取消能力，不等待 gate。

ApiProvider 方法拆为 abort_inflight、reset_window、close：

- abort_inflight：取消当前传输并参与提交权竞争，不清空 accepted history，不写空 ledger。
- reset_window：核对 expected_ref/reset permit，先提交新窗口再更新内存；不能自己决定丢弃开放调用。
- close：取消未提交传输、释放 session 引用；保留 accepted history 和持久 ref。

bind_context 只恢复已存在的主窗口或由明确 FIRST_OPEN 创建；切换 session 时先取消/释放旧运行资源，不清空旧聊天的 ledger。

Zen 包装层明确转发这些公共方法、窗口引用和绑定，不能通过 __getattr__ 造成 wrapper/runtime 身份不同。Browser/text provider 使用对应窗口适配能力；不具备可验证跨进程恢复能力时明确 WindowUnavailable，不宣称浏览器远端历史也能恢复。

task_entry、project_adapter、planning_flow 和主 writer 只接收 ActivatedWindow。research/review/probe 的独立辅助会话仍可打开自己的窗口，但不能修改用户主窗口 head。

ApiProvider 和 ZenProvider 的旧 new_chat/abandon_inflight 入口在迁移完成后删除，不保留别名。使用 API 辅助 provider 的 review_service、sibling_probe、research_iteration 等调用点同步改为隔离窗口激活。浏览器后端真正的远端 new_chat 可以由对应窗口适配器调用；它不应变成共享 API 的兼容入口。ConversationProvider.new_chat/fresh_window 这条重复 begin_window 路径一并清理。

**9．审批事实：执行只领取一次，结果不随 pop 消失**

继续使用 RuntimeSessionLog.mutate，不修改外层四类日志 entry kind。新增审批事实放在 operation_effect payload 内，声明 effect_kind=shell_approval；使用 schema 和投影校验。

审批事实至少记录：approval_id、session_id、source_run_id、invocation_origin、原 selection 引用、原 policy/授权引用、command_ref/command_digest、cwd、执行效果 ID、裁决、result_ref/result_digest、关联 delivery_batch_id。

命令和 stdout 等正文使用现有 ManagedOutputStore 保存有界 artifact；runtime log 只保存引用、摘要和状态，遵守现有 forbidden raw payload 规则。

现有 write_tool_output 是展示输出接口，会裁剪 metadata.command，并可能因存储政策返回 None，不能拿它的 command 元数据恢复可执行命令。给 ManagedOutputStore 增加严格的 write_json_artifact/read_json_artifact 能力：原子写 payload 和元数据、返回有 scope/digest 的引用、存储失败抛明确异常。审批 command payload 保存完整已校验命令，超限在申请入口拒绝，不截断后执行；执行 result payload 保存 exit_code、status、ok、output、truncated 等语义字段。引用摘要覆盖规范化 JSON（排序 key、禁 NaN、UTF-8），不把展示时间戳纳入 results_digest。恢复读取校验文件大小、schema、scope 和 digest。

结果 artifact 的保留/清理策略必须保护未投递、unknown 和未决审批的引用；不能因 source run 已结束便删除结果。保存原始正文与日志记录引用之间没有跨文件原子提交：先写 artifact，再提交引用，失败留下的孤立 artifact 不代表已执行或已投递，后续可按明确无引用证据清理。

合法事实类别：requested、approved/denied、execution_claimed、execution_settled、abandoned。投递事实仍使用原 tool_result_delivery，不在审批里另存一套 delivered 布尔值。

UI waiting_approval、executing、result_ready、continuation_failed、completed 从这些事实和现有 OperationState 投影。仅在现有 leaf 无法明确表达下一步合法动作时扩展相应 leaf；不增加平行总状态机。

申请：先持久化 command artifact，再原子提交 requested + 原调用归属，最后发布审批 UI。记录失败则不显示可执行卡片，不执行命令。

批准：沿用现有 Stop/spawn gate 次序。原子领取裁决及 execution_claimed，持久化成功后才能 Popen。不要 pop 后失去唯一记录。重复同一裁决返回已有状态；不同裁决冲突报错；执行者只产生一个。

拒绝：不执行 Shell，持久化 denied 及拒绝结果，然后在同一窗口回答原调用。

执行完成：先持久化有界结果 artifact，再提交 execution_settled、result_ref 和待投递 batch；成功后才能调度 ResolveShellApproval。写入失败不报告执行未发生，也不重新执行，明确 local_settlement_failed。

重启：

- requested：恢复审批卡。
- denied：恢复拒绝结果，投递待回答调用。
- execution_settled：读取结果，只恢复投递。
- execution_claimed 但无 settlement：execution_unknown，禁止自动重跑。不能仅凭“没看到完成记录”认定命令没执行。
- abandoned：拒绝任何过期批准/续接。

Stop 与批准竞争保留现有 epoch 和 spawn gate 保证，同时记录 durable abandon。Stop 后不自动续接；已经完成的执行事实仍保留供明确恢复或查看。

**10．投递事务及两个持久化系统之间的对账**

现有 send_attempt/delivered 回执继续使用。新增字段 window_ref、invocation_kind、results_digest，并扩展严格 payload 白名单及摘要校验。

投递步骤：

1. 加载结果 artifacts，校验 digest、session、来源 selection 和原调用。
2. 在 window gate 下检查当前 pending batch 完整、无重复。
3. 完成纯编码和本地配对验证；失败时 HTTP=0，结果仍在。
4. 通过 RuntimeSessionLog.mutate 原子记录唯一 live send_attempt，并绑定 provider_effect_id。
5. 实际 HTTP 请求；写前失败用已有 send_superseded 表达可验证 not_sent；unknown 保持 live attempt 并阻止自动重发。
6. Provider 提交协议事务时，同事务写入 DeliveryCommit：window_ref、batch_id、provider_effect_id、results_digest、输入/输出摘要、reply_status。
7. ledger head 提交成功后更新内存，之后写 runtime delivered 回执。
8. 发布模型回答及 UI 终态。

DeliveryCommit 是“本地协议事务已接受此批结果”的证据，不是网络恰好一次执行证明。

对账规则：

- ledger 有同 ref/batch/effect/digest 的有效 commit，runtime 无 delivered：补 runtime 回执，不重发、不重跑 Shell。
- 有 send_attempt、无 commit，且没有确切 not_sent：unknown，阻止自动续接。
- 有完整有效响应但 ledger 写失败：generation 已发生但本地接受失败；不能把它当作 not_sent 重发，保留明确失败及执行结果。
- runtime delivered 存在而对应 ledger commit 丢失或不匹配：一致性错误，不能自动补造调用或重新发送。
- 已接受输入而输出解码无效：保留原合法输入事务及其提交证据，标记 reply_unavailable；不要重新发送相同工具结果。最终回答未完成的事实独立记录。

对账修改复用现有 recovery 与 delivery_projection，不建立新对账数据库。协议 journal 中的窗口事件和 commit witness 不能被普通压缩删除。

**11．严格错误及 UI 表达**

稳定错误码建议：window_unavailable、window_mismatch、binding_mismatch、pending_call_missing、duplicate_result、incomplete_result_batch、unsupported_proxy_scheme、delivery_unknown、execution_unknown、local_settlement_failed、cleanup_failed。

错误包含 session/run/window/approval 的非秘密关联 ID、发送状态和是否允许明确恢复。错误文字不参与控制逻辑。

命令成功但续接失败：“命令已执行，回答续接失败”；本地配对失败标明未发送；unknown 标明发送结果不确定；不把全部显示成 provider failed。

UI 已显示执行输出，不代表审批投递完成。只有执行 settlement 和 delivery projection 成立才能显示对应完成阶段。Local update paused 继续属于可选本地更新观察，不得遮盖正确性错误，也不扩大为本轮 Ghost 重构。

原生续接不拼长伪用户消息；工具结果由结构化协议发送。用户新补充指令是明确 instruction，在结果批次被合法处理后作为下一条用户输入，不与旧结果混写或偷换来源。

**12．测试装配与允许替换的边界**

新增测试支持：tests/support/api_scripted_service.py（HTTP/HTTPS/CONNECT 脚本服务），tests/support/task_lifecycle_harness.py（临时 state、真实 task/审批/ledger/内核装配）。这些 helper 不被生产导入。

端到端测试使用真实 ApiProvider、codec、任务解析、conversation plan/window activation、审批记录、RuntimeSessionLog、ManagedOutputStore、delivery 回执、内核。

只替换远端 HTTP 服务、Shell 执行边界；需要可控任务调度时，测试 scheduler 仅排队/运行真实 run_task，不能绕过 task_submit、dispatch 或 kernel。重启场景必须销毁整个 app/provider/cache 并从 tmp_path 重建；至少一个子进程恢复测试，不能仅把变量换名。

真实 socket 用本地 127.0.0.1 动态端口。TLS 测试使用测试 CA/证书，正常验证链和 hostname；不要以全局关闭验证证明 TLS 正确。

并发使用 Event/Barrier 固定顺序，sleep 只能作为超时保护，不作为排列事件的手段。全部 worker 在 finally 放行并 join；服务端和 session 关闭。超时断言通常采用 1 秒取消窗口，需结合 CI 留余量；不以昂贵机器的绝对毫秒指标宣称性能。

时间计算单测注入时钟或 monkeypatch 模块 monotonic，不等待真实 60 秒。取消延迟、连接复用、CONNECT 路径仍用真实 socket。

fake response 正确实现 read/read1/headers/status/close/context manager；fake transport 明确接受新 kwargs。不得让 MagicMock 的任意属性自动行为变成生产兼容需求。

**13．按顺序执行的 TDD 批次**

每批先提交对应红测试并保存证据，再实现至绿色。实现提交前该批及邻近回归必须通过；临时红提交不能进入发布分支。最终可按两个领域审查：传输资源修复、窗口/审批生命周期重构。

**批次 0：建立基线和先钉住用户可见缺陷**

先运行现有相关回归、完整 lint/typecheck/test，记录已有失败、skip 和平台限制，不把旧失败算到新修复里。

新增 tests/test_shell_continuation_lifecycle_e2e.py：

- test_local_approval_success_replies_without_extra_user_message，参数化两种协议、chat/project。使用现有生产入口，让模型首先发出 Shell 调用、审批执行、自动续接、最终回答；断言无需“然后呢”，且续接 wire payload 回答原 call_id。项目路径即使文本 fallback 能得到回答，原生投递断言仍必须捕捉这个错误。
- test_planned_resume_keeps_pending_call_history：更小的定位测试，真实 build_conversation_plan → _open_fresh_session（若计划要求）→ 真实 ApiProvider.send_tool_results；断言续接调用合法。
- test_denied_approval_answers_original_call：HTTP/执行计数同时验证。

旧代码预期：续接 RequestPrepError，第二次生成 HTTP 未发生，最终回答缺失。第一批红必须来自该行为。此时不立刻用同步 fresh_chat 布尔值的补丁修绿；先保持红测试作为后续窗口切片验收，独立推进传输批次。

这是一个外层验收红循环，批次 1～7 是内层独立的红绿循环。此期间不能声称全量绿色；若要求每个提交全量绿，可以把外层验收测试留在未合并的测试分支，直到窗口批次实现后与实现一起合并。不得用 skip/xfail 暂时隐藏它。

**批次 1：取消与归还的原子租约**

在 tests/test_api_transport_real_stream_and_pool_lifecycle.py 增加：

- test_old_stop_after_reborrow_does_not_close_new_request：A 完整读完并归还，B 相同 connection 开始读，A Stop，放行 B；B 最终文本正确，A Stop 及时返回，连接/请求计数正确。
- test_cancel_snapshot_taken_before_return_cannot_close_next_lease：暂停 A cancel 已取目标快照后的阶段，A 归还、B 租用，再继续 cancel；B 正常。
- test_cancel_wins_before_return_discards_exactly_once：cancel 先赢，连接不能入池。
- test_return_wins_before_cancel_keeps_connection_reusable。
- test_old_response_abort_cannot_shutdown_reborrowed_socket：直接覆盖旧 response 取消通道。

红：新请求失败/Stop 等待、重复关闭或旧 response shutdown。绿：租约状态机、control 只持取消能力、PooledResponse/abort 路由。邻近回归：headers/body/connect cancellation、stream pool lifecycle。

**批次 2：池关闭与 Zen session 拥有权**

新增 tests/test_api_http_session_ownership.py；扩展 test_zen_cross_task_cooldown_and_whitelist.py：

- test_late_return_after_last_release_closes_connection。
- test_closed_session_cannot_retain_or_acquire。
- test_release_closes_idle_outside_session_lock：close callback 尝试取得 session lock。
- test_last_zen_provider_releases_idle_session_but_retains_cooldown：真实 registry+provider 引用路径，关闭后 socket 已关闭。
- test_next_provider_reuses_coordinator_with_new_session。
- test_registry_releases_session_outside_registry_lock。
- test_connection_factory_failure_releases_acquired_resources。

红：迟到连接复活、空闲连接残留、旧 session 被继续复用。绿：_closed、独立 session detach、锁外 close、构造失败对称释放。原来无断言的跨任务冷却测试用实际重新取得资源和冷却准入断言替换。

**批次 3：冷却边界与试探版本**

新增/扩展 tests/test_zen_reliability_regressions.py：

- test_short_retry_after_does_not_shorten_existing_cooldown：两个真实 policy failure 路径，固定响应顺序。
- test_old_probe_success_cannot_clear_new_cooldown。
- test_probe_remains_owned_during_decode_and_commit：HTTP 完成后暂停 decode/commit，新的同模型 probe 不可准入；pending 仍登记。
- test_probe_is_released_after_failure_and_pending_cleanup。

红：until 变短、错误版本清除、提前释放。绿：max、版本、registration token；通过唯一 policy.settle 测试，不直调用另一个测试专用成功入口。

**批次 4：代理协议与 CONNECT 隔离**

新增 tests/test_api_proxy_contract.py：

- test_https_proxy_is_rejected_before_any_socket，参数化池化/非池化、显式/环境代理。
- test_unknown_proxy_scheme_is_rejected_not_sent。
- test_http_connect_auth_is_not_forwarded_to_https_origin：真实 CONNECT 代理转发字节，源站真实 TLS，认证仅在 CONNECT。
- test_http_forward_proxy_auth_remains_on_proxy_path。
- test_proxy_errors_do_not_contain_credentials。

红：HTTPS 代理收到明文/连接被建立、源站收到代理头。绿：统一校验、两套 headers、保持 HTTP CONNECT。不能用现有 HTTP→HTTP 代理测试替代这一批。

**批次 5：预算、写前 EOF 和单一重试调度**

扩展 tests/test_api_transport_tls_handshake_retries_only_before_http_submission.py、test_api_transport_retry_budget_and_idle_timeout.py：

- test_pooled_pre_submission_tls_eof_reconnects_once：第一次真实调用路径注入握手 EOF，第二次成功；POST 接收一次，尝试两次。
- test_nonpooled_and_pooled_have_same_submission_classification。
- test_eof_after_first_generation_write_is_unknown_and_not_replayed。
- test_certificate_error_is_not_retried。
- test_tls_eof_and_rate_rejection_share_one_budget：脚本式 EOF→已确认 429→完整明确过载→成功；用受控测试策略预算 4 时成功、预算 3 时在第三次结束，证明没有隐藏循环。
- test_local_policy_override_is_used：Local 子类 decide_retry 真正控制结果。
- test_plain_503_is_not_automatically_retried：明确记录 Local 行为收紧。
- test_backoff_holds_registration_but_not_capacity。
- test_partial_rejection_body_is_unknown_not_retryable。

红：池化只尝试一次、写前 EOF unknown、策略重写被绕过、预算次数不准确。绿：Submission 记录、transport 单次、Provider 唯一循环、策略所有决策、完整拒绝正文。

**批次 6：交换收尾异常安全**

新增 tests/test_api_exchange_exception_safety.py：

- test_settle_failure_still_releases_send_lock_registration_and_probe。
- test_pending_close_notification_failure_still_detaches_owner_and_releases_lock。
- test_decode_failure_keeps_accepted_inputs_and_releases_resources。
- test_optional_observer_failure_does_not_turn_committed_reply_into_send_failure。
- test_core_cleanup_failure_is_reported_and_coordinator_is_not_reused。
- test_abort_wins_before_commit_claim_prevents_late_commit。
- test_commit_claim_wins_before_abort_preserves_one_consistent_commit。

红：_send_lock 锁住、pending/probe 留存、已提交回答进入失败重试、磁盘/内存分歧。绿：分层 finally、结构性 detach、可选观察隔离、提交权竞争；逐项核对 outcome。

**批次 7：持久窗口身份与原子 reset**

新增 tests/test_protocol_window_persistence.py：

- test_first_open_persists_window_ref。
- test_restart_restores_exact_window_id_and_pending_calls，两种协议。
- test_reset_changes_window_id_without_deleting_old_facts。
- test_old_model_window_cannot_be_restored_after_model_switch。
- test_failed_reset_head_write_keeps_old_ref_and_view。
- test_close_and_abort_do_not_erase_accepted_history。
- test_pending_call_blocks_reset_and_automatic_rollover。
- test_compaction_preserves_window_id_and_open_call_group。
- test_wrapper_and_runtime_share_one_binding。

先用现有 close/abort/reset 路径测试历史保持，产生真实行为红；新身份 API 需要最小类型和测试接口后才能运行，不能把缺 import 当红。绿：纯类型、单聊天级 ledger、身份 journal、reset 与 abort 拆开。

**批次 8：明确输入与统一激活，修绿批次 0**

新增 tests/test_task_input_contract.py、tests/test_window_activation_contract.py：

- test_resume_requires_valid_source_run_and_cannot_override_model。
- test_approval_resume_accepts_only_approval_id。
- test_old_boolean_and_raw_results_fields_are_rejected。
- test_plan_is_read_only。
- test_chat_and_project_use_same_primary_window_activation。
- test_modes_do_not_reset_an_already_activated_window。
- test_resume_preflight_cannot_switch_provider。
- test_window_is_not_reset_between_validation_and_send：用 barrier 并发 reset，返回 busy/conflict，不跨窗口发送。

绿：迁移 HTTP/CLI/headless/内部恢复，WindowAction、ResolvedTask、ActivatedWindow。删除 continue 强制重开、重复 new_chat。批次 0 端到端测试现在必须全部绿；最终请求携带原 ID，window_id 与第一次一致。

**批次 9：审批持久化、重复裁决、重启及 Stop**

新增 tests/test_shell_approval_durable_resolution.py，扩展原 approval_epoch 和 stress Stop/Allow：

- test_success_nonzero_exit_and_denial_deliver_original_call，两种协议、chat/project。
- test_repeat_same_approval_never_executes_shell_twice。
- test_conflicting_decision_is_rejected。
- test_restart_after_execution_before_delivery_only_resends_result。
- test_restart_with_claim_without_settlement_does_not_execute_again。
- test_approval_record_failure_prevents_spawn。
- test_result_persist_failure_reports_execution_fact_and_does_not_rerun。
- test_stop_wins_before_spawn_and_prevents_auto_resume。
- test_abandoned_approval_cannot_be_resolved_after_reset。

绿：持久事实与投影、claim 原子化、结果 artifact、only approval_id 续接；不增加另一套任务总状态机。已有成功/拒绝测试迁移到完整真实路径，不保留只绕过 plan 的假成功。

**批次 10：严格投递及崩溃对账**

扩展 tests/test_restart_preserves_facts_and_pending_delivery.py、test_tool_result_delivery.py，新增 tests/test_window_bound_delivery_reconciliation.py：

- test_wrong_window_wrong_binding_missing_call_reject_before_http，HTTP=0，Shell=0，结果保留。
- test_duplicate_and_incomplete_batch_are_rejected_before_http。
- test_native_missing_id_never_becomes_text_prompt。
- test_explicit_text_origin_uses_its_text_protocol。
- test_restart_after_protocol_commit_before_delivery_receipt_reconciles_without_send。
- test_received_response_but_ledger_write_failure_is_not_retryable。
- test_unknown_network_delivery_does_not_repeat_post_or_shell。
- test_local_preparation_error_preserves_result_and_allows_explicit_corrected_resume。
- test_delivered_receipt_without_valid_protocol_commit_fails_closed。
- test_decode_failure_after_input_commit_does_not_redeliver_same_tool_result。

绿：origin、window 绑定、完整 batch、DeliveryCommit、原 recovery 对账。保留配对校验；不增加原生→文本 fallback。

**批次 11：Zen 删除边界、端到端终态和 UI**

扩展 tests/support/optional_zen_removal_probe.py 和 test_optional_zen_removal_preserves_local_api.py：

- 在子进程移除 Zen 注册并阻止 codey.providers.zen 任意导入。
- Local 两种协议完成模型调用→审批→结果→最终回答。
- 成功、非零退出码、拒绝都有最终解释。
- 原 Zen 历史展示无需 Zen import；原 Zen 未完成续接明确 connection unavailable；Local POST=0，不静默替换。
- Zen 适配另行验证声明映射后的 call_id/window_ref/digest 保留。

扩展 UI 测试：执行结果出现后还在 awaiting delivery；本地错误显示 not_sent；unknown 显示不确定；续接成功后直接出现最终回答，不卡在 Steps/Done 错误状态。

绿：公共层完全独立于 Zen，显式 unavailable，无新插件框架。原 Zen 专属白名单仍在包内。

**批次 12：清理兼容分支、重复入口与资源边界**

先把测试 fake 改为真实接口，再删生产 inspect.signature、MagicMock/read1 回退、unittest.mock 模块名判断；不得靠删除这些失败测试获得绿色。

正文读取统一 read_chunk/迭代及 read_sse；JSON 也始终按同一字节读取路径收集完整 body。删除无消费者的 read_json、require_cached、Scope、phase；确实需要阶段诊断的字段必须有明确消费者和测试，否则不保留。

新增 waiter FIFO、probe 占用等待不高频轮询、idle 上限、所有 key 过期清理的行为测试。接下来才实现这些边界；不增加后台线程或性能框架。

最后清理旧字段时以行为测试为主，rg 为辅助结构检查：

```powershell
rg -n 'continue_task|fresh_chat|provider_session_changed|initial_shell_results' codey tests
rg -n 'inspect\.signature|unittest\.mock|MagicMock' codey/providers
rg -n 'codey\.providers\.zen|provider_id\s*==\s*["\x27]zen' codey/operations codey/runtime codey/task
```

普通 recovery 的 source_run_id 仍需要，不能看到 previous/source 名称就全部删除。辅助 review/probe 自己的 new_chat 也不能机械删除；验收要求主聊天/项目路径没有重复重开。

**14．可执行命令与每批验收**

已有相关基线建议：

```powershell
python -m pytest -q tests/test_shell_approval_native_identity.py tests/test_conversation_lifecycle.py tests/test_optional_zen_removal_preserves_local_api.py tests/test_zen_cross_task_cooldown_and_whitelist.py tests/test_api_transport_tls_handshake_retries_only_before_http_submission.py
```

新增测试尚未创建。实现时每个小周期运行具体 nodeid，而非只看全量计数，例如：

```powershell
python -m pytest -q tests/test_shell_continuation_lifecycle_e2e.py -k planned_resume
python -m pytest -q tests/test_api_transport_real_stream_and_pool_lifecycle.py -k old_stop_after_reborrow
python -m pytest -q tests/test_api_exchange_exception_safety.py
python -m pytest -q tests/test_protocol_window_persistence.py
python -m pytest -q tests/test_window_bound_delivery_reconciliation.py
```

最终按仓库 CI 执行：

```powershell
python -m ruff check .
python -m mypy codey
pyrefly check codey
python -m tools.machine_contract_gate
python -m pytest -q -o faulthandler_timeout=120
```

修改 JS 时运行仓库既有 node --check 及相关 UI 测试；按 CI 的 Windows/Linux 矩阵验证关键路径。工具缺失或基线已失败如实记录，不擅自更新 baseline 或把 skip 算 pass。

只在有新失败或并发风险未解决时扩大/重复测试；不靠重复跑绿色计数替代 barrier 和崩溃场景。

**15．完成定义与交付证据**

必须同时满足：

- 两种 Local 协议在 chat/project 下的成功、失败、拒绝审批均直接产生最终回答。
- 新 run 保持原 window；reset/模型切换依法生成新 window；开放调用不能被自动丢弃。
- restart 恢复原 window/ref/results；重复裁决和未知发送不会重跑 Shell/POST。
- 协议提交和 delivery 回执之间的崩溃能对账，不补造历史。
- 原 Stop 与新连接借用完全隔离；关闭池和最后 registry session 引用真正释放资源。
- HTTPS 代理被明确拒绝；HTTP CONNECT 认证不进入源站请求。
- 冷却单调、试探版本正确、重试预算一致、清理故障可见。
- 删除 Zen 包/注册后完整 Local 续接仍成立。
- 旧模糊接口和生产测试兼容分支清理，既有静态/类型/机器合同/全量测试验收完成或限制明确列出。

交付表逐个关联缺陷 → 红 nodeid/失败原因 → 生产改动 → 绿结果 → 邻近回归。实机 Zen 长推理和总体性能如果没有测量，明确不作改善结论。

最终执行链：严格输入 → 持久事实解析 → 唯一窗口激活 → 内核执行或结果恢复 → Provider 单次传输及协议提交 → 投递回执/对账 → UI 投影。

**16．比函数级更细的关键实现清单**

以下是实现时逐行审查的算法约束。名称可以调整，定序和断言不能省略。

**16.1．池化正常归还伪代码**

```python
def release_if_complete(lease):
    with lease.lock:
        if lease.state != ACTIVE:
            return
        lease.state = RETURNING
        connection = lease.take_connection()
        response = lease.take_response()

    reusable = False
    cleanup_ok = False
    close_error = None
    terminal = CLOSED
    try:
        reusable = framing_complete(response) and not response.will_close
        response.close()  # 旧响应不再持有可读缓冲，才允许下一个拥有者使用
        cleanup_ok = True
    except Exception as error:
        close_error = error
    finally:
        try:
            if reusable and cleanup_ok:
                terminal = lease.session.return_or_discard(lease.key, connection)
            else:
                close_physical_connection(connection)
        except Exception:
            close_physical_connection(connection)
            raise
        finally:
            with lease.lock:
                lease.state = terminal
    observe_optional_close_error(close_error)  # 观察边界不抛回调用者
```

这是定序示意。return_or_discard 给出 RELEASED/CLOSED 终态，内部所有权转移之后不得再运行可能抛错的观察回调；否则调用者无法判断是否还能关闭连接。底层 close 也必须有明确幂等和错误记录边界。ConnectionLease 的资源指针一旦取走，旧 response 的 sock/read/abort 均不能重新取得它。

**16.2．取消与 close 的线程顺序表**

| 已发生事件 | 取消线程下一步 | 归还线程下一步 | 新借用者 |
|---|---|---|---|
| ACTIVE，cancel 取得旧目标快照 | 尚未取得租约关闭权 | 可以争取 RETURNING | 暂无 |
| RETURNING 已取得归还权 | cancel 检查租约，立即返回 | 关闭旧 response，再入池 | 入池后才可借 |
| CANCELLING 已取得关闭权 | shutdown、close，最后 CLOSED | 不归还 | 必须另建连接 |
| RELEASED，物理连接已在新租约 | 旧租约取消无效 | 重复 close 无效 | 正常读取 |

真实 socket 红测试暂停点放在“已取旧目标快照、未执行租约 cancel”之间。暂停能力由测试包装已有调用边界提供，不在生产添加 test_barrier 参数或环境变量。

**16.3．ledger 事务写入检查点**

1. 验证 expected_ref、schema、session 和 expected_revision，不先修改 self.view/ref/sequence。
2. 组装新 row，包含 window_ref、view_digest、events/checkpoint、delivery_commit 及前一 chain；深拷贝输入，防止调用者随后修改摘要内容。
3. 在 ledger 文件锁下重读 head，核对磁盘序号/chain/ref；另一写者变化则 conflict，不能自动覆盖。
4. 写完整 row 文件；构造新的 head，包含相同 ref、sequence、chain 和 view_digest。
5. 原子替换 head；这是本地持久化提交点。
6. 仅在成功后更新 sequence/chain/ref/view/history_revision；提交 ID/witness 随返回值提供给对账层。
7. 失败不更新内存。下一次恢复仅加载 head 指定的已提交 prefix，不能直接加载编号最大的 segment。
8. reopen 时验证 head 元数据与最后 row 一致；篡改 ref 而没有相应 row 应失败。

reset 使用同一过程写新 window_ref 与初始 view；不能先清 _messages 再写盘。

**16.4．结果验证的精确顺序**

```text
approval/session 所属关系
→ artifact 存在、scope/schema/digest 合法
→ 原 selection 可用且不允许 failover
→ 当前主 window_ref 完全匹配
→ invocation kind 为申请时的原来源
→ batch 已存在、尚未 delivered/abandoned/unknown
→ 每个原生 call_id 非空且仍开放
→ 结果 ID 无重复、集合等于完整待回答批次
→ codec.prepare 完成配对验证
→ 领取唯一 send_attempt
→ HTTP
```

每个前置验证失败都断言：HTTP 请求计数零、Shell 执行计数不增加、result artifact 仍存在、window_ref 和原 accepted view 不变化。不要只断言抛了某个异常。

**16.5．崩溃点与恢复动作矩阵**

| 崩溃点 | 可证明的持久事实 | 恢复动作 |
|---|---|---|
| command artifact 写完，requested 未提交 | 没有有效审批 | 不执行，孤立 artifact 可清理 |
| requested 已提交，尚未裁决 | 等待用户批准 | 恢复卡片，禁止自动执行 |
| execution_claimed 已提交，Popen 前后无 settlement | 无法证明执行是否发生 | execution_unknown，不重跑 |
| result artifact 写完，settlement 未提交 | 不能只据孤立文件判定完整执行效果 | 按执行 effect 校验；无可信 settlement 则明确异常 |
| execution_settled 已提交，send_attempt 尚未领取 | 已执行，结果待投递 | 精确恢复窗口，投递结果 |
| send_attempt 已领取但尚无 socket 证据 | 无法证明网络未发生 | 保守 unknown，除非有可信 not_sent 结算 |
| 写前 EOF 已结算 not_sent | 确定没有生成 POST | 预算允许时重试，不重跑 Shell |
| 生成写入开始，响应丢失 | 服务端可能接受 | delivery_unknown，不自动重发 |
| 完整响应收到，ledger head 写失败 | 生成已发生，本地事务未接受 | local_settlement_failed，不自动重发 |
| ledger head 已提交，runtime delivered 未提交 | 同批结果已经本地接受 | 根据 witness 补回执，不发送 |
| delivered 已提交，UI 未发布 | 完成事实已在磁盘 | 重建 UI，不能重新执行 |

不是所有崩溃都可以自动恢复到继续运行；明确 unknown 是正确结果。

**16.6．锁与 I/O 约束**

- 不在 session/registry/租约状态锁里 close socket/response、写 ledger、运行观察回调或等待 condition。
- 物理归还前旧 response 必须关闭；状态锁外清理期间资源仍属于 RETURNING 租约，不属于池。
- 生命周期 gate 先取得，再访问 Provider 的发送/状态锁；reset 路径不能反向从 Provider 回调获取生命周期 gate。
- app 的 Stop/spawn gate 次序沿用现有顺序；实际 Shell Popen 不在 app 状态锁里等待。
- RuntimeSessionLog.mutate callback 不做外部效果、网络或 ledger 写入；回执和 ledger 通过 witness 对账，不尝试跨两把文件锁做伪原子事务。
- abort 使用独立取消能力，不能先等生命周期 gate。取消后的 commit 是否成立按提交权的定序判断。
- 会话文件锁只负责单写者 CAS；本轮继续遵守现有单活动应用运行约束，不据此声称多个独立进程都能安全同时发送同一窗口。多进程同时运行需要单独的执行所有权设计。

**16.7．每个测试至少观察什么**

| 类别 | 最小必要观察 |
|---|---|
| 审批正常续接 | 用户最终回答、原 call_id、window_id、HTTP 次数、Shell 次数、持久 delivered |
| 续接本地失败 | HTTP=0、原结果保留、发送状态 not_sent、无额外 Shell |
| unknown | live attempt 保留、无自动第二 POST、无第二 Shell |
| reset 失败 | 内存和磁盘 ref/view 一致、旧历史未清、开放调用仍可查询 |
| 取消串扰 | B 正常完成、A Stop 返回、实际连接复用已发生 |
| 资源收尾 | 锁可重新取得、pending/probe 释放或明确损坏、idle/socket 关闭 |
| 冷却 | 实际准入时间边界和版本、试探所有者；不是仅测计数器 |
| Zen 删除 | 阻止包导入、没有 Zen 模块在 sys.modules、Local 完整最终回答 |

这些观察避免把“最终某个调用次数相等”当成生命周期所有阶段都正确。
