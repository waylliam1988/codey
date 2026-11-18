# CodeMode 观察空转识别与恢复验证

## 修改范围

基线 `56004e4e`，直接在原 `master` 修改。没有创建或复制 worktree，没有停止用户服务。原有 `codey/runtime.json`、发行包删除、页面原型和既有模型探针等用户修改保留，不纳入提交。

本轮复用现有正式子回执、进度预算、冻结控件选择、交接和文件版本合同：

- `progress_comparison.py`：只有成功、完整且子回执全部可核对的快照/正文读取程序，才归一化实际观察到的控件引用。比较保留文档身份、页面正文、控件状态、读取参数与范围、父程序一般计算结果；不修改正式回执。
- `kernel_progress.py`：观察状态从 A → B → A 仍算一次真实转换，不因 A 以前出现过而丢掉进展。
- `browser_task_handoff.py`、`kernel_context.py`：首次确认重复后，在原停止预算内提供已有结构化填写目标和 CodeMode 单步入口。目标由模型选，值从任务取得；程序不猜业务参数、不自动执行建议。继续重复才沿原规则停止。
- 已完成或可能交付的点击、填写、上传等动作不进入此类观察归一化，也不由新交接重放。来源和目标证据不足时不开放固定字段复制；自由编辑不自动绑定较新的读取凭据。

`declared_field_input.py` 的固定复制程序没有另建或改写；现有来源完整性、读取与 `base_result_ref`、版本检查继续负责复制安全。

新增 `test_codemode_observation_progress.py` 覆盖正反例、提醒先于停止、冷恢复与批次重送计数、有效信息变化、无效/不完整子回执、已交付动作保护，以及生产 HTTP/Chromium 交接后完成流程。

## 冻结与实机方法

最终冻结的生产源码摘要：`5b8bb92fd871cc2ba8de96db0a72f7065e30d02fd5860b1961a48353cd0897fc`，证据在 `final/`。

此前冻结版摘要 `ddfb6ae27c159b96d3444c9f127652ca567827c3883bbfc91adfadba4ec5fbf7` 的完整实机、失败和复跑仍保留在证据根。最终复核发现占位符碰撞后修改了比较表示，因此重新冻结并完整验证，不能拿此前结果代替最终源码验收。

完整证据根：`D:/codey-local-evidence/observation-progress-20261118/`。其中 `source-freeze.json`、`frozen-production.patch`、`frozen-production-provenance.json` 记录冻结条件。实机沿生产入口运行，使用本地真实模型和 Chromium；模型实机串行执行，没有改原 17 项矩阵参数，没有目录展示 A/B 实验。

自然首轮与恢复样本分别记录。`browser_failure_handoff_live_gate.py` 新增 observation 用例：先预置两次提前退出循环的只读程序，再交给真实模型。partial 用例沿用原脚本：填写已成功，后续错误访问 `Result.elements`，再由真实模型继续。脚本前缀不是自然首答成功。

## 首次失败和诊断

1. 最初新集成测试在模拟 HTTP 回复生成器内部过早检查 `provider.jsonl`，此时当前请求尚未落盘：1 failed / 43 passed；诊断复现一次失败。把检查放到回复 yield 后，44 passed。原始日志保留在证据根的父目录；可用的 wire 和 events 已另存 `test-first-wire/`、`test-debug-wire/`。这是测试检查时机问题，不是生产未发送恢复建议。
2. 扩大回归首轮：7 failed / 363 passed。其中新上传负例没有授予 `browser.effect`，模拟了当前工具表中不存在的上传；补齐测试授权。其余 6 项在内存加载 HEAD 原生产模块后也全部失败，详见 `../observation-progress-baseline-stale-tests.log`：旧断言仍要求普通 Research 不得初始化明确要求的导航、等待缺少显式 timeout 必须拒绝、CodeMode 模式冻结快照完全不变化。更新为已提交的正式行为，保留非法枚举、权限和外层工具表等检查。没有据此收紧生产合同。
3. 强化类型检查首轮发现原进度模块的隐式 Any 与新增用例类型问题，已改用正式 JSON 解码与明确类型。工具脚本一度重复声明局部变量，已更名。曾误把强化 Any 配置应用到所有导入模块，暴露了未修改依赖中的既有类型问题；最终逐修改模块启用强化配置，正常跟踪导入，没有屏蔽类型错误。旧测试移除了只为取得常量而加载大型其他测试模块的依赖，使用相同参数的明确 `Target`。
4. 最后复核发现本轮初版归一化的确定性错误：原业务字符串若恰好为 `<observed-control:0:0>`，会和运行时引用被替换后的字符串碰撞。新反例先失败，见 `../observation-progress-placeholder-first.log`。最终改为把普通文本段与已观察引用段分别编码，保留两者区别；不是添加字符串特例或放宽停止预算。修复后重新运行完整回归、静态检查和全部实机。

## 此前冻结版结果与完整失败记录

