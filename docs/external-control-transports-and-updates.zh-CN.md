# 外部控制实施细则：RPC、MCP、广播优化与自动版本切换

状态：v0.3 设计基线，已进入实现。日期：2026-10-21。

设计阶段只修改文档；后续实现、接口及实际支持边界见[实现与使用](external-control-implementation.zh-CN.md)。
实现必须同时满足[外部控制主设计](external-control-design.zh-CN.md)中的权限、去重、
快照、人工接管、审批、预算、证据和未知副作用规则。本文不建立第二套任务状态机。

## 1. 交付范围与源码依据

八项能力作为一轮完整交付：委派、观察、桌面可见、返修验收、RPC、MCP、
广播性能优化、自动候选版本切换与失败回退。可以分提交实现，不将任一项静默推迟到下一版。
范围是本机共享服务、桌面/浏览器、CLI/脚本、外部智能体；本轮 MCP 明确交付 stdio 接入。
跨公网托管和任意远程机器部署不属于这份本机协议的完成条件。

### 1.1 实际阅读的新增参考位置

| 来源与本地文件 | 实际看到的实现 | 采用方式与边界 |
| --- | --- | --- |
| Pi `reference-projects/pi/packages/coding-agent/src/modes/rpc/rpc-mode.ts` | 命令交给 session，响应与事件分开，重绑定订阅 | Codey RPC 交给共享服务；不启动第二个 agent runtime |
| Pi `reference-projects/pi/packages/coding-agent/src/modes/rpc/rpc-client.ts` | 请求响应关联、事件监听、进程退出与超时 | 区分传输关联 ID 与业务 request ID；失败查询原回执 |
| Pi `reference-projects/pi/packages/coding-agent/src/modes/rpc/jsonl.ts` | LF 分帧、UTF-8 解码、CRLF 兼容，保留 U+2028/U+2029 | 增加单帧字节上限和有限待处理请求；不用 splitlines 解析协议 |
| Pi `reference-projects/pi/packages/coding-agent/test/rpc-jsonl.test.ts` | Unicode 分隔符、CRLF、无末尾 LF 的测试 | 对 Codey 的分帧边界做同类回归，并明确写操作 EOF 策略 |
| OpenCode `reference-projects/opencode/packages/server/src/event-feed.ts` | 公共事件编码一次、独立有界订阅队列、溢出只影响该订阅 | 复用不可变正文，按授权范围过滤；增加字节上限、写入期限和同步恢复 |
| OpenCode `reference-projects/opencode/packages/server/test/event-feed.test.ts` | 编码次数、慢订阅者、内部事件过滤的测试 | 用行为和计数器证明优化，不能只看到一个新 feed 类就算完成 |
| OpenCode `reference-projects/opencode/packages/client/src/effect/service.ts` | discover 与 ensure 分开、注册文件/探测/版本检查、停止旧进程 | 普通客户端发现不触发升级；版本切换由独立 launcher 明确协调 |
| OpenCode `reference-projects/opencode/packages/client/src/service-contender.ts` | Windows 隐藏启动、有界 stderr、启动竞争退避 | Windows 后台启动不弹窗口，启动失败可诊断；Codey 优先使用已有 OS lease 单所有者 |
| OpenCode `reference-projects/opencode/packages/core/src/session/execution/restart.ts` | 持久化恢复次数、防止跨重启循环，明确恢复不是副作用 exactly-once | 恢复次数持久化且有限；Codey 继续遵守现有 effect unknown 不重放规则 |
| Codex `reference-projects/codex/codex-rs/rmcp-client/src/bin/rmcp_test_server.rs` | 测试服务端的工具 schema、list/call、结构化结果、stdio 生命周期 | 仅借鉴 MCP 边界与测试形状；它是测试夹具，不宣称是完整生产服务端 |
| Codex `reference-projects/codex/codex-rs/app-server/src/turn_admission.rs` | 短锁协调入场与 drain，permit 表示尚未结束的入场 | Codey 共用准入/关闭协调点；等待和 IO 在锁外 |
| Codex `reference-projects/codex/codex-rs/app-server/src/daemon_thread_recovery.rs` | 保存加载会话、消费恢复记录，通过正常路径恢复 | 重启后加载原会话及原任务，不伪造一条新用户任务来代替恢复 |
| Codex `reference-projects/codex/codex-rs/app-server-transport/src/daemon_recovery.rs` | 原子保存恢复候选和中断身份 | 切换记录原子保存；任务执行事实仍以 Codey 原 durable store 为准 |
| Codex `reference-projects/codex/codex-rs/app-server/tests/suite/v2/daemon_update_recovery.rs` | 管理服务关闭/恢复的集成测试，包含无客户端重连恢复场景 | 验证无桌面/无原客户端时也能完成允许的恢复 |

