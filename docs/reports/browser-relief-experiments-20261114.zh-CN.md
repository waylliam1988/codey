# 浏览器减负机制：采用、证据范围与原文绑定实验

基线为 `1cae7197`，直接在 `master` 工作，没有创建或复制 worktree。本轮只修改实验脚本、测试和报告，`git diff -- codey` 为空。开发期间曾尝试修改生产交接辅助函数，收到“不要改生产代码”的纠正后已完整撤回；以下验证均使用未修改的生产树。

## 实验边界与脚本校验

- `tools/browser_result_adoption_ab.py`：四种原场景 basic、reread、legacy_structured、stale_ref，各配原文和小幅同义改写；menu / call_hint 配对，交替运行顺序。两组都明确声明相同的真实等待条件，只在候选组当前请求的交接文本中追加完整调用。没有固定模型首步、改写模型输出或扩大工具菜单、权限及执行能力。
- 提示要求当前完整快照、相同文档和 generation、冻结计划中实际可执行的等待与正式读取；尚有必需填写、已送达或未知送达的查询时不提供首次查询提示。有多个按钮或链接时列出现有名称、上下文和引用，由模型选择。
- 当前生产 `_call_handoff()` 用原叶工具 schema 校验点击参数，会拒绝 `purpose`。实验因此使用**进程内呈现适配器**，按既有公开组合合同校验，再交给原 `normalize_turn()` 和执行链。没有修复该生产函数，不能将结果解释为该辅助函数已支持此路径。
- 实际采用由原始子回执和被动 host trace 联合确认，自动等待、读取还须逐项匹配捕获的冻结计划。程序中额外手写的读取仍算手动。外层回执的 `ToolCall.call_id` 可以为空，各份独立外层回执分别计数；同一 CodeMode 请求内的多次手写调用按原父调用分组。
- 单独报告提示有机会出现的请求数、真正展示的请求数及覆盖运行数；没有展示机会的运行不能用于判断提示效果。成本另按双方共同成功的配对统计，失败不通过省略成本造成假收益。
- 每次 HTTP 请求前和每个实验单元后核对生产及七个相关脚本的摘要；审计真实请求、响应、重试次数、usage 和采样配置。配对任务和初始冻结菜单、策略、权限必须一致。

新脚本检查包括有效与无效调用、直接与嵌套合同、候选重名、过期和截断快照、授权拒绝、未知送达、额外手写读取、空调用 ID、错误 trace、隐含重试、脚本替代模型、共同成功成本、历史原文暴露与值错误的独立归因。固定 HTTP 响应配真实 Chromium 另外验证了两组的生产执行、独立回执及计数。

同时修正了旧实验审计器的一处范围判断错误：`tools/browser_leaf_exposure_ab.py` 原来要求 passage 覆盖整个来源，误拒绝合法的目标切片；现在从原来源版本恢复真正交付的 passage，再核对事实和引用，继续拒绝截断来源、截断回执及篡改范围。这是测试脚本修正。

脚本最终检查为 **84 通过、2 个 strict xfail**。两个 xfail 对应原有范围混入，分别使用 inline / external_panel 原夹具，实际断言交付 passage 不应包含邻居；当前生产行为确定失败，未来意外通过也会使 strict xfail 报错，需明确更新回归。

早期失败全部保留在 `.e2e-artifacts/browser-relief-20261114/checks/`：包括调用合同、冻结 mapping、错误使用全文覆盖条件、测试夹具缺字段、查询绑定仅首次消费，以及外层空 ID 导致的请求合并。首次实机抽查发现空 ID 问题后，已中止 `adoption-live-first/`，保留原始记录与原摘要副本，并将摘要标为 `valid=false`；该轮不纳入最终结果。补充了真实 Chromium 中两次空 ID 外层读取与一次自动读取的回归后，才完整重新运行。

## 自动串联实机配对

最终矩阵目录：`.e2e-artifacts/browser-relief-20261114/adoption-live-final/`。

运行命令：

```powershell
python -u -m tools.browser_result_adoption_ab --run-dir .e2e-artifacts/browser-relief-20261114/adoption-live-final --timeout 180
```

本地模型为 `koboldcpp/Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M`，seed `123456789`、temperature 0、非流式输出，使用真实 Chromium。

生产树摘要：`2c0463965188cd466002281183ad9311fc5b1e0a1c39737709ded3a8c3e455d5`。

七个实验脚本摘要：`364ba54ff7e5cbfe8b31e29fdf4510a603436aa3f522ae3c84f62c898e00d463`。

最终 **16/16 运行完成且审计有效**；配对任务、初始合同、源码及脚本摘要检查通过。额外按原 durable `turn` 独立重算全部 16 项的手动请求数，与脚本统计一致，记录在 `independent-durable-count-check-final.json`。

