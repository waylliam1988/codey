# 必需来源、上传说明和任务 URL 改口修复

日期：2026-11-13。基于 `da2377c7` 的生产代码局部修复，没有创建或复制 worktree。

## 行为和边界

- `_URL_LINE` 接受 `Source:`、`URL:`、`来源：`，继续使用现有相邻指令判断。连续的裸地址和带标签地址属于同一组，继承同一个动作；同组否定同样有效。“其他背景资料”等任意标签不在允许范围内。必需来源仍只保存到现有 `required_source_urls`，预取和结构化上传继续共用它。
- 同一份任务文本按规范化 URL 顺序更新要求。否定撤销该地址及其原始写法候选，之后再次明确允许时重新建立候选。已撤销的歧义不会污染新决定；连续允许多个原始写法仍保留导航绑定的歧义保护。不修改已冻结任务的跨轮次合同。
- `json_explanation` 恢复逐字保留模型说明，包括证据暂缺时的草稿。对同时绑定附件名称和 SHA256 的上传说明，完成检查先验证当前页面的附件选择和读回证据，再只检查组装 JSON 中的 `explanation`，不检查 `page_text` 或其他业务字段。
- 已知、明确越过选择回执证据的中英文状态断言触发可重试的完成拒绝。反馈携带真实状态及解码后的原草稿，要求保留分析、只修正状态并重交 `done`。文件名、SHA256、页面文字的正式回执绑定不变，不自动重传附件。
- 状态检查仅覆盖代码列明的表述，处理相应否定、不确定表述和引号/代码中的引用，不是通用自然语言真实性证明。

## 自动化回归

最终相关生产回归在同一次定向执行中 **263 项全部通过**，没有运行全量 pytest。日志为 `.e2e-artifacts/source-upload-regressions-20261113/production-regression-after-live-fix.log`。此前阶段的记录也分别保留，不把重复执行累加为不同测试项：

| 组别 | 数量 | 日志 |
| --- | --- | --- |
| 来源识别、任务导航绑定、来源覆盖、说明组装、浏览器报告/附件及 Chat 合同 | 229 | `.e2e-artifacts/source-upload-regressions-20261113/production-regression-final.log` |
| 预取、结构化上传和相关正式回执交付 | 29 | `.e2e-artifacts/source-upload-regressions-20261113/production-integration-final.log` |

新增生产用例验证：标签地址的预取回执进入首轮请求；模型使用结构化上传；中英文错误说明被拒绝后只重交 `done`；有用分析原样保留；页面正文包含“服务器已接收”不会误报；全程只有一次附件选择、一次选择后读回，且经过快照恢复。

修改前的定向记录保存在 `baseline.log`，其中也包含尚未实现的新检查接口，不把这些接口缺失全部计为旧生产 bug。首轮生产集成 **27 通过、2 失败**，原始请求与正式回执保存于 `integration-first-failures/`：

1. 混合地址用例错误地期待夹具中未提供的第二个页面成功，实际收到 HTTP 404；改用失败来源场景验证两条地址均被预取，不放宽生产完成规则。
2. 中文纠正请求没有明确携带解码后的原说明；上传纠正反馈现直接附上原稿。修复后中英文用例均通过。

集成测试退出时出现 RAM 临时目录清理的 WinError 3 警告，退出码为 0，测试没有失败。失败与最终记录分别保留，未覆盖首次失败。

## 静态检查

- Ruff：10 个变更 Python 文件通过。
- 生产全树 mypy（Python 3.11）：680 个文件通过。
- 变更生产文件和独立回归文件的 strict mypy：9 个文件通过。
- 既有内核类型精度门通过；10 个变更 Python 文件的类型逃逸 AST 检查无违规，没有新增 Any、cast、type ignore 或检查豁免。
- 单独检查旧实验测试 `tests/test_protocol_surface.py` 时，导入链报告 **5 个未修改文件中的 71 项历史错误**，该变更文件本身没有诊断。这不是整体严格测试类型检查通过：`tests/support/provider_actions.py` 6 项、`tests/support/kernel_runtime.py` 11 项、`tests/test_session_log_receipt_recovery_preserves_facts.py` 48 项、`tests/test_mechanical_surface.py` 1 项、`tests/test_structured_surface.py` 5 项。没有扩大本轮修改去重构这些旧测试支持文件，也没有关闭检查。

日志：`ruff-final.log`、`mypy-production.log`、`mypy-targeted.log`、`type-precision.log`、`changed-type-precision.json`、`mypy-legacy-experiment.log`，均位于本轮 `.e2e-artifacts` 目录。

实机补漏后的 Ruff、680 个生产文件和 9 个变更/独立回归文件的 mypy 再次通过，对应日志为 `ruff-after-live-fix.log`、`mypy-production-after-live-fix.log`、`mypy-targeted-after-live-fix.log`。

## 实机记录

使用既有 `tools.browser_full_surface_live_gate`，真实 KoboldCpp Gemma4 模型和 Chromium，生产 Registry、入口、CodeMode、正式回执链均保持原样。完整首轮矩阵 **9/12 通过**，目录为 `full-surface-first/`。首次失败的原始请求、回执和结果独立保留。

模型：`koboldcpp/Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M`，seed `123456789`，每次模型请求超时 90 秒。生产源码运行前后摘要相同：`9571998d27f964c5a6417f395b4f5f12240c75d9cab30f289746d0d4fe5e0560`。

