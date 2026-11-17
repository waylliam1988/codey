# Ghost 接收登记与隔离双重故障

- 日期：2026-11-17。
- 冻结基线：`84c57aac`，接收前登记已默认生效。
- 状态：`EXPERIMENT_COMPLETE`（有效负结果）；`EXPERIMENT_ONLY`，未修复或生产晋级。
- 实验提交：与本记录、脚本和脱敏报告同提交，可用 `git log --grep="expose intake receipt quarantine double failure"` 定位。
- 模型 / endpoint：确定性 transport，不调用真实模型或 HTTP，不恢复已停止的语言批次。

## 目标与固定变量

假设：接收时 `ghost_input_origins` 登记失败，且 `ghost_retired_inputs` 隔离也失败的输入，
不能因为 Ghost 存储恢复、删除/reset 或重启，就在原 canonical task 中重新被当作新输入学习。
普通任务不能依赖 Ghost 存储健康才能完成；用户显式关闭仍应保留。单次登记失败已有隔离
正结果，本次不以它代替双故障证明。

使用真实 AppContext、InputQueue placed/pending 接收、canonical claim/start、任务结算与
后台 Sleep。故障只由目标 run 的两个 SQLite INSERT trigger 注入，不修改生产函数或 scorer。
每格独立 state；输入、确定性回答、身份、任务预算固定，不测语言行为收益。

主矩阵：

- 双故障 × user/project/session delete 与 reset × accept_events 开/关。
- 恢复时点分为 intake 后删除前、provider 已进入但尚未返回，两种，共 16 格。
- 额外复验 intake 后删除前恢复的 8 格：接收后关闭应用，使用相同 state 重开再删除/执行。
- 单登记故障 / session delete × 两个恢复时点 × 控制开/关，共 4 格，校准持久隔离。
- 无删除且控制开启的 healthy / 单故障 / 双故障，各 1 格，校准正常学习与故障恢复。

共 31 格。复用前轮健康删除矩阵，不重新补跑健康的全部 scope。

## 有效性、指标与停止条件

每格必须证明：正常接收得到 submitted；canonical task 为 pending/accepted/placed；
provider 尚未进入；尚无 observation/continuity；故障 trigger 正确安装，故障输入登记确实
被拒绝；主路径删除成功；恢复后 provider 到达、任务 done，实际 Sleep 完成。
任一前提失败记作无效 fixture，不能计成安全通过或模型阴性。保存接收时 receipt / retired
存在性、登记警告数和终态 observation / continuity 数；报告不保存正文或临时绝对路径。

删除后且控制开启的旧输入投影必须为 0；关闭组同样为 0。单故障恢复后仍应被隔离。
健康无删除组应有真实投影，证明 fixture 没有整体关闭 Ghost。双故障无删除组单列：它检验
已知安全登记失败是否被记住，不与删除效果混为一个指标。模型、HTTP 或完整浏览器调度
均不属于此实验的证明范围。

先提交有效负结果及复现材料，再据结果选择生产修复。若需要持久故障状态，使用已有
canonical 输入/产物和 Ghost 删除元数据，不增加第二个任务日志；不能靠阻塞普通任务、
关闭全部学习、墙钟比较或把旧身份改成新身份绕过故障隔离。

## 命令与结果

```powershell
python -m tests.manual.ghost_memory_routing.intake_double_fault_probe --output tests/manual/ghost_memory_routing/reports/intake-double-fault-baseline-20261117.json
```

[脱敏报告](../../tests/manual/ghost_memory_routing/reports/intake-double-fault-baseline-20261117.json)
包含 31 个有效 cell，0 个无效 cell；健康无删除校准产生 1 observation / 3 continuity，
确认实验没有整体关闭 Ghost。

| 分组 | cell | 产生旧输入投影的 cell |
|---|---:|---:|
| 双故障、删除、学习开启（含两种恢复时点及重启） | 12 | 12 |
| 双故障、删除、学习关闭 | 12 | 0 |
| 单登记故障、删除、开/关 | 4 | 0 |
| 单登记故障、无删除、开启 | 1 | 0 |
| 双故障、无删除、开启 | 1 | 1 |
| 健康、无删除、开启 | 1 | 1 |

所有泄漏 cell 均产生 1 observation / 3 continuity。双故障的晚恢复场景还观察到执行入口
登记失败；这没有阻止恢复后的结算和后台 Sleep 消费旧输入。警告数只作证据，不作为安全门。

结论：接收登记和备用持久隔离共享同一 Ghost 数据库，两者同时失败后，没有可靠状态记住
这次失败。删除不知道该 run 身份；存储恢复后执行登记或投影写口会重新接受旧输入。
单故障 quarantine 的正结果不覆盖此缺口。普通 canonical 输入仍接收、执行并完成，
生产修复必须保留这一点。

本轮只新增实验材料，不改变生产默认行为，故无需生产回滚。后续应在已有 canonical 输入
存储中持久保存安全接收结果，并让实际结算、Sleep 及来源写口遵守它；不能只增加入口重试、
进程内标记或另一个日志。具体修复设计与验收另记。

检查命令与结果见下节。完整 manifest 调度、升级前无 receipt 输入、身份/scope 异常、
确定性 replay 和 World Model 不因此自动完成；无模型、HTTP 或完整浏览器调度证据。

## 静态检查

实验脚本通过 mypy、Pyrefly、ty 的针对性检查（均带 `--python-version 3.11`）。
额外 mypy `--disallow-any-expr` 审计中本脚本零诊断；传递依赖仍有 352 个未修改文件的
11232 项诊断，退出码 1，不宣称全仓零 Any。本地审计输出为
`.e2e-artifacts/ghost-intake-admission-20261117/double-fault-any-audit.txt`。

本轮按顺序重跑以下门全部通过：

```powershell
python -m ruff check .
python -m mypy codey --python-version 3.11
pyrefly check codey --python-version 3.11
ty check codey --python-version 3.11
python -m tools.kernel_type_precision_gate
```

mypy 覆盖 700 个生产模块；Pyrefly 为 0 errors、35 个既有 suppression、159 个 warning。
本轮生产未修改，实际 31-cell 探针已覆盖此次实验所需任务路径；未重复扩大回归、未跑
全量 pytest，前轮记录的既有懒加载和架构回归失败仍未修复。
