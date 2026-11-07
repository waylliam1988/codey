# Kernel 类型契约与 Go 迁移指南

本文对应九批 Python 重构的实际结果。工作记录见
[进度文档](kernel-go-contract-progress-20261107.zh-CN.md)，旧格式与摘要规则见
[wire v1](kernel-go-wire-contract-v1.md)。

## 先看验收边界

原表十四模块原来合计 **192** 个显式 `Any`，现在都为 **0**。
共享契约拥有者也进入防退化检查，避免把动态类型挪到相邻模块。
这不代表整个仓库没有 `Any`、隐式动态值或运行时错误。

内部数据流应按下面的顺序理解：

```text
第三方对象、外部 JSON、旧持久化记录
    → compat_transport / JSON decoder / 各领域 codec
    → ProviderRequest、TextResultSpan、VerificationObservation、ResearchReport
    → 执行、恢复、完成判定
    → 旧 wire codec
```

合法 JSON、合法工具参数、被授权的当前状态，是三种不同判断。
`{"summary": 123}` 是合法 JSON，但不是合法完成参数；合法参数也不意味着允许发送。

## 十四个文件分别发生了什么

| 模块 | 现在使用的契约 | Go 对应与迁移重点 |
|---|---|---|
| `kernel_actions` | `FormattedToolResults`、`ResultContinuation`、`TextResultSpan` | 返回具名 struct；不再通过可变 out 参数收集 span |
| `kernel_execution` | `ToolExecutionContext`、`ExecutionEvidence`、`SettleToolResult`、具体 workspace state | struct + 小接口；保留已编辑但身份未确认的恢复分支 |
| `kernel_protocol` | `ToolArgs`、冻结 `ToolSpec`、标准化 provider calls | JSON 解码后做 schema/领域校验，再形成 ToolPlan |
| `kernel_contracts` | 有限 provider transport、研究报告联合、具名依赖 | struct 拆出执行资源与观察资源，避免双重 sink 所有权 |
| `kernel_request_builder` | 与 contracts 同一套 provider、attempt gate、路径和身份类型 | 构造函数完成准入和规范化，内部不再放宽类型 |
| `kernel_context` | 具体 operation state/action/view、`PreparedProviderRequest` | 明确状态、事件和返回动作；不要翻译为反射调用 |
| `kernel_driver` | 请求 codec、有限 transport、持久化 effect 边界 | 发送与 CAS 持久化分开；CAS 重试不得重发网络 |
| `completion_gate` | 统一 `CompletionContext`、不可变 `CompletionCheckInput` | 判定输入快照与 verdict；扩展无法修改 live session |
| `project_completion_checks` | `ProjectCompletionFacts`、有序 verification observations | 采集 workspace 与纯评价分离，保持最新坏记录的遮蔽语义 |
| `research_completion_checks` | 同一 CompletionContext、历史 prerequisite facts | 历史研究依据与本轮 acquisition 分开 |
| `research/tools` | JSON 参数、现有领域校验、PassageDescriptor、具名研究输出 | 工具边界解码；写入前形成 KnowledgeNote/KnowledgeMutation |
| `provider_session` | 六个明确旧 API、`execute_request`、具名 observer | 有限端口，兼容探测委托专门 adapter；没有开放 `__getattr__` |
| `kernel_capabilities` | 明确 window ref 和能力快照 | 准入时采集，不让核心反复猜测第三方属性 |
| `completion_context` | 具体候选验证、changes、analysis、研究前提 | 结构体字段；旧 mapping/属性兼容只保留在入口 codec |

这里没有假造一个万能 `KnowledgeWriteArgs`。旧 `knowledge_write` 本身具有创建、更新、
source、fact 等不同模式，继续在工具入口接收已验证 JSON，经现有领域校验后构造具名对象。
`knowledge_commit` 已有具体的 `KnowledgeCommitRequest`。Go 后续可按真实模式拆分命令，
但不能把旧字段统统设为 optional 后就声称完成领域建模。

## Go 应先实现数据契约，再实现外部系统

建议分成四个包，依赖只朝领域层流动：

```text
domain       state/action、request/reply、workspace、completion facts
ports        Provider、EffectWriter、WorkspaceReader、KnowledgeReader
wire         旧请求/回复/checkpoint/报告 codec 与摘要
adapters     HTTP、浏览器、文件系统、SQLite、第三方 provider
```

核心端口可以从这个形状开始：

```go
type Provider interface {
    Execute(context.Context, ProviderRequest) (ProviderReply, error)
}

type WorkspaceReader interface {
    Current(context.Context, WorkspaceQuery) (WorkspaceState, error)
}
```

`ProviderRequest` 应是封闭接口或通过构造函数创建的有限判别结构。
若采用接口，包内 marker 方法可以阻止调用方塞入无关实现；每种 request 一个具体 struct。
若采用 kind + 字段组合，构造函数和 decoder 必须拒绝不可能的组合。
不要把 Python 的动态方法名和 `*args/**kwargs` 翻译成 Go reflection。

领域请求与持久化格式不同。已有 artifact 仍然是 method/args/kwargs，
Go decoder 将其转换为有限请求；encoder 继续生成 v1 格式。
已保存的 artifact bytes、摘要和引用不应为了适配内部 struct 被重写。

## 缺失、null、false、0 必须分别表示

例如 timeout 未传与显式 null，以及输出限制未传与显式 0，均有不同的 wire 表示。
单独 `*T` 只能表达两种状态，必要时使用：

```go
type Optional[T any] struct {
    Present bool
    Value   *T // Present=true, Value=nil 表示显式 null
}
```

