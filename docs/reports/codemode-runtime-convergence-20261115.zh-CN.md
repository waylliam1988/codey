# Code Mode 运行时收敛与验证（2026-11-15）

## 实现边界

保留 QuickJS/WASM 与唯一的 Node worker 驱动。AST 预检只提供策略诊断，隔离仍由 QuickJS、冻结的 intrinsics、有限 RPC 与 kernel 授权承担；没有把 node:vm 作为安全边界。

- `protocol.analyze_codemode_source()` 用 tree-sitter 检查实际语法节点，返回带位置的结构化诊断，按字符而非 UTF-8 字节计算列号，并识别 JavaScript 行分隔符。字符串、注释、模板文本和数据属性不再因关键词被拒绝。Python 入口统一执行预检，guest 不再维护第二份源码规则。
- Future 的完成队列决定实时交付顺序。原 durable checkpoint 增加 `delivery` 引用，记录 RPC ID 与可空的子调用位置，写入后才向 guest 交付。state 和目录响应也进入同一逻辑顺序。工具结果仍只属于原始 effect receipt，没有第二份结果日志。
- 冻结目录提供 summary/search/describe。模型声明默认上限为 24,000 UTF-8 字节，保留 namespace 数量与发现接口，优先选择 read_file/grep/browser_snapshot/edit 的完整声明。发现结果包含精确 `tools.NAME` 路径与参数说明；搜索不会授权工具。
- 浏览器组合动作与 JSON 字段编辑移到 CodeModeHost。组合动作在点击前预留子调用额度；每个实际工具动作继续进入 execute_child_turn，独立结算。JSON 编辑使用显式 `base_result_ref`，host 从原始完整读取 receipt 推导路径与文本。guest 不再保存 read 对象到产品语义的 WeakMap。
- CodeModeSession 提供 start/wait/terminate/recover。同步 execute 也走这个 session 和原来的 worker driver；wait 只是观察窗口。取消先关闭新调用，等待已接收调用结算，再发布终止结果。execution deadline、单工具 deadline 与 wait budget 分开。

生命周期目前是内部接口，模型可见 codemode 协议仍为一次提交、一个最终结果。进程重启的能力与 receipt 重建继续属于既有 kernel 恢复路径；durable request 使用原 kernel execution ID，session 的 restore 回调只接收该所有者重建的 request 或终态，不存储另一份能力或结果。未配置所有者恢复时，未知 handle 明确抛出 KeyError。没有 QuickJS heap snapshot，也没有旧恢复格式兼容层。

## 失败记录与诊断

本轮没有调用外部模型。真实 Chromium 场景使用脚本化 provider 或保存的原始回复；保存回复中任务失败的断言仍然是失败，不能把 pytest 通过等同于模型任务通过。

开发中的首次失败与重跑分别记录，未用后续通过覆盖：

| 批次 | 首次结果 | 诊断与后续结果 |
| --- | --- | --- |
| 前段 runtime/source 回归 | collection error；随后 110 passed / 1 failed | IO 类型导入错误已修复；旧 race 断言要求慢结果，改为完成顺序合同。 |
| 前段恢复回归 | 3 failed / 29 passed | 检查原始 sqlite receipt：子调用已结算，state 恢复被工具交付次序阻塞。最终改为记录所有 RPC 的交付顺序，未增加第二份状态日志。 |
| 前段扩大相关回归 | 32 failed / 298 passed | 2 个孤立 surrogate 的错误类别是生产编码问题；其余为旧 JSON edit 测试接口、实验脚本旧 execute 签名及实验空 schema。生产 Unicode 编码、测试与工具入口已修正。 |
| focused-01 | collection error，0 tests | 命令误写不存在的 test_codemode_wasm_isolation.py；保留日志，修正为实际 isolation 文件。 |
| focused-02 | 1 failed / 68 passed | 测试对冻结 checkpoint 使用严格 wire decoder，tuple 被拒绝；改用已有 canonical decoder。 |
| recovery-01 | 3 failed / 24 passed | 测试绕开正式 kernel 恢复导致 duplicate effect ID；另一个断言误认 deadline 错误文案。改从正式 generation boundary 恢复，保持生产效果合同。 |
| recovery-02 | 1 failed / 8 passed | 故障注入时慢读取尚未结算，原始 receipt 为 ERROR: task stopped。增加结算屏障，使测试准确覆盖效果已结算、尚未交付的窗口。 |
| production-01 扩大浏览器范围 | 23 failed / 366 passed | 本轮 host 异常包装将参数拒绝变成普通值是确定性回归，已修复；其余原始请求显示结构化上传、done.paragraphs、顶层 browser 工具与实验 request_snapshot 签名不一致，未放宽生产合同。原始请求、结果与回执另存 production-01-original-evidence.json。 |
| production-02 受影响场景复查 | 4 failed / 68 passed | 参数拒绝语义、受控 JSON 编辑与受控 paragraphs 输入已通过。剩下 4 项位于未修改的 test_browser_entry_followup.py：两个 summary/paragraphs 失配、一个结构化上传入口输入失配，以及一个“必须只有一条父 receipt”的旧假设。未修改生产合同来消除它们。 |
| recovery-03 | 9 passed | 同时覆盖日志前/日志后崩溃、完成顺序反转、禁止底层 read 重执行、取消等待结算、CPU 中断与独立工具 deadline。 |

