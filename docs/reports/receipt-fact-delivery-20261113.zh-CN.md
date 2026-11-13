# 回执事实交付：固定回复与实机 A/B（2026-11-13）

结论：固定回复能证明程序从选定回执补齐事实，但本轮实机没有收益。A 自由说明为 **8/8**，B 位置绑定为 **0/8**。保留负结果，不接入生产。脚本、测试和记录在 `master` 提交，`codey/` 未修改，也未合并或推送。

实验入口是 [receipt_fact_delivery_ab.py](../../tools/receipt_fact_delivery_ab.py)，取值逻辑是 [receipt_fact_delivery.py](../../tools/receipt_fact_delivery.py)，固定回复在 [test_receipt_fact_delivery.py](../../tests/test_receipt_fact_delivery.py)。[机器结果](receipt-fact-delivery-20261113.json)保留逐项输出及新旧评分；[原始记录包](receipt-fact-delivery-20261113.zip)包含完整请求、模型回复、需求声明、交付、真实工具回执、实机时脚本快照和检查日志，包内 `manifest.json` 记录每个文件的 SHA-256。

模型先只看自然问题，声明需要交付的事实，每项含 key、label、subject。两组复用同一份声明和真实浏览器回执。A 在说明中写值；B 选择完整记录集、实体列、值列，程序按 subject 唯一匹配并读取值。B 不传字面值，不能缩小成某一行或重新定义 subject。预期答案仅进入评分函数，不传给模型或取值逻辑。

浏览器读取通过现有 headless → CodeMode → Chromium 路径执行并落盘。固定 DOM 投影读取可见表格和 `pre[data-json]`，不是模型自由编写的提取代码。取值要求成功的父回执、完整读取、固定投影及相同文档 loader；后续导航使旧文档失效。读回原文也必须含该值。唯一匹配失败时保留已核验事实并列出缺项。字符串保留前导零，JSON 的数字零保持整数。

| 场景 | 固定回复验收 | 实机 A | 实机 B |
| --- | --- | --- | --- |
| 鱼丸表格 | 说明漏值，仍补齐 `173 克`、`QUARTZ_851` | 2/2 | 0/2 |
| 订单表格 | 补齐 A-173 的日期、`0042091` | 2/2 | 0/2 |
| 库存 JSON | 补齐西仓整数 `0` | 2/2 | 0/2 |
| 订单日期归属不明 | 只交付追踪号，明确缺送达时间 | 2/2 | 0/2 |

新增固定回复共 25 项，与已有 task-contract 固定回复合跑为 36 项全部通过。另覆盖读取缺失或截断、失败回执、模型伪造的 eval、同 URL 重载、选择单行、重复实体、来源值变化、复制字面值、覆盖 subject、重复绑定和评分边界。固定回复中的 needs 和位置是预先给定的，因此不能证明模型会自己正确声明或选择。

实机使用 `http://127.0.0.1:5001/v1` 的 `koboldcpp/Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M`，temperature 0、thinking 关闭、JSON schema，两个 seed 为 `123456789`、`123456790`。每场景两对，第二对反转 A/B 顺序。共四次真实浏览器采集；模型阶段 32 次真实请求：需求声明 8 次、A 8 次、B 16 次。B 每项最多两次回复，反馈只含运行时缺项，不含验收答案。A 都在首次回复结束，B 都用满两次。回复阶段累计耗时 A 13.280 秒、B 66.689 秒，需求声明另用 24.485 秒；这是本地串行调用耗时，不是端到端性能结论。生成 token 分别为 A 332、B 2025、声明 716。

失败发生在需求声明：模型把 subject 写成“福州鱼丸的净重”“订单 A-173 的送达时间”等完整事实名，而不是记录中的“福州鱼丸”“A-173”。库存还额外声明了仓库名称。八份声明均不满足本实验的实体匹配要求；冻结声明后，B 选择正确列也无法匹配目标实体。库存 B 只交付了多声明的“西仓”，未交付库存数。A 则忽略了这个绑定障碍，直接正确写出所问值；歧义场景也指出缺少送达时间。两组都没有输出预设的错归属日期，但 B 同时丢失了本可交付的追踪号，不能把拒绝当作收益。

原评分有两处错误，已在保留原记录的前提下重评：一是把需求声明是否可绑定混进答案通过条件，导致正确的 A 也被判失败；二是用成文内容匹配零，把 B 诊断中的 `found 0` 当成库存值出现。现在单列 `declaration_usable`；A 检查答案中的完整值，B 只检查程序交付的带类型 facts，不扫描诊断文本。新增回归覆盖这两处及 `0` / `10`、整数零 / 字符串零 / 布尔值的区别。记录包保留原 `summary.json`、原 `score.json`，更正结果写入 `rescored-summary.json`，未重新请求模型或选择性删除失败。

另外从四份持久状态导出工具回执，重新构建证据，再执行原 B 回复的全部 16 次位置选择，输出与原始记录逐项一致。取值核心与实机时快照字节相同。实机后的脚本修改仅涉及评分、记录导出、重评分入口，以及不改变 schema 内容的类型标注整理。

复现固定回复和新的实机采样（`--run-dir` 必须是新目录）：

```powershell
python -m pytest tests/test_receipt_fact_delivery.py -q
python -u -m tools.receipt_fact_delivery_ab --run-dir .e2e-artifacts/receipt-fact-delivery-new --pairs 2
```

只复核已有记录，不请求模型：

```powershell
Expand-Archive docs/reports/receipt-fact-delivery-20261113.zip .e2e-artifacts/receipt-fact-delivery-review
python -m tools.receipt_fact_delivery_ab --run-dir .e2e-artifacts/receipt-fact-delivery-review/live --rescore
```

最终验证记录见包内 `checks/`。执行全树 Ruff、生产树 mypy，以及包含新增脚本和测试的 pyrefly、ty；新增三文件另跑 mypy `--disallow-any-expr`，未新增 `Any`、类型忽略或放宽配置。pyrefly 成功仍报告原有 suppression / warning 计数，不表示全仓无动态类型。生产源码采样前后 SHA-256 相同：`d326c7e78cdf46062992381eca5ca8ed21ba4a295b24bbe77f65a5c879846038`。

本轮仅验证共享、固定采集回执后的实验性交付。没有验证模型自主发现页面、动态表单、任意非结构化文本或生产 `done` 完成约束。运行时校验唯一记录和位置，不理解所选列是否符合用户语义；列语义及需求理解仍由模型负责。每场景仅两对，两个 seed 的输出也高度重复，不能作可靠率推断。下一步若继续，应先验证自然问题到实体声明的接口是否可用，再独立采样；当前证据不支持合入生产。
