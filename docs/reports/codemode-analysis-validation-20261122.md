# CodeMode 计算、语义查询和 finding 阶段验证

基线提交：`90d84ccd`。本轮直接修改原工作区，没有创建或复制 worktree。实机使用已运行的 `http://127.0.0.1:5001/v1`，模型为 `koboldcpp/Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M`。没有跨供应商或第二模型验收；重命名复跑脚本禁用了额外 reviewer。

## 本轮实现

| 项目 | 当前行为 |
| --- | --- |
| LSP | Python、JS/TS、Go、Rust 共用一个 stdio 客户端，实现 definition、references、hover、diagnostics、documentSymbol 和只读 rename 计划。按文件语言发现 PATH 中的服务器，带取消、超时和进程树关闭。 |
| 语义修改 | CodeMode 用 `tools.edit({semantic_result_ref: receipt.result_ref})` 选择原始 rename 回执。宿主读取、核对版本，沿原多文件 Patch 预检和写入链路执行。版本变化时拒绝整组写入。 |
| 结构查询 | `syntax_query` 用 Python AST 或 JS/TS tree-sitter 查节点、原文件版本和行范围。它提供观察；修改仍通过原 Patch 工具。 |
| 数据输入 | `read_data` 从冻结的 eN passage 或原始完整 read_file 回执解析 quantities、CSV、JSON。保留来源、版本、原始数字字符串和偏移。模型可以在现有 CodeMode 中筛选行、选择字段、组织计算。 |
| 精确计算 | `calculate` 只接受原 read_data / calculate 回执的字段引用。支持求和、减法、乘除、比例、增长率、百分点、均值、加权平均、单位换算和计数。使用 Decimal、明确缺失值策略及 half-even 舍入。 |
| 报告绑定 | 宿主从原 calculate 回执派生 cN 槽，菜单显示完整槽、运算类型、数值和单位。`{{calc:cN}}` 插入原结果与原来源引用；归档重新核对原计算回执。模型不用抄数值或长 ID。 |
| finding 阶段 | 从原任务比较谓词及已提交回执重建阶段，不另存阶段状态。比较完成后移除保存入口，接收处拒绝新的无关保存；完全相同的已提交请求仍可幂等重试。新来源、版本、未覆盖范围或新研究计划可以重新开放阶段。已读内容的子范围不会重新开放。 |

计算仍使用原 CodeMode 执行、权限、子调用和 durable 回执机制。没有第二套脚本运行时或另一个计算结果库。eN/cN 和语义回执选择沿用原引用机制；模型提交的裸数字不能冒充原始计算输入。

CodeMode 中保留外层 Result，才能引用 `result_ref`：

```javascript
const receipt = await tools.read_data({evidence: inputs.evidence, format: 'quantities'});
const rows = unwrap(receipt).data;
const selected = rows.flatMap((row, i) => row.context.includes(inputs.term)
  ? [{result_ref: receipt.result_ref, pointer: '/data/' + i + '/value'}] : []);
return unwrap(await tools.calculate({operation: 'sum', operands: selected, scale: 6}));
```

JSON pointer 只选择 `/data/...`，不能拼接 result_ref 或选择来源元数据。增长率的两个操作数顺序是 `[baseline, new]`；percentage 是部分除以总体，percentage_points 是两个百分数之差。

## 实机证据和失败分类

所有首次请求、模型响应、工具回执和复跑结果保留在 [本轮证据目录](../../.e2e-artifacts/codemode-analysis-20261122/)。不同目录独立保存，没有覆盖失败结果。调整了任务提示的运行不作为同题胜率对照。