以上是本地源码静态阅读，不代表已运行这些测试或覆盖三个项目全部实现。
没有把参考代码自动更新到在线最新版本，也不据此宣称其存在未复现的生产缺陷。

## 2. 统一命令目录与依赖方向

```text
UI / HTTP handler ───────────────────────────┐
CLI ─────────────────── ControlClient ───────┤
JSONL RPC adapter ───── ControlClient ───────┤── ControlService
MCP stdio adapter ───── ControlClient ───────┘      │
                                   原 admission / scheduler / kernel

版本操作 ── ReleaseClient ── 独立 launcher ── 服务 lifecycle API / 发布目录
```

ControlService 管理任务命令、查询和前提；版本操作由 launcher 拥有。
ControlClient/ReleaseClient 只封装连接、发现、查询、重连与共同错误映射。
它们不直接打开共享任务数据库，不导入执行内核，不推断或重写任务终态。

在 `codey/control/protocol.py` 维护版本、类型化请求/响应和命令描述目录。
目录包含输入/输出 schema、查询或修改分类、所需能力、有界输出与操作处理器名称。
HTTP、RPC、MCP 从它生成或核对边界 schema，应用服务只接收已解析的类型。
不让 MCP SDK 类型进入 durable/core；不把 SDK 自动生成的工具描述当作授权规则。

统一对外目录的最小覆盖：

| 操作 | 用途 | 特殊约束 |
| --- | --- | --- |
| create_conversation / read_conversation | 服务端创建与读取聊天快照 | 创建空会话不自动授予新一轮委派 |
| send_message / wait_run | 委派、返修、干预、等待 | 稳定业务 request ID、代次、预期 run；等待返回明确 reason |
| get_run_result / read_run_evidence | 有界结果与取证 | 所属及 digest 检查、分页、截断标志 |
| record_review | 保存独立验收结论 | 绑定候选与验证证据，不触发新模型任务 |
| answer_question | 回答已授权的普通问题 | audience、调用者、revision、代次一致 |
| stop_run / pause_control | 精确运行取消或整轮协作停止 | 两者含义不同，不能相互伪装 |
| get_control / get_health | 观察控制权与诊断 | 查询不获取权限、不启动服务替换 |
| prepare_release / activate_release / get_upgrade | 准备候选、启动切换、读取切换状态 | 独立版本能力、候选 digest、预期旧实例和代次 |

用户授权的委派启停操作在桌面和 CLI 提供。若也经 RPC/MCP 暴露，仍要求对应用户能力；
工具可发现不意味着外部模型可以自行授予控制权、审批权或提高预算。

协议目录只定义一份命令意义，不使用“任意 method + 任意 params”绕过类型和能力校验。
运行证据和审批正文属于数据，不能被解析成新的控制指令。

## 3. JSONL RPC 的具体实现

新增 `codey/control/rpc.py`，新增 CLI 入口 `codey rpc`。
它是长驻的 stdio 客户端适配器，可由其他语言启动；内部连接共享服务。
与 MCP 是两个明确的启动入口，不在同一 stdin 上猜测客户端协议。

### 3.1 消息形状

采用 JSON-RPC 2.0 外壳；业务协议主版本在 initialize 中协商，二者版本号不混用。

```json
{"jsonrpc":"2.0","id":"wire-1","method":"control.send_message","params":{"request_id":"req-17","conversation_id":"chat-1","control_generation":4,"mode":"follow_up","text":"实现任务并运行约定检查"}}
```

