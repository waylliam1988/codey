# Codey 外部控制：参考实现评估与具体设计

状态：v0.3 设计基线，已进入实现。日期：2026-10-21。
当前接口、落地取舍及支持边界见[实现与使用](external-control-implementation.zh-CN.md)；本文保留设计依据，不作为已完成测试的证明。

评估对象是本仓库 `reference-projects/pi`、`opencode`、`codex` 的本地源码快照，
不代表它们在线仓库的最新版本。本文没有运行参考项目，也没有性能比较结果。

## 1. 目标与结论

同一套 Codey 能力同时供桌面、浏览器、CLI、脚本和外部智能体使用。
桌面是可选客户端：没有桌面时任务照常执行；之后打开桌面可以看到原聊天。
“无桌面”指没有 Codey 窗口；网页模型自身对浏览器和登录状态的要求仍由其连接实现决定。

本轮完整实现范围：委派、观察、桌面可见、返修验收，以及 RPC、MCP、广播性能优化、
自动候选版本切换与失败回退。允许按依赖分步实现，但这些能力全部验收后才能称本轮完成。
共享服务拥有执行与持久化，HTTP、CLI、RPC、MCP 复用同一应用级控制能力。
MCP 提供给外部智能体调用；无需让 Codey 的任务内核承担 MCP 客户端职责。
新增四项的源码依据、具体文件改法、协议与切换流程见
[RPC、MCP、广播与自动版本切换实施细则](external-control-transports-and-updates.zh-CN.md)。

```text
桌面 / 浏览器 ─────────────────┐
CLI / JSONL RPC ─ ControlClient ├── 本机 Codey 服务
Codex ─ MCP ───── ControlClient ┘          │
                              应用级命令与查询
                                    │
                    DurableSession / 输入队列 / 原执行内核
                                    │
                    持久化提交 → 实时通知与一致性快照

独立 launcher ── 候选验证 / drain / 版本切换 / 回退
                 （不执行模型任务，不复制任务状态）
```

每个 state home 只有一个执行服务；可同时有多个客户端。
独立运行也使用同一应用能力，但必须使用独立 state home，不能再开一个执行器抢占共享实例。

## 2. 三个参考项目实际提供了什么

### Pi：小而明确的命令与事件协议

关键源码：

- `reference-projects/pi/packages/coding-agent/src/modes/rpc/rpc-mode.ts:394`
- `reference-projects/pi/packages/coding-agent/src/modes/rpc/rpc-client.ts:471`
- `reference-projects/pi/packages/coding-agent/docs/rpc.md`
- `reference-projects/pi/packages/coding-agent/test/rpc-prompt-response-semantics.test.ts`

RPC 模式通过 stdin/stdout JSONL 控制长期子进程。`prompt` 调用同一个 session，
入场检查成功后返回 started/queued/handled 等 disposition；完成通过 session 事件表达。
`steer`、`follow_up`、`abort` 有明确区分，事件订阅在 session 重绑定时重新建立。
客户端把请求与响应按 ID 配对，并处理进程退出和请求超时。

值得学：接收与完成分开；命令少且语义明确；传输适配器复用 session；
stdout 只写协议、stderr 写诊断；JSONL 正确处理分帧和反压。

需要适应 Codey 的地方：这种启动子进程的客户端不能直接满足“共享运行中的桌面会话”。
Codey RPC 子进程应是连接共享服务的客户端，而不是新的任务执行器。
等待也必须绑定 run ID，不能只等某个无运行身份的 idle 事件。
这是目标差异，不是对 Pi 整体可靠性的判断。

### OpenCode：共享服务、持久化收件箱、多客户端事件

关键源码：

- `reference-projects/opencode/packages/server/src/handlers/session.ts:308`
- `reference-projects/opencode/packages/core/src/session/inbox.ts:155`
- `reference-projects/opencode/packages/core/src/session/execution.ts`
- `reference-projects/opencode/packages/server/src/event-feed.ts:33`
- `reference-projects/opencode/packages/client/src/solid/data.ts:635`
- `reference-projects/opencode/specs/v2/event-stream-architecture.md`

HTTP handler 调用 session service，输入先进入持久化 inbox；execution 管理唤醒、
执行和中断。其执行源码明确区分“中断已接收”与“清理已结束”，并保存崩溃恢复所需 claim。
Server EventFeed 将公共事件编码一次，再向每个订阅者的有界队列非阻塞投递。
某个订阅者过慢时终止该订阅，不让它堵住其他客户端。

值得学：共享执行所有者；持久化输入；传输层与业务层分离；单独限制慢客户端；
重连后重新校准实际状态。

两个具体设计取舍：

