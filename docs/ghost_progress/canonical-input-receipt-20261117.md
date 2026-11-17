# Ghost canonical 接收结果与双故障修复

- 日期：2026-11-17。
- 基线：`f0fa59a7`（生产 `84c57aac`）。
- 状态：`PRODUCTION_MERGED`（限定正常应用 runtime 的接收结果与写口保护），不是完整 0.6.3。
- 生产提交：与本记录、源码、回归和候选报告同提交，可用 `git log --grep="preserve Ghost intake failures in canonical input receipts"` 定位。
- 模型/endpoint：确定性 provider，实际接收、结算、Sleep；不调用模型或 HTTP。

## 假设与主要变量

仅增加可靠的接收结果及其生产消费者：Ghost 登记成功/失败由应用回调返回，随原输入的
canonical admission commit 原子保存；Ghost DB 不健康时，普通任务仍可接收、执行和完成。
原始 admission 结果不可被排队编辑、claim/start、后续内容更新或重启覆盖。

使用已有 submission content 携带应用所有的类型化 receipt，并将它写入同一 admission
commit 的既有公开投影，初始 commit 是只读 receipt 的权威；不增加任务日志、虚构产物或
进程内永久集合。用户请求不能提供或覆盖应用生成的 receipt，原请求 fingerprint 不变。
durable 层只持有通用 component/bool 契约，不 import Ghost。

Ghost 通过 durable-owned 的只读 API 查阅初始接收结果。Ghost schema 7 保存同目录 canonical
数据库的绑定元数据（只有文件名，无任务正文）；已绑定后缺库、查询错误、缺 receipt 均不能
放行 run 投影。未绑定的独立 Ghost store 保留原用法。绑定在真实应用接收路径默认生效。
observation 与 run-source INSERT/UPDATE 的 SQLite trigger 使用每连接注册的只读检查，
同时保护实际 writer；projection fence 与新来源筛选也检查 receipt。独立 Hebbian 和
research-note 命名空间不受 run receipt 限制。旧代码不识别 schema 7，旧连接无新函数时写入拒绝。

## 固定验收

复用 `intake_double_fault_probe` 的 31 cell、输入、删除/控制、scorer、恢复时点与重启方式，
不改原报告。新报告另存。有效性必须与基线相同，健康无删除仍有 observation/continuity；
12 个开启删除双故障 cell 的旧输入必须 12→0；关闭和单故障组保持 0，双故障无删除同样隔离。

增加针对性回归：原始 receipt 与 canonical commit 同生效、失败 commit/retry、输入来源和
fingerprint 不变、编辑与后续恶意 receipt 更新不能改初始结果、重开与新 worker、数据库不可读、
缺任务/缺 receipt、健康新输入、scope 与独立知识来源、直接 SQL INSERT/UPDATE、schema 升级
及旧 writer 拒绝。补跑受影响生产回归、完整静态门与改动文件 Any 表达式审计；不跑全量 pytest。

如普通任务受 Ghost 故障阻断、健康新输入整体停止学习、无效 cell 或隔离状态可被覆盖，
本次候选保持未完成，先修对应问题。完整 manifest、混合恢复、身份/scope 异常、维护撤回、
显式 replay 和 World Model 仍需各自推进，不用本次矩阵代替其验收。

## 当前接线与已验证结果

`DurableAppServices.open → register_ghost_input → InputReceipt` 默认接线；失败返回明确的
`ghost=false`，canonical task 不因此失败。kernel 将回调结果加入 submission metadata，
与原始 admission event 同事务提交；外部提供的同名字段被替换，request fingerprint 和
原 `input_request` 不改。`read_input_receipt` 只读原始 commit 的有界公开 projections，
不加载 `_workflow` 或正文、不初始化缺失数据库、不从后来可修改的 content 重构。

