# 编码与研究组合任务：减负链路、原始失败和修复

日期：2026-11-21，Asia/Taipei。实施基线：`95d205c1`。没有创建 worktree。

两类固定自然任务本轮均完成操作闭环；原 21 个研究测试失败已解决，全生产源码和本轮修改文件的三种类型检查通过。**全部历史测试/工具的扩大类型检查仍未清零，不能称为本轮全部要求已完成。** 首败及各次重跑完整保留。

## 根因与旧结论更正

问题同时包含确定性的程序缺陷、减负入口的接入问题和模型错误，不能只选其中一种解释。

上轮报告称第九、十次实机“实际请求已提供自动复制，模型跳过”，该结论撤回。重新合并 HTTP `response_format.json_schema.schema.oneOf[*].allOf` 中并列的 tool/args clauses 后，实际 codemode 仍要求 `code`，edit 仍包含 content/base_result_ref。提示里出现 path-only 示例不证明该轮已经绑定。账本只有查询前的完整正文“尚未加载记录”，没有字段，因此固定复制正确地没有激活；先前证据核查不充分。

本轮以实际 wire schema、模型原始响应、原 effect 回执、最终文件和完成证明核对，不把基础 snapshot 或提示示例当成发送合同。

## 确定性的修复

1. **查询动作与正式来源之间断开。** 模型选择 click purpose=controls 后只有快照，既有交接没有可执行的正式读取建议。现在在已派发点击缺少正式读取时给出 browser_read，仍允许继续其他控件操作；不把点击或页面变化认证为查询完成。
2. **固定复制的语义入口不合适。** 既有 path-only 绑定临时改变 codemode 的参数，普通 edit 仍要求抄文件回执。来源充分时现在直接提供 `edit(path)`，降低为原固定 CodeMode 程序：完整 read_file → 外层 result_ref → 原版本化 edit。自由 CodeMode 继续要求 code；历史 codemode 捕获仍能恢复。没有第二套编辑器或运行时。
3. **父任务落后的工作区身份造成假失败。** 实机已编辑、检查 exit 0，同 fingerprint 下父任务 revision=2、子回执 revision=3，完成门判验证过期。restore_facts 现在读取原 WorkspaceRevisionStore 当前身份，同步父 session 与既有 execution evidence。回归同时验证：检查后再外改文件，旧 proof 仍失效；没有降低 revision/fingerprint 匹配。
4. **条件来源被提前访问，破坏任务顺序。** “先检查，需核对时打开 URL”原先在第一模型轮前自动预读。WebIntent 现在派生 deferred_urls，抑制条件来源的抢先 prefetch；开始浏览前的交接明确后续来源义务不能取代用户的前置步骤。授权、required URLs、正式来源要求保持原身份；模型仍可主动打开来源。
5. **缺字段后的恢复太晚。** 原恢复只处理缺少显式 fill 回执，已有动作后就不再提供填写选择。现在在当前文档的正式读取缺字段且已有填写时，立即复用原冻结 textbox 选择和上次实际输入，提示核对用户原拼写；重复观察时也提供同一恢复。未知效果或错误文档不能重放，不自动选城市、不自动翻译或填值。
6. **完成阶段仍存在多余抄写。** whole-answer 的来源/保存证据版本由原提交报告解析，done 不再要求第二份 knowledge_refs；普通任务的保存知识引用合同保留。模型把 eN 填成保存知识 ID 的历史请求仍按原门禁拒绝。
7. **可选执行声明改为真实候选。** 模型曾把回执 status=exit 抄成 success。现在使用原 execution_receipt 和 observed_claim 生成完整枚举选项，模型选择真实元组，不能拼错 ref/status/scope/exit_code。原校验器仍拒绝错误声明，省略声明也仍检查真实执行回执；没有自动改正错误输出。
8. **文本模式遗漏完成修复规则。** 原 _completion_followup 计算了 whole-submission 指令，却没有加入文本 transport。现在两个 transport 都明确：缺失或无效报告用 submit_research_answer，已有效报告保留，完成项目工作与验证后再 done。
9. **对象枚举的持久化渲染不稳定。** 新的真实回执枚举暴露既有 renderer 依赖字典插入顺序，durable JSON 排序后误判合同不一致。枚举对象现在稳定排序渲染；新测试经过真实 JSON 序列化再恢复，并验证错误 status 仍拒绝。
10. **错误地址后没有复用免抄 URL 选择器。** CodeMode 原本支持 browser_navigate(target="task")，但错误页面的交接只显示文字地址。现在提供已冻结 task target 的原调用模板，由既有 host 解析成任务原 URL；其他授权导航保留，不自动纠正或替换模型提交的 URL。