1. 当前 inbox 的 `matches()` 核对 session 和类型，注释明确采用 first admission wins。
   Codey 外部控制可以选择更严格的契约：相同 request ID 必须对应相同命令内容。
   内容变化报冲突，避免调用方误以为更新后的要求已经生效。
2. 客户端 `data.ts:675` 的注释记录了分别读取 inbox 与消息的竞态：输入可能在两次读取之间
   从 inbox 转为历史，导致两次读取都漏掉它。Codey 应提供同一读快照中的组合结果。

不需要为了借鉴这些原则，将 Codey 改写成 Effect 框架或照搬其包数量。

### Codex：有身份约束的控制、入场与关闭协调

关键源码：

- `reference-projects/codex/codex-rs/app-server-protocol/src/protocol/v2/turn.rs:170`
- `reference-projects/codex/codex-rs/app-server-protocol/src/protocol/v2/turn.rs:317`
- `reference-projects/codex/codex-rs/app-server/src/request_processors/turn_processor.rs:1045`
- `reference-projects/codex/codex-rs/app-server/src/turn_admission.rs`
- `reference-projects/codex/codex-rs/app-server/src/request_serialization.rs`
- `reference-projects/codex/codex-rs/app-server/src/transport.rs:138`

协议区分 thread、turn、输入和通知。`turn/steer` 要求 `expected_turn_id`；
`turn/interrupt` 指定 turn ID；启动输入可携带客户端消息身份和 parent/root turn 关联。
TurnAdmission 用短锁协调入场与 shutdown drain，permit 存活期间关闭流程知道还有工作。
请求按 thread 等资源串行化；可断开的连接在出站队列满时被断开。

值得学：所有修改有明确目标；旧客户端不能误修改新运行；关闭与接收共享协调点；
等待和网络 IO 不占用业务写锁。

不需要照搬完整 app-server 协议、全局资源调度器或 RPC 消息总量。
Codey 只暴露自身能够稳定履约的一组命令即可。

## 3. Codey 已有基础与必须补的地方

| 现有位置 | 已有能力 | 需要补齐的契约 |
| --- | --- | --- |
| `codey/app/server.py:716` | HTTP 服务、state home 独占 lease、桌面窗口 | 服务启动/关闭与窗口启动/关闭分离 |
| `codey/app/headless_runner.py:70` | 复用生产入口的 headless context | 共享模式连现有服务；隔离模式仍复用应用逻辑 |
| `codey/app/input_queue.py:112` | 现有唯一持久化输入队列、请求冲突检查 | 类型化命令、调用者身份、目标运行与停止代次 |
| `codey/durable/admission.py:30` | 输入、submission、task 原子入场 | 稳定命令指纹与来源元数据的完整持久化 |
| `codey/durable/session.py:653` | 排队输入原子转入聊天历史 | 转入与 steer 路径均保留来源及关联信息 |
| `codey/app/server.py`、`durable_events.py` | 订阅、持久化重放、keepalive | 对外游标约定及组合快照 |
| `frontend/src/conversation_ui.ts:573` | durable commit 已触发历史刷新 | 外部新会话发现、来源显示、统一快照 |
| `codey/runs/details.py:69` | 有界的 UI 运行解释 | 单独提供机器可读结果，避免解析 UI 文案 |

不能将 `input_queue.pause()` 当作外部协作暂停屏障：本快照里它只唤醒队列。
`AppContext.request_stop()` 已有真实运行取消和 shell spawn 协调，应继续复用；
新增的是阻止旧控制请求继续派发的持久化约束，而不是替换现有取消实现。

## 4. 文件职责与落地顺序

建议新增的模块（最终文件数量可按实际规模合并）：

```text
codey/app/service.py          服务生命周期、唯一实例、启动与 drain
codey/app/desktop.py          连接服务并打开窗口
codey/app/control_service.py  应用级命令/查询，调用现有入场和运行机制
codey/app/control_routes.py   HTTP 边界解码、身份验证、错误映射
codey/app/public_event_feed.py 公共投影共享编码、按范围分发、有界背压
codey/control/protocol.py     外部命令、回执、快照的稳定类型
codey/control/client.py       HTTP 客户端、连接发现、查询与等待
codey/control/cli.py          脚本友好的命令与 JSON 输出
codey/control/rpc.py          JSONL RPC 到 ControlClient 的映射
codey/control/mcp.py          MCP 工具到 ControlClient 的映射
codey/launcher/               独立生命周期、候选发布、切换与恢复协调
```

`app` 拥有执行依赖；`control/client.py` 不 import AppContext、DurableSession 或 runner。
HTTP handler 不直接拼 SQL；CLI/MCP 不直接打开共享执行数据库。
业务写入仍走现有 DurableSession 的 mutation line。

