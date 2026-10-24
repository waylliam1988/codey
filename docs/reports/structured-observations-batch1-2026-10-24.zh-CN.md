# 结构化观察第 1 批验收记录

状态：按用户确认的相关测试准入标准验收通过，可合并第 1 批。本批要求明确改善事实记录和恢复可靠性，
且不引入新增回归；原版已有且没有恶化的问题记录为已知限制，不要求在本批解决。
此记录不宣称通用幻觉率已下降。

## 修改范围

- 搜索、来源打开、页内搜索共用纯结构化观察校验；缺失或畸形观察明确失败。
- 删除展示文字提取搜索 URL、请求参数/别名补出已打开来源，以及显示行反推 locator。
- 删除重复的 `opened_url`、`request_url` canonical 字段；别名仍用于执行前定位。
- ToolResult/ToolOutcome 共用无损 canonical JSON 边界。presentation/audit 仍为有界展示；
  完整结果复用原受管回执，不增加存储、执行器或恢复状态机。
- 生产者、事实投影、恢复消费者同时更新；测试替身显式声明结构化采集结果。

不包含：结算/发布生命周期调整、来源完成门收敛、时间上下文、强制引用或后续读取。

## 参考实现

- DeepSeek Harness `packages/web/tool-web/src/search.ts`：结构化来源与有损显示分离。
- DeepSeek Harness `packages/core/agent-loop/src/tool-calls.ts`：工具结果是上下文的依据。
  本批只收敛观察校验，提交和发布顺序留给第 2 批。
- Codey 原有 `kernel_receipts.py`、`ledger_receipts.py`：完整回执、摘要验证与现有恢复。

Codex 的时间上下文、OpenCode 按可见工具生成提示的实现属于后续批次，本批未搬入。

## 确定性验证

- 新增缺失观察、畸形字段、布尔伪整数、无时区时间、无效 URL、重复页、正文摘要
  不一致、零结果、失败读取、重定向及长身份测试。
- 使用真实 durable 回执保存并重建大于 2,000 字符的来源正文和 URL，检查正文、
  来源身份及读取时间保持一致。没有绕开 ToolResult 构造器。
- 原有恢复入口测试仍验证重复恢复不重新执行来源工具。
- 静态检查：全仓 Ruff、全树 mypy/Pyrefly/ty（目标 Python 3.11）及 `git diff --check`
  通过；Pyrefly 仍有现有警告与抑制项，不宣称零警告。
- 最新相关边界定向运行：98 passed。旧流程测试替身迁移后的定向运行：187 passed、
  81 subtests passed；另一个来源/项目交错回归文件 11 passed。各次选择有重叠，不相加。
- 必跑契约并行运行：1169 passed、14 subtests passed、3 failed，未通过。
  失败为两个 Shell 审批 5 秒等待及一个 JSON 恢复上下文关闭失败。
- 上述相关文件单独重跑：7 passed、1 failed；恢复测试通过，但另一个审批参数组合
  仍在 5 秒等待处失败。不把重跑部分通过等同于原门全绿，也不直接认定是资源竞争。
  未修改审批、上下文关闭代码或测试等待时限。
- 用户确认只要求相关 pytest；全量运行在约 94% 时主动停止，不计通过，也不作为
  本批准入条件。此前全仓契约中的审批/上下文关闭失败记录保留，不扩大本批范围。
- 最终串行相关选择（15 个文件）：329 passed、85 subtests passed，31.50 s；
  原始 JUnit：`.e2e-artifacts/structured-observations/related-final.xml`。
  此后全仓 Ruff、mypy（512 个源码文件）、Pyrefly、ty 及 diff 检查通过。

## 性能对照

使用相同 `tests/manual/observation_benchmark.py`，在本机依次运行原提交 dd51049e
与修改工作树；每个尺寸先预热 3 次，再测量 25 次。走真实 SQLite settlement、
受管结果回执及恢复构造，不含网络和模型耗时，也不在 pytest 并发期间测量。
原始样本位于 `artifacts/observation-performance/{baseline,candidate}-1.json`。

