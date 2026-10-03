# 0.5.10 版本提交验收

日期：2026-10-03。开始时基线为 `3ecd4a1b`，工作区干净。
上一版源码版本为 `0.5.9`，对应版本提交 `d71683d`；本次为 `0.5.10`。

## 结论与执行范围

本次候选代码通过仓库的静态、确定性、机器契约、UI 与实机门槛，可以进行
本次版本 commit 和 push。按用户选择，不创建 tag、GitHub Release 或发布资产，
不等待推送后的 GitHub CI。

验收不是全库无 bug、无死代码或所有模型都可靠的证明。当前文件和职责见
[源码地图](project_structure.zh-CN.md)；每次历史测试结果仍保留在
[TEST_REPORT](../TEST_REPORT.md) 和中英文 changelog。

## 本轮找到并修复的问题

### 1. 顾问预算的浮点上溢

基线 GitHub CI 的 Python 3.12 作业失败于顾问 timeout 上界断言。
本地用受控时钟 `76.013` 复现：`(76.013 + 180.0) - 76.013` 为
`180.00000000000003`，超过配置的 180 秒。CI 日志没有打印实际 timeout，
因此不宣称知道其具体时钟值，也不把问题归因为 Python 3.12 独有。

`provider_session.DeadlineProvider` 持有同一 episode 的预算与绝对截止时间，
发送时取两者的最小剩余值；过期不再发送，调用方的更小 timeout 仍保留。
`project_audit_advisor.run_project_audit_advisor` 的 timer 与 provider 共用这份预算。
没有保留旧构造接口。

### 2. JSON 格式错误的诊断与整轮拒绝

第一份 JSON 实机矩阵为 **12/13**：测试生成项连续四次把真实换行放在
JSON 字符串中。严格解析正确拒绝，但修复提示只有“没有找到工具调用”。

`kernel_protocol._extract_json_objects` 现在给出 JSON 错误位置与换行、制表符
转义说明，保留严格解析；合法转义仍还原为原始文件内容。格式错误的批次
整轮拒绝，不再跳过错误成员执行其余调用。解码资源限制也作为拒绝处理。
删除了 scanner 导入失败后换另一套解析方式的兜底。

### 3. 协议修复丢失当前契约

另一份 JSON 实机矩阵为 **12/13**：引用修改项输出了缺少实际工具名的
`<|tool_call>call:tool...`，四轮未纠正，文件未改。原始报告按独立验证失败保存；
原始产物和自动分类没有被事后改写。

回归确认 `task_loop._kernel_handle_protocol` 原来没有传回冻结工具契约，
且原生路径的文本修复提示也要求 JSON。现在复用本轮 `snapshot.contract_text`，
由 `kernel_prompt._repair_prompt` 按真实协议提示。缺名字的回复仍被拒绝，
不从参数猜工具，不引入新的方言执行兜底。引用项复测及最终完整矩阵均通过。

### 4. 重连 UI 残留“连接中”

第一轮全量的真实 Edge E2E 发现：后端状态已变为 `running`，事件流却只有
`connecting`，重连时旧事件可能覆盖状态快照，界面停留在“Connecting to browser”。

`provider_services.open_provider_session` 与 `HeadlessAppContext.get_provider`
现在在成功连接后发布 `running`；失败不发布成功状态。
已有 `sse.js` 统一状态文字和样式，事件、状态快照和开始运行共用它。
E2E 通过明确的 provider-entered 事件锁定重载时机，保留原来的 Running 断言。

### 5. 测试接线卫生

- 顾问执行器断言改读真实 typed request，要求准确的非空工具集合；旧断言
  读取不存在的 kwargs 后退为空集合，可能假通过。
- 版本一致性测试同步到 `0.5.10`，仍同时核对两种语言 README 和 changelog。
- 连接事件测试精确要求 `connecting → running → providers`，更新旧数组索引。
- UI 内联预算仍为 1650 行，最终为 **1648**；没有提高预算、放宽完成门或增加跳过。

## TDD 与最终验证

新增四个行为测试文件，合计 **29 条**：

