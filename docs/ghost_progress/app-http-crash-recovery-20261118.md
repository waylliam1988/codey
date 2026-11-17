# 正常 App 硬退出与真实 HTTP 恢复审查

- 日期：2026-11-18；关联 0.6.1–0.6.4。
- 状态：`PRODUCTION_MERGED`（本次 master 保存响应/本地 native 回执冷恢复修复）。
- 诊断 A 基线：`6d844752`；B 负结果 `c0fa5117`，C 冻结 `f56e7d5b`。
- 模型：A 旧 smoke 离线；B/C 使用固定 SSE 的真实 loopback HTTP，不调用模型。

## 已有证据与实际缺口

`tests/durable/test_prepared_provider_request.py` 有真实 SQLite/os._exit，但 provider 是
fake；`tests/test_ghost_native_refusal_recovery.py` 有真实 loopback HTTP/shared kernel
及 warm/cold 同 run，但不是完整 App 硬退出。普通 App→HTTP 的偏好准入/撤回已在
`tests/test_ghost_language_production.py` 验收，不能据这三组独立证据宣称它们的组合已验。

旧 `tests/manual/completion_operation_resume_smoke.py` 声称正常 headless 硬退出和
同 run 续传，但仍以 positional JSONL adapter 构造现在要求 keyword-only durable 的
operation store，并用相同 run_id 再次提交普通 headless。当前 production 只从 durable
checkpoint/effect 恢复，普通 headless 重复提交应拒绝；静态疑点须先实际诊断归属。

## 冻结诊断 A：旧 smoke

目标：区分过时验证材料与有效生产恢复失败。主要变量只有原样旧 smoke 对当前代码；
对照为代码中的现行 durable owner/重复 ID 契约，不构造兼容生产补丁。

- 入口：`python -B tests/manual/completion_operation_resume_smoke.py --self-test`。
- 样本：原样执行一次；冻结脚本和整个 codey 生产树 SHA256，不修改脚本/生产后复跑
  并替换原结果。无模型、endpoint、采样参数或随机 arm。
- 指标：退出码、首个失败点、是否真实进入 writer/持久恢复/续传、是否有无主子进程。
- 成功：原脚本到达它声称的恢复断言并通过；失败：精确记录接口或运行契约违反。
  参数不匹配或旧结构访问属于脚本失效，不能称为模型阴性或生产丢状态。
- 停止：一次结束即停止；父进程上限 60 秒，子进程由脚本 finally 清理。超时另记
  `INCOMPLETE`，确认真实进程状态后清理，不把观察超时当进程自然退出。
- 检查：诊断后运行全仓 Ruff、完整 codey 的 mypy/Pyrefly/ty/precision gate。
  本阶段不修改 Python，不以旧脚本预存的 Any 代表新代码满足类型要求。

原始 stdout/stderr、检查日志及冻结 metadata 仅留
`.e2e-artifacts/ghost-app-http-crash-recovery-20261118`；仓库留本记录和脱敏指标。
旧脚本使用临时目录，不能在失败后假称保留了独立的崩溃数据库。

## 后续交付与验收

### 诊断 A 结果

冻结提交 `15cefa66` 后原样一次执行，exit 1；首次失败正是 positional
`RuntimeOperationStore(RuntimeSessionLog(...))` 的 TypeError。尚未进入 writer、持久恢复
或续传断言，不能把该旧 release gate 计为当前生产恢复证据，也不证明生产丢状态。
脚本 finally 已结束子进程，进程枚举未发现遗留 smoke；无模型调用。旧重复 ID 路径
仍只经静态核对，未被这次早退覆盖。全仓 Ruff、完整 702 文件 codey 的三类型与
precision gate 全部通过，生产树/旧脚本哈希冻结验证通过。

[脱敏诊断报告](../../tests/manual/ghost_memory_routing/reports/app-http-crash-legacy-smoke-20261118.json)
保留原命令、退出码和脚本哈希；完整 traceback/检查输出留本地。旧脚本临时 state 未
保留，不声称已保存崩溃恢复数据库。下一步按真实 durable/正常 App 入口更新脚本，
恢复原行为断言的现行等价验证，再补物理发送组合。

