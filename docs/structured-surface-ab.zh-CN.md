# 结构化必经步骤与回执交付 A/B

2026-11-11。先完成确定性测试，再冻结脚本运行实机。只修改 tools、tests、docs；不修改生产源码、生产提示模板或用户模型配置，不创建 worktree。

**固定矩阵结果：A 17/28，B 13/28；常规场景 A 15/24，B 11/24。本版组合未显示整体收益，不能进入生产。** B 两轮旧文件冲突虽正确绑定新凭证，却都删除外部新增字段，暴露同计划重读与静态全文写入的数据依赖问题。

## 预先固定的实验设计

A 沿用上一轮 full-surface 的生产工具路径。B 将同名 codemode 的模型侧参数改为结构化 steps，按冻结的嵌套工具契约生成 schema 和说明，再将经过完整静态校验的 JSON 编译为固定的 CodeMode 程序。真实执行仍经生产 guest、host、execute_turn、持久工具回执、版本保护、来源覆盖和完成门。原始 HTTP 回答、转换后的提案和最终程序分别记录，生成的 JavaScript 不算模型手写代码。

B 是组合机制，不能从总成功率推断其中任一组件的独立贡献：

1. 模型选择步骤、目标和值，程序处理 Result、父返回、命名目标和 window 属性读取。浏览器动作只接受 name，不接受手填 ref；目标缺失或不唯一时不执行动作。仅 stale_ref 诊断暴露历史 snapshot_id，由程序从该历史快照按名称取真实 ref。
2. 整个计划的静态 schema、步骤 id 和参数先校验，再执行第一步。browser_wait 的实验接口显式默认 5000ms；生成时将该值传入原主机工具，非法超时仍拒绝。动态依赖和页面状态在相应步骤前检查。
3. 额外对原 CodeModeHost 的冻结参数做入场前校验，避免无正式意图的调用先进入 durable 恢复集合；不跳过原恢复检查。
4. 步骤成功后写入现有任务内 CodeMode 状态。相同变更步骤 id 和相同参数复用已完成结果；同 id 改参数拒绝，显式新 id 允许新动作。读取步骤重新执行；edit 从 read_id 绑定真实读取路径、完整性和 result_ref，最终由原主机检查版本。
5. 使用上一轮已验证的原始正文选择器和回执事实报告。模型负责解释和编辑内容，程序交付声明的精确事实。普通上传仍保留 effect_verified=false；strict Research 保留原完整提交与引用规则。
6. 完成门仍运行全部 required checks，只调整反馈选择顺序，优先报告绑定所依赖的工具或父调用错误，不将缺失回执归为答案排版修复。

生产 frozen turn contract 保持原形状；新的模型侧 wire schema、转换规则和程序摘要另存实验记录。snapshot_restore 仍只证明原冻结主机契约的序列化恢复，不证明实验控制器或整任务杀进程恢复。

步骤状态写入发生在工具结算之后；本实验不声称覆盖两者之间的进程崩溃窗口，也不承诺跨新步骤 id 的全局副作用去重。原有 CodeMode 状态容量、工具权限和预算不扩张。

source_failure 的两组都由模型选择真实 open_url，不沿用上一版 B 的机械前置来源读取。source_failure_fixed 仍是双方共享的脚本首步诊断。

## 验收与运行

确定性测试包括：合法/非法计划、后置缺参阻止前置导航、冻结权限、属性名注入拒绝、目标歧义、同一步骤复用与参数冲突、durable 缺参错误定位、完成门反馈优先级、真实 Chromium 上传只执行一次、模型漏报事实、真实旧文件冲突后重读并保留外部字段、旧 DOM ref、原快照恢复及真实 403。

预先固定 14 个原场景 × 两轮 × A/B，共 56 个实机单元，串行交替 AB/BA。每次模型请求 90 秒，每单元 300 秒，最多 10 轮；失败不自动补跑，不用补跑结果替换首轮。

