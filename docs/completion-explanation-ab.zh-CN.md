# 自然语言解释由程序包装：实机 A/B

本实验只修改 `tools/completion_explanation_ab.py` 和相关测试。生产输出合同、组装器、Registry、浏览器执行器和完成校验不作修改。B 是实验适配，不代表生产已经支持自然语言解释绑定。

## 对照范围

| 项目 | A | B |
|---|---|---|
| 模型的 `done.summary` | 内层 JSON 对象，包含 `explanation` | 自然语言解释字符串 |
| 解释包装 | 模型处理 JSON 引号、换行等 | 实验适配用 `json.dumps` 包装 |
| `page_url`、`token` | 生产回执绑定 | 相同 |
| 最终 schema 与字段精确校验 | 严格检查 | 相同 |

实验适配只改变模型面向的输出说明，以及有效 `done` 调用中的 `summary`。不修复外层工具调用的 JSON，不猜测或拆解看起来像 JSON 的解释，不改写模型工具程序，不替模型补执行步骤。原始请求、模型响应、原始解释、适配后调用均单独留存。

URL 从 `browser_tab_list` 的正式回执读取；token 限定 `browser_eval(code="window.__contractToken")`，两者都要求成功的 CodeMode 父调用。绑定和最终发布继续走生产 `prepare_completion_output`、完成校验及原有任务循环，没有另外实现执行循环或回执选择逻辑。

## 实机方法

模型为本地 KoboldCpp Gemma4，`temperature=0`，每次请求显式携带相同 seed；记录服务端实际 seed。固定 seed 不能保证后端输出完全确定。

先进行一对开放式自主浏览试跑，两组都因模型浏览器程序错误在交付前停止，未产生可比较的解释提交。该失败保留，不通过重跑覆盖。

正式实验聚焦交付接缝：相同的固定 CodeMode 程序经生产执行链，在真实 Chromium 中完成导航、快照、填写、新快照、点击、等待、正文读取、eval 和标签列表。该采集程序属于实验准备，不是模型生成；最终解释由实机模型生成。成功完成采集后，两组才比较解释包装的责任差异。

先用一对固定采集试跑确认接线，再正式运行 10 对。两种试跑不合并进正式统计；正式案例串行执行，顺序为 AB、BA 交替。五类解释要求各重复两对：单段、多段真实换行、英文双引号、Markdown 列表、行内代码。

每组运行独立子进程和状态目录，任务仍由生产 headless 入口执行。子进程超时通过已有进程树所有权清理。仅在内存中注入受控网页、模型连接和 B 的候选输出适配。

## 指标含义

- 首次交付成功：固定采集后，第一次真实模型生成就完成严格验收；外层协议错误和额外模型调用不会被忽略。
- 最终成功：生产完成、所需工具确实执行、最终 schema 合法、URL/token 字段与受控页面预期精确相等。
- 事实标记：解释包含独立数字 `173` 和完整 `QUARTZ_851`，仅为有限内容检查，不能代表任意解释语义正确。
- 修复成本：模型调用、完成提案、交付后重复工具、token 和耗时。响应或 usage 缺失时报告未知，不按零计算。
- 解释质量需要另查换行、引号、格式要求以及是否在正文再次抄写绑定字段。最终字段准确不能证明整段解释准确。

## 运行与产物

```powershell
python -m tools.completion_explanation_ab --run-dir .e2e-artifacts/explanation-ab-new --pairs 10
python -m tools.completion_explanation_ab --run-dir .e2e-artifacts/explanation-ab-new --audit
```

默认固定真实浏览器采集以隔离交付。`--autonomous-acquisition` 用于额外开放式探索，结果应单独报告。脚本正常完成实验且生产源码指纹未变时返回 0；这不是“所有案例通过”的退出码，实际成功率以 summary 为准。

本轮产物：

