# 普通 Hybrid 浏览器直接工具接入与实机收益

本轮补齐的是普通 Hybrid 的浏览器操作入口。用户引用的 structured 失败属于另一类问题：完成检查已经拦住不合格说明，但模型没有补正。本轮没有修改说明验收、自动补写答案或放宽最终合同。

生产改动集中在三个文件：

- `kernel_protocol.offer_browser_tools` 允许符合原有条件的 Chat、Hybrid 共用现有 snapshot、fill、click、read 入口；已授权的 wait 不再依赖必做工具清单才能直接使用。
- `kernel_protocol._lower_bound_inputs` 复用工具要求的解析器：合同明确要求 CodeMode 父调用时，直接 snapshot/read 也降到现有单步程序。没有父调用要求时保留原行为，不猜参数，不补做额外读取。
- `KernelContext.request_snapshot` 删除重复的 Chat 限制，统一交给上述入口检查资格；当前轮流程提示优先推荐直接工具，需要编程组合的操作仍使用 CodeMode。提示不写入冻结合同，历史快照恢复不必依赖新增提示。
- `provider_receipts.restore_snapshot` 删除用 `ignore_runtime_claims` 推断“必须是 Chat”的限制。恢复仍校验工具名单、冻结规格、授权、strict Research、专用输入绑定和合同文本。

填写、点击、等待和有父调用要求的观察，复用现有固定 CodeMode 程序及同一生产执行链。填写后的新引用、点击目的、声明的等待条件、正式读取和失败续跑全部复用原实现。没有新增执行器、运行时、持久化字段或浏览器状态副本。严格 Research、明确的 `Use codemode only`、控制器拒绝、显式模型工具白名单、上传/复制绑定继续保留原边界。

实机工具脚本的普通任务删除了要求模型编写 `snapshot.elements` 循环的提示；完整矩阵新增独立 Chat CodeMode、Hybrid CodeMode 场景。严格 Research 继续使用原有的 CodeMode 编程提示。普通任务文字明确要求 `browser_wait`，与现有实机验收保持一致；验收仍要求该调用成功。

四组前后对照使用同一生产入口、真实 Chromium、受控网页、同一 Gemma4 12B Q4 模型，temperature=0、seed=123456789，每组最多 12 轮。Chat 有工具清单；普通 Hybrid 不提供必做工具清单。每对任务文本逐字相同，前后各轮内部生产源码摘要一致。换名版本改成台南、Region、Fetch details，净重和校验码也更换。原文、原始请求、模型回复、事件和正式回执保存在 `.e2e-artifacts/browser-direct-hybrid-20261115/{before,after}/`。

| 同题对照 | 模型浏览器 JS 提交：修改前→后 | 模型回复轮数：修改前→后 | 实际结果与限制 |
| --- | --- | --- | --- |
| Chat 原题 | 0→0 | 6→6 | 两次均通过，正式读取各一次 |
| Chat 换名 | 0→0 | 6→6 | 两次均通过，正式读取各一次 |
| Hybrid 原题 | 12→0 | 12→5 | 原来漏 unwrap 后反复使用旧引用，11 次 stale-ref；修改后填写、点击、读取、编辑各一次，文件正确，但漏了验收要求的 wait 调用，仍判失败 |
| Hybrid 换名 | 3→0 | 3→5 | 原来被源码检查的 `Fetch` 字样误报拦住；修改后浏览器步骤各一次，但漏 wait，且把 amount 写成字符串，仍判失败 |

两个 Hybrid 合计浏览器 JavaScript 提交从 15 次降到 0，重复浏览器程序从 11 次降到 0。修改后每个 Hybrid 仍提交一次文件编辑 JavaScript，该项单独计数。程序内部固定 CodeMode 执行不算模型编程。**同题对照的完整验收通过数仍是 2/4，不能宣称端到端通过率提升。**收益证据是浏览器编程、重复填写、重复读取和旧引用循环减少；剩余等待调用和文件字段类型问题没有被这些数字掩盖。

同题对照提示当时只写“等待出现鱼丸”，外部验收却硬性要求调用 browser_wait。上述首次失败保留原样。之后只把实机任务文字补充为明确的工具调用要求，完整矩阵另行记录；这部分不能算作与原题完全相同的重跑。

基线换名版本的 `Fetch` 拒绝发生在执行之前，属于 CodeMode 源码词法检查对字符串的误报，不能归为浏览器操作失败或把未执行的漏 unwrap 当成实测失败。本轮未修改该检查；直接工具路径不需要把按钮文字写入 JavaScript 源码，因而避开了它。后续涉及该词的 CodeMode 任务仍存在限制，任务数据可以按现有合同放在 inputs 中。

