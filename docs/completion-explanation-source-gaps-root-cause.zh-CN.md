# B 接入生产后 source_gaps 停滞的根因调查

调查日期：2026-11-11。对象为 `production-explanation-20261111-first` 的 10 个案例，以及提交 `3c59237e` 对应历史实验中的 B 组。本节记录最初的只读调查，当时没有修改生产代码或调用实机。后续已实施普通 Chat 双声明合同调整，最终设计见[修改方案](completion-explanation-source-gaps-fix-plan.zh-CN.md)，实机及剩余限制见[生产验证报告](completion-explanation-production.zh-CN.md)。历史实验结果未改写。

## 结论

这 6 次失败的直接原因是模型在外层 `done.args.source_gaps` 中把成功回执声明为失败采集。B 的解释字符串组装正确；完成门拒绝不实声明也符合现有合同。

系统层面的缺陷是完成接口的职责没有随解释组装一起收缩：普通 Chat 仍收到混合了 Research 报告字段及可由运行时推导的失败声明的通用 `done`。模型即使已经正确完成采集和解释，仍能通过填错一个多余的可选字段使整个交付失败。反馈又没有明确告诉它当前没有可选失败回执，应省略这个字段，因而进入重复拒绝。

这一接口问题在旧实验时已经存在。生产接入改变了模型面向的解释指导，旧实验 B 10 次只生成 `summary`；新首轮有 8 次同时生成 `report_sections`，其中 6 次带有 `source_gaps`。这些记录能够证明行为变化及失败路径，不能证明某一句新增指导必然导致模型生成这些字段。

## 原始记录与确定性重放

首轮失败案例为 02、04、05、07、09、10。每例都有 10 份成功工具回执，来源覆盖中的 `failed_sources` 和 `acquisition_failures` 均为空。这些事实也出现在实际发送给模型的请求中。

首个失败提案全部使用字符串 `reason="None"`。02、05、07、09 引用的实际工具为成功的 `browser_read`；04、10 首次引用成功的 `codemode`，后续改成成功的 `browser_read`。因此这里虚构的是“发生失败”的声明，不是必须不存在的回执 ID。

重放通过只读 SQLite 恢复原始 intent/settlement，以生产 `restore_result_receipt` 核验回执摘要、参数及状态，并使用原冻结 policy、原任务文字、来源 ledger、解释及其他完成参数。比较当前生产组装与旧 B 的 `json.dumps({"explanation": summary})` 适配方式，均调用当前生产组装器和完成门。

| 输入与处理 | 生产 B | 旧 B 适配方式 |
|---|---:|---:|
| 新首轮的原始首次提案 | 4/10 通过 | 4/10 通过 |
| 同一提案仅省略 `source_gaps` | 10/10 通过 | 10/10 通过 |

两种方式组装出的最终 JSON 在全部 10 例中逐字相同，`explanation` 也逐字等于模型原始正文。省略字段的操作只用于离线因果对照，没有写回生产，也不代表已经获得新一轮实机 10/10。

- [重放脚本](../.e2e-artifacts/completion-explanation-root-cause-20261111/replay.py)
- [逐例重放结果、回执及组装内容](../.e2e-artifacts/completion-explanation-root-cause-20261111/replay.json)
- [新旧实际模型请求的逐例比较](../.e2e-artifacts/completion-explanation-root-cause-20261111/request-comparison.json)

在仓库根目录复现：

```powershell
python .e2e-artifacts/completion-explanation-root-cause-20261111/replay.py
```

这是完成边界的离线重放，不是完整历史进程恢复，也不验证解释的全部语义和排版要求。

## 代码中的因果链