第一批：提取服务生命周期和 launcher 基础，完成实例发现、独占与版本身份；桌面连接服务。
第二批：会话配置、命令入场、组合快照与人工接管；CLI 能创建/发送/等待/停止。
第三批：RPC 与 MCP 接入，统一工具目录和错误语义；桌面显示同一消息来源及运行事实。
第四批：公共事件共享编码、有界背压、跨客户端恢复；外部验收、返修和异常取证。
第五批：候选隔离验证、自动切换、失败回退、原问题复测；完成跨入口的真实闭环与故障验收。

这些是同一轮交付的实现顺序；不把前几批完成称为完整交付，也不以“以后优化”为由省略
RPC、MCP、广播优化或自动版本切换。操作权限、输入去重和未知副作用规则对全部入口一致。

建议命令形状，尚未实现：

```text
codey serve
codey ui
codey rpc
codey mcp
codey control create --project E:\project --json
codey control send --session <id> --request-id <id> --text-file task.txt --json
codey control wait --session <id> --run <id> --until terminal_or_attention --timeout 30 --json
codey control result --session <id> --run <id> --json
codey control stop --session <id> --run <id> --json
codey control upgrade --candidate <release-id> --request-id <id> --json
codey control upgrade-status --upgrade <id> --json
```

正常 CLI 和桌面都复用同一实例。独立执行要显式选择隔离状态目录并持有同类执行 lease。
已有 `chat` 命令直接调用 provider；在声明“所有普通入口共享同一聊天”之前也需要迁移它，
或明确将它保留为不产生共享任务的底层 provider 调试命令。
不要在第一阶段声称已经实现所有入口的完全一致性。

服务发现文件记录 endpoint、instance ID、启动时间和协议版本，使用原子替换。
沿用 OS lease 选定执行服务；发现文件和 PID 不能代替活跃性及身份校验。
凭据只进入受本机用户权限保护的凭据文件，不输出到日志、模型和公开健康响应。
重启后的新 instance ID 用于识别陈旧连接；database ID 用于识别持久化历史。
服务明确提供 shutdown；窗口关闭只断开客户端，不替代任务停止。

## 5. 类型化命令：身份、来源和执行约束分开

以下是协议骨架，不是可直接调用现有仓库方法的补丁：

```python
from dataclasses import dataclass
from typing import Literal

@dataclass(frozen=True)
class Caller:
    actor_id: str              # 服务端从凭据解析
    display_name: str          # 例如 Codex；仅用于展示

@dataclass(frozen=True)
class SendMessage:
    request_id: str
    conversation_id: str
    text: str
    control_generation: int   # 防止用户停止后的旧请求继续入场
    mode: Literal["follow_up", "steer"] = "follow_up"
    expected_run_id: str | None = None
    in_reply_to_run_id: str | None = None

@dataclass(frozen=True)
class AdmissionReceipt:
    conversation_id: str
    request_id: str
    submission_id: str
    run_id: str
    entry_id: str | None       # queued 输入可能还没有历史 entry
    disposition: Literal["queued", "placed"]
    database_id: str
    commit_seq: int
```

外部客户端不传 `Caller`，也不指定任意系统角色。显示名不参与权限判断。
先用现有术语 conversation/submission/task(entry) 建立映射，不再新增一套 job 状态机。
`steer` 必须携带预期运行并核对它，`follow_up` 代表下一次输入；两者不能隐式互换。

首次入场的应用行为：

1. 边界解析、校验长度和枚举；服务端确认调用者。
2. 从规范化的原始命令算指纹；按 conversation + request ID 查既有回执。
3. 相同命令重试返回原回执；不同正文/目标/执行约束报冲突。
4. 对首次入场，在既有写入协调点核对停止代次、会话绑定和预期运行。
5. 冻结项目身份、模型选择和授权快照；原子提交来源、输入、submission 与 task。
6. 提交后唤醒原调度器。通知只作为提示，调度器可以从已提交记录发现待执行任务。

重试不能重新使用“当前模型选择”替换首次冻结的选择。
指纹包含影响命令意义的稳定字段；不包含执行后会变化的状态，也不包含由忙闲决定的 placement。
底层事务仍要核对唯一性，不能只依赖应用层先查后写。
当前 admission 指纹与队列指纹用途不同，实施时明确各自职责，不能假定已有指纹覆盖所有新增字段。

来源与关联信息放入 submission 的规范内容，placed 时投影到 user_message。
修改 `admission.py`、`place_submission()` 和 steer 消费路径，保证排队后不会丢失来源。
不得只把来源放在 SSE 或前端本地消息里。

