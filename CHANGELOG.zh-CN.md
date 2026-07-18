# 版本更新记录

[English version](CHANGELOG.md)

这里记录 Codey 从最早版本到现在的发布历史，最新版本排在最前面。

## 0.1.59 - Package Manager Setup 提示

- 可靠性：setup context 现在和可信验证发现使用同一套 Node package manager
  识别规则：先看 `packageManager`，再看当前 lockfile，再向上找 lockfile，
  最后才退回 `npm`。
- UX：setup 提示现在会给出更具体的安装命令，比如 `pnpm install`、
  `yarn install`，或 `npm ci or npm install`，不再只是泛泛地说 package install。
- 一致性：shell 审批后的 follow-up 现在复用统一的 verification candidate
  formatter，带 cwd 的检查命令格式会和 Project Map / Verification Map 保持一致。

## 0.1.58 - 成功改动检查保留工作目录

- 可靠性：successful-change facts 现在会保留本地检查命令的工作目录，所以 scoped
  验证会渲染成 `backend/: npm test`，不再丢掉“这个检查是在 backend 里跑的”。
- 兼容性：旧的 `successful_changes[].checks` 字符串格式仍然可以读取，并默认
  `cwd="."`；新写入会使用 `{command, cwd}` 结构化 check 记录。
- 安全：非检查命令、敏感命令和不安全的工作目录仍然会被过滤，不会进入持久项目事实。

## 0.1.57 - 验证候选命令来源统一

- 可靠性：Project Map 和 Review Verification Map 现在都从同一套可信的
  `verification_policy` 发现路径接收候选检查命令，不再由 Project Map 自己
  根据 manifest 另猜一套命令。
- Review：Verification Map 只会把唯一选中、和本次改动相关的检查命令标为
  `Recommended local check candidates`；没有唯一选择时，其他命令仍会放在更弱的
  broader candidate 标签下。
- 清理：直接调用 `render_project_map()` 不再推断候选命令。需要生产上下文的
  manual probe 现在改用 `ProjectTaskContextBuilder`，让评测脚本和真实 Writer
  路径保持一致。

## 0.1.56 - Composer 文件夹文案收敛

- UX：无项目聊天里的 composer context 现在始终显示 `Choose folder`，即使输入框里有
  草稿也不再显示更长的发送文案。草稿发送行为仍然只通过同一个明确的文件夹点击触发，
  但不会占用可见 composer 文案。
- 安全：项目访问行为不变。用户仍然必须明确选择文件夹；在无项目聊天里按 Enter
  仍然只是普通聊天发送。

## 0.1.55 - 草稿接入项目发送

- UX：普通 New Chat 现在可以从输入框上方的项目上下文原地接到一个项目文件夹。
  如果输入框里已有草稿，同一个明确的文件夹点击会保留草稿，并在用户选完文件夹后
  用同一个 session 发送。
- 连续性：chat -> project 的切换现在会保留之前普通聊天里的 handoff 和最近可见对话事实，
  Writer 不会丢掉“先讨论方案，再落到项目里执行”的上下文。
- 安全：没有自然语言意图识别，也不会自动开放项目访问。用户必须明确点击文件夹上下文；
  在无项目聊天里按 Enter 仍然只是普通聊天发送。

## 0.1.54 - 可信验证发现增强

- 可靠性：改完代码后的可信验证发现现在能识别更多本来就已被本地 `run`
  工具允许的安全检查，包括按 `packageManager`/lockfile 选择的 package
  scripts、`pytest` 配置、`tests/` unittest discovery、`ruff`/`mypy` 配置，
  以及简单安全的 Makefile target。
- 选择策略：completion 阶段的验证候选现在有一层很小的命令优先级，避免发现
  `test`、`typecheck`、`lint`、`build` 和 Makefile target 后让常见项目变成
  ambiguous。更具体的生态命令会优先于 Makefile fallback。
- 安全：没有 UI 改动，没有自动安装，没有扩大 shell 权限，也没有新增自动执行行为。

## 0.1.53 - CDP 浏览器预热