| 指标 | 原菜单 | 完整调用提示 |
|---|---:|---:|
| 运行数 / 任务通过 | 8 / 6 | 8 / 6 |
| 有呈现机会的运行 / 请求 | 8 / 8 | 8 / 8 |
| 真正展示提示的运行 / 请求 | 0 / 0 | 8 / 8 |
| 实际 result 调用 / 送达 | 0 / 0 | 8 / 8 |
| 单独等待请求 | 8 | 0 |
| 单独读取请求 | 10 | 2 |
| 有效点击 / 未知送达 | 8 / 0 | 8 / 0 |
| 所有运行模型调用 | 54 | 40 |
| 所有运行 prompt tokens | 485,106 | 355,561 |
| 所有运行 completion tokens | 1,781 | 1,609 |

两个剩余的单独读取都是 reread 任务明确要求的查询前预读。候选 8 次 result 均产生查询基线和正式查询读回；点击数没有增加。

| 任务措辞 | 场景 | 原菜单调用数 | 提示组调用数 | 双方任务结果 |
|---|---|---:|---:|---|
| 原文 | basic | 6 | 4 | 均通过 |
| 原文 | reread | 7 | 7 | 均通过 |
| 原文 | legacy_structured | 8 | 6 | 均通过 |
| 原文 | stale_ref | 6 | 4 | 均失败 |
| 改写 | basic | 6 | 4 | 均通过 |
| 改写 | reread | 7 | 5 | 均通过 |
| 改写 | legacy_structured | 8 | 6 | 均通过 |
| 改写 | stale_ref | 6 | 4 | 均失败 |

双方共同成功 **6 个配对**，没有新增成功或损失成功。只在这 6 对中比较：模型调用 **42 → 32，减少 23.8%**；prompt tokens **380,540 → 288,216**；completion tokens **1,349 → 1,301**。

原文 reread 候选第一次填写时把 ref 抄漏了字符，被拒绝后重新观察、填写，多用两次请求，抵消了查询阶段省下的两次请求。四条 stale_ref 轨迹全部漏做任务要求的旧按钮引用拒绝验证，拒绝回执均为 0；即使最终事实正确、候选采用了 result，仍保留为任务失败。没有将这个场景的少步骤计入共同成功成本。

这批数据确认：在这些明确等待条件、可展示调用的夹具中，呈现改变能使模型选中已有自动串联并减少补步骤请求。样本只有一个模型、一个 seed、两个相近措辞和四个受控场景，不能据此估计通用网站上的稳定收益；采用增加也没有解决额外语义步骤遗漏或其他参数抄写错误。

## 定向证据范围

`tools/browser_query_scope_probe.py --seeded-only` 固定在原两个相邻记录布局中选择 A-173 并触发既有 `purpose=result`。固定采集明确标记为 scripted acquisition，不计作模型采用或真实模型请求；后续作答使用本地模型。

审计分别恢复原点击目标、实际目标节点文本、正式来源版本、交付 passage 和答案真正引用的 passage。`passed` 保留原任务事实与来源引用成功口径；`scope_passed` 另外要求交付无邻居且真正引用了干净目标 passage。仅有 `[s1]` 不能证明精确归属。目标文本校验也不会将元数据标签里的目标编号误认为节点正文。

`tests/test_browser_scope_channels.py` 还单独验证初次窗口、`fitting_full_read()`、续读、原来源版本恢复及篡改范围拒绝。这些测试确认当前通道会交付哪些原文，不实现尚不存在的目标范围约束。

命令与目录：

```powershell
python -u -m tools.browser_query_scope_probe --run-dir .e2e-artifacts/browser-relief-20261114/scope-live-final --seeded-only --timeout 180
```

生产及脚本摘要与采用矩阵一致，前后检查通过。两次固定采集和 **2 次真正模型请求**分别记账，每布局一次真实请求、无 HTTP 重试、usage 完整。

| 布局 | 目标节点正确 | 正式来源含邻居 | 交付 passage 含邻居 | 真正引用精确 passage | 答案含邻居字段 | 范围验收 |
|---|---|---|---|---|---|---|
| inline | 是 | 是 | 是 | 否，仅 s1 | 否 | 失败 |
| external_panel | 是 | 是 | 是 | 否，仅 s1 | 否 | 失败 |

两个点击均绑定 `aria-controls` 对应的实际 backend node，目标节点文本包含 18:20 和 TARGET_173；正式来源版本分别为 `27fc93b18db79743`、`d35bb36f75a0135e`。从这些原版本恢复的交付 passage 都包含 OTHER_174。实际答案只引用来源 `[s1]`，`actually_cited_target_passages` 均为空，所以不存在可声称已确认精确归属的 passage 引用。

事实与来源引用口径 **2/2 通过**，独立范围验收 **0/2**。这复现了范围混入，仍未观察到答案字段混用，也没有用来源级引用推断精确归属。本轮保留已知失败，没有实现生产范围约束。

## 历史原文绑定审计

命令：