可用的逐次日志位于 `.e2e-artifacts/codemode-20261115/`，原始 recovery-02 账本导出为 `recovery-02-original-ledger.json`。前段交接记录中的部分 RAM 临时目录已由测试环境清理，以上保留已记录的结果，不声称这些早期临时目录仍然存在。后续批次结果另列，不覆盖上述记录。`focused-*.log`、`recovery-*.log` 与 `production-*.log` 各自保留首次运行和后续运行。

## 最终验证

| 检查 | 结果 |
| --- | --- |
| 最终核心生产回归 | **357 passed，300.54 秒**。包含全部 `tests/test_codemode*.py`，以及 browser_click_plan、browser_entry_surface、browser_followups_production、receipt_bound_edit_ab、text_codemode_quote_boundary、task_contract_fixed_replies、frozen_tool_views。 |
| 全树 Ruff：`ruff check .` | 通过。最后一次检查前曾发现测试缺少 JsonObject 导入，首次失败保存在 ruff-final.log；修复后的通过结果保存在 ruff-final-02.log。 |
| 全生产树 mypy：`python -m mypy codey --python-version 3.11` | 692 个文件，通过。 |
| 全生产树 Pyrefly：`pyrefly check codey --python-version 3.11` | 0 errors；35 suppressed，158 warnings not shown。没有新增 suppression，不宣称零警告。 |
| 全生产树 ty：`ty check codey --python-version 3.11` | 通过。 |
| 本轮 19 个 Python 文件的额外 mypy 检查 | `--disallow-any-expr --disallow-any-explicit --disallow-untyped-defs --follow-imports=silent`，通过。没有 cast、type: ignore 或关闭检查。 |
| `python -m tools.kernel_type_precision_gate` | 通过，kernel Any counts 为零，typed contract escape checks 通过。 |
| 浏览器实验工具最后的局部清理 | 下载已结算后错误访问的单项回归 1 passed；仅移除多余的 dict 判断、对齐新增参数缩进，并对工具脚本重新进行严格类型检查。 |
| `git diff --check` | 通过。 |

最终核心批次退出码为 0。进程退出清理 RAM 临时目录时出现 WinError 3（目标文件已经不存在）的 RuntimeWarning，保留在 production-core-final.log；这不是测试断言或生产执行失败。

扩展实验批次不是全绿：production-01 为 23 failed / 366 passed，production-02 为 4 failed / 68 passed。上述核心批次的范围明确，不覆盖或抹去扩展批次失败。未修改的 browser_entry_candidates、browser_entry_followup 与 browser_chat_followup_gate 实验仍有旧输入或测试假设的问题；尤其后者的 request_snapshot hook 缺少当前生产签名中的 delivered_results 参数。对应生产入口与实验合同在本轮之前已存在，未扩大到这些实验的整体迁移。保存的模型原始回复文件没有改动。

日志与原始故障证据仅保留在本地 `.e2e-artifacts/codemode-20261115/`，不将 provider 请求、sqlite 或临时产物提交到 Git。

## 限制

未运行全量 pytest；未运行外部模型质量、安装包、跨平台或多 Python 版本实机验证。当前实机为 Windows Python 3.12，类型检查按项目 CI 的 Python 3.11 目标运行。取消对宿主工具仍是合作式的，无法强制中断任意不合作的 Python 回调；已提交 effect 的事实仍由 kernel 的 durable 结算负责。
