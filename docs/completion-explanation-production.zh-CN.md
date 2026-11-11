# 解释组装与普通 Chat 双声明合同调整

日期：2026-11-11。[实施方案](completion-explanation-source-gaps-fix-plan.zh-CN.md) · [根因调查](completion-explanation-source-gaps-root-cause.zh-CN.md) · [历史适配器 A/B](completion-explanation-ab.zh-CN.md)。

## 生产行为

保留 B：显式 `output_format="json_explanation"` 时，模型在 `done.summary` 提交解释，`prepare_completion_output` 将完整字符串放入 `/explanation`，从正式回执绑定声明的其他字段，最终经过已有 schema 和完成检查。引号、换行、首尾空白、空字符串及 JSON 外观均不在归一化时改写；空字符串能否交付由输出 schema 决定。

`output_schema` 必须声明必填字符串 explanation；省略时使用该最小对象 schema。精确字段通过已有 `require_tool_success(... answer_pointer=..., result_pointer=...)` 声明绑定。绑定不得覆盖根、explanation 或其子路径；字段缺失、类型不符、父调用未提交或回执冲突仍拒绝。声明沿原 required_checks 冻结，不额外存一份绑定结果。该模式可由 API / Python Headless 显式启用；不新增 UI 或 CLI 开关，strict Research 在提交时拒绝混用。

普通 Chat（`task_kind == "chat"` 且非 strict Research）的 text、json、json_explanation 合同同时移除 source_gaps、execution_claims。模型仍看到真实工具结果和来源覆盖，用来解释限制。若旧式回复多填这两个外层字段，文本与原生协议在同一个归一化入口整字段忽略，原始响应保留。其余参数、外层协议、重复键和大小限制继续严格检查；不处理解释或业务 JSON 中的同名字段。

不生成新的 execution_claims，不增加事实表或执行循环。现有完成门直接解码正式 shell/run 及浏览器回执，读取 task_source_coverage 和 ledger。浏览器或 CodeMode 父回执不能证明进程成功；必要工具未执行、要求成功却非零退出、要求结束却仍运行、必要来源未尝试都仍阻断。真实必要来源不可获取按原规则 blocked。分析失败本身的任务不被一概禁止，后来成功及可选来源失败按原任务要求判断。

冻结快照写入 version 4，唯一布尔标记为 ignore_runtime_claims；version 1～3 恢复默认为 False，保留旧合同。恢复不猜当前 Registry，不跳过 implementation identity，不改写历史 no_progress。Hybrid、Project、Research 等保持各自声明语义。report_sections 和 knowledge_refs 保留；报告修复模式读取原请求冻结 done 的必填字段，模型多填字段不能改变模式。

直接相关的确定性问题一并修复：json_explanation 在生产归一化阶段被 strip；外层重复 JSON 键被覆盖；修复模式由提案字段触发。重复键检查复用共享 JSON 解码辅助函数，覆盖文本和两种原生 API 协议。

## 当前源码验证

相关生产回归 **373 项通过**，另一个不重叠的 provider / 原生终止 / 修复快照 / ToolSpec 回归组 **44 项通过**，共 **417 项**。包含真实 Headless Chat/Hybrid 文本及原生入口、失败进程与运行中进程、声明负例、来源覆盖、回执绑定、冷恢复、浏览器上传下载、重接管及 CDP 生命周期。未运行全量 pytest。

修改的 **25 个 Python 文件通过 Ruff 和 strict mypy**；同时检查 api_provider / api_codec 的类型依赖。修改文件不含 Any、cast 或 type: ignore，没有关闭检查。mypy 使用 `--explicit-package-bases`，避免无 __init__.py 的 tools 同时被当作两个模块名。一次辅助测试命令写错测试文件名，未执行测试；纠正后 44 项通过，不计为生产失败。

最终 A/B 目录为 `.e2e-artifacts/runtime-claims-20261111-ab-final/`，源码 SHA256 为 `36dde2ace4552224c10bb334b97e15225d437242f44be6273522b720927e26a9`，前后相同。真实 Chromium、生产采集链、相同双字段新合同与正式绑定，五类解释各两对、AB/BA 交替，本地 Gemma4、temperature=0、seed=123456789；实际请求和响应均保留。

| 指标 | A：生产 json | B：生产 json_explanation |
|---|---:|---:|
| 首次交付 | 4/10 | 10/10 |
| 最终结构、精确字段及有限事实检查 | 4/10 | 10/10 |
| 模型调用 | 24 | 10 |
| 全部额外解释要求 | 4/10 | 8/10 |