- UX：启动 UI 后会排队执行一次 best-effort 浏览器预热；当没有任何 provider
  标签页可见时，Codey 会准备受控 CDP 浏览器并打开 DeepSeek、Qwen、MiMo 和 GLM。
- 安全：预热不会检查登录态、不会发送测试消息、不会改 UI，也不会绕过 provider
  supervisor 的健康状态过滤。已有 provider 标签页会被复用，不会重复打开 provider 页面。
- 可靠性：预热运行在统一 browser worker 上，使用较短超时，不复用无关外部 CDP
  浏览器；慢加载但已到达目标 URL 的 provider 页会保留，失败的空白预热页会关闭。

## 0.1.52 - Provider Send Loop 收拢

- 可维护性：新增共享的 provider send-loop 小原语，收拢 response watch 生命周期、
  响应稳定状态、completion flow 检查、flow response 读取和标准 timeout recovery。
- 范围：GLM、Qwen、DeepSeek 和 MiMo 都已迁到共享 helper，但各自的提交、
  completion、重试和 response 读取差异仍保留在对应网页 driver 内。
- 安全：没有 UI 改动，没有 selector 改动，没有 provider 基类，也没有引入宽泛的
  `run_send_flow` callback 框架。

## 0.1.51 - Shell 审批后续提示

- 后续提示：已批准的 shell 结果现在会附带短小的内部提示，提醒 Writer 注意命令失败、
  输出截断、PATH 刷新、dev server 歧义、发布确认，以及相关的可信本地检查。
- 安全：这些提示不会自动执行命令、重试安装或改变 UI。Writer 仍然必须显式请求下一次
  工具调用或 shell 审批。

## 0.1.50 - Setup-aware Shell 审批

- UX：Shell 审批卡现在使用中性的 `Approval required`，并针对依赖安装、系统安装、
  外部源码获取、发布、开发服务器和普通 shell 命令显示简短风险说明。
- 上下文：用户批准 setup 类 shell 命令后，Codey 会给 Writer 回传一段有界只读的
  `Setup Context`，包含本机工具可用性、项目 manifest、lockfile 和带目录作用域的
  setup 提示。它不是新的模型工具，也不会注入普通 prompt。
- 安全：Setup Context 本身不会安装、clone、写文件或联网。它复用敏感路径过滤，
  不暴露工具绝对路径，会说明列表上限，并继续把 shell 执行放在现有用户审批之后。

## 0.1.49 - Tool Start 可见性

- UX：Agent 工具现在会在本地执行开始前发出轻量 `tool_started` 事件。
  Web UI 会先显示安静的 pending 工具行，例如 `read app.py -> Reading app.py`，
  工具完成后再替换成最终结果。
- 设计：生产工具执行仍然保持串行和可观察。pending 行复用现有单色
  `.tool-line` 样式；不加入 spinner、progress 系统、并发 runner 或 ToolSpec registry。
- 安全：`tool_started` 只服务 UI/CLI 可见性，不计入 execution evidence、
  reviewer recent log，也不参与任务完成进度判断。

## 0.1.48 - 工具函数注入和并行 Probe

- 改进：Agent runtime 现在支持显式 `AgentToolFns` 注入，测试和手工 probe 可以替换
  工具函数，不再 monkeypatch `codey.agent` 全局函数。
- UX 决策：生产 Codey 默认保持 `read`、`ls`、`search` 串行执行。deterministic
  probe 证明只读并发 batch 可以缩短本地 wall-clock，但串行 tool event 更可观察，
  更符合 Codey 作为安静本地开发工具的气质。
- 安全：bounded file scan 和 search 的长循环现在会检查协作式 cancellation。
- 测试/probe：手工 A/B probe 现在使用显式工具函数注入。只读 parallel probe
  保留为脚本内实验，并记录为什么生产 Codey 默认不启用只读并发。

## 0.1.47 - Search 遗漏提示