诊断完成后及时提交原结果。若旧脚本失效，按现行 durable owner 和真实恢复入口修正
验证材料，完整清理该文件参数/返回值/回调/容器及动态边界的显式和隐式 Any；不使用
cast、ignore、豁免或放宽检查。保留它验证硬退出、原 run/operation 唯一性、诚实进度、
路径脱敏、已提交状态恢复和终态的能力；旧 JSONL lane 的假设须映射到现行 durable
任务/提交/效果契约，不能只删除断言。

真正缺失的 App×physical HTTP×硬退出组合另行冻结目标、配置、任务、对照、scope、
样本、指标和停止条件，采用正常入口/实际 ApiProvider/SQLite。先复用已验证的受测
Gemma 中文偏好配置作为生产消费者边界；loopback 固定回复只证明接线，不冒充新模型
收益或当前模型服务健康。恢复不能重复不确定发送，不能以新 run 偷换原 run 的恢复，
也不能把正常 headless 的新提交入口改成旧 ID 重放入口。

本轮完成前不标 `PRODUCTION_MERGED`，不新增默认关闭模块或功能开关。若当前生产
本已正确，交付组合验收和修正的验证工具即可，不强行改生产。相关回归、完整静态门、
文档和独立 commit 完成后继续其他 G0 缺口与有新增预测信息的 World Model 消费者。

## 冻结实验 B：正常入口、四个真实崩溃窗口与跨 run

代码基线为诊断结果提交 `567b202c`。旧 smoke 将改为有类型的 loopback harness，
保留同一 `--self-test` 正常复现入口；不注入 agent_run、provider.send 或 fake durable。
初始化使用普通 `run_headless`，恢复使用默认开启 scheduler 的正常 `AppContext`，
后续新任务仍走普通 headless；不重复提交原 run_id 冒充恢复。

24 cell = scope(user/project/session) × crash(请求冻结后、intent 已提交但 HTTP 前、
服务端收到 HTTP 后尚未响应、完整 response artifact 已提交) × policy(enabled/revoked)。
唯一处理变量为崩溃后的 scoped admit_hints 撤回；enabled 是相同 fixture 的正对照，
四窗口/三 scope 为边界分层，每格一次 deterministic 运行，不计模型样本量。

生产消费者固定为已有人工确认中文偏好。先通过正常任务保存来源、正常 propose/accept
建立对应 scope 的偏好，fixture 必须证明首个可发送请求含该 source。模型标识沿用
Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M，协议 OpenAI Completions、
native tools、context/reserve/keep=262144/32768/32000。本轮真实 Local 连接普通默认
temperature=0.3、stream=true，固定 SSE done 回执；这是物理发送/恢复验收，不是
temperature=0 的历史模型实验补跑。endpoint 为每格独立 loopback server，不连接
127.0.0.1:5001 或其他真实模型，不新增模型收益/服务健康主张。

配置和 state 全隔离；使用真实保存的 Local 配置及正常 capture/validate/open_selection，
只把配置文件根指向 fixture 目录，不修改用户配置。前两窗口和 response 窗口在真实
SQLite commit 返回后 os._exit，wire 窗口由父进程确认收到请求后硬 kill。持久 mutation、
request/response、Attempt Gate、transport 和正常 scheduler 不替换；故障 seam 不改
事务内容。记录原 request/response ref/hash、operation/run/submission 身份与 timeline。

冻结验收：

- 所有 fixture 均必须完成真实来源确认、进入 writer，再在指定持久/物理窗口硬退出；
  fixture 不成立记 `INCOMPLETE_FIXTURE`，不计生产阴性，不改 scorer 迎合夹具。
- 请求冻结组初始 HTTP=0。enabled 冷恢复必须只发送原准备请求一次并完成原 run；
  revoked 冷恢复必须在 HTTP 前拒绝旧 hinted payload，不能报 done 或伪造回复事实，
  原 request 与已提交历史保留。允许诚实的准入拒绝终态，不能偷偷换成新的 prepared
  request 或为旧 ID 分配新 operation。
