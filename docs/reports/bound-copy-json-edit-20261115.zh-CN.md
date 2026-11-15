# 已绑定复制参数与 JSON 编辑反馈

基线 `1cbaab10`。直接修改当前工作区，没有创建或复制 worktree，没有运行全量 pytest。

## 生产改动

- 浏览器复制的编辑阶段只接受 `source`。文件和目标字段从冻结的 `CopyTarget` 取得，在描述中呈现供核对；已有 `business_key` 的表格不再要求模型回填行键，由 `lower()` 补入后继续检查唯一匹配。未绑定行键、来源列、键列、JSON Pointer 和多来源 ID 仍需要选择。没有扩大任务识别规则。
- 浏览器复制保留原 `EDIT` 和 `browser_source`，继续校验来源回执、父子动作归属、当前文档、来源数据、授权目标、类型、完整文件读取及版本。没有改成通用 JSON 编辑程序。
- 通用两行编辑程序提取为 `JSON_FIELD_EDIT_PROGRAM`，提示和固定执行测试共用；路径和值经 `inputs` 传递。普通 Hybrid 仍由模型调用模板，提取常量不保证其自主程序不会有语法错误。
- 原字段检查在拒写时给出缺失字段、已有顶层字段和提交字段，每组最多列出 12 个字段名。不存在的字段包裹了真实顶层字段时，提示具体 `changes["字段名"]` 可能多套了一层；Host 从完整读取绑定补上文件路径。只反馈、不拆壳、不写入；真实存在的 `value` 字段仍合法。
- 旧编辑阶段冻结合同不做迁移：沿用严格定义比较，定义不匹配时明确要求重新发起请求。当前合同序列化后可继续恢复，没有增加第二套兼容合同。
- 完成交付生产逻辑保持原实现，只补贯穿回归：读取和写入已成功，说明缺少一项事实时，仅反馈该项和正式回执引用；修复说明后完成，浏览器点击、浏览器正文读取、文件读取及编辑各执行一次。

## 回归与类型检查

首轮针对性回归 55 项通过。最终 19 个相关测试模块共 **226 项通过**，包含前面的 55 项，不重复累加。覆盖换文件名/字段名/行键、未绑定行键、非法旧参数、重复行键拒绝、原始完整读取、真实版本冲突、保留外部字段及数值精度、成功子动作恢复、写入状态未知、旧冻结合同拒绝、当前合同恢复、说明缺项修复、回执不可用及 strict Research 边界。

最终命令：

```powershell
python -u -m pytest tests/test_codemode_json_edit.py tests/test_model_input_binding.py tests/test_browser_copy_production.py tests/test_explanation_delivery_production.py tests/test_simplified_receipt_recovery.py tests/test_simplified_recovery_boundaries.py tests/test_codemode_host.py tests/test_codemode_unwrap_contract.py tests/test_codemode_writes_and_state.py tests/test_codemode_repair_replay.py tests/test_codemode_replay_authority.py tests/test_codemode_child_failure_feedback.py tests/test_codemode_production_boundaries.py tests/test_browser_full_surface_contract.py tests/test_browser_snapshot_recovery.py tests/test_browser_completion_production.py tests/test_completion_output.py tests/test_explanation_delivery_contract.py tests/test_completion_source_vs_strict_research.py -q
python -m ruff check .
python -m mypy codey tests/test_browser_copy_production.py tests/test_codemode_json_edit.py tests/test_model_input_binding.py tests/test_explanation_delivery_production.py --explicit-package-bases --follow-imports=silent --config-file .e2e-artifacts/bound-edit-20261115/mypy-strict.toml
python -m tools.kernel_type_precision_gate
```

Ruff 全树通过。mypy **692 文件通过**；配置沿用项目设置，并为本轮修改的全部 4 个生产模块及 4 个测试模块增加 `disallow_any_expr`、`disallow_any_explicit`。类型精度门通过。本轮文件没有 `Any`、`cast` 或 `type: ignore`。

首次 Ruff 发现新增测试导入顺序问题；首次 mypy 发现 4 处序列化上下文产生的隐式动态类型，包括修改文件中的既有 `model_selection`。均改为明确类型后通过，首次日志仍保留。两轮 pytest 退出后分别出现 Windows 缓存目录占用、RAM 盘临时目录不存在的清理警告；两轮测试退出码均为 0，断言均通过，没有据此改动无关生产代码。

日志与类型配置：`.e2e-artifacts/bound-edit-20261115/` 下的 `regression-first.log`、`regression-final.log`、`ruff-first.log`、`ruff-final.log`、`mypy-first.log`、`mypy-final.log`、`mypy-strict.toml`。

## 完整 full-surface 首轮

```powershell
python -u -m tools.browser_full_surface_live_gate --run-dir .e2e-artifacts/bound-edit-20261115/full-surface-first --scenario all --timeout 180
```

本地 Gemma4 12B Q4_K_M，seed `123456789`，temperature 0，真实 Chromium、生产入口/kernel/CodeMode/持久回执。首轮 **11/12**，退出码 1。生产摘要前后一致：`f099573c64046eda1efca8420b2dca12e78512fe2acfcd7ac828187ad65b1e8a`。

