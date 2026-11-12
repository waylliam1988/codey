# 绑定读取凭证的 JSON 局部更新实验

生产基线为 `346f7795`。在同一个本地 Gemma4 12B、同一任务文字、真实磁盘及生产文件执行器上，比较旧的读取对象助手和局部更新候选。A 接收 `edit({read, content})`，B 接收 `edit({read, changes: {amount, code}})`。两边都从同一次 CodeMode 的原始读取对象取得路径和版本凭证。这一轮直接提供两个目标值，单独检验文件写入链路。

B 仅接受对 `result.json` 的这两个既有顶层字段的更新。实验宿主验证并解析原始完整读取，只替换两个值所在的文本范围，然后使用同一读取的凭证调用生产 `edit`。不改其他字段的文本，也不重新序列化未修改的数字。缺字段、非法 JSON、重复键或错误类型会被拒绝。生产版本检查照常生效，冲突后由模型显式重读再提交。

## 确定性验证

脚本化模型在同一个任务中读取后触发外部修改，再发起两次真实写入尝试。验证第一次写入确实因版本变化失败、失败后的磁盘文件与注入内容逐字节相同、第二次使用的是冲突后的新读取凭证。

| 注入用例 | 第一次真实写入 | 重读后结果 |
| --- | --- | --- |
| A 正确解析原始文件正文 | 版本冲突，磁盘不变 | 正确修改两个值，保留所有其他字段 |
| A 把读取返回对象当文件 JSON | 版本冲突，磁盘不变 | 版本校验放行，磁盘结构验收失败，复现原缺陷 |
| B 只提交两个字段 | 版本冲突，磁盘不变 | 正确修改两个值，原有嵌套元数据和外部新增字段均保留，备份文件未动 |

另验证了精确数字与未修改文本的保留，包括超出 JavaScript 安全整数范围的值和高精度小数。

## 自主实机 A/B

| 对照 | 完成 | 模型请求 | 真实写入尝试 | 真实版本冲突 |
| --- | --- | ---: | ---: | ---: |
| A 旧全文助手 | 通过 | 3 | 2 | 1 |
| B 局部更新 | `no_progress` | 4 | 0 | 0 |

A 自主经历了真实冲突，拒绝后磁盘逐字节不变，重读后成功写入并保留外部字段。B 的四次模型请求均含局部更新签名。四个模型程序都调用了 `read_file`，都只返回修改后的 JSON 字符串，均未调用 `tools.edit`。B 的磁盘保持外部注入状态，局部更新机制在自主样本里没有触发。

局部更新在触发时消除了由模型生成完整文件的风险。此配对没有观察到自主完成率或调用数收益，接下来的阻断点是让模型实际发起持久化写入。两臂任务文字相同，生产源码和执行脚本的摘要在批次内一致，没有服务失败或预算中止。实机后仅补充类型标注，批次目录保存了摘要匹配的冻结脚本。

相关三份测试文件共 49 项通过，Ruff、strict mypy、Pyrefly 和 ty 通过。额外加入 `test_versioned_content_edit.py` 的宽回归为 63 项通过、2 项失败，两项都在断言旧的 `base_result_ref` 错误文案。该生产路径和测试文件在本轮保持原样。pytest 退出时有 RAM disk 清理告警。未改生产代码，未创建 worktree。

## 复现

```powershell
python -m pytest -q tests/test_receipt_bound_json_edit_probe.py tests/test_receipt_bound_edit_ab.py tests/test_receipt_bound_edit_recovery_probe.py
python -u -m tools.receipt_bound_json_edit_probe --run-dir .e2e-artifacts/json-edit-stale-20261112
```

[实机总表](../.e2e-artifacts/json-edit-stale-20261112/summary.json) · [A 的持久回执](../.e2e-artifacts/json-edit-stale-20261112/A/receipts.json) · [B 的模型程序](../.e2e-artifacts/json-edit-stale-20261112/B/programs.jsonl)
