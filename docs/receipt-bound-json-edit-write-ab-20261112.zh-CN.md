# 明确写入指令能否促成实际编辑

生产基线为 `ce956c9a`。上一轮的局部更新候选四次只返回修改后的 JSON，实际编辑次数为零。本轮 A、B 使用同一个局部更新助手 `tools.edit({read, changes})`。A 保留上一轮的任务文字。B 附加一段指令，要求首次 CodeMode 在同一程序中读取、调用 `tools.edit`、返回编辑结果，并给出该场景的两行例子。发生版本冲突后，仍由模型发起下一次读取和编辑。

两对实机使用同一个本地 Gemma4 12B 和 temperature 0，按 AB、BA 串行执行。两边都使用原有的真实冲突注入和磁盘验收，脚本不会代发编辑。通过条件包括首次编辑产生真实版本冲突、拒绝后文件逐字节保持外部修改、后续重读绑定新凭证、只修改两个目标值并保留所有其他字段。

脚本化回归在两个配置中都执行真实冲突并恢复，同时核对模型请求中两边均存在局部更新签名，只有 B 携带写入指令。

## 结果

| 配对 | A 原任务 | B 明确写入指令 |
| --- | --- | --- |
| 01，AB | `no_progress`，4 次模型请求，0 次编辑 | 通过，5 次模型请求，2 次编辑 |
| 02，BA | `no_progress`，5 次模型请求，0 次编辑 | 通过，3 次模型请求，2 次编辑 |

**真实编辑调用和完整验收都从 A 的 0/2 到 B 的 2/2。** 两个 B 单元都在第一个模型响应中提交了包含 `tools.edit` 的 CodeMode 程序。每个单元恰有一次真实旧版本冲突，拒绝后的磁盘字节与注入文件一致。随后模型显式重读，第二次编辑成功，`amount`、`code` 正确，原有 `metadata`、外部新增 `external_revision` 和备份文件都保留。

两个 A 单元各成功读取四次，修改只出现在返回文本里。第 2 对 A 另有一次程序编译失败。第 1 对 B 在写入已成功后两次提交了无效的可选 `execution_claims`，第 5 次请求才正式完成。A、B 总请求数为 9 和 8，这两对不足以判断调用成本是否稳定下降。

已核对全部 17 次实际模型请求的局部更新签名与指令分配。两对任务文字与预先设定一致，生产和实验脚本摘要在运行中一致。无服务失败或预算中止。这个结果支持在该编辑场景用明确的工具调用示例促成写入。示例含该场景的路径和目标值，泛化到其他文件、字段或模型仍需验证。

51 项相关回归通过，Ruff、strict mypy、Pyrefly、ty 通过。pytest 退出时有 RAM disk 清理告警。未改生产代码，未创建 worktree。

[实机汇总](../.e2e-artifacts/json-edit-write-ab-20261112/summary.json) · [第一对 B 的持久回执](../.e2e-artifacts/json-edit-write-ab-20261112/01/B/receipts.json) · [第二对 B 的持久回执](../.e2e-artifacts/json-edit-write-ab-20261112/02/B/receipts.json)

## 复现

```powershell
python -m pytest -q tests/test_receipt_bound_json_edit_probe.py tests/test_receipt_bound_edit_ab.py tests/test_receipt_bound_edit_recovery_probe.py
python -u -m tools.receipt_bound_json_edit_probe --write-instruction-ab --run-dir .e2e-artifacts/json-edit-write-ab-20261112
```
