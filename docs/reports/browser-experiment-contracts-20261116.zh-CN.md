# 浏览器实验合同复查（2026-11-16）

## 范围与归因

本次从上一份报告留下的三个实验开始，扩大验证后继续处理共享的 follow-up、protocol 和 structured 实验适配器及其直接回归。没有修改生产代码或生产合同。以保存的 provider request、原始响应和 durable child receipt 为依据，确认需要修复的是实验适配、受控成功输入与测试断言。`StopIteration` 和后续找不到 snapshot 的 `IndexError` 是脚本未按当前合同执行后的次生失败，不是外部服务或 Chromium 环境失败。

| 原失败 | 当前生产事实 | 修改所有者 |
| --- | --- | --- |
| structured 场景提交 `done.summary` | 请求明确要求 `done.paragraphs`；本实验的最终 explanation schema 还要求两段。缺少业务事实会被 `answer_text_missing` 拒绝。 | 受控成功回复使用两段 paragraphs；负例检查实际拒绝，保留缺 token、缺事实的区分。 |
| 上传任务提交通用 JavaScript | 当前 frozen catalog 提供 `upload.observe/select/read`，原诊断为缺少 `upload`。 | 生产集成测试使用结构化动作，从当前任务的 attachment store 取得真实 attachment ID。命名 helper 与投影另在原 QuickJS runtime 中验证。 |
| 下载测试选择第一条或唯一父回执 | 自动导航与实际程序分别拥有父回执，下载文件本身成功且 hash 正确。 | 通过已有 `child_effect_ids` 定位实际所属父回执；继续检查唯一文件动作、原始 receipt 引用与数据投影。 |
| request_snapshot hook 抛 TypeError | 生产入口传递 `delivered_results`。 | 实验 hook 使用当前具体签名并转发参数。 |
| completion hook 抛 TypeError | 生产入口传递 `metadata`。 | 实验 hook 转发现有 `BrowserMetadata / MetadataPending`，没有生产兼容分支。 |
| direct 实验缺少 snapshot | 普通浏览器 turn 现在也有 receipt progress handoff；旧的 `not handoff` 条件阻止实验提供自己的已授权叶子工具。 | 去除以提示文本为空作为曝光前提的判断；仍由原 expose_leaves 与 frozen policy、allowlist、nested inventory 求交集，保留授权及快照往返不变的断言。 |
| 审计误报生产 follow-up 泄漏 | 当前快照序列化保留值为 null 的可选字段；字段存在不代表启用。 | 检查非空 fill binding 与启用的 follow-up 标志；两个注入真实泄漏的负例仍必须被审计拒绝。 |
| follow-up 消融臂动作数增加、缺少实验 readback | 生产普通浏览器 turn 已自动提供 follow-up，干扰了实验自己的 lower 与曝光控制。 | 各臂共用原有 frozen authority，隔离生产曝光入口，由该臂提供既定表面；保留原动作次数、模型请求次数和成本对账断言。 |
| 合法 browser_navigate 被报 unexpected arg url | 冻结 catalog 的导航 schema 使用 `anyOf`；实验预检重建平面 ToolSpec 时丢掉了组合分支。 | 实验直接复用现有完整 schema 校验器；新增 URL、task target、错误类型、额外字段与互斥分支正负例，不重复导航合同。 |
| 快照测试要求 ToolSpec Python 对象 identity 不变 | freeze/refresh 会重建等价的不可变定义，合同要求定义、权限、执行器与往返相同。 | 删除过时对象 identity 断言，保留每个 leaf 唯一定义、完整定义相等、executor/policy 与序列化往返检查。 |
| 页面重置测试导航数多一条或故障注入打错轮次 | 生产入口已执行首次导航；旧脚本又在第一轮重复导航。 | 第一轮复用入口导航，第二轮仍真实重新导航、观察、填写、点击、等待、读取；保留次数与逐步删除回执的负例。 |
| stale edit 恢复在发送前 ContextOverflowError | 六轮受控脚本最低请求估计 25122，超过该脚本 provider 的 24576 输入预算。 | 仅此测试使用更大的脚本 context budget；保留真实版本冲突、拒绝旧内容绑定新 receipt、重新交付观察与外部字段保护检查。未修改生产预算或实际模型配置。 |
| structured 旧适配器令 amount 获得整段正文 | 实验覆盖 `_observe_value` 后丢掉了生产 `browser_text` 字段选择，真实 edit 回执明确拒绝将正文视为数字。 | structured 删除重复覆盖；protocol 中声明字段选择的要求交给生产选择器。新增正例、缺 marker、截断正文的回归，保留生产的完整 observation 与回执检查。 |
| structured 旧适配器重复组装 URL/token | 生产 completion 已投影当前文档 metadata，旧实验还从过时 tab 字段再组装一次。 | legacy metadata 直接使用生产 completion output，不新增投影器。 |
| structured 测试仍期待旧错误包装或旧 completion 私有签名 | 当前生产保留 timeout_ms 根因，`_required_tool_checks` 要求 report 参数。 | 测试检查实际根因并传入当前 report 参数；不把错误改回旧的 recorded-control-flow 包装。 |
| structured 上传集成继续重复提交旧 steps | 当前上传入口要求 upload 动作，原请求明确拒绝缺少 upload。 | 集成走当前 observe/select/read 并检查一次上传的原始回执；原缓存实验扩展同一 QuickJS 单元测试覆盖 upload 与 download，继续检查同 id 重用、参数冲突及新 id 的真实新调用。 |
| 合法 upload 又被实验改写为 steps | 旧 structured 适配器在生成请求与翻译回复时都无条件安装自己的 steps 合同。 | 当前快照有 input binding 时保留原冻结 schema、提示和回复，不再应用普通编程步骤实验。 |