- Bugfix：`grep` / `search` 现在会报告非 UTF-8 文件和不可读文件，不再把这些被省略的
  文件静默当成“完整搜索后没有匹配”。这个修复只作用在 Writer 的 search 工具上：
  oversized、read budget、bounded scan budget 的旧提示保持不变，也不迁移 hidden advisor
  search。

## 0.1.46 - Coverage-aware References

`find_references` 现在会说明有边界的文本引用扫描跳过了哪些可能仍包含引用的文件。
底层引用扫描器只收集紧凑的 `ScanReport` 事实：oversized 文件、不可读文件、
非 UTF-8 文件；不会暴露这些文件的源码内容。Writer 可见的工具层会把这些事实渲染成
很短的 `Scan coverage` 提示，并把工具结果标记为 truncated，让 JSON 工具协议继续提醒
模型不要把省略内容当成安全或干净。

这是一个刻意收窄的 production slice。隐藏 project-audit advisor 仍然使用旧的低层
reference 输出；Project Map、Verification Map、持久索引、缓存层和 ScanPolicy profile
都没有扩张。手工 scan-coverage A/B probe 现在会重建旧的低层 baseline，并和生产 Writer
coverage renderer 对比。

DeepSeek、MiMo、Qwen 和 GLM 的实机 A/B 已确认这个行为。GLM 的旧 baseline 仍会误称扫描完整，
并给出自信的 unused 结论；coverage arm 会明确指出跳过的 oversized 文件，并产出安全的
“扫描不完整，不能确定”回答。

## 0.1.45 - Provider Adapter 自修复

当网页变化大到 Provider adapter 代码本身也失效，并且控件级恢复和 Flow 级恢复都不够时，
Codey 现在可以尝试有边界的后台自修复。明确的结构性 Provider 故障会进入去重队列，
不会阻塞新的用户任务；失败的修复不会直接丢失，而是进入冷却期后等待下次重试。

修复运行在独立 Python 子进程中。健康 helper 模型只会看到坏掉的 Provider id、
有边界的故障上下文、允许修改的 adapter 源码和只读 Provider 测试。第一版只允许
在临时 sandbox 里修改目标 Provider 的 adapter 文件；核心文件、测试文件、registry、
tool runtime、server、supervisor、恢复模块和安全控制面都不在自修改范围内。

候选必须通过修复策略、`py_compile`、Ruff、对应 Provider 单测和中性 marker canary，
才会成为本地 provisional override。Override 只通过子进程 Provider worker 加载，
不会直接进入 Codey 主进程。子进程使用同一个已登录 Codey 浏览器 profile 的后台新标签页，
不需要复制 cookie，也不会抢用户当前聊天页。worker 卡住时，父进程会先按 CDP target id
关闭临时标签页，再清理子进程。

本地 adapter override 会记录内置 Codey base hash，先 provisional，只有自然成功后才
晋级 active；连续结构性失败会回滚。第一个 helper 产出非法候选、policy 失败、测试失败
或 canary 失败时，会继续尝试下一个健康 helper。

最终共享主 profile fresh-tab 路径已在 DeepSeek、Qwen、MiMo 和 GLM 上做过实机 smoke：
repair helper 和 candidate worker canary 都能真实发送并读回中性 marker。Qwen 暴露了
这次关键设计修正：独立 profile 可以看起来已经登录，却无法真正提交；复用主 Codey
登录 profile 的后台新标签页后可以正常发送。

## 0.1.44 - Focused Project Map

Project Map 现在会在有任务时加入一个有边界的 `Focused subtree` 小节，用来帮助
模型在深层仓库里更快找到相关模块。它在固定的文件数、目录数、单文件大小、总字节数
和输出字符预算内扫描源码文件，只展示最高分模块的相对路径、source/test 标记和符号
签名。它不展示源码体、不建索引、不持久化、不增加 UI，也不会额外调用 planner 模型。

Focused subtree 只在存在 task 且普通 Symbol overview 可能受大仓预算影响时出现。它出现时
会替代普通 Symbol overview，让 Project Map 把 token 留给深层任务相关模块，而不是继续展示
低相关的前序文件符号。