B 没有 source_gaps / execution_claims 拒绝及对应修复调用，10 份解释原样保存，URL/token 精确正确。质量 audit 单独保留；A 的 6 次失败均有原始提案、实际首个拒绝及修复链，不能把最终停机说明的格式检查当作原因。完整 full-surface 与自主案例在独立目录验收，不并入这 10 对。

## 原始提案重放

`.e2e-artifacts/runtime-claims-20261111-replay-final/replay.json` 保留两轮共 20 份原始首次提案、原冻结快照和结果、提案 SHA256、校验过的正式回执及新合同快照。旧合同完成 **5/20**，新生产文本归一化 **20/20**，原生等价归一化 **20/20**；解释逐字保存、URL/token 精确匹配均 **20/20**。

`execution-claims-04B.json` 单独重放 source_gaps-only 批次 04/B：它把浏览器回执申报成 process。新合同文本和原生等价入口均完成，解释和精确字段保持不变。原始中间实验 v4 快照完整保留，生产不为尚未发布的开发形状增加迁移兼容；不重新解释它的原结果。

原生等价重放使用原始外层提案解码出的相同参数重新表达，未手工删字段，不冒充原始原生传输。离线重放不运行模型或浏览器，也不等同于真实进程恢复；历史 implementation identity 检查没有放宽。

## 历史批次与首次失败

以下产物全部保留；不同源码与合同的批次不合并。路径均位于忽略的 .e2e-artifacts，原始状态不加入 Git。

| 批次目录 | A | B | 限制 / 首次失败 |
|---|---|---|---|
| completion-explanation-ab-20261111-main | 1/10，35 调用 | 10/10，10 调用 | 历史实验适配器；B 额外解释要求 7/10 |
| production-explanation-20261111-first | 未同期运行 | 4/10，24 调用 | 6 次虚假 source_gaps；过程中补过生产代码，不作为固定源码验收 |
| production-explanation-20261111-final | 未同期运行 | 1/10，31 调用 | 固定源码，9 次虚假 source_gaps；采集均成功 |
| source-gaps-fix-20261111-ab | 3/10，26 调用 | 9/10，13 调用 | 仅移除 source_gaps；04/B 的 execution_claims 错误促成范围调整 |
| source-gaps-fix-20261111-ab-v2 | 5/10，21 调用 | 10/10，10 调用 | 双字段、中间两个内部标记版本；B 全部要求 7/10 |
| source-gaps-fix-20261111-ab-v3 | 4/10，24 调用 | 10/10，10 调用 | 单标记，之后恢复非 Chat 的原始描述；B 全部要求 8/10 |

A/B 表中通过为最终结构、精确字段及有限事实检查，首次交付率须读取对应 first_delivery_successes，不能用最终完成替代。B 这些通过均首次完成。事实 marker 和排版模式属于有限检查，不证明解释完全正确。

较早的 production-explanation-full-surface-20261111-first 为 5/7，structured 的模型程序错误、Hybrid 的旧文件版本修复失败均保留；独立单次重跑 structured 仍失败（URL 抄错），Hybrid 成功，不拼接统计。旧 structured 使用 text，不能证明 B 的绑定能力。

source-gaps-fix-20261111-surface-first 因范围调整中断。surface-v2 因审计工具未处理无 JSON ref 的下载元数据中断；该工具错误已修复。surface-v2-rerun 为 **6/10**，通过的恰为 basic、legacy_structured、reread、stale_ref、download、research；structured、stale_edit、upload、source_failure 失败。此前文档把 structured 错列为通过，现按原始 summary 更正，原始 summary 未修改。

surface-v3 原始脚本结果 **4/10**：basic、reread、stale_ref、research 通过。逐项原始证据显示：structured 的 CodeMode 程序漏 return、修复又未消费原子调用顺序；stale_edit 使用无效 base_result_ref，随后 provider 超时；upload 已上传但解释使用 [content] 占位；download 先误用 snapshot 解包，再出现 provider 响应混入 HTTP headers 的错误；source_failure 未实际读来源。legacy_structured 的 URL 实际正确但带 Markdown 加粗，被旧验收正则误判；修正的是 gate URL 提取，补了正确 Markdown / 混入错误 URL 的回归，未改生产合同，也不改写旧统计。

自主新合同 source-gaps-fix-20261111-autonomous-v2 为 1/1 完成、5 调用、3 次模型程序失败。较早 production-explanation-20261111-autonomous 也单独保留，不并入固定采集统计。