- intent 已提交组初始 HTTP=0；wire 组初始 HTTP=1。两者恢复都不得重新发送不确定
  effect，不得完成原 run；必须保留未知结果，不能把“服务端实际没收”猜成 NOT_SENT。
- response 已提交组初始 HTTP=1，两种 policy 都应消费已保存 response 完成原 run，
  恢复新增 HTTP=0。Ghost 撤回不能删除真实 provider 回执或强制重做已完成发送。
- 冷恢复后再冷开正常 App，不得重复发送、重建 operation 或改变原完成/不确定结论。
  然后同 session/project 以独立新 run 发一个普通任务，必须成功；revoked 新 payload
  不含旧中文 hint，enabled 仍可使用偏好。原 run/effect/request 历史继续保留。
- 原 run 只有一个 canonical submission/operation，writer 历史可追溯；details 的
  Progress 与持久 operation 相符，operation payload 不含原始 project 路径。旧 JSONL
  lane/operation_started 断言映射到 durable 身份、timeline 和 submission 终态。

指标逐 cell 输出 fixture_valid、崩溃前/两次恢复/新 run 的物理请求数与 hint 可见数、
原始 refs/hash 保持、task/submission/operation 结论、诚实进度与唯一性。24/24 通过才
能称本矩阵闭合；任一有效违约先保留基线并独立 commit，再决定生产修复。一个 cell
单阶段子进程上限 45 秒；失联先保留完整输出/state，检查进程并清理后标 INCOMPLETE，
不把模型或测试服务故障算候选阴性。不放宽 scorer、跳过失败或删除原行为断言。

复现入口为旧 smoke `--self-test`；pytest 将逐 scope/crash/policy 参数化调用相同
typed cell。先做单格 fixture 预检，夹具修订保留失败证据；正式冻结结果使用独立 state，
检查期锁定整个生产树与本轮脚本/测试哈希。完成后运行必需生产回归、全部指定静态门
及两个修改/新增文件的三类型和 Any 审计，不运行全量 pytest。原始 replies/wire/state
只留本地；仓库报告只留脱敏 refs/digest/counts、命令、失败归属和限制。

### 正式基线前的夹具核对

所有预检 state、日志、JUnit 留在本地 `fixture*`，不覆盖任何失败。首版单格未保存
App 模型允许列表，正常 App 正确报 `Selected model is disabled`，headless 则不依赖
此 UI 设置；这是未满足冷恢复前提的夹具失败，不能记为生产故障。已用真实
`ModelPreferences.save` 在隔离目录显式启用 Local/受测模型，并在崩溃前验证。

第二、三版各单格的 HTTP/完成行为通过，但脚本误把 native terminal 的本地
`acknowledge_tool_results_bound` 当作替换模型请求：现行实现为其保存独立
request/response/effect，却只提交协议历史、不发送 HTTP。第三版还误用了控制回执的
display_ref。两次断言失败保留；按实际 display_ref 与 owner_effect_id 分辨回执，
模型请求/响应 ref+hash 必须完全相等，不删除身份断言，也不修改生产迁就夹具。
同时追加原冻结要求的 prepared context hint 校验、第二次冷开/新任务后原结论一致、
撤回组无虚构 response/已发送 receipt 检查；不改变物理发送/完成/未知状态验收门。

第四版预检两格：user/request_ready/enabled 通过；user/response_saved/revoked 为
有效 fixture，原响应存在且没有新增 HTTP，但原 run 未完成。先保留该现象，不在
完整基线前修生产，也不把它算模型负结果。正式矩阵使用独立 state，继续全部 24 格。

### 实验 B 基线结果

`python -m pytest tests/test_ghost_app_http_crash_recovery.py -q -s --tb=short`
（完整 basetemp/JUnit 参数见脱敏报告）完成 24/24 有效 fixture，18 passed / 6 failed，
336.52 秒，exit 1。三 scope 的 `response_saved` 在 enabled 和 revoked 下均失败，
其他 18 格通过。每格原始 request/ref/hash、submission/input/operation 身份保留；
没有重复物理 HTTP，第二次冷开和后续独立任务不改变原结论，24 个新任务均成功且
hint 符合控制。冻结的 702 个生产文件及两个 owned 文件 hash 全部保持不变。