`id=wire-1` 仅用于这次传输响应配对；重连后可变化。
`request_id=req-17` 标识原修改命令；重试不得变化，也不得擅自替调用方补出新 ID。
成功响应 result 返回主设计中的真实 AdmissionReceipt；这不表示任务执行完成。
修改命令必须有传输 ID 和业务 request ID；无响应通知不允许悄悄启动任务。
无法响应的非法 notification 只记有界诊断且不执行副作用。

订阅更新使用通知，携带 session/run、持久化游标和有界公共数据。
输出仅三类：result、error、notification。stdout 不混入启动提示、模型原始输出或日志。
stderr 有界，包含 request/run/instance 关联身份；不得输出控制凭据。

### 3.2 分帧、并发与取消

- 只按 LF 分帧，允许 CRLF；逐完整帧严格 UTF-8/JSON 解码，保留字符串中的 U+2028/U+2029。
- 使用固定大小的字节读取和单帧上限；建议初始上限 1 MiB，最终与任务正文/证据分页预算一起校验。
- EOF 时未以 LF 结束的修改帧不执行，返回或记录不完整输入；完整但回执未收到的写入按原 request ID 查询。
  Pi 的无末尾 LF 解析是可选参考行为；Codey 明确选择更严格的修改帧提交规则。
- 非法 JSON 返回有界 parse error；超大帧终止该适配连接，既有服务任务继续。
- 同时处理请求时按传输 ID 配对；设置 in-flight 上限，长 wait 不堵住 stop/get_health。
- 每个输出 writer 使用有界队列和写入期限；不读取 stdout 的调用方只能拖住自己的适配进程。
- 取消 wait/subscription 只取消观察。停止任务必须显式调用 stop_run 或 pause_control。
- stdin EOF、父进程退出、stdout broken pipe 均释放连接、观察订阅和待处理查询，不隐式取消已接纳任务。
- 服务切换时重新发现实例并完成握手，读取原回执/快照；不自动再调用 send_message 创建新任务。

Parse error、非法 method、协议不兼容与业务错误分层映射；保留原业务 code，
不能让调用者通过匹配中文 error.message 决定是否重试。

## 4. MCP 服务端的具体实现

新增 `codey/control/mcp.py` 与 `codey mcp`。它把同一命令目录映射成 MCP 工具，
连接 Codey 服务，不启动模型执行器。分帧、initialize、tools/list、tools/call 和取消等
协议行为交给受维护的 SDK；实现时根据所需能力核对并固定依赖版本，本次不指定未核实版本。

MCP 依赖只在适配入口加载，不能让普通 CLI、headless 或 durable 导入被 SDK 拖重。
可用独立安装依赖组交付，但本轮必须实际安装该组并完成 MCP 验收；“依赖可选”不代表功能延期。
修改计划涉及 `pyproject.toml` 的依赖组和 CLI 帮助；本次不实际安装或更改它们。

### 4.1 工具与返回

工具保持上节同名、同字段、同输出意义；输入 schema 拒绝未知关键字段，输出 schema 有版本。
read-only/idempotent 等标记按真实行为填写，不能把发送/升级误标为查询。
标记辅助宿主判断，服务端仍做认证、能力和作用域检查。

工具调用返回共同响应的 structured content，并提供短文本摘要以便模型阅读。
大证据通过 read_run_evidence 分页；不同时把整份结构化结果再复制成一段长文本。
业务失败返回工具错误及稳定 code；非法参数/协议错误由 SDK 的相应错误机制表达。
MCP 的取消只解除本次等待；原任务取消仍需带明确目标和代次的 stop 工具。

### 4.2 宿主接入与升级重连

启动配置指向稳定的 Codey 启动器和 `mcp` 子命令，不能绑定很快被替换的候选目录。
控制身份从受保护的本机配置取得，工具参数不能传入“我是用户”来获取权限。
连接初始化获取 Codey 业务版本、capabilities、instance ID；MCP 协议协商由 SDK 处理。
只读发现不会因 CLI 与服务包版本不同就替换服务；业务协议不兼容时明确报错。

