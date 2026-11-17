# Hebbian 损坏事件的原子重建

- 日期：2026-11-17
- 状态：`PRODUCTION_MERGED`（普通 Hebbian 显式重建入口；原始负结果保留）
- 关联：0.6.3 / 0.6.4；不重复已完成的 Affinity 时间校验。
- 起点：master `6762f1ea`；保留已有 tar.gz 删除与未跟踪 HTML。
- 实验提交：`ab68edb7248b4aa34ac221df81b7218a1baa1363`。
- 生产提交：`da850a5b36df9811cbc11690854bc7f6f2cc29d9`（本地 master，非远端发布声明）。

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

生产已限定修复 `hebbian.py`：首先验证严格 int schema、已知事件类型和记录时间，然后
拒绝无法解码的 node/edge，以及与事件类型不符的 superseded 状态。原事务异常回滚并
返回 False，CLI 返回 `ok=false` / exit 1；不再跳过坏事件后写入不完整投影。
私有重建函数明确返回具体节点/边列表，移除不可能返回 None 的死分支。

复用原 graph/SQLite owner，无中央日志、后台模型、权限或 Evidence 写口。普通生产入口
`python -m codey ghost rebuild-state --state-home <隔离目录> --yes` 直接消费修复，无新增
默认关闭开关；不修改偏好入选、内容或语言模型 allowlist。返回 False 后继续使用最后有效
投影；不会“修好”原日志。原日志、id 和 sequence 全部保留供排障。

追加验收预定为：三 scope × valid/坏 supersede × 有/无 --yes 的 12 项真实 CLI；三 scope
× delete/reset 的 6 项四开关关闭后显式删除及冷恢复；3 项写入后故障的原子回滚与后续
不读 clock 的正常重建；6 项事件类型/节点状态不一致拒绝。合计 27 项，连同原矩阵 81 项。

补充测试首轮 75 项中 6 项因误以为 CLI error JSON 写 stderr 而失败，按实际 stdout
契约修订，保留确认/exit/未变状态断言。再追加状态不一致测试时，clock 恢复断言误放进
拒绝测试，81 项中 6 项失败；Ruff 同时指出错位的 events 未定义，已移回故障恢复测试。
这两轮均为测试编写问题，不计作生产负结果，原日志/XML 保留。

## 最终验证

原 54 项由 12 通过/42 失败变为 54/54 通过。扩大 26 文件回归为 891 passed / 34 subtests，
零失败/错误/跳过，包含 81 项新验收。随后定向 mypy 指出故障回调组合断言的 tuple 泛型
上下文推断冲突，拆为两条等价断言，保留 node/edge 非空检查；最终该 81 项全部复验通过。
891 与 81 属不同测试源码阶段，不相加；两阶段 702 个生产文件 SHA256 均冻结且验证不变。

最终全树 Ruff、mypy、Pyrefly、ty、precision gate 和两个修改文件定向三类型检查通过。
Pyrefly 的 35 既有 suppressed / 158 warnings 不代表全仓无 Any；修改文件额外 Any
审计零 owned 诊断，exit 1 来自未修改依赖的 3897 项/182 文件诊断，未增加豁免或改范围。
结果及全部命令见[脱敏报告](../../tests/manual/ghost_memory_routing/reports/hebbian-replay-integrity-production-20261117.json)。
不运行全量 pytest，无前端变更、无新增模块或额外模型调用，不需要静态资源发布。

```powershell
python -m pytest tests/test_ghost_hebbian_replay_integrity.py -q --tb=short
python -m ruff check .
python -m mypy codey --python-version 3.11
pyrefly check codey --python-version 3.11
ty check codey --python-version 3.11
python -m tools.kernel_type_precision_gate
python -m mypy codey/ghost/hebbian.py tests/test_ghost_hebbian_replay_integrity.py --python-version 3.11
pyrefly check codey/ghost/hebbian.py tests/test_ghost_hebbian_replay_integrity.py --python-version 3.11
ty check codey/ghost/hebbian.py tests/test_ghost_hebbian_replay_integrity.py --python-version 3.11
python -m mypy codey/ghost/hebbian.py tests/test_ghost_hebbian_replay_integrity.py --python-version 3.11 --disallow-any-expr
```

回滚：可 revert 生产修复，但会重新暴露原 42 项缺口。故障隔离优先保留原日志和最后有效
投影，暂停显式 rebuild 或用现有控制禁止检索/准入。用户可显式 delete/reset；scope delete
仍因旧坏日志拒绝后续重建并保留空投影，reset 推进窗口后可重建，原始历史都不被重写。
不以自动跳过坏事件或物理删日志恢复“成功”。

## 剩余审查

本轮只证明上述 Hebbian envelope、结构解码与状态一致性边界，不是任意内容损坏检测，
也不自动发现或恢复此前已经被旧 replay 覆盖的有效投影。节点/边内部各时间字段、记录型
control payload 的更严格校验仍应独立冻结验证，不能把 envelope ts 通过写成完整时间验收。
图的同分稳定排序、snapshot/digest 与其他领域 replay、完整 App/硬退出/跨 run 的发送
恢复、升级/混版本和其余生命周期仍未整体闭合。World Model 仍无真实闭环；依赖满足后
继续独立消费者实验，不用本存储修复冒充模型收益或 World Model 验收。