1. `codey/toolchain/tool_spec.py:398` 把 Research 的 `done` 描述、`report_sections` 等合并进通用规格；`:416` 再统一追加 `source_gaps`。实际 Chat 请求确认包含全部这些字段。普通 Chat 的必填参数仍只有 `summary`，程序并未强迫模型填写其他字段。
2. `source_gaps` 一旦被填写，schema 的 `minItems=1` 要求至少一项，内部又要求 `reason` 和 `result_ref`。模型生成一条 `reason="None"`，看起来在把“无缺口”填成一条记录；这是对生成模式的解释，无法直接证明模型内部意图。该记录符合外层形状，却不符合回执语义。schema 能约束形状，不能据此证明引用的是失败采集。
3. `codey/operations/completion_output.py:65` 正常保留解释并绑定正式字段。它没有制造或修正外层 `source_gaps`。
4. `codey/operations/completion_gate.py:420` 在后续完成检查之前调用 `validate_source_gaps`。`codey/operations/source_gaps.py:139` 发现回执成功或缺少 `source_failure` 后拒绝。因此这些提案甚至尚未进入后面的输出 schema 检查，不能把拒绝归因于解释转义。
5. `source_gap_context` 和 `task_source_coverage` 已独立提供来源事实；不提交 `source_gaps` 不会消除真实失败，也不免除来源要求。现有字段注释同样说明它只是可选历史声明，覆盖判定不依赖模型抄写。
6. 拒绝信息只有“source_gaps must select distinct failed acquisition receipts from this task”。没有指出具体成功回执，也没有说明当前没有合法失败回执，正确修复动作是省略可选字段。模型收到真实反馈后，继续重复该字段或更换为另一个成功回执，最终被无进展保护停止。
7. `codey/operations/kernel_control.py:139` 还按错误提案是否包含 `report_sections` 决定报告修复提示，而不是按任务冻结合同决定。普通 Chat 的报告字段在 `prepare_research_completion` 中并不编译为严格报告，却能影响修复指导。本轮文本协议只追加了泛化的报告修复措辞；没有证据说本轮走过原生工具协议的报告提示。

此外，实验 `summary.json` 中的 `reason="output_format_invalid"` 来自脚本对最终停机说明再次调用 `final_checks`。它说明停机说明不是交付 JSON，不能替代真实的首个拒绝原因。真正原因应以原始拒绝反馈和提案为准。

## 为什么旧实验能通过，新版本却失败

逐例比较实际 HTTP 请求，10 对 B 请求的 system message、`response_format`、模型、temperature、seed 及其他非 messages 参数全部相同，来源覆盖事实也相同。新版本没有把 `source_gaps` 从可选变成必填，没有新加它的校验，旧适配器也没有过滤它。

变化包括用户任务中移除“实验适配已启用”，生产解释指导替换实验指导，并在两个绑定说明里保留更详细的指针、类型和冲突说明。模型的完整请求还含不同的回执 ID、工作目录及时间。固定 seed 不保证后端完全确定，也不能跨不同请求保证相同字段选择。

所以，旧 10/10 证明当时那组模型提案可以交付，不能证明新提示内容下模型只会提交 `summary`。此次将更多机械工作交给程序确实解决了内层 JSON 包装；外层通用完成接口仍保留了其他能够独立导致失败的模型决策。

## 修正方向与验证目标

后续固定决定将普通 Chat 中同类的 `execution_claims` 一并收回程序端。04/B 的新合同实机失败显示，模型把浏览器操作回执伪装成 `process` 执行声明；这与 `source_gaps` 的问题相同：模型被要求重复选择和分类运行时已拥有的事实。普通 Chat 新冻结合同移除两项字段，归一化入口兼容整字段忽略；正式回执、来源覆盖和任务要求仍由程序完成检查。`execution_claims` 的真实性校验保留给历史快照及其他仍采用旧合同的入口，不能把浏览器回执放宽为进程回执。

后续讨论已形成[完整修改方案](completion-explanation-source-gaps-fix-plan.zh-CN.md)：新普通 Chat 合同废弃 source_gaps 与 execution_claims 两字段并兼容忽略，不再增加针对它的修复提示；历史快照保持原有语义。以下建议保留为调查时的判断，实施以新方案为准。

优先修正模型面向的完成接口：按当前任务冻结真正需要的 `done` 字段和说明。普通 Chat 的解释交付应聚焦 `summary`，Research 保留自身报告合同；来源失败和覆盖继续由正式回执推导。若保留兼容用的可选声明，其合法性检查仍应成立，不能让虚假声明变成证据。

修复反馈应说明具体字段、实际回执状态及可执行动作。在没有任何合法失败采集回执时，明确要求省略 `source_gaps`，复用现有成功采集及解释。报告修复分支也应由冻结任务合同决定，避免错误提案自行切换修复模式。

验收需要同时覆盖成功采集、真实采集失败、错误引用、文本与原生协议及恢复后的同一合同，并保存新一轮固定源码实机结果。离线删字段得到的 10/10 只能证明阻断点，不能代替模型实测，也不能证明解释内容完全正确。

本次运行以下现有相关测试，69 项通过，未运行全量 pytest：

```powershell
python -m pytest tests/test_completion_explanation_production.py tests/test_completion_explanation_ab.py tests/test_completion_output.py tests/test_source_failure_closure.py tests/test_required_source_coverage.py -q
```

生产入口测试的受控 provider 按脚本提交正常参数，能够验证执行、组装和拒绝规则，却不能验证真实模型会否多填可选字段。这也是原有回归通过与此次实机停滞可以同时成立的原因。