| 检查 | 结果 |
| --- | --- |
| 此前冻结版相关生产回归 | 370 passed，未运行全量 pytest |
| 全树 Ruff | 通过 |
| 生产 mypy | 709 文件通过 |
| 生产 Pyrefly / ty | 通过；没有新增忽略或关闭检查 |
| 内核类型精度门 | Any 计数为零，合同逃逸检查通过 |
| 本轮 8 个 Python 文件强化 mypy | 显式、隐式 Any 检查通过，正常跟踪导入 |
| 原 17 项 full-surface 首轮 | 15 通过 / 2 失败；生产源码摘要前后一致 |

最终回归进程退出码为 0；退出清理阶段仍出现已有 RAM 盘路径不存在警告，不影响已完成的断言。首轮失败日志没有覆盖。静态检查和回归日志为证据根父目录的 `observation-progress-*.log`，强化配置为证据根的 `precision.toml`。

原 17 项逐项结果：

| 场景 | 首轮最终结果 | 中途失败或恢复 |
| --- | --- | --- |
| basic / structured / legacy_structured | 通过 | 无失败工具回执 |
| codemode | 通过 | 两次提前 break 的观察空转，提醒后填写、点击、读正文并完成 |
| reread | 通过 | 快照恢复与重复读取 |
| stale_ref | **失败** | 模型跳过明确要求的“故意用旧编号”步骤，直接用当前结构化控件；事实正确，但预期拒绝回执为零 |
| hybrid | 通过 | 复用固定字段复制 |
| hybrid_codemode | 通过 | 模型填写和点击后漏 return，父程序失败；继续缺失工作，填写和点击均未重做，最终固定复制成功 |
| stale_edit | **失败** | 真实版本冲突被拒绝后重新读取并正确写入；模型随后将 result.json 误填为 knowledge_refs 对象 ID，三次提交无效完成提议而停止 |
| upload / download | 通过 | 沿原生产文件传输合同 |
| research / research_sequence | 通过 | 正式正文读取和研究提交 |
| source_failure / fixed_failure | 通过 | 预期的来源读取失败，正确 blocked，未假装完成 |
| research_codemode | 通过 | 首次把整个控件行当 ref，合同拒绝；模型修正后完成 |
| stale_ref_injected | 通过 | 实际旧编号被拒绝，模型恢复后完成 |

失败分类依据原请求与正式回执，不以“最终 done”代替完整验收。`stale-ref-first-failure-audit.json` 证明负例要求存在于实际请求中，模型没有提交旧编号。`stale-edit-first-failure-audit.json` 保留真实冲突、成功重读、正确最终文件及无效知识引用提议。`full-surface-receipt-audit.json` 保留通过场景中途发生的 missing_return、tool_error 等失败；“最终通过”不表示模型第一步全对。固定读取凭据交接另见 `fixed-copy-reuse-audit.json`。

此前同源码单独复跑：stale_ref 仍失败；stale_edit 通过。首轮仍为 15/17，不合并成全绿。两次任务使用相同配置和语义要求，但包含新生成的引用等运行时信息，不是逐字相同请求的配对成功率实验。对首轮两个失败场景全部 9 / 15 条正式回执的检查表明，HEAD 与此前版本的调用及进度信息投影逐条相同，详见 `failed-case-projection-baseline-audit.json`；不能把这两项失败直接归因于新的观察折叠。

此前普通 Research 两种问法各 5/5 通过。单列 partial 恢复：1 个脚本前缀、4 次真实生成，后续第一步为 browser_click，导航 1、填写 1、点击 1、正文读取 1；没有重复填写。单列 observation 恢复：2 个脚本前缀、5 次真实生成，后续第一步为 browser_fill，导航 1、填写 1、点击 1、正文读取 1。真实请求第 3 次起确实包含恢复提醒，见 `recovery-request-only-audit.json`。实机脚本后来把提示检查收紧为只检查原始 request payload，不从模型回答或其他日志推断；已在同一原始 wire 上离线验证，没有覆盖实机原记录。

同一正式回执夹具与 HEAD 原比较模块的对照见 `reference-churn-baseline-comparison.json`：原模块连续三次均 idle=0、没有提醒；本轮依次 idle=0 / 1 / 2，第二次先提醒，第三次继续重复才停止。对照仅在独立 Python 进程内加载 HEAD 模块，没有替换磁盘生产源码或复制工作区。

已确认：自然 CodeMode 的首两个模型程序相同，均因找到输入框后提前 break 而没有找到按钮；正式回执只有快照。重复提醒确实出现在下一次真实请求中，模型随后填写、点击、等待、读取，第四次生成提交正确净重与校验码。填写、点击各一次，导航一次。详见 `natural-codemode-recovery-audit.json` 和 `full-surface-first/codemode/`。这是自然模型收到恢复反馈后完成的一个样本，不只是 max_turns 改成 no_progress。

## 最终冻结版验证