## 结果解释与剩余限制

### 提交时状态：用户要求先提交并停止

最终 full-surface 批次 `.e2e-artifacts/runtime-claims-20261111-surface-final/` 在额外创建 worktree 后发生机器死机、重启，运行中断，未取得完整批次 summary，不作为完整通过验收。已保存场景的首次结果保留；未完成项不算通过。此后不再使用 worktree，不启动新实机或测试；按用户明确要求提交当前修改后停止。

最终矩阵已观察到 Hybrid stale_edit 失败：模型首次编辑没有提供 base_result_ref，后续反复从 unwrap 后的数据中读取 result_ref，得到 undefined。写入被拒，项目文件仍为外部修改后的内容，stop_reason=no_progress。该案例没有完成真正的旧文件版本修复，不能拿 DOM stale-ref 通过替代文件 stale-edit 验收。

额外补查 `tests/test_versioned_content_edit.py` 为 **14 通过、2 失败**：两例实际拒绝非法 base_result_ref 属性，但错误提示文字不符合测试中要求的 readResult.result_ref / not readResult.value.result_ref 文案。它们不在前述 417 项通过中；基线运行未完成，不能断言已证实是历史问题，也不能声称相关测试全部通过。该文件及 CodeMode 的对应提示实现本轮未修改，问题保留。基线目录日志为 no tests ran，不能用作基线结论。

创建 worktree 是本次操作失误；机器重启的具体系统原因未经诊断。不扫描、清理或继续使用该副本，后续应直接在原工作区验证。最终源码的自主 Chat B 新样本未完成；之前成功样本保留其原批次限制。

收回两个声明解决的是多余的事实重编码职责；不保证模型正文无虚假陈述，也不保证自主浏览器程序、项目写入或自然语言排版正确。固定 Chromium 采集 A/B 仅隔离交付责任，不能替代自主 full-surface。

每次实机保存实际请求、seed、响应、正式回执、首个完成拒绝及后续修复、停止原因、事后格式检查和源码指纹。工具程序失败可能发生在 done 之前，此时 first_rejection 为空；应读原始 provider 与工具回执。停机说明不是 JSON 的事后检查不作为首个失败原因。

不通过继续忽略其他参数、把浏览器放宽为进程证据或反复重跑追求全绿。当前模型和服务环境的失败需分别报告，后续改进另定范围。

## 后续重新授权的实验验证（2026-11-11）

用户在上述历史停止要求之后，明确重新授权先写实验脚本、再运行完整表面 A/B，最后要求本组结束后更新文档、汇报失败、commit 并停止。本轮直接使用原工作区，未改生产代码或生产 prompt，未创建或使用 worktree。详细任务、协议、证据与限制见[原始读取对象完整表面实验](receipt-bound-edit-surface-probe.zh-CN.md)。

`.e2e-artifacts/receipt-bound-edit-surface-20261111/` 完整 **23/28**：现有编辑接口 A **11/14**、原始读取对象接口 B **12/14**；排除上传绑定与固定来源读取两个独立诊断，常规场景为 A **10/12**、B **11/12**。Chat basic、新 structured、Hybrid、strict Research、冻结契约快照恢复、重复读取、DOM stale-ref、文件 stale-edit、下载及自主 open_url 来源失败均两组通过。source-failure 的通过含义是真实 403 后 blocked，未将其统计为读取成功。

五个失败单元全部保留：legacy structured / A 是非法页面求值代码与编译错误耗尽轮次；legacy structured / B 在工具成功后用占位说明替代真实 token；upload / A 上传和读取成功，但最终漏报 SHA256；upload_bound / A、B 都先把 browser_read 字符串错误取 `.text` 导致父调用失败，随后修复未正确消费回放控制流，完成绑定缺少成功父回执，最终 no_progress。脚本另外用成功父调用的真实 Chromium 负例确认：现有绑定器只消费 canonical.value，browser_read 正文位于 canonical.research_observation.text，不能直接据现有接口绑定 page_text；这是独立于实机首次程序错误的机制限制。

Hybrid / A 曾漏传 base_result_ref，恢复后 6 次模型调用完成；B 正确使用原始读取对象，无失败工具回执，4 次完成。stale-edit 两组均经历一次有效旧凭证的真实版本拒绝，重新读取并保留外部新增字段后完成，各 5 次调用。它支持原型保持版本保护，没有证明成功率稳定提高，也没有证据把其余场景差异归因于编辑接口。固定来源读取诊断由脚本提供首次 open_url，不能冒充模型自主工具选择。

