# 浏览器失败后的下一步交接（2026-11-14）

本轮实现两个窄分支：编译失败后交付可执行观察，以及有当前文档 404 证据时交付任务地址选择器。恢复仍由模型提交；没有自动修写 JavaScript、自动纠正 URL 或新增执行器。

## 实现范围

第一项通过 `deliver_results()` → `request_snapshot()` 传入本轮父结果。只有失败码为 `syntax_error`、`child_effect_ids` 明确为空、最近导航仍对应已绑定任务页面，且没有需要保留的动作时，才提供快照下一步。已有当前、完整、同 generation 的成功快照直接复用。Chat 使用现有直接入口，CodeMode only 与 Hybrid 使用原一行包装。

动作判断取自正式原子回执和冻结 ToolSpec 的效果分类。点击已经送达时，复用已有 follow-up plan 提供尚缺的等待或读取；送达不确定时保留状态并观察。下载复用 `_download_handoff()`；成功或送达不确定的下载先交付原回执。写入还使用已有 `applied_edit` 执行事实，避免把工作区身份尚未确认的已应用写入误判成无动作。

第二项要求唯一任务地址绑定、模型请求其他地址的成功导航回执中 `http_status == 404`、导航属于当前文档、最终 URL 仍为当前 URL，以及导航之后当前完整快照的 `elements == []`。存在需保留的动作时不建议返回。父程序成功返回空快照同样可触发。

交付的调用是 `return unwrap(await tools.browser_navigate(inputs));`，`inputs` 为 `{"target":"task"}`。参数按当前冻结的 CodeMode catalog 校验，再沿用现有 Host 参数转换；底层导航仍接收 `url`，绑定没有另存一份。合法第二页、正确入口重定向和 `pushState` 均有反例。

未改变解析、授权、自由 JavaScript 校验、回放规则、轮次和 `no_progress` 限额。没有引入 Any、cast、type ignore 或检查豁免。

## 原回复与固定下一步回放

新夹具保存 Basic 和旧下载的完整原始回复及来源 SHA256，没有修改转义。错页使用原有历史夹具。

| 回放 | 检查结果 |
| --- | --- |
| 原 Basic 语法错误，普通 Chat | 父结果无子动作；下一轮实际提供并执行 `browser_snapshot({})`，取得 Locality；任务完成，点击一次 |
| 同一 Basic，CodeMode only | 执行所提供的一行包装，任务完成；没有直接调用浏览器叶工具 |
| 旧下载 `.elements` 解包错误 | 原快照仍有效，选择其真实下载 ref；总快照一次、下载一次；磁盘产物 SHA256 与回执一致 |
| 已送达点击之后出现原 Basic 错误 | 下一步只补 wait，再补 read；总点击一次、等待一次 |
| 原错拼 URL 回复 | 正确初始导航 → 模型导航到 404 → 提交提供的 task 选择器 → 取得正确页面控件并完成 |
| 父程序成功返回错页空快照 | 同样可提交 task 选择器完成恢复 |

对应测试在 `tests/test_browser_failure_handoff.py` 与 `tests/test_browser_navigation_handoff.py`。后者还覆盖非 404、未完整观察、旧 generation、不同文档、未提交回执、动作保留、权限拒绝、任务地址不唯一等情况。

## 自动回归与静态检查

未运行全量 pytest。相关的 17 个模块共 **272 passed / 8 failed**：

```powershell
python -m pytest -q --basetemp=.e2e-artifacts/bh-final `
  tests/test_browser_failure_handoff.py tests/test_browser_navigation_handoff.py `
  tests/test_browser_handoff_next_steps.py tests/test_browser_task_handoff.py `
  tests/test_browser_context_delivery.py tests/test_task_contract_fixed_replies.py `
  tests/test_task_navigation_binding.py tests/test_browser_full_surface_contract.py `
  tests/test_browser_observation_regressions.py tests/test_browser_snapshot_recovery.py `
  tests/test_browser_query_binding.py tests/test_browser_action_cold_recovery.py `
  tests/test_receipt_bound_edit_surface_probe.py tests/test_codemode_host.py `
  tests/test_codemode_kernel_integration.py tests/test_codemode_replay_authority.py `
  tests/test_codemode_repair_replay.py
```

8 项失败全部来自旧 `test_receipt_bound_edit_surface_probe.py`，已用改动前 HEAD `e25387e430f493a820e48faa01e58e5279b6d556` 的两个生产模块原样复现，结果为 **4 passed / 8 failed**。诊断只在独立测试进程的导入层加载 `git show HEAD:...` 内容，没有复制 worktree 或改动工作文件：

- 4 项仍提交旧自由 JavaScript，当前附件合同要求结构化 `upload` 参数，原始拒绝为 `codemode missing required arg 'upload'`，因此没有上传动作。
- 另 4 项旧实验适配器触发 `captured contract text differs from its frozen model definitions`，在 provider 边界失败。
- 这些基线失败本轮未修改。真实生产上传在下面两轮实机矩阵中均通过。

第一项聚焦回归曾为 82 passed，第二项为 106 passed；随后增加的两项“写入已应用但身份未确认”回归在上述 272 项通过中。第一提交 `9b5dc622` 的暂存生产源码还单独运行了 83 项聚焦回归，全部通过。

静态检查全部通过：

```powershell
python -m ruff check .
python -m mypy codey tools/browser_failure_handoff_live_gate.py `
  tests/test_browser_failure_handoff.py tests/test_browser_navigation_handoff.py `
  tests/test_browser_handoff_next_steps.py tests/test_task_contract_fixed_replies.py --python-version 3.11
