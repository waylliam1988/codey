# 统一任务与浏览器：首批实施和验证记录

> 本文保留首批实施的历史状态与失败记录。async worker、frame、截图入模及后续回归的当前结果见[后续实施与验证](codey-unified-agent-browser-worker-and-validation-20261121.zh-CN.md)；本页“尚未完成”章节不再代表这些实现的当前状态。

日期：2026-11-21，Asia/Taipei。

## 实施边界

以 [函数级方案](codey-unified-agent-browser-function-plan-20261121.zh-CN.md) 为依据，本批实现 P1 的主要能力与组合交付路径，并推进 P2 的父任务资源所有权及显式注入；一并修复已复现的浏览器遮挡、pending 查询恢复、失败来源页面销毁和旧响应状态泄漏问题。

**这不是 P0–P8 全部完成的声明。** BrowserSession 目前仍通过既有同步 driver 使用研究 provider 的资源；依赖反转尚未完成，未引入另一套运行时。独立进程 async worker、完整 effect 调用身份/进展、frame、稳定元素 registry、局部/delta 观察、普通 div 的显式查询结果绑定、原生新输入、截图进入模型请求及完整人工协助/UI 仍需后续批次。

## 生产修改

- 授权继续由冻结 TaskPolicy 决定。明确要求项目修改时保留已授权读写和验证；研究 scope 不再用全局白名单或准备好的报告隔断其他工作。只读、显式否决、项目边界和命令审批继续有效，恢复使用原策略。
- 共用 task_handoff 组装浏览器与研究交接。提交研究报告是交付物；有项目修改义务或已修改项目时，任务继续执行，整体完成仍要求真实项目检查。
- 共同 next_browser_step 从现有声明和回执计算当前阶段的网页动作前提。修复研究等待要求在填写/点击前隐藏其入口的问题，删除研究专用的重复 readiness 计算；恢复提示保留已授权控件，原查询重复派发由实际 delegate 检查。
- 最终组合交付保留项目总结和完整原报告。共享 report_delivery_matches/has_project_delivery 校验冻结交付；研究外层只发布原报告，恢复时仍校验原发布版本及完成 proof。
- ToolResources 记录父任务 browser owner，来源适配器和后台等待借用同一资源。等待不释放父租约，终态提交及关闭幂等释放；显式 browser_session 贯穿 kernel、CodeMode 和 delegate。
- 点击前根据实际 DOM 命中检查遮挡，逐层验证 Shadow DOM 及宿主所在区域，不把宿主本身当作遮罩。明确未点击的 preflight 失败记为 not_delivered；待确认查询只阻止重放原目标，允许其他关闭/恢复控件。换一个展示 ref 不能绕过对同一 backend node 的保护。
- 403、登录及正文读取拒绝后保留页面；失败来源不生成 passage。导航后可以先观察，来源拒绝不能让 snapshot 根本不执行。响应事实绑定 document identity，避免同一个 Page 换文档后继承旧 403。
- 修复解释型 done 附加 memory_candidates 后的冻结快照恢复问题，使用同一个 schema 生成函数严格重建，不放松合同验证。
- 对外层工具与 CodeMode 中完全相同的描述去重，保留完整签名、权限和 discovery；对应实验比较剔除派生 declarations，保留执行定义及权限比较。实验同时隔离生产单步/组合 follow-up 入口。
- 修复 project.verify 已授权、project.write 未授权时，外层工具目录包含 run 而 CodeMode 目录丢失它的不一致；保留显式只读宿主开关。

## 验证与失败记录

所有本轮日志和实机原请求、响应、事件、回执保留在 `.e2e-artifacts/unified-*`；首败与重跑使用不同路径，没有覆盖首次失败。机器可读清单记录各日志 SHA256，见 [验证清单](data/codey-unified-validation-20261121.json)。

已完成的关键定向批次：

