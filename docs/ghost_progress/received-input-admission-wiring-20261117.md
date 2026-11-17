# Ghost 输入接收登记与删除接线

- 日期：2026-11-17。
- 实验基线：`8893c132`（生产代码 `78c907de`）。
- 状态：`PRODUCTION_MERGED`（限定本次新接收输入）；不是完整 0.6.3 验收。
- 生产提交：见 `git log --grep="bind input lifetimes before durable admission"`，与本记录及报告同提交。
- 模型 / endpoint：确定性 transport；不调用模型或 HTTP，不恢复已停止的语言实机批次。

## 假设与接线

[接收后删除负结果](received-input-deletion-20261117.md)证明执行入口登记晚于真实 intake。
本次只把相同的 run/session/project 身份协议前移：在新的 canonical task commit 前登记，
而不是在 commit 订阅、调度或首次投影时补登记。身份登记仍只保存删除所需的 ID/scope，
不保存任务正文，不是第二套 canonical 日志。

`SessionKernel.before_admit` 是可选的具体类型回调，仅在新 task 尚不存在时调用，发生在
`store.commit` 和 scheduler/publication 之前。durable 核心不 import Ghost。
应用层 `DurableAppServices.open` 默认把它接到 `ghost_input_admission.register_ghost_input`，
使用该应用 runtime 目录中的既有 `ghost.sqlite3`；覆盖 InputQueue placed / busy queue、
普通 submit_task、headless 和 operation 直接接收。已有 task 的 claim、start、编辑和恢复
不重复创建接收寿命；执行入口的幂等检查和持久 retired-input fence 保留。

Ghost 删除无需与 canonical 数据库共享跨库事务：登记先于任务可见，删除发生在登记之后、
canonical commit 之前时，也能退役该 ID。canonical commit 被拒绝可能留下无正文的 receipt，
它自身不能调度或产生记忆；未把这一事实描述成跨库原子性。

未开启新的学习开关，不覆盖用户显式关闭。`state_home=None` 的应用只在临时 runtime 目录
保存身份元数据，Ghost memory store 仍是 None，不读取或写入用户长期记忆。
单次 receipt 插入故障沿用 observation store 的持久 quarantine，canonical task 继续；
登记失败仅记录有界错误类别，不记录正文。

## 固定矩阵与结果

复用原 8 cell：user/project/session delete 与 reset × accept_events 开/关。
不修改任务、控制、scorer 或原始基线报告；新增报告另存：

```powershell
python -m tests.manual.ghost_memory_routing.received_input_delete_probe --output tests/manual/ghost_memory_routing/reports/received-input-delete-intake-20261117.json
```

[接收登记候选报告](../../tests/manual/ghost_memory_routing/reports/received-input-delete-intake-20261117.json)：

| 控制 | 有效 cell | 基线有旧输入 cell | 候选有旧输入 cell |
|---|---:|---:|---:|
| accept_events 开 | 4 | 4 | 0 |
| accept_events 关 | 4 | 0 | 0 |

8 cell 均满足真实 intake 投影、provider 尚未进入、删除成功、后续 provider 到达且任务 done。
每 cell 最终 observation / continuity 都是 0 / 0。这里仍不是模型行为收益或完整 HTTP 验收。

新增 `tests/test_ghost_received_input_lifetime.py` 的 27 项验收已通过，包括：

- 相同 8 cell。
- busy queue × 四类删除 × 原进程/重启，共 8 项；重复接收、编辑、放置后仍不重学。
- 登记后、canonical commit 前的可控并发删除；commit/publication 后旧 ID 仍不可写，新 ID 可写。
- receipt INSERT 故障后 canonical 输入照常执行，Ghost quarantine 不因存储恢复而解除。
- 删除后新输入的真实任务、settlement 与 Sleep 在原进程/重启均正常产生 observation 和 continuity。
- session/project 删除保留无关 scope；普通 submit_task 在 worker 接收前、headless 在 operation
  入口前已登记，并经真实任务路径验证删除；不把 worker stub 称为完整 HTTP 调度。
- durable 核心回调先于发布、幂等重试及 claim 不重复调用；临时应用不启用用户记忆。
- spawn 子进程在 receipt 提交后、canonical commit 前以退出码 23 终止；重开真实应用后确认
  task 尚不存在，删除孤立 receipt，再用同 ID 重试并真实执行，仍不重学旧身份。

## 生产回归与检查归属

扩大回归命令：

```powershell
python -m pytest tests/durable tests/test_ghost_received_input_lifetime.py tests/test_ghost_inflight_input_lifetime.py tests/test_ghost_deleted_input_fence.py tests/test_ghost_deleted_source_lifetime.py tests/test_ghost_projection_delete_fence.py tests/test_ghost_sleep_source_controls.py tests/test_task_submit.py tests/test_task_submit_durable.py tests/test_headless_runner.py tests/test_headless_real_kernel_lifecycle.py tests/test_headless_api_model_selection_admission.py tests/test_server_lazy_state.py tests/test_architecture.py -q
```

