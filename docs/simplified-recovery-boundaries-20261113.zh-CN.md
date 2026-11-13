# 简化复制恢复与原生结果交付的边界修复

本轮修复对 `2d94a0e7` 的三个复现问题。沿用现有 CodeMode、kernel、浏览器执行器、原子回执、快照与压缩归档；没有增加运行时、浏览器入口或补读授权，也没有创建 worktree。开始时已有的三个未跟踪文件 `docs/browser-leaf-exposure-ab-20261113.zh-CN.md`、`tests/test_browser_leaf_exposure_ab.py`、`tools/browser_leaf_exposure_ab.py` 不属于本轮，未修改或提交。

## 修改

1. **已写入但身份未确认时明确阻塞。** `browser_copy.write_unconfirmed()` 读取原回执的结构化 `mutation_applied` 事实。推导下一轮复制快照时，先检查目标文件的这类回执，再推导完成或编辑阶段；较早的成功写入不能掩盖较晚的未确认写入。即使父子引用集合不完整，也不能将已发生的写入当作可重试。该复制操作明确停止，给出文件路径和原回执引用，要求核对当前文件和工作区身份后再开展其他操作，不自动开放另一套核对入口。
2. **无损编码保留回执保护。** 原生工具结果做重复文本编码时，仅编码正文，把原 `ResultMetadata.render()` 生成的标记和后续文本保留在编码正文之外。语义压缩仍从同一标记识别回执；补读不可用时不能选择该结果做摘要。未新增元数据格式或正文副本，原始内容仍在现有归档的 `source` 中。
3. **结构化冲突与简化错误视图。** 文件写入边界的两条版本冲突分支，统一产生 `STALE_READ_REFERENCE_CODE`，由原 `ToolOutcome.audit` 传入子回执。普通 CodeMode 的 JSON 编辑指导也改为检查此码。简化入口按子回执的结构化原因与写入事实生成模型提示：已写入不重放；未确认写入阻塞；写前版本冲突用同一来源重试。原始运行错误留在父回执 audit，原子子回执不修改。简化父调用的子失败上下文展示结构化原因，不再复述 `read_file`、`readResult.result_ref` 等底层修补说明；一般 CodeMode 的诊断保持可见。只有错误文案、没有结构化原因时，不把它当作安全重试依据。

本轮也清理了触及文件中已有的隐式 Any：包括原 CodeMode host 的结构化读取值、编辑锚点排序回调，以及下一页读取 JSON 参数的类型。没有使用 `cast`、`type: ignore` 或关闭检查。

## 确定性验证

新增测试首先在原代码复现 **8 项失败**，日志 `.e2e-artifacts/recovery-boundaries-repro.txt` 保留。覆盖已发生写入的两种结构化位置、完整与不完整父回执、无损编码后丢失保护、冲突产生处的结构化原因、简化提示泄漏和仅文案误分类。

开发中的验证记录同样保留：

- `.e2e-artifacts/recovery-boundaries-fix-first.txt`：39 通过、1 失败。原冲突测试只构造文案，没有构造新增的结构化原因；改为符合生产回执的数据，同时新增“文案不能授权重试”测试。
- `.e2e-artifacts/recovery-boundaries-fix-second.txt`：44 通过、2 失败。新增测试误用了协议名称和不存在的 HeadlessResult 字段，均已修正。
- `.e2e-artifacts/recovery-boundaries-focused-final.txt`：46 通过、1 失败。测试错误地在持久化工件中寻找未持久化的 controller 异常文案；改为验证真实的失败父回执、停止原因、落盘内容、编辑次数和 mutation_applied 事实。

真实 Chromium 复制测试注入“实际编辑成功、工作区身份确认失败”：先保留一次真实文件冲突回执，再确认写入已发生、后续复制操作停止，没有第三次编辑。另测较早成功写入不能掩盖未确认写入。压缩测试同时覆盖 Chat Completions 原生结果和 Responses 的 function_call_output：先实际完成无损编码，再验证隐藏补读时不能被语义压缩选择，补读可用时仍能选择，重复编码保持稳定。

最终相关回归为 **411 passed、18 subtests passed**，耗时 282.82 秒，完整日志为 `.e2e-artifacts/recovery-boundaries-regressions-final.txt`。选择了 33 个相关测试文件，覆盖真实 Chromium 的复制、上传、下载、Research 提交，以及普通工具运行、身份失败、快照、重放和压缩；未运行全量 pytest。退出时有一条 RAM 盘临时路径清理警告（WinError 3），测试本身通过。