入场去重不等于任意 shell/网络副作用 exactly-once。
发生崩溃时，继续遵守现有 intent/effect/settlement 和 unknown 不自动重放的约束。

### 5.1 用户、外部控制者与审批的协作规则

控制状态是会话元数据中的小型权限记录，不是第二套任务生命周期：

```text
mode: manual | delegated | paused
controller_actor_id: 当前获得委派的调用者，非委派状态为空
control_generation: 单调递增的控制代次
```

运行状态继续归 task，控制状态只决定谁可以提交下一条命令。
同一会话第一版只允许一个外部控制者；其他已授权客户端可以观察。
桌面、CLI 都能发出用户授权的控制操作，不依赖窗口是否存在。
委派权限绑定会话及本轮根请求；能创建空会话不意味着能自行在新会话开启委派。
用户控制入口与外部控制凭据的能力分别授予，不能相信请求体自称 human 来完成接管或扩权。
这是应用权限约束；同一操作系统用户下拥有任意文件/命令权限的程序不构成可由此协议隔离的边界。

| 操作 | 服务端规定 |
| --- | --- |
| 用户明确开启委派 | 检查预期代次，递增代次，记录控制者并进入 delegated；重复请求只产生一次转移 |
| 外部发送/返修 | 调用者必须是当前控制者，代次必须匹配；有活动运行时 follow_up 排队，steer 核对 expected_run_id |
| 用户在 delegated 时直接发送消息 | 默认先接管：递增代次并进入 manual，取消旧控制者的排队输入，请求停止当前自动执行；用户输入在清理结束后运行 |
| 用户在 manual 时发送消息 | 沿用明确的 follow_up/steer 规则；不从“先别改”等自然语言猜测协议模式 |
| 用户暂停整轮协作 | 递增代次并进入 paused，取消该控制者的排队输入，按原停止路径中断当前自动运行 |
| 用户恢复委派 | 明确授权后进入新代次；不恢复旧请求，也不自动批准过去的审批 |
| 客户端断线或桌面关闭 | 不改变控制权，不停止任务；重连后读取当前代次 |

“接管并放置用户输入”在既有入场协调点提交，不能先接管成功再因进程退出丢失输入。
接管完成不代表旧命令已经清理完毕；新执行要等待原 executor 释放运行所有权。
接管后的现存日志仍可读取，旧控制者不能追加任务、回答问题或触发续跑。
同命令重试若已经在旧代次成功入场，仍返回原回执及当前取消状态，绝不因此重新入场。

问题与审批使用现有 `DurableUserQuestionStore` 和 `ShellApprovalWorkflow`，
由 control service 检查调用者、conversation/run、问题或审批 ID、预期 revision 和控制代次：

- 普通任务问题可在委派授权明确包含回答能力时交给当前控制者；只限已有任务范围。
- 必须由用户提供的信息或决定标记为 `audience=user`，Codex 转告用户并等待，不能编造答案。
- 第一版 shell 审批沿用用户审批；外部控制者能读取待审批详情，不能因为控制身份自动放行。
- 若以后开放外部审批，必须单独授予有范围的能力，仍保留具体命令、cwd、digest 和 revision 校验。
- 问题或审批被人工处理、取消、过期后，迟到回答返回冲突和当前状态，不触发第二次执行。
- 停止或接管与回答竞争时，通过相同控制代次和既有停止/spawn 协调线排序；已经开始的副作用
  不能承诺撤销，待执行的旧续跑必须失效。

新增问题 audience 及回答者信息需要贯穿持久化、投影和恢复；不只给 UI 加一个标签。
同一服务端处理用户与控制者的决定，所有客户端显示最终实际接纳的回答和审批状态。

### 5.2 协议版本、身份与错误

第一版使用明确的协议主版本 `1`，连接时获取版本与 capabilities。
主版本不兼容时拒绝连接；未知操作或枚举明确报错，不静默降级。
可选能力由 capabilities 声明；客户端不根据包版本或是否存在某个文件推测能力。

身份的作用域固定如下：

| 字段 | 含义 |
| --- | --- |
| request_id | 会话内一个确定的修改命令；重试使用相同 ID 和内容，指纹包含服务端调用者身份 |
| submission_id | 已持久化接纳的输入事实；排队和转入历史保持同一身份 |
| run_id | API 对现有 task ID 的名称；一次执行结果必须指向它 |
| entry_id | 聊天历史中的条目；排队输入可以暂时没有它 |
| revision | 指定资源的并发修改前提；不同资源的 revision 不作大小比较 |
| database_id + commit_seq | 持久化历史游标；数据库更换后需要重新同步 |
| instance_id | 当前进程启动身份；断线重连需要核对，不能代替 database_id |
| control_generation | 会话控制权代次；不能代替资源 revision 或消息游标 |

