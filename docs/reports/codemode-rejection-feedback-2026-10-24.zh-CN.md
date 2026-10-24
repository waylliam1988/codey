# CodeMode 拒绝反馈与返回值契约验收（2026-10-24）

基线：`f8b656ab`。本批目标是改善 CodeMode 接口选择和失败后的纠错条件；验收采用“本批有明确收益，相对原版没有发现新增回归”。不以所有模型流程全部通过为合并条件。

## 已复现的问题与修改

1. 宿主访问、递归调用共用一条错误，文件读取被拒也收到 shell/HTTP 建议。文本与原生归一化均可复现。现有检查表达式保持不变，错误改为指出实际命中的禁止 token 或递归调用，并说明程序在执行前被拒。
2. 拒绝回执没有带回当轮已经发送的 CodeMode 说明。错误渲染现在直接接收该轮冻结 ToolSpec 的 description；冻结后修改实时注册表，回执仍保留原说明，不携带后来未发送的定义。
3. 真实 Node worker 执行 `const a = await tools.read_file(...); return a.split(...)`，返回 `a.split is not a function`，原生产结果没有返回值契约。现在运行失败与参数拒绝共用 `codemode_error_text`，运行结果在正式提交之前附上冻结说明，仍保留失败状态、原错误、调用身份和审计字段。

初始说明同步明确已有运行语义：子工具返回 `{ok, value}`，检查 ok 后才使用 value；最终结果通过 return 返回，console 不产生返回输出。唯一 JavaScript 示例展示检查 ok、取出 value 的完整过程；原生说明和文本 JSON 示例继续由同一字符串生成。

确定性失败记录：`.e2e-artifacts/codemode-feedback/before.xml`（4 项失败），`runtime-before.xml`（真实 worker 的返回值类型错误反馈缺失）。新增测试共 11 项，覆盖两种协议、冻结后注册表变化、拒绝无执行、真实子读取失败、错误后纠正、关闭并重开 SQLite 后恢复以及重复恢复。

## 实机对照

沿用 `tests/manual/contract_consistency_live.py`，使用正式本地连接、任务内核、Node worker、子工具、回执、完成门和恢复入口。模型为 `koboldcpp/Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M`，temperature 0.3，thinking 关闭，8 轮上限；没有调高轮次或放宽验收。

本批未修改版本由 `git archive f8b656ab` 独立展开运行，不受候选版本源文件修改影响。

| 场景 | 本批观察 | 结论 |
| --- | --- | --- |
| 原版原生读取 | 反复使用 require/fs，最终普通读取，缺少成功 CodeMode | 未通过 |
| 第一轮候选原生读取 | 后来改用 tools.read_file，但重复对返回对象调用 split | 未通过，不合并该中间版本 |
| 最终候选原生读取 | 连续两轮首次调用即完成两个 CodeMode 子读取，JSON 正确、恢复通过 | 两轮均通过 |
| 最终候选原生写入 | 连续两轮有真实 CodeMode edit 子调用，内容和恢复正确 | 两轮均通过 |
| 最终候选文本读取 | 连续两轮有两个 CodeMode 子读取，JSON 和恢复正确 | 两轮均通过 |
| 最终候选文本写入 | 两轮均未通过指定子写入路径，尽管文件正确且完成门通过 | 保留未通过 |
| 本批原版文本写入对照 | 普通 edit 写入后用 CodeMode 读取，同样没有 CodeMode edit 子调用 | 相同失败已在原版复现 |

记录目录：

- `artifacts/codemode-feedback-baseline-20261024-01`：本批原版原生读取。
- `artifacts/codemode-feedback-baseline-20261024-02`：本批原版文本写入。
- `artifacts/codemode-feedback-candidate-20261024-01`：中间候选失败，完整保留。
- `artifacts/codemode-feedback-candidate-20261024-02`、`-03`：最终候选的两轮四场景检查。

每项通过都要求输出/文件正确、对应 CodeMode 子读取或子写入确实发生、完成门及恢复判断通过、重复恢复稳定。文本写入即使 stop_reason 为 done，也不算实机验收通过。现有 `tool_succeeded:codemode` 只检查成功工具观察，不能证明任意自然语言指定的参数、顺序或子调用；本批没有把它扩大解释为完整流程证明。

两轮原生读取成功都发生在初次生成，因此能够证明本批整体契约调整改善了该探针，不能单独证明错误回执调整带来的因果收益。真实错误后的完整链路由脚本化响应测试验证；它不计作真实模型纠错成功率。小样本结果也不代表模型普遍稳定或所有幻觉问题已解决。

## 架构与验收

- ToolSpec 仍是说明和示例的定义来源；错误反馈消费当轮冻结说明，不回查实时注册表，不维护另一套纠错示例。
- 新增的是一个纯文本渲染函数；参数拒绝和运行失败共用它。没有新增执行循环、权限存储、事实表或双写，没有模型名称判断、自动程序改写、隐式普通读取替代或放开宿主访问。
- 成功结果交付保持原样；失败反馈随原有 ToolResult 提交、恢复。四项错误后纠正测试均关闭并重开 SQLite，确认失败/成功记录和完成判断恢复一致，重复恢复不重复累计或执行。
- 相关 pytest：**128 passed、3 skipped**，记录在 `.e2e-artifacts/codemode-feedback/acceptance-final.xml`；没有运行全量 pytest。3 项跳过为原有旧 delivery fixture 跳过，没有新增跳过。
- Ruff、mypy（518 个源文件）、Pyrefly、ty、runtime 清单检查及 `git diff --check` 通过。Pyrefly 仍有原有 29 suppressed / 80 hidden warnings。
- 提示 hash 已逐行核对：只把当前 CodeMode 契约行换回归档版本，就重建旧 hash `b54d4697…`；新 hash 为 `3930b6d7…`。记录在 `.e2e-artifacts/codemode-feedback/prompt-review.json`。结果和通用修复提示的原 hash 测试保持通过。

合并依据是确定性反馈缺陷修复、相关测试和静态检查通过、原生读取实机改善；保留文本写入的原有未通过边界，不宣称所有指定流程均已解决。本批独立提交并合并到本地 master，不推送远端。
