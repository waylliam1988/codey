# 原始读取对象绑定编辑：完整表面实验

2026-11-11。脚本为 `tools/receipt_bound_edit_surface_probe.py`。仅修改 tools、tests、docs，不修改生产代码或生产 prompt，不创建 worktree。复用真实 Headless、生产 kernel、工具执行器、来源覆盖和完成门禁；网页由独立的真实 Chromium 加载受控 fixture。

## 对照范围

A 使用现有编辑接口；B 安装同一份原始读取对象包装层，将已有的 `edit` 替换为 `edit({read, content})`。包装层最终调用原有主机 edit，不重新读取、不重试、不放宽权限或版本检查；没有 edit 的 catalog 不增加 edit 声明。A/B 使用同一任务文字、provider 类、模型、temperature=0、seed=123456789、预算与场景。轮流执行 AB、BA，各单元独立子进程，全部串行，不自动重跑失败项。

模型 provider 为现有 `ExperimentProvider`：文本 JSON schema 协议，实际 HTTP 请求与响应保留。与此前普通编辑实验的 native/RecordingProvider 接入不同，不能跨批次把成功率差异归因于编辑接口。固定 seed 不保证输出跨上下文确定。

可选的第二模型代码审查在两组实验进程内标为不可用，不伪造审查通过。用户模型配置不变；任务的工具成功要求、生产来源覆盖、真实回执、done/Research 提交检查仍执行。

## 场景及验收

| 场景 | 验收要点 |
| --- | --- |
| basic | Chat 完整浏览器操作，报告净重及校验码 |
| structured | Chat json_explanation，URL/token 从正式回执绑定 |
| legacy_structured | 普通 Chat 报告 URL/token，保留原来的文本任务 |
| hybrid | 浏览器读取后，更新项目 result.json |
| research | strict Research，来源原文与 [eN] 引用 |
| snapshot_restore | 每轮将冻结工具快照序列化后恢复，检查契约完全一致 |
| reread | 加载前后重复读取同一来源，固定采集时钟，以后一次正文为准 |
| stale_ref | 故意使用 DOM 旧 ref，拒绝后重新 snapshot 并完成 |
| stale_edit | 目标完整读取后外部修改；必须真实版本冲突、随后重新读取并成功编辑，保留外部新增字段 |
| upload | 实际选择附件、等待、读取；答案须含真实文件名、内容标记、SHA256 |
| download | 下载的实际字节与 SHA256 正确，报告文件名及 hash |
| source_failure | 模型自主执行必要 open_url，403 后生产完成门进入 blocked |
| upload_bound | 独立诊断：尝试用已有 json_explanation 绑定文件名、hash、browser_read 正文 |
| source_failure_fixed | 独立诊断：固定首条 CodeMode/open_url，真实工具读取 403 后让模型解释 |

snapshot_restore 只验收冻结 turn contract 的序列化恢复，不冒充整任务杀进程重启。source_failure_fixed 的第一步由脚本提供，不算模型自主选择工具。upload_bound 两组都使用相同绑定声明，不能把它的行为算作编辑接口收益。

旧 source-failure 脚本为必然返回 403 的来源又声明了工具必须成功。本轮将实验请求改成“必要 URL 必须尝试”：保留任务原文与 sources_open_required，通过入场冻结的 required URL 和来源覆盖验收；不再同时要求 open_url 成功。只有真实来源失败回执及 blocked 才通过，未尝试不能通过。

stale_edit 用原始正式回执区分凭证漏传与有效凭证过期。只有此前成功完整、同路径的读取凭证和生产版本冲突错误才计入 true_stale_conflicts；恢复须使用冲突后新读取的凭证。注入的 external_revision 必须保留。

## 已确认的边界

脚本化 HTTP 回答配合真实 Chromium 验证发现：browser_upload 的 canonical.value 含文件名和 SHA256，可供现有绑定器使用；browser_read 的正文保存在 canonical.research_observation.text，没有绑定器当前要求的 canonical.value。因此 upload_bound 的正文绑定不具备现有生产支持，模型重复修改解释也不能补齐 page_text。保留该负例，不为通过而改写回执或放宽 schema。

