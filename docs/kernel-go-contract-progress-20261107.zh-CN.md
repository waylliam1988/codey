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
| 3 | TextResultSpan、execution、recovery | 待开始 | |
| 4 | 六种 provider 请求联合、回复、版本化 codec | 待开始 | |
| 5 | provider adapter、wrapper、capabilities | 待开始 | |
| 6 | contracts、builder、driver、context 主链 | 待开始 | |
| 7 | completion 输入、verification observation、纯判定 | 待开始 | |
| 8 | research 参数、报告、receipt、staging | 待开始 | |
| 9 | 强化门禁、Go 差分实现与测试、最终审计 | 待开始 | |

## 不得遗漏的契约拥有者

除原始十四模块，覆盖 providers/base.py、runtime/core/models.py、kernel_recovery_context.py、provider_request.py、kernel_prompt.py、research/passages.py、ledger_receipts.py、finding_report.py、answer_report.py 及工具 validator/repair。

## 接续工作位置

第二批已完成。下一批修改 TextResultSpan/FormattedToolResults 贯穿 prompt/actions/context/provider codec，并收紧 execution/recovery 的 workspace/evidence/settle；不得破坏新旧冻结请求或移动 Any 债务。

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
