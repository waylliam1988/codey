# Provider Control Revocation — offline report

- 日期：2026-10-21；负责人：Codex。
- 决策：EXPERIMENT_ONLY；0.6.1 / 0.6.2 仍 INCOMPLETE；无生产补丁。
- 目标：检验 session admit_hints 在首轮响应前关闭后，后续实际请求是否尊重新状态。
- 假设：entry 的一次控制快照不更新，旧 provider history 继续携带 hint。
- 探针提交：`2db5f328c20c588f549550c26e9975a7545b621a`；最终仪表与受测提交：
  `c39f0897c74b6d3c980c1ca75ed9894ea1c474a0`；其中 scoped 故障关闭生产修复为 `34e201dc`。

## 配置与对照

固定 synthetic-control-revocation 响应，临时 loopback HTTP；不调用 KoboldCpp。
suite `provider_control_revocation_20261021_final`；read_file→done 一个任务；
text/native × 一直开启 / 初始关闭 / 首轮后关闭，6 cells，各一次。
真实 AppContext + GhostInboxStore 的 session 控制；实际 task entry、workspace builder、kernel、
ApiProvider、HTTP 与本地文件读取。只有候选 directive 由固定 fixture 提供，不代表真实语言学习。

context 32768、reserve 8192、keep recent 12000、output 128、temperature 0.3、timeout 5s、max turns 2。
第一条 HTTP 收到后、返回 read_file 响应前设置 session admit_hints=false，并从实际 store 验证。
不修改 scorer 或门槛。无模型行为评分；无测得 token usage，不记为 0。

## 结果

| 模式 | text 初次 / 第二次有 hint | native 初次 / 第二次有 hint | 结果 |
|---|---|---|---|
| 一直开启 | 是 / 是 | 是 / 是 | 有效对照 |
| 初始关闭 | 否 / 否 | 否 / 否 | 有效对照 |
| 首轮后关闭 | 是 / 是 | 是 / 是 | 两项均重发已撤回 hint |

6/6 完整，每项 done、2 次 HTTP，总计 12 次合成 HTTP。全部 6 项第二次 payload 包含实际
read_file 返回的 synthetic fixture 内容（归档只读复查），不是只观察 builder 字符串。
两项转换 write_ok=true、effective_enabled=false。runtime 的 admit_hints read 每项恰好一次，
发生在 HTTP 请求数为 0 时，project/session 均正确。转换确认的实验读取不计入 runtime 读取。

初版 suite `provider_control_revocation_20261021_recorded` 六项结论相同；其读取列表混入实验自己
确认开关的读取。`c39f0897` 分离两类读取后重跑新 suite；保留旧归档，不累计作 12 cells 或模型样本。
实际 provider-send 已走通；生产默认 prompt 未修改；不改 Evidence、Permission、CompletionProof。

## 复现与限制

```powershell
python tests/manual/ghost_memory_routing/control_revocation_probe.py --suite provider_control_revocation_repro --metrics-out artifacts/ghost_memory_routing/control-revocation-repro-metrics.json
python -m ruff check tests/manual/ghost_memory_routing/control_revocation_probe.py
```

已有 suite 不覆盖；每次复现更换名称。exit 0 是完整性成功，不能写成安全 gate 成功。
指标见 [JSON](control-revocation-metrics.json)；本地 raw 请求/回复、manifest、events、state 只在
ignored `artifacts/ghost_memory_routing/provider_control_revocation_20261021_final/`。

这证明生产控制即时生效缺口，不证明模型语言阴性。没有模型成本、延迟或跨模型推广结论；
单任务、每项一次足以定位确定性结构缺口，不能用于语言晋级。不覆盖 repair/receipt/resume/compaction。

下一步与 history expiry 合并研究最终 payload 的单一准入位置。要移动现有 gate 而非叠加校验；
明确已发送历史的不可撤回边界、拒绝与恢复语义，再做新的 red/green 矩阵。
本实验无需生产回滚；保留探针/负结果即可，不将未验证 guard 合并生产。World Model 继续 SHADOW。
