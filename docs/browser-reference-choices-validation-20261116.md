# 浏览器控件选择与引用诊断验证（2026-11-16）

基线：`5796b29d`。本轮改动只涉及冻结观察中的控件选择、引用诊断，以及这些选择与原 CodeMode 回执恢复的衔接。

## 修改与边界

- 复用 `BrowserReferences`，从一份完整的冻结控件观察派生唯一、可用的 `{role,name,context?}` 完整对象候选。外层工具与 CodeMode 使用同一参数声明，不能通过分别组合角色、名称、上下文制造不存在的候选。
- 唯一性判断包含禁用的同名邻居；同名同上下文无法消歧时保留实际 string ref。观察缺失或控件截断时继续允许重新观察、扩大观察。程序内部新观察产生的原始 ref 仍可使用。
- 控件展示直接序列化原元素对象，将 `ref` 字段和标签分开。冻结观察存在时清除旧的 `<snapshot ref>` 静态调用示例，参数示例直接取自同一候选集合。
- 执行仍绑定原始 ref，由原执行器检查页面、文档、代次、节点及上下文。没有重新按 DOM 名称找控件，没有截取整行后自动执行，也没有自动重放副作用。
- 实时表中不存在的 ref 先报告 `unknown_ref`；只有正式历史 snapshot 回执证明曾观察到相同 ref 时，才诊断为 `stale_ref`。整行 `[已观察ref] role "name"` 误传诊断为 `invalid_ref_format`，禁用控件单独报告 `disabled_control`。只对明确 `not_delivered` 的失败补充诊断，不从历史观察推荐新的具体目标。

本轮发现并修复三处直接相关的确定性问题：对象 enum 冻结为只读映射后无法生成声明；enum 比较不能处理冻结映射及元组；本轮观察候选变化被误计为回执 repair 的执行能力变化。前两项复用原 schema 冻结、展开及 JSON 等值规则，保留布尔值与数字的区别。最后一项从同一冻结工具合同派生执行能力视图，仍验证权限、参数、输出 schema、原调用参数及回执顺序；仅返回回执的恢复权限没有扩大，正常 durable replay 身份检查保留。

泛化依据是正式观察、唯一性、原节点身份和正式回执；生产代码没有加入网址、标签、语言、测试名称或特定 ref 的分支。随机引用与标签、多语言、重复上下文、控件截断、节点替换和恢复不重复交付由相关回归覆盖。

## 自动验证

原始记录位于 [.e2e-artifacts/browser-reference-choices-20261116](../.e2e-artifacts/browser-reference-choices-20261116)。未运行全量 pytest，未创建或复制 worktree。

| 检查 | 结果 / 记录 |
| --- | --- |
| 首轮针对性回归 | 26 failed / 25 passed，`focused-01.log`；下一轮 9 failed / 42 passed，`focused-02.log`；修正后 51 passed，`focused-03.log` |
| 64 个文件的相关生产回归 | 13 failed / 1,097 passed，共 1,110 项，`production-01.log`；清单 `production-tests.txt` |
| 修正旧脚本后的完整文件复跑 | 本轮全部 10 个修改测试文件共 178 passed，`production-updated-01.log`；清单 `updated-tests.txt` |
| 最后补齐冻结参数示例后的回归 | 控件选择、原引用执行与恢复、冻结 enum 三个文件共 61 passed，`focused-final-01.log` |
| 最终目录与提示合同回归 | 4 个文件共 26 passed，`prompt-contract-final.log` |
| 全树 Ruff | 通过，`ruff-final-02.log` |
| 全生产树及本轮全部修改测试的 mypy | 706 files 通过，`mypy-final-03.log`；本轮修改模块额外禁止显式 Any 和 Any 表达式，配置 `mypy-strict.toml` |
| 同范围 Pyrefly | 0 errors；35 suppressed、158 warnings 保留，无新增抑制，`pyrefly-final-02.log` |
| 同范围 ty | 通过，`ty-final-02.log` |
| kernel 类型精度门禁 | 通过，`type-precision-final.log` |