浏览器 click、wait、read、upload、download 仍执行原生产工具并保留原始回执。没有增加执行器、结果存储或恢复格式。上传集成与实验 helper 的职责分开，避免为测试旧 JavaScript 入口而关闭生产的结构化上传合同。修改文件的动态边界使用现有 JSON decoder、明确的 object 边界和具体参数类型；没有引入 Any、cast、type ignore 或检查豁免。

## 逐次失败记录

上一批 `production-01: 23 failed / 366 passed` 和 `production-02: 4 failed / 68 passed` 原样保留在 [前一份报告](codemode-runtime-convergence-20261115.zh-CN.md)。本次记录独立存放于 `.e2e-artifacts/browser-experiment-contracts-20261116/`。

| 本次批次 | 结果 | 归因 |
| --- | --- | --- |
| chat-01 | 1 failed | 补齐 delivered_results 后，原始下一轮请求显示 completion hook 缺少 metadata。 |
| chat-02 | 1 failed | codemode 流程成功；direct arm 被旧 not handoff 条件挡住，后续 scripted proposal 找不到 snapshot。 |
| entry-01 | 8 failed / 23 passed | 迁移中仍遗漏两段要求、upload_bound 的 paragraphs、browser_read 的正文位置、父回执选择，以及缺业务事实应拒绝的预期。原始诊断与回执分别保存。 |
| affected-02 | 4 failed / 39 passed | 缺事实负例仍使用旧断言；其余三项实际操作成功，但审计将 null 字段误判为启用。 |
| related-final-01 | 31 failed / 215 passed | 扩大到九个明确文件后发现相邻实验失配。原三个实验已通过，但不能覆盖此批失败：7 项 follow-up 消融污染，1 项 ToolSpec 对象 identity 假设，23 项 protocol 成功输入、completion hook、组合 schema 预检、父回执定位、首次导航与测试预算问题。日志名包含 final，不代表通过。 |
| preflight-01 | 18 passed / 104 deselected | 只验证完整导航 schema 正负例与既有 admission/default/configuration 机制。不能替代浏览器回归。 |
| affected-03 | 10 failed / 238 passed | 七个明确文件。此前 31 个失败均已修复，但一条诊断断言未跟进完整 schema 检查的 `args.condition` 路径；新增纳入的 structured 文件有 9 项旧合同/适配问题，分别为旧错误包装 1、缺 report 参数 2、旧上传 steps 2、覆盖生产字段选择 2、重复 metadata 投影 1、重复首次导航 1。 |
| selector-01 | 6 failed / 122 deselected | 本轮新增测试夹具的 source observation 不完整；生产正确拒绝，未以此认定生产失败。 |
| selector-02 | 6 failed / 122 deselected | 改用生产 observation 编码器后，夹具使用了错误长度的正文 hash，被编码器正确拒绝。 |
| selector-03 | 6 passed / 122 deselected | 修正夹具后，原生产选择器保留字段选择，并拒绝缺 marker 或截断的正文。 |
| affected-04 | 2 failed / 40 passed | entry follow-up 与 structured 两文件。此前诊断断言、字段选择、版本冲突、metadata 和导航用例通过；两项上传仍被旧实验改写为 steps，原始请求与回执保存于独立 evidence 文件。 |
| structured-final-01 | 28 passed | 完整 structured 测试文件；修复 input binding 边界后，上传、版本冲突、metadata、预检与原有缓存正负例全部通过。退出码 0；进程退出清理 RAM 临时目录时有文件已不存在的 WinError 3 警告，保留原日志。 |
| typed-boundaries-final-01 | 66 passed / 140 deselected | 最后补充类型后，对四个文件中的实验安装、完整 schema、字段选择、JSON 分支、冻结授权、矩阵审计、fill 往返与原 QuickJS 缓存边界作短回归。不是四个文件的完整重跑。 |
| cli-final-01 | 3 个命令退出码均为 0 | chat/follow-up 两个工具的 `--help`，protocol 工具的 `--plan --config B3`；验证明确类型的 CLI options 路径，无模型请求。 |

