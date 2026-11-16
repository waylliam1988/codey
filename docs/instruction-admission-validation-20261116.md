# 任务指令与引用数据边界验证（2026-11-16）

基线：`2f8ac638`。本轮只修改任务准入的文本判断，不增加执行通道、回执格式或恢复状态。

## 修改

- 点击阶段和自由编程要求共用保持原始偏移的指令视图，屏蔽引号、行内代码、代码块和 Markdown 引用中的数据。明确调用的行内工具名仍可作为操作识别。
- 点击次数只从指令视图计算；等待条件从原文提取，保留条件中的分号、中文引号、连续反引号及英文缩写。等待值与数据屏蔽共用引用边界，避免引用内的 `Don't` / `Don’t` 造成截断或虚假阶段。
- 路径入口不再因否定或引用的自由 JS 指令关闭；识别已有指令形式的否定，忽略引用旧指令，并按最后一条实际正向或否定要求判定。`JS` 不再误匹配 `JSON`、`JSX`。
- 新增 [准入回归](../tests/test_task_instruction_admission.py)，覆盖阶段编号、否定、引用、真实回执完成检查及冻结快照恢复后的文件菜单。

## 自动验证

完整原始记录保存在 [.e2e-artifacts/instruction-admission-20261116](../.e2e-artifacts/instruction-admission-20261116)。未运行全量 pytest，未创建或复制 worktree。

| 检查 | 结果 / 记录 |
| --- | --- |
| 原实现的首批红测 | 36 failed / 11 passed，`baseline.log` |
| 第一轮聚焦回归 | 3 failed / 175 passed，`focused-01.log` |
| 补充缩写边界红测 | 4 failed / 61 passed，`quote-boundary-red.log` |
| 修复后的聚焦回归 | 196 passed，`quote-boundary-green.log` |
| 最终生产回归，19 个文件 | 657 passed，包含新增准入用例 65 项；`production-regression-final.log`，文件清单 `production-tests.txt` |
| 全树 Ruff | 通过，`ruff-final.log` |
| 全生产树及新增测试 mypy | 697 files 通过，修改的四个模块额外禁止显式和隐式 Any，`mypy-final.log` |
| 同范围 Pyrefly | 0 errors；原有 35 suppressed、157 warnings 保留，无新增抑制，`pyrefly-final.log` |
| 同范围 ty | 通过，`ty-final.log` |
| kernel 类型精度门禁 | 通过，`precision-final.log` |

第一轮聚焦失败中的两个是生产解析遗漏：紧贴中文的单引号，以及引用内的英文缩写。另一个是新增测试把 Markdown 引用直接接在上一行后面，实际不构成引用；仅修正测试换行，没有放宽生产判定。后续补充探针又锁住了弯引号缩写和等待值提取的同类边界，并共用生产解析修复。

部分测试退出时出现 RAM 盘清理 `WinError 3` 警告，断言均通过；日志保留。未修改不相关的清理代码。

## 实机与限制

使用本地 KoboldCpp 的 `Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M`，完整 17 项矩阵未删减。原始请求、模型回复、事件和正式回执均保留。

首轮 [full-surface-01](../.e2e-artifacts/instruction-admission-20261116/full-surface-01/summary.json) 为 **15/17**，源码前后摘要均为 `0d9f5e17883103ca4d0a7c112e06f9c13346fc037fdb5df940cfad5b9d9e25fd`。补齐缩写边界后重新冻结源码运行完整矩阵，首轮结果没有被覆盖。

- `structured` 首轮失败：模型三次把包含方括号、角色和标签的整行 snapshot 文本当作 `ref` 传入，正式回执正确拒绝；最终 `no_progress`。属于模型参数错误，没有放宽引用合同。
- `stale_ref` 首轮失败：原始 wire request 明确要求填写后先尝试旧引用；模型直接使用新引用完成业务读取，正式回执没有该负例尝试。属于模型漏做验收步骤。真实旧引用注入场景通过，不能把这项失败记为通过。

最终冻结源码的 [full-surface-02](../.e2e-artifacts/instruction-admission-20261116/full-surface-02/summary.json) 为 **16/17**，源码前后摘要均为 `5972ddff4b41f231f39c9156359cdd4748b29fcaf0b55432ac146ce8f69d5340`。`structured` 复跑通过；`stale_ref` 仍失败，原始请求确认包含同一要求，正式回执仍无旧引用尝试，详见该场景的 `failure-audit.json`。两轮通过率独立记录，不合并。

最终矩阵覆盖 Chat、strict Research、顺序 Research、Hybrid、各模式 CodeMode、结构化输出、快照恢复及重复读取、真实文件冲突恢复、上传、下载、来源失败和真实旧引用注入。除模型漏做负例步骤外，全部通过；未发现需要迁就过时测试或模型错误的执行合同问题。

新增 [定向实机](../.e2e-artifacts/instruction-admission-20261116/quoted-task-01/summary.json) 首次通过，与最终矩阵使用相同冻结源码。按钮名改为“点击查询”，任务同时包含 `Do not write your own JavaScript.` 和引用的旧正向 JS 指令。恢复快照 6 次；正式回执中的导航、填写、点击、等待、正文读取、文件完整读取、编辑各一次，模型实际只提交 `{"tool":"codemode","args":{"path":"result.json"}}` 完成文件阶段，最终文件和答案事实正确。用例及完整矩阵均复用原生产执行器，没有替代执行链。

修复针对新任务准入。已有快照仍恢复原冻结合同，不静默重写历史阶段。文本判断覆盖明确的指令形式和引用语法，不是任意自然语言语义解析器；受控实机样本也不能证明模型成功率稳定提升。