| 日志 | 结果与覆盖 |
| --- | --- |
| unified-expanded-first.log | 112 passed；whole research、授权、恢复和协议 |
| unified-recovery-second.log | 19 passed；pending 查询与回执恢复 |
| unified-resources-first.log | 16 passed；父租约、CDP 生命周期和用户浏览器 |
| unified-source-second.log | 40 passed；失败来源保留和来源适配 |
| unified-catalog-first.log | 61 passed；工具发现、绑定和混合能力 |
| unified-kernel-integration-third.log | 10 passed；实际 kernel 执行报告→读文件→编辑→临时项目指定 pytest→整体交付 |
| unified-composition-eighth.log | 42 passed；组合交付和生产研究交接 |
| unified-outer-composition-first.log | 1 passed；真实 ResearchFlowDeps/AppContext、实际文件修改/验证、只发布原报告及发布 proof 校验 |
| unified-research-entry-second.log | 78 passed；whole answer、冷恢复、发布和入口授权 |
| unified-production-fixes-first.log | 41 passed；Shadow DOM 命中、查询恢复、验证授权和 CodeMode 合同 |
| unified-lifecycle-regression-second.log | 44 passed；按原失败顺序执行观察、查询恢复、浏览器执行和绑定，验证租约清理 |
| unified-tools-regression-final-second.log | 56 passed；修改的 follow-up/leaf 实验脚本与权限、回执、计数 |
| unified-memory-explanation-final-first.log | 65 passed；记忆元数据传输和解释型交付合同 |
| unified-frozen-production-a-first.log | **296 passed**；当前最终生产源码，17 个测试文件 |
| unified-frozen-production-b-first.log | **252 passed**；当前最终生产源码，25 个测试文件 |
| unified-action-recovery-third.log | 4 passed；实际 Chromium 跨进程恢复，派发后崩溃、回执后恢复及未知派发不重放 |

扩大回归首次 `unified-regression-first.log` 为 **20 failed, 225 passed**，在 maxfail=20 停止。失败包括旧 strict 模式隔离断言、实验未隔离新生产入口/混入派生描述、旧模式限制断言和 fixture 缺 supports_background。按新生产合同修正测试/工具，不恢复已经删除的任务类型隔离。其后 `unified-repaired-first.log` 为 **1 failed, 65 passed**，剩余失败是实验权限比较仍包括派生的 CodeMode declarations，已继续修复。

扩大回归选择由 `unified-regression-selection.txt` 明确列出 109 个相关文件，初次 collection 为 1746 个测试。第二次扩大运行遇到 3 个旧重复描述断言；已修正并通过该文件全部 17 项。随后单独复现了 Shadow DOM 命中误判（3 failed）和验证授权目录不一致（1 failed）。为修复生产问题，中断仍在使用旧模块的扩大运行，保留 `unified-regression-second.log` 及 `unified-regression-second-interruption.json`，不把中断当作通过。

修复后冻结生产源码，最终按实际影响选择 43 个生产测试文件，collection 为 597 项，并单独回归修改的实验工具。`unified-production-regression-final-first.log` 在 maxfail=15 停止，**15 failed, 270 passed**：2 项仍期待研究标签禁止导航，1 项仍期待仅有 CodeMode 的读取提示，其余 12 项由旧测试未关闭 durable owner、遗留父任务浏览器租约导致同步 Playwright 事件循环留在 worker 上。修正测试断言和 owner 清理后，导航 6 项及上述顺序回归 44 项通过；没有改成提前释放生产父任务浏览器。余下未执行节点独立补跑。

工具扩大首跑 `unified-tools-regression-final-first.log` 为 **8 failed, 73 passed**，8 项均为旧测试直接比较含派生 declarations 的整个 ToolSpec。改用既有 execution_specs 投影比较，参数、执行器、授权、nested inventory 及恢复验证继续比较；失败函数重跑 9 项通过，受影响的两个文件完整重跑 56 项通过。

