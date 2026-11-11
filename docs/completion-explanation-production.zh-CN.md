# 解释组装与普通 Chat 双声明合同调整

日期：2026-11-11。[实施方案](completion-explanation-source-gaps-fix-plan.zh-CN.md) · [根因调查](completion-explanation-source-gaps-root-cause.zh-CN.md) · [历史适配器 A/B](completion-explanation-ab.zh-CN.md)。

## 生产行为

保留 B：显式 `output_format="json_explanation"` 时，模型在 `done.summary` 提交解释，`prepare_completion_output` 将完整字符串放入 `/explanation`，从正式回执绑定声明的其他字段，最终经过已有 schema 和完成检查。引号、换行、首尾空白、空字符串及 JSON 外观均不在归一化时改写；空字符串能否交付由输出 schema 决定。

`output_schema` 必须声明必填字符串 explanation；省略时使用该最小对象 schema。精确字段通过已有 `require_tool_success(... answer_pointer=..., result_pointer=...)` 声明绑定。绑定不得覆盖根、explanation 或其子路径；字段缺失、类型不符、父调用未提交或回执冲突仍拒绝。声明沿原 required_checks 冻结，不额外存一份绑定结果。该模式可由 API / Python Headless 显式启用；不新增 UI 或 CLI 开关，strict Research 在提交时拒绝混用。

普通 Chat（`task_kind == "chat"` 且非 strict Research）的 text、json、json_explanation 合同同时移除 source_gaps、execution_claims。模型仍看到真实工具结果和来源覆盖，用来解释限制。若旧式回复多填这两个外层字段，文本与原生协议在同一个归一化入口整字段忽略，原始响应保留。其余参数、外层协议、重复键和大小限制继续严格检查；不处理解释或业务 JSON 中的同名字段。

不生成新的 execution_claims，不增加事实表或执行循环。现有完成门直接解码正式 shell/run 及浏览器回执，读取 task_source_coverage 和 ledger。浏览器或 CodeMode 父回执不能证明进程成功；必要工具未执行、要求成功却非零退出、要求结束却仍运行、必要来源未尝试都仍阻断。真实必要来源不可获取按原规则 blocked。分析失败本身的任务不被一概禁止，后来成功及可选来源失败按原任务要求判断。

冻结快照写入 version 4，唯一布尔标记为 ignore_runtime_claims；version 1～3 恢复默认为 False，保留旧合同。恢复不猜当前 Registry，不跳过 implementation identity，不改写历史 no_progress。Hybrid、Project、Research 等保持各自声明语义。report_sections 和 knowledge_refs 保留；报告修复模式读取原请求冻结 done 的必填字段，模型多填字段不能改变模式。

直接相关的确定性问题一并修复：json_explanation 在生产归一化阶段被 strip；外层重复 JSON 键被覆盖；修复模式由提案字段触发。重复键检查复用共享 JSON 解码辅助函数，覆盖文本和两种原生 API 协议。

## 当前源码验证

相关生产回归 **373 项通过**，另一个不重叠的 provider / 原生终止 / 修复快照 / ToolSpec 回归组 **44 项通过**，共 **417 项**。包含真实 Headless Chat/Hybrid 文本及原生入口、失败进程与运行中进程、声明负例、来源覆盖、回执绑定、冷恢复、浏览器上传下载、重接管及 CDP 生命周期。未运行全量 pytest。

修改的 **25 个 Python 文件通过 Ruff 和 strict mypy**；同时检查 api_provider / api_codec 的类型依赖。修改文件不含 Any、cast 或 type: ignore，没有关闭检查。mypy 使用 `--explicit-package-bases`，避免无 __init__.py 的 tools 同时被当作两个模块名。一次辅助测试命令写错测试文件名，未执行测试；纠正后 44 项通过，不计为生产失败。

最终 A/B 目录为 `.e2e-artifacts/runtime-claims-20261111-ab-final/`，源码 SHA256 为 `36dde2ace4552224c10bb334b97e15225d437242f44be6273522b720927e26a9`，前后相同。真实 Chromium、生产采集链、相同双字段新合同与正式绑定，五类解释各两对、AB/BA 交替，本地 Gemma4、temperature=0、seed=123456789；实际请求和响应均保留。

| 指标 | A：生产 json | B：生产 json_explanation |
|---|---:|---:|
| 首次交付 | 4/10 | 10/10 |
| 最终结构、精确字段及有限事实检查 | 4/10 | 10/10 |
| 模型调用 | 24 | 10 |
| 全部额外解释要求 | 4/10 | 8/10 |

B 没有 source_gaps / execution_claims 拒绝及对应修复调用，10 份解释原样保存，URL/token 精确正确。质量 audit 单独保留；A 的 6 次失败均有原始提案、实际首个拒绝及修复链，不能把最终停机说明的格式检查当作原因。完整 full-surface 与自主案例在独立目录验收，不并入这 10 对。

## 原始提案重放

`.e2e-artifacts/runtime-claims-20261111-replay-final/replay.json` 保留两轮共 20 份原始首次提案、原冻结快照和结果、提案 SHA256、校验过的正式回执及新合同快照。旧合同完成 **5/20**，新生产文本归一化 **20/20**，原生等价归一化 **20/20**；解释逐字保存、URL/token 精确匹配均 **20/20**。

`execution-claims-04B.json` 单独重放 source_gaps-only 批次 04/B：它把浏览器回执申报成 process。新合同文本和原生等价入口均完成，解释和精确字段保持不变。原始中间实验 v4 快照完整保留，生产不为尚未发布的开发形状增加迁移兼容；不重新解释它的原结果。

