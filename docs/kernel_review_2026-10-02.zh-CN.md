# 内核、协议与恢复边界审查（2026-10-02）

基线：`aff30e0`。范围包含工作区原有的 canonical edit 修改，以及本轮审查和修复。
本报告在最终两版本全量测试结束后写入。随后暂存检查只修正一份新 provider 测试末尾的多余空行，已验证前后 AST 完全相同；无行为改动。

## 结论

本轮已定位并复现的问题已修复。模型工具循环、参数验证、事实投影和完成入口继续收口，生产代码相对基线 **+281 / -999，净减少 718 行**。没有新增生产模块或另一套任务循环。

这支持“本轮架构继续收敛”，不能据此宣布整个项目百分之百无 bug 或死代码。真实 UI E2E、浏览器模型完整任务和模型延迟/token 本轮未测；原 GitHub 卡死的解释器底层原因也未得到确定性复现。

## 1. 函数职责与确认修复

| 文件 / 函数 | 问题与修改 | 回归证据 |
| --- | --- | --- |
| `runtime/core/cancellation.py::_pump_stream / _wait_readers / wait_process` | 以 pump 的 `finished` 事件代表 EOF 或读取失败；共用一个单调时钟预算，去掉管道排空对 native `Thread.join` 的依赖。线程退出善后不再被误认为读取尚未完成。活跃读取器的管道不能由另一线程阻塞关闭 | `test_process_capture_completion_ownership.py`；`test_bounded_capture_and_context.py` |
| `providers/local_openai.py::_assistant_turn_or_fail_closed` | 完整文本工具帧归一化后，原先调用会在 native 路径丢失。现在同步生成调用和历史中的同一 call id；真实 native 调用优先；截断帧不提升；畸形 native 批次的文本不能另作 JSON done 执行 | `test_local_provider_canonical_turn_boundary.py`；native delivery / length continuation 回归 |
| `operations/kernel_provenance.py::sync_workspace_state_after_edit / _session_workspace_identity` | 删除没有版本存储时扫描指纹、配上旧版本号的测试兼容路径。缺失存储不伪造身份；版本号不再用 `int()` 洗成合法值 | `test_workspace_no_store_never_fabricates_identity.py` |
| `operations/kernel_execution.py::_settle_unconfirmed_edit` | 缺存储、bump 失败、非法 changed 或证明附加失败，统一结算一次；持久收据记录 `workspace_unconfirmed=True`，记录实际编辑并清空当前身份，阻止同批后续执行和跨轮借用旧验证身份 | 同上：真实文件、真实日志、两次重启与完成门断言 |
| `operations/kernel_facts.py::record_facts_for_result` | 未确认的编辑在 error settlement 恢复时仍投影为编辑事实。持久记录使用的执行器名称 `read` 也经现有 `canonical_tool_name` 投影为已读事实，避免恢复后错误要求重读 | 同上；`test_manual_write_benchmarks_have_real_identity.py` 的真实恢复 smoke |
| `operations/task_loop.py::_handle_done_reply` | 只接受 `verdict.complete is True`；合并重复的 gate 异常处理，不增加第二套完成判定 | `test_kernel_readability_and_exact_done.py` |
| `toolchain/tool_args_repair.py::_normalize_edit` | 现有文件只接受 `replacements[{old_string,new_string}]`，新文件接受 content。删除顶层 old/new、JSON 字符串列表、单 dict 包装及编辑别名修复。删除内容允许空 new_string，非法结构执行前拒绝 | `test_edit_normalizer_has_no_parallel_contract.py`；`test_native_edit_schema_matches_kernel_contract.py`；codec / protocol / kernel 回归 |
| `tests/manual/real_local_ab.py::_metrics / _run_arm` | 启动但无结果计 unknown，匿名结果不绑定其他调用，同 id 结果去重；验证要求精确 bool 和整数退出码；发送真实 case.task；先创建输出目录 | `test_edit_consumers_and_ab_truth.py`；`test_real_local_ab_helpers.py` |
| 写入类 manual A/B、stress scheduler | 显式接入真实 `WorkspaceRevisionStore`，项目与状态目录分开；不恢复缺存储兜底。恢复 smoke 迁移 canonical edit/read/grep 参数 | `test_manual_write_benchmarks_have_real_identity.py`；真实 completion producer / soak / recovery 回归 |

未确认编辑的处理顺序是 **先持久结算 → 投影编辑事实 → 清空实时身份 → 拒绝继续借用该身份**。恢复时读取真实版本存储的当前身份；不会为旧验证补上新身份，也不会重新写文件。

