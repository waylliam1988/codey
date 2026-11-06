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

开发目录 `.e2e-artifacts/research-delivery-4a-dev-v1/` 已完成 4 题 × 2 臂真实运行。候选提示在 4 个 delivery 请求中均进入 wire；知识保存题产生真实 candidate claim，普通题未新增 claim。该开发证据随后由正式 v2 覆盖：48/48 完成，baseline 发布 9、delivery 发布 8，候选未增加知识保存成功，正式判定 `reject`。

## 生产状态

确定性生产 bug 已修复：`a14172a8` 修复 KoboldCPP JSON grammar 正则兼容；`4f26c81d` 防止过多 passage preview descriptors 触发恢复失败。实验候选未合入生产。当前 5001 只有一个 KoboldCPP 进程监听（PID 44524；IPv4/IPv6 两条监听属于同一进程）。

## 当前结论与未完成项

2B 已完成 8 个新题 × 2 seed × 2 臂的 32 次真实联网矩阵，并完成逐包语义评分；因严格成功 0/16、材料错配和成本中位时延比 1.414 超过 1.25，判定 `reject`。③开发机制未形成成对溢出，判定 `inconclusive`，没有正式集。4A 正式 v2 已完成 48/48，判定 `reject`。4B 开发 v4 在 17/32 时因真实 writer 空路径 `open_url` 重试后 `no_progress` 停止，判定 `inconclusive`，因此没有启动正式 12 原稿双 seed。所有失败、请求、回执、答案、知识库和成本记录保留在对应 `.e2e-artifacts` 目录。生产候选均未合入；只有确定性读取/协议 bug 已修复。

## 验证范围

本轮使用定向测试、Ruff 和相关类型检查；没有运行全量 pytest。

### 4A 正式结案

4A v1 在第 13 次 task-level `no_progress` 失败处封存为 `inconclusive`。新合同 v2 将此类失败计入样本并继续健康矩阵，48/48 完成：baseline 发布 9、失败 15；delivery 发布 8、失败 16。两臂知识保存题均有 8 个运行保存 candidate claim，候选没有增加保存成功，且多一次失败；`decision.json` 为 `reject`。4A 提示不进入生产。

### 4B：修订与独立审查（开发 v4）

冻结合同为 4 个自然原稿 × 2 seed × U/R/S/V 四臂，共 32 次；每臂都使用真实 KoboldCPP writer、冻结 source fixture、`submit_research_answer`、隔离 knowledge store 和原子发布。`.e2e-artifacts/research-revision-4b-dev-v4/` 已完成 21 次，至 `knowledge_rag-4301-U` 停止。该次真实 writer 连续五次发出空路径 `open_url`，随后 `no_progress`，没有形成证据、提交或知识保存；源码、fixture 和工具协议均已进入运行，属于任务级 writer 执行失败。按冻结合同保留失败并停止，`decision.json` 判 `inconclusive`。不启动 4B 正式 12 原稿双 seed 矩阵，生产保持基线不变。若重做，必须新建合同并先处理空路径重试边界；不能续写 v4。

### 2026-11-07：③ 候选修正与 4B v5 接线合同

③ 的隔离候选发现并修复了确定性语义错误：原实现按“保留优先”排序，却在溢出时按该顺序删除；同时把所有版本折叠成一个 `version` 类别。现在实验候选按 `version:V1`、`version:V2` 等独立覆盖单元计算，并返回明确的淘汰顺序；冗余窗口优先淘汰，所有比较面在预算允许时同时保留。定向测试 2 项通过，Ruff 通过。尚未启动真实模型矩阵。

4B v5 已新增无模型合同/接线辅助和 3 项定向测试：保留完整证据片段、原始 eN 引用映射，U/R/S/V 分别对应直接提交、直接修订、自查后修订、独立证据审查后修订。测试通过；旧 v4 目录继续封存，未覆盖。下一步是用新合同准备开发原稿和真实调用。

### 2026-11-07：③ 无模型成对溢出重放

