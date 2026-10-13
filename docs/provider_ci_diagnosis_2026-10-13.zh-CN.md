# 模型、CI 与审批续接排查（2026-10-13）

本轮已修复可定位的客户端缺陷，但不能把免费服务写成全部稳定通过。
首次完整实测覆盖 10 个当前免费模型、14 个选择组合、每组合 3 个生产入口用例，
结果为 **36/42**。对失败的 6 项单独复验，4 项通过，Ling thinking=true 的聊天和
审批用例仍失败。两次记录都保留，复验没有覆盖或删除首次失败证据。

## 1. CONNECTION_REVISION 的日期

日期不是过期日期、API 参数或服务端要求。它原来是人为命名的连接版本，供
状态缓存、访问观察、限流协调和冻结的模型选择使用。已改为 `zen-public-partner`。
这些身份隔离和校验仍有实际用途，不能一起删除。旧 run 如果冻结了旧标识，
其恢复校验仍会拒绝旧选择；没有自动改写用户的旧运行或协议历史。

## 2. Big Pickle 只能聊一句，随后 KeyError: index

原 SSE 解析器直接使用 `call["index"]`。上游省略 index 时，模型返回的工具调用
尚未进入执行内核就会触发 KeyError，不能据此判断模型没有工具能力。

SSE 组装已迁到 `providers/api_stream.py`：有 index 时用 index，没有 index 时用
完整 call id；没有 index/id 的续片仅在已有唯一调用时可归属。多个调用的模糊
续片、冲突 id、非法 index 会明确失败，不猜测身份或执行部分调用。同一 call id
重复出现时保留其原值，不把 id 拼接成另一个 id。

Big Pickle 在真实聊天、文件读取与结果回传、批准 shell 后续接三项均通过。
另一个三轮探针也通过：读取 fixture → 回传结果 → 下一轮复述 fixture 标记。

## 3. Ling / Step 的 429 与 OpenCode 对照

实际安装目录为 `C:/Users/Administrator/AppData/Local/Programs/@opencode-aidesktop`。
从 app.asar 包元数据确认版本 **1.19.0**，并读取了该安装版的核心实现。
也核对了截图中「问候对话」的原始记录：确实是 Ling 3.1 Flash Free，先问候，
再调用 bash 查询 Python，随后得到 `Python 3.12.8`。该组合命令的整体退出码为 1，
不能把整个组合命令说成 exit 0；其中的版本输出依然是有效观察。

控制实验将同一请求正文和同一组 Codey 连接头经 Node fetch 与 Python 发送。
观察到 Node 200、Python 持续 429；进一步删 reasoning、删 parallel 标志、调整
输出预算、换工具集合、合并用户消息、使用安装版请求正文，均不能稳定修复
Python 路径。这个证据支持传输路径兼容差异，**尚不能证明是某一种 TLS 指纹、
Clash 路由规则或请求字段导致**。系统确有 Clash TUN / Fake-IP；没有修改其配置。

Zen 已使用 Node fetch 传输，只运送原请求与响应字节，复用共享 JSON/SSE 解析、
错误处理、总期限与工具执行内核。Node 优先使用既有 Playwright 分发的运行时，
不需要下载浏览器。每次请求的 helper 都受取消控制，退出时关闭进程与管道；
安装包已确认包含 `.mjs` 资产。

OpenCode 对完整的可重试服务拒绝实行退避；Codey 原先把
`429 server_error: Upstream request failed: Endpoint is unavailable.` 当作未知错误，
不重试。现在只对这个明确拒绝以及既有已确认短时限流实行有界退避，最多首次
请求加 5 次重试，仍受原总期限限制。额度耗尽、资格拒绝、模糊错误与提交后的
连接中断不由此获得重试资格。每次完整拒绝保留 upstream request ID。