```powershell
python -m tools.receipt_bound_edit_surface_probe --structured-b --repeats 2 --run-dir .e2e-artifacts/structured-surface-named-ab-20261111 --timeout 90 --budget 300 --max-turns 10
```

沿用相同任务文字、选定模型、temperature=0 和 seed=123456789。B 的工具 wire schema、自动生成接口说明、运行时校验和事实组装发生变化，完整请求不相同；固定 seed 不保证上下文不同的生成确定性。

主要指标为全部矩阵与常规 12 场景完成率，另外报告配对胜负、真实模型调用与 token、实际上传/下载次数、父调用失败、结构化入口采用与拒绝、步骤复用、首次真实工具错误及首次完成拒绝。来源 403 的通过表示正确 blocked，不算读取成功。upload_bound 与 source_failure_fixed 两个诊断独立统计。

保留 manifest、脚本/生产摘要、真实请求响应、原始与转换提案、生成程序、工具与来源回执、durable 状态、原始说明与最终交付。批次前后核对源码和脚本内容一致；任务文字比较沿用原配对审计。历史命令使用的目录已存在，重新运行必须改用新目录，脚本拒绝覆盖旧批次。

## 探索批次的入口缺口

首次探索目录 `.e2e-artifacts/structured-surface-ab-20261111` 完成 basic/A 通过、basic/B 与 structured/B 达到 max_turns；structured/A 未完成。B 仍同时允许原始 ref 和命名目标，模型选择手填 ref，并在同一计划先执行新 snapshot，使 ref 失效。该批次已明确中止，不能作为完整配对结果。目录中保留原始回执、interruption.json 与 frozen-harness 原脚本，不覆盖或替换失败。

正式批次在此负例后冻结为 structured-surface-named-v2，普通浏览器动作强制命名定位；原始 ref 静态拒绝。新增回归检查正常场景无法使用 snapshot_id，stale_ref 诊断可使用历史快照但仍必须提供命名目标。完整矩阵使用独立目录，结果另列。

## 确定性检查结果

开发阶段组合回归 92 项通过；命名目标改为必经入口之后，新模块 26 项通过（包含新增的两个入口约束测试）。Ruff 检查三个变更 Python 文件通过；strict mypy 使用 explicit-package-bases 检查两个 tools 脚本通过。未声称被导入的旧测试模块全部通过 strict mypy。

缺少 browser_wait.timeout_ms 的配对回归使用真实 durable 内核：A 保留“recovery did not consume its recorded control flow”，实验预校验保留具体 timeout_ms 错误，两者均无已入场子效果。完成门配对回归在原始正文缺少成功父回执时，A 优先报告 output_format_invalid，B 优先报告 tool_parent_missing；两者均继续拒绝完成，未放宽检查。

真实 Chromium 与脚本化 HTTP 测试还确认：模型完全漏写精确事实时，原始回执可正确交付；相同上传步骤计划两次提交只选择一次附件；上传 JSON 的正文来自原始 research_observation.text；旧编辑凭证真实冲突后重新读取，保留外部字段再成功写入。这些是机制可行性证据，不替代自主模型 A/B 结果。

pytest 退出码为 0，仍出现已有 RAM-disk 清理路径告警；未修改该基础设施。冻结批次保存七份运行脚本及新测试文件副本，实机期间不改运行脚本。

实机结束后，将“同计划重读后以预先固定的全文删除外部字段”补成确定性负例，新模块最终 **27 项通过**。该项通过表示成功复现原型的失败，不表示字段保留已修复；正例仍要求重读结果先交付模型，再单独提交保留字段的编辑。最终测试文件的 Ruff 通过，两个 tools 文件此后未变。未修改或宣称修复旧 versioned_content_edit 两项提示文案失败。

## 固定矩阵实机结果

目录 `.e2e-artifacts/structured-surface-named-ab-20261111/`，版本 structured-surface-named-v2。模型为 `koboldcpp/Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M`，temperature=0、seed=123456789。完整控制器执行了 56 个单元，无补跑替换；55 个有完整 worker summary，`02-legacy_structured/B` 达到 300 秒预算被拥有它的进程树管理器终止，仅保留部分日志和 worker-failure.json。控制器退出码 1 表示验收失败，未把中止单元算通过。