| 正文尺寸 | 原版总耗时中位数 | 修改版总耗时中位数 | 恢复结果 |
| --- | --- | --- | --- |
| 512 字符 | 66.63 ms | 65.96 ms | 两边 25/25 成功 |
| 65,536 字符 | 68.27 ms | 102.03 ms | 原版 0/25；修改版 25/25 成功 |

原来可用的短来源路径未观察到明显耗时退化。长来源完整保存、校验和恢复增加约
34 ms，以及每条约 67 KB 受管回执；原版更快的结果伴随裁剪和恢复失败，不能当作
相同正确工作量。该成本明确保留，不宣称所有路径速度或存储占用均无增加。

## 本地模型实机

模型：`koboldcpp/Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M`，
KoboldCpp `http://127.0.0.1:5001`。文本与原生两种协议，窗口 32768，预留 1024，
保留上下文 12000，关闭 thinking；实际 temperature 为 0.3，max_tokens 为 1024，
没有提交 seed。实际请求参数保存在每个场景的 result.json 中。结果只说明这些实测
轨迹，不能解释为确定性模型保证。

运行器：`python -m tests.manual.structured_observation_live`。
使用生产 `run_task_kernel`、KernelEffectSink、受管结果回执及正式恢复入口。
只有来源 search/fetch backend 使用受控材料，且只允许两个指定的 `.test` locator；
这不是互联网搜索质量测试，也不是进程 kill 故障测试。

本批原始记录：

- `artifacts/structured-observations-live-20261024-01/`：首次八场景。
- `artifacts/structured-observations-live-20261024-02/`：修正验收器的工具别名判断后，
  重新运行重定向和页内定位；恢复原有页内搜索显示格式。
- `artifacts/structured-observations-baseline-20261024-01/`：相同运行器对提交
  `dd51049e` 的基线比较。原始请求和失败记录保留。

结果：

| 场景 | 原生 | 文本 | 说明 |
| --- | --- | --- | --- |
| 搜索后读取重定向来源 | 通过 | 通过 | marker、最终 URL 正确；恢复事实一致 |
| 搜索零结果 | 通过 | 通过 | 无虚构来源；恢复后仍无来源 |
| 读取失败 | 通过 | 通过 | 如实返回具体失败；帮助链接不变成来源 |
| 页内搜索后按要求 open_hit | 未通过 | 未通过 | 模型跳过 open_hit；不能当作完整流程通过 |

两轮本批实机运行中的来源事实都可从结果回执重建；重复恢复稳定，恢复期间没有增加
backend 调用。首次重定向场景的验收器错误地只接受 `open_result`，但真实执行记录
将该别名规范化为 `open_url`；第 2 轮修正为接受两者并重新执行，未修改原始结果。

基线的相同页内场景也跳过 open_hit。基线四个含长来源场景全部在恢复时报
`recovered source body differs from observation`：原 canonical 投影裁剪正文，
却保留原摘要。本批无损 canonical 修正消除了该恢复错误。

## 验收边界及已知限制

实机 open_hit 要求在原版与修改版均未满足，作为第 4 批完成契约的已知限制记录，
不独立阻止第 1 批合并；不把两边的失败改写为完整流程通过。此前将这一要求作为
第 1 批结构化观察的准入条件混淆了批次边界，现按用户确认的比较标准修正。
最终相关选择没有失败需要归因；不将先前全仓测试中的无关失败认定为本批引入或已修复。
性能比较及正确性结果见上表；本批没有发现原来有效路径的新增回归。
没有为测试题追加生产 prompt、隐藏工具调用或新增独立模型循环。本批没有提前修改
第 4 批完成门。

旧版本已裁掉正文的历史回执不能凭本批补回内容；缺少结构化观察的历史记录也不能
靠预览或参数升级成成功来源。这些记录明确拒绝，不伪造迁移成功，也不自动重新执行。

本批独立提交并合并到本地 master；基线比较材料保留供后续批次比较。
