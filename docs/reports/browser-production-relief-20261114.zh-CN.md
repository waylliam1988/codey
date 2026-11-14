# 浏览器自动串联与描述引用生产接入

本轮在主分支原目录实施，分两次生产提交；不创建 worktree，不运行全量 pytest。

## 第一提交：正式交接中的自动串联提示

- 查询候选判断进入 `browser_task_handoff()`，服从未确认点击、等待、导航恢复、下载和补观察分支的优先级。
- 只列出任务点名或冻结页面轮次要求中的控件，保留真实名称和观察上下文；自然语言顺序使用可选提示，不把它改写成立即执行指令。
- 调用生成器按实际入口校验：外层 `model_specs`，嵌套 `snapshot_catalog()`。无等待要求时允许点击后正式读取；裸等待缺条件时不开放 result。
- 原采用实验复用生产提示；menu 仅屏蔽新增提示，两组执行合同相同。Chromium 测试从生产提示提取调用，核对点击、等待、正式读回和重复读取计数。
- 清理修改模块中原有隐式 Any，导航回执先解码为明确 JSON 类型。

验证记录位于 `.e2e-artifacts/browser-production-relief-20261114/checks/`：相关回归 155 passed；Ruff、mypy（修改模块额外禁止 Any 表达式）和类型精度门禁通过。完整实机矩阵在第二提交完成后执行，并在下文记录首轮和独立重跑。

历史配对收益不改写：采用 0/8 → 8/8，任务双方 6/8；共同成功配对请求 42 → 32，prompt tokens 380540 → 288216。该结果来自先前独立实验，不代表本轮新增生产矩阵的测量结果。

## 第二提交：冻结观察中的可读控件选择

保留原字符串 `ref`，填写和点击增加对象形式，例如：

```json
{"ref":{"role":"textbox","name":"备注","context":"订单 A-173"},"value":"下午配送"}
```

- 描述来自当前正式 snapshot 的 role、name 和可选上下文标签，精确匹配；先判唯一，再查禁用状态。同名上下文仍有多个目标时拒绝选择，返回真实 ref 等消歧信息。
- 请求冻结一个不可变绑定，外层工具和 CodeMode 从同一函数生成 ref schema，Host 统一解析为字符串，子动作继续使用原叶工具、原执行器、原回执。
- 允许正文截断，但控件、上下文、正式回执、文档和 generation 必须满足绑定条件。没有有效绑定时只提供原字符串形式。
- 动作前复用浏览器线程的节点观察，核对节点自身角色、完整名称和当前可用状态，补上同节点就地改名仍可能执行的确定性缺口；结构上下文和文档检查继续有效。
- 快照 v9 显式保存 followups 标记和控件绑定，v6/v7/v8 继续解析；运行时实现指纹仍独立决定旧请求能否继续执行。
- 恢复请求保留原绑定，已提交动作通过原子回执接续。`repair_of` 按原子回执中的真实 ref 找到原观察，再解析描述并核对原参数摘要；当前新观察不能把修复重绑到另一个控件。
- 模型描述留在 provider 响应及父调用参数中，实际字符串 ref 留在正式子回执中。

这项直接实施并用确定性回归验收，没有另设前置收益实验。定向证据 passage 的邻居混入及模型漏做故意 stale-ref 步骤，继续按原口径记录，不以本次引用绑定修改替代它们的验收。

## 确定性验证与源码锁定

- 第一组相关生产回归：`447 passed, 2 xfailed`，481.74 秒。两项 xfail 是原有 passage 范围混入，没有放宽或改成通过。
- 审阅发现失效绑定的嵌套声明可能残留对象形式。先运行失败回归，记录于 `checks/expired-declarations-reproduction.log`，再使声明始终来自当前冻结 catalog。
- 修复后的快照、合同及相关入口回归：`156 passed`，280.22 秒，覆盖原引用有效性、描述引用、上传、网页复制及 Research 详情。与前组存在重叠，不相加作独立样本。
- Ruff 全树、mypy 695 个文件、修改模块额外禁止 Any 表达式/显式 Any，以及 kernel 类型精度门禁通过。未增加 cast、type ignore 或检查豁免。
- 跨进程中断恢复同时覆盖字符串和对象引用，包含点击已提交与送达未提交两种情况；后者保留不确定性，不通过重复点击恢复。`repair_of` 另验证节点改名、新同名节点及换页后仅重放原回执。