| 范围 | A | B |
| --- | --- | --- |
| 全部 14 场景、两轮 | 17/28 | 13/28 |
| 排除 upload_bound / source_failure_fixed 两个诊断 | 15/24 | 11/24 |
| 排除首轮 basic 的模型服务异常整对 | 16/27 | 13/27 |
| 仅原配对审计完整的 27 对，排除第二轮 legacy | 16/27 | 13/27 |
| 同时排除上述两对，描述性敏感性对照 | 15/26 | 13/26 |

配对胜负：A 独自通过 7 对、B 独自通过 3 对、双方通过 10 对、双方失败 8 对。三项 B 独自通过为首轮 source_failure、第二轮 research / download。首轮 basic/B 的服务异常与第二轮 legacy/B 的预算失败仍保留在主要统计中。小样本和组合干预不能证明稳定差异，也不能将新版 B 的负面总分归因于每个局部机制。

括号为真实模型请求次数；`done` 后验收失败明确标注。403 的通过表示真实尝试后正确 blocked，不是成功读取。

| 场景 | 第一轮 A | 第一轮 B | 第二轮 A | 第二轮 B |
| --- | --- | --- | --- | --- |
| basic | 通过（6） | 服务异常（1） | 通过（3） | 通过（2） |
| structured | no_progress（4） | max_turns（10） | done，漏实际数值（4） | no_progress（9） |
| legacy_structured | 通过（3） | max_turns（10） | 通过（4） | 300 秒预算中止（10*） |
| hybrid | no_progress（6） | no_progress（7） | no_progress（6） | no_progress（4） |
| research | 通过（4） | 通过（2） | no_progress（5） | 通过（2） |
| snapshot_restore | 通过（3） | 通过（2） | 通过（3） | 通过（2） |
| reread | 通过（3） | 通过（2） | 通过（5） | 通过（2） |
| stale_ref | 通过（5） | done，未做旧引用（2） | no_progress（6） | done，未做旧引用（2） |
| stale_edit | 通过（8） | done，外部字段丢失（3） | no_progress（9） | done，外部字段丢失（3） |
| upload | 通过（4） | max_turns（10） | 通过（3） | max_turns（10） |
| download | 通过（3） | 通过（2） | max_turns（10） | 通过（2） |
| source_failure | no_progress（3） | blocked（2） | blocked（2） | blocked（2） |
| upload_bound，诊断 | no_progress（6） | max_turns（10） | no_progress（6） | max_turns（10） |
| source_failure_fixed，诊断 | blocked（1） | blocked（1） | blocked（1） | blocked（1） |

`*` 预算中止单元的 10 次请求及 10 个响应来自原始 provider 日志，非完整 worker summary；计入审计派生的真实用量，不伪造原单元摘要或改写 pair_audit。该单元最终执行状态仍不完整。

## 全部 26 个失败单元

同类项合并列出，但每个失败单元都在下表和完整矩阵中保留；原始程序、回执和完成拒绝见对应目录及 wire-facts-audit.json。

