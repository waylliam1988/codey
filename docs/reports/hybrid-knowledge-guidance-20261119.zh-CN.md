# Hybrid 知识引用提示与错误完成提议恢复

## 问题和修改边界

旧 `stale_edit` 实机中，网页正文正确，首次编辑按预期遇到版本冲突，重新读取后的编辑也已经成功；模型最后却把输出文件 `result.json` 填成 `done.knowledge_refs` 的知识对象。三次错误完成提议使任务停止。原始案例及比较见 [上轮记录](browser-observation-progress-20261118.zh-CN.md)。

本轮在原目录修改，没有创建或复制 worktree。保留非严格 Hybrid 的原有合法引用规则：没有本任务 `knowledge_read` 回执，不等于没有合法知识引用。旧笔记关联候选和新保存对象不能冒充直接读取，成功保存也不代表自动采用。

改动包括：

- `chat_knowledge.py` / `kernel_protocol.py`：非严格 Hybrid 复用现有正式读取回执投影，在冻结参数说明中展示完整的 ID／版本对，说明这只是非穷尽候选、由模型选择实际采用的知识。兼容路径保留原可选字段，不把不完整列表变成封闭枚举。强制知识校验、结构化解释和严格 Research 保留各自合同。
- `answer_refs.py`：现有校验在拒绝时保留实际出错的知识引用，供反馈使用；清除该文件已有显式和隐式 Any。存在性、访问范围、精确版本和旧答案限制不放宽，不改用最新版本。
- `research_completion_checks.py`：最终校验和纠错调用同一个引用解析入口。错误反馈说明被拒绝的精确 ID／版本及校验原因；仅当该 ID 确实出现在当前任务的输出／读取路径记录中，才补充路径身份。没有按文件后缀推断，合法的 `.json` 知识对象仍可引用。无正式读取时说明这一事实，同时说明其他合法知识仍可能存在。仅在非严格、无强制知识校验的 Hybrid 中，条件式建议未使用保存知识时省略可选引用；提醒只修正完成提议。

没有静默删除模型的错误参数，没有自动采用保存对象，没有新候选账本，也没有改写浏览器执行、文件读取凭据、版本冲突或既有真实执行回执兼容逻辑。

## 回归与类型检查

新增回归覆盖：正式读取／搜索／失败／未提交／错配回执，native 与文本参数展示及快照恢复；合法非本任务读取对象、旧笔记关联候选、刚保存对象；合法文件名形式 ID、错误版本、权限拒绝、旧答案和畸形参数；强制引用反馈；生产 Chromium + headless 入口完成提议纠错且不重复动作。

首次聚焦检查为 24 failed / 36 passed。20 项因新增测试来源漏了必需的 provenance URL；2 项因测试前缀把来源采集和文件写入放在同一程序，被生产的先交付证据合同拒绝。修正测试准备代码，复用现有固定字段复制输入，保留正式版本冲突和重读。下一轮 13 failed / 12 passed 是测试把冻结 schema 内部元组当成外部 JSON 解码；改用既有 canonical 解码后，新增 25 项通过。

另外 2 项旧 native Chat 测试把整个工具说明中的 `knowledge_refs` 字样当成字段仍然存在。用 HEAD 原模块在独立进程复核，同样 2 failed；实际参数已经没有该字段。测试改为检查真正的参数结构。强化类型检查还修正这份测试的 connector 签名，以及实机 gate 的 `model_selection` 字典注解，均不改变实机行为。

聚焦原 4 模块最终为 60 passed。首次扩大 27 模块为 562 passed / 7 failed；在 HEAD 的原四个生产模块上，7 项全部复现（独立进程通过 git show 加载旧模块，没有复制源码树）。用户随后明确要求扩大并修好这些测试，修复与最终扩大结果见下节。检查日志和首次失败请求／回执保留在 `D:/codey-local-evidence/hybrid-knowledge-guidance-20261119/`，没有运行全量 pytest。

## 追加：修复旧测试并扩大回归

这 7 项不是本轮知识引用修改引入的失败，但测试长期不符合当前生产接口也会掩盖后续回退，因此按用户追加要求修复。只更新测试替身和断言，没有为通过测试修改生产接口。

