# 普通 Chat 完成合同调整：运行时负责来源与执行事实

日期：2026-11-11。实施记录和实际验收结果见[生产验证报告](completion-explanation-production.zh-CN.md)，问题证据见[根因调查](completion-explanation-source-gaps-root-cause.zh-CN.md)。本文件取代此前“只移除 source_gaps、保留 execution_claims”的范围决定。

## 范围与职责

保留 B 的生产 `json_explanation` 组装。程序负责编码、正式回执绑定、来源覆盖和执行事实；模型负责解释、结论及限制。普通 Chat 的判定固定为 `task_kind == "chat"` 且非 strict Research，对 `text`、`json`、`json_explanation` 一致，不根据采集结果临时切换。

| 合同 | source_gaps / execution_claims | 其余参数 |
|---|---|---|
| 新普通 Chat | 冻结 ToolSpec 不展示；旧式多填整字段忽略 | 严格校验 |
| 历史快照 v1～3 | 原定义、原校验 | 原定义、原校验 |
| Hybrid、Project、strict Research、evidence followup | 保持各自现有定义 | 保持各自现有定义 |

不清理 `report_sections`、`knowledge_refs` 或其他未知字段，不自动生成声明写回 `done.args`，不建立第二份执行事实。模型正文仍可能说错；撤销申报职责不能证明自然语言陈述真实。

## 唯一合同与归一化入口

`done_spec_for_purpose` / `build_turn_snapshot` 在现有边界投影两个字段及其共享说明。文本提示、文本 schema、原生工具 schema 都由同一个冻结 ToolSpec 生成。来源上下文继续展示真实覆盖、失败和阅读要求，去掉填写废弃字段的说明；不增加针对这两个字段的修复提示。

处理顺序为：原始响应保存 → 外层解码 → 工具、权限和控制器检查 → 按冻结规则移除两个旧声明 → 严格参数校验 → 规范化 ToolPlan → 原有输出组装与完成门。

兼容规则仅作用于外层 `done.args.source_gaps` 和 `done.args.execution_claims`。列表、对象、字符串、数字、布尔值、null 统一整字段忽略；不逐条筛选。解释或业务 JSON 中的同名内容不变。重复键、错误外层协议、体积限制、缺失或错误类型 summary、其他未知参数继续拒绝。浏览器、实验 provider、组装器、完成门不重复删字段。

## 冻结与恢复

`TurnSnapshot.ignore_runtime_claims` 是一项内部布尔规则，与普通 Chat 投影同时冻结，不是模型参数、用户配置或 API 开关。快照写入 version 4，严格检查形状与布尔类型；version 1～3 恢复默认 False。

恢复沿用原快照和同一个归一化入口，不从当前 Registry 或任务状态猜测。implementation identity 检查保持有效；历史 no_progress 不重写，不自动重跑浏览器。开发过程中出现的中间实验 v4 形状不作为已发布历史 schema 接纳。

## 正式事实与任务要求

继续使用 `task_source_coverage`、正式 effect intent/settlement、ledger 和已有回执解码。未申报 execution_claims 时，现有完成检查已经遍历相关执行回执，无需新建执行系统。

| 正式事实 / 要求 | 判定 |
|---|---|
| 必需工具未执行 | 拒绝完成 |
| 要求命令成功，真实退出码非零 | 成功要求不满足 |
| 要求结束而进程仍运行 | 不能证明完成 |
| 浏览器或 CodeMode 父回执 | 不作为进程成功证明 |
| 分析命令失败且不要求成功 | 可依据真实失败解释 |
| 必要来源未尝试 | 拒绝完成 |
| 必要来源真实不可获取 | 按既有规则 blocked |
| 失败后成功、可选来源失败 | 按既有覆盖规则判断 |
| 父调用未提交、字段缺失或冲突 | 仍拒绝绑定交付 |

不采用“任何失败回执都阻断”的新规则。原 `validate_source_gaps` 和执行声明校验继续服务旧合同及其他入口。

## 两项直接相关修正

报告修复模式读取原请求冻结 done 的必填字段，不读取模型提案是否带 report_sections。普通 Chat 可选字段不能切换 Research 修复路径；whole Research 保持 submit_research_answer。

json_explanation 逐字保存解码后的 summary，包括首尾空格、换行、引号和 JSON 外观。冻结 summary schema 明确允许空字符串，最终是否合格仍由输出 schema 决定；其他历史输出模式保留原空白处理。回归必须经过真实生产入口，不能只测组装器。

外层重复 JSON 键原先会在解析时覆盖；在文本与两个原生 provider 的解码边界统一拒绝重复键，避免严格参数检查前信息已丢失。复用共享 JSON 解码辅助函数。

## 验证与交付

1. 自动化覆盖三种 Chat 输出、文本/原生协议、所有旧字段 JSON 值、其他参数负例、真实来源和执行失败、精确回执绑定、新旧快照与冷恢复、冻结修复分支、解释逐字保存。
2. 两轮历史共 20 份首次提案，使用经过验证的正式回执建立新合同，经过生产归一化和完成链重放。原快照、提案及结果保留；原生等价重放明确标记为重新表达原参数，不冒充历史原生传输。另重放 source_gaps-only 批次 04/B 的 execution_claims 原始错误。
3. 固定源码运行同期 10 对 A/B：A 为生产 json，B 为生产 json_explanation；五类解释各两对、交替顺序；同一合同、采集程序和精确回执绑定。不修改模型提案，不在脚本包装 B。
4. 单独运行自主 Chat B、完整 full-surface：Chat、strict Research、Hybrid、快照恢复、重复读取、DOM stale-ref、文件 stale-edit、上传、下载、真实来源失败。受控失败采集和模型自主失败采集分开报告。
5. 每批保留源码指纹、请求、seed、响应、回执、首次拒绝与修复、停止原因和事后格式检查；首次结果与有限重跑分开，源码改变后不拼接统计。
6. 对相关修改运行生产回归、Ruff、mypy，不运行全量 pytest。不引入 Any、cast、type: ignore 或关闭检查。

B 的预设结构交付目标是首次 10/10、10 次解释生成、没有两项废弃声明的拒绝；事实、排版、不重复绑定值及全部要求通过率另报。完整矩阵若存在模型或环境失败，明确保留并分类，不把固定采集成功率当成整体自主成功率，不放宽合同迁就错误。

完成验证后只提交本轮相关生产、测试、工具及文档；原始实机状态保存在忽略的 .e2e-artifacts，不将大型产物加入 Git。
