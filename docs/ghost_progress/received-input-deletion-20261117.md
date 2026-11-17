# Ghost 已接收、未执行输入的删除边界

- 日期：2026-11-17
- 基线：`78c907de`，执行入口的 input origin 已默认接线。
- 实验结论：`EXPERIMENT_COMPLETE`（负结果）；接收端修复仍 `INCOMPLETE`。
- 模型 / endpoint：有效矩阵使用确定性 transport，不调用真实模型或 HTTP，不恢复语言实机批次。

后续：[接收登记与删除接线](received-input-admission-wiring-20261117.md)已在新输入的 canonical
commit 前默认登记，相同 8 cell 中开启状态旧输入 4→0；原负结果和无效 fixture 不重写。
下面的“修复仍 INCOMPLETE”和下一步描述对应本次负结果提交时；当前状态以进度板和后续记录为准。

## 固定矩阵与有效性

假设：普通 InputQueue 已接收的输入，即使尚未进入 `_ensure_durable_task`，删除后也不应
在首次执行中被 Ghost 重新消费。唯一阶段变量是删除发生在 intake 后、execution entry 前。

矩阵为 user/project/session delete 与 reset × 当前 accept_events 开/关，各一次，共 8 cell。
使用真实 AppContext、DurableConversationInputQueue.create、GhostControlSurface，以及共享
run_task_submission 的真实任务结算和后台 Sleep。调度器在 fixture 中显式关闭，只注入
确定性 provider；通过实际 durable claim/start 转换后在独立 worker thread 执行任务。

这里测的是 **placed、pending dispatch** 的已接收输入，不是 busy conversation 中
placement=queued 的等待/合并输入；也不是 HTTP/browser worker 的完整调度验收。

每 cell 必须同时满足：intake 返回 submitted 投影；canonical task 是 pending/accepted、
input_state=placed；provider 尚未进入且 Ghost projection 为空；删除成功；后续 provider
确实进入且任务正常 done。任一不满足即为无效 fixture，不可当作安全通过或模型阴性。

## 结果与夹具修正

```powershell
python -m tests.manual.ghost_memory_routing.received_input_delete_probe --output tests/manual/ghost_memory_routing/reports/received-input-delete-baseline-20261117.json
```

[有效基线指标](../../tests/manual/ghost_memory_routing/reports/received-input-delete-baseline-20261117.json)：
8 cell 全有效，无效 cell 为 0。

| 当前控制 | cell 数 | 每 cell observation / continuity | 有旧输入 cell |
|---|---:|---|---:|
| accept_events 开 | 4 | 1 / 3 | 4 |
| accept_events 关 | 4 | 0 / 0 | 0 |

这证明执行入口登记仍晚于普通接收边界。删除时既无 Ghost projection，也尚无 input origin，
首次执行会在删除后为旧 canonical 输入登记一个新 live identity。不是缺少第二模型，也不是
语言 hint 未生效；不能通过减少断言或打开更多学习开关修复。

两次早期夹具均有 8 个无效 cell，单独保留：

- [初始无效指标](../../tests/manual/ghost_memory_routing/reports/received-input-delete-fixture-invalid-20261117.json)。
- [隔离调度后的无效指标](../../tests/manual/ghost_memory_routing/reports/received-input-delete-fixture-invalid-worker-20261117.json)。

夹具原先把 placed input 的 UI state 错认为 queued；现行投影为 submitted。此外直接把
accepted checkpoint 交给裸 operation，会缺少正常 worker 的 claim/start，出现
`CommitConflict: task does not admit new external effects`，provider 未到达。已按当前
生产生命周期修正 fixture；这些失败不算 Ghost/模型的有效负结果，也未为其修改生产代码。
原始无效报告中的 transport 描述不能作为“provider 路径已验证”的证据，以有效矩阵为准。

## 验证与下一步

实验脚本参数、回调、映射边界和结果类型明确，分别通过 mypy、Pyrefly、ty；全仓 Ruff、
完整 699 模块的 codey mypy、Pyrefly、ty 和 kernel precision gate 通过。Pyrefly 完整树
0 errors、35 个既有 suppression、159 个 warning；未修改配置或增加忽略。
本轮仅新增实验脚本和脱敏报告，生产代码仍为 `78c907de`；不重复全量 pytest 或语言模型批次。

下一步把相同身份协议接到真实 intake producer，覆盖 InputQueue、普通 /api/run、headless
以及恢复路径，验证 busy-queue placement 与并发删除；不能在 dispatch 时为已删除的旧输入
补一个新身份。继续使用 canonical input 与既有 Ghost 删除元数据，不靠跨库墙钟时间比较，
不新增第二个任务日志。完成必要边界后直接默认启用并单独提交。

完整 manifest 调度、多重故障、确定性 replay、其他消费者和 World Model 仍未完成。
