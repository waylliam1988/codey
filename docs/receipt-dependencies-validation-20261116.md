# 冻结合同与回执依赖修复验证（2026-11-16）

基线：`d379cc63`。本轮修复五处确定性问题，不改变原授权、文件版本检查、正式回执或执行器。

## 修改

1. 浏览器控件快照显式保存展示版本。历史无版本快照仅在完整生成合同匹配时选择对应展示格式；显式版本、候选或合同不一致仍拒绝。旧格式只参与解码，继续使用原冻结身份与执行链。另修复不兼容快照再次序列化会被盖上当前实现身份的问题，避免恢复过程升级执行兼容性。
2. 等待、正文证据及下一步建议共用页面影响判断。明确 `not_delivered` 的拒绝不清空有效证据；已交付或交付不确定仍按原规则处理。
3. 有序来源的正式观察同时交付前置回执，包括等待和阶段计数所需的点击。路径入口按正式提交顺序冻结这些引用，使 host 可以在相同证据上重现原来源判断，不重取最新值。
4. 需要执行并补读保存输出的任务，即使命令同时被选为验证命令，也必须先完成补读才能进入文件阶段。真正的编辑后验证仍按原分类处理。
5. 最终字段事实检查按文本顺序选择最后完整 JSON 对象，同时支持 JSON 围栏及裸 JSON；其他语言代码围栏不作为字段断言。复用原严格 JSON 解码器，保留数字精度，不把内层对象当作外层文件身份。

## 回归依据

新增 `tests/test_receipt_dependency_regressions.py` 共 29 项，并修正 `tests/test_declared_content_continuation.py` 中错误豁免补读前置条件的旧断言。正例经过真实 `run`、保存输出读取和路径入口；有序全文复制经过原 host/kernel/edit 链及快照恢复。广回归另发现旧字段绑定实验脚本未跟上正式接口，适配了 `tests/test_browser_field_binding_probe.py` 和 `tools/browser_field_binding_probe.py`；没有为实验接口修改生产合同。

旧快照 fixture 完整提取自此前实机 artifact，未由当前生成函数模拟：

```text
.e2e-artifacts/instruction-admission-20261116/full-surface-02/structured/state/managed_outputs/
0e11504157f77fb83ab0f8e6/run_2242f0e755fd4152bc58ece5f37a16a9/
artifact_83ee5afdbec65e74ddbd1e67_2ea495e3a10a2028025d0660.json
```

原 artifact SHA256：`2ea495e3a10a2028025d066029f55b8fd00238f50337ec6a0a5f6c33e1f318c6`。
提取后 fixture 统一使用 LF 换行，SHA256：`c8e6fb4b6e76e9503fb3c9b95aeba938fae29ba4b5df2a6ec68441e3e753c98f`。
旧实现身份不兼容的快照可还原合同，但不会获得当前实现的执行权限。

## 自动验证

全部原始记录保留在 [.e2e-artifacts/receipt-dependencies-20261116](../.e2e-artifacts/receipt-dependencies-20261116)。未运行全量 pytest，未复制或创建 worktree。

| 检查 | 结果与记录 |
| --- | --- |
| 原代码复现 | `baseline-01.log`：10 failed / 6 passed，覆盖五项问题 |
| 最终核心回归 | `focused-05.log`：99 passed，包含全部 29 项新增回归及两个 declared 文件 |
| 72 个相关生产测试文件 | `production-02.log` / `.xml`：1,203 passed、10 failed；清单 `production-tests.txt`，失败全部为旧实验脚本，见下文 |
| 脚本适配后的完整聚焦复跑 | `focused-final.log` / `.xml`：109 passed，包含全部 29 项新增回归、两个 declared 文件及整个字段绑定实验测试文件 |
| 全树 Ruff | 最后 `ruff-final-05.log`：通过；此前记录均保留 |
| 全生产树及本轮三个测试文件、一个工具脚本的 mypy | 最后 `mypy-final-05.log`：700 source files 通过；本轮 13 个模块额外禁止显式 Any 与 Any 表达式，配置 `mypy-strict-final.toml` |
| 同范围 Pyrefly | `pyrefly-final-03.log`：0 errors，35 suppressed、158 warnings 保留，无新增抑制 |
| 同范围 ty | `ty-final-03.log`：通过 |
| kernel 类型精度门禁 | `precision-final.log`、`precision-final-02.log`：通过 |

首次聚焦失败与修正记录没有覆盖。`focused-01.log` 的新测试错误要求在来源更新后继续接受旧值，实际原合同正确拒绝；最终回归确认冻结生成的参数仍为原值、没有静默换源且没有写入。`focused-02.log` 的版本库放置错误触发原工作区身份保护，测试已将版本库移至项目外。`focused-03.log`、`focused-04.log` 的测试先误认外层 `run` 为嵌套能力，再使用不允许的命令；改用正式清单和允许的 unittest 命令，未修改生产授权。

