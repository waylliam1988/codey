# Local thinking 展示与重复工具调用修复（2026-10-31）

本次修改以 `95f485eb` 为基线。只运行定向测试，不运行全量测试、不修改用户的历史数据库、不重启或替换正在运行的安装版，也不访问截图中的目标网站。后续按用户要求连接本机真实 Qwen 服务，并通过独立状态目录验证工作区生产代码和已发布前端。

## 原始运行的结论

只读核对本机原始回执与运行清单，定位到 `run_ae05ba68110142728b57532afa0f10ff`：

- 提供方是 Local / KoboldCpp 的 `Qwen3.8-27B-TurboFCFusion-735-882-Here-Uncen-NEO-CODER-MAX-Q4_K_M`。
- 最后请求上下文为 **22,519 / 262,144 tokens**，与截图一致。
- 三次回答都包含相同的 `reasoning_content`、空白正文和相同的五个 `research_plan` 问题。
- 调用 ID 分别为 `call_48543`、`call_22774`、`call_91531`。三次工具执行均成功，返回相同内容。
- 截图中的 `{"next":"Investigate these questions...","questions":[...]}` 来自研究计划工具，**不是 thinking**。
- `detect_exact_repeat` 默认三次重复即阻断，因此第三次结果后停止。没有证据显示此次由权限拦截、上下文溢出或网络失败触发。
- 三次 usage 均缺少上游报告值。界面的 `Known: 0 ... 3 requests with incomplete usage` 是已知用量之和为零，不能解释成没有发送请求或模型没有生成。

模型已经输出 thinking，故不能把“界面看不到 thinking”归结为这个模型不支持 reasoning。
这也不需要按 Qwen 模型名增加专用解析分支。

## 已复现并修复的程序问题

| 路径 | 修复前 | 修改后 |
| --- | --- | --- |
| 展示历史 | turn/reasoning 只发实时事件，工具记录才被保存；冷恢复丢失 thinking | 非空 thinking 按 run/turn 幂等保存为 display-only，重启可读，展示副本不进入模型上下文 |
| 原生调用历史 | response artifact 到 tool_call、tool_call 到 provider context 两处丢失 reasoning；后者也丢失普通 content | 在这两处保留原始 content 与 reasoning_content，不修改工具参数或调用 ID |
| 工具行 | 没有 path/command 的成功工具没有可见名称，只剩一个“·”和可展开的 JSON | 使用可读工具名作为后备标签；完整原始结果仍在详情中 |
| 重复结果续跑 | 第二次重复后没有明确指出当前调用已重复成功/失败，第三次直接停 | 在既有预算内给出明确纠偏，文本协议与原生协议都覆盖；继续重复仍按原阈值停止 |
| SSE reasoning | 与非流式支持不一致：漏掉 thinking 别名，非字符串 reasoning 会 TypeError | 支持 reasoning_content/reasoning/thinking，只累计字符串 |
| 历史刷新 | 保存 thinking 后，实时记录与已提交记录可能同时保留 | 按 run、turn 与内容匹配，用已提交记录替换实时副本，保留后续新思考 |

纠偏不改变真实工具回执、进展指纹、授权或完成验收。登记计划不等于已经调查完成。
如果模型仍无视反馈，停止保护仍然生效。

已重建前端并同步到仓库的 `codey/web`，`tools.publish_frontend --check` 通过。
运行中的独立安装版尚未切换到这些工作区修改。

历史兼容边界：新增 display-only 保存针对修复后发生的事件，未回填旧数据库。
旧的原生 response artifact 中仍保存了本次原始 thinking；原生模型上下文投影现在可保留它。

## 测试与证据

先加反例再修改：第一批 7 项 Python 反例及 1 项工具标签反例失败；相邻路径随后又复现 7 项 SSE 失败和 1 项历史合并失败。上述反例修改后均通过。

新增回归入口：

```powershell
python -m pytest -q tests/test_native_repeated_plan_replay.py tests/test_repeated_tool_feedback.py tests/test_reasoning_cold_history.py
```

结果：**23 passed**。覆盖生产 API → 内核的原生响应回放、完整五问题计划、不同 call ID 的重复、工具结果配对、听取/不听取纠偏的分支、失败结果纠偏、进展快照恢复、SQLite 冷启动和 SSE 字段/类型差异。
回放使用脚本化 HTTP 响应，不将它当成真实 Qwen 生成测试。

扩大选择涉及 API 原生历史、终止与取消、原生 ID 关闭、执行轨迹、内核进展、usage、响应持久化、上下文跨协议、展示事件及 Local 解码器：

