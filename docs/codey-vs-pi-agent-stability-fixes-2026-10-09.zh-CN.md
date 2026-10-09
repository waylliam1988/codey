# Codey / Pi Agent 稳定性修复与新种子验证（2026-10-09）

修复前历史样本为 Codey 11/20、Pi 10/20。本轮冻结生产代码，用未参与调整的
种子 51/52 验证十个场景、40 次原生运行：**Codey 18/20，Pi 9/20**。
不同种子的前后样本不能直接当作严格配对因果实验；修复依据是确定性测红、
每批同场景实机证据及最后独立的新种子验证。不能据此宣布所有模型、场景或速度全面领先。

## 最终场景验收

| 场景 | Codey | Pi |
|---|---:|---:|
| 普通修复 | 1/2 | 2/2 |
| 先测再改 | 1/2 | 0/2 |
| 无需修改 | 2/2 | 1/2 |
| 只诊断、禁止修改 | 2/2 | 0/2 |
| 多文件修复 | 2/2 | 0/2 |
| 长输出中段恢复 | 2/2 | 0/2 |
| 503 自动恢复 | 2/2 | 1/2 |
| 停止运行中的测试 | 2/2 | 2/2 |
| 编辑中断后续接 | 2/2 | 1/2 |
| 停止后改变需求 | 2/2 | 2/2 |

20 对中双方通过 8 对、仅 Codey 通过 10 对、
仅 Pi 通过 1 对、双方未通过 1 对。验收规则没有改动：
补丁、范围、实际且新鲜的 Agent 验证、正常终态共同决定修复成功；只读任务需明确 blocked
且不得修改；停止需原生确认、进程退出、无残留且无需强制清理。

## 找到并修复的根因

1. **明确只读指令没有完全进入权限。** 入口已有词表漏掉 `do not edit`、
   `don't change`、`must not modify` 等表达。补足既有全局只读判定，局部“不改测试/某文件”
   仍保持局部含义。不是另外建一套自然语言授权系统。
2. **检索之前，结果就已被截掉。** 原结果读取只能分页；增加同一个只读工具的
   literal query 后，长输出实机仍失败。复查发现普通命令捕获先按 256 KiB 丢掉中段，
   后面的 2 MiB 存储只能保存剩余文本。受管执行现在按现有归档容量捕获，预览仍短。
   实际大输出子进程测试证明中段可检索、命令只执行一次；所有权、digest 和不完整标记保留。
3. **完整 503 拒绝直接终止。** 共享运行时对明确拒绝的 503 最多三次物理尝试，
   相同已计数请求、同一截止时间，尊重 Retry-After、可取消，并分别观察用量。
   结果未知、网络中断及其他 HTTP 状态没有增加自动重发。
4. **停止还等待一次没有用途的生成。** 最终 API 工具回执原来调用发送路径，
   实际启动模型生成并可能再返回工具。现在本地校验 call ID 配对、提交现有历史，
   不增加物理请求。需要继续回答的路径仍发送；Zen 内部真正继续生成的闭合留在 Zen 包。
5. **续接的编辑范围没有到达共同完成门。** 已保存的正确补丁和真实通过验证，
   仍被判定为缺少修改，再陷入无效重复。写入阶段把检查点范围传给共同完成判断，
   与本次编辑合并；没有当前验证的续接仍被拒绝。没有降低 no_progress 或 review 的要求。
6. **完成事件先于续接事实。** 外部看见 edit/run 完成就中断时，WorkCheckpoint
   可能尚未更新。现在先投影工作区与检查点，再发布完成事件。健康存储下的事件顺序由
   实际持久化测试锁定；原存储故障处理仍在，不能承诺磁盘失败时也持久成功。
7. **恢复可防止重跑，却不能读取已完成结果。** 正式恢复现将验证过的回执填入
   已有结果缓存，整批校验成功才发布；不增加第二份持久化记录，不通过重跑恢复输出。
8. **异常回复擦掉有效输入，原生提示同时教两套格式。** 服务端接受输入后发生
   解码失败，现在保留合法用户输入和已经执行的工具结果，不保留坏调用；取消迟到响应
   仍被隔离。原生提示只引用 wire schema，文本模式保留原 JSON 协议。当前验证状态明确
   current/stale/unknown，只读失败诊断可以按要求结束，不被提示继续修复不可写文件。
9. **最终回答被当成进度预览截断。** 实机中模型已经返回 blocked JSON，CLI 却
   只交付前 1,000 字符，切掉末尾 JSON。最终 summary 使用独立 64,000 字符上限，
   超限明确标记 summary_truncated；进度和工具预览仍维持 1,000 / 200 字符。

主要实现与测试入口：

