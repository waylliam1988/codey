# Operation 执行与恢复统一验收

## 阶段一：现有行为基线

生产基线：`1e3ea099`。本阶段只增加测试、测量夹具和本文档，不改变生产控制流。

`tests/support/operation_trace_probe.py` 通过真实 `run_task_kernel`、
`KernelRecordedProvider`、`execute_turn`、mutation line 和 durable generation boundary
运行确定性外部观察。它不实现另一个恢复执行器。脚本响应耗尽会报错，不伪造完成。

基线保存在 `tests/fixtures/operation_execution_baseline.json`，包含 21 种轨迹、
provider effect 数、工具执行/回执数、逻辑轮数、历史种类、pending effects、
数据库提交数、checkpoint 字节和耗时。正常轨迹重复三次验证语义相同；耗时取中位数，
只作同机诊断，不设跨机器阈值。

重测命令：

```powershell
python -m tests.manual.operation_execution_baseline
python -m pytest -q tests/test_operation_execution_traces.py
```

需要保持的行为：

- 文本及原生 read→done：两逻辑轮、一次工具执行；原生原 call id 进入结果交付。
- 协议修复及完成拒绝：保留原 call id；修复请求使用原声明；不产生工具副作用。
- completion gate 异常：对外沿用暂不能完成并继续的行为，内部故障分类后续补齐。
- 原生截断：无工具调用时只允许一次续写；合法工具调用的截断行为由既有测试锁定。
- 问题身份稳定；审批保持原调用位置并暂停，不运行命令。
- provider 返回后的取消阻止工具执行；已有工具结果仍正常记录和交付。
- 无进展观察、正常轮数、provider 调用数和协议关闭预算分别计算。
- 预算结束的原生收尾只回答调用，不执行收尾回复提出的新工具。

故障观察点：provider 回复返回、工具开始、post_tools 提交前和提交后。
当前夹具抛出 `BaseException` 绕过控制层异常映射，以保留真实已提交事实。
它是边界观测基线，不代表已验证真实进程重启；后续必须增加 SQLite 子进程强杀及双次恢复测试。

现有专项测试继续锁定工具注册变化、同轮冻结声明、provider 窗口、审批与完成证明；
durable approval/question 专项测试负责真正的等待记录持久化，内核事件探针不代替这些测试。

已确认需要修改、不能作为保持行为的缺口：

- 回复已返回但尚未跨 generation boundary 时，回复内容和待结算状态只在内存里。
- `TaskCheckpoint.phase`、operation leaf、正常 loop 和 durable recovery 各有执行决策。
- 协议修复、done、question、收尾辅助函数可以隐含请求 provider。
- 截断、无进展和关闭计数依赖当前进程；恢复不能重新获得预算。
- 恢复路径直接执行工具；最终应委托同一个 driver 和 `execute_turn`。

上述缺口修复可以改变相应故障结果和提交量，但必须单独说明，不能借机改变正常轮数、
授权边界或重复执行已结算副作用。

## 后续阶段的硬约束

只有 operation checkpoint 与 reducer 拥有当前执行位置。TaskStatus 继续表示调度所有权；
TaskCheckpoint 的 provider/tool phase 不得继续独立决定 operation 的下一步。
effect、delivery、submission/approval 和输出存储分别拥有各自事实，checkpoint 使用引用。
同一事务写入不同事实不属于双写；同一事实存在两套权威状态，即使原子更新也属于重复建模。

公开入口只可委托唯一 driver；旧格式只可做有版本、幂等迁移。禁止新实现失败后运行旧 loop、
从 trace 猜恢复状态、回执缺失后重执行、快照不匹配后换用最新实现，或生产 durable 缺失后转入内存执行。

附件中的 pytest 失败按现行契约逐项分类并回测相关文件；不恢复已删除的生产接口来迁就旧测试。
只运行相关测试，不执行全量 pytest。六阶段完成还需要本地 Gemma 原生/文本冒烟、
真实重启恢复及与 pi/DeepSeek 的等价场景比较，当前阶段未声称这些已完成。