| 记录目录 | 结果与根因 |
| --- | --- |
| `lsp-first` | 首次 TS 查询受语言服务器 syntax server 路由影响，定义、引用和 rename 缺少跨文件结果。初始化改用语义服务后，`lsp-semantic-rerun.json` 得到两个文件的结果。 |
| `lsp-wire-first` | 原始线缆记录发现 Pyright 的编码盘符 URI 与请求 URI 字符串不同，诊断被漏接。也发现 TS 局部导入 rename 合法返回 `total as doubled`，不能把服务器替换文本强制等同于新名字。 |
| `lsp-wire-rerun` | 统一文件 URI 路径比较后，Python 类型错误诊断、定义、引用、hover 和 TS 查询均得到真实服务器回执。推送诊断明确标为 `complete: false`。 |
| `stage-first` | 通过：2 项比较、0 项无比较字段保存，自动补搜 1 次。 |
| `stage-final` | 模型把比较绑定到错误来源，未形成预期冲突和补搜，验收失败；没有放宽来源断言。 |
| `findings-explicit` | 失败：模型先把两项比较保存到同一来源，再读取另一来源；新证据按阶段合同重新开放保存，产生 4 项带比较字段的保存。不是最初“完成后退回无比较字段合同”的问题。 |
| `findings-read-first` | 在任务中明确要求先读两源后，通过：2 项比较各绑定正确来源、0 项无比较字段保存、补搜恰好 1 次。 |
| `calculation-first`、`calculation-rerun` | 模型混淆 Result/result_ref、数据字段和 JSON pointer，并生成语法错误。宿主拒绝非法输入。补充真实返回 schema、原始数量局部上下文和正确的外层回执示例。 |
| `calculation-example-rerun` | 算出正确增长率，但报告把长回执 ID 当槽名，按合同拒绝；没有自动替换或猜测槽。 |
| `calculation-slots-rerun` | 完整槽已可复制，但模型选中 count 的“2 items”，称其为增长率。验收失败；因此菜单补充运算类型，测试任务明确选择 growth_rate 槽。 |
| `calculation-operation-rerun` | 端到端通过：原 passage → 模型编写 CodeMode → Decimal 增长率 `29.166667 %` → 原槽插入报告并归档。此运行中模型先发出两次单操作数 growth_rate 调用，均被正确拒绝，再用两个操作数成功；失败子回执保留。 |
| `rename-first` | 入口把“Apply its read-only plan through tools.edit”误判为整项任务只读，移除 edit。修复仅针对这类明确应用计划的局部描述，并回归真正只读任务仍禁止写入。 |
| `rename-rerun` | 通过：模型原生编写 CodeMode，引用语义计划修改两个 TS 文件；最终版本 npm test / tsc 通过，CRLF 保留。 |
| `rename-final` | 模型丢失外层 Result，提交 undefined 的 semantic_result_ref；宿主拒绝，文件未写入。 |
| `rename-receipt-example` | 补充保留外层回执的工具示例后通过：两个文件各写一次、最终 TypeScript 检查通过、无失败工具回执。 |

以上模型调用串行执行，运行中的生产源码摘要保持稳定。一次扩展 pytest 运行期间修改了生产入口代码，导致其中 1 个夹具的 source_stable 检查失败；该运行保留于 `regression-first.log`，冻结源码复跑的 `regression-rerun.log` 为 104 项全部通过。

## 回归与类型检查

没有运行全量 pytest。测试覆盖合法/非法引用、精确小数、比例与百分点、权重来源、缺失值、零分母、舍入、表格原版本绑定、报告归档、防篡改、真实 JS/TS/Python 语言服务器、语义计划过期零写入、阶段关闭与重新开放、旧快照恢复、原 CodeMode/Patch 恢复及入口只读权限。

| 检查 | 结果 / 记录 |
| --- | --- |
| 主生产回归 | `regression-rerun.log`：104 passed |
| 工具合同、归档、Patch、入口授权扩展 | `contracts-archive-verified.log`：154 passed，24 subtests passed |
| 最终语义、计算及阶段定向组 | `semantic-calculation-verified.log`：48 passed |
| 全树 Ruff | `ruff-final.log`：通过 |
| 全生产树 mypy / pyrefly / ty | Python 3.11 目标检查通过，mypy 覆盖 749 个生产模块；见 `gates.json` 与各日志 |
| 类型精度及修改测试/工具的严格 mypy | 通过；修改 Python 文件未使用 Any、cast、type: ignore 或关闭检查 |

回归首次暴露的旧替身签名、快照版本断言、固定工具清单和旧 JsonToolCodec 验证器，已按当前生产合同修订。可选活动提示沿用原通用显示；工具示例使用实际 ToolSpec 校验。没有为旧测试恢复旧生产合同。

## 边界

- 算术和引用绑定通过，不等于公式、实体、时间、样本或分母的语义正确；结果明确保留 `interpretation_unreviewed`。上面的错误选槽和错误来源绑定就是实际反例。
- 现有 CodeMode 可以执行模型编写的数据处理代码；此轮没有新增自动 Python/Pandas 运行器。普通 JS 自算结果不会自动升级为带来源的研究证据。
- 语义查询和修改有文件、节点、超时、完整读取及最多 8 个修改文件的边界。当前查询以行号及该行唯一的简单标识符定位；rename 是具体 codemod，结构查询不是任意 AST 自动改写系统。
- Python 使用 pyright-langserver，JS/TS 使用 typescript-language-server；需将服务器放入 PATH，且位于查询项目之外。本机实测使用外部于夹具项目的 `.e2e-artifacts/followup-lsp-tools/node_modules/.bin` 临时 PATH，没有给生产进程永久改 PATH。
- gopls、rust-analyzer 在本机未安装，已接发现及请求适配，但未宣称 Go/Rust 实机通过。
- 没有做跨供应商、第二模型或七个参考项目的同题胜率评测。几次成功实机不代表消除了模型输出错误；全部失败保留供后续比较。