| 场景 | 请求数 | 语法错误 | 字段形状拒绝 | 真实版本冲突 | 成功编辑 | 结果 |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| basic | 2 | 0 | 0 | 0 | 0 | 通过 |
| structured | 2 | 0 | 0 | 0 | 0 | 门禁通过，说明质量问题见下 |
| legacy_structured | 8 | 0 | 0 | 0 | 0 | 通过 |
| reread | 7 | 0 | 0 | 0 | 0 | 通过，8 次冻结快照恢复 |
| stale_ref | 6 | 0 | 0 | 0 | 0 | 失败，模型跳过旧引用验收 |
| stale_edit | 4 | 0 | 0 | 1 | 1 | 通过，重读恢复 |
| upload | 4 | 0 | 0 | 0 | 0 | 通过，实际选择附件一次 |
| download | 3 | 0 | 0 | 0 | 0 | 通过，实际下载一次 |
| research | 2 | 0 | 0 | 0 | 0 | strict Research 通过 |
| research_sequence | 3 | 0 | 0 | 0 | 0 | strict Research 顺序合同通过 |
| source_failure | 1 | 0 | 0 | 0 | 0 | 按预期 blocked |
| fixed_failure | 1 | 0 | 0 | 0 | 0 | 按预期 blocked |

每项只观察到一次 `task_done` 事件；没有回执回放，成功编辑、上传、下载均各一次。这只说明本轮观察到的执行和交付次数，崩溃后的重复送达边界另由回归覆盖。完整计数在 `live-metrics.json`。

stale-ref 已检查 `task.txt`、原始 `provider.jsonl` 和持久回执：用户要求保留填写前的按钮引用并实际尝试一次；模型原始回复在填写后直接选用了新引用 `...g4e2`，实际点击成功，没有任何 `stale_ref` 拒绝，计数为 0。属于模型漏步骤，未发现对应生产确定性问题；没有放宽合同或把它计为通过。

structured 的正式输出字段由原绑定正确组装，但模型说明把分段写成字面反斜杠换行，并重复了 URL/token。现有门禁记录 `requested_shape_valid=false`、`bound_values_repeated=true`，没有把这两项纳入 `passed`；因此只报告现有门禁通过，保留这项模型输出质量限制。本轮新的说明修复回归使用明确声明的事实与段落规则，没有更改生产合同来迁就该输出。

首轮原始请求、模型回复、事件和 state 均保留在 `full-surface-first/`，首个 stale-ref 失败另有 `stale-ref-first-provider-audit.log`。

## stale-ref 单项重跑

```powershell
python -X utf8 -u -m tools.browser_full_surface_live_gate --run-dir .e2e-artifacts/bound-edit-20261115/stale-ref-rerun --scenario stale_ref --timeout 180
```

同一生产摘要、同一模型和 seed，重跑 **1/1 通过**，8 次请求，实际 `stale_ref` 拒绝一次，随后获取新引用并完成。重跑不是全矩阵通过的替代，首轮仍为 **11/12**。

重跑中还保留了两次模型程序错误：遍历控件时找到输入框就 `break`，按钮引用因此仍为空，原合同以 `browser_click missing required arg 'ref'` 拒绝。之后模型改用已观察的旧引用，取得正式 stale-ref 拒绝回执，再继续正常操作。这些错误没有被删除或计成预期 stale-ref；没有修改生产合同来接受空引用。证据在 `stale-ref-rerun/` 及 `stale-ref-rerun-audit.log`。

## 补充实机：冲突恢复保留外部新增字段

直接调用同一生产实机入口，启用已有选项：

```python
from pathlib import Path
from tools.browser_production_live_gate import run_case, select_target

run_case(
    Path(".e2e-artifacts/bound-edit-20261115/preserve-external-live"),
    "hybrid", select_target(180, "http://127.0.0.1:5001/v1"),
    stale_edit=True, preserve_external_field=True, restore_snapshots=True,
)
```

**通过**：4 次模型请求、5 次冻结快照恢复；完整文件读取两次，编辑回执依次为拒写、成功，两个编辑引用分别对应两次原始读取。真实冲突一次、成功写入一次，最终只将 `amount`、`code` 更新为网页确认值，外部新增的 `external_revision={"revision":1,"marker":"EXTERNAL_CHANGE_MUST_SURVIVE"}` 完整保留。生产摘要前后均与首轮一致。

证据：`preserve-external-live/` 的原始请求、回执及 `verification.json`，以及 `preserve-external-digest.json`。这项补充与 stale-ref 重跑分别记录，不将它们合并成新的全矩阵通过率。

## 范围限制

这次泛化的是复用冻结绑定，未证明任意网页和文件间的字段映射能力，也未进行模型收益配对实验。现有业务键识别格式仍有限；普通 Hybrid 的映射和 JavaScript 仍可能由模型写错。没有发现需要扩大本轮范围处理的生产确定性问题。