第一次实机采样保留在 `full-surface-first/`。发现 `production_source_digest()` 使用 `git ls-files`，而新模块尚未暂存，导致指纹没有覆盖新模块，因此主动中止并写入 `aborted.json`，标记 `valid=false`。当时 basic、structured、legacy_structured、reread 已完成，stale_ref 尚在执行；这些不计入完整矩阵。basic 的原始响应中还保留三次编译失败：JSON 中的 `\r` 使预期 `return` 变成新行的 `eturn`；回执确认均未执行子动作。

随后先将新模块纳入 Git 追踪，保存 `source-manifest.json`（完整生产指纹及新模块 SHA256），再重新运行完整矩阵，目录为 `full-surface-tracked-first/`。这一步只改变追踪状态，没有修改生产源码内容。

## 完整实机：10/12，独立重跑 1/2

命令：`python -u -m tools.browser_full_surface_live_gate --run-dir .e2e-artifacts/browser-production-relief-20261114/full-surface-tracked-first --scenario all --timeout 180`。

使用本地 KoboldCpp Gemma4 12B Q4、temperature 0、seed 123456789 和真实 Chromium。前后完整源码指纹均为 `711866a3520bf56bfac31d652f6e482c0075d3efcc1e2e762801ac7a9ffab732`，与单独保存的源码清单一致。

| 场景 | 完整轮 | 核对结果 |
| --- | --- | --- |
| basic Chat | 失败 | 五个模型响应已完成观察、对象引用填写/点击、等待、正式读取；第六次生成答案时上游把 HTTP 头写进 JSON 响应体，生产以 GenerationUnknownError 停止，未暗中重发。环境问题。 |
| structured | 通过 | 正式正文及元数据按生产合同交付。 |
| legacy_structured | 通过 | 实际使用对象引用填写和点击，实际点击一次。 |
| reread + restore | 通过 | 8 次快照恢复，正式读取两次、实际点击一次；填写和点击使用对象引用。 |
| stale_ref | 失败 | 模型循环找到 Locality 后立即 break，按钮变量仍为空；四次程序均在 browser_click 参数校验处停止。真实 stale_ref 拒绝回执为 0。模型输出问题。 |
| Hybrid stale_edit | 通过 | 一次真实文件版本冲突，重新读取后成功写入实测字段。 |
| upload | 通过 | 正式上传、等待、正文回执。 |
| download | 通过 | 原下载链路生成真实托管产物。 |
| strict Research | 通过 | 正式读取并提交引用。 |
| Research sequence | 通过 | 等待、正式读取及引用顺序满足原合同。 |
| source_failure | 通过 | 夹具预设 403，按实际失败阻断。 |
| fixed_failure | 通过 | 固定采集的预设来源失败被保留并阻断。 |

逐项复核原始 `provider.jsonl`、正式子回执和场景摘要，汇总于 `live-audit.json`。完整轮至少六次外层对象引用调用成功解析，正式子回执仍保存字符串 ref。所有送达查询均只有一次实际点击。basic 的 token usage 不完整，因此不汇总整个矩阵的 token 成本。

独立重跑保存在 `independent-reruns/`，沿用完全相同的生产源码及原场景配置：basic 通过；stale_ref 返回了正确正文，但模型跳过故意使用旧引用的步骤，`failed_stale_refs=0`，仍判失败。完整轮保持 **10/12**，不合并重跑包装成 11/12 或 12/12。未发现对应链路的生产确定性故障，未为模型错误放宽合同。

## 明确等待条件下的生产提示冒烟

在全部实现及回归完成后，用迁移后的生产提示脚本补跑一对 basic/original，目录 `result-smoke/`。两组同版本、同任务、同执行合同，menu 只屏蔽新增查询提示；没有再为描述引用设前置收益实验。

| 指标 | menu | 生产提示 |
| --- | ---: | ---: |
| 任务通过 | 1/1 | 1/1 |
| 实际 result 调用 | 0 | 1 |
| 模型请求 | 6 | 4 |
| prompt tokens | 51235 | 33156 |
| completion tokens | 153 | 120 |
| 单独等待请求 | 1 | 0 |
| 单独读取请求 | 1 | 0 |
| 实际点击 | 1 | 1 |
| 不确定点击 | 0 | 0 |

原始模型请求、Host 调用 trace 和原子回执均通过脚本交叉核对；源码及 harness 指纹稳定，usage 完整。这一对只确认生产提示确实被采用并减少了步骤，不替代先前八对数据，也不用于推断普遍通过率。

剩余限制：自主 stale_ref 负例仍会被模型漏做；两项已有 passage 范围失败仍保留。描述引用已经在实机采用，正确性由确定性回归验收，其单项净 token 收益没有另行估计。