后续 `unified-production-remaining-final-second.log` 为 **15 failed, 77 passed**。除旧版本/派生描述断言及遗漏的共享测试助手参数迁移外，对原请求和回执的检查确认：共同交接先执行等待收窄，导致已授权的填写、点击根本没有进入执行。修复统一动作前提及恢复工具展示后，最终冻结生产源码的 42 文件、**548 项回归全部通过**。这是两个完整绿色批次的合计，没有把先前失败或中断计入通过数。未运行仓库全量 pytest。

附加交接/依赖回归 `unified-handoff-prerequisites-first.log` 为 **8 failed, 206 passed**。3 项提示仍期待 CodeMode 字符串，3 项在恢复完成后仍假定自有页面不会随 owner 关闭，2 项期待旧活快照可以进入当前执行器。修正测试，继续保留原 fixture 字节和严格版本拒绝；动作次数在 owner 清理前实际观察并保存。`unified-archived-recovery-second.log` 为 **1 failed, 58 passed**，最后一项是派发未知时测试探针不应擅自重接未提交的资源绑定；修正探针后跨进程 4 项全部通过。未知结果仍为失败，没有把观察到动作发生改写成已确认成功。

## 静态检查

- 全树 `python -m ruff check .` 通过。
- 完整生产树 `python -m mypy codey --python-version 3.11` 通过，714 个源文件；`pyrefly check codey --python-version 3.11`、`ty check codey --python-version 3.11` 通过。
- 57 个修改 Python 文件的 pyrefly、ty 检查通过；AST 逐文件检查没有 Any、未标注函数、裸泛型、未闭合 Callable、cast 或类型忽略。kernel type precision gate 通过。未新增检查豁免或降低配置。
- 补充 mypy 命令连同测试依赖检查时仍有 **68 个错误、4 个未修改的历史测试辅助文件**，修改文件自身诊断为 0。完整日志为 `unified-owned-mypy-final-second.log`，没有关闭导入检查掩盖结果。4 个文件为 `tests/support/kernel_runtime.py`、`tests/support/provider_actions.py`、`tests/test_mechanical_surface.py` 和 `tests/test_session_log_receipt_recovery_preserves_facts.py`；本批没有迁移整套历史测试替身的类型。

## 尚未完成的扩大回归

**不能据此声明全部扩大回归通过。** `test_task_entry_cutover.py` 未修改，两个旧提示断言仍要求整个提示不出现现已授权的 save_finding 等工具；另一个 `test_unified_writer_keeps_managed_output_receipt_in_event` 在旧测试适配器的 `wait_tool_task` 中持续等待，保留了 faulthandler 堆栈并中断。尚未完成这组历史 fixture 的迁移与挂起原因的完整验证；没有为满足其工具白名单断言撤销生产授权。

该文件的 49 个节点不计入最终 548 个通过节点，选择文件及中断记录均保留。当前真实 AppContext 和 kernel 的读写、指定测试执行、报告发布场景另有绿色集成记录。这一限制和前述历史测试类型错误仍是后续工作，不能用其他场景通过来抵消。

## 实机结论与限制

本地模型保持同一模型、seed=123456789、temperature=0。自然任务要求浏览器查询、先提交报告、修改 result.json、运行验证和整体交付，用户任务未列工具名。

1. `unified-natural-first/`：失败。原请求估算 24729 tokens，超过 24576 上限；模型同时提交不受现有命令策略允许且引号有误的 python -c。未修改项目文件。
2. `unified-natural-second/`：去除完全重复描述后未再出现上述上下文溢出，但模型重复提交被拒绝的同一命令，最终 no_progress，仍未完成查询和项目修改。

两次实机均不能计为用户目标通过；没有为模型错误放宽命令权限或修改生产合同。脚本化集成通过只证明指定生产调用链，不替代自然模型决策验收。三组自然组合任务及小红书/知乎/PubMed 的完整真实站点验收尚未完成。

本批使用 Windows/Python 3.12，类型检查目标 Python 3.11；没有据此声称其他操作系统或解释器版本已验证。未复制或创建 worktree。