`production-01.log` 只留下 5% 进度，进程随会话中断退出，无最终结果，不计为通过。恢复工作后另存 `production-02.log` 和 JUnit 结果。

完整广回归中的 10 个失败均来自旧实验的 `{read,changes}` 调用，与该实验自身已要求的 `base_result_ref` 参数不符，未进入选择器检查，也没有正式编辑。脚本改用原始外层 `read.result_ref`，模型可见示例同步更新。`field-probe-01.log` 保留首次适配后的 2 failed / 8 passed：表格与 JSON 文件已经正确写入，真实冲突均已恢复，但实验只执行 `browser_eval`，缺少当前合同要求的正式来源读取，最终正确拒绝完成。采集程序补上真实 `browser_read`，继续使用原生产完成门。

本轮新增修改的实验脚本同时清除了既有隐式 Any：明确 JSON 解码、provider 回调、环境与模型参数类型、CLI 参数和 generator 返回类型；测试去掉不必要的跨测试模块导入，直接使用明确类型的调用数据。类型检查首次失败与 ty 的旧 contextmanager 注解诊断均保留，未添加抑制或关闭检查。最终只针对脚本实际影响复跑相关文件，未重复整轮广回归，未把不同脚本版本合并为一次全通过结果。生产源码在完整实机矩阵及后续脚本适配期间保持不变。

最终聚焦进程退出码为 0。退出时出现一条 pytest RAM 盘清理 `WinError 3` 警告，原日志保留；没有测试断言失败，未扩大到不相关临时目录清理代码。

## 实机矩阵

使用本地 KoboldCpp 的 `Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M`，seed `123456789`，原 production entry/kernel/正式回执链及受控 Chromium 页面。未删减原矩阵，覆盖 Chat、strict Research、Hybrid、各模式 CodeMode、结构化输出、快照恢复、重复读取、真实版本冲突、上传、下载及来源失败。

首次完整 [full-surface-01](../.e2e-artifacts/receipt-dependencies-20261116/full-surface-01/summary.json) 为 **15/17**。生产源码前后摘要均为 `642798d760082bd6e61615e2e09f4e481ed5f4fea71b6c55bd9ccbd27a548d2c`。

- `reread` 通过，实际恢复快照 6 次。`stale_edit` 通过，正式回执确认 1 次真实版本冲突，并通过完整重读恢复全文写入。上传、下载、strict Research、顺序 Research、Research CodeMode 均通过。
- `stale_ref` 失败：4 次实际 wire 请求包含完整原任务，但模型跳过要求的旧引用负例，使用刷新后的 ref 完成业务。9 条正式调用全部成功，没有旧引用尝试。真实 `stale_ref_injected` 则实际触发 1 次 stale-ref 拒绝后通过，两项分别记录。
- `hybrid_codemode` 失败：12 次实际 wire 请求包含完整原任务。模型反复提交“snapshot 同时取输入框与按钮 ref → 填写 → 使用填写前按钮 ref 点击”的自由 JS，正式回执记录 12 次成功填写、12 次 `stale_ref/not_delivered` 拒绝。代码未检查子调用失败，父程序返回对象；模型重复整段程序直到 `max_turns`。没有点击交付、等待、正文读取或文件写入。此结果不能归因于本轮等待或文件入口修复，也没有为迁就模型输出放宽引用合同。

两项失败的 `raw-audit.json` 与 `failure-classification.md` 保留原请求 hash、模型原始回复及正式调用。相同源码的独立 [stale-ref-rerun-01](../.e2e-artifacts/receipt-dependencies-20261116/stale-ref-rerun-01/summary.json) 为 **0/1**，仍遗漏负例；4 次请求包含完整任务，9 条正式调用均成功。

独立 [hybrid-codemode-rerun-01](../.e2e-artifacts/receipt-dependencies-20261116/hybrid-codemode-rerun-01/summary.json) 为 **1/1**，中途失败仍保留：首次 JS 语法错误未交付子调用；随后填写成功、旧引用点击被拒。模型重新观察后只点击，但漏传等待超时参数，父程序失败。正式反馈指出点击已完成，模型随后仅补等待、正文读取与文件写入。18 条正式回执中，导航、成功填写、成功点击、等待、正文读取、文件写入各一次，最终文件与说明事实正确。该轮的 `raw-audit.json`、`recovery-audit.md` 单列记录，不能覆盖首次失败或表述为自由编程稳定成功。两次重跑的源码前后摘要均与完整矩阵一致。

## 限制

字段检查只覆盖合同明确声明的 JSON 字段，不解释自然语言中的任意数字。快照合同可解码与实现兼容、可执行仍是不同检查。实机采用单一本地模型和受控 Chromium 页面，不能据此推断任意网页上的模型成功率。