| 原失败位置 | 确认原因 | 测试修复及新增对照 |
| --- | --- | --- |
| Hybrid completion research projection：2 项 | 不完整替身缺少当前字段、evidence；假 Changes 不属于正式 KnowledgeChanges | 使用正式 RunFrame / RunWork / ResearchTools / KnowledgeChanges 和 headless 状态；验证有变更才登记可恢复记录，空 tracker 不登记 |
| Research payload restore availability：1 项 | 假结果缺 semantic_review 等当前结果字段 | 使用正式 ResearchRunResult；可恢复 true / false 都保留 |
| Hybrid outcome projection：2 项 | tools 缺 changes / synthesis_id；frame / work / session 替身不完整 | 使用正式领域对象；done / no_progress / max_turns 均保留原裁决，保留元数据且不修改原 event |
| 知识生命周期：1 项 | patch 旧模块的类没有拦到工厂已导入的别名，意外走真实联网 | 从当前来源工厂注入正式 BrowserSearchProvider 的搜索／读取替身；复用既有 scripted API；保留采集、保存、重启回忆、推断、修订、固定旧依赖和图追踪；native / 文本均运行 |
| 无知识库联网 headless：1 项 | 按旧入口 mock，并禁止当前基础浏览器资源创建 | 使用当前来源工厂及正式 provider；Chat / Hybrid × native / 文本各验证搜索→阅读→完成，仍不创建 vault |

追加修复涉及 4 个测试模块：最初 13 passed / 3 failed，暴露新测试替身不是正式浏览器资源；修正继承边界后仍有 3 项因实机 provider 证据目录未创建而 provider_failure，随后补齐目录，16 passed。扩大 native 与来源失败对照后出现 3 项失败：测试误把 KnowledgeObject 交给只收 canonical JSON 的解码器，另有测试用了不存在的 http_error 失败码。改用正式 transport 编码器和 access_restricted 失败码，最终 **22 passed**。所有失败日志和原始请求／回执保留，没有修改生产合同适配错误测试。另在独立进程加载修改前 HEAD 的四个生产模块，修复后的同一组 **22 项也全部通过**（`repaired-tests-head-baseline.log`）；旧测试失败与本轮生产修改没有因果依赖。

额外负例确认：403、空正文和无效结构状态都不能进入已读来源集合或打开来源账本，无知识库时也一样。测试 DNS 使用既有 public_source_dns，搜索／正文由替身控制，避免把公网波动当成生产回归。

回归范围从 27 个模块扩大至 **37 个模块**，另加当前来源工厂归属、source gateway、用户指定来源阅读要求、Chat 联网完成政策、Web/Responses 授权一致性、只读知识提示、SQLite 权威存储、首轮知识、完成输入与完成检查归属。

最终扩大结果：**665 passed，37 个模块，942.81 秒，退出码 0**。命令和逐模块范围保存在 `regression-expanded-repaired-command.json`；没有运行全量 pytest。退出时另有 pytest RAM 临时目录清理的 WinError 3（路径已不存在）警告，完整日志保留；这属于测试临时目录清理告警，未作为用例失败，也未隐去。

本轮 12 个修改 Python 文件通过额外的 `disallow_any_expr` / `disallow_any_explicit` 检查，不使用 cast、type: ignore 或新增忽略。生产 mypy（674 文件）、Pyrefly（0 errors；35 个已有 suppressed、165 个 warning 未显示）、ty 和 kernel precision gate 全部通过。全树 Ruff：**通过**。中途全树检查曾发现并发新增的无关 `tools/agent_work_graph_probe.py:305` 存在 1 项 SIM105，保留当时日志；未改该文件，最终全树复查已通过（`ruff-final-recheck.log`）。

追加修复日志在 `D:/codey-local-evidence/hybrid-knowledge-guidance-20261119/`。失败请求／回执另存短路径 `D:/codey-local-evidence/hkg19/test-repair-wire/index.json`，附原路径与 SHA-256，避免 Windows 长路径丢失证据。

## 冻结与实机方法

生产源码冻结摘要：`5d18fa1264cc9f35a70b87036068edf561fa03e7a0eb7865184e34261771fd9e`。逐文件哈希及本轮生产补丁在 `D:/codey-local-evidence/hkg19/source-freeze.json`、`frozen-production.patch`；没有复制源码树。真实模型与 Chromium 串行运行，使用已有生产工具、冻结快照、headless 内核、完成校验和 durable 回执。

实机使用本地 `koboldcpp/Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M`，温度 0，seed 123456789。固定前缀与自然生成分别计数，不把复跑或恢复成功计作首轮成功。

## 旧错误完成提议的真实模型恢复

