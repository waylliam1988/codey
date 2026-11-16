# 浏览器部分成功续跑验证（2026-11-16）

基线：`029a020a`。本轮解决自由 JS 已完成填写、随后用旧引用点击失败时，交接仍让模型重复整段程序的问题。继续使用原冻结合同、页面身份、执行记录与正式子回执；没有增加运行时、阶段账本、自动替换引用或副作用重放。

## 修改与边界

1. `browser_next_step.py` 从正式已结算回执推导当前缺失步骤。仅在同文档填写已验证、后续点击因引用格式/未知/失效被明确拒绝且未交付时，说明填写已完成，并要求从点击继续。有有效当前观察则交付原控件候选；没有则先观察。父程序成功不掩盖子调用失败，父程序失败也不抹除已完成动作。交付不确定、禁用控件、未结算动作、页面切换不套用这条恢复规则。
2. Chat、Hybrid 与 strict Research 共用这份临时推导。Research 可以在点击前置要求尚未通过时恢复控件，但等待、读取仍须满足原有前置条件。前一声明阶段证据缺失时不能跳到后一阶段。
3. 明确的恢复或有序证据阶段收窄模型外层工具选择，保留观察、澄清和恢复能力。冻结的底层 `nested_tool_names` 与叶子规格不删减，快照恢复使用原执行能力。普通无序 click/read 清单仍允许业务探索，只提供条件建议，不强制立即读取或禁止后续点击。待核实的页面证据先于文件工作；明确未交付的可选点击不阻塞已具备前置条件的文件入口。
4. 失败反馈交付正式 `settled_actions`，区分已验证、已送达、未交付。它是回执展示，不改变父程序成功语义或重放授权。纠正提示允许复用已经提供的有效当前观察，不再一律要求重新观察。
5. 实机额外发现并修复正文交付缺口：成功父程序中没有 `query_click` 标记的普通 `browser_read` 曾被反馈过滤。JS 仅返回编辑结果时，模型下一轮看不到正式来源事实。现在所有属于该程序的成功正式读取均沿现有有界展示交付，保留来源、引用及完整性/截断信息，不重复读取、不推断或改写最终说明。

业务目标仍由模型选择。原始 ref、页面/代次/节点校验及授权继续由原链执行；本轮不会解析错误 ref 后偷偷执行，也不会修补 JS 再整段重跑。

## 自动回归

新增 `tests/test_browser_partial_continuation.py` 共 30 项，原始失败 JS 提取到 `tests/fixtures/browser_partial_success_reply_20261116.json`。测试不依赖本机 artifact 路径；原失败程序经过真实 Chromium、host/kernel 与回执链，覆盖父成功/失败、观察有无、恢复快照、延迟交接、当前页有效性、拒绝原因、未结算动作、有序多阶段、文件工作优先级及普通正式正文交付。另验证点击已成功、等待参数错误时仅续跑等待和读取，填写、点击不重复。

原始记录保留在 [.e2e-artifacts/browser-partial-continuation-20261116](../.e2e-artifacts/browser-partial-continuation-20261116)。没有运行全量 pytest，没有创建或复制 worktree。没有修改旧回归断言、原矩阵场景或实验执行脚本来适配生产行为。

| 检查 | 结果与记录 |
| --- | --- |
| 首次扩大回归：76 个相关文件 | `production-regressions.log`：1,262 passed / 4 failed，清单 `production-tests.txt`；归因见下文 |
| 菜单、阶段及旧回归修复后 | `post-fix-regressions.log`：308 passed |
| 正式正文与 CodeMode | `plain-read-regressions.log`：80 passed |
| 阶段、字段和回执依赖 | `final-stage-regressions.log`：129 passed |
| 最终源码相关回归 | `final-ordering-regressions.log`：219 passed，包含全部新增回归及浏览器、Research、全文编辑与回执依赖集合 |
| 全树 Ruff | `final-ruff.log`：通过 |
| 全生产树及新增测试 mypy | `final-mypy.log`：697 source files 通过；四个修改生产模块与新测试额外启用 `disallow_any_expr` / `disallow_any_explicit`，配置 `mypy-strict.toml` |
| 同范围 Pyrefly | `final-pyrefly.log`：0 errors；既有 35 suppressed、158 warnings 保留，无新增抑制 |
| 同范围 ty | `final-ty.log`：通过 |
| kernel 类型精度门禁 | `final-type_precision.log`：通过，Any counts 为零 |