| 检查 | 最终源码结果 |
| --- | --- |
| 相关生产回归 | **371 passed**，包含碰撞反例；进程退出 0，未运行全量 pytest |
| 全树 Ruff | 通过 |
| 生产全树 mypy / Pyrefly / ty，目标 Python 3.11 | 全部通过，mypy 709 文件 |
| 内核精度门 + 本轮 8 个 Python 文件强化 Any 检查 | 全部通过，无新增 cast、type: ignore 或关闭检查 |
| 原 17 项完整 full-surface | **15/17**，所有原场景重新执行；失败仍为 stale_ref 和 stale_edit |
| 自主 Research，单列 URL 问法 | **5/5** |
| 自主 Research，“帮我看看该 URL”问法 | **5/5** |
| 自由 JS 部分成功后取值失败恢复 | **1/1** |
| 只读空转提醒后的真实模型恢复 | **1/1** |

全部最终实机的源码摘要与最终冻结摘要相同，详见 `final/validation-summary.json`。最终静态及回归日志为证据根父目录的 `observation-progress-*-final-source.log`、`observation-progress-production-final.log`。回归退出清理阶段仍有同类 RAM 盘路径警告，没有断言失败。

最终自然 CodeMode：0 次脚本生成、5 次真实生成。模型先两次提交相同提前 break 的循环，真实请求第 3 次起包含恢复提醒；之后把结构化 ref 错写进 JS 字符串，发生一次语法错误，编译失败没有执行子动作。模型再使用当前已观察引用继续，最终成功填写、点击、等待、读取并报告正确事实。导航、填写、点击、正式正文读取各 1 次。错误和修正都在 `final/natural-codemode-recovery-audit.json`、原始 `provider.jsonl` 和正式回执中。最终 Hybrid CodeMode 和 Research CodeMode 均通过且没有失败工具回执，不能据此抹掉此前冻结版中的 missing_return / tool_error。

最终两项失败均核对原请求与回执：stale_ref 跳过指定负例；stale_edit 的实际版本冲突恢复成功、最终文件正确，但无效知识引用使完成提议被原有拒绝预算停止。将这两项的全部 9 / 15 条正式回执分别送入 HEAD 与最终进度比较函数，调用及信息投影逐条一致，见 `final/failed-case-projection-baseline-audit.json`。这两项没有因为新的观察归一化而改变比较结果；仍不能由一个小矩阵证明整体稳定性与旧实现相同。

普通 Research 每组覆盖：没有预声明控制工作流的动态查询、两个同名按钮的上下文消歧、明确字段交付、初始正文已经足够、慢更新、无关控件。两个初始正文足够/无关控件场景均为零点击；动态场景均只点击一次并引用正式查询正文。原始任务、required_tools、请求与研究提交保留在各 case 目录。

单列恢复记录：

| 用例 | 脚本前缀 / 后续真实生成 | 续跑位置 | 实际调用次数 |
| --- | --- | --- | --- |
| partial | 1 / 4 | 正式回执序号 2 的失败父程序后，第一项后续叶子调用为 browser_click | 导航 1、填写 1、点击 1、正文读取 1；没有重新填写 |
| observation | 2 / 5 | 正式回执序号 4 的第二个观察程序后，第一项后续叶子调用为 browser_fill | 导航 1、填写 1、点击 1、正文读取 1；恢复提醒出现在真实请求 exchange 3–7 |

序号为 `tool_receipts()` 的零起始位置；父程序和子回执均保留。详细位置、首项动作、脚本来源、原始哈希和计数在各用例 `continuation-summary.json`。仅从 request payload 核实提示，额外的反对照证明模型回答中出现同样文字不算程序交接证据，见 `final/request-evidence-checks.json`。

复现使用现有生产 gate，指定全新的输出目录，保持本地模型服务空闲：

```powershell
python -m tools.browser_full_surface_live_gate --run-dir <new>/full-surface --scenario all
python -m tools.research_query_delivery_live_gate --run-dir <new>/ordinary-url
python -m tools.research_query_delivery_live_gate --run-dir <new>/ordinary-wording --source-access-wording
python -m tools.browser_failure_handoff_live_gate --run-dir <new>/partial --case partial
python -m tools.browser_failure_handoff_live_gate --run-dir <new>/observation --case observation
```

上述 29 项最终源码实机不是全绿；没有继续反复运行直至凑齐通过。此前两项单独复跑的失败/成功均仍保留，没有换成最终矩阵统计。

## 边界

- 这是保守的进度投影；任意数字 generation 计算结果、字典键、疑似业务字符串、不完整快照、未知子调用继续保留一般返回语义，不宣称所有运行时差异都可识别。
- 新填写恢复建议需要当前唯一可解析控件、既有正式填写要求、权限和同一文档；没有根据聊天内容猜出一套新工作流。未声明填写要求的自主任务不由这条窄入口自动推断意图。
- 提供便捷入口不能保证小模型一定选择正确动作；单轮通过不能证明普遍成功率提升。目录展示方式的影响尚未做配对实验。
- 本轮页面为受控实机夹具，验证执行、交接、回执和研究交付；不等于知乎、小红书、PubMed 的真实网络、登录和反爬问题已经解决。
