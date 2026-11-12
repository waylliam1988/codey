# 固定网页采集与结构化回执编辑 A/B

生产基线为 `577e82f6`。实验复用上一轮的正文、表格和 JSON 回执绑定夹具，本轮自主实机聚焦两项已观察到的失败。两臂使用同一个本地 Gemma4 12B、真实 Chromium、同一表格或 JSON 夹具、原文件凭证和生产版本检查。表格按 AB，JSON 按 BA 串行运行。

| 配置 | 模型负责的动作 |
| --- | --- |
| A | 按上轮实验接口生成 CodeMode，调用固定采集器，保存网页回执，再以字段映射提交编辑。 |
| B | 实验接线按已提交的回执依次暴露 `observe`、`edit`、`done`。模型用结构化参数选择来源和目标。接线把前两种提案降为固定的 CodeMode 程序，运行时传递网页回执。 |

B 只在有已提交的采集回执后开放编辑，并根据回执类型提供表格或 JSON 的 `edit.source` 参数。`source.id` 只能取该回执中真实、唯一的页面 ID。文件目标用 `edit.target.file` 与 `edit.target.field` 表示。夹具声明了本任务唯一授权的文件字段，字段名进入 schema，验收期望值不用于预填编辑参数。工具拒绝越出夹具 URL、文件或目标字段的提案。模型没有提供任意 JS 的入口。原回执的身份、当前文档、父调用成功状态和文件版本检查沿用上轮流程。B 使用符合接口的任务说明，A 保留上轮接口说明。

共同验收要求真实导航、一次真实文件版本冲突、首次拒绝后的文件字节保持、冲突后重读、精确类型及值、保留外部字段和备份文件，并由生产完成门接受 `done`。B 另要求实际使用至少两次结构化编辑。定向用例向 B 提交错误文件字段或回执形状的页面 ID，验证在文件操作之前拒绝，再以正确参数继续完成。

## 探索批次

第一版在同一请求中同时提供三个动作，目标使用 `target.pointer`。表格 A/B 各用 3 次请求，JSON A/B 各用 5 次请求，均未通过。表格 A 的三次程序编译失败，B 三次跳过观察直接编辑，被缺少网页回执拦截。JSON B 虽取得真实网页数据，随后选错了表格来源形状，并反复把网页路径填入文件目标。 [第一批](../.e2e-artifacts/browser-structured-edit-20261112/summary.json)

第二版按回执开放动作和来源参数，并单列目标字段。A 仍为 0/2，B 为 1/2。JSON B 用 4 次请求完成。表格 B 成功观察，后续三次把工具回执编号填为页面表格 ID，写入被拒绝。由此在正式对照中把回执中的可用 ID 加入模型实际收到的 schema。 [第二批](../.e2e-artifacts/browser-structured-edit-staged-20261112/summary.json)

## 正式实机结果

| 场景 | A 模型写 CodeMode | B 固定采集和结构化编辑 |
| --- | --- | --- |
| 表格 | `no_progress`，3 次请求。三次 JS 编译失败，无浏览器采集。 | **通过**，4 次请求。用已观察到的 `orders` 表，定位 `A-102` 的 `Due`，写入整数 `4875`。 |
| JSON | `no_progress`，5 次请求。取得正确网页值，四次都提交了不存在的文件字段 `shipping.trackingNumber`。 | **通过**，4 次请求。来源是 `/shipping/trackingNumber`，目标是 `tracking`，写入字符串 `"0042091"`。 |

**A 0/2，B 2/2。** 两臂各 8 次模型请求。两个 B 的实际请求依次只允许 `observe`、`edit`、`edit`、`done`，模型未提交 JS 或回执编号。第一次编辑各遇到一次真实版本冲突，冲突后的文件字节保持不变。第二次编辑各使用新文件读取凭证，继续绑定同一网页回执，保留外部字段、原有元数据和备份文件。16 次实际 HTTP 请求的模型、seed 123456789、temperature 0 和非流式设置一致，无服务失败。生产摘要和两份脚本摘要保持一致。

这次观察到的收益来自固定采集、按回执开放动作和参数约束的组合。每种页面只有一对，目标文件和可写字段由夹具声明。结果覆盖有唯一标识的表格和嵌入 JSON，以及顶层 JSON 字段更新。模型仍负责选择业务上的行、列或 JSON Pointer。 [正式矩阵](../.e2e-artifacts/browser-structured-edit-observed-20261112/summary.json) · [表格实际提案](../.e2e-artifacts/browser-structured-edit-observed-20261112/table-B/structured-actions.jsonl) · [JSON 实际提案](../.e2e-artifacts/browser-structured-edit-observed-20261112/json-B/structured-actions.jsonl)

29 项 pytest 通过，退出时有 RAM disk 清理告警。Ruff、限定范围 strict mypy 和 `git diff --check` 通过。

## 复现

```powershell
python -m pytest -q tests/test_browser_structured_edit_ab.py tests/test_browser_field_binding_probe.py tests/test_browser_receipt_edit_ab.py tests/test_codemode_json_edit.py
python -u -m tools.browser_structured_edit_ab --run-dir .e2e-artifacts/browser-structured-edit-observed-20261112
```
