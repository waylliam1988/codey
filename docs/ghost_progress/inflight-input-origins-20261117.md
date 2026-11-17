# Ghost 首次投影前输入身份与删除

- 日期：2026-11-17
- 基线：`91af3ec5`，沿用已提交的[在途删除复现](inflight-deletion-source-controls-20261117.md)。
- 实验状态：`EXPERIMENT_COMPLETE`（限定已进入执行生命周期的输入）。
- 实验提交：`b2812172`；来源命名空间负结果：`48e6f66f`。
- 生产结论：`PRODUCTION_MERGED`（限定执行入口登记与退役；生产提交见下）。
- 模型、endpoint：无。仍是确定性 transport、真实任务生命周期与 Ghost store；不恢复语言实机批次。

## 冻结的假设、变量与判定

基线在真实任务的首次 provider send 内暂停；Ghost observation/continuity 此时为空。
session delete / reset × accept_events 开 / 关 × 两次重复，共 8 cell，每 cell 要求删除
成功且原任务正常完成。基线来源控制已修复：关闭的 4 cell 无旧输入，开启的 4 cell
仍各重建 1 observation、3 continuity。复用相同脚本和 scorer，不增加模型实验。

本轮变量：在 `_ensure_durable_task` 的执行生命周期入口登记只含 run/session/project 的
Ghost 输入身份，再由普通 observation delete/reset 的同库事务退役尚未投影的身份。
canonical runtime input、ledger、checkpoint、Evidence、Permission、CompletionProof 不改写。
该身份表是删除边界的元数据，不是第二个任务事件日志，也不保存 task/prompt/reply。

仅保护 projection 方法进入时的 generation 不足：接线前候选已有下游存储 gate，四个
运行时断言仍为 **2 failed / 2 passed**，开启时仍重消费。输入 producer 必须真实接线，
不能将增加一个存储 API 当作关闭问题。

目标是 8 cell 全有效、旧 observation/continuity 全为零；新 run 和其他 scope 仍可用。
扩展验收包括新消费者、旧 checkpoint、reopen、重复删除、多进程、SQL insert/update
绕过、schema 升级/缺表、身份拒绝和故障隔离。任一复活或错误完成即保留负结果，不放宽断言。

## 候选与实测

schema 6 增加 `ghost_input_origins(run_id,session_id,project)`，会话和项目有索引。
普通执行入口默认登记；同一身份重入幂等，被退役后不能重新登记。登记发生在 provider
effect 前；receipt 与删除同库串行，删除不需要从已经存在的投影猜测在途 run。

删除/reset 在自身 observation 事务内将这些身份转入既有 `ghost_retired_inputs`，然后
移除 live receipt。continuity/work queue 的 fresh source filter 和 SQLite 写口拒绝
退役的 run source；各 `fence_projection` 从显式 run_id、真实 RunLedgerProjection、
terminal event 取得输入身份。Sleep 也在实际 worker 调用时检查，故不依赖线程上下文传播。
observation append 同一写事务复查 generation，INSERT/UPDATE 均有退役 gate。

发现一个候选缺陷：只警告 receipt 写失败时，后续仍可写 observation。真实 SQLite trigger
故障注入为 **1 failed / 1 passed**（失败是登记后的无身份学习，checkpoint 对照通过）。
修复为单次登记存储故障时持久隔离该 run 并推进 generation，然后仍上报登记异常；
canonical task 不因这个已隔离的 Ghost 输入而取消。未将登记故障当作任务失败或模型阴性。

继续检查来源命名空间时新增两项负结果：continuity 的 source_ref 也可能沿用独立 Hebbian
或 research-note 身份。只比较字符串 run ID 会误拒绝这两类输入，新增对照 **2 failed**。
修复必须依据 metadata.continuity_source 保留明确的非 run 来源，不能为通过删除矩阵误伤
独立身份；未知来源仍不能借同名旧 run 绕过退役 gate。原 46 项通过不包含这两个新断言。

46 项新增验收在候选通过，包括两个 spawn 子进程与 canonical task 重入；旧 checkpoint
仍归 runtime 保留，但首次 Ghost 消费被拒绝。新任务与其他 scope 的合法输入仍可写。

```powershell
python -m tests.manual.ghost_memory_routing.inflight_delete_probe --output tests/manual/ghost_memory_routing/reports/inflight-delete-input-origins-20261117.json
python -m pytest tests/test_ghost_inflight_input_lifetime.py -q --tb=short
```