工具目录在连接期间保持稳定；可用性变化通过 capability/工具结果表达。
若确需改变目录，遵循协商能力发送目录变化通知；不假定宿主总能收到主动进度。
正确性依赖可恢复的 wait/result/snapshot，不能依赖某一条 MCP 进度通知。
版本切换时 MCP 适配进程可以保持连接，使用新服务实例恢复查询；若适配器本身不兼容，
有界退出并让宿主重启连接，已接纳任务和请求身份不受影响。

所有公共 schema 做快照及跨适配器一致性检查；用 Codex 作为真实客户端完成一次委派和返修。
Rust 测试服务端中读取环境的 echo 仅是夹具演示，不移植成 Codey 的环境读取工具。

## 5. 广播优化：共享正文，隔离权限与背压

现有 `codey/app/event_bus.py` 按订阅者复制 payload，`server.py` 的每条 SSE 连接
再调用 `browser_event_payload` 和 `http_plumbing.write_sse_event()` 编码。
本轮在传输边界增加公共 feed，使重复投影/编码不随同一视图的客户端数量增长。

### 5.1 数据路径与所有者

```text
原运行事件 / durable commit
           │ 唯一序号、作用域、不可变事实
           ▼
public_event_feed
  公共投影 / schema / 授权视图分类 / JSON→UTF-8 一次
           │ 共享不可变正文引用
           ├── 桌面 SSE queue → 连接专有 cursor/header → socket
           ├── 浏览器 SSE queue → 连接专有 cursor/header → socket
           └── 控制观察 queue → RPC/MCP 有界结果
```

`event_bus.py` 保留进程内事件序号与订阅生命周期；向公共 feed 提供带序号的单一发布钩子。
feed 不重新生成另一个互相漂移的事件序号，也不增加持久化事件数据库。
公共投影在发布边界冻结嵌套内容，避免共享可变 dict 导致一个客户端改变另一个客户端消息。
序列化失败形成可观察的编码错误和重新同步要求，不能回退成发送原始内部事件。

同一事件按 `(public_schema_version, visibility_class)` 缓存不可变 JSON/UTF-8 正文，
编码次数为公共视图数量，不是订阅者数量。不同授权视图允许分别编码。
每个连接在入队前检查所属会话和授权范围；凭据变化撤销相应订阅。
不能因为共享编码先发出跨会话数据，再依赖前端把它隐藏。

### 5.2 游标、回放与输出范围

连接私有的 SSE id/header、已见 commit cursor、初始 hello 和错误不强行共享。
正文可共享，连接前缀单独拼接；业务游标与实例内序号仍遵循主设计。
历史 durable commit 回放使用同一公共投影/编码路径，按需有界缓存，不永久保存所有编码结果。
正常 HTTP JSON 查询与 MCP 工具 envelope 可有各自必要编码；
不宣称不同传输外壳可以一份字节直接通用，优化目标是消除相同公共正文的重复工作。

UI 可展示完整过程，Codex 默认 wait/result 返回精简变化和证据引用。
流式文本允许合并相邻增量；终态、权限变化、问题和审批不能因合并而丢失。
所有丢失都必须能触发快照恢复，不能直接把后续 commit cursor 前移来冒充已消费。

### 5.3 内存与慢连接

每个订阅同时限制排队事件数和累计正文/前缀字节数，另限制单帧尺寸和写入期限。
共享引用节约实际复制内存，但每个订阅的逻辑字节预算仍独立计算。
writer 取走的 in-flight 帧也有上限，不能靠快速搬到另一个无界队列隐藏积压。

超过任一限制后，仅该订阅进入 resync_required 并断开，或在可保证送达时发送恢复标志后断开。
恢复标志也写不出去时直接关闭；新连接必须先校准快照，不假定旧连接最后一帧已送达。
网络写、刷新和关闭不占用 durable 写锁；feed 的订阅锁内只做短时索引/队列操作。
事件数量上限不能替代字节和网络缓冲控制；不能把 OpenCode 的 4096 当作未经测量的最优值。

指标包含每公共视图编码次数、编码耗时、队列字节高水位、溢出数、socket 写超时、
重连次数和提交至 UI 显示延迟。指标只观察，不成为第二套执行状态。

## 6. 自动版本切换与失败回退

