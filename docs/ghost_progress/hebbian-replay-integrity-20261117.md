# Hebbian 损坏事件的原子重建

- 日期：2026-11-17
- 状态：`EXPERIMENT_COMPLETE`（原始负结果；生产修复另提交）
- 关联：0.6.3 / 0.6.4；不重复已完成的 Affinity 时间校验。
- 起点：master `6762f1ea`；保留已有 tar.gz 删除与未跟踪 HTML。

## 冻结目标与配置

假设：Hebbian replay 静默跳过损坏的 upsert/supersede 或未知事件，可能复活旧 active
偏好或丢失状态，却返回成功。主要变量仅为已提交日志中的一个事件内容；保留真实 SQLite
中最后有效投影。故障注入不是正常 producer 行为，不声称能够自动修复损坏日志。

不调用模型；模型/endpoint/采样参数不适用。这是存储可靠性验收，不产生新的模型收益结论，
不重跑停止的语言实机批次。任务为两次真实人工确认同槽偏好、一次有效 coactivation edge，
然后通过普通 `rebuild_from_events` 及 directive 消费者检验状态。

最低样本量：user/project/session × warm/cold × 9 variants = 54 cell，每 cell 两次重建。
variant 固定为 valid、superseded_node、upsert_node、edge、unknown_type、schema_version、
boolean_schema、timestamp、deletion_payload。valid 是完整正常日志正对照；坏事件处理的
对照为事务前有效投影、原始日志及正常 directive。各 cell 必须先证明真实 supersession
和非空 edge，再注入故障；不能将无效 fixture 算作生产失败。

指标：有效 cell 数；正常重建成功数；坏日志拒绝数；projection/原始日志是否保持不变；
旧 active 是否复活；directive 是否改变。通过要求正常日志成功、坏日志拒绝且保留上一次
有效投影/日志，重复及冷重开结果一致。停止条件为 fixture 无效、破坏性操作触及非隔离
state 或结果显示假设不成立；不通过删断言或放宽 scorer 获得通过。

## 原始结果

54 cell 均先通过 fixture 有效性检查，12 通过、42 生产失败。6 个 valid 和 6 个
deletion_payload 对照正确；另外七种故障均错误返回成功。superseded_node 和 unknown_type
共 12 cell 复活旧 concise active、改变 directive；upsert_node 丢失当前 active，edge
丢失图边。schema/布尔 schema/坏 ts 虽未改变本 fixture 的状态，仍错误接受无法验证的日志。
每 cell 两次重建结论相同，原日志保持不变；暖冷结果一致。

相关 4 文件 140 项及 34 子测试通过；全树 Ruff、mypy、Pyrefly、ty 和 precision gate 通过。
定向 Pyrefly 首次发现故障夹具的空 dict 无元素类型，已具体化；额外 Any 审计发现打印
measurement 字面量受 json.dumps 参数上下文影响推断为 dict[Any, Any]，改为显式
dict[str, object]。这些是测试类型问题，未改变故障变量或 scorer。修订后原 54 cell 仍为
12/42，三个定向类型检查通过，额外 Any 审计零本文件诊断；该审计 exit 1 来自未修改依赖
3897 项/182 文件诊断，未增加豁免或改变检查范围。

原始日志/XML 在 `.e2e-artifacts/ghost-hebbian-replay-integrity-20261117`，仓库仅存
测试和[脱敏矩阵](../../tests/manual/ghost_memory_routing/reports/hebbian-replay-integrity-baseline-20261117.json)。

```powershell
python -m pytest tests/test_ghost_hebbian_replay_integrity.py -q -s --tb=short
```

## 生产边界与后续

若有可复现缺口，限定修复 Hebbian replay 的验证与事务失败路径，复用原 owner，不增加
中央日志、后台模型、权限或 Evidence 写口。正常生产重建直接消费修复，无新增默认关闭开关。
保留显式 reset/delete 和原日志供排障；未知未来 schema/type 必须拒绝而不是猜测语义。

完成后补记实际结果、回归、三个完整生产树类型检查、Ruff、precision gate、修改文件
定向类型/Any 审计及独立实验/生产 commit。完整 G0 与 World Model 仍未完成。
