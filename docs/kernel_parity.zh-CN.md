# 统一 kernel 的确定性 parity 检查

基线：`958bcb485bf05d0ae8232763681d1df5ecee1d34`。当前审计日期：2026-09-30。
这份报告把旧版行为变成固定输入、独立 oracle、精确差异和持续回归。
它覆盖下列已枚举边界；有限场景不能证明任意模型输出、浏览器 DOM 或未来功能都无遗漏。

## 方法与边界

| 层 | 可执行产物 | 失败条件 |
| --- | --- | --- |
| 清单 | 所有 `codey/**/*.py` 的 SHA-256、顶层 AST 声明及调用表达式，工具、别名、参数、权限 profile、provider ID、AgentRequest 字段 | 基线导出源码集合或内容不等于指定 Git commit |
| 调用边界 | `tests/fixtures/kernel_parity/boundaries.json` | 消失模块或旧 request 字段没有去向、当前 owner 或测试不存在 |
| 行为 | `baseline.json` 的固定 case 与实际旧版运行结果 | 新版结果不同且没有精确、仍有效的解释 |
| 差异裁决 | `intentional_deltas.json` 的具体 before/after、原因、测试、CHANGELOG 依据 | 新差异、过期例外、多余例外、遗漏场景 |

旧源码从 `reference-projects/codey-pre-unified-958bcb4` 在 Python `-I` 子进程中导入。
每次运行使用独立 HOME 和临时项目/知识库，检查实际导入目录，禁止向 oracle 源码写字节码。
provider、网络和验证子进程使用固定输入；解析器、生产循环、文件工具、权限门、知识库和 evidence ledger 执行真实代码。
普通 CI 使用已冻结的旧结果，不要求下载旧源码，也不把旧循环接回产品。
重新导出只更新旧 oracle，绝不从新版结果生成期望。

清单有旧 323、新 346 个 Python 模块：旧版独有 12 个、新版独有 35 个。
声明增删是审查索引，不等于功能增删；CLI 报告列出模块和声明 diff。
`boundaries.json` 为 12 个消失模块以及全部 40 个旧 AgentRequest 字段记录去向。

主要调用链：

- Coding：`AgentRequest -> project_adapter.run -> TaskSession -> run_task_kernel -> normalize_turn -> execute_turn -> completion_gate.evaluate -> RunResult`。
- Research：`run_research_iteration -> TaskSession / controller snapshot -> run_task_kernel -> normalize_turn -> execute_turn -> completion_gate.evaluate -> synthesis`。
- Durable：`KernelRecordedProvider / KernelEffectSink -> effect intent / settlement / delivery -> kernel_recovery_context / kernel_recovery_result`。

## 场景范围与结果

682 个固定场景：639 个协议场景、33 个 coding 实际循环、10 个 research 实际循环。
协议输入从旧工具定义生成，逐一覆盖示例、别名、required 参数遗漏、每个声明参数的固定单字段变体、额外字段、
JSON/native、权限 profile、围栏/散文/未知模板、工具数边界、缺 call_id 与混入 done。
生成规则也是测试：遗漏旧工具、别名或参数会导致冻结矩阵检查失败。

Coding 比较真实文件、实际工具操作、结果标志、turn 数、fresh chat、handoff、conversation snapshot 和 native receipt ID。
Research 比较 search/fetch、打开来源、evidence 数以及知识笔记的稳定 metadata：
type/title/body/tags/sources/aliases/relations/open_questions/confidence/status/session_id/project/valid_until。
只去掉 UUID、时间戳和语义等价的已声明可选默认值；不会去掉笔记正文或真实文件变化。

| 裁决 | 数量 |
| --- | ---: |
| PASS：与旧版相同 | 583 |
| INTENTIONAL_DENY：有意收紧 | 91 |
| INTENTIONAL_CHANGE：明确协议扩展或旧缺陷纠正 | 8 |
| MISSING / 未分类差异 | 0 |

`INTENTIONAL_CHANGE` 单独标记，避免把接受更多规范输入误称为“拒绝”。
所有 99 个例外都锁定具体 case 的完整 before/after，而非工具名、路径或失败种类的宽泛白名单。

| 差异依据 | 场景数 | 具体规则 |
| --- | ---: | --- |
| strict-arguments | 57 | 严格类型与额外参数检查，拒绝旧 coercion/fail-open |
| canonical-done | 25 | 非空文本 `done.summary` 为唯一结束协议，拒绝 `answer` 和类型强转 |
| canonical-envelope | 6 | 不从散文、think 标签或未知 provider 模板挖工具 JSON |
| shared-grammar | 4 | Research 共享规范 JSON 围栏、name 入口和文本去重 |
| shared-budget | 1 | Research native 批次统一为最多 8 个显式 call ID |
| fresh-verification | 3 | 有改动必须有新鲜、结构化、相关验证；明确禁止验证的请求除外 |
| actual-turn-count | 1 | 发模型请求前已取消时，实际使用 turn 为 0 |
| native-receipt-closure | 2 | accepted done 和最后预算批次也闭合 native ID |

## 已按 TDD 补齐的缺口

以下生产问题先由失败测试或独立旧版 replay 复现，再修改，最后回测。
具体锁在 `tests/test_kernel_parity_regressions.py`，另有冻结矩阵测试。