`hybrid_knowledge_recovery_live_gate.py` 从保留的旧 `stale_edit/proposals.jsonl` 抽取原始错误完成提议，夹具记录原文件路径及 SHA-256。4 次明确脚本回复依次准备：读取真实页面、固定字段复制遭遇版本冲突、重新完整读文件并复制成功、原样提交旧错误 done。来源、实际文件写入及冲突都通过生产入口执行；没有伪造成功回执。

首轮恢复 **通过**。下一次真实模型请求确实包含针对性错误说明及 Hybrid 参数提示。模型在 1 次真实生成后，保留原 summary，省略无关 `knowledge_refs` 并完成，没有新增工具调用。

| 正式调用 | 次数 |
| --- | --- |
| 导航 / 填写 / 点击 / 等待 / 正文读取 | 各 1 |
| 快照 | 2 |
| 文件读取 | 2 |
| 编辑 | 2：1 次预期版本冲突拒绝，1 次成功 |
| 成功编辑之后的浏览器／文件调用 | 0 |

续跑位置：零起始正式回执序号 14，成功 edit 之后。`recovery-first/continuation-summary.json` 及 `raw-recovery-audit.json` 核实原始请求、错误提议、最终提议、正确文件和实际动作次数。真实请求输入 12611 tokens，输出 121 tokens；没有做成本或成功率的配对结论。

## 验证范围和剩余限制

完整原 17 项 full-surface 首轮 **15/17**，原始结果为 `full-first/summary.json`，全部正式失败回执另存 `all-receipt-failures.json`。保留失败，不合并成全绿：

- `codemode`：三次生成完全相同的循环，找到 Locality 后提前 break，未检查后面的 Load record，只有快照回执，最终 no_progress。第三次实际请求已提供 observation recovery 交接，模型仍重复。程序 hash、交接所在请求和正式回执见 `codemode/first-failure-audit.json`。没有进入知识引用纠错阶段，不能通过改知识合同修复；没有完整请求的反事实配对，不能断言模型输出变化的唯一触发因素。
- `stale_ref`：业务查询正确完成，但模型未执行任务明确要求的“故意用旧 ref 一次”负例，failed_stale_refs=0。实际请求包含该要求，见 `stale_ref/first-failure-audit.json`；业务完成不替代负例覆盖。单独的 stale_ref_injected 用例通过，正式拒绝及恢复正常。
- 自然 `stale_edit` 通过：一次正式版本冲突后重读并成功写入。但模型仍把真实成功执行回执 ID 放入 knowledge_refs，经既有兼容规则通过；不能因此宣称模型已完全分清文件、执行回执与知识身份。原完成提议见 `stale_edit/natural-completion-audit.json`。
- `research_codemode` 最终通过，但中途保留错误 ref、stale click、wait timeout 和 2 次 JS syntax error，不能描述为一次无错完成。source_failure / fixed_failure 的受控 403 按预期停为 blocked，不代表意外联网故障。

普通 Research 自主查询与交付两组均为 **5/5**：`research-url-first/summary.json` 与 `research-wording-first/summary.json`。覆盖单列 URL 来源接入、无预声明工作流的动态查询、同名按钮消歧、明确字段、初始正文已足够、慢更新和无关控件。正文足够和无关控件用例零点击；动态查询各一次点击，引用正式查询正文。

自由 JS 部分成功后取值失败恢复首轮 **通过**：`partial-first/partial/continuation-summary.json`。明确前缀注入 Result<T> 取值错误，父程序失败不撤销已完成的导航／填写。失败父程序的零起始正式回执序号为 **2**，它的已提交子回执 **3、4、5**（快照、填写、快照）保留；新的续跑程序从回执 **6** 开始，首个新业务动作是回执 **7** 的点击。4 次真实生成完成剩余查询；总导航 1、快照 2、填写 1、点击 1、等待 1、正文读取 1，没有重复填写或点击。实际请求中的恢复交接及所有动作计数另经 `raw-partial-recovery-audit.json` 核对。与旧错误 done 的 1 次真实生成恢复分开记录。

实机前后冻结 hash 一致，追加测试修复后又核对 **815 个源码文件**，全部未变，记录为 `source-freeze-final-check.json`。追加变更只影响测试，因此复用同一冻结生产源码的完整实机结果，没有为了把模型失败跑绿而反复重试。

这轮修复提供可靠的引用提示与针对性恢复反馈，不证明模型每次都会采用提示，也不把存在／版本／权限校验当成内容采用或语义正确的证明。Hybrid 的知识引用政策没有改成“只允许本任务已读”。