schema 7 的 authority binding 保持跨重启；默认 runtime `durable.sqlite3` 在 Ghost 恢复初始化时
也会绑定，覆盖 intake 时整个 Ghost 数据库都不可用、无法写入任何 Ghost metadata 的情况。
已绑定后缺库、坏库、未识别 schema、缺任务、缺 receipt 或 malformed bool 均拒绝 run 投影。
旧 canonical 输入没有 receipt 时不被猜成已通过；已有投影的完整迁移/重建策略仍待单独验收。

结算 observation、continuity/work queue 的 SQL INSERT/UPDATE 默认检查结果；projection fence
及新来源筛选也读同一结果。刷新调用中的 run_id 有时只是审计标识，只有携带 task excerpt、
ledger 或 terminal 输入时才作为源身份进入 fence。独立 research-note/Hebbian 来源继续使用
自己的命名空间；本轮扩大回归发现并修复了知识刷新被审计 run_id 误挡的问题。

同一 31-cell 探针，0 无效，健康组仍有 1 observation / 3 continuity，
双故障开启且删除的 12 cell 旧输入从 12→0；关闭、单故障和双故障无删除均为 0。
报告中的 `baseline=84c57aac` 是固定实验对照版本，候选生产源码与本记录同提交。

```powershell
python -m tests.manual.ghost_memory_routing.intake_double_fault_probe --output tests/manual/ghost_memory_routing/reports/intake-double-fault-canonical-20261117.json
```

[候选报告](../../tests/manual/ghost_memory_routing/reports/intake-double-fault-canonical-20261117.json)
与原始基线分开保存，不恢复已停止的语言批次。

新测试还覆盖原始 receipt 在普通编辑/恶意 content 替换/placement/claim/start/重开后不变；
外部伪造 receipt 不生效且原请求重试仍幂等；canonical commit 拒绝时没有可见 receipt，
重试后与输入同提交；缺/坏 authority、异常 bool、SQL 直写和 schema 6→7 的旧连接拒绝。
使用真正 spawn 子进程在双故障接收成功后以退出码 23 终止；父进程恢复后旧输入不学，
真实新输入仍学。另用同名目录令 intake 时整个 Ghost DB 无法打开，恢复后也不重学旧输入。

## 回归过程与失败归属

扩大相关回归（未运行全量 pytest）：

```powershell
python -m pytest tests/durable tests/test_ghost_canonical_input_receipts.py tests/test_ghost_received_input_lifetime.py tests/test_ghost_inflight_input_lifetime.py tests/test_ghost_deleted_input_fence.py tests/test_ghost_deleted_source_lifetime.py tests/test_ghost_projection_delete_fence.py tests/test_ghost_sleep_source_controls.py tests/test_ghost_post_turn.py tests/test_ghost_post_turn_affinity.py tests/test_ghost_post_turn_work_queue.py tests/test_ghost_control_contract.py tests/test_ghost_control_surface.py tests/test_ghost_controls.py tests/test_ghost_sleep.py tests/test_architecture.py -q
```

该批 **1193 passed / 15 failed / 5 skipped / 484 subtests passed**，不是全绿结果。

- 1 项 startup：新增生产模块未进入 runtime 发布清单。已运行 `python -m tools.publish_runtime`，
  本轮清单只新增 `codey/durable/input_receipts.py`。
- 7 项跨进程恢复：该批进行中我修改了生产源码，而 `runtime_implementation_id` 按进程缓存
  整棵生产源码摘要；子进程和父进程看到不同源码。生产正确拒绝不兼容恢复，不放宽其契约。
  固定源码后，operation-driver/provider-receipt/research-restart/startup 四文件复验 **48 passed**。
- 5 项 work queue：独立研究笔记刷新被审计 run_id 误挡，属于本轮生产回归，已修改
  projection source 判定，未改这些旧测试。加公开知识消费者回归后，与新 receipt、投影删除
  和 Sleep source control 的复验批 **111 passed**。