1,110 项首轮的 13 个失败没有记为通过。核对后分别是：路径简化合同已启用而脚本仍提交 JS、最终答案已要求 `paragraphs` 而脚本仍交 `summary`、阶段一等待被错误预期为应随阶段二点击失效、低层无正式回执却要求推断 stale，以及手工 fixture 只放入 `_memory_results`、未投影正式执行记录。修正对应脚本、fixture 和过时文字断言后，复跑所有修改测试文件；没有为这些失败放宽生产合同。最后的示例修改另行运行受影响回归及完整实机矩阵，未将不同源码的测试次数合并为一次全通过结果。

清除了本轮修改文件中的既有隐式 Any 和两处 `type: ignore`。没有使用 `cast`、新抑制或关闭检查。部分 pytest 进程退出时出现 RAM 盘清理 `WinError 3` 警告，断言通过且进程退出为 0；日志保留，未扩大到不相关清理代码。

## 实机记录

使用本地 KoboldCpp 的 `Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M`，seed `123456789`。各轮均调用原 production entry、kernel 和正式回执链，受控页面用 Chromium 执行。完整 17 项矩阵未删减，覆盖 Chat、strict Research、顺序 Research、Hybrid、各模式 CodeMode、结构化输出、快照恢复、重复读取、真实文件冲突、上传、下载、来源失败和真实旧引用注入。

首轮 [full-surface-01](../.e2e-artifacts/browser-reference-choices-20261116/full-surface-01/summary.json) 为 **16/17**，源码前后摘要均为 `d1df7ed4e23b9f2cdd5ac22db67207278686317b8d7e917a123dba6552c7de34`。唯一失败 `stale_ref` 中，模型跳过任务明确要求的“先尝试旧 ref 并得到拒绝”，直接使用新引用完成业务。原始请求包含该要求，正式回执没有旧引用尝试；该场景的 `failure-audit.json` 保留请求 hash、模型回复与正式调用。没有发现程序把旧 ref 错误执行为新目标。

同源码的 [stale-ref-rerun-01](../.e2e-artifacts/browser-reference-choices-20261116/stale-ref-rerun-01/summary.json) 为 **0/1**，模型仍遗漏同一步骤；原始记录与失败审计分别保留。完整矩阵中的真实 `stale_ref_injected` 场景通过，不用它替代模型主动负例的失败。

同源码额外运行 [whole-row-recovery-01](../.e2e-artifacts/browser-reference-choices-20261116/whole-row-recovery-01/recovery-summary.json)：注入原始整行 ref 错误后，接真实模型继续，并恢复快照 4 次。正式填写回执为两次尝试、一次实际成功；初次 `invalid_ref_format` 且 `not_delivered`。导航、成功填写、点击、等待、正式读取各一次，最终事实正确。模型改用了正确的原始 string ref；这证明该恢复路径可行，不能表述为模型已经普遍采用结构化对象。

最后补齐参数示例后重新冻结源码，最终 [full-surface-02](../.e2e-artifacts/browser-reference-choices-20261116/full-surface-02/summary.json) 为 **16/17**，源码前后摘要均为 `a150088eba2f7e3364d0e402e62fdff3c6cc99f5c0baed90bc19a3627f9aeb95`。唯一失败仍是 `stale_ref` 的模型遗漏；原始请求 4 次携带完整任务，9 条正式回执均无旧引用尝试，详见该场景的 `failure-audit.json`。填写已实际采用冻结观察中的结构化选择，但点击使用新 ref 跳过了负例。这两件事分别记录，不能用业务结果正确替代负例验收。真实旧引用注入通过，其他 15 项也通过。各轮原始记录均保留，没有覆盖或合并通过率。

最终相同源码的 [whole-row-recovery-02](../.e2e-artifacts/browser-reference-choices-20261116/whole-row-recovery-02/recovery-summary.json) 也通过，源码前后摘要与最终矩阵一致。初次整行 ref 被正式诊断为 `invalid_ref_format`，明确未交付；真实模型随后完成任务。恢复快照 4 次，两次填写尝试仅一次实际成功；正式回执中导航、成功填写、点击、等待、正文读取各一次，snapshot 两次，最终事实正确。

## 剩余限制

结构化候选减少抄引用和组合字段的负担，原始 ref 仍是必要能力；模型仍可能提交错误参数或遗漏用户要求的负例步骤。没有增加通用自然语言纠错系统。受控页面、单模型与少量实机运行不能证明任意开放网页上的稳定成功率；候选过多时也仍受既有观察和目录预算限制。