| 边界 | 生产入口 | 锁定行为的测试 |
|---|---|---|
| 全局只读 | `task/entry_auth.py::_explicit_readonly_task` | `test_entry_readonly_imperatives_enforce_permissions.py` |
| 输出归档与检索 | `storage/managed_outputs.py::run_command_with_managed_output`、`operations/tool_result_reader.py::read_tool_result` | `test_managed_command_capture_preserves_searchable_middle.py`、`test_stored_tool_output_keyword_search.py` |
| 拒绝重试与最终配对 | `providers/api_provider.py::_generate`、`acknowledge_tool_results` | `test_api_rejected_generation_retry_lifecycle.py`、`test_api_terminal_receipts_commit_without_generation.py` |
| 完成范围 | `operations/project_writer_phase.py`、`project_completion_checks.py::_normalized_scope_and_change` | `test_writer_checkpoint_scope_reaches_completion_gate.py` |
| 持久化先于发布 | `operations/task_phases/hooks.py::_RunHookCallbacks.on_event` | `test_tool_completion_event_follows_durable_checkpoint.py` |
| 回执恢复 | `operations/kernel_session_recovery.py::restore_task_session` | `test_restored_settled_tool_receipts_remain_readable.py` |
| 正确历史与提示 | `providers/api_provider.py::_exchange`、`operations/kernel_prompt.py` | `test_unusable_native_reply_preserves_accepted_context.py`、`test_native_tool_prompts_use_wire_schemas_without_text_wrappers.py` |
| 最终 JSONL 交付 | `app/event_payloads.py::_payload_task_done` | `test_headless_final_summary_preserves_structured_answers.py` |

生产路径均相对 `codey/`；测试均相对 `tests/`。完整测试索引见
[测试说明](../tests/README.md)。Pi 参考入口为
`reference-projects/pi/packages/coding-agent/src/core/agent-session.ts::_prepareRetry/abort`
和 `reference-projects/pi/packages/agent/src/agent.ts::abort`。

Pi 的当前源码提供可取消重试、Agent abort 和持久历史；它是定位差异的参考，
实际比较使用已有 dist 构建。对应源码版本未知，不能把当前源码每个细节都声称为该构建的行为。
Codey 的优势应来自更可靠的权限、回执和连续工作，不来自复制整个 Pi 框架。

## 分批实机证据，包括失败的调整

以下各行使用不同中间 Codey 源码，**不能把通过数合并成总胜率**。每批原始结果和
生产补丁指纹可从数值报告追溯；失败未删除，评分未放宽。

| 阶段 | 原生运行数 | Codey | Pi |
|---|---:|---:|---:|
| 503 | 4 | 2/2 | 1/2 |
| readonly | 8 | 2/4 | 1/4 |
| search | 4 | 0/2 | 0/2 |
| capture | 4 | 2/2 | 0/2 |
| terminal | 12 | 5/6 | 5/6 |
| resume | 4 | 1/2 | 2/2 |
| checkpoint-order | 4 | 2/2 | 1/2 |
| guidance | 12 | 2/6 | 3/6 |
| preserved-context | 8 | 2/4 | 2/4 |
| native-guidance | 12 | 5/6 | 2/6 |
| final-delivery | 4 | 1/2 | 0/2 |

关键词检索单独加入仍是 0/2，修复前置捕获后 Codey 为 2/2。续接范围修复后仍
1/2，修复完成事件顺序后为 2/2。原生提示调整后普通修复为 2/2，但其中一次约
118 秒，慢于 Pi 约 84 秒。最终交付修复的测红证明截断消失，不过该批第二次模型
改为重复读结果、触发 no_progress，仍失败。它们是逐层定位证据，不是挑选的最终成绩。

另有普通任务中，模型把“删除标点”误写成“标点替换为空格”；可见测试过了，
隐藏输入失败，Local reviewer 也误批。这是实际语义失误，不能靠正确的运行时回执自动消除。
没有把隐藏测试答案塞进提示、关闭审核或允许假完成来改分数。

### 未奏效的复核改进

最终 Codey 普通修复 / 52、先测再改 / 52 均违反同一原始要求：
`removes ASCII punctuation` 应删除字符，而实际补丁替换为空格。
例如 `A.B 42!` 应得到 `ab-42`，实际为 `a-b-42`。这不是执行回执缺失；
可见测试、真实验证和 reviewer 都通过，说明这些证据仍不足以证明完整语义。

针对冻结失败样本又进行了 **26 次独立 reviewer 实机调用**：加强独立审查、
要求区分边界输入、调整 writer 自述位置、移除自述，以及先从原任务提炼验收含义。
前三组各 8 次，最后一组 2 次。错误补丁仍被误批；正确对照仍获通过。
task-first 模型甚至一边声明删除标点，一边把 `123-456` 的期望写为 `123-456`。
这些测试不能算作修复成功，相关生产提示改动及仅锁定提示形状的测试均已撤回。
[独立复核实验记录](reports/codey-local-review-semantic-probes-2026-10-09.json)
保留返回内容、用量和输入指纹；不混入原生 Agent 的 40 次最终成绩。

因此，本轮已改善确定性的运行时边界，**尚未解决全部语义判断失误**。
下一步需要跨任务验证更可靠的契约检查，不能针对一个隐藏输入写特例，
也不能把多问一次同一模型当成独立证据。

## 双方成功时的耗时

只比较双方都满足完整场景的配对。Codey 时间包含真实 Local review；负数表示
Codey 耗时较少。失败得快不计为速度优势，不把不同场景混成总体速度。