查询先检查调用者访问范围；知道 ID 本身不授予读取或操作权限。
所有修改操作都明确 request ID 和幂等语义，包括控制转移、回答问题和记录验收。
第一版错误形状为 `code, request_id, message, details`；message 仅供显示，客户端按 code 分支：

| code | HTTP 映射 | 客户端行为 |
| --- | --- | --- |
| INVALID_ARGUMENT | 400 | 修正请求；不重试原始错误参数 |
| UNAUTHORIZED / FORBIDDEN | 401 / 403 | 重新建立身份或请求缺少的授权，不自动扩大范围 |
| NOT_FOUND | 404 | 停止等待该资源；不能解释为任务已成功或从未执行 |
| REQUEST_CONFLICT | 409 | 同 ID 内容不同；核对原请求，不能自动换 ID 再执行 |
| STALE_CONTROL / REVISION_CONFLICT / RUN_MISMATCH | 409 | 读取当前状态；修改命令不能盲目刷新前提后重试 |
| RESYNC_REQUIRED | 409 | 重读组合快照，替换旧游标；不改变运行状态 |
| RATE_LIMITED / SERVER_DRAINING | 429 / 503 | 仅在明确未接纳时按 retry_after_ms 退避；保持原请求身份 |
| INTERNAL_ERROR | 500 | 获取 request ID 对应回执与诊断；执行结果未确认时不重发副作用 |

网络超时没有服务端错误响应，不应伪造为 SERVER_DRAINING 或任务 failed。
ControlClient 将它表示为 outcome_unknown，然后按原请求身份查找回执；必要时重试同一入场命令。
服务端维护幂等与准入规则，客户端统一封装连接、等待和恢复查询；各传输适配器不复制这些判断。

## 6. 会话归服务所有，UI 只保存客户端状态

当前 durable conversations 不保存标题、项目和模型绑定；这些在 `ui-state.json` 中。
现有 input route 还会读取 UI session/project 来确认工作区，不能直接作为最终的无桌面入口。

建议将服务使用的会话配置移入现有 durable store 的 conversation 元数据，包含标题、
项目引用、默认模型引用和控制代次。实际单次运行选择仍在 task 入场时冻结。
UI 只保存活动会话、草稿、滚动等客户端状态；不要再成为会话存在性的唯一来源。

这是一个有实际必要的数据迁移，不是顺手重构：

- 通过明确的 schema migration 导入旧会话配置，不清空旧数据库。
- durable 已存在的配置优先，重复启动迁移必须幂等。
- 正确处理 durable 有聊天但 UI 无条目，以及 UI 有空聊天但 durable 无记录的两种情况。
- 调整 cold-start schema 校验并验证旧数据库升级、失败回滚和新数据库创建。
- UI 保存旧快照不能删除服务端新建的会话。

新 `create_conversation` 返回服务端会话和 revision。UI 通过服务端 inventory 与提交发现它。
`/api/new_chat` 继续保持既有“清理会话”的意义，不复用这个名称做创建。

## 7. 一个一致性快照解决聊天与队列之间的竞态

增加 `conversation_snapshot`，在 DurableSession 的同一读协调范围内读取：

```text
database_id、commit_seq、conversation_revision、control_generation
会话基本信息、当前 head、最近历史页及分页游标
待处理输入、运行状态、待回答问题、待审批摘要
```

不得先 GET 历史、再 GET 队列、然后把它们称为同一个快照。
若仍受单服务写锁保护，可先复用现有 `_mutation_line` 的短读范围；
需要跨连接读事务时由 SQLite store 明确提供同一读事务，不能自行拼接不一致的数据。

同步顺序：先建立订阅并暂存事件，再读取带 high-water cursor 的快照；
应用快照后处理更新的持久化提交。事件按 durable entry/submission 身份去重。
快照及分页必须有界；不要为一次刷新加载整段历史和全部输出。

持久化游标使用 `(database_id, commit_seq)`；进程内事件序号还要绑定 instance ID。
会话过滤后的序号有间隔是正常的，不能用“必须 +1”判断漏消息。
纯流式文本事件可合并；最终消息和运行事实从 durable 历史恢复。

Codey 已有 durable replay、重连和历史刷新机制，应扩展它们。
本轮实现公共事件共享编码：同一授权视图与协议版本的事件正文编码一次，多客户端共享不可变结果。
慢客户端必须可检测地重新同步，不能阻塞执行器；限制事件数、排队字节和写入期限。
权限过滤、连接私有游标、回放和正文共享的具体边界见实施细则，不能为了共享编码广播越权信息。

