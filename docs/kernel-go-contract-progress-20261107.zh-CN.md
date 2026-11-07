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
| 2 | 共享 JSON、ToolCall/provider arguments、底层 codec | 待开始 | |
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

第一批已建立语言无关 JSON golden fixtures 与 Python consumer，锁定六种请求、Unicode 坐标、摘要和验证记录缺陷。原有 durable 真实进程死亡/丢失确认测试通过。下一批先审计已有 canonical JSON 与 provider serializer 的区别，再建立严格低依赖 JSON decoder。

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