该修改按任务种类、已授权工具和既有冻结合同工作；生产代码未加入城市、按钮名、净重、校验码或测试页面特例。换名实机提供了题面外的有限证据，但四组受控样本不足以推断任意网站或统计稳定性；固定 seed 也不保证后端逐字确定性。

验证记录：`.e2e-artifacts/browser-direct-hybrid-20261115/`。完整矩阵首轮和一组回归因用户报告的重启中断，分别保留 `full-surface-first/interrupted.json` 和 `regression-interrupted.json`，不计通过。中断回归日志有一个失败标记但未打印 traceback，不能把它记为成功；后续结果分开保存。已完成的 136 项最终回归保存在 `regression-final.log`。

重启后的首个完整矩阵 `full-surface-post-restart` 为 9/15，内部生产源码摘要一致。其 structured 原始请求与回执证明：顶层 browser_read 读取成功，但两个说明事实合同明确要求 `via=codemode`，观察返回 `tool_parent_missing`；模型随后重复点击、等待、读取，12 轮耗尽，没有进入说明提交。这是本轮发现并修复的执行衔接问题，不能直接归为历史上的说明补正失败，也没有通过放宽父调用合同解决。

对应回归先在未修实现上失败：直接 snapshot/read 的父调用检查两项失败，Hybrid 无声明等待的实际链路无法完成。补上衔接后，44 项接入、父调用、元数据、说明提交和等待边界测试通过，正式 click/read 各一次。此前大组回归 322 项通过、1 项失败，失败是旧用例要求无清单时不提供等待入口；更新该预期并扩展拒绝授权检查后，单模块 21 项通过。最终状态的全部相关模块重新验证，结果另附。

最终代码的完整矩阵 `full-surface-final` 为 **11/15**，内部源码摘要与最终生产源码一致。通过 basic、Chat CodeMode、legacy structured、快照恢复与重复读取、Hybrid CodeMode、上传、下载、plain strict Research、声明顺序 Research、open_url 来源失败、固定浏览器来源失败场景。来源失败两项按合同返回 blocked，属于预期通过。

| 最终未通过场景 | 原始请求和回执确认的原因 | 本轮结论 |
| --- | --- | --- |
| structured | 读取、eval、标签页调用都成功，正式读取仅一次；说明重复独立 token，收到明确纠正提示后仍重复提交 | 父调用衔接已接住；错误说明被拦住，模型补正仍未完成 |
| stale-ref | 模型用了当前名称/角色引用，没有按故障注入任务要求实际提交旧引用；失败旧引用次数为 0 | 故障覆盖未触发，不能称实机拒绝旧引用验证通过 |
| 普通 Hybrid | 浏览器 JS 提交为 0，但漏 browser_wait，并将 amount 写成字符串 `"173"`；外部验收失败 | 无必做清单时的调用遗漏、文件字段类型仍需模型正确选择或明确的额外合同 |
| stale-edit | 已给出正式读页结果和现有编辑模板，模型反复调用 read_tool_result，未发起 edit；版本检查和冲突注入均未触发 | 尚未实测触达冲突恢复，不能据此判定生产版本检查损坏 |

最终 11/15 不能覆盖首个完整矩阵 9/15、同题对照中两个 Hybrid 的失败，也不能证明模型稳定。两轮之间除父调用衔接外，还恢复了 strict Research 的编程提示；Research 从失败到通过不能单独归因于该生产修复。严格 Research 和明确 CodeMode 任务仍依赖模型写出正确 JavaScript。

最终相关生产回归 **356 项全部通过**，日志为 `regression-final-complete.log`，覆盖本轮修改及快照恢复、冻结合同、授权拒绝、原始 CodeMode 路径、上传/下载、引用管理、Research 提交和文件冲突边界。没有运行全量 pytest。运行环境为 Windows / Python 3.12.8；日志末尾有既有 RAM 临时目录清理 WinError 3 警告，pytest 退出成功。

静态检查全部通过，记录在 `parent-bound-final-checks/`：全树 `ruff check .`；生产树 mypy（688 个文件）、Pyrefly、ty，均显式指定 Python 3.11；修改的测试/工具文件也分别通过这三套检查；kernel 类型精度门通过。Pyrefly 生产树报告 0 errors，既有 35 suppressed / 158 warnings 保留，未为本轮新增豁免。修改文件没有 Any、cast、type: ignore 或关闭检查；旧测试中的 json.loads 已改成有明确返回类型的 parse_json_object。

机器结果见同目录 `browser-direct-hybrid-20261115.json`，包含同题原始统计、两轮完整矩阵摘要、生产摘要、检查命令和退出状态。所有首轮、重跑和中断文件各自保留，没有用重跑覆盖原始失败。