提交前连接失败与提交后中断也已分开。Node 通过 Undici 的
[sendHeaders / connectError 诊断事件](https://github.com/nodejs/undici/blob/main/docs/docs/api/DiagnosticsChannel.md)
报告发送边界；仅有积极的连接失败证据且没有发送证据时，才认定未提交。
Python 在把请求交给异步 helper 后、收到发送回执前取消，按提交不确定处理，
不会因 IPC 回执延迟误认成未发送并重放。

同一失败请求稍后能成功，安装版核心运行同类请求也出现过相同 429。
因此「模型一直断掉」和「全部是客户端 bug」都不能从现有证据推出。
目前尚未解决的是 Ling thinking=true 的服务拒绝/连接中断，不能承诺永久可用。

## 4. Python 3.13 的 partial-frame 测试

生产代码存在两个可确认的问题：

- `_open_request_sync` 对普通 HTTP 请求也初始化 HTTPS 的系统证书上下文。
  Windows 证书初始化和线程调度可能耗掉测试仅 150ms 的总期限，测试甚至未
  进入响应体阶段。现在仅真正打开 HTTPS 时创建其上下文；测试同时断言普通
  HTTP 不调用证书初始化。
- 普通 HTTP handler 只更新内部 submission 字典，没有更新 AttemptControl。
  已发送的请求可能被取消线程错误归类为未提交。HTTP 与池化发送路径已修复。

另一次全量实测暴露了 Stop 等待静默错误响应体的竞态：urllib 交接响应时可能
清空连接对象上的 socket 指针，而响应尚未登记给取消控制器。现在连接建立后
直接保留实际 socket，使该交接窗口也可 shutdown。

这些是跨版本缺陷，3.13 的耗时变化是触发条件，不能称为 3.13 独有逻辑错误。
以独立官方 Python 3.11.9 / 3.13.9 解释器和当前 3.12.8 验证相关回归；
3.11 / 3.13 各 24 项通过。额外解释器放在忽略的测试目录中，没有修改系统默认
Python。它们使用了既有纯 Python 测试依赖和各自 ABI 的 greenlet，因此不是
完整复刻 GitHub CI 的依赖环境。

## 5. project continue 的 CI 失败

该持久化测试使用裸 `AppContext()`，会共享 pytest 进程的默认 conversation、
checkpoint、facts 等目录，并反复使用 `session-1`。本地单项能过不能证明它与
完整套件顺序无关。已使用仓库既有 `make_app_state` 隔离其持久状态。

恢复逻辑已有「同连接的明确 resume 不强制开新聊天」规则；本轮没有删掉
`assert_not_called` 或以放宽断言伪造通过。当前这项在 3.11 / 3.12 / 3.13 的独立
相关回归均通过。原 GitHub 那一次没有完整 trace，不能把状态共享直接说成其
唯一已证明触发链；它是已确认并修复的测试隔离缺陷。

## 6. MiMo 网页的两次命令与错误显示

已读取用户提供的附件、对应 session 的运行日志、审批事实和结果资产，只作
证据，不把附件内的工具合同或历史指令当成本轮指令。

实际存在两个不同审批 id 和 source run。两个受校验的结果资产均为
`ok=true, status=exit, exit_code=0, output=Python 3.12.8`：**两次真的执行成功了，
并非只有重复显示**。

续接丢结果的原因是：HTTP 调度层构造了包含执行结果的 continuation，但
`TaskSubmission.from_input(ResolveShellApproval(...))` 把 task 换成了固定文本
`Resolve the shell approval`。恢复阶段只为 native call id 构造回传行，网页文本
调用没有 native id，因而结果也没有从该路径送回模型。模型再次申请相同命令，
最后用 unittest 判断 Python 存在，却说版本不知道。

现已从已验证的持久审批结果重建文本 continuation，包括成功输出、exit code、
拒绝与禁止无授权重试说明；不会重新执行命令。单独测试覆盖同意/拒绝两条路径。

`Failed / exit 0` 则是 UI 持久化缺陷：`UiStateStore.MESSAGE_KEYS` 缺少 shellStatus，
保存/恢复时静默丢字段，前端把未知状态显示成 Failed。已保留该字段，并将历史
缺失/未知状态显示为 `Result status unknown`，不会把未知状态谎报为成功或失败。
旧 UI 中已丢失的字段没有自动回写或猜测恢复；原始执行资产没有被修改。

最初 `run python --version` 被项目命令白名单拒绝，是 run/shell 的既有权限边界，
不是 Python 未安装。`python -m unittest` 的「0 tests」可以说明解释器启动了，
不证明版本查询完成，也不证明任何测试通过。

`Local update paused` 属于后台本地维护警告。对应保存的 UI 只剩此通用文案，
trace warnings 为空，现有证据不足以进一步确定该次维护失败的具体阶段。
它不能作为 Python 命令失败或 Python 未安装的依据。

## 7. 体积与监控

`api_transport.py` 当前 **971 行**，`api_stream.py` **114 行**。拆分边界是 HTTP
连接/取消/响应读取与 SSE 事件/工具调用组装，确实各自有职责；没有按 1000 行
机械切段。Node 适配仅在可移除的 Zen 包，Python/JS 分别为 157 / 56 行。

新增 `python -m tools.source_size_inventory`，扫描 git 已跟踪和未忽略的新源码，
覆盖 codey、tools、tests、.github，列出达到阈值的文件；它是审查清单，1000 行
不是强制拆分或构建失败条件。Windows/Linux CI 都会打印此清单并检查新 `.mjs`
的语法。现有大文件还有 evidence_ledger、trace、runtime、
controls、changes、Ghost 等，未发现职责拆分证据前不建议一起大改。

live gate 指纹已统一使用现有 source_fingerprints，递归覆盖生产源和工具子目录，
包含新 SSE/Node 文件及之前手工清单漏掉的 HTTP、请求策略、Zen 可靠性/usage、
shell gate 脚本。未启用的可选连接继续从其他连接的指纹排除。还修复了 shell
gate 状态目录归档冲突、嵌套 recorder 重复记录及策略重试被错误计为新逻辑发送。

新增可复用真实免费模型验证入口：

```powershell
python -m tools.zen_free_model_gate --run-dir .e2e-artifacts/zen-free-new-run --timeout 120
```

目录必须是新的。用例只批准自身明确的无害打印命令，并校验审批只消费一次、
工具结果只回传一次、原协议窗口没有被换掉。完整矩阵与失败复验在忽略的
`.e2e-artifacts/zen-free-production-node-v2` 和 `zen-free-failure-verification`。

## 验证结果与边界

- 全量阶段：8558 passed、7 skipped、1510 subtests passed。
- 当前必跑 machine contracts：1123 passed、14 subtests passed、无跳过。
- 最后 UI/审批/Node/recorder 修补后的相关回归：35 passed。
- Ruff、全树 mypy、Pyrefly、修改的 JS 语法检查通过。
- wheel 已验证包含 Node helper 与新 SSE 模块。

全量阶段完成之后仍有小范围提交边界、UI 持久化和 recorder 修补；它们由后续
当前 machine contracts 与专项回归验证，不能把旧的全量统计说成最后一次所有
源码快照的全量统计。未直接运行 GitHub 远端 CI。

免费模型矩阵仍为未全绿，失败复验没有掩盖首次失败。Ling thinking=true 的
剩余两项需要继续在服务/网络边界调查，或取得对应 upstream request ID 的服务端
诊断，当前客户端代码无法证明私有后端返回 Endpoint is unavailable 的具体原因。