```powershell
python -m tools.browser_fill_history_audit --roots .e2e-artifacts/browser-chat-bound-text-20261113 .e2e-artifacts/browser-chat-bound-text-following-block-20261113 .e2e-artifacts/browser-chat-followup-final .e2e-artifacts/browser-leaf-exposure-final-20261113 .e2e-artifacts/browser-followups-production-20261114-full-first .e2e-artifacts/browser-followups-production-20261114-full-final --output .e2e-artifacts/browser-relief-20261114/fill-history.json
```

只读审计 **102 个目录、68 个实际填写回合**，无无法读取或无法归因的目录。按原 provider effect ID、冻结请求、原响应、durable turn 和成功填写回执归因；失败的 JavaScript 或未成功执行的填写不算抄写错误。没有使用目录名推断原文绑定是否暴露，也没有把当前解析器的能力追认成历史请求的实际能力。

| 历史入口 | 填写回合 | 成功填写 | 当前解析器可明确绑定 | 原请求实际冻结原文 | 可比原文的成功填写错误 |
|---|---:|---:|---:|---:|---:|
| 直接 browser_fill | 40 | 38 | 16 | 4 | 11 |
| 嵌套 CodeMode | 28 | 28 | 0 | 0 | 0（无可比样本） |

11 个实际原文错误全部走直接入口，且原请求没有冻结原文。4 个实际冻结原文的回合都使用直接 `source=task_text`，值与冻结原文逐字一致。目录名含 `bound_text` 的一个改写样本实际上没有绑定，填写内容只剩 `"明天到"`；后来的 following-block 实验才在该改写请求中冻结完整两行并正确填写。

本批没有“原文明确可绑定，走嵌套入口后抄错”的可比样本。尤其嵌套 28 项的分母是**可比样本 0**，不能声称嵌套入口已被证明无损。现有证据支持继续保留入口与绑定暴露统计，尚不足以据此扩大通用 CodeMode 的原文绑定实现。

审计结果和逐条 provider effect ID、task SHA256、原期望值及实际值保存在 `fill-history.json`，不包含新模型调用。

## 相关回归与静态检查

实机完成后，最终选定的 **178 项生产回归全部通过**，用时 308.85 秒；覆盖点击计划、生产自动串联、下一轮交接、任务交接、查询绑定、正文交付、观测回归、失败与导航恢复。与上述脚本检查 **84 通过、2 个已知范围 xfail** 分别记录，不与早期重复检查相加。

```powershell
python -m pytest -q tests/test_browser_click_plan.py tests/test_browser_followups_production.py tests/test_browser_handoff_next_steps.py tests/test_browser_task_handoff.py tests/test_browser_query_binding.py tests/test_browser_context_delivery.py tests/test_browser_observation_regressions.py tests/test_browser_failure_handoff.py tests/test_browser_navigation_handoff.py --basetemp=.e2e-artifacts/br-prod-final
```

最终日志：`checks/production-regressions-final.log`；脚本日志：`checks/scripts-final-meter-pass.log`。两个目录均相对于 `.e2e-artifacts/browser-relief-20261114/`。

较早扩展检查为 **288 通过、7 失败**。7 项全在旧 `tests/test_browser_followup_ab.py`：快照计数仍假定旧次数、旧呈现文字及已被生产自动串联改变的步骤，部分固定响应被耗尽后报 `StopIteration`。将 HEAD 中原版 `tools/browser_leaf_exposure_ab.py` 只在独立进程内加载、不修改任何文件，再对这些场景复验，得到**相同 7 项失败、2 通过**。这些是已有旧实验兼容问题，本轮未扩大修复，也没有隐去为全通过。

实机后的生产及本轮脚本/测试 mypy 检查覆盖 **695 个文件**，通过；八个修改模块额外启用 `disallow_any_expr` 和 `disallow_any_explicit` 的检查也通过，未增加 cast、type ignore 或检查豁免。Ruff 全树、kernel 类型精度门禁和 `git diff --check` 通过。

```powershell
python -m ruff check codey tests tools
python -m mypy codey tools/browser_result_adoption_ab.py tools/browser_fill_history_audit.py tools/browser_query_scope_probe.py tools/browser_leaf_exposure_ab.py tests/test_browser_result_adoption_ab.py tests/test_browser_fill_history_audit.py tests/test_browser_scope_channels.py tests/test_browser_scope_probe_audit.py --explicit-package-bases --python-version 3.11
python -m tools.kernel_type_precision_gate
git diff --check
```

对应 `checks/ruff-post-live.log`、`checks/mypy-post-live.log`、`checks/precision-final.log`。额外严格配置和通过日志为 `checks/strict-mypy.ini`、`checks/mypy-frozen-harness.log`。

所有检查只运行选定相关 pytest 模块，没有运行全量 pytest。原始模型请求、响应、快照、passage、durable 回执和失败日志保留在 `.e2e-artifacts/browser-relief-20261114/`；报告中的计数是派生索引，原始记录用于复核。