Qwen readiness 也更稳：Codey 现在会等聊天输入框、bootstrap 信号，以及模型选择器文本
连续两次读取相同且非空后才发送。输入框清空不再被当作提交成功；Qwen 提交确认只认 stop
出现或回答数量增加。

手工 probe 也记录了两条未采纳路线：两段式 scoped planner 和本地 deterministic
pre-scope 都没有证明值得进入生产。最终保留的是更轻的分层 map。四个网页模型在深层
synthetic monorepo 上从 `0/16` top1 提升到 `16/16`，同时 prompt 字符从 `53,424`
降到合并后实机复测的 `33,564`。

内部上，项目任务上下文准备逻辑已从 `TaskRunner` 抽到
`codey/project_task_context.py`。这个 builder 负责 verified facts、Project Map、
checkpoint 恢复/启动、checkpoint prompt 和初始验证候选；`TaskRunner` 仍然负责
Writer、Review、Receipt、会话状态、Provider 接管，以及显式的 evidence seed /
invalidate 调用。

Diff Review 生命周期也已抽到 `codey/review_coordinator.py`。Coordinator 负责 Review 前
diff 重试、是否值得 Review、Review 不可用时降级、Reviewer 反馈生成 Writer followup、
repair 后 diff dirty 状态，以及 review repair 后极窄的绿色检查继承规则。Reviewer
连接、Writer 接管、Receipt、ProjectFacts 和会话状态仍然留在 `TaskRunner`。

## 0.1.43 - 安静的 UI 持久化与侧边栏打磨

UI 状态持久化现在会在 SSE 热路径上节流。连续的 turn、tool、info 事件会合并
localStorage 和服务器端全量保存；用户主动操作和任务终态事件仍然立即 flush。
这能减少长任务中的隐藏序列化、网络 POST 和原子写盘开销，同时不改变持久化数据结构。

侧边栏不再使用浏览器原生 `prompt()` 和 `confirm()`。聊天和项目重命名改为就地输入框，
删除/清空等危险操作改为现有单色菜单内的安静二次确认。

连续的只读工具行现在会在渲染层折叠为更紧凑的分组，例如 `read · 5 files`。
单独一条 read/search/list/reference 仍然正常显示；只有连续同类安全工具行才会分组。
edit、run、shell 和错误行始终展开。

内部上，Writer Provider 接管逻辑已从 `TaskRunner` 抽到一个小型、可测试的状态机。
这个重构不改变 Provider 协议或用户可见行为，但让接管、共享 turn 预算、canary、
checkpoint refresh 和 Stop 优先级更容易证明正确。

## 0.1.42 - 更多检查命令与克制 Markdown

受控 `run` 现在可以执行更多常见验证命令，而不需要走不安全的 shell 路径：
`ruff check`、`ruff format --check`、`mypy`、`python -m mypy`、
`python -m ruff`、安全的 `make` 目标、`bun test` 或允许的 `bun run` 脚本，
以及安全的 Deno test/lint/check/fmt 形式。会修改文件或安装依赖的形式仍然拒绝，
例如 `ruff --fix`、没有 `--check` 的 `ruff format`、`mypy --install-types`、
`make deploy`、`bun install` 和 `deno run`。

完整测试/构建套件现在有 300 秒超时，快速命令继续使用 90 秒预算。超时反馈会明确
说明这是 timeout，不是测试断言失败，并提示 Writer 运行更小的子集，而不是猜测代码
修复。字面 grep 达到匹配上限时，也会提示缩小查询或指定子目录。

本地 UI 的助手回复现在支持一小部分单色 Markdown：代码块、行内代码、粗体、标题和
简单列表。代码块带安静的复制按钮。仍然没有语法高亮、没有新配色，也没有新增模式或
面板。

## 0.1.41 - 智能分页提示

`read_file` 只返回大文件的一页时，现在会直接附带下一页的 JSON 工具调用。
Codey 仍然保持原来的完整行分页和 `next offset` 文案，但额外给出同一路径、下一
offset 和当前有效 limit 的 `read_file` 调用，让 Writer 不必自己推断怎样继续读。