同一 8 cell 候选复测全部有效：开启的旧输入 cell **4→0**，关闭维持 **0**。
[脱敏复测报告](../../tests/manual/ghost_memory_routing/reports/inflight-delete-input-origins-20261117.json)；
历史基线不覆盖、不改写。来源类型修正后新增对照通过，最终新增 **48 项**验收全部通过。

## 默认生产接线与验证

执行入口、普通 delete/reset、派生消费者与实际后台 Sleep 使用同一持久输入身份，默认
生效，没有新增默认关闭开关。该变化是存储/控制边界，适用于正常任务入口，不扩大任何
模型的 language rollout。明确关闭仍禁止正文进入 Ghost projection；身份 receipt 只用于
防止删除后重新消费输入，不保存正文、事实或模型结果。

最终代码的五文件回归为 **162 passed / 2 subtests passed**：

```powershell
python -m pytest tests/test_ghost_inflight_input_lifetime.py tests/test_ghost_deleted_source_lifetime.py tests/test_ghost_projection_delete_fence.py tests/test_ghost_work_queue.py tests/test_ghost_continuity.py -q --tb=short
python -m ruff check .
python -m mypy codey --python-version 3.11
pyrefly check codey --python-version 3.11
ty check codey --python-version 3.11
python -m tools.kernel_type_precision_gate
python -m mypy tests/test_ghost_inflight_input_lifetime.py --python-version 3.11
pyrefly check tests/test_ghost_inflight_input_lifetime.py --python-version 3.11
ty check tests/test_ghost_inflight_input_lifetime.py --python-version 3.11
```

全部静态门串行通过，mypy 覆盖 699 个生产模块；Pyrefly 为 0 errors、35 个既有 suppression、
159 个 warning，新增测试为 0 errors；kernel Any 与 typed contract gate 通过。测试文件不在
生产树 gate 内，已分别检查。未运行全量 pytest，未创建或复制 worktree。

生产提交：`git log --grep="retire Ghost inputs before first projection"`。

## 回归、类型与剩余边界

生产 lifecycle 原文件的 Any/cast 已清理：具体资源的 LifecycleDeps、trace/store 返回值，
TaskState 与 TaskSubmission 的明确字段，以及经验证的 proof mapping。没有修改检查配置、
增加忽略或豁免，没有用宽泛对象继续未经验证地动态访问。

首轮七文件扩展为 **170 passed / 2 failed / 6 subtests passed**。两个失败都在未改动的
`tests/test_task_entry_cutover.py`：默认 search 测试传入 SimpleNamespace knowledge 和 object
search，不满足现行 KnowledgeStore / SearchProvider；open_hit 的 Tools 假对象缺少现行
ResearchTools 的字段，delegate 构造失败。这两条生产路径与测试文件在基线和候选没有改动；
已定位为旧 fixture 契约缺口，仍需以真实资源更新并清理该测试文件类型，不能让生产放宽校验。
该测试文件及其旧 support helpers 本身有大量未注解接口，尚未改写；此处没有删除、skip
或放宽任何断言，也没有把这次扩展报告为全绿。

随后针对输入寿命、删除 fence、独立开关、控制失败、Sleep、真实 task operation 与 headless
生命周期的八文件回归为 **168 passed / 6 subtests passed**。两批有重叠，不相加当作独立样本。
另外 experience/vector/manual-memory、已删除输入、corruption、学习关闭、effect sandwich、
SQLite 和来源生命周期九文件回归 **115 passed / 6 subtests passed**。这两批发生在来源
命名空间修正前；最后的 162 项回归在修正后的最终代码上重新覆盖来源相关部分，不声称
所有批次都是互不重叠或最终代码上的独立测量。

本轮不宣称完整 0.6.3 完成：

- 登记边界是 `_ensure_durable_task`；已被接收但尚未进入该生命周期的排队输入、升级前
  已执行但无 receipt 的输入尚未覆盖，必须继续验证真正的 intake 与 restart 路径。
- 单次 receipt 写故障后的持久隔离已验证；receipt 与隔离同时写失败的多重故障未验收。
- 完整 manifest 调度、混合 crash/recovery、跨 scope 派生数据的删除归属仍按路线图继续。
- live receipt 与永久退役历史有增长成本，尚无完整 retention/容量验收；不能丢弃身份来省空间。
- 不据此扩大 knowledge 来源、语言 family 或模型配置的晋级范围；World Model 仍未实现。

回滚须保留 schema 6、origins、generation 和退役数据；旧 schema 5 程序冷启动会拒绝 schema 6。
不要清库降级；有问题优先前向修复，不以旧版本重新学习已删除输入。
