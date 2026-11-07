# Kernel 契约收敛与 Go 迁移准备

## 目标与约束

落实目标文件中的全部九批：核心领域契约明确化、受控 adapter/codec、十四个原始模块显式 Any 归零，以及可供未来 Go 实现复用的行为样例与差分验证。不能通过把 Any 移到相邻文件、泛化成 object 或跳过导入来宣称完成。

- 每批完成相关测试、扩大相关回归、Ruff、mypy/Pyrefly/ty 后单独 commit。
- 不执行全量 pytest。失败先定位生产缺陷或过时测试假设，再修复根因。
- 保留已冻结请求、摘要、effect 归属、窗口身份、未知外部结果禁止盲目重放等语义。
- 当前主机 Python 3.12.8；仓库 minimum/CI 仍包括 3.11，除非另有明确需求不撤销兼容。
- 初始 PATH 无 Go；现已在gitignored的.e2e-artifacts/toolchains安装并校验Go 1.27.3，用于真实差分执行。
- 开始时 git 工作区干净。原始精度基线合计 192（不是 172）。

## 批次追踪

| 批次 | 范围 | 状态 | 验证 / 提交 |
|---|---|---|---|
| 1 | 行为样例、wire/摘要/坐标/恢复契约 | 完成 | 113 passed、30 subtests；Ruff 通过；类型基线见下 |
| 2 | 共享 JSON、ToolCall/provider arguments、底层 codec | 完成 | 105 passed/4 subtests；静态负例及 codec 21 passed；三工具定向检查/Ruff 通过 |
| 3 | TextResultSpan、execution、recovery | 完成 | 首轮85 passed；扩大134 passed；三工具/Ruff/gate 通过 |
| 4 | 六种 provider 请求联合、回复、版本化 codec | 完成 | 扩大102 passed；补充80 passed；三工具/Ruff/gate通过 |
| 5 | provider adapter、wrapper、capabilities | 完成 | 首轮122；扩大97及67 passed；三工具/Ruff/gate通过 |
| 6 | contracts、builder、driver、context 主链 | 完成 | 24个生产文件三工具/Ruff通过；扩大133 passed；见第六批记录 |
| 7 | completion 输入、verification observation、纯判定 | 完成 | 扩大390 passed/64 subtests；契约与静态负例32 passed；三工具/Ruff通过 |
| 8 | research 参数、报告、receipt、staging | 完成 | 0708626e；271 passed/77 subtests，新增及修正后145 passed/77 subtests；91生产文件三工具通过 |
| 9 | 强化门禁、Go 差分实现与测试、最终审计 | 完成 | Go615差分通过；完整生产树624文件三工具通过；扩大172及164 passed，见第九批记录 |

## 不得遗漏的契约拥有者

除原始十四模块，覆盖 providers/base.py、runtime/core/models.py、kernel_recovery_context.py、provider_request.py、kernel_prompt.py、research/passages.py、ledger_receipts.py、finding_report.py、answer_report.py 及工具 validator/repair。

## 接续工作位置

九批实施与验证完成。十四个原始模块显式Any均为0，门禁保护共享拥有者；完整生产树624文件三工具target3.12通过。615条真实Go差分通过。第九批提交后本工作结束；后续Go完整运行时仍需扩展effect/batch/CAS/网络/并发的差分与故障注入，不能把reference切片当作已完成的Go应用。详细迁移说明见kernel-go-migration-guide.zh-CN.md。

## 第一批验证记录

- `python -m pytest -q tests/test_kernel_go_contract_fixtures.py tests/durable/test_prepared_provider_request.py tests/test_provider_not_sent_settlement.py tests/test_text_result_views.py tests/test_runtime_operation_reducer.py tests/test_completion_incomplete_latest_observation_blocks.py tests/test_kernel_parity_regressions.py tests/test_kernel_type_precision_gate.py`：113 passed、30 subtests passed。
- `ruff check tests/test_kernel_go_contract_fixtures.py`：通过。
- 首轮新样例失败原因是测试使用了缺少 sha256: 的指纹，以及直接比较 Python tuple 与 JSON array；修正样例和 wire 投影比较后通过。不是生产回归。
- 在尚未修改生产代码时执行完整生产树三工具检查（Python target 3.12）：mypy 16 errors/12 files/603 sources；Pyrefly 29 errors；ty 27 diagnostics。均为开始时生产树问题，不得声称完整类型检查通过，也不添加豁免。涉及附件边界、completion_context.__dict__、provider receipt bindings、可选 policy、路径/run id 等；后续按所属批次修复，最终必须重新全树验证。