这个提示使用 JSON 转义，只在确实还有下一页时出现，不改变读取预算、文件内容、
工具协议或截断语义。

## 0.1.40 - 有边界的 Stacktrace 降噪

受控 `run` 输出现在会在原有中间截断预算前，先折叠明显的依赖库调用栈。
Python 会折叠来自 `site-packages`、`dist-packages`、`.venv` 和 `venv` 的依赖
frame，并一起折叠紧随其后的依赖源码行。Node 只折叠明确的 `at ...` stack
entry，且路径必须带有 `:line:column` 并位于 `node_modules` 或 `.pnpm` 内。

项目源码 frame、assertion 信息、异常摘要、测试名和普通日志都会保留。如果没有
可折叠的依赖调用栈，输出会字节级保持不变。这个能力不改变工具协议、退出码、
`ok`、`changed` 或 `truncated` 语义。

## 0.1.39 - MiMo Typing Flow 与中性网页标记

MiMo completion 恢复现在复用网页明确提供的 `data-is-typing` 状态转换。
Flow 观察严格区分 true、false 和不可用三种状态，因此属性缺失或 DOM 读取异常
绝不会被当成完成。恢复仍要求先观察到 typing，再明确转换为 false，同时回答
非空且文字稳定；内置完成判断始终优先。

短回答、长代码和深度思考实机 probe 均观察到所需转换，Flow 判定完成后回答没有
继续增长。强制 Flow 测试在第一次发送后保存 provisional 规则，并在下一次自然
发送中晋级 active。网页可见的验证 marker、临时 DOM 属性、页面全局变量和剪贴板
sentinel 现已全部使用中性名称；仅存在于本机的配置名称保持不变。

## 0.1.38 - 有边界的 Provider Flow 恢复

Provider 恢复包除了经过验证的控件，现在还能携带一条有边界的网页状态规则。
Flow Recipe 只能使用固定的布尔观察，不包含 selector、JavaScript、URL、任意动作、
网页正文或项目数据，并且复用现有的 provisional、晋级、失败计数和回滚生命周期。

completion 恢复必须同时看到稳定非空回答，以及从生成证据到终止证据的真实转换；
只凭文字稳定绝不会判定完成。Qwen 首先使用 stop 从可见到消失的转换进行安全试点。
MiMo 和 GLM 没有同等可靠终止证据时，继续使用内置完成逻辑并安全降级。四家
Edge/CDP 控件故障注入全部通过；更严格的 Qwen 实机测试关闭内置完成判断后仍能
完成回答，并在下一次发送中复用 Flow、从 provisional 晋级 active。

## 0.1.37 - Python 语法回归提示

Python 文件的 replacement edit 成功后，如果原文件语法有效、最终修改结果却无法解析，
Codey 现在会立即返回一条有边界的语法回归提示。文件仍然正常写入，edit 仍然成功；
Codey 不会自动回滚、运行命令，也不会把提示算成绿色检查。原文件已经损坏、修改后
语法有效、非 Python 文件，以及超过 128K 字符解析预算的文件都不会产生提示。

DeepSeek、Qwen、MiMo、GLM 的实机 A/B 均避免了一次失败测试，并保持最终代码正确、
独立测试通过；其中三家还减少了轮次或工具调用。四家的合法编辑 control 均为零提示。

## 0.1.36 - Provider Revival 与 Writer 接管

Provider 恢复现在以一次有边界的事务覆盖输入框、发送按钮和回答读取。本地发现
无法确定时，Codey 最多依次询问三个健康的兄弟模型，让它们只从脱敏后的结构候选
中选择；候选必须经过一次真实发送验证，才会原子保存为 provisional 恢复包。
下一次自然发送成功后恢复包晋级 active，明确的连续控件失败则自动回滚上一版。

