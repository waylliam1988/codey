# Ghost 实验：Final Payload Admission Placement

- 实验名称：Final Payload Admission Placement
- 状态：INCOMPLETE
- 负责人：Codex
- 日期：2026-10-21
- 实验 commit：`7453bc5a`（探针）；`996605eaa16f58fba234a29b6b98d6c485d9d150`（修正 fixture count method，受测提交）
- 关联 Roadmap 验收门：0.6.1 / 0.6.2
- 生产合并结论：EXPERIMENT_ONLY

## 目标和假设

历史 expiry 和运行中撤回均已在真实 HTTP 复现。验证最终 gate 必须经过 configure_request、
compaction 和 request lease 等待，且覆盖 retry 与辅助模型。假设仅移动到 `_admit_request`
仍有 lease/retry 时隙；移动到 `_generate_attempt` 可封住时隙，但原 validator 的精确文本契约
未必能接受被 JSON 编码的辅助摘要输入。不得为了通过而添加 decoder fallback 或跳过辅助发送。

## 实验配置

- 模型 / suite / endpoint：synthetic-payload-placement /
  final_payload_placement_20261021_fixed / 临时 loopback HTTP，无 live 模型。
- tasks / cells / reps：一个 done 任务；两种真实 API codec × 10 个边界案例 × 3 种 gate 位置；60 项各一次。
- on/off 对照：初始开启 / 初始关闭；正常与 lease 时 expiry/revoke、503 retry expiry、wire rewrite、
  compaction 正常/lease expiry、中文多行含引号的正常/compaction。
- 三种位置：现有 kernel 基线；完整 payload 准备后；lease 获取后每次 `_generate_attempt`。
- 候选只在实验进程 monkeypatch 移动现有 validator；替换 kernel predicate，不叠加第二份判定。
- builder、kernel、ApiProvider、codec、真实 HTTP、compaction 辅助发送与 scoped store 均走真实代码；
  history、响应、时间、pressure counter、lease/retry policy 是显式 deterministic fixture。
- context 65536 / reserve 8192 / keep recent 12000 / output 128 / temperature 0 / timeout 5s / max turns 1。
- source 在 T00:00:01Z 到期，首次 lease 或 retry 前显式将时间从 T00:00:00Z 推进到 T00:00:02Z。
- 不测语言效果，不把合成 counter 写成真实 tokenizer，不修改 scorer 或样本门槛。
- 原始 payload/reply、state、policy、manifest 只存 ignored artifacts；只提交脱敏指标。

## 本次完成 / 验证命令和结果

60/60 完整，两种 codec 结论一致。每个位置各 20 项：

| gate 位置 | 实际 HTTP | 已失效 source HTTP | wire rewrite 发送 | 正常对照被拒 |
|---|---:|---:|---:|---:|
| 现有 kernel | 28 | 10 | 2 | 0 |
| 最终 payload 准备后 | 20 | 8 | 0 | 2 |
| lease 后每次 attempt | 12 | 0 | 0 | 2 |

prepare gate 仍允许 lease/retry 期间的失效，compaction expiry 场景甚至在拒绝主请求前已向
辅助模型发出过期 source。attempt gate 在受测场景阻止所有失效发送与 rewrite，但健康的
中文多行含引号 compaction 在两个 codec 均被 `not_in_provider_payload` 拒绝。
辅助 renderer 把原文放在 JSON source 字符串里，逐字符串匹配不能证明其编码来源；
不能靠试探性 unescape 或缺失时放行修复。

开启、初始关闭、中文直接发送、ASCII compaction 四组健康对照 × 两个 codec，三个位置的
实际 payload 摘要序列均完全一致。全部 lease acquire/release 数量平衡，无额外未归属请求。
这些摘要仅用于实验 wire 对照，不是生产 ContextEpoch/trace parity 的完成证据。

```powershell
python tests/manual/ghost_memory_routing/payload_admission_placement.py --suite final_payload_placement_repro --metrics-out artifacts/ghost_memory_routing/payload-placement-repro-metrics.json
python -m ruff check tests/manual/ghost_memory_routing/payload_admission_placement.py
```