| 场景 | 首轮结果 | 原始请求与正式回执核对 |
| --- | --- | --- |
| basic | 通过 | 实际浏览器操作和正文事实交付 |
| structured | 失败，模型输出 | URL/token 绑定精确且说明原样保存；模型说明漏掉净重 173 和校验码 QUARTZ_851，另有字面反斜杠换行。schema 合格不等于任务内容完整 |
| legacy_structured | 通过 | 真实 eval、tab list 和元数据交付 |
| reread | 通过 | 读回两次，恢复 10 次快照 |
| stale_ref | 失败，模型程序 | 寻找输入框的循环提前 break，按钮 ref 保持空串；重复提交空 ref 后无进展，未覆盖预期的真实 stale-ref 拒绝 |
| stale_edit / Hybrid | 失败，模型恢复 | 达到版本校验点，注入并正确拒绝 1 次真实冲突；之后模型把解包后的内容当作编辑凭证，恢复未完成，最终文件不正确 |
| upload | 通过 | 真实附件选择、等待和页面读回 |
| download | 通过 | 真实下载和持久化文件/哈希核验 |
| strict Research | 通过 | 实际读取、提交和引用 |
| Research sequence | 通过 | 正式等待和读取后，实际模型请求只开放 submit_research_answer |
| source_failure | 通过，预期 blocked | 正式 HTTP 403 来源失败回执，未把失败说成读取成功 |
| fixed_failure | 通过，预期 blocked | 固定采集得到 HTTP 403，正确交付限制 |

三项失败均有模型原始程序/说明与工具回执对应，没有为其修改生产合同，不能把首轮矩阵描述为全部通过。HTTP 403 是受控夹具的预期负例，不是本机网络故障。`provider.jsonl`、`proposals.jsonl`、`receipts.json` 和 `state/` 均保留在各自场景目录。

另运行带标签来源、结构化上传、json_explanation 与快照恢复的专项实机。首轮 `labeled-upload-bound-first/` 的机械字段/回执检查通过，但语义审查失败：模型写了“成功上传了附件”，初版规则只覆盖“已成功上传”等写法，遗漏了这个明确的成功断言。本轮已补上“成功上传”及对应否定、不确定表述测试；修复后的规则能拒绝首次实机原稿。该首次漏检属于本轮生产检查的确定性缺口，单独记录在 `semantic-review.json`，不以机械检查通过冒充说明状态正确。

补漏后的专项 `labeled-upload-bound-final/` **通过**：模型前两次仍声称“用户已成功上传附件”，两次均被生产完成检查拒绝；第三次改为“用户已选择附件”后完成。实际记录共 3 次 `done` 提案、2 次完成拒绝、1 次附件选择、7 次快照恢复。首轮请求包含正式预取回执；最终解释与最后一次模型原稿相同，并且相比首次原稿仅替换了状态措辞，其余内容逐字相同。`semantic-review.json` 记录了这些断言，首次漏检与本次通过分开保存。

专项运行前后生产源码摘要相同：`c9f94317784a08e81f897a9652186192f2f02c57424ffee51df20dd2f9b82641`。复现入口：本轮证据目录中的 `live_labeled_upload.py`，复用现有 `run_case`，不增加执行器或修改模型回复。

由于专项实机后修改了生产状态规则，在最终代码上重新完整运行矩阵，结果为 **12/12 通过，退出码 0**，保存于独立的 `full-surface-final/`，日志为 `full-surface-final.log`。运行前后摘要及提交前重新计算的摘要均为 `c9f94317784a08e81f897a9652186192f2f02c57424ffee51df20dd2f9b82641`。

| 最终场景 | 结果及正式回执核对 |
| --- | --- |
| basic、structured、legacy_structured | 均通过；structured 的精确字段、schema 和实际净重/校验码检查全部通过 |
| reread | 通过；正式 `browser_read` 两次，快照恢复 7 次 |
| stale_ref | 通过；真实旧 ref 被拒绝一次，重新 snapshot 后点击成功并读回正文 |
| stale_edit / Hybrid | 通过；正式版本校验拒绝一次真实冲突，重新读取后编辑成功，最终文件为 `{"amount": 173, "code": "QUARTZ_851"}` |
| upload | 通过；正式附件选择一次，随后等待及读回成功 |
| download | 通过；实际下载 18 字节，文件内容及 SHA256 `bf6e36ad931d47c37734b551ed4026ab33db8610fb71051e4b0b9573da8b3036` 核验通过 |
| strict Research、Research sequence | 均通过；原始请求确认顺序约束满足后只开放 `submit_research_answer` |
| source_failure、fixed_failure | 均通过；正式 HTTP 403 回执后按预期 blocked，没有宣称读取成功 |

首轮三项模型失败与最终通过同时保留。最终结果说明这次模型执行完成了对应场景，不能据此把首轮模型失败归为生产缺陷已修复，也不代表模型每次都能正确执行。两轮之间与生产有关的变化仅为上述上传状态措辞漏检补充，没有放宽导航、回执、编辑版本或完成合同。

说明保留的程序保证是：每次被接受的模型草稿在组装时原样保存。模型修正后的说明质量仍依赖模型，状态检查也不覆盖任意自然语言表达。
