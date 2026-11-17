# Hebbian 内部记录时间与普通消费者

- 日期：2026-11-17
- 状态：`EXPERIMENT_COMPLETE`（原始负结果，生产修复另提交）
- 关联：0.6.4；基线 `e60757b4`。
- 前序：已完成 [event envelope 原子重建](hebbian-replay-integrity-20261117.md)，不重做该矩阵。

## 冻结配置

目标：验证合法 event ts 下，node/edge 内部坏时间是否仍被重建接受、污染有效投影，
以及普通 directive 是否因此出错。主要变量仅为一条已提交事件的内部时间字段；
使用真实人工确认、supersession/coactivation、SQLite graph 和普通重建/读取/directive。
不调用模型，不声称模型行为收益；model、endpoint、采样配置不适用。

矩阵为 user/project/session × node/edge × 9 variant = 54 cell，每项暖/冷重建各一次。
variant：valid；created_at/updated_at/last_reinforced_at/last_decayed_at 各一个非法日期；
nonstring（False 基点）；missing_anchor（三个有效衰减基点全部空）；legacy_optional
（created_at/last_decayed_at 空）；legacy_fallback（updated_at/last_decayed_at 空、保留
last_reinforced_at）。合法旧记录允许空可选时间，但每个非空时间必须合法，且至少保留
一个可解析衰减基点；不从 wall clock 补造历史。

18 个合法对照必须成功，36 个坏时间必须拒绝，重复/重开时有效投影和原日志不变；普通
directive 与事前一致，不得因坏记录产生未处理异常。每项先验证非空有效 projection、
明确 active 集和 edge，再注入故障；合法 legacy 同时保留相应有效投影作对照。
记录 accepted、projection_unchanged、directive_unchanged、consumer_error、active 集。
停止条件为 fixture 无效、测试触及非隔离 state 或假设被否定；不降门槛或删断言。

## 接入与验收计划

有缺口才在 Hebbian 解码边界校验原始时间，复用现有类型与事务；影响重建、普通 reader、
Sleep 和 directive 的正常消费者，不加默认关闭开关。保留合法旧日志、删除/reset 和
最后有效 projection；不改变权限、Evidence、CompletionProof 或模型配置 allowlist。
补充真实 CLI、坏 projection reader、普通 Sleep、关闭控制、删除/reset/重启和写入故障
相关边界，复用前序有效证据；每个完成阶段独立提交并更新进度板/路线图。

运行相关回归及完整 Ruff/mypy/Pyrefly/ty/precision gate；新增测试和修改生产文件
定向三类型及额外 Any 审计。原始日志/state 只留本地，不运行全量 pytest。

完整 G0 与 World Model 仍未完成；本项结束后继续稳定排序/digest 与真实恢复依赖，
不能将内部时间校验冒充 World Model 闭环。

## 原始结果

54 cell 全部有效，18 合法对照通过、36 坏时间全部失败：暖/冷重建都错误返回 True，
且替换了原有效 projection。node 的 last_decayed_at 和 missing_anchor 共 6 cell
进一步使普通 directive 抛出 ValueError；其他坏字段不一定在本次渲染读取，但仍污染历史投影。
原始日志保持不变，旧可选时间与真实 last_reinforced_at fallback 均正确，不能一律禁用。

相关 3 文件 171 项回归通过，完整生产树五静态门及新增文件定向三类型检查通过；
额外 Any 审计本文件零诊断。审计 exit 1 来自未修改依赖的 3897 项/182 文件诊断，
未增加豁免或改变检查范围。
不需要模型服务，不把本次 deterministic 存储失败计为模型阴性。
原始 stdout/XML、隔离 pytest state 路径及全部静态检查日志留在
`.e2e-artifacts/ghost-hebbian-row-time-20261117`；
[脱敏 54 项矩阵](../../tests/manual/ghost_memory_routing/reports/hebbian-row-time-baseline-20261117.json)
可以与下面命令对应复查。

```powershell
python -m pytest tests/test_ghost_hebbian_row_time.py -q -s --tb=short
```