各聚焦集合有重叠，不相加为唯一测试数量。类型检查目标 Python 3.11；没有新增 Any、cast、type ignore 或关闭检查。部分通过的 pytest 进程退出时出现既有 RAM 盘清理 `WinError 3` 警告，退出码为 0，原日志保留，未扩大到无关清理代码。

首次四个失败没有被覆盖：

- 配对矩阵测试在运行过程中生产源码仍发生变化，源码稳定性检查正确失败；动作、事实与引用正确，冻结后同一测试通过。属于本轮验证调度错误，未修改稳定性检查。
- 普通无序 click/read 清单下，本轮初版过早隐藏第二次业务点击，属于确定性生产回归。改为仅明确恢复/有序阶段收窄；最终还消除“无序声明必须立即读取”的强提示。
- 待核实查询的两个 delivered/uncertain 测试发现本轮初版将文件入口提前，属于确定性生产回归。使用同一份下一步推导确定优先级，保留页面证据前置要求。

详细过程及红测日志保留在 `production-regression-classification.md`、`paired-matrix-classification.md` 及各原始日志中。

## 完整实机矩阵

模型为本地 KoboldCpp `Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M`，seed `123456789`，受控 Chromium 页面，沿 production entry/kernel/正式回执链执行。原 17 项全部保留：Chat、CodeMode、结构化及旧结构化输出、重复读取/快照恢复、stale-ref 与注入拒绝、上传、下载、Hybrid、强制 Hybrid CodeMode、全文真实版本冲突、strict Research、顺序 Research、Research CodeMode、来源失败及固定来源失败。

| 轮次 | 结果 | 源码稳定性与用途 |
| --- | --- | --- |
| `full-surface-01` | 16/17 | 期间生产源码发生变化，仅作为诊断记录 |
| `full-surface-02` | 15/17 | 固定中间源码；`stale_ref` 和 `hybrid_codemode` 失败 |
| `full-surface-03` | 16/17 | 固定正文交付修复后源码；仅 `stale_ref` 失败 |
| `full-surface-04` | 16/17 | 最终冻结源码；仅 `stale_ref` 遗漏负例失败 |

生产源码摘要：

```text
01 before 8648d8e313e00a13eef31a5646555ac858c2bbb07383a33164926d223ddf5b8f
01 after  2c637892f750a5d78cb58279ea55c36a0db5f8a1d8505575797d356912d03f5e
02 before=after 2c637892f750a5d78cb58279ea55c36a0db5f8a1d8505575797d356912d03f5e
03 before=after 30450819d8644db4a334c61b2a870c5e821341e10f12beece04810f3c4adcd94
04 before=after 7eaa35087d049d876bb60291742a04ccef6ddf4a5cd4c603b6e4edbec1f30e12
```

前三轮的 `stale_ref` 均为模型遗漏任务明确要求的旧引用负例：实际请求含原任务，模型使用填写后当前 ref 完成业务，没有产生预期拒绝回执，因此原验收判失败。没有改变生产合同或测试验收。相同矩阵中的 `stale_ref_injected` 实际触发拒绝并恢复，两者单独记录，不能相互替代。

