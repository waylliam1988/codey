# Research A/B 实验检查点（2026-11-06）

本文件记录当前事实。顺序为 2A → 2B → ③ → 4A/4B。候选没有达到合同门槛前不进入生产。

## 已完成

### ① 缺口驱动研究

旧版候选结束，不采用；不再调词或追加新题。

### 2A：连接器早停

修复后开发集 4 题 × 2 臂共 8/8 完成，实际 JSON tool schema 均在 KoboldCPP 1.117.1 编译成功。修复后正式冻结在首个 baseline writer `no_progress` 失败处停止，计划 48、实际 1，判 `inconclusive`。目录：`.e2e-artifacts/research-live-web-2a-formal-compiled-v2/`。

### 2B：公开全文恢复

正式合同为 8 个新题 × 2 seed × 2 臂，共 32 次运行。目录：`.e2e-artifacts/research-live-web-2b-formal-v1/`。32/32 真实联网、真实 writer、真实提交完成；候选 16 次中 14 次恢复全文、2 次无公开链接。全量配对评分保存于 `semantic-ratings.json`：两臂严格成功均为 0/16，且出现 Attention/QWYC、SPRINT/PRST、STEP 1/STEP-HFpEF、RECOVERY/TKA 等材料错配。候选中位运行时长约为基线 1.414 倍，超过 1.25 成本门槛；输入 token 和总 token 中位增幅约 1.167 与 1.170。`decision.json` 为 `reject`，生产保持不变。

### ③ 覆盖比较面上下文保留

新候选脚本和单元测试已完成。开发目录 `.e2e-artifacts/research-window-coverage-dev-v7/` 的 4 次真实运行只有 1 次 baseline-only overflow，候选没有成对溢出，判 `inconclusive`；未启动正式矩阵。

### 4A：交付完整性提示

开发目录 `.e2e-artifacts/research-delivery-4a-dev-v1/` 已完成 4 题 × 2 臂真实运行。候选提示在 4 个 delivery 请求中均进入 wire；知识保存题产生真实 candidate claim，普通题未新增 claim。仅为开发接线证据，正式 12 题双 seed 尚未开始。

## 生产状态

确定性生产 bug 已修复：`a14172a8` 修复 KoboldCPP JSON grammar 正则兼容；`4f26c81d` 防止过多 passage preview descriptors 触发恢复失败。实验候选未合入生产。当前 5001 只有一个 KoboldCPP listener（PID 23364）。

## 尚未完成

4A 正式 12 题双 seed、4B 四臂真实提交矩阵尚未完成。③没有有效溢出配对，因此不能进入正式集。所有失败、请求、回执、答案、知识库和成本记录保留在对应 `.e2e-artifacts` 目录。

## 验证范围

本轮使用定向测试、Ruff 和相关类型检查；没有运行全量 pytest。

### 4A 正式结案

4A v1 在第 13 次 task-level `no_progress` 失败处封存为 `inconclusive`。新合同 v2 将此类失败计入样本并继续健康矩阵，48/48 完成：baseline 发布 9、失败 15；delivery 发布 8、失败 16。两臂知识保存题均有 8 个运行保存 candidate claim，候选没有增加保存成功，且多一次失败；`decision.json` 为 `reject`。4A 提示不进入生产，4B 尚未开始。

### 4B：修订与独立审查（开发 v4）

冻结合同为 4 个自然原稿 × 2 seed × U/R/S/V 四臂，共 32 次；每臂都使用真实 KoboldCPP writer、冻结 source fixture、`submit_research_answer`、隔离 knowledge store 和原子发布。`.e2e-artifacts/research-revision-4b-dev-v4/` 已完成 21 次，至 `knowledge_rag-4301-U` 停止。该次真实 writer 连续五次发出空路径 `open_url`，随后 `no_progress`，没有形成证据、提交或知识保存；源码、fixture 和工具协议均已进入运行，属于任务级 writer 执行失败。按冻结合同保留失败并停止，`decision.json` 判 `inconclusive`。不启动 4B 正式 12 原稿双 seed 矩阵，生产保持基线不变。若重做，必须新建合同并先处理空路径重试边界；不能续写 v4。