以上复用 TaskPolicy、TaskSession、ToolSpec、冻结请求、原 CodeMode、workspace revision store 和 effect receipts。没有扩大命令许可、修改来源选择器、接受过期 ref、双写 settlement 或新增 runtime。

## 原有机制是否实际使用

| 机制 | 本轮核查 |
| --- | --- |
| 语义控件选择、角色区分 | 实际 fill/click 参数接受原冻结 role/name 候选，driver 仍复核节点 |
| fill 自动观察、click 固定 follow-up | 继续产生原子与父回执，已派发动作不因观察失败而重放 |
| 字段自动复制 | 仅完整正式来源满足声明字段后激活；最新自然组合任务实际 schema 提供 edit(path)，实际走原读/改程序 |
| 完整结果区域、文件版本检查 | 原测试继续覆盖截断、范围不足、错误文档、并发版本冲突和恢复 |
| 报告字段槽、保存证据引用 | 仍从原 scope 和提交报告导出；最终完成不另抄一套 eN/knowledge ID |
| 原始执行回执 | 进程状态、退出码和引用来自同一解析；新冻结候选不是新事实来源 |

不应把“函数仍存在”说成“每次实机已经启用”。来源不足时不能提供复制绑定；模型填错查询、编造字段或提交错误回执时仍应拒绝。

## 原 21 个研究测试失败

已逐项修复并保留首败。FakeSearch 原继承真实 BrowserSearchProvider，任务资源绑定替换了 fake，预设搜索没有运行；改为满足原 provider 协议的独立 fake。旧隔离断言迁到统一授权合同，取消/空正文后的观察页由 task owner 在终态清理；anchor fallback 用真实 Chromium main 区域验证。tests/test_research.py 原 153 个测试函数名称全部保留，没有删掉失败覆盖或恢复旧授权隔离。

研究迭代 adapter 改用已有 typed kernel fixture 与生产 ResearchFlowDeps，去除未类型化 adapter 依赖。整个修改测试文件补齐参数、返回值、JSON/可空域值与 HTTP/PDF fixture 类型。ty 还发现 HTTP log_message 覆盖参数名不兼容，已修正。

扩大回归还发现脚本模拟了生成 API，却漏掉新增模型元数据探测，访问虚构 scripted.test。只补齐 scripted provider 安装函数的元数据模拟；不修改生产网络合同。较慢的 38 项浏览器集成批次持续推进，保存了 py-spy 栈，最终 673.38 秒通过，不能把正常执行时间称作死锁。

## 失败记录与自然任务

本轮全部目录使用 `.e2e-artifacts/unified-repair-*`，原先 `.e2e-artifacts/unified-*-natural-*` 等目录不改写。模型保持 Gemma4-12B 同一实例，seed=123456789、temperature=0、max_turns=14，用户任务和网页 fixture 未改变。

| 记录 | 结果和归因 |
| --- | --- |
| composition-first | no_progress；缺查询后正式读取，自动复制未激活 |
| composition-second | 报告、实际 edit、文件和检查已正确；父子 revision 不同导致完成假失败，程序缺陷 |
| composition-third/fourth | 模型把“福州”填为 Fuzhou，未取得字段；模型输出错误，不能改 fixture 接受翻译 |
| repair-first | kernel done、文件与检查正确，但抢先研究，未满足先失败再调查的自然顺序；验收失败 |
| repair-second | 正确先检查失败再研究，文件和检查正确；最终把 eN 用作 knowledge_refs，被拒绝，max_turns |
| composition-fifth | 报告、自动 edit(path)、检查均正确；模型重复 status=success，实际回执 status=exit，未闭环 |
| composition-sixth | 自动复制与检查正确；对象枚举持久化渲染不稳定导致 provider_failure，本轮引入候选后暴露的确定性问题，已修复 |
| composition-seventh | Fuzhou 后编造 500/F123456，编辑被拒；检查仍失败，未闭环。没有为此放宽来源或编辑合同 |
| composition-eighth | **自然闭环通过**，8 轮：报告先于实际编辑，173/QUARTZ_851，keep 保留，unittest exit 0，完成门接受 |
| repair-third | kernel done、文件与检查正确，但模型仍先研究、后首次失败检查；自然顺序失败，不能计为通过。报告中还出现 code 原值和句子不完整的模型撰写错误，未据此修改生产语义合同 |
| repair-fourth | 模型把地址 production-browser-record 抄成 production_browser_record，实际收到 404，来源和编辑失败；不是正确目标网站的环境故障。自然顺序也未满足，未闭环 |
| repair-fifth | **自然操作闭环通过**，12 轮：list_dir → 首次 unittest 失败 → 打开原 URL → 查询福州 → 正式读取 → edit(path) → unittest exit 0 → 报告 → done；两个字段正确，keep 保留 |