## 已确认的风险

- CompletionContext.selected_check 声明/归一化使用 str，但消费方需要 command/cwd；在第七批沿调用方确认并修复。
- ProviderInvocation 仍是 method + 通用 args/kwargs；第四至六批必须贯穿替换。
- RuntimeAction 目前是普通 dataclass，不得误称已具备判别联合的静态约束。
- frozen dataclass 内部 dict/list 仍可变；跨语言边界与 staging 必须明确所有权。
- digest(text) 是 JSON 编码字符串的 SHA-256；不是原文本 SHA-256。
- span/passage 使用 Python Unicode 码点坐标；Go 必须显式转换，不能按 bytes 切片。

## 第二批变更与验证

- 新增低依赖 codey/types/json_value.py：JsonValue/JsonObject/ToolArgs、严格外部 decoder、单独的 Python transport encoder、tuple-compatible canonical codec。返回分离容器；拒绝循环、非有限浮点、非字符串键、非法 Unicode，并限制深度/节点/字符串字节量。
- runtime/core/models.py 与 providers/base.py 的 ToolCall/ProviderToolCall/control args 接上 JSON 类型；原 canonical 与 response serializer 委托共享实现，保留 tuple/dataclass/Enum/set 在各自边界的既有区别。
- CodeMode host、本地 provider markup、safe replay 的开放对象经过 decoder；audit search 移除开放 kwargs 转发，逐字段验证。toolchain 的显示辅助函数维持对非法运行时对象的防御，同时避免精确类型后的不可达诊断。
- 扩大回归 105 passed、4 subtests（JSON/golden/projection/CodeMode/local codec/safe replay/audit/boolean schema）；较早的 durable/projection 组合 80 passed、4 subtests。静态负例与 JSON codec 合计 21 passed，三检查器均拒绝向 ToolCall/ProviderToolCall 注入 object。
- 修改的十个生产文件：mypy --follow-imports=silent（保留导入类型解析，不 skip）、Pyrefly、ty，target 3.12 均通过。Ruff、precision gate、git diff --check 通过。全树起始类型问题仍待后续所属批次处理。
- 行为补强：provider serializer 现在在入口拒绝 NaN/Infinity、循环和非法 Unicode，而非将其拖入持久化；有明确边界回归。

## 第三批变更与验证

- TextResultSpan 使用真实六字段（start/end/digest/result_ref/tool/ok）；decoder 验证坐标、顺序、摘要和字段类型。format_results 返回 FormattedToolResults，actions 返回 ResultContinuation，context 用 replace 显式平移坐标，provider wire 最后转字典；不再依赖 mutable out 参数。
- kernel_execution 剩余八个 Any 全部消除，复用 ExecutionEvidence/Path/WorkspaceRevisionStore/SettleToolResult；结算结果限制 trusted/unconfirmed。注入 executor 返回值仍通过独立 object 验证入口检查，未因静态注解删除运行时防御。
- kernel_recovery_context 同步移除所有 Any，current_state 返回 WorkspaceState|None，显式处理缺失状态。未将执行路径债务转移到 recovery。
- 原始十四模块计数：192 → 181；actions 1→0、execution 8→0、context 19→17。更新精度基线。
- 首轮 prompt/golden/epoch/receipt/workspace/durable/gate 回归85 passed。扩大 Unicode span/三工具负例/workspace 缺失与扫描失败/native receipt/research scoped projection/parity 回归134 passed。
- 六个修改生产模块的 mypy --follow-imports=silent、Pyrefly、ty（3.12）通过，Ruff/precision gate/diff check 通过。
- tests/test_text_result_views.py 改为消费新返回结构；这是内部 API 演进，既有文本和 receipt wire 格式保持不变。


## 第四批变更与验证

