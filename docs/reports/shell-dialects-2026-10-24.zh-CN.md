# Shell 方言扩展三批落地

起点为 3a5c5747。三批各自验证并提交，原 run/shell 内核、审批和过程生命周期保持唯一。
只运行相关 pytest 与静态检查，不运行全量 pytest 或性能对照。

## 第一批：解释器覆盖

ShellSpec 扩为 CMD、PowerShell、Bash、Zsh、Sh；PowerShell 7 与 Windows PowerShell
5.1 使用同一方言，版本和路径区分。新增 version、startup、binary_sha256，缺少字段
的旧审批不从环境补齐。执行前核对入口二进制摘要，变化后拒绝执行并要求新审批。

默认仍为 Windows CMD、其他系统 /bin/sh。执行主机可通过 SHELL_EXECUTABLE 指定解释器
名称或绝对路径，通过 SHELL_STARTUP 选择 noninteractive/login。显式选择不可用或未知
就失败，不改换其他解释器。Git for Windows 的 Bash/Sh 发现只发生在新任务准入前。

PowerShell 用 UTF-16LE EncodedCommand 传输，框架只添加 UTF-8 输出设置，不替换用户
脚本或推测退出码；保留原 PowerShell 的退出语义。调用原生程序后要保留其准确退出码，
需在脚本中明确 exit $LASTEXITCODE。非交互启动不加载 profile，login 启动允许 profile。
Bash 非交互关闭 profile/rc，Zsh 使用 -f；CMD 不支持 login。所有路径仍进入原
cancellation.start_process、进程树、超时、输出捕获、结算和恢复，没有第二执行服务。

实际版本从解释器获得；不提供版本的 sh 明确保存 null，并保存入口文件摘要。摘要
不是整个解释器依赖/DLL/配置环境的快照，不承诺冻结所有外部状态。

本机实测 CMD 10.0.26100.1742、PowerShell 7.5.3、Windows PowerShell 5.1.26100.1591、
Git Bash/Sh 5.3.9(1)-release。非交互/login 模式下测试含空格目录、引号、中文输出与
原生程序退出码 7；CMD 的 login 明确拒绝。Zsh 本机不可用，对应三项跳过，不算通过。

第一组相关 pytest 65 passed、3 skipped；审批/取消/恢复相关选择 58 passed、1 skipped、
8 subtests passed。选择重叠，不累计。Ruff、mypy（514 源码文件）、Pyrefly、ty 通过，
目标 Python 3.11；保留既有 Pyrefly 29 抑制与 80 未展示警告。

真实 Gemma CMD 环境变量场景通过生产 HTTP/worker/审批/续跑：合法 echo 命令、实际
输出与最终答案一致，审批携带上述完整合同。材料：
artifacts/shell-coverage-batch1-live-20261024-01。后续两批结果在各批验证后追加。

## 第二批：方言分析

增加唯一 analyze_shell 入口及 ShellAnalysis：分别记录命令/参数、连接与管道、重定向、
展开、动态执行、解析器身份与诊断。结构化结果绑定完整命令摘要和 ShellSpec 摘要；
统一严格恢复校验，不从展示文字反推。第三批将它接入现有审批与授权消费者。

Bash 使用固定版本 tree-sitter/tree-sitter-bash；Sh/Zsh 只对该语法树能确认的共同部分
给出 parsed，不认识的扩展给出 opaque，不把 Bash 的错误诊断冒充完整 Zsh 语法判断。
CMD 只确认有界的字面量、引号、连接、重定向、环境展开子集，控制流/caret 等明确 opaque。
PowerShell 7/5.1 使用所选引擎的原生 Parser.ParseInput；随包可信解析程序通过原过程
管理执行，用户源代码只作为 Base64 数据传入。未 Invoke/编译用户脚本，未修改执行策略。
超时/缺少解析器/预算超限标记 opaque，无替代解析器重试；取消继续传播。