- `.e2e-artifacts/completion-explanation-ab-20261111-pilot/`：最初开放式试跑，包括首次失败。
- `.e2e-artifacts/completion-explanation-ab-20261111-fixed-pilot/`：固定采集接线试跑。
- `.e2e-artifacts/completion-explanation-ab-20261111-main/`：正式 10 对，保留每次请求、响应、回执状态库、原始提案、适配后内容与结果。

正式运行目录还保留执行时的实验脚本副本及 SHA256，便于区分后续审计代码。不能将固定采集的结果解释成任意网页自主任务、strict Research、Hybrid、上传下载或进程恢复已得到验证。

## 2026-11-11 实测结果

本地 KoboldCpp 1.117.1，模型 `Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M`。正式 20 次采集全部成功，浏览器/CodeMode 工具失败数为 0。45 次真实模型调用均观测到 seed `123456789`，服务生成计数连续；生产源码 SHA256 前后相同。

| 指标 | A：模型包装 | B：实验程序包装 |
|---|---:|---:|
| 首次生成就完成结构与精确字段验收 | 1/10 | 10/10 |
| 最终结构与精确字段验收通过 | 1/10 | 10/10 |
| 模型调用/完成提案 | 35 | 10 |
| 额外修复提案 | 25 | 0 |
| 输入 token 总量 | 323,288 | 88,035 |
| 输出 token 总量 | 6,338 | 913 |
| 模型请求耗时总量 | 289.970 秒 | 65.827 秒 |
| 案例总耗时（含准备与采集） | 376.250 秒 | 136.592 秒 |

A 的 9 次失败均检查了原始提案、实际模型请求中的拒绝反馈和正式工具事件：6 次未提供所需内层 JSON 对象，2 次内层双引号未转义，1 次内层字符串包含未转义的真实换行。生产完成检查明确拒绝缺失 `explanation` 的结果；模型修复未成功，最终 `no_progress`。不是浏览器执行失败，也未发现本实验触发的生产确定性 bug。首次失败和所有修复尝试保留，未另跑直到通过。

例如 `03/A` 和 `08/A` 的内层内容包含 `{"explanation":"点击 "Load record" …"}`，而对应 B 只提交带有正常双引号的解释。B 的 10 份最终解释都与模型原始解释逐字一致，换行与引号由程序序列化保存；URL/token 字段全部精确正确。

### 解释质量审计与剩余限制

`audit.json` 为只读原始产物后另存的审计，不覆盖 `summary.json`。

- B 的明确排版要求通过 8/10；`04/B`、`09/B` 没有遵守“仅两条列表”的要求。
- B 有 2/10 在解释中重复了已绑定的 URL/token（`02/B`、`04/B`），违反“不重复这些值”的提示要求；本次没有检测到错误 URL 或 token。
- 同时满足上述排版和不重复要求的 B 为 7/10。因此不能将 10/10 的结构交付通过率说成解释所有要求全部通过。
- 两组交付后均未重复执行浏览器工具。B 的收益主要来自避免内层包装错误及修复调用，没有证据表明它修复了自主浏览脚本生成问题。
- 开放式原始试跑 A/B 都在交付前失败；固定采集接线试跑两组都通过。这些结果单独保留，不计入正式 10 对。
- 单个本地模型、受控页面、小样本及状态敏感后端，支持继续验证这条设计方向，不能推广为所有模型或任意任务的成功率。

验证命令：

```powershell
python -m pytest tests/test_completion_explanation_ab.py tests/test_completion_output.py -q
python -m ruff check tools/completion_explanation_ab.py tests/test_completion_explanation_ab.py
python -m mypy --explicit-package-bases tools/completion_explanation_ab.py tests/test_completion_explanation_ab.py
```

只验证本轮测试/工具影响范围，未运行全量 pytest 或改动浏览器生产执行链；没有宣称完成 full-surface 全矩阵验收。

本轮相关回归 27 项通过，Ruff 通过，mypy 严格检查的两个新增 Python 文件通过；未使用 `Any`、`cast`、`type: ignore` 或关闭检查规避类型问题。