保存报告时 suite 为 `final_payload_placement_20261021_fixed`，metrics-out 为
`tests/manual/ghost_memory_routing/reports/payload-placement-metrics.json`。新复现须更换 suite。
exit 0 仅表示矩阵完整，不代表候选通过生产晋级。
初次 suite `final_payload_placement_20261021` 第一个 cell 因实验 counter 使用生产枚举不接受的
method 字符串而失败，0 个完整 cell / 0 HTTP；保留 failure/命令/时间/stack，改脚本为合法
`estimated`，不修改生产类型或算作模型阴性。完整矩阵只计修正后的 suite。

[脱敏指标](../../tests/manual/ghost_memory_routing/reports/payload-placement-metrics.json)；
[离线报告](../../tests/manual/ghost_memory_routing/reports/payload-placement.md)。原始 payload、state、
policy、manifest 与日志只在 ignored `artifacts/ghost_memory_routing/`，无 KoboldCpp 调用。

```powershell
python -m pytest tests/test_ghost_provider_send_boundary.py tests/test_ghost_context_admission.py tests/test_api_request_context_counting.py tests/test_api_provider_request_policy_lifecycle.py tests/test_api_request_policy_lifecycle_and_budget.py tests/test_api_transport_retry_policy_contract.py tests/test_compaction_counted_tool_views.py tests/test_compaction_source_encoding_and_summary_contract.py tests/test_responses_compaction_preserves_complete_exchanges.py -q --tb=short
python -m mypy codey --python-version 3.11
pyrefly check codey --python-version 3.11
ty check codey --python-version 3.11
```

结果：89 passed（21.07s）；mypy 495 files clean；Pyrefly 0 errors、29 suppressed、74 warnings；
ty 和本轮脚本 Ruff 通过；没有全量 pytest。一次选错旧文件名的回归命令未收集任何测试，
随后从当前文件列表选择上述实际文件；不把未运行的命令算作通过。

## 当前生产边界

- 是否经过真实 provider-send：是，两种 codec 的 ApiProvider → loopback HTTP，包括辅助摘要及显式 503 重试。
- 是否改变默认 prompt：否，实验进程中的固定 fixture，不合并候选 guard。
- 支持模型 / 任务 / 配置：确定性合成边界，不作模型/语言效果推广。
- 生产合并结论：EXPERIMENT_ONLY。

## 未完成或阻塞 / 下一步

现阶段不合并候选 guard：attempt 位置是受测时隙的必要条件，原样搬移的表示契约仍不充分。
下一步对照显式 producer 声明的 JSON 表示，保留原始 source digest/scope/budget/expiry，
验证编码能否精确校验，以及 history-only source 被摘要改写后是否还需要 canonical lineage。
不新增 decoder fallback、不绕过辅助发送、不把范围扩大为模型收益。
本实验不证明跨 run/resume、远端 web history 可见性或完整 trace/wire parity。
World Model 继续 SHADOW。

## 继续实验：Producer Declaration / History-only Derivation

目标：保持最终 attempt 单一 predicate，由实际 auxiliary fork producer 显式登记 JSON source
表示，而非 gate 在匹配失败后猜编码。保留原文/rendered digest、scope、budget、expiry；
未登记的 client 不放行。假设这能修复 quote/newline 编码误拒，却仍不足以追踪语义摘要后的来源。
增加 history-only compaction：当前 builder 无 Ghost candidate，source 只在 fixture 旧 history 中；
候选持有 fixture 声明的历史 catalog，kernel 基线只持当前候选。这不冒充生产已有历史 catalog。

配置保持前一阶段；Completions/Responses × 11 场景 × kernel/attempt/attempt_declared_json，
66 项各一次；原 60 项作为独立归档，不合并统计样本。本阶段没有生产改动，状态 INCOMPLETE。
新参数是显式实验 arm，不改变原 60 项的默认复现配置。
