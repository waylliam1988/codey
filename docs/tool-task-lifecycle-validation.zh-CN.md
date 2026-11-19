# 后台命令生命周期：实施与验证

日期：2026-11-19。直接修改当前工作目录，没有创建或复制 worktree。

## 实施范围

- 原 `run` 通过显式 `run_in_background=true` 启动后台工作；`task_output` 读取状态、增量输出、有界等待；`task_cancel` 请求停止并确认清理状态。
- 接纳回执 A、父任务执行位置、所属工具子任务 T 在现有 durable 事务中提交。执行结果 E 单独结算，A 不会被后来的结果覆盖。
- 复用原调度器、授权、进程执行、输出采集、结果存储和恢复链。工具工作不调用模型、不占父任务模型槽、不更新用户 submission。
- 项目写入与命令执行共用资源协调；查看与停止不等待目标命令释放项目锁。每个进程只有原执行路径负责采集和清理。
- 完成判断要求后台工作已经结束，并已通过控制工具观察终态。接纳成功不算测试通过。
- 崩溃后已有正式结果只收尾；已有执行意图但结果未知的工作记为中断，不自动重跑。
- 冷启动版本：快照版本 10、工具结果内层版本 3；不保留旧快照或旧界面字段兼容。

首版只接通 `run`。未实现终端输入、PTY、浏览器或研究后台操作。

## 实机发现与修复

| 发现 | 分类与处理 |
|---|---|
| headless 接纳后子任务一直 pending | 生产问题：启动现有调度器的工具任务模式，不另建调度器 |
| 取消收尾冻结状态比较不一致 | 生产问题：统一冻结/解冻比较，并允许工具任务合法清理，不取消父 submission |
| 父任务失败时仍有存活工具工作 | 生产问题：请求清理后再保留原始失败原因结束父任务 |
| 接纳回执被展示为普通结果、事件标记为 error | 生产问题：明确 accepted 与 task_output 路径；UI 接纳成功与验证成功分开 |
| 模型读取启动收据后猜测试结果 | 模型操作错误与生产引导缺陷并存：修提示与终态观察门禁；原失败场景修后已通过 |
| “不要改测试”被当作整个项目禁止修改 | 生产问题：识别具名文件/测试保护，保留真正全局只读限制 |
| CodeMode 的 run 使用错误写权限 | 生产问题及旧测试问题：使用原工具 `project.verify` 合同，测试从同一 ToolSpec 取得合同 |
| 精确替换缩进不匹配 | 模型输出错误：原文和原始请求传递正确，拒绝修改正确；补清同源编辑指导和已有版本绑定整文件更新退路 |
| 修改后的测试失败，提示仍建议立即运行验证 | 提示改进：对正式完成判断中的当前验证失败显示先检查、修复的指导；不把失败状态改成通过 |
| 测试项目没有 pytest 配置，推荐了 unittest | 测试项目问题：补 pytest.ini，不改生产验证发现规则迁就样例 |
| 分阶段提示使用“不要不修改代码……”双重否定，触发只读识别 | 测试说明问题：改为清晰的正向要求；保留该次失败，不扩大生产授权解析 |

### 缩进错误的边界证据

`edit-boundary-diagnosis-01.json` 显示：原文件需要 12、16 个前导空格，模型原始 JSON 使用 4、8 个。完整原文确已送达；原始模型参数与工具收到参数完全一致；是真实换行，没有额外转义损坏。程序拒绝修改，文件未被误改。

补清编辑说明后，两次复杂任务的第一次编辑成功，但后续仍有修复不完整、重复运行失败测试、再次提交不匹配替换的问题。因此不能把一次编辑成功称为复杂任务稳定通过，也不能单凭这组实验证明参数规模是唯一原因。本机模型还同时包含量化、训练变体、协议和提示组织等因素。

## 自动验证

日志均保留在 `.e2e-artifacts/tool-task-lifecycle/`；各次编号日志不覆盖。

| 验证 | 结果 |
|---|---|
| 扩大的相关生产回归 `pytest-regression-03.txt` | 1431 passed，6 skipped；进程退出有 RAM 盘清理 WinError 3 警告 |
| 后续任务入口、事件、headless 回归 `pytest-final-entry-events-01.txt` | 204 passed |
| 编辑与后台相关回归 `pytest-edit-related-01.txt` | 114 passed |
| 后台与完成投影最终回归 `pytest-background-final-01.txt` | 52 passed |
| 工具合同、验证命令选择 `pytest-contract-final-01.txt` | 67 passed，22 subtests passed |
| 最后提示改动、真实失败/修改/通过转换、后台与入口 `pytest-context-background-final-02.txt` | 113 passed，22 subtests passed |
| 最终实机任务说明的授权检查 `pytest-fixture-entry-final-01.txt` | 10 passed |
| 修改源码后后台验证、读取结果、正式完成（文本/原生）`pytest-edited-background-completion-01.txt` | 6 passed；使用脚本模型检验生产链路，与真实模型实机结果分开 |
| 全树 Ruff `ruff-final-10.txt` | 通过 |
| 全生产树 mypy `mypy-final-06.txt` | 681 文件通过，目标 Python 3.11 |
| 全生产树 Pyrefly `pyrefly-final-06.txt` | 0 errors；工具另报告 35 suppressed、164 warnings |
| 全生产树 ty `ty-final-06.txt` | 通过，目标 Python 3.11 |
| 修改测试/工具 mypy `mypy-focused-10.txt` | 7 文件通过 |
| 类型精度门禁 `precision-final-03.txt` | 通过 |