保存响应组不是 Ghost 撤回专属故障：普通冷启动已从 response artifact 还原 native
tool call，但未还原原请求的 declared tools。`ApiProvider` 的本地
`acknowledge_tool_results_bound` 因 `terminal tools must belong to the previously declared set`
失败，任务变为 failed/unanswered，原成功 response 与 sent receipt 保留；ack effect
诚实保留 error/maybe_sent，不能重发来伪造闭环。此处真实 HTTP=1 且恢复增量=0，
失败属于生产原工具契约投影缺失，不是夹具、模型阴性或 endpoint 故障。

[脱敏基线报告](../../tests/manual/ghost_memory_routing/reports/app-http-crash-baseline-20261118.json)
保留每格 counts/refs/hash/结论与 frozen owned hashes；完整 wire/回复/state/trace 只留
本地 `baseline-state`。baseline 没有新增生产消费者、开关或默认 prompt；结论为
`EXPERIMENT_ONLY`，不标 `PRODUCTION_MERGED`。下一步只从校验过的原响应/原请求恢复
同 window 的传输工具声明，保持 Completion/Permission、撤回与真实发送 gate 不变。

基线后相关 16 文件回归：328 passed / 1 既有 skipped，209.28 秒；文件清单与命令
见报告。跳过是已有 execution-fencing 替代的 legacy shared-owner fixture，本轮未新增
skip、未删断言，亦未运行全量 pytest。全仓 Ruff、完整 702 文件 codey 的 mypy /
Pyrefly / ty 和 kernel precision gate 均通过（Pyrefly 158 warnings / 35 suppressed
为既有事实，不称零警告）。两个 owned 文件三类型检查通过；额外
`mypy --disallow-any-expr` owned 零诊断，未修改导入依赖 10988 项/336 文件单独留档。
全树及 owned hash 在基线和检查前后保持不变，无模型调用或前端改动。

## 冻结修复 C：保存响应的原工具传输契约

基线提交 `c0fa5117`，唯一生产变量为 cold API 窗口恢复原响应对应的 declared tools。
从已有 hash/owner 校验的 response→request→snapshot 取原工具定义，不能从当前 registry
或新任务猜测；同 window 的纯 durable 投影才可填充空契约。已有 live 契约不可替换，
跨 window、非 canonical 历史或并发发送须拒绝，锁与原 provider 的 send/state owner 共用。
此恢复只影响本地协议校验，不给工具/完成授予权限，也不绕过 physical Attempt Gate。

目标：原 24 格原脚本原 scorer 全通过，尤其保存响应 enabled/revoked 原 run 完成且
新增 HTTP=0；其他 18 格无回归。模型/任务/参数/样本沿用 B，不新增模型调用。新增
两 API 协议的冷 native 回执、幂等恢复、跨 window、live 契约、非 canonical 历史、
并发锁和 wrapper 边界检查；补跑 B 的 16 文件生产回归与相关 API context 回归。
所有修改源文件（包括拥有 helper 的既有模块）清理到额外 Any 零诊断，运行全仓 Ruff、
完整 codey 三类型/precision 与 owned 三类型检查。不更改检查范围或 scorer。

停止：任何 valid 原格不通过、原 refs/hash 丢失或产生重发，先保留候选失败再定位；
不得将未完成实验写成通过。不新增默认关闭开关，修复若通过即由现有正常 App/kernel
默认消费；生产 commit 与基线分开，之后同步进度板/roadmap/回滚限制。

### C 预检（不替代正式矩阵）

只修改 `kernel_driver` 的原响应契约恢复与 API context owner 的有限 helper；从
response owner 对应的原 request artifact 解码 snapshot，在恢复原 window 后恢复
空 declared tools。helper 共用 send/state 锁，拒绝不同 window、已有不同契约和非
canonical 历史。模型请求、原响应、Permission、Completion、Attempt Gate 不改写。