- 第一组选定 18 个 Python 文件：**185 passed，2 failed**。
- 第二组选定 7 个 Python 文件：**46 passed**。
- 两组文件不重叠，合计 **231 passed，2 failed**；23 项新增测试已包含在第一组，不重复累加。
- 前端 `render`、`process_thinking_dom`、`conversation_history`、`durable_history`、`reply_stream`：**46 passed**。
- 修改涉及的 7 个生产 Python 文件通过 scoped mypy；修改涉及的 Python 源码和测试通过 Ruff。
- 前端 TypeScript、修改文件的 Biome 格式检查、生产构建、发布一致性检查及 `git diff --check` 通过。

两项扩大测试失败都在未修改的 `95f485eb` 独立检出中原样复现：

```powershell
python -m pytest -q tests/test_kernel_parity_regressions.py::test_research_synthesis_finalizes_claim_support_before_persisting tests/test_local_response_codec.py::test_incomplete_done_frame_gets_specific_repair_without_executing
```

分别是旧研究综合基准期望 done、实际 protocol，以及旧 done-frame 测试期望的修复提示与当前契约不同。
没有为消除这些既有失败而削弱完成或协议校验。本次没有运行全量 pytest 或全量前端测试；真实模型生成与浏览器验证见下一节。

## 真实模型与浏览器验证

新增可重复运行的实机脚本：

```powershell
python tools/local_thinking_live_gate.py --output .e2e-artifacts/thinking-live-20261031-a
```

脚本使用生产 `run_headless`、真实 Local 原生工具协议和 thinking，连接 `http://127.0.0.1:5001/v1` 的同名 Qwen 模型。每次创建独立配置、SQLite 状态及源码项目；源码仅是未启动的本地审计样例，包含原五类问题涉及的模式和实际读取标记。工具执行与模型回答均未替换。随后启动工作区的生产 HTTP 服务，用真实 Edge 无头浏览器加载打包前端，再刷新页面核对历史。

第一次实机运行（`thinking-live-20261031-a`）用时 **193.66 秒**：

- 三轮真实请求依次执行 `research_plan`、`read_file`、`done`，停止原因 **done**，无重复计划。
- 回答覆盖 q1..q5，包含实际源码标记；样例文件未修改。
- 三轮 thinking 已写入 SQLite，关闭并重开 AppContext 后可恢复，浏览器刷新前后内容相同。
- 页面显示 `research plan` 工具标签，无 JavaScript 页面错误。
- 实际后续请求保留第一轮 **1010 字符**、第二轮 **143 字符**的 `reasoning_content`；工具结果 ID 与调用 ID 配对。

第二次使用全新隔离状态运行（`thinking-live-20261031-b`）用时 **209.39 秒**，同样三轮完成 `research_plan → read_file → done`，全部检查通过。额外展开页面中的 Thinking，确认三段模型思考均可见、没有被工具 JSON 替代，刷新恢复后段数与持久化记录一致。

实机阶段再次定向运行 10 个相关 Python 文件：**87 passed**；5 个前端文件：**46 passed**。这些是重复回测，不额外计入前述唯一测试总数。新增实机脚本及修改文件 Ruff、前端发布一致性和 diff 空白检查通过。

证据保存在各输出目录下的 `result.json`、`events.jsonl`、`provider.jsonl`、`thinking-reload.png` 和隔离状态目录。证据目录不提交到仓库；复现脚本提交。

该实机任务验证了真实模型可继续调查及 thinking 的冷恢复；没有强制让模型先重复两次，因此第二次重复后的纠偏分支由原始响应形状的定向回放测试验证。模型持续无视反馈时，原有第三次重复停止保护仍然生效。实机通过不代表所有未来提示都不会使模型停滞。

## 当前服务的额外核对

提供只做元数据读取与模板分词、不触发生成的脚本：

```powershell
python tests/manual/local_tool_template_probe.py
```

本次在 `KoboldCpp 1.117.1`、同名 Qwen 部署执行，`/api/extra/tokencount` 返回 **408 tokens**。
渲染后的 prompt 中同时包含 reasoning、工具结果 sentinel 与纠偏 sentinel，三项检查通过。
原始请求、返回与检查写入 `.e2e-artifacts/qwen-thinking-20261031/template-probe.json`。

这证明当前模板能接收这三类内容，不是“当前模板必定丢弃所有工具结果”的证据。
该模板探针不是历史请求的完整重渲染，也不执行模型生成，不能单独证明 Qwen 会稳定听取反馈；真实生成另见上一节。
原始回执仅足以定位重复发生在提供方回答层；要进一步区分模型生成、服务端解析或其他推理参数，需要解析前输出或受控的真实生成对照。
