# Producer Declaration / History-only Derivation — offline report

- 实验轨道：Final Payload Admission Placement；负责人 Codex；日期 2026-10-21。
- 受测提交：`3b04b8a67733a15b1ae6021c619ce2ef05970015`。
- 状态：EXPERIMENT_ONLY / INCOMPLETE；没有生产改动或生产合并。
- 目标：验证显式 producer 表示能否解决转义误拒，以及是否足以覆盖语义摘要后的历史来源。

## 配置与方法

synthetic-payload-placement，临时 loopback HTTP，suite `final_payload_declared_20261021`。
Completions/Responses × 11 场景 × kernel/attempt/attempt_declared_json，共 66 项各一次。
前一阶段 60 项独立归档，不混合计数。原十场景不变，新增 history-only compaction：当前
builder 不含 hint，旧 history 有已声明的 fixture source。kernel 仅获得当前候选；两个
provider arm 获得实验声明的历史 catalog，不冒充生产已持久化 source metadata。

JSON arm 在 auxiliary fork producer 为新 client 显式登记 json_string_fragment 形式及
origin/rendered digest，gate 只消费声明，不在失败后尝试解码，不改变 scope/budget/expiry。
保持单一 workspace validator，实验进程替换 kernel 判定，不叠加 validator。
真实 builder/kernel/codec/compaction/HTTP/scoped store；history、时间、响应、counter/policy 为 fixture。
context 65536、reserve 8192、keep recent 12000、output 128、temperature 0、timeout 5s、max turns 1。
counter 固定 60000/40（estimated），不代表 tokenizer、真实容量、成本或延迟。

## 结果

| arm（各 22 项） | 实际 HTTP | 失效 source HTTP | rewrite HTTP | 健康对照被拒 |
|---|---:|---:|---:|---:|
| kernel | 32 | 10 | 2 | 0 |
| attempt 原文 | 14 | 0 | 0 | 4 |
| attempt + producer JSON 声明 | 18 | 0 | 0 | 2 |

66/66 完整、共 64 次合成 HTTP，全部 acquire/release 平衡。
两个 codec 的中文多行含引号 compaction 在 JSON arm 正常完成，原文 arm 误拒。
初始开启、初始 off、ASCII compaction、中文直接发送、中文 compaction 五组 × 两种 codec，
kernel 与声明 arm 的实际 payload 摘要序列一致（10 组）。没有语义评分或模型样本量推广。

history-only 场景在两个 provider arm 均成功向辅助模型发送一次，然后主请求因
`ghost_directive:not_in_provider_payload` 被拒：摘要不是原文，只有字面匹配不能验证来源。
不能通过丢失 source catalog、给派生项换 key 加预算、猜解码或缺 metadata 时放行来让案例变绿。
这证明编码声明解决局部表示问题，未完成 canonical source lineage 与派生预算。

## 复现、边界与下一步

```powershell
python tests/manual/ghost_memory_routing/payload_admission_placement.py --suite final_payload_declared_repro --placement kernel attempt attempt_declared_json --include-history-only --metrics-out artifacts/ghost_memory_routing/payload-declared-repro-metrics.json
python -m pytest tests/test_context_compaction_transactions_and_lifecycle.py tests/test_zen_compaction_auxiliary_request_boundary.py tests/test_ghost_round4_resume.py -q --tb=short
```

回归 23 passed（1.38s）。本阶段没有生产源码改动；前阶段 mypy/Pyrefly/ty 结果不重复计数。
Ruff 通过。已有 suite 不覆盖，exit 0 代表矩阵完整，不代表 gate 生产晋级。
指标见 [JSON](payload-declared-metrics.json)；raw/state/policy/manifest/log 只在 ignored
`artifacts/ghost_memory_routing/final_payload_declared_20261021/`；没有 KoboldCpp 调用。

经过真实 provider-send；未改变生产默认 prompt；不扩大 Evidence、Permission 或 CompletionProof。
无生产回滚需求。下一步复用 canonical journal/checkpoint 来源关系，并验证 provider 内部拒绝的
durable NOT_SENT 分类、lease 释放与 native 恢复；尚不覆盖真实 entry 全矩阵、跨 run、未知 web
history 或生产 trace/wire parity。World Model 继续 SHADOW。