## 2. run_task_kernel 如何拆分

`run_task_kernel` 从 **228 行降到 165 行**，移除了 `# noqa: C901, PLR0912`，普通 Ruff 门槛通过，配置未放宽。

- `_KernelStartup` 替代难以辨认的 12 元素启动 tuple。
- `_ProviderTurnState` 只持有待交付消息、下一轮 prompt 和有界修复计数。
- `_receive_turn_plan` 负责一次发送、取消、截断处理、解析、trace 和协议修复；不执行工具、不接受完成、不含工具循环。
- `run_task_kernel` 保留唯一循环，按快照 → 接收 → done/approval → 执行 → 结果交付推进。
- `_rebuild_turn_snapshot` 只重建当前快照和 schema。原先每轮渲染随后丢弃的初始 prompt 已删除；工具可用性变化使用同一个冻结快照的名字。

有真实两轮 read → done 测试锁定初始 prompt 只渲染一次，以及 snapshot → send → execute → snapshot → send 的顺序。取消、轮数终止和 native 结果交付继续由原边界负责。

## 3. 删除清单及依据

### 生产代码

删除已无生产消费者的 `AgentLoopSession / LoopProgress / LoopVerification / LoopStagnation / ResolvedLoopConfig / PendingPromptState`、旧 state 的 emit/snapshot、`verification_driver.py`、两个旧执行 facade、旧验证提醒、旧修复上下文渲染、旧编辑块 helper 和旧长输出 externalize helper。

`agents/state.py` 保留实际由内核使用的 LRU / 工具尝试记录。长结果通过正式 managed receipt 测试锁定，不再为旧 helper 保留生产实现。删除无消费者的 Ollama 原生 `/api/chat` codec；实际使用的 OpenAI-compatible Ollama 通信仍保留。

删除 `_local_length_continuation_prompt`、`_consume_local_length_reply`、`_is_native_provider` 等无用转发，以及缺版本存储的身份 shim。没有新增转发 shim 来保持旧名字。

### 测试与手工工具

- `tests/support/kernel_harness.py` 的 479 行旧循环夹具只有两个实际消费者，包含无用恢复路径和过期状态构造。消费者已迁移到正式 `AgentRequest`、真实内核或收据测试。
- `tool_args_repair_simulated_ab.py` 比较退役解析器；`tool_args_repair_live_ab.py` 自身说明两臂都使用当前内核；`tool_args_repair_dialect_pressure_ab.py` 依赖前者及旧编辑方言；`tool_args_repair_smoke.py` 也以历史修复为目标。这四份脚本已删除，历史结果未改写。
- 当前替代覆盖包括 canonical edit 的 JSON/native schema、旧形状执行前拒绝、真实 provider 文本帧提升、截断拒绝、native 交付和当前真实本地 A/B 指标测试。没有声称自动化测试替代了真实模型性能实验。
- `read_before_edit_ab.py` 的旧 baseline patch 一个已经不存在的旧循环函数。移除该臂和 patch/restore 链，保留当前生产 guard 探针；不是让两个相同实现继续生成 A/B 指标。
- 七份写入 benchmark 删除多参数 run_agent 转发，直接构造正式请求；相关入口用真实存储测试锁定，并另测真实 read → edit → run → done。

没有删除异常处理、平台进程管理或网页文本 JSON 适配。这些是当前有效行为，不是历史兼容。正式 effect 记录中的执行器名称映射由现有规范名称解析器处理，不能当作死代码删除。

## 4. TDD 和测试卫生

确认漏洞和删除锁先红后绿。新入口测试最初有一处调用签名写错，已先纠正测试，确认其因缺目标行为变红后才修实现。恢复 smoke 的编辑参数修正后仍失败，进一步定位到真实已读事实投影漏洞；没有通过插入重读回复来绕开它。

旧测试只在正式契约改变时迁移：例如为成功编辑场景创建真实项目与版本存储、使用 canonical 参数、patch 正式所有者。仍保留实际文件内容、拒绝执行、退出码、原 call id、重启幂等和完成证明等断言。未增加 skip、未放宽真假或身份校验，也未重新生成冻结 parity baseline 来掩盖差异。

## 5. Python 3.13 CI：可确认与尚不能确认的部分

已读取原始 GitHub 日志：

