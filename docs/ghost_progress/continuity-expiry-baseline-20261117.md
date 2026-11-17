# Ghost continuity 过期持久投影与消费者基线

- 状态：`EXPERIMENT_COMPLETE`（原始负结果）；关联：0.6.4/0.6.3。
- 基线生产 commit：`405410f5aab87f4e1beef722430f22de7fe9b505`。
- 模型/endpoint：不使用，不恢复已停止语言实机批次，不修改生产代码。

## 冻结矩阵

同一记录的expires_at为2026-04-01T00:00:00Z，创建时间为1月1日。三scope × 六场景
共18 cell：过期前一秒、恰好过期、过期后、过期与有效项共存、过期同时有新来源、
只读消费者尚未维护。所有ID/来源和初始事件固定，project使用同一真实路径；真实SQLite
事务种子，使用普通Sleep与真实work queue同步，不以新接口是否存在判断。

每cell先确认种子、有效事务和维护成功，再检查：维护报告total与原持久投影一致；
应保留的有效项仍可读/生成队列，过期项不进入普通reader、不创建新的work item；
renderer过期控制正确；两次在过期前/后墙上时间重建，原记录集合与digest不变。
直接读取原始事件表验证维护前历史逐行保留，重建不写历史；读消费者不修改continuity。
新来源场景由真实Hebbian输入生成确认偏好，允许生成一条新的continuity；该偏好不生成open-question工作项。

只读场景允许原始持久记录尚未清理，但普通消费必须筛掉过期项。过期不是永久删除，
不能用source retirement阻止同来源有新事实后重新出现。维护后恰好过期/已过期应为空，
混合场景保留一条有效记录，新来源保留一条新记录；之前一秒对照保留旧项且可生成队列。
比较行为行与digest，不比较报告UUID、耗时及审计时间。本实验不证明已经存在的独立work
item应随来源TTL撤销，也不证明整个模型发送、混版本或World Model完成。

## 命令与结果

已运行：`python -m pytest tests/test_ghost_continuity_expiry_boundary.py -q --tb=short`，
另保存JUnit与完整捕获。18 cell全部有效，3通过、15失败、0夹具错误/跳过。
运行期间702个生产Python文件冻结并复核哈希；未修改生产、断言或评分。

| 场景 | 三scope结果 | 归属 |
|---|---|---|
| 过期前一秒 | 3通过 | reader、renderer、真实队列正对照有效 |
| 恰好过期、过期后、混合有效项 | 9失败 | 报告已排除过期项，但持久投影仍有旧记录，实际队列继续生成旧工作 |
| 过期同时有新来源 | 3失败 | 当次持久投影正确，但固定事件重建恢复旧项 |
| 只读、尚未维护 | 3失败 | reader和新队列消费过期项；renderer正确过滤 |

两种重建时钟均得到相同结果，排除随机ID和当前重建时钟造成的差异；原始历史保留和
读操作无写入断言均通过。失败违反过期消费及事件重建契约，应修生产。
相关continuity/work queue/Sleep/显式时间回归135通过、2子测试通过。
全树Ruff、mypy、Pyrefly、ty、precision及本实验三类型检查通过；额外Any审计仅以
本文件诊断为准，导入的未修改依赖诊断另列于脱敏报告，不能称全树Any清零。
脱敏报告：`tests/manual/ghost_memory_routing/reports/continuity-expiry-baseline-20261117.json`。
本地产物：`.e2e-artifacts/ghost-continuity-expiry-20261117/baseline*`。

## 下一步

以真实结果修复持久过期事件/重建和普通消费者，保证逻辑时间贯通；保留原始历史、
新事实可用性、容量窗口和事务回滚，再运行相关生产回归、全树静态门与owned Any审计。
