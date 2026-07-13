# 版本更新记录

[English version](CHANGELOG.md)

这里记录 Codey 从最早版本到现在的发布历史，最新版本排在最前面。

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