新增被动 Provider 健康熔断，区分结构故障、临时错误、限流、提交状态不确定、
登录和验证码状态。Writer 遇到明确的网页 Provider 故障后，可以在严格的新会话中
把未完成任务交给健康兄弟模型，并且只传递有边界的本地 checkpoint 事实。切换次数
和总轮次有上限；Stop、普通工具失败、协议失败和提交不确定都不会触发危险重发。
DeepSeek、Qwen、MiMo、GLM 的 Edge/CDP 故障注入均验证了自动恢复和持久复用。

## 0.1.35 - Default Post-edit Verification

代码发生修改后，如果 Codey 能确定存在唯一、可运行且与变更文件匹配的检查命令，
现在会在完成前提醒 Writer 处理一次验证。候选只来自历史成功检查，或项目明确配置的
pytest、npm、Cargo 和 Go 入口。最后一次修改后的绿色检查会直接复用；仅文档变更、
命令歧义、执行程序缺失或跨技术栈误配都不会启用门槛。Codey 不会自动安装依赖或
自行执行命令，默认检查失败后也不会无限重复。
完成边界会重新读取当前 manifest，因此任务中修改检查脚本后不会继续使用过期命令。

## 0.1.34 - Bounded Edit Failure Context

精确 replacement 失败后，如果能证明存在唯一词法锚点，Codey 会返回有边界的
当前文件证据；匹配不唯一时，最多返回三个真实起始行。写入判定仍然完全精确：
不会自动采用 closest match，不会把超长行截断后伪装成可复制代码，也不会把内存中
尚未落盘的半成品当作磁盘证据。正常成功 edit 不增加 prompt 成本或输出。

## 0.1.33 - Read-before-edit Guard

新增本轮 agent run 内的读后编辑保护：Writer 对已有文件做精确替换前，必须先在
本轮成功读过该文件。`content` 全量写入只允许创建新文件；已有文件必须使用精确
replacement。本轮创建或修改过的文件会被视为已知，后续可继续做替换编辑。这样
Symbol overview 仍只是导航提示，不会变成跳过真实文件检查的理由。DeepSeek 和
GLM 也新增了可见限流重试按钮的短冷却后自动点击。初始项目提示不再暴露绝对
临时路径，也不再显示空 instructions 小节；只有实际存在 `AGENTS.md` 或
`CLAUDE.md` 时才加入仓库说明。

## 0.1.32 - Bounded Symbol Overview

在现有 Project Map 里加入 task-aware 的 Symbol overview，让 Writer
第一次读文件前先看到更准确的文件和符号导航提示。它仍然是有边界、本地
只读的小节：不增加 UI、不增加公开工具、不做缓存/索引/embedding/LSP，
也不把源码体塞进 prompt。Qwen 还新增了窄范围恢复：容忍重定向
`net::ERR_ABORTED`，并对一次确认提交后无回复的卡顿做单次重试。

## 0.1.31 - Structured Execution Evidence

新增有边界的内存执行证据账本，让 Verification Map、Review、任务收据和成功项目事实共同使用同一份读取、搜索、编辑、截断和最后一次编辑后检查记录。

## 0.1.30 - 简化导航工具

实机评估显示 Project Map、字面量 `grep`、`find_references` 和带 offset 的 `read_file` 更稳定，因此完整移除了已撤回的 `outline_file`。

## 0.1.29 - Verification Map

新增隐藏且有边界的测试候选与检查证据，供 Reviewer 判断验证是否充分；它不声称能证明影响范围或测试覆盖率。

## 0.1.28 - Durable Execution Checkpoint

为未完成的项目任务保存同 session 的恢复事实：改动文件 hash、仍然有效的成功检查、最后一次 edit/run 和中断原因。

## 0.1.27 - Find References 与有边界扫描

新增有边界的文本引用提示，并让 references、grep 和隐藏审查共用流式扫描器，扫描不完整时会明确说明。

## 0.1.26 - Outline File 实验

曾加入 `outline_file` 作为有边界的导航实验；自然使用评估显示采用率较低，该工具最终在 0.1.30 完整移除。

## 0.1.25 - Hidden Project Map

新增有边界、只读的项目结构图，供 Writer、隐藏顾问和 Reviewer 使用，不索引源码、不引入 RAG，也不增加 UI。

