# Hebbian 内部记录时间与普通消费者

- 日期：2026-11-17 冻结并执行基线；2026-11-18 完成生产验收
- 状态：`PRODUCTION_MERGED`（Hebbian 解码及普通消费者默认路径）
- 关联：0.6.4；基线 `e60757b4`。
- 实验提交：`0cc805486cf97fc510fd22dacb872e712e97bab8`；生产修复独立提交。
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

## 生产修复与默认消费者

`GhostNode.from_payload` / `GhostEdge.from_payload` 在字符串转换前检查四个原始时间字段：
非字符串、不可解析的非空时间和全部为空的衰减基点均拒绝；合法可选空值、真实
last_reinforced_at fallback、naive UTC 和带时区历史继续可读，不读取当前时钟补造时间。
重建沿用既有事务，坏事件不替换最后有效投影，也不改原日志。

本地 master 的普通 `ghost rebuild-state --yes`、reader、Sleep 和 directive 默认使用此边界，
无新增默认关闭开关，不限定模型。坏 projection 使 reader 明确报损坏、Sleep 保留现有
step_error、directive 返回 store_unreadable 并省略提示；普通 App→entry→kernel→HTTP
仍能完成请求，原事件有效时显式重建恢复已确认中文提示。retrieve/admit 关闭仍禁止提示。
本轮不调用真实模型，不把存储可靠性收益或 loopback HTTP 当作模型行为收益。

## 最终验证

原 54 项从 18 通过/36 失败变为 54/54 通过；暖/冷各验证一次。
新增测试文件最终 140 项通过，包含 64 项原始坏值解码、6 项时区与无时钟、12 项
三 scope reader/Sleep/关闭控制/删除或 reset/冷恢复，以及 4 项真实 App 到 HTTP 的消费者验收。
此前候选 181 项/34 子测试与单文件 140 项为不同阶段，不相加。
最终冻结的 27 文件回归为 1031 passed / 34 subtests，零失败/错误/跳过。

全仓 Ruff、完整 codey 树 mypy/Pyrefly/ty/precision gate 与两个修改文件定向三类型检查
通过；702 个生产文件和两个修改文件 SHA256 冻结验证不变。额外 Any 审计 owned 零诊断，
exit 1 来自未修改依赖的 10919 项/338 文件诊断；新增 App/HTTP 回归导入了更多既有依赖，
不能与基线 3897/182 直接作质量差比较。Pyrefly 仍有既有 35 suppressed/158 warnings，
不声称全仓无 Any。未增加豁免、cast 或忽略。原始日志、XML、隔离 state 和冻结哈希只留本地。
不运行全量 pytest，无前端或静态资源改动。完整命令和源码 SHA256 见
[生产报告](../../tests/manual/ghost_memory_routing/reports/hebbian-row-time-production-20261117.json)。

回滚：revert 本生产修复会重新暴露 36 项坏时间覆盖与 6 项普通 directive 异常。
优先保留原始日志和最后有效投影；已有 retrieve_projections/admit_hints 控制可隔离提示。
坏 projection 可从有效原事件显式重建；用户仍可在四开关关闭时 delete/reset。
坏历史不被自动跳过；reset 推进重放窗口后冷重建为空且不复活，历史仍保留。

## 剩余审查

本轮不是任意内容损坏检测，也不自动还原旧版本已经覆盖的投影。记录型 control payload、
稳定排序/digest、其余 replay、完整 App/硬退出/跨 run 恢复及混版本生命周期仍需独立验证。
G0 与 World Model 不能据此标为完成；下一轮收窄到真实验证复盘消费者的收益实验，
复用本轮存储和准入证据，不再重做已闭合矩阵。