目标是自动完成“Codey 修复自己 → 候选验证 → 切换 → 新实例复测 → 继续观察”。
自动化执行已经授权的发布策略；准备候选与激活候选是两个操作，
有版本激活能力的 Codex 可连续完成，普通任务写入能力不自动获得激活权限。
不要求每次切换重新询问已经明确授权的用户，也不让模型仅凭修复总结宣布候选可用。

### 6.1 launcher 的边界

新增小型 `codey/launcher/`，作为独立进程协调生命周期，不调用模型、不执行任务内核。
运行路径属于稳定启动安装，不能直接从正在自修的项目目录加载协调代码。
launcher 自身升级与普通候选服务切换分离，避免替换服务时一起破坏恢复入口。
Windows 使用隐藏后台启动和 argv 数组，保留有界错误输出；不拼接包含用户任务正文的 shell 命令。

候选服务放入以 digest/release ID 命名的独立发布目录，验证后不可原地修改。
Python 环境、依赖标识、后端源码、前端构建和资源清单共同构成 release identity。
主设计的新服务与桌面拆分也必须覆盖这个发布路径：旧窗口不能混用新后端与旧资源。
候选含有可重现的构建/验证记录；仅改 `__version__` 不算版本身份。

任务事实仍在原 durable store。launcher 只维护受保护的 release manifest 和升级 journal：

```text
upgrade_id、request_id、expected_instance_id、previous_release、candidate_release
candidate_digest、phase、attempt_count、backup_ref、schema_compatibility
verification_refs、admission_may_have_opened、timestamps、last_error
```

这是发布状态，不复制 task 状态。journal 原子替换并校验 revision；操作按 request ID 幂等。
发布操作的幂等记录由 launcher journal 拥有，任务入场的幂等记录由原 durable store 拥有；
ControlService 转发发布操作时不得再制造一个与 launcher 互相矛盾的“升级任务终态”。
launcher 使用升级协调 lease；服务仍持有原执行 lease。两种 lease 的获取顺序固定，
launcher 不在持有 durable 写锁时等待退出，不存在旧实例仍写入却已启动后继执行器的窗口。

### 6.2 切换阶段与失败行为

```text
prepared → validated → draining → quiescent → starting → verifying → active
     └→ failed/cancelled                     └→ rolling_back → rolled_back
                                                    └→ recovery_required
```

这是版本切换状态机，不接管任务状态机。

| 阶段 | 必须完成的工作 | 失败时行为 |
| --- | --- | --- |
| prepared | 固定候选 digest、旧 release/instance、权限范围、验证计划 | 不接触运行服务，保留失败记录 |
| validated | 独立代码目录、独立 state home、临时端口验证启动/协议/资源/迁移及原 bug 复现 | 原服务继续运行；候选不进入切换 |
| draining | 在共同准入点拒绝新任务；允许观察、停止、诊断和协调退出 | 到策略期限仍忙则结束本次升级或按已授权取消策略收尾；不偷偷强杀 |
| quiescent | 所有执行及工具清理结束，冻结调度；保存恢复身份；完成一致性数据库备份 | 无法证明静止则不启动后继；原实例未退出时不冒充切换成功 |
| starting | 旧进程已退出且执行 lease 已释放；启动候选，仍关闭普通任务入场 | 候选退出或超时，进入有界回退 |
| verifying | 核对新 instance/release/schema；加载原会话和回执；验证服务、资源、复现证据与客户端同步 | 保持新任务入场关闭，停止失败候选再回退 |
| active | 发布 active manifest，开放入场；客户端重连；原恢复策略接管可恢复任务 | 此后失败按已经接纳的新工作和 schema 兼容性处理，不能随意还原旧数据 |
| rolled_back | 恢复已知可运行版本，确认其身份与状态；记录候选失败 | 整次升级结果为失败且已回退，不能报告候选修复成功 |
| recovery_required | 无法安全恢复或达到恢复次数上限 | 保留所有事实和诊断，停止自动反复切换，明确需要处理 |

activate_release 返回持久化 upgrade ID 和“已接收”，不冒充版本已切换。
get_upgrade 通过独立 launcher 查询，即使服务关闭也能读取过程。
等待升级的 RPC/MCP 连接丢失只停止观察，重连查询同一 upgrade ID。
普通 ControlClient.discover 不自动替换版本；并发 activate 通过协调 lease 和预期实例拒绝冲突。