此时新增接收测试为 26 项；该批 **1133 passed / 6 failed / 5 skipped / 488 subtests passed**。
这是有效的非全绿结果，未删断言、跳过失败项或放宽生产校验。

- `test_startup_failure_never_announces_ui_ready`：runtime 发布清单已落后于现有源码和静态资源，
  新增接收模块也需纳入发布。运行 `python -m tools.publish_runtime` 同步 `codey/runtime.json`，
  包含既有缺项，不改实际资源；随后该项与最终 27 项接收验收一起复验 **28 passed**。
  `python -m tools.publish_frontend --check` 通过；未修改前端源码或资源。
- server import 及 consensus_service/task_submit 两个 subtest 提前加载浏览器：用内存 import
  loader 加载本轮三个已修改既有模块的 `8893c132` 源码，在不复制 worktree、不改生产文件的
  子进程复查中，三个入口的基线和候选都加载相同的 53 个受限模块（集合逐项相等）。本地证据
  为 `.e2e-artifacts/ghost-intake-admission-20261117/baseline-import-comparison.json`。
  此处是既有生产懒加载缺口，不改测试
  使其接受浏览器，也未声称本轮已修复。
- 两项 architecture 检查：七个长文件超出既有限额；runtime/core/models.py 依赖
  `codey.protocols.json_repair`，而静态契约未列入。逐项与 HEAD Git 对象比较，相关源码、
  `tests/test_architecture.py` 和 lazy 测试均未变化。长文件债务是既有实现问题；json_repair
  的归属尚需专门核对，不能仅凭旧 allowlist 自动判定修生产还是修测试。本轮不提高尺寸限制、
  不扩大 allowlist，也不把这两项改写为通过。

因此初始六项失败中，发布清单对应项已经修复，另外五项（含两个 subtest）仍单列；
没有为报告全绿而缩小原批范围。相关未改动的 Research cutover 两项旧 fixture 缺口仍按
前序输入身份记录保留，本轮不把它们写成已修复。

补充 Ghost 控制与结算回归：

```powershell
python -m pytest tests/test_ghost_post_turn.py tests/test_ghost_post_turn_affinity.py tests/test_ghost_post_turn_work_queue.py tests/test_ghost_control_contract.py tests/test_ghost_control_surface.py tests/test_ghost_controls.py tests/test_ghost_sleep.py -q
```

结果 **72 passed**。这些批次有重叠，不相加宣称最终全套通过。

按配置依次执行以下完整门，均通过；mypy 覆盖 700 个生产模块，Pyrefly 为 0 errors、
35 个既有 suppression、159 个 warning，未改配置或增加忽略：

```powershell
python -m ruff check .
python -m mypy codey --python-version 3.11
pyrefly check codey --python-version 3.11
ty check codey --python-version 3.11
python -m tools.kernel_type_precision_gate
```

新增测试文件另外通过 mypy、Pyrefly、ty（均带 `--python-version 3.11`）。未运行全量 pytest、
完整新语言批次或模型 HTTP；静态门通过不等于上面的既有生产/架构缺口已经闭合。

另加 `--disallow-any-expr` 对四个改动生产文件及新增测试审计：所改五文件零表达式诊断。
初次测试审计发现旧队列返回值与 runner 回调会带入 Any，已改为具体端口、返回值校验与明确
回调签名；最终 27 项再验通过。该额外模式连同传递依赖仍有 354 个未修改文件的 11285 项
诊断，整体退出非零；它不是配置中的全树生产门，也不是全仓零 Any 证明。完整输出留本地
`.e2e-artifacts/ghost-intake-admission-20261117/final-any-expression-audit.txt`，未加忽略或缩小门。

## 边界、回滚与下一步

当前覆盖本次代码接线后、新接收且身份/scope 可登记的输入。升级前已经存在、从未登记的
canonical 输入没有自动补认成安全；receipt 与 quarantine 同时失败、身份/scope 冲突后的
完整恢复和 orphan receipt 的容量/保留仍需后续实验。不能用执行时补一个新身份掩盖这些缺口。

完整 durable manifest 调度、七 store 混合 writer/reader/crash/restart、来源所有权与维护撤回、
显式时间/replay 及 World Model 仍未完成。其他输入和独立知识命名空间不应被扩大退役。

用户可继续关闭 accept_events / admit_hints。代码回滚可撤回本次应用接收回调，但这会恢复
接收后执行前的已知缺口；应先停止相关 Ghost 消费。保留 schema 6、origins、generation 与
retired-input/source 元数据，不以删除安全表回滚；旧 schema-5 代码不能直接冷启动 schema 6。