## 0.1.24 - Hidden Change Briefs

新增 Writer 与 Reviewer 共用的私有 ChangeBrief，并只从真实改动和检查沉淀验证过的成功事实。

## 0.1.23 - 浏览器启动健壮性

加入 Edge 优先、Chrome 回退的浏览器发现，改善 WebView 启动失败说明，并明确标记工具和 Review 结果截断。

## 0.1.22 - Durable Conversation Handoff

网页模型上下文不再可信时，在事实 handoff 中加入有边界的近期可见对话摘录。

## 0.1.21 - Durable Chat State

持久化有边界的侧边栏和聊天状态，增加低干扰复制按钮，并在重启后对齐 Send/Stop 状态。

## 0.1.20 - Quiet Chat Controls

继续收紧紧凑聊天控件和交互状态，不增加新的工作流或模式。

## 0.1.19 - MiMo Answer Completion

把 MiMo 发送按钮识别与回答完成判断分开，改用回答 DOM 避免过早判定完成。

## 0.1.18 - Provider Reliability

收紧 MiMo、Qwen、GLM 的网页状态处理、本地 JSON 协议验证，以及 Review 修复后的检查新鲜度。

## 0.1.17 - Hidden MoA Layer

新增隐藏的主模型优先多模型建议：普通聊天与新项目使用草稿评议，已有项目使用有边界的只读顾问审查。

## 0.1.16 - Plain Chat 与项目讨论

New Chat 保持无项目权限，同时让同一项目对话可以自然地从讨论进入读取和编辑。

## 0.1.15 - GLM Provider

加入第四个网页模型 GLM，并统一 Provider 注册和 smoke 选择。

## 0.1.14 - 协议效率与安全

统一本地工具契约，限制安全并行读取，分页读取大文件，并让多替换 edit 保持原子性。

## 0.1.13 - Runtime Ownership Cleanup

统一 Git 与 snapshot 改动处理、集中运行时存储，并明确 Provider session 的生命周期所有权。

## 0.1.12 - Resilient Run Reconciliation

加入有边界的后端运行快照，在刷新或短暂断线后按顺序恢复 UI 真实状态。

## 0.1.11 - Responsive Stop

让 Stop 能中断 Provider 等待、恢复、Review 和受控命令，并同时保留长输出的开头与结尾。

## 0.1.10 - ProfileDoctor Recovery

新增有边界、严格脱敏的第二层恢复：可让已打开模型从结构候选中选择网页控件。

## 0.1.9 - Bounded Provider Recovery

加入版本化 Provider Profile，并对变化后的输入框、发送按钮和回答做保守、经过验证的重新发现。

## 0.1.8 - Durable Local Continuity

持久保存少量已验证项目命令、有边界的事实聊天快照，以及非 Git 项目的恢复基线。

## 0.1.7 - Structured Runtime

引入结构化工具结果与事件，分离任务编排和 HTTP 传输，并让 UI 不再解析散文日志。

## 0.1.6 - Hidden Context Handoff

接近共享上下文预算时，生成有边界的事实摘要并在新网页对话中继续。

## 0.1.5 - Control Teaching Cleanup

收紧用户教学网页控件后的恢复和清理，同时保持教学只作为低干扰的最后兜底。

## 0.1.4 - Task Receipts

新增紧凑任务收据，显示改动文件、检查状态和是否可恢复。

## 0.1.3 - Durable CDP Browser Reuse

Codey UI 重启后优先复用已有 Edge CDP 浏览器和模型网页，再决定是否启动新浏览器。

## 0.1.2 - Provider Status 与输入快捷键

改善 Provider 状态反馈和以键盘为主的消息输入操作。

## 0.1.1 - Stability Smoke

为最初的本地网页模型工作流补充发布级稳定性 smoke 验证。

## 0.1.0 - 首个双语版本

发布首个中英文 Codey：把支持的网页 AI 聊天克制地连接到本地文件编辑、检查、Diff 和恢复。
