# 编辑回执绑定与 stale-edit 恢复测试

本轮只改实验脚本、测试及本文档，未改 `codey/` 生产文件、生产 prompt 或完成门禁，也未创建 worktree。复用已有 `receipt_bound_edit_ab.py` 的原始读取对象原型，不另建写入实现。

A 使用现有 `tools.edit({path, content, base_result_ref})`；B 使用实验接口 `tools.edit({read, content})`，由测试包装层从原始 `read_file` 返回对象提取路径和回执，最终仍调用生产编辑执行器。包装层不重新读取、不自动重试，也不替旧正文换成新凭证。

## 默认：离线检查

在仓库根目录运行，输出目录须不存在：

```powershell
python -m tools.receipt_bound_edit_recovery_probe --run-dir .e2e-artifacts/edit-recovery-offline-new
```

默认不发现或调用模型，不启动浏览器。固定 JavaScript 程序经真实 kernel、Node guest、read_file、edit 与版本保护执行以下三类场景，两组共六个单元：

| 场景 | 验收条件 |
| --- | --- |
| clean | 一次读取、一次编辑，目标正确，备份不变 |
| invalid-read | A 从解包正文错误取凭证；B 错传解包对象；均拒绝写入 |
| stale-recovery | 首次读取后注入外部修改；原有效凭证被版本保护拒绝；下一轮重新读取、重新计算并成功写入，保留外部新增字段 |

stale-recovery 两轮使用同一 session 与 revision store，但各自执行独立 guest 程序。B 第二轮必须取得新的原始读取对象；测试不把对象跨 guest 序列化。

`receipts.json` 保存原始工具参数、正文、canonical 与 audit，并记录首次工具拒绝及首次编辑拒绝。只有凭证指向此前成功、完整、同路径的 read_file，且生产错误明确为文件版本变化，才计入 `true_stale_conflicts`。漏传、伪造、部分读取或其他工具失败不冒充版本冲突。`fresh_read_recovery` 要求成功写入使用冲突发生后取得的新读取凭证。

`stages.json` 保存各轮文件内容，检查拒绝后外部修改仍在；最终内容还须保留 `external_revision` 字段。根目录 summary 记录生产文件内容摘要，摘要变化会使整个验收失败。

2026-11-11 首次离线运行位于 `.e2e-artifacts/receipt-bound-edit-recovery-offline-20261111/`；最终脚本验证位于 `.e2e-artifacts/receipt-bound-edit-recovery-offline-20261111-validated/`。两次均为六个单元通过，生产文件内容摘要前后一致。固定程序成功仅证明接口机制与版本保护可组合，不能证明模型会自主恢复或证明原失败的历史根因。

## 可选：模型自主恢复 A/B

```powershell
python -m tools.receipt_bound_edit_recovery_probe --live --run-dir .e2e-artifacts/edit-recovery-live-new --repeats 3 --timeout 240 --max-turns 8
```

默认 endpoint 为 `http://127.0.0.1:5001/v1`，可通过 `--kobold-base-url` 指定。沿用现有实验的模型选择器，只接受该 endpoint 暴露的唯一 Gemma4，不自动换模型；生产入口仍会校验本机已配置的 Local connection。

每轮包含普通字段、嵌套数组、Unicode 三对任务，串行执行，AB/BA 交替。每个单元由独立子进程运行，超时只终止脚本拥有的进程树。两组均在实验进程内关闭辅助 provider tab 探测，将可选的模型代码审查标为不可用，不返回伪造的审查通过结果；该审查不在本轮编辑验收范围内。生产读取、写入、版本保护、工具成功要求及 done 验证仍执行。A/B 的用户任务文字完全相同，写入各自 `task.txt`；不附加旧脚本中的分组教学示例。B 仅替换实验工具接口声明及 guest 包装层，所需接口差异保留在真实模型请求中。

第一次完整读取目标文件的生产回执提交后，脚本注入外部字段。模型必须自主处理拒绝；脚本不替模型重试。通过需要同时满足：真实版本冲突、冲突后的新读取及成功编辑、正确目标内容、保留外部字段与备份、仅修改目标文件、正常 done、生产源码摘要稳定。

每个单元保留 `provider.jsonl` 原始请求和响应、`programs.jsonl` 原始及执行程序、`events.jsonl`、`injection.json`、`receipts.json` 和生产 durable state。失败单元保留，不用重跑成功结果替换。进度写入 `progress.json`，最终结果写入 `summary.json`。

本轮未执行真实模型推理。配套测试使用脚本化 HTTP 回答走真实 provider/headless/durable 链，验证注入时序、相同任务文字及评分；这些回答同样不算模型自主恢复证据。upload、source-failure、浏览器恢复及完整 full-surface 尚未覆盖。

上述限制指本独立离线／编辑脚本的首轮验证。用户随后重新授权的完整表面实机已结束，见[完整表面实验报告](receipt-bound-edit-surface-probe.zh-CN.md)：28 个单元通过 23 个，覆盖 Chat、Hybrid、strict Research、冻结契约快照恢复、重复读取、stale-ref、stale-edit、上传、下载和来源失败。没有将这些结果冒充本脚本三类静态 JSON 任务的独立模型重复试验。

## 配套验证

新旧脚本相关测试共 37 项通过，Ruff 与严格 mypy 通过。pytest 退出码为 0；进程退出时另有 RAM disk 长路径清理告警，不涉及断言失败，未为此修改生产代码或测试基础设施。

```powershell
python -m pytest tests/test_receipt_bound_edit_recovery_probe.py tests/test_receipt_bound_edit_ab.py -q
python -m ruff check tools/receipt_bound_edit_ab.py tools/receipt_bound_edit_recovery_probe.py tests/test_receipt_bound_edit_recovery_probe.py
python -m mypy --explicit-package-bases tools/receipt_bound_edit_ab.py tools/receipt_bound_edit_recovery_probe.py tests/test_receipt_bound_edit_recovery_probe.py
```