后续通过不能抵消以上失败，也不能推出模型在任意网站或措辞下都可靠。

两次通过的判据都是 probe 原有的 natural_task_closed，核对的是各自原任务顺序、文件、检查和已接受完成。基础 browser gate 的 passed=false 仍保留：它还要求固定浏览器步骤清单中的 browser_wait 等，不是这两个未要求固定工具顺序的自然任务判据。没有修改这些自然目标来取得绿色。

修复第五次的最终摘要和带引文数据段包含正确事实、修复与检查结果，但提交报告末尾仍有一个未写完的句子。原 API 回复本身如此（finish_reason=stop），不是应用截断，也未自动续写或篡改。操作闭环和报告文字质量是不同结论，这项模型写作缺陷保留为限制。

## 相关回归与类型验证

未运行全量 pytest。批次存在重叠，不能把各批次数相加当成互不重复覆盖。

| 验证 | 结果 |
| --- | --- |
| 原研究测试、项目验证、固定复制核心批次 | 239 passed，4 subtests passed；之前的 21 个旧失败已解决 |
| 合并扩大批次（研究、版本、复制、交接、冻结合同、CodeMode 恢复等） | 410 passed，4 subtests passed，288.87 秒 |
| 研究提交、直接浏览器步骤、动态查询生产批次 | 38 passed，673.38 秒 |
| 执行声明、原回执恢复和递归 schema 验证 | 56 passed，5.67 秒 |
| 缺字段输入恢复与报告交付专项 | 71 passed；新增条件来源交接、免抄 URL 恢复及交付专项 13 passed |
| 浏览器填写、固定 follow-up 与失败读取恢复的生产入口 | 7 passed，8 deselected，80.29 秒 |
| 最终交接、报告交付与提示归属 | 39 passed，3.80 秒 |

本轮首败包含固定程序识别遗漏 FrozenMapping/补全的 language、旧 legacy parent-throw 用例、错误 inventory 顺序断言、HTTP override 参数名、测试传入未 decode 的文本、测试导入名错误、对象枚举未声明 type 等。分别修复实现或测试，日志均保留，不用绿色重跑抹掉首败。

全树 Ruff 已通过。整个生产源码 `codey` 的 mypy、pyrefly、ty 以 Python 3.11 为目标通过；本轮修改 Python 文件的三种检查也通过，AST precision 检查覆盖修改整文件，没有 Any、cast、type-ignore、裸泛型或未标注函数。原历史 checker suppression/warning 不等于本轮新增豁免。

**扩大到 `codey tests tools` 的类型检查仍失败：mypy 18,072 errors / 1,268 files（检查 2,560 文件），pyrefly 21,171 errors，ty 4,949 diagnostics。** 这些输出保留，不能把生产源码/修改文件的绿色说成全仓测试工具都已迁移。本轮没有为清零数字而关闭检查、添加忽略规则或改造上千个无关历史文件；这一扩大要求仍有剩余工作。

自然任务的项目检查回执和文件均直接核对，但 UI 的完整性监控仍显示 `verification limited / monitoring incomplete`，不把本次已执行检查说成完整环境监控或任意项目验证覆盖。真实网站登录、任意网站成功、其他系统与模型行为可靠性也不在本轮通过范围内。

原始请求、响应、回执、首败和重跑日志及源码 SHA256 见[机器验证清单](data/codey-unified-composition-repair-validation-20261121.json)。原 `.e2e-artifacts` 保存本地证据；清单不以摘要取代原始材料。

## 验证范围与限制

未运行全量 pytest。各批次有重叠，不将数量相加当作独立覆盖。最终结果、原始日志和 SHA256 以[机器清单](data/codey-unified-composition-repair-validation-20261121.json)为准。

- 全树 Ruff 已通过；完整生产源码 codey 的 mypy、pyrefly、ty 已通过，Python 3.11，mypy 检查 734 个文件。
- 本轮 23 个 Python 文件三种类型检查均通过。整文件 AST 检查没有 Any、cast、未注解函数、开放 callback 参数或新增忽略规则；未修改检查配置或增加豁免。
- 扩大至 codey/tests/tools 仍未通过：mypy 18072 errors /1268 files（2560 文件），pyrefly 21171 errors，ty 4949 diagnostics。错误在历史测试/工具，不能称为全仓类型清零；本轮只迁移与任务及验证相关文件，没有改动 1268 个历史文件来扩大重构。
- 实机是受控页面、真实 Chromium/worker、真实本地模型及生产入口/kernel。通过不证明真实网站人工登录、任意来源可达、其他模型或操作系统可靠。
- 完成事件仍如实标记 verification trust=limited、完整性监控 unobserved；实际 unittest 成功与工作区证明不等于所有运行环境监控均已认证。