- ProviderRequest 为六种 dataclass 联合；不再把 method + 通用 args/kwargs 存为 driver 领域对象。ProviderRequestOptions 显式保留 omitted/null/false/zero/empty；计数限制非负 int64，timeout 限有限正数。
- legacy-v1 codec 保留旧 wire，已存 artifact 不重写或重新计算摘要；支持真实 API 的位置/关键字参数，拒绝重复参数、未知 options、未知版本、非法 schema/results/window。
- driver 在 durable admission 之前解码请求；新增冻结请求非法时不 admit、不发送的回归。六方法实际调用只在有限 compat_transport adapter 分派。完整 wrapper 契约留到第五批。
- 持久化回复使用 TextReplyPayload/NativeReplyPayload，验证嵌套 arguments/raw、finish，不再返回未经验证的字典。
- 扩大 codec/golden/static/durable response/snapshot/receipt/native/reply normalization/gate 回归102 passed；补充新增 codec/admission 与 prepared/not-sent 回归80 passed。六个修改生产文件 mypy --follow-imports=silent、Pyrefly、ty（3.12）通过；Ruff和精度门禁通过。
- 原始十四模块 Any 181→180（driver 12→11）；下调基线。全树起始类型问题仍待最终清理。


## 第五批变更与验证

- ProviderAdapter 删除 __getattr__ 和通用 *args/**kwargs 发送。六个旧 API 有明确签名，并统一进入 ProviderTransport.execute_request(ProviderRequest)。Conversation/Deadline/Observed wrapper 都在这个具名请求端口拦截；BeforeSend 只接收 ProviderRequest。研究 topic continuity 观察及测试/手工探针随之迁移。
- 第三方动态调用和能力查找集中 compat_transport；循环链先拒绝，normalizer 仅识别显式声明且只调用一次。原始 transport 回复仍为边界 object，由 normalize_provider_reply 严格验证、分离容器后才交给核心；未将开放 object 冒充已验证领域回复。
- ContextBudget、窗口、text format、attempt gate、usage binding 通过明确边界保留。KernelCapabilityView.window_ref 由 object 收紧为 ProtocolWindowRef；字符串窗口测试是过时假设，改为真实结构并添加非法窗口拒绝测试。
- 回复 codec 现在拒绝非法 finish、call 对象及嵌套非 JSON 参数；旧测试允许任意 sentinel 穿透 normalizer 的假设被删除。合法 text/native、local markup 的归一化仍通过。
- Deadline 覆盖全部六协议，包含位置 timeout 不得与关键字重复；浮点 rounding 仍不扩大总预算。wrapper 自身 episode timeout 优先于底层 provider timeout。
- 原十四模块显式 Any 180→148（provider_session 32→0）；没有新增开放动态模块接纳这些 Any。另修 research_iteration 的 project_path str→Path 起始类型问题。
- 首轮协议/deadline/lifecycle/local codec/parity 回归122 passed；扩大 wrapper/admission/not-sent/static 回归97 passed；研究 topic/scoped projection/native receipt/capability/local codec 回归67 passed。十个修改生产模块三工具 target3.12通过，Ruff/gate通过。静态负例新增 observer 签名、非法 prompt 和不存在扩展方法的检查。


## 第六批变更与验证

- contracts、builder、context、driver接入有限ProviderRequest；新增ProviderRequestContext与PreparedProviderRequest，恢复路径接收具名请求、冻结snapshot/authority、intent和计数器，不再携带通用args/kwargs包。
- KernelResult持有ResearchReport联合，payload逐字段编码并在恢复时验证交付文本一致性；attempt gate在兼容边界转换为只读AttemptGateView和已验证JsonObject。
- sink只在execution拥有；旧observation入口构造时归一化并清除旧位置。保留既有重启替换sink语义。sink的durable为必需资源，managed output在实际需要时校验，保留合法的无managed store内联工具结算。
- snapshot的ToolSpec逐字段解码，拒绝错误primitive/集合/参数结构；冻结context缺effect_scope拒绝、显式空scope仍允许；补测嵌套NaN/Infinity、非法delivery和文本协议非有限值拒绝。
- 修改的24个生产文件mypy --python-version 3.12 --follow-imports=silent、Pyrefly和ty target3.12均通过；所有修改Python文件Ruff、precision gate、diff check通过。全树初始类型问题及completion/research领域债务仍待后续批次解决。
- 大范围领域/provider/研究/重启验证初轮270 passed、2 failed：两处测试分别仍按dict访问CompletionContext、以及假ResearchTools遗漏协议要求的sources_read/search_result_urls；按真实契约修正测试。另扩大83 passed、1 failed为旧架构allowlist未包含新增共享provider契约/codec模块，已更新有限allowlist，继续禁止具体provider实现。
- 修正后合并重跑入口、领域、协议、native、not-sent、normalization、settlement、恢复、receipt、parity及workspace provenance：133 passed。领域与架构边界单独22 passed；三工具静态负例在前述270通过范围内。
- 原十四模块显式Any 148→93；不声称第七至九批完成，不以Python样例通过替代Go执行证据。


## 第七批变更与验证

- CompletionContext.selected_check修正为VerificationCandidate；旧mapping/属性兼容只在completion_context_codec固定字段表归一化，删除extras/get动态后门。changes与analysis提取成具名不可变输入，历史研究前提捕获为ResearchPrerequisiteFacts，不再持有可变父session。
- VerificationObservation区分CompleteVerification与IncompleteVerification，保留输入顺序、command/cwd/edit epoch、缺字段与显式false差异。最新坏记录仍遮蔽旧成功；禁止忽略最新坏记录后恢复旧绿灯。
- workspace采集从项目完成评价拆出；ProjectCompletionFacts捕获不可变路径/观察事实，evaluate_project_completion不扫描workspace且不修改传入execution evidence。扩展CheckProvider接收不可变CompletionCheckInput，不能改写live session。
- TaskSession真实拥有者接上TaskPolicy、VerificationCandidate/VerificationRecord、ExecutionRecord、HitTarget和JsonObject；kernel_facts与execution_evidence同步消除Any。clone_projection显式复制嵌套可变事实，补测未commit的staged更改不泄漏。
- 修复blocked_result遇facts=None的确定性回归。旧测试的内部dict context、假evaluation/evidence、假native reply改为真实领域对象；不放宽生产codec以适应过时测试。
- completion/research/project/verification及recovery/golden/convergence扩大回归390 passed、64 subtests。新增input/observation codec、candidate冲突、不可变扩展、嵌套隔离与三工具静态负例合计32 passed（含原静态负例）。未执行全量pytest。
- 18个修改/新增生产模块mypy --follow-imports=silent、Pyrefly、ty target3.12通过；Ruff、precision gate与diff check通过。原十四模块Any 93→20，completion三个基线归零；报告/工具/receipt/staging仍属于第八批，未宣称完成。


## 第八批变更与验证

- research/tools及passages、receipt、source gateway、staging、两个报告编译/恢复入口、工具contract/repair接上共享JSON、PassageDescriptor及具名报告字段；原十四模块显式Any从20降至0。不是宣称整个research/knowledge目录没有Any，旧展示/知识库内部仍需后续独立迁移。
- AnswerSubmission区分未签名body与必需submission_digest的完成提交；AnswerScope入口逐字段解码。InvestigationRecord、CoverageRow、关系结构与报告来源/证据/引用字段由明确拥有者定义。完整JSON合法性、工具schema和既有领域/权限检查仍是不同层。
- FindingReport与AnswerReport拒绝恢复后被丢弃的摘要绑定字段；构造时复制嵌套容器。frozen只防重绑定，公开TypedDict仍可变，采用独立拥有、只读使用的约定，不宣称深不可变。
- Staging复制输入note、mutation及返回read视图；精确revision读取支持待提交版本；相同commit重复调用保持既有幂等行为。补测调用方和read返回值更改不污染待提交状态，未提交不写入parent。
- PDF二进制边界拒绝整数和任意对象，避免bytes(integer)伪造内容；非法fetch变为明确工具失败，不污染ledger。报告恢复拒绝重新计算摘要后夹带的未知嵌套字段。
- 扩大研究/whole-answer/report/knowledge/recovery/golden/repair回归271 passed、77 subtests。另扩大测试首轮188 passed、3 failed：旧observation测试使用TaskSession(policy=None)，已改成明确空授权TaskPolicy以保留测试本意；修正后相关合并145 passed、77 subtests。新增typed-boundary回归覆盖submission、binary、stage隔离和report字段篡改。未执行全量pytest。
- 本批91个生产文件mypy（follow-imports=silent保留解析）、Pyrefly、ty target3.12通过；Ruff、precision baseline、diff check通过。全生产树类型检查与Go差分仍属第九批，未宣称完成。


## 第九批变更与验证

- 精度基线十四模块全部为0；AST额外保护共享codec/领域拥有者，识别typing.Any、导入别名、TypeAlias、forward annotations、cast目标；拒绝裸泛型、缺注解函数、核心通用args/kwargs、__getattr__、核心直接provider getattr分派及已知领域参数泛化object。明确保留adapter准入入口。AST不是动态数据流的形式化证明，仍依赖三工具、架构和边界测试。
- tools/go_contract为独立Go reference，Python脚本真实build/vet/test并执行615个差分用例（508接受、107拒绝）。包含原golden输入、六请求/回复/非法参数、中文/astral/combining/span、passage、JSON摘要、256个固定seed有限IEEE-754样本、verification缺口与最新坏记录遮蔽，以及无effects/batches的reducer分支。Go单测另验证未配对surrogate拒绝与大整数保留。本机实际Go 1.27.3 windows/amd64；下载归档校验SHA-256，工具链与二进制仅在gitignored .e2e-artifacts。
- CI新增独立kernel-go-contract job；找不到Go会失败，没有skip或Python fallback。未触发远端CI，也未修改分支保护。reference没有实现完整Go durable/effect/batch/网络/并发；真实崩溃和CAS验证仍由现有Python测试覆盖，迁移后需继续移植，文档明确记录此限制。
- 修完整生产树的attachment对象收窄、run_id、Cline未知image能力及若干空容器/构造注解；共享工具repair在入口严格JSON解码，ToolSpec消费具名schema访问器。provider底层tools_from_specs明确ToolSpec类型；driver/bind入口继续收紧ProviderTransport。
- 最终完整生产树mypy --python-version 3.12（无follow-imports跳过）624文件通过，Pyrefly/ty target3.12通过。Pyrefly仍有既存35处suppression、126个warning，不宣称零warning。Ruff全仓通过，精度与diff check通过。
- durable请求/NOT_SENT/operation reducer/state/provider codec/wrapper/span/recovery/completion/研究边界合并172 passed、42 subtests。attachment/codemode/cline/static/architecture等扩大首轮278 passed、461 subtests及6个架构失败；逐项定位并修复：共享纯JSON增加精确leaf依赖许可，DurableStore从public facade仅用于类型导入，audit诊断展示投影移出Trace防止行为层依赖Trace状态模块。重跑architecture/audit/precision为164 passed、464 subtests。
- 三项size snapshot失败属于任务开始前240e329e附件提交已改变的源码，本九批对providers/worker.py、providers/api_provider.py、toolchain/runtime.py无改动（git diff eb083405 --这三文件为空）；据当前真实尺寸校准陈旧记录，继续禁止后续增长，没有以调高精度基线掩盖Any。
- 新research-payload三工具静态负例3 passed；最终precision/golden/CI契约47 passed。未运行全量pytest。上述测试集合存在重叠，不把它们简单相加成独立用例数。
- 详细教学与后续Go重写路线：docs/kernel-go-migration-guide.zh-CN.md；更新wire v1的实际差分覆盖与限制。

## 九批提交索引

1. e365ecd1 — 行为/wire/recovery fixtures
2. 53c15775 — 共享严格JSON
3. cf02bf7e — span/execution/recovery
4. 688ddbe6 — 有限provider请求及回复codec
5. b1fd142e — 显式provider wrappers/adapters
6. cbcb25eb — kernel主链与冻结authority
7. dd43e2c4 — completion输入与有序verification
8. 0708626e — research报告/receipt/staging
9. 本文随最终gate/Go differential提交；通过git log可查最终提交号。