使用相同的观察窗口、原文片段和 token 权重完成两个无模型 A/B 重放，两个案例的 baseline 与 candidate 均真实触发 overflow。baseline 保留结果分别为 `b,c,d` 与 `b,c,d,e`，丢失 V1；覆盖候选保留 `a,b,c` 与 `a,b,c,d`，同时保住 V1/V2（第二题还保住 V3）和限制窗口。工件为 `.e2e-artifacts/research-window-coverage-replay-v3/replay.json`。这证明候选算法方向和版本覆盖测试有效，但仍不是 writer 答案收益证据；下一步需在相同真实阅读状态重放接入，再决定是否进行模型开发矩阵。

### 2026-11-07：4B v5e 开发接线通过

v5b/v5c 的失败分别是 harness 使用 `__main__` 启动子进程，以及传入生产不支持的 `reasoning_effort=none`；两批封存，不混入结果。修复后新冻结 `.e2e-artifacts/research-revision-4b-dev-v5e/` 使用三份单一 `[e1]` 自然原稿（语义错误、交付遗漏、原本正确），完成 12/12 真实 writer 运行，均实际 `open_url`、`submit_research_answer` 并完成隔离保存。S/V 各完成 3 次独立 reviewer 调用，再进入 writer 修订；`review.json` 共 6 份。该轮只证明四臂接线和真实提交路径有效，不证明语义净收益；`decision.json` 允许进入独立冻结的正式合同。正式合同仍需 12 份新自然原稿 × 2 seed × U/R/S/V，共 96 次运行，未开始。

### 2026-11-07：2A v2 开发验证完成

新增 `.e2e-artifacts/research-live-web-2a-dev-v3/`，4 个开发题 × baseline/browser-after-connector 两臂共 8/8 真实联网、真实 writer、真实提交成功。四个配对都出现连接器 2 条命中；baseline 浏览器调用 0 次，candidate 浏览器调用 4 次，且浏览器结果实际进入各自运行的检索轨迹。该轮只证明取消早停的接线和机制触发有效，尚未完成语义答案评分，不作为生产收益证据。下一步可按新失败规则冻结 12 个正式新题 × 2 seed × 2 臂。

### 2026-11-07：2A 正式 v3 结案

正式新题矩阵 `.e2e-artifacts/research-live-web-2a-formal-v3/` 已完成 12 个新题 × 2 seed × 两臂，共 48/48，全部真实联网、真实 writer、真实提交。机制在 20/24 个候选配对中有效触发（候选浏览器调用 24/24；基线 4/24，另外 20 个基线连接器达到两条后早停）。保守逐题语义评分为 baseline 11/24、candidate 20/24，严重错误均为 4/24；但候选中位时延比 1.363、配对时延比 1.396、admitted 输入字符比 1.332，均超过合同 1.25 成本门槛。`decision.json` 判 `reject`，不做确认集，生产保持基线。完整 `run-inventory-v3.json`、`semantic-ratings-v3.json`、wire、检索观察和提交工件保留；候选质量改善不能抵消预注册成本门槛。

### 2026-11-07：③ 真实 writer 开发 v10 暂停

v10 首对真实阅读状态显示 baseline 可提交，但 coverage 候选把部署限制窗口淘汰后反复重读并 `no_progress`；另有旧 worker arm 名称接线问题，均不构成候选结果。根因是候选只识别通用 `object`，没有把中文任务中的来源对照、条件和边界视为独立覆盖单元。已停止 v10，未把其失败当作收益或损害证据。

随后补充实验候选：来源对照任务要求每个观察来源保持独立覆盖，条件/限制词增加中英文边界识别；新增定向测试 3 项通过，Ruff 通过。必须新建合同后才可重跑真实 writer；当前没有正式③矩阵。

### 2026-11-07：③ v12 开发结案

覆盖候选修正后，新合同 v12 完成 4 题 × 两臂、seed 1701，共 8/8 真实 writer、真实读取和提交成功。压力校准后只有 `coverage_exceptions` 一题在 baseline 与 candidate 两臂都 overflow；版本、限制和对象题两臂都未成对 overflow。按预注册要求，只有一组成对机制触发不足以进入正式矩阵，`decision.json` 判 `inconclusive`，生产保持不变；不再通过追加 padding 追求触发。此前无模型重放的两组成对 overflow 仍只作算法机制证据，不能替代真实 writer 对照。