14 对任务文字一致，生产源码内容摘要始终为 `0686d61cd4fbd923cfb089a89abc0f772a9331366c8152a9c756b49a118ca4b1`（codey-files-sha256-v2），五份实验脚本运行期间未变。实际请求、seed、响应、原始／展开程序、来源及工具回执、首次工具拒绝与首次完成拒绝、状态库及最终矩阵全部保留；wire-audit.json 记录审计。完整请求含独立 workspace、时钟和回执标识，不能把固定 seed 当作跨单元确定性。

本轮相关测试 **52 项通过**，Ruff、strict mypy 通过，不包含或覆盖此前 versioned_content_edit 的两项提示文案失败。未放宽任何生产完成规则、未将实验回执事实报告替代真实模型答案。按用户最新要求，本组文档与实验代码提交后停止，不再启动测试或实机。

## 再次授权的机械协议实验（2026-11-11）

用户随后重新授权在实验层继续接管可机械化协议、校验、状态管理和结果组装，并运行实机；运行中再次明确要求本组结束后更新文档、汇报失败、commit 并停止。仅修改 tools / tests / docs，生产文件和生产提示模板未改，未创建 worktree。B 的实验工具声明、正文返回结构、调度和组装改变，不能说两组完整模型上下文不变。详细记录见[机械协议与事实交付实验](mechanical-surface-ab.zh-CN.md)。

固定 14 场景、每场景两对 A/B，共 56 单元，串行交替 AB/BA，全部完成；没有补跑挑选。`.e2e-artifacts/mechanical-surface-ab-20261111/` 为 **46/56**，A **23/28**、B **23/28**。排除上传绑定与固定来源两个诊断，常规场景 A **21/24**、B **20/24**；再排除 B 已知来源机械前置读取的 source_failure，A **19/22**、B **18/22**。**本批次没有证明这版 B 比 A 更稳。**

十个失败单元为：第一轮 legacy 的 A/B；两轮 stale_edit 的 A；第一轮普通 upload 的 B；第二轮 snapshot_restore 和 stale_ref 的 B；两轮 upload_bound 的 A；第二轮 upload_bound 的 B。legacy 两组首轮非法 eval / 修复语法错误耗尽轮次、第二轮均通过，B 未采用属性助手；两个 stale_edit / A 均先漏凭证，第二轮又从 unwrap 后对象取 result_ref，未发生真实有效旧凭证冲突。普通 upload / B 及第二轮 upload_bound / B 均在 snapshot.elements 未解包处重复失败，未实际上传。两个 upload_bound / A 后续已有成功父调用，但现有绑定器仍取不到正文，缺 page_text 而 no_progress。

第二轮 snapshot_restore / B 和 stale_ref / B 均先成功机械修复导航返回错误，之后漏传 browser_wait 必填 timeout_ms；原始父错误被恢复控制流集合检查呈现为通用的“未消费控制流”，模型重复程序后 no_progress。冻结契约序列化校验未报失真，stale-ref 拒绝及新 ref 点击也实际正确；不是快照或 DOM 保护失效。只读追查同时暴露入场参数预校验与错误定位缺口，未修改生产实现。

B 两轮 stale_edit 均恰好一次真实版本冲突，随后新读取、重新计算、保留外部字段并成功写入；A 两轮均未完成。B 共成功提交 7 次现有 repair_of 回放，未重复执行回放序列的动作，但其中两个任务后来仍失败。第一轮 upload_bound / B 的正文绑定实机通过，且只选择一次附件，A 同轮选择三次；第二轮 B 在前置解包失败。第一轮 download 虽两组通过，B 因下载后访问未解包的 path 又下载一次，实际 A/B 为 1/2 次，说明普通父调用错误仍可能导致重复副作用。

全部入场程序未观察到新属性 / 命名助手采用。B 的普通文本事实报告有 18 次准备、14 次最终交付，32 项事实选值及 11 项 JSON 绑定字段均回溯到原始成功父/子回执；实机成功文本的模型原说明已含所检事实，不能宣称本批次实际挽救了模型漏报。对应遗漏正反例由真实 Chromium 配合脚本化回答测试单独验证；程序没有自动修正模型自由说明的语义。

