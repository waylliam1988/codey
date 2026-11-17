# Ghost 普通 scope 删除的领域历史保留基线

- 日期：2026-11-17；生产比较基线：`f09eddd2`。
- 状态：`EXPERIMENT_COMPLETE`（基础设施负结果，无模型或HTTP调用）。
- 合同：Roadmap 0.6.3要求普通tombstone scope delete不物理重写历史事件；删除来源不再
  进入消费者，重启和重放不得复活。物理purge与容量retention需要独立、显式的策略。

## 冻结矩阵与有效性

七个真实store × user/project/session × 不重放/重放，共42个删除cell；另有21个不删除
对照。所有cell先检查领域事件非空且普通reader能消费目标来源，然后从普通
GhostControlSurface.dispatch_action执行默认持久Manifest，验证completed/tombstone、
删除后不再可见及重开/重放不复活，最后比较原SQL事件的sequence、identity及payload前缀。
这是低于现有容量阈值的普通删除实验，不把一次容量compaction混入主要变量。

inbox使用现存内部reducer及projection writer重建；hebbian、continuity、work queue、
affinity使用现有公开rebuild。Sleep和observation仅重开，不为实验虚构不存在的重建API。
输入用合法独立store、人工确认偏好、独立研究continuity及显式用户待办，避免授予假receipt。

初次夹具错误使用continuity.state_home、未向affinity reader传入scope上下文；第二次又调用
不存在的inbox公开rebuild。这两次准备失败不算生产负结果，日志保留本地。改为真实database
目录、明确session/project及既有内部reducer后，最终63项均执行到消费者和历史判断。

```powershell
python -m pytest tests/test_ghost_scope_history_retention.py -q --tb=short --junitxml=.e2e-artifacts/ghost-scope-history-20261117/baseline-final.xml
```

## 结果

**39 passed / 24 failed**：21个未删除对照全通过；continuity、work queue、affinity的18个
删除cell全通过。inbox、Hebbian、Sleep、observations各6个删除cell失败，均在最后的
历史保留断言；它们的删除隐藏、Manifest完成和本次重开/重放检查已通过。
不能把“没有复活”等同“满足历史保留”：前两者重写剩余状态快照，Sleep移除旧report事件，
observation物理删行，原sequence/identity/payload丢失。

[脱敏逐cell报告](../../tests/manual/ghost_memory_routing/reports/scope-history-retention-baseline-20261117.json)
不保存临时路径、原始事件正文或测试state。日志留本地
`.e2e-artifacts/ghost-scope-history-20261117/`。

## 类型与后续

新增测试不引入Any；额外mypy --disallow-any-expr为本文件零诊断，未修改依赖191文件4085项
诊断使扩展命令exit1。既有inbox/Hebbian/Sleep/experience回归 **75 passed / 133 subtests passed**。
全树Ruff、完整codey的mypy（701模块）、Pyrefly（0 errors / 35既有suppressed / 159 warnings）、
ty和precision gate依次通过；新测试另经三种检查通过，无新增豁免。
不运行全量pytest或停止的语言批次。下一步以追加领域删除事件和永久退役过滤修复普通生产路径，保留新输入、其他scope、
重放和故障恢复，不自动改变reset/purge或宣称完整retention/0.6.3/World Model完成。