- [0144e54 失败运行](https://github.com/waylliam1988/codey/actions/runs/36865863825)：Windows Python 3.13 在外部管道持有者测试中，pump 停在 read，主线程停在 `Thread.join`；120 秒 dump 后直到 45 分钟任务被取消。3.11、3.12 成功。
- [aff30e0 成功运行](https://github.com/waylliam1988/codey/actions/runs/36936058724)：成功。两个提交的 cancellation、该测试和 CI 工作流没有差异，不能把后者成功归因于一个不存在的管道修复。

本地使用官方 Windows Python **3.13.15**，原外部持有者场景、bounded capture 文件以及 CI 顺序前缀都通过；没有确定性重现原来的 native join 卡死。因此不能宣称查明了 CPython 底层 bug。

本轮确实复现并修复了“EOF 已完成但线程尚未结束，错误报 drain timeout”的生命周期缺陷，并删除原卡死位置的 native join 依赖。外部持有者测试放在子进程中，保留断言，使用 10 秒 faulthandler 强制退出和 15 秒父进程 watchdog，避免再次把整个 pytest 挂住。

若外部进程一直持有管道，活跃 reader 仍只能被放弃并记录不完整清理，不能安全地跨线程强制关闭其 BufferedReader。本轮没有把这一限制包装为清理成功。托管 CI 的后续结果仍应单独观察。

3.13 本地运行使用嵌入发行版、现有纯 Python 依赖与匹配 3.13 的编译依赖，不等同于重新创建全部 GitHub runner 环境。

## 6. 最终验证

全部代码和测试修改完成后，最终全量：

| 环境 | 结果 | 时间 |
| --- | --- | --- |
| Windows / Python 3.12.8 | **6475 passed，29 skipped，1488 subtests passed** | 447.36 秒 |
| Windows / 官方 Python 3.13.15 | **6475 passed，29 skipped，1488 subtests passed** | 441.12 秒 |

命令为 `python -m pytest -q -o faulthandler_timeout=120 -rs`；嵌入版通过本地 bootstrap 运行等价参数。两轮最终测试的 TEMP/TMP 分别为仓库外独立目录，均零失败。

`ruff check .`、compileall、JavaScript syntax、`git diff --check` 通过。29 项跳过涉及 Windows 的符号链接权限/POSIX 能力，以及 opt-in UI 浏览器测试；没有将原超时测试加入跳过。

中间失败没有省略：两版本一轮各有 10 个过期接线/测试问题；随后出现过并行运行共享系统临时目录造成的清理断言冲突。我将隔离目录误放工作区，又导致各 13 个场景把夹具识别为 Git 仓库，属于测试环境配置失误。改到仓库外后，13 个受影响场景和最终全量均通过，没有为环境问题修改生产实现。

## 7. 数学验证的准确范围

现有脚本已重跑，83 项通过：

```powershell
python -m pytest tests/test_kernel_finite_state_invariants.py tests/test_recovery_trace_invariants.py -q -s
```

- 交付协议：一个 safe batch、两个 provider effect id，BFS 达到固定点，**44 个状态 / 134 条合法边**。遍历每条允许转移，检查 delivered/abandoned 互斥、交付来源合法、终态不可逆等不变量。
- 授权：**512 个 grant 子集 × 2 种 denial 配置**，检查 JSON/native 可见能力一致和精确 round-trip。两种拒绝是无拒绝和全拒绝，不是所有部分拒绝组合。
- 恢复：**81 条长度三的真实持久恢复轨迹**，检查不同验证历史的事实、交付和完成边界。有限轨迹不证明任意长度。

有限状态检查的逻辑是：初态满足 I；对本模型所有可达状态和允许转移检查 I 保持；穷举达到固定点，所以该有限抽象内的可达状态都满足 I。它不能自动证明实际程序的任意字符串、插件、线程调度、文件系统及网络行为都属于这个抽象。

交付图含重试环，遍历到固定点也不意味着所有真实运行都会终止。任务轮数能约束内核循环，但终止仍以 provider/执行器调用返回或履行超时契约为前提。这是可执行的有限模型验证，不能称作全程序形式化无 bug 证明。

全项目 AST/name-reference 扫描未留下无引用顶层函数候选；这是启发式检查，不能排除动态引用、同名造成的误判、死字段或死分支。已确认的旧路径由删除锁防止回生。

## 8. 性能与验收边界

确定减少了每轮被丢弃的初始 prompt 渲染，移除旧状态/执行链与无用包装。没有新增跨轮工作区扫描缓存、新日志或通用框架。

测试证明本轮受审查边界继续统一，不能从行数和假 provider 测试推断真实模型延迟、token、浏览器顺滑度或所有端到端验收百分之百通过。当前最准确的结论是：**本轮确认问题已修复，结构更紧凑，回归通过；完整程序仍不能被宣称无 bug。**