activate_release 的公共输入固定为：request_id、candidate_release、candidate_digest、
expected_instance_id、授权 policy 引用；属于本轮自修时还带 conversation、根请求身份及 control_generation。
启动程序、备份位置和回退路径从受保护的 release manifest 解析，不接受模型提供任意 shell 字符串。
同 request ID 指纹不同报 REQUEST_CONFLICT，不能换一个升级 ID 再试。
结果区分 active、rolled_back、failed、cancelled、recovery_required；只有目标候选为 active
且关联验证通过时，才表示本次激活成功。

停止或接管发生在旧服务退出之前时，取消尚未完成的激活并恢复安全的原服务入口。
已经停掉旧服务后，launcher 必须先完成安全启动或回退，再结束取消流程；
取消不能使系统停在无可用服务且无人协调的中间阶段，也不能恢复旧代次的模型任务。

### 6.3 候选验证与运行现场

候选验证不连接生产模型会话执行未经授权的命令，不复用生产服务的 browser worker 所有权。
默认使用模拟 provider、隔离项目和状态目录做契约及复现测试。
确需真实模型/浏览器复现时使用明确授权的验证范围，并计入原轮预算；不能“为测试”绕过审批。
验证完成后核对候选 digest 未变化，变化则重新验证。

旧服务 drain 后保存的恢复记录包含会话、任务和现存 effect 身份；
恢复只通过原 scheduler 的正常路径，保留原授权、控制代次和预算。
用户已经停止或接管的任务不会因版本更新恢复；未知外部效果不重放。
候选验证报告与新实例的 read-only/隔离复现结果组合验收，不能仅凭 HTTP 200 判定原 bug 已修复。
需要对真实工作区有副作用的复测，必须保留其原授权、执行回执及范围，不能藏在健康探针中。

候选连接生产 state home 的 starting/verifying 启动模式必须关闭 scheduler 自动派发、
输入队列续跑和 provider 自动连接；可以复用 AppContext 已有的延迟启动配置，再补统一 activation gate。
否则“尚未开放 HTTP 入场”并不能阻止启动恢复在验证前执行旧排队任务。
只有验证通过且 launcher 允许激活后，原 scheduler 才按正常权限与恢复规则开始工作。
桌面和控制客户端在 verifying 期间可查询升级诊断，不能订阅候选普通任务事件并取得尚未确认的游标。

持久化切换阶段、已启动实例身份和有限 attempt_count；launcher 自身重启时读取阶段并核对
实际进程、lease 和 release，再继续或回退，不因 journal 写到 starting 就认定候选已启动。
launcher 看不到明确安全事实时停止自动恢复；不能靠无限重启等待偶然成功。

### 6.4 数据库与前端回退

当前 `durable/sqlite_schema.py` 主要创建或严格校验当前 schema，不是完整跨版本升级器。
本轮必须扩展真正的事务迁移、兼容范围检查与失败恢复，不能把版本字段加一就算完成。
候选 manifest 声明可读/可写 schema 范围及迁移身份，隔离验证覆盖旧数据库的真实结构。

数据库备份使用 SQLite 一致性 backup 能力或等效已验证方案，不直接复制正在使用的 DB/WAL 文件。
恢复副本前必须确认所有相关连接已关闭、原执行所有权释放，且没有随后接纳的新工作。
开放入场前，launcher 先持久化 `admission_may_have_opened=true`，再向候选发送激活许可。
这是不可逆的回退屏障：即使候选已接收任务后 launcher 还没记下 active 就退出，恢复逻辑也不能
因为 journal 仍是 verifying 而错误还原备份。无法核实是否已有新工作时按可能存在处理。
回退规则明确分两段：

1. 开放新入场前失败：可以在确认无新工作后恢复切换前备份并启动旧 release，保留失败诊断。
2. 新版本已开放入场：仅在旧 release 明确兼容当前 schema 时回退程序并保留新数据；
   不兼容则进入 recovery_required，保留数据库，不能自动还原备份丢弃新消息/回执。