| 场景 / 种子 | Codey 秒 | Pi 秒 | Codey 相对耗时 |
|---|---:|---:|---:|
| 普通修复 / 51 | 42.00 | 81.98 | -48.8% |
| 无需修改 / 51 | 17.65 | 14.89 | +18.6% |
| 503 自动恢复 / 52 | 47.19 | 24.59 | +91.9% |
| 停止运行中的测试 / 51 | 3.99 | 3.97 | +0.5% |
| 停止运行中的测试 / 52 | 3.97 | 3.86 | +2.9% |
| 编辑中断后续接 / 51 | 71.87 | 22.98 | +212.7% |
| 停止后改变需求 / 51 | 33.32 | 17.17 | +94.0% |
| 停止后改变需求 / 52 | 33.99 | 17.05 | +99.4% |

停止单独计时，从控制器发出停止到 Agent 进程退出，含收尾：

| Agent / 种子 | 停止秒 |
|---|---:|
| codey / 51 | 0.250 |
| pi / 51 | 0.266 |
| pi / 52 | 0.235 |
| codey / 52 | 0.219 |

历史 Codey 两次停止约 1.797 秒；终止回执修复的阶段为 0.234 / 0.219 秒，
对应 Pi 0.250 / 0.265 秒。小于几十毫秒的差异不具有全面速度优势的证明力。

## 最终未通过记录

以下字段是观察结果，不自行把正常终态、正确补丁或外部测试通过提升为完整成功：

| Agent / 场景 / 种子 | 终态 | 验收事实 |
|---|---|---|
| pi / 先测再改 / 51 | `error` | scope=True, patch=False, fresh=False |
| pi / 只诊断、禁止修改 / 51 | `error` | scope=True, patch=False, fresh=False |
| pi / 多文件修复 / 51 | `error` | scope=True, patch=False, fresh=False |
| pi / 长输出中段恢复 / 51 | `stop` | scope=True, patch=True, fresh=True |
| pi / 503 自动恢复 / 51 | `error` | scope=True, patch=True, fresh=False |
| codey / 普通修复 / 52 | `done` | scope=True, patch=False, fresh=True |
| pi / 先测再改 / 52 | `error` | scope=True, patch=False, fresh=False |
| codey / 先测再改 / 52 | `done` | scope=True, patch=False, fresh=True |
| pi / 无需修改 / 52 | `stop` | scope=False, patch=False, fresh=False |
| pi / 只诊断、禁止修改 / 52 | `error` | scope=True, patch=False, fresh=False |
| pi / 多文件修复 / 52 | `error` | scope=True, patch=False, fresh=False |
| pi / 长输出中段恢复 / 52 | `stop` | scope=True, patch=True, fresh=True |
| pi / 编辑中断后续接 / 52 | `stop` | scope=True, patch=False, fresh=False |

“Pi 能成功，所以完全排除模型”需要收紧：它证明同一模型在该输入与运行设置下
具备可行路径，但提示、工具协议、历史保留和采样轨迹仍会改变输出。双方失败也不能
直接归因模型；长输出捕获就是双方失败场景中已确认的 Codey bug。

## 条件与验证

- KoboldCpp 1.117.1，Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M；
  temp=0，种子 51/52；双方 32,768 上下文、2,048 输出，180 秒和 24 次物理生成上限。
- 交替执行原生 Agent；各运行独立工作区与状态，后台空闲等待不进入任务计时。
  无云端模型请求；Codey 保留真实本地 review、完成门和停滞保护。
- 本轮先后 76 次分批运行，再 40 次冻结版本验证；历史基线 40 次另记。
- 新增 12 个按行为命名的测试文件，68 项新用例。定向扩展回归
  **1,600 passed，64 subtests passed，91.85s**；机器契约 **798 passed，174.74s**。
- 首次扩展回归为 17 failed / 1583 passed，首次机器契约为 19 failed / 779 passed：
  旧断言要求清空有效输入、额外 ACK 生成或空恢复缓存。逐条更新为新行为，并保留
  配对、取消、异常调用不可执行、实际回执读取等检查后，相关及扩展回归通过。
  移除 Zen 的探针仍真实执行两种 Local 协议；只计实际的两次生成，不再伪造第三次 usage。
- Ruff 全树、mypy Windows/Linux（402 个文件）、直接 Pyrefly（0 errors）、
  compileall 和 diff 检查通过。**全量 pytest 按此前明确要求未运行**；没有本轮全量结果。
- Pi 流式 usage 不完整，不能公平比较总 token；重复调用的参数缺失也保持 unknown，
  不能把未知记成零。样本只有两组种子，不代表模型与平台的普遍排名。

完整条件、source/build/benchmark SHA、生产补丁与原始结果 digest 见
[可移植数值报告](reports/codey-vs-pi-agent-stability-fixes-2026-10-09.json)。
历史结果见[修复前报告](codey-vs-pi-agent-stability-2026-10-09.zh-CN.md)。
版本保持 0.5.11；不创建 tag，不 release。