python -m tools.kernel_type_precision_gate
git diff --check
git diff --cached --check
```

mypy 检查 692 个源文件；kernel Any 计数为零。

## Full-surface 实机矩阵

使用真实 Chromium 与 `http://127.0.0.1:5001/v1`，模型为 `koboldcpp/Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M`，seed `123456789`，单次生成 timeout 180 秒。

```powershell
python -u -m tools.browser_full_surface_live_gate --run-dir .e2e-artifacts/bh-matrix-first --scenario all --timeout 180
python -u -m tools.browser_full_surface_live_gate --run-dir .e2e-artifacts/bh-matrix-final --scenario all --timeout 180
```

| 场景 | 首轮 | 最终轮 |
| --- | --- | --- |
| Basic Chat | 通过 | 通过 |
| json_explanation | 通过 | 通过 |
| legacy structured | 通过 | 通过 |
| 快照恢复与重复读取 | 通过 | 通过 |
| stale-ref | 未满足对抗覆盖 | 未满足对抗覆盖 |
| Hybrid stale-edit | 通过，真实冲突 1 次并恢复 | 模型语法错误，no_progress |
| 上传 | 通过 | 通过 |
| 下载 | 通过 | 通过 |
| strict Research | 通过 | 通过 |
| strict Research sequence | 通过 | 通过 |
| 来源失败 | 正确 blocked | 正确 blocked |
| 固定来源失败 | 正确 blocked | 正确 blocked |
| 合计 | **11/12** | **10/12** |

两轮各自执行期间 source digest 不变。首轮后补充了 `applied_edit` 保留判断，最终轮对应最终生产源码 digest `2c0463965188cd466002281183ad9311fc5b1e0a1c39737709ded3a8c3e455d5`。

原始请求和回执显示：

- stale-ref 两轮均完成任务，但模型只用新 ref，未实际尝试旧 ref，`failed_stale_refs == 0`。因此门禁保持失败；不能用普通任务完成替代 stale-ref 路径覆盖。相关确定性旧引用回归已通过。
- 最终轮 Hybrid 首个程序成功返回有效快照，后续三个程序重复非法转义并编译失败，子动作均为空。交接已在原请求中复用快照和真实 ref；模型仍未执行 fill、click 或 edit。没有为迁就该输出放宽合同。

## 相同失败前缀，12B 自主续跑

`tools/browser_failure_handoff_live_gate.py` 只注入一条原失败回复，之后交给真实模型。初始前置导航、合同、执行与回执仍走生产链。它保存每轮 handoff、原始 wire request/response、来源哈希和续跑指标。

```powershell
python -u -m tools.browser_failure_handoff_live_gate --run-dir .e2e-artifacts/bh-prefix-first --case all --timeout 180
```

| 失败前缀 | 模型采用交接情况 | 最终结果 |
| --- | --- | --- |
| Basic 编译错误 | 下一轮原样提交直接 snapshot | 完成，实际点击一次 |
| 下载解包错误 | 看到了真实候选 ref，但重新 snapshot 后下载 | 完成，下载一次；未复用前缀快照 |
| 错拼 URL 的 404 | 三轮忽略 task 调用，只在错误页面重复 snapshot | no_progress，没有恢复导航或点击 |

合计 **2/3**。错页交接在三次实际 wire request 中均存在，模型程序则返回 `Locality ref not found`。这说明固定调用证明“恢复可执行”，但本样本尚未证明 12B 会采用该恢复。下载同样不能宣称自主复用了旧快照。这三项没有再挑选重跑结果。

## 首次失败留存与限制

所有本轮日志保留于 `.e2e-artifacts/browser-handoff-20261114/`：

- `pytest-first.log`：初始测试夹具/规范化断言问题及点击后缺可执行下一步的发现。
- `pytest-compile-regression.log`：78 passed / 4 failed。其中 3 项是 Windows 长测试路径读取产物失败；通过生产路径转换读取原产物，18 bytes 与 SHA256 均吻合，缩短 basetemp 后通过。另外 1 项 query cold-recovery 曾缺 wait/read，后续稳定源码聚焦回归及最终广回归通过，首次失败根因未证实，保留为未定瞬态。
- `unconfirmed-edit-before.log`：修复前两项已应用写入保留回归失败，随后修复并通过。
- `pytest-compile-rerun.log`、`pytest-navigation-first.log`、`pytest-final.log`：聚焦及最终广回归结果。
- `pytest-upload-baseline.log`、`upload_head_baseline.py`：旧上传实验的 HEAD 基线复现。
- `pytest-index-first.log`、`staged_compile_check.py`：独立加载第一提交暂存源码的聚焦验证。
- `ruff-final.log`、`mypy-complete.log`、`type-precision.log`：静态检查。
- `full-surface-first.log`、`full-surface-final.log`、`prefix-first.log`：两轮矩阵和前缀续跑日志。

完整请求、回执、产物和汇总分别保留于 `.e2e-artifacts/bh-matrix-first/`、`.e2e-artifacts/bh-matrix-final/`、`.e2e-artifacts/bh-prefix-first/`。本地 artifact 不加入提交。

本次补足了生产链可执行恢复入口及其确定性回放。它不保证模型采用建议，也未覆盖任意运行时错误的自动续接；错页策略仅覆盖上述有正式证据的 404。矩阵未全绿，首次失败与后续结果均保留。