生产源码摘要仍为上述 `0686d61c…ca4b1`，六份运行脚本全程冻结，28 对任务文字相同，254 份真实 HTTP 请求均核对 seed / temperature。模型调用 A/B **125/129**，输入 token **1,005,886/1,048,149**，没有观察到调用或输入减少。实机前先有 73 项相关回归通过，增加截断检查后新模块 22 项通过，合计 74 个不同测试项；Ruff / strict mypy 通过，不包含旧 versioned_content_edit 两项文案失败。

现有 B 仍把低层 JS、Result 解包、必填参数及父返回组装留给模型，可选助手与后置组装不足以把机械责任从主流程移走。记录该负面结果及所有原始失败，按用户要求提交后停止，不补跑、不改生产代码。

## 结构化必经入口实验（2026-11-11）

用户再次授权先写实验脚本、运行 A/B，先不改生产代码，并要求本轮结束就 commit。本轮只修改 tools / tests / docs，未改生产源码、生产提示或模型配置，未创建 worktree。详细设计、完整矩阵、26 个失败单元、审计限制及下一步建议见[结构化必经步骤与回执交付实验](structured-surface-ab.zh-CN.md)。

固定批次 `.e2e-artifacts/structured-surface-named-ab-20261111/` 执行 56 个单元：**A 17/28、B 13/28**；排除上传绑定与固定来源诊断，常规场景 **A 15/24、B 11/24**。55 个有完整 worker summary，第二轮 legacy/B 达到 300 秒预算中止，保留部分日志与失败；首轮 basic/B 遇到服务响应混入 HTTP headers，未重发。排除该服务异常整对后 A 16/27、B 13/27，仍未显示组合收益。更早的可选 ref 探索批次明确中止并独立保留，不混入正式统计。

B 将模型入口改为 JSON steps，编译为固定真实 CodeMode 程序，接管解包、父返回、命名目标、等待默认参数及编辑凭证绑定；真实生产权限、版本、回执与完成门仍执行。模型手写 JS 为 0，94 个计划编译、3 个静态拒绝，但已结算回执中有 74 次 failed_step。两轮旧文件冲突均正确拒绝旧凭证、重读并以新凭证写入正确数值，却都删除 external_revision：静态计划在收到新读取结果前已经固定全文，说明新凭证不能代替对新正文的重新计算。该负例已确定性复现，未改原型掩盖失败，当前组合不宜进入生产。

四个上传单元的 B 共 40 次把文件名 fixture.txt 当控件名，全部未执行上传；不能当作成功去重或绑定器验证。44 次缓存复用仅为导航、填表、点击；页面重置后复用旧步骤又造成等待失败。两个 stale_ref/B 均未选择历史 snapshot_id，done 但未完成故意旧引用诊断。属性操作在主矩阵零采用；矩阵后另有两次不执行工具的最小 schema 诊断，两者均可输出属性分支，不能认为该分支始终被解码器屏蔽。适配层另丢失逐工具说明/输出契约，静态拒绝反馈仍附旧 JS 契约，这些独立贡献尚未隔离。

局部正例为 B 的 Research、快照、重复读取、下载均两轮通过，各 2 次模型调用；自主来源失败两轮均真实 403 后 blocked。B 事实报告有 16 次准备、11 个最终交付、24 项准备阶段选值回溯核对，未观察到成功文本仅靠报告挽救模型遗漏；没有成功的 B JSON 绑定交付。不能将局部能力或父调用失败减少等同于端到端收益。

251 次矩阵真实请求逐份核对 seed / temperature 与 A-code、B-steps 契约；额外两次 schema 请求单列。A/B 模型调用 **126/125**；输入 token **1,044,241/至少 1,089,818**，输出 **23,065/至少 30,408**，B 的服务异常请求缺用量，不当零处理。28 对任务文字相同，27 对有完整配对摘要，超时单元原 comparable=false 保留。批次前后生产摘要仍为 `0686d61cd4fbd923cfb089a89abc0f772a9331366c8152a9c756b49a118ca4b1`，七份运行脚本全程冻结。

开发阶段组合回归 92 项通过；冻结前新模块 26 项通过，实机后补入字段丢失负例，最终新模块 **27 项通过**，其中负例通过表示已复现未修复问题。Ruff 通过，strict mypy 明确只覆盖两个变更 tools 脚本；不声称被导入的旧测试模块全部通过 strict mypy，也未修复旧 versioned_content_edit 两项提示文案失败。RAM-disk 清理告警保留。完成报告和检查后按用户要求提交并停止，不补跑追求全绿，不改生产代码。