原先失败的 user/response_saved/enabled+revoked 两格预检现 2/2 通过，原 run 完成且
新增 HTTP=0。追加 API 两协议边界 12/12 通过。首版单元夹具把未配对的 tool call
交给 `load_durable_context`，12 项均在进入 helper 前被 codec 拒绝；日志与 JUnit
保留在 `repair-contract.*`，不能计为生产失败。按正常 durable 消费视图补入已配对
的 pending receipt 后 `repair-contract-v2.*` 通过，不放宽 codec 或删除边界断言。
三个新增/修改文件的三类型预检通过；包括 B 两脚本的五个 owned 文件额外 Any
零诊断，未修改依赖 10988 项/336 文件仍单独保留。生产正式 24 格另用独立 state
与冻结 hash 运行，不用两格预检或单元样本替代。

### C 正式矩阵结果

原 24 格 / 原脚本 / 原 scorer 现 24 passed，338.19 秒；B 的 18/24→C 的 24/24。
六个保存响应组均完成原 run，原模型 request/response refs+hash 不变、恢复新增 HTTP=0。
请求冻结 enabled 3 格只发送一次后完成；revoked 3 格在 HTTP 前拒绝。intent / wire
各 6 格保留 unknown、不重发、不伪造 done；这 12 格的“通过”指诚实中止，不是任务完成。
24 个后续独立任务都成功，每次一个 HTTP，hint 与 scoped 控制一致。两批各 63 个
物理 HTTP（含 24 来源任务与 24 新任务），没有为修复增加额外模型请求。

正式矩阵与检查期冻结全部 702 个生产文件及五个 owned 文件；相对 B 只有上述两个
生产模块变更，B 两脚本字节不变。原始 wire/state/JUnit/日志在本地
`production*`，仓库 [脱敏结果](../../tests/manual/ghost_memory_routing/reports/app-http-crash-production-20261118.json)
保留逐格结论和 source hashes。此处先记矩阵结果，扩大回归/全树门及生产 commit
完成后再标正式合并，不能只据候选预检宣称接线完毕。

受测正常入口是保存实际 Local 配置/模型允许列表→普通 headless→冷 App 默认 scheduler；
修复消费者是现有 shared kernel 的窗口恢复，非 harness-only hook，无新增默认关闭开关。
它不增加模型可见内容，也不扩大中文偏好的模型/任务配置白名单；API 两协议本地契约
边界由独立 12 项覆盖，完整 App/HTTP 24 格仅证明本记录指定的 Completions 配置。

回滚为 revert 本次生产提交并在正常重启时重新加载；保留原 SQLite 与 artifacts，
不删原始历史或将 unknown 猜成 NOT_SENT。既有模型禁用/任务控制仍可隔离发送。
此前已经 failed 的旧任务不会被自动重新打开或重试；本轮验证的是冷启动恢复尚未终态
的 checkpoint，不保证旧失败状态迁移、混版本运行或完整 G0 验收。

### C 最终验证与生产结论

扩大 24 个相关文件：384 passed / 1 既有 skipped，210.67 秒；包括 B 的 16 文件、
新增两协议 12 项，以及 API context budget/checkpoint/compaction/policy 与 durable
provider context/generation/sink 回归。未新增跳过项，不运行全量 pytest；两批不同
范围的数字不相加。完整命令、检查状态及逐格结论见生产脱敏报告。

回归后全仓 Ruff、完整 codey 702 文件 mypy / Pyrefly / ty 及 kernel precision gate
全通过，五个 owned 文件三类型检查通过；额外 Any 审计 owned 零诊断，未修改导入
依赖 10988 项/336 文件单独保留，命令本身 exit 1 不伪称全依赖无 Any。
Pyrefly 仍有既有 158 warnings / 35 suppressed，无新增豁免或检查范围变化。
正式矩阵、扩大回归和静态门之间全部 702 生产文件/五 owned hashes 不变。

修复随本次 master 生产 commit 默认进入现有 `restore_provider_window` 消费者，
不需要用户打开实验开关；生产提交可用
`git log --grep="restore native declarations before cold result delivery"` 定位。
结论 `PRODUCTION_MERGED` 仅指上述恢复边界；没有新模型效果或服务健康主张，
不扩展中文偏好白名单，也不宣称全 G0/World Model 完成。后续继续混版本/retention、
其余真实发送消费者和有新增预测信息的 World Model，两个重复事实候选不重跑。
