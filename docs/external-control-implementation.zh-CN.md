# 外部控制 v0.3 实现与使用

日期：2026-10-22。本页说明当前工作树的实现；两份 v0.3 设计稿保留方案依据，具体接口以
`codey/control/protocol.py` 为准。本轮没有改变 Codey 产品版本号。

## 统一执行入口

`codey/app/service.py` 提供窗口独立的本机服务，每个 state home 由 OS lease 保证一个所有者。
桌面关闭后服务继续运行。桌面、浏览器和控制客户端共用原有输入队列、DurableSession、
scheduler 和执行内核，没有另建智能体执行器或任务数据库。
桌面重新启动服务时优先使用 active-release 指定的不可变版本，并复核摘要；
显式运行源码目录下的 `serve` 仍表示启动该源码版本。

桌面客户端自动启动服务时，会先在 `startup.lock` 下检查当前源码、物化前端并生成
不可变 runtime 快照，再执行启动或版本切换。并发启动会共用这条串行路径；只有健康检查
确认返回相同的 `release_id` 后才写入 `active-release.json`。服务本身不再从源码目录
推导 release identity，也不会在运行时隐式 bootstrap 当前源码。

`codey/app/control_service.py` 统一授权、去重、委派代次、人工接管、停止、观察和验收。
配置与回执是原 durable store 中的被动条目，不进入模型上下文；聊天消息和任务状态仍使用原权威写入路径。
服务通过 DurableSession 的会话枚举和提交读取接口观察持久事实，不直接绕过 session 访问底层 store。
HTTP handler 必须显式绑定 AppContext，移除了原模块级 STATE、隐式 get_state 和旧提交包装器。
测试通过自己的显式 context fixture 注入，不在生产代码里保留测试兼容入口。

启动与连接示例：

```powershell
python -m codey serve --state-home E:\codey-state --allow-project E:\my-project
python -m codey ui --state-home E:\codey-state
python -m codey control health --state-home E:\codey-state
python -m codey rpc --state-home E:\codey-state
python -m codey mcp --state-home E:\codey-state
```

RPC 与 MCP 连接已启动的服务，不暗中启动第二个执行器。MCP 是可选依赖：
`python -m pip install -e ".[mcp]"`。外部 MCP 客户端配置 stdio command 为 Python，
args 为 `-m codey mcp --state-home E:\codey-state`。
服务只绑定 loopback；私有凭据文件分开保存 operator 与 agent 能力。外部智能体不会因为自称 Codex 获得用户审批能力。

## 委派、观察、返修

1. `create_conversation` 创建持久会话，指定项目、provider、model_selection 和预算。
2. `send_message` 携带稳定 request_id、conversation_id、control_generation；返回入场回执，不能当作完成。
3. `wait_run` 可等待终态、待处理事项或 revision 变化。超时和观察者断开不会停止执行。
4. `get_run_result` 返回实际任务状态、回答、变更、验证记录、候选摘要及运行实例身份。
5. 控制者直接检查文件、diff 和必要行为，再 `record_review` 记录与 candidate_digest 绑定的独立验收。
6. 需要返修时，同一聊天 `send_message` 指定 in_reply_to_run_id；重新检查后记录新验收。

同一 request_id、相同内容重发会取得原回执；不同内容报 REQUEST_CONFLICT。
`steer` 必须带 expected_run_id；返回的 run_id 指向被干预的执行，另有 input_run_id 标识输入消费。
暂停与用户接管会推进代次，旧修改请求失效。停止只请求取消指定运行，cleanup_pending 消失前不声称清理完成。
外部控制者不能回答交给用户的命令审批。max_seconds、max_revisions、max_turns 限制循环；
独立验收仍由调用者负责，接口不会把 Codey 自己的完成总结当成通过依据。

默认观察返回摘要，详情通过 `read_run_evidence` 按页读取。详细条目过大时给出有标记的 JSON 预览；
本地原始记录仍保留。suspected_stall 是待调查信号，不是已经证明执行器卡死。

外部消息来源和返修关联随原聊天条目持久化；桌面从服务端消息恢复正常聊天，来源显示 Codex。
新聊天进入侧栏，不抢走正在浏览的聊天。未确认投递时，原文保留在输入框，Check status 位于输入区域。
服务替换保留同一端口；浏览器会话使用与 API 凭据隔离派生的标识，一次性启动链接仍在进程内失效。

