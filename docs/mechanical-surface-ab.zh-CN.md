# 机械协议与事实交付：实验 B 对照

2026-11-11。继原始读取对象编辑实验之后，用户重新授权继续修改实验脚本并运行实机。仅修改 tools、tests、docs，不改 codey 生产文件、生产提示或模型设置，不创建 worktree。

## 实验边界

A 为现有生产工具、回执、完成门和绑定器，经既有 full-surface 实验入口运行。两组共同使用 ExperimentProvider 文本 JSON schema 协议、受控真实 Chromium 页面及已有 CodeMode 工具表面；因此不是完整桌面产品的无干预基线。B 是一组实验机制的组合，不将结果归因于某一个助手。

同一对任务原文、生产源码、模型、temperature=0、seed=123456789、轮次和超时相同。B 的工具声明、运行时包装、恢复调度和结果组装不同；不能说两组完整 prompt 或 HTTP 请求逐字相同。没有人为润色生产提示，但接口改变必然改变自动生成的工具契约。

## B 将什么交给程序

1. 保留上一轮的 edit({read, content})，从原始 read_file 对象绑定路径和版本，不隐藏重新读取或重试。真实过期仍须由模型重新读取、重新计算并保留外部字段。
2. browser_read 在 guest 中明确返回 Result<{text:string}>，保留原始字符串内容和 result_ref；主机执行器、原始回执仍不变。调用方必须按新的真实声明使用 .text，不通过猜测改写模型源程序。
3. 新增按名称 fill/click/upload/download 助手：模型选择名称和值，程序获取新 snapshot，要求唯一匹配，再调用原始动作。缺失或歧义不执行动作；不重试副作用。原始工具仍可用于故意 stale-ref 的场景。
4. browser_property({name}) 只接受一个合法 JS 标识符，生成 window.name 调用真实 browser_eval。模型仍决定需要读取哪个属性；不写死 token 或其他答案。
5. 对 missing_return/result_encoding 且所有子调用均成功的父调用，最多安排一次现有 repair_of 回放：按原始顺序和参数消费所有原回执，只返回回放确认，不重建语义计算，不重新上传。生产内核仍验证 token、能力、参数及完整序列。运行错误、子工具失败、未知回执不自动修复；包含 guest 绑定 edit 的程序不适用此通用回放。
6. 文本任务的必要事实从成功父调用下的原始叶子回执附加：页面正文；legacy 的真实 token/URL；上传的文件名、SHA256、字节数及 effect_verified；下载的文件名、路径、字节数及 SHA256。模型原始说明逐字保留，事实另带回执引用。缺失、冲突或不一致不补猜，不读取测试预期值；重复读取场景按任务明确要求选最后一次读取。
7. 仅在实验 B 内，browser_read 的绑定选择器直接支持 canonical.research_observation.text，验证原始调用身份和参数，保留成功父调用、冲突及截断拒绝；不添加伪造 canonical.value。
8. source_failure 的必要 URL 已由任务显式指定，B 先安排真实 open_url，再让模型解释 403。单独记为机械来源尝试，不算模型自主选工具。source_failure_fixed 仍是两组共享的脚本诊断。

strict Research 保留生产结构化提交和来源引用约束，不附加普通 Chat 的事实报告。structured 和 upload_bound 保留 json_explanation 的模型解释，仅由绑定机制填事实字段。程序不验证模型自由说明的全部语义、不修正错误解释，也不将 effect_verified=false 提升为服务器已接受上传。

## 预先固定的验证

单元及真实 Chromium 脚本化 HTTP 正反例覆盖：明确正文类型；命名目标歧义；缺失、失败、不同调用、不同参数、冲突、截断回执；成功父调用；模型遗漏事实；真实上传一次且失败父调用通过 receipt-only 修复；必要来源先取得真实 403。

实机：14 个场景 × 2 次配对 × A/B，共 56 个单元。串行、交替 AB/BA；每次请求 90 秒，单元预算 300 秒，最多 10 轮。先冻结脚本内容，再运行整个批次，不自动重跑失败，不以成功补跑替换首次结果。模型调用数与脚本动作数分别记录。

