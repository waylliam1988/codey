# 交付验收接入与普通 Hybrid 续跑修复

这次补上两处接线：已明确的交付要求进入现有完成检查；普通 Hybrid 即使没有工具验收清单，也能在读完网页后收到已有的 JSON 编辑模板。历史错误没有被改写，也没有增加自动猜测文件、字段或业务答案的机制。

修改基线是 `3863ad8f`。先前的 [11 月 10—15 日基线对账](historical-error-reconciliation-20261115.zh-CN.md) 保留原结论；本报告记录后续修改和验证。完整逐项结果与产物路径见 [机器报告](completion-contract-continuation-20261115.json)。

## 修改及实际边界

| 修改 | 现在的行为 | 边界 |
| --- | --- | --- |
| 已知 structured 任务的要求进入 `HeadlessRequest` | 从正式 `browser_read` 正文选取净重和校验码，要求说明包含它们；按现有 schema 检查真实两段；独立回执字段启用 `omit_from_explanation` | 合同由这份任务入口声明，不宣称任意自然语言都已自动转换为合同 |
| gate 使用同一份实际 schema 和本次期望值 | 格式、事实遗漏、重复绑定值均影响 `passed`；更换页面事实后验收仍跟随新数据 | 业务期望值仅用于外部验收；运行时从实际回执选择事实，不硬编码答案 |
| 普通 Hybrid 的续跑提示移出工具清单分支 | 有无 `ToolSuccessRequirement` 都能复用同一份 `JSON_FIELD_EDIT_GUIDANCE` | 仍由模型选择正确文件和字段值；提示本身不会执行编辑 |
| 收紧已有编辑提示的触发条件 | 要求明确的 `project_changes_required`、JSON 任务、工具可用、当前文档的完整成功读取，并且尚无成功编辑；等待或送达不确定时先处理浏览器结果 | 只读任务不被催写；旧页面、截断或未记账的读取不被说成“证据已就绪” |
| 修复数字字符串的部分匹配 | 共用 `_contains_value` 对数字字符串也使用完整数字边界，`173` 不再能被 `173.5`、`-173` 或 `+173` 冒充 | 保留字符串原文和前导零，不进行数值转换或改写回执 |

编辑沿用现有完整读取凭证、局部 JSON 更新与版本检查。没有第二套执行器、独立状态账本或双写。类型检查另发现本轮工具文件中三处既有空容器隐式 `Any`，连同新增的一处补齐了类型；没有使用 `cast`、`type: ignore` 或关闭检查。

主要落点：

- `codey/operations/browser_task_handoff.py`：共用续跑提示及触发条件。
- `codey/policies/tool_requirement.py`：修复从正文选出数字字符串时的匹配边界。
- `tools/browser_production_live_gate.py`：声明已知交付合同，并对齐通过条件。
- `tools/completion_explanation_ab.py`：已有验收器接收实际 schema 和期望事实，复用生产的完整选值匹配规则。
- `tools/browser_full_surface_live_gate.py`：增加无工具清单的普通 Hybrid 场景，保留全部原场景。
- `tests/test_browser_project_handoff.py`、`tests/test_explanation_delivery_production.py`：普通请求、只读反例、原回执复用和只修说明。
- `tests/test_historical_browser_reconciliation.py` 及历史夹具：逐字旧回复与单独补正验证。

两段规则原先只在测试中定义；本次移至场景入口，测试复用它。没有复制一份仅供测试使用的合同。

## 历史错误原样重放

在 11 月 10—15 日对账材料中，重放上轮选出的 **8 条问题轨迹、31 条原始回复**。逐项核对原始 provider 文件 SHA256，并将夹具中的回复与原始 wire 内容逐字比较。没有新模型调用，没有修写旧 JavaScript、答案或旧引用。当前生产任务与受控 Chromium 页面用于执行；不是恢复旧会话身份，也没有将全部 2,415 份历史 provider 文件重新执行。

| 旧错误 | 本次原样重放 | 归类 |
| --- | --- | --- |
| 11 日、12 日把读页字符串当 `.text` 对象 | 各四次程序失败，仍为 `no_progress` | 报错，仍需模型改程序 |
| 13 日编译错误后抄错 URL、空 ref | `no_progress` | 报错，仍需模型修正调用 |
| 13 日 structured 说明漏事实、字面换行并重复字段 | 不再 `done`；格式检查失败，原回复用尽后 `max_turns` | 拦住错误交卷，仍需模型补说明 |
| 13 日 stale-ref 程序编译失败 | `no_progress` | 报错，未到真实旧引用检查 |
| 13 日 Hybrid 传错读取对象 | `no_progress`，没有写坏文件 | 安全拒写，仍需模型采用正确模板 |
| 14 日循环提前退出、按钮 ref 为空 | 四次缺 ref，仍为 `no_progress` | 报错，仍需模型修正选择 |
| 15 日字面换行冒充分段，并重复 URL/token | 不再 `done`；格式检查失败，原回复用尽后 `max_turns` | 拦住错误交卷，仍需模型补说明 |