| 文件 | 锁定的行为 |
| --- | --- |
| `test_provider_episode_budget.py` | 浮点上界、三种发送接口共享剩余预算、过期拒绝 |
| `test_kernel_json_syntax_repair.py` | 控制字符诊断、内容保真、错误批次拒绝、解码限制、真实内核修复 |
| `test_provider_connection_running_event.py` | 桌面/headless 成功与失败事件、真实 Node.js 状态显示 |
| `test_kernel_protocol_repair_keeps_snapshot.py` | 修复保留冻结契约、协议选择正确、缺工具名不推断执行 |

行为红测后修复；状态显示提取在绿测保护下完成。没有新增 skip/xfail 来绕过失败，
没有改写 parity 基线。29 条新增测试均在最终全量运行。

| 验证 | 实际结果 |
| --- | --- |
| 最终全量，Python 3.12.8、Windows、`RUN_BROWSER_E2E=1` | **6776 passed、6 skipped、1497 subtests passed，481.80s** |
| Python 3.13 相关协议、连接、超时、子进程回归 | **81 passed、1 skipped，8.78s** |
| Ruff、compileall、diff 检查 | 通过 |
| `python -m mypy codey` | 363 个源文件，零错误（当前配置） |
| JavaScript | 11 个 asset 与 1 个实际内联脚本语法通过 |
| 离线 parity | 682 cases / 554 equal / 128 intentional / 0 failures |
| 最终 JSON 实机矩阵 | **13/13**；6 个 objective tasks 均完成、产物正确 |
| 最终原生实机矩阵 | **13/13**；6 个 objective tasks 均完成、产物正确 |

全量命令：`python -u -m pytest -q -o faulthandler_timeout=120 -rs`。
6 个跳过是 Windows 不适用的 POSIX 权限、进程组、绝对路径和 `O_NOFOLLOW` 检查。
真实 Edge 使用脚本模型驱动 UI，不能替代真实网页 provider 登录与页面改版验证。

本轮前两次全量分别为 `2 failed, 6765 passed`（旧版本锁与真实 UI 问题），以及
`2 failed, 6771 passed`（旧事件索引与内联预算）。最后一轮零失败；前两轮记录保留。

## 实机记录与性能范围

模型为 `/models` 返回的
`koboldcpp/Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M`，端点
`http://127.0.0.1:5001/v1`，temperature 0，未发送 seed。
量化与聊天模板没有独立验证。任务使用仓库外隔离项目、状态与研究 vault。

最终原始产物在忽略目录：

- `.e2e-artifacts/release-0.5.10-json-final-candidate-20261003/`
- `.e2e-artifacts/release-0.5.10-native-final-candidate-20261003/`
- `.e2e-artifacts/release-0.5.10-full-pytest-complete.log`

门槛元数据中记录的生产文件哈希与最终源码一致。提交前运行的 metadata Git 字段
仍是基线 `3ecd4a1b`，不是尚未创建的版本提交；用生产哈希区分工作区候选代码。
原始对话、来源和运行产物不提交 Git；只提交本验收说明。

此前失败矩阵与单项复测不混入最终矩阵的成功分母。13 个 case 包括控制面检查，
不能称为 13 个编程任务，也不能据一份矩阵声称模型的普遍成功率为 100%。
回答质量与跨模型/跨项目体验没有独立评审；耗时和 token 没有做受控比较，
不能声称整体性能更快。

## 可证明的范围与收尾边界

有限交付模型遍历达到固定点：**44 个状态、134 条可接受边**；权限枚举覆盖
**512 个子集 × 2 种拒绝状态**。在这些抽象内检查终态互斥、收据身份、授权
边界及 JSON/native 能力一致性。细节见[不变量说明](kernel_invariants.zh-CN.md)。

预算发送满足 `remaining = min(budget, deadline - now)`；在预算有限且时钟可信
的前提下，每次允许发送的 timeout 不大于预算。它不证明任意 provider 都遵守
timeout，也不证明整个程序必定在精确墙钟时间内结束。

生产复杂度扫描仍为：195 个函数大于 10、48 个大于 15、零个大于 20。
没有因此机械拆分共同内核或连贯的安全校验器。静态引用、类型检查与测试都有
边界；当前验收支持这次版本提交，不能扩展为数学证明全库绝无 bug 或死代码。