```powershell
python -m tools.receipt_bound_edit_surface_probe --mechanical-b --repeats 2 --run-dir .e2e-artifacts/mechanical-surface-ab-20261111 --timeout 90 --budget 300 --max-turns 10
```

常规 12 场景与 upload_bound/source_failure_fixed 两个诊断分开统计。source_failure 的 B 采用确定性前置读取，单列其收益。保留原始模型回答和已组装的最终交付，区分“模型本身抄对”与“应用把真实事实交付正确”。本小样本只提供本批次观察，不证明统计意义上的稳定优势。

## 实机结果

固定批次 `.e2e-artifacts/mechanical-surface-ab-20261111/` 已完整结束，**46/56**，A **23/28**、B **23/28**。退出码 1 来自下列 10 个评分失败；56 个 worker 均取得完整 summary，没有超时中断或补跑替换。

| 范围 | A | B |
| --- | --- | --- |
| 全部 14 场景、两轮 | 23/28 | 23/28 |
| 排除 upload_bound / source_failure_fixed 两个诊断 | 21/24 | 20/24 |
| 再排除 B 前置机械读取的 source_failure | 19/22 | 18/22 |

**本批次不支持“这版 B 比生产路径 A 更稳”。** 常规场景 B 少通过一个单元；两个诊断合计 B 比 A 多通过一个，抵消后总分相同。即使只看工具失败记录，排除预期 stale / 403 后，无非预期工具失败而通过的单元两组均为 13/28；该指标不等同于没有任何协议或完成拒绝。

表中括号为真实模型调用次数，脚本读取及回放不冒充模型调用。403 的“通过”表示来源确实失败、生产完成门正确 blocked。

| 场景 | 第一轮 A | 第一轮 B | 第二轮 A | 第二轮 B |
| --- | --- | --- | --- | --- |
| basic | 通过（6） | 通过（8） | 通过（3） | 通过（3） |
| structured | 通过（10） | 通过（9） | 通过（3） | 通过（4） |
| legacy_structured | max_turns（10） | max_turns（10） | 通过（4） | 通过（5） |
| hybrid | 通过（5） | 通过（4） | 通过（4） | 通过（4） |
| research | 通过（5） | 通过（4） | 通过（3） | 通过（4） |
| snapshot_restore | 通过（3） | 通过（3） | 通过（3） | no_progress（4） |
| reread | 通过（4） | 通过（9） | 通过（4） | 通过（4） |
| stale_ref | 通过（4） | 通过（4） | 通过（4） | no_progress（4） |
| stale_edit | no_progress（7） | 通过（10） | max_turns（10） | 通过（5） |
| upload | 通过（6） | no_progress（5） | 通过（2） | 通过（6） |
| download | 通过（3） | 通过（5） | 通过（3） | 通过（4） |
| source_failure | blocked（2） | blocked（1） | blocked（2） | blocked（1） |
| upload_bound，诊断 | no_progress（7） | 通过（3） | no_progress（6） | no_progress（4） |
| source_failure_fixed，诊断 | blocked（1） | blocked（1） | blocked（1） | blocked（1） |

## 十个失败单元