| 单元 | 数量 | 原始失败链 |
| --- | --- | --- |
| 01-basic/B | 1 | 首次请求响应正文混入 HTTP headers，provider_failure；未重发，未执行工具。不是步骤编译或浏览器失败。 |
| 01-structured/A；02-structured/A | 2 | 首轮未读取 URL/token，完成门优先要求只修答案而 no_progress。第二轮正式 JSON 的 URL/token 正确，但 explanation 没有实际 173 / QUARTZ_851，事后事实验收失败。 |
| 01-structured/B | 1 | 将指定 URL 的连字符抄成下划线，导航得到 404；随后重复 snapshot / 填表，目标缺失，10 次 failed_step 后 max_turns。 |
| 02-structured/B | 1 | 未采用属性步骤，反复导航重置页面；复用旧填表/点击结果后等待超时，修改同 id 参数又触发冲突。完成门已指向缺 browser_eval，仍未恢复。 |
| 01-legacy_structured/B；02-legacy_structured/B | 2 | 均未采用 browser_property，以多次导航/切换标签代替读取 token；另有重复步骤 id 静态拒绝、缓存结果与当前页面状态不一致、等待超时。首轮 max_turns，第二轮 300 秒中止。 |
| 01-hybrid/A | 1 | 首次导航漏 return；后续浏览器完成但未执行编辑，多次 done 却声称已更新文件，project_changes_required 拒绝。 |
| 02-hybrid/A | 1 | 已读取文件，但 edit 漏 base_result_ref，后续未成功写入。 |
| 01-hybrid/B | 1 | 浏览器计划已返回完整正文，后续仍对正文叶回执调用 read_tool_result(view="value")；该 canonical 视图不存在，反复读取而未编辑。 |
| 02-hybrid/B | 1 | 浏览器与 read_file 成功，但未提交 edit；重复 done 被 project_changes_required 拒绝。 |
| 02-research/A | 1 | 导航漏 return，后续读取与研究提交已发生，但没有成功 browser_wait，完成门重复拒绝。 |
| 01-stale_ref/B；02-stale_ref/B | 2 | 均未传历史 snapshot_id，命名步骤实际使用新 ref；正文虽正确并 done，任务要求的真实旧引用拒绝次数为 0。 |
| 02-stale_ref/A | 1 | 首次父错误为恢复控制流错误；后续浏览器步骤已成功，最终因无效 done.knowledge_refs 反复完成拒绝，不是始终未恢复 DOM 操作。 |
| 01-stale_edit/B；02-stale_edit/B | 2 | 各一次有效旧凭证真实冲突；随后同计划“新读取＋预先固定全文”以新凭证成功写入 173 / QUARTZ_851，却都删除 external_revision。新凭证和重读指标为真，整任务仍失败。 |
| 02-stale_edit/A | 1 | 导航返回及编译错误后，编辑先漏凭证，再从 unwrap(readResult).result_ref 取 undefined；真实版本冲突为 0，外部修改未被覆盖。 |
| 01-upload/B；02-upload/B；01-upload_bound/B；02-upload_bound/B | 4 | 40 个上传步骤都把控件名称写成文件名 fixture.txt，真实页面控件名为 Attachment；零匹配而停止。真实 browser_upload 次数全部为 0，未到达上传正文绑定。 |
| 02-download/A | 1 | 首次直接调用无 tools. 前缀的函数；后续把可访问名称 Download fixture 当作 ref，九次实际下载调用均被旧引用保护拒绝，真实成功下载为 0。 |
| 01-source_failure/A | 1 | 三次把 open_url 当作 run 的 command，并误写 URL 路径；原项目 guard 均拒绝，未执行真正的来源工具，最终 no_progress。 |
| 01-upload_bound/A；02-upload_bound/A | 2 | 先有 Result 解包/父返回错误，随后各已有一次真实上传及成功父调用下的读取；生产绑定仍取不到 research_observation.text，缺 page_text，却优先反馈只修答案，最终 no_progress。 |

## 局部机制、采用情况与成本

**机械错误移出，不等于任务完成。** A 原始响应提出 86 个 CodeMode JS 程序；B 提出 0 个模型 JS，94 个计划编译、3 个静态拒绝。B 的 3 个失败父调用均来自静态拒绝，但已结算父回执中另有 **74 次 failed_step**：控件名 fixture.txt 零匹配 40、错误 URL 后 Locality 缺失 10、等待超时 14、同 id 参数冲突 7、预期文件版本冲突 2、HTTP 403 停步 1。A 的失败父调用为 36。返回层形状不同，不能用 3 对 36 宣称任务稳定性提高。