原生等价重放使用原始外层提案解码出的相同参数重新表达，未手工删字段，不冒充原始原生传输。离线重放不运行模型或浏览器，也不等同于真实进程恢复；历史 implementation identity 检查没有放宽。

## 历史批次与首次失败

以下产物全部保留；不同源码与合同的批次不合并。路径均位于忽略的 .e2e-artifacts，原始状态不加入 Git。

| 批次目录 | A | B | 限制 / 首次失败 |
|---|---|---|---|
| completion-explanation-ab-20261111-main | 1/10，35 调用 | 10/10，10 调用 | 历史实验适配器；B 额外解释要求 7/10 |
| production-explanation-20261111-first | 未同期运行 | 4/10，24 调用 | 6 次虚假 source_gaps；过程中补过生产代码，不作为固定源码验收 |
| production-explanation-20261111-final | 未同期运行 | 1/10，31 调用 | 固定源码，9 次虚假 source_gaps；采集均成功 |
| source-gaps-fix-20261111-ab | 3/10，26 调用 | 9/10，13 调用 | 仅移除 source_gaps；04/B 的 execution_claims 错误促成范围调整 |
| source-gaps-fix-20261111-ab-v2 | 5/10，21 调用 | 10/10，10 调用 | 双字段、中间两个内部标记版本；B 全部要求 7/10 |
| source-gaps-fix-20261111-ab-v3 | 4/10，24 调用 | 10/10，10 调用 | 单标记，之后恢复非 Chat 的原始描述；B 全部要求 8/10 |

A/B 表中通过为最终结构、精确字段及有限事实检查，首次交付率须读取对应 first_delivery_successes，不能用最终完成替代。B 这些通过均首次完成。事实 marker 和排版模式属于有限检查，不证明解释完全正确。

较早的 production-explanation-full-surface-20261111-first 为 5/7，structured 的模型程序错误、Hybrid 的旧文件版本修复失败均保留；独立单次重跑 structured 仍失败（URL 抄错），Hybrid 成功，不拼接统计。旧 structured 使用 text，不能证明 B 的绑定能力。

source-gaps-fix-20261111-surface-first 因范围调整中断。surface-v2 因审计工具未处理无 JSON ref 的下载元数据中断；该工具错误已修复。surface-v2-rerun 为 **6/10**，通过的恰为 basic、legacy_structured、reread、stale_ref、download、research；structured、stale_edit、upload、source_failure 失败。此前文档把 structured 错列为通过，现按原始 summary 更正，原始 summary 未修改。

surface-v3 原始脚本结果 **4/10**：basic、reread、stale_ref、research 通过。逐项原始证据显示：structured 的 CodeMode 程序漏 return、修复又未消费原子调用顺序；stale_edit 使用无效 base_result_ref，随后 provider 超时；upload 已上传但解释使用 [content] 占位；download 先误用 snapshot 解包，再出现 provider 响应混入 HTTP headers 的错误；source_failure 未实际读来源。legacy_structured 的 URL 实际正确但带 Markdown 加粗，被旧验收正则误判；修正的是 gate URL 提取，补了正确 Markdown / 混入错误 URL 的回归，未改生产合同，也不改写旧统计。

自主新合同 source-gaps-fix-20261111-autonomous-v2 为 1/1 完成、5 调用、3 次模型程序失败。较早 production-explanation-20261111-autonomous 也单独保留，不并入固定采集统计。

## 结果解释与剩余限制

### 提交时状态：用户要求先提交并停止

最终 full-surface 批次 `.e2e-artifacts/runtime-claims-20261111-surface-final/` 在额外创建 worktree 后发生机器死机、重启，运行中断，未取得完整批次 summary，不作为完整通过验收。已保存场景的首次结果保留；未完成项不算通过。此后不再使用 worktree，不启动新实机或测试；按用户明确要求提交当前修改后停止。

最终矩阵已观察到 Hybrid stale_edit 失败：模型首次编辑没有提供 base_result_ref，后续反复从 unwrap 后的数据中读取 result_ref，得到 undefined。写入被拒，项目文件仍为外部修改后的内容，stop_reason=no_progress。该案例没有完成真正的旧文件版本修复，不能拿 DOM stale-ref 通过替代文件 stale-edit 验收。

额外补查 `tests/test_versioned_content_edit.py` 为 **14 通过、2 失败**：两例实际拒绝非法 base_result_ref 属性，但错误提示文字不符合测试中要求的 readResult.result_ref / not readResult.value.result_ref 文案。它们不在前述 417 项通过中；基线运行未完成，不能断言已证实是历史问题，也不能声称相关测试全部通过。该文件及 CodeMode 的对应提示实现本轮未修改，问题保留。基线目录日志为 no tests ran，不能用作基线结论。

创建 worktree 是本次操作失误；机器重启的具体系统原因未经诊断。不扫描、清理或继续使用该副本，后续应直接在原工作区验证。最终源码的自主 Chat B 新样本未完成；之前成功样本保留其原批次限制。

收回两个声明解决的是多余的事实重编码职责；不保证模型正文无虚假陈述，也不保证自主浏览器程序、项目写入或自然语言排版正确。固定 Chromium 采集 A/B 仅隔离交付责任，不能替代自主 full-surface。

每次实机保存实际请求、seed、响应、正式回执、首个完成拒绝及后续修复、停止原因、事后格式检查和源码指纹。工具程序失败可能发生在 done 之前，此时 first_rejection 为空；应读原始 provider 与工具回执。停机说明不是 JSON 的事后检查不作为首个失败原因。

不通过继续忽略其他参数、把浏览器放宽为进程证据或反复重跑追求全绿。当前模型和服务环境的失败需分别报告，后续改进另定范围。