## 8. 等待、停止与异常：必须写清楚的语义

`wait_run` 绑定 conversation + run，并接收已见 revision/cursor，以及明确的 until：

- `change`：目标运行的公共快照发生变化，或终止、需要处理时返回。
- `terminal_or_attention`：普通进度变化继续等待，只在终止、需要处理或超时时返回；CLI 默认使用它。

响应包含 `reason=changed|terminal|attention|timeout` 和同一时刻的运行快照。
游标失效按 RESYNC_REQUIRED 错误处理；运行不存在按 NOT_FOUND 处理。
调用者必须检查 reason 和真实 execution 状态，不能把“wait 调用成功返回”当成任务完成。
attention 包含问题/审批 ID、revision、audience 和可执行的响应操作。
优先级为 terminal、有效 attention、符合 until 的 changed、timeout；不返回已解决问题的旧 attention。
查询可带 `seen_attention_token`，表示已见的待处理集合身份和版本；相同集合不再次触发 attention，
仍在快照中保留 pending 状态。初次查询不带该 token；新增或更新待处理项会产生新 token。
查询 timeout_seconds 限制在 0 到 30 秒，0 表示立即读取；长任务通过后续有界等待继续观察。

服务端先订阅变化、再读状态；已终止则立即返回。等待期间不持有 durable 写锁。
等待醒来后重新读真实状态，不能把“收到任意事件”当成该 run 完成。

```python
def wait_run(query, *, changes, read_snapshot, monotonic):
    """算法示意；changes/read_snapshot 是待实现的服务端端口。"""
    deadline = monotonic() + query.timeout_seconds
    with changes.subscribe(query.conversation_id) as subscription:
        while True:
            snapshot = read_snapshot(query.conversation_id, query.run_id)
            if snapshot.terminal:
                return {"reason": "terminal", "snapshot": snapshot}
            if snapshot.needs_attention and snapshot.attention_token != query.seen_attention_token:
                return {"reason": "attention", "snapshot": snapshot}
            if query.until == "change" and snapshot.revision != query.after_revision:
                return {"reason": "changed", "snapshot": snapshot}
            remaining = deadline - monotonic()
            if remaining <= 0:
                return {"reason": "timeout", "snapshot": snapshot}
            subscription.wait(remaining)
```

这里的 revision 是目标运行公共快照的版本，变化范围包括其任务状态、问题和审批，
不能仅复用 task revision 而漏掉审批更新。第一版可用这些持久化投影的版本组成不透明 token，
避免另建可独立漂移的版本计数器。无关运行的更新可以唤醒订阅，但不满足 changed 条件。

调用方超时或连接断开只停止观察，服务端任务继续。
客户端没收到发送回执时，按原 request ID 查询或重试原命令，不能生成新 ID 再发一遍。
HTTP 查询有并发数和等待时间上限；不要让无限 wait 请求耗尽线程。

`stop_run` 返回“取消请求已提交”，不冒充 shell 清理已经结束。
需要停止整轮协作时，应有明确的会话控制停止操作：在同一入场协调点递增持久化
control_generation、使旧控制请求失效并取消其待执行输入，再调用原运行取消路径。
它不能依赖 Codex 在断线后记住“用户已经停止”。恢复控制需明确的新授权，不自动恢复旧代次。
精确 run 取消与整轮协作停止要在 API 和 UI 行为中区分。

task 已记录 cancelled 但进程/工具仍在清理时，快照明确包含 cleanup_pending。
wait 的 terminal 表示持久化终态；需要安全重启的调用方还必须等待运行所有权释放，
不能把 terminal 回执当成所有进程都已停止的证明。

关闭服务也与用户停止分开：先拒绝新入场，再按既有安全恢复语义保存/中断运行，
最后释放 lease；不能先释放 lease 再让旧 worker 继续写。

### 8.1 任务观察与 Codey 诊断分开

任务观察读取原有执行事实；Codey 诊断说明进程及执行器的可观察状态，不改变任务结果。
第一版 `get_health` 与诊断快照分开表达：

| 信息 | 用途及限制 |
| --- | --- |
| instance_id、PID、启动时间、运行代码身份 | 关联本次进程与候选版本；PID 存在不证明任务有进展 |
| HTTP 可响应、服务处于 ready/draining | 说明控制入口是否可用；不能据此报告执行器健康 |
| 当前 task、阶段、工具/模型请求开始时间 | 区分执行、等待模型、等待命令、等待用户和清理 |
| last_progress_at、last_committed_seq、最近工具回执 | 反映实际观测进展；超过预期只标记 suspected_stall |
| 后台组件 heartbeat 和已捕获异常引用 | heartbeat 由被观察组件提供；独立 HTTP 线程活跃不冒充执行线程活跃 |
| evidence refs、记录完整性、截断标记 | 支持取证，明确观察缺失；缺少日志不证明没有异常或副作用 |