第二轮 `hybrid_codemode` 文件正确为 `173 / QUARTZ_851`，模型却最终报告 `150 / ABC123XYZ`。最初仅归因于模型事实错误；补查实际 wire 后确认下一轮请求也没有正确来源值，暴露了上述正文交付缺口。模型猜测错误仍是输出错误，但不能把失败仅归于模型。原 `raw-audit.json` 与补充归因保留。修复后第三轮最终 wire 的 exchange 2 已含实际正文，文件和最终说明均正确，导航、填写、成功点击、等待、读取、完整文件读取、编辑各一次，观察两次；详见第三轮 `hybrid_codemode/fact-delivery-audit.json`。

最终 [full-surface-04/summary.json](../.e2e-artifacts/browser-partial-continuation-20261116/full-surface-04/summary.json) 保持原验收，退出码为 1。`stale_ref` 的 4 次 wire 请求均包含原任务，9 条调用全成功，没有旧引用尝试，因此仍为覆盖失败；`raw-audit.json` 和 `failure-classification.md` 已单列保存。没有发现本轮最终矩阵中的环境故障。

其余 16 项全部通过。`reread` 实际恢复快照 6 次；`stale_edit` 正式回执确认 1 次真实版本冲突、完整重读及恢复写入；上传、下载、strict Research、顺序 Research、Research CodeMode 均通过。`stale_ref_injected` 实际触发 1 次拒绝后恢复。`hybrid_codemode` 的文件与最终说明均正确，导航、填写、成功点击、等待、正文读取、完整文件读取、编辑各一次，观察两次；真实 wire 的 exchange 2、3 均有正式来源事实，记录在该目录 `fact-delivery-audit.json`。

最终矩阵的 `codemode` 虽通过，仍保留其中途失败：模型首段 JS 使用填写前按钮 ref，点击被拒后未检查返回值，继续等待而超时。交接后模型只重新观察、点击、等待和读取，没有重复填写或导航；第二次读取及最终事实正确。原始回复、13 条正式回执和 `recovery-audit.md` 保留，不把最终通过表述为从未出错。

## 原始部分成功程序的独立实机续跑

额外用例只预置从原始失败记录提取的首段 JS，保证真实出现“填写成功、旧引用点击未交付”；其后所有回复来自真实模型，不用脚本补答。独立于原矩阵记录，不把原矩阵成功但未触发失败的场景当作恢复收益证明。

`partial-success-live-01` 通过，源码前后均为 `0a188b24547913eb8bc1a1a78f1b45fa1763461d5c906e3c7db6e3a7adaa6299`。正式回执确认导航、填写、成功点击、等待、正文读取、完整文件读取、编辑各一次，观察两次，旧点击拒绝一次；没有重复填写或导航。文件与最终说明正确。原始请求、回复、动作次数及 `recovery-audit.md` 保留。

最终源码 [partial-success-live-02/summary.json](../.e2e-artifacts/browser-partial-continuation-20261116/partial-success-live-02/summary.json) 也通过，源码前后均与最终完整矩阵一致。核对 5 条真实模型回复及 15 条正式回执：预置原始程序父成功、填写已验证、旧引用点击被拒；模型下一轮仅观察并点击，随后分别等待、读取、使用路径入口编辑及提交答案。导航、填写、成功点击、等待、正文读取、完整文件读取、编辑各一次，观察两次，旧点击拒绝一次；没有重复填写或导航，文件与最终说明均正确。原始 `raw-audit.json` 和 `recovery-audit.md` 独立保留。

## 剩余限制

固定续跑建议与工具菜单可以减少模型重建步骤、引用及失败状态的负担，仍不能保证自由 JS 选择一定正确，也不会替模型自动选择业务控件。明确负例被模型跳过时，保留失败，不能把业务答案正确当作覆盖通过。

来源正文交付修复不构成通用自然语言事实检查；仅合同明确声明的结构化字段适合机械校验。实机为单一本地模型与受控页面，不能据此推断开放网页上的稳定成功率。所有首次失败、重跑与不同源码的结果分别保留。
