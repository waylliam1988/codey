# 同一批真实回执的字段交付 A/B

生产基线为 `3c7a533c`。实验从真实 Chromium 操作采集已结算回执，答案交付在实验脚本内运行。每对只采集一次。`browser_tab_list` 提供当前 URL，`browser_eval` 分别读取页面 token 和结构化业务记录，`browser_read` 读取可见正文。回执必须来自同一个成功的 CodeMode 父调用。采集脚本会检查 URL 与当前测试页一致、业务记录和可见正文一致。

两组的实际模型请求都携带相同字节的回执 JSON，其中包含原始回执编号。两组使用相同的模型、温度、种子、请求时钟、说明要求、最终 JSON Schema、字段验收和最多三次答复机会。A 由模型提交解释及五个字段。B 的模型只提交解释，由实验程序从共享回执填写 `page_url`、`token`、`amount`、`unit` 和 `code`。两个发布流程都没有浏览器工具，采集用的脚本回复不计入模型调用。每对交替使用 AB 和 BA 顺序。

字段按值和类型精确比较。分别记录第一次答复生成的候选和最终发布结果，错值与缺字段分别计数。最终完成要求解释为包含「福州」的字符串，且五个字段和最终格式均正确。解释的完整语义和写作质量超出本实验的评分范围。模型调用按实际发出的生成交换计数，另保留 HTTP 请求次数和服务端 token 统计。

## 注入回归

脚本化模型沿用真实 Chromium 回执。A 首次把 URL 连字符写成下划线，第二次漏 token，第三次完成。B 仅给解释，一次组装出相同的正确结果。原始回执截断、布尔值冒充整数、重复 JSON 键、B 额外提交伪造字段也有定向覆盖。这个回归展示了机制生效时的路径，调用差异由注入故障触发。

## 实机结果

正式批次使用本地 KoboldCpp Gemma4 12B，完成 12 次独立浏览器采集和 12 对答复。逐对核对了持久化回执和实际请求。除填写分工说明外，每对首个请求完全相同。24 次模型调用全部正常返回。

| 指标 | A 模型抄写 | B 程序填字段 |
| --- | ---: | ---: |
| 首次答复错值 | 0/60 | 0/60 |
| 首次答复漏字段 | 0/60 | 0/60 |
| 最终错值或漏字段 | 0/60 | 0/60 |
| 最终完成 | 12/12 | 12/12 |
| 模型调用 | 12 | 12 |
| 输入 token | 14,753 | 14,753 |
| 输出 token | 1,740 | 310 |

B 在 12 对里每次都少生成 token，合计减少 **82.2%**。本批 A 已全部一次答对，因此实机没有观察到字段准确率、完成率或调用次数的提升。注入回归确认，字段已在可靠回执中时，程序组装可以避开具体的抄错和漏填。浏览器自主采集、父程序失败后的结果交付、开放文本的业务值抽取属于另外的验证范围。

31 项回归通过。实验脚本与测试通过 Ruff、strict mypy、Pyrefly 和 ty。生产源码摘要前后一致。

## 复现

```powershell
python -m pytest -q tests/test_receipt_field_assembly_ab.py tests/test_completion_explanation_ab.py tests/test_browser_production_execution.py
python -u -m tools.receipt_field_assembly_ab --run-dir .e2e-artifacts/receipt-field-assembly-fixed-clock-20261112 --pairs 12 --max-calls 3
```

正式批次存放在 `.e2e-artifacts/receipt-field-assembly-fixed-clock-20261112/`。每对有一份 `acquisition/shared-receipts.json`、原始持久化回执和两个 `provider.jsonl`。汇总由实验脚本写入 `summary.json`。未固定请求时钟的前两批保留在 `.e2e-artifacts/receipt-field-assembly-ab-20261112/` 和 `.e2e-artifacts/receipt-field-assembly-verified-20261112/`。它们复用了相同任务和种子，统计以正式批次为准。