本次原样错误输入没有一条可以据此记为“整个任务自动救回”。两个原先被接受的错误交卷已不再放行；另外六条自由程序错误仍未自动纠正。

另做两个明确区分的补正实验：在 13 日和 15 日的全部原始回复之后，只追加一条正确的两段说明。运行时保留原来的拒绝记录，随后完成；每条轨迹均只点击一次、正式读页一次。**追加说明由测试提供**，证明的是答案修复可以复用已完成工作，不证明模型一定会自行修对。

默认入口的生产回归还覆盖：漏一个事实、旧事实、字面换行、三段文字、重复绑定值，以及把页面事实从 `173 / QUARTZ_851` 改为 `284 / TOPAZ_029`。无需测试额外注入合同，错误都会被拦下，补正后接受；Hybrid 已写好的文件不会重读或重写。

复核时发现正文选出的数字具有字符串类型，旧匹配规则会接受三个错误数字。反例先得到 **3 failed、16 passed**，再修复共用匹配函数，并在真实生产入口增加小数冒充净重的拒绝及只修说明测试。这是本轮相关的确定性问题；修复后重新运行合同／历史回归及完整实机矩阵。

## 完整 full-surface 实机矩阵

使用本机 `KoboldCpp / Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M`、真实 Chromium 和生产 headless/kernel/CodeMode/回执链。页面和故障由既有受控 fixture 提供；未修改模型输出或生产合同。

| 场景 | 首轮 | 该轮失败场景单独重跑 | 数字匹配修复后完整一轮 |
| --- | --- | --- | --- |
| Chat basic | 通过 | 未重跑 | **失败** |
| structured 完整交付合同 | 通过 | 未重跑 | **失败，错误交卷被拒绝** |
| legacy structured | 通过 | 未重跑 | 通过 |
| 快照恢复与重复读取 | 通过 | 未重跑 | 通过 |
| stale-ref | **失败** | 通过 | **失败** |
| 普通 Hybrid，无工具清单 | **失败** | 通过 | **失败** |
| Hybrid 真实版本冲突与重读恢复 | 通过 | 未重跑 | 通过 |
| 上传 | 通过 | 未重跑 | 通过 |
| 下载 | 通过 | 未重跑 | 通过 |
| strict Research | 通过 | 未重跑 | 通过 |
| strict Research 声明顺序与提交收敛 | 通过 | 未重跑 | 通过 |
| 来源失败 | 正确 blocked，通过 | 未重跑 | 正确 blocked，通过 |
| 固定采集失败 | 正确 blocked，通过 | 未重跑 | 正确 blocked，通过 |

**首次是 11/13，失败场景另行重跑是 2/2；不合并成“首次 13/13”。** 三轮生产源码摘要均为 `40abd81a911984c97b1a22f1814b56cab0064ba196d39b291d6e21876de24cb4`，每轮前后相同。

数字字符串修复之后，**最终源码完整矩阵为 9/13**。该轮前后源码摘要均为 `d9c2c528c13a4cf8299678bfc42e263a0f5f1ff9840a1e86d4c559355a3202d9`。没有把前轮成功结果替换进这一轮，也没有为了取得通过而修改提示或合同。

已检查两次首次失败的原始 `request.json`、`provider.jsonl` 和正式工具回执：

- **stale-ref：模型遗漏要求。** 直接填写后已经获得新观察，模型选择了当前有效按钮引用，因此没有出现要求验证的旧引用拒绝。业务事实正确，`failed_stale_refs=0`，整项保持失败。重跑才实际完成旧引用拒绝验证。没有放宽 stale-ref 规则。
- **普通 Hybrid：模型漏掉指定调用。** 任务明确列出 `browser_navigate`，但首次模型从预取页面直接开始 snapshot，没有该调用回执，外部验收失败。它确实收到了现有两行模板，原样采用，并把 `result.json` 正确写为本次事实；因此续跑接线有独立正证据，整项仍保持失败。重跑包含指定导航，完整通过。没有给普通请求偷偷加回工具清单。

这两项没有发现生产确定性异常或环境错误。一个模型、受控页面和少量重跑不能代表任意网站上的成功率。

最终一轮的四项失败也逐项检查了原始请求、模型回复和正式回执：

