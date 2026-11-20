# 工具任务第二阶段实现与验证

日期：2026-11-20。起点：`9cdada0d`。直接修改当前工作目录，没有创建或复制 worktree。

## 实现范围

| 能力 | 实现位置与行为 |
| --- | --- |
| 验证恢复顺序 P1 | `TaskQueryFacade.settled_effects()` 在同一提交边界读取父任务和直属工具子任务的正式结算顺序；`verification_history.rebuild_verifications()` 重建验证视图。热恢复、冷恢复和重复恢复使用同一历史，不再先父后子追加。 |
| 撤回旧成功 | `ExecutionEvidence.remove_check_keys()` 与完成检查的原投影入口配合；最新结果未知或不完整时撤回同命令、同目录的旧成功，保留其他证据。 |
| 终端输入 | 原 `run` 增加 `stdin`，原控制目录增加 `task_input`。输入绑定任务和 attempt，复核原授权和当前授权，返回实际交付字节数；重复 effect 返回原收据，未结算的输入不自动重发。 |
| PTY | 原 `run` 增加 `tty/cols/rows`；新增 `task_signal`、`task_resize`。Windows 使用原生 ConPTY，POSIX 使用 PTY；两者都交回原 `wait_process()` 收集输出和清理进程树。 |
| 浏览器后台等待 | 原 `browser_wait` 增加 `run_in_background`。接纳时捕获原页面和文档绑定，仍用原 BrowserWorker，等待在浏览器线程外协调，线程只检查一次条件。 |
| 活资源清理 | `ToolResources` 只保存内存句柄和租约；任务终态、接纳失败、工作结束和会话关闭回收浏览器租约。没有新增持久任务状态、调度器或模型运行时。 |
| CodeMode | 控制操作从同一 ToolSpec 生成目录，子操作回到原内核检查权限、归属和 effect；只读 host 仍拒绝未授权的写入目录。 |
| 发布清单 | 通过原 `tools.publish_runtime` 更新 `codey/runtime.json`，同时补齐既有清单遗漏的 `tool_task_delivery.py`、`edit_text.py`。 |

任务状态仍归原 `TaskCheckpoint`，执行和输入的正式结果仍归原 effect 收据，输出仍归原输出存储。父任务观察子结果时保存引用；反复观察不会重新运行进程或登记研究来源。

## 使用与语义

受管 `run` 可以传入：

```json
{"command":"python -m pytest -q -s","stdin":true,"tty":true,"cols":100,"rows":30,"run_in_background":true}
```

取得 `task_ref` 后，使用原控制目录中的操作：

```json
{"tool":"task_input","args":{"task_ref":"实际引用","data":"原样输入"}}
{"tool":"task_resize","args":{"task_ref":"实际引用","cols":120,"rows":40}}
{"tool":"task_signal","args":{"task_ref":"实际引用","signal":"interrupt"}}
{"tool":"task_output","args":{"task_ref":"实际引用","wait_ms":1000}}
{"tool":"task_cancel","args":{"task_ref":"实际引用","wait_ms":1000}}
```

不自动补换行。Windows 终端的 Enter 通常发送 `\r`；普通管道可以发送 `\n`。管道支持 `eof=true` 关闭输入；终端 EOF 与程序模式有关，因此不把关闭管道伪装成终端 EOF。单次输入最多 65536 个 UTF-8 字节，有界交付，部分交付明确报告不完整。

PTY 的 stdout/stderr 合并，保留原始终端控制序列；`task_output` 标明 `output_mode=terminal`、`streams_merged=true`。中断只表示交付 Ctrl+C，程序可以处理它并继续接收输入；停止则走原取消和进程树清理。

浏览器文字和 selector 等待绑定原文档；同 URL 重载也使绑定失效。URL 条件允许同一 target 导航。丢失原活资源后明确失败，不新开页面代替，不自动重跑。等待成功本身不产生来源证据。

## 首次失败与修正记录

原始日志保存在 `.e2e-artifacts/tool-task-next-stage-20261120/`，没有覆盖首次失败。失败的 pytest RAM 目录也由原测试设施保留。