| 问题 | 修复后的行为 |
| --- | --- |
| `read_files / parallel` 被隐藏并拒绝 | 文本批次先整体校验，再降低为有序只读调用；保留每个工具的权限、恢复和结算边界 |
| 同轮文本重复请求，以及 AAA/ABAB 无进展循环 | 文本重复只执行一次，native 不合并不同 ID；记录结果指纹和 SeenInfo，重复无进展有界停止，变化输出或真实写入不误停 |
| `list_dir` 的默认目录缺失 | 遗漏 path 时恢复 `.` |
| coding context 权限与验证 freshness 不完整 | 无 project.read 不渲染；无 write 不宣称可编辑；失败、过期或身份不匹配的验证不标 fresh |
| verification candidate / loader 未接入 | 编辑后按 epoch 刷新、选择并提示相关候选；候选被移除时清空旧选择；提示不会成为验证 proof |
| 用户明确禁止验证后仍卡在验证要求 | 记录禁止标志，完成检查可为 not_applicable，不伪造 checks_passed |
| adapter 结果和 conversation snapshot 丢状态 | 保存本轮结果、累计 changed_files 和 blocker；后续编辑后不投影旧绿色结果 |
| provider 显式响应 normalizer 被包装器遮蔽 | 会话、durable 及组合包装器明确转交钩子，执行一次；动态 mock 属性仍不能成为可信钩子 |
| 取消在 send 中或同批工具之间到达 | 取消后不执行新工具；剩余已创建槽结算为错误；native 当前和有界后续 ID 均回错误，不执行 |
| native 同 ID 多次出现 | 解析阶段拒绝；修复回执每个 ID 只发一次 |
| 研究 open_questions 丢失 | canonical done 保留最多 4 个追问，传入 synthesis 并可恢复 |
| 研究 finalizer 只被检查、正文却未使用 | 检查、返回、保存同一份 evidence-compiled 答案；无证据结论降入限制，来源编号编译结果真正送达 |
| done.summary 内嵌另一个可执行工具对象 | 拒绝工具对象冒充最终答案 |

批次是语义上兼容的只读工具批次，执行保持有序；不引入线程并发，也不为 native wrapper 编造 child call ID。
嵌套批次、写工具、未知工具、无效参数和展开超限均整批拒绝。

## 对先前分析的校正

runaway/SeenInfo 和 coding context 门控疑点成立，而且遗漏多于最初列出的三个点。
r1 的 provider 适配缺口与拒绝未知模板必须区分；本次还锁定了显式 provider codec 在生产包装器中的传播。

“同轮 JSON/native fallback 完全删除”不准确：协议能力选择在启动时固定，但无 native tool_calls 的标准回复仍可走规范 JSON fallback。
缺少 `NativeToolResultError` 是随旧内部模块一起删除的实现 API；没有发现仍引用它的当前生产调用方，因此不恢复旧协议模块或添加猜测兼容别名。
controller 的静态示例不能单凭函数 diff 判缺失：当前结果上下文仍给实际 r/s/h ID，本次增加 open_result、reopen_source、source_search、open_hit 和 source 引用的实际循环检查。
evidence followup 的 owner 已迁移，并有现有生产测试覆盖。

`AgentRequest.codec` 属于单一权威协议替代的旧扩展点；
`verification_changed_files / verification_successful_checks` 是先前提示，不作为当前 workspace 的验证证明导入。
durable 路径保留显式 provider_id、workspace provenance 和恢复 fail-closed。
不恢复 overflow rollover、不静默换 provider、不猜新模型模板。
默认 turn 数必须在相同入口比较，不能把旧 AgentRequest 的默认值和内部 TaskSession 的默认值直接视为能力丢失。

## 运行与维护

日常回归不依赖旧源码：

```powershell
python tools/kernel_parity.py --report parity.json
python -m pytest -q tests/test_kernel_legacy_parity.py tests/test_kernel_parity_regressions.py
```

完整重放旧源码及校验源码指纹：

```powershell
python tools/kernel_parity.py --legacy-root reference-projects/codey-pre-unified-958bcb4 --report parity.json
```

仅当有经过审查的 oracle/场景变更，明确重新导出：

```powershell
python tools/kernel_parity.py --legacy-root reference-projects/codey-pre-unified-958bcb4 --export-baseline tests/fixtures/kernel_parity/baseline.json
```

新增差异先按 case 写最小失败测试。真实缺失修到 PASS；有意差异必须写具体协议依据、正/负回归和 CHANGELOG，再提交精确例外。
CLI 报告与 pytest 都不会自动认可新差异。

尚未纳入离线 oracle 的范围：真实网页 DOM、真实模型生成质量、网络/浏览器时序、全部未来组合。
Hybrid、持久恢复、effect settlement、repair_context 实际送达、provider capability 和 fault injection 继续由现有专门测试覆盖；
边界清单明确这些测试引用，不声称离线 corpus 对它们做了完整旧新差分。
新增能力或 bug 输入应持续加入矩阵，不能用“0 个差异”推导绝对无缺陷。

## 最终验证

全量 `python -m pytest -q -p no:cacheprovider`：`5822 passed, 32 skipped, 1471 subtests passed in 357.78s (0:05:57)`。零失败。
预检查：ruff、compileall、JavaScript 语法、diff 与 5854 项测试收集均通过。
正式文档在全量 pytest 完成后更新；未修改版本号、未 release。