## RPC、MCP 与广播

`control/protocol.py` 是命令目录，HTTP、CLI、JSONL RPC 和 MCP 共用校验与字段定义。
RPC 使用有界 UTF-8 LF 帧、请求 ID、有限并发与单写队列；取消 RPC 观察不等于取消模型任务。
MCP 使用官方 Python SDK，提供结构化结果和工具注解，不把 SDK 类型传入内核。

`app/public_event_feed.py` 对同一公共视图编码一次并缓存不可变字节，按会话范围过滤后分发。
每个订阅同时限制条目数和字节数；慢订阅溢出只关闭自己，不能阻塞任务生产者。
内部事件必须经过显式公共字段白名单。重连通过持久记录和一致性快照补齐。
目前用编码计数、队列边界和隔离测试验证机制，没有进行跨项目吞吐量排名或节省 token 的对照测量。

## 自动切换与恢复边界

operator 调用 `prepare_release`，生成带摘要的 runtime 快照，在单独验证副本中运行检查，
随后用隔离数据库副本启动 verify-only 服务，不能触发生产任务。验证命令不能改写不可变候选。
失败验证也留下回执，同一请求不重复执行有副作用的检查命令。

`activate_release` 绑定候选摘要和旧实例身份，由独立的 stdlib launcher 执行：
记录 accepted → drain → 等待运行和审批清理 → 关闭旧服务 → 启动候选 → 检查 → 开放入场。
切换日志落盘，`get_upgrade` 可在 HTTP 服务停机期间直接读取。旧版快照在任务修改源码之前保存。
候选无法启动时使用原不可变版本启动，沿用当前数据库，不回滚已经发生的执行事实。

本轮自动切换支持**依赖与数据库 schema 不变**的 runtime 更新。改变这两者会在 prepare 阶段拒绝，
不能把任意迁移自动回滚描述为已实现能力。协调器异常中断后的日志和独立 runner 保留，
没有另外安装系统级常驻恢复服务；需要人工依据 recovery_required 证据恢复时，明确报告。
源码已修改、修复版本已启动、原问题复测通过是三个独立条件。

## OpenCode 模型地址动态发现与可拆除性

`providers/zen/catalog.py` 直接读取官方目录 `https://models.opencode.ai/api.json`，
使用 ETag 和 60 秒按需刷新，缓存目录原始事实。模型地址、协议、上下文上限、免费价格均来自目录，
不依赖 OpenCode.exe、其本地进程、安装位置、SDK、账号凭据或另一个 models 可用性索引。

这次连接故障的实际原因是目录已切换到 `/inference/openai/v1`，新地址没有 `/models` 路由；
仅更新常量再拼 `/models` 仍然会失败。现在模型发现只用官方目录，生成请求使用每个模型声明的地址。
同一已入场任务固定 endpoint revision；目录变化后旧选择明确失效，新任务重新选择，避免悄悄切换运行中的传输。
目录刷新失败会标记 stale/error 并保留有效缓存，不静默将错误页当作空模型列表。

自动接受的范围是已批准 OpenCode 域名下的 HTTPS API 路径；任意新域名、重定向、未知协议或目录结构
不能自动获得凭据。通常的地址路径和模型变化无需更新 Codey；新的信任域或不兼容协议仍需要评估与适配。
合作适配集中在 `providers/zen/` 和 API_CONNECTIONS 的一处注册；拆除测试验证本地 API 仍可使用。

## 参考实现与验证

采用 Pi 的薄协议适配与入场/完成分离、OpenCode 的共享服务和有界公共事件分发、Codex 的身份绑定入场与关闭协调。
本地参考文件和设计取舍保留在两份设计稿；没有直接复制三个项目的执行器，也不宣称未经测量的整体领先。

新增回归覆盖去重、内容冲突、停止代次、迟到请求、提问、独立验收、证据上限、RPC/MCP stdio、
慢订阅、实例归属、真实进程切换、候选损坏拒绝、启动失败回退、验证隔离和浏览器会话保留。
实机使用独立 state home 和公开 Zen 模型创建 greeting.txt，独立检查初稿后追加内容要求，再次检查最终字节。
产物位于本地忽略目录 `artifacts/external-control-live/`；不把凭据和完整运行现场提交到仓库。
其中返修因 max_turns 限制结束，但最终文件内容通过独立检查；不把模型自身总结视为验收依据。