每个上传单元另生成 receipt-report.json，从原始成功上传和随后成功读取的回执直接提取文件名、hash、字节数及页面正文，带回执编号。它只验证机械组装所需事实是否存在，不替换模型答案、不改变 passed，也不证明生产应用已经交付这些字段。effect_verified=false 不改写为服务器已接受上传；fixture 只证明文件输入选择及页面显示。

## 运行及证据

```powershell
python -m tools.receipt_bound_edit_surface_probe --run-dir .e2e-artifacts/receipt-bound-edit-surface-20261111 --timeout 90 --budget 300 --max-turns 10
```

默认 14 对、28 单元。--scenario 可单独选场景，但不能用补跑成功结果覆盖首次矩阵。--budget 是单元总超时，--timeout 是单次模型请求超时。终止只针对脚本拥有的子进程树。

manifest.json 记录模型、协议、seed、脚本内容摘要与生产源码内容摘要。各单元保留 task.txt、request.json、provider.jsonl、programs.jsonl、events.jsonl、receipts.json、首次工具及首次完成拒绝、生产 durable state 和 summary。配对检查任务文字相同、生产源码摘要一致。原始失败保留；进度写入 progress.json，完整结果写入 summary.json。

## 本轮实机结果

完整矩阵位于 `.e2e-artifacts/receipt-bound-edit-surface-20261111/`，共 **23/28**：A **11/14**、B **12/14**。排除两个独立诊断后，常规场景为 A **10/12**、B **11/12**。脚本正常完成整个矩阵，退出码 1 来自五个评分失败，没有超时中断，也没有补跑替换首次结果。

| 场景 | A | B | 模型调用 A / B |
| --- | --- | --- | --- |
| basic | 通过 | 通过 | 3 / 8 |
| structured | 通过 | 通过 | 4 / 2 |
| legacy_structured | 失败：max_turns | 失败：done 后事实检查未通过 | 10 / 5 |
| hybrid | 通过 | 通过 | 6 / 4 |
| research | 通过 | 通过 | 5 / 5 |
| snapshot_restore | 通过 | 通过 | 8 / 8 |
| reread | 通过 | 通过 | 8 / 4 |
| stale_ref | 通过 | 通过 | 4 / 4 |
| stale_edit | 通过 | 通过 | 5 / 5 |
| upload | 失败：漏报 SHA256 | 通过 | 2 / 2 |
| download | 通过 | 通过 | 4 / 5 |
| source_failure | 通过：真实 403，blocked | 通过：真实 403，blocked | 2 / 2 |
| upload_bound，独立诊断 | 失败：no_progress | 失败：no_progress | 5 / 5 |
| source_failure_fixed，独立诊断 | 通过：blocked | 通过：blocked | 1 / 1 |

全部 14 对的任务文字一致，全部单元的生产源码摘要一致，运行期间五份实验脚本的内容摘要不变。生产摘要类型为 codey-files-sha256-v2，值为 `0686d61cd4fbd923cfb089a89abc0f772a9331366c8152a9c756b49a118ca4b1`。wire-audit.json 核对每份真实请求的 seed=123456789、temperature=0，及实际工具声明；没有编辑接口的 catalog 未增加 edit。

### 五个失败单元

1. **legacy_structured / A**：浏览器记录已读到，但 browser_eval 的代码含顶层 return，页面求值报 `Illegal return statement`；后续还有 CodeMode 编译错误和重复错误求值，10 轮耗尽。没有完成提案，因此 first_rejection 为空；首次工具拒绝已另行保存，不能把 max_turns 停机文字当作首次错误。
2. **legacy_structured / B**：先对不匹配的内容做正则并取匹配结果下标，报 `cannot read property '1' of null`。之后 browser_eval、browser_tab_list 都成功，但最终仅写“从 window.__contractToken 读取”的占位说明，没有真实 `SCHEMA_TOKEN_74`。生产任务正常 done，场景的事实检查失败；本轮并未给普通文本任务追加 token 的精确输出绑定。
3. **upload / A**：browser_upload、browser_wait、browser_read 均成功，最终报告了文件名及包含真实 `UPLOAD_BYTES_82` 的整段页面文本，但遗漏选择回执的 SHA256。没有工具拒绝或完成拒绝；失败发生在答案事实检查。B 完整报告三项事实，2 次调用通过，但没有使用编辑入口，不能据此宣布编辑原型修好了上传。
4. **upload_bound / A**：首次程序用 `unwrap(finalRead).text`，而 browser_read 声明并实际返回字符串，最终 page_text 为 undefined，CodeMode 父调用失败。随后改写程序未正确消费原子回放控制流，父调用仍未成功。完成门首先拒绝缺少 filename：上传叶子动作成功不能替代成功的父调用。之后重复修解释，最终 no_progress。
5. **upload_bound / B**：首次同样错误读取 `.text`，报 `undefined at $["probe_value"]["page_text"]`；后续父调用修复也失败，首次完成拒绝同样缺少 filename，最终 no_progress。probe_value 是实验包装层的返回包路径，原始模型程序和展开程序均保留；不能把停机说明中提到的缺字段反过来当作首个错误。