依据：[Tree-sitter Python API](https://tree-sitter.github.io/py-tree-sitter/)、
[Bash grammar](https://github.com/tree-sitter/tree-sitter-bash)、
[PowerShell ParseInput](https://learn.microsoft.com/en-us/dotnet/api/system.management.automation.language.parser.parseinput)、
[CMD 规则](https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/cmd)。

相关组合 74 passed、8 skipped、14 subtests passed，含两代 PowerShell 的真实 AST、
命令替换、带引号元字符、管道/重定向/变量区分，以及恶意输入仅解析、不生成标记文件。
随后 glob 与恢复身份检查的定向选择 33 passed、5 skipped。Zsh 缺少本地解释器的跳过不算
实机通过。Ruff 与三项全树类型检查通过；wheel 实际包含 PowerShell 解析 helper，
依赖 hash lock 只增加两个解析包，正式 runtime 描述由原 publish_runtime 生成。

环境名检查同时发现上一轮探针的品牌前缀变量不符合仓库规则，已改为无品牌 SHELL_PROBE；
没有增加生产环境配置兼容名。

## 第三批：授权与实机

审批准入只产生一份分析：ActionSubject 校验同一份 ShellAnalysis，ShellApprovalRequest
将它和 ShellSpec 放入原命令 artifact。恢复只严格校验命令/解释器/解析器身份，不重跑
解析器；执行票据必须携带已绑定分析，执行前再次校验与检查入口二进制摘要。
删除旧 compatibility_error/heredoc 专用扫描和项目授权中的原始元字符/shlex 分支。

现有 CommandPermissionStore 继续保存精确授权。静态字面量、noninteractive 才可复用；
变量/通配符展开、动态执行、opaque、login 都仅逐次审批。项目授权额外要求窄命令族
与 simple_argv。带引号的分号/竖线不再被原始字符串黑名单误判。PowerShell 的通配符
参数（含引号）采取保守逐次审批，因为 AST 字面量不证明目标 cmdlet 按字面量使用它。
参数和重定向复用一个 Get-ShellWord 解码程序，重定向展开也进入统一分析。
没有增加权限系统、事实日志、模型特例、命令翻译或执行器。

审批卡片只消费同一服务端授权判断投影：不能记住的命令不再提供本会话选项。新增
session_rule_available 由原权限模块统一计算；前端不读取 AST、不复制规则，HTTP 仍
再次校验。前端通过原 build/publish_frontend 发布，运行清单保持由原工具生成。

本批相关验证（组合重叠，不相加）：

| 范围 | 结果 |
| --- | --- |
| 主要相关选择 | 241 passed、12 skipped、110 subtests passed；另 1 个续跑失败，见基线归因 |
| 补充消费者 | 100 passed；另 1 个压缩异常测试失败，见基线归因 |
| 分析与授权 | 49 passed、6 skipped |
| 最终授权/恢复定向组 | 59 passed、1 skipped |
| 最终真实子进程超时/Stop | 10 passed、2 skipped（Zsh）、20 deselected |
| HTTP 独立复查 | 16 passed；未增加 5 秒请求超时 |
| 架构边界定向组 | 5 passed、93 deselected、14 subtests passed |
| Chromium + 正式打包 UI + HTTP + subprocess | 4 passed；含 once/deny/session/project、响应丢失与单次执行 |
| 前端审批相关 Vitest | 24 passed；TypeScript/Biome/build 通过 |

本机原进程链路的超时和 Stop 测试逐一覆盖 CMD、两代 PowerShell、Git Bash/Sh；实际
启动子进程，在超过子进程预定写文件时间后确认它没有逃逸写入。Zsh 本机不存在，跳过
不计成功。解释器 noninteractive/login 启动矩阵 9/9；CMD 不支持 login。矩阵同时保存
原分析与执行结果，PowerShell exit 语句属于 opaque、逐次审批，不伪装完整语法覆盖。
材料：artifacts/shell-dialects-matrix-20261024/windows-accepted.json。

基线归因均直接运行从 3a5c5747 导出的原代码，使用相同测试，不改超时：

- test_bounded_capture_and_context.py::LocalPrepareRequestTests::test_compaction_failure_is_explicit：
  原版和修改版都在调用被 mock 的压缩器前抛 ContextOverflowError，而测试期待压缩失败。
- test_release_shell_continuation_http.py::test_zen_wrapper_restores_approved_call_before_native_continuation[openai-responses]：
  原版也报 approval continuation did not finish；修改版后续整个 HTTP 文件独立运行 16 项通过。
  不将不稳定的旧失败冒充已修复。

故障修复过程也保留：最初的通配符重定向测试发现 PowerShell 目标遗漏了展开，已与参数
共用 Get-ShellWord 后通过。最初矩阵曾错误地要求含 exit 语句的所有脚本都 parsed，导致
4 个实际退出码正确的 PowerShell 项被门本身拒绝；已纠正为允许显式 opaque，确认结果仍
为真实退出码，不把 opaque 当作可记住授权。

Ruff、mypy（517 源码文件）、Pyrefly、ty 全 codey 源码检查通过，目标 Python 3.11；
Pyrefly 保留 29 抑制与 80 未展示警告。没有运行全量 pytest，也没有性能对照。

### 真实模型结果与限制

固定同一个本地 Gemma4 12B Q4 模型，实际 wire 为 temperature=0.3、max_tokens=1024、
stream=true、未指定 seed。测试调用原 HTTP/worker/native tool/审批/续跑链路，探针只
允许有限的无害精确命令，既没有替代执行器，也没有 Gemma 专用生产逻辑。

| 解释器 | 已通过场景 | 未通过/未验证 |
| --- | --- | --- |
| CMD | 带空格文件、环境变量、真实退出 7、拒绝后报告 | 无本轮上述场景失败 |
| PowerShell 7 | 带空格文件、环境变量、真实退出 7 | 拒绝后两次重新申请同一命令，任务未完成 |
| Windows PowerShell 5.1 | 环境变量、真实退出 7 | 未单独跑模型拒绝/读文件 |
| Git Bash/Sh | 各自环境变量、真实退出 7 | 未单独跑模型拒绝/读文件 |
| Zsh | 无本机实测 | 未安装；Linux/macOS 待 CI |

PowerShell 7 最初退出码探针的安全白名单漏了分号前空格，测试拒绝执行；补入这一有限
变体后实测退出 7 正确。保留原失败目录，不改生产命令。两次拒绝场景则是真实模型行为
失败：wire 已有不得重试指令与 Denied by user 回执，模型再次发出 shell 请求；系统仍
要求审批，未执行、未伪报成功。这段拒绝指令/续跑逻辑与原版相同，但没有相同 PS7 基线
来证明模型服从率不变，因此不能把此场景写成通过或声称本批消除了重复申请。

完整材料保留在 artifacts/shell-dialects-batch3-live-{cmd,pwsh,powershell,bash,sh}-20261024-*。
真实模型是有限样本，不据此宣称总体命令错误率下降。

新增 .github/workflows/shell-contracts.yml：Windows 要求五种实际解释器，Ubuntu/macOS
要求 Bash/Zsh/Sh；缺少 required 解释器直接失败，不能以 skip 通过矩阵。CI 复用同一
execute_shell_ticket，不自建进程执行器。Linux/macOS 尚未远端运行，本机 Windows
结果不代替那些平台结果；本轮未推送远端。

两项旧失败在后续任务中已进一步定位并修复；此处保留当时验收记录。见
[根因与修复报告](context-shell-failure-roots-2026-10-24.zh-CN.md)。

## 三个参考项目的借鉴边界

- Codex：`reference-projects/codex/codex-rs/core/src/shell.rs:24` 的
  Shell::derive_exec_args，以及 `shell-command/src/shell_detect.rs:363` 的选择边界。
  Codey 把方言/路径/版本/模式纳入 ShellSpec，由类型生成参数；保留原默认值，显式
  选择失败不切换解释器，不复制其 fallback 发现链。
- OpenCode：`reference-projects/opencode/packages/core/src/shell/select.ts` 的解释器选择
  与参数生成，以及 `tool/plugin/shell.ts:117` 的 prepare/scan(invocation.command,
  invocation.shell)。Codey 同样让分析与执行绑定同一合同，分析额外进入原持久 artifact
  与恢复；没有复制 scanLegacy/scanPortable 双扫描。
- DeepSeek Harness：`reference-projects/deepseek-harness/packages/shell/shell/src/index.ts`
  的 ShellExecutor 与 bash-local/pwsh-local 的 subprocess 执行边界，尤其
  `pwsh-local/src/index.ts:193` 的原生 argv。Codey 复用原 cancellation 执行边界，
  各方言只提供启动合同与解析前端，不复制 Bash/Pwsh 两份镜像过程生命周期。

这是一组架构选择与实际边界验证，不是三个项目的完整能力排名，也不证明模型不再出错。