- **basic：模型程序逻辑错误。** 循环找到输入框就 `break`，没有找到按钮，随后反复返回找不到按钮的诊断；只有导航和快照回执，最终 `max_turns`。这是历史同类错误再次出现。
- **structured：模型没有修好答案。** 曾有编译前拒绝，恢复执行后已取得全部工具结果；随后四次说明都只有一个换行，未满足两段要求，并把校验码写成 `851`。运行时持续以 `output_format_invalid` 拒绝，最终 `no_progress`。不能把“拦住错误交卷”记成任务已获救。
- **stale-ref：再次跳过指定验证。** 业务完成，但 `failed_stale_refs=0`，保留失败。
- **普通 Hybrid：模型程序语法错误。** 四次重复 `eturn await tools.browser_wait(...)`，均在编译前拒绝，没有子浏览器动作，更没有读页或编辑回执。未达到本次编辑续跑提示的触发阶段；保留 `no_progress`。

四项均归为模型输出／选择问题，未发现环境故障或需要放宽生产合同的理由。它们与 407 项确定性回归通过并不矛盾：前者测模型能否走通，后者验证既定输入下的生产行为。

首次矩阵：[cc15-live-first](../../.e2e-artifacts/cc15-live-first/summary.json)。两份重跑：[stale-ref](../../.e2e-artifacts/cc15-live-staleref-rerun/summary.json)、[Hybrid](../../.e2e-artifacts/cc15-live-hybrid-rerun/summary.json)。首次失败的动作与回执摘录另保存在 [first-live-failure-evidence.json](../../.e2e-artifacts/continuation-contract-20261115/first-live-failure-evidence.json)。

最终源码矩阵：[cc15-live-final](../../.e2e-artifacts/cc15-live-final/summary.json)。该轮失败的原始回复和回执摘录：[final-live-failure-evidence.json](../../.e2e-artifacts/continuation-contract-20261115/final-live-failure-evidence.json)。

## 回归、静态检查与首轮记录

没有运行全量 pytest。相关用例的逐次结果、最终去重结果及完整产物路径见机器报告中的 `regressions`。验证包括生产入口、真实 Chromium、快照恢复、只修说明不重复动作、只读反例、待确认操作优先级、JSON 局部更新和冲突恢复、复制、上传、下载及 Research 交接。

最终合同／历史／交接回归 **180 passed**，相关生产机制回归 **227 passed**，合计 **407 个不同用例通过**。数字字符串修复后重跑了前一组全部用例；后一组所覆盖的编辑、浏览器执行及 Research 等路径未因此改变。

- 修改前复现：10 failed、20 passed，保存于 `repro.log`，确认缺口及提示触发条件问题。
- 首次聚焦回归：68 passed、2 failed。测试误写了已有拒绝码名称；改为实际的 `answer_text_forbidden`，生产逻辑未为此放宽。
- 首次扩展回归：138 passed、1 failed。历史轨迹同时有多种错误，测试只预期“漏事实”，实际先触发格式／前置工具检查；修正为检查该原始答案确实不合 schema。补正实验另外明确断言运行时出现 `output_format_invalid`。
- 历史与补正验证：17 passed，包含上述失败节点的修正重跑。
- 最后答案和工具验收回归：25 passed。
- 浏览器交接、编辑、复制、上传及 Research 等相关生产回归：227 passed。

全树 Ruff 使用 `python -m ruff check .`。按仓库全生产树类型门禁运行：

```powershell
python -m mypy codey --python-version 3.11
pyrefly check codey --python-version 3.11
ty check codey --python-version 3.11
python -m tools.kernel_type_precision_gate
```

上述检查通过。mypy 覆盖 688 个生产源文件；Pyrefly 为 0 errors，仍有既有的 35 suppressed、158 warnings，不能写成全树无警告。

本轮另对修改的 9 个工具／测试模块运行 mypy、Pyrefly、ty，均通过，Pyrefly 为 0 errors。额外 mypy 首次因工具目录的模块名解析重复而中止，补充 `--explicit-package-bases` 后通过；该调用错误的日志保留。额外 Pyrefly 首次发现四处空容器隐式 `Any`，补齐类型后通过，没有加抑制。

完整日志目录：[continuation-contract-20261115](../../.e2e-artifacts/continuation-contract-20261115/)。所有首次失败、后续修正和实机重跑分别保存。

## 仍需模型做对的部分

Codey 现在能检查这份任务明确声明的交付要求，也会把已有编辑做法递给普通 Hybrid。模型仍需选对来源、文件、字段及业务含义，并采用正确调用；任意 JavaScript 错误、错误来源归属或未声明的语义要求没有因此获得自动修复保证。历史原样错误继续失败的六条应继续保留，不能从后续改进清单注销。