| 日志 | 分类及处理 |
| --- | --- |
| `01`、`02`、`03` | 首次包含无效指纹 fixture；修正 fixture 后复现 P1：恢复顺序颠倒、最新未知结果保留旧成功。修复生产投影后相关 15 例通过。 |
| `09`、`10`、`11`、`12` | 实机 ConPTY 首次继承宿主标准句柄，输出绕过收集器；按原生终端要求设置标准句柄后修复。之后测试把 `\n` 当 Windows Enter，修正测试为原样 `\r`。真实终端与进程树用例通过。 |
| `13`、`16`、`17` | 新增 unsafe 控制进入 CodeMode 后旧 host 拒绝整个目录，影响已有后台命令；原调用和收据保存在 `16`。修复 host 的允许类别，保留写入授权要求。另修正重复交付测试错误推进新轮次的问题。 |
| `15`、`19`、`20`、`21`、`22` | 浏览器新测试先误用未开放导航入口、再误用缺少 CodeMode executor 的底层入口。改用原 worker 设置页面。随后发现生产接纳错误要求 headless 页面必须有可重连 CDP 端口；改为使用原活资源和已识别文档，4 例通过。 |
| `29`、`30`、`33` | ConPTY 控制序列可插入文字之间，修正测试的可见文本断言，生产保留原输出。旧进程替身缺少正式 `args/pid`，补齐替身和类型；定向 11 例通过。 |
| `34`、`49`、`51` | 两处浏览器扩大回归失败：旧断言未允许共享任务控制同时出现在直接和嵌套目录；另一例在运行期间源码被修改，稳定性检查失败。原始请求与回执、页面状态和摘要证明操作及证据均正确，`51-first-browser-*` 保留原始记录。修正断言并冻结源码后，重验 32 例通过。 |
| `38` 至 `43`、`47`、`50`、`57` | 加严表达式检查先暴露既有动态边界及新增 ctypes 类型问题；清除本轮文件中的问题。导入依赖的既有额外诊断不扩大重构；全部生产代码另跑原全树类型检查。修改文件单独加严后 30 个 Python 文件通过。 |
| `44`、`46` | 类型收紧时的新测试错误解码 frozen tuple，以及 join 替身定位错误；修正测试入口和原 ToolTaskInput 解码，28 例通过。 |
| `48`、`56` | 进程关闭路径的 `isinstance(Popen)` 与原 Popen 工厂 spy 冲突；把 stdin 关闭放回实际创建它的函数，移除类型探测，30 通过、1 跳过。 |
| `65` | 新增页面未打开的回归，复现后台等待抛出未处理 ValueError；接纳边界改为返回正式工具错误，不创建子任务。后续结果见最终回归日志。 |
| `32` | 测试选择命令包含不存在的文件名，没有执行用例；修正选择列表后执行 `34`。 |

没有调用外部模型。终端和 Chromium 使用真实进程；涉及模型轮次的端到端用例使用原 scripted provider，因此不能据此宣称任意真实模型都能稳定完成交互任务。

## 最终验证

最终结果以末轮日志为准，各组有重叠，不把它们相加宣称独立用例总数：

| 范围 | 结果与日志 |
| --- | --- |
| 终端、输入、启动失败清理、恢复及原取消回归 | 30 通过、1 跳过，`56-final-cleanup.txt`。跳过项依赖 POSIX。 |
| 浏览器随访、原工具合同及搜索恢复 | 32 通过，`49-final-browser-recheck.txt`。 |
| 浏览器 worker、真实交互、生产执行、绑定、后台边界、CodeMode 恢复和类型门 | 134 通过，`59-final-browser-replay.txt`。 |
| 验证与完成门扩大回归 | 243 通过、46 个子测试通过，`52-expanded-verification.txt`。 |
| 最终生命周期与合同回归 | 123 通过、77 个子测试通过，`66-final-lifecycle-contracts.txt`，包含页面未打开的错误返回与资源清理。 |
| 全树 Ruff、生产树 mypy / pyrefly / ty | 全树 Ruff 通过；生产树 mypy 检查 689 个文件通过；pyrefly 0 errors，保留其 warnings；ty 通过。日志 `68` 至 `71`、`73`；末次仅校正共享参数的默认等待说明，对应合同回归 `72` 为 9 通过。 |
| 修改文件加严检查 | 30 个 Python 文件无显式 Any、裸泛型、未定参数回调、无类型函数；mypy `disallow-any-expr` 通过。最终结果见 `67-final-precision-all.txt`。 |

没有运行全量 pytest。没有修改类型检查配置、添加 cast、type ignore 或关闭检查。

## 剩余限制

- 本机实测 Windows 11 x64、Python 3.12 和真实 Chromium。ConPTY 原生适配要求 64 位 Windows；POSIX PTY 代码尚未在 Linux/macOS 实机验收。
- 输入和 PTY 接到原受管 `run`，继续受原命令与项目权限检查。原独立 `shell` 人工审批／恢复入口没有改造成交互式任务，不允许通过输入控制绕过审批。
- 浏览器后台只开放原 `browser_wait`；导航、点击、填写、上传和下载仍走原浏览器操作。
- 活进程、headless 页面和未结算的输入不承诺跨服务重启恢复。未知结果明确报告，不自动再次执行或再次输入。
- 这轮没有新增 GUI 终端模拟器；模型通过同源工具目录操作，终端输出保持原始控制序列。