1. **01-legacy_structured / A、B，共两个**：两组前七步浏览器操作均成功，之后将 `return window.__contractToken;` 传入 browser_eval，得到 Illegal return statement；两次修复又生成带错误分号/引号的 CodeMode 源码，耗尽 10 轮。没有 done 提案，first_rejection 为空是正常观测。B 没有采用 browser_property。第二轮两组均通过，且 B 原始模型答案已含真实 token，不能把第二轮成功归因于属性助手或遗漏补齐。
2. **01-stale_edit / A**：三次主机 edit 均漏传 base_result_ref，真实版本冲突计数为 0，重复无进展。文件仍为外部修改状态，外部字段没有被覆盖；这不是旧版本凭证被错误接受。
3. **02-stale_edit / A**：首次主机 edit 漏凭证，随后将 unwrap 后的 readResult.result_ref 当凭证，得到 undefined，在参数编码阶段失败，耗尽 10 轮。正式失败 edit 只有一次，不能把后两次未入场的程序错误算成主机编辑拒绝；真实版本冲突仍为 0。
4. **01-upload / B**：四次程序直接访问未解包 snapshot.elements，连续 result_contract 失败；browser_upload 根本没有执行。正文类型适配、哈希组装和 return-only 回放均不能补救尚未到达的上传步骤。
5. **02-snapshot_restore / B**：导航缺少 return 的错误先由程序回放成功修复；后续三个程序都漏传 browser_wait.timeout_ms，执行到等待前停止，原始父回执为 `CodeMode recovery did not consume its recorded control flow`，最终 no_progress。五次冻结契约序列化恢复均未报不一致，不能把该单元失败说成快照恢复失真。
6. **02-stale_ref / B**：同样先成功机械修复导航返回；旧 ref 三次均被正确拒绝，之后新 snapshot、新 ref 点击也已成功，但程序又漏传 wait 的必填超时，三次父调用以相同控制流错误结束。未执行最终 browser_read；失败点是后续参数/状态路径，不能归因于旧 ref 被错误接受。
7. **01-upload_bound / A**：首次 snapshot 未解包；后续还有返回形状错误。实际成功选择附件三次，最终已有成功父调用下的上传、读取，但生产绑定器仍取不到 canonical.value 中不存在的正文，首次完成拒绝缺 page_text，最终 no_progress。
8. **02-upload_bound / A**：首次从 browser_read 字符串取 path/text/sha256，返回 filename=undefined；后续又直接访问未解包的 upload.name。实际成功选择附件两次，修复后有成功父调用，最终仍因正文绑定缺 page_text 而连续完成拒绝。原始最后说明本身也未报全事实，不能用其替代正式绑定结果。
9. **02-upload_bound / B**：四次 snapshot.elements 未解包，在上传前失败，browser_upload 次数为 0。该负例并未执行到新的正文选择器，不能说选择器本身又失效。

第 5、6 项的更具体原因来自原始程序、browser_wait 必填 schema 及只读代码追查：`DurableCodeModeReplay.child` 在调用嵌套 execute_turn 前记录已见子身份；缺参调用未形成对应已提交工具意图，最终 assert_valid 的控制流集合检查覆盖了更具体的失败。它暴露了入场前预校验及错误定位的缺口。本轮没有修改生产实现，也没有另开实机复现来扩大结论。

## 有效的局部机制及其限制

**编辑凭证绑定**：B 两轮 stale_edit 均恰好一次有效旧凭证冲突，随后重新读取、重新计算并成功写入 amount=173 / code=QUARTZ_851，外部 external_revision 完整保留；两次各确认一个成功 helper edit。A 两轮均卡在凭证装配，未进入真实版本冲突。B 两轮用 10 / 5 次模型调用，说明正确完成不等于轮次稳定减少。

**回放恢复**：B 共安排并成功提交 **7 次**现有 repair_of 回放：01-snapshot_restore、01-stale_ref、01-upload_bound、02-legacy_structured、02-reread、02-snapshot_restore、02-stale_ref。它们只消费原子序列原有回执，不重新执行其中的动作；后两个单元后来仍因新的必填参数错误失败，不能把“回放成功”算作“整任务成功”。

**上传正文绑定**：01-upload_bound / B 在一次 snapshot 解包失败后恢复；后续 result_encoding 由程序回放，最终生产完成链交付了原始文件名、hash 与真实正文。该单元只有一次成功 browser_upload，A 同一轮有三次。第二轮 B 在更早的 snapshot 上失败，说明正文选择能力存在与模型能否到达它是两件事。

**重复副作用仍未解决**：01-download 两组最终均通过，但 A 成功下载一次，B 两次；B 在下载成功后直接访问未解包结果的 path，父调用 result_contract 失败，模型再次下载。该错误没有生产签发的 return-only 修复凭证，当前程序不会自造 token 或放宽成功父调用要求。两轮 upload_bound 的 A 分别成功选择附件 3 / 2 次，B 分别 1 / 0 次；这是文件输入选择次数，不是服务器接受上传次数。