切换后已经写入的外部项目修改也不会被“恢复数据库”撤销；状态回退不代表副作用回滚。
升级 journal 与审计副本保留已经失败的切换，不能通过恢复任务数据库把失败现场一起抹掉。
存在主设计新增会话元数据等迁移时，同样遵守以上兼容与回退约束。

前端资源随 release 固定，复用/扩展 `http_plumbing.send_index()` 的实际资源身份校验。
新服务提示已连接桌面刷新到匹配资源；刷新恢复会话、草稿和阅读位置，不重复提交未确认输入。
SDK/业务协议不兼容时明确提示，不让旧客户端凭陈旧 schema 执行修改命令。

## 7. 具体修改地图与实施依赖

以下全部是待实施位置，本次不创建这些生产文件或安装依赖。

| 位置 | 计划修改 |
| --- | --- |
| `codey/app/server.py`、`app/context.py`、新增 `app/service.py`、`app/desktop.py` | 提取服务/窗口生命周期，提供发现、握手、ready/draining、版本身份和停止协调；候选验证期间冻结自动派发 |
| `codey/app/cli.py`、`app/headless_runner.py` | 增加 rpc/mcp/版本命令；共享执行连接服务，隔离执行使用独立状态与 lease |
| 新增 `control/protocol.py`、`control/client.py`、`app/control_service.py` | 单一命令目录、类型与回执、原准入/查询能力、重连恢复规则 |
| 新增 `control/rpc.py`、`control/mcp.py`、`pyproject.toml` | 两个 stdio 入口、协议 SDK 隔离依赖、schema 与帮助文档 |
| `app/input_queue.py`、`input_queue_routes.py`、`durable/admission.py`、`durable/session.py` | 来源/关联保留、完整指纹、控制代次、队列与历史组合快照；覆盖 placed/queued/steer 路径 |
| `durable/model.py`、`schema.py`、`sqlite_schema.py`、`sqlite.py` | 会话配置与事务迁移，保留数据库身份与原任务事实；备份及兼容校验 |
| `app/event_bus.py`、新增 `app/public_event_feed.py`、`http_plumbing.py` | 唯一序号，公共正文共享编码，范围过滤，事件/字节/写入期限限制 |
| `frontend/src/sse.ts`、`app_runtime.ts`、`conversation_ui.ts`、`conversation_history.ts`、类型/渲染 | 外部会话发现、来源显示、组合快照、断线补齐、升级状态与匹配资源重连 |
| 新增 `launcher/`、`storage/file_lock.py` 的复用接口 | 稳定启动器、发布清单、journal、候选验证、lease 交接、自动回退 |
| `runs/`、既有 trace/receipt/managed output 与诊断投影 | 原事实有界查询、候选绑定验收、版本/实例关联；不复制日志数据库 |

按以下依赖顺序实现并分别验证，但最终一起交付：

1. 服务与窗口分离、稳定 launcher、单实例和握手；同时验证共享和隔离执行。
2. 会话配置迁移、统一命令、停止/接管、快照与证据；形成 CLI→桌面可见路径。
3. RPC/MCP 目录、schema 与同语义契约测试；真实客户端委派和一次返修。
4. 公共 feed 接入全部观察者，完成权限过滤、回放、背压和共享编码计数验证。
5. 独立候选与发布目录，升级 journal、drain、备份、切换、回退及跨实例恢复。
6. 将 Codey 自修真实任务接入前述能力，验收新实例复现、预算、人工接管和完整聊天。

不得通过关闭现有完成门、跳过审批、扩大可重放效果或删除旧历史来使新入口“跑通”。
模块合并与拆分按实际复杂度决定，依赖方向与事实所有者比文件数量更重要。

## 8. 功能、故障与性能验收

主设计的 20 项故障场景继续全部适用，新增以下专项矩阵：

