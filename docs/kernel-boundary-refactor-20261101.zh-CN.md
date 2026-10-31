# 内核边界整理与验证，2026-11-01

范围：沿现有 operation reducer、durable mutation、effect 收据及 completion gate
整理职责。分四批提交，每批先验证再提交；不运行全量 pytest，不引入第二运行时、
新事实库或通用领域生命周期框架。

## 第一批：资源与评审依据

删除 KernelExecutionDeps 中生产内核未读取的 durable、managed_outputs、session_id。
这些资源继续来自原 KernelEffectSink；测试会话身份由测试 helper 显式配置。
生产入口、测试及相关手工实机脚本一并停止传递重复资源。

评审的 schema、工作提示和完成准备统一读取 session.research_review_basis，
删除 completion_context 中的第二入口。保留原 basis/batch digest 和收据校验。
review_basis 仍从已接受报告及精确知识版本重建；本项不承诺跨实现版本的判断单元
完全不变，重建范围与原收据不符时应拒绝，不能改用最新知识解释旧评审。

为已存在的 session、响应、快照、effect 与完成证明补充具体类型，并明确拒绝
缺少评审 basis 或非结构化的 native 响应。

实机使用本机 Gemma 4 12B Q4_K_M、seed=74，隔离任务/状态目录。
第一次 contract_consistency_live 读文件运行因手工入口遗漏 durable boundary callback
失败；已修正入口，失败原件保留。第二次两次生成完成 CodeMode 双文件读取、
观察值绑定 JSON 交付、事实恢复与重复恢复检查，ok=true。
原始资料：artifacts/kernel-refactor-20261101/batch1-read{-02}/。

回归中在原始 cf276731 独立工作树复现了两类既有测试问题：Research 提示 fixture
遗漏当前所需的任务/问题绑定，以及 CodeMode 断言绑定旧引擎的完整错误文案。
fixture 按当前合同补全；错误测试继续要求真实类型错误、原冻结合同和失败事实，
使用两种引擎共有的错误语义，不改生产错误内容。

扩大选集时发现 Zen 包装器未透传内核已冻结的观察文本、历史组数与输出预算；
补齐三个原请求参数，不重新计算参数、不降级请求。原基线也复现此失败。
其余相关旧 fixture 按当前协议修复：覆盖选择替代模型重写报告、明确失败的发布、
严格 native 调用 ID、正式 durable 恢复入口及原始 provider 请求作用域。
生产提示未改，更新的提示 hash 与原基线实际输出一致。

针对性修复选集 36 passed，正式恢复独立选集 8 passed；扩大选集 1378 passed、3 skipped、38 subtests passed（9779 deselected，310.61 秒）。
实机成功记录为 batch1-read-02（2 次生成，约 9.86 秒）。
Mypy、Pyrefly、ty 生产全目录检查通过；Ruff 检查生产目录与本批修改文件。

## 第二批：合同、决定和完整迁移表

将完整迁移关系集中在一个表里；与第一批保存的最终图逐项比较完全相等，
包含全部 18 个状态和 76 条边，含自循环和关闭路径。
KernelResult 编解码集中到 kernel_contracts，所有原调用直接迁移，未留转发函数；
报告摘要一致性、快照 digest 和 proof 校验保留。编码仍使用同一个 json_value。
控制决定使用带 purpose 判别字段的 TypedDict 联合，未提交决定用 None 明确表示。
序列化输入的运行时校验保留，并拒绝在终结位置读取 retry 作为结果。
删除 finalize_research_text 空操作及旧调用。

旧 decision 测试在原基线也错误地期望 max_turns 和两份 artifact；当前正确行为是
第三次重复完成提案因 no_progress 停止，三个不同进度快照各有审计 artifact。
按当前合同修正断言；第四批去掉 retry 内的计数后再验证 artifact 去重。
一次与格式修复并行的重启测试因源文件改变而拒绝冻结实现，停止修改源码后独立复跑
6 passed。这验证了实现冻结的拒绝边界；后续检查期间固定生产源码。

最终实机：batch2-write-02，2 次生成，真实创建 created.txt，完成与重复恢复通过。
Mypy、Pyrefly、ty 通过。扩大测试 82 个相关文件：1319 passed、104 subtests passed（326.97 秒）。

## 第三批：Research 准备进入原 completion gate

研究报告编译及已提交评审收据的检查移入 research_completion_checks，gate 在
引用、正文、必需工具检查及 proof 引用收集之前调用准备。各检查显式消费本次
FindingReport；不再接受 context 中外部传入的 research_report 作为另一入口。
GateVerdict.research_report 仅是本次返回值，接受后进入原 decision artifact。
资源来自原 completion context 的 ledger、store 与作用域，不传递 KernelContext。
KernelContext 将已有 ResearchTools 的资源接入该 context，不新增资源所有者。

准备错误单独保留 ValueError/ResearchIssue 的完整字段信息，包括可用引用及补证提示；
其他 gate 异常仍保留原处理。原 last_done_args、模型正文与 proposal 进度记录不被编译
结果覆盖。只有 done 控制提案更新这些字段；评审工具计划不再把占位正文当成模型提案。
保留 research_review 的真实成功收据、完整批次及关闭分支，未引入另一套完成流程。
evidence_followup 继续只报告采集结果，显式评审只记录诊断，不编译或发布研究报告。

