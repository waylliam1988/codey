# Ghost 升级旧投影的默认消费者准入

- 日期：2026-11-17；比较基线：`286503dc`（生产基线`9e318370`）。
- 状态：`PRODUCTION_MERGED`（本记录随默认生产消费者修复提交）。
- 前置：[24项旧投影准入负结果](legacy-projection-admission-baseline-20261117.md)，
  [逐cell原始指标](../../tests/manual/ghost_memory_routing/reports/legacy-projection-admission-baseline-20261117.json)。
- 模型/配置：确定性基础设施修复；实际应用任务使用确定性provider，不调用模型/HTTP，
  不恢复停止的语言实机批次，不新增模型收益主张。

## 冻结合同与改动

原始canonical admission receipt为true才允许绑定应用中的run投影成为消费者输入。
缺receipt保持未获准，false保持隔离，不凭旧投影存在、可修改的content或人工猜测补true。
已有退役来源同样不得进入消费者。独立Ghost、人工确认偏好、研究笔记各自保留其来源命名空间。

普通observation `read_committed → retrieve → vector/lexical ranking` 默认在SQL WHERE
复查原始receipt与retired identity，之后才按新近排序及LIMIT；更近的未准入旧记录不能
挤掉仍在窗口内的健康记录。没有新增后台调用、第二份receipt或migration授权表。

continuity和work queue的公共list默认复用已有live_sources；先筛来源再按投影容量裁剪，
renderer、控制面summary及后续来源转换共用这个入口。claim_next另对事件重放后的候选
以及stale-claim转换后的候选复查，防止只过滤projection写入、却先把执行项返回给调用者。
事件重放自身的既有容量规则不变，不承诺找回早已被历史容量裁剪丢弃的数据。

私有raw load继续服务删除、重建和显式审计；公共消费入口显式选择live来源。
该内部参数不是产品开关，普通消费者默认启用此修复。读取本身不清除旧原文或事件，
raw read_all/export仍能供操作员审计；审计数据不等同当前可投递/可执行数据。
已有rebuild会按现存live_sources规则重建有效projection，不重写canonical原始receipt。

## 冻结验证与当前结果

原24项矩阵、来源、receipt和scorer保持不变，初步修复后 **24/24通过**；
连同控制面与canonical receipt回归首组 **103 passed**。
新增边界后本文件 **47 passed**，包含：

- None/false读取及拒绝claim后，旧原文、record、事件与原始receipt均保留；
- observation先过滤再LIMIT；缺/坏authority及原始true但已退役的四类消费者均关闭；
- 原始receipt为None/false时，独立研究笔记和人工确认Hebbian仍有真实continuity消费者；
- rebuild、Sleep、compaction入口及重开后，原输入不重新获得资格；
- 实际AppContext/InputQueue/task_entry结算和Sleep仍能接收并学习健康新输入，重开后仍可检索。

额外Any审计发现work_queue现有claim排序lambda受sorted重载上下文影响推断为Any。
改为明确GhostWorkItem参数及tuple返回值的命名回调，不改排序字段；最终扩大回归在
此源码上执行。生产Python在实际任务和子进程回归期间固定，不边跑边修改。

最终扩大回归 **553 passed / 8 subtests passed**，无失败；脚本末轮类型清理后另批
**50 passed**。脱敏汇总见[生产报告](../../tests/manual/ghost_memory_routing/reports/legacy-projection-admission-production-20261117.json)。

### 扩大回归中旧 A/B 夹具的归属

首批扩大回归为 **552 passed / 1 failed / 8 subtests passed**；失败是
`test_production_spine_work_queue_beats_no_queue_baseline`。通过 Git 对象把比较基线
`9e318370` 的三个生产模块加载到内存后，同一脚本仍失败：五个 queue 用例仅4个匹配，
project-followup退回chat，baseline exact2/cost5、queue exact4/cost3，无运行异常。
因此没有为迁就夹具而移除receipt检查，也没有降低五个用例的评分门槛。

旧脚本直接构造WorkCheckpoint，没有相应canonical输入。这种来源早已被生产写口拒绝，
不能用伪造true receipt修复。现在通过AppContext配置的真实admit_input登记同session/project
前序输入，读取原始commit验证true，再claim/start/cancel，留下合法中断checkpoint。
生命周期准备在既有control_line中完成，防止live scheduler同时抢走前序任务。
首次夹具修订未加该事务锁，出现scheduler抢占，属于无效准备，保留本地日志，不算生产负结果。

