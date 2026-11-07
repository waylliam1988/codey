# Kernel 契约收敛与 Go 迁移准备

## 目标与约束

落实目标文件中的全部九批：核心领域契约明确化、受控 adapter/codec、十四个原始模块显式 Any 归零，以及可供未来 Go 实现复用的行为样例与差分验证。不能通过把 Any 移到相邻文件、泛化成 object 或跳过导入来宣称完成。

- 每批完成相关测试、扩大相关回归、Ruff、mypy/Pyrefly/ty 后单独 commit。
- 不执行全量 pytest。失败先定位生产缺陷或过时测试假设，再修复根因。
- 保留已冻结请求、摘要、effect 归属、窗口身份、未知外部结果禁止盲目重放等语义。
- 当前主机 Python 3.12.8；仓库 minimum/CI 仍包括 3.11，除非另有明确需求不撤销兼容。
- 当前 PATH 无 Go，跨语言执行验证需要安装或发现实际 Go 工具链；不能以 Python 自测替代 Go 验证。
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
| 7 | completion 输入、verification observation、纯判定 | 待开始 | |
| 8 | research 参数、报告、receipt、staging | 待开始 | |
| 9 | 强化门禁、Go 差分实现与测试、最终审计 | 待开始 | |

## 不得遗漏的契约拥有者

除原始十四模块，覆盖 providers/base.py、runtime/core/models.py、kernel_recovery_context.py、provider_request.py、kernel_prompt.py、research/passages.py、ledger_receipts.py、finding_report.py、answer_report.py 及工具 validator/repair。

## 接续工作位置

第六批已完成并验证，主十四模块显式 Any 为93，核心五模块及context/driver/provider_session均为0。下一步第七批：先修复CompletionContext.selected_check丢弃VerificationCandidate的问题，建立具名verification observation与session事实模型，统一completion输入并分离workspace采集与纯判定。第八、九批尚未开始。完整目标仍包括研究契约拥有者与真实Go差分验证。

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
