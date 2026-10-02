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

## 9. 托管 CI 与真实 UI 后续审查

以上第 1—8 节记录第一次审查结果，已提交为 `bbfcd99c`。推送后继续等待托管 CI、启用真实 Edge，并检查存储异常路径，发现以下问题。这里的结果是本次工作最终状态；旧结果保留为历史。

### 9.1 并发测试：区分锁拒绝与丢失已提交数据

[bbfcd99c 的 CI](https://github.com/waylliam1988/codey/actions/runs/36946735196) 中，Windows 3.11/3.12 与 Linux 文件边界检查通过，3.13 失败于 `test_concurrent_put_baseline_does_not_lose_entries`。二十个线程中的十二个抛出 LockTimeout，只有八个成功；这些线程异常未由主测试显式接收。原管道测试没有再次卡死。

修复 `tests/test_changes.py` 的这个函数：用 `ThreadPoolExecutor(max_workers=2)` 执行全部二十次真实写入；首批两个写者以 Barrier 同时进入，每个 future 必须 `.result()`。保留二十项文件/内容检查。两个重叠写者足以覆盖丢失更新风险，测试不应假定二十个耐久事务在 hosted runner 上都能在默认十秒内获得锁。

新增 `test_snapshot_contention_preserves_committed_baselines`：事件保持第一个写者，第二个以零锁预算被明确拒绝；释放后第一个 baseline 不丢失，重试第二个写入成功，后续写入不得覆盖首次 baseline。未 mock fsync、未放宽生产超时。日志只能证明锁超时，不能证明是哪一个文件系统调用耗时，也不能据此认定 CPython 缺陷。

### 9.2 SnapshotStore：发布后异常不能删除已引用正文

`atomic_io` 的顺序是写临时文件 → replace → 目录 fsync。最后一步可在 replace 成功后失败。旧 `SnapshotStore.put_baseline()` 在所有异常上删除正文，会把已发布 manifest 留成坏引用。

先用真实 atomic write、仅在 manifest 目录 fsync 注入错误写出两条红测：正常重新读取，以及错误后重新读取也失败。两者都必须继续抛出原错误、保留原正文，重启加载与重试不能用新内容覆盖原 baseline。

生产修改仅在 `put_baseline()` 的异常清理处调用 `_discard_unpublished_baseline_locked()`。它在原锁内读取并验证 manifest；明确不含该路径才删除正文，含引用或发布状态未知时保留。后者是安全的异常处理，不是旧协议兼容或伪造成功。已有“确实未发布时清理孤儿”测试继续通过。

### 9.3 恢复：当前身份同样严格，两次失败不是稳定

旧 `verified_persisted_identity()` 对当前 revision 使用 `int()`，会把 `True`、`"1"`、`1.0` 变成合法的 1；`RecoveryContext.workspace_epoch_stable()` 还把两次读取失败的 `None == None` 当作稳定。

新增 `test_recovery_current_identity_never_coerces_revision`，初轮十六失败、一条合法观察通过。`_state_identity()` 复用现有 WorkspaceIdentity 校验，保持当前 fingerprint 的真实类型和文本；非法观察成为不可信身份。持久收据必须由当前可信身份精确佐证。

在已配置项目存储、已有初次观察的前提下，稳定判断为：

```text
stable = trusted(initial) AND trusted(latest) AND initial == latest
```

因此“两次失败”不满足 stable。未配置项目、未开始读取的上下文不凭空建立项目身份；危险结果的身份佐证仍必须经过正式存储。这没有新增第二个证明系统。

### 9.4 auto：持久项目恢复与收据必须贯通

真实 Edge 首次运行揭示了生产问题：auto 可以编辑文件、执行验证并回答，但入口未传 ChangeTracker，也没有完整 changed 收据；界面缺少 Done/View diff，无法靠持久 baseline 还原。

函数级修改：

1. `task_entry._entry_project_tracker()` 在项目写授权下，从实际 AppContext 创建正式 tracker；非 Git 项目启用持久快照。缺少或失败明确拒绝，不创建测试替代存储。
2. `_run_entry_kernel()` 将同一个 tracker 交给唯一 `run_task_kernel()`，不新增循环。
3. `_entry_project_receipt()` 在实际编辑后调用正式 collect_changes，生成共同收据、登记已有 run ledger 的变更，返回有限显示投影。读取失败不冒充“未改动”。
4. `task_session.session_checks_passed()` 成为已有收据检查投影的唯一所有者；project adapter 与 auto 共用它。它投影共同 gate 的检查，不再次作完成判定。
5. `runs.receipt.build_task_receipt()` 接显式 proof/provenance，删除对 CompletionDecision 的隐式抽取。项目流程和手工基准迁移参数，不保留旧 decision 兜底。
6. terminal mode 调既有 `task.kind.ui_mode()`，保持 agent UI 契约。事件保留完整完成证明，持久 schema-v1 收据存正式 proof refs。

新增 `test_auto_kernel_keeps_project_receipt_and_restore`：现有/新文件两场景，实际 auto → edit → run → done、AppContext、RunLedgerStore 和 restart restore；断言真实文件内容、changed/count、可信度、proof refs、重新创建 AppContext 后的还原。既有异常测试补断言 kernel 确实被调用，避免因缺 tracker 提前失败而“错误地绿”。

### 9.5 冷启动卫生与 UI 测试卫生

- `project_completion_flow` 删除十八项只为重导出而存在的进口；正式入口和内部使用的类型保留。ghost、服务测试和完成测试导入真实 context 所有者，无转发 alias。删除锁与 split lock 都验证正式所有权。
- `tools/ui_e2e.ScriptedWriter` 按当前 auto 请求头提取本夹具的一行请求，避免历史焦点误选响应；识别正式 `[result: edit/run]`，未知 prompt 报错，不默认编辑。删除废弃 reviewer prompt 分支和 step 字段。
- 重连测试原来寻找已不存在的全局 evtSrc，现以 page init script 观察真实 EventSource 实例，只在测试关闭它们，产品代码未加测试后门。
- reload 测试原来等 provider 四秒，在高负载下观察 Running 前就完成；先补红测，再用 entered/release Events 保持任务，浏览器确认还原的 Running 后释放。保留 watchdog 和取消检查，失败清理也会释放，未删除 Running 断言。
- 手工 `edit_integrity_ab.py --self-test` 的二十个确定性完整性场景全部通过。它不由 pytest 收集，两个旧收据调用在前一轮全量期间发现并单独修复；最终全量开始前已完成全部生产与测试迁移。

六个新测试文件合计四十六场景，包含既有合法行为与关键红测反例；不能把原实现已经通过的边界测试也说成先红。首次 UI 全量又发现 split lock 的旧重导出断言，以及 3.13 reload 时间窗口竞争；都明确修复后再完整重跑，没有跳过或放松内容断言。

### 9.6 最终验收及可以证明的范围

在全部生产、pytest 和手工脚本修改完成后，启用 `RUN_BROWSER_E2E=1`：

| 环境 | 最终全量 | 时间 |
| --- | --- | --- |
| Windows / Python 3.12.8 | **6522 passed，28 skipped，1488 subtests passed** | 468.39 秒 |
| Windows / 官方 Python 3.13.15 embed | **6522 passed，28 skipped，1488 subtests passed** | 462.37 秒 |

均零失败，分别使用仓库外独立 TEMP/TMP。真实 Edge 的十八项流程检查通过，但模型是 scripted provider；没有测试真实网页模型时延、token 或泛化的流畅度。Ruff、compileall、diff 检查通过，产品 JavaScript 未改变。二十八项 skip 是 Windows 权限/POSIX 能力，不含 UI 或原超时测试。

有限不变量与真实 crash 轨迹再次八十三项通过，范围仍是第 7 节的有限模型。精确的结论是：

```text
I(s0)
对该有限抽象每条可达边 s → s'，I(s) ⇒ I(s')
BFS 已遍历到固定点
所以对该抽象所有可达状态，I 成立
```

仍缺少把实际 Python、文件系统、任意 provider 输入和线程调度全部精化到该抽象的证明；因此不能把这个结论扩大成“全程序无 bug”或“任意执行都终止”。本轮新测试锁定的是身份不被转换、读取失败不证明稳定、已发布 baseline 不被错误清除，以及真实 auto 收据/恢复链等可观察性质。

完整生产差异相对 aff30e0 为 **+384/-1091，净 -707 行**；`run_task_kernel` 保持单循环、一百六十五行，无复杂度豁免。三千二百四十四个顶层函数的引用扫描无明显未引用候选，但名称扫描无法证明无死字段、动态死分支或同名误判。没有新增日志、通用框架或跨轮缓存。

可以确认本次发现的缺陷已收口、重复入口已减少、实测行为通过；不能确认百分之百完成所有未来需求，不能承诺没有未知 bug。文档在最终全量结束后更新；按用户最新要求，推送后不等待托管 CI，不声称本次托管通过；不 release、不打 tag、不 bump 版本。

## 10. 可读性收尾与拆分停止条件

本次以 d5bf1a94 为起点，扫描所有生产 Python 文件的行数、函数长度和 Ruff C901（忽略 noqa），再按职责评估。复杂度十或十五不是自动拆分命令，也不是数学安全线。全项目 C901 阈值仍是二十。

### 10.1 run_task_kernel 保留

那条 `# noqa: C901, PLR0912` 已在前次审查删除。当前函数一百六十五行、一个循环、圈复杂度十六，能顺着读出停止检查 → 单快照 → 接收/归一化 → done/审批 → 执行 → 记录/交付 → 进展检查。它是编排入口，不拥有工具实现或另一套证明。

建议保持这个结构。为了让十六变成十五，再隐藏一个终止分支或增加状态包装，没有足够的可读性收益。较多关键字参数仍是显式接线，当前不为缩短签名引入一个重复持有会话、授权和执行状态的大对象。

### 10.2 三处确实有收益的拆分

| 入口 | 圈复杂度前→后 | 函数行数前→后 | 具体职责 |
| --- | --- | --- | --- |
| runtime/core/operation_reducer.next_runtime_action | 19→8 | 161→34 | 入口只分派 leaf；三个私有函数分别决定 provider/tool/delivery pending 行为 |
| runs/receipt.task_receipt_from_payload | 20→10 | 122→103 | `_receipt_sections_well_formed` 先检查字段类型，入口重算可信度/措辞并构造正式收据 |
| operations/kernel_protocol.normalize_turn | 19→6 | 78→35 | 两种输入只负责解包，统一保留 snapshot 授权再作参数/工具校验 |

抽出的函数复杂度分别为三/七/四、十一、五/八，最多四个参数；没有新生产文件、框架、策略注册表、转发兼容层或状态对象。生产物理行数增加四十一行，主要是显式签名和职责边界；完整审查相对 aff30e0 仍净减少六百六十六行。这里接受有意义的函数边界，不把总行数当目标。

协议拆分顺便修复真实遗漏：直接调用 normalize_turn 并提供 snapshot_names 时，native 无 tool call 的回复原先递归回文本，漏传这项限制。完整 snapshot 主路径原本受到保护。新实现解包后只进入一次共同验证，授权、动态范围和冻结 specs 一起保留；非法 native 帧不以文本救回，合法文本 JSON 仍是正式协议。删除空/非空文本返回相同错误的重复分支及切片循环中的多余 break。

### 10.3 测试过程与最终验证

先加公开行为锁：收据字段类型、实际 round-trip/篡改拒绝、已结算优先于重新执行、纯文本不得变 done；旧行为应先绿，不能为了声称红测而改变契约。新测试中一个把合法的 None control 当对象读取的编写错误先修正，再确认生产未动时二十七通过、四失败：一条 snapshot_names 行为反例，三条目标入口复杂度≤10 的结构红测。

修改后新增三十一例全部通过，3.13 单独复验通过；相关回归三百一十九通过、四百一十四子测试通过，包含既有有限模型与真实 crash 轨迹。仍保留已结算优先级、call id、批次限制和最终共同校验，不迁移冻结 parity baseline 来掩盖变化。

最终全量启用真实 Edge，两版本均零失败：

| 环境 | 结果 | 时间 |
| --- | --- | --- |
| Windows Python 3.12.8 | 6553 passed，28 skipped，1488 subtests passed | 469.19 秒 |
| Windows 官方 Python 3.13.15 embed | 6553 passed，28 skipped，1488 subtests passed | 463.51 秒 |

全部生产与测试编辑在全量之前完成；用户在运行中要求处理 artifacts，仅调整 Git 忽略元数据并独立检查。文档在两轮全量结束后更新。Ruff、compileall、diff 检查通过；真实模型性能未测，数学证明边界仍是第 7 节的有限抽象。

### 10.4 停止拆分的理由

全仓扫描结果：复杂度>10 的函数一百九十七→一百九十五，>15 的五十二→四十九，>20 的仍为零。剩下的数字公开保留，不能说所有函数都低于十或十五。

- affinity/work_queue 已有独立 model/events/sources，剩余长 Store 是锁、持久变更与选择的所有者；这轮不再把事务拆散。
- trace 的篇幅主要来自有界记录模型和 recorder 方法，不是一个超长决策函数；不新增文件间转发。
- evidence_ledger 的 schema、完整性和图闭包校验仍集中在领域所有者，许多长度来自明确的数据约束；本轮没有确认继续搬迁常量/校验 helper 的收益。
- provider controls 与 tool runtime 仍较长，但已有具名的小步骤；再按行数拆会增加状态/缓存/执行语义的跨文件阅读成本。这是当前维护判断，未来职责真的独立时可重新评估。
- 安全校验和 pending-state 决策中的 guard 是可观察约束，不为降低数字合并成晦涩表达式、通用规则引擎或增加 noqa。

本轮建议到此停止拆分。这不等于每个函数最优，更不等于无未知 bug；它表示没有再确认一个收益足以抵消接线和验证成本的拆分目标。

### 10.5 工作区产物

检查时根 artifacts 共五百七十一份文件，只有 patch/json/jsonl/log/lock/txt，未发现独立源码或可执行文件，Git 无已跟踪文件。其中 pre-review-work.patch 是修改前的本地备份。按用户授权新增 `/artifacts/` 忽略规则，现有文件和备份均不删除，正式报告仍保留在原有受版本控制的路径。

`git check-ignore` 与 `git ls-files -- artifacts` 已验证忽略。代码、测试、文档和忽略规则一起 commit/push；推送后核对 Git 工作区干净，不等待 GitHub，不 release、不打 tag。