没有运行全量 pytest。上述批次有重叠，不能将通过数量相加成独立用例总数。扩大回归后新增的小改动另跑受影响的回归。

首次失败也保留：`pytest-regression-01/02` 包含遗漏运行文件清单、UI 期望、旧 CodeMode fake 合同，以及测试期间修改生产实现导致的跨进程恢复哈希不一致；分别修复实现/测试或在实现固定后重跑。`pytest-observation-01`、`pytest-scoped-auth-01`、`pytest-admission-event-01`、`pytest-edit-boundary-02` 和 `pytest-failed-check-context-01` 保留对应缺陷及引导改动的修前记录。`pytest-context-background-final-01` 的 1 项失败来自上述双重否定测试说明，修测试说明后为 113 项通过。

修改 Python 文件扫描未发现显式 Any、cast、type: ignore 或缺失函数类型标注。外部 JSON 通过现有明确类型边界解码；没有新增检查豁免。

## 本地模型实机记录

后端 `http://127.0.0.1:5001/v1`，模型 `koboldcpp/Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M`。真实 headless 入口、真实模型请求、真实进程和 durable 回执；没有脚本预编排模型动作或伪造工具结果。

每次使用新目录，原始请求/回复、事件、回执、独立检验保存于 `.e2e-artifacts/tool-task-lifecycle/live/<目录>/`。模型任务文本以该目录 manifest 为准。

| 目录 | 结果 |
|---|---|
| success-native-d77f6359 | 通过：后台测试、阅读文件、正式退出码 0 |
| cancel-native-2bb45eac | 通过：读取实时输出、停止慢测试、快测试与主任务继续 |
| failure-native-e19bfdb8、failure-native-34d7bf76 | 失败：模型未观察最终执行结果，读取启动收据后猜结论 |
| failure-native-cb19b3aa | 修后通过：读取正式退出码 1，报告 25 与 26 的断言差异 |
| failure-native-dd29ad69 | 最终代码再次通过相同失败观察场景，正式读取失败后如实报告 |
| cancel-native-7ddd991e | 失败：模型漏后台参数，实际前台运行，未取消 |
| cancel-native-fcc66f34 | 通过：读取输出、取消、继续；完成门禁拒绝过一次未观察终态的提前结束 |
| integration-native-5e0bcb96 | 失败：生产中文局部只读误判，模型前拒绝 |
| integration-native-0ac3a4d7 | 失败：模型反复错误缩进，安全拒绝编辑 |
| integration-native-a21cc6d4、integration-native-c0414e8a | 失败：第一次编辑成功，但模型漏后台参数、修复不完整、重复失败测试 |
| integration-native-b3b4c1d9 | 失败：测试说明双重否定触发只读，模型前拒绝 |
| integration-native-62abf0a1 | 失败：分阶段提示后正确后台启动并读取初次正式失败，但后续修复不完整、重复执行与不匹配编辑，未到诊断取消阶段 |
| integration-native-a1da99c5 | 失败：英文说明明确边界并读过测试后，模型仍漏布尔数量与键冲突，丢失事务提交，独立检查 5 failed、5 passed；后台运行与失败读取正确，未到诊断取消阶段 |
| integration-text-aa210a69 | 按用户最新要求停止可选对照：已后台复现并读到正式失败，在后续生成期间取消主任务并仅停止本次 genkey；stop_reason=stopped。不是生产回归失败，也不能据此比较协议效果 |

复杂样例涵盖 SQLite 库存、重试去重、冲突、非法数量、4 路并发、真实 localhost HTTP、慢合作方测试、诊断取消和取消后健康检查。仅允许修改 checkout.py；不可修改文件逐字节核对；另用独立 pytest 检验修复结果。

任务说明曾从中文长段落改为分阶段说明，再改为明确边界条件的英文阶段说明。这是诊断模型操作能力的辅助实验，不能当作相同提示下的严格对照，也不能用后续成功抹掉之前的失败。

目录名 native 表示请求的模式。对这份本地模型，原有 `safe_native_tools` 会选择 JSON Schema 文本工具协议并关闭思考输出，实际没有使用原生 function calling。普通 text 模式另作对照，以 manifest、原始请求和回执为准。

按用户最后确认的验收口径，代码以相关生产回归、Ruff、明确类型边界及三种类型检查器为提交门槛；不要求该小模型把复杂任务修复完成。已定位的本轮确定性生产问题均修复，相关检查通过。复杂模型任务的失败不算代码通过的证据，也不据此继续放宽生产合同。可选对照已停止，后端确认 idle=1、queue=0。