- 2 项 architecture：七个既有长文件限额和 `runtime/core/models.py → protocols.json_repair`
  的归属争议，均是前轮已记录的未修改源码；本轮不提高上限、不改 allowlist，保持未通过记录。

另一个接收边界测试原来把从未 canonical 接收的任意 run_id 当新输入直接写入。现已补上真实
admission，保留“删除后新输入能学习”的原断言。新增测试的几次失败也属于 fixture：排队
编辑缺原请求、content 更新缺配套 checkpoint、work-item factory 清理了 continuity 专用
source、坏 receipt 注入误选 event 序号；均修复有效前提，未放宽生产校验和安全断言。

最终固定源码后的相关 Ghost 批次：

```powershell
python -m pytest tests/test_ghost_canonical_input_receipts.py tests/test_ghost_received_input_lifetime.py tests/test_ghost_inflight_input_lifetime.py tests/test_ghost_deleted_input_fence.py tests/test_ghost_deleted_source_lifetime.py tests/test_ghost_projection_delete_fence.py tests/test_ghost_sleep_source_controls.py tests/test_ghost_post_turn.py tests/test_ghost_post_turn_affinity.py tests/test_ghost_post_turn_work_queue.py tests/test_ghost_control_contract.py tests/test_ghost_control_surface.py tests/test_ghost_controls.py tests/test_ghost_sleep.py -q
```

结果 **296 passed**；新增 canonical-receipt 文件含 65 项，已包含在本批，不另相加。
前述恢复与启动复验命令：

```powershell
python -m pytest tests/durable/test_operation_driver_restart.py tests/durable/test_provider_response_receipts.py tests/durable/test_research_continuation_restart.py tests/durable/test_legacy_chat_rescue.py -q
```

结果 **48 passed**。历史广批次与这些复验有重叠，不相加宣称最终全套通过；前轮懒加载等
未闭合问题也不因此消失。完整架构批次保留两项既有失败。

最终生产源码固定后，依次运行以下完整门，均通过：

```powershell
python -m ruff check .
python -m mypy codey --python-version 3.11
pyrefly check codey --python-version 3.11
ty check codey --python-version 3.11
python -m tools.kernel_type_precision_gate
```

mypy 覆盖 701 个生产模块；Pyrefly 为 0 errors、35 个既有 suppression、159 个 warning。
两个改动测试文件另外通过 mypy、Pyrefly、ty（均带 `--python-version 3.11`）。
额外 `--disallow-any-expr` 对 9 个改动生产文件及两个测试审计，11 个文件零 owned 诊断；
SQLite 行字段先作为 object 接收，再做具体值校验/解码，row factory 有明确签名。
传递依赖仍有 351 个未修改文件的 11268 项诊断、退出码 1，不宣称全仓零 Any；未新增
cast、ignore 或检查豁免。本地完整审计为
`.e2e-artifacts/ghost-intake-admission-20261117/canonical-receipt-any-audit.txt`。
runtime 清单已同步，`python -m tools.publish_frontend --check` 及 `git diff --check` 通过；
未修改前端源码，未跑全量 pytest 或新语言实机批次。

## 范围、回滚及后续

这是一项默认生效的输入安全修复，不是新增模型 treatment，不改变已启用中文偏好范围。
控制仍可显式关闭；不写 Evidence、Permission 或 CompletionProof，不调用模型/工具。
schema 7 不能直接交给旧 schema-6 代码冷启动；不能靠降版本或删安全表回滚。

本次验证的是正常应用 runtime 和同目录 authority 绑定。自定义 canonical 文件名的健康
接线已验证；在首次绑定前整个 Ghost DB 不可用时，该自定义布局的自动恢复仍需验证，
不把默认 runtime 的正结果外推到它。整库恢复、删除 manifest、升级旧输入、身份/scope
异常、维护撤回、容量保留、确定性 replay 和 World Model 仍保持未完成。