进展时间、组件心跳是有界的诊断观察，不作为完成证明，也不每秒写入 durable 聊天日志。
等待模型/外部命令期间缺少任务进展可以正常；诊断需要同时呈现阶段与 elapsed，
第一版不因一个统一超时阈值就自动重启或重新执行。

诊断关联字段包括 request/submission/run/instance、阶段、工具调用身份和时间。
沿用已有 RunTrace、receipt、managed output 和 provider failure 记录，补充缺少的关联信息；
复用现有诊断脱敏与有界输出，不复制一套“控制日志数据库”。
运行结果、日志片段和错误堆栈分别返回，避免将完整 prompt、凭据或网页正文灌入默认响应。

服务不可达时，外部 CLI 仍能读取进程退出信息和已有落盘诊断，明确标注它们是最后已知记录。
不能要求崩溃进程通过自身 HTTP 接口证明崩溃；控制客户端也不自动启动另一个执行器覆盖现场。
无响应且进程未退出时，第一版允许外部工具保留现有现场并人工决定下一步；
主动线程栈采集可另行扩展；本轮自动重启只通过实施细则中的受控版本切换与回退流程，
不依据 suspected_stall 自动杀进程或重跑未知副作用。

## 9. 结果、验收和自修

不要把 `RunDetailsSummary.rows` 的 UI 文案解析成机器状态。
control result 直接从现有 task、submission、receipt、effect 和 artifact 投影：

- execution：completed/failed/cancelled/running 等真实任务状态；
- attention：question/approval/none；
- delivery：已确认/失败/未知；
- answer、changed files、验证回执引用；
- evidence refs、摘要是否截断、运行代码身份。

现有队列 `_state_for_task()` 会把 failed 投影成 unconfirmed；这是现有展示契约，
外部机器接口应读取真实 task.status，明确失败与未知投递的差别。
默认仅返回有界摘要；详细 evidence 以 session/run 所属、digest 和分页约束读取。
证据丢失或输出截断必须明确标注，不能以“没有找到日志”证明没有执行。

Codex 的验收结论作为可见审查记录保存，并绑定 candidate digest/文件身份与验证回执。
之后文件改变，旧结论成为过期；不能只加 `passed=true` 覆盖 Codey 原完成事实。
发送“修改意见”创建正常下一轮任务；单独“记录验收通过”不应触发模型新运行。

Codey 自修仍是普通项目任务。复测需要记录实际运行的版本、启动时 build/源码身份、
PID/启动实例及原始复现步骤。包版本号或当前磁盘文件内容不能单独证明运行代码已更新。
若修改影响执行内核，先保存现场，在隔离候选中验证，再受控重启和复测。
Codey 无法接任务时，外部工具读取现存诊断并直接修复，不依赖 Codey 自己返回错误。
本轮由独立 launcher 自动协调已授权的候选验证、实例替换、重连与失败回退；
控制接口不会只返回“请手动重启”就宣称完成自动版本切换。

自修复测保留一组可核对的记录：旧 instance 和复现证据、候选代码 digest、验证命令及回执、
新 instance 和启动代码身份、原复现步骤的实际结果、失败时可恢复的已知版本。
停止旧实例并等待清理/lease 释放后，才能启动候选实例使用同一 state home。
源码变化会使旧实例标记为 code_changed_since_start；这只说明需要重启，不证明新代码已加载。
模块可能延迟导入，不能假定 Python 旧进程完全不会看到磁盘新源码；因此涉及运行模块的修改
优先在隔离候选中进行，避免工作进程混用不同代码版本。

v1 的返修循环由 Codex 承担，不在 Codey 内再创建一个“监督 Codex”的模型循环。
Codey 现有内部 review 保持既有语义；若以后增加 external review 模式，必须明确区分
内部完成与外部验收，不能为了省一次 review 静默移除当前完成门。

### 9.1 外部返修的预算和退出条件

预算是本轮委派的控制约束，不添加新的模型监督循环。
开启委派时保存本轮根请求身份、允许的返修次数、总历时限制和可选的用量限制；
每次自动返修关联原运行与根请求。重新连接和客户端重试不重置预算。
这些信息使用原有控制元数据和执行/审查记录投影，不另存一套 task 终态。

建议在界面中明确配置；例如“最多返修 3 次、总时长 30 分钟”仅是可调整的起始设置，
不是已证明最优的默认值。返修次数从首次交付后的修改任务计数，初始任务不占返修次数。