upload_bound 的**实机首次失败**是模型程序与父回执问题；前文 browser_read 没有 canonical.value 的**绑定机制限制**由脚本化回答、成功父调用及真实 Chromium 的负例单独证实。本轮不混淆这两层原因，也不通过修改回执、忽略缺字段或补跑把它变绿。

### 编辑及来源的证据

Hybrid / A 首次编辑漏传 base_result_ref，被现有执行器拒绝，随后恢复，最终 6 次调用完成。Hybrid / B 使用原始读取对象，一次编辑完成，无失败工具回执，4 次调用。B 的成功 guest 包计数确认使用了原型。

stale_edit 两组均有且只有一次真实版本冲突：凭证对应此前成功、完整、同路径的读取，主机明确返回文件版本变化。之后均重新读取并成功编辑，最终 `amount=173`、`code=QUARTZ_851`，外部 `external_revision` 字段完整保留；两组均 5 次模型调用。B 的失败 guest 包计数仍为未知，失败编辑依据原始主机回执确认，不把失败计数当作零次采用。原型没有用隐藏读取为旧正文取得新凭证。

source_failure 两组都由模型自主调用 open_url，真实来源回执为 403，最终 blocked。source_failure_fixed 首次调用由脚本提供，来源仍经真实生产工具取得，后续各用 1 次模型调用解释限制。它说明已知 URL 的前置读取可以在实验脚本中机械执行，不能据此声称生产入口已实现自动前置读取，或单独证明历史失败只有一个原因。

四个上传单元的 receipt-report.json 都能从原始上传与来源回执提取文件名、hash、字节数和页面正文，且保留 effect_verified=false。它证明所需事实存在，不证明生产绑定器已能消费正文，更不替代上述失败的真实交付结果。

### 结论与限制

原始读取对象接口在本轮被模型实际采用，正常 Hybrid 和真实 stale-edit 均保持正确写入及版本保护；快照契约恢复、重复读取、DOM stale-ref、strict Research、下载也通过。它可以消除显式传递路径和回执编号的机械负担，但不能统一解决页面返回类型误用、父调用回放、正文事实遗漏和当前绑定器的正文选择限制。

全矩阵模型调用 A/B 为 67/60，输入 token 为 523,637/449,452，输出 token 为 9,799/8,634。这些只是描述性统计，不作为接口收益：只读 basic 的调用数就从 3 变为 8；不同单元有不同失败路径，完整请求还含不同 workspace、运行时钟与回执标识。任务文字相同、seed 相同不等于完整上下文逐字相同。仅有一次配对，stale-edit 两组均通过且调用数相同，不能证明成功率稳定提升，也不足以直接宣布整体方案已适合生产。

本轮相关回归 **52 项通过**，包含真实 Chromium 与脚本化 HTTP 的正反例、已有编辑原型和快照测试；Ruff、strict mypy 通过。pytest 的 RAM disk 长路径清理告警单独保留，未修改测试基础设施，不影响退出码 0。这些检查不覆盖旧生产报告中 versioned_content_edit 的两项提示文案失败，未将其宣称为已修复。

用户要求本组结束后更新文档、汇报失败、commit 并停止。按此结束本轮：不启动新实机、不补跑追求全绿、不改生产代码，不创建或使用 worktree。