新增集成回归覆盖：字段纠错前不进行引用检查；修复后 proof 引用绑定本次报告；
两种 q8 缺口状态；接受后进程中断，恢复既不编译也不读取知识；原始 coverage 不变；
实际发布、重复发布与原快照一致；评审多批次通过 gate 关闭，无伪造 done、无发布。
定点选集 57 passed，完成合同/评审选集 40 passed。Mypy、Pyrefly、ty 通过。
扩大回归 107 个相关文件：1141 passed、64 subtests passed（339.01 秒）。

实机目录：artifacts/kernel-refactor-20261101/batch3-research。
Gemma 4 12B Q4_K_M、seed=74、temperature=0.3、JSON 文本协议、真实网页、9 轮后 done。
q2=not_investigated、q3=unanswered_after_search，正文、原 decision、answer 版本和 note
逐字一致。按 publication effect 的冻结 result_ref 两次打开实际数据库恢复，禁用编译器
仍通过，结果完全相同（batch3-live-replay-final.log）。终态会清空 work_ref，回放需沿
原 publication envelope 读取，不能从终态位置猜测另一个结果。

**该实机回答的语义内容未通过。** q1 模型把读写不互相阻塞误推成官方未保证单写者，
系统忠实采用了它保存的错误解释；此次整理没有修复选证和语义推理能力。
本轮只确认 q8 缺口编译、发布及恢复一致，不将 done 或成功 proof 解释为语义认证。
浏览器退出时记录了 TargetClosedError 警告，实际采集与发布已完成，原日志保留。

## 第四批：retry 观察提交及版本化恢复

新 decision envelope 为 schema_version=2，retry 不再保存 counters。协议错误、无工具调用的
首次截断与完成拒绝都在保存控制决定时，同一 checkpoint mutation 原子提交观察计数。
计数记录已观察的事实：invalid_turns 是连续无效响应数（合法计划重置）；
length_continuation_used 记录已观察并使用一次截断续接额度；progress 记录已拒绝的原提案。
因此即使没有下一请求，观察仍成立，保存决定不再借助下一请求提交已发生的错误。
冻结请求保留 expected_counters 与 counters；新 retry 两者均为当前 checkpoint，
请求准入继续负责 stale 检查与原有准入约束，关闭轮数等原准入语义不变。

只保留明确的 v1 历史读取：旧 retry counters 可能已提交，也可能有一项未提交观察。
必须与 checkpoint 相等，或仅 invalid_turns +1，或仅 length 标记 false→true；
重置、跳数、替换 progress、改变关闭轮数及同时增加两种观察都在发送前拒绝。
旧决定继续冻结原 expected 与 proposed，在原请求准入中消费一次；达到回合/无进展上限
而不再发送时，随 closing checkpoint 提交。已经冻结的旧请求不重算、不改 intent。
v2 禁止 counters 字段，v1 必须携带并校验它，不靠缺字段猜测提交状态；没有新双写者。

测试覆盖文本/native 协议错误与截断，在 control_ready 和 provider_request_ready 中断，
以及真实子进程 os._exit 后重开 SQLite；检查保存决定后计数存在，下一请求不重复扣减，
重复恢复无发送。旧格式涵盖待提交、已提交、提前关闭、非法差异及预算重置拒绝。
重复完成拒绝的相同 retry artifact 从三份去重为一份，checkpoint 仍记录三次拒绝。
测试内同进程模拟死亡使用 force 接管原租约；不放宽生产中的存活 worker 保护。

实机：artifacts/kernel-refactor-20261101/batch4-retry-02，Gemma 4 12B Q4_K_M，seed=74，
temperature=0.3、JSON 文本协议。两次真实生成均正确返回指定 done；测试明确将第一次
回复替换为坏 JSON，以验证实际错误路径。retry 保存后模拟退出，关闭 SQLite/provider，
冷启动续接；已提交 invalid=1，下一请求 expected==proposed==checkpoint，最终合法计划
将连续 invalid 重置为 0，完成且重复恢复无新增请求。该测试不宣称模型自然产生了错误。
首次实机内核行为同样成功，观察脚本把同一冻结请求的两次 commit 通知重复计入边界，
导致脚本的计数断言失败；按 request_ref 去重后复跑通过，两份记录都保留。

Mypy（578 files）、Pyrefly、ty 生产全目录及 Ruff 通过。
定点恢复/准入选集 106 passed（65.50 秒）；Research 评审/快照/未发送收据补充选集
88 passed（7.55 秒）。扩大回归 139 个相关文件：初跑 1722 passed、8 failed、
3 skipped、104 subtests passed（586.21 秒）。8 项失败全部在原始 cf276731 工作树复现：
并发测试的 _generate 替身缺少 output_token_limit；旧 SimpleNamespace 缺少严格 policy/
ToolResult 字段；native 提示断言绑定固定消息位置；RecordingProvider 未透传冻结请求选项。

修复时不修改生产提示或放宽运行时合同：使用真实 TaskPolicy/ToolResult，替身补当前明确
参数，断言检查本次新增修复文本而非消息下标；RecordingProvider 显式透传全部原请求选项。
新增断言核对实际 HTTP 的 max_tokens=123，确保记录器不吞掉预算。扩展的 decoder 测试中
两处 gate 替身也迁移为实际 GateVerdict，补上本次新结果字段合同，不增加 getattr fallback。
相关 11 文件最终复跑 85 passed（10.28 秒），覆盖并消除主选集全部 8 项失败。
其余主选集通过结果保留；此次修复未改变 codey 生产源码，不重复整轮近十分钟的检查。
所有失败、基线复现、最终复跑日志和主选集文件清单保留在
.e2e-artifacts/kernel-refactor-20261101/，各选集存在重叠，数字不作为唯一测试数相加。