| 类别 | 必须验证的行为 |
| --- | --- |
| RPC 分帧 | 多字节 UTF-8 跨块、CRLF、Unicode 分隔符、非法/超大帧、EOF 不完整写入、日志不污染 stdout |
| RPC 并发 | wait 未结束仍可查询/停止；乱序响应按 ID 配对；stdout 堵塞不阻止共享服务执行 |
| MCP 契约 | 工具发现、参数与输出 schema、稳定 code、结构化有界结果、取消只取消观察 |
| 跨入口一致性 | HTTP/CLI/RPC/MCP 对同一命令返回同一任务和回执；跨入口重试不重复入场 |
| 桌面可见 | 通过 RPC 和真实 Codex MCP 各完成一次委派→结果→返修→验收；来源、工具和停止状态均可见 |
| 无桌面 | 无窗口执行后再打开窗口恢复原聊天；关闭窗口不结束任务 |
| 广播计数 | 同公共视图 1/5/20 个客户端时，单事件公共正文编码次数都是 1；不同视图按视图数量计数 |
| 广播隔离 | 未授权会话不入队；内部事件不泄漏；慢连接溢出只影响自己；重新同步不漏终态 |
| 广播资源 | 长输出/流式突发下队列事件数、逻辑字节、in-flight 与写入期限均受限；广播缓冲驻留内存不会随时长无界增长 |
| 升级并发 | 两客户端同时激活；旧预期实例/候选 digest 不符；服务启动与升级争抢时仍只有一个执行所有者 |
| 升级故障 | 在每个阶段中断 launcher/旧服务/候选，重启后有界恢复且 journal 与真实实例一致 |
| 激活屏障 | 验证阶段不会自动执行生产排队任务；许可开放入场后、记录 active 前中断，也不能回退备份抹掉新任务 |
| 回退边界 | 候选启动失败/验证失败自动恢复旧 release；新工作已接纳后不回退旧备份丢数据 |
| 迁移 | 旧数据库升级、迁移中断、候选不兼容、备份损坏、旧 release 无法读取新 schema 均明确失败并保留事实 |
| 版本身份 | 新后端、前端资源与候选 digest 匹配；升级未成功不能冒充已修复；回退结果明确显示 |
| 控制与预算 | 接管/停止/审批与升级竞争，旧代次不能复活；重连、切换、回退不重置返修预算 |
| 恢复效果 | 已确认工具结果复用；未知外部副作用不自动重复；自动恢复次数跨重启有界 |

性能测量使用固定事件集、相同机器、相同公共投影与客户端数量，分别测当前实现和新实现。
记录重复运行的中位数、p95/p99、CPU/RSS、编码次数、队列高水位、正常客户端延迟与执行推进。
至少包含无订阅、正常订阅、一个停止读取的订阅、较大事件和混合权限视图。
在测量前确定可接受的延迟回归预算；不能事后选择最有利的一次结果。

共享编码的验收依据是可观察计数和字节路径；“提高多少性能”以实测为准。
真实模型费用使用主设计的同任务对照，分别报告宿主与 Codey 连接用量，缺失不记成零。
部署停机时间也需报告，自动版本切换不承诺零停机。

## 9. “比参考项目更好”的具体目标

目标限定在本方案的本机委派与自修协作范围，不宣称全面超过三个项目。

| 要吸收的优点 | Codey 要进一步证明的目标 | 证据 |
| --- | --- | --- |
| Pi 的薄 RPC 与清晰命令 | RPC 接入同时复用正在运行的共享会话；重试具有完整内容身份与有界 IO | 跨入口回执一致、分帧/断线/EOF 测试 |
| OpenCode 的持久化收件箱与事件分发 | 内容变化明确冲突；队列/历史一致性快照；事件数和字节双重背压 | 原子快照竞态测试、权限隔离、编码计数与内存测量 |
| Codex 的目标运行约束与 drain/恢复 | 人工接管代次、候选/验收/实例绑定、数据库感知的版本回退共同闭合 | 竞争测试、升级阶段故障注入、新旧实例复现记录 |
| 三者的复用原则 | 新增任何入口不复制执行循环、任务队列、预算或事实存储 | 依赖检查、服务契约测试、代码审查 |

表中部分是针对 Codey 需求的组合与取舍，并非断言参考项目没有相同保障。
“更优雅”用事实所有者清楚、入口一致、边界少且可测试来判断；
“更健壮”用故障矩阵证明；“更省”用真实正确任务的总用量证明。
在这些证据存在前，状态始终为待实现/待验证，不提前填写通过或优于基线。