**编辑绑定有效，但字段保留倒退。** 两轮 B 都有真实冲突和新凭证恢复，只有 3 次模型调用；但两轮都删除外部字段。A 首轮 8 次调用正确保留字段，第二轮卡在凭证装配。这证明“凭证有效”“写入数值正确”“保留外部字段”必须独立验收。新增确定性负例复现了同计划读/写的问题，未修改原型去掩盖实机失败。

**没有实机副作用去重收益证据。** A 成功上传 4 次，恰为两个普通上传和两个绑定诊断各一次；B 为 0，原因是从未定位到上传控件。A 成功下载 1 次，B 2 次，分别为已通过单元各一次。B 的 44 次缓存复用全部是导航 16、填表 14、点击 14；没有上传、下载或编辑复用。不能把 B 未执行副作用算作安全完成。重复提交同一上传计划仅执行一次的能力仍只由确定性真实 Chromium 测试证明；缓存状态与页面重置冲突则有实机负例。

**属性入口在主矩阵未采用。** 97 个原始结构化提案（含拒绝）未出现 browser_property；真实 wire schema 和契约均包含该入口。stale_ref 专用 snapshot_id 两轮也未采用。命名动作本身已经成为必经接口，但字段含义、操作发现、历史快照选择和更高层任务规划仍留给模型。

**事实交付存在，但本批未挽救模型遗漏。** 16 次事实报告准备、11 个最终文本交付，其中 7 个验收通过、4 个因旧引用或外部字段问题失败。24 项准备阶段选值（最终交付涉及 19 项）均回溯到成功父调用下的原始叶回执。成功文本单元的模型原始说明已经含任务所验的实际数值，下载原说明也含文件名/hash；未观察到仅靠附加事实挽救遗漏的实机正例。B 的 structured / upload_bound 均未完成，实机成功 JSON 绑定字段为 0；正文绑定可行性只由确定性正例支持。自由说明语义未被全面校验，附加正文不能修复错误编辑或未执行的诊断。

**少数场景少调用。** B 的两个 Research、两个快照、两个重复读取及两个下载均通过，各 2 次调用；对应 A 有通过，也有未完成。B 自主 source_failure 两轮均真实 403 后 blocked，各 2 次；没有脚本前置来源读取。它们是本批局部正例，不能抵消其他失败来宣布整体收益。

| 指标 | A | B |
| --- | --- | --- |
| 真实矩阵模型请求 | 126 | 125（含预算中止日志的 10 次） |
| 已记录输入 token | 1,044,241 | ≥1,089,818 |
| 已记录输出 token | 23,065 | ≥30,408 |
| 有完整用量记录的单元 | 28 | 27（含预算中止日志；服务异常单元缺用量） |
| 有完整 worker summary 的单元 | 28 | 27 |

B 的 token 为已知下界，服务异常请求不按零用量处理。已知输入、输出都高于 A，而调用只少一次、成功少四个，不支持成本收益。A 28 个完整单元内部耗时合计 1,450.486 秒；B 27 个完整单元为 1,607.297 秒，另有一个 300 秒预算中止。两种耗时范围不混为统一平均或节省百分比。

## 单列的 schema 分支诊断

矩阵结束后，另作 **2 次 provider 请求，不执行任何返回工具，不计入 251 次矩阵请求或 56 单元**。保留原完整联合 schema，与仅保留 browser_property 步骤分支的 schema 比较；两者使用相同最小提示、模型、seed、temperature 和期望 JSON，明确要求返回一条 `browser_property(name="__contractToken")`。

两次均逐项匹配期望 JSON，各输入 69、输出 32 token。原 schema 在该最小上下文中可以选中属性分支，故不能把主矩阵的零采用解释为该分支始终被解码器屏蔽。它不重现复杂任务上下文，也不能单独区分长契约、说明丢失、上下文与规划之间的贡献。日志、脚本摘要和请求/响应保留在 `schema-choice-probe/`，脚本为同批目录中的 schema_choice_probe.py。

## 审计与提交范围