- Codey 服务可核对已接纳返修次数与总历时，在同一准入协调点拒绝超限的新返修。
  总历时默认包括等待用户和服务停机，持久化 expires_at；进程内等待使用 monotonic 时钟。
- 达到已授权的历时上限时，按既有取消路径请求停止正在运行的任务并保存现场；
  超限后不因重连重新启动。预算状态在服务重启时根据持久化记录恢复。
- Codex 的实际 token/费用只有外部宿主提供真实用量时才能统计或限制；缺失值是 unknown，
  不能记成零，也不能承诺 Codey 服务可以强制限制宿主自身的消耗。
- 只有有权限修改的预算转移操作才能增加额度；外部控制者不能通过换会话或换根请求自动续费。
- 连续两次返修后，候选内容 digest 与未通过项证据签名均未变化时，报告 no_progress，
  停止自动追加并等待用户处理；签名只用于循环退出，不能代替语义验收。
- 验收通过、控制权被接管、明确阻塞、预算耗尽和无进展都结束本轮自动派发。
  返修次数/预算耗尽表示未完成，不能包装成验收通过。

Codex 等待优先使用 terminal_or_attention，桌面继续接收完整事件。
默认结果仅包含有界摘要、验证回执和差异引用；长日志按需分页。
相同 attention revision 只在新出现或变化时向宿主提交处理，避免反复读取同一待审批状态。

对照测量使用相同任务、初始代码、验收要求和执行环境：分别记录 Codex 独立执行与委派执行的
宿主用量、Codey 连接用量、返修次数、总耗时和最终正确性。不同连接缺少费用数据时单独报告，
不能用“Codex 用量下降”直接等同于总费用下降。

## 10. 用可验证指标定义“更好”

可以在这个特定目标上做到契约更适合、状态更少，不先宣称整体优于三个项目。
必须通过下列故障场景，而不只是 happy path：

1. 无桌面创建并完成任务；之后打开桌面，原聊天完整出现。
2. 桌面与 CLI 同时观察同一运行，消息和终态一致。
3. 提交已持久化但响应丢失；相同请求重试只得到一个 submission/task。
4. 同 ID 修改正文或目标，明确冲突，不静默忽略新要求。
5. 重试期间默认模型设置已变化，仍返回原任务及原选择。
6. 输入正从 inbox 转入历史时读取快照，不遗漏也不重复。
7. 任务在 wait 订阅前已结束，立即读到终态；不同运行完成不能唤醒成错误结果。
8. wait 超时、客户端退出、桌面关闭，均不隐式取消执行。
9. 用户停止与外部发送竞争；停止提交后旧代次不能再入场。
10. 崩溃发生在 intent/effect/settlement 各边界，未知副作用不自动重跑。
11. 慢订阅者发生溢出，执行和健康订阅者仍可推进；重连后事实可恢复。
12. 服务在 admission/drain 竞争中关闭，不遗失已确认接收的请求，不出现双执行所有者。
13. 修复 Codey 后，使用新的运行身份复测原问题；旧验证不能证明新候选。
14. UI 旧状态保存、数据库迁移失败、发现文件陈旧时，都不丢会话、不错误连接新实例。
15. 用户接管与旧返修/问题回答/审批续跑同时发生，只有排序在接管前的合法动作生效，
    接管后旧代次不能启动新副作用；已经开始的动作准确显示为运行或清理中。
16. 两个客户端同时回答问题或处理审批，迟到决定得到冲突，不能执行第二次。
17. change 模式返回进度时 reason=changed；terminal_or_attention 不因普通进度提前返回。
18. HTTP 可响应但执行器无进展，诊断分别报告入口可用和 suspected_stall；不谎报任务健康。
19. 返修额度并发争用、断线重连和服务重启不重置预算；未知宿主用量仍标为 unknown。
20. 涉及运行模块的自修使用隔离候选，复测记录与新实例对应；原问题未通过时保留失败证据。

RPC/MCP 一致性、广播性能与自动版本切换还须通过实施细则中的专项验收矩阵。
验收证据分别记录功能覆盖、故障正确性、性能和真实用量；不能用一种结果代替其他结果。

先做无需模型的协议/事务/故障注入测试，再做桌面与服务联合测试，最后跑一次真实
“任务 → 独立验收 → 返修 → 再验收”。真实模型测试另行记录环境和成本，不混入普通单测。

性能至少测三项：一次任务的 Codex 输入/输出用量、返修次数、提交到 UI 显示的延迟。
再测慢客户端下内存和执行推进。只有实测后才能判断是否更省、更快、更健壮。