`*-evidence.json` 导出了失败批次的原始请求、响应、回执与恢复 artifact，避免依赖 RAM 临时目录。`related-final-01-evidence.json` 保存 117 组证据，`affected-03-evidence.json` 保存 118 组；纯单元测试伪造的 wire 记录没有 durable DB，仍保存 wire，不将缺少 DB 伪报为生产失败。最初的类型失败和 Ruff 的闭包变量绑定失败也单独保留，修正后的检查另记日志。没有修改保存的历史模型原始回复文件，没有将失败任务改写为成功结果。

## 验证范围与限制

上述长批次的文件范围分别为：

- `related-final-01`：`test_browser_entry_candidates.py`、`test_browser_entry_followup.py`、`test_browser_chat_followup_gate.py`、`test_browser_entry_surface.py`、`test_browser_followups_production.py`、`test_browser_file_upload.py`、`test_browser_followup_ab.py`、`test_browser_leaf_exposure_ab.py`、`test_protocol_surface.py`。
- `affected-03`：三个原实验，加 `test_browser_followup_ab.py`、`test_browser_leaf_exposure_ab.py`、`test_protocol_surface.py`、`test_structured_surface.py`。
- `affected-04`：`test_browser_entry_followup.py`、`test_structured_surface.py`。之后仅完整重跑 structured 文件，并补充有实际影响的短回归。

原三个实验及相邻 follow-up、leaf、protocol 文件的既有用例已在 `affected-03` 通过，唯 entry follow-up 的一条诊断路径断言随后在 `affected-04` 通过；新增的六个字段选择用例在 `selector-03` 和最终短回归通过。structured 文件在独立的 `structured-final-01` 全部通过。生产 browser entry、follow-up、upload 三文件在 `related-final-01` 通过。这里没有把不同批次合并成一次“扩展批次全绿”，旧批次的失败记录仍有效。

最终静态检查如下，目标 Python 均为 3.11。生产目录检查与修改文件检查分开记录；没有声称检查了全部历史实验的类型。

| 检查命令与范围 | 结果 | 日志 |
| --- | --- | --- |
| `python -m ruff check .`，全树 | passed | `ruff-final-04.log` |
| `python -m mypy codey --python-version 3.11` | 692 个生产源文件无问题 | `mypy-production-final.log` |
| `pyrefly check codey --python-version 3.11` | 0 errors；35 suppressed、158 warnings 为现有检查状态，本轮未添加豁免 | `pyrefly-production-final.log` |
| `ty check codey --python-version 3.11` | passed | `ty-production-final.log` |
| 修改的 11 个 Python 文件：mypy 加 `--disallow-any-expr --disallow-any-explicit --disallow-untyped-defs --follow-imports=silent` | 11 个文件无问题 | `mypy-changed-final-03.log` |
| 修改的 11 个 Python 文件：Pyrefly | 0 errors，11 条既有 warnings；未关闭检查 | `pyrefly-changed-final-04.log` |
| 修改的 11 个 Python 文件：ty | passed | `ty-changed-final-03.log` |

修改文件的首次最终 Pyrefly 检查有 17 errors（`pyrefly-changed-final-01.log`），补齐空容器和 JSON 分支类型后为 0 errors；首次 ty 检查有 2 errors 和 8 条 contextmanager 类型弃用警告（`ty-changed-final-01.log`），修正 JSON 分支、动态 getter 返回边界和 Generator 注解后通过。首次最终 Ruff 检查还发现合并 helper 后遗留的一条未使用导入，删除后通过；所有失败日志独立保留。修改文件已检查没有 `Any`、`cast`、`type: ignore` 或检查器忽略指令，`git diff --check` 通过。

另外用 `--min-severity warn` 展开复查 Pyrefly warnings，首次列出 12 条多余 `str()` 转换并返回退出码 1（`pyrefly-changed-warning-review.log`）。去掉本轮新增的一条冗余转换后，复查仍以退出码 1 列出 11 条原有同类警告（`pyrefly-changed-warning-review-02.log`）；普通错误门槛检查为 0 errors。未通过关闭规则或调整门槛掩盖这些警告，也未扩大清理其余旧代码。

11 个 Python 文件是六个测试文件（原三个实验、leaf、protocol、structured）和五个工具文件（chat、follow-up、protocol、protocol AB、structured），与本报告的修改范围一致。最后短回归的完整筛选命令：

```powershell
python -m pytest -q tests/test_protocol_surface.py tests/test_browser_chat_followup_gate.py tests/test_browser_followup_ab.py tests/test_structured_surface.py -k 'configuration_installs or experiment_preflight or experiment_retains or context_reduction or frozen_permissions_descriptions or combination_checks_evidence or matrix_preserves or matrix_stops or source_digest_unchanged or model_purpose_is_stripped or explicit_payload_preserves or frozen_fill_roundtrip or variants_reuse_frozen or followups_do_not_bypass or matrix_audit_rejects or whole_plan_is_validated or same_mutation_id_reuses'
```

本次使用脚本化 provider 和真实 Chromium，不调用外部模型。pytest 通过仅表示测试的正负断言成立，不代表历史失败回复或真实模型质量已经变好。未运行全量 pytest；未覆盖外部模型质量、跨平台、安装包和多 Python 版本运行验证。
