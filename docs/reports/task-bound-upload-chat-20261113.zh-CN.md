# 上传等待、Chat 知识引用与实机矩阵验证

本轮只修改任务合同绑定和对应验证入口，没有创建 worktree，也没有增加执行器或第二套运行时。

## 生产行为

- 上传的结构化 `read` 从冻结 `TaskPolicy.required_checks` 中的 `browser_wait` 参数谓词取得等待条件，复用现有 CodeMode 和浏览器执行链。未声明超时时使用 5000 ms。等待成功后才读正文，失败立即中止；真实子回执仍分别提交。
- 模型补传同一个文本条件时不重复等待。只要求等待、但没有可执行条件时，简化 schema 要求 `wait_for_text`；普通上传仍允许直接读回。没有新增自然语言条件抽取。
- 成功的知识对象读取把实际 ID、revision 写入原有工具回执。Chat 的 `done` schema 和完成校验共用已提交且调用身份一致的读取回执：没有候选时隐藏 `knowledge_refs`，有候选时只接受实际读过的版本组合。搜索结果、失败或未提交的读取、网页来源和旧答案不产生候选。
- 知识库对象存在性、访问范围和版本校验继续保留。读取文档笔记列出的关联对象不等于已经读取那些对象；需要实际读取对象。旧回执如果没有版本元数据，需要重新读取才能产生候选。

## 测试入口

- full-surface 保留普通 strict Research，新增 `research_sequence`。专项声明 `BrowserResearchSequence`，并检查实际模型请求确实出现只开放 `submit_research_answer` 的合同。
- Hybrid 的并发修改从首次读取后移到合法的整体写入即将执行版本校验的位置。参数或凭证在此前被拒绝时不注入。
- 分别报告 `final_file_correct`、`version_check_reached`、`conflict_injected`、`true_stale_conflicts` 和 `conflict_recovery_covered`。最终文件正确但没有真实冲突恢复，专项仍失败。
- 固定回复测试覆盖等待超时不读正文、普通上传直接读取、冻结快照恢复、知识引用的两种协议及真实模型请求、合法写入冲突恢复、早期凭证错误，以及绕过版本校验但文件正确时不能冒充恢复成功。

## 首次记录与限制

修改前的定向复现日志保存在 `.e2e-artifacts/task-contract-baseline-20261113.log`。其中确认了无知识读取仍暴露引用字段，以及省略上传等待参数后没有等待回执；普通上传测试也暴露了工具脚本此前未处理 `explicit_wait=False`。

首次完整矩阵保存在 `.e2e-artifacts/task-contract-full-surface-20261113-first/`，结果为 10/12。`stale_ref` 漏返回值且修复未重放完整子调用，未到达旧引用拒绝；普通 Research 已读取正文，却继续操作而未提交。顺序 Research、Hybrid 真实冲突恢复、上传和下载通过。上传的原始回复只有 `{"upload":{"action":"read"}}`，成功等待由任务声明绑定。该轮期间有代码和脚本修正，不能作为最终源码稳定性证明。

较广回归的首次记录为 315 通过、8 失败，日志保存在 `.e2e-artifacts/task-contract-production-regression-20261113.log`：

- 6 项旧实验上传测试仍发送原始 JavaScript 或实验 steps，遭到改动前已经存在的结构化上传合同拒绝；没有为它们开放旧输入。
- `test_edit_boundary_rejects_new_receipt_for_precomposed_text[B]` 的文件与真实冲突恢复均正确，但测试期间生产源码变化，`source_stable=False` 导致失败；随后对最终代码单独重验。
- `test_b4_repair_delivers_original_body_on_wire_without_reexecution` 替换的 HTML 标记未进入按默认参数固定的实验夹具，断言失败。该旧实验入口未在本轮扩展修复。

所有首次失败、原始请求、正式回执和后续验证均保留在各自目录，没有以重跑覆盖首次结果。

## 最终验证

最终相关生产回归 **213 项全部通过**，没有运行全量 pytest。覆盖新增合同测试，以及已有上传、Chat 提交、网页复制、快照恢复、Research 提交切换、编辑冲突、工具成功谓词和回执恢复。上述因源码变化失败的编辑实验两臂也已重验通过。日志：`.e2e-artifacts/task-contract-production-regression-final-20261113.log`。pytest 退出码为 0；退出清理 RAM 临时目录时有 WinError 3 警告，没有测试失败。

生产全树 mypy：680 个文件通过。修改的工具脚本和测试另以严格 mypy 检查通过；零 Any 精度检查通过。Ruff 结果单独保存在 `.e2e-artifacts/task-contract-ruff-final.log`。

最终完整矩阵保存在 `.e2e-artifacts/task-contract-full-surface-20261113-final/`，仍为 **10/12**，跟踪生产文件的运行前后摘要一致：

| 场景 | 首次 | 最终 |
| --- | --- | --- |
| basic / structured / legacy_structured | 通过 | 通过 |
| reread（含快照恢复） | 通过 | 通过；恢复 6 次快照 |
| stale_ref | 模型漏返回和错误重放，未覆盖拒绝 | 通过；真实拒绝 1 次后恢复 |
| stale_edit / Hybrid | 通过；真实冲突 1 次后恢复 | 模型把 URL 连字符改成下划线，另有 JS 语法错误；未到版本校验点 |
| upload | 通过；模型省略等待参数，程序执行等待 | 模型把控件 ID `file-1-e4512fd6-3` 抄成 `file-1-e451fd6-3` 并重复提交，未到读回阶段 |
| download | 通过 | 通过 |
| 普通 strict Research | 已读取正文但不提交 | 通过 |
| Research sequence | 通过 | 通过；实际请求只开放提交 |
| source_failure / fixed_failure | 预期阻断，通过 | 预期阻断，通过 |

最终两项失败均由原始请求与正式回执确认属于模型输出错误；没有把它们改记为通过，也没有放宽合同或继续重跑以覆盖失败。首次矩阵和最终矩阵的失败集合不同，不能据此宣称自主执行稳定性已解决。

额外旧实验组的 8 项首次失败请求与回执已从 RAM 临时目录保存到 `.e2e-artifacts/task-contract-experimental-first-20261113/`。其中 1 项受源码变化干扰的测试已通过最终重验；另外 6 项旧输入不兼容和 1 项 HTML 夹具问题保留为未修复的实验验证限制，不作为本轮生产回归通过结果的一部分。