encoder 根据 Present 决定是否写字段，再根据 Value 决定 null 或实际值。
不能依赖 `omitempty` 自动处理所有字段；它容易丢掉合法的 false、0 和空集合。
空列表与 null 列表也需要保持区别。

## 摘要与字符串坐标最容易出现跨语言偏差

- Python span 按 Unicode 码点计数；Go 的 `len(string)` 是 UTF-8 字节数。
  验证/切片时使用 rune 坐标，或明确维护码点到字节的映射。
- 不做 NFC/NFD、换行或空白规范化。`e` 加组合重音与预组合字符不是同一原文。
- v1 `digest(text)` 哈希的是 JSON 字符串编码，包含引号与转义，不是裸文本。
- Go 默认 JSON 会转义 HTML 字符；即使关闭 EscapeHTML，U+2028/U+2029 仍需处理。
- 整数 `1` 与浮点 `1.0`、`0.0` 与 `-0.0` 可能有不同摘要。
- 不要用默认 `float64` 接收开放 JSON 整数。`json.Decoder.UseNumber` 保留数字词法，
  已知字段再按对应领域规则解码。

provider 的计数选项已限制为非负 int64。旧 workspace revision 等 Python 整数并非都已有
int64 上限；Go reference 用任意精度整数保留这类旧记录。不能直接增加上限后称为无行为变化。
需要收紧时，应增加明确版本和迁移规则。

`tools/go_contract` 提供实际可执行的 Go reference，包括上述 JSON 摘要编码差异处理。
它是迁移参考切片，不是完整 JSON 标准实现的形式化证明。

## 恢复流程先明确发送状态

至少区分未发送、可能已发送、已得到响应、响应已持久化。
取消、超时或本地异常，不足以证明远端没有产生 effect。

```text
冻结请求 → durable admission → 发送一次 → 保存响应 → 结算/交付
```

请求冻结但未 admission，可以继续准入后发送。
已经 admission、结果未知时，只能走既有 unknown-outcome 结算规则，不能盲目重发。
保存时发生 CAS 冲突，应重新读取并提交已知响应，不能重新调用 provider。
用带 SendStage 的错误类型表达这些区别，比把一切折叠为普通 error 更利于迁移。

现有 process-death、NOT_SENT/MAYBE_SENT、lost acknowledgement、window mismatch、
重复结算等 Python 测试应持续保留。未来 Go durable 实现必须加入相同故障注入，
当前 Go reference 尚未替代这些真实持久化测试。

## Completion 要消费有序事实

verification 的主键是 command + cwd，还要匹配 edit epoch。
同一主键最新记录缺 exit code 或 workspace 身份时，它仍然是一条观察事实。
不能过滤坏记录，再退回较早的成功结果。

Go 中应先逐条 decode 为 Complete/Incomplete observation，再按顺序更新最新事实。
不要先把坏 JSON 行丢弃。命令/cwd 的主键用 struct，避免字符串拼接分隔符造成碰撞。
workspace 扫描先生成事实快照，完成判定只评价快照。

## Research 要保持精确版本和容器所有权

同一 URL 可以对应多个读取版本。passage/source/evidence 的精确版本必须保留；
accepted report 不能使用 latest source 重新编译。
失败 acquisition 可证明尝试过调查，但不能成为支持论断的证据。
引用、版本与交付文本通过校验，也不等于论断语义已经独立审查。

Python `frozen=True` 不会冻结嵌套 dict/list；Go struct 复制也不会复制 map/slice 的底层存储。
本次报告、输出 observation、staging、session projection 在对应边界复制容器。
Go 应在构造/投影边界做等价深复制，或明确转移唯一所有权。
待提交 stage 不应泄漏到 parent，read 返回值也不应反过来更改 stage。

研究报告恢复必须验证摘要、领域结构，以及重新编码后字段是否完整保留。
不能接受一个包含未知字段的已签名对象，静默删掉字段后仍声称它是原来的报告。

## 如何继续 Go 重写

1. 先实现 wire/JSON、request/reply、span、passage 和 verification；运行现有差分程序。
2. 再扩展纯 reducer：从无 effect/batch 分支扩展到具体 effect、delivery、approval。
3. 接入 durable store，复用崩溃点和 CAS 冲突脚本，特别检查发送次数。
4. 接 provider 和 workspace ports；通过脚本化 adapter 验证后再接真实远端。
5. 移植 completion，比较 reason code、候选验证和最新观察处理。
6. 最后移植研究工具、知识事务、报告编译和 UI/application ports。

每一步同时保留旧记录恢复与新记录生成的测试；不要把升级当前 schema 当作旧记录迁移。

## 复现检查

```powershell
python -m tools.kernel_type_precision_gate
python -m mypy codey --python-version 3.12
pyrefly check codey --python-version 3.12
ty check codey --python-version 3.12
python -m ruff check .
python -m tools.kernel_go_differential
```

最后一个命令要求真实 Go：从 PATH、`GO_EXECUTABLE` 或本地隔离工具链查找，
构建 reference，运行 Go vet/test，再与 Python 比较输出；找不到 Go 会失败，不会 skip。
CI 有独立的 `kernel-go-contract` job，检查失败会使该 job 失败；未修改远端分支保护设置。

当前差分覆盖有限请求/回复、合法与非法参数、Unicode span、passage、摘要、
verification 缺口/有序遮蔽，以及无 effects/batches 的 reducer 分支。
**尚不覆盖完整 Go durable、网络、并发、取消和业务工作流。**
这九批提供的是可直接继续实现和验证的契约基础，不是已经完成 Go 重写。
