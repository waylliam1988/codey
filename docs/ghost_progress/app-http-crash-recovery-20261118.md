# 正常 App 硬退出与真实 HTTP 恢复审查

- 日期：2026-11-18；关联 0.6.1–0.6.4。
- 状态：旧 smoke 单项诊断完成；真实 HTTP 矩阵已冻结，夹具预检后运行基线。
- 代码基线：`6d844752`；上一轮裁剪修复 `34852638` 保留，不重复其矩阵。
- 模型：本阶段不调用模型；旧 smoke 使用离线 scripted writer/provider。

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
