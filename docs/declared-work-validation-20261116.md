# 声明工作续跑验证（2026-11-16）

基线：`b8fc1529`。生产源码冻结摘要：
`ad0cb86d8abe06f8fb587f83d9ec584ea512643a43e024562c0690f27a51c1c2`。
完整矩阵、`stale_ref` 定向复跑及自由 JS 两轮实机的前后摘要均相同。

## 修改范围

- 等待建议和完成检查共用 `ordered_browser_progress()`。没有前置动作回执时不推荐对应等待；交付不确定时先观察。任务准入保留单次点击的阶段编号，导航等待仍可在阶段 0 执行。
- 声明字段入口根据冻结的编辑规格选择普通字段或全文形式。模型只提供授权路径；固定程序完整读取文件，host 将绑定展开为正式的 `edit(path, content, base_result_ref)`，经过原 kernel、授权、版本检查和正式回执。
- 新绑定固定来源回执。历史调用先恢复原编辑参数；缺少可还原的正式回执时拒绝重新生成。新冲突尝试重新读文件和生成全文。未增加阶段账本、第二套运行时或写入通道。
- 编辑前条件与整个任务完成分开，编辑后验证不阻断编辑入口。证据完整的文件阶段只向模型提供路径入口和已有澄清能力，保留底层冻结的读写能力。
- 自由 JS 失败交接交付已结算动作、当前控件和具体错误；形状诊断与重放授权分开，不自动修改、重放含副作用的程序。
- 声明输出 schema 对应的文件字段可由同一来源绑定组装，保留数字精度。完成检查核对最终显式 JSON 对象中的声明事实；多个文件共用字段名时要求明确文件身份，不猜测归属。历史示例块不当作最终 JSON 断言；不检查任意自然语言数字。

历史兼容夹具 [declared_fields_snapshot_b8fc1529.json](../tests/fixtures/declared_fields_snapshot_b8fc1529.json) 是修改生产代码前从该基线导出的快照，保留原模型入口描述和合同。

## 自动验证

未运行全量 pytest，未创建或复制 worktree。

| 检查 | 结果 | 原始记录 |
| --- | --- | --- |
| 相关生产回归，21 个测试文件 | 459 passed | [production-05.log](../.e2e-artifacts/declared-content-20261116/production-05.log) |
| 准入、来源覆盖、导航绑定及 JS 反馈补充批次 | 244 passed，含与主批次重复的 21 项 | [admission-feedback-final.log](../.e2e-artifacts/declared-content-20261116/admission-feedback-final.log) |
| 导航测试类型修正后的定向确认 | 4 passed，52 deselected | [navigation-fingerprint-final.log](../.e2e-artifacts/declared-content-20261116/navigation-fingerprint-final.log) |
| 全树 Ruff | 通过 | `ruff-final-04.log` |
| 全生产树及修改的测试/工具，mypy | 703 files 通过；修改模块额外启用 `disallow_any_expr`、`disallow_any_explicit` | `types-strict-final-05.log` |
| 同范围 Pyrefly | 0 errors；保留仓库已有 35 suppressed、157 warnings，无新增抑制 | `pyrefly-final-04.log` |
| 同范围 ty | 通过 | `ty-final-04.log` |
| kernel 类型精度门禁 | 通过 | `precision-final.log` |

回归覆盖普通/全文形式、路径和字段变化、小数/高精度数/科学计数法/前导零字符串、真实版本冲突并保留外部新增字段、旧快照恢复、编辑后验证、来源缺失/截断/权限不足、分阶段及分文档等待、最终 JSON 错误事实拒绝。

恢复回归还覆盖：编辑结算后父程序失败或进程中断，来源或文件随后变化；从磁盘正式回执恢复原参数，不二次写入。恢复时原来源已不在进程内存的情况也有覆盖。文件已变化时允许原恢复检查拒绝继续，不能据此重放写入。

