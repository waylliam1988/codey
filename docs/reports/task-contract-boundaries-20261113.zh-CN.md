# 上传等待、知识提示与发布合同边界修复

本轮沿用现有任务声明、冻结工具合同和正式回执，没有增加执行器。运行时清单通过 `python -m tools.publish_runtime` 重新生成，补入 `chat_knowledge.py`，并同步项目已有的 `tree-sitter-javascript` 依赖。新增回归直接调用当前 checkout 的 `read_runtime`，验证发布清单能够通过正式校验。

上传读回共用一个等待计划：逐条核对 `TaskPolicy.required_checks` 中的参数谓词。已绑定的等待可以满足通用的“成功等待一次”，不能掩盖另一条具体条件。缺少一个文本条件时，schema 要求补充该文本；缺少多个条件、非文本条件或不同超时时，schema 从现有 `browser_wait` 定义提供 `waits` 参数。执行前再次检查完整覆盖，任何等待失败都停止，不继续读取。固定回复回归包含第一条等待成功、第二条超时，以及普通上传直接读回；检查真实子回执。

知识召回及搜索提示改为先读取确切 ID 和 revision，再遵循下一轮 `done` 合同引用。Chat 原有的回执候选筛选、schema 限制和提交校验继续使用同一事实来源。测试覆盖文本与原生协议、直接读取与先搜索后读取，并检查实际发给提供方的请求。

相关旧测试也已修复：测试替身接受任务检查参数；实验层遇到生产输入绑定时保留其合同，上传固定回复使用正式的 observe/select/read；正文恢复夹具把随机标记放进可见正文，并显式传入夹具。没有为了旧实验重新开放原始 JavaScript 上传入口。修改文件中的动态 JSON、SQLite 行及测试边界使用明确类型。

验证记录位于 `.e2e-artifacts/task-contract-boundaries-20261113/`：

- `before.txt`：修改前 6 失败、3 通过。其中知识搜索用例最初误用了测试输出的 `.text`，随后修正为 `.model_text`；其余失败重现发布清单、等待覆盖、知识背景提示和替身签名问题。
- `focused-first.txt`：首批生产合同回归 48 项通过。
- `legacy-before.txt`：7 项旧实验失败；旧上传输入及正文夹具问题均有记录。中间接线复验和原始产物分别保留。
- `production-regression.txt`：较广相关回归 237 通过、1 失败。剩余失败来自本轮更新测试时额外添加的普通文本状态句断言；生产普通文本并不保证该句。已改为检查真实上传回执 `effect_verified=False`、文件名、SHA256、正文和单次读取。
- `regression-final.txt`：最终源码的定向组 79 通过、同一文案断言失败。`legacy-final.txt`：修正该断言后，受影响实验相关组 11 项全部通过。上述集合有重叠，不相加为总数。
- `publication-final.txt`：发布相关 9 项通过。所有 pytest 调用均限定在相关用例。
- `mypy-production-final.txt`：生产全树 680 个文件通过。`any-final.txt`：修改的 10 个 Python 文件额外通过 `--disallow-any-expr`；导入模块另由生产全树检查覆盖。
- `ruff-final.txt`：全仓 Ruff 通过；`kernel-precision-final.txt`：内核类型精度及逃逸检查通过。

首次失败的实验原始请求、回执和汇总另保存于上述目录的 `legacy-*-artifacts`、`regression-*-artifacts` 子目录。实机两轮分别保存，未覆盖首次结果：

| 场景 | 首次完整矩阵 | 最终源码完整矩阵 |
| --- | --- | --- |
| basic | 通过 | 首次编译失败；随后三次新调用导航到抄错的 URL，得到 404 和空控件快照，父调用却返回控制流回放错误；没有 repair_of，失败 |
| structured | 通过 | 操作完成、URL/token 正确，解释漏 173 克和 QUARTZ_851，失败 |
| legacy_structured | 反复对未解包 Result 取 elements，失败 | 通过 |
| reread | 恢复 6 次快照，但仅读正文一次，后续解包错误，失败 | 恢复 10 次快照、读正文两次，通过 |
| stale_ref | 真实拒绝后恢复，通过 | 原始回复把 return 写成回车加 eturn，编译失败；真实拒绝 0 次，未覆盖恢复 |
| stale_edit / Hybrid | 到达版本校验，真实冲突 1 次后恢复，通过 | 编辑凭证传错及后续语法错误；文件未改对、未到版本校验、冲突 0 次，未覆盖恢复 |
| upload | 真实等待及读回，通过 | 通过 |
| download | 通过 | 通过 |
| 普通 strict Research | 通过 | 通过 |
| BrowserResearchSequence 专项 | 通过 | 通过；实际请求出现仅提交合同 |
| source_failure / fixed_failure | 按预期阻断，通过 | 按预期阻断，通过 |

首次矩阵目录为 `.e2e-artifacts/task-contract-boundaries-full-surface-20261113-first/`，结果 10/12。最终矩阵目录为 `.e2e-artifacts/task-contract-boundaries-full-surface-20261113-final/`，结果 8/12。两轮各自的运行前后生产源码摘要一致。没有继续重跑以替换这四项最终失败。

失败分析同时检查了原始提供方回复和正式回执。最终 basic、stale_ref、Hybrid 的错误确实存在于原始模型回复；structured 的缺项存在于最终提交。它们不能算通过，两个恢复专项未达到目标校验点，也不能据此判断恢复机制失效。

`initial-contract-comparison.json` 对比 `4f3e68f3` 留存最终矩阵与本轮两次矩阵：basic、structured、stale_ref、Hybrid 的首轮系统提示、实际 response_format schema、模型和 seed 均一致。该检查仅覆盖首轮合同，不等于完整历史请求相同，也不是提交之间的因果实验。本轮确认确定性合同和回归修复，实机样本仍不足以证明自主执行通过率改善或排除所有提示交互影响。

后续固定回复回归已纠正 basic 的原归因：模型没有申请回执修复；空 ref 的参数拒绝未创建子调用 intent，却已被控制流计入，最终完整性检查盖住了缺少 ref 的直接诊断。原导航和快照回执仍在。复现方法、恢复对照及验证边界见 [固定回复回归报告](task-contract-fixed-replies-20261113.zh-CN.md)。本次后续验证未修改生产代码。