**可选助手没有被采用**：全部保存的原始入场程序均未见 browser_property 或四个 *_named 助手调用。它们在确定性测试中可用，实机未显示采用；不能把合法 eval、刷新 snapshot 或成功动作归因于这些助手。增加声明没有自动把机械责任从模型主流程移走。

**事实报告**：B 记录 18 次事实组装准备，其中 14 个普通文本单元最终成功交付；32 项附加事实选值全部能回溯到成功父调用下的原始回执，JSON 绑定交付另有 11 项字段逐项核对。读取正文只选原回执，不伪造 canonical.value；普通上传保留 effect_verified=false。全部实机成功文本单元的模型原始最后说明已经包含本场景所检查的事实，因此本批次没有出现“模型漏掉必要事实、仅靠附加报告才通过”的实机正例；模型遗漏而由程序正确交付的能力由真实 Chromium + 脚本化回答测试单独验证。自由说明未做完整语义审查，附加正确字段也不能证明错误叙述已被修正。

**明确来源前置读取**：source_failure 两轮 A 自主 open_url，B 机械执行任务指定 URL，再由模型解释真实 403；两轮均正确 blocked。真实模型调用 A 各 2 次、B 各 1 次，收益来自确定性前置读取，不能算模型更会选工具。两个 fixed-source 诊断双方仍各一次模型调用。

## 审计、检查与停止

28 对任务文字相同，所有单元及批次前后生产源码摘要均为 `0686d61cd4fbd923cfb089a89abc0f772a9331366c8152a9c756b49a118ca4b1`（codey-files-sha256-v2）。六份运行脚本内容摘要在整个批次中未变。254 份真实 HTTP 请求均核对 seed=123456789、temperature=0；脚本动作另记，不计入真实模型调用。完整结果见 summary.json；额外只读审计为 wire-facts-audit.json，保留 audit_batch.py。

真实模型调用 A/B **125 / 129**；输入 token **1,005,886 / 1,048,149**，输出 **17,739 / 16,257**；单元耗时合计 **1,236.625 / 1,221.968 秒**。没有观察到调用数或输入 token 的降低。运行时钟、workspace、回执标识及历史消息不同，数据仅作描述，不作因果节省或统计稳定性证明。

原始 HTTP 请求与响应、提案、任务及请求声明、模型/脚本程序与各层展开、首次工具失败、首次完成拒绝、修复动作、冻结快照、原始正式回执、状态库、产物和失败均保留。mechanical-programs.jsonl 的 model_code 字段也可能记录脚本调度的 repair 程序，应以 mechanical-events.jsonl / provider.jsonl 的事件来源区分，不能将其冒充模型代码。`probe_value` 错误路径来自已有编辑包装；B 的正文壳改变了 guest 返回结构，这是一组组合机制的实验。

实机前相关回归先 **73 项通过**；增加截断正文拒绝后，新模块 **22 项通过**，合计覆盖 **74 个不同测试项**。Ruff 和 strict mypy（explicit-package-bases）通过。pytest 的 RAM-disk 清理告警保留，未修改基础设施；未覆盖或修复旧报告中 versioned_content_edit 的两项文案预期失败。

本批次的主要缺口是：模型仍自行生成低层 JavaScript、解包 Result、填写必填参数和组装父返回值；可选助手与后置事实报告没有接管这些必经步骤。完整的机械执行接口需要先验证计划及参数、明确已完成步骤和回执归属，并将返回组装从任意模型程序中移出；当前程序只在生产已授权的狭窄错误类型内恢复。这些是后续设计依据，本轮不继续实现或另跑矩阵。

用户在运行中再次要求本组结束后更新文档、汇报失败、commit 并停止。依此提交当前实验脚本、测试与报告后停止；不改生产代码，不增加实机或测试，不创建 worktree。