Ruff 通过，日志为 `.e2e-artifacts/recovery-boundaries-ruff-final.txt`。`python -m mypy codey --python-version 3.11` 对 **679 个生产文件**通过，日志为 `.e2e-artifacts/recovery-boundaries-mypy-final.txt`。额外对本轮 **10 个 Python 文件**运行 `--disallow-any-expr`，本轮文件 **0 项诊断**，结果索引为 `.e2e-artifacts/recovery-boundaries-any-changed-files.json`。全依赖树的历史诊断仍保存在 `.e2e-artifacts/recovery-boundaries-any-final.txt`，没有将本轮归零表述为全仓库无 Any。

## 实机矩阵

使用原 `tools.browser_full_surface_live_gate`，本机 KoboldCpp 的 `koboldcpp/Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M`，原 seed、授权和场景。首轮目录为 `.e2e-artifacts/recovery-boundaries-full-surface-20261113-first/`，终端日志为 `.e2e-artifacts/recovery-boundaries-live-first.log`。生产源码在回归结束后冻结，矩阵期间不修改生产代码。

完整矩阵首轮 **11/11 通过**，未为获得通过而额外重跑。`summary.json` 的源码摘要前后一致：`a3bd9708c02c3102a33bb8b4cb095c13a2821ec1c251ba0f898ad5c8ad161302`。

| 场景 | 模式 | 结果及回执核对 |
| --- | --- | --- |
| basic | Chat | 通过；导航、填写、点击、等待、读回。 |
| structured | Chat | 通过；页面事实、token、标签页 URL 和输出格式校验。 |
| legacy_structured | Chat | 通过；完成旧结构化输出要求；中途失败仍保留，见下。 |
| reread | Chat | 通过；恢复 5 个快照，实际读取正文 2 次。 |
| stale_ref | Chat | 通过；真实过期引用拒绝 1 次，重新观察并完成。 |
| stale_edit | Hybrid | 通过；真实文件版本冲突 1 次，结构化原因是 stale_read_reference，重新读取后的恢复标志为 True，最终文件值正确。 |
| upload | Chat | 通过；固定程序完成观察、附件选择、等待和页面读回，文件名、标记和 hash 相符。 |
| download | Chat | 通过；受管下载路径、字节数与 hash 回执有效；中途模型解包错误保留，见下。 |
| research | strict Research | 通过；交互、等待、读回和 submit_research_answer。 |
| source_failure | Chat | 按预期 blocked；受控 fixture 的来源 HTTP 403，不将尝试当作成功。 |
| fixed_failure | Chat | 按预期 blocked；固定来源的 HTTP 403 保留原尝试回执。 |

11/11 是场景最终判定，不表示模型每次调用都正确。已检查原始 `provider.jsonl` 和 `receipts.json`：

- legacy_structured 第 4 次响应只调用导航，没有 return，形成 missing_return。第 5 次响应带 repair_of，却提交了以 browser_snapshot 开始的不同子序列，因此原重放边界拒绝，产生 repair_rejected。模型随后继续，最终完成。两次失败都属于模型输出，不修改生产合同迁就。
- download 首次直接访问 browser_snapshot 返回的 `.elements`，遗漏 Result 解包，被原 result_contract 守卫拒绝；下一次修正解包后完成下载。
- stale_ref 和 stale_edit 的失败回执来自场景要求的真实故障注入，随后按原恢复规则完成。Hybrid 是普通 CodeMode 合同，保留其 JavaScript 编辑指导；本轮只在简化入口中隐藏不适用的底层步骤。
- 两个来源失败场景的 HTTP 403 是 fixture 预期，不是新的环境故障。本轮未出现额外的实机环境失败或新的确定性生产失败。

所有中途失败另有索引 `.e2e-artifacts/recovery-boundaries-live-child-failures.json`，原始请求、回执和首个拒绝仍在各场景目录中。实机矩阵不直接注入“复制写后身份未确认”；该边界由本轮真实 Chromium 生产回归覆盖，不能把一般 Hybrid 冲突恢复当作它的替代证明。

## 剩余边界

身份未确认的复制操作采用明确阻塞；本轮不实现自动核对、自动重试或自动恢复任务。无损编码保留信息和回执标记，但不能保证小模型总能正确解读编码内容；预算不足仍沿用发送前的明确溢出失败。旧的、只有文案的失败回执不会被推测成新的结构化冲突。模型仍可能写错 JavaScript、解包错误或遗漏必需动作，实机失败与确定性生产测试分别报告。
