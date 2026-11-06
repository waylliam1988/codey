# Research A/B 实验检查点（2026-11-06）

本文件是当前事实记录。顺序为 **2A → 2B → ③ → 4A/4B**。实验候选没有达到合同门槛前，不进入生产。

## 已完成

### ① 缺口驱动研究

旧版候选结束，不采用。没有继续调词或追加新题。

### 2A：连接器早停

- 修复后开发集：4 题 × 2 臂，共 8/8 完成；实际发送的 3 个 JSON tool schema 均在 KoboldCPP 1.117.1 编译成功。
- 第一份正式冻结因生产读取路径的 passage descriptor 上限 bug 停止；修复提交为 `4f26c81d`。
- 第二份修复后正式冻结在首个 baseline writer `no_progress` 失败处停止，计划 48、实际 1，判 `inconclusive`。
- 目录：`.e2e-artifacts/research-live-web-2a-formal-compiled-v2/`。

### 2B：公开全文恢复

- 正式合同是 **8 个新题 × 2 seed × 2 臂 = 32 次运行**，不是 12 题正式集。
- 目录：`.e2e-artifacts/research-live-web-2b-formal-v1/`。
- 32/32 真实联网、真实 writer、真实提交完成。
- 候选 16 次中 14 次恢复公开全文、2 次明确无公开链接；基线没有全文恢复尝试。
- 全量配对评分已完成并保存到 `semantic-ratings.json`：两臂严格成功均为 0/16；多题出现检索对象与任务不一致（Attention/QWYC、SPRINT/PRST、STEP 1/STEP-HFpEF、RECOVERY/TKA 等）。
- 成本不通过：候选相对基线中位运行时长约 1.414 倍，超过合同 1.25；输入 token 增幅约 1.167，总 token 增幅约 1.170。
- `decision.json`：`reject`。全文恢复机制实际触发，但不能证明最终答案收益；生产候选保持不变。
- 候选适配器在原冻结身份中位于外部草案目录，事前身份未覆盖；`candidate-source-posthoc-sha256.json` 仅记录事后哈希，不能修复该限制。

## 生产状态

- 确定性生产 bug 已修复：`a14172a8` 修复 KoboldCPP JSON grammar 正则兼容；`4f26c81d` 防止过多 passage preview descriptors 触发恢复失败。
- 实验候选未合入生产。
- 当前 5001 只有一个 KoboldCPP listener（PID 23364）。

## 下一步

开始③新方案：只研究覆盖对象、版本、限制的上下文保留；旧词项排序实验不复用。先写隔离 A/B 脚本和开发合同，确保实际溢出，再做正式新题和双 seed。③未通过前不进入 4A/4B；每阶段独立冻结、评分、提交和文档记录。

## 验证范围

本轮使用定向测试、Ruff 和相关类型检查；没有运行全量 pytest。所有正式失败、请求、回执、答案、知识库和成本记录保留在对应 `.e2e-artifacts` 目录。

### ③ 覆盖比较面上下文保留

新候选脚本与单元测试已完成。开发目录 esearch-window-coverage-dev-v7 的 4 次真实运行只有 1 次 baseline-only overflow，候选没有成对溢出，decision.json 判 inconclusive；未启动正式矩阵。