调试阶段的首次失败及后续记录全部保存在同一证据目录。发现并修复了阶段准入丢失单次点击编号的问题；旧测试中的不完整正文夹具、过时 JS 入口断言、旧提示变量名及打在错误导入位置的摘要捕获也已修正。原始保存的错误回复没有改写：在当前路径合同下明确拒绝旧 JS 文件输入，历史冻结快照仍保留原合同。

部分测试退出时产生 RAM 盘清理 `WinError 3` 警告，断言均通过；原日志保留。

## 冻结源码实机

使用本地 KoboldCpp 的 `Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M`，原 17 项矩阵未删减。

| 场景 | 首轮 | 定向复跑 |
| --- | --- | --- |
| basic | 通过 | — |
| codemode | 通过，保留转义及缺失 timeout 的首次错误 | — |
| structured / legacy_structured | 均通过 | — |
| reread（含快照恢复） | 通过 | — |
| stale_ref | 失败 | 仍失败 |
| hybrid / hybrid_codemode | 均通过 | — |
| stale_edit | 通过，真实冲突 1 次，新读取后恢复 | — |
| upload / download | 均通过 | — |
| strict Research / research_sequence / research_codemode | 均通过 | — |
| source_failure / fixed_failure | 均按合同 blocked，通过限制验收 | — |
| stale_ref_injected | 通过 | — |

首轮 **16/17**，不把复跑合并成首轮通过率。

- [完整首轮结果](../.e2e-artifacts/declared-content-20261116/full-surface-01/summary.json)
- [stale_ref 定向复跑](../.e2e-artifacts/declared-content-20261116/stale-ref-rerun-01/summary.json)

`stale_ref` 两轮都读取到正确业务事实，但模型跳过了任务要求的“填写后先用旧引用尝试一次并接受 stale_ref 拒绝”。原始 wire request 确实包含该要求，正式回执中没有该尝试；这是模型输出不遵循负例步骤，并非运行时未拒绝旧引用。独立的真实旧引用注入场景通过。没有为此放宽引用合同或改变原矩阵。

三个文件场景均实际采用路径入口，普通写入各完整读取、编辑一次；`stale_edit` 两次提供路径，形成两条完整读取和两条正式编辑回执，其中第一条编辑因版本冲突拒绝。正式编辑参数确有 `content` 和原外层读取引用。严格 Research sequence 的正式动作计数为导航、填写、点击、等待、正文读取各一次。

新增自由 JS 用例单列：固定前缀实际填写成功并取得新快照，随后故意访问外层 Result 的 `.elements`；后续均由真实模型生成。

- [首次结果](../.e2e-artifacts/declared-content-20261116/free-js-partial-01/summary.json)：**失败**。模型连续三次重复未解包的 `current.elements`，最终 `no_progress`；导航一次、填写一次，没有重放已完成的填写。
- [同源码复跑](../.e2e-artifacts/declared-content-20261116/free-js-partial-02/summary.json)：**通过**。从当前页面继续，填写、点击、成功等待、正文读取各一次。期间仍出现漏传 `timeout_ms` 的模型错误，由后续步骤恢复。

首轮保存的辅助字段 `partial_success_recovered_without_repeating_fill` 当时仅表达“没有重复填写”的边界，不能代表业务完成；以 `passed=false` 为准。工具脚本现已将该辅助字段同时绑定业务完成结果，原记录未覆盖。

原始 provider 请求/回复、事件、正式回执及两轮结果均保存在 [.e2e-artifacts/declared-content-20261116](../.e2e-artifacts/declared-content-20261116)。未发现这轮稳定实机失败由环境或生产确定性错误引起。失败依据另见 `failure-audit.json`。

## 限制

路径入口移走了明确字段复制中的 JS、抄值、回执传递和全文拼装负担。这些受控小样本不能证明模型成功率稳定提升。自由 JS 仍可能反复输出相同错误，新的诊断不保证模型修正。最终事实检查只覆盖声明输出字段和最终显式 JSON，不是通用自然语言事实纠错。