28 对任务文字相同；27 对有原配对审计要求的完整两臂摘要及源码核对。第二轮 legacy/B 缺最终摘要，原 pair_audit.comparable=false 保留，不人为改为通过。批次及所有完整单元前后生产摘要均为 `0686d61cd4fbd923cfb089a89abc0f772a9331366c8152a9c756b49a118ca4b1`，七份运行脚本摘要全程不变。251 次真实矩阵请求逐份检查了 seed、temperature，以及 A 的 code / B 的 steps wire 形状；B 的动作 schema 均不暴露原始 ref，历史 snapshot_id 只在 stale_ref 场景暴露。

原始 summary.json 不补写超时单元的指标；审计在 wire-facts-audit.json 单列从部分日志派生的指标与限制。保持原请求响应、转换提案、正式回执、状态库、首次工具失败及首次完成拒绝。探索批次和正式批次分开保存，无绿色补跑替换。原实验的内存补丁仅作用于 worker，未改生产服务、源码、模板或用户配置。

用户要求本轮结束就 commit；完成报告、最终检查后提交 tools / tests / docs 并停止，不再启动矩阵、不改生产代码、不创建 worktree。当前结果支持继续修小范围确定缺陷，**不支持直接推广这版组合**。

## 由负例约束的修改方向（本批未实施）

1. **先修读取与全文写入之间的数据依赖。** 当前静态计划允许同一计划先 read_file 再 edit，但 content 在模型发计划前已固定。程序取得刚读出的新凭证，不代表模型根据新正文计算过 content。第一轮 stale_edit/B 已表现为真实冲突、重读、新凭证写入均成功，却删除 external_revision。最小改法是全文 edit 只能引用已经交付模型的先前计划读取；读后先交付观察，再接收写入。若未来增加局部更新，也必须明确保留未改字段及版本前提，不能暗中读新版本给旧全文换凭证。
2. **补回操作语义，统一接口与错误契约，减少字段歧义。** 生成步骤 schema 时应保留逐项工具说明及结果形状；当前代码只拿参数，并清空旧 declarations。静态拒绝通过固定 throw 程序进入原内核后，反馈仍追加旧 code/JS 契约，与 wire 的 steps 契约冲突；应保留原错误和回执，同时按真正的模型入口给出修复契约。浏览器动作宜使用含明确说明的 target_name，表示快照控件的可访问名称，避免上传时将文件名当目标。零匹配和多匹配应分别报告，并带回受限的真实候选名称/角色和快照身份。不能只增加另一套可选助手。这些接口缺口的独立效应尚未隔离。
3. **用明确恢复状态管理已完成动作。** 本版同 id 同参数缓存只证明复用那次结果，不证明当前浏览器仍处于那次动作之后的状态。第一轮 legacy/B 多次导航重置页面后复用原填表/点击，等待超时。后续应给出已完成回执及显式续跑边界，并检查动作依赖的页面/文件状态；不能把所有变更步骤按名字永久跳过，也不能自动重复上传等副作用。工具结算到状态写入之间的崩溃窗口仍需单独设计和验证。
4. **统一模型看到的数据与结果恢复入口。** 当前计划返回嵌套 Result 和回执，其他模型工具仍保留原契约。第一轮 hybrid/B 在完整正文已经交付后，又对正文叶回执使用 read_tool_result(view="value")，反复失败。后续应提供简洁、按操作声明的数据投影及明确可读视图，避免把另一层类型选择重新留给模型；保留原始来源边界和正式回执，不伪造 canonical.value。
5. **把局部缺陷分开验证。** 入场参数校验、正文选择器、完成门反馈优先级已经有确定性配对反例。宜先对这些小范围机制独立验证，再测计划接口、结果投影与恢复状态。当前 B 是组合，不能用总分判定每项单独无效；也不能只用 JS 错误减少来宣布端到端收益。属性操作和 stale-ref 历史快照未被模型采用时，要保留采用失败，并另行区分接口说明与本地受限解码行为，不能直接断言为模型能力原因。

以上只是分析建议。生产代码、生产模板和冻结实机脚本均未据此修改。