脚本改用生产build_task_deps并显式传入provider，事件字段经object边界校验，报告与汇总用
TypedDict，命令行使用类型明确的Namespace；修改文件原有Any同步清除，保留原子部分报告、
五个任务、两臂、原scorer及安全mode stub。修订后脚本三项测试与新增47项合计 **50 passed**。
随后额外Any审计五个owned文件零诊断；未修改依赖352文件11584项诊断使该扩展命令exit1，
不能将其描述为全树无Any。最终扩大回归与正常三种类型检查另录下文。

### 最终命令与静态门

```powershell
python -m pytest tests/test_ghost_legacy_projection_admission.py tests/test_experience_memory.py tests/test_ghost_vectors.py tests/test_ghost_continuity.py tests/test_ghost_work_queue.py tests/test_ghost_work_queue_ownership.py tests/test_ghost_work_queue_ab.py tests/test_ghost_sleep.py tests/test_ghost_sleep_source_controls.py tests/test_ghost_canonical_input_receipts.py tests/test_ghost_received_input_lifetime.py tests/test_ghost_inflight_input_lifetime.py tests/test_ghost_deleted_input_fence.py tests/test_ghost_deleted_source_lifetime.py tests/test_ghost_projection_delete_fence.py tests/test_ghost_post_turn.py tests/test_ghost_post_turn_affinity.py tests/test_ghost_post_turn_work_queue.py tests/test_ghost_control_surface.py tests/test_ghost_control_contract.py tests/test_ghost_controls.py tests/test_ghost_manual_memory.py tests/test_ghost_language_production.py tests/test_ghost_manifest_production_wiring.py tests/test_ghost_manifest_discovery.py tests/durable/test_operation_driver_restart.py tests/durable/test_legacy_chat_rescue.py -q --tb=short --junitxml=.e2e-artifacts/ghost-legacy-admission-20261117/production-regression-final.xml
python -m pytest tests/test_ghost_work_queue_ab.py tests/test_ghost_legacy_projection_admission.py -q --tb=short
python -m ruff check .
python -m mypy codey --python-version 3.11
pyrefly check codey --python-version 3.11
ty check codey --python-version 3.11
python -m tools.kernel_type_precision_gate
python -m mypy tests/manual/ghost_work_queue_production_ab.py tests/test_ghost_legacy_projection_admission.py --python-version 3.11
pyrefly check tests/manual/ghost_work_queue_production_ab.py tests/test_ghost_legacy_projection_admission.py --python-version 3.11
ty check tests/manual/ghost_work_queue_production_ab.py tests/test_ghost_legacy_projection_admission.py --python-version 3.11
python -m mypy codey/ghost/continuity.py codey/ghost/observations.py codey/ghost/work_queue.py tests/manual/ghost_work_queue_production_ab.py tests/test_ghost_legacy_projection_admission.py --python-version 3.11 --disallow-any-expr
```

正常静态门依次通过：mypy生产701模块无问题，Pyrefly为0 errors / 35既有suppressed /
159 warnings，ty与precision gate通过；两个测试/脚本的三种针对检查也通过，Pyrefly有4项warning。
末行额外Any审计有上述未修改依赖诊断，五个owned文件零诊断。没有cast、ignore、新豁免、
修改检查范围、全量pytest或新增实机模型批次。日志、JUnit和旧源码复现输出留本地
`.e2e-artifacts/ghost-legacy-admission-20261117/`，仓库报告不保存临时路径或原始state。

## 实际消费者、回滚及限制

默认生产消费者包括普通经历检索、可见continuity、控制面summary、work queue选择及
通过这些公共reader构建的后续来源；没有增加待开启的feature flag。用户主动关闭、scope、
发送admission、Evidence/Permission/CompletionProof沿用原边界，本修复不授予任何权限。

无新schema，回滚可回退本生产提交；这会恢复已证实的旧投影消费缺口，不能将回滚视为
receipt迁移或人为接受旧输入。保留原始记录供操作员审计，不自动为旧输入补接收成功。
无法证明来源资格的旧经历暂不参与自动投递/执行，这是本次明确的升级策略；它们没有被
自动提升为已验证记忆。需要未来显式重新确认的旧来源不能伪装成原输入拥有true receipt。

本次只闭合已有run投影的默认读取/选择，不代表所有升级策略完成。自定义canonical布局
首次Ghost写失败恢复、身份/scope异常、完整retention与历史事件保留、显式时间replay、
其余family和真实World Model闭环仍需推进。0.6.3整体保持INCOMPLETE。
