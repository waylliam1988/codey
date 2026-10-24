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

待本批完成后记录最终实现及验收。Windows 结果不代替 Linux/macOS 实机结果。
